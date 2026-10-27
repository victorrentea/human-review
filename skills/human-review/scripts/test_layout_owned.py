"""The script-owned tabs and the scope bar are the skill's, whatever the content file says.

The fixture is the content file a GitHub Copilot (GPT) run wrote for petclinic on 2 Oct 2026,
cut down to the parts it got wrong: a Data tab with a section of its own prose and snippets,
an API tab of four sections instead of the verdict over the visual diff, a Demo tab declared
with no blocks (so it vanished), and a `gate green: CI …` chip on the scope bar.
"""
from __future__ import annotations

import json
import re

from test_build_review import _build, REF

COPILOT = {
    "title": "t",
    "scope": [{"auto": "autofixed", "href": "#review", "by": "Opus 5"},
              {"label": "gate", "value": "green: CI for 7f719558de59"}],
    "findings": [{"title": "f", "body": "<p>b</p>"}],
    "sections": [
        {"id": "video", "title": ""},
        {"id": "api-note", "title": "Coordinated contract migration",
         "body": "<p>Deploy and roll back the backend and frontend together.</p>",
         "snippets": [{"ref": REF + "-3", "caption": "The list response contract"}]},
        {"id": "swaggerdiff", "title": "", "includeHtml": "assets/openapi-diff.html"},
        {"id": "compatibility", "title": "", "includeHtml": "assets/openapi-compat.html"},
        {"id": "pb33f", "title": "Independent contract comparison",
         "includeHtml": "assets/openapi-changes.html"},
        {"id": "data-note", "title": "Page first, associations second",
         "body": "<p>The migration adds indexes without changing entity relationships.</p>",
         "snippets": [{"ref": REF + "-3", "caption": "Additive owner indexes"}]},
        {"id": "conceptual", "title": "", "body": "{{drawio:conceptual}}"},
    ],
    "tabs": [
        {"id": "review", "label": "Review", "blocks": [{"type": "findings"}]},
        {"id": "behaviour", "label": "Demo", "blocks": []},
        {"id": "api", "label": "API", "blocks": [
            {"type": "section", "id": "api-note"}, {"type": "section", "id": "swaggerdiff"},
            {"type": "section", "id": "compatibility"}, {"type": "section", "id": "pb33f"}]},
        {"id": "data", "label": "Data", "blocks": [
            {"type": "diagrams", "only": ["DomainModel", "DB"]},
            {"type": "section", "id": "data-note"}, {"type": "section", "id": "conceptual"}]},
    ],
}


def _assets(tmp_path):
    a = tmp_path / "assets"
    a.mkdir()
    (a / "openapi-verdict.html").write_text('<div class="oav">Backwards compatible</div>',
                                            encoding="utf-8")
    (a / "openapi-visual-diff.html").write_text("<html></html>", encoding="utf-8")
    (a / "feature.verdict.json").write_text(json.dumps(
        {"exit": 2, "log": ["[video] no feature script — nothing to film"]}), encoding="utf-8")


def _panel(page, tid):
    m = re.search(rf'<section class="panel" id="{tid}"[^>]*>(.*?)</section>\s*'
                  r'(?=<section class="panel"|</main>|<footer|$)', page, re.S)
    assert m, f"no {tid!r} panel on the page"
    return m.group(1)


def test_a_model_written_data_section_does_not_reach_the_page(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    assert "Page first, associations second" not in page
    assert "Additive owner indexes" not in page
    assert "data-note" in err, "the drop is named on stderr"
    # …while what the scripts produced stays: the conceptual model closes the tab.
    assert "run the <code>diagrams</code> step" in _panel(page, "data")


def test_the_api_tab_is_the_verdict_over_the_visual_diff_and_nothing_else(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    api = _panel(page, "api")
    assert "Backwards compatible" in api
    # Embedded in a shadow root, not framed: one scrollbar, the page's (5 Oct 2026).
    assert 'class="oavhost"' in api and 'data-hash="#only-touched"' in api
    assert "<iframe" not in api
    assert "Coordinated contract migration" not in page
    assert "Independent contract comparison" not in page
    assert "api-note" in err and "pb33f" in err


def test_a_demo_tab_declared_empty_still_says_why_there_is_no_film(tmp_path):
    _assets(tmp_path)
    page, _ = _build(tmp_path, COPILOT)
    demo = _panel(page, "behaviour")
    assert "Nothing was filmed." in demo
    assert "no feature script" in demo


def test_a_demo_tab_left_out_is_put_back_when_the_video_step_ran(tmp_path):
    _assets(tmp_path)
    content = {**COPILOT, "tabs": [t for t in COPILOT["tabs"] if t["id"] != "behaviour"]}
    page, _ = _build(tmp_path, content)
    assert 'id="tabbtn-behaviour"' in page
    assert "Nothing was filmed." in _panel(page, "behaviour")


def test_a_typed_chip_does_not_reach_the_scope_bar(tmp_path):
    _assets(tmp_path)
    page, err = _build(tmp_path, COPILOT)
    bar = page[page.index('<div class="scopebar">'):]
    bar = bar[:bar.index("</div>")]
    assert "green: CI" not in bar and ">gate<" not in bar
    assert "Opus 5" in bar, "the computed chip beside it still renders"
    assert "gate typed by hand" in err


# ── eval run 5 (3 Oct 2026): what the Sequence and Structure tabs were handed ─────────

def _own(tabs, tmp_path):
    import importlib
    layout = importlib.import_module("hrbuild.shared.layout")
    spec = {"tabs": tabs, "sections": []}
    return spec, layout.own_layout(spec, tmp_path)


def test_the_sequence_block_quotes_derived_tests_under_the_skills_own_heading(tmp_path):
    """Run 5 typed the snippets — another branch's line ranges — and `"title": ""`, which
    cost the tab the `Sequence diagrams of tests` heading the reference opens on."""
    spec, warnings = _own([{"id": "sequence", "label": "Sequence", "blocks": [
        {"type": "testpairs", "id": "sequences", "kind": "sequence", "title": "",
         "snippets": [{"ref": "petclinic-test/src/add-visit.spec.ts:31-43"}],
         "unpaired": {"id": "tests-nosequence", "title": "t", "body": "pipeline prose"}}]}],
        tmp_path)
    block = spec["tabs"][0]["blocks"][0]
    assert block["snippets"] == {"auto": "genseq"}
    assert "title" not in block, "absent, so the renderer prints its default heading"
    import importlib
    owned = importlib.import_module("hrbuild.shared.layout").LAYOUT_TESTPAIRS["unpaired"]
    assert block["unpaired"] == owned
    assert owned["title"] == "Tagged for tracing, but no diagram yet"
    assert block["unpaired"]["body"] != "pipeline prose"
    assert any("snippets" in w and "title" in w for w in warnings)


def test_a_heading_that_only_repeats_the_card_under_it_is_dropped(tmp_path):
    """Run 5's Structure tab: a bare `C2 Containers` H2 straight above the `C2-Containers`
    card. A heading that says something else is the author's to keep."""
    spec, warnings = _own([{"id": "packages", "label": "Structure", "blocks": [
        {"type": "diagrams", "manifest": "assets/c2/MANIFEST.tsv", "only": ["C2-Containers"],
         "id": "c2-containers", "title": "C2 Containers"},
        {"type": "puml", "src": "docs/MavenModules.puml", "name": "Maven modules",
         "title": "How the build is cut"}]}], tmp_path)
    c2, maven = spec["tabs"][0]["blocks"]
    assert "title" not in c2
    assert maven["title"] == "How the build is cut"
    assert any("C2 Containers" in w for w in warnings)


# ── the deployment drawing closes the Structure tab, when the c2 step drew one ────────

STRUCTURE = [{"id": "packages", "label": "Structure", "blocks": [
    {"type": "diagrams", "manifest": "assets/c2/MANIFEST.tsv", "only": ["C2-Containers"]}]}]


def test_the_deployment_drawing_goes_above_the_container_view_when_it_was_drawn(tmp_path):
    """Victor, 20 Oct 2026: above the C2 view it is checked against, no longer under it."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "deployment-diff.json").write_text("{}")
    spec, _ = _own(json.loads(json.dumps(STRUCTURE)), tmp_path)
    blocks = spec["tabs"][0]["blocks"]
    assert blocks[0] == {"type": "section", "id": "deployment"}
    assert blocks[1]["type"] == "diagrams"
    assert {"id": "deployment", "title": "", "body": "{{drawio:deployment}}"} in spec["sections"]


def test_no_deployment_section_for_a_project_that_configured_none(tmp_path):
    spec, _ = _own(json.loads(json.dumps(STRUCTURE)), tmp_path)
    assert [b["type"] for b in spec["tabs"][0]["blocks"]] == ["diagrams"]


def test_the_legend_shows_only_the_marks_the_drawing_has(tmp_path):
    import importlib
    diagrams = importlib.import_module("hrbuild.shared.diagrams")
    legend = diagrams.trace_legend({"traces": {"walked": ["a → b"], "unwalked": [],
                                               "undrawn": ["c → d"]}})
    assert "walked by a test" in legend and "missing from the drawing" in legend
    assert "no test walks it" not in legend
    assert diagrams.trace_legend({}) == ""


# ── a changed diagram no block names lands on its tab by itself (PR #49, 20 Oct 2026) ──

FRONTEND_ROW = {"name": "frontend-packages", "kind": "structural", "status": "modified",
                "source": "web/docs/frontend-packages.puml"}
STRUCTURE_TABS = [
    {"id": "data", "label": "Data", "blocks": [
        {"type": "diagrams", "only": ["DomainModel", "DB"]}]},
    {"id": "packages", "label": "Structure", "blocks": [
        {"type": "puml", "src": "docs/packages.puml", "name": "Java packages"},
        {"type": "puml", "src": "docs/MavenModules.puml", "name": "Maven modules"},
        {"type": "diagrams", "manifest": "assets/c2/MANIFEST.tsv", "only": ["C2-Containers"]}]},
]


def _claim(rows, tmp_path, files):
    import importlib
    layout = importlib.import_module("hrbuild.shared.layout")
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    spec = {"tabs": json.loads(json.dumps(STRUCTURE_TABS))}
    notes = layout.claim_changed_diagrams(spec, rows, tmp_path)
    return {t["id"]: t["blocks"] for t in spec["tabs"]}, notes


def test_a_changed_box_diagram_no_block_names_goes_on_structure_after_its_diagrams(tmp_path):
    """frontend-packages.puml is boxes and arrows: Structure, after the two context cards
    and before the C2 view that has its own manifest."""
    tabs, notes = _claim([FRONTEND_ROW], tmp_path, {
        FRONTEND_ROW["source"]: "@startuml\n[app] <<src/app>>\n[visits] <<src/app/visits>>\n"
                                "[visits] --> [app]\n@enduml\n"})
    assert tabs["packages"][2] == {"type": "diagrams", "only": ["frontend-packages"]}
    assert tabs["packages"][3]["manifest"] == "assets/c2/MANIFEST.tsv"
    assert tabs["data"][0]["only"] == ["DomainModel", "DB"]
    assert notes and "frontend-packages" in notes[0]


def test_a_changed_class_diagram_joins_the_data_gallery(tmp_path):
    row = {"name": "Billing", "kind": "structural", "status": "added",
           "source": "docs/Billing.puml"}
    tabs, _ = _claim([row], tmp_path, {
        "docs/Billing.puml": '@startuml\nentity "invoice" as invoice {\n}\nclass Line\n@enduml\n'})
    assert tabs["data"][0]["only"] == ["DomainModel", "DB", "Billing"]
    assert all("Billing" not in (b.get("only") or []) for b in tabs["packages"])


def test_a_context_card_whose_diagram_changed_becomes_its_delta_in_place(tmp_path):
    """A `puml` card would draw a changed packages.puml as `unchanged`, beside a delta
    card for the same file — so it turns into that card, where it stood, keeping its name."""
    row = {"name": "packages", "kind": "structural", "status": "modified",
           "source": "docs/packages.puml"}
    tabs, _ = _claim([row], tmp_path, {"docs/packages.puml": "@startuml\n[a]\n@enduml\n"})
    assert tabs["packages"][0] == {"type": "diagrams", "only": ["packages"],
                                   "labels": {"packages": "Java packages"}}
    assert len(tabs["packages"]) == 3, "no second card for the same diagram"


def test_a_diagram_a_block_already_names_is_left_where_it_is(tmp_path):
    row = {"name": "DB", "kind": "structural", "status": "modified",
           "source": "docs/generated/DB.puml"}
    tabs, notes = _claim([row], tmp_path, {})
    assert tabs["data"][0]["only"] == ["DomainModel", "DB"] and notes == []
    assert len(tabs["packages"]) == 3


def test_the_build_puts_an_unnamed_changed_diagram_on_structure_without_a_warning(tmp_path):
    _assets(tmp_path)
    d = tmp_path / "assets" / "diagrams"
    d.mkdir()
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>frontend delta</text></svg>'
    for f in ("fp.diff.svg", "fp.new.svg", "fp.old.svg"):
        (d / f).write_text(svg, encoding="utf-8")
    cols = ["name", "source", "kind", "status", "diff_puml", "svg", "focus", "new_svg",
            "old_svg"]
    (d / "MANIFEST.tsv").write_text(
        "\t".join(cols) + "\n" + "\t".join(["frontend-packages",
                                            "web/docs/frontend-packages.puml", "structural",
                                            "modified", "fp.diff.puml", "fp.diff.svg", "",
                                            "fp.new.svg", "fp.old.svg"]) + "\n",
        encoding="utf-8")
    content = {**COPILOT, "tabs": [*COPILOT["tabs"], {"id": "packages", "label": "Structure",
                                                       "blocks": []}]}
    page, err = _build(tmp_path, content)
    assert "no tab claims" not in err, err
    assert "frontend delta" in _panel(page, "packages")
    assert "frontend delta" not in _panel(page, "data")


def test_complexity_stands_right_after_city_whatever_order_the_content_file_wrote(tmp_path):
    """Victor, 20 Oct 2026: "Move the complexity after city". Tabs the layout does not own
    (`review`, `cost`) keep their own slots."""
    ids = ["review", "behaviour", "city", "dsaudit", "complexity", "logging", "cost"]
    spec, _ = _own([{"id": i, "label": i, "blocks": []} for i in ids], tmp_path)
    got = [t["id"] for t in spec["tabs"]]
    assert got[got.index("city") + 1] == "complexity"
    assert got[0] == "review" and got[-1] == "cost"


def test_a_diagrams_block_whose_context_changed_shows_its_delta_once(tmp_path):
    """PR #49: `only: ["Frontend packages"]` names the card, not the manifest row, so the
    changed frontend-packages.puml went unclaimed, joined the Java packages block as a
    delta, and its own block drew it again underneath as `unchanged`."""
    import importlib
    layout = importlib.import_module("hrbuild.shared.layout")
    (tmp_path / FRONTEND_ROW["source"]).parent.mkdir(parents=True)
    (tmp_path / FRONTEND_ROW["source"]).write_text("@startuml\n[app]\n@enduml\n", encoding="utf-8")
    java = {"type": "diagrams", "only": ["Java packages"],
            "context": {"src": "docs/packages.puml", "name": "Java packages"}}
    front = {"type": "diagrams", "only": ["Frontend packages"],
             "context": {"src": FRONTEND_ROW["source"], "name": "Frontend packages"}}
    spec = {"tabs": [{"id": "packages", "blocks": [java, front]}]}
    notes = layout.claim_changed_diagrams(spec, [FRONTEND_ROW], tmp_path)
    blocks = spec["tabs"][0]["blocks"]
    assert blocks[0]["only"] == ["Java packages"], "not homed on a stranger's block"
    assert blocks[1]["only"] == ["frontend-packages"]
    assert blocks[1]["labels"] == {"frontend-packages": "Frontend packages"}
    assert not notes
