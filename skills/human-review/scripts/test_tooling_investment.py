"""The tool's own bill on the cost tab: a dated note under the table, never part of it.

`TOOLING_INVESTMENT` is what building human-review itself cost (measured once, by
`tools/tooling-cost.py`). The cost tab is what THIS change cost. A reader must not be able
to add the two up, so the note stays outside the table, says it is a baseline, and its
breakdown still sums to its headline.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

#: Upstream's measured baseline, kept as a fixture: the Ludvig fork ships none
#: (`TOOLING_INVESTMENT = {}`), and the renderer is still tested against a real one.
SAMPLE = {
    "usd": 3986, "sessions": 227, "first": "27 Aug 2026", "asof": "20 Oct 2026",
    "buckets": [("human-review repo", 1120), ("workspace sessions", 795),
                ("subagents", 1809), ("eval runs (claude -p)", 226),
                ("other checkouts", 33), ("other projects", 3)],
    "models": [("Opus 5.5", 1883), ("Opus 5", 1782), ("Fable 5.1", 170),
               ("Sonnet 5", 101), ("Sonnet 5.5", 49)],
    "borderline": {"sessions": 29, "usd": 1360, "prorated": 583},
    "humanSeconds": 122285, "agentSeconds": 1587010, "wallSeconds": 795131,
}


def test_the_ludvig_fork_draws_no_note_of_its_own():
    assert build.TOOLING_INVESTMENT == {} and build.tooling_investment_html() == ""


def test_the_note_is_not_a_row_of_the_bill():
    note = build.tooling_investment_html(SAMPLE)
    assert note.startswith('<div class="costtooling">')
    assert "<tr" not in note and "<table" not in note
    # A baseline, and dated as one: the foot names the sessions and the span it measured.
    assert "not this change" in note and '<p class="costtooling-foot">' in note


def test_the_hours_also_read_as_eight_hour_working_days():
    note = build.tooling_investment_html(
        {**SAMPLE, "humanSeconds": 34 * 3600, "wallSeconds": 8 * 3600})
    assert "<b>34 h (4 days)</b>" in note
    assert "<b>8 h (1 day)</b>" in note
    assert note.count("8-hour working days") == 2  # each tile says what its brackets are


def test_the_breakdown_sums_to_the_headline():
    t = SAMPLE
    assert abs(sum(v for _, v in t["buckets"]) - t["usd"]) <= len(t["buckets"])
    assert abs(sum(v for _, v in t["models"]) - t["usd"]) <= len(t["models"])


def test_the_note_follows_the_table_it_must_not_be_added_to():
    """Appended after the ledger, so it can never land inside the table's total."""
    body = build.cost_ledger_html({"components": None}, [])
    if body:
        assert body.rindex("costtooling") > body.rindex("</table>")
