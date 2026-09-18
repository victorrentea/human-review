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
# _scan / gather_turns — dedupe by message.id keeps the richest usage
# --------------------------------------------------------------------------- #

def test_dedupe_keeps_the_earliest_when_but_the_richest_usage(tmp_path):
    """Claude Code writes several rows per message.id as a turn streams, each carrying
    `usage` so far, with `output_tokens` growing row over row. Keeping whichever row
    arrived first (the old rule) kept the smallest one — understating the bill by
    whatever the turn still had left to stream when that row was written. The fix keeps
    the earliest `when` (the phase windows need the turn placed at its start) but the
    usage with the most output_tokens, wherever in the stream it landed."""
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T10:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"type": "assistant", "timestamp": "2026-09-02T10:05:03Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 50}}},
        {"type": "assistant", "timestamp": "2026-09-02T10:05:07Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 80}}},
    ])
    best: dict = {}
    rc._scan(fake, None, False, best)
    assert len(best) == 1
    key, model, usage, side, when = best["m1"]
    assert usage["output_tokens"] == 80
    assert when == _ts("2026-09-02T10:05:00+00:00"), \
        "the turn is still placed at its first row, not its last"


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


# --------------------------------------------------------------------------- #
# Drift between a step wrap and the tab list — the failure that used to be silent
# --------------------------------------------------------------------------- #

def test_a_ledger_tab_the_page_does_not_have_is_named_not_ignored():
    """`tab_costs` has to ignore a tab it has no row for — but ignoring it *quietly* is how
    the binding rots, because the renamed tab then reads exactly like an uninstrumented one."""
    steps = [{"tabs": ["packages"], "label": "diagrams",
              "start": _ts("2026-09-02T10:00:00+00:00"),
              "end": _ts("2026-09-02T10:10:00+00:00")}]
    result = rc.tab_costs([_turn("2026-09-02T10:05:00+00:00")], steps, ["structure"])
    assert result["unknown"] == ["packages"]
    assert result["residual"]["messages"] == 1, "its tokens still have to land somewhere"


def test_no_drift_reports_no_unknown_tabs():
    steps = [{"tabs": ["data"], "label": "diagrams",
              "start": _ts("2026-09-02T10:00:00+00:00"),
              "end": _ts("2026-09-02T10:10:00+00:00")}]
    assert rc.tab_costs([], steps, ["data", "owners"])["unknown"] == []


def test_the_report_makes_drift_the_stated_reason_a_tab_is_unmeasured(tmp_path, monkeypatch,
                                                                     capsys):
    """`reason` is what the breakdown panel prints on its "N tabs not measured" row, so
    putting the mismatch there is what carries it to a human instead of a log nobody opens."""
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T10:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
    ])
    monkeypatch.setattr(rc, "transcript", lambda session: fake)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    steps_path = tmp_path / ".steps.json"
    steps_path.write_text(json.dumps([
        {"tabs": ["packages"], "label": "diagrams",
         "start": "2026-09-02T10:00:00+00:00", "end": "2026-09-02T10:10:00+00:00"},
    ]), encoding="utf-8")
    report = rc.tab_cost_report("fake-session", None, steps_path, ["structure"])
    assert report["unknown_tabs"] == ["packages"]
    assert "packages" in report["reason"] and "drifted apart" in report["reason"]
    assert "[review-cost]" in capsys.readouterr().err


def test_a_clean_run_still_reports_no_reason(tmp_path, monkeypatch):
    """The reason field stays None when nothing is wrong — the panel falls back to its own
    wording for a tab that simply had no step, and must not be handed a false alarm."""
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T10:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
    ])
    monkeypatch.setattr(rc, "transcript", lambda session: fake)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    steps_path = tmp_path / ".steps.json"
    steps_path.write_text(json.dumps([
        {"tabs": ["data"], "label": "diagrams",
         "start": "2026-09-02T10:00:00+00:00", "end": "2026-09-02T10:10:00+00:00"},
    ]), encoding="utf-8")
    report = rc.tab_cost_report("fake-session", None, steps_path, ["data", "owners"])
    assert report["reason"] is None and report["unknown_tabs"] == []


# --------------------------------------------------------------------------- #
# The residual, decomposed — one row carrying 90% of the bill is not a caveat
# --------------------------------------------------------------------------- #

def _win(tabs, a, b):
    return {"tabs": tabs, "label": ",".join(tabs), "start": _ts(a), "end": _ts(b)}


def test_the_guide_pseudo_tab_is_its_own_bucket_not_a_tab_row():
    """`guide` is Step 9 assembling the page. It has to be named — it is normally the biggest
    single share — without becoming a row in a table of tabs, because it is not one."""
    steps = [_win(["guide"], "2026-09-02T11:00:00+00:00", "2026-09-02T11:30:00+00:00")]
    r = rc.tab_costs([_turn("2026-09-02T11:10:00+00:00")], steps, ["data"])
    assert "guide" not in r["tabs"], "the pseudo-tab must never render as a tab"
    assert r["residual_parts"]["guide"]["messages"] == 1
    assert r["unknown"] == [], "`guide` is reserved, not drift"


def test_unattributed_turns_split_by_whether_a_subagent_spent_them():
    """The two halves answer different questions: delegated work that nobody bracketed, and
    the orchestrating conversation that never belonged to a step at all."""
    turns = [_turn("2026-09-02T09:00:00+00:00", side=True),
             _turn("2026-09-02T09:01:00+00:00", side=True),
             _turn("2026-09-02T09:02:00+00:00", side=False)]
    r = rc.tab_costs(turns, [], ["data"])
    assert r["residual_parts"]["subagent"]["messages"] == 2
    assert r["residual_parts"]["conversation"]["messages"] == 1
    assert r["residual_parts"]["guide"]["messages"] == 0


def test_the_parts_always_add_back_up_to_the_residual():
    """The invariant every caller leans on — tabs + residual == the scope chip's total —
    must survive the decomposition, or the panel's own total stops matching the chip."""
    steps = [_win(["data"], "2026-09-02T10:00:00+00:00", "2026-09-02T10:10:00+00:00"),
             _win(["guide"], "2026-09-02T11:00:00+00:00", "2026-09-02T11:10:00+00:00")]
    turns = [_turn("2026-09-02T10:05:00+00:00"),
             _turn("2026-09-02T11:05:00+00:00"),
             _turn("2026-09-02T12:00:00+00:00", side=True),
             _turn("2026-09-02T12:01:00+00:00")]
    r = rc.tab_costs(turns, steps, ["data"])
    parts = sum(v["cost"] for v in r["residual_parts"].values())
    assert r["residual"]["cost"] == pytest.approx(parts)
    assert r["residual"]["messages"] == 3
    total = r["tabs"]["data"]["cost"] + r["residual"]["cost"]
    assert total == pytest.approx(sum(rc.price(rc.family(m), u) for _, m, u, _s, _w in turns))


def test_the_report_carries_the_parts_through_to_the_page(tmp_path, monkeypatch):
    fake = _fake_transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2026-09-02T11:05:00Z",
         "message": {"id": "m1", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"type": "assistant", "timestamp": "2026-09-02T13:00:00Z",
         "message": {"id": "m2", "model": "claude-sonnet-5-20260101",
                     "usage": {"input_tokens": 200, "output_tokens": 20}}},
    ])
    monkeypatch.setattr(rc, "transcript", lambda session: fake)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    steps_path = tmp_path / ".steps.json"
    steps_path.write_text(json.dumps([
        {"tabs": ["guide"], "label": "assemble the page",
         "start": "2026-09-02T11:00:00+00:00", "end": "2026-09-02T11:10:00+00:00"},
    ]), encoding="utf-8")
    report = rc.tab_cost_report("fake-session", None, steps_path, ["data"])
    assert report["reason"] is None, "`guide` must not be reported as a drifted tab id"
    parts = report["residual_parts"]
    assert parts["guide"]["messages"] == 1
    assert parts["conversation"]["messages"] == 1
    assert sum(p["cost"] for p in parts.values()) == pytest.approx(report["residual"]["cost"])


# --------------------------------------------------------------------------- #
# Prices, and the cache-read multiplier that is not one number
# --------------------------------------------------------------------------- #

def test_sonnet_is_two_and_ten():
    """The rate Victor is actually quoted. It was 3/15 here, which overstated every
    Sonnet turn on the page by half."""
    assert rc.PRICES["sonnet"] == (2.0, 10.0)


def test_a_fable_cache_read_is_a_fortieth_of_input_not_a_tenth():
    """Fable reads cache at $0.25 against a $10 input. The flat tenth every other family
    uses charges $1.00 for the same tokens — four times over — and on a long agentic run
    cache reads are most of the tokens, so it lands on the total, not in the noise."""
    usage = {"cache_read_input_tokens": 1_000_000}
    assert rc.price("fable", usage) == pytest.approx(0.25)
    assert rc.price("fable", usage) == pytest.approx(0.025 * 10.0)


@pytest.mark.parametrize("fam,inp", [("opus", 5.0), ("sonnet", 2.0), ("haiku", 1.0)])
def test_every_other_family_still_reads_cache_at_a_tenth(fam, inp):
    assert rc.price(fam, {"cache_read_input_tokens": 1_000_000}) == pytest.approx(inp * 0.1)


def test_the_multiplier_is_per_family_and_not_a_constant_anyone_can_forget():
    """A family with no entry falls back to the tenth, so adding a model cannot silently
    price its cache reads at zero."""
    assert rc.CACHE_READ.get("fable") == 0.025
    assert rc.CACHE_READ.get("opus") is None and rc.CACHE_READ_DEFAULT == 0.10


# --------------------------------------------------------------------------- #
# Where a subagent's transcript is read from
# --------------------------------------------------------------------------- #

def _session_tree(tmp_path, agents=(), rows=()):
    """`<dir>/<id>.jsonl` plus `<dir>/<id>/subagents/agent-*.jsonl`, as Claude Code writes it."""
    session = tmp_path / "abc.jsonl"
    session.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    subs = tmp_path / "abc" / "subagents"
    for name, meta, agent_rows in agents:
        subs.mkdir(parents=True, exist_ok=True)
        (subs / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in agent_rows) + "\n", encoding="utf-8")
        if meta is not None:
            (subs / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return session


def _assistant(mid, when, model="claude-sonnet-5-20260101", in_tok=1000, out_tok=100,
              side=False):
    row = {"type": "assistant", "timestamp": when,
           "message": {"id": mid, "model": model,
                       "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}}
    if side:
        row["isSidechain"] = True
    return row


def test_a_subagent_is_read_from_the_durable_copy_beside_the_transcript(tmp_path):
    """The tmp `tasks/*.output` files are symlinks into exactly these, and a tmp sweep
    turns them into dangling paths — which the old glob counted as no subagent at all."""
    session = _session_tree(tmp_path,
                            agents=[("agent-aaaaaaaaaaaa", {"name": "code-review"},
                                     [_assistant("a1", "2026-09-02T10:00:00Z")])],
                            rows=[_assistant("m1", "2026-09-02T09:00:00Z")])
    found = rc.subagent_transcripts(session)
    assert [p.name for p in found] == ["agent-aaaaaaaaaaaa.jsonl"]
    assert found[0].parent == session.parent / "abc" / "subagents"


def test_the_reviewers_are_picked_by_the_name_the_harness_recorded(tmp_path):
    """By `name`, not by prose in the description: `pass_costs` learned that the hard way,
    billing two implementation errands to the review because their description said so."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"name": "code-review", "description": "/code-review main"},
                 [_assistant("a1", "2026-09-02T10:00:00Z")]),
                ("agent-bbbbbbbbbbbb", {"name": "general-purpose",
                                        "description": "review the seed data"},
                 [_assistant("b1", "2026-09-02T10:05:00Z")]),
                ("agent-cccccccccccc", None, [_assistant("c1", "2026-09-02T10:06:00Z")])],
        rows=[_assistant("m1", "2026-09-02T09:00:00Z")])
    files = rc.review_agent_files(session)
    assert [p.stem for p in files] == ["agent-aaaaaaaaaaaa"]


def _skill_use(when, tid, skill="code-review"):
    """The parent's own record of invoking a skill — which is where a fork that carries no
    `name` of its own is identified from."""
    return {"type": "assistant", "timestamp": when,
            "message": {"id": "u" + tid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                        "content": [{"type": "tool_use", "id": tid, "name": "Skill",
                                     "input": {"skill": skill, "args": "high"}}]}}


def _skill_result(when, tid):
    return {"type": "user", "timestamp": when,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": "Skill completed (forked execution)."}]}}


def test_a_reviewer_with_no_name_is_found_by_the_parents_skill_call(tmp_path):
    """The first real `/implement-ticket` run: `/code-review high` forked, and the fork's
    `.meta.json` was `{"agentType": "general-purpose", "spawnDepth": 1, …}` — no `name`,
    no `description`, and the run envelope said `subagent_stats.spawned: 0`. Nothing in
    the fork's own files says what it was; the parent's `Skill` tool_use does."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa",
                 {"agentType": "general-purpose", "spawnDepth": 1,
                  "requestShape": "foreground"},
                 [_assistant("a1", "2026-09-02T10:00:10Z"),
                  _assistant("a2", "2026-09-02T10:04:00Z")])],
        rows=[_assistant("m1", "2026-09-02T09:00:00Z"),
              _skill_use("2026-09-02T10:00:00Z", "toolu_1"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1"),
              _assistant("m2", "2026-09-02T10:05:00Z")])
    assert [p.stem for p in rc.review_agent_files(session)] == ["agent-aaaaaaaaaaaa"]
    first, last = rc.agent_span(rc.review_agent_files(session))
    assert (first, last) == (_ts("2026-09-02T10:00:10+00:00"),
                             _ts("2026-09-02T10:04:00+00:00"))


def test_an_errand_outside_the_skill_window_stays_out_of_the_review(tmp_path):
    """The window is bounded by the `tool_result`, so an agent that merely overlaps the
    review — one launched before the skill, or long after it returned — starts outside it
    and is not billed to the review. `pass_costs` was billing two implementation errands
    to the review before it matched on something the harness records."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"agentType": "general-purpose"},
                 [_assistant("a1", "2026-09-02T10:00:10Z")]),
                ("agent-bbbbbbbbbbbb", {"agentType": "general-purpose"},
                 [_assistant("b1", "2026-09-02T09:30:00Z"),     # before the skill call
                  _assistant("b2", "2026-09-02T10:02:00Z")]),   # still running during it
                ("agent-cccccccccccc", {"agentType": "general-purpose"},
                 [_assistant("c1", "2026-09-02T10:06:00Z")])],  # after the result
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1"),
              _assistant("m2", "2026-09-02T10:05:00Z")])
    assert [p.stem for p in rc.review_agent_files(session)] == ["agent-aaaaaaaaaaaa"]


def test_the_named_reviewer_and_the_windowed_one_are_both_reviewers(tmp_path):
    """The two rules add up rather than replacing each other: a session can fork one
    reviewer the harness named and another it did not, and half a review priced as the
    whole of it is worse than either rule alone."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"name": "code-review"},
                 [_assistant("a1", "2026-09-02T09:00:10Z")]),
                ("agent-bbbbbbbbbbbb", {"agentType": "general-purpose"},
                 [_assistant("b1", "2026-09-02T10:00:10Z")])],
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1")])
    assert [p.stem for p in rc.review_agent_files(session)] == [
        "agent-aaaaaaaaaaaa", "agent-bbbbbbbbbbbb"]


def test_a_skill_call_with_no_result_closes_at_the_parents_next_turn(tmp_path):
    """An interrupted run, or a transcript still being written, has the `tool_use` and no
    `tool_result`. The parent's first turn after the subagent's last line is where it
    demonstrably resumed, and that is the honest end of the window."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"agentType": "general-purpose"},
                 [_assistant("a1", "2026-09-02T10:00:10Z"),
                  _assistant("a2", "2026-09-02T10:03:00Z")])],
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1"),
              _assistant("m2", "2026-09-02T10:05:00Z")])
    assert [p.stem for p in rc.review_agent_files(session)] == ["agent-aaaaaaaaaaaa"]


def test_another_skill_does_not_open_a_review_window(tmp_path):
    """`/db` forks too. A window opened by any skill at all would bill whatever it did to
    the review."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"agentType": "general-purpose"},
                 [_assistant("a1", "2026-09-02T10:00:10Z")])],
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1", skill="victor-skills:db"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1")])
    assert rc.review_agent_files(session) == []


def test_a_plugin_prefix_on_the_skill_name_is_not_part_of_it(tmp_path):
    """`code-review`, `victor-skills:code-review` and `code-review:code-review` all name
    the same pass as far as attribution goes."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"agentType": "general-purpose"},
                 [_assistant("a1", "2026-09-02T10:00:10Z")])],
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1",
                         skill="code-review:code-review"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1")])
    assert [p.stem for p in rc.review_agent_files(session)] == ["agent-aaaaaaaaaaaa"]


def test_the_skill_windows_are_the_use_and_the_matching_result(tmp_path):
    session = _session_tree(
        tmp_path,
        rows=[_skill_use("2026-09-02T10:00:00Z", "toolu_1"),
              _skill_result("2026-09-02T10:04:30Z", "toolu_1"),
              _skill_use("2026-09-02T11:00:00Z", "toolu_2")])
    windows, stamps = rc.skill_windows(session)
    assert windows == [(_ts("2026-09-02T10:00:00+00:00"),
                        _ts("2026-09-02T10:04:30+00:00")),
                       (_ts("2026-09-02T11:00:00+00:00"), None)]
    assert stamps == sorted(stamps) and len(stamps) == 3


def test_the_review_window_is_the_span_of_the_reviewers_own_turns(tmp_path):
    session = _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"name": "code-review"},
                 [_assistant("a1", "2026-09-02T10:00:00Z"),
                  _assistant("a2", "2026-09-02T10:20:00Z")]),
                ("agent-dddddddddddd", {"name": "code-review"},
                 [_assistant("d1", "2026-09-02T10:10:00Z"),
                  _assistant("d2", "2026-09-02T10:30:00Z")])],
        rows=[_assistant("m1", "2026-09-02T09:00:00Z")])
    first, last = rc.agent_span(rc.review_agent_files(session))
    assert first == _ts("2026-09-02T10:00:00+00:00")
    assert last == _ts("2026-09-02T10:30:00+00:00")


def test_no_reviewer_transcripts_is_no_window_rather_than_a_zero_length_one(tmp_path):
    assert rc.agent_span([]) == (None, None)


# --------------------------------------------------------------------------- #
# write_window — which turns wrote review-points.md
# --------------------------------------------------------------------------- #

def _tool_use(when, name, inp):
    """An assistant turn whose content is a tool call — and which is billed like any
    other, because it is one."""
    return {"type": "assistant", "timestamp": when,
            "message": {"id": f"t{when}", "model": "claude-sonnet-5-20260101",
                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                        "content": [{"type": "tool_use", "name": name, "input": inp}]}}


def test_writing_the_points_file_is_evidence_and_reading_it_is_not(tmp_path):
    """The same rule `authoring-sessions.py` applies to authorship, pointed at one file —
    which is what lets the review-points row be told apart from the fixes around it."""
    session = _session_tree(tmp_path, rows=[
        _tool_use("2026-09-02T11:00:00Z", "Read", {"file_path": "/repo/review-points.md"}),
        _tool_use("2026-09-02T11:05:00Z", "Write", {"file_path": "/repo/review-points.md"}),
        _tool_use("2026-09-02T11:20:00Z", "Bash",
                  {"command": "cat >> review-points.md <<'EOF'\n## Ignored\nEOF"}),
        _tool_use("2026-09-02T11:30:00Z", "Bash",
                  {"command": "grep -n Ignored review-points.md"}),
    ])
    first, last = rc.write_window(session, "review-points.md")
    assert first == _ts("2026-09-02T11:05:00+00:00"), "the Read must not open the window"
    assert last == _ts("2026-09-02T11:20:00+00:00"), "the grep must not extend it"


def test_a_session_that_never_wrote_the_file_has_no_window(tmp_path):
    session = _session_tree(tmp_path, rows=[_assistant("m1", "2026-09-02T11:00:00Z")])
    assert rc.write_window(session, "review-points.md") == (None, None)


# --------------------------------------------------------------------------- #
# phase_costs — one row per phase, and a reason wherever there is no number
# --------------------------------------------------------------------------- #

def _phase_session(tmp_path):
    """A synthetic run: code written 09:00-09:30, reviewed 10:00-10:30 by one forked
    reviewer, fixes 10:40, the write-up 10:50, commit #2 at 11:00."""
    return _session_tree(
        tmp_path,
        agents=[("agent-aaaaaaaaaaaa", {"name": "code-review"},
                 [_assistant("a1", "2026-09-02T10:00:00Z", in_tok=4000),
                  _assistant("a2", "2026-09-02T10:30:00Z", in_tok=4000)])],
        rows=[_assistant("m0", "2026-09-02T08:00:00Z"),          # before t0
              _assistant("m1", "2026-09-02T09:10:00Z"),          # implementation
              _assistant("m2", "2026-09-02T09:20:00Z"),
              _assistant("m3", "2026-09-02T10:40:00Z"),          # post-review fixes
              _tool_use("2026-09-02T10:50:00Z", "Write",
                        {"file_path": "/repo/review-points.md"}),
              _tool_use("2026-09-02T10:55:00Z", "Edit",
                        {"file_path": "/repo/review-points.md"}),
              _assistant("m6", "2026-09-02T12:00:00Z")])         # after commit #2


def _phases(tmp_path, monkeypatch, **kw):
    session = _phase_session(tmp_path)
    monkeypatch.setattr(rc, "transcript", lambda s: session)
    reviewers = rc.review_agent_files(session)
    stamps = [_ts(s) for s in ("2026-09-02T09:00:00+00:00", "2026-09-02T09:30:00+00:00",
                               "2026-09-02T10:00:00+00:00", "2026-09-02T10:30:00+00:00",
                               "2026-09-02T11:00:00+00:00")]
    return rc.phase_costs("fake", *stamps, reviewers, **kw), session


def test_the_phases_are_the_rows_the_dollar_tab_promises(tmp_path, monkeypatch):
    doc, _ = _phases(tmp_path, monkeypatch)
    assert [r["key"] for r in doc["rows"]] == [
        "implementation", "code_review", "post_review_fixes", "review_points",
        "video", "images", "page_build"]
    assert [r["label"] for r in doc["rows"][:4]] == [
        "implementation", "code-review agents", "post-review fixes", "review-points"]


def test_the_implementation_row_is_bounded_by_the_first_edit_and_commit_one(tmp_path,
                                                                           monkeypatch):
    doc, _ = _phases(tmp_path, monkeypatch)
    row = doc["rows"][0]
    assert row["measured"] and row["messages"] == 2, "08:00 is before t0, 10:40 after t1"
    assert row["window"][0].startswith("2026-09-02T09:00")


def test_the_reviewers_are_priced_whole_because_their_file_is_the_pass(tmp_path,
                                                                      monkeypatch):
    doc, session = _phases(tmp_path, monkeypatch)
    row = next(r for r in doc["rows"] if r["key"] == "code_review")
    exact = rc.collect(rc.review_agent_files(session)[0], None, include_subagents=False)
    assert row["measured"] and row["cost"] == pytest.approx(exact["cost"])
    assert row["messages"] == 2


def test_the_reviewers_turns_are_not_also_charged_to_the_phase_they_ran_in(tmp_path,
                                                                          monkeypatch):
    """They have their own row, priced exactly. Counting them in the window as well would
    bill the review twice — which is what makes the rows stop adding up."""
    doc, _ = _phases(tmp_path, monkeypatch)
    fixes = next(r for r in doc["rows"] if r["key"] == "post_review_fixes")
    assert fixes["messages"] == 1, "10:40 only: the write-up's turns are the next row"


def test_the_write_up_is_its_own_row_and_is_subtracted_from_the_fixes(tmp_path,
                                                                     monkeypatch):
    """A sub-window of the fixes phase, taken out of it so the rows still sum to the
    total instead of counting the same turns in two places."""
    doc, _ = _phases(tmp_path, monkeypatch)
    points = next(r for r in doc["rows"] if r["key"] == "review_points")
    fixes = next(r for r in doc["rows"] if r["key"] == "post_review_fixes")
    assert points["measured"] and points["messages"] == 2
    assert "less the 2 turn(s)" in fixes["detail"]
    assert doc["messages"] == sum(r["messages"] for r in doc["rows"] if r["measured"])


def test_the_windows_are_reported_because_a_window_is_a_bound_not_a_fence(tmp_path,
                                                                         monkeypatch):
    doc, _ = _phases(tmp_path, monkeypatch)
    assert doc["boundaries"]["t0"].startswith("2026-09-02T09:00")
    assert doc["boundaries"]["t4"].startswith("2026-09-02T11:00")
    for row in doc["rows"][:1]:
        assert row["window"][0] and row["window"][1]


def test_an_undatable_phase_says_why_instead_of_saying_zero(tmp_path, monkeypatch):
    """`$0.00` and "we could not date this" render identically and mean opposite things:
    one is a phase that cost nothing, the other a phase whose cost is in another row."""
    session = _phase_session(tmp_path)
    monkeypatch.setattr(rc, "transcript", lambda s: session)
    doc = rc.phase_costs("fake", None, None, None, None, None, [])
    # Every row but the write-up, which needs no commit to be dated — it is found by the
    # turns that wrote the file, and that is the point of measuring it that way.
    undated = [r for r in doc["rows"] if r["key"] != "review_points"]
    for row in undated:
        assert row["measured"] is False
        assert row["reason"], f"{row['key']} has neither a number nor a reason"
        assert row["cost"] == 0.0
    assert set(doc["unmeasured"]) == {r["key"] for r in undated}
    points = next(r for r in doc["rows"] if r["key"] == "review_points")
    assert points["measured"] and points["messages"] == 2
    impl = next(r for r in doc["rows"] if r["key"] == "implementation")
    assert "no first edit" in impl["reason"]
    review = next(r for r in doc["rows"] if r["key"] == "code_review")
    assert "inline" in review["reason"], "the pass that cannot be priced has to say so"


def test_no_transcript_at_all_is_one_reason_on_every_coding_row(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "transcript", lambda s: None)
    doc = rc.phase_costs("gone", None, None, None, None, None, [])
    coding = [r for r in doc["rows"] if r["key"] in
              ("implementation", "code_review", "post_review_fixes", "review_points")]
    assert all(not r["measured"] for r in coding)
    assert all("no transcript on disk" in r["reason"] for r in coding)
    doc = rc.phase_costs(None, None, None, None, None, None, [])
    assert all("no session id" in r["reason"] for r in doc["rows"][:4])


def test_the_page_building_rows_come_from_the_step_ledger_of_that_run(tmp_path,
                                                                     monkeypatch):
    """The video and the images are not the coding session at all — they are steps of the
    run that built the page, and the point of the row is to put both halves of the bill in
    one table."""
    build = tmp_path / "build.jsonl"
    build.write_text("\n".join(json.dumps(r) for r in [
        _assistant("v1", "2026-09-03T10:05:00Z"),     # inside the video step
        _assistant("d1", "2026-09-03T11:05:00Z"),     # inside the dsaudit step
        _assistant("g1", "2026-09-03T12:05:00Z"),     # the guide pseudo-tab
        _assistant("x1", "2026-09-03T13:05:00Z"),     # no step at all
    ]) + "\n", encoding="utf-8")
    steps = tmp_path / ".steps.json"
    steps.write_text(json.dumps([
        {"tabs": ["video"], "label": "record", "start": "2026-09-03T10:00:00+00:00",
         "end": "2026-09-03T10:10:00+00:00"},
        {"tabs": ["dsaudit"], "label": "shots", "start": "2026-09-03T11:00:00+00:00",
         "end": "2026-09-03T11:10:00+00:00"},
        {"tabs": ["guide"], "label": "assemble", "start": "2026-09-03T12:00:00+00:00",
         "end": "2026-09-03T12:10:00+00:00"},
    ]), encoding="utf-8")
    coding = _phase_session(tmp_path)
    monkeypatch.setattr(rc, "transcript",
                        lambda s: build if s == "build-run" else coding)
    monkeypatch.setattr(rc, "subagent_transcripts",
                        lambda path: [] if path == build else
                        sorted((coding.parent / "abc" / "subagents").glob("agent-*.jsonl")))
    doc = rc.phase_costs("fake", None, None, None, None, None, [],
                         run_session="build-run", steps_path=steps)
    video = next(r for r in doc["rows"] if r["key"] == "video")
    images = next(r for r in doc["rows"] if r["key"] == "images")
    page = next(r for r in doc["rows"] if r["key"] == "page_build")
    assert video["measured"] and video["messages"] == 1
    assert images["measured"] and images["messages"] == 1
    assert page["measured"] and page["messages"] == 1, (
        "the guide turn only -- the stray one ran nothing and is somebody else's work")
    rest = next(r for r in doc["rows"] if r["key"] == "not_this_report")
    assert rest["measured"] and rest["excluded"] and rest["messages"] == 1


def test_a_narrow_tab_list_must_not_report_the_other_tabs_as_drift(tmp_path, monkeypatch,
                                                                   capsys):
    """Asking `tab_cost_report` for two tabs makes every other tab in the ledger look like
    a renamed one — and that warning would be printed as the reason the video row has no
    number, which is a different and untrue thing."""
    build = tmp_path / "build.jsonl"
    build.write_text(json.dumps(_assistant("v1", "2026-09-03T10:05:00Z")) + "\n",
                     encoding="utf-8")
    steps = tmp_path / ".steps.json"
    steps.write_text(json.dumps([
        {"tabs": ["sequence"], "label": "diagrams", "start": "2026-09-03T09:00:00+00:00",
         "end": "2026-09-03T09:10:00+00:00"},
        {"tabs": ["video"], "label": "record", "start": "2026-09-03T10:00:00+00:00",
         "end": "2026-09-03T10:10:00+00:00"},
    ]), encoding="utf-8")
    monkeypatch.setattr(rc, "transcript", lambda s: build)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    doc = rc.phase_costs(None, None, None, None, None, None, [],
                         run_session="build-run", steps_path=steps)
    images = next(r for r in doc["rows"] if r["key"] == "images")
    assert "drifted apart" not in (images["reason"] or "")
    assert "no step in the ledger named it" in images["reason"]
    assert "drifted apart" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the ledger's phases key
# --------------------------------------------------------------------------- #

def test_a_missing_phases_file_is_a_reason_not_an_empty_table(tmp_path):
    got = rc.load_phases(tmp_path / "phases.json")
    assert got["measured"] is False and got["rows"] == []
    assert "session-cost.py" in got["reason"]


def test_a_phases_file_from_something_else_is_refused(tmp_path):
    p = tmp_path / "phases.json"
    p.write_text(json.dumps({"rows": "nope"}), encoding="utf-8")
    assert "not something session-cost.py wrote" in rc.load_phases(p)["reason"]
    p.write_text("{oops", encoding="utf-8")
    assert "unreadable" in rc.load_phases(p)["reason"]


def test_the_ledger_carries_the_phases_through_to_the_page(tmp_path, monkeypatch):
    """The `$` tab reads one document. A phase breakdown that only existed in a terminal
    would be a second, invisible answer to the same question."""
    monkeypatch.setattr(rc, "transcript", lambda s: None)
    monkeypatch.setattr(rc, "authoring_cost", lambda *a, **k: {"measured": False,
                                                               "cost": 0.0, "tokens": 0})
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / "phases.json").write_text(json.dumps(
        {"rows": [{"key": "implementation", "label": "implementation", "measured": True,
                   "cost": 1.5, "tokens": 10, "messages": 2}],
         "measured": True, "cost": 1.5}), encoding="utf-8")
    got = rc.ledger(None, None, tmp_path / "nope.json", ["data"], "origin/main", tmp_path)
    assert got["phases"]["measured"] is True
    assert got["phases"]["rows"][0]["key"] == "implementation"
    assert got["total"] == pytest.approx(0.0), (
        "the phases are a view INSIDE the same money, never added to it")


# --------------------------------------------------------------------------- #
# Which turns built THIS report -- and which ones the pinned session spent on
# something else entirely
# --------------------------------------------------------------------------- #

def _bash(when, command, mid=None):
    """An assistant turn whose tool call is a shell command, with its result on the next."""
    tid = mid or f"b{when}"
    return {"type": "assistant", "timestamp": when,
            "message": {"id": tid, "model": "claude-sonnet-5-20260101",
                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                        "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                     "input": {"command": command}}]}}


def _result(when, tid):
    return {"type": "user", "timestamp": when,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": "ok"}]}}


@pytest.mark.parametrize("command", [
    "python3 refresh-report.py --no-model",
    "refresh-report.py",
    "cd /repo && python3 .claude/skills/human-review/scripts/build-review-html.py c.json",
    "timeout 600 python3 run-steps.py --steps 3",
    "FOO=1 python3 rerun-model.py",
    "python3 -c 'import x' && refresh-report.py --redraw",
])
def test_a_turn_that_runs_a_builder_is_page_building(command):
    assert rc.runs_builder(command) is True


@pytest.mark.parametrize("command", [
    "sed -i '' 's/a/b/' build-review-html.py",
    "git add run-steps.py && git commit -m 'wip'",
    "grep -n residual refresh-report.py",
    "cat build-review-html.py | head -20",
    "ls -la scripts/refresh-report.py",
    "rg rerun-model.py .",
])
def test_editing_or_reading_a_builder_is_not_running_it(command):
    """The distinction the whole row rests on. The sessions that build these pages are
    usually also *writing* the program that builds them, and a rule that matched the
    filename anywhere in the line would put the skill's own development straight back into
    the row this exists to take it out of."""
    assert rc.runs_builder(command) is False


def test_a_build_window_covers_the_call_and_the_turn_that_reads_it(tmp_path):
    session = _session_tree(tmp_path, rows=[
        _bash("2026-09-03T10:00:00Z", "python3 refresh-report.py", mid="t1"),
        _result("2026-09-03T10:02:00Z", "t1"),
        _assistant("m2", "2026-09-03T10:02:30Z"),
    ])
    windows = rc.build_windows(session)
    assert len(windows) == 1
    start, end = windows[0]
    assert start == _ts("2026-09-03T10:00:00+00:00")
    assert end == _ts("2026-09-03T10:02:30+00:00"), (
        "reading the build's output is the other half of running it")


def test_overlapping_build_windows_are_merged_not_double_counted(tmp_path):
    """The build is often driven from a subagent while the parent waits on it. Two
    overlapping windows would charge the same turn to the same row twice."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-bbbbbbbbbbbb", {"name": "builder"}, [
            _bash("2026-09-03T10:01:00Z", "python3 build-review-html.py c.json", mid="s1"),
            _result("2026-09-03T10:04:00Z", "s1")])],
        rows=[_bash("2026-09-03T10:00:00Z", "python3 refresh-report.py", mid="t1"),
              _result("2026-09-03T10:03:00Z", "t1")])
    windows = rc.build_windows(session)
    assert len(windows) == 1
    assert windows[0][0] == _ts("2026-09-03T10:00:00+00:00")
    assert windows[0][1] == _ts("2026-09-03T10:04:00+00:00")


def _steps_doc(*rows):
    return [{"tabs": list(t), "label": l, "start": _ts(a), "end": _ts(b)}
            for t, l, a, b in rows]


def test_the_residual_splits_into_ours_and_somebody_else_s(tmp_path):
    steps = _steps_doc((["data"], "diagram", "2026-09-03T09:00:00+00:00",
                        "2026-09-03T09:10:00+00:00"))
    windows = [(_ts("2026-09-03T10:00:00+00:00"), _ts("2026-09-03T10:10:00+00:00"))]
    turns = [_turn("2026-09-03T09:05:00Z"),   # inside a step: neither half
             _turn("2026-09-03T10:05:00Z"),   # ran the build: ours
             _turn("2026-09-03T11:05:00Z"),   # somebody else's evening
             _turn(None)]                     # no timestamp: cannot be ours
    got = rc.split_residual(turns, steps, windows, {"data", rc.GUIDE_TAB})
    assert got["ours"]["messages"] == 1
    assert got["theirs"]["messages"] == 2


def test_a_step_naming_only_tabs_this_page_lacks_does_not_claim_its_turns(tmp_path):
    """Same filter `tab_costs` uses. A split of the residual that disagreed about what the
    residual IS would not add up to it."""
    steps = _steps_doc((["ghost"], "renamed", "2026-09-03T09:00:00+00:00",
                        "2026-09-03T09:10:00+00:00"))
    got = rc.split_residual([_turn("2026-09-03T09:05:00Z")], steps, [], {"data"})
    assert got["theirs"]["messages"] == 1


def test_the_other_work_row_says_what_it_was_from_its_own_tool_calls(tmp_path):
    """"$347 of something else" is a number nobody can act on without opening a
    transcript. Counting what those turns did turns it into a sentence."""
    session = _session_tree(tmp_path, rows=[
        _bash("2026-09-03T11:00:00Z", "python3 -m pytest test_x.py"),
        _bash("2026-09-03T11:01:00Z", "python3 -m pytest test_y.py"),
        _tool_use("2026-09-03T11:02:00Z", "Write", {"file_path": "/repo/skill.py"}),
        _bash("2026-09-03T11:03:00Z", "git commit -m wip"),
    ])
    said = rc.other_work(session, [], {"data"})
    assert "running tests (2)" in said
    assert "editing files (1)" in said and "git (1)" in said
    assert "1 file(s) written" in said


def test_the_catch_all_shell_bucket_never_crowds_out_what_the_turns_were_doing(tmp_path):
    """It is the commonest bucket on every transcript and the one that says least."""
    rows = [_bash(f"2026-09-03T11:{n:02d}:00Z", f"ls -la /tmp/{n}") for n in range(20)]
    rows.append(_bash("2026-09-03T11:30:00Z", "python3 -m pytest test_x.py"))
    session = _session_tree(tmp_path, rows=rows)
    said = rc.other_work(session, [], {"data"}, limit=1)
    assert said.startswith("running tests (1)"), said
    assert rc.SHELL_BUCKET not in said


def test_a_session_with_no_tool_calls_at_all_still_says_something(tmp_path):
    session = _session_tree(tmp_path, rows=[_assistant("m1", "2026-09-03T11:00:00Z")])
    assert "no tool call" in rc.other_work(session, [], {"data"})


def test_a_build_in_one_agent_does_not_claim_what_another_did_meanwhile(tmp_path):
    """A run forks. While one agent sits inside a two-minute `run-steps.py`, three others
    are doing something else entirely — and judged against one merged timeline all three
    would be billed to the page. A build belongs to the conversation that ran it."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-cccccccccccc", {"name": "builder"}, [
                    _bash("2026-09-03T10:00:00Z", "python3 run-steps.py --steps all",
                          mid="s1"),
                    _result("2026-09-03T10:05:00Z", "s1")]),
                ("agent-dddddddddddd", {"name": "other"}, [
                    _assistant("o1", "2026-09-03T10:02:00Z"),
                    _assistant("o2", "2026-09-03T10:03:00Z")])],
        rows=[])
    got = rc.run_residual(session, [], {"data"})
    assert got["ours"]["messages"] == 1, "the turn that ran the build, and only it"
    assert got["theirs"]["messages"] == 2, "the other agent was not building anything"
    # The merged view still answers "when was this run building", which is a real question
    # and the reason it is kept.
    assert rc.build_windows(session) == [(_ts("2026-09-03T10:00:00+00:00"),
                                          _ts("2026-09-03T10:05:00+00:00"))]


def test_the_two_halves_still_add_up_to_the_residual_they_split(tmp_path):
    """The dedupe has to be `gather_turns`' or the phase table quietly stops balancing."""
    session = _session_tree(
        tmp_path,
        agents=[("agent-eeeeeeeeeeee", {"name": "x"}, [
            _bash("2026-09-03T10:00:00Z", "python3 refresh-report.py", mid="s1"),
            _result("2026-09-03T10:00:30Z", "s1"),
            _assistant("o1", "2026-09-03T11:00:00Z")])],
        rows=[_assistant("p1", "2026-09-03T09:00:00Z")])
    got = rc.run_residual(session, [], {"data"})
    turns, _ = rc.gather_turns(session, None)
    assert got["ours"]["messages"] + got["theirs"]["messages"] == len(turns)
    assert got["ours"]["cost"] + got["theirs"]["cost"] == pytest.approx(
        sum(rc.price(rc.family(m), u) for _k, m, u, _s, _w in turns))


def _build_run(tmp_path, monkeypatch, rows):
    build = tmp_path / "build.jsonl"
    build.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    steps = tmp_path / ".steps.json"
    steps.write_text(json.dumps([
        {"tabs": ["guide"], "label": "assemble", "start": "2026-09-03T12:00:00+00:00",
         "end": "2026-09-03T12:10:00+00:00"},
    ]), encoding="utf-8")
    monkeypatch.setattr(rc, "transcript", lambda s: build)
    monkeypatch.setattr(rc, "subagent_transcripts", lambda path: [])
    return rc.phase_costs(None, None, None, None, None, None, [],
                          run_session="build-run", steps_path=steps)


def test_the_page_is_not_charged_for_the_rest_of_the_session_that_built_it(tmp_path,
                                                                          monkeypatch):
    """The regression this row exists for: the pinned session spent the same evening
    writing the skill, and `page build` swallowed all of it -- $170 of a $207 total for a
    page whose own build was $23."""
    doc = _build_run(tmp_path, monkeypatch, [
        _assistant("g1", "2026-09-03T12:05:00Z"),                       # the guide step
        _bash("2026-09-03T13:00:00Z", "python3 refresh-report.py", mid="t1"),
        _result("2026-09-03T13:01:00Z", "t1"),
        _bash("2026-09-03T14:00:00Z", "python3 -m pytest test_skill.py"),
        _tool_use("2026-09-03T14:05:00Z", "Edit", {"file_path": "/repo/skill.py"}),
    ])
    page = next(r for r in doc["rows"] if r["key"] == "page_build")
    rest = next(r for r in doc["rows"] if r["key"] == "not_this_report")
    assert page["messages"] == 2, "the guide step and the turn that ran the builder"
    assert rest["messages"] == 2, "the pytest run and the edit to the skill"
    assert rest["label"].startswith("not this report")
    assert "running tests" in rest["detail"] and "editing files" in rest["detail"]


def test_the_other_work_is_measured_and_still_kept_out_of_the_total(tmp_path, monkeypatch):
    """Measured is not the same as owed. It is printed -- hiding a real number teaches the
    reader the evening was cheaper than it was -- and it is not summed."""
    doc = _build_run(tmp_path, monkeypatch, [
        _assistant("g1", "2026-09-03T12:05:00Z"),
        _assistant("x1", "2026-09-03T15:00:00Z", in_tok=999_000),
    ])
    rest = next(r for r in doc["rows"] if r["key"] == "not_this_report")
    assert rest["measured"] is True and rest["excluded"] is True
    assert rest["cost"] > 0
    assert doc["excluded"] == ["not_this_report"]
    assert doc["cost"] == pytest.approx(
        sum(r["cost"] for r in doc["rows"] if r["measured"] and not r["excluded"]))
    assert rest["cost"] not in [doc["cost"]]
    assert doc["messages"] == sum(r["messages"] for r in doc["rows"]
                                 if r["measured"] and not r["excluded"])


def test_a_session_that_did_nothing_else_grows_no_extra_row(tmp_path, monkeypatch):
    """The row is evidence of a shared session, not furniture. A run that only built the
    page must not print `$0.00 of other work` and invite the question."""
    doc = _build_run(tmp_path, monkeypatch, [_assistant("g1", "2026-09-03T12:05:00Z")])
    assert [r["key"] for r in doc["rows"] if r["key"] == "not_this_report"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
