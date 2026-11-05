#!/usr/bin/env python3
"""Several snapshots go out in one commit and one push.

Every push to main deploys Pages and rebuilds the zip, so publishing `demo2` and then `demo`
did all of it twice, for two pages that were rebuilt together. Run against a real clone and a
real bare remote, because what is pinned is what reaches the remote.

Run with:  python3 -m pytest test_publish_demo.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLISH = HERE / "publish-demo.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout of the skill's repo, as much of it as publishing touches, and its remote."""
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True,
                   capture_output=True)
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "demo").mkdir()
    (repo / "demo" / "index.html").write_text("  <h2>One caveat</h2>\n", encoding="utf-8")
    (repo / "README.md").write_text("no featured demo here\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "start")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


def _snapshot(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name / ".human-review"
    d.mkdir(parents=True)
    (d / "review.html").write_text(f"<p>{name}</p>", encoding="utf-8")
    (d / "content.json").write_text(json.dumps({"title": name}), encoding="utf-8")
    return d


def _publish(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(PUBLISH), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "HUMAN_REVIEW_REPO": str(repo)})


def test_two_snapshots_are_one_commit_and_one_push(tmp_path):
    repo, remote = _repo(tmp_path)
    before = _git(remote, "rev-parse", "main").strip()
    p = _publish(repo, "--push", f"one={_snapshot(tmp_path, 'a')}", f"two={_snapshot(tmp_path, 'b')}")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _git(remote, "rev-list", "--count", f"{before}..main").strip() == "1"
    assert _git(remote, "log", "-1", "--format=%s", "main").strip() == \
        "demo: publish one two snapshots"
    files = _git(remote, "show", "--name-only", "--format=", "main").split()
    assert {"demo/one/review.html", "demo/two/review.html", "demo/index.html"} <= set(files)


def test_several_snapshots_must_be_pushed(tmp_path):
    """Staged and left there, the next commit anybody makes in this checkout would carry them."""
    repo, _ = _repo(tmp_path)
    p = _publish(repo, f"one={_snapshot(tmp_path, 'a')}")
    assert p.returncode == 2 and "--push" in p.stderr
    assert not (repo / "demo" / "one").exists()


def test_one_snapshot_still_publishes_the_way_it_did(tmp_path):
    repo, remote = _repo(tmp_path)
    p = _publish(repo, "one", _snapshot(tmp_path, "a"), "--push")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _git(remote, "log", "-1", "--format=%s", "main").strip() == "demo: publish one snapshot"
