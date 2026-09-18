"""The Sequence tab: a diagram paired with the test that draws it."""
from __future__ import annotations

import functools
import html
import json
import re
import subprocess
from pathlib import Path

from ..shared.diagrams import _context_svg, _source_link, render_diagrams, select_rows
from ..shared.genseq import GENSEQ_HANDLE, genseq_by_test, genseq_details, pair_anchor, test_of_genseq
from ..shared.snippets import SNIPPET_BASE, snippet_html
from ..shared.svg import inline_svg

#: The three kinds of test the page names, and what each one is — the Tests tab's own
#: vocabulary, word for word, because a reader who learnt `UI` over there must not have to
#: learn it again here. The words that follow each chip are that tab's legend line; they
#: ride on the hover rather than being printed again under the tab title, since a second
#: copy of a legend is a second place for the two to disagree.
TEST_CATS = {
    "e2e":  ("UI", "clicks the screen"),
    "api":  ("API", "REST/MCP"),
    "unit": ("unit", "one isolated component"),
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
#: not read as a bare `.ts`.
TEST_RUNNERS = (
    (".feature", "Gherkin", "a Cucumber scenario"),
    (".spec.ts", "Playwright", "a Playwright spec"),
    (".spec.tsx", "Playwright", "a Playwright spec"),
    (".spec.js", "Playwright", "a Playwright spec"),
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
        text = (root / puml_rel).read_text(encoding="utf-8")
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
    """The kind of test, and what wrote it: one pill reading `UI · Gherkin`.

    The kind alone comes off the Tests tab — same words, same palette, same pill. Copied
    rather than shared, like `FILE_PAGE` above it: the Tests tab is an included asset that
    builds its own markup, and the two will not be made to import from each other. What is
    shared is the decision, which is written down in `TEST_CATS`.

    The runner is this tab's own, and it is here because the kind is not enough to place a
    row: `UI` covers both a Playwright spec and a Cucumber feature, and a reader looking
    for the Gherkin scenario had to open every `UI` row to find which one it was. It rides
    INSIDE the same pill rather than beside it in a second one — a second chip is a second
    thing to learn and a second column to line up, for a word that only ever qualifies the
    first. A file whose extension says nothing gets the kind alone, exactly as before."""
    if cat not in TEST_CATS:
        return ""
    label, what = TEST_CATS[cat]
    runner = _pair_runner(test_rel)
    face = f"{label} \u00b7 {runner[0]}" if runner else label
    tip = f"{face} \u2014 {what}" + (f", {runner[1]}" if runner else "")
    return (f'<span class="testcat" data-cat="{cat}"'
            + (f' data-runner="{html.escape(runner[0].lower(), quote=True)}"' if runner else "")
            + f' data-tip="{html.escape(tip, quote=True)}">{html.escape(face)}</span>')


def _scenarios_drawn(puml_rel: str, test_rel: str, root: Path) -> list[tuple[int, str]]:
    """Which scenarios this diagram actually drew, as (line, title).

    Read from the committed `.puml` rather than by looking for `@generate_sequence` in the
    test: the tag is a request, the chapter is the record that the request was granted and
    that there is a picture on this page to link to. A scenario the generator skipped has
    no chapter and gets no 🕵️. One per file since the generator started drawing a picture
    per scenario — the list survives because a diagram this page was built before that
    still has several, and reading it is how this page keeps working on both.

    Only the test's own handles count, and only on a `title` or `==` line. The same
    `src://` scheme is on every class and endpoint the diagram names, and a line number
    from `OwnerRepository.java` resolved against a feature file would point at nothing."""
    try:
        text = (root / puml_rel).read_text(encoding="utf-8")
    except OSError:
        return []
    found = {}
    for line in text.splitlines():
        m = GENSEQ_HANDLE.match(line.strip())
        if m and m["path"] == test_rel:
            found.setdefault(int(m["line"]), (m["title"] or "").strip())
    return sorted(found.items())


#: The Tests tab's file glyph, and the corner mark that says what the branch did to that
#: file. Copied here from `requirements-map.html` deliberately: one vocabulary for "what
#: happened to this file" across the page, drawn the same way in both places, so a reader
#: who has learnt it on one tab is not taught it again on another. A page with a `+` is a
#: file that did not exist, a page with a pencil is one this branch edited, a bare page is
#: one it left alone — and the words those glyphs replace are on the hover, never dropped.
FILE_PAGE = ('<path class="fm-page" d="M9.5 1.1l3.4 3.5.1.4v2h-1V6H8V2H3v11h4v1H2.5l-.5-.5'
             'v-12l.5-.5h6.7l.3.1zM9 2v3h2.9L9 2z"/>')
FILE_PLUS = '<path class="fm-mark" d="M13 16h-1v-3H9v-1h3V9h1v3h3v1h-3v3z"/>'
FILE_PENCIL = ('<path class="fm-mark" d="M8.65 13.65 13.65 8.65 15.55 10.55 10.55 15.55Z'
               'M8.65 13.65 10.55 15.55 7.9 16.3Z"/>'
               '<path class="fm-mark" d="M12.5 9.8 14.4 11.7 13.75 12.35 11.85 10.45Z"/>')

#: `<span class="code-badge" data-diff="new" data-tip="…">new file</span>` — the words
#: `srcbar_html` prints at the end of a source bar.
CODE_BADGE = re.compile(
    r'<span class="code-badge"[^>]*data-tip="(?P<tip>[^"]*)"[^>]*>(?P<label>[^<]*)</span>')


def _badge_as_glyph(bar: str) -> str:
    """The bar's own `new file` / `2 lines changed` badge, drawn instead of spelled.

    `NEW FILE` in caps beside a file name is read before the name is — a label louder than
    its subject, on a row whose subject is the file. The Tests tab settled this one level
    down already: the same page glyph, marked `+` or pencil in its corner, with the words
    it replaces moved into the hover. This is that decision applied to the row the Sequence
    tab puts above a quoted test, and it is the same drawing, not a lookalike.

    Keyed off the badge's words rather than its `data-diff`, exactly as the Tests tab keys
    it: `new file` and `new code` are both `new` to git and are not the same fact — one is
    a file that did not exist, the other is fresh lines inside one that did.
    """
    def swap(m):
        label = m["label"]
        kind = ("new" if label.startswith("new file")
                else "unchanged" if label.startswith("unchanged") else "edited")
        mark = {"new": FILE_PLUS, "edited": FILE_PENCIL, "unchanged": ""}[kind]
        tip = m["tip"]
        return (f'<span class="filemark" data-kind="{kind}" role="img"'
                f' aria-label="{html.escape(label, quote=True)}"'
                f' data-tip="{label[:1].upper()}{label[1:]} &mdash; {tip}">'
                f'<svg viewBox="0 0 16 16" aria-hidden="true">{FILE_PAGE}{mark}</svg></span>')

    return CODE_BADGE.sub(swap, bar, count=1)


#: The header `extract-snippet.py` puts at the top of every quoted block: the two handles,
#: the file name with the lines it quotes, and what changed in it. Matched rather than
#: rebuilt, because only that module knows what the bar says — the window may have snapped
#: past a leading comment, and the badge is computed against the review's own base. It has
#: no nested `<div>`, so the first `</div>` is its own; anything else would need a parser.
SRCBAR = re.compile(r'<div class="srcbar">.*?</div>', re.S)


def _fold_over(quoted: list[str]) -> tuple[str, list[str]]:
    """Move the first quoted block's source bar out of the block and onto the fold's row.

    The row above a quoted test used to read `the test · lines 60–61,70–94,124–155`, and
    the bar immediately below it read `AddVisitApiTest.java:60-61,70-94,124-155`. The same
    line numbers twice, the second time beside the file they belong to — so the first copy
    was saying nothing the second did not say better, and it cost a row on a tab whose
    whole shape is one row per thing.

    What is left of that row is the only thing it ever said that the bar does not: whether
    the test is open. So the control and the bar become one line — `Show Test`, then the
    file, its lines, and what changed in it — and the block underneath keeps the code
    alone. Only the first block's bar moves: a second excerpt of the same file is a
    different window and still has to name itself.
    """
    if not quoted:
        return "", []
    m = SRCBAR.search(quoted[0])
    if not m:
        return "", list(quoted)
    return (_badge_as_glyph(m.group(0)),
            [quoted[0][: m.start()] + quoted[0][m.end():], *quoted[1:]])


def _folded_pair(puml_rel: str, test_rel: str, pieces: list[str],
                 quoted: list[str] = (),
                 scenarios: list[tuple[int, str]] = (),
                 cat: str | None = None) -> str:
    """The test and the sequence its run recorded, foldable together — with the quoted
    test folded closed inside it, and the whole pair folded closed too.

    Closed, because a sequence is tall. One of them is three or four screens of arrows, and
    a tab that opens on four of those opens on a wall: the reader scrolls past pictures
    they did not ask for to find out what is even on the tab. Closed, the tab opens on its
    own table of contents — one line per test — and the reader picks. The folding is done
    by `SEQFOLD_JS` rather than by leaving out this `open`, because the click targets
    inside every diagram are measured with `getBBox()` while the page loads, and
    `getBBox()` inside a closed `<details>` returns zeros; see that script.

    Both halves fold, which is the older correction: the fold used to close over the quoted
    test alone and leave the diagram standing underneath, orphaned. A sequence is a drawing
    of one test — without the test above it, it is a picture of nothing.

    The summary names the *scenarios*, not the file — `AddVisitApiTest: remembers the vet
    who attended it`, which is what the generator wrote into the diagram's own title and
    what the Tests tab calls it. The path is not lost: it is the summary's tooltip, and the
    fold's row under it names the file, the lines it quotes and what changed in them. A
    pair whose generator recorded no chapter falls back to the basename, all there is.

    It leads with the kind of test this is, in the Tests tab's own chip. Shut, this tab is
    a list of sentences, and "which of these went through a browser?" was a question
    the reader could only answer by opening each one and looking at the top lifeline —
    which is where the chip reads it from anyway. Leading, not trailing: the chips line up
    into a column the eye can run down, and a kind that arrives after the sentence arrives
    after it was needed.
    """
    titles = [t for _, t in scenarios if t]
    name = (" · ".join(html.escape(t) for t in titles) if titles
            else html.escape(test_rel.rsplit("/", 1)[-1]))
    src = ""
    bar, blocks = _fold_over(list(quoted))
    if blocks:
        src = ('<details class="testsrc">'
               f'<summary><span class="foldlbl"></span>{bar}</summary>'
               + "\n".join(x.strip("\n") for x in blocks)
               + "</details>\n")
    return (f'<details class="testpair" open id="{pair_anchor(puml_rel)}"'
            f' data-test="{html.escape(test_rel)}">'
            f'<summary data-tip="{html.escape(test_rel)}">'
            f'{_cat_chip(cat, test_rel)}{name}</summary>'
            + src
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
            + genseq_details(rel, root)
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
            + genseq_details(rel, root)
            + body + '</div>')


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path):
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
    snippets = list(block.get("snippets", []))
    parts, used = [], set()
    # The registry the 🕵️ on the covering-tests rows reads: one entry per scenario the
    # generator drew, keyed the way that map addresses a row, so the jump is a lookup and
    # not a guess. Filled as the pairs are rendered — a pair that is not on this tab must
    # not be linkable from the other one.
    index = []

    def register(puml_rel, scenarios):
        for line, title in scenarios:
            index.append({"test": f"{test_of_genseq(puml_rel, root)}:{line}",
                          "pair": pair_anchor(puml_rel), "title": title})

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

    for test_rel, entries in plan.items():
        quoted_by_pair = _share_excerpts(test_rel, entries, snippets, used, root)
        for puml_rel, row in entries:
            scenarios = _scenarios_drawn(puml_rel, test_rel, root)
            quoted = [snippet_html(x["ref"], x.get("caption"), root)
                      for x in quoted_by_pair[puml_rel]]
            # No lead. The scenario names used to be printed here as deep links, and every
            # one of them was said again a few hundred pixels lower: the diagram's own
            # section headers are those same titles, linked to those same lines, drawn by
            # the generator. Two copies of one list, and the one on the picture is the one
            # that sits where the reader is already looking.
            pieces = [] if quoted else [_unquoted_note(test_rel, root)]
            pieces.append(
                _stale_sequence(puml_rel, test_rel, root, out_dir) if row is STALE
                else render_diagrams(merged, root, out_dir, [row], bare=test_rel)
                if row is not None
                else _unchanged_sequence(puml_rel, test_rel, root, out_dir))
            parts.append(_folded_pair(puml_rel, test_rel, pieces, quoted, scenarios,
                                      _pair_cat(puml_rel, root,
                                                authored_cat.get(test_rel))))
            register(puml_rel, scenarios)

    orphaned = [x for x in snippets if id(x) not in used]
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

    if not parts:
        return "", 0, 0
    # `"title": ""` means no heading at all, and is worth having: every pair below already
    # names its own scenarios and carries its own source path, so a heading over them can
    # only restate what the tab label said — and it does it above the fold, where the
    # first picture should be. An absent title still gets the default; only an author who
    # typed an empty one is asking for the space back.
    title = block.get("title", "Sequence deltas")
    head = ((f'<h3 id="{html.escape(block.get("id", "sequences"))}">'
             f'{html.escape(title)}</h3>') if title else "")
    head += f'<p>{block["body"]}</p>' if block.get("body") else ""
    # Invisible, and last: nothing to look at, only the map's way back in. `</` cannot
    # appear inside a script element, whatever its type.
    if index:
        parts.append('<script type="application/json" id="hr-genseq">'
                     + json.dumps(index).replace("</", "<\\/") + "</script>")
    # Weight counts every exhibit; changes count only the manifest's rows. An unchanged
    # pair is context, exactly as a `puml` block is, and must not un-strike the tab.
    return ("\n".join(([head] if head else []) + parts) + "\n",
            len(rows) + unchanged + stale + len(orphaned), len(rows))
