"""The footer's boilerplate and the takeaway under it."""
from __future__ import annotations

import re

HOME_URL = "https://github.com/victorrentea/human-review"

# Where a finished page lives once the projector is off. `.github/workflows/pages.yml`
# publishes every snapshot under `demo/` to GitHub Pages on each push to main, so a reader
# who saw this report on somebody else's screen can open the real thing — diagrams, Code
# City, the feature video — by clicking, with nothing to install and nothing to download.
# It is the one URL worth reading out to a room.
#
# The gallery index rather than this report's own address, on purpose: `publish-demo.sh`
# copies review.html into `demo/<slug>/` **verbatim**, so the page cannot carry a link to
# where it is about to be published — at build time it has no slug and no way to learn
# one. Rewriting the file on publish would buy a self-address at the cost of the property
# that makes the snapshot trustworthy, which is a bad trade for one href. The index lists
# every snapshot with a card, so the reader is one click from the right one.
DEMO_PAGES_URL = "https://victorrentea.github.io/human-review/"

# Still built and still linked from the index, just not from here: `.github/workflows/
# demo-zip.yml` attaches a zip per snapshot to a rolling release. The footer used to offer
# it beside the container and now offers neither it nor a third link — a closing line has
# room for two, and of the three ways to keep this page the zip is the weakest, because
# off disk a review cannot reliably fetch its own content and every request it makes is
# cross-origin. Kept as a constant because the index page and the release notes both point
# at it and a reader who wants it is one click away.
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
# One sentence, two links, and almost no prose between them. It read `Download zip · or a
# runnable docker of this report.` — two nouns side by side, each naming a *file format*,
# which is an answer to a question the reader has not asked yet. The question they have is
# where this page is, and the two links are now the two answers to it: somewhere I can
# open it, and on my own machine. The verb in a link was a real mistake once (`Download
# here a standalone demo zip`, where the noun trailed outside the href and the eye landed
# on words that said nothing about what arrives); `online` and `run it locally` do not
# repeat it, because each is complete on its own and needs no words after it to be
# understood. What each one costs the reader stays in the hover.
#
# Online first: it is free, instant, and the only one of the two a reader can act on from
# a phone in the back of a room.
TAKEAWAY = (
    '<span class="takeaway">'
    'See this report '
    f'<a href="{DEMO_PAGES_URL}" target="_blank" rel="noopener" '
    'data-tip="Every published snapshot on GitHub Pages — the live page, diagrams, '
    'Code City and the feature video. Nothing to install.">online</a> or '
    f'<a href="{DEMO_DOCKER_URL}" target="_blank" rel="noopener" '
    'data-tip="The same pages as a container: docker run --rm -p 8642:80 '
    'ghcr.io/victorrentea/human-review:&lt;snapshot&gt; — served rather than off disk, '
    'so the page behaves the way it does here.">run it locally</a>.'
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
