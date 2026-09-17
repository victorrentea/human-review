"""The line between the half a program can redo and the half only a model writes.

`refresh-report.py` is the program half, as one command. What these pin is the boundary
itself: that a refresh never writes the model's artifacts, never buys a model call, refuses
rather than builds a page whose judgement is missing, and that "refresh the page" does not
silently mean "drive a browser and run the test suite again".
"""
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


refresh = _load("refresh-report")
build = _load("build-review-html")


def _review(tmp_path: Path, *, complete=True) -> Path:
    d = tmp_path / ".human-review"
    (d / "assets").mkdir(parents=True)
    (d / "test-index").mkdir()
    if complete:
        (d / "content.json").write_text("{}", encoding="utf-8")
        (d / "assets" / "requirements-map.html").write_text("<div/>", encoding="utf-8")
        (d / "test-index" / "rest.json").write_text("[]", encoding="utf-8")
    return d


# ── what the program refuses to do ────────────────────────────────────────────────

def test_a_missing_judgement_stops_the_refresh_and_is_named(tmp_path):
    """A page rebuilt without the findings, the matrix or the catalogue is not a smaller
    page — it is the same page with its argument deleted. And the reason it is missing is
    never "a step did not run", so the build's own wording would send the reader after the
    wrong half: this one names the model's part and stops."""
    d = _review(tmp_path, complete=False)
    assert [rel for rel, _ in refresh.missing_model_work(d)] == [
        "content.json", "assets/requirements-map.html", "test-index"]
    assert refresh.missing_model_work(_review(tmp_path / "ok")) == []


def test_an_emptied_catalogue_counts_as_missing(tmp_path):
    """`test-index/` left behind empty by a wipe is the same absence as no directory at
    all — nobody has written the catalogue — and the refusal has to read the same."""
    d = _review(tmp_path)
    for f in (d / "test-index").iterdir():
        f.unlink()
    assert [rel for rel, _ in refresh.missing_model_work(d)] == ["test-index"]


def test_the_refusal_exits_three_and_says_which_half_writes_them(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _review(tmp_path, complete=False)
    assert refresh.main(["--dry-run"]) == 3
    err = capsys.readouterr().err
    assert "only a model writes" in err and "content.json" in err
    assert "/human-review" in err, "the way to get them back is named"


def test_no_report_at_all_is_a_different_answer(tmp_path, capsys, monkeypatch):
    """Nothing to refresh is not a broken report: it is a review that never ran."""
    monkeypatch.chdir(tmp_path)
    assert refresh.main(["--dry-run"]) == 2
    assert "there is no report to refresh" in capsys.readouterr().err


# ── what "refresh" means by default ───────────────────────────────────────────────

def test_the_default_refresh_reruns_no_producer(tmp_path):
    """Most refreshes are a change to the *page*, not to the evidence. Re-deriving a
    Code City shot nobody touched is a minute of waiting for an identical byte."""
    d = _review(tmp_path)
    cmds = refresh.plan(d, "none", None, True, False, None)
    assert not any("run-steps.py" in c[1] for c in cmds)
    assert [Path(c[1]).name for c in cmds] == ["build-review-html.py", "serve-review.py"]


def test_cheap_leaves_out_the_producers_that_drive_a_browser_or_a_suite(tmp_path):
    """`--steps cheap` is "re-derive what a change to the branch could have changed",
    and the four that need a served app, a Chrome or a tracing backend are not that."""
    assert refresh.steps_argv("cheap") == ["--skip", "sequence,video,city,dsaudit"]
    assert refresh.steps_argv("all") == []
    assert refresh.steps_argv("none") is None and refresh.steps_argv("") is None
    assert refresh.steps_argv("diagrams,tests") == ["--only", "diagrams,tests"]
    # Every name in the heavy list is a step that exists; a typo here would silently
    # stop skipping something.
    steps = _load("run-steps")
    assert set(refresh.HEAVY_STEPS) <= {s[0] for s in steps.STEPS}


def test_the_producers_run_before_the_build_and_the_build_before_the_server(tmp_path):
    d = _review(tmp_path)
    cmds = refresh.plan(d, "cheap", "origin/main", True, False, None)
    assert [Path(c[1]).name for c in cmds] == [
        "run-steps.py", "build-review-html.py", "serve-review.py"]
    assert cmds[0][-2:] == ["--base", "origin/main"]


def test_the_page_is_never_served_when_the_caller_asked_only_for_the_file(tmp_path):
    d = _review(tmp_path)
    cmds = refresh.plan(d, "none", None, False, False, None)
    assert not any("serve-review.py" in c[1] for c in cmds)


# ── the model call the build itself makes ─────────────────────────────────────────

def test_a_refresh_builds_with_no_model_unless_asked(tmp_path):
    """The Logging tab's privacy verdicts are the one model call inside a *build*. A
    refresh reuses what an earlier run paid for and buys nothing new."""
    d = _review(tmp_path)
    build_cmd = [c for c in refresh.plan(d, "none", None, True, False, None)
                 if "build-review-html.py" in c[1]][0]
    assert "--no-model" in build_cmd
    allowed = [c for c in refresh.plan(d, "none", None, True, True, None)
               if "build-review-html.py" in c[1]][0]
    assert "--no-model" not in allowed


def test_offline_verdicts_come_out_of_the_cache_or_say_they_were_never_asked(tmp_path, monkeypatch):
    """Not evaluated is a state the page already has words for, and it is the honest one:
    a build that cannot ask is not a model that failed to answer."""
    (tmp_path / "A.java").write_text("class A { }\n", encoding="utf-8")
    h = {"text": 'log.warn("hi {}", id)', "file": "A.java", "line": 3, "args": [],
         "abs_file": str(tmp_path / "A.java"), "raw_line": 'log.warn("hi {}", id);'}
    monkeypatch.setattr(build, "OFFLINE", True)
    monkeypatch.setattr(build, "_call_privacy_model",
                        lambda p: pytest.fail("a --no-model build must not call out"))
    got = build.privacy_verdict(h, tmp_path, {})
    assert got["verdict"] == "error" and "--no-model" in got["note"]
    assert got["cost_usd"] == 0.0

    # …and a statement an earlier run already paid for still renders its real verdict.
    monkeypatch.setattr(build, "OFFLINE", False)
    build.privacy_verdict(h, tmp_path, cache := {},
                          call=lambda p: {"verdict": "safe", "values": [], "cost_usd": 1.0})
    monkeypatch.setattr(build, "OFFLINE", True)
    again = build.privacy_verdict(h, tmp_path, cache)
    assert again["verdict"] == "safe" and again["cached"] is True


def test_the_flag_is_on_the_build_and_reaches_the_module(tmp_path):
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert '"--no-model"' in src and "OFFLINE = args.no_model" in src


# ── the cost the page reports ─────────────────────────────────────────────────────

def test_a_refresh_keeps_the_session_that_did_the_work(tmp_path):
    """A build run in a later session cannot recompute what the first one spent, and a
    page that drops the number silently is a page claiming the review was free."""
    d = _review(tmp_path)
    (d / ".session").write_text("abc-123\n", encoding="utf-8")
    assert refresh.session_id(d) == "abc-123"
    assert refresh.session_id(tmp_path / "nowhere") is None


def test_the_skill_tells_the_reader_to_run_the_program_not_the_commands():
    """The whole point is that nobody retypes the three commands — including the model
    reading this skill, which is how the two halves got run together in the first place."""
    skill = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "refresh-report.py" in skill
    assert skill.count("refresh-report.py") >= 2, "Step 5 and the iteration path"
