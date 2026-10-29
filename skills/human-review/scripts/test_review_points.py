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

from conftest import page_source

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
- confidence: 0.85
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


NOTE = FULL + """
## Taken over without a new pass — 21 Sep 2026

The commits below sit after the review commit `ce56d912` and were folded in
without re-running the reviewers.

- 6ef4ae6b Rename the no-vet scenario
- f9f9faa4 Make DTO setters fluent

The point this page counts from is now `6ef4ae6b`.
"""


def test_a_takeover_note_is_read_as_prose_and_not_as_a_pile(tmp_path):
    """A second Review-Points commit moves the aftermath anchor; the note beside it is
    the sentence that keeps the piles from reading as a review of commits they never
    saw. It is carried to the page whole, bullets as a list, and adds nothing to any
    pile."""
    doc = _doc(tmp_path, NOTE)
    assert doc["note"]["heading"] == "Taken over without a new pass — 21 Sep 2026"
    assert "<ul><li>6ef4ae6b Rename the no-vet scenario</li>" in doc["note"]["html"]
    assert "<code>ce56d912</code>" in doc["note"]["html"]
    assert set(doc["sections"]) == {"Fixed", "Ignored", "Assumptions"}
    assert doc["items"] == _doc(tmp_path, FULL)["items"]


def test_a_file_without_a_note_says_so(tmp_path):
    assert _doc(tmp_path, FULL)["note"] is None


def test_an_item_under_the_note_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, FULL + "\n## Taken over\n\n### slipped in\n- file: a.py:1\n")
    assert "prose only" in str(bad.value)


def test_an_empty_note_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, FULL + "\n## Carried over\n\n")
    assert "says nothing" in str(bad.value)


def test_a_second_note_is_refused(tmp_path):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, FULL + "\n## Taken over\n\nonce.\n\n## Taken over again\n\ntwice.\n")
    assert "second note" in str(bad.value)


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


def test_two_refs_glued_with_a_comma_are_two_refs(tmp_path):
    """hr-try-4: `- file: a.ts:92, a.ts:288` passed `--check` as ONE ref, and the build
    then aborted on a file called `a.ts:92, a.ts`. Both parts are refs, so both are kept."""
    doc = _doc(tmp_path, "## Fixed\n### t\n- file: src/a.ts:92, src/b.ts:288\n"
                         "- fixed-in: HEAD\n- observation: x.\n")
    item = doc["autofixes"][0]
    assert item["refs"] == ["src/a.ts:92", "src/b.ts:288"]
    assert item["snippets"] == [{"ref": "src/a.ts:92"}, {"ref": "src/b.ts:288"}]
    assert [d["path"] for d in item["diffs"]] == ["src/a.ts", "src/b.ts"]


def test_line_numbers_after_a_comma_stay_on_their_file(tmp_path):
    """`a.py:89,93-95` is one ref with two spans — `extract-snippet.py` quotes it as one
    card — with or without a space after the comma."""
    for value in ("a.py:89,93-95", "a.py:89, 93-95"):
        doc = _doc(tmp_path, f"## Ignored\n### t\n- file: {value}\n")
        assert doc["findings"][0]["refs"] == ["a.py:89,93-95"]
        assert doc["findings"][0]["snippets"] == [{"ref": "a.py:89,93-95"}]


@pytest.mark.parametrize("value, says", [
    ("a.ts:12, the guard clause", "is not a path"),
    ("a.ts:12 b.ts:30", "glued together"),
    ("a.ts:12,, b.ts:3", "empty ref between its commas"),
    ("a.ts, 12", "line number with no file before it"),
    ("a.ts:12, b.ts:30 | both guards", "a caption belongs to one snippet card"),
])
def test_a_file_value_that_is_not_refs_is_refused_and_says_why(tmp_path, value, says):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, f"## Ignored\n### t\n- file: {value}\n")
    assert says in str(bad.value)


def _git_repo_with(tmp_path: Path, files: dict[str, int]) -> Path:
    root = tmp_path / "repo"
    for rel, n in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("".join(f"line {i}\n" for i in range(1, n + 1)))
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "a"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env,
                       capture_output=True)
    return root


def _build():
    spec = importlib.util.spec_from_file_location("build_review_rp", HERE / "build-review-html.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ACCEPTED = ["src/a.ts:12, src/b.ts:30", "src/a.ts:3-5", "src/a.ts:3,7-9", "src/b.ts",
            "src/a.ts:2 | captioned", "src/a.ts:40, src/b.ts"]


def test_the_renderer_draws_every_ref_the_parser_accepts(tmp_path):
    """The guarantee bug 5 broke: whatever `--check` lets through, the page renders —
    the refs resolve and every snippet card is cut — instead of aborting the build."""
    build = _build()
    root = _git_repo_with(tmp_path, {"src/a.ts": 50, "src/b.ts": 50})
    body = "## Ignored\n" + "".join(f"### t{i}\n- file: {v}\n" for i, v in enumerate(ACCEPTED))
    doc = rp.document(_write(tmp_path, body), "review-points.md")
    for item in doc["findings"]:
        assert all(rp.split_refs(r) == ([r], []) for r in item["refs"]), item["refs"]
        build.resolve_refs(item["refs"], root)
        for s in item.get("snippets", []):
            assert "srcbar" in build.snippet_html(s["ref"], s.get("caption"), root)


def test_a_report_with_a_glued_ref_from_an_older_parser_still_renders(tmp_path, capsys):
    """hr-try-4's `review-points.json` was written before the parser split commas. The
    build splits such a ref itself, and drops (with a warning) one it cannot read, rather
    than abort the page on a file named `a.ts:92, b.ts`."""
    build = _build()
    root = _git_repo_with(tmp_path, {"src/a.ts": 300, "src/b.ts": 300})
    out_dir = root / ".human-review"
    out_dir.mkdir()
    doc = rp.document(_write(tmp_path, "## Fixed\n### t\n- file: src/a.ts:9\n"
                                       "- fixed-in: HEAD\n- observation: x.\n"
                                       "## Ignored\n### u\n- file: src/a.ts:1\n"),
                      "review-points.md")
    glued = "src/a.ts:92, src/b.ts:288"
    doc["autofixes"][0].update(refs=[glued], snippets=[{"ref": glued, "caption": "c"}],
                               diffs=[{"path": "src/a.ts:92, src/b.ts", "base": "x"}])
    doc["findings"][0].update(refs=["src/a.ts:1, the guard"])
    (out_dir / "review-points.json").write_text(json.dumps(doc), encoding="utf-8")
    spec = {"findings": {"auto": "review-points"}, "autofixes": {"auto": "review-points"},
            "assumptions": {"auto": "review-points"}}
    build.resolve_review_points(spec, out_dir)
    fix = spec["autofixes"][0]
    assert fix["refs"] == ["src/a.ts:92", "src/b.ts:288"]
    assert fix["snippets"] == [{"ref": "src/a.ts:92", "caption": "c"}, {"ref": "src/b.ts:288"}]
    assert [d["path"] for d in fix["diffs"]] == ["src/a.ts", "src/b.ts"]
    assert spec["findings"][0]["refs"] == []
    assert "dropped" in capsys.readouterr().err
    for item in spec["autofixes"] + spec["findings"]:
        build.resolve_refs(item["refs"], root)
        for s in item.get("snippets", []):
            build.snippet_html(s["ref"], s.get("caption"), root)


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
    # Stamped by who decided it, never by a free-text source the page would echo.
    assert doc["assumptions"][0]["decidedBy"] == "agent"
    assert "source" not in doc["assumptions"][0]
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


def test_a_wrapped_field_in_the_middle_continues_onto_the_indented_line(tmp_path):
    doc = _doc(tmp_path, "## Assumptions\n### t\n- file: a.py:1\n"
                         "- alternative: annotate bookVisit as asked — which would be a\n"
                         "  silent no-op\n"
                         "- why: Spring AOP ignores self-invoked private methods.\n")
    item = doc["assumptions"][0]
    assert item["alternative"] == ("annotate bookVisit as asked — which would be a silent "
                                   "no-op")
    assert item["why"] == "Spring AOP ignores self-invoked private methods."


def test_a_wrapped_last_field_continues_instead_of_being_truncated_into_the_body(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n"
                         "- why: out of scope for #37, and deleting the flat endpoint\n"
                         "  is an API break.\n"
                         "\n"
                         "More context follows as ordinary prose.\n")
    item = doc["findings"][0]
    assert item["why"] == ("out of scope for #37, and deleting the flat endpoint is an "
                           "API break.")
    assert item["body"] == "More context follows as ordinary prose."


def test_an_unindented_line_after_a_field_still_starts_the_body(tmp_path):
    doc = _doc(tmp_path, "## Ignored\n### t\n- file: a.py:1\n"
                         "- why: short.\n"
                         "Not indented, so this is body, not a continuation.\n")
    item = doc["findings"][0]
    assert item["why"] == "short."
    assert item["body"] == "Not indented, so this is body, not a continuation."


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
    assert set(doc["assumptions"][0]) <= {"title", "body", "why", "decidedBy", "refs",
                                          "snippets", "alternative", "diffs",
                                          "confidence"}
    src = page_source()
    for key in ("refs", "snippets", "diffs", "severity", "alternative", "why", "decidedBy"):
        assert f'"{key}"' in src or f"'{key}'" in src


# --------------------------------------------------------------------------- #
# confidence — how sure the agent is about the reading it chose
# --------------------------------------------------------------------------- #

def test_confidence_reaches_the_item_as_a_number(tmp_path):
    """A number, not a string: the page has to compare it against a threshold to decide
    how loudly to say it, and a string that sorts `"0.9" < "0.85"` would be worse than
    having nothing."""
    doc = _doc(tmp_path, "## Assumptions\n### t\n- file: a.py:1\n- confidence: 0.85\n")
    item = doc["assumptions"][0]
    assert item["confidence"] == 0.85
    assert isinstance(item["confidence"], float)


@pytest.mark.parametrize("written,read", [
    ("0", 0.0), ("1", 1.0), ("0.0", 0.0), ("1.0", 1.0), ("0.3", 0.3), ("1.00", 1.0),
    ("0.857", 0.86), ("0.3333", 0.33),
])
def test_the_ends_of_the_scale_are_inside_it_and_a_third_decimal_rounds(tmp_path, written,
                                                                       read):
    doc = _doc(tmp_path, f"## Assumptions\n### t\n- file: a.py:1\n"
                         f"- confidence: {written}\n")
    assert doc["assumptions"][0]["confidence"] == read


def test_an_assumption_without_confidence_says_nothing_rather_than_defaulting(tmp_path):
    """Every file written before the field existed still parses — and none of them
    acquires a number the agent never typed. A defaulted confidence would be the page
    answering, on the agent's behalf, the one question only the agent could answer."""
    doc = _doc(tmp_path, "## Assumptions\n### t\n- file: a.py:1\n- why: because.\n")
    assert "confidence" not in doc["assumptions"][0]
    assert doc["warnings"] == []


@pytest.mark.parametrize("value", ["1.5", "-0.1", "2", "95", "inf"])
def test_a_confidence_outside_the_scale_is_refused_with_its_line(tmp_path, value):
    """There is no being surer than certain. A typo'd `0.95` written as `95` would
    otherwise reach the page as a confidence nobody has."""
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, f"## Assumptions\n### t\n- file: a.py:1\n"
                       f"- confidence: {value}\n")
    assert "line 2" in str(bad.value) and "between 0 and 1" in str(bad.value)


@pytest.mark.parametrize("value", ["high", "0.8ish", "", "85%"])
def test_a_confidence_that_is_not_a_number_is_refused(tmp_path, value):
    with pytest.raises(rp.Unparseable) as bad:
        _doc(tmp_path, f"## Assumptions\n### t\n- file: a.py:1\n"
                       f"- confidence: {value}\n")
    assert "confidence" in str(bad.value) and "line 2" in str(bad.value)


@pytest.mark.parametrize("heading,pile", [("Fixed", "autofixes"), ("Ignored", "findings")])
def test_confidence_on_a_fix_or_a_decline_is_warned_about_and_dropped(tmp_path, heading,
                                                                     pile):
    """Softer than `severity:` on an assumption, which is fatal, and deliberately so: a
    confidence in the wrong pile is a misplaced field, while ranking a decision the reader
    must confirm as though it were a defect is a category error."""
    doc = _doc(tmp_path, f"## {heading}\n### Something I repaired\n- file: a.py:1\n"
                         f"- confidence: 0.9\n")
    assert "confidence" not in doc[pile][0], "a fix is in the diff or it is not"
    assert len([w for w in doc["warnings"] if "observation" not in w]) == 1
    assert "Something I repaired" in doc["warnings"][0]
    assert heading in doc["warnings"][0]


def test_an_out_of_range_confidence_on_a_fix_is_still_only_a_warning(tmp_path):
    """The pile is checked before the number is: the field does not belong there at all,
    so refusing the file over the value of a field nobody will read would be reporting the
    second problem and hiding the first."""
    doc = _doc(tmp_path, "## Fixed\n### t\n- file: a.py:1\n- confidence: 7\n")
    assert "confidence" not in doc["autofixes"][0]
    assert len([w for w in doc["warnings"] if "observation" not in w]) == 1


def test_confidence_is_printed_beside_the_title_by_check(tmp_path, capsys):
    """`--check` is what the agent runs before committing, so it is where a confidence it
    did not mean to write — or forgot to — has to be visible."""
    _write(tmp_path, "## Assumptions\n### The reading I chose\n- file: a.py:1\n"
                     "- confidence: 0.4\n")
    assert rp.main(["--root", str(tmp_path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "The reading I chose" in out
    assert "confidence 0.4" in out


def test_confidence_survives_into_the_json_the_build_reads(tmp_path):
    _write(tmp_path, FULL)
    assert rp.main(["--root", str(tmp_path)]) == 0
    data = json.loads((tmp_path / ".human-review" / "review-points.json")
                      .read_text(encoding="utf-8"))
    assert data["assumptions"][0]["confidence"] == 0.85


def test_the_reference_documents_the_confidence_scale():
    """The scale is the whole field: a number with no agreed meaning is a number every
    agent picks differently, and the page would be averaging apples."""
    doc = (HERE.parent / "reference" / "review-points.md").read_text(encoding="utf-8")
    assert "- confidence: 0.85" in doc, "the format example has to show it"
    for phrase in ("the ticket left no other reading", "coin flip", "expects to be "
                   "corrected"):
        assert phrase in doc



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


HARNESS_TRAILER = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"


def test_the_trailers_are_read_out_of_the_body_when_the_harness_appends_its_own(tmp_path):
    """The case the first real `/implement-ticket` run produced, and the reason this reads
    the body at all: Claude Code adds `Co-Authored-By:` as a paragraph of its own *after*
    whatever the agent wrote, so git's trailer parser — which only looks at the last
    paragraph — finds none of the three keys and the page reports two perfectly trailered
    commits as "not recorded"."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    impl = _commit(repo, "a.py", "one\n",
                   f"feature\n\nClaude-Session: {SESSION}\n\n{HARNESS_TRAILER}\n")
    (repo / "review-points.md").write_text(FULL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm",
         "fixes\n\nReview-Points: review-points.md\n"
         f"Implements: {impl}\nClaude-Session: {SESSION}\n\n{HARNESS_TRAILER}\n")
    review = _git(repo, "rev-parse", "HEAD")

    assert _git(repo, "log", "-1",
                "--format=%(trailers:key=Review-Points,valueonly)").strip() == "", \
        "git itself sees no trailer here — that is the whole point of the fallback"

    found = rc.detect(repo, base)
    assert found["review"] == review
    assert found["implementation"] == impl
    assert found["session"] == SESSION
    assert found["fallback"] is False, "a key on its own line IS the record, not a guess"
    assert found["warnings"] == [], "reading the body is the normal case, not a complaint"


def test_a_trailer_line_is_recognised_wherever_in_the_message_it_sits(tmp_path):
    """Not only the penultimate paragraph: an agent that writes the keys mid-message, or a
    squash that buries them, still recorded them."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    impl = _commit(repo, "a.py", "one\n", "feature")
    (repo / "review-points.md").write_text(FULL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm",
         f"fixes\n\nReview-Points: review-points.md\nImplements: {impl}\n"
         f"Claude-Session: {SESSION}\n\nand then some prose about what changed, which\n"
         "makes the keys neither the last paragraph nor a trailer block.\n")
    found = rc.detect(repo, base)
    assert (found["review"], found["implementation"], found["session"]) == (
        _git(repo, "rev-parse", "HEAD"), impl, SESSION)
    assert found["warnings"] == []


def test_a_key_inside_the_prose_is_not_a_record(tmp_path):
    """`Implements:` has to start the line. Quoted mid-sentence it is somebody describing
    the convention, and taking it as the record would base every fix diff on a sha the
    sentence merely mentioned."""
    assert rc.from_body("we agreed the commit Implements: abc123 the ticket\n") == {}
    assert rc.from_body("Implements:\n") == {}, "a key with no value records nothing"
    assert rc.from_body(f"Implements: {'a' * 40}\n") == {"implements": "a" * 40}


def test_two_body_lines_for_one_key_arrive_the_way_git_would_have_joined_them(tmp_path):
    """`%(trailers:…,separator=%x2C)` comma-joins repeats, and every reader downstream
    takes `.split(",")[0]` — so the body path has to produce the same shape."""
    assert rc.from_body("Implements: aaa\nImplements: bbb\n") == {"implements": "aaa,bbb"}


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


def test_auto_fix_commits_are_listed_and_a_later_one_is_marked(tmp_path):
    """`[auto-fix]` on the subject is how the agent's own fixes are found again later —
    the review commit, and a second round after it, which is not a human's hand edit."""
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    impl = _commit(repo, "a.py", "one\n", "feature")
    (repo / "review-points.md").write_text(FULL)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "[auto-fix] take the review's findings\n\n"
                                f"Review-Points: review-points.md\nImplements: {impl}\n")
    review = _git(repo, "rev-parse", "HEAD")
    again = _commit(repo, "a.py", "two\n", "[auto-fix] second round")
    human = _commit(repo, "a.py", "three\n", "hand edit")
    found = rc.detect(repo, base)
    assert found["review"] == review
    assert [c["sha"] for c in found["auto_fixes"]] == [review, again]
    assert [(c["sha"], c["auto_fix"]) for c in found["after_detail"]] == [
        (again, True), (human, False)]


def test_with_no_trailer_the_last_auto_fix_commit_is_the_review_commit(tmp_path):
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "one\n", "feature")
    _commit(repo, "review-points.md", FULL, "[auto-fix] apply the review")
    tagged = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "review-points.md", FULL + "\n", "touch the record again")
    found = rc.detect(repo, base)
    assert found["review"] == tagged, "two commits touch the file; the tag decides"
    assert any("[auto-fix]" in w for w in found["warnings"])


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
    return (SKILLS / "record-review" / "prompt.md").read_text(encoding="utf-8")


def _script() -> str:
    return (SKILLS / "record-review" / "record-review.py").read_text(encoding="utf-8")


def test_the_script_writes_all_three_trailers_the_scripts_read():
    """Each trailer is read by a different script, so a missing one is a silent loss of
    exactly one row on the page. The agent no longer types them: the script does, which
    is the only way they stop drifting from what review-commits.py parses."""
    script = _script()
    for trailer in ('"Review-Points"', '"Implements"', '"Claude-Session"'):
        assert trailer in script, f"{trailer} is read by a script and written nowhere"


def test_the_script_tags_the_auto_fix_commit():
    """`[auto-fix]` on the subject is how review-driven changes are found again later,
    and `review-commits.py` lists them by it — the two have to agree on the tag."""
    assert rc.AUTO_FIX_TAG in _script()


def test_the_prompt_leaves_the_mechanics_to_the_script():
    """What the first VS Code run spent its 332 tool calls on: the paths, the base, the
    trailers, the PR comments. Each of those has one right answer, so a program gives it."""
    prompt = _prompt()
    assert "record-review.py" in prompt
    assert "prepare" in prompt and "finish" in prompt
    assert "Commit nothing yourself" in prompt


def test_the_prompt_points_at_paths_that_exist():
    """A path in a prompt is never resolved by anything, so a wrong one fails as the agent
    quietly skipping the step."""
    prompt = _prompt()
    rel = "skills/human-review/reference/review-points.md"
    assert rel in prompt and (SKILLS.parent / rel).is_file()
    assert "../human-review/reference/review-points.md" in prompt
    assert (SKILLS / "record-review" / "record-review.py").is_file()


def test_the_prompt_still_refuses_fix():
    """`--fix` applies the findings to the working tree, which destroys the accept/decline
    record this whole flow exists to capture."""
    assert "Do NOT pass --fix" in _prompt()


def test_the_prompt_keeps_review_points_terse():
    prompt = _prompt()
    assert "15 words at most" in prompt
    assert "no prose under an" in prompt
    doc = (HERE.parent / "reference" / "review-points.md").read_text(encoding="utf-8")
    assert "Terse by design" in doc


def test_the_three_pile_names_are_the_ones_the_parser_accepts():
    prompt = _prompt()
    for heading in ("Fixed", "Ignored", "Assumptions"):
        assert heading in prompt
        assert rp.SECTIONS[heading.lower()]


def test_the_skill_explains_why_the_body_is_read_and_not_just_the_trailer_block():
    skill = (SKILLS / "record-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "penultimate" in skill and "Co-Authored-By" in skill
    assert "%(trailers" in skill, "the git construct that returns empty here is the fact"


def test_the_skill_is_discoverable_as_a_skill():
    skill = (SKILLS / "record-review" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    assert "name: record-review" in skill
    assert "disable-model-invocation: true" in skill, (
        "it commits and reviews — it runs when somebody asks for it, never on a guess")
    assert "prompt.md" in skill, "the two entry points must read one text"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_fixed_item_says_what_the_reviewer_found_and_how_it_was_repaired(tmp_path):
    """`void this.router.navigate(…)` under a title told the reader that something changed,
    never what was wrong. The observation is that sentence; `fix:` is optional."""
    doc = _doc(tmp_path, "## Fixed\n### Floating router promise\n- file: a.ts:141\n"
                         "- observation: navigate() returns a promise nobody awaits.\n"
                         "- fix: mark it void, as the lint rule asks.\n"
                         "## Ignored\n### Bare decline\n- file: a.ts:2\n")
    fixed = doc["autofixes"][0]
    assert "nobody awaits" in fixed["observation"] and "void" in fixed["fix"]
    assert any("Bare decline" in w and "observation" in w for w in doc["warnings"])


# ── a refuted finding, and an assumption that does not say why it is that sure ──────
# Run 5 filed "HTTP errors reported as …" under Ignored at `severity: medium` with
# `why: wrong — handleError rethrows`: a finding the agent had disproved, counted on the
# page as "worth a look" and in the grade as an open issue.

REFUTED = """## Fixed

### Guard the empty list
- file: a.py:2
- severity: low
- observation: The loop indexes [0] on an empty list.
- fix: the reviewer was wrong, nothing to change
- fixed-in: HEAD

## Ignored

### HTTP errors reported as a missing page
- file: b.ts:43
- severity: medium
- observation: The reviewer claims handleError swallows a 400.
- why: wrong — handleError rethrows; the service spec asserts a 400 reaches the caller.

### Same refutation, filed the way the prompt asks
- file: b.ts:50
- severity: info
- observation: The reviewer claims the retry loops forever.
- why: refuted — the retry is capped at three by RetryPolicy.

### Wrong status code is a real defect, kept open
- file: b.ts:60
- severity: medium
- observation: Wrong status code returned on a missing owner.
- why: out of scope for #25.

## Assumptions

### Empty sort falls back to the default
- file: c.java:109
- alternative: reject it with 400
- confidence: 0.6
"""


def test_a_finding_that_says_the_reviewer_was_wrong_is_flagged_unless_filed_at_info(tmp_path):
    warnings = rp.parse(REFUTED)["warnings"]
    said = [w for w in warnings if "reviewer was wrong" in w]
    assert any("Guard the empty list" in w and "`fix`" in w for w in said)
    assert any("HTTP errors reported" in w and "`why`" in w for w in said)
    # Filed under Ignored at info, as asked: no warning. An observation that merely opens
    # on "Wrong …" is the defect itself, not a refutation.
    assert not any("Same refutation" in w or "Wrong status code" in w for w in said)


def test_is_refuted_reads_an_info_findings_why_and_nothing_else():
    """The page counts these apart from the open pile (eval run 8: "11 open" over four
    CONTEXT cards that said "refuted — …"). One reading, here, that the page reuses."""
    assert rp.is_refuted({"severity": "info",
                          "why": "refuted — this spec asserts <code>%2B</code>"})
    assert rp.is_refuted({"severity": "info", "why": "wrong — handleError rethrows"})
    assert rp.is_refuted({"why": "a false positive: the guard is upstream"})   # info default
    assert not rp.is_refuted({"severity": "low", "why": "refuted — filed at low"}), \
        "above info it is still open; the parser warns about the filing instead"
    assert not rp.is_refuted({"severity": "info", "why": "deliberate — out of scope",
                              "observation": "the reviewer said this was refuted"})


def test_an_assumption_with_no_why_is_told_to_say_what_holds_its_confidence(tmp_path):
    warnings = rp.parse(REFUTED)["warnings"]
    assert any("Empty sort falls back" in w and "no `why:`" in w
               and "confidence where it is" in w for w in warnings)


CITED = """## Ignored

### V4 indexes nobody asked for
- file: app.py:3
- source: ticket-fit reviewer
- observation: three indexes add write overhead.
- why: design.md Decision 3 and task 2.1 specify them.

### Name sorts by last name
- file: app.py:4
- source: tests reviewer
- observation: rows read as unsorted.
- why: Q3 decided by the human, Q&A.md:28.

### Query budget is fine
- file: app.py:5
- source: tests reviewer
- observation: may mask lazy loads.
- why: refuted — statistics count every statement.
"""


def test_a_reason_resting_on_the_spec_without_its_line_is_warned_about():
    """Eval run 10: open issues were declined on "design.md Decision 3 and task 2.1" and
    "Q3 decided by the human", and nothing named the line either rests on. A reason that
    cites a document must give the `file:line`, so the page can link and quote it."""
    warnings = rp.parse(CITED)["warnings"]
    cited = [w for w in warnings if "without the file:line it cites" in w]
    assert len(cited) == 1 and "V4 indexes nobody asked for" in cited[0], warnings
    assert "'design.md'" in cited[0]


# ── eval run 6 ──────────────────────────────────────────────────────────────────

def test_a_code_span_is_escaped_once(tmp_path):
    """Run 6: `?size=&page=` in backticks reached the JSON as `&amp;amp;` and the screen as
    `&amp;` — the line was escaped whole, then the code span again."""
    assert rp.inline("Reviewer: `?size=&page=` answers 200 & <b>") == \
        "Reviewer: <code>?size=&amp;page=</code> answers 200 &amp; &lt;b&gt;"
    doc = _doc(tmp_path, "## Ignored\n### Empty `size=&page=`\n- file: a.py:1\n"
                         "- observation: encodes `+ & % #` only\n")
    item = doc["findings"][0]
    assert item["title"] == "Empty <code>size=&amp;page=</code>"
    assert item["observation"] == "encodes <code>+ &amp; % #</code> only"
    assert "&amp;amp;" not in json.dumps(doc)


def _two_commits(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    old = [f"l{i}" for i in range(1, 21)]
    (tmp_path / "a.py").write_text("\n".join(old) + "\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    new = old[:3] + ["new1", "", "new2"] + old[3:9] + old[10:]   # +3 after l3, l10 gone
    new[new.index("l15")] = "l15 rewritten"
    (tmp_path / "a.py").write_text("\n".join(new) + "\n")
    git("commit", "-qam", "[auto-fix]")
    return impl, git("rev-parse", "HEAD")


def test_a_ref_is_carried_through_the_hunks_of_the_diff(tmp_path):
    impl, fix = _two_commits(tmp_path)
    assert rp.remap_ref(tmp_path, "a.py:2", impl) == "a.py:2"          # above every hunk
    assert rp.remap_ref(tmp_path, "a.py:5", impl) == "a.py:8"          # below an insertion
    assert rp.remap_ref(tmp_path, "a.py:10", impl) is None             # deleted
    assert rp.remap_ref(tmp_path, "a.py:15", impl) == "a.py:17"        # rewritten in place
    assert rp.remap_ref(tmp_path, "a.py:9-11", impl) == "a.py:12-13"   # a range, one line lost
    assert rp.remap_ref(tmp_path, "a.py", impl) == "a.py"              # no lines, nothing to do
    assert rp.remap_ref(tmp_path, "a.py:5", fix) == "a.py:5"           # already at HEAD


def test_reanchor_falls_back_past_a_blank_line_but_never_past_a_deleted_one(tmp_path):
    impl, fix = _two_commits(tmp_path)
    # Written at HEAD: line 5 is blank there, read as the implementation's l5 → 8.
    got = rp.reanchor(tmp_path, "a.py:5", [fix, impl])
    assert got == {"ref": "a.py:8", "from": impl, "moved": True, "blank": False}
    # The implementation's l10 is gone: said, not swapped for whatever now sits at 10.
    got = rp.reanchor(tmp_path, "a.py:10", [impl, fix])
    assert got["ref"] is None and got["from"] == impl
    assert rp.reanchor(tmp_path, "a.py:5", [rp.WORKTREE, impl])["ref"] == "a.py:8"


# ── eval run 11 ─────────────────────────────────────────────────────────────────

def test_an_anchor_spanning_more_than_a_dozen_lines_is_warned_about():
    """Run 11 quoted a 41-line class on one card. A range longer than the page opens is
    named, so the agent anchors the lines that do the thing."""
    doc = """## Fixed

### Retry on a failed page
- file: a.ts:270-284
- file: b.ts:10-21
- observation: no way to retry.
"""
    said = [w for w in rp.parse(doc)["warnings"] if "spans" in w]
    assert len(said) == 1 and "`file: a.ts:270-284` spans 15 lines" in said[0], said
    assert f"opens {rp.ANCHOR_LINES} and folds the rest" in said[0]


# ── visit-has-vet: the record re-recorded after the review, front-matter only ──────────

def test_a_commit_that_only_moves_the_front_matter_is_not_where_the_refs_were_written(
        tmp_path):
    """`5f84b2cd review-points: base moves to 33977c2c after merging main` touched only
    `base:`; taking it as the review commit read every ref against a tree seven commits
    after the one they were written in."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    rec = "---\nbase: aaa\n---\n\n## Fixed\n\n### X\n- file: a.py:2\n"
    (tmp_path / "review-points.md").write_text(rec)
    (tmp_path / "a.py").write_text("x\ny\n")
    git("add", ".")
    git("commit", "-qm", "review")
    review = git("rev-parse", "HEAD")
    (tmp_path / "a.py").write_text("new\nx\ny\n")
    git("commit", "-qam", "retouch")
    (tmp_path / "review-points.md").write_text(rec.replace("base: aaa", "base: bbb"))
    git("commit", "-qam", "review-points: base moves")
    assert rp.recorded_in(tmp_path, "review-points.md") == review
    (tmp_path / "review-points.md").write_text(rec.replace("a.py:2", "a.py:3"))
    git("commit", "-qam", "review-points: re-anchor")
    assert rp.recorded_in(tmp_path, "review-points.md") == git("rev-parse", "HEAD")


def test_a_line_split_in_two_is_carried_to_the_half_most_like_it(tmp_path):
    """`20e1df32` rewrote `visit.setVet(vetRepository.findByIdOrNull(…))` as
    `Vet vet = vetRepository.findByIdOrNull(…)` + `visit.setVet(vet)`. The ref used to be
    called deleted, and the page quoted whatever had line 192 by then: `@WithSpan`."""
    hunks = [{"a": 3, "b": 1, "c": 3, "d": 2,
              "old": ["visit.setVet(vetRepository.findByIdOrNull(dto.getVetId()));"],
              "new": ["Vet vet = vetRepository.findByIdOrNull(dto.getVetId());",
                      "visit.setVet(vet);"]}]
    assert rp.map_line(hunks, 3) == 3
    assert rp.map_line(hunks, 5) == 6
    unlike = [{**hunks[0], "new": ["log.info(\"x\");", "return 1;"]}]
    assert rp.map_line(unlike, 3) is None, "a line replaced by something else is gone"


def test_each_fixed_item_names_the_commit_that_fixed_it(tmp_path):
    """Visits-paginated's round had two `[auto-fix]` commits, e1d37517 for the backend and
    4321c5bc for the grid. Each Fixed item's buttons go to its own (`fixCommit`): the commit
    that touched its files, told apart by who last wrote its lines when several did."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "A.java").write_text("a1\na2\na3\n")
    (repo / "B.ts").write_text("b1\nb2\nb3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "impl")
    impl = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "A.java").write_text("a1\nA2\na3\n")
    _git(repo, "commit", "-qam", "[auto-fix] backend")
    first = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "A.java").write_text("a1\nA2\nA3\n")
    (repo / "B.ts").write_text("b1\nB2\nb3\n")
    _git(repo, "commit", "-qam", "[auto-fix] grid")
    second = _git(repo, "rev-parse", "HEAD").strip()
    prov = {"implementation": impl, "reviewCommit": second}
    assert rp.round_fix_commit(repo, prov) == second
    assert rp.fix_commit_of(repo, ["B.ts:2"], prov) == second
    # Both commits touched A.java; its line 2 was last written by the first.
    assert rp.fix_commit_of(repo, ["A.java:2"], prov) == first
    assert rp.fix_commit_of(repo, ["A.java:3"], prov) == second
    assert rp.fix_commit_of(repo, ["A.java:2"], prov, own=impl) == impl, "fixed-in wins"
    # Refs on a file the round never touched: the round's own commit.
    assert rp.fix_commit_of(repo, ["C.md:1"], prov) == second
    # No implementation to start a range from: `fixed-in: HEAD` is the recording commit.
    assert rp.fix_commit_of(repo, ["A.java:2"], {"reviewCommit": first}, pinned="HEAD") == first
    assert rp.fix_commit_of(repo, ["A.java:2"], {"reviewCommit": first}) is None
