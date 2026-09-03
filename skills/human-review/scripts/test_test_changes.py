#!/usr/bin/env python3
"""What `test-changes.py` promises: a test case's fate, read off the diff.

The classification is the whole point of the script — a reviewer reads a new test and a
tweaked one differently — so the cases below pin each of the four states, including the
two that are easy to get wrong: a *new test inside an existing file* (which `git diff
--name-status` calls `M`, and which must not be reported as "modified"), and a deleted
test, which has no line of its own left and has to borrow the place its removal landed.

Run with:  python3 -m pytest test_test_changes.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("test_changes", HERE / "test-changes.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)


# --------------------------------------------------------------------------- #
# which files are tests at all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", [
    "petclinic-backend/src/test/java/victor/VisitTest.java",
    "petclinic-test/src/add-visit.spec.ts",
    "petclinic-test/features/add-visit.feature",
    "docs/scripts/db/test_db_schema_to_puml.py",
    "internal/store/store_test.go",
])
def test_a_test_file_is_recognised_by_where_it_sits_or_what_it_is_called(rel):
    assert tc.is_test_file(rel)


@pytest.mark.parametrize("rel", [
    "petclinic-backend/src/main/java/victor/VisitRestController.java",
    "petclinic-frontend/src/app/visits/vet-name.pipe.ts",
    "README.md",
])
def test_production_code_is_not_swept_in(rel):
    assert not tc.is_test_file(rel)


# --------------------------------------------------------------------------- #
# finding the test cases
# --------------------------------------------------------------------------- #
JAVA = """package victor;

class VisitTest {
  @Autowired VisitRepository repo;

  private void flushAndClear() {
    em.flush();
  }

  @Test
  void create_withVet() {
    assertThat(1).isEqualTo(1);
  }

  @ParameterizedTest
  @ValueSource(ints = {1, 2})
  void update_changesTheAttendingVet(int id) {
  }
}
"""


def test_a_java_method_is_a_test_because_of_its_annotation_not_its_return_type():
    """`flushAndClear` is `void` too. Counting it would put a helper in a requirement's
    coverage list, which is the one thing this list must not do."""
    cases = tc.test_cases("VisitTest.java", JAVA)
    assert set(cases) == {"create_withVet", "update_changesTheAttendingVet"}
    assert cases["create_withVet"] == 11


def test_an_annotation_between_the_test_and_its_method_does_not_break_the_link():
    assert "update_changesTheAttendingVet" in tc.test_cases("VisitTest.java", JAVA)


def test_playwright_and_jest_cases_come_back_by_their_written_title():
    src = ("test('Add a visit attended by a vet', async ({ page }) => {\n"
           "});\n"
           "it.each([1])('renders the attending vet', () => {});\n")
    cases = tc.test_cases("add-visit.spec.ts", src)
    assert cases == {"Add a visit attended by a vet": 1, "renders the attending vet": 3}


def test_python_go_and_gherkin_each_have_a_shape():
    assert tc.test_cases("test_x.py", "def helper():\n    pass\ndef test_one():\n    pass\n") \
        == {"test_one": 3}
    assert tc.test_cases("x_test.go", "func helper() {}\nfunc TestOne(t *testing.T) {}\n") \
        == {"TestOne": 2}
    assert tc.test_cases("a.feature", "Feature: x\n  Scenario: A visit remembers the vet\n") \
        == {"A visit remembers the vet": 2}


# --------------------------------------------------------------------------- #
# reading the diff
# --------------------------------------------------------------------------- #
def test_hunk_lines_reports_added_lines_and_where_removals_landed():
    diff = (
        "--- a/x.java\n"
        "+++ b/x.java\n"
        "@@ -10,0 +11,2 @@\n"
        "+  one\n"
        "+  two\n"
        "@@ -30,2 +32,0 @@\n"
        "-  gone one\n"
        "-  gone two\n"
    )
    added, removed = tc.hunk_lines(diff)
    assert added == {11, 12}
    # Both removals sat where line 32 of the new file now is — the place a reader opens
    # to see the gap.
    assert removed == {30: 32, 31: 32}


def test_the_file_header_is_not_mistaken_for_an_added_line():
    added, removed = tc.hunk_lines("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert added == {1} and removed == {1: 1}


# --------------------------------------------------------------------------- #
# the four states
# --------------------------------------------------------------------------- #
BEFORE = """class VisitTest {
  @Test
  void update_ok() {
    old();
  }

  @Test
  void delete_ok() {
  }

  @Test
  void obsolete() {
  }
}
"""

AFTER = """class VisitTest {
  @Test
  void update_ok() {
    fresh();
  }

  @Test
  void delete_ok() {
  }

  @Test
  void create_withVet() {
  }
}
"""


def _rows(status="M"):
    added, removed = tc.hunk_lines(
        "--- a/VisitTest.java\n+++ b/VisitTest.java\n"
        "@@ -4 +4 @@\n-    old();\n+    fresh();\n"
        "@@ -12,2 +12,2 @@\n-  void obsolete() {\n-  }\n+  void create_withVet() {\n+  }\n"
    )
    return {r["name"]: r for r in
            tc.classify_file("VisitTest.java", status, BEFORE, AFTER, added, removed)}


def test_a_new_test_inside_a_modified_file_is_added_not_modified():
    """The whole reason this works per test case rather than per file: `--name-status`
    calls the file `M`, and reporting that would bury the row the reviewer came for."""
    assert _rows()["create_withVet"]["status"] == "added"
    assert _rows()["create_withVet"]["line"] == 12


def test_a_test_whose_body_the_diff_touched_is_modified():
    assert _rows()["update_ok"]["status"] == "modified"


def test_a_test_the_diff_never_reached_is_unchanged():
    assert _rows()["delete_ok"]["status"] == "unchanged"


def test_a_deleted_test_keeps_a_line_to_open_in_the_surviving_file():
    row = _rows()["obsolete"]
    assert row["status"] == "deleted"
    assert row["line"] == 12 and not row.get("gone")


def test_a_test_in_a_file_that_was_deleted_outright_says_there_is_nothing_to_open():
    rows = {r["name"]: r for r in
            tc.classify_file("VisitTest.java", "D", BEFORE, None, set(), {})}
    assert rows["update_ok"] == {"name": "update_ok", "path": "VisitTest.java",
                                 "status": "deleted", "line": None, "gone": True}


def test_every_case_of_an_added_file_is_added():
    rows = tc.classify_file("New.java", "A", None, AFTER, {1, 2, 3}, {})
    assert {r["status"] for r in rows} == {"added"}


# --------------------------------------------------------------------------- #
# end to end, against a real repository
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_it_reads_a_real_branch(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "test").mkdir(parents=True)
    f = repo / "src" / "test" / "VisitTest.java"
    _git_init = ["git", "init", "-q", "-b", "main", str(repo)]
    subprocess.run(_git_init, check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    f.write_text(BEFORE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    f.write_text(AFTER)
    _git(repo, "commit", "-qam", "change")

    rows = {r["name"]: r["status"] for r in tc.collect(repo, base, [])}
    assert rows == {"update_ok": "modified", "delete_ok": "unchanged",
                    "create_withVet": "added", "obsolete": "deleted"}


def test_the_cli_writes_the_manifest_the_page_reads(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "tests" / "a.spec.ts").write_text("it('one', () => {});\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "tests" / "a.spec.ts").write_text("it('one', () => {});\nit('two', () => {});\n")

    out = tmp_path / "assets" / "test-changes.json"
    monkeypatch.chdir(repo)
    assert tc.main(["--base", "HEAD", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["base"] == "HEAD"
    assert {t["name"]: t["status"] for t in doc["tests"]} == {"one": "unchanged", "two": "added"}
