"""The UX tab between captures: a fragment drawn by older code is redrawn, and a gap the
branch added turns the tab's pill amber.

The visit-vet report (7 Oct 2026) showed its UX tab as it looked two days earlier — a bare
"UX" title and the prompt at the bottom of the panel — after every rebuild. The page pastes
`assets/ds-audit.html` whole, and the step that writes it needs both sides' stacks up, so no
refresh ever re-ran it: the 5 Oct fragment outlived every change to how ds-audit renders.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_ds_audit import _result_from_capture, ds  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


refresh = _load("refresh-report")

OLD_FRAGMENT = ('<div class="dsa-run"><p class="dsa-hdr">5 of 19 screens changed · '
                '<span class="dsa-gap">⚠ +1 gap — native control</span></p></div>')


def _review(tmp_path, fragment=OLD_FRAGMENT, with_json=True):
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    (review / "assets" / "ds-audit.html").write_text(fragment, encoding="utf-8")
    if with_json:
        (review / "assets" / "ds-audit.json").write_text(
            json.dumps(_result_from_capture()), encoding="utf-8")
    return review


def test_a_fragment_says_which_ds_audit_drew_it():
    frag = ds.render(_result_from_capture(), "")
    assert ds.rendered_by(frag) == ds.RENDER_STAMP
    assert ds.rendered_by(OLD_FRAGMENT) is None, "a fragment older than the stamp has none"


def test_a_fragment_drawn_by_older_code_is_redrawn_from_its_json(tmp_path):
    review = _review(tmp_path)
    assert ds.rerender_if_stale(review) is True
    frag = (review / "assets" / "ds-audit.html").read_text(encoding="utf-8")
    assert ds.rendered_by(frag) == ds.RENDER_STAMP
    assert '<h2 class="tabtitle">UX design system</h2>' in frag, \
        "the current title, which the build's prompt placement anchors on"
    assert (review / "assets" / "ds-audit.css").read_text(encoding="utf-8") == ds.CSS + "\n"


def test_a_fragment_this_script_drew_is_left_alone(tmp_path):
    review = _review(tmp_path)
    ds.rerender_if_stale(review)
    frag = review / "assets" / "ds-audit.html"
    frag.write_text(frag.read_text(encoding="utf-8") + "<!-- hand -->", encoding="utf-8")
    assert ds.rerender_if_stale(review) is False
    assert frag.read_text(encoding="utf-8").endswith("<!-- hand -->")


def test_no_json_means_nothing_to_redraw_from(tmp_path):
    review = _review(tmp_path, with_json=False)
    assert ds.rerender_if_stale(review) is False
    assert (review / "assets" / "ds-audit.html").read_text(encoding="utf-8") == OLD_FRAGMENT


def test_a_refresh_redraws_the_ux_fragment_before_the_build(tmp_path):
    review = _review(tmp_path)
    cmds = refresh.plan(review, "none", None, False, False, None)
    names = [Path(c[1]).name for c in cmds]
    assert names == ["ds-audit.py", "build-review-html.py"]
    assert cmds[0][2:] == ["--rerender-if-stale", str(review)]


def test_a_review_without_an_audit_gets_no_redraw(tmp_path):
    review = _review(tmp_path, with_json=False)
    cmds = refresh.plan(review, "none", None, False, False, None)
    assert [Path(c[1]).name for c in cmds] == ["build-review-html.py"]


# ── the pill ──────────────────────────────────────────────────────────────────────

def _build(tmp_path, fragment) -> str:
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "ds-audit.html").write_text(fragment, encoding="utf-8")
    content = {
        "title": "t", "summary": "<p>x</p>",
        "sections": [{"id": "one", "title": "One", "body": "<p>a</p>"},
                     {"id": "ds-audit", "title": "", "includeHtml": "assets/ds-audit.html"}],
        "tabs": [{"id": "one", "label": "One", "blocks": [{"type": "section", "id": "one"}]},
                 {"id": "dsaudit", "label": "UX",
                  "blocks": [{"type": "section", "id": "ds-audit"}]}]}
    src = tmp_path / "content.json"
    src.write_text(json.dumps(content), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home)}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    out = tmp_path / "review.html"
    proc = subprocess.run([sys.executable, str(HERE / "build-review-html.py"), str(src),
                           "--out", str(out)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8")


def _ux_button(page: str) -> str:
    start = page.index('id="tabbtn-dsaudit"')
    return page[page.rindex("<button", 0, start):page.index(">", start) + 1]


def test_a_gap_the_branch_added_turns_the_ux_pill_amber(tmp_path):
    """The Codeowners colour for "open me": `⚠ +1 gap` in the audit's header line."""
    btn = _ux_button(_build(tmp_path, ds.render(_result_from_capture(), "")))
    assert 'class="tab warn"' in btn
    assert 'aria-label="UX — 1 new design-system gap"' in btn


def test_a_gap_the_base_already_had_leaves_the_pill_alone(tmp_path):
    frag = ('<div class="dsa-run"><h2 class="tabtitle">UX design system</h2>'
            '<p class="dsa-hdr">1 of 3 screens changed · <span class="dsa-pre">2 gaps already '
            'on the base</span></p><p><span class="dsa-gap">+1 gap</span> on one screen</p>'
            '</div>')
    btn = _ux_button(_build(tmp_path, frag))
    assert 'class="tab"' in btn and "warn" not in btn


def test_no_screen_changed_fades_the_ux_pill(tmp_path):
    """Victor, 10 Nov 2026: no change on any screen, the UX tab goes grey like Data."""
    frag = ('<div class="dsa-run"><h2 class="tabtitle">UX design system</h2>'
            '<p class="dsa-hdr"><span class="dsa-count" role="button">0 of 19 screens '
            'changed</span></p></div>')
    assert 'class="tab quiet"' in _ux_button(_build(tmp_path, frag))


def test_a_changed_screen_the_catalogue_missed_keeps_the_ux_pill_lit(tmp_path):
    frag = ('<div class="dsa-run"><h2 class="tabtitle">UX design system</h2>'
            '<p class="dsa-unlisted">⚠ <b>Not audited:</b> <code>/x</code></p>'
            '<p class="dsa-hdr"><span class="dsa-count" role="button">0 of 19 screens '
            'changed</span></p></div>')
    assert "quiet" not in _ux_button(_build(tmp_path, frag))


def test_a_changed_screen_keeps_the_ux_pill_lit(tmp_path):
    frag = ('<div class="dsa-run"><h2 class="tabtitle">UX design system</h2>'
            '<p class="dsa-hdr"><span class="dsa-count" role="button">10 of 19 screens '
            'changed</span></p></div>')
    assert "quiet" not in _ux_button(_build(tmp_path, frag))


def test_the_ux_stylesheet_comes_with_the_fragment_without_an_extracss_line(tmp_path):
    """demo2 (20 Oct 2026): the content file listed no `assets/ds-audit.css`, so the tab
    shipped unstyled — legend chips run together, the screenshot out of its frame, and
    "frame the changes" toggling frames nobody had drawn."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "ds-audit.css").write_text(ds.CSS + "\n", encoding="utf-8")
    page = _build(tmp_path, ds.render(_result_from_capture(), ""))
    assert page.count(".dsa-legend {") == 1


def test_a_span_caret_goes_link_blue_with_its_row_like_the_pseudo_ones():
    """The UX tab's screen rows moved from a `summary::before` caret to `span.disclose`;
    the hover colour (and the gap before the verdict icon) stayed with the old one, so the
    from-scratch rebuild of the PR #51 page lost what the live polish showed."""
    css = (HERE / "hrbuild" / "assets" / "css" / "core.css").read_text(encoding="utf-8")
    assert "details > summary:hover .disclose { color:var(--link); }" in css
    assert "details.dsa-screen > summary .disclose { margin-right:.3rem; }" in css


def test_the_caret_selector_lists_never_name_a_bare_details():
    """`details.costdetail, details.snipmore, details.fmtonly > summary::before` reads as
    one entry and is three: the first two styled the whole <details> (display, width,
    colour). Every entry has to reach the summary."""
    css = (HERE / "hrbuild" / "assets" / "css" / "core.css").read_text(encoding="utf-8")
    import re
    for sel in re.findall(r"([^{}]+)\{", css):
        for part in sel.split(","):
            part = part.strip()
            if part.startswith("details.") and "summary" not in part and "[open]" not in part \
                    and " " not in part and part.split(".")[1] in ("costdetail", "snipmore"):
                raise AssertionError(f"bare details selector in a caret list: {part!r}")
