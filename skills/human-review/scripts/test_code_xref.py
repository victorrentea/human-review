#!/usr/bin/env python3
"""What the cross-links may claim, and what they must refuse to claim.

A link that lands on the wrong code is worse than no link: the reader follows it, reads
something plausible, and carries the wrong answer away. So the interesting half of this
file is the refusals — a local variable that opens a bracket where a parameter list would
go, a Cucumber keyword whose callback ends the line in `{`, a name nothing on the page
defines. Each of those shipped as a link once.

Run with:  python3 -m pytest test_code_xref.py
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("code_xref", HERE / "code_xref.py")
xref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xref)


# --- reading and rewriting one highlighted line ----------------------------------------

def test_plain_text_is_the_source_line_the_markup_shows():
    line = '<span class="k">  When </span><span class="nf">a pet&#39;s visit</span>'
    assert xref.plain(line) == "  When a pet's visit"


def test_a_range_crossing_a_token_boundary_is_wrapped_as_two_anchors():
    """Pygments splits a Gherkin step at its quoted argument, so the sentence is three
    spans. One anchor opened inside the first and closed inside the third would not be a
    tree; several flush anchors are, and they read as one link."""
    line = ('<span class="k">When </span><span class="nf">I book with </span>'
            '<span class="s">"Helen"</span>')
    out = xref.wrap(line, 5, len(xref.plain(line)), '<a class="xref">')
    assert out.count('<a class="xref">') == out.count("</a>") == 2
    assert xref.plain(out) == xref.plain(line)
    # …and never straddling a tag: every anchor closes before the span it opened in does.
    assert "</a></span>" in out and "<span" not in out.split('<a class="xref">')[1].split("</a>")[0]


def test_an_entity_counts_as_the_one_character_the_reader_sees():
    line = '<span class="nf">pet&#39;s history</span>'
    out = xref.wrap(line, 0, 5, '<a class="xref">')
    assert "<a class=\"xref\">pet&#39;s</a>" in out


def test_an_empty_range_leaves_the_line_alone():
    line = '<span class="nf">nothing to mark</span>'
    assert xref.wrap(line, 4, 4, '<a>') == line


# --- what a window may claim to define -------------------------------------------------

def test_the_shapes_a_definition_really_takes():
    ts = [
        "export async function open_owner_detail_page(page: Page): Promise<void> {",
        "const bookTheVisit = async (world) => {",
        "  private helperMethod(page: Page): void {",
    ]
    assert set(xref.defined_names(".ts", ts)) == {
        "open_owner_detail_page", "bookTheVisit", "helperMethod"}
    assert xref.defined_names(".java", [
        "  private VisitDto callGet(int visitId) throws Exception {"]) == {"callGet": 1}
    assert xref.defined_names(".py", ["async def fetch_owner(client):"]) == {"fetch_owner": 1}


def test_a_local_bound_to_an_expression_is_not_a_definition():
    """`const vetName = (await cell.textContent()).trim()` opens a bracket exactly where a
    parameter list would. Read as a definition, it made every later mention of a local
    variable a link back to the line that computed it — three of them in one window."""
    assert xref.defined_names(".ts", [
        "  const vetName = (await firstVet.textContent() || '').trim();"]) == {}


def test_a_cucumber_step_is_not_a_method_called_when():
    """The callback is the last argument, so the line ends in `{` and reads like a
    declaration. The page offered "Show where When is defined", which is node_modules."""
    assert xref.defined_names(".ts", [
        "When('I book a visit', async function (this: PlaywrightWorld) {"]) == {}


def test_a_call_is_not_a_declaration():
    assert xref.defined_names(".ts", ["  await bookVisit(this, vetName);"]) == {}
    assert xref.defined_names(".java", ["    if (visit.getVet() != null) {"]) == {}


# --- what a scenario sentence binds to -------------------------------------------------

def test_a_cucumber_expression_matches_the_sentence_a_reader_wrote():
    pattern = xref.step_pattern("I book a visit with {string} attending", False)
    assert pattern.match('I book a visit with "Helen Leary" attending')
    assert not pattern.match("I book a visit with nobody attending")
    assert xref.step_pattern("I have {int} cucumber(s)", False).match("I have 4 cucumbers")
    assert xref.step_pattern("^the vet list$", True).match("the vet list")


def test_a_pattern_that_will_not_compile_costs_the_link_and_not_the_build():
    assert xref.step_pattern("(unclosed", True) is None


def test_step_definitions_in_both_dialects_and_through_the_source_escape():
    js = ["Then('that pet\\'s history shows the visit', async function () {"]
    (pattern, line), = xref.defined_steps(".ts", js)
    assert line == 1 and pattern.match("that pet's history shows the visit")
    (pattern, line), = xref.defined_steps(".java", ['  @When("the receptionist books {string}")'])
    assert pattern.match('the receptionist books "a visit"')


# --- the whole pass, over a document ---------------------------------------------------

def figure(href: str, first: int, lines: list[str]) -> str:
    rows = "\n".join(
        f'<span class="ln-row"><span class="dm"> </span><span class="ln">{first + i}</span>'
        f'<span class="x">{line}</span></span>'
        for i, line in enumerate(lines))
    return ('<figure class="snippet">'
            f'<div class="srcbar"><a class="srcref srcbar-path" href="{href}"'
            ' data-tip="Open in VS Code">f</a></div>'
            f'<pre class="code lang-x"><code>{rows}</code></pre></figure>')


def page(*blocks: str) -> str:
    return "<!doctype html><html><body>" + "".join(blocks) + "</body></html>"


def test_a_scenario_sentence_reaches_the_glue_quoted_beside_it():
    doc = xref.cross_link(page(
        figure("vscode://file//r/add-visit.feature:15:1", 15,
               ["  When I book a visit with &quot;Helen&quot; attending"]),
        figure("vscode://file//r/glue.ts:20:1", 20,
               ["When('I book a visit with {string} attending', async function () {",
                "  await bookVisit(this);", "});"])))
    link = re.search(r'<a class="xref"[^>]*>', doc)
    assert link, doc
    assert 'data-xref-line="20"' in link[0]
    assert "glue.ts:20:1" in link[0]
    # The window it points at knows its own name, and the index is on the page for the
    # browser to read.
    assert doc.count('<figure class="snippet" data-xref-id="') == 2
    assert '<script type="application/json" id="xref-index">' in doc


def test_a_name_nothing_on_the_page_defines_stays_plain_text():
    """The index is built from the windows the report chose to quote. A call whose callee
    is not one of them is not followable, and the page must not pretend it is."""
    doc = xref.cross_link(page(
        figure("vscode://file//r/glue.ts:20:1", 20, ["  await somethingElsewhere(this);"])))
    assert "xref" not in doc


def test_a_name_does_not_resolve_across_languages():
    """`bookVisit` is a local helper in the TypeScript glue and a `private int
    bookVisit(VisitDto)` in a Spring controller quoted three tabs away. The glue's call
    linked to the controller, and the link read exactly like the true ones beside it."""
    doc = xref.cross_link(page(
        figure("vscode://file//r/glue.ts:78:1", 78, ["    await bookVisit(this, vetName);"]),
        figure("vscode://file//r/VisitRestController.java:81:1", 81,
               ["    private int bookVisit(VisitDto visitDto) {", "    }"])))
    assert "xref" not in doc


def test_the_definition_in_the_file_being_read_wins():
    """Two windows declare `visitRow`. The call is in a third, cut from the same file as one
    of them, and that is the one it means."""
    doc = xref.cross_link(page(
        figure("vscode://file//r/dsl.ts:10:1", 10, ["export function visitRow(page) {", "}"]),
        figure("vscode://file//r/other.ts:40:1", 40, ["export function visitRow(page) {", "}"]),
        figure("vscode://file//r/dsl.ts:50:1", 50, ["  return visitRow(page).first();"])))
    link, = re.findall(r'<a class="xref"[^>]*href="([^"]*)"', doc)
    assert link.endswith("dsl.ts:10:1"), link


def test_a_document_with_nothing_to_link_comes_back_untouched():
    plain_page = page("<p>no code here</p>")
    assert xref.cross_link(plain_page) == plain_page


def rm_page(tests: dict) -> str:
    return page('<script type="application/json" class="rm-data">'
                + json.dumps({"tests": tests}) + "</script>")


def test_the_map_s_own_excerpts_are_linked_and_the_ones_pointed_at_are_folded():
    doc = xref.cross_link(rm_page({"t1": {"parts": [
        {"label": "a.feature:15-15", "href": "vscode://file//r/a.feature:15:1", "from": 15,
         "html": ["  When I book a visit"]},
        {"label": "glue.ts:20-22", "href": "vscode://file//r/glue.ts:20:1", "from": 20,
         "html": ["When('I book a visit', async function () {",
                  "  await expect_the_row(this);", "});"]},
        {"label": "dsl.ts:40-41", "href": "vscode://file//r/dsl.ts:40:1", "from": 40,
         "html": ["export async function expect_the_row(page) {", "}"]},
    ]}}))
    data = json.loads(re.search(r'class="rm-data">(.*?)</script>', doc, re.S)[1]
                      .replace("<\\/", "</"))
    parts = data["tests"]["t1"]["parts"]
    assert 'class="xref"' in parts[0]["html"][0]          # the sentence reaches the glue
    assert 'class="xref"' in parts[1]["html"][1]          # the glue reaches the DSL
    # …and every quoted line still holds exactly one source line, which is what the
    # excerpt's own label promises and what `check_baked_excerpts` counts.
    assert [len(p["html"]) for p in parts] == [1, 3, 2]

    index = json.loads(re.search(r'id="xref-index">(.*?)</script>', doc, re.S)[1]
                       .replace("<\\/", "</"))
    shut = {href.rsplit("/", 1)[-1]: entry["shut"] for href, entry in index.items()}
    # The test's own first excerpt is what the reader opened the row for; the two behind
    # it are what the links are for.
    assert shut == {"a.feature:15:1": False, "glue.ts:20:1": True, "dsl.ts:40:1": True}
    assert index["vscode://file//r/glue.ts:20:1"]["face"].startswith("When('I book a visit'")


def test_one_window_quoted_twice_keeps_one_id():
    """Two tests can walk through the same helper. Two ids for one range would leave one of
    them naming an element the page never renders."""
    same = {"label": "dsl.ts:40-41", "href": "vscode://file//r/dsl.ts:40:1", "from": 40,
            "html": ["export async function expect_the_row(page) {", "}"]}
    doc = xref.cross_link(rm_page({
        "t1": {"parts": [{"label": "glue.ts:20-20", "href": "vscode://file//r/glue.ts:20:1",
                          "from": 20, "html": ["  await expect_the_row(this);"]},
                         dict(same)]},
        "t2": {"parts": [{"label": "glue.ts:30-30", "href": "vscode://file//r/glue2.ts:30:1",
                          "from": 30, "html": ["  await expect_the_row(this);"]},
                         dict(same)]}}))
    ids = set(re.findall(r'data-xref=\\"(\w+)\\"', doc))
    assert len(ids) == 1, ids


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
