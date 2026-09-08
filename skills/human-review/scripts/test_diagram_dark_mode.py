#!/usr/bin/env python3
"""PlantUML draws every diagram in one fixed, hardcoded palette — light boxes, near-black
strokes, black text, no `!theme` or colour skinparam. Inlined as-is, that palette is
invisible to `prefers-color-scheme`: dark mode flips the page around it and leaves a
bright white slab where a diagram was.

A hand-written .puml may also carry a `<style>` block of its own, in a palette PlantUML
would never have picked — `packages.puml` does — and those literals need the same
treatment for a sharper reason: the text drawn on top of them is still plain black, so it
follows `--dgm-fg` into near-white whether or not the fill under it moved.

`_theme_diagram_colors` (build-review-html.py) rewrites each known literal colour, from
either palette, to the matching `--dgm-*` CSS variable at inline time, so the diagram repaints
along with the rest of the page. It is deliberately narrow — a fixed palette recognised
by exact value, not a blanket filter — because the diff renderers (`puml_diff.py`,
`seq_puml_diff.py`) paint an addition a deliberate green and a removal a deliberate red,
and a rule that is not precise about which literal it is touching risks fighting that
signal — or, worse, swapping the two — instead of just carrying it into dark mode.

Run with:  python3 -m pytest test_diagram_dark_mode.py
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def luminance(hexval):
    r, g, b = (int(hexval[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG relative-contrast ratio. Shared, because every colour this file adds has to
    answer the same question the diff reds already answer below."""
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)



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


def test_added_and_removed_get_their_own_distinct_variables():
    """Both differs paint an addition green and a removal red — one palette across the
    structural delta and the sequence delta, so a reader flipping between them is not
    told two things by one colour. The two hues stay two variables: the point of the
    split is that they carry opposite meanings into dark mode, not one brightness."""
    out_add = build._theme_diagram_colors('<text fill="#2E7D32">added</text>')
    out_del = build._theme_diagram_colors('<path style="stroke:#C62828;"/>')
    assert 'var(--dgm-diff-add)"' in out_add
    assert 'var(--dgm-diff-del)' in out_del
    assert build.DIAGRAM_COLOR_VARS["#2E7D32"] != build.DIAGRAM_COLOR_VARS["#C62828"]


def test_the_map_holds_exactly_the_literals_the_differs_paint_with():
    """The palette is declared in puml_diff.py and re-typed here as hex literals, because
    the CSS is a string in this file and cannot import anything. Re-typed is not the same
    as remembered: a hue changed in the differ and not here renders as an unthemed shape
    that never follows the page into dark mode, and nothing else would say so."""
    sys.path.insert(0, str(HERE.parent / "puml-diff"))
    try:
        import puml_diff as puml
        import seq_puml_diff as seq
    finally:
        sys.path.pop(0)

    assert build.DIAGRAM_COLOR_VARS[puml.ADDED] == "--dgm-diff-add"
    assert build.DIAGRAM_COLOR_VARS[puml.REMOVED] == "--dgm-diff-del"
    assert build.DIAGRAM_COLOR_VARS[seq.ADDED_TINT] == "--dgm-diff-add-bg"
    assert build.DIAGRAM_COLOR_VARS[seq.REMOVED_TINT] == "--dgm-diff-del-bg"


def test_the_hand_styled_puml_palette_is_covered_too():
    """PlantUML's defaults are not the only palette on the page. `packages.puml` carries a
    `<style>` block of its own (Material blue-grey), and for a long while those three
    literals fell through: the fills stayed pale while the `#000000` labels drawn on top of
    them followed --dgm-fg to near-white, so in dark mode every box on the Structure tab
    swallowed its own label. Regenerating the .puml is not the fix — the styling is
    deliberate and ArchUnit-tested — so the palette is recognised here like any other."""
    for hexval in ("#ECEFF1", "#546E7A", "#78909C"):
        assert hexval in build.DIAGRAM_COLOR_VARS, hexval
    assert build._theme_diagram_colors('<rect fill="#ECEFF1"/>') == \
        '<rect fill="var(--dgm-box-accent)"/>'
    assert 'stroke:var(--dgm-line-accent)' in \
        build._theme_diagram_colors('<rect style="stroke:#546E7A;"/>')
    assert 'stroke:var(--dgm-arrow-accent)' in \
        build._theme_diagram_colors('<path style="stroke:#78909C;"/>')


def test_every_variable_the_map_names_is_declared_in_both_themes():
    """The two halves are edited apart — a literal added to the map with no `--dgm-*`
    declaration behind it renders as an unresolved var, which paints black, not as an
    error. --dgm-icon is the one deliberate exception, documented in the CSS: its pale
    green already reads on a dark box, so dark mode leaves it alone."""
    css = build.CSS
    light_block, dark_block = css.split("@media (prefers-color-scheme: dark)", 1)
    for var in set(build.DIAGRAM_COLOR_VARS.values()):
        assert f"{var}:" in light_block, f"{var} has no light-mode value"
        if var != "--dgm-icon":
            assert f"{var}:" in dark_block, f"{var} has no dark-mode value"


def test_the_accent_boxes_still_hold_their_labels_in_dark_mode():
    """The labels inside those boxes are `#000000` -> --dgm-fg, so the pairing that has to
    hold is fill against foreground, not fill against canvas. Pinned as a number because
    the failure mode is not a crash but a box that looks fine and reads as empty."""
    dark = build.CSS.split("@media (prefers-color-scheme: dark)", 1)[1]
    box = re.search(r"--dgm-box-accent:(#[0-9a-fA-F]{6})", dark)[1]
    fg = re.search(r"--dgm-fg:(#[0-9a-fA-F]{6})", dark)[1]
    assert contrast(fg, box) >= 4.5, "the label inside a blue-grey component box is too dim"


def test_the_accent_strokes_read_against_the_dark_canvas():
    """A component border and an arrow are graphical objects, so the bar is WCAG's 3:1 for
    non-text, not 4.5:1 — but a hairline nobody can see is still not a diagram."""
    dark = build.CSS.split("@media (prefers-color-scheme: dark)", 1)[1]
    bg = re.search(r"--dgm-bg:(#[0-9a-fA-F]{6})", dark)[1]
    for name in ("--dgm-line-accent", "--dgm-arrow-accent"):
        val = re.search(rf"{name}:(#[0-9a-fA-F]{{6}})", dark)[1]
        assert contrast(val, bg) >= 3.0, f"{name} is too dim against the dark canvas"


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


def test_both_diff_hues_read_at_at_least_aa_contrast_on_the_dark_diagram_canvas():
    """The dark-mode values live only as literals inside the CSS string in
    build-review-html.py, so this pins the actual numbers rather than re-deriving them
    from the source — a change to either literal should have to walk through this
    assertion, not silently drop below the WCAG AA text minimum (4.5:1)."""

    css = build.CSS
    dark_block = css.split("@media (prefers-color-scheme: dark)", 1)[1]
    dgm_bg = re.search(r"--dgm-bg:(#[0-9a-fA-F]{6})", dark_block)[1]
    for name, what in (("--dgm-diff-add", "added green"), ("--dgm-diff-del", "removed red")):
        val = re.search(rf"{name}:(#[0-9a-fA-F]{{6}})", dark_block)[1]
        assert contrast(val, dgm_bg) >= 4.5, f"the {what} is too dim against the dark diagram canvas"


def test_the_diff_tints_still_hold_their_labels_in_dark_mode():
    """A sequence delta fills an added or removed lifeline box — and a note — with a tint,
    and PlantUML draws the name inside it in plain black, which the page rewrites to
    --dgm-fg. In dark mode that is near-white, so a tint that stayed pale is a box that
    reads as empty. Both must invert with the theme, not merely exist in it."""
    dark = build.CSS.split("@media (prefers-color-scheme: dark)", 1)[1]
    fg = re.search(r"--dgm-fg:(#[0-9a-fA-F]{6})", dark)[1]
    for name in ("--dgm-diff-add-bg", "--dgm-diff-del-bg"):
        tint = re.search(rf"{name}:(#[0-9a-fA-F]{{6}})", dark)[1]
        assert contrast(fg, tint) >= 4.5, f"a label on {name} is too dim to read"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
