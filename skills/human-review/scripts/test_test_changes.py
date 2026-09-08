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


# --------------------------------------------------------------------------- #
# the tests that are still written and no longer run
# --------------------------------------------------------------------------- #
# Deletion is the loud way to lose a test and the only one a diff makes obvious. These
# pin the two quiet ways, in every language the script claims to read, because a chip
# that says "2 lost" while three more sit under an @Disabled is worse than no chip.
SILENCED = {
    "VisitTest.java": ("""class VisitTest {
  @Test
  void runs() {}

  @Disabled("flaky on CI")
  @Test
  void off() {}

  @Nested
  @Disabled
  class Inner {
    @Test
    void nested_off() {}
  }

  @Test
  void still_runs() {}
}
""", {"off", "nested_off"}, {"runs", "still_runs"}),
    "visits.spec.ts": ("""describe('visits', () => {
  it('adds one', () => {});
  it.skip('is skipped', () => {});
  xit('is x-skipped', () => {});
});
describe.skip('vets', () => {
  it('sits in a skipped suite', () => {});
});
""", {"is skipped", "is x-skipped", "sits in a skipped suite"}, {"adds one"}),
    "test_visits.py": ("""@pytest.mark.skip(reason="flaky")
def test_off(): pass

def test_on(): pass

@pytest.mark.skipif(SLOW, reason="slow")
class TestGroup:
    def test_in_a_skipped_class(self): pass
""", {"test_off", "test_in_a_skipped_class"}, {"test_on"}),
    "store_test.go": ("""func TestOff(t *testing.T) {
\tt.Skip("needs docker")
}
func TestOn(t *testing.T) {
\tok()
}
""", {"TestOff"}, {"TestOn"}),
    "add-visit.feature": ("""Feature: visits

  @wip
  Scenario: parked
    Given a

  Scenario: live
    Given b
""", {"parked"}, {"live"}),
}


@pytest.mark.parametrize("rel", sorted(SILENCED))
def test_a_test_that_is_still_written_but_switched_off_says_so(rel):
    text, off, on = SILENCED[rel]
    scanned = tc.scan_cases(rel, text)
    assert {n for n, (_, why) in scanned.items() if why} == off
    assert {n for n, (_, why) in scanned.items() if not why} == on


def test_a_tag_on_the_feature_reaches_every_scenario_under_it():
    """Feature-level is the case worth pinning: one `@wip` at the top of the file turns
    off scenarios that carry no marker of their own anywhere near them."""
    scanned = tc.scan_cases("x.feature", "@wip\nFeature: x\n\n  Scenario: a\n    Given b\n")
    assert scanned["a"][1] == "disabled"


@pytest.mark.parametrize("rel, text, name, line", [
    ("VisitTest.java", "class T {\n//  @Test\n//  void parked() {\n//  }\n}\n", "parked", 3),
    ("visits.spec.ts", "describe('x', () => {\n// it('parked', () => {});\n});\n", "parked", 2),
    ("test_visits.py", "# def test_parked():\n#     pass\n", "test_parked", 1),
])
def test_a_test_that_exists_only_inside_a_comment_is_found(rel, text, name, line):
    """Commenting a test out costs the run exactly as much as deleting it, and costs the
    diff nothing — the body is still there, so it reads as kept. The line is the one it
    occupies on disk, so the row stays clickable straight to the comment."""
    assert tc.commented_cases(rel, text) == {name: line}
    assert name not in tc.scan_cases(rel, text)


def test_live_code_is_not_reported_as_commented_out():
    assert tc.commented_cases("T.java", "class T {\n  @Test\n  void runs() {}\n}\n") == {}


# --------------------------------------------------------------------------- #
# what the diff did to the run, not just to the file
# --------------------------------------------------------------------------- #
RUNNING = """class VisitTest {
  @Test
  void kept() {}

  @Test
  void about_to_be_disabled() {}

  @Test
  void about_to_be_commented() {}

  @Disabled
  @Test
  void about_to_come_back() {}
}
"""

SILENT = """class VisitTest {
  @Test
  void kept() {}

  @Disabled
  @Test
  void about_to_be_disabled() {}

//  @Test
//  void about_to_be_commented() {}

  @Test
  void about_to_come_back() {}
}
"""


def _silenced_rows():
    return {r["name"]: r for r in
            tc.classify_file("VisitTest.java", "M", RUNNING, SILENT, set(), {})}


def test_a_test_left_in_place_under_a_disabled_is_flagged_where_it_stands():
    row = _silenced_rows()["about_to_be_disabled"]
    assert row["silenced"] == "disabled" and not row.get("wasSilenced")
    assert row["status"] != "deleted", "it is still declared; it just does not run"


def test_a_commented_out_test_is_a_deletion_that_can_still_be_opened():
    row = _silenced_rows()["about_to_be_commented"]
    assert row["status"] == "deleted" and row["silenced"] == "commented"
    assert row["line"] == 10, "the comment itself — the thing the reviewer has to judge"


def test_a_test_switched_back_on_says_where_it_came_from():
    row = _silenced_rows()["about_to_come_back"]
    assert row["wasSilenced"] == "disabled" and not row.get("silenced")


def test_the_totals_reconcile_with_the_rows_behind_them():
    """The chip states `+gained / −lost`, and a reader is entitled to assume those two
    numbers are the difference between the run before and the run after. They are, by
    construction — this is the arithmetic that says so."""
    t = tc.totals(list(_silenced_rows().values()))
    assert t["runningBefore"] == 3 and t["runningAfter"] == 2
    assert t["gained"] == 1 and t["lost"] == 2
    assert t["runningAfter"] - t["runningBefore"] == t["gained"] - t["lost"]
    assert t["disabled"] == 1 and t["commented"] == 1 and t["reenabled"] == 1


def test_deleting_a_test_nobody_was_running_moves_nothing():
    """A `@Disabled` test that this change set finally removes is housekeeping, not a
    loss: the run did not have it before and does not have it now."""
    t = tc.totals(tc.classify_file(
        "T.java", "M", "class T {\n  @Disabled\n  @Test\n  void dead() {}\n}\n",
        "class T {\n}\n", set(), {2: 2, 3: 2, 4: 2}))
    assert t["deleted"] == 1 and t["lost"] == 0
    assert t["runningBefore"] == 0 and t["runningAfter"] == 0
