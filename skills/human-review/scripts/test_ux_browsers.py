"""Chrome · Edge on the UX tab: every changed screen is shot in both, and a switch by the
tab's title swaps every picture to the browser picked (Victor, 3 Nov 2026: "să văd cum
arată paginile în ambele browsere, cu diferențe vizuale înainte și după")."""
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
    sc["browsers"] = {"edge": {"new": edge_diff[0], "old": edge_diff[1]}}
    result["browsers"] = [{"id": "chrome", "label": "Chrome", "version": "156.0.1"},
                          {"id": "edge", "label": "Edge", "version": "156.0.2"}]
    return result


def test_a_run_shot_in_one_browser_has_no_switch():
    frag = ds.render(_result_from_capture(), "assets/")
    assert '<h2 class="tabtitle">UX design system</h2>' in frag
    assert "dsa-browsers" not in frag


def test_two_browsers_put_the_switch_in_the_title_primary_pressed():
    frag = ds.render(_two_browser_result(), "assets/")
    title = frag[frag.index('<h2 class="tabtitle">'):frag.index("</h2>")]
    assert title.startswith('<h2 class="tabtitle">UX design system<span class="dsa-browsers"')
    assert 'data-br="chrome" data-primary="" aria-pressed="true"' in title
    assert 'data-br="edge" aria-pressed="false"' in title
    assert ">Chrome</button>" in title and ">Edge</button>" in title


def test_the_switch_tooltip_says_whether_the_browsers_agree():
    same = ds.browser_switch(_two_browser_result())
    assert "Pixel-identical to Chrome on all 1 changed screen" in same
    off = ds.browser_switch(_two_browser_result((0.0123, 0.0)))
    assert "Differs from Chrome on 1 of 1 changed screens" in off and "1.23%" in off


def test_a_screen_shot_in_the_other_browser_says_so_for_the_swap():
    frag = ds.render(_two_browser_result(), "assets/")
    assert 'class="dsa-screen" id="dsa-' in frag and 'data-brs="edge"' in frag
    assert "'.' + br + '.png'" in frag, "the swap renames <name>.png to <name>.<br>.png"


def test_the_other_browsers_pictures_sit_beside_the_primarys():
    png = Path("assets/ds-audit-book-a-visit-new.png")
    assert ds.browser_png(png, None) == png
    assert ds.browser_png(png, "edge") == Path("assets/ds-audit-book-a-visit-new.edge.png")


def test_a_browser_not_installed_is_skipped_and_chromium_stands_in(monkeypatch, tmp_path):
    monkeypatch.setitem(ds.BROWSERS, "edge", ("Edge", "msedge", (str(tmp_path / "none"),)))
    exe = tmp_path / "chrome"
    exe.write_text("")
    monkeypatch.setitem(ds.BROWSERS, "chrome", ("Chrome", "chrome", (str(exe),)))
    got = ds.resolve_browsers(["chrome", "edge"])
    assert [b["id"] for b in got] == ["chrome"]
    assert got[0]["launch"] == {"executable_path": str(exe)}
    monkeypatch.setitem(ds.BROWSERS, "chrome", ("Chrome", "chrome", (str(tmp_path / "x"),)))
    assert ds.resolve_browsers(["chrome", "edge"])[0]["id"] == "chromium"


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
