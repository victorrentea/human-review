#!/usr/bin/env python3
"""Whose application the film is of, and what happens to the recorder's verdict.

Two failures, one day, one film.

`record-feature-video.sh` used to prove the stack was up by fetching `http://127.0.0.1:4200/`
and `http://127.0.0.1:8080/api/pettypes` and checking that *something* answered. That is a
test of the port. This machine keeps three checkouts of one repository, each able to serve
:4200, so a review of `test-pr` was illustrated with a film of `main`: the narration was
generated from this branch's diff, the frames came from another branch, and every caption
asserted something the picture denied. The film is the one artifact on that page a reader
believes without opening anything.

And the recorder *said so*. It exited 3 — filmed, and the feature did not hold, three screens
never reached — and `run-steps.py` `_video` ran it without capturing anything, so the exit
code became a line in a status table that a human read once. The page showed the player with
no mark on it, because footage of a feature failing looks exactly like footage of one working.

So: the run owns its own instance where the project says how (`steps.video.app`), the recorder
refuses an application that reports a different commit, and every non-zero exit lands on disk
as a verdict the page draws over the player.

Run with:  python3 -m pytest test_feature_video_app.py
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RECORDER = HERE / "record-feature-video.sh"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


steps = _load("run-steps")
build = _load("build-review-html")

SHA = "904aa0517435e7327372f7280e28cf53734d20c4"
SHORT = "904aa051"

APP = {
    "up": "./start-docker.sh up --ref {sha} --ttl 1800",
    "url": "./start-docker.sh url petclinic-{shortsha}",
    "down": "./start-docker.sh down petclinic-{shortsha}",
}


class Recorder:
    """A stand-in for `sh` that answers by substring and remembers what it was asked."""

    def __init__(self, answers):
        self.answers = answers          # [(substring, returncode, stdout)]
        self.ran: list[str] = []

    def __call__(self, cmd, ctx, check=True, capture=False):
        self.ran.append(cmd)
        for needle, code, out in self.answers:
            if needle in cmd:
                if code and check:
                    raise RuntimeError(f"exit {code}: {cmd}")
                return subprocess.CompletedProcess(cmd, code, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def first(self, needle) -> str:
        return next(c for c in self.ran if needle in c)

    def has(self, needle) -> bool:
        return any(needle in c for c in self.ran)


def _ctx(tmp_path, monkeypatch, app=APP, recorder=None, film=(0, "")):
    """A run directory, a config with (or without) an `app` block, and a faked shell."""
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    answers = [("git rev-parse HEAD", 0, SHA + "\n"),
               ("git rev-parse --short HEAD", 0, SHORT + "\n"),
               ("start-docker.sh up", 0, "building\n   http://localhost:63241\n"),
               ("record-feature-video.sh", film[0], film[1])]
    sh = recorder or Recorder(answers)
    monkeypatch.setattr(steps, "sh", sh)
    cfg = {"steps": {"video": {"out": ".human-review/assets/feature.webm",
                              **({"app": app} if app else {})}}}
    return steps.Ctx("origin/main", cfg, dry=False), sh


# ── whose application is being filmed ─────────────────────────────────────────────

def test_the_run_starts_the_commit_under_review_and_films_at_the_port_it_printed(
        tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch)
    steps._video(ctx)

    # `{sha}` from git, never from the config: a sha written into human-review.json is a
    # sha that is right until the next push.
    assert sh.first("start-docker.sh up").endswith(f"up --ref {SHA} --ttl 1800")
    film = sh.first("record-feature-video.sh")
    # The host picks the port so that several branches can be up at once, so the address
    # can only come out of what `up` printed.
    assert "BASE_URL=http://localhost:63241" in film
    # Both, and the SAME one. The container's nginx proxies /api/ on its own origin; two
    # different values here is a film driving one instance's screens against another
    # instance's data.
    assert "API_URL=http://localhost:63241" in film
    assert f"HUMAN_REVIEW_APP_COMMIT={SHA}" in film
    assert "HUMAN_REVIEW_APP_STARTED=1" in film
    # And it says which instance it filmed, so the guide never has to guess.
    assert any("started by this run" in n for n in ctx.notes)


def test_the_instance_is_named_with_gits_own_abbreviation(tmp_path, monkeypatch):
    """`start-docker.sh up --ref <sha>` names what it creates with `rev-parse --short`,
    whose width is `core.abbrev` — `auto`, which grows with the repository. Slicing seven
    characters off the sha here would have `down` naming an instance that does not exist,
    and a failed `down` is a whole stack left running after every film."""
    ctx, sh = _ctx(tmp_path, monkeypatch)
    steps._video(ctx)
    assert sh.has("git rev-parse --short HEAD")
    assert sh.first("start-docker.sh down").endswith(f"down petclinic-{SHORT}")


def test_the_stack_is_stopped_even_when_the_film_crashes(tmp_path, monkeypatch):
    """A stack left up outlives the run — and the next run then finds it answering and
    films it, which is the original bug wearing a different hat."""
    ctx, sh = _ctx(tmp_path, monkeypatch, film=(1, "[video] boom\n"))
    with pytest.raises(RuntimeError):
        steps._video(ctx)
    assert sh.has("start-docker.sh down")


def test_a_stack_that_would_not_start_is_not_filmed(tmp_path, monkeypatch):
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh up", 1, "")])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(RuntimeError, match="would not start"):
        steps._video(ctx)
    assert not sh.has("record-feature-video.sh")
    # Nothing came up, so nothing is torn down: `down` on an instance that does not exist
    # is an error report about the wrong thing.
    assert not sh.has("start-docker.sh down")


def test_an_instance_that_prints_no_url_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    """Falling back to :4200 here is exactly how the wrong tree gets filmed."""
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh up", 0, "started, no address\n"),
                   ("start-docker.sh url", 0, "nothing here either\n")])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(RuntimeError, match="printed no URL"):
        steps._video(ctx)
    assert not sh.has("record-feature-video.sh")


def test_the_url_command_answers_when_up_was_quiet(tmp_path, monkeypatch):
    """`up` on an instance that is already running prints `↻ … is already up` and then the
    address; a host that prints it only from `url` is just as valid."""
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh up", 0, "already up\n"),
                   ("start-docker.sh url", 0, "http://localhost:51999\n")])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    steps._video(ctx)
    assert "BASE_URL=http://localhost:51999" in sh.first("record-feature-video.sh")


def test_a_project_with_no_app_block_films_exactly_as_it_did_before(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch, app=None)
    steps._video(ctx)
    assert not sh.has("start-docker.sh")
    # No environment forced on it: the recorder's own :4200/:8080 defaults stand, and its
    # identity guard is what keeps that honest.
    assert sh.first("record-feature-video.sh").startswith(str(steps.HERE))


# ── the verdict nobody can lose ───────────────────────────────────────────────────

EXIT3_LOG = """\
[video] title card: "Demo" / "Link Visit with Vet", 3.40s
[video] feature-script.js: 1/4 changed screens filmed | FAILED to reach: \
visits (locator.waitFor: Timeout); visits/:id/edit (no such element); owners/:id (nope)
[video] narration: 6/6 cues, 24.1s of speech, voice "Samantha"
[video] NOTE: the feature did NOT hold — the film says so out loud.
"""


def test_a_non_zero_exit_leaves_a_verdict_beside_the_film(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, film=(3, EXIT3_LOG))
    steps._video(ctx)
    verdict = json.loads(
        (tmp_path / ".human-review/assets/feature.verdict.json").read_text(encoding="utf-8"))
    assert verdict["exit"] == 3
    # Named, not counted. "Three screens missed" sends the reader back to the log; the
    # names are what tells them whether the gap is the one the change was about.
    assert len(verdict["missed"]) == 3
    assert any("visits/:id/edit" in m for m in verdict["missed"])
    assert "1/4 changed screens filmed" in verdict["note"]
    assert verdict["log"], "the last lines are the whole of the fix for whoever reads them"
    # The log is kept whatever the exit code, so the next question has an answer on disk.
    assert (tmp_path / ".human-review/assets/feature.run.log").read_text() == EXIT3_LOG
    # And the status table still says it, because the guide is written from that table.
    assert any("EXIT 3" in n and "3 screen(s) missed" in n for n in ctx.notes)


def test_a_film_that_worked_takes_the_previous_verdict_away(tmp_path, monkeypatch):
    """A stale verdict is worse than none: it draws a red band over a film that is fine."""
    ctx, _ = _ctx(tmp_path, monkeypatch, film=(3, EXIT3_LOG))
    steps._video(ctx)
    stale = tmp_path / ".human-review/assets/feature.verdict.json"
    assert stale.is_file()

    ctx2, _ = _ctx(tmp_path / "again", monkeypatch, film=(0, "[video] all four filmed\n"))
    (tmp_path / "again" / ".human-review" / "assets" / "feature.verdict.json").write_text(
        '{"exit": 3}', encoding="utf-8")
    steps._video(ctx2)
    assert not (tmp_path / "again/.human-review/assets/feature.verdict.json").exists()
    assert (tmp_path / "again/.human-review/assets/feature.run.log").is_file()


def test_exit_two_is_a_skip_that_names_where_to_look(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch, film=(2, "[video] the app is commit abc\n"))
    with pytest.raises(LookupError, match="feature.run.log"):
        steps._video(ctx)
    assert json.loads((tmp_path / ".human-review/assets/feature.verdict.json")
                      .read_text(encoding="utf-8"))["exit"] == 2


# ── and what the page does with it ────────────────────────────────────────────────

def _section(tmp_path, verdict=None, film=True):
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "feature.cues.json").write_text(json.dumps([{"t": 0.0, "text": "a caption"}]),
                                              encoding="utf-8")
    if film:
        (assets / "feature.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    if verdict is not None:
        (assets / "feature.verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    return build.video_html({"video": "assets/feature.webm"}, tmp_path)


def test_a_film_that_held_carries_no_band(tmp_path):
    assert "vidverdict" not in _section(tmp_path)


def test_exit_three_is_drawn_over_the_player(tmp_path):
    """The whole point. The footage of a feature failing is a browser, a form and a list —
    it looks like every other demo — so the page has to say what the frames cannot."""
    out = _section(tmp_path, {"exit": 3, "note": "1/4 changed screens filmed",
                              "missed": ["visits (Timeout)", "visits/:id/edit (nope)"],
                              "log": ["[video] NOTE: the feature did NOT hold"]})
    assert "The feature did not hold on film." in out
    assert "visits/:id/edit" in out
    assert "1/4 changed screens filmed" in out
    assert "the feature did NOT hold" in out
    # Above the player, not below it: anything under the picture is read after the reader
    # has already believed it.
    assert out.index("vidverdict") < out.index("<video")


def test_an_exit_code_nobody_wrote_a_sentence_for_still_says_something(tmp_path):
    out = _section(tmp_path, {"exit": 9, "log": ["[video] segfault"]})
    assert "exit 9" in out and "partial" in out


def test_an_unreadable_verdict_is_no_band_rather_than_a_broken_build(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "feature.cues.json").write_text("[]", encoding="utf-8")
    (assets / "feature.verdict.json").write_text("{half a fi", encoding="utf-8")
    assert "vidverdict" not in build.video_html({"video": "assets/feature.webm"}, tmp_path)


# ── the recorder's own identity guard ─────────────────────────────────────────────

class _Info(http.server.BaseHTTPRequestHandler):
    commit = ""

    def do_GET(self):
        if self.path.startswith("/actuator/info"):
            body = json.dumps({"git": {"commit": {"id": self.commit}}}).encode()
        elif self.path in ("/", "/api/pettypes"):
            body = b"{}"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@pytest.fixture
def app_saying(request):
    """A loopback application that answers liveness and claims to be some commit."""
    _Info.commit = request.param
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Info)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def repo(tmp_path):
    """A repository of its own with one commit, so the guard has a HEAD to compare to.

    The recorder resolves the project under review from the working directory — that is
    what makes the skill's symlink install work — so "which commit is this review about"
    is a question about the checkout the command was run in, and the test has to bring
    one."""
    at = tmp_path / "repo"
    at.mkdir()
    run = lambda *a: subprocess.run(a, cwd=str(at), capture_output=True, text=True, check=True)
    run("git", "init", "-q")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "one")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(at),
                          capture_output=True, text=True).stdout.strip()
    return at, head


def _record(url, cwd, **env):
    at = dict(os.environ, BASE_URL=url, API_URL=url, **env)
    at.pop("HUMAN_REVIEW_FEATURE_SCRIPT", None)
    return subprocess.run(["bash", str(RECORDER), str(cwd / "feature.webm")],
                          cwd=str(cwd), env=at, capture_output=True, text=True)


@pytest.mark.parametrize("app_saying", ["deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"],
                         indirect=True)
def test_an_application_on_another_commit_is_refused(app_saying, repo):
    """Exit 2, the same as a stack that is down — because it is the same fact: there is no
    application here this film could honestly be of."""
    at, _head = repo
    got = _record(app_saying, at)
    assert got.returncode == 2, got.stderr
    assert "is commit deadbeef" in got.stderr
    assert "another branch's screens" in got.stderr
    # And it says what to do about it, both ways.
    assert "steps.video.app" in got.stderr


@pytest.mark.parametrize("app_saying", ["set-by-the-test"], indirect=True)
def test_the_commit_under_review_passes_the_guard(app_saying, repo):
    """Matched by prefix, either way round: whether a project publishes a short sha or a
    full one is not this script's business."""
    at, head = repo
    _Info.commit = head[:12]
    got = _record(app_saying, at)
    assert f"reports commit {head[:12]}" in got.stderr
    # Past the guard, and stopped by the next thing — which is a different sentence.
    assert "another branch's screens" not in got.stderr
    assert "no feature script" in got.stderr


@pytest.mark.parametrize("app_saying", [""], indirect=True)
def test_an_application_that_cannot_say_only_warns(app_saying, repo):
    """Plenty of builds write no build-info, and refusing them would take the film away
    from every project that never had this bug. But the warning is loud where nothing in
    the run started the stack, that being the case where what is listening is a guess."""
    at, _head = repo
    loud = _record(app_saying, at)
    assert "cannot say which commit it is" in loud.stderr
    assert "nothing in this run started it" in loud.stderr
    assert "no feature script" in loud.stderr, "warned, not blocked"

    quiet = _record(app_saying, at, HUMAN_REVIEW_APP_STARTED="1",
                    HUMAN_REVIEW_APP_COMMIT=SHA)
    assert "this run started it itself" in quiet.stderr
    assert "nothing in this run started it" not in quiet.stderr


# ── and the rule the cues are written to ──────────────────────────────────────────

def test_the_script_writers_are_told_to_wait_for_what_they_assert():
    """`say()` takes the element only to draw a spotlight, so a locator that matches
    nothing still speaks the sentence — over a screen that does not contain the thing it
    names. `waitFor()` is what turns that into a miss instead of a burnt-in lie."""
    guide = (HERE.parent / "reference" / "feature-script.md").read_text(encoding="utf-8")
    assert "Every cue waits for what it asserts" in guide
    assert "FAILED to reach:" in guide, "the handle between the script and the page"
    recorder = RECORDER.read_text(encoding="utf-8")
    assert "waitFor()" in recorder, "the usage message is where a new project starts"


def test_the_schema_example_names_the_instance_and_not_the_ref():
    """`start-docker.sh up --ref <sha>` creates `petclinic-<shortsha>`, and `down`/`url`
    take that name. The example said `down --ref <sha>` for a while — refused by the host
    on the click, not at build time, so the button looked fine and did nothing."""
    schema = (HERE.parent / "reference" / "content-schema.md").read_text(encoding="utf-8")
    example = schema[schema.index('"runtime": {'):]
    example = example[:example.index("```")]
    assert "down petclinic-9f3c1ab" in example
    assert "url petclinic-9f3c1ab" in example
    assert "--ref" in example, "up still takes the ref; only down and url take the name"
    assert "down --ref" not in example and "url --ref" not in example


# ── the same block, for the steps that drive the app without filming it ───────────
#
# `video` had this to itself for a day, because a film of the wrong branch is what was
# noticed. The traced suites have the same hazard and a worse ending: a sequence diagram of
# another checkout is not visibly wrong the way a film is — it is a plausible picture of some
# other code, drawn under this branch's name and *committed* to `generated/`.

def _steps_ctx(tmp_path, monkeypatch, cfg_steps):
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh up", 0, "building\n   http://localhost:63241\n")])
    monkeypatch.setattr(steps, "sh", sh)
    return steps.Ctx("origin/main", {"steps": cfg_steps}, dry=False), sh


def test_the_sequence_step_can_start_the_stack_its_suites_are_traced_against(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {
        "sequence": {"app": APP, "commands": ["cd petclinic-test && ./run-tests-with-tracing.sh"]}})
    steps._sequence(ctx)

    assert sh.first("start-docker.sh up").endswith(f"up --ref {SHA} --ttl 1800")
    traced = sh.first("run-tests-with-tracing.sh")
    assert traced.startswith("BASE_URL=http://localhost:63241 ")
    assert "API_URL=http://localhost:63241" in traced
    assert sh.has(f"start-docker.sh down petclinic-{SHORT}")


def test_a_step_borrows_the_video_block_by_name_rather_than_repeating_it(
        tmp_path, monkeypatch):
    """One `up` builds the one stack both steps want, and saying so is a word, not a copy.
    Borrowing is never implicit: a stack that is right for the film is not automatically
    right for a suite that needs a trace collector standing behind it."""
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {
        "video": {"app": APP},
        "traces": {"app": "video", "report": "r", "commands": ["npm run test:cucumber"]}})
    steps._traces(ctx)

    assert sh.has("start-docker.sh up --ref " + SHA)
    assert sh.first("npm run test:cucumber").startswith("BASE_URL=http://localhost:63241 ")


def test_a_project_names_the_env_vars_its_own_tests_read(tmp_path, monkeypatch):
    """`BASE_URL`/`API_URL` is only the default. petclinic's suites read `API_BASE_URL`,
    and a step that renamed the tests around this file's default would be the tail wagging
    the dog."""
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {
        "sequence": {"app": {**APP, "env": {"BASE_URL": "{url}", "API_BASE_URL": "{url}/api"}},
                     "commands": ["npm run test:sequence"]}})
    steps._sequence(ctx)

    ran = sh.first("npm run test:sequence")
    assert "API_BASE_URL=http://localhost:63241/api" in ran
    assert "API_URL=" not in ran


def test_without_an_app_block_nothing_is_started_and_the_commands_run_as_they_always_did(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {
        "sequence": {"commands": ["cd petclinic-test && ./run-tests-with-tracing.sh"]}})
    steps._sequence(ctx)

    assert not sh.has("start-docker.sh")
    assert sh.first("run-tests-with-tracing.sh") == "cd petclinic-test && ./run-tests-with-tracing.sh"


def test_a_suite_that_could_not_run_says_what_has_to_be_listening(tmp_path, monkeypatch):
    """The page renders a LookupError as "this could not be produced, and here is why",
    which is the difference between a reader who knows what to start and one who only
    learns that a step died."""
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    sh = Recorder([("run-tests-with-tracing.sh", 1, "")])
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {
        "commands": ["cd petclinic-test && ./run-tests-with-tracing.sh"]}}}, dry=False)

    with pytest.raises(LookupError, match="collector"):
        steps._sequence(ctx)


def test_borrowing_a_block_that_is_not_there_is_an_error_and_not_a_silent_skip(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {
        "sequence": {"app": "video", "commands": ["npm run test:sequence"]}})
    with pytest.raises(LookupError, match="steps.video.app"):
        steps._sequence(ctx)


def test_a_red_suite_keeps_the_diagrams_it_drew_and_puts_the_others_back(
        tmp_path, monkeypatch):
    """The commands sweep `generated/` before they refill it, so a suite that dies halfway
    leaves the OTHER suites' diagrams deleted — it neither wrote them nor can put them back.
    Raising on the first failure skipped both the restore and every later command, which is
    how one red Cucumber run took the backend's @GenerateSequence diagram with it and the
    branch was reported as having removed a picture."""
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "Kept.genseq.puml").write_text("@startuml\n@enduml\n")
    monkeypatch.chdir(tmp_path)
    sh = Recorder([
        ("run-tests-with-tracing.sh", 1, ""),
        ("git status --porcelain", 0, " D generated/Gone.genseq.puml\n"
                                      " M generated/Kept.genseq.puml\n"),
    ])
    monkeypatch.setattr(steps, "sh", sh)
    # `has_genseq` asks git, and this fixture is a directory rather than a repository; the
    # question it stands for here is "did anything survive the run", and something did.
    monkeypatch.setattr(steps, "has_genseq", lambda: True)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {"commands": [
        "cd petclinic-test && ./run-tests-with-tracing.sh",
        "cd petclinic-backend && mvn -Pgenseq test",
    ]}}}, dry=False)

    steps._sequence(ctx)          # a red suite is a finding, not a lost tab

    # every command still ran — the backend's diagram is drawn by the second one
    assert sh.has("mvn -Pgenseq test")
    # the deleted one is put back; the modified one is left exactly as the run left it
    restored = sh.first("git checkout --")
    assert "generated/Gone.genseq.puml" in restored
    assert "Kept.genseq.puml" not in restored
    # and the page is told, so the guide cannot present a red run as a clean one
    assert any("RED" in n for n in ctx.notes)
    assert any("restored 1 diagram file" in n for n in ctx.notes)
