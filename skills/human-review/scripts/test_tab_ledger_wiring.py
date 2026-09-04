#!/usr/bin/env python3
"""Every tab the page declares is fed by a step, every step stamps a tab that exists, and a
step that is stamped is always closed.

This used to read SKILL.md and check that a runbook's thirteen hand-written `steps-ledger.py
start` wraps still named tabs the worked schema recognised. The wraps are code now
(`run-steps.py`'s `STEPS` table), so the drift it was guarding against is checked against the
table itself — and the two invariants that a text test could only ever hope for are executed
here instead:

  * a step whose prerequisite fails is **never stamped**, so no record can name a tab the
    page will not contain;
  * a step that *is* stamped is **always closed**, including when it raises.

Run with:  python3 -m pytest test_tab_ledger_wiring.py
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCHEMA = HERE.parent / "reference" / "content-schema.md"
SKILL_MD = HERE.parent / "SKILL.md"

_spec = importlib.util.spec_from_file_location("run_steps", HERE / "run-steps.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

GUIDE = "guide"
# Tabs no step produces, for three different reasons worth keeping apart:
#   overview     — synthesised from the others; never will have a step.
#   requirements — a real tab whose step has not been built yet; will get one.
# Both honestly report "not measured", so they are exempt — but only by being named here, so
# that building the missing step forces a deliberate edit rather than quietly satisfying a
# test that had stopped looking.
UNFED_BY_DESIGN = {"overview", "requirements"}


def _schema_tab_ids() -> set[str]:
    text = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r'"tabs":\s*\[(.*?)\n\]', text, re.S)
    assert m, 'could not find the worked `"tabs": [...]` example in content-schema.md'
    return set(re.findall(r'\{"id"\s*:\s*"([a-zA-Z0-9_]+)"', m.group(1)))


def _step_tabs() -> set[str]:
    tabs: set[str] = set()
    for _name, t, _label, _prereq, _fn in rs.STEPS:
        if t:
            tabs |= set(t.split(","))
    return tabs


def test_the_parsers_find_what_this_test_expects():
    """A sanity check on the regex and the import, so a shape change that silently matched
    nothing fails loudly instead of making every real test below vacuously pass."""
    assert _schema_tab_ids() == {
        "review", "behaviour", "sequence", "requirements", "data",
        "packages", "api", "city", "complexity", "logging", "dsaudit", "owners",
    }
    assert len(rs.STEPS) >= 10


def test_every_step_stamps_a_tab_the_page_actually_has():
    unknown = _step_tabs() - _schema_tab_ids() - {GUIDE}
    assert not unknown, (
        f"run-steps.py stamps tab(s) {sorted(unknown)} that the schema does not declare — "
        "a typo, or the schema moved on without the step table"
    )


def test_every_tab_has_a_step_behind_it():
    unfed = _schema_tab_ids() - _step_tabs() - UNFED_BY_DESIGN - {"review"}
    assert not unfed, (
        f"tab(s) {sorted(unfed)} are declared but no step produces them — every run will "
        'show "not measured" on that tab'
    )


def test_the_id_table_covers_every_declared_tab():
    text = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"\| `id` \| label on the reference page \|.*?\n\n", text, re.S)
    assert m, "could not find the canonical tab-id table in content-schema.md"
    tabled = set(re.findall(r"`([a-z][a-z0-9_]*)`", m.group(0).split("\n", 2)[2]))
    missing = _schema_tab_ids() - tabled
    assert not missing, f"tab(s) {sorted(missing)} are declared but not in the id table"
    assert GUIDE in tabled, "the reserved `guide` id is not documented"


def test_the_skill_still_stamps_the_pages_own_assembly():
    """Writing `content.json` is the model's work and the largest single stretch of a run.
    It cannot be attributed to a tab, so unstamped it lands in the residual beside idle time
    and the breakdown degenerates into one row carrying most of the bill."""
    text = SKILL_MD.read_text(encoding="utf-8")
    assert re.search(r"steps-ledger\.py start guide\b", text), (
        "SKILL.md no longer opens the `guide` step — Step 4's own cost disappears into an "
        "undifferentiated residual"
    )
    assert re.search(r"steps-ledger\.py end \"\$\(cat \.human-review/\.step-guide\)\"", text), (
        "the `guide` step is opened and never closed"
    )


def test_the_reserved_id_is_spelled_the_same_in_both_scripts():
    for script in ("steps-ledger.py", "review-cost.py"):
        src = (HERE / script).read_text(encoding="utf-8")
        assert f'GUIDE_TAB = "{GUIDE}"' in src, f"{script} does not reserve `{GUIDE}`"


# --------------------------------------------------------------- the executable invariants

class _Ledger:
    """Records what run_step asked the ledger to do, without touching a real one."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, argv, **kw):
        import subprocess
        verb = argv[2]
        self.calls.append((verb, argv[3] if len(argv) > 3 else ""))
        return subprocess.CompletedProcess(argv, 0, "7", "")


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    log = _Ledger()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rs.subprocess, "run", log)
    return log


def _ctx():
    return rs.Ctx(base="origin/main", cfg={}, dry=False)


def test_a_step_whose_prerequisite_fails_is_never_stamped(ledger):
    """The gate comes before the stamp. A record naming a tab the page will not contain is
    drift the build has to shout about, so it must never be written in the first place."""
    result = rs.run_step("logging", "logging", "l", lambda c: "ast-grep not installed",
                         lambda c: None, _ctx())
    assert result["status"] == rs.SKIPPED
    assert result["reason"] == "ast-grep not installed"
    assert ledger.calls == [], "a skipped step stamped the ledger anyway"


def test_a_stamped_step_is_closed_even_when_it_raises(ledger):
    """An open record makes the page report 'started but never recorded finishing' — a step
    that died — when in fact the runner simply failed to close it."""
    def boom(_ctx):
        raise RuntimeError("the producer exploded")

    result = rs.run_step("api", "api", "a", None, boom, _ctx())
    assert result["status"] == rs.FAILED
    assert "exploded" in result["reason"]
    verbs = [v for v, _ in ledger.calls]
    assert verbs == ["start", "end"], f"a failing step did not close its record: {verbs}"


def test_a_step_that_skips_itself_midway_still_closes_its_record(ledger):
    """`LookupError` is how a step reports a prerequisite only it could see — a missing
    feature script, an unconfigured path. The tokens spent getting that far belong to that
    tab, and closing the record says so."""
    def gives_up(_ctx):
        raise LookupError("no feature script, or the stack is down")

    result = rs.run_step("video", "behaviour", "v", None, gives_up, _ctx())
    assert result["status"] == rs.SKIPPED
    assert [v for v, _ in ledger.calls] == ["start", "end"]


def test_each_step_gets_its_own_handle_file():
    """Two steps sharing one handle overwrite each other's index, so the second `end` closes
    the first step's record and the second never closes at all."""
    names = [name for name, _t, _l, _p, _f in rs.STEPS]
    assert len(names) == len(set(names)), f"duplicate step names: {names}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
