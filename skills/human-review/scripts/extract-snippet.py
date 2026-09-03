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
        # code. Green already means "covered" on the Requirements tab, one column to the
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
        "@media (prefers-color-scheme: dark) {\n"
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
    status = _git(root, "diff", "--name-status", f"{DIFF_BASE}...HEAD", "--", rel)
    if status is None:
        return frozenset(), False, False
    is_new = bool(status.strip()) and status.strip().split("\t")[0].startswith("A")
    out = _git(root, "diff", "-U0", f"{DIFF_BASE}...HEAD", "--", rel)
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


def diff_badge(status) -> str:
    if not status:
        return ""
    return (f'<span class="code-badge" data-diff="{status["diff"]}" '
            f'data-tip="{html.escape(status["tip"], quote=True)}">'
            f'{html.escape(status["label"])}</span>')


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


def render(ref: str, caption: str | None, root: Path, exact: bool = False) -> str:
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
    label = ",".join(f"{s}-{e}" if e != s else f"{s}" for s, e in spans)
    label = f"{rel}:{label}"
    link = f"vscode://file/{path}:{start}:1"
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
        f'<a class="srcref" href="{html.escape(link)}" data-tip="Open in VS Code">{html.escape(label)}</a>\n'
        f'{diff_badge(status)}\n'
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
