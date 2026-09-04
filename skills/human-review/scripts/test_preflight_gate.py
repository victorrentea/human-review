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


def _stub_gh(tmp_path: Path, runs: list, workflows: str = "ci  active  1") -> dict:
    """A `gh` that answers `run list` with `runs` and `workflow list` with `workflows`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        f"RUNS = {json.dumps(json.dumps(runs))}\n"
        f"WORKFLOWS = {json.dumps(workflows)}\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['run', 'list']: print(RUNS)\n"
        "elif a[:2] == ['workflow', 'list']: print(WORKFLOWS)\n"
        "else: print('')\n")
    gh.chmod(0o755)
    # `git push` must succeed without a remote, and must not be the thing under test here.
    git = bindir / "git"
    git.write_text('#!/bin/sh\nif [ "$1" = push ]; then exit 0; fi\nexec /usr/bin/git "$@"\n')
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
    env = _stub_gh(tmp_path, [], workflows="ci  active  1")
    p = _run(repo, env, "--wait-minutes", "0")
    assert p.returncode == 1
    assert "Absence is not success" in p.stdout + p.stderr
    assert (repo / ".human-review" / "assets" / "old.svg").is_file()


def test_a_repo_with_no_ci_at_all_is_let_through_but_reported(tmp_path):
    repo = _repo(tmp_path)
    env = _stub_gh(tmp_path, [], workflows="")
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
    git.write_text('#!/bin/sh\nif [ "$1" = push ]; then exit 0; fi\nexec /usr/bin/git "$@"\n')
    git.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", CLAUDE_CODE_SESSION_ID="s1")
    assert _run(repo, env).returncode == 0
    called = log.read_text()
    sha = subprocess.run("git rev-parse HEAD", shell=True, cwd=repo, text=True,
                         capture_output=True).stdout.strip()
    assert f"run list --commit {sha}" in called
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
