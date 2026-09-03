#!/usr/bin/env python3
"""SKILL.md's own consistency: every tab the worked schema declares must be fed by at
least one `steps-ledger.py start` call in the runbook, and every tab a step names must be
a real tab id — not a typo, not one the schema renamed out from under it.

This is the guard for the gap `steps-ledger.py` exists to close: the mechanism works, but
nothing enforced that a step's wrap in SKILL.md still points at a tab id the schema
recognises. `data,packages` in Step 2, wired against a `"tabs"` array where `"packages"`
got renamed to `"pkgs"`, would parse, run, and silently misattribute cost forever — exactly
the kind of drift this project pins with a test rather than trusting the next edit to catch
it, the same instinct behind `test_tooltips.py` and the ast-grep-rules sync test.

Run with:  python3 -m pytest test_skill_tab_ledger_wiring.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _schema_tab_ids(text: str) -> set[str]:
    """The tab ids declared in the worked `"tabs"` array example under Step 9."""
    m = re.search(r'"tabs":\s*\[(.*?)\n\]', text, re.S)
    assert m, 'could not find the worked `"tabs": [...]` example in SKILL.md'
    return set(re.findall(r'\{"id"\s*:\s*"([a-zA-Z0-9_]+)"', m.group(1)))


def _ledger_start_tabs(text: str) -> set[str]:
    """Every tab id named in a real (non-placeholder) `steps-ledger.py start` call.

    The generic `<tab[,tab2]>` mention in the intro and the bare `start`/`end` mention in
    Step 0's forward pointer both fail to match — the placeholder starts with `<` and the
    bare mention is followed by a backtick, neither of which is a tab-id character."""
    calls = re.findall(r"steps-ledger\.py start ([a-zA-Z][a-zA-Z0-9_,]*)", text)
    tabs: set[str] = set()
    for call in calls:
        tabs.update(call.split(","))
    return tabs


def test_skill_md_is_readable():
    assert SKILL_MD.is_file(), f"expected SKILL.md at {SKILL_MD}"


def test_schema_example_has_the_tabs_this_test_expects():
    # Sanity check on the parser itself, so a regex that silently matched nothing still
    # fails loudly instead of making both real tests vacuously pass.
    schema = _schema_tab_ids(_text())
    assert schema == {
        "autoreview", "video", "behaviour", "data", "packages",
        "owners", "api", "specchanges", "logging", "shape",
    }


def test_every_step_wired_tab_is_a_real_schema_tab():
    text = _text()
    schema = _schema_tab_ids(text)
    wired = _ledger_start_tabs(text)
    unknown = wired - schema
    assert not unknown, (
        f"steps-ledger.py start names tab(s) {sorted(unknown)} that are not in the worked "
        f'"tabs" example ({sorted(schema)}) — a typo, or the schema moved on without the '
        "step wrap"
    )


def test_every_schema_tab_but_overview_has_a_feeding_step():
    text = _text()
    schema = _schema_tab_ids(text) - {"overview"}  # synthesised — deliberately never stamped
    wired = _ledger_start_tabs(text)
    unfed = schema - wired
    assert not unfed, (
        f"tab(s) {sorted(unfed)} appear in the worked \"tabs\" example but no step in "
        "SKILL.md stamps the ledger for them — every real run will show \"not measured\" "
        "on that tab"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
