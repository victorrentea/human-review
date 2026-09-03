#!/usr/bin/env python3
"""Per-tab cost attribution: the ledger parser, the window arithmetic, and the report a
tab tooltip is built from.

`collect()`'s own machinery (transcript discovery, subagent globbing, dedupe) already has
no test here and needs a live session to exercise — this file is about the part added for
per-tab attribution, which is pure once it has a list of turns and parsed ledger records.

Run with:  python3 -m pytest test_review_cost.py
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("review_cost", HERE / "review-cost.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _turn(when: str | None, in_tok=1000, out_tok=100, model="claude-sonnet-5-20260101",
         side=False):
    """A `(key, model, usage, side, when)` tuple shaped like `gather_turns()` produces."""
    usage = {"input_tokens": in_tok, "output_tokens": out_tok}
    w = _ts(when) if when else None
    return (w or dt.datetime.max.replace(tzinfo=dt.timezone.utc), model, usage, side, w)


# --------------------------------------------------------------------------- #
# load_steps — parsing the ledger, honestly
# --------------------------------------------------------------------------- #

def test_a_missing_ledger_is_not_found(tmp_path):
    records, found = rc.load_steps(tmp_path / "nope.json")
    assert records == [] and found is False


def test_a_malformed_ledger_is_not_found(tmp_path):
    p = tmp_path / ".steps.json"
    p.write_text("{not json", encoding="utf-8")
    records, found = rc.load_steps(p)
    assert records == [] and found is False


def test_a_ledger_that_is_not_a_list_is_not_found(tmp_path):
    p = tmp_path / ".steps.json"
    p.write_text(json.dumps({"tabs": ["x"]}), encoding="utf-8")
    records, found = rc.load_steps(p)
    assert records == [] and found is False


def test_a_valid_empty_ledger_is_found(tmp_path):
    """Found, but empty: the pipeline ran the ledger machinery and simply had nothing to
    stamp yet — different from the file never existing at all."""
    p = tmp_path / ".steps.json"
    p.write_text("[]", encoding="utf-8")
    records, found = rc.load_steps(p)
    assert records == [] and found is True


def test_a_record_missing_a_start_is_dropped(tmp_path):
    p = tmp_path / ".steps.json"
    p.write_text(json.dumps([{"tabs": ["a"], "end": "2026-09-02T10:00:00+00:00"}]),
                encoding="utf-8")
    records, found = rc.load_steps(p)
    assert records == [] and found is True


def test_a_record_with_no_tabs_is_dropped(tmp_path):
    p = tmp_path / ".steps.json"
    p.write_text(json.dumps([{"tabs": [], "start": "2026-09-02T10:00:00+00:00"}]),
                encoding="utf-8")
    records, found = rc.load_steps(p)
    assert records == []


def test_an_open_record_parses_with_end_none(tmp_path):
    p = tmp_path / ".steps.json"
    p.write_text(json.dumps([{"tabs": ["a"], "start": "2026-09-02T10:00:00+00:00",
                              "end": None}]), encoding="utf-8")
    records, found = rc.load_steps(p)
    assert len(records) == 1
    assert records[0]["end"] is None
    assert records[0]["tabs"] == ["a"]


# --------------------------------------------------------------------------- #
# tab_costs — the window arithmetic
# --------------------------------------------------------------------------- #

def test_a_turn_inside_the_window_is_attributed_to_its_tab():
    turns = [_turn("2026-09-02T10:05:00+00:00")]
    steps = [{"tabs": ["data"], "label": "", "start": _ts("2026-09-02T10:00:00+00:00"),
             "end": _ts("2026-09-02T10:10:00+00:00")}]
    out = rc.tab_costs(turns, steps, ["data"])
    assert out["tabs"]["data"]["messages"] == 1
    assert out["tabs"]["data"]["cost"] > 0
    assert out["residual"]["messages"] == 0


def test_a_turn_outside_every_window_is_residual():
    turns = [_turn("2026-09-02T11:00:00+00:00")]      # an hour after the only window
    steps = [{"tabs": ["data"], "label": "", "start": _ts("2026-09-02T10:00:00+00:00"),
             "end": _ts("2026-09-02T10:10:00+00:00")}]
    out = rc.tab_costs(turns, steps, ["data"])
    assert out["tabs"]["data"]["messages"] == 0
    assert out["residual"]["messages"] == 1
    assert out["residual"]["cost"] > 0


def test_a_turn_with_no_timestamp_is_residual_not_dropped():
    """The assembling conversation's own turns (Step 0, Step 9) need an honest home —
    residual — never silently vanishing from the accounting."""
    turns = [_turn(None)]
    steps = [{"tabs": ["data"], "label": "", "start": _ts("2026-09-02T10:00:00+00:00"),
             "end": _ts("2026-09-02T10:10:00+00:00")}]
    out = rc.tab_costs(turns, steps, ["data"])
    assert out["residual"]["messages"] == 1
    assert out["tabs"]["data"]["messages"] == 0


def test_a_turn_in_two_tabs_windows_is_split_evenly_and_the_halves_add_up():
    turns = [_turn("2026-09-02T10:05:00+00:00")]
    steps = [{"tabs": ["data", "packages"], "label": "", "start": _ts("2026-09-02T10:00:00+00:00"),
             "end": _ts("2026-09-02T10:10:00+00:00")}]
    out = rc.tab_costs(turns, steps, ["data", "packages"])
    a, b = out["tabs"]["data"]["cost"], out["tabs"]["packages"]["cost"]
    assert a == pytest.approx(b)
    # The two halves must reconstruct the turn's real cost, not double it or halve it away.
    full_cost = rc.price(rc.family(turns[0][1]), turns[0][2])
    assert (a + b) == pytest.approx(full_cost)


def test_an_unclosed_step_attributes_nothing_but_is_flagged():
    """A step that started and crashed must not silently claim every later turn, and must
    not look like a tab that was simply never touched by the pipeline."""
    turns = [_turn("2026-09-02T10:05:00+00:00")]
    steps = [{"tabs": ["data"], "label": "", "start": _ts("2026-09-02T10:00:00+00:00"),
             "end": None}]
    out = rc.tab_costs(turns, steps, ["data"])
    assert out["tabs"]["data"]["messages"] == 0
    assert out["tabs"]["data"]["has_unclosed"] is True
    assert out["tabs"]["data"]["has_closed"] is False
    assert out["residual"]["messages"] == 1


def test_a_wanted_tab_the_ledger_never_named_has_no_signal_either_way():
    out = rc.tab_costs([], [], ["never-mentioned"])
    row = out["tabs"]["never-mentioned"]
    assert row["has_closed"] is False and row["has_unclosed"] is False
    assert row["messages"] == 0


# --------------------------------------------------------------------------- #
# tab_cost_tip — a missing measurement must not read like a measured zero
# --------------------------------------------------------------------------- #

def test_never_named_and_unclosed_read_as_different_reasons():
    never = rc.tab_cost_tip({"has_closed": False, "has_unclosed": False, "cost": 0, "tokens": 0})
    crashed = rc.tab_cost_tip({"has_closed": False, "has_unclosed": True, "cost": 0, "tokens": 0})
    assert "no step in the ledger named it" in never
    assert "started but never recorded finishing" in crashed
    assert never != crashed
    assert "$" not in never and "$" not in crashed


def test_a_measured_tab_reports_money_not_a_reason():
    tip = rc.tab_cost_tip({"has_closed": True, "has_unclosed": False, "cost": 1.23, "tokens": 6200})
    assert "not measured" not in tip
    assert "$1.23" in tip and "6k tok" in tip


def test_a_measured_zero_says_measured_not_absent():
    """The requirement this whole tip exists for: a genuine $0 turn count in a real,
    closed window must not read the same as a tab nobody instrumented."""
    zero = rc.tab_cost_tip({"has_closed": True, "has_unclosed": False, "cost": 0.0, "tokens": 0})
    absent = rc.tab_cost_tip({"has_closed": False, "has_unclosed": False, "cost": 0.0, "tokens": 0})
    assert zero != absent
    assert "not measured" not in zero
    assert "not measured" in absent


def test_a_partially_measured_tab_says_its_a_lower_bound():
    tip = rc.tab_cost_tip({"has_closed": True, "has_unclosed": True, "cost": 2.0, "tokens": 1000})
    assert "lower bound" in tip


def test_tips_are_plain_text_never_markup():
    """`data-tip` is read with `textContent`, not innerHTML — the page's one tooltip
    component, never a second one that happens to accept HTML."""
    tip = rc.tab_cost_tip({"has_closed": True, "has_unclosed": False, "cost": 1.0, "tokens": 100})
    assert "<" not in tip and ">" not in tip


# --------------------------------------------------------------------------- #
# tab_cost_report — every requested tab always gets an entry
# --------------------------------------------------------------------------- #

def test_no_session_id_reports_every_tab_as_not_measured():
    report = rc.tab_cost_report(None, None, Path("/nonexistent"), ["a", "b"])
    assert report["available"] is False
    assert set(report["tabs"]) == {"a", "b"}
    for row in report["tabs"].values():
        assert row["measured"] is False
        assert "not measured" in row["tip"]
    assert "not measured" in report["residual"]["tip"]


def test_an_unresolvable_session_reports_every_tab_as_not_measured():
    report = rc.tab_cost_report("no-such-session-id", None, Path("/nonexistent"), ["a"])
    assert report["available"] is False
    assert "no transcript" in report["reason"]
    assert "not measured" in report["tabs"]["a"]["tip"]


def _fake_transcript(tmp_path, lines) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_a_missing_ledger_is_named_as_the_reason_but_the_run_is_still_available(tmp_path, monkeypatch):
    """The transcript resolves fine; only the ledger is missing. That is a narrower,
    truer reason than "not available" — the run itself was measurable, nobody wired the
    per-tab attribution up yet."""
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T10:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
    ])
    monkeypatch.setattr(rc, "transcript", lambda session: fake)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    report = rc.tab_cost_report("fake-session", None, tmp_path / "nope.json", ["data"])
    assert report["available"] is True
    assert report["ledger"] is False
    assert "no step ledger" in report["reason"]
    assert "not measured" in report["tabs"]["data"]["tip"]


def test_an_end_to_end_report_measures_the_tab_and_the_residual(tmp_path, monkeypatch):
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T10:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"type": "assistant", "timestamp": "2026-09-02T12:00:00Z",   # outside every window
         "message": {"id": "m2", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 200, "output_tokens": 20}}},
    ])
    monkeypatch.setattr(rc, "transcript", lambda session: fake)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    steps_path = tmp_path / ".steps.json"
    steps_path.write_text(json.dumps([
        {"tabs": ["data"], "label": "diagrams",
         "start": "2026-09-02T10:00:00+00:00", "end": "2026-09-02T10:10:00+00:00"},
    ]), encoding="utf-8")
    report = rc.tab_cost_report("fake-session", None, steps_path, ["data", "owners"])
    assert report["ledger"] is True
    assert report["tabs"]["data"]["measured"] is True
    assert report["tabs"]["data"]["messages"] == 1
    assert report["tabs"]["owners"]["measured"] is False
    assert "no step in the ledger named it" in report["tabs"]["owners"]["tip"]
    assert report["residual"]["messages"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
