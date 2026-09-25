#!/usr/bin/env python3
"""Per-test coverage of the change: the `testcov` step, the join, and the measured card.

Three layers, tested where each can be wrong on its own:

  * `testcov.py`'s pure parts — the diff it reads, what it counts as a line worth counting,
    and how it explains a changed line no probe can see (an annotation stands in for its
    method's body, a repository method for its call sites, a migration for nothing);
  * `run-steps.py`'s wiring — the step exists, feeds the Tests tab, runs after the traced
    browser run it harvests, and is cached on what it reads;
  * `hrbuild/tabs/tests.py` — the join with the diff done at build time (aimed vs passing
    through, gaps, proxies reached) and the card that replaces the AI's pairing, which
    shrinks to a chip; and without a measurement, the old card saying it is not one.

Run with:  python3 -m pytest test_testcov.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from hrbuild.tabs import tests as T  # noqa: E402


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load("hr_testcov", "testcov.py")
rs = _load("hr_run_steps_testcov", "run-steps.py")


# --------------------------------------------------------------------------- testcov.py

DIFF = """diff --git a/a/Visit.java b/a/Visit.java
--- a/a/Visit.java
+++ b/a/Visit.java
@@ -10,0 +11,3 @@ class Visit {
+    private Vet vet;
+
+    public Vet getVet() { return vet; }
@@ -20 +23 @@
-old
+new
diff --git a/gone.txt b/gone.txt
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
@@ -30,2 +33,0 @@
"""


def test_the_diff_gives_new_side_lines_and_nothing_for_a_deletion():
    assert tc.parse_diff(DIFF) == {"a/Visit.java": [11, 12, 13, 23]}


def test_ranges_round_trip():
    assert tc.ranges([7, 1, 2, 3, 9, 10]) == "1-3,7,9-10"
    assert tc.unranges("1-3,7,9-10") == [1, 2, 3, 7, 9, 10]
    assert tc.unranges("") == []


def test_blank_comment_brace_and_import_lines_are_not_counted():
    for line in ("", "   ", "// note", " * javadoc", "}", "  });", "import x.Y;", "  </div>"):
        assert tc.TRIVIAL.match(line), line
    for line in ("return vet;", "@PreAuthorize(\"x\")", "<td>{{v.vet}}</td>", "private Vet vet;"):
        assert not tc.TRIVIAL.match(line), line


def test_scope_is_the_sources_minus_the_excludes():
    src, ex = ["app/src/main/**", "web/src/app/**"], ["**/*.spec.ts", "**/generated/**"]
    assert tc.in_scope("app/src/main/java/A.java", src, ex)
    assert tc.in_scope("web/src/app/x/y.ts", src, ex)
    assert not tc.in_scope("web/src/app/x/y.spec.ts", src, ex)
    assert not tc.in_scope("web/src/app/generated/api.ts", src, ex)
    assert not tc.in_scope("app/src/test/java/ATest.java", src, ex)


CONTROLLER = """class VetRestController {
    private final VetRepository repo;

    @PreAuthorize("hasRole('OWNER_ADMIN')")
    public List<Vet> listVets() {
        return repo.findAll();
    }
}""".splitlines()

REPOSITORY = """interface VetRepository {
    @Query(\"\"\"
        select v from Vet v
        \"\"\")
    Optional<Vet> findByIdWithoutSpecialties(int id);
}""".splitlines()


def test_an_annotation_stands_in_for_its_methods_body():
    got = tc.classify("app/VetRestController.java", CONTROLLER, [4], {6}, lambda n: {})
    assert got == [{"file": "app/VetRestController.java", "lines": [4],
                    "reason": "annotation — runs as its method runs",
                    "proxy": {"app/VetRestController.java": [6]}}]


def test_a_method_signature_folds_into_its_body():
    got = tc.classify("app/VetRestController.java", CONTROLLER, [5], {6}, lambda n: {})
    assert got[0]["reason"] == tc.SIGNATURE
    tests = [{"hits": {"app/VetRestController.java": [6]}}, {"hits": {}}]
    ex = {"app/VetRestController.java": {6}}
    tc.fold_signature(got[0], ex, tests)
    assert ex["app/VetRestController.java"] == {5, 6}
    assert tests[0]["hits"]["app/VetRestController.java"] == [5, 6]
    assert tests[1]["hits"] == {}


def test_a_repository_query_stands_in_as_its_call_sites():
    asked = []

    def callers(name):
        asked.append(name)
        return {"app/OwnerRestController.java": [209]}
    got = tc.classify("app/VetRepository.java", REPOSITORY, [2, 3, 5], set(), callers)
    assert asked and set(asked) == {"findByIdWithoutSpecialties"}
    assert len(got) == 1
    assert got[0]["lines"] == [2, 3, 5]
    assert got[0]["proxy"] == {"app/OwnerRestController.java": [209]}
    assert "called" in got[0]["reason"]


def test_a_field_has_no_proxy_and_sql_has_no_probe():
    assert "proxy" not in tc.classify("app/VetRestController.java", CONTROLLER, [2], {6},
                                      lambda n: {})[0]
    sql = tc.classify("db/V4__x.sql", ["alter table visits add vet_id int;"], [1], set(),
                      lambda n: {})
    assert sql == [{"file": "db/V4__x.sql", "lines": [1],
                    "reason": "SQL — runs inside the database, where no probe looks"}]


def test_a_line_only_a_constructor_runs_is_said_to_be_one():
    got = tc.classify("app/VetRestController.java", CONTROLLER, [2], {6}, lambda n: {},
                      init={2})
    assert got[0]["reason"].startswith("runs while the object is built")


def test_the_repo_finds_a_source_by_its_package_suffix(tmp_path, monkeypatch):
    repo = tc.Repo.__new__(tc.Repo)
    repo.root = tmp_path
    repo.files = ["be/src/main/java/v/rest/A.java", "be/src/test/java/v/rest/A.java",
                  "other/src/main/java/v/rest/A.java"]
    repo._by_name = {"A.java": repo.files}
    assert repo.by_suffix("v/rest/A.java", "be") == "be/src/main/java/v/rest/A.java"
    assert repo.by_suffix("/v/rest/B.java") is None


def test_a_spec_is_found_by_its_description():
    src = ["describe('VisitAdd', () => {", "  it('should submit the \\'vet\\'', () => {"]
    assert tc.spec_line(src, "should submit the 'vet'") == 2


# --------------------------------------------------------------------------- run-steps

def test_the_step_feeds_the_tests_tab_after_the_traced_run():
    names = [row[0] for row in rs.STEPS]
    assert "testcov" in names
    row = rs.STEPS[names.index("testcov")]
    assert row[1] == T.LEDGER_TAB
    assert names.index("testcov") > names.index("traces") > names.index("city")


def test_the_step_is_cached_on_what_it_reads():
    spec = rs.STEP_INPUTS["testcov"]
    assert spec["outputs"] == ("assets/test-coverage.json",)
    assert "testcov.py" in spec["tools"] and "testcov" in spec["tools"]
    assert any("run.json" in r for r in spec["reads"])


def test_the_step_is_skipped_and_named_when_not_configured():
    ctx = rs.Ctx("origin/main", {}, dry=True)
    row = next(r for r in rs.STEPS if r[0] == "testcov")
    got = rs.run_step(*row, ctx)
    assert got["status"] == rs.SKIPPED and "testcov" in got["reason"]


def test_the_re_run_tests_press_includes_it():
    assert "testcov" in T.run_tests_steps(HERE)


# --------------------------------------------------------------------------- the join

def _doc():
    """Six tests over one changed file: lines 10-11 are what everyone runs, 12 is what
    only the aimed tests reach, 13 is never run, and a repository query is reached through
    its call site at Caller.java:5."""
    common = {"F.java": [1, 10, 11]}
    tests = [
        {"id": f"s:{i}", "suite": "Backend JUnit", "title": f"t{i}", "file": "FTest.java",
         "line": 10 + i, "status": "passed", "source": "jacoco", "hits": dict(common)}
        for i in range(4)
    ]
    tests[0]["hits"] = {"F.java": [10, 11, 12], "Caller.java": [5]}
    tests.append({"id": "s:quiet", "suite": "Backend JUnit", "title": "quiet", "file": "Q.java",
                  "line": 3, "status": "passed", "source": "jacoco", "hits": {"F.java": [1]}})
    tests.append({"id": "k:1", "suite": "Frontend Karma", "title": "should create",
                  "file": "w/a.spec.ts", "line": 7, "status": "failed", "source": "karma",
                  "hits": {"w/a.ts": [3]}})
    return {"changed": {"F.java": [10, 11, 12, 13, 14], "w/a.ts": [3], "Repo.java": [8]},
            "executable": {"F.java": [1, 10, 11, 12, 13], "w/a.ts": [3]},
            "unmeasurable": [
                {"file": "F.java", "lines": [14], "reason": "declaration — no code of its own"},
                {"file": "Repo.java", "lines": [8], "reason": "no body of its own — runs where it is called",
                 "proxy": {"Caller.java": [5]}}],
            "suites": [{"name": "Backend JUnit", "source": "jacoco", "status": "ran", "tests": 5},
                       {"name": "Frontend Karma", "source": "karma", "status": "ran", "tests": 1},
                       {"name": "E2E Playwright", "source": "jacoco+v8", "status": "stale",
                        "note": "measured on abc, not on HEAD"}],
            "tests": tests}


def test_the_join_counts_measurable_changed_lines_only():
    j = T.coverage_join(_doc())
    assert j["total"] == 5                        # F.java 10-13, a.ts 3
    assert j["gaps"] == {"F.java": [13]}
    assert j["quiet"] == {"Backend JUnit": 1}
    by = {r["id"]: r for r in j["rows"]}
    assert by["s:0"]["n"] == 3 and by["s:0"]["via"] == [1]
    assert j["unmeasurable"][1]["reached"] == 1


def test_a_test_that_runs_only_what_most_of_its_suite_runs_passes_through():
    by = {r["id"]: r for r in T.coverage_join(_doc())["rows"]}
    assert by["s:0"]["aimed"]
    assert not any(by[f"s:{i}"]["aimed"] for i in (1, 2, 3))
    # A suite too small to have a "most" aims every test it has.
    assert by["k:1"]["aimed"]


# --------------------------------------------------------------------------- the card

FRAG = """<div class="reqmap">
  <script type="application/json" class="rm-data">{"tests": {"FTest.java:11": {"title": "t1"},
    "Gone.java:4": {"title": "never measured"}},
    "sentences": {"s1": {"groups": [{"tests": [{"id": "FTest.java:11"}, {"id": "Gone.java:4"}]}]}}}</script>
  <div class="rm-body">
    <div class="rm-text">
      <div class="rm-legend">legend</div>
      <div class="rm-ticket"><div class="rm-tkhead"><span class="rm-who">victor</span></div>
        <p><span class="rm-f" data-s="s1" data-cov="covered">The visit names its vet.</span></p></div>
    </div>
    <div class="rm-side">
      <p class="rm-cats">cats</p>
      <aside class="rm-code">
        <div class="rm-tkhead"><span class="rm-av rm-av-ai">\U0001f916</span><span class="rm-who">Covering tests</span><span class="rm-when">as matched by AI</span></div>
        <div class="rm-list"></div>
      </aside>
    </div>
  </div>
</div>"""


def _with_coverage(tmp_path, doc=None):
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "test-coverage.json").write_text(json.dumps(doc or _doc()))
    (tmp_path / "assets" / "test-changes.json").write_text(json.dumps(
        {"tests": [{"name": "t0", "path": "FTest.java", "status": "added", "line": 10}]}))
    return T.reqmap_layout(FRAG, {"testChanges": "assets/test-changes.json"}, tmp_path,
                           root=tmp_path)


def _data(page):
    return json.loads(re.search(r'class="rm-data">(.*?)</script>', page, re.S).group(1)
                      .replace("<\\/", "</"))


def test_the_card_keeps_its_shape_and_is_retitled(tmp_path):
    page = _with_coverage(tmp_path)
    assert T.COVCARD_WHO in page and "📏" in page
    assert "Covering tests" not in page and "as matched by AI" not in page
    # One list, drawn by the model's own renderer: no second card, no per-row counts.
    assert "rm-aicard" not in page and "cov-card" not in page and "rm-list" in page
    assert "runs 3 of 5" not in page and "changed lines run" not in page


def test_every_test_that_runs_changed_code_joins_the_models_list(tmp_path):
    # t0 is declared on line 10, as coverage reports it, with its @Test on line 9.
    (tmp_path / "FTest.java").write_text(
        "class FTest {\n" + "\n" * 7 + "  @Test\n  void t0() {\n    go();\n  }\n}\n")
    tests = _data(_with_coverage(tmp_path))["tests"]
    # The model's rows stay as it wrote them, the one it could not reach included.
    assert tests["FTest.java:11"] == {"title": "t1"} and "Gone.java:4" in tests
    # Coverage adds what it measured and the model did not name — never the quiet one.
    assert {"FTest.java:10", "FTest.java:12", "FTest.java:13", "w/a.spec.ts:7"} <= set(tests)
    assert "Q.java:3" not in tests
    assert tests["FTest.java:10"]["status"] == "new"          # test-changes says added
    assert tests["FTest.java:12"]["status"] == "unchanged"
    assert tests["w/a.spec.ts:7"]["cat"] == "unit"            # a Karma spec
    part = tests["FTest.java:10"]["parts"][0]                 # its own body, one excerpt
    assert part["from"] == 9 and len(part["html"]) == 4 and "@Test" in part["html"][0]
    assert tests["w/a.spec.ts:7"]["parts"] == []              # no file, no excerpt


def test_gaps_and_unmeasurable_changes_fold_under_the_card(tmp_path):
    page = _with_coverage(tmp_path)
    assert "Changed lines no test runs <b>1</b>" in page
    assert "Not measurable <b>2</b>" in page
    # The card's footer, so the UI/API/unit key stays directly under the card.
    assert page.index("rm-code") < page.index("cov-after") < page.index("rm-cats")


def test_without_a_measurement_the_old_card_says_it_is_not_one(tmp_path):
    page = T.reqmap_layout(FRAG, {}, tmp_path, root=tmp_path)
    assert "cov-card" not in page
    assert "Coverage was not measured" in page
    assert T.CARD_WHO in page                     # the AI's card, as today
