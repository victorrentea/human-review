"""The Requirements/Tests tab: the ledger, the requirement lists, the recordings."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ..shared.actions import rebuild_interpreter
from ..shared.commands import RUN_TESTS_FACE
from ..shared.util import PENCIL, TEST_KIND_ORDER, TESTCHANGES

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


#: What the page calls a test whose own lines are untouched but which calls a helper in
#: the same file that this change set rewrote (`test-changes.py:helpers_called`). Counted
#: under edited — the run exercises different code — and said apart from a body edit.
VIA_HELPER_LABEL = "edited via helper"


def via_helper_tip(helpers: list[dict]) -> str:
    """`Its own lines are unchanged; it calls anOwnerWithAPet() (line 105, +13/−6), which
    this change set rewrote.`"""
    named = ", ".join(f'{h.get("name")}() (line {h.get("line")}, '
                      f'+{h.get("added", 0)}/\u2212{h.get("removed", 0)})' for h in helpers)
    return (f"Its own lines are unchanged; it calls {named}, which this change set "
            "rewrote in the same file.")


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
        # A deleted test (not a commented-out one, which is still on disk) is shown and
        # linked where it WAS: its working-tree `line` is only where the removal landed,
        # and unrelated code sits there now. So the location is the base commit's line
        # and the link is the base commit's blob — never a vscode:// into HEAD.
        at_base = r["status"] == "deleted" and r.get("silenced") != "commented"
        line = r.get("baseLine") if at_base else r.get("line")
        where = Path(r["path"]).name + (f':{line}' if line else "")
        inner = (f'{html.escape(r["name"])} '
                 f'<span class="tloc">{html.escape(where)}</span>')
        if at_base:
            sha = (r.get("baseSha") or "")[:8]
            at = f" at the base commit {sha}" if sha else " at the base commit"
            if r.get("baseUrl"):
                body = (f'<a class="srcref testref tbase" href="{html.escape(r["baseUrl"])}"'
                        f' target="_blank" rel="noopener"'
                        f' data-tip="{html.escape(r["path"])}:{line or ""}{at} — deleted '
                        f'on this branch; opens on GitHub">{inner}</a>')
            else:
                show = f"git show {sha or 'BASE'}:{r['path']}"
                body = (f'<span class="srcref testref tgone" data-tip="'
                        f'{html.escape(r["path"])}:{line or ""}{at} — deleted on this '
                        f'branch; see it with: {html.escape(show)}">{inner}</span>')
        elif r.get("line") and not r.get("gone"):
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
        if r.get("viaHelper"):
            # Its own lines are as they were; a same-file helper it calls is not.
            note = (f' <span class="tnote tvia" data-tip="'
                    f'{html.escape(via_helper_tip(r["viaHelper"]), quote=True)}">'
                    f'{VIA_HELPER_LABEL}</span>') + note
        # Off inside the ledger below, where the group heading already says the word and
        # a column repeating `NEW` twenty-two times is a column of noise. Kept everywhere
        # else, and kept even in the ledger's one mixed group.
        flag = f'<span class="tflag {cls}">{label}</span>' if flags else ""
        items.append(f'<li>{flag}{body}{state}{note}</li>')
    return '<ul class="req-tests">' + "\n".join(items) + "</ul>"


def _names_by_file(rows) -> str:
    """`a.spec.ts: x, y; B.java: z` — the names a count stands for, for its hover."""
    by: dict[str, list[str]] = {}
    for r in rows:
        by.setdefault(Path(r.get("path") or "?").name, []).append(
            (r.get("name") or "").strip() or f'line {r.get("line") or "?"}')
    return "; ".join(f"{f}: {', '.join(ns)}" for f, ns in by.items())


def render_test_ledger(rows, root: Path) -> tuple[str, int]:
    """What the change set did to the tests, as one line of counts — `(html, how many it
    moved)`: `+54 −9 ✏️7`, each sign's meaning on its hover, and the untouched rest counted
    on the line's.

    It used to fold open into the grouped list of every moved test. Victor dropped the
    fold (5 Oct 2026): on the requirements map it sits on the covering card, whose rows
    already list the tests, so the list was the same names a second time. The two groups
    no list on the page shows — tests that stopped running and tests that are gone, which
    run nothing and so cover nothing — keep their names, on their count's hover.
    `root` is unused since the list went; the callers still pass it.
    """
    off, new, gone, edited = [], [], [], []
    untouched = 0
    for r in rows:
        if r.get("silenced") and r["status"] != "deleted":
            off.append(r)
        elif r["status"] == "added":
            new.append(r)
        elif r["status"] == "deleted":
            gone.append(r)
        elif r["status"] == "modified":
            edited.append(r)
        else:
            untouched += 1

    moved = len(off) + len(new) + len(gone) + len(edited)
    if not moved and not untouched:
        return "", 0
    # The three moves are their signs and their counts only, `+54 −9 ✏️7` (Victor, 5 Oct
    # 2026): on the covering card's header strip the words and the dots made the line
    # longer than the title it sits beside. The word each sign stands for is on its hover.
    def count(cls: str, face: str, items: list, what: str, named: bool = False) -> str:
        n = len(items)
        tip = f"{n} {what}" if n != 1 else f"1 {what.replace('tests', 'test')}"
        if named:
            tip += ": " + _names_by_file(items)
        return (f'<span class="{cls}" data-tip="{html.escape(tip, quote=True)}">'
                f'{face}{n}</span>') if n else ""
    # Three slots, an empty one kept, so the chapters' counts under it (testchapters.js)
    # line up with these column by column (Victor, 14 Oct 2026). `+` last, and a blank
    # fourth: a chapter writes its total there with its `+N` right in front of it, the
    # Complexity tab's `+1 12` (Victor, 19 Oct 2026).
    slots = (count("removed", "−", gone, "tests gone", named=True),
             count("changed", PENCIL, edited, "tests edited"),
             count("added", "+", new, "new tests"))
    moves = (f'<span class="tmoves">{"".join(x or "<span></span>" for x in slots)}'
             '<span></span></span>' if any(slots) else "")
    stopped = ""
    if off:
        why = f"Still written; never runs: {_names_by_file(off)}"
        stopped = (f'<span class="toff" data-tip="{html.escape(why, quote=True)}">'
                   f'{len(off)} stopped running</span>')
    # No dot between them: the counts sit at the row's right end, under the chapters'
    # columns, and "stopped running" at its left (CSS), so a dot would float between.
    face = "".join(x for x in (stopped, moves) if x) or "no test moved"
    rest = (f'{untouched} more test{"s" if untouched != 1 else ""} in the files this '
            "change set touched, left exactly as they were") if untouched else ""
    tip = f' data-tip="{html.escape(rest, quote=True)}"' if rest else ""
    return f'<p class="tledger" id="test-ledger"{tip}>{face}</p>', moved


def _ms(value) -> str:
    """`1658` → `1.7s`. Under a second stays in milliseconds: a step that took 43ms and
    one that took 430ms are a different kind of fast, and `0.0s` says neither."""
    ms = int(value or 0)
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def render_traces(doc: dict, root: Path, out_dir: Path,
                  touched: set[tuple[str, int]] | None = None) -> tuple[str, int]:
    """What the run recorded, as a registry the 🎭 on the covering-tests rows reads.

    Nothing visible. There used to be a list here — "Step through what the tests did",
    one collapsible row per recording with the viewer framed inside it — and every word
    on it was already on the covering-tests map above: the test's title, its file and
    line, whether it passed. The one thing the row added was the way into the recording,
    and that is now the 🎭 itself: served, it opens the viewer in a window of its own,
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
    # `cd <repo> && …`, and an absolute zip, which is the contract every other command
    # this page hands out already keeps. This one used to be
    # `npx playwright show-trace .human-review/assets/traces/011-….zip` — relative to a
    # directory the line does not name, so it only worked if the reader happened to be
    # standing in the repository root, and said nothing if they were not. The `cd` is not
    # redundant beside the absolute path either: `npx` resolves `playwright` out of the
    # project's own `node_modules`, so the command has to run inside the project whatever
    # the zip is called.
    #
    # Not in `.actions.json`, though, and that is deliberate: the manifest is the list of
    # things the *server* may be asked to run, and `show-trace` opens a desktop window.
    # Served, the 🎭 has a better answer anyway — the trace viewer copied beside this page,
    # in a browser window of its own — so the command exists for exactly the reader who
    # has no server to ask.
    home = shlex.quote(str(root.resolve()))
    entries = []
    for t in tests:
        if not t.get("trace"):
            continue
        key = Path(t.get("file", "")).name + (f':{t["line"]}' if t.get("line") else "")
        zip_path = shlex.quote(str((out_dir / t["trace"]).resolve()))
        entries.append({"test": key, "trace": t["trace"], "status": t.get("status", ""),
                        "cmd": f"cd {home} && npx playwright show-trace {zip_path}"})
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
           + f', {t["modified"]} edited'
           # Untouched itself, edited through a same-file helper it calls.
           + (f' ({t["viaHelper"]} via a helper)' if t.get("viaHelper") else "")
           + f', {gone}'
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
#   * the **UI/API/unit key** goes on the title row, over the card it explains, level
#     with the ticket's title — the one stretch of that row that was empty — and each of
#     its three words becomes a checkbox that filters the card's rows by kind.
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


#: The switch on the title row that turns the ticket's coverage fills on and off. On by
#: default: the reader opens the tab and sees the matrix say what it was built to say; the
#: box is there for the moment they want to read the ticket as the author wrote it, four
#: sentences with no green under them, and then put the fills back.
#: The 🤖 owns the doubt (Victor, 20 Oct 2026): a model's mapping, not exact science.
SEMCOV_LABEL = "Semantic coverage \U0001F916"
#: Its hover (Victor, 5 Oct 2026): the colours it switches are a model's reading of which
#: test proves which claim, and a checkbox named like a metric reads as a measured one.
_SEMCOV_TIP = "Claim ↔ test, as matched by AI"


def semcov_switch() -> str:
    """The `Semantic coverage` checkbox, checked. It sits at the far end of the
    ticket frame's own header strip (`victorrentea opened on …`), on the thing whose
    colouring it switches, rather than on the title row above the frame."""
    return (f'<label class="rm-semcov" data-tip="{html.escape(_SEMCOV_TIP, quote=True)}">'
            f'<input type="checkbox" checked> {SEMCOV_LABEL}</label>')


#: What the card's own header strip says, whatever the model wrote there. It said
#: *Covering tests — as matched by AI*, which put the doubt on the wrong word: the tests on
#: the card are real, they resolve in the tree and `test-changes.py` stamps what happened
#: to each. What a model decided is only the *pairing* — which test pins which sentence of
#: the ticket. So the strip names the pairing, and the robot at its left end owns that and
#: nothing more. "Tests executing code changed by this PR" is a different list, and a
#: measured one; it is kept for the per-test coverage run that can actually say it.
CARD_WHO = "Semantic test coverage"
CARD_WHEN = "paired with the ticket by AI"
CARD_AI_TIP = ("The tests are real and resolve in the tree; which sentence each one "
               "pins is AI's reading, not a measurement")


def card_head(side: str) -> str:
    """The card's header strip, re-worded — or `side` untouched when it has none.

    Only the first `rm-tkhead` inside `rm-code` is touched, and only its two text spans
    and the robot's hover: the strip's structure and classes stay the model's, because the
    fragment's own stylesheet lays them out."""
    i = _find(side, "rm-code")
    if i is None:
        return side
    j = _find(side[i:], "rm-tkhead")
    if j is None:
        return side
    span = _element(side, i + j)
    if span is None:
        return side
    a, b = span
    strip = side[a:b]
    strip = re.sub(r'(<span class="rm-who"[^>]*>).*?(</span>)',
                   lambda m: m.group(1) + html.escape(CARD_WHO) + m.group(2),
                   strip, count=1, flags=re.S)
    strip = re.sub(r'(<span class="rm-when"[^>]*>).*?(</span>)',
                   lambda m: m.group(1) + html.escape(CARD_WHEN) + m.group(2),
                   strip, count=1, flags=re.S)
    strip = re.sub(r'(class="rm-av rm-av-ai"[^>]*?data-tip=")[^"]*(")',
                   lambda m: m.group(1) + html.escape(CARD_AI_TIP, quote=True) + m.group(2),
                   strip, count=1)
    return side[:a] + strip + side[b:]


# --- the measured column: which tests execute the change ------------------------------
#
# The card on the right used to be the model's: the tests it paired with the ticket's
# sentences, sixteen on the PR it was built for. A pairing says which tests were *meant*
# for the change; `testcov.py` measures which tests *reach* it, per test, and on the same
# PR that is fifty-odd. So when `assets/test-coverage.json` exists the card is drawn from
# it, and the model's pairing shrinks to a chip on the rows it named. The model's own card
# stays in the DOM, hidden: its inline script still wires the ticket's sentences, and a
# script that cannot find its list throws before it gets that far.
#
# The join with the diff is done here, at build time, from each test's raw covered lines.
# A rebuild after the branch moves recounts without re-running a single suite.

#: Where the measurement is read from, relative to the report directory.
COVERAGE_JSON = "assets/test-coverage.json"
#: Said of a change set with no pull request — eval run 10 said "in this PR" over a branch
#: that had none. "PR" only when there is one (`covcard_who`).
#: Victor, 5 Oct 2026: "Tests covering the change set" — "Covering tests" left the reader
#: asking "covering what?".
COVCARD_WHO = "Tests covering the change set"
COVCARD_WHO_PR = "Tests covering the change set"
COV_SHIELD = ('<svg viewBox="0 0 16 16"><path d="M8 .9 14.2 3.2v4.6c0 3.6-2.6 6.4-6.2 7.6C4.4 14.2 '
              '1.8 11.4 1.8 7.8V3.2Z"/><path class="rm-play" d="M6.4 5.1v5.6l4.4-2.8Z"/></svg>')


#: The card title's hover, and its twin while "All tests" is checked (testchapters.js
#: swaps them). No count (Victor, 7 Oct 2026): "Each of the 363 tests ran alone..." read as
#: a statistic to check, when all the hover has to say is where the list comes from.
COVCARD_TIP = "Tests that ran a changed line, as captured by a coverage probe."
COVCARD_TIP_ALL = "All tests, as captured by a coverage probe."


def covcard_tip(doc: dict) -> str:
    """How the card was computed, in one line. `doc` is the measurement the card was drawn
    from; the hover no longer quotes its size."""
    return COVCARD_TIP


#: A changed line counts as "passed through" when more than this share of a suite's
#: reaching tests run it — a getter every GET calls, a component's constructor.
COV_COMMON_SHARE = 0.5
#: …and only in a suite with at least this many reaching tests: with three, "most" is two.
COV_COMMON_MIN = 4
#: Said on the card when there is no measurement, over the model's own list.
COV_NOT_MEASURED = ("Coverage was not measured on this build, so this list is AI's pairing, "
                    "not a run. Configure <code>steps.testcov</code> in human-review.json "
                    "and re-run the tests to see which tests execute the change.")


def covcard_who(spec: dict | None, out_dir: Path) -> str:
    """The card's title: "…in this PR" only when there is a pull request. The build
    decides that once (`review.py:prepare_pr_push` sets `_noPr` from `pr_exists`); a spec
    that went through no build is asked the same question directly."""
    spec = spec or {}
    if "_noPr" in spec:
        has_pr = not spec["_noPr"]
    else:
        from .review import pr_exists
        has_pr = pr_exists(spec, out_dir)
    return COVCARD_WHO_PR if has_pr else COVCARD_WHO


def load_coverage(out_dir: Path, spec: dict | None = None) -> dict | None:
    """`test-coverage.json`, or None when this build has no measurement."""
    rel = (spec or {}).get("testCoverage") or COVERAGE_JSON
    try:
        doc = json.loads((out_dir / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("tests") is not None else None


def coverage_join(doc: dict) -> dict:
    """Each test against the diff: which measurable changed lines it ran, which
    unmeasurable changes it reached through their proxy, and whether it is aimed at the
    change or only passes through it.

    Returns `{"measurable": {file: set}, "total": int, "rows": [...], "gaps": {file: [..]},
    "unmeasurable": [...with "reached"], "quiet": {suite: n}}`. `rows` holds only tests
    that reach something; `quiet` counts, per suite, the measured tests that reach nothing."""
    changed = {f: set(v) for f, v in (doc.get("changed") or {}).items()}
    exe = {f: set(v) for f, v in (doc.get("executable") or {}).items()}
    measurable = {f: changed[f] & exe[f] for f in changed if f in exe and changed[f] & exe[f]}
    unm = [dict(u) for u in doc.get("unmeasurable") or []]
    rows, quiet = [], {}
    for t in doc.get("tests") or []:
        hits = t.get("hits") or {}
        got = {f: sorted(set(hits.get(f, ())) & lines) for f, lines in measurable.items()}
        got = {f: v for f, v in got.items() if v}
        via = [i for i, u in enumerate(unm) if u.get("proxy") and any(
            set(hits.get(f, ())) & set(ls) for f, ls in u["proxy"].items())]
        if not got and not via:
            quiet[t.get("suite", "")] = quiet.get(t.get("suite", ""), 0) + 1
            continue
        rows.append({**t, "changedHits": got, "n": sum(map(len, got.values())), "via": via})
    for i, u in enumerate(unm):
        u["reached"] = sum(1 for r in rows if i in r["via"])
    by_suite: dict[str, list[dict]] = {}
    for r in rows:
        by_suite.setdefault(r.get("suite", ""), []).append(r)
    for suite, rs in by_suite.items():
        counts: dict = {}
        for r in rs:
            for f, ls in r["changedHits"].items():
                for ln in ls:
                    counts[(f, ln)] = counts.get((f, ln), 0) + 1
            for i in r["via"]:
                counts[("via", i)] = counts.get(("via", i), 0) + 1
        common = ({k for k, c in counts.items() if c > COV_COMMON_SHARE * len(rs)}
                  if len(rs) >= COV_COMMON_MIN else set())
        for r in rs:
            own = [(f, ln) for f, ls in r["changedHits"].items() for ln in ls] \
                + [("via", i) for i in r["via"]]
            r["aimed"] = any(k not in common for k in own)
    run = {f: set() for f in measurable}
    for r in rows:
        for f, ls in r["changedHits"].items():
            run[f].update(ls)
    gaps = {f: sorted(measurable[f] - run[f]) for f in measurable if measurable[f] - run[f]}
    _rendered_templates(doc, measurable, rows, gaps, unm)
    return {"measurable": measurable, "total": sum(map(len, measurable.values())),
            "rows": rows, "gaps": gaps, "unmeasurable": unm, "quiet": quiet}


#: Said of a changed template line no browser test ran, in a component a Karma spec
#: rendered. Eval run 8 listed owner-list.component.html:23 — the `#ownersError` alert —
#: under "Changed lines no test runs" while owner-list.component.spec.ts:212 renders it and
#: asserts its text: Istanbul instruments the component's TypeScript, never its template,
#: so a unit spec runs every template line and is seen running none. Only a browser run
#: maps template lines (`v8-join.js`); where it did not reach one, the honest word is
#: "unseen", not "unrun" — the component's specs are its proxy.
TEMPLATE_UNSEEN = ("template — its component's unit specs render it, but Karma sees "
                   "TypeScript only, not which template lines ran")


def _rendered_templates(doc: dict, measurable: dict, rows: list[dict], gaps: dict,
                        unm: list[dict]) -> None:
    """Move the gap lines of a template whose component a Karma spec ran (`x.html` beside
    `x.ts`) out of `gaps` into `unm`, proxied by the component lines those specs ran."""
    karma: dict[str, set] = {}
    for t in doc.get("tests") or []:
        if t.get("source") == "karma":
            for f, ls in (t.get("hits") or {}).items():
                karma.setdefault(f, set()).update(ls)
    for f in sorted(gaps):
        if not f.endswith(".html"):
            continue
        ts = f[:-len(".html")] + ".ts"
        if not karma.get(ts):
            continue
        lines = gaps.pop(f)
        measurable[f] = measurable[f] - set(lines)
        if not measurable[f]:
            del measurable[f]
        proxy = {ts: sorted(karma[ts])}
        idx = len(unm)
        unm.append({"file": f, "lines": lines, "reason": TEMPLATE_UNSEEN, "proxy": proxy})
        for r in rows:
            if set((r.get("hits") or {}).get(ts, ())) & karma[ts]:
                r["via"].append(idx)
        unm[idx]["reached"] = sum(1 for r in rows if idx in r["via"])


def model_pairing(frag: str) -> dict:
    """The model's pairing, out of the matrix's own data: `{test key: {"title", "sids"}}`
    plus the ticket's sentences as `{"sentences": {sid: text}}` under the key `""`."""
    m = re.search(r'<script type="application/json" class="rm-data">(.*?)</script>', frag, re.S)
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:
        data = {}
    out = {k: {"title": t.get("title", ""), "sids": []} for k, t in (data.get("tests") or {}).items()}
    for sid, s in (data.get("sentences") or {}).items():
        for g in s.get("groups") or []:
            for ev in g.get("tests") or []:
                if ev.get("id") in out and sid not in out[ev["id"]]["sids"]:
                    out[ev["id"]]["sids"].append(sid)
    texts = {}
    for sm in re.finditer(r'<span class="rm-f" data-s="([^"]+)"[^>]*>(.*?)</span>', frag, re.S):
        texts.setdefault(sm.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", sm.group(2))).strip())
    out[""] = {"sentences": texts}
    return out


def _model_key(row: dict, model: dict) -> str | None:
    """The model's key for a measured test: same file, and the same declaration line or
    the same name."""
    f, line, title = row.get("file"), row.get("line"), (row.get("title") or "").strip()
    for key, t in model.items():
        if not key:
            continue
        path, _, ln = key.rpartition(":")
        if path != f:
            continue
        # The same line, or the same name — never "a line or two away": one-line specs sit
        # on consecutive lines, and a tolerance would fold a neighbour into the model's row.
        if line and ln.isdigit() and int(ln) == int(line):
            return key
        if title and t.get("title", "").strip() == title:
            return key
    return None


def _cov_ranges(lines) -> str:
    out, run = [], []
    for n in sorted(set(lines)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def _snippet_module():
    """`extract-snippet.py`, for its highlighter and its brace-matching window."""
    from ..shared.snippets import _extract_module
    return _extract_module()


#: How long a quoted test body may run before it is cut: a test is a screen, not a file.
COV_PART_MAX = 60
_GHERKIN_NEXT = re.compile(r"\s*(Scenario|Rule|Feature|Background|Examples|@)")


def _cov_part(root: Path, file: str, line: int, text: str | None = None,
              href: str | None = None) -> dict | None:
    """The test's own body as one excerpt, in the matrix's `parts` shape — the same
    `{label, href, from, html}` the model writes, so the card draws it the same way:
    annotations above the declaration, down to the brace that closes it (a Gherkin
    scenario down to the next keyword).

    `text` quotes another version of the file than the working tree's — a deleted test's,
    read from the base commit (`semcov.py:base_part`) — and `href` is then where it opens
    ("" for nowhere: a vscode:// into HEAD would open unrelated code)."""
    path = root / file
    try:
        lines = (text if text is not None else path.read_text(encoding="utf-8")).splitlines()
    except OSError:
        return None
    if not 1 <= line <= len(lines):
        return None
    start = line
    while start > 1 and lines[start - 2].strip().startswith("@"):
        start -= 1
    es = _snippet_module()
    if path.suffix == ".feature":
        end = line
        while end < len(lines) and not _GHERKIN_NEXT.match(lines[end]):
            end += 1
    else:
        end = es._closing_line(lines, line, line)
    while end > line and not lines[end - 1].strip():
        end -= 1
    end = min(end, start + COV_PART_MAX - 1)
    body = lines[start - 1:end]
    indents = [len(x) - len(x.lstrip()) for x in body if x.strip()]
    shift = min(indents) if indents else 0
    dedented = [x[shift:] if x.strip() else "" for x in body]
    lexer = es._lexer_for(path, "\n".join(dedented))
    if lexer is not None:
        rendered = es.highlight("\n".join(dedented), lexer,
                                es.HtmlFormatter(nowrap=True)).rstrip("\n").split("\n")
        rendered += [""] * (len(dedented) - len(rendered))
    else:
        rendered = [html.escape(x) for x in dedented]
    return {"label": f"{file}:{start}-{end}",
            "href": f"vscode://file/{path.resolve()}:{line}:1" if href is None else href,
            "from": start, "html": rendered}


#: Which of the card's three kinds a measured test is. A browser run is UI; Karma is a
#: component in isolation; a backend test is API when it goes through the HTTP layer.
_API_MARKERS = re.compile(r"MockMvc|TestRestTemplate|RestAssured|WebTestClient")


def _cov_cat(r: dict, root: Path) -> str:
    src = r.get("source") or ""
    if "v8" in src:
        return "e2e"
    if src == "karma":
        return "unit"
    if (r.get("suite") or "").endswith("Cucumber"):
        return "api"
    try:
        text = (root / (r.get("file") or "")).read_text(encoding="utf-8")
    except OSError:
        return "unit"
    return "api" if _API_MARKERS.search(text) else "unit"


#: The id of the JSON block `all_tests_inventory` writes and testchapters.js reads.
ALL_TESTS_ID = "rm-all-tests"


def all_tests_inventory(frag: str, doc: dict | None, root: Path) -> str:
    """Every test the coverage run executed that the card does not already list, as the
    JSON block the card's "All tests" checkbox draws from (testchapters.js).

    The inventory is the one the build already has: `test-coverage.json` ran each test
    alone under a probe, so its `tests` are every test that ran, with suite, file and line.
    Name, kind (`_cov_cat`, the same call that badges the card's own rows), file and the
    file's `vscode://` link - nothing to preview, so no source. Data, not markup: the rows
    are built in the browser on the first check, because a big repo runs thousands of tests
    and nobody has asked to see them yet. Empty when there was no measurement."""
    if not doc:
        return ""
    on_card: set[str] = set()
    m = re.search(r'<script type="application/json" class="rm-data">(.*?)</script>', frag, re.S)
    if m:
        try:
            on_card = {k.replace("@base", "") for k in (json.loads(m.group(1)).get("tests") or {})}
        except ValueError:
            pass
    seen, out = set(on_card), []
    for r in doc.get("tests") or []:
        file, line = r.get("file") or "", r.get("line") or 0
        key = f"{file}:{line}" if file and line else f"?{r.get('suite')}:{r.get('title')}"
        if key in seen:
            continue
        seen.add(key)
        t = {"title": r.get("title") or key, "cat": _cov_cat(r, root), "file": file, "line": line}
        if file and line:
            t["href"] = f"vscode://file/{(root / file).resolve()}:{line}:1"
        out.append(t)
    out.sort(key=lambda t: (t["file"], t["line"], t["title"]))
    body = json.dumps({"tests": out}, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="{ALL_TESTS_ID}">{body}</script>'


#: The id of the JSON block `test_cover_files` writes and testchapters.js reads.
TEST_COVER_ID = "rm-test-cover"
#: How many files the shield's popover names before it says "+N more".
TEST_COVER_MAX = 12


def test_cover_files(frag: str, doc: dict | None, root: Path) -> str:
    """For each test on the card, the production files its own run executed: what the row's
    🛡 answers on hover (Victor, 7 Oct 2026: "what part of the code is this test really
    covering?").

    Read off the same per-test probe the card was drawn from (`hits`: file -> lines run).
    Only the files the branch changed, each with how many of its changed lines the test
    ran, most first. The unchanged files it also ran were listed under them ("Also runs")
    until Victor dropped them (19 Oct 2026): an end-to-end run touches half the frontend,
    and the shield's question is which *changed* code. Capped at `TEST_COVER_MAX` names
    with the remainder counted. Each name carries its `vscode://` link at the first changed
    line it ran. A test that reaches the change only through a proxy (`via`) ran no changed
    file and gets no entry: its shield keeps the one-line tip. Empty when there was no
    measurement."""
    if not doc:
        return ""
    m = re.search(r'<script type="application/json" class="rm-data">(.*?)</script>', frag, re.S)
    try:
        on_card = set(json.loads(m.group(1)).get("tests") or {}) if m else set()
    except ValueError:
        on_card = set()
    changed = {f: set(v) for f, v in (doc.get("changed") or {}).items()}
    out: dict[str, dict] = {}
    for r in doc.get("tests") or []:
        key = f'{r.get("file")}:{r.get("line")}'
        if key not in on_card or key in out or not r.get("hits"):
            continue
        rows = []
        for f, lines in r["hits"].items():
            hit = sorted(set(lines) & changed.get(f, set()))
            if hit:
                rows.append((-len(hit), f, hit[0], len(hit)))
        if not rows:
            continue
        rows.sort()
        files = [[f, at, n] for _, f, at, n in rows[:TEST_COVER_MAX]]
        for item in files:
            item.append(f"vscode://file/{(root / item[0]).resolve()}:{item[1]}:1")
        out[key] = {"files": files, "more": max(0, len(rows) - TEST_COVER_MAX)}
    body = json.dumps(out, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="{TEST_COVER_ID}">{body}</script>'


def coverage_tests(frag: str, doc: dict, test_doc: dict | None, root: Path) -> str:
    """The matrix's own test list, grown to every test coverage says runs changed code.

    The card is the model's, and so is its renderer: the UI/API/unit badge, the new/edited
    stamp, the 🎭 replay and the sequence diagram hang off a row by its `file:line` key.
    A test coverage found and the model did not name is added to `rm-data` in the model's
    own shape — its body as the one excerpt — so it is drawn by the same code as every other
    row, not by a second list that looks different and knows none of that."""
    m = re.search(r'(<script type="application/json" class="rm-data">)(.*?)(</script>)',
                  frag, re.S)
    if not m:
        return frag
    try:
        data = json.loads(m.group(2))
    except ValueError:
        return frag
    tests = data.setdefault("tests", {})
    model = model_pairing(frag)
    states = {}
    for t in (test_doc or {}).get("tests") or []:
        states[(t.get("path"), t.get("name"))] = t
        states[(t.get("path"), t.get("line"))] = t
    stamp = {"added": "new", "modified": "changed"}
    for r in coverage_join(doc)["rows"]:
        file, line = r.get("file"), r.get("line")
        if not file or not line or _model_key(r, model):
            continue
        key = f"{file}:{line}"
        if key in tests:
            continue
        part = _cov_part(root, file, int(line))
        state = states.get((file, r.get("title"))) or states.get((file, line)) or {}
        tests[key] = {"title": r.get("title") or key, "cat": _cov_cat(r, root),
                      "status": stamp.get(state.get("status"), "unchanged"),
                      "parts": [part] if part else []}
        if state.get("viaHelper"):
            tests[key].update(status="helper", via=via_helper_tip(state["viaHelper"]))
    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return frag[:m.start(2)] + body + frag[m.end(2):]


def suite_chips(doc: dict) -> str:
    """One muted chip per suite whose coverage is not on the card — stale, skipped, failed —
    the reason on its hover. Eval run 12's Playwright coverage was measured on another
    branch's commit, dropped as stale, and the page never said a suite was missing from the
    right-hand column: a reader took "no UI test runs this line" at face value."""
    chips = []
    for su in doc.get("suites") or []:
        status = su.get("status") or ""
        if status == "ran":
            continue
        why = f'{su.get("name", "")}: {su.get("note") or status}'
        chips.append(f'<span class="cov-suite" data-tip="{html.escape(why, quote=True)}">'
                     f'{html.escape(su.get("name") or "a suite")} coverage: '
                     f'{html.escape(status or "missing")}</span>')
    return f'<p class="cov-suites">{"".join(chips)}</p>' if chips else ""


def coverage_after(doc: dict) -> str:
    """Under the card, a chip for each suite whose coverage is missing or stale
    (`suite_chips`), since every row above it is short by that suite; nothing when all ran.
    The lists of changed lines no test runs and of changes no probe can see used to fold
    here too. Victor dropped both (5 Oct 2026): what they flagged was mostly declarations
    (an NgModule's `declarations: [ … ]`, a local variable), and they cost the column
    height. `coverage_join` still computes them."""
    chips = suite_chips(doc)
    return f'<div class="cov-after">{chips}</div>' if chips else ""


def coverage_side(side: str, frag: str, spec: dict, out_dir: Path, root: Path,
                  generated: bool = False) -> str:
    """The right-hand column: the model's card, retitled for what it lists once coverage
    was measured (its rows are then every test that runs changed code — see
    `coverage_tests`), with the suites it is short of noted under it. With no measurement,
    the model's card as it was, saying it is not one. `root` is unused since the gap lists
    went; the callers still pass it."""
    doc = load_coverage(out_dir, spec)
    if doc is None:
        i = _find(side, "rm-code")
        j = _find(side[i:], "rm-tkhead") if i is not None else None
        span = _element(side, i + j) if j is not None else None
        if span is None:
            return side
        note = COV_NOT_MEASURED_SCRIPTED if generated else COV_NOT_MEASURED
        return side[:span[1]] + f'<p class="cov-none">{note}</p>' + side[span[1]:]
    # No shield before the title (Victor, 20 Oct 2026): the card's choice (testchapters.js)
    # lists the AI-paired tests or all of them as often as the measured ones, so the
    # "measured by a coverage probe" mark no longer speaks for the whole list.
    head = (f'<div class="rm-tkhead"><span class="cov-head" '
            f'data-tip="{html.escape(covcard_tip(doc), quote=True)}">'
            f'<span class="rm-who">{covcard_who(spec, out_dir)}</span></span></div>')
    side = re.sub(r'<div class="rm-tkhead">.*?</div>', lambda _: head, side, count=1, flags=re.S)
    # Last in the column, under the card: the suites missing from it are a footnote to it.
    after = coverage_after(doc)
    close = side.rfind("</div>")
    if close < 0 or not after:
        return side
    return side[:close] + after + side[close:]


#: The matrix's own program: the ticket, the per-test coverage and the pairing in,
#: `assets/requirements-map.html` out. Loaded by path, like `test-changes.py`.
SEMCOV = TESTCHANGES.parent / "semcov.py"
#: Said under the card when the build has no coverage run and the list is the manifest's.
COV_NOT_MEASURED_SCRIPTED = (
    "Coverage was not measured on this build, so this list is the tests this branch "
    "declares (test-changes.py), not a run. Configure <code>steps.testcov</code> in "
    "human-review.json and re-run the tests to see which tests execute the change.")


def _semcov_module():
    import importlib.util
    if "semcov" in sys.modules:
        return sys.modules["semcov"]
    spec = importlib.util.spec_from_file_location("semcov", str(SEMCOV))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["semcov"] = mod
    spec.loader.exec_module(mod)
    return mod


def scripted_reqmap(spec: dict, out_dir: Path, root: Path) -> str | None:
    """Draw the Tests tab's matrix from its inputs, before the layout looks for it.

    The matrix is no longer a model's HTML. `semcov.py` renders it — the ticket's sentences
    on the left, the tests whose coverage runs this PR's changed lines on the right, and
    the pairing between them, scripted where shared evidence decides it and taken from the
    model's `test-mapping.json` where it does not. Only once that file exists: until the
    model half has run, a model-written `requirements-map.html` from an older run is kept
    and pasted as before, and a review with neither gets the layout's "not produced" line.
    Never fatal — a matrix that cannot be drawn is one tab, not the page."""
    if not (out_dir / "test-mapping.json").is_file():
        if (out_dir / "assets" / "requirements-map.html").is_file():
            print("[review] no test-mapping.json: the Tests tab shows the model-written "
                  "requirements-map.html from an older run. rerun-model.py replaces it with "
                  "the scripted matrix.", file=sys.stderr)
        return None
    try:
        said = _semcov_module().write_fragment(spec, out_dir, root)
    except Exception as exc:                      # noqa: BLE001 - one tab, never the page
        print(f"[review] the Tests matrix could not be drawn ({type(exc).__name__}: {exc}); "
              "the previous assets/requirements-map.html, if any, is used.", file=sys.stderr)
        return None
    if said:
        print(f"[review] Tests matrix: {said}", file=sys.stderr)
    return said


def _load_test_changes(spec: dict, out_dir: Path) -> dict | None:
    if not spec.get("testChanges"):
        return None
    try:
        return json.loads((out_dir / spec["testChanges"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def drawn_ticket(frag: str) -> dict | None:
    """The ticket a `semcov.py` matrix was drawn against (its data blob's `ticket`), or
    None — an older fragment, or a model's. The title row then names the same text the
    left column shows, whichever source `semcov.fetch_ticket` took it from."""
    m = re.search(r'<script type="application/json" class="rm-data">(.*?)</script>',
                  frag, re.S)
    try:
        got = json.loads(m.group(1).replace("<\\/", "</")).get("ticket") if m else None
    except (ValueError, AttributeError):
        return None
    return got if isinstance(got, dict) and (got.get("title") or got.get("number")) else None


def ticket_head(ref: dict | None) -> str:
    """The ticket's title over its frame — the issue's own, never the PR's.

    `Issue #37: Link Visit with Vet` — the number rides on the word it numbers, in the
    muted weight. At the end of the title, GitHub's own place for it, it was a long line
    away from the "Issue" it belongs to. Only the number is the link (Victor, 20 Oct 2026:
    "only the number thirty seven should be the link"), as GitHub links `#37` in prose.
    With no ticket resolved the row is still there, empty, so both columns under it keep
    starting level."""
    if ref and ref.get("number") is None and ref.get("title"):
        # Not an issue — the requirement text came from the conversation, an OpenSpec
        # change or the front-matter, and the heading says so rather than invent a number.
        face = f'Requirement: {html.escape(ref["title"])}'
        title = f'<span class="rm-title">{face}</span>'
    elif ref and ref.get("number") is not None:
        # "Issue" first, so the heading says what it names before it names it: over the
        # ticket's own frame, a bare title read as the PR's.
        num = (f'<a class="rm-num" href="{html.escape(ref["url"])}">#{ref["number"]}</a>'
               if ref.get("url") else f'<span class="rm-num">#{ref["number"]}</span>')
        title = (f'<span class="rm-title" data-tip="{html.escape(ref.get("title") or "", quote=True)}">'
                 f'<span class="rm-ref">Issue {num}</span>: '
                 f'{html.escape(ref.get("title") or "")}</span>')
    else:
        title = ""
    # The tab's own first-line title, the shared `h2.tabtitle` (core.css) every tab that
    # opens on one wears, in its size and margins (Victor, 5 Oct 2026).
    return f'<h2 class="tabtitle rm-head">{title}</h2>'


#: The three levels as the page names them. A fragment a model drew says `UI` for the
#: end-to-end level (and `unit` in lower case); the builder says it its own way at build
#: time, so a stale artefact is never regenerated for a word.
CAT_LABELS = {"e2e": "E2E", "api": "API", "unit": "Unit"}


def relabel_cats(frag: str) -> str:
    """Rename the level labels in a matrix fragment: the legend chips and the `cats` map of
    its `rm-data` blob, which the row pills are drawn from."""
    def chip(m: re.Match) -> str:
        return m.group(1) + CAT_LABELS.get(m.group(2), m.group(3)) + "</span>"
    frag = re.sub(r'(<span class="rm-cat" data-cat="([^"]+)"[^>]*>)([^<]*)</span>', chip, frag)
    frag = frag.replace("end to end: clicks the screen", "clicks the screen").replace(
        "clicks the screen", "end to end: clicks the screen")

    def blob(m: re.Match) -> str:
        try:
            data = json.loads(m.group(2).replace("<\\/", "</"))
        except ValueError:
            return m.group(0)
        if isinstance(data.get("cats"), dict):
            data["cats"] = {k: CAT_LABELS.get(k, v) for k, v in data["cats"].items()}
        data["order"] = list(TEST_KIND_ORDER)
        return m.group(1) + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + m.group(3)
    return re.sub(r'(<script type="application/json" class="rm-data">)(.*?)(</script>)', blob, frag,
                  flags=re.S)


#: A test that drew a diagram on the Sequence tab, or has a Playwright recording, is the
#: evidence the page is built around — never one of the "only pass through changed code"
#: rows folded out of sight. An end-to-end run walks the whole stack, so every one of them
#: looks like a pass-through to the coverage join.
TRACED_RANK = 2
TRACED_LABEL = "Traced on the Sequence tab, paired with no sentence"


def promote_traced(page: str) -> str:
    """Give the covering-tests rows that are traced their own group, above the folds.

    Done on the finished page because the two registries that say which tests are traced
    (`hr-genseq`, `hr-traces`) are written by other tabs. The new group takes integer rank 2:
    every rank from 2 up moves one place down, and the fold thresholds with them, so the
    reader (`reqmap.js`) is unchanged. Only the untouched, unpaired ranks are promoted — a
    traced test the branch wrote or edited already has a group that is open."""
    def reg(id_: str):
        m = re.search(r'<script type="application/json" id="%s">(.*?)</script>' % id_, page, re.S)
        try:
            return json.loads(m.group(1).replace("<\\/", "</")) if m else None
        except ValueError:
            return None
    keys: set[str] = set()
    for e in reg("hr-genseq") or []:
        if isinstance(e, dict) and e.get("test"):
            keys.add(e["test"])
            keys.add(e["test"].split("/")[-1])
    for e in (reg("hr-traces") or {}).get("tests", []):
        if isinstance(e, dict) and e.get("test"):
            keys.add(e["test"])
    if not keys:
        return page
    m = re.search(r'(<script type="application/json" class="rm-data">)(.*?)(</script>)', page, re.S)
    if not m:
        return page
    try:
        data = json.loads(m.group(2).replace("<\\/", "</"))
    except ValueError:
        return page
    ranks, tests = data.get("ranks"), data.get("tests")
    if not isinstance(ranks, dict) or not isinstance(tests, dict) or TRACED_LABEL in ranks.values():
        return page
    fold_from = (data.get("fold") or {}).get("from")
    if fold_from is None:
        return page
    traced = [i for i, t in tests.items()
              if t.get("rank", 0) >= fold_from and (i in keys or i.split("/")[-1] in keys)]
    if not traced:
        return page
    shift = lambda r: r + 1 if r >= TRACED_RANK else r
    for t in tests.values():
        if "rank" in t:
            t["rank"] = shift(t["rank"])
    data["ranks"] = {str(shift(int(k))): v for k, v in ranks.items()}
    data["ranks"][str(TRACED_RANK)] = TRACED_LABEL
    data["ranks"] = dict(sorted(data["ranks"].items(), key=lambda kv: int(kv[0])))
    for name in ("fold", "foldOwn", "foldGone"):
        f = data.get(name)
        if isinstance(f, dict):
            for k in ("from", "to"):
                if isinstance(f.get(k), int):
                    f[k] = shift(f[k])
    for i in traced:
        tests[i]["rank"] = TRACED_RANK
    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return page[:m.start(2)] + body + page[m.end(2):]


def all_tests_toggle(cats: str) -> str:
    """The title row over the card, without the Unit/API/E2E filters that stood on it.

    The filters went when the card was split into E2E/API/UNIT chapters (Victor, 7 Oct
    2026): a closed chapter is the filter, and the kind's few words are the chapter's
    subtitle. The switch the card gained, "All tests", sits on the card's own title row
    (testchapters.js puts it there, before the changed-tests counts). The `<p
    class="rm-cats">` stays, empty, because the title row's grid and the adopt line hang
    off it."""
    m = re.match(r'(<p class="rm-cats"[^>]*>)', cats.strip())
    return (m.group(1) if m else '<p class="rm-cats">') + '</p>'


#: The layout above, as the stylesheet that has to hold it. Emitted with the fragment
#: rather than added to `css/tests.css` on purpose: every rule here is scoped to `.reqmap` and
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
   well: the `gap` under it is a gutter between two columns, and inherited downwards it
   opened half a screen between the title and the ticket it names.
   The gutter is the page's own 20px side gutter (`.wrap`'s padding), not the 46px it was
   widened to for the wires (Victor, 7 Oct 2026): two cards a different distance apart
   than they are from the window's edge read as two layouts. The 26px it gives back goes
   to the cards, in the proportion they had at the 1040px body (474 : 520), and they
   share any width the window adds - the gap never grows with it. */
.reqmap .rm-body{display:grid;grid-template-columns:minmax(0,474fr) minmax(0,520fr);
  column-gap:20px;row-gap:0;align-items:start}
.reqmap .rm-text{grid-column:1;grid-row:2}
.reqmap .rm-side{grid-column:2;grid-row:2;max-width:none}
/* Its size, weight and margins are `h2.tabtitle`'s (core.css), the same title every tab
   opens on; this only places it in the grid. */
.reqmap .rm-head{grid-column:1;grid-row:1;display:flex;align-items:center;
  justify-content:space-between;gap:12px;min-width:0}
/* The switch keeps to the far end of the ticket's header strip, and reads in the muted
   weight of a control rather than the weight of the login beside it. */
.reqmap .rm-semcov{flex:0 0 auto;margin-left:auto;display:inline-flex;align-items:center;
  gap:6px;font-size:.85em;font-weight:500;color:var(--muted,#6b6b6b);cursor:pointer;
  user-select:none;white-space:nowrap}
.reqmap .rm-semcov input{margin:0;cursor:pointer}
.reqmap .rm-semcov:hover{color:var(--fg,#1c1c1c)}
/* Unchecked: the ticket as its author wrote it. The fills come off every sentence, and
   the legend under the ticket - which explains nothing once nothing is coloured - keeps
   its height but not its ink, so the card beside it does not jump. */
.reqmap[data-semcov=off] .rm-f[data-cov]{background:none}
.reqmap[data-semcov=off] .rm-legend{visibility:hidden}
/* The title in the heading's ink; only its "#25" links out to the issue, in link blue. */
/* One line, never two (Victor, 20 Oct 2026): a long title ellipsises, its hover keeps it. */
.reqmap .rm-head .rm-title{color:var(--fg,#1c1c1c);flex:0 1 auto;min-width:0;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.reqmap .rm-head .rm-num{color:var(--muted,#6b6b6b);font-weight:400}
.reqmap .rm-head .rm-title .rm-ref .rm-num{color:var(--link);font-weight:inherit;text-decoration:none}
.reqmap .rm-head .rm-title a.rm-num:hover{text-decoration:underline}
/* Both keys now sit under what they explain, so the margin that lifted them off it moves
   to the other side. The shared `--rm-key-h` band goes with them: above the frames it
   kept two unequal rows on one line, and under them there is nothing to keep level. */
.reqmap .rm-legend{min-height:0;margin:10px 2px 0}
/* The Unit/API/E2E key sits on the title row, over the card, in the stretch the title left
   empty; its margins are the title's, so the two read as one line. */
.reqmap .rm-cats{grid-column:2;grid-row:1;align-self:center;min-height:0;margin:.2rem 2px .15rem}
/* The card in three chapters by kind of test (testchapters.js), closed by default: a band
   in its own tint, the caret first in the column the rows keep for their wire button, the
   row's own badge over the rows' badges, the subtitle where the test names start, and the
   chapter's +added / -deleted / edited counts where the card title keeps its own. */
.reqmap .rm-list > .rm-tgroup,.reqmap .rm-list > .rm-fold{display:none}
.reqmap .rm-chh{display:flex;align-items:center;gap:8px;padding:7px 16px 7px 6px;
  font-size:12px;font-weight:600;line-height:1.35;color:var(--fg,#1c1c1c);cursor:pointer;
  user-select:none;border-bottom:1px solid var(--line,#e2e2e2);
  background:linear-gradient(rgba(127,127,127,.12),rgba(127,127,127,.12)),var(--card,#fff)}
.reqmap .rm-chh:hover{
  background:linear-gradient(rgba(127,127,127,.19),rgba(127,127,127,.19)),var(--card,#fff)}
/* The heading of the chapter being read stays on the card's top edge while its rows scroll
   under it, and the next chapter's pushes it off (Victor, 19 Oct 2026: "so I always know
   what category I'm looking at"). Opaque, its tint laid over the card's own colour, or the
   rows would show through it. */
.reqmap .rm-chh{position:sticky;top:0;z-index:2}
/* While a sentence is picked: how many of this kind's tests cover it, `none` in red. */
.reqmap .rm-chhit{flex:0 0 auto;font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--rm-g-edge);white-space:nowrap;cursor:help}
.reqmap .rm-chhit[hidden]{display:none}
.reqmap .rm-chhit{display:inline-flex;align-items:center;gap:3px}
.reqmap .rm-chhit .rm-fan{display:inline-block;width:10px;height:12px}
.reqmap .rm-ch[data-hits="0"] .rm-chhit{color:var(--rm-r-edge)}
.reqmap .rm-ch[data-hits="0"] > .rm-chh .rm-chsub{opacity:.55}
.reqmap .rm-chh .rm-cat{display:inline-flex;justify-content:center;min-width:4.5em}
.reqmap .rm-chh .rm-chev{flex:0 0 auto;font-size:13px!important;line-height:1;opacity:1;
  color:var(--muted,#6b6b6b);
  transition:transform .15s ease}
.reqmap .rm-chh .rm-chcaret{width:12px;text-align:center;margin-right:-4px}
/* A row wears no badge: the chapter heading over it already says the kind, and the badge
   took 53px of every row from the test's name (Victor, 19 Oct 2026). The name wears the
   kind's colour instead - the badge's own ink, E2E's orange darkened to read as text. The
   badge stays in the markup, hidden: the selection still marks how hard a test pins the
   picked sentence on it (`data-strength`), and the name says it now - dashed underline
   for a test that only runs through the sentence, dotted for one that only touches it. */
.reqmap{--rm-e2e-ink:#b4580a}
@media (prefers-color-scheme:dark){.reqmap{--rm-e2e-ink:#ec9a52}}
.reqmap .rm-t .rm-thead > .rm-cat{display:none}
.reqmap .rm-t[data-cat=e2e] .rm-tt{color:var(--rm-e2e-ink)}
.reqmap .rm-t[data-cat=api] .rm-tt{color:var(--rm-api-fg)}
.reqmap .rm-t[data-cat=unit] .rm-tt{color:var(--rm-unit-fg)}
.reqmap .rm-thead:has(> .rm-cat[data-strength=exercised]) .rm-tt{
  text-decoration:underline dashed 1px;text-underline-offset:3px}
.reqmap .rm-thead:has(> .rm-cat[data-strength=touched]) .rm-tt{
  text-decoration:underline dotted 1px;text-underline-offset:3px}
/* The DB-fixture dot (fixtures.js) slides right, into the blank the file column keeps on a
   `.java` row - the column is as wide as `.feature` - so it sits by the file it is about
   and the name gets the room (Victor, 19 Oct 2026). Only where the link says `.java`: a
   longer file name has no blank to slide into (`data-clash`, testchapters.js). */
.reqmap .rm-thead > .fx-dot:has(+ .rm-tw[data-face=".java"]:not([data-clash])){
  margin-right:-21px;position:relative;z-index:1}
.reqmap .rm-ch[data-open=yes] > .rm-chh .rm-chcaret{transform:rotate(90deg)}
.reqmap .rm-chsub{flex:1 1 auto;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* The counts are a grid of four fixed columns, `−`, ✏️, `+` and the chapter's total,
   right-aligned and empty when zero, here and in the card title's `.tmoves` alike (whose
   fourth is blank): the numbers stand in columns. The `+N` right before the total, both in
   the Complexity tab's faces - `.cx-badge` and `.cx-n`, `+1 12` (Victor, 19 Oct 2026). */
.reqmap .rm-chled,.reqmap .rm-code > .rm-tkhead .tmoves{flex:0 0 auto;display:inline-grid;
  grid-template-columns:repeat(3,43px) 34px;gap:0;justify-items:end;align-items:baseline;
  font-size:13px;font-weight:400;font-variant-numeric:tabular-nums;white-space:nowrap}
.reqmap .rm-chled > span{font:700 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace}
.reqmap .rm-chled > .rm-chn{font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--fg,#1c1c1c)}
.reqmap .rm-chled [data-tip]{cursor:help}
/* The `−N` opens the list of the tests it counts (testchapters.js `goneHtml`). */
.reqmap .rm-chled > .removed[data-gone]{cursor:pointer}
.reqmap .rm-ch[data-open=no] > .rm-chb{display:none}
/* Where a picked sentence's wire runs off the card's top or bottom edge (reqmap.js
   `drawSel`): `▲ 3` - three more of its tests that way. */
.reqmap .rm-wires .rm-edge rect{fill:var(--card,#fff);stroke:var(--rm-g-edge);stroke-width:1}
.reqmap .rm-wires .rm-edge text{fill:var(--rm-g-edge);font:700 10px/1 ui-monospace,SFMono-Regular,
  Menlo,monospace;text-anchor:middle}
/* A wire button whose test pins no sentence (testchapters.js `quiet`): still out of sight,
   as reqmap.css hides it, but its slot answers a hover with why - a faint fan and a tip. */
.reqmap .rm-code .rm-link[aria-disabled=true]{visibility:visible;opacity:0;cursor:help}
.reqmap .rm-code .rm-link[aria-disabled=true]:hover{opacity:.35;color:var(--muted,#6b6b6b)}
.reqmap .rm-ch:last-child .rm-chb > .rm-t:last-child,
.reqmap .rm-ch:last-child .rm-cho > .rm-t:last-child{border-bottom:0}
.reqmap .rm-chb > .rm-t:last-of-type{border-bottom:1px solid var(--line,#e2e2e2)}
/* The card's title strip, and the ticket's beside it so the two stay level, a fifth
   shorter (Victor, 7 Oct 2026): 41px to 34px. */
.reqmap .rm-ticket > .rm-tkhead,.reqmap .rm-code > .rm-tkhead{padding-top:4.5px;padding-bottom:4.5px}
/* The shield's popover (testchapters.js): the page tip's dark bubble, but it takes the
   pointer, so the files in it can be clicked. Outside `.reqmap`: it hangs off <body>. */
.rm-covpop{position:fixed;z-index:9999;background:#141416;color:#fff;max-width:26rem;
  padding:.5rem .8rem .6rem;border-radius:.6rem;box-shadow:0 10px 30px rgba(0,0,0,.35);
  font:400 13px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;display:none}
.rm-covpop.on{display:block}
.rm-covpop .rm-cph{margin:.35rem 0 .1rem;font-size:12px;opacity:.72}
.rm-covpop .rm-cph:first-child{margin-top:0}
.rm-covpop ul.tiplist{margin:0;padding-left:1.1rem;list-style:disc;
  font:400 13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
.rm-covpop a{color:#8ab4f8;text-decoration:none;cursor:pointer}
.rm-covpop a:hover{text-decoration:underline}
.rm-covpop .rm-cpn{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
  font-size:12px;opacity:.72}
.rm-covpop .rm-cpm{margin:.3rem 0 0;font-size:12px;opacity:.72}
/* "Tests: [covering the change set ▾]" (testchapters.js): the title's tail is the choice
   of which tests the card lists, in the title's own type. A row the choice leaves out is
   `data-off`; the rest of the run's rows exist only under "all". */
.reqmap .rm-list:not([data-all=yes]) .rm-cho{display:none}
.reqmap .rm-list .rm-t[data-off=yes]{display:none}
.reqmap .rm-code > .rm-tkhead > .rm-mode{flex:0 0 auto;min-width:0;align-self:center;
  margin-left:0;padding:1px 4px;font:inherit;font-weight:600;color:var(--fg,#1c1c1c);
  background:transparent;border:1px solid var(--border,#d0d7de);border-radius:6px;
  cursor:pointer;text-overflow:ellipsis}
.reqmap .rm-code > .rm-tkhead > .rm-mode:hover{border-color:var(--link)}
/* The choice starts where the rows' content does, level with the chapters' carets: the
   strip's 16px padding, the emptied title before it and its own margin had set it 24px in
   (Victor, 29 Oct 2026: "move it to the left"). What it means is its hover, not a line
   beside it (testchapters.js `modeTip`). */
.reqmap .rm-code > .rm-tkhead:has(> .rm-mode){padding-left:6px}
.reqmap .rm-code > .rm-tkhead:has(> .rm-mode) > .cov-head{display:none}
.reqmap .rm-code > .rm-tkhead > .rm-mode option{background:var(--bg,#fff);color:var(--fg,#1c1c1c)}
/* The grand total closes the title row, in the chapters' four columns and right above
   them. The strip's own 16px right padding is the headings', so the columns stand one
   over the other. */
.reqmap .rm-code > .rm-tkhead > .rm-grand{flex:0 0 auto;margin-left:auto;align-self:center}
.reqmap .rm-grand > .rm-chn{font-weight:700}
/* One row, always (Victor, 19 Oct 2026): on a narrow card the totals wrapped under the
   title. The title gives way instead, cut with an ellipsis; its hover keeps the rest. A
   page with the changed-tests ledger keeps the wrap that ledger's own row needs. */
.reqmap .rm-code > .rm-tkhead:not(:has(> .tledger .toff)){flex-wrap:nowrap}
/* The grand total says the ledger's `− ✏️ +` again, one row up (Victor, 20 Oct 2026: "why
   do I see these numbers twice?"): under it the ledger keeps only "N stopped running",
   and with nothing of that goes altogether. */
.reqmap .rm-code > .rm-tkhead:has(> .rm-grand) > .tledger .tmoves{display:none}
.reqmap .rm-code > .rm-tkhead:has(> .rm-grand) > .tledger:not(:has(.toff)){display:none}
.reqmap .rm-code > .rm-tkhead > .cov-head{flex:0 0 auto}
/* "Tests:" is never cut (Victor, 20 Oct 2026): on a narrow card the choice gives way. */
.reqmap .rm-code > .rm-tkhead > .cov-head .rm-who{overflow:visible;text-overflow:clip}
.reqmap .rm-code > .rm-tkhead > .cov-head .rm-who{min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
/* The counts take a row of their own, right-aligned, so their three columns end where the
   chapters' do, one column over the other (Victor, 14 Oct 2026). */
.reqmap .rm-code > .rm-tkhead > .tledger{flex:1 0 100%;margin:2px 0 0}
.reqmap .rm-code > .rm-tkhead .tmoves{margin-left:auto}
.reqmap .rm-code > .rm-tkhead .tmoves > :empty{display:block}
/* A row of the rest of the run has no preview and no stamp: both slots kept, empty. */
.reqmap .rm-st-none{width:15px;height:15px}
.reqmap .rm-other .rm-thead{cursor:default}
/* Two columns, two scrollbars (Victor, 4 Oct 2026): the ticket and the test list each
   scroll on their own, so a sentence and the tests that cover it can be read side by side
   however far apart they are. Clicking either brings the other side's matches into view
   (reqmap.js `bringIn`). Wide screens only; stacked, the page scrolls both. An open test
   no longer unpins its column: it scrolls inside it.
   And only those two (Victor, 5 Oct 2026): the matrix is exactly one window tall, from
   the masthead's bottom edge to the window's, so the page itself has nothing left to
   scroll but the footer under the fold. The panes used to be sticky and capped at the
   window, which left the title row, the adopt line and the footer to an outer bar — a
   third scrollbar, moving the two pinned ones under the masthead. The body is a grid of
   the title row and a 1fr row the panes stretch into; `--strip-h` is the masthead's own
   measured height (tabs.js), and the panel starts right under it. `100vh` is said first
   for a browser without `dvh`.
   And the footer is in the window too (Victor, 7 Oct 2026): left under the fold it was
   81px of page scroll - a third scrollbar again, for two lines nobody came to the tab
   for. `--rm-tail-h` is everything the page lays out under the matrix (the footer, its
   margin, `.wrap`'s bottom padding), measured by REQMAP_FIT_JS, so the matrix plus the
   tail is exactly the window at any height. */
@media (min-width:901px){
  .reqmap .rm-body{height:calc(100vh - var(--strip-h, 7rem) - var(--rm-tail-h, 0px));
    height:calc(100dvh - var(--strip-h, 7rem) - var(--rm-tail-h, 0px));
    grid-template-rows:auto minmax(0,1fr)}
  .reqmap .rm-text,.reqmap .rm-side,.reqmap .rm-side:has(.rm-t[data-open=yes]){
    position:static;align-self:stretch;min-height:0;display:flex;flex-direction:column;
    overflow:hidden}
  /* What scrolls is under each pane's header strip, not the pane (Victor, 5 Oct 2026):
     the author row over the ticket and the card's title row stand still, and each
     scrollbar starts at the strip's bottom edge instead of running up beside it. A frame
     takes the height its content asks for, up to what the pane has left once the keys
     under it are laid out; its strip keeps its own, and the body under the strip — the
     ticket's prose, the card's list — is what gives and scrolls. `clip` keeps the round
     corners without making the frame a scroller of its own. */
  .reqmap .rm-ticket,.reqmap .rm-code{flex:0 1 auto;min-height:0;display:flex;
    flex-direction:column;overflow:clip}
  .reqmap .rm-ticket > .rm-tkhead,.reqmap .rm-code > .rm-tkhead,.reqmap .rm-ticket > .rm-src,
  .reqmap .rm-legend{flex:none}
  .reqmap .rm-issue,.reqmap .rm-list{flex:1 1 auto;min-height:0;overflow-y:auto;
    overscroll-behavior:contain}
  /* The two scrollbars take no width and push nothing left (Victor, 19 Oct 2026: "the
     screen is very crowded"): the native one is hidden and rmscroll.js draws a thin thumb
     over the content - out of sight until the pane is pointed at (faint) or scrolled. */
  .reqmap .rm-ticket,.reqmap .rm-code{position:relative}
  .reqmap .rm-ovs{scrollbar-width:none}
  .reqmap .rm-ovs::-webkit-scrollbar{display:none}
  .reqmap .rm-ovbar{position:absolute;right:2px;width:6px;border-radius:3px;z-index:3;
    background:rgba(127,127,127,.75);opacity:0;transition:opacity .25s ease}
  .reqmap .rm-ovbar.near{opacity:.3}
  .reqmap .rm-ovbar.on,.reqmap .rm-ovbar:hover{opacity:.7}
  /* The blind-spot box under the ticket and the suite chips under the card can each be
     long; they scroll inside a cap rather than squeeze the list above them to nothing. */
  .reqmap .rm-gap,.reqmap .rm-side > .cov-after{flex:none;max-height:35vh;overflow-y:auto}
}
/* The changed-tests counts (`+54 −9 ✏️7`) sit on the card's title row, at its right end. */
.reqmap .rm-code > .rm-tkhead{flex-wrap:wrap}
.reqmap .rm-code > .rm-tkhead > .tledger{font-weight:400}
/* Stacked, the grid is one column: title, ticket, card. The gutter the two columns shared
   becomes the gap between them, which `row-gap:0` above gave up for the title's sake. */
@media (max-width:900px){
  .reqmap .rm-body{grid-template-columns:1fr}
  .reqmap .rm-head,.reqmap .rm-text,.reqmap .rm-cats,.reqmap .rm-side{grid-column:1;
    grid-row:auto}
  .reqmap .rm-cats{margin:46px 2px 10px}
  /* Stacked, the list is not a scroller and the page is: a heading stuck to the window's
     top would hide under the masthead, so it stays in the flow. */
  .reqmap .rm-chh{position:static}
}
/* The fragment's own stylesheet is written by a model and has, so far, only ever been a
   light one: ink #1b1f23, ticket and card on #f6f8fa, pastel status chips. On a dark page
   that was a white ticket beside a white card, and every requirement sentence and test
   file name in near-black on the near-black page. Its class names are fixed by the
   prompt (and by `reqmap_layout`, which finds its footing by them), so the dark skin is
   put back here, after the fragment, on those names and on the page's own tokens. Light
   mode is left exactly as the model drew it; dark mode is the page's, whatever the model
   wrote. */
@media (prefers-color-scheme:dark){
  .reqmap,.reqmap .rm-who{color:var(--fg)}
  .reqmap .rm-ticket,.reqmap .rm-code{background:var(--card);border-color:var(--line)}
  .reqmap .rm-req,.reqmap .rm-t{border-color:var(--line)}
  .reqmap .rm-tkhead,.reqmap .rm-scope,.reqmap .rm-covering-label,.reqmap .rm-note,
  .reqmap .rm-summary-line,.reqmap .rm-tsuite,.reqmap .rm-surf{color:var(--muted)}
  .reqmap .rm-cat{background:var(--code-bg)}
  .reqmap .rm-cat[data-cat=e2e]{background:var(--rm-e2e-bg)}
  .reqmap .rm-none{color:#f08a8a}
  .reqmap .rm-s-asserted{background:#1b2c1f;color:#9ad3a5}
  .reqmap .rm-s-executed{background:#1c2738;color:#9dc0f5}
  .reqmap .rm-s-selected,.reqmap .rm-s-na{background:#24282e;color:#9aa3af}
  .reqmap .rm-s-missing{background:#3a1f1f;color:#f2a0a0}
  .reqmap .rm-req[data-strength="asserted"]{border-left-color:#5fbf7a}
  .reqmap .rm-req[data-strength="executed"]{border-left-color:#8ab4f8}
  .reqmap .rm-req[data-strength="missing"]{border-left-color:#f08a8a}
}
</style>"""


#: Which cells of the matrix are cut to fit, and therefore need somewhere for the rest of
#: the words to live. `.rm-tt` is the covering test's name, and it is the only column in
#: the fragment whose content is a sentence: nine of them were cut on this project's own
#: PR — *"The vet chosen while booking is named everywhere the visit i…"*, 42 % of it on
#: screen — and two UNIT rows for two different components collapsed to nearly the same
#: visible string.
REQMAP_CUT = ".reqmap .rm-tt"

#: The hover on a name the matrix had to cut, measured at the moment it is asked for.
#:
#: Not written into the markup at build time, because the build cannot know: whether a
#: name fits is a question about the reader's window, their font and which column the
#: layout gave it, and the honest answer changes when they drag the window. A `data-tip`
#: stamped on every row regardless would also put a tooltip on the rows that are NOT cut,
#: where it repeats, word for word, the text the pointer is already resting on.
#:
#: Not an observer either. `.rm-tt` lives in a fragment a model renders, inside a tab
#: panel that is `display:none` until the reader opens it — where `scrollWidth` and
#: `clientWidth` are both 0 and nothing looks truncated. Anything measuring ahead of time
#: therefore has to be told when the panel appears, when the fragment's own script has
#: finished writing rows, and when the window resizes; three subscriptions to get one
#: attribute right.
#:
#: So it is measured on the way in. The listener is on `document` in the CAPTURE phase,
#: which is what puts it ahead of `TIP_JS`'s own delegated `pointerover` on the same
#: document — by the time the tooltip asks `closest('[data-tip]')`, the attribute is
#: either there or gone. `focusin` alongside it, for a reader arriving by keyboard.
#: The switch's one line of behaviour: the reqmap wears `data-semcov="off"` while the box is
#: unchecked, and the stylesheet does the rest. Delegated on `document` like the hover, so
#: it survives a fragment's own script rewriting rows under it. Not emitted on the give-up
#: path: the switch is on the row this function draws, and a fragment it declined to
#: rewrite has no such row.
REQMAP_SEMCOV_JS = """
<script data-hr-swap="rerun">(function () {
  // Every script the Tests tab carries runs again when a rebuild swaps the tab into an
  // open served page (`data-hr-swap="rerun"`, `shared/panels.py`): the document-wide
  // listeners below are registered once, the paint is done again over the new markup.
  var first = !window.__hrSemcovOn;
  window.__hrSemcovOn = true;
  if (first) document.addEventListener('change', function (ev) {
    var box = ev.target;
    if (!box || !box.matches || !box.matches('.rm-semcov input')) return;
    var map = box.closest('.reqmap');
    if (!map) return;
    if (box.checked) map.removeAttribute('data-semcov');
    else map.setAttribute('data-semcov', 'off');
  });
  // The stale-pairing pill: its robot presses the Tests tab's own paid rerun (the chip
  // that confirms the spend first) when that chip is live on this page; else tooltip only.
  function aiChip() {
    return document.querySelector('button.chip-rerun-ai[data-tab="requirements"]');
  }
  function live() {
    var c = aiChip();
    return !!c && !c.hidden && !c.disabled && c.getAttribute('aria-disabled') !== 'true';
  }
  function paint() {
    var on = live();
    [].forEach.call(document.querySelectorAll('.rm-stale-badge'), function (b) {
      if (on) b.setAttribute('data-live', ''); else b.removeAttribute('data-live');
    });
  }
  if (first) document.addEventListener('click', function (ev) {
    var go = ev.target && ev.target.closest && ev.target.closest('.rm-stale-ai');
    if (go && live()) aiChip().click();
  });
  function start() {
    paint();
    var c = aiChip();
    if (c && window.MutationObserver)
      new MutationObserver(paint).observe(c, {attributes: true});
    setTimeout(paint, 1500);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();</script>"""

#: The card split into E2E/API/UNIT chapters, and the "All tests" checkbox that adds the
#: rest of the run to them (Victor, 7 Oct 2026). A real file, `assets/testchapters.js`,
#: inlined after the map has drawn its rows: it moves them, the renderer does not know.
REQMAP_CHAPTERS_JS = ('<script data-hr-swap="rerun">'
                      + (Path(__file__).resolve().parent.parent / "assets" / "testchapters.js")
                      .read_text(encoding="utf-8") + "</script>")

#: The two panes' scrollbars drawn over the content instead of beside it (Victor, 19 Oct
#: 2026): `assets/rmscroll.js`, after the chapters so it measures the list they built.
REQMAP_SCROLL_JS = ('<script data-hr-swap="rerun">'
                    + (Path(__file__).resolve().parent.parent / "assets" / "rmscroll.js")
                    .read_text(encoding="utf-8") + "</script>")

#: The changed-tests summary, moved onto the covering card's title row (Victor, 4 Oct
#: 2026): rendered as its own block after the matrix, it floated at the bottom of the left
#: column, far from the list it summarises.
REQMAP_LEDGER_JS = """
<script data-hr-swap="rerun">(function (move) {
  // On DOMContentLoaded: the ledger block is rendered after the matrix, below this script.
  // At once when a rebuild swaps the tab in, which is after the whole panel is there.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', move);
  else move();
})(function () {
  var led = document.getElementById('test-ledger');
  var head = document.querySelector('.reqmap .rm-code > .rm-tkhead');
  if (led && head) head.appendChild(led);
});</script>"""

#: What the page lays out under the matrix, published as `--rm-tail-h` for REQMAP_CSS to
#: take off the window's height — the same bargain as tabs.js's `--strip-h` above it: the
#: script measures, the sheet does the arithmetic. Measured, not a constant, because the
#: footer is not one: it wraps to more lines on a narrow window and its rows depend on how
#: the page is served. The tail is the body's bottom minus the matrix's, which does not
#: depend on the matrix's own height, so setting it cannot feed back into the next
#: measurement. Skipped while the panel is hidden (every box is 0 there) and in show-all
#: (the other panels are under it, and the page scrolls anyway); a ResizeObserver on the
#: matrix fires again the moment the tab is shown, and on the body for the footer.
REQMAP_FIT_JS = """
<script data-hr-swap="rerun">(function () {
  // Run again over a swapped-in tab: the last run's observer and window listeners go first.
  if (window.__hrRmFitOff) { window.__hrRmFitOff(); window.__hrRmFitOff = null; }
  var body = document.querySelector('.reqmap .rm-body');
  if (!body) return;
  var root = document.documentElement;
  function fit() {
    if (!body.getClientRects().length || document.body.classList.contains('showall')) return;
    var tail = document.body.getBoundingClientRect().bottom - body.getBoundingClientRect().bottom;
    // Rounded up: a fraction of a pixel left over is a page scrollbar.
    var v = Math.max(0, Math.ceil(tail)) + 'px';
    if (root.style.getPropertyValue('--rm-tail-h') !== v) root.style.setProperty('--rm-tail-h', v);
  }
  var ro = null;
  if (window.ResizeObserver) {
    ro = new ResizeObserver(fit);
    ro.observe(body);
    ro.observe(document.body);
  }
  window.addEventListener('resize', fit);
  window.addEventListener('load', fit);
  document.addEventListener('DOMContentLoaded', fit);
  if (document.readyState !== 'loading') fit();
  window.__hrRmFitOff = function () {
    if (ro) ro.disconnect();
    window.removeEventListener('resize', fit);
  };
})();</script>"""

REQMAP_TIP_JS = """
<script data-hr-swap="keep">(function () {
  function measure(ev) {
    var el = ev.target && ev.target.closest && ev.target.closest('%s');
    if (!el) return;
    var full = (el.textContent || '').trim();
    // +1: sub-pixel layout makes scrollWidth exceed clientWidth by a fraction on rows
    // that are not cut at all, and a tooltip repeating a name the reader can already
    // read in full is how a page teaches people to stop hovering.
    if (full && el.scrollWidth > el.clientWidth + 1) el.setAttribute('data-tip', full);
    else el.removeAttribute('data-tip');
  }
  document.addEventListener('pointerover', measure, true);
  document.addEventListener('focusin', measure, true);
})();</script>""" % REQMAP_CUT


def gap_under_card(text_col: str, side_col: str) -> tuple[str, str]:
    """The blind-spot box (`.rm-gap`), out of the ticket's column and under the card.

    It says what no test shows (Victor, 19 Oct 2026: "this panel should show up on the
    other side"): a finding about the tests, so it stands under the tests, right after the
    card and before the suites it is short of. Optional - a fragment without the box keeps
    its two columns as they are, rather than giving the whole layout up over it."""
    cut = _take(text_col, "rm-gap")
    if not cut:
        return text_col, side_col
    rest, gap = cut
    end = side_col.find("</aside>")
    if end < 0:
        return text_col, side_col
    end += len("</aside>")
    return rest, side_col[:end] + gap + side_col[end:]


def reqmap_layout(frag: str, spec: dict, out_dir: Path, root: Path | None = None) -> str:
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
        # The layout is abandoned; the hover is not. It hangs off a class name the
        # fragment's own stylesheet declares, so it keeps working on a matrix this
        # function no longer recognises — which is exactly the matrix whose names are
        # most likely to be cut somewhere new.
        return frag + REQMAP_TIP_JS

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
    # The switch goes on the ticket frame's header strip, at its far end.
    # The stale-pairing pill (`semcov.render`) comes out of the text column and sits right
    # after the checkbox, in the same strip.
    pill = re.search(r'<span class="rm-stale-badge".*?</button></span>', text_col, re.S)
    if pill:
        text_col = text_col.replace(pill.group(0), "", 1)
    text_col = re.sub(r'(<div class="rm-tkhead">.*?)(</div>)',
                      lambda h: h.group(1) + semcov_switch() + (pill.group(0) if pill else "")
                      + h.group(2),
                      text_col, count=1, flags=re.S)
    # A matrix `semcov.py` drew already says what its card lists; only a model's is reworded.
    generated = 'data-generated="semcov"' in frag
    if not generated:
        side_col = card_head(side_col)
    side_col = coverage_side(side_col, frag, spec, out_dir,
                             root if root is not None else out_dir.resolve().parent,
                             generated=generated)
    text_col, side_col = gap_under_card(text_col, side_col)
    ref = (generated and drawn_ticket(frag)) or ticket_ref(spec, out_dir)
    body = (m.group(0) + ticket_head(ref)
            + text_col + all_tests_toggle(cats) + side_col + "</div>")
    out = frag[:a] + body + frag[b:]
    doc = load_coverage(out_dir, spec)
    if doc is not None:
        out = coverage_tests(out, doc, _load_test_changes(spec, out_dir),
                             root if root is not None else out_dir.resolve().parent)
    out = relabel_cats(out)
    at = root if root is not None else out_dir.resolve().parent
    inventory = all_tests_inventory(out, doc, at) + test_cover_files(out, doc, at)
    # The matrix's own renderer (`reqmap/reqmap.js`) re-renders a fresh matrix and may run
    # again (`data-hr-swap="rerun"`); semcov.py says so, and a fragment written before it
    # did is told here.
    out = re.sub(r"<script>(\s*// The Tests tab's matrix renderer\.)", r'<script data-hr-swap="rerun">\1',
                 out, count=1)
    return (out + REQMAP_CSS + inventory + REQMAP_FIT_JS + REQMAP_TIP_JS + REQMAP_SEMCOV_JS
            + REQMAP_CHAPTERS_JS + REQMAP_LEDGER_JS + REQMAP_SCROLL_JS)


# --- the third run mode: run the tests, then re-derive ---------------------------------
#
# The ↻ beside the Tests pill re-reads what is on disk (`test-changes.py` over the tree),
# and the ↻+🤖💸 buys the matrix again from a model. Neither re-runs a test, and a list of
# the tests that execute this branch's code can only be answered by running them: whatever
# the page last recorded is a claim about the tree as it was at that run. So this tab has
# a third press, free and slow — every producer that feeds the tab, the suite-running ones
# included, with the step cache bypassed so a suite whose inputs look unchanged runs anyway.
#
# The declaration lives here because *which* producers are "the tests" is this tab's
# knowledge. Drawing the button beside the other two, routing it through the server's
# rerun lock and teaching the page's rerun machine its id belong to the shared modules
# (`shared/commands.py:tab_rerun_html`, `shared/actions.py`, `assets/server.js`,
# `serve-review.py:tab_rerun_plan`); this is the half they call.

#: The manifest id of the third press. A prefix of its own rather than a flag on
#: `__rerun__:<tab>`: the server reaches a verb by its URL or its id, never by a field a
#: caller could flip, and "run the whole e2e suite" is a different offer from "re-read".
RUN_TESTS_ACTION = "__rerun_tests__"


def run_tests_steps(skill_dir: Path) -> list[str]:
    """Every `run-steps.STEPS` producer that feeds this tab, heavy ones included — in the
    table's own order. `[]` when the table cannot be read."""
    from ..shared.actions import _load
    try:
        table = _load(skill_dir / "run-steps.py", "hr_run_steps_table_tests").STEPS
    except Exception:              # noqa: BLE001 - no table, no third press
        return []
    return [row[0] for row in table
            if LEDGER_TAB in [t.strip() for t in (row[1] or "").split(",")]]


def declare_run_tests_rerun(root: Path, out_dir: Path, skill_dir: Path, *,
                            tab: str = LEDGER_TAB, then: tuple[str, ...] = (),
                            label: str = "Re-run the tests, then re-derive the Tests tab",
                            tip: str = "Re-run the tests. Free, takes minutes, needs the app "
                                       "running.") -> dict | None:
    """Declare `__rerun_tests__:<tab>` in the page's action manifest and return
    `{"id", "steps", "tip", "tab", "label"}` for the button — or None where the page
    cannot offer it.

    `tab` and `then` are for another tab whose evidence is made OF the tests: the Code City
    colours itself with the coverage they measured, so its ⏳ is this same press with the
    city's own producer appended (`tabs/city.py`) — one runner, which tests it runs decided
    here and nowhere else.

    `--force`, because the point of the press is that the suite RUNS: `run-steps.py` would
    otherwise find `traces` unchanged since its last run and hand back the old recordings.
    Only the producers are forced; the build after them keeps its own caches."""
    from ..shared.actions import declare_action, tab_rerun_id
    refresh = skill_dir / "refresh-report.py"
    if not refresh.is_file():
        return None
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return None
    steps = run_tests_steps(skill_dir)
    if not steps:
        return None
    steps += [s for s in then if s not in steps]
    here = shlex.quote(str(root.resolve()))
    line = (f"{rebuild_interpreter()} {shlex.quote(str(refresh))} "
            f"--dir {shlex.quote(rel)} --steps {shlex.quote(','.join(steps))} --force --no-serve")
    action = declare_action(tab_rerun_id(RUN_TESTS_ACTION, tab), f"cd {here} && {line}",
                            reload=True, label=label)
    return {"id": action, "steps": steps, "tip": tip, "tab": tab, "label": label}


def run_tests_button(info: dict | None) -> str:
    """The third button, in the free ↻'s own markup so the page's rerun machine drives it:
    `↺⏳`, hidden until the server's probe says it can run it. Empty without `info`."""
    if not info:
        return ""
    steps = html.escape(",".join(info["steps"]), quote=True)
    tab = html.escape(info.get("tab") or LEDGER_TAB, quote=True)
    label = html.escape(info.get("label") or "Re-run the tests, then re-derive the Tests tab",
                        quote=True)
    return ('<button type="button" class="chip chip-rerun chip-served tabrerun tabrerun-tests" '
            f'hidden aria-disabled="true" data-rerun="{RUN_TESTS_ACTION}" '
            f'data-tab="{tab}" data-steps="{steps}" '
            f'aria-label="{label}" '
            f'data-tip="{html.escape(info["tip"], quote=True)}">'
            # ↺⏳ in one button, the masthead's own face for the same press: regenerate,
            # and wait for the suites.
            + RUN_TESTS_FACE + '</button>')
