"""The Review tab: findings, assumptions, auto-fixes, the aftermath band."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ..shared.actions import ACTIONS, declare_action, RERUN_ACTION
from ..shared.bands import _lede_above
from ..shared.commands import command_html

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
        "note": doc.get("note") if isinstance(doc.get("note"), dict) else None,
        "path": path.name}
    return spec["_reviewPoints"]


def points_note_band(points: dict | None) -> str:
    """The file's takeover note, as a band above the piles it qualifies.

    Amber, like the aftermath band for generated-only drift, because it is the same kind
    of statement: the piles below describe the branch at an earlier commit, and here is
    what was folded in since without anyone re-reading them. Grey would say "absence" and
    red would say "somebody changed the code"; this is neither — it is a decision, on
    record, that the reader has to know before trusting a count."""
    note = (points or {}).get("note")
    if not note:
        return ""
    return (f'<div class="rband rband-warn" role="status">'
            f'<p><b>{html.escape(note.get("heading", ""))}</b></p>'
            f'<div class="rb-sub">{note.get("html", "")}</div></div>')


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
    who already knows has to read past.

    `review-points.md`'s `source:` is one field carrying up to three facts the parser
    never splits apart — the pass, the effort it filed the item at, and which of several
    same-titled findings this one is (`/code-review high (the PUT-clears-the-vet
    scenario)`) — because splitting them is a rendering decision, not a parsing one, and
    `review-points.py` is not this tab's to change. Only the pass name is the link: effort
    is a fact for the tooltip, not the face, and the parenthesised detail is prose that
    happens to follow a chip, not part of the chip itself."""
    src = (f.get("source") or "").strip()
    if not src:
        return ""
    # A known pass may be followed by its effort and a parenthesised detail; an unknown
    # source (`assumption`, a human name, a linter) never matches and falls straight to
    # the plain stamp below, whatever it says after the first word.
    name = next((n for n in PASS_DOCS if src == n or src.startswith(n + " ")), None)
    if not name:
        return f'<span class="f-src">{html.escape(src)}</span>'
    rest = src[len(name):].strip()
    detail_m = re.search(r"\(([^()]*)\)\s*$", rest)
    detail = detail_m.group(1).strip() if detail_m else ""
    effort = (rest[:detail_m.start()] if detail_m else rest).strip()
    tip = f"What {name} does, in the Claude Code docs"
    if effort:
        tip += f" — filed at {effort} effort"
    # The face is the command, which says nothing about where the link goes or how hard
    # the pass looked; the tooltip spends itself on the first, as every other tooltip on
    # this page does, and the detail after the chip spends itself on the second, in plain
    # text rather than crowded into a face that is also a link.
    chip = (f'<a class="f-src" href="{html.escape(PASS_DOCS[name])}" target="_blank" '
            f'rel="noopener" data-tip="{html.escape(tip)}">{html.escape(name)}</a>')
    return chip + (f' <span class="f-src-detail">({html.escape(detail)})</span>' if detail else "")


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


def pile_numbers(spec) -> tuple[int, int, int]:
    """`(open, fixed, assumed)` — the three counts every summary of this tab reads off the
    same three arrays, so a number cannot drift between the sticky line under the header
    and the masthead's review chip above it — including the third, which the chip prints
    as the coder's `N assumptions` and the line as `N implementation assumptions`. It used
    to be the other way round: a hand-typed `/code-review 8 findings` outlived the ninth
    finding being added, and nothing caught it, because nothing was looking. Both callers
    now count nothing twice."""
    return (len(spec.get("findings", [])), len(spec.get("autofixes", [])),
            len(spec.get("assumptions", [])))


def review_tab_badge(spec) -> dict:
    """What the number on the **Review** pill counts, and what it says it counts.

    It used to count nothing a reader could find. The pill said **10** while the sticky
    row under it said `6 open · 3 auto-fixed · 7 assumptions` and the masthead said
    `9 raised` — because the badge fell back to the tab's render *weight*, which is the
    "is there anything at all to show here" number every tab is dropped or kept by. On
    this tab that weight is findings + auto-fixes + one: the assumptions pile weighs a
    fixed 1 whatever is in it, so that its "which kind of empty this is" sentence keeps
    the tab alive. A layout sentinel was being read as a count of something, and it even
    collided with the `6 /10` score chip beside it — 10 read as the score's denominator.

    The tab's own comment says a number on a tab is a promise that it means something. The
    one number on this tab that a reader can point at is the open pile, which is what the
    header chip leads with (`6 open, 3 auto-fixed`) and what the sticky row's first clause
    counts — so it is that, off `pile_numbers`, the same arrays both of those read. The
    other two piles are not hidden by leaving them out: they are one line down, and they
    are work already dealt with, which is exactly what a pill on a tab should not be
    adding to a count of what is left to do.

    And it carries its own hover, because a bare number beside a score chip is a number a
    reader has to guess at.
    """
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    rest = " and ".join(x for x in (
        f"the {fixed_n} auto-fixed" if fixed_n else "",
        f'the {assumed_n} assumption{"" if assumed_n == 1 else "s"}' if assumed_n else "",
    ) if x)
    label = f'{open_n} open review issue{"" if open_n == 1 else "s"}'
    if rest:
        label += f". {rest[:1].upper()}{rest[1:]} are further down the tab, already dealt " \
                 "with, and this number leaves them out"
    return {"count": open_n, "label": label}


#: Kept only because `build-review-html.py` imports it by name. It used to be the width
#: at which the chip's third number stopped fitting and was cut whole — back when that
#: number rode along as `, N assumptions` on the same clause as the fixes. The third pile
#: is no longer an optional extra on the review's own sentence: it is a second sentence,
#: about a different agent, and a chip that drops it for want of two characters drops the
#: only trace on the masthead of what the coder guessed at. Nothing measures the face
#: against this any more.
SCOPE_CHIP_MAX_LEN = 34


def scope_chip_value(spec) -> str:
    """`6 open, <span class="sub">3 fixed; \U0001f916coder: 7 assumptions</span>` — read by
    the masthead's review chip rather than composed a second time beside it, off the same
    `pile_numbers` the counts line under the header reads. One vocabulary now, not two:
    the chip used to swap to `fixed, … declined` on a branch reviewed through
    `review-points.md`, which read as a different review from the one the line under the
    header described one scroll away.

    Two claims, two authors. The first clause is the reviewer's (what it found, what it
    already fixed — `auto-fixed` on the face was a word about how the fix arrived, which
    is the tooltip's business, not the pill's). The second is the coder's, and it carries
    its own robot because the page's robot means "a model produced this", not "the
    reviewer said this": the assumptions were recorded by the agent that wrote the code,
    while it was writing it. With no assumptions there is no second claim to make, so the
    clause is absent rather than zeroed. `implementation assumptions` — the counts line's
    own name for the pile — is spelled out there; the chip says only `assumptions`,
    because the room a tooltip has is the room a pill does not."""
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    sub = f"{fixed_n} fixed"
    if assumed_n:
        sub += f'; \U0001f916coder: {assumed_n} assumption{"" if assumed_n == 1 else "s"}'
    return f'{open_n} open, <span class="sub">{sub}</span>'


#: What lights up the counts line as the reader scrolls past the chapter each clause
#: names — the alternative to freezing the section heading itself, which is the one this
#: page settled on: the line already sits still (it is sticky), so it is the line that
#: gains a mark rather than a second element competing for the same job.
#:
#: An inline script, not a file added to `hrbuild/assets/` and wired through
#: `shared/assets.py`: that pipeline, and the script list it feeds `build-review-html.py`,
#: is `shared/`'s to change, and two other agents were mid-edit in exactly those files
#: while this was written. A paragraph-sized behaviour that belongs to one tab's one
#: paragraph is safer self-contained than borrowed into a home somebody else is using.
#:
#: The trigger line is read off the row's own resolved `top` (the masthead's height,
#: however `--strip-h` is currently expressed) plus its own `offsetHeight` — not a second
#: copy of those numbers, so the mark cannot land a pile-width off from where the row is
#: actually pinned. Recomputed on resize, because the strip wraps to a second row exactly
#: when the tab count or the viewport does. No `IntersectionObserver`, no mark: the links
#: stay exactly as clickable as they were before this existed.
#:
#: Watching the three headings themselves, directly, was the first attempt and it read
#: wrong: a heading is one line tall, so `isIntersecting` on it alone is true only while
#: it is crossing the trigger, which is the top of a scroll through a section a thousand
#: pixels long and false for the rest of the read — the mark would go dark the moment a
#: reader actually started reading. `IntersectionObserver` still drives it (it wakes the
#: check only when a heading nears the line, never on every scroll frame), but what
#: decides the mark is a position check across all three: whichever heading is the last
#: one to have scrolled above the trigger is the section the reader is standing in, and
#: that stays true for as long as the next heading has not arrived.
PILELEDE_SPY_JS = """<script>(function(){
// The row prints above its own tab's first heading (`_lede_above` puts the lede before
// the head, on purpose — the line describes the whole list, not the pile under it), so
// this script's own tag sits in the document *before* `#first`/`#fixed`/
// `#assumed` have been parsed. Read at the top level, `getElementById` on any of them
// returns null every time, `pairs` comes up empty, and the whole thing silently no-ops
// — the bug this file shipped with once already. Deferred to `DOMContentLoaded` (or run
// immediately if that has already fired, for a script that lands after it), the same
// three ids exist wherever else on the page they are.
function whenReady(fn){
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', fn);
  }else{
    fn();
  }
}
whenReady(function(){
var lede=document.querySelector('.pilelede');
if(!lede||!('IntersectionObserver' in window))return;
var links={};
lede.querySelectorAll('a[href^="#"]').forEach(function(a){
  links[a.getAttribute('href').slice(1)]=a;
});
var ids=Object.keys(links);
var pairs=ids.map(function(id){return {id:id, el:document.getElementById(id)};})
  .filter(function(p){return p.el;});
if(!pairs.length)return;
var io=null;
function triggerY(){
  return (parseFloat(getComputedStyle(lede).top)||0)+lede.offsetHeight;
}
// A heading counts as reached once its top is at the trigger line -- or at its own
// `scroll-margin-top`, whichever is lower on the page. The two are not the same line: a
// deep link (`#fixed`, from the row itself) parks the heading exactly at its scroll
// margin, which the stylesheet sets to strip + lede + .6rem so the heading clears the
// pinned row, and that .6rem left it just *under* the trigger. The mark then stayed on
// the previous chapter after a click on this one, which is the one moment a reader is
// certain which chapter they asked for.
function paint(){
  var t=triggerY(), current=null;
  pairs.forEach(function(p){
    var margin=parseFloat(getComputedStyle(p.el).scrollMarginTop)||0;
    if(p.el.getBoundingClientRect().top<=Math.max(t,margin)+1)current=p.id;
  });
  ids.forEach(function(id){links[id].classList.toggle('here', id===current);});
}
function setup(){
  if(io)io.disconnect();
  var t=Math.max(triggerY(),0);
  // A band, not a line. A 1-2px trigger line is exact on paper and wrong in a browser:
  // `IntersectionObserver` only reports what it sampled on a rendered frame, and a fast
  // flick of the wheel can move a heading clean across two pixels between one frame and
  // the next without either frame catching it mid-crossing — the mark then never wakes
  // up and stays lit on whatever section it last saw. A few hundred pixels of band is
  // cheap to observe and near-impossible for an ordinary scroll to jump over unseen.
  var band=Math.min(300, Math.max(window.innerHeight-t-40, 40));
  var bottom=Math.max(window.innerHeight-t-band,0);
  io=new IntersectionObserver(paint,
    {rootMargin:'-'+t+'px 0px -'+bottom+'px 0px', threshold:0});
  pairs.forEach(function(p){io.observe(p.el);});
  paint();
}
setup();
window.addEventListener('resize',setup);
// A belt beside the band's braces: once scrolling actually stops, `paint()` runs once
// more off the headings' real positions regardless of whether the band caught every
// frame in between, so the mark is never left stuck on a section the reader scrolled
// straight past. Unknown to a browser (`scrollend` is recent), `addEventListener`
// silently ignores the event name and the band above is what carries the behaviour.
window.addEventListener('scrollend', paint, {passive:true});
});
})();</script>"""


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
        # Open first, same as the other vocabulary below — the two sources disagree about
        # what to *call* the undecided pile (a content file's `findings` are untriaged, a
        # branch's are declined-by-the-agent) but agree on where it goes: first, because
        # it is the one a human still owes a decision to. What was fixed without asking
        # comes next, and what nobody could be asked about is always the tail — see the
        # `block is not None` clause below.
        if spec.get("findings"):
            n_open = len(spec["findings"])
            parts.append(clause(
                f"{n_open} open review issue{'' if n_open == 1 else 's'}",
                "findings", "first"))
        if spec.get("autofixes"):
            parts.append(clause(f"{len(spec['autofixes'])} auto-fixed", "autofixes", "fixed"))
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
        # `6 implementation assumptions`, flat. `coder` named who produced them, which the
        # card's own purple `assumption` chip says where the reader is standing, and `to
        # check` named the work — in a line whose other two clauses are bare counts, so the
        # asymmetry read as a fourth fact rather than as the same shape said three times.
        # `implementation` is the one word carried over from that trimming: on a page that
        # also runs `/code-review` and `/simplify`, "assumptions" alone reads as ambiguous
        # about *whose* — this pile is what the coder assumed while implementing, not a
        # reviewer's. Mode C is still the exception: there is no count to give, only the
        # reason there is none.
        parts.append(clause(
            "coder could not be asked"
            if block.get("mode") == "C" and not assumed
            else f"{assumed} implementation assumption{'' if assumed == 1 else 's'}",
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
            + "</p>" + PILELEDE_SPY_JS)


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


#: The confidence chip's tooltip, fixed rather than composed per item — Victor's own
#: words, verbatim. It names the scale, not the one number already on the chip's own
#: face; the number does not need saying twice.
CONFIDENCE_TIP = "Confidence ∈ [0.1 .. 0.9]"


def _confidence_chip(f) -> str:
    """The number beside the purple `assumption` chip, read verbatim off
    `review-points.json`'s `confidence` — how sure the agent that wrote the code is that
    this reading of the ticket is the right one, not a severity: absent when the item
    declares none, because a scale a model was never asked to fill in is not the same fact
    as a model that filled it in at the middle. `.sev-med`'s amber marks anything under
    0.5, the same hue the rest of the page already spends on "worth a second look" — a
    confidence low enough to flag is exactly that, not a new colour to learn."""
    c = f.get("confidence")
    if c is None:
        return ""
    shown = f"{c:.2f}".rstrip("0").rstrip(".") or "0"
    cls = "f-confidence sev-med" if c < 0.5 else "f-confidence"
    return (f'<span class="{cls}" title="{html.escape(CONFIDENCE_TIP, quote=True)}">'
            f'{shown}</span>')


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
    # Least sure first. A confidence is the one number on this pile that ranks the
    # cards by how much they need the reader's judgement rather than by anything about
    # when the agent happened to write them down, and the reader's attention is worth
    # spending on the ones the agent itself was least sure about before the ones it
    # already trusted. An item that named no confidence at all is neither sure nor
    # unsure — it is unmeasured — so it goes after every measured one, in the order the
    # file already put them in: `sorted` is stable, and comparing `(False, 0.4)` against
    # `(True, None)` never touches `None` against a number.
    items = sorted(items, key=lambda f: (f.get("confidence") is None, f.get("confidence")))
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
            + _confidence_chip(f)
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


def _revert_offer(c: dict, root: Path) -> str:
    """*Revert it*, on the row of the commit it reverts.

    It was taken off the rows once, on the argument that a commit in this band is not a
    mistake but a commit the page has not caught up with — which is true of the *tooling*
    half of the band, and is why this is only rendered over the branch's own commits, with
    the cherry-picks folded away under their own grey row carrying nothing to press. Over
    what a human actually wrote after the review was signed off, the question the band
    raises is exactly "do I want this in the change under review", and the README has
    promised the answer since the band was built.

    It is safe to put behind a button because of the flag: `git revert --no-commit` stages
    an inverse and stops. Nothing is committed, nothing is pushed, `git reset` undoes it —
    the click leaves a diff to look at rather than a commit made on the reader's behalf,
    which is the whole reason the band's promise is worded that way.

    Per commit and not for the range, because the range is usually not what anyone wants
    undone: the hand edit is the question and the guardrail beside it is not.

    The **short** sha, not the full one: the command is what the clipboard hands over and
    what its hover shows, and a reader checking that line before pasting it stops checking
    at forty characters of hex. `git revert` resolves a short sha, and a prefix that is
    genuinely ambiguous makes git refuse loudly instead of reverting the wrong commit.
    """
    short = c.get("short") or (c.get("sha") or "")[:9]
    if not short:
        return ""
    aid = declare_action(
        f"aftermath-revert:{short}",
        f"cd {shlex.quote(str(root.resolve()))} && git revert --no-commit {short}",
        label=f"Stage the inverse of {short} in the working tree")
    return ('<span class="rb-act">'
            + command_html(ACTIONS[aid]["command"], aid, label="Revert it",
                           tip=(f"Revert it \u2014 runs `git revert --no-commit {short}` on the "
                                "server serving this page. It stages the inverse and stops: "
                                "nothing is committed, nothing is pushed, `git reset` undoes it."),
                           running="Staging the inverse\u2026")
            + '</span>')


def _aftermath_commit(c: dict, root: Path | None = None) -> str:
    """One commit's row: what it is, and — for the branch's own — what to do about it.

    The sha (carrying the file list in its hover), what the commit did, when, and
    *Revert it*. No `root`, no button: that is how the tooling fold renders the base's own
    cherry-picks, which are `main` arriving the way this project's workflow says it should
    and are not anybody's mistake to undo.

    The band's other answer — *Regenerate the report* — stays where it is, once under the
    whole list: it does not name a commit, so three copies of it under three shas would be
    three identical buttons inviting the reader to work out which row each belonged to.
    This one names one, which is exactly why it belongs on the row.
    """
    when = (c.get("when") or "")[:10]
    return ('<li>'
            f'<code data-tip="{html.escape(_aftermath_files_tip(c), quote=True)}">'
            f'{html.escape(c["short"])}</code> '
            f'{html.escape(c.get("subject", ""))}'
            + (f' <span class="rb-gen">{html.escape(when)}</span>' if when else "")
            + (_revert_offer(c, root) if root is not None else "")
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

    **One control, not a button with a glyph beside it.** It was a grey pill reading
    *Regenerate the report* and, next to it, a separate `↻` — two elements, one action, and
    a reader who pressed one had no way to know the other did the same thing. Now the words
    and the mark are the same control, which is the rule every command on this page follows.

    **And the line it copies is the line the server runs.** `__rerun__` is not a name out of
    the content file — it is the server's own verb — but its command is declared in the
    manifest like everything else, so this does not reconstruct it. It used to, and the two
    had drifted: this printed `cd <repo> && refresh-report.py --dir … --steps static` while
    `serve-review.py` ran the same program through a different interpreter with `--no-serve`
    on the end. Reading the register is what makes the drift unrepresentable rather than
    merely fixed.
    """
    entry = ACTIONS.get(RERUN_ACTION)
    if not entry:
        return ""
    return ('<p class="rb-actions"><span class="rb-act">'
            + command_html(entry["command"], RERUN_ACTION,
                           label="Regenerate the report",
                           tip="Rebuilds this page against the branch as it is now",
                           running="Rebuilding this page…")
            + '</span></p>')


def _tooling_commit_shas(root: Path, base_ref: str | None, shas: list[str]) -> set[str]:
    """Which of these commits are the base's own, already — read with `git`, at build
    time, because the aftermath step hands the band a list of commits and nothing about
    which of them the base already carries.

    Two different ways a commit can already be `base_ref`'s: `git cherry` catches a
    cherry-pick — a new sha, the same patch, reported `-` when an equivalent (by patch-id)
    already sits on the base. `git merge-base --is-ancestor` catches the other way a
    commit crosses branches unchanged — a merge, or a rebase that replays without
    conflict — where the sha itself, not just its patch, is already reachable from the
    base. Neither test alone catches both: a cherry-pick gets a new sha `git
    merge-base` has never seen, and a merged commit's patch-id is exactly the one already
    on the base, which is what `git cherry` is answering in the first place — the two are
    complementary, not redundant.

    **Asked once per commit, not once for the branch.** `git cherry <base> <head>` is a
    *symmetric* difference: it patch-ids `base..head` on one side and `head..base` on the
    other, and reports `-` only for a pair that matches across the two. The moment the
    branch merges the base — which is exactly what a branch that also cherry-picks tooling
    does, and what `CLAUDE.md` tells this project to do — `head..base` empties out, because
    the base's commits are now reachable from the head. There is nothing left on the right
    to match against, so every pick comes back `+`. On the demo branch that reported 2
    tooling commits where 8 had been picked: the six it missed were all older than the
    first `Merge main`.

    Per commit the window is the honest one again: `<sha>..<base>` is everything the base
    grew that this particular commit cannot see, which is where a commit copied off the
    base actually lives. One `git cherry` per commit, and on a twenty-commit branch the
    whole loop is well under a second — the band is built once per page.

    Best-effort, and silent about it: a repository this cannot ask (no `base_ref`, a
    shallow clone, `git` missing) reports every commit as the branch's own rather than
    guessing, because a tooling commit wrongly kept in the list a reader can filter with
    their own judgement; a branch commit wrongly folded away as tooling is invisible."""
    if not shas or not base_ref:
        return set()
    tooling: set[str] = set()
    for sha in shas:
        cherry = subprocess.run(["git", "cherry", base_ref, sha], cwd=root,
                                capture_output=True, text=True)
        if cherry.returncode == 0:
            for line in cherry.stdout.splitlines():
                marker, _, listed = line.strip().partition(" ")
                # Its own line, not the whole range: `base..sha` ends at `sha` but also
                # holds every branch commit before it, and those are answered by their
                # own pass with their own window.
                if listed == sha and marker == "-":
                    tooling.add(sha)
        if sha in tooling:
            continue
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, base_ref],
                              cwd=root, capture_output=True)
        if anc.returncode == 0:
            tooling.add(sha)
    return tooling


def _merge_seam_shas(root: Path, shas: list[str]) -> set[str]:
    """The merge commits among these, which the band drops rather than lists.

    A `Merge main: …` commit has no patch of its own — `git show` on it is empty — so it
    can be neither a cherry-pick (`git cherry` refuses to patch-id a merge and leaves it
    out of its output entirely) nor an ancestor of the base (the merge itself was made on
    the branch). It falls through both tests in `_tooling_commit_shas` and lands in the
    branch's own list, where it reads as a commit that changed nothing.

    Everything it brought is already on the list beside it, commit by commit, folded as
    tooling. The seam itself is the one row that says nothing a reader can act on, so it
    does not get a row. Merges only: an ordinary commit with an empty file list is a
    person having committed nothing, which is worth seeing."""
    if not shas:
        return set()
    out = subprocess.run(["git", "rev-list", "--merges", "--no-walk", *shas],
                         cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _code_totals(commits: list[dict]) -> dict:
    """`{files, added, deleted, genFiles}` over exactly these commits' own file lists —
    not the aftermath step's pre-aggregated `totals`, which is summed over every commit
    the step saw. This band may now be folding some of those away as tooling, and a total
    that still includes a folded commit's lines is the thing the fold exists to stop
    saying. Same arithmetic `run-steps.py` already does per commit; asked here of
    whichever subset the band is about to head with."""
    files = added = deleted = gen_files = 0
    for c in commits:
        for f in c.get("files") or []:
            if f.get("generated"):
                gen_files += 1
                continue
            files += 1
            added += f.get("added", 0) or 0
            deleted += f.get("deleted", 0) or 0
    return {"files": files, "added": added, "deleted": deleted, "genFiles": gen_files}


def _tooling_fold_html(commits: list[dict], base_label: str) -> str:
    """The base's own commits, folded to one grey row — expandable, never counted in the
    band's headline. A cherry-picked guardrail is not news about this review; it is
    `main`'s own history riding along, and a band that lists eight of them beside two
    commits that actually touched the feature buries the two a reader came for."""
    n = len(commits)
    plural = "" if n == 1 else "s"
    items = "".join(_aftermath_commit(c) for c in commits)
    return ('<details class="toolcommits"><summary><span class="foldlbl">'
            f'{n} tooling commit{plural} merged from {html.escape(base_label)}'
            f'</span></summary><ul>{items}</ul></details>')


def aftermath_html(out_dir: Path, root: Path, base_ref: str | None = None) -> str:
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

    `base_ref` splits the commits a second way, orthogonal to generated/not: a tooling
    cherry-pick from `main` (see `_tooling_commit_shas`) is folded into one grey row and
    left out of every count in the headline, because it is not a change to this review —
    it is `main` arriving, the way `CLAUDE.md`'s own workflow says it should. The merge
    commits that brought it are dropped outright (see `_merge_seam_shas`): they carry no
    patch, so they belong to neither half of that split.
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
    # The seams first: a merge that brought the base in is not a commit this band has
    # anything to say about, and leaving it in makes both halves of the split wrong — it
    # is not tooling (it was made here) and it is not the branch's own work (it carries no
    # patch), so it inflates whichever list it falls into.
    seams = _merge_seam_shas(root, [c["sha"] for c in commits if c.get("sha")])
    commits = [c for c in commits if c.get("sha") not in seams]
    if not commits:
        return ""
    tooling_shas = _tooling_commit_shas(
        root, base_ref, [c["sha"] for c in commits if c.get("sha")])
    tooling = [c for c in commits if c.get("sha") in tooling_shas]
    branch_only = [c for c in commits if c.get("sha") not in tooling_shas]
    base_label = (base_ref or "the base").split("/", 1)[-1]
    code = _code_totals(branch_only)
    n = len(branch_only)
    plural = "" if n == 1 else "s"
    if code["files"]:
        lines = code["added"] + code["deleted"]
        # What is stale and what is not, named. The band used to say that everything on
        # every tab described the branch as it was — true of the page the agent built,
        # false one press of *Regenerate* later, when every measured tab is rebuilt from
        # the branch as it is now and only the model's half still dates from the review.
        # A reader who had just regenerated read the band as the page contradicting
        # itself, and asked why regenerating had not made the list go away. It cannot:
        # the list is code the review never judged, and only a new review pass — a commit
        # carrying `Review-Points:` — moves the point it is counted from. Said here, once,
        # so the button under the list is not mistaken for the thing that clears it.
        head = (f'<p><b>{n} commit{plural}, {lines} line'
                f'{"" if lines == 1 else "s"} changed since the agent finished.</b> '
                'The findings, the assumptions and the requirements matrix were written '
                'before them and have not seen them; every measured tab is rebuilt from '
                'the branch as it is now.</p>')
        sub = ('Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>. '
               + (f'{code["genFiles"]} generated file'
                  + ("" if code["genFiles"] == 1 else "s")
                  + ' moved as well and are not counted here. '
                  if code["genFiles"] else
                  'None of it is a generated file. ')
               + 'This list clears when a new review pass lands — a commit carrying a '
                 '<code>Review-Points:</code> trailer — not when the page is regenerated.')
        cls = "rband-alert"
        role = "alert"
    elif n:
        head = (f'<p>{n} commit{plural} since the agent finished, and every file '
                'in them is generated.</p>')
        sub = ('Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>. '
               'Regenerated output, not somebody editing the change under review — which '
               'is why this band is grey.')
        cls = "rband-warn"
        role = "status"
    else:
        # Every commit since the review folded away as tooling: nothing here is news
        # about the review, only about what `main` shipped in the meantime.
        head = (f'<p>Only tooling from {html.escape(base_label)} since the agent '
                'finished — nothing about this review changed.</p>')
        sub = 'Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>.'
        cls = "rband-warn"
        role = "status"
    return (f'<div class="rband {cls}" role="{role}">' + head
            + f'<p class="rb-sub">{sub}</p>'
            + ('<ul>' + "".join(_aftermath_commit(c, root) for c in branch_only) + '</ul>'
               if branch_only else '')
            + (_tooling_fold_html(tooling, base_label) if tooling else '')
            # After the list, not inside it: the commits are what happened, and this is the
            # one thing to do about all of them.
            + _regenerate_offer(out_dir, root) + '</div>')


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
    # changes what an empty one is allowed to say, and what each *item's* badge in the
    # autofixes pile is stamped with (`review-points.md` items were read and chosen —
    # "fixed" — a bare content file's were applied by the pass that raised them —
    # "auto-fixed") — and nothing else: the item shapes are identical, which is the whole
    # reason `review-points.md` could be bolted on without touching a renderer. The section
    # headings themselves no longer branch on it: "Open review issues" and "Auto-fixed"
    # read the same in both vocabularies, and only the lede above them still says whether a
    # pass or an agent's own second look raised the open pile.
    points = spec.get("_reviewPoints")
    if kind == "findings":
        items = spec.get("findings", [])
        head = _lede_above(
            head_of("first", "Open review issues" if points else "Requires human review"),
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
        head = _lede_above(head_of("assumed", "Implementation assumptions"),
                           opening_lede(spec))
        if points and not items and not points.get("missing"):
            return (head + points_empty_html("assumptions", points), 1, 0)
        # Weight 1 even with nothing in it: an empty pile still carries the sentence
        # saying *which* kind of empty it is, and that sentence is the point.
        return (head + render_assumptions(items, mode),
                1 if (items or mode) else 0, len(items))
    items = spec.get("autofixes", [])
    head = _lede_above(head_of("fixed", "Auto-fixed"), opening_lede(spec))
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
