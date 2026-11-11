#!/usr/bin/env python3
"""The build gate, tested by running it rather than by reading the runbook's prose.

This used to be a text test over SKILL.md: it asserted that the paragraph about the gate
appeared above the paragraph about the wipe. That pinned the *document*, which is the wrong
artifact — the prose still reads exactly as convincing in the wrong place, and nothing
stopped the code from doing something else entirely. Now the gate is `preflight.py`, so the
tests drive it with a stub `gh` on PATH and check what it actually does.

The invariant with teeth: **a run that stops at the gate must leave the previous guide
intact.** Every case below therefore asserts on the artifacts as much as on the exit code.

Run with:  python3 -m pytest test_preflight_gate.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PREFLIGHT = HERE / "preflight.py"


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    run = lambda c: subprocess.run(c, shell=True, cwd=r, check=True, capture_output=True)
    run("git init -q -b main")
    run("git config user.email t@t && git config user.name t")
    (r / "a.txt").write_text("hi\n")
    run("git add -A && git commit -qm one")
    # A previous run's guide, which the gate must not destroy when it refuses.
    (r / ".human-review" / "assets").mkdir(parents=True)
    (r / ".human-review" / "assets" / "old.svg").write_text("<svg/>")
    (r / ".human-review" / "review.html").write_text("<html>previous</html>")
    return r


CI = {"id": 1, "name": "ci", "path": ".github/workflows/ci.yml", "state": "active"}
PAGES = {"id": 2, "name": "pages-build-deployment",
         "path": "dynamic/pages/pages-build-deployment", "state": "active"}


def _stub_gh(tmp_path: Path, runs: list, workflows=(CI,), fail_workflow_list=False) -> dict:
    """A `gh` that answers `run list` with `runs` and `workflow list` with `workflows`.

    Like the real one it honours `--workflow` (by name or file name), answers a workflow it
    does not know with a 404, and fills in `headSha` with HEAD when a run does not say."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, os\n"
        f"RUNS = json.loads({json.dumps(json.dumps(runs))})\n"
        f"WORKFLOWS = json.loads({json.dumps(json.dumps(list(workflows)))})\n"
        f"FAIL_LIST = {fail_workflow_list!r}\n"
        "a = sys.argv[1:]\n"
        "opt = lambda k: a[a.index(k) + 1] if k in a else None\n"
        "if a[:2] == ['run', 'list']:\n"
        "    sha, wf = opt('--commit'), opt('--workflow')\n"
        "    if wf is not None:\n"
        "        known = [w for w in WORKFLOWS if wf in (w['name'], os.path.basename(w['path']))]\n"
        "        if not known:\n"
        "            sys.stderr.write(f'HTTP 404: workflow {wf} not found on the default branch\\n')\n"
        "            sys.exit(1)\n"
        "        names = {w['name'] for w in known}\n"
        "    out = []\n"
        "    for r in RUNS:\n"
        "        r = dict(r); r.setdefault('headSha', sha)\n"
        "        if wf is not None and r.get('workflowName') not in names: continue\n"
        "        out.append(r)\n"
        "    print(json.dumps(out))\n"
        "elif a[:2] == ['workflow', 'list']:\n"
        "    if FAIL_LIST:\n"
        "        sys.stderr.write('HTTP 401: Bad credentials\\n'); sys.exit(1)\n"
        "    print(json.dumps(WORKFLOWS))\n"
        "else: print('')\n")
    gh.chmod(0o755)
    # `git push` must succeed without a remote, and must not be the thing under test here.
    git = bindir / "git"
    # And the commit is on a remote already: the gate reads CI only for a pushed commit.
    git.write_text('#!/bin/sh\nif [ "$1" = push ]; then exit 0; fi\n'
                   'if [ "$1" = branch ] && [ "$2" = -r ]; then echo "  origin/main"; exit 0; fi\n'
                   'exec /usr/bin/git "$@"\n')
    git.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", CLAUDE_CODE_SESSION_ID="s1")
    return env


def _run(repo: Path, env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PREFLIGHT), *args],
                          cwd=repo, env=env, text=True, capture_output=True,
                          timeout=60)


def test_a_green_run_for_this_commit_opens_the_gate(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "success", "workflowName": "ci"}])
    p = _run(repo, env)
    assert p.returncode == 0, p.stderr
    assert (repo / ".human-review" / ".started").is_file()
    assert (repo / ".human-review" / ".session").read_text().strip() == "s1"


def test_a_failed_run_refuses_and_destroys_nothing(tmp_path):
    """The whole point of gating before the wipe: a refused run leaves the last guide."""
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "failure", "workflowName": "ci"}])
    p = _run(repo, env)
    assert p.returncode == 1
    assert (repo / ".human-review" / "assets" / "old.svg").is_file(), \
        "the gate refused but the previous run's assets were wiped anyway"
    assert (repo / ".human-review" / "review.html").read_text() == "<html>previous</html>"
    assert not (repo / ".human-review" / ".started").exists()
    assert "ci" in p.stdout and "failure" in p.stdout


def test_no_run_at_all_is_not_a_pass(tmp_path):
    """`gh run list` returns `[]` because nothing ever built this commit. It is the one
    non-green state that looks like a pass, and the repository *does* have workflows."""
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [])
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1
    assert "Absence is not success" in p.stdout + p.stderr
    assert (repo / ".human-review" / "assets" / "old.svg").is_file()


def test_a_repo_with_no_ci_at_all_is_let_through_but_reported(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [], workflows=())
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 0
    assert "no build proved this" in p.stdout
    assert "no build proved this" in (repo / ".human-review" / ".gate").read_text(), \
        "the caveat has to survive into the run, or the guide cannot repeat it"


def test_the_wait_is_bound_to_the_commit_never_to_the_branch(tmp_path):
    """A branch almost always has *some* green run on it, which is what makes `--branch`
    tempting and wrong. Asserted on the arguments actually handed to `gh`."""
    repo = _repo(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    gh = bindir / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport sys\n"
                  f"open({json.dumps(str(log))}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
                  "a = sys.argv[1:]\n"
                  "print('[{\"databaseId\":1,\"status\":\"completed\",\"conclusion\":"
                  "\"success\",\"workflowName\":\"ci\"}]' if a[:2] == ['run','list'] else '')\n")
    gh.chmod(0o755)
    git = bindir / "git"
    # And the commit is on a remote already: the gate reads CI only for a pushed commit.
    git.write_text('#!/bin/sh\nif [ "$1" = push ]; then exit 0; fi\n'
                   'if [ "$1" = branch ] && [ "$2" = -r ]; then echo "  origin/main"; exit 0; fi\n'
                   'exec /usr/bin/git "$@"\n')
    git.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", CLAUDE_CODE_SESSION_ID="s1")
    assert _run(repo, env, "--workflow", "ci.yml").returncode == 0
    called = log.read_text()
    sha = subprocess.run("git rev-parse HEAD", shell=True, cwd=repo, text=True,
                         capture_output=True).stdout.strip()
    assert f"run list --commit {sha} --workflow ci.yml" in called
    assert "--branch" not in called


def test_no_gate_still_records_that_nothing_proved_it(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [])
    p = _run(repo, env, "--no-gate")
    assert p.returncode == 0
    assert "no build proved this" in (repo / ".human-review" / ".gate").read_text()


def test_the_wipe_and_the_ledger_reset_happen_together(tmp_path):
    """A stale `.steps.json` parses perfectly and turns 'not measured' into a confident
    `$0.00` on every tab, so it must not survive a run the assets did not."""
    repo = _repo(tmp_path)
    (repo / ".human-review" / ".steps.json").write_text('[{"tabs":["api"],"start":"x"}]')
    env = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "success", "workflowName": "ci"}])
    assert _run(repo, env).returncode == 0
    assert not (repo / ".human-review" / "assets" / "old.svg").exists()
    leftover = (repo / ".human-review" / ".steps.json")
    assert not leftover.exists() or json.loads(leftover.read_text()) == []


def test_the_wipe_removes_earlier_builds_logs_and_keeps_this_runs(tmp_path):
    """Eval run 11 served `refresh.out` from another folder's build and a `run-steps-2.out`
    measured from an old base beside the new page. The wipe takes the top-level `*.out` an
    earlier build wrote; a log the shell opened for this run (just now) stays, and so does
    anything that is not a log. A refused gate removes nothing."""
    repo = _repo(tmp_path)
    hr = repo / ".human-review"
    old = time.time() - 3600
    for name in ("refresh.out", "run-steps-2.out"):
        (hr / name).write_text("[run-steps] base 5a97353ee542\n")
        os.utime(hr / name, (old, old))
    (hr / "notes.txt").write_text("keep")
    os.utime(hr / "notes.txt", (old, old))
    (hr / "preflight.out").write_text("")                  # this run's own redirect

    red = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "failure", "workflowName": "ci"}])
    assert _run(repo, red).returncode == 1
    assert (hr / "refresh.out").is_file(), "a refused run must not touch the last guide"

    env = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "success", "workflowName": "ci"}])
    p = _run(repo, env)
    assert p.returncode == 0, p.stderr
    assert not (hr / "refresh.out").exists() and not (hr / "run-steps-2.out").exists()
    assert (hr / "preflight.out").is_file() and (hr / "notes.txt").is_file()
    assert "cleared refresh.out" in p.stdout


# ---- Which workflow is authoritative -------------------------------------------------------
# A push runs several workflows. `gh run list --commit … --limit 1` took whichever came first,
# so a green Pages deploy could open the gate while the application's CI was red or queued.


def _ok(name, rid=1, **kw):
    return {"databaseId": rid, "status": "completed", "conclusion": "success",
            "workflowName": name, "createdAt": f"2026-10-02T10:00:{rid:02d}Z", **kw}


def _evidence(repo: Path) -> dict:
    return json.loads((repo / ".human-review" / ".gate.json").read_text())


def test_a_green_deploy_cannot_open_the_gate_while_ci_is_red(tmp_path):
    repo = _repo(tmp_path)
    (repo / "human-review.json").write_text(json.dumps({"ci": {"workflows": ["ci.yml"]}}))
    env = _stub_gh(tmp_path, [_ok("pages-build-deployment", 2),
                              dict(_ok("ci", 1), conclusion="failure")],
                   workflows=(CI, PAGES))
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1, p.stdout
    assert "ci (run 1) concluded failure" in p.stdout
    assert (repo / ".human-review" / "assets" / "old.svg").is_file()
    ev = _evidence(repo)
    assert ev["verdict"] == "failure" and ev["selection"] == "configured"
    assert [(w["workflow"], w["runId"], w["verdict"]) for w in ev["workflows"]] == \
        [("ci.yml", 1, "failure")]


def test_a_green_deploy_cannot_open_the_gate_while_ci_is_still_queued(tmp_path):
    """Auto-detected: `pages-build-deployment` has "build" in its name and must not count.
    A run never picked up by a runner is an outage, reported as one — not as a red build."""
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [_ok("pages-build-deployment", 2),
                              dict(_ok("ci", 1), status="queued", conclusion="")],
                   workflows=(CI, PAGES))
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1
    assert "runner outage" in p.stdout and "failure" not in p.stdout
    ev = _evidence(repo)
    assert ev["selection"] == "auto-detected"
    assert ev["verdict"] == "queued"
    assert ev["workflows"][0]["workflow"] == "ci.yml"


def test_green_evidence_names_workflow_run_and_exact_sha(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [_ok("pages-build-deployment", 2), _ok("ci", 7)],
                   workflows=(CI, PAGES))
    p = _run(repo, env)
    assert p.returncode == 0, p.stdout + p.stderr
    sha = subprocess.run("git rev-parse HEAD", shell=True, cwd=repo, text=True,
                         capture_output=True).stdout.strip()
    ev = _evidence(repo)
    assert ev["verdict"] == "green" and ev["sha"] == sha
    assert [(w["workflow"], w["name"], w["runId"], w["sha"], w["verdict"])
            for w in ev["workflows"]] == [("ci.yml", "ci", 7, sha, "success")]
    assert "[preflight] evidence {" in p.stdout
    assert "green: ci (run 7)" in (repo / ".human-review" / ".gate").read_text()


def test_every_configured_workflow_must_be_green(tmp_path):
    repo = _repo(tmp_path)
    e2e = {"id": 3, "name": "e2e", "path": ".github/workflows/e2e.yml", "state": "active"}
    env = _stub_gh(tmp_path, [_ok("ci", 1)], workflows=(CI, e2e))
    p = _run(repo, env, "--wait-minutes", "0", "--workflow", "ci.yml", "--workflow", "e2e.yml")
    assert p.returncode == 1
    assert "no e2e.yml run" in p.stdout and "Absence is not success" in p.stdout
    assert {w["workflow"]: w["verdict"] for w in _evidence(repo)["workflows"]} == \
        {"ci.yml": "success", "e2e.yml": "not-found"}


def test_the_cli_flag_overrides_the_config(tmp_path):
    repo = _repo(tmp_path)
    e2e = {"id": 3, "name": "e2e", "path": ".github/workflows/e2e.yml", "state": "active"}
    (repo / "human-review.json").write_text(json.dumps({"ci": "e2e.yml"}))
    env = _stub_gh(tmp_path, [_ok("ci", 1)], workflows=(CI, e2e))
    assert _run(repo, env, "--wait-minutes", "0", "--workflow", "ci").returncode == 0
    assert _run(repo, env, "--wait-minutes", "0").returncode == 1   # config: e2e.yml, absent


def test_a_cancelled_run_is_not_reported_as_a_failing_build(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [dict(_ok("ci", 4), conclusion="cancelled")])
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1
    assert "was cancelled" in p.stdout and "concluded failure" not in p.stdout
    assert _evidence(repo)["verdict"] == "cancelled"


def test_a_misnamed_workflow_is_a_configuration_error_not_a_red_build(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [_ok("ci", 1)])
    p = _run(repo, env, "--wait-minutes", "5", "--workflow", "nosuch.yml")
    assert p.returncode == 1                        # and immediately, not after 5 minutes
    assert "does not exist" in p.stdout
    assert _evidence(repo)["verdict"] == "workflow-not-found"


def test_github_not_answering_is_a_discovery_failure_never_no_ci(tmp_path):
    """Before, an unauthenticated `gh workflow list` printed nothing, which read as "this
    repository has no CI" — and let the run through."""
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [], fail_workflow_list=True)
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1
    assert "discovery failure" in p.stdout and "no CI configured" not in p.stdout
    assert _evidence(repo)["verdict"] == "discovery-failed"
    assert (repo / ".human-review" / "assets" / "old.svg").is_file()


def test_a_run_for_another_sha_never_counts(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [_ok("ci", 1, headSha="0" * 40)])
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1 and "Absence is not success" in p.stdout


def test_without_a_ci_like_workflow_every_run_on_the_sha_must_be_green(tmp_path):
    repo = _repo(tmp_path)
    lint = {"id": 4, "name": "lint", "path": ".github/workflows/lint.yml", "state": "active"}
    deploy = {"id": 5, "name": "deploy", "path": ".github/workflows/deploy.yml",
              "state": "active"}
    env = _stub_gh(tmp_path, [_ok("deploy", 2), dict(_ok("lint", 1), conclusion="failure")],
                   workflows=(lint, deploy))
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1 and "lint (run 1) concluded failure" in p.stdout
    assert _evidence(repo)["selection"] == "all-runs"


def test_the_newest_run_of_the_workflow_decides(tmp_path):
    """A failed run, then a green re-run on the same SHA: the re-run is the verdict."""
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [dict(_ok("ci", 1), conclusion="failure"), _ok("ci", 2)])
    p = _run(repo, env)
    assert p.returncode == 0, p.stdout
    assert _evidence(repo)["workflows"][0]["runId"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_the_wipe_clears_model_state_that_belongs_to_another_branch(tmp_path):
    """Eval run 8 reused a `.human-review/` three branches old: `.model-prev/` still held
    `feature-script.hr-claude-5.js`, and the AI chip priced itself off a run from before
    the branch forked. What predates the fork goes; what this branch made stays."""
    repo = _repo(tmp_path)
    hr = repo / ".human-review"
    prev = hr / ".model-prev"
    prev.mkdir()
    (prev / "feature-script.hr-claude-5.js").write_text("old")
    os.utime(prev / "feature-script.hr-claude-5.js", (1_600_000_000, 1_600_000_000))
    (prev / "test-mapping.json").write_text("{}")             # made after the fork: kept
    os.utime(prev / "test-mapping.json", (4_000_000_000, 4_000_000_000))
    (hr / ".model-runs.json").write_text(json.dumps({"version": 1, "runs": [
        {"when": "2020-01-01T00:00:00+00:00", "model": "sonnet", "cost": 4.0},
        {"when": "2099-01-01T00:00:00+00:00", "model": "haiku", "cost": 0.17}]}))
    env = _stub_gh(tmp_path, [])
    p = _run(repo, env, "--no-gate", "--base", "main")
    assert p.returncode == 0, p.stderr
    assert sorted(f.name for f in prev.iterdir()) == ["test-mapping.json"]
    runs = json.loads((hr / ".model-runs.json").read_text())["runs"]
    assert [r["model"] for r in runs] == ["haiku"]
    assert "cleared .model-prev/feature-script.hr-claude-5.js" in p.stdout
    assert (hr / ".branch").read_text().strip() == "main"
    # The last run was on another branch: everything it kept goes, however recent.
    (hr / ".branch").write_text("hr-claude-7\n")
    assert _run(repo, env, "--no-gate", "--base", "main").returncode == 0
    assert not any(prev.iterdir())


def test_a_branch_switch_clears_another_branchs_coverage(tmp_path, monkeypatch):
    """Eval run 12 read run 11's Playwright coverage, measured on another branch."""
    import importlib.util, subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "two"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "x"], cwd=repo, check=True)
    hr = repo / ".human-review"
    (hr / "coverage" / "playwright").mkdir(parents=True)
    (hr / "coverage" / "playwright" / "run.json").write_text("{}")
    (hr / ".branch").write_text("one\n")
    monkeypatch.chdir(repo)
    spec = importlib.util.spec_from_file_location("pf", Path(__file__).parent / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    monkeypatch.setattr(pf, "HR", hr)
    gone = pf.clear_foreign_model_state("HEAD")
    assert not (hr / "coverage").exists()
    assert any("coverage" in g for g in gone)


# --- Ludvig: the gate never pushes unless told to, and reads CircleCI's commit statuses ---

def _stub_status_gh(tmp_path: Path, statuses: list, checks: list = ()) -> dict:
    """A `gh` for a repository with no Actions workflows, whose CI (CircleCI) reports as
    commit statuses and check runs; `git branch -r` says the commit is pushed."""
    env = _stub_gh(tmp_path, [], workflows=())
    gh = tmp_path / "bin" / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['workflow', 'list'] or a[:2] == ['run', 'list']: print('[]')\n"
        "elif a[0] == 'api' and a[1].endswith('/status'):\n"
        f"    print(json.dumps({{'statuses': {json.dumps(statuses)}}}))\n"
        "elif a[0] == 'api' and '/check-runs' in a[1]:\n"
        f"    print(json.dumps({{'check_runs': {json.dumps(list(checks))}}}))\n"
        "else: print('')\n")
    return env


def test_a_commit_that_is_not_pushed_is_unproven_and_nothing_is_pushed(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [])
    (tmp_path / "bin" / "git").write_text(
        '#!/bin/sh\nif [ "$1" = push ]; then echo pushed >> "$0.log"; exit 0; fi\n'
        'exec /usr/bin/git "$@"\n')
    p = _run(repo, env)
    assert p.returncode == 0, p.stderr
    gate = json.loads((repo / ".human-review" / ".gate.json").read_text())
    assert gate["verdict"] == "unproven-not-pushed"
    assert not (tmp_path / "bin" / "git.log").exists()


def test_ci_push_lets_the_gate_push(tmp_path):
    repo = _repo(tmp_path)
    (repo / "human-review.json").write_text(json.dumps({"ci": {"push": True}}))
    env = _stub_gh(tmp_path, [{"databaseId": 1, "status": "completed",
                               "conclusion": "success", "workflowName": "ci"}])
    git = tmp_path / "bin" / "git"
    git.write_text(git.read_text().replace(
        'if [ "$1" = push ]; then exit 0; fi', 'if [ "$1" = push ]; then echo p >> "$0.log"; exit 0; fi'))
    assert _run(repo, env).returncode == 0
    assert (tmp_path / "bin" / "git.log").read_text().strip() == "p"


def test_circleci_statuses_prove_the_commit_and_its_approval_job_is_not_waited_for(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_status_gh(tmp_path, [
        {"id": 1, "context": "ci/circleci: lint", "state": "success"},
        {"id": 2, "context": "ci/circleci: test", "state": "success"},
        {"id": 3, "context": "ci/circleci: approve-deploy-test", "state": "pending"}])
    p = _run(repo, env)
    assert p.returncode == 0, p.stderr
    gate = json.loads((repo / ".human-review" / ".gate.json").read_text())
    assert gate["verdict"] == "green"
    assert {w["name"] for w in gate["workflows"]} == {"ci/circleci: lint", "ci/circleci: test"}


def test_a_red_circleci_job_refuses(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_status_gh(tmp_path, [], checks=[
        {"id": 7, "name": "test", "status": "completed", "conclusion": "failure",
         "app": {"slug": "circleci-checks"}}])
    p = _run(repo, env)
    assert p.returncode == 1
    assert "test" in p.stdout + p.stderr


def test_a_configured_name_matches_the_circleci_job(tmp_path):
    repo = _repo(tmp_path)
    (repo / "human-review.json").write_text(json.dumps({"ci": {"workflows": ["test"]}}))
    env = _stub_status_gh(tmp_path, [
        {"id": 2, "context": "ci/circleci: test", "state": "success"},
        {"id": 4, "context": "ci/circleci: lint", "state": "failure"}])
    p = _run(repo, env)
    assert p.returncode == 0, p.stderr
