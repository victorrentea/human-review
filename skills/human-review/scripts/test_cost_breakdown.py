#!/usr/bin/env python3
"""The cost has to be *visible*, and that is the part that keeps going missing.

`review-cost.py --tab-costs` and `steps-ledger.py` have had tests since the day they were
written, and both were green for the whole period during which the measurement they produce
reached nobody at all: its only surface was a `data-tip` on each tab header, and when tab
tooltips were removed the emission went with them. The subprocess still ran on every build.
The number still came back correct. The page showed nothing.

So the tests here are deliberately at the other end of the pipe. They do not check that the
arithmetic is right (test_review_cost.py owns that) — they check that the arithmetic reaches
the HTML, with the right numbers in it, from a real `build-review-html.py` run over a real
transcript and a real step ledger. A rendering regression of the exact kind this file exists
to catch cannot be silent again: the end-to-end test fails if the ledger tab is absent, and
it also fails if the tab is present but empty.

The surface has moved twice now. It was a `data-tip` per tab header; then a drawer under a
scope-bar chip; it is a tab of its own, labelled with the money, because the review's own
cost turned out to be the smaller half of the answer — the conversation that WROTE the code
is on the same disk and costs more.

Run with:  python3 -m pytest test_cost_breakdown.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _tab(tid, label):
    return {"id": tid, "label": label}


def _row(cost=0.0, tokens=0, measured=True, messages=1):
    return {"measured": measured, "cost": cost, "tokens": tokens,
            "messages": messages, "tip": "…"}


def _report(tabs: dict, residual=None, reason=None):
    return {"available": True, "ledger": True, "reason": reason, "tabs": tabs,
            "residual": residual or {"measured": False, "cost": 0.0, "tokens": 0,
                                     "messages": 0, "tip": "…"}}


def _ledger(tabs_report, writing=None, passes=None, run=None, total=0.0, tokens=0):
    """A whole bill around a per-tab report. The tab rows are what most of these tests are
    about, so everything else defaults to the shape a run with nothing else to say produces
    — which is itself a case worth keeping exercised."""
    return {"tabs": tabs_report,
            "writing": writing or {"measured": False, "reason": "not a git repository",
                                   "sessions": [], "cost": 0.0, "tokens": 0},
            # A measured run by default: that is what a page built by the session that
            # reviewed it has, and without it the table declines to render at all — which
            # is its own behaviour, pinned by its own two tests below.
            "run": run or {"measured": True, "cost": total, "tokens": tokens,
                           "messages": 1, "models": ["Opus 5"]},
            "passes": passes or {"measured": True, "groups": {}, "inline": 0},
            "total": total, "total_tokens": tokens}


def _table(tabs_report, tabs, **kw):
    return build.cost_ledger_html(_ledger(tabs_report, **kw), tabs)


# --------------------------------------------------------------------------- #
# cost_ledger_html — the shape of the table
# --------------------------------------------------------------------------- #

def test_a_tab_with_spend_gets_its_own_row_with_both_numbers():
    html = _table(
        _report({"review": _row(cost=12.5, tokens=1_400_000)}),
        [_tab("review", "🤖 Review")],
    )
    assert "🤖 Review" in html
    assert "$12.50" in html
    assert "1.4M" in html


def test_the_rows_are_ordered_by_spend_not_by_tab_order():
    html = _table(
        _report({"a": _row(cost=1.0, tokens=10_000),
                 "b": _row(cost=9.0, tokens=90_000)}),
        [_tab("a", "Cheap"), _tab("b", "Dear")],
    )
    assert html.index("Dear") < html.index("Cheap"), (
        "the breakdown exists to answer 'where did the money go' — the biggest row has to "
        "be the first one read")


def test_measured_zero_tabs_collapse_into_one_honest_row():
    """A zero is information: a script produced that tab, so it cost nothing. But six of
    them stacked above the two rows that carry the money is a wall, so they fold into one
    row that still names every one of them."""
    html = _table(
        _report({"review": _row(cost=4.0, tokens=100_000),
                 "data": _row(cost=0.0, tokens=0),
                 "api": _row(cost=0.0, tokens=0),
                 "owners": _row(cost=0.0, tokens=0)}),
        [_tab("review", "Review"), _tab("data", "Data"),
         _tab("api", "API"), _tab("owners", "CODEOWNERS")],
    )
    assert "3 tabs with no model spend" in html
    for name in ("Data", "API", "CODEOWNERS"):
        assert name in html, f"the zero row must still name {name}"
    assert "$0.00" in html, "a measured zero prints as $0.00, never as <$0.01"
    # Counted inside the tab section alone: the groups above it have rows of their own,
    # and what this test is about is the folding of the zeros.
    tabs_part = html.split("building this guide")[1]
    assert len(re.findall(r"<tr(?! class=\"costtotal\").*?</tr>", tabs_part, re.S)) == 2, (
        "the spend row and the zero row: the zero tabs must occupy exactly one row "
        "between them")
    assert "costtotal" in html


def test_a_single_zero_tab_says_tab_not_tabs():
    html = _table(
        _report({"review": _row(cost=4.0, tokens=100_000), "data": _row()}),
        [_tab("review", "Review"), _tab("data", "Data")],
    )
    assert "1 tab with no model spend — Data" in html


def test_unmeasured_tabs_never_render_as_a_measured_zero():
    """The failure this whole feature guards against: "we could not measure this" looking
    exactly like "this measured zero"."""
    html = _table(
        _report({"review": _row(cost=4.0, tokens=100_000),
                 "logging": _row(measured=False)},
                reason="no step ledger at .human-review/.steps.json"),
        [_tab("review", "Review"), _tab("logging", "Logging")],
    )
    assert "1 tab not measured" in html
    assert "no step ledger" in html
    assert "Logging" in html
    # The em-dash placeholder, not a number: nothing was counted, so nothing is claimed.
    assert "<td>—</td><td>—</td>" in html


def test_the_residual_and_the_total_are_both_shown():
    html = _table(
        _report({"a": _row(cost=2.0, tokens=20_000), "b": _row(cost=3.0, tokens=30_000)},
                residual={"measured": True, "cost": 1.25, "tokens": 5_000,
                          "messages": 4, "tip": "…"}),
        [_tab("a", "A"), _tab("b", "B")], total=6.25,
    )
    assert "$1.25" in html                      # the residual
    assert "not one tab's" in html
    assert "$6.25" in html                      # 2.00 + 3.00 + 1.25
    assert "costtotal" in html


def test_no_tab_is_exempt_from_the_ledger_by_name_any_more():
    """`overview` used to be skipped here: it was synthesised from the other tabs, so a
    permanent "not measured" row for it was noise about a tab with no step by design. The
    tab is gone — the summary and the verdict open the first tab now — and with it the
    exemption, so an unmeasured tab is reported like any other whatever it is called."""
    html = _table(
        _report({"overview": _row(measured=False), "review": _row(cost=1.0, tokens=1000)}),
        [_tab("overview", "Overview"), _tab("review", "Review")],
    )
    assert "Overview" in html
    assert "1 tab not measured" in html


def test_nothing_measured_anywhere_means_no_tab():
    """A pill reading `$0` claims this change was free. The absence of a measurement is
    not a measurement of zero, so the page carries no tab at all."""
    assert _table({}, [], run={"measured": False, "cost": 0.0, "tokens": 0,
                              "messages": 0, "models": []}) == ""


def test_either_half_alone_is_enough_to_keep_the_tab():
    """A page rebuilt outside the session that reviewed it can still say what writing the
    code cost, and losing one half must not lose the other."""
    assert _table({}, [], writing=WROTE_IT, total=648.23,
                  run={"measured": False, "cost": 0.0, "tokens": 0,
                       "messages": 0, "models": []}) != ""


def test_no_ledger_at_all_means_no_tab():
    """`review-cost.py` could not be asked — not "it answered nothing". The page then has
    no cost tab rather than one that says nothing, because a tab labelled `$0` is a claim
    and this is the absence of one."""
    assert build.cost_ledger_html(None, [_tab("a", "A")]) == ""


def test_a_ledger_with_no_tab_report_still_renders_the_rest_of_the_bill():
    """The per-tab half is the part that needs a step ledger; what the code cost to write
    does not. Losing one must not lose the other."""
    html = _table({}, [], writing=WROTE_IT, total=648.23)
    assert "$648.23" in html
    assert "no step ledger" in html


def test_a_ledgerless_run_still_says_so_rather_than_showing_nothing():
    """The whole run unmeasured is the case that used to render as silence. It renders as
    a sentence: one row, with the reason in it."""
    html = _table(
        _report({t: _row(measured=False) for t in ("a", "b", "c")},
                reason="no session id ($CLAUDE_CODE_SESSION_ID unset)"),
        [_tab("a", "A"), _tab("b", "B"), _tab("c", "C")],
    )
    assert html
    assert "3 tabs not measured" in html
    assert "no session id" in html


# --------------------------------------------------------------------------- #
# the three acts — writing it, reviewing it, building this guide
# --------------------------------------------------------------------------- #

WROTE_IT = {
    "measured": True, "mode": "B", "weak": False, "cost": 648.23, "tokens": 1_044_604_362,
    "sessions": [{"session": "f98b888f-1111-2222-3333-444444444444", "cost": 648.23,
                  "tokens": 1_044_604_362, "messages": 4544, "subagents": 3,
                  "models": ["Opus 5", "Sonnet 5"], "first": "2026-09-02T15:41:21Z",
                  "last": "2026-09-03T12:50:37Z", "edits": 25, "bash": 6, "files": 15,
                  "current": False}],
}


def test_the_conversation_that_wrote_the_code_is_the_first_row():
    """The number the page could never state and the reader most wants. Reviewing is the
    cheaper half of what a change costs, and it is the half a review page used to report
    alone — which quietly answered "what did this PAGE cost" instead."""
    html = _table(_report({"review": _row(cost=4.0, tokens=100_000)}),
                  [_tab("review", "Review")], writing=WROTE_IT, total=652.23)
    assert "writing the code" in html
    assert html.index("writing the code") < html.index("building this guide")
    assert "$648.23" in html
    assert "f98b888f" in html, "the conversation is named, so the reader can go and look"


def test_the_authoring_row_prints_the_window_it_was_costed_over():
    """A window is not a fence: work inside it that belonged to something else is counted.
    The row prints the window precisely so nobody reads the number as tighter than it is."""
    html = _table(_report({"review": _row(cost=4.0, tokens=100_000)}),
                  [_tab("review", "Review")], writing=WROTE_IT)
    assert "2 Sep 15:41" in html and "3 Sep 12:50" in html
    assert "25 edits across 15 files" in html


def test_an_unmeasurable_author_says_so_instead_of_printing_a_zero():
    """Mode C — a branch from somebody else, or transcripts since cleaned up. A `$0.00`
    there would read as "writing this was free", which is the one thing it does not mean."""
    html = _table(_report({"review": _row(cost=4.0, tokens=100_000)}),
                  [_tab("review", "Review")],
                  writing={"measured": False, "sessions": [], "cost": 0.0, "tokens": 0,
                           "reason": "no conversation on disk wrote these files"})
    assert "no conversation on disk wrote these files" in html
    assert "$0.00" not in html.split("building this guide")[0], \
        "a zero under `writing the code` would read as `writing this was free`"


def test_a_shell_only_author_is_flagged_rather_than_trusted():
    weak = {**WROTE_IT, "weak": True,
            "sessions": [{**WROTE_IT["sessions"][0], "edits": 0, "bash": 5}]}
    html = _table(_report({"review": _row(cost=1.0, tokens=1000)}),
                  [_tab("review", "Review")], writing=weak)
    assert "no conversation used the edit tools" in html
    assert "5 shell writes across 15 files" in html


def test_the_passes_are_split_into_finding_and_fixing():
    passes = {"measured": True, "inline": 0, "groups": {
        "finding": {"cost": 4.91, "earlier": 4.91, "tokens": 4_500_000, "messages": 46,
                    "passes": 2, "invoked": ["/code-review origin/main"]},
        "fixing": {"cost": 2.10, "earlier": 2.10, "tokens": 900_000, "messages": 12,
                   "passes": 1, "invoked": ["/simplify"]},
    }}
    html = _table(_report({"review": _row(cost=1.0, tokens=1000)}),
                  [_tab("review", "Review")], passes=passes)
    assert "reviewing it" in html, "the group caption"
    assert "the passes that read the diff" in html
    assert "the passes that applied what they found" in html
    assert "$4.91" in html and "$2.10" in html
    assert "/code-review origin/main" in html and "/simplify" in html
    assert html.index("read the diff") < html.index("applied what they found"), \
        "found first, then fixed — the order the work happened in"


def test_a_pass_already_inside_the_run_says_it_is_not_added_twice():
    """A pass fired mid-run is inside the run's own total. Its row still shows the whole
    number — it is what that pass cost — and says where the arithmetic put it."""
    passes = {"measured": True, "inline": 0, "groups": {
        "finding": {"cost": 5.00, "earlier": 1.00, "tokens": 1000, "messages": 4,
                    "passes": 1, "invoked": ["/code-review"]}}}
    html = _table(_report({"review": _row(cost=1.0, tokens=1000)}),
                  [_tab("review", "Review")], passes=passes)
    assert "already inside the run below" in html


def test_an_inline_pass_is_counted_and_named_rather_than_estimated():
    passes = {"measured": True, "inline": 4, "groups": {}}
    html = _table(_report({"review": _row(cost=1.0, tokens=1000)}),
                  [_tab("review", "Review")], passes=passes)
    assert "4 passes ran in this conversation rather than forking" in html
    assert "no transcript of their own to price" in html


def test_the_total_is_the_ledger_s_own_not_a_sum_of_the_rows():
    """The rows are not disjoint by construction — the passes are a view inside the run —
    so the table prints the total `review-cost.py` computed rather than adding a column up
    and getting a different answer than the tab label says."""
    html = _table(_report({"review": _row(cost=4.0, tokens=100_000)}),
                  [_tab("review", "Review")], writing=WROTE_IT, total=652.23,
                  tokens=1_100_000_000)
    assert "$652.23" in html and "costtotal" in html


# --------------------------------------------------------------------------- #
# end to end — a real build, over a real transcript and a real ledger
# --------------------------------------------------------------------------- #

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _turn(uid: str, when: str, inp: int, out: int) -> str:
    return json.dumps({
        "type": "assistant", "uuid": uid, "timestamp": when,
        "message": {"id": uid, "model": "claude-opus-5-20260101",
                    "usage": {"input_tokens": inp, "output_tokens": out}},
    })


@pytest.fixture
def built_page(tmp_path):
    """`{"auto": "cost"}` is still in the scope, because content files in the wild still
    ask for it. It is a no-op now, and the cost tab appears regardless — which is exactly
    what this fixture is here to keep true."""
    return _build_page(tmp_path, [{"label": "files", "value": "1"}, {"auto": "cost"}])


@pytest.fixture
def merged_page(tmp_path):
    """The scope every real content file writes: the review chip and the retired cost
    chip side by side."""
    return _build_page(tmp_path, [{"auto": "autofixed", "href": "#review"}, {"auto": "cost"}])


def _build_page(tmp_path, scope):
    """A page built the way the skill builds it, with the session's transcript and the step
    ledger faked but read through the real code path — subprocess, `git rev-parse`, ledger
    parsing and all. `HOME` is redirected so `review-cost.py` finds our transcript under
    its own `~/.claude/projects` glob instead of the developer's."""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-tmp-repo"
    proj.mkdir(parents=True)
    # Opus list price is $5/1M in, $25/1M out.
    (proj / f"{SESSION}.jsonl").write_text("\n".join([
        _turn("m1", "2026-09-02T10:05:00+00:00", 100_000, 10_000),   # $0.75, 110k tok
        _turn("m2", "2026-09-02T10:25:00+00:00", 20_000, 2_000),     # $0.15,  22k tok
        _turn("m3", "2026-09-02T11:00:00+00:00", 4_000, 400),        # $0.03,   4k tok
    ]) + "\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   env={**os.environ, "HOME": str(home)})

    hr = repo / ".human-review"
    hr.mkdir()
    (hr / ".started").write_text("2026-09-02T09:00:00+00:00", encoding="utf-8")
    (hr / ".steps.json").write_text(json.dumps([
        {"tabs": ["review"], "label": "autoreview",
         "start": "2026-09-02T10:00:00+00:00", "end": "2026-09-02T10:10:00+00:00"},
        {"tabs": ["data"], "label": "diagrams",
         "start": "2026-09-02T10:20:00+00:00", "end": "2026-09-02T10:30:00+00:00"},
        # A window that opened, closed, and caught no turn: a tab a script produced.
        {"tabs": ["packages"], "label": "structure",
         "start": "2026-09-02T10:40:00+00:00", "end": "2026-09-02T10:45:00+00:00"},
        # "logging" is deliberately absent: an uninstrumented step, which must read as
        # "not measured" rather than as a zero.
    ]), encoding="utf-8")

    content = {
        "title": "cost breakdown", "summary": "<p>s</p>",
        "verdict": {"score": 7, "label": "ok", "bullets": ["b"]},
        "scope": scope,
        "findings": [{"title": "f", "body": "b", "severity": "high"}],
        "sections": [{"id": "s", "title": "S", "body": "<p>b</p>"},
                     {"id": "t", "title": "T", "body": "<p>b</p>"},
                     {"id": "u", "title": "U", "body": "<p>b</p>"}],
        "tabs": [
            {"id": "review", "label": "🤖 Review", "blocks": [{"type": "findings"}]},
            {"id": "data", "label": "Data", "blocks": [{"type": "section", "id": "s"}]},
            {"id": "packages", "label": "Structure",
             "blocks": [{"type": "section", "id": "t"}]},
            {"id": "logging", "label": "Logging",
             "blocks": [{"type": "section", "id": "u"}]},
        ],
    }
    src = repo / "content.json"
    src.write_text(json.dumps(content), encoding="utf-8")
    out = repo / "review.html"
    proc = subprocess.run(
        [sys.executable, str(HERE / "build-review-html.py"), str(src), "--out", str(out)],
        cwd=repo, capture_output=True, text=True,
        env={**os.environ, "HOME": str(home), "CLAUDE_CODE_SESSION_ID": SESSION},
    )
    assert proc.returncode == 0, proc.stderr
    # Stashed rather than returned: one test is about what the build SAID, and threading a
    # second return value through every other caller to serve it is worse than this.
    _build_page.last_stderr = proc.stderr
    return out.read_text(encoding="utf-8")


def _panel(page: str) -> str:
    m = re.search(r'<section class="panel" id="cost"[^>]*>(.*?)</section>', page, re.S)
    assert m, ("the built page has no cost tab at all — the measurement ran and reached "
               "nobody, which is the bug this file exists for")
    return m.group(1)


def test_the_built_page_carries_the_cost_as_its_own_tab(built_page):
    assert 'id="tabbtn-cost"' in built_page
    assert 'aria-controls="cost"' in built_page
    assert "<p class=\"paneltag\">Cost</p>" in _panel(built_page)


def test_the_tab_is_labelled_with_the_money_and_no_cents(built_page):
    """`$0.93` on a pill invites reading the cents of a list-price estimate whose error
    bars are the width of a whole session. The label says the size; the cents are one
    click away, in the table it opens."""
    btn = re.search(r'<button[^>]*id="tabbtn-cost"[^>]*>(.*?)</button>', built_page, re.S)
    assert btn, "no cost tab on the strip"
    assert btn.group(1).strip() == "$1"
    assert "$0.93" in _panel(built_page), "the cents are in the table, not on the pill"


def test_the_cost_tab_is_the_last_pill_on_the_strip(built_page):
    """After CODEOWNERS and after everything else, because a bill goes at the end. It is
    also why the page appends it rather than the content file declaring it: position and
    label are both facts about the page, not about any one review."""
    ids = re.findall(r'<button[^>]*id="tabbtn-([a-z]+)"', built_page)
    assert ids[-1] == "cost", f"the strip ends with {ids[-1]!r}: {ids}"


def test_the_cost_tab_is_not_in_the_walk_through_the_lede_has_to_name(built_page):
    """`{{tabcount}}` and the lede's enumeration are about the tabs carrying the review.
    Requiring every content file to also recite `$1` would be the page reading its own
    furniture back to the reader — and the build warns about unnamed tabs, so a cost tab
    inside that list would make every page warn, forever."""
    assert "4 of them" in _build_page.last_stderr, (
        "the enumeration check counted the bill as a tab to be named: "
        + _build_page.last_stderr)
    assert 'id="tabbtn-cost"' in built_page, "…while the tab itself is on the strip"


def test_the_built_page_shows_the_real_per_tab_numbers(built_page):
    panel = _panel(built_page)
    assert "🤖 Review" in panel and "$0.75" in panel and "110k" in panel
    assert "Data" in panel and "$0.15" in panel and "22k" in panel


def test_the_built_page_shows_a_script_made_tab_as_a_measured_zero(built_page):
    panel = _panel(built_page)
    assert "1 tab with no model spend — Structure" in panel
    assert "$0.00" in panel


def test_the_built_page_distinguishes_an_uninstrumented_tab(built_page):
    panel = _panel(built_page)
    assert "1 tab not measured" in panel and "Logging" in panel


def test_the_built_page_totals_the_run_including_the_residual(built_page):
    panel = _panel(built_page)
    assert "$0.03" in panel, "the turn outside every window is the residual"
    assert "$0.93" in panel, "0.75 + 0.15 + 0.00 + 0.03"


def test_the_built_page_says_it_could_not_find_who_wrote_the_code(built_page):
    """The fixture's repo has no commits and no origin, so there is no change set to
    attribute. The row has to say that in words — a `$0.00` there would read as "writing
    this was free", which is the one thing it does not mean."""
    panel = _panel(built_page)
    assert "writing the code" in panel
    assert "nothing to attribute" in panel or "not measured" in panel


def test_the_cost_is_no_longer_a_chip_in_the_scope_bar(built_page):
    """The surface it left. A chip AND a tab is the same number in two places, and the
    chip is the one that could only ever carry the review's own share of it."""
    assert "chip-cost" not in built_page
    assert "cost-breakdown" not in built_page
    assert "chip chip-run" not in built_page


def test_the_review_chip_survives_the_cost_leaving_it(merged_page):
    """What the pill was always best at: who reviewed, and what is left to do. It used to
    carry the money as a second segment; losing that must not lose the findings."""
    assert "review" in merged_page
    assert "1 open" in merged_page
    assert "chip-run" not in merged_page, "one half left, so it is a plain chip again"


def test_the_breakdown_did_not_come_back_as_a_tab_header_tooltip(built_page):
    """The surface that was rejected, twice. Every tab-strip button must stay bare."""
    offenders = [m.group(0)[:120] for m in
                 re.finditer(r'<button type="button" class="tab[^>]*>', built_page)
                 if "data-tip" in m.group(0)]
    assert not offenders, (
        "a tab header grew a tooltip again — the cost has a tab of its own now:\n  "
        + "\n  ".join(offenders))


def test_the_cost_tab_needs_no_network(built_page):
    """The page is opened from disk: everything the table needs is inline."""
    panel = _panel(built_page)
    assert "http://" not in panel
    assert "cdn" not in panel.lower()


def test_the_cost_tab_is_styled_for_both_themes():
    """No literal colours in the table's own rules — the page's tokens flip with
    `prefers-color-scheme`, and a hard-coded hex would only be right in one of them."""
    rules = [line for line in build.CSS.splitlines()
             if "costtab" in line or "costledger" in line or "costsub" in line]
    assert rules, "the cost table has no styles of its own"
    hexes = [line for line in rules if re.search(r"#[0-9a-fA-F]{3,8}\b", line)]
    assert not hexes, ("the cost table must use var(--fg)/var(--muted)/… like the rest "
                       "of the page:\n  " + "\n  ".join(hexes))
    assert any("var(--" in line for line in rules)


# --------------------------------------------------------------------------- #
# The residual, decomposed (added after the panel shipped)
#
# The panel's first version put every unattributed dollar in one row. On a real run that row
# carried 90%+ of the bill, sitting under tab rows worth cents — which does not read as a
# caveat, it reads as an instruction to ignore the table. `review-cost.py` now names the
# parts, and the panel renders them; a report that predates that still renders the old single
# row, which is what the tests above pin.
# --------------------------------------------------------------------------- #

def _parts(guide=None, subagent=None, conversation=None):
    def part(v):
        c, tok, msgs = v
        return {"measured": True, "cost": c, "tokens": tok, "messages": msgs}
    return {k: part(v) for k, v in
            (("guide", guide), ("subagent", subagent), ("conversation", conversation))
            if v is not None}


def _report_with_parts(tabs, residual, parts):
    r = _report(tabs, residual=residual)
    r["residual_parts"] = parts
    return r


def test_the_residual_renders_as_named_rows_when_the_report_names_them():
    html = _table(
        _report_with_parts(
            {"a": _row(cost=2.0, tokens=20_000)},
            {"measured": True, "cost": 10.0, "tokens": 100_000, "messages": 9, "tip": "…"},
            _parts(guide=(6.0, 60_000, 3), subagent=(3.0, 30_000, 4),
                   conversation=(1.0, 10_000, 2)),
        ),
        [_tab("a", "A")],
    )
    assert "assembling the guide itself" in html
    assert "subagent work" in html
    assert "orchestrating conversation" in html
    assert "not one tab&#x27;s" not in html and "not one tab's" not in html, (
        "the undifferentiated row must give way to the named ones, not sit beside them")
    assert "$6.00" in html and "$3.00" in html and "$1.00" in html


def test_a_part_with_no_turns_in_it_is_not_rendered_at_all():
    """A run with no subagents should not be told it spent $0.00 on subagents — an empty
    bucket is not a finding, it is a row of noise."""
    html = _table(
        _report_with_parts(
            {"a": _row(cost=2.0, tokens=20_000)},
            {"measured": True, "cost": 4.0, "tokens": 40_000, "messages": 3, "tip": "…"},
            _parts(guide=(4.0, 40_000, 3), subagent=(0.0, 0, 0), conversation=(0.0, 0, 0)),
        ),
        [_tab("a", "A")],
    )
    assert "assembling the guide itself" in html
    assert "subagent work" not in html
    assert "orchestrating conversation" not in html


def test_the_guide_row_comes_first_because_it_is_the_one_with_a_real_name():
    html = _table(
        _report_with_parts(
            {"a": _row(cost=1.0, tokens=1000)},
            {"measured": True, "cost": 9.0, "tokens": 90_000, "messages": 6, "tip": "…"},
            _parts(guide=(1.0, 10_000, 1), conversation=(8.0, 80_000, 5)),
        ),
        [_tab("a", "A")],
    )
    assert html.index("assembling the guide itself") < html.index("orchestrating conversation")


def test_a_report_without_parts_still_renders_the_single_residual_row():
    """Forward compatibility in the other direction: the panel must not go blank against a
    `review-cost.py` that has not learned to decompose."""
    html = _table(
        _report({"a": _row(cost=2.0, tokens=20_000)},
                residual={"measured": True, "cost": 1.0, "tokens": 10_000,
                          "messages": 2, "tip": "…"}),
        [_tab("a", "A")],
    )
    assert "not one tab" in html
    assert "$1.00" in html, "the undifferentiated residual still carries its own number"


def test_the_caption_says_what_the_change_cost_not_what_the_page_cost():
    """The tab's subject changed when it stopped being a drawer under the review's own
    bill: it is what producing AND reviewing this change came to, and the caption has to
    promise that rather than "which of our steps burned time"."""
    html = _table(
        _report({"a": _row(cost=1.0, tokens=1000)}), [_tab("a", "A")])
    caption = re.search(r"<caption>(.*?)</caption>", html, re.S).group(1)
    assert "produce" in caption and "review" in caption
    assert "list price" in caption, "the number is not what anybody was billed"
    assert "subscription is billed" in caption


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── the phase cut, when the branch's trailers made it datable ──────────────────────
# "What did writing it cost and what did reviewing it cost" cannot answer the question a
# reader arrives with, because taking the review's advice falls in neither. The phases do,
# and they are the same dollars — so they REPLACE the two groups rather than joining them.

PHASES = {"rows": [
    {"key": "implementation", "label": "implementation", "measured": True,
     "cost": 23.37, "tokens": 36_934_793, "messages": 191,
     "detail": "first edit to the change set → commit #1",
     "window": ["2026-09-02T15:41:21.413000+00:00", "2026-09-17T21:29:54+03:00"]},
    {"key": "code_review", "label": "code-review agents", "measured": True,
     "cost": 5.98, "tokens": 6_757_796, "messages": 66,
     "detail": "1 forked reviewer(s), whole transcripts",
     "window": ["2026-09-17T18:30:12+00:00", "2026-09-17T18:34:27+00:00"]},
    {"key": "review_points", "label": "review-points", "measured": True,
     "cost": 0.27, "tokens": 341_850, "messages": 1, "detail": "first → last write"},
    {"key": "video", "label": "demo video", "measured": False,
     "reason": "no step in the ledger named it"},
]}


def test_a_phase_that_cannot_be_dated_says_so_rather_than_printing_zero():
    """`$0.00` and "we could not date this" render identically to a reader and mean
    opposite things: one is a phase that cost nothing, the other is a phase whose cost is
    sitting in some other row of the same table."""
    out = build.phase_rows_html(PHASES)
    assert "demo video — no step in the ledger named it" in out
    assert "costquiet" in out
    # …and the row carries no money at all, not a zero.
    row = [r for r in out.split("<tr") if "demo video" in r][0]
    assert "$0.00" not in row and "<td>—</td>" in row


def test_every_phase_prints_its_window_because_a_window_is_not_a_fence():
    out = build.phase_rows_html(PHASES)
    assert "2 Sep 15:41" in out and "17 Sep" in out
    assert "first edit to the change set" in out


def test_phases_appear_in_the_order_the_money_was_spent_not_the_files():
    out = build.phase_rows_html({"rows": list(reversed(PHASES["rows"]))})
    assert out.index("implementation") < out.index("code-review agents") \
        < out.index("review-points")


def test_a_phase_this_build_does_not_know_still_reaches_the_table():
    """Dropping it would make the rows stop summing to the total, silently, the first time
    a phase is added to session-cost.py."""
    out = build.phase_rows_html({"rows": [
        {"key": "something_new", "label": "something new", "measured": True,
         "cost": 1.5, "tokens": 1000}]})
    assert "something new" in out and "$1.50" in out


def test_nothing_datable_means_no_phase_group_at_all():
    assert build.phase_rows_html(None) == ""
    assert build.phase_rows_html({"rows": [
        {"key": "implementation", "measured": False, "reason": "no session"}]}) == ""


def test_the_phase_cut_replaces_the_two_groups_rather_than_joining_them():
    """The same dollars, cut two ways, under one `total` row is how a reader ends up adding
    a number to itself."""
    led = {
        "writing": {"measured": True, "sessions": [
            {"session": "abcdef12", "edits": 9, "files": 3, "tokens": 100, "cost": 1.0,
             "first": "2026-09-02T15:41:00+00:00", "last": "2026-09-02T16:00:00+00:00"}]},
        "passes": {"groups": {"finding": {"cost": 5.98, "tokens": 6_757_796,
                                          "invoked": ["/code-review"]}}},
        "run": {"measured": True},
        "tabs": {}, "total": 29.62, "total_tokens": 44_034_439,
        "phases": {**PHASES, "cost": 29.62, "tokens": 44_034_439},
    }
    out = build.cost_ledger_html(led, [])
    assert "phase by phase" in out
    assert "writing the code" not in out, "the phase cut already covers it"
    assert "the passes that read the diff" not in out, "same dollars, counted twice"
    # The per-tab group stays: it answers which part of the PAGE cost what, which is a
    # different question and the only one of the three that is about the page.
    assert "building this guide" in out

    # …and without phases, both groups are exactly as they were.
    plain = build.cost_ledger_html({**led, "phases": None}, [])
    assert "writing the code" in plain and "the passes that read the diff" in plain
    assert "phase by phase" not in plain


def test_the_footer_totals_the_rows_on_screen_and_not_another_cut_of_them():
    """`led["total"]` adds the authoring conversation, the passes and the run — three
    overlapping measurements of one bill, reconciled by the two groups the phase cut
    replaces. Printed under the phases it is a footer the column above does not add up to,
    and nothing on the page says which of the two numbers is the answer."""
    led = {"writing": {"measured": True, "sessions": []}, "run": {"measured": True},
           "tabs": {}, "total": 702.99, "total_tokens": 1_100_000_000,
           "phases": {**PHASES, "cost": 206.91, "tokens": 312_869_953}}
    out = build.cost_ledger_html(led, [])
    assert "$206.91" in out and "$702.99" not in out
    # …and with no phases it is the ledger's own total, exactly as before.
    plain = build.cost_ledger_html({**led, "phases": None}, [])
    assert "$702.99" in plain


def test_the_tab_pill_says_what_the_table_says():
    """A `$703` pill over a `$207` table is the footer's contradiction again, read first
    and by everyone — the pill is the only part of this tab a reader sees without opening
    it."""
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    i = src.index("cost_label = f'$")
    assert "phase_total" in src[i - 400:i + 200]
