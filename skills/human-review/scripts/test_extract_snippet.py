"""`preview_line` — the collapsed row's one-line preview of a quoted slice.

A slice that opens on its own comment (a step's JSDoc, a method's `//` note) used to
preview as that comment's opening punctuation: `/**` beside a `path:lines` reference says
nothing about what the slice is. These pin the fix — blank and comment lines are walked
past to find the first line of real code — for every comment style the page snips, and
the fallback when a slice is comment end to end.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location("extract_snippet", HERE / "extract-snippet.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_snippet"] = module
    spec.loader.exec_module(module)
    return module


extract_snippet = _load()
preview_line = extract_snippet.preview_line


def test_a_jsdoc_block_is_skipped_for_the_first_line_of_code():
    lines = ["/**", " * doc", " */", 'Then("x", async function () {']
    assert preview_line(lines) == 'Then("x", async function () {'


def test_a_slice_that_opens_on_code_is_unchanged():
    lines = ["test('Add a visit attended by a vet',", "  {tag: [GENERATE_SEQUENCE_TAG]},"]
    assert preview_line(lines) == "test('Add a visit attended by a vet',"


def test_an_all_comment_slice_falls_back_to_its_own_first_line():
    lines = ["// still just", "// a comment,", "// every line of it"]
    assert preview_line(lines) == "// still just"


def test_a_leading_line_comment_and_blank_lines_are_both_skipped():
    lines = ["", "  // note", "", "  return vetName;"]
    assert preview_line(lines) == "return vetName;"


def test_a_python_hash_comment_is_skipped():
    lines = ["# a note", "def resolve_vet():"]
    assert preview_line(lines) == "def resolve_vet():"


def test_a_sql_dash_comment_is_skipped():
    lines = ["-- explains the join", "SELECT * FROM visit"]
    assert preview_line(lines) == "SELECT * FROM visit"


def test_an_empty_slice_previews_as_an_empty_line():
    assert preview_line([]) == ""
