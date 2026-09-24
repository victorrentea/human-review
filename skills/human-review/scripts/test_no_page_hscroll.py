#!/usr/bin/env python3
"""The window never scrolls sideways.

The masthead and the tab strip are full-bleed bands, `width:100vw`, and `vw` counts the
vertical scrollbar. Wherever scrollbars take room — a mouse plugged in, "always show" in
System Settings — both bands ran 15px under it, and every tab long enough to scroll grew
a horizontal scrollbar along the bottom of the window. Headless Chromium hides scrollbars
by default, which is why no measurement caught it: this test launches it with them shown
and styles them classic (`::-webkit-scrollbar`), 15px, as a mouse-driven Mac draws them.

The page here is the real stylesheet around a real masthead-shaped band and enough text
to scroll, so it needs no build and no server.

Run with:  python3 -m pytest test_no_page_hscroll.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("hr_assets", HERE / "hrbuild" / "shared" / "assets.py")
assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assets)

PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><style>{assets.CSS}</style>
<style>::-webkit-scrollbar{{width:15px;height:15px}}</style></head>
<body><div class="wrap"><header class="masthead"><div class="titlerow oneline"><h1>PR</h1></div>
<nav class="tabstrip"><button class="tab">Review</button></nav></header>
{"<p>a line long enough to be a paragraph of a review</p>" * 400}</div></body></html>"""


@pytest.mark.parametrize("width", [1280, 1728])
def test_the_window_has_no_horizontal_scrollbar(width, tmp_path):
    sync = pytest.importorskip("playwright.sync_api")
    page_file = tmp_path / "page.html"
    page_file.write_text(PAGE, encoding="utf-8")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        try:
            page = browser.new_page(viewport={"width": width, "height": 800})
            page.goto(page_file.as_uri())
            # What the reader sees is the scrollbar, not `scrollWidth`: a clipped viewport
            # still reports the bands' 15px in it. A horizontal bar takes its height out of
            # the viewport, and a sideways wheel is what would move the page if it could.
            page.mouse.move(width / 2, 400)
            page.mouse.wheel(200, 0)
            page.wait_for_timeout(150)
            ch, ih, cw, iw, sx = page.evaluate(
                "() => [document.documentElement.clientHeight, innerHeight, "
                "document.documentElement.clientWidth, innerWidth, scrollX]")
        finally:
            browser.close()
    if cw == iw:
        pytest.skip("this Chromium draws overlay scrollbars; nothing to measure")
    assert ch == ih, f"a horizontal scrollbar takes {ih - ch}px of the window at {width}px"
    assert sx == 0, f"a sideways wheel moved the page {sx}px at {width}px"
