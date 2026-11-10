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

import html
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


# ── the screens are shot on the seed, never on what a suite left behind ─────────────
# Eval run 5 (3 Oct 2026): city's Playwright suite wrote into the commit's stack, and the
# design-system audit and the film then shot that same stack. Three of the audit's four
# "changed" screens were test data (`Join 26` → `Join 28 happy pet owners`, an e2e visit),
# and the film opened its sorted list on two junk owners.

def _reused(tmp_path, monkeypatch, app, posts=None, port="63241"):
    """A run whose stack is already up — started by an earlier step of the same run."""
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh url", 0, "http://localhost:51999\n"),
                   ("start-docker.sh up", 0, f"recreated\n   http://localhost:{port}\n")])
    ctx, sh = _ctx(tmp_path, monkeypatch, app=app, recorder=sh)
    monkeypatch.setattr(steps, "answers", lambda url, timeout=3.0: True)
    monkeypatch.setattr(steps, "_post", lambda url, timeout=30.0: (posts.append(url), True)[1]
                        if posts is not None else True)
    return ctx, sh


def test_a_stack_another_step_wrote_into_is_reset_before_it_is_filmed(tmp_path, monkeypatch):
    posts = []
    ctx, sh = _reused(tmp_path, monkeypatch, {**APP, "reset": "/__reset"}, posts)
    steps._video(ctx)
    # The endpoint the page's own Reset button presses, on the instance the film is of.
    assert posts == ["http://localhost:51999/__reset"]
    assert "BASE_URL=http://localhost:51999" in sh.first("record-feature-video.sh")
    # Reused, so left up for the steps after it — the reset is not a reason to tear down.
    assert not sh.has("start-docker.sh up") and not sh.has("start-docker.sh down")


def test_a_reset_that_is_a_command_runs_with_the_instance_named(tmp_path, monkeypatch):
    ctx, sh = _reused(tmp_path, monkeypatch,
                      {**APP, "reset": "./start-docker.sh reset petclinic-{shortsha}"})
    steps._video(ctx)
    assert sh.ran.index(f"./start-docker.sh reset petclinic-{SHORT}") < \
        sh.ran.index(sh.first("record-feature-video.sh"))


def test_without_a_reset_a_stack_found_running_is_recycled_from_its_seed(tmp_path, monkeypatch):
    """Slower than a reset and never wrong: `down` drops the volume, `up` seeds it again —
    and the film is pointed at the port the instance came back on."""
    ctx, sh = _reused(tmp_path, monkeypatch, APP, port="64000")
    steps._video(ctx)
    down = sh.ran.index(f"./start-docker.sh down petclinic-{SHORT}")
    up = sh.ran.index(sh.first("start-docker.sh up"))
    film = sh.ran.index(sh.first("record-feature-video.sh"))
    assert down < up < film
    assert "BASE_URL=http://localhost:64000" in sh.ran[film]
    assert any("app.reset" in n for n in ctx.notes), "the note says how to make it cheap"
    assert sh.ran.count(f"./start-docker.sh down petclinic-{SHORT}") == 1, \
        "found running, so left running for the steps after it"


def test_a_stack_this_step_started_needs_no_second_start(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch)          # nothing up: `up` starts it from the seed
    steps._video(ctx)
    assert sh.ran.count(sh.first("start-docker.sh up")) == 1
    assert sh.ran.index(sh.first("start-docker.sh up")) < \
        sh.ran.index(sh.first("record-feature-video.sh"))


def test_the_design_system_audit_resets_both_sides_before_it_shoots(tmp_path, monkeypatch):
    posts = []
    ctx, sh = _reused(tmp_path, monkeypatch, {**APP, "reset": "/__reset"}, posts)
    ctx.cfg["steps"]["dsaudit"] = {"app": "video"}
    sh.answers.insert(0, ("git rev-parse 1111111", 0, "1" * 40 + "\n"))
    with steps._dsaudit_origins(ctx, ctx.step_cfg("dsaudit"), "1111111") as (new, old):
        assert len(posts) == 2, "both instances were brought back to their seed first"
    assert all(p.endswith("/__reset") for p in posts)


def test_every_step_that_shoots_the_screens_resets_first():
    """Read off the source, the way the convention is kept: a new capture step that forgets
    `clean=True` shoots whatever the last suite left in the database."""
    import inspect
    for name in steps.CAPTURES:
        fn = {"video": steps._video, "dsaudit": steps._dsaudit_origins}[name]
        assert "clean=True" in inspect.getsource(fn), name


def test_say_holds_the_shot_until_its_sentence_is_spoken():
    """Eval run 5's film fell a step behind its own narration: say() returned at once, the
    script clicked on, and "Harry and Beatrix Potter are the two matches" was captioned over
    the no-match screen the next search had already drawn."""
    src = RECORDER.read_text(encoding="utf-8")
    body = src[src.index("const say = async"):src.index("const pause =")]
    tail = body[body.index("cues.push(cue);"):]
    assert "waitForTimeout" in tail and "spokenUntil" in tail


# ── the verdict nobody can lose ───────────────────────────────────────────────────

EXIT3_LOG = """\
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


# ── what the Demo tab shows when the content file said nothing ──────────────────────
# Eval run 5's model wrote no `video` section, so its Demo tab had no Running-app row and
# no linked words in the transcript — while everything behind both was on disk.

def _project(tmp_path, app=None):
    root = tmp_path / "repo"
    (root / ".human-review" / "assets").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True, text=True).stdout.strip()
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    (root / "human-review.json").write_text(json.dumps(
        {"steps": {"video": {"app": app or {**APP, "reset": "/__reset"}}}}))
    out = root / ".human-review"
    (out / "assets" / "feature.cues.json").write_text(json.dumps([
        {"t": 1.0, "text": "The Owners grid is now paginated on the server."},
        {"t": 4.0, "text": "Clearing the search brings every owner back."}]))
    (out / "assets" / "feature.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    return root, out, run("rev-parse", "HEAD"), run("rev-parse", "--short", "HEAD")


def test_the_deployed_app_row_is_the_films_own_app_block(tmp_path):
    root, out, sha, short = _project(tmp_path)
    page = build.video_html({"video": "assets/feature.webm"}, out)
    assert "Running app" in page and "Start App in Docker" in page
    assert html.escape(f"cd {root} && ./start-docker.sh up --ref {sha} --ttl 1800") in page
    assert html.escape(f"./start-docker.sh down petclinic-{short}") in page
    assert 'data-reset="/__reset"' in page
    # The config's commands win over the content file's (441021c, 5 Oct 2026: a hand-typed
    # `cd ~/workspace/petclinic-pr && …` went stale when the folder was renamed); the content
    # file still supplies what the config cannot know, such as its own `reset`.
    mine = build.video_html({"video": "assets/feature.webm",
                             "runtime": {"command": "make up", "reset": "/__wipe"}}, out)
    assert "make up" not in mine and "start-docker.sh up" in mine
    assert 'data-reset="/__wipe"' in mine


def test_a_reset_that_is_a_command_is_not_a_button(tmp_path):
    _, out, _, _ = _project(tmp_path, {**APP, "reset": "./start-docker.sh reset x"})
    page = build.video_html({"video": "assets/feature.webm"}, out)
    assert "Start App in Docker" in page
    assert "start-docker.sh reset" not in page, "a browser cannot press a shell command"


def test_the_screens_this_branch_changed_are_linked_on_the_words_that_name_them(tmp_path):
    _, out, _, _ = _project(tmp_path)
    changed = {"dom": {"added": ["x"], "removed": [], "changed": []}, "elements": {}}
    same = {"dom": {"added": [], "removed": [], "changed": []}, "elements": {}}
    quiet = {"regressions": [], "improvements": []}
    (out / "assets" / "ds-audit.json").write_text(json.dumps({"screens": [
        {"screen": "Owners", "route": "/owners", "delta": changed, "summary": quiet},
        {"screen": "Welcome", "route": "/welcome", "delta": changed, "summary": quiet},
        {"screen": "Vets", "route": "/vets", "delta": same, "summary": quiet}]}))
    page = build.video_html({"video": "assets/feature.webm"}, out)
    assert 'The <a data-app="/owners" href="/owners">Owners</a> grid' in page
    # Changed and never named on film: no caption to ride on, so not printed at all.
    assert "Not filmed." not in page and "/welcome" not in page
    assert "/vets" not in page, "a screen the branch did not change is not linked"


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

def _draws(monkeypatch):
    """The traced run drew one diagram: the step tells what a run drew from what was
    already committed by comparing two snapshots, and these fakes write no file."""
    shots = iter([{}, {"generated/Drawn.genseq.puml": (1, 1)}])
    monkeypatch.setattr(steps, "genseq_stamps", lambda: next(shots))


def _steps_ctx(tmp_path, monkeypatch, cfg_steps):
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _draws(monkeypatch)
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
    assert traced.startswith("export BASE_URL=http://localhost:63241 ")
    # Its Playwright report goes to a folder of its own, never the one `traces` harvests
    # from city's run (eval run 17: overwritten 6 s before the harvest).
    assert "export PLAYWRIGHT_HTML_OUTPUT_DIR=" in traced and "hr-sequence-report-" in traced
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
    assert sh.first("npm run test:cucumber").startswith("export BASE_URL=http://localhost:63241 ")


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
    assert sh.first("run-tests-with-tracing.sh").endswith(
        "; cd petclinic-test && ./run-tests-with-tracing.sh")


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
    (gen / "Kept.genseq.puml").write_text("@startuml\nold\n@enduml\n")
    (gen / "Gone.genseq.puml").write_text("@startuml\ngone\n@enduml\n")
    monkeypatch.chdir(tmp_path)
    ran = []

    def sh(cmd, ctx, check=True, capture=False):
        ran.append(cmd)
        if "run-tests-with-tracing.sh" in cmd:      # sweeps, then dies
            (gen / "Gone.genseq.puml").unlink()
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if "mvn -Pgenseq test" in cmd:              # redraws the backend's picture
            (gen / "Kept.genseq.puml").write_text("@startuml\nnew\n@enduml\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(steps, "sh", sh)
    _draws(monkeypatch)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {"commands": [
        "cd petclinic-test && ./run-tests-with-tracing.sh",
        "cd petclinic-backend && mvn -Pgenseq test",
    ]}}}, dry=False)

    steps._sequence(ctx)          # a red suite is a finding, not a lost tab

    # every command still ran — the backend's diagram is drawn by the second one
    assert any("mvn -Pgenseq test" in c for c in ran)
    # the deleted one is put back from the bytes held, never by `git checkout`
    assert (gen / "Gone.genseq.puml").read_text() == "@startuml\ngone\n@enduml\n"
    assert not any("git checkout" in c for c in ran)
    # and the page is told, so the guide cannot present a red run as a clean one
    assert any("RED" in n for n in ctx.notes)
    assert any("restored 1 diagram file" in n for n in ctx.notes)


# ── eval run 6: the step left the branch it reviewed dirty, and a degraded trace in it ──
# Six tracked `generated/*.genseq.*` files were left modified, and the re-traced
# AddVisitApiTest had lost NotificationService and the SMS gateway (the in-process test
# could not reach the traced stack's notification-service) while the band said "the
# diagrams below are this run's".

COMMITTED_ADD_VISIT = """@startuml
participant Test
participant Backend
participant DB
participant NotificationService
participant "SMS gateway"
Test -> Test: [[src://AddVisitApiTest.java:85{line} and a visit is added ↗]]
Test -> Backend: [[genseq://09akplx{body} Add a visit\\nPOST /api/owners/{ownerId}/pets/{petId}/visits ⊕]]
Backend -> DB: [[genseq://09ukh7p{sql} insert for Visit ⊕]]
Backend -> NotificationService: [[genseq://0a5r3wo{body} POST /api/notifications/visit-booked ⊕]]
NotificationService -> "SMS gateway": [[src://SmsGateway.java:23{open} send-sms ↗]]
NotificationService --> Backend: 202
Backend --> Test: 201
@enduml
"""
RETRACED_ADD_VISIT = """@startuml
participant Test
participant Backend
participant DB
Test -> Test: [[src://AddVisitApiTest.java:85{line} and a visit is added ↗]]
Test -> Backend: [[genseq://1jmv4vp{body} Add a visit\\nPOST /api/owners/{ownerId}/pets/{petId}/visits ⊕]]
Backend -> DB: [[genseq://0kzn69z{sql} insert for Visit ⊕]]
Backend --> Test: 201
@enduml
"""


def _git_repo_with(tmp_path, files: dict) -> None:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)


def test_a_retrace_that_lost_calls_is_flagged_and_the_tree_is_left_clean(
        tmp_path, monkeypatch):
    rel = "generated/AddVisitApiTest.java.adds-a-visit.genseq.puml"
    side = rel[:-len(".puml")] + ".json"
    _git_repo_with(tmp_path, {rel: COMMITTED_ADD_VISIT, side: '{"details": {"a": 1}}\n',
                              ".gitignore": ".human-review/\n"})
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def sh(cmd, ctx, check=True, capture=False):
        if "mvn" in cmd:                            # the regeneration
            (tmp_path / rel).write_text(RETRACED_ADD_VISIT)
            (tmp_path / side).write_text('{"details": {"b": 2}}\n')
            (tmp_path / "generated/New.java.x.genseq.puml").write_text("@startuml\n@enduml\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {
        "commands": ["cd petclinic-backend && mvn -o -Pgenseq test -Dgroups=genseq"]}}},
        dry=False)

    steps._sequence(ctx)

    # The branch is exactly as the run found it: tracked bytes back, nothing new left over.
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                            text=True).stdout
    assert status == "", status
    # What the run drew is in the review directory, pinned to the commit it traced.
    overlay = tmp_path / ".human-review/assets/genseq"
    assert (overlay / rel).read_text() == RETRACED_ADD_VISIT
    assert (overlay / side).read_text() == '{"details": {"b": 2}}\n'
    assert (overlay / "generated/New.java.x.genseq.puml").is_file()
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    assert (overlay / ".head").read_text().strip() == head.stdout.strip()
    # And the loss is computed against the committed diagram, not left for a judge to find.
    verdict = json.loads(Path(".human-review/assets/sequence.verdict.json").read_text())
    assert verdict["state"] == "degraded"
    assert verdict["lost"] == [{
        "diagram": rel, "participants": ["NotificationService", "SMS gateway"],
        "calls": ["Backend → NotificationService: POST /api/notifications/visit-booked",
                  "NotificationService → SMS gateway: send-sms"]}]
    assert any("LOST" in n and "NotificationService" in n for n in ctx.notes)


def test_a_retrace_that_matches_the_committed_diagram_loses_nothing():
    """The per-run ids on every handle move each time; the calls do not."""
    moved_ids = COMMITTED_ADD_VISIT.replace("09akplx", "zzzzzzz").replace("0a5r3wo", "yyyyyyy")
    assert steps.seq_inventory(moved_ids) == steps.seq_inventory(COMMITTED_ADD_VISIT)
    parts, calls = steps.seq_inventory(COMMITTED_ADD_VISIT)
    assert parts == ["Test", "Backend", "DB", "NotificationService", "SMS gateway"]
    assert "Backend → DB: insert for Visit" in calls
    assert not any(c.startswith("Test → Test") for c in calls), "a sentence is not a call"
    assert not any("202" in c for c in calls), "a response is not a call"


def test_a_found_message_and_a_controller_line_are_not_calls_a_retrace_can_lose():
    """The generator's version-3 shapes: `[-> Browser` is where the frontend method starts
    (no lifeline `[`), and a request arrow may name its controller on a third line — where
    the call lands, which a picture drawn without that option has not lost."""
    v3 = (Path(__file__).resolve().parent.parent / "puml-diff" / "testdata"
          / "seq_v3_before.puml").read_text(encoding="utf-8")
    parts, calls = steps.seq_inventory(v3)
    assert parts == ["Browser", "Backend", "DB"]
    assert calls == ["Browser → Backend: Get an owner by ID GET /api/owners/{ownerId}",
                     "Backend → DB: select owners"]
    plain = v3.replace(
        "\\n[[src://be/src/main/java/x/OwnerRestController.java:41{Click to open "
        "OwnerRestController.getOwner} → OwnerRestController.getOwner ↗]]", "")
    assert plain != v3 and steps.seq_inventory(plain) == (parts, calls)


def test_the_delta_is_drawn_from_this_runs_copy_while_the_tree_holds_the_committed_one(
        tmp_path):
    """`puml-diff.sh` used to read the work tree, which is exactly what the step no longer
    leaves dirty: the re-traced picture and its sidecar are read from the review directory."""
    rel = "generated/AddVisitApiTest.java.adds-a-visit.genseq.puml"
    side = rel[:-len(".puml")] + ".json"
    _git_repo_with(tmp_path, {rel: COMMITTED_ADD_VISIT, side: '{"details": {"a": 1}}\n',
                              ".gitignore": ".human-review/\n"})
    overlay = tmp_path / ".human-review/assets/genseq"
    (overlay / "generated").mkdir(parents=True)
    (overlay / rel).write_text(RETRACED_ADD_VISIT)
    (overlay / side).write_text('{"details": {"b": 2}}\n')
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True).stdout.strip()
    (overlay / ".head").write_text(head + "\n")
    out = tmp_path / ".human-review/assets/diagrams"
    subprocess.run([str(HERE / "puml-diff.sh"), "HEAD", str(out)], cwd=tmp_path, check=True,
                   capture_output=True)
    header, line = (out / "MANIFEST.tsv").read_text().splitlines()
    row = dict(zip(header.split("\t"), line.split("\t")))
    assert row["source"] == rel and row["status"] == "modified", row
    assert (out / row["new_details"]).read_text() == '{"details": {"b": 2}}\n'
    assert "NotificationService" in (out / row["diff_puml"]).read_text(), \
        "the lost lifeline is drawn as removed"
    # Another HEAD's copy is ignored: the committed diagram is unchanged against HEAD.
    (overlay / ".head").write_text("0" * 40 + "\n")
    subprocess.run([str(HERE / "puml-diff.sh"), "HEAD", str(out)], cwd=tmp_path, check=True,
                   capture_output=True)
    assert len((out / "MANIFEST.tsv").read_text().splitlines()) == 1


def test_a_skipped_run_drops_the_previous_runs_drawings(tmp_path, monkeypatch):
    """A run that drew nothing shows the committed diagrams; last time's copy must not win
    over them on the page."""
    (tmp_path / ".human-review/assets/genseq/generated").mkdir(parents=True)
    (tmp_path / ".human-review/assets/genseq/generated/Old.genseq.puml").write_text("x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(steps, "sh", Recorder([("mvn", 1, "boom\n")]))
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {"commands": ["mvn test"]}}},
                    dry=False)
    with pytest.raises(LookupError):
        steps._sequence(ctx)
    assert not (tmp_path / ".human-review/assets/genseq/generated/Old.genseq.puml").exists()


# ── eval run 8: the branch's own tests are traced too, untagged ─────────────────────
# Only tests carrying @generate_sequence / @GenerateSequence were traced, so none of the
# paging and sorting scenarios the branch added got a picture; two of the three diagrams
# were visit flows that touched the changed GET /api/owners only in their setup.

SELECT = {"max": 4, "suites": {
    "e2e": {"files": ["petclinic-test/src/**/*.feature"], "item": "{file}::{name}",
            "join": "\n", "max": 3},
    "java": {"files": ["petclinic-backend/src/test/java/**/*.java"],
             "exclude": ["**/genseq/**"], "item": "{class}#{name}", "join": ","}}}

OWNER_SEARCH = """Feature: Search owners
  @generate_sequence
  Scenario: Searching with an empty last name shows the first page
    When I open the owners page

  Scenario: Paging forward reaches every owner
    When I page forward

  Scenario: Sorting by city, then reversing it
    When I sort by "City"
"""


def _manifest_tests():
    feat = "petclinic-test/src/owner-search.feature"
    lst = "petclinic-backend/src/test/java/x/rest/OwnerListTest.java"
    return {"tests": [
        {"path": feat, "name": "Searching with an empty last name shows the first page",
         "line": 3, "status": "modified"},
        {"path": feat, "name": "Paging forward reaches every owner", "line": 6,
         "status": "added"},
        {"path": feat, "name": "Sorting by city, then reversing it", "line": 9,
         "status": "added"},
        {"path": "petclinic-backend/src/test/java/x/repo/MigrationTest.java",
         "name": "freshDatabase_hasTheIndexes", "line": 30, "status": "added"},
        {"path": lst, "name": "defaultRequest_returnsFirstTen", "line": 40, "status": "added"},
        {"path": lst, "name": "requestedPage_isTheSlice", "line": 50, "status": "added"},
        {"path": lst, "name": "getAll", "line": 60, "status": "modified"},
        {"path": "petclinic-frontend/src/app/owner-list.component.spec.ts",
         "name": "opens on the first page", "line": 52, "status": "added"},
        {"path": "petclinic-backend/src/test/java/x/genseq/Rest.java", "name": "helper",
         "line": 5, "status": "added"},
        {"path": feat, "name": "an old one", "line": 1, "status": "unchanged"},
    ]}


def test_the_branchs_new_tests_are_picked_untagged_and_the_suites_take_turns(
        tmp_path, monkeypatch):
    (tmp_path / "petclinic-test/src").mkdir(parents=True)
    (tmp_path / "petclinic-test/src/owner-search.feature").write_text(OWNER_SEARCH)
    monkeypatch.chdir(tmp_path)

    sel = steps.select_traced(_manifest_tests(), SELECT)

    picked = [(t["suite"], t["name"]) for t in sel["picked"]]
    # The tagged scenario is traced by its tag and takes no slot; the suites alternate; the
    # file the branch wrote the most tests in (OwnerListTest) goes before the lone migration
    # test, and an added test before an edited one.
    assert picked == [("e2e", "Paging forward reaches every owner"),
                      ("java", "defaultRequest_returnsFirstTen"),
                      ("e2e", "Sorting by city, then reversing it"),
                      ("java", "requestedPage_isTheSlice")]
    assert {t["why"] for t in sel["picked"]} == {"written by this branch"}
    left = {t["name"]: t["left"] for t in sel["left"]}
    assert left["getAll"] == left["freshDatabase_hasTheIndexes"] == \
        "over the cap of 4 traced tests"
    assert left["opens on the first page"] == "no traced suite runs this file"
    assert left["helper"] == "no traced suite runs this file", "excluded by the suite"
    assert "an old one" not in left, "an unchanged test is no candidate"
    values = steps.selection_values(sel, SELECT)
    assert values == {
        "e2e": "owner-search.feature::Paging forward reaches every owner\n"
               "owner-search.feature::Sorting by city, then reversing it",
        "java": "OwnerListTest#defaultRequest_returnsFirstTen,OwnerListTest#requestedPage_isTheSlice"}


def test_a_suites_own_cap_and_an_empty_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sel = steps.select_traced(_manifest_tests(), {**SELECT, "max": 6, "suites": {
        **SELECT["suites"], "java": {**SELECT["suites"]["java"], "max": 1}}})
    assert [t["suite"] for t in sel["picked"]].count("java") == 1
    none = steps.select_traced({"tests": []}, SELECT)
    assert none["picked"] == [] and steps.selection_values(none, SELECT) == {"e2e": "", "java": ""}


def test_the_selection_reaches_the_commands_as_one_shell_word_and_when_gates_a_run(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {"sequence": {"select": SELECT, "commands": [
        "cd petclinic-test && GENSEQ_SELECT={tests.e2e} ./run-tests-with-tracing.sh",
        {"run": "cd petclinic-backend && mvn -Pgenseq test -Dtest={tests.java}", "when": "java"},
        {"run": "echo never {tests.nope}", "when": "nope"},
    ]}})
    manifest = _manifest_tests()
    manifest["tests"] = [t for t in manifest["tests"] if "java" not in t["path"]
                         or "OwnerListTest" in t["path"]]
    monkeypatch.setattr(steps, "_test_manifest", lambda ctx: manifest)

    steps._sequence(ctx)

    traced = sh.first("run-tests-with-tracing.sh")
    assert ("GENSEQ_SELECT='owner-search.feature::Paging forward reaches every owner\n"
            "owner-search.feature::Sorting by city, then reversing it' ") in traced
    # No feature file on disk here, so its tag is not seen: the edited scenario is a
    # candidate too, and an added test goes first, so it is the one over the cap.
    assert sh.first("-Dtest=").endswith(
        "-Dtest='OwnerListTest#defaultRequest_returnsFirstTen,OwnerListTest#requestedPage_isTheSlice'")
    assert not sh.has("echo never"), "a `when` suite with nothing picked boots nothing"
    doc = json.loads(Path(".human-review/assets/sequence.selection.json").read_text())
    assert [t["name"] for t in doc["picked"]][:2] == [
        "Paging forward reaches every owner", "defaultRequest_returnsFirstTen"]
    assert "Searching with an empty last name shows the first page" in \
        [t["name"] for t in doc["left"]]
    assert any("also traced 4 test(s) this branch wrote or edited" in n for n in ctx.notes)
    assert [r["command"] for r in doc["runs"]] == [
        "cd petclinic-test && GENSEQ_SELECT={tests.e2e} ./run-tests-with-tracing.sh",
        "cd petclinic-backend && mvn -Pgenseq test -Dtest={tests.java}"]
    assert all(r["seconds"] >= 0 for r in doc["runs"])


def test_without_select_the_placeholders_are_empty_and_no_selection_is_left_behind(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {"sequence": {"commands": [
        "cd petclinic-test && GENSEQ_SELECT={tests.e2e} ./run-tests-with-tracing.sh"]}})
    stale = Path(".human-review/assets/sequence.selection.json")
    stale.write_text("{}")
    monkeypatch.setattr(steps, "_test_manifest", lambda ctx: pytest.fail("not asked"))

    steps._sequence(ctx)

    assert sh.first("run-tests-with-tracing.sh").endswith(
        "; cd petclinic-test && GENSEQ_SELECT='' ./run-tests-with-tracing.sh")
    assert not stale.exists()


def test_each_traced_command_records_how_long_it_took(tmp_path, monkeypatch):
    """The cost of tracing more tests has to be measurable from the verdict, per command."""
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {"sequence": {"commands": ["mvn x"]}})
    runs: list = []
    steps._run_traced(ctx, {}, ["mvn x"], runs, {})
    assert runs[0]["seconds"] >= 0 and runs[0]["command"] == "mvn x"


# ── a missing environment is a skip with a reason, never a red step ────────────────
# hr-try-4 (2 Oct 2026): the tracing script found no collector and aborted before running
# anything, Maven went red only because a Cucumber suite class matched no `genseq` tag, and
# the step still reported `ran` with a RED note — because committed diagrams existed, and
# "a diagram exists" was the question asked instead of "this run drew one".

TRACING_ABORT = ("\x1b[1;33m[tracing]\x1b[0m The stack is not fully up. Start the missing "
                 "pieces, then re-run:\n   • OTLP collector (:4318)    → ./start-grafana.sh\n"
                 "\x1b[1;31m[tracing] aborting — nothing was started or stopped.\x1b[0m\n")
MVN_NO_TESTS = (
    "[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0 -- in x.AddVisitApiTest\n"
    "[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.007 s "
    "<<< FAILURE! -- in x.functional.FunctionalCucumberTest\n"
    "[ERROR] x.functional.FunctionalCucumberTest -- Time elapsed: 0.007 s <<< ERROR!\n"
    "org.junit.platform.suite.engine.NoTestsDiscoveredException: Suite "
    "[x.functional.FunctionalCucumberTest] did not discover any tests\n"
    "[ERROR]   FunctionalCucumberTest » NoTestsDiscovered Suite "
    "[x.functional.FunctionalCucumberTest] did not discover any tests\n"
    "[INFO] BUILD FAILURE\n")
GENERATOR_SKIPPED_ALL = (
    "> ts-node src/genseq/generate.ts\n"
    "⚠️  \"Add a visit to an existing pet from the owner detail page\": fetch failed — skipped\n"
    "⚠️  \"adds a visit to an existing pet\": fetch failed — skipped\n"
    "📊 Generated 0 diagram(s)\n")
HR_TRY_4 = ["cd petclinic-test && ./run-tests-with-tracing.sh",
            "cd petclinic-backend && mvn -o -Pgenseq test -Dgroups=genseq",
            "cd petclinic-test && GENSEQ_REFRESH=1 npm run trace:diagram"]


def _committed_diagram(tmp_path):
    gen = tmp_path / "generated"
    gen.mkdir()
    (gen / "Committed.genseq.puml").write_text("@startuml\n@enduml\n")


def test_suites_that_could_not_start_are_skipped_with_their_reason_not_red(
        tmp_path, monkeypatch):
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    _committed_diagram(tmp_path)          # what made it "ran, RED" before
    monkeypatch.chdir(tmp_path)
    sh = Recorder([("run-tests-with-tracing.sh", 1, TRACING_ABORT),
                   ("mvn -o -Pgenseq", 1, MVN_NO_TESTS),
                   ("trace:diagram", 0, GENERATOR_SKIPPED_ALL)])
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {"commands": HR_TRY_4}}}, dry=False)

    row = steps.run_step("sequence", None, "sequence", None, steps._sequence, ctx)

    assert row["status"] == steps.SKIPPED, row
    assert "aborting — nothing was started" in row["reason"]
    assert "\x1b[" not in row["reason"]
    assert "FunctionalCucumberTest discovered no tests under the tag filter" in row["reason"]
    assert "collector" in row["reason"]
    verdict = json.loads(Path(".human-review/assets/sequence.verdict.json").read_text())
    assert verdict["state"] == "skipped"
    # Eval run 5: the generator exited 0 after skipping every scenario, and the band said
    # `passed` under "drew no diagram". A zero exit that drew nothing is its own outcome.
    assert [r["outcome"] for r in verdict["runs"]] == ["failed", "no-tests", steps.DREW_NOTHING]
    assert verdict["runs"][2]["detail"] == \
        "exit 0, and drew no diagram — “fetch failed — skipped” ×2"
    assert "passed" not in row["reason"]


def test_a_tag_filter_that_matched_nothing_is_not_a_failure():
    outcome, said = steps.suite_outcome(1, MVN_NO_TESTS)
    assert outcome == steps.NO_TESTS
    assert said == "FunctionalCucumberTest discovered no tests under the tag filter"
    assert steps.suite_outcome(1, "Error: No tests found.\n")[0] == steps.NO_TESTS
    # One real failure beside it and the command is red: an empty filter must not
    # launder a broken test.
    broken = MVN_NO_TESTS + ("[ERROR] x.AddVisitApiTest.adds -- Time elapsed: 1 s <<< FAILURE!\n"
                             "org.opentest4j.AssertionFailedError: expected 1\n")
    assert steps.suite_outcome(1, broken)[0] == steps.FAILED
    assert steps.suite_outcome(0, "anything")[0] == steps.RAN


def test_a_tag_filter_that_matched_nothing_beside_drawn_diagrams_is_a_note(
        tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {"sequence": {"commands": HR_TRY_4[1:2]}})
    monkeypatch.setattr(steps, "sh", Recorder([("mvn -o -Pgenseq", 1, MVN_NO_TESTS)]))
    steps._sequence(ctx)                  # ran: no LookupError, no RED

    assert not any("RED" in n for n in ctx.notes)
    assert any("tag filter that matched nothing" in n for n in ctx.notes)
    verdict = json.loads(Path(".human-review/assets/sequence.verdict.json").read_text())
    assert verdict["state"] == "notests"


def test_what_the_suites_require_is_probed_before_anything_runs(tmp_path, monkeypatch):
    import socket
    with socket.socket() as s:            # a port nothing listens on any more
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    sh = Recorder([])
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {
        "commands": HR_TRY_4,
        "requires": [{"url": f"tcp://127.0.0.1:{port}", "what": "OTLP collector"}]}}},
        dry=False)

    with pytest.raises(LookupError, match="OTLP collector"):
        steps._sequence(ctx)
    assert sh.ran == [], "nothing is run against an environment known to be missing"
    verdict = json.loads(Path(".human-review/assets/sequence.verdict.json").read_text())
    assert verdict["missing"] == [f"OTLP collector (tcp://127.0.0.1:{port})"]


# ── a traced stack of the run's own: every address it published, not only the app's ───
# The sequence step on a host stack was red or skipped on every eval run: it needs Grafana
# on :3300 and a collector on :4318 beside the app, and those are one global pair that two
# branches traced at once would share. petclinic's `start-docker.sh up --otel` puts a Tempo
# inside the instance — on host-picked ports, which only `ports` can say afterwards.

SEQ_APP = {
    "up": "./start-docker.sh up --ref {sha} --name petclinic-{shortsha}-otel --otel --fresh",
    "vars": "./start-docker.sh ports petclinic-{shortsha}-otel",
    "down": "./start-docker.sh down petclinic-{shortsha}-otel",
}


def _seq_ctx(tmp_path, monkeypatch, ports_out, requires=None):
    ctx, _ = _steps_ctx(tmp_path, monkeypatch, {"sequence": {
        "app": SEQ_APP, "commands": HR_TRY_4,
        **({"requires": requires} if requires is not None else {})}})
    sh = Recorder([("git rev-parse HEAD", 0, SHA + "\n"),
                   ("git rev-parse --short HEAD", 0, SHORT + "\n"),
                   ("start-docker.sh up", 0, "✅ ready\n   Grafana  http://localhost:61000\n"
                                             "   http://localhost:63241\n"),
                   ("start-docker.sh ports", 0, ports_out)])
    monkeypatch.setattr(steps, "sh", sh)
    return ctx, sh


@pytest.mark.parametrize("app_saying", ["any"], indirect=True)
def test_the_traced_suites_get_every_address_the_instance_published(
        app_saying, tmp_path, monkeypatch):
    ports = ("PETCLINIC_INSTANCE=petclinic-904aa051-otel\n"
             "BASE_URL=http://127.0.0.1:63241\n"
             f"GRAFANA_URL={app_saying}\n"
             f"OTEL_EXPORTER_OTLP_ENDPOINT={app_saying}\n")
    ctx, sh = _seq_ctx(tmp_path, monkeypatch, ports, requires=[
        {"url": "{GRAFANA_URL}/api/health", "what": "Grafana"},
        {"url": "{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces", "what": "OTLP collector"}])
    steps._sequence(ctx)

    assert sh.first("start-docker.sh up").endswith(
        f"up --ref {SHA} --name petclinic-{SHORT}-otel --otel --fresh")
    assert sh.has(f"start-docker.sh ports petclinic-{SHORT}-otel")
    for cmd in HR_TRY_4:          # the Java suite exports to it as much as the browser does
        ran = sh.first(cmd)
        assert ran.endswith(f"; {cmd}"), "exported for the whole command, not only its `cd`"
        exported = ran.split("; ", 1)[0] + " "   # the instance's export, before any of the step's own
        assert f"GRAFANA_URL={app_saying} " in exported
        assert f"OTEL_EXPORTER_OTLP_ENDPOINT={app_saying} " in exported
        # The scraped `{url}` is the LAST one `up` printed, never the Grafana line above
        # it, and the instance's own 127.0.0.1 spelling is what the commands end up with.
        assert exported.index("BASE_URL=http://127.0.0.1:63241") > \
            exported.index("BASE_URL=http://localhost:63241")
    assert sh.has(f"start-docker.sh down petclinic-{SHORT}-otel")


def test_an_instance_without_the_trace_store_is_a_skip_and_is_still_taken_down(
        tmp_path, monkeypatch):
    """A commit whose stack predates `--otel` comes up fine and publishes no Grafana. Run
    anyway, the suites pass and draw nothing — or, worse, find the operator's :3300."""
    ctx, sh = _seq_ctx(tmp_path, monkeypatch, "BASE_URL=http://127.0.0.1:63241\n",
                       requires=[{"url": "{GRAFANA_URL}/api/health", "what": "Grafana"}])
    with pytest.raises(LookupError, match="Grafana"):
        steps._sequence(ctx)

    assert not any(sh.has(c) for c in HR_TRY_4), "nothing runs against a store that is missing"
    assert sh.has(f"start-docker.sh down petclinic-{SHORT}-otel")
    verdict = json.loads(Path(".human-review/assets/sequence.verdict.json").read_text())
    assert verdict["state"] == "skipped"
    assert verdict["missing"] == ["Grafana ({GRAFANA_URL}/api/health)"]


def test_a_vars_command_that_says_nothing_stops_the_step_rather_than_fall_back(
        tmp_path, monkeypatch):
    """Without the instance's addresses the commands would use their defaults — :3300 and
    :4318, the shared host stack — which is the hazard the whole block removes."""
    ctx, sh = _seq_ctx(tmp_path, monkeypatch, "❌ no such instance\n")
    with pytest.raises(RuntimeError, match="NAME=value"):
        steps._sequence(ctx)
    assert not any(sh.has(c) for c in HR_TRY_4)
    assert sh.has(f"start-docker.sh down petclinic-{SHORT}-otel")


def test_the_env_reaches_every_command_after_a_cd_not_only_the_cd(tmp_path):
    """Found by the first real run of the block: `GRAFANA_URL=… cd petclinic-test && ./run…`
    gave the variable to `cd` alone, and the suite searched the host's :3300 instead."""
    inst = steps.AppInstance(env="GRAFANA_URL=http://127.0.0.1:1 A='two words' ")
    got = subprocess.run(inst.command(f'cd {tmp_path} && echo "$GRAFANA_URL|$A|$PWD"'),
                         shell=True, capture_output=True, text=True)
    assert got.stdout.strip() == f"http://127.0.0.1:1|two words|{tmp_path}"
    assert steps.AppInstance().command("cd x && y") == "cd x && y"


def test_only_assignments_are_read_off_the_vars_output():
    assert steps.app_vars("\x1b[1mbanner\x1b[0m\nA=1\n  B=http://x:2/api \nnot one\nC=\n") == {
        "A": "1", "B": "http://x:2/api", "C": ""}


def test_a_clean_run_takes_the_previous_runs_band_away(tmp_path, monkeypatch):
    ctx, sh = _steps_ctx(tmp_path, monkeypatch, {"sequence": {"commands": ["npm run x"]}})
    stale = Path(".human-review/assets/sequence.verdict.json")
    stale.write_text('{"state": "red"}')
    steps._sequence(ctx)
    assert not stale.exists()


def test_the_harness_never_calls_the_projects_own_api():
    """A probe of petclinic's `/api/owners` inside the generic harness died on a branch
    that paginated it (hr-try-4), before the feature script ran. The project's data is
    the feature script's business."""
    import re
    src = (Path(__file__).resolve().parent / "record-feature-video.sh").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    assert not re.search(r"apiUrl\s*\+\s*[\"'`]/api/", code)


def test_the_single_quoted_node_block_is_valid_javascript():
    """05b5245 put apostrophes in a comment inside `node -e '…'`: the shell closed the quote
    early and the recorder did not even parse (eval run 5). `bash -n` does not catch it, so
    the block is cut out exactly as the shell would hand it to node and checked by node."""
    import re
    import shutil
    import subprocess
    src = (Path(__file__).resolve().parent / "record-feature-video.sh").read_text()
    m = re.search(r"node -e '\n(.*?)\n' \"\$BASE_URL\"", src, re.S)
    assert m, "the node -e block moved; update this test"
    assert "'" not in m.group(1), "an apostrophe inside the single-quoted node block"
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", "-"], input=m.group(1), text=True,
                           capture_output=True)
        assert r.returncode == 0, r.stderr
