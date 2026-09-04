#!/usr/bin/env python3
"""What `review-passes.py` may and may not call a review pass.

Two failure directions, and they are not symmetric. Missing a pass that ran costs a re-run
of the most expensive thing in the pipeline. *Inventing* one is worse: the skill would go on
to assemble a page of findings from a review that never happened, which is the confident
wrong page this whole project exists to avoid.

The false positive that motivated this file is the one nobody would predict: the harness
records tool output as `user` rows, so a session that greps its own transcript for
`<command-name>` puts that very tag into the transcript — and a detector that reads a whole
`user` row then reports its own evidence-gathering as the pass it was looking for.

Run with:  python3 -m pytest test_review_passes.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("review_passes", HERE / "review-passes.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

T0 = "2026-09-04T10:00:00.000Z"


def _row(**kw):
    kw.setdefault("timestamp", T0)
    return kw


def _typed(text: str):
    return _row(type="user", message={"role": "user", "content": text})


def _tool_result(text: str):
    return _row(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "content": text}]})


def _assistant(text: str):
    return _row(type="assistant", message={"role": "assistant", "content": [
        {"type": "text", "text": text}]})


def _write(tmp_path: Path, rows: list) -> Path:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_a_slash_command_the_human_typed_is_a_pass(tmp_path):
    f = _write(tmp_path, [_typed("<command-name>/code-review</command-name>"),
                          _assistant("Found three things.")])
    hits = rp.scan_main(f, None)
    assert [h["pass"] for h in hits] == ["code-review"]
    assert hits[0]["how"] == "slash command"
    assert "three things" in hits[0]["output"]


def test_the_same_tag_inside_a_tool_result_is_not_a_pass(tmp_path):
    """The detector must not report its own evidence-gathering. A run that greps the
    transcript for `<command-name>` writes that tag into a tool result, which is a `user`
    row like any other."""
    f = _write(tmp_path, [_tool_result("<command-name>/simplify</command-name>"),
                          _assistant("here is the grep output")])
    assert rp.scan_main(f, None) == []


def test_a_command_merely_mentioned_in_prose_is_not_a_pass(tmp_path):
    f = _write(tmp_path, [_typed("should I run /code-review now?"), _assistant("Probably.")])
    assert rp.scan_main(f, None) == []


def test_an_unrelated_command_is_ignored(tmp_path):
    f = _write(tmp_path, [_typed("<command-name>/clear</command-name>")])
    assert rp.scan_main(f, None) == []


def test_an_unknown_review_shaped_command_counts_as_custom(tmp_path):
    """A project's own pass is exactly the case this must not be blind to."""
    f = _write(tmp_path, [_typed("<command-name>/my-adversarial-review</command-name>"),
                          _assistant("...")])
    assert [h["pass"] for h in rp.scan_main(f, None)] == ["custom"]


def test_a_skill_invocation_counts(tmp_path):
    f = _write(tmp_path, [_row(type="assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "code-review:code-review"}}]}),
        _assistant("done")])
    hits = rp.scan_main(f, None)
    assert [h["pass"] for h in hits] == ["code-review"]
    assert hits[0]["how"] == "Skill tool"


def test_output_stops_at_the_next_human_turn_not_at_the_first_tool_result(tmp_path):
    """Tool results are `user` rows, so ending the extraction at the first `user` row would
    end it at the pass's first tool call — which is to say, immediately."""
    f = _write(tmp_path, [
        _typed("<command-name>/code-review</command-name>"),
        _assistant("first"), _tool_result("some file"), _assistant("second"),
        _typed("thanks"), _assistant("not part of the pass")])
    out = rp.scan_main(f, None)[0]["output"]
    assert "first" in out and "second" in out
    assert "not part of the pass" not in out


def test_report_findings_is_harvested_as_data(tmp_path):
    finding = {"file": "A.java", "summary": "npe", "failure_scenario": "null owner"}
    f = _write(tmp_path, [
        _typed("<command-name>/code-review</command-name>"),
        _row(type="assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "ReportFindings",
             "input": {"findings": [finding], "level": "high"}}]})])
    hit = rp.scan_main(f, None)[0]
    assert hit["findings"] == [finding]
    assert hit["fidelity"] == "structured"


def test_prose_only_output_is_labelled_as_such(tmp_path):
    f = _write(tmp_path, [_typed("<command-name>/simplify</command-name>"),
                          _assistant("I removed a wrapper.")])
    assert rp.scan_main(f, None)[0]["fidelity"] == "prose"


def test_a_forked_review_subagent_is_found_by_its_meta(tmp_path):
    """`/code-review` runs forked by default, so a detector that only reads the parent
    transcript would report that nothing was reviewed."""
    session = tmp_path / "s.jsonl"
    session.write_text(json.dumps(_assistant("spawning")), encoding="utf-8")
    subs = tmp_path / "s" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-abc123456789.jsonl").write_text(
        json.dumps(_assistant("Reuse: three duplicated helpers.")), encoding="utf-8")
    (subs / "agent-abc123456789.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "Reuse review",
                    "model": "fable"}), encoding="utf-8")
    hits = rp.scan_subagents(session, None)
    assert [h["pass"] for h in hits] == ["code-review"]
    assert "fable" in hits[0]["how"]
    assert "duplicated helpers" in hits[0]["output"]


def test_a_subagent_that_is_not_a_review_is_ignored(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text(json.dumps(_assistant("x")), encoding="utf-8")
    subs = tmp_path / "s" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-deadbeef1234.jsonl").write_text(json.dumps(_assistant("y")), encoding="utf-8")
    (subs / "agent-deadbeef1234.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "Find the config loader"}),
        encoding="utf-8")
    assert rp.scan_subagents(session, None) == []


def test_extract_writes_one_file_per_pass(tmp_path, monkeypatch):
    f = _write(tmp_path, [_typed("<command-name>/code-review</command-name>"),
                          _assistant("A finding.")])
    monkeypatch.setattr(rp, "transcript", lambda _s: f)
    out = tmp_path / "passes"
    rc = rp.main(["--session", "s", "--extract", str(out)])
    assert rc == 0
    written = sorted(p.name for p in out.iterdir())
    assert written == ["01-code-review.md"]
    assert "A finding." in (out / "01-code-review.md").read_text()


def test_require_exits_3_when_nothing_was_reviewed(tmp_path, monkeypatch):
    """The gate the whole inversion rests on: no pass, no write-up."""
    f = _write(tmp_path, [_typed("just chatting"), _assistant("sure")])
    monkeypatch.setattr(rp, "transcript", lambda _s: f)
    assert rp.main(["--session", "s", "--require"]) == 3


def test_require_exits_0_when_something_was_reviewed(tmp_path, monkeypatch):
    f = _write(tmp_path, [_typed("<command-name>/simplify</command-name>"), _assistant("ok")])
    monkeypatch.setattr(rp, "transcript", lambda _s: f)
    assert rp.main(["--session", "s", "--require"]) == 0


def test_a_missing_transcript_is_its_own_exit_code(tmp_path, monkeypatch):
    """2, not 3: 'I cannot tell' must not be reported as 'nothing was reviewed'."""
    monkeypatch.setattr(rp, "transcript", lambda _s: None)
    assert rp.main(["--session", "nope", "--require"]) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
