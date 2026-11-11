#!/usr/bin/env python3
"""The Code City's coverage colours, converted from what the test step already measured.

`city-coverage.py` turns `assets/test-coverage.json` (testcov.py: per-test hits, per-file
executable lines) into the JSON code-city's `generate.sh --coverage` reads. These hold it
to the three things the colours promise: `line` is every suite merged, `acceptance` is
exactly what the Tests tab calls UI or API, and a file nobody could measure is "not
measured" — absent — never 0%.

Run with:  python3 -m pytest test_city_coverage.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("city_coverage", "city-coverage.py")
tc = _load("testcov_for_city", "testcov.py")

OWNER = "be/src/main/java/a/Owner.java"
PET = "be/src/main/java/a/Pet.java"
VET = "be/src/main/java/a/Vet.java"


def _root(tmp_path: Path) -> Path:
    """Two JVM test classes: one through the HTTP layer (API), one not (unit)."""
    t = tmp_path / "be/src/test/java/a"
    t.mkdir(parents=True)
    (t / "OwnerApiTest.java").write_text("event = {\"httpMethod\": \"GET\"}\n")
    (t / "OwnerTest.java").write_text("class OwnerTest { }\n")
    return tmp_path


def _doc(**over) -> dict:
    doc = {
        "head": "abc123",
        "executableAll": {OWNER: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], PET: [1, 2, 3, 4],
                          VET: [1, 2]},
        "tests": [
            {"suite": "Backend JUnit", "source": "jacoco",
             "file": "be/src/test/java/a/OwnerTest.java",
             "hits": {OWNER: [1, 2, 3, 4, 5, 6]}},
            {"suite": "Backend JUnit", "source": "jacoco",
             "file": "be/src/test/java/a/OwnerApiTest.java",
             "hits": {OWNER: [1, 2, 3]}},
            {"suite": "E2E Playwright", "source": "jacoco+v8", "file": "e2e/owner.spec.ts",
             # line 99 is not executable: a hit there must not inflate the count
             "hits": {OWNER: [3, 4, 99]}},
            {"suite": "Frontend Karma", "source": "karma", "file": "fe/x.spec.ts",
             "hits": {"fe/src/app/x.ts": [1]}},
            {"suite": "Backend JUnit", "source": "jacoco", "file": None,    # unplaced
             "hits": {OWNER: [10]}},
        ],
    }
    doc.update(over)
    return doc


def test_line_is_every_suite_merged_and_acceptance_only_what_the_tests_tab_calls_ui_or_api(
        tmp_path):
    out = cc.convert(_doc(), _root(tmp_path))
    owner = out["files"][OWNER]
    # unit 1-6 and 10, API 1-3, UI 3-4 (99 is not a line any probe can see)
    assert owner["line"] == {"covered": 7, "total": 10}
    # API 1-3 and UI 3-4: the plain JUnit test is a unit test, and does not count
    assert owner["acceptance"] == {"covered": 4, "total": 10}
    assert out["commit"] == "abc123" and out["acceptance"] is True


def test_a_measured_file_no_test_ran_is_zero_and_an_unmeasured_one_is_absent(tmp_path):
    out = cc.convert(_doc(), _root(tmp_path))
    assert out["files"][PET]["line"] == {"covered": 0, "total": 4}        # 0%, measured
    assert out["files"][PET]["acceptance"] == {"covered": 0, "total": 4}
    assert "be/src/main/java/a/Generated.java" not in out["files"]       # not measured
    assert "fe/src/app/x.ts" not in out["files"], \
        "hits in a file with no executable map are not a percentage of anything"


def test_without_an_end_to_end_test_acceptance_is_not_measured_rather_than_zero(tmp_path):
    doc = _doc()
    doc["tests"] = [t for t in doc["tests"] if t["source"] == "karma"
                    or (t["file"] or "").endswith("/OwnerTest.java")]
    out = cc.convert(doc, _root(tmp_path))
    assert out["acceptance"] is False
    assert all("acceptance" not in e for e in out["files"].values())
    assert out["files"][OWNER]["line"]["covered"] == 6


def test_a_file_from_before_the_whole_project_map_converts_to_nothing(tmp_path):
    doc = _doc()
    del doc["executableAll"]
    assert cc.convert(doc, _root(tmp_path)) is None
    src = tmp_path / "test-coverage.json"
    src.write_text(json.dumps(doc))
    dest = tmp_path / "out.json"
    assert cc.main(["--in", str(src), "--out", str(dest), "--root", str(tmp_path)]) == 3
    assert not dest.exists()
    assert cc.main(["--in", str(tmp_path / "absent.json"), "--out", str(dest),
                    "--root", str(tmp_path)]) == 3


def test_main_writes_the_json_code_city_reads(tmp_path):
    root = _root(tmp_path)
    src = tmp_path / "test-coverage.json"
    src.write_text(json.dumps(_doc()))
    dest = tmp_path / "assets/codecity-coverage.json"
    assert cc.main(["--in", str(src), "--out", str(dest), "--root", str(root)]) == 0
    got = json.loads(dest.read_text())
    assert set(got["files"]) == {OWNER, PET, VET}
    assert got["files"][VET]["line"] == {"covered": 0, "total": 2}


# ── testcov.py: the denominator, and re-reading a run without running it ────────────

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_testcov_reuse_reads_the_last_karma_run_and_writes_every_files_executable_lines(
        tmp_path, monkeypatch):
    """`--reuse` runs nothing and touches nothing in `.human-review/coverage`; the output
    carries `executableAll` for every file in `sources`, not just the changed ones."""
    repo = tmp_path / "repo"
    app = repo / "fe/src/app"
    app.mkdir(parents=True)
    (app / "a.ts").write_text("export const a = 1;\n")
    (app / "b.ts").write_text("export const b = 2;\n")
    (app / "a.spec.ts").write_text("it('works', () => {});\n")
    (repo / "fe/src/polyfills.ts").write_text("// outside sources\n")
    (repo / ".gitignore").write_text(".human-review/\n")
    (repo / "human-review.json").write_text(json.dumps({"steps": {"testcov": {
        "sources": ["fe/src/app/**"], "exclude": ["**/*.spec.ts"],
        "karma": [{"label": "Frontend Karma", "cwd": "fe", "command": "exit 1"}]}}}))
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    work = repo / ".human-review/coverage"
    work.mkdir(parents=True)
    karma = work / "karma-0.json"
    karma.write_text(json.dumps({
        "executable": {str(app / "a.ts"): [1], str(app / "b.ts"): [1],
                       str(repo / "fe/src/polyfills.ts"): [1]},
        "tests": [{"id": "a works", "description": "works", "status": "passed",
                   "hits": {str(app / "a.ts"): [1]}}]}))
    before = karma.read_bytes()
    monkeypatch.chdir(repo)
    ran = []
    monkeypatch.setattr(tc, "run_logged", lambda *a, **k: ran.append(a) or 0)

    out = tmp_path / "test-coverage.json"
    assert tc.main(["--base", "HEAD", "--reuse", "--only", "karma", "--out", str(out)]) == 0

    assert ran == [], "a reuse runs no suite"
    assert karma.read_bytes() == before, "...and leaves the last run's dump where it was"
    doc = json.loads(out.read_text())
    assert doc["executableAll"] == {"fe/src/app/a.ts": [1], "fe/src/app/b.ts": [1]}
    assert doc["changed"] == {}, "nothing changed on HEAD..HEAD, yet the map is whole"
    assert [t["hits"] for t in doc["tests"]] == [{"fe/src/app/a.ts": [1]}]
