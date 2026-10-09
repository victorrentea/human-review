#!/usr/bin/env python3
"""The page frame stays put: the sticky masthead neither drifts under a click nor loses
the top of the window to something a panel raised.

Two bugs Victor hit on 9 Oct 2026, both measured here in headless Chromium against the
real stylesheet and the real tabs.js around a masthead-shaped band and two long panels:

* Clicking a tab crept the page up 8px per click. `select()` read the masthead's live
  rect to find the panel top, and a pinned masthead reports top 0 wherever the page is,
  so the target was always "8px above here".
* A pressed (i) wears z-index 61 to stand on its explainer's folder tab; with the panel
  flat that number beat the masthead's 31 and, scrolled, the (i) floated over the tab
  strip. Every panel is now its own stacking context.

Run with:  python3 -m pytest test_frame_scroll.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hr_assets", HERE / "hrbuild" / "shared" / "assets.py")
assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assets)
TABS_JS = (HERE / "hrbuild" / "assets" / "tabs.js").read_text(encoding="utf-8")

_FILL = "<p>a line long enough to be a paragraph of a review</p>" * 300
PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><style>{assets.CSS}</style></head>
<body><div class="wrap"><div style="height:200px">above the masthead</div>
<header class="masthead"><div class="titlerow oneline"><h1>PR</h1></div>
<nav class="tabstrip" role="tablist">
<button class="tab" role="tab" aria-controls="review">Review</button>
<button class="tab" role="tab" aria-controls="tests">Tests</button></nav></header>
<section class="panel" id="review"><div class="adoptline" style="margin-top:900px">
<button class="hrx-i" type="button" aria-pressed="true"></button></div>{_FILL}</section>
<section class="panel" id="tests">{_FILL}</section></div>
<script>{TABS_JS}</script></body></html>"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    sync = pytest.importorskip("playwright.sync_api")
    page_file = tmp_path_factory.mktemp("frame") / "page.html"
    page_file.write_text(PAGE, encoding="utf-8")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        pg = browser.new_page(viewport={"width": 1440, "height": 800})
        pg.goto(page_file.as_uri())
        pg.wait_for_timeout(200)
        yield pg
        browser.close()


def _scroll(page, y):
    page.evaluate("y => window.scrollTo(0, y)", y)
    page.wait_for_timeout(60)
    return page.evaluate("() => window.pageYOffset")


def _click(page, name):
    page.locator("button.tab", has_text=name).click()
    page.wait_for_timeout(60)
    return page.evaluate("() => window.pageYOffset")


def test_clicking_the_open_tab_leaves_the_page_where_it_is(page):
    _click(page, "Review")
    at = _scroll(page, 1500)
    assert [_click(page, "Review") for _ in range(4)] == [at] * 4


def test_switching_tabs_lands_at_the_panel_top_every_time(page):
    _scroll(page, 0)
    natural = page.evaluate("() => document.querySelector('.masthead').getBoundingClientRect().top")
    want = max(0, natural - 8)
    landed = []
    for name in ["Tests", "Review", "Tests", "Review"]:
        _scroll(page, 1500)
        landed.append(_click(page, name))
    assert landed == [want] * 4, f"want {want} on every switch, got {landed}"


def test_a_pressed_info_button_slides_under_the_sticky_header(page):
    _click(page, "Review")
    _scroll(page, 0)
    page.evaluate("""() => {
      const head = document.querySelector('.masthead').getBoundingClientRect().height;  // pinned, it spans 0..height
      const r = document.querySelector('button.hrx-i').getBoundingClientRect();
      window.scrollTo(0, r.top + r.height / 2 + window.pageYOffset - (head - 8));
    }""")
    page.wait_for_timeout(60)
    hit = page.evaluate("""() => {
      const r = document.querySelector('button.hrx-i').getBoundingClientRect();
      const head = document.querySelector('.masthead').getBoundingClientRect();
      if (r.top + r.height / 2 >= head.bottom) return 'not under the header: ' + r.top;
      const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return el && el.closest('.masthead') ? 'masthead' : (el ? el.className : null);
    }""")
    assert hit == "masthead", f"the (i) paints over the header: the point hits {hit!r}"
