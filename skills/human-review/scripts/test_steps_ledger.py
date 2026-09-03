#!/usr/bin/env python3
"""The start/end ledger `review-cost.py --tab-costs` reads: append-only, crash-honest.

Run with:  python3 -m pytest test_steps_ledger.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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


# --------------------------------------------------------------------------- #
# Surviving a run that does not go to plan
# --------------------------------------------------------------------------- #

def test_a_write_never_leaves_a_truncated_ledger_behind(tmp_path, monkeypatch):
    """`load()` reads unparseable JSON as an *empty* ledger, so a half-written file does not
    lose one record, it loses the run. The write therefore lands with `os.replace`: a crash
    mid-write leaves the previous array intact, never a prefix of the new one."""
    p = tmp_path / ".steps.json"
    sl.start(p, ["data"], "first")
    before = p.read_text()

    real_replace = os.replace

    def explode(src, dst):
        raise KeyboardInterrupt("killed between the temp file and the rename")

    monkeypatch.setattr(sl.os, "replace", explode)
    with pytest.raises(KeyboardInterrupt):
        sl.start(p, ["packages"], "second")
    monkeypatch.setattr(sl.os, "replace", real_replace)

    assert p.read_text() == before, "an interrupted write changed the ledger"
    assert json.loads(p.read_text())[0]["label"] == "first"
    leftovers = [f.name for f in tmp_path.iterdir() if f.name.endswith(".tmp")]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_concurrent_starts_do_not_lose_a_record(tmp_path):
    """Two processes stamping at once is a read-modify-write race that would silently drop
    whichever lost — the survivor being valid JSON is exactly what makes it invisible. The
    lock serialises them, so every record survives and every index is distinct."""
    p = tmp_path / ".steps.json"
    script = str(HERE / "steps-ledger.py")
    procs = [subprocess.Popen([sys.executable, script, "--path", str(p),
                               "start", f"tab{i}", "--label", f"step {i}"],
                              stdout=subprocess.PIPE, text=True)
             for i in range(12)]
    indexes = sorted(int(pr.communicate()[0].strip()) for pr in procs)
    assert indexes == list(range(12))
    records = json.loads(p.read_text())
    assert len(records) == 12
    assert {r["tabs"][0] for r in records} == {f"tab{i}" for i in range(12)}


def test_reset_clears_the_ledger_its_lock_and_every_handle_file(tmp_path):
    """A ledger surviving into the next run is the one input that yields a confident wrong
    number: it parses, so every tab it names reports as *measured*, while this run's turns
    all fall outside last run's windows — `$0.00` across the board."""
    p = tmp_path / ".steps.json"
    idx = sl.start(p, ["data"], "last run")
    sl.end(p, idx)
    (tmp_path / ".step-data").write_text("0")
    (tmp_path / ".step-api").write_text("1")
    (tmp_path / ".started").write_text("2026-01-01T00:00:00+00:00")

    removed = sl.reset(p)

    assert not p.exists()
    assert not (tmp_path / ".step-data").exists()
    assert not (tmp_path / ".step-api").exists()
    assert any(".steps.json" in r for r in removed)
    # The run marker is a different artifact with a different lifetime — Step 0 rewrites it
    # itself, and eating it here would make the cost chip fall back to the whole session.
    assert (tmp_path / ".started").exists()


def test_reset_on_a_clean_tree_is_a_no_op(tmp_path):
    assert sl.reset(tmp_path / ".steps.json") == []


# --------------------------------------------------------------------------- #
# check — the loud half of the step-to-tab binding
# --------------------------------------------------------------------------- #

def _content(tmp_path, *ids):
    p = tmp_path / "content.json"
    p.write_text(json.dumps({"tabs": [{"id": i, "label": i.title()} for i in ids]}))
    return p


def test_check_passes_when_every_stamped_tab_is_on_the_page(tmp_path, capsys):
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data", "packages"], "diagrams"))
    sl.end(p, sl.start(p, [sl.GUIDE_TAB], "assemble the page"))
    code, lines = sl.check(p, _content(tmp_path, "data", "packages"))
    assert code == 0
    assert "all matching" in " ".join(lines)


def test_the_reserved_guide_id_is_never_reported_as_drift(tmp_path):
    """`guide` is deliberately not a tab, so a page that does not list it is correct."""
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, [sl.GUIDE_TAB], "assemble the page"))
    sl.end(p, sl.start(p, ["data"], "diagrams"))
    code, lines = sl.check(p, _content(tmp_path, "data"))
    assert code == 0
    assert "DRIFT" not in " ".join(lines)


def test_check_says_so_when_step_9_never_stamped_itself(tmp_path):
    """Without it the largest, most explicable chunk of the bill is indistinguishable from
    dead time — the exact thing that made the first breakdown 90% one anonymous row."""
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data"], "diagrams"))
    code, lines = sl.check(p, _content(tmp_path, "data"))
    assert code == 0, "an unstamped Step 9 is a warning, not a build failure"
    assert "Step 9 never stamped itself" in " ".join(lines)


def test_check_fails_loudly_when_a_tab_was_renamed_under_the_wrap(tmp_path):
    """The whole failure this exists for: `packages` renamed to `structure` in content.json
    while Step 2 goes on stamping `packages`. Nothing else in the pipeline notices — the tab
    just reports 'not measured', which reads as an uninstrumented step."""
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data", "packages"], "diagrams"))
    code, lines = sl.check(p, _content(tmp_path, "data", "structure"))
    assert code == 1
    said = " ".join(lines)
    assert "DRIFT" in said
    assert "packages" in said and "structure" in said


def test_check_names_tabs_no_step_fed_without_failing(tmp_path):
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data"], "diagrams"))
    code, lines = sl.check(p, _content(tmp_path, "data", "overview"))
    assert code == 0, "a tab with no step is a warning, not a build failure"
    assert "overview" in " ".join(lines)


def test_check_reports_a_step_that_never_recorded_finishing(tmp_path):
    p = tmp_path / ".steps.json"
    sl.start(p, ["data"], "diagrams")
    code, lines = sl.check(p, _content(tmp_path, "data"))
    assert code == 0
    assert "never recorded finishing" in " ".join(lines)


def test_check_fails_when_no_step_ever_stamped(tmp_path):
    """The state this whole task started from: the ledger plumbed end to end and empty."""
    p = tmp_path / ".steps.json"
    code, lines = sl.check(p, _content(tmp_path, "data"))
    assert code == 1
    assert "no step records" in " ".join(lines)


def test_check_says_so_when_there_is_no_content_file_to_check_against(tmp_path):
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data"], "x"))
    code, lines = sl.check(p, tmp_path / "nope.json")
    assert code == 2
    assert "cannot read tab ids" in " ".join(lines)


def test_check_through_the_cli_exits_non_zero_on_drift(tmp_path):
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["packages"], "x"))
    content = _content(tmp_path, "structure")
    assert sl.main(["--path", str(p), "check", "--content", str(content)]) == 1


# --------------------------------------------------------------------------- #
# --rev — the before side of a diff, recorded because it cannot be recovered
# --------------------------------------------------------------------------- #

def test_a_step_records_the_revision_it_started_from(tmp_path):
    p = tmp_path / ".steps.json"
    sl.start(p, ["review"], "code-review + simplify", rev="cb0988f5")
    assert json.loads(p.read_text())[0]["rev"] == "cb0988f5"


def test_no_rev_leaves_the_record_the_shape_it_always_was(tmp_path):
    """A step with no before-side must not grow an empty field — a `"rev": ""` in the file
    reads as "we looked and there was none", which is a different claim from not looking."""
    p = tmp_path / ".steps.json"
    sl.start(p, ["data"], "diagrams")
    assert "rev" not in json.loads(p.read_text())[0]


def test_the_rev_survives_a_round_trip_through_the_cli(tmp_path, capsys):
    p = tmp_path / ".steps.json"
    assert sl.main(["--path", str(p), "start", "review", "--rev", "deadbeef"]) == 0
    idx = capsys.readouterr().out.strip()
    assert sl.main(["--path", str(p), "end", idx]) == 0
    rec = json.loads(p.read_text())[0]
    assert rec["rev"] == "deadbeef" and rec["end"] is not None


def test_check_warns_when_the_review_step_recorded_no_pre_fix_revision(tmp_path):
    """The one fact a run cannot recover later: once the fixes are applied, squashed or
    folded into a feature commit, nothing says what the tree looked like before them."""
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["review"], "code-review + simplify"))
    sl.end(p, sl.start(p, [sl.GUIDE_TAB], "assemble the page"))
    code, lines = sl.check(p, _content(tmp_path, "review"))
    assert code == 0, "a missing rev is a warning, not a build failure"
    assert "recorded no --rev" in " ".join(lines)


def test_check_is_quiet_once_the_revision_is_there(tmp_path):
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["review"], "code-review + simplify", rev="cb0988f5"))
    sl.end(p, sl.start(p, [sl.GUIDE_TAB], "assemble the page"))
    code, lines = sl.check(p, _content(tmp_path, "review"))
    assert code == 0
    assert "--rev" not in " ".join(lines)


def test_a_project_with_no_review_step_is_not_nagged_for_a_rev(tmp_path):
    """Not every page has an Auto-fixed tab, and a warning that fires where it cannot apply
    is the kind that teaches people to ignore the channel."""
    p = tmp_path / ".steps.json"
    sl.end(p, sl.start(p, ["data"], "diagrams"))
    sl.end(p, sl.start(p, [sl.GUIDE_TAB], "assemble the page"))
    code, lines = sl.check(p, _content(tmp_path, "data"))
    assert "--rev" not in " ".join(lines)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
