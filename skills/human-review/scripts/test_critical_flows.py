#!/usr/bin/env python3
"""☢️ — an entry point marked `@critical`, and a branch that changes a method it runs.

Victor, 21 Nov 2026: mark an entry point in the backend code; its whole call graph is then
critical, and a PR that changes any method on it gets ☢️ in the grade pill, top right.
The chain under test: the marker read off the source (`endpoint-complexity.py`), the
spans each flow method carries, the diff intersected with them (`critical-flows.py`), the
mark and its hover in the pill (`grade_pill_with_risk`), the row on the Complexity tab
(`endpoint-complexity-delta.py`) — and once end to end over a real git repository.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("endpoint_complexity_crit", "endpoint-complexity.py")
cf = _load("critical_flows", "critical-flows.py")
delta = _load("endpoint_complexity_delta_crit", "endpoint-complexity-delta.py")

CTRL = "app/src/main/java/demo/PayController.java"
SVC = "app/src/main/java/demo/PayService.java"
CONTROLLER = """package demo;

public class PayController {
    private PayService service;

    /**
     * Takes the money.
     * @critical
     */
    @PostMapping("/pay")
    public String pay(String card) {
        return service.charge(card);
    }

    @GetMapping("/ping")
    public String ping() {
        return service.hello();
    }
}
"""
SERVICE = """package demo;

public class PayService {
    public String charge(String card) {
        if (card == null) {
            return "no";
        }
        return "ok";
    }

    public String hello() {
        return "hi";
    }
}
"""


def _entries(files):
    return {e["path"]: e for e in ec.measure(files, engine="regex")}


def test_the_marker_in_the_javadoc_makes_the_entry_point_critical_and_no_other():
    got = _entries({CTRL: CONTROLLER, SVC: SERVICE})
    assert got["/pay"]["critical"] == {"file": CTRL, "line": 8}
    assert "critical" not in got["/ping"]


@pytest.mark.parametrize("header", [
    "    // @critical: the money path\n    @PostMapping(\"/pay\")\n",
    "    @Critical\n    @PostMapping(\"/pay\")\n",
    "    @PostMapping(\"/pay\") @Critical\n",
])
def test_a_comment_or_an_annotation_of_the_teams_own_marks_it_too(header):
    src = CONTROLLER.replace(
        '    /**\n     * Takes the money.\n     * @critical\n     */\n    @PostMapping("/pay")\n', header)
    assert "@critical\n     */" not in src
    assert "critical" in _entries({CTRL: src, SVC: SERVICE})["/pay"]


def test_a_marker_on_the_previous_method_does_not_leak_onto_the_next():
    src = CONTROLLER.replace("     * @critical\n", "").replace(
        "    @GetMapping(\"/ping\")", "    // @critical belongs to nothing below\n    @GetMapping(\"/ping\")")
    got = _entries({CTRL: src, SVC: SERVICE})
    assert "critical" not in got["/pay"]
    assert "critical" in got["/ping"]


def test_every_flow_method_carries_its_span_from_annotations_to_closing_brace():
    flow = {f["display"].split("(")[0]: f for f in _entries({CTRL: CONTROLLER, SVC: SERVICE})["/pay"]["flow"]}
    assert (flow["PayController.pay"]["line"], flow["PayController.pay"]["endLine"]) == (10, 13)
    assert (flow["PayService.charge"]["line"], flow["PayService.charge"]["endLine"]) == (4, 9)
    assert flow["PayService.charge"]["file"] == SVC


def test_hunks_read_the_old_and_new_ranges_of_a_zero_context_diff():
    diff = ("--- a/X.java\n+++ b/X.java\n@@ -5,0 +6,2 @@\n+a\n+b\n@@ -9 +11 @@\n-c\n+d\n"
            "@@ -20,3 +21,0 @@\n-e\n-f\n-g\n--- a/Gone.java\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
    got = cf.hunks(diff)
    assert got["X.java"] == {"old": [(9, 9), (20, 22)], "new": [(6, 7), (11, 11)]}
    assert got["Gone.java"] == {"old": [(1, 2)], "new": []}


def _snap(files):
    return ec.measure(files, engine="regex")


def test_a_line_changed_deep_in_the_flow_touches_the_critical_entry_point():
    snap = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    got = cf.touched(snap, snap, {SVC: {"old": [(6, 6)], "new": [(6, 6)]}})
    assert [t["name"] for t in got["touched"]] == ["POST /pay"]
    via = got["touched"][0]["touched"]
    assert [v["display"].split("(")[0] for v in via] == ["PayService.charge"]
    assert via[0]["added"] == [6] and via[0]["removed"] == [6]


def test_a_change_outside_every_critical_flow_touches_nothing():
    snap = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    got = cf.touched(snap, snap, {SVC: {"old": [], "new": [(12, 12)]}})   # hello(), /ping's
    assert got["touched"] == [] and [c["name"] for c in got["critical"]] == ["POST /pay"]


def test_a_method_deleted_from_the_flow_counts_off_the_base_side():
    before = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    after = _snap({CTRL: CONTROLLER.replace("service.charge(card)", '"free"'), SVC: SERVICE})
    got = cf.touched(before, after, {SVC: {"old": [(4, 9)], "new": []}})
    assert [t["name"] for t in got["touched"]] == ["POST /pay"]


def test_dropping_the_marker_does_not_let_the_branch_off():
    before = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    after = _snap({CTRL: CONTROLLER.replace("     * @critical\n", ""), SVC: SERVICE})
    got = cf.touched(before, after, {SVC: {"old": [(6, 6)], "new": [(6, 6)]}})
    assert [t["name"] for t in got["touched"]] == ["POST /pay"]


def test_a_snapshot_without_spans_proves_nothing():
    snap = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    for e in snap:
        for f in e["flow"]:
            for k in ("line", "endLine", "spans"):
                f.pop(k, None)
    got = cf.touched(snap, snap, {SVC: {"old": [(6, 6)], "new": [(6, 6)]}})
    assert got["touched"] == [] and got["provable"] is False


# ── the page ───────────────────────────────────────────────────────────────────────────

build = _load("build_review_crit", "build-review-html.py")
TOUCHED = {"touched": [{"name": "POST /pay", "handler": "PayController.pay(String)",
                        "touched": [{"display": "PayService.charge(String)"}]}]}


def test_the_grade_pill_leads_with_radioactive_and_says_why_on_hover():
    pill = '<a class="titlescore v-good" href="#x" data-tip="Overall quality: 8/10"><b>8</b></a>'
    out = build.grade_pill_with_risk({"_critical": TOUCHED, "mergeRisk": {"door": "two-way"}}, pill)
    assert out.index(build.CRITICAL_MARK) < out.index("✅")
    assert 'class="titlecritical"' in out and 'data-cxname="POST /pay"' in out
    tip = out.split('data-tip-html="', 1)[1].split('"', 1)[0]
    assert "Touches 1 critical flow" in tip and "POST /pay — via PayService.charge" in tip
    assert "Overall quality: 8/10" in tip


def test_no_touched_flow_leaves_the_pill_as_it_was():
    pill = '<span class="titlescore v-good"><b>8</b></span>'
    assert build.grade_pill_with_risk({"_critical": {"touched": []}}, pill) == pill
    assert build.grade_pill_with_risk({}, pill) == pill


def test_radioactive_alone_when_there_is_no_grade_yet():
    out = build.grade_pill_with_risk({"_critical": TOUCHED}, "")
    assert out.startswith('<span class="titlescore ts-alone"') and build.CRITICAL_MARK in out


def test_the_complexity_row_of_a_critical_entry_point_wears_the_mark():
    snap = _snap({CTRL: CONTROLLER, SVC: SERVICE})
    keyed = {(e["kind"], e["httpMethod"], e["path"]): e for e in snap}
    rows = {r["path"]: r for r in delta.compare(keyed, keyed)}
    html = delta.render_row(rows["/pay"], 10)
    assert "cx-crit" in html and "☢" in html and 'data-cxname="POST /pay"' in html
    assert "☢" not in delta.render_row(rows["/ping"], 10)


# ── end to end, over a real repository ─────────────────────────────────────────────────

def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin",
                        "HOME": str(root)})


def test_end_to_end_a_branch_editing_the_service_gets_radioactive(tmp_path):
    for rel, text in ((CTRL, CONTROLLER), (SVC, SERVICE)):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / SVC).write_text(SERVICE.replace('return "ok";', 'return "ok!";'))
    _git(tmp_path, "commit", "-qam", "change")
    run = lambda *a: subprocess.run([sys.executable, *a], cwd=tmp_path, check=True,
                                    capture_output=True, text=True)
    run(str(HERE / "endpoint-complexity.py"), "--engine", "regex", "--base", "main", "--out", "b.json")
    run(str(HERE / "endpoint-complexity.py"), "--engine", "regex", "--out", "a.json")
    run(str(HERE / "critical-flows.py"), "b.json", "a.json", "--base", "main", "--out", "c.json")
    got = json.loads((tmp_path / "c.json").read_text())
    assert [t["name"] for t in got["touched"]] == ["POST /pay"]
    assert got["touched"][0]["touched"][0]["added"] == [8]
