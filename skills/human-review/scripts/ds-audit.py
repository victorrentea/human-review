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

Every screen of the app per run, not the two somebody guessed the branch touched: the
DOM diff decides which screens changed, the changed ones get a viewer, the rest are
named in one line. "It flagged the bare one" is a weak claim; "it flagged *only* the bare
one, and called the other three right" is the one worth making — and "the screen it
changed was not on the list" is the failure this arrangement exists to rule out.

The JSON is the artefact; the picture is its rendering. An adversarial review agent
reads `--json` and never has to OCR a PNG. Emit the stylesheet the fragment needs with
`--css`, the same way `openapi-compat.py` and friends do.

The one thing fixed with the design system is that a DS component marks its host with
`data-ds="<name>"`. Nothing here depends on its class names or its DOM shape.

Requires: playwright (`pip install playwright && playwright install chromium`),
Pillow and numpy for the pixel diff.
"""
from __future__ import annotations

import argparse
import base64

import datetime as _dt
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
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
  // stray focus and would report every input as "changed" between two runs. And
  // `ng-tns-c<hash>-<n>` is the animation namespace stamped per component *build*: two
  // builds of one template carry two hashes, so every node under it read as replaced.
  const VOLATILE = /^(ng-(untouched|touched|pristine|dirty|valid|invalid|star-inserted|tns-[\w-]+)|cdk-(focused|mouse-focused|keyboard-focused|program-focused)|mat-focus-indicator|_ng)/;

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
  const rawIdOf = (el) => el.getAttribute('id') || '';
  // A component kit numbers its own ids — `mat-select-0`, `mat-input-3`, `cdk-overlay-1`.
  // That is a counter, not a name: it shifts when one more select renders above, and on
  // the page it read "#mat-select-0" where the reader needed "Items per page". It is
  // still recorded (`id`), but it never anchors a signature, a selector or a label.
  const AUTO_ID = /^(mat|cdk|mdc|ng|mat-mdc)-[\w-]*?\d+$/;
  const idOf = (el) => { const i = rawIdOf(el); return AUTO_ID.test(i) ? '' : i; };
  // A component from a UI kit (Angular Material, CDK, PrimeNG, ng-zorro, Ionic, Shoelace)
  // and the kit directive that has no element of its own: `matSort` on a <table>, its
  // `mat-sort-header` cells. Which of them is a *control* is decided in Python
  // (`KIT_CONTROLS`); here they are only recorded with a name and their kit ancestors.
  const KIT_TAG = /^(mat|cdk|p|nz|ion|sl)-/;
  const kitOf = (el) => {
    const t = el.tagName.toLowerCase();
    if (KIT_TAG.test(t)) return t;
    if (el.classList.contains('mat-sort-header')) return 'mat-sort-header';
    if (el.classList.contains('mat-sort')) return 'mat-sort';
    return '';
  };
  const kitHostsOf = (el) => {
    const out = [];
    for (let p = el.parentElement; p && p !== document.body && out.length < 6; p = p.parentElement) {
      const k = kitOf(p);
      if (k) out.push(k);
    }
    return out;
  };

  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    // The accessible name a kit control actually carries: mat-paginator points its
    // page-size select at "Items per page:" this way. The element's own parts (the
    // select's current value, also listed) are not its name and are left out.
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map((i) => i && document.getElementById(i))
      .filter((l) => l && !el.contains(l))
      .map((l) => l.textContent.trim()).filter(Boolean).join(' ');
    if (by) return by;
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
    const ph = (el.getAttribute('placeholder') || '').trim();
    if (ph) return ph;
    // A kit component with nothing pointing at it: a sort header is named by what it
    // says ("Name"); a paginator says forty characters of counts, which is no name.
    const said = kitOf(el) ? el.textContent.replace(/\s+/g, ' ').trim() : '';
    return said.length <= 40 ? said : '';
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

  // What the reader is shown of a judged element: its tag, the attributes somebody wrote
  // and a short inside, as data rather than as a string. Python pretty-prints and caps it
  // (`pretty_html`), so the cut is testable without a browser. Angular's own stamps are not
  // markup anybody wrote: `_ngcontent-*` / `_nghost-*` scope the CSS, `ng-reflect-*` mirror
  // bindings in a dev build, and the state classes are the VOLATILE ones dropped above.
  const NOISE_ATTR = /^(_ngcontent|_nghost|ng-reflect-|ng-version$|style$)/;
  const SNIP_KIDS = 4;
  const shortOf = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const snipOf = (el, depth) => {
    const attrs = [];
    for (const a of Array.from(el.attributes)) {
      if (NOISE_ATTR.test(a.name)) continue;
      const v = a.name === 'class' ? classesOf(el).join(' ') : a.value;
      if (a.name === 'class' && !v) continue;
      attrs.push([a.name, shortOf(v, 60)]);
    }
    const node = { tag: el.tagName.toLowerCase(), attrs: attrs };
    const kids = Array.from(el.children).filter((c) => !/^(script|style|template)$/i.test(c.tagName));
    if (!kids.length) {
      const said = el.textContent.replace(/\s+/g, ' ').trim();
      if (said) node.text = shortOf(said, 48);
    } else if (depth > 0) {
      node.children = kids.slice(0, SNIP_KIDS).map((c) => snipOf(c, depth - 1));
    }
    const rest = depth > 0 ? kids.slice(SNIP_KIDS) : kids;
    if (rest.length) {
      node.more = rest.length;
      if (new Set(rest.map((c) => c.tagName)).size === 1) node.more_tag = rest[0].tagName.toLowerCase();
    }
    return node;
  };
  // The custom elements around it, nearest first. The first one whose template holds the
  // element is the file it was written in (`locate_source`).
  const hostsOf = (el) => {
    const out = [];
    for (let p = el.parentElement; p && p !== document.body && out.length < 8; p = p.parentElement) {
      const t = p.tagName.toLowerCase();
      if (t.includes('-')) out.push(t);
    }
    return out;
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
    // Only what can become a verdict carries its markup: every node of the page would
    // turn the snapshot into a second copy of the DOM.
    const judged = isNative(el) || !!el.getAttribute('data-ds') || !!kitOf(el);

    out.push({
      sig: sigOf(el),
      selector: selectorOf(el),
      tag: t,
      id: rawIdOf(el) || null,
      auto_id: !!rawIdOf(el) && !idOf(el),
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
      label: isNative(el) || el.getAttribute('data-ds') || kitOf(el) ? labelOf(el) : '',
      kit: kitOf(el) || null,
      multiple: el.hasAttribute('multiple') || el.getAttribute('aria-multiselectable') === 'true',
      kit_hosts: isNative(el) || kitOf(el) ? kitHostsOf(el) : [],
      disabled: el.disabled === true,
      leaf: el.children.length === 0,
      // Page coordinates, not viewport ones: the shot is full-page.
      box: { x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
             w: Math.round(r.width), h: Math.round(r.height) },
      text: text,
      html: judged ? snipOf(el, 2) : null,
      hosts: judged ? hostsOf(el) : [],
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
                # The element a template writes to use it — `<app-combo>` — which is what
                # a gap's rule names: "combo" is the audit's word, the tag is the author's.
                tags = comp.setdefault("tags", [])
                if n["tag"] not in tags:
                    tags.append(n["tag"])

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

# The UI-kit controls the audit judges, keyed on what the page records as `kit` (the tag,
# or the class a kit directive puts on its host). Each is (what to call it, the role it
# fills). A kit control is never the design system's component — that is a `data-ds`
# host, whatever library it is built on — so it is judged either way: in a role the
# registry covers it is the gap the role model exists to find; in a role the registry
# does not know it is still a control from outside the design system, and saying so with
# the reason is the difference between "0 gaps" and "never looked". `mat-sort-header` is
# judged through its table (`mat-sort`), once per table, not once per column.
KIT_CONTROLS = {
    "mat-select": ("Angular Material select", "select"),
    "mat-paginator": ("Angular Material paginator", "paginator"),
    "mat-sort": ("Angular Material sort directive", "sortable table header"),
    "mat-checkbox": ("Angular Material checkbox", "input[type=checkbox]"),
    "mat-radio-button": ("Angular Material radio button", "input[type=radio]"),
    "mat-slide-toggle": ("Angular Material slide toggle", "role=switch"),
    "mat-slider": ("Angular Material slider", "input[type=range]"),
    "mat-button-toggle-group": ("Angular Material button toggle", "toggle group"),
    "mat-tab-group": ("Angular Material tabs", "tabs"),
    "p-dropdown": ("PrimeNG dropdown", "select"),
    "p-select": ("PrimeNG select", "select"),
    "p-paginator": ("PrimeNG paginator", "paginator"),
    "nz-select": ("ng-zorro select", "select"),
    "nz-pagination": ("ng-zorro pagination", "paginator"),
    "ion-select": ("Ionic select", "select"),
    "sl-select": ("Shoelace select", "select"),
}

# The ids a component kit numbers itself. Kept in step with `AUTO_ID` in the page-side
# extractor; here it decides what a row is *called* on a capture that predates it.
AUTO_ID = re.compile(r"^(?:mat|cdk|mdc|ng|mat-mdc)-[\w-]*?\d+$")


def effective_role(n: dict) -> str | None:
    """The role a control fills, which is not always the role its markup states.

    A `role="combobox"` on anything but a text input is the ARIA "select-only combobox":
    `<mat-select>`, a PrimeNG dropdown, a div picker. It answers exactly the question a
    `<select>` answers, so it is judged as one — reported as `role=combobox`, a role
    nothing claims, it walked past a registry whose combo covers `select`."""
    role = n.get("role")
    if role == "role=combobox" and n.get("tag") != "input":
        return "select[multiple]" if _multiple(n) else "select"
    return role


def _multiple(n: dict) -> bool:
    """A multi-select, however the kit says so: the attribute, ARIA, or Material's own
    `mat-mdc-select-multiple` class on a capture taken before `multiple` was recorded."""
    return bool(n.get("multiple") or any(c.endswith("-select-multiple")
                                         for c in n.get("cls") or []))


def element_name(f: dict) -> str:
    """What a reader calls the element: its accessible name, else its control name, else
    an id a person wrote — never a kit's counter (`mat-select-0`) — else what it is."""
    el = f["element"]
    label = (el.get("label") or "").strip().rstrip(":").strip()
    if label:
        return label
    if el.get("name"):
        return el["name"]
    if el.get("id") and not AUTO_ID.match(el["id"]):
        return el["id"]
    kit = KIT_CONTROLS.get(el.get("kit") or el.get("tag") or "")
    return kit[0] if kit else el.get("tag") or "element"


def _kit_host(n: dict) -> str | None:
    """The nearest kit *control* this node sits inside: its machinery, not a control of
    its own. The select inside a paginator is the paginator's page-size picker."""
    for k in n.get("kit_hosts") or []:
        if k in KIT_CONTROLS:
            return k
    return None


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
    claimed = ", ".join(f'<code>{r}</code>' for r in sorted(covered)) or "nothing"
    nodes = snapshot["nodes"]
    out = []
    for n in nodes:
        if n.get("ds"):
            out.append(_finding(side, n, "ds", None,
                                f'design-system component <b>{n["ds"]}</b>'))
            continue
        kit = KIT_CONTROLS.get(n.get("kit") or "")
        host = _kit_host(n)
        if host and not n.get("ds_host") and (kit or n.get("native")):
            # The kit control around it is the one judged; its parts are its machinery,
            # the way a combo's own <select> is the combo's.
            out.append(_finding(side, n, "internal", None,
                                f'inside the {KIT_CONTROLS[host][0]} — its own control',
                                role_name=effective_role(n)))
            continue
        if kit and not n.get("ds_host"):
            out.append(_kit_finding(side, n, kit, covered, claimed, nodes))
            continue
        if not n.get("native"):
            continue
        role = covered.get(effective_role(n))
        if not role:
            # Considered and not judged. It is in the JSON so the agent reading it can
            # see the auditor looked at this control and can say why it let it past —
            # an audit that only reports what it flagged cannot be argued with. Never
            # badged on the picture: that is what "leave everything else unmarked" means.
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
        # Spelled out rather than implied. The finding is not "a select is in a covered
        # role" — that is the evidence; the finding is "this is not the design-system
        # component", and a reader looking at a red box on a screenshot has to be told
        # that in words, not left to infer it from an arrow between two nouns.
        out.append(_finding(
            side, n, "bare", role,
            f'not the design-system component — a plain '
            f'<code>&lt;{n["tag"]}&gt;</code> where {owners} belongs'))
    return out


def _kit_finding(side, n, kit, covered, claimed, nodes) -> dict:
    """A UI-kit control: the design system's component if the registry names one for its
    role (then it is in the wrong library: a gap where that component belongs), else a
    control from outside the design system — `foreign`, a gap with its reason."""
    what, kit_role = kit
    if kit_role == "select" and _multiple(n):
        what, kit_role = what.replace("select", "multi-select"), "select[multiple]"
    tag = f'<code>&lt;{html.escape(n["tag"])}&gt;</code>'
    if n.get("kit") == "mat-sort":
        cols = [m.get("label") for m in nodes
                if m.get("kit") == "mat-sort-header" and m.get("label")
                and "mat-sort" in (m.get("kit_hosts") or [])]
        if cols:
            n = dict(n, label="matSort on " + ", ".join(cols))
        tag = "<code>matSort</code>"
    role = covered.get(kit_role)
    if role:
        owners = " or ".join(f"<b>{d}</b>" for d in role["covered_by"])
        return _finding(
            side, n, "bare", role,
            f"not the design-system component — {tag}, an {what}, where {owners} "
            "belongs")
    inner = [m for m in nodes if _kit_host(m) == n.get("kit")
             and KIT_CONTROLS.get(m.get("kit") or "", (None, None))[1] in covered]
    parts = "".join(
        f'; its \u201c{html.escape(element_name({"element": m}))}\u201d control is a '
        f'<code>&lt;{html.escape(m["tag"])}&gt;</code>, not '
        + " or ".join(f"<b>{d}</b>" for d in covered[KIT_CONTROLS[m["kit"]][1]]["covered_by"])
        for m in inner)
    return _finding(
        side, n, "foreign", None,
        f"not from the design system — {tag}, an {what}; the design system has no "
        f"component for a <code>{html.escape(kit_role)}</code>{parts}",
        role_name=kit_role)


def _finding(side: str, n: dict, verdict: str, role: dict | None, message: str,
             *, role_name: str | None = None) -> dict:
    f = {
        "id": f'{side}:{n["sig"]}',
        "side": side,
        "verdict": verdict,
        "role": (role or {}).get("role") or role_name or n.get("role"),
        "expected_ds": (role or {}).get("covered_by") or [],
        "ds": n.get("ds"),
        "element": {"tag": n["tag"], "id": n.get("id"), "name": n.get("name"),
                    "label": n.get("label") or "", "sig": n["sig"],
                    **({"kit": n["kit"]} if n.get("kit") else {}),
                    **({"hosts": n["hosts"]} if n.get("hosts") else {})},
        "selector": n["selector"],
        "box": n["box"],
        "message": message,
    }
    # Only a verdict that is drawn gets its markup: the reader opens it from the row, and
    # an `internal` or `uncovered` control has no row to open it from.
    if verdict in DRAWN:
        f["snippet"] = snippet_of(n)
    return f


# ── what a verdict shows when it is opened ────────────────────────────────────────
#
# A red box on a screenshot says *that* something is wrong and leaves the reader to find
# *what*: which element it is in the markup, which file it was written in, and why that is
# a gap rather than a choice. Each drawn verdict carries the three — `snippet`, `source`,
# `rule` — in the JSON, and the row under it in the table renders them.

DRAWN = ("ds", "bare", "foreign")
SNIPPET_LINES = 10
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}


# Written bare when empty (`disabled`, not `disabled=""`); any other empty value is a value,
# and `<option value>` would read as an attribute somebody forgot to finish.
BOOLEAN_ATTRS = {"disabled", "required", "multiple", "readonly", "selected", "checked",
                 "hidden", "autofocus", "novalidate", "open"}


def _open_tag(node: dict) -> str:
    attrs = "".join(f" {k}" if v == "" and k in BOOLEAN_ATTRS
                    else f' {k}="{html.escape(v, quote=True)}"'
                    for k, v in node.get("attrs") or [])
    return f'<{node["tag"]}{attrs}>'


def _html_lines(node: dict, depth: int = 0) -> list[str]:
    pad = "  " * depth
    head = pad + _open_tag(node)
    if node["tag"] in VOID_TAGS:
        return [head]
    kids, more = node.get("children") or [], node.get("more") or 0
    if not kids and not more:
        return [f'{head}{html.escape(node.get("text") or "", quote=False)}</{node["tag"]}>']
    lines = [head]
    for k in kids:
        lines += _html_lines(k, depth + 1)
    if more:
        # A raw `<option>` is legal inside a comment, and the block is read as code: an
        # entity there would be shown to the reader as the five characters `&lt;`.
        what = f' <{node["more_tag"]}>' if node.get("more_tag") else ""
        lines.append(f"{pad}  <!-- {more} more{what} -->")
    lines.append(f'{pad}</{node["tag"]}>')
    return lines


def pretty_html(node: dict, max_lines: int = SNIPPET_LINES) -> str:
    """The element as markup a reader can scan: one tag per line, two-space indent, at most
    `max_lines`. A longer one keeps its head and its closing tag and says how much of the
    middle it left out — the opening tag is the finding, its fourteenth `<option>` is not."""
    lines = _html_lines(node)
    if len(lines) > max_lines:
        hidden = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 2] + [f"  <!-- … {hidden} more lines -->", lines[-1]]
    return "\n".join(lines)


def snippet_of(n: dict) -> dict:
    """`{"html": …, "from": "rendered" | "reconstructed"}` for one snapshot node.

    A capture taken before the extractor recorded markup has only the identity the
    snapshot always kept, and the tag rebuilt from it says so rather than passing for the
    page's own."""
    if n.get("html"):
        return {"html": pretty_html(n["html"]), "from": "rendered"}
    attrs = [[k, n[k]] for k in ("id", "name", "type") if n.get(k)]
    if n.get("ds"):
        attrs.append(["data-ds", n["ds"]])
    return {"html": pretty_html({"tag": n["tag"], "attrs": attrs, "more": 0}),
            "from": "reconstructed"}


def _component_tag(ds: str, registry: dict) -> str:
    comp = next((c for c in registry.get("components") or [] if c["ds"] == ds), {})
    tags = comp.get("tags") or []
    return (f"<code>&lt;{html.escape(tags[0])}&gt;</code>" if tags
            else f'its <b>{html.escape(ds)}</b> component')


def _other_screens(ds: str, registry: dict, screen: str) -> int:
    """On how many *other* screens of the branch the component renders. A component the
    rest of the app already uses is the strongest form of "one belongs here"."""
    comp = next((c for c in registry.get("components") or [] if c["ds"] == ds), {})
    seen = {s.rsplit(":", 1)[0] for s in comp.get("seen_on") or []
            if ":" in s and s.rsplit(":", 1)[1] == "new"}
    return len(seen - {screen})


CONSISTENT = "use the component so styling, keyboard behaviour and validation stay consistent"


def rule_html(f: dict, registry: dict, screen: str) -> str:
    """The rule a drawn verdict applies, in one sentence that names the component.

    The message in the table is the verdict ("not the design-system component"); this is
    the reason, spelled out for the one reader who does not already know what the design
    system offers or why it matters — the person who copied the older template."""
    el = f["element"]
    tag = f'<code>&lt;{html.escape(el["tag"])}&gt;</code>'
    kit = KIT_CONTROLS.get(el.get("kit") or "")
    if el.get("kit") == "mat-sort":
        tag = "<code>matSort</code>"
    if f["verdict"] == "ds":
        return (f'The design system’s <b>{html.escape(f.get("ds") or "")}</b> component '
                f'({_component_tag(f.get("ds") or "", registry)}) — the control this '
                "role should be.")
    if f["verdict"] == "foreign":
        what = kit[0] if kit else f'<code>{html.escape(el["tag"])}</code>'
        # A fact, like the `bare` rule: no advice paragraph on what the team should
        # agree (20 Oct 2026, Victor: it read as model-written, on a tab that costs $0).
        return (f"{_a(what)} ({tag}); the design system has no "
                f'<code>{html.escape(f.get("role") or "")}</code> component.')
    owners = f.get("expected_ds") or []
    offers = " or ".join(_component_tag(d, registry) for d in owners) or "a component"
    used = [n for n in (_other_screens(d, registry, screen) for d in owners) if n]
    names = " or ".join(f"<b>{html.escape(d)}</b>" for d in owners)
    usage = f", used on {_n(max(used), 'other screen')}" if used else ""
    what = f"{_a(kit[0])} ({tag})" if kit else f"A native {tag}"
    out = (f"{what} where the design system offers {offers} ({names}{usage}): "
           f"{CONSISTENT}.")
    if (f.get("history") or "").startswith("was a design-system"):
        out += " On the base this field was the component; this branch replaced it."
    if f.get("resolved"):
        out += " This branch migrated it."
    return out


def _a(noun: str) -> str:
    """"An Angular Material select", "A PrimeNG dropdown" — the article a sentence needs."""
    plain = re.sub(r"<[^>]+>", "", noun)
    return ("An " if plain[:1].lower() in "aeiou" else "A ") + noun


# ── where it was written ──────────────────────────────────────────────────────────
#
# The screenshot shows the element as rendered; the fix happens in a template. Angular
# makes the mapping cheap: every component names its selector and its template, and the
# page records which custom elements sit around the control (`hosts`, nearest first). The
# first of those whose template has an opening tag matching the control — on its id, its
# name, its formControlName — is the file and the line. A control projected into a child
# component is not in that child's template, so the search keeps climbing.

_COMPONENT_DECL = re.compile(r"@Component\s*\(\s*\{")
_SELECTOR_DECL = re.compile(r"""\bselector\s*:\s*(['"`])(.+?)\1""", re.S)
_TEMPLATE_URL = re.compile(r"""\btemplateUrl\s*:\s*(['"`])(.+?)\1""")
_INLINE_TEMPLATE = re.compile(r"\btemplate\s*:\s*`")


def template_index(roots: list[Path]) -> dict[str, dict]:
    """Element selector → `{"path", "line0", "text"}`: the template that component renders.

    `line0` is the line the template text starts after — zero for a `templateUrl` file,
    the backtick's line for an inline `template:` — so a match inside it is reported at
    the line a reader would find it on."""
    out: dict[str, dict] = {}
    for root in roots:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*.ts")
            if "node_modules" not in p.parts and not p.name.endswith(".spec.ts"))
        for ts in files:
            try:
                text = ts.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _COMPONENT_DECL.finditer(text):
                end = text.find("export class", m.end())
                chunk = text[m.end():end if end > 0 else None]
                sel = _SELECTOR_DECL.search(chunk)
                if not sel:
                    continue
                tags = [s.strip() for s in sel.group(2).split(",")
                        if re.fullmatch(r"[a-z][\w]*-[\w-]*", s.strip())]
                url = _TEMPLATE_URL.search(chunk)
                inline = _INLINE_TEMPLATE.search(chunk)
                if url:
                    path = (ts.parent / url.group(2)).resolve()
                    try:
                        entry = {"path": path, "line0": 0,
                                 "text": path.read_text(encoding="utf-8", errors="replace")}
                    except OSError:
                        continue
                elif inline:
                    start = m.end() + inline.end()
                    close = text.find("`", start)
                    entry = {"path": ts.resolve(), "line0": text.count("\n", 0, start),
                             "text": text[start:close if close > 0 else None]}
                else:
                    continue
                for t in tags:
                    out.setdefault(t, entry)
    return out


def _identity(f: dict) -> list[tuple[str, str | None]]:
    """What a template would have written to make this element: the attributes that name
    it. Read off the rendered markup when there is some, which knows `formcontrolname`
    from `name`; else off the snapshot's own fields."""
    el = f["element"]
    snip = f.get("snippet") or {}
    ident = []
    if snip.get("from") == "rendered":
        head = snip["html"].split("\n", 1)[0]
        rendered = {}
        for k, v in re.findall(r'\s([\w:.-]+)="([^"]*)"', head):
            rendered.setdefault(k.lower(), html.unescape(v))
        for k in ("id", "name", "formcontrolname"):
            v = rendered.get(k)
            if v and not (k == "id" and AUTO_ID.match(v)):
                ident.append((k, v))
    else:
        # The snapshot's `name` is `name` or `formcontrolname`, whichever was there.
        if el.get("id") and not AUTO_ID.match(el["id"]):
            ident.append(("id", el["id"]))
        if el.get("name"):
            ident += [("name", el["name"]), ("formcontrolname", el["name"])]
    if el.get("kit") == "mat-sort":
        ident.append(("matsort", None))
    return ident


def _attr_written(attrs: str, name: str, value: str | None) -> bool:
    """A static attribute in a template's opening tag. `[name]`, `[attr.id]`, `#name` and
    `data-name` are other attributes, so the name may not follow `[ ( . # -` or a letter."""
    pat = rf"(?<![\w\[\(.*#-]){re.escape(name)}"
    pat += (rf"""\s*=\s*["']{re.escape(value)}["']""" if value is not None
            else r"(?![\w-])")
    return bool(re.search(pat, attrs, re.I))


def _match_in(entry: dict, tag: str, ident, *, identified_only: bool) -> dict | None:
    hits = []
    for m in re.finditer(rf"""<{re.escape(tag)}(?=[\s/>])((?:[^>"']|"[^"]*"|'[^']*')*)>""",
                         entry["text"], re.I):
        score = sum(3 for k, v in ident if _attr_written(m.group(1), k, v))
        line = entry["line0"] + entry["text"].count("\n", 0, m.start()) + 1
        hits.append((score, line))
    if not hits:
        return None
    best = max(s for s, _ in hits)
    if identified_only and best == 0:
        return None
    lines = [ln for s, ln in hits if s == best]
    return {"path": entry["path"], "line": lines[0], "matches": len(lines)}


def locate_source(f: dict, index: dict, repo_root: Path,
                  changed: list[str] | tuple = ()) -> dict | None:
    """`{"file", "line", "via", "matches"}` — where this element was written — or None.

    `via` is the component whose template it was found in, or `search` when the page did
    not say (a capture older than `hosts`) and every template was searched for an opening
    tag that names the element; then only a match on an id or a name counts, the changed
    templates are tried first, and `matches` says when more than one tag fits equally."""
    el = f["element"]
    tag = "table" if el.get("kit") == "mat-sort" else el["tag"]
    ident = _identity(f)
    hit, via = None, None
    for h in el.get("hosts") or []:
        entry = index.get(h)
        if entry and h != tag:
            hit = _match_in(entry, tag, ident, identified_only=False)
            if hit:
                via = h
                break
    if hit is None and ident:
        entries = list({id(e): e for e in index.values()}.values())
        rel = lambda e: _rel(e["path"], repo_root)
        entries.sort(key=lambda e: (rel(e) not in set(changed), rel(e)))
        for entry in entries:
            hit = _match_in(entry, tag, ident, identified_only=True)
            if hit:
                via = "search"
                break
    if hit is None:
        return None
    return {"file": _rel(hit["path"], repo_root), "line": hit["line"], "via": via,
            "matches": hit["matches"]}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def attach_sources(screen: dict, index: dict, repo_root: Path, changed=()) -> dict:
    """Give every drawn verdict of a screen its `source` (or None: not found)."""
    for f in screen["findings"]:
        if f["verdict"] in DRAWN:
            f["source"] = locate_source(f, index, repo_root, changed)
    return screen


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


def pixels_differing(old_png: Path, new_png: Path) -> int:
    """How many pixels of the two shots are not the same, counted raw: no YIQ weighting,
    no threshold, no erosion — none of the forgiveness `_yiq_mask` grants. Any area only
    one side has (the page grew or shrank) counts whole.

    It is what lets the tab say of a screen the DOM diff left out "every pixel is the same"
    instead of "nothing we measure moved": the reader asked, of "5 of 19 screens changed",
    how anyone knows the other 14 did not, and an exact zero is the one answer that does
    not need the rest of this file to be believed."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(old_png).convert("RGBA"))
    b = np.asarray(Image.open(new_png).convert("RGBA"))
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    same_area = int((a[:h, :w] != b[:h, :w]).any(-1).sum())
    union = max(a.shape[0], b.shape[0]) * max(a.shape[1], b.shape[1])
    return same_area + union - h * w


# ── framing the changes ───────────────────────────────────────────────────────────
#
# Two changes closer than FRAME_VGAP px vertically share a frame; within one band of
# changed rows, ink is split into separate frames only across a horizontal gap wider than
# FRAME_HGAP_SHARE of the page — a table header whose labels all moved is one change, a
# field on the left and a badge on the far right are two. FRAME_PAD is the breathing room.
FRAME_VGAP = 56
FRAME_HGAP_SHARE = 0.25
FRAME_PAD = 8
FRAME_MIN_INK = 6


def _runs(mask, gap: int) -> list[tuple[int, int]]:
    """[start, end) runs of True in a 1-D mask, bridging holes of at most `gap`."""
    import numpy as np

    xs = np.flatnonzero(mask)
    if not xs.size:
        return []
    out, start, prev = [], int(xs[0]), int(xs[0])
    for x in xs[1:]:
        x = int(x)
        if x - prev > gap:
            out.append((start, prev + 1))
            start = x
        prev = x
    out.append((start, prev + 1))
    return out


def change_frames(old_png: Path, new_png: Path, *, vgap: int = FRAME_VGAP,
                  hgap_share: float = FRAME_HGAP_SHARE, pad: int = FRAME_PAD) -> dict:
    """Where on the screen the branch changed something: a few rectangles per side.

    No model, no DOM — a text diff over the picture's *rows*. Each pixel row of either
    screenshot is hashed, and `difflib` aligns the two sequences the way `diff` aligns
    two files. A field inserted mid-form is then an *insertion* of 40 rows, and the
    buttons below it, which only slid down, are rows that match — so the layout shift
    that paints a naive pixel diff magenta down to the footer costs nothing here.

    Each non-matching hunk is reduced to its ink: for rows present on both sides, the
    columns that differ; for rows only one side has, the columns that differ from both
    rows bounding the hunk on that side (the background and a card's vertical borders run
    straight through and drop out). The ink is split into horizontal runs across wide
    gaps, each run trimmed to the rows it actually touches, then runs are clustered —
    two whose rectangles come within `vgap` on either side merge, to a fixpoint. One
    change is one frame; two changes at opposite ends of the screen are two.

    Every frame has a twin on the other side, at the same columns. When one side has no
    rows for it — a pure insertion or removal — its twin is `insert: True`, a zero-height
    line at the place the content went in or came out.
    """
    import difflib

    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(old_png).convert("RGB"))
    b = np.asarray(Image.open(new_png).convert("RGB"))
    w = min(a.shape[1], b.shape[1])
    a, b = a[:, :w], b[:, :w]
    hgap = int(w * hgap_share)
    sm = difflib.SequenceMatcher(None, [r.tobytes() for r in a], [r.tobytes() for r in b],
                                 autojunk=False)

    def ink_alone(block, own, lo, hi):
        refs = ([own[lo - 1]] if lo > 0 else []) + ([own[hi]] if hi < own.shape[0] else [])
        m = np.ones(block.shape[:2], dtype=bool)
        for r in refs:
            m &= (block != r[None]).any(-1)
        return m

    items = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        k = min(i2 - i1, j2 - j1)
        new_ink = np.zeros((j2 - j1, w), dtype=bool)
        old_ink = np.zeros((i2 - i1, w), dtype=bool)
        if k:
            d = (a[i1:i1 + k] != b[j1:j1 + k]).any(-1)
            new_ink[:k] |= d
            old_ink[:k] |= d
        if j2 - j1 > k:
            new_ink[k:] = ink_alone(b[j1 + k:j2], b, j1, j2)
        if i2 - i1 > k:
            old_ink[k:] = ink_alone(a[i1 + k:i2], a, i1, i2)
        for x0, x1 in _runs(new_ink.any(0) | old_ink.any(0), hgap):
            def span(m, base):
                rows = np.flatnonzero(m[:, x0:x1].any(1))
                return ((base, base) if not rows.size
                        else (base + int(rows[0]), base + int(rows[-1]) + 1))
            ink = int(new_ink[:, x0:x1].sum() + old_ink[:, x0:x1].sum())
            if ink >= FRAME_MIN_INK:
                items.append({"x": (x0, x1), "new": span(new_ink, j1),
                              "old": span(old_ink, i1)})

    def near(p, q):
        if p["x"][0] > q["x"][1] + hgap or q["x"][0] > p["x"][1] + hgap:
            return False
        return any(not (p[s][0] > q[s][1] + vgap or q[s][0] > p[s][1] + vgap)
                   for s in ("new", "old"))

    merged = True
    while merged:
        merged = False
        for i in range(len(items)):
            j = next((j for j in range(i + 1, len(items)) if near(items[i], items[j])), None)
            if j is not None:
                p, q = items[i], items.pop(j)
                for key in ("x", "new", "old"):
                    p[key] = (min(p[key][0], q[key][0]), max(p[key][1], q[key][1]))
                merged = True
                break

    def blank(img, y, x0, x1):
        row = img[y, x0:x1]
        return bool((row == row[0]).all())

    def seam(img, y, x0, x1):
        """Where on the side that lacks it the change went in. The row diff pins it to
        the first row that stopped matching, which is often the last pixel of the field
        above — so the line is centred in the run of blank rows around that point, the
        gutter between the two fields, instead of striking through one of them."""
        lo = hi = min(max(y, 0), img.shape[0] - 1)
        while lo > 0 and blank(img, lo - 1, x0, x1):
            lo -= 1
        while hi < img.shape[0] - 1 and blank(img, hi, x0, x1):
            hi += 1
        return (lo + hi) // 2

    out = {"new": [], "old": []}
    for it in sorted(items, key=lambda it: (it["new"][0], it["x"][0])):
        for side, img in (("new", b), ("old", a)):
            (x0, x1), (y0, y1) = it["x"], it[side]
            X0, X1 = max(x0 - pad, 0), min(x1 + pad, img.shape[1])
            if y0 == y1:
                out[side].append({"x": X0, "y": seam(img, y0, x0, x1), "w": X1 - X0,
                                  "h": 0, "insert": True})
                continue
            Y0, Y1 = max(y0 - pad, 0), min(y1 + pad, img.shape[0])
            out[side].append({"x": X0, "y": Y0, "w": X1 - X0, "h": Y1 - Y0,
                              "insert": False})
    return out


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
/* One per screen, stacked. The gap between two screens' cards is the collapsed margin
   between them: 1rem, half the 2rem it was — the cards are closed rows of one list, and
   2rem read as five separate sections. The last one keeps the old 2rem before the footer. */
.dsa { margin: 1rem 0; }
.dsa:not(:has(~ .dsa)) { margin-bottom: 2rem; }
.dsa-shot { position: relative; line-height: 0; }
.dsa-shot img { width: 100%; height: auto; display: block; border-radius: .3rem; }
.dsa-mark { position: absolute; box-sizing: border-box; border-radius: .2rem; pointer-events: auto; }
.dsa-mark.ok  { border: 2px solid var(--dsa-ok); background: color-mix(in srgb, var(--dsa-ok) 10%, transparent); }
.dsa-mark.bad { border: 3px solid var(--dsa-bad); background: color-mix(in srgb, var(--dsa-bad) 14%, transparent); }
.dsa-mark.new { border: 2px dashed var(--dsa-new); background: transparent; }
/* The ink on a label whose background IS one of the verdict hues. Not always white:
   the dark palette lifts every one of them to a pastel so the box outline can be seen
   against a near-black page, and white on a pastel is what put `✓ combo · added` at
   2.3:1 and the plain-control regression beside it at 2.8:1 — on the verdicts, which
   are the one thing on this tab a reader must be able to read. The label's own colour flips
   instead of the fills. (`--dsa-label-fg`, not `--dsa-ink`: `.dsa-ink` is already the
   legend's name for the pen the screenshots are marked up with.) */
.dsa-mark b { position: absolute; left: 0; top: -1.15rem; font: 700 .68rem/1.15rem
  -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--dsa-label-fg);
  padding: 0 .35rem; border-radius: .2rem; white-space: nowrap; }
.dsa-mark.ok b  { background: var(--dsa-ok); }
.dsa-mark.bad b { background: var(--dsa-bad); }
.dsa-mark.new b { background: var(--dsa-new); }
.dsa-mark.hot { outline: 3px solid var(--dsa-hot); outline-offset: 3px; }
/* The wanted tag on a gap's chip: its right end, in the verdict-ok hue, so the red half
   says what is there and the green half what should be (`badge_html`). */
.dsa-mark b i.dsa-want { font-style: normal; display: inline-block; background: var(--dsa-ok);
  margin: 0 -.35rem 0 .15rem; padding: 0 .35rem; border-radius: 0 .2rem .2rem 0; }
/* The hover bubble's found/should-be pair (`mark_tip_html`). tip.js draws the bubble the
   same near-black in both schemes, so these hues are fixed for that ground and do not
   follow the page's --dsa-* tokens, which flip with the scheme. */
.tip .dsa-vs { display: grid; grid-template-columns: max-content auto; gap: .35rem .6rem;
  align-items: center; }
.tip .dsa-vsk { font-size: 12px; font-weight: 600; white-space: nowrap; }
.tip .dsa-vsk.bad { color: #ff8f8f; }
.tip .dsa-vsk.good { color: #7fe0a7; }
.tip .dsa-vs code { font: 600 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  padding: .05rem .4rem; border-radius: .25rem; white-space: nowrap; }
.tip .dsa-vs code.bad { color: #ffc2c2; background: rgba(255, 92, 92, .2);
  box-shadow: inset 0 0 0 1px rgba(255, 107, 107, .6); text-decoration: line-through;
  text-decoration-color: #ff8f8f; }
.tip .dsa-vs code.good { color: #b4f5cf; background: rgba(70, 192, 122, .22);
  box-shadow: inset 0 0 0 1px rgba(70, 192, 122, .65); }
.tip .dsa-vsnone { font-size: 13px; font-style: italic; opacity: .8; }
.tip .dsa-vsor { font-size: 12px; opacity: .7; margin: 0 .2rem; }
.dsa-legend { display: flex; gap: 1.2rem; flex-wrap: wrap; margin: .5rem 0 .3rem;
  font-size: .82rem; align-items: center; }
.dsa-legend i { width: .85rem; height: .85rem; border-radius: .2rem; display: inline-block;
  vertical-align: -.1rem; margin-right: .3rem; }
.dsa-legend .k-ok i  { background: var(--dsa-ok); }
.dsa-legend .k-bad i { background: var(--dsa-bad); }
.dsa-legend .k-new i { background: var(--dsa-new); }
.dsa-legend .k-frame i { background: transparent; border: 2px solid var(--dsa-frame);
  box-sizing: border-box; }
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
.dsa-prov { opacity: .7; font-style: italic; }
/* Wraps only at the `<wbr>` after each `>` (`selector_html`), never mid-token. */
.dsa-sel { font-size: .78rem; opacity: .72; word-break: normal; overflow-wrap: normal;
  white-space: normal; }
.dsa-sel .dsa-step { white-space: nowrap; }
/* A side is a branch name or a short sha: one word, never broken over three lines. */
.dsa-table td:nth-child(2) { white-space: nowrap; }
/* One collapsible row per screen, closed by default — the same furniture the Sequence
   tab's `details.testpair` wears for the same reason: a run audits the whole catalogue,
   and most of it did not change in a way worth a picture. The triangle and the
   inline-flex summary are copied from `hrbuild/assets/css/sequence.css` rather than
   shared with it, because a fragment meant to render standalone (`report_page.py`,
   `--css`) cannot depend on a stylesheet that ships with the page around it; the two are
   free to drift apart the day one of them needs to. */
details.dsa-screen { background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: .4rem .95rem; margin: .55rem 0; }
details.dsa-screen[open] { padding-bottom: .8rem; }
details.dsa-screen > summary { cursor: pointer; list-style: none; display: flex; gap: .4rem;
  align-items: baseline; color: var(--fg); font-size: .95rem; font-weight: 600;
  padding: .25rem 0; }
details.dsa-screen > summary::-webkit-details-marker { display: none; }
details.dsa-screen > summary:hover { color: var(--link); }
.dsa-route { opacity: .7; font-weight: 400; }
.dsa-considered { margin: .6rem 0 0; font-size: .84rem; opacity: .85; }
.dsa-considered summary { cursor: pointer; }
.dsa-considered ul { margin: .4rem 0 0 .2rem; }
/* The change frames: one per place the branch changed (see `change_frames`), off with
   the checkbox on the viewer's bar. `.insert` is the zero-height twin on the side that
   lacks the change — a dashed line where it went in or came out. */
.dsa-frame { position: absolute; box-sizing: border-box; pointer-events: none;
  border: 3px solid var(--dsa-frame); border-radius: .35rem; }
.dsa-frame.insert { border: 0; border-top: 3px dashed var(--dsa-frame); border-radius: 0; }
/* What a frame holds is said in a caption UNDER the picture (`.dsa-framecap`), never on
   it: the chip that rode the frame's top edge covered the table header it framed (eval
   run 8). With several frames each carries only its number, outside its top-left corner. */
.dsa-frame > b.dsa-fnum { position: absolute; left: -3px; top: -3px; transform: translateX(-100%);
  font: 700 .68rem/1.15rem -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--dsa-label-fg); background: var(--dsa-frame); padding: 0 .3rem;
  border-radius: .2rem; z-index: 2; }
.dsa-framecap { margin: .4rem 0 0; font-size: .82rem; line-height: 1.5; display: flex;
  flex-wrap: wrap; gap: .2rem 1rem; }
.dsa-fcap { display: inline-flex; align-items: baseline; gap: .35rem; }
.dsa-fcap i { width: .85rem; height: .85rem; border: 2px solid var(--dsa-frame);
  border-radius: .2rem; box-sizing: border-box; align-self: center; flex: none; }
.dsa:has(.dsa-frameon:not(:checked)) .dsa-frame,
.dsa:has(.dsa-frameon:not(:checked)) .dsa-framecap { display: none; }
/* Chrome · Edge, in the title: a two-button segment, the pressed one filled. Sized as
   body text, not as the heading it sits in. */
.dsa-browsers { display: inline-flex; margin-left: .8rem; vertical-align: middle;
  border: 2px solid var(--line); border-radius: .45rem; overflow: hidden; }
.dsa-br { font: inherit; font-size: .8rem; font-weight: 600; line-height: 1.5;
  padding: .2rem .5rem; border: 0; background: var(--card); color: var(--muted); cursor: pointer;
  display: inline-flex; align-items: center; }
.dsa-br + .dsa-br { border-left: 2px solid var(--line); }
.dsa-br:hover { color: var(--fg); background: color-mix(in srgb, var(--fg) 8%, var(--card)); }
/* Pressed reads as lit: the logo at full strength on a tinted cell. Not pressed: a faded
   logo. Neither pressed — the pictures below disagree — leaves both faded. */
.dsa-br[aria-pressed="true"] { background: color-mix(in srgb, var(--fg) 22%, var(--card)); }
.dsa-br .dsa-logo { width: 1.35rem; height: 1.35rem; background: center / contain no-repeat;
  opacity: .4; filter: grayscale(.7); }
.dsa-br[aria-pressed="true"] .dsa-logo, .dsa-br:hover .dsa-logo { opacity: 1; filter: none; }
/* The per-picture copy, pinned to the right end of the viewer's own bar: above the
   picture's top-right corner. Smaller than the title's. */
.dsa-browsers.dsa-local { margin-left: auto; }
.dsa-local .dsa-br { padding: .1rem .35rem; }
.dsa-local .dsa-br .dsa-logo { width: 1.05rem; height: 1.05rem; }
details.dsa-screen.dsa-nobr .dsa-shot img { opacity: .55; }
/* Another engine's picture: the boxes were measured on the primary's layout, and WebKit
   lays the same page out a few pixels apart, so they are not drawn over it. */
/* Another browser's picture: the primary's marks and frames would sit where Chrome laid
   the page out, so they give way to the ones re-measured on that browser (`.dsa-brlayer`). */
details.dsa-screen.dsa-alt .dsa-shot > .dsa-mark, details.dsa-screen.dsa-alt .dsa-shot > .dsa-frame { display: none; }
.dsa-brlayer { position: absolute; inset: 0; display: none; pointer-events: none; }
details.dsa-screen[data-br-on="chrome"] .dsa-brlayer[data-br="chrome"],
details.dsa-screen[data-br-on="edge"] .dsa-brlayer[data-br="edge"],
details.dsa-screen[data-br-on="safari"] .dsa-brlayer[data-br="safari"] { display: block; }
/* Two logos in one cell (Chrome and Edge): side by side, slightly overlapping. */
.dsa-br .dsa-logo + .dsa-logo { margin-left: -.3rem; }
.dsa-frametoggle { margin-left: .6rem; font-size: .82rem; display: inline-flex; gap: .35rem;
  align-items: center; cursor: pointer; user-select: none; color: var(--fg); }
.dsa-frametoggle input { accent-color: var(--dsa-frame); margin: 0; }
details.dsa-screen > summary .dsa-sumtail { font-weight: 400; }
/* Plain running text, not a flex row: the parts are joined by " · " text, and a flex
   gap around every text node set the dots adrift from the words they separate. */
.dsa-hdr { line-height: 1.8; }
/* The deltas on a screen's line and in the header. `+N gap` is the one warning among
   them, so it alone is coloured: a yellow chip, the hue this page gives a caution. The
   rest are plain words with a hover; a gap the base already had is context, so muted. */
.dsa-gap { color: var(--dsa-warn); background: color-mix(in srgb, var(--dsa-warn) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--dsa-warn) 55%, transparent); border-radius: .3rem;
  padding: 0 .35rem; font-weight: 700; white-space: nowrap; }
.dsa-fixed { color: var(--dsa-ok); }
.dsa-pre { opacity: .7; }
.dsa-count { text-decoration: underline dotted; text-underline-offset: 3px; cursor: pointer; }
/* Open, the count's underline goes solid: the box under it belongs to that word. */
.dsa-count[aria-expanded="true"] { text-decoration-style: solid; }
/* The box as Victor tuned it live on 7 Oct 2026 (PR #51 page): a little more air inside
   and under it, each heading a short paragraph, the bullets indented by margin. */
.dsa-howbox { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: .6rem .9rem; margin: .4rem 0 .9rem; font-size: .88rem; line-height: 1.45; }
.dsa-howbox[hidden] { display: none; }
.dsa-howbox p { margin: .2rem 0; }
.dsa-howbox ul { margin: .1rem 0 .5rem 1.1rem; padding: 0; }
.dsa-howbox li { margin: .1rem 0; }
.dsa-gap, .dsa-comp, .dsa-fixed, .dsa-pre { cursor: help; }
.dsa-unlisted { color: var(--dsa-bad); border: 1px solid var(--dsa-bad); border-radius: 6px;
  padding: .45rem .7rem; margin: .4rem 0 .8rem; font-size: .9rem; }
.dsa-unlisted code { color: inherit; }
/* One number per drawn verdict, on the picture and at the head of its row
   (`number_findings`). Inside a change frame the frame's own chip carries it; a mark
   outside every frame wears it the way a frame does, outside its top-left corner, in the
   verdict's colour. The row's chip is the same chip in the same colour, so the eye can
   match the two without reading. */
.dsa-mark > b.dsa-mnum { left: -3px; top: -3px; transform: translateX(-100%);
  padding: 0 .3rem; z-index: 2; }
.dsa-mark { cursor: pointer; }
.dsa-rnum { display: inline-block; min-width: 1.15rem; box-sizing: border-box; text-align: center;
  margin-right: .4rem; padding: 0 .25rem; border-radius: .2rem; cursor: help;
  font: 700 .68rem/1.15rem -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--dsa-label-fg); }
.dsa-rnum.framed { background: var(--dsa-frame); }
.dsa-rnum.bad { background: var(--dsa-bad); }
.dsa-rnum.ok { background: var(--dsa-ok); }
/* What opens under a verdict's row (`detail_html`): the rule, then the element's markup
   under the page's own source bar. The bar and the token colours are the page's snippet
   stylesheet; only the spacing is local — a rendered element has no line numbers, so its
   code would otherwise touch the block's left edge. */
.dsa-table tr:has(+ tr.dsa-more) td { border-bottom-color: transparent; }
/* Its one wide cell is the row's second, which the side column's `nowrap` would otherwise
   reach: the rule ran 200px past the card on one unbroken line. */
.dsa-table tr.dsa-more td { padding-top: 0; white-space: normal; }
details.dsa-why > summary { cursor: pointer; font-size: .84rem; line-height: 1.5; }
details.dsa-why.ok > summary { font-size: .78rem; opacity: .65; }
details.dsa-why.ok[open] > summary { opacity: .8; }
.dsa-whyk { font-weight: 700; text-transform: uppercase; font-size: .68rem; letter-spacing: .04em;
  color: var(--dsa-bad); margin-right: .15rem; }
.dsa-rule { margin: .3rem 0 0; font-size: .82rem; opacity: .85; }
.dsa-snip { margin: .45rem 0 .35rem; padding: .45rem .6rem; max-width: 56rem; }
.dsa-snip .srcbar { margin-bottom: .35rem; }
.dsa-snip pre.code { padding-left: .7rem; }
details.dsa-why.ok .dsa-snip { opacity: .85; }
.dsa-nosrc { font-size: .78rem; opacity: .75; font-style: italic; }
.dsa-srcnote { font-size: .72rem; opacity: .7; cursor: help; }
.dsa-table tr.dsa-more.flash td { background: color-mix(in srgb, var(--dsa-hot) 16%, transparent); }
:root { --dsa-ok: #1f7a45; --dsa-bad: #c1121f; --dsa-new: #1a4fa0; --dsa-hot: #f0a500;
        --dsa-label-fg: #ffffff; --dsa-frame: #a23fd6; --dsa-warn: #946200; }
@media (prefers-color-scheme: dark) {
  :root { --dsa-ok: #46c07a; --dsa-bad: #ff6b6b; --dsa-new: #7aa9ef; --dsa-hot: #ffc94d;
          --dsa-label-fg: #15151a; --dsa-frame: #c77dff; --dsa-warn: #f2c24a; }
  .dsa-shot img { filter: none; }
}
"""

def _logo_css() -> str:
    """The browsers' logos (`LOGOS/<name>.png`), inlined once in the stylesheet rather
    than once per button — the switch is repeated on every screen. A button with no logo
    file keeps its browser's name (`_br_button`)."""
    out = []
    for f in sorted(LOGOS.glob("*.png")):
        data = base64.b64encode(f.read_bytes()).decode()
        out.append(f'.dsa-logo[data-logo="{f.stem}"] '
                   f'{{ background-image: url(data:image/png;base64,{data}); }}')
    return "\n".join(out)


#: Where `_logo_css` finds the logos: small PNGs cut from each browser's own app icon.
LOGOS = Path(__file__).resolve().parent / "ds-audit-logos"
CSS += "\n" + _logo_css()

HL_JS = """<script data-hr-swap="keep">
// Hovering a row lights its box on the picture. Delegated on `document` for the same
// reason the diagram viewer is: the fragment is pasted into a page it does not own.
// Delegated only, which is what `data-hr-swap="keep"` says: when a rebuild swaps this tab
// into an open page (`hrbuild/shared/panels.py`), the listeners registered at load serve
// the new markup, and this script is not run a second time.
(function () {
  // The count opens the box that says how "changed" is decided.
  function toggleHow(el) {
    var box = document.getElementById(el.getAttribute('aria-controls'));
    if (!box) return;
    box.hidden = !box.hidden;
    el.setAttribute('aria-expanded', String(!box.hidden));
  }
  document.addEventListener('click', function (e) {
    var el = e.target.closest && e.target.closest('.dsa-count');
    if (el) toggleHow(el);
  });
  document.addEventListener('keydown', function (e) {
    var el = e.target.closest && e.target.closest('.dsa-count');
    if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleHow(el); }
  });
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
  // A mark on the picture opens what its row says about it: the markup, the template line,
  // the rule. The row may be off-screen under a tall shot, so it is brought into view and
  // flashed once, the way a hover on the row lights the mark.
  document.addEventListener('click', function (ev) {
    var mark = ev.target.closest && ev.target.closest('.dsa-mark[data-find]');
    if (!mark) return;
    var scope = mark.closest('.dsa');
    var row = scope && scope.querySelector('tr.dsa-more[data-find="'
      + CSS.escape(mark.getAttribute('data-find')) + '"]');
    if (!row) return;
    var more = row.querySelector('details');
    if (more) more.open = true;
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    row.classList.add('flash');
    setTimeout(function () { row.classList.remove('flash'); }, 1200);
  });
  // "frame the changes" is one preference, not one per screen: flipping any flips all.
  document.addEventListener('change', function (ev) {
    if (!ev.target.classList || !ev.target.classList.contains('dsa-frameon')) return;
    document.querySelectorAll('.dsa-frameon').forEach(function (c) { c.checked = ev.target.checked; });
  });
  // Chrome · Edge: a picture swaps to that browser's capture, which sits beside the
  // primary's as `<name>.<browser>.png`. The switch by the title sets every screen; the one
  // on a screen's own bar sets that screen only, and then the title's shows nothing pressed
  // unless every screen agrees. A screen that browser did not shoot keeps the primary's
  // picture and says so (`dsa-nobr`).
  function showAs(d, br, primary) {
    var has = br === primary || (' ' + (d.getAttribute('data-brs') || '') + ' ')
      .indexOf(' ' + br + ' ') >= 0;
    d.querySelectorAll('.dsa-shot img').forEach(function (img) {
      var base = img.getAttribute('data-src0');
      if (!base) { base = img.getAttribute('src'); img.setAttribute('data-src0', base); }
      img.setAttribute('src', br === primary || !has ? base
        : base.replace(/\\.png(?=$|[?#])/, '.' + br + '.png'));
    });
    d.classList.toggle('dsa-nobr', !has);
    d.classList.toggle('dsa-alt', has && br !== primary);
    if (has) d.setAttribute('data-br-on', br); else d.removeAttribute('data-br-on');
    d.querySelectorAll('.dsa-local .dsa-br').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-br') === br));
    });
  }
  function syncTitle(run, primary, fallback) {
    var on = [].map.call(run.querySelectorAll('details.dsa-screen[data-brs]'), function (d) {
      return d.getAttribute('data-br-on') || primary;
    });
    var all = !on.length ? fallback
      : on.every(function (x) { return x === on[0]; }) ? on[0] : null;
    run.querySelectorAll('.dsa-global .dsa-br').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-br') === all));
    });
  }
  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest && ev.target.closest('.dsa-br[data-br]');
    if (!btn) return;
    ev.preventDefault();
    var run = btn.closest('.dsa-run') || document;
    var first = run.querySelector('.dsa-br[data-primary]');
    var primary = first ? first.getAttribute('data-br') : '';
    var br = btn.getAttribute('data-br');
    var local = btn.closest('details.dsa-screen');
    (local ? [local] : run.querySelectorAll('details.dsa-screen')).forEach(function (d) {
      showAs(d, br, primary);
    });
    syncTitle(run, primary, br);
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


def shot_html(png_rel: str, page: dict, marks: list[dict],
              frames: list[dict] | None = None, numbered: bool | None = None,
              fnums: dict[int, int] | None = None, alts: list[dict] | None = None) -> str:
    """A screenshot with boxes over it, positioned in percentages so the picture stays
    responsive — the report is read on a laptop and on a projector. `frames` are the
    change frames (`change_frames`), drawn under the marks so a badge stays readable.

    `numbered` says whether the numbers are drawn (`number_findings` decides it for the
    whole screen, so both sides agree); left out, they are drawn when this side has more
    than one frame. A frame's number is its place in the list, which every frame's twin
    on the other side shares — so `2` is the same change in New and in Old even where the
    other side's `1` is only an insertion line. A mark outside every frame carries its
    own number (`num`), the one its row in the table carries.

    `fnums` (`frame_numbers`) maps a frame's place in the list to its number. A frame
    that holds no verdict is not in it and carries no number: a number is a key into the
    table, and a frame with nothing judged in it has no row to key. Left out, every frame
    is numbered by its place.

    `alts` are the same marks and frames as another browser laid them out
    (`browser_layout`): `{"br", "page", "marks", "frames"}` each, drawn in a layer of their
    own that shows only while that browser's picture is (`.dsa-brlayer`). The caption under
    the picture stays the primary's: it names the change, not where it was drawn."""
    out = [f'<div class="dsa-shot"><img src="{html.escape(png_rel)}" alt="" loading="lazy">']
    boxed = [fr for fr in frames or [] if not fr.get("insert")]
    if numbered is None:
        numbered = len(boxed) > 1
    layer, caps = _overlay(page, marks, frames, numbered, fnums)
    out.append(layer)
    for alt in alts or []:
        inner, _ = _overlay(alt["page"], alt["marks"], alt["frames"], numbered,
                            alt.get("fnums", fnums))
        out.append(f'<div class="dsa-brlayer" data-br="{html.escape(alt["br"])}">{inner}</div>')
    out.append("</div>")
    if caps:
        out.append('<p class="dsa-framecap">' + "".join(
            f'<span class="dsa-fcap"><i></i>{f"{n} " if numbered and n else ""}'
            f'<b>{html.escape(label)}</b></span>' for n, label in caps) + "</p>")
    return "".join(out)


def _overlay(page: dict, marks: list[dict], frames: list[dict] | None,
             numbered: bool, fnums: dict[int, int] | None) -> tuple[str, list]:
    """The frames and marks over one picture, and the caption entries its frames make."""
    w, h = max(page["w"], 1), max(page["h"], 1)
    out = []
    # A frame's words go UNDER the picture, never on it. The chip used to ride the frame's
    # top edge, and eval run 8's sat over the table's own header (`Pets`) — the screen the
    # reader came to look at, covered by the label explaining it. With more than one frame
    # each gets a small number outside its corner, and the caption is keyed by it.
    caps = []
    for i, fr in enumerate(frames or []):
        style = (f'left:{_pct(fr["x"], w)};top:{_pct(fr["y"], h)};'
                 f'width:{_pct(fr["w"], w)};height:{_pct(fr["h"], h)}')
        label = "" if fr.get("insert") else frame_label(fr, marks)
        tip = "Inserted here" if fr.get("insert") else label
        chip = ""
        if not fr.get("insert"):
            n = (i + 1) if fnums is None else fnums.get(i)
            caps.append((n, label))
            chip = f'<b class="dsa-fnum">{n}</b>' if numbered and n else ""
        out.append(f'<div class="dsa-frame{" insert" if fr.get("insert") else ""}" '
                   f'style="{style}" data-tip="{html.escape(tip)}">{chip}</div>')
    for m in marks:
        b = m["box"]
        style = (f'left:{_pct(b["x"], w)};top:{_pct(b["y"], h)};'
                 f'width:{_pct(b["w"], w)};height:{_pct(b["h"], h)}')
        num = (f'<b class="dsa-mnum">{m["num"]}</b>'
               if numbered and m.get("num") and not m.get("in_frame") else "")
        # `data-tip-html` when the mark has one (tip.js prefers it): the found/should-be
        # pair. Plain `data-tip` for the rest, the Diff pane's marks among them.
        tip = (f'data-tip-html="{html.escape(m["tip_html"])}"' if m.get("tip_html")
               else f'data-tip="{html.escape(m["tip"])}"')
        badge = m.get("badge_html") or html.escape(m["badge"])
        out.append(
            f'<div class="dsa-mark {m["cls"]}" style="{style}" '
            f'data-find="{html.escape(m["id"])}" {tip}>'
            f'<b>{badge}</b>{num}</div>')
    return "".join(out), caps


def _centre_in(box: dict, frame: dict) -> bool:
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    return (frame["x"] <= cx <= frame["x"] + frame["w"]
            and frame["y"] <= cy <= frame["y"] + frame["h"])


def _drawn_in_frame(f: dict, frames: dict) -> int | None:
    """The place in its side's frame list of the frame a drawn verdict sits in, or None."""
    return next((i for i, fr in enumerate(frames.get(f["side"]) or [])
                 if not fr.get("insert") and _centre_in(f["box"], fr)), None)


def frame_numbers(findings: list[dict], frames: dict) -> dict[int, int]:
    """`{place in the frame list: number}` for the frames that hold a drawn verdict, on
    either side (a frame and its twin share a place), numbered 1, 2, … in list order.

    A frame with nothing judged in it — a column header the branch added to a list —
    gets no number. The number keys the picture to a row of the table, and that frame has
    no row; numbered, it read as a finding nobody wrote down (Book A Visit's `2`)."""
    held = sorted({i for f in findings if f["verdict"] in DRAWN
                   for i in [_drawn_in_frame(f, frames)] if i is not None})
    return {i: n + 1 for n, i in enumerate(held)}


def number_findings(findings: list[dict], frames: dict) -> tuple[dict, bool]:
    """`{finding id: (number, in_frame)}` for every drawn verdict, and whether to draw them.

    One set of numbers per screen, shared by the picture and the table. A verdict inside a
    change frame takes that frame's number — the reader already sees it on the frame, and
    a second, different number beside it would be two names for one place. One outside
    every frame (a gap the base already had, on a screen the branch changed elsewhere)
    gets the next number after the frames, keyed on the field, so the same field is the
    same number in New and in Old. Numbers are drawn only when there is more than one of
    them: a single frame around a single control needs no key. A frame with no verdict in
    it is not numbered at all (`frame_numbers`)."""
    fnums = frame_numbers(findings, frames)
    count = len(fnums)
    on_picture = set(fnums.values())
    nums, orphans = {}, {}
    drawn = sorted((f for f in findings if f["verdict"] in DRAWN),
                   key=lambda f: (f["side"] != "new", f["box"]["y"], f["box"]["x"]))
    for f in drawn:
        hit = _drawn_in_frame(f, frames)
        if hit is not None:
            nums[f["id"]] = (fnums[hit], True)
            continue
        el = f["element"]
        key = el.get("id") or el.get("name") or el.get("label") or el["sig"]
        if key not in orphans:
            orphans[key] = count + len(orphans) + 1
        nums[f["id"]] = (orphans[key], False)
        on_picture.add(orphans[key])
    return nums, len(on_picture) > 1


def _snippets_module():
    """The page's snippet machinery — the source bar and its two handles — imported the
    way `page_base_commit` imports the page base: the package sits next to this file."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from hrbuild.shared import snippets
    return snippets


def _highlight(text: str) -> str:
    """Token-coloured the way every other quoted block on the page is: Pygments at build
    time, into the `pre.code` classes the page's snippet stylesheet colours."""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import HtmlLexer
    except ImportError:
        return html.escape(text)
    return highlight(text, HtmlLexer(), HtmlFormatter(nowrap=True)).rstrip("\n")


def _source_links(rel: str, root: Path, line: int, base: str) -> str:
    """The VS Code and github.com handles of a source bar, against the audit's base — the
    two `_snippet_links` emits for a quoted block, built from the same two functions with
    the base passed in rather than read off the page's global. Either drops itself where
    it could not open what it promises; their notes on stderr would be about the review
    page, not this step, so they are kept out of its log."""
    import contextlib
    import io
    s = _snippets_module()
    if not all(hasattr(s, n) for n in ("diff_link_html", "_github_compare_link",
                                       "_shown_in_compare", "_icon")):
        return ""
    with contextlib.redirect_stderr(io.StringIO()):
        vsc = s.diff_link_html(rel, base, root, face=s._icon("VSC"), line=line)
        at = line if s._shown_in_compare(rel, base, str(root), line) else None
        gh = s._github_compare_link(rel, base.removeprefix("origin/"), root, line=at,
                                    face=s._icon("GH"))
    return vsc + gh


def source_bar(f: dict, root: Path | None, base: str | None) -> str:
    """The header over the element's markup: the template it was written in, at the line,
    with the page's own two handles — or, when no template was found, that sentence."""
    snip = f.get("snippet") or {}
    badge = ('<span class="code-badge" data-diff="unchanged" data-tip="The element as the '
             'browser rendered it, trimmed; the file beside it is where it was written">'
             'as rendered</span>' if snip.get("from") == "rendered" else
             '<span class="code-badge" data-diff="unchanged" data-tip="This capture predates '
             'the markup being recorded: the tag is rebuilt from its id and name">'
             'rebuilt</span>')
    src = f.get("source")
    if not src:
        return (f'<div class="srcbar"><span class="dsa-nosrc">no template under the audited '
                f'source writes this element</span>{badge}</div>')
    rel, line = src["file"], src["line"]
    note = ""
    if src.get("matches", 1) > 1:
        note = (f'<span class="dsa-srcnote" data-tip="More than one opening tag there fits '
                f'equally; this is the first">1 of {src["matches"]} '
                f'&lt;{html.escape(f["element"]["tag"])}&gt;</span>')
    path = Path(root or ".") / rel
    # Whatever bar every other quoted block wears. The page's builder has carried the two
    # handles as `links`, and is dropping them for a bar whose file name is the one link;
    # this follows whichever it is rather than keeping a second opinion about it.
    import inspect
    build_bar = _snippets_module()._extract_module().srcbar_html
    takes_links = "links" in inspect.signature(build_bar).parameters
    links = (_source_links(rel, Path(root), line, base)
             if takes_links and root and base and path.is_file() else "")
    return build_bar(f"vscode://file/{path.resolve()}:{line}:1", rel, str(line),
                     badge + note, *([links] if takes_links else []))


def detail_html(f: dict, registry: dict, screen: str, root: Path | None,
                base: str | None) -> str:
    """What opens under a drawn verdict's row: the rule in one sentence, then the element's
    markup under the bar naming the template line it came from. A gap opens by default —
    it is the product; a component that is right stays shut, one click away."""
    rule = f.get("rule") or rule_html(f, registry, screen)
    snip = f.get("snippet") or snippet_of({"tag": f["element"]["tag"],
                                            "id": f["element"].get("id"),
                                            "name": f["element"].get("name"),
                                            "ds": f.get("ds")})
    f = dict(f, snippet=snip)
    bad = f["verdict"] in ("bare", "foreign") and not f.get("resolved")
    figure = (f'<figure class="snippet dsa-snip">{source_bar(f, root, base)}'
              f'<pre class="code lang-html"><code>{_highlight(snip["html"])}</code></pre>'
              "</figure>")
    if bad:
        return (f'<details class="dsa-why bad" open><summary><span class="dsa-whyk">why'
                f'</span> {rule}</summary>{figure}</details>')
    return (f'<details class="dsa-why ok"><summary>markup and template</summary>'
            f'<p class="dsa-rule">{rule}</p>{figure}</details>')


def short_selector(selector: str, keep: int = 2) -> str:
    """A path selector cut to its last `keep` steps. An element with no id or name has a
    signature from the app root down — six lines of `div.container-fluid:1>…` in a table
    cell — and only its tail tells two rows apart. The whole path stays in the tip."""
    steps = selector.split(">")
    return selector if len(steps) <= keep + 1 else "\u2026>" + ">".join(steps[-keep:])


def selector_html(selector: str) -> str:
    """A selector that may only wrap between its steps: `<wbr>` after each `>`, and the
    cell's CSS forbids any other break. Eval run 8's column cut `div#ownersTable>ta / ble.
    mat-sort.tabl / e:1` — `word-break: break-all` splitting tokens wherever the column
    ran out, which is unreadable for the one string a reader would search the code for.
    The whole path is in the tip."""
    # Each step is nowrap too: in normal wrapping a browser still breaks after a hyphen,
    # and `mat-` / `mdc-paginator` is the same mid-token cut by another route.
    return "&gt;<wbr>".join(f'<span class="dsa-step">{html.escape(step)}</span>'
                            for step in selector.split(">"))


def frame_label(frame: dict, marks: list[dict]) -> str:
    """The chip on a change frame: what was judged inside it, in the marks' own words.

    An unlabelled purple box said "something here changed" and left the reader to work
    out what, and whether it was fine. The chip names the verdicts the frame holds —
    `✗ matSort · ✗ Select page of owners — added` — or says there is nothing to judge."""
    held = [m for m in marks if _centre_in(m["box"], frame)]
    names = list(dict.fromkeys(m.get("short") or m["badge"] for m in held))
    if not names:
        return "changed \u2014 nothing here to judge"
    status = {m.get("status") for m in held}
    tail = f" \u2014 {status.pop()}" if len(status) == 1 and None not in status else ""
    return " \u00b7 ".join(names) + tail


def _plain(message: str) -> str:
    """A finding’s message as text, for a `data-tip` the tooltip sets as textContent.

    Stripping the tags is not enough: the message is HTML, so its `<select>` is written
    `&lt;select&gt;`, and `html.escape` on the way into the attribute turned the ampersand
    into `&amp;lt;` — the bubble read the literal characters `&lt;select&gt;` out loud
    for as long as this shipped. Unescape after stripping and the tag reads as a tag.
    """
    return html.unescape(re.sub("<[^>]+>", "", message))


def _ds_tag(ds: str, registry: dict | None) -> str | None:
    """The element a template writes to use component `ds` (`app-combo`), or None when the
    capture never rendered it — then the component is named, not guessed at as a tag."""
    comp = next((c for c in (registry or {}).get("components") or [] if c["ds"] == ds), {})
    return (comp.get("tags") or [None])[0]


def _tag_text(tag: str, name: str | None = None, ds: str | None = None) -> str:
    """`<select name="vetId">`: the opening tag, with the one attribute both sides of the
    swap share — the form field's `name` — so the pair differs in the tag and nothing else.

    A component whose host is a custom element (`<app-combo>`) is named by its tag. One
    mounted on a plain element (`<div data-ds="combo">`) is not — `<div>` says nothing — so
    it is written with its marker, and the field's name stays on the control inside it."""
    if ds and "-" not in tag:
        return f'<{tag} data-ds="{ds}">'
    return f'<{tag} name="{name}">' if name else f"<{tag}>"


def mark_tip_html(f: dict, registry: dict | None) -> str:
    """The bubble over a drawn verdict: the tag that is there against the tag that should
    be, as two tags, red and struck against green.

    "not the design-system component — a plain <select> where combo belongs" made the
    reader translate "combo" into markup (20 Oct 2026, Victor: "what should have appeared
    in the HTML? put a tag"). The pair shows it: the found opening tag, then the design
    system's own element (`registry.components[].tags`, what the rendered hosts were),
    carrying the same `name`. One sentence under it says why. The bubble is tip.js's
    near-black in both schemes, so its colours are fixed, not the page's flipping tokens."""
    el, esc = f["element"], html.escape
    name = el.get("name")
    found = ("matSort" if el.get("kit") == "mat-sort"
             else _tag_text(el["tag"], name, f.get("ds") if f["verdict"] == "ds" else None))
    kit = KIT_CONTROLS.get(el.get("kit") or "")

    def row(key: str, cls: str, value: str) -> str:
        return f'<span class="dsa-vsk {cls}">{key}</span><span>{value}</span>'

    if f["verdict"] == "ds":
        ds = f.get("ds") or ""
        rows = row("✓ found", "good", f'<code class="good">{esc(found)}</code>')
        foot = f"The design system’s {esc(ds)} component."
    elif f["verdict"] == "foreign":
        role = f.get("role") or ""
        rows = (row("✗ found", "bad", f'<code class="bad">{esc(found)}</code>')
                + row("✓ should be", "good",
                      f'<span class="dsa-vsnone">nothing — the design system has no '
                      f'{esc(role)} component</span>'))
        what = kit[0] if kit else el["tag"]
        foot = f"{esc(_a(what))}, from outside the design system."
        # A kit control may hold one the design system does cover (the paginator's
        # page-size select); `_kit_finding` appends each as "; its … control is a …".
        inner = _plain(f["message"]).split("; its ", 1)
        if len(inner) == 2:
            foot += f" Its {esc(inner[1])}."
    else:
        owners = f.get("expected_ds") or []
        wanted = [(_ds_tag(d, registry), d) for d in owners]
        cells = " <span class=\"dsa-vsor\">or</span> ".join(
            f'<code class="good">{esc(_tag_text(t, name, d))}</code>' if t
            else f'<span class="dsa-vsnone">the {esc(d)} component</span>'
            for t, d in wanted) or '<span class="dsa-vsnone">a design-system component</span>'
        rows = (row("✗ found", "bad", f'<code class="bad">{esc(found)}</code>')
                + row("✓ should be", "good", cells))
        what = f"{_a(kit[0])}" if kit else f"A plain <{el['tag']}>"
        names = " or ".join(owners) or "a component"
        foot = esc(f"{what} where the design system’s {names} belongs.")
    return f'<div class="dsa-vs">{rows}</div><p class="tipfoot">{foot}</p>'


def _marks_for(findings, side, registry: dict | None = None):
    marks = []
    for f in findings:
        if f["side"] != side:
            continue
        if f["verdict"] in ("internal", "uncovered"):
            continue
        st = f.get("delta", {})
        status = st.get("status") if st.get("status") in ("added", "restyled", "changed") \
            else None
        # What the branch did to it, in the badge's own first word: "new" for a control it
        # added, "changed" for one it touched, "existing" for one the base already had.
        age = {"added": "new", "restyled": "changed", "changed": "changed"}.get(status, "existing")
        note = f' · {st["status"]}' if status else ""
        if f["verdict"] == "ds":
            marks.append({"id": f["id"], "cls": "ok", "box": f["box"],
                          "badge": f'✓ {f["ds"]}{note}', "short": f'✓ {f["ds"]}',
                          "tip": _plain(f["message"]), "status": status,
                          "tip_html": mark_tip_html(f, registry)})
        elif f["verdict"] == "foreign":
            # The kit's own name on the picture (`mat-paginator`, `matSort`): it says what
            # library the control came from, which is the finding. The accessible name is
            # in the table row, where there is room for both.
            kit = f["element"].get("kit") or f["element"]["tag"]
            name = "matSort" if kit == "mat-sort" else kit
            what = KIT_CONTROLS.get(kit, (name,))[0]
            marks.append({"id": f["id"], "cls": "bad", "box": f["box"],
                          "badge": f'✗ {age} {what} — the design system has none',
                          "short": f'✗ {name}', "tip": _plain(f["message"]), "status": status,
                          "tip_html": mark_tip_html(f, registry)})
        else:
            # Says what the control is and what it should have been: the component the
            # design system has for that role — as the tag a template writes for it
            # (`<app-combo>`) when the capture saw one rendered, else by its name.
            tags = [_ds_tag(d, registry) for d in f["expected_ds"]]
            if f["expected_ds"] and all(tags):
                expect = " or ".join(_tag_text(t, ds=d)
                                     for t, d in zip(tags, f["expected_ds"]))
            else:
                expect = " or ".join(f["expected_ds"])
                expect = f"the {expect} component" if expect else "a design-system component"
            plain = "" if f["element"].get("kit") else "plain "
            head = f'✗ {age} {plain}<{f["element"]["tag"]}> — should be '
            marks.append({"id": f["id"], "cls": "bad", "box": f["box"],
                          "badge": head + expect,
                          # The wanted tag rides a green end of the red chip: the same
                          # contrast as the bubble, readable without hovering.
                          "badge_html": html.escape(head) + (
                              f'<i class="dsa-want">{html.escape(expect)}</i>'
                              if all(tags) and tags else html.escape(expect)),
                          "short": f'✗ <{f["element"]["tag"]}>',
                          "tip": _plain(f["message"]), "status": status,
                          "tip_html": mark_tip_html(f, registry)})
    return marks


# The frame's purple is in the legend because it is on the picture. The blue entry that
# used to sit here named no box the New/Old views draw — blue is the Diff view's pen.
FRAME_KEY = '<span class="k-frame"><i></i>where this branch changed the screen</span>'

LEGEND = (
    '<div class="dsa-legend">'
    '<span class="k-ok"><i></i>design-system component</span>'
    '<span class="k-bad"><i></i>native or outside control where one belongs</span>'
    f"{FRAME_KEY}"
    "</div>")

DIFF_LEGEND = (
    '<div class="dsa-legend"><span class="k-new"><i></i>new or changed</span>'
    '<span class="dsa-ink"><i></i>differs</span>'
    f'<span class="dsa-ghost"><i></i>only moved</span>{FRAME_KEY}</div>')


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "screen"


def _title_case(name: str) -> str:
    """Every word's first letter up, everything else untouched — `str.title()`
    mangles an apostrophe (`Owner'S`) and would lowercase an acronym the catalogue wrote
    in caps. The summary line reads as a heading; the catalogue's own casing does not
    have to."""
    return re.sub(r"\b\w", lambda m: m.group().upper(), name)


def screen_touched(screen: dict) -> bool:
    """Did this branch modify this screen at all?

    A run audits every screen the app has, and most of them come back identical on both
    sides. Those are the screens the reviewer would scroll past — three full-page
    screenshots and a table of components that were already right — so they are not
    drawn at all: one line under the verdict names them, and only the screens the branch
    actually moved get a viewer. The catalogue is the whole app precisely so that the
    choice of *which* screens to look at is never made by hand — the DOM diff makes it.

    The question is asked of the delta, not of the findings: an element the audit does
    not judge is still a modification, and a screen whose only change is a paragraph is
    not an unchanged screen. `moved` is deliberately not counted — a field that only
    slid down because something above it grew is the one case the differ exists to call
    "nothing happened here".
    """
    dom = screen.get("delta", {}).get("dom", {})
    if dom.get("added") or dom.get("removed") or dom.get("changed"):
        return True
    if any(st.get("status") == "restyled"
           for st in screen.get("delta", {}).get("elements", {}).values()):
        return True
    counts = screen["summary"]
    return bool(counts["regressions"] or counts["improvements"])


def _n(k: int, word: str) -> str:
    return f'{k} {word}{"" if k == 1 else "s"}'


def delta_parts(counts: dict, *, long: bool = False, gap_tip: str = "") -> list[str]:
    """What this branch did to the design-system picture, said only as the non-zero
    signed deltas: `+1 gap`, `+1 component`, `−1 gap`.

    The totals it replaces — `1 gap · 0 components`, `0 gaps · 1 component (+1 on this
    branch)`, `all controls from the design system` — were counts of the *screen*, not of
    the change, and most of them were zero or a restatement: a reviewer reading a
    branch's page asks what it added, and "0 gaps" answers nothing. A zero is omitted, so
    a screen the branch changed without touching any control the design system covers
    simply has no tail.

    `gap` is a native control where a design-system component belongs, and `+N` counts
    only the ones this branch is to blame for (`regressions`); it is the one warning on
    the line and wears the warning's yellow. A gap the base already had is still a gap,
    but not a delta, so it trails, muted. `−N gap` is a bare control the branch migrated.
    `component` is a design-system component rendered on the screen, as a difference
    between the two sides — a migration shows as both `−1 gap` and `+1 component`, which
    is what it is. `long` spells the words out for the tab's header, where the line has
    no screen around it to explain them."""
    parts = []
    reg, fixed = len(counts["regressions"]), len(counts["improvements"])
    pre = len(counts.get("pre_existing") or [])
    d = counts["new"]["ds"] - counts["old"]["ds"]
    if reg:
        word = _n(reg, "gap")
        tip = gap_tip or "Control outside the design system"
        parts.append(f'<span class="dsa-gap" data-tip-html="{html.escape(tip, quote=True)}">'
                     f'{"\u26a0 " if long else ""}+{word}</span>')
    if d:
        sign = "+" if d > 0 else "\u2212"
        what = "design-system component" if long else "component"
        # Uses, not kinds: two combos on one screen are 2. The header sums every screen.
        scope = "all screens" if long else "this screen"
        tip = (f'Design-system component uses, {scope}: '
               f'{counts["old"]["ds"]} → {counts["new"]["ds"]}')
        parts.append(f'<span class="dsa-comp" data-tip="{html.escape(tip, quote=True)}">'
                     f'{sign}{_n(abs(d), what)}</span>')
    if fixed:
        parts.append(f'<span class="dsa-fixed" data-tip="bare on the base, migrated into a '
                     f'design-system component by this branch">\u2212{_n(fixed, "gap")}</span>')
    if pre:
        parts.append(f'<span class="dsa-pre" data-tip="already bare on the base; this branch '
                     f'did not add it and did not close it">{_n(pre, "gap")} '
                     f'already on the base</span>')
    # The controls considered and let past, as a delta too. `uncovered` going 41 → 42 under
    # a tab that said nothing was a control the branch added that the audit never judged;
    # said here, "0 gaps" can no longer be mistaken for "not looked at".
    u = counts["new"].get("uncovered", 0) - counts["old"].get("uncovered", 0)
    if u:
        sign = "+" if u > 0 else "\u2212"
        what = "control no design-system component claims" if long else "not judged"
        if long and abs(u) != 1:
            what = "controls no design-system component claims"
        parts.append(f'<span class="dsa-pre" data-tip="native controls in a role no '
                     f'design-system component claims: {counts["old"].get("uncovered", 0)} on '
                     f'the base, {counts["new"].get("uncovered", 0)} on this branch \u2014 '
                     f'considered and deliberately not judged">{sign}{abs(u)} {what}</span>')
    return parts


def screen_has_nothing_to_judge(screen: dict) -> bool:
    """A changed screen with no control in a role the design system covers: a list that
    grew a column, a detail page. The audit has no verdict on it, and its heading has to
    say that rather than print `0 gaps · 0 components` as if that were a clean bill."""
    c = screen["summary"]
    return not any(c[side].get(k) for side in ("new", "old") for k in ("bare", "ds", "foreign"))


NOTHING_TIP = "Changed, but no control on it is, or should be, a design-system component"


def render_screen(screen: dict, assets_prefix: str, build, registry: dict | None = None,
                  root: Path | None = None, base: str | None = None,
                  browsers: list | None = None) -> str:
    findings = screen["findings"]
    pages = {s: screen["sides"][s]["page"] for s in ("new", "old")}
    stem = f'{assets_prefix}ds-audit-{slug(screen["screen"])}'

    frames = screen.get("frames") or {}
    nums, numbered = number_findings(findings, frames)
    fnums = frame_numbers(findings, frames)

    def annotated(side):
        marks = [dict(m, num=nums.get(m["id"], (None, False))[0],
                      in_frame=nums.get(m["id"], (None, False))[1])
                 for m in _marks_for(findings, side, registry)]
        return LEGEND + shot_html(f"{stem}-{side}.png", pages[side], marks,
                                  frames.get(side), numbered, fnums, alts(side, marks))

    layouts = screen.get("browser_layout") or {}

    def alts(side, marks):
        """The marks and frames of `side` once more for each other browser, moved to
        where that browser laid each element out. A frame keeps its number only when that
        browser cut as many frames as the primary did — the numbers key the table."""
        out = []
        for br, lay in sorted(layouts.items()):
            boxes = lay["boxes"].get(side) or {}
            fr = (lay.get("frames") or {}).get(side) or []
            same = len(fr) == len(frames.get(side) or [])
            out.append({"br": br, "page": lay["pages"][side], "frames": fr,
                        "fnums": fnums if same else {},
                        "marks": [dict(m, box=boxes[m["id"].removesuffix(":d")])
                                  for m in marks if m["id"].removesuffix(":d") in boxes]})
        return out

    # The delta pane: the pixel mask over the new shot, with the elements the DOM says
    # are new or changed outlined on top of it. Neither half is enough on its own.
    delta_marks = []
    for f in findings:
        if f["side"] != "new" or f["verdict"] == "internal":
            continue
        st = f.get("delta", {})
        if st.get("status") in ("added", "changed", "restyled"):
            delta_marks.append({
                "id": f["id"] + ":d", "cls": "new", "box": f["box"],
                "badge": f'{st["status"]}: {element_name(f)}',
                "tip": ""})  # the badge says it: `added: app-combo`
    # Order is the control's, not the reader's: Diff is always the first button. Which
    # one *opens* is a separate question, and here the answer is New — see the call to
    # `dgm_views_html` at the bottom of this function.
    panes = [("diff", DIFF_LEGEND + shot_html(f"{stem}-delta.png", pages["new"], delta_marks,
                                                      frames.get("new"), numbered, fnums,
                                                      alts("new", delta_marks))),
             ("new", annotated("new")), ("old", annotated("old"))]

    rows = []
    for f in sorted(findings, key=lambda f: (f["verdict"] not in ("bare", "foreign"),
                                             f["side"] != "new", f["box"]["y"])):
        if f["verdict"] in ("internal", "uncovered"):
            continue
        st = f.get("delta", {})
        cls = ("bad" if f["verdict"] in ("bare", "foreign") and not f.get("resolved")
               else "ok")
        word = "gap" if cls == "bad" else ("fixed" if f.get("resolved") else "ok")
        churn_txt = "\u2014" if st.get("pixel_churn") is None else f'{st["pixel_churn"]:.0%}'
        # The number the picture carries for it \u2014 on its frame, or on the mark itself.
        num, framed = nums.get(f["id"], (None, False))
        view = "New" if f["side"] == "new" else "Old"
        chip = (f'<b class="dsa-rnum {"framed" if framed else cls}" data-tip="'
                f'{"Frame" if framed else "Mark"} {num} on the {view} picture">{num}</b>'
                if numbered and num else "")
        rows.append(
            f'<tr class="{cls}" data-find="{html.escape(f["id"])}">'
            f'<td>{chip}<span class="dsa-v {cls}">{word}</span></td>'
            f'<td>{html.escape(screen["sides"][f["side"]]["label"])}</td>'
            f'<td><b>{html.escape(element_name(f))}</b>'
            f'<br><code class="dsa-sel" data-tip="{html.escape(f["selector"])}">'
            f'{selector_html(short_selector(f["selector"]))}</code></td>'
            f'<td>{html.escape(f["role"] or "")}</td>'
            + f'<td>{f["message"]}'
            + (f'<br><span class="dsa-prov">{f["history"]}</span>'
               if f.get("history") else "") + '</td>'
            f'<td>{html.escape(st.get("status", "\u2014"))}</td>'
            f'<td>{churn_txt}</td></tr>')
        # The element itself, under its row: the markup, the template line, the rule.
        rows.append(
            f'<tr class="dsa-more {cls}" data-find="{html.escape(f["id"])}"><td></td>'
            f'<td colspan="6">{detail_html(f, registry or {}, screen["screen"], root, base)}'
            "</td></tr>")

    counts = screen["summary"]
    # One line: the fold's arrow, the verdict icon, the name, the route, and the two
    # counts that used to sit on lines of their own under it. Opening it is what "pictures
    # and findings" used to be a second fold for.
    gaps = counts["new"]["bare"] + counts["new"].get("foreign", 0)
    # ✅ reads "audited and fine". A screen with no control the audit judges was not
    # audited at all, only found changed, so it wears a neutral mark and says why.
    blank = screen_has_nothing_to_judge(screen)
    icon = "\u26a0\ufe0f" if gaps else ("\u26aa" if blank else "\u2705")
    route = screen.get("route")
    route_html = (f' <span class="dsa-route">({html.escape(route)})</span>' if route else "")
    # Only what the branch changed, as signed deltas (`delta_parts`); a screen with none
    # gets no tail at all rather than `all controls from the design system`, which read
    # as a claim and said nothing.
    parts = delta_parts(counts) or ([f'<span class="dsa-pre" data-tip="{NOTHING_TIP}">'
                                     "nothing to judge</span>"] if blank else [])
    tail = (f' <span class="dsa-sumtail">\u00b7 {" \u00b7 ".join(parts)}</span>'
            if parts else "")
    summary = (f'{icon} {html.escape(_title_case(screen["screen"]))}{route_html}{tail}')

    # Every changed screen gets the table, the place a reviewer looks for the verdicts. A
    # screen with nothing in it to judge says so in the table's own body: no table at all
    # read the same as a table nobody filled in.
    if not rows:
        rows.append('<tr class="none"><td></td><td colspan="6">No control on this screen '
                    "is a design-system component or fills a role one covers \u2014 "
                    "nothing to judge here; what was considered is listed below.</td></tr>")
    table = ('<table class="dsa-table"><thead><tr><th></th><th>side</th><th>element</th>'
             '<th>role</th><th>why</th><th>delta</th><th>churn</th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table>")

    # What the audit looked at and let past. "Nothing was flagged" is not a claim anyone
    # can check; "these five controls were considered, and here is the role each one
    # fills and why no component claims it" is. It is also the only place a reviewer sees
    # a control the team left native *on purpose* — a multi-select the design system has
    # no component for — being recognised as that rather than missed.
    passed = [f for f in findings if f["side"] == "new" and f["verdict"] == "uncovered"]
    considered = ""
    if passed:
        items = "".join(
            f'<li><b>{html.escape(element_name(f))}</b>'
            + ("" if AUTO_ID.match(f["selector"].lstrip("#"))
               else f' <code>{html.escape(f["selector"])}</code>')
            + f' \u2014 role <code>{html.escape(f["role"] or "")}</code>, not covered</li>'
            for f in passed)
        considered = (f'<details class="dsa-considered"><summary>{len(passed)} control'
                      f'{"" if len(passed) == 1 else "s"} considered and deliberately not '
                      f'judged</summary><ul>{items}</ul></details>')
    # Every screen folds shut on load, the way the Sequence tab's `details.testpair`
    # rows do: a run audits the whole catalogue, and the summary line already carries the
    # one thing worth scanning for — the icon — without paying for a picture nobody
    # asked to see yet.
    return (f'<div class="dsa">'
            f'<details class="dsa-screen" id="dsa-{slug(screen["screen"])}"'
            f'{_brs_attr(screen)}>'
            f'<summary><span class="disclose" aria-hidden="true"></span>{summary}</summary>'
            f'{_with_bar_extra(_with_frame_toggle(build.dgm_views_html(panes, initial="new"), frames), screen_switch(screen, browsers))}'
            f'{table}{considered}</details></div>')


def _brs_attr(screen: dict) -> str:
    """The browsers other than the primary this screen was also shot in, for the switch."""
    brs = " ".join(sorted(screen.get("browsers") or {}))
    return f' data-brs="{html.escape(brs)}"' if brs else ""


def _major(version: str) -> str:
    return (version or "").split(".")[0]


def _br_button(b: dict, tip: str, pressed: bool, primary: bool) -> str:
    """One browser button: its logos (`BROWSERS[...]["logos"]`) when `LOGOS` has them, its
    name otherwise."""
    logos = [x for x in (BROWSERS.get(b["id"]) or {}).get("logos", ())
             if (LOGOS / f"{x}.png").is_file()]
    face = ("".join(f'<i class="dsa-logo" data-logo="{x}" aria-hidden="true"></i>'
                    for x in logos) or html.escape(b["label"]))
    return (f'<button type="button" class="dsa-br" data-br="{html.escape(b["id"])}"'
            + (' data-primary=""' if primary else "")
            + f' aria-pressed="{"true" if pressed else "false"}"'
            f' aria-label="{html.escape(_aka(b))}"'
            f' data-tip="{html.escape(tip, quote=True)}">{face}</button>')


def _aka(b: dict) -> str:
    """`Chrome and Edge` for the button that carries both logos, else the browser's name."""
    logos = (BROWSERS.get(b["id"]) or {}).get("logos", ())
    others = [BROWSERS[x]["label"] for x in logos if x != b["id"] and x in BROWSERS]
    return " and ".join([b["label"], *others])


#: Said on a non-primary browser's tooltip: why the boxes vanish when it is picked.
MARKS_HIDDEN = "Marks and frames are hidden: they were measured on {primary}'s layout."
#: Said instead when the marks and frames were re-measured on that browser's own layout.
MARKS_REMEASURED = "Marks and frames re-measured on {name}'s own layout."
#: Said on the primary's tooltip when its button carries Edge's logo too.
SAME_AS_EDGE = ("Edge draws them the same: it is Chromium too, and shot side by side the "
                "two came out pixel-identical.")


def _vs_primary(frac: float | None, primary: str) -> str:
    if frac is None:
        return ""
    return (f"pixel-identical to {primary}" if not frac
            else f"{frac:.2%} of its pixels differ from {primary}")


def browser_switch(result: dict) -> str:
    """The browser logos beside the tab's title: click one and every picture below is
    shown as that browser drew it. Nothing when the run was shot in one browser only. Each
    tooltip says what it shows, then how far that browser's pictures are from the
    primary's — "identical on all 5" is the answer a reviewer usually gets, and the one
    worth having in writing."""
    brs = result.get("browsers") or []
    if len(brs) < 2:
        return ""
    primary = brs[0]
    touched = [sc for sc in result.get("screens") or [] if screen_touched(sc)]
    out = []
    for i, b in enumerate(brs):
        name = f'{b["label"]} {_major(b.get("version", ""))}'.strip()
        tip = f"Every picture below as {name} drew it, base and branch. "
        if i == 0:
            tip += ("The marks and verdicts were judged on these."
                    + (" " + SAME_AS_EDGE if "edge" in _aka(b).lower() else ""))
        else:
            got = [(sc, (sc.get("browsers") or {}).get(b["id"])) for sc in touched]
            got = [(sc, d) for sc, d in got if d]
            off = [(sc, max(v for v in d.values() if v is not None))
                   for sc, d in got if any(v for v in d.values())]
            if not got:
                tip += "No screen was shot in it."
            elif not off:
                tip += (f"Pixel-identical to {primary['label']} on all {len(got)} "
                        f"changed screen{'' if len(got) == 1 else 's'}.")
            else:
                worst = max(off, key=lambda t: t[1])
                tip += (f"Differs from {primary['label']} on {len(off)} of {len(got)} "
                        f"changed screens — most on {_title_case(worst[0]['screen'])}, "
                        f"{worst[1]:.2%} of its pixels.")
            if got:
                laid = all(b["id"] in (sc.get("browser_layout") or {}) for sc, _ in got)
                tip += " " + (MARKS_REMEASURED.format(name=b["label"]) if laid
                              else MARKS_HIDDEN.format(primary=primary["label"]))
        out.append(_br_button(b, tip, i == 0, i == 0))
    return ('<span class="dsa-browsers dsa-global" role="group" aria-label="Browser every '
            'picture is shown as">' + "".join(out) + "</span>")


def screen_switch(screen: dict, browsers: list | None) -> str:
    """The same logos on one screen's own bar: that screen only. Only on a screen the
    other browser shot too — elsewhere there is nothing to swap to."""
    brs = browsers or []
    shot = screen.get("browsers") or {}
    if len(brs) < 2 or not shot:
        return ""
    primary = brs[0]
    out = []
    for i, b in enumerate(brs):
        if i and b["id"] not in shot:
            continue
        name = f'{b["label"]} {_major(b.get("version", ""))}'.strip()
        tip = f"This screen as {name} drew it, base and branch"
        if i:
            d = shot[b["id"]]
            vs = {side: _vs_primary(d.get(side), primary["label"]) for side in ("new", "old")}
            tip += (f": {vs['new']}." if vs["new"] == vs["old"]
                    else f": branch {vs['new']}, base {vs['old']}.")
            tip += " " + (MARKS_REMEASURED.format(name=b["label"])
                          if b["id"] in (screen.get("browser_layout") or {})
                          else MARKS_HIDDEN.format(primary=primary["label"]))
        else:
            tip += "." + (" " + SAME_AS_EDGE if "edge" in _aka(b).lower() else "")
        out.append(_br_button(b, tip, i == 0, i == 0))
    return ('<span class="dsa-browsers dsa-local" role="group" aria-label="Browser this '
            'screen is shown as">' + "".join(out) + "</span>")


def _with_bar_extra(viewer: str, extra: str) -> str:
    """`extra` appended inside the viewer's button bar, after what is already there."""
    if not extra or '<div class="dgmbar">' not in viewer:
        return viewer
    head, sep, rest = viewer.partition('<div class="dgmbar">')
    bar, close, tail = rest.partition("</div>")
    return head + sep + bar + extra + close + tail


FRAME_TOGGLE = ('<label class="dsa-frametoggle">'
                '<input type="checkbox" class="dsa-frameon" checked> frame the changes</label>')


def _with_frame_toggle(viewer: str, frames: dict) -> str:
    """The checkbox rides on the viewer's own button bar, after Diff and New/Old. The
    viewer is the report's shared one, so it is appended to its markup here rather than
    taught a new option; a screen with no frames gets no checkbox that would do nothing."""
    if not (frames.get("new") or frames.get("old")) or '<div class="dgmbar">' not in viewer:
        return viewer
    head, sep, rest = viewer.partition('<div class="dgmbar">')
    bar, close, tail = rest.partition("</div>")
    return head + sep + bar + FRAME_TOGGLE + close + tail


def _side_name(meta: dict) -> str:
    """`label (sha)`, or just the sha when the label already is its short form."""
    label, commit = meta.get("label") or "", (meta.get("commit") or "")[:8]
    if label and commit and label != commit:
        return f"{label} ({commit})"
    return label or commit or "?"


COUNT_TIP = "Base and branch, opened side by side. Click: how \u201cchanged\u201d is decided."


def unchanged_pixels(result: dict) -> tuple[list[dict], list[dict]] | None:
    """The screens the count leaves out, split into the pixel-identical ones and the rest.
    None when there are none, or when a screen carries no raw count (a JSON older than
    `pixels_differ`): then the page claims nothing it did not measure."""
    rest = [sc for sc in result["screens"] if not screen_touched(sc)]
    if not rest or any(sc.get("pixels_differ") is None for sc in rest):
        return None
    return ([sc for sc in rest if sc["pixels_differ"] == 0],
            [sc for sc in rest if sc["pixels_differ"]])


def count_tip(result: dict) -> str:
    """The count's tooltip opens on what it leaves out: "5 of 19 changed" says nothing
    about the other 14 until it says how they were found unchanged."""
    split = unchanged_pixels(result)
    if split is None:
        return COUNT_TIP
    same, differ = split
    k = len(same) + len(differ)
    if not differ:
        head = (f"The other {k}: every pixel identical on base and branch, compared raw "
                "\u2014 no blur, no tolerance.")
    else:
        head = (f"The other {k}: {len(same)} pixel-identical; "
                + ", ".join(sc["screen"] for sc in differ)
                + (" differs" if len(differ) == 1 else " differ")
                + " by a few pixels, with no element changed.")
    return f"{head} Click: how \u201cchanged\u201d is decided."


def how_box(result: dict) -> str:
    """The box the count opens: how the screens were compared, and what makes one
    "changed" (`screen_touched` and `combine`, in words)."""
    screens = result["screens"]
    sides = (screens[0].get("sides") if screens else None) or {}
    new, old = _side_name(sides.get("new") or {}), _side_name(sides.get("old") or {})
    c = lambda t: f"<code>{html.escape(t)}</code>"
    return (
        '<div class="dsa-howbox" id="dsa-howbox" hidden>'
        "<p><b>How the screens were compared</b></p><ul>"
        f"<li>All {len(screens)} screens listed in {c('human-review.json')}, each opened "
        "twice, side by side</li>"
        f"<li>base {c(old)} \u00b7 branch {c(new)}</li>"
        "<li>same seeded data, same browser, animations off</li></ul>"
        "<p><b>A screen counts as changed when</b></p><ul>"
        "<li>an element was added, removed or changed (moving alone doesn\u2019t count)</li>"
        "<li>the DOM is identical, but an element repainted more than "
        f"{RESTYLE_CHURN:.0%} of its own box</li>"
        "<li>the branch added or removed a gap or a design-system component</li></ul>"
        + unchanged_box(result) + "</div>")


def unchanged_box(result: dict) -> str:
    """The how-box's last word: the screens the count left out, and the proof they are."""
    split = unchanged_pixels(result)
    if split is None:
        return ""
    same, differ = split
    k = len(same) + len(differ)
    rows = []
    if same:
        rows.append(f"<li>{'all ' if not differ else ''}{len(same)} pixel-identical: same "
                    "size, every pixel equal, compared raw \u2014 no blur, no anti-alias "
                    "filter, no threshold</li>")
    rows += [f'<li>{html.escape(sc["screen"])}: {sc["pixels_differ"]:,} pixels differ, but '
             "no element was added, removed, changed or repainted</li>" for sc in differ]
    return f"<p><b>The other {k} screen{'' if k == 1 else 's'}</b></p><ul>{''.join(rows)}</ul>"


def regression_tip(result: dict) -> str:
    """What the header's "introduced by this branch" count is made of, named per control.

    The count alone read as "1 regression = ?": a regression of what, where. It counts the
    gaps this branch is to blame for — a native control it added bare, or one that was a
    design-system component on the base — as opposed to a gap already bare on the base.
    """
    rows = []
    for sc in result["screens"]:
        ids = set(sc["summary"]["regressions"])
        for f in sc["findings"]:
            if f["id"] in ids and f["side"] == "new" and f["verdict"] in ("bare", "foreign"):
                el = f["element"]
                tag = f'<{el["tag"]}' + (f' id="{el["id"]}"' if el.get("id")
                                         and not AUTO_ID.match(el["id"]) else "") + ">"
                name = element_name(f)
                if f["verdict"] == "foreign":
                    rows.append(f'{html.escape(sc["screen"])}: {html.escape(name)} '
                                f'{html.escape(tag)}, from outside the design system')
                    continue
                where = " or ".join(f.get("expected_ds") or []) or "a DS component"
                was = f.get("history", "").startswith("was a design-system")
                rows.append(f'{html.escape(sc["screen"])}: {html.escape(tag)} where '
                            f'{html.escape(where)} belongs'
                            + (" (a DS component on the base)" if was else ""))
    return ('<p class="tipfoot">New controls outside the design system:</p>'
            '<ul class="tiplist">'
            + "".join(f"<li>{r}</li>" for r in rows) + "</ul>")


def render(result: dict, assets_prefix: str, *, root: Path | None = None,
           base: str | None = None) -> str:
    """The fragment: the registry once, then one three-state viewer per screen.

    The viewer is the report\u2019s own \u2014 the Diff / New-Old control built last round
    for exactly this shape of content. A second one with different ergonomics on the same
    page would be the mistake worth failing a build over.

    `root` is the checkout the templates a verdict points at live in, and `base` the
    commit the branch is compared with; with both, each template line gets the page's two
    handles (the diff in VS Code, the change on github.com). Without, the bar still links
    the file at its line.
    """
    build = _build_review()
    counts = result["summary"]
    touched = [sc for sc in result["screens"] if screen_touched(sc)]
    n = len(result["screens"])
    # The tab's title says what the branch changed and nothing else, in the same signed
    # deltas each screen's line uses, words spelt out because there is no screen around it
    # to explain them. It used to open on `1 gap · 1 introduced by this branch`: the same
    # gap counted twice, then `4 components (+1 on this branch)`, a total over every
    # screen of the app, changed or not, that left "components" undefined.
    deltas = delta_parts(counts, long=True, gap_tip=regression_tip(result))
    # Nothing to say is still said: a bare "1 of 19 screens changed" could not be told
    # apart from a tab whose audit never judged anything.
    if touched and not deltas:
        deltas = ['<span class="dsa-pre">no gap and no design-system component added or '
                  "removed</span>"]
    verdict_line = " \u00b7 ".join(
        [f'<span class="dsa-count" role="button" tabindex="0" aria-expanded="false" '
         f'aria-controls="dsa-howbox" data-tip="{html.escape(count_tip(result), quote=True)}">'
         f'{len(touched)} of {n} screens changed</span>'] + deltas)

    # The embedded copy drops the per-element table. It is keyed on every signature on
    # every screen — 190KB of it on a seven-screen run, most of the fragment's weight —
    # and it is redundant here: each finding already carries its own `delta`. The file
    # written by --json keeps it, and the payload says where to find it.
    embedded = dict(result, screens=[
        dict(sc, delta={k: v for k, v in sc["delta"].items() if k != "elements"})
        for sc in result["screens"]])
    embedded["full_json"] = "the --json file beside this page carries delta.elements too"
    payload = json.dumps(embedded, separators=(",", ":")).replace("</", "<\\/")
    # A changed screen the catalogue does not reach is the one finding this audit cannot
    # make by itself, so it is the first thing on the page and it is red: the DOM diff
    # decides which screens matter, but only among the screens it was given.
    unlisted = result.get("unlisted") or []
    unlisted_line = "".join(
        f'<p class="dsa-unlisted">\u26a0 <b>Not audited:</b> '
        f'<code>{html.escape(u["route"])}</code> '
        f'(<code>{html.escape(u["component"])}</code>'
        + (f' via <code>{html.escape(u["via"])}</code>' if u.get("via") else "")
        + ') changed and is not in <code>steps.dsaudit.screens</code>.</p>'
        for u in unlisted)
    return (
        f'<!-- ds-audit render {RENDER_STAMP} -->'
        '<div class="dsa-run">'
        f'<h2 class="tabtitle">UX design system{browser_switch(result)}</h2>'
        f'{unlisted_line}'
        f'<p class="dsa-hdr">{verdict_line}</p>'
        f'{how_box(result)}'
        # Screens with a verdict first — a gap, a regression, a component — so the tab
        # opens on a marked-up picture; the changed-but-nothing-to-judge ones trail.
        + "".join(render_screen(sc, assets_prefix, build, result.get("registry") or {},
                                root, base, result.get("browsers"))
                  for sc in sorted(touched, key=screen_has_nothing_to_judge))
        + f'<script type="application/json" class="ds-audit-data">{payload}</script>'
        + HL_JS + "</div>")


# ── capture ───────────────────────────────────────────────────────────────────────

#: The browsers a screen is shot in. The first one found is the *primary*: its captures
#: carry every verdict, mark and change frame, and its PNGs keep the names they always had.
#: Every other one re-shoots only the changed screens, into `<name>.<id>.png`, so the
#: tab's switch can swap the pictures. A browser not installed is skipped with a line on
#: stderr; with none of them, the bundled Chromium stands in.
#:
#: `logos` is what its button shows. Chrome's carries Edge's logo too: Edge is Chromium —
#: same Blink, same Skia — and shot side by side (3 Nov 2026, Chrome 156 and Edge 156) the
#: two drew every screen byte for byte the same, so a second button for Edge offered a
#: choice that changes nothing. Safari is the engine that draws differently: WebKit, the
#: build Playwright bundles (`paths` None), not the Safari.app on this Mac.
BROWSERS = {
    "chrome": {"label": "Chrome", "engine": "chromium", "logos": ("chrome", "edge"),
               "paths": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                         "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                         "/opt/google/chrome/chrome")},
    "edge": {"label": "Edge", "engine": "chromium", "logos": ("edge",),
             "paths": ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/opt/microsoft/msedge/msedge")},
    "safari": {"label": "Safari", "engine": "webkit", "logos": ("safari",), "paths": None},
}

#: The flags every Chromium capture is taken with; WebKit takes none of them.
CHROMIUM_ARGS = ["--force-color-profile=srgb", "--font-render-hinting=none",
                 "--disable-lcd-text"]


def _bundled(engine: str) -> bool:
    """Whether Playwright has its own build of `engine` installed."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(getattr(p, engine).executable_path).exists()
    except Exception:
        return False


def resolve_browsers(wanted: list[str]) -> list[dict]:
    """`[{"id", "label", "launch"}]` for each browser in `wanted` that is installed, in the
    order asked. `launch` names the engine and, for an installed app, its executable —
    found here rather than through Playwright's `channel`, which only looks in
    /Applications: an Edge unpacked into ~/Applications without sudo is still Edge."""
    out = []
    for b in wanted:
        if b not in BROWSERS:
            raise SystemExit(f"[ds-audit] unknown browser {b!r} — one of {', '.join(BROWSERS)}")
        spec = BROWSERS[b]
        if spec["paths"] is None:
            if _bundled(spec["engine"]):
                out.append({"id": b, "label": spec["label"],
                            "launch": {"engine": spec["engine"]}})
                continue
        else:
            exe = next((Path(x).expanduser() for x in spec["paths"]
                        if Path(x).expanduser().is_file()), None)
            if exe:
                out.append({"id": b, "label": spec["label"],
                            "launch": {"engine": spec["engine"], "executable_path": str(exe)}})
                continue
        print(f'[ds-audit] {spec["label"]} is not installed — not shot in it', file=sys.stderr)
    return out or [{"id": "chromium", "label": "Chromium", "launch": {"engine": "chromium"}}]


def browser_png(png: Path, browser: str | None) -> Path:
    """Where a capture taken in `browser` is written: the primary keeps the plain name."""
    return png if not browser else png.with_name(f"{png.stem}.{browser}.png")


def browser_diff(a_png: Path, b_png: Path, threshold: float = 0.1) -> float | None:
    """The share of pixels two browsers drew differently on the same screen — what the
    switch's tooltip quotes. None when either picture is missing; pages of different
    heights are compared over the part they share, and the height gap counts as different."""
    try:
        import numpy as np
        from PIL import Image
        a = np.asarray(Image.open(a_png).convert("RGB"))
        b = np.asarray(Image.open(b_png).convert("RGB"))
    except Exception:
        return None
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    diff = int(_yiq_mask(a[:h, :w], b[:h, :w], threshold).sum())
    total = max(a.shape[0], b.shape[0]) * max(a.shape[1], b.shape[1])
    diff += total - h * w
    return diff / total if total else 0.0


def browser_layout(screen: dict, snaps: dict, pngs: dict) -> dict:
    """Where another browser put what the primary judged: each finding's box re-read from
    that browser's own DOM (by the element's signature, the key a finding's id carries),
    the change frames re-cut from that browser's own pictures, and its pictures' size, the
    denominator the overlay's percentages need. Without it the marks drawn on Safari's
    picture sat where Chrome had laid the element out, so they were hidden — and Safari
    showed no change at all (Victor, 3 Nov 2026: "nu se văd diffuri pe safari")."""
    boxes = {}
    for side in ("new", "old"):
        by_sig = {n["sig"]: n["box"] for n in snaps[side].get("nodes") or []}
        boxes[side] = {f["id"]: by_sig[f["element"]["sig"]] for f in screen["findings"]
                       if f["side"] == side and f["element"]["sig"] in by_sig}
    return {"pages": {side: _png_size(pngs[side], snaps[side].get("page") or {"w": 1, "h": 1})
                      for side in ("new", "old")},
            "boxes": boxes,
            "frames": change_frames(pngs["old"], pngs["new"])}


def answers(url: str, timeout: float = 3.0) -> bool:
    """Whether anything at all is serving `url` (any HTTP status counts — a dev server
    answers every path with the same index). Kept in step with `run-steps.py`'s twin: the
    runner gates on it before the ledger stamp, and this one is the last line of defence
    for a direct invocation, so that "nobody is listening on 4301" is said in one line
    here rather than by Playwright, in a traceback, after the browser was launched."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def unreachable(wanted: list, label_new: str, label_old: str) -> list[str]:
    """`"<origin> (<side>: <label>)"` for every http(s) origin in `wanted` that does not
    answer — one entry per origin, not per screen, because the screens of one side all
    live on the same server and the message should name the server."""
    origins: dict[str, str] = {}
    for _name, new_url, old_url in wanted:
        for url, side in ((new_url, f"new: {label_new}"), (old_url, f"old: {label_old}")):
            m = re.match(r"^(https?://[^/]+)", url)
            if m:
                origins.setdefault(m.group(1), side)
    return [f"{o} ({side})" for o, side in origins.items() if not answers(o)]


def capture(url: str, png: Path, *, viewport, epoch: int, seed: int, wait_for: str | None,
            settle_ms: int, mask: list[str], color_scheme: str,
            launch: dict | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    pin = PIN_JS.replace("__EPOCH__", str(epoch)).replace("__SEED__", str(seed))
    with sync_playwright() as p:
        opts = dict(launch or {})
        engine = opts.pop("engine", "chromium")
        browser = getattr(p, engine).launch(
            args=CHROMIUM_ARGS if engine == "chromium" else [], **opts)
        version = browser.version
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
    snap["browser_version"] = version
    return snap


def capture_retrying(url: str, png: Path, **kw) -> dict:
    """`capture`, once more after a timeout. Thirty-eight captures of one run share the
    machine with two Docker stacks, and one page that missed its 60 s under load (3 Nov
    2026, load average 141) cost the whole audit; a second attempt costs one screen."""
    try:
        return capture(url, png, **kw)
    except Exception as e:
        if "Timeout" not in type(e).__name__:
            raise
        print(f"[ds-audit] {url}: {str(e).splitlines()[0]} — once more", file=sys.stderr,
              flush=True)
        return capture(url, png, **kw)


# ── main ──────────────────────────────────────────────────────────────────────────

def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True,
                              cwd=cwd).stdout.strip()
    except Exception:
        return ""


def page_base_commit(named: str, cwd: Path | None = None) -> str:
    """The commit the old side was built from, worked out the way `run-steps.py` works it
    out before it builds that side: the page's one base (the commit the review audited,
    when `review-points.md` records one inside the branch), forked from HEAD.

    `run-steps.py` used to be the only one who knew, and it passed nothing on: every
    screen's `sides.old.commit` was `''` and the side was labelled `main`, which was
    neither the review base nor the merge-base."""
    root = Path(cwd or Path.cwd())
    base = named
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from hrbuild.shared.chips import page_base
        st = page_base(root, None, named)
        if st and st.get("diffBaseSource") == "audited":
            base = st["diffBase"]
    except Exception:  # noqa: BLE001 - no answer is the named base, as run-steps does
        pass
    return _git("merge-base", base, "HEAD", cwd=root) or _git(
        "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", cwd=root)


def side_label(label: str, commit: str, cwd: Path | None = None) -> str:
    """A side's label is a ref name; one that does not point at the commit that side was
    rendered from is a wrong label, so it gives way to the commit. `main` over a build of
    the review base `5a97353e` becomes `5a97353e`; the branch name over HEAD stays."""
    if not commit:
        return label
    at = _git("rev-parse", "--verify", "--quiet", f"{label}^{{commit}}", cwd=cwd)
    return label if at and at == commit else commit[:8]


def stamp_sides(sides: dict, commits: dict, cwd: Path | None = None) -> dict:
    """Fill in each side's `commit` (where it is missing) and fix its label to match."""
    for side, meta in sides.items():
        if not meta.get("commit") and commits.get(side):
            meta["commit"] = commits[side]
        if meta.get("commit"):
            meta["label"] = side_label(meta.get("label") or side, meta["commit"], cwd)
    return sides


def _git_head(path: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def build_screen(name, old_snap, new_snap, registry, *, sides_meta, delta,
                 route: str | None = None) -> dict:
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
    rank = {"ds": 0, "bare": 1, "foreign": 1, "internal": 2, "uncovered": 3}
    by = {}
    for f in sorted(findings, key=lambda f: rank[f["verdict"]]):
        by.setdefault((f["side"], field[f["id"]]), f)

    # A kit control from outside the design system is this branch's gap only when this
    # branch brought it. One the base already renders on the same field — the vet form's
    # Material multi-select, left there on purpose — is a decision somebody already made:
    # considered and let past, like any control in a role nothing claims. One only the
    # base has is gone.
    for f in findings:
        if f["verdict"] != "foreign":
            continue
        twin = by.get(("old" if f["side"] == "new" else "new", field[f["id"]]))
        if f["side"] == "old" or (twin is not None and twin["verdict"] != "ds"):
            f["verdict"] = "uncovered"
            f["message"] += (" — the base already renders it, so it is context here, "
                             "not a gap this branch added" if twin is not None else
                             " — this branch removed it")

    def count(side):
        c = {"ds": 0, "bare": 0, "foreign": 0, "internal": 0, "uncovered": 0}
        for f in findings:
            if f["side"] == side:
                c[f["verdict"]] += 1
        return c

    regressions, improvements, preexisting = [], [], []
    for f in findings:
        # `foreign` is charged the same way: a kit control this branch added is a gap it
        # is to blame for, one the base already rendered is context.
        if f["side"] != "new" or f["verdict"] not in ("bare", "foreign"):
            continue
        was = by.get(("old", field[f["id"]]))
        if was and was["verdict"] == "ds":
            f["severity"] = "high"
            f["history"] = "was a design-system component on the base — this branch replaced it"
            regressions.append(f["id"])
        elif was is None:
            f["severity"] = "high"
            # Not "new"/"added": the element's delta column says that, and an element the
            # base already had (a <table> that gains matSort) reads "changed" there —
            # eval runs 14-18 flagged "changed" beside "new on this branch: added".
            f["history"] = ("brought in by this branch"
                            if f["verdict"] == "foreign" else
                            "this branch shipped it bare, never migrated")
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
        # Last, because it reads what the passes above decided: the history ("this branch
        # replaced it") and the migration (`resolved`) are both part of the sentence.
        if f["verdict"] in DRAWN:
            f["rule"] = rule_html(f, registry, name)

    return {
        "screen": name,
        "route": route,
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
    roll = {"new": {"ds": 0, "bare": 0, "foreign": 0, "internal": 0, "uncovered": 0},
            "old": {"ds": 0, "bare": 0, "foreign": 0, "internal": 0, "uncovered": 0}}
    regressions, preexisting, improvements = [], [], []
    for sc in screens:
        for side in ("new", "old"):
            for k, v in sc["summary"][side].items():
                roll[side][k] = roll[side].get(k, 0) + v
        regressions += sc["summary"]["regressions"]
        preexisting += sc["summary"]["pre_existing"]
        improvements += sc["summary"]["improvements"]
    return {
        "schema": SCHEMA,
        "generated": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "verdict": "gaps" if roll["new"]["bare"] or roll["new"]["foreign"] else "clean",
        "registry": registry,
        "determinism": {"pins": PINS,
                        "settled": {sc["screen"]: sc["settled"] for sc in screens}},
        "screens": screens,
        "summary": {**roll, "regressions": regressions, "pre_existing": preexisting,
                    "improvements": improvements, "weighting": WEIGHTING},
    }


def asset_prefix(raw: str) -> str:
    """`assets` and `assets/` both mean the folder next to the page.

    The prefix is glued straight onto the PNG name, so a caller who writes it the way
    every other `--assets`-style flag is written (a bare directory) got `assetsds-audit-…`
    and a page of broken images. Settled here, once, rather than at each of the places
    that join it: empty stays empty (the PNGs sit beside the fragment), anything else
    ends in exactly one slash.
    """
    return raw.rstrip("/") + "/" if raw else ""


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
    ap.add_argument("--unlisted", action="append", default=[], metavar="COMPONENT=ROUTE[=VIA]",
                    help="a changed routed component no --screen reaches; rendered as a red "
                         "warning at the top and carried in the JSON as `unlisted`")
    # Defaulted from the environment so the two origins can be *exported* by whoever
    # started the builds rather than written down. They used to be two fixed ports in
    # `human-review.json` (:4300 / :4301), which is a promise about somebody else's
    # machine: nothing in the pipeline started those builds, so the audit was skipped on
    # every run and the UX tab arrived empty. A caller that starts the two instances
    # itself gets ephemeral ports and can only pass them on — by flag, or through these.
    ap.add_argument("--base-new", default=os.environ.get("DS_AUDIT_BASE_NEW"),
                    help="origin the branch is served from, e.g. http://localhost:4300 "
                         "($DS_AUDIT_BASE_NEW)")
    ap.add_argument("--base-old", default=os.environ.get("DS_AUDIT_BASE_OLD"),
                    help="origin the base is served from, e.g. http://localhost:4301 "
                         "($DS_AUDIT_BASE_OLD)")
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
    ap.add_argument("--commit-new", help="commit the branch side was built from "
                                         "(default: HEAD of the working tree)")
    ap.add_argument("--commit-old", help="commit the base side was built from (default: the "
                                         "page base, worked out as run-steps.py does)")
    ap.add_argument("--base-ref", default="origin/main",
                    help="the ref the branch merges into, for the default --commit-old")
    ap.add_argument("--source", action="append", default=[],
                    help="tree scanned for design-system component sources (repeatable)")
    ap.add_argument("--viewport", default="1280x900")
    ap.add_argument("--wait-for", help="CSS selector to wait for before shooting")
    ap.add_argument("--mask", action="append", default=[],
                    help="CSS selector painted flat grey before the shot (repeatable) — "
                         "for the clock the page renders that no pin can freeze")
    ap.add_argument("--settle", type=int, default=250, help="ms between settle frames")
    ap.add_argument("--jobs", type=int, default=6,
                    help="captures taken at once, each in its own browser (default 6; 1 = "
                         "one after another)")
    ap.add_argument("--epoch", type=int, default=1756857600000,
                    help="the frozen wall clock, ms since epoch")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--color-scheme", default="light", choices=("light", "dark"))
    ap.add_argument("--browsers", default=os.environ.get("DS_AUDIT_BROWSERS", "chrome,safari"),
                    help="browsers to shoot in, comma-separated, of chrome, edge and safari "
                         "(default chrome,safari; "
                         "$DS_AUDIT_BROWSERS). The first installed one carries the verdicts; "
                         "the rest re-shoot the changed screens for the tab's switch")
    ap.add_argument("--threshold", type=float, default=0.1,
                    help="pixelmatch YIQ threshold (0..1)")
    ap.add_argument("--assets", default=".", help="directory the PNGs are written to")
    ap.add_argument("--asset-prefix", default="assets/",
                    help="how the fragment refers to the PNGs from the page")
    ap.add_argument("--from-capture", metavar="DIR",
                    help="skip the browser and re-render an earlier capture")
    ap.add_argument("--rerender", metavar="JSON",
                    help="no browser, no snapshots: re-render the fragment from an earlier "
                         "--json and the PNGs already in --assets, recomputing the change "
                         "frames; the JSON is rewritten with them")
    ap.add_argument("--rerender-if-stale", metavar="REVIEW_DIR",
                    help="re-render REVIEW_DIR/assets/ds-audit.html from the JSON beside it, "
                         "but only when another version of this script drew it")
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
    if args.rerender_if_stale:
        done = rerender_if_stale(Path(args.rerender_if_stale))
        if not done:
            print("[ds-audit] fragment already drawn by this ds-audit.py", file=sys.stderr)
        return
    args.asset_prefix = asset_prefix(args.asset_prefix)
    commits = {"new": args.commit_new or _git("rev-parse", "HEAD"),
               "old": args.commit_old or page_base_commit(args.base_ref)}
    sources = [Path(x) for x in args.source]
    repo_root = Path(_git("rev-parse", "--show-toplevel") or Path.cwd())
    if args.rerender:
        rerender(Path(args.rerender), Path(args.assets), args.asset_prefix, Path(args.out),
                 commits, sources=sources, repo_root=repo_root)
        return

    assets = Path(args.assets)
    assets.mkdir(parents=True, exist_ok=True)

    # What to audit: `NAME=PATH` against two origins, or explicit URL pairs. `routes`
    # rides beside `wanted` on the same key (the screen name) rather than folding into
    # its tuple: it is display-only, a name the catalogue already gave for free, and
    # every caller of `wanted` that doesn't care about it stays a 3-tuple unpack.
    wanted = []
    routes: dict[str, str] = {}
    for spec in args.screen:
        name, _, path = spec.partition("=")
        if not (args.base_new and args.base_old):
            ap.error("--screen needs --base-new and --base-old")
        routes[name] = "/" + (path or name).strip("/")
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
            routes[name] = _route_of(url)
        else:
            name = _name_from_url(new_url, i)
            wanted.append((name, new_url, old_url))
            routes[name] = _route_of(new_url)

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

    down = [] if cap_dir else unreachable(wanted, args.label_new, args.label_old)
    if down:
        print(f"[ds-audit] no app answering at {' and '.join(down)} — the audit compares "
              "two running builds; start it and re-run", file=sys.stderr)
        sys.exit(2)

    # Every capture launches its own Chromium and waits for the page to settle, and the
    # 38 of a petclinic run (19 screens, two builds) were taken one after another: the
    # audit was the review's critical path at 105 s, nearly all of it waiting. They share
    # nothing — each has its own browser, context and PNG — so they are taken in parallel
    # (`--jobs`, one sync Playwright per thread) and then read back in screen order, so
    # everything downstream of this loop is exactly what the serial run produced.
    browsers = resolve_browsers([b.strip() for b in args.browsers.split(",") if b.strip()])
    primary = browsers[0]
    shot: dict = {}
    if not cap_dir:
        from concurrent.futures import ThreadPoolExecutor
        todo = [(name, side, url) for name, new_url, old_url in wanted
                for side, url in (("new", new_url), ("old", old_url))]

        def take(job):
            name, side, url = job
            print(f"[ds-audit] {name}: capturing {url}", file=sys.stderr, flush=True)
            png = assets / f"ds-audit-{slug(name)}-{side}.png"
            return (name, side), capture_retrying(url, png, launch=primary["launch"], **common)

        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            shot = dict(pool.map(take, todo))

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
            pair = {side: shot[(name, side)] for side in ("new", "old")}
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
    registry = derive_registry(snaps, sources)
    templates = template_index(sources)
    changed = changed_files(commits["old"], sources)

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
        stamp_sides(sides_meta, commits)
        screen = build_screen(name, pair["old"], pair["new"], registry,
                              sides_meta=sides_meta,
                              delta={"dom": dom, "elements": elements},
                              route=routes.get(name))
        attach_sources(screen, templates, repo_root, changed)
        screen["pixels_differ"] = pixels_differing(pngs["old"], pngs["new"])
        if screen_touched(screen):
            screen["frames"] = change_frames(pngs["old"], pngs["new"])
        screens.append(screen)

    result = build_result(screens, registry)
    versions = {primary["id"]: next((s.get("browser_version") for s in shot.values()
                                     if s.get("browser_version")), "")}
    others = [] if cap_dir else browsers[1:]
    if others:
        urls = {name: {"new": n, "old": o} for name, n, o in wanted}
        todo = [(b, sc, side) for b in others for sc in screens if screen_touched(sc)
                for side in ("new", "old")]

        def retake(job):
            b, sc, side = job
            stem = slug(sc["screen"])
            print(f'[ds-audit] {sc["screen"]}: capturing {side} in {b["label"]}',
                  file=sys.stderr, flush=True)
            png = browser_png(assets / f"ds-audit-{stem}-{side}.png", b["id"])
            try:
                return job, capture_retrying(urls[sc["screen"]][side], png,
                                             launch=b["launch"], **common)
            except Exception as e:  # one browser failing must not cost the audit
                print(f'[ds-audit] {b["label"]} could not shoot {sc["screen"]} {side}: {e}',
                      file=sys.stderr)
                return job, None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            again = list(pool.map(retake, todo))
        for b in others:
            for sc in screens:
                if not screen_touched(sc):
                    continue
                got = {side: snap for (bb, s2, side), snap in again
                       if bb is b and s2 is sc and snap}
                if len(got) < 2:
                    continue
                versions.setdefault(b["id"], got["new"].get("browser_version", ""))
                stem = assets / f'ds-audit-{slug(sc["screen"])}'
                pngs = {side: browser_png(Path(f"{stem}-{side}.png"), b["id"])
                        for side in ("new", "old")}
                pixel_delta(pngs["old"], pngs["new"],
                            browser_png(Path(f"{stem}-delta.png"), b["id"]), args.threshold)
                sc.setdefault("browsers", {})[b["id"]] = {
                    side: browser_diff(Path(f"{stem}-{side}.png"), pngs[side], args.threshold)
                    for side in ("new", "old")}
                sc.setdefault("browser_layout", {})[b["id"]] = browser_layout(sc, got, pngs)
    result["browsers"] = [{"id": b["id"], "label": b["label"],
                           "version": versions.get(b["id"], "")}
                          for b in browsers if b is primary or any(
                              b["id"] in (sc.get("browsers") or {}) for sc in screens)]
    result["unlisted"] = [dict(zip(("component", "route", "via"), u.split("=", 2)))
                          for u in args.unlisted]
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(render(result, args.asset_prefix, root=repo_root,
                                     base=commits["old"] or None), encoding="utf-8")

    s = result["summary"]
    print(f'[ds-audit] {args.out} · {len(screens)} screen(s), '
          f'{s["new"]["bare"] + s["new"]["foreign"]} gap(s), '
          f'{s["new"]["ds"]} component(s), {len(s["regressions"])} regression(s) '
          f'→ {args.json_out}', file=sys.stderr)


def changed_files(base: str, sources: list[Path]) -> list[str]:
    """Repo-relative paths under `sources` that differ between `base` and HEAD — the
    templates a source search tries first."""
    if not base or not sources:
        return []
    return _git("diff", "--name-only", base, "HEAD", "--",
                *[str(s) for s in sources]).split()


#: Which ds-audit.py drew a fragment: a hash of this file, written into the fragment's
#: first line. A page build pastes `assets/ds-audit.html` whole, and the step that writes it
#: (two Docker stacks, both sides captured) is never part of a plain refresh — so an edit to
#: how this script *renders* never reached a page whose capture was older. The visit-vet
#: report kept its 5 Oct fragment through every rebuild after the 7 Oct title change: no
#: `h2.tabtitle`, so the build gave the tab a bare "UX" title row and, with no title to join,
#: the prompt fell to the panel's end. With the stamp, `refresh-report.py` notices a fragment
#: drawn by other code and re-renders it from its JSON (`rerender_if_stale`), free.
RENDER_STAMP = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
_RENDER_STAMP = re.compile(r"<!-- ds-audit render ([0-9a-f]+) -->")


def rendered_by(fragment: str) -> str | None:
    """The `RENDER_STAMP` a fragment was drawn with, or None for one older than stamps."""
    m = _RENDER_STAMP.search(fragment[:200])
    return m.group(1) if m else None


def rerender_if_stale(review: Path) -> bool:
    """Re-render `<review>/assets/ds-audit.html` (and its stylesheet) from the JSON beside
    it when this script did not draw it. True when it did. No browser, no stacks: the
    verdicts and the PNGs are the capture's, only the drawing is this file's. Each side keeps
    the commit its capture recorded, and the template search reads the same `source` dirs
    the step was given (`steps.dsaudit.source` in `human-review.json`)."""
    assets = review / "assets"
    js, frag = assets / "ds-audit.json", assets / "ds-audit.html"
    if not js.is_file():
        return False
    try:
        if rendered_by(frag.read_text(encoding="utf-8")) == RENDER_STAMP:
            return False
    except OSError:
        pass
    result = json.loads(js.read_text(encoding="utf-8"))
    commits: dict[str, str] = {}
    for sc in result.get("screens") or []:
        for side, meta in (sc.get("sides") or {}).items():
            if meta.get("commit"):
                commits.setdefault(side, meta["commit"])
    root = Path(_git("rev-parse", "--show-toplevel", cwd=review) or review.resolve().parent)
    try:
        cfg = json.loads((root / "human-review.json").read_text(encoding="utf-8"))
        sources = [root / s for s in ((cfg.get("steps") or {}).get("dsaudit") or {})
                   .get("source") or []]
    except (OSError, ValueError, AttributeError):
        sources = []
    cwd = os.getcwd()
    os.chdir(root)       # `changed_files` and the template search run git from here
    try:
        rerender(js, assets, asset_prefix("assets"), frag, commits or None,
                 sources=sources, repo_root=root)
    finally:
        os.chdir(cwd)
    (assets / "ds-audit.css").write_text(CSS + "\n", encoding="utf-8")
    return True


def rerender(json_path: Path, assets: Path, prefix: str, out: Path,
             commits: dict | None = None, *, sources: list[Path] | None = None,
             repo_root: Path | None = None) -> None:
    """A generator change, seen without a second pair of builds: the result JSON already
    carries every verdict, and the frames only need the two PNGs beside it. A side whose
    commit was never recorded gets it now, from the same resolution a capture uses; a
    drawn verdict with no `source` yet is located now, when `--source` says where."""
    result = json.loads(json_path.read_text(encoding="utf-8"))
    templates = template_index(sources or [])
    changed = changed_files((commits or {}).get("old", ""), sources or [])
    for sc in result["screens"]:
        if commits:
            stamp_sides(sc["sides"], commits)
        stem = assets / f'ds-audit-{slug(sc["screen"])}'
        old, new = Path(f"{stem}-old.png"), Path(f"{stem}-new.png")
        if old.is_file() and new.is_file():
            sc["pixels_differ"] = pixels_differing(old, new)
            if screen_touched(sc):
                sc["frames"] = change_frames(old, new)
        if templates:
            for f in sc["findings"]:
                if f["verdict"] in DRAWN and not f.get("source"):
                    f.setdefault("snippet", snippet_of({**f["element"], "ds": f.get("ds")}))
                    f["source"] = locate_source(f, templates, repo_root or Path.cwd(),
                                                changed)
    json_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    out.write_text(render(result, prefix, root=repo_root,
                          base=(commits or {}).get("old") or None), encoding="utf-8")
    print(f"[ds-audit] re-rendered {out} from {json_path}", file=sys.stderr)


def _name_from_url(url: str, i: int) -> str:
    path = re.sub(r"^\w+://[^/]+", "", url).strip("/")
    return path or f"screen {i + 1}"


def _route_of(url: str) -> str:
    """The path `url` carries, front-slashed — `/pets/11/edit`, not `pets/11/edit`.
    Empty when the URL is bare (an origin with nothing after it is not a route worth
    parenthesising in a screen's summary line)."""
    path = re.sub(r"^\w+://[^/]+", "", url).strip("/")
    return f"/{path}" if path else ""


if __name__ == "__main__":
    main()
