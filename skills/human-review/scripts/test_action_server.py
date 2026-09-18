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
import os
import re
import shlex
import socketserver
import subprocess
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
    # Every copy target on the block, not just the first: the sentence's own offer copies
    # the command off disk now, and the fold's copy glyph copies it too. They must all be
    # the one line the server was told to run — two renderings of one command is how the
    # one that gets run quietly stops matching the one that gets read.
    copied = {html.unescape(c) for c in re.findall(r'data-copy="(.*?)"', out, re.S)}
    assert copied == {entry["command"]}


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
    # One clipboard for the page, on HR, with the `execCommand` fallback a `file://` page
    # needs — there were two of these and the one *without* the fallback was on the control
    # that only exists off disk, which is where `navigator.clipboard` may not be there.
    assert build.SERVER_JS.count("function copy(") == 1
    assert "document.execCommand('copy')" in build.SERVER_JS
    assert "window.HR.copy(cmd)" in build.APP_ENV_JS
    assert "navigator.clipboard" not in build.APP_ENV_JS
    assert "copy(cmd.getAttribute('data-copy')" in build.EDITOR_JS
    # And off disk a click on the words of an offer copies its command rather than
    # explaining why nothing happened.
    assert "if (runhere && !cmd.getAttribute('data-copy'))" in build.EDITOR_JS
    # Each of the three is guarded by its own verb, not by "am I served".
    assert "window.HR.can('cue-drive')" in build.APP_ENV_JS
    assert "window.HR.can('demo-env')" in build.APP_ENV_JS
    assert "window.HR.can(action)" in build.EDITOR_JS


def test_a_page_with_no_server_behind_it_resolves_the_probe_to_nothing():
    """`fetch` on a file:// page throws, and that throw is the normal path for a guide read
    off disk or out of the zip — not a failure to report."""
    assert ".catch(function () { return null; })" in build.SERVER_JS


# --------------------------------------------------------------------------- #
# and the page that follows the build
# --------------------------------------------------------------------------- #
#
# The report is rebuilt under a tab that is already showing it. Every other part of this
# file is about refusing things; this part is about the one thing the server volunteers —
# a stamp that moves when the directory it serves has finished being rewritten, which is
# the page's cue to reload itself.
#
# What is worth pinning is the *debounce*, not the detection. A build writes the html,
# then eleven diagrams, then the manifest. Reloading on the first write drops the reader
# into a half-built report and leaves them there, because the writes that follow are
# changes the page is no longer around to see.


def test_the_first_stamp_is_published_immediately(tmp_path):
    """It is the baseline the page is served against. A stamp that arrived empty and
    filled in half a second later would spend that half-second looking like a change,
    and the first thing the reader would see is a reload they did not ask for."""
    (tmp_path / "review.html").write_text("built", encoding="utf-8")
    assert srv.Watcher(tmp_path).stamp


def test_the_stamp_holds_still_while_the_build_is_still_writing(tmp_path):
    (tmp_path / "review.html").write_text("built", encoding="utf-8")
    watcher = srv.Watcher(tmp_path, quiet=5)
    baseline = watcher.stamp

    (tmp_path / "review.html").write_text("rebuilt, one", encoding="utf-8")
    assert watcher.tick(now=100) == baseline
    (tmp_path / "diagram.svg").write_text("<svg/>", encoding="utf-8")
    assert watcher.tick(now=101) == baseline
    # Quiet since 101, so at 103 the tree has only held still for two of the five
    # seconds it owes. The reader is still reading the old page, correctly.
    assert watcher.tick(now=103) == baseline

    settled = watcher.tick(now=107)
    assert settled != baseline
    # And then it stays put: a stamp that keeps moving is a page that keeps reloading.
    assert watcher.tick(now=108) == settled == watcher.stamp


def test_a_file_the_build_rewrote_in_place_still_counts_as_a_change(tmp_path):
    """Same name, same length — `build-review-html.py` overwriting review.html with a
    report of the same size is not an exotic case, it is the common one."""
    page = tmp_path / "review.html"
    page.write_text("aaaa", encoding="utf-8")
    watcher = srv.Watcher(tmp_path, quiet=0)
    baseline = watcher.stamp
    page.write_text("bbbb", encoding="utf-8")
    os.utime(page, ns=(0, 1234567891))
    watcher.tick(now=200)
    assert watcher.tick(now=201) != baseline


def test_the_git_directory_is_not_watched(tmp_path):
    """A review is often served out of the working tree it describes. Every `git status`
    the reader runs in the terminal beside the page writes the index — and a page that
    reloaded itself on that would be reloading all afternoon, for nothing."""
    (tmp_path / "review.html").write_text("built", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    watcher = srv.Watcher(tmp_path, quiet=0)
    baseline = watcher.stamp
    (tmp_path / ".git" / "index").write_bytes(b"\x01\x02")
    watcher.tick(now=300)
    assert watcher.tick(now=301) == baseline


@pytest.fixture
def watched(tmp_path):
    """The module-level watcher the handler answers out of."""
    (tmp_path / "review.html").write_text("built", encoding="utf-8")
    srv.WATCHER = srv.Watcher(tmp_path, quiet=0)
    try:
        yield srv.WATCHER
    finally:
        srv.WATCHER = None


def test_the_probe_hands_the_page_the_baseline_it_was_served_against(server, watched):
    status, payload = _call(server, "GET", srv.MARKER)
    assert status == 200
    assert json.loads(payload)["watch"] == watched.stamp


def test_a_build_that_stopped_watching_takes_the_reload_away(server):
    """Empty and not absent: the page reads it as "do not poll", the same way an action
    it no longer declares stops being offered by a tab that is still open."""
    srv.WATCHER = None
    status, payload = _call(server, "GET", srv.MARKER)
    assert status == 200 and json.loads(payload)["watch"] == ""


def test_the_watch_endpoint_answers_with_the_stamp_that_moved(server, watched, tmp_path):
    status, payload = _call(server, "GET", srv.WATCH)
    assert status == 200
    first = json.loads(payload)["stamp"]
    assert first == watched.stamp

    (tmp_path / "review.html").write_text("rebuilt", encoding="utf-8")
    watched.tick(now=400)
    watched.tick(now=401)
    status, payload = _call(server, "GET", srv.WATCH)
    assert status == 200
    assert json.loads(payload)["stamp"] != first


def test_a_cross_site_watch_is_refused(server, watched):
    """Guarded like everything else here. The stamp is a fact about the reader's disk —
    it moves when they build — and any tab in the browser could otherwise watch it."""
    status, _ = _call(server, "GET", srv.WATCH, headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_a_tab_parked_on_the_report_does_not_keep_the_server_alive(server, watched):
    """The idle clock measures use, and a poller is not a user. Without this, a report
    left open on a second monitor pins a server until the machine reboots — which is the
    artifact `--idle-minutes` exists to prevent."""
    srv.Handler.last_seen = 0.0
    _call(server, "GET", srv.WATCH)
    assert srv.Handler.last_seen == 0.0
    # While anything a person actually did still feeds it.
    _call(server, "GET", "/review.html")
    assert srv.Handler.last_seen > 0.0


def test_the_page_only_polls_when_the_server_said_it_is_watching():
    """A page off disk, out of the zip, or on GitHub Pages must not poll an endpoint
    that is not there — that is the same bug as the editor handles on github.io."""
    assert "if (!j || !j.watch) return;" in build.SERVER_JS
    assert "fetch('/__watch__'" in build.SERVER_JS
    assert "location.reload();" in build.SERVER_JS


def test_the_poll_gives_up_when_the_server_has_been_reaped():
    """The server is mortal by design, and it dies under tabs that are still open. That
    is the end of the poll, not an error to report at somebody."""
    assert "if (++misses < 3) next();" in build.SERVER_JS


# --------------------------------------------------------------------------- #
# the header's Rerun
# --------------------------------------------------------------------------- #
#
# The one command this server runs that the build did not declare, and the exception that
# proves the rule: it takes no id, because there is nothing for the page to name. "Rebuild
# yourself from the repository as it is now" is a property of being served at all, so the
# capability is answered by the probe rather than written into a manifest a build could
# forget — and a page out of the zip has no button, because there is nobody there to ask.


def _slow_refresh(tmp_path, seconds=5):
    """Stand in for `refresh-report.py` with something that takes its time.

    The real program exits in a tenth of a second on a directory with no judgement in it,
    which is the right answer and the wrong stopwatch for testing a lock."""
    script = tmp_path / "slow-refresh.py"
    script.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
    return script


def test_the_rerun_runs_the_static_refresh_from_the_repository_root(tmp_path):
    """Two things the plan must get right, and both are about the page ending up next to
    evidence it really refreshed: the producers write to a *relative* `.human-review/`, so
    the cwd is the repository and `--dir` is the review directory inside it."""
    _fresh(tmp_path)
    (tmp_path / ".human-review").mkdir()
    srv.ROOT = tmp_path
    argv, cwd = srv.rerun_plan(tmp_path / ".human-review")
    assert cwd == tmp_path
    assert Path(argv[1]).name == "refresh-report.py"
    assert argv[2:] == ["--dir", ".human-review", "--steps", "static", "--no-serve"]
    # Never the model's half: not the film, not a privacy verdict, not the findings.
    assert "--allow-model" not in argv and "video" not in argv and "all" not in argv


def test_a_review_directory_outside_the_repository_gets_no_rerun(tmp_path):
    """`run-steps.py` writes to `.human-review/assets` relative to where it was launched.
    A rerun from anywhere else would rebuild the page beside evidence it never touched —
    the one outcome nobody can tell from success — so there is no button at all."""
    _fresh(tmp_path)
    srv.ROOT = tmp_path / "repo"
    (srv.ROOT).mkdir()
    assert srv.rerun_plan(tmp_path) is None
    # And with no repository under the served directory, nothing to run it in either.
    srv.ROOT = None
    assert srv.rerun_plan(tmp_path) is None


def test_the_probe_says_whether_this_page_can_rebuild_itself(server, tmp_path):
    srv.ROOT = tmp_path
    assert json.loads(_call(server, "GET", srv.MARKER)[1])["rerun"] is True
    srv.ROOT = None
    assert json.loads(_call(server, "GET", srv.MARKER)[1])["rerun"] is False


def test_the_rerun_endpoint_really_launches_the_refresh_program(server, tmp_path):
    """Run for real, against the real program, in a directory whose judgement is missing —
    so what comes back is `refresh-report.py`'s own exit 3 and its own sentence about it.
    That is the proof the endpoint reaches the program and not a lookalike."""
    srv.ROOT = tmp_path
    status, payload = _call(server, "POST", srv.RERUN, body={})
    assert status == 200, payload
    snap = _finish(server, json.loads(payload)["run"], deadline=30.0)
    assert snap["exit"] == 3 and snap["state"] == "failed"
    assert "only a model writes" in snap["output"]
    # And a failed rerun is an answer the page can show, not an exception it has to guess
    # the meaning of.
    assert "content.json" in snap["output"]


def test_a_second_click_joins_the_rerun_already_in_flight(server, tmp_path, monkeypatch):
    """One rerun at a time, and the lock is the join. Two `refresh-report.py` runs over one
    directory would have the second build reading assets the first is halfway through
    rewriting; refusing the click instead would leave a reader who could not tell the
    first one had started with nothing on screen either way."""
    srv.ROOT = tmp_path
    monkeypatch.setattr(srv, "REFRESH", _slow_refresh(tmp_path))
    first = json.loads(_call(server, "POST", srv.RERUN, body={})[1])
    second = json.loads(_call(server, "POST", srv.RERUN, body={})[1])
    assert second["run"] == first["run"] and second["state"] == "running"
    assert len([r for r in srv.RUNS.values() if r.action == srv.RERUN_ACTION]) == 1
    srv.RUNS[first["run"]]._kill()


def test_the_rerun_holds_the_reload_until_it_is_finished(server, watched, tmp_path,
                                                         monkeypatch):
    """`refresh-report.py` runs eight producers and a build, and between two of them the
    tree holds still for seconds while a program computes. Six tenths of a second of
    stillness is all the watcher asks for — so without the hold, one rerun reloads the
    reader's tab several times on the way through, each time into a directory one step
    into being rewritten and each time out from under the button they pressed."""
    srv.ROOT = tmp_path
    monkeypatch.setattr(srv, "REFRESH", _slow_refresh(tmp_path))
    baseline = watched.stamp
    run_id = json.loads(_call(server, "POST", srv.RERUN, body={})[1])["run"]

    (tmp_path / ".human-review").mkdir()
    (tmp_path / "review.html").write_text("half a page", encoding="utf-8")
    watched.tick(now=500)
    assert watched.tick(now=501) == baseline, "reloaded mid-rebuild"
    assert json.loads(_call(server, "GET", srv.WATCH)[1])["stamp"] == baseline

    srv.RUNS[run_id]._kill()
    _finish(server, run_id)
    # Released on every path out, including the one where the command died — and then it
    # is one reload, at the end, which is the one the reader wanted.
    watched.tick(now=600)
    assert watched.tick(now=601) != baseline


def test_a_hold_that_is_released_twice_does_not_silence_the_watcher(tmp_path):
    """A counter that could go negative would leave the page unable to reload for the rest
    of the server's life, with nothing on screen to say so."""
    (tmp_path / "review.html").write_text("built", encoding="utf-8")
    watcher = srv.Watcher(tmp_path, quiet=0)
    baseline = watcher.stamp
    watcher.release()
    watcher.release()
    (tmp_path / "review.html").write_text("rebuilt", encoding="utf-8")
    watcher.tick(now=700)
    assert watcher.tick(now=701) != baseline


@pytest.mark.parametrize("headers, why", [
    ({"Sec-Fetch-Site": "cross-site"}, "a page on the internet may not rebuild this"),
    ({"Host": "review.example.com"}, "a rebound name may not either"),
    ({"X-Human-Review-Token": "not-the-token"}, "nor a caller that never read the probe"),
])
def test_the_rerun_is_guarded_exactly_like_the_rest(server, tmp_path, headers, why):
    srv.ROOT = tmp_path
    status, _ = _call(server, "POST", srv.RERUN, body={}, headers=headers)
    assert status == 403, why


def test_a_form_post_cannot_rebuild_the_page(server, tmp_path):
    """The Content-Type is insisted on, not merely accepted: a form post cannot set it, so
    requiring it is what forces a cross-origin caller through a preflight this server does
    not answer — the browser then never sends the request at all."""
    srv.ROOT = tmp_path
    status, _ = _call(server, "POST", srv.RERUN, body={},
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415


def test_a_page_that_is_not_served_has_no_rerun_button():
    """Hidden in the markup and raised by the probe, like every other control here. A
    static copy has no process behind it, and a button that copied a shell line instead
    would be handing back the terminal round trip this exists to remove."""
    assert 'id="hr-rerun" hidden' in build.RERUN_CHIP
    assert 'aria-disabled="true"' in build.RERUN_CHIP
    # Per button, from the probe's own answer for that verb — not from "is there a server".
    assert "if (!window.HR.can(btn.getAttribute('data-rerun'))) return;" in build.RERUN_JS
    assert "btn.hidden = false;" in build.RERUN_JS
    assert "fetch(route, {" in build.SERVER_JS and "'/__rerun__'" in build.SERVER_JS
    # No clipboard consolation prize: there is nothing to paste that would be this button.
    assert "clipboard" not in build.RERUN_JS


def test_the_button_says_what_it_will_not_do():
    """The part a reader cannot see, and the part they are right to worry about: the
    findings are a judgement bought once, and the film costs minutes and a running app."""
    tip = re.search(r'data-tip="([^"]*)"', build.RERUN_CHIP).group(1)
    assert "Not the findings" in tip and "not the film" in tip


def test_the_rerun_chip_is_the_served_badge():
    """They were one fact written twice: a page is served *exactly when* it can rerun
    itself, so the badge announced the condition and the button beside it was the only
    thing that condition let you do. The badge is the button now — it wears the run glyph
    every command on the page wears, it says both halves in its hover, and the word
    `served` steps aside the moment this one comes up."""
    assert build.CMD_RUN in build.RERUN_CHIP
    assert "chip-served" in build.RERUN_CHIP
    tip = re.search(r'data-tip="([^"]*)"', build.RERUN_CHIP).group(1)
    assert tip.startswith("Served by the review server")
    assert "rebuild the page" in tip
    # Only the free one takes the badge's place: `Rerun + AI` is a second thing the page
    # can do, not a second way of saying what the page is.
    assert "if (btn.getAttribute('data-rerun') !== '__rerun__') return;" in build.RERUN_JS
    assert "if (mode) mode.hidden = true;" in build.RERUN_JS
    # And the word is still there for the served page whose server cannot rebuild it.
    assert "chip.textContent = 'served';" in build.SERVER_JS


def test_a_running_chip_turns_its_glyph_rather_than_growing_a_word():
    """These chips are one and three characters wide; `Running…` in one reflowed the whole
    masthead the instant it was pressed. The mark that spins is the mark the run glyphs
    down the page spin, so a reader who has seen one knows this one is working."""
    assert "btn.textContent" not in build.RERUN_JS
    assert "data-face" not in build.RERUN_JS
    assert "button.chip-rerun.running .rr-ico { animation:hrspin" in build.CSS
    # The robot and the banknote stay still: only the run mark is inside `.rr-ico`.
    assert build.RERUN_AI_CHIP.index("rr-ico") < build.RERUN_AI_CHIP.index("\U0001F916")


def test_where_the_reader_was_survives_the_rebuild():
    """The reload at the end of a rerun is not always ours — the server watches the
    directory it serves, so the build finishing can reload the tab first. Both routes have
    to land on the same saved place, which is why it is written when the button is pressed
    and not just before a reload we might never reach."""
    # On `HR` now, because three controls end in a reload — the masthead's two Reruns and
    # every command declared with `reload` — and "the same scroll position, saved under the
    # same key, restored on the same event" is not a thing to keep two copies of.
    assert "var remember = window.HR.keepPlace;" in build.RERUN_JS
    assert "sessionStorage.setItem(PLACE" in build.SERVER_JS
    assert "sessionStorage.removeItem(PLACE)" in build.SERVER_JS
    assert "window.scrollTo(0, saved.y)" in build.SERVER_JS


def test_a_failed_rerun_shows_the_last_lines_rather_than_a_shrug():
    assert "rerunfail-log" in build.RERUN_JS
    assert "log.slice(-14)" in build.RERUN_JS
    assert "snap.exit" in build.RERUN_JS


# --------------------------------------------------------------------------- #
# Rerun + AI
# --------------------------------------------------------------------------- #
#
# The same verb with the model's half in front of it, and the only control on the page that
# spends money. Everything below is about the three things that keeps honest: it is a
# *second* endpoint rather than a flag, it shares the first one's lock, and nothing in the
# test suite may ever actually call a model.


def test_the_ai_rerun_runs_the_model_step_before_the_refresh(tmp_path):
    """The order is the whole claim. The model writes the matrix and the catalogue, then
    the build turns them into the page; reversed, the click would rebuild the page from the
    matrix it is about to replace and leave the reader looking at the old one under a green
    tick. `&&` and not `;`, so a model step that failed is not followed by a build that
    hides it."""
    _fresh(tmp_path)
    (tmp_path / ".human-review").mkdir()
    srv.ROOT = tmp_path
    argv, cwd = srv.rerun_ai_plan(tmp_path / ".human-review")
    assert cwd == tmp_path
    assert argv[:2] == ["/bin/sh", "-c"]
    line = argv[2]
    assert line.index("rerun-model.py") < line.index("refresh-report.py"), \
        "the build must not run before the model it is building from"
    assert " && " in line and "; " not in line
    assert "--allow-model" in line and "--no-serve" in line
    assert "--steps static" in line
    # Never the film, and never the model's *other* half: `content.json` is the layout and
    # the ledes, which is a human's answer and no button's.
    assert "all" not in line.split() and "content.json" not in line


def test_the_printed_command_is_the_command_that_runs(tmp_path):
    """The page prints this line beside the button. A page printing a different line from
    the one the button runs is the only failure mode that control has."""
    _fresh(tmp_path)
    srv.ROOT = tmp_path
    argv, _ = srv.rerun_ai_plan(tmp_path)
    assert argv[2] == srv.rerun_ai_command(".")


def test_the_rerun_runs_the_string_the_manifest_holds_and_not_one_of_its_own(tmp_path):
    """One string, two surfaces — and this is the surface that runs it.

    The command behind the masthead's Rerun used to be written twice: assembled here, as an
    argv, and printed separately by the page for the clipboard. Two authors, one command,
    and by the time anybody looked they had drifted — the line a reader copied was missing
    the interpreter and `--no-serve`, so pasting it did something other than pressing it.
    The build declares it now and this reads it back, literally: `sh -c` over the exact
    bytes the page put on the clipboard, with nothing reconstructed in between."""
    line = "cd /somewhere && /usr/bin/python3 /skill/refresh-report.py --dir . --steps static --no-serve"
    _fresh(tmp_path, {"version": 1, "actions": {
        srv.RERUN_ACTION: {"command": line, "params": {}, "reload": True}}})
    srv.ROOT = tmp_path
    argv, cwd = srv.rerun_plan(tmp_path)
    assert argv == ["/bin/sh", "-c", line]
    assert cwd == tmp_path


def test_the_paid_rerun_runs_the_string_the_manifest_holds_too(tmp_path):
    """Same rule, and the one where drift would be expensive: a copied line that left
    `--allow-model` off would quietly buy nothing, and one that left it on where the button
    did not would buy it twice."""
    line = "cd /somewhere && /usr/bin/python3 /skill/rerun-model.py --dir . && true"
    _fresh(tmp_path, {"version": 1, "actions": {
        srv.RERUN_AI_ACTION: {"command": line, "params": {}, "reload": True}}})
    srv.ROOT = tmp_path
    argv, _ = srv.rerun_ai_plan(tmp_path)
    assert argv == ["/bin/sh", "-c", line]


def test_a_page_older_than_the_declaration_still_reruns(tmp_path):
    """The manifest beside a page built before the rerun was declared in it does not name
    the verb. The fallback is why upgrading the skill does not take the masthead's button
    away from the page the upgrade is for — it comes back the moment that page rebuilds."""
    _fresh(tmp_path)
    (tmp_path / ".human-review").mkdir()
    srv.ROOT = tmp_path
    argv, _ = srv.rerun_plan(tmp_path / ".human-review")
    assert Path(argv[1]).name == "refresh-report.py"


def test_the_two_reruns_are_not_reachable_through_the_plain_run_endpoint(tmp_path):
    """They live in the manifest because that is where their command lives, not because
    they gained a second door. `/__rerun__` and `/__rerun_ai__` carry the shared lock, the
    watcher hold and — for the paid one — the confirmation in front of it; `/__run__` has
    none of those, and the first thing it would skip is the $5."""
    _fresh(tmp_path, {"version": 1, "actions": {
        srv.RERUN_ACTION: {"command": "true", "params": {}},
        srv.RERUN_AI_ACTION: {"command": "true", "params": {}}}})
    srv.ROOT = tmp_path
    for verb in (srv.RERUN_ACTION, srv.RERUN_AI_ACTION):
        run, problem, status = srv.start_run(verb, {}, tmp_path)
        assert run is None and status == 400
        assert "endpoint of its own" in problem


# --------------------------------------------------------------------------- #
# what is already running, and what it has cost
# --------------------------------------------------------------------------- #

def test_the_run_status_endpoint_answers_without_a_run_id(server, tmp_path):
    """"Is anything running here" had no answer, and the gap cost real money.

    A reader pressed Rerun + AI, was joined in silence to a paid run somebody else had
    started an hour earlier over a working tree that had moved since, and had to pay for a
    second one when it turned out not to be theirs. A page cannot warn about a run it
    cannot see, and with no id this endpoint only ever said `no such run`."""
    _fresh(tmp_path)
    srv.ROOT = tmp_path
    status, payload = _call(server, "GET", srv.RUN_STATUS)
    assert status == 200
    idle = json.loads(payload)
    assert idle == {"active": None, "kind": None, "started": None, "joined": 0}


def test_the_global_status_names_the_run_and_when_it_started(server, tmp_path, monkeypatch):
    _fresh(tmp_path, {"version": 1, "actions": {
        "slow": {"command": f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(20)'",
                 "params": {}}}})
    srv.ROOT = tmp_path
    run, problem, _ = srv.start_run("slow", {}, tmp_path)
    assert problem is None
    try:
        state = json.loads(_call(server, "GET", srv.RUN_STATUS)[1])
        assert state["kind"] == "action"
        assert state["active"]["run"] == run.id
        assert state["started"] == pytest.approx(run.started, abs=5)
    finally:
        run._kill()


def _slow_rerun(tmp_path, monkeypatch):
    """A rerun really in flight, so the lock and the refusal are tested against a process
    rather than against a fake state.

    Through `monkeypatch` and never by assigning on the module: `REFRESH` and `MODEL_STEP`
    are process-wide, and a test that leaves a twenty-second sleep behind in one of them
    hands the next test a rerun that never finishes.
    """
    monkeypatch.setattr(srv, "REFRESH", _slow_refresh(tmp_path, 20))
    monkeypatch.setattr(srv, "MODEL_STEP", _slow_refresh(tmp_path, 20))
    run, problem, status, _ = srv.start_rerun(tmp_path / ".human-review")
    assert problem is None and status == 200
    return run


def test_a_paid_rerun_is_refused_while_anything_is_running_rather_than_joined(
        tmp_path, monkeypatch):
    """The free button joins; this one must not.

    A join is the right answer for `Rerun`: the reader could not tell the first one had
    started and the run in flight is what they were asking for. For the paid one it is
    wrong twice over — they are not told what they joined, so somebody else's hour-old run
    over an older tree looks like theirs; and when it turns out not to be, the only way out
    is to press again and pay again. 409, with the run named, and the decision handed back.
    """
    _fresh(tmp_path)
    (tmp_path / ".human-review").mkdir()
    srv.ROOT = tmp_path
    live = _slow_rerun(tmp_path, monkeypatch)
    try:
        run, problem, status, joined = srv.start_rerun(tmp_path / ".human-review", ai=True)
        assert status == 409 and joined is False
        assert run is live, "the refusal has to name what is already going"
        assert "still going" in problem and "wait for it" in problem.lower()
        # …and the free one still joins, and says so.
        run, problem, status, joined = srv.start_rerun(tmp_path / ".human-review")
        assert (problem, status, joined) == (None, 200, True)
        assert run is live
    finally:
        live._kill()


def test_the_page_is_told_it_joined_rather_than_started(server, tmp_path, monkeypatch):
    """A press handed somebody else's run looks exactly like a press that started one."""
    _fresh(tmp_path)
    (tmp_path / ".human-review").mkdir()
    srv.ROOT = tmp_path
    live = _slow_rerun(tmp_path, monkeypatch)
    try:
        status, payload = _call(server, "POST", srv.RERUN, body={})
        assert status == 200
        assert json.loads(payload)["joined"] is True
        status, payload = _call(server, "POST", srv.RERUN_AI, body={})
        assert status == 409
        refused = json.loads(payload)
        assert "still going" in refused["error"]
        assert refused["run"] == live.id, "the page needs the run to show it"
    finally:
        live._kill()


def test_the_price_is_derived_from_what_this_pages_runs_really_cost(tmp_path):
    """`~$5 on Sonnet` was a constant somebody typed once. Three real runs on the demo page
    came in at $4.00, $8.09 and $10.63, so a reader who budgeted for the label was out by a
    factor of two, in the direction that matters."""
    (tmp_path / srv.MODEL_RUNS_FILE).write_text(json.dumps({"version": 1, "runs": [
        {"when": "2026-09-18T09:00:00+00:00", "cost": 4.00},
        {"when": "2026-09-18T12:00:00+00:00", "cost": 8.09},
        {"when": "2026-09-18T18:00:00+00:00", "cost": 10.63}]}), encoding="utf-8")
    price = srv.price_estimate(tmp_path)
    assert price["text"] == "~$7.57"
    assert price["last"] == 10.63, "the last real invoice goes in the dialog"
    assert price["n"] == 3


def test_a_page_nobody_has_paid_for_says_a_range_and_not_a_number(tmp_path):
    """A number with nothing behind it is a promise. A ledger that is missing, unreadable,
    or holds only runs whose cost could not be read all mean the same thing here."""
    assert srv.price_estimate(tmp_path) == {"text": "~$5\u2013$10", "last": None, "n": 0}
    (tmp_path / srv.MODEL_RUNS_FILE).write_text("{not json", encoding="utf-8")
    assert srv.price_estimate(tmp_path)["n"] == 0
    (tmp_path / srv.MODEL_RUNS_FILE).write_text(
        json.dumps({"runs": [{"when": "x", "cost": None}]}), encoding="utf-8")
    assert srv.price_estimate(tmp_path)["text"] == "~$5\u2013$10"


def test_the_probe_carries_the_price_so_the_button_can_stop_guessing(server, tmp_path):
    _fresh(tmp_path)
    srv.ROOT = tmp_path
    (tmp_path / srv.MODEL_RUNS_FILE).write_text(json.dumps({"runs": [{"cost": 9.5}]}),
                                                encoding="utf-8")
    caps = json.loads(_call(server, "GET", srv.MARKER)[1])
    assert caps["price"]["text"] == "~$9.50" and caps["price"]["last"] == 9.5


def test_no_model_step_beside_us_means_no_paid_button(tmp_path, monkeypatch):
    """The free button survives a skill directory without `rerun-model.py`; the paid one
    does not. A button that quietly did less than its label is worse than no button."""
    _fresh(tmp_path)
    srv.ROOT = tmp_path
    monkeypatch.setattr(srv, "MODEL_STEP", tmp_path / "nowhere.py")
    assert srv.rerun_ai_plan(tmp_path) is None
    assert srv.rerun_plan(tmp_path) is not None


def test_a_review_directory_outside_the_repository_gets_no_paid_rerun(tmp_path):
    """Same honesty condition as the free one: the producers write to a relative
    `.human-review/`, so a rerun from anywhere else would rebuild the page beside evidence
    it never touched — and this one would have paid for the privilege."""
    _fresh(tmp_path)
    srv.ROOT = tmp_path / "repo"
    srv.ROOT.mkdir()
    assert srv.rerun_ai_plan(tmp_path) is None


def test_the_probe_answers_for_each_verb_separately(server, tmp_path, monkeypatch):
    """Two capabilities, not one. A page that inferred the paid one from the free one would
    draw a $5 button over a server with no model step beside it."""
    srv.ROOT = tmp_path
    both = json.loads(_call(server, "GET", srv.MARKER)[1])
    assert both["rerun"] is True and both["rerunAi"] is True
    monkeypatch.setattr(srv, "MODEL_STEP", tmp_path / "nowhere.py")
    one = json.loads(_call(server, "GET", srv.MARKER)[1])
    assert one["rerun"] is True and one["rerunAi"] is False


def test_the_paid_endpoint_launches_the_model_step(server, tmp_path):
    """Run for real, against the real program, in a directory with no report in it — so
    what comes back is `rerun-model.py`'s own refusal and its own sentence about it. That
    is the proof the endpoint reaches the program and not a lookalike, and it is safe to
    run because the program checks the directory before it checks the model."""
    srv.ROOT = tmp_path
    status, payload = _call(server, "POST", srv.RERUN_AI, body={})
    assert status == 200, payload
    snap = _finish(server, json.loads(payload)["run"], deadline=30.0)
    assert snap["state"] == "failed"
    assert "is not a review directory" in snap["output"]
    # And nothing was asked of a model on the way to that refusal. This is the test that
    # would have spent real money: `--dir` is computed *relative to the repository root*,
    # so it is `.` whenever the report is served from the root — and `.` is a directory
    # that exists. Only the content-file check stops a model being handed the repository.
    assert "claude -p" not in snap["output"]


def test_the_model_step_spends_nothing_on_a_dry_run(tmp_path):
    """Every test of the paid half, and every check that the button is wired to the right
    program, goes through `--dry-run`. So it has to print the whole invocation — the model
    included, because "which model did that page cost" is a question the command answers
    rather than one the invoice does — and call nothing."""
    review = tmp_path / ".human-review"
    (review / "test-index").mkdir(parents=True)
    (review / "test-index" / "rest.json").write_text("[]", encoding="utf-8")
    (review / "assets").mkdir()
    (review / "assets" / "requirements-map.html").write_text("<div/>", encoding="utf-8")
    (review / "content.json").write_text("{}", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(HERE / "rerun-model.py"), "--dir", str(review), "--dry-run"],
        capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "claude -p --model sonnet" in out.stdout
    assert "dry run" in out.stdout
    # The prompt is 60 lines long; a log line carrying all of it is a log line nobody
    # reads. It goes in on stdin — as the trailing argument it was swallowed by the
    # variadic `--add-dir`, and `claude` exited with "Input must be provided".
    assert "< matrix-prompt.md" in out.stdout
    # The redirection is the fix, not decoration: `--add-dir` comes last in the argv and
    # takes a list, so anything after it is another directory.
    assert re.search(r"--add-dir \S+\s+< matrix-prompt", out.stdout)
    # And it left the pair alone: a dry run that had already copied files away would be a
    # dry run with a side effect.
    assert not (review / ".model-prev").exists()


def test_the_model_step_refuses_rather_than_half_writing(tmp_path, monkeypatch):
    """A model run that ends with one of the two artifacts missing leaves the report in the
    one state `refresh-report.py` is built to refuse. Saying so here, before the build is
    reached, is the difference between a named failure and a page that quietly lost its
    matrix."""
    model = _load("rerun_model", "rerun-model.py")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    (review / "assets" / "requirements-map.html").write_text("<div/>", encoding="utf-8")
    assert model.missing(review) == ["test-index"]
    # An empty directory is the same absence as no directory: a catalogue a dead run left
    # behind is not a smaller catalogue.
    (review / "test-index").mkdir()
    assert model.missing(review) == ["test-index"]
    (review / "test-index" / "rest.json").write_text("[]", encoding="utf-8")
    assert model.missing(review) == []


def test_the_model_step_refuses_a_matrix_that_lost_a_mapped_test(tmp_path):
    """The catalogue and the matrix are one artifact in two files, and a run can exit 0
    having quietly broken the link between them.

    That is not hypothetical: `keep_previous` copies today's pair into `.model-prev/`
    *before* the model starts, so the two are byte-identical at that moment, and a run
    that read "diff your work against the previous copy" as "am I different from
    `.model-prev/`?" answered no for free and wrote nothing. A Gherkin scenario the branch
    had added reached `test-index/` and never reached *Covering tests*, and the step
    reported success. The prompt now says the copy proves nothing; this is the half that
    does not depend on the model having read it."""
    model = _load("rerun_model", "rerun-model.py")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    (review / "test-index").mkdir()
    mapping = {"blocks": [{"sentences": [{"id": "s1", "tests": [
        {"id": "src/add-visit.spec.ts:51"}, {"id": "src/book-visit.feature:18"}]}]}]}
    (review / "test-index" / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    matrix = review / "assets" / "requirements-map.html"

    matrix.write_text('<div class="reqmap">src/add-visit.spec.ts:51</div>', encoding="utf-8")
    assert model.unmapped(review) == ["src/book-visit.feature:18"]

    matrix.write_text('<div class="reqmap">src/add-visit.spec.ts:51 '
                      'src/book-visit.feature:18</div>', encoding="utf-8")
    assert model.unmapped(review) == []

    # A consistency check between two files, not a second opinion on either one's shape:
    # nothing to compare is nothing to complain about.
    (review / "test-index" / "mapping.json").write_text("{not json", encoding="utf-8")
    assert model.unmapped(review) == []
    (review / "test-index" / "mapping.json").unlink()
    assert model.unmapped(review) == []


def test_the_pair_being_replaced_is_kept_out_of_the_published_copy(tmp_path):
    """This replaces a judgement rather than refreshing it, so the copy the reader was
    looking at has to survive the click. Dot-prefixed, because `publish-demo.sh` publishes
    what does not start with a dot and a demo carrying two matrices is a demo with a bug."""
    model = _load("rerun_model", "rerun-model.py")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    (review / "assets" / "requirements-map.html").write_text("old", encoding="utf-8")
    (review / "test-index").mkdir()
    (review / "test-index" / "rest.json").write_text("[1]", encoding="utf-8")
    kept = model.keep_previous(review)
    assert kept.name.startswith(".")
    assert (kept / "requirements-map.html").read_text() == "old"
    assert (kept / "test-index" / "rest.json").read_text() == "[1]"
    # Copied, not moved: the prompt asks the model to diff its work against what was there.
    assert (review / "assets" / "requirements-map.html").is_file()


def test_one_rerun_at_a_time_across_both_endpoints(server, tmp_path, monkeypatch):
    """The lock is shared, and that is not tidiness: the paid rerun ends in a
    `refresh-report.py` of its own, so the free button and the paid one are two names for
    the same collision over one directory. A click on either while the other is working
    joins the run in flight — which is also the only answer that shows a reader who could
    not tell the first one had started what is actually happening."""
    srv.ROOT = tmp_path
    monkeypatch.setattr(srv, "REFRESH", _slow_refresh(tmp_path))
    monkeypatch.setattr(srv, "MODEL_STEP", _slow_refresh(tmp_path))
    paid = json.loads(_call(server, "POST", srv.RERUN_AI, body={})[1])
    free = json.loads(_call(server, "POST", srv.RERUN, body={})[1])
    assert free["run"] == paid["run"], "the free click started a second build"
    assert free["action"] == srv.RERUN_AI_ACTION
    assert len([r for r in srv.RUNS.values()
                if r.action in srv.RERUN_ACTIONS]) == 1
    srv.RUNS[paid["run"]]._kill()


def test_the_free_click_never_starts_the_paid_run(server, tmp_path, monkeypatch):
    """A join hands back a running Run; it does not launch anything. So the worst a shared
    lock can do is give a reader more than they asked for and tell them so in the tail they
    are watching — never spend money on a click that asked for the free half."""
    srv.ROOT = tmp_path
    monkeypatch.setattr(srv, "REFRESH", _slow_refresh(tmp_path))
    first = json.loads(_call(server, "POST", srv.RERUN, body={})[1])
    second = json.loads(_call(server, "POST", srv.RERUN_AI, body={})[1])
    assert second["run"] == first["run"]
    assert second["action"] == srv.RERUN_ACTION, "a free run was relabelled as the paid one"
    srv.RUNS[first["run"]]._kill()


@pytest.mark.parametrize("headers, why", [
    ({"Sec-Fetch-Site": "cross-site"}, "a page on the internet may not spend this money"),
    ({"Host": "review.example.com"}, "a rebound name may not either"),
    ({"X-Human-Review-Token": "not-the-token"}, "nor a caller that never read the probe"),
])
def test_the_paid_rerun_is_guarded_exactly_like_the_rest(server, tmp_path, headers, why):
    srv.ROOT = tmp_path
    status, _ = _call(server, "POST", srv.RERUN_AI, body={}, headers=headers)
    assert status == 403, why


def test_a_form_post_cannot_spend_money(server, tmp_path):
    srv.ROOT = tmp_path
    status, _ = _call(server, "POST", srv.RERUN_AI, body={},
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415


def test_the_paid_button_says_the_price_before_it_is_pressed(tmp_path):
    """The price is in the hover, in those words, because "costs money" is the part a
    reader cannot see and the part they are right to worry about."""
    tip = re.search(r'data-tip="([^"]*)"', build.RERUN_AI_CHIP).group(1)
    assert "costs money" in tip and "$5" in tip and "Sonnet" in tip
    assert 'id="hr-rerun-ai" hidden' in build.RERUN_AI_CHIP
    # The free one's mark, a plus, then the two things this one adds to it: a model, and
    # money leaving. The `+` is the sentence — this chip is the one beside it *and*
    # something more — and it is what keeps three marks from running together into one
    # picture. No words: `Rerun + AI` said neither the price nor anything the free chip
    # beside it had not already said, and cost the masthead two words to say it.
    assert build.RERUN_AI_CHIP.endswith(
        '<span class="rr-plus">+</span>\U0001F916\U0001F4B8</button>')
    assert build.CMD_RUN in build.RERUN_AI_CHIP
    assert ".rr-plus { margin:" in build.CSS
    assert "Rerun" not in build.RERUN_AI_CHIP[build.RERUN_AI_CHIP.index('data-tip'):]
    # The words are in the accessibility tree, where a glyph-only control has to put them.
    assert 'aria-label="Rerun with AI' in build.RERUN_AI_CHIP


def test_the_confirmation_is_the_pages_own_and_defaults_to_not_spending():
    """`window.confirm` cannot say the price in this page's voice, cannot make the safe
    answer the default one, and is the dialog every reader has been trained to dismiss
    unread — a reflex that on a native confirm costs five dollars and here lands on
    Cancel."""
    # Not called anywhere — the phrase survives in a comment saying why.
    assert "window.confirm(" not in build.RERUN_JS
    assert "confirm(" not in build.RERUN_JS.replace("confirmSpend(", "")
    assert "$5" in build.RERUN_AI_CONFIRM or "$5" in build.RERUN_AI_CHIP
    assert 'role="dialog"' in build.RERUN_AI_CONFIRM
    assert "hrconfirm" in build.RERUN_AI_CONFIRM and "hidden" in build.RERUN_AI_CONFIRM
    # Cancel takes focus when the panel opens, and Escape and the backdrop both mean no.
    assert "if (no && no.focus) no.focus();" in build.RERUN_JS
    assert "ev.key === 'Escape'" in build.RERUN_JS
    assert "if (t === panel) return done(false);" in build.RERUN_JS
    # The free button never reaches it.
    assert "if (!paid) { go(btn); return; }" in build.RERUN_JS


def test_pressing_one_rerun_disables_the_other():
    """The server runs one at a time and a second press on the other would join this run
    rather than start its own — correct, and unreadable: a reader who pressed the free
    button and watched the paid one's log scroll past has been told the wrong thing."""
    assert "buttons.forEach(function (other) { other.disabled = true; });" in build.RERUN_JS
    assert "buttons.forEach(function (other) { other.disabled = false; });" in build.RERUN_JS
