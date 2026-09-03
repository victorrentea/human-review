#!/usr/bin/env python3
"""PlantUML draws every diagram in one fixed, hardcoded palette — light boxes, near-black
strokes, black text, no `!theme` or colour skinparam. Inlined as-is, that palette is
invisible to `prefers-color-scheme`: dark mode flips the page around it and leaves a
bright white slab where a diagram was.

`_theme_diagram_colors` (build-review-html.py) rewrites each of PlantUML's known literal
colours to the matching `--dgm-*` CSS variable at inline time, so the diagram repaints
along with the rest of the page. It is deliberately narrow — a fixed palette recognised
by exact value, not a blanket filter — because the diff renderers (`puml_diff.py`,
`seq_puml_diff.py`) paint added/removed elements a deliberate red, and a rule that is not
precise about which literal it is touching risks fighting that signal instead of just
carrying it into dark mode.

Run with:  python3 -m pytest test_diagram_dark_mode.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def test_every_known_plantuml_fill_becomes_its_variable():
    for hexval, var in build.DIAGRAM_COLOR_VARS.items():
        svg = f'<rect fill="{hexval}" width="1" height="1"/>'
        out = build._theme_diagram_colors(svg)
        assert out == f'<rect fill="var({var})" width="1" height="1"/>', (hexval, out)


def test_lowercase_hex_is_matched_too():
    """PlantUML's own output is consistently uppercase, but the match should not depend
    on it — a future PlantUML version is not a contract this pipeline was given."""
    out = build._theme_diagram_colors('<rect fill="#f1f1f1"/>')
    assert out == '<rect fill="var(--dgm-box)"/>'


def test_stroke_and_background_are_rewritten_inside_style_but_not_other_props():
    svg = ('<rect style="stroke:#181818;stroke-width:0.5;" fill="#F1F1F1"/>'
           '<svg style="width:10px;height:10px;background:#FFFFFF;">')
    out = build._theme_diagram_colors(svg)
    assert 'stroke:var(--dgm-line)' in out
    assert 'background:var(--dgm-bg)' in out
    assert 'stroke-width:0.5' in out          # untouched — not a colour
    assert 'width:10px;height:10px' in out    # untouched — not a colour


def test_the_two_diff_reds_get_their_own_distinct_variables():
    """puml_diff.py colours a changed class/ER element `<color:red>` (-> #FF0000);
    seq_puml_diff.py colours a changed sequence arrow literally `#D40000`. Different
    generators, different reds — collapsing them to one variable would mean picking one
    literal's contrast check for both, so each keeps its own."""
    out_class = build._theme_diagram_colors('<text fill="#FF0000">Diff</text>')
    out_seq = build._theme_diagram_colors('<path style="stroke:#D40000;"/>')
    assert 'var(--dgm-diff)"' in out_class
    assert 'var(--dgm-diff-seq)' in out_seq
    assert build.DIAGRAM_COLOR_VARS["#FF0000"] != build.DIAGRAM_COLOR_VARS["#D40000"]


def test_a_colour_plantuml_is_not_known_to_emit_is_left_alone():
    """Degrade safely: an unrecognised literal is not a colour this palette can name,
    so it stays exactly as rendered rather than being guessed at."""
    svg = '<rect fill="#123456"/><path style="stroke:#abcdef;"/>'
    assert build._theme_diagram_colors(svg) == svg


def test_non_colour_fill_values_are_untouched():
    """`fill="none"` and `fill="url(#grad)"` are not hex colours; the regex must not
    misfire on them."""
    svg = '<rect fill="none"/><path fill="url(#grad1)"/>'
    assert build._theme_diagram_colors(svg) == svg


def test_both_diff_reds_read_at_at_least_aa_contrast_on_the_dark_diagram_canvas():
    """The dark-mode values live only as literals inside the CSS string in
    build-review-html.py, so this pins the actual numbers rather than re-deriving them
    from the source — a change to either literal should have to walk through this
    assertion, not silently drop below the WCAG AA text minimum (4.5:1)."""

    def luminance(hexval):
        r, g, b = (int(hexval[i:i + 2], 16) / 255 for i in (1, 3, 5))

        def lin(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = lin(r), lin(g), lin(b)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a, b):
        la, lb = luminance(a), luminance(b)
        lighter, darker = max(la, lb), min(la, lb)
        return (lighter + 0.05) / (darker + 0.05)

    css = build.CSS
    dark_block = css.split("@media (prefers-color-scheme: dark)", 1)[1]
    dgm_bg = re.search(r"--dgm-bg:(#[0-9a-fA-F]{6})", dark_block)[1]
    dgm_diff = re.search(r"--dgm-diff:(#[0-9a-fA-F]{6})", dark_block)[1]
    dgm_diff_seq = re.search(r"--dgm-diff-seq:(#[0-9a-fA-F]{6})", dark_block)[1]

    assert contrast(dgm_diff, dgm_bg) >= 4.5, "class/ER diff red is too dim against the dark diagram canvas"
    assert contrast(dgm_diff_seq, dgm_bg) >= 4.5, "sequence diff red is too dim against the dark diagram canvas"


if __name__ == "__main__":
    import sys
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
