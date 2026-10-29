#!/usr/bin/env python3
"""The four components of the bill, read from each harness's own store.

hr-try-3 is the case this exists for: the code was written in VS Code Copilot Chat, the
review recorded by the Copilot CLI, the page built by the Copilot CLI — and the only
conversation the `$` tab could see was the Claude session that orchestrated all three,
which wrote none of it. Every store here is a fixture: a temp SQLite shaped like
`~/.copilot/session-store.db` and a temp VS Code `workspaceStorage` with an op-log chat.

Run with:  python3 -m pytest test_harness_cost.py
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness_cost as hc  # noqa: E402
import review_points_schema as schema  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

COPILOT_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, host_type TEXT,
  branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_index INTEGER,
  user_message TEXT, assistant_response TEXT, timestamp TEXT);
CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  turn_index INTEGER, agent_id TEXT, parent_tool_call_id TEXT, model TEXT, input_tokens INTEGER,
  output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  reasoning_tokens INTEGER, total_nano_aiu INTEGER, request_multiplier REAL,
  duration_ms INTEGER, initiator TEXT, created_at TEXT, copilot_usage_model TEXT);
CREATE TABLE session_files (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT);
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


def commit(repo: Path, msg: str, when: str) -> str:
    env = {**ENV, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, env=env)
    return git(repo, "rev-parse", "HEAD")


class Copilot:
    """A session store, written the way the CLI writes it."""

    def __init__(self, path: Path):
        self.path = path
        con = sqlite3.connect(path)
        con.executescript(COPILOT_SCHEMA)
        con.commit()
        con.close()

    def session(self, sid: str, cwd: Path, summary: str, branch: str = "feat") -> None:
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                    (sid, str(cwd), "o/r", "github", branch, summary, "2026-10-02T10:00:00Z",
                     "2026-10-02T10:00:00Z"))
        con.commit()
        con.close()

    def call(self, sid: str, when: str, aic: float, agent: str | None = None,
             tokens: int = 1000) -> None:
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO assistant_usage_events (session_id, agent_id, model, "
                    "input_tokens, output_tokens, total_nano_aiu, duration_ms, initiator, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, agent, "claude-sonnet-5", tokens, 10, int(aic * 1e9), 2000,
                     "sub-agent" if agent else "agent", when))
        con.commit()
        con.close()

    def wrote(self, sid: str, path: Path) -> None:
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO session_files (session_id, file_path, tool_name) "
                    "VALUES (?,?,?)", (sid, str(path), "edit"))
        con.commit()
        con.close()


def ms(iso: str) -> int:
    return int(hc.parse(iso).timestamp() * 1000)


def vscode_chat(user: Path, repo: Path, name: str, requests: list[dict],
                edits: list[Path] = ()) -> Path:
    """An op-log chat: a snapshot line, then one append per request, as VS Code writes."""
    ws = user / "workspaceStorage" / f"h-{name}"
    (ws / "chatSessions").mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": f"file://{repo}"}))
    lines = [{"kind": 0, "v": {"sessionId": name, "customTitle": f"chat {name}",
                               "requests": []}}]
    for i, r in enumerate(requests):
        resp = ([{"kind": "textEditGroup", "uri": {"fsPath": str(p)}} for p in edits]
                if i == 0 else [])
        lines.append({"kind": 2, "k": ["requests"], "v": [{
            "requestId": f"{name}-{i}", "message": {"text": r["text"]},
            "timestamp": ms(r["end"]) - 60_000, "responseTimestamp": ms(r["end"]),
            "elapsedMs": 60_000, "copilotCredits": r["aic"], "modelId": "copilot/auto",
            "promptTokens": 5000, "completionTokens": 100, "response": resp}]})
    f = ws / "chatSessions" / f"{name}.jsonl"
    f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return f


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A branch written in VS Code, reviewed and paged by the Copilot CLI, isolated from
    this machine's real stores."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COPILOT_AGENT_SESSION_ID", raising=False)
    db = tmp_path / "session-store.db"
    monkeypatch.setenv("HUMAN_REVIEW_COPILOT_DB", str(db))
    user = tmp_path / "Code" / "User"
    monkeypatch.setenv("HUMAN_REVIEW_VSCODE_USER", str(user))

    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".human-review/\n")
    (repo / "README").write_text("x\n")
    base = commit(repo, "base", "2026-10-01T20:00:00Z")
    git(repo, "checkout", "-qb", "feat")
    (repo / "app.py").write_text("def f():\n    return 1\n")
    impl = commit(repo, "impl", "2026-10-02T05:00:00Z")
    return {"repo": repo, "base": base, "impl": impl, "db": Copilot(db), "user": user}


def test_session_kind_reads_what_the_session_was_for():
    assert hc.session_kind("Run the human-review skill … after /record-review") == "human-review"
    assert hc.session_kind("You are continuing a /record-review that ran") == "record-review"
    assert hc.session_kind("/record-review\n\nContext") == "record-review"
    assert hc.session_kind("add pagination to owners") == "other"


def test_harness_names_from_prose_are_one_vocabulary():
    assert hc.normalize_harness("Copilot CLI") == hc.COPILOT_CLI
    assert hc.normalize_harness("copilot -p") == hc.COPILOT_CLI
    assert hc.normalize_harness("VS Code Copilot Chat") == hc.VSCODE
    assert hc.normalize_harness("claude-code") == hc.CLAUDE
    assert hc.normalize_harness("") == ""


def test_vscode_chat_writes_the_implementation_and_its_fork_is_not_billed_twice(world):
    repo, user = world["repo"], world["user"]
    reqs = [{"text": "add pagination", "end": "2026-10-02T04:30:00Z", "aic": 30.0},
            {"text": "go", "end": "2026-10-02T04:50:00Z", "aic": 200.0},
            # After the implementation commit, and a review prompt: neither is writing.
            {"text": "/record-review", "end": "2026-10-02T06:00:00Z", "aic": 9.0}]
    vscode_chat(user, repo, "orig", reqs, edits=[repo / "app.py"])
    # VS Code's "fork chat" copies the parent's requests, credits and timestamps included,
    # under new request ids. Counted once.
    vscode_chat(user, repo, "fork", reqs[:2], edits=[repo / "app.py"])
    # A chat that touched nothing in the change set is not an author.
    vscode_chat(user, repo, "chatter", [{"text": "hi", "end": "2026-10-02T04:40:00Z",
                                         "aic": 0.5}])
    c = hc.measure_implementation(repo, "main", hc.fork_time(repo, "main"),
                                  hc.committed_at(repo, world["impl"]))
    assert c["measured"]
    assert c["aic"] == pytest.approx(230.0)
    assert c["usd"] is None, "Copilot is in credits, never priced as Claude"
    assert {e["harness"] for e in c["entries"]} == {hc.VSCODE}


def copilot_review(world, *, ci_round: bool = True):
    db, repo = world["db"], world["repo"]
    db.session("rr", repo, "Run the record-review skill from the human-review plugin")
    db.call("rr", "2026-10-02T10:00:00Z", 5.0)
    for agent in ("a1", "a2"):
        db.call("rr", "2026-10-02T10:01:00Z", 20.0, agent)
    db.call("rr", "2026-10-02T10:05:00Z", 30.0, "a2")       # the reviewers' last call
    db.call("rr", "2026-10-02T10:06:00Z", 7.0)               # deciding, fixing
    db.call("rr", "2026-10-02T10:20:00Z", 3.0)
    if ci_round:
        db.session("ci", repo, "You are continuing a /record-review: CI is red")
        db.call("ci", "2026-10-02T11:00:00Z", 4.0)
    # Not this repository; and a page run, which is the fourth component's, not these.
    db.session("elsewhere", repo.parent, "Run the record-review skill")
    db.call("elsewhere", "2026-10-02T10:02:00Z", 99.0)
    db.session("page", repo, "Run the human-review skill from the human-review plugin")
    db.call("page", "2026-10-02T12:00:00Z", 40.0)
    db.call("page", "2026-10-02T12:10:00Z", 60.0, "matrix")


def test_a_copilot_review_is_split_at_the_reviewers_last_call(world):
    copilot_review(world)
    review, fixes = hc.measure_review_fixes(world["repo"], hc.COPILOT_CLI, [],
                                            "2026-10-02T09:59:00Z", None,
                                            "2026-10-02T11:30:00Z", "feat")
    assert review["aic"] == pytest.approx(5 + 20 + 20 + 30)
    # Half-open: the reviewers' last call is the review's and not the fixes' as well.
    assert fixes["aic"] == pytest.approx(7 + 3 + 4), "the CI round's session is a fix too"
    assert {e["session"] for e in fixes["entries"]} == {"rr", "ci"}
    assert "elsewhere" not in {e["session"] for e in review["entries"] + fixes["entries"]}


def test_a_branch_recorded_before_the_record_is_derived_from_the_stores(world):
    """hr-try-3's shape: no review-cost.json, a front-matter that says `Copilot CLI`."""
    repo = world["repo"]
    copilot_review(world)
    vscode_chat(world["user"], repo, "impl", [{"text": "write it", "end":
                                               "2026-10-02T04:30:00Z", "aic": 100.0}],
                edits=[repo / "app.py"])
    (repo / "review-points.md").write_text(
        f"---\nimplementation: {world['impl']}\nharness: Copilot CLI\n---\n## Fixed\n")
    review_sha = commit(repo, "[auto-fix] x", "2026-10-02T11:01:00Z")
    hr = repo / ".human-review"
    hr.mkdir()
    (hr / ".started").write_text("2026-10-02T12:00:30+00:00\n")
    (hr / ".session").write_text("\n")                       # blank: not a Claude run
    (hr / ".steps.json").write_text(json.dumps([
        {"tabs": ["guide"], "label": "assemble", "start": "2026-10-02T12:05:00+00:00",
         "end": "2026-10-02T12:15:00+00:00"}]))
    doc = hc.components(repo, "main", hr, None,
                        {"implementation": world["impl"], "review": review_sha})
    rows = {r["key"]: r for r in doc["rows"]}
    assert [r["key"] for r in doc["rows"]] == ["implementation", "review", "autofix", "guide"]
    assert rows["implementation"]["aic"] == pytest.approx(100.0)
    assert rows["review"]["aic"] == pytest.approx(75.0)
    assert rows["autofix"]["aic"] == pytest.approx(14.0)
    assert rows["guide"]["aic"] == pytest.approx(100.0), "the page run and its subagent"
    assert all(r["source"] == "derived" for r in doc["rows"])
    assert doc["usdEquivalent"] == pytest.approx((100 + 75 + 14 + 100) * hc.AIC_USD)
    assert doc["wallclock"]["seconds"] == pytest.approx(14 * 60 + 30)


def test_the_record_is_what_finish_commits_and_it_passes_its_schema(world):
    copilot_review(world, ci_round=False)
    state = {"base": world["base"], "implementation": world["impl"],
             "reviewStartedAt": "2026-10-02T09:59:00Z",
             "reviewersDoneAt": "2026-10-02T10:05:30Z"}
    doc = hc.record(world["repo"], "main", state, hc.COPILOT_CLI, at="2026-10-02T10:30:00Z")
    assert schema.cost_problems(doc) == []
    comps = {c["key"]: c for c in doc["components"]}
    assert comps["review"]["aic"] == pytest.approx(75.0)
    assert comps["autofix"]["aic"] == pytest.approx(10.0)
    assert not comps["implementation"]["measured"]
    assert "edited these files" in comps["implementation"]["reason"], \
        "unmeasured says why, never $0"


def test_a_review_without_a_start_stamp_is_unmeasured_not_the_whole_session(world):
    copilot_review(world)
    doc = hc.record(world["repo"], "main", {"base": world["base"],
                                            "implementation": world["impl"]},
                    hc.COPILOT_CLI, at="2026-10-02T10:30:00Z")
    comps = {c["key"]: c for c in doc["components"]}
    assert not comps["review"]["measured"] and "start" in comps["review"]["reason"]
    assert schema.cost_problems(doc) == []


def test_the_run_records_itself_once_and_a_refresh_adds_time_never_money(world):
    copilot_review(world)
    hr = world["repo"] / ".human-review"
    hr.mkdir()
    (hr / ".started").write_text("2026-10-02T11:59:00+00:00\n")
    (hr / ".session").write_text("")
    (hr / ".model-runs.json").write_text(json.dumps({"runs": [
        {"when": "2026-10-02T12:08:00+00:00", "model": "sonnet", "cost": 0.42,
         "seconds": 90},
        {"when": "2026-09-01T00:00:00+00:00", "model": "sonnet", "cost": 9.0,
         "seconds": 90}]}))
    first = hc.record_run(world["repo"], hr, "copilot-cli", at="2026-10-02T12:20:00Z")
    g = first["guide"]
    assert g["aic"] == pytest.approx(100.0)
    assert g["usd"] == pytest.approx(0.42), "the mapping run inside the window, not last month's"
    assert first["wallclock"]["seconds"] == 21 * 60
    hc.note_refresh(hr, 12.5, "static")
    again = hc.record_run(world["repo"], hr, "copilot-cli", at="2026-10-02T13:00:00Z")
    assert again["guide"] == g, "a second call for the same run does not re-bill it"
    assert [r["seconds"] for r in again["refreshes"]] == [12.5]


def test_the_tab_leads_with_four_rows_and_says_it_adds_two_kinds_of_price():
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    comp = {"rows": [
        hc.component("implementation", [hc.entry(hc.VSCODE, "v1", "chat", aic=404.48)]),
        hc.component("review", [hc.entry(hc.CLAUDE, "c1abcdef", "reviewers", usd=5.98)]),
        hc.component("autofix", [], "the reviewers' end was not stamped"),
        hc.component("guide", [hc.entry(hc.COPILOT_CLI, "p1", "page", aic=424.8)]),
    ], "usd": 5.98, "aic": 829.3, "usdEquivalent": 14.27, "aicUsd": 0.01,
        "unmeasured": ["autofix"], "wallclock": {"seconds": 1412, "modelSeconds": 891},
        "refreshSeconds": 30}
    out = cost.components_html(comp)
    assert out.count("data-component=") == 4
    assert "404 AIC" in out and "≈ $4.04 billed" in out
    assert "unmeasured — the reviewers&#x27; end was not stamped" in out
    # The two kinds of price, under the total (984b8c1 dropped the "added" caption).
    assert ("Copilot 829 AIC = $8.29 at GitHub&#x27;s $0.01 per AI credit + Claude $5.98 at "
            "API list price") in out
    assert "took 24 min, of which model 15 min; plus 30 s of refreshes, no model" in out
    assert cost.cost_pill_label({"components": comp}) == "$14?"
    assert cost.components_html({"rows": [hc.component("guide", [], "x")]}) == ""


# --------------------------------------------------------------------------- Claude, headless

def _turn(f: Path, ts: str, mid: str, model: str = "claude-opus-5-5", read: int = 1_000_000,
          out: int = 0, branch: str = "feat", edit: Path | None = None) -> None:
    content = ([{"type": "tool_use", "name": "Edit", "input": {"file_path": str(edit)}}]
               if edit else [{"type": "text", "text": "ok"}])
    rec = {"type": "assistant", "timestamp": ts, "gitBranch": branch,
           "message": {"id": mid, "model": model, "content": content,
                       "usage": {"input_tokens": 0, "output_tokens": out,
                                 "cache_read_input_tokens": read}}}
    with f.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _prompt(f: Path, ts: str, text: str, branch: str = "feat") -> None:
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": ts, "gitBranch": branch,
                             "message": {"role": "user", "content": text}}) + "\n")


@pytest.fixture
def claude_world(tmp_path, monkeypatch):
    """Eval run 8's shape, headless: `claude -p /openspec-apply-change`, then
    `claude -p --resume … /record-review` in the same session — four Sonnet reviewers, a
    fix, `ci --push` waiting on CI in the background — and no review-cost.json, because
    finish refused it. Beside it, the previous eval run's session: the same files edited
    on a sibling branch forked from the same base. Opus 5.5 reads cache at $0.20/M, so
    every 1M-token turn below is $0.20."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(hc.rc(), "PROJECTS", projects)
    monkeypatch.setenv("HUMAN_REVIEW_COPILOT_DB", str(tmp_path / "none.db"))
    monkeypatch.setenv("HUMAN_REVIEW_VSCODE_USER", str(tmp_path / "no-vscode"))
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".human-review/\n")
    (repo / "README").write_text("x\n")
    base = commit(repo, "base", "2026-10-02T09:00:00Z")
    git(repo, "checkout", "-qb", "other")                     # the previous eval run
    (repo / "app.py").write_text("def f():\n    return 0\n")
    commit(repo, "impl, run 7", "2026-10-02T09:40:00Z")
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-qb", "feat")
    sid, old = "s-impl", "s-run7"
    (repo / "app.py").write_text("def f():\n    return 1\n")
    impl = commit(repo, f"impl\n\nClaude-Session: {sid}", "2026-10-02T10:30:00Z")
    (repo / "review-points.md").write_text(
        f"---\nimplementation: {impl}\nharness: claude-code\nsession: {sid}\n---\n## Fixed\n")
    review = commit(repo, f"[auto-fix] x\n\nClaude-Session: {sid}", "2026-10-02T10:53:00Z")

    slug = projects / str(repo).replace("/", "-")
    slug.mkdir(parents=True)
    f = slug / f"{sid}.jsonl"
    _prompt(f, "2026-10-02T10:00:00Z", "/openspec-apply-change owners")
    _turn(f, "2026-10-02T10:00:10Z", "a1")                    # reading, before any edit
    _turn(f, "2026-10-02T10:05:00Z", "a2", edit=repo / "app.py")
    _turn(f, "2026-10-02T10:20:00Z", "a3")                    # the tests, after the edit
    _prompt(f, "2026-10-02T10:40:00Z", "/record-review")
    _turn(f, "2026-10-02T10:41:00Z", "a4")                    # before prepare: writing
    _turn(f, "2026-10-02T10:46:00Z", "a5")                    # briefing the reviewers
    _turn(f, "2026-10-02T10:49:59Z", "a6", read=1_000_000, out=500)
    _turn(f, "2026-10-02T10:50:05Z", "a6", read=1_000_000, out=1000)   # same turn, streamed
    _turn(f, "2026-10-02T10:52:00Z", "a7")                    # fixing
    _turn(f, "2026-10-02T10:55:30Z", "a8")                    # `ci --push`, waits in background
    _prompt(f, "2026-10-02T11:05:00Z", "<task-notification>\nCI green</task-notification>")
    _turn(f, "2026-10-02T11:05:10Z", "a9")                    # reading the verdict
    _prompt(f, "2026-10-02T11:30:00Z", "now something else")
    _turn(f, "2026-10-02T11:30:10Z", "a10")
    agents = slug / sid / "subagents"
    agents.mkdir(parents=True)
    _turn(agents / "agent-r1.jsonl", "2026-10-02T10:47:00Z", "r1",
          model="claude-sonnet-5-5", read=0, out=10_000)      # $0.10
    o = slug / f"{old}.jsonl"
    _prompt(o, "2026-10-02T09:30:00Z", "/openspec-apply-change owners", branch="other")
    _turn(o, "2026-10-02T09:35:00Z", "o1", branch="other", edit=repo / "app.py")
    _turn(o, "2026-10-02T10:10:00Z", "o2", branch="other", edit=repo / "app.py")

    hr = repo / ".human-review" / "review"
    hr.mkdir(parents=True)
    state = {"base": base, "implementation": impl, "session": sid, "sessions": [sid],
             "harness": "claude-code", "reviewStartedAt": "2026-10-02T10:45:00+00:00",
             "reviewersDoneAt": "2026-10-02T10:50:00+00:00",
             "lastCiAt": "2026-10-02T10:55:00+00:00", "reviewCommit": review,
             "finishes": ["2026-10-02T10:53:00+00:00"]}
    (hr / "state.json").write_text(json.dumps(state))
    return {"repo": repo, "base": base, "impl": impl, "review": review, "sid": sid,
            "state": state}


def test_a_headless_claude_run_without_a_record_is_measured_from_its_transcripts(claude_world):
    """Eval run 8: `review-cost.json` missing, no `phases.json`, `.session` pinning the
    /human-review run — and implementation, review and auto-fixes all read "session-cost.py
    did not date it" while every stamp and the transcript were on disk."""
    w = claude_world
    doc = hc.derive(w["repo"], "main", {"measured": False, "rows": [],
                                        "reason": "no phases.json"},
                    {"implementation": w["impl"], "review": w["review"], "session": w["sid"]})
    rows = {c["key"]: c for c in doc["components"]}
    assert all(rows[k]["measured"] for k in ("implementation", "review", "autofix"))
    impl = rows["implementation"]
    # Fork → prepare, the whole session: the turn before the first edit and the two after
    # the last are writing the code. The sibling branch's session (same files) is not.
    assert impl["usd"] == pytest.approx(0.80)
    assert [e["session"] for e in impl["entries"]] == [w["sid"]]
    # Prepare → reviewers done, the Sonnet reviewer inside; the turn that streamed across
    # the stamp is billed once, where it started.
    assert rows["review"]["usd"] == pytest.approx(0.20 + 0.22 + 0.10)
    assert rows["review"]["entries"][0]["subagents"] == 1
    # To the end of the turn that ran `ci` — past the background task's notification, not
    # past the next real prompt.
    assert rows["autofix"]["usd"] == pytest.approx(0.60)
    assert rows["autofix"]["window"][1] == "2026-10-02T11:05:10+00:00"
    assert all(c["derivedBecause"].startswith("record-review.py finish did not write")
               for c in doc["components"])


def test_what_finish_would_have_committed_passes_its_schema_with_the_reviewer_count(claude_world):
    """Run 8's finish printed `review-cost.json: not written — unknown key subagents`: the
    count the review chip needs was added to the entry and never to the schema."""
    w = claude_world
    rec = hc.record(w["repo"], w["base"], w["state"], hc.CLAUDE, at="2026-10-02T10:53:00Z")
    assert schema.cost_problems(rec) == []
    review = next(c for c in rec["components"] if c["key"] == "review")
    assert review["entries"][0]["subagents"] == 1


def test_a_turn_runs_to_the_next_real_prompt_not_to_a_harness_notification(claude_world):
    w = claude_world
    a, b = hc.claude_turn_bounds(w["sid"], "2026-10-02T10:55:00Z")
    assert hc.iso(a) == "2026-10-02T10:40:00+00:00"
    assert hc.iso(b) == "2026-10-02T11:05:10+00:00"


def test_an_unmeasured_claude_window_names_what_was_missing(claude_world):
    w = claude_world
    review, fixes = hc.measure_review_fixes(w["repo"], hc.CLAUDE, ["gone-session"],
                                            "2026-10-02T10:45:00Z", "2026-10-02T10:50:00Z",
                                            "2026-10-02T10:53:00Z")
    assert not review["measured"] and "gone-ses" in review["reason"]
    assert "not under" in review["reason"], "the transcript is what is missing"
    review, _ = hc.measure_review_fixes(w["repo"], hc.CLAUDE, [], "2026-10-02T10:45:00Z",
                                        "2026-10-02T10:50:00Z", "2026-10-02T10:53:00Z")
    assert "state.json" in review["reason"] and "record-review.py finish" in review["reason"]


def test_without_a_prepare_stamp_the_rows_say_which_file_to_produce(claude_world):
    """No reviewStartedAt and no phases.json: not "session-cost.py did not date it", but
    which record is missing and what to run."""
    w = claude_world
    st = w["repo"] / ".human-review" / "review" / "state.json"
    st.write_text(json.dumps({k: v for k, v in w["state"].items()
                              if k != "reviewStartedAt"}))
    doc = hc.derive(w["repo"], "main", {"measured": False, "rows": [], "reason":
                                        "no .human-review/phases.json — run session-cost.py "
                                        "to date the phases"}, {})
    for c in doc["components"]:
        assert not c["measured"]
        assert "review-cost.json" in c["reason"] and "reviewStartedAt" in c["reason"]
        assert "run session-cost.py" in c["reason"]


def test_a_partial_bill_says_which_share_is_missing_and_a_whole_one_has_no_question_mark():
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    whole = {"rows": [hc.component(k, [hc.entry(hc.CLAUDE, "s", "x", usd=1.0)])
                      for k, _ in hc.COMPONENTS],
             "usd": 4.0, "aic": 0.0, "usdEquivalent": 4.0, "aicUsd": 0.01, "unmeasured": []}
    assert cost.cost_pill_label({"components": whole}) == "$4"
    assert "costpartial" not in cost.components_html(whole)
    part = {**whole, "rows": [hc.component("implementation", [], "no transcript"),
                              *whole["rows"][1:]], "usdEquivalent": 3.0,
            "unmeasured": ["implementation"]}
    title = cost.cost_pill_title({"components": part})
    assert "3 of 4 parts" in title and "1 of 4 missing" in title and "no transcript" in title
    assert "Partial bill: 1 of 4 parts not measured — implementation" in \
        cost.components_html(part)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_ci_round_after_the_record_extends_the_autofix_window(tmp_path, monkeypatch):
    """hr-try-4: finish recorded at 20:23, a second CI round was fixed and committed with
    no second finish — 207.7 AIC recorded of 252.6 spent."""
    (tmp_path / ".human-review" / "review").mkdir(parents=True)
    (tmp_path / ".human-review" / "review" / "state.json").write_text(json.dumps(
        {"reviewStartedAt": "2026-10-02T19:40:00+00:00",
         "reviewersDoneAt": "2026-10-02T19:50:00+00:00",
         "lastCiAt": "2026-10-02T21:00:00+00:00"}))
    rec = {"schema": hc.RECORD_SCHEMA, "harness": "copilot-cli",
           "recordedAt": "2026-10-02T20:23:00+00:00", "rounds": ["2026-10-02T20:23:00+00:00"],
           "components": [{"key": "implementation"}, {"key": "review"},
                          {"key": "autofix", "aic": 192.7}]}
    seen = {}

    def fake_record(root, base, state, harness, at=None):
        seen["at"] = at
        return {"components": [{"key": "implementation"}, {"key": "review"},
                               {"key": "autofix", "aic": 237.6}]}
    monkeypatch.setattr(hc, "record", fake_record)
    monkeypatch.setattr(hc, "git", lambda *a: "")
    out = hc.extend_to_last_round(tmp_path, "main", rec)
    assert hc.iso(seen["at"]).startswith("2026-10-02T21:00")
    assert out["components"][2]["aic"] == 237.6
    assert "extended" in out["components"][2]["source"]
    rec["recordedAt"] = "2026-10-02T21:30:00+00:00"
    assert hc.extend_to_last_round(tmp_path, "main", rec) is rec


def test_a_copilot_guide_recorded_mid_session_is_completed_at_build(tmp_path, monkeypatch):
    """hr-try-4: report-cost.py recorded 198.3 of the 296.7 AIC — the session went on
    calling the model through the build and the close."""
    import datetime as dt
    rep = {"recordedAt": "2026-10-02T20:40:22+00:00", "harness": hc.COPILOT_CLI,
           "guide": {"harnesses": [hc.COPILOT_CLI], "aic": 198.3,
                     "entries": [{"session": "511bbe15"}]},
           "wallclock": {"seconds": 600}}
    last = dt.datetime(2026, 10, 2, 20, 43, 42, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(hc, "git", lambda *a: "hr-try-4")
    monkeypatch.setattr(hc, "copilot_sessions", lambda root, branch: [
        {"id": "511bbe15", "kind": "human-review", "first": last, "last": last}])
    monkeypatch.setattr(hc, "measure_guide", lambda root, review, h, end=None: (
        {"harnesses": [hc.COPILOT_CLI], "aic": 296.7, "entries": []}, {"seconds": 800}))
    guide, wall = hc.complete_guide(tmp_path, tmp_path, rep)
    assert guide["aic"] == 296.7 and "completed at build" in guide["source"]
    assert wall["seconds"] == 800
    rep["harness"], rep["guide"]["harnesses"] = hc.CLAUDE, [hc.CLAUDE]
    assert hc.complete_guide(tmp_path, tmp_path, rep)[0]["aic"] == 198.3


def test_a_copilot_review_records_which_models_did_the_reviewing(world):
    """The review chip said `Reviewed by Opus 5` — the first model on the run's whole
    bill, the implementation's — over four Sonnet reviewers. The record now keeps the
    models of the agents the review forked, and the chip names those."""
    copilot_review(world, ci_round=False)
    state = {"base": world["base"], "implementation": world["impl"],
             "reviewStartedAt": "2026-10-02T09:59:00Z",
             "reviewersDoneAt": "2026-10-02T10:05:30Z"}
    doc = hc.record(world["repo"], "main", state, hc.COPILOT_CLI, at="2026-10-02T10:30:00Z")
    assert schema.cost_problems(doc) == []
    review = {c["key"]: c for c in doc["components"]}["review"]
    assert review["entries"][0]["subagentModels"] == ["Sonnet 5"]
    (world["repo"] / hc.RECORD_FILE).write_text(json.dumps(doc))
    assert hc.reviewer_models(world["repo"]) == ["Sonnet 5"]


def _claude_session(projects: Path, sid: str, main_model: str, agents: dict) -> None:
    """A transcript under `projects`, and one subagent transcript per `agents` entry
    (`id -> (model, first timestamp)`), the way Claude Code lays them out."""
    folder = projects / "-repo"
    (folder / sid / "subagents").mkdir(parents=True)

    def turn(model, when, mid):
        return json.dumps({"type": "assistant", "timestamp": when, "message": {
            "id": mid, "model": model, "usage": {"input_tokens": 10, "output_tokens": 1}}})

    (folder / f"{sid}.jsonl").write_text(turn(main_model, "2026-10-02T21:49:30Z", "m") + "\n")
    for aid, (model, when) in agents.items():
        (folder / sid / "subagents" / f"agent-{aid}.jsonl").write_text(
            turn(model, when, aid) + "\n")


def test_an_older_record_is_answered_from_the_reviewers_own_transcripts(tmp_path, monkeypatch):
    """Eval run 5's record predates `subagentModels`: its review row says `Opus 5 80% /
    Sonnet 5 20%`, the orchestrator and the reviewers mixed. The agents that started in the
    review window say which of the two reviewed — and with the version the old label lost."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(hc.rc(), "PROJECTS", projects)
    _claude_session(projects, "s1", "claude-opus-5-5",
                    {"aaaa": ("claude-sonnet-5-5", "2026-10-02T21:49:40Z"),
                     "later": ("claude-haiku-4-5", "2026-10-02T22:05:00Z")})
    repo = tmp_path / "repo"
    (repo / ".human-review" / "review").mkdir(parents=True)
    (repo / ".human-review" / "review" / "state.json").write_text(json.dumps({
        "session": "s1", "sessions": ["s1"], "reviewStartedAt": "2026-10-02T21:49:19Z",
        "reviewersDoneAt": "2026-10-02T21:50:37Z"}))
    entry = {"harness": hc.CLAUDE, "session": "s1", "models": {"Opus 5": 80, "Sonnet 5": 20}}
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "review", "entries": [entry]}]}))
    assert hc.reviewer_models(repo) == ["Sonnet 5.5"], \
        "an agent the page build forked later is not a reviewer"
    row = hc.relabel({"entries": [dict(entry)]})
    assert row["entries"][0]["models"] == {"Opus 5.5": 80, "Sonnet 5.5": 20}


def test_with_no_transcript_the_front_matter_names_the_reviewers(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.rc(), "PROJECTS", tmp_path / "nothing")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "review-points.md").write_text(
        "---\nreviewers: 4 read-only Sonnet subagents (correctness, tests)\n---\n\n## Fixed\n")
    entry = {"harness": hc.CLAUDE, "session": "gone", "models": {"Opus 5": 80, "Sonnet 5": 20}}
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "review", "entries": [entry]}]}))
    assert hc.reviewer_models(repo) == ["Sonnet 5"]
    assert hc.relabel({"entries": [dict(entry)]})["entries"][0]["models"] == entry["models"], \
        "with the transcript gone, a recorded name stays what it was recorded as"
    (repo / "review-points.md").unlink()
    assert hc.reviewer_models(repo) == ["Opus 5", "Sonnet 5"], \
        "nothing names the reviewers: the review row's own models, largest first"


def test_a_model_step_carries_its_program_and_the_tokens_it_recorded(tmp_path):
    """Eval run 6 billed the mapping `$0.16` beside 0 tokens, and nothing on the entry
    said which tab it fed. The program rides on the entry; the tokens come from the run."""
    (tmp_path / ".model-runs.json").write_text(json.dumps({"runs": [
        {"when": "2026-10-03T00:03:32+00:00", "model": "haiku", "cost": 0.16,
         "seconds": 99.8, "tokens": 46500, "models": {"claude-haiku-4-5-20251001": 46500}},
        {"when": "2026-10-03T00:03:40+00:00", "model": "haiku", "cost": 0.1,
         "seconds": 10}]}))
    lo, hi = hc.parse("2026-10-02T23:50:53Z"), hc.parse("2026-10-03T00:04:00Z")
    new, old = hc._ledger_runs(tmp_path, lo, hi)
    assert new["program"] == "rerun-model.py" and "rerun-model.py" in new["what"]
    assert new["tokens"] == 46500 and new["models"] == {"Haiku 4.5": 46500}
    assert old["tokens"] == 0 and old["models"] == {"haiku": 0}, \
        "an older run says which model ran, and claims no count it never recorded"


def test_the_review_chip_names_the_reviewers_and_who_orchestrated_them(tmp_path, monkeypatch):
    """`Reviewed by Sonnet 5.5` over a review row reading `Opus 5.5 73% / Sonnet 5.5 27%`:
    four Sonnet subagents read the diff, an Opus session briefed them. Both are said."""
    monkeypatch.setattr(hc.rc(), "PROJECTS", tmp_path / "nothing")
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = {"harness": hc.CLAUDE, "session": "gone", "subagentModels": ["Sonnet 5.5"],
             "models": {"Opus 5.5": 73, "Sonnet 5.5": 27}}
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "review", "entries": [entry]}]}))
    (repo / "review-points.md").write_text(
        "---\nreviewers: 4 read-only Sonnet subagents (correctness, tests)\n---\n")
    assert hc.review_line(repo) == \
        "Reviewers: Sonnet 5.5 (4 subagents), orchestrated by Opus 5.5"
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "review", "entries": [{**entry, "subagents": 2}]}]}))
    assert hc.review_line(repo).startswith("Reviewers: Sonnet 5.5 (2 subagents)"), \
        "a recorded count beats the prose"
    (repo / "review-points.md").unlink()
    inline = {"harness": hc.CLAUDE, "session": "gone", "models": {"Opus 5.5": 10}}
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "review", "entries": [inline]}]}))
    assert hc.review_line(repo) == "Reviewed by Opus 5.5", "an inline review had no agents"


def test_the_four_rows_explain_only_the_prices_on_screen():
    """Run 6 was Claude end to end and its caption still explained Copilot AI credits.
    And an entry's price is glued to its words: `$0.16` had wrapped onto its own line."""
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    comp = {"rows": [
        hc.component("implementation", [hc.entry(hc.CLAUDE, "c1abcdef", "edited", usd=21.66)]),
        hc.component("guide", [hc.entry(hc.CLAUDE, "claude -p (.model-runs.json)",
                                        "requirements↔tests mapping (rerun-model.py)",
                                        tokens=46500, usd=0.16,
                                        models={"Haiku 4.5": 46500})]),
    ], "usd": 21.82, "aic": 0.0}
    out = cost.components_html(comp)
    assert "Copilot" not in out and "AI credit" not in out
    # Claude alone: one kind of price, so no line explaining two (a8911a0, Victor: the
    # total's own line already says it is the API list price).
    assert "per AI credit" not in out
    assert out.count("$0.16") == 1, "a row's only entry is priced once, in the cost column"
    assert "claude -p on Haiku 4.5" in out and ".model-runs.json" not in out
    assert "film script" not in out, "the hint names no step the run did not take"


# --------------------------------------------------------------------------- eval run 10

def test_no_turn_is_running_while_the_conversation_is_idle_or_over(claude_world):
    """The reference page: `.session` named the conversation that wrote, reviewed and fixed
    the change (one prompt, over by 18:40), and the run started at 19:59 in another one. The
    turn "running at 19:59" came back as that whole conversation, and "this guide" billed
    its $38.15 on top of the three rows that had already paid for it: $75.23 for $37.48."""
    w = claude_world
    # Between the CI verdict (11:05:10) and the next prompt (11:30): idle.
    assert hc.claude_turn_bounds(w["sid"], "2026-10-02T11:20:00Z") == (None, None)
    # After the transcript's last record: over.
    assert hc.claude_turn_bounds(w["sid"], "2026-10-02T13:00:00Z") == (None, None)
    # Inside a turn the answer is unchanged.
    a, b = hc.claude_turn_bounds(w["sid"], "2026-10-02T11:30:05Z")
    assert hc.iso(a) == "2026-10-02T11:30:00+00:00" and hc.iso(b) == "2026-10-02T11:30:10+00:00"


def test_a_guide_pinned_to_a_finished_conversation_bills_none_of_it(claude_world):
    w = claude_world
    review = w["repo"] / ".human-review"
    (review / ".session").write_text(w["sid"])
    (review / ".started").write_text("2026-10-02T13:00:00+00:00")
    guide, _ = hc.measure_guide(w["repo"], review, end="2026-10-02T13:20:00Z")
    assert not guide["measured"], "the run's own window holds no turn of that conversation"


def test_the_guide_row_never_bills_a_turn_the_rows_above_already_billed(claude_world):
    """The invariant behind the fix above: the four rows are summed, so they never overlap.
    A guide entry for a session rows 1–3 bill starts where their last window ends."""
    w = claude_world
    paid = [hc.component("autofix", [hc.entry(hc.CLAUDE, w["sid"], "fixing",
                                              ("2026-10-02T10:50:00Z", "2026-10-02T11:05:10Z"),
                                              usd=0.6)])]
    whole = hc.component("guide", [hc.claude_entry(w["sid"], "2026-10-02T10:00:00Z",
                                                   "2026-10-02T11:31:00Z", "the run")])
    kept = hc.without_paid_turns(whole, paid)
    assert kept["usd"] == pytest.approx(0.20), "only a10, the turn after the fixes"
    assert kept["entries"][0]["window"][0] == "2026-10-02T11:05:11+00:00"
    assert "less the turns the rows above already billed" in kept["source"]
    other = hc.component("guide", [hc.entry(hc.CLAUDE, "s-other", "the run",
                                            ("2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z"),
                                            usd=1.0)])
    assert hc.without_paid_turns(other, paid) is other, "another conversation is untouched"


def test_an_extended_autofix_row_keeps_what_the_record_said(tmp_path, monkeypatch):
    """Eval run 10: $2.26 / 7.7M on the page, $1.97 / 6.5M in the committed record, and
    nothing on the row to say why. The extension keeps the recorded figure beside it."""
    (tmp_path / ".human-review" / "review").mkdir(parents=True)
    (tmp_path / ".human-review" / "review" / "state.json").write_text(json.dumps(
        {"reviewStartedAt": "2026-10-03T05:32:57+00:00", "lastCiAt": "2026-10-03T05:50:09+00:00"}))
    recorded = {"key": "autofix", "measured": True, "usd": 1.9744, "aic": None,
                "tokens": 6_531_866, "window": ["2026-10-03T05:34:23+00:00",
                                                "2026-10-03T05:42:55+00:00"]}
    rec = {"schema": hc.RECORD_SCHEMA, "harness": "claude-code",
           "recordedAt": "2026-10-03T05:42:55+00:00", "rounds": [],
           "components": [{"key": "implementation"}, {"key": "review"}, recorded]}
    monkeypatch.setattr(hc, "record", lambda *a, **k: {"components": [
        {"key": "implementation"}, {"key": "review"},
        {"key": "autofix", "measured": True, "usd": 2.2583, "tokens": 7_666_958}]})
    monkeypatch.setattr(hc, "git", lambda *a: "")
    row = hc.extend_to_last_round(tmp_path, "main", rec)["components"][2]
    assert row["recorded"] == {"usd": 1.9744, "aic": None, "tokens": 6_531_866,
                               "window": recorded["window"]}
    assert row["extendedTo"].startswith("2026-10-03T05:50:09")
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    out = cost.components_html({"rows": [{**row, "label": "auto-fixes", "entries": []}],
                                "usd": 2.2583, "aic": 0.0})
    # Eval run 11: on the label's hover, not as a visible line under it.
    # 984b8c1 capitalises a row's name ("Auto-fixes").
    tip = re.search(r'<td><span data-tip="([^"]*)">Auto-fixes</span>', out)
    assert tip, out
    said = html.unescape(tip.group(1))
    assert said.startswith("extended to the last CI round")
    assert "+$0.28 / +1.1M tok since the committed record, which says $1.97 / 6.5M" in said
    visible = re.sub(r'data-tip="[^"]*"', "", out)
    assert "extended to the last CI round" not in visible


def test_a_step_run_twice_is_one_row_of_wallclock_the_last_run(tmp_path):
    """Eval run 11 recorded the Demo film twice (the first attempt failed), and
    `wallclock.steps` listed 'feature recording' twice: stepSeconds 1109 > a 934 s run."""
    hr = tmp_path / ".human-review"
    hr.mkdir()
    (hr / ".steps.json").write_text(json.dumps([
        {"tabs": ["behaviour"], "label": "feature recording",
         "start": "2026-10-03T08:50:43+00:00", "end": "2026-10-03T08:55:50+00:00"},
        {"tabs": ["dsaudit"], "label": "design-system audit",
         "start": "2026-10-03T08:55:50+00:00", "end": "2026-10-03T08:56:30+00:00"},
        {"tabs": ["behaviour"], "label": "feature recording",
         "start": "2026-10-03T08:57:06+00:00", "end": "2026-10-03T09:01:00+00:00"},
        {"tabs": ["guide"], "label": "assemble",
         "start": "2026-10-03T09:01:05+00:00", "end": "2026-10-03T09:03:08+00:00"}]))
    wall = hc.wallclock(hr, "2026-10-03T08:47:35+00:00", "2026-10-03T09:03:09+00:00")
    film = [s for s in wall["steps"] if s["label"] == "feature recording"]
    assert film == [{"label": "feature recording", "tabs": ["behaviour"], "seconds": 234}]
    assert wall["stepSeconds"] == 234 + 40 + 123
    assert wall["stepSeconds"] <= wall["seconds"]


# --------------------------------------------------------------------------- time per phase

def test_a_phase_takes_its_turns_not_the_pauses_between_them(claude_world):
    """Busy time: each prompt to the last record before the next one. The 20 minutes nobody
    typed (10:20 → 10:40) are not in it; the CI wait the agent itself sat through, woken by
    a task-notification, is (10:55:30 → 11:05:10)."""
    w = claude_world
    e = hc.entry(hc.CLAUDE, w["sid"], "x", ("2026-10-02T10:00:00Z", "2026-10-02T11:20:00Z"))
    assert hc.busy_seconds([e]) == 20 * 60 + (25 * 60 + 10)
    step = hc.entry(hc.CLAUDE, "claude -p (.model-runs.json)", "mapping",
                    ("2026-10-02T11:10:00Z", "2026-10-02T11:11:30Z"))
    assert hc.busy_seconds([e, step]) == 20 * 60 + (25 * 60 + 10) + 90, \
        "a model step outside the turns adds its own window"
    inside = hc.entry(hc.CLAUDE, "claude -p (.model-runs.json)", "mapping",
                      ("2026-10-02T10:45:00Z", "2026-10-02T10:46:00Z"))
    assert hc.busy_seconds([e, inside]) == 20 * 60 + (25 * 60 + 10), \
        "one that ran inside a turn is not counted twice"
    gone = hc.entry(hc.CLAUDE, "no-such-session", "x",
                    ("2026-10-02T10:00:00Z", "2026-10-02T11:20:00Z"))
    assert hc.busy_seconds([e, gone]) is None, "a phase partly untimed is untimed, not less"


def test_the_four_rows_carry_a_time_column_and_a_dash_when_untimed():
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    impl = hc.component("implementation", [hc.entry(hc.CLAUDE, "c1abcdef", "w", usd=20.0)])
    impl["busySeconds"], impl["modelSeconds"] = 2 * 3600 + 5 * 60, 1800
    review = hc.component("review", [hc.entry(hc.CLAUDE, "c1abcdef", "r", usd=1.5)])
    review["busySeconds"] = 67
    fixes = hc.component("autofix", [hc.entry(hc.CLAUDE, "c1abcdef", "f", usd=2.0)])
    fixes["busySeconds"] = None
    guide = hc.component("guide", [hc.entry(hc.CLAUDE, "g1", "run", usd=2.4)])
    guide["busySeconds"] = 600
    comp = {"rows": [impl, review, fixes, guide], "usd": 25.9, "aic": 0.0,
            "busySeconds": None, "wallclock": {"seconds": 720, "modelSeconds": 240},
            "refreshSeconds": 240}
    out = cost.components_html(comp)
    assert '<th scope="col"><span data-tip-html="' in out and '">time</span>' in out
    cells = re.findall(r'<td>(?:<span class="costtime"[^>]*>)?([^<]*)(?:</span>)?</td>'
                       r'<td><span class="costmoney"', out)
    assert cells == ["2 h 05 min", "1 min", "—", "10 min", "—"], cells
    assert ("model thinking and writing 30 min</li><li>waiting for tools 1 h 35 min"
            in html.unescape(out)), "the split is the time's hover"
    assert "costsplit" not in out, "not a line of its own under every time"
    assert "took 12 min" not in out, "the run's time is in the column, not twice"
    assert "plus 4 min of later refreshes, no model" in out
    comp["busySeconds"] = 2 * 3600 + 5 * 60 + 67 + 600
    fixes["busySeconds"] = 0
    assert re.search(r'costtotal.*>2 h 16 min</span></td><td><span class="costmoney"',
                     cost.components_html(comp))


# --------------------------------------------------------------------------- 7 Oct 2026

def _restarted_branch(tmp_path):
    """The reference PR's history: an attempt with its trailer, a revert of the whole
    branch to its base, then the implementation that stands."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    base = commit(repo, "base", "2026-09-01T10:00:00+00:00")
    git(repo, "checkout", "-qb", "feat")
    (repo / "a.txt").write_text("first attempt\n")
    commit(repo, "attempt\n\nClaude-Session: old-attempt", "2026-09-17T18:30:00+00:00")
    (repo / "a.txt").write_text("base\n")
    undo = commit(repo, "revert the branch to main", "2026-10-05T16:58:00+00:00")
    (repo / "a.txt").write_text("second attempt\n")
    commit(repo, "implement\n\nClaude-Session: the-one", "2026-10-05T17:10:00+00:00")
    return repo, base, undo


def test_a_branch_reset_to_its_base_forgets_who_wrote_what_it_undid(tmp_path):
    """petclinic test-pr: d247348a implemented #37 on 17–18 Sep, the branch was reverted
    to origin/main on 5 Oct and #37 implemented again in 82329d0a. One trailer on a
    pre-revert commit billed the first attempt ($15.53, 27 h) as a second implementing
    session of a change it wrote none of."""
    repo, base, undo = _restarted_branch(tmp_path)
    sha, at = hc.restart_point(repo, base)
    assert sha == undo and at.isoformat().startswith("2026-10-05T16:58")
    assert hc.claimed_sessions(repo, base) == ["the-one"]
    assert hc.fork_time(repo, base) == at, "the change forks again at the reset"


def test_the_reset_still_counts_after_main_is_merged_into_the_branch(tmp_path):
    """test-pr merged origin/main on 7 Oct: the fork moved to the new main, whose tree the
    5 Oct reset no longer equals, and the rebuilt Cost tab billed the undone 17-18 Sep
    session again ($11 -> $27). A reset to main as it was then is still a reset."""
    repo, base, undo = _restarted_branch(tmp_path)
    git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("main moved on\n")
    commit(repo, "main moves on", "2026-10-06T09:00:00+00:00")
    git(repo, "checkout", "-q", "feat")
    git(repo, "merge", "-q", "--no-edit", "main")
    sha, at = hc.restart_point(repo, "main")
    assert sha == undo and at.isoformat().startswith("2026-10-05T16:58")
    assert hc.claimed_sessions(repo, "main") == ["the-one"]


def test_a_recorded_implementation_drops_a_session_the_branch_undid(tmp_path):
    """`review-cost.json` was written before the reset was understood: the build drops the
    undone session from it and says so on the row's hover."""
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    repo, base, undo = _restarted_branch(tmp_path)
    old = hc.entry(hc.CLAUDE, "old-attempt-session", "the implementing session, fork → prepare",
                   ("2026-09-17T18:29:54+00:00", "2026-09-18T21:49:56+00:00"), usd=15.53)
    new = hc.entry(hc.CLAUDE, "the-one-session", "the implementing session, fork → prepare",
                   ("2026-10-05T17:00:48+00:00", "2026-10-05T17:11:53+00:00"), usd=4.78)
    row = hc.component("implementation", [new, old],
                       window=("2026-09-17T18:29:54+00:00", "2026-10-05T17:11:53+00:00"))
    kept = hc.drop_undone(repo, base, row)
    assert [e["session"] for e in kept["entries"]] == ["the-one-session"]
    assert kept["usd"] == 4.78 and kept["dropped"][0]["undoneBy"] == undo[:8]
    out = cost.components_html({"rows": [kept], "usd": 4.78, "aic": 0.0})
    assert "left out: session old-atte ($15.53), whose work" in out
    assert "fork → prepare" not in out, "the pipeline's words are not the reader's"
    review = hc.component("review", [new])
    assert hc.drop_undone(repo, base, review) is review, "only the implementation row"


def test_several_sessions_are_a_breakdown_under_the_row_not_a_line_of_prices(tmp_path):
    """Victor, 7 Oct 2026: `· $2.16` at the end of a line read as an odd second total. Each
    session is a muted line of its own under the row — its time split under the time, its
    price under the price — the id muted, no dates; the fold says it opens."""
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import cost
    run = hc.entry(hc.CLAUDE, "20f87074-aaaa", "the /human-review run",
                   ("2026-10-05T16:26:00+00:00", "2026-10-05T16:39:00+00:00"), usd=2.16)
    run["busySeconds"], run["modelSeconds"] = 840, 180
    step = hc.entry(hc.CLAUDE, "claude -p (.model-runs.json)",
                    "requirements↔tests mapping (rerun-model.py)", usd=0.27,
                    models={"Sonnet 5.5": 54000})
    step["busySeconds"], step["modelSeconds"] = 53, 53
    guide = hc.component("guide", [run, step])
    guide["busySeconds"], guide["modelSeconds"] = 840, 233
    out = cost.components_html({"rows": [guide], "usd": 2.43, "aic": 0.0}, fold="<table></table>")
    parts = re.findall(r'<tr class="costpart">(.*?)</tr>', out)
    assert len(parts) == 2
    assert '<code class="costsid">20f87074</code>' in parts[0]
    assert ("model thinking and writing 3 min</li><li>waiting for tools 11 min</li></ul>\">14 min<"
            in html.unescape(parts[0])) and "$2.16" in parts[0]
    assert "requirements↔tests mapping<" in parts[1] and "rerun-model.py" not in parts[1]
    assert "16:26" not in out and "&rarr;" not in out, "no dates on a session's line"
    assert "costnum" not in out
    assert "costfold" in out.split('<tr class="costpart">')[-1], "the fold follows the parts"
    assert "nextElementSibling}while" in out, "the toggle finds its fold past the parts"


# --------------------------------------------------------------------------------------- #
# The human's time: Wispr's measured dictations, the words it has no row for, the reading.
# --------------------------------------------------------------------------------------- #

WISPR_SCHEMA = ("CREATE TABLE History (timestamp DATETIME, asrText TEXT, formattedText TEXT, "
                "duration FLOAT, numWords INTEGER)")


def _rec(kind: str, at: str, text, **extra) -> str:
    return json.dumps({"type": kind, "timestamp": at, **extra,
                       "message": {"role": kind, "content": text}})


def _human_fixture(tmp_path: Path, monkeypatch, entrypoint: str = "cli") -> Path:
    db = tmp_path / "flow.sqlite"
    con = sqlite3.connect(db)
    con.execute(WISPR_SCHEMA)
    # Logged at the start of speech, a minute before the relay delivers it; the relay
    # sends Wispr's raw asrText, which the formatted text does not match word for word.
    con.execute("INSERT INTO History VALUES ('2026-10-08 09:00:30.000 +00:00', "
                "'please add a column for how much time it took', "
                "'Please add a column for how long it took.', 60.0, 10)")
    con.commit()
    con.close()
    monkeypatch.setattr(hc, "WISPR_DB", db)
    words = " ".join(["word"] * 250)                     # one minute at READ_WPM
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        _rec("user", "2026-10-08T08:50:00Z", "fix the build " * 10, entrypoint=entrypoint),
        _rec("assistant", "2026-10-08T08:55:00Z", [{"type": "text", "text": words}]),
        _rec("user", "2026-10-08T09:01:40Z",
             "please add a column for how much time it took\n\n[Dictated in RO or EN]\n"
             "[📁=$WALKIE_SHOTS/x]", entrypoint=entrypoint),
        _rec("user", "2026-10-08T09:02:00Z", [{"type": "tool_result", "content": "ok"}]),
        _rec("assistant", "2026-10-08T09:03:00Z", [{"type": "text", "text": "done " * 125}]),
    ]) + "\n")
    return path


def test_human_time_measures_dictation_and_estimates_the_rest(tmp_path, monkeypatch):
    path = _human_fixture(tmp_path, monkeypatch)
    got = hc.claude_human_time(path, hc.parse("2026-10-08T08:00:00Z"),
                               hc.parse("2026-10-08T10:00:00Z"))
    assert got["prompts"] == 2 and got["dictated"] == 1 and got["measured"] == 1
    assert got["speak"] == 60                            # Wispr's own duration
    assert got["type"] == 45                             # 30 typed words at 40 wpm
    assert got["read"] == 60 + 30                        # 250 words before, 125 after
    assert got["seconds"] == 60 + 45 + 90


def test_reading_is_capped_by_the_wait(tmp_path, monkeypatch):
    """He cannot have read the reply for longer than he waited before speaking."""
    path = _human_fixture(tmp_path, monkeypatch)
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace("08:55:00", "09:00:10")  # 20 s before the dictation began
    path.write_text("\n".join(lines) + "\n")
    got = hc.claude_human_time(path, hc.parse("2026-10-08T08:00:00Z"),
                               hc.parse("2026-10-08T10:00:00Z"))
    assert got["read"] == 20 + 30


def test_a_scripted_run_has_nobody_at_the_keyboard(tmp_path, monkeypatch):
    path = _human_fixture(tmp_path, monkeypatch, entrypoint="sdk-cli")
    got = hc.claude_human_time(path, hc.parse("2026-10-08T08:00:00Z"),
                               hc.parse("2026-10-08T10:00:00Z"))
    assert got["prompts"] == 0 and got["seconds"] == 0


def test_no_you_column_and_the_time_header_says_tools_are_in_it():
    # Victor, 14 Oct 2026: "you" said `none` on every row the agent ran alone, and
    # "agent" read as the model's thinking only.
    import importlib.util
    spec = importlib.util.spec_from_file_location("brh", HERE / "build-review-html.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    assert "tools" in b.BUSY_TIP and "<li>" in b.BUSY_TIP
    cell = html.unescape(b._time_cell(600, 240))
    assert "model thinking and writing 4 min" in cell and "waiting for tools 6 min" in cell


# --------------------------------------------------------------------------- who coded

def _effort_turn(f: Path, ts: str, mid: str, effort: str, model: str = "claude-opus-5-5",
                 context: int = 50_000) -> None:
    """An assistant record as Claude Code writes it: `effort` beside `message.model`."""
    rec = {"type": "assistant", "timestamp": ts, "effort": effort, "perTurnEffort": effort,
           "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "ok"}],
                       "usage": {"input_tokens": 2, "output_tokens": 1,
                                 "cache_read_input_tokens": context}}}
    with f.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_the_coder_is_named_with_the_effort_its_transcript_records(tmp_path, monkeypatch):
    """PR #49 ended with "🤖 Generated with Claude Code" and its title said "by Opus":
    which Opus, at what effort, was only in the transcript — where every assistant record
    carries `model` and `effort`. A record from before the field is filled from it, a turn
    past 200k tokens proves the 1M window, and a turn outside the window is not counted."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(hc.rc(), "PROJECTS", projects)
    (projects / "-repo").mkdir(parents=True)
    t = projects / "-repo" / "s1.jsonl"
    _effort_turn(t, "2026-10-05T17:00:52Z", "m1", "high")
    _effort_turn(t, "2026-10-05T17:00:52Z", "m1", "high")   # one call streamed as two rows
    _effort_turn(t, "2026-10-05T17:05:00Z", "m2", "high", context=211_323)
    _effort_turn(t, "2026-10-05T18:30:00Z", "m3", "low")     # after the window: the review
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = {"harness": hc.CLAUDE, "session": "s1", "models": {"Opus 5.5": 1000},
             "window": ["2026-10-05T17:00:48+00:00", "2026-10-05T17:11:53+00:00"]}
    (repo / hc.RECORD_FILE).write_text(json.dumps({"schema": hc.RECORD_SCHEMA, "components": [
        {"key": "implementation", "measured": True, "entries": [entry]}]}))
    sig = hc.coder_signature(repo, scan=False)
    assert sig == "Opus 5.5 (effort: high, context: 1M)"
    assert hc.pr_footer(sig) == ("🤖 Generated with [Claude Code](https://claude.com/claude-code)"
                                 " · Opus 5.5 (effort: high, context: 1M)")
    row = hc.fill_efforts({"entries": [dict(entry)]})
    assert row["entries"][0]["efforts"] == {"Opus 5.5": {"high": 2}}
    assert row["entries"][0]["peakContext"] == {"Opus 5.5": 211_323 + 2}


def test_mixed_efforts_and_a_small_context_say_only_what_is_known():
    got = {"efforts": {"medium": 3, "high": 9, "": 4}, "peak": 150_000}
    assert hc.model_signature("Opus 5.5", got) == "Opus 5.5 (effort: high/medium)"
    assert hc.model_signature("Opus 5.5", {}) == "Opus 5.5"
    assert hc.pr_footer("") == hc.CLAUDE_CODE_FOOTER, "nothing known: Claude Code's own line"


def test_a_scout_model_is_not_named_as_the_coder():
    row = {"measured": True, "entries": [
        {"models": {"Opus 5.5": 950, "Haiku 4.5": 50},
         "efforts": {"Opus 5.5": {"xhigh": 40}, "Haiku 4.5": {"low": 9}}}]}
    assert [k for k, _ in hc.coder_models(row)] == ["Opus 5.5"]


def test_the_cost_column_names_the_effort_beside_the_model():
    from hrbuild.tabs import cost
    cell = cost._cost_cell("$4.78", 11_168_006, {"Opus 5.5": 11_168_006}, {"Opus 5.5": 4.78},
                           {"Opus 5.5": {"efforts": {"high": 77}, "peak": 211_323}})
    assert '<span class="costsub">Opus 5.5 (high)</span>' in cell
    assert "Opus 5.5 (effort: high, context: 1M): 11.2M tokens" in html.unescape(cell)
    plain = cost._cost_cell("$1", 10, {"Opus 5.5": 10})
    assert '<span class="costsub">Opus 5.5</span>' in plain, "no effort recorded: the name alone"


def test_with_no_transcript_the_implementation_commits_trailer_names_the_coder(tmp_path,
                                                                               monkeypatch):
    """The transcript stays on the machine that wrote the code; the commit travels. PR #49's
    first attempt (`Claude Opus 5`, 17 Sep) was reverted to the base, and `Implements:`
    names the re-implementation — whose trailer, in Victor's words, carries the effort."""
    monkeypatch.setattr(hc.rc(), "PROJECTS", tmp_path / "nothing")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    commit(repo, "base", "2026-09-01T10:00:00Z")
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.txt").write_text("first try\n")
    commit(repo, "feat: first try\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>",
           "2026-09-17T18:29:54Z")
    (repo / "a.txt").write_text("base\n")
    commit(repo, "Revert to main", "2026-10-05T16:58:39Z")
    (repo / "a.txt").write_text("again\n")
    impl = commit(repo, "Link Visit with Vet\n\nCo-Authored-By: Claude Opus 5.5 "
                        "(effort: high, context: 1M)", "2026-10-05T17:10:35Z")
    (repo / "b.txt").write_text("fix\n")
    commit(repo, f"[auto-fix] x\n\nImplements: {impl}\n"
                 "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>",
           "2026-10-05T17:16:43Z")
    assert hc.implementation_commit(repo, "main") == impl
    assert hc.coder_signature(repo, "main", scan=False) == "Opus 5.5 (effort: high, context: 1M)"
    git(repo, "commit", "-q", "--amend", "-m", "[auto-fix] x")   # no Implements: any more
    assert hc.implementation_commit(repo, "main") == impl, "the oldest commit past the revert"
