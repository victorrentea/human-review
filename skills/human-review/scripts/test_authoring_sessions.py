#!/usr/bin/env python3
"""Who wrote the code under review, and how sure the answer is.

The page's assumptions pile is only as honest as this: mode B hands a whole transcript to
a subagent on the strength of one session being named the author, so a reader promoted to
an author is the expensive direction to be wrong in. These fixtures are synthetic
transcripts — the same JSONL shape Claude Code writes — under a fake HOME.

Run with:  python3 -m pytest test_authoring_sessions.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "authoring-sessions.py"


def _repo(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "kept.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / "src.py").write_text("changed\n")
    return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                               capture_output=True, text=True).stdout.strip())


def _session(home: Path, repo: Path, sid: str, *tool_calls, parent: str = "") -> Path:
    """One transcript holding the given tool calls, filed the way Claude Code files it."""
    slug = str(repo).replace("/", "-")
    d = home / ".claude" / "projects" / slug
    if parent:
        d = d / parent / "subagents"
        name = f"agent-{sid}.jsonl"
    else:
        name = f"{sid}.jsonl"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"timestamp": "2026-09-01T10:0%d:00Z" % i,
             "message": {"content": [{"type": "tool_use", "name": name_, "input": inp}]}}
            for i, (name_, inp) in enumerate(tool_calls)]
    path = d / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _run(repo: Path, home: Path, sid: str = "", *args: str):
    env = dict(os.environ, HOME=str(home))
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    return subprocess.run([sys.executable, str(SCRIPT), "--base", "HEAD", *args],
                          cwd=repo, capture_output=True, text=True, env=env)


def test_an_edit_tool_call_on_a_changed_file_names_the_author(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa", ("Edit", {"file_path": str(repo / "src.py")}))
    out = _run(repo, home, "aaa", "--json")
    doc = json.loads(out.stdout)
    assert doc["mode"] == "A"
    assert out.returncode == 0
    assert doc["sessions"][0]["session"] == "aaa"
    assert doc["sessions"][0]["edits"] == 1


def test_reading_a_file_is_not_writing_it(tmp_path):
    """Half the sessions on this machine have read these files; one wrote them."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa", ("Read", {"file_path": str(repo / "src.py")}),
             ("Grep", {"pattern": "x", "path": str(repo / "src.py")}))
    out = _run(repo, home, "aaa", "--json")
    assert json.loads(out.stdout)["mode"] == "C"
    assert out.returncode == 5


def test_a_redirect_elsewhere_in_the_pipeline_does_not_make_a_reader_an_author(tmp_path):
    """`cat src.py | grep -n x > /tmp/out` names the file and contains a redirect. On an
    anywhere-in-the-command reading that is an edit, and it is the most common shell
    command in any transcript."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa",
             ("Bash", {"command": "cat src.py | grep -n x > /tmp/out"}),
             ("Bash", {"command": "sed -n '1,5p' src.py >> /tmp/notes"}))
    assert json.loads(_run(repo, home, "aaa", "--json").stdout)["mode"] == "C"


def test_a_heredoc_written_to_the_file_is_an_edit(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa",
             ("Bash", {"command": "cat > src.py <<'EOF'\nprint(1)\nEOF"}),
             ("Bash", {"command": "sed -i '' 's/a/b/' src.py"}))
    doc = json.loads(_run(repo, home, "aaa", "--json").stdout)
    assert doc["mode"] == "A"
    assert doc["sessions"][0]["bash"] == 2


def test_a_python_heredoc_that_writes_counts_even_though_no_redirect_points_at_the_path(
        tmp_path):
    """The path lives in a string literal there, where no `>` can vouch for it. The write
    call is the vouch instead — and this is how a whole class of agent editing looks."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa", ("Bash", {
        "command": "python3 - <<'PY'\np = Path('src.py')\np.write_text(s)\nPY"}),
        ("Bash", {"command": "python3 - <<'PY'\nprint(Path('src.py').read_text())\nPY"}))
    doc = json.loads(_run(repo, home, "aaa", "--json").stdout)
    assert doc["sessions"][0]["bash"] == 1, "the reading one is not an edit"


def test_a_script_that_writes_one_file_while_naming_another_does_not_claim_both(tmp_path):
    """The remaining looseness in a heredoc, closed: the write call vouches for the file
    being opened, not for every path the script happens to print."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "aaa", ("Bash", {
        "command": "python3 - <<'PY'\nPath('/tmp/o').write_text(x)\nprint('src.py is next')\nPY"}))
    assert json.loads(_run(repo, home, "aaa", "--json").stdout)["mode"] == "C"


def test_an_older_session_puts_the_run_in_mode_b(tmp_path):
    """The case the three modes exist for: the code was written in a conversation that is
    not this one, and its transcript is the only place the hesitation is recorded."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "older", ("Edit", {"file_path": str(repo / "src.py")}))
    out = _run(repo, home, "somebody-else", "--json")
    assert json.loads(out.stdout)["mode"] == "B"
    assert out.returncode == 4
    paths = _run(repo, home, "somebody-else", "--paths").stdout.split()
    assert paths and paths[0].endswith("older.jsonl")


def test_nothing_on_disk_wrote_these_files(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    out = _run(repo, home, "aaa", "--json")
    assert json.loads(out.stdout)["mode"] == "C"
    assert out.returncode == 5


def test_a_subagents_edits_belong_to_the_session_that_spawned_it(tmp_path):
    """A forked implementer is still this conversation working, and its transcript sits
    beside the parent's rather than in it."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo, "parent", ("Read", {"file_path": str(repo / "src.py")}))
    _session(home, repo, "1234", ("Edit", {"file_path": str(repo / "src.py")}),
             parent="parent")
    doc = json.loads(_run(repo, home, "parent", "--json").stdout)
    assert doc["mode"] == "A"
    assert doc["sessions"][0]["session"] == "parent"


def test_a_session_started_in_an_ancestor_directory_is_still_in_scope(tmp_path):
    """`~/workspace` sessions reach the repo by path and are filed under the ancestor."""
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    _session(home, repo.parent, "up", ("Write", {"file_path": str(repo / "src.py")}))
    assert json.loads(_run(repo, home, "up", "--json").stdout)["mode"] == "A"


def test_a_session_from_an_unrelated_project_is_never_consulted(tmp_path):
    repo = _repo(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "elsewhere"
    other.mkdir()
    _session(home, other, "stranger", ("Edit", {"file_path": str(repo / "src.py")}))
    assert json.loads(_run(repo, home, "aaa", "--json").stdout)["mode"] == "C"


def test_no_change_set_is_not_a_mode_at_all(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "all in"], cwd=repo, check=True)
    out = _run(repo, tmp_path / "home", "aaa")
    assert out.returncode == 2
    assert "nothing to attribute" in out.stderr
