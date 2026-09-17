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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
