#!/usr/bin/env python3
"""What the change cost in entry-point complexity, ranked against the whole app.

`endpoint-complexity.py` answers "how complex is each entry point's whole flow right now"
— REST endpoints, MCP tools, message listeners and jobs alike. A reviewer needs the
derivative of that: which entry points this branch made heavier, by how much, and whether
that lands on an already-expensive one or turns a cheap one into a hot spot. So this diffs
two of its JSON snapshots and renders the full ranked list, grouped by kind — touched rows
called out, untouched rows kept for scale, because "+3" only means something next to the
numbers it is standing among.

Colour reads as authorship, not as judgement: green is what the branch ADDED, red is what
it REMOVED.

Take the two snapshots at the merge-base and at HEAD:
    endpoint-complexity.py --base origin/main --out before.json
    endpoint-complexity.py --out after.json
Any producer of that schema will do — a project that measures its own entry points points
`complexity.before` / `complexity.after` in human-review.json at its two files instead.

Usage:
    endpoint-complexity-delta.py before.json after.json [--out fragment.html] [--json]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path


# Snapshots taken before entry points other than HTTP were extracted carry no 'kind' at all.
DEFAULT_KIND = "http"
KIND_TITLES = [
    ("http", "HTTP / REST APIs"),
    ("mcp", "MCP tools"),
    ("listener", "Message listeners"),
    ("job", "Jobs"),
]


@lru_cache(maxsize=1)
def repo_root() -> Path:
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )


@lru_cache(maxsize=None)
def _source_of(fqcn: str):
    """`victor.training.petclinic.mcp.PetClinicMcp` -> the .java file that declares it."""
    rel = Path(*fqcn.split(".")).with_suffix(".java")
    for src in sorted(repo_root().glob("*/src/main/java")):
        candidate = src / rel
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def entry_source(flow_method: str):
    """Where a flow starts, as (absolute path, 1-based line) — or None if not resolvable.

    The JSON names the handler as `pkg.Class#method`. The line comes from the declaration in
    the file rather than from bytecode, so it stays right for any project layout and needs no
    debug symbols; an overload resolves to its first declaration, which is the file and method
    a reviewer wanted anyway.
    """
    if "#" not in flow_method:
        return None
    fqcn, method = flow_method.split("#", 1)
    path = _source_of(fqcn)
    if path is None:
        return None
    decl = re.compile(r"\b" + re.escape(method) + r"\s*\(")
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")) or "=" in stripped.split("(")[0]:
            continue
        if decl.search(line):
            return path, n
    return path, 1


def load(path: Path):
    return {
        (e.get("kind", DEFAULT_KIND), e["httpMethod"], e["path"]): e
        for e in json.loads(path.read_text(encoding="utf-8"))
    }


def compare(before, after):
    rows = []
    # Union, not `after` alone. An entry point this branch DELETED is absent from `after`, so
    # iterating `after` dropped it silently — while the legend above promises red means "removed".
    for key in sorted(set(before) | set(after)):
        cur = after.get(key)
        old = before.get(key)
        gone = cur is None
        if gone:
            cur = dict(old)
            cur["flowCc"] = 0
        was = old["flowCc"] if old else None
        rows.append(
            {
                "kind": key[0],
                "method": key[1],
                "path": key[2],
                "now": cur["flowCc"],
                "was": was,
                "gone": gone,
                "delta": cur["flowCc"] - was if was is not None else None,
                "handler": cur.get("handler", ""),
                "entry": (cur.get("flow") or [{}])[0].get("method", ""),
                "methods": cur.get("methods"),
                "why": [] if gone else breakdown(cur, old),
            }
        )
    rows.sort(key=lambda r: (-max(r["now"], r["was"] or 0), r["path"]))
    return rows


def breakdown(cur, old):
    """How the branch's number was arrived at: every increment, under the method it was
    counted in, in the order the flow walks them.

    A bar is an assertion about somebody's code, and the reviewer it is shown to is the
    person least able to check it. So the row opens onto its own arithmetic — the same
    increments the extractor summed, each still carrying the line it was read off.

    A line is marked `new` when the branch has one more of it than the merge-base did,
    matched on (method, construct, source text) rather than on line number: an increment
    pushed three lines down by an import is not new, and this is the cheapest comparison
    that knows the difference. A brand-new entry point marks nothing — the badge already
    says the whole flow is new, and painting every line green says it a second time.
    """
    seen = Counter()
    for f in (old or {}).get("flow") or []:
        for h in f.get("hits") or []:
            seen[(f.get("method", ""), h.get("why"), h.get("code"))] += 1
    groups = []
    for f in cur.get("flow") or []:
        hits = []
        for h in f.get("hits") or []:
            key = (f.get("method", ""), h.get("why"), h.get("code"))
            fresh = bool(old) and seen[key] <= 0
            if old and not fresh:
                seen[key] -= 1
            hits.append({**h, "new": fresh})
        if hits:
            groups.append({"method": f.get("method", ""),
                           "display": f.get("display") or f.get("method", ""),
                           "cognitive": f.get("cognitive"), "hits": hits})
    return groups


WHY_EMPTY = ("Nothing counted: every method behind this entry point is straight-line code. "
             "Cognitive complexity charges for branching, loops and boolean runs, and there "
             "are none here.")
WHY_TIP = "{why} — +{inc}{deep}. {file}:{line} — open in VS Code"


def _why_panel(r) -> str:
    """The fold under a row: one line of real source per increment, `[+N]` on the right.

    Not a highlighted snippet with a margin. The question the fold answers is "which
    lines did this number come from", and the answer is a list you can run your eye down
    and click; a rendered snippet per increment would be the same list, three times taller
    and with the evidence padded out by the code around it."""
    groups = r.get("why") or []
    if not groups:
        return f'<div class="cx-why"><p class="cx-why-none">{WHY_EMPTY}</p></div>'
    out = ['<div class="cx-why">']
    for g in groups:
        total = sum(h["inc"] for h in g["hits"])
        out.append(f'<div class="cx-why-m">{html.escape(g["display"])}'
                   f'<span class="cx-why-mn">{total}</span></div>')
        for h in g["hits"]:
            target = (repo_root() / h["file"]).resolve()
            deep = f' (1 + {h["inc"] - 1} nesting)' if h["inc"] > 1 else ""
            tip = WHY_TIP.format(why=h["why"], inc=h["inc"], deep=deep,
                                 file=h["file"], line=h["line"])
            if h.get("new"):
                tip += "\nNew on this branch."
            out.append(
                f'<a class="cx-why-line{" cx-why-new" if h.get("new") else ""}"'
                f' href="vscode://file/{target}:{h["line"]}:1"{_tip(tip)}>'
                f'<code>{html.escape(h["code"])}</code>'
                f'<span class="cx-why-inc">[+{h["inc"]}]</span></a>')
    out.append("</div>")
    return "".join(out)


def _path_cell(r) -> str:
    """The label, linked to the method the flow starts at, so a reviewer lands on the
    controller / tool / listener that owns it instead of going hunting.

    The path leads the hover, on its own line, because this column is the one that runs
    out of room: the longest route in a REST tree is usually the one a branch is about
    (`POST /api/owners/{ownerId}/pets/{petId}/visits` is this project's), and it is the
    only row wide enough to reach the column's edge. Ellipsised it now says it was cut;
    the hover is where the cut half comes back. Said once, on the whole cell, rather than
    as a second tooltip nested inside the first — two hover targets a pixel apart is how
    a reader learns to stop hovering.
    """
    label = f'<code class="cx-path">{html.escape(r["path"])}</code>'
    found = entry_source(r.get("entry", ""))
    if not found:
        return f'<span class="cx-cell"{_tip(r["path"])}>{label}</span>'
    path, line = found
    handler = (r.get("handler") or "").strip()
    tip = r["path"] + (f"\n{handler} — open in VS Code" if handler
                       else "\nOpen in VS Code")
    return (
        f'<a class="cx-link" href="vscode://file/{path}:{line}:1"'
        f'{_tip(tip)}>{label}</a>'
    )


# The bar is three facts drawn as two rectangles, and none of them is labelled: how big the
# flow was, how big it is now, and which part of it this branch is responsible for. A
# reviewer who hovers is asking exactly that, so each segment answers for itself — and says
# where the baseline came from, because "12 on main" is a number people reasonably suspect
# of being an estimate. It is not: the same extractor read it off the source at the
# merge-base. `{base}` is the real base branch, never the word "main" hardcoded — half the
# repositories this runs in do not have one.
TIP_BASELINE = ("{baseline} on {base} before this branch. Measured, not estimated — the "
                "same extractor read the merge-base's own source for this number.")
TIP_UP = "+{delta} added by this branch — {baseline} → {total}."
TIP_DOWN = "−{delta} removed by this branch — {baseline} → {total}."
TIP_BAR = ("Whole-flow complexity behind this entry point: {baseline} on {base} "
           "→ {total} on this branch.")
TIP_SAME = ("Unchanged at {total} — this branch did not touch this flow. Measured, not "
            "estimated: the same extractor reads the same number at the merge-base "
            "with {base}.")
TIP_NEW = "New on this branch — {total}, none of it inherited: there was no such entry point on {base}."
TIP_GONE = "Removed by this branch — {baseline} on {base}, gone here."


def _tip(attr: str) -> str:
    return f' data-tip="{html.escape(attr)}"'


def _row(cls, head: str, why: str) -> str:
    """A row, and — for every row that has a branch to explain — the fold under it.

    The fold is a real `<details>`: with the script below it opens on a click on the bar
    and nowhere else, and without the script it opens on a click anywhere on the row.
    Degrading to "the whole row is the handle" is the right way round — the reader still
    gets the breakdown, they just get it from a wider target."""
    if not why:
        return f'<div class="cx-row {cls}"><div class="cx-head">{head}</div></div>'
    return (f'<details class="cx-row {cls}"><summary class="cx-head">{head}</summary>'
            f"{why}</details>")


def render_row(r, peak, base) -> str:
    if r.get("gone"):
        # The branch deleted this entry point. Red for the whole width it used to occupy —
        # the same "red is what the branch removed" convention as everywhere else.
        was = r["was"] or 0
        gone_tip = TIP_GONE.format(baseline=was, base=base)
        # No fold: there is no branch code left to break down. A row whose flow the branch
        # deleted has nothing to open onto, and an empty fold under it would read as a bug.
        return _row(
            "cx-down cx-gone",
            f'<span class="cx-verb cx-{r["method"].lower()}">{html.escape(r["method"])}</span>'
            f'<s>{_path_cell(r)}</s>'
            f'<span class="cx-bar"{_tip(gone_tip)}>'
            f'<u style="width:{100.0 * was / peak:.1f}%"></u></span>'
            f'<span class="cx-badge">gone</span>'
            f'<span class="cx-n">0</span>',
            "",
        )
    if r["delta"] is None:
        badge, cls = "new", "cx-up"
    elif r["delta"] > 0:
        badge, cls = f'+{r["delta"]}', "cx-up"
    elif r["delta"] < 0:
        badge, cls = str(r["delta"]), "cx-down"
    else:
        badge, cls = "", "cx-same"
    # The delta rides on the *end* of the bar, so the eye reads "this much of this bar the
    # branch added" (green) or "this much it removed" (red), instead of subtracting two numbers.
    # A growth splits the current bar; a shrink hangs the lost part off its end; a brand-new
    # entry point is added whole.
    delta = r["delta"] or 0
    now_pct = 100.0 * r["now"] / peak
    if r["delta"] is None:
        kept_pct, delta_pct = 0, now_pct
    else:
        delta_pct = 100.0 * abs(delta) / peak
        kept_pct = now_pct - (delta_pct if delta > 0 else 0)
    # The baseline is `cx-n` minus `cx-badge` — the two numbers already on the row — so the
    # tooltip can never disagree with what the reader can see next to it.
    total, baseline = r["now"], r["was"]
    if r["delta"] is None:
        bar_tip = TIP_NEW.format(total=total, base=base)
        grey_tip, delta_tip = "", bar_tip
    elif delta:
        bar_tip = TIP_BAR.format(baseline=baseline, base=base, total=total)
        grey_tip = TIP_BASELINE.format(baseline=baseline, base=base)
        delta_tip = (TIP_UP if delta > 0 else TIP_DOWN).format(
            delta=abs(delta), baseline=baseline, total=total)
    else:
        # An unchanged row has nothing to attribute to anyone, so the segments say nothing
        # and the whole bar answers with the one fact there is.
        bar_tip = TIP_SAME.format(total=total, base=base)
        grey_tip = delta_tip = ""
    return _row(
        cls,
        f'<span class="cx-verb cx-{r["method"].lower()}">{html.escape(r["method"])}</span>'
        f"{_path_cell(r)}"
        f'<span class="cx-bar"{_tip(bar_tip)}>'
        f'<i{_tip(grey_tip) if grey_tip else ""} style="width:{kept_pct:.1f}%"></i>'
        f'<u{_tip(delta_tip) if delta_tip else ""} style="width:{delta_pct:.1f}%"></u></span>'
        f'<span class="cx-badge">{badge}</span>'
        f'<span class="cx-n">{r["now"]}</span>',
        _why_panel(r),
    )


def base_branch() -> str:
    """What to call the other side of the comparison, in the reader's own words.

    Hardcoding "main" is wrong in every repository that calls it something else, and a
    tooltip that names the wrong branch is worse than one that names none."""
    for cmd in (["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                ["config", "--get", "init.defaultBranch"]):
        out = subprocess.run(["git", *cmd], capture_output=True, text=True)
        name = out.stdout.strip()
        if out.returncode == 0 and name:
            return name.rpartition("/")[2]
    return "the base branch"


def render(rows, base="main") -> str:
    # A shrunk bar still draws what was removed past its current end, so the scale must fit
    # the taller of the two snapshots.
    peak = max((max(r["now"], r["was"] or 0) for r in rows), default=1) or 1
    out = [
        # One line, and only what the picture cannot say for itself. What counts as an
        # entry point, how the score is derived, and how many of them moved all used to
        # close this sentence; the groups below are titled by kind, every bar says on
        # hover what its colour and its number mean, and a reader counting moved rows is
        # reading the bars, not this line.
        '<p class="cx-lede">Cognitive complexity of the <em>whole flow</em> behind each entry '
        "point. <span class=\"cx-hint\">Click a bar to see the lines it is made of.</span></p>",
    ]
    known = {kind for kind, _ in KIND_TITLES}
    groups = KIND_TITLES + [
        (k, k) for k in dict.fromkeys(r["kind"] for r in rows) if k not in known
    ]
    for kind, title in groups:
        of_kind = [r for r in rows if r["kind"] == kind]
        if not of_kind:
            continue
        out.append('<div class="cx-group">')
        out.append(
            f'<div class="cx-kind">{html.escape(title)} <span class="cx-count">'
            f"{len(of_kind)}</span></div>"
        )
        out.append('<div class="cx-list">')
        out.extend(render_row(r, peak, base) for r in of_kind)
        out.append("</div></div>")
    out.append(TOGGLE_JS)
    return "\n".join(out)


# The bar is the handle, not the row. A `<summary>` is activated by a click anywhere in
# it, so without this the verb, the path link and the two numbers would all open the fold
# — and the path link would open it *instead of* opening the file in some browsers and *as
# well as* in others. Three lines of delegation settle it: the bar toggles, a link
# navigates and puts the fold back the way it found it, everything else does nothing.
#
# Inline in the fragment on purpose. The page pastes this HTML into a tab whole; a tab
# that needed a file from `hrbuild/assets` would be a tab that only works inside one
# builder. And with the script absent the `<details>` is still a `<details>`: the whole
# row becomes the handle and the breakdown still opens.
TOGGLE_JS = """<script>
(function () {
  document.addEventListener('click', function (e) {
    var head = e.target.closest && e.target.closest('summary.cx-head');
    if (!head) return;
    if (e.target.closest('.cx-bar')) return;            /* the handle: let it toggle */
    var row = head.parentNode;
    if (e.target.closest('a')) {                        /* a link opens the file, not the fold */
      var was = row.open;
      setTimeout(function () { row.open = was; }, 0);
      return;
    }
    e.preventDefault();                                 /* verb, badge, number: dead */
  });
})();
</script>"""


CSS = """
/* Green = complexity this branch ADDED, red = complexity it REMOVED: the colour names the
    author of the change, it is not a verdict on whether growing is bad. */
.cx-lede { color:var(--muted); font-size:.92rem; --cx-added:#2e9e5b; --cx-removed:#c62828; }
.cx-group { --cx-added:#2e9e5b; --cx-removed:#c62828; }
.cx-group + .cx-group { margin-top:1.1rem; }
.cx-kind { font:600 11px/1 system-ui,sans-serif; text-transform:uppercase; letter-spacing:.07em;
            color:var(--muted); margin:0 0 .35rem .15rem; }
.cx-count { opacity:.65; font-weight:400; }
.cx-key { font-weight:700; }
.cx-list { border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--card); }
/* The grid moved off the row and onto `.cx-head`, because the row now has a second thing
    in it: the fold, which must run the full width under the columns rather than sit in
    one of them. A `<details>` cannot be a grid whose first track holds the summary and
    whose second holds the panel, so the row stays a plain block and the columns live one
    level in. `list-style:none` + the WebKit marker rule take away the disclosure triangle
    — the bar is the affordance, and a triangle in the first column would be a second one
    pointing at nothing. */
.cx-row { border-bottom:1px solid var(--line); }
.cx-row:last-child { border-bottom:0; }
.cx-head { display:grid; grid-template-columns:3.6rem minmax(9rem,17rem) 1fr 2.6rem 2.2rem;
          align-items:center; gap:.55rem; padding:.3rem .8rem; font-size:.84rem;
          cursor:default; list-style:none; }
.cx-head::-webkit-details-marker { display:none; }
summary.cx-head:focus-visible { outline:2px solid var(--link); outline-offset:-2px; }
.cx-hint { opacity:.75; }
.cx-same { opacity:.5; }
/* An untouched row is dimmed because the branch has nothing to say about it — but the
    moment a reader opens one they are reading it on purpose, and code at half opacity is
    code nobody reads. Opening restores it, and tints the head so the row and its fold
    read as one block. */
.cx-row[open] { opacity:1; }
.cx-row[open] > .cx-head { background:var(--code-bg); }
.cx-verb { font:700 10.5px/1 ui-monospace,Menlo,monospace; letter-spacing:.03em; }
/* Amber is the one hue that never read on white either: #e08a00 is 2.7:1 there, well
    under the 4.5:1 a 10.5px bold monospace word needs. Darkened until it clears it,
    and still amber — it is what tells PUT from DELETE at a glance down the column. */
.cx-get{color:#2e7d32}.cx-post{color:#1565c0}.cx-put{color:#a35f00}.cx-delete{color:#c62828}.cx-any{color:var(--muted)}
.cx-patch{color:#8e44ad}.cx-mcp{color:#7c4dff}.cx-job{color:#a35f00}
.cx-kafka,.cx-rabbit,.cx-jms{color:#00838f}
a.cx-link, .cx-cell { text-decoration:none; display:block; overflow:hidden; }
a.cx-link:hover .cx-path { text-decoration:underline; }
/* `display:block` is what makes the ellipsis appear. `text-overflow` only applies to a
    block container, and <code> is inline — so the rule was there, doing nothing, while the
    link's own `overflow:hidden` chopped the path mid-token. `POST /api/owners/{ownerId}
    /pets/{petId}/vis` is a different endpoint from the one the row is about, and nothing
    on screen said it had been cut. Blockified, the code element takes the grid column's
    width and can end in a `…`; the full path is on the row's hover either way. */
.cx-path { display:block; font:12px/1.4 ui-monospace,Menlo,monospace; overflow:hidden;
           text-overflow:ellipsis; white-space:nowrap; }
.cx-bar { display:flex; height:9px; border-radius:5px; overflow:hidden; background:transparent; }
/* The bar opens the breakdown, so it wears a hand. It has to out-specify `tip.js`, which
    paints every `[data-tip]` outside a link or a summary with `cursor:help` — right for a
    mark that only explains itself, wrong for one you can click. Two classes beat its
    `[data-tip]:not(…):not(…)` chain. The grey `?` stays on the rows that open onto
    nothing: a deleted endpoint's bar really is only a tooltip. */
details.cx-row .cx-bar, details.cx-row .cx-bar i, details.cx-row .cx-bar u { cursor:pointer; }
/* A hit target 9px tall is a hit target people miss. The padding is invisible and the
    bar keeps its height; only the clickable box grows. */
details.cx-row .cx-bar { padding:7px 0; margin:-7px 0; box-sizing:content-box; }
.cx-bar i { background:#c9c9d4; border-radius:5px 0 0 5px; }
.cx-bar u { border-radius:0 5px 5px 0; }
.cx-up .cx-bar u { background:var(--cx-added); }
.cx-down .cx-bar u { background:var(--cx-removed); }
.cx-same .cx-bar i { border-radius:5px; }
.cx-badge { font:700 11px/1 ui-monospace,Menlo,monospace; text-align:right; color:var(--muted); }
.cx-up .cx-badge, .cx-up.cx-key { color:var(--cx-added); }
.cx-down .cx-badge, .cx-down.cx-key { color:var(--cx-removed); }
.cx-n { font:600 12px/1 ui-monospace,Menlo,monospace; text-align:right; }
/* ── the breakdown ──────────────────────────────────────────────────────────────────
    What the bar is made of, one counted construct per line: the source line it was read
    off, and what it cost, hard right. The number on a row is an assertion about somebody
    else's code; this is the working, and every line of it opens the file at that line.
    Indented past the verb column so the fold reads as belonging to the row above it. */
.cx-why { padding:.35rem .8rem .55rem 4.15rem; background:var(--code-bg);
          border-top:1px dashed var(--line); }
.cx-why-m { display:flex; align-items:baseline; gap:.4rem; margin:.35rem 0 .15rem;
            font:600 10.5px/1.6 ui-monospace,Menlo,monospace; letter-spacing:.02em;
            color:var(--muted); }
.cx-why-m:first-child { margin-top:0; }
.cx-why-mn { font-weight:700; opacity:.8; }
.cx-why-none { margin:0; font-size:.8rem; color:var(--muted); }
a.cx-why-line { display:flex; align-items:baseline; gap:.75rem; text-decoration:none;
                color:inherit; padding:1px 0; }
a.cx-why-line code { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis;
                     white-space:nowrap; font:12px/1.55 ui-monospace,Menlo,monospace; }
a.cx-why-line:hover code { text-decoration:underline; }
/* `[+1]` is a column, not a suffix: same width on every line, so the eye can add them up
    without reading them. `tabular-nums` keeps `[+10]` from widening the column by a hair. */
.cx-why-inc { flex:0 0 2.9rem; text-align:right; font-variant-numeric:tabular-nums;
              font:700 10.5px/1.55 ui-monospace,Menlo,monospace; color:var(--muted); }
/* Green is authorship everywhere else on this tab, and it means the same here: this line
    was not behind this entry point at the merge-base. */
a.cx-why-new code { color:var(--cx-added); }
a.cx-why-new .cx-why-inc { color:var(--cx-added); }
@media (prefers-color-scheme: dark) {
  .cx-lede, .cx-group { --cx-added:#4ec27f; --cx-removed:#ef6a6a; }
  .cx-bar i { background:#3d3d4a; }
  /* The verb is the only coloured *text* on the row, and the light palette was never
     re-themed for dark: #2e7d32 on --card is 3.3:1 and #1565c0 is 2.9:1, so forty-three
     chips on this tab read as grey smudges. Same hues, lifted until each clears the 4.5:1
     text minimum — and lifted onto the tokens the rest of the page already uses in dark
     (--link's blue, --accent's red, --drift's amber), so the row's verbs belong to the
     same palette as everything around them instead of being a second green and a second
     red invented here. */
  .cx-get{color:#8fd39c}.cx-post{color:#8ab4f8}.cx-put{color:#e0a33c}.cx-delete{color:#f08a8a}
  .cx-patch{color:#c9a0ff}.cx-mcp{color:#b39dff}.cx-job{color:#e0a33c}
  .cx-kafka,.cx-rabbit,.cx-jms{color:#4dd0e1}
}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("before", nargs="?")
    ap.add_argument("after", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true", help="emit the rows as JSON instead of HTML")
    ap.add_argument("--css", action="store_true", help="print the stylesheet this fragment needs")
    ap.add_argument("--base", help="name of the base branch, for the tooltips "
                                  "(default: whatever origin/HEAD points at)")
    args = ap.parse_args(argv)

    if args.css:
        print(CSS)
        return 0
    if not args.before or not args.after:
        ap.error("the following arguments are required: before, after")

    rows = compare(load(Path(args.before)), load(Path(args.after)))
    body = json.dumps(rows, indent=1) if args.json else render(rows, args.base or base_branch())
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"[complexity-delta] wrote {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
