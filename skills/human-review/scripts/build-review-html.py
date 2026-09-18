#!/usr/bin/env python3
"""Render a self-contained HTML review guide from a small JSON content file.

Splits the review into the half a machine should own and the half a human should:

  * this script owns the *mechanics* — page shell, styling, inlining the delta
    SVGs, laying out the diagram gallery, cutting every code snippet out of the
    working tree at build time via extract-snippet.py;
  * the JSON owns the *judgement* — what changed, what is risky, in what order a
    reviewer should look.

No snippet text ever lives in the JSON: only a `path:from-to` reference, so a
guide can never drift from the code it quotes.

Usage:
    build-review-html.py content.json --out .human-review/review.html
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import code_xref  # noqa: E402 - resolved from next to this file, not from site-packages

# --------------------------------------------------------------------------- #
# the stylesheet and the scripts, as files
# --------------------------------------------------------------------------- #
#
# They were twenty-one string literals in this file and 3,700 of its 10,500 lines: a
# stylesheet nothing could highlight, scripts no linter could see, and a diff over any of
# them that read as a diff over the page builder. They are the same bytes, in
# `hrbuild/assets/`, read at import time by `hrbuild/shared/assets.py`. The order the page
# emits them in has not moved — it is the document template at the foot of `main`, where
# it always was.
# --------------------------------------------------------------------------- #

from hrbuild.shared.assets import (
    APP_ENV_JS, CAPTION_JS, CSS, DGM_VIEWS_JS, EDITOR_JS, FOCUS_JS, FRAME_JS, GENSEQ_JS,
    HSCROLL_JS, LATE_CSS, PAINT_HOLD_JS, PAINT_RELEASE_JS, RERUN_JS, SEQFOLD_JS, SEQLINK_JS,
    SERVER_JS, TABS_JS, TIP_JS, TRACE_JS, XREF_CSS, XREF_JS
)

EXTRACT = HERE / "extract-snippet.py"
CODEOWNERS = HERE / "codeowners-check.py"
TESTCHANGES = HERE / "test-changes.py"

# The scope bar's third sign, beside `+` and `−`. It was `±`, which everywhere else a
# reader has met it means a *range* — "forty, give or take" — while the count of edited
# files is exact. A pencil says "someone went in and changed these", which is the fact,
# and it is the mark the page already uses for an edited row further down.
PENCIL = "\u270d\ufe0f"


# --------------------------------------------------------------------------- #
# what the page is allowed to ask the server to run
# --------------------------------------------------------------------------- #

# Dot-prefixed deliberately: `publish-demo.sh` publishes everything in a run directory
# that does not begin with a dot, and the downloadable zip is built from what it
# publishes. A manifest that travelled with either would be a list of this machine's
# commands sitting next to a page on someone else's, offering to run them.
ACTIONS_FILE = ".actions.json"

# Three buttons on this page describe a command and hand it to the clipboard, because a
# file on disk cannot run anything. Served by serve-review.py they can — but the command
# must not travel from the page, or "the review guide" becomes "a shell on :7654 that any
# tab in the browser can reach". So the page sends an id and the server looks the command
# up here, in a manifest written beside review.html by this build.
#
# The consequences are the point, not a side effect:
#   * a page from an older build can only name ids the *current* build still declares;
#   * the copy in the zip and the copy on GitHub Pages sit next to no manifest at all, so
#     they can ask for nothing — which is also exactly what they could do before;
#   * every command in it was written by this build out of the content file, so reviewing
#     what the button may run is reviewing the content file, which is already reviewed.
#
# A module-level register rather than a value threaded through the emitters: the three
# declarations are made by `runtime_html` and `rerun_html`, which are leaves of a render
# tree eight calls deep whose every other node is a pure string function. Passing a
# collector down that tree would put a parameter for the action server on a dozen
# signatures that have nothing to do with it. `main` clears it before a build and writes
# it after, which is the only ordering that matters.
ACTIONS: dict[str, dict] = {}


def declare_action(action_id: str, command: str, *, params: dict[str, str] | None = None,
                   scrape: str = "", reload: bool = False, label: str = "") -> str:
    """Register one runnable command and return the id the page should send.

    `params` maps each `{name}` hole in the command to the shape its value must have
    (`int`, `url`, `word` — serve-review.py owns the patterns). A hole with no declared
    shape is refused at run time rather than interpolated, so a template can never grow a
    parameter here without someone deciding what is allowed to go in it."""
    ACTIONS[action_id] = {"command": command, "params": dict(params or {}),
                          "scrape": scrape, "reload": reload, "label": label}
    return action_id


def write_actions(out_dir: Path) -> Path:
    """Drop the manifest beside the page, always — an empty one included.

    Always, because the file is read by mtime and the alternative to rewriting it is
    leaving the previous build's manifest in place: a page that no longer has the button
    next to a server that still offers to run the command behind it. An empty `actions`
    is a perfectly good statement and the one this build means when it renders no
    runnable control."""
    path = out_dir / ACTIONS_FILE
    path.write_text(json.dumps({"version": 1, "actions": ACTIONS}, indent=2) + "\n",
                    encoding="utf-8")
    return path


SEVERITIES = {
    "high": ("sev-high", "must look"),
    "medium": ("sev-med", "worth a look"),
    "low": ("sev-low", "nit"),
    "info": ("sev-info", "context"),
}


#: The one glyph a command wears — whichever of the two is true of *this* copy of the
#: report. Characters and not SVG: they are one text node each, they inherit the pill's
#: colour and size for free in both themes, and a page that carries eleven of these does
#: not want eleven inline documents in it.
#:
#: The run glyph is a circular arrow and not a ▶ because every command this page offers is
#: a *rerun*: it re-derives something the page is already showing and the page then catches
#: up with it. A play triangle promises a thing that starts and plays; this promises the
#: thing that comes round again, which is also what the masthead's badge and the spinner
#: mid-run are drawn from. One mark, learnt once, everywhere it can happen.
#:
#: `↻` and not the 🔃 emoji, for two reasons that both come down to it being *text*. It
#: takes the pill's colour — the served badge is green and the paid chip is amber, and an
#: emoji is a picture that stays its own colours inside both of them. And it is a stroke
#: rather than a two-tone glyph, so at .82rem it reads as a mark instead of a small
#: illustration. Picked over `⟳` (U+27F3) and `⥁` (U+2941) by measuring them: at this size
#: those come out a fifth to a third narrower, and over `⭮` (U+2BAE), which measured
#: exactly as wide as a private-use codepoint — i.e. it was tofu.
CMD_COPY = "\U0001F4CB"   # 📋
CMD_RUN = "\u21BB"        # ↻

# The masthead's Rerun — which is also the served badge, because they are one fact.
#
# Not to be confused with `rerun_html` further down, which is the offer under a *diagram*
# — one picture, re-rendered, from a command the build declared in the manifest. This one
# is the whole page, from a command the build never sees: it belongs to the server.
#
# It used to be a chip reading `Rerun` beside a chip reading `served`, and the two were
# saying the same thing twice. A page is served *exactly when* it can rerun itself: the
# badge announced the condition and the button beside it was the only thing that condition
# let you do. So the badge is the button. The glyph is the run mark every command on the
# page now wears, which makes the masthead the place a reader learns it — and its absence
# is what `static` means, in the one word that is left when nothing can run here.
#
# Emitted hidden and raised by the probe, like every other control here. A static copy has
# no process behind it, and a button that copied a shell line instead would be handing back
# the terminal round trip this exists to remove.
#
# The tooltip carries both halves, and names what the button will NOT do, because that is
# the part a reader cannot see and the part they are right to worry about: the findings on
# this page are a judgement bought once, and the film costs minutes and a running
# application.
RERUN_CHIP = ('<button type="button" class="chip chip-rerun chip-served" id="hr-rerun" '
              'hidden aria-disabled="true" data-rerun="__rerun__" '
              'aria-label="Served by the review server \u2014 rerun and rebuild this page" '
              'data-tip="Served by the review server: commands run from this page and '
              'recordings play in it. Click to re-derive the evidence and rebuild the '
              'page: diagrams, complexity, the REST contract, the logging scan, the test '
              f'manifest. Not the findings, and not the film. Free.">'
              f'<span class="rr-ico">{CMD_RUN}</span></button>')

# The same button with the model's half in front of it, and the only control on this page
# that spends money.
#
# It is a second button rather than a modifier on the first because the difference between
# them is not a degree of thoroughness — it is that one of them is free and reproducible
# and the other buys a judgement. A single Rerun that sometimes called a model would make
# every press a question about what it was about to do; two buttons make the answer the
# label.
#
# Three things guard it, in this order, and none of them is a substitute for another:
# the price is in the hover before the click, the click opens a dialog that says the price
# again and defaults to nothing, and the server will not honour it at all unless
# `rerun-model.py` is really beside it. The tooltip leads with the money, in those words,
# because "costs money" is the part a reader cannot see and the part they are right to
# worry about — everything else about this button is legible from its label.
RERUN_AI_CHIP = ('<button type="button" class="chip chip-rerun chip-rerun-ai" '
                 'id="hr-rerun-ai" hidden aria-disabled="true" '
                 'data-rerun="__rerun_ai__" '
                 'aria-label="Rerun with AI \u2014 costs about $5" '
                 'data-tip="costs money: ~$5 on Sonnet. Rewrites the requirements↔tests '
                 'matrix and the per-test catalogue with a model, then re-derives the '
                 'evidence and rebuilds the page.">'
                 # The free one's mark, a plus, then the two things this one adds to it:
                 # a model, and money leaving. The `+` is the whole sentence — this chip is
                 # the one beside it *and* something more — and without it the three marks
                 # ran together as one picture nobody could take apart. No words, because
                 # the sentence that matters here is the price, and the price is in the
                 # hover and again in the dialog: a label reading `Rerun + AI` said neither,
                 # and cost the masthead two words to say `rerun` a second time.
                 f'<span class="rr-ico">{CMD_RUN}</span>'
                 '<span class="rr-plus">+</span>'
                 '\U0001F916\U0001F4B8</button>')

# The confirmation, in the page rather than in the browser.
#
# `window.confirm` was the first version of this and it is the wrong control for the job in
# three ways at once: it cannot say the price in the page's own voice, it cannot make the
# safe answer the default one, and it is the dialog every abusive site on the internet has
# trained readers to dismiss without reading. A reader who reflexively clicks OK on a
# native confirm has spent five dollars; the same reflex here lands on Cancel, because
# Cancel is what has focus when the panel opens and what Escape and a click on the backdrop
# both mean.
#
# In the markup of every copy of the page, hidden, like every other control here — the
# button that opens it is what the probe raises, so a static copy never reaches this.
RERUN_AI_CONFIRM = (
    '<div class="hrconfirm" id="hr-ai-confirm" hidden role="dialog" aria-modal="true"'
    ' aria-labelledby="hr-ai-confirm-t">'
    '<div class="hrconfirm-box">'
    '<p class="hrconfirm-t" id="hr-ai-confirm-t"><b>This one costs money.</b></p>'
    '<p class="hrconfirm-b">Rerun&nbsp;+&nbsp;AI rewrites the requirements↔tests matrix '
    'and the per-test catalogue by asking a model — <b>about $5 on Sonnet</b> — and then '
    're-derives the evidence and rebuilds the page. The matrix you are looking at is '
    'replaced, not confirmed: a second pass over the same diff words and ranks it '
    'differently. The copy being replaced is kept in '
    '<code>.human-review/.model-prev/</code>.</p>'
    '<p class="hrconfirm-b hrconfirm-alt">Plain <b>Rerun</b> does everything except the '
    'model half, and costs nothing.</p>'
    '<div class="hrconfirm-row">'
    '<button type="button" class="hrconfirm-no" data-tip="Nothing is spent">Cancel</button>'
    '<button type="button" class="hrconfirm-yes" '
    'data-tip="Runs the model step, then rebuilds">Spend it, rerun with AI</button>'
    '</div></div></div>')

# Under the masthead rather than inside it: the header is a block that never scrolls, and
# a log tail pinned to the top of the viewport for the rest of the read is a worse artifact
# than the failure it reports. Hidden until there is something to report, so the normal
# read never pays for it, and dismissible, because the reader decides when it is read.
RERUN_FAIL = ('<div class="rerunfail" id="hr-rerun-fail" hidden role="alert">'
              '<div class="rerunfail-head"><b>Rerun failed</b>'
              '<span class="rerunfail-why"></span>'
              '<button type="button" class="rerunfail-x" '
              'aria-label="Dismiss this report" data-tip="Dismiss">✕</button></div>'
              '<pre class="rerunfail-log"></pre></div>')


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


# `src://<repo-relative path>[:line]` — the handle the diagram generators leave on a
# class, a field, an endpoint. They cannot emit `vscode://file/<abs>` themselves: their
# .puml is committed, and an absolute path in it is a diff on every machine that
# regenerates the diagram. Resolving it here, against this checkout, is the last moment
# where the absolute path is a fact rather than a guess.
SRC_HANDLE = re.compile(r'href="src://(?P<path>[^"#:]+)(?::(?P<line>\d+))?"')


def resolve_source_links(svg: str, root: Path) -> str:
    def fix(m):
        target = (root / m.group("path")).resolve()
        line = m.group("line") or "1"
        return f'href="vscode://file/{target}:{line}:1"'

    return SRC_HANDLE.sub(fix, svg)


# PlantUML renders a title's creole into coloured <text>, and *also* copies the title
# verbatim into the SVG's own <title> element — which is what the browser shows as a
# tooltip over the diagram background. A title that says `- <color:red>Diff</color>`
# therefore reads correctly on the page and as raw markup in the tooltip. Escaped there,
# hence both spellings.
CREOLE_IN_TITLE = re.compile(r"(?:<|&lt;)/?(?:color(?::[^>&]*)?|s|b|i|u)(?:>|&gt;)", re.I)
#: `[[src://path:12{Click to open the test} Add a visit]]` → `Add a visit`. Since a
#: sequence diagram's title became the scenario — clickable, straight into the test — the
#: verbatim copy PlantUML drops into `<title>` is the whole creole link, so hovering the
#: picture showed a reader the markup that made the heading they were already looking at.
CREOLE_LINK = re.compile(r"\[\[[^\]\s{]+(?:\{[^}]*\})?\s*([^\]]*?)\s*\]\]")
SVG_TITLE = re.compile(r"(<title>)(.*?)(</title>)", re.S | re.I)


def _plain_svg_title(svg: str) -> str:
    def plain(text: str) -> str:
        return CREOLE_IN_TITLE.sub("", CREOLE_LINK.sub(r"\1", text)).strip()

    return SVG_TITLE.sub(lambda m: m[1] + plain(m[2]) + m[3], svg)


# A class/entity PlantUML draws as `<g class="entity">`: a `<rect>` box, an optional
# stereotype icon (the ellipse+letter for `class`/`interface`/…), the name as one or more
# `<text>` runs (split when the diff colours part of it), a `<line>` under the title, and
# then the field rows. When the element carries `[[link]]`, PlantUML wraps the *entire*
# group's content in one `<a>` — box, name and every field alike — so a reviewer who
# ⌘-clicks or hovers a field lands on the class, not the field. Fields carry none of their
# own; the generator puts exactly one link per element, on the element itself.
ENTITY_BLOCK = re.compile(r'(<g class="entity"[^>]*>)(.*?)(</g>)', re.S)
ENTITY_SOLE_ANCHOR = re.compile(r'^<a\b(?P<attrs>[^>]*)>(?P<inner>.*)</a>$', re.S)
ENTITY_TITLE_BAND = re.compile(
    r'^(?P<rect><rect\b[^>]*/>)'
    r'(?P<icon>(?:<(?!text\b|line\b)[^>]*/>)*)'      # stereotype icon: ellipse, path, …
    r'(?P<title>(?:<text\b[^>]*>.*?</text>)+)'       # the name — one run, or several if coloured
    r'(?P<line><line\b[^>]*/>)'
    r'(?P<fields>.*)$',
    re.S,
)


def _scope_entity_links(svg: str) -> str:
    """Re-scope a class/entity's `<a>` to the title band (icon + name) it should be.

    Restructures the markup rather than overlaying a rect: the icon and name are already
    exactly the shapes that should answer to a click, so wrapping just them in the `<a>`
    — and moving the box and field rows outside it — gets the right hit area for free,
    with no coordinates to compute or keep in sync with the box's own size.

    Touches only a `<g class="entity">` whose entire content is one `<a>…</a>` shaped
    exactly as PlantUML draws it (rect, optional icon, name text(s), divider line, then
    fields). Anything else — no link, more than one `<a>`, an unrecognised inner shape —
    is left byte-for-byte alone; guessing at a diagram family this generator doesn't
    produce is worse than leaving its box fully clickable.
    """

    def fix_block(m):
        open_tag, content, close_tag = m.group(1), m.group(2), m.group(3)
        stripped = content.strip()
        if content.count("<a ") != 1 or not stripped.startswith("<a ") or not stripped.endswith("</a>"):
            return m.group(0)
        anchor = ENTITY_SOLE_ANCHOR.match(stripped)
        if not anchor:
            return m.group(0)
        band = ENTITY_TITLE_BAND.match(anchor["inner"])
        if not band:
            return m.group(0)
        title_anchor = f'<a{anchor["attrs"]}>{band["icon"]}{band["title"]}</a>'
        return f'{open_tag}{band["rect"]}{title_anchor}{band["line"]}{band["fields"]}{close_tag}'

    return ENTITY_BLOCK.sub(fix_block, svg)


# PlantUML paints every diagram it draws for this repo (class, ER, sequence — none of
# them set `!theme` or a colour skinparam beyond `hyperlinkColor`) in one fixed, hardcoded
# palette — plus, for a .puml that carries a `<style>` block of its own, whatever that
# block names. Both kinds are enumerated below by exact literal, and between them they
# cover every fill/stroke/background this pipeline inlines. Mapping each to a `--dgm-*`
# custom property (declared in CSS, above) — rather than a blanket `filter:invert()` on
# the diagram, which would flatten these into a wash and turn the diff renderer's
# deliberate reds into cyans — lets dark mode restyle exactly these shapes and nothing
# else, and lets the reds stay red (just brighter) instead of getting fought by a filter.
# `stroke` and `background` only ever appear as literal colours inside a `style="…"`
# attribute in this generator's output (never `fill`, and there is no `<style>` block to
# collide with); `fill` only ever appears as a bare attribute. Case-insensitive because
# PlantUML is consistent within one render but not guaranteed to be across versions.
DIAGRAM_COLOR_VARS = {
    "#FFFFFF": "--dgm-bg", "#F1F1F1": "--dgm-box", "#EEEEEE": "--dgm-frame",
    "#DDDDDD": "--dgm-legend", "#181818": "--dgm-line", "#000000": "--dgm-fg",
    "#ADD1B2": "--dgm-icon", "#E2E2F0": "--dgm-activation", "#888888": "--dgm-muted",
    "#1A4FA0": "--dgm-link",
    # The two differs' shared palette — puml_diff.ADDED / .REMOVED and the sequence
    # differ's lifeline and note tints. Kept in step with those constants by
    # test_diagram_dark_mode.py rather than by memory.
    "#2E7D32": "--dgm-diff-add", "#C62828": "--dgm-diff-del",
    "#EAF6EC": "--dgm-diff-add-bg", "#FFEBEB": "--dgm-diff-del-bg",
    # puml_diff.RIPPLE — the three washes that say how far a box sits from the change,
    # one hop out to three. Same bookkeeping, sharper stakes: these are *fills*, so a
    # tint left unthemed is not merely off-palette in dark mode, it is a pale slab with
    # a near-white name written on it.
    "#F2CF8E": "--dgm-ripple-1", "#F4DCB4": "--dgm-ripple-2", "#F2EBDB": "--dgm-ripple-3",
    # packages.puml's own <style> block (Material blue-grey): component fill,
    # component border, arrow. Same treatment, different source — see the CSS.
    "#ECEFF1": "--dgm-box-accent", "#546E7A": "--dgm-line-accent",
    "#78909C": "--dgm-arrow-accent",
}
DIAGRAM_FILL_ATTR = re.compile(r'\bfill="(#[0-9A-Fa-f]{6})"')
DIAGRAM_STYLE_COLOR = re.compile(r'\b(stroke|background):(#[0-9A-Fa-f]{6})\b')


def _theme_diagram_colors(svg: str) -> str:
    """Rewrite PlantUML's hardcoded palette to the page's `--dgm-*` variables.

    A colour this generator is not known to emit is left exactly as written — degrading
    to an unthemed shape in the unlikely event PlantUML's defaults change, rather than
    guessing at what a var name for it should mean."""

    def fix_fill(m):
        var = DIAGRAM_COLOR_VARS.get(m[1].upper())
        return f'fill="var({var})"' if var else m[0]

    def fix_style(m):
        var = DIAGRAM_COLOR_VARS.get(m[2].upper())
        return f'{m[1]}:var({var})' if var else m[0]

    svg = DIAGRAM_FILL_ATTR.sub(fix_fill, svg)
    return DIAGRAM_STYLE_COLOR.sub(fix_style, svg)


def inline_svg(path: Path, root: Path) -> str:
    """Inline rather than <img src>: the guide must survive being emailed as one file."""
    svg = path.read_text(encoding="utf-8")
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
    svg = _scope_entity_links(svg)
    svg = _theme_diagram_colors(svg)
    return _plain_svg_title(resolve_source_links(svg, root))


# `Browser → Backend: POST /api/owners/{ownerId}/pets/{petId}/visits` — a call arrow's
# title, as the generator writes it. The route is the contract; the method that answers it
# is where a reviewer actually has to go, and nothing in a trace knows its name.
GENSEQ_CALL_TITLE = re.compile(
    r"(?:\u2192|->)\s*\w+\s*:\s*"
    r"(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?P<route>/\S*)\s*$"
)

MAPPING_ANNOTATION = re.compile(r"@(?P<kind>Get|Post|Put|Patch|Delete|Request)Mapping\b")
# `@GetMapping(produces = "application/json")` names a media type, not a route, so the
# first string literal in the arguments is only the path when it is positional or spelled
# `value =`/`path =`. Anything else leaves the mapping at its class-level base.
MAPPING_NAMED_PATH = re.compile(r'(?:value|path)\s*=\s*\{?\s*"(?P<path>[^"]*)"')
MAPPING_POSITIONAL_PATH = re.compile(r'^\s*\{?\s*"(?P<path>[^"]*)"')
REQUEST_METHOD = re.compile(r"RequestMethod\.(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)")
TYPE_DECL = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed)\s+)*"
    r"(?:class|interface|record|enum)\s+(?P<name>\w+)"
)
METHOD_NAME = re.compile(r"(?P<name>\w+)\s*\(")
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".idea", ".gradle"}


def _annotation_span(lines, i: int):
    """The parenthesised argument text of the annotation on line `i`, and where it ends.

    Read as one balanced span rather than per line: a mapping wraps as soon as it carries
    `produces`, and an `@ApiResponse` above it wraps over four — whose continuation lines
    look exactly like a method declaration to a line scan, which is how `@Content(` once
    became the name of the method under it."""
    text, depth, started = "", 0, False
    for j in range(i, len(lines)):
        for ch in lines[j]:
            if ch == "(":
                depth += 1
                started = True
                if depth == 1:
                    continue
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text, j
            if started:
                text += ch
        if not started and lines[j].strip():
            return "", j
    return text, len(lines) - 1


def _mapping_path(args: str) -> str:
    m = MAPPING_NAMED_PATH.search(args) or MAPPING_POSITIONAL_PATH.match(args)
    return m["path"] if m else ""


def _join_route(base: str, sub: str) -> str:
    parts = [p for p in (base.strip("/"), sub.strip("/")) if p]
    return "/" + "/".join(parts)


def _controller_routes(source: str, rel: str, found: dict) -> None:
    """Every `VERB /route` this file handles, mapped to `Class.method` and its line.

    A line scan, not a parser: the three things it needs — the class-level base path, a
    method's mapping annotation, and the declaration under it — are each one line of Java,
    and a mapping annotation is never anywhere else in a file. Annotations pile up until a
    declaration consumes them, so the comments and the other annotations Spring code puts
    between `@PostMapping` and its method cost nothing."""
    lines = source.splitlines()
    base, cls, pending, i = "", Path(rel).stem, None, 0
    while i < len(lines):
        stripped, at = lines[i].strip(), i
        i += 1
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("@"):
            args, end = _annotation_span(lines, at)
            i = end + 1
            mapping = MAPPING_ANNOTATION.match(stripped)
            if mapping:
                verbs = ([mapping["kind"].upper()] if mapping["kind"] != "Request"
                         else [m["verb"] for m in REQUEST_METHOD.finditer(args)] or list(HTTP_VERBS))
                pending = (verbs, _mapping_path(args))
            continue
        declared = TYPE_DECL.match(stripped)
        if declared:
            cls = declared["name"]
            base, pending = (pending[1] if pending else ""), None
            continue
        name = METHOD_NAME.search(stripped) if pending else None
        if name:
            verbs, sub = pending
            for verb in verbs:
                found[f"{verb} {_join_route(base, sub)}"] = (rel, at + 1, f"{cls}.{name['name']}")
        pending = None


@functools.lru_cache(maxsize=None)
def spring_handlers(root: Path) -> dict:
    """`VERB /route` → (repo-relative file, line, `Class.method`) for this checkout.

    Only files that declare themselves controllers are opened, so a repo with no Spring in
    it pays one directory walk and nothing else. Resolved here rather than in the diagram
    generator because that generator sees a trace, which carries the route and no code."""
    found: dict = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".java"):
                continue
            path = Path(folder) / name
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "@RestController" not in source and "@Controller" not in source:
                continue
            _controller_routes(source, str(path.relative_to(root)), found)
    return found


def _with_handlers(index: dict, root: Path) -> dict:
    """Hang the entry point on every call arrow whose route this checkout still serves.

    A route the branch deleted simply gets no row — the base-ref sidecar is resolved
    against the work tree too, and a link into a method that is no longer there would be
    worse than the route alone."""
    handlers = None
    for entry in (index.get("details") or {}).values():
        called = GENSEQ_CALL_TITLE.search(entry.get("title", ""))
        if not called:
            continue
        if handlers is None:
            handlers = spring_handlers(root)
        hit = handlers.get(f'{called["verb"]} {called["route"]}')
        if hit:
            rel, line, name = hit
            entry["handler"] = {"name": name,
                                "href": f"vscode://file/{(root / rel).resolve()}:{line}:1"}
    return index


def genseq_details(rel: str, root: Path) -> str:
    """The sidecar the generator filed beside the diagram, carried into the page.

    Inlined rather than fetched: review.html is opened from file://, where fetch() of a
    neighbouring file is blocked, and the guide has to survive being mailed as one file."""
    if not rel.endswith(".genseq.puml"):
        return ""
    sidecar = root / (rel[: -len(".puml")] + ".json")
    if not sidecar.is_file():
        return ""
    return _details_carrier(sidecar, root)


def _details_carrier(sidecar: Path, root: Path) -> str:
    # `<` is the only character that can end a <script> block early, and a JSON string
    # may legally spell it \\u003c — so the payload stays valid JSON and inert to the
    # HTML parser without any un-escaping step on the other side.
    index = _with_handlers(json.loads(sidecar.read_text(encoding="utf-8")), root)
    payload = json.dumps(index, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" class="genseq-details">{payload}</script>'


def genseq_details_at_render(row, assets: Path, root: Path) -> str:
    """The sidecar as it stood when this diagram was drawn — not as it stands now.

    The handles PlantUML drew into the picture are generation-time ids derived from the
    payload behind each arrow, and a generated payload carries per-run values: a
    timestamp inside a description, a database-assigned row id. So every arrow whose body
    holds one is re-identified by the next run of the suite, while its SQL neighbours —
    whose statement text does not move — keep the id they had.

    That matters because the picture is rendered once, early, and the report is built
    later. Anything in between that re-runs the acceptance suite (the trace capture does)
    advances the sidecar a generation past the pictures. Read live, it then hands the page
    ids that no arrow in the SVG carries, and `GENSEQ_JS` unwraps each of them as a dead
    handle: the `200 ⊕` on an HTTP response stops opening its JSON body and becomes plain
    text, while `select owners ⊕` beside it still works. The failure is silent and reads
    as "the response handles were never wired".

    So the renderer copies the sidecar next to the SVG it drew from it, and this prefers
    that copy. The work tree stays the fallback, for a manifest written before the
    renderer took the snapshot.
    """
    name = (row.get("new_details") or "").strip()
    if name and (assets / name).is_file():
        return _details_carrier(assets / name, root)
    return genseq_details(row["source"], root)


def genseq_details_at_base(row, assets: Path, root: Path) -> str:
    """The same sidecar as of the base ref, for the diagram's `Old` pane.

    The handles PlantUML draws are generation-time ids, and an id is derived from the
    payload — so a request body that changed on this branch has a different id on each
    side. With only the work tree's sidecar in the page, every such handle on the old
    render resolved to nothing and `GENSEQ_JS` dropped it as a dead one: the pane looked
    wired (cursor, hit area) and expanded nothing. The ids that happened to survive —
    SQL whose statement did not change — kept working, which is what made the failure
    read as "bodies are broken" rather than "the payloads for that side are missing".

    Both carriers go into the page and the reader merges them, work tree first. Ids are
    content-derived, so an id in both sides means the same payload on both.
    """
    name = (row.get("old_details") or "").strip()
    return _details_carrier(assets / name, root) if name and (assets / name).is_file() else ""


def read_manifest(path: Path):
    rows = []
    if not path.is_file():
        return rows
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            rows.append(dict(zip(header, line.split("\t"))))
    return rows


VIEW_WORDS = {"new": "New", "old": "Old"}


def dgm_views_html(panes, initial: str = "diff") -> str:
    """The Diff / New-Old control and its panes — the ONE implementation of it.

    `panes` is an ordered list of `(view, inner-html)` with `"diff"` first. Three states
    off two buttons: the second holds both words and toggles between them, with the live
    one underlined, because a third pill costs as much room as the two that carry the
    argument and this control has to sit above a tall sequence diagram as comfortably as
    above a small structural one.

    `initial` is which of the three the widget opens on. It is "diff" for a *structural*
    diagram, where the delta is a clean two-colour drawing and the whole point of the
    section. Two callers pass "new" instead, for the same reason in two guises: the delta
    is not trustworthy enough to be the first thing read. The UX audit's is a pixel mask
    over a screenshot, and a half-ghosted photo of a form has to be decoded before it says
    anything; a sequence diagram's is drawn from traces, and a run that reorders two
    concurrent calls — or a generator that relabels an arrow — is reported as a change
    nobody made. In both cases the undiffed picture is the one that reads at a glance, and
    the delta stays one click away for when the question is what moved.

    Callers: `render_diagrams` below, for every PlantUML delta, and `drawio_widget_html`,
    for the hand-drawn conceptual model. Behaviour and styling live in `DGM_VIEWS_JS` / the
    stylesheet and are delegated off `document`, so a widget that lands in a section body
    is driven by the same code as one emitted here — a second implementation of this would
    drift within a week.
    """
    pair = [v for v, _ in panes if v in VIEW_WORDS]
    # Nothing to switch to: a lone "Diff" button is a control that does nothing, and a
    # frame colour-coding one state is a legend for a single entry. Emit the picture.
    if not pair:
        return "".join(body for _, body in panes)
    # A caller asking to open on a side that did not render would open on nothing at all,
    # so an absent side falls back to the delta rather than blanking the widget.
    if initial not in {v for v, _ in panes}:
        initial = "diff"
    buttons = ['<button type="button" class="dgm-diff" data-go="diff" '
               f'aria-pressed="{str(initial == "diff").lower()}" '
               'data-tip="the delta &mdash; green for what this branch added, '
               'red and struck through for what it removed">Diff</button>']
    if pair:
        buttons.append(
            '<button type="button" class="dgm-newold" data-go="newold" '
            f'aria-pressed="{str(initial != "diff").lower()}" '
            'data-tip="the diagram itself, undiffed. Click again to swap sides. '
            'Worth reaching for whenever the delta looks wrong: a generated sequence '
            'diagram reorders concurrent calls between runs, and the differ reports that '
            'as a change.">'
            + "/".join(f'<u data-view="{v}"{' class="on"' if v == initial else ""}>'
                       f'{VIEW_WORDS[v]}</u>' for v in pair)
            + "</button>")
    panels = "".join(
        f'<div class="dgmpane" data-view="{v}"{"" if v == initial else " hidden"}>{body}</div>'
        for v, body in panes)
    return (f'<div class="dgmviews" data-state="{initial}"><div class="dgmbar">'
            + "".join(buttons) + "</div>" + panels + "</div>")


def _diagram_views(row, assets: Path, full_svg: Path, root: Path):
    """One diagram's panes: the delta (with its focus chooser inside it) plus whichever
    of the undiffed pair `puml-diff.sh` managed to render. Returns the markup and whether
    a New/Old pair exists — the header only advertises itself as a toggle when it does.

    A sequence diagram opens on `New`, a structural one on `Diff`. Not a preference: a
    structural delta is derived from two files a human wrote, so every mark in it is a
    change somebody made, while a sequence delta is derived from two *recordings*. The
    order of concurrent calls is not stable between runs and the generator's own labels
    move under it, so the differ reliably reports arrows nobody touched — which is what
    the New/Old button's tooltip has always warned about, and what the guide keeps having
    to say out loud next to the picture. Opening on the recording itself puts the reader
    in front of something true first; the delta is one click away, where the claim it
    makes can be taken with the caveat it needs."""
    panes = [("diff", _focus_views(row, assets, full_svg, root))]
    for view, column in (("new", "new_svg"), ("old", "old_svg")):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'))
    if len(panes) == 1:
        return panes[0][1], False
    return dgm_views_html(panes, initial="new" if row.get("kind") == "sequence" else "diff"), True


# `{{drawio:conceptual}}` — the hand-drawn diagram's three pictures, read off
# `.human-review/assets/` at build time.
#
# It exists because this one diagram is the only thing on the page the report *asks the
# reader to go and change*: red is automation's to-do, and the picture is supposed to look
# different once a human has re-laid it out in draw.io. Markup pasted into content.json
# freezes it — the reader re-draws the map, rebuilds the page, and still sees the drawing
# that was current when a model last wrote the section, with a legend still promising a
# layout that has since been drawn. The token keeps the pictures in the files
# `drawio-diff.py` writes, so re-running that step and rebuilding is the whole refresh.
DRAWIO_TOKEN = re.compile(r"\{\{drawio:(?P<name>[A-Za-z0-9_.-]+)\}\}")

# Meanings, not colours: the swatch is already the colour, so the bold goes on the one
# thing the reader cannot see.
#
# The to-do row names the state and stops. It used to carry the whole instruction as well
# — what the red is, and to go open it in draw.io and turn every line black — and that
# sentence read as a puzzle at the size a legend is read at: the reader is standing in
# front of a picture, and the legend was explaining a workflow. The instruction is not
# lost, it is written on the map itself, in red, by `conceptual-model-patch.py`, where
# the person who can act on it is already looking. Both rows are read off the verdict, so
# the page stops saying it the moment it stops being true.
#
# The green row stops at "added by this PR" for the same reason. "New against the base
# branch" was the definition of "added by this PR" — the same fact, restated for a reader
# who has evidently understood it, since they are reading a legend on a diff.
CM_LEGEND_NEW = '<span class="new"><i></i><b>added by this PR</b></span>'

CM_LEGEND_TODO = ('<span class="todo"><i></i>'
                  "<b>still waiting for a manual re-layout</b></span>")


def drawio_widget_html(name: str, assets: Path, root: Path, rebuild: str = "") -> str:
    """The Diff / New / Old widget for one `drawio-diff.py` output set.

    Which pane it opens on is not a style choice, it is a reading of the verdict: while
    anything in the drawing is still red, the delta is a picture of automation's routing
    rather than of the change, and `New` is the pane that answers first. Once the layout
    has been drawn by hand there is no red left, and `Diff` earns the open — which is the
    same rule the author used to have to remember and re-type after every re-layout.
    """
    verdict = {}
    vfile = assets / f"{name}-diff.json"
    if vfile.is_file():
        verdict = json.loads(vfile.read_text(encoding="utf-8"))
    red = bool(verdict.get("red"))
    green = any(not a.get("already_red") for a in verdict.get("added") or [])

    legend = (f'<p class="cmlegend">{CM_LEGEND_NEW if green else ""}'
              f'{CM_LEGEND_TODO if red else ""}</p>') if (green or red) else ""
    # Repeated under `New`, where the red is on screen with nothing else to explain it.
    # The green is not: nothing is coloured green in the undiffed drawing.
    todo_only = f'<p class="cmlegend">{CM_LEGEND_TODO}</p>' if red else ""

    panes = []
    for view, suffix, tail in (("diff", "diff", legend),
                               ("new", "new", todo_only),
                               ("old", "original", "")):
        svg = assets / f"{name}-{suffix}.svg"
        if svg.is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(svg, root)}</div>{tail}'))
    if not panes:
        print(f"[review] no {name}-*.svg under {assets} — run the diagrams step",
              file=sys.stderr)
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-diff.svg</code></p>')
    return (dgm_views_html(panes, initial="new" if red else "diff")
            + rerun_html(verdict.get("rerun"), rebuild, name,
                         verdict.get("drawio_url") or "",
                         verdict.get("drawio_web_url") or "",
                         verdict.get("redraw"), verdict.get("revert"),
                         verdict.get("reveal")))


def drawio_open_html(app_url: str, web_url: str = "") -> str:
    """The two ways to edit the drawing, as links.

    Under the picture and not inside it: a rendered diagram cannot show a cursor, so an
    invitation painted onto the map has to spell out in words that it is clickable — and
    then it is a sentence about tooling sitting on the drawing, re-read every time the
    reader looks at the boxes. In HTML it is just a link, and it can afford to be two.

    They are not the same offer, which is why both are named rather than one being "the"
    link. The **App** opens the file on disk, so an edit lands where the rerun command
    can pick it up. The **Web** editor opens a copy carried in the URL — nothing is
    uploaded, and nothing it saves reaches the repository either. It is the answer when
    draw.io is not installed on this machine, and the reader can tell which is which
    before clicking rather than after.
    """
    links = []
    if app_url:
        links.append(f'<a href="{html.escape(app_url, quote=True)}">App ↗</a>')
    if web_url:
        links.append(f'<a href="{html.escape(web_url, quote=True)}" '
                     'target="_blank" rel="noopener">Web ↗</a>')
    # The product is named once and the two editors are named after it — `draw.io App or
    # draw.io Web` said the brand twice in six words, which is the half of the phrase that
    # carries no information: the choice the reader is making is App or Web.
    return "draw.io " + " or ".join(links) if links else ""


# What a click-to-run offer says on a static page — where it stays visible and explains
# itself rather than being absent from one copy of the report and present in the other.
# `data-tip-served` is what the same button says where it actually works; the probe swaps
# them, so the two readings live next to each other here instead of in the script.
STATIC_RUN_TIP = ("This copy of the report is static, so nothing here can run: serve the "
                  "page — the static badge at the top copies the line that does — and "
                  "this button does the job.")


def reveal_html(reveal: dict | None, name: str) -> str:
    """"this diagram" as a handle on the file, rather than as a noun.

    The sentence already says *edit* it and *re-render* it; the one thing it says nothing
    about is where the thing actually is. And the two words that name it were sitting right
    there, unclickable, at the front of the line. So the subject of the sentence became the
    control: press it and the file is selected on disk, in the window the reader would have
    gone looking for it in.

    Not folded, and not paired with a command to read: revealing a file changes nothing and
    costs nothing to press, which is the one offer under this picture that needs no second
    click and no `reload`. Where it cannot run — a static copy, or a verdict written before
    the command was recorded — the two words are two words again, and the sentence reads
    exactly as it did before any of this.
    """
    if not reveal or not reveal.get("command") or not name:
        return "this diagram"
    aid = declare_action(f"drawio-reveal:{name}", reveal["command"],
                         label=f"Show {name} on disk")
    where = reveal.get("in") or "the file manager"
    # Two words and a plain copy of them, and the same rule as every other offer on this
    # line picks: where nothing can run, the subject of the sentence is a noun again rather
    # than a control that explains why it does not work. There is no `run this` fallback
    # here because there is nothing to fall back to — `open -R` is not a step in anyone's
    # workflow, it is a shortcut for one, and a reader without a server has their own.
    return ('<span class="offer">'
            f'<button type="button" class="runhere" '
            f'data-action="{html.escape(aid, quote=True)}" '
            f'data-tip="{html.escape(STATIC_RUN_TIP, quote=True)}" '
            f'data-tip-served="Selects the file on disk, in {html.escape(where, quote=True)}"'
            '>this diagram</button>'
            '<span class="plainword">this diagram</span></span>')


#: What a static click does, appended to the static tooltip of any offer that carries its
#: command. A sentence and not a word, because "copies it" leaves out the half a reader
#: needs — that the page cannot run it here, and that the copy is therefore the offer.
STATIC_COPIES = ("This copy of the report cannot run it, so clicking here copies the "
                 "command instead.")

#: The copy glyph's hover. Says what the click does and then the line it will put on the
#: clipboard, which is the only place a command appears on this page in full.
COPY_TIP = "Copy command to paste in terminal"


def command_html(cmd: str, action_id: str | None = None, *, tip: str = "",
                 running: str = "") -> str:
    """The affordances of one shell command, beside the control that describes it.

    **The command itself is not printed.** It used to be, in a parenthesis, and it was the
    right instinct and the wrong artifact: a review page is prose and pictures, and a
    forty-to-two-hundred-character absolute path in the middle of a sentence is a wall the
    eye has to climb over on every read. The information was for the one reader in ten who
    wanted to paste it, charged to all ten, forever.

    So what is on screen is the *offer*, not its implementation: one glyph, and it is the
    one that is true here. Off disk, out of the zip and on GitHub Pages that is the
    clipboard, and the command lives in its hover — the only place the line appears in
    full on this page. Served, the probe raises the run glyph and takes the clipboard
    away, because the command has somewhere to go.

    Both are in the markup and only one is ever on screen, which is not the same as
    rendering both. They were both visible for a while and the pair asked the reader a
    question the page already knew the answer to — press this and it runs, press that and
    you get a line to run somewhere else, and neither glyph said which was which. A
    control whose whole job is to say *what this copy of the report can do* must not need
    a click to say it.

    `action_id` is what the server will be asked for: a manifest id, or one of the two
    server-owned verbs (`__rerun__`, `__rerun_ai__`, which `window.HR.can` answers off the
    probe rather than out of the manifest). No id means no run glyph and the clipboard
    stays, which is the honest rendering of a command the build did not declare — the line
    is real, and nothing here can run it.
    """
    quoted = html.escape(cmd, quote=True)
    # The command in the hover, on its own line after the sentence. This is the only place
    # it appears in full, so it is not truncated: a half-copied command in a tooltip is
    # worse than none, because the reader cannot tell which half they are looking at.
    copy_tip = html.escape(f"{COPY_TIP}:\n{cmd}", quote=True)
    out = [f'<span class="cmd">'
           f'<button type="button" class="copycmd cmd-copy" data-copy="{quoted}" '
           f'data-tip="{copy_tip}" aria-label="{html.escape(COPY_TIP, quote=True)}">'
           f'{CMD_COPY}</button>']
    if action_id:
        # `runhere` because the page's existing handler runs a `runhere` with a
        # `data-action` through the action server — spinner, log tail and reload included.
        # `hidden` from the start and raised by the probe, like every other control here.
        play_tip = html.escape(
            ((tip.rstrip(".") + ". ") if tip else "")
            + f"Runs it through the server serving this page:\n{cmd}", quote=True)
        out.append('<button type="button" class="runhere cmd-run" hidden '
                   f'data-action="{html.escape(action_id, quote=True)}" '
                   f'data-tip="{play_tip}"'
                   + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
                   + f' aria-label="Run this command">{CMD_RUN}</button>')
    out.append('</span>')
    return "".join(out)


def offer_words_html(label: str, action_id: str, static_tip: str, served_tip: str,
                     running: str = "", cmd: str = "", pill: bool = False) -> str:
    """The words of an offer — one control, in both copies of the report.

    This replaces the pair `_run_or_read` used to render. That function put *two* controls
    in the markup and let CSS choose: `click here` served, `run this` off disk, the second
    one opening a fold with the command in it. The whole arrangement existed to answer "the
    command is only useful in one of the two copies", and it answered it by making the same
    control read differently in each — so a reader could not learn the page, and "what does
    this button run" had no answer in the copy where the button worked.

    One control, and the *click* differs rather than the words:

      * **served**, it runs the command through the review server;
      * **off disk**, it copies it, with a `copied` toast. That is the only thing that copy
        of the report can do with the command, so it is what the click does. A control
        whose whole answer is a sentence explaining why it did nothing is a control readers
        learn to stop pressing.

    The copy glyph beside it (`command_html`) is what keeps that from being a magic trick:
    it is the signal that a click here copies something, and its hover carries the line.

    `pill` dresses it as a button rather than as an underlined word. For the offers in the
    aftermath band, which are the page's answer to "somebody changed the code after the
    review was written" — a fact in a red band, with two things to do about it. Inline
    links inside that sentence were the same weight as the prose around them and were read
    as part of it; the two things to do are the point of the band.
    """
    tip = f"{static_tip} {STATIC_COPIES}" if cmd else static_tip
    cls = "runhere offer-words" + (" offer-pill" if pill else "")
    return (f'<button type="button" class="{cls}" '
            f'data-action="{html.escape(action_id, quote=True)}" '
            + (f'data-copy="{html.escape(cmd, quote=True)}" ' if cmd else "")
            + f'data-tip="{html.escape(tip, quote=True)}" '
            f'data-tip-served="{html.escape(served_tip, quote=True)}"'
            + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
            + f'>{html.escape(label)}</button>')


def regenerate_html(redraw: dict | None, rerun: dict, rebuild: str,
                    name: str) -> tuple[str, str | None]:
    """The one way back: put the machine's own drawing there, and rebuild around it.

    There used to be two of these and they were the same offer to the reader. *Undo your
    edits* walked back to the newest committed drawing; *start over* restored the base and
    re-ran the repository's patch script, which draws what the code has and the map lacks.
    Two commands, two tooltips, two paragraphs of this docstring explaining that they land
    in different places — and every reader who pressed either was asking one question:
    **give me back the diagram the machine makes.** Only the second answers it. The first
    hands back a human's layout from an earlier commit, which is a different drawing and
    is not "generated" in any sense the reader meant.

    So the one that runs the generator stays, and it is named after what it produces rather
    than after the gesture that gets you there: *Regenerate the diagram*, not *start over*,
    which is a direction and not a destination.

    It is destructive — the hand-drawn layout goes — so it banks the work first. The
    command that went away was the survivable one (`git stash push` before the checkout),
    and losing that property along with it would be a bad trade for a simpler line, so the
    stash comes across. `git stash push -- <path>` exits 0 with "No local changes to save"
    when there is nothing to bank, so it costs a clean tree nothing.

    Returns `(offer, action_id)`; `("", None)` where the repository declared no patch
    script. That script is the reviewed project's, not this tool's — guessing it from a
    naming convention and running it on a reader's click is not a trade worth making.
    """
    if not redraw or not redraw.get("command"):
        return "", None
    # Five stages, and the middle three are the reason this is one offer and not three:
    # banking the layout, restoring the base drawing and redrawing it all change the file
    # on disk, and the picture in this page is an inlined SVG that only `drawio-diff.py`
    # rewrites. Stopping before the last two would leave the reader looking at their own
    # layout with a green tick beside it.
    stash = ""
    if redraw.get("diagram"):
        stash = (f"git stash push -m {shlex.quote(f'human-review: layout of {name}')} -- "
                 f"{shlex.quote(redraw['diagram'])} && ")
    line = (f'cd {shlex.quote(redraw["cwd"])} \\\n  && {stash}{redraw["command"]} \\\n'
            f'  && {rerun["command"]} \\\n  && {rebuild}')
    aid = None
    if name:
        aid = declare_action(f"drawio-redraw:{name}", line, reload=True,
                             label=f"Regenerate {name} from the repository's own script")
    base = redraw.get("base") or "the base branch"
    tip = (f"Puts automation's own drawing back: restores the file to {base} and runs the "
           "repository's script over it, which draws what the code has and the map lacks "
           "— in red, as a to-do. Your layout is banked with `git stash`, not binned.")
    served = ("Runs it here and reloads, with automation's drawing back. Your layout goes "
              "to the git stash.")
    return (offer_words_html("Regenerate the diagram", aid or "", tip, served,
                             "Putting automation's drawing back…", cmd=line, pill=True)
            + command_html(line, aid, tip=served,
                           running="Putting automation's drawing back…"), aid)


def rerun_html(rerun: dict | None, rebuild: str, name: str = "",
               app_url: str = "", web_url: str = "", redraw: dict | None = None,
               revert: dict | None = None, reveal: dict | None = None) -> str:
    """Under the drawing: where to edit it, and the two things to do about it afterwards.

    The command is not a convenience. The picture above is inlined into the HTML, and it
    has to be: the boxes are links into the classes they name and the to-do note is a link
    into draw.io, and an SVG loaded through `<img src>` renders those as decoration — the
    reader can see them and cannot click them. So the file on disk and the picture in the
    page are two artefacts, and reloading the browser only ever refreshes the second one.
    That is a thing the page owes the reader an answer to, at the moment they need it, in
    the form of something they can press.

    **A sentence, then two buttons.** It was one sentence with everything inside it, and
    by the time the offers had grown their glyphs it carried seven underlined runs of text
    — `this diagram`, `App`, `Web`, `click here`, `undo your edits`, `start over` — which
    read as a wall of links with no rank. Underlining is for the actions now and the
    actions are pills; the places to go (the file, the two editors) are plain links that
    underline on hover. One kind of emphasis, one meaning.

    **`revert` is accepted and ignored**, so a verdict written by an older `drawio-diff.py`
    still builds. See `regenerate_html` for why there is one way back rather than two.

    `rerun` is what `drawio-diff.py` recorded about its own invocation; `rebuild` is how
    this build was started. Neither is reconstructed here — a guessed command that does
    not work is worse than no command, because it is tried first.
    """
    edit = drawio_open_html(app_url, web_url)
    it = reveal_html(reveal, name)
    if not rerun or not rerun.get("command"):
        return f'<p class="dgm-open">Edit {it} in {edit}</p>' if edit else ""
    line = f'cd {shlex.quote(rerun["cwd"])} \\\n  && {rerun["command"]} \\\n  && {rebuild}'
    # Per diagram, because a page can carry several and each one reruns its own. The id
    # is the diagram's name for the same reason every other handle on this page is: so a
    # button that has been on screen since the last build cannot end up running the
    # command belonging to a different picture.
    #
    # `reload`, because the last stage of this line rewrites the very file the browser is
    # displaying. Leaving the reader on the old bytes with a green tick beside them would
    # be the worst possible outcome: the page would look like it had picked the edit up.
    aid = None
    if name:
        aid = declare_action(f"drawio:{name}", line, reload=True,
                             label=f"Re-render {name} and rebuild this page")
    served = "Runs it here, then reloads with the new picture"
    again, _ = regenerate_html(redraw, rerun, rebuild, name)
    # The status line, under the buttons and empty until something is running. This is the
    # whole of the answer to the complaint that produced it: the command behind *Update the
    # report* re-renders a diagram and rebuilds the page, which is seconds of nothing
    # whatever, in front of a control that gave no sign it had been pressed. A reader with
    # no feedback does not wait patiently — they press it again, and then they stop
    # believing the page. What goes in it is the command's own last line, polled from
    # `/__run_status__`, so it says what is actually happening rather than a guess.
    #
    # In the markup of both copies of the report. Off disk nothing fills it, which costs
    # a hidden empty paragraph.
    # The sentence, when there is somewhere to send them. With no editor link declared
    # there is no "in draw.io App ↗" to write, and `Edit this diagram in .` is worse than
    # silence — but the file itself is still worth naming if the verdict recorded how to
    # reveal it.
    where = (f'<p class="dgm-open">Edit {it} in {edit}.</p>' if edit
             else (f'<p class="dgm-open">Edit {it}.</p>' if reveal else ""))
    return ('<div class="rerun">' + where
            + '<div class="rerun-acts">'
            + offer_words_html("Update the report", aid or "", STATIC_RUN_TIP, served,
                               "Re-rendering the diagram…", cmd=line, pill=True)
            + command_html(line, aid, tip=served, running="Re-rendering the diagram…")
            + again
            + '</div>'
            + '<p class="runstatus" hidden role="status" aria-live="polite">'
              '<b class="rs-phase"></b><span class="rs-tail"></span></p>'
            + '</div>')


def expand_drawio(text: str, out_dir: Path, root: Path, rebuild: str) -> str:
    return DRAWIO_TOKEN.sub(
        lambda m: drawio_widget_html(m["name"], out_dir / "assets", root, rebuild), text)


def _pretty(name: str) -> str:
    """`DomainModel` is a filename; `Domain Model` is a heading. Split the camel hump,
    which leaves acronyms (DB) and already-spaced names untouched."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


DGM_SRC_ANCHOR = re.compile(r'<a class="dgm-src"(?P<attrs>[^>]*)>(?P<face>[^<]+)</a>')


def shorten_dgm_src(markup: str) -> str:
    """A diagram header names its file by **name**, with the path on hover.

    `petclinic-backend/docs/ConceptualModel.drawio.png` spends two segments on where the
    repository keeps its documents before reaching the one word that answers "which
    drawing is this?" — and it does it in the header of a card whose title already said
    *Conceptual Model*. The name alone is the same answer in a quarter of the width; the
    path is still one hover away, which is where a reader goes only when they want to
    find the file rather than read the picture.

    This is the rule `srcbar_html` already applies to every quoted block on the page,
    down to the wording of the tip, so the two rows read the same way. It runs over
    rendered markup rather than at each call site because the conceptual model's header
    is written by hand into `content.json` — a rule enforced only in `_source_link`
    would hold for the generated PlantUML cards and quietly not for the one card the
    reader is being asked to go and edit.

    Idempotent: a face with no slash left in it is already short (or is a file at the
    repository root, which has no path to move)."""
    def one(m: re.Match) -> str:
        rel = html.unescape(m["face"])
        if "/" not in rel:
            return m.group(0)
        attrs = re.sub(r'\s+data-tip="[^"]*"', "", m["attrs"])
        tip = html.escape(f"Open in VS Code: {rel}", quote=True)
        return (f'<a class="dgm-src"{attrs} data-tip="{tip}">'
                f'{html.escape(Path(rel).name)}</a>')
    return DGM_SRC_ANCHOR.sub(one, markup)


def _source_link(rel: str, root: Path) -> str:
    """The path already shown on the right of the header, made the link to the file.

    It used to be plain text with a second `<a>name.puml</a>` under the title — two
    controls for one destination, and the shorter of the two said less."""
    if (root / rel).is_file():
        return shorten_dgm_src(
            f'<a class="dgm-src" href="vscode://file/{(root / rel).resolve()}:1:1">'
            f'{html.escape(rel)}</a>')
    return f'<span>{html.escape(rel)}</span>'


def _provenance(rel: str, root: Path) -> str:
    """Links back to what produced a diagram: the test that generated it, and the .puml.

    A sequence diagram is evidence only if the reviewer can reach the scenario behind it.
    `test_of_genseq` is what derives that — from the picture's own `src://` handle, or
    failing that from its name — so the guide never has to be told, and the link survives
    both the per-scenario naming and the generator filing its output away from the test."""
    links = []
    if rel.endswith('.genseq.puml'):
        test = test_of_genseq(rel, root)
        if (root / test).is_file():
            links.append(f'<a class="srcref" href="vscode://file/{(root / test).resolve()}:1:1">'
                          f'generated by {html.escape(Path(test).name)}</a>')
    return ('<p class="prov">' + " ".join(links) + '</p>') if links else ''


# Which radius a reviewer meets first — the change and nothing else.
#
# It opened on the whole diagram for a while, on the reasoning that a pruned view is a
# claim that the rest does not matter. In practice the whole DB and DomainModel deltas are
# a wall of forty unchanged entities with the change somewhere inside, so it moved to one
# hop of context, and then to none: the first question a delta has to answer is *what
# changed*, and every element beside it is a candidate the eye still has to rule out. Zero
# is the only level that cannot mislead about that. Each wider radius is one click away,
# and the reader takes it the moment the change alone does not explain itself.
DEFAULT_FOCUS = "0"


def _focus_views(row, assets: Path, full_svg: Path, root: Path) -> str:
    """The delta at each focus level, one visible at a time, with the chooser above them.

    All of them are inlined rather than fetched on demand: the guide has to survive being
    emailed as a single file, and a chooser whose other options 404 is worse than none.
    """
    levels = []
    for pair in (row.get("focus") or "").split(","):
        level, sep, name = pair.partition(":")
        svg = assets / name
        if sep and svg.is_file():
            levels.append((level, svg))
    levels.append(("all", full_svg))

    if len(levels) == 1:                       # sequence diagrams, and anything unpruned
        return f'<div class="svgbox">{inline_svg(full_svg, root)}</div>'

    default = DEFAULT_FOCUS if any(l == DEFAULT_FOCUS for l, _ in levels) else "all"
    buttons = "".join(
        f'<button type="button" data-level="{html.escape(level)}" '
        f'aria-pressed="{"true" if level == default else "false"}">{html.escape(level)}</button>'
        for level, _ in levels
    )
    boxes = "".join(
        f'<div class="svgbox" data-level="{html.escape(level)}"'
        f'{"" if level == default else " hidden"}>{inline_svg(svg, root)}</div>'
        for level, svg in levels
    )
    return (
        '<div class="focus"><span class="lbl">Diff + extra neighbours:</span>'
        + buttons + "</div>" + boxes
    )


#: `[[src://<test>:<line>{tip} <scenario title>]]` — the handle the sequence generators
#: leave on the scenario a picture draws. It is the only record of *which* tests in a file
#: carry `@generate_sequence`: the tag is in the source, but the generator is what decides
#: it produced a drawing, and the drawing is what this page can link to.
#:
#: Two places carry it, and both are read. `title …` is where it lives now that a picture
#: is one scenario — the scenario names the diagram. `== … ==` is the chapter divider a
#: picture drawn per *file* put above each scenario inside it, which is still what the
#: committed diagrams of a repository that has not been through the generator split look
#: like, and what `renderDiagram` still writes when handed several scenarios at once.
GENSEQ_HANDLE = re.compile(
    r"^(?:title|==)\s*\[\[src://(?P<path>[^\s:{\]]+):(?P<line>\d+)"
    r"(?:\{(?P<tip>[^}]*)\})?\s*(?P<title>[^\]]*?)\s*\]\]")


@functools.lru_cache(maxsize=None)
def _declared_test(puml: Path, root: Path) -> str | None:
    """The test a diagram names on its own title line, if it names one this checkout has.

    The generator writes `title [[src://<repo-relative test>:<line>{…} <scenario>]]`, which
    is the only statement of provenance that survives the file being moved: a generator is
    free to file its output wherever it likes — beside the test, or in one `generated/`
    directory — and the picture still says what drew it.
    """
    try:
        with puml.open(encoding="utf-8") as fh:
            for line in fh:
                m = GENSEQ_HANDLE.match(line.strip())
                if m and (root / m["path"]).is_file():
                    return m["path"]
                if line.startswith(("participant ", "actor ")):
                    break     # past the header: the arrows' handles are not the test
    except OSError:
        pass
    return None


def test_of_genseq(source: str, root: Path) -> str:
    """Which test a generated sequence was drawn from.

    The diagram is asked first, because it says so itself — see `_declared_test`. That is
    what lets a repository move its generated diagrams out of the source tree without the
    pairing on this page quietly pointing at files that are not there.

    Everything below is the older, path-based derivation, kept for diagrams that carry no
    handle (a run whose working tree the generator could not read) and for pages built
    before the handle existed. It used to be the file name with `.genseq.puml` cut off,
    because there was one diagram per test file. There is now one per scenario, named
    `<test>.<scenario-slug>.genseq.puml` — and a test file has dots of its own
    (`add-visit.spec.ts`), so the slug cannot be told from the extension by looking. The
    checkout is asked instead: cut the suffix, and if what is left is not a file, cut one
    more dotted segment and try again.

    A diagram whose test is not in this checkout at all — the case `_unquoted_note` exists
    for — cannot be checked that way, so the slug is cut on its shape: all lowercase,
    digits and dashes, over something that still has an extension.
    """
    declared = _declared_test(root / source, root) if source.endswith(".genseq.puml") else None
    if declared:
        return declared
    rel = source[: -len(".genseq.puml")] if source.endswith(".genseq.puml") else source
    if (root / rel).is_file():
        return rel
    head, dot, slug = rel.rpartition(".")
    if dot and (root / head).is_file():
        return head
    if dot and "." in head and re.fullmatch(r"[a-z0-9-]+", slug):
        return head
    return rel


@functools.lru_cache(maxsize=None)
def genseq_by_test(root: Path) -> dict[str, tuple[str, ...]]:
    """Every generated sequence in this checkout, grouped by the test that drew it.

    It used to be a glob next to the test — `<test>.*.genseq.puml` in the test's own
    directory — which was true of the only generator that existed and false the moment a
    repository filed its diagrams anywhere else (petclinic moved both suites' output into
    `petclinic-test/generated/`). The unchanged diagrams then silently stopped appearing
    on the page: not an error, just a tab that showed the changed ones and quietly dropped
    the rest, which is the worst way for a page of evidence to be wrong.

    One walk per build, shared by every tab, skipping the directories nothing generated
    ever lives in. Sorted, so the page orders the pictures the same way on every run.
    """
    found: dict[str, list[str]] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".genseq.puml"):
                continue
            rel = str((Path(folder) / name).relative_to(root))
            found.setdefault(test_of_genseq(rel, root), []).append(rel)
    return {test: tuple(sorted(pumls)) for test, pumls in found.items()}


def pair_anchor(rel: str) -> str:
    """The id of the pair a sequence is drawn in, derived from the diagram's own path.

    Derived rather than counted, because the thing that links to it — the 🕵️ on a
    covering-tests row, a tab away — knows the test and nothing else about this tab. A
    path is unique inside a checkout, so the slug is too. It is the *diagram's* path that
    is passed now, one per scenario, so two scenarios of one file get two anchors."""
    return "seq-" + re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")


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


def _cat_chip(cat: str | None) -> str:
    """The kind, as the chip the Tests tab wears — same words, same palette, same pill.

    Copied rather than shared, like `FILE_PAGE` above it: the Tests tab is an included
    asset that builds its own markup, and the two will not be made to import from each
    other. What is shared is the decision, which is written down in `TEST_CATS`."""
    if cat not in TEST_CATS:
        return ""
    label, what = TEST_CATS[cat]
    return (f'<span class="testcat" data-cat="{cat}"'
            f' data-tip="{html.escape(label, quote=True)} &mdash; '
            f'{html.escape(what, quote=True)}">{html.escape(label)}</span>')


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
            f'{_cat_chip(cat)}{name}</summary>'
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
    content file, not two), and then it goes under *both*. The alternative is to pick one
    and leave the other pair claiming its test is "not excerpted here", which is false —
    and the pairs are folded shut, so a block quoted twice costs the reader nothing until
    they ask for it.

    The excerpts come back as they went in, unrendered: which excerpt belongs to which
    picture is the decision this function exists to make, and it is one a test can check
    by reading refs rather than by searching rendered HTML for line numbers.
    """
    mine = [x for x in snippets if x["ref"].rpartition(":")[0] == test_rel]
    used.update(id(x) for x in mine)
    quoted = {rel: [] for rel, _ in entries}
    lines_of = {rel: [ln for ln, _ in _scenarios_drawn(rel, test_rel, root)]
                for rel, _ in entries}
    first = entries[0][0]
    for x in mine:
        spans = _line_spans(x["ref"].rpartition(":")[2])
        owners = [rel for rel, _ in entries
                  if any(lo <= ln <= hi for lo, hi in spans for ln in lines_of[rel])]
        for owner in (owners or [first]):
            quoted[owner].append(x)
    return quoted


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
    drawn = genseq_by_test(root)
    for test_rel in dict.fromkeys(x["ref"].rpartition(":")[0] for x in snippets):
        for rel in drawn.get(test_rel, ()):
            if any(rel == q for q, _ in plan.get(test_rel, [])):
                continue          # this branch changed it: it is already a row above
            plan_add(rel, None)
            unchanged += 1

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
            pieces.append(render_diagrams(merged, root, out_dir, [row], bare=test_rel)
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
            len(rows) + unchanged + len(orphaned), len(rows))


def select_rows(rows, block) -> list:
    """The manifest rows one diagram block is responsible for.

    Tabs split the gallery by what a diagram *answers* — sequence diagrams sit next to
    the tests that generated them, DB and DomainModel next to each other — so a block
    names either the families it takes (`kind`) or the diagrams themselves (`only`)."""
    kinds = block.get("kind")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        rows = [r for r in rows if r["kind"] in kinds]
    only = block.get("only")
    if only:
        rows = [r for r in rows if r["name"] in only]
    if block.get("except"):
        rows = [r for r in rows if r["name"] not in block["except"]]
    return rows


def render_diagrams(spec, root: Path, out_dir: Path, rows=None, bare: str = "") -> str:
    """`bare` is the test file a pair's heading already names.

    A sequence diagram inside a test pair used to print three answers to one question in
    four centimetres: a title that was the test's file name with `.genseq` on the end, a
    `generated by <test>` line under it, and the `.puml` path on the right — above a fold
    whose summary was that same file name. Only the last is news. So the title and the
    provenance line go, and the href they carried for the scenario links rides on the card
    as `data-test-src` instead."""
    manifest = out_dir / spec.get("manifest", "assets/diagrams/MANIFEST.tsv")
    if rows is None:
        rows = read_manifest(manifest)
        if spec.get("only"):
            rows = [r for r in rows if r["name"] in spec["only"]]
    if not rows:
        return '<p class="sub">No PlantUML diagram changed on this branch.</p>'
    notes = spec.get("notes", {})
    order = {"structural": 0, "sequence": 1}
    # An explicit `only` is a running order, not just a filter: an author who writes
    # ["DomainModel", "DB"] means the domain first. Alphabetical only decides the rest.
    wanted = spec.get("only") or []
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 9),
                                       wanted.index(r["name"]) if r["name"] in wanted else 99,
                                       r["name"]))
    parts = []
    for r in rows:
        note = notes.get(r["name"], "")
        svg_rel = manifest.parent / r["svg"] if r.get("svg") else None
        if svg_rel and svg_rel.is_file():
            body, toggles = _diagram_views(r, manifest.parent, svg_rel, root)
        else:
            body, toggles = (f'<p class="sub">not rendered — see '
                             f'<code>{html.escape(r["diff_puml"])}</code></p>', False)
        test_src = (f' data-test-src="vscode://file/{(root / bare).resolve()}:1:1"'
                    if bare and (root / bare).is_file() else "")
        parts.append(
            f'<div class="diagram{" dgm-toggles" if toggles else ""}{" dgm-bare" if bare else ""}"'
            f'{test_src}>'
            f'<div class="head">'
            + ("" if bare else f'<b>{html.escape(_pretty(r["name"]))}</b>')
            # A badge earns its place by saying something surprising. "modified" is what
            # a diagram in a delta gallery always is, and "structural" is legible from the
            # picture — so only the states that carry information get one.
            + (f'<span class="badge {"sev-high" if r["status"] == "added" else "sev-low"}">'
               f'{html.escape(r["status"])}</span>' if r["status"] != "modified" else "")
            + _source_link(r["source"], root) + '</div>'
            + (f"<p>{note}</p>" if note else "")
            + ("" if bare else _provenance(r["source"], root))
            + genseq_details_at_render(r, manifest.parent, root)
            + genseq_details_at_base(r, manifest.parent, root)
            + body + '</div>'
        )
    return "\n".join(parts)


#: Where `review-points.py` leaves what it parsed off the branch. A content file asks for
#: it with `{"auto": "review-points"}` on `findings` / `autofixes` / `assumptions` — the
#: same convention `diffstat` and `tests` already use for a number nobody should type.
REVIEW_POINTS_JSON = "review-points.json"

#: Which pile each `{"auto": …}` key fills, and the heading `review-points.md` writes it
#: under. Kept here rather than imported from the parser: this is the build's side of the
#: contract, and a rename in either file has to be a deliberate change in both.
POINTS_PILES = {"autofixes": "Fixed", "findings": "Ignored", "assumptions": "Assumptions"}


def resolve_review_points(spec: dict, out_dir: Path) -> dict | None:
    """Fill the piles the content file delegated to `review-points.md`, in place.

    `content.json` stops being the judgement here. Before this, a model read the passes,
    decided what to fix and what to leave, wrote the three piles into the content file,
    and the page rendered its prose — so the record of the review was produced by the same
    conversation that produced the page, minutes after the fact, and nothing outside that
    file could corroborate a word of it. Now the coding agent writes `review-points.md`
    and commits it with the fixes, and this reads it: the content file keeps the layout and
    the ledes, which are the page's, and hands over the three piles, which are the
    branch's.

    The answer is recorded on the spec as `_reviewPoints` as well as returned, because
    every reader of it is downstream of one dict being threaded through eight call layers:
    the ledes, the piles and the band all have to agree about whether the record exists,
    and the spec is the thing all three already have in hand.

    None when no pile asked for it — an older content file with its piles written out is
    rendered exactly as it always was. Otherwise the dict says what the page now has to be
    honest about: `missing` when the file is not on the branch at all (and the piles are
    emptied, so nothing renders items nobody recorded), `sections` so an empty pile can
    say whether the file had no such section or an empty one.

    An absent file empties the piles rather than leaving whatever the content file happened
    to carry. A page that renders last week's findings under this week's diff is the one
    failure mode worse than a page that renders none.
    """
    asked = {k for k in POINTS_PILES
             if isinstance(spec.get(k), dict) and spec[k].get("auto") == "review-points"}
    if not asked:
        spec["_reviewPoints"] = None
        return None
    path = out_dir / (spec.get("reviewPoints") or REVIEW_POINTS_JSON)
    doc = None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        for key in asked:
            spec[key] = []
        # Mode C, in the layout itself rather than at render time, so the counts line and
        # the pile agree. With no record on the branch the conversation that wrote the code
        # genuinely could not be asked — the thing that would have answered was never
        # written down — and the lede has to say that instead of counting an empty pile to
        # zero. `0 assumptions` is the good news ("it was asked, and named nothing"); this
        # is the other one.
        block = _assumptions_block(spec)
        if block is not None:
            block["mode"] = "C"
        print(f"[review] no {path.name} — the Review tab will say that nothing on this "
              "branch records what was reviewed, fixed or declined. Run "
              "run-steps.py --only reviewpoints, or accept the band: a branch nobody "
              "reviewed is a real state.", file=sys.stderr)
        spec["_reviewPoints"] = {"missing": True, "asked": asked, "sections": {},
                                 "path": path.name}
        return spec["_reviewPoints"]
    for key in asked:
        items = doc.get(key)
        spec[key] = items if isinstance(items, list) else []
    for warning in doc.get("warnings") or []:
        print(f"[review] review-points: {warning}", file=sys.stderr)
    spec["_reviewPoints"] = {
        "missing": False, "asked": asked, "sections": doc.get("sections") or {},
        "source": doc.get("source") or "review-points.md",
        "fixed_in": doc.get("fixed_in"), "meta": doc.get("meta") or {},
        "path": path.name}
    return spec["_reviewPoints"]


#: The band that goes where the piles would have been. Not `render_findings([])`'s
#: "Nothing outstanding — the automated passes came back clean", which is the confident-
#: wrong page: no file is not a clean review, it is no review recorded.
POINTS_MISSING_BAND = (
    '<div class="rband rband-none" role="status">'
    '<p>No <code>review-points.md</code> on this branch — nothing records what was '
    'reviewed or declined.</p>'
    '<p class="rb-sub">The piles below are empty because the record is absent, not '
    'because the review was clean. <code>/implement-ticket</code> is what writes the '
    'file; a branch it never ran on has nothing to read.</p></div>')


def points_empty_html(kind: str, points: dict) -> str:
    """The sentence an empty pile gets when the pile is `review-points.md`'s to fill.

    Three different silences, and the reader has to be able to tell them apart: the file is
    not there, the file has no such section, or the section is there and empty. Only the
    third is news about the review; the first two are news about the record. The renderers
    are left alone — their empty states are about a *content file* that listed nothing,
    which is a fourth thing again — so the substitution happens here, at the block.
    """
    if points.get("missing"):
        return {
            "findings": '<p class="sub">Not recorded — with no <code>review-points.md</code>'
                        ' on this branch, nothing says which findings were read and '
                        'declined.</p>',
            "autofixes": '<p class="sub">Not recorded — with no <code>review-points.md</code>'
                         ' on this branch, nothing says which findings were accepted and '
                         'fixed.</p>',
        }.get(kind, "")
    heading = POINTS_PILES.get(kind, kind)
    src = html.escape(points.get("source") or "review-points.md")
    if heading not in (points.get("sections") or {}):
        return (f'<p class="sub"><code>{src}</code> has no <b>{html.escape(heading)}</b> '
                f'section, so nothing on this branch records this pile either way.</p>')
    return {
        "findings": f'<p class="sub"><code>{src}</code> declines nothing — every finding '
                    'the review raised was accepted.</p>',
        "autofixes": f'<p class="sub"><code>{src}</code> records no fix — the review '
                     'raised nothing the agent accepted.</p>',
        "assumptions": f'<p class="sub"><code>{src}</code> records no assumption: the '
                       'agent was asked what it had to decide for itself, and named '
                       'nothing.</p>',
    }.get(kind, "")


# The one list. Everything the reviewer has to act on, numbered straight through, because
# a reviewer asking "how much is there for me here?" should get one answer and not three.
# What separates the piles is the card's colour and one badge — not a restart of the
# counter, and not a second surface. There are three of them, in the order the reader can
# act on them: what only they can answer (assumptions), what they have to judge (findings),
# and what is already done (applied fixes).
#
# The list is rendered a pile at a time, and each pile has to know where the one before it
# stopped. That is this counter. It is module state, and state is a thing to justify: the
# alternative is threading a number through `render_block`, which renders blocks one at a
# time by type and has no notion that three of them belong to the same list. Reading the
# offset here rather than hard-coding "fixes come after findings" also means the numbering
# follows the order the content file puts the blocks in, whatever that order is.
_LIST_OFFSET = 0

#: Whether the counts line has already been printed on this page. The offset used to
#: answer that question on its own — it is zero exactly until the first pile renders an
#: `<ol>` — but a pile with no items renders no list and leaves it at zero, so all three
#: piles got the line. With one empty pile that was a repeated sentence; with all three
#: empty (a branch carrying no `review-points.md`, which is the common case for a branch
#: nobody ran the flow on) it was the line three times down one short tab.
_LEDE_SHOWN = False


def reset_list() -> None:
    """Start the numbering over, once per page.

    The offset is module state, so without this the second page built in one process
    continues the first one's numbering — which no build does, and every test that renders
    a pile directly would otherwise have to know about."""
    global _LIST_OFFSET, _LEDE_SHOWN
    _LIST_OFFSET = 0
    _LEDE_SHOWN = False


def _open_list(n: int) -> str:
    """The `<ol>` for the next pile, numbered on from wherever the last one stopped.

    `counter-reset` sets the counter to N so the first `counter-increment` lands on N+1 —
    the number straight after the last item already on the page."""
    global _LIST_OFFSET
    start = _LIST_OFFSET
    _LIST_OFFSET += n
    return (f'<ol class="findings" style="counter-reset:f {start}">' if start
            else '<ol class="findings">')


#: Where a reader can go to find out what a pass actually does. Only the two commands this
#: skill runs are in it, because those are the two it stamps — a source it does not
#: recognise (`assumption`, a human name, a linter) renders as the plain stamp it always
#: was rather than being sent somewhere that does not describe it.
PASS_DOCS = {
    "/code-review": "https://code.claude.com/docs/en/code-review#review-a-diff-locally",
    "/simplify": "https://code.claude.com/docs/en/commands#all-commands",
}


def _finding_source(f) -> str:
    """Which pass raised it, when the content file says so.

    Optional by design: an item that does not claim a source renders without one rather
    than being attributed to a guess. Provenance exists only where the decision was made,
    which is why it now arrives from the branch: `review-points.md`'s `source:` field is
    written by the agent that read the finding and accepted or declined it, in the session
    where the pass that raised it was the one running.

    The assumptions pile does not come through here at all: its provenance never varies,
    so it is the card's one purple chip rather than a grey stamp behind a second badge.

    A stamp naming a documented pass is the link to that documentation. The stamp already
    is the question — *what is `/code-review`?* — and answering it in place costs the page
    nothing, where answering it in prose costs a line under the verdict that every reader
    who already knows has to read past."""
    src = (f.get("source") or "").strip()
    if not src:
        return ""
    href = PASS_DOCS.get(src)
    if not href:
        return f'<span class="f-src">{html.escape(src)}</span>'
    # The face is the command, which says nothing about where the link goes; the tooltip
    # spends itself on that half, as every other tooltip on this page does.
    return (f'<a class="f-src" href="{html.escape(href)}" target="_blank" rel="noopener"'
            f' data-tip="What {html.escape(src)} does, in the Claude Code docs">'
            f'{html.escape(src)}</a>')


def _raised_by(items, total: int) -> str:
    """`12 raised — 9 by /code-review, 3 by /simplify`: the review chip's hover.

    Counted off each item's own `source`, the same string the stamp beside it renders, so
    a reader who hovers the chip and then counts the stamps gets the same answer twice.
    Passes appear in the order the content file first mentions them.

    `source` is optional (see `_finding_source`), and an item without one is counted as
    itself rather than folded into whichever pass happens to be first — an unattributed
    finding is a real state, and a hover that hides it is a hover that lies by rounding.
    With nothing attributed at all the breakdown is dropped entirely: `12 raised, 12 of
    them unattributed` is the total said twice."""
    counts: dict[str, int] = {}
    for it in items:
        src = (it.get("source") or "").strip()
        counts[src] = counts.get(src, 0) + 1
    named = [f"{n} by {src}" for src, n in counts.items() if src]
    if not named:
        return f"{total} raised"
    if counts.get(""):
        named.append(f'{counts[""]} with no pass named')
    return f"{total} raised — " + ", ".join(named)


def _finding_refs(f) -> str:
    """The bare `file:line` links — only when nothing else already carries them.

    An item that shows a snippet or a diff already links the file, with a line RANGE, from
    that block's own header. Repeating a bare `file:line` link above it says the same thing
    twice and worse."""
    if f.get("_snippets") or f.get("_diffs"):
        return ""
    return "".join(_ref_link(r) for r in f.get("_refs", []))


def _ref_link(r) -> str:
    """`VetRestController.java:96-100`, with the path it came from on hover.

    The same trade a diff header makes: a repo-relative Java path spends five segments on
    module, `src/main/java` and the org package before it reaches the one word that says
    which file this is, and a line of three such references is a wall no reader parses.
    The path is not dropped, it is moved to the tooltip — and a file at the repo root has
    no path to move, so it gets no tooltip repeating its own name."""
    label = r["label"]
    rel, _, lines = label.rpartition(":")
    name = f"{Path(rel).name}:{lines}" if rel else label
    tip = f' data-tip="{html.escape(rel)}"' if "/" in rel else ""
    return (f'<a class="srcref" href="vscode://file/{r["abs"]}"{tip}>'
            f'{html.escape(name)}</a> ')


def _assumptions_block(spec):
    """The `assumptions` block as the layout declared it, or None if the page declares none.

    The lede counts the coder's pile even when it is empty, and the only sentence it can
    honestly print about an empty one depends on the mode — so it has to find the block
    itself, not infer the pile from the items that happen to be in it."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == "assumptions":
                return b
    return None


def _pile_anchor(spec, kind, fallback):
    """The id the pile's own heading will carry, or None when no such block is laid out.

    Read from the layout rather than assumed: a block may name itself, and a lede whose
    links point at ids no heading has is worse than a lede with no links at all."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == kind:
                return b.get("id", fallback)
    return None


def opening_lede(spec) -> str:
    """The shape of the whole list, for whichever pile opens it — and only for that one.

    Computed, and deliberately a line. What stood here was three sentences of prose
    restating the shape of the list directly beneath it ("They are one list: the nine that
    need your judgement first, then the three I applied, greyed out and numbered straight
    on"), which a reader can see. The reader is a developer who came for the findings; the
    counts are the only part of that paragraph they could not have got by looking.

    It is asked for by all three piles and answers only the first, because the piles are
    one list and their order is the content file's to choose. Pinned to `findings`, a lede
    describing three piles renders underneath one the reader has already walked past.
    `_LIST_OFFSET` is still zero exactly until the first pile renders, so the question
    "am I the top of the list?" is already answered and does not need a second flag.
    """
    global _LEDE_SHOWN
    if _LIST_OFFSET or _LEDE_SHOWN:
        return ""
    # Counts, and nothing else. Every clause that described how the list *looks* has been
    # cut — "greyed out", "yours to confirm", and finally "worst first" itself: the
    # applied fixes are visibly grey, an assumption visibly wears its purple chip, and an
    # ordering is the one thing a reader can see without being told. Each was the
    # paragraph-the-reader-can-see rule reappearing one clause at a time, inside the line
    # that replaced the paragraph.
    block = _assumptions_block(spec)
    parts = []

    def clause(text, kind, fallback):
        """A count, and the way to the pile it counts.

        The line is the first thing read in the tab and names three chapters further down
        it, so every clause is the jump to its own — the reader was going to scroll looking
        for them anyway. A pile with no block laid out keeps its count as plain text: a
        dead anchor that silently does nothing is worse than a number that never claimed
        to be clickable."""
        at = _pile_anchor(spec, kind, fallback)
        return f'<a href="#{html.escape(at)}">{text}</a>' if at else text

    # Two vocabularies, because the two sources mean different things by the same pile.
    # With the piles written into the content file, `findings` is what a review pass raised
    # and nobody has answered yet — *open*. Read out of `review-points.md`, the same array
    # is what the agent read and said no to, which is not open at all: it is closed, by the
    # agent, and the reader's job is to disagree or agree. Calling that "open" would ask
    # the reviewer to triage a decision that has already been made, and would hide the one
    # fact the file exists to carry.
    points = spec.get("_reviewPoints")
    if points:
        # Fixed first, then declined. The old order led with what is still open because
        # that was the pile with work in it; here the reader is being shown a review that
        # is already finished, and it reads in the order it happened — accepted, declined,
        # and then the decisions nobody was asked about.
        if spec.get("autofixes"):
            parts.append(clause(f"{len(spec['autofixes'])} fixed", "autofixes", "fixed"))
        if spec.get("findings"):
            parts.append(clause(f"{len(spec['findings'])} declined", "findings", "first"))
    else:
        if spec.get("findings"):
            # "open LLM review issues", not "open, worst first": the ordering fact was the
            # one clause here that a reader could not have counted themselves, and it was
            # also the one nobody acts on — the list is in front of them, worst first or
            # not. What they do act on is *who raised these*, because the page carries two
            # piles a machine produced and one a human owns, and the clause that opens the
            # line is the one that has to say which of them it is counting.
            n_open = len(spec["findings"])
            parts.append(clause(
                f"{n_open} open LLM review issue{'' if n_open == 1 else 's'}",
                "findings", "first"))
        if spec.get("autofixes"):
            # `auto-fixed`, the same word the badge on every one of those items already
            # wears. "auto-applied" was a second name for one thing, and a reader who
            # scrolls to the pile has to satisfy themselves the two words mean the same
            # before they can trust the count.
            parts.append(clause(f"{len(spec['autofixes'])} auto-fixed",
                                "autofixes", "fixed"))
    # Last, because the first two clauses count what a review pass produced and this one
    # counts what it could not: a reader who has just been told how many items are open
    # and how many were applied is at exactly the point where "and here is what nobody
    # checked" lands. Leading with it puts the softest pile in front of the defects.
    if block is not None:
        # Zero is a number the reader came for, so this clause renders at zero too. A pile
        # that appears only when it is non-empty disappears exactly where it matters most:
        # "the page says nothing about what the coder guessed at" and "the coder was asked
        # and guessed at nothing" are the same blank line, and only one of them is good
        # news. Mode C is the case where a zero would be the lie instead — nobody was in a
        # position to be asked — so it says that rather than counting an empty pile.
        assumed = len(spec.get("assumptions", []))
        # `6 assumptions`, flat. `coder` named who produced them, which the card's own
        # purple `assumption` chip says where the reader is standing, and `to check` named
        # the work — in a line whose other two clauses are bare counts, so the asymmetry
        # read as a fourth fact rather than as the same shape said three times. Mode C is
        # still the exception: there is no count to give, only the reason there is none.
        parts.append(clause(
            "coder could not be asked"
            if block.get("mode") == "C" and not assumed
            else f"{assumed} assumption{'' if assumed == 1 else 's'}",
            "assumptions", "assumed"))
    if not parts:
        return ""
    _LEDE_SHOWN = True
    # The stamp clause went the same way as "greyed out" and "yours to confirm": every
    # item carries its source beside its own title, so a line announcing that they do
    # describes the thing directly under it. What is left is three counts and the jump to
    # each — the only part of the list that counting it yourself would not have told you.
    # `pilelede` is what the stylesheet pins: the line names three chapters that are
    # thousands of pixels apart, so it has to still be on screen when the reader is inside
    # one of them and wants the next. Sticky under the masthead, never over it.
    return ('<p class="sub counts pilelede">' + " &middot; ".join(parts)
            + "</p>")


#: Bands the Review tab owes the reader before its first heading: what is not recorded,
#: and what changed after the agent stopped. Module state for the same reason
#: `_LIST_OFFSET` is — they are emitted by a leaf of a render tree eight calls deep, and
#: they belong to the *tab*, not to whichever pile happens to open it. `main` fills the
#: list before rendering; the first pile drains it.
_BANDS: list[str] = []


def set_bands(bands) -> None:
    """Replace the pending bands. Called once per page, beside `reset_list`."""
    _BANDS[:] = [b for b in bands if b]


def _flush_bands() -> str:
    """Every pending band, once. Drained rather than read, so a tab with three piles in it
    does not print the same red band three times."""
    out = "".join(_BANDS)
    _BANDS.clear()
    return out


def _lede_above(head: str, lede: str) -> str:
    """Above the first pile's heading, not tucked under it.

    The line counts all three piles, so under `Requires human review` it reads as a
    description of the findings and the reader meets `4 coder assumptions to check` as a
    footnote to a heading that has nothing to do with them. Hoisted above, it is what it
    is: the shape of the whole list, before the list starts. Which pile happens to open
    the list is then an editorial choice that cannot move the line.

    The tab's bands land between the two: under the counts line, which is sticky and has
    to stay the topmost thing in the tab, and above the first heading, because what a band
    says ("nothing records this review", "someone changed the code after the agent
    finished") governs how every item under it should be read.
    """
    return (lede + _flush_bands() + head) if lede else (_flush_bands() + head)


def render_findings(findings) -> str:
    if not findings:
        return '<p class="sub">Nothing outstanding \u2014 the automated passes came back clean.</p>'
    items = []
    for f in findings:
        cls, label = SEVERITIES.get(f.get("severity", "info"), SEVERITIES["info"])
        refs = _finding_refs(f)
        items.append(
            f'<li class="{cls.replace("sev-", "n-")}">'
            f'<span class="badge {cls}">{html.escape(label)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(findings)) + "\n".join(items) + "</ol>"


def render_assumptions(items, mode: str = "") -> str:
    """What the agent that wrote the code decided without being told — and its alternative.

    This is the one pile on the page no pass can produce. A finding is found by reading the
    diff; an assumption is knowable only from the side that made it, and it lives in exactly
    one place: the transcript of the conversation that did the work. `authoring-sessions.py`
    says whether that conversation is the one running (mode A), an older one on disk whose
    transcript a subagent reads verbatim (mode B), or gone (mode C).

    An empty pile still renders, because the three ways of being empty are not the same
    fact and a blank space would read as the friendliest of them. "Nothing was assumed" is
    a claim; "nobody could be asked" is an admission; and the reviewer has to be able to
    tell which one they are looking at."""
    if not items:
        return {
            "A": '<p class="sub">The conversation that wrote this code was asked what it '
                 'had to guess at, and named nothing.</p>',
            "B": '<p class="sub">The transcript of the conversation that wrote this code '
                 'was read back in full, and it recorded no open question.</p>',
            "C": '<p class="sub">No transcript of the conversation that wrote this code '
                 'survives, so it could not be asked. This is not the agent saying it was '
                 'sure \u2014 it is nobody having been in a position to ask.</p>',
        }.get(mode, '<p class="sub">Nothing was assumed.</p>')
    out = []
    for f in items:
        refs = _finding_refs(f)
        # One chip, not two. Every card here used to open with a purple `your call` badge
        # and then a grey monospaced `assumption` stamp — the badge naming what the reader
        # owes, the stamp naming where the item came from, and the two of them together
        # spending the whole first line of every card on the one thing all of them have in
        # common. The provenance is the word worth keeping (`assumption` is what
        # distinguishes this pile from `/code-review` and `/simplify`, which is exactly the
        # distinction the counts line above now draws), and it wears the badge's purple so
        # the pile still reads as the one the human owns.
        out.append(
            '<li class="n-assumed">'
            f'<span class="badge sev-assumed">'
            f'{html.escape((f.get("source") or "assumption").strip())}</span>'
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-alt"><b>Read the other way:</b> {f["alternative"]}</p>'
               if f.get("alternative") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(items)) + "\n".join(out) + "</ol>"


def render_autofixes(fixes, badge: str = "auto-fixed") -> str:
    """What the agent already fixed \u2014 the tail of the same list.

    It continues the open findings' numbering on purpose. The two piles are one decision
    split in two: everything with a single obvious right answer was applied, everything a
    second engineer could reasonably disagree about was left. A reviewer who cannot see the
    first pile has to take the size of the second on trust \u2014 and a reviewer shown two lists
    that both start at 1 has to add them up by hand.

    Each item shows its diff rather than describing it. That is the whole difference between
    this and a changelog: the reader sees what was done to their code without leaving the
    page or trusting a sentence about it.

    `badge` is the word on each card, and it is a parameter because the same pile now
    arrives two ways. `auto-fixed` is right for a pass that applied its own findings with
    nobody in between. Read off `review-points.md` it would be a small lie in the one place
    a reader looks first: the agent read each finding and *chose* to accept it, which is the
    fact the pile exists to record, so there the word is `fixed`."""
    if not fixes:
        return '<p class="sub">Nothing was applied automatically \u2014 every finding needed a human.</p>'
    items = []
    for f in fixes:
        refs = _finding_refs(f)
        items.append(
            '<li class="fixed">'
            f'<span class="badge sev-fixed">{html.escape(badge)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_diffs", "") or "")
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return _open_list(len(fixes)) + "\n".join(items) + "</ol>"


#: What the Code City shot is above, said as a heading rather than as a caption. Fixed in
#: the build and not asked of every content file, for the same reason the zip offer in the
#: footer is: it is a fact about what this picture always shows, not about this branch. A
#: `title` on the block overrides it for a page that means something else by the picture.
CITY_HEADING = "Code impact of this PR: size, complexity, coupling, \u2026"

#: Where `run-steps.py`'s `aftermath` step leaves what it measured.
AFTERMATH_JSON = "aftermath.json"

#: How many files one commit's row names before it stops naming them. A commit that
#: touched forty files is a commit whose *subject* is the answer; the list is there so a
#: reader recognises a one-file config tweak without opening anything.
AFTERMATH_FILES = 6


def _aftermath_files_tip(c: dict) -> str:
    """What this commit touched, as one hover on its sha.

    It used to be a line of its own under every commit — `human-review.json +18 −1` — and
    on a branch with six commits that was six lines of filenames and arithmetic between the
    reader and the two things they can do about any of it. The band's job is to say *that
    the page is describing an older branch*; which file moved is the follow-up question, and
    a follow-up question belongs on the thing it is about, which is the sha.

    Plain text, not markup: a tooltip is read in one glance with a hand on the mouse."""
    files = c.get("files") or []
    if not files:
        # A merge commit prints no numstat. "Nothing changed" is the wrong reading of it.
        return "No file list — a merge, or nothing git could count."
    parts = []
    for f in files[:AFTERMATH_FILES]:
        counts = " ".join(x for x in (
            f"+{f['added']}" if f.get("added") else "",
            f"\u2212{f['deleted']}" if f.get("deleted") else "") if x) or "no lines"
        parts.append(f"{f['path']} {counts}"
                     + (" (generated)" if f.get("generated") else ""))
    if len(files) > AFTERMATH_FILES:
        parts.append(f"and {len(files) - AFTERMATH_FILES} more")
    return "\n".join(parts)


def _aftermath_commit(c: dict) -> str:
    """One commit's row: what it is, and nothing to press.

    It used to carry a *Revert it* button, per commit, running `git revert --no-commit`.
    That is gone, and not because it did not work. It answered the wrong question: a
    commit in this band is not a mistake to be undone, it is a commit the page has not
    caught up with, and the overwhelmingly common case on a branch like this one is the
    infrastructure cherry-pick that *had* to land here. Offering to reverse it first, in
    red, made the band read as an accusation — and put a button that rewrites the working
    tree at the top of a page whose whole contract is that it only ever reads the
    repository. A reader who really does want a commit back has `git revert` and does not
    need a review page to type it.

    So the row is what it always was underneath: the sha, what it did, when. The one
    action the band offers is the band's, rendered once beside the list rather than once
    per commit — the answer is the same command however many commits landed, and three
    copies of it down a list is three chances to wonder whether they differ.
    """
    when = (c.get("when") or "")[:10]
    return ('<li>'
            f'<code data-tip="{html.escape(_aftermath_files_tip(c), quote=True)}">'
            f'{html.escape(c["short"])}</code> '
            f'{html.escape(c.get("subject", ""))}'
            + (f' <span class="rb-gen">{html.escape(when)}</span>' if when else "")
            + '</li>')


def _regenerate_offer(out_dir: Path, root: Path) -> str:
    """The band's one answer, once, under the list of commits.

    The band says a human moved the code after the review was written, and everything else
    on the page describes the branch as the agent left it. The thing a reader wants at that
    point is not to undo the commit — it is legitimate more often than not — but to make
    the rest of the page catch up with it, which is the masthead's rerun, offered here
    because here is where the reader is actually looking at the problem.

    Once per band and not once per commit: the command does not name a commit, so three
    copies of it under three shas would be three identical buttons inviting the reader to
    work out which one applies to which row. The answer is the band's, so it sits with the
    band.

    `__rerun__` is not a manifest id — it is the server's own verb, and `window.HR.can`
    answers for it off the probe. Which means the run glyph here appears under exactly the
    same condition as the rerun chip in the masthead, and where nothing can run, the
    clipboard in its place carries the same command a reader would type. One offer, two
    places, one command.
    """
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(out_dir.resolve())
    line = (f'cd {shlex.quote(str(root.resolve()))}'
            f' && {shlex.quote(str(HERE / "refresh-report.py"))}'
            f' --dir {shlex.quote(rel)} --steps static')
    return ('<p class="rb-actions"><span class="rb-act">'
            + offer_words_html(
                "Regenerate the report", "__rerun__",
                "Re-derives the evidence a program can re-derive — diagrams, complexity, "
                "the REST contract, the logging scan, the test manifest — and rebuilds "
                "this page around the branch as it is now. Free, and not the findings.",
                "Rebuilds this page against the branch as it is now and reloads. The "
                "same thing the Rerun in the header does. Free.",
                "Rebuilding this page…", cmd=line, pill=True)
            + command_html(line, "__rerun__",
                           tip="Rebuilds this page against the branch as it is now and "
                               "reloads",
                           running="Rebuilding this page…")
            + '</span></p>')


def aftermath_html(out_dir: Path, root: Path) -> str:
    """What landed on the branch after the agent stopped, at the top of the Review tab.

    This is the one band on the page that is about the page rather than about the code.
    Every number here was measured from a diff, and a diff cannot say when it was written:
    the film, the findings, the declined items, the assumptions and the costs all describe
    the branch as the agent left it, and three commits later they describe something
    nobody reviewed. The reader cannot see that, because the page is the only thing in a
    position to say it.

    Red when a file no generator owns has moved; grey when every one of them is generated,
    which on this project's own demo branch is the normal case — the guardrails regenerate
    diagrams, a spec and a `.drawio` on every commit, and a band that is red for that is a
    band nobody reads by the third branch. Nothing at all when the agent's commit is the
    tip, which is the state this whole flow is trying to produce.
    """
    try:
        doc = json.loads((out_dir / AFTERMATH_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No measurement is not "nothing happened". The step says why on its own row of
        # the status table (no review commit, most often), and inventing a reassuring
        # band here would be the page asserting the one thing it does not know.
        return ""
    commits = doc.get("commits") or []
    if not commits:
        return ""
    totals = doc.get("totals") or {}
    code = totals.get("code") or {}
    gen = totals.get("generated") or {}
    n = len(commits)
    plural = "" if n == 1 else "s"
    if code.get("files"):
        lines = (code.get("added", 0) or 0) + (code.get("deleted", 0) or 0)
        head = (f'<p><b>{n} commit{plural}, {lines} line'
                f'{"" if lines == 1 else "s"} changed since the agent finished.</b> '
                'Everything else on this tab — and on every other tab — describes the '
                'branch as it was when the review was written.</p>')
        sub = ('Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>. '
               + (f'{gen["files"]} generated file' + ("" if gen["files"] == 1 else "s")
                  + ' moved as well and are not counted here.'
                  if gen.get("files") else
                  'None of it is a generated file.'))
        cls = "rband-alert"
        role = "alert"
    else:
        head = (f'<p>{n} commit{plural} since the agent finished, and every file '
                'in them is generated.</p>')
        sub = ('Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>. '
               'Regenerated output, not somebody editing the change under review — which '
               'is why this band is grey.')
        cls = "rband-warn"
        role = "status"
    return (f'<div class="rband {cls}" role="{role}">' + head
            + f'<p class="rb-sub">{sub}</p><ul>'
            + "".join(_aftermath_commit(c) for c in commits)
            # After the list, not inside it: the commits are what happened, and this is the
            # one thing to do about all of them.
            + '</ul>' + _regenerate_offer(out_dir, root) + '</div>')


#: The three block types that render the one list. Named so `render_block` can hand all
#: three to one function: they share the lede, the numbering, the band and — since
#: `{"auto": "review-points"}` — the question of what an empty one is allowed to say.
PILE_BLOCKS = ("findings", "assumptions", "autofixes")


def render_pile_block(spec, block, heading=None):
    """One of the three piles, as `(html, weight, changes)`.

    Lifted out of `render_block` when the piles stopped being the content file's own list.
    The decision it now makes is not about layout at all — it is *whose* silence an empty
    pile is, the author's or the branch's — and that is worth testing directly rather than
    through a page build with a repository, a manifest and PlantUML behind it.

    `heading` is `render_block`'s local heading emitter; without one (a test, a caller
    rendering a pile on its own) the piles render bare.
    """
    def head_of(fallback_id, fallback_title):
        if heading is None:
            return ""
        return heading(block, fallback_id, fallback_title)

    kind = block.get("type", "section")
    # Whether these three piles are the branch's record or the content file's own list. It
    # changes what an empty one is allowed to say, and what the two defect piles are
    # *called* — a content file's `findings` are untriaged and a branch's are declined —
    # and nothing else: the item shapes are identical, which is the whole reason
    # `review-points.md` could be bolted on without touching a renderer.
    points = spec.get("_reviewPoints")
    if kind == "findings":
        items = spec.get("findings", [])
        head = _lede_above(
            head_of("first", "Read and declined" if points else "Requires human review"),
            opening_lede(spec))
        if points and not items:
            # Weight 1: the sentence saying which kind of empty this is has to keep the
            # tab alive, exactly as the assumptions pile's always has.
            return (head + points_empty_html("findings", points), 1, 0)
        return (head + render_findings(items), len(items), len(items))
    if kind == "assumptions":
        items = spec.get("assumptions", [])
        # `resolve_review_points` has already forced this block to mode C when the branch
        # carries no record, so the mode read here is the one the counts line read too.
        mode = block.get("mode", "")
        head = _lede_above(head_of("assumed", "Decided without asking you"),
                           opening_lede(spec))
        if points and not items and not points.get("missing"):
            return (head + points_empty_html("assumptions", points), 1, 0)
        # Weight 1 even with nothing in it: an empty pile still carries the sentence
        # saying *which* kind of empty it is, and that sentence is the point.
        return (head + render_assumptions(items, mode),
                1 if (items or mode) else 0, len(items))
    items = spec.get("autofixes", [])
    head = _lede_above(head_of("fixed", "Fixed" if points else "Auto-fixed"),
                       opening_lede(spec))
    if points and not items:
        return (head + points_empty_html("autofixes", points), 1, 0)
    return (head + render_autofixes(items, badge="fixed" if points else "auto-fixed"),
            len(items), len(items))


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


def render_puml(block, root: Path, out_dir: Path) -> str:
    """A diagram this branch did *not* change, drawn as context rather than as a delta.

    Package structure is the case that asks for it: a reviewer wants to see the shape
    the change landed in even on the \u2014 common, and good \u2014 branches that left it alone.
    Rendered here from the committed source, so the page carries no stale SVG."""
    src = root / block["src"]
    cache, why_not = _context_svg(block["src"], root, out_dir)
    if not cache:
        return why_not
    return (
        '<div class="diagram">'
        f'<div class="head"><b>{html.escape(block.get("name", src.stem))}</b>'
        f'<span class="badge sev-info">{html.escape(block.get("status", "unchanged"))}</span>'
        + _source_link(block["src"], root) + '</div>'
        + (f'<p>{block["note"]}</p>' if block.get("note") else "")
        + _provenance(block["src"], root)
        + f'<div class="svgbox">{inline_svg(cache, root)}</div></div>'
    )


def _context_svg(rel: str, root: Path, out_dir: Path):
    """The committed `.puml` drawn as it stands, cached under assets/ by mtime.

    Returns `(svg_path, "")`, or `(None, <the paragraph to print instead>)`: a missing
    file and a missing PlantUML are the two ways there is no picture, and each is named
    rather than swallowed. Shared by the `puml` block and by a test pair whose sequence
    exists but carried no delta \u2014 the same picture, drawn the same way."""
    src = root / rel
    if not src.is_file():
        return None, f'<p class="sub">no diagram at <code>{html.escape(rel)}</code></p>'
    cache = out_dir / "assets" / (Path(rel).stem + ".context.svg")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        out = subprocess.run(["plantuml", "-tsvg", "-pipe"],
                             input=src.read_bytes(), capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return None, (f'<p class="sub">plantuml could not render '
                          f'<code>{html.escape(rel)}</code> \u2014 is it installed?</p>')
        cache.write_bytes(out.stdout)
    return cache, ""


# The guide is one of forty tabs the reviewer has open, all of them named after the
# branch. At that width the strip has room for the favicon and nothing else — so the
# mark that makes the guide findable belongs on the icon, not in front of the title,
# where it was only legible in the hover card you reach after already finding the tab.
#
# Base64 rather than percent-encoding the SVG: the payload is full of quotes and angle
# brackets, and one missed escape is a silently blank icon rather than an error.
FAVICON_EMOJI = "\U0001F471\U0001F3FB\u200D\u2642\uFE0F"
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<text y=".9em" font-size="90" font-family="Apple Color Emoji,Segoe UI Emoji,'
    'Noto Color Emoji,sans-serif">' + FAVICON_EMOJI + "</text></svg>"
)
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(FAVICON_SVG.encode()).decode()


def _app_anchor(href: str) -> str:
    """The opening tag for a link into the running app.

    A root-relative href is a *path into whatever instance is up right now*, and the port
    that instance got is not knowable when this page is built — the host picks it, so that
    several branches can be running at once. So the path is kept verbatim in `data-app` and
    the `href` is only ever a best guess, rewritten by the script once a base URL is known.
    An absolute href is left exactly as written: it names a specific server on purpose."""
    if not href.startswith("/"):
        return f'<a href="{html.escape(href)}">'
    return f'<a data-app="{html.escape(href)}" href="{html.escape(href)}">'


def runtime_html(rt) -> str:
    """The app the walkthrough was filmed against: start it, open it, stop it, reset it.

    This page is a file on disk that outlives the branch it describes, so it cannot hold a
    live URL: by the time anyone opens it the environment is long gone, and the next one
    will come up on a different port. What it *can* hold is a way to bring the environment
    back \u2014 and there are two of those, which is the whole shape of this bar.

    Served, the row is verbs, and it keeps its own state: nothing answering, so `Start`;
    something answering, so the address it answers at \u2014 a link, into a new tab, port and
    all \u2014 then `Stop` and `Reset DB`. The page asks its own server to run the commands
    the build declared and scrapes the URL out of what `Start` printed, so there is nothing
    for the reader to paste anywhere.

    Off disk none of that can happen: no process here runs a command, and Reset has
    nothing to reset until one does. So the row carries no verbs at all and the terminal
    command takes their place. Both are in the markup either way \u2014 the same file is
    opened both ways and only the script knows which \u2014 and CSS hides the half that is
    lying. One offer, in the register that copy of the report can actually honour, rather
    than the same offer twice: greyed buttons above the command that replaces them.

    Every control except the command is opt-in on something the environment actually
    provides \u2014 `stop`, `reset` \u2014 because a button that always fails is worse than no
    button, and none of these can be derived from `command` by string surgery without
    working for the one host this was written against and failing silently on the next.
    """
    if not rt:
        return ""
    cmd = rt.get("command", "")
    fallback = rt.get("base", "")

    def btn(cls: str, face: str, tip: str) -> str:
        # `hidden` from the start, and raised by the script once the probe has answered:
        # a verb drawn live that turns out not to apply has already been clicked by then.
        # aria-disabled rides along so the guard in the script survives a stylesheet that
        # never loaded, where `hidden` alone would have left a live button behind.
        return (f'<button type="button" class="{cls}" hidden aria-disabled="true"'
                f' data-tip="{html.escape(tip, quote=True)}">{face}</button>')

    # The state first, because it is the subject of everything after it: "Offline", and
    # then the one verb that changes that — or the address, and then the two verbs that
    # act on what is answering there. Exactly one of the pill and the link is ever shown.
    at = ('<span class="appenv-at">'
          '<span class="appenv-state" data-state="unknown">checking\u2026</span>'
          '<a class="appenv-url" target="_blank" rel="noopener" hidden></a></span>')

    controls = btn("appenv-start", "Start",
                   "Start the app and fill the address in from what it prints") if cmd else ""
    if rt.get("stop"):
        controls += btn("appenv-stop", "Stop", "Stop the app and free its port")
    if rt.get("reset"):
        controls += btn("appenv-reset", "Reset DB", "Put the demo data back to its seed")

    if cmd:
        declare_action("demo-env", cmd, scrape="url",
                       label="Start the environment the walkthrough was filmed against")
    if rt.get("stop"):
        declare_action("demo-env-stop", rt["stop"],
                       label="Stop the environment the walkthrough was filmed against")
    # Optional and never guessed. Turning `… up --ref abc` into `… url --ref abc` by
    # string surgery would work for the one host this was written against and fail
    # silently on the next, at probe time, where nobody would see it fail. Declared or
    # absent — and absent costs only the re-discovery of a base the browser had already
    # remembered in localStorage.
    if rt.get("urlCommand"):
        declare_action("demo-env-url", rt["urlCommand"], scrape="url",
                       label="Ask the host where the environment is already answering")
    if rt.get("drive"):
        declare_action("cue-drive", rt["drive"], params={"n": "int", "base": "url"},
                       label="Drive the app to one caption of the walkthrough")

    # The commands this row is made of, as a clipboard each — and, served, a play each.
    #
    # In *both* copies of the report, which is the change. The stylesheet used to print the
    # `up` line off disk and hide it the moment the probe answered, so "what does this
    # button actually run" had no answer in the copy where the button worked. Now the
    # affordance is there either way, and the line itself is in the glyph's hover rather
    # than in the row: a `cd … && ./start-docker.sh up --ref abc123` printed in a bar of
    # four controls was the widest thing in the Demo tab and was read once.
    #
    # All three, not just `up`. `stop` and `url` were declared for the buttons and never
    # offered to anybody, which meant the one reader who needed to know how the host is
    # asked where the stack is answering had to go and read the manifest.
    rows = []
    if cmd:
        rows.append(("start", command_html(
            cmd, "demo-env", tip="Starts the app and fills the address in from what it "
            "prints", running="Starting the app…")))
    if rt.get("stop"):
        rows.append(("stop", command_html(rt["stop"], "demo-env-stop",
                                          tip="Stops the app and frees its port",
                                          running="Stopping…")))
    if rt.get("urlCommand"):
        rows.append(("where", command_html(
            rt["urlCommand"], "demo-env-url",
            tip="Asks the host where the app is already answering",
            running="Asking the host…")))
    manual = ('<p class="appenv-manual">'
              + "".join(f'<span class="appenv-cmd">'
                        f'<span class="appenv-verb">{face}</span>{box}</span>'
                        for face, box in rows)
              + '</p>') if rows else ""

    return (f'<div class="appenv" data-fallback="{html.escape(fallback)}"'
            f'{f' data-reset="{html.escape(rt["reset"])}"' if rt.get("reset") else ""}'
            f'{f' data-drive="{html.escape(rt["drive"])}"' if rt.get("drive") else ""}>'
            '<div class="appenv-run"><span class="appenv-title">Deployed app</span>'
            + at + controls + '</div>' + manual + '</div>')


def _link_captions(cues, links, drive=False):
    """Put the app links *inside* the narration, on the words that already name the page.

    They used to sit in a paragraph of their own — "Pages this change touches: owner detail
    · all visits · vets" — a second list of the same screens the captions were already
    walking through, in a different order and different words. A caption that says "back on
    the owner" is the natural handle for the owner page; the separate list was a handle
    nobody needed and a thing to keep in sync.

    Returns (rendered <li> items, links that found no caption). A link is *never* dropped:
    one whose phrase is not in the narration is reported back to be printed after the
    transcript, because a page this change touches and the film did not show is a fact
    about the coverage of the film."""
    texts = [c["text"] for c in cues]
    # Each caption is escaped once, then the anchors are spliced into the escaped text —
    # so the phrase has to be escaped the same way to be found in it.
    cells = [html.escape(t) for t in texts]
    unplaced = []
    for link in links:
        phrase = html.escape(link.get("anchor") or "")
        href = link["href"]
        for i, cell in enumerate(cells):
            at = cell.find(phrase) if phrase else -1
            # Never inside an anchor already spliced in: nested <a> is invalid, and the
            # second link would be unclickable. An unbalanced count of open tags before the
            # match is exactly "we are inside one".
            if at < 0 or cell[:at].count("<a ") != cell[:at].count("</a>"):
                continue
            cells[i] = (cell[:at] + _app_anchor(href) + phrase + "</a>"
                        + cell[at + len(phrase):])
            break
        else:
            unplaced.append(link)
    # 1-based, and the same numbering the reader is looking at: the driver replays the
    # walkthrough and stops after the nth caption, so "cue 3" has to mean the third row.
    drive_btn = (lambda n: f'<button type="button" class="cue-drive" data-n="{n}" aria-disabled="true" '
                           f'data-tip="Start the app first">&#9656;</button>') \
        if drive else (lambda n: "")
    items = "".join(
        f'<li data-t="{c["t"]:.2f}"><span class="ts">{int(c["t"]) // 60}:'
        f'{int(c["t"]) % 60:02d}</span><span>{cell}{drive_btn(i)}</span></li>'
        for i, (c, cell) in enumerate(zip(cues, cells), 1)
    )
    return items, unplaced


#: What `_video` writes beside the film when the recorder exited non-zero, named after the
#: film so two sections cannot read each other's verdict.
VIDEO_VERDICT = ".verdict.json"

#: What the recorder's exit codes mean, in the words the banner uses. Exit 3 is the one
#: worth the machinery: the film was made, it is on the page, and it shows the feature not
#: working. Nothing about the footage says so — it is a normal-looking demo of a screen
#: that did not do what the caption claims — which is why the page has to.
VERDICT_FACE = {
    3: ("The feature did not hold on film.",
        "The recorder drove this branch's own walkthrough and it did not complete. "
        "Everything below is the film of that: watch it before reading anything else "
        "on this page."),
    2: ("Nothing was filmed.",
        "The recorder refused: no feature script, or the application answering was not "
        "the commit under review. A film of another branch's screens under this branch's "
        "narration is worse than no film."),
}


def video_verdict_html(rel: str, out_dir: Path) -> str:
    """The recorder's non-zero exit, said over the player — or nothing at all.

    This exists because the loudest thing this pipeline can produce was also the easiest
    to lose. `record-feature-video.sh` exits 3 when the walkthrough did not complete, and
    the film is kept *on purpose*: it is the most review-worthy artifact the whole page can
    carry. But the footage of a feature failing looks like footage of a feature working —
    a browser, a form, a list — and the exit code used to live only in a status table that
    a human read once and then had to remember to write about. On 17 Sep 2026 that is
    exactly what went wrong: exit 3 with three screens missed, and the page showed the
    player with no mark on it at all.

    So the step writes the verdict next to the film and this draws it. Missing file means
    the recorder exited 0, which is the common case and renders nothing.
    """
    path = out_dir / (rel.replace(".webm", "") + VIDEO_VERDICT)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    code = doc.get("exit")
    title, why = VERDICT_FACE.get(code, (
        f"The recorder failed (exit {code}).",
        "There is no complete film of this change. Treat whatever plays below as partial."))
    missed = [m for m in (doc.get("missed") or []) if isinstance(m, str)]
    parts = [f'<p class="vv-head"><b>{html.escape(title)}</b> {html.escape(why)}</p>']
    if missed:
        # Named, not counted: "three screens missed" sends the reader back to the log,
        # and the names are what tells them whether the gap is the one that matters.
        parts.append('<p class="vv-missed"><b>Never reached:</b> '
                     + " · ".join(f'<code>{html.escape(m)}</code>' for m in missed)
                     + "</p>")
    if doc.get("note"):
        parts.append(f'<p class="vv-note">{html.escape(str(doc["note"]))}</p>')
    log = [str(l) for l in (doc.get("log") or [])]
    if log:
        # Folded: the lines are the answer for whoever is fixing it and furniture for
        # everybody else, and the summary above already says what happened.
        parts.append('<details class="vv-log"><summary>the recorder’s last words'
                     "</summary><pre>"
                     + html.escape("\n".join(log[-14:])) + "</pre></details>")
    return '<div class="vidverdict" role="alert">' + "".join(parts) + "</div>"


def video_html(s, out_dir: Path) -> str:
    """The player and its transcript — or, when the recording failed, the transcript alone.

    The first thing this page ever got wrong was a `<video src="assets/….webm">` whose
    asset no step had written: a black rectangle stuck at 0:00 under a confident heading,
    with nothing on the page to say the film was missing rather than broken. So the player
    is only ever emitted for a file that is on disk. The narration is *not* held hostage to
    it: the cue list is a written account of the same walkthrough and stays on the page,
    under a notice that names the file that is absent."""
    rel = s["video"]
    cues_path = out_dir / rel.replace(".webm", ".cues.json")
    cues = json.loads(cues_path.read_text(encoding="utf-8")) if cues_path.is_file() else []
    rt = s.get("runtime") or {}
    items, unplaced = _link_captions(cues, s.get("appLinks", []), bool(rt.get("drive")))
    player = (f'<video controls preload="metadata" src="{html.escape(rel)}"></video>'
              if (out_dir / rel).is_file() else
              f'<p class="embedded-note"><b>Not filmed.</b> <code>{html.escape(rel)}</code> '
              'was not produced by this run, so there is no player here — the narration '
              'below is what the recording would have shown, and it is the only part of '
              'this section that is not evidence.</p>')
    # A screen the branch touched and the film never showed is a fact about the *coverage
    # of the film*, so it belongs to the transcript, not to the page under it. It used to
    # be a paragraph of its own below the player — a full block of vertical space, in the
    # page's own prose voice, for a footnote. As the transcript's last row it costs no
    # height at all (the cue list is a fixed-height scroller) and it is read where the
    # question it answers is actually asked: "is that everything the film covered?".
    # No `data-t`: there is no frame to seek to, which is the whole point of the row.
    if unplaced:
        items += ('<li class="uncovered"><span class="ts">--:--</span><span>'
                  '<b>Not filmed.</b> Touched by this change: '
                  + " · ".join(_app_anchor(l["href"])
                               + f'{html.escape(l.get("label") or l["href"])}</a>'
                               for l in unplaced) + ".</span></li>")
    # The verdict sits OUTSIDE the wrap, not inside it beside the player: `.vidwrap` is a
    # two-column grid, so a band emitted as one of its children takes a column and stands
    # next to the picture instead of across the top of it. What it contradicts is the
    # picture, so it has to be the thing read first, full width.
    return (runtime_html(rt) + video_verdict_html(rel, out_dir)
            + f'<div class="vidwrap">{player}<ol class="transcript">{items}</ol></div>')


def embed_html(s, out_dir: Path) -> str:
    """Another tool's whole report, framed rather than re-drawn.

    `aria-label`, not `title`: a `title` on an iframe is a native tooltip, and this page has
    exactly one tooltip component. The label is the same string either way, and a screen
    reader reads it from `aria-label` just as happily."""
    e = s.get("embed")
    if not e:
        return ""
    # `src` may carry a fragment — a framed report that reads its own hash can be opened
    # on a particular view (`…#only-touched`). Only the path in front of it is a file.
    path = e["src"].split("#", 1)[0]
    if not (out_dir / path).is_file():
        # The tool that writes it is an optional install. Say which one is missing rather
        # than framing a 404.
        return (f'<p class="sub">No embedded report at <code>{html.escape(path)}</code>'
                + (f' — { e["missing"]}' if e.get("missing") else "")
                + ".</p>")
    return (f'<iframe class="{html.escape(e.get("class", "oacframe"))}" '
            f'src="{html.escape(e["src"])}" '
            f'aria-label="{html.escape(e.get("label", ""))}"></iframe>')


def resolve_refs(items, root: Path):
    """Turn `path:from-to` strings into {label, abs} so the renderer can link them.

    A reference to a file that is not there is a build failure, not a link. A snippet
    already fails loudly — `extract-snippet.py` cannot cut lines out of nothing — but a
    bare ref used to render whatever it was given, so a path that went stale (a file
    renamed on the base branch, say) reached the reviewer as a deep link that silently
    did nothing when clicked. Failing here costs one build; failing there costs the
    reviewer's trust in every other link on the page.
    """
    out = []
    missing = []
    for ref in items:
        rel, _, pos = ref.rpartition(":")
        start = pos.split("-")[0]
        target = (root / rel).resolve()
        if not target.is_file():
            missing.append(ref)
        out.append({"label": ref, "abs": f"{target}:{start}:1"})
    if missing:
        raise SystemExit(
            "[review] these references point at files that do not exist:\n  "
            + "\n  ".join(missing)
            + "\nFix the path in the content file (a base-branch rename is the usual cause)."
        )
    return out


ANCHOR = re.compile(r'<a\s+([^>]*?)href="(?P<href>[^"]*)"([^>]*)>', re.I)
TARGET_ATTR = re.compile(r'\s+target="[^"]*"', re.I)


def one_tooltip_only(doc: str) -> str:
    """Every native `title` becomes the page's own tooltip.

    Our own generators emit `data-tip` directly. PlantUML does not: it turns a
    `[[url{hint}]]` in the diagram source into `title="hint"` inside the SVG we inline,
    and that generator is not ours to change. Rewriting the assembled document is the
    one place that catches both. `<title>` *elements* are a different thing and are
    left alone — the regex only matches the attribute."""
    return re.sub(r'(<[a-zA-Z][^>]*?)\stitle="', r'\1 data-tip="', doc)


def check_baked_excerpts(doc: str) -> None:
    """Does every excerpt a fragment baked in actually hold the lines its label claims?

    A fragment that quotes code carries the lines as data — one entry per source line —
    beside a label saying which lines they are. Nothing forces the two to agree, and when
    they disagree it is invisible: `visits.spec.ts:27-43` showing sixteen lines looks
    exactly like `visits.spec.ts:27-43` showing seventeen, because the reader does not have
    the file open to count against. What they see is a test method with no closing brace —
    a method they cannot see the end of, and cannot tell whether that is the range or the
    code.

    Which is what happened: every one of a map's twenty-seven excerpts was cut one line
    short, `lines[a-1:b-1]` instead of `lines[a-1:b]`, and the page shipped that way. The
    check is arithmetic the build can do and the author cannot, so it belongs here — and
    it only warns, because a fragment is hand-authored and the build is not the place to
    refuse one."""
    for data in re.findall(r'class="rm-data">(.*?)</script>', doc, re.S):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        for test in (parsed.get("tests") or {}).values():
            for part in test.get("parts") or []:
                m = re.search(r":(\d+)(?:-(\d+))?$", part.get("label") or "")
                if not m:
                    continue
                want = int(m[2] or m[1]) - int(m[1]) + 1
                got = len(part.get("html") or [])
                if want != got:
                    print(f"[review] excerpt {part['label']} is labelled {want} lines but "
                          f"quotes {got} — the reader sees a window that stops early, with "
                          f"nothing on the page to say so", file=sys.stderr)


def open_links_in_new_tabs(doc: str) -> str:
    """Every outbound link leaves the guide in a new tab — a reviewer reading this page
    should never lose their place in it. In-page anchors keep the current tab (a new tab
    for a jump to a section is nonsense).

    `vscode://` is deliberately *not* given one. A new tab was tried, for the guide read
    inside VS Code's Simple Browser — and it made things worse: the browser opened another
    Simple Browser tab, pointed it at the `vscode://` URL and rendered a blank page, so
    every click left a dead tab behind. A webview cannot hand a custom scheme to the OS at
    all; no anchor markup changes that. EDITOR_JS handles both cases instead — navigating
    in place at top level, copying the reference where it cannot."""

    def fix(m):
        whole = m.group(0)
        href = m.group("href")
        if href.startswith("vscode:"):
            # PlantUML stamps `target="_top"` on the links it renders into an SVG, which
            # inside a webview navigates the whole frame to a scheme it cannot open and
            # leaves a blank page where the guide was. Strip any target: these links are
            # driven by EDITOR_JS, never by the browser's own navigation.
            return TARGET_ATTR.sub("", whole)
        if href.startswith("#") or "target=" in whole.lower():
            return whole
        return whole[:-1] + ' target="_blank" rel="noopener">'

    return ANCHOR.sub(fix, doc)


# The code-owners flag is the one thing on this page whose severity is *discovered* at
# build time rather than authored: whether a merge is blocked depends on the diff, not on
# what we wrote about it. So the renderer runs the check itself instead of including a
# fragment somebody remembered to regenerate — a stale "no owner touched this" is worse
# than no tab at all.
def codeowners_fragment(block, root: Path, out_dir: Path):
    dest = out_dir / block.get("out", "assets/codeowners.html")
    cmd = [sys.executable, str(CODEOWNERS), "--base", block.get("base", "origin/main"),
           "--out", str(dest), "--json"]
    if block.get("noUntracked"):
        cmd.append("--no-untracked")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "[review] codeowners-check.py failed")
    return dest.read_text(encoding="utf-8"), json.loads(proc.stdout)


# The summary walks the reader through the strip — "Eleven tabs, one question each, start
# on Review, then …". Written by hand it is a second copy of the strip, and the second copy
# is the one that rots: a tab added at the end of `tabs` leaves the sentence saying "Ten"
# and skipping the newcomer, and nothing anywhere complains. So the number is a token the
# build fills in from the tabs it actually emitted, and the names are checked against the
# same list.
TAB_COUNT_TOKEN = "{{tabcount}}"
NUMBER_WORDS = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve "
                "Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen Twenty").split()


def spelled(n: int) -> str:
    return NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)


def check_tab_enumeration(lede: str, labels: list[str]) -> None:
    """Warn when the lede's walk-through has drifted from the strip it describes.

    Not a build failure: prose is judgement, and a lede may legitimately group two tabs
    into one clause or leave a self-evident one out. But it may not do so *by accident*,
    which is what silence would make indistinguishable from a rotted sentence."""
    if not lede:
        return
    seen, missing = [], []
    for label in labels:
        # A label may carry a marker the prose has no business repeating — "🤖 Review"
        # on the pill, "Review" in the sentence. Match on the words, not the badge.
        words = label.lstrip("".join(c for c in label if not c.isalnum())).strip()
        at = lede.find(html.escape(words or label))
        (seen if at >= 0 else missing).append((at, label))
    if missing:
        print("[review] WARNING: the summary never names these tabs: "
              + ", ".join(l for _, l in missing)
              + f" — the strip has {len(labels)} of them and the summary walks "
                f"through {len(seen)}.",
              file=sys.stderr)
    out_of_order = [l for (a, l), (b, _) in zip(seen[1:], seen) if a < b]
    if out_of_order:
        print("[review] WARNING: the summary names tabs in a different order than the "
              "strip does, from: " + ", ".join(out_of_order), file=sys.stderr)


LOGEXTRACT = HERE / "logextract.py"


SRCREF_HREF = re.compile(r'(<a class="srcref" href="vscode://file/[^:"]*)(?::\d+){0,2}"')


def _aim_at_statement(snippet: str, ref: str, hits) -> str:
    """Point a quoted window's `path:line` link at the statement it is quoting.

    Everything else on this page links a snippet to its first line, which is right when the
    snippet *is* the thing. Here it is not: the snippet is four lines of context around one
    `log.warn(...)`, and landing the reader on the first of them makes them find it again by
    eye. The extractor already knows the line and the column, so the link uses them — and
    only when exactly one known statement falls inside the window, because two would make
    the choice a guess."""
    rel, _, span = ref.rpartition(":")
    lo = int(span.split("-")[0])
    hi = int(span.split("-")[-1])
    inside = [h for h in hits if h["file"] == rel and lo <= h["line"] <= hi]
    if len(inside) != 1:
        return snippet
    h = inside[0]
    return SRCREF_HREF.sub(lambda m: f'{m.group(1)}:{h["line"]}:{h["column"]}"', snippet, count=1)


def _logging_aside(part, found, what, root: Path, hits=()) -> str:
    """One of the two context registers under the added-logging finding.

    The prose and the snippets are the author's — a log line is only interesting once
    somebody says what is wrong with it — but the *count* is the extractor's, so a section
    that quotes three of four statements is caught here rather than by a reader."""
    if not part:
        return ""
    quoted = len(part.get("snippets", []))
    if found and quoted != found:
        print(f"[review] WARNING: the logging tab quotes {quoted} {what} statement(s) but "
              f"logextract found {found} — one of the two is out of date.", file=sys.stderr)
    return (
        f'<h2 id="{html.escape(part["id"])}">{html.escape(part["title"])}</h2>'
        + part.get("body", "")
        + "".join(_aim_at_statement(
            snippet_html(x["ref"], x.get("caption"), root, exact=True), x["ref"], hits)
            for x in part.get("snippets", []))
    )


# --------------------------------------------------------------------------- #
# GDPR verdict per logging statement — a real model call, not a word list.
#
# A word list over the argument names was tried first and rejected: it comes out SAFE
# for `log.info("{}", x)` when `x` was assigned three lines up from
# `owner.getName()`, because `x` looks like nothing. Knowing what a value actually
# holds means following the assignment, and no word list does that — so this asks a
# model, and gives it enough source to trace it: the statement's enclosing method
# (parameters and locals both) plus the class's field declarations, never the whole
# file and never the one line alone. `AI Evaluation` on the legend is therefore an
# accurate label, not the aspirational one a word list would have made it.
# --------------------------------------------------------------------------- #

# One mark, one sentence, for every verdict a model produced.
AI_MARK = '<sup class="ai-mark" data-tip="LLM evaluated">\U0001F916</sup>'

PRIVACY_MARK = {
    "safe": ("✅", "SAFE", "added"),
    "doubt": ("🤔", "DOUBT", ""),
    "privacy": ("❌", "PRIVACY", "removed"),
    # Not a fourth colour on the same footing as the other three: this is what a
    # guessed SAFE would have looked like if the model could not be reached and this
    # function papered over it instead of admitting so. DOUBT means the model looked
    # and said it could not tell; this means it was never successfully asked at all.
    "error": ("⚠️", "NOT EVALUATED", "warn"),
}

# No `chain` field any more. Asking the model where a value came from, and then
# rendering its answer as a list of `file:line` + the source line, was a paraphrase of
# code standing where the code could have stood. `logextract.py` walks that back
# syntactically now and the snippet quotes the real lines, so the only thing left for
# the model is the part no line of Java says out loud: whether the value is personal
# data. `trace` is that, in one clause.
# One clause per *value*, not one sentence per statement. A statement that logs three
# things and gets one fused sentence makes the reader do the un-fusing, and the thing
# they are trying to find out — which of the three carried the risk — is exactly what
# the fusing destroyed. So the model answers per value, keyed by the argument as it is
# written in the source, and the page renders one bullet per logged value.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
                    "note": {"type": "string"},
                },
                "required": ["name", "verdict", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "values"],
    "additionalProperties": False,
}

# SAFE < DOUBT < PRIVACY. The headline verdict is the worst of what the bullets say and
# what the model called the statement overall — never better than its own worst bullet,
# which is the one way a per-value answer could have made the page *less* honest than
# the single sentence it replaced.
VERDICT_RANK = {"safe": 0, "doubt": 1, "privacy": 2}


def _worst_verdict(*verdicts: str) -> str:
    real = [v for v in verdicts if v in VERDICT_RANK]
    return max(real, key=lambda v: VERDICT_RANK[v]) if real else "doubt"

VERDICT_SYSTEM_PROMPT = (
    "You are a precise static-analysis assistant embedded in a code review build script. "
    "You are given one Java logging statement plus enough of its surrounding source to "
    "trace where each interpolated value comes from. Decide whether the statement, once "
    "it executes, could write personal data (GDPR-relevant: a name, an email address, a "
    "phone number, a postal address, a government ID, free text about a person, or "
    "similar) to a log aggregator kept for months.\n\n"
    "Trace each interpolated value through the source you were given: where it is "
    "declared or assigned, and onward if that right-hand side is itself another "
    "variable. If you cannot resolve a value with what you were given, its verdict is "
    "DOUBT -- never guess SAFE past a value you could not follow.\n\n"
    "Answer per VALUE, not per statement. Return one entry in `values` for EVERY value "
    "the statement interpolates -- exactly those, no more and no fewer -- in the order "
    "they appear in the call. Set each entry's `name` to the argument EXACTLY as it is "
    "written in the source (`vetId`, `owner.getName()`), so the page can line your "
    "answer up with the call; do not rename, shorten or paraphrase it. A statement that "
    "interpolates nothing gets an empty `values` list. The top-level `verdict` is the "
    "worst of the individual ones.\n\n"
    "The page already shows the reader the statement AND the lines each value came "
    "from, quoted verbatim from the file. So a `note` must not retell any of that: no "
    "file names, no line numbers, no restating a declaration the reader is looking at, "
    "no naming the enclosing method, no repeating the value's own name (the bullet is "
    "already labelled with it), and no restating the verdict (`not personal data`, "
    "`safe`, `a privacy risk` -- the bullet already carries its own mark). Give only "
    "what the code cannot say for itself: WHAT that value actually holds, as ONE noun "
    "phrase of at most 15 words, no trailing full stop.\n"
    "Good: `{\"name\": \"vetId\", \"verdict\": \"SAFE\", "
    "\"note\": \"just a numeric vet database id\"}`.\n"
    "Good: `{\"name\": \"owner.getName()\", \"verdict\": \"PRIVACY\", "
    "\"note\": \"the owner's full name, straight into the log\"}`.\n"
    "Bad (restates the verdict): `\"note\": \"a numeric id, not personal data\"`.\n"
    "Bad: `{\"name\": \"vetId\", \"note\": \"vetId is the declared Integer "
    "parameter of resolveVet(Integer vetId) (VisitRestController.java:89), a numeric "
    "database identifier passed straight into the log call...\"}`.\n\n"
    "Respond only through the given JSON schema."
)


def _statement_context(h: dict) -> str:
    """Enough source for a model to trace every interpolated argument: the enclosing
    method (`logextract.py` resolves its range structurally, the same AST pass that
    finds the statement itself) so parameters and locals are both visible, plus the
    class's field declarations (numbered, so a field-rooted value can be placed)
    for a value that turns out to come from `this`. Never the whole file — a class with
    forty methods is forty methods of noise around the one that matters — and never just
    the statement alone, which is the version of this feature that cannot tell a
    parameter from a field from thin air, let alone trace a value past either of them."""
    ms, me = h.get("method_start"), h.get("method_end")
    try:
        src_lines = Path(h["abs_file"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        src_lines = []
    if ms and me and src_lines:
        window = src_lines[ms - 1:me]
        numbered = "\n".join(f"{n:>4}  {l}" for n, l in zip(range(ms, me + 1), window))
        method_block = f"Enclosing method ({h['file']}:{ms}-{me}):\n{numbered}"
    else:
        method_block = ("No enclosing method could be resolved. All that is available "
                        f"is the statement itself:\n{h['raw_line'].strip()}")
    fields = h.get("_fields") or []
    if fields:
        field_lines = "\n".join(f"{f['line']:>4}  {f['type']} {f['name']}" for f in fields)
        fields_block = f"Class fields in scope ({h['file']}):\n{field_lines}"
    else:
        fields_block = "Class fields in scope: none."
    return f"{method_block}\n\n{fields_block}"


def _verdict_prompt(h: dict, context: str) -> str:
    return (
        f"Logging statement ({h['file']}:{h['line']}):\n    {h['text']}\n\n"
        f"{context}\n\n"
        "Which value(s) does this statement log, where does each come from, and is any "
        "of it personal data? Give one entry per interpolated value, named exactly as "
        "the argument is written above, plus the statement's overall verdict."
    )


def _claude_bin() -> str | None:
    for cand in (os.environ.get("CLAUDE_BIN"), "claude"):
        p = shutil.which(cand) if cand else None
        if p:
            return p
    return None


def _call_privacy_model(prompt: str) -> dict:
    """One live model call. Returns `{"verdict","values","cost_usd"}` on success, or
    raises `RuntimeError` with a message written to go straight on the page — a missing
    binary, a non-zero exit, a timeout, or a response that does not match the schema.
    Never returns a guessed verdict; the caller turns any exception here into the loud
    `error` state, not a fallback answer. `values` is validated for *shape* only —
    whether it actually covers the values the statement logs is decided against
    `logextract.py`'s argument list at render time, not against the model's word."""
    claude_bin = _claude_bin()
    if not claude_bin:
        raise RuntimeError("the `claude` CLI is not on PATH (set $CLAUDE_BIN to point at it)")
    cmd = [claude_bin, "-p", prompt, "--output-format", "json",
           "--model", os.environ.get("PRIVACY_VERDICT_MODEL", "sonnet"),
           "--restricted", "--strict-mcp-config", "--no-session-persistence",
           "--max-turns", "1", "--system-prompt", VERDICT_SYSTEM_PROMPT,
           "--json-schema", json.dumps(VERDICT_SCHEMA)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the model call timed out")
    except OSError as e:
        raise RuntimeError(f"could not run `claude`: {e}")
    # The answer decides, not the exit code. `claude -p --json-schema --max-turns 1`
    # stops on the structured-output tool call and can exit non-zero while stdout holds
    # a complete, schema-conforming, already-paid-for response (`is_error: false`,
    # `subtype: "success"`). Reading the exit code first threw that answer away and put
    # the loud "the model could not be reached" on a page whose model *had* been
    # reached — the one state that is supposed to mean nobody was ever asked. So parse
    # first, and let a bad exit only colour the message when the payload is unusable
    # too. Nothing here is loosened: an unparsable body, `is_error`, or a response that
    # misses the schema still raises, and no verdict is ever guessed.
    exited = (f" (the CLI also exited {proc.returncode}"
              + (f": {proc.stderr.strip()[-200:]}" if proc.stderr.strip() else "")
              + ")") if proc.returncode != 0 else ""
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"the model call returned unparsable output{exited}")
    if payload.get("is_error"):
        raise RuntimeError(f"the model call failed: {str(payload.get('result'))[:300]}{exited}")
    out = payload.get("structured_output")
    values = out.get("values") if isinstance(out, dict) else None
    if (not isinstance(out, dict) or out.get("verdict") not in ("SAFE", "DOUBT", "PRIVACY")
            or not isinstance(values, list)
            or not all(isinstance(v, dict) and v.get("name") and v.get("note")
                       and v.get("verdict") in ("SAFE", "DOUBT", "PRIVACY")
                       for v in values)):
        raise RuntimeError("the model's response did not match the expected verdict "
                           f"schema{exited}")
    return {"verdict": out["verdict"].lower(),
            "values": [{"name": v["name"], "verdict": v["verdict"].lower(),
                        "note": v["note"]} for v in values],
            "cost_usd": payload.get("total_cost_usd") or 0.0}


def _verdict_cache_path(root: Path) -> Path:
    return root / ".human-review" / ".privacy-verdicts.json"


def _load_verdict_cache(root: Path) -> dict:
    p = _verdict_cache_path(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_verdict_cache(root: Path, cache: dict) -> None:
    p = _verdict_cache_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


#: Set by `--no-model`. A build that cannot ask is not the same as a model that could not
#: be reached, and the page says so in those words — the Logging tab already has a state
#: for "nobody was ever asked", and this is one honest way of arriving in it.
OFFLINE = False


def _offline_call(prompt: str) -> dict:
    raise RuntimeError("not evaluated: this build ran with --no-model, so the Logging "
                       "tab shows only verdicts an earlier run already paid for")


def privacy_verdict(h: dict, root: Path, cache: dict, call=None) -> dict:
    """SAFE / DOUBT / PRIVACY / error, with one clause per interpolated value — a live
    model call over the statement's enclosing method, cached by a hash of exactly what
    was sent (the statement plus its context) so a re-run on unchanged code neither
    flips the answer nor pays for it twice. `cache` is loaded once by the caller and
    mutated here; the file is rewritten on every new entry, not batched, so a run that
    dies partway through does not lose the calls it already paid for.

    The key hashes the system prompt alongside the statement and its context, so an edit
    to what the model is *asked* invalidates the cache the same way an edit to the code
    does — a shortened `trace` instruction that kept serving the old paragraph out of
    cache would be a silent no-op.

    `call` defaults to `None`, resolved to `_call_privacy_model` *inside* the body
    rather than as `def ...(call=_call_privacy_model)` — a default bound at def-time
    would freeze in the original function object, so patching the module-level name
    for a test (`monkeypatch.setattr(build, "_call_privacy_model", fake)`) would
    silently do nothing here; every caller that does not pass its own `call` needs the
    patch to actually take."""
    call = call or (_offline_call if OFFLINE else _call_privacy_model)
    context = _statement_context(h)
    key = hashlib.sha256(
        (VERDICT_SYSTEM_PROMPT + "\n" + h["text"] + "\n" + context).encode("utf-8")
    ).hexdigest()
    cached = cache.get(key)
    if cached:
        return {**cached, "cached": True, "cost_usd": 0.0}
    try:
        result = call(_verdict_prompt(h, context))
    except RuntimeError as e:
        return {"verdict": "error", "values": [], "note": str(e),
                "cached": False, "cost_usd": 0.0}
    entry = {"verdict": result["verdict"], "values": result.get("values") or []}
    cache[key] = entry
    _save_verdict_cache(root, cache)
    return {**entry, "cached": False, "cost_usd": result.get("cost_usd", 0.0)}


# How many origin lines one entry may pull in. `logextract.py` already caps the walk
# (three hops per value, six lines per statement); this is the *page's* cap on top of
# that, and it is deliberately tighter, because the failure here is not a wrong answer,
# it is a tab. This tab lists every touched Java file, and an entry that grows from
# three lines to twenty to show a chain nobody asked about has made the tab worse in
# exactly the way the prose it replaced did.
MAX_ORIGIN_LINES_SHOWN = 4


def _logging_ref(h: dict) -> str:
    """The snippet reference for one statement: its own line(s), plus the lines its
    interpolated values were traced back to — `Foo.java:89,93`.

    The origins are the extractor's (`logextract.py` walks them syntactically, from the
    ast-grep graph); all that happens here is the cap and the sort. Nearest-first, so
    when the budget runs out what survives is the hop closest to the statement — the one
    a reader would have looked at first anyway — and never a far-away line with the
    intervening ones silently dropped."""
    end = h.get("end_line") or h["line"]
    spans = [f'{h["line"]}' if end == h["line"] else f'{h["line"]}-{end}']
    origins = sorted({o["line"] for o in (h.get("origins") or [])
                      if not (h["line"] <= o["line"] <= end)},
                     key=lambda n: abs(n - h["line"]))[:MAX_ORIGIN_LINES_SHOWN]
    spans += [str(n) for n in sorted(origins)]
    return f'{h["file"]}:{",".join(spans)}'


def _value_bullets(args: list, values: list) -> tuple[list[dict], bool]:
    """One row per value the statement actually logs — driven by `logextract.py`'s
    argument list, never by whatever the model chose to mention.

    The model is asked for one clause per interpolated value; a model that quietly drops
    one must not quietly drop it from the page, so the rows come from the *code* and the
    model's clauses are matched onto them. Anything left without a clause renders as its
    own `unresolved` row and drags the headline verdict down — that is the same rule the
    tab already applies to a value the model could not follow, and for the same reason:
    "nobody said" must never read like "nothing to say".

    Matching is by the argument text (whitespace-insensitive), then by root identifier
    when that is unambiguous among the rows still unmatched — a model that answers
    `owner` for `owner.getName()` is answering the right question with a shorter name,
    but only while there is exactly one candidate it could mean.

    Returns the rows and whether any of them came out unresolved."""
    rows = [{"arg": a, "verdict": None, "note": None} for a in args]
    pool = list(enumerate(rows))

    def norm(t):
        return re.sub(r"\s+", "", t or "")

    def claim(idx, v):
        # `_call_privacy_model` already lower-cases, but `privacy_verdict` accepts any
        # `call`, and a verdict word is a key into `PRIVACY_MARK` two functions later.
        verdict = str(v.get("verdict") or "").lower()
        rows[idx]["verdict"] = verdict if verdict in VERDICT_RANK else None
        rows[idx]["note"] = v.get("note")

    unmatched = []
    for v in values:
        hit = next((i for i, r in pool if r["verdict"] is None
                    and norm(r["arg"]) == norm(v.get("name"))), None)
        if hit is None:
            unmatched.append(v)
            continue
        claim(hit, v)
    for v in unmatched:
        root = logextract_root(v.get("name"))
        cands = [i for i, r in pool
                 if r["verdict"] is None and root and logextract_root(r["arg"]) == root]
        if len(cands) == 1:
            claim(cands[0], v)

    broken = any(r["verdict"] is None for r in rows)
    return rows, broken


def logextract_root(expr: str | None) -> str | None:
    """The leading identifier of an expression, for matching a model's `owner` onto the
    page's `owner.getName()`. Deliberately the same reading `logextract.py` uses to root
    its origin walk, imported rather than re-derived so the two cannot drift apart."""
    if not expr:
        return None
    return _logextract().origin_root(expr)


@functools.lru_cache(maxsize=1)
def _logextract():
    """`logextract.py` as a module, not a subprocess — this needs one pure function out
    of it, not a scan. Registered in `sys.modules` *before* `exec_module`: its `@dataclass`
    declarations resolve their own annotations by looking their module up by name, and a
    module executed without being registered is not there to be found."""
    import importlib.util
    if "logextract" in sys.modules:
        return sys.modules["logextract"]
    spec = importlib.util.spec_from_file_location("logextract", str(LOGEXTRACT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["logextract"] = mod
    spec.loader.exec_module(mod)
    return mod


# The alternation inside `log-import`'s constraint: `(org\.slf4j|org\.apache…|ch\.qos\.logback)`.
# Two or more dotted, lower-case package roots between one pair of parentheses is the only
# group in that rule shaped like this — the `(static\s+)?` before it is neither dotted nor
# an alternation — so the rule can be rewritten freely without this having to be told.
_LOG_PKGS_RE = re.compile(r"\(([a-z][\w\\.]*(?:\|[a-z][\w\\.]*)+)\)")


@functools.lru_cache(maxsize=1)
def logging_libraries() -> tuple[str, ...]:
    """The packages the scan actually searches for, in the rule's own order.

    Read out of `logextract.py`'s own `log-import` rule rather than typed here. A list of
    library names on a page is a claim about what a scan looked for, and the only version
    of that claim worth showing is the one that cannot go stale: add a logging API to the
    rule and this list gains it on the next build; nobody has to remember the page.

    The escaping is undone (`org\\.slf4j` is a regex, not a package). Returns empty when
    the regex cannot be found at all — the caller says where to look instead, because
    naming the rule beats inventing a list."""
    rule = _logextract().RULES.get("log-import", "")
    for alt in _LOG_PKGS_RE.findall(rule):
        pkgs = [p.replace("\\", "") for p in alt.split("|")]
        if any("." in p for p in pkgs):
            return tuple(pkgs)
    return ()


def logging_libraries_tip() -> str:
    """The same packages as the hover panel's markup: one per line, in code type.

    Eight dotted package roots welded into a sentence is a list pretending to be prose —
    the reader's question is "is mine in there", and answering it meant reading a
    comma-separated run to the end. One bullet per package, monospace because these are
    identifiers and not words, and the two-line tail says the part that is genuinely
    prose: a logger reached without an import still counts."""
    pkgs = logging_libraries()
    if not pkgs:
        return "<p class=\"tipfoot\">The packages named by logextract.py's log-import rule.</p>"
    items = "".join(f"<li>{html.escape(p)}</li>" for p in pkgs)
    return (f'<ul class="tiplist">{items}</ul>'
            f'<p class="tipfoot">&hellip;plus loggers reached by type, factory or Lombok.</p>')


def _value_bullets_html(rows: list) -> str:
    """The bullets under the verdict: one per logged value, `name — what it is`.

    The name is the argument as the source writes it, in `<code>`, so a reader scanning
    a three-value statement can see which of the three carried the risk without reading
    a sentence that fused them. A row the model never answered says exactly that."""
    if not rows:
        return ""
    items = []
    for r in rows:
        name = f'<code>{html.escape(r["arg"])}</code>'
        if r["verdict"] is None:
            items.append(f'<li class="val-unresolved">{name} — no clause came back for '
                         f'this value; it was not assessed</li>')
            continue
        emoji = PRIVACY_MARK[r["verdict"]][0]
        # The per-value mark is shown only when it differs from "fine": a column of green
        # ticks under a green tick is decoration, and the row a reader must not miss is
        # the one that is not green.
        mark = "" if r["verdict"] == "safe" else f'{emoji} '
        items.append(f'<li>{mark}{name} — {html.escape(r["note"])}</li>')
    return f'<ul class="log-values">{"".join(items)}</ul>'


def _logging_listing(added: list, root: Path, fields_by_file: dict | None = None,
                      call=None, cache_root: Path | None = None) -> str:
    """The leading answer: one code snippet per logging statement this change set
    actually added or modified — the same `.snippet` figure every other quoted line on
    this page uses (`extract-snippet.py`), not a second, invented code-block style.
    Files with nothing to say do not appear here at all.

    An empty list is not silence. `logextract.py` ran and genuinely found zero — see
    `logging_fragment`'s docstring for why that is itself the finding — so it renders as
    a sentence carrying the same weight as the snippets it replaces, never as a blank
    stretch of page that would read exactly like the scan never having run at all.

    `cache_root` defaults to `root` — they are the same directory in production (both
    the reviewed repo's checkout) — and exists as its own parameter only so a test can
    point the verdict cache at a throwaway `tmp_path` while still handing `snippet_html`
    the real repo it needs to resolve a fixture file against."""
    if not added:
        return ('<p class="lede"><b>None.</b> Not one logging statement was added or '
                'changed on the lines this change set touches.</p>')
    cache_root = cache_root or root
    fields_by_file = fields_by_file or {}
    cache = _load_verdict_cache(cache_root)
    boxes = []
    # `new code` / `2 lines changed` — dropped on this tab only. Everywhere else the badge
    # answers "is this quoted block new, or an old one with a line in it?", which is a real
    # question about a snippet a reviewer did not choose. Here it is not: the gutter beside
    # the statement already marks the added lines with `+`, and every block on this tab is
    # here *because* the branch added or rewrote that logging line. A badge repeating the
    # tab's own entry condition on every box is a word the eye has to skip. The rest of the
    # bar stays: the file it came from, and the two handles that open the change.
    BADGE_RE = re.compile(r'<span class="code-badge"[^>]*>[^<]*</span>')
    for h in added:
        ref = _logging_ref(h)
        h = {**h, "_fields": fields_by_file.get(h["file"])}
        result = privacy_verdict(h, cache_root, cache, call=call)
        # The rows come from the code (`logextract.py`'s argument list), the clauses from
        # the model, and the headline verdict from the worst of everything below it — a
        # value the model skipped counts as unassessed, not as fine.
        rows, unassessed = _value_bullets(h.get("args") or [], result.get("values") or [])
        verdict_key = result["verdict"]
        if verdict_key != "error":
            verdict_key = _worst_verdict(verdict_key,
                                         *[r["verdict"] for r in rows if r["verdict"]],
                                         *(["doubt"] if unassessed else []))
        emoji, word, css = PRIVACY_MARK[verdict_key]
        verdict_class = f"privacy-verdict {css}".strip()
        # This box used to take the snippet apart — anchor stripped off the top, a
        # hand-rebuilt copy of it glued into the footer as a bottom-right marker — from
        # back when the header was a bare path on a line of its own and the footer had
        # room to spare. It is a shared component now, carrying the file *and* the two
        # handles that open the change, and a tab that quietly rebuilds a component is a
        # tab that stops getting its fixes. So the bar stays where every other tab has it,
        # at the top, and the footer keeps only what is this tab's own: the verdict.
        # The bar's own link opens at the *first* line of the window, which with origin
        # lines pulled in is the origin rather than the statement. Re-aimed at the hit's
        # own line and column: that is where a reader clicking a logging box expects to
        # land, and it is the one thing the generic bar cannot work out for itself.
        snippet = snippet_html(ref, None, root, exact=True,
                               link_at=(h["line"], h.get("column", 1)))
        snippet = BADGE_RE.sub("", snippet, count=1)
        # The verdict, and nothing else. What used to ride on this line was one run-on
        # sentence about the whole statement; it is a bullet per logged value below,
        # because "which of the three values is the problem" is the question a reader
        # brings here and a fused sentence is precisely what destroys the answer. The
        # location left this row too, upwards, into the source bar every tab shares.
        footer = (
            f'<p class="log-footer">'
            f'<span class="{verdict_class}">{emoji} <b>{word}</b></span>'
            # Outside the span, deliberately: the verdict word ends at the word. This is a
            # note about *how the verdict was reached*, and only where one actually was --
            # NOT EVALUATED means the model was never successfully asked.
            + (AI_MARK if verdict_key != "error" else "")
            + f'</p>'
        )
        # The model-failure state has no per-value answers to show — it never got any —
        # so its one message rides under the verdict in the same place the bullets would.
        note = result.get("note")
        body = (f'<p class="log-note">{html.escape(note)}</p>' if note
                else _value_bullets_html(rows))
        snippet = snippet.replace("</figure>", f"{footer}{body}</figure>", 1)
        boxes.append(snippet)
    # "The page marks tabs by what produced them" — this legend is the disclosure for a
    # tab whose verdicts are a model's reading, not a program's. What it no longer carries
    # is the machinery behind that reading: which half of the box is a live call, how many
    # calls it took, and what they cost. That was a paragraph about the build, on a tab a
    # reviewer opened to read about *their diff*, and the 🤖 on every verdict already says
    # the only part of it they can act on — that a model, not a program, decided this one.
    legend = (
        '<div class="privacy-legend"><p class="privacy-legend-title">🤖 AI Evaluation:'
        f'</p><p class="privacy-legend-note">The code block is the evidence: alongside '
        f'each statement it quotes the lines its logged values came from, walked back '
        f'structurally by <code>ast-grep</code> and cut from the working tree with their '
        f'real line numbers.</p>'
        '<ul class="privacy-legend-list">'
        '<li>✅ <b>SAFE</b> — nothing traced reads as personal data</li>'
        '<li>🤔 <b>DOUBT</b> — could not trace it with confidence, and an unresolved '
        'case is read as DOUBT on purpose rather than guessed SAFE</li>'
        '<li>❌ <b>PRIVACY</b> — a value traced back to personal data, on its way to a '
        'log aggregator kept for months</li>'
        '<li>⚠️ <b>NOT EVALUATED</b> — the model could not be reached; never silently '
        'read as SAFE</li>'
        '</ul></div>'
    )
    return "".join(boxes) + legend


def logging_fragment(block, root: Path):
    """What this change set will say for itself at 3 a.m., found structurally.

    Grep cannot answer this question. `log.info(...)` is a hit and `Math.log(x)` is not, and
    only the syntax tree plus a symbol table of what is actually a logger can tell them
    apart — which is what `logextract.py` does, and why it is a script and not a regex.

    The zero case is the point, not an edge case: a change set that logs nothing is not an
    empty section — it is a finding, said as a plain sentence rather than shown as an
    absence a reader could mistake for the scan not having run. There is no table of every
    touched file behind this any more — a table where almost every row read `0 logging`
    was exactly the noise a reviewer had to read past to find the one or two lines that
    were the actual answer, on every change set, not just the pathological ones — so a
    reviewer who wants proof the scan ran gets that from the tab actually rendering
    (weight 1, a real sentence) rather than from an inventory of the files it walked. A
    dropped tab (ast-grep missing, or the scan failing outright) is the other thing this
    must never be confused with — that path returns `("", 0, 0)` below and the tab
    disappears with a loud line in the build log, which is a different, visible failure
    mode from a real, rendered zero."""
    paths = block.get("paths") or ["."]
    base = subprocess.run(["git", "merge-base", block.get("base", "origin/main"), "HEAD"],
                          cwd=root, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "logging.json"
        proc = subprocess.run(
            [sys.executable, str(LOGEXTRACT), *paths, "--root", str(root), "--repo", str(root),
             "--since", base, "--json", str(report)],
            cwd=root, capture_output=True, text=True)
        if proc.returncode != 0 or not report.is_file():
            # ast-grep is a binary, not a Python dependency, so a machine without it is a
            # real case. Say which tool is missing rather than quietly reporting "no
            # logging" — a false all-clear is the one answer this tab must never give.
            print("[review] logextract.py failed — dropping the logging tab:\n"
                  + proc.stderr.strip()[-500:], file=sys.stderr)
            return "", 0, 0
        payload = json.loads(report.read_text(encoding="utf-8"))

    added = payload.get("changed", payload["all"])["logging"]
    # No heading: the tab is called Logging and the panel opens with it — a `<h2>Logging
    # added/updated` under a selected `Logging` pill is the tab's own label, said twice.
    # The anchor it used to carry rides on the lede instead, so `#logging-added` still
    # lands where it always did.
    #
    # No authored lede either. What stood here was three sentences of methodology (grep
    # vs. ast-grep, `Math.log(x)`, walking the syntax tree) that a reader can see for
    # themselves in the blocks below: every one of them quotes the lines it traced. What
    # they cannot see is *which* libraries were looked for — so that is the one fact left
    # standing, in a line, with the list itself one hover away rather than spent on the
    # page. It is computed, never typed: `logging_libraries` reads the packages back out
    # of the very rule `logextract.py` runs, so the hover cannot claim a library the scan
    # does not actually search for.
    head = (f'<p class="lede" id="{html.escape(block.get("id", "logging-added"))}">'
            f'Found structurally searching for '
            f'<span class="dfn" data-tip-side="right"'
            f' data-tip-html="{html.escape(logging_libraries_tip(), quote=True)}">'
            f'common logging libraries</span>.</p>')
    body = ""
    # No header bar and no surrounding card any more: no heading repeating "logging", no
    # count pill, no `path, base…HEAD` provenance line — the tab's own title already says
    # "logging", and the snippets below say what they are without a caption restating it.
    # The snippets and the legend sit directly on the page, exactly like every other
    # block's content on this tab. `_logging_listing` alone decides what shows: the real
    # snippets, or the explicit "None." sentence for a genuine zero.
    listing = _logging_listing(added, root, payload.get("stats", {}).get("fields", {}))
    frag = (
        head + body + listing
        # "What does this service log today" is the question a reader asks in the same
        # breath as "what did this branch add", and `System.out` is a third answer that must
        # not be counted as a fourth logger. Both are context, both sit under the finding.
        + _logging_aside(block.get("existing"), len(payload["all"]["logging"]),
                         "pre-existing logging", root, payload["all"]["logging"])
        + _logging_aside(block.get("console"), len(payload["all"]["antipattern"]),
                         "console-output", root, payload["all"]["antipattern"])
    )
    # Weight is 1 whenever the scan actually ran — never tied to the header bar or the
    # card that used to wrap the snippets, both gone now, and never computed from `n`
    # either. The zero is not "we looked at unrelated context and nothing moved" — the
    # tab that gets struck through — it is a statement *about this diff*: twelve touched
    # Java files, five hundred added lines, and not one of them will say anything at 3
    # a.m. Striking that through would file the finding as a non-event, and dropping the
    # tab (weight 0) would be worse: that reading is reserved for the one case that is
    # not a real answer — `ast-grep` missing or the scan crashing outright, handled above
    # by returning `("", 0, 0)` before any of this runs.
    assert listing, "logging_fragment must always have content: a real listing or the zero sentence"
    return frag, 1, 1


# What an entry must carry. A nested tuple means *any one of these* — the rule is that an
# item has to say something past its title, not that it has to say it in a particular key.
# `body` was the only accepted place for months, because a model writing the content file
# put its prose there; an item parsed out of `review-points.md` often has no prose at all
# and carries its whole argument in `why:` (a finding that was declined) or in
# `alternative:` (a reading that was not taken). Both are the item saying something
# checkable, and refusing them would mean the branch's own record could not satisfy a rule
# written for a different author.
REQUIRED = {
    "sections": ("id", "title"),
    "tabs": ("id", "label"),
    "findings": ("title", ("body", "why")),
    "assumptions": ("title", ("body", "why", "alternative")),
    "autofixes": ("title",),
}


def cost_chip(root: Path) -> dict | None:
    """What this review run consumed, asked of the run itself.

    Returns None — dropping the chip rather than showing a wrong one — whenever the answer
    cannot be trusted: no session id in the environment (the page was built outside a
    Claude Code session), or no transcript for it.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file():
        return None
    proc = subprocess.run([sys.executable, str(script), "--chip"],
                          cwd=root, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost chip: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


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


# What a diffstat must never count. Every path below is written by a generator -- a
# sequence diagram redrawn from a trace, an API client regenerated from a spec, a lock
# file resolved by a package manager -- and none of it is code a reviewer reads.
#
# Counting them does not merely inflate the number, it inverts it. On the branch this was
# written for, one regenerated `endpoint-complexity.json` supplied 1405 of 2896 added
# lines, and the redrawn `.genseq.*` pairs supplied almost every deletion: a reviewer
# reading `-333 lines` was reading a diagram being redrawn, not a line of logic being
# removed. The chip is there to say how much there is to read, and a number dominated by
# machine output answers a different question than the one being asked.
#
# `exclude` in the content file adds to this list; it never replaces it. There is no way
# to switch the default off, because "count the generated files too" is not a reviewing
# preference -- it is the mistake this exists to prevent. The tooltip states the
# unfiltered totals anyway, so nothing is hidden, only ranked.
GENERATED_PATHSPECS = [
    "*/generated/*", "generated/*",
    "*.genseq.json", "*.genseq.puml",
    "*.min.js", "*.min.css", "*.snap",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "package-lock.json", "*/package-lock.json",
    "yarn.lock", "*/yarn.lock",
    "pnpm-lock.yaml", "*/pnpm-lock.yaml",
    "go.sum", "*/go.sum",
    "Cargo.lock", "*/Cargo.lock",
    "poetry.lock", "*/poetry.lock",
    ".human-review/*",
]


def _git(root: Path, *args: str) -> str | None:
    """A git command whose failure is an answer, not an exception.

    Every caller here is asking a question that can legitimately have no answer -- a ref
    that does not exist locally, a range that cannot be walked -- and each of them turns
    `None` into a dropped chip or a dropped warning rather than a wrong one.
    """
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def _resolve_base(root: Path, named: str) -> tuple[str, str] | None:
    """Which ref the page should actually measure against, given the name it was told.

    A content file says `"base": "main"`, and on the machine the review is built on that
    is a *local* branch which may be days behind the remote it names. Comparing against it
    charges the branch under review with every commit the local ref has not pulled yet:
    on the branch this was written for, local `main` was 18 commits behind `origin/main`,
    and diffing against it reported 142 files and 4099 deleted lines for a change set that
    deletes 39. So a bare name resolves to `origin/<name>` when that exists -- the ref a
    pull request would actually merge into -- and only falls back to the local branch when
    there is no remote-tracking ref to prefer. A name that already carries a remote
    (`origin/main`, `upstream/main`) is taken at its word.

    Returns `(ref, sha)`, or None when nothing by that name resolves at all. Deliberately
    never falls back to HEAD: a base that will not resolve must not silently become the
    thing it is supposed to be compared against, or the page reports a change set of zero
    and calls it a clean review.
    """
    candidates = [named] if "/" in named else [f"origin/{named}", named]
    for ref in candidates:
        sha = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return ref, sha
    return None


def base_state(root: Path, named: str) -> dict | None:
    """Where the base sits relative to the branch -- the two ways the comparison goes stale.

    A review page is a claim about a *pair* of refs, and it keeps being rendered long after
    one of them has moved. Two distinct things can be wrong, and they need saying
    differently because the fix differs:

    `ahead` -- commits on the base that are not on the branch. The branch forked from
    behind and has stayed there, so every diagram, count and finding on the page describes
    a merge that has not been rehearsed against what main actually contains now. Merging or
    rebasing makes it zero, which is exactly why the warning disappears on its own: there
    is no flag to clear and nothing to remember.

    `localBehind` -- the *local* branch named as the base is behind its own remote. Nothing
    is wrong with the branch under review here; what is stale is the yardstick. This one is
    quieter and nastier than the first: the page looks current, the numbers look measured,
    and they are measured against a main from last week.

    Returns None when the base does not resolve, which drops the marker rather than
    inventing a reassuring absence of one.
    """
    resolved = _resolve_base(root, named)
    if not resolved:
        return None
    ref, sha = resolved
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not head:
        return None
    merge_base = _git(root, "merge-base", sha, head)

    def count(rng: str) -> int | None:
        n = _git(root, "rev-list", "--count", rng)
        return int(n) if n and n.isdigit() else None

    state = {
        "named": named,
        "ref": ref,
        "sha": sha,
        "head": head,
        "mergeBase": merge_base,
        # Commits the base has that the branch does not. Not `merge_base != sha`: the
        # count is the number a reader acts on ("nine commits behind"), and the boolean
        # falls out of it.
        "ahead": count(f"{head}..{sha}"),
        "localRef": None,
        "localBehind": None,
    }
    # Only meaningful when a *local* branch of that name exists beside the remote one we
    # preferred. `origin/main` given verbatim in the content file has no local twin to be
    # behind, and neither does a repository with no remote at all.
    if ref != named and _git(root, "rev-parse", "--verify", "--quiet", f"{named}^{{commit}}"):
        state["localRef"] = named
        state["localBehind"] = count(f"{named}..{ref}")
    return state


def base_warning(state: dict | None) -> str | None:
    """The sentence behind the `!` on the base chip, or None when the pair is current.

    Both conditions are reported in one tooltip when both hold, because they compound: a
    branch forked from behind a base that is *itself* behind its remote is two hops from
    the merge it claims to describe, and a reader told only about one of them will fix
    that one and trust the rest.
    """
    if not state:
        return None
    # Short, like every other hover on the scope bar. Each clause names the gap and the
    # one command that closes it -- which is all a reader standing over the page can act
    # on. Why it matters (nothing here was measured against those commits; the page looks
    # current while its yardstick is a week old) is in this function's docstring, for
    # whoever is fixing the build rather than reading it.
    parts = []
    ahead = state.get("ahead")
    if ahead:
        parts.append(f"{state['ref']} is {ahead} commit{'s' if ahead != 1 else ''} ahead of "
                     "the fork point. Merge or rebase, then rebuild.")
    behind = state.get("localBehind")
    if behind:
        # Not `git fetch`: the count above was read off the remote-tracking ref, so the
        # fetch has already happened, and it never moves the local branch anyway. What
        # closes this gap is fast-forwarding the local branch onto what was fetched.
        parts.append(f"Compared against {state['ref']} ({state['sha'][:8]}); local "
                     f"{state['localRef']} is {behind} behind it. "
                     f"git branch -f {state['localRef']} {state['ref']}.")
    return " ".join(parts) or None


def _numstat(root: Path, rng: str, pathspecs: list[str]) -> tuple[int, int, int, int, int]:
    """`(files_added, files_edited, files_deleted, lines_added, lines_removed)` for a range.

    Binary files report `-` for both line counts; they are counted as files touched and
    contribute no lines, which is the only honest reading -- "a PNG changed by 14142 bytes"
    is not a number that belongs beside a count of lines a human reads.
    """
    args = ["diff", "--numstat", rng, "--", ".", *pathspecs]
    numstat = _git(root, *args) or ""
    status = _git(root, "diff", "--name-status", rng, "--", ".", *pathspecs) or ""
    adds = dels = 0
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        a, d = cols[0], cols[1]
        adds += int(a) if a.isdigit() else 0
        dels += int(d) if d.isdigit() else 0
    added = edited = deleted = 0
    for line in status.splitlines():
        code = line.split("\t", 1)[0][:1]
        if code == "A":
            added += 1
        elif code == "D":
            deleted += 1
        elif code:
            # R (renamed) and C (copied) land here with M. A rename is a file edited from
            # the reviewer's side of the desk, not one added and one removed.
            edited += 1
    return added, edited, deleted, adds, dels


def _compare_href(pr: dict | None) -> str:
    """`<repo>/compare/<base>...<branch>` — the diff the diffstat is a count of.

    Built from the two refs the page already names rather than from the shas it
    measured: a sha pair is only a URL once the branch has been pushed, and the number
    in the chip is read off a working tree that may be a commit ahead of the remote. Two
    branch names are the comparison github.com keeps current by itself — the same pair
    the ref chips beside it link to, one page further in.

    `origin/` is stripped: it names a remote in *this* checkout, and github.com has
    never heard of it. Empty when anything is missing, and the chip stays an inert pill
    rather than linking somewhere that 404s."""
    pr = pr or {}
    repo = (pr.get("repo") or "").rstrip("/")
    base = (pr.get("base") or "").removeprefix("origin/")
    branch = pr.get("branch") or ""
    if not (repo and base and branch):
        return ""
    return (f"{repo}/compare/{urllib.parse.quote(base)}..."
            f"{urllib.parse.quote(branch)}")


def diffstat_chips(root: Path, state: dict | None, extra: list[str] | None,
                   pr: dict | None = None) -> list[dict]:
    """`{"auto": "diffstat"}` -- how much there is to read, measured rather than typed.

    The fourth chip to be taken away from the author, and the one with the clearest reason
    to be. `files` and `lines` outlived the `autofixed`, `cost` and `tests` chips being
    computed because they *look* like facts: a number with a sign in front of it reads as
    something a tool produced. On the branch this was written for, the page had said
    `files +1 / ~40` and `lines +1198 / -863` for six days. The file count was roughly
    right. The line counts matched no range in the repository at all -- not the branch
    against its base (+2896 / -333), not against the merge-base recorded in the same
    content file (+5089 / -4378), not against the stale local main (+4347 / -4099). They
    had been typed once, from a branch state three commits and one `git reset` ago, and
    nothing was ever going to catch them, because nothing was looking.

    Two chips out of one measurement, so the file count and the line count can never
    describe different ranges -- which is its own class of drift, and the one a reader is
    least equipped to notice.

    Returns [] when the base will not resolve: no base, no comparison, no chip. A page that
    cannot say what it measured against must not print a number as though it could.
    """
    if not state or not state.get("mergeBase"):
        return []
    # `A...B` and `A..B` differ only when the base has moved ahead, and that is precisely
    # the case the `!` on the ref chip is about. Three dots is the pull request's own
    # reading -- what this branch did, not what has happened since it forked -- so the two
    # marks stay independent: the numbers describe the branch, the warning describes the
    # gap.
    rng = f"{state['mergeBase']}...{state['head']}"
    excludes = [f":(exclude){p}" for p in GENERATED_PATHSPECS + list(extra or [])]
    a, e, d, adds, dels = _numstat(root, rng, excludes)
    fa, fe, fd, fadds, fdels = _numstat(root, rng, [])
    hidden = (fa + fe + fd) - (a + e + d)

    where = f"vs {state['ref']}"
    # The signs are the page's, not this chip's: `+` added, `-` removed, a pencil for
    # changed, and a zero is dropped rather than printed. A row of chips is read as a row
    # of signed numbers, and `-0` is noise that costs a glance to dismiss.
    files_value = " / ".join(piece for piece in (
        f'<span class="added">+{a}</span>' if a else "",
        f'<span class="removed">−{d}</span>' if d else "",
        f'<span class="changed">{PENCIL}{e}</span>' if e else "",
    ) if piece) or "none"
    lines_value = " / ".join(piece for piece in (
        f'<span class="added">+{adds}</span>' if adds else "",
        f'<span class="removed">−{dels}</span>' if dels else "",
    ) if piece) or "none"

    # The unfiltered totals stay in the hover; the prose explaining them does not. A
    # filtered number with no way to see what was filtered leaves the reader taking the
    # exclusion on trust, which is the position the typed chip left them in -- so the
    # guarantee is that the generated files are *ranked below* the code, never hidden from
    # it. What went is the argument for that: a redrawn diagram is not a line written, and
    # everything here was measured with `git diff` at build time rather than typed. Both
    # true, neither actionable, and a tooltip is read standing up in one glance. Whoever
    # needs the reasoning is reading this function.
    if hidden:
        skipped = (f" {hidden} generated left out; with them {fa + fe + fd} files, "
                   f"+{fadds} / −{fdels}.")
    else:
        skipped = " No generated files to leave out."

    # The line count is the one number on the bar a reader wants to *open*: "+921 / −68"
    # is the size of what there is to read, and the next question is always what those
    # lines are. So it carries the compare page, and the file count beside it stays an
    # inert pill -- two identical-looking links to the same page is a row that teaches the
    # reader to ignore half of it.
    href = _compare_href(pr)
    lines_tip = f"+{adds} / −{dels} {where}.{skipped}"
    if href:
        lines_tip += " Opens the whole diff on github.com."
    return [
        {"label": "files",
         "value": files_value,
         "tip": f"{a} added, {e} edited, {d} deleted {where}.{skipped}"},
        {"label": "lines",
         "value": lines_value,
         "tip": lines_tip,
         **({"href": href} if href else {})},
    ]


def tab_cost_report(root: Path, tab_ids: list[str]) -> dict | None:
    """What each tab cost, asked of the run itself — same discipline as `cost_chip`.

    Unlike `cost_chip`, this does not go quiet on a bad day: no session, no transcript, no
    step ledger, a step that never stamped — every one of those comes back as *data*
    (`report["tabs"][id]["tip"]` says so in words), because a tab whose cost silently has
    no tooltip reads exactly like a tab that measured zero. Only returns None when
    `review-cost.py` itself could not be asked at all.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file() or not tab_ids:
        return None
    proc = subprocess.run(
        [sys.executable, str(script), "--tab-costs", "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no per-tab cost report: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


#: Where the ledger is kept between builds. Dot-prefixed like every other private file
#: beside the page: `publish-demo.sh` publishes what does not begin with a dot, and a
#: measurement of one machine's transcripts is not something to ship in a demo zip.
COST_CACHE = ".cost-ledger.json"


def _cost_inputs(root: Path, out_dir: Path, tab_ids: list[str], base: str) -> str:
    """A fingerprint of everything the ledger is computed *from*.

    Not of the answer — of the inputs, so a hit means "nothing this number depends on has
    moved" rather than "somebody said it was fine". The pieces:

      * the session id, which is whose transcripts are read;
      * that session's `.jsonl` and every `agent-*.jsonl` beside it, by size and mtime —
        a conversation that ran another turn is a different bill;
      * `.steps.json`, which is how the ledger splits the run across tabs;
      * the base ref and the tab list, which are what was asked;
      * `review-cost.py` itself, so a change to the pricing invalidates every cache on
        this machine rather than being invisible until somebody deletes a file.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{base}\0{','.join(tab_ids)}\0".encode())
    script = Path(__file__).resolve().parent / "review-cost.py"
    for f in (script, out_dir / ".steps.json", out_dir / ".session"):
        try:
            st = f.stat()
            h.update(f"{f.name}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
        except OSError:
            h.update(b"\0gone\0")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not sid:
        try:
            sid = (out_dir / ".session").read_text(encoding="utf-8").strip()
        except OSError:
            sid = ""
    h.update(f"{sid}\0".encode())
    if sid:
        projects = Path(os.path.expanduser("~/.claude/projects"))
        # The session's own transcript and its subagents'. Sorted, because a set of paths
        # in filesystem order is a fingerprint that changes for no reason.
        for f in sorted(list(projects.glob(f"*/{sid}.jsonl"))
                        + list(projects.glob(f"*/{sid}/subagents/agent-*.jsonl"))):
            try:
                st = f.stat()
                h.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
            except OSError:
                h.update(b"\0gone\0")
    return h.hexdigest()


def cost_ledger_report(root: Path, tab_ids: list[str], base: str,
                       out_dir: Path | None = None) -> dict | None:
    """The whole bill — writing the code, the passes, every tab, the residual.

    Same discipline as `tab_cost_report`, which it supersedes: every failure comes back as
    data with a sentence explaining it, never as a silently missing number. It returns None
    only when `review-cost.py` could not be asked at all.

    **Cached, on the inputs.** This one call was 40 seconds of a 47-second build — it reads
    every turn of a conversation that wrote a feature over two days, and it does it again on
    every rebuild of a page whose bill has not moved since. That is most of what a reader
    waits through after pressing a button on the page: re-rendering a diagram takes three
    seconds and then they sit for forty, watching nothing, in front of a control they
    pressed. Re-deriving a number from transcripts nobody has appended to is not a
    measurement, it is the same measurement, and `_cost_inputs` is what says so.

    A cache that could be *wrong* would be much worse than a slow build — the cost tab is
    the one part of this page nothing else corroborates — so the key is the inputs and never
    a timestamp: the session id, the byte length and mtime of its transcript and every
    subagent's, `.steps.json`, the base, the tab list, and `review-cost.py` itself. Anything
    moves and the answer is recomputed. Nothing moves and the answer cannot have.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file():
        return None
    cache = (out_dir / COST_CACHE) if out_dir else None
    key = _cost_inputs(root, out_dir, tab_ids, base) if out_dir else ""
    if cache:
        try:
            held = json.loads(cache.read_text(encoding="utf-8"))
            if held.get("key") == key and "ledger" in held:
                return held["ledger"]
        except (OSError, ValueError):
            pass
    proc = subprocess.run(
        [sys.executable, str(script), "--ledger", "--base", base,
         "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost ledger: {line}", file=sys.stderr)
        return None
    try:
        ledger = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if cache:
        # Best effort: a read-only directory is a slow build, not a failed one.
        try:
            cache.write_text(json.dumps({"key": key, "ledger": ledger}), encoding="utf-8")
        except OSError:
            pass
    return ledger


# The unattributed cost, in the order a reader wants it: the one part that has a real name
# first, then the two that are honestly leftovers. Keys come from `review-cost.py`'s
# `tab_costs`; a part with no turns in it is not rendered at all.
RESIDUAL_ROWS = [
    ("guide", "assembling the guide itself — Step 9 writes every tab&rsquo;s prose in one pass"),
    ("subagent", "subagent work that fell outside every step&rsquo;s window"),
    ("conversation", "the orchestrating conversation — reading, deciding, recovering"),
]


def _cost_money(c: float) -> str:
    """`review-cost.py`'s own `money()`, with one difference that matters in a table: a
    measured zero prints as `$0.00`, not as `<$0.01`. In a tooltip the two read the same;
    in a column of numbers, "less than a cent" claims a script-generated tab spent
    something, which is the one thing the zero rows are there to deny."""
    if c <= 0:
        return "$0.00"
    return f"${c:,.2f}" if c >= 0.01 else "<$0.01"


def _cost_tokens(n: float) -> str:
    n = int(round(n))
    # A conversation that wrote a feature over two days runs to ten figures, and `1044.6M`
    # is four digits the reader has to convert before the column means anything.
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)



COST_TAB_ID = "cost"

# The groups the ledger reports, in the order the money was spent: somebody wrote it,
# somebody reviewed it, and then this page was assembled. Each is a caption row, not a
# separate table — the reader is comparing magnitudes across all three, and three tables
# means three column widths and no comparison.
PASS_ROWS = [
    ("finding", "the passes that read the diff"),
    ("fixing", "the passes that applied what they found"),
]

#: The phases `session-cost.py` dates, in the order the money was spent. They are a
#: *replacement* for the two groups above, not an addition: the same dollars, cut by what
#: the work was rather than by which program billed it. The reader's question is where the
#: money went in the work — was the review the expensive part, or was acting on it — and
#: `writing the code` + `reviewing it` cannot answer it, because taking the review's advice
#: falls in neither.
#:
#: The keys are `session-cost.py`'s, and the labels are too: a second set of names here
#: would let the table and the terminal disagree about what a row is. Order is fixed here
#: rather than trusted to the file, so a phase nobody could date still holds its place in
#: the sequence instead of vanishing from the middle of it.
PHASE_ROWS = ["implementation", "code_review", "post_review_fixes", "review_points",
              "video", "images", "page_build"]


def _when(raw: str | None) -> str:
    """`2026-09-02T15:41:21.4Z` as `2 Sep 15:41`. The date is there because the writing
    happened on a different day from the review and that is half the point of the row;
    the seconds are not, because nothing here is timed to the second."""
    if not raw:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{t.day} {t.strftime('%b')} {t:%H:%M}"


def phase_rows_html(phases: dict | None) -> str:
    """What each phase of the work cost, or why it could not be dated.

    This is the cut a reader actually arrives with, and nothing was in a position to make
    it until the commits started carrying trailers saying which commit was which. With
    `Implements:` and `Review-Points:` on the branch, `session-cost.py` can date the first
    edit, the implementation commit, the review's first and last turn and the review
    commit — and the four phases between them are the answer to "was the review the
    expensive part, or was acting on it".

    **A phase that cannot be dated prints its reason, never `$0.00`.** The two render
    identically to a reader and mean opposite things: one is a phase that cost nothing, the
    other is a phase whose cost is sitting in some other row of the same table.

    **Every window is printed**, for the reason the authoring row already prints its own: a
    window is a bound, not a fence. Work inside it that belonged to something else is
    counted, and a table that hid the width of the bound would imply a precision the
    transcript cannot support.
    """
    rows_by_key = {r.get("key"): r for r in (phases or {}).get("rows") or []
                   if isinstance(r, dict)}
    if not any(r.get("measured") for r in rows_by_key.values()):
        return ""
    out = []
    for key in PHASE_ROWS:
        r = rows_by_key.get(key)
        if not r:
            continue
        label = html.escape(str(r.get("label") or key))
        if not r.get("measured"):
            why = html.escape(str(r.get("reason") or "not measured"))
            out.append('<tr class="costquiet"><td><span class="costnote">'
                       f'{label} — {why}</span></td><td>—</td><td>—</td></tr>')
            continue
        window = r.get("window") or []
        sub = " &middot; ".join(x for x in (
            html.escape(str(r.get("detail") or "")),
            (f'{_when(window[0])} &rarr; {_when(window[1])}'
             if len(window) == 2 and _when(window[0]) else ""),
        ) if x)
        out.append(f'<tr><td>{label}<span class="costsub">{sub}</span></td>'
                   f'<td>{_cost_tokens(r.get("tokens") or 0)}</td>'
                   f'<td>{_cost_money(r.get("cost") or 0.0)}</td></tr>')
    # Anything the file dates that this table does not know the name of. Dropping it would
    # make the rows stop summing to the total, silently, the first time a phase is added.
    for key, r in rows_by_key.items():
        if key in PHASE_ROWS or not r.get("measured"):
            continue
        out.append(f'<tr><td>{html.escape(str(r.get("label") or key))}</td>'
                   f'<td>{_cost_tokens(r.get("tokens") or 0)}</td>'
                   f'<td>{_cost_money(r.get("cost") or 0.0)}</td></tr>')
    return "".join(out)


def cost_ledger_html(led: dict | None, tabs: list[dict]) -> str:
    """What this change set cost, from the first line written to this page being built.

    This was a chip in the scope bar with a breakdown hanging off it, and the chip
    answered the wrong question: *what did this page cost to make*. The question a reader
    arrives with is what the **change** cost, and producing the code is the larger half of
    it — on the branch this was built for, the conversation that wrote the feature cost
    nearly seven times the review that read it. A number that big is not a footnote on a
    bar of chips; it is its own tab, and it is the honest answer to "is this way of working
    worth it", which is the only reason anybody totals up an agent's bill at all.

    Every row is measured or says it is not. Three kinds of honesty the table has to keep:

      * **A window is not a fence.** The authoring row is costed between that conversation's
        first and last edit to these files. Work inside that window which belonged to
        something else is counted, and the row prints the window so the reader can see how
        wide it is rather than trusting a number that cannot be tightened.
      * **The passes are added once.** A review pass usually runs before the guide does, so
        its cost is outside the run's own total and is added; one fired mid-run is already
        inside it and is not. The ledger tells the two apart rather than assuming.
      * **A zero is not an absence.** A tab a script produced costs nothing to produce and
        says so in its own row; a tab nothing could measure says *that*, in words.
    """
    if not led:
        return ""
    # Nothing measured anywhere — no transcript for the run, and no conversation on disk
    # that wrote the code — is an absence, and the page carries no tab for it. A pill
    # reading `$0` is a claim that this change was free, which is the one thing the
    # absence does not mean. A run that measured EITHER half still gets the tab, with the
    # other half saying in words why it is missing.
    if not ((led.get("writing") or {}).get("measured")
            or (led.get("run") or {}).get("measured")):
        return ""
    rows = []
    # The phase cut, when the branch's trailers made it datable. It *replaces* the two
    # groups below rather than joining them: the same money, cut by what the work was, and
    # printing both cuts of one total in one table is how a reader ends up adding a number
    # to itself. The per-tab group stays either way — it answers a different question
    # (which part of this page cost what), and it is the only one of the three that is
    # about the page rather than about the change.
    phases = phase_rows_html(led.get("phases"))

    def row(label: str, tokens, cost, cls: str = "") -> None:
        tok = _cost_tokens(tokens) if tokens is not None else "—"
        money = _cost_money(cost) if cost is not None else "—"
        rows.append(f'<tr{f' class="{cls}"' if cls else ""}><td>{label}</td>'
                    f'<td>{tok}</td><td>{money}</td></tr>')

    def group(title: str) -> None:
        rows.append(f'<tr class="costgroup"><td colspan="3">{title}</td></tr>')

    # --- writing it ---------------------------------------------------------
    writing = led.get("writing") or {}
    if phases:
        group("phase by phase, from the first edit to this build")
        rows.append(phases)
    elif writing.get("measured"):
        group("writing the code")
        for sess in writing.get("sessions") or []:
            where = " &middot; ".join(x for x in (
                f'{sess["edits"]} edits across {sess["files"]} files' if sess.get("edits")
                else f'{sess["bash"]} shell writes across {sess["files"]} files',
                f'{_when(sess.get("first"))} &rarr; {_when(sess.get("last"))}',
                # Escaped, and `<synthetic>` dropped: it is what `review-cost.py` calls a
                # turn with no model on it, it costs nothing, and unescaped it was a tag
                # the browser swallowed along with the comma in front of it.
                ", ".join(html.escape(m) for m in (sess.get("models") or [])
                          if m and m != "<synthetic>"),
            ) if x)
            name = "this conversation" if sess.get("current") else \
                f'conversation <code>{html.escape(sess["session"][:8])}</code>'
            rows.append(
                f'<tr><td>{name}<span class="costsub">{where}</span></td>'
                f'<td>{_cost_tokens(sess.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(sess.get("cost") or 0.0)}</td></tr>')
        if writing.get("weak"):
            row('<span class="costnote">no conversation used the edit tools on these '
                "files — this is the strongest shell-only match, and may be the wrong "
                "one</span>", None, None, "costquiet")
    else:
        group("writing the code")
        why = writing.get("reason") or "not measured"
        row(f'<span class="costnote">{html.escape(str(why))}</span>', None, None, "costquiet")

    # --- reviewing it -------------------------------------------------------
    # Skipped entirely under the phase cut, and not as a tidy-up: `the passes that read the
    # diff` and `code-review agents` are the SAME dollars counted a second way, and two
    # cuts of one total under one `total` row is how a reader ends up adding a number to
    # itself. The finding/fixing split survives where phases could not be dated.
    passes = {} if phases else (led.get("passes") or {})
    groups = passes.get("groups") or {}
    if groups or passes.get("inline"):
        group("reviewing it")
    for key, title in PASS_ROWS:
        g = groups.get(key)
        if not g:
            continue
        invoked = ", ".join(f"<code>{html.escape(i)}</code>" for i in g.get("invoked") or [])
        inside = (g.get("cost") or 0.0) - (g.get("earlier") or 0.0)
        note = (" &middot; already inside the run below, so not added twice"
                if inside > 0.005 else "")
        rows.append(
            f'<tr><td>{title}<span class="costsub">{invoked}{note}</span></td>'
            f'<td>{_cost_tokens(g.get("tokens") or 0)}</td>'
            f'<td>{_cost_money(g.get("cost") or 0.0)}</td></tr>')
    if passes.get("inline"):
        n = passes["inline"]
        row(f'<span class="costnote">{n} pass{"es" if n != 1 else ""} ran in this '
            "conversation rather than forking, so there is no transcript of their own to "
            "price — their cost is in the rows below</span>", None, None, "costquiet")

    # --- building the guide -------------------------------------------------
    group("building this guide")
    rows.append(_cost_tab_rows(led.get("tabs") or {}, tabs))

    # The total of the rows on screen, which under the phase cut is not the ledger's own.
    # `led["total"]` adds the authoring conversation, the passes and the run — three
    # overlapping measurements of one bill, reconciled by the groups that are no longer
    # being drawn. Printing it under the phases would put a number in the footer that the
    # column above it does not add up to, and the reader has no way to tell which of the
    # two is the answer.
    phase_doc = led.get("phases") or {}
    if phases and phase_doc.get("cost") is not None:
        total, total_tokens = phase_doc.get("cost") or 0.0, phase_doc.get("tokens") or 0
    else:
        total, total_tokens = led.get("total") or 0.0, led.get("total_tokens") or 0
    foot = (f'<tr class="costtotal"><td>total</td>'
            f'<td>{_cost_tokens(total_tokens)}</td>'
            f'<td>{_cost_money(total)}</td></tr>')
    return (
        '<table class="costtab costledger">'
        '<caption>What this change cost to produce and to review, at list price — every '
        'turn priced from the transcripts that recorded it. Nobody on a subscription is '
        'billed this; it is what the same tokens would cost on the API.</caption>'
        '<thead><tr><th scope="col">where it went</th><th scope="col">tokens</th>'
        '<th scope="col">cost</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody><tfoot>{foot}</tfoot></table>'
    )


def _cost_tab_rows(costs: dict, tabs: list[dict]) -> str:
    """The per-tab half of the ledger.

    Three shapes of row, because there are three honest answers: a tab with measured spend
    gets its own, biggest first; every measured-zero tab collapses into one muted row that
    names them all (a script wrote that tab, so zero is true, but ten of those stacked
    above the rows carrying the money would bury the point); and every unmeasured tab
    collapses the same way carrying the reason in words, because "we could not measure
    this" must never render identically to a measured zero.
    """
    rows = costs.get("tabs") or {}
    entries = [(t.get("label") or t.get("id"), rows[t.get("id")])
               for t in tabs if rows.get(t.get("id"))]
    if not entries:
        why = costs.get("reason") or "no step ledger, so no turn could be placed in a tab"
        return ('<tr class="costquiet"><td><span class="costnote">'
                f'{html.escape(str(why))}</span></td><td>—</td><td>—</td></tr>')

    def spend(r):
        return r.get("cost") or 0.0

    def toks(r):
        return r.get("tokens") or 0

    measured = [e for e in entries if e[1].get("measured")]
    paid = sorted([e for e in measured if spend(e[1]) or toks(e[1])], key=lambda e: -spend(e[1]))
    free = [e for e in measured if not (spend(e[1]) or toks(e[1]))]
    unknown = [e for e in entries if not e[1].get("measured")]

    def names(items):
        return ", ".join(html.escape(str(l)) for l, _ in items)

    out = "".join(f'<tr><td>{html.escape(str(l))}</td><td>{_cost_tokens(toks(r))}</td>'
                  f'<td>{_cost_money(spend(r))}</td></tr>' for l, r in paid)
    if free:
        out += (f'<tr class="costquiet"><td>{len(free)} tab{"s" if len(free) != 1 else ""} '
                f'with no model spend — {names(free)}</td><td>0</td><td>$0.00</td></tr>')
    if unknown:
        why = costs.get("reason") or "no step in the ledger named them"
        out += (f'<tr class="costquiet"><td>{len(unknown)} tab'
                f'{"s" if len(unknown) != 1 else ""} not measured — '
                f'{html.escape(str(why))} ({names(unknown)})</td>'
                '<td>—</td><td>—</td></tr>')
    resid = costs.get("residual") or {}
    if resid.get("measured"):
        parts = costs.get("residual_parts") or {}
        shown = [(label, parts[key]) for key, label in RESIDUAL_ROWS
                 if (parts.get(key) or {}).get("messages")]
        if shown:
            out += "".join(
                f'<tr class="costquiet"><td>{label}</td>'
                f'<td>{_cost_tokens(part.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(part.get("cost") or 0.0)}</td></tr>'
                for label, part in shown)
        else:
            out += ("<tr class=\"costquiet\"><td>not one tab's — assembling the guide "
                    "itself, plus any step whose window did not cover it</td>"
                    f'<td>{_cost_tokens(resid.get("tokens") or 0)}</td>'
                    f'<td>{_cost_money(resid.get("cost") or 0.0)}</td></tr>')
    return out


def chip_face(c: dict) -> str:
    """A chip's own words: the label it was given and the value it measured. Shared by
    every renderer below so that a chip which moves house — the cost chip becoming half of
    the run chip — cannot pick up different markup on the way."""
    return f'{html.escape(c["label"])} <b>{c["value"]}</b>'


def chip_html(c: dict) -> str:
    """One resolved chip, as the scope bar renders it: a link when it has somewhere to
    send the reader, an inert pill otherwise."""
    inner = chip_face(c)
    if c.get("tip"):
        inner = f'<span data-tip="{html.escape(c["tip"])}">{inner}</span>'
    if c.get("href"):
        return (f'<a class="chip chip-link" href="{html.escape(c["href"])}"'
                f'{" target=_blank" if c["href"].startswith("http") else ""}>{inner}</a>')
    return f'<span class="chip">{inner}</span>'


def validate(spec: dict, out_dir: Path) -> list[str]:
    """Every problem in the content file, named, in one pass.

    A bare ``KeyError: 'png'`` from 300 lines further down tells the author nothing about
    which entry was wrong. ``resolve_refs`` already collects and names its failures; this is
    the same courtesy for the rest of the file, and it runs before any subprocess so a bad
    content file costs a second rather than a full page build."""
    problems = []
    for key, fields in REQUIRED.items():
        for i, item in enumerate(spec.get(key) or []):
            for f in fields:
                if isinstance(f, tuple):
                    if not any(item.get(alt) for alt in f):
                        problems.append(
                            f"{key}[{i}] has none of "
                            + ", ".join(repr(alt) for alt in f)
                            + " — an item has to say something past its title")
                    continue
                # An explicit empty title is a decision, not an omission: a section whose
                # content announces itself does not need a heading repeating the tab name
                # above it. A *missing* key is still the mistake it always was.
                if f == "title" and f in item and not item[f]:
                    continue
                if not item.get(f):
                    problems.append(f"{key}[{i}] is missing {f!r}")
    v = spec.get("verdict")
    if v is not None and "score" not in v:
        problems.append("verdict is missing 'score' (0-10, drives the pip scale)")
    city = spec.get("codecity")
    if city is not None:
        for f in ("png", "href"):
            if f not in city:
                problems.append(f"codecity is missing {f!r}")
        if city.get("png") and not (out_dir / city["png"]).is_file():
            problems.append(f"codecity.png -> {city['png']} does not exist — did step 4 run?")
    for i, s in enumerate(spec.get("sections") or []):
        inc = s.get("includeHtml")
        if inc and not (out_dir / inc).is_file():
            problems.append(f"sections[{i}] ({s.get('id')}) includeHtml -> {inc} "
                            "does not exist — did the step that produces it run?")
    # A requirement may name tests only when the page can say what happened to them. The
    # alternative — rendering every one as "unchanged" because no manifest was loaded —
    # would be the page inventing an answer, which is the one thing it must never do.
    tc = spec.get("testChanges")
    if tc and not (out_dir / tc).is_file():
        problems.append(f"testChanges -> {tc} does not exist — run scripts/test-changes.py first")
    pt = spec.get("playwrightTraces")
    if pt and not (out_dir / pt).is_file():
        problems.append(f"playwrightTraces -> {pt} does not exist — "
                        "run scripts/playwright-traces.py first")
    if any(b.get("type") == "traces" for t_ in spec.get("tabs") or []
           for b in t_.get("blocks") or []) and not pt:
        problems.append("a tab declares a 'traces' block, but no top-level "
                        "'playwrightTraces' manifest says which recordings to show")
    for i, s in enumerate(spec.get("sections") or []):
        for j, req in enumerate(s.get("requirements") or []):
            if not req.get("text"):
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] is missing 'text'")
            for k, t in enumerate(req.get("tests") or []):
                if not t.get("name"):
                    problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] "
                                    f"tests[{k}] is missing 'name'")
            if req.get("tests") and not tc:
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] names tests, "
                                "but no top-level 'testChanges' manifest says what the change "
                                "set did to them")
    for c in spec.get("extraCss") or []:
        if not (out_dir / c).is_file():
            problems.append(f"extraCss -> {c} does not exist "
                            "(the fragment's --css was never written)")
    ids = {s.get("id") for s in spec.get("sections") or []}
    for t_ in spec.get("tabs") or []:
        for b in t_.get("blocks") or []:
            if b.get("type") == "section" and b.get("id") not in ids:
                problems.append(f"tabs[{t_.get('id')}] references section {b.get('id')!r}, "
                                "which is not in 'sections'")
    return problems

HOME_URL = "https://github.com/victorrentea/human-review"

# Where a finished page lives once the projector is off. The `demo zip` workflow rebuilds
# a zip per snapshot under `demo/` on every push to main and clobbers it onto a rolling
# release under a fixed tag, so this address never moves and never goes stale — it is the
# one URL worth reading out to a room, and the one worth forwarding afterwards.
#
# The tag page rather than a particular `.zip`: which snapshot a reader wants is theirs to
# pick, the release notes there name the commit the assets actually stand on, and a link
# that starts a download the instant it is clicked is a poor thing to paste into a chat.
DEMO_ZIP_URL = "https://github.com/victorrentea/human-review/releases/tag/demo"

# The other way to keep this page, and the better one for anybody who is going to read it
# rather than skim it: `.github/workflows/demo-image.yml` bakes every snapshot under
# `demo/` into a container and pushes it to this repository's own registry, so
# `docker run --rm -p 8642:80 ghcr.io/victorrentea/human-review:<slug>` stands the report
# up at an address on the reader's own machine.
#
# It is worth a second link beside the zip because *served is not the same page as
# unzipped*. Off disk a review cannot reliably fetch its own content, every request it
# makes is cross-origin, and the recordings open in another application instead of in the
# page. The container is the only copy a stranger can be handed that behaves exactly like
# the one being demoed to them.
#
# The package page and not a `docker pull` line: the tags are listed there with what each
# one holds, and a footer is a place to send somebody, not a place to print a command they
# cannot run from a browser.
DEMO_DOCKER_URL = ("https://github.com/victorrentea/human-review/"
                   "pkgs/container/human-review")

# The footer's own line is where the offer goes. A reader still reading has no use for it;
# a reader who has reached the bottom is precisely the one who wants to keep a copy — and
# the page they are looking at is usually on somebody else's screen, so "keep a copy" is
# the only thing they can act on at all.
#
# A sibling of the footer sentence, not a clause inside it: that sentence belongs to the
# content file and an author may write anything there or nothing, while this offer is the
# build's and is owed to every page it produces.
#
# Two links and almost no prose between them. It read `Download here a standalone demo
# zip.`, where the verb was the link and the noun trailed after it — so the reader's eye
# landed on *Download here*, which says nothing about what arrives, and had to read on to
# find out. The links are the nouns now (`Download zip`, `a runnable docker of this
# report`), which is what a reader at the foot of a page is scanning for: a thing to take,
# not a sentence about taking it. What each one actually is stays in the hover.
TAKEAWAY = (
    '<span class="takeaway">'
    f'<a href="{DEMO_ZIP_URL}" target="_blank" rel="noopener" '
    'data-tip="Sample review pages on GitHub, one zip each. Unzip it and open '
    'review.html — no install, no server.">Download zip</a> · or '
    f'<a href="{DEMO_DOCKER_URL}" target="_blank" rel="noopener" '
    'data-tip="The same pages as a container: docker run --rm -p 8642:80 '
    'ghcr.io/victorrentea/human-review:&lt;snapshot&gt; — served rather than off disk, '
    'so the page behaves the way it does here.">a runnable docker of this report</a>.'
    '</span>'
)


# The footer used to end on a sentence about the page's own honesty — "every snippet is cut
# from the working tree at build time; every number on this page was measured by the step
# that produced it". It is true, and it is the build's job to *be* true rather than to say
# so; a reviewer opening a review does not want a paragraph of methodology before they have
# seen a line of code. Stripped here rather than only in the writing guidance, because
# content files outlive the instructions that produced them.
FOOTER_BOILERPLATE = re.compile(
    r"\s*(?:Every snippet is cut from the working tree at build time;?\s*"
    r"every number on this page was measured by the step that produced it\.?"
    r"|Snippets were cut from the working tree at build time by\s*"
    r"<code>scripts/extract-snippet\.py</code>;?\s*"
    r"every\s*<code>path:line</code>\s*link opens VS Code at that line\.?)",
    re.I,
)


# "against the running stack" says how the page was built. That was worth saying while the
# alternative was a report written from memory; it is now what the build always does, and a
# reader who cannot run the thing has no use for the distinction. Out, and the room it
# leaves goes to the one sentence a stranger holding this page can act on.
RUNNING_STACK = re.compile(r"\s+against the running stack", re.I)

# Nothing. The footer line is the address the page came from and the date it was built,
# and that is all it is for.
#
# It carried a sentence for a while — "Tell your agent to adapt this to your environment",
# after "Fork, Clone and Port with your Agent" before that — on the reasoning that a GitHub
# link in a footer reads as provenance and gets skipped, so it should be told what to do
# with the address. Both readings are right and the conclusion was not: the two links
# beside it already *are* the things to do, and an instruction sitting between the
# provenance and the offer was a third voice in a line that has room for two. The empty
# string is load-bearing — `_link_home` appends this, so emptying it empties the sentence
# on every page rebuilt from here, including ones already published.
INVITATION = ""

#: The ones that shipped, so a footer written against any of them comes out clean.
PAST_INVITATIONS = re.compile(
    r"\s*(?:Tell your agent to adapt this to your environment\.?"
    r"|Fork,? Clone and Port with your Agent\.?)", re.I)


def _link_home(footer: str) -> str:
    """Turn the footer's `/human-review` into the repository it names.

    The slash-command spelling only means something to a reader who already has the skill
    installed; everyone else is holding a page produced by a tool they cannot find. So the
    mention is *replaced* by the URL, not merely linked — the address is the useful thing,
    and it survives the page being printed, pasted or mailed on.

    Only the first occurrence, and never one already inside an <a>.

    The URL alone says where the page came from but not what a reader is meant to do with
    it, and a GitHub link in a footer is read as provenance and skipped. So the address
    is followed by the invitation: this page is a thing you take and re-point at your own
    code, not a credit you note. Appended here rather than asked of every author, for the
    same reason the boilerplate is stripped here.
    """
    footer = RUNNING_STACK.sub("", FOOTER_BOILERPLATE.sub("", footer or "")).strip()
    if not footer or "/human-review" not in footer or 'human-review"' in footer:
        return footer
    linked = footer.replace(
        "/human-review",
        f'<a href="{HOME_URL}" target="_blank" rel="noopener">{HOME_URL}</a>', 1)
    if not INVITATION:
        # And strip it where a content file (or an older build's output re-used as one)
        # still carries it: the sentence is gone from the template, and a page that
        # rebuilt itself and kept it would be the one place it survives.
        return PAST_INVITATIONS.sub("", linked).strip()
    return f"{linked} {INVITATION}" if INVITATION not in linked else linked


def page_title(spec: dict) -> str:
    """The one line that says which change this is.

    A content file’s own `title` is a sentence about the change ("Attending vet on a
    visit"). The reviewer, though, is looking at a pull request, and the name that
    matches what is in their tabs, their notifications and their `gh pr` output is
    `GH#37 <the PR’s own title>`. So when the content file names a PR, that wins, and
    the number is the link to it. With no `pr` block nothing changes.

    `GH#37` is the only link on this page a reader cannot recognise as one by where it
    sits: it is the first word of the `<h1>`, so it wears the page's heading weight, not
    a link's. The hover says where it goes — the one thing a reader wants before clicking
    away from the review they just opened.
    """
    pr = spec.get("pr") or {}
    if pr.get("number") and pr.get("title"):
        num = f'GH#{html.escape(str(pr["number"]))}'
        if pr.get("url"):
            num = (f'<a class="prref" href="{html.escape(pr["url"])}" '
                   f'data-tip="Open #{html.escape(str(pr["number"]), quote=True)} '
                   f'on GitHub">{num}</a>')
        return f'{num} {html.escape(pr["title"])}'
    return html.escape(spec.get("title", "Review guide"))


def ref_badges(spec: dict, state: dict | None = None) -> str:
    """`branch test-pr` `base main` — the two refs every number on this page is a
    comparison of, each one click from its own page on GitHub.

    They lead the scope bar rather than trailing the title, and both moves are the same
    decision. "Against what, again?" is a question asked halfway down the ninth tab, so
    the answer has to be somewhere the masthead still shows — and the row it belongs in
    is the one that already answers *how much*: `files`, `lines`, what the review found,
    what it cost. Two refs and six numbers about them are one thought, and the title row
    is then free to be a title.

    They are chips, not parenthesised asides, because in that row `(test-pr)` beside
    `files +1 / ✍️40` reads as an unlabelled number. The label is what makes the pair
    legible in one pass, and it costs four characters.

    The base chip carries a `!` when the two refs have drifted apart — the base has moved
    ahead of the fork point, or the local branch named here is behind the remote actually
    measured. It is on *this* chip and not in a banner because the question it answers is
    "compared against what, exactly?", which is the question the chip already exists to
    answer; a page-wide warning would be read once and dismissed, while a mark on the ref
    is there every time a reader comes back to check the pair. It is computed on every
    build from the refs as they stand, so it clears itself the moment main is merged in —
    there is no state to reset and nothing to remember.
    """
    pr = spec.get("pr") or {}
    repo = (pr.get("repo") or "").rstrip("/")
    warning = base_warning(state)
    out = []
    for key, label, cls, why in (("branch", "branch", "head", "the branch under review"),
                                 ("base", "base", "base", "the base it is compared against")):
        ref = pr.get(key)
        if not ref:
            continue
        inner = (f'{label} <b class="refname {cls}">{html.escape(ref)}</b>')
        # The chip is a link, so the hover's job is to say where the click goes — not to
        # re-describe a ref whose name is already the thing being read. A chip that does
        # not link anywhere gets no bubble at all rather than a sentence about itself.
        tip = "Open in GitHub" if repo else ""
        cls_extra = ""
        if key == "base" and warning:
            # The mark carries its own tooltip rather than extending the chip's: the chip
            # says what the ref is, the mark says what is wrong with it, and a reader who
            # hovers the `!` is asking the second question, not the first.
            inner += (f'<span class="drift" role="img" aria-label="stale base" '
                      f'data-tip="{html.escape(warning)}">!</span>')
            cls_extra = " drifted"
        tip = html.escape(tip)
        if repo:
            href = html.escape(f"{repo}/tree/{urllib.parse.quote(ref)}")
            out.append(f'<a class="chip chip-link refchip{cls_extra}" href="{href}" '
                       f'data-tip="{tip}">{inner}</a>')
        else:
            out.append(f'<span class="chip refchip{cls_extra}">{inner}</span>')
    return "".join(out)


# There is no `verdict_band_html` any more, and that is the point of this note: the band
# it built — full-bleed amber, the score at 3.4rem, a ten-pip dial, the bullets beside it —
# said the masthead's pill again a screenful lower and spent the first screenful of a review
# on a conclusion, so the list of findings the reader came for started below the fold. The
# `verdict` block in the content file is still read: its `score` is the pill's number and
# its band its colour. Its `bullets` are kept in the file and rendered nowhere; if the page
# ever needs the reasons stated in prose again, that is what `summary` is for.


def _score_target(spec) -> tuple[str, str]:
    """`("review", "Review")` — the tab the verdict's reasons live in, for the score to
    link to, and the name to say in the hover.

    Found by what a tab renders, not by its id: `findings` is the block that holds the
    calls behind a score, wherever the content file puts it. The first tab is the fallback
    — it is the panel the page opens on, so a score linking there at worst goes where the
    reader already was. The emoji a label may lead with is dropped from the hover: `Open
    the 🤖 Review tab` reads as a glyph the sentence has to step over, and the pill in the
    strip is recognisable by its word.
    """
    tabs = spec.get("tabs") or []
    for tab in tabs:
        if any(b.get("type") == "findings" for b in tab.get("blocks") or []):
            return tab.get("id", ""), (tab.get("label") or tab.get("id", "")).lstrip("🤖 ")
    if tabs:
        return tabs[0].get("id", ""), (tabs[0].get("label") or "").lstrip("🤖 ")
    return "", ""


def masthead_html(spec: dict, title_score: str, chips: str, strip_html: str,
                  base_st: dict | None = None) -> str:
    """Title, refs, note, scope chips and the tab strip — as one block that stays put.

    They used to be four bands that scrolled away, leaving the strip pinned alone over
    the text. Every one of them answers a question a reader has *while* reading a tab,
    so they travel together, and the title sits against the top edge rather than behind
    a gutter that would then be pinned there for the whole read.

    Only a tabbed page gets one: without a strip there is nothing to pin the masthead
    for, and the plain single-column guide keeps the heading it always had.
    """
    heading = f'<h1>{page_title(spec)}</h1>'
    if spec.get("pr"):
        # One line, and only the two things a reader navigates by: which change this is,
        # and how it scored. The refs moved down to the scope bar (`ref_badges`), and the
        # sentence describing the change is gone from here entirely — it is the first
        # thing the summary says, and a block that never scrolls cannot spend its width
        # on a sentence that is read once. `subtitle` is still in the content file and
        # still renders on a page with no `pr` block.
        rows = [f'<div class="titlerow oneline">{heading}'
                f'<span class="titleside">{title_score}</span></div>']
        chips = ref_badges(spec, base_st) + chips
    else:
        rows = [f'<div class="titlerow">{heading}'
                f'<span class="titleside">{title_score}</span></div>',
                f'<p class="sub">{spec.get("subtitle", "")}</p>']
    rows.append(f'<div class="scopebar">{chips}</div>')
    if not strip_html:
        return "\n".join(rows)
    return '<header class="masthead">\n' + "\n".join(rows + [strip_html]) + "\n</header>"


def rebuild_interpreter() -> str:
    """How to say "python, with Pygments" on *this* machine, in a command a reader pastes.

    Not `sys.executable`. Under `uv run --with pygments` that is a path inside a build
    directory uv deletes on exit, so the one command guaranteed to have worked is also the
    one guaranteed not to work again — and a command that fails when pasted is worse than
    no command, because it is tried first and believed second.

    So: plain `python3` when it can already import Pygments, which is the legible answer
    and the portable one; `uv run --with pygments python` when it cannot and uv is here,
    which installs nothing permanently and is ~100ms warm; and only then the interpreter
    running this build, which at least names something real.
    """
    # The probe has to ask what `python3` means *in the reader's terminal*, not in this
    # process. Under `uv run` the front of PATH is two throwaway directories — the
    # interpreter's own, and the one holding the packages `--with` installed — and both
    # answer to `python3` with Pygments importable, so a naive probe cheerfully reports
    # that `python3` works about interpreters that are deleted on exit. Dropping every
    # ephemeral directory is what asks the right question; if nothing outside them answers
    # to `python3`, that is an answer too, and the uv line below is the honest one.
    env = dict(os.environ)
    throwaway = [str(Path(sys.prefix)), env.get("VIRTUAL_ENV") or "",
                 os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")]
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", "").split(os.pathsep)
        if d and not any(t and (d == t or d.startswith(t + os.sep)) for t in throwaway))
    env.pop("VIRTUAL_ENV", None)
    outside = shutil.which("python3", path=env["PATH"])
    if outside and subprocess.run([outside, "-c", "import pygments"],
                                  capture_output=True, env=env).returncode == 0:
        return "python3"
    if shutil.which("uv", path=env["PATH"]) or shutil.which("uv"):
        return "uv run --with pygments python"
    return shlex.quote(sys.executable)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("content", help="JSON content file")
    ap.add_argument("--out", required=True, help="where to write the HTML")
    # The one thing in a build that is not a program: the Logging tab asks a model whether
    # a logged value is a privacy problem. Cached by a hash of what was sent, so a rebuild
    # of unchanged code neither re-asks nor re-pays — but a *new* statement would, and a
    # refresh that quietly buys an answer is exactly the thing `refresh-report.py` exists
    # to keep separate from the half a human asked for.
    ap.add_argument("--no-model", action="store_true",
                    help="use only cached privacy verdicts; render the rest as "
                         "not evaluated instead of calling the model")
    args = ap.parse_args(argv)

    global OFFLINE
    OFFLINE = args.no_model

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    spec = json.loads(Path(args.content).read_text(encoding="utf-8"))
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Emptied here rather than trusted to be empty: the register is module state, and the
    # module is imported and driven directly by the test suite, where two builds in one
    # process would otherwise leave the second one declaring the first one's actions.
    ACTIONS.clear()
    # How to start this build again — the last stage of every command offered under a
    # hand-drawn diagram.
    #
    # `--no-model` always, whatever this build was run with. A reader pressing *Update the
    # report* under a picture is asking for the picture to be picked up; they are not asking
    # to buy a privacy verdict for a logging statement, and a click that can spend money is
    # the one thing `refresh-report.py` goes out of its way to make impossible. It is also
    # most of why the command is quick: cached verdicts still render, and an uncached one
    # says *not evaluated* rather than going and asking.
    rebuild_cmd = " ".join([rebuild_interpreter(), shlex.quote(str(Path(__file__).resolve())),
                            shlex.quote(args.content), "--out", shlex.quote(args.out),
                            "--no-model"])

    # Before `validate`, and before anything walks the piles: the three arrays may be a
    # delegation (`{"auto": "review-points"}`) rather than a list, and everything
    # downstream — the validator, the ref resolution, the ledes, the renderers — takes
    # them as lists. This is the point at which the branch's own record becomes the page's.
    resolve_review_points(spec, out_dir)

    problems = validate(spec, out_dir)
    if problems:
        print(f"[review] {Path(args.content).name} cannot be rendered:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # The base every `diffs` entry is measured against when the entry does not name its
    # own: the rev the ledger recorded before the review pass touched anything. That is the
    # only left side that shows a fix on its own, and step 1 exists to record it.
    default_diff_base = review_step_rev(out_dir)
    for f in spec.get("findings", []) + spec.get("assumptions", []) + spec.get("autofixes", []):
        f["_refs"] = resolve_refs(f.get("refs", []), root)
        f["_snippets"] = "".join(
            snippet_html(s["ref"], s.get("caption"), root) for s in f.get("snippets", [])
        )
        # An applied fix that shows no diff is a claim with nothing behind it, so the build
        # says so — loudly enough to fix, quietly enough not to block a page whose ledger
        # has no rev (an older run, or a review re-rendered from a fresh clone).
        diffs = f.get("diffs", [])
        if diffs and not all(d.get("base") for d in diffs) and not default_diff_base:
            print("[review] WARNING: %r asks for a diff with no base, and the ledger's "
                  "review step recorded no rev \u2014 that diff is dropped"
                  % f.get("title", "")[:60], file=sys.stderr)
        f["_diffs"] = "".join(
            diff_html(d["path"], d.get("base") or default_diff_base, root, d.get("caption"),
                      d.get("head"))
            for d in diffs
            if d.get("base") or default_diff_base
        )

    # An assumption with no code under it is the one item on this page that cannot be
    # checked at all. A finding without a snippet is at least a claim about a defect a
    # reader can go and look for; "I assumed the tenant is always the caller's" points at
    # nothing, and a model asked at the end of a long session what it was unsure about will
    # produce fluent sentences of exactly that shape whether or not it ever hesitated. The
    # anchor is what separates a recollection from a guess about a recollection, so an
    # unanchored one is dropped rather than printed with a shrug.
    floating = [a for a in spec.get("assumptions", [])
                if not (a.get("_snippets") or a.get("_diffs") or a.get("_refs"))]
    for a in floating:
        print("[review] WARNING: assumption %r names no code — dropped. Give it a "
              "'snippets' entry (or 'refs'/'diffs') pointing at the line the decision "
              "landed on; an assumption a reader cannot go and look at is indistinguishable "
              "from one that was never made." % (a.get("title", "")[:60]), file=sys.stderr)
    if floating:
        spec["assumptions"] = [a for a in spec["assumptions"] if a not in floating]

    # A page that never declares the block says nothing at all about the third pile, and
    # nothing reads as "there was nothing to say". The pile is the one part of this page no
    # pass can reconstruct afterwards, so its absence has to be noisy at build time rather
    # than silent on the page — declare it with its mode (`authoring-sessions.py` says
    # which) and the lede prints the count, zero included.
    if _assumptions_block(spec) is None:
        print("[review] WARNING: no 'assumptions' block in any tab — the page will say "
              "nothing about what the agent that wrote the code had to guess at, which a "
              "reader cannot tell from it having guessed at nothing. Declare the block "
              "with its mode (authoring-sessions.py --base ... says A, B or C) even when "
              "the pile is empty.", file=sys.stderr)

    # Every panel is `id="<tab id>"`, so a section that happens to share a tab's id puts the
    # same id on two elements — `id="api"` on the API contract panel and on the <h2> inside
    # it, which is what this page shipped for months. Nothing looked broken, because
    # getElementById returns the first match and the first match is the panel, which is
    # where `#api` should land anyway. It is still invalid HTML and still a trap for the
    # next person. The panel's id is not negotiable (the strip's aria-controls points at
    # it), so the duplicate is resolved on the heading, which loses nothing.
    tab_ids = {t.get("id") for t in (spec.get("tabs") or [])}

    # What the change set did to each test, computed by `test-changes.py` from the diff
    # itself. Loaded once: the content file only says which requirement a test belongs to.
    test_doc = (json.loads((out_dir / spec["testChanges"]).read_text(encoding="utf-8"))
                if spec.get("testChanges") else {})
    # The recordings the same run left behind, harvested by `playwright-traces.py`. Loaded
    # the same way and for the same reason: what happened in a test is measured, never
    # written down by hand.
    traces_doc = (json.loads((out_dir / spec["playwrightTraces"]).read_text(encoding="utf-8"))
                  if spec.get("playwrightTraces") else {})
    tests_idx = test_index(test_doc.get("tests", []))

    sections, by_id, unchanged_ids = [], {}, {}
    for s in spec.get("sections", []):
        unchanged_ids[s["id"]] = bool(s.get("unchanged"))
        snips = "".join(
            snippet_html(x["ref"], x.get("caption"), root) for x in s.get("snippets", [])
        )
        # An include is a fragment another generator produced (e.g. the complexity delta):
        # rendered by whoever owns that data, pasted in here rather than re-derived.
        inc = ""
        if s.get("includeHtml"):
            inc = (out_dir / s["includeHtml"]).read_text(encoding="utf-8")
        # Usually the include is commentary on the prose, so it follows it. `includeFirst`
        # is for the one shape where it is the other way round: the fragment *is* what the
        # section is about — the Tests tab opens on the ticket the branch answers —
        # and the prose reads as the reply to it. Off by default: every other tab wants a
        # sentence of its own before a generated fragment lands.
        include_first = bool(s.get("includeFirst"))
        vid = ""
        if s.get("video"):
            vid = video_html(s, out_dir)
        body = shorten_dgm_src(
            expand_drawio(expand_snippets(s.get("body", ""), root), out_dir, root,
                          rebuild_cmd))
        collides = s["id"] in tab_ids
        if collides:
            print(f'[review] section {s["id"]!r} shares its id with a tab: the heading drops '
                  f'its id, so #{s["id"]} lands on the panel (which is where it was already '
                  "going). Rename the section to get an anchor of its own.", file=sys.stderr)
        h2_id = "" if collides else f' id="{html.escape(s["id"])}"'
        rendered = (
            # An empty title means the section speaks for itself; emit no heading rather
            # than an empty one, which would still take the vertical space of a heading.
            (f'<h2{h2_id}>{html.escape(s["title"])}</h2>\n' if s.get("title") else "")
            + (f"{inc}\n" if include_first and inc else "")
            # A section with no prose of its own — the video tab is one — must not open with
            # a blank line where the paragraph would have been.
            + (f"{body}\n" if body else "")
            # The requirement list sits between the prose and anything else the section
            # carries: it *is* the section's answer, and a snippet or an include is
            # commentary on it.
            + render_requirements(s.get("requirements") or [], tests_idx, root)
            + f'{"" if include_first else inc}{vid}{snips}{embed_html(s, out_dir)}'
        )
        sections.append(rendered)
        by_id[s["id"]] = rendered

    city = spec.get("codecity")
    city_html = ""
    if city:
        # A heading, and not the lede that used to be here. The two are different kinds of
        # sentence and only one of them earns the space: the lede described the picture
        # ("10 buildings lit — the classes this change set touched"), which a reader is
        # looking at; the heading names what the picture is *for*, which they are not. The
        # tab pill says "Code City", the name of the visualisation; this says what it is
        # being shown to them to answer, and the trailing ellipsis is deliberate — the
        # three named axes are the ones the panel inside the shot lets them switch between,
        # and they are not all of them.
        heading = city.get("title") or CITY_HEADING
        # No lede. The line it held was always some version of *"10 buildings lit — the
        # classes this change set touched, in a city of the whole backend"*, which is three
        # claims the reader can already see: the count is legible in the shot, the lit slice
        # is what lit means, and the city being the whole backend is what a city is. It was
        # also a **hand-typed number** in a file nothing revalidates — the exact thing this
        # skill's own writing rule forbids — so it went stale silently the first time a
        # class was added.
        #
        # The anchor sits on the heading, so `#codecity` still lands here.
        if city.get("body"):
            print("[review] codecity.body is no longer rendered — the picture starts under "
                  "the tab strip. Delete it from the content file; every sentence it can "
                  "hold is either in the shot or a number that goes stale.", file=sys.stderr)
        city_html = (
            f'<h2 id="codecity">{html.escape(heading)}</h2>\n'
            f'<a class="city" href="{html.escape(city["href"])}"'
            f' target="_blank" rel="noopener"'
            f' data-tip="Open the interactive Code City in a new tab">'
            f'<img src="{html.escape(city["png"])}" alt="Code City with the branch change set highlighted"></a>\n'
            # Only when there is one: the empty <p> still took a paragraph's margin under
            # the picture, on every page that never wrote a caption.
            + (f'<p class="sub">{city["caption"]}</p>' if city.get("caption") else "")
        )

    # Chips carry HTML on purpose: a chip is often a link (to the branch on GitHub, to a
    # section further down) or coloured (+added / -removed), and escaping would kill both.
    chips = []
    scope = spec.get("scope", [])
    # Where the base actually is, asked once: the diffstat chip measures against it and
    # the ref chip warns about it, and those two must never be talking about different
    # commits. `origin/main` is the default because it is what a pull request merges into;
    # a content file naming something else is taken at its word.
    base_st = base_state(root, (spec.get("pr") or {}).get("base") or "origin/main")

    # The cost of the run answers two chips now -- what it cost, and which model did the
    # reviewing -- and they can appear in either order in the content file, so the answer
    # is memoised rather than fetched where it happens to be needed first. A list, not a
    # variable, so `None` (a real answer: no session to ask) is distinguishable from
    # "not asked yet".
    cost_memo: list = []

    def resolved_cost() -> dict | None:
        if not cost_memo:
            cost_memo.append(cost_chip(root))
        return cost_memo[0]

    def emit(c: dict) -> None:
        """Render one resolved chip. Shared so that a chip which expands into several --
        `diffstat` becomes `files` and `lines` -- cannot pick up different markup than
        the ones written by hand beside it."""
        chips.append(chip_html(c))

    for c in scope:
        # The other chip that must never be typed. `{"auto": "autofixed"}` counts the two
        # lists this page actually renders — the open findings and the applied fixes — so
        # the chip and the LLM Review tab can never disagree with each other. The reason
        # it exists is that they already did: the hand-typed `/code-review 8 findings`
        # outlived the ninth finding being added, and nothing caught it, because nothing
        # was looking. `href` (and any label or tip) still comes from the content file.
        if c.get("auto") == "autofixed":
            # No record, no chip. `🤖 LLM review: 0 open, 0 auto-fixed` is the whole
            # failure this flow exists to end, in eleven characters: two measured-looking
            # zeros asserting a review that found nothing, where the truth is that nothing
            # says a review happened. Every other computed chip drops itself rather than
            # print a number it cannot stand behind; this one now does too.
            if (spec.get("_reviewPoints") or {}).get("missing"):
                continue
            fixed = len(spec.get("autofixes", []))
            total = len(spec.get("findings", [])) + fixed
            # Who reviewed is half of what this chip says, and it used to sit in a
            # second chip beside it (`reviewed by  Opus 5`) that nobody could check. The
            # run knows: `review-cost.py` returns the models it spent money on, most
            # expensive first. Two chips carrying one thought become one carrying it
            # fully -- `Opus 5 review  9 open · 3 autofixed` -- and the name is now as
            # measured as the numbers next to it. `by` in the content file is the fallback
            # for a page rebuilt outside the session that reviewed it; "LLM review" is the
            # last resort, and says exactly as much as it knows.
            paid = resolved_cost() or {}
            reviewer = next((m for m in paid.get("models") or [] if m and m != "synthetic"),
                            None) or c.get("by")
            computed = {
                # A colon, not a gap. The pill reads as one sentence — `🤖 Fable 5 review:
                # 6 open, 4 auto-fixed` — where before it was a label, a gap and a row of
                # numbers, which is the shape of a measurement rather than of a statement.
                # The robot is the page's own mark for "a model produced this", the same
                # one the inferred headings wear, and it is what makes the chip legible as
                # a claim by a machine rather than as another count of the diff.
                "label": f"\U0001f916{reviewer} review:" if reviewer else "\U0001f916LLM review:",
                # Both halves computed. The chip used to read `auto-fixed <n>`, and the
                # label did the lying the tooltip then had to walk back: only three of the
                # twelve were fixed, and a reader who never hovers was told all twelve
                # were. Neither number here can drift from the lists behind it.
                #
                # Open leads, and the total is gone from the face: `12 raised` is the sum
                # of the other two, so it is the one number on the chip nobody acts on,
                # while `3 autofixed` is the fact a reader cannot get anywhere else
                # without opening the tab. The order is the order of the work — what is
                # left to do first, what was already done for you second. The total is
                # still one hover away.
                # The applied half is greyed: it is on the page so the reader can check
                # it, not so they can act on it, and at full contrast it competes with the
                # number that IS the work. Grey is the page's own "already handled" —
                # the same treatment the fixes themselves get in the list below.
                # Read off `review-points.md`, the two halves swap round and change
                # name: the items are not open, they were declined, by the agent, with a
                # reason — and the fixed pile leads because it is what the agent did
                # rather than what it left. The same distinction the counts line in the
                # tab draws, in the same words, so a reader who compares the chip with
                # the line is not asked which of them to believe.
                "value": (f'{fixed} fixed, <span class="sub">{total - fixed} '
                          'declined</span>'
                          if (spec.get("_reviewPoints") or {}).get("missing") is False
                          else f'{total - fixed} open, '
                               f'<span class="sub">{fixed} auto-fixed</span>'),
                # The total, which the face no longer carries, split by the pass that
                # raised each item. `by /code-review and /simplify` named the two passes
                # and left the reader to guess the split — which is the only thing the
                # hover could add, since the chip is already about a number. It is counted
                # off each item's own `source`, so the breakdown cannot disagree with the
                # stamps in the list below it.
                #
                # What is not here any more: `running on <model>`. The chip's own face
                # reads `Opus 5 review` — a hover restating the word next to it is a hover
                # that taught the reader not to bother with the next one.
                "tip": _raised_by(spec.get("findings", []) + spec.get("autofixes", []),
                                  total),
            }
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}
        # A chip that has to be kept up to date by hand is a chip that will be wrong. The
        # cost of the run is the extreme case: it is still changing while the page is being
        # written, so it is computed here, at build time, and never typed into the content
        # file. `{"auto": "cost"}` is the whole declaration; label, value and tooltip all
        # come back from the script.
        # The same discipline for the test count: the page already parses every changed
        # test file to classify the rows under each requirement, so the number at the top
        # is read off that same manifest and cannot disagree with the list below it.
        # `files` and `lines` -- one measurement, two chips, so they can never end up
        # describing different ranges. `exclude` adds pathspecs to the generated-file list
        # this always applies; there is no way to turn that list off, because a diffstat
        # dominated by regenerated diagrams is not a stricter answer, it is a wrong one.
        if c.get("auto") == "diffstat":
            for computed in diffstat_chips(root, base_st, c.get("exclude"),
                                           spec.get("pr")):
                emit({**computed, **{k: v for k, v in c.items()
                                     if k not in ("auto", "exclude")}})
            continue

        if c.get("auto") == "tests":
            computed = tests_chip(test_doc)
            if computed is None:
                continue
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}

        if c.get("auto") == "cost":
            # Dropped, not rendered and not an error. The cost is the last tab on the strip
            # now; a chip in the bar could only ever carry the review's own share of it,
            # which turned out to be the smaller half of what the reader wanted. Content
            # files in the wild still ask for the chip, and silence is the right answer to
            # them: the number they wanted is on the page, one pill further right.
            continue
        emit(c)
    chips = "".join(chips)

    # This used to require a chip per automated pass — /code-review hunts bugs, /simplify
    # shrinks the solution, so one number over both reports neither. The page now carries a
    # single merged chip instead, by decision, and the old check fired on every run: a
    # warning that is always on is a warning nobody reads.
    #
    # The concern behind it survives in a form the merged chip can actually fail. One number
    # over two questions is honest only while the split it summarises is still visible. That
    # split used to be a second tab with a chapter per pass; it is now the `source` stamp on
    # each item, which is a better home for it — the reader sees who raised a finding beside
    # the finding, rather than in a parallel listing that can fall out of step with this one.
    # So two things are checked: that the chip's target exists, and that the stamps are
    # actually there. A merged count with nothing behind it is the real regression.
    # The check that was missing for six days. `files` and `lines` are computed now, so a
    # content file still typing them is not merely redundant — it is the exact failure this
    # release exists to end, and it fails silently, because a number with a sign in front of
    # it reads as something a tool produced. Loud, and not fatal: a page that still renders
    # is better than a build that refuses, and the author sees this the moment they run it.
    typed = [c.get("label") for c in scope
             if not c.get("auto") and c.get("label") in ("files", "lines")]
    if typed:
        print(f"[review] WARNING: {' and '.join(typed)} typed by hand in 'scope' — replace "
              'them with {"auto": "diffstat"}, which measures the change set at build time. '
              "A typed diffstat is the one number on this page nothing can catch going "
              "stale: it looks measured, and it outlives every commit made after it.",
              file=sys.stderr)

    if any(c.get("auto") == "autofixed" for c in scope):
        target = next((c.get("href", "") for c in scope if c.get("auto") == "autofixed"), "")
        anchor = target.lstrip("#")
        known = {tb.get("id") for tb in spec.get("tabs") or []} | \
                {sec.get("id") for sec in spec.get("sections", [])}
        if anchor and anchor not in known:
            print(f"[review] WARNING: the LLM-review chip deep-links to {target} but nothing "
                  "on the page has that id — the one number on the scope bar opens nothing",
                  file=sys.stderr)
        items = spec.get("findings", []) + spec.get("autofixes", [])
        if items and not any((i.get("source") or "").strip() for i in items):
            print("[review] WARNING: the chip merges /code-review and /simplify into one "
                  "number and not one item carries a 'source' — the split the chip "
                  "summarises is nowhere on the page behind it", file=sys.stderr)

    # The piles are one numbered list and each starts where the last stopped, so the order
    # in this file is the order on the page — including the order of the numbers. That
    # makes the ordering an editorial choice rather than a bug waiting to happen, and leaves
    # one rule worth enforcing: work that is already done is the tail. An `autofixes` block
    # anywhere but last opens the reviewer's list with items they have nothing to do about.
    _blocks = [b.get("type") for tb in (spec.get("tabs") or []) for b in (tb.get("blocks") or [])
               if b.get("type") in ("findings", "assumptions", "autofixes")]
    if "autofixes" in _blocks and _blocks[-1] != "autofixes":
        after = _blocks[_blocks.index("autofixes") + 1]
        print(f"[review] WARNING: the autofixes block renders before {after!r}, so the list "
              "opens with work that is already done — the piles are one list, and "
              "the applied fixes are its tail", file=sys.stderr)

    v = spec.get("verdict")
    title_score = ""
    if v:
        n = int(v["score"])
        # The score belongs beside the title: it is the one thing a reader wants before
        # they have decided whether to read anything. The band below keeps the reasons.
        band = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")
        face = (f'<b>{n}</b><small>/10</small>'
                f'<i>{html.escape(v.get("label", ""))}</i>')
        # `5/10 not yet mergeable` states a conclusion and shows none of the reasoning, so
        # the click every reader tries on it is the one that goes to the findings. It is
        # not a decoration with a link bolted on: the tab it opens is found by *content* —
        # whichever tab renders the findings — so a page that arranges its tabs
        # differently still sends the score where its reasons actually are.
        target, target_label = _score_target(spec)
        title_score = (
            f'<a class="titlescore {band}" href="#{html.escape(target, quote=True)}" '
            f'data-tip="Open the {html.escape(target_label, quote=True)} tab">{face}</a>'
            if target else f'<span class="titlescore {band}">{face}</span>')

    extra_css = "".join((out_dir / c).read_text(encoding="utf-8") for c in spec.get("extraCss", []))
    # The snippet extractor owns its own token colours, so the page asks it for them
    # rather than keeping a second copy that would drift from the highlighter.
    extra_css += subprocess.run(
        [sys.executable, str(EXTRACT), "--css"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout
    # Same rule for the code-owners check: the block is rendered by the script, so the
    # content file never has to remember to list a stylesheet it does not own.
    if any(b.get("type") == "codeowners"
           for t in spec.get("tabs") or [] for b in t.get("blocks", [])):
        extra_css += subprocess.run(
            [sys.executable, str(CODEOWNERS), "--css"],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout

    dspec = spec.get("diagrams", {})
    manifest_rows = read_manifest(out_dir / dspec.get("manifest", "assets/diagrams/MANIFEST.tsv"))
    placed = set()

    def heading(block, fallback_id, fallback_title):
        title = block.get("title", fallback_title)
        if not title:
            return ""
        head = (f'<h2 id="{html.escape(block.get("id", fallback_id))}">'
                f'{html.escape(title)}</h2>')
        return head + (f'<p>{block["body"]}</p>' if block.get("body") else "")

    # A block can hang a badge on the tab that holds it — filled in per tab, below.
    auto_badge = {}
    # Whether any tab asked for the test ledger. If none did and there is a manifest, the
    # page appends it to the tab the manifest belongs to rather than dropping it: the
    # whole point of computing what happened to the tests is that a reviewer sees it, and
    # a content file written before this block existed must not silently lose it.
    placed_ledger: list[bool] = []
    reset_list()
    # What the Review tab owes the reader above its first heading. Both are facts about the
    # branch rather than about any pile, so they are set once here and drained by whichever
    # pile renders first.
    # The aftermath first: it is the louder statement and it governs how the piles under
    # it should be read. The missing-record band is second, directly above the piles it
    # explains.
    set_bands([aftermath_html(out_dir, root),
               POINTS_MISSING_BAND if (spec.get("_reviewPoints") or {}).get("missing")
               else ""])

    def render_block(block):
        """One block of a tab, as (html, weight, changes).

        `weight` is "is there anything at all to show" — a tab whose every block weighs
        nothing is dropped. `changes` is the narrower question "did *this branch* move
        anything here" — a tab that is all context and no delta is kept, and struck
        through on the strip. A picture of the current state is not a change; that is
        why `puml` and `codecity` carry weight but no changes."""
        kind = block.get("type", "section")
        if kind in PILE_BLOCKS:
            return render_pile_block(spec, block, heading)
        if kind == "diagrams":
            # A block may name a manifest of its own. One producer does: the C2 view is
            # projected from the sequence diagrams rather than diffed out of a .puml that
            # changed, and it cannot file its row in `assets/diagrams/MANIFEST.tsv`
            # because `puml-diff.sh` does `rm -rf` on that whole directory every time it
            # runs — which the `sequence` step makes it do AFTER the `diagrams` step
            # wrote it. Its rows stay out of `placed` on purpose: `placed` answers "did
            # every row of the SHARED gallery find a tab", and a private manifest has no
            # orphans to warn about.
            own = block.get("manifest")
            source_rows = read_manifest(out_dir / own) if own else manifest_rows
            rows = select_rows(source_rows, block)
            if not own:
                placed.update(r["name"] for r in rows)
            # Nothing of this family changed. A block that names a `context` diagram
            # (the Packages case: no delta, but the current package shape is still
            # worth showing) falls back to rendering it from source — exactly like a
            # standalone `puml` block, and `render_puml` never returns zero weight, not
            # even for a missing file. That is what makes a tab built on this one block
            # *reliably* struck-through-but-present rather than droppable: the guarantee
            # lives here, not in the discipline of remembering to pair it with a second
            # block that happens to always weigh 1.
            if not rows:
                context = block.get("context")
                if context:
                    return (heading(block, "diagrams", dspec.get("title", ""))
                            + render_puml(context, root, out_dir), 1, 0)
                return "", 0, 0
            merged = dict(dspec)
            # The rows are already filtered; `only` survives purely as the running order
            # the author asked for. Popping it here is what used to make
            # `only: ["DomainModel", "DB"]` come out alphabetical anyway.
            merged["only"] = block.get("only") or dspec.get("only") or []
            if own:
                # Every SVG a row names is resolved relative to its own manifest, so this
                # has to travel with the rows or the pictures 404 next to the gallery's.
                merged["manifest"] = own
            return (
                heading(block, "diagrams", dspec.get("title", ""))
                + render_diagrams(merged, root, out_dir, rows),
                len(rows), len(rows),
            )
        if kind == "testpairs":
            rows = [r for r in select_rows(manifest_rows, block) if r["kind"] == "sequence"]
            placed.update(r["name"] for r in rows)
            return render_testpairs(block, dspec, manifest_rows, root, out_dir)
        if kind == "logging":
            return logging_fragment(block, root)
        if kind == "puml":
            return (heading(block, "puml", block.get("title", ""))
                    + render_puml(block, root, out_dir), 1, 0)
        if kind == "codeowners":
            frag, summary = codeowners_fragment(block, root, out_dir)
            state, owned = summary["state"], summary["owned"]
            # No CODEOWNERS in the repository is not a finding, it is an absence: drop
            # the tab rather than teach the reviewer to ignore a permanent grey box.
            if state == "no_codeowners":
                print("[review] no CODEOWNERS file — dropping the code-owners tab",
                      file=sys.stderr)
                return "", 0, 0
            if state == "approval_required":
                auto_badge["tabClass"] = "alarm" if summary.get("severity") == "critical" else "warn"
                auto_badge["label"] = "approval required"
            # No default heading, for the reason `codecity` has none: the tab pill says
            # CODEOWNERS, its badge says "Code owners approval required", and the seal
            # under it says APPROVAL REQUIRED. A fourth `Code owners` above the first
            # filename is the label said again. An explicit `title` still renders.
            return (heading(block, "codeowners", "") + frag,
                    1, len(owned))
        if kind == "tests":
            frag, moved = render_test_ledger(test_doc.get("tests", []), root)
            placed_ledger.append(True)
            if not frag:
                return "", 0, 0
            return (heading(block, "test-ledger",
                            block.get("title", "What this change set did to the tests"))
                    + frag, 1, moved)
        if kind == "traces":
            touched = {(Path(t["path"]).name, t.get("line"))
                       for t in test_doc.get("tests", []) if t.get("status") != "unchanged"}
            frag, n = render_traces(traces_doc, root, out_dir, touched)
            if not frag:
                return "", 0, 0
            # No heading: the block is a registry the 📺 on the covering-tests rows read,
            # not a thing to look at. Weight, and no changes — the same call `codecity`
            # and `puml` make. A trace is a recording of how the code behaves now; it is
            # evidence *about* the branch, not a thing the branch moved.
            return frag, n, 0
        if kind == "codecity":
            # A delta, unlike `puml`. The strike-through on a tab means "we looked and this
            # branch did not touch it", and a `puml` card earns it honestly: a context
            # diagram can be the same picture at both ends of the branch. This shot cannot
            # — the lit buildings *are* the classes the change set touched, so a city with
            # anything in it is a city this branch changed, and the tab was being struck
            # through over a picture whose whole subject is the change.
            return city_html, (1 if city_html else 0), (1 if city_html else 0)
        if kind == "section":
            body = by_id.get(block["id"])
            if body is None:
                raise SystemExit(f'[review] tab block references no section: {block["id"]}')
            # A section is prose we wrote about the change, so it counts as a change
            # unless it declares itself context.
            return body, 1, 0 if unchanged_ids.get(block["id"]) else 1
        if kind == "html":
            has = 1 if block.get("html") else 0
            return block.get("html", ""), has, 0 if block.get("unchanged") else has
        raise SystemExit(f"[review] unknown tab block type: {kind}")

    tabs = spec.get("tabs")
    lede_html =f'<div class="lede">{spec.get("summary", "")}</div>' if spec.get("summary") else ""
    summary_html = lede_html
    overview_html = ""
    if tabs:
        # The summary and the verdict used to sit above the strip, which pushed the
        # questions below the fold on a laptop — a reviewer scrolled past the answers to
        # find out what the answers were. So they became a tab, and that was one move too
        # far: a tab is a question a reader chooses, and *"what is this change, and is it
        # mergeable"* is not chosen — it is what the page opens with. It bought a pill in
        # the strip, a click to leave, and a second click to come back for a summary
        # nobody returns to twice.
        #
        # They open the first tab instead, as its lede. The strip still lands in the first
        # screenful, the reader still reads the summary before anything else — and the tab
        # they are standing in when they finish it is the one they were going to open
        # next. It rides on `intro` rather than as a block, so it sits above the tab's own
        # preamble (the summary is about the change; an intro is about the tab) and, like
        # every intro, carries no weight: the panel it opens is kept alive by its own
        # content, never by the page's lede leaning on it.
        overview_html = lede_html
        summary_html = lede_html
        if overview_html:
            first, *rest = tabs
            tabs = [{**first, "intro": overview_html + first.get("intro", "")}, *rest]
            lede_html = ""
    # The ledger is derived data, like the requirement lists it sits under: nobody writes
    # it, and a content file that predates the block would otherwise leave the manifest
    # computed and unread. So a page that has a manifest and no `tests` block gets one,
    # appended to the tab the `tests` step feeds. Declaring the block explicitly is still
    # how you put it somewhere else in the panel — this only fills a gap, it never moves
    # a block the author placed.
    #
    # `"testLedger": false` turns the gap-filling off, and is for the one page shape that
    # does not have the gap: a Tests tab whose own card already lists every test the change
    # set moved — new, edited and deleted alike — which is what the requirements map does.
    # There the ledger is the same rows a second time, grouped by a question the stamps on
    # those rows already answer, and a reader made to hold two lists and diff them is a
    # reader the second list cost something. Off is a claim the author is making, so it is
    # said out loud rather than inferred: the build cannot read a hand-authored fragment
    # and know what is in it.
    if tabs and spec.get("testChanges") and not any(
        b.get("type") == "tests" for tab in tabs for b in tab.get("blocks", [])
    ):
        if spec.get("testLedger") is False:
            print("[review] testLedger:false — no ledger. Every test the change set moved "
                  "has to be listed on the page some other way, deleted ones included.",
                  file=sys.stderr)
        else:
            host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
            if host is None:
                print(f'[review] WARNING: there is a test manifest and no tab carries a '
                      f'"tests" block — and no tab is called {LEDGER_TAB!r} to append it '
                      "to, so what the change set did to the tests is on no page.",
                      file=sys.stderr)
            else:
                host["blocks"] = list(host.get("blocks", [])) + [{"type": "tests"}]

    # The recordings fill their gap the same way, and for the identical reason: the step
    # that harvests them runs whether or not a content file mentions them, and a run that
    # copied eleven traces onto disk for nobody to open has spent the reader's disk and
    # given them nothing. They go *under* the ledger — the ledger is what the branch did,
    # the recordings are what the run did, and that is the order the questions arrive in.
    if tabs and spec.get("playwrightTraces") and not any(
        b.get("type") == "traces" for tab in tabs for b in tab.get("blocks", [])
    ):
        host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
        if host is None:
            print("[review] WARNING: traces were harvested and no tab carries a "
                  f'"traces" block — and no tab is called {LEDGER_TAB!r} to append one '
                  "to, so the recordings are on no page.", file=sys.stderr)
        else:
            host["blocks"] = list(host.get("blocks", [])) + [{"type": "traces"}]

    # Only a tabbed page grows a masthead; the plain single-column guide keeps the
    # heading it always had.
    strip_html = allbtn_html = mode_html = rerun_fail_html = ""
    if tabs:
        # Measured once, for every tab, before the loop: one subprocess and one transcript
        # scan rather than one per tab. `led` is None only when review-cost.py itself
        # could not be asked; a tab's own entry inside it is never missing (see
        # `tab_cost_report`'s docstring) — a bad day comes back as a "not measured"
        # sentence, not as a tab silently getting no number at all.
        led = cost_ledger_report(root, [t["id"] for t in tabs],
                                 (spec.get("pr") or {}).get("base") or "origin/main",
                                 out_dir)
        costs = (led or {}).get("tabs")
        strip, panels, dropped, quiet, emitted = [], [], [], [], []
        for tab in tabs:
            body, weight, changes = "", 0, 0
            auto_badge.clear()
            for block in tab.get("blocks", []):
                chunk, w, c = render_block(block)
                body += chunk
                weight += w
                changes += c
            if not weight and not tab.get("keepEmpty"):
                dropped.append(tab["label"])
                continue
            tid = html.escape(tab["id"])
            # A number on a tab is a promise that it means something. It does on the tab
            # holding the findings; on "Data model" it would just count pictures.
            badge = (tab.get("badge") or auto_badge.get("badge")
                     or (str(weight) if tab.get("count") else ""))
            badge_class = tab.get("badgeClass") or (
                auto_badge.get("class", "") if not tab.get("badge") else "")
            # An alarm is a colour, not a mark: the tab's own label goes red rather than
            # growing a `!` beside it. That leaves the words it stands for with nowhere on
            # screen to live, so they go where a machine still finds them — the button's
            # `aria-label`, which has to restate the label too, because `aria-label`
            # replaces the accessible name rather than adding to it.
            badge_label = tab.get("badgeLabel") or (
                auto_badge.get("label", "") if not tab.get("badge") else "")
            # A class on the pill itself, for a fact about the whole tab rather than about
            # a number on it. `tabClass` in the content file overrides, the same way
            # `badgeClass` does.
            tab_class = tab.get("tabClass") or auto_badge.get("tabClass", "")
            count = (
                f'<span class="n{" " + html.escape(badge_class) if badge_class else ""}"'
                + (f' role="img" aria-label="{html.escape(badge_label)}"'
                   f' data-tip="{html.escape(badge_label[:1].upper() + badge_label[1:])}"'
                   if badge_label else "")
                + f'>{html.escape(badge)}</span>'
            ) if badge else ""
            # Struck through rather than dropped: the answer "we looked, and this branch
            # did not touch it" is worth as much to a reviewer as the answer that it did.
            still = not changes and not tab.get("noStrike")
            if still:
                quiet.append(tab["label"])
            # No `data-tip` on a tab header, on purpose. The strip used to carry two
            # sentences on hover — why a tab is struck through, and what it cost — and both
            # were removed: a hover hint on a pill is unfindable, and the strip is the one
            # part of the page a reviewer navigates by, not reads. Both facts still reach
            # the reader, elsewhere and visibly: the strike-through itself says the branch
            # did not touch that tab, and the cost moved into the breakdown the cost chip
            # opens (`cost_ledger_html`), where every tab's number can be read at once.
            strip.append(
                f'<button type="button" class="tab{" quiet" if still else ""}'
                f'{" " + html.escape(tab_class) if tab_class else ""}" role="tab" '
                f'id="tabbtn-{tid}" aria-controls="{tid}" aria-selected="false" tabindex="-1"'
                + (f' aria-label="{html.escape(tab["label"])} — {html.escape(badge_label)}"'
                   if tab_class and badge_label else "")
                + f'>{html.escape(tab["label"])}{count}</button>'
            )
            # `intro` is prose about the *tab*, not about any one block in it — where the
            # data behind a whole panel came from, or what it deliberately does not say. It
            # is raw HTML and carries no weight: a tab is not kept alive by its own preamble.
            panels.append(
                f'<section class="panel" id="{tid}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{tid}">'
                f'<p class="paneltag">{html.escape(tab["label"])}</p>'
                f'{tab.get("intro", "")}{body}</section>'
            )
            emitted.append(tab)
        # A diagram in the manifest that no tab claimed would vanish without a word —
        # the exact silent drop this pipeline exists to prevent.
        orphans = [r["name"] for r in manifest_rows if r["name"] not in placed]
        if orphans:
            print(f"[review] WARNING: no tab claims these changed diagrams: {', '.join(orphans)}",
                  file=sys.stderr)
        if dropped:
            print(f"[review] dropped empty tabs: {', '.join(dropped)}", file=sys.stderr)
        # The cost tab is the build's own, not the content file's, and it is appended
        # after every declared tab — the last pill on the strip, past CODEOWNERS. Two
        # reasons it cannot be declared: its label is a measured number (`$744`), and this
        # page's whole discipline is that a number nobody can keep up to date is a number
        # that will be wrong; and its position is a fact about the page rather than about
        # any one review — the bill goes at the end, where a bill goes.
        cost_tab_body = cost_ledger_html(led, emitted)
        if cost_tab_body:
            # No decimals. `$744.18` on a tab pill invites reading the cents of a
            # list-price estimate whose error bars are the width of a whole session; `$744`
            # says the size, which is the only thing a label has room to say. The cents are
            # one click away, in the table the tab opens.
            # The pill says what the table says. Under the phase cut that is the phases'
            # own total, not the ledger's three overlapping measurements of one bill —
            # a `$703` pill over a `$207` table is the same contradiction as the footer's,
            # read first and by everyone.
            phase_total = (led.get("phases") or {}).get("cost")
            cost_label = f'${(phase_total if phase_total is not None and phase_rows_html(led.get("phases")) else (led.get("total") or 0.0)):,.0f}'
            strip.append(
                f'<button type="button" class="tab" role="tab" id="tabbtn-{COST_TAB_ID}" '
                f'aria-controls="{COST_TAB_ID}" aria-selected="false" tabindex="-1" '
                f'aria-label="cost — {cost_label} to write and review this change">'
                f'{cost_label}</button>')
            panels.append(
                f'<section class="panel" id="{COST_TAB_ID}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{COST_TAB_ID}">'
                '<p class="paneltag">Cost</p>'
                f'{cost_tab_body}</section>')

        # The strip leaves the body: it belongs to the masthead now, and the masthead is
        # assembled around it below. `body_html` is the panels alone, which is what every
        # rewrite downstream of here (the tab count, the enumeration check) is about.
        strip_html = (
            '<div class="tabstrip" role="tablist" aria-label="Review sections">'
            + "".join(strip) + "</div>"
        )
        # The show-everything toggle is not part of the strip any more (see the CSS): it
        # is emitted at the foot of the page, centred on its own line under the footer's.
        # The label says what it does *next* and therefore has to change with the state,
        # which is what the pressed styling alone could no longer carry once the button
        # left the strip — down here there is nothing beside it to read the highlight
        # against.
        # And which of the two pages this is — served by scripts/serve-review.py, where
        # buttons run and recordings play in the page, or a static copy (a file, the zip,
        # Pages), where they copy their command and hand over a `show-trace` line. Every
        # control on the page already degrades on its own; this is the one place that
        # says which world the reader is in, so it goes in the title row, beside the
        # score: the two things a reader wants before pressing anything are how the
        # branch did and what this copy can do. (It sat in the footer for an evening; a
        # fact nobody scrolls down for is a fact nobody reads.) Emitted as static: the
        # probe in SERVER_JS promotes it, never the other way round.
        # The static badge is a button, and what it copies is the way out of static: one
        # line that starts the server on this directory and opens this page from it.
        # `serve-review.py` prints the URL it ends up serving on — the next free port when
        # :7654 is already serving another checkout — so the line has to *read* that URL
        # rather than assume it, which is what the `$(…)` is. Absolute paths on purpose:
        # this is a fact about the machine the page was built on, like the show-trace
        # command on every trace row, and a reader on another machine has the zip's own
        # README for the general recipe.
        try:
            here = out_dir.resolve().relative_to(root.resolve())
        except ValueError:
            here = out_dir.resolve()
        serve_cmd = (f'cd {shlex.quote(str(root.resolve()))} && u="$('
                     f'{shlex.quote(str(HERE / "serve-review.py"))} {shlex.quote(str(here))}'
                     f' --page {shlex.quote(out_path.name)})" && (open "$u" 2>/dev/null'
                     ' || xdg-open "$u")')
        mode_html = (
            '<button type="button" class="chip chip-mode copycmd" id="hr-mode" '
            f'data-copy="{html.escape(serve_cmd, quote=True)}" '
            'data-tip="A static copy of the page: buttons copy their command instead of '
            'running it, and recordings open natively, not here. Click to copy the line '
            'that serves this directory and opens the page from it.">'
            'static</button>')
        # And, on the served copy only, the way to make the page catch up with the
        # repository. Both pieces are constants above, so what the page carries is one
        # thing a test can read rather than a string assembled inside a 400-line function.
        # Two of them: the free one, and the same thing with the model's half in front of
        # it. Side by side and in that order, because the cheap answer is the one a reader
        # should reach first and the expensive one should be the deliberate second look.
        mode_html += RERUN_CHIP + RERUN_AI_CHIP
        rerun_fail_html = RERUN_FAIL + RERUN_AI_CONFIRM
        allbtn_html = (
            '<div class="allbar">'
            '<button type="button" class="allbtn" aria-pressed="false" '
            'data-label-off="show single page" data-label-on="back to one tab at a time" '
            'data-tip="Every tab on one page. Makes \u2318F search all of it.">'
            "show single page</button></div>"
        )
        body_html = "\n".join(panels)
        # These two facts used to be appended to the page as a `<p class="sub">` — and the
        # append landed OUTSIDE every `<section class="panel">`, so the only element on the
        # page that no tab could hide sat under all eleven of them, restating a strike-
        # through the strip was already drawing three inches above it. Nothing in the
        # markup is a good home for it: a note about the strip belongs to the strip, and the
        # strip already says it (struck-through label, tooltip on hover; a dropped tab is
        # absent, which is the honest rendering of "nothing to show"). So it is said to the
        # build log, where the person assembling the page is the one who needs it.
        if quiet:
            print("[review] tabs kept as context (struck through, no delta): "
                  + ", ".join(quiet), file=sys.stderr)
        # Filled in from the tabs that survived, not from the tabs that were asked for: a
        # tab dropped for having nothing to show must not be counted in the walk-through
        # that promises the reader eleven of them.
        # The cost tab is deliberately absent from this list. `{{tabcount}}` and the
        # lede's walk-through are about the tabs that carry the review; requiring the
        # summary to also name `$744` would make every content file recite the page's own
        # furniture back at the reader.
        tab_labels = [t["label"] for t in emitted]
        body_html = body_html.replace(TAB_COUNT_TOKEN, spelled(len(tab_labels)))
        # The summary alone, not the whole overview: the walk-through is prose, and the
        # verdict beside it is a score and a label. Handed both, a page that dropped its
        # summary still arrives here with a non-empty string and gets warned that its
        # score dial forgot to name twelve tabs.
        check_tab_enumeration(summary_html, tab_labels)
    else:
        # No tab layout in the content file: the original single-column guide, unchanged.
        body_html = (
            '<h2 id="first">Requires human review</h2>\n'
            + render_findings(spec.get("findings", []))
            + f'\n<h2 id="diagrams">{html.escape(dspec.get("title", ""))}</h2>\n'
            + f'<p>{dspec.get("body", "")}</p>\n'
            + render_diagrams(dspec, root, out_dir)
            + f"\n{city_html}\n"
            + "".join(sections)
        )


    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(spec.get('title', 'Review guide'))}</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
{PAINT_HOLD_JS}
<style>{CSS}{extra_css.rstrip()}
{LATE_CSS}{XREF_CSS}</style></head>
<body><div class="wrap">
{masthead_html(spec, mode_html + title_score, chips, strip_html, base_st)}
{rerun_fail_html}
{lede_html}

{body_html}
<footer><p class="footrow"><span>{_link_home(spec.get('footer', ''))}</span> {TAKEAWAY}</p>{allbtn_html}</footer>
</div>
{SERVER_JS}
{CAPTION_JS}
{APP_ENV_JS}
{GENSEQ_JS}
{FOCUS_JS}
{DGM_VIEWS_JS}
{XREF_JS}
{EDITOR_JS}
{FRAME_JS}\n{TRACE_JS}\n{SEQLINK_JS}\n{SEQFOLD_JS}\n{HSCROLL_JS}\n{TABS_JS}\n{PAINT_RELEASE_JS}
{RERUN_JS}
{TIP_JS}
</body></html>
"""
    doc = code_xref.cross_link(doc)
    doc = open_links_in_new_tabs(doc)
    doc = one_tooltip_only(doc)
    check_baked_excerpts(doc)
    out_path.write_text(doc, encoding="utf-8")
    # After the page, so the manifest can never promise a verb for a build that failed to
    # write its own HTML — and every declaration is in by now, the register being filled
    # as the emitters run.
    write_actions(out_dir)
    print(f"[review] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
