#!/usr/bin/env python3
"""Mutagent 🧬: the program (mutagent.py), the Tests tab's card and its two presses.

The model and the real test runners are faked: an answer file stands in for claude -p,
and a runner that reads the worktree stands in for Maven. What is real is git — the patches,
git apply --check against HEAD, and the throwaway worktree that must leave the reviewed
checkout exactly as it was.

Run with:  python3 -m pytest test_mutagent.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FENCE = "\x60" * 3


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mg = _load("hr_mutagent_test", "mutagent.py")
srv = _load("hr_serve_review_mutagent", "serve-review.py")
build = _load("hr_build_review_mutagent", "build-review-html.py")

SEARCH = """package shop;

import java.util.List;

public class Search {
    public List<String> names(Repo repo) {
        return repo.query("SELECT name FROM item ORDER BY name");
    }

    public String none() {
        return "none";
    }
}
"""

TEST = """package shop;

import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

class SearchTest {
    @Test
    void returnsInOrder() {
        insert("A", "B", "C");
        assertThat(search.names(repo)).containsExactly("A", "B", "C");
    }

    @Nested
    class Empty {
        @Test
        void saysNone() {
            assertThat(search.none()).isEqualTo("none");
        }
    }

    @Test
    void namesComeBackSorted() {
        assertThat(search.names(repo)).isSorted();
    }
}
"""

TEST_ID = "src/test/java/shop/SearchTest.java:10"
NESTED_ID = "src/test/java/shop/SearchTest.java:17"
FEATURE_ID = "e2e/search.feature:3"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src/main/java/shop").mkdir(parents=True)
    (root / "src/test/java/shop").mkdir(parents=True)
    (root / "e2e").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(root, "config", k, v)
    (root / "pom.xml").write_text("<project/>\n")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "src/main/java/shop/Search.java").write_text(SEARCH)
    (root / "src/test/java/shop/SearchTest.java").write_text(TEST)
    (root / "e2e/search.feature").write_text("Feature: search\n\n  Scenario: Finds things\n"
                                             "    When I search\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feature")
    review = root / ".human-review"
    review.mkdir()
    (review / "content.json").write_text("{}")
    (review / "test-mapping.json").write_text(json.dumps({"schema": "test-mapping/1",
        "sentences": [
            {"id": "s1", "coverage": "covered", "tests": [
                {"id": TEST_ID, "strength": "asserted", "why": "sorted"},
                {"id": FEATURE_ID, "strength": "asserted", "why": "e2e"}]},
            {"id": "s2", "coverage": "partial", "tests": [
                {"id": NESTED_ID, "strength": "asserted", "why": "none"}]},
            {"id": "s3", "coverage": "missing", "tests": []}]}))
    return root


ORDER_BY = 'return repo.query("SELECT name FROM item ORDER BY name");'
ANSWER = {"mutants": [
    {"sentence": "s1", "title": "Drop ORDER BY in search", "file": "src/main/java/shop/Search.java",
     "find": ORDER_BY, "replace": ORDER_BY.replace(" ORDER BY name", ""),
     "rationale": "unsorted rows must fail a sorting test", "expect": [TEST_ID, FEATURE_ID]},
    {"sentence": "s2", "title": "Return null for none", "file": "src/main/java/shop/Search.java",
     "find": 'return "none";', "replace": "return null;", "rationale": "none is promised",
     "expect": [NESTED_ID]},
    {"sentence": "s9", "title": "unknown sentence", "file": "src/main/java/shop/Search.java",
     "find": 'return "none";', "replace": 'return "";', "expect": [NESTED_ID]},
    {"sentence": "s1", "title": "touches a test", "file": "src/test/java/shop/SearchTest.java",
     "find": "A", "replace": "B", "expect": [TEST_ID]},
    {"sentence": "s1", "title": "ambiguous find", "file": "src/main/java/shop/Search.java",
     "find": "return", "replace": "throw", "expect": [TEST_ID]},
    {"sentence": "s1", "title": "no known test", "file": "src/main/java/shop/Search.java",
     "find": ORDER_BY, "replace": "return null;", "expect": ["nope.java:1"]},
]}


def _generate(repo, tmp_path, monkeypatch, answer=ANSWER):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mg, "sentence_texts",
                        lambda review, root: {"s1": "Results come back sorted by name.",
                                              "s2": "An empty search says none."})
    path = tmp_path / "answer.json"
    path.write_text("Here you go:\n" + FENCE + "json\n" + json.dumps(answer) + "\n" + FENCE
                    + "\n")
    return mg.main(["generate", "--dir", ".human-review", "--answer", str(path)])


def test_generate_keeps_the_mutants_that_apply_and_says_why_it_dropped_the_rest(
        repo, tmp_path, monkeypatch, capsys):
    assert _generate(repo, tmp_path, monkeypatch) == 0
    doc = json.loads((repo / ".human-review/mutagent/mutants.json").read_text())
    assert doc["schema"] == "mutagent/1"
    assert [m["id"] for m in doc["mutants"]] == ["01-drop-order-by-in-search",
                                                 "02-return-null-for-none"]
    first = doc["mutants"][0]
    assert first["sentence"] == {"id": "s1", "text": "Results come back sorted by name."}
    assert first["expect"] == [TEST_ID, FEATURE_ID]
    patch = (repo / ".human-review/mutagent" / first["patch"]).read_text()
    assert patch.startswith("diff --git a/src/main/java/shop/Search.java")
    assert "-        " + ORDER_BY in patch and "+        return repo.query" in patch
    assert mg.apply_check(repo, patch) is None
    why = {d["title"]: d["why"] for d in doc["dropped"]}
    assert "attacks no claimed sentence" in why["unknown sentence"]
    assert "not production code" in why["touches a test"]
    assert "occurs 2 times" in why["ambiguous find"]
    assert "expects no test" in why["no known test"]
    # The tests the card needs are labelled once, here, and an e2e one says why it won't run.
    assert doc["tests"][TEST_ID]["label"] == "SearchTest#returnsInOrder"
    assert doc["tests"][NESTED_ID]["label"] == "SearchTest$Empty#saysNone"
    assert doc["tests"][FEATURE_ID]["runnable"] is False
    assert "Cucumber" in doc["tests"][FEATURE_ID]["why"]
    assert "dropped" in capsys.readouterr().err


def test_generate_refuses_without_a_claim_and_without_a_usable_answer(repo, tmp_path,
                                                                      monkeypatch):
    assert _generate(repo, tmp_path, monkeypatch, {"mutants": []}) == 1
    assert not (repo / ".human-review/mutagent/mutants.json").exists()
    (repo / ".human-review/test-mapping.json").write_text('{"sentences": []}')
    assert _generate(repo, tmp_path, monkeypatch) == 2


def test_the_prompt_carries_the_claims_the_test_bodies_and_the_changed_code(
        repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mg, "sentence_texts", lambda review, root: {"s1": "Sorted."})
    assert mg.main(["generate", "--dir", ".human-review", "--prompt-only"]) == 0
    out = capsys.readouterr().out
    asked = json.loads(out.split(FENCE + "json\n")[-1].split("\n" + FENCE)[0])
    assert [s["id"] for s in asked["sentences"]] == ["s1", "s2"]
    assert [f["path"] for f in asked["files"]] == ["src/main/java/shop/Search.java"]
    assert "ORDER BY name" in asked["files"][0]["content"]
    t = asked["sentences"][0]["tests"][0]
    assert t["runnable"] is True and 'insert("A", "B", "C")' in t["body"]


def test_a_patch_without_a_final_newline_still_applies(repo, tmp_path):
    before = (repo / "src/main/java/shop/Search.java").read_text().rstrip("\n")
    patch = mg.make_patch("x.java", before, before.replace('return "none";', "return null;"))
    assert "\\ No newline at end of file" in patch
    (tmp_path / "x.java").write_text(before)
    assert subprocess.run(["git", "apply", "--check", "-"], cwd=tmp_path, input=patch,
                          text=True, capture_output=True).returncode == 0
    assert mg.apply_check(repo, "diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n"
                                "@@ -1 +1 @@\n-a\n+b\n") is not None


def test_java_names_and_the_default_runner(repo):
    text = (repo / "src/test/java/shop/SearchTest.java").read_text()
    assert mg.java_names(text, 10, "x") == ("shop.SearchTest", "returnsInOrder")
    assert mg.java_names(text, 17, "x") == ("shop.SearchTest$Empty", "saysNone")
    c = mg.classify(TEST_ID, repo, {})
    assert (c["runnable"], c["suite"], c["cwd"]) == (True, "maven", ".")
    cmd = mg.suite_command([c, mg.classify(NESTED_ID, repo, {})], c["how"], repo, repo)
    assert cmd.startswith("mvn -o -q test -Dtest='shop.SearchTest#returnsInOrder,"
                          "shop.SearchTest$Empty#saysNone'")


def test_a_configured_suite_wins_and_spells_its_own_command(repo):
    cfg = {"suites": {"e2e": {"files": ["e2e/**/*.feature"], "cwd": "e2e",
                              "item": "{file}:{line}", "join": " ", "quote": False,
                              "run": "npx cucumber-js {tests}", "reports": "reports"}}}
    c = mg.classify(FEATURE_ID, repo, cfg)
    assert c["runnable"] and c["suite"] == "e2e" and c["name"] == "Finds things"
    assert mg.suite_command([c], c["how"], repo, repo / "e2e") == \
        "npx cucumber-js e2e/search.feature:3"


def test_junit_reports_are_matched_to_the_expected_tests(tmp_path):
    (tmp_path / "TEST-shop.SearchTest.xml").write_text(
        '<testsuite><testcase classname="shop.SearchTest" name="returnsInOrder">'
        '<failure message="expected: [A, B, C] but was: [C, A, B]"/></testcase>'
        '<testcase classname="shop.SearchTest$Empty" name="saysNone()"/></testsuite>')
    cases = mg.parse_junit(tmp_path)
    t = {"file": "SearchTest.java", "class": "shop.SearchTest", "name": "returnsInOrder"}
    assert mg.match_detail(t, cases) == ("fail", "expected: [A, B, C] but was: [C, A, B]")
    nested = {"file": "SearchTest.java", "class": "shop.SearchTest$Empty", "name": "saysNone"}
    assert mg.match_case(nested, cases) == "pass"
    assert mg.match_case({**t, "name": "gone"}, cases) == "missing"


RUNNABLE = {"a": {"runnable": True}, "b": {"runnable": True},
            "e2e": {"runnable": False, "why": "a browser spec"}}


@pytest.mark.parametrize("expect, baseline, outcome, applied, want", [
    (["a", "b"], {"a": "pass", "b": "pass"},
     {"ok": True, "results": {"a": "pass", "b": "fail"}}, True, "killed"),
    (["a"], {"a": "pass"}, {"ok": True, "results": {"a": "pass"}}, True, "survived"),
    (["a"], {"a": "pass"}, {"ok": False, "results": {"a": "missing"}, "tail": "BUILD FAILURE"},
     True, "broken"),
    (["a"], {"a": "pass"}, None, False, "broken"),
    (["e2e"], {}, None, True, "not-run"),
    (["a"], {"a": "fail"}, None, True, "not-run"),
])
def test_the_verdicts(expect, baseline, outcome, applied, want):
    v = mg.verdict(expect, RUNNABLE, baseline, outcome, applied)
    assert v["verdict"] == want, v


def test_run_mutates_a_throwaway_worktree_and_never_the_checkout(repo, tmp_path, monkeypatch):
    assert _generate(repo, tmp_path, monkeypatch) == 0
    (repo / "README.md").write_text("uncommitted work\n")
    seen = []

    def runner(wt, tests):
        assert wt != repo and wt.is_dir()
        src = (wt / "src/main/java/shop/Search.java").read_text()
        seen.append(src)
        # The sorting test is strong (it fails without ORDER BY); the "none" test is weak
        # in this story: it stays green with the null mutant.
        results = {t["id"]: ("fail" if t["id"] == TEST_ID and "ORDER BY" not in src
                             else "pass") for t in tests}
        return {"results": results, "messages": {}, "ok": True, "tail": ""}

    out = mg.run_all(repo / ".human-review", repo, {}, runner=runner)
    v = {k: x["verdict"] for k, x in out["mutants"].items()}
    assert v == {"01-drop-order-by-in-search": "killed", "02-return-null-for-none": "survived"}
    assert "ORDER BY" in seen[0] and "ORDER BY" not in seen[1] and "return null;" in seen[2]
    # The checkout is as it was: the uncommitted edit kept, nothing mutated, no worktree left.
    assert (repo / "README.md").read_text() == "uncommitted work\n"
    assert ORDER_BY in (repo / "src/main/java/shop/Search.java").read_text()
    assert _git(repo, "worktree", "list").count("\n") == 1
    saved = json.loads((repo / ".human-review/mutagent/results.json").read_text())
    assert saved["mutants"]["02-return-null-for-none"]["verdict"] == "survived"


# --------------------------------------------------------------------- the page's half

def _review(tmp_path, mutants=True, results=None):
    out = tmp_path / ".human-review"
    (out / "mutagent").mkdir(parents=True)
    (out / "test-mapping.json").write_text("{}")
    if mutants:
        (out / "mutagent/01-drop.patch").write_text(
            "diff --git a/Search.java b/Search.java\n--- a/Search.java\n+++ b/Search.java\n"
            "@@ -1,1 +1,1 @@\n-ORDER BY name\n+\n")
        (out / "mutagent/02-null.patch").write_text(
            "diff --git a/Search.java b/Search.java\n--- a/Search.java\n+++ b/Search.java\n"
            "@@ -1,1 +1,1 @@\n-none\n+null\n")
        (out / "mutagent/mutants.json").write_text(json.dumps({
            "schema": "mutagent/1", "head": "abc", "model": "sonnet", "cost": 0.22,
            "mutants": [
                {"id": "01-drop", "title": "drop ORDER BY", "file": "Search.java",
                 "sentence": {"id": "s1", "text": "Sorted by name."}, "rationale": "r1",
                 "expect": ["T.java:3"], "patch": "01-drop.patch"},
                {"id": "02-null", "title": "return null", "file": "Search.java",
                 "sentence": {"id": "s2", "text": "Says none."}, "rationale": "r2",
                 "expect": ["T.java:9"], "patch": "02-null.patch"}],
            "tests": {"T.java:3": {"label": "T#sorted", "runnable": True},
                      "T.java:9": {"label": "T#none", "runnable": True}}}))
    if results is not None:
        (out / "mutagent/results.json").write_text(json.dumps(results))
    return out


RESULTS = {"mutantsHead": "abc", "mutants": {
    "01-drop": {"verdict": "killed", "tests": {"T.java:3": "fail"},
                "messages": {"T.java:3": "expected: [A, B] but was: [B, A]"}},
    "02-null": {"verdict": "survived", "tests": {"T.java:9": "pass"}}}}


def _marks(out, root):
    blob = build.mutagent_marks(out, root)
    return json.loads(blob.split(">", 1)[1].rsplit("</script>", 1)[0]) if blob else {}


def test_each_test_is_marked_by_what_it_did_against_its_mutant(tmp_path):
    """No card any more: the verdict rides on the test's own row (testchapters.js) — a
    killed mutant marks its test green, a survivor red, with the change before → after."""
    got = _marks(_review(tmp_path, results=RESULTS), tmp_path)
    assert {t: [m["how"] for m in ms] for t, ms in got.items()} == {
        "T.java:3": ["killed"], "T.java:9": ["survived"]}
    killed = got["T.java:3"][0]
    assert killed["title"] == "drop ORDER BY" and killed["why"] == "r1"
    # The mutation itself: before → after, the changed token marked.
    assert '<code class="mg-before" data-tip="At HEAD"><del>ORDER BY name</del>' in killed["change"]
    assert 'Search.java:1</span>' in killed["change"]


def test_no_mutants_or_old_results_mark_nothing(tmp_path):
    assert build.mutagent_marks(_review(tmp_path, mutants=False), tmp_path) == ""
    stale = {**RESULTS, "mutantsHead": "older"}
    assert build.mutagent_marks(_review(tmp_path / "b", results=stale), tmp_path / "b") == ""
    assert build.mutagent_marks(_review(tmp_path / "c"), tmp_path / "c") == ""   # not run yet


def test_the_mutation_is_shown_word_by_word_and_wraps_instead_of_hiding():
    patch = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -84,3 +84,3 @@\n"
             "     ctx();\n-        put(\"description\", List.of(\"v.description\"));\n"
             "+        put(\"description\", List.of(\"v.id\"));\n     ctx();\n")
    out = build.mutation_html(patch, "src/A.java", Path("/nowhere"))
    assert 'A.java:85</span>' in out and 'data-tip="src/A.java:85"' in out
    assert ('<code class="mg-before" data-tip="At HEAD">put(&quot;description&quot;, '
            'List.of(&quot;v.<del>description</del>&quot;));</code>') in out
    assert '<ins>id</ins>' in out and "→" in out
    assert build._word_diff("a b", "a c") == ("a <del>b</del>", "a <ins>c</ins>")


def test_the_two_presses_are_declared_and_drawn_after_the_paid_one(tmp_path):
    build.ACTIONS.clear()
    out = _review(tmp_path, mutants=False)
    info = build.declare_mutagent(tmp_path, out, HERE)
    assert info["run"] is None and set(build.ACTIONS) == {"__mutagent__:requirements"}
    cmd = build.ACTIONS["__mutagent__:requirements"]["command"]
    assert "mutagent.py" in cmd and " generate --dir .human-review && " in cmd
    assert cmd.endswith("--steps none --no-serve")
    assert build.ACTIONS["__mutagent__:requirements"]["reload"]
    _review(tmp_path / "x")                                    # now with mutants.json
    info = build.declare_mutagent(tmp_path / "x", tmp_path / "x/.human-review", HERE)
    assert " run --dir .human-review && " in build.ACTIONS["__mutagent_run__:requirements"][
        "command"]
    buttons = build.mutagent_buttons(info)
    assert 'data-rerun="__mutagent__"' in buttons and 'data-rerun="__mutagent_run__"' in buttons
    assert buttons.count(" hidden ") == 2 and 'data-price="mutagent"' in buttons
    assert "data-confirm=" in buttons and 'data-tip="costs money.' in buttons
    tabre = build.tab_rerun_html("requirements", "Tests",
                                 {"steps": ["tests"], "ai": True, "aiTip": "x",
                                  "after": buttons})
    assert tabre.index('data-rerun="__rerun_ai__"') < tabre.index('data-rerun="__mutagent__"') \
        < tabre.index('data-rerun="__mutagent_run__"') < tabre.rindex("</span>")
    # No mapping, nothing to attack: no press at all.
    (out / "test-mapping.json").unlink()
    assert build.declare_mutagent(tmp_path, out, HERE) is None
    build.ACTIONS.clear()


def test_the_server_routes_both_presses_by_mode_and_never_through_run(tmp_path):
    (tmp_path / srv.ACTIONS_FILE).write_text(json.dumps({"version": 1, "actions": {
        "__rerun__:requirements": {"command": "echo free", "params": {}},
        "__rerun_ai__:requirements": {"command": "echo paid", "params": {}},
        "__mutagent__:requirements": {"command": "echo mutants", "params": {}},
        "__mutagent_run__:requirements": {"command": "echo run", "params": {}}}}))
    srv._manifest.update(mtime=None, actions={})
    srv.RUNS.clear()
    srv.ROOT = tmp_path
    plan = srv.tab_rerun_plan
    assert plan(tmp_path, True, "requirements", "mutagent")[0][-1] == "echo mutants"
    assert plan(tmp_path, False, "requirements", "mutagent")[0][-1] == "echo run"
    assert plan(tmp_path, True, "requirements")[0][-1] == "echo paid"
    for aid in ("__mutagent__:requirements", "__mutagent_run__:requirements"):
        _, problem, status = srv.start_run(aid, {}, tmp_path)
        assert status == 400 and "endpoint of its own" in problem
    assert srv.MUTAGENT_ACTION in srv.PAID_ACTIONS
    assert "mutagent" in srv.PRICED_RUNS
    assert "'/__rerun_ai__', 'mutagent'" in build.SERVER_JS
    assert "'/__rerun__', 'mutagentRun'" in build.SERVER_JS


LEGACY_ID = "src/test/java/shop/SearchTest.java:22"


def _measured(repo):
    """What the Tests tab measured: a pre-existing, unclaimed test running a changed line,
    beside a claimed one and a new one."""
    assets = repo / ".human-review/assets"
    assets.mkdir()
    (assets / "test-coverage.json").write_text(json.dumps({
        "changed": {"src/main/java/shop/Search.java": [7, 11]},
        "tests": [
            {"file": "src/test/java/shop/SearchTest.java", "line": 22, "status": "passed",
             "title": "namesComeBackSorted", "hits": {"src/main/java/shop/Search.java": [7]}},
            {"file": "src/test/java/shop/SearchTest.java", "line": 8, "status": "passed",
             "title": "returnsInOrder", "hits": {"src/main/java/shop/Search.java": [7]}},
            {"file": "src/test/java/shop/SearchTest.java", "line": 16, "status": "passed",
             "title": "saysNone", "hits": {"README.md": [1]}}]}))
    (assets / "test-changes.json").write_text(json.dumps({"tests": [
        {"name": "namesComeBackSorted", "path": "src/test/java/shop/SearchTest.java",
         "status": "unchanged", "line": 22},
        {"name": "returnsInOrder", "path": "src/test/java/shop/SearchTest.java",
         "status": "added", "line": 8}]}))


def test_an_unclaimed_pre_existing_test_is_attacked_on_what_its_name_claims(
        repo, tmp_path, monkeypatch, capsys):
    _measured(repo)
    picked = mg.load_unclaimed(repo / ".human-review", repo, set(), {})
    assert [p["id"] for p in picked] == ["t:" + LEGACY_ID]        # new and off-change ones out
    assert picked[0]["text"] == "names come back sorted" and picked[0]["kind"] == "test"
    assert picked[0]["changed"] == {"src/main/java/shop/Search.java": [7]}
    monkeypatch.chdir(repo)
    monkeypatch.setattr(mg, "sentence_texts", lambda review, root: {})
    assert mg.main(["generate", "--dir", ".human-review", "--prompt-only"]) == 0
    asked = json.loads(capsys.readouterr().out.split(FENCE + "json\n")[-1].split("\n" + FENCE)[0])
    legacy = [s for s in asked["sentences"] if s.get("kind") == "test"]
    assert [s["id"] for s in legacy] == ["t:" + LEGACY_ID]
    assert "isSorted" in legacy[0]["tests"][0]["body"]
    answer = {"mutants": [{"sentence": "t:" + LEGACY_ID, "title": "drop the sort",
                           "file": "src/main/java/shop/Search.java", "find": ORDER_BY,
                           "replace": ORDER_BY.replace(" ORDER BY name", ""),
                           "rationale": "unsorted", "expect": [LEGACY_ID]}]}
    assert _generate(repo, tmp_path, monkeypatch, answer) == 0
    doc = json.loads((repo / ".human-review/mutagent/mutants.json").read_text())
    assert doc["mutants"][0]["sentence"] == {"id": "t:" + LEGACY_ID, "kind": "test",
                                             "text": "names come back sorted"}
    assert doc["tests"][LEGACY_ID]["label"] == "SearchTest#namesComeBackSorted"
    assert build.mutagent_marks(repo / ".human-review", repo) == ""   # generated, not run


def test_a_test_row_carries_the_mutations_it_met(tmp_path):
    """The ledger's row opens on its mutants (testchapters.js): the title and the change,
    before → after; its 🧬 is red with one survivor, green when it caught them all."""
    got = _marks(_review(tmp_path, results=RESULTS), tmp_path)
    how, title, change = (got["T.java:9"][0][k] for k in ("how", "title", "change"))
    assert (how, title) == ("survived", "return null")
    assert '<div class="mg-change">' in change and "<ins>null</ins>" in change
    js = (Path(build.__file__).parent / "hrbuild/assets/testchapters.js").read_text()
    assert "rm-chmg" in js and "mutDetails" in js and "mutColour" in js
    css = (Path(build.__file__).parent / "hrbuild/assets/css/tests.css").read_text()
    assert "rm-mg-pulse" in css and "prefers-reduced-motion" in css
    assert ".rm-mg[data-mg=green]" in css


def test_a_test_that_stayed_green_while_another_caught_the_mutant_is_not_red(tmp_path):
    out = _review(tmp_path, results={"mutantsHead": "abc", "mutants": {
        "01-drop": {"verdict": "killed", "tests": {"T.java:3": "pass", "T.java:9": "fail"}}}})
    got = _marks(out, tmp_path)
    assert got["T.java:3"][0]["how"] == "missed" and got["T.java:9"][0]["how"] == "killed"


def test_the_card_is_gone_from_the_tests_tab():
    src = (Path(build.__file__).parent / "hrbuild/tabs/tests.py").read_text()
    assert "mutagent_card" not in src and 'class="mutagent"' not in src

