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


def test_the_note_is_not_a_row_of_the_bill():
    note = build.tooling_investment_html()
    assert note.startswith('<div class="costtooling">')
    assert "<tr" not in note and "<table" not in note
    # A baseline, and dated as one: the foot names the sessions and the span it measured.
    assert "not this change" in note and '<p class="costtooling-foot">' in note


def test_the_hours_also_read_as_eight_hour_working_days():
    note = build.tooling_investment_html(
        {**build.TOOLING_INVESTMENT, "humanSeconds": 34 * 3600, "wallSeconds": 8 * 3600})
    assert "<b>34 h (4 days)</b>" in note
    assert "<b>8 h (1 day)</b>" in note
    assert note.count("8-hour working days") == 2  # each tile says what its brackets are


def test_the_breakdown_sums_to_the_headline():
    t = build.TOOLING_INVESTMENT
    assert abs(sum(v for _, v in t["buckets"]) - t["usd"]) <= len(t["buckets"])
    assert abs(sum(v for _, v in t["models"]) - t["usd"]) <= len(t["models"])


def test_the_note_follows_the_table_it_must_not_be_added_to():
    """Appended after the ledger, so it can never land inside the table's total."""
    body = build.cost_ledger_html({"components": None}, [])
    if body:
        assert body.rindex("costtooling") > body.rindex("</table>")
