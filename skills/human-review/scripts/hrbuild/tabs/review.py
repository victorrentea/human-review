"""The Review tab: findings, assumptions, auto-fixes, the aftermath band."""
from __future__ import annotations

import html
import json
import shlex
import sys
from pathlib import Path

from ..shared.bands import _lede_above
from ..shared.commands import command_html, offer_words_html
from ..shared.util import HERE

SEVERITIES = {
    "high": ("sev-high", "must look"),
    "medium": ("sev-med", "worth a look"),
    "low": ("sev-low", "nit"),
    "info": ("sev-info", "context"),
}


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
