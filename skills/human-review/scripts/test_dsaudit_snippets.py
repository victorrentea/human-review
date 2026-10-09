#!/usr/bin/env python3
"""A drawn verdict opens onto the element itself: its markup, its template line, its rule.

A red box on a screenshot says *that* a control is wrong. The reviewer then has to find
which element it is in the markup, which template wrote it, and why it counts as a gap
rather than a choice — three lookups the audit already has every answer to. Pinned below:

* **the markup is the page's, trimmed** — tag, the attributes somebody wrote, a short
  inside, at most ten lines; Angular's own stamps are not markup anybody wrote;
* **the source is the template that wrote it** — reached through the component the page
  says the element sits in, never a guess dressed as a line number;
* **the rule names the component** that should have been used, from the audit's own
  registry, and how widely the rest of the app already uses it;
* **one number per verdict**, the same on the picture and on its row.

Run with:  python3 -m pytest test_dsaudit_snippets.py
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CAPTURE = HERE / "testdata" / "ds-audit" / "capture"

_spec = importlib.util.spec_from_file_location("ds_audit", HERE / "ds-audit.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def _select(n_options=3, **attrs):
    return {"tag": "select",
            "attrs": [[k, v] for k, v in (attrs or {"id": "vet", "name": "vetId"}).items()],
            "children": [{"tag": "option", "attrs": [], "text": f"Vet {i}"}
                         for i in range(min(n_options, 4))],
            **({"more": n_options - 4, "more_tag": "option"} if n_options > 4 else {})}


# ── the markup ────────────────────────────────────────────────────────────────────

def test_an_element_prints_one_tag_per_line_with_its_options_under_it():
    out = ds.pretty_html(_select(2))
    assert out.split("\n") == [
        '<select id="vet" name="vetId">',
        "  <option>Vet 0</option>",
        "  <option>Vet 1</option>",
        "</select>"]


def test_what_the_extractor_left_out_is_counted_not_hidden():
    out = ds.pretty_html(_select(9))
    assert "<!-- 5 more <option> -->" in out
    assert out.count("  <option>") == 4


def test_a_long_element_is_capped_and_keeps_its_head_and_its_closing_tag():
    """The opening tag is the finding; its fourteenth option is not. Ten lines at most,
    and the cut says how much of the middle it dropped."""
    host = {"tag": "app-combo", "attrs": [["name", "vetId"], ["data-ds", "combo"]],
            "children": [_select(4), _select(4, id="other")]}
    lines = ds.pretty_html(host).split("\n")
    assert len(lines) == 10
    assert lines[0] == '<app-combo name="vetId" data-ds="combo">'
    assert lines[-1] == "</app-combo>"
    assert re.fullmatch(r"  <!-- … \d+ more lines -->", lines[-2])


def test_text_and_attribute_values_are_escaped_as_markup():
    node = {"tag": "button", "attrs": [["aria-label", 'say "hi"'], ["disabled", ""]],
            "text": "<b>Save</b>"}
    assert ds.pretty_html(node) == \
        '<button aria-label="say &quot;hi&quot;" disabled>&lt;b&gt;Save&lt;/b&gt;</button>'
    assert ds.pretty_html({"tag": "input", "attrs": [["type", "date"]]}) == \
        '<input type="date">', "a void element has no closing tag"
    assert ds.pretty_html({"tag": "option", "attrs": [["value", ""]], "text": "No vet"}) == \
        '<option value="">No vet</option>', "only a boolean attribute is written bare"


def test_a_capture_without_markup_says_its_tag_was_rebuilt():
    """A capture older than the extractor's markup still gets a snippet — labelled as
    rebuilt from the identity the snapshot always kept, never passed off as the page's."""
    old = ds.snippet_of({"tag": "select", "id": "vet", "name": "vetId", "type": None})
    assert old == {"html": '<select id="vet" name="vetId"></select>', "from": "reconstructed"}
    new = ds.snippet_of({"tag": "select", "html": _select(1)})
    assert new["from"] == "rendered" and new["html"].startswith('<select id="vet"')


def test_the_page_records_markup_only_for_what_can_become_a_verdict():
    js = ds.SNAPSHOT_JS
    assert "html: judged ? snipOf(el, 2) : null" in js
    assert "hosts: judged ? hostsOf(el) : []" in js
    # Angular's stamps: CSS scoping, dev-mode binding mirrors. Not markup anybody wrote.
    assert "_ngcontent|_nghost|ng-reflect-" in js


def test_the_extractor_trims_a_real_angular_element_in_a_browser():
    """The extractor, run on a page: the scoping attributes and the state classes Angular
    stamps go, the attributes the template wrote stay, and the components around the
    element are listed nearest first."""
    sync = pytest.importorskip("playwright.sync_api")
    page_html = """<body><app-root _nghost-abc=""><app-visit-edit _ngcontent-abc="">
      <form id="visit"><div class="form-group"><label for="vet">Vet</label>
      <select _ngcontent-xyz-c12="" id="vet" name="vetId" ng-reflect-model="1"
              class="form-control ng-untouched ng-pristine ng-valid" style="width:200px">
        <option value="0: null">-- none --</option><option value="1: 1">James Carter</option>
        <option value="2: 2">Helen Leary</option><option value="3: 3">Linda Douglas</option>
        <option value="4: 4">Rafael Ortega</option><option value="5: 5">Henry Stevens</option>
      </select></div></form></app-visit-edit></app-root></body>"""
    try:
        with sync.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(page_html)
            snap = page.evaluate(ds.SNAPSHOT_JS)
            browser.close()
    except Exception as e:  # noqa: BLE001 - no browser on this machine is a skip
        pytest.skip(f"no Chromium for Playwright: {e}")
    sel, = [n for n in snap["nodes"] if n["tag"] == "select"]
    assert sel["hosts"] == ["app-visit-edit", "app-root"]
    out = ds.snippet_of(sel)["html"].split("\n")
    assert out[0] == '<select id="vet" name="vetId" class="form-control">'
    assert out[1] == '  <option value="0: null">-- none --</option>'
    assert out[-2] == "  <!-- 2 more <option> -->" and out[-1] == "</select>"
    label, = [n for n in snap["nodes"] if n["tag"] == "label"]
    assert label["html"] is None and label["hosts"] == [], "not a control: no markup kept"


# ── where it was written ──────────────────────────────────────────────────────────

def _angular_tree(tmp_path):
    app = tmp_path / "src" / "app"
    (app / "visits" / "visit-edit").mkdir(parents=True)
    (app / "visits" / "visit-edit" / "visit-edit.component.ts").write_text(
        "import {Component} from '@angular/core';\n"
        "@Component({\n  selector: 'app-visit-edit',\n"
        "  templateUrl: './visit-edit.component.html'\n})\n"
        "export class VisitEditComponent {}\n")
    (app / "visits" / "visit-edit" / "visit-edit.component.html").write_text(
        '<form id="visit">\n'
        '  <select [id]="dynamicId" name="other"></select>\n'
        '  <div class="form-group">\n'
        '    <select id="vet" name="vetId"\n'
        '            class="form-control" [(ngModel)]="visit.vetId">\n'
        '      <option [ngValue]="null">-- none --</option>\n'
        "    </select>\n"
        "  </div>\n"
        "  <app-card><select name=\"inCard\"></select></app-card>\n"
        "</form>\n")
    (app / "shared").mkdir()
    (app / "shared" / "card.component.ts").write_text(
        "@Component({\n  selector: 'app-card',\n"
        "  template: `\n    <div class=\"card\">\n      <ng-content></ng-content>\n"
        "    </div>`\n})\nexport class CardComponent {}\n")
    (app / "shared" / "card.component.spec.ts").write_text(
        "@Component({ selector: 'app-spec-only', template: `<select id=\"vet\"></select>` })\n"
        "export class X {}\n")
    return tmp_path, app


def _bare(tag="select", hosts=None, html=None, **el):
    f = {"verdict": "bare", "side": "new", "element": {"tag": tag, "sig": "s", **el}}
    if hosts is not None:
        f["element"]["hosts"] = hosts
    f["snippet"] = ({"html": html, "from": "rendered"} if html else
                    ds.snippet_of({"tag": tag, **el}))
    return f


def test_every_component_is_indexed_by_its_selector_to_its_template(tmp_path):
    root, app = _angular_tree(tmp_path)
    idx = ds.template_index([app])
    assert set(idx) == {"app-visit-edit", "app-card"}, "a spec file declares no screen"
    assert idx["app-visit-edit"]["path"].name == "visit-edit.component.html"
    card = idx["app-card"]
    assert card["path"].name == "card.component.ts" and card["line0"] == 2
    assert '<div class="card">' in card["text"]


def test_the_element_is_found_in_the_template_of_the_component_around_it(tmp_path):
    """The page says which components sit around the select; the nearest one whose
    template has a `<select>` naming it is the file, and the line is its opening tag's —
    across a tag whose attributes run over two lines."""
    root, app = _angular_tree(tmp_path)
    f = _bare(hosts=["app-visit-edit", "app-root"],
              html='<select id="vet" name="vetId" class="form-control">\n</select>')
    src = ds.locate_source(f, ds.template_index([app]), root)
    assert src == {"file": "src/app/visits/visit-edit/visit-edit.component.html",
                   "line": 4, "via": "app-visit-edit", "matches": 1}


def test_a_bound_attribute_is_not_the_static_one_it_resembles(tmp_path):
    """`[id]="dynamicId"` on line 2 says nothing about an id of `vet`; only a written
    `id="vet"` scores. Otherwise the first select of the form wins every time."""
    root, app = _angular_tree(tmp_path)
    f = _bare(hosts=["app-visit-edit"], html='<select id="vet">\n</select>')
    assert ds.locate_source(f, ds.template_index([app]), root)["line"] == 4
    assert not ds._attr_written(' [id]="vet"', "id", "vet")
    assert not ds._attr_written(' data-name="vetId"', "name", "vetId")
    assert ds._attr_written(' formControlName="vetId"', "formcontrolname", "vetId")


def test_a_projected_element_is_found_in_the_parent_that_wrote_it(tmp_path):
    """A select inside `<app-card>` sits in the card's DOM but was written in the visit
    form's template, inside the card's tag. The card's own template has no select, so the
    search climbs to the next component out."""
    root, app = _angular_tree(tmp_path)
    f = _bare(hosts=["app-card", "app-visit-edit"], html='<select name="inCard">\n</select>')
    src = ds.locate_source(f, ds.template_index([app]), root)
    assert src["via"] == "app-visit-edit" and src["line"] == 9


def test_with_no_hosts_only_a_match_on_a_name_counts_and_changed_templates_go_first(
        tmp_path):
    """A capture older than `hosts`: every template is searched, but a bare `<select>`
    tag with nothing naming it is not evidence of anything."""
    root, app = _angular_tree(tmp_path)
    idx = ds.template_index([app])
    f = _bare(id="vet", name="vetId")
    src = ds.locate_source(f, idx, root, changed=[])
    assert src["via"] == "search" and src["line"] == 4
    assert ds.locate_source(_bare(id="nowhere"), idx, root) is None
    assert ds.locate_source(_bare(), idx, root) is None, "no identity, no guess"


def test_an_ambiguous_match_says_so(tmp_path):
    root, app = _angular_tree(tmp_path)
    f = _bare(hosts=["app-visit-edit"], html="<select class=\"x\">\n</select>")
    src = ds.locate_source(f, ds.template_index([app]), root)
    assert src["matches"] == 3 and src["line"] == 2


def test_the_source_bar_links_the_template_line_the_way_every_snippet_does(tmp_path):
    root, app = _angular_tree(tmp_path)
    f = _bare(hosts=["app-visit-edit"], html='<select id="vet">\n</select>')
    f["source"] = ds.locate_source(f, ds.template_index([app]), root)
    bar = ds.source_bar(f, root, None)
    target = (root / f["source"]["file"]).resolve()
    assert f'href="vscode://file/{target}:4:1"' in bar
    assert ">visit-edit.component.html</a>" in bar, "line 4 is in the href, not the face"
    assert 'class="srcref srcbar-path"' in bar, "the page's own source bar, not a copy"
    assert ">as rendered</span>" in bar
    assert "no template" in ds.source_bar(dict(f, source=None), root, None)


# ── the rule ──────────────────────────────────────────────────────────────────────

def _registry(seen=("Add a pet:new", "Edit a pet:new", "Book a visit:new", "Add a vet:new",
                    "Add a vet:old", "Edit a visit:old")):
    return {"components": [{"ds": "combo", "roles": ["select"], "tags": ["app-combo"],
                            "seen_on": list(seen)}],
            "roles": [{"role": "select", "covered_by": ["combo"]}]}


def test_the_rule_names_the_component_and_how_widely_it_is_already_used():
    f = {"verdict": "bare", "element": {"tag": "select"}, "expected_ds": ["combo"]}
    rule = ds.rule_html(f, _registry(), "Edit a visit")
    assert rule == ("A native <code>&lt;select&gt;</code> where the design system offers "
                    "<code>&lt;app-combo&gt;</code> (<b>combo</b>, used on 4 other screens): "
                    "use the component so styling, keyboard behaviour and validation stay "
                    "consistent.")


def test_a_component_used_nowhere_else_is_named_without_a_count():
    f = {"verdict": "bare", "element": {"tag": "select"}, "expected_ds": ["combo"]}
    rule = ds.rule_html(f, _registry(seen=("Edit a visit:new",)), "Edit a visit")
    assert "(<b>combo</b>):" in rule and "used on" not in rule


def test_a_kit_control_and_an_outside_one_get_their_own_sentence():
    kit = {"verdict": "bare", "element": {"tag": "mat-select", "kit": "mat-select"},
           "expected_ds": ["combo"]}
    assert ds.rule_html(kit, _registry(), "s").startswith(
        "An Angular Material select (<code>&lt;mat-select&gt;</code>) where the design "
        "system offers <code>&lt;app-combo&gt;</code>")
    foreign = {"verdict": "foreign", "role": "paginator",
               "element": {"tag": "mat-paginator", "kit": "mat-paginator"}}
    rule = ds.rule_html(foreign, _registry(), "s")
    assert rule.startswith("An Angular Material paginator (<code>&lt;mat-paginator&gt;"
                           "</code>) is a control from outside the design system")
    assert "no component for a <code>paginator</code>" in rule


def test_the_registry_knows_the_tag_a_template_writes_for_each_component():
    snaps = {"Book a visit:new": {"nodes": [
        {"sig": "h", "tag": "app-combo", "ds": "combo", "selector": "h", "native": False},
        {"sig": "h>s", "tag": "select", "ds_host": "combo", "ds_host_sig": "h",
         "native": True, "role": "select", "id": "vet", "name": None}]}}
    combo, = ds.derive_registry(snaps, [])["components"]
    assert combo["tags"] == ["app-combo"]


# ── on the page ───────────────────────────────────────────────────────────────────

def _fixture_result():
    snaps = {}
    for stem in ("book-a-visit", "clinic-settings"):
        for side in ("new", "old"):
            snaps[f"{stem}:{side}"] = json.loads((CAPTURE / f"{stem}.{side}.dom.json")
                                                 .read_text())
    reg = ds.derive_registry(snaps, [])
    new_snap, old_snap = snaps["book-a-visit:new"], snaps["book-a-visit:old"]
    dom = ds.dom_delta(old_snap, new_snap)
    elements, _ = ds.combine(old_snap, new_snap, dom, CAPTURE / "book-a-visit.old.png",
                             CAPTURE / "book-a-visit.new.png", 0.1)
    sides = {s: {"label": s, "url": "", "commit": "", "png": f"{s}.png",
                 "page": {"w": 1200, "h": 620}, "viewport": {"w": 1200, "h": 620}}
             for s in ("new", "old")}
    screen = ds.build_screen("Book a visit", old_snap, new_snap, reg, sides_meta=sides,
                             delta={"dom": dom, "elements": elements})
    return ds.build_result([screen], reg)


def test_the_gap_carries_its_markup_and_its_rule_in_the_json():
    """The JSON is the artefact: an agent reading it gets the same three answers."""
    result = _fixture_result()
    gap, = [f for f in result["screens"][0]["findings"] if f["verdict"] == "bare"]
    assert gap["snippet"]["from"] == "rendered"
    assert gap["snippet"]["html"].startswith(
        '<select id="vetId" name="vetId" class="form-control">')
    assert "<code>&lt;div&gt;</code> (<b>combo</b>" in gap["rule"], \
        "the fixture's combo host is a <div data-ds>, and the rule says so"
    assert all("snippet" not in f for f in result["screens"][0]["findings"]
               if f["verdict"] in ("internal", "uncovered")), "nothing to open them from"


def test_the_gap_s_row_opens_onto_its_markup_highlighted_like_every_other_snippet():
    frag = ds.render(_fixture_result(), "")
    more = re.findall(r'<tr class="dsa-more (bad|ok)" data-find="[^"]+"><td></td>'
                      r'<td colspan="6">(.*?)</td></tr>', frag, re.S)
    assert [cls for cls, _ in more].count("bad") == 1
    bad, = [body for cls, body in more if cls == "bad"]
    assert bad.startswith('<details class="dsa-why bad" open><summary>'), "a gap opens"
    assert "use the component so styling" in bad
    assert '<pre class="code lang-html"><code><span class="p">&lt;</span>' \
           '<span class="nt">select</span>' in bad, "Pygments tokens, the page's classes"
    oks = [body for cls, body in more if cls == "ok"]
    assert oks and all(b.startswith('<details class="dsa-why ok"><summary>') for b in oks), \
        "a component that is right stays shut"


def test_one_number_per_verdict_on_the_picture_and_on_its_row():
    """No frames on the fixture: every drawn verdict is a mark of its own, numbered on the
    mark and at the head of its row with the same number."""
    result = _fixture_result()
    frag = ds.render(result, "")
    marks = dict(re.findall(r'data-find="([^"]+)" data-tip="[^"]*"><b>[^<]*</b>'
                            r'<b class="dsa-mnum">(\d+)</b>', frag))
    rows = dict(re.findall(r'<tr class="(?:ok|bad)" data-find="([^"]+)"><td><b class='
                           r'"dsa-rnum [^"]+" data-tip="[^"]+">(\d+)</b>', frag))
    assert marks and marks == {html_id: n for html_id, n in rows.items() if html_id in marks}
    assert set(rows) == set(marks)
    # The same field is the same number on both sides.
    sc = result["screens"][0]
    nums, numbered = ds.number_findings(sc["findings"], {})
    by_field = {}
    for f in sc["findings"]:
        if f["id"] in nums:
            by_field.setdefault(f["element"]["label"], set()).add(nums[f["id"]][0])
    assert numbered and len(by_field) == 3, by_field      # Pet type, Clinic, Vet
    assert all(len(v) == 1 for v in by_field.values()), by_field


def test_a_verdict_inside_a_frame_takes_the_frame_s_number():
    findings = [
        {"id": "new:a", "side": "new", "verdict": "bare", "element": {"id": "a", "sig": "a"},
         "box": {"x": 10, "y": 10, "w": 10, "h": 10}},
        {"id": "new:b", "side": "new", "verdict": "ds", "element": {"id": "b", "sig": "b"},
         "box": {"x": 10, "y": 300, "w": 10, "h": 10}},
        {"id": "old:b", "side": "old", "verdict": "bare", "element": {"id": "b", "sig": "b"},
         "box": {"x": 10, "y": 280, "w": 10, "h": 10}}]
    frames = {"new": [{"x": 0, "y": 0, "w": 50, "h": 50, "insert": False},
                      {"x": 0, "y": 100, "w": 50, "h": 50, "insert": False}],
              "old": [{"x": 0, "y": 0, "w": 50, "h": 0, "insert": True},
                      {"x": 0, "y": 100, "w": 50, "h": 50, "insert": False}]}
    nums, numbered = ds.number_findings(findings, frames)
    assert nums == {"new:a": (1, True), "new:b": (3, False), "old:b": (3, False)}
    assert numbered
    # A frame keeps its place in the list on both sides: Old's only box is still `2`.
    old = ds.shot_html("o.png", {"w": 100, "h": 400}, [], frames["old"], numbered=True)
    assert '<b class="dsa-fnum">2</b>' in old and '<b class="dsa-fnum">1</b>' not in old
    lone, single = ds.number_findings(findings[:1], {"new": frames["new"][:1], "old": []})
    assert lone == {"new:a": (1, True)} and not single, "one thing on the picture: no key"


def test_a_mark_opens_its_row_from_the_picture():
    js = ds.HL_JS
    assert "closest('.dsa-mark[data-find]')" in js and "more.open = true" in js
    assert "document.addEventListener('click'" in js
    css = ds.CSS
    assert ".dsa-mark > b.dsa-mnum" in css and ".dsa-rnum.framed" in css


def test_an_old_json_still_renders_its_details(tmp_path):
    """`--rerender` over a JSON written before any of this: the markup is rebuilt from
    the identity, the rule is worked out from the registry the JSON carries."""
    result = _fixture_result()
    old = copy.deepcopy(result)
    for f in old["screens"][0]["findings"]:
        for k in ("snippet", "rule", "source"):
            f.pop(k, None)
    frag = ds.render(old, "")
    assert ">rebuilt</span>" in frag
    assert "use the component so styling" in frag
