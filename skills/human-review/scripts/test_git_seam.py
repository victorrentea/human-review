#!/usr/bin/env python3
"""A build asks git each question once, through one function.

There were nine spellings of "run git and read stdout" in the build's code, and 93 of a
page's 237 git processes were repeats. What is pinned here is the seam, and that the answers
stop being held the moment they could be wrong.

Run with:  python3 -m pytest test_git_seam.py
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from hrbuild.shared import util

HERE = Path(__file__).resolve().parent

#: The seam itself, the forge's own module, and `fetch_base`: the one call that writes.
ALLOWED = {"hrbuild/shared/util.py", "hrbuild/shared/forge.py", "hrbuild/shared/chips.py"}

BUILD_CODE = [*sorted(HERE.glob("hrbuild/**/*.py")),
              *(HERE / n for n in ("build-review-html.py", "extract-snippet.py",
                                   "harness_cost.py", "review-points.py", "semcov.py"))]


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    _commit(tmp_path, "one")
    return tmp_path


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", message],
                   check=True)


def _head(root: Path) -> str | None:
    return util._git(root, "rev-parse", "HEAD")


def test_the_build_asks_git_through_the_seam_alone():
    spelled = re.compile(r'subprocess\.run\(\s*\[\s*"git"')
    bad = [str(p.relative_to(HERE)) for p in BUILD_CODE
           if str(p.relative_to(HERE)) not in ALLOWED and spelled.search(p.read_text("utf-8"))]
    assert not bad, f"use _git or _git_run from hrbuild/shared/util.py: {bad}"


def test_outside_a_build_every_question_reaches_git(repo):
    before = _head(repo)
    _commit(repo, "two")
    assert _head(repo) != before, "a test or a tool that commits between two calls sees it"


def test_inside_a_build_a_question_is_asked_once(repo):
    with util.git_answers_held():
        before = _head(repo)
        _commit(repo, "two")
        assert _head(repo) == before
    assert _head(repo) != before, "the answers end with the build"


def test_a_failure_is_held_like_an_answer(repo, monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    with util.git_answers_held():
        assert util._git(repo, "rev-parse", "--verify", "--quiet", "nope") is None
        assert util._git(repo, "rev-parse", "--verify", "--quiet", "nope") is None
    assert len(calls) == 1


def test_fetching_forgets_what_git_said_about_the_refs(repo):
    with util.git_answers_held():
        before = _head(repo)
        _commit(repo, "two")
        util.forget_git_answers()
        assert _head(repo) != before


def test_a_build_of_a_page_ends_with_nothing_held(repo):
    with pytest.raises(RuntimeError):
        with util.git_answers_held():
            raise RuntimeError
    assert util._GIT_ANSWERS.get() is None


def test_no_root_and_no_folder_are_answers_and_not_exceptions(tmp_path):
    assert util._git(None, "status") is None
    assert util._git(tmp_path / "gone", "status") is None


def test_a_file_keeps_its_leading_lines_when_asked(repo):
    (repo / "f.txt").write_text("\n\nline three\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit(repo, "file")
    assert util._git(repo, "show", "HEAD:f.txt", strip=False) == "\n\nline three\n"
    assert util._git(repo, "show", "HEAD:f.txt") == "line three"
    assert util._git_run(repo, "show", "HEAD:f.txt", binary=True).stdout == b"\n\nline three\n"
