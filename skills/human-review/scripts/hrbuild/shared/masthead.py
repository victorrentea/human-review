"""The page's head: title, favicon, score, ref badges, the strip's frame."""
from __future__ import annotations

import base64
import html
import urllib.parse

from .chips import base_warning

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
