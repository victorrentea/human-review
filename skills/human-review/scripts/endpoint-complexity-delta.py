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
    ("http", "REST APIs"),
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
                "graph": [] if gone else graph_nodes(cur, old),
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


def graph_nodes(cur, old):
    """The flow as call-graph nodes: who calls whom, what each one costs, and how much of
    that this branch put there.

    `delta` is the method's cognitive score now minus at the merge-base, *within this
    entry point's flow*: a method the base flow never reached brings its whole score with
    it. A brand-new entry point marks nothing, for the same reason `breakdown` marks no
    line of it — the row's badge already says all of it is new. Snapshots taken before
    the extractor wrote edges carry no `calls`, and draw no graph."""
    flow = cur.get("flow") or []
    if not flow or not any("calls" in f for f in flow):
        return []
    was = {f.get("method"): f.get("cognitive") or 0 for f in (old or {}).get("flow") or []}
    return [{"method": f.get("method", ""), "display": f.get("display") or f.get("method", ""),
             "cognitive": f.get("cognitive") or 0,
             "calls": f.get("calls") or [],
             "delta": ((f.get("cognitive") or 0) - was.get(f.get("method"), 0)) if old else 0}
            for f in flow]


# How much of the flow the graph draws before it starts folding. The biggest flow in
# petclinic reaches forty methods, thirty of them getters and setters: drawn whole, the
# graph is a wall of `getId` nobody reads. So a method that costs nothing and reaches
# nothing that costs is not drawn at all, and a node with more expensive children than
# this shows the costliest and folds the rest into "+N".
GRAPH_KIDS = 6


def _split(key: str) -> tuple[str, str]:
    """`pkg.Class#method` -> ("Class", "method")."""
    owner, _, name = key.partition("#")
    return owner.rsplit(".", 1)[-1], name


SONAR_COGNITIVE = "https://www.sonarsource.com/resources/cognitive-complexity/"


def _graph(nodes, groups=()) -> tuple[str, set[str]]:
    """The flow behind a row, drawn left to right as the tree the extractor walked it in,
    and every method that is drawn carries the lines it was charged for.

    Each method appears once, under whoever reached it first — the same breadth-first
    order the score is summed in, so the graph and the number can never disagree about
    what is in the flow. A node is the class above `method()` below, its cognitive score
    beside them, and two handles: ↗ opens the method in the editor, a click anywhere else
    on the box folds open the lines that make up its score. That used to be a second list
    under the graph, the same methods again in another order — the reader had to match a
    heading down there to a box up here. Now the detail is where the box is.

    One score, not two. Cyclomatic sat under the cognitive number and the reader could not
    tell which one the bar summed; only cognitive is summed, so only cognitive is shown.

    Returns the HTML and the methods it drew, so the caller can still offer the lines of
    the ones folded into a `+N` chip."""
    if not nodes:
        return "", set()
    lines_of = {g["method"]: g for g in groups}
    by = {n["method"]: n for n in nodes}
    root = nodes[0]["method"]
    kids: dict[str, list[str]] = {}
    seen = {root}
    for n in nodes:  # breadth-first: `nodes` is already in the order the flow was walked
        for t in n["calls"]:
            if t in by and t not in seen:
                seen.add(t)
                kids.setdefault(n["method"], []).append(t)
    weight: dict[str, int] = {}
    drawn: set[str] = set()

    def w(k):
        if k not in weight:
            weight[k] = 0  # a cycle is cut by `seen` above, but stay safe
            weight[k] = (by[k]["cognitive"] + abs(by[k]["delta"])
                         + sum(w(c) for c in kids.get(k, [])))
        return weight[k]

    def tree(k) -> str:
        drawn.add(k)
        children = kids.get(k, [])
        heavy = [c for c in children if w(c) > 0]
        shown = sorted(heavy, key=lambda c: -w(c))[:GRAPH_KIDS]
        shown = [c for c in heavy if c in shown]  # keep the call order among those shown
        rest = [c for c in heavy if c not in shown]
        parts = [tree(c) for c in shown]
        if rest:
            names = "\n".join(by[c]["display"] for c in rest)
            parts.append(f'<div class="cg-t"><span class="cg-more"'
                         f'{_tip("Also called, and folded to keep this readable:" + chr(10) + names)}>'
                         f'+{len(rest)}</span></div>')
        sub = f'<div class="cg-kids">{"".join(parts)}</div>' if parts else ""
        node = _node(by[k], lines_of.get(k))
        return f'<div class="cg-t">{node}{sub}</div>'

    body = tree(root)
    return (f'<div class="cg" role="group" aria-label="Call graph of this entry point">'
            # No key line over the graph: repeated under every open row it was the same
            # sentence N times, and what it explained the boxes show on hover. Its one
            # link — what "cognitive complexity" means — moved to the tab's lede.
            f'{body}</div>'), drawn


def _lines(hits) -> str:
    """One line of real source per increment, `[+N]` hard right, each a link to that line."""
    out = []
    for h in hits:
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
    return "".join(out)


def _node(n, group=None) -> str:
    """A method as a box. The box itself is a toggle, not a link: a click selects it and,
    when the method was charged for anything, folds its lines open inside it. Navigation
    is the ↗ alone — a box that sometimes opened the editor and sometimes did nothing was
    a box nobody dared click."""
    cls, name = _split(n["method"])
    cog, d = n["cognitive"], n["delta"]
    mark = " cg-add" if d > 0 else " cg-cut" if d < 0 else ""
    zero = " cg-zero" if not cog and not d else ""
    hits = (group or {}).get("hits") or []
    tip = (f'{n["display"]}\ncognitive complexity {cog}'
           + (f"\n+{d} added by this branch" if d > 0 else "")
           + (f"\n−{-d} removed by this branch" if d < 0 else ""))
    tip += (f"\nClick for the {len(hits)} line{'s' if len(hits) > 1 else ''} it is charged for"
            if hits else "")
    found = entry_source(n["method"])
    go = (f'<a class="cg-go" href="vscode://file/{found[0]}:{found[1]}:1"'
          f'{_tip("Open " + n["display"] + " in VS Code")} aria-label="Open in VS Code">↗</a>'
          if found else "")
    tog = '<span class="cg-tog" aria-hidden="true"></span>' if hits else ""
    delta = (f'<b class="cg-d">+{d}</b>' if d > 0 else
             f'<b class="cg-d">−{-d}</b>' if d < 0 else "")
    lines = f'<div class="cg-lines">{_lines(hits)}</div>' if hits else ""
    has = " cg-has" if hits else ""
    return (f'<div class="cg-n{mark}{zero}{has}" tabindex="0" role="button"'
            f' aria-expanded="false"{_tip(tip)}>'
            f'<span class="cg-c">{html.escape(cls)}</span>'
            f'<span class="cg-cog">{cog}</span>'
            f'<span class="cg-m">{tog}{html.escape(name)}()</span>'
            f'<span class="cg-tools">{go}</span>{delta}{lines}</div>')


WHY_EMPTY = ("Nothing counted: every method behind this entry point is straight-line code. "
             "Cognitive complexity charges for branching, loops and boolean runs, and there "
             "are none here.")
WHY_TIP = "{why} — +{inc}{deep}. {file}:{line} — open in VS Code"


def _why_panel(r) -> str:
    """The fold under a row: the call graph, whose boxes open onto their own lines.

    Not a highlighted snippet with a margin. The question the fold answers is "which
    lines did this number come from", and the answer is a list you can run your eye down
    and click — kept inside the box of the method it belongs to. Only lines no drawn box
    can hold (a method folded into `+N`) are listed under the graph; a snapshot with no
    edges draws no graph, and then the whole list is all there is."""
    groups = r.get("why") or []
    graph, drawn = _graph(r.get("graph") or [], groups)
    if not groups:
        return f'<div class="cx-why">{graph}<p class="cx-why-none">{WHY_EMPTY}</p></div>'
    rest = [g for g in groups if g["method"] not in drawn]
    out = ['<div class="cx-why">', graph]
    if graph and rest:
        n = sum(len(g["hits"]) for g in rest)
        out.append(f'<details class="cx-why-rest"><summary>{n} more line'
                   f'{"s" if n > 1 else ""} in the methods folded into <b>+N</b></summary>')
    for g in rest:
        total = sum(h["inc"] for h in g["hits"])
        out.append(f'<div class="cx-why-m">{html.escape(g["display"])}'
                   f'<span class="cx-why-mn">{total}</span></div>')
        out.append(_lines(g["hits"]))
    if graph and rest:
        out.append("</details>")
    out.append("</div>")
    return "".join(out)


def _path_cell(r) -> str:
    """The label, and beside it a ↗ into the method the flow starts at, so a reviewer
    lands on the controller / tool / listener that owns it instead of going hunting.

    The label itself is not a link. It is the word a reader clicks to see what is behind
    the row, and a path that sometimes opened the editor and sometimes the fold was a
    label nobody dared click — the same bargain the boxes of the call graph strike, with
    the same ↗, so there is one way into the editor on this tab and it looks the same
    everywhere.

    The path leads the hover, on its own line, because this column is the one that runs
    out of room: the longest route in a REST tree is usually the one a branch is about
    (`POST /api/owners/{ownerId}/pets/{petId}/visits` is this project's), and it is the
    only row wide enough to reach the column's edge. Ellipsised it now says it was cut;
    the hover is where the cut half comes back. Said once, on the whole cell, rather than
    as a second tooltip nested inside the first — two hover targets a pixel apart is how
    a reader learns to stop hovering.
    """
    label = f'<code class="cx-path">{html.escape(r["path"])}</code>'
    handler = (r.get("handler") or "").strip()
    found = entry_source(r.get("entry", ""))
    go = ""
    if found:
        path, line = found
        go = (f'<a class="cg-go" href="vscode://file/{path}:{line}:1"'
              f'{_tip("Open " + (handler or r["path"]) + " in VS Code")}'
              f' aria-label="Open in VS Code">↗</a>')
    tip = r["path"] + (f"\n{handler}" if handler else "")
    return f'<span class="cx-cell"{_tip(tip)}>{label}{go}</span>'


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


def _row(cls, head: str, why: str, key: str = "") -> str:
    """A row, and — for every row that has a branch to explain — the fold under it.

    The fold is a real `<details>`, opened by a click anywhere on its head but the ↗.
    `key` is the row's name in the address bar (`GET /api/owners`): the script below
    lists every open row there, so a reload — or a colleague handed the link — opens the
    same rows again."""
    if not why:
        return (f'<div class="cx-row {cls}"><div class="cx-head">'
                f'<span class="cx-caret"></span>{head}</div></div>')
    named = f' data-cx="{html.escape(key)}"' if key else ""
    return (f'<details class="cx-row {cls}"{named}><summary class="cx-head">'
            f'<span class="cx-caret" aria-hidden="true"></span>{head}</summary>'
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
        f'{r["method"]} {r["path"]}',
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
        f'<p class="cx-lede"><a href="{SONAR_COGNITIVE}" target="_blank" rel="noopener">'
        'Cognitive complexity</a> of the <em>whole flow</em> behind each entry point.</p>',
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


# The whole head is the handle: verb, path, bar and numbers all open the fold. The one
# link on it — the ↗ beside the path — opens the file and puts the fold back the way it
# found it: a `<summary>` is activated by a click anywhere in it, and a link inside one
# would open the fold *instead of* the file in some browsers and *as well as* in others.
#
# Which rows are open is in the address bar, as `?cx=GET+/api/owners&cx=…` — one `cx` per
# open row. A query parameter, not the hash: the hash is the tab strip's (`#complexity`),
# and every tab click rewrites it whole, so anything riding in it would be wiped the moment
# the reader looked at another tab and back. `replaceState`, like every other script on
# the page that writes the address, because assigning it scrolls the page or reloads it.
#
# Inline in the fragment on purpose. The page pastes this HTML into a tab whole; a tab
# that needed a file from `hrbuild/assets` would be a tab that only works inside one
# builder. And with the script absent the `<details>` is still a `<details>`: the row
# still opens, it just forgets it was open.
TOGGLE_JS = """<script>
(function () {
  document.addEventListener('click', function (e) {
    var head = e.target.closest && e.target.closest('summary.cx-head');
    if (!head || !e.target.closest('a')) return;        /* anywhere else: let it toggle */
    var row = head.parentNode, was = row.open;          /* the ↗ opens the file, not the fold */
    setTimeout(function () { row.open = was; }, 0);
  });
  var P = 'cx';
  var rows = document.querySelectorAll('details.cx-row[data-cx]');
  function remember() {
    if (!history.replaceState || !window.URLSearchParams) return;
    var q = new URLSearchParams(location.search);
    q.delete(P);
    Array.prototype.forEach.call(rows, function (r) {
      if (r.open) q.append(P, r.getAttribute('data-cx'));
    });
    var s = q.toString().replace(/%2F/gi, '/');         /* a path reads as a path */
    history.replaceState(history.state, '', location.pathname + (s ? '?' + s : '') + location.hash);
  }
  var wanted = window.URLSearchParams ? new URLSearchParams(location.search).getAll(P) : [];
  Array.prototype.forEach.call(rows, function (r) {
    if (wanted.indexOf(r.getAttribute('data-cx')) >= 0) r.open = true;
    r.addEventListener('toggle', remember);
  });
  /* A box in the call graph is a toggle: it lights up and folds its own lines open. The
     ↗ and every source line inside it are links and keep doing what links do. */
  function flip(n) {
    var on = !n.classList.contains('cg-open');
    n.classList.toggle('cg-open', on);
    n.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  document.addEventListener('click', function (e) {
    var n = e.target.closest && e.target.closest('.cg-n');
    if (!n || e.target.closest('a') || e.target.closest('.cg-lines')) return;
    flip(n);
  });
  document.addEventListener('keydown', function (e) {
    var n = e.target;
    if (!n.classList || !n.classList.contains('cg-n')) return;
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    flip(n);
  });
})();
</script>"""


CSS = """
/* Green = complexity this branch ADDED, red = complexity it REMOVED: the colour names the
    author of the change, it is not a verdict on whether growing is bad. */
.cx-lede a { color:var(--link); }
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
/* The verb column is as wide as DELETE and no wider, and the gap after it is the smaller
    one: at 3.6rem + .55rem a `GET` sat 46px from its own path and read as two columns of
    unrelated things. The caret column in front is the fold's second handle, next to the
    word a reader looks at first. */
.cx-head { display:grid; grid-template-columns:1.1rem 2.45rem minmax(9rem,17rem) 1fr 2.6rem 2.2rem;
          align-items:center; gap:.4rem; padding:.3rem .8rem .3rem .5rem; font-size:.84rem;
          cursor:default; list-style:none; }
/* The full-size triangles (U+25B6/U+25BC, forced to text with U+FE0E so macOS does not
    swap in the emoji), not the small ▸/▾: those stay a speck at any font size — at 10px
    beside a 13px verb the caret read as punctuation, not as the handle that opens the
    call graph. */
.cx-caret { font-size:13px; line-height:1; color:var(--muted); text-align:center; }
details.cx-row > summary .cx-caret { cursor:pointer; }
details.cx-row > summary .cx-caret::before { content:"\\25B6\\FE0E"; }
details.cx-row[open] > summary .cx-caret::before { content:"\\25BC\\FE0E"; }
.cx-head::-webkit-details-marker { display:none; }
summary.cx-head:focus-visible { outline:2px solid var(--link); outline-offset:-2px; }
.cx-same { opacity:.5; }
/* An untouched row is dimmed because the branch has nothing to say about it — but the
    moment a reader opens one they are reading it on purpose, and code at half opacity is
    code nobody reads. Opening restores it, and tints the head so the row and its fold
    read as one block. */
.cx-row[open] { opacity:1; }
.cx-row[open] > .cx-head { background:var(--code-bg); }
/* A row you can open has to look like one before you try: under the pointer its head
    lights up, a bar in the link colour marks its left edge, the caret takes the same
    colour and nudges, and a dimmed row comes back to full strength — together they say
    "click me", where a faint tint alone read as nothing at all. */
details.cx-row > summary.cx-head { cursor:pointer; transition:background .12s, box-shadow .12s; }
details.cx-row > summary.cx-head:hover { background:var(--code-bg);
    box-shadow:inset 3px 0 0 var(--link); }
details.cx-row > summary.cx-head:hover .cx-caret { color:var(--link); }
details.cx-row:not([open]) > summary.cx-head:hover .cx-caret::before { display:inline-block;
    transform:translateX(2px); }
details.cx-row.cx-same:hover { opacity:.9; }
.cx-verb { font:700 10.5px/1 ui-monospace,Menlo,monospace; letter-spacing:.03em; }
/* Amber is the one hue that never read on white either: #e08a00 is 2.7:1 there, well
    under the 4.5:1 a 10.5px bold monospace word needs. Darkened until it clears it,
    and still amber — it is what tells PUT from DELETE at a glance down the column. */
.cx-get{color:#2e7d32}.cx-post{color:#1565c0}.cx-put{color:#a35f00}.cx-delete{color:#c62828}.cx-any{color:var(--muted)}
.cx-patch{color:#8e44ad}.cx-mcp{color:#7c4dff}.cx-job{color:#a35f00}
.cx-kafka,.cx-rabbit,.cx-jms{color:#00838f}
/* The path, then its ↗ right after it — not at the column's far edge, where it would
    belong to the bar as much as to the path. The path gives way first: it shrinks to its
    ellipsis and the ↗ stays whole. */
.cx-cell { display:flex; align-items:center; gap:3px; min-width:0; overflow:hidden; }
.cx-cell .cx-path { flex:0 1 auto; min-width:0; }
.cx-cell a.cg-go { flex:none; }
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
/* ── the call graph ─────────────────────────────────────────────────────────────────
    The flow drawn left to right: a node per method, its callees in a column to its right,
    joined by elbow lines drawn from borders alone (no SVG, no layout engine), each ending
    in an arrowhead on the callee. A node is two badges — the class, and `method()` under
    it — with its cognitive score beside them. The graph scrolls sideways inside its own
    box rather than squeeze a deep chain or push the page wider than the window. The top
    padding leaves room for the `+N` badge that rides above a node's corner. */
.cg { overflow-x:auto; padding:.55rem 0 .45rem; margin:0 0 .35rem -3.3rem; }
/* Top-aligned, not centred: a centred parent floats to the middle of however tall its
    subtree is, and one wide branch then opens a screen of empty space above and below
    every sibling. Aligned to the top, a node sits level with its first callee and the
    elbows are drawn at a fixed height — half a node — from the top of each row. */
.cg-t { --cg-mid:15px; display:flex; align-items:flex-start; position:relative; }
.cg-kids { display:flex; flex-direction:column; gap:3px; margin-left:12px; position:relative; }
.cg-kids::before { content:""; position:absolute; left:-12px; top:var(--cg-mid); width:12px;
                   border-top:1px solid var(--cg-line); }
.cg-kids > .cg-t { padding-left:13px; }
.cg-kids > .cg-t::before { content:""; position:absolute; left:0; top:-3px; bottom:0;
                           border-left:1px solid var(--cg-line); }
.cg-kids > .cg-t:first-child::before { top:var(--cg-mid); }
.cg-kids > .cg-t:last-child::before { bottom:auto; height:calc(var(--cg-mid) + 3px); }
.cg-kids > .cg-t:only-child::before { display:none; }
.cg-kids > .cg-t::after { content:""; position:absolute; left:0; top:var(--cg-mid); width:8px;
                          border-top:1px solid var(--cg-line); }
/* The arrowhead: a border triangle hung off the callee's left edge, tip on its frame, so
    every edge reads caller → callee without an SVG. */
.cg-kids > .cg-t > .cg-n::before, .cg-kids > .cg-t > .cg-more::before {
    content:""; position:absolute; left:-7px; top:calc(var(--cg-mid) - 5px);
    border:4px solid transparent; border-left:6px solid var(--cg-arrow); border-right:0; }
.cg-more { position:relative; }
.cg { --cg-line:color-mix(in srgb, var(--muted) 55%, transparent); --cg-arrow:var(--muted); }
/* A node: the class badge over the `method()` badge, the ↗ to the right of the class and
    the score to the right of the method — the score is the method's, so it sits on its row. The names are set in the UI face, not monospace: the
    graph is as wide as its deepest chain times its widest names, and a proportional face
    buys back a fifth of that. */
.cg-n { display:inline-grid; grid-template-columns:auto auto; column-gap:5px; row-gap:1px;
        align-items:center; padding:2px 4px; border:1px solid var(--line); border-radius:6px;
        background:var(--card); color:inherit; white-space:nowrap; cursor:pointer;
        position:relative; z-index:1; transition:border-color .12s, background .12s; }
.cg-n:hover { border-color:var(--link); }
.cg-n:focus-visible { outline:2px solid var(--link); outline-offset:1px; }
/* Clicked: the box takes the link blue — frame, ring and a tint — so the one being read
    stands out of the graph around it. */
.cg-n.cg-open { border-color:var(--link); box-shadow:0 0 0 1px var(--link) inset;
                background:color-mix(in srgb, var(--link) 12%, var(--card)); opacity:1; }
.cg-c, .cg-m { font:600 10.5px/1.3 system-ui,sans-serif; padding:0 4px; border-radius:4px;
               grid-column:1; justify-self:start; }
.cg-c { color:var(--muted); border:1px solid var(--line); font-weight:500; }
.cg-m { background:var(--code-bg); border:1px solid transparent; }
.cg-cog { grid-column:2; grid-row:2; font:700 9.5px/1.3 ui-monospace,Menlo,monospace;
          text-align:right; font-variant-numeric:tabular-nums; }
.cg-tools { grid-column:2; grid-row:1; justify-self:end; }
/* ↗ is the only way into the editor from the graph, so it is a real target, not a glyph:
    a small square that fills blue under the pointer. */
a.cg-go { display:inline-block; min-width:14px; text-align:center; border-radius:3px;
          font:700 11px/14px system-ui,sans-serif; color:var(--link); text-decoration:none; }
a.cg-go:hover { background:var(--link); color:var(--card); }
/* The fold's caret sits in front of the method name, the word a reader clicks on. */
.cg-tog::before { content:"\\25B8"; display:inline-block; width:.8em; color:var(--muted);
                  transition:transform .12s; }
.cg-n:hover .cg-tog::before { color:var(--link); }
.cg-open .cg-tog::before { transform:rotate(90deg); color:var(--link); }
.cg-lines { display:none; grid-column:1 / 3; margin:3px 0 1px; padding:3px 2px 0;
            border-top:1px dashed var(--line); cursor:auto; }
.cg-open > .cg-lines { display:block; }
.cg-lines a.cx-why-line code { font-size:11px; }
.cg-d { font:700 9.5px/1 ui-monospace,Menlo,monospace; position:absolute; top:-6px; right:-6px;
        padding:1px 3px; border-radius:6px; background:var(--card); }
.cg-zero { opacity:.55; }
.cg-more { font:700 10px/1 ui-monospace,Menlo,monospace; color:var(--muted); padding:3px 6px;
           border:1px dashed var(--line); border-radius:6px; cursor:help; }
/* A method whose score this branch raised wears the added colour on its frame and its
    number; one it lowered, the removed colour. */
.cg-add { border-color:var(--cx-added); box-shadow:0 0 0 1px var(--cx-added) inset; }
.cg-add .cg-d, .cg-add .cg-cog { color:var(--cx-added); }
.cg-cut { border-color:var(--cx-removed); }
.cg-cut .cg-d { color:var(--cx-removed); }
/* Lines of methods folded into `+N`: one closed fold under the graph, never lost. */
.cx-why-rest > summary { cursor:pointer; font-size:.78rem; color:var(--muted); margin:.1rem 0 .3rem; }
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
