"""The page's head: title, favicon, score, ref badges, the strip's frame."""
from __future__ import annotations

import base64
import html
import urllib.parse

from .chips import base_warning
from .forge import forge_at, is_pull_url

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
# The tab of a page a review server is behind: the face at a laptop, drawn round with a
# black outline and a white glow outside it. The tab strip is where a reader picks between
# a live copy and a static one, and the icon is the one part of a tab that stays legible at
# forty tabs wide — a mark in front of the title is cut off before it is read. Outline and
# glow are grown from the emoji's own silhouette (`feMorphology`), so they hug the face;
# 6 units of the 100 are 2px at the 32px a Retina tab strip draws it (Victor, 21 Oct 2026).
FAVICON_SERVED_EMOJI = "\U0001F9D1\u200D\U0001F4BB"
FAVICON_SERVED_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<defs><filter id="o" x="-30%" y="-30%" width="160%" height="160%">'
    '<feMorphology in="SourceAlpha" operator="dilate" radius="6" result="line"/>'
    '<feMorphology in="SourceAlpha" operator="dilate" radius="11" result="wide"/>'
    '<feGaussianBlur in="wide" stdDeviation="4" result="soft"/>'
    '<feFlood flood-color="#fff"/><feComposite in2="soft" operator="in" result="glow"/>'
    '<feFlood flood-color="#000"/><feComposite in2="line" operator="in" result="ink"/>'
    '<feMerge><feMergeNode in="glow"/><feMergeNode in="glow"/><feMergeNode in="ink"/>'
    '<feMergeNode in="SourceGraphic"/></feMerge></filter></defs>'
    '<text x="50" y="52" text-anchor="middle" dominant-baseline="central" font-size="66" '
    'filter="url(#o)" '
    'font-family="Apple Color Emoji,Segoe UI Emoji,Noto Color Emoji,sans-serif">'
    + FAVICON_SERVED_EMOJI + "</text></svg>"
)
FAVICON_SERVED = ("data:image/svg+xml;base64,"
                  + base64.b64encode(FAVICON_SERVED_SVG.encode()).decode())


def page_title(spec: dict) -> str:
    """The one line that says which change this is.

    A content file’s own `title` is a sentence about the change ("Attending vet on a
    visit"). The reviewer, though, is looking at a pull request, and the name that
    matches what is in their tabs, their notifications and their `gh pr` output is
    `PR#37 <the PR’s own title>`. So when the content file names a PR, that wins, and
    the number is the link to it. With no `pr` block nothing changes.

    `PR#37` is the only link on this page a reader cannot recognise as one by where it
    sits: it is the first word of the `<h1>`, so it wears the page's heading weight, not
    a link's. The hover says where it goes, and only that — the number is already the word
    the pointer is on — the one thing a reader wants before clicking
    away from the review they just opened.
    """
    pr = spec.get("pr") or {}
    if pr.get("number") and pr.get("title"):
        num = f'PR#{html.escape(str(pr["number"]))}'
        if pr.get("url"):
            num = (f'<a class="prref" href="{html.escape(pr["url"])}" '
                   f'data-tip="Open on {forge_at(pr["url"]).name}">'
                   f'{num}</a>')
        return f'{num} {html.escape(pr["title"])}' + title_ticket_ref(pr, pr["title"])
    title = spec.get("title", "Review guide")
    # Where `PR#37` would stand, two muted words: every judge of eval runs 13-18 read the
    # missing publish button and GitHub links as a silent omission — the hover on the
    # counts line was not enough. A `/pull/` URL alone still counts as a PR.
    nopr = ("" if not pr or is_pull_url(pr.get("url")) else
            '<span class="nopr" data-tip="No pull request yet — no GitHub links or '
            'publishing.">no PR</span> ')
    return nopr + html.escape(title) + title_ticket_ref(pr, title)


def title_ticket_ref(pr: dict, title: str) -> str:
    """` (#25)` — the ticket the change answers, after the title, linked to it.

    The reference page reads `PR#49 Link Visit with Vet (#37)`; a branch with no pull
    request yet lost the ticket along with the PR number, and the title then named the
    change without saying which request it answers. Nothing when the content file names
    no ticket, or when the title already carries its number."""
    t = (pr or {}).get("ticket") or {}
    if not isinstance(t, dict):          # `"ticket": 37`, the short form the schema allows
        t = {"number": t}
    num = t.get("number")
    if not num or f"#{num}" in (title or ""):
        return ""
    ref = f"#{html.escape(str(num))}"
    if t.get("url"):
        where = forge_at(t["url"]).name
        tip = (f"Open the ticket on {where}: {t['title']}" if t.get("title")
               else f"Open the ticket on {where}")
        ref = (f'<a class="prref ticketref" href="{html.escape(t["url"])}" '
               f'data-tip="{html.escape(tip)}">{ref}</a>')
    return f" ({ref})"


#: The id that ties the branch chip's `+N` badge to the list it opens (tabs.js).
OUTSIDE_ID = "hr-outside"


def _outside_where(state: dict) -> str:
    return ("outside the review" if state.get("diffBaseSource") == "audited"
            else f"before {state['diffBase'][:8]}, where the counts start")


def _split_outside(state: dict | None) -> tuple[list[dict], list[dict]]:
    """(unreviewed, spec): the commits before the counts, apart from the ones that wrote
    this change's own OpenSpec documents (`spec` set by the build, off the Review tab's
    `_before_range_commits`). Eval run 10 counted b12c9bdb — this change's proposal, design
    and spec — among the tooling commits nobody reviewed; it is what the change was built
    against, and is listed as that, not counted."""
    outside = [c for c in (state or {}).get("outside") or [] if not c.get("onBase")]
    return [c for c in outside if not c.get("spec")], [c for c in outside if c.get("spec")]


def _picked_outside(state: dict | None) -> list[dict]:
    """The commits before the counts that the base already carries, cherry-picked under
    another sha (`chips.py:patch_equivalent`). Eval run 11 counted 5 of them among
    "7 never reviewed": a merge would bring none of them, so the `+N` leaves them out."""
    return [c for c in (state or {}).get("outside") or [] if c.get("onBase")]


def outside_badge(state: dict | None) -> str:
    """`+8` on the branch chip: the branch's commits the page does not count, one click
    from their list (`outside_note`).

    It used to be a line of its own under the chips, `8 earlier commits outside the review
    ▸`, and eval run 10 measured what that line cost: with the review chip wrapping too,
    the sticky header was 164px against the reference's 108px, on every tab, for a fact a
    reader needs once. It is a fact about the branch — which commits on it the numbers
    beside it leave out — so it rides on the chip that names the branch, as a count. The
    sentence is in its hover; the commits are in the list it opens."""
    other, specs = _split_outside(state)
    picked = _picked_outside(state)
    if not other and not specs:
        return ""
    n = len(other)
    tip = (f"{n} earlier commit{'' if n == 1 else 's'} on this branch "
           f"{_outside_where(state)}." if n else "")
    if picked:
        tip += f" {len(picked)} more already on {state.get('ref') or 'the base'}."
    if specs:
        tip += ((" Plus" if tip else "Before the review:") + " the spec this change was "
                f"built against ({', '.join(c['sha'][:8] for c in specs)}).")
    tip += " Click to list."
    # The face counts only the commits nobody reviewed; a branch whose only earlier commit
    # is its spec shows `spec`, not a `+1` that would read as one more unreviewed change.
    face, label = ((f"+{n}", f"{n} earlier commits {_outside_where(state)}") if n
                   else ("spec", "the spec this change was built against"))
    return (f'<button type="button" class="sn-badge" aria-expanded="false" '
            f'aria-controls="{OUTSIDE_ID}" aria-label="{html.escape(label)}" '
            f'data-tip="{html.escape(tip)}">{face}<span class="sn-caret" aria-hidden="true">'
            '&#9656;</span></button>')


def outside_note(state: dict | None, repo: str = "") -> str:
    """The list `outside_badge` opens: the branch's commits the page does not count.

    `page_base` measures from the base the review audited, which on a branch that carried
    commits before the review — a plan, an AGENTS.md, a skill — is past the fork point.
    The numbers above are then honest about the review and silent about those commits,
    and a reader comparing them with GitHub's `main...branch` would find a gap with no
    explanation. Named here, oldest last as `git log` lists them, so the gap is visible.
    Empty when the page measures from the fork point.

    Hidden until the badge is clicked. Opened, it sits IN FLOW at the foot of the masthead,
    under the tab strip — never over it (eval run 8 found an overlay covering 11 of 13
    tabs), and never between the chips and the strip, where opening it would move every
    tab out from under the pointer. Esc or a click outside closes it (tabs.js)."""
    other, specs = _split_outside(state)
    picked = _picked_outside(state)
    if not other and not specs:
        return ""
    n = len(other)

    def one(c: dict) -> str:
        # Sha and raw subject, one commit per line: the subjects are the commits' own words,
        # untranslated, because a paraphrase of a commit message is a claim about it.
        sha = html.escape(c["sha"][:8])
        face = (f'<a href="{html.escape(forge_at(repo.rstrip("/")).commit_url(c["sha"]))}"'
                f' target="_blank" rel="noopener"><code>{sha}</code></a>'
                if repo else f'<code>{sha}</code>')
        return f'<li>{face} {html.escape(c.get("subject") or "")}</li>'

    parts = []
    if other:
        parts.append(f'<p class="sn-head">{n} earlier commit{"" if n == 1 else "s"} '
                     f'{html.escape(_outside_where(state))}</p>'
                     f'<ul class="sn-list">{"".join(one(c) for c in other)}</ul>')
    if specs:
        where = ", ".join(sorted({html.escape(str(c["spec"])) + "/" for c in specs}))
        parts.append(f'<p class="sn-head sn-spec">The spec this change was built against '
                     f'(<code>{where}</code>), before the reviewed range — not unreviewed '
                     f'code</p><ul class="sn-list">{"".join(one(c) for c in specs)}</ul>')
    if picked:
        parts.append(f'<p class="sn-head">Already on {html.escape(state.get("ref") or "the base")}'
                     f' (cherry-picked)</p>'
                     f'<ul class="sn-list">{"".join(one(c) for c in picked)}</ul>')
    return (f'<div class="scopenote" id="{OUTSIDE_ID}" hidden>{"".join(parts)}</div>')


def ref_badges(spec: dict, state: dict | None = None) -> str:
    """`branch test-pr from main⚠️` — the two refs every number on this page is a
    comparison of, in one chip, each ref one click from its own page on GitHub.

    They lead the scope bar rather than trailing the title, and both moves are the same
    decision. "Against what, again?" is a question asked halfway down the ninth tab, so
    the answer has to be somewhere the masthead still shows — and the row it belongs in
    is the one that already answers *how much*: `files`, `lines`, what the review found,
    what it cost. Two refs and six numbers about them are one thought, and the title row
    is then free to be a title.

    They are chips, not parenthesised asides, because in that row `(test-pr)` beside
    `files +1 / ✏️40` reads as an unlabelled number. The label is what makes the pair
    legible in one pass, and it costs four characters.

    The chip carries `N↓` before the base ref when the two refs have drifted apart — the base has moved
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
    forge = forge_at(repo)
    warning = base_warning(state)

    def ref_html(ref: str, cls: str) -> str:
        name = f'<b class="refname {cls}">{html.escape(ref)}</b>'
        if not repo:
            return name
        href = html.escape(forge.tree_url(urllib.parse.quote(ref)))
        return (f'<a class="refl" href="{href}" data-tip="Open on {forge.name}" '
                f'target="_blank" rel="noopener">{name}</a>')

    branch, base = pr.get("branch"), pr.get("base")
    badge = outside_badge(state)
    if not branch and not base:
        return f'<span class="chip refchip">{badge}</span>' if badge else ""
    # One chip, one sentence: `branch test-pr from main`. Each ref is its own link, so
    # the chip is a span holding two anchors rather than one anchor around both.
    parts = []
    cls_extra = ""
    after = spec.get("_afterReview")
    ahead_mark = ""
    if branch and after and after.get("commits"):
        # `7↑test-pr`: commits added to the branch after the reviewed one, same style as
        # the `5↓main` mark. Their list is the hover, one per line.
        n = len(after["commits"])
        items = "".join(
            f'<li><code>{html.escape(c["short"])}</code> {html.escape(c["subject"])} '
            f'<span class="tipwhen">{html.escape(c["when"])}</span></li>'
            for c in after["commits"])
        tip = (f'<p class="tipfoot">{n} commit{"" if n == 1 else "s"} added to this PR after '
               f'the review (at {html.escape(after["review"])}):</p>'
               f'<ul class="tiplist prose">{items}</ul>')
        ahead_mark = (f'<span class="drift drift-ahead" role="img" '
                      f'aria-label="{n} commits after the review" '
                      f'data-tip-html="{html.escape(tip, quote=True)}">{n}\u2191</span>')
        cls_extra = " drifted"
    if branch:
        parts.append(f"branch {ahead_mark}{badge}{ref_html(branch, 'head')}")
        badge = ""
    mark = ""
    if base and warning:
        # `5↓main`: five commits behind main, the number sitting on the ref it measures.
        # The mark carries its own tooltip: the chip says what the refs are, the mark
        # says what is wrong with the pair, and a reader who hovers it is asking the
        # second question, not the first. A plain arrow, not the ⬇️ emoji.
        n = ((state.get("ahead") or 0) + (state.get("aheadPicked") or 0)
             or state.get("localBehind") or "")
        mark = (f'<span class="drift" role="img" aria-label="stale base" '
                f'data-tip="{html.escape(warning)}">{n}\u2193</span>')
        cls_extra = " drifted"
    if base:
        parts.append(f"{'from' if branch else 'base'} {mark}{ref_html(base, 'base')}")
    inner = " ".join(parts)
    return f'<span class="chip refchip{cls_extra}">{badge}{inner}</span>'


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
    if not spec.get("pr"):
        # No refs chip to carry the count: the badge leads the chips on its own.
        badge = outside_badge(base_st)
        chips = (f'<span class="chip refchip">{badge}</span>' if badge else "") + chips
    rows.append(f'<div class="scopebar">{chips}</div>')
    note = outside_note(base_st, (spec.get("pr") or {}).get("repo") or "")
    if not strip_html:
        return "\n".join(rows + ([note] if note else []))
    return ('<header class="masthead">\n' + "\n".join(rows + [strip_html] + ([note] if note else []))
            + "\n</header>")
