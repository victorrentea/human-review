#!/usr/bin/env python3
"""Is Sonar green too? — the gate records what the CI run's Sonar steps said, and the grade
panel says it with a second link (Victor, 20 Oct 2026).

The producer half reads a recorded `gh run view --json jobs,event,headBranch` (petclinic's
run 37347327883 on be762955, both Sonar steps green) and a recorded SonarCloud
`project_branches/list`; nothing here touches the network.

Run with:  python3 -m pytest test_sonar_gate.py
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DATA = HERE / "testdata" / "sonar"
RUN = json.loads((DATA / "petclinic-run-37347327883.json").read_text())
BRANCHES = json.loads((DATA / "sonarcloud-branches.json").read_text())
SHA = "be762955d5f477feeca0f7db55e32fa1c2fb8db5"
RUN_URL = "https://github.com/victorrentea/petclinic/actions/runs/37347327883"
DASH = "https://sonarcloud.io/summary/new_code?id=victorrentea_petclinic&branch=test-pr"

sys.path.insert(0, str(HERE))
import preflight  # noqa: E402

_spec = importlib.util.spec_from_file_location("build_review_html",
                                               HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv(preflight.SONAR_API_ENV, "0")
    (tmp_path / "sonar-project.properties").write_text(
        "sonar.projectKey=victorrentea_petclinic\nsonar.organization=victorrentea\n"
        "sonar.host.url=https://sonarcloud.io\n")
    return tmp_path


def _with(step: str, conclusion: str) -> dict:
    run = copy.deepcopy(RUN)
    for st in run["jobs"][0]["steps"]:
        if st["name"] == step:
            st["conclusion"] = conclusion
    return run


# ── the producer ──────────────────────────────────────────────────────────────────────

def test_a_green_push_run_records_sonar_green_and_the_branch_dashboard(project):
    s = preflight.sonar_from_run(RUN, SHA, project)
    assert s == {"verdict": "success", "gate": "success", "scanStep": "SonarCloud Scan",
                 "gateStep": "SonarCloud Quality Gate check",
                 "projectKey": "victorrentea_petclinic", "url": DASH}


def test_the_cache_steps_that_say_sonar_are_not_the_scan(project):
    """`Cache SonarCloud packages` and its `Post …` twin analyse nothing."""
    run = _with("Cache SonarCloud packages", "failure")
    for st in run["jobs"][0]["steps"]:
        if st["name"] == "Post Cache SonarCloud packages":
            st["conclusion"] = "failure"
    assert preflight.sonar_from_run(run, SHA, project)["verdict"] == "success"


@pytest.mark.parametrize("step,conclusion,verdict,gate", [
    ("SonarCloud Quality Gate check", "failure", "failure", "failure"),
    ("SonarCloud Scan", "failure", "failure", "success"),
    ("SonarCloud Scan", "skipped", "skipped", "success"),
])
def test_the_steps_conclusions_make_the_verdict(project, step, conclusion, verdict, gate):
    s = preflight.sonar_from_run(_with(step, conclusion), SHA, project)
    assert (s["verdict"], s["gate"]) == (verdict, gate)


def test_a_run_without_sonar_records_nothing(project):
    run = copy.deepcopy(RUN)
    run["jobs"][0]["steps"] = [st for st in run["jobs"][0]["steps"]
                               if "sonar" not in st["name"].lower()]
    assert preflight.sonar_from_run(run, SHA, project) is None


def test_a_pull_request_run_links_the_pull_requests_analysis(project):
    run = dict(RUN, event="pull_request")
    s = preflight.sonar_from_run(run, SHA, project, pr=37)
    assert s["url"] == ("https://sonarcloud.io/summary/new_code?id=victorrentea_petclinic"
                        "&pullRequest=37")


def test_the_key_comes_from_the_pom_or_the_workflow_when_there_is_no_properties_file(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><sonar.projectKey>acme_shop</sonar.projectKey>"
        "</properties></project>")
    assert preflight.sonar_project(tmp_path) == ("acme_shop", "https://sonarcloud.io")
    (tmp_path / "pom.xml").unlink()
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("run: mvn verify sonar:sonar -Dsonar.projectKey=acme_cart\n")
    assert preflight.sonar_project(tmp_path)[0] == "acme_cart"


def _api(monkeypatch, sha: str, status: str):
    doc = copy.deepcopy(BRANCHES)
    row = next(b for b in doc["branches"] if b["name"] == "test-pr")
    row["status"]["qualityGateStatus"], row["commit"]["sha"] = status, sha
    seen = []

    def urlopen(url, timeout):
        seen.append((url, timeout))
        return io.BytesIO(json.dumps(doc).encode())
    monkeypatch.delenv(preflight.SONAR_API_ENV, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return seen


def test_a_red_gate_the_api_holds_for_this_commit_is_a_failure(project, monkeypatch):
    """A gate step with `continue-on-error` leaves a green job over a red gate."""
    seen = _api(monkeypatch, SHA, "ERROR")
    s = preflight.sonar_from_run(RUN, SHA, project)
    assert s["verdict"] == "failure" and s["api"] == {"status": "ERROR", "sha": SHA,
                                                     "stale": False}
    assert seen == [("https://sonarcloud.io/api/project_branches/list"
                     "?project=victorrentea_petclinic", preflight.SONAR_API_TIMEOUT_S)]


def test_an_api_answer_about_another_commit_changes_nothing(project, monkeypatch):
    _api(monkeypatch, "39346ec5" + "0" * 32, "ERROR")
    s = preflight.sonar_from_run(RUN, SHA, project)
    assert s["verdict"] == "success" and s["api"]["stale"] is True


def test_the_api_unreachable_is_silence_not_a_failure(project, monkeypatch):
    def down(url, timeout):
        raise OSError("offline")
    monkeypatch.delenv(preflight.SONAR_API_ENV, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", down)
    s = preflight.sonar_from_run(RUN, SHA, project)
    assert s["verdict"] == "success" and "api" not in s


def _stub_gh(tmp_path: Path, view: dict | None, head_runs: list | None = None,
             head_view: dict | None = None, reviewed: str = "") -> dict:
    """A `gh` whose `run list` describes one green CI run — or, for any commit but
    `reviewed` when `head_runs` is given, those — and whose `run view` answers with `view`
    (`head_view` for a run of `head_runs`), or fails on None; `git push` is a no-op."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    green = [{"databaseId": 37347327883, "status": "completed", "conclusion": "success",
              "workflowName": "CI", "event": "push", "url": RUN_URL}]
    (bindir / "gh").write_text(
        "#!/usr/bin/env python3\nimport sys, json\n"
        f"VIEW = json.loads({json.dumps(json.dumps(view))})\n"
        f"HEAD_VIEW = json.loads({json.dumps(json.dumps(head_view))})\n"
        f"GREEN = json.loads({json.dumps(json.dumps(green))})\n"
        f"HEAD_RUNS = json.loads({json.dumps(json.dumps(head_runs))})\n"
        f"REVIEWED = {reviewed!r}\n"
        "a = sys.argv[1:]\n"
        "opt = lambda k: a[a.index(k) + 1] if k in a else None\n"
        "if a[:2] == ['run', 'view']:\n"
        "    ids = {str(r['databaseId']) for r in HEAD_RUNS or []}\n"
        "    v = HEAD_VIEW if a[2] in ids else VIEW\n"
        "    if v is None: sys.exit(1)\n"
        "    print(json.dumps(v))\n"
        "elif a[:2] == ['run', 'list']:\n"
        "    sha = opt('--commit')\n"
        "    runs = HEAD_RUNS if HEAD_RUNS is not None and sha != REVIEWED else GREEN\n"
        "    print(json.dumps([dict(r, headSha=sha) for r in runs]))\n"
        "elif a[:2] == ['workflow', 'list']:\n"
        "    print(json.dumps([{'id': 1, 'name': 'CI', 'path': '.github/workflows/ci.yml',\n"
        "        'state': 'active'}]))\n"
        "else: print('')\n")
    (bindir / "gh").chmod(0o755)
    (bindir / "git").write_text(
        '#!/bin/sh\nif [ "$1" = push ]; then exit 0; fi\n'
        'if [ "$1" = branch ] && [ "$2" = -r ]; then echo "  origin/main"; exit 0; fi\n'
        'exec /usr/bin/git "$@"\n')
    (bindir / "git").chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                **{preflight.SONAR_API_ENV: "0"})


def _sh(repo: Path, cmd: str) -> str:
    return subprocess.run(cmd, shell=True, cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _sh(r, "git init -q -b test-pr && git config user.email t@t && git config user.name t")
    (r / "sonar-project.properties").write_text("sonar.projectKey=victorrentea_petclinic\n")
    _sh(r, "git add -A && git commit -qm one")
    (r / ".human-review" / "assets").mkdir(parents=True)
    (r / ".human-review" / "assets" / "old.svg").write_text("<svg/>")
    return r


def _recorded(sha: str) -> dict:
    return {"sha": sha, "verdict": "green", "caveat": "green: CI (run 37347327883)",
            "workflows": [{"workflow": "ci.yml", "name": "CI", "runId": 37347327883,
                           "url": RUN_URL, "status": "completed", "verdict": "success"}]}


def _ci_only(repo: Path, env: dict) -> dict:
    p = subprocess.run([sys.executable, str(HERE / "preflight.py"), "--ci-only"],
                       cwd=repo, env=env, text=True, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads((repo / ".human-review" / ".gate.json").read_text())


def test_the_gate_writes_sonar_into_gate_json(tmp_path):
    repo, env = _repo(tmp_path), _stub_gh(tmp_path, RUN)
    p = subprocess.run([sys.executable, str(HERE / "preflight.py")], cwd=repo, env=env,
                       text=True, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    gate = json.loads((repo / ".human-review" / ".gate.json").read_text())
    assert gate["verdict"] == "green" and "head" not in gate
    assert gate["sonar"]["verdict"] == "success" and gate["sonar"]["url"] == DASH
    assert gate["sonar"]["runId"] == 37347327883


def test_ci_only_rereads_the_recorded_run_and_touches_nothing_else(tmp_path):
    """The cheap re-run: no push, no gate, no wipe — only `sonar` in `.gate.json` moves,
    and no `head` while the head is the reviewed commit."""
    repo, env = _repo(tmp_path), _stub_gh(tmp_path, RUN)
    recorded = _recorded(_sh(repo, "git rev-parse HEAD"))
    (repo / ".human-review" / ".gate.json").write_text(json.dumps(recorded))
    gate = _ci_only(repo, env)
    assert {k: v for k, v in gate.items() if k != "sonar"} == recorded
    assert gate["sonar"]["url"] == DASH
    assert (repo / ".human-review" / "assets" / "old.svg").is_file()
    assert not (repo / ".human-review" / ".started").exists()


def test_gh_failing_on_run_view_records_no_sonar(tmp_path):
    repo, env = _repo(tmp_path), _stub_gh(tmp_path, None)
    p = subprocess.run([sys.executable, str(HERE / "preflight.py")], cwd=repo, env=env,
                       text=True, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "sonar" not in json.loads((repo / ".human-review" / ".gate.json").read_text())


# ── CI on the branch's head ───────────────────────────────────────────────────────────

#: Petclinic's 39346ec5, recorded: the push run failed on attempt 2 ("Frontend logical
#: architecture"), its pull_request twin was skipped by an `if:`, Sonar never ran.
HEAD_RUNS = json.loads((DATA / "petclinic-head-39346ec5-runs.json").read_text())
HEAD_VIEW = json.loads((DATA / "petclinic-run-39326398747.json").read_text())


def test_the_head_run_is_the_one_that_concluded_not_its_skipped_twin():
    one = preflight.pick_head_run(HEAD_RUNS, "test-pr")
    assert (one["databaseId"], one["event"], one["attempt"]) == (39326398747, "push", 2)


@pytest.mark.parametrize("runs,want", [
    # A cancelled push (concurrency) beside a pull_request run that concluded.
    ([{"databaseId": 1, "event": "push", "status": "completed", "conclusion": "cancelled"},
      {"databaseId": 2, "event": "pull_request", "status": "completed",
       "conclusion": "success"}], 2),
    # Both concluded: the push run on the branch.
    ([{"databaseId": 2, "event": "pull_request", "status": "completed",
       "conclusion": "success"},
      {"databaseId": 1, "event": "push", "status": "completed", "conclusion": "failure",
       "headBranch": "test-pr"}], 1),
    # Nothing concluded, one still running: that one.
    ([{"databaseId": 1, "event": "push", "status": "completed", "conclusion": "cancelled"},
      {"databaseId": 3, "event": "pull_request", "status": "in_progress"}], 3),
    # Only a skipped run: it is all there is.
    ([{"databaseId": 4, "event": "pull_request", "status": "completed",
       "conclusion": "skipped"}], 4),
])
def test_picking_the_head_run(runs, want):
    assert preflight.pick_head_run(runs, "test-pr")["databaseId"] == want


def test_ci_only_records_a_red_head_beside_the_reviewed_commit(tmp_path):
    repo = _repo(tmp_path)
    reviewed = _sh(repo, "git rev-parse HEAD")
    (repo / "b.txt").write_text("e2e\n")
    _sh(repo, "git add b.txt && git commit -qm two")
    head = _sh(repo, "git rev-parse HEAD")
    env = _stub_gh(tmp_path, RUN, HEAD_RUNS, HEAD_VIEW, reviewed)
    (repo / ".human-review" / ".gate.json").write_text(json.dumps(_recorded(reviewed)))
    gate = _ci_only(repo, env)
    assert gate["sha"] == reviewed and gate["verdict"] == "green"   # the gate stands
    h = gate["head"]
    assert (h["sha"], h["branch"], h["verdict"]) == (head, "test-pr", "failure")
    [w] = h["workflows"]
    assert (w["runId"], w["event"], w["attempt"], w["verdict"]) == \
        (39326398747, "push", 2, "failure")
    assert w["url"] == "https://github.com/victorrentea/petclinic/actions/runs/39326398747"
    assert h["sonar"]["verdict"] == "skipped" and h["sonar"]["runId"] == 39326398747


# ── the grade panel ───────────────────────────────────────────────────────────────────

def _panel(tmp_path, sonar: dict | None, score: int = 9,
           head: dict | None = None) -> tuple[str, dict]:
    out = tmp_path / ".human-review"
    out.mkdir()
    gate = {"sha": SHA, "verdict": "green", "caveat": "green",
            "workflows": [{"name": "CI", "runId": 37347327883, "url": RUN_URL,
                           "verdict": "success"}]}
    if sonar is not None:
        gate["sonar"] = sonar
    if head is not None:
        gate["head"] = head
    (out / ".gate.json").write_text(json.dumps(gate))
    spec = {"verdict": {"score": score}}
    build.grade_signals(spec, out, root=None)
    build.cap_grade(spec)
    return build.grade_reasons_html(spec), spec


def _a(href: str, face: str) -> str:
    return f'<a href="{href}" target="_blank" rel="noopener">{face}</a>'


def test_ci_and_sonar_green_is_one_bullet_with_two_links(tmp_path):
    panel, spec = _panel(tmp_path, {"verdict": "success", "gate": "success", "url": DASH})
    assert (f'<li>{_a(RUN_URL, "CI")} ✅ and {_a(html_amp(DASH), "Sonar")} ✅ '
            'on be762955</li>') in panel
    assert spec["verdict"]["score"] == 9


def html_amp(url: str) -> str:
    return url.replace("&", "&amp;")


def test_a_red_sonar_gate_under_green_ci_is_its_own_bullet_and_caps_at_7(tmp_path):
    panel, spec = _panel(tmp_path, {"verdict": "failure", "gate": "failure", "url": DASH,
                                    "gateStep": "SonarCloud Quality Gate check"})
    assert f'<li>{_a(RUN_URL, "CI")} ✅ on be762955</li>' in panel
    assert (f'{_a(html_amp(DASH), "Sonar")} quality gate failed on be762955 '
            '(caps the grade at 7)</li>') in panel
    assert "SonarCloud Quality Gate check step concluded failure" in panel
    assert spec["verdict"] == {"score": 7, "modelScore": 9}
    keys = [s["key"] for s in spec["_gradeSignals"]]
    assert keys[:2] == ["ci-green", "ci-sonar-failed"]


@pytest.mark.parametrize("sonar", [None, {"verdict": "skipped", "url": DASH}])
def test_no_sonar_or_a_skipped_one_leaves_the_ci_bullet_as_it_was(tmp_path, sonar):
    panel, spec = _panel(tmp_path, sonar)
    assert f'<li>{_a(RUN_URL, "CI")} ✅ on be762955</li>' in panel
    assert "Sonar" not in panel and spec["verdict"] == {"score": 9}


def test_sonar_green_without_a_dashboard_url_links_only_ci(tmp_path):
    panel, _ = _panel(tmp_path, {"verdict": "success", "url": "javascript:alert(1)"})
    assert f'<li>{_a(RUN_URL, "CI")} ✅ and Sonar ✅ on be762955</li>' in panel


HEAD = "39346ec541cac0bc8b4a1c207e5507fb4f98bd83"
HEAD_RUN_URL = "https://github.com/victorrentea/petclinic/actions/runs/39326398747"
GREEN_SONAR = {"verdict": "success", "gate": "success", "url": DASH}


def _head(verdict: str, sonar: dict | None = None, run_verdict: str | None = None) -> dict:
    h = {"sha": HEAD, "branch": "test-pr", "verdict": verdict,
         "workflows": [{"workflow": "ci.yml", "name": "CI", "runId": 39326398747,
                        "url": HEAD_RUN_URL, "attempt": 2,
                        "verdict": run_verdict or ("success" if verdict == "green"
                                                   else verdict)}]}
    if sonar:
        h["sonar"] = sonar
    return h


def test_a_red_head_is_its_own_bullet_and_caps_like_a_red_build(tmp_path):
    """Petclinic, 20 Oct 2026: "✅ CI and Sonar green" on be762955 while 39346ec5 was red."""
    panel, spec = _panel(tmp_path, GREEN_SONAR,
                         head=_head("failure", {"verdict": "skipped", "url": DASH}))
    assert (f'<li>{_a(RUN_URL, "CI")} \u2705 and {_a(html_amp(DASH), "Sonar")} \u2705 '
            'on be762955 (reviewed)</li>') in panel
    assert (f'{_a(HEAD_RUN_URL, "CI")} \u274c on 39346ec5 (latest), Sonar skipped '
            '(caps the grade at 4)</li>') in panel
    assert "CI (run 39326398747, attempt 2) concluded failure" in panel
    assert spec["verdict"] == {"score": 4, "modelScore": 9}
    assert [s["key"] for s in spec["_gradeSignals"]][:2] == ["ci-green", "ci-head-failed"]


def test_a_green_head_with_sonar_reads_like_the_reviewed_one_with_latest(tmp_path):
    panel, spec = _panel(tmp_path, GREEN_SONAR, head=_head("green", GREEN_SONAR))
    assert (f'<li>{_a(HEAD_RUN_URL, "CI")} \u2705 and {_a(html_amp(DASH), "Sonar")} \u2705 '
            'on 39346ec5 (latest)</li>') in panel
    assert spec["verdict"] == {"score": 9}


@pytest.mark.parametrize("verdict,why", [("pending", "is still running"),
                                         ("cancelled", "was cancelled")])
def test_a_head_no_build_proved_says_so_and_caps_like_an_unproven_one(tmp_path, verdict, why):
    panel, spec = _panel(tmp_path, None, head=_head(verdict))
    assert f'<li>{_a(RUN_URL, "CI")} \u2705 on be762955 (reviewed)</li>' in panel
    assert (f'No {_a(HEAD_RUN_URL, "build")} proved 39346ec5 (latest) '
            '(caps the grade at 6)</li>') in panel
    assert why in panel and spec["verdict"] == {"score": 6, "modelScore": 9}


def test_a_head_that_is_the_reviewed_commit_changes_nothing(tmp_path):
    panel, spec = _panel(tmp_path, None, head=dict(_head("failure"), sha=SHA))
    assert f'<li>{_a(RUN_URL, "CI")} \u2705 on be762955</li>' in panel
    assert "latest" not in panel and spec["verdict"] == {"score": 9}
