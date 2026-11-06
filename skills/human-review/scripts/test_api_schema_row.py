#!/usr/bin/env python3
"""The API tab's schema block, in a browser: one row, and Tree | Diagram under it.

Swagger UI comes off a CDN, so the page here is the real template, stylesheet and script,
over a stand-in `SwaggerUIBundle` that draws the DOM Swagger UI 5.33 draws for a response and
a request. Everything the block does is that stylesheet and that script reading this DOM.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ovd_row", HERE / "openapi-visual-diff.py")
ovd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ovd)


def select(*values, cls="content-type") -> str:
    return (f'<select class="{cls}">'
            + "".join(f'<option value="{v}">{v}</option>' for v in values) + "</select>")


def tabs(example: bool) -> str:
    """Swagger UI's DOM order is Example first; the stylesheet puts Schema first."""
    a, b = (" active", "") if example else ("", " active")
    panel = ('<div role="tabpanel" data-name="examplePanel"><pre>{}</pre></div>' if example else
             '<div role="tabpanel" data-name="modelPanel" class="model-container">'
             '<div class="model-box"><article class="json-schema-2020-12">tree</article></div></div>')
    return (f'<div class="model-example"><ul class="tab" role="tablist">'
            f'<li class="tabitem{a}"><button class="tablinks" data-name="example">Example Value</button></li>'
            f'<li class="tabitem{b}"><button class="tablinks" data-name="model">Schema</button></li>'
            f'</ul>{panel}</div>')


def response(code: str, medias: tuple, samples: tuple = (), example: bool = False) -> str:
    examples = ""
    if samples:
        examples = ('<div class="response-control-examples"><small class="response-control-'
                    'examples__title">Examples</small><div class="examples-select">'
                    + select(*samples, cls="examples-select-element") + "</div></div>")
    return (f'<tr class="response" data-code="{code}"><td class="response-col_status">{code}</td>'
            '<td class="response-col_description"><div class="response-col_description__inner">'
            '<div class="renderedMarkdown"><p>OK</p></div></div>'
            '<div class="response-controls"><div class="response-control-media-type">'
            '<small class="response-control-media-type__title">Media type</small>'
            f'<div class="content-type-wrapper">{select(*medias)}</div></div>{examples}</div>'
            f'{tabs(example)}</td><td class="response-col_links"></td></tr>')


REQUEST = ('<div class="opblock-section opblock-section-request-body">'
           '<div class="opblock-section-header"><h4 class="opblock-title">Request body</h4>'
           '<label><div class="content-type-wrapper body-param-content-type">'
           + select("application/json") + '</div></label></div>'
           f'<div class="opblock-description-wrapper"><div>{tabs(False)}</div></div></div>')


def operation(method: str, path: str, body: str, rows: str) -> str:
    return (f'<div class="opblock is-open"><div class="opblock-summary">'
            f'<span class="opblock-summary-method">{method}</span>'
            f'<span class="opblock-summary-path" data-path="{path}">{path}</span></div>'
            f'<div class="opblock-body">{body}<div class="responses-inner">'
            f'<table class="responses-table"><tbody>{rows}</tbody></table></div></div></div>')


FAKE = ('<div class="swagger-ui"><div class="opblock-tag-section">'
        '<h3 class="opblock-tag" data-tag="t" data-is-open="true"></h3>'
        + operation("POST", "/a", REQUEST,
                    response("200", ("application/json",)) +
                    response("201", ("application/json", "application/xml")) +
                    response("202", ("application/json",), ("sample",), example=True) +
                    response("203", ("application/json",), ("one", "two"), example=True) +
                    response("204", ("application/json",), ("sample",)))
        + '<div class="opblock-body dv-noparams"><div class="opblock-section">'
        '<div class="opblock-section-header"><h4>Parameters</h4></div>'
        '<div class="parameters-container"><div class="opblock-description-wrapper">'
        '<p>No parameters</p></div></div>' + REQUEST + '</div></div>'
        + "</div></div>")

ENTRIES = {"POST /a": {"state": "untouched", "changes": [], "ghosts": [], "deepSkipped": 0,
                       "diagrams": {"request application/json": "d1",
                                    "200 application/json": "d2",
                                    "201 application/json": "d2"}}}
SVG = '<svg width="10" height="10"><text>{}</text></svg>'


@pytest.fixture()
def page(tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    doc = tmp_path / "api.html"
    doc.write_text(ovd.render({"paths": {}}, ENTRIES, [], {"t": "touched"}, "a", "b",
                              {"d1": SVG.format("request"), "d2": SVG.format("response")}),
                   encoding="utf-8")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.route("**/*swagger-ui*", lambda r: r.abort())
        pg.add_init_script(
            "window.SwaggerUIBundle = o => { o.domNode.innerHTML = " + repr(FAKE)
            + "; o.onComplete(); };")
        pg.goto(doc.as_uri())
        pg.wait_for_selector(".dv-views")
        yield pg
        browser.close()
        assert not errors, errors


def box(pg, selector: str) -> dict:
    return pg.evaluate("s => { const r = document.querySelector(s).getBoundingClientRect();"
                       " return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,"
                       " mid: (r.top + r.bottom) / 2, h: r.height}; }", selector)


def row(code: str) -> str:
    return f'tr.response[data-code="{code}"]'


def test_the_media_type_ends_the_radio_row(page):
    radios = box(page, f"{row('200')} ul.tab")
    media = box(page, f"{row('200')} .response-control-media-type")
    panel = box(page, f"{row('200')} [role=tabpanel]")
    assert media["top"] < radios["bottom"] and radios["top"] < media["bottom"], "not one row"
    assert media["left"] > radios["right"]
    assert abs(media["right"] - panel["right"]) <= 1, "not pinned to the right end"


def test_the_request_reads_the_same(page):
    body = ".opblock-section-request-body"
    radios, media = box(page, f"{body} ul.tab"), box(page, f"{body} label")
    title, panel = box(page, f"{body} h4"), box(page, f"{body} [role=tabpanel]")
    assert media["top"] < radios["bottom"] and radios["top"] < media["bottom"]
    assert title["bottom"] <= radios["top"] + 1 and panel["top"] >= radios["bottom"] - 1
    assert abs(media["right"] - panel["right"]) <= 1
    label = page.evaluate("s => getComputedStyle(document.querySelector(s), '::before').content",
                          f"{body} label")
    assert label == '"Media type"'


def test_a_request_without_parameters_still_shows_its_body(page):
    """Swagger UI nests the body in the section that says "No parameters"."""
    assert page.locator(".dv-noparams .opblock-section-request-body ul.tab").is_visible()
    assert page.locator(".dv-noparams .parameters-container").is_hidden()


def test_the_sample_follows_example_value_and_only_then(page):
    assert page.locator(f"{row('204')} .response-control-examples").is_hidden()
    examples = box(page, f"{row('202')} .response-control-examples")
    radios = box(page, f"{row('202')} ul.tab")
    media = box(page, f"{row('202')} .response-control-media-type")
    assert examples["top"] < radios["bottom"] and radios["top"] < examples["bottom"]
    assert radios["right"] <= examples["left"] < media["left"]
    assert examples["left"] - radios["right"] < 40, "not right after the radio"


LOOKS = """s => { const c = getComputedStyle(document.querySelector(s));
  return {appearance: c.appearance, border: c.borderTopWidth, events: c.pointerEvents}; }"""


@pytest.mark.parametrize("selector", [
    f"{row('200')} select.content-type", ".body-param-content-type select",
    f"{row('202')} select.examples-select-element"])
def test_one_value_is_plain_text(page, selector):
    assert page.evaluate(LOOKS, selector) == {
        "appearance": "none", "border": "0px", "events": "none"}
    assert page.evaluate("s => document.querySelector(s).tabIndex", selector) == -1


@pytest.mark.parametrize("selector", [
    f"{row('201')} select.content-type", f"{row('203')} select.examples-select-element"])
def test_two_values_stay_a_dropdown(page, selector):
    look = page.evaluate(LOOKS, selector)
    assert look["appearance"] != "none" and look["events"] == "auto"
    assert page.evaluate("s => document.querySelector(s).tabIndex", selector) == 0


def test_the_switch_says_tree_and_diagram_and_opens_on_tree(page):
    words = page.locator(f"{row('200')} .dv-views button").all_inner_texts()
    assert words == ["Tree", "Diagram"]
    assert page.locator(f"{row('200')} article").is_visible()
    assert page.locator(f"{row('200')} .dv-dgm").is_hidden()
    assert page.locator(f"{row('200')} .dv-dgm svg").count() == 0, "drawn before it was asked for"


def test_it_sits_under_the_row_and_only_under_schema(page):
    bar = box(page, f"{row('200')} .dv-views")
    assert bar["top"] >= box(page, f"{row('200')} ul.tab")["bottom"] - 1
    assert page.locator(f"{row('202')} .dv-views").count() == 0   # on Example Value
    assert page.locator(f"{row('204')} .dv-views").count() == 0   # no diagram for that body


def test_diagram_replaces_the_tree_in_every_open_block(page):
    page.click(f"{row('200')} .dv-views button[data-view=diagram]")
    for where, word in ((row("200"), "response"), (row("201"), "response"),
                        (".opblock-body > .opblock-section-request-body", "request")):
        assert page.locator(f"{where} article").is_hidden()
        assert page.locator(f"{where} .dv-dgm svg").text_content() == word
    assert page.get_attribute(f"{row('201')} .dv-views button[data-view=diagram]",
                              "aria-pressed") == "true"
    page.click(f"{row('201')} .dv-views button[data-view=tree]")
    assert page.locator(f"{row('200')} article").is_visible()
    assert page.locator(f"{row('200')} .dv-dgm").is_hidden()


def test_a_block_redrawn_by_swagger_ui_keeps_the_view(page):
    page.click(f"{row('200')} .dv-views button[data-view=diagram]")
    page.evaluate("s => { const t = document.querySelector(s); const n = t.cloneNode(true);"
                  " n.querySelector('.dv-views').remove(); n.querySelector('.dv-dgm').remove();"
                  " delete n.dataset.dvView; t.replaceWith(n); }",
                  f"{row('200')} [role=tabpanel]")
    page.wait_for_selector(f"{row('200')} .dv-dgm svg")
    assert page.locator(f"{row('200')} article").is_hidden()


def test_the_page_does_not_scroll_sideways(page):
    assert page.evaluate("document.documentElement.scrollWidth <= "
                         "document.documentElement.clientWidth")
