#!/usr/bin/env python3
"""Lift a line range out of a source file, verbatim, as an HTML snippet block.

A review guide that paraphrases code is a review guide that goes stale the moment
someone edits the file. This exists so the guide never retypes a line: every
snippet in it is cut from the working tree at build time, carries its real line
numbers, and is titled with a `path:12-14` reference that opens the file at that
exact line in VS Code.

Usage:
    extract-snippet.py <path>:<from>-<to> [<path>:<line> ...] [--caption "..."]
    extract-snippet.py --self-test

Emits one <figure class="snippet"> per reference on stdout, ready to paste into
the guide. Paths are repo-relative in the caption and absolute in the vscode://
link, because VS Code resolves nothing itself.
"""
from __future__ import annotations

import argparse
import functools
import html
import os
import re
import subprocess
import tempfile
import sys
from pathlib import Path

try:
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
except ImportError:
    raise SystemExit("[extract-snippet] needs Pygments: python3 -m pip install pygments")
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename, guess_lexer
from pygments.util import ClassNotFound

REF_RE = re.compile(r"^(?P<path>.+?):(?P<spans>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)$")
SPAN_RE = re.compile(r"^(?P<start>\d+)(?:-(?P<end>\d+))?$")

LANG_BY_SUFFIX = {
    ".java": "java",
    ".ts": "typescript",
    ".js": "javascript",
    ".html": "html",
    ".sql": "sql",
    ".py": "python",
    ".sh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".puml": "plantuml",
    ".feature": "gherkin",
    ".json": "json",
    ".css": "css",
}

# Highlighting is done here, at extraction time, by Pygments — not by a JS highlighter in
# the page. The guide is a local file that must render offline and survive being emailed
# around, and the language is already known from the filename, so there is nothing for a
# runtime highlighter to work out that we do not know at build time.
LIGHT_STYLE = "friendly"
DARK_STYLE = "github-dark"


def _lexer_for(path: Path, body: str):
    """Filename first (it is authoritative), guessing only as a fallback."""
    try:
        return get_lexer_for_filename(path.name, body)
    except ClassNotFound:
        pass
    lang = LANG_BY_SUFFIX.get(path.suffix)
    if lang:
        try:
            return get_lexer_by_name(lang)
        except ClassNotFound:
            pass
    try:
        return guess_lexer(body)
    except ClassNotFound:
        return None


def stylesheet() -> str:
    """The Pygments token colours, light and dark, scoped to our own code blocks."""
    light = HtmlFormatter(style=LIGHT_STYLE).get_style_defs("pre.code")
    dark = HtmlFormatter(style=DARK_STYLE).get_style_defs("pre.code")
    return (
        f"{light}\n@media (prefers-color-scheme: dark) {{\n{dark}\n}}\n"
        "pre.code .ln { color:inherit; }\n"
        # The skipped-lines row. Muted and italic so it never reads as source, and
        # `user-select:none` so copying the block out yields the real lines only.
        "pre.code .ln-gap { font-style:normal; }\n"
        "pre.code .code-gap { font-style:italic; opacity:.55; user-select:none; }\n"
        # Lines this branch added are marked in a column of their own, never behind the
        # code. Green already means "covered" on the Tests tab, one column to the
        # left, so a green band under source would be a genuine ambiguity; a `+` and a
        # rule down the left edge are diff vocabulary instead, they sit outside the code,
        # and they leave the syntax colours untouched.
        "pre.code .dm { display:inline-block; width:1.05em; text-align:center;\n"
        "  user-select:none; border-left:3px solid transparent; margin-right:.15em;\n"
        "  color:transparent; }\n"
        "pre.code .ln-row.added .dm { border-left-color:#2da44e; color:#2da44e;\n"
        "  font-weight:700; }\n"
        # In a window that is mostly old, the untouched lines are context. Letting them
        # recede answers "what do I look at" better than making one line among twenty
        # shout. Not done when the whole window is new - there is nothing to recede from.
        "pre.code.diff-changed .ln-row:not(.added) { opacity:.55; }\n"
        ".code-badge { display:inline-block; font:600 10.5px/1.6 ui-monospace,\n"
        "  SFMono-Regular,Menlo,monospace; letter-spacing:.06em; text-transform:uppercase;\n"
        "  padding:0 7px; border-radius:999px; border:1px solid currentColor;\n"
        "  color:#1a7f37; background:rgba(45,164,78,.09); }\n"
        ".code-badge[data-diff=unchanged] { color:var(--muted,#6b6b6b);\n"
        "  background:transparent; }\n"
        # --- the source bar -----------------------------------------------------------
        # The one header a quoted block gets, wherever it is quoted: the two ways to open
        # the change, the file they open, and what changed in it — one group at the right
        # end, read left to right. It was three different headers before — the Tests tab's
        # own bar in a per-review asset, the Review tab's diff corner, the Logging tab's
        # bottom-right marker — which meant a reader learned the vocabulary three times and
        # a fix to one of them left the other two alone. Defined here, in the stylesheet
        # the snippets already share, so a hand-authored fragment gets it by using the
        # class name.
        # The three parts are one phrase, so none of them is pushed away from the others
        # with an auto margin: the row is aligned as a whole against the right edge, and
        # the gap between its parts is the same everywhere the bar appears.
        ".srcbar { display:flex; align-items:center; flex-wrap:wrap; gap:7px;\n"
        "  justify-content:flex-end; margin:0 0 .5rem; }\n"
        ".srcbar .srcbar-path { text-align:right; }\n"
        # It trails the name now, and a smaller gap ties it to the word it is a fact about
        # rather than letting it float between the file and the edge.
        ".srcbar .code-badge { flex:0 0 auto; font-size:9px; padding:1px 6px;\n"
        "  margin-left:-2px; }\n"
        # The two handles open the file named immediately after them. Each is emitted only
        # where that side can really show it, so a missing one is an honest absence. They
        # are stripped of the standalone handle's skin: `a.srcref.diffref` boxes the pill
        # that floats under a snippet with nothing around it, and carrying that into the
        # bar bought a second vocabulary for a link in a row of three. It also outranked
        # this rule, so its -.35rem/1.15rem pull-up came along and lifted both handles off
        # the baseline the badge and the path share. The selector keeps its type so it
        # outranks that one; margin:0 puts them back on the line.
        ".srcbar a.srcref.diffref { flex:0 0 auto; padding:0; margin:0; background:none;\n"
        "  border:0; border-bottom:1px dotted currentColor; border-radius:0;\n"
        "  letter-spacing:0; white-space:nowrap; }\n"
        ".srcbar a.srcref.diffref:hover { background:var(--accent-soft,#eef2fb); }\n"
        # A logo needs no dotted underline to say it is a link — the underline was there to
        # mark three letters that could otherwise be read as a label, and under a mark it
        # is just a line. What is left is a hit area big enough to click at 14px.
        # ...and they sit closer than the row's own 7px: a mark is a smaller thing than
        # the words around it, and given the same air it floats off the name it belongs
        # to. Two thirds of the gap, taken back with a negative margin so the hover box
        # keeps its full hit area.
        ".srcbar a.srcref.srcbar-diff { border-bottom:0; padding:1px 3px;\n"
        "  border-radius:5px; line-height:0; margin-right:-2.33px; }\n"
        # The marks themselves, wherever a `.srcref` carries one: at the size of the text
        # beside them, sitting on its baseline rather than on the box's.
        # Grey at rest, lit on hover — and never the link colour it would otherwise
        # inherit, because a blue octocat reads as a decorated arrow. Two logos in full
        # colour beside a file name are the brightest thing in the row and the least of
        # what it says; grey, they sit at the weight of furniture and the name reads
        # first. Under the cursor each comes up in the colour it is recognised by: the
        # octocat in the page's own ink (its mark is monochrome by its own brand) and the
        # ribbon in its blue, which is the only thing that makes it VS Code.
        ".srcref svg.ico { display:inline-block; width:14px; height:14px;\n"
        "  vertical-align:-.2em; fill:var(--muted,#6b6b6b);\n"
        "  transition:fill .12s ease; }\n"
        "a.srcref:hover svg.ico, a.srcref:focus-visible svg.ico { fill:var(--fg,#1c1c1c); }\n"
        "a.srcref:hover svg.ico-vsc, a.srcref:focus-visible svg.ico-vsc { fill:#0098ff; }\n"
        # A Java test path is longer than this column is wide. `anywhere` lets it wrap, and
        # the <wbr> after each slash keeps every fragment a readable path segment;
        # `break-word` alone would split `victor` down the middle.
        ".srcbar .srcref { margin-bottom:0; overflow-wrap:anywhere; }\n"
        "@media (prefers-color-scheme: dark) {\n"
        "  a.srcref:hover svg.ico, a.srcref:focus-visible svg.ico { fill:var(--fg,#e6e6e6); }\n"
        "  pre.code .ln-row.added .dm { border-left-color:#3fb950; color:#3fb950; }\n"
        "  pre.code.diff-changed .ln-row:not(.added) { opacity:.5; }\n"
        "  .code-badge { color:#56d364; background:rgba(63,185,80,.12); }\n"
        "  .code-badge[data-diff=unchanged] { color:var(--muted,#9a9aa2);\n"
        "    background:transparent; }\n"
        "}\n"
    )


# --- what this branch added, per line -------------------------------------------------
# The reviewer's first question about a quoted test is "is this new, or is it an old test
# with a line in it?" - and until the answer is on the page they have to read the whole
# block to find out. It comes from git against the same merge-base the rest of the report
# diffs against (`origin/main...HEAD`), never from a hand-kept list. git models an edited
# line as delete+add, so a rewritten line counts as added, which is what a reader wants:
# it is a line this branch is responsible for.
DIFF_BASE = os.environ.get("HUMAN_REVIEW_DIFF_BASE", "origin/main")
HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,\d+)? @@")
# Above this share of a window's non-blank lines being added, the window is not "changed",
# it is new - and saying "15 of 16 lines changed" about a test that did not exist before
# is a worse answer than "new test".
NEW_BLOCK_RATIO = 0.8


def _git(root: Path, *args: str) -> str | None:
    """stdout, or None if git could not answer - no repo, no such ref, no git."""
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


@functools.lru_cache(maxsize=None)
def _diff_state(rel: str, root_s: str) -> tuple[frozenset[int], bool, bool]:
    """(lines added on this branch, the file itself is new, git had an answer at all).

    The third flag matters: "git said nothing changed" and "git could not be asked" must
    not render the same, or a snippet from outside the repo would claim to be untouched.
    """
    root = Path(root_s)
    if _git(root, "rev-parse", "--verify", "--quiet", DIFF_BASE) is None:
        return frozenset(), False, False
    # Against the WORK TREE, not against HEAD. `render` reads the lines it is about to
    # draw straight off the work tree, so a diff whose right-hand side is HEAD describes a
    # different text than the one on screen — and says so with a badge. A @SpringBootTest
    # written for the review it appears in, every line of it minutes old, rendered under
    # `UNCHANGED`, because the commit had not happened yet. The two halves of one claim
    # have to be read from the same version of the file.
    #
    # `BASE...HEAD` did two things: pick the merge-base, and end at HEAD. The merge-base
    # is still right — commits that landed on the base after this branch started are not
    # this branch's — so it is asked for by name and only the right-hand side moves. This
    # is also the comparison the rest of the pipeline already makes: puml-diff.sh reads
    # the work tree ("a diagram regenerated by the test run this review is about is still
    # uncommitted"), and so does the difflink helper.
    merge_base = (_git(root, "merge-base", DIFF_BASE, "HEAD") or "").strip() or DIFF_BASE
    # An untracked file is invisible to `git diff` at either end, so it has to be asked
    # about separately: the one kind of file that is certainly new would otherwise be the
    # one reported as untouched.
    if _git(root, "ls-files", "--error-unmatch", "--", rel) is None:
        try:
            count = len((root / rel).read_text(encoding="utf-8").splitlines())
        except OSError:
            return frozenset(), False, False
        return frozenset(range(1, count + 1)), True, True
    status = _git(root, "diff", "--name-status", merge_base, "--", rel)
    if status is None:
        return frozenset(), False, False
    is_new = bool(status.strip()) and status.strip().split("\t")[0].startswith("A")
    out = _git(root, "diff", "-U0", merge_base, "--", rel)
    if out is None:
        return frozenset(), False, False
    added, n = set(), None
    for line in out.splitlines():
        m = HUNK_RE.match(line)
        if m:
            n = int(m.group(1))
            continue
        if n is None or line.startswith("+++") or line.startswith("\\"):
            continue
        if line.startswith("+"):
            added.add(n)
            n += 1
        elif not line.startswith("-"):
            n += 1
    return frozenset(added), is_new, True


def added_lines(rel: str, root: Path) -> frozenset[int]:
    return _diff_state(rel, str(root))[0]


def block_status(rel: str, root: Path, spans, lines: list[str], noun: str = "code"):
    """The one-line answer to "what am I looking at?", or None when git cannot say.

    Counted over non-blank lines only: a window whose blank lines happen to be untouched
    is not thereby "partly old".
    """
    added, is_new, known = _diff_state(rel, str(root))
    if not known:
        return None
    nums = [n for s, e in spans for n in range(s, e + 1) if n <= len(lines)]
    real = [n for n in nums if lines[n - 1].strip()]
    hit = [n for n in real if n in added]
    if is_new:
        # A file that did not exist has no old lines to contrast with, so marking every
        # row green says nothing the badge has not already said. Said once, not painted.
        return {"diff": "new", "file_new": True, "label": f"new file",
                "added": len(real), "total": len(real),
                "tip": f"Every line here is new: {rel} does not exist before this branch."}
    if not hit:
        return {"diff": "unchanged", "label": "unchanged", "added": 0, "total": len(real),
                "tip": f"No line in this window was touched on this branch "
                       f"(diffed against {DIFF_BASE})."}
    if real and len(hit) / len(real) >= NEW_BLOCK_RATIO:
        return {"diff": "new", "label": f"new {noun}", "added": len(hit), "total": len(real),
                "tip": f"{len(hit)} of {len(real)} lines in this window are new on this "
                       f"branch - read it as newly written code."}
    return {"diff": "changed", "label": f"{len(hit)} line{'' if len(hit) == 1 else 's'} changed",
            "added": len(hit), "total": len(real),
            "tip": f"{len(hit)} of {len(real)} lines were added or rewritten on this branch "
                   f"(git counts a rewritten line as an addition); the rest is context and "
                   f"is dimmed."}


# The marks of the two places a handle can take you, drawn rather than abbreviated.
# `GH` and `VSC` were initials: three monospace letters the width of a short file name,
# which made the bar read as three words of equal weight when only one of them answers
# "which file is this?". A logo is recognised without being read, so it can sit right up
# against the name it belongs to without competing with it. The tooltips are unchanged and
# still carry the sentence — they were already doing that work, because "GH" did not say
# github.com either.
# Octicon `mark-github`, which is the octocat already inside its own circle.
ICON_GH = ('<svg class="ico ico-gh" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
           '<path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38'
           ' 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95'
           ' 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27'
           's-1.36.09-2 .27c-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.27-.82 2.15'
           ' 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82'
           '-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49'
           ' 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>')
# The VS Code ribbon. Kept in its own blue instead of `currentColor`: the octocat is
# monochrome by its own brand and reads as itself in either theme, but the ribbon is only
# recognisable as VS Code while it is that blue.
ICON_VSC = ('<svg class="ico ico-vsc" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<path d="M23.15 2.587 18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128'
            'a.999.999 0 0 0-1.276.057L.327 7.261A1 1 0 0 0 .326 8.74L3.899 12 .326 15.26'
            'a1 1 0 0 0 .001 1.479L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63'
            'a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352z'
            'm-5.146 14.861L10.826 12l7.178-5.448v10.896z"/></svg>')


def diff_badge(status) -> str:
    if not status:
        return ""
    return (f'<span class="code-badge" data-diff="{status["diff"]}" '
            f'data-tip="{html.escape(status["tip"], quote=True)}">'
            f'{html.escape(status["label"])}</span>')


def srcbar_html(href: str, rel: str, lineref: str = "", badge: str = "",
                links: str = "") -> str:
    """The header every quoted block on this page wears: *what* it is, *how* to open the
    change, and *which file* it came from.

    One builder for all three tabs. The Tests tab, the Review tab's applied fixes and the
    Logging tab each grew their own version of this row, and they disagreed on all three
    parts — which end the path sat at, whether the diff handles were there at all, whether
    the face was the path or the name. A reader crossing tabs had to relearn it each time,
    and a fix to one never reached the other two.

    The row reads as one sentence about one file: **where you can open it, what it is, and
    what changed in it** — handles, name, badge, in that order and in that order only.
    `links` are the two ways in (the editor and github.com) and they lead, pressed right up
    against the name, because the name is what each of them opens; each caller passes only
    the ones its side can really show, so a missing handle is an honest absence rather than
    a link that 404s. `badge` is what the block *is* (`new file`, `2 lines changed`, a
    diff's `+8 -4`) and it trails, because it is a fact *about* the file and can only be
    read once the file has been named — a badge that leads makes the reader hold "new file"
    in mind across the whole bar before finding out which file is new.

    The face is the file's **name** and the full path is on hover. A repo-relative Java
    path spends five segments on module, `src/main/java` and the org package before it
    reaches the one word that answers "which file is this?", which is the only question
    this row exists to answer. A file at the repo root has no path to move, and a tooltip
    repeating the name is a tooltip saying nothing."""
    name = Path(rel).name
    label = f"{name}:{lineref}" if lineref else name
    # What it does first, what it opens second. A tip that opens with a sixty-character
    # path makes the reader parse the path to find out whether the sentence at the end is
    # worth reading; the four words that never change are cheaper to skip than to hunt for.
    tip = (f"Open in VS Code: {rel}" if "/" in rel else "Open in VS Code")
    # Break the path at its own slashes: a <wbr> after each one gives the browser a legal
    # place to wrap, so a long name folds into readable pieces instead of snapping wherever
    # the box happens to end.
    face = html.escape(label).replace("/", "/<wbr>")
    return (f'<div class="srcbar">{links}'
            f'<a class="srcref srcbar-path" href="{html.escape(href)}"'
            f' data-tip="{html.escape(tip, quote=True)}">{face}</a>{badge}</div>')


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def parse_ref(ref: str):
    """`path:12`, `path:12-14`, or several of those comma-separated: `path:89,93-95`.

    The comma form is what the logging tab's "data flow to here" needs — the log line
    plus the lines its values came from, which are nowhere near each other in the file
    and must not be quoted as if they were. Returns the path and a list of (start, end),
    sorted and merged; a single-span reference is the same one-element list, so nothing
    else in here needs to know which form it was given."""
    m = REF_RE.match(ref)
    if not m:
        raise SystemExit(f"[extract-snippet] not a path:from-to reference: {ref!r}")
    spans = []
    for part in m["spans"].split(","):
        sm = SPAN_RE.match(part)
        start = int(sm["start"])
        end = int(sm["end"]) if sm["end"] else start
        spans.append((min(start, end), max(start, end)))
    return m["path"], merge_spans(spans)


# Two spans one line apart are quoted whole rather than split: an "… lines omitted …"
# marker standing in for a single line is more interruption than the line it hides.
GAP_MERGE = 1


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1] + 1 + GAP_MERGE:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


# A hand-written line range is a guess at where a construct begins and ends, and it is
# wrong in the same two ways every time: it opens on the tail of the comment above the
# code, and it stops a line or two before the closing brace. Both are fixed here rather
# than in each reference, because the reference is written by someone reading the file in
# an editor, where the range is obvious and the off-by-two is not.

COMMENT_LINE = re.compile(r"^\s*(//|#|/\*|\*/|\*(?!\S))")
# A line that only *finishes* something: `}`, `);`, `},` — the tail of the construct above
# the one being quoted.
CLOSING_ONLY = re.compile(r"^[\s)\]};,]*$")
OPENERS, CLOSERS = "([{", ")]}"
MAX_SNAP = 40


def _first_code_line(lines: list[str], start: int, end: int) -> int:
    """Skip what comes *before* the construct: blanks, comments, and the previous
    construct's closing brace.

    A range written by hand routinely opens one line early, so the snippet begins with a
    lone `}` belonging to the method above. That is not merely ugly: it makes the bracket
    depth start negative, cancelling out the `{` of the method actually being quoted, so
    the end-snapping below concludes there is nothing left to close and stops mid-method.
    One off-by-one at the top silently truncated the bottom.
    """
    i = start
    while i < end and (not lines[i - 1].strip()
                       or COMMENT_LINE.match(lines[i - 1])
                       or CLOSING_ONLY.match(lines[i - 1])):
        i += 1
    return i if i <= end else start


def _depth(line: str) -> int:
    """Bracket balance of one line, ignoring anything after a `//`."""
    code = line.split("//")[0]
    return sum(c in OPENERS for c in code) - sum(c in CLOSERS for c in code)


def _closing_line(lines: list[str], start: int, end: int) -> int:
    """Extend `end` until whatever the range opened is closed.

    A snippet that stops at `return vetName;` and never shows the `}` reads as a method
    the reviewer cannot see the end of — and they cannot tell whether that is the range
    or the code. Bounded, so an unbalanced file (or a brace inside a string this does not
    parse) costs a slightly long snippet rather than the whole rest of the file.
    """
    depth = sum(_depth(l) for l in lines[start - 1 : end])
    limit = min(len(lines), end + MAX_SNAP)
    while depth > 0 and end < limit:
        end += 1
        depth += _depth(lines[end - 1])
    return end


# The row that stands in for what was skipped between two spans. Deliberately not a
# number: the gutter is a line-number column and every other row in it is true, so the
# one row that is *not* a line must not look like one. The count rides in the code
# column, because "lines 90-92 are not here" is the whole point of the row.
def _gap_row(hidden: int) -> str:
    return (f'<span class="ln ln-gap">⋯</span>'
            f'<span class="code-gap">{hidden} line{"" if hidden == 1 else "s"} not shown</span>')


def render(ref: str, caption: str | None, root: Path, exact: bool = False,
           links="", link_at: tuple[int, int] | None = None) -> str:
    """`links` is the pre-rendered VSC/GH handles for the source bar, when the caller knows
    what this snippet is being compared against. The CLI does not — a snippet lifted by
    hand has no base ref in the argument list — so it renders the bar without them, which
    is the same bar minus two buttons rather than a different header.

    It may also be a **callable**, and that is how the report passes it: the two handles
    have to open where the bar's own link opens, and only this function knows where that
    is. The window a caller asks for is not the line the bar lands on — `--exact` off
    snaps past a leading comment, `link_at` overrides the window entirely — so a caller
    that built the handles from the reference it typed would aim them somewhere the face
    beside them does not say. Given a callable, it is called with the resolved line, once
    that line is settled, and all three parts of the bar answer the same question.

    `link_at` re-aims the bar at one (line, column) instead of at the window. The logging
    tab needs it: its windows pull in the lines a logged value came *from*, so the window
    is `9-11` while the statement the box is about is line 9 alone. Both halves move
    together — the face says `:9` and the link lands on `:9` — because a bar that reads
    `:9-11` and opens at 9 is two answers to one question."""
    rel, spans = parse_ref(ref)
    path = (root / rel).resolve()
    if not path.is_file():
        raise SystemExit(f"[extract-snippet] no such file: {rel}")

    lines = path.read_text(encoding="utf-8").splitlines()
    if spans[0][0] > len(lines):
        raise SystemExit(f"[extract-snippet] {rel} has {len(lines)} lines, "
                         f"asked for {spans[0][0]}")
    spans = [(s, min(e, len(lines))) for s, e in spans if s <= len(lines)]
    # Both snaps are right for a snippet that quotes a *method*: it should not open on a
    # blank line and a reviewer must be able to see where it stops. Both are wrong for one
    # that quotes a single statement in its neighbourhood, which is what the logging tab
    # does — there, skipping the leading comment drops the sentence that explains the line,
    # and extending to the end of the enclosing handler buries it in twenty lines the
    # caption is not about. `--exact` means "I chose this window; give me exactly it".
    # A multi-span reference is exact by construction: the caller picked those lines one
    # at a time, so there is no hand-written range left to correct.
    if not exact and len(spans) == 1:
        start, end = spans[0]
        start = _first_code_line(lines, start, end)
        spans = [(start, _closing_line(lines, start, end))]
    body = [l for s, e in spans for l in lines[s - 1 : e]]

    # Strip the common indent so a deeply nested method does not read as a column
    # of whitespace, but keep the relative shape.
    indents = [len(l) - len(l.lstrip()) for l in body if l.strip()]
    shift = min(indents) if indents else 0

    start, end = spans[0][0], spans[-1][1]
    lineref = ",".join(f"{s}-{e}" if e != s else f"{s}" for s, e in spans)
    if link_at:
        link = f"vscode://file/{path}:{link_at[0]}:{link_at[1]}"
        lineref = str(link_at[0])
    else:
        link = f"vscode://file/{path}:{start}:1"
    if callable(links):
        links = links(link_at[0] if link_at else start)
    lang = LANG_BY_SUFFIX.get(path.suffix, "")

    dedented = [l[shift:] if l.strip() else "" for l in body]
    lexer = _lexer_for(path, "\n".join(dedented))
    if lexer is not None:
        # nowrap=True keeps Pygments from adding its own <pre>/<div>, and it closes and
        # reopens token spans at every newline, so splitting per line stays well-formed
        # even through a block comment or a multi-line string.
        rendered = (
            highlight("\n".join(dedented), lexer, HtmlFormatter(nowrap=True))
            .rstrip("\n")
            .split("\n")
        )
    else:
        rendered = [html.escape(l) for l in dedented]
    # `.rstrip("\n")` above drops trailing blank lines, and a snippet that ends on one
    # would otherwise run the row loop off the end of the list. Pad rather than zip:
    # `zip` used to hide this by silently truncating, which is the same bug quieter.
    rendered += [""] * (len(dedented) - len(rendered))

    # Line numbers stay the file's own — never renumbered to look adjacent. The gap row
    # between two spans is what makes the jump readable instead of a silent lie.
    status = block_status(rel, root, spans, lines)
    added = added_lines(rel, root) if status else frozenset()
    # No marker column at all when git could not be asked: a snippet from outside the
    # repository must render exactly as it always has, not claim to be untouched.
    # No marker column when git could not be asked (a snippet from outside the repo must
    # render exactly as it always has), nor when the whole file is new (the badge said it).
    show_marks = bool(status) and not status.get("file_new")
    mark = '<span class="dm">+</span>' if show_marks else ""
    blank = '<span class="dm"> </span>' if show_marks else ""
    rows, i = [], 0
    for k, (s, e) in enumerate(spans):
        if k:
            rows.append(blank + _gap_row(s - spans[k - 1][1] - 1))
        for n in range(s, e + 1):
            hit = show_marks and n in added
            rows.append(f'<span class="ln-row{" added" if hit else ""}">'
                        f'{mark if hit else blank}<span class="ln">{n}</span>'
                        f'{rendered[i]}</span>')
            i += 1
    numbered = "\n".join(rows)

    # The caption is prose, and every other piece of prose in a content file is HTML —
    # `<code>log.warn</code>`, a bolded lead-in, a link. Escaping it here made this the one
    # field where markup came out as literal angle brackets on the page, so it does not.
    # The caption comes from the same authored content file as every body on the page; a
    # content file that can already inject markup everywhere loses nothing by doing it here.
    cap = f'<figcaption class="snippet-note">{caption}</figcaption>' if caption else ""
    return (
        f'<figure class="snippet">\n'
        f"{cap}"
        f'{srcbar_html(link, rel, lineref, diff_badge(status), links)}\n'
        f'<pre class="code lang-{lang}'
        f'{" diff-changed" if status and status["diff"] == "changed" else ""}">'
        f'<code>{numbered}</code></pre>\n'
        f"</figure>\n"
    )


def self_test() -> int:
    """Two things worth pinning: a range is lifted verbatim, and it snaps to real code.

    This file lives in the skill's own repository, not in the project under review, so it
    is its own fixture: the root for the self-test is wherever *it* sits.
    """
    here = Path(__file__).resolve()
    root, me = here.parent, Path(here.name)

    # A range opening on the shebang and the docstring snaps past both — a snippet that
    # begins `#!/usr/bin/env python3` is showing the reader the one line they did not ask
    # about. Numbering still counts from the real file, so line 1 is nowhere in the output.
    out = render(f"{me}:1-3", None, root)
    assert "srcref" in out and str(me) in out, out
    assert "usr/bin/env python3" not in out, "the shebang should have been snapped past"
    assert '<span class="ln">1</span>' not in out, out

    # …and a range over real code is lifted exactly, numbered from where it starts.
    lines = here.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines, 1) if l.startswith("def parse_ref"))
    out = render(f"{me}:{start}-{start + 1}", None, root)
    assert f'<span class="ln">{start}</span>' in out, out
    assert "parse_ref" in out, out

    # A range that opens one line early, on the previous construct's closing brace, snaps
    # past it *and* still closes. Both halves matter: the stray `}` starts the bracket
    # depth negative, which cancels the `{` of the construct actually being quoted, so an
    # off-by-one at the top used to silently truncate the bottom. (Brace languages only —
    # an indentation language has no bracket to balance, so its ranges are taken as given.)
    braced = "\n".join([
        "function before() {", "  return 1;", "}", "",
        "function quoted(x) {", "  if (x) {", "    return 1;", "  }",
        "  return 0;", "}",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        (sandbox / "snap.ts").write_text(braced + "\n", encoding="utf-8")
        out = render("snap.ts:3-7", None, sandbox)      # opens on `}`, ends inside the `if`
        assert '<span class="ln">3</span>' not in out, out          # the stray brace is gone
        assert '<span class="ln">5</span>' in out, out              # starts at the signature
        # …and runs to its own `}`, two lines past where the range stopped — highlighting
        # splits the source across spans, so the line gutter is what to assert on.
        assert '<span class="ln">9</span>' in out, out
        assert '<span class="ln">10</span>' in out, out

    print("[extract-snippet] self-test ok", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("refs", nargs="*", metavar="path:from-to")
    ap.add_argument("--caption", help="one-line note shown above the snippet (HTML allowed)")
    ap.add_argument("--exact", action="store_true",
                    help="quote the given window verbatim; do not extend it to a closing brace")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument(
        "--css",
        action="store_true",
        help="print the syntax-highlighting stylesheet these snippets need",
    )
    args = ap.parse_args(argv)

    if args.css:
        print(stylesheet())
        return 0
    if args.self_test:
        return self_test()
    if not args.refs:
        ap.error("give at least one path:from-to reference")

    root = repo_root()
    for ref in args.refs:
        sys.stdout.write(render(ref, args.caption, root, exact=args.exact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
