"""Code excerpts and diffs cut out of the working tree at build time."""
from __future__ import annotations

import functools
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


def diff_html(rel: str, base: str, root: Path, caption: str | None = None,
              head: str | None = None, hunks=None, fold: bool = False) -> str:
    """One file's change, rendered as a GitHub-style two-gutter table.

    `head` is the right-hand side, and defaults to the working tree. Name it when the fix
    is one commit and the file moved for other reasons since `base`: `base` alone would
    show the fix buried in every unrelated edit that landed in between, which is precisely
    the noise this block exists to cut. A committed fix is usually `base: "<sha>^"`,
    `head: "<sha>"` — the change on its own.

    Same contract as `diff_link_html`: the before-side has to be real. A base that does not
    resolve, a file that did not exist in it, or a diff that comes back empty drops the
    whole block and says which on stderr — a fix illustrated with a diff of nothing is the
    page lying about its own work, which is the one failure it exists to prevent.

    `hunks` keeps only those hunks of the diff — 0-based, in the order `git diff
    -U{DIFF_CONTEXT}` prints them — so one file a commit touched for two reasons can be
    shown as two blocks, each under the card it answers (`tabs/review.py`,
    `attribute_fix_hunks`). The stat then counts the kept lines only."""
    src = root / rel
    if not src.is_file():
        print(f"[review] diff: no file at {rel} — block dropped", file=sys.stderr)
        return ""
    show = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True)
    # A file added since `base` has an empty before-state — all additions, still a diff.
    if show.returncode != 0 and subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{base}^{{commit}}"],
            capture_output=True).returncode != 0:
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
    text = proc.stdout
    if hunks is not None:
        # Split on the `@@` headers, not on parsed rows: a `\ No newline` line parses to
        # the same row kind as a header and would shift every index after it.
        keep = set(hunks)
        parts = re.split(r"(?m)^(?=@@ )", text)
        text = "".join(p for i, p in enumerate(parts[1:]) if i in keep)
        if not text.strip():
            return ""
    rows = _parse_unified(text)
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
    # The name, and the path on hover. A repo-relative Java path spends five segments on
    # ceremony -- module, `src/main/java`, the org package -- before it reaches the one
    # word that says which file this is, and the header is where a reader looks to answer
    # exactly that. The full path is not lost, it is moved to where the face could not fit
    # it, which is what this page's tooltips are for. A file at the repo root has no path
    # to move, and a tooltip repeating the name is a tooltip saying nothing.
    # The same source bar every other quoted block on this page wears, built by the same
    # function: the file, then the stat as this block's badge — `+8 -4` is
    # exactly the "what changed in it" a snippet's file mark answers. No VS Code / GitHub
    # icons in front of the name any more (Victor, 5 Oct 2026): the name is the link.
    stat = (f'<span class="stat"><span class="added">+{adds}</span> '
            f'<span class="removed">&minus;{dels}</span></span>')
    bar = _extract_module().srcbar_html(
        f"vscode://file/{src.resolve()}", rel, "", stat)
    scroll = (f'<div class="ghdiff-scroll"><table class="ghdiff-body"><tbody>{"".join(body)}'
              '</tbody></table></div>')
    note = f'<p class="ghdiff-note">{caption}</p>' if caption else ""
    if fold:
        # Review tab: the code is reviewed in VS Code through the PR, so the snippet only
        # obscures the discussion. The file bar is the fold's summary (caret, name, stat),
        # the table is its body; collapsed until asked for.
        return ('<details class="ghfold"><summary>' + bar + '</summary>'
                '<div class="ghdiff">' + scroll + note + '</div></details>')
    return '<div class="ghdiff">' + bar + scroll + note + '</div>'


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
    # The Human Review extension (vscode-extension/ in this repo) first; victor-vsc, where its
    # URI handler was born and which still carries one, after it.
    for ext in ("victorrentea.human-review", "victorrentea.victor-vsc"):
        for store in (Path.home() / ".vscode" / "extensions",
                      Path.home() / ".vscode-insiders" / "extensions"):
            try:
                if any(d.name.startswith(ext + "-") for d in store.iterdir()):
                    return ext
            except OSError:
                continue
    return None


def commit_stamp(root: Path) -> str:
    """The attributes `<html>` carries so a click can ask for the version of a file this
    page quotes: the checkout it was built in, its HEAD, its branch, and the extension that
    can check a window against them (the same one, and the same override, as the diff).

    A reference is an absolute path, and a path names a place, not a version: opened in a
    window on another branch, or on this checkout three commits later, it shows a plausible
    wrong file. With the commit on the page, the editor bridge opens it only where the file
    is the one reviewed, and says so when nowhere is."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    head = git("rev-parse", "HEAD")
    if not head:
        return ""
    attrs = {"data-hr-root": str(root), "data-hr-head": head,
             "data-hr-branch": git("branch", "--show-current"),
             "data-hr-open-uri": diff_uri_handler() or ""}
    return "".join(f' {k}="{html.escape(v)}"' for k, v in attrs.items() if v)


def diff_link_html(rel: str, base: str, root: Path, face: str | None = None,
                   line: int | None = None) -> str:
    """A link that opens `<rel>` as a diff: the file at `base` on the left, the working
    tree on the right.

    `face` shortens the label to a pill for a diff header, where the sentence it renders in
    prose ("diff vs 5acf2472") would fight the file name beside it. The tooltip already
    carried the whole comparison, so nothing is lost by the shorter face.

    **Emitted only when the before-side is real.** The ref has to resolve and the two sides
    have to actually differ; a file absent from the ref is one the branch added, and its
    before-side is recorded too — empty. A diff whose left half is a
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
    added = False
    if show.returncode != 0:
        # A file the branch ADDED has a recorded before-state too: nothing. Its diff is all
        # additions, and that is what the reviewer needs for a new OwnerListPaging.java —
        # eval run 12 dropped every such link. Only a base that is not a commit is unknown.
        known = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{base}^{{commit}}"],
                               capture_output=True).returncode == 0
        if not known:
            print(f"[review] difflink: {rel} does not exist at {base} "
                  f"({show.stderr.decode(errors='replace').strip()}) — link dropped, because "
                  "a diff needs a before-state that was actually recorded", file=sys.stderr)
            return ""
        added = True
    before, after = (b"" if added else show.stdout), src.read_bytes()
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
    # The extension's diff reads the base side out of git itself, and a file added since
    # has none there: an added file goes through the served diff (an empty left side) or
    # the plain file link, never a URL that would dead-end.
    handler = None if added else diff_uri_handler()
    uri = ""
    if handler:
        q = urllib.parse.urlencode({"file": str(src.resolve()), "base": base, "line": line})
        uri = f' data-diff-uri="{html.escape(f"vscode://{handler}/diff?{q}")}"'
    return (
        f'<a class="srcref diffref{" srcbar-diff" if face else ""}"'
        f' href="vscode://file/{src.resolve()}:{line}:1"{uri}'
        f' data-diff-path="{html.escape(rel)}" data-diff-base="{html.escape(base)}"'
        f' data-tip="{"Diff in VS Code" if face else html.escape(f"Open this fix as a diff in VS Code — {short} on the left, the working tree on the right")}"'
        f'>{face or f"diff vs {html.escape(short)}"}</a>'
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


def set_diff_base(base: str) -> str:
    """Make `base` the ref every snippet on this page is measured from; returns the old one.

    The build calls this once, with `page_base`'s answer, before the first snippet renders:
    the NEW FILE / NEW CODE badge (`extract-snippet.py`'s `DIFF_BASE`) and the two diff
    handles beside it (`SNIPPET_BASE` here) were both frozen at import to `origin/main`,
    while the tabs around them measured from the base the review audited — so a file the
    audited base already had was badged NEW FILE. The environment variable is set too, so
    a subprocess started later reads the same answer.

    A tab module that imported the name (`from ..shared.snippets import SNIPPET_BASE`)
    holds a copy, so every loaded `hrbuild.*` module carrying one is updated with it."""
    import sys
    global SNIPPET_BASE
    old = SNIPPET_BASE
    SNIPPET_BASE = base
    os.environ["HUMAN_REVIEW_DIFF_BASE"] = base
    ext = _extract_module()
    ext.DIFF_BASE = base
    ext._diff_state.cache_clear()
    for name, mod in list(sys.modules.items()):
        if name.startswith("hrbuild.") and "SNIPPET_BASE" in vars(mod or object):
            mod.SNIPPET_BASE = base
    return old


@functools.lru_cache(maxsize=1)
def _extract_module():
    """`extract-snippet.py` is hyphenated, so it is not importable by name.

    Loaded rather than shelled out to, which it used to be: `set_diff_base` moves its
    `DIFF_BASE` once per build, and `diff_html` borrows its `srcbar_html`, neither of which
    a command line can carry. The interpreter is the same one either way, so the Pygments
    requirement is unchanged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_snippet", str(EXTRACT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def snippet_html(ref: str, caption: str | None, root: Path, exact: bool = False,
                 link_at: tuple[int, int] | None = None) -> str:
    return _extract_module().render(ref, caption, root, exact, link_at=link_at)
