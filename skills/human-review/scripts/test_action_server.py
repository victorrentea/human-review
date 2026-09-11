#!/usr/bin/env python3
"""What the action server promises, and what the page promises without one.

Three buttons on the review guide used to do nothing but fill the clipboard. Served by
`serve-review.py` they now run the command instead — which means this file is testing a
loopback endpoint that executes shell commands on the reader's machine, and the things
worth pinning are mostly the refusals.

The shape of the defence, in the order the tests walk it:

  1. the command is never in the request. The page sends an id; the command behind it was
     written into `.human-review/.actions.json` by the build. An id the manifest does not
     declare is not runnable, and a page built somewhere else declares nothing at all.
  2. the parameters are the ones the manifest declared, in the shapes it declared them.
  3. the request has to come from the page this server served — Sec-Fetch-Site, Origin,
     Host, a per-process token, and a Content-Type that forces a preflight nobody answers.
  4. and with no server at all, the page does exactly what it did before: clipboard.

The server is exercised for real, on a socket, because half of what is being tested is
header handling and a hand-rolled fake would be testing the fake. The commands it is
asked to run are `printf` and `false` — the real ones are a docker build and a PlantUML
render, neither of which belongs in a unit test.

Run with:  python3 -m pytest test_action_server.py
"""
from __future__ import annotations

import functools
import html
import http.client
import importlib.util
import json
import re
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srv = _load("serve_review", "serve-review.py")
build = _load("build_review_actions", "build-review-html.py")


MANIFEST = {
    "version": 1,
    "actions": {
        "demo-env": {"command": "printf 'listening\\nhttp://localhost:49521\\n'",
                     "params": {}, "scrape": "url", "reload": False, "label": "start"},
        "cue-drive": {"command": "printf 'cue %s at %s\\n' {n} {base}",
                      "params": {"n": "int", "base": "url"}, "scrape": "", "reload": False},
        "drawio:conceptual": {"command": "false", "params": {}, "reload": True},
    },
}


# --------------------------------------------------------------------------- #
# reading the manifest
# --------------------------------------------------------------------------- #

def _fresh(tmp_path, doc=MANIFEST):
    """A served directory with a manifest in it, and the module's caches wiped.

    The manifest cache is keyed on mtime, and two tests writing two manifests inside one
    filesystem tick would otherwise have the second read the first."""
    if doc is not None:
        (tmp_path / srv.ACTIONS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    srv._manifest.update(mtime=None, actions={})
    srv.RUNS.clear()
    srv.ROOT = tmp_path
    return tmp_path


def test_the_declared_actions_are_read_back(tmp_path):
    got = srv.actions(_fresh(tmp_path))
    assert set(got) == {"demo-env", "cue-drive", "drawio:conceptual"}
    assert got["cue-drive"]["params"] == {"n": "int", "base": "url"}


def test_no_manifest_means_no_actions(tmp_path):
    """The copy in the zip and the copy on GitHub Pages sit beside no manifest, and this is
    the same answer a reader gets for a directory that was never built."""
    assert srv.actions(_fresh(tmp_path, doc=None)) == {}


def test_an_unparseable_manifest_is_no_actions_rather_than_a_crash(tmp_path):
    _fresh(tmp_path)
    (tmp_path / srv.ACTIONS_FILE).write_text("{not json", encoding="utf-8")
    srv._manifest.update(mtime=None, actions={})
    assert srv.actions(tmp_path) == {}


def test_an_entry_with_a_parameter_shape_nobody_defined_is_dropped(tmp_path):
    """`params: {n: "anything"}` is how a hole gets into a command with no rule about what
    may go in it. The entry is refused whole — the alternative is running a template whose
    one unguarded parameter is filled by the page."""
    got = srv.actions(_fresh(tmp_path, {"version": 1, "actions": {
        "ok": {"command": "true", "params": {}},
        "loose": {"command": "echo {n}", "params": {"n": "anything"}},
        "wordless": {"command": 42},
    }}))
    assert set(got) == {"ok"}


def test_the_manifest_is_re_read_when_the_build_rewrites_it(tmp_path):
    """One of the actions *is* the rebuild: it re-runs the generator, which rewrites the
    page and this file beside it. A registry read once at startup would keep offering the
    previous build's verbs to a reader looking at the new page."""
    _fresh(tmp_path)
    assert "demo-env" in srv.actions(tmp_path)
    time.sleep(0.01)
    (tmp_path / srv.ACTIONS_FILE).write_text(
        json.dumps({"version": 1, "actions": {"other": {"command": "true"}}}), encoding="utf-8")
    assert set(srv.actions(tmp_path)) == {"other"}


# --------------------------------------------------------------------------- #
# filling the holes
# --------------------------------------------------------------------------- #

def test_a_declared_parameter_lands_in_the_command():
    argv, problem = srv.resolve_command(MANIFEST["actions"]["cue-drive"],
                                        {"n": "3", "base": "http://localhost:8080"})
    assert problem is None
    assert argv[:2] == ["/bin/sh", "-c"]
    assert argv[2] == "printf 'cue %s at %s\\n' 3 http://localhost:8080"


def test_a_value_goes_in_shell_quoted_even_after_its_shape_let_it_through(monkeypatch):
    """Nothing that survives one of the three shapes needs quoting — which is exactly why
    the quoting is there. It is the lock that still holds the day somebody widens a shape
    without thinking about the shell, so the failure is a command that does not work
    rather than a command that works too well. Simulated by widening one."""
    monkeypatch.setitem(srv.PARAM_SHAPES, "word", re.compile(r"^.*$"))
    argv, _ = srv.resolve_command({"command": "run {name}", "params": {"name": "word"}},
                                  {"name": "x; touch /tmp/pwned"})
    assert argv[2] == "run 'x; touch /tmp/pwned'"


@pytest.mark.parametrize("params, expected", [
    ({"n": "3; rm -rf ~", "base": "http://localhost:1"}, "n is not a valid int"),
    ({"n": "3", "base": "http://evil.example/x"}, "base is not a valid url"),
    ({"n": "3", "base": "http://localhost:1", "extra": "1"}, "extra is not a parameter"),
    ({"n": "3"}, "this action needs base"),
])
def test_a_parameter_that_is_not_what_it_was_declared_to_be_is_refused(params, expected):
    argv, problem = srv.resolve_command(MANIFEST["actions"]["cue-drive"], params)
    assert argv is None and expected in problem


def test_a_hole_the_manifest_never_declared_is_refused_rather_than_shipped_literally():
    """`{base}` with no `base` in `params` would otherwise reach the shell as four
    characters of nonsense and the command would fail somewhere confusing."""
    argv, problem = srv.resolve_command({"command": "run --app {base}", "params": {}}, {})
    assert argv is None and "{base}" in problem


# --------------------------------------------------------------------------- #
# who is allowed to ask
# --------------------------------------------------------------------------- #

class _Headers(dict):
    def get(self, key, default=None):          # http.client.HTTPMessage is case-insensitive
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default

    def __getitem__(self, key):
        return self.get(key)


@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:7654"},
    {"Host": "localhost:7654", "Sec-Fetch-Site": "same-origin"},
    # A typed URL or a bookmark: no page was involved, so there is no page to have been
    # tricked into sending it.
    {"Host": "127.0.0.1:7654", "Sec-Fetch-Site": "none"},
    {"Host": "127.0.0.1:7654", "Sec-Fetch-Site": "same-origin",
     "Origin": "http://127.0.0.1:7654"},
])
def test_the_page_this_server_served_may_ask(headers):
    assert srv.refuse_reason(_Headers(headers)) is None


@pytest.mark.parametrize("headers, why", [
    ({"Host": "127.0.0.1:7654", "Sec-Fetch-Site": "cross-site"}, "cross-site"),
    ({"Host": "127.0.0.1:7654", "Sec-Fetch-Site": "same-site"}, "same-site"),
    # DNS rebinding: a name the attacker owns, resolved to 127.0.0.1. Every other check
    # passes, because as far as the browser is concerned the two really are same-origin.
    ({"Host": "rebind.example.com:7654"}, "loopback only"),
    ({"Host": "127.0.0.1:7654", "Origin": "https://evil.example"}, "not this review"),
    # Same host, different port — a *different* review server, or anything else on
    # loopback that a reader happens to have open.
    ({"Host": "127.0.0.1:7654", "Origin": "http://127.0.0.1:5500"}, "not the origin"),
])
def test_anything_else_is_refused(headers, why):
    assert why in (srv.refuse_reason(_Headers(headers)) or "")


# --------------------------------------------------------------------------- #
# the endpoints, on a real socket
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(tmp_path):
    _fresh(tmp_path)
    srv.Handler.root = str(tmp_path)
    srv.Handler.token = "test-token"
    httpd = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), functools.partial(srv.Handler, directory=str(tmp_path)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()


def _call(address, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*address, timeout=10)
    sent = {"Host": f"127.0.0.1:{address[1]}", "Sec-Fetch-Site": "same-origin",
            "X-Human-Review-Token": "test-token"}
    if body is not None:
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, sent)
    response = conn.getresponse()
    payload = response.read().decode()
    conn.close()
    return response.status, payload


def _finish(address, run_id, deadline=10.0):
    """Poll to completion, the way the page does."""
    until = time.time() + deadline
    while time.time() < until:
        status, payload = _call(address, "GET", f"{srv.RUN_STATUS}?run={run_id}")
        assert status == 200, payload
        snap = json.loads(payload)
        if snap["state"] != "running":
            return snap
        time.sleep(0.05)
    raise AssertionError("the run never finished")


def test_the_probe_identifies_itself_and_lists_its_verbs(server):
    status, payload = _call(server, "GET", srv.MARKER)
    body = json.loads(payload)
    assert status == 200
    # The marker is the whole point: GitHub Pages answers this path with an HTML 404 that
    # does not parse, which is how a published page knows it is not being served by us.
    assert body[srv.MARKER_KEY] == 1
    assert set(body["actions"]) == {"demo-env", "cue-drive", "drawio:conceptual"}
    assert body["actions"]["cue-drive"]["params"] == {"n": "int", "base": "url"}


def test_an_id_the_manifest_does_not_declare_runs_nothing(server):
    status, payload = _call(server, "POST", srv.RUN, {"id": "rm -rf /", "params": {}})
    assert status == 404 and "does not" not in payload
    assert "is not an action this review declares" in payload
    assert not srv.RUNS


def test_a_command_cannot_be_smuggled_in_beside_the_id(server):
    """The one thing this design exists to make impossible. `command` in the body is not a
    field anything reads — there is no code path from a request to a shell line."""
    status, _ = _call(server, "POST", srv.RUN,
                      {"id": "demo-env", "command": "touch /tmp/pwned", "params": {}})
    assert status == 200
    snap = _finish(server, json.loads(_call(server, "POST", srv.RUN,
                                            {"id": "demo-env"})[1])["run"])
    assert "pwned" not in snap["output"]


def test_a_cross_site_request_is_refused(server):
    status, payload = _call(server, "POST", srv.RUN, {"id": "demo-env"},
                            {"Sec-Fetch-Site": "cross-site"})
    assert status == 403 and "cross-site" in payload
    assert not srv.RUNS


def test_a_request_addressed_to_a_rebound_name_is_refused(server):
    status, payload = _call(server, "POST", srv.RUN, {"id": "demo-env"},
                            {"Host": "totally.legit.example"})
    assert status == 403 and "loopback only" in payload


def test_a_form_content_type_is_refused(server):
    """A cross-origin `fetch` may send `application/json` only after a preflight this
    server does not answer — so insisting on it is what makes the browser refuse on our
    behalf. A form post, which needs no preflight, cannot set it."""
    status, payload = _call(server, "POST", srv.RUN, {"id": "demo-env"},
                            {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415 and "application/json" in payload


def test_a_caller_that_never_read_the_probe_has_no_token(server):
    status, payload = _call(server, "POST", srv.RUN, {"id": "demo-env"},
                            {"X-Human-Review-Token": "guessed"})
    assert status == 403 and "not served by this server" in payload


def test_open_is_guarded_too_without_changing_its_contract(server):
    """It has always been a bare GET that opens a path in the reader's editor. Same URL,
    same query string, same 404 for a path it will not open — but not from another site."""
    status, payload = _call(server, "GET", f"{srv.OPEN}?path=/nope/x.java&line=1",
                            headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403 and "may not drive this machine" in payload
    status, _ = _call(server, "GET", f"{srv.OPEN}?path=/nope/x.java&line=1")
    assert status == 404


def test_the_url_the_command_printed_comes_back_as_the_base(server):
    """The prize. `start-docker.sh` ends by printing the port the host gave it, and that
    port is the one thing the page could not know when it was built — the whole reason
    there is a box asking the reader to paste it in."""
    status, payload = _call(server, "POST", srv.RUN, {"id": "demo-env"})
    assert status == 200
    snap = _finish(server, json.loads(payload)["run"])
    assert snap["state"] == "done" and snap["exit"] == 0
    assert snap["result"]["base"] == "http://localhost:49521"
    assert "listening" in snap["output"]


@pytest.mark.parametrize("line, expected", [
    ("   http://localhost:49521", "http://localhost:49521"),
    ("✅ ready at http://127.0.0.1:8080/petclinic.", "http://127.0.0.1:8080/petclinic"),
    ("pulling from https://registry.example.com/image", None),
])
def test_only_a_loopback_url_is_scraped(line, expected):
    found = srv.URL_IN_OUTPUT.findall(line)
    assert (found[-1].rstrip(".,)") if found else None) == expected


def test_a_command_that_failed_is_an_answer_not_an_error(server):
    """`state: failed` with the exit code and the tail, so the page can say what happened.
    A rejected promise here would be indistinguishable from the server having gone away,
    which is the case where the button has to fall back to the clipboard."""
    status, payload = _call(server, "POST", srv.RUN, {"id": "drawio:conceptual"})
    assert status == 200
    snap = _finish(server, json.loads(payload)["run"])
    assert snap["state"] == "failed" and snap["exit"] != 0
    # …and the page is told to reload on success, because that command rewrites the very
    # file the browser is displaying.
    assert snap["reload"] is True


def test_a_bad_parameter_is_a_bad_request_not_a_missing_action(server):
    status, payload = _call(server, "POST", srv.RUN,
                            {"id": "cue-drive", "params": {"n": "$(id)", "base": "http://localhost:1"}})
    assert status == 400 and "not a valid int" in payload


def test_a_run_id_nobody_issued_is_not_a_run(server):
    status, _ = _call(server, "GET", f"{srv.RUN_STATUS}?run=made-up")
    assert status == 404


def test_a_second_click_joins_the_run_already_in_flight(server, tmp_path):
    """A docker build takes minutes and prints nothing a reader recognises for the first
    of them. The second click is not a second build; it is someone who could not tell the
    first one had started."""
    _fresh(tmp_path, {"version": 1, "actions": {"slow": {"command": "sleep 3"}}})
    first = json.loads(_call(server, "POST", srv.RUN, {"id": "slow"})[1])
    second = json.loads(_call(server, "POST", srv.RUN, {"id": "slow"})[1])
    assert first["run"] == second["run"]
    assert len(srv.RUNS) == 1


def test_the_reaper_waits_for_a_command_that_is_still_running(server, tmp_path):
    """Shutting down under `start-docker.sh up` would orphan a docker build halfway
    through the thing the click asked for."""
    _fresh(tmp_path, {"version": 1, "actions": {"slow": {"command": "sleep 3"}}})
    assert srv.runs_in_flight() is False
    _call(server, "POST", srv.RUN, {"id": "slow"})
    assert srv.runs_in_flight() is True


# --------------------------------------------------------------------------- #
# what the build declares
# --------------------------------------------------------------------------- #

RUNTIME = {"command": "cd ~/repo && ./start-docker.sh up --ref abc",
           "base": "", "reset": "/__reset",
           "drive": "node drive-to-cue.js --app {base} --cue {n}"}


def test_the_runtime_block_declares_its_own_commands(tmp_path):
    build.ACTIONS.clear()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "f.cues.json").write_text(
        json.dumps([{"t": 0.0, "text": "a caption"}]), encoding="utf-8")
    out = build.video_html({"video": "assets/f.webm", "runtime": RUNTIME}, tmp_path)
    assert build.ACTIONS["demo-env"]["command"] == RUNTIME["command"]
    assert build.ACTIONS["demo-env"]["scrape"] == "url"
    assert build.ACTIONS["cue-drive"]["params"] == {"n": "int", "base": "url"}
    # The template stays in the page too: it is what the clipboard path hands over where
    # nothing is serving this guide, and the two must be the same line.
    assert RUNTIME["drive"] in out


def test_the_url_command_is_declared_only_when_the_content_file_says_so(tmp_path):
    """Turning `… up --ref abc` into `… url --ref abc` by string surgery would work for
    the one host this was written against and fail silently on the next — at probe time,
    where nobody would see it fail."""
    build.ACTIONS.clear()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "f.cues.json").write_text("[]", encoding="utf-8")
    build.video_html({"video": "assets/f.webm", "runtime": RUNTIME}, tmp_path)
    assert "demo-env-url" not in build.ACTIONS
    build.ACTIONS.clear()
    rt = dict(RUNTIME, urlCommand="cd ~/repo && ./start-docker.sh url --ref abc")
    build.video_html({"video": "assets/f.webm", "runtime": rt}, tmp_path)
    assert build.ACTIONS["demo-env-url"]["command"].endswith("url --ref abc")


def test_a_section_with_no_runtime_declares_nothing(tmp_path):
    build.ACTIONS.clear()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "f.cues.json").write_text("[]", encoding="utf-8")
    build.video_html({"video": "assets/f.webm"}, tmp_path)
    assert build.ACTIONS == {}


def test_each_diagram_declares_its_own_rerun():
    build.ACTIONS.clear()
    out = build.rerun_html({"cwd": "/repo", "command": "drawio-diff.py --name conceptual"},
                           "build-review-html.py content.json --out review.html", "conceptual")
    assert 'data-action="drawio:conceptual"' in out
    entry = build.ACTIONS["drawio:conceptual"]
    assert entry["reload"] is True
    # The copied line and the run line are the same line — two renderings of one command
    # is how the one that gets run quietly stops matching the one that gets read.
    assert entry["command"] == html.unescape(
        re.search(r'data-copy="(.*?)" data-tip', out, re.S).group(1))


def test_the_manifest_is_written_even_when_it_is_empty(tmp_path):
    """Leaving the previous build's manifest in place would mean a page without the button
    beside a server still offering to run the command behind it."""
    build.ACTIONS.clear()
    path = build.write_actions(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "actions": {}}


# --------------------------------------------------------------------------- #
# and with no server at all
# --------------------------------------------------------------------------- #

def test_the_page_asks_the_server_instead_of_reading_the_url_scheme():
    """The bug this replaced: the demo published on GitHub Pages is https, so the old
    `location.protocol` test said "served", and every editor handle on it fetched
    `/__open__` against github.io and toasted a failure at the reader."""
    assert "SERVED = location.protocol" not in build.EDITOR_JS
    assert "fetch('/__human_review__'" in build.SERVER_JS
    # A body that parses is not enough; it has to be ours.
    assert "j.humanReview" in build.SERVER_JS


def test_every_control_starts_degraded_and_rises():
    """Never the other way round. A button drawn as live that falls back to the clipboard
    30ms later has already been clicked by then, and has already lied."""
    assert "var SERVED = false;" in build.EDITOR_JS
    assert "window.HR.onready" in build.EDITOR_JS
    assert "window.HR.onready" in build.APP_ENV_JS


def test_the_clipboard_path_is_still_there_behind_every_button():
    """The requirement at the centre of this: with no server, or with a server that does
    not declare the action, the page does exactly what it did before."""
    assert "navigator.clipboard.writeText(cmd)" in build.APP_ENV_JS
    assert build.APP_ENV_JS.count("navigator.clipboard.writeText") == 2
    assert "copy(cmd.getAttribute('data-copy')" in build.EDITOR_JS
    # Each of the three is guarded by its own verb, not by "am I served".
    assert "window.HR.can('cue-drive')" in build.APP_ENV_JS
    assert "window.HR.can('demo-env')" in build.APP_ENV_JS
    assert "window.HR.can(action)" in build.EDITOR_JS


def test_a_page_with_no_server_behind_it_resolves_the_probe_to_nothing():
    """`fetch` on a file:// page throws, and that throw is the normal path for a guide read
    off disk or out of the zip — not a failure to report."""
    assert ".catch(function () { return null; })" in build.SERVER_JS
