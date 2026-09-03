#!/usr/bin/env python3
"""
ds-audit — a design-system consistency audit of one screen, before and after.

The question is not "which of these are design-system components". Labelling the
components that *are* right proves nothing; the defect is an **absence** — someone
copied an older template and shipped a bare `<select>` where the standardised combo
belongs, and it looks close enough that review slides past it. So the audit is built
around the negative: know which *roles* the design system covers, then flag native
controls filling one of those roles that are **not** inside a DS component.

Green is context. Red is the product.

    ./ds-audit.py --base-new http://localhost:4300 --base-old http://localhost:4301 \
                  --screen "Book a visit=pets/11/visits/add" \
                  --screen "Edit a pet=pets/11/edit" \
                  --label-new test-pr --label-old main \
                  --source ../petclinic-frontend/src \
                  --assets assets -o assets/ds-audit.html --json assets/ds-audit.json

Several screens per run, because a migration touches one control per form: "it flagged
the bare one" is a weak claim, "it flagged *only* the bare one, and called the other
three right" is the one worth making.

The JSON is the artefact; the picture is its rendering. An adversarial review agent
reads `--json` and never has to OCR a PNG. Emit the stylesheet the fragment needs with
`--css`, the same way `openapi-diff.py` and friends do.

The one thing fixed with the design system is that a DS component marks its host with
`data-ds="<name>"`. Nothing here depends on its class names or its DOM shape.

Requires: playwright (`pip install playwright && playwright install chromium`),
Pillow and numpy for the pixel diff.
"""
from __future__ import annotations

import argparse

import datetime as _dt
import hashlib
import html
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCHEMA = "ds-audit/1"

# ── the page-side extractor ───────────────────────────────────────────────────────
#
# One `page.evaluate`, one pass over the DOM. Everything downstream — the registry, the
# audit, the DOM diff — is pure Python over this snapshot, which is what makes the whole
# thing testable without a browser.

SNAPSHOT_JS = r"""
() => {
  const MAX = 4000;

  // Angular writes validity and touch state into the class list. Those flip on a
  // stray focus and would report every input as "changed" between two runs.
  const VOLATILE = /^(ng-(untouched|touched|pristine|dirty|valid|invalid|star-inserted)|cdk-(focused|mouse-focused|keyboard-focused|program-focused)|mat-focus-indicator|_ng)/;

  const classesOf = (el) =>
    Array.from(el.classList).filter((c) => !VOLATILE.test(c)).sort();

  // A widget the design system could plausibly stand in for. Hidden inputs and buttons
  // are not "fields"; a submit button is not a combo in disguise.
  //
  // ARIA widgets count as well as elements. A component kit whose picker is a div with
  // `role="combobox"` is answering the same question a `<select>` answers, and an audit
  // that only knew about tag names would be silent about it — not "considered and let
  // past", *silent*, which is the answer a reviewer cannot check.
  const ARIA_WIDGET = ['combobox', 'listbox', 'spinbutton', 'slider', 'searchbox',
                       'textbox', 'switch'];

  const isNative = (el) => {
    const t = el.tagName.toLowerCase();
    if (t === 'select' || t === 'textarea') return true;
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      return !['hidden', 'button', 'submit', 'reset', 'image'].includes(ty);
    }
    return ARIA_WIDGET.includes((el.getAttribute('role') || '').toLowerCase());
  };

  const roleOf = (el) => {
    const t = el.tagName.toLowerCase();
    // A multi-select is not a single select wearing a hat, and a design system that has
    // a combo does not thereby have a multi-select. Reported as the same role, the one
    // control a team deliberately left native comes back red every run until somebody
    // switches the audit off — which is the failure this whole role model guards against.
    if (t === 'select') return el.multiple ? 'select[multiple]' : 'select';
    if (t === 'textarea') return 'textarea';
    if (t === 'input') return 'input[type=' + (el.getAttribute('type') || 'text').toLowerCase() + ']';
    return 'role=' + (el.getAttribute('role') || '').toLowerCase();
  };

  // NEVER `el.id`. On a <form>, the id *property* is the named child control — a
  // petclinic form with <input name="id"> answers `[object HTMLInputElement]`, and every
  // signature under it is garbage that matches nothing on the other side.
  const idOf = (el) => el.getAttribute('id') || '';

  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (idOf(el)) {
      const l = document.querySelector('label[for="' + CSS.escape(idOf(el)) + '"]');
      if (l) return l.textContent.trim();
    }
    const own = el.closest('label');
    if (own) return own.textContent.trim();
    const group = el.closest('.form-group, .field, .form-field, [class*=field]');
    if (group) {
      const l = group.querySelector('label');
      if (l) return l.textContent.trim();
    }
    return (el.getAttribute('placeholder') || '').trim();
  };

  // A path that survives the branch. An id or a form control name is worth more than
  // any position, so climbing stops at the first one: `select#vetId` says the same
  // thing on both sides even when four wrappers appeared around it.
  const sigOf = (el) => {
    const parts = [];
    let node = el;
    while (node && node !== document.body && node.nodeType === 1) {
      const t = node.tagName.toLowerCase();
      const ds = node.getAttribute('data-ds');
      if (idOf(node)) { parts.unshift(t + '#' + idOf(node)); break; }
      const name = node.getAttribute('name') || node.getAttribute('formcontrolname');
      if (name) { parts.unshift(t + '[name=' + name + ']'); node = node.parentElement; continue; }
      if (ds) {
        // A `data-ds` host rarely carries an id, and an nth-of-type index would make it
        // a *different* element the moment a field is inserted above it — reported as
        // one removed and one added, which is the noise this whole diff exists to avoid.
        // The control it wraps does have a name, and that is the host's real identity.
        const inner = node.querySelector('select[id],select[name],input[id],input[name],textarea[id],textarea[name]');
        const anchor = inner ? '#' + (idOf(inner) || inner.getAttribute('name')) : '';
        parts.unshift(t + '[data-ds=' + ds + anchor + ']');
        node = node.parentElement; continue;
      }
      const cls = classesOf(node).slice(0, 2);
      let nth = 1;
      let sib = node.previousElementSibling;
      while (sib) { if (sib.tagName === node.tagName) nth++; sib = sib.previousElementSibling; }
      parts.unshift(t + (cls.length ? '.' + cls.join('.') : '') + ':' + nth);
      node = node.parentElement;
    }
    return parts.join('>');
  };

  const selectorOf = (el) => {
    if (idOf(el)) return '#' + CSS.escape(idOf(el));
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
    const ds = el.getAttribute('data-ds');
    if (ds) {
      // A DS host almost never has an id, and its full path is unreadable in a table.
      // The control it wraps names it, and `:has()` turns that into a selector the
      // reader can paste straight into devtools.
      const inner = el.querySelector('[id],[name]');
      const anchor = inner && idOf(inner) ? ':has(#' + CSS.escape(idOf(inner)) + ')'
        : inner && inner.getAttribute('name') ? ':has([name="' + inner.getAttribute('name') + '"])' : '';
      return '[data-ds="' + ds + '"]' + anchor;
    }
    return sigOf(el);
  };

  const out = [];
  const all = document.body.querySelectorAll('*');
  for (let i = 0; i < all.length && out.length < MAX; i++) {
    const el = all[i];
    const t = el.tagName.toLowerCase();
    if (t === 'script' || t === 'style' || t === 'link' || t === 'template') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;

    const dsHostEl = el.parentElement && el.parentElement.closest('[data-ds]');
    // Text is the DOM's own answer to "did this change"; capped so a table does not
    // turn the snapshot into a copy of the page.
    const text = (el.children.length === 0 ? el.textContent : '').trim().slice(0, 120);

    out.push({
      sig: sigOf(el),
      selector: selectorOf(el),
      tag: t,
      id: idOf(el) || null,
      name: el.getAttribute('name') || el.getAttribute('formcontrolname') || null,
      type: t === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : null,
      aria_role: el.getAttribute('role') || null,
      cls: classesOf(el),
      ds: el.getAttribute('data-ds'),
      ds_covers: el.getAttribute('data-ds-covers'),
      ds_host: dsHostEl ? dsHostEl.getAttribute('data-ds') : null,
      ds_host_sig: dsHostEl ? sigOf(dsHostEl) : null,
      native: isNative(el),
      role: isNative(el) ? roleOf(el) : null,
      label: isNative(el) || el.getAttribute('data-ds') ? labelOf(el) : '',
      disabled: el.disabled === true,
      leaf: el.children.length === 0,
      // Page coordinates, not viewport ones: the shot is full-page.
      box: { x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
             w: Math.round(r.width), h: Math.round(r.height) },
      text: text,
    });
  }
  return {
    url: location.href,
    title: document.title,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    page: { w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight },
    nodes: out,
  };
}
"""

# ── determinism ───────────────────────────────────────────────────────────────────
#
# Two runs of the same screen differ in more ways than anyone expects, and every one of
# them lands in the pixel diff as a finding. Each pin below closes one of them, and each
# is reported in the JSON so the reader can see what was frozen rather than trust it.

PIN_JS = r"""
(() => {
  const FIXED = __EPOCH__;
  // A form that defaults its date field to "today" renders differently in two runs that
  // straddle midnight, and a relative timestamp ("2 minutes ago") differs every run.
  const RealDate = Date;
  function FrozenDate(...args) {
    if (args.length === 0) return new RealDate(FIXED);
    return new RealDate(...args);
  }
  FrozenDate.prototype = RealDate.prototype;
  FrozenDate.now = () => FIXED;
  FrozenDate.parse = RealDate.parse;
  FrozenDate.UTC = RealDate.UTC;
  window.Date = FrozenDate;

  // Mulberry32. Anything that seeds an id, a placeholder or a shuffle off Math.random
  // gets the same sequence on both sides.
  let seed = __SEED__ >>> 0;
  Math.random = function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  const kill = () => {
    if (!document.head) return;
    const s = document.createElement('style');
    s.id = 'ds-audit-pins';
    // A ripple mid-flight, a spinner, a blinking caret: all three are a different
    // picture 40ms later, and none of them is the change under review.
    s.textContent =
      '*,*::before,*::after{animation:none!important;transition:none!important;' +
      'caret-color:transparent!important;scroll-behavior:auto!important}' +
      'html{scrollbar-width:none}::-webkit-scrollbar{width:0;height:0}';
    document.head.appendChild(s);
  };
  if (document.head) kill();
  else document.addEventListener('DOMContentLoaded', kill);
})();
"""

PINS = [
    "viewport and deviceScaleFactor fixed (no retina resampling between machines)",
    "colour scheme, reduced-motion and locale forced identical on both sides",
    "wall clock frozen — a form defaulting to \"today\" is the same date on both sides",
    "Math.random seeded (mulberry32) — same sequence, same generated ids",
    "animations, transitions and the text caret disabled; scrollbars given no width",
    "screenshot taken with Playwright's animations=\"disabled\"",
    "settle loop: shot repeatedly until two consecutive frames are byte-identical",
    "same browser build drives both sides in one process — same fonts, same rasteriser",
    "both sides read the same backend in the same run, so row order and ids match",
]


# ── the role model ────────────────────────────────────────────────────────────────
#
# A hand-written list of "roles the design system covers" is wrong the day the second
# component lands and nobody remembers this file exists. So it is derived, from four
# sources in descending order of authority, and every role carries the provenance that
# admitted it. Adding `data-ds="datepicker"` to a component needs no code here.

# Used only when a `data-ds` name is present but nothing else says what it covers. It is
# a guess, it is labelled a guess in the output, and it exists so the audit degrades to
# "probably right and visibly unsure" rather than to silence.
FALLBACK_LEXICON = {
    "combo": ["select"],
    "combobox": ["select"],
    "select": ["select"],
    "dropdown": ["select"],
    "datepicker": ["input[type=date]"],
    "textfield": ["input[type=text]"],
    "textarea": ["textarea"],
    "checkbox": ["input[type=checkbox]"],
    "radio": ["input[type=radio]"],
}

# Tags a template scan is allowed to read a role off. Kept in step with `isNative` in
# the page-side extractor: a hidden input is not a field.
_SOURCE_CONTROL = re.compile(
    r"<(select|textarea)\b([^>]*)|<input\b([^>]*)", re.I)
_TYPE_ATTR = re.compile(r"""\btype\s*=\s*["']?([a-zA-Z-]+)""")
_SKIP_TYPES = {"hidden", "button", "submit", "reset", "image"}


def _role_of_tag(tag: str, type_: str | None) -> str:
    return f"input[type={type_ or 'text'}]" if tag == "input" else tag


def primary_control(host_sig: str, ds: str, nodes: list[dict]) -> list[dict]:
    """The native control a DS host *exposes* — not everything inside it.

    A combobox built on an input plus a listbox also contains a search box and, in some
    kits, a hidden mirror. Admitting all of them would make every text field on the page
    a violation of a component that has nothing to do with text fields, which is the
    false positive that gets an audit like this switched off in week two.

    A `<select>` wins outright when one is present: it is unambiguous about where the
    value lives. Otherwise the first visible form control in document order is taken.
    """
    inner = [n for n in nodes
             if n.get("ds_host") == ds and n.get("ds_host_sig") == host_sig
             and n.get("native") and not n.get("ds")]
    if not inner:
        return []
    selects = [n for n in inner if n["tag"] == "select"]
    return [selects[0]] if selects else [inner[0]]


def derive_registry(snapshots: dict, source_roots: list[Path]) -> dict:
    """Which roles the design system covers, and who says so.

    Precedence, highest first:

    1. `data-ds-covers="select,input[type=date]"` — the component author said it out loud.
    2. **runtime** — the control a rendered DS host actually wraps. The component's own
       implementation is the honest answer to "what does it replace".
    3. **source** — the same reading, taken off the template, for a component that this
       screen does not happen to render.
    4. **lexicon** — a guess from the name, marked as one.

    The registry is built from *both* sides at once and applied to both. That is the
    point: the branch that introduced the combo teaches the audit what a combo covers,
    and the base gets measured against it too, so a migration reads as an improvement
    and a straggler reads as a gap.
    """
    components: dict[str, dict] = {}

    def note(ds: str, roles, provenance: str, detail: str = ""):
        comp = components.setdefault(
            ds, {"ds": ds, "roles": [], "provenance": "unknown", "detail": "",
                 "seen_on": []})
        # First writer wins — the passes below run in precedence order — so the
        # provenance recorded is the one that actually admitted the first role.
        first = not comp["roles"]
        for r in roles:
            if r not in comp["roles"]:
                comp["roles"].append(r)
        if first and comp["roles"]:
            comp["provenance"], comp["detail"] = provenance, detail

    # A ds name may exist with no roles yet (rendered but empty, or source-only). Record
    # it anyway — a component nobody can attribute a role to is itself worth showing.
    for side, snap in snapshots.items():
        for n in snap["nodes"]:
            if n.get("ds"):
                comp = components.setdefault(
                    n["ds"], {"ds": n["ds"], "roles": [], "provenance": "unknown",
                              "detail": "", "seen_on": []})
                if side not in comp["seen_on"]:
                    comp["seen_on"].append(side)

    # 1 — declared
    for side, snap in snapshots.items():
        for n in snap["nodes"]:
            if n.get("ds") and n.get("ds_covers"):
                roles = [r.strip() for r in n["ds_covers"].split(",") if r.strip()]
                if roles:
                    note(n["ds"], roles, "declared", f'data-ds-covers on {n["selector"]}')

    # 2 — runtime
    for side, snap in snapshots.items():
        for n in snap["nodes"]:
            if not n.get("ds"):
                continue
            comp = components.get(n["ds"], {})
            if comp.get("provenance") == "declared":
                continue
            for inner in primary_control(n["sig"], n["ds"], snap["nodes"]):
                note(n["ds"], [inner["role"]], f"runtime:{side}",
                     f'<{inner["tag"]}> inside {n["selector"]}')

    # 3 — source
    src = scan_sources(source_roots)
    for ds, found in src.items():
        comp = components.setdefault(
            ds, {"ds": ds, "roles": [], "provenance": "unknown", "detail": "",
                 "seen_on": []})
        if comp["roles"]:
            continue
        note(ds, found["roles"], "source", found["file"])

    # 4 — lexicon
    for ds, comp in components.items():
        if comp["roles"]:
            continue
        guess = FALLBACK_LEXICON.get(ds.lower())
        if guess:
            comp["roles"] = list(guess)
            comp["provenance"] = "lexicon"
            comp["detail"] = f'no implementation found; guessed from the name "{ds}"'

    roles: dict[str, dict] = {}
    for comp in components.values():
        for r in comp["roles"]:
            entry = roles.setdefault(
                r, {"role": r, "covered_by": [], "provenance": comp["provenance"]})
            if comp["ds"] not in entry["covered_by"]:
                entry["covered_by"].append(comp["ds"])
    return {
        "components": sorted(components.values(), key=lambda c: c["ds"]),
        "roles": sorted(roles.values(), key=lambda r: r["role"]),
    }


def scan_sources(roots: list[Path]) -> dict:
    """Find every `data-ds="x"` in the tree and read the controls its template renders.

    This is what keeps a component honest when the audited screen does not render it:
    the datepicker that only appears on the edit form still teaches the registry that
    `input[type=date]` is spoken for.
    """
    found: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*")
            if p.suffix.lower() in (".html", ".ts", ".tsx", ".jsx", ".vue", ".svelte")
            and "node_modules" not in p.parts)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            names = set(re.findall(r"""data-ds\s*=\s*["']([\w-]+)["']""", text))
            if not names:
                continue
            roles = []
            for m in _SOURCE_CONTROL.finditer(text):
                if m.group(1):
                    tag = m.group(1).lower()
                    role = ("select[multiple]"
                            if tag == "select" and re.search(r"\bmultiple\b", m.group(2) or "")
                            else tag)
                else:
                    ty = _TYPE_ATTR.search(m.group(3) or "")
                    t = (ty.group(1).lower() if ty else "text")
                    if t in _SKIP_TYPES:
                        continue
                    role = _role_of_tag("input", t)
                if role not in roles:
                    roles.append(role)
            # Same rule as at runtime: a select settles it, otherwise the first control.
            if "select" in roles:
                roles = ["select"]
            elif "select[multiple]" in roles:
                roles = ["select[multiple]"]
            elif roles:
                roles = roles[:1]
            for ds in names:
                if roles and ds not in found:
                    found[ds] = {"roles": roles, "file": str(path)}
    return found


# ── the audit ─────────────────────────────────────────────────────────────────────

def audit_side(snapshot: dict, registry: dict, side: str) -> list[dict]:
    """One side's verdicts. Three of them, and only two get drawn.

    `ds` — a design-system host. Green, and it is context, not a finding.
    `bare` — a native control in a covered role with no `[data-ds]` above it. Red.
    `internal` — the same native control, but inside a DS host: that is the component's
                 own machinery and marking it would bury the one badge that matters.
    `uncovered` — a native control in a role no DS component claims. Recorded, never
                 drawn: it is the auditor showing its work, not a finding.
    """
    covered = {r["role"]: r for r in registry["roles"]}
    out = []
    for n in snapshot["nodes"]:
        if n.get("ds"):
            out.append(_finding(side, n, "ds", None,
                                f'design-system component <b>{n["ds"]}</b>'))
            continue
        if not n.get("native"):
            continue
        role = covered.get(n["role"])
        if not role:
            # Considered and not judged. It is in the JSON so the agent reading it can
            # see the auditor looked at this control and can say why it let it past —
            # an audit that only reports what it flagged cannot be argued with. Never
            # badged on the picture: that is what "leave everything else unmarked" means.
            claimed = ", ".join(f'<code>{r}</code>' for r in sorted(covered)) or "none"
            out.append(_finding(side, n, "uncovered", None,
                                f'<code>&lt;{n["tag"]}&gt;</code> in role '
                                f'<code>{n["role"]}</code>. No design-system component '
                                f'claims that role \u2014 the registry covers {claimed} \u2014 '
                                "so this control is considered and deliberately not judged"))
            continue
        if n.get("ds_host"):
            out.append(_finding(side, n, "internal", role,
                                f'inside the <b>{n["ds_host"]}</b> component — its own control'))
            continue
        owners = " or ".join(f"<b>{d}</b>" for d in role["covered_by"])
        out.append(_finding(
            side, n, "bare", role,
            f'native <code>&lt;{n["tag"]}&gt;</code> in a role the design system covers '
            f'({owners}), and it is not inside any <code>[data-ds]</code> host'))
    return out


def _finding(side: str, n: dict, verdict: str, role: dict | None, message: str) -> dict:
    return {
        "id": f'{side}:{n["sig"]}',
        "side": side,
        "verdict": verdict,
        "role": (role or {}).get("role") or n.get("role"),
        "expected_ds": (role or {}).get("covered_by") or [],
        "ds": n.get("ds"),
        "element": {"tag": n["tag"], "id": n.get("id"), "name": n.get("name"),
                    "label": n.get("label") or "", "sig": n["sig"]},
        "selector": n["selector"],
        "box": n["box"],
        "message": message,
    }


# ── the comparison: DOM decides, pixels corroborate ───────────────────────────────

def dom_delta(old: dict, new: dict) -> dict:
    """What the DOM says moved, keyed on the signature rather than on position.

    The signature stops climbing at the first id or control name, so four new wrapper
    divs around a field do not make it a different field.
    """
    o = {n["sig"]: n for n in old["nodes"]}
    v = {n["sig"]: n for n in new["nodes"]}
    added = set(v) - set(o)
    removed = set(o) - set(v)

    # Second pass, and it is what stops the diff crying wolf. A signature still carries
    # some position, so a field inserted above an unnamed wrapper renumbers it and the
    # set difference reports one element removed and one added — twice the noise, in the
    # place the reader is meant to be looking. Anything left over is re-paired on its own
    # identity (what it is, what it is called, what it says), but only where that
    # identity is unique on both sides: an ambiguous match is worse than none.
    def named(n):
        """Strong identity: what the element *is*, with nothing about what it says. An
        element with an id, a control name or a `data-ds` is the same element on both
        sides even when its label was rewritten — which is a change, not a replacement."""
        if not (n.get("id") or n.get("name") or n.get("ds")):
            return None
        return (n["tag"], n.get("id"), n.get("name"), n.get("ds"), n.get("aria_role"))

    def spoken(n):
        """Weak identity, for the anonymous majority — a `<td>`, a `<label>`. All they
        have is what they say, so an anonymous element whose text changed is honestly
        indistinguishable from a new one and stays reported as added."""
        return (n["tag"], n.get("aria_role"), (n.get("label") or "")[:40],
                n.get("text", "")[:40])

    def unique(sigs, table, key_of):
        seen = {}
        for sig in sigs:
            key = key_of(table[sig])
            if key is None:
                continue
            seen[key] = None if key in seen else sig
        return {k: sig for k, sig in seen.items() if sig}

    paired = {}
    for key_of in (named, spoken):
        left = unique(removed - set(paired), o, key_of)
        right = unique(added - set(paired.values()), v, key_of)
        for key, old_sig in left.items():
            if right.get(key):
                paired[old_sig] = right[key]
    removed -= set(paired)
    added -= set(paired.values())

    changed = {sig for sig in set(o) & set(v) if _digest(o[sig]) != _digest(v[sig])}
    for old_sig, new_sig in paired.items():
        if _digest(o[old_sig]) != _digest(v[new_sig]):
            changed.add(new_sig)
    return {"added": sorted(added), "removed": sorted(removed),
            "changed": sorted(changed), "moved": {k: v for k, v in paired.items()}}


def _digest(n: dict) -> str:
    payload = json.dumps({k: n.get(k) for k in
                          ("tag", "id", "name", "type", "aria_role", "cls", "ds",
                           "ds_covers", "ds_host", "text", "label", "disabled")},
                         sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _yiq_mask(a, b, threshold: float = 0.1):
    """pixelmatch's perceptual comparison, in numpy, plus one erosion pass.

    The YIQ weighting is pixelmatch's (`maxDelta = 35215`): it is what stops a hairline
    hue shift counting as much as a control appearing. The erosion is the cheap half of
    what pixelmatch's antialias detector buys — a differing pixel counts only if most of
    its neighbours differ too, which deletes the one-pixel fringe that text rendering
    leaves along every glyph and keeps solid regions intact.
    """
    import numpy as np

    a = a.astype("float32")
    b = b.astype("float32")

    def yiq(x):
        r, g, bl = x[..., 0], x[..., 1], x[..., 2]
        return (r * 0.29889531 + g * 0.58662247 + bl * 0.11448223,
                r * 0.59597799 - g * 0.27417610 - bl * 0.32180189,
                r * 0.21147017 - g * 0.52261711 + bl * 0.31114694)

    ya, ia, qa = yiq(a)
    yb, ib, qb = yiq(b)
    delta = (0.5053 * (ya - yb) ** 2 + 0.299 * (ia - ib) ** 2 + 0.1957 * (qa - qb) ** 2)
    raw = delta > (35215 * threshold * threshold)
    if raw.shape[0] < 3 or raw.shape[1] < 3:
        return raw
    padded = np.pad(raw, 1, constant_values=False).astype("uint8")
    neighbours = sum(padded[dy:dy + raw.shape[0], dx:dx + raw.shape[1]]
                     for dy in (0, 1, 2) for dx in (0, 1, 2)
                     if not (dy == 1 and dx == 1))
    return raw & (neighbours >= 4)


def pixel_delta(old_png: Path, new_png: Path, out_png: Path | None,
                threshold: float = 0.1, explained: list | None = None):
    """The whole-page mask, painted over the new shot — minus what structure explains.

    A raw whole-page diff of a form with one field inserted is magenta from that field
    to the footer, because everything below it moved 40px down. That picture is true and
    useless: it is one change reported a hundred times, and the eye cannot find the one
    that matters inside it.

    So the paint is subtracted. `explained` is the boxes of elements the DOM paired
    across the two sides and that are byte-identical when each is cropped on *its own*
    box — an element that only moved. What is left is the residue: pixels that differ
    for a reason structure did not account for. Both layers are kept, the explained one
    at a fraction of the strength, because "this moved" is still worth seeing faintly.
    """
    import numpy as np
    from PIL import Image

    a = Image.open(old_png).convert("RGB")
    b = Image.open(new_png).convert("RGB")
    h, w = min(a.height, b.height), min(a.width, b.width)
    na = np.asarray(a)[:h, :w]
    nb = np.asarray(b)[:h, :w]
    mask = _yiq_mask(na, nb, threshold)
    if out_png is not None:
        canvas = np.asarray(b.convert("RGB")).copy()
        full = np.zeros(canvas.shape[:2], dtype=bool)
        full[:h, :w] = mask
        moved = np.zeros(canvas.shape[:2], dtype=bool)
        for box in explained or []:
            y0, x0 = max(box["y"], 0), max(box["x"], 0)
            y1, x1 = min(box["y"] + box["h"], moved.shape[0]), min(box["x"] + box["w"], moved.shape[1])
            if y1 > y0 and x1 > x0:
                moved[y0:y1, x0:x1] = True
        residue = full & ~moved
        # Dim everything nobody touched, so the delta is what the eye lands on.
        out = (canvas * 0.35 + 255 * 0.65).astype("float32")
        out[full & moved] = np.array([245, 205, 232], dtype="float32")
        out[residue] = np.array([214, 31, 165], dtype="float32")
        Image.fromarray(out.astype("uint8")).save(out_png)
    return mask


def churn(mask, box: dict) -> float:
    """The share of one element's box that differs. `mask` is already registered on the
    page origin; boxes that moved are handled by the caller, which crops each side on
    its own box before asking."""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    y2, x2 = min(y + h, mask.shape[0]), min(x + w, mask.shape[1])
    if y >= y2 or x >= x2:
        return 0.0
    region = mask[max(y, 0):y2, max(x, 0):x2]
    return float(region.mean()) if region.size else 0.0


def registered_churn(old_png, new_png, old_box, new_box, threshold=0.1) -> float:
    """The same element on both sides, each cropped on *its own* box and compared with
    the two crops aligned at their top-left corner.

    This is the whole answer to "pixels drown you in layout shift". A field that moved
    40px down because a paragraph grew above it is byte-identical to itself; compared in
    absolute page coordinates it is 100% different, and so is everything below it.
    """
    import numpy as np
    from PIL import Image

    a = Image.open(old_png).convert("RGB").crop(
        (old_box["x"], old_box["y"], old_box["x"] + old_box["w"], old_box["y"] + old_box["h"]))
    b = Image.open(new_png).convert("RGB").crop(
        (new_box["x"], new_box["y"], new_box["x"] + new_box["w"], new_box["y"] + new_box["h"]))
    h, w = min(a.height, b.height), min(a.width, b.width)
    if h < 1 or w < 1:
        return 1.0
    mask = _yiq_mask(np.asarray(a)[:h, :w], np.asarray(b)[:h, :w], threshold)
    # A box that changed size is a change in its own right, and the crop only compared
    # the overlap; charge the missing area to the difference.
    overlap = (h * w) / max(a.height * a.width, b.height * b.width, 1)
    return float(mask.mean()) * overlap + (1 - overlap)


# How the two are weighted, and it is not a blend. The DOM answers "which element", the
# pixels answer "did it look different"; a number that averaged them would answer
# neither. So: structure decides membership, pixels only get a vote in the one case
# structure is blind to — same element, same attributes, different picture.
RESTYLE_CHURN = 0.12
# Below this, an element cropped on its own box is the same picture on both sides: it
# moved and nothing else. Not zero, because a subpixel reflow leaves a thread of fringe.
MOVED_ONLY_CHURN = 0.01


def combine(old_snap, new_snap, dom, old_png, new_png, threshold) -> dict:
    """Per-signature status, DOM first."""
    o = {n["sig"]: n for n in old_snap["nodes"]}
    v = {n["sig"]: n for n in new_snap["nodes"]}
    status, explained = {}, []
    for sig in dom["added"]:
        status[sig] = {"dom": "added", "pixel_churn": None, "status": "added"}
    for sig in dom["removed"]:
        status[sig] = {"dom": "removed", "pixel_churn": None, "status": "removed"}
    pairs = [(sig, sig) for sig in set(o) & set(v)]
    pairs += list(dom.get("moved", {}).items())
    changed = set(dom["changed"])
    for old_sig, sig in pairs:
        d = "changed" if sig in changed else "same"
        entry = {"dom": d, "pixel_churn": None, "status": d}
        node = v[sig]
        # Leaves as well as controls: the leaves are where the ink is, and they are what
        # tells the delta picture which magenta is just a shifted paragraph.
        if node.get("native") or node.get("ds") or node.get("leaf"):
            c = registered_churn(old_png, new_png, o[old_sig]["box"], node["box"], threshold)
            entry["pixel_churn"] = round(c, 4)
            if c <= MOVED_ONLY_CHURN:
                explained.append(node["box"])
            if d == "same" and c > RESTYLE_CHURN:
                # The visual twist DOM alone misses: a control the branch restyled
                # without touching a single attribute the snapshot records.
                entry["status"] = "restyled"
        status[sig] = entry
    return status, explained


# ── the fragment ──────────────────────────────────────────────────────────────────

def _build_review():
    spec = importlib.util.spec_from_file_location(
        "build_review", HERE / "build-review-html.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CSS = """/* ds-audit — the annotated screenshots and the findings table, and nothing else:
   the three-state viewer they sit in belongs to the report, and a fragment that
   restyled it would be a second implementation of it wearing a hat. */
.dsa { margin: 1rem 0 2rem; }
.dsa-shot { position: relative; line-height: 0; }
.dsa-shot img { width: 100%; height: auto; display: block; border-radius: .3rem; }
.dsa-mark { position: absolute; box-sizing: border-box; border-radius: .2rem; pointer-events: auto; }
.dsa-mark.ok  { border: 2px solid var(--dsa-ok); background: color-mix(in srgb, var(--dsa-ok) 10%, transparent); }
.dsa-mark.bad { border: 3px solid var(--dsa-bad); background: color-mix(in srgb, var(--dsa-bad) 14%, transparent); }
.dsa-mark.new { border: 2px dashed var(--dsa-new); background: transparent; }
.dsa-mark b { position: absolute; left: 0; top: -1.15rem; font: 700 .68rem/1.15rem
  -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #fff;
  padding: 0 .35rem; border-radius: .2rem; white-space: nowrap; }
.dsa-mark.ok b  { background: var(--dsa-ok); }
.dsa-mark.bad b { background: var(--dsa-bad); }
.dsa-mark.new b { background: var(--dsa-new); }
.dsa-mark.hot { outline: 3px solid var(--dsa-hot); outline-offset: 3px; }
.dsa-legend { display: flex; gap: 1.2rem; flex-wrap: wrap; margin: .5rem 0 .3rem;
  font-size: .82rem; align-items: center; }
.dsa-legend i { width: .85rem; height: .85rem; border-radius: .2rem; display: inline-block;
  vertical-align: -.1rem; margin-right: .3rem; }
.dsa-legend .k-ok i  { background: var(--dsa-ok); }
.dsa-legend .k-bad i { background: var(--dsa-bad); }
.dsa-legend .k-new i { background: var(--dsa-new); }
.dsa-legend .dsa-ink i { background: #d61fa5; }
.dsa-legend .dsa-ghost i { background: #f5cde8; border: 1px solid #d61fa5; }
.dsa-legend .dsa-ink, .dsa-legend .dsa-ghost { display: inline-flex;
  align-items: center; gap: .3rem; }
.dsa-table { width: 100%; border-collapse: collapse; margin-top: .8rem; font-size: .86rem; }
.dsa-table th { text-align: left; font-weight: 700; border-bottom: 2px solid currentColor;
  padding: .3rem .5rem; opacity: .75; }
.dsa-table td { padding: .35rem .5rem; border-bottom: 1px solid rgba(128,128,128,.28);
  vertical-align: top; }
.dsa-table tr.bad td:first-child { border-left: 4px solid var(--dsa-bad); }
.dsa-table tr.ok  td:first-child { border-left: 4px solid var(--dsa-ok); }
.dsa-table tr:hover { background: rgba(128,128,128,.10); }
.dsa-v { font-weight: 700; text-transform: uppercase; font-size: .72rem; letter-spacing: .04em; }
.dsa-v.bad { color: var(--dsa-bad); }
.dsa-v.ok  { color: var(--dsa-ok); }
.dsa-table tr.ok td:first-child { border-left: 4px solid var(--dsa-ok); }
.dsa-reg { font-size: .82rem; margin: .6rem 0; }
.dsa-reg code { font-size: .95em; }
.dsa-prov { opacity: .7; font-style: italic; }
.dsa-sel { font-size: .78rem; opacity: .72; word-break: break-all; }
.dsa-none { opacity: .7; font-style: italic; }
.dsa-considered { margin: .6rem 0 0; font-size: .84rem; opacity: .85; }
.dsa-considered summary { cursor: pointer; }
.dsa-considered ul { margin: .4rem 0 0 .2rem; }
.dsa-hdr { display: flex; gap: .8rem; align-items: baseline; flex-wrap: wrap; }
.dsa-hdr .dsa-count { font-weight: 700; }
:root { --dsa-ok: #1f7a45; --dsa-bad: #c1121f; --dsa-new: #1a4fa0; --dsa-hot: #f0a500; }
@media (prefers-color-scheme: dark) {
  :root { --dsa-ok: #46c07a; --dsa-bad: #ff6b6b; --dsa-new: #7aa9ef; --dsa-hot: #ffc94d; }
  .dsa-shot img { filter: none; }
}
"""

HL_JS = """<script>
// Hovering a row lights its box on the picture. Delegated on `document` for the same
// reason the diagram viewer is: the fragment is pasted into a page it does not own.
(function () {
  function marks(root, id) {
    return root.querySelectorAll('.dsa-mark[data-find="' + CSS.escape(id) + '"]');
  }
  document.addEventListener('mouseover', function (ev) {
    var tr = ev.target.closest && ev.target.closest('.dsa-table tr[data-find]');
    if (!tr) return;
    var scope = tr.closest('.dsa');
    if (!scope) return;
    scope.querySelectorAll('.dsa-mark.hot').forEach(function (m) { m.classList.remove('hot'); });
    marks(scope, tr.getAttribute('data-find')).forEach(function (m) { m.classList.add('hot'); });
  });
  document.addEventListener('mouseout', function (ev) {
    var tr = ev.target.closest && ev.target.closest('.dsa-table tr[data-find]');
    if (!tr) return;
    var scope = tr.closest('.dsa');
    if (scope) scope.querySelectorAll('.dsa-mark.hot').forEach(function (m) { m.classList.remove('hot'); });
  });
})();
</script>"""


def _pct(v, total):
    return f"{(v / total * 100):.4f}%" if total else "0%"


def shot_html(png_rel: str, page: dict, marks: list[dict]) -> str:
    """A screenshot with boxes over it, positioned in percentages so the picture stays
    responsive — the report is read on a laptop and on a projector."""
    w, h = max(page["w"], 1), max(page["h"], 1)
    out = [f'<div class="dsa-shot"><img src="{html.escape(png_rel)}" alt="" loading="lazy">']
    for m in marks:
        b = m["box"]
        style = (f'left:{_pct(b["x"], w)};top:{_pct(b["y"], h)};'
                 f'width:{_pct(b["w"], w)};height:{_pct(b["h"], h)}')
        out.append(
            f'<div class="dsa-mark {m["cls"]}" style="{style}" '
            f'data-find="{html.escape(m["id"])}" data-tip="{html.escape(m["tip"])}">'
            f'<b>{html.escape(m["badge"])}</b></div>')
    out.append("</div>")
    return "".join(out)


def _marks_for(findings, side):
    marks = []
    for f in findings:
        if f["side"] != side:
            continue
        if f["verdict"] in ("internal", "uncovered"):
            continue
        st = f.get("delta", {})
        note = ""
        if st.get("status") in ("added", "restyled", "changed"):
            note = f' · {st["status"]}'
        if f["verdict"] == "ds":
            marks.append({"id": f["id"], "cls": "ok", "box": f["box"],
                          "badge": f'✓ {f["ds"]}{note}',
                          "tip": re.sub("<[^>]+>", "", f["message"])})
        else:
            expect = "/".join(f["expected_ds"]) or "a design-system component"
            marks.append({"id": f["id"], "cls": "bad", "box": f["box"],
                          "badge": f'✗ {f["role"]} → {expect}{note}',
                          "tip": re.sub("<[^>]+>", "", f["message"])})
    return marks


LEGEND = (
    '<div class="dsa-legend">'
    '<span class="k-ok"><i></i>is a design-system component</span>'
    '<span class="k-bad"><i></i>native control in a role the DS covers, outside any '
    '<code>[data-ds]</code></span>'
    '<span class="k-new"><i></i>new or changed on this branch</span>'
    '<span class="dsa-prov">everything else is deliberately unmarked</span>'
    "</div>")

DIFF_LEGEND = (
    '<div class="dsa-legend"><span class="k-new"><i></i>outlined: the DOM says this '
    'element is new or changed</span>'
    '<span class="dsa-ink"><i></i>differs, and structure does not explain it</span>'
    '<span class="dsa-ghost"><i></i>differs only because it moved</span>'
    '<span class="dsa-prov">faded: identical</span></div>')


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "screen"


def render_screen(screen: dict, assets_prefix: str, build) -> str:
    findings = screen["findings"]
    pages = {s: screen["sides"][s]["page"] for s in ("new", "old")}
    stem = f'{assets_prefix}ds-audit-{slug(screen["screen"])}'

    def annotated(side):
        return LEGEND + shot_html(f"{stem}-{side}.png", pages[side],
                                  _marks_for(findings, side))

    # The delta pane: the pixel mask over the new shot, with the elements the DOM says
    # are new or changed outlined on top of it. Neither half is enough on its own.
    delta_marks = []
    for f in findings:
        if f["side"] != "new" or f["verdict"] == "internal":
            continue
        st = f.get("delta", {})
        if st.get("status") in ("added", "changed", "restyled"):
            c = st.get("pixel_churn")
            churn_txt = "not comparable (no counterpart)" if c is None else f"{c:.0%}"
            delta_marks.append({
                "id": f["id"] + ":d", "cls": "new", "box": f["box"],
                "badge": f'{st["status"]}: {f["element"]["tag"]}'
                         + (f' #{f["element"]["id"]}' if f["element"]["id"] else ""),
                "tip": f'the DOM says {st["dom"]}; pixels differ over {churn_txt} '
                       "of the element\u2019s own box"})
    panes = [("diff", DIFF_LEGEND + shot_html(f"{stem}-delta.png", pages["new"], delta_marks)),
             ("new", annotated("new")), ("old", annotated("old"))]

    rows = []
    for f in sorted(findings, key=lambda f: (f["verdict"] != "bare", f["side"] != "new",
                                             f["box"]["y"])):
        if f["verdict"] in ("internal", "uncovered"):
            continue
        st = f.get("delta", {})
        cls = "bad" if f["verdict"] == "bare" and not f.get("resolved") else "ok"
        word = "gap" if cls == "bad" else ("fixed" if f.get("resolved") else "ok")
        churn_txt = "\u2014" if st.get("pixel_churn") is None else f'{st["pixel_churn"]:.0%}'
        rows.append(
            f'<tr class="{cls}" data-find="{html.escape(f["id"])}">'
            f'<td><span class="dsa-v {cls}">{word}</span></td>'
            f'<td>{html.escape(screen["sides"][f["side"]]["label"])}</td>'
            f'<td><b>{html.escape(f["element"]["label"] or f["element"]["id"] or f["element"]["tag"])}</b>'
            f'<br><code class="dsa-sel">{html.escape(f["selector"])}</code></td>'
            f'<td>{html.escape(f["role"] or "")}</td>'
            + f'<td>{f["message"]}'
            + (f'<br><span class="dsa-prov">{f["history"]}</span>'
               if f.get("history") else "") + '</td>'
            f'<td>{html.escape(st.get("status", "\u2014"))}</td>'
            f'<td>{churn_txt}</td></tr>')

    counts = screen["summary"]
    head = (f'<h3 id="dsa-{slug(screen["screen"])}">{html.escape(screen["screen"])}</h3>'
            f'<p class="dsa-hdr"><span class="dsa-count">{counts["new"]["bare"]}</span> gap'
            f'{"" if counts["new"]["bare"] == 1 else "s"} '
            f'\u00b7 {counts["new"]["ds"]} design-system component'
            f'{"" if counts["new"]["ds"] == 1 else "s"} in place'
            + (f' \u00b7 {len(counts["improvements"])} migrated by this branch'
               if counts["improvements"] else "")
            + '</p>')

    table = ('<table class="dsa-table"><thead><tr><th></th><th>side</th><th>element</th>'
             '<th>role</th><th>why</th><th>delta</th><th>churn</th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table>") if rows else (
        '<p class="dsa-none">Nothing on this screen is a design-system component or a '
        "gap where one belongs.</p>")

    # What the audit looked at and let past. "Nothing was flagged" is not a claim anyone
    # can check; "these five controls were considered, and here is the role each one
    # fills and why no component claims it" is. It is also the only place a reviewer sees
    # a control the team left native *on purpose* — a multi-select the design system has
    # no component for — being recognised as that rather than missed.
    passed = [f for f in findings if f["side"] == "new" and f["verdict"] == "uncovered"]
    considered = ""
    if passed:
        items = "".join(
            f'<li><code>{html.escape(f["selector"])}</code>'
            + (f' <b>{html.escape(f["element"]["label"])}</b>' if f["element"]["label"] else "")
            + f' \u2014 role <code>{html.escape(f["role"] or "")}</code>, not covered</li>'
            for f in passed)
        considered = (f'<details class="dsa-considered"><summary>{len(passed)} control'
                      f'{"" if len(passed) == 1 else "s"} considered and deliberately not '
                      f'judged</summary><ul>{items}</ul></details>')
    return f'<div class="dsa">{head}{build.dgm_views_html(panes)}{table}{considered}</div>'


def render(result: dict, assets_prefix: str) -> str:
    """The fragment: the registry once, then one three-state viewer per screen.

    The viewer is the report\u2019s own \u2014 the Diff / New-Old control built last round
    for exactly this shape of content. A second one with different ergonomics on the same
    page would be the mistake worth failing a build over.
    """
    build = _build_review()
    reg_rows = "".join(
        f'<li><code>data-ds="{html.escape(c["ds"])}"</code> covers '
        + (", ".join(f'<code>{html.escape(r)}</code>' for r in c["roles"])
           or '<span class="dsa-none">nothing we could determine</span>')
        + f' <span class="dsa-prov">\u2014 {html.escape(c["provenance"])}'
        + (f': {html.escape(c["detail"])}' if c["detail"] else "") + "</span></li>"
        for c in result["registry"]["components"])
    if not reg_rows:
        reg_rows = ('<li class="dsa-none">No <code>data-ds</code> component was found on '
                    "either side or in the sources scanned, so no role is claimed and "
                    "nothing can be called a gap. That is an empty registry, not a clean "
                    "bill of health.</li>")

    counts = result["summary"]
    verdict_line = (
        f'<span class="dsa-count">{counts["new"]["bare"]}</span> gap'
        f'{"" if counts["new"]["bare"] == 1 else "s"} across '
        f'{len(result["screens"])} screen{"" if len(result["screens"]) == 1 else "s"}, '
        f'{counts["new"]["ds"]} design-system component'
        f'{"" if counts["new"]["ds"] == 1 else "s"} in place'
        + (f' \u00b7 <b>{len(counts["regressions"])} regression'
           f'{"" if len(counts["regressions"]) == 1 else "s"}</b>'
           if counts["regressions"] else "")
        + (f' \u00b7 {len(counts["improvements"])} migrated by this branch'
           if counts["improvements"] else ""))

    # The embedded copy drops the per-element table. It is keyed on every signature on
    # every screen — 190KB of it on a seven-screen run, most of the fragment's weight —
    # and it is redundant here: each finding already carries its own `delta`. The file
    # written by --json keeps it, and the payload says where to find it.
    embedded = dict(result, screens=[
        dict(sc, delta={k: v for k, v in sc["delta"].items() if k != "elements"})
        for sc in result["screens"]])
    embedded["full_json"] = "the --json file beside this page carries delta.elements too"
    payload = json.dumps(embedded, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<div class="dsa-run">'
        f'<p class="dsa-hdr">{verdict_line}</p>'
        f'<div class="dsa-reg"><b>Roles the design system covers</b>, derived \u2014 not '
        "listed by hand, so a second component needs no change here:"
        f'<ul>{reg_rows}</ul></div>'
        + "".join(render_screen(sc, assets_prefix, build) for sc in result["screens"])
        + f'<script type="application/json" class="ds-audit-data">{payload}</script>'
        + HL_JS + "</div>")


# ── capture ───────────────────────────────────────────────────────────────────────

def capture(url: str, png: Path, *, viewport, epoch: int, seed: int, wait_for: str | None,
            settle_ms: int, mask: list[str], color_scheme: str) -> dict:
    from playwright.sync_api import sync_playwright

    pin = PIN_JS.replace("__EPOCH__", str(epoch)).replace("__SEED__", str(seed))
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-color-profile=srgb",
                                          "--font-render-hinting=none",
                                          "--disable-lcd-text"])
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]},
                                  device_scale_factor=1, color_scheme=color_scheme,
                                  reduced_motion="reduce", locale="en-GB",
                                  timezone_id="UTC")
        ctx.add_init_script(pin)
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        if wait_for:
            page.wait_for_selector(wait_for, timeout=30000, state="visible")
        page.wait_for_timeout(settle_ms)

        masks = [page.locator(m) for m in mask]
        # Shoot until two consecutive frames are identical. A single "wait 500ms and
        # hope" is where flaky before/after pairs come from.
        previous, identical = None, False
        for _ in range(8):
            shot = page.screenshot(full_page=True, animations="disabled", mask=masks,
                                   mask_color="#c8c8c8")
            if shot == previous:
                identical = True
                break
            previous = shot
            page.wait_for_timeout(settle_ms)
        png.write_bytes(previous)
        snap = page.evaluate(SNAPSHOT_JS)
        browser.close()
    snap["settled"] = identical
    snap["png"] = str(png)
    return snap


# ── main ──────────────────────────────────────────────────────────────────────────

def _git_head(path: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def build_screen(name, old_snap, new_snap, registry, *, sides_meta, delta) -> dict:
    """One screen's verdicts, before and after. A run audits several — three of the four
    controls the sibling migrated live on three different forms, and "it flagged only the
    right one" is a claim you cannot make from one screen."""
    findings = (audit_side(new_snap, registry, "new")
                + audit_side(old_snap, registry, "old"))

    # A field is not its signature. When a bare `<select id="timezone">` is migrated, the
    # green finding lands on the *wrapper* the branch introduced, whose signature has
    # nothing in common with the select's. Matched on signature, the base's gap and the
    # branch's fix look like two unrelated elements — the migration reads as neither an
    # improvement nor a regression, and the answer to "did this branch make it better or
    # worse" is silence. So the two sides are paired on the *field*: the control's own id
    # or name, reached through the DS host when there is one.
    field = {}
    for side, snapshot in (("new", new_snap), ("old", old_snap)):
        for f in findings:
            if f["side"] != side:
                continue
            node = f["element"]
            key = node["id"] or node["name"]
            if f["verdict"] == "ds":
                inner = primary_control(node["sig"], f["ds"], snapshot["nodes"])
                key = (inner[0]["id"] or inner[0]["name"]) if inner else None
            field[f["id"]] = key or node["sig"]
    # A DS host and the control inside it answer to the same field. The host is the one
    # that speaks for it — "this field is a combo" — so it wins the slot; without the
    # precedence the `internal` child overwrites it and a component the branch tore out
    # reads as a gap that was always there.
    rank = {"ds": 0, "bare": 1, "internal": 2, "uncovered": 3}
    by = {}
    for f in sorted(findings, key=lambda f: rank[f["verdict"]]):
        by.setdefault((f["side"], field[f["id"]]), f)

    def count(side):
        c = {"ds": 0, "bare": 0, "internal": 0, "uncovered": 0}
        for f in findings:
            if f["side"] == side:
                c[f["verdict"]] += 1
        return c

    regressions, improvements, preexisting = [], [], []
    for f in findings:
        if f["side"] != "new" or f["verdict"] != "bare":
            continue
        was = by.get(("old", field[f["id"]]))
        if was and was["verdict"] == "ds":
            f["severity"] = "high"
            f["history"] = "was a design-system component on the base — this branch replaced it"
            regressions.append(f["id"])
        elif was is None:
            f["severity"] = "high"
            f["history"] = "new on this branch: it shipped bare, it was never migrated"
            regressions.append(f["id"])
        else:
            f["severity"] = "medium"
            f["history"] = "already bare on the base — a pre-existing gap this branch did not close"
            preexisting.append(f["id"])
    for f in findings:
        if f["side"] == "new" and f["verdict"] == "ds":
            was = by.get(("old", field[f["id"]]))
            if was and was["verdict"] == "bare":
                improvements.append(f["id"])
                # The base's gap is real and it is also *fixed*. Left as a plain red row
                # it reads as one more thing to do, beside the one that actually is.
                was["resolved"] = True
                was["severity"] = "info"
                was["history"] = "this branch migrated it into <b>" + f["ds"] + "</b>"

    # The element table is keyed on the branch's signature. An element that only moved
    # has two of them, so the base side is translated through the pairing before asking
    # — otherwise every re-paired element reads "absent" on the side it came from.
    moved = delta["dom"].get("moved", {})
    for f in findings:
        sig = f["element"]["sig"]
        if f["side"] == "old":
            sig = moved.get(sig, sig)
        st = delta["elements"].get(sig, {})
        f["delta"] = {"dom": st.get("dom", "absent"),
                      "pixel_churn": st.get("pixel_churn"),
                      "status": st.get("status", "absent")}
        f.setdefault("severity", "info")

    return {
        "screen": name,
        "sides": sides_meta,
        "settled": {"new": new_snap.get("settled"), "old": old_snap.get("settled")},
        "delta": delta,
        "findings": findings,
        "summary": {"new": count("new"), "old": count("old"),
                    "regressions": regressions, "pre_existing": preexisting,
                    "improvements": improvements},
    }


WEIGHTING = (
    "DOM decides which element a finding is about; pixels only vote in the one case the "
    "DOM is blind to \u2014 same signature, same attributes, "
    f">{RESTYLE_CHURN:.0%} of the element\u2019s own box repainted, reported as "
    "`restyled`. Each element is compared cropped on its own box, so a layout shift "
    "above it costs nothing.")


def build_result(screens, registry) -> dict:
    """The run. One registry for all of it — a component is a component whichever screen
    happens to render it — and one rolled-up verdict over every screen audited."""
    roll = {"new": {"ds": 0, "bare": 0, "internal": 0, "uncovered": 0},
            "old": {"ds": 0, "bare": 0, "internal": 0, "uncovered": 0}}
    regressions, preexisting, improvements = [], [], []
    for sc in screens:
        for side in ("new", "old"):
            for k, v in sc["summary"][side].items():
                roll[side][k] += v
        regressions += sc["summary"]["regressions"]
        preexisting += sc["summary"]["pre_existing"]
        improvements += sc["summary"]["improvements"]
    return {
        "schema": SCHEMA,
        "generated": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "verdict": "gaps" if roll["new"]["bare"] else "clean",
        "registry": registry,
        "determinism": {"pins": PINS,
                        "settled": {sc["screen"]: sc["settled"] for sc in screens}},
        "screens": screens,
        "summary": {**roll, "regressions": regressions, "pre_existing": preexisting,
                    "improvements": improvements, "weighting": WEIGHTING},
    }


def _png_size(path: Path, fallback: dict) -> dict:
    """The overlay is positioned in percentages of the *picture*, so the denominator has
    to be the PNG's own pixel size. `scrollWidth` is a good guess and occasionally a pixel
    or two out, which is a badge sitting beside its field instead of on it."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"w": im.width, "h": im.height}
    except Exception:
        return fallback


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--screen", action="append", default=[], metavar="NAME=PATH",
                    help="a screen to audit: a heading and the path appended to --base-new "
                         "/ --base-old (repeatable). Three of the four controls a "
                         "migration touches usually live on three different forms, so "
                         "\"it flagged only the right one\" needs more than one screen.")
    ap.add_argument("--base-new", help="origin the branch is served from, e.g. http://localhost:4300")
    ap.add_argument("--base-old", help="origin the base is served from, e.g. http://localhost:4301")
    ap.add_argument("--new", action="append", default=[],
                    help="full URL of a screen on the branch (repeatable; pairs with --old "
                         "by position). Use instead of --screen/--base-* when the two "
                         "sides do not share a path. `NAME=URL` names the screen.")
    ap.add_argument("--old", action="append", default=[],
                    help="full URL of the same screen on the base (repeatable)")
    ap.add_argument("--label-new", default="new")
    ap.add_argument("--label-old", default="old")
    ap.add_argument("--repo-new", help="working tree behind the branch, for the commit stamp")
    ap.add_argument("--repo-old", help="working tree behind the base, for the commit stamp")
    ap.add_argument("--source", action="append", default=[],
                    help="tree scanned for design-system component sources (repeatable)")
    ap.add_argument("--viewport", default="1280x900")
    ap.add_argument("--wait-for", help="CSS selector to wait for before shooting")
    ap.add_argument("--mask", action="append", default=[],
                    help="CSS selector painted flat grey before the shot (repeatable) — "
                         "for the clock the page renders that no pin can freeze")
    ap.add_argument("--settle", type=int, default=250, help="ms between settle frames")
    ap.add_argument("--epoch", type=int, default=1756857600000,
                    help="the frozen wall clock, ms since epoch")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--color-scheme", default="light", choices=("light", "dark"))
    ap.add_argument("--threshold", type=float, default=0.1,
                    help="pixelmatch YIQ threshold (0..1)")
    ap.add_argument("--assets", default=".", help="directory the PNGs are written to")
    ap.add_argument("--asset-prefix", default="assets/",
                    help="how the fragment refers to the PNGs from the page")
    ap.add_argument("--from-capture", metavar="DIR",
                    help="skip the browser and re-render an earlier capture")
    ap.add_argument("--keep-capture", metavar="DIR",
                    help="write the raw snapshots there for a later --from-capture")
    ap.add_argument("-o", "--out", default="ds-audit.html")
    ap.add_argument("--json", dest="json_out", default="ds-audit.json")
    ap.add_argument("--css", action="store_true",
                    help="print the stylesheet this fragment needs and exit")
    args = ap.parse_args()

    if args.css:
        print(CSS)
        return

    assets = Path(args.assets)
    assets.mkdir(parents=True, exist_ok=True)

    # What to audit: `NAME=PATH` against two origins, or explicit URL pairs.
    wanted = []
    for spec in args.screen:
        name, _, path = spec.partition("=")
        if not (args.base_new and args.base_old):
            ap.error("--screen needs --base-new and --base-old")
        wanted.append((name, args.base_new.rstrip("/") + "/" + (path or name).lstrip("/"),
                       args.base_old.rstrip("/") + "/" + (path or name).lstrip("/")))
    if len(args.new) != len(args.old):
        ap.error("--new and --old pair by position, so there must be the same number of each")
    for i, (new_url, old_url) in enumerate(zip(args.new, args.old)):
        name, sep, url = new_url.partition("=")
        # `--new "Book a visit=http://…"` names the screen; a bare URL falls back to its
        # path, which is a poor heading and an unreadable asset filename for a file:// one.
        if sep and "://" in url:
            wanted.append((name, url, old_url.partition("=")[2] or old_url))
        else:
            wanted.append((_name_from_url(new_url, i), new_url, old_url))

    cap_dir = Path(args.from_capture) if args.from_capture else None
    keep = Path(args.keep_capture) if args.keep_capture else None
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
        _keep_names = []
    if cap_dir and not wanted:
        # The names, not the slugs. Rebuilt from filenames alone, "Book a visit" comes
        # back as "book-a-visit" and every heading in the report is a filename.
        manifest = cap_dir / "screens.json"
        if manifest.is_file():
            wanted = [(name, "", "") for name in json.loads(manifest.read_text())]
        else:
            wanted = [(f.name[: -len(".new.dom.json")], "", "")
                      for f in sorted(cap_dir.glob("*.new.dom.json"))]
    if not wanted:
        ap.error("nothing to audit: give --screen (with --base-*), or --new/--old, "
                 "or --from-capture")

    w, h = (int(x) for x in args.viewport.lower().split("x"))
    common = dict(viewport=(w, h), epoch=args.epoch, seed=args.seed,
                  wait_for=args.wait_for, settle_ms=args.settle, mask=args.mask,
                  color_scheme=args.color_scheme)

    snaps, screens_io = {}, []
    for name, new_url, old_url in wanted:
        stem = slug(name)
        pngs = {"new": assets / f"ds-audit-{stem}-new.png",
                "old": assets / f"ds-audit-{stem}-old.png"}
        if cap_dir:
            pair = {side: json.loads((cap_dir / f"{stem}.{side}.dom.json").read_text())
                    for side in ("new", "old")}
            for side in ("new", "old"):
                src = cap_dir / f"{stem}.{side}.png"
                if src.resolve() != pngs[side].resolve():
                    pngs[side].write_bytes(src.read_bytes())
        else:
            pair = {}
            for side, url in (("new", new_url), ("old", old_url)):
                print(f"[ds-audit] {name}: capturing {url}", file=sys.stderr)
                pair[side] = capture(url, pngs[side], **common)
            if keep:
                _keep_names.append(name)
                (keep / "screens.json").write_text(json.dumps(_keep_names, indent=1))
                for side in ("new", "old"):
                    (keep / f"{stem}.{side}.dom.json").write_text(
                        json.dumps(pair[side], indent=1))
                    (keep / f"{stem}.{side}.png").write_bytes(pngs[side].read_bytes())
        snaps[f"{name}:new"] = pair["new"]
        snaps[f"{name}:old"] = pair["old"]
        screens_io.append((name, stem, pair, pngs))

    # One registry over every screen and every source tree: a component is a component
    # whichever form happens to render it, and a screen that renders none of them is
    # still audited against the ones that exist.
    registry = derive_registry(snaps, [Path(x) for x in args.source])

    screens = []
    for name, stem, pair, pngs in screens_io:
        dom = dom_delta(pair["old"], pair["new"])
        elements, explained = combine(pair["old"], pair["new"], dom,
                                      pngs["old"], pngs["new"], args.threshold)
        pixel_delta(pngs["old"], pngs["new"], assets / f"ds-audit-{stem}-delta.png",
                    args.threshold, explained)
        sides_meta = {
            "new": {"label": args.label_new, "url": pair["new"].get("url"),
                    "commit": _git_head(Path(args.repo_new)) if args.repo_new else "",
                    "png": f"{args.asset_prefix}ds-audit-{stem}-new.png",
                    "page": _png_size(pngs["new"], pair["new"]["page"]),
                    "viewport": pair["new"]["viewport"]},
            "old": {"label": args.label_old, "url": pair["old"].get("url"),
                    "commit": _git_head(Path(args.repo_old)) if args.repo_old else "",
                    "png": f"{args.asset_prefix}ds-audit-{stem}-old.png",
                    "page": _png_size(pngs["old"], pair["old"]["page"]),
                    "viewport": pair["old"]["viewport"]},
        }
        screens.append(build_screen(name, pair["old"], pair["new"], registry,
                                    sides_meta=sides_meta,
                                    delta={"dom": dom, "elements": elements}))

    result = build_result(screens, registry)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(render(result, args.asset_prefix), encoding="utf-8")

    s = result["summary"]
    print(f'[ds-audit] {args.out} · {len(screens)} screen(s), {s["new"]["bare"]} gap(s), '
          f'{s["new"]["ds"]} component(s), {len(s["regressions"])} regression(s) '
          f'→ {args.json_out}', file=sys.stderr)


def _name_from_url(url: str, i: int) -> str:
    path = re.sub(r"^\w+://[^/]+", "", url).strip("/")
    return path or f"screen {i + 1}"


if __name__ == "__main__":
    main()
