#!/usr/bin/env python3
"""The start/end ledger `review-cost.py --tab-costs` reads: append-only, crash-honest.

Run with:  python3 -m pytest test_steps_ledger.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("steps_ledger", HERE / "steps-ledger.py")
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)


def test_start_appends_a_record_with_no_end_yet(tmp_path):
    p = tmp_path / ".steps.json"
    idx = sl.start(p, ["data", "packages"], "diagrams")
    assert idx == 0
    records = json.loads(p.read_text())
    assert records == [{"tabs": ["data", "packages"], "label": "diagrams",
                        "start": records[0]["start"], "end": None}]
    assert records[0]["start"]


def test_end_closes_the_record_start_returned(tmp_path):
    p = tmp_path / ".steps.json"
    idx = sl.start(p, ["data"])
    assert sl.end(p, idx) is True
    records = json.loads(p.read_text())
    assert records[0]["end"] is not None
    assert records[0]["end"] >= records[0]["start"]


def test_two_steps_close_independently_by_index(tmp_path):
    """A step nested inside another (or two that overlap) must not close the wrong one —
    the index `start` returns is the handle, not "the most recent record"."""
    p = tmp_path / ".steps.json"
    a = sl.start(p, ["data"], "first")
    b = sl.start(p, ["packages"], "second")
    sl.end(p, b)
    records = json.loads(p.read_text())
    assert records[a]["end"] is None
    assert records[b]["end"] is not None


def test_ending_an_index_that_does_not_exist_fails_without_writing(tmp_path, capsys):
    p = tmp_path / ".steps.json"
    sl.start(p, ["data"])
    before = p.read_text()
    assert sl.end(p, 7) is False
    assert p.read_text() == before
    assert "no record #7" in capsys.readouterr().err


def test_a_malformed_ledger_is_treated_as_empty_not_fatal(tmp_path, capsys):
    p = tmp_path / ".steps.json"
    p.write_text("{not json", encoding="utf-8")
    idx = sl.start(p, ["data"])
    assert idx == 0
    assert "not valid JSON" in capsys.readouterr().err


def test_main_start_then_end_round_trips_through_the_cli(tmp_path, capsys):
    p = tmp_path / ".steps.json"
    assert sl.main(["--path", str(p), "start", "data,packages", "--label", "x"]) == 0
    idx = capsys.readouterr().out.strip()
    assert idx == "0"
    assert sl.main(["--path", str(p), "end", idx]) == 0
    records = json.loads(p.read_text())
    assert records[0]["tabs"] == ["data", "packages"]
    assert records[0]["end"] is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
