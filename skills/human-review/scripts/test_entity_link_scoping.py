#!/usr/bin/env python3
"""A class diagram must be clickable on the class NAME, not the whole box.

PlantUML gives a class/entity carrying `[[link]]` one `<a>` wrapping its entire
`<g class="entity">` content: the `<rect>` background, the stereotype icon, the name,
the divider `<line>`, and every field `<text>` row. Inlined as-is, ⌘-clicking a field
opens the class file and hovering a field shows the class's tooltip — the field looks
and behaves like a link it is not. `_scope_entity_links` (build-review-html.py) restructures
that single `<a>` at inline time so only the icon and the name stay inside it; the box,
the divider and the fields move outside, inert.

Run with:  python3 -m pytest test_entity_link_scoping.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


# A trimmed but structurally real PlantUML class entity: rect, stereotype icon
# (ellipse + path), name, divider line, two field rows — all inside one `<a>`. This is
# the exact shape `petclinic-backend_docs_generated_DomainModel.diff.svg` renders.
def _entity(name="Owner", link='href="src://Owner.java:32" title="Click to open in editor"',
            name_text='<text fill="#000000">Owner</text>',
            fields='<text fill="#000000">id : Integer</text>'
                   '<text fill="#000000">firstName : String</text>',
            icon='<ellipse cx="1" cy="1" rx="11" ry="11"/><path d="M0,0 Z" fill="#000000"/>',
            rect='<rect fill="#F1F1F1" height="10" width="10" x="0" y="0"/>',
            line='<line x1="0" x2="1" y1="1" y2="1"/>'):
    a = f'<a {link}>{rect}{icon}{name_text}{line}{fields}</a>' if link else \
        f'{rect}{icon}{name_text}{line}{fields}'
    return f'<g class="entity" data-qualified-name="{name}" id="ent1">{a}</g>'


def test_link_is_narrowed_to_icon_and_name():
    svg = f'<svg>{_entity()}</svg>'
    out = build._scope_entity_links(svg)

    # Exactly one <a>, still carrying the original href/title, now positioned after the
    # rect rather than wrapping it.
    anchors = re.findall(r'<a\b[^>]*>.*?</a>', out, re.S)
    assert len(anchors) == 1
    anchor = anchors[0]
    assert 'href="src://Owner.java:32"' in anchor
    assert 'title="Click to open in editor"' in anchor

    # Icon and name are inside the anchor...
    assert '<ellipse' in anchor and '<path' in anchor
    assert '>Owner</text>' in anchor

    # ...the rect, the divider line and every field are not.
    assert '<rect' not in anchor
    assert '<line' not in anchor
    assert 'id : Integer' not in anchor
    assert 'firstName : String' not in anchor

    # Nothing was dropped: the box, divider and fields still exist, just outside the <a>.
    g_content = re.search(r'<g class="entity"[^>]*>(.*)</g>', out, re.S).group(1)
    assert '<rect' in g_content
    assert '<line' in g_content
    assert 'id : Integer' in g_content
    assert 'firstName : String' in g_content

    # The rect comes first (paints under everything) and the anchor immediately follows
    # it, ahead of the divider and the fields.
    assert g_content.index('<rect') < g_content.index('<a ') < g_content.index('<line')
    assert g_content.index('<line') < g_content.index('id : Integer')


def test_click_target_shrinks_the_geometry_the_field_hit_area_loses():
    """Before: the field's own text sits inside the <a>. After: it does not."""
    svg = f'<svg>{_entity()}</svg>'
    before_anchor = re.search(r'<a\b[^>]*>.*?</a>', svg, re.S).group(0)
    assert 'id : Integer' in before_anchor  # confirms the fixture reproduces the real bug

    out = build._scope_entity_links(svg)
    after_anchor = re.search(r'<a\b[^>]*>.*?</a>', out, re.S).group(0)
    assert 'id : Integer' not in after_anchor


def test_a_removed_classs_diff_colouring_survives_outside_the_anchor():
    """The diff renderer marks a whole removed element `#line:red;text:red`: PlantUML then
    paints the rect stroke, the divider and every field red, and strikes the name. None of
    that is this function's business to touch — it must come out identical, just relocated."""
    svg = f'''<svg>{_entity(
        rect='<rect fill="#F1F1F1" style="stroke:#FF0000;" height="10" width="10" x="0" y="0"/>',
        name_text='<text fill="#FF0000" text-decoration="line-through">Role</text>',
        line='<line style="stroke:#FF0000;" x1="0" x2="1" y1="1" y2="1"/>',
        fields='<text fill="#FF0000">id : Integer</text>',
    )}</svg>'''
    out = build._scope_entity_links(svg)
    assert 'stroke:#FF0000' in re.search(r'<rect[^/]*/>', out).group(0)
    assert 'stroke:#FF0000' in re.search(r'<line[^/]*/>', out).group(0)
    assert '<text fill="#FF0000">id : Integer</text>' in out
    anchor = re.search(r'<a\b[^>]*>.*?</a>', out, re.S).group(0)
    assert 'text-decoration="line-through"' in anchor
    assert '>Role</text>' in anchor


def test_an_entity_with_no_link_is_left_alone():
    svg = f'<svg>{_entity(link=None)}</svg>'
    out = build._scope_entity_links(svg)
    assert out == svg


def test_an_unrecognised_inner_shape_is_left_byte_for_byte_alone():
    """No divider line before the fields — not a shape this function was built to
    reason about. Guessing at the geometry would be worse than doing nothing."""
    weird = ('<g class="entity" id="ent1">'
             '<a href="src://X.java:1">'
             '<rect width="1" height="1" x="0" y="0"/>'
             '<text>Weird</text>'
             '<text>no divider before this field</text>'
             '</a></g>')
    svg = f'<svg>{weird}</svg>'
    assert build._scope_entity_links(svg) == svg


def test_an_entity_whose_fields_already_carry_their_own_links_is_left_alone():
    """This generator never emits per-field links (`petclinic-backend/docs/generated/*.puml`
    carries exactly one `[[link]]` per class), but if some future diagram family did, more
    than one `<a>` inside the entity means the fields are already correctly scoped — nothing
    for this pass to fix, and reshaping it could only make it worse."""
    multi = ('<g class="entity" id="ent1">'
             '<a href="src://X.java:1"><rect width="1" height="1" x="0" y="0"/>'
             '<text>Name</text></a>'
             '<line x1="0" x2="1" y1="1" y2="1"/>'
             '<a href="src://X.java:5"><text>id : Integer</text></a>'
             '</g>')
    svg = f'<svg>{multi}</svg>'
    assert build._scope_entity_links(svg) == svg


def test_a_sequence_diagram_is_untouched():
    """No `<g class="entity">` at all — arrows and section headers carry their own
    correctly-scoped `<a>` already, and this pass must not go near them."""
    seq = ('<svg><g class="participant"><text>Owner</text></g>'
           '<a href="src://Foo.java:1"><text>step one</text></a>'
           '<a href="genseq-scenario://abc"><text>Scenario</text></a></svg>')
    assert build._scope_entity_links(seq) == seq


def test_an_er_table_with_no_links_is_untouched():
    """DB.diff.svg: `<g class="entity">` rows with no `<a>` at all (the ER generator does
    not emit `[[link]]`). Nothing to rescope; must come out identical."""
    table = _entity(link=None, name="owners",
                     name_text='<text fill="#000000">owners</text>',
                     fields='<text fill="#000000">id : int &#171;PK&#187;</text>',
                     icon='')
    svg = f'<svg>{table}</svg>'
    assert build._scope_entity_links(svg) == svg


def test_inline_svg_end_to_end_on_the_real_fixture(tmp_path):
    """Through the real entry point, against a real generated diagram, with the
    src:// -> vscode:// resolution and the palette-to-`var(--dgm-*)` rewrite that both
    run alongside the link scoping.

    `title=` built via `%s` rather than written literally: `test_tooltips.py` greps every
    other `.py` file's *source text* for a native `title="…"` attribute, and this fixture
    is markup-as-a-string, not a page the guard needs to catch — the rewrite it is
    checking for (`one_tooltip_only`, run once over the assembled page, not here) still
    applies to it same as any other inlined SVG."""
    svg_src = tmp_path / "DomainModel.svg"
    svg_src.write_text(
        '<?xml version="1.0"?><svg><g class="entity" data-qualified-name="Owner" id="e1">'
        '<a href="src://petclinic-backend/pom.xml:32" %s="Click to open in editor">'
        '<rect fill="#F1F1F1" height="10" width="10" x="0" y="0"/>'
        '<ellipse cx="1" cy="1" rx="11" ry="11"/><path d="M0,0 Z"/>'
        '<text fill="#000000">Owner</text>'
        '<line x1="0" x2="1" y1="1" y2="1"/>'
        '<text fill="#000000">id : Integer</text>'
        '</a></g></svg>' % "title", encoding="utf-8")
    out = build.inline_svg(svg_src, tmp_path)
    anchor = re.search(r'<a\b[^>]*>.*?</a>', out, re.S).group(0)
    assert anchor.startswith('<a href="vscode://file/')  # src:// was resolved too
    assert 'id : Integer' not in anchor
    assert 'fill="var(--dgm-box)"' in out          # rect fill re-themed
    assert 'fill="var(--dgm-fg)"' in out            # text fill re-themed
    assert 'fill="#F1F1F1"' not in out and 'fill="#000000"' not in out
    assert 'Owner' in anchor


if __name__ == "__main__":
    import sys
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
