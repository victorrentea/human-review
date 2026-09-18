#!/usr/bin/env python3
"""Whose two builds the design-system audit is of — and that both are torn down.

The audit is the only step that needs *both* sides of the branch running at the same
time, and that is exactly why it never ran. `steps.dsaudit` named two fixed ports,
:4300 and :4301, which is not a configuration: it is a promise about somebody else's
machine. Nothing in the pipeline started those builds, so `_dsaudit_prereq` found
nothing answering, the step was skipped on every single run, and the UX tab arrived
with a note explaining that it was empty. Victor read that page and said the obvious
thing out loud — "there is no audit for design system I see right now" — about a branch
whose whole subject is a control moved onto the design system's own component.

So the step starts them, the way `video` starts the one it films: `steps.dsaudit.app`
is the same block (`"video"` borrows it), expanded twice — once with HEAD's slots, once
with the merge-base's. The ports are ephemeral on purpose, which is what lets two builds
of one repository be up together, so they can only be read back out of what `up` printed.

What is worth pinning here is not the happy path but the arithmetic of two instances:

* the *second* ref is the merge-base, never `origin/main` as it points today;
* two instances mean two `down`s, and both run even when the audit dies in the middle —
  a stack left up is 700MB and a port, and the next run would find it answering;
* the first instance is torn down even when the *second* one refuses to start, which is
  the leak a single `try/finally` around both would not have caught;
* a project with no `app` block keeps the old behaviour exactly, because that is what
  every other project using this step still does.

Run with:  python3 -m pytest test_dsaudit_app.py
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


steps = _load("run-steps")

HEAD_SHA = "e9e23b6a16aa41d1fc45ff8596d8eed04fea50e4"
HEAD_SHORT = "e9e23b6a"
BASE_SHA = "fff6bd15b128ec40e6944a85a9b6713e4ee63232"
BASE_SHORT = "fff6bd15"

APP = {
    "up": "./start-docker.sh up --ref {sha} --ttl 1800",
    "url": "./start-docker.sh url petclinic-{shortsha}",
    "down": "./start-docker.sh down petclinic-{shortsha}",
}

SCREENS = {"Book a visit": "pets/11/visits/add", "Edit a pet": "pets/11/edit"}


class Recorder:
    """A stand-in for `sh` that answers by substring and remembers what it was asked.

    The same shape `test_feature_video_app.py` uses, deliberately: these two steps share
    `app_instance`, and a divergent fake would hide a change that broke only one of them.
    """

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

    def all(self, needle) -> list[str]:
        return [c for c in self.ran if needle in c]

    def has(self, needle) -> bool:
        return any(needle in c for c in self.ran)


#: What git is asked, and what it says. `merge-base` answers before `rev-parse` in the
#: list because `sh` is matched by substring and the merge-base command contains neither.
GIT = [
    ("git merge-base", 0, BASE_SHA + "\n"),
    ("git rev-parse --abbrev-ref HEAD", 0, "test-pr\n"),
    ("git rev-parse --short HEAD", 0, HEAD_SHORT + "\n"),
    (f"git rev-parse --short {BASE_SHA}", 0, BASE_SHORT + "\n"),
    ("git rev-parse HEAD", 0, HEAD_SHA + "\n"),
    (f"git rev-parse {BASE_SHA}", 0, BASE_SHA + "\n"),
    ("git diff --name-only", 0, ""),
]

UP_NEW = (f"up --ref {HEAD_SHA}", 0, "🐳 building\n   http://localhost:63241\n")
UP_OLD = (f"up --ref {BASE_SHA}", 0, "🐳 building\n   http://localhost:63999\n")


def _ctx(tmp_path, monkeypatch, app=APP, recorder=None, cfg_extra=None):
    """A run directory, a `dsaudit` block with (or without) an `app`, and a faked shell."""
    (tmp_path / ".human-review" / "assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    sh = recorder or Recorder([UP_NEW, UP_OLD, *GIT])
    monkeypatch.setattr(steps, "sh", sh)
    ds = {"label-old": "main", "source": ["petclinic-frontend/src"], "screens": SCREENS,
          **({"app": app} if app else {}), **(cfg_extra or {})}
    return steps.Ctx("origin/main", {"steps": {"dsaudit": ds, "video": {"app": APP}}},
                     dry=False), sh


# ── two builds, started by the run ────────────────────────────────────────────────

def test_the_run_starts_both_sides_and_audits_the_ports_they_printed(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch)
    steps._dsaudit(ctx)

    ups = sh.all("start-docker.sh up")
    assert len(ups) == 2, "one instance per side of the branch"
    # HEAD from git, and the *merge-base* for the other side — not `origin/main` as it
    # points today, which would attribute to this branch everything that landed on main
    # since it forked.
    assert ups[0].endswith(f"up --ref {HEAD_SHA} --ttl 1800")
    assert ups[1].endswith(f"up --ref {BASE_SHA} --ttl 1800")

    audit = sh.first("ds-audit.py --base-new")
    # The host picks both ports so that two builds of one repository can be up together,
    # so the only place the addresses can come from is what each `up` printed.
    assert "--base-new http://localhost:63241" in audit
    assert "--base-old http://localhost:63999" in audit
    assert '--label-new "test-pr"' in audit
    assert "--label-old main" in audit


def test_both_instances_are_torn_down(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch)
    steps._dsaudit(ctx)
    downs = sh.all("start-docker.sh down")
    assert sorted(downs) == sorted([f"./start-docker.sh down petclinic-{HEAD_SHORT}",
                                    f"./start-docker.sh down petclinic-{BASE_SHORT}"])


def test_both_are_torn_down_when_the_audit_itself_dies(tmp_path, monkeypatch):
    """A stack left up outlives the run — and it answers, which is how the wrong build
    gets audited next time."""
    sh = Recorder([UP_NEW, UP_OLD, ("ds-audit.py --base-new", 1, ""), *GIT])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(RuntimeError):
        steps._dsaudit(ctx)
    assert len(sh.all("start-docker.sh down")) == 2


def test_the_first_instance_is_torn_down_when_the_second_will_not_start(tmp_path, monkeypatch):
    """The leak a single `try/finally` around both would not have caught: one `up`
    succeeded, the other did not, and the successful one is nobody's to stop but ours."""
    sh = Recorder([UP_NEW, (f"up --ref {BASE_SHA}", 1, ""), *GIT])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(RuntimeError, match="would not start"):
        steps._dsaudit(ctx)
    assert sh.all("start-docker.sh down") == [
        f"./start-docker.sh down petclinic-{HEAD_SHORT}"]
    assert not sh.has("ds-audit.py --base-new"), "nothing was audited"


def test_the_instance_names_use_gits_own_abbreviation(tmp_path, monkeypatch):
    """`--short` is `core.abbrev` wide, which grows with the repository. A hardcoded
    seven would have `down` naming an instance that does not exist — twice over here."""
    ctx, sh = _ctx(tmp_path, monkeypatch)
    steps._dsaudit(ctx)
    assert sh.has("git rev-parse --short HEAD")
    assert sh.has(f"git rev-parse --short {BASE_SHA}")


def test_an_instance_that_prints_no_url_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    """Falling back to :4300/:4301 here is the original bug wearing a different hat."""
    sh = Recorder([UP_NEW, (f"up --ref {BASE_SHA}", 0, "started, no address\n"),
                   ("start-docker.sh url", 0, "nothing here either\n"), *GIT])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(RuntimeError, match="printed no URL"):
        steps._dsaudit(ctx)
    assert not sh.has("ds-audit.py --base-new")


def test_the_note_says_which_two_builds_were_compared(tmp_path, monkeypatch):
    ctx, _ = _ctx(tmp_path, monkeypatch)
    steps._dsaudit(ctx)
    note = next(n for n in ctx.notes if "compared" in n)
    assert "http://localhost:63241" in note and "http://localhost:63999" in note
    assert BASE_SHORT in note


def test_the_app_block_can_be_borrowed_from_video(tmp_path, monkeypatch):
    """One `up` builds the one stack both steps want; repeating it is a second place to
    forget to change."""
    ctx, sh = _ctx(tmp_path, monkeypatch, app="video")
    steps._dsaudit(ctx)
    assert len(sh.all("start-docker.sh up")) == 2


def test_a_branch_that_is_its_own_merge_base_is_skipped_not_audited(tmp_path, monkeypatch):
    """Two instances of one commit compare equal, and "no screen changed" is the same
    sentence a clean audit prints. The step says why instead."""
    sh = Recorder([("git merge-base", 0, HEAD_SHA + "\n"),
                   ("git rev-parse --abbrev-ref HEAD", 0, "test-pr\n"),
                   ("git rev-parse --short", 0, HEAD_SHORT + "\n"),
                   ("git rev-parse", 0, HEAD_SHA + "\n"),
                   ("git diff --name-only", 0, "")])
    ctx, sh = _ctx(tmp_path, monkeypatch, recorder=sh)
    with pytest.raises(LookupError, match="merge-base"):
        steps._dsaudit(ctx)
    assert not sh.has("start-docker.sh up")


# ── the projects that keep their own builds up ────────────────────────────────────

def test_without_an_app_block_the_configured_origins_are_used_unchanged(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch, app=None,
                   cfg_extra={"base-new": "http://localhost:4300",
                              "base-old": "http://localhost:4301"})
    steps._dsaudit(ctx)
    assert not sh.has("start-docker.sh")
    audit = sh.first("ds-audit.py --base-new")
    assert "--base-new http://localhost:4300 --base-old http://localhost:4301" in audit


def test_neither_an_app_block_nor_two_origins_is_a_skip_that_says_both(tmp_path, monkeypatch):
    ctx, sh = _ctx(tmp_path, monkeypatch, app=None)
    with pytest.raises(LookupError, match="app"):
        steps._dsaudit(ctx)


def test_no_screens_is_a_skip_before_anything_is_started(tmp_path, monkeypatch):
    """Booting two stacks to then discover there is nothing to shoot is four minutes
    spent on a step that was never going to run."""
    ctx, sh = _ctx(tmp_path, monkeypatch, cfg_extra={"screens": {}})
    with pytest.raises(LookupError, match="screen"):
        steps._dsaudit(ctx)
    assert not sh.has("start-docker.sh up")


# ── the prerequisite ──────────────────────────────────────────────────────────────

def test_the_prerequisite_probes_no_port_when_the_step_owns_the_instances(monkeypatch):
    """With `app` there is nothing to probe, and probing anyway would be worse than
    useless: :4300 belongs to nobody, so the step would be skipped for the absence of a
    build it was about to create. An `up` that fails is a failure of the step, reported
    with its own output — same arrangement as `video`, whose prerequisite is `None`."""
    monkeypatch.setattr(steps, "answers",
                        lambda *a, **k: pytest.fail("no port may be probed"))
    ctx = steps.Ctx("origin/main", {"steps": {"dsaudit": {"app": APP, "screens": SCREENS}}},
                    dry=False)
    assert steps._dsaudit_prereq(ctx) is True


def test_the_prerequisite_still_guards_the_projects_that_serve_their_own(monkeypatch):
    monkeypatch.setattr(steps, "answers", lambda url, *a, **k: url.endswith("4300"))
    ctx = steps.Ctx("origin/main",
                    {"steps": {"dsaudit": {"base-new": "http://localhost:4300",
                                           "base-old": "http://localhost:4301",
                                           "screens": SCREENS}}}, dry=False)
    reason = steps._dsaudit_prereq(ctx)
    assert "4301" in reason and "4300" not in reason
    # And it names the way out that did not exist before: the step can start them itself.
    assert "steps.dsaudit.app" in reason
