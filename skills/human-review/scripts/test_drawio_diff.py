#!/usr/bin/env python3
"""`drawio-diff.py` compares two hand-drawn draw.io diagrams by the identity each
element declares in the mxGraph XML, and paints what the revision adds.

The two things worth guarding here are the two that are easy to get quietly wrong:

* **extraction** — the XML lives in a PNG `zTXt` chunk, deflate-compressed and
  percent-encoded, and draw.io sometimes wraps the `<diagram>` body in base64+deflate
  on top of that. A recipe copied from a blog post gets one of the three layers wrong
  and returns something that still parses. So the tests run against a real
  `.drawio.png` built here, and against both wrappings.
* **the colour rule** — red is the diagram's own to-do ("automation drew this, a human
  still has to re-lay it out"), orange is this tool's verdict ("new against the base").
  An element that is both renders red. The moment a human turns that element black in
  draw.io, it must render orange — still new — and that transition is the acceptance
  test the whole feature is bought for.

Run with:  python3 -m pytest test_drawio_diff.py
"""
from __future__ import annotations

import base64
import importlib.util
import json
import struct
import subprocess
import sys
import urllib.parse
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("drawio_diff", HERE / "drawio-diff.py")
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)


# ── fixtures: a diagram, and a real PNG carrying it ───────────────────────────────

def model(cells: str) -> str:
    return ('<mxfile host="test"><diagram id="d" name="Conceptual Model"><mxGraphModel>'
            '<root><mxCell id="0"/><mxCell id="1" parent="0"/>' + cells
            + "</root></mxGraphModel></diagram></mxfile>")


def box(cid: str, concept: str, x: int, y: int, style: str = "") -> str:
    return (f'<object label="{concept}" concept="{concept}" id="{cid}">'
            f'<mxCell style="rounded=0;fillColor=#dae8fc;strokeColor=#6c8ebf;{style}" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="120" height="40" as="geometry"/>'
            f"</mxCell></object>")


def line(cid: str, assoc: str, src: str, tgt: str, style: str = "strokeColor=#333333;",
         extra: str = "") -> str:
    return (f'<object label="" assoc="{assoc}" id="{cid}"{extra}>'
            f'<mxCell style="endArrow=none;{style}" edge="1" parent="1" '
            f'source="{src}" target="{tgt}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell></object>')


def star(cid: str, parent: str, style: str = "") -> str:
    return (f'<mxCell id="{cid}" value="*" style="edgeLabel;fontSize=24;{style}" '
            f'vertex="1" connectable="0" parent="{parent}">'
            f'<mxGeometry x="0.6" relative="1" as="geometry"/></mxCell>')


BASE = model(box("c-owner", "Owner", 40, 0) + box("c-pet", "Pet", 120, 100)
             + box("c-vet", "Vet", 520, 0) + box("c-visit", "Visit", 120, 200)
             + line("e-owner-pet", "Owner-Pet", "c-owner", "c-pet")
             + star("e-owner-pet-t", "e-owner-pet"))

# the branch: automation drew Vet–Visit, in red, and dropped a red note next to it
RED_EDGE = line("e-vet-visit", "Vet-Visit", "c-vet", "c-visit",
                "strokeColor=#FF0000;strokeWidth=2;",
                ' addedBy="pull-request-automation"')
RED_NOTE = ('<object label="Please manually fix the layout." id="note" '
            'addedBy="pull-request-automation"><mxCell style="text;fontColor=#FF0000;" '
            'vertex="1" parent="1">'
            '<mxGeometry x="250" y="250" width="300" height="26" as="geometry"/>'
            "</mxCell></object>")
BRANCH = model(BASE.split("<root>")[1].split("</root>")[0] + RED_EDGE
               + star("e-vet-visit-t", "e-vet-visit", "fontColor=#FF0000;") + RED_NOTE)

# the same branch after a human re-routed the line and turned it black
BLACKENED = BRANCH.replace("strokeColor=#FF0000", "strokeColor=#333333") \
                  .replace("fontColor=#FF0000", "fontColor=#333333")


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def png_with(xml: str, *, compressed_diagram: bool = False) -> bytes:
    """A real, decodable 1×1 PNG carrying `xml` the way draw.io carries it."""
    if compressed_diagram:
        # draw.io's other wrapping: the <diagram> body is base64(raw-deflate(quoted XML))
        inner = xml.split("<diagram", 1)[1].split(">", 1)[1].rsplit("</diagram>", 1)[0]
        deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = base64.b64encode(
            deflate.compress(urllib.parse.quote(inner).encode()) + deflate.flush()
        ).decode()
        xml = xml.split("<diagram", 1)[0] + "<diagram" + \
            xml.split("<diagram", 1)[1].split(">", 1)[0] + ">" + packed + "</diagram>" + \
            xml.rsplit("</diagram>", 1)[1]

    text = urllib.parse.quote(xml).encode()
    ztxt = b"mxGraphModel\x00\x00" + zlib.compress(text)
    raw = b"\x00\xff\xff\xff"  # one white pixel, filter byte first
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _chunk(b"zTXt", ztxt)
            + _chunk(b"IDAT", zlib.compress(raw))
            + _chunk(b"IEND", b""))


@pytest.fixture
def branch_png(tmp_path) -> Path:
    p = tmp_path / "ConceptualModel.drawio.png"
    p.write_bytes(png_with(BRANCH))
    return p


# ── extraction ────────────────────────────────────────────────────────────────────

def test_xml_comes_out_of_a_real_drawio_png(branch_png):
    xml = dd.extract_xml(branch_png)
    assert "<mxGraphModel" in xml
    assert 'concept="Owner"' in xml and 'assoc="Vet-Visit"' in xml


def test_the_png_the_test_builds_is_a_png_python_can_decode(branch_png):
    """Otherwise the extraction test would be passing against a fixture that is not
    actually a PNG, and would keep passing when the chunk walker breaks."""
    data = branch_png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    kinds = [k for k, _ in dd.png_text_chunks(data)]
    assert kinds == ["mxGraphModel"]


def test_the_project_diagram_is_readable():
    """The real file in the repo under review, when this is run from it — the fixtures
    above are only as good as their resemblance to it."""
    real = Path("petclinic-backend/docs/ConceptualModel.drawio.png")
    if not real.is_file():
        pytest.skip("not running from a checkout that has the diagram")
    cells = dd.parse_model(dd.extract_xml(real))
    concepts = {c.attrs["concept"] for c in cells.values() if c.attrs.get("concept")}
    assert {"Owner", "Pet", "Vet", "Visit"} <= concepts


def test_a_base64_deflated_diagram_body_is_unwrapped(tmp_path):
    p = tmp_path / "packed.drawio.png"
    p.write_bytes(png_with(BRANCH, compressed_diagram=True))
    xml = dd.extract_xml(p)
    assert 'assoc="Vet-Visit"' in xml
    assert dd.parse_model(xml)["c-owner"].attrs["concept"] == "Owner"


def test_plain_xml_files_are_accepted_too(tmp_path):
    p = tmp_path / "d.drawio"
    p.write_text(BRANCH)
    assert 'assoc="Vet-Visit"' in dd.extract_xml(p)


def test_a_png_without_a_diagram_says_so(tmp_path):
    p = tmp_path / "photo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n"
                  + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                  + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="no mxGraph XML"):
        dd.extract_xml(p)


# ── the diff ──────────────────────────────────────────────────────────────────────

def test_added_edge_and_annotation_are_both_found():
    v = dd.diff_models(BASE, BRANCH)
    assert [(a["kind"], a["what"]) for a in v["added"] if a["kind"] != "label"] == [
        ("edge", "Vet-Visit"), ("annotation", "Please manually fix the layout.")]
    assert v["removed"] == [] and v["changed"] == []


def test_a_sticky_note_is_not_counted_as_a_box():
    """The note automation drops next to a line it drew is a text shape, not a concept.
    Calling it a box is how the report ends up announcing a domain class nobody added."""
    v = dd.diff_models(BASE, BRANCH)
    assert [a["kind"] for a in v["added"] if a["kind"] != "label"] == ["edge", "annotation"]
    assert "0 boxes, 1 line, 1 note" in dd.counted(v)


def test_a_real_new_concept_box_is_counted_as_one():
    """The other half of the claim: when a concept box genuinely arrives, it says box."""
    grown = model(BASE.split("<root>")[1].split("</root>")[0]
                  + box("c-invoice", "Invoice", 520, 200))
    v = dd.diff_models(BASE, grown)
    assert [(a["kind"], a["what"]) for a in v["added"]] == [("node", "Invoice")]
    assert dd.counted(v).startswith("added 1 box, 0 lines")


def test_removed_edge_and_annotation_are_both_found():
    v = dd.diff_models(BRANCH, BASE)
    assert [(r["kind"], r["what"]) for r in v["removed"] if r["kind"] != "label"] == [
        ("edge", "Vet-Visit"), ("annotation", "Please manually fix the layout.")]
    assert v["added"] == []


def test_an_empty_base_makes_everything_new():
    v = dd.diff_models(dd.EMPTY_MODEL, BRANCH)
    kinds = [a["kind"] for a in v["added"]]
    assert kinds.count("node") == 4 and kinds.count("edge") == 2
    assert kinds.count("annotation") == 1
    assert v["removed"] == []


def test_a_missing_file_at_the_base_is_an_empty_diagram_not_a_crash(monkeypatch):
    monkeypatch.setattr(dd.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 128, b"", b"nope"))
    assert dd.read_at("deadbeef", "docs/Missing.drawio.png") == dd.EMPTY_MODEL


def test_identity_is_declared_not_drawn():
    """A box renamed in draw.io is the same box; the guardrail test says so, and the
    differ has to agree or every rename would read as a delete plus an add."""
    renamed = BASE.replace('<object label="Owner" concept="Owner"',
                           '<object label="Proprietar" concept="Owner"')
    v = dd.diff_models(BASE, renamed)
    assert v["added"] == [] and v["removed"] == []
    assert [c["what"] for c in v["changed"]] == ["Owner"]


def test_moving_a_box_is_reported_as_moved_not_as_a_change():
    """Position belongs to the human. A drag must never read as content drift."""
    dragged = BASE.replace('x="520" y="0"', 'x="600" y="300"')
    v = dd.diff_models(BASE, dragged)
    assert v["added"] == [] and v["removed"] == [] and v["changed"] == []
    assert [m["what"] for m in v["moved"]] == ["Vet"]


def test_rerouting_an_edge_to_another_box_is_a_change():
    rerouted = BRANCH.replace('source="c-vet" target="c-visit"',
                              'source="c-owner" target="c-visit"')
    v = dd.diff_models(BRANCH, rerouted)
    assert v["added"] == [] and v["removed"] == []
    assert any("source" in " ".join(c["changes"]) for c in v["changed"])


def test_an_unchanged_diagram_diffs_to_nothing():
    v = dd.diff_models(BRANCH, BRANCH)
    assert v == {"added": [], "removed": [], "changed": [], "moved": []}


# ── the colour rule ───────────────────────────────────────────────────────────────

def test_red_is_read_off_the_drawn_colour():
    assert dd.is_red("strokeColor=#FF0000;") and dd.is_red("fontColor=red;")
    assert not dd.is_red("strokeColor=#333333;")
    assert not dd.is_red("fillColor=#FF0000;"), "a red fill is not a red to-do marker"


def test_an_element_automation_drew_red_stays_red_in_the_diff():
    v = dd.diff_models(BASE, BRANCH)
    assert all(a["already_red"] for a in v["added"])
    painted = dd.paint_added(BRANCH, v)
    assert dd.ADDED_COLOR not in painted
    assert "strokeColor=#FF0000" in painted


def test_once_a_human_turns_it_black_the_diff_turns_it_orange():
    """The acceptance test for the whole feature: red is a to-do that the human clears,
    orange is "new against main", which stays true after they clear it."""
    v = dd.diff_models(BASE, BLACKENED)
    assert not any(a["already_red"] for a in v["added"])
    painted = dd.paint_added(BLACKENED, v)
    cells = dd.parse_model(painted)
    assert dd.style_dict(cells["e-vet-visit"].style)["strokeColor"] == dd.ADDED_COLOR
    assert dd.style_dict(cells["note"].style)["fontColor"] == dd.ADDED_COLOR
    # the cardinality label rides along with the edge it belongs to
    assert dd.style_dict(cells["e-vet-visit-t"].style)["fontColor"] == dd.ADDED_COLOR


def test_nothing_that_was_already_on_the_map_is_repainted():
    painted = dd.paint_added(BLACKENED, dd.diff_models(BASE, BLACKENED))
    cells = dd.parse_model(painted)
    assert dd.style_dict(cells["e-owner-pet"].style)["strokeColor"] == "#333333"
    assert "strokeColor" not in dd.style_dict(cells["c-owner"].style) or \
        dd.style_dict(cells["c-owner"].style)["strokeColor"] == "#6c8ebf"


def test_the_orange_keeps_its_own_dark_half():
    svg = f'<path style="stroke: light-dark({dd._rgb(dd.ADDED_COLOR)}, rgb(1, 2, 3));"/>'
    assert dd._rgb(dd.ADDED_COLOR_DARK) in dd.pin_added_dark(svg)
    untouched = '<path style="stroke: light-dark(#FF0000, #ff9090);"/>'
    assert dd.pin_added_dark(untouched) == untouched


# ── rendering ─────────────────────────────────────────────────────────────────────

def test_the_builtin_renderer_draws_every_box_and_line(tmp_path):
    out = tmp_path / "d.svg"
    dd.render_builtin(dd.paint_added(BLACKENED, dd.diff_models(BASE, BLACKENED)), out)
    svg = out.read_text()
    assert svg.count("<rect") == 4 and svg.count("<line") == 2
    assert "Owner" in svg and "Visit" in svg
    assert f"light-dark({dd.ADDED_COLOR}, {dd.ADDED_COLOR_DARK})" in svg
    assert "color-scheme: light dark" in svg


def test_slim_keeps_the_label_and_drops_the_bitmap_fallback():
    svg = ('<g><switch><foreignObject requiredFeatures="x"><div>Owner</div>'
           '</foreignObject><image xlink:href="data:image/png;base64,AAAA"/></switch></g>')
    out = dd.slim(svg)
    assert "Owner" in out and "<image" not in out and "<switch" not in out


def test_end_to_end_writes_three_svgs_and_a_verdict(tmp_path, branch_png):
    base_png = tmp_path / "base.drawio.png"
    base_png.write_bytes(png_with(BASE))
    puml = tmp_path / "DomainModel.puml"
    puml.write_text(DOMAIN_PUML)
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(HERE / "drawio-diff.py"), str(base_png), str(branch_png),
         "--out-dir", str(out), "--name", "conceptual", "--renderer", "builtin",
         "--concepts", str(puml), "--repo-root", "/repo"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for view in ("original", "new", "diff"):
        assert (out / f"conceptual-{view}.svg").read_text().startswith("<svg")
    verdict = json.loads((out / "conceptual-diff.json").read_text())
    assert [a["what"] for a in verdict["added"] if a["kind"] == "edge"] == ["Vet-Visit"]
    assert "red (automation's to-do)" in proc.stdout


def test_a_repainted_note_gets_a_colour_but_not_a_border():
    """draw.io reads `strokeColor` on a text shape as "draw a box around it". Painting
    the note orange must not put it in a frame the diagram never had."""
    painted = dd.paint_added(BLACKENED, dd.diff_models(BASE, BLACKENED))
    note = dd.style_dict(dd.parse_model(painted)["note"].style)
    assert note["fontColor"] == dd.ADDED_COLOR
    assert "strokeColor" not in note


def test_repainting_leaves_the_rest_of_the_style_recipe_alone():
    """A style is an ordered recipe, not a dict: draw.io's bare keys (`text`,
    `edgeLabel`, `rounded=0`) mean something and must survive the rewrite."""
    out = dd._paint("rounded=0;whiteSpace=wrap;html=1;strokeColor=#333333;", "#E8760D")
    assert out.startswith("rounded=0;whiteSpace=wrap;html=1;")
    assert "strokeColor=#E8760D" in out and "fontColor=#E8760D" in out
    assert dd._paint("text;html=1;fontSize=14;", "#E8760D").startswith("text;html=1;")


# ── linking a concept box to the class it names ───────────────────────────────────

DOMAIN_PUML = """@startuml
title Domain Model
class Owner [[src://backend/src/main/java/x/domain/Owner.java:32{Click to open in editor}]] {
  id : Integer
}
class Pet [[src://backend/src/main/java/x/domain/Pet.java:33{Click to open in editor}]] {
}
class Vet [[src://backend/src/main/java/x/domain/Vet.java:15{Click to open in editor}]] {
}
class Visit [[src://backend/src/main/java/x/domain/Visit.java:15{Click to open in editor}]] {
}
Owner "1" -- "0..*" Pet
@enduml
"""


@pytest.fixture
def sources(tmp_path):
    puml = tmp_path / "DomainModel.puml"
    puml.write_text(DOMAIN_PUML)
    return dd.concept_sources(puml)


def test_the_mapping_is_read_off_the_generated_domain_model(sources):
    """Not a second name-matching scheme: `DomainModelExtractor` decides what a domain
    class is, `DomainModelExtractorTest` writes that out as this PlantUML, and
    `ConceptualModelDiagramTest` checks the draw.io map against the same extractor. The
    line each class is declared on comes along for free."""
    assert sources == {"Owner": ("backend/src/main/java/x/domain/Owner.java", 32),
                       "Pet": ("backend/src/main/java/x/domain/Pet.java", 33),
                       "Vet": ("backend/src/main/java/x/domain/Vet.java", 15),
                       "Visit": ("backend/src/main/java/x/domain/Visit.java", 15)}


def test_a_missing_domain_model_is_no_links_rather_than_a_crash(tmp_path):
    assert dd.concept_sources(tmp_path / "nope.puml") == {}
    out, missing = dd.link_concepts(BRANCH, {}, tmp_path)
    assert "link=" not in out
    assert missing == ["Owner", "Pet", "Vet", "Visit"]


def test_every_concept_box_points_at_its_class(sources, tmp_path):
    out, missing = dd.link_concepts(BASE, sources, "/repo")
    assert missing == []
    cells = dd.parse_model(out)
    assert cells["c-owner"].attrs["link"] == \
        "vscode://file//repo/backend/src/main/java/x/domain/Owner.java:32:1"
    assert cells["c-visit"].attrs["link"].endswith("Visit.java:15:1")


def test_the_line_is_the_class_declaration_not_line_one(sources):
    out, _ = dd.link_concepts(BASE, sources, "/repo")
    assert ":32:1" in dd.parse_model(out)["c-owner"].attrs["link"]


def test_nothing_but_a_concept_box_becomes_a_link(sources):
    """The user made the same call on the class PlantUML: the class name is the handle,
    a field is inert. Here: not the line, not its cardinality, not the sticky note."""
    out, _ = dd.link_concepts(BRANCH, sources, "/repo")
    cells = dd.parse_model(out)
    assert cells["note"].kind == "annotation" and "link" not in cells["note"].attrs
    assert "link" not in cells["e-vet-visit"].attrs
    assert "link" not in cells["e-vet-visit-t"].attrs
    assert sum(1 for c in cells.values() if c.attrs.get("link")) == 4


def test_a_concept_with_no_class_is_left_unlinked_and_named(sources):
    """Impossible while the guardrail passes — it refuses a box whose concept no longer
    exists in the domain package. If it ever happens the test is right and the link is
    wrong, so the box gets nothing and the caller is told which one."""
    grown = model(BASE.split("<root>")[1].split("</root>")[0]
                  + box("c-ghost", "Ghost", 700, 0))
    out, missing = dd.link_concepts(grown, sources, "/repo")
    assert missing == ["Ghost"]
    cells = dd.parse_model(out)
    assert "link" not in cells["c-ghost"].attrs
    assert cells["c-owner"].attrs["link"].endswith("Owner.java:32:1")


def test_the_old_pane_drops_a_link_the_working_tree_no_longer_has(sources):
    """All three panes are linked from ONE map, taken from the working tree. That is what
    makes `Old` behave: a concept this branch deleted is simply absent from it."""
    retired = model(BASE.split("<root>")[1].split("</root>")[0]
                    + box("c-visitor", "Visitor", 700, 0))   # deleted on this branch
    out, missing = dd.link_concepts(retired, sources, "/repo")
    assert missing == ["Visitor"]
    assert "link" not in dd.parse_model(out)["c-visitor"].attrs
    assert "vscode://" not in dd.parse_model(out)["c-visitor"].style


# ── getting the anchor to work on this page ───────────────────────────────────────

def test_an_xlink_only_anchor_gains_a_plain_href():
    """draw.io exports SVG 1.1 anchors, which carry `xlink:href` alone. The report's one
    delegated listener selects `a[href^="vscode:"]`, and an attribute selector matches the
    attribute that is written — so xlink alone is inert. PlantUML needed both too."""
    out = dd.dual_href('<a xlink:href="vscode://file//x.java:3:1"><rect/></a>')
    assert 'href="vscode://file//x.java:3:1"' in out
    assert 'xlink:href="vscode://file//x.java:3:1"' in out


def test_an_anchor_that_already_has_href_is_left_alone():
    same = '<a href="https://x" xlink:href="https://x"><rect/></a>'
    assert dd.dual_href(same) == same


def test_the_builtin_renderer_writes_both_spellings(tmp_path, sources):
    xml, _ = dd.link_concepts(BASE, sources, "/repo")
    out = tmp_path / "d.svg"
    dd.render_builtin(xml, out)
    svg = out.read_text()
    assert svg.count('xlink:href="vscode://') == 4 and svg.count('href="vscode://') == 8
    assert 'xmlns:xlink="http://www.w3.org/1999/xlink"' in svg
    assert "cursor:pointer" in svg


def test_slim_removes_the_offsite_link_drawio_signs_its_exports_with():
    """A "Text is not SVG - cannot display" line pointing at drawio.com, shown only where
    foreignObject is missing. Invisible here, but still an off-site link in a review page
    that nothing on the page explains."""
    tail = ('<g>kept</g><switch><g requiredFeatures="http://www.w3.org/TR/SVG11/feature'
            '#Extensibility"/><a transform="translate(0,-5)" xlink:href='
            '"https://www.drawio.com/doc/faq/svg-export-text-problems" target="_blank">'
            '<text x="50%">Text is not SVG - cannot display</text></a></switch>')
    out = dd.slim(tail)
    assert out == "<g>kept</g>"


def test_end_to_end_links_all_three_panes(tmp_path, branch_png):
    base_png = tmp_path / "base.drawio.png"
    base_png.write_bytes(png_with(BASE))
    puml = tmp_path / "DomainModel.puml"
    puml.write_text(DOMAIN_PUML)
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(HERE / "drawio-diff.py"), str(base_png), str(branch_png),
         "--out-dir", str(out), "--name", "c", "--renderer", "builtin",
         "--concepts", str(puml), "--repo-root", "/repo"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for view in ("original", "new", "diff"):
        svg = (out / f"c-{view}.svg").read_text()
        assert svg.count('href="vscode://file//repo/backend') == 8, view
    assert "4 concept(s) linked to their class" in proc.stdout
    assert json.loads((out / "c-diff.json").read_text())["unlinked_concepts"] == []

def test_concepts_is_required_so_unlinked_boxes_cannot_ship_silently(tmp_path, branch_png):
    """Omitting --concepts used to succeed and quietly produce boxes that are not links —
    the failure nobody notices until they try to click one. It is an argparse error now."""
    base_png = tmp_path / "base.drawio.png"
    base_png.write_bytes(png_with(BASE))
    proc = subprocess.run(
        [sys.executable, str(HERE / "drawio-diff.py"), str(base_png), str(branch_png),
         "--out-dir", str(tmp_path / "out"), "--renderer", "builtin"],
        capture_output=True, text=True)
    assert proc.returncode != 0
    assert "--concepts" in proc.stderr
