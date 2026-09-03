#!/usr/bin/env python3
"""The per-tab cost has to be *visible*, and that is the part that keeps going missing.

`review-cost.py --tab-costs` and `steps-ledger.py` have had tests since the day they were
written, and both were green for the whole period during which the measurement they produce
reached nobody at all: its only surface was a `data-tip` on each tab header, and when tab
tooltips were removed the emission went with them. The subprocess still ran on every build.
The number still came back correct. The page showed nothing.

So the tests here are deliberately at the other end of the pipe. They do not check that the
arithmetic is right (test_review_cost.py owns that) — they check that the arithmetic reaches
the HTML, with the right numbers in it, from a real `build-review-html.py` run over a real
transcript and a real step ledger. A rendering regression of the exact kind this file exists
to catch cannot be silent again: the end-to-end test fails if the breakdown is absent, and it
also fails if the breakdown is present but empty.

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


# --------------------------------------------------------------------------- #
# cost_breakdown_html — the shape of the panel
# --------------------------------------------------------------------------- #

def test_a_tab_with_spend_gets_its_own_row_with_both_numbers():
    html = build.cost_breakdown_html(
        _report({"review": _row(cost=12.5, tokens=1_400_000)}),
        [_tab("review", "🤖 Review")],
    )
    assert "🤖 Review" in html
    assert "$12.50" in html
    assert "1.4M" in html


def test_the_rows_are_ordered_by_spend_not_by_tab_order():
    html = build.cost_breakdown_html(
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
    html = build.cost_breakdown_html(
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
    assert html.count("<tr") == 4, (  # header + the spend row + the zero row + total
        "the zero tabs must occupy exactly one row between them")


def test_a_single_zero_tab_says_tab_not_tabs():
    html = build.cost_breakdown_html(
        _report({"review": _row(cost=4.0, tokens=100_000), "data": _row()}),
        [_tab("review", "Review"), _tab("data", "Data")],
    )
    assert "1 tab with no model spend — Data" in html


def test_unmeasured_tabs_never_render_as_a_measured_zero():
    """The failure this whole feature guards against: "we could not measure this" looking
    exactly like "this measured zero"."""
    html = build.cost_breakdown_html(
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
    html = build.cost_breakdown_html(
        _report({"a": _row(cost=2.0, tokens=20_000), "b": _row(cost=3.0, tokens=30_000)},
                residual={"measured": True, "cost": 1.25, "tokens": 5_000,
                          "messages": 4, "tip": "…"}),
        [_tab("a", "A"), _tab("b", "B")],
    )
    assert "$1.25" in html                      # the residual
    assert "not one tab's" in html
    assert "$6.25" in html                      # 2.00 + 3.00 + 1.25
    assert "costtotal" in html


def test_overview_is_left_out_because_no_step_ever_stamps_it():
    html = build.cost_breakdown_html(
        _report({"overview": _row(measured=False), "review": _row(cost=1.0, tokens=1000)}),
        [_tab("overview", "Overview"), _tab("review", "Review")],
    )
    assert "Overview" not in html
    assert "not measured" not in html


def test_no_report_means_no_panel():
    assert build.cost_breakdown_html(None, [_tab("a", "A")]) == ""
    assert build.cost_breakdown_html(_report({"a": _row()}), []) == ""


def test_a_ledgerless_run_still_says_so_rather_than_showing_nothing():
    """The whole run unmeasured is the case that used to render as silence. It renders as
    a sentence: one row, with the reason in it."""
    html = build.cost_breakdown_html(
        _report({t: _row(measured=False) for t in ("a", "b", "c")},
                reason="no session id ($CLAUDE_CODE_SESSION_ID unset)"),
        [_tab("a", "A"), _tab("b", "B"), _tab("c", "C")],
    )
    assert html
    assert "3 tabs not measured" in html
    assert "no session id" in html
    assert "costtotal" not in html, (
        "a $0.00 total under a chip that says $308.64 reads as wrong, not as unmeasured — "
        "a run that measured nothing has no total to show")


# --------------------------------------------------------------------------- #
# cost_chip_html — the handle, and its promise
# --------------------------------------------------------------------------- #

def test_the_chip_becomes_a_disclosure_button_when_there_is_a_breakdown():
    out = build.cost_chip_html({"label": "review cost", "value": "$165.05", "tip": "x"},
                               '<div class="costbreak" id="cost-breakdown" hidden></div>')
    assert 'class="chip chip-cost"' in out
    assert 'aria-expanded="false"' in out
    assert f'aria-controls="{build.COST_PANEL_ID}"' in out
    assert "caret" in out, "the chip must look like it opens something without being hovered"
    assert "$165.05" in out


def test_the_chip_stays_an_inert_pill_when_there_is_nothing_to_open():
    out = build.cost_chip_html({"label": "review cost", "value": "$165.05", "tip": "x"}, "")
    assert "chip-cost" not in out
    assert "aria-expanded" not in out
    assert "$165.05" in out, "losing the breakdown must never cost the reader the total"


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
def built_page(tmp_path, monkeypatch):
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
        "scope": [{"label": "files", "value": "1"}, {"auto": "cost"}],
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
    return out.read_text(encoding="utf-8")


def _panel(page: str) -> str:
    m = re.search(r'<div class="costbreak"[^>]*>(.*?)</div>', page, re.S)
    assert m, ("the built page has no per-tab cost breakdown at all — the measurement ran "
               "and reached nobody, which is the bug this test exists for")
    return m.group(1)


def test_the_built_page_carries_the_breakdown(built_page):
    assert 'id="cost-breakdown"' in built_page
    assert 'class="chip chip-cost"' in built_page
    assert 'aria-controls="cost-breakdown"' in built_page


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


def test_the_breakdown_is_closed_until_it_is_asked_for(built_page):
    """Not noisy: the page's subject is the diff, not what measuring it cost."""
    assert re.search(r'<div class="costbreak" id="cost-breakdown" hidden>', built_page)
    assert 'aria-expanded="false"' in built_page


def test_the_breakdown_did_not_come_back_as_a_tab_header_tooltip(built_page):
    """The surface that was rejected, twice. Every tab-strip button must stay bare."""
    offenders = [m.group(0)[:120] for m in
                 re.finditer(r'<button type="button" class="tab[^>]*>', built_page)
                 if "data-tip" in m.group(0)]
    assert not offenders, (
        "a tab header grew a tooltip again — the per-tab cost has a panel now:\n  "
        + "\n  ".join(offenders))


def test_the_breakdown_needs_no_network(built_page):
    """The report is opened from disk: everything it needs is inline."""
    panel_and_script = built_page[built_page.index('id="cost-breakdown"'):]
    assert "http://" not in panel_and_script[:4000]
    assert "cdn" not in panel_and_script[:4000].lower()


def test_the_breakdown_is_styled_for_both_themes():
    """No literal colours in the panel's own rules — the page's tokens flip with
    `prefers-color-scheme`, and a hard-coded hex would only be right in one of them."""
    rules = [line for line in build.CSS.splitlines()
             if "costtab" in line or "costbreak" in line or "chip-cost" in line]
    assert rules, "the breakdown has no styles of its own"
    hexes = [line for line in rules if re.search(r"#[0-9a-fA-F]{3,8}\b", line)]
    assert not hexes, ("the breakdown must use var(--fg)/var(--muted)/… like the rest of "
                       "the page:\n  " + "\n  ".join(hexes))
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
    html = build.cost_breakdown_html(
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
    assert "$12.00" in html, "the total is still tabs + the whole residual"


def test_a_part_with_no_turns_in_it_is_not_rendered_at_all():
    """A run with no subagents should not be told it spent $0.00 on subagents — an empty
    bucket is not a finding, it is a row of noise."""
    html = build.cost_breakdown_html(
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
    html = build.cost_breakdown_html(
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
    html = build.cost_breakdown_html(
        _report({"a": _row(cost=2.0, tokens=20_000)},
                residual={"measured": True, "cost": 1.0, "tokens": 10_000,
                          "messages": 2, "tip": "…"}),
        [_tab("a", "A")],
    )
    assert "not one tab" in html
    assert "$3.00" in html


def test_the_caption_says_which_steps_burned_time_not_what_a_tab_cost():
    """Five of the ten tabs are produced by shell scripts and honestly cost nothing, so a
    caption promising "what each tab cost" over-claims what the table can answer."""
    html = build.cost_breakdown_html(
        _report({"a": _row(cost=1.0, tokens=1000)}), [_tab("a", "A")])
    caption = re.search(r"<caption>(.*?)</caption>", html, re.S).group(1)
    assert "steps" in caption.lower()
    assert "costs nothing" in caption or "cost nothing" in caption


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
