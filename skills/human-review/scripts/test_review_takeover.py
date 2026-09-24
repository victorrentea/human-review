#!/usr/bin/env python3
"""A takeover is not a review, and its commits are on the aftermath band, not in a note.

A takeover moves the review point forward without a new pass: a `Review-Points:` commit
whose points file carries a `## Taken over without a new pass — <date>` note. Read as a
review, it did two kinds of damage on the demo branch: its `Implements:` (the previous
takeover) was reported as the implementation commit, and the commits it took over left the
scripted aftermath band and survived only as a list an agent typed into the note — a second
list of the same kind of commits, a screen above the first, counted from another commit.

Run with:  python3 -m pytest test_review_takeover.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, HERE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


commits_mod = _load("review_commits_takeover", "review-commits.py")
points_mod = _load("review_points_takeover", "review-points.py")
build = _load("build_review_takeover", "build-review-html.py")


def _git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, msg, files):
    for rel, text in files.items():
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    base = _commit(repo, "base", {"app.py": "x = 1\n"})
    impl = _commit(repo, "feature", {"app.py": "x = 2\n"})
    review = _commit(repo, f"review\n\nReview-Points: review-points.md\nImplements: {impl}",
                     {"review-points.md": "## Fixed\n"})
    kept = _commit(repo, "hand edit, taken over later", {"app.py": "x = 3\n"})
    takeover = _commit(
        repo, f"take over\n\nReview-Points: review-points.md\nImplements: {kept}",
        {"review-points.md": "## Fixed\n\n## Taken over without a new pass — 21 Sep 2026\n\n"
                             "Victor's decision.\n"})
    fresh = _commit(repo, "hand edit after the takeover", {"app.py": "x = 4\n"})
    return repo, dict(base=base, impl=impl, review=review, kept=kept,
                      takeover=takeover, fresh=fresh)


def test_the_two_readers_of_the_note_agree_on_its_opening_words():
    assert commits_mod.NOTE_HEADINGS == points_mod.NOTE_HEADINGS


def test_a_takeover_is_not_the_review_commit_and_its_implements_is_not_the_implementation(
        tmp_path):
    repo, c = _branch(tmp_path)
    got = commits_mod.detect(repo, c["base"])
    assert got["review"] == c["review"]
    assert got["implementation"] == c["impl"]
    assert got["takeover"]["sha"] == c["takeover"]
    assert got["takeover"]["heading"] == "Taken over without a new pass — 21 Sep 2026"
    rows = {r["sha"]: r for r in got["after_detail"]}
    assert rows[c["kept"]]["taken_over"] is True
    assert rows[c["takeover"]]["takeover"] is True
    assert rows[c["fresh"]]["taken_over"] is False
    assert rows[c["fresh"]]["takeover"] is False


def test_a_real_pass_after_a_takeover_is_the_review_again(tmp_path):
    repo, c = _branch(tmp_path)
    again = _commit(repo, "second pass\n\nReview-Points: review-points.md",
                    {"review-points.md": "## Fixed\n\nall re-read\n"})
    got = commits_mod.detect(repo, c["base"])
    assert got["review"] == again
    assert got["takeover"] is None
    assert got["after_detail"] == []


# --------------------------------------------------------------------------- #
# the band
# --------------------------------------------------------------------------- #

def _row(sha, subject, taken_over=False, takeover=False, code=True):
    files = [{"path": "app.py" if code else "review-points.md", "added": 3, "deleted": 1,
              "binary": False, "generated": False}]
    return {"sha": sha * 5, "short": sha, "when": "2026-09-22T10:00:00+03:00",
            "subject": subject, "files": files, "added": 3, "deleted": 1,
            "measured": True, "generated_only": False,
            "taken_over": taken_over, "takeover": takeover}


def _band(tmp_path, rows):
    doc = {"review": "ce56d912" * 5, "review_short": "ce56d912", "head": "x",
           "takeover": {"sha": "db6b67ec" * 5, "when": "2026-09-21T07:24:00+03:00",
                        "subject": "take over",
                        "heading": "Taken over without a new pass — 21 Sep 2026"},
           "commits": rows, "totals": {}}
    (tmp_path / "aftermath.json").write_text(json.dumps(doc), encoding="utf-8")
    build.ACTIONS.clear()
    build.declare_rerun_actions(tmp_path, tmp_path, HERE)
    return build.aftermath_html(tmp_path, tmp_path)


def test_one_band_counts_what_came_after_the_takeover_and_folds_what_it_took_over(tmp_path):
    out = _band(tmp_path, [
        _row("aaaaaaaa", "hand edit, taken over later", taken_over=True),
        _row("db6b67ec", "take over", taken_over=True, takeover=True, code=False),
        _row("bbbbbbbb", "hand edit after the takeover"),
    ])
    assert "rband-alert" in out
    assert "1 commit, 4 lines changed since the review was taken over" in out
    assert "taken over at <code>db6b67ec</code> on 2026-09-21 without a new pass" in out
    assert ('1 commit after <code>ce56d912</code> taken over without a new pass on '
            '2026-09-21') in out
    assert "hand edit, taken over later" in out
    # The bookkeeping commit is the band's sentence, not one of its rows.
    assert ">take over<" not in out and " take over <" not in out


def test_with_nothing_after_the_takeover_the_band_is_amber_and_says_so(tmp_path):
    out = _band(tmp_path, [_row("aaaaaaaa", "hand edit, taken over later", taken_over=True)])
    assert "rband-warn" in out
    assert "1 commit taken over without a new pass" in out


def test_the_hand_typed_note_row_stands_down_when_the_band_carries_the_takeover(tmp_path):
    assert build.aftermath_reads_takeover(tmp_path) is False
    _band(tmp_path, [_row("aaaaaaaa", "x", taken_over=True)])
    assert build.aftermath_reads_takeover(tmp_path) is True
