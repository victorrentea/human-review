#!/usr/bin/env python3
"""What the change cost in entry-point complexity, ranked against the whole app.

`endpoint-complexity.py` answers "how complex is each entry point's whole flow right now"
— REST endpoints, MCP tools, message listeners and jobs alike. A reviewer needs the
derivative of that: which entry points this branch made heavier, by how much, and whether
that lands on an already-expensive one or turns a cheap one into a hot spot. So this diffs
two of its JSON snapshots and renders the full ranked list, grouped by kind — touched rows
called out, untouched rows kept for scale, because "+3" only means something next to the
numbers it is standing among.

Colour reads as authorship, not as judgement: green is what the branch ADDED; what it
REMOVED is a neutral slate ghost of the old length, badged "simpler" in words. It used to be
red, and eval run 10's judges read a shorter flow drawn in alarm red as an alarm.

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
    ("invoke", "Invoked by other services"),
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
    """`src.core.services.rpa.RpaService` (or the module itself) -> the .py file that
    declares it: the longest prefix of the dotted name that is a module of the repository,
    a package's `__init__.py` included. Only a fallback — `endpoint-complexity.py` writes
    `file` and `line` on every flow item, which `DECL_AT` reads first."""
    parts = fqcn.split(".")
    while parts:
        for candidate in (repo_root().joinpath(*parts).with_suffix(".py"),
                          repo_root().joinpath(*parts, "__init__.py")):
            if candidate.is_file():
                return candidate
        parts = parts[:-1]
    return None


# Where each flow method is declared, when the snapshot says so (the JavaParser engine
# writes `file` and `line` on every flow item): exact for overloads and nested classes,
# where a search of the file by name would land on the first method of that name.
DECL_AT: dict[str, tuple[str, int]] = {}


@lru_cache(maxsize=None)
def entry_source(flow_method: str):
    """Where a flow starts, as (absolute path, 1-based line) — or None if not resolvable.

    The JSON names a method `module.Class#method` and a function `module#function`. The line comes from the declaration in
    the file rather than from bytecode, so it stays right for any project layout and needs no
    debug symbols; an overload resolves to its first declaration, which is the file and method
    a reviewer wanted anyway.
    """
    if "#" not in flow_method:
        return None
    if flow_method in DECL_AT:
        rel, line = DECL_AT[flow_method]
        path = repo_root() / rel
        if path.is_file():
            return path, line
    fqcn, method = flow_method.split("#", 1)
    method = method.split("(", 1)[0].split("#", 1)[0]  # `render(int)` -> `render`
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


def class_source(member: str):
    """Where the class of a flow method is declared, as (absolute path, 1-based line) — or
    None if not resolvable. Looked up in the file `entry_source` found the method in, so a
    nested class lands on its own line and every project layout that resolves a method
    resolves its class."""
    found = entry_source(member)
    if found is None:
        return None
    name = member.partition("#")[0].rsplit(".", 1)[-1]
    decl = re.compile(r"\b(?:class|interface|record|enum)\s+" + re.escape(name) + r"\b")
    for n, line in enumerate(found[0].read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip().startswith(("//", "*", "/*")) and decl.search(line):
            return found[0], n
    return found[0], 1


def load(path: Path):
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        for f in e.get("flow") or []:
            if f.get("file") and f.get("line"):
                # Later snapshots win: `main` loads the merge-base first and the branch
                # second, and a link must open the line where the method is *now*.
                DECL_AT[f["method"]] = (f["file"], f["line"])
    return {(e.get("kind", DEFAULT_KIND), e["httpMethod"], e["path"]): e for e in entries}


def load_pair(before: Path, after: Path):
    """Both snapshots, keyed so that two entry points sharing a name stay two rows.

    Rows are keyed by kind, verb and path, and two `@KafkaListener(topics = "orders")` share
    all three: keyed that way one of them silently vanished from the tab. A name that is
    ambiguous in *either* snapshot is told apart by its handler in *both* — deciding per
    snapshot turned the untouched listener into "gone" plus "new" the day a second one
    appeared on its topic."""
    lists = [json.loads(p.read_text(encoding="utf-8")) for p in (before, after)]
    for entries in lists:
        for e in entries:
            for f in e.get("flow") or []:
                if f.get("file") and f.get("line"):
                    DECL_AT[f["method"]] = (f["file"], f["line"])
    base = lambda e: (e.get("kind", DEFAULT_KIND), e["httpMethod"], e["path"])  # noqa: E731
    ambiguous = {k for entries in lists for k, n in Counter(map(base, entries)).items() if n > 1}
    out = []
    for entries in lists:
        keyed = {}
        for e in entries:
            k = base(e)
            if k in ambiguous:
                e = {**e, "path": f'{e["path"]} · {(e.get("handler") or "").split("(")[0]}'}
                k = base(e)
            keyed[k] = e
        out.append(keyed)
    return out[0], out[1]


def engine_of(path: Path) -> str:
    """Which extractor wrote a snapshot: `javaparser` or `regex` (older snapshots: regex)."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return (entries[0].get("engine") if entries else None) or "ast"


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
                "unresolved": [] if gone else (cur.get("unresolved") or []),
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
# nothing that costs is not drawn at all, and a method with more expensive callees than
# this shows the costliest and folds the rest into "+N" — never one that leads to what the
# branch changed, which is the one path the reader opened the row for.
GRAPH_KIDS = 6


def _split(key: str) -> tuple[str, str]:
    """`pkg.Class#method` -> ("Class", "method")."""
    owner, _, name = key.partition("#")
    return owner.rsplit(".", 1)[-1], name


SONAR_COGNITIVE = "https://www.sonarsource.com/resources/cognitive-complexity/"
# The extractor both snapshots come from, on GitHub: the repository is public, and a
# reader forwarded the page has no checkout of this skill to open it in.
EXTRACTOR_URL = ("https://github.com/stojanpeshov/human-review/blob/ludvig/"
                 "skills/human-review/scripts/endpoint-complexity.py")


def _graph(nodes, groups=()) -> tuple[str, set[str]]:
    """The flow behind a row: one faded box per class, its methods as solid blocks inside,
    and an arrow from every caller to every callee it reached first.

    A box used to be a method, with its class written on top — so a mapper whose three
    methods were all in the flow appeared three times, and the reader counted classes
    that were not there (Victor, 20 Oct 2026: "boxes mean classes, not methods — why does
    PetMapper appear three times?"). Now the class is the container, named once, and the
    method is the thing that stands out: the box is a faint neutral tint, the methods are
    cards. Every box wears the same tint: a hue per class said nothing the name above it
    did not, and made the page a rainbow (Victor, 4 Nov 2026). A call inside the class is an
    arrow between two blocks of the same box; a call into another class crosses to that
    class's box.

    Both names are the way into the editor, the class's and the method's alike, and both
    underline under the pointer (Victor, 4 Nov 2026, on the ↗ they used to wear: "no longer
    needed").

    Each method appears once, under whoever reached it first — the same breadth-first
    order the score is summed in, so the graph and the number can never disagree about
    what is in the flow. That is also what keeps recursion finite: a call back into a
    method already drawn adds no edge, and the `+1` the extractor charges for it is one of
    the lines inside the block.

    The classes are laid out in columns, left to right, by how many class boundaries the
    flow crosses to reach them (the longest such path, so a callee is right of every
    caller); inside a box the methods keep the order the flow met them. The page's script
    then slides each box down level with its first caller and draws the arrows over the
    columns (`data-edges`, `caller>callee`, `*` on an edge that leads to what the branch
    changed). Without the script the boxes still stand, they are only unjoined.

    A method the branch changed opens with its lines already folded out, so the `+1` on
    the row is answered without a click (Victor, 20 Oct 2026: "I should see it directly,
    without having to expand anything myself"); a click folds it shut like any other.

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
    hot: dict[str, bool] = {}

    def w(k):
        if k not in weight:
            weight[k] = 0  # a cycle is cut by `seen` above, but stay safe
            weight[k] = (by[k]["cognitive"] + abs(by[k]["delta"])
                         + sum(w(c) for c in kids.get(k, [])))
        return weight[k]

    def leads(k):
        """Whether this method, or anything drawn under it, is what the branch changed."""
        if k not in hot:
            hot[k] = False
            hot[k] = bool(by[k]["delta"]) or any(leads(c) for c in kids.get(k, []))
        return hot[k]

    order: list[str] = []          # every drawn method, depth first: the order in a box
    edges: list[tuple[str, str]] = []
    folded: dict[str, list[str]] = {}

    def walk(k):
        order.append(k)
        heavy = [c for c in kids.get(k, []) if w(c) > 0]
        shown = set(sorted(heavy, key=lambda c: (not leads(c), -w(c)))[:GRAPH_KIDS])
        if len(heavy) > len(shown):
            folded[k] = [c for c in heavy if c not in shown]
        for c in heavy:
            if c in shown:
                edges.append((k, c))
                walk(c)

    walk(root)
    owner = lambda k: k.partition("#")[0]  # noqa: E731
    classes = list(dict.fromkeys(owner(k) for k in order))
    # The longest run of class boundaries from the entry point to each class, capped at
    # the number of classes so a call back into an earlier class cannot loop forever.
    col = {c: 0 for c in classes}
    cross = [(owner(a), owner(b)) for a, b in edges if owner(a) != owner(b)]
    for _ in classes:
        moved = False
        for a, b in cross:
            if b != classes[0] and col[a] + 1 > col[b] and col[a] + 1 < len(classes):
                col[b], moved = col[a] + 1, True
        if not moved:
            break
    ranks = sorted(set(col.values()))
    columns: list[list[str]] = [[c for c in classes if col[c] == r] for r in ranks]
    idx = {k: i for i, k in enumerate(order)}

    def box(c) -> str:
        name = c.rsplit(".", 1)[-1]
        blocks = "".join(_node(by[k], lines_of.get(k), idx[k], folded.get(k), by)
                         for k in order if owner(k) == c)
        found = class_source(next(k for k in order if owner(k) == c))
        label = (f'<a class="cg-c" href="vscode://file/{found[0]}:{found[1]}:1"{_tip(c)}>'
                 f'{html.escape(name)}</a>' if found else
                 f'<span class="cg-c"{_tip(c)}>{html.escape(name)}</span>')
        return (f'<div class="cg-k" data-k="{html.escape(name)}">'
                f'<div class="cg-kh">{label}</div><div class="cg-ms">{blocks}</div></div>')

    cols = "".join(f'<div class="cg-col">{"".join(box(c) for c in column)}</div>'
                   for column in columns)
    data = " ".join(f'{idx[a]}>{idx[b]}{"*" if leads(b) else ""}' for a, b in edges)
    return (f'<div class="cg" role="group" aria-label="Call graph of this entry point"'
            f' data-edges="{data}"><div class="cg-cols">'
            # The arrows: drawn by the page's script once the boxes have a size.
            f'<svg class="cg-svg" width="0" height="0" aria-hidden="true"></svg>{cols}</div></div>'), set(order)


def _lines(hits) -> str:
    """One line of real source per increment, `+N` hard right, each a link to that line."""
    out = []
    for h in hits:
        target = (repo_root() / h["file"]).resolve()
        deep = f' (1 + {h["inc"] - 1} nesting)' if h["inc"] > 1 else ""
        # The face shows the line and its `+N`; the green row marks it new. The hover
        # adds the file and line it opens, and the nesting that made a `+2`.
        tip = WHY_TIP.format(name=Path(h["file"]).name, line=h["line"], deep=deep)
        out.append(
            f'<a class="cx-why-line{" cx-why-new" if h.get("new") else ""}"'
            f' href="vscode://file/{target}:{h["line"]}:1"{_tip(tip)}>'
            f'<code>{html.escape(h["code"])}</code>'
            f'<span class="cx-why-inc">+{h["inc"]}</span></a>')
    return "".join(out)


def _node(n, group=None, i=None, folded=None, by=None) -> str:
    """A method as a block inside its class's box. The block itself is a toggle: a click
    folds its lines open inside it. Its name alone is a link into the editor, and says so
    by underlining under the pointer, so the two targets never read as one. A method the
    branch raised starts open, on the lines that raised it."""
    cls, name = _split(n["method"])
    cog, d = n["cognitive"], n["delta"]
    mark = " cg-add" if d > 0 else " cg-cut" if d < 0 else ""
    zero = " cg-zero" if not cog and not d else ""
    hits = (group or {}).get("hits") or []
    opened = d > 0 and bool(hits)
    tip = (f'complexity {cog}' + (f" (+{d})" if d > 0 else f" (−{-d})" if d < 0 else "")
           + (". Click for its lines" if hits else ""))
    found = entry_source(n["method"])
    face = html.escape(name if "(" in name else name + "()")
    label = (f'<a class="cg-go-m" href="vscode://file/{found[0]}:{found[1]}:1"'
             f'{_tip("Open " + n["display"] + " in VS Code")}>{face}</a>' if found else face)
    more = ""
    if folded:
        names = "\n".join((by or {}).get(c, {}).get("display", c) for c in folded)
        more = (f'<span class="cg-more"'
                f'{_tip("Also called, and folded to keep this readable:" + chr(10) + names)}>'
                f'+{len(folded)}</span>')
    tog = '<span class="cg-tog" aria-hidden="true"></span>' if hits else ""
    delta = (f'<b class="cg-d">+{d}</b>' if d > 0 else
             f'<b class="cg-d">−{-d}</b>' if d < 0 else "")
    lines = f'<div class="cg-lines">{_lines(hits)}</div>' if hits else ""
    has = " cg-has" if hits else ""
    at = f' data-i="{i}"' if i is not None else ""
    return (f'<div class="cg-n{mark}{zero}{has}{" cg-open" if opened else ""}"{at}'
            f' tabindex="0" role="button" aria-expanded="{"true" if opened else "false"}"'
            f'{_tip(tip)}><span class="cg-mh">'
            f'<span class="cg-m">{tog}{label}</span>'
            f'{more}</span><span class="cg-cog">{cog}</span>'
            f'{delta}{lines}</div>')


WHY_EMPTY = ("Nothing counted: every method behind this entry point is straight-line code. "
             "Cognitive complexity charges for branching, loops and boolean runs, and there "
             "are none here.")
WHY_TIP = "{name}:{line}{deep}"


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
    """The label, a link into the method the flow starts at, so a reviewer lands on the
    controller / tool / listener that owns it instead of going hunting. It underlines
    under the pointer like the names in the call graph (Victor, 4 Nov 2026), and the
    row's caret and the rest of its head still open the fold.

    The path leads the hover, on its own line, because this column is the one that runs
    out of room: the longest route in a REST tree is usually the one a branch is about
    (`POST /api/owners/{ownerId}/pets/{petId}/visits` is this project's), and it is the
    only row wide enough to reach the column's edge. Ellipsised it now says it was cut;
    the hover is where the cut half comes back. Said once, on the whole cell, rather than
    as a second tooltip nested inside the first — two hover targets a pixel apart is how
    a reader learns to stop hovering.
    """
    face = html.escape(r["path"])
    handler = (r.get("handler") or "").strip()
    found = entry_source(r.get("entry", ""))
    label = (f'<a class="cx-path" href="vscode://file/{found[0]}:{found[1]}:1">{face}</a>'
             if found else f'<code class="cx-path">{face}</code>')
    tip = r["path"] + (f"\n{handler}" if handler else "")
    return f'<span class="cx-cell"{_tip(tip)}>{label}{_not_followed(r)}</span>'


def _not_followed(r) -> str:
    """"N not followed", when the engine met calls into this project it could not place.

    Those calls are not in the number, and a number that silently leaves things out is the
    one thing this tab must not be. So the row says how many, and the hover names each one
    — the call, where it is, and why it could not be bound — so the reader can judge
    whether what was skipped matters. Only the JavaParser engine reports them; the regex
    engine drops what it cannot place without knowing it did."""
    lost = r.get("unresolved") or []
    if not lost:
        return ""
    lines = [NOT_FOLLOWED_TIP.format(n=len(lost))]
    for u in lost[:12]:
        lines.append(f'{Path(u.get("file", "")).name}:{u.get("line", "?")}  {u.get("call", "")}'
                     f'  ({u.get("reason", "")})')
    if len(lost) > 12:
        lines.append(f"… and {len(lost) - 12} more")
    return (f'<span class="cx-nf"{_tip(chr(10).join(lines))}>{len(lost)} not followed</span>')


NOT_FOLLOWED_TIP = ("{n} call(s) into this project that could not be bound to a method — "
                    "not followed, so not in this number:")


# The bar, the delta and the total carry no tooltip. They had one, the same on all three
# ("Total complexity of this flow: 12 / 11 before, +1 by this branch"), and it only said
# again what the row already shows: the total at the end, the green part of the bar, the
# +1 beside it (Victor, 10 Nov 2026: "e evident acest tooltip, scoate-l").
# The two column heads. First line: what it counts.
TIP_COL_ADDED = "Complexity this branch added to the flow"
TIP_COL_TOTAL = "Complexity of the whole flow, on this branch"


def _tip(attr: str) -> str:
    return f' data-tip="{html.escape(attr)}"'


def _row(cls, head: str, why: str, key: str = "") -> str:
    """A row, and — for every row that has a branch to explain — the fold under it.

    The fold is a real `<details>`, opened by a click anywhere on its head but the path.
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


def render_row(r, peak) -> str:
    if r.get("gone"):
        # The branch deleted this entry point. A ghost for the whole width it used to
        # occupy — the same "slate is what the branch removed" convention as everywhere else.
        was = r["was"] or 0
        # No fold: there is no branch code left to break down. A row whose flow the branch
        # deleted has nothing to open onto, and an empty fold under it would read as a bug.
        return _row(
            "cx-down cx-gone",
            f'<span class="cx-verb cx-{r["method"].lower()}">{html.escape(r["method"])}</span>'
            f'<s>{_path_cell(r)}</s>'
            f'<span class="cx-bar">'
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
        # A typographic minus and the word: "-5" alone, in the removed colour, was read as
        # a warning. The row says what happened to the flow, not just by how much.
        badge, cls = f'−{-r["delta"]}<small> simpler</small>', "cx-down"
    else:
        badge, cls = "", "cx-same"
    # The delta rides on the *end* of the bar, so the eye reads "this much of this bar the
    # branch added" (green) or "this much it removed" (a slate ghost), instead of subtracting two numbers.
    # A growth splits the current bar; a shrink hangs the lost part off its end; a brand-new
    # entry point is added whole.
    delta = r["delta"] or 0
    now_pct = 100.0 * r["now"] / peak
    if r["delta"] is None:
        kept_pct, delta_pct = 0, now_pct
    else:
        delta_pct = 100.0 * abs(delta) / peak
        kept_pct = now_pct - (delta_pct if delta > 0 else 0)
    return _row(
        cls,
        f'<span class="cx-verb cx-{r["method"].lower()}">{html.escape(r["method"])}</span>'
        f"{_path_cell(r)}"
        f'<span class="cx-bar">'
        f'<i style="width:{kept_pct:.1f}%"></i><u style="width:{delta_pct:.1f}%"></u></span>'
        f'<span class="cx-badge">{badge}</span>'
        f'<span class="cx-n">{r["now"]}</span>',
        _why_panel(r),
        f'{r["method"]} {r["path"]}',
    )


LEDE_AST = ("with Python&#39;s own <code>ast</code>: entry points from "
            "<code>serverless*.yml</code>, and every function reachable from each handler, a call "
            "bound by the class its receiver was built from.")


def _lede_engine(engines) -> str:
    """One extractor in the Ludvig fork; a snapshot from another names itself."""
    before, after = engines
    if before == after == "ast":
        return LEDE_AST
    return (f"— the merge-base by {before}, this branch by {after}: the two sides were "
            "measured by different engines, so read the deltas with care.")


def render(rows, engines=("ast", "ast")) -> str:
    # A shrunk bar still draws what was removed past its current end, so the scale must fit
    # the taller of the two snapshots.
    peak = max((max(r["now"], r["was"] or 0) for r in rows), default=1) or 1
    out = [
        # One line, and only what the picture cannot say for itself. What counts as an
        # entry point, how the score is derived, and how many of them moved all used to
        # close this sentence; the groups below are titled by kind, every bar says on
        # hover what its colour and its number mean, and a reader counting moved rows is
        # reading the bars, not this line.
        # A heading naming the measure, then how it is taken — Victor's wording. The
        # measure's name is the link to what it means. The page's tab title and subtitle
        # (`tabtitle` / `tabsub`, the review page's core.css), so this tab opens in the same
        # size and dress as every other tab that opens on a title.
        f'<h2 class="tabtitle cx-title"><a href="{SONAR_COGNITIVE}" target="_blank" '
        'rel="noopener">Cognitive Complexity</a> per Entry Point</h2>'
        # Victor, 5 Oct 2026: name what computes it. Nothing parses Java here: the
        # extractor is this skill's own script, regular expressions over the sources with
        # comments and literals blanked and nesting counted by braces — so the line says
        # that, rather than the syntax tree the old wording implied.
        # Since 7 Oct 2026 there are two extractors, and the line names the one that ran.
        f'<p class="tabsub cx-lede">Computed by <a href="{EXTRACTOR_URL}" target="_blank" '
        f'rel="noopener">endpoint-complexity.py</a> {_lede_engine(engines)}</p>',
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
        # The two numbers at the right end of every row had no names, and a `+1` beside a
        # `12` was read as one number. The group's own header line names them, in its own
        # small caps, over the columns they sit in.
        out.append(
            f'<div class="cx-kind cx-kind-cols"><span>{html.escape(title)} '
            f'<span class="cx-count">{len(of_kind)}</span></span>'
            f'<span class="cx-colh cx-colh-added"{_tip(TIP_COL_ADDED)}>added</span>'
            f'<span class="cx-colh cx-colh-total"{_tip(TIP_COL_TOTAL)}>total</span></div>'
        )
        out.append('<div class="cx-list">')
        out.extend(render_row(r, peak) for r in of_kind)
        out.append("</div></div>")
    out.append(TOGGLE_JS)
    return "\n".join(out)


# The whole head is the handle: caret, verb, bar and numbers all open the fold. The one
# link on it — the path — opens the file and puts the fold back the way it
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
TOGGLE_JS = """<script data-hr-swap="keep">
/* Delegated on `document`, every listener — no row is bound by itself. That is what
   `data-hr-swap="keep"` promises: when a rebuild swaps this tab into an open served page
   (the review page's panel swap), the listeners registered at load serve the new rows, and
   this script is not run again. `toggle` does not bubble, so it is heard in the capture
   phase. */
(function () {
  document.addEventListener('click', function (e) {
    var head = e.target.closest && e.target.closest('summary.cx-head');
    if (!head || !e.target.closest('a')) return;        /* anywhere else: let it toggle */
    var row = head.parentNode, was = row.open;          /* the path opens the file, not the fold */
    setTimeout(function () { row.open = was; }, 0);
  });
  var P = 'cx';
  function rows() { return document.querySelectorAll('details.cx-row[data-cx]'); }
  function remember() {
    if (!history.replaceState || !window.URLSearchParams) return;
    var q = new URLSearchParams(location.search);
    q.delete(P);
    Array.prototype.forEach.call(rows(), function (r) {
      if (r.open) q.append(P, r.getAttribute('data-cx'));
    });
    var s = q.toString().replace(/%2F/gi, '/');         /* a path reads as a path */
    history.replaceState(history.state, '', location.pathname + (s ? '?' + s : '') + location.hash);
  }
  document.addEventListener('toggle', function (e) {
    var r = e.target;
    if (r && r.matches && r.matches('details.cx-row[data-cx]')) remember();
  }, true);
  var wanted = window.URLSearchParams ? new URLSearchParams(location.search).getAll(P) : [];
  Array.prototype.forEach.call(rows(), function (r) {
    if (wanted.indexOf(r.getAttribute('data-cx')) >= 0) r.open = true;
  });
  /* A box in the call graph is a toggle: it lights up and folds its own lines open. The
     names and every source line inside it are links and keep doing what links do. */
  function flip(n) {
    var on = !n.classList.contains('cg-open');
    n.classList.toggle('cg-open', on);
    n.setAttribute('aria-expanded', on ? 'true' : 'false');
    var g = n.closest('.cg[data-edges]');
    if (g) sched(g);
  }
  /* The call graph's arrows. The boxes are laid out by CSS, one column per class depth;
     here each box slides down level with its first caller, then every edge in
     `data-edges` is drawn into the graph's <svg> from the caller's right edge to the
     callee's left — or, inside one class, round the box's left margin from one block to
     the next. Redrawn whenever a block changes size: a fold opening, the tab or the row
     being shown (a hidden graph measures zero and is skipped). */
  var NS = 'http://www.w3.org/2000/svg';
  function draw(g) {
    var cols = g.querySelector('.cg-cols'), svg = cols && cols.querySelector('.cg-svg');
    if (!svg || !cols.offsetWidth) return;
    var ks = cols.querySelectorAll('.cg-k'), at = {};
    Array.prototype.forEach.call(ks, function (k) { k.style.marginTop = ''; });
    Array.prototype.forEach.call(cols.querySelectorAll('.cg-n[data-i]'), function (n) {
      at[n.getAttribute('data-i')] = n;
    });
    var edges = (g.getAttribute('data-edges') || '').split(' ').filter(Boolean).map(function (s) {
      var h = s.slice(-1) === '*', p = (h ? s.slice(0, -1) : s).split('>');
      return { a: at[p[0]], b: at[p[1]], hot: h };
    }).filter(function (e) { return e.a && e.b; });
    function box(n) {
      var base = cols.getBoundingClientRect(), r = n.getBoundingClientRect();
      var m = (n.querySelector('.cg-mh') || n).getBoundingClientRect();
      return { l: r.left - base.left, r: r.right - base.left, y: m.top + m.height / 2 - base.top };
    }
    function colOf(n) { return Array.prototype.indexOf.call(cols.children, n.closest('.cg-col')); }
    Array.prototype.forEach.call(cols.querySelectorAll('.cg-col'), function (c) {
      var here = colOf(c.firstElementChild || c);
      Array.prototype.forEach.call(c.querySelectorAll('.cg-k'), function (k) {
        for (var i = 0; i < edges.length; i++) {
          var e = edges[i];
          if (!k.contains(e.b) || colOf(e.a) >= here) continue;
          var dy = box(e.a).y - box(e.b).y;
          if (dy > 0.5) k.style.marginTop = dy + 'px';
          return;
        }
      });
    });
    /* Measured with the <svg> shrunk to nothing: an <svg> with no size is 300×150, and
       one sized by the last draw is as tall as that draw — either way it propped the
       columns up from inside, and a graph shorter than it scrolled in its own box. */
    svg.setAttribute('width', 0); svg.setAttribute('height', 0);
    svg.setAttribute('width', cols.scrollWidth);
    svg.setAttribute('height', cols.scrollHeight);
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    /* Every arrow ends on a straight, level run at least as long as its head, so the head
       always continues the line it sits on. The same-class bend used to be one curve
       whose only level stretch was shorter than the head: the head pointed right while
       the line under it was still coming down. Now it is a lane — out left, down, back
       in — with rounded corners, its last run a straight line into the block. It leaves
       its caller 6px below the middle, clear of the head of the arrow coming in; a longer
       call takes a lane further out, so two calls from one block do not share a line. */
    var HEAD = 6;
    edges.forEach(function (e) {
      var a = box(e.a), b = box(e.b), d, x = b.l, y = b.y;
      if (e.a.closest('.cg-k') === e.b.closest('.cg-k')) {
        var s = b.y < a.y ? -1 : 1, ay = a.y + s * 6, dy = b.y - ay;
        var lx = Math.min(a.l, b.l) - Math.min(16, 10 + Math.abs(dy) / 14);
        var r = Math.min(4, Math.abs(dy) / 2);
        d = 'M' + a.l + ' ' + ay + 'H' + (lx + r) + 'Q' + lx + ' ' + ay + ' ' + lx + ' ' + (ay + s * r)
          + 'V' + (b.y - s * r) + 'Q' + lx + ' ' + b.y + ' ' + (lx + r) + ' ' + b.y + 'H' + x;
      } else {
        var end = b.l - HEAD, dx = Math.max(14, Math.abs(end - a.r) / 2);
        d = 'M' + a.r + ' ' + a.y + 'C' + (a.r + dx) + ' ' + a.y + ' ' + (end - dx) + ' ' + b.y + ' ' + end + ' ' + y + 'H' + x;
      }
      var p = document.createElementNS(NS, 'path'), t = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      t.setAttribute('d', 'M' + x + ' ' + y + 'L' + (x - HEAD) + ' ' + (y - 3.5) + 'L' + (x - HEAD) + ' ' + (y + 3.5) + 'Z');
      p.setAttribute('class', 'cg-e' + (e.hot ? ' cg-hot' : ''));
      t.setAttribute('class', 'cg-ah' + (e.hot ? ' cg-hot' : ''));
      svg.appendChild(p); svg.appendChild(t);
    });
  }
  function sched(g) {
    if (g._cgq) return;
    g._cgq = (window.requestAnimationFrame || setTimeout)(function () { g._cgq = 0; draw(g); });
  }
  function arm(g) {
    if (g._cgArmed) return;
    g._cgArmed = true;
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () { sched(g); });
      ro.observe(g);
      Array.prototype.forEach.call(g.querySelectorAll('.cg-n'), function (n) { ro.observe(n); });
    }
    sched(g);
  }
  function armIn(el) {
    Array.prototype.forEach.call(el.querySelectorAll('.cg[data-edges]'), arm);
  }
  armIn(document);
  /* A row swapped in by a rebuild was not here when the script ran: arm it when it opens,
     or when the pointer first reaches its graph. */
  document.addEventListener('toggle', function (e) {
    var r = e.target;
    if (r && r.matches && r.matches('details.cx-row') && r.open) armIn(r);
  }, true);
  document.addEventListener('mouseover', function (e) {
    var g = e.target.closest && e.target.closest('.cg[data-edges]');
    if (g) arm(g);
  });
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
/* Green = complexity this branch ADDED; what it REMOVED is a neutral slate ghost, never red:
    the colour names the author of the change, it is not a verdict — and a simpler flow
    painted in alarm red read as an alarm. */
.cx-title a { color:var(--link); }
.cx-lede a { color:inherit; text-decoration:underline; text-underline-offset:2px; }
.cx-lede a:hover { color:var(--link); }
.cx-lede { --cx-added:#2e9e5b; --cx-removed:#5b6b8c; }
/* The last two columns of a row — the delta and the score — are named once, on the
    group's header line, so their widths live here and both grids read them: a label that
    copied the numbers would drift off its column the day one of them is widened. */
.cx-group { --cx-added:#2e9e5b; --cx-removed:#5b6b8c; --cx-col-added:4.4rem; --cx-col-total:2.2rem; }
.cx-group + .cx-group { margin-top:1.1rem; }
.cx-kind { font:600 11px/1 system-ui,sans-serif; text-transform:uppercase; letter-spacing:.07em;
            color:var(--muted); margin:0 0 .35rem .15rem; }
.cx-count { opacity:.65; font-weight:400; }
/* Same tracks and the same right edge as `.cx-head`: its .8rem right padding plus the
    list's 1px border. */
.cx-kind-cols { display:grid; grid-template-columns:1fr var(--cx-col-added) var(--cx-col-total);
    gap:.4rem; align-items:baseline; padding-right:calc(.8rem + 1px); }
.cx-kind-cols .cx-colh { text-align:right; }
/* "Added" is the colour of the `+2` under it; "Total" and the totals are plain, bold. */
.cx-colh-added { color:#2e7d32; }
.cx-colh-total { color:var(--fg); font-weight:700; }
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
.cx-head { display:grid; grid-template-columns:1.1rem 2.45rem minmax(9rem,17rem) 1fr
              var(--cx-col-added) var(--cx-col-total);
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
    colour, and a dimmed row comes back to full strength — together they say "click me",
    where a faint tint alone read as nothing at all. The caret does not move: nudged 2px
    right on hover, a column of them jittered as the pointer ran down the list. */
details.cx-row > summary.cx-head { cursor:pointer; transition:background .12s, box-shadow .12s; }
details.cx-row > summary.cx-head:hover { background:var(--code-bg);
    box-shadow:inset 3px 0 0 var(--link); }
details.cx-row > summary.cx-head:hover .cx-caret { color:var(--link); }
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
/* Calls the engine could not bind: not in the number, so the row says so, in amber — a
    caveat about the measurement, not about the code. */
.cx-nf { flex:none; margin-left:4px; padding:1px 5px; border-radius:8px; white-space:nowrap;
         font:600 10px/1.3 system-ui,sans-serif; color:#8a5300; background:#fff3dc;
         border:1px solid #e9c784; cursor:help; }
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
/* What the branch took away hangs off the bar's end as an outline of the old length — a
    ghost, lightly filled — so it reads as "no longer there", not as something on fire. */
.cx-down .cx-bar u { background:color-mix(in srgb, var(--cx-removed) 22%, transparent);
    box-shadow:inset 0 0 0 1.5px var(--cx-removed); }
.cx-badge small { font:600 10px/1 system-ui,sans-serif; }
.cx-same .cx-bar i { border-radius:5px; }
.cx-badge { font:700 11px/1 ui-monospace,Menlo,monospace; text-align:right; color:var(--muted);
    white-space:nowrap; }
.cx-up .cx-badge, .cx-up.cx-key { color:var(--cx-added); }
.cx-down .cx-badge, .cx-down.cx-key { color:var(--cx-removed); }
.cx-n { font:600 12px/1 ui-monospace,Menlo,monospace; text-align:right; }
.cx-head .cx-n { color:var(--fg); font-weight:700; }
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
/* `+1` is a column, not a suffix: same width on every line, so the eye can add them up
    without reading them — no brackets since 2026-09-25 (Victor: *"[+1] => +1"*), the
    `+N` badges on the graph never had them. `tabular-nums` keeps `+10` from widening the column by a hair. */
.cx-why-inc { flex:0 0 2.9rem; text-align:right; font-variant-numeric:tabular-nums;
              font:700 10.5px/1.55 ui-monospace,Menlo,monospace; color:var(--muted); }
/* ── the call graph ─────────────────────────────────────────────────────────────────
    The flow drawn left to right: one faded box per class, a column per class depth, its
    methods stacked inside as solid blocks. The page's script draws the arrows into the
    <svg> over the columns and slides each box down level with its first caller. The graph
    scrolls sideways inside its own box rather than push the page wider than the window.
    The top padding leaves room for the `+N` badge that rides above a block's corner. */
.cg { overflow-x:auto; padding:.45rem 0 .45rem; margin:0 0 .35rem -3.3rem;
      --cg-line:color-mix(in srgb, var(--muted) 60%, transparent); --cg-arrow:var(--muted);
      --cg-hot:color-mix(in srgb, var(--fg) 78%, transparent); }
.cg-cols { position:relative; display:inline-flex; align-items:flex-start; gap:30px;
           padding:8px 10px 4px 2px; }
/* Over the boxes' tint, under the blocks: an arrow inside a class runs round the box's
    left margin, and ends on a block's frame, never across its text. */
.cg-svg { position:absolute; left:0; top:0; overflow:visible; pointer-events:none; z-index:1; }
.cg-svg .cg-e { fill:none; stroke:var(--cg-line); stroke-width:1.2; }
.cg-svg .cg-ah { fill:var(--cg-arrow); }
/* The edges that lead to what the branch changed are the path the reader came for. */
.cg-svg .cg-e.cg-hot { stroke:var(--cg-hot); stroke-width:1.6; }
.cg-svg .cg-ah.cg-hot { fill:var(--cg-hot); }
.cg-col { display:flex; flex-direction:column; align-items:flex-start; gap:12px; }
/* A class: one faint neutral tint and a hairline of it, every class alike, named once on
    top. The left padding is the lane the same-class arrows run in. */
.cg-k { position:relative; padding:0 8px 8px 18px; border-radius:9px;
        background:color-mix(in srgb, var(--muted) 7%, transparent);
        border:1px solid color-mix(in srgb, var(--muted) 30%, transparent); }
.cg-kh { display:flex; align-items:center; gap:3px; height:22px; margin-left:-10px; }
.cg-c { font:500 10.5px/14px system-ui,sans-serif; color:var(--muted); }
/* A name is the way into the editor: it keeps its colour at rest, so the graph stays
    quiet, and underlines under the pointer, class and method alike. */
a.cg-c, a.cg-go-m, a.cx-path { color:inherit; text-decoration:none; }
a.cg-c { color:var(--muted); }
a.cg-c:hover, a.cg-go-m:hover, a.cx-path:hover { color:var(--link); text-decoration:underline; }
.cg-ms { display:flex; flex-direction:column; align-items:stretch; gap:9px; }
/* A method: a solid card on the faded box, its name bold, its score hard right. The
    names are set in the UI face, not monospace: the graph is as wide as its deepest chain
    times its widest names, and a proportional face buys back a fifth of that. */
.cg-n { display:grid; grid-template-columns:1fr auto; column-gap:8px; align-items:center;
        padding:0; border:1px solid color-mix(in srgb, var(--fg) 24%, var(--line));
        border-radius:6px; background:var(--card); color:inherit; white-space:nowrap;
        cursor:pointer; position:relative; z-index:2;
        box-shadow:0 1px 2px color-mix(in srgb, #000 14%, transparent);
        transition:border-color .12s, background .12s; }
.cg-n:hover { border-color:var(--link); }
.cg-n:focus-visible { outline:2px solid var(--link); outline-offset:1px; }
.cg-mh { display:flex; align-items:center; gap:4px; height:24px; padding-left:7px; }
.cg-m { display:flex; align-items:center; font:600 11.5px/14px system-ui,sans-serif; }
.cg-cog { grid-column:2; grid-row:1; padding-right:7px; font:700 10px/1.3 ui-monospace,Menlo,monospace;
          text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); }
/* Clicked open: a tint of the link blue, and its frame unless the branch's green holds it. */
.cg-n.cg-open { background:color-mix(in srgb, var(--link) 8%, var(--card)); opacity:1; }
.cg-n.cg-open:not(.cg-add):not(.cg-cut) { border-color:var(--link);
    box-shadow:0 0 0 1px var(--link) inset; }
/* The fold's caret sits in front of the method name, the word a reader clicks on — the
    full-size ▶, not ▸: at the name's own size ▸ was a speck, and a speck does not say
    "this opens". */
.cg-tog { display:inline-flex; margin-right:5px; }
.cg-tog::before { content:"\\25B6\\FE0E"; font-size:13px; line-height:1; color:var(--muted);
                  transition:transform .12s; }
.cg-n:hover .cg-tog::before { color:var(--link); }
.cg-open .cg-tog::before { transform:rotate(90deg); color:var(--link); }
.cg-lines { display:none; grid-column:1 / 3; margin:0 0 1px; padding:3px 7px 0;
            border-top:1px dashed var(--line); cursor:auto; }
.cg-open > .cg-lines { display:block; }
.cg-lines a.cx-why-line code { font-size:11px; }
.cg-d { font:700 9.5px/1 ui-monospace,Menlo,monospace; position:absolute; top:-6px; right:-6px;
        padding:1px 3px; border-radius:6px; background:var(--card); }
.cg-zero { opacity:.7; }
.cg-zero .cg-m { font-weight:500; }
.cg-more { font:700 10px/16px ui-monospace,Menlo,monospace; color:var(--muted); padding:0 5px;
           border:1px dashed var(--line); border-radius:6px; cursor:help; }
/* A method whose score this branch raised wears the added colour on its frame and its
    number — open or shut; one it lowered, the removed colour. */
.cg-n.cg-add { border-color:var(--cx-added); box-shadow:0 0 0 1px var(--cx-added) inset; }
.cg-n.cg-add.cg-open { background:color-mix(in srgb, var(--cx-added) 7%, var(--card)); }
.cg-add .cg-d, .cg-add .cg-cog { color:var(--cx-added); }
.cg-n.cg-cut { border-color:var(--cx-removed); }
.cg-cut .cg-d { color:var(--cx-removed); }
/* Lines of methods folded into `+N`: one closed fold under the graph, never lost. */
.cx-why-rest > summary { cursor:pointer; font-size:.78rem; color:var(--muted); margin:.1rem 0 .3rem; }
/* Green is authorship everywhere else on this tab, and it means the same here: this line
    was not behind this entry point at the merge-base. */
a.cx-why-new code { color:var(--cx-added); }
a.cx-why-new .cx-why-inc { color:var(--cx-added); }
@media (prefers-color-scheme: dark) {
  .cx-lede, .cx-group { --cx-added:#4ec27f; --cx-removed:#9fb0d0; }
  .cx-colh-added { color:#4ec27f; }
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
  .cx-nf { background:#3a2d12; color:#f0c069; border-color:#6b5423; }
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
    args = ap.parse_args(argv)

    if args.css:
        print(CSS)
        return 0
    if not args.before or not args.after:
        ap.error("the following arguments are required: before, after")

    rows = compare(*load_pair(Path(args.before), Path(args.after)))
    engines = (engine_of(Path(args.before)), engine_of(Path(args.after)))
    body = json.dumps(rows, indent=1) if args.json else render(rows, engines)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"[complexity-delta] wrote {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
