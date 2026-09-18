#!/usr/bin/env python3
"""A step whose inputs have not moved is not re-run, and one whose inputs moved is.

`run-steps.py` used to re-derive every producer's answer on every refresh, which cost about
forty-five seconds to arrive at bytes already on disk — and cost a further forty-five in the
*build*, because stamping the ledger moved `.steps.json` and the cost report is cached on it.
The fix is a fingerprint per step. What has to be pinned is not the speed but the two ways a
fingerprint can be wrong, and they are not symmetrical:

  * a **false miss** is slow. Annoying, self-correcting, and the tests for it are here only
    so a refactor cannot quietly turn the cache off and leave everything passing;
  * a **false hit** is a page asserting evidence that no longer matches the repository, with
    nothing on it admitting so. Every test below that changes something and demands a re-run
    is guarding that direction.

Run with:  python3 -m pytest test_step_cache.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("run_steps", HERE / "run-steps.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


# --------------------------------------------------------------------------- a repository

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repository with a branch off main, because every fingerprint here is a
    question asked of git and a fake would only pin the fake."""
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    _git_init = subprocess.run(["git", "init", "-q", "-b", "main", str(r)],
                               capture_output=True, text=True)
    assert _git_init.returncode == 0, _git_init.stderr
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "src" / "A.java").write_text("class A {}\n", encoding="utf-8")
    (r / "d.puml").write_text("@startuml\nA -> B\n@enduml\n", encoding="utf-8")
    (r / "openapi.yaml").write_text("openapi: 3.0.0\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "feature")
    (r / "src" / "B.java").write_text("class B {}\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "work")
    (r / ".human-review" / "assets").mkdir(parents=True)
    # The base every producer diffs against. Written as config rather than passed on each
    # command line because that is how a real project says it, and `run-steps.py` defaults
    # to `origin/main`, which a repository with no remote does not have.
    (r / "human-review.json").write_text(json.dumps({"base": "main"}), encoding="utf-8")
    monkeypatch.chdir(r)
    return r


def _ctx(repo: Path, cfg: dict | None = None) -> "rs.Ctx":
    return rs.Ctx("main", cfg or {}, False)


def test_the_review_directory_is_not_an_input(repo):
    """Its own outputs must not be a reason to re-run. A project that does not gitignore
    `.human-review/` would otherwise see every whole-repo step miss for ever, because this
    program rewrites that directory on every run."""
    before = _key(repo, "tests")
    (repo / ".human-review" / "assets" / "anything.json").write_text("{}", encoding="utf-8")
    assert _key(repo, "tests") == before


def _key(repo: Path, step: str, cfg: dict | None = None) -> str | None:
    ctx = _ctx(repo, cfg)
    head = rs._git_out(["rev-parse", "HEAD"]).strip()
    mb = rs._git_out(["merge-base", "main", "HEAD"]).strip()
    return rs._step_key(step, ctx, mb, head, rs.dirty_paths())


# --------------------------------------------------------------------------- the key moves

def test_an_untouched_repository_fingerprints_the_same_twice(repo):
    """The whole cache rests on this: asked twice with nothing between, the answer is equal.

    It is worth its own test because almost everything that could go wrong here — a set
    iterated in hash order, a dict of globs, an mtime folded in where a hash belonged —
    shows up first as a fingerprint that is merely *unstable*, and an unstable fingerprint
    is a cache that silently never hits."""
    for step in ("complexity", "diagrams", "tests", "api"):
        assert _key(repo, step) == _key(repo, step), f"{step} fingerprints unstably"


def test_editing_a_declared_input_moves_that_steps_key(repo):
    before = _key(repo, "complexity")
    (repo / "src" / "A.java").write_text("class A { int x; }\n", encoding="utf-8")
    assert _key(repo, "complexity") != before


def test_editing_an_input_leaves_an_unrelated_step_alone(repo):
    """The point of declaring inputs per step rather than hashing the whole tree once.

    A Java edit must re-run `complexity` and must NOT re-run `diagrams` — otherwise the
    cache degenerates into "everything or nothing", which is what it replaced."""
    before = _key(repo, "diagrams")
    (repo / "src" / "A.java").write_text("class A { int y; }\n", encoding="utf-8")
    assert _key(repo, "diagrams") == before


def test_an_untracked_file_counts_as_a_change(repo):
    """`c2` reads `*.genseq.puml`, which the `sequence` step writes minutes earlier and does
    not commit. A fingerprint taken from the index alone would call the repository unchanged
    while the only file the step exists to read had just appeared."""
    before = _key(repo, "c2")
    (repo / "new.genseq.puml").write_text("@startuml\n@enduml\n", encoding="utf-8")
    assert _key(repo, "c2") != before


def test_a_new_commit_moves_every_key(repo):
    """Every producer diffs against the merge-base, so HEAD is an input to all of them even
    when the files they name are untouched."""
    before = {s: _key(repo, s) for s in rs.STEP_INPUTS}
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "more")
    after = {s: _key(repo, s) for s in rs.STEP_INPUTS}
    assert all(after[s] != before[s] for s in before), "a step ignored a new commit"


def test_editing_the_producer_moves_its_own_key_only(repo, monkeypatch, tmp_path):
    """A change to `endpoint-complexity.py` has to re-run `complexity`.

    The tools live in the skill, not in the repository under review, so nothing git says
    about the branch can notice them — which is exactly how an improvement to a producer
    used to leave every cached page showing the old producer's output."""
    fake = tmp_path / "tools"
    fake.mkdir()
    for rel in ("endpoint-complexity.py", "endpoint-complexity-delta.py",
                "puml-diff.sh", "drawio-diff.py"):
        (fake / rel).write_text("# v1\n", encoding="utf-8")
    (fake / "..").resolve()
    monkeypatch.setattr(rs, "HERE", fake)
    before_c = _key(repo, "complexity")
    before_d = _key(repo, "diagrams")
    (fake / "endpoint-complexity.py").write_text("# v2\n", encoding="utf-8")
    assert _key(repo, "complexity") != before_c
    assert _key(repo, "diagrams") == before_d


def test_changing_the_step_config_moves_its_key(repo):
    """`human-review.json` is an input like any other: the same sources with a different
    `drawio.diagram` are a different drawing."""
    a = {"steps": {"diagrams": {"drawio": {"diagram": "one.png"}}}}
    b = {"steps": {"diagrams": {"drawio": {"diagram": "two.png"}}}}
    assert _key(repo, "diagrams", a) != _key(repo, "diagrams", b)


def test_a_step_with_no_declared_inputs_is_never_cacheable(repo):
    """None, not a hash. A step nobody has described must run every time — the alternative
    is a page built from a stale artifact that nothing on it admits to."""
    assert "sequence" not in rs.STEP_INPUTS
    assert _key(repo, "sequence") is None
    assert _key(repo, "video") is None


# ------------------------------------------------------------------- the skip itself

def _run(name="tests", key="k", cached=None, ran=None, tabs="requirements"):
    """`run_step` with a runner that records whether it was called."""
    calls = []

    def fn(ctx):
        calls.append(1)
        if ran:
            ran()

    ctx = rs.Ctx("main", {}, False, no_ledger=True)
    result = rs.run_step(name, tabs, "a label", None, fn, ctx, key, cached)
    return result, calls


def test_a_matching_key_skips_the_runner(repo):
    (repo / ".human-review" / "assets" / "test-changes.json").write_text("{}", encoding="utf-8")
    result, calls = _run(key="k", cached={"key": "k", "seconds": 9.0})
    assert calls == [], "the step ran although its inputs were unchanged"
    assert result["cached"] is True
    assert result["saved"] == 9.0


def test_a_skipped_step_still_reports_ran(repo):
    """Not a fourth status, on purpose. `ran` is the truthful answer to the only question
    anything downstream asks of this table — is this tab's evidence on disk and current —
    and a new status would have every reader treat a fresh artifact as a missing one."""
    (repo / ".human-review" / "assets" / "test-changes.json").write_text("{}", encoding="utf-8")
    result, _ = _run(key="k", cached={"key": "k", "seconds": 1.0})
    assert result["status"] == rs.RAN


def test_a_different_key_runs_the_step(repo):
    (repo / ".human-review" / "assets" / "test-changes.json").write_text("{}", encoding="utf-8")
    _result, calls = _run(key="new", cached={"key": "old", "seconds": 9.0})
    assert calls == [1]


def test_no_cache_entry_runs_the_step(repo):
    (repo / ".human-review" / "assets" / "test-changes.json").write_text("{}", encoding="utf-8")
    _result, calls = _run(key="k", cached=None)
    assert calls == [1]


def test_an_uncacheable_step_runs_even_with_an_entry(repo):
    """`key` is None for a step that declares no inputs, and None must never match."""
    (repo / ".human-review" / "assets" / "test-changes.json").write_text("{}", encoding="utf-8")
    _result, calls = _run(key=None, cached={"key": None, "seconds": 9.0})
    assert calls == [1]


def test_a_wiped_output_runs_the_step_although_the_key_matches(repo):
    """The one that makes a fresh review safe. Step 0 empties `.human-review/assets/`; a
    cache that only knew about inputs would then skip every step and leave the page naming
    evidence that had just been deleted."""
    out = repo / ".human-review" / "assets" / "test-changes.json"
    out.write_text("{}", encoding="utf-8")
    out.unlink()
    _result, calls = _run(key="k", cached={"key": "k", "seconds": 9.0})
    assert calls == [1]


def test_an_emptied_output_directory_counts_as_wiped(repo):
    (repo / ".human-review" / "assets" / "diagrams").mkdir(parents=True)
    result, calls = _run(name="diagrams", tabs="data,packages",
                         key="k", cached={"key": "k", "seconds": 9.0})
    assert calls == [1], "an empty assets/diagrams/ was treated as a rendered one"


# --------------------------------------------------------------------------- the store

def test_the_cache_round_trips(repo):
    rs.save_step_cache({"tests": {"key": "abc", "seconds": 1.5, "at": "now"}})
    assert rs.load_step_cache()["tests"]["key"] == "abc"


def test_a_cache_from_another_version_is_ignored(repo):
    rs.STEP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    rs.STEP_CACHE.write_text(json.dumps(
        {"version": rs.CACHE_VERSION - 1, "steps": {"tests": {"key": "abc"}}}),
        encoding="utf-8")
    assert rs.load_step_cache() == {}, "a key computed by an older, differently-meaning hash was trusted"


def test_an_unreadable_cache_is_no_cache(repo):
    rs.STEP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    rs.STEP_CACHE.write_text("{ not json", encoding="utf-8")
    assert rs.load_step_cache() == {}


# --------------------------------------------------------------------- end to end

def _main(repo: Path, *argv: str) -> list[dict]:
    """`run-steps.py --json` over a repository, as a subprocess, returning the status rows.

    A subprocess and not a call, because what is being pinned includes the parts that only
    exist in `main` — the key computed after the step, the store, `--force`."""
    proc = subprocess.run(
        ["python3", str(HERE / "run-steps.py"), "--json", "--no-ledger", *argv],
        cwd=str(repo), capture_output=True, text=True)
    out = proc.stdout
    return json.loads(out[out.index("["):])


@pytest.mark.parametrize("step", ["tests", "owners"])
def test_the_second_run_skips_and_the_third_still_does(repo, step):
    first = _main(repo, "--only", step)
    assert first[0]["status"] == rs.RAN and not first[0].get("cached")
    second = _main(repo, "--only", step)
    assert second[0].get("cached") is True, f"{step} re-ran although nothing moved"
    third = _main(repo, "--only", step)
    assert third[0].get("cached") is True, f"{step} cached once and then stopped"


def test_a_changed_input_re_runs_only_the_steps_that_read_it(repo):
    _main(repo, "--only", "tests,complexity,diagrams")
    (repo / "src" / "A.java").write_text("class A { int z; }\n", encoding="utf-8")
    rows = {r["step"]: r for r in _main(repo, "--only", "tests,complexity,diagrams")}
    assert not rows["complexity"].get("cached"), "a Java edit did not re-run complexity"
    assert not rows["tests"].get("cached"), "a Java edit did not re-run the test manifest"
    assert rows["diagrams"].get("cached") is True, "a Java edit needlessly redrew the diagrams"


def test_force_re_runs_everything_and_still_records_a_usable_key(repo):
    """`--force` suppresses the lookup and nothing else.

    The second half is the one that bit: a forced run that stored keys taken against an
    empty git context left every later refresh a guaranteed miss, which is how a cache
    turns into a permanent tax."""
    _main(repo, "--only", "tests")
    forced = _main(repo, "--only", "tests", "--force")
    assert not forced[0].get("cached"), "--force honoured the cache"
    after = _main(repo, "--only", "tests")
    assert after[0].get("cached") is True, "--force poisoned the cache it wrote"


def test_a_failing_step_records_no_key(repo, monkeypatch):
    """A failure must not leave last run's key behind it, or the next refresh reports as
    current an artifact this run had just proved it could not produce."""
    rs.save_step_cache({"tests": {"key": "stale", "seconds": 1.0}})
    # `test-changes.py` cannot run against a repo with no such script reachable... it can,
    # so break the input instead: an unreadable base makes the producer exit non-zero.
    rows = _main(repo, "--only", "tests", "--base", "no-such-ref")
    if rows[0]["status"] == rs.FAILED:
        assert "tests" not in rs.load_step_cache()


def test_the_ledger_is_untouched_when_a_refresh_skips(repo):
    """The whole reason the skip is worth more than the seconds it saves.

    `.steps.json` is an input to the build's own cost cache, so a refresh that stamps it
    costs the *build* forty-five seconds re-reading a conversation that has not gained a
    turn. A cached step stamps nothing, so the file does not move."""
    ledger = repo / ".human-review" / ".steps.json"
    _main(repo, "--only", "tests")
    ledger.write_text("[]", encoding="utf-8")
    before = ledger.read_bytes()
    rows = _main(repo, "--only", "tests")
    assert rows[0].get("cached") is True
    assert ledger.read_bytes() == before, "a skipped step still moved the ledger"
