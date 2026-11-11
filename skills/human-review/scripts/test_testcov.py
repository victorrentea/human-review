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








def test_a_field_has_no_proxy_and_sql_has_no_probe():
    assert "proxy" not in tc.classify("app/VetRestController.java", CONTROLLER, [2], {6},
                                      lambda n: {})[0]
    sql = tc.classify("db/V4__x.sql", ["alter table visits add vet_id int;"], [1], set(),
                      lambda n: {})
    assert sql == [{"file": "db/V4__x.sql", "lines": [1],
                    "reason": "SQL — runs inside the database, where no probe looks"}]




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


def test_a_karma_spec_with_an_escaped_quote_is_placed_at_its_file_and_line():
    """Eval run 12: Karma reported "… the newer one's loading" and the source says
    `'… the newer one\\'s loading'`; the plain substring search found no file, the row
    carried no file and no line, and the matrix dropped the spec. Escapes are decoded
    before the source is searched."""
    repo = tc.Repo.__new__(tc.Repo)
    repo.root = Path("/r")
    spec = "fe/src/a.component.spec.ts"
    repo.files = [spec, "fe/src/b.component.spec.ts"]
    srcs = {spec: ["describe('OwnerListComponent', () => {",
                   "  it('does not end the newer one\\'s loading', () => {",
                   '  it("says \\"hi\\"", () => {'],
            "fe/src/b.component.spec.ts": ["describe('B', () => {"]}
    repo.text = lambda f: srcs.get(f, [])
    doc = {"tests": [
        {"id": "OwnerListComponent does not end the newer one's loading",
         "description": "does not end the newer one's loading", "status": "passed",
         "hits": {"/r/fe/src/a.component.ts": [3]}},
        {"id": 'OwnerListComponent says "hi"', "description": 'says "hi"', "hits": {}}]}
    got = tc.karma_tests(doc, repo, "Frontend Karma", "fe", lambda p: p[3:])
    assert [(t["file"], t["line"]) for t in got] == [(spec, 2), (spec, 3)]
    assert got[0]["title"] == "does not end the newer one's loading"
    assert got[0]["hits"] == {"fe/src/a.component.ts": [3]}
    assert tc.spec_line(["  it('a \\\\ b', () => {"], "a \\ b") == 1


# --------------------------------------------------------------------------- run-steps

def test_the_step_feeds_the_tests_tab_after_the_traced_run():
    names = [row[0] for row in rs.STEPS]
    assert "testcov" in names
    row = rs.STEPS[names.index("testcov")]
    assert row[1] == T.LEDGER_TAB
    # ...and before the city, which is coloured by what this step measured.
    assert names.index("city") > names.index("testcov") > names.index("traces")


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


def test_a_template_line_a_component_spec_renders_is_unseen_not_unrun():
    """Eval run 8: owner-list.component.html:23 (the error alert) sat under "Changed lines
    no test runs" although spec:212 renders and asserts it. Karma instruments only the
    TypeScript, so a template line no browser run reached is unseen — proxied by the
    component lines its specs ran — never "no test runs"."""
    doc = _doc()
    doc["changed"]["w/a.html"] = [5, 6]
    doc["executable"]["w/a.html"] = [5, 6]
    doc["tests"].append({"id": "e:1", "suite": "E2E Playwright", "title": "renders",
                         "file": "e/a.spec.ts", "line": 3, "status": "passed",
                         "source": "jacoco+v8", "hits": {"w/a.html": [5]}})
    j = T.coverage_join(doc)
    assert j["gaps"] == {"F.java": [13]}, "line 6 left the gaps, line 5 was run"
    unseen = [u for u in j["unmeasurable"] if u["file"] == "w/a.html"]
    assert unseen == [{"file": "w/a.html", "lines": [6], "reason": T.TEMPLATE_UNSEEN,
                       "proxy": {"w/a.ts": [3]}, "reached": 1}]
    assert {r["id"]: r for r in j["rows"]}["k:1"]["via"] == [2]
    # A template whose component no Karma spec ran stays a gap: nothing rendered it.
    doc["tests"] = [t for t in doc["tests"] if t["source"] != "karma"]
    assert T.coverage_join(doc)["gaps"]["w/a.html"] == [6]


def test_the_karma_hook_charges_function_heads_and_owns_lines_innermost():
    """Eval run 8: owner-list.component.ts:67 (`error => {`) was "never run" — Istanbul
    counts it as a function, not a statement, and only statements were snapshotted. And
    the one statement wrapping both callbacks charged the success body to the failing spec."""
    client = (HERE / "testcov" / "karma" / "client.js").read_text(encoding="utf-8")
    plugin = (HERE / "testcov" / "karma" / "plugin.js").read_text(encoding="utf-8")
    assert "cov[k].f" in client and "fhits: fhits" in client
    assert "fc.fnMap" in plugin and "s.fhits" in plugin
    assert "inner.some(" in plugin


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
    assert T.COVCARD_WHO in page and "rm-play" not in page
    # The model's old title, not the semantic switch's tip, which says the same words.
    assert "Covering tests" not in page and "— as matched by AI" not in page
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


def test_no_list_of_unrun_or_unmeasurable_lines_is_drawn_under_the_card(tmp_path):
    """Victor, 5 Oct 2026: "Changed lines no test runs" and "Not measurable" flagged
    declarations (an NgModule's `declarations: [ … ]`, a local variable) and took the
    column's height — both went for good. The join still computes them; the page does not
    draw them, not even as a zero."""
    page = _with_coverage(tmp_path)
    assert T.coverage_join(_doc())["gaps"], "the fixture does have unrun lines"
    for gone in ("no test runs", "Not measurable", "cov-gaps", "cov-unm", "cov-zero"):
        assert gone not in page, gone
    doc = _doc()
    doc["suites"] = [s for s in doc["suites"] if s["status"] == "ran"]
    assert "cov-after" not in _with_coverage(tmp_path, doc).split("</style>")[-1], \
        "with every suite run nothing is left to say under the card"
    css = (HERE / "hrbuild" / "assets" / "css" / "tests.css").read_text(encoding="utf-8")
    for gone in (".cov-gaps", ".cov-unm", ".cov-zero", ".cov-why", ".cov-gapn"):
        assert gone not in css, gone


def test_a_suite_whose_coverage_is_stale_or_missing_is_named_on_the_tab(tmp_path):
    """Eval run 12: the Playwright coverage was measured on another branch's commit,
    dropped as stale, and the page never said a suite was missing from the column. Each
    such suite is a muted chip under the card — a few words, the reason on hover; a suite
    that ran is not mentioned."""
    page = _with_coverage(tmp_path)
    chip = ('<span class="cov-suite" data-tip="E2E Playwright: measured on abc, not on HEAD">'
            "E2E Playwright coverage: stale</span>")
    assert chip in page
    assert "Backend JUnit coverage" not in page and "Frontend Karma coverage" not in page
    assert page.index("rm-code") < page.index("cov-after") < page.index("cov-suites")
    doc = _doc()
    doc["suites"] = [s for s in doc["suites"] if s["status"] == "ran"]
    doc["suites"].append({"name": "E2E Cucumber", "status": "skipped", "note": "no run.json"})
    assert ('data-tip="E2E Cucumber: no run.json">E2E Cucumber coverage: skipped</span>'
            in _with_coverage(tmp_path, doc))
    doc["suites"] = doc["suites"][:-1]
    assert "cov-suite" not in _with_coverage(tmp_path, doc).split("</style>")[-1]
    css = (HERE / "hrbuild" / "assets" / "css" / "tests.css").read_text(encoding="utf-8")
    assert ".cov-suite {" in css and "cursor:help" in css


def test_the_card_says_pr_only_over_a_pull_request(tmp_path):
    """Eval run 10 said "…modified in this PR" on a branch with no pull request."""
    page = _with_coverage(tmp_path)
    assert f'<span class="rm-who">{T.COVCARD_WHO}</span>' in page
    # The title's tooltip; no shield before it any more (Victor, 20 Oct 2026).
    assert f'<span class="cov-head" data-tip="{T.COVCARD_TIP}">' in page
    assert 'cov-av' not in page.split('<span class="cov-head"')[1].split("</span></span>")[0]
    assert "in this PR" not in page
    (tmp_path / "assets" / "test-coverage.json").write_text(json.dumps(_doc()))
    with_pr = T.reqmap_layout(FRAG, {"testChanges": "assets/test-changes.json",
                                     "pr": {"number": 7}}, tmp_path, root=tmp_path)
    assert f'<span class="rm-who">{T.COVCARD_WHO_PR}</span>' in with_pr


def test_without_a_measurement_the_old_card_says_it_is_not_one(tmp_path):
    page = T.reqmap_layout(FRAG, {}, tmp_path, root=tmp_path)
    assert "cov-card" not in page
    assert "Coverage was not measured" in page
    assert T.CARD_WHO in page                     # the AI's card, as today


def test_a_second_measurement_of_the_same_checkout_waits_for_the_first(tmp_path):
    """Two runs on one checkout share `junit-0/`, the Karma output and `target/`: one of
    them (20 Oct 2026) wrote a coverage file with 386 JUnit tests of 193. The second now
    waits, and says why, until the first lets go."""
    import fcntl
    import subprocess
    import time
    work = tmp_path / "coverage"
    work.mkdir()
    with open(work / ".lock", "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        code = (f"import importlib.util,sys; s=importlib.util.spec_from_file_location('t', "
                f"{str(HERE / 'testcov.py')!r}); m=importlib.util.module_from_spec(s); "
                f"s.loader.exec_module(m); from pathlib import Path; "
                f"m.hold_measuring_lock(Path({str(work)!r})); print('held', flush=True)")
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert proc.poll() is None, "the second run did not wait for the first"
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    out, err = proc.communicate(timeout=20)
    assert "held" in out and "another run is measuring this checkout" in err


def test_the_traces_step_takes_the_same_lock(tmp_path, monkeypatch):
    """The browser suites write their dumps where `testcov` reads them."""
    import fcntl
    monkeypatch.chdir(tmp_path)
    seen = []

    def probe(ctx):
        with open(Path(".human-review/coverage/.lock"), "a") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
    monkeypatch.setattr(rs, "_traces_locked", probe)
    # flock is per open file description: a second open in the same process conflicts.
    rs._traces(rs.Ctx("main", {}, False))
    assert seen == ["held"]


# ---------------------------------------------------------------- pytest (the Ludvig fork)

def _py_project(tmp_path):
    """A tiny service and its tests in a real git repo: a changed function, a changed
    signature, a module-level constant and a docstring."""
    import subprocess
    root = tmp_path / "svc"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_calc.py").write_text(
        "import pytest\nfrom src.calc import add, Box\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n\n"
        "class TestBox:\n    @pytest.mark.parametrize('n', [1, 2])\n"
        "    def test_size(self, n):\n        assert Box().size() == 0\n\n\n"
        "def test_fails():\n    assert add(1, 1) == 3\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    run("tag", "base")
    (root / "src" / "calc.py").write_text(
        "LIMIT = 10\n\n\ndef add(a, b,\n        c=0):\n    \"\"\"Sum.\"\"\"\n    return a + b + c\n\n\n"
        "class Box:\n    def size(self):\n        return 0\n")
    run("commit", "-qam", "change")
    return root


def test_a_pytest_suite_is_measured_per_test_and_joined_to_the_change(tmp_path, monkeypatch):
    root = _py_project(tmp_path)
    (root / "human-review.json").write_text(json.dumps({"steps": {"testcov": {
        "sources": ["src/**"],
        "pytest": [{"label": "pytest", "python": sys.executable, "args": "-q -p no:cacheprovider"}],
    }}}))
    monkeypatch.chdir(root)
    monkeypatch.setattr(tc, "WORK", Path(".human-review/coverage"))
    assert tc.main(["--base", "base", "--out", ".human-review/assets/test-coverage.json"]) == 0
    doc = json.loads((root / ".human-review/assets/test-coverage.json").read_text())
    assert doc["changed"] == {"src/calc.py": [1, 4, 5, 6, 7, 10, 11, 12]}
    rows = {t["id"]: t for t in doc["tests"]}
    assert set(rows) == {"tests/test_calc.py::test_add", "tests/test_calc.py::TestBox::test_size[1]",
                         "tests/test_calc.py::TestBox::test_size[2]", "tests/test_calc.py::test_fails"}
    add = rows["tests/test_calc.py::test_add"]
    assert (add["status"], add["source"], add["line"]) == ("passed", "pytest", 5)
    # The body ran under the test, and the changed header — signature and docstring — is
    # charged to it with it.
    assert add["hits"]["src/calc.py"] == [4, 5, 6, 7]
    assert rows["tests/test_calc.py::test_fails"]["status"] == "failed"
    assert rows["tests/test_calc.py::TestBox::test_size[1]"]["hits"]["src/calc.py"] == [11, 12]
    why = {g["reason"]: g["lines"] for g in doc["unmeasurable"]}
    assert why == {tc.IMPORT_TIME: [1, 10]}
    [suite] = doc["suites"]
    assert (suite["source"], suite["status"], suite["tests"]) == ("pytest", "ran", 4)
    assert "1 failed" in suite["note"]


def test_a_python_header_line_runs_as_its_body_runs():
    src = ["@cache", "def f(a,", "      b):", '    """doc."""', "    return a"]
    assert tc.python_owners(src) == {1: 5, 2: 5, 3: 5, 4: 5}


# ---------------------------------------------------------------- vitest (the Ludvig fork)

def test_a_vitest_run_s_per_test_dumps_become_rows_joined_through_the_source_maps(
        tmp_path, monkeypatch):
    """What `vitest-percov.mjs` leaves (one JSON per test) and what `v8-join.js` makes of it
    (lines per test, through the inline source maps) become the Tests tab's rows."""
    import subprocess as sp
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    out = tmp_path / "work" / "vitest-0"
    out.mkdir(parents=True)
    (out / "1-1.json").write_text(json.dumps({
        "id": "src/a.test.tsx::Card > renders", "title": "renders",
        "file": "src/a.test.tsx", "line": 7, "status": "passed", "v8": []}))
    monkeypatch.setattr(tc, "WORK", tmp_path / "work")
    joined = {"executable": {"frontend/src/a.tsx": [1, 2, 3]},
              "tests": {"1-1.json": {"frontend/src/a.tsx": [2, 3]}}}
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return sp.CompletedProcess(cmd, 0, json.dumps(joined), "")
    monkeypatch.setattr(tc.subprocess, "run", fake_run)
    repo = tc.Repo.__new__(tc.Repo)
    repo.root = root
    tests, suites, executable = tc.vitest_suites(
        [{"label": "Vitest", "cwd": "frontend"}], repo, reuse=True)
    assert seen[0][1].endswith("v8-join.js") and seen[0][3] == "frontend"
    assert tests == [{"id": "Vitest:src/a.test.tsx::Card > renders", "suite": "Vitest",
                      "title": "renders", "file": "frontend/src/a.test.tsx", "line": 7,
                      "status": "passed", "source": "vitest",
                      "hits": {"frontend/src/a.tsx": [2, 3]}}]
    assert executable == {"frontend/src/a.tsx": {1, 2, 3}}
    assert suites[0]["status"] == "ran" and suites[0]["tests"] == 1


def test_the_vitest_wrapper_keeps_the_project_s_setup_files_and_adds_the_hook():
    text = tc.VITEST_WRAPPER.format(config='"/r/fe/vite.config.ts"', root='"/r/fe"',
                                    setup='"/r/fe/node_modules/.cache/hr/vitest-percov.mjs"')
    assert 'import base from "/r/fe/vite.config.ts";' in text
    assert 'root: "/r/fe"' in text and "concurrent: false" in text
    assert "f.replace(/^\\.\\//, '')" in text
