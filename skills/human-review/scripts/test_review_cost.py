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
    assert page["measured"] and page["messages"] == 2, "the guide turn plus the stray one"


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
