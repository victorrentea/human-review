#!/usr/bin/env python3
"""What landed on the branch after the agent stopped, and how the page says it.

This is the page's one honesty gate, and the only fact on it that no diff can carry.
Every other number — the diffstat, the complexity delta, the contract diff, the film, the
costs — is measured from the change set, and a change set cannot say *when* it was
written. So a page can be entirely accurate about a branch and entirely misleading about
the review it claims to be: the findings, the declined items and the assumptions describe
the branch as the agent left it, and three commits later they describe something nobody
looked at. The reader cannot see that, because the page is the only thing in a position to
say it.

Two things are pinned here, and they are the two ways this band could be useless:

* **the split.** Red for a file a human wrote, grey when every path in the range is
  generated. On a repository whose guardrails regenerate diagrams, a spec and a `.drawio`
  on every commit, a band that does not split is red on every branch — and a red band that
  is always on is a red band nobody reads by the third one;
* **the silence.** With no review commit there is nothing to date "after" from, and the
  band must be absent rather than reassuring. "Nothing has changed since the agent
  finished" resting on not knowing when that was is the worst sentence this page could
  print.

Run with:  python3 -m pytest test_aftermath.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, HERE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


steps = _load("run_steps", "run-steps.py")
build = _load("build_review_aftermath", "build-review-html.py")


# --------------------------------------------------------------------------- #
# which paths nobody typed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pattern,path,hit", [
    # `**/` has to be optional, or the spelling every project writes misses the
    # top-level directory it is most obviously about.
    ("**/generated/**", "generated/DB.puml", True),
    ("**/generated/**", "petclinic-backend/docs/generated/DB.puml", True),
    ("**/generated/**", "src/generator/Thing.java", False),
    ("docs/generated/**", "docs/generated/c4/a.puml", True),
    ("docs/generated/**", "backend/docs/generated/c4/a.puml", False),
    # `*` stops at a slash — which is the whole reason fnmatch is not enough here: its
    # `*` would make this pattern match `docs/openapi.yaml` too.
    ("openapi.yaml", "openapi.yaml", True),
    ("openapi.yaml", "petclinic-backend/openapi.yaml", False),
    ("**/*.genseq.*", "petclinic-test/generated/add-visit.spec.ts.a.genseq.puml", True),
    ("**/*.genseq.*", "petclinic-test/src/add-visit.spec.ts", False),
    ("**/api-types.ts", "petclinic-frontend/src/app/generated/api-types.ts", True),
    ("**/*.drawio*", "petclinic-backend/docs/ConceptualModel.drawio.png", True),
])
def test_a_generated_path_is_recognised_by_its_shape(pattern, path, hit):
    assert bool(steps.glob_rx(pattern).match(path)) is hit


def test_a_project_that_says_what_it_generates_replaces_the_defaults():
    """Adding to them would mean a project could never say a path it generates is *not*
    generated, and the list a project writes is the one it has thought about."""
    assert steps.generated_globs({"generated": ["build/**"]}) == ["build/**"]
    assert steps.generated_globs({}) == list(steps.GENERATED_DEFAULT)
    assert steps.generated_globs({"generated": []}) == list(steps.GENERATED_DEFAULT)
    assert steps.generated_globs({"generated": "build/**"}) == list(steps.GENERATED_DEFAULT)


def test_the_example_config_documents_the_key():
    """The defaults live in code and the example is where a reader meets them; the two
    drifting apart is how a config file starts lying about what it does."""
    cfg = json.loads((HERE.parent / "human-review.example.json").read_text(encoding="utf-8"))
    assert cfg["generated"] == list(steps.GENERATED_DEFAULT)


# --------------------------------------------------------------------------- #
# reading `git --numstat`
# --------------------------------------------------------------------------- #

def test_numstat_counts_lines_and_classifies_each_file():
    rxs = [steps.glob_rx(g) for g in steps.GENERATED_DEFAULT]
    rows, adds, dels = steps.numstat(
        "18\t1\thuman-review.json\n"
        "4\t0\tpetclinic-backend/docs/generated/DB.puml\n", rxs)
    assert (adds, dels) == (22, 1)
    assert [r["generated"] for r in rows] == [False, True]


def test_a_binary_file_is_a_file_that_moved_and_no_lines():
    rxs = [steps.glob_rx("**/*.drawio*")]
    rows, adds, dels = steps.numstat("-\t-\tdocs/Model.drawio.png\n", rxs)
    assert (adds, dels) == (0, 0)
    assert rows[0]["binary"] is True and rows[0]["generated"] is True


def test_a_rename_is_judged_by_where_the_file_landed():
    """A diagram moved into `generated/` is generated from here on, whatever it was."""
    rxs = [steps.glob_rx("**/generated/**")]
    rows, _, _ = steps.numstat("0\t0\tdocs/{a.puml => generated/a.puml}\n", rxs)
    assert rows[0]["generated"] is True


# --------------------------------------------------------------------------- #
# the step, over a real repository
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    monkeypatch.chdir(repo)
    return repo


def _sha(repo, rev="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", rev],
                          capture_output=True, text=True).stdout.strip()


def _run(cfg=None):
    ctx = steps.Ctx("main", cfg or {}, dry=False)
    return steps.run_step("aftermath", "review", "after", None, steps._aftermath, ctx), ctx


def test_with_no_review_commit_the_step_is_skipped_rather_than_reassuring(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    (repo / ".human-review").mkdir()
    result, _ = _run()
    assert result["status"] == steps.SKIPPED
    assert "Review-Points" in result["reason"]
    assert not (repo / ".human-review" / "aftermath.json").exists()


def _with_review_commit(repo, files: dict):
    """A review commit, then one commit on top touching `files`."""
    (repo / "review-points.md").write_text("## Fixed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "review\n\nReview-Points: review-points.md")
    review = _sha(repo)
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "landed afterwards")
    (repo / ".human-review").mkdir(exist_ok=True)
    (repo / ".human-review" / "review-commits.json").write_text(json.dumps({
        "review": review,
        "after": [_sha(repo)],
        "after_detail": [{"sha": _sha(repo), "when": "2026-09-17T21:48:02+03:00",
                          "subject": "landed afterwards"}],
    }), encoding="utf-8")
    return review


def test_a_hand_edit_after_the_review_is_measured_and_named(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _with_review_commit(repo, {"human-review.json": "{}\n"})
    result, ctx = _run()
    assert result["status"] == steps.RAN
    doc = json.loads((repo / ".human-review" / "aftermath.json").read_text())
    assert doc["totals"]["commits"] == 1
    assert doc["totals"]["code"]["files"] == 1
    assert doc["totals"]["generated"]["files"] == 0
    assert doc["commits"][0]["generated_only"] is False
    assert any("red band" in n for n in ctx.notes)


def test_a_regenerated_diagram_after_the_review_is_counted_and_not_alarmed_about(
        tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _with_review_commit(repo, {"docs/generated/DB.puml": "@startuml\n@enduml\n"})
    result, ctx = _run()
    assert result["status"] == steps.RAN
    doc = json.loads((repo / ".human-review" / "aftermath.json").read_text())
    assert doc["totals"]["code"]["files"] == 0
    assert doc["totals"]["generated"]["files"] == 1
    assert doc["commits"][0]["generated_only"] is True
    assert any("grey" in n for n in ctx.notes)


# --------------------------------------------------------------------------- #
# the band
# --------------------------------------------------------------------------- #

def _doc(code_files=1, gen_files=0, commits=1, **extra):
    files = ([{"path": "human-review.json", "added": 18, "deleted": 1,
               "binary": False, "generated": False}] * code_files
             + [{"path": "docs/generated/DB.puml", "added": 4, "deleted": 0,
                 "binary": False, "generated": True}] * gen_files)
    return {
        "review": "ce56d912" * 5, "review_short": "ce56d912", "head": "753f724c" * 5,
        "generated_globs": [], "clean": not commits,
        "commits": [{"sha": "753f724c" * 5, "short": "753f724c",
                     "when": "2026-09-17T21:48:02+03:00",
                     "subject": "landed afterwards", "files": files,
                     "added": 18 * code_files + 4 * gen_files,
                     "deleted": code_files, "measured": True,
                     "generated_only": not code_files}] * commits,
        "totals": {"commits": commits, "files": len(files),
                   "added": 0, "deleted": 0,
                   "code": {"files": code_files, "added": 18 * code_files,
                            "deleted": code_files},
                   "generated": {"files": gen_files, "added": 4 * gen_files,
                                 "deleted": 0}},
        **extra}


def _band(tmp_path, doc):
    if doc is not None:
        (tmp_path / "aftermath.json").write_text(json.dumps(doc), encoding="utf-8")
    build.ACTIONS.clear()
    return build.aftermath_html(tmp_path, tmp_path)


def test_a_hand_edit_makes_the_band_red_and_says_how_much(tmp_path):
    out = _band(tmp_path, _doc(code_files=1))
    assert "rband-alert" in out
    assert 'role="alert"' in out
    assert "1 commit, 19 lines changed since the agent finished" in out
    assert "753f724c" in out and "landed afterwards" in out


def test_only_generated_files_make_the_band_grey(tmp_path):
    out = _band(tmp_path, _doc(code_files=0, gen_files=1))
    assert "rband-warn" in out and "rband-alert" not in out
    assert "every file in them is generated" in out
    # The grey is explained where it is read, so nobody "fixes" it in the prose.
    assert "which is why this band is grey" in out


def test_a_branch_the_agent_left_alone_gets_no_band(tmp_path):
    assert _band(tmp_path, _doc(commits=0)) == ""


def test_no_measurement_is_not_a_reassuring_band(tmp_path):
    """The step says why on its own row of the status table. A band invented here would be
    the page asserting the one thing it does not know."""
    assert _band(tmp_path, None) == ""


def test_every_commit_offers_a_revert_that_only_stages_it(tmp_path):
    """`git revert --no-commit` is the only form of this safe to put behind a button: the
    click produces a diff to look at, not a commit made on the reader's behalf."""
    out = _band(tmp_path, _doc(code_files=1))
    entry = build.ACTIONS["aftermath-revert:753f724c"]
    assert entry["command"].endswith("git revert --no-commit " + "753f724c" * 5)
    assert entry["command"].startswith("cd ")
    assert entry["reload"] is False, "the band reports commits; a revert does not move HEAD"
    assert "revert it" in out
    assert "nothing is committed" in out


def test_a_merge_commit_with_no_numstat_is_not_read_as_harmless(tmp_path):
    doc = _doc(code_files=0, gen_files=0)
    doc["commits"][0]["files"] = []
    doc["commits"][0]["measured"] = False
    doc["commits"][0]["generated_only"] = False
    out = _band(tmp_path, doc)
    assert "no file list" in out
