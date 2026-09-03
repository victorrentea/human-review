#!/usr/bin/env python3
"""Step 0's build gate: pushed, and green *for the commit that was pushed*, before anything else.

A review of a branch that does not build is worse than no review — it is a confident-looking
guide about code nobody has proven compiles, every number on it measured from a tree of
unknown status. The rule is only worth anything if it stays *first*, so what is pinned here is
its **position** as much as its wording: an edit that moves the gate below the asset wipe, or
below the first ledger stamp, turns a gate into a decoration and would otherwise be invisible
in review — the prose still reads exactly as convincing in the wrong place.

Run with:  python3 -m pytest test_skill_ci_gate.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _at(text: str, needle: str) -> int:
    i = text.find(needle)
    assert i != -1, f"SKILL.md no longer contains {needle!r}"
    return i


def test_the_gate_section_exists():
    assert re.search(r"###[^\n]*push, then wait for green", _text()), (
        "Step 0 has no build gate — a review can start on a branch CI has never seen"
    )


def test_the_gate_comes_before_anything_destructive_or_expensive():
    """It has to precede the wipe specifically: a run that stops at the gate must leave the
    previous guide intact rather than deleting it on the way out."""
    text = _text()
    gate = _at(text, "push, then wait for green")
    for label, needle in (
        ("the assets wipe", "rm -rf .human-review/assets"),
        ("the ledger reset", "steps-ledger.py reset"),
        ("the run's start marker", ".human-review/.started"),
    ):
        assert gate < _at(text, needle), (
            f"the build gate now appears after {label} — a run that stops at the gate would "
            "already have destroyed the previous run's artifacts"
        )


def test_the_gate_comes_before_the_first_step_is_stamped():
    text = _text()
    gate = _at(text, "push, then wait for green")
    first_start = min(m.start() for m in
                      re.finditer(r"steps-ledger\.py start [a-zA-Z]", text))
    assert gate < first_start, (
        "a step is stamped before the build gate runs — the gate is no longer a gate"
    )


def test_the_wait_is_bound_to_the_pushed_commit_not_the_branch():
    """The failure this prevents is a green run for a *different* commit. A branch almost
    always has some green run on it, which is what makes `--branch` so tempting and so wrong."""
    text = _text()
    gate = text[_at(text, "push, then wait for green"):]
    gate = gate[:gate.index("**Then wipe")]
    assert re.search(r"gh run list --commit", gate), (
        "the gate does not filter runs by commit SHA — it would accept a green build for "
        "some other commit on the same branch"
    )
    assert not re.search(r"gh run list[^\n]*--branch", gate), (
        "the gate filters runs by branch, which is the exact confidently-wrong signal it "
        "exists to reject"
    )
    assert "rev-parse HEAD" in gate, "the gate never captures the SHA it pushed"


def test_the_gate_refuses_to_read_no_build_as_a_pass():
    """The state that passes silently when written carelessly: `gh run list` returns `[]`
    because nothing ever built this commit, and an unguarded check reads that as 'no failures'."""
    gate = _text()
    gate = gate[_at(gate, "push, then wait for green"):]
    gate = gate[:gate.index("**Then wipe")]
    assert "Absence is not success" in gate, (
        "the gate does not say what an empty run list means — absence of a build is the one "
        "non-green state that looks like a pass"
    )


def test_a_repo_without_ci_is_let_through_but_reported():
    gate = _text()
    gate = gate[_at(gate, "push, then wait for green"):]
    gate = gate[:gate.index("**Then wipe")]
    assert "gh workflow list" in gate, "no way to tell 'no CI at all' from 'no run for this commit'"
    assert "no build proved this" in gate, (
        "a repository with no CI passes the gate silently — the guide must say that nothing "
        "proved this, or the absence reads as a pass"
    )


def test_the_gate_does_not_ask_for_the_reviews_own_fixes_to_be_pushed():
    """The skill deliberately leaves its own fixes uncommitted for a human to inspect. A gate
    read as 'push everything' would commit them away and defeat that."""
    gate = _text()
    gate = gate[_at(gate, "push, then wait for green"):]
    gate = gate[:gate.index("**Then wipe")]
    assert "not the tree at the end" in gate and "uncommitted" in gate, (
        "the gate does not distinguish 'the branch as pushed' from 'the tree at the end of "
        "the run' — someone will read it as an instruction to push the review's own fixes"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
