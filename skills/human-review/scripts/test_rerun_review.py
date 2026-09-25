"""`rerun-review.py` — the Review tab's paid 🤖, which re-reviews the whole PR and commits.

No test here calls a model. The real-run tests put a fake `claude` first on PATH: a script
that reads the prompt, edits the tree the way the prompt asks a real run to, and prints
the `--output-format json` envelope. Everything after it — the snapshot, the refusals, the
commits, the trailers, the push — is this program's own work and runs for real, against a
throwaway repository with a bare `origin`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "rerun-review.py"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load("rerun_review_under_test", "rerun-review.py")
commits_mod = _load("review_commits_for_rerun_test", "review-commits.py")


def sh(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(root: Path, message: str, **files: str) -> str:
    for rel, text in files.items():
        p = root / rel.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        sh(root, "add", "--", str(p.relative_to(root)))
    sh(root, "commit", "-q", "-m", message)
    return sh(root, "rev-parse", "HEAD")


OLD_POINTS = textwrap.dedent("""\
    ---
    ticket: acme/app#7
    base: 0000000
    implementation: {impl}
    reviewers: /code-review high
    session: old-session
    ---

    ## Fixed

    ### Old fix
    - file: app.py:1
    - fixed-in: HEAD
    """)


@pytest.fixture
def repo(tmp_path):
    """main on a bare origin; `feature` = implementation, review, a human commit, a
    takeover of it, and one more human commit — the demo branch's shape, small. `feature`
    tracks `origin/main`, as `test-pr` does, so a bare push would land on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root = tmp_path / "repo"
    root.mkdir()
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.email", "t@example.com")
    sh(root, "config", "user.name", "T")
    sh(root, "config", "commit.gpgsign", "false")
    commit(root, "init", **{"README.md": "hello\n", ".gitignore": ".human-review/\n"})
    sh(root, "remote", "add", "origin", str(origin))
    sh(root, "push", "-q", "origin", "main")
    sh(root, "fetch", "-q", "origin")
    sh(root, "checkout", "-q", "-b", "feature")
    sh(root, "branch", "--set-upstream-to=origin/main")
    impl = commit(root, "feat: the feature", **{"app.py": "a = 1\nb = 2\nc = 3\n"})
    review = commit(root, "[auto-fix] review\n\nReview-Points: review-points.md\n"
                    f"Implements: {impl}\nClaude-Session: old-session\n",
                    **{"review-points.md": OLD_POINTS.format(impl=impl[:8])})
    commit(root, "human: tweak", **{"app.py": "a = 1\nb = 2\nc = 4\n"})
    commit(root, "review-points: take over\n\nReview-Points: review-points.md\n"
           f"Implements: {review}\n",
           **{"review-points.md": OLD_POINTS.format(impl=impl[:8])
              + "\n## Taken over without a new pass — 1 Jan 2026\n\n- the tweak\n"})
    commit(root, "human: another", **{"other.py": "x = 1\n"})
    sh(root, "push", "-q", "origin", "HEAD:feature")
    (root / ".human-review").mkdir()
    (root / ".human-review" / "content.json").write_text("{}")
    return {"root": root, "origin": origin, "impl": impl, "review": review,
            "main": sh(origin, "rev-parse", "main")}


FAKE = r'''#!{python}
"""A stand-in for `claude -p`: it does what the prompt asks, and records being called."""
import json, os, re, sys
from pathlib import Path
argv = sys.argv[1:]
session = argv[argv.index("--session-id") + 1]
prompt = sys.stdin.read()
Path(os.environ["FAKE_LOG"]).write_text(json.dumps({{"argv": argv, "prompt": prompt}}))
mode = os.environ.get("FAKE_MODE", "fix")
review = Path(".human-review")
Path("app.py").write_text("a = 1\nb = 2\nc = 5\n")               # the fix
if mode == "unclaimed":
    Path("stray.py").write_text("oops = 1\n")
(review / ".review-fixes.json").write_text(json.dumps({{"fixes": [
    {{"id": "FIX-1", "subject": "keep c in range", "why": "c overflowed.",
      "files": ["app.py"]}}]}}))
Path("review-points.md").write_text("""---
ticket: acme/app#7
base: whatever
implementation: made-up
reviewers: me
session: made-up
---

## Fixed

### c overflowed its range
- file: app.py:3
- source: /code-review high
- severity: medium
- fixed-in: FIX-1

## Ignored

### b could be a constant
- file: app.py:2
- source: /code-review high
- severity: low
- why: out of scope

## Assumptions

### a starts at one
- file: app.py:1
- alternative: zero
- confidence: 0.5
- why: the ticket says first

## Taken over without a new pass — never

- this note must not survive
""")
(review / "pr-comments.json").write_text(json.dumps({{
    "version": 1, "commit_id": "REVIEW-COMMIT", "event": "COMMENT",
    "body": "**Review record**: 1 auto-fixed", "comments": [
        {{"pile": "fixed", "title": "c overflowed its range", "path": "app.py",
          "line": 3, "side": "RIGHT", "anchor": "c = 5",
          "body": "\U0001F6E0 **Auto-fixed** in FIX-1 — c overflowed."}}]}}))
print(json.dumps({{"total_cost_usd": 0.42, "result": "1 fixed, 1 declined, 1 assumed",
                  "session_id": session}}))
'''


@pytest.fixture
def fake_claude(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "claude-called.json"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               FAKE_LOG=str(log))
    return {"env": env, "log": log}


def run(root: Path, env: dict, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--dir", ".human-review", *extra],
                          cwd=str(root), env=env, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# --dry-run: the command, the prompt, the commits — and nothing spent
# --------------------------------------------------------------------------- #

def test_dry_run_prints_the_command_the_prompt_and_the_commits(repo, fake_claude):
    p = run(repo["root"], fake_claude["env"], "--dry-run")
    assert p.returncode == 0, p.stderr
    o = p.stdout
    assert "claude -p --model opus --session-id " in o
    assert "--permission-mode acceptEdits" in o and "--max-budget-usd" in o
    assert "'Bash(git commit:*)'" in o.split("--disallowedTools", 1)[1]
    assert "'Bash(git push:*)'" in o.split("--disallowedTools", 1)[1]
    assert "< review-prompt.md" in o
    assert "You are re-reviewing a pull request" in o          # the prompt's head
    merge_base = sh(repo["root"], "merge-base", "origin/main", "HEAD")
    assert f"merge base `{merge_base}`" in o
    assert f"Implements: {repo['impl']}" in o                  # the real implementation,
    assert f"Implements: {repo['review']}" not in o            # not the takeover's link
    assert "Review-Points: review-points.md" in o and "Claude-Session: " in o
    assert "[auto-fix] <subject>" in o
    assert "git push origin HEAD:feature" in o
    assert "~$15" in o and "Opus" in o
    assert not fake_claude["log"].exists(), "a dry run called the model"


def test_the_prompt_file_has_no_unfilled_holes(repo):
    import re
    body = rr.prompt_body(rr.PROMPT.read_text(encoding="utf-8"))
    holes = set(re.findall(r"@@([A-Z_]+)@@", body))
    filled = rr.fill(body, {h: "x" for h in holes})
    assert "@@" not in filled
    # Every hole the prompt uses is one `main` fills.
    src = SCRIPT.read_text(encoding="utf-8")
    assert all(f'"{h}":' in src for h in holes), holes


# --------------------------------------------------------------------------- #
# the real run, against a fake model
# --------------------------------------------------------------------------- #

def test_a_run_commits_the_fix_then_the_record_and_pushes_to_its_own_branch(
        repo, fake_claude):
    root = repo["root"]
    (root / "notes.txt").write_text("somebody's untracked scratch\n")
    head_before = sh(root, "rev-parse", "HEAD")
    p = run(root, fake_claude["env"])
    assert p.returncode == 0, p.stdout + p.stderr
    called = json.loads(fake_claude["log"].read_text())
    session = called["argv"][called["argv"].index("--session-id") + 1]

    # Two new commits: the fix, then the record.
    new = sh(root, "rev-list", "--reverse", f"{head_before}..HEAD").split()
    assert len(new) == 2
    fix, rec = new
    assert sh(root, "log", "-1", "--format=%s", fix).startswith("[auto-fix] keep c in range")
    assert sh(root, "show", "--name-only", "--format=", fix).split() == ["app.py"]
    assert f"Claude-Session: {session}" in sh(root, "log", "-1", "--format=%B", fix)
    assert sh(root, "show", "--name-only", "--format=", rec).split() == ["review-points.md"]
    msg = sh(root, "log", "-1", "--format=%B", rec)
    assert "Review-Points: review-points.md" in msg
    assert f"Implements: {repo['impl']}" in msg
    assert f"Claude-Session: {session}" in msg
    assert msg.startswith("review-points: re-review the whole PR — 1 fixed")

    # The record: no takeover note, the facts ours, the fix id resolved.
    text = (root / "review-points.md").read_text()
    assert "Taken over" not in text and "must not survive" not in text
    assert f"fixed-in: {fix[:8]}" in text and "FIX-1" not in text
    front = rr.frontmatter(text)
    assert front["implementation"] == repo["impl"][:8]
    assert front["session"] == session and front["ticket"] == "acme/app#7"
    assert "rerun-review.py" in front["reviewers"]

    # The PR comments name the commits they could not know.
    payload = json.loads((root / ".human-review" / "pr-comments.json").read_text())
    assert payload["commit_id"] == rec
    assert f"Auto-fixed** in {fix[:8]}" in payload["comments"][0]["body"]

    # What was replaced is kept; what nobody asked for is left alone.
    prev = root / ".human-review" / ".model-prev" / "review-points.md"
    assert "Taken over without a new pass" in prev.read_text()
    assert "?? notes.txt" in sh(root, "status", "--porcelain")
    ledger = json.loads((root / ".human-review" / ".review-runs.json").read_text())
    assert ledger["runs"][-1]["cost"] == 0.42

    # Pushed to the branch's own name on origin — and main, its upstream, untouched.
    assert sh(repo["origin"], "rev-parse", "feature") == rec
    assert sh(repo["origin"], "rev-parse", "main") == repo["main"]

    # And the page now reads it as a review with nothing after it: the band clears.
    found = commits_mod.detect(root, "origin/main")
    assert found["review"] == rec and found["after"] == []
    assert found["takeover"] is None
    assert found["implementation"] == repo["impl"]


def test_no_push_commits_and_leaves_the_remote_alone(repo, fake_claude):
    p = run(repo["root"], fake_claude["env"], "--no-push")
    assert p.returncode == 0, p.stderr
    assert sh(repo["origin"], "rev-parse", "feature") != sh(repo["root"], "rev-parse", "HEAD")
    assert "git push origin HEAD:feature" in p.stdout


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #

def test_a_staged_file_is_refused_before_anything_is_spent(repo, fake_claude):
    root = repo["root"]
    (root / "README.md").write_text("half-finished\n")
    sh(root, "add", "README.md")
    head = sh(root, "rev-parse", "HEAD")
    for extra in ((), ("--dry-run",)):
        p = run(root, fake_claude["env"], *extra)
        assert p.returncode == 3
        assert "README.md" in p.stderr and "staged" in p.stderr
    assert not fake_claude["log"].exists(), "the model was called over a dirty index"
    assert sh(root, "rev-parse", "HEAD") == head
    assert sh(root, "diff", "--cached", "--name-only") == "README.md"   # still staged


def test_uncommitted_edits_to_the_record_are_refused(repo, fake_claude):
    (repo["root"] / "review-points.md").write_text("mine\n")
    p = run(repo["root"], fake_claude["env"])
    assert p.returncode == 3 and "review-points.md" in p.stderr
    assert not fake_claude["log"].exists()


def test_a_change_no_fix_group_claims_is_not_committed(repo, fake_claude):
    root = repo["root"]
    head = sh(root, "rev-parse", "HEAD")
    env = dict(fake_claude["env"], FAKE_MODE="unclaimed")
    p = run(root, env)
    assert p.returncode == 6
    assert "stray.py" in p.stderr
    assert sh(root, "rev-parse", "HEAD") == head


def test_a_file_already_dirty_before_the_run_is_not_committed(repo, fake_claude):
    root = repo["root"]
    (root / "app.py").write_text("a = 1\nb = 2\nc = 4  # mine\n")      # unstaged, Victor's
    head = sh(root, "rev-parse", "HEAD")
    p = run(root, fake_claude["env"])
    assert p.returncode == 6
    assert "already dirty" in p.stderr and "app.py" in p.stderr
    assert sh(root, "rev-parse", "HEAD") == head


def test_the_push_goes_to_the_branch_s_own_name_never_its_upstream(repo):
    root = repo["root"]
    assert sh(root, "rev-parse", "--abbrev-ref", "@{u}") == "origin/main"
    remote = rr.remote_of(root, "feature")
    assert rr.push_argv(remote, "feature") == ["git", "push", "origin", "HEAD:feature"]


def test_strip_takeover_drops_only_the_note():
    text = "## Fixed\n\n### x\n- file: a:1\n\n## Taken over — today\n\n- y\n\n## Ignored\n"
    out, dropped = rr.strip_takeover(text, commits_mod.NOTE_HEADINGS)
    assert dropped and "Taken over" not in out
    assert "## Fixed" in out and "## Ignored" in out and "- y" not in out


# --------------------------------------------------------------------------- #
# the button
# --------------------------------------------------------------------------- #

build = _load("build_review_for_rerun_review", "build-review-html.py")


def test_the_review_tab_has_a_paid_press_that_runs_the_review_then_the_rebuild(tmp_path):
    build.ACTIONS.clear()
    (tmp_path / "out").mkdir()
    got = build.declare_tab_reruns(tmp_path, tmp_path / "out", HERE, ["review"])
    info = got["review"]
    assert info["ai"] and not info.get("priced")   # the probe's price is the matrix's
    entry = build.ACTIONS["__rerun_ai__:review"]
    cmd = entry["command"]
    assert cmd.index("rerun-review.py") < cmd.index("refresh-report.py")
    assert "rerun-model.py" not in cmd
    assert cmd.endswith("--steps reviewpoints,aftermath --no-serve")
    assert "--allow-model" not in cmd
    assert entry["reload"] and "commit and push" in entry["label"]
    assert build.TAB_AI["review"][0] == "review"


def test_the_review_tab_paid_press_says_what_it_costs_and_what_it_commits(tmp_path):
    (tmp_path / "out").mkdir()
    got = build.declare_tab_reruns(tmp_path, tmp_path / "out", HERE, ["review"])
    face = build.tab_rerun_html("review", "Review", got["review"])
    assert face.count("<button") == 2
    paid = face[face.index("chip-rerun-ai"):]
    assert 'data-rerun="__rerun_ai__"' in paid and 'data-tab="review"' in paid
    assert "data-tip-fmt" not in paid              # no borrowed matrix price
    assert 'data-tip="costs money. About $15' in paid
    confirm = paid.split('data-confirm="', 1)[1].split('"', 1)[0]
    for words in ("Opus", "/code-review", "whole PR", "[auto-fix]", "review-points.md",
                  "pushes", "clears the red band", "refuses", ".model-prev"):
        assert words in confirm, words
    # The free ↺ beside it still says it does *not* re-run the review.
    assert "does not re-run the review" in face


def test_the_confirmation_does_not_borrow_the_matrix_invoice():
    js = (HERE / "hrbuild" / "assets" / "rerun.js").read_text(encoding="utf-8")
    assert ".hrconfirm-last" in js and "data-tip-fmt" in js
    assert "lastP.hidden = borrowed" in js
