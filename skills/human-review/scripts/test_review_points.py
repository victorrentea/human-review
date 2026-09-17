#!/usr/bin/env python3
"""What `review-points.md` promises: three piles, strictly read, loudly refused.

The file is written by the same model whose work it describes, and it is the only input to
the Review tab nobody else can check — so every case below is about a way the record could
quietly become *less* than it claims: a section the parser skipped, a ref it dropped, an
item with nothing behind it, a file that is not there at all. Each of those has to arrive
at the caller as its own answer, because the page says something different about each.

Run with:  python3 -m pytest test_review_points.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("review_points", HERE / "review-points.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


FULL = """---
ticket: victorrentea/petclinic#37
base: 2a45c210
implementation: 7f3c1a9
reviewers: /code-review high
session: 16a1e790-2c96-4f1b-8a4f-2ddcf2d10a8e
---

## Fixed

### The seed hard-coded the number of vets
- file: db/seed/R__seed.sql:143
- source: /code-review agent 2 (shallow bug scan)
- fixed-in: HEAD
Both bounds now come from the vets table, so adding a seventh vet
cannot leave it unassigned.

## Ignored

### Collapse the two divergent booking implementations
- file: src/main/java/VisitRestController.java:66
- source: /code-review agent 1
- severity: medium
- why: out of scope for #37 and an API break.

## Assumptions

### @Transactional went on the public endpoints
- file: src/main/java/VisitRestController.java:62-68
- alternative: annotate bookVisit as asked — a silent no-op
- why: Spring AOP ignores self-invoked private methods.
"""


def _write(tmp_path: Path, text: str, name: str = "review-points.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _doc(tmp_path: Path, text: str) -> dict:
    return rp.document(_write(tmp_path, text), "review-points.md")


# --------------------------------------------------------------------------- #
# the three piles
# --------------------------------------------------------------------------- #

def test_the_three_sections_land_in_the_three_arrays_the_build_already_reads(tmp_path):
    doc = _doc(tmp_path, FULL)
    assert len(doc["autofixes"]) == 1, "Fixed is what the page calls an autofix"
    assert len(doc["findings"]) == 1, "Ignored is what the page calls a finding"
    assert len(doc["assumptions"]) == 1
    assert doc["mode"] == "points" and doc["source"] == "review-points.md"


def test_the_frontmatter_is_read_without_a_yaml_parser(tmp_path):
    doc = _doc(tmp_path, FULL)
    assert doc["meta"]["ticket"] == "victorrentea/petclinic#37"
    assert doc["meta"]["implementation"] == "7f3c1a9"
    assert doc["meta"]["session"].startswith("16a1e790")


@pytest.mark.parametrize("heading,pile", [
    ("Fixed", "autofixes"), ("Repaired", "autofixes"), ("Applied", "autofixes"),
    ("Ignored", "findings"), ("Rejected", "findings"), ("Declined", "findings"),
    ("Not fixed", "findings"),
    ("Assumptions", "assumptions"), ("Assumed", "assumptions"),
])
def test_every_alias_reaches_its_pile_whatever_its_case(tmp_path, heading, pile):
    doc = _doc(tmp_path, f"## {heading.upper()}\n\n### t\n- file: a.py:1\n")
    assert len(doc[pile]) == 1


def test_an_unknown_section_is_refused_rather_than_skipped(tmp_path):
    """A pile the parser dropped reads on the page exactly like a pile nobody wrote."""
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Findings\n\n### t\n- file: a.py:1\n")
    assert "unknown section" in str(bad.value) and "Findings" in str(bad.value)


def test_a_repeated_pile_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Fixed\n### a\n- file: a.py:1\n## Applied\n### b\n- file: b.py:1\n")
    assert "repeats the Fixed pile" in str(bad.value)


def test_prose_with_no_sections_at_all_is_not_this_format(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "I fixed the seed and left the duplication alone.\n")
    assert "prose, not review-points.md" in str(bad.value)


# --------------------------------------------------------------------------- #
# fields → the item shape build-review-html.py reads
# --------------------------------------------------------------------------- #

def test_a_ref_with_lines_earns_a_snippet_card_and_a_bare_one_does_not(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:12-30\n- file: b.py\n")
    item = doc["findings"][0]
    assert item["refs"] == ["a.py:12-30", "b.py"]
    assert item["snippets"] == [{"ref": "a.py:12-30"}]


def test_a_snippet_can_be_captioned(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:12-30 | the round-robin\n")
    assert doc["findings"][0]["snippets"] == [{"ref": "a.py:12-30",
                                               "caption": "the round-robin"}]


def test_captioning_a_whole_file_is_refused_because_there_is_no_card_to_caption(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Ignored\n### t\n- file: a.py | why\n")
    assert "names no lines" in str(bad.value)


def test_fixed_in_becomes_a_diff_based_at_the_implementation_commit(tmp_path):
    doc = _doc(tmp_path, FULL)
    assert doc["autofixes"][0]["diffs"] == [{"path": "db/seed/R__seed.sql",
                                             "base": "7f3c1a9"}]
    assert doc["fixed_in"] == "HEAD"


def test_a_pinned_fixed_in_pins_the_head_side_too(tmp_path):
    """`HEAD` leaves the head side as the working tree, which is what keeps the editor
    link; a named rev is a comparison the reader is being pointed at, so it is pinned."""
    doc = _doc(tmp_path, "---\nimplementation: aaa111\n---\n## Fixed\n### t\n"
                         "- file: a.py:1\n- fixed-in: bbb222\n")
    assert doc["autofixes"][0]["diffs"] == [{"path": "a.py", "base": "aaa111",
                                             "head": "bbb222"}]


def test_fixed_in_with_no_file_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Fixed\n### t\n- fixed-in: HEAD\n")
    assert "nothing to diff" in str(bad.value)


def test_two_different_fixed_in_revs_leave_the_top_level_key_silent(tmp_path):
    doc = _doc(tmp_path, "## Fixed\n### a\n- file: a.py:1\n- fixed-in: aaa\n"
                         "### b\n- file: b.py:1\n- fixed-in: bbb\n")
    assert doc["fixed_in"] is None, "two answers is not something this key can say"


def test_a_declined_finding_with_no_severity_is_context_not_a_rank(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n")
    assert doc["findings"][0]["severity"] == "info"


def test_an_assumption_is_stamped_as_one_and_may_not_carry_a_severity(tmp_path):
    doc = _doc(tmp_path, "## Assumptions\n### t\n- file: a.py:1\n")
    assert doc["assumptions"][0]["source"] == "assumption"
    assert "severity" not in doc["assumptions"][0]
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Assumptions\n### t\n- file: a.py:1\n- severity: high\n")
    assert "not a defect" in str(bad.value)


def test_an_invented_severity_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n- severity: critical\n")
    assert "severity 'critical'" in str(bad.value)


def test_a_misspelled_field_is_refused_not_dropped(tmp_path):
    """`- fille:` typed once drops a ref, and a dropped ref gets the whole item deleted by
    the anchoring rule — three steps from the typo that caused it."""
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Ignored\n### t\n- fille: a.py:1\n")
    assert "unknown field `fille:`" in str(bad.value)


def test_a_field_after_the_prose_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\nSome prose.\n- why: late\n")
    assert "comes after the prose" in str(bad.value)


def test_an_ordinary_markdown_bullet_in_the_body_is_not_mistaken_for_a_field(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\nBecause:\n"
                         "- the endpoint is public\n- the caller retries\n")
    body = doc["findings"][0]["body"]
    assert "the endpoint is public" in body and "the caller retries" in body


def test_the_body_is_escaped_but_keeps_code_spans_and_inline_tokens(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n"
                         "`bookVisit` is <private>.\n\n{{snippet:a.py:1-2|here}}\n")
    body = doc["findings"][0]["body"]
    assert "<code>bookVisit</code>" in body
    assert "&lt;private&gt;" in body, "a stray < in prose must not reach the page as markup"
    assert "{{snippet:a.py:1-2|here}}" in body, "the page's own tokens still expand"
    assert "<br><br>" in body, "a paragraph break is not a wall of text"


def test_a_fenced_code_block_in_the_body_survives_verbatim(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n```\n- file: not-a-field\n```\n")
    assert "not-a-field" in doc["findings"][0]["body"]
    assert doc["findings"][0]["refs"] == ["a.py:1"]


def test_the_item_keys_are_the_ones_the_renderer_reads(tmp_path):
    """The contract with `build-review-html.py`: it reads these keys off the item and
    nothing normalises them in between."""
    doc = _doc(tmp_path, FULL)
    assert set(doc["autofixes"][0]) <= {"title", "body", "why", "source", "severity",
                                        "refs", "snippets", "diffs", "alternative"}
    assert set(doc["assumptions"][0]) <= {"title", "body", "why", "source", "refs",
                                          "snippets", "alternative", "diffs"}
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    for key in ("refs", "snippets", "diffs", "severity", "alternative", "why"):
        assert f'"{key}"' in src or f"'{key}'" in src


# --------------------------------------------------------------------------- #
# the anchoring rule
# --------------------------------------------------------------------------- #

def test_an_unanchored_item_is_dropped_and_named(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### I was careful about the tenant check\n"
                         "- why: it felt right.\n### Anchored\n- file: a.py:1\n")
    assert len(doc["findings"]) == 1 and doc["dropped"] == 1
    assert "I was careful about the tenant check" in doc["warnings"][0]
    assert "Ignored" in doc["warnings"][0]


def test_an_item_anchored_only_by_a_diff_is_kept(tmp_path):
    doc = _doc(tmp_path, "---\nimplementation: aaa\n---\n## Fixed\n### t\n"
                         "- file: a.py\n- fixed-in: HEAD\n")
    assert doc["dropped"] == 0 and doc["autofixes"][0]["diffs"]


# --------------------------------------------------------------------------- #
# the CLI — one exit code per thing that can be wrong
# --------------------------------------------------------------------------- #

def test_a_missing_file_exits_3_and_says_nobody_recorded_anything(tmp_path, capsys):
    assert rp.main(["--root", str(tmp_path)]) == 3
    err = capsys.readouterr().err
    assert "no review-points.md" in err and "records what" in err


def test_an_unparseable_file_exits_4_and_never_a_silent_zero(tmp_path, capsys):
    _write(tmp_path, "## Findings\n### t\n- file: a.py:1\n")
    assert rp.main(["--root", str(tmp_path)]) == 4
    assert "cannot be read" in capsys.readouterr().err
    assert not (tmp_path / ".human-review" / "review-points.json").exists(), \
        "a refused file must not leave a half-written one behind"


def test_a_file_whose_every_item_is_unanchored_exits_5(tmp_path, capsys):
    """Distinct from exit 3: 'nobody wrote one' and 'somebody wrote one with nothing
    checkable in it' are different failures and the page says different things."""
    _write(tmp_path, "## Ignored\n### t\n- why: it felt right.\n")
    assert rp.main(["--root", str(tmp_path)]) == 5
    assert "says nothing checkable" in capsys.readouterr().err


def test_a_good_file_writes_the_json_the_build_reads(tmp_path, capsys):
    _write(tmp_path, FULL)
    assert rp.main(["--root", str(tmp_path)]) == 0
    out = tmp_path / ".human-review" / "review-points.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["mode"] == "points"
    assert [len(doc[k]) for k in ("findings", "autofixes", "assumptions")] == [1, 1, 1]
    assert str(out) in capsys.readouterr().out


def test_check_validates_and_writes_nothing(tmp_path, capsys):
    _write(tmp_path, FULL)
    assert rp.main(["--root", str(tmp_path), "--check"]) == 0
    assert not (tmp_path / ".human-review").exists()
    out = capsys.readouterr().out
    assert "1 fixed, 1 ignored, 1 assumptions" in out
    assert "The seed hard-coded the number of vets" in out
    assert "would write" in out


def test_out_and_file_are_overridable(tmp_path):
    (tmp_path / "docs").mkdir()
    _write(tmp_path, FULL, "docs/points.md")
    out = tmp_path / "elsewhere" / "rp.json"
    assert rp.main(["--root", str(tmp_path), "--file", "docs/points.md",
                    "--out", str(out)]) == 0
    assert json.loads(out.read_text())["source"] == "docs/points.md"


def test_the_repos_own_config_can_move_the_file(tmp_path):
    (tmp_path / "human-review.json").write_text(
        json.dumps({"reviewPoints": "docs/points.md"}), encoding="utf-8")
    (tmp_path / "docs").mkdir()
    _write(tmp_path, FULL, "docs/points.md")
    assert rp.main(["--root", str(tmp_path), "--check"]) == 0
    assert rp.config_path(tmp_path) == "docs/points.md"


def test_the_reference_documents_the_exit_codes_it_is_read_with():
    """The parser's contract is quoted in the prompt the coding agent follows; a code that
    exists only in the source is a code nobody handles."""
    doc = (HERE.parent / "reference" / "review-points.md").read_text(encoding="utf-8")
    for code in ("0", "3", "4", "5"):
        assert f"| {code} |" in doc
    assert "review-points.py --check" in doc


# --------------------------------------------------------------------------- #
# review-commits.py — which commit is which, over a real repository
# --------------------------------------------------------------------------- #

_rc_spec = importlib.util.spec_from_file_location("review_commits", HERE / "review-commits.py")
rc = importlib.util.module_from_spec(_rc_spec)
_rc_spec.loader.exec_module(rc)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


SESSION = "16a1e790-2c96-4f1b-8a4f-2ddcf2d10a8e"


def _two_commit_branch(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    impl = _commit(repo, "feature.py", "one\n",
                   f"Link visit with vet\n\nClaude-Session: {SESSION}\n")
    (repo / "review-points.md").write_text(FULL)
    (repo / "feature.py").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Take the review's three real findings\n\n"
         f"Review-Points: review-points.md\nImplements: {impl}\n"
         f"Claude-Session: {SESSION}\n")
    return repo, base, impl, _git(repo, "rev-parse", "HEAD")


def test_the_trailers_name_both_commits_and_the_session(tmp_path):
    repo, base, impl, review = _two_commit_branch(tmp_path)
    found = rc.detect(repo, base)
    assert found["implementation"] == impl
    assert found["review"] == review
    assert found["session"] == SESSION
    assert found["fallback"] is False and found["after"] == []
    assert found["warnings"] == []


def test_a_short_implements_sha_is_resolved_to_the_full_one(tmp_path):
    """Trailers get typed by hand and pasted from `git log --oneline`; the phase windows
    compare shas, so a 7-character one has to become the commit it names."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    impl = _commit(repo, "a.py", "one\n", "feature")
    (repo / "review-points.md").write_text(FULL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm",
         f"fixes\n\nReview-Points: review-points.md\nImplements: {impl[:7]}\n")
    assert rc.detect(repo, base)["implementation"] == impl


def test_commits_after_the_review_commit_are_listed_because_nothing_else_shows_them(tmp_path):
    """A page built from the diff cannot see that somebody kept committing once the agent
    stopped — and that is the one change that can make every other claim on it stale."""
    repo, base, _impl, review = _two_commit_branch(tmp_path)
    later = _commit(repo, "feature.py", "three\n", "tweak it by hand")
    found = rc.detect(repo, base)
    assert found["review"] == review
    assert found["after"] == [later]
    assert found["after_detail"][0]["subject"] == "tweak it by hand"


def test_the_fallback_is_the_only_commit_touching_the_file_and_says_it_is_a_guess(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "one\n", "feature")
    review = _commit(repo, "review-points.md", FULL, "fixes and the write-up")
    found = rc.detect(repo, base)
    assert found["review"] == review and found["fallback"] is True
    assert any("falling back" in w for w in found["warnings"])
    assert found["implementation"] is None, "a guessed review commit does not get a " \
                                            "guessed implementation on top"
    assert any("no Implements trailer" in w for w in found["warnings"])


def test_two_commits_touching_the_file_are_not_guessed_between(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "review-points.md", "## Fixed\n", "first write-up")
    _commit(repo, "review-points.md", FULL, "second write-up")
    found = rc.detect(repo, base)
    assert found["review"] is None and found["fallback"] is False
    assert any("cannot be guessed" in w for w in found["warnings"])


def test_nothing_recorded_at_all_is_said_plainly(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "one\n", "feature")
    found = rc.detect(repo, base)
    assert found["review"] is None
    assert any("nobody recorded what was reviewed" in w for w in found["warnings"])
    assert any("no Claude-Session trailer" in w for w in found["warnings"])


def test_two_review_commits_take_the_last_and_name_them_both(tmp_path):
    repo, base, _impl, first = _two_commit_branch(tmp_path)
    (repo / "review-points.md").write_text(FULL + "\n### later\n- file: a.py:1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second round\n\nReview-Points: review-points.md\n")
    second = _git(repo, "rev-parse", "HEAD")
    found = rc.detect(repo, base)
    assert found["review"] == second
    assert any(first[:8] in w and "2 commits carry" in w for w in found["warnings"])


def test_an_unresolvable_implements_is_reported_not_passed_through(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "review-points.md").write_text(FULL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixes\n\nReview-Points: review-points.md\n"
                                "Implements: 0000000000000000000000000000000000000000\n")
    found = rc.detect(repo, base)
    assert found["implementation"] is None
    assert any("does not resolve" in w for w in found["warnings"])


def test_a_trailer_survives_the_cherry_pick_that_a_subject_convention_does_not(tmp_path):
    """The reason this reads trailers at all: a demo branch is rebuilt by cherry-picking,
    which rewrites every sha and keeps every trailer."""
    repo, base, impl, review = _two_commit_branch(tmp_path)
    _git(repo, "checkout", "-q", "-b", "redone", base)
    infra = _commit(repo, "guardrail.py", "assert True\n", "cherry-pick the guardrail first")
    _git(repo, "cherry-pick", impl)
    new_impl = _git(repo, "rev-parse", "HEAD")
    _git(repo, "cherry-pick", review)
    new_review = _git(repo, "rev-parse", "HEAD")
    assert new_impl != impl and new_review != review, "the picks did rewrite the shas"
    found = rc.detect(repo, infra, "HEAD")
    assert found["review"] == new_review
    assert found["session"] == SESSION
    assert found["implementation"] == impl, (
        "Implements still names the original sha, which the cherry-pick did not rewrite — "
        "so it resolves as long as that commit is still reachable")


def test_the_cli_reports_the_pair_and_exits_3_when_there_is_none(tmp_path, capsys):
    repo, base, impl, review = _two_commit_branch(tmp_path)
    assert rc.main(["--root", str(repo), "--base", base]) == 0
    out = capsys.readouterr().out
    assert impl in out and review in out and SESSION in out
    assert "nothing — the branch is as the agent left it" in out

    plain = _repo(tmp_path / "other")
    plain_base = _git(plain, "rev-parse", "HEAD")
    _commit(plain, "a.py", "one\n", "feature")
    assert rc.main(["--root", str(plain), "--base", plain_base, "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["review"] is None


# --------------------------------------------------------------------------- #
# the prompt the coding agent follows — the one drift nobody would notice
# --------------------------------------------------------------------------- #

SKILLS = HERE.parent.parent          # <repo>/skills


def _prompt() -> str:
    return (SKILLS / "implement-ticket" / "prompt.md").read_text(encoding="utf-8")


def test_the_prompt_names_all_three_trailers_the_scripts_read():
    """Each trailer is read by a different script, so a missing one is a silent loss of
    exactly one row on the page — not an error anybody would see."""
    prompt = _prompt()
    for trailer in ("Review-Points:", "Implements:", "Claude-Session:"):
        assert trailer in prompt, f"{trailer} is read by a script and named nowhere"


def test_the_prompt_names_the_file_and_the_checker_the_parser_actually_is():
    prompt = _prompt()
    assert "review-points.md at the repo root" in prompt
    assert "review-points.py --check" in prompt, (
        "the check is the last thing step 4 does — it is what rejects a malformed file "
        "while the agent can still fix it")


def test_the_prompt_points_at_paths_that_exist():
    """A path in a prompt is never resolved by anything, so a wrong one fails as the agent
    quietly skipping the step."""
    prompt = _prompt()
    for rel in ("skills/human-review/reference/review-points.md",
                "skills/human-review/scripts/review-points.py"):
        assert rel in prompt, f"{rel} is not the path the prompt gives"
        assert (SKILLS.parent / rel).is_file(), f"{rel} does not exist"


def test_the_prompt_still_refuses_fix():
    """`--fix` applies the findings to the working tree, which destroys the accept/decline
    record this whole flow exists to capture."""
    assert "Do NOT pass --fix" in _prompt()


def test_the_three_pile_names_are_the_ones_the_parser_accepts():
    prompt = _prompt()
    for heading in ("Fixed", "Ignored", "Assumptions"):
        assert heading in prompt
        assert rp.SECTIONS[heading.lower()]


def test_the_skill_is_discoverable_as_a_skill():
    skill = (SKILLS / "implement-ticket" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    assert "name: implement-ticket" in skill
    assert "disable-model-invocation: true" in skill, (
        "it commits and reviews — it runs when somebody asks for it, never on a guess")
    assert "prompt.md" in skill, "the two entry points must read one text"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
