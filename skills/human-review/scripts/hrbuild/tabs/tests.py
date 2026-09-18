"""The Requirements/Tests tab: the ledger, the requirement lists, the recordings."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ..shared.util import PENCIL, TESTCHANGES

# What happened to a test, and what the page calls it. The colour classes are the page's
# existing added/removed vocabulary — the same green and red the diff gutters, the line
# counts in the scope bar and the diagram deltas already use, dark mode included — because
# a fourth palette for the fourth surface would read as a fourth meaning.
# The tab the test ledger belongs to when no block asks for it by hand. It is the tab id
# `run-steps.py` already attributes the `tests` step to; the label above it reads "Tests".
LEDGER_TAB = "requirements"

TEST_STATES = {
    "added":     ("added", "new"),
    "modified":  ("changed", "modified"),
    "deleted":   ("removed", "deleted"),
    "unchanged": ("same", "unchanged"),
}
# A test that is still written but no longer runs. It keeps its diff state — a disabled
# test that was also edited is both — because the two answer different questions: what
# the branch did to the code, and whether the code still holds anything up. A commented-out
# test is flagged `deleted`, which is what it costs the run, and stamped `commented out`,
# which is what it costs to undo.
SILENCED_LABEL = {
    "disabled":  "disabled",
    "commented": "commented out",
}


def _test_changes_module():
    """`test-changes.py`, loaded by path — a hyphen is not an identifier."""
    import importlib.util
    if "test_changes" in sys.modules:
        return sys.modules["test_changes"]
    spec = importlib.util.spec_from_file_location("test_changes", str(TESTCHANGES))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_changes"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_index(rows) -> dict:
    """The manifest, keyed both ways: by `(path, name)` and by name alone.

    Naming the path in the content file is optional, because most test names are unique
    across a change set and repeating the path for each is noise. When one is not unique
    the build says so rather than picking a side."""
    by_key, by_name = {}, {}
    for r in rows:
        by_key[(r["path"], r["name"])] = r
        by_name.setdefault(r["name"], []).append(r)
    return {"key": by_key, "name": by_name}


def resolve_tests(entries, index: dict, root: Path) -> list[dict]:
    """Attach each named test to what the diff says happened to it.

    A test the manifest does not mention is not an error: a requirement is often covered
    by a test nobody touched, and saying so is worth a row. But it has to *exist* — the
    file is parsed for the declaration, and a name that is nowhere in it fails the build,
    for the same reason `resolve_refs` fails on a stale path. A coverage claim that links
    to nothing is worse than no claim."""
    out = []
    for e in entries:
        name, rel = e["name"], e.get("path")
        if rel:
            row = index["key"].get((rel, name))
        else:
            hits = index["name"].get(name, [])
            if len(hits) > 1:
                raise SystemExit(
                    f"[review] the test name {name!r} occurs in {len(hits)} changed files "
                    f"({', '.join(sorted(h['path'] for h in hits))}) — add a 'path' to the "
                    "entry so the page links the right one."
                )
            row = hits[0] if hits else None
        if row is None:
            if not rel:
                raise SystemExit(
                    f"[review] test {name!r} is not in the change set, so it needs a 'path' "
                    "saying which existing file it lives in."
                )
            f = root / rel
            if not f.is_file():
                raise SystemExit(f"[review] test {name!r} names a file that does not exist: {rel}")
            found = _test_changes_module().scan_cases(
                rel, f.read_text(encoding="utf-8", errors="replace")).get(name)
            if found is None:
                raise SystemExit(
                    f"[review] no test called {name!r} in {rel} — the change set did not touch "
                    "it and the file does not declare it either. Fix the name, or the path."
                )
            # Untouched by this branch, but the page still has to say whether it runs: a
            # requirement pinned by a test somebody `@Disabled`d last month is not pinned,
            # and the branch that inherits the claim is where a reader will see it.
            line, silenced = found
            row = {"name": name, "path": rel, "status": "unchanged", "line": line}
            if silenced:
                row["silenced"] = silenced
        out.append(dict(row, note=e.get("note", "")))
    return out


def render_tests(rows, root: Path, flags: bool = True) -> str:
    """The sub-list under one requirement: what pins it, and what the diff did to each.

    The link is the page's ordinary `vscode://file/…` reference, so it inherits the whole
    fallback chain in EDITOR_JS for free — served, top level, or embedded in a webview.
    A test whose *file* was deleted gets no link, deliberately: there is nothing on disk
    to open, and a dead custom URL is the one thing this page never emits."""
    if not rows:
        return ""
    items = []
    for r in rows:
        cls, label = TEST_STATES.get(r["status"], TEST_STATES["unchanged"])
        where = Path(r["path"]).name + (f':{r["line"]}' if r.get("line") else "")
        inner = (f'{html.escape(r["name"])} '
                 f'<span class="tloc">{html.escape(where)}</span>')
        if r.get("line") and not r.get("gone"):
            target = (root / r["path"]).resolve()
            body = (f'<a class="srcref testref" href="vscode://file/{target}:{r["line"]}:1"'
                    f' data-tip="{html.escape(r["path"])}">{inner}</a>')
        else:
            why = ("the file is gone" if r.get("gone") else "no line left to open it at")
            body = (f'<span class="srcref testref tgone"'
                    f' data-tip="{html.escape(r["path"])} — {why}">{inner}</span>')
        # Said after the link rather than in front of it, and in a second vocabulary. The
        # flag column answers "what did the branch do to this test"; the stamp answers
        # "does it still run", which is a different question and can contradict the first
        # — a row flagged `new` and stamped `disabled` is the loudest case on this page,
        # and the one a single fixed-width column would have had to choose between. It
        # also keeps that column aligned: "commented out" is twice the width of the words
        # around it, and a flag that shoves its own row sideways costs more than it says.
        state = ""
        if r.get("silenced"):
            state = (f'<span class="tsilenced" data-tip="Still written; never runs.">'
                     f'{SILENCED_LABEL.get(r["silenced"], r["silenced"])}</span>')
        elif r.get("wasSilenced") and r["status"] != "deleted":
            state = ('<span class="tback" data-tip="Was disabled; runs now.">'
                     "back on</span>")
        note = f' <span class="tnote">{r["note"]}</span>' if r.get("note") else ""
        # Off inside the ledger below, where the group heading already says the word and
        # a column repeating `NEW` twenty-two times is a column of noise. Kept everywhere
        # else, and kept even in the ledger's one mixed group.
        flag = f'<span class="tflag {cls}">{label}</span>' if flags else ""
        items.append(f'<li>{flag}{body}{state}{note}</li>')
    return '<ul class="req-tests">' + "\n".join(items) + "</ul>"


def render_test_ledger(rows, root: Path) -> tuple[str, int]:
    """Every test the change set moved, as one list — `(html, how many it moved)`.

    The requirement lists above answer "is *this* sentence pinned, and by what". They
    cannot answer the question a reviewer asks next, which is the blunt one: *what did
    this branch do to the tests?* A test that pins no requirement anybody wrote down —
    and a deleted one, which by definition is no longer under any requirement — appears
    in no list on the page otherwise. The chip at the top states the count; this is where
    the count is spelled out into names you can click.

    Each test appears exactly once, under the most consequential thing that happened to
    it. Silenced comes first for that reason: a test that is *new* and `@Disabled` is not
    news about coverage, it is news about a test that has never run, and filing it under
    "new" would hide it among twenty-one that do run. The untouched rest are counted in a
    sentence rather than listed — a reviewer scrolling past a hundred unchanged names to
    find the two that went away is a reviewer who stops scrolling.
    """
    groups = [
        ("stopped running", "Still written, and no longer part of any run — nothing "
                            "under them is asserted on any build.", []),
        ("new", "Tests this change set wrote.", []),
        ("gone", "Tests the run has lost — deleted outright, or commented out in place.", []),
        ("edited", "Tests whose body this change set moved: worth reading for what they "
                   "stopped asserting, not only for what they now do.", []),
    ]
    untouched = 0
    for r in rows:
        if r.get("silenced") and r["status"] != "deleted":
            groups[0][2].append(r)
        elif r["status"] == "added":
            groups[1][2].append(r)
        elif r["status"] == "deleted":
            groups[2][2].append(r)
        elif r["status"] == "modified":
            groups[3][2].append(r)
        else:
            untouched += 1

    moved = sum(len(g[2]) for g in groups)
    if not moved and not untouched:
        return "", 0
    blocks = []
    for name, why, items in groups:
        if not items:
            continue
        # The one group whose rows do not share a fate: a silenced test may be new,
        # edited or untouched, and which it is changes what the reader does about it.
        mixed = name == "stopped running"
        blocks.append(
            f'<section class="tgroup{" tgroup-off" if mixed else ""}">'
            f'<h3>{html.escape(name)} <b>{len(items)}</b></h3>'
            f'<p class="sub">{html.escape(why)}</p>'
            + render_tests(items, root, flags=mixed)
            + "</section>"
        )
    rest = (f'<p class="sub">{untouched} more test'
            f'{"s" if untouched != 1 else ""} in the files this change set touched, '
            "left exactly as they were.</p>") if untouched else ""
    return '<div class="tledger">' + "".join(blocks) + "</div>" + rest, moved




def _ms(value) -> str:
    """`1658` → `1.7s`. Under a second stays in milliseconds: a step that took 43ms and
    one that took 430ms are a different kind of fast, and `0.0s` says neither."""
    ms = int(value or 0)
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def render_traces(doc: dict, root: Path, out_dir: Path,
                  touched: set[tuple[str, int]] | None = None) -> tuple[str, int]:
    """What the run recorded, as a registry the 📺 on the covering-tests rows reads.

    Nothing visible. There used to be a list here — "Step through what the tests did",
    one collapsible row per recording with the viewer framed inside it — and every word
    on it was already on the covering-tests map above: the test's title, its file and
    line, whether it passed. The one thing the row added was the way into the recording,
    and that is now the 📺 itself: served, it opens the viewer in a window of its own,
    where a three-pane application belongs, instead of in 78vh of a text column that has
    to be scrolled to keep the snapshot pane in view.

    The registry keys a recording by the test's file basename and declaration line, which
    is exactly how the map addresses a row, so the pairing is a lookup and not a guess.
    `viewer` is the copied trace viewer, relative to the page; `trace` is the zip; `cmd`
    is the line that opens the same recording natively, for a reader holding the page as
    a file — from the zip, from Pages — where the viewer cannot fetch anything. It is
    written whether or not that reader exists, because the build cannot know which of
    the two is reading. `touched` is accepted for the caller's sake and no longer changes
    what is emitted: with no rows there is no order to put the branch's own tests in.
    """
    tests = doc.get("tests") or []
    if not tests:
        return "", 0
    # Where this page was built, said the way a terminal at the repo root would say it:
    # `.human-review` is only the default, and a command naming a directory the reader does
    # not have is worse than no command at all.
    try:
        here = out_dir.resolve().relative_to(root.resolve())
    except ValueError:
        here = out_dir.resolve()
    entries = []
    for t in tests:
        if not t.get("trace"):
            continue
        key = Path(t.get("file", "")).name + (f':{t["line"]}' if t.get("line") else "")
        entries.append({"test": key, "trace": t["trace"], "status": t.get("status", ""),
                        "cmd": f"npx playwright show-trace {shlex.quote(str(here / t['trace']))}"})
    reg = {"viewer": doc.get("viewer") or "", "tests": entries}
    # `</` cannot appear inside a script element, whatever its type.
    return ('<script type="application/json" id="hr-traces">'
            + json.dumps(reg).replace("</", "<\\/") + "</script>", len(entries))


def render_requirements(items, index: dict, root: Path) -> str:
    """Each requirement, with the tests that pin it nested under its own text.

    Nested rather than tabulated on purpose: the question a reviewer is asking here is
    "is *this* requirement covered, and by what", and a table elsewhere on the page makes
    them hold the requirement in their head while they go and look it up."""
    if not items:
        return ""
    lis = []
    for it in items:
        lis.append(
            '<li>'
            + f'<div class="req-text">{it.get("text", "")}</div>'
            + render_tests(resolve_tests(it.get("tests", []), index, root), root)
            + '</li>'
        )
    return '<ul class="reqlist">' + "\n".join(lis) + "</ul>"


def tests_chip(doc: dict | None) -> dict | None:
    """`{"auto":"tests"}` — what the branch did to the test run, counted off the test
    code itself by `test-changes.py`.

    It replaces a chip that used to be typed by hand (`unit tests · 125 green (20 new)`),
    which could only ever be true for as long as nobody wrote another test. This one
    states the number a reviewer acts on, and states it as a balance: how many tests
    entered the run, how many left it. Both halves matter, and the second is the reason
    the chip exists — a branch that adds nine tests and quietly `@Disabled`s three has
    not added nine.

    The loss is deliberately one number over three causes. Deleting a test, commenting it
    out and disabling it cost the run the same test, and only deletion is visible to
    someone skimming a diff; splitting them on the chip's face would invite reading the
    smallest one as the answer. The split is in the tooltip, where it belongs.

    Returns None when there is no manifest — dropping the chip rather than printing a
    zero, which would read as "this branch touched no tests" when the truth is "nobody
    counted".
    """
    t = (doc or {}).get("totals")
    if not t:
        return None
    balance = " / ".join(
        piece for piece in (
            f'<span class="added">+{t["gained"]}</span>' if t["gained"] else "",
            f'<span class="removed">\u2212{t["lost"]}</span>' if t["lost"] else "",
        ) if piece
    )
    # `PENCIL` for the edited ones, beside `+` and `−`; see the constant for why it is
    # not `±` any more. It retired a `~` before that, for the same reason: an
    # approximation standing in for a number that was never approximate.
    edited = f'<span class="changed">{PENCIL}{t["modified"]}</span>' if t["modified"] else ""
    value = " / ".join(x for x in (balance, edited) if x) or "none touched"

    # A hover is read standing up, one glance, hand on the mouse. It gets the numbers the
    # face could not fit and stops — the reasoning behind them is in this docstring, where
    # whoever needs it is already reading. Three clauses at the outside.
    gone = [f'{t["deleted"] - t["commented"]} deleted' if t["deleted"] - t["commented"] else "",
            f'{t["commented"]} commented out' if t["commented"] else "",
            f'{t["disabled"]} disabled' if t["disabled"] else ""]
    gone = ", ".join(x for x in gone if x) or "none lost"
    # A new test that arrives `@Disabled` is written but never ran, so it is in `added`
    # and not in `gained`. Without this the two numbers look like a bug — "22 new" over a
    # chip reading `+21` — when they are in fact the finding.
    inert = t["added"] - (t["gained"] - t["reenabled"])
    tip = (f'{t["added"]} new'
           + (f' ({inert} disabled on arrival)' if inert else "")
           + f', {t["modified"]} edited, {gone}'
           # The one clause that has to survive the cut: it is why `+10` can stand over
           # `9 new`, and without it the face looks like it cannot add up.
           + (f', {t["reenabled"]} back on' if t["reenabled"] else ""))
    return {"label": "tests", "value": value, "tip": tip}


# --- the matrix's own layout -----------------------------------------------------------
#
# The requirements↔tests matrix is the one fragment on this tab the build does not write:
# a model renders `assets/requirements-map.html` and the section pastes it in whole. What
# the model may decide is what the matrix *says* — which test covers which sentence, and
# how honestly. Where the two columns sit, and what is written over them, is not that kind
# of question: it is the same answer on every branch, in every repository, and a layout
# that comes back slightly different after each paid run is a page the reader has to learn
# again. So the frame is taken back here, deterministically, on every build.
#
# Three things move, and all three are the same correction — *a key is read once, a title
# is read first*:
#
#   * the ticket's **title** goes over the ticket. It was nowhere on the page: the masthead
#     carries the PR's title, which on this branch is not the issue's, and the ticket frame
#     opens straight into `victorrentea opened on Jun 13, 2026` with nothing saying what
#     was opened. It is a link to the issue, because the reader's next question after
#     reading four sentences of a ticket is the rest of it;
#   * the **colour legend** (`fully covered … N/A`) goes under the ticket it explains;
#   * the **UI/API/unit key** goes under the card it explains.
#
# Nothing else moves. The card's own header strip — the robot, *Covering tests*, *as
# matched by AI* — stays inside the card, where it is the exact counterpart of the strip
# the ticket wears: two frames, each headed by who wrote what is in it. Lifting it out to
# pair it with the ticket's title *looked* symmetrical and was not — it left the right-hand
# frame bare-topped while the left kept its strip, and stacked on a narrow window it
# stranded the card's byline a screen above the card.
#
# The title is made a child of `.rm-body` rather than of the left column, and that is the
# whole of the alignment: it is a grid row of its own, spanning nothing on the right, so
# the row under it starts both columns together. The two frames are level by construction —
# at any width, with no measured constant to keep in step. There was such a constant
# (`--rm-key-h:25px`, "measured: the pill row draws 24.7px, the text row 23px") and it is
# exactly the kind of number that goes stale the first time a font changes.

#: The resolved ticket, cached under the report directory. The build must not need a
#: network to draw a title it drew yesterday — and `gh` is not available to every reader
#: of this repository at all. Asked once, written down, read from disk ever after.
TICKET_CACHE = "ticket.json"


def _element(s: str, i: int) -> tuple[int, int] | None:
    """`(start, end)` of the element whose opening tag starts at `s[i]`, nesting counted.

    A regex cannot do this: `.rm-ticket` holds two more divs and `<div class="rm-ticket">
    .*?</div>` stops at the first of their closing tags. Self-closing tags are skipped
    rather than counted, so a stray `<br/>` inside the element does not unbalance it."""
    m = re.compile(r"<([A-Za-z][\w-]*)").match(s, i)
    if not m:
        return None
    tag = m.group(1)
    depth = 0
    for t in re.finditer(rf"<(/?){tag}\b[^>]*?(/?)>", s[i:]):
        if t.group(1):
            depth -= 1
            if depth == 0:
                return i, i + t.end()
        elif not t.group(2):
            depth += 1
    return None


def _find(s: str, cls: str) -> int | None:
    """Where the first element carrying `cls` in its class list opens."""
    for m in re.finditer(r"<[A-Za-z][\w-]*\b[^>]*>", s):
        attr = re.search(r'class="([^"]*)"', m.group(0))
        if attr and cls in attr.group(1).split():
            return m.start()
    return None


def _take(s: str, cls: str) -> tuple[str, str] | None:
    """Lift the first `cls` element out of `s` — `(what is left, the element)`."""
    i = _find(s, cls)
    if i is None:
        return None
    span = _element(s, i)
    if span is None:
        return None
    a, b = span
    return s[:a] + s[b:], s[a:b]


def _append_inside(el: str, extra: str) -> str:
    """`extra` as the element's new last child."""
    close = el.rfind("<")
    return el[:close] + extra + el[close:]


def _issue_url(pr: dict, number: int) -> str:
    repo = (pr.get("repo") or "").rstrip("/")
    return f"{repo}/issues/{number}" if repo else ""


def _gh_issue(pr: dict, number: int, out_dir: Path) -> dict | None:
    """Ask GitHub once for the issue's title, and write the answer down."""
    slug = re.sub(r"^https?://github\.com/", "", pr.get("repo") or "").strip("/")
    args = ["gh", "issue", "view", str(number), "--json", "number,title,url"]
    if slug:
        args += ["-R", slug]
    try:
        raw = subprocess.run(args, capture_output=True, text=True, timeout=20,
                             check=True).stdout
        got = json.loads(raw)
        ref = {"number": int(got["number"]), "title": got["title"],
               "url": got.get("url") or _issue_url(pr, number)}
    except Exception as exc:                      # noqa: BLE001 - every failure is the same
        # Not fatal, and deliberately not fatal: a reader building this page on a machine
        # with no `gh`, no token or no network gets the matrix with its title row empty,
        # which is one missing sentence. Refusing the build over it would cost them the
        # whole tab for a heading.
        print(f"[review] no ticket title on the matrix: `gh issue view {number}` "
              f"did not answer ({exc}). It is cached in "
              f"{TICKET_CACHE} once it does.", file=sys.stderr)
        return None
    try:
        (out_dir / TICKET_CACHE).write_text(json.dumps(ref, indent=1) + "\n",
                                            encoding="utf-8")
    except OSError:
        pass
    return ref


def ticket_ref(spec: dict, out_dir: Path) -> dict | None:
    """`{"number", "title", "url"}` of the ticket this branch answers, or None.

    Four sources, in the order of how much they are worth. An explicit `pr.ticket` block
    in the content file is an author saying which ticket this is and what it is called,
    and nothing overrules it. A cached `ticket.json` is the same answer, resolved by an
    earlier build. Otherwise the number is read off the PR's own title — `Link Visit with
    Vet (#37), reimplemented unguided by Opus` names its issue, and a `#49` there would be
    the PR quoting itself, so the PR's own number is not a candidate — and the title comes
    from `gh issue view`, which is asked once and written down.

    What is never a source is the model: the matrix is regenerated by a paid run, and a
    heading that changed wording between two runs of the same branch would be the page
    disagreeing with GitHub about what the ticket is called."""
    pr = spec.get("pr") or {}
    declared = (pr.get("ticket") or pr.get("issue")
                or spec.get("ticket") or spec.get("issue"))
    number, title, url = None, "", ""
    if isinstance(declared, dict):
        number = declared.get("number")
        title = declared.get("title") or ""
        url = declared.get("url") or ""
    elif declared:
        m = re.search(r"\d+", str(declared))
        number = m.group() if m else None
    if number is None:
        for m in re.finditer(r"#(\d+)", pr.get("title") or ""):
            if int(m.group(1)) != pr.get("number"):
                number = m.group(1)
                break
    if number is None:
        return None
    number = int(number)
    if title:
        return {"number": number, "title": title, "url": url or _issue_url(pr, number)}
    try:
        cached = json.loads((out_dir / TICKET_CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    if cached.get("number") == number and cached.get("title"):
        return {"number": number, "title": cached["title"],
                "url": cached.get("url") or _issue_url(pr, number)}
    return _gh_issue(pr, number, out_dir)


def ticket_head(ref: dict | None) -> str:
    """The ticket's title over its frame — the issue's own, never the PR's.

    GitHub's own shape, because that is where the reader has read this title before: the
    title, then the number after it in the muted weight. The whole of it is the link;
    half a title being clickable is a target nobody aims at."""
    if not ref:
        return ""
    face = (f'{html.escape(ref["title"])} '
            f'<span class="rm-num">#{ref["number"]}</span>')
    body = (f'<a class="rm-title" href="{html.escape(ref["url"])}">{face}</a>'
            if ref.get("url") else f'<span class="rm-title">{face}</span>')
    return f'<p class="rm-head">{body}</p>'


#: The layout above, as the stylesheet that has to hold it. Emitted with the fragment
#: rather than added to `page.css` on purpose: every rule here is scoped to `.reqmap` and
#: is meaningless — dead weight in every other tab's stylesheet — on a page built without
#: the matrix. It lands after the model's own `<style>`, so equal specificity resolves the
#: way it has to.
REQMAP_CSS = """
<style>
/* The ticket's title is a grid row of its own above both columns, so the row below it
   starts them level whatever the title does — one line, two lines, or nothing at all when
   no ticket could be resolved. Widths are the flex layout's, restated: the card was
   `flex:0 0 50%` of the body and is now a 50% track, and its `max-width:50%` has to go or
   it would be read against its own track and halve the card. `row-gap` has to be said as
   well: the 46px `gap` under it is a gutter between two columns, and inherited downwards
   it opened half a screen between the title and the ticket it names. */
.reqmap .rm-body{display:grid;grid-template-columns:1fr 50%;column-gap:46px;row-gap:0;
  align-items:start}
.reqmap .rm-text{grid-column:1;grid-row:2}
.reqmap .rm-side{grid-column:2;grid-row:2;max-width:none}
/* A heading's distance from the thing it heads, not a column gutter's: close enough under
   it to read as its title, with a little air over it so it does not hang off the tab strip. */
.reqmap .rm-head{grid-column:1;grid-row:1;display:flex;align-items:center;
  margin:10px 2px 8px;min-width:0}
/* The ticket's own heading scale, not the page's h2: this is quoted furniture around
   quoted text, and an h2 here would outrank the tab's own heading. */
.reqmap .rm-head .rm-title{font-size:1.35em;line-height:1.25;font-weight:600;
  color:var(--fg,#1c1c1c);text-decoration:none;overflow-wrap:anywhere}
.reqmap .rm-head .rm-title:hover{text-decoration:underline}
.reqmap .rm-head .rm-num{color:var(--muted,#6b6b6b);font-weight:400}
/* Both keys now sit under what they explain, so the margin that lifted them off it moves
   to the other side. The shared `--rm-key-h` band goes with them: above the frames it
   kept two unequal rows on one line, and under them there is nothing to keep level. */
.reqmap .rm-legend{min-height:0;margin:10px 2px 0}
.reqmap .rm-cats{min-height:0;margin:10px 2px 0}
/* Stacked, the grid is one column: title, ticket, card. The gutter the two columns shared
   becomes the gap between them, which `row-gap:0` above gave up for the title's sake. */
@media (max-width:900px){
  .reqmap .rm-body{grid-template-columns:1fr}
  .reqmap .rm-head,.reqmap .rm-text,.reqmap .rm-side{grid-column:1;grid-row:auto}
  .reqmap .rm-side{margin-top:46px}
}
</style>"""


def reqmap_layout(frag: str, spec: dict, out_dir: Path) -> str:
    """Re-lay the model's requirements↔tests matrix, or hand it back untouched.

    Every include on the page goes through here and only the matrix is recognised, by the
    `reqmap` class the prompt has always required. Recognised and then *checked*: each
    piece this moves is looked up by name, and a piece that is not there aborts the whole
    rewrite rather than emitting half of it. The fragment is written by a model and the
    honest failure mode is the layout the model shipped, with a line on stderr saying the
    build could not find its footing — not a column with its heading gone."""
    if 'class="reqmap"' not in frag:
        return frag

    def give_up(what: str) -> str:
        print(f"[review] the matrix kept the model's own layout: no {what} in "
              "requirements-map.html. If the fragment was redesigned, "
              "hrbuild/tabs/tests.py:reqmap_layout is what has to learn the new names.",
              file=sys.stderr)
        return frag

    m = re.search(r'<div class="rm-body"[^>]*>', frag)
    if not m:
        return give_up(".rm-body")
    span = _element(frag, m.start())
    if span is None:
        return give_up("closing tag for .rm-body")
    a, b = span
    inner = frag[a + len(m.group(0)):b - len("</div>")]

    cut_text = _take(inner, "rm-text")
    if not cut_text:
        return give_up(".rm-text")
    inner, text_col = cut_text
    cut_side = _take(inner, "rm-side")
    if not cut_side:
        return give_up(".rm-side")
    _, side_col = cut_side

    cut_legend = _take(text_col, "rm-legend")
    if not cut_legend:
        return give_up(".rm-legend")
    text_col, legend = cut_legend
    cut_cats = _take(side_col, "rm-cats")
    if not cut_cats:
        return give_up(".rm-cats")
    side_col, cats = cut_cats

    text_col = _append_inside(text_col, legend)
    side_col = _append_inside(side_col, cats)
    body = (m.group(0) + ticket_head(ticket_ref(spec, out_dir))
            + text_col + side_col + "</div>")
    return frag[:a] + body + frag[b:] + REQMAP_CSS
