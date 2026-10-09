"""The Review tab: findings, assumptions, auto-fixes, the aftermath band."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

from ..shared.actions import ACTIONS, declare_action, RERUN_ACTION
from ..shared.bands import _lede_above, _flush_top_bands
from ..shared.commands import command_html
from ..shared.snippets import DIFF_CONTEXT, diff_html, github_blob_base

# The report's contract lives next to the scripts, beside the parser that writes it: the
# directory this package sits in is on sys.path whenever the package is importable.
import review_points_schema

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


def _points_parser():
    """`review-points.py` — hyphenated, so loaded by path, once."""
    import importlib.util
    name = "hr_review_points_parser"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(review_points_schema.__file__).with_name("review-points.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[name] = mod
    return sys.modules[name]


def unglue_refs(item: dict) -> None:
    """Split, in place, any ref of a report item that is two refs glued with a comma.

    The parser splits `- file: a.ts:12, b.ts:30` now; a report written before it did —
    or edited by hand — carries `a.ts:12, b.ts:30` as one path, and the build used to abort
    the whole page on a file by that name not existing. A ref the parser's own check still
    refuses is dropped with a warning instead: one card missing, not the page."""
    rp = _points_parser()
    title = re.sub(r"<[^>]+>", "", str(item.get("title") or ""))[:60]

    def parts(ref: str) -> list[str]:
        got, bad = rp.split_refs(str(ref))
        if bad:
            print(f"[review] WARNING: {title!r}: ref {ref!r} dropped — "
                  + "; ".join(bad), file=sys.stderr)
            return []
        return got

    if isinstance(item.get("refs"), list):
        item["refs"] = [p for ref in item["refs"] for p in parts(ref)]
    if isinstance(item.get("snippets"), list):
        out = []
        for s in item["snippets"]:
            if not isinstance(s, dict) or "ref" not in s:
                out.append(s)
                continue
            for i, p in enumerate(parts(s["ref"])):
                if rp.RANGED.search(p):
                    one = {**s, "ref": p}
                    if i:
                        one.pop("caption", None)
                    out.append(one)
        item["snippets"] = out
    if isinstance(item.get("diffs"), list):
        out = []
        for d in item["diffs"]:
            if not isinstance(d, dict) or "path" not in d:
                out.append(d)
                continue
            out.extend({**d, "path": re.sub(rp.RANGED, "", p)} for p in parts(d["path"]))
        item["diffs"] = out


def resolve_review_points(spec: dict, out_dir: Path) -> dict | None:
    """Make the Review tab's inputs the branch's and the page's, not the content file's.

    The piles come off `review-points.md` (`resolve_piles`); each Fixed card is dealt the
    hunks of the fix commit its anchors reach (`attribute_fix_hunks`); every anchor is
    carried from the commit it was written at to the tree the page quotes
    (`reanchor_refs`); the grade's reasons
    are computed from what the page measured and its number capped by them
    (`grade_signals`, `cap_grade`); and the content file's prose `summary`, which opened
    this tab above the grade, is dropped (`drop_model_summary`)."""
    drop_model_summary(spec)
    points = resolve_piles(spec, out_dir)
    attribute_fix_hunks(spec, out_dir)
    reanchor_refs(spec, out_dir)
    link_spec_citations(spec, out_dir)
    grade_signals(spec, out_dir)
    cap_grade(spec)
    return points


def drop_model_summary(spec: dict) -> None:
    """Drop `summary` when it would open the Review tab, and say so on stderr.

    It rendered as a bordered paragraph of the model's prose above the grade, where the
    reader arriving from the score expects the computed reasons. Everything it can say
    honestly the page now measures; what it says beyond that nobody checked."""
    tabs = spec.get("tabs") or []
    first = tabs[0] if tabs else {}
    if spec.get("summary") and any(b.get("type") in POINTS_PILES
                                    for b in first.get("blocks") or []):
        print("[review] content.json's `summary` is not rendered — the Review tab opens on "
              "the computed grade reasons, not on prose", file=sys.stderr)
        spec.pop("summary", None)


def resolve_piles(spec: dict, out_dir: Path) -> dict | None:
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
    path = out_dir / (spec.get("reviewPoints") or REVIEW_POINTS_JSON)
    doc = None
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as bad:
            raise SystemExit(f"[review] {path} is not JSON ({bad}) — regenerate it with "
                             "run-steps.py --only reviewpoints")
        # The report is the Review tab's only source, so its shape is checked before a
        # single pile renders — loudly, as `review-points.py` checks it on the way out. A
        # report from before the schema has no `schema` key and is refused the same way:
        # the reviewpoints step regenerates it from the committed file in a second.
        bad = review_points_schema.problems(doc)
        if bad:
            raise SystemExit(
                f"[review] {path} does not match "
                f"{review_points_schema.SCHEMA_PATH.name} — regenerate it with "
                "run-steps.py --only reviewpoints (or review-points.py):\n  "
                + "\n  ".join(bad[:20]))
        # When the branch carries a report, it is the Review tab: all three piles come
        # from it whatever the content file says. A content file that typed its own
        # piles beside a report is the old two-sources page, and the record wins.
        for key in POINTS_PILES:
            if isinstance(spec.get(key), list) and spec[key]:
                print(f"[review] WARNING: content.json writes its own `{key}`, ignored — "
                      f"the Review tab is rendered from {path.name} only", file=sys.stderr)
        asked = set(POINTS_PILES)
    if not asked:
        spec["_reviewPoints"] = None
        return None
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
        own_review_tab(spec)
        return spec["_reviewPoints"]
    for key in asked:
        items = doc.get(key)
        spec[key] = items if isinstance(items, list) else []
        for item in spec[key]:
            if isinstance(item, dict):
                unglue_refs(item)
    for warning in doc.get("warnings") or []:
        print(f"[review] review-points: {warning}", file=sys.stderr)
    spec["_reviewPoints"] = {
        "missing": False, "asked": asked, "sections": doc.get("sections") or {},
        "source": doc.get("source") or "review-points.md",
        "fixed_in": doc.get("fixed_in"), "meta": doc.get("meta") or {},
        "provenance": doc.get("provenance") or {},
        "frontmatter": doc.get("frontmatter") or {},
        "note": doc.get("note") if isinstance(doc.get("note"), dict) else None,
        "path": path.name}
    own_review_tab(spec)
    return spec["_reviewPoints"]


#: What each pile is called and what its intro says. The builder's, not the content
#: file's: a model writing content.json used to name the piles itself, and one run called
#: them "Candidates retained for human judgement", "Corrections from the recorded audit"
#: and "Implementation decisions" — the same three piles, unrecognisable from one page to
#: the next. With the piles read off the report, their titles are fixed here.
PILE_TITLES = {"assumptions": "Implementation assumptions",
               "findings": "Open review issues", "autofixes": "Auto-fixed"}

#: The tab's hover, owned for the same reason as the titles.
REVIEW_TAB_TIP = ("What the coding agent left open, fixed and assumed — its own "
                  "record, committed with the code.")


def pile_intro(kind: str, points: dict | None) -> str:
    """The one paragraph under a pile's heading, from the report rather than from prose.

    The fixed pile names the commit its diffs are measured against, because that is the
    one fact about it a reader cannot see: the left side of every diff below."""
    if kind == "assumptions":
        return "Where the ticket was ambiguous: the reading the coder chose."
    if kind == "findings":
        # "Open", like the chip, the tab's badge and the grade panel that count them: run 6
        # titled this pile "Open review issues" and then called it "closed decisions, not a
        # queue" one line down. The agent decided; the item stays open until a human agrees.
        return ("Left in the code, each with its reason. They stay open until you agree or "
                "disagree.")
    impl = ((points or {}).get("provenance") or {}).get("implementation", "")
    against = (f"<code>{html.escape(impl[:8])}</code>, the implementation commit"
               if impl else "the implementation commit")
    # Every commit of the fix range by name (`fix_commits_html`), not "the fix commit"
    # over a range whose last commit only re-anchored one line (eval run 10).
    named = (points or {}).get("fixCommitsHtml") or "the fix commit"
    warn = "".join(f' <span class="fixwarn">{w}</span>'
                   for w in (points or {}).get("fixWarnings") or [])
    # Copy pass (3 Oct 2026): where the pile was read from, and why the diffs are cut the
    # way they are, went. The left side of every diff below is the one fact kept.
    return f"Fixed by the review in {named}. Diffs against {against}." + warn


def own_review_tab(spec: dict) -> None:
    """Drop what the content file wrote over the piles: titles, intros, the tab's hover.

    Named on stderr, the way `own_layout` names what it drops from the script-owned
    tabs, so a content file that keeps typing them is told so instead of wondering why
    its words never reach the page."""
    for tab in spec.get("tabs") or []:
        blocks = [b for b in tab.get("blocks") or [] if b.get("type") in POINTS_PILES]
        if not blocks:
            continue
        for b in blocks:
            for key in ("title", "body"):
                if b.get(key) and b[key] != (PILE_TITLES[b["type"]] if key == "title"
                                             else None):
                    print(f"[review] content.json's {b['type']} {key} "
                          f"({re.sub('<[^>]+>', '', str(b[key]))[:50]!r}) is ignored — "
                          "the Review tab's headings are the builder's", file=sys.stderr)
                b.pop(key, None)
        tab["tip"] = REVIEW_TAB_TIP


def points_note_band(points: dict | None, repo: str | None = None) -> str:
    """The file's takeover note, as one amber row above the piles it qualifies:
    `▸ 32 commits made after the reviewed version (ce56d912)`, which unfolds into the
    commits themselves, each hash a link to it on github.com.

    Amber, like the aftermath band for generated-only drift, because it is the same kind
    of statement: the piles below describe the branch at an earlier commit, and here is
    what was folded in since without anyone re-reading them. The note's paragraph of
    reasons is not on the page any more — a reader needs the count and the commit the
    count starts from, and the heading (who decided, when) rides in the hover. The
    reviewed commit is a link too, so "what came after it" is one click into the branch's
    history on GitHub. A note with no commit list to fold stays the prose it was."""
    note = (points or {}).get("note")
    if not note:
        return ""
    body = note.get("html", "")
    commits = re.findall(r"<li>\s*([0-9a-f]{7,40})\s+(.*?)</li>", body, flags=re.S)
    reviewed = re.search(r"<code>([0-9a-f]{7,40})</code>", body)
    if not commits or not reviewed:
        return (f'<div class="rband rband-warn" role="status">'
                f'<p><b>{html.escape(note.get("heading", ""))}</b></p>'
                f'<div class="rb-sub">{_fold_note_lists(body)}</div></div>')

    def sha(h: str) -> str:
        code = f"<code>{h}</code>"
        return (f'<a href="{repo}/commit/{h}" target="_blank" rel="noopener">{code}</a>'
                if repo else code)
    n = len(commits)
    rows = "".join(f"<li>{sha(h)} {subject}</li>" for h, subject in commits)
    return (f'<div class="rband rband-warn" role="status">'
            f'<details class="takeover" title="{html.escape(note.get("heading", ""))}">'
            f'<summary><b>{n} commit{"s" if n != 1 else ""}</b> made after the reviewed '
            f'version ({sha(reviewed.group(1))})</summary><ul>{rows}</ul></details></div>')


def aftermath_reads_takeover(out_dir: Path) -> bool:
    """Whether the aftermath band already carries the takeover, read off `git`.

    When it does, `points_note_band` stands down: its list is the one an agent typed into
    the note, counted from the same commit, and two lists of the same commits a screen
    apart — one frozen, one live — was exactly the confusion. The note band stays the
    fallback for a page with no aftermath measurement."""
    try:
        doc = json.loads((out_dir / AFTERMATH_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("takeover"), dict)


def _fold_note_lists(body: str) -> str:
    """Each list in the note folded to one row: the sentence is what the reader needs,
    the thirty commit subjects are there to check, not to read before the piles."""
    def fold(m: re.Match) -> str:
        n = m.group(0).count("<li>")
        return (f'<details class="toolcommits"><summary>{n} commit{"s" if n != 1 else ""}'
                f'</summary>{m.group(0)}</details>')
    return re.sub(r"<ul>.*?</ul>", fold, body, flags=re.S)


#: The band that goes where the piles would have been. Not `render_findings([])`'s
#: "Nothing outstanding — the automated passes came back clean", which is the confident-
#: wrong page: no file is not a clean review, it is no review recorded.
POINTS_MISSING_BAND = (
    '<div class="rband rband-none" role="status">'
    '<p>No <code>review-points.md</code> on this branch — nothing records what was '
    'reviewed or declined.</p>'
    '<p class="rb-sub">The piles below are empty because the record is absent, not '
    'because the review was clean. <code>/record-review</code> is what writes the '
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


# Three piles, three lists, each numbered from 1: what the reviewer has to judge
# (findings), what is already done (applied fixes), what only they can answer
# (assumptions). They were one list numbered straight through, on the theory that "how much
# is there for me here?" wants one answer — but the counts line already gives that answer
# per pile, and a pile opening on 7 under "3 auto-fixed" made the reader hunt for the six
# that were not there. What separates the piles is their heading, the card's colour and one
# badge.
#
# The counter below no longer numbers anything; it only records how many items have been
# rendered so far, which is how the counts line knows it is at the top of the list. It is
# module state rather than a number threaded through `render_block`, which renders blocks
# one at a time by type and has no notion that three of them sit together.
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
    """The `<ol>` for the next pile, numbered from 1.

    The piles used to be one list numbered straight through, so "Auto-fixed" opened on 7
    under a counts line that said *3 auto-fixed* — and the reader's first question was
    where the other six had gone. Each pile is its own section with its own heading and its
    own count, so each counts from 1 and its last number is the count above it. The offset
    still advances: it is how `review_lede` knows the top of the list has been printed."""
    global _LIST_OFFSET
    _LIST_OFFSET += n
    return '<ol class="findings">'


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
    tip = f"{name} docs"
    if effort:
        tip += f" · filed at {effort} effort"
    # The face is the command, which says nothing about where the link goes or how hard
    # the pass looked; the tooltip spends itself on the first, as every other tooltip on
    # this page does, and the detail after the chip spends itself on the second, in plain
    # text rather than crowded into a face that is also a link.
    chip = (f'<a class="f-src" href="{html.escape(PASS_DOCS[name])}" target="_blank" '
            f'rel="noopener" data-tip="{html.escape(tip)}">{html.escape(name)}</a>')
    return chip + (f' <span class="f-src-detail">({html.escape(detail)})</span>' if detail else "")


#: Who said the observation, by the words of its `source:` — the label before it. Eval
#: run 11 printed `Reviewer:` over a line-length refusal whose source chip read
#: `pre-push hook`.
_OBS_LABELS = ((re.compile(r"\bhook\b", re.I), "Hook"),
               (re.compile(r"\blint(?:er)?\b", re.I), "Linter"),
               (re.compile(r"^\s*CI\b|\bSonar", re.I), "CI"))


def _obs_label(f) -> str:
    src = str(f.get("source") or "")
    return next((label for rx, label in _OBS_LABELS if rx.search(src)), "Reviewer")


_STOPWORDS = frozenset("a an the and or of to in on at by for with from is are was were be "
                       "it its this that each every now no not so as into".split())


def _words(text: str) -> set[str]:
    """Content words, cut to five letters so `wrapped` and `wraps` are one word."""
    return {w[:5] for w in re.findall(r"[a-z0-9]+", _plain_text(text).lower())
            if w not in _STOPWORDS}


def restates_title(fix: str, title: str) -> bool:
    """Does a `fix:` line say nothing its card's title has not? Then the card drops it.

    Three quarters of its content words in the title: `Fix: retry on a failed page` under
    *Retry on a failed owners page* is the title again (eval run 11's judges counted a
    `Fix:` line on every Fixed card among the prose the reference never had)."""
    said = _words(fix)
    return bool(said) and len(said & _words(title)) >= 0.75 * len(said)


def _raised_by(items, total: int) -> str:
    """`12 raised — 9 by /code-review, 3 by /simplify`: the review chip's hover.

    Counted off each item's own `source`, the same string the stamp beside it renders, so
    a reader who hovers the chip and then counts the stamps gets the same answer twice.
    Passes appear in the order the content file first mentions them.

    `source` is optional (see `_finding_source`), and an item without one is counted as
    itself rather than folded into whichever pass happens to be first — an unattributed
    finding is a real state, and a hover that hides it is a hover that lies by rounding.
    With nothing attributed at all the breakdown is dropped entirely: `12 raised, 12 of
    them unattributed` is the total said twice.

    Counted per reviewer, not per spelling of `source`. Run 6's items named several
    reviewers each (`reviewer correctness, reviewer ticket-fit`), and the hover listed every
    combination as a group of its own — the same reviewer in three of them. A source is
    split on commas, `+` and `and` (outside parentheses), the parenthesised detail that
    tells same-titled findings apart is dropped, and an item two reviewers raised counts
    once for each; the hover says how many were raised by more than one."""
    counts: dict[str, int] = {}
    shared = 0
    for it in items:
        names = _reviewers(it.get("source") or "")
        if len(names) > 1:
            shared += 1
        for name in names or [""]:
            counts[name] = counts.get(name, 0) + 1
    named = [f"{n} by {src}" for src, n in counts.items() if src]
    if not named:
        return f"{total} raised"
    if counts.get(""):
        named.append(f'{counts[""]} with no pass named')
    return (f"{total} raised — " + ", ".join(named)
            + (f" ({shared} raised by more than one reviewer)" if shared else ""))


def _reviewers(source: str) -> list[str]:
    """`reviewer correctness, reviewer ticket-fit` → both names, in order, once each;
    `/code-review high (the PUT scenario)` → `/code-review high`."""
    src = re.sub(r"\s*\([^()]*\)", "", source).strip()
    parts = re.split(r"\s*(?:,|\+|;|\band\b)\s*", src)
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def _finding_refs(f) -> str:
    """The bare `file:line` links — only when nothing else already carries them.

    An item that shows a snippet or a diff already links the file, with a line RANGE, from
    that block's own header. Repeating a bare `file:line` link above it says the same thing
    twice and worse."""
    if f.get("_snippets") or f.get("_diffs") or f.get("_fixDiffs"):
        return ""
    return "".join(_ref_link(r) for r in f.get("_refs", []))


def _ref_link(r) -> str:
    """`VetRestController.java`, linked to lines 96-100, with its path on hover.

    The same trade a diff header makes: a repo-relative Java path spends five segments on
    module, `src/main/java` and the org package before it reaches the one word that says
    which file this is, and a line of three such references is a wall no reader parses.
    The path is not dropped, it is moved to the tooltip — and a file at the repo root has
    no path to move, so it gets no tooltip repeating its own name.

    The face is the name alone: `VetRestController.java`, not `…:96-100`. The href still
    opens line 96; a number on the label is one nobody acts on (Victor, 9 Oct 2026)."""
    label = r["label"]
    rel, _, lines = label.rpartition(":")
    if not (rel and re.fullmatch(r"[\d,–-]+", lines)):
        rel = label
    name = Path(rel).name
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
    now count nothing twice.

    `open` leaves out the findings the agent refuted (`refuted_number`): eval run 8's
    header, counts line and tab pill all said "11 open" over four CONTEXT cards the page
    itself marked "refuted — …", so a reviewer was told to rule on seven live issues and
    four settled ones as if they were the same thing."""
    findings = [f for f in spec.get("findings", []) or [] if not is_refuted(f)]
    return (len(findings), len(spec.get("autofixes", [])),
            len(spec.get("assumptions", [])))


def is_refuted(item) -> bool:
    """`review-points.py:is_refuted` — the parser's own reading, so the page and the
    parser's warnings can never disagree about which item is a refuted claim."""
    return _points_parser().is_refuted(item)


def refuted_number(spec) -> int:
    """How many declined findings are refuted claims: counted apart from `open` so every
    summary can say `7 open · 4 refuted` rather than folding them in."""
    return sum(1 for f in spec.get("findings", []) or [] if is_refuted(f))


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
    # Copy pass (3 Oct 2026): the badge counts the open pile and says so — nothing more.
    # Which piles it leaves out, and where they sit, is what the tab itself shows a line
    # down; listing them here was a paragraph on a two-digit number.
    label = f'{open_n} open review issue{"" if open_n == 1 else "s"}'
    return {"count": open_n, "label": label}


#: Kept only because `build-review-html.py` imports it by name. It used to be the width
#: at which the chip's third number stopped fitting and was cut whole — back when that
#: number rode along as `, N assumptions` on the same clause as the fixes. The third pile
#: is no longer an optional extra on the review's own sentence: it is a second sentence,
#: about a different agent, and a chip that drops it for want of two characters drops the
#: only trace on the masthead of what the coder guessed at. Nothing measures the face
#: against this any more.
SCOPE_CHIP_MAX_LEN = 34


def scope_chip_face(spec, reviewer: str | None = None) -> str:
    """`\U0001f916Code: <b>7 unsure</b>; \U0001f916Review: <b>6 open</b>, <b>3 fixed</b>`
    — the masthead's review chip, whole, off the same `pile_numbers` the counts line
    under the header reads, so the two cannot drift.

    Two agents, two clauses, one typography: each clause is `Agent: counts`, the coder's
    first because its assumptions were made before the review ran. `unsure` is the
    coder's own pile in one word — what it had to guess at — where the counts line has
    the width for `implementation assumptions`. Every count is bold with what it counts.
    With no assumptions the coder's clause is absent rather than zeroed. The reviewer's
    model is not on the face (`reviewer` is accepted and ignored here): the pill names
    the role, the hover names the model."""
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    face = f"\U0001f916Review: <b>{open_n} open</b>, <b>{fixed_n} fixed</b>"
    if assumed_n:
        face = f"\U0001f916Code: <b>{assumed_n} unsure</b>; " + face
    return face


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
//
// Before any heading has got there -- the page as it opens, scrolled to the top -- the
// first chapter is the one lit, not none. The reader is about to read it; a row with no
// mark at all read as broken until the first scroll lit it up.
function paint(){
  var t=triggerY(), current=null, first=null, firstTop=Infinity;
  pairs.forEach(function(p){
    var margin=parseFloat(getComputedStyle(p.el).scrollMarginTop)||0;
    var top=p.el.getBoundingClientRect().top;
    if(top<firstTop){firstTop=top;first=p.id;}
    if(top<=Math.max(t,margin)+1)current=p.id;
  });
  if(!current)current=first;
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


#: Where a short clause ends inside a verdict bullet: the first stop, colon, semicolon,
#: comma or dash. The verdict's bullets are paragraphs written for the band that used to
#: sit under the score; their first clause is the claim, the rest is the evidence for it.
_CLAUSE_END = re.compile(r"(?:[.:;,]\s|\s[\u2014\u2013-]\s|[.:;]$)")


#: Past this many characters a bullet with no clause boundary outside code is cut at a word
#: instead, with an ellipsis — the hover keeps the whole of it.
CLAUSE_CAP = 120

#: What a clause boundary may never sit inside: a `<code>` span (or a backticked one the
#: tags were stripped from), and an open brace, bracket or parenthesis.
_CODE_SPAN = re.compile(r"<code\b[^>]*>.*?</code>|`[^`]*`", re.S)
_OPENERS, _CLOSERS = "([{", ")]}"


def _first_clause(text: str) -> str:
    """`No build proved this commit` out of `No build proved this commit: <code>…</code>
    failed for … .` — tags stripped, entities kept, cut at the first clause boundary
    that sits outside code.

    Run 6 cut `GET /api/owners changes shape from an array to <code>{content,
    totalElements}</code>: backend and frontend must ship…` at the comma inside the code
    span, and the grade panel read `…from an array to {content`. A boundary inside a code
    span or an open brace/bracket/parenthesis is not one; with no boundary outside them
    the whole sentence stands, cut at a word only past `CLAUSE_CAP`."""
    src = text or ""
    plain, guarded = [], []
    pos = 0
    for m in _CODE_SPAN.finditer(src):
        before = html.unescape(re.sub(r"<[^>]+>", "", src[pos:m.start()]))
        inside = html.unescape(re.sub(r"<[^>]+>", "", m.group(0))).strip("`")
        plain.append(before)
        guarded.extend([False] * len(before))
        plain.append(inside)
        guarded.extend([True] * len(inside))
        pos = m.end()
    tail = html.unescape(re.sub(r"<[^>]+>", "", src[pos:]))
    plain.append(tail)
    guarded.extend([False] * len(tail))
    whole = "".join(plain)
    lead = len(whole) - len(whole.lstrip())
    depth = 0
    nested = []
    for ch, g in zip(whole, guarded):
        if not g and ch in _OPENERS:
            depth += 1
        nested.append(g or depth > 0)
        if not g and ch in _CLOSERS and depth:
            depth -= 1
    cut = next((m.start() for m in _CLAUSE_END.finditer(whole)
                if m.start() > lead and not nested[m.start()]), None)
    out = (whole[:cut] if cut is not None else whole).strip().rstrip(".")
    if len(out) > CLAUSE_CAP:
        out = out[:CLAUSE_CAP].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    return out


#: Where `preflight.py` leaves the CI gate's verdict on the commit under review.
GATE_JSON = ".gate.json"
#: The API tab's verdict band, as `openapi-compat.py` wrote it.
API_VERDICT_HTML = "assets/openapi-verdict.html"
#: The two producers that leave a verdict when their tab could not be measured this run.
SEQUENCE_VERDICT_JSON = "assets/sequence.verdict.json"
FILM_VERDICT_JSON = "assets/feature.verdict.json"
#: How many lines of its own the content file's `verdict` may add under the computed ones.
MODEL_GRADE_LINES = 2
#: The most lines the grade panel shows, computed and the model's together. Eval run 11
#: printed nine against the reference's five, and the grade box stretched to match.
GRADE_LINES_MAX = 6
#: Computed lines that inform rather than grade — no cap, nothing to act on in the panel
#: itself — dropped from the panel, last first, when it would run past `GRADE_LINES_MAX`.
#: The spec commit is still on the branch chip's `+N` list.
SPARE_SIGNALS = ("narrowed", "spec-commit")

#: The highest grade each signal allows. A grade is the model's number, lowered to the
#: lowest ceiling any signal on the page sets — never raised. One table, so the reader can
#: be told in one hover why an 8 became a 6, and a reviewer can argue with a row of it.
GRADE_CAPS = {
    "ci-failed": 4,         # CI ran on the reviewed commit and failed
    "ci-unproven": 6,       # no CI run proved the reviewed commit (skipped, cancelled, none)
    "open-high": 5,         # an open review issue the reviewer filed as `high`
    "api-breaking": 7,      # the REST contract breaks a client
    "no-evidence": 7,       # one tab carries a reason instead of this run's evidence
    "no-evidence-2": 6,     # two or more do
    "after-review": 7,      # code moved on the branch after the review was recorded
    "out-of-range": 7,      # commits in the PR that the review never read
}


def _plain_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git_out(root: Path | None, *args: str) -> str | None:
    """`git <args>` in `root`, stdout stripped — or None when it fails or there is no root."""
    if root is None:
        return None
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_root(out_dir: Path) -> Path | None:
    top = _git_out(Path(out_dir), "rev-parse", "--show-toplevel") if Path(out_dir).is_dir() \
        else None
    return Path(top) if top else None


def _signal(key: str, short: str, full: str, cap: int | None = None) -> dict:
    return {"key": key, "short": short, "full": full, "cap": cap}


def _ci_signal(out_dir: Path) -> dict | None:
    """What CI said about the reviewed commit — green or not, either way on the panel.

    Silence about CI read as "nothing to worry about" on a page whose CI was green, and as
    the same on a page whose CI never ran. No `.gate.json` at all (a page from before the
    gate, or a test) says nothing rather than guessing."""
    gate = _read_json(Path(out_dir) / GATE_JSON)
    if not isinstance(gate, dict) or not gate.get("verdict"):
        return None
    sha = str(gate.get("sha") or "")[:8]
    caveat = _plain_text(str(gate.get("caveat") or ""))
    runs = [w for w in gate.get("workflows") or [] if isinstance(w, dict)]
    def linked(sig: dict, run: dict) -> dict:
        # The run is the evidence, so it is a link a reader can click — eval run 8 had the
        # URL only in the hover, where "CI green" had to be taken on trust.
        url = str(run.get("url") or "")
        if url.startswith(("https://", "http://")):
            sig.update(href=url, linkText=f"{run.get('name') or 'CI'} run"
                       + (f" {run['runId']}" if run.get("runId") else "") + " \u2197")
        return sig
    if gate["verdict"] == "green":
        run = next((w for w in runs if w.get("runId")), {})
        # No hover: the face says green and links the run.
        return linked(_signal("ci-green", f"CI green on {sha}" if sha else "CI green", ""), run)
    if any(w.get("verdict") == "failure" for w in runs) or gate["verdict"] == "failure":
        run = next((w for w in runs if w.get("verdict") == "failure"), None) \
            or next((w for w in runs if w.get("runId")), {})
        return linked(_signal("ci-failed", f"CI failed on {sha}" if sha else "CI failed",
                              caveat or "a CI workflow concluded failure on the reviewed commit",
                              GRADE_CAPS["ci-failed"]), run)
    return _signal("ci-unproven", "No build proved this commit",
                   caveat or f"the CI gate says {gate['verdict']!r}, not green",
                   GRADE_CAPS["ci-unproven"])


def _api_signal(out_dir: Path) -> dict | None:
    """The API tab's own verdict band, read rather than recomputed: red means breaking."""
    try:
        band = (Path(out_dir) / API_VERDICT_HTML).read_text(encoding="utf-8")
    except OSError:
        return None
    if 'class="apiverdict red"' not in band:
        return None
    text = _plain_text(re.sub(r"<style>.*?</style>", "", band, flags=re.S))
    text = re.sub(r"\s+", " ", re.sub(r"\(report\s*\u2197\)", "", text)).strip()
    # The band counts endpoints, not changes, since 7 Oct 2026 ("1 endpoint broken"), and
    # so does this line: the two side by side must not print two different numbers.
    m = re.search(r"(\d+)\s+endpoints?\s+broken", text)
    n = int(m.group(1)) if m else 0
    short = (f"{n} API endpoint{'' if n == 1 else 's'} broken" if n
             else "The API contract breaks")
    # No hover: the API tab is the detail, and its verdict line restated here named the
    # two differs — tooling, not a fact about the change.
    return _signal("api-breaking", short, "", GRADE_CAPS["api-breaking"])


def _evidence_signal(spec, out_dir: Path) -> dict | None:
    """The tabs that carry a reason instead of evidence from this run, as one line.

    Read off the verdicts their producers leave — the Sequence tab's when the traced
    suites drew nothing or were red, the Demo tab's when the film did not complete. The
    C2 view on Structure is projected from the sequence diagrams, so a Sequence tab that
    was not re-traced takes it along."""
    names = []
    detail = []
    seq = _read_json(Path(out_dir) / SEQUENCE_VERDICT_JSON)
    if isinstance(seq, dict) and seq.get("state") in ("skipped", "red"):
        what = ("not re-traced on this run" if seq["state"] == "skipped"
                else "the traced suite was red")
        names.append("Sequence")
        c2 = (Path(out_dir) / "assets" / "c2").is_dir()
        detail.append(f"Sequence: {what}"
                      + (" — and the C2 view on Structure is drawn from those same "
                         "diagrams" if c2 else ""))
    film = _read_json(Path(out_dir) / FILM_VERDICT_JSON)
    if isinstance(film, dict) and film.get("exit"):
        names.append("Demo")
        detail.append("Demo: " + {3: "the feature did not hold on film",
                                  2: "nothing was filmed"}.get(
            film.get("exit"), f"the recorder failed (exit {film.get('exit')})"))
    if not names:
        return None
    n = len(names)
    short = (f"{'One tab carries' if n == 1 else f'{n} tabs carry'} a reason instead of "
             f"evidence: {', '.join(names)}")
    return _signal("no-evidence", short, "; ".join(detail),
                   GRADE_CAPS["no-evidence" if n == 1 else "no-evidence-2"])


def _base_ref(spec, root: Path | None) -> str | None:
    """The branch the PR merges into, as a ref this clone can resolve."""
    base = str((spec.get("pr") or {}).get("base") or "main")
    for ref in (base if base.startswith("origin/") else f"origin/{base}", base):
        if _git_out(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    return None


def commits_after_review(out_dir: Path, root: Path | None) -> dict | None:
    """What was added to the branch after the reviewed commit, for the masthead's `N↑`.

    `{"review": <short>, "commits": [{"short", "when", "subject"}, …]}`, newest first, read
    off git as `git log --first-parent --no-merges <review>..HEAD` — no judgement of what
    changed, no generated/tooling split. None when there is no recorded review commit or
    nothing came after it."""
    doc = _read_json(Path(out_dir) / AFTERMATH_JSON)
    review = doc.get("review") if isinstance(doc, dict) else None
    if not review or root is None:
        return None
    out = _git_out(root, "log", "--first-parent", "--no-merges", "--date=format:%d %b %H:%M",
                   "--format=%h%x09%ad%x09%s", f"{review}..HEAD")
    rows = [ln.split("\t", 2) for ln in (out or "").splitlines() if ln.count("\t") >= 2]
    if not rows:
        return None
    return {"review": review[:8],
            "commits": [{"short": a, "when": b, "subject": c} for a, b, c in rows]}


def _after_review_signal(out_dir: Path, root: Path | None, base_ref: str | None) -> dict | None:
    """Retired: the grade panel no longer says `N commits landed after the review` (Victor,
    7 Oct 2026) — the count is the masthead branch chip's `N↑`. Kept as a no-op so the
    signal list and its `after-review` ranking stay as they were."""
    return None


def _out_of_range_signal(spec, root: Path | None, base_ref: str | None,
                        out_dir: Path | None = None) -> dict | None:
    """Commits the PR carries that sit before the range the reviewers read.

    The review reads `audited-base..implementation`; the PR is everything since it left
    its base. When the review started later than that, the commits in between are in the
    diff a merge would ship, and nobody read them. With no pull request open they are
    *on the branch*: run 6 said "in the PR" on a branch that had none, beside a header
    that correctly showed no PR number."""
    prov = ((spec.get("_reviewPoints") or {}).get("provenance") or {})
    audited = prov.get("auditedBase") or prov.get("base")
    rows = [f"{c['sha'][:8]} {c['subject']}"
            for c in _before_range_commits(spec, root, base_ref) if not c["spec"]]
    if not rows:
        return None
    n = len(rows)
    where = "in the PR" if out_dir is not None and pr_exists(spec, Path(out_dir)) \
        else "on the branch"
    return _signal("out-of-range",
                   f"{n} commit{'' if n == 1 else 's'} {where} before the reviewed range",
                   "Never reviewed: " + "; ".join(rows[:3]) + (" …" if n > 3 else ""),
                   GRADE_CAPS["out-of-range"])


def _before_range_commits(spec, root: Path | None, base_ref: str | None) -> list[dict]:
    """The branch's commits between its fork point and the audited base, newest first:
    `{sha, subject, spec}` — `spec` names `openspec/changes/<change>/` when the commit
    wrote the change this branch implements. A commit the base already carries under
    another sha (`git cherry`) is left out.

    Eval run 10 listed b12c9bdb — the OpenSpec proposal, design and spec of this very
    change — among tooling commits as "never reviewed". It is the spec the change was
    built against: said as that, linked, and never counted against the grade."""
    prov = ((spec.get("_reviewPoints") or {}).get("provenance") or {})
    audited = prov.get("auditedBase") or prov.get("base")
    if not (root and base_ref and audited):
        return []
    mb = _git_out(root, "merge-base", base_ref, "HEAD")
    if not mb or not _git_out(root, "merge-base", "--is-ancestor", mb, audited) == "":
        return []
    listed = _git_out(root, "log", "--no-merges", "--format=%H%x1f%s", f"{mb}..{audited}")
    change = _spec_change_dir(root, spec)
    # Eval run 11: 5 of "7 commits never reviewed" were already on origin/main, cherry-
    # picked under other shas. A merge brings none of them, so none of them is counted.
    from ..shared.chips import patch_equivalent
    picked = patch_equivalent(root, base_ref, audited, mb)
    out = []
    for line in (listed or "").splitlines():
        sha, _, subject = line.partition("\x1f")
        if not sha.strip() or sha in picked:
            continue
        touched = _git_out(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
                           "--root", sha, "--", f"{change}/") if change else ""
        out.append({"sha": sha, "subject": subject, "spec": change if touched else None})
    return out


def _spec_commit_signals(spec, root: Path | None, base_ref: str | None) -> list[dict]:
    """`Built against the spec in b12c9bdb` — a reason line per commit before the reviewed
    range that wrote this change's OpenSpec documents, linked to it on GitHub. No cap:
    reading the spec was never the review's job."""
    found = [c for c in _before_range_commits(spec, root, base_ref) if c["spec"]]
    if not found:
        return []
    gh = github_blob_base(Path(root)) if root else None
    out = []
    for c in found:
        sig = _signal("spec-commit", f"Built against the spec in {c['sha'][:8]}",
                      f"{c['sha'][:8]} {c['subject']} — wrote {c['spec']}/, the OpenSpec "
                      "change this branch implements, before the reviewed range: the spec "
                      "the change was built against, not unreviewed code")
        if gh:
            sig.update(href=f"{gh}/commit/{c['sha']}", linkText=f"{c['spec']}/")
        out.append(sig)
    return out


#: Where `semcov.py` leaves the Tests tab's sentence-to-test mapping, merged copy first;
#: and the matrix fragment, the one place the ticket's sentences are kept as text.
TEST_MAPPING_FILES = ("assets/test-mapping.merged.json", "test-mapping.json")
REQMAP_HTML = "assets/requirements-map.html"
#: How many narrowed sentences get a line of their own before the rest are one line.
NARROWED_LINES = 2
NARROWED_QUOTE = 70


def _quote(text: str, limit: int = NARROWED_QUOTE) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"


def _decision_link(entry: dict, root: Path | None) -> tuple[str, str, str] | None:
    """`(face, quote, href)` of the recorded decision a narrowed sentence rests on.

    `decisionRef` when `semcov.py` wrote one (`where`, `quote`, `path`, `href`); otherwise
    out of the decision's own text — `Scope (<change>/<file>): <quote>` names an OpenSpec
    file, and the quote is looked up in it for the line. A link to github.com when the
    clone has one, so the panel works off the reviewer's machine too; else the editor."""
    ref = dict(entry.get("decisionRef") or {})
    text = str(entry.get("decisionText") or "")
    m = re.match(r"^Scope \(([^)/]+)/([^)]+)\):\s*(.*)$", text, re.S)
    if m and not ref.get("path"):
        ref.setdefault("path", f"openspec/changes/{m[1]}/{m[2]}")
        ref.setdefault("quote", m[3].strip())
    quote = str(ref.get("quote") or re.sub(r"^\w+(?: \([^)]*\))?:\s*", "", text)).strip()
    path, line = ref.get("path"), None
    where = str(ref.get("where") or "")
    wm = re.search(r":(\d+)$", where)
    if wm:
        line = int(wm[1])
    if path and root is not None and line is None:
        try:
            lines = (Path(root) / path).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        probe = quote[:60].strip()
        line = next((i for i, ln in enumerate(lines, 1) if probe and probe in ln), None)
    if path and not where:
        where = Path(path).name + (f":{line}" if line else "")
    if not where:
        return None
    href = ref.get("href") or ""
    gh = github_blob_base(Path(root)) if (root is not None and path) else None
    head = _git_out(root, "rev-parse", "HEAD") if gh else None
    if gh and head:
        href = f"{gh}/blob/{head}/{path}" + (f"#L{line}" if line else "")
    elif path and root is not None and not href:
        href = f"vscode://file/{(Path(root) / path).resolve()}" + (f":{line}:1" if line else "")
    return where, quote, href


def _narrowed_signals(out_dir: Path, root: Path | None) -> list[dict]:
    """A ticket sentence the Tests tab marks *narrowed* — cut by a recorded decision — as a
    reason on the grade panel, naming and linking that decision.

    Run 6's biggest scope cut ("sortable by any column", delivered for Name and City,
    recorded in the OpenSpec proposal) reached the page only as a hatched sentence on the
    Tests tab. A sentence of the ticket not delivered as written is a fact about the
    change, so the grade's reasons carry it; it does not lower the grade — it was decided,
    and the decision is the link. Read-only use of `semcov.py`'s mapping."""
    doc = None
    for name in TEST_MAPPING_FILES:
        doc = _read_json(Path(out_dir) / name)
        if isinstance(doc, dict):
            break
    entries = [e for e in (doc or {}).get("sentences") or []
               if isinstance(e, dict) and e.get("coverage") == "narrowed"] \
        if isinstance(doc, dict) else []
    if not entries:
        return []
    try:
        matrix = (Path(out_dir) / REQMAP_HTML).read_text(encoding="utf-8")
    except OSError:
        matrix = ""

    def sentence(e: dict) -> str:
        m = re.search(r'data-s="' + re.escape(str(e.get("id"))) + r'"[^>]*>(.*?)</span>',
                      matrix, re.S)
        return _plain_text(m[1]) if m else ""
    out = []
    for e in entries[:NARROWED_LINES]:
        said = sentence(e)
        dec = _decision_link(e, root)
        short = (f"Ticket narrowed on purpose: “{_quote(said)}”" if said
                 else "A ticket sentence was narrowed on purpose")
        full = " ".join(x for x in (
            f"Not delivered as written: “{said}”." if said else "",
            str(e.get("gap") or ""),
            f"Decided in {dec[0]}: “{dec[1]}”" if dec and dec[1] else
            (f"Decided in {dec[0]}" if dec else "")) if x)
        sig = _signal("narrowed", short, full or short)
        if dec and dec[2]:
            sig.update(href=dec[2], linkText=dec[0])
        out.append(sig)
    rest = len(entries) - NARROWED_LINES
    if rest > 0:
        out.append(_signal("narrowed",
                           f"{rest} more ticket sentence{'' if rest == 1 else 's'} narrowed "
                           "on purpose", "Listed, with their decisions, on the Tests tab"))
    return out


#: The signals `_pile_signals` produces, which `grade_reasons` recounts at render time.
PILE_SIGNALS = ("open", "open-high", "assumptions")


def _pile_signals(spec) -> list[dict]:
    """The open pile by severity and the unconfirmed assumptions — the two signals a spec
    carries on its own, with no build around it."""
    out = []
    # A refuted claim is not an open issue, so it neither counts nor grades (eval run 8).
    findings = [f for f in spec.get("findings") or [] if isinstance(f, dict)
                and not is_refuted(f)] if isinstance(spec.get("findings"), list) else []
    if findings:
        by: dict[str, int] = {}
        for f in findings:
            by[f.get("severity", "info")] = by.get(f.get("severity", "info"), 0) + 1
        split = ", ".join(f"{by[k]} {SEVERITIES[k][1]}{'s' if k == 'low' and by[k] > 1 else ''}"
                          for k in ("high", "medium", "low", "info") if by.get(k))
        n = len(findings)
        out.append(_signal(
            "open-high" if by.get("high") else "open",
            f"{n} open review issue{'' if n == 1 else 's'}: {split}",
            "",  # no hover: the cards are right below, titles and all
            GRADE_CAPS["open-high"] if by.get("high") else None))
    assumed = [a for a in spec.get("assumptions") or [] if isinstance(a, dict)] \
        if isinstance(spec.get("assumptions"), list) else []
    if assumed:
        unsure = sum(1 for a in assumed
                     if isinstance(a.get("confidence"), (int, float)) and a["confidence"] < .7)
        n = len(assumed)
        out.append(_signal(
            "assumptions",
            f"{n} implementation assumption{'' if n == 1 else 's'} unconfirmed"
            + (f", {unsure} under 70% sure" if unsure else ""),
            # No hover. It said "the last pile below" over the tab's FIRST pile.
            ""))
    return out


def grade_signals(spec, out_dir: Path, root: Path | None = None) -> list[dict]:
    """What the page measured that bears on the grade, one dict per reason, in the order
    the panel prints them — and, as `spec["_gradeSignals"]`, what `grade_reasons` reads.

    Every reason is computed: CI, the open pile by severity, the unconfirmed assumptions,
    a breaking API change, tabs left without evidence, code that moved after the review,
    commits the review never read. A content file's verdict adds at most
    `MODEL_GRADE_LINES` lines under these (`grade_reasons`), and its number is lowered to
    the lowest ceiling the signals set (`cap_grade`)."""
    root = root if root is not None else _git_root(out_dir)
    base_ref = _base_ref(spec, root) if root else None
    out = []
    ci = _ci_signal(out_dir)
    if ci:
        out.append(ci)
    out.extend(_pile_signals(spec))
    for sig in (_api_signal(out_dir), _evidence_signal(spec, out_dir),
                _after_review_signal(out_dir, root, base_ref),
                _out_of_range_signal(spec, root, base_ref, out_dir)):
        if sig:
            out.append(sig)
    out.extend(_spec_commit_signals(spec, root, base_ref))
    out.extend(_narrowed_signals(out_dir, root))
    spec["_gradeSignals"] = out
    return out


def cap_grade(spec) -> int | None:
    """Lower `verdict.score` to the lowest ceiling a computed signal sets, in place.

    The model's own number is kept as `verdict.modelScore` so the panel can say what it was
    and which signals brought it down. Never raises a grade: the ceilings say how good a
    page with this evidence can be, not how good it is. Returns the ceiling that bound, or
    None when the model's number stands."""
    v = spec.get("verdict")
    if not isinstance(v, dict) or "score" not in v:
        return None
    caps = [s["cap"] for s in spec.get("_gradeSignals") or [] if s.get("cap")]
    if not caps:
        return None
    model = int(v.get("modelScore", v["score"]))
    ceiling = min(caps)
    if ceiling >= model:
        return None
    v["modelScore"] = model
    v["score"] = ceiling
    return ceiling


def grade_reasons(spec) -> list[tuple[str, str]]:
    """`[(short, full), …]` — why the score is what it is, in a few words each
    (`_grade_rows` without the links).

    The computed signals first (`grade_signals`, or — for a spec that never went through
    it — the two piles counted here), then at most `MODEL_GRADE_LINES` of the content
    file's own: `verdict.why` if it has one, else `verdict.bullets`, each cut to its first
    clause with the whole kept for the hover. A model used to write the whole list, and
    run 5 shipped a green 8/10 whose two reasons were counts — nothing about the breaking
    API change, the tab nobody re-traced, or the CI run. The page states what it measured;
    the model gets two lines for what it alone knows."""
    return [(short, full) for short, full, _ in _grade_rows(spec)]


def _grade_rows(spec) -> list[tuple[str, str, tuple[str, str] | None]]:
    """`[(short, full, (href, face) or None), …]` — `grade_reasons`, plus the link a
    signal carries to what it rests on (a narrowed ticket sentence → the decision)."""
    v = spec.get("verdict") or {}
    # The piles are counted here, at render time, not off the list the build computed:
    # the build drops unanchored assumptions after the signals were taken, and the panel
    # has to count the pile the reader sees under it.
    measured = [s for s in spec.get("_gradeSignals") or [] if s["key"] not in PILE_SIGNALS]
    signals = ([s for s in measured if s["key"].startswith("ci-")] + _pile_signals(spec)
               + [s for s in measured if not s["key"].startswith("ci-")])
    def head(b: str) -> str:
        # A `why` line is written to be short and kept whole unless it runs long; a
        # `bullet` is a paragraph, and its first clause is the claim.
        return (_first_clause(b) if not v.get("why") or len(_plain_text(b)) > 80
                else _plain_text(b))
    own = [b for b in (v.get("why") or v.get("bullets") or [])
           if not _drop_model_line(b, spec, head(b))]
    # The grader's own reason keeps one line: eval runs 15, 17 and 18 dropped it to fit
    # six computed lines, and the one risk behind the grade was missing from its box.
    budget = GRADE_LINES_MAX - (1 if own else 0)
    if len(signals) > budget:
        spare = [s for s in signals if s["key"] in SPARE_SIGNALS]
        room = max(0, budget - (len(signals) - len(spare)))
        spare.sort(key=lambda s: SPARE_SIGNALS.index(s["key"]))
        cut = spare[room:]
        signals = [s for s in signals if not any(s is c for c in cut)]
    model_score = v.get("modelScore")
    out = []
    for s in signals:
        short = s["short"]
        if s.get("cap") and model_score is not None and s["cap"] < model_score:
            short += f" (caps the grade at {s['cap']})"
        link = (s["href"], s.get("linkText") or "source") if s.get("href") else None
        out.append((short, s.get("full") or short, link))
    room = max(1 if own else 0, min(MODEL_GRADE_LINES, GRADE_LINES_MAX - len(out)))
    if len(own) > room:
        print(f"[review] verdict carries {len(own)} lines of its own; the grade panel shows "
              f"the first {room} — the rest of its reasons are computed", file=sys.stderr)
    for b in own[:room]:
        short = head(b)
        if short:
            out.append((short, _plain_text(b), None))
    return out


#: A count a model line may state about a pile the page counts itself: `6 assumptions`,
#: `10 open review issues`, `3 refuted`, `6 fixes`.
_PILE_COUNT = re.compile(
    r"\b(\d+)\s+(open\s+)?(?:LLM\s+)?(?:review\s+)?(?:implementation\s+)?"
    r"(assumptions?|issues?|findings?|fixes|fixed|auto-fixed|refuted)\b", re.I)
#: A Q&A question a model line names, alone or as a range (`Q3`, `Q5–Q15`).
_Q_REF = re.compile(r"\bQ(\d+)(?:\s*[–-]\s*Q?(\d+))?\b")


#: A model line that says again what a computed line on the panel already says, by the
#: signal it repeats: run 11's `GET /api/owners now answers {content…}` under
#: `2 breaking API changes`. Read off the line's visible head.
_REPEATS = {
    "api-breaking": re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/|\bbreak(?:s|ing)\b"
                               r"|\bAPI\b|\bcontract\b"),
    "ci-": re.compile(r"\bCI\b|\bbuild (?:is |was )?(?:green|red|passed|failed)", re.I),
    "no-evidence": re.compile(r"\bre-?traced\b|\bnot filmed\b|\bno evidence\b", re.I),
    "after-review": re.compile(r"\b(?:landed|committed|pushed) after the review", re.I),
    "out-of-range": re.compile(r"\bbefore the review(?:ed)? range\b|\bnever reviewed\b",
                               re.I),
    "narrowed": re.compile(r"\bnarrow", re.I),
    "spec-commit": re.compile(r"\bbuilt against the spec\b", re.I),
}
#: A word that puts items in a pile, and the pile it puts them in: `None` for the words
#: the page never uses — a declined item is shown as OPEN, so calling it declined plays it
#: down (run 11: "Two declined items are product calls" over two open WORTH A LOOK cards).
_PILE_WORD = re.compile(r"\b(?:(not|never|un)[\s-]*)?(declined|ignored|dismissed|rejected|"
                        r"auto-fixed|fixed|refuted)\b", re.I)
_PILE_OF_WORD = {"declined": None, "ignored": None, "dismissed": None, "rejected": None,
                 "fixed": "fixed", "auto-fixed": "fixed", "refuted": "refuted"}
_PILE_SAID = {"open": "open", "refuted": "refuted", "fixed": "fixed",
              "assumptions": "an assumption"}


def _plain_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _plain_text(text).lower())


def named_items(text: str, spec) -> list[tuple[str, str]]:
    """`[(pile, title), …]` — the items a line names: four words of a title in a row (the
    whole title when it is shorter). `pile` is where the page shows the item: `open`,
    `refuted`, `fixed`, `assumptions`."""
    said = " " + " ".join(_plain_words(text)) + " "
    out = []
    for kind in ("findings", "autofixes", "assumptions"):
        for item in spec.get(kind) or []:
            if not isinstance(item, dict):
                continue
            words = _plain_words(str(item.get("title") or ""))
            if not words:
                continue
            grams = ([words] if len(words) < 4 else
                     [words[i:i + 4] for i in range(len(words) - 3)])
            if any(f" {' '.join(g)} " in said for g in grams):
                pile = {"autofixes": "fixed", "assumptions": "assumptions"}.get(kind) or (
                    "refuted" if is_refuted(item) else "open")
                out.append((pile, _plain_text(str(item.get("title")))))
    return out


def model_line_conflict(text: str, spec, short: str | None = None) -> str | None:
    """Why a content file's verdict line contradicts the record the page renders, or None.

    The rule: a model line may say what only the model knows, but it may not restate a
    number the page computes — and when it does, the number has to be the page's. Two
    checks, both against the piles as rendered:

    * a count of a pile (`6 assumptions`, `10 open issues`, `3 refuted`, `6 fixes`) must
      be that pile's count (`pile_numbers`, `refuted_number`);
    * a line that ties Q&A question numbers to the assumptions (`Q5–Q15 … the
      assumptions below are the coder's answers to them`) needs at least one assumption
      on the page that cites one of those questions.

    Eval run 10's hand-typed bullet did the second: Q&A.md says the planner adopted the
    Q5–Q15 answers, and none of the six assumptions is about any of them — the page
    printed it as a reason for the grade."""
    plain = _plain_text(text)
    open_n, fixed_n, assumed_n = pile_numbers(spec)
    refuted_n = refuted_number(spec)
    for m in _PILE_COUNT.finditer(plain):
        n, what = int(m[1]), m[3].lower()
        if what.startswith("assumption"):
            ok = {assumed_n}
        elif what == "refuted":
            ok = {refuted_n}
        elif what in ("fixes", "fixed", "auto-fixed"):
            ok = {fixed_n}
        elif m[2]:                   # `N open issues`: the open pile
            ok = {open_n}
        else:                        # `N findings` may count a subset; not checked
            continue
        if n not in ok:
            return f"it says {m[0]!r}; the page counts {min(ok)}"
    qs: set[int] = set()
    for m in _Q_REF.finditer(plain):
        lo, hi = int(m[1]), int(m[2] or m[1])
        qs |= set(range(lo, hi + 1)) if 0 < hi - lo < 100 else {lo}
    if qs and re.search(r"\bassum|\bcoder", plain, re.I):
        cited = set()
        for a in spec.get("assumptions") or []:
            if isinstance(a, dict):
                said = " ".join(_plain_text(str(a.get(k) or ""))
                                for k in ("title", "why", "alternative", "body"))
                cited |= {int(q) for q in re.findall(r"\bQ(\d+)\b", said)}
        if not cited & qs:
            names = ", ".join(f"Q{q}" for q in sorted(qs)[:3]) + ("…" if len(qs) > 3 else "")
            return (f"it ties {names} to the assumptions, and no assumption on the page "
                    "cites any of those questions")
    # Eval run 11: three more ways a model line spends a busy reader's attention.
    head = _plain_text(short if short is not None else _first_clause(text))
    # (a) It says again what a computed line on the panel says.
    keys = [s["key"] for s in spec.get("_gradeSignals") or []]
    for key, rx in _REPEATS.items():
        if any(k == key or (key.endswith("-") and k.startswith(key)) for k in keys) \
                and rx.search(head):
            return f"it repeats the computed {key.rstrip('-')} line"
    for m in _PILE_COUNT.finditer(head):
        what = m[3].lower()
        if what.startswith("assumption") or (m[2] and what.startswith(("issue", "finding"))):
            return f"it repeats the computed count {m[0]!r}"
    # (b) It names nothing a reader could go and look at: no number, no code, no link, no
    # path, no item of the piles.
    named = named_items(text, spec)
    if not (named or re.search(r"\d|<code\b|<a\b|`|/\w", text)):
        return "it names no item, number, file or link"
    # (c) A pile word that is not where the items it names are.
    for m in _PILE_WORD.finditer(plain):
        word = m[2].lower()
        pile = None if m[1] else _PILE_OF_WORD[word]
        wrong = [(p, t) for p, t in named if p != pile]
        if wrong:
            p, t = wrong[0]
            return (f"it calls {t[:50]!r} {m[0].lower()}, and the page shows it "
                    f"{_PILE_SAID[p]}")
    return None


def _drop_model_line(text: str, spec, short: str | None = None) -> bool:
    """`model_line_conflict`, said once per build on stderr when it drops a line."""
    why = model_line_conflict(text, spec, short)
    if not why:
        return False
    said = spec.setdefault("_droppedModelLines", set())
    if text not in said:
        said.add(text)
        print(f"[review] WARNING: verdict line dropped from the grade panel — {why}: "
              f"{_plain_text(text)[:90]!r}", file=sys.stderr)
    return True


# --------------------------------------------------------------------------------------- #
# The PR's own description, as its author wrote it on GitHub (Victor, 8 Oct 2026: "we miss
# something very important"). Every other line of this tab is the review's reading of the
# change; this is the one place the page says what the change claims to be, in its
# author's words, before the reader is told what was found in it.
# --------------------------------------------------------------------------------------- #

PR_BODY_JSON = "pr-body.json"
_PULL_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
#: The attribution line an agent appends to every PR it opens: the same words on every
#: page, about the tool rather than the change.
_PR_BODY_BOILERPLATE = re.compile(r"^\s*🤖 Generated with \[Claude Code\]\([^)]*\)\s*$", re.M)


def attach_pr_body(spec: dict, out_dir: Path, root: Path | None = None) -> None:
    """Put the PR's description on `spec["_prBody"]`: asked of GitHub (`gh pr view`) at
    every build, so an edit on GitHub shows at the next refresh, and written down in
    `pr-body.json` so a build with no network still has the last one it saw. Nothing when
    the content file names no PR. `HR_NO_GITHUB` keeps the test suite off the network."""
    pr = spec.get("pr") or {}
    m = _PULL_URL.search(str(pr.get("url") or ""))
    num = pr.get("number") or (int(m.group(2)) if m else None)
    if not num:
        return
    cache = out_dir / PR_BODY_JSON
    try:
        got = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    if str(got.get("number")) != str(num):
        got = {}
    if not os.environ.get("HR_NO_GITHUB"):
        args = ["gh", "pr", "view", str(num), "--json", "number,body,url"]
        if m:
            args += ["-R", m.group(1)]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=15,
                                 cwd=root, check=True)
            raw = json.loads(out.stdout)
            got = {"number": num, "url": raw.get("url") or pr.get("url") or "",
                   "body": raw.get("body") or ""}
            cache.write_text(json.dumps(got, indent=1) + "\n", encoding="utf-8")
        except Exception as exc:                  # noqa: BLE001 - the cache stands in
            print(f"[review] `gh pr view {num}` did not answer ({exc}); "
                  + ("using the description cached in " + PR_BODY_JSON if got
                     else "the PR description is left off the page"), file=sys.stderr)
    if got:
        spec["_prBody"] = got


def _md_inline(text: str, repo_url: str) -> str:
    """One line of GitHub markdown as HTML: code, bold, links, bare URLs, `#25`."""
    keep: list[str] = []

    def stash(h: str) -> str:
        keep.append(h)
        return f"\x00{len(keep) - 1}\x00"

    t = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: stash(
        f'<a href="{html.escape(m.group(2), quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(m.group(1))}</a>'), t)
    t = re.sub(r"(?<![\w/])(https?://[^\s<>()]+[^\s<>().,;:!?])", lambda m: stash(
        f'<a href="{html.escape(m.group(1), quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(m.group(1))}</a>'), t)
    if repo_url:
        t = re.sub(r"(?<![\w&/])#(\d+)\b", lambda m: stash(
            f'<a href="{html.escape(repo_url)}/issues/{m.group(1)}" target="_blank" '
            f'rel="noopener">#{m.group(1)}</a>'), t)
    t = html.escape(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)


def pr_body_html(body: str, repo_url: str = "") -> str:
    """A PR description's markdown as HTML: paragraphs, headings, lists (task boxes too),
    fenced code, quotes. Escaped before anything is let through — it is somebody's text."""
    out, para, items, code = [], [], [], None
    def flush():
        if para:
            out.append("<p>" + "<br>".join(_md_inline(x, repo_url) for x in para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            items.clear()
    for line in body.replace("\r\n", "\n").split("\n"):
        if code is not None:
            if line.strip().startswith("```"):
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
            continue
        if not line.strip():
            flush()
            continue
        h = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        li = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)$", line)
        q = re.match(r"^\s*>\s?(.*)$", line)
        if h:
            flush()
            out.append(f"<h4>{_md_inline(h.group(1), repo_url)}</h4>")
        elif li:
            if para:
                flush()
            box = {" ": "\u2610 ", "x": "\u2611 ", "X": "\u2611 "}.get(li.group(1) or "", "")
            items.append(box + _md_inline(li.group(2), repo_url))
        elif q:
            flush()
            out.append(f"<blockquote>{_md_inline(q.group(1), repo_url)}</blockquote>")
        else:
            if items:
                flush()
            para.append(line.strip())
    if code is not None:
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush()
    return "".join(out)


def pr_description_html(spec) -> str:
    """The PR's description, between the grade's reasons and the counts line. Empty when
    there is no PR, or its description is empty once the agent's attribution line is cut."""
    got = spec.get("_prBody") or {}
    body = _PR_BODY_BOILERPLATE.sub("", str(got.get("body") or "")).strip()
    if not body:
        return ""
    url = str(got.get("url") or "")
    m = _PULL_URL.search(url)
    repo_url = f"https://github.com/{m.group(1)}" if m else ""
    link = (f' <a class="prdesc-gh" href="{html.escape(url, quote=True)}" target="_blank" '
            'rel="noopener">on GitHub &#8599;</a>' if url else "")
    return (f'<section class="prdesc" aria-label="Pull request description">'
            f'<p class="prdesc-h">PR description{link}</p>'
            f'<div class="prdesc-body">{pr_body_html(body, repo_url)}</div></section>')


def grade_reasons_html(spec) -> str:
    """The panel above the three piles that the score in the masthead links to: a bullet
    per reason on the left, and the grade itself, large, in the right-hand space the short
    bullets leave empty. Empty when there is no verdict.

    The grade was a small `Why graded 6/10` heading over the bullets, which made the
    number the least visible thing in a panel that exists to explain it, and left half the
    panel blank beside a column of five-word lines. Now the bullets start at the top and
    the number sits beside them, where the eye lands after reading them. When a signal
    lowered the model's number, the panel says from what (`was 8`)."""
    v = spec.get("verdict")
    if not v or "score" not in v:
        return ""
    reasons = _grade_rows(spec)
    if not reasons:
        return ""
    n = int(v["score"])
    band = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")

    def link(at) -> str:
        if not at:
            return ""
        href, face = at
        # The file, not its line: `proposal.md:86` reads `proposal.md` and the link still
        # lands on line 86 (Victor, 9 Oct 2026: no line numbers as labels on this page).
        face = re.sub(r":\d+(?:[-–]\d+)?(?:,\d+(?:[-–]\d+)?)*$", "", face)
        return (f' — <a href="{html.escape(href, quote=True)}" target="_blank" '
                f'rel="noopener">{html.escape(face)}</a>')
    def rest(short: str, full: str) -> str:
        # The hover adds what the bullet does not say: a full sentence that opens with the
        # bullet's own words loses them, and one with nothing left gets no hover at all.
        head = short.rstrip(" .:;")
        if full.startswith(head):
            full = full[len(head):].lstrip(" .:;,—–-")
        return "" if full.rstrip(" .") == short.rstrip(" .") else full

    # Good news gets a tick: green CI is the one bullet that is not a worry. Red or pending
    # keep the plain wording.
    good = lambda short: "\u2705 " if short.startswith("CI green") else ""
    items = "".join(
        f'<li data-tip="{html.escape(rest(short, full), quote=True)}">{good(short)}{html.escape(short)}{link(at)}</li>'
        if full and rest(short, full) else f"<li>{good(short)}{html.escape(short)}{link(at)}</li>"
        for short, full, at in reasons)
    was = v.get("modelScore")
    # The word, not the arithmetic: "was 7" struck through raised the question it was meant
    # to answer. The hover says who graded what and why it dropped.
    capped = (f'<span class="gradewhy-was" title="Capped: the AI graded it {was}/10; the '
              f'reasons marked on the left cap it lower.">capped</span>'
              if was is not None and int(was) != n else "")
    return (f'<aside class="gradewhy {band}" id="grade-why" aria-label="Why graded {n}/10">'
            f'<ul>{items}</ul>'
            f'<p class="gradewhy-score">'
            f'<span class="gradewhy-l">graded</span>'
            f'<span class="gradewhy-n"><b>{n}</b>/10</span>{capped}</p></aside>')


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
            n_open = pile_numbers(spec)[0]
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
            n_open = pile_numbers(spec)[0]
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
        # First, since the piles read as rounds: what was assumed while coding (I) came
        # before anything the review raised (II) or fixed (III).
        parts.insert(0, clause(
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
    # The grade's reasons go above the counts line, not under it: the line is sticky and
    # has to stay the topmost thing in the tab once the reader scrolls, and the panel is
    # read once, on arrival from the score, and then left behind.
    # The takeover row ("32 commits made after the reviewed version") goes between the
    # two: under the grade, which is read first on arrival from the masthead, and directly
    # above the counts line it qualifies — every number on that line was counted at the
    # reviewed commit, not at the branch's head.
    # The PR's own words go right under the grade (Victor, 8 Oct 2026): what the change
    # claims to be, read before the counts of what was found in it.
    return (grade_reasons_html(spec) + pr_description_html(spec) + _flush_top_bands()
            + f'<p class="sub counts pilelede"{no_pr_line(spec)}>' + " &middot; ".join(parts)
            + push_pr_button(spec) + "</p>" + push_pr_dialog(spec)
            + PILELEDE_SPY_JS)


def render_findings(findings) -> str:
    """The open pile, numbered — and nothing of the refuted claims.

    A refuted claim is settled, so it is not drawn at all (Victor, 5 Oct 2026): it used to
    be a small pile of its own under the open one, with a `N refuted` count on the counts
    line, the tab pill and the masthead chip. The items stay in the review data, and
    `pile_numbers` still leaves them out of `open`, so every count on the page is still a
    count of something drawn."""
    if not findings:
        return '<p class="sub">Nothing outstanding \u2014 the automated passes came back clean.</p>'
    live = [f for f in findings if not is_refuted(f)]
    return (_render_finding_items(live) if live
            else '<p class="sub">Nothing left open.</p>')


def _render_finding_items(findings) -> str:
    items = []
    # Worst first, whatever order the source listed them in: a pile that read "worth a
    # look, nit, worth a look" made the reader sort it in their head. Stable, so equal
    # severities keep the author's order.
    rank = {k: i for i, k in enumerate(SEVERITIES)}
    findings = sorted(findings, key=lambda f: rank.get(f.get("severity", "info"), len(rank)))
    for f in findings:
        cls, label = SEVERITIES.get(f.get("severity", "info"), SEVERITIES["info"])
        li_cls = cls.replace("sev-", "n-")
        refs = _finding_refs(f)
        items.append(
            f'<li class="{li_cls}">'
            f'<span class="badge {cls}">{html.escape(label)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + gh_comment_link(f)
            + (f'<p class="f-obs"><b>{_obs_label(f)}:</b> {f["observation"]}</p>'
               if f.get("observation") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + _anchor_note(f)
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(findings)) + "\n".join(items) + "</ol>"


#: The confidence chip's tooltip, fixed rather than composed per item — Victor's own
#: words, with the scale in the unit the chip now shows. It names the scale, not the one
#: number already on the chip's own face; the number does not need saying twice.
CONFIDENCE_TIP = "Confidence ∈ [10% .. 90%]"


def _confidence_chip(f) -> str:
    """The number beside the purple `assumption` chip, read verbatim off
    `review-points.json`'s `confidence` — how sure the agent that wrote the code is that
    this reading of the ticket is the right one, not a severity: absent when the item
    declares none, because a scale a model was never asked to fill in is not the same fact
    as a model that filled it in at the middle. Coloured by band, in the page's own
    severity hues (Victor, 7 Oct 2026: "super important"): red under 50%, amber up to the
    lede's own "under 70% sure" line, green from 70% — so the chips and the lede count
    the same unsure items. Shown as a percentage (`0.45` → `45%`), the stored value stays
    a rate."""
    c = f.get("confidence")
    if c is None:
        return ""
    # A percentage, not a rate: `45%` is read at a glance, `0.45` is read as arithmetic.
    # The lede already says "3 under 70% sure"; the chips now speak the same unit.
    shown = f"{round(c * 100)}% confident"
    band = "sev-high" if c < 0.5 else "sev-med" if c < 0.7 else "sev-info"
    cls = f"f-confidence {band}"
    # No tooltip (Victor, 7 Oct 2026): the number says it; the old range hint only added noise.
    return f'<span class="{cls}">{shown}</span>'


#: The word on every assumption card — fixed, never the item's own `source`. The report
#: once carried `source` through to this badge verbatim, so one run's cards read
#: `implementation decision` and `human` where every other page reads `assumption`.
ASSUMPTION_BADGE = "assumption"


def _decided_by(f) -> str:
    """`chosen by the human`, after the chip, on the one kind of assumption the agent did
    not make: a call the human made in the conversation, recorded so the reader knows it
    was not a guess. Nothing for the agent's own — that is what the badge already says."""
    if f.get("decidedBy") == "human":
        return ' <span class="f-src">chosen by the human</span>'
    return ""


def _assumption_why(f) -> str:
    """`Why 55%: …` — the agent's reason for this reading *and* for how sure it is of it.

    The confidence chip says how sure; nothing on the card used to say why, so a 50–65%
    reading could not be argued with from the one clause beside it. `record-review` now
    asks for one or two sentences that say what holds the number where it is, and the
    label names the number the sentence answers."""
    if not f.get("why"):
        return ""
    c = f.get("confidence")
    label = f"Why {round(c * 100)}%:" if isinstance(c, (int, float)) else "Why:"
    return f'<p class="f-why"><b>{label}</b> {f["why"]}</p>'


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
            f'<span class="badge sev-assumed">{ASSUMPTION_BADGE}</span>'
            + _confidence_chip(f)
            + _decided_by(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + gh_comment_link(f)
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-alt"><b>Read the other way:</b> {f["alternative"]}</p>'
               if f.get("alternative") else "")
            + _assumption_why(f)
            + (f"<p>{refs}</p>" if refs else "")
            + _anchor_note(f)
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
            + gh_comment_link(f)
            + (f'<p class="f-obs"><b>{_obs_label(f)}:</b> {f["observation"]}</p>'
               if f.get("observation") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + _anchor_note(f)
            + (f.get("_fixDiffs") or f.get("_diffs", "") or "")
            + (f'<p class="f-fix"><b>Fix:</b> {f["fix"]}</p>'
               if f.get("fix") and not f.get("_formatOnly")
               and not restates_title(f["fix"], f.get("title", "")) else "")
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return _open_list(len(fixes)) + "\n".join(items) + "</ol>"


# --------------------------------------------------------------------------- #
# Each fix's own hunks, not the whole file
# --------------------------------------------------------------------------- #

#: How far, in lines, a hunk's change may sit from a Fixed card's `file:line` and still be
#: that card's: the context the hunk itself shows (`DIFF_CONTEXT`), so a card takes a hunk
#: only when its line is ON the hunk as drawn. It was 15, and eval run 8's two Fixed cards
#: on one spec (lines 179 and 192) both "reached" the same +25 hunk and both drew it in
#: full. Further than this nobody takes it, and it is listed under the pile as another
#: change in the fix commit — shown once, never dropped.
FIX_HUNK_REACH = DIFF_CONTEXT

#: Files a fix commit carries that are the review's bookkeeping rather than a fix. The
#: points file itself is added from the report's own `source`.
FIX_BOOKKEEPING = ("review-cost.json",)

#: Where `run-steps.py` leaves `review-commits.py --json`: which commit is which, and its
#: doubts about the answer (`warnings`).
REVIEW_COMMITS_JSON = "review-commits.json"


def generated_in(root: Path, base: str, head: str | None) -> list[str]:
    """The files `base..head` changes that a generator owns — the project's `"generated"`
    globs in `human-review.json`, else the shared list (`shared/chips.py:generated_globs`),
    matched by git's own `:(glob)` pathspec so the same string means the same paths as in
    the header chips and the aftermath band.

    Eval run 10: the fix commit's re-recorded `*.genseq.json` / `.puml` traces were drawn
    under *Other changes in the fix commit* as 4 KB single-line JSON diffs — about
    3,000 px whose only change was a random step id. A fix is what a person changed."""
    from ..shared.chips import _project_cfg, generated_globs
    globs = generated_globs(_project_cfg(Path(root)))
    listed = _git_out(root, "diff", "--name-only", "--no-renames", base,
                      *([head] if head else []), "--", *[f":(glob){g}" for g in globs])
    return [p for p in (listed or "").splitlines() if p]


def fix_commits(points: dict, root: Path | None, fixed_by: str | None = None) -> list[dict]:
    """Every commit between the implementation and the review commit, oldest first:
    `{sha, subject, files, bookkeeping}` — `bookkeeping` when it touched nothing but the
    points file and `review-cost.json`.

    The fix range is `implementation..review commit`, and eval run 10 had three commits in
    it: 6b14c32b carried every fix, 1338ed9e and e7b807e8 only re-anchored one line of
    `review-points.md`. The page called `91905dff..e7b807e8` "the fix commit" and never
    named 6b14c32b; this is what lets it name all three and say which one is the fixes."""
    prov = points.get("provenance") or {}
    impl = prov.get("implementation") or prov.get("auditedHead")
    head = fixed_by or fix_commit(points, root)
    if not (root and impl and head):
        return []
    listed = _git_out(root, "log", "--reverse", "--format=%H%x1f%s", f"{impl}..{head}") or ""
    src = points.get("source") or "review-points.md"
    out = []
    for line in listed.splitlines():
        sha, _, subject = line.partition("\x1f")
        if not sha:
            continue
        files = [p for p in (_git_out(root, "diff-tree", "--no-commit-id", "--name-only",
                                      "-r", "--root", sha) or "").splitlines() if p]
        out.append({"sha": sha, "subject": subject, "files": files,
                    "bookkeeping": bool(files) and all(
                        p == src or Path(p).name in FIX_BOOKKEEPING for p in files)})
    return out


def _commit_face(sha: str, repo: str | None, tip: str = "") -> str:
    code = f"<code>{html.escape(sha[:8])}</code>"
    t = f' data-tip="{html.escape(tip, quote=True)}"' if tip else ""
    if repo:
        return (f'<a href="{html.escape(repo)}/commit/{html.escape(sha)}" target="_blank" '
                f'rel="noopener"{t}>{code}</a>')
    return f"<span{t}>{code}</span>" if t else code


def fix_commits_html(commits: list[dict], repo: str | None, src: str) -> str:
    """`the fix commit 6b14c32b`, or with several, `3 fix commits: 6b14c32b (the fixes);
    1338ed9e, e7b807e8 only re-record review-points.md` — every one named, each a link."""
    if not commits:
        return "the fix commit"
    if len(commits) == 1:
        c = commits[0]
        return "the fix commit " + _commit_face(c["sha"], repo, c["subject"])
    work = [c for c in commits if not c["bookkeeping"]]
    books = [c for c in commits if c["bookkeeping"]]
    said = []
    if work:
        said.append(", ".join(_commit_face(c["sha"], repo, c["subject"]) for c in work)
                    + (" (the fixes)" if books else ""))
    if books:
        names = sorted({Path(p).name for c in books for p in c["files"]})
        said.append(", ".join(_commit_face(c["sha"], repo, c["subject"]) for c in books)
                    + f" only re-record{'s' if len(books) == 1 else ''} "
                    + " and ".join(f"<code>{html.escape(n)}</code>" for n in names))
    return f"{len(commits)} fix commits: " + "; ".join(said)


def review_commits_warnings(out_dir: Path, commits: list[dict]) -> list[str]:
    """What `review-commits.py` doubted about which commit is which, in words, when it
    bears on the fixes' diffs — never shown before (eval run 10 wrote "3 commits carry a
    Review-Points trailer…" to the JSON and the page said nothing).

    Several trailered commits inside one fix range are the same round recorded in pieces;
    the page now names each of them (`fix_commits_html`), so that warning is said as a
    sentence about them instead. The session warning is about the cost tab, not here."""
    doc = _read_json(Path(out_dir) / REVIEW_COMMITS_JSON)
    out = []
    for w in (doc or {}).get("warnings") or [] if isinstance(doc, dict) else []:
        w = str(w)
        if "Claude-Session" in w:
            continue
        m = re.match(r"(\d+) commits carry a Review-Points trailer", w)
        if m:
            if len(commits) > 1:
                out.append(f"{m[1]} commits carry a <code>Review-Points:</code> trailer; "
                           "the page reads them as one round of fixes, from the "
                           "implementation to the last of them.")
            continue
        out.append(html.escape(w[:1].upper() + w[1:]))
    return out


def fix_hunks(rel: str, base: str, head: str | None, root: Path) -> list[tuple[int, int]]:
    """`[(lo, hi), …]` — the new-side lines each hunk of `rel` changed, one pair per hunk,
    in the order `diff_html(…, hunks=…)` indexes them (same command, same context).

    Context lines are not counted: a hunk reaches as far as what it changed. A pure deletion
    sits at the new-side line it was cut before."""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", f"-U{DIFF_CONTEXT}", "--no-color", base]
        + ([head] if head else []) + ["--", rel], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    out = []
    for part in re.split(r"(?m)^(?=@@ )", proc.stdout)[1:]:
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", part)
        new_no = int(m.group(1)) if m else 0
        touched = []
        for line in part.split("\n")[1:]:
            if line.startswith("+"):
                touched.append(new_no)
                new_no += 1
            elif line.startswith("-"):
                touched.append(new_no)
            elif line.startswith("\\"):
                continue
            else:
                new_no += 1
        if touched:
            out.append((min(touched), max(touched)))
        else:
            out.append((new_no, new_no))
    return out


def hunk_bodies(rel: str, base: str, head: str | None,
                root: Path) -> list[tuple[list[str], list[str]]]:
    """`[(removed, added), …]` — each hunk's `-` and `+` lines, in `fix_hunks` order."""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", f"-U{DIFF_CONTEXT}", "--no-color", base]
        + ([head] if head else []) + ["--", rel], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    out = []
    for part in re.split(r"(?m)^(?=@@ )", proc.stdout)[1:]:
        lines = part.split("\n")[1:]
        out.append(([ln[1:] for ln in lines if ln.startswith("-")],
                    [ln[1:] for ln in lines if ln.startswith("+")]))
    return out


def _layout_free(lines: list[str]) -> str:
    """The text with its layout gone: no whitespace, no `* ` opening a doc-comment line,
    no `" + "` joining a string split across lines."""
    kept = []
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("*") and not ln.startswith("*/"):
            ln = ln[1:]
        kept.append(ln)
    text = re.sub(r"\s+", "", "".join(kept))
    return re.sub(r'"\+"', "", text)


def format_only_hunk(removed: list[str], added: list[str]) -> tuple[int, bool] | None:
    """`(lines, rewrapped)` when a hunk changes only layout — the same code re-wrapped,
    re-indented, a string split with `+`, a doc comment opened onto several lines — else
    None. `lines` is how many lines it re-laid (the old side); `rewrapped` whether the line
    count changed. A hunk that adds or removes nothing is not one."""
    if not removed or not added:
        return None
    if _layout_free(removed) != _layout_free(added):
        return None
    return len(removed), len(removed) != len(added)


#: A constant declared on an added line, and the value it names: Java/Kotlin
#: `static final String NAME = "…";` / `const val NAME = …`, TS/JS `const NAME = '…'` /
#: `readonly NAME = …`, Python `NAME = "…"` at the start of a line.
_CONST_DECL = re.compile(
    r"""(?:\b(?:static\s+final|final\s+static|const\s+val|const|readonly)\s+"""
    r"""(?:[\w<>\[\],.? ]+?\s+)?|^\s*)(?P<name>[A-Z][A-Z0-9_]{2,})\s*(?::\s*[\w<>\[\]]+\s*)?="""
    r"""\s*(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`|-?\d[\w.]*)\s*;?\s*$""")


def constants_declared(added: list[str]) -> dict[str, str]:
    """`{NAME: value}` for every constant an added line declares (`_CONST_DECL`)."""
    out = {}
    for ln in added:
        m = _CONST_DECL.search(ln)
        if m:
            out[m.group("name")] = m.group("value")
    return out


def replaces_constant(removed: list[str], added: list[str], consts: dict[str, str]) -> bool:
    """Whether a hunk swaps a literal for the constant that names it: a removed line holds
    the value, an added line the name. The use-site half of a Sonar S1192 extract-constant
    fix — eval run 12 drew the two declarations under the card and the eight uses under
    *Other changes*, where nothing said they were the same fix."""
    for name, value in consts.items():
        used = re.compile(r"\b" + re.escape(name) + r"\b")
        # A number is matched whole: `20` must not claim every line holding `2026`.
        lit = re.compile((r"(?<![\w.])" + re.escape(value) + r"(?![\w.])")
                         if value[:1] not in "\"'`" else re.escape(value))
        if any(lit.search(ln) for ln in removed) and any(used.search(ln) for ln in added):
            return True
    return False


def _ref_spans(ref: str) -> tuple[str, list[tuple[int, int]] | None]:
    """`path:12-30,40` → `("path", [(12, 30), (40, 40)])`; a bare path → `(path, None)`."""
    m = re.match(r"^(.*?):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)$", ref)
    if not m:
        return ref, None
    spans = []
    for part in m.group(2).split(","):
        lo, _, hi = part.partition("-")
        spans.append((int(lo), int(hi or lo)))
    return m.group(1), spans


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Lines between two inclusive ranges; 0 when they touch or overlap."""
    return max(0, a[0] - b[1], b[0] - a[1])


def fix_commit(points: dict, root: Path | None) -> str | None:
    """The commit whose hunks are the round's fixes, or None when there is none after the
    implementation commit.

    The review commit when it descends from the implementation and is not it; else the
    newest commit after the implementation whose subject opens on `[auto-fix]` or whose
    message carries a `Review-Points:` trailer. Run 6 recorded `fixed-in: HEAD` in the
    front-matter only, so no item had `diffs` and every Fixed card fell back to a NEW CODE
    snapshot against main — though its `[auto-fix]` commit sat right there, one after the
    implementation. Whether a fix commit exists is the question; how the file spelled
    `fixed-in` is not."""
    prov = points.get("provenance") or {}
    impl = prov.get("implementation") or prov.get("auditedHead")
    if not (root and impl):
        return None
    impl_sha = _git_out(root, "rev-parse", "--verify", "--quiet", f"{impl}^{{commit}}")
    if not impl_sha:
        return None

    def after(rev: str | None) -> str | None:
        sha = rev and _git_out(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        if not sha or sha == impl_sha:
            return None
        return sha if _git_out(root, "merge-base", "--is-ancestor", impl_sha, sha) == "" \
            else None
    found = after(prov.get("reviewCommit"))
    if found:
        return found
    listed = _git_out(root, "log", "--format=%H", r"--grep=^\[auto-fix\]",
                      "--grep=^Review-Points:", f"{impl_sha}..HEAD") or ""
    return next((line for line in listed.splitlines() if line.strip()), None)


def _fix_range(item: dict, points: dict,
               fixed_by: str | None = None) -> tuple[str | None, str | None]:
    """`(base, head)` of the commit(s) a Fixed item's diff is read from.

    The base is the implementation commit (or the one the item's diffs name). The head is
    the rev the item pins (`fixed-in: <sha>`), else the fix commit (`fix_commit`: the
    `[auto-fix]` commit that carries every fix of the round), else the working tree."""
    d = (item.get("diffs") or [{}])[0]
    prov = points.get("provenance") or {}
    return (d.get("base") or prov.get("implementation"),
            d.get("head") or fixed_by or prov.get("reviewCommit"))


def attribute_fix_hunks(spec: dict, out_dir: Path, root: Path | None = None) -> None:
    """Show each Fixed card the hunks its own anchors reach, and list the rest after the pile.

    Each card used to render the whole file against the implementation commit, once per
    file it named. One `[auto-fix]` commit usually carries every fix, so a file two fixes
    touched appeared under both cards with both changes, a one-line fix showed +22 because
    another fix's tests sat in the same spec, and a file no card named was on no card at
    all. Now the fix commit's hunks are dealt out by position: a hunk goes to the card
    whose `file:line` it overlaps or comes nearest to, within `FIX_HUNK_REACH` lines. What
    no card reaches is rendered once, under the pile, as *other changes in the fix commit*
    — so the pile still adds up to the whole commit.

    Rendered here, as `_fixDiffs` on each item and `fixOther` on the report, rather than
    through the item's `diffs`: those the build would draw whole. A card whose anchor got
    a hunk loses the snippet of the same lines, which the hunk already shows."""
    points = spec.get("_reviewPoints") or {}
    if points.get("missing"):
        return
    root = root if root is not None else _git_root(out_dir)
    if root is None:
        return
    # Every Fixed item with an anchor, not only those carrying `diffs`: whenever a fix
    # commit follows the implementation, its hunks are what the card shows.
    fixed_by = fix_commit(points, root)
    # Every commit of the fix range named, and what `review-commits.py` doubted about it,
    # for the pile's intro (`pile_intro`) and the block under the pile.
    commits = fix_commits(points, root, fixed_by)
    repo = github_blob_base(Path(root))
    points["fixCommits"] = commits
    points["fixCommitsHtml"] = fix_commits_html(
        commits, repo, points.get("source") or "review-points.md")
    points["fixWarnings"] = review_commits_warnings(out_dir, commits)
    fixes = [f for f in spec.get("autofixes") or [] if isinstance(f, dict)
             and (f.get("diffs") or (fixed_by and f.get("refs")))]
    if not fixes:
        return
    skip = {points.get("source") or "review-points.md"}
    groups: dict[tuple, list[dict]] = {}
    for f in fixes:
        base, head = _fix_range(f, points, fixed_by)
        if base and _git_out(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") \
                and (not head or _git_out(root, "rev-parse", "--verify", "--quiet",
                                          f"{head}^{{commit}}")):
            groups.setdefault((base, head), []).append(f)
    other_html = []
    for (base, head), items in groups.items():
        listed = _git_out(root, "diff", "--name-only", "--no-renames", base,
                          *([head] if head else [])) or ""
        generated = set(generated_in(root, base, head))
        files = [p for p in listed.splitlines()
                 if p and p not in skip and Path(p).name not in FIX_BOOKKEEPING
                 and p not in generated]
        owned: list[dict[str, list[int]]] = [{} for _ in items]
        cache: dict[str, list[tuple[list[str], list[str]]]] = {}

        def bodies(rel: str, base=base, head=head, cache=cache):
            if rel not in cache:
                cache[rel] = hunk_bodies(rel, base, head, root)
            return cache[rel]
        # A hunk two cards' lines both sit on is drawn ONCE, under the first of them, with
        # every title it serves; the others say where it is (eval run 8 drew one +25 spec
        # hunk in full under two cards in a row).
        shared: list[list[tuple[str, int, list[int]]]] = [[] for _ in items]
        pointers: list[list[tuple[str, int]]] = [[] for _ in items]
        unowned: dict[str, list[int]] = {}
        for rel in files:
            for idx, span in enumerate(fix_hunks(rel, base, head, root)):
                near, whole = [], []
                for i, f in enumerate(items):
                    for ref in f.get("refs") or []:
                        path, spans = _ref_spans(ref)
                        if path != rel:
                            continue
                        if spans is None:
                            whole.append(i)
                            continue
                        gap = min(_gap(s, span) for s in spans)
                        if gap <= FIX_HUNK_REACH:
                            near.append((gap, i))
                if near:
                    best = min(g for g, _ in near)
                    takers = sorted({i for g, i in near if g == best})
                else:
                    takers = sorted(set(whole))
                if len(takers) == 1:
                    owned[takers[0]].setdefault(rel, []).append(idx)
                elif takers:
                    shared[takers[0]].append((rel, idx, takers))
                    for i in takers[1:]:
                        pointers[i].append((rel, takers[0]))
                if not takers:
                    unowned.setdefault(rel, []).append(idx)
        # A card that extracts a constant (Sonar S1192) also owns the hunks of the same file
        # that swap that constant's literal for its name — attributed by the name, since
        # the uses sit far from the declaration its anchor points at.
        for i in range(len(items)):
            for rel, idxs in list(owned[i].items()):
                consts: dict[str, str] = {}
                for k in idxs:
                    if k < len(bodies(rel)):
                        consts.update(constants_declared(bodies(rel)[k][1]))
                if not consts or rel not in unowned:
                    continue
                moved = [k for k in unowned[rel] if k < len(bodies(rel))
                         and replaces_constant(*bodies(rel)[k], consts)]
                if moved:
                    owned[i][rel] = sorted(set(idxs) | set(moved))
                    unowned[rel] = [k for k in unowned[rel] if k not in moved]
                    if not unowned[rel]:
                        del unowned[rel]
        for i, (f, mine) in enumerate(zip(items, owned)):
            # The card's own files first, in the order it named them; then any other file
            # its anchors reached (a whole-file ref), in diff order.
            order = []
            for ref in f.get("refs") or []:
                path = _ref_spans(ref)[0]
                if path in mine and path not in order:
                    order.append(path)
            order += [p for p in mine if p not in order]
            body = "".join(diff_html(p, base, root, None, head, hunks=mine[p], fold=True)
                           for p in order)
            # Drawn once, under the first card it serves, with no caption: the other
            # card's pointer below says where it is (eval run 11 cut the caption here).
            for rel, idx, takers in shared[i]:
                body += diff_html(rel, base, root, None, head, hunks=[idx], fold=True)
            # A card whose every hunk only re-wraps or re-indents lines (a pre-push hook's
            # line-length refusal) is one line, `6 lines re-wrapped in 5 files`, its diffs
            # folded and its `fix:` the fold's hover (eval run 11: ~1,100px of line wraps).
            hunks = [(p, k) for p in order for k in mine[p]] \
                + [(rel, idx) for rel, idx, _ in shared[i]]
            fmt = [format_only_hunk(*bodies(rel)[k]) if k < len(bodies(rel)) else None
                   for rel, k in hunks]
            if body and fmt and all(fmt):
                lines = sum(n for n, _ in fmt)
                files = len({rel for rel, _ in hunks})
                verb = "re-wrapped" if any(w for _, w in fmt) else "re-indented"
                tip = (f' data-tip="{html.escape(_plain_text(f["fix"]), quote=True)}"'
                       if f.get("fix") else "")
                body = (f'<details class="fmtonly"><summary{tip}>{lines} line'
                        f'{"" if lines == 1 else "s"} {verb}'
                        + (f" in {files} files" if files > 1 else "")
                        + f'</summary>{body}</details>')
                f["_formatOnly"] = True
            for rel, owner in pointers[i]:
                body += (f'<p class="fixshared"><code>{html.escape(Path(rel).name)}</code>: '
                         f'diff shown under <b>{items[owner]["title"]}</b>.</p>')
            f["_fixDiffs"] = body
            drawn = set(mine) | {rel for rel, _, _ in shared[i]} | {rel for rel, _ in pointers[i]}
            if f.get("snippets"):
                f["snippets"] = [s for s in f["snippets"]
                                 if _ref_spans(str(s.get("ref", "")))[0] not in drawn]
            f["diffs"] = []
        rng = (f"<code>{html.escape(base[:8])}..{html.escape(head[:8])}</code>" if head
               else f"<code>{html.escape(base[:8])}</code>..the working tree")
        which = "fix commits" if len(commits) > 1 else "fix commit"
        if unowned:
            # Folded, with its count on the fold: what no card reaches is mostly mechanical
            # churn a card already summarises (run 10's eleven toBe → toHaveSize hunks), and
            # drawn open it was the longest thing on the tab. Still one click from the page.
            n = sum(len(v) for v in unowned.values())
            nf = len(unowned)
            other_html.append(
                '<details class="fixother">'
                f'<summary class="fixother-h"><b>Other changes in the {which}</b>: '
                f'{n} hunk{"" if n == 1 else "s"} in {nf} file{"" if nf == 1 else "s"}'
                '</summary>'
                + "".join(diff_html(p, base, root, None, head, hunks=v, fold=True)
                          for p, v in unowned.items())
                + '</details>')
        gen = sorted(p for p in listed.splitlines() if p in generated)
        if gen:
            k = len(gen)
            other_html.append(
                f'<p class="fixother-gen sub" data-tip="{html.escape(", ".join(gen))}">'
                f'{k} generated file{"" if k == 1 else "s"} re-recorded in the {which} '
                f'({rng}) — regenerated output, not a fix, so not drawn.</p>')
    if other_html:
        points["fixOther"] = "".join(other_html)


# --------------------------------------------------------------------------- #
# Every anchor, carried to the code the page shows
# --------------------------------------------------------------------------- #

#: The front-matter key `record-review.py finish` writes once it has carried every ref to
#: the tree it commits: all of them are then written at the review commit.
ANCHORS_KEY = "anchors"
ANCHORS_AT_REVIEW = "review-commit"


def _written_at(kind: str, points: dict, fixed_by: str | None) -> list[str]:
    """The revs a pile's refs were most likely written against, most likely first.

    `record-review.py finish` re-anchors and says so (`anchors: review-commit`), and then
    there is one answer. A file from before it says nothing, and the piles were written at
    different times: the assumptions while coding, at the implementation commit; the fixes
    and the declined findings after the fixes, at the review commit — run 6 to the line."""
    prov = points.get("provenance") or {}
    impl = prov.get("implementation") or prov.get("auditedHead")
    review = prov.get("reviewCommit") or fixed_by
    if ((points.get("frontmatter") or {}).get(ANCHORS_KEY) or "").strip() == ANCHORS_AT_REVIEW:
        return [review or "HEAD"]
    if kind == "assumptions":
        return [impl, review or "HEAD"]
    return [review or "HEAD", impl]


def reanchor_refs(spec: dict, out_dir: Path, root: Path | None = None) -> None:
    """Carry every item's `file:line` from the commit it was written against to the tree
    the page quotes, in place, through `git diff`'s hunks (`review-points.py:reanchor`).

    Run 6's assumptions were written at the implementation commit and rendered at HEAD
    after the `[auto-fix]` commit had moved them: `ExceptionControllerAdvice.java:85` came
    out as one blank line, `owner-list.component.ts:82` as an unrelated statement, and
    nothing on the page said so. A ref the diff removed keeps its file as a link and loses
    its snippet, and the card says what happened (`_anchorNotes`) instead of quoting
    whatever line now has that number.

    Runs after `attribute_fix_hunks`, which deals the fix commit's hunks by the Fixed
    cards' refs as they read at the review commit."""
    points = spec.get("_reviewPoints") or {}
    if not points or points.get("missing"):
        return
    root = root if root is not None else _git_root(out_dir)
    if root is None:
        return
    rp = _points_parser()
    fixed_by = fix_commit(points, root)
    for kind in POINTS_PILES:
        written = [w for w in _written_at(kind, points, fixed_by)
                   if w and _git_out(root, "rev-parse", "--verify", "--quiet",
                                     f"{w}^{{commit}}")]
        if not written:
            continue
        for item in spec.get(kind) or []:
            if not isinstance(item, dict):
                continue
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or ""))[:60]
            moved: dict[str, str | None] = {}
            notes = []
            for ref in item.get("refs") or []:
                if ref in moved or rp.ref_spans(ref)[1] is None:
                    continue
                got = rp.reanchor(root, ref, written)
                path = rp.ref_spans(ref)[0]
                face = html.escape(Path(path).name + ref[len(path):])
                at = html.escape(str(got["from"] or "")[:8])
                if got["ref"] is None:
                    moved[ref] = None
                    notes.append(f"<code>{face}</code> was written at <code>{at}</code>, and "
                                 "a later commit removed that line — there is no line to "
                                 "show for it any more.")
                    print(f"[review] WARNING: {title!r}: {ref} (written at {at}) no longer "
                          "exists — the card says so instead of quoting another line",
                          file=sys.stderr)
                    continue
                if got["moved"]:
                    moved[ref] = got["ref"]
                    print(f"[review] {title!r}: {ref} written at {at} reads as "
                          f"{got['ref']} now", file=sys.stderr)
                if got["blank"]:
                    notes.append(f"<code>{face}</code> points at a blank line — the line "
                                 "it meant has moved and could not be found again.")
            if not moved and not notes:
                continue
            item["refs"] = list(dict.fromkeys(
                r if r not in moved else (moved[r] or rp.ref_spans(r)[0])
                for r in item.get("refs") or []))
            if isinstance(item.get("snippets"), list):
                item["snippets"] = [
                    {**s, "ref": moved[s.get("ref")]} if moved.get(s.get("ref"))
                    else s for s in item["snippets"]
                    if not (s.get("ref") in moved and moved[s.get("ref")] is None)]
            if notes:
                item["_anchorNotes"] = notes


# --------------------------------------------------------------------------- #
# A reason that cites the spec, linked to the line it cites
# --------------------------------------------------------------------------- #

#: The OpenSpec documents a reason may cite by name, inside `openspec/changes/<change>/`.
SPEC_DOCS = ("design.md", "proposal.md", "tasks.md")
#: The planning Q&A, at the repository root or beside the change's documents.
QA_DOC = "Q&A.md"
#: Which fields of an item are prose a citation can sit in.
CITING_FIELDS = ("why", "observation", "body", "alternative", "fix")
#: How much of the cited text the hover quotes.
CITE_QUOTE = 220

_CITE = re.compile(
    r"(?P<doc>(?:[\w.-]+/)*(?:design|proposal|tasks)\.md|(?:[\w.-]+/)*specs/[\w.-]+/spec\.md)"
    r"(?::(?P<line>\d+))?"
    r"(?:\s+(?P<sec>Decision\s+\d+|Risks(?:\s*/\s*Trade-offs)?|Goals(?:\s*/\s*Non-Goals)?"
    r"|Non-Goals|Context|Migration Plan))?"
    r"|(?P<qa>Q&(?:amp;)?A\.md)(?::(?P<qaline>\d+))?"
    r"|\b(?P<task>[Tt]asks?\s+(?P<taskno>\d+\.\d+))\b"
    r"|\bQ(?P<q>\d+)(?:\s*(?:–|&#x2013;|&ndash;|-)\s*Q?(?P<q2>\d+))?\b")
#: What a citation is never looked for inside: code, an existing link, a tag.
_CITE_GUARD = re.compile(r"(<code\b.*?</code>|<a\b.*?</a>|<[^>]+>)", re.S)


def _spec_change_dir(root: Path, spec) -> str | None:
    """`openspec/changes/<change>` — the change this branch was built against: the one
    directory there (outside `archive/`) carrying a design, proposal or tasks file; with
    several, the one the branch itself committed to."""
    base = Path(root) / "openspec" / "changes"
    if not base.is_dir():
        return None
    dirs = sorted(d for d in base.iterdir() if d.is_dir() and d.name != "archive"
                  and any((d / n).is_file() for n in SPEC_DOCS))
    if len(dirs) <= 1:
        return f"openspec/changes/{dirs[0].name}" if dirs else None
    base_ref = _base_ref(spec, root)
    mb = _git_out(root, "merge-base", base_ref, "HEAD") if base_ref else None
    touched = _git_out(root, "diff", "--name-only", f"{mb}..HEAD", "--",
                       "openspec/changes") if mb else ""
    hit = [d for d in dirs if f"openspec/changes/{d.name}/" in (touched or "")]
    return f"openspec/changes/{hit[0].name}" if len(hit) == 1 else None


def _doc_lines(root: Path, rel: str) -> list[str]:
    try:
        return (Path(root) / rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _cited_quote(lines: list[str], line: int) -> str:
    """The cited line, markdown marks dropped, plus the next line of prose when the cited
    one is a heading — `### 3. New indexes…` alone says what, not what was decided."""
    def clean(s: str) -> str:
        return re.sub(r"^\s*(?:#+|[-*]\s*(?:\[[ xX]\]\s*)?)\s*", "", s).strip()
    if not (1 <= line <= len(lines)):
        return ""
    said = clean(lines[line - 1])
    if lines[line - 1].lstrip().startswith("#"):
        nxt = next((ln for ln in lines[line:line + 6] if ln.strip()
                    and not ln.lstrip().startswith("#")), "")
        if nxt:
            said += " — " + clean(nxt)
    said = re.sub(r"[*`]", "", said)
    return said if len(said) <= CITE_QUOTE else said[:CITE_QUOTE].rsplit(" ", 1)[0] + "…"


def _find_line(lines: list[str], pattern: str, after: str | None = None) -> int | None:
    start = 0
    if after:
        start = next((i for i, ln in enumerate(lines) if re.match(after, ln)), 0)
    rx = re.compile(pattern)
    return next((i + 1 for i, ln in enumerate(lines[start:], start) if rx.search(ln)), None)


def _resolve_citation(m: re.Match, root: Path, change: str | None,
                      qa: str | None) -> tuple[str, int | None, str] | None:
    """`(path, line or None, what the hover says)` for one citation, or None when the
    repository has nothing it could mean."""
    if m["doc"]:
        name = Path(m["doc"]).name
        # A capability's spec is cited by its path inside the change (`specs/owner-list/
        # spec.md:123`, eval run 11's CONTEXT card, left plain beside linked siblings).
        inside = m["doc"] if m["doc"].startswith("specs/") else name
        rel = m["doc"] if "/" in m["doc"] and (Path(root) / m["doc"]).is_file() else (
            f"{change}/{inside}" if change else None)
        if not rel or not (Path(root) / rel).is_file():
            return None
        lines = _doc_lines(root, rel)
        line = int(m["line"]) if m["line"] else None
        sec = m["sec"] or ""
        if line is None and sec.startswith("Decision"):
            n = sec.split()[-1]
            line = _find_line(lines, rf"^#+\s*(?:Decision\s+)?{n}[.:)]\s", r"^##\s+Decisions")
        elif line is None and sec:
            line = _find_line(lines, rf"^#+\s*{re.escape(sec.split()[0])}")
        return rel, line, (_cited_quote(lines, line) if line else
                           "the reason names the document, not a line in it")
    if m["qa"]:
        if not qa:
            return None
        line = int(m["qaline"]) if m["qaline"] else None
        lines = _doc_lines(root, qa)
        return qa, line, (_cited_quote(lines, line) if line else
                          "the reason names the document, not a line in it")
    if m["task"]:
        rel = f"{change}/tasks.md" if change else None
        if not rel or not (Path(root) / rel).is_file():
            return None
        lines = _doc_lines(root, rel)
        line = _find_line(lines, rf"^\s*(?:[-*]\s*(?:\[[ xX]\]\s*)?|#+\s*){re.escape(m['taskno'])}\b")
        return (rel, line, _cited_quote(lines, line)) if line else None
    if m["q"] and qa:
        lines = _doc_lines(root, qa)
        line = _find_line(lines, rf"^#+\s*Q{m['q']}\b")
        if not line:
            return None
        quote = _cited_quote(lines, line)
        if m["q2"]:
            last = _find_line(lines, rf"^#+\s*Q{m['q2']}\b")
            if last:
                quote += f" … through Q{m['q2']}: {_cited_quote(lines, last)}"
        return qa, line, quote
    return None


def link_spec_citations(spec: dict, out_dir: Path, root: Path | None = None) -> int:
    """Every `design.md Decision 3`, `task 2.1`, `Q3`, `Q&A.md:28` in an item's prose,
    turned into a link to the line it cites — the editor and GitHub at HEAD — with that
    line quoted on hover. Returns how many were linked.

    Eval run 10: open issues were dismissed with "design.md decided to surface it",
    "design.md Decision 3 and task 2.1 specify them", "Q3 decided by the human" — and the
    page had no link to any of those documents, so no reason could be checked from it.
    Only what the repository has is linked; a bare document name links the file and its
    hover says no line was cited, which is the thing `/record-review` now asks for."""
    root = root if root is not None else _git_root(out_dir)
    if root is None:
        return 0
    change = _spec_change_dir(root, spec)
    qa = next((r for r in ([f"{change}/{QA_DOC}"] if change else []) + [QA_DOC]
               if (Path(root) / r).is_file()), None)
    if not change and not qa:
        return 0
    gh = github_blob_base(Path(root))
    head = _git_out(root, "rev-parse", "HEAD") if gh else None
    linked = 0

    def link(m: re.Match) -> str:
        nonlocal linked
        got = _resolve_citation(m, root, change, qa)
        if not got:
            return m.group(0)
        rel, line, quote = got
        at = f"{rel}:{line}" if line else rel
        tip = html.escape(f"{at} — {quote}" if quote else at, quote=True)
        vs = f"vscode://file/{(Path(root) / rel).resolve()}" + (f":{line}:1" if line else "")
        # The citation keeps its words and loses its line: `design.md:44` reads `design.md`,
        # and the link and the hover still land on line 44. A line number on the face is a
        # number nobody acts on (Victor, 9 Oct 2026) — the link is what it was for.
        face = re.sub(r":\d+(?:[-–]\d+)?(?:,\d+(?:[-–]\d+)?)*$", "", m.group(0))
        out = (f'<a class="specref" href="{html.escape(vs, quote=True)}" data-tip="{tip}">'
               f'{face}</a>')
        if gh and head:
            web = f"{gh}/blob/{head}/{urllib_quote(rel)}" + (f"#L{line}" if line else "")
            out += (f'<a class="specref-gh" href="{html.escape(web, quote=True)}" '
                    f'target="_blank" rel="noopener" data-tip="{html.escape(at)} on GitHub, '
                    f'at {head[:8]}">↗</a>')
        linked += 1
        return out

    for kind in POINTS_PILES:
        for item in spec.get(kind) or []:
            if not isinstance(item, dict):
                continue
            for key in CITING_FIELDS:
                text = item.get(key)
                if not isinstance(text, str) or not text:
                    continue
                parts = _CITE_GUARD.split(text)
                item[key] = "".join(p if i % 2 else _CITE.sub(link, p)
                                    for i, p in enumerate(parts))
    return linked


def urllib_quote(rel: str) -> str:
    """A repository path as a URL path: `Q&A.md` → `Q%26A.md`, slashes kept."""
    import urllib.parse
    return urllib.parse.quote(rel, safe="/")


def _anchor_note(f) -> str:
    """What `reanchor_refs` could not carry across, said on the card itself."""
    notes = f.get("_anchorNotes") or []
    return "".join(f'<p class="f-anchor sub">{n}</p>' for n in notes)

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
    """One commit's row: the sha (carrying the file list in its hover), what the commit
    did, and when. Nothing to press: reverting, cherry-picking or re-reviewing any of it is
    the developer's call, made from the command line, and the page does not teach it.
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


def _taken_fold_html(commits: list[dict], takeover: dict | None,
                     review_short: str) -> str:
    """The branch's own commits a takeover accepted without a pass, folded to one row.

    They used to be a second list, above this band, typed by the agent that wrote the
    takeover note: the same kind of statement as the band, counted from a different commit,
    and frozen at the moment the note was written. Here they are read off `git` with the
    rest of the band, split from tooling the same way."""
    n = len(commits)
    when = html.escape(((takeover or {}).get("when") or "")[:10])
    items = "".join(_aftermath_commit(c) for c in commits)
    return (f'<details class="toolcommits takenover"><summary>'
            f'<span class="foldlbl">{n} commit{"" if n == 1 else "s"} after '
            f'<code>{html.escape(review_short)}</code> taken over without a new pass'
            + (f' on {when}' if when else '')
            + f'</span></summary><ul>{items}</ul></details>')


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
    # A takeover's own commit is bookkeeping — it touches the points file and nothing else,
    # and the band already says, in words, where it sits.
    commits = [c for c in doc.get("commits") or [] if not c.get("takeover")]
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
    branch_all = [c for c in commits if c.get("sha") not in tooling_shas]
    # What a takeover accepted without a pass, and what nobody has signed off at all. The
    # headline, the colour and the counts are about the second: the first was a decision,
    # and it is on the band as one fold, not as news.
    taken = [c for c in branch_all if c.get("taken_over")]
    branch_only = [c for c in branch_all if not c.get("taken_over")]
    takeover = doc.get("takeover") if isinstance(doc.get("takeover"), dict) else None
    since = "the review was taken over" if takeover else "the agent finished"
    reviewed = 'Reviewed at <code>' + html.escape(doc.get("review_short", "")) + '</code>'
    if takeover:
        reviewed += ('; taken over at <code>' + html.escape(takeover.get("sha", "")[:8])
                     + '</code> on ' + html.escape((takeover.get("when") or "")[:10])
                     + ' without a new pass')
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
        title = (f'<b>{n} commit{plural}, {lines} line'
                 f'{"" if lines == 1 else "s"} changed since {since}</b>')
        head = ('<p>Findings, assumptions and the tests map have not seen them; the other '
                'tabs are current.</p>')
        sub = (reviewed + '. '
               + (f'{code["genFiles"]} generated file'
                  + ("" if code["genFiles"] == 1 else "s") + ' not counted. '
                  if code["genFiles"] else '')
               + 'Clears with a new review pass, not with Regenerate.')
        cls = "rband-alert"
        role = "alert"
    elif n:
        title = f'{n} commit{plural} since {since}, and every file in them is generated'
        head = ''
        sub = reviewed + '. Regenerated output only.'
        cls = "rband-warn"
        role = "status"
    elif taken:
        # Everything the branch did since the review was taken over, and nothing since:
        # the piles still describe the reviewed commit, and that is the one thing to say.
        title = (f'{len(taken)} commit{"" if len(taken) == 1 else "s"} taken over '
                 'without a new pass')
        head = ''
        sub = reviewed + '.'
        cls = "rband-warn"
        role = "status"
    else:
        # Every commit since the review folded away as tooling: nothing here is news
        # about the review, only about what `main` shipped in the meantime.
        title = (f'Only tooling from {html.escape(base_label)} since {since} '
                 '— nothing about this review changed')
        head = ''
        sub = reviewed + '.'
        cls = "rband-warn"
        role = "status"
    # Folded to its one line. The count is the news; the commits behind it are there to
    # check, and a band that lists them open pushes the piles it qualifies off the screen.
    return (f'<details class="rband aftermath {cls}" role="{role}"><summary>{title}'
            '</summary>' + head
            + f'<p class="rb-sub">{sub}</p>'
            + ('<ul>' + "".join(_aftermath_commit(c) for c in branch_only) + '</ul>'
               if branch_only else '')
            + (_taken_fold_html(taken, takeover, doc.get("review_short", ""))
               if taken else '')
            + (_tooling_fold_html(tooling, base_label) if tooling else '')
            # After the list, not inside it: the commits are what happened, and this is the
            # one thing to do about all of them.
            + _regenerate_offer(out_dir, root) + '</details>')


#: The three block types that render the one list. Named so `render_block` can hand all
#: three to one function: they share the lede, the numbering, the band and — since
#: `{"auto": "review-points"}` — the question of what an empty one is allowed to say.
PILE_BLOCKS = ("findings", "assumptions", "autofixes")


#: The rounds the piles belong to, in the order they ran on the model: what the coder
#: assumed while implementing (I), what the code review raised (II), and the pass that
#: then applied the fixes it accepted (III) — a third run of its own, not part of the review.
PILE_ROUND = {"assumptions": ("I", "while coding"),
              "findings": ("II", "code review"), "autofixes": ("III", "fixing the review")}


def _round_kicker(spec, kind) -> str:
    """`I · while coding` over the first pile of each round, and nothing over the second
    pile of the same round. Read off the tab's own block list, so it needs no state."""
    for tab in spec.get("tabs") or []:
        kinds = [b.get("type") for b in tab.get("blocks") or [] if b.get("type") in PILE_ROUND]
        if kind not in kinds:
            continue
        first = next(k for k in kinds if PILE_ROUND[k] == PILE_ROUND[kind])
        if first != kind:
            return ""
        num, what = PILE_ROUND[kind]
        return (f'<p class="pileround"><b>Round {num}</b> \u00b7 {what}</p>')
    return ""


def render_pile_block(spec, block, heading=None):
    """One of the three piles, as `(html, weight, changes)`.

    Lifted out of `render_block` when the piles stopped being the content file's own list.
    The decision it now makes is not about layout at all — it is *whose* silence an empty
    pile is, the author's or the branch's — and that is worth testing directly rather than
    through a page build with a repository, a manifest and PlantUML behind it.

    `heading` is `render_block`'s local heading emitter; without one (a test, a caller
    rendering a pile on its own) the piles render bare.
    """
    kind = block.get("type", "section")
    points = spec.get("_reviewPoints")

    def head_of(fallback_id, fallback_title):
        if heading is None:
            return ""
        shown = block
        if points:
            # Read off the report, the heading and its intro are the builder's: whatever
            # the content file typed over them was already dropped (`own_review_tab`).
            fallback_title = PILE_TITLES[kind]
            shown = {**{k: v for k, v in block.items() if k not in ("title", "body")},
                     "title": fallback_title, "body": pile_intro(kind, points)}
        return _round_kicker(spec, kind) + heading(shown, fallback_id, fallback_title)

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
    return (head + render_autofixes(items, badge="fixed" if points else "auto-fixed")
            + ((points or {}).get("fixOther") or ""),
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
        # `path`, `path:12`, `path:12-30`, `path:89,93-95`. A whole-file ref has no line
        # to cut off: `rpartition(":")` on one left an empty path, and the build aborted on
        # every `- file: b.py` review-points.py has always accepted.
        m = re.match(r"^(.*):(\d+)(?:-\d+)?(?:,\d+(?:-\d+)?)*$", ref)
        rel, start = (m.group(1), m.group(2)) if m else (ref, "1")
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


# --------------------------------------------------------------------------- #
# A card's quoted code: the whole statement, never a whole class
# --------------------------------------------------------------------------- #

#: The most lines a card's quote shows open; the rest of the window is folded under it.
#: Eval run 11 embedded a 41-line class (OwnerPageRequest.java:20-60) in one assumption.
SNIPPET_LINES = 12
#: The most lines a one-line anchor is widened to when it sits inside a longer statement.
STATEMENT_LINES = 6
#: The languages whose statements continue on lines that open with `.foo()` or `)`.
_STATEMENT_SUFFIXES = frozenset((".java", ".kt", ".kts", ".ts", ".tsx", ".js", ".jsx",
                                 ".mjs", ".cs", ".scala", ".groovy", ".swift", ".go", ".dart"))
#: A line that continues the one above it: `.toList();`, `?.x`, `)`, `&& b`, `+ "x"`.
_CONT_START = re.compile(r"^(?:\.\w|\?\.|\)|\]|\}\s*\)|&&|\|\||\?\s|:\s|->|=>|\+\s)")
#: A line the statement goes on after: it ends in `(`, `,`, `.`, `=`, an operator.
_OPEN_END = re.compile(r"(?:\(|\[|,|\.|=|\+|&&|\|\||\?|->|=>)$")


def widen_anchor(ref: str, root: Path | None) -> str:
    """`path:142` → `path:139-142` when line 142 is the tail of a longer statement.

    Eval run 11 pinned two findings to OwnerRestController.java:142, which reads
    `.toList();` — the throwing `.orElseThrow(…)` was line 141, off the card. A one-line
    anchor in a code file is widened to the statement it sits in: back over lines that
    continue the one above (`.foo()`, `)`, `&&`) or follow one left open (`(`, `,`, `=`),
    forward while the line itself is left open, never past `STATEMENT_LINES`. A line that
    is a whole statement already, a range, or a file the page cannot read stays as is."""
    m = re.match(r"^(.*):(\d+)$", ref)
    if not m or root is None or Path(m[1]).suffix not in _STATEMENT_SUFFIXES:
        return ref
    rel, n = m[1], int(m[2])
    try:
        lines = (Path(root) / rel).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ref
    if not 1 <= n <= len(lines):
        return ref

    def code(i: int) -> str:
        return re.sub(r"\s//.*$", "", lines[i - 1]).strip()

    def comment(s: str) -> bool:
        return not s or s.startswith(("//", "/*", "*"))
    lo = hi = n
    while hi - lo + 1 < STATEMENT_LINES and lo > 1 and not comment(code(lo - 1)):
        if not (_CONT_START.match(code(lo)) or _OPEN_END.search(code(lo - 1))):
            break
        lo -= 1
    while hi - lo + 1 < STATEMENT_LINES and hi < len(lines) and not comment(code(hi + 1)):
        if not (_OPEN_END.search(code(hi)) or _CONT_START.match(code(hi + 1))):
            break
        hi += 1
    return ref if lo == hi else f"{rel}:{lo}-{hi}"


def _snapped_spans(ref: str, root: Path) -> tuple[str, list, list] | None:
    """`(path, asked, drawn)`: the spans the ref names (past a leading comment) and the
    spans `extract-snippet.py` will draw — a single span closed down to its brace — or None
    when the file cannot be read."""
    from ..shared.snippets import _extract_module
    mod = _extract_module()
    try:
        rel, spans = mod.parse_ref(ref)
        lines = (Path(root) / rel).read_text(encoding="utf-8").splitlines()
    except (SystemExit, OSError, UnicodeDecodeError):
        return None
    spans = [(s, min(e, len(lines))) for s, e in spans if s <= len(lines)]
    if len(spans) != 1:
        return rel, spans, spans
    start, end = spans[0]
    start = mod._first_code_line(lines, start, end)
    return rel, [(start, end)], [(start, mod._closing_line(lines, start, end))]


def _spans_ref(rel: str, spans: list[tuple[int, int]]) -> str:
    return rel + ":" + ",".join(f"{s}-{e}" if e != s else f"{s}" for s, e in spans)


def _first_lines(spans: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """The first `n` lines of `spans`."""
    out = []
    for s, e in spans:
        if n <= 0:
            break
        out.append((s, min(e, s + n - 1)))
        n -= out[-1][1] - s + 1
    return out


def _snippet_card_open(ref: str, caption: str | None, root: Path) -> str:
    """One card's quote: the anchor widened to its statement (`widen_anchor`), and at most
    `SNIPPET_LINES` lines open. A window that would draw more — a long range, or a one-line
    anchor on a class opener that the snippet closes 40 lines down (eval run 11:
    OwnerPageRequest.java:20 drew the whole class) — opens on the lines the ref names, up
    to the cap, and folds the rest under a `N more lines` toggle."""
    from ..shared.snippets import snippet_html
    ref = widen_anchor(ref, root)
    got = _snapped_spans(ref, root)
    total = sum(e - s + 1 for s, e in got[2]) if got else 0
    if total <= SNIPPET_LINES:
        return snippet_html(ref, caption, root)
    rel, asked, drawn = got
    head = _first_lines(asked, SNIPPET_LINES)
    last = head[-1][1]
    rest = [(max(s, last + 1), e) for s, e in drawn if e > last]
    more = sum(e - s + 1 for s, e in rest)
    out = snippet_html(_spans_ref(rel, head), caption, root, exact=True)
    if more:
        out += (f'<details class="snipmore"><summary>{more} more line'
                f'{"" if more == 1 else "s"}</summary>'
                + snippet_html(_spans_ref(rel, rest), None, root, exact=True) + "</details>")
    return out


def snippet_card(ref: str, caption: str | None, root: Path) -> str:
    """`_snippet_card_open`, folded when it quotes two lines or more: the same `details.ghfold`
    the diffs wear — file bar as the summary, collapsed — because a reader who reviews the
    code in the editor reads the discussion here, and the excerpt only obscures it. A
    one-line excerpt is as short as its own summary, so it stays inline."""
    out = _snippet_card_open(ref, caption, root)
    if out.count('class="ln-row') < 2:
        return out
    m = re.search(r'<div class="srcbar">.*?</div>\n', out, re.S)
    if not m:
        return out
    bar = m.group(0).strip()
    return ('<details class="ghfold snipfold"><summary>' + bar + '</summary>'
            + out.replace(m.group(0), "", 1) + '</details>')


# There is no `verdict_band_html` any more, and that is the point of this note: the band
# it built — full-bleed amber, the score at 3.4rem, a ten-pip dial, the bullets beside it —
# said the masthead's pill again a screenful lower and spent the first screenful of a review
# on a conclusion, so the list of findings the reader came for started below the fold. The
# `verdict` block in the content file is still read: its `score` is the pill's number and
# its band its colour, once `cap_grade` has lowered it to what the computed signals allow.
# Up to two of its `bullets` join the computed reasons in the grade panel; `summary` is
# dropped from this tab (`drop_model_summary`).


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


# --------------------------------------------------------------------------- #
# Push to GitHub PR — the piles, as inline comments on the pull request
# --------------------------------------------------------------------------- #

#: Written by the agent that wrote `review-points.md` (reference/pr-comments.md), or by
#: `push-pr-comments.py --from-review-points`; sent unchanged by `push-pr-comments.py`.
PR_COMMENTS_JSON = "pr-comments.json"
#: What the last push left behind: `{"comments": {"A:<slug>": {"html_url": …}}, …}`.
PR_POSTED_JSON = "pr-comments.posted.json"
PUSH_PR_ACTION = "__push_pr_comments__"
PUSH_PR_DRY_ACTION = "__push_pr_comments__:dry"
PR_PILE_LETTER = {"autofixes": "F", "findings": "I", "assumptions": "A"}
_PR_SLUG_MAX = 48


def pr_comment_slug(title: str) -> str:
    """The id `push-pr-comments.py` hides in each comment, from the item's title.

    A copy of that script's `slug`, not an import of it: the script is a dataclass module
    and `shared.actions._load` does not register what it loads in `sys.modules`, which
    `@dataclass` needs. `test_push_pr_comments.py` holds the two copies to one answer."""
    text = html.unescape(re.sub(r"<[^>]+>", "", title or "")).lower()
    s = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(s) > _PR_SLUG_MAX:
        s = s[:_PR_SLUG_MAX].rsplit("-", 1)[0]
    return s or "item"


def drop_stale_pr_comments(out_dir: Path, root: Path | None, skill_dir: Path) -> None:
    """Rebuild or delete `pr-comments.json` when its `commit_id` is not on this branch.

    Run 6's review directory kept the previous run's payload, pinned to `0746abc5` of
    another branch, and nothing in the run noticed. Asked of `push-pr-comments.py
    --drop-stale` — the one place that knows how to rebuild it from `review-points.md` —
    on every build, PR or not, so the file on disk is never another branch's."""
    path = Path(out_dir) / PR_COMMENTS_JSON
    script = Path(skill_dir) / "push-pr-comments.py"
    if root is None or not path.is_file() or not script.is_file():
        return
    try:
        rel = str(path.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return
    r = subprocess.run([sys.executable, str(script), "--root", str(root), "--file", rel,
                        "--drop-stale"], capture_output=True, text=True)
    said = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or ("rebuilt" in said or "dropped" in said):
        print(f"[review] {said or f'push-pr-comments.py --drop-stale exit {r.returncode}'}",
              file=sys.stderr)


def prepare_pr_push(spec: dict, out_dir: Path, root: Path, skill_dir: Path) -> dict | None:
    """Declare the two push actions and stamp each pile item with its PR comment's URL.

    Runs right after `resolve_review_points`, when the piles are lists. No payload, no
    button: the page never offers to post what nobody prepared. Each item gets `_ghUrl`
    in place — the same trick as `_snippets` / `_diffs` — so the three renderers need no
    new parameter."""
    spec["_prPush"] = None
    spec["_noPr"] = not pr_exists(spec, out_dir)
    drop_stale_pr_comments(out_dir, root, skill_dir)
    try:
        payload = json.loads((out_dir / PR_COMMENTS_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    comments = payload.get("comments") if isinstance(payload, dict) else None
    script = skill_dir / "push-pr-comments.py"
    if not comments or not script.is_file():
        return None
    if not pr_exists(spec, out_dir):
        # No pull request, nothing to post to: `push-pr-comments.py` would exit 2 on
        # "no PR for this branch", after the reader had pressed a button the page offered.
        print("[review] no pull request named in content.json (`pr.number` / `pr.url`) — "
              "the Review tab offers no 'Publish on GitHub'", file=sys.stderr)
        return None
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return None
    here, py = shlex.quote(str(root.resolve())), shlex.quote(sys.executable)
    push = (f"{py} {shlex.quote(str(script))}"
            f" --file {shlex.quote(rel + '/' + PR_COMMENTS_JSON)}")
    refresh = (f"{py} {shlex.quote(str(skill_dir / 'refresh-report.py'))}"
               f" --dir {shlex.quote(rel)} --steps reviewpoints,aftermath --no-serve")
    declare_action(PUSH_PR_DRY_ACTION, f"cd {here} && {push} --dry-run",
                   label="List the comments a push would post")
    # The push alone, no rebuild after it: the page reads `pr-comments.posted.json` back
    # from the server and puts the ↗ beside every item itself (`PR_PUSH_JS`). It used to be
    # `push && refresh`: PR #51's push timed out half-way, `&&` skipped the refresh, and the
    # page went on saying nothing had been posted while 41 comments sat on the PR — and on
    # a success the rebuild rewrote review.html under the reader. The next build bakes the
    # links in anyway, from the same record.
    del refresh
    declare_action(PUSH_PR_ACTION, f"cd {here} && {push}",
                   label="Post the Review tab's items as comments on the pull request")
    try:
        posted = json.loads((out_dir / PR_POSTED_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        posted = {}
    urls = {cid: (c or {}).get("html_url")
            for cid, c in (posted.get("comments") or {}).items()}
    modes = {cid: (c or {}).get("mode") for cid, c in (posted.get("comments") or {}).items()}
    for key, letter in PR_PILE_LETTER.items():
        for item in spec.get(key) or []:
            cid = f"{letter}:{pr_comment_slug(item.get('title', ''))}"
            if urls.get(cid):
                item["_ghUrl"] = urls[cid]
                item["_ghInSummary"] = modes.get(cid) == "body"
                # Where the thread sits, for the click that opens the card's lines in VS
                # Code to bring it up there too (`gh_comment_link`'s data-pr-*).
                c = (posted.get("comments") or {}).get(cid) or {}
                if c.get("path"):
                    item["_ghFile"] = (f"vscode://file/{root.resolve()}/{c['path']}"
                                       f":{c.get('line') or 1}:1")
                    item["_ghWhere"] = f"{c['path']}:{c.get('line') or 1}"
                if not item["_ghInSummary"] and c.get("path") and c.get("line"):
                    item["_ghThread"] = {"path": c["path"], "line": c["line"],
                                         "start": c.get("start_line") or c["line"]}
    counts = {p: sum(1 for c in comments if c.get("pile") == p)
              for p in ("fixed", "ignored", "assumption")}
    spec["_prPush"] = {"count": len(comments), "counts": counts,
                       "posted": sum(1 for u in urls.values() if u),
                       "pushedAt": posted.get("pushed_at"),
                       "reviewUrl": posted.get("review_url"), "prUrl": posted.get("url"),
                       # A push that stopped half-way says so on the page until a retry
                       # completes it — the record is what is on GitHub, not what was meant.
                       "status": posted.get("status") or (
                           "complete" if urls and all(urls.values()) else None),
                       "message": posted.get("message"),
                       # Edited since it went out: the payload is not the one the record
                       # was posted from, so "Published" gets a small re-publish beside it.
                       "changed": _payload_changed(out_dir, posted)}
    return spec["_prPush"]


def _payload_changed(out_dir: Path, posted: dict) -> bool:
    """Whether `pr-comments.json` is no longer the file the record was pushed from. Only a
    record that says what it was pushed from (`payload_sha256`) can answer yes."""
    want = (posted or {}).get("payload_sha256")
    if not want:
        return False
    try:
        return hashlib.sha256((out_dir / PR_COMMENTS_JSON).read_bytes()).hexdigest() != want
    except OSError:
        return False


def pr_exists(spec: dict, out_dir: Path) -> bool:
    """Whether there is a pull request to post to: the content file names one (a number,
    or a `/pull/` URL), or an earlier push left its receipt. Read off what the run already
    knows rather than asked of GitHub at build time — the build runs offline too."""
    pr = spec.get("pr") or {}
    if pr.get("number") or "/pull/" in str(pr.get("url") or ""):
        return True
    return (out_dir / PR_POSTED_JSON).is_file()


#: The GitHub Pull Requests extension's URI handler (microsoft/vscode-pull-request-github,
#: `src/common/uri.ts` `UriHandlerPaths.OpenPullRequestWebview`, parsed by
#: `fromOpenOrCheckoutPullRequestWebviewUri`): `?uri=` must be the bare PR URL — its regex
#: is anchored at `/pull/<n>$`, so a `#discussion_r…` would fail it. It opens the PR's
#: overview in the window that takes the URI, without a prompt; no path of the handler
#: takes a comment or a thread, so the PR is as close as the link can aim.
VSCODE_PR_URI = "vscode://github.vscode-pull-request-github/open-pull-request-webview?uri="
_PR_URL = re.compile(r"^(https://github\.com/[^/#?]+/[^/#?]+/pull/\d+)")


#: The VS Code mark, in its blue, in front of every *in VS Code*.
VSC_ICON = ('<svg class="vsc-ico" viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="#007ACC" d="M23.15 2.587L18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128a.999.999 0 0 0-1.276.057L.327 7.261A1 1 0 0 0 .326 8.74L3.899 12 .326 15.26a1 1 0 0 0 .001 1.479L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352zm-5.146 14.861L10.826 12l7.178-5.448v10.896z"/></svg>')


def vscode_pr_uri(url: str | None) -> str | None:
    """The PR an `html_url` belongs to, as the extension's open-in-VS-Code URI."""
    m = _PR_URL.match(url or "")
    return VSCODE_PR_URI + urllib.parse.quote(m.group(1), safe="") if m else None


def vscode_pr_link(url: str | None, cls: str = "f-vsc") -> str:
    """*in VS Code* — the same PR in the GitHub Pull Requests view. Served, the page first
    brings forward the VS Code window on this checkout (`PR_PUSH_JS`), so the URI lands
    there and not in whichever window was used last."""
    uri = vscode_pr_uri(url)
    if not uri:
        return ""
    n = _PR_URL.match(url).group(1).rsplit("/", 1)[1]
    return (f' <a class="{cls}" href="{html.escape(uri, quote=True)}" '
            f'data-tip="Open PR #{n} in VS Code (GitHub Pull Requests)">{VSC_ICON} in VS Code</a>')


def vscode_file_link(href: str | None, where: str) -> str:
    """*in VS Code* beside an item's *on GitHub ↗*: the commented file at the comment's line,
    as every other reference on the page opens one — editor.js sends it to the VS Code
    window on this checkout, and because the card names its thread (`data-pr-*` on the ↗)
    it asks for the thread too, which the GitHub Pull Requests extension renders inline
    under the line when the PR's branch is checked out."""
    if not href:
        return ""
    return (f' <a class="f-vsc" href="{html.escape(href, quote=True)}" '
            f'data-tip="{html.escape(f"Open {where} in VS Code, with the PR comment", quote=True)}">'
            f'{VSC_ICON} in VS Code</a>')


def gh_comment_link(f) -> str:
    """*on GitHub ↗* beside an item's title, once it has a comment on the PR. The words
    say where the arrow goes; a bare ↗ read as "open this item" and left the reader
    guessing."""
    url = f.get("_ghUrl")
    if not url:
        return ""
    # A line the PR's diff does not show cannot carry a review comment; the push quotes it
    # in the review's summary instead, with a permalink to the lines, and the link says so.
    tip = ("In the review's summary on GitHub — this line is not in the PR's diff"
           if f.get("_ghInSummary") else "PR comment")
    # An inline thread says where it sits, so editor.js can ask VS Code to bring it up —
    # expanded and focused — when the reader opens the card's lines (`comment=1`). A
    # summary-only item has no thread in the file, so it says nothing.
    t = f.get("_ghThread") or {}
    where = (f' data-pr-path="{html.escape(str(t["path"]), quote=True)}"'
             f' data-pr-line="{int(t["line"])}" data-pr-start="{int(t["start"])}"'
             if t.get("path") and t.get("line") else "")
    return (f' <a class="f-gh" href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener" data-tip="{html.escape(tip, quote=True)}"{where}>on GitHub ↗</a>'
            + vscode_file_link(f.get("_ghFile"), f.get("_ghWhere") or "the file"))


#: The last line `push-pr-comments.py` prints after `--dry-run` and after a push: one JSON
#: object, the only part of its output the page reads.
PR_PREVIEW_TAG = "::hr-push-preview::"
PR_RESULT_TAG = "::hr-push-result::"

# Run on DOMContentLoaded, not inline: this sits in the Review tab, far above the page's
# own scripts, and `window.HR` (server.js) does not exist yet where it is parsed — an
# inline IIFE returned early and the button was never raised.
#
# The confirmation asks one question in the reader's words — "Post 13 comments on GitHub
# PR #51 as @victorrentea?" — and keeps the comments themselves one click away. It used to
# show the dry run's `gh api` calls, "done on GitHub in my name": the right facts in the
# wrong language. Failures land on the counts line, in words, with Retry on the button —
# never an alert() quoting a command line, and never a button left on "Posting…".
#
# Written so it can also be dropped into a page built before it (a live patch): it
# rebuilds the dialog's inside and the message slot when they are the old ones, and swaps
# the button for a clone, which sheds the old click handler.
PR_PUSH_JS = """<script>(function () {
  // *in VS Code*: bring the window on this checkout forward first (served), then hand the
  // OS the extension's URI — VS Code gives a URI to the window used last. In the capture
  // phase, ahead of editor.js, which takes every vscode: link for a file reference.
  var token = null;
  if (window.HR) HR.onready(function (caps) { token = caps && caps.token; });
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest && ev.target.closest('a.pr-vsc');
    if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
    ev.preventDefault(); ev.stopPropagation();
    var href = a.getAttribute('href');
    function go() { window.location.href = href; }
    if (!token) return go();
    fetch('/__editor_open__', {method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-Human-Review-Token': token},
      body: '{}'})
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (j) { setTimeout(go, j && j.how === 'focused' ? 250 : 1500); }, go);
  }, true);
})();
var HR_VSC_PR = '""" + VSCODE_PR_URI + """';
var HR_VSC_ICON = '""" + VSC_ICON + """';
function hrVscPr(url) {
  var m = /^(https:\\/\\/github\\.com\\/[^\\/#?]+\\/[^\\/#?]+\\/pull\\/\\d+)/.exec(url || '');
  return m ? HR_VSC_PR + encodeURIComponent(m[1]) : null;
}
document.addEventListener('DOMContentLoaded', function(){
  var old = document.querySelector('.pr-push');
  if (!old || !window.HR) return;
  var b = old.cloneNode(true); old.replaceWith(b);
  var dlg = document.getElementById('pr-push-dlg');
  if (!dlg.querySelector('.pp-q')) dlg.innerHTML = '<form method="dialog">'
    + '<p class="pp-q"></p><p class="pp-sub"></p>'
    + '<details class="pp-what"><summary>Show what will be posted</summary>'
    + '<ol class="pp-list"></ol></details>'
    + '<menu><button value="cancel">Cancel</button> '
    + '<button value="post" class="primary">Post</button></menu></form>';
  var line = b.parentNode;
  var msg = line.querySelector('.pr-push-msg');
  if (!msg) { msg = document.createElement('span'); msg.className = 'pr-push-msg';
    msg.setAttribute('role', 'status'); msg.hidden = true; b.after(msg); }
  var rep = line.querySelector('.pr-repub');
  if (!rep) { rep = document.createElement('button'); rep.type = 'button';
    rep.className = 'pr-repub'; rep.textContent = 're-publish'; rep.hidden = true;
    rep.dataset.tip = 'The comments changed since they were published \\u2014 post the '
      + 'difference; nothing already there is posted twice';
    b.after(rep); }
  var vsc = line.querySelector('.pr-vsc');
  if (!vsc) { vsc = vscLink('pr-vsc', HR_VSC_PR); vsc.hidden = true; b.after(vsc); }
  var face = b.dataset.face || 'Publish on GitHub';
  var rec = null, changed = false;
  function tagged(out, tag) {
    var ls = String(out || '').split('\\n');
    for (var i = ls.length - 1; i >= 0; i--) if (ls[i].indexOf(tag) === 0) {
      try { return JSON.parse(ls[i].slice(tag.length)); } catch (e) { return null; } }
    return null;
  }
  function say(text, kind) {
    msg.textContent = text || ''; msg.hidden = !text;
    msg.className = 'pr-push-msg' + (kind ? ' pp-' + kind : '');
  }
  function n(k, w) { return k + ' ' + w + (k === 1 ? '' : 's'); }
  function el(tag, cls, text) {
    var e = document.createElement(tag); if (cls) e.className = cls;
    if (text != null) e.textContent = text; return e;
  }
  // push-pr-comments.py's `slug`, on the title as the page shows it (tags gone, entities
  // read): the key of each item in the record.
  function slug(t) {
    var s = String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (s.length > 48) { s = s.slice(0, 48); var k = s.lastIndexOf('-'); if (k > 0) s = s.slice(0, k); }
    return s || 'item';
  }
  // The ↗ beside every item the record says is on GitHub — what a rebuild would bake in,
  // put there without one.
  function decorate(r) {
    var by = {};
    Object.keys(r.comments || {}).forEach(function (cid) {
      by[cid.replace(/^[A-Za-z]:/, '')] = r.comments[cid]; });
    document.querySelectorAll('.f-title').forEach(function (t) {
      var c = by[slug(t.textContent)];
      if (!c || !c.html_url) return;
      var a = t.nextElementSibling;
      if (!a || !a.classList.contains('f-gh')) {
        a = el('a', 'f-gh', 'on GitHub \\u2197'); a.target = '_blank'; a.rel = 'noopener';
        t.after(document.createTextNode(' '), a);
      }
      a.href = c.html_url;
      a.dataset.tip = c.mode === 'body'
        ? 'In the review\\u2019s summary on GitHub \\u2014 this line is not in the PR\\u2019s diff'
        : 'PR comment';
      if (c.mode !== 'body' && c.path && c.line) {
        a.dataset.prPath = c.path; a.dataset.prLine = c.line;
        a.dataset.prStart = c.start_line || c.line; }
      // The commented file at the comment's line, opened the way every reference on the
      // page is (editor.js): in the checkout's window, the thread brought up with it.
      var root = (document.documentElement.dataset.hrRoot || '').replace(/\\/+$/, '');
      if (!root || !c.path) return;
      var where = c.path + ':' + (c.line || 1);
      var v = a.nextElementSibling;
      if (!(v && v.classList.contains('f-vsc'))) {
        v = vscLink('f-vsc', '', ''); a.after(document.createTextNode(' '), v); }
      v.href = 'vscode://file/' + root + '/' + where + ':1';
      v.dataset.tip = 'Open ' + where + ' in VS Code, with the PR comment';
    });
  }
  function vscLink(cls, uri, tip) {
    var v = el('a', cls); v.innerHTML = HR_VSC_ICON + ' in VS Code';
    if (uri) v.href = uri;
    v.dataset.tip = tip != null ? tip : 'Open PR #' + decodeURIComponent(uri).split('/').pop()
      + ' in VS Code (GitHub Pull Requests)';
    return v;
  }
  // A record from before pushes said how they ended: complete when every item has its link.
  function status(r) {
    if (!r) return null;
    if (r.status) return r.status;
    var cs = Object.keys(r.comments || {}).map(function (k) { return r.comments[k] || {}; });
    return cs.length && cs.every(function (c) { return c.html_url; }) ? 'complete' : null;
  }
  function show() {
    var st = status(rec);
    if (st === 'complete' && (rec.review_url || rec.url)) {
      b.textContent = 'Published on GitHub \\u2197'; b.dataset.state = 'done';
      b.dataset.tip = 'Open the review on GitHub' + (rec.pushed_at
        ? ' \\u2014 published ' + rec.pushed_at.slice(0, 16).replace('T', ' ') : '');
      rep.hidden = !changed;
      var uri = hrVscPr(rec.url || rec.review_url);
      if (uri) { vsc.href = uri; vsc.dataset.tip = 'Open PR #' + (rec.pr || '')
        + ' in VS Code (GitHub Pull Requests)'; vsc.hidden = false; }
    } else if (st === 'partial') {
      b.textContent = 'Retry'; b.dataset.state = 'retry'; rep.hidden = true;
      say(rec.message, 'err');
    } else { b.textContent = face; b.dataset.state = ''; rep.hidden = true; }
    b.disabled = false; rep.disabled = false;
  }
  function sha(text) {
    if (text == null || !(window.crypto && crypto.subtle && window.TextEncoder))
      return Promise.resolve(null);
    return crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)).then(function (h) {
      return Array.prototype.map.call(new Uint8Array(h), function (x) {
        return ('0' + x.toString(16)).slice(-2); }).join(''); });
  }
  function load() {
    return fetch('pr-comments.posted.json', {cache: 'no-store'})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (r) {
        if (!r) return;
        rec = r; decorate(r);
        if (!r.payload_sha256) { changed = false; return; }
        return fetch('pr-comments.json', {cache: 'no-store'})
          .then(function (x) { return x.ok ? x.text() : null; }).then(sha)
          .then(function (h) { changed = !!h && h !== r.payload_sha256; });
      }).catch(function () {}).then(show);
  }
  HR.onready(function () { if (HR.can(b.dataset.push)) { b.hidden = false; load(); } });
  function done(text, kind) { show(); if (text) say(text, kind); }
  function publish(from) {
    b.disabled = rep.disabled = true; from.textContent = 'Checking GitHub\\u2026'; say('');
    var label = from === rep ? 're-publish' : null;
    function back(text, kind) { if (label) rep.textContent = label; done(text, kind); }
    HR.run(b.dataset.dry).then(function (s) {
      var pv = tagged(s.output, '""" + PR_PREVIEW_TAG + """');
      if (s.exit !== 0 || !pv) return back('Could not read the pull request from GitHub '
        + '\\u2014 check that gh is logged in, then try again.', 'err');
      var items = pv.items || [];
      if (!items.length) return back('All ' + n(pv.total, 'comment') + ' are already on '
        + 'GitHub PR #' + pv.pr + '.', 'ok');
      dlg.querySelector('.pp-q').textContent = 'Post ' + n(items.length, 'comment')
        + ' on GitHub PR #' + pv.pr + (pv.user ? ' as @' + pv.user : '') + '?';
      var edits = items.filter(function (i) { return i.fate === 'update'; }).length;
      var sub = [];
      if (pv.already) sub.push(n(pv.already, 'comment') + ' already there stay as they are.');
      if (edits) sub.push(edits + ' of these edit a comment already there.');
      var subEl = dlg.querySelector('.pp-sub');
      subEl.textContent = sub.join(' '); subEl.hidden = !sub.length;
      var list = dlg.querySelector('.pp-list'); list.textContent = '';
      items.forEach(function (i) {
        var li = el('li');
        li.appendChild(el('code', 'pp-where', i.where));
        li.appendChild(el('div', 'pp-text', i.text));
        list.appendChild(li);
      });
      dlg.querySelector('.pp-what').open = false;
      if (label) rep.textContent = label;
      show(); b.disabled = rep.disabled = true;
      dlg.showModal();
      dlg.addEventListener('close', function once() {
        dlg.removeEventListener('close', once);
        if (dlg.returnValue !== 'post') return back();
        b.textContent = 'Posting\\u2026';
        say('Posting ' + n(items.length, 'comment') + ' on GitHub \\u2014 this can take a '
          + 'minute.', 'info');
        HR.run(b.dataset.push).then(function (s2) {
          var r = tagged(s2.output, '""" + PR_RESULT_TAG + """');
          var text = r ? r.message : (s2.exit === 0 ? 'Posted.' : 'Posting stopped before '
            + 'GitHub answered \\u2014 click Retry; nothing already there is posted twice.');
          load().then(function () {
            if (s2.exit !== 0 && !(rec && rec.status === 'partial')) {
              b.textContent = 'Retry'; b.dataset.state = 'retry'; }
            say(text, s2.exit === 0 ? 'ok' : 'err');
          });
        }, function () {
          back('Lost the review server while posting \\u2014 reload the page to see what '
            + 'reached GitHub.', 'err');
        });
      });
    }, function () {
      back('The review server did not answer \\u2014 is it still running?', 'err');
    });
  }
  b.addEventListener('click', function () {
    if (b.dataset.state === 'done' && rec) {
      window.open(rec.review_url || rec.url, '_blank', 'noopener'); return; }
    publish(b);
  });
  rep.addEventListener('click', function () { publish(rep); });
});</script>"""


def push_pr_button(spec) -> str:
    """*Publish on GitHub*, at the end of the Review tab's sticky counts line, and the slot
    after it where the outcome is said in words.

    Hidden until the probe says this server can run it — off disk, in the zip and on
    GitHub Pages there is nothing to post with. A press asks GitHub what is already there
    (`--dry-run`), then asks the reader one question — post N comments as @them? — with the
    comments a click away; only *Post* sends anything, under the reader's own `gh` login.
    Once everything is on the PR it reads *Published on GitHub ↗* and opens the review,
    with a small *re-publish* beside it only when the comments changed since; a push that
    stopped half-way leaves it on *Retry* with the record's sentence beside it. The page
    keeps all three true without a rebuild, from the record the server serves."""
    pp = spec.get("_prPush")
    if not pp:
        return ""
    c = pp["counts"]
    again = pp["posted"] > 0
    face = "Publish on GitHub"
    partial = pp.get("status") == "partial"
    done = pp.get("status") == "complete" and bool(pp.get("reviewUrl") or pp.get("prUrl"))
    n = c['fixed'] + c['ignored'] + c['assumption']
    if done:
        label, state = "Published on GitHub ↗", "done"
        tip = "Open the review on GitHub" + (
            f" — published {pp['pushedAt'][:16].replace('T', ' ')}" if pp.get("pushedAt") else "")
    else:
        label, state = ("Retry", "retry") if partial else (face, "")
        tip = (f"{n} inline PR comment{'' if n == 1 else 's'}. Asks before posting; "
               "re-pushing posts only what is missing, never twice."
               + (f" Last pushed {pp['pushedAt'][:16].replace('T', ' ')}."
                  if again and pp.get("pushedAt") else ""))
    note = pp.get("message") if partial else ""
    repub = done and pp.get("changed")
    vsc = vscode_pr_link(pp.get("prUrl") or pp.get("reviewUrl"), "pr-vsc") if done else ""
    return (f' <button type="button" class="pr-push" hidden '
            f'data-dry="{PUSH_PR_DRY_ACTION}" data-push="{PUSH_PR_ACTION}" '
            f'data-face="{html.escape(face, quote=True)}" data-state="{state}" '
            f'data-tip="{html.escape(tip, quote=True)}">{html.escape(label)}</button>'
            + vsc.replace("<a ", "<a hidden ", 1)
            + f'<button type="button" class="pr-repub"{"" if repub else " hidden"} '
            'data-tip="The comments changed since they were published — post the difference; '
            'nothing already there is posted twice">re-publish</button>'
            f'<span class="pr-push-msg{" pp-err" if note else ""}" role="status"'
            f'{"" if note else " hidden"}>{html.escape(note or "")}</span>')


#: Said on hover of the counts line, where the publish button would be, when there is no pull
#: request: run 6 simply had no button and no `on GitHub ↗` links, and a reader comparing
#: it with a page that had them saw controls missing with no reason given.
NO_PR_LINE = "No pull request yet — no GitHub links or publishing."


def no_pr_line(spec) -> str:
    """` data-tip="No pull request yet…"` for the counts line, or nothing.

    A hover on the line the publish button would end, not a line of its own: eval run 11
    counted it among the visible one-liners a busy reviewer reads past."""
    if not spec.get("_noPr") or spec.get("_prPush"):
        return ""
    return f' data-tip="{html.escape(NO_PR_LINE, quote=True)}"'


def push_pr_dialog(spec) -> str:
    """The one question before posting, and the comments a click away — after the counts
    line, not in it: a `<dialog>` inside a `<p>` is not HTML the parser keeps where it was
    written. Filled by `PR_PUSH_JS` from the dry run's last line, never from its calls."""
    if not spec.get("_prPush"):
        return ""
    return ('<dialog id="pr-push-dlg" class="pr-push-dlg"><form method="dialog">'
            '<p class="pp-q"></p><p class="pp-sub"></p>'
            '<details class="pp-what"><summary>Show what will be posted</summary>'
            '<ol class="pp-list"></ol></details>'
            '<menu><button value="cancel">Cancel</button> '
            '<button value="post" class="primary">Post</button></menu></form></dialog>'
            + PR_PUSH_JS)
