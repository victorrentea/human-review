"""The IDE logos beside `Served`: which IDE a click on code opens in (green, one at most),
and, served, whether that IDE's window is on the reviewed commit (amber when the reviewed
branch is open at another commit)."""
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


# ---- two IDEs: VS Code and IntelliJ on one bridge protocol -------------------------------
# IntelliJ's plugin registers `idea-<pid>.json` and answers the same routes, bar the ones its
# first milestone answers 501. The page sends the reader's pick as `ide`; without one, the
# server routes exactly as it did when VS Code was the only editor it knew.

class _Ide(http.server.BaseHTTPRequestHandler):
    """One editor's bridge: /ping with its folders, and every POST recorded. A route in
    `not_yet` answers 501, the way IntelliJ's M1 answers /open-diff, /review-open, /open-url."""
    app = "vscode"
    folders: list = []
    focused = False
    not_yet: tuple = ()
    asked: list = []

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == "/ping" and self.headers["x-relay-token"] == "t"
        self._reply({"ok": True, "app": self.app, "focused": self.focused,
                     **({"ide": "IntelliJ IDEA"} if self.app == "idea" else {}),
                     "folders": self.folders})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.asked.append((self.path.split("?")[0], json.loads(raw) if raw else None))
        if self.path.split("?")[0] in self.not_yet:
            self._reply({"ok": False, "error": "not in this version"}, 501)
        elif self.path.split("?")[0] in self.answers:
            payload, status = self.answers[self.path.split("?")[0]]
            self._reply(payload, status)
        else:
            self._reply({"ok": True})

    def log_message(self, *a):
        pass


def _ide(tmp_path, monkeypatch, app, folder: Path, focused=False, not_yet=(), answers=None):
    H = type("H", (_Ide,), {"app": app, "focused": focused, "not_yet": not_yet, "asked": [],
                            "answers": answers or {},
                            "folders": [{"name": folder.name, "path": str(folder),
                                         "realPath": str(folder.resolve())}]})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    registry = tmp_path / "home" / ".human-review" / "ide"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{app}-{httpd.server_address[1]}.json").write_text(
        json.dumps({"app": app, "port": httpd.server_address[1], "token": "t", "pid": 1}))
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return httpd, H


def _src(repo: Path) -> Path:
    f = repo / "src" / "Visit.java"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("class Visit {}\n" * 5)
    return f


@pytest.fixture
def stop():
    servers = []
    yield servers.append
    [s.shutdown() for s in servers]


def test_an_intellij_registry_entry_is_found_after_vs_codes(tmp_path, monkeypatch):
    registry = tmp_path / ".human-review" / "ide"
    registry.mkdir(parents=True)
    for name in ("idea-3.json", "vscode-9.json", "notes.json"):
        (registry / name).write_text("{}")
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))
    assert [f.name for f in srv.bridge_files()] == ["vscode-9.json", "idea-3.json"]


def test_the_picked_ide_takes_the_click_and_the_answer_says_so(
        server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    f = _src(r)
    vsc, V = _ide(tmp_path, monkeypatch, "vscode", r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", r)
    stop(vsc), stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&ide=idea")
    assert status == 200 and json.loads(payload) == {"ide": "idea", "fallback": False}
    assert [p for p, _ in I.asked] == ["/open-file"] and V.asked == []
    assert I.asked[0][1] == {"path": str(f), "line": 3, "focus": True}


def test_no_window_of_the_picked_ide_on_the_file_never_reroutes_to_the_other(
        server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    f = _src(r)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    vsc, V = _ide(tmp_path, monkeypatch, "vscode", r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", elsewhere)
    stop(vsc), stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, **kw: None)
    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&ide=idea")
    # Victor, 20 Oct 2026: IntelliJ connected, the click is IntelliJ's even with VS Code
    # alone owning the file — its plugin opens a file no window of it has yet.
    assert V.asked == [], V.asked
    if payload:
        assert json.loads(payload).get("fallback") in (False, None)


def test_without_ide_only_vs_code_is_asked_as_before(server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    f = _src(r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", r)
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    opened = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    status, _ = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3")
    # IntelliJ owns the file, but a page that sent no pick gets today's route: no VS Code
    # window, so the OS's vscode:// — and a 204 with no body, as before.
    assert status == 204 and I.asked == []
    assert opened == [["open", f"vscode://file/{f}:3:1"]]


def test_nobody_owns_the_file_so_the_os_gets_the_picked_ides_url(
        server, repo, tmp_path, monkeypatch):
    r, _, _ = repo
    f = _src(r)
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "nobody"))
    monkeypatch.setattr(srv, "ROOT", r)
    opened = []
    monkeypatch.setattr(srv.subprocess, "run", lambda cmd, **kw: opened.append(cmd))
    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=7&ide=idea")
    assert status == 200 and json.loads(payload) == {"ide": "idea", "fallback": False}
    assert opened == [["open", "idea://open?" + srv.urllib.parse.urlencode(
        {"file": str(f), "line": 7})]]


def test_intellij_without_review_open_opens_the_file_in_intellij_not_vs_code(
        server, repo, tmp_path, monkeypatch, stop):
    """501 is "not yet", not "no": the click stays in the IDE the reader picked."""
    r, _, head = repo
    f = _src(r)
    vsc, V = _ide(tmp_path, monkeypatch, "vscode", r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", r, not_yet=("/review-open",))
    stop(vsc), stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&sha={head}&root={r}"
                                           "&branch=feature&ide=idea")
    assert status == 200 and json.loads(payload)["ide"] == "idea"
    assert [p for p, _ in I.asked] == ["/review-open", "/open-file"] and V.asked == []


def test_a_diff_in_intellij_without_diffs_opens_the_file_there_never_code_diff(
        server, repo, tmp_path, monkeypatch, stop):
    r, first, _ = repo
    ij, I = _ide(tmp_path, monkeypatch, "idea", r, not_yet=("/open-diff",))
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    monkeypatch.setattr(srv, "code_cli", lambda: pytest.fail("VS Code's CLI was asked"))
    status, payload = _call(server, "GET",
                            f"{srv.OPEN_DIFF}?path=a.txt&base={first}&line=1&ide=idea")
    assert status == 200 and json.loads(payload) == {"ide": "idea", "fallback": False}
    assert [p for p, _ in I.asked] == ["/open-diff", "/open-file"]


def test_intellij_with_diffs_takes_the_diff_and_nothing_else(
        server, repo, tmp_path, monkeypatch, stop):
    """Plugin 0.2.0 answers /open-diff itself: no fallback to the plain file."""
    r, first, _ = repo
    ij, I = _ide(tmp_path, monkeypatch, "idea", r)
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    monkeypatch.setattr(srv, "code_cli", lambda: pytest.fail("VS Code's CLI was asked"))
    status, payload = _call(server, "GET",
                            f"{srv.OPEN_DIFF}?path=a.txt&base={first}&line=1&ide=idea")
    assert status == 200 and json.loads(payload) == {"ide": "idea", "fallback": False}
    assert I.asked == [("/open-diff", {"file": str(r / "a.txt"), "base": first, "line": 1,
                                       "focus": True})]


def test_intellijs_no_window_refusal_reaches_the_page_with_its_prompt(
        server, repo, tmp_path, monkeypatch, stop):
    """Plugin 0.2.0 answers /review-open: its 409 is relayed as it is, never an open-file."""
    r, _, head = repo
    f = _src(r)
    said = {"ok": False, "error": "no-window", "message": "No open IntelliJ window is on this commit",
            "prompt": "Find a local checkout"}
    ij, I = _ide(tmp_path, monkeypatch, "idea", r, answers={"/review-open": (said, 409)})
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    status, payload = _call(server, "GET", f"{srv.OPEN}?path={f}&line=3&sha={head}&root={r}"
                                           "&branch=feature&ide=idea")
    assert status == 409 and json.loads(payload) == {**said, "ide": "idea", "fallback": False}
    assert [p for p, _ in I.asked] == ["/review-open"]


def test_the_badge_poll_lists_the_ides_on_this_checkout_only(repo, tmp_path, monkeypatch, stop):
    r, _, head = repo
    other = tmp_path / "other"
    other.mkdir()
    for app, folder, focused in (("vscode", r, False), ("idea", r, True),
                                 ("vscode", other, True)):
        stop(_ide(tmp_path, monkeypatch, app, folder, focused=focused)[0])
    got = srv.editor_state(head, str(r), "feature")
    assert got["ides"] == [{"app": "vscode", "label": "VS Code", "focused": False},
                           {"app": "idea", "label": "IntelliJ IDEA", "focused": True}]
    assert got["state"] == "on" and "VS Code" in got["tip"]
    picked = srv.editor_state(head, str(r), "feature", ide="idea")
    assert picked["state"] == "on" and picked["tip"].startswith("Click to bring IntelliJ window")
    assert picked["ides"] == got["ides"]


def test_the_poll_endpoint_passes_the_pick_on_only_when_sent(server, monkeypatch):
    asked = []
    monkeypatch.setattr(srv, "ROOT", None)
    monkeypatch.setattr(srv, "editor_state",
                        lambda *a, **k: asked.append((a, k)) or {"state": "on", "tip": "x"})
    _call(server, "GET", f"{srv.EDITOR}?sha=abc&root=/r&branch=b&ide=idea")
    _call(server, "GET", f"{srv.EDITOR}?sha=abc&root=/r&branch=b&ide=emacs")
    assert asked == [(("abc", "/r", "b"), {"ide": "idea"}), (("abc", "/r", "b"), {})]


def test_pressing_the_badge_for_intellij_raises_or_opens_intellij(repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    launched = []
    monkeypatch.setattr(srv, "_launch", lambda *a: launched.append(a))
    ij, I = _ide(tmp_path, monkeypatch, "idea", r)
    stop(ij)
    assert srv.open_editor(r, "idea") == {"how": "focused", "window": r.name}
    assert I.asked == [("/command", None)] and launched == []
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "nobody"))
    assert srv.open_editor(r, "idea") == {"how": "opened", "window": None}
    assert launched == [(r, "idea")]


def test_the_badge_press_endpoint_takes_the_pick_from_its_body(server, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(srv, "open_editor", lambda *a: opened.append(a) or {"how": "opened"})
    monkeypatch.setattr(srv, "ROOT", tmp_path)
    status, payload = _call(server, "POST", srv.EDITOR_OPEN, {"ide": "idea"})
    assert status == 200 and json.loads(payload)["ide"] == "idea"
    _call(server, "POST", srv.EDITOR_OPEN, {})
    assert opened == [(tmp_path, "idea"), (tmp_path,)]


# ---- the PR line's *in IntelliJ*: the PR in IntelliJ's Pull Requests tool window ------------
# The page names the PR; the server finds the IntelliJ window on its own checkout and asks its
# plugin (`/open-pr`), relaying the plugin's sentence when it cannot.

PR = "https://github.com/o/r/pull/51"


def test_the_pr_goes_to_the_intellij_window_on_the_checkout(
        server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    vsc, V = _ide(tmp_path, monkeypatch, "vscode", r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", r, answers={
        "/open-pr": ({"ok": True, "number": 51, "folder": "petclinic"}, 200)})
    stop(vsc), stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    status, payload = _call(server, "POST", srv.OPEN_PR, {"url": PR})
    assert status == 200 and json.loads(payload) == {"how": "opened", "window": "petclinic",
                                                     "number": 51}
    assert I.asked == [("/open-pr", {"url": PR, "root": str(r)})] and V.asked == []


@pytest.mark.parametrize("said, status, sentence", [
    ({"ok": False, "error": "This project is not connected to o/r on GitHub"}, 409,
     "This project is not connected to o/r on GitHub"),
    ({"ok": False, "error": "no project is open"}, 404, "no project is open"),
    ({"ok": False}, 404, "too old to open PRs — update it to 0.3.5 or later"),
], ids=["refused", "no-project", "older-plugin"])
def test_intellijs_refusal_reaches_the_page_as_a_sentence(
        server, repo, tmp_path, monkeypatch, stop, said, status, sentence):
    r, _, _ = repo
    ij, I = _ide(tmp_path, monkeypatch, "idea", r, answers={"/open-pr": (said, status)})
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    got, payload = _call(server, "POST", srv.OPEN_PR, {"url": PR})
    assert got == 409 and sentence in payload, payload
    assert [p for p, _ in I.asked] == ["/open-pr"]


def test_no_intellij_window_on_the_checkout_says_so_and_launches_nothing(
        server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    vsc, V = _ide(tmp_path, monkeypatch, "vscode", r)
    ij, I = _ide(tmp_path, monkeypatch, "idea", elsewhere)
    stop(vsc), stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    monkeypatch.setattr(srv, "_launch_in", lambda *a: pytest.fail("IntelliJ was launched"))
    status, payload = _call(server, "POST", srv.OPEN_PR, {"url": PR})
    assert status == 409 and payload.startswith(f"No IntelliJ window has {r} open"), payload
    assert I.asked == [] and V.asked == []


def test_only_a_github_pull_url_is_taken_and_only_from_this_page(
        server, repo, tmp_path, monkeypatch, stop):
    r, _, _ = repo
    ij, I = _ide(tmp_path, monkeypatch, "idea", r)
    stop(ij)
    monkeypatch.setattr(srv, "ROOT", r)
    for url in ("https://example.com/o/r/pull/51", f"{PR}#discussion_r1", f"{PR}/files",
                "http://github.com/o/r/pull/51", "file:///etc/passwd", 51, None):
        status, _ = _call(server, "POST", srv.OPEN_PR, {"url": url})
        assert status == 400, url
    status, _ = _call(server, "POST", srv.OPEN_PR, {"url": PR}, {"X-Human-Review-Token": "guess"})
    assert status == 403 and I.asked == []


# ---- the page: the picker, and what a click sends ------------------------------------------

@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                       # no chromium installed on this box
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _picker_page(browser, ides, pick=None, extra="", state="on"):
    """A served page with the masthead logos, one source link and one *in VS Code* link,
    against a fake server whose badge poll answers `ides`. Returns the page and the
    requests it sent (`METHOD path`, and the body of a POST)."""
    from test_action_server import build
    sent = []
    link = build.gh_comment_link({"_ghUrl": "https://github.com/o/r/pull/51#discussion_r1",
                                  "_ghFile": "vscode://file//r/src/A.java:12:1",
                                  "_ghWhere": "src/A.java:12"})
    body = ('<!doctype html><html data-hr-head="abc" data-hr-root="/r"><head>'
            + '<style>' + build.CSS + '</style>' + build.SERVER_JS
            + '</head><body><div class="titlerow"><span class="titleside">'
            + build.IDE_CHIPS
            + '</span></div><a class="srcref" id="ref" href="vscode://file//r/src/A.java:3:1">'
            f'A.java:3</a><p id="gh">{link}</p>' + extra + build.TIP_JS + build.EDITOR_JS
            + '</body></html>')

    def serve(route):
        req = route.request
        url = req.url
        sent.append(req.method + " " + url.split("127.0.0.1:9", 1)[1]
                    + (" " + (req.post_data or "") if req.method == "POST" else ""))
        if url.endswith("/review.html"):
            return route.fulfill(body=body, content_type="text/html")
        if "/__human_review__" in url:
            return route.fulfill(body='{"humanReview": 1, "token": "t"}',
                                 content_type="application/json")
        if "/__editor__" in url:
            tip = "Click to open a new IntelliJ in r\nstate line" if state == "off" \
                else "Click\nstate line"
            return route.fulfill(body=json.dumps({"state": state, "tip": tip,
                                                  "ides": ides}),
                                 content_type="application/json")
        if "/__editor_open__" in url:
            return route.fulfill(body='{"how": "focused", "window": "r", "opened": "/r"}',
                                 content_type="application/json")
        if "/__open__" in url:
            return route.fulfill(body='{"ide": "idea", "fallback": false}',
                                 content_type="application/json")
        return route.fulfill(status=404, body="")
    page = browser.new_page()
    if pick:
        page.add_init_script(f"localStorage.setItem('hr-ide', '{pick}')")
    page.route("http://127.0.0.1:9/**", serve)
    page.goto("http://127.0.0.1:9/review.html")
    return page, sent


IDES2 = [{"app": "vscode", "label": "VS Code", "focused": False},
         {"app": "idea", "label": "IntelliJ IDEA", "focused": True}]


def _states(page):
    """Each logo as `app:state`, `app:hidden` when the masthead does not show it."""
    return page.eval_on_selector_all(
        ".chip-ide", "cs => cs.map(c => c.dataset.ide + ':' + (c.hidden ? 'hidden' : "
        "(['ide-on', 'ide-near', 'ide-off'].find(k => c.classList.contains(k)) || '-')))")


def test_the_logos_connect_one_ide_at_a_time_and_clicks_follow_it(browser):
    page, sent = _picker_page(browser, IDES2)
    page.wait_for_timeout(300)
    # Nothing connected until the reader connects one, whatever has a window: both red.
    assert _states(page) == ["vscode:ide-off", "idea:ide-off"]
    assert page.evaluate("document.documentElement.classList.contains('ide-none')")
    assert page.get_attribute("#hr-ide-idea", "data-tip") == "Click to connect IntelliJ"
    assert page.get_attribute("#hr-ide-idea", "data-tip-html") is None
    # Red → connected, alone in the masthead, green.
    page.click("#hr-ide-idea")
    assert _states(page) == ["vscode:hidden", "idea:ide-on"]
    assert page.get_attribute("#hr-ide-idea", "aria-pressed") == "true"
    assert page.get_attribute("#hr-ide-idea", "data-tip") == "Click to disconnect IntelliJ"
    assert page.evaluate("document.documentElement.classList.contains('ide-idea')")
    assert page.locator("a.f-vsc .ide-word").text_content() == "IntelliJ"
    assert page.locator("a.f-vsc svg.ij-ico").is_visible()
    assert not page.locator("a.f-vsc svg.vsc-ico").is_visible()
    page.wait_for_timeout(200)
    assert any(u.startswith("POST /__editor_open__") and '"ide":"idea"' in u for u in sent), sent
    page.click("#ref")
    page.wait_for_timeout(200)
    assert any(u.startswith("GET /__open__?") and "&ide=idea" in u for u in sent), sent
    assert any(u.startswith("GET /__editor__?") and "&ide=idea" in u for u in sent), sent
    # The pick is the reader's, kept across a reload.
    page.reload()
    page.wait_for_function("document.querySelector('#hr-ide-idea.ide-on')")
    assert _states(page) == ["vscode:hidden", "idea:ide-on"]
    # Green → disconnected: both back, red; no IDE link, and a click on code copies.
    page.click("#hr-ide-idea")
    assert _states(page) == ["vscode:ide-off", "idea:ide-off"]
    assert not page.locator("a.f-vsc").is_visible()
    before = len([u for u in sent if "/__open__" in u])
    page.click("#ref")
    page.wait_for_timeout(200)
    assert len([u for u in sent if "/__open__" in u]) == before, sent
    assert "no IDE connected" in page.locator("#copy-toast").inner_text()
    # …and the other one can be connected now.
    page.click("#hr-ide-vscode")
    assert _states(page) == ["vscode:ide-on", "idea:hidden"]
    page.close()


def test_a_picked_ide_with_no_window_on_the_checkout_is_red_and_a_press_opens_it(browser):
    """Victor, 29 Oct 2026: IntelliJ was green while its only window had another project,
    and every button failed until a disconnect and a reconnect."""
    page, sent = _picker_page(browser, [], pick="idea", state="off")
    page.wait_for_function("document.querySelector('#hr-ide-idea.ide-off')")
    assert _states(page) == ["vscode:hidden", "idea:ide-off"]
    assert page.get_attribute("#hr-ide-idea", "data-tip") == "Click to open a new IntelliJ in r"
    page.click("#hr-ide-idea")
    page.wait_for_timeout(200)
    assert any(u.startswith("POST /__editor_open__") and '"ide":"idea"' in u for u in sent), sent
    assert page.evaluate("localStorage.getItem('hr-ide')") == "idea", "still the pick"
    page.close()


def test_the_connected_logo_tip_says_only_what_a_click_does(browser):
    """Victor, 20 Oct 2026: "say click to disconnect IntelliJ, the rest should not be there"."""
    page, sent = _picker_page(browser, IDES2[1:], pick="idea")
    page.wait_for_function("document.querySelector('#hr-ide-idea.ide-on')")
    page.wait_for_timeout(300)
    assert page.get_attribute("#hr-ide-idea", "data-tip") == "Click to disconnect IntelliJ"
    assert page.get_attribute("#hr-ide-idea", "data-tip-html") is None
    page.close()


def test_a_findings_title_opens_its_reference_in_the_picked_ide(browser):
    """Victor, 20 Oct 2026: a click on the card's title opens its lines; the GitHub and IDE
    icons sit at the right end of its header row, icons only."""
    from test_action_server import build
    card = build._render_finding_items([{
        "severity": "high", "title": "Unknown vetId", "body": "b",
        "_ghUrl": "https://github.com/o/r/pull/51#discussion_r1",
        "_ghFile": "vscode://file//r/src/A.java:12:1", "_ghWhere": "src/A.java:12"}])
    page, sent = _picker_page(browser, IDES2, pick="idea", extra=f'<ol class="findings">{card}</ol>')
    page.wait_for_timeout(300)
    li = page.locator("ol.findings > li").first
    box, gh, ide = (li.bounding_box(), li.locator("> a.f-gh").bounding_box(),
                    li.locator("> a.f-vsc").bounding_box())
    assert gh["x"] + gh["width"] > box["x"] + box["width"] - 40, (box, gh)
    # Icons only, the same size, on one centre line, the IDE's left of GitHub's.
    assert ide["width"] < 20 and abs(ide["width"] - gh["width"]) < 1, (ide, gh)
    assert ide["x"] < gh["x"] and abs((ide["y"] + ide["height"] / 2)
                                      - (gh["y"] + gh["height"] / 2)) < 1.5, (ide, gh)
    page.click("ol.findings .f-title")
    page.wait_for_timeout(200)
    assert any(u.startswith("GET /__open__?") and "A.java" in u and "&ide=idea" in u
               for u in sent), sent
    page.close()


def test_a_folder_box_names_the_picked_ide_to_a_screen_reader_too(browser):
    """The Structure tab's package boxes said "Reveal … in VS Code" in their aria-label whatever
    the pick; the label now follows it, including a pick made after the page painted."""
    from test_action_server import build
    body = ('<!doctype html><html data-hr-head="abc" data-hr-root="/r"><head>'
            + '<style>' + build.CSS + '</style>' + build.SERVER_JS
            + '</head><body><div class="titlerow"><span class="titleside">'
            + build.IDE_CHIPS
            + '</span></div><svg><g id="box" data-folder="src/owner" data-folder-name="owner">'
            '<rect width="9" height="9"/></g></svg>'
            + build.TIP_JS + build.EDITOR_JS + build.FOLDERS_JS + '</body></html>')
    sent = []

    def serve(route):
        url = route.request.url
        sent.append(url)
        if url.endswith("/review.html"):
            return route.fulfill(body=body, content_type="text/html")
        if "/__human_review__" in url:
            return route.fulfill(body='{"humanReview": 1, "token": "t", "revealFolder": true}',
                                 content_type="application/json")
        if "/__editor__" in url:
            return route.fulfill(body=json.dumps({"state": "on", "tip": "x", "ides": IDES2}),
                                 content_type="application/json")
        return route.fulfill(status=404, body="")
    page = browser.new_page()
    page.route("http://127.0.0.1:9/**", serve)
    page.goto("http://127.0.0.1:9/review.html")
    page.wait_for_timeout(300)
    page.click("#hr-ide-vscode")
    assert page.get_attribute("#box", "aria-label") == "Reveal owner in VS Code"
    page.click("#hr-ide-vscode")
    page.click("#hr-ide-idea")
    assert page.get_attribute("#box", "aria-label") == "Reveal owner in IntelliJ"
    # None connected: the box raises no IDE.
    page.click("#hr-ide-idea")
    page.click("#box")
    page.wait_for_timeout(200)
    assert not any("/__reveal_folder__" in u for u in sent), sent
    page.close()


def _offdisk_page(browser, tmp_path, pick=None):
    """The page read off disk (`file:`): no server, the pick only picks the link scheme."""
    from test_action_server import build
    f = tmp_path / "review.html"
    f.write_text('<!doctype html><html data-hr-head="abc" data-hr-root="/r"><head>'
                 + '<style>' + build.CSS + '</style>' + build.SERVER_JS + '</head><body>'
                 + '<div class="titlerow"><span class="titleside">' + build.IDE_CHIPS
                 + '</span></div>'
                 + build.gh_comment_link({"_ghUrl": "https://github.com/o/r/pull/51#discussion_r1",
                                          "_ghFile": "vscode://file//r/src/A.java:12:1",
                                          "_ghWhere": "src/A.java:12"})
                 + build.TIP_JS + build.EDITOR_JS + '</body></html>')
    page = browser.new_page()
    if pick:
        page.add_init_script(f"localStorage.setItem('hr-ide', '{pick}')")
    page.goto(f.as_uri())
    page.wait_for_timeout(200)
    return page


def test_off_disk_the_remembered_intellij_pick_words_the_links(browser, tmp_path):
    """No server: a reader who picked IntelliJ before still reads "in IntelliJ" — the clicks
    go to its plugin (`ideaLink`)."""
    for pick, word in (("idea", "IntelliJ"), ("vscode", "VS Code"), (None, "your IDE")):
        page = _offdisk_page(browser, tmp_path, pick)
        assert page.locator("a.f-vsc .ide-word").text_content() == word
        assert page.evaluate("document.documentElement.classList.contains('ide-idea')") == (pick == "idea")
        page.close()


def test_off_disk_the_logos_toggle_too(browser, tmp_path):
    page = _offdisk_page(browser, tmp_path)
    assert _states(page) == ["vscode:ide-off", "idea:ide-off"]
    page.click("#hr-ide-idea")
    assert _states(page) == ["vscode:hidden", "idea:ide-on"]
    assert page.locator("a.f-vsc .ide-word").text_content() == "IntelliJ"
    page.click("#hr-ide-idea")
    assert _states(page) == ["vscode:ide-off", "idea:ide-off"]
    assert page.evaluate("localStorage.getItem('hr-ide')") == "none"
    page.close()


def test_online_the_logos_are_greyed_and_a_press_changes_nothing(browser):
    """GitHub Pages: no IDE on the reader's machine is reachable from here."""
    from test_action_server import build
    body = ('<!doctype html><html data-hr-head="abc" data-hr-root="/r"><head>'
            + '<style>' + build.CSS + '</style>' + build.SERVER_JS + '</head><body>'
            + '<div class="titlerow"><span class="titleside">' + build.IDE_CHIPS
            + '</span></div>' + build.TIP_JS + build.EDITOR_JS + '</body></html>')
    page = browser.new_page()
    page.route("https://example.github.io/**",
               lambda route: route.fulfill(body=body, content_type="text/html"))
    page.goto("https://example.github.io/review.html")
    page.wait_for_timeout(200)
    assert _states(page) == ["vscode:-", "idea:-"]
    assert page.evaluate("getComputedStyle(document.getElementById('hr-ide-idea')).opacity") == "0.45"
    page.click("#hr-ide-idea", force=True)            # aria-disabled: Playwright would wait
    assert _states(page) == ["vscode:-", "idea:-"]
    assert "local copy" in page.locator("#copy-toast").inner_text()
    page.close()


def _idea_link(href, face="", tip="", attrs=None, stamp=None):
    """Run the page's own `ideaLink` in node against a stand-in for the clicked anchor."""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    from test_action_server import build
    js = build.EDITOR_JS
    parts = [js[js.index(f"  function {name}("):js.index("\n  }\n", js.index(f"  function {name}(")) + 4]
             for name in ("parse", "endOf", "commentAim", "ideaLink")]
    got = dict(attrs or {}, href=href, **{"data-tip": tip})
    script = (f"var STAMP = {json.dumps(stamp or {})};\n" + "".join(parts)
              + f"var A = {json.dumps(got)};\n"
              + f"var link = {{textContent: {json.dumps(face)}, getAttribute: function (k) "
                "{ return k in A ? A[k] : null; }};\n"
              + "process.stdout.write(JSON.stringify(ideaLink(link)));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_off_disk_intellij_links_carry_what_the_uri_handler_reads():
    assert _idea_link("vscode://file//r/src/A.java:49:1", face="A.java:49-51") == (
        "jetbrains://idea/human-review/open?file=%2Fr%2Fsrc%2FA.java&line=49&endLine=51")
    assert _idea_link("vscode://file//r/My%20Dir/A.java:3:1", face="A.java:3") == (
        "jetbrains://idea/human-review/open?file=%2Fr%2FMy%20Dir%2FA.java&line=3")
    stamp = {"hrHead": "abc1234", "hrRoot": "/r", "hrBranch": "test-pr"}
    assert _idea_link("vscode://file//r/src/A.java:7:1", face="A.java:7", stamp=stamp) == (
        "jetbrains://idea/human-review/review-open?file=%2Fr%2Fsrc%2FA.java&line=7"
        "&sha=abc1234&root=%2Fr&branch=test-pr")
    # A diff handle asks for the diff, stamped page or not.
    assert _idea_link("vscode://file//r/src/A.java:12:1", stamp=stamp,
                      attrs={"data-diff-base": "origin/main", "data-diff-path": "src/A.java"}) == (
        "jetbrains://idea/human-review/diff?file=%2Fr%2Fsrc%2FA.java&base=origin%2Fmain&line=12")
    assert _idea_link("https://github.com/o/r") is None


def test_off_disk_intellij_is_asked_before_the_vs_code_handlers():
    from test_action_server import build
    js = build.EDITOR_JS
    jb = js.index("var jb = offDiskIdea() && ideaLink(link);")
    assert js.index("if (SERVED && base && dpath)") < jb < js.index("var duri = link.getAttribute('data-diff-uri');")
