"""Browsers on the UX tab: every changed screen is shot in Chrome and in Safari (WebKit),
and a switch by the tab's title — and one on each screen's bar — swaps the pictures to the
browser picked (Victor, 3 Nov 2026: "să văd cum arată paginile în ambele browsere, cu
diferențe vizuale înainte și după"). Chrome's button carries Edge's logo too: shot side by
side, the two drew every screen byte for byte the same."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_ds_audit import _result_from_capture, ds  # noqa: E402


def _two_browser_result(edge_diff=(0.0, 0.0)):
    result = _result_from_capture()
    sc = result["screens"][0]
    assert ds.screen_touched(sc), "the fixture's screen is a changed one"
    sc["browsers"] = {"safari": {"new": edge_diff[0], "old": edge_diff[1]}}
    result["browsers"] = [{"id": "chrome", "label": "Chrome", "version": "156.0.1"},
                          {"id": "safari", "label": "Safari", "version": "26.0"}]
    return result


def test_a_run_shot_in_one_browser_has_no_switch():
    frag = ds.render(_result_from_capture(), "assets/")
    assert '<h2 class="tabtitle">UX design system</h2>' in frag
    assert "dsa-browsers" not in frag


def test_two_browsers_put_the_logos_in_the_title_primary_pressed():
    frag = ds.render(_two_browser_result(), "assets/")
    title = frag[frag.index('<h2 class="tabtitle">'):frag.index("</h2>")]
    assert title.startswith('<h2 class="tabtitle">UX design system'
                            '<span class="dsa-browsers dsa-global"')
    assert ('data-br="chrome" data-primary="" aria-pressed="true" '
            'aria-label="Chrome and Edge"') in title
    assert 'data-br="safari" aria-pressed="false" aria-label="Safari"' in title
    assert ">Chrome<" not in title and ">Safari<" not in title, "logos, no text"


def test_chromes_button_carries_edges_logo_too_and_says_why():
    title = ds.browser_switch(_two_browser_result())
    chrome = title[:title.index('data-br="safari"')]
    assert ('<i class="dsa-logo" data-logo="chrome" aria-hidden="true"></i>'
            '<i class="dsa-logo" data-logo="edge" aria-hidden="true"></i>') in chrome
    assert "Edge draws them the same" in chrome
    assert chrome.count("dsa-logo") == 2 and title.count('data-logo="safari"') == 1


def test_each_screen_shot_in_both_gets_its_own_logos_on_its_bar():
    frag = ds.render(_two_browser_result((0.0, 0.004)), "assets/")
    bar = frag[frag.index('<div class="dgmbar">'):]
    bar = bar[:bar.index("</div>")]
    assert '<span class="dsa-browsers dsa-local"' in bar
    assert "This screen as Safari 26 drew it, base and branch: branch pixel-identical to " \
           "Chrome, base 0.40% of its pixels differ from Chrome." in bar
    assert "Marks and frames are hidden: they were measured on Chrome&#x27;s layout." in bar


def test_another_engines_picture_hides_the_marks_measured_on_the_primary():
    frag = ds.render(_two_browser_result(), "assets/")
    assert "d.classList.toggle('dsa-alt', has && br !== primary)" in frag
    assert "details.dsa-screen.dsa-alt .dsa-mark" in ds.CSS


def test_the_switch_tooltip_says_whether_the_browsers_agree():
    same = ds.browser_switch(_two_browser_result())
    assert "Pixel-identical to Chrome on all 1 changed screen" in same
    off = ds.browser_switch(_two_browser_result((0.0123, 0.0)))
    assert "Marks and frames are hidden" in off
    assert "Differs from Chrome on 1 of 1 changed screens" in off and "1.23%" in off


def test_a_screen_shot_in_the_other_browser_says_so_for_the_swap():
    frag = ds.render(_two_browser_result(), "assets/")
    assert 'class="dsa-screen" id="dsa-' in frag and 'data-brs="safari"' in frag
    assert "'.' + br + '.png'" in frag, "the swap renames <name>.png to <name>.<br>.png"


def test_the_other_browsers_pictures_sit_beside_the_primarys():
    png = Path("assets/ds-audit-book-a-visit-new.png")
    assert ds.browser_png(png, None) == png
    assert ds.browser_png(png, "edge") == Path("assets/ds-audit-book-a-visit-new.edge.png")


def test_a_browser_not_installed_is_skipped_and_chromium_stands_in(monkeypatch, tmp_path):
    exe = tmp_path / "chrome"
    exe.write_text("")
    monkeypatch.setitem(ds.BROWSERS, "chrome", dict(ds.BROWSERS["chrome"], paths=(str(exe),)))
    monkeypatch.setitem(ds.BROWSERS, "edge", dict(ds.BROWSERS["edge"],
                                                  paths=(str(tmp_path / "none"),)))
    monkeypatch.setattr(ds, "_bundled", lambda engine: False)
    got = ds.resolve_browsers(["chrome", "edge", "safari"])
    assert [b["id"] for b in got] == ["chrome"]
    assert got[0]["launch"] == {"engine": "chromium", "executable_path": str(exe)}
    monkeypatch.setitem(ds.BROWSERS, "chrome", dict(ds.BROWSERS["chrome"],
                                                    paths=(str(tmp_path / "x"),)))
    assert ds.resolve_browsers(["chrome", "edge"])[0]["id"] == "chromium"


def test_safari_is_playwrights_webkit_when_it_is_installed(monkeypatch):
    monkeypatch.setattr(ds, "_bundled", lambda engine: engine == "webkit")
    got = [b for b in ds.resolve_browsers(["safari"]) if b["id"] == "safari"]
    assert got and got[0]["launch"] == {"engine": "webkit"}


def test_browser_diff_counts_the_pixels_two_browsers_drew_differently(tmp_path):
    from PIL import Image
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (10, 10), "white").save(a)
    im = Image.new("RGB", (10, 10), "white")
    for x in range(3, 7):          # a 4x4 block: the erosion keeps all but its 4 corners
        for y in range(3, 7):
            im.putpixel((x, y), (0, 0, 0))
    im.save(b)
    assert ds.browser_diff(a, a) == 0.0
    assert ds.browser_diff(a, b) == 0.12
    assert ds.browser_diff(a, tmp_path / "missing.png") is None


def test_a_capture_that_times_out_is_taken_once_more(monkeypatch, tmp_path):
    class TimeoutError(Exception):
        pass
    calls = []

    def flaky(url, png, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("Page.goto: Timeout 60000ms exceeded.")
        return {"ok": True}
    monkeypatch.setattr(ds, "capture", flaky)
    assert ds.capture_retrying("http://x/a", tmp_path / "a.png") == {"ok": True}
    assert calls == ["http://x/a", "http://x/a"]


def test_a_capture_that_fails_otherwise_is_not_retried(monkeypatch, tmp_path):
    import pytest
    calls = []

    def broken(url, png, **kw):
        calls.append(url)
        raise ValueError("no such page")
    monkeypatch.setattr(ds, "capture", broken)
    with pytest.raises(ValueError):
        ds.capture_retrying("http://x/a", tmp_path / "a.png")
    assert len(calls) == 1
