"""Code excerpts and diffs cut out of the working tree at build time."""
from __future__ import annotations

import functools
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from .util import EXTRACT

# The caption is prose, and prose about this project says things like `{{ visit | vetName }}`.
# Stopping it at the first `}` cut the directive in half there and spilled the rest onto the
# page as literal text, so the caption now runs to the last `}}` on the line — stopping
# only at a following `{{snippet:`, so two directives in one paragraph stay two
# directives while a caption may still quote a template expression.
SNIPPET_TOKEN = re.compile(
    r"\{\{snippet:(?P<ref>[^|}]+)(?:\|(?P<caption>(?:(?!\{\{snippet:).)*))?\}\}"
)


# `{{difflink:<repo-relative path>@<ref>}}` — the fix as a *change*, not as a result.
# A snippet shows the code that is there now, which is the answer to "what does it say"
# and not to "what did you do": the reader has to imagine the delta. This opens the real
# before/after in the editor instead.
DIFF_TOKEN = re.compile(r"\{\{difflink:(?P<path>[^|}@]+)@(?P<base>[0-9a-fA-F]{7,40})\}\}")

# The same handle, one letter shorter, for the diff a reader should not have to click to
# see. `{{diff:path@base}}` renders the hunks inline, GitHub-style; `{{difflink:…}}` only
# links out to them. The rule of thumb the page follows: an applied fix shows its diff,
# because the diff *is* the whole finding, and an open call links to one, because the
# reviewer is going to open the file anyway.
# The base accepts a trailing `^` or `~n`, because the usual left side of a committed fix
# is the commit before it, and spelling that out as a second sha is one more thing to get
# wrong.
DIFF_INLINE_TOKEN = re.compile(
    r"\{\{diff:(?P<path>[^|}@]+)@(?P<base>[0-9a-fA-F]{7,40}(?:\^|~\d+)?)"
    r"(?:\|(?P<caption>[^}]*))?\}\}"
)

# How much unchanged code to keep around each hunk. Three is git's own default and what
# GitHub shows before you click "expand": enough to place a hunk in its method, short
# enough that the diff stays the thing being read.
DIFF_CONTEXT = 3


@functools.lru_cache(maxsize=None)
def github_blob_base(root: Path) -> str | None:
    """`https://github.com/<owner>/<repo>` for this checkout, or None when it is not one.

    Read from `origin` rather than configured, because a URL that has to be maintained by
    hand in a content file is a URL that eventually points at somebody else's fork. Both
    remote spellings are accepted; anything that is not github.com returns None and the
    caller falls back to the editor link, which always works."""
    proc = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
                 proc.stdout.strip())
    return f"https://github.com/{m['slug']}" if m else None


def _github_compare_link(rel: str, base: str, root: Path, head: str | None = None,
                         line: int | None = None, side: str = "R",
                         face: str | None = None) -> str:
    """The same comparison on github.com — the link a reviewer forwards to somebody else.

    `face` shortens the label for somewhere there is no room for a sentence — a diff
    header, beside the editor link that opens the same comparison. It moves what the label
    stops saying into the tooltip rather than dropping it.

    Only emitted when the *after* side is something GitHub can be expected to have. A fix
    still sitting uncommitted in the working tree has no URL there at all, and inventing
    one that 404s is worse than the editor link rendered beside it."""
    host = github_blob_base(root)
    if not host:
        return ""
    if head is None:
        # The right side is the working tree. That has a URL on github.com only if the
        # file is clean at HEAD; otherwise the page is showing something github.com has
        # never seen, and inventing a link to it would be a confident lie.
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
        if rev.returncode != 0:
            return ""
        if subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", rel],
                          capture_output=True, text=True).stdout.strip():
            return ""
        head = rev.stdout.strip()
    url = f"{host}/compare/{base}...{head}"
    # A compare page can be forty files long, and landing at its top makes the reader hunt
    # for the one file the finding is about. GitHub anchors each file by the sha-256 of its
    # path exactly as the diff header spells it — no `a/`/`b/` prefix — so jump straight
    # there. Without a path there is nothing to hash, and the bare compare URL stands.
    if rel:
        url += "#diff-" + hashlib.sha256(rel.encode()).hexdigest()
        # And then the line. A file in a compare page opens at its own first line, which
        # for a five-hundred-line class is nowhere near the four lines the review is
        # about — so the reader arrives on github.com and starts hunting a second time,
        # having already been sent there to stop hunting. GitHub numbers the two sides
        # separately inside the file anchor: `R<n>` is the right side, `L<n>` the left,
        # which is the only landing a pure deletion has.
        if line:
            url += f"{side}{line}"
    # The face already says "on GitHub" and the arrow already says it opens elsewhere, so
    # a tooltip repeating either is a sentence the reader can see. What it cannot see is
    # where in a forty-file compare page it lands.
    tip = ("This change, in the compare page" if line else
           "Just this file, inside the compare page") if rel else "The whole compare page"
    # Short-faced in a bar, the tip is short too. The prose link can afford a sentence
    # about where in a forty-file compare page it lands; a mark in a row of three is
    # hovered to answer "what is this?", and a sentence there is a paragraph in a corner.
    if face:
        tip = "GitHub"
    return (f'<a class="srcref{" diffref srcbar-diff" if face else ""}" target="_blank"'
            f' rel="noopener" href="{html.escape(url)}"'
            f' data-tip="{tip}">{face or (_icon("GH") + " on GitHub")}</a>')


def _first_changed(rows) -> tuple[int | None, str]:
    """The line a reader of this diff is actually looking at, and which side it is on.

    The first added line, because that is what the change *did*; a pure deletion has no
    right side to land on, so it falls back to the first removed line on the left. Context
    lines are never it — landing three lines above the change is the same hunt in miniature.
    """
    for kind, old_no, new_no, _ in rows:
        if kind == "add":
            return new_no, "R"
    for kind, old_no, new_no, _ in rows:
        if kind == "del":
            return old_no, "L"
    return None, "R"


def _parse_unified(diff_text: str):
    """Unified diff text -> rows of `(kind, old_no, new_no, text)`, one per rendered line.

    Only the hunks: the `diff --git`/`index`/`---`/`+++` preamble says nothing a reader of
    a single-file diff needs, and the header above the table already names the file."""
    rows, old_no, new_no = [], 0, 0
    # `.split` on text that ends in a newline leaves an empty tail element, and an empty
    # line in a unified diff is a *context* line — so without this the table grows a blank
    # row and every number after it is off by one.
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file",
                            "old mode", "new mode", "similarity", "rename ")):
            continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$", line)
        if m:
            old_no, new_no = int(m[1]), int(m[2])
            rows.append(("hunk", None, None, m[3].strip()))
        elif line.startswith("+"):
            rows.append(("add", None, new_no, line[1:])); new_no += 1
        elif line.startswith("-"):
            rows.append(("del", old_no, None, line[1:])); old_no += 1
        elif line.startswith("\\"):
            rows.append(("hunk", None, None, line[1:].strip()))
        else:
            rows.append(("ctx", old_no, new_no, line[1:] if line else ""))
            old_no += 1; new_no += 1
    return rows


def review_step_rev(out_dir: Path) -> str | None:
    """The revision the review pass started from, as the ledger recorded it.

    The fallback base for an applied-fix diff whose entry does not name its own. Items
    parsed out of `review-points.md` always do — the file's frontmatter carries the
    `implementation:` sha, which is the commit the fixes were applied on top of and which
    survives a rebase — so this answers for a content file that still writes its own
    `autofixes` by hand. There it is genuinely unrecoverable after the fact, because once
    fixes are squashed or folded into a feature commit nothing says what the tree looked
    like before. Absent, the caller drops the diffs rather than guessing at a
    before-state."""
    ledger = out_dir / ".steps.json"
    if not ledger.is_file():
        return None
    try:
        records = json.loads(ledger.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for rec in records:
        if "review" in (rec.get("tabs") or []) and rec.get("rev"):
            return rec["rev"]
    return None


def _unmoved_since(rel: str, rev: str, root: Path) -> bool:
    """Is the working tree's copy of `rel` still byte-for-byte the one at `rev`?

    The question a pinned diff has to answer before it may also offer an editor link: the
    editor can only compare a ref against the file on disk, so `base -> rev` and
    `base -> disk` are the same comparison exactly while the file has not moved since
    `rev`. Asked of git rather than of the clock, because "the fix was the last commit" is
    not the same claim as "nobody has touched it since"."""
    proc = subprocess.run(["git", "-C", str(root), "diff", "--quiet", rev, "--", rel])
    return proc.returncode == 0


def diff_html(rel: str, base: str, root: Path, caption: str | None = None,
              head: str | None = None) -> str:
    """One file's change, rendered as a GitHub-style two-gutter table.

    `head` is the right-hand side, and defaults to the working tree. Name it when the fix
    is one commit and the file moved for other reasons since `base`: `base` alone would
    show the fix buried in every unrelated edit that landed in between, which is precisely
    the noise this block exists to cut. A committed fix is usually `base: "<sha>^"`,
    `head: "<sha>"` — the change on its own.

    Same contract as `diff_link_html`: the before-side has to be real. A base that does not
    resolve, a file that did not exist in it, or a diff that comes back empty drops the
    whole block and says which on stderr — a fix illustrated with a diff of nothing is the
    page lying about its own work, which is the one failure it exists to prevent."""
    src = root / rel
    if not src.is_file():
        print(f"[review] diff: no file at {rel} — block dropped", file=sys.stderr)
        return ""
    show = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True)
    if show.returncode != 0:
        print(f"[review] diff: {rel} does not exist at {base} — block dropped, because a "
              "diff needs a before-state that was actually recorded", file=sys.stderr)
        return ""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", f"-U{DIFF_CONTEXT}", "--no-color", base]
        + ([head] if head else []) + ["--", rel],
        capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"[review] diff: {rel} is unchanged since {base} — block dropped rather than "
              "rendering an empty diff", file=sys.stderr)
        return ""
    rows = _parse_unified(proc.stdout)
    adds = sum(1 for r in rows if r[0] == "add")
    dels = sum(1 for r in rows if r[0] == "del")
    body = []
    for kind, old_no, new_no, code in rows:
        if kind == "hunk":
            body.append('<tr class="hunk"><td class="gln"></td><td class="gln"></td>'
                        f'<td class="code">{html.escape(code) or "&nbsp;"}</td></tr>')
            continue
        marker = {"add": "+", "del": "-", "ctx": " "}[kind]
        body.append(
            f'<tr class="{kind}">'
            f'<td class="gln">{old_no or ""}</td><td class="gln">{new_no or ""}</td>'
            f'<td class="code">{html.escape(marker + code)}</td></tr>')
    # Two ways out of this block, in the header's own corner rather than in a footer under
    # it: the reader who wants the diff somewhere they can scroll it wants that *before*
    # reading the excerpt, not after, and a link below a forty-line diff is a link they
    # have to come back up from. Each wears the mark of what it opens — VS Code, and
    # github.com — rather than initials for it, because the corner is shared with the
    # file's stat and a logo is recognised in less room than a word is read.
    # The editor link diffs against the working tree, so it is the same comparison when
    # `head` is the working tree — and also when `head` is a commit the file has not moved
    # off since, which is the ordinary state of a fix applied and left alone. Checked
    # rather than assumed: a file edited after the commit it is pinned to would open in the
    # editor showing that later edit too, which is a different diff wearing this one's
    # label. Then, and only then, the GitHub link stands alone.
    link = ("" if head and not _unmoved_since(rel, head, root)
            else diff_link_html(rel, base, root, face=_icon("VSC")))
    gh = _github_compare_link(rel, base, root, head, *_first_changed(rows), face=_icon("GH"))
    # The name, and the path on hover. A repo-relative Java path spends five segments on
    # ceremony -- module, `src/main/java`, the org package -- before it reaches the one
    # word that says which file this is, and the header is where a reader looks to answer
    # exactly that. The full path is not lost, it is moved to where the face could not fit
    # it, which is what this page's tooltips are for. A file at the repo root has no path
    # to move, and a tooltip repeating the name is a tooltip saying nothing.
    # The same source bar every other quoted block on this page wears, built by the same
    # function: the two handles, the file they open, then the stat as this block's badge —
    # `+8 -4 vs 5acf2472` is exactly the "what changed in it" a snippet's `new code` answers.
    stat = (f'<span class="stat"><span class="added">+{adds}</span> '
            f'<span class="removed">&minus;{dels}</span> vs '
            f'<code>{html.escape(base[:8])}</code></span>')
    bar = _extract_module().srcbar_html(
        f"vscode://file/{src.resolve()}", rel, "", stat, link + gh)
    return (
        '<div class="ghdiff">'
        f'{bar}'
        f'<div class="ghdiff-scroll"><table class="ghdiff-body"><tbody>{"".join(body)}</tbody></table></div>'
        + (f'<p class="ghdiff-note">{caption}</p>' if caption else "")
        + '</div>'
    )



@functools.lru_cache(maxsize=None)
def diff_uri_handler() -> str | None:
    """The extension id that can open a diff for a guide read off disk, if it is installed.

    A guide served over http asks its own origin for a diff. Read from `file://` — which is
    how it is actually read — there is no origin to ask and no reach to loopback, so the
    only channel left is a `vscode://<publisher>.<extension>/...` URL that VS Code routes
    to an extension. That URL is *not* portable: on a machine without the extension it
    dead-ends instead of degrading, which is worse than no diff at all.

    So it is emitted only where it will work, and the check is for the thing itself rather
    than for a flag someone has to remember to set. `HUMAN_REVIEW_DIFF_URI_HANDLER` forces
    it either way — an id to use, or empty to suppress it — for building a guide meant to
    be read on another machine."""
    override = os.environ.get("HUMAN_REVIEW_DIFF_URI_HANDLER")
    if override is not None:
        return override.strip() or None
    for store in (Path.home() / ".vscode" / "extensions",
                  Path.home() / ".vscode-insiders" / "extensions"):
        try:
            if any(d.name.startswith("victorrentea.victor-vsc-") for d in store.iterdir()):
                return "victorrentea.victor-vsc"
        except OSError:
            continue
    return None


def diff_link_html(rel: str, base: str, root: Path, face: str | None = None,
                   line: int | None = None) -> str:
    """A link that opens `<rel>` as a diff: the file at `base` on the left, the working
    tree on the right.

    `face` shortens the label to a pill for a diff header, where the sentence it renders in
    prose ("diff vs 5acf2472") would fight the file name beside it. The tooltip already
    carried the whole comparison, so nothing is lost by the shorter face.

    **Emitted only when the before-side is real.** The ref has to resolve, the file has to
    exist in it, and the two sides have to actually differ. A diff whose left half is a
    guess would be this page telling a confident lie about history — the exact failure it
    exists to prevent — so a base that is missing, unreadable, or identical to the working
    tree drops the link and says which on stderr. No link is strictly better than a wrong
    one here; the snippet beside it still stands on its own.

    The `href` is the ordinary `vscode://file/…` every other reference on this page emits,
    aimed at the first line that actually differs. That is the whole portability story: a
    reader with none of this installed, or reading the file off disk, gets today's
    behaviour — the file, at the interesting line — rather than a dead custom URL. The
    upgrade to a real diff happens in the click handler, and only where a server is
    running that can perform it."""
    src = root / rel
    if not src.is_file():
        print(f"[review] difflink: no file at {rel} — link dropped", file=sys.stderr)
        return ""
    show = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"],
                          capture_output=True)
    if show.returncode != 0:
        print(f"[review] difflink: {rel} does not exist at {base} "
              f"({show.stderr.decode(errors='replace').strip()}) — link dropped, because a "
              "diff needs a before-state that was actually recorded", file=sys.stderr)
        return ""
    before, after = show.stdout, src.read_bytes()
    if before == after:
        print(f"[review] difflink: {rel} is identical at {base} and in the working tree — "
              "link dropped rather than opening an empty diff", file=sys.stderr)
        return ""
    # The first line that differs, so the fallback (and the diff itself) opens where the
    # change is instead of at the top of a file the reader then has to scan.
    #
    # `line` overrides it, and a snippet's bar always passes one. "The first line that
    # differs" is the right answer for a link standing for the *whole file* — a prose
    # `diff vs 5acf2472` — and the wrong one for a handle sitting against
    # `VisitRestController.java:102`, where it opened the file at its first changed import
    # while the face beside it said 102. One bar, one line.
    if line is None:
        b_lines, a_lines = before.split(b"\n"), after.split(b"\n")
        line = next((i + 1 for i, (x, y) in enumerate(zip(b_lines, a_lines)) if x != y),
                    min(len(b_lines), len(a_lines)) + 1)
    short = base[:8]
    # The extra handle for the unserved case. The `href` stays the ordinary
    # `vscode://file/...`, so dropping this attribute costs the diff and nothing else.
    handler = diff_uri_handler()
    uri = ""
    if handler:
        q = urllib.parse.urlencode({"file": str(src.resolve()), "base": base, "line": line})
        uri = f' data-diff-uri="{html.escape(f"vscode://{handler}/diff?{q}")}"'
    return (
        f'<a class="srcref diffref{" srcbar-diff" if face else ""}"'
        f' href="vscode://file/{src.resolve()}:{line}:1"{uri}'
        f' data-diff-path="{html.escape(rel)}" data-diff-base="{html.escape(base)}"'
        f' data-tip="{"Open Diff in VSC" if face else html.escape(f"Open this fix as a diff in VS Code — {short} on the left, the working tree on the right")}"'
        f'>{face or _icon("VSC") + f" diff vs {html.escape(short)}"}</a>'
    )


def expand_snippets(text: str, root: Path) -> str:
    """Let prose interleave with code: `{{snippet:path:12-14|caption}}` inside any body."""
    text = DIFF_INLINE_TOKEN.sub(
        lambda m: diff_html(m["path"].strip(), m["base"].strip(), root,
                            (m["caption"] or "").strip() or None),
        text,
    )
    text = DIFF_TOKEN.sub(
        lambda m: diff_link_html(m["path"].strip(), m["base"].strip(), root), text
    )
    return SNIPPET_TOKEN.sub(
        lambda m: snippet_html(m["ref"].strip(), (m["caption"] or "").strip() or None, root), text
    )


#: The ref every snippet on this page is implicitly a claim about. `extract-snippet.py`
#: reads the same environment variable to decide which of a snippet's lines are new, so
#: reading it here keeps one answer behind both halves of the source bar: the badge says
#: "new code *since this*", and the handle beside it opens exactly that comparison. Two
#: bases would let the badge and the button disagree in a way nothing on the page shows.
SNIPPET_BASE = os.environ.get("HUMAN_REVIEW_DIFF_BASE", "origin/main")


@functools.lru_cache(maxsize=1)
def _extract_module():
    """`extract-snippet.py` is hyphenated, so it is not importable by name.

    Loaded rather than shelled out to, which it used to be, because the source bar needs
    something a command line cannot carry: the two diff handles are built here — only this
    side knows the review's base ref and whether github.com can be expected to have the
    file — and they have to arrive *inside* the bar, not be glued onto its markup
    afterwards. The interpreter is the same one either way, so the Pygments requirement is
    unchanged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_snippet", str(EXTRACT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _icon(which: str) -> str:
    """The github.com / VS Code mark, from the module that also ships the CSS sizing it.

    Both halves of an icon are a unit — the `<svg>` and the rule that gives it 14px and a
    baseline — and the rule lives in the snippet stylesheet, because that is the sheet
    every page carrying a source bar already loads. Keeping the markup next to it means
    the two cannot drift into a mark rendered at whatever the browser guesses."""
    return getattr(_extract_module(), f"ICON_{which}")


@functools.lru_cache(maxsize=None)
def _shown_in_compare(rel: str, base: str, root: str, line: int) -> bool:
    """Would github.com's compare page have a row — and therefore an `R<line>` anchor — here?

    Asked because an anchor is a promise, and a fragment that matches no id does not fall
    back to the file anchor it was appended to: the browser simply does not scroll, and the
    reader lands at the top of a forty-file compare page. A snippet quotes whatever window
    its author chose, most of whose lines are untouched context nowhere near the change, so
    unlike the diff blocks — which aim at a line they just rendered as added — this one has
    to be checked before it is offered.

    `-U3` and three dots, because that is exactly what the compare page renders unexpanded
    and exactly what a compare URL compares. Anything wider vouches for a row that is
    behind an "expand" button over there. A base this checkout cannot resolve answers no,
    which costs the line and keeps the file."""
    proc = subprocess.run(["git", "-C", root, "diff", "-U3", "--no-color",
                           f"{base}...HEAD", "--", rel], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    return any(new_no == line for _, _, new_no, _ in _parse_unified(proc.stdout))


def _snippet_links(rel: str, root: Path, line: int | None = None) -> str:
    """The VS Code and github.com handles for a quoted block, against the review's own base.

    Both are optional and for the same reason: each is emitted only where that side can
    really open what it promises. `diff_link_html` drops itself when the base does not
    resolve, when the file did not exist in it, or when the two sides are identical;
    `_github_compare_link` drops itself when the file is dirty in the working tree, which
    is precisely when github.com has never seen what the snippet is showing. A bar with
    one handle, or none, is the honest rendering — a dead button is worse than no button.

    `origin/` is stripped for github.com only: it is a name for a ref in *this* checkout,
    and a compare URL spelling it 404s.

    `line` is the line the bar's own face names, and both handles are aimed at it. Without
    it each side picked its own landing — the editor the file's first changed line, github
    the top of the file inside the compare page — so a bar reading `:102` carried three
    buttons that went to three different places, and the two that were there to open the
    change went nowhere near the statement the box was about."""
    vsc = diff_link_html(rel, SNIPPET_BASE, root, face=_icon("VSC"), line=line)
    # The editor can open any line of the file; github.com can only anchor one it draws.
    # Checked against the ref as *this* checkout spells it (`origin/main`), which is the
    # thing github calls `main` — the stripped spelling below is for the URL alone, and
    # asking git about it would compare against a local branch that may be days behind.
    at = line if line and _shown_in_compare(rel, SNIPPET_BASE, str(root), line) else None
    gh = _github_compare_link(rel, SNIPPET_BASE.removeprefix("origin/"), root,
                              line=at, face=_icon("GH"))
    return vsc + gh


def snippet_html(ref: str, caption: str | None, root: Path, exact: bool = False,
                 link_at: tuple[int, int] | None = None) -> str:
    rel = ref.rsplit(":", 1)[0] if ":" in ref else ref
    # Deferred, not built here: the line this snippet actually opens at is settled inside
    # `render` — the window may snap past a leading comment, and `link_at` replaces it
    # outright — so the handles are built once that answer exists rather than from the
    # reference as it was typed.
    return _extract_module().render(
        ref, caption, root, exact,
        links=functools.partial(_snippet_links, rel, root), link_at=link_at)
