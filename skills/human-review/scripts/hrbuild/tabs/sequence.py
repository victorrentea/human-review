"""The Sequence tab: a diagram paired with the test that draws it."""
from __future__ import annotations

import functools
import html
import json
import os
import re
import subprocess
from pathlib import Path

from ..shared.diagrams import _context_svg, _source_link, render_diagrams, select_rows
from ..shared.layout import _squash
from ..shared.fixtures import fixture_colors, fixtures_by_test
from ..shared.genseq import (GENSEQ_HANDLE, GENSEQ_OVERLAY, genseq_by_test, genseq_details,
                             genseq_file, pair_anchor, test_of_genseq)
from ..shared.snippets import SNIPPET_BASE, snippet_html
from ..shared.svg import inline_svg
from ..shared.util import TEST_KIND_ORDER

#: The three kinds of test the page names, and what each one is — the Tests tab's own
#: vocabulary, word for word, because a reader who learnt `UI` over there must not have to
#: learn it again here. The words that follow each chip are that tab's legend line; they
#: ride on the hover rather than being printed again under the tab title, since a second
#: copy of a legend is a second place for the two to disagree.
TEST_CATS = {
    "e2e":  ("E2E", "end to end: clicks the screen"),
    "api":  ("API", "REST/MCP"),
    "unit": ("Unit", "one isolated component"),
}

#: What actually ran the test, keyed by the extension of the file the pair quotes.
#:
#: The kind above says which END the run was driven from; it does not say what a reader
#: has to open, or in what language, to change the thing. Two rows on this tab were both
#: `UI` — a Playwright spec and a Cucumber scenario — and nothing on the shut row told
#: them apart, though one of them is the only test on the page written in a language a
#: non-programmer reads.
#:
#: Read off the extension and nothing else, and that is not the same shortcut `_pair_cat`
#: refuses. The kind is a claim about what the run DID, which a path cannot answer. The
#: runner IS the file: `.feature` is Gherkin because Gherkin is what a `.feature` file
#: contains, and no diagram is needed to know it. Longest suffix first, so `.spec.ts` is
#: not read as a bare `.ts`. The Playwright spec says `TypeScript`, the language, to sit
#: beside `Gherkin` and `JUnit` as the thing a reader would have to read, not the tool
#: that runs it; the tool stays in the hover.
TEST_RUNNERS = (
    (".feature", "Gherkin", "a Cucumber scenario"),
    (".spec.ts", "TypeScript", "a Playwright spec, in TypeScript"),
    (".spec.tsx", "TypeScript", "a Playwright spec, in TypeScript"),
    (".spec.js", "TypeScript", "a Playwright spec, in TypeScript"),
    (".java", "JUnit", "a JUnit test"),
    (".kt", "JUnit", "a JUnit test"),
)

#: A lifeline declaration in a generated sequence: `participant Browser`,
#: `actor "A vet" as Vet`, `participant UI as "Pet Clinic UI"`. Only the declarations are
#: read — an arrow can name a lifeline that was never declared, and the ORDER of the
#: declarations is the part that matters here.
SEQ_DECL = re.compile(
    r'^(?:participant|actor|database|queue|collections|boundary|control|entity)\s+'
    r'(?P<first>"[^"]*"|\S+)(?:\s+as\s+(?P<second>"[^"]*"|\S+))?\s*$')

#: The first arrow, for a diagram that declares no lifelines at all — PlantUML lets a
#: sender spring into existence on its first message, and a generator that leans on that
#: still has a driver; it is just never announced.
SEQ_ARROW = re.compile(r'^(?P<from>"[^"]*"|[^\s"<>-]+)\s*-+>+\s*'
                       r'(?P<to>"[^"]*"|[^\s"<>:-]+)\s*:')

#: What a driver lifeline is called when the run went through the screens. The generators
#: name their own driver — Playwright's is `Browser`, a `@SpringBootTest`'s is `Client` —
#: and this page already writes that convention down in `c2-from-sequence.py`. Anything
#: not on this list is a synthetic client calling the contract directly, which is `api`.
SEQ_UI_DRIVERS = re.compile(r"\b(browser|ui|frontend|front-end|chrome|playwright|page)\b",
                            re.I)


def _pair_cat(puml_rel: str, root: Path, authored: str | None = None) -> str | None:
    """Which kind of test drew this sequence: `e2e`, `api` or `unit`.

    Derived from the diagram, not from the file name, and for the same reason the pairing
    itself is derived: a `.spec.ts` is a Playwright run in one module and a component test
    in the next, and the page must not settle that by guessing at a path. The diagram is a
    record of what the run actually did, and its first lifeline is the end the test was
    driving from — `Browser` for a run through the screens, `Client` for a `@SpringBootTest`
    calling the contract in the same JVM. That is the very distinction the tab's own tip
    draws, so reading it off the picture keeps the badge and the tip from ever disagreeing.

    One lifeline is `unit`: a test that made no call anyone else could observe is a test of
    one isolated component, which is exactly what the Tests tab means by the word.

    A diagram that declares nothing is read off its first arrow instead — PlantUML lets a
    sender exist from its first message, and such a diagram has a driver too; it just never
    announced one. A diagram with neither gets no chip rather than a guessed one: an empty
    space says "not classified", and a wrong pill says something false in the page's own
    confident voice.

    `authored` wins when it is given. The classification above is a reading of a generated
    file, and a suite that names its driver something this has never heard of should be
    able to say so in the content file rather than wait for this list to grow.
    """
    if authored in TEST_CATS:
        return authored
    try:
        text = genseq_file(puml_rel, root).read_text(encoding="utf-8")
    except OSError:
        return None
    lifelines = []
    for raw in text.splitlines():
        m = SEQ_DECL.match(raw.strip())
        if not m:
            continue
        first, second = m["first"].strip('"'), (m["second"] or "").strip('"')
        # `participant "Pet Clinic UI" as UI` and `participant UI as "Pet Clinic UI"` both
        # exist; the quoted side is the label whichever order it came in, and with neither
        # quoted PlantUML's own rule applies — the alias is the second.
        label = first if not second or m["first"].startswith('"') else second
        if label not in lifelines:
            lifelines.append(label)
    if not lifelines:
        for raw in text.splitlines():
            m = SEQ_ARROW.match(raw.strip())
            if m:
                lifelines = [m["from"].strip('"'), m["to"].strip('"')]
                break
    if not lifelines:
        return None
    if len(lifelines) == 1:
        return "unit"
    return "e2e" if SEQ_UI_DRIVERS.search(lifelines[0]) else "api"


def _pair_runner(test_rel: str) -> tuple[str, str] | None:
    """`…/book-visit.feature` -> `("Gherkin", "a Cucumber scenario, in Gherkin")`."""
    low = test_rel.lower()
    for suffix, label, what in sorted(TEST_RUNNERS, key=lambda r: -len(r[0])):
        if low.endswith(suffix):
            return label, what
    return None


def _cat_chip(cat: str | None, test_rel: str = "") -> str:
    """The kind of test, and what wrote it: one pill reading `E2E` or `API`.

    The kind alone comes off the Tests tab — same words, same palette, same pill. Copied
    rather than shared, as `reqmap.js` copies the file glyph: the Tests tab is an included
    asset that builds its own markup, and the two will not be made to import from each
    other. What is shared is the decision, which is written down in `TEST_CATS`.

    The language is not in the pill any more: `_lang_label` says it as the file's extension
    at the row's right end, and the runner here only rides the hover."""
    if cat not in TEST_CATS:
        return ""
    label, what = TEST_CATS[cat]
    runner = _pair_runner(test_rel)
    tip = what + (f", {runner[1]}" if runner else "")
    return (f'<span class="testcat" data-cat="{cat}"'
            + (f' data-runner="{html.escape(runner[0].lower(), quote=True)}"' if runner else "")
            + f' data-tip="{html.escape(tip, quote=True)}">{html.escape(label)}</span>')


def _lang_label(test_rel: str, root: Path | None = None, line: int | None = None,
                mark: str = "", lead: str = "") -> str:
    """The test's file name — `OwnerListTest.java`, `owner-search.feature`,
    `add-visit.spec.ts` — at the row's right end, as the way to the code, with the glyph for
    what the branch did to it right beside.

    It used to be the bare extension here and, inside the pair, a "Show Test" fold quoting
    the test under a bar that named the file again with its line range. The page does not
    render the code any more: a reader who wants the test opens it where they read code,
    and this link is how — at the scenario's own line, which is the line the diagram was
    drawn from. No line numbers on the face: the hover says which line it opens at.

    A `srcref` like every other file reference on the page, so the served page's
    open-in-editor handler on `document` takes it; `SEQFOLD_JS` keeps the click from also
    folding the row. A test not in this checkout has nothing to open and stays plain text.

    `lead` goes first inside the label: the DB fixture dot, which sits just before the file
    name (Victor, 19 Oct 2026) — where the Tests tab's `fixtures.js` puts the same dot, and
    out of the sentence, which reads better without a mark in its middle.
    """
    name = Path(test_rel).name
    if not name:
        return lead
    face = html.escape(name)
    if root is not None and (root / test_rel).is_file():
        at = line or 1
        face = (f'<a class="srcref seqfile" href="vscode://file/{(root / test_rel).resolve()}'
                f':{at}:1" data-tip="Open in VS Code: {html.escape(name, quote=True)}'
                f' (line {at})">{face}</a>')
    return f'<span class="seqlang">{lead}{face}{mark}</span>'


#: A JUnit scenario's title as the generator writes it: `AddVisitApiTest: adds a visit…`.
#: Only an identifier before the colon counts — a Gherkin or Playwright title is prose, and
#: a colon in prose is not a class.
_CLASS_TITLE = re.compile(r"^([A-Za-z_$][\w$]*):\s+(\S.*)$")


def _pair_title(titles: list[str]) -> str:
    """The row's sentence: the test it runs, without its class, read as words.

    The class is the file already named at the row's right end, so it is dropped (Victor,
    20 Oct 2026: "I can only see the class name in the right hand, so ditch it"), and a bare
    method name is spelled the way `PrettyTestNames` spells one — `create_withoutVet()`
    reads "create without vet", like its neighbour's `adds a visit to an existing pet`.
    Prose titles (E2E scenarios) are left as they were; a colon in prose is not a class."""
    split = [_CLASS_TITLE.match(t) for t in titles]
    if not titles or not all(split):
        return " · ".join(html.escape(t) for t in titles)
    return ('<span class="seqtitle">'
            + " · ".join(html.escape(_test_words(m[2])) for m in split) + '</span>')


#: A method name as JUnit's standard generator prints it: `create_withoutVet()`.
_METHOD_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:\(\))?$")


def _test_words(name: str) -> str:
    """`create_withoutVet()` → `create without vet`: what `PrettyTestNames` (ReplaceUnderscores,
    then the capitals lowered behind a space) would have named it. Anything not a bare
    method name — a `@DisplayName`, a sentence already — is kept as written."""
    if not _METHOD_NAME.match(name):
        return name
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.removesuffix("()").replace("_", " "))
    return " ".join(words.split()).lower()


#: The glyph `extract-snippet.py` puts on a quoted block's source bar for what the branch
#: did to the lines it quotes (`shared/filemark.py`). Lifted off the bar rather than
#: recomputed: only that module knows the window and the base it was measured against.
FILEMARK = re.compile(r'<span class="filemark"[^>]*>.*?</span>', re.S)


#: What `run-steps.py` `_sequence` writes about the tests it traced beyond the tagged ones —
#: the tests this branch wrote or edited, picked from the test manifest (`steps.sequence.
#: select`), and the ones it left out and why.
SEQ_SELECTION = "assets/sequence.selection.json"

#: Why a picture exists, said on its row: `kind` -> (chip face, hover). Eval run 8: the page
#: showed three pictures and never said they were there because somebody had once tagged
#: those tests, nor that the branch's own paging and sorting scenarios carried no tag —
#: which was the whole reason they had none.
SEQ_WHY = {
    "tagged": ("tagged", "Has the tracing tag"),
    # Said as the rule, not as a choice: `select_traced` picks these from the git diff, no
    # model involved, and "decided to" read as if an agent had judged them worth a picture.
    "added": ("new test", "Traced because this branch wrote it (picked from the diff)."),
    "modified": ("edited test", "Traced because this branch edited it (picked from the diff)."),
}


def sequence_selection(out_dir: Path) -> dict | None:
    try:
        doc = json.loads((Path(out_dir) / SEQ_SELECTION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def picked_for(test_rel: str, scenarios, selection: dict | None) -> dict | None:
    """The selection entry this pair's picture was traced for, if it was one.

    By line, or by name: a JVM picture is titled with JUnit's display name
    (`defaultRequest_returnsFirstTen…()`), which begins with the slug of the method the
    manifest names."""
    for t in (selection or {}).get("picked") or []:
        if not isinstance(t, dict) or t.get("path") != test_rel:
            continue
        name = _slug(t.get("name", ""))
        for line, title in scenarios:
            if (t.get("line") and line == t["line"]) or (name and _slug(title).startswith(name)):
                return t
    return None


#: What the ledger says the branch did to a TAGGED test, said after `tagged` on its chip.
#: Eval run 10: 'Opening the owners page…' was written by this branch and also tagged, and
#: its row said only `tagged` while the Tests tab listed it as new — two tabs, two labels.
SEQ_ALSO = {"added": ("new test", "This branch wrote this test."),
            "modified": ("edited test", "This branch edited this test.")}


#: A tagged test the branch neither wrote nor edited, whose file or a module it imports
#: directly the branch did change. Eval run 11: 'Add a visit to an existing pet…' had its
#: add-visit.dsl.ts edited and its picture moved +20/−20, and its row said only `tagged`.
SEQ_TOUCHED = ("touched", "This branch changed: ")


def _why_chip(kind: str | None, also: str | None = None, via: tuple[str, ...] = ()) -> str:
    if kind not in SEQ_WHY:
        return ""
    _, tip = SEQ_WHY[kind]
    # One badge per test, never two: the tracing tag supersedes everything else, so a
    # tagged test is a grey circled `@` whatever the branch did to it. An untagged pick is
    # `+` (the branch wrote it) or `\u270d\ufe0f` (edited; the header's tests chip says it so too).
    if kind == "tagged":
        badge, tip = '<span class="sw-at">@</span>', "Traced because it carries the tracing tag."
    elif kind == "added":
        badge = '<span class="sw-add">+</span>'
    else:
        badge = '<span class="sw-edit">\u270d\ufe0f</span>'
    return (f'<span class="seqwhy" data-why="{kind}"'
            f' data-tip="{html.escape(tip, quote=True)}">{badge}</span>')


def ledger_status(test_rel: str, scenarios, tests) -> str | None:
    """`added` / `modified` when the ledger (`test-changes.json`) says the branch wrote or
    edited one of the scenarios this picture draws — matched by line, or by name the way
    `picked_for` matches a JUnit display name."""
    found = None
    for t in tests or []:
        if not isinstance(t, dict) or t.get("path") != test_rel:
            continue
        if t.get("status") not in SEQ_ALSO:
            continue
        name = _slug(t.get("name", ""))
        for line, title in scenarios:
            if (t.get("line") and line == t["line"]) or (name and _slug(title).startswith(name)):
                if t["status"] == "added":
                    return "added"
                found = "modified"
    return found


@functools.lru_cache(maxsize=None)
def _branch_changed(root: str, base: str) -> frozenset[str]:
    """Every path the branch changed since its merge-base with `base`, work tree included —
    the same comparison `_moved_since_base` makes. Empty when git cannot say."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    mb = git("merge-base", base, "HEAD").stdout.strip()
    if not mb:
        return frozenset()
    r = git("diff", "--name-only", "--no-renames", mb)
    return frozenset(r.stdout.split()) if r.returncode == 0 else frozenset()


_TS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s+|\brequire\()['"](\.{1,2}/[^'"]+)['"]""")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)\s*;", re.M)


def _direct_imports(test_rel: str, root: Path) -> list[str]:
    """The repo files `test_rel` imports itself — one level, never what those import.

    TypeScript/JavaScript: the relative module specifiers, resolved the way Node does.
    Java: each imported class looked up under the test module's `src/test/java` and
    `src/main/java`. A Gherkin file imports nothing."""
    try:
        text = (root / test_rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    found: list[str] = []
    if test_rel.endswith(".java"):
        module = test_rel.split("/src/", 1)[0] + "/src" if "/src/" in test_rel else "src"
        for static, name in _JAVA_IMPORT.findall(text):
            parts = name.split(".")
            if static or parts[-1] == "*":
                parts = parts[:-1]
            for tree in ("test/java", "main/java"):
                rel = f"{module}/{tree}/{'/'.join(parts)}.java"
                if (root / rel).is_file():
                    found.append(rel)
                    break
        return found
    if not re.search(r"\.(?:[cm]?[jt]sx?)$", test_rel):
        return found
    here = Path(test_rel).parent
    for spec in _TS_IMPORT.findall(text):
        base = Path(os.path.normpath(here / spec)).as_posix()
        for cand in (base, *(base + ext for ext in (".ts", ".tsx", ".js", ".mjs")),
                     base + "/index.ts", base + "/index.js"):
            if (root / cand).is_file():
                found.append(cand)
                break
    return found


def touched_via(test_rel: str, root: Path) -> tuple[str, ...]:
    """What the branch changed among the test's own file and the files it imports directly."""
    changed = _branch_changed(str(root), SNIPPET_BASE)
    if not changed:
        return ()
    hits = [test_rel] if test_rel in changed else []
    hits += [f for f in dict.fromkeys(_direct_imports(test_rel, root)) if f in changed]
    return tuple(hits)


def _scenarios_drawn(puml_rel: str, test_rel: str, root: Path) -> list[tuple[int, str]]:
    """Which scenarios this diagram actually drew, as (line, title).

    Read from the committed `.puml` rather than by looking for `@generate_sequence` in the
    test: the tag is a request, the chapter is the record that the request was granted and
    that there is a picture on this page to link to. A scenario the generator skipped has
    no chapter and gets no 🎼. One per file since the generator started drawing a picture
    per scenario — the list survives because a diagram this page was built before that
    still has several, and reading it is how this page keeps working on both.

    Only the test's own handles count, and only on a `title` or `==` line. The same
    `src://` scheme is on every class and endpoint the diagram names, and a line number
    from `OwnerRepository.java` resolved against a feature file would point at nothing."""
    try:
        text = genseq_file(puml_rel, root).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        src = (root / test_rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        src = []
    found = {}
    for line in text.splitlines():
        m = GENSEQ_HANDLE.match(line.strip())
        if m and m["path"] == test_rel:
            title = (m["title"] or "").strip()
            found.setdefault(_current_decl(src, _test_kind(test_rel), int(m["line"]), title),
                             title)
    return sorted(found.items())


def _decl_names(lines: list[str], decl: int, kind: str) -> set[str]:
    """The names (squashed) of the scenario declared on 1-based line `decl`, if one is:
    the Gherkin title, the Playwright title, the Java method and its `@DisplayName`."""
    i = decl - 1
    if not 0 <= i < len(lines):
        return set()
    if kind == "feature":
        m = _FEATURE_DECL.match(lines[i])
        return {_squash(lines[i][m.end():])} if m else set()
    if kind == "ts":
        m = _TS_DECL.match(lines[i]) and _STRINGS.search(" ".join(lines[i:i + 3]))
        return {_squash(m.group(0)[1:-1])} if m else set()
    if kind == "java" and _JAVA_DECL.match(lines[i]) and not re.match(
            r"\s*(?:return|new|throw|else)\b", lines[i]):
        names = {_squash(m[1]) for m in re.finditer(r"(\w+)\s*\(", lines[i])}
        for j in range(max(0, i - 6), i):
            d = re.search(r'@DisplayName\(\s*"((?:\\.|[^"\\])*)"', lines[j])
            if d:
                names.add(_squash(d[1]))
        return names
    return set()


def _current_decl(lines: list[str], kind: str, line: int, title: str) -> int:
    """Where the scenario a diagram recorded at `line` is declared now.

    The diagram keeps the line the test had when it was traced. Six imports added above
    `add-visit.spec.ts`'s one tagged test (19 Oct 2026) moved it from 31 to 37, and the tab
    then showed that test twice: drawn, by its picture's 31, and "no diagram came back", by
    its tag's 37. So the line is checked against the title the diagram also recorded, and
    moved to the declaration that carries that title. A title that matches nothing — the
    scenario was renamed or deleted — keeps the recorded line: never dropped."""
    want = {_squash(title), _squash(title.split(": ", 1)[-1])} - {""}
    if not want or not lines or _decl_names(lines, line, kind) & want:
        return line
    hits = [n for n in range(1, len(lines) + 1) if _decl_names(lines, n, kind) & want]
    return min(hits, key=lambda n: abs(n - line)) if hits else line


def _folded_pair(puml_rel: str, test_rel: str, pieces: list[str],
                 quoted: list[str] = (),
                 scenarios: list[tuple[int, str]] = (),
                 cat: str | None = None, why: str | None = None,
                 also: str | None = None, via: tuple[str, ...] = (),
                 root: Path | None = None, fixture: str = "") -> str:
    """The test and the sequence its run recorded, folded closed together under one row
    that names the test and links to it.

    Closed, because a sequence is tall. One of them is three or four screens of arrows, and
    a tab that opens on four of those opens on a wall: the reader scrolls past pictures
    they did not ask for to find out what is even on the tab. Closed, the tab opens on its
    own table of contents — one line per test — and the reader picks. The folding is done
    by `SEQFOLD_JS` rather than by leaving out this `open`, because the click targets
    inside every diagram are measured with `getBBox()` while the page loads, and
    `getBBox()` inside a closed `<details>` returns zeros; see that script.

    The test is not quoted inside any more (Victor, 7 Oct 2026: "in the spirit of not
    rendering the code ourselves"). There was a "Show Test" fold here with the excerpt and
    a bar naming the file and its lines; the row's right end now names the file and opens
    it at the scenario's line in the editor, with the bar's file glyph beside it. `quoted`
    is still handed in — the rendered excerpts — because that glyph is measured on them.

    The summary names the *scenarios*, not the file — `AddVisitApiTest: remembers the vet
    who attended it`, which is what the generator wrote into the diagram's own title and
    what the Tests tab calls it. The path is not lost: it is the summary's tooltip, and the
    file name is the link at the row's right end. A pair whose generator recorded no
    chapter falls back to the basename, all there is.

    It leads with the kind of test this is, in the Tests tab's own chip. Shut, this tab is
    a list of sentences, and "which of these went through a browser?" was a question
    the reader could only answer by opening each one and looking at the top lifeline —
    which is where the chip reads it from anyway. Leading, not trailing: the chips line up
    into a column the eye can run down, and a kind that arrives after the sentence arrives
    after it was needed.

    `fixture` is the DB fixture dot the test starts from, at the right end just before the
    file name — the Tests tab's own dot (see `render_testpairs`).
    """
    titles = [t for _, t in scenarios if t]
    name = _pair_title(titles) if titles else html.escape(test_rel.rsplit("/", 1)[-1])
    lines = [ln for ln, _ in scenarios if ln]
    mark = next((m.group(0) for q in quoted for m in [FILEMARK.search(q)] if m), "")
    label = _lang_label(test_rel, root, min(lines) if lines else None, mark, lead=fixture)
    return (f'<details class="testpair" open id="{pair_anchor(puml_rel)}"'
            f' data-test="{html.escape(test_rel)}">'
            f'<summary data-tip="{html.escape(test_rel)}">'
            f'{_cat_chip(cat, test_rel)}{_why_chip(why, also, via)}{name}{label}'
            '</summary>'
            + "\n".join(x.strip("\n") for x in pieces)
            + "</details>")


def _line_spans(ref_tail: str) -> list[tuple[int, int]]:
    """`35-48,52-65` → [(35, 48), (52, 65)]; `12` → [(12, 12)].

    One snippet reference can carry several ranges — that is how a test is quoted without
    the forty lines of setup between its two halves. Anything unparseable yields nothing,
    which is the honest answer: a reference this cannot read is a reference that cannot be
    said to cover any scenario."""
    spans = []
    for part in ref_tail.split(","):
        lo, _, hi = part.strip().partition("-")
        try:
            spans.append((int(lo), int(hi or lo)))
        except ValueError:
            continue
    return spans


def _share_excerpts(test_rel: str, entries, snippets, used: set, root: Path):
    """Hand each of one test file's pictures the excerpts that belong to it.

    One diagram per test file needed no sharing: every excerpt quoting the file went under
    the one picture. Per scenario, four pictures of `owner-search.feature` would each have
    repeated the same thirty lines, or — the way it worked out before this — the first
    would have taken all of them and the rest would have shown none.

    The split is derived, not authored. An excerpt is a set of line ranges in the content
    file, and a range that contains a scenario's declaration line is an excerpt *of* that
    scenario; the generator already told us which line each picture starts at. What matches
    nothing — a Background, a set of imports, a helper below the last scenario — goes under
    the first picture, where a reader meets it before the scenarios that use it.

    One excerpt often quotes two scenarios at once (`35-48,52-65` is one snippet in the
    content file, not two), and then it goes under *both* — but narrowed, each pair
    getting only its own scenario's lines. Whole, both pairs quoted `34-64` and both
    opened on the test declared at 34, so the vet's diagram sat beside the other
    scenario's code; and picking one owner instead leaves the other pair claiming its test
    is "not excerpted here", which is false. `_spans_for` does the cutting.

    The excerpts come back as they went in, unrendered: which excerpt belongs to which
    picture is the decision this function exists to make, and it is one a test can check
    by reading refs rather than by searching rendered HTML for line numbers.
    """
    mine = [x for x in snippets if x["ref"].rpartition(":")[0] == test_rel]
    used.update(id(x) for x in mine)
    quoted = {rel: [] for rel, _ in entries}
    lines_of = {rel: [ln for ln, _ in _scenarios_drawn(rel, test_rel, root)]
                for rel, _ in entries}
    extents = _scenario_extents(lines_of)
    first = entries[0][0]
    for x in mine:
        spans = _line_spans(x["ref"].rpartition(":")[2])
        owners = [rel for rel, _ in entries
                  if any(lo <= ln <= hi for lo, hi in spans for ln in lines_of[rel])]
        if len(owners) > 1:
            for owner in owners:
                quoted[owner].append(_narrowed(x, test_rel,
                                               _spans_for(spans, owner, lines_of, extents)))
            continue
        for owner in (owners or [first]):
            quoted[owner].append(x)
    return quoted


def _scenario_extents(lines_of: dict[str, list[int]]) -> dict[str, list[tuple[int, int]]]:
    """Where each picture's scenario stops, in the test file: the line before the next
    scenario starts — whosever it is.

    A test file gives away where a scenario BEGINS and never where it ends: the generator
    records the declaration line, and nothing records the closing brace. The next
    declaration is the only end the page can know, and it is the right one — everything
    between two scenarios is either the first one's body or the second one's lede, and
    which of the two it is, is settled per span by `_spans_for` rather than guessed here.
    The last scenario runs to the end of the file, which is what a helper below it is
    part of as far as a reader scrolling the quote is concerned. The first one runs back
    to line 1 for the same reason and the one `_share_excerpts` already states: whatever
    stands above the first scenario belongs to nobody in particular and is met under the
    first picture."""
    marks = sorted((ln, rel) for rel, lines in lines_of.items() for ln in lines)
    extents: dict[str, list[tuple[int, int]]] = {rel: [] for rel in lines_of}
    for i, (line, rel) in enumerate(marks):
        end = marks[i + 1][0] - 1 if i + 1 < len(marks) else 10 ** 9
        extents[rel].append((1 if i == 0 else line, end))
    return extents


def _spans_for(spans, owner: str, lines_of, extents) -> list[tuple[int, int]]:
    """The part of a shared excerpt that is this picture's own test.

    An excerpt that quotes two tagged tests of one file used to go to both pairs whole, so
    both quoted `34-64` and both opened on the test declared at 34 — the vet's diagram
    sitting beside the other scenario's code, which is the one mistake this tab exists to
    prevent. The excerpt is still shared (the alternative is a pair claiming its test is
    "not excerpted here", which is false), but each side is handed only the lines that are
    its own.

    Per RANGE first, because the content file already draws the line: `34-47,49-64` is two
    ranges around two tests, and the comment on 49-50 that introduces the second one
    travels with it. Only a range that swallows several declarations is cut, and then at
    the next declaration — see `_scenario_extents`. A range holding no declaration at all
    (imports, a Background, a helper) goes to every picture: it is setup both scenarios
    run through, and dropping it from all but one would quote a test without the fixture
    it stands on."""
    mine: list[tuple[int, int]] = []
    for lo, hi in spans:
        inside = [rel for rel in lines_of
                  if any(lo <= ln <= hi for ln in lines_of[rel])]
        if not inside:
            mine.append((lo, hi))
        elif inside == [owner]:
            mine.append((lo, hi))
        elif owner in inside:
            mine += [(max(lo, a), min(hi, b)) for a, b in extents[owner]
                     if max(lo, a) <= min(hi, b)]
    return sorted(mine)


def _narrowed(snippet: dict, test_rel: str, spans) -> dict:
    """The same excerpt, quoting only the lines `_spans_for` left to this pair.

    A copy, never an edit: the snippet dicts belong to the content file and the same one
    is handed to both pairs. An unchanged set of ranges returns the original object, so a
    file with one picture per excerpt goes through this function byte for byte."""
    if not spans:
        return snippet
    tail = ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in spans)
    if tail == snippet["ref"].rpartition(":")[2]:
        return snippet
    return {**snippet, "ref": f"{test_rel}:{tail}"}


def _unquoted_note(test_rel: str, root: Path) -> str:
    """What to say beside a diagram no snippet quotes.

    Silence would read as "this diagram has no test", which is never true — the manifest
    only knows about a diagram *because* a test generated it. The two things that are
    true are said instead, and they are different: nobody chose an excerpt, or the file
    the generator recorded is not in this checkout. The second one is the reader's cue
    that the pairing is real but the source is not here to read."""
    if (root / test_rel).is_file():
        return (f'<p class="testlead"><span>Generated by '
                f'<a class="srcref" href="vscode://file/{(root / test_rel).resolve()}:1:1" '
                f'data-tip="Open in VS Code">{html.escape(test_rel)}</a>, '
                f'not excerpted here.</span></p>')
    return (f'<p class="testlead"><span>Generated by <code>{html.escape(test_rel)}</code>, '
            f'which is not in this checkout — the diagram is the only record of it '
            f'left.</span></p>')


# ── the tests the tab quotes, derived ───────────────────────────────────────────────
#
# Which test to quote beside which picture used to be the content file's `snippets`, typed
# by the model that wrote the review. Eval run 5 (3 Oct 2026) copied them from the reference
# PR: `add-visit.spec.ts:31-43`, `AddVisitApiTest.java:73-98` — line ranges of another branch,
# quoting tests by position. Nothing about that choice needs a model. A picture names the
# scenario that drew it (`title [[src://<test>:<line> …]]`), and a test asks for a picture with
# a tag. So the build reads both: every scenario a diagram names, and every scenario tagged
# for tracing that no diagram names, each quoted from its tag to its last line.

#: A test asking for a sequence: the Gherkin tag, the Java annotation, the Playwright tag
#: (`{tag: [GENERATE_SEQUENCE_TAG]}` or the literal). Read on a line, then placed on the
#: scenario it belongs to — a line that belongs to no scenario (the constant's own
#: declaration, an import, a Javadoc) is dropped there, not here.
GENSEQ_TAG = re.compile(r"^\s*@(?:generate_sequence|GenerateSequence)\b"
                        r"|GENERATE_SEQUENCE_TAG|[\"']@generate_sequence[\"']")
_FEATURE_DECL = re.compile(r"^\s*(?:Scenario(?: Outline| Template)?|Example)\s*:")
_FEATURE_STOP = re.compile(r"^\s*(?:@|Scenario|Example\s*:|Rule\s*:|Background\s*:|Feature\s*:)")
_TS_DECL = re.compile(r"^\s*(?:test|it)(?:\.\w+)?\s*\(")
_JAVA_DECL = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|"
                        r"synchronized)\s+)*[\w<>\[\],.? ]+\s+\w+\s*\(")
_STRINGS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


def _test_kind(rel: str) -> str:
    low = rel.lower()
    return ("feature" if low.endswith(".feature")
            else "java" if low.endswith((".java", ".kt"))
            else "ts" if low.endswith((".ts", ".tsx", ".js", ".mjs")) else "")


def scenario_span(lines: list[str], decl: int, kind: str) -> tuple[int, int]:
    """1-based `(first, last)` of the scenario declared on line `decl`, from its tag.

    From its tag because the tag is why the picture exists: a quote opening below
    `@generate_sequence` or `@GenerateSequence` quotes a test that looks untagged. A Gherkin
    scenario ends before the next scenario, tag or rule; a Java method or a Playwright
    `test(…)` where its brackets close — counted over parentheses and braces together, so
    a `{tag: […]}` options object on the second line does not end the test early."""
    i = decl - 1
    lo = i
    while lo > 0 and lines[lo - 1].strip().startswith("@"):
        lo -= 1
    if kind == "feature":
        hi = i
        for j in range(i + 1, len(lines)):
            if _FEATURE_STOP.match(lines[j]):
                break
            if lines[j].strip() and not lines[j].strip().startswith("#"):
                hi = j
        return lo + 1, hi + 1
    depth, opened = 0, False
    for j in range(i, min(len(lines), i + 400)):
        code = _STRINGS.sub('""', lines[j]).split("//", 1)[0]
        for ch in code:
            if ch in "({":
                depth += 1
                opened = opened or ch == "{"
            elif ch in ")}":
                depth -= 1
                if opened and depth <= 0:
                    return lo + 1, j + 1
    return lo + 1, min(len(lines), i + 60)


def _tagged_decl(lines: list[str], at: int, kind: str) -> int | None:
    """The declaration line (1-based) of the scenario the tag on line `at` (0-based) is on."""
    if kind == "feature":
        for j in range(at + 1, min(len(lines), at + 6)):
            if _FEATURE_DECL.match(lines[j]):
                return j + 1
            if lines[j].strip() and not lines[j].strip().startswith(("@", "#")):
                return None
        return None
    if kind == "java":
        for j in range(at + 1, min(len(lines), at + 12)):
            t = lines[j].strip()
            if not t or t.startswith(("@", "//", "/*", "*")):
                continue
            if re.search(r"\b(?:class|interface|record|enum)\b", t):
                return None            # on the class: every test in it, which is no scenario
            return j + 1 if _JAVA_DECL.match(lines[j]) else None
        return None
    if kind == "ts":
        for j in range(at, max(-1, at - 5), -1):
            if _TS_DECL.match(lines[j]):
                return j + 1
    return None


def tagged_scenarios(root: Path) -> dict[str, list[int]]:
    """`{test: [declaration line, …]}` of every scenario tagged for tracing in the index."""
    found: dict[str, list[int]] = {}
    got = subprocess.run(["git", "-C", str(root), "grep", "-n", "-I", "-E",
                          "@generate_sequence|@GenerateSequence|GENERATE_SEQUENCE_TAG"],
                         capture_output=True, text=True)
    hits: dict[str, list[int]] = {}
    for row in got.stdout.splitlines() if got.returncode == 0 else []:
        rel, _, rest = row.partition(":")
        num, _, text = rest.partition(":")
        if num.isdigit() and _test_kind(rel) and GENSEQ_TAG.search(text) \
                and "/genseq/" not in f"/{rel}":
            hits.setdefault(rel, []).append(int(num) - 1)
    for rel, ats in hits.items():
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        kind = _test_kind(rel)
        decls = {d for d in (_tagged_decl(lines, at, kind) for at in ats) if d}
        if decls:
            found[rel] = sorted(decls)
    return found


def _ref(rel: str, decls, root: Path) -> dict | None:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    spans: list[list[int]] = []
    for lo, hi in sorted(scenario_span(lines, d, _test_kind(rel)) for d in decls
                         if 0 < d <= len(lines)):
        if spans and lo <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    if not spans:
        return None
    return {"ref": f"{rel}:" + ",".join(f"{a}-{b}" if a != b else str(a) for a, b in spans),
            "derived": True}


def derived_snippets(root: Path) -> tuple[list[dict], list[dict]]:
    """`(drawn, undrawn)` excerpts, one per test file each.

    `drawn` quotes every scenario a diagram on disk names, in the file the diagram says drew
    it; `undrawn` every scenario tagged for tracing that no diagram names — the group the
    tab heads "Tagged for tracing, but no diagram yet", each file captioned with why, as far
    as the checkout can tell (`undrawn_reason`). One reference per file, of as many spans as
    it has scenarios, which is the shape `_share_excerpts` hands out."""
    drawn: dict[str, set[int]] = {}
    for test_rel, pumls in genseq_by_test(root).items():
        if not (root / test_rel).is_file():
            continue
        for puml in pumls:
            drawn.setdefault(test_rel, set()).update(
                ln for ln, _ in _scenarios_drawn(puml, test_rel, root))
    undrawn = {rel: [d for d in decls if d not in drawn.get(rel, set())]
               for rel, decls in tagged_scenarios(root).items()}
    pool = [x for x in (_ref(rel, sorted(lines), root)
                        for rel, lines in sorted(drawn.items()) if lines) if x]
    rest = [dict(x, caption=html.escape(undrawn_reason(rel, root)))
            for rel, lines in sorted(undrawn.items()) if lines
            for x in [_ref(rel, lines, root)] if x]
    return pool, rest


def undrawn_reason(test_rel: str, root: Path) -> str:
    """One line on why a tagged scenario has no diagram, as far as the checkout can tell.

    When the tests were last traced: the Sequence step's own copy of its run (`.head`,
    written when it ran), else the last commit of a generated diagram. When the test file
    last changed: now, if it has uncommitted edits, else its last commit."""
    import datetime as dt

    def _git_time(*args: str) -> float | None:
        got = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct", *args],
                             capture_output=True, text=True)
        out = got.stdout.strip()
        return float(out) if got.returncode == 0 and out.isdigit() else None

    try:
        traced = (root / GENSEQ_OVERLAY / ".head").stat().st_mtime
    except OSError:
        traced = _git_time("--", ":(glob)**/*.genseq.puml")
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", test_rel],
                           capture_output=True, text=True).stdout.strip()
    try:
        changed = (root / test_rel).stat().st_mtime if dirty \
            else _git_time("--", test_rel)
    except OSError:
        changed = None
    if traced is None:
        return "Never traced in this checkout."

    def day(t: float, exact: bool) -> str:
        d = dt.datetime.fromtimestamp(t)
        return f"{d.day} {d:%b}" + (f" {d:%H:%M}" if exact else "")

    if changed is not None and changed > traced:
        same = day(changed, False) == day(traced, False)
        return (f"Changed on {day(changed, same)}, after the last traced run "
                f"({day(traced, same)}).")
    return f"The last traced run ({day(traced, False)}) drew no diagram for it."


#: What the layout puts in a Sequence block's `snippets`: derive them (`derived_snippets`).
AUTO_SNIPPETS = {"auto": "genseq"}


#: What `plan` carries in place of a manifest row for a diagram that has no delta drawn
#: for it and is *not* identical to the base. `None` already means "unchanged", and the
#: two must not be told apart by a boolean beside the plan — the plan is what the render
#: loop reads, so the distinction belongs in it.
STALE = object()


@functools.lru_cache(maxsize=None)
def _moved_since_base(rel: str, root: str, base: str) -> bool:
    """Whether the work tree's copy of `rel` is *not* what the review's base ref has.

    `render_testpairs` learns that a diagram changed by finding a row for it in
    `MANIFEST.tsv`, and the manifest is written by a producer — `puml-diff.sh`, run by the
    `diagrams` and `sequence` steps. Absence from it therefore has two meanings that look
    identical from here: the branch really left the diagram alone, or the manifest is
    older than the diagram. The second one happens in the ordinary way of working: the
    page is rebuilt without re-running the producers (`refresh-report.py` runs none by
    default, because most refreshes are a change to the *page*), and any `.genseq.puml`
    the acceptance suite regenerated in the meantime is then described by a manifest that
    predates it. The pair came out pilled UNCHANGED over a picture that differs from the
    base in every lifeline — a page asserting the opposite of what `git diff` says, with
    nothing on it admitting the claim was second-hand.

    So the claim is checked against the repository instead of inferred from an artifact.
    Against the merge-base with the base ref, not its tip, for the same reason
    `puml-diff.sh` uses it: commits that landed on the base after this branch started are
    not this branch's doing.

    `False` is also the answer when git cannot say — no repository, no such base ref, a
    checkout with no commits — and that is deliberate: the fixture-shaped cases are
    exactly the ones where "unchanged" was never a claim about a base to begin with, and
    turning them into a warning would be inventing a problem.
    """
    def git(*args):
        return subprocess.run(["git", "-C", root, *args],
                              capture_output=True, text=True)
    mb = git("merge-base", base, "HEAD").stdout.strip()
    if not mb:
        return False
    # This run's copy, when the Sequence step drew one: the work tree holds the committed
    # bytes again, and it is the re-traced picture the tab is showing.
    traced = genseq_file(rel, Path(root))
    if traced != Path(root) / rel:
        was = git("show", f"{mb}:{rel}")
        return was.returncode != 0 or was.stdout != traced.read_text(encoding="utf-8")
    # `--quiet` exits 1 on a difference, 0 on none — and 128 when git could not look,
    # which must not be read as "it moved".
    r = git("diff", "--quiet", mb, "--", rel)
    return r.returncode == 1


def _stale_sequence(puml_rel: str, test_rel: str, root: Path, out_dir: Path) -> str:
    """The card for a sequence that has no delta on disk and is not identical to the base.

    It cannot be drawn as a delta — nothing rendered one — and it must not be pilled
    UNCHANGED, because the repository says otherwise. What is true is that the evidence is
    out of date, and that the reader can fix it with the button in the masthead, so that
    is what it says, over the picture the work tree actually holds."""
    rel = puml_rel
    cache, why_not = _context_svg(rel, root, out_dir)
    body = f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not
    return (f'<div class="diagram dgm-bare"'
            f' data-test-src="vscode://file/{(root / test_rel).resolve()}:1:1">'
            '<div class="head"><span class="badge sev-med" data-tip="No delta was drawn '
            'for this diagram, but it differs from the base — the diagram manifest is '
            'older than the picture">delta not drawn</span>'
            + _source_link(rel, root) + '</div>'
            '<p class="sub">This sequence differs from the base ref, but no delta was '
            'drawn for it: <code>assets/diagrams/MANIFEST.tsv</code> is older than the '
            'diagram. Press <b>Rerun</b> — or run <code>run-steps.py --only diagrams</code>'
            ' — to redraw it.</p>'
            + genseq_details(rel, root, work_tree=True)
            + body + '</div>')


def _unchanged_sequence(puml_rel: str, test_rel: str, root: Path, out_dir: Path) -> str:
    """The card for a sequence the branch left alone, inside its test's pair.

    The same bare card `render_diagrams` draws for a delta — no title, no provenance line,
    the test's href on the card for the scenario links — with two differences that are
    the whole point: the pill says UNCHANGED in the neutral colour a `puml` context block
    wears, and there is no Diff/New/Old bar, because there is nothing to diff against.
    The picture is drawn from the committed `.puml` by the `puml` block's own renderer,
    and the generator's sidecar rides along so the handles in it still expand."""
    rel = puml_rel
    cache, why_not = _context_svg(rel, root, out_dir)
    body = f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not
    return (f'<div class="diagram dgm-bare"'
            f' data-test-src="vscode://file/{(root / test_rel).resolve()}:1:1">'
            '<div class="head"><span class="badge sev-info">unchanged</span>'
            + _source_link(rel, root) + '</div>'
            + genseq_details(rel, root, work_tree=True)
            + body + '</div>')


#: One trace of the traced run, shot in Grafana while its stack was up (`trace-shot.py`,
#: from `run-steps.py` `_sequence`), and beside it what it shows: which diagram, how many
#: traces that test made. Absent on a run with no Grafana to shoot — the picture is then
#: simply not offered.
TRACE_SHOT = "assets/sequence.trace.png"
TRACE_SHOT_META = "assets/sequence.trace.json"

#: The three steps from a test to the picture of it, for a reader who has never met a
#: trace. Tiny on purpose, and free of the machinery — no script, no file, no command: the
#: reviewer is busy, and what they lack is the two words, not the plumbing.
# The fold's handle, and the whole subtitle: a <details> shows nothing outside its <summary>
# while shut, and Victor wants it to say where the pictures come from — and how many tests
# they cover — without a click. `{tests}` is counted at build time (`trace_how_html`).
TRACE_ANSWER = ('These diagrams were captured from <a href="https://opentelemetry.io/" '
                'target="_blank" rel="noopener">OpenTelemetry</a> traces of {tests}.')
TRACE_HOW = (
    "The tests ran against the real app, with OpenTelemetry tracing switched on.",
    "Each HTTP call and database query was recorded as a <dfn>span</dfn>: who called whom, "
    "and for how long. The spans that one action set off form a <dfn>trace</dfn>.",
    "Each test's traces were drawn as its diagram below.",
)


#: Under the shot: this is telemetry, not a drawing — recorded from the e2e run itself, every
#: request kept. Victor, 8 Oct 2026: readers should relate the arrows to where they came from.
TRACE_SAMPLED = "Recorded while the e2e tests ran, 100% sampled: every request is in it."


def _trace_shot_html(out_dir: Path, root: Path, shown: dict[str, str]) -> str:
    """The trace itself, closing the "How" fold, or "" when this run shot none.

    It names the test the trace belongs to and links to that test's pair when the pair is
    on this tab (`shown`: diagram path -> test file), so the waterfall and the sequence can
    be read side by side. The image links to itself: a trace is wide and dense, and the
    reader who wants it bigger gets the full-resolution file, not a thumbnail."""
    if not (Path(out_dir) / TRACE_SHOT).is_file():
        return ""
    try:
        meta = json.loads((Path(out_dir) / TRACE_SHOT_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    rel = meta.get("diagram") if isinstance(meta.get("diagram"), str) else ""
    many = meta.get("traces") if isinstance(meta.get("traces"), int) else 1
    test = shown.get(rel)
    title = ""
    if test:
        title = next((t for _, t in _scenarios_drawn(rel, test, root) if t), "")
    name = title or (Path(test).name if test else str(meta.get("test") or ""))
    which = (f"One of the {many} traces" if many > 1 else "The trace")
    if test:
        said = (f'{which} of <a href="#{pair_anchor(rel)}">{html.escape(name)}</a>, in '
                "Grafana Tempo. Compare it with that test's diagram below.")
    elif name:
        said = f"{which} of {html.escape(name)}, in Grafana Tempo."
    else:
        said = "A trace of this run, in Grafana Tempo."
    said += " " + TRACE_SAMPLED
    alt = f"A trace in Grafana Tempo{': ' + name if name else ''}"
    # No fold of its own: one inside "How were these captured?" was a second click for a
    # reader who had already asked how — the picture is the answer's last line.
    return ('<div class="seqhow-shot">'
            f'<p class="seqhow-cap">{said}</p>'
            f'<a class="seqhow-img" href="{TRACE_SHOT}" target="_blank" rel="noopener"'
            ' data-tip="Open full size">'
            f'<img src="{TRACE_SHOT}" alt="{html.escape(alt, quote=True)}" loading="lazy">'
            "</a></div>")


def trace_how_html(out_dir: Path, root: Path, shown: dict[str, str], tests: int) -> str:
    """"These diagrams were captured from OpenTelemetry traces of N tests." — shut, as the
    subtitle under the tab's title, and itself the fold's handle. It used to be a question
    with this answer beside it ("How were these captured? From OpenTelemetry recordings of
    end-to-end tests."); Victor, 5 Oct 2026, wanted one sentence, with the count in it.
    `tests` is how many tests the tab's diagrams were drawn from.

    The diagrams on this tab are drawn from OpenTelemetry traces, and nothing on the page
    said so: a reader who does not know what a trace is met a column of arrows with no
    account of where they came from, and even the person who set the pipeline up could not
    tell from the page. Three lines, folded, so the reader who knows pays one line of
    height for it; the picture of a real trace right under them, for whoever wants to see
    the thing the three lines describe."""
    steps = "".join(f"<li>{s}</li>" for s in TRACE_HOW)
    said = TRACE_ANSWER.format(tests=f"{tests} test{'' if tests == 1 else 's'}")
    return ('<details class="seqhow tabsub"><summary>'
            f'<span class="seqhow-ans">{said}</span></summary>'
            f"<ol>{steps}</ol>" + _trace_shot_html(out_dir, root, shown) + "</details>")


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path,
                     test_changes: list | None = None):
    """Each acceptance test next to the sequence its own run recorded.

    They used to be two lists on the same tab — a gallery of diagrams, then a list of test
    snippets — and the reader had to work out which picture belonged to which test from the
    file names. Nothing was hidden and nothing was reliable. The pairing is not a judgement
    call, either: the manifest says which test file each diagram came from, and the
    diagram's own chapter titles say which scenarios inside that file, at which lines. So it
    is derived, not authored.

    Neither side is ever dropped, and neither is ever given a partner it did not produce.
    The manifest lists only the diagrams this branch *changed*, so a quoted test with no
    row in it has two different stories, and they are told apart on disk: its
    `<test>.genseq.puml` is either there, identical to the base — a sequence the branch
    left alone, paired and marked as such, counted as no delta — or it is not there at
    all, and the test goes to a trailing group that says exactly that. The second is the
    more interesting absence, because a tagged test with no recorded trace is a fact about
    the *evidence* rather than a gap in the page; the first used to be filed under it, and
    every such page had to be corrected by hand. A diagram whose test is not quoted, or
    not in this checkout at all, says so under its own heading rather than sitting there
    looking like it came from nowhere."""
    # A diagram the branch DELETED has no picture to pair with a test, and a frame
    # titled by the file it used to be drawn from is the worst of both: it reads as a
    # test, on a tab whose frames ARE the list of tests. It happens whenever a test
    # loses its `@generate_sequence` / `@GenerateSequence` — a fact about the source,
    # visible where the source is quoted rather than as an empty exhibit here.
    rows = [r for r in select_rows(manifest_rows, block)
            if r["kind"] == "sequence" and r.get("status") != "deleted"]
    # Derived when the layout says so (every content file's Sequence block, since run 5);
    # a list is still read as it stands, for a block built by hand or by a test.
    undrawn: list[dict] = []
    if isinstance(block.get("snippets"), dict) and block["snippets"].get("auto") == "genseq":
        snippets, undrawn = derived_snippets(root)
    else:
        snippets = list(block.get("snippets", []))
    parts, used = [], set()
    # The registry the 🎼 on the covering-tests rows reads: one entry per scenario the
    # generator drew, keyed the way that map addresses a row, so the jump is a lookup and
    # not a guess. Filled as the pairs are rendered — a pair that is not on this tab must
    # not be linkable from the other one.
    index = []

    def register(puml_rel, scenarios):
        for line, title in scenarios:
            index.append({"test": f"{test_of_genseq(puml_rel, root)}:{line}",
                          "pair": pair_anchor(puml_rel), "title": title})

    # The DB fixture each test starts from: the Tests tab's dot, from the same verdicts
    # (`shared/fixtures.py`), with its words — the fixture the test declares, or the seed its
    # Flyway config loads; none when it starts on an empty database. Read once, on the
    # first pair. An id is `<test>:<line>`; the diagram's handle names the line it was
    # drawn from, which for a JUnit test may sit below the annotation the verdict is keyed
    # on, so a key only annotations or blank lines away still counts. Nothing is guessed
    # past that: a test the verdicts do not name gets no dot.
    fx: dict = {}

    def fixture_dot(test_rel: str, scenarios) -> str:
        if not fx:
            colours = fixture_colors(root)
            fx["colour"] = lambda n: (colours["seed"] if n == "seed"
                                      else colours["colors"].get(n, colours["seed"]))
            marks: dict[str, dict[int, str]] = {}
            for key, name in fixtures_by_test(root).items():
                f, _, ln = key.rpartition(":")
                marks.setdefault(f, {})[int(ln)] = name
            fx["marks"] = marks
        known = fx["marks"].get(test_rel) or {}
        if not known:
            return ""
        try:
            src = (root / test_rel).read_text(encoding="utf-8").splitlines()
        except OSError:
            src = []

        def mark_at(line):
            if line in known:
                return known[line], line
            for at in sorted(known, key=lambda k: abs(k - line)):
                lo, hi = sorted((at, line))
                if hi - lo > 8 or hi > len(src):
                    break
                if all(not s.strip() or s.strip().startswith("@") for s in src[lo:hi - 1]):
                    return known[at], at
            return None, None

        hits = [mark_at(ln) for ln, _ in scenarios] or [(None, None)]
        if len({got for got, _ in hits}) != 1:
            return ""                   # two scenarios, two starts: not one dot's to say
        name = hits[0][0]
        if name is None:
            return ""                   # starts on an empty database: no dot
        tip = "DB Fixture: " + ("Default" if name == "seed" else name)
        return (f'<span class="fx-dot" role="img"'
                f' aria-label="{html.escape(tip, quote=True)}"'
                f' data-tip="{html.escape(tip, quote=True)}"'
                f' data-fixture="{html.escape(name, quote=True)}"'
                f' style="--fx:{html.escape(fx["colour"](name), quote=True)}"></span>')

    # Every diagram on this tab, in page order, grouped by the test it was drawn from —
    # because the excerpts are quoted per test file and have to be shared out among that
    # file's pictures. A changed one is a manifest row; an unchanged one is a `.puml` on
    # disk that no row mentions, found below.
    plan: dict[str, list[tuple[str, object]]] = {}

    def plan_add(puml_rel, what):
        plan.setdefault(test_of_genseq(puml_rel, root), []).append((puml_rel, what))

    merged = dict(dspec)
    merged.pop("only", None)
    for r in rows:
        plan_add(r["source"], r)

    unchanged = 0
    stale = 0
    drawn = genseq_by_test(root)
    for test_rel in dict.fromkeys(x["ref"].rpartition(":")[0] for x in snippets):
        for rel in drawn.get(test_rel, ()):
            if any(rel == q for q, _ in plan.get(test_rel, [])):
                continue          # this branch changed it: it is already a row above
            # No row, so the manifest says nothing changed here — but the manifest is an
            # artifact of a producer that may not have run since this file did. Ask git
            # before repeating it: a diagram that differs from the base is never
            # "unchanged", whatever an older manifest was told. See `_moved_since_base`.
            moved = _moved_since_base(rel, str(root), SNIPPET_BASE)
            plan_add(rel, STALE if moved else None)
            stale += 1 if moved else 0
            unchanged += 0 if moved else 1

    # An author's say on what kind of test a file holds, keyed by the file the snippet
    # quotes — one kind per test file, which is the grain the content file already writes
    # its references at. Absent, `_pair_cat` reads it off the diagram.
    authored_cat = {x["ref"].rpartition(":")[0]: x.get("cat")
                    for x in snippets if x.get("cat")}
    lost_by_rel = {x["diagram"]: x for x in _lost(sequence_verdict(out_dir))}
    selection = sequence_selection(out_dir)
    picked = (selection or {}).get("picked") or []
    drew: set[int] = set()
    tagged = None
    # The tests the pictures were drawn from, for the subtitle's count: one per scenario a
    # diagram's chapters name, and one for a diagram that names none.
    tests_shown: set[tuple] = set()

    pairs: list[tuple[int, str]] = []
    for test_rel, entries in plan.items():
        quoted_by_pair = _share_excerpts(test_rel, entries, snippets, used, root)
        for puml_rel, row in entries:
            scenarios = _scenarios_drawn(puml_rel, test_rel, root)
            tests_shown.update({(test_rel, ln) for ln, _ in scenarios} or {(puml_rel,)})
            quoted = [snippet_html(x["ref"], x.get("caption"), root)
                      for x in quoted_by_pair[puml_rel]]
            # No lead. The scenario names used to be printed here as deep links, and every
            # one of them was said again a few hundred pixels lower: the diagram's own
            # section headers are those same titles, linked to those same lines, drawn by
            # the generator. Two copies of one list, and the one on the picture is the one
            # that sits where the reader is already looking.
            # The row's file link is the way to the test, so the pair says nothing more
            # about it — unless the file is not in this checkout, which is news.
            pieces = [] if (root / test_rel).is_file() else [_unquoted_note(test_rel, root)]
            if puml_rel in lost_by_rel:
                pieces.append(lost_note_html(lost_by_rel[puml_rel]))
            pieces.append(
                _stale_sequence(puml_rel, test_rel, root, out_dir) if row is STALE
                else render_diagrams(merged, root, out_dir, [row], bare=test_rel)
                if row is not None
                else _unchanged_sequence(puml_rel, test_rel, root, out_dir))
            hit = picked_for(test_rel, scenarios, selection)
            if hit is not None:
                drew.add(picked.index(hit))
                why = hit.get("status")
            else:
                if tagged is None:
                    tagged = tagged_scenarios(root)
                why = ("tagged" if any(ln in tagged.get(test_rel, ()) for ln, _ in scenarios)
                       else None)
            also = ledger_status(test_rel, scenarios, test_changes) if why == "tagged" else None
            via: tuple[str, ...] = ()
            if why == "tagged" and also is None:
                via = touched_via(test_rel, root)
                also = "touched" if via else None
            cat = _pair_cat(puml_rel, root, authored_cat.get(test_rel))
            pairs.append((TEST_KIND_ORDER.index(cat) if cat in TEST_KIND_ORDER
                          else len(TEST_KIND_ORDER),
                          _folded_pair(puml_rel, test_rel, pieces, quoted, scenarios, cat,
                                       why, also, via, root=root,
                                       fixture=fixture_dot(test_rel, scenarios))))
            register(puml_rel, scenarios)
    # By kind, in the Tests card's order (`TEST_KIND_ORDER`); within a kind, as planned.
    parts += [h for _, h in sorted(pairs, key=lambda p: p[0])]

    orphaned = [x for x in snippets if id(x) not in used] + undrawn
    tail = block.get("unpaired") or {}
    if orphaned:
        pieces = [""] + [snippet_html(x["ref"], x.get("caption"), root).strip("\n")
                         for x in orphaned]
        parts.append(
            f'<h3 id="{html.escape(tail.get("id", "tests-nosequence"))}">'
            f'{html.escape(tail.get("title", "Tests that record no sequence"))}</h3>'
            + (f'<p>{tail["body"]}</p>' if tail.get("body") else "")
        )
        parts.append('<div class="testpair">' + "\n".join(pieces) + "</div>")

    band = sequence_verdict_html(out_dir)
    if not parts:
        # A tab with nothing on it but the reason it is empty is still worth its pill: an
        # absent Sequence tab says nothing, and this one says what to start.
        return (band, 1, 1) if band else ("", 0, 0)
    # `"title": ""` means no heading at all, and is worth having: every pair below already
    # names its own scenarios and carries its own source path, so a heading over them can
    # only restate what the tab label said — and it does it above the fold, where the
    # first picture should be. An absent title still gets the default; only an author who
    # typed an empty one is asking for the space back. The default names what the tab is —
    # the label says *Sequence*, and the heading says of what: the tests' own runs.
    title = block.get("title", "Sequence diagrams of tests")
    head = ((f'<h2 class="tabtitle" id="{html.escape(block.get("id", "sequences"))}">'
             f'{html.escape(title)}</h2>') if title else "")
    # Its subtitle: about every picture on the tab, the committed ones a band may be warning
    # about included. Only over pictures — a tab with none has nothing for it to explain,
    # and returned above. With no title it is simply the head's first line.
    if plan:
        head += trace_how_html(out_dir, root, {rel: test_rel for test_rel, entries in plan.items()
                                               for rel, _ in entries}, len(tests_shown))
    head += f'<p>{block["body"]}</p>' if block.get("body") else ""
    # Invisible, and last: nothing to look at, only the map's way back in. `</` cannot
    # appear inside a script element, whatever its type.
    if index:
        parts.append('<script type="application/json" id="hr-genseq">'
                     + json.dumps(index).replace("</", "<\\/") + "</script>")
    # Weight counts every exhibit; changes count only the manifest's rows — an unchanged
    # pair is context, exactly as a `puml` block is, unless the suites were not re-traced:
    # a run that drew nothing cannot know the branch left the sequences alone. The count
    # is a fact about the delta only: the tab holding this block is never struck through
    # whatever it says (`NEVER_STRUCK` in build-review-html.py).
    return (band + "\n".join(([head] if head else []) + parts) + "\n",
            len(rows) + unchanged + stale + len(orphaned),
            len(rows) or int(sequence_verdict_alarm(out_dir) is not None))


#: What `run-steps.py` `_sequence` writes beside the diagrams whenever the traced suites did
#: not simply pass — read here the way the Demo tab reads `feature.verdict.json`.
SEQ_VERDICT = "assets/sequence.verdict.json"

#: `state` -> (band class, headline, what it means for the pictures under it). The classes
#: are the Review tab's bands (`.rband`, `review.css`): the same kind of statement — read
#: everything below differently — in the same shape, rather than a second vocabulary.
SEQ_VERDICT_FACE = {
    "skipped": ("rband-warn", "Not re-traced on this run.",
                "The traced suites drew no diagram, so every sequence on this tab is the "
                "one committed on the branch — not evidence of this run, and possibly "
                "stale against the code under review."),
    "red": ("rband-alert", "The traced suite was red.",
            "The diagrams below come from that run: a scenario that failed is drawn only "
            "as far as it got."),
    "notests": ("rband-none", "Part of the traced run found nothing to run.",
                "A tag filter matched no test — not a failure, and the diagrams below are "
                "this run's."),
    # Eval run 6: the re-traced AddVisitApiTest lost NotificationService, the SMS gateway
    # and both calls — the backend's in-process test could not reach the traced stack's
    # notification-service — and the band above it said "the diagrams below are this
    # run's", with nothing admitting they contradicted the committed ones.
    "degraded": ("rband-warn", "This run's trace lost what the committed diagrams show.",
                 "The diagrams below are this run's, and the ones named here no longer show "
                 "participants or calls their committed version has."),
}

#: `state` -> the words on the tab pill's accessible name when the band is an alarm.
SEQ_VERDICT_ALARM = {"skipped": "not re-traced on this run", "red": "traced suite red",
                     "degraded": "trace lost calls the committed diagrams show"}


def sequence_verdict(out_dir: Path) -> dict | None:
    try:
        doc = json.loads((Path(out_dir) / SEQ_VERDICT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("state") in SEQ_VERDICT_FACE else None


def sequence_verdict_alarm(out_dir: Path) -> str | None:
    """The pill's label when the tab must be read as not this run's evidence, else None.
    A run that LOST calls is one, whatever else it was: a tag filter that matched nothing
    beside it does not make the pictures it degraded trustworthy."""
    doc = sequence_verdict(out_dir) or {}
    return SEQ_VERDICT_ALARM.get(doc.get("state")) or (
        SEQ_VERDICT_ALARM["degraded"] if _lost(doc) else None)


def _lost(doc: dict | None) -> list[dict]:
    """`run-steps.py` `lost_vs_committed`: per re-traced diagram, what its committed copy
    shows and it no longer does."""
    return [x for x in (doc or {}).get("lost") or []
            if isinstance(x, dict) and isinstance(x.get("diagram"), str)
            and (x.get("participants") or x.get("calls"))]


def lost_note_html(entry: dict) -> str:
    """The warning inside one pair whose re-traced diagram lost what the committed one has.

    Neutral about the cause on purpose — the trace cannot tell a call the code stopped
    making from one the traced stack never reached — except when a whole participant is
    gone, which is far more often the stack (a service not started, or not reachable from
    the JVM that ran the test) than a branch deleting a system from its own picture."""
    parts = [str(x) for x in entry.get("participants") or []]
    calls = [str(x) for x in entry.get("calls") or []]
    said = []
    if parts:
        said.append("participant" + ("s " if len(parts) != 1 else " ")
                    + ", ".join(f"<b>{html.escape(x)}</b>" for x in parts))
    if calls:
        said.append(f"{len(calls)} call{'s' if len(calls) != 1 else ''}: "
                    + "; ".join(f"<code>{html.escape(x)}</code>" for x in calls))
    cause = ("A whole participant missing is usually the traced stack — a service that was "
             "not started, or not reachable from the JVM that ran the test — rather than the "
             "code." if parts else
             "Either the code under review stopped making these calls, or the traced stack "
             "did not reach them; the trace alone cannot tell which.")
    return ('<p class="rband rband-warn seqlost" role="note"><b>Lost vs the committed '
            'diagram:</b> this run\'s trace no longer shows ' + " and ".join(said)
            + f". {html.escape(cause)}</p>")


#: `⚠️ "<scenario>": fetch failed — skipped` — a generator naming what it skipped. Read off
#: the log for a verdict written before `run-steps.py` recorded `skips` itself.
_SKIP_LINE = re.compile(r"(?:\"[^\"]*\"|'[^']*')\s*:\s*(?P<why>.*\bskipped\b.*)$", re.I)


def _drew_nothing(r: dict) -> str:
    """`exit 0, and drew no diagram — “fetch failed — skipped” ×3` for a command that
    exited 0 on a run that drew nothing — never `passed`."""
    if r.get("outcome") == "drew-nothing" and r.get("detail"):
        return str(r["detail"])
    skips = r.get("skips")
    if skips is None:
        skips = [m["why"].strip() for m in map(_SKIP_LINE.search, map(str, r.get("log") or []))
                 if m]
    counts: dict[str, int] = {}
    for why in skips:
        counts[why] = counts.get(why, 0) + 1
    said = ", ".join(f"“{w}”" + (f" ×{n}" if n > 1 else "") for w, n in counts.items())
    return "exit 0, and drew no diagram" + (f" — {said}" if said else "")


def _counted(runs: list[dict], state: str) -> str:
    """One plain sentence for the band: how the commands ended, counted, no shell in it."""
    nothing = state == "skipped"
    tally = {"failed": 0, "no-tests": 0, "empty": 0, "passed": 0}
    for r in runs:
        o = r.get("outcome")
        key = ("failed" if o == "failed" else "no-tests" if o == "no-tests"
               else "empty" if o == "drew-nothing" or (o == "ran" and nothing) else "passed")
        tally[key] += 1
    words = [(tally["failed"], "could not run"),
             (tally["no-tests"], "found no test under its tag filter"),
             (tally["empty"], "exited cleanly with nothing to draw"),
             (tally["passed"], "passed")]
    total = len(runs)
    # One command is "The traced command found no test…", never "…command 1 found".
    said = [what if total == 1 else f"{n} {what}" for n, what in words if n]
    if not said:
        return ""
    head = f"Of the {total} traced command{'s' if total != 1 else ''}, " if total > 1 else "The traced command "
    return head + ", ".join(said[:-1]) + (" and " if len(said) > 1 else "") + said[-1] + "."


def sequence_verdict_html(out_dir: Path) -> str:
    """Why the Sequence tab is empty, red or stale, said at its top — or nothing.

    The reason used to exist only as a row of the producers' status table, which reached
    the page if and when a model copied it into the guide, on the Review tab. The Sequence
    tab itself was merely struck through, which reads as "this branch did not touch its
    sequences" — while it was showing committed pictures nobody had re-traced.

    It leads with plain sentences — what the tab is, then how the commands ended, counted —
    and everything in shell (the commands, their exits, their last lines) goes in one fold
    under them. Eval run 5 printed `npm run trace:diagram: passed` in the open for a
    generator that had skipped every scenario and drawn nothing: a command that drew
    nothing is not `passed`, and the raw lines were the first thing a judge read."""
    doc = sequence_verdict(out_dir)
    if not doc:
        return ""
    state = doc["state"]
    cls, title, why = SEQ_VERDICT_FACE[state]
    lost = _lost(doc)
    if lost and state == "notests":
        # The loss leads: a tag filter that matched nothing is said below, as its cause.
        cls, title, why = SEQ_VERDICT_FACE["degraded"]
    parts = [f'<p><b>{html.escape(title)}</b> {html.escape(why)}</p>']
    if lost:
        gone = list(dict.fromkeys(p for x in lost for p in x.get("participants") or []))
        names = ", ".join(Path(x["diagram"]).name.split(".genseq.")[0] for x in lost)
        parts.append(
            '<p class="rb-sub"><b>Lost vs the committed diagrams</b> in ' + html.escape(names)
            + (": " + ", ".join(f"<b>{html.escape(g)}</b>" for g in gone) if gone else "")
            + " — flagged on each picture below. Do not read a missing call as the code "
              "no longer making it until the trace is re-run with the whole stack "
              "reachable.</p>")
    # The plain cause of each command that found nothing, said in the open: Maven prints a
    # tag filter that matched nothing as `[ERROR] … MojoFailureException`, and the fold
    # below would otherwise be a red dump under a reassuring headline (eval run 6).
    for r in doc.get("runs") or []:
        if isinstance(r, dict) and r.get("outcome") == "no-tests" and r.get("detail"):
            parts.append(f'<p class="rb-sub">The cause, in plain words: '
                         f'{html.escape(str(r["detail"]))}. The <code>[ERROR]</code> lines '
                         'in the fold below are that, not a broken test.</p>')
    missing = [m for m in doc.get("missing") or [] if isinstance(m, str)]
    if missing:
        parts.append("<p class=\"rb-sub\">Nothing answers at: "
                     + " · ".join(f"<code>{html.escape(m)}</code>" for m in missing)
                     + ". Start them, or configure <code>steps.sequence.app</code>, and "
                       "rerun this tab.</p>")
    runs = [r for r in doc.get("runs") or [] if isinstance(r, dict)]
    if runs:
        counted = _counted(runs, state)
        if counted:
            parts.append(f'<p class="rb-sub">{html.escape(counted)}</p>')

        def said(r):
            outcome = r.get("outcome")
            text = (_drew_nothing(r) if outcome == "drew-nothing"
                    or (outcome == "ran" and state == "skipped")
                    else "passed" if outcome == "ran"
                    else str(r.get("detail") or "") if outcome == "no-tests"
                    else f'exit {r.get("exit")} — {r.get("detail", "")}')
            return (f'<li><code>{html.escape(str(r.get("command", "")))}</code>: '
                    f'{html.escape(text)}</li>')
        log = [f'$ {r.get("command", "")}\n' + "\n".join(map(str, r.get("log") or []))
               for r in runs if (r.get("outcome") != "ran" or state == "skipped")
               and r.get("log")]
        # Folded, like the recorder's last words over the film: the answer for whoever
        # is fixing the environment, furniture for everybody else.
        parts.append('<details class="toolcommits"><summary>What each command said'
                     '</summary><ul>' + "".join(said(r) for r in runs) + "</ul>"
                     + ('<pre>' + html.escape("\n\n".join(log)) + "</pre>" if log else "")
                     + "</details>")
    elif doc.get("reason") and not missing:
        parts.append(f'<p class="rb-sub">{html.escape(str(doc["reason"]))}</p>')
    role = "alert" if state in SEQ_VERDICT_ALARM or lost else "status"
    cls = "rband-warn" if lost and cls == "rband-none" else cls
    return f'<div class="rband {cls} seqverdict" role="{role}">' + "".join(parts) + "</div>"
