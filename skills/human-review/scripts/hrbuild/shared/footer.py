"""The footer's boilerplate and the takeaway under it."""
from __future__ import annotations

import re

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
