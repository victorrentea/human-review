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
# Tabs no step produces. Empty, and that is the point: it is the exemption list, so a tab
# arriving without a step has to be named here deliberately rather than quietly satisfying
# a test that had stopped looking. Two entries have left it. `requirements` left when its
# step arrived (`tests`, the manifest behind the Tests tab). `overview` left when the tab
# did: the summary and the verdict open the first tab now instead of owning one, so there
# is no longer a pill in the strip with no step behind it.
UNFED_BY_DESIGN: set[str] = set()


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
        "review", "behaviour", "sequence", "c2", "requirements", "data",
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


def _dsaudit_ctx(new="http://localhost:4300", old="http://localhost:4301"):
    return rs.Ctx(base="origin/main", dry=False, cfg={"steps": {"dsaudit": {
        "base-new": new, "base-old": old, "label-old": "main",
        "source": ["src"], "screens": {"Book a visit": "pets/11/visits/add"}}}})


def _dead_port() -> int:
    """A port nothing listens on: bound once by the kernel and released, so a connect to
    it is refused rather than left hanging until the probe's timeout."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dsaudit_with_no_app_up_is_skipped_by_name_and_never_stamped(ledger, monkeypatch):
    """The audit compares two running builds, so an app that is not up is its missing
    binary. Before this, nothing probed: Playwright hit ERR_CONNECTION_REFUSED three calls
    deep, the step came back `failed` with the 400-character command line as its reason,
    and the reader had to work out for themselves that two instances were required."""
    monkeypatch.setattr(rs, "answers", lambda url, timeout=3.0: False)
    spec = next(p for n, _t, _l, p, _f in rs.STEPS if n == "dsaudit")
    result = rs.run_step("dsaudit", "dsaudit", "d", spec, lambda c: None, _dsaudit_ctx())
    assert result["status"] == rs.SKIPPED
    assert "http://localhost:4300 (this branch)" in result["reason"]
    assert "http://localhost:4301 (main)" in result["reason"]
    assert "--only dsaudit" in result["reason"]
    assert ledger.calls == [], "a skipped step stamped the ledger anyway"


def test_dsaudit_names_only_the_side_that_is_down(monkeypatch):
    monkeypatch.setattr(rs, "answers", lambda url, timeout=3.0: url.endswith("4300"))
    reason = rs._dsaudit_prereq(_dsaudit_ctx())
    assert "4301 (main)" in reason and "4300" not in reason


def test_dsaudit_runs_when_both_answer(monkeypatch):
    monkeypatch.setattr(rs, "answers", lambda url, timeout=3.0: True)
    assert rs._dsaudit_prereq(_dsaudit_ctx()) is True


def test_the_probe_counts_any_http_answer_as_up_and_a_refused_connect_as_down():
    """A dev server answers every path, usually with its index; a 404 is still a build
    that is up. Only nobody-listening is down."""
    import http.server, threading

    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        def log_message(self, *_a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert rs.answers(f"http://127.0.0.1:{srv.server_port}/anything") is True
    finally:
        srv.shutdown()
    assert rs.answers(f"http://127.0.0.1:{_dead_port()}/") is False


def test_ds_audit_run_by_hand_refuses_in_one_line_not_a_traceback(tmp_path):
    """The runner gates first, but the script is also run by hand — and the run that
    exposed this ended in `Page.goto: net::ERR_CONNECTION_REFUSED` under a Python
    traceback, after a browser had been launched for nothing."""
    import subprocess, sys
    dead, dead2 = (f"http://127.0.0.1:{_dead_port()}" for _ in range(2))
    r = subprocess.run([sys.executable, str(HERE / "ds-audit.py"),
                        "--base-new", dead, "--base-old", dead2,
                        "--screen", "Book a visit=pets/11/visits/add",
                        "--label-new", "test-pr", "--label-old", "main",
                        "--assets", str(tmp_path), "-o", str(tmp_path / "o.html"),
                        "--json", str(tmp_path / "o.json")],
                       text=True, capture_output=True, cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert f"{dead} (new: test-pr)" in r.stderr and "(old: main)" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1, r.stderr


def test_each_step_gets_its_own_handle_file():
    """Two steps sharing one handle overwrite each other's index, so the second `end` closes
    the first step's record and the second never closes at all."""
    names = [name for name, _t, _l, _p, _f in rs.STEPS]
    assert len(names) == len(set(names)), f"duplicate step names: {names}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
