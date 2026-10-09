"""The VSC badge beside `Served`: green when a VS Code window is on the reviewed commit,
amber when the reviewed branch is open at another commit, red otherwise."""
from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

from test_action_server import _call, server, srv  # noqa: F401  (fixture)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A checkout on `feature` two commits deep, and a worktree of it on the first commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "feature")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("1\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "one")
    first = _git(r, "rev-parse", "HEAD")
    (r / "a.txt").write_text("2\n")
    _git(r, "commit", "-qam", "two")
    return r, first, _git(r, "rev-parse", "HEAD")


def _windows(tmp_path, monkeypatch, *folders: Path):
    """One fake VS Code bridge per folder, registered where the server looks for them."""
    registry = tmp_path / "home" / ".walkie-talkie" / "ide"
    registry.mkdir(parents=True)
    servers = []
    for i, folder in enumerate(folders):
        body = json.dumps({"ok": True, "app": "vscode", "folders": [
            {"name": folder.name, "path": str(folder), "realPath": str(folder.resolve())}]}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self, body=body):
                assert self.path == "/ping" and self.headers["x-relay-token"] == "t"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        (registry / f"vscode-{i}.json").write_text(
            json.dumps({"port": httpd.server_address[1], "token": "t"}))
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return servers


def test_green_when_a_window_is_on_the_reviewed_commit(repo, tmp_path, monkeypatch):
    r, _, head = repo
    servers = _windows(tmp_path, monkeypatch, r)
    try:
        assert srv.editor_state(head, str(r), "feature")["state"] == "on"
    finally:
        [s.shutdown() for s in servers]


def test_amber_when_the_branch_is_open_at_another_commit(repo, tmp_path, monkeypatch):
    r, first, _ = repo
    servers = _windows(tmp_path, monkeypatch, r)
    try:
        got = srv.editor_state(first, str(r), "feature")
        assert got["state"] == "near" and first[:8] in got["tip"]
    finally:
        [s.shutdown() for s in servers]


def test_red_when_the_window_is_on_another_branch(repo, tmp_path, monkeypatch):
    r, first, _ = repo
    wt = tmp_path / "wt"
    _git(r, "worktree", "add", "-q", "-b", "other", str(wt), first)
    (wt / "a.txt").write_text("3\n")
    _git(wt, "commit", "-qam", "three")
    servers = _windows(tmp_path, monkeypatch, wt)
    try:
        got = srv.editor_state(_git(r, "rev-parse", "HEAD"), str(r), "feature")
        assert got["state"] == "off" and "wt (other @" in got["tip"]
    finally:
        [s.shutdown() for s in servers]


def test_a_window_on_a_folder_above_the_checkout_counts(repo, tmp_path, monkeypatch):
    r, _, head = repo
    servers = _windows(tmp_path, monkeypatch, tmp_path)
    try:
        assert srv.editor_state(head, str(r), "feature")["state"] == "on"
    finally:
        [s.shutdown() for s in servers]


def test_red_when_no_window_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))
    got = srv.editor_state("a" * 40, str(tmp_path), "feature")
    assert got["state"] == "off" and "victorrentea.human-review" in got["tip"]


def test_the_endpoint_answers_the_page_and_is_not_use(server, monkeypatch):
    asked = []
    monkeypatch.setattr(srv, "ROOT", None)
    monkeypatch.setattr(srv, "editor_state", lambda *a: asked.append(a) or {"state": "on", "tip": "x"})
    before = srv.Handler.last_seen

    status, payload = _call(server, "GET", f"{srv.EDITOR}?sha=abc&root=/r&branch=b")

    assert status == 200 and json.loads(payload) == {"state": "on", "tip": "x"}
    assert asked == [("abc", "/r", "b")]
    # Polled every few seconds by an open tab: like the watch poll, it keeps no server alive.
    assert srv.Handler.last_seen == before


def test_pressing_the_badge_opens_the_servers_own_checkout_and_nothing_else(
        server, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(srv, "open_editor", opened.append)
    monkeypatch.setattr(srv, "ROOT", tmp_path)

    # A folder in the body is ignored: the page cannot point the server at another one.
    status, payload = _call(server, "POST", srv.EDITOR_OPEN, {"root": "/etc"})

    assert status == 200 and json.loads(payload) == {"opened": str(tmp_path)}
    assert opened == [tmp_path]


def test_the_badge_cannot_be_pressed_from_another_page(server, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(srv, "open_editor", opened.append)
    monkeypatch.setattr(srv, "ROOT", tmp_path)

    status, _ = _call(server, "POST", srv.EDITOR_OPEN, {}, {"X-Human-Review-Token": "guess"})

    assert status == 403 and opened == []


def test_pressing_the_footer_path_shows_the_served_page_in_finder(server, tmp_path, monkeypatch):
    revealed = []
    monkeypatch.setattr(srv, "reveal_in_finder", revealed.append)
    (tmp_path / "review.html").write_text("<p>")

    status, payload = _call(server, "POST", srv.REVEAL, {"page": "/review.html"})

    target = (tmp_path / "review.html").resolve()
    assert status == 200 and json.loads(payload) == {"revealed": str(target)}
    assert revealed == [target]


def test_the_footer_path_reveals_nothing_outside_the_served_folder(server, tmp_path, monkeypatch):
    revealed = []
    monkeypatch.setattr(srv, "reveal_in_finder", revealed.append)
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("x")

    for page in ("/../secret.txt", str(outside), "/missing.html"):
        status, _ = _call(server, "POST", srv.REVEAL, {"page": page})
        assert status == 404, page
    status, _ = _call(server, "POST", srv.REVEAL, {"page": "/review.html"},
                      {"X-Human-Review-Token": "guess"})
    assert status == 403 and revealed == []


# ---- the three states a press can be in, and what the press does in each ----------------

class _Bridge(http.server.BaseHTTPRequestHandler):
    """A VS Code window: answers /ping with its folders, and records /command presses."""
    folders: list = []
    focus_ok = True
    pressed: list = []

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply({"ok": True, "app": "vscode", "folder": self.folders[0]["name"],
                     "folders": self.folders})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.pressed.append(self.path)
        if self.focus_ok:
            self._reply({"ok": True})
        else:  # a bridge from before the focus command was allowed
            self._reply({"ok": False, "error": "not allowed"}, 400)

    def log_message(self, *a):
        pass


def _bridge(tmp_path, monkeypatch, folder: Path, focus_ok=True):
    H = type("H", (_Bridge,), {"folders": [{"name": folder.name, "path": str(folder),
                                            "realPath": str(folder.resolve())}],
                               "focus_ok": focus_ok, "pressed": []})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    registry = tmp_path / "home" / ".walkie-talkie" / "ide"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "vscode-1.json").write_text(
        json.dumps({"port": httpd.server_address[1], "token": "t"}))
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return httpd, H


def test_state_one_window_on_the_reviewed_commit_no_prompt(repo, tmp_path, monkeypatch):
    r, _, head = repo
    httpd, _ = _bridge(tmp_path, monkeypatch, r)
    try:
        got = srv.editor_state(head, str(r), "feature")
    finally:
        httpd.shutdown()
    assert got["state"] == "on" and got["window"] == "repo" and got["prompt"] is None
    assert "reviewed commit" in got["tip"] and "front" in got["tip"]
    assert got["checkout"] == {"path": str(r), "branch": "feature", "head": head}


def test_state_two_window_on_another_branch_offers_a_checkout_prompt(repo, tmp_path, monkeypatch):
    r, first, head = repo
    _git(r, "checkout", "-q", "-b", "elsewhere", first)
    httpd, _ = _bridge(tmp_path, monkeypatch, r)
    try:
        got = srv.editor_state(head, str(r), "feature")
    finally:
        httpd.shutdown()
    assert got["state"] == "near" and got["window"] == "repo"
    assert "elsewhere @ " + first[:8] in got["tip"] and "behind" in got["tip"]
    assert got["checkout"]["branch"] == "elsewhere"
    assert got["prompt"] == (f"In {r}, check out branch feature at {head} (stash or commit "
                             "local changes first if any; don't discard them), then confirm "
                             "with git status.")
    # Nothing was moved to find that out.
    assert _git(r, "branch", "--show-current") == "elsewhere"


def test_state_three_no_window_on_the_checkout(repo, tmp_path, monkeypatch):
    r, _, head = repo
    other = tmp_path / "other"
    other.mkdir()
    httpd, _ = _bridge(tmp_path, monkeypatch, other)
    try:
        got = srv.editor_state(head, str(r), "feature")
    finally:
        httpd.shutdown()
    assert got["state"] == "off" and got["window"] is None and got["prompt"] is None
    # The press first, the state on a line of its own (Victor, 9 Oct: the whole paragraph,
    # path and all, ended with what the click does).
    press, state = got["tip"].split("\n")[:2]
    assert press == f"Click to open a new VS Code in {r.name}"
    assert state.startswith("Open now: ")


def test_the_colour_is_about_the_servers_checkout_not_the_one_the_page_names(
        server, repo, monkeypatch):
    r, _, _ = repo
    asked = []
    monkeypatch.setattr(srv, "ROOT", r)
    monkeypatch.setattr(srv, "editor_state", lambda *a: asked.append(a) or {"state": "off", "tip": ""})
    _call(server, "GET", f"{srv.EDITOR}?sha=abc&root=/elsewhere&branch=b")
    assert asked == [("abc", str(r), "b")]


def test_pressing_green_asks_that_window_to_raise_itself_and_opens_nothing(
        repo, tmp_path, monkeypatch):
    r, _, _ = repo
    launched = []
    monkeypatch.setattr(srv, "_launch", launched.append)
    httpd, H = _bridge(tmp_path, monkeypatch, tmp_path)   # a window on the folder above it
    try:
        got = srv.open_editor(r)
    finally:
        httpd.shutdown()
    assert got == {"how": "focused", "window": tmp_path.name}
    assert H.pressed == ["/command?id=workbench.action.focusWindow"] and launched == []


def test_an_older_bridge_gets_open_a_on_the_folder_it_has_not_on_the_checkout(
        repo, tmp_path, monkeypatch):
    r, _, _ = repo
    launched = []
    monkeypatch.setattr(srv, "_launch", launched.append)
    httpd, _ = _bridge(tmp_path, monkeypatch, tmp_path, focus_ok=False)
    try:
        got = srv.open_editor(r)
    finally:
        httpd.shutdown()
    # The one spelling VS Code matches to that window, so it is reused, not duplicated.
    assert got["how"] == "focused" and launched == [str(tmp_path)]


def test_pressing_red_opens_vs_code_on_the_checkout(repo, tmp_path, monkeypatch):
    r, _, _ = repo
    launched = []
    monkeypatch.setattr(srv, "_launch", launched.append)
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "nobody"))
    assert srv.open_editor(r) == {"how": "opened", "window": None}
    assert launched == [r]
