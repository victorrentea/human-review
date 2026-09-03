#!/usr/bin/env python3
"""The design-system audit, pinned on the one thing it is for: the absence.

An auditor that labels the components a page *does* use proves nothing — every screen in
the repo would come back green, including the one that just shipped a bare `<select>`
where the combo belongs. The finding is the control that is missing its component, so
almost everything below is a test that something got flagged, or that something did
*not* get flagged for the wrong reason.

Three properties are worth failing a build over, and they are the three that rot first:

* **the role set is derived, never listed.** A second design-system component must need
  no change to this file. `test_a_second_component_needs_no_new_code` is that promise.
* **an empty registry is not a clean bill of health.** Point it at a page with no
  `data-ds` anywhere and the honest answer is "I know of no component, so I claim
  nothing" — not a green tick.
* **DOM decides, pixels corroborate.** Every element below a newly inserted field moves;
  a differ that reports each of them has told the reader nothing, loudly.

The fixture pair in `testdata/ds-audit/` is a screen before and after a Vet field was
added by copying an older template — the exact defect the auditor exists to catch. The
recorded capture beside it (`testdata/ds-audit/capture/`) keeps these tests hermetic:
no browser, no dev server, no stack. Regenerate it after any change to `SNAPSHOT_JS`:

    ./ds-audit.py --new file://$PWD/testdata/ds-audit/new.html \
                  --old file://$PWD/testdata/ds-audit/old.html \
                  --viewport 1200x620 --assets /tmp/x --asset-prefix "" \
                  --keep-capture testdata/ds-audit/capture \
                  -o /tmp/x/f.html --json /tmp/x/f.json

Run with:  python3 -m pytest test_ds_audit.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CAPTURE = HERE / "testdata" / "ds-audit" / "capture"

_spec = importlib.util.spec_from_file_location("ds_audit", HERE / "ds-audit.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


# ── little snapshots, written by hand ─────────────────────────────────────────────

def node(sig, tag, **kw):
    n = {"sig": sig, "selector": kw.pop("selector", sig), "tag": tag, "id": kw.pop("id", None),
         "name": kw.pop("name", None), "type": kw.pop("type", None), "aria_role": None,
         "cls": [], "ds": None, "ds_covers": None, "ds_host": None, "ds_host_sig": None,
         "native": False, "role": None, "label": "", "disabled": False, "leaf": False,
         "box": {"x": 0, "y": 0, "w": 100, "h": 20}, "text": ""}
    n.update(kw)
    return n


def control(sig, tag="select", role="select", **kw):
    return node(sig, tag, native=True, role=role, **kw)


def snap(*nodes, page=(1200, 800)):
    return {"url": "about:blank", "title": "t", "viewport": {"w": page[0], "h": page[1]},
            "page": {"w": page[0], "h": page[1]}, "nodes": list(nodes)}


def registry_of(new_nodes, old_nodes=(), sources=()):
    return ds.derive_registry({"new": snap(*new_nodes), "old": snap(*old_nodes)},
                              list(sources))


# ── the role model: derived, and honest about where each role came from ───────────

def test_a_component_declares_what_it_covers_by_wrapping_it():
    """The runtime answer, and the one that needs no cooperation: whatever native control
    the rendered host contains is what that component stands in for."""
    reg = registry_of([node("host", "div", ds="combo"),
                       control("host>select", ds_host="combo", ds_host_sig="host")])
    combo, = reg["components"]
    assert combo["roles"] == ["select"]
    assert combo["provenance"] == "runtime:new"


def test_a_second_component_needs_no_new_code():
    """The whole design point. `data-ds="datepicker"` is a name this file has never seen;
    the moment one is rendered around a date input, a loose date input is a gap."""
    reg = registry_of([
        node("d", "div", ds="datepicker"),
        control("d>in", "input", "input[type=date]", type="date",
                ds_host="datepicker", ds_host_sig="d"),
    ])
    assert [r["role"] for r in reg["roles"]] == ["input[type=date]"]

    findings = ds.audit_side(
        snap(node("d", "div", ds="datepicker"),
             control("d>in", "input", "input[type=date]", type="date",
                     ds_host="datepicker", ds_host_sig="d"),
             control("loose", "input", "input[type=date]", type="date", id="when")),
        reg, "new")
    bare = [f for f in findings if f["verdict"] == "bare"]
    assert len(bare) == 1 and bare[0]["element"]["id"] == "when"


def test_the_registry_is_shared_across_both_sides():
    """The base is measured against the *branch's* design system. Otherwise a base with
    no components yet can never be shown as the thing the branch improved on — and,
    worse, a branch that deleted the last combo would audit itself clean."""
    reg = registry_of(new_nodes=[node("h", "div", ds="combo"),
                                 control("h>s", ds_host="combo", ds_host_sig="h")],
                      old_nodes=[control("bare", id="vetId")])
    findings = ds.audit_side(snap(control("bare", id="vetId")), reg, "old")
    assert [f["verdict"] for f in findings] == ["bare"]


def test_a_combo_built_on_an_input_and_a_select_admits_only_the_select():
    """A real combobox wraps a search box as well as the control holding the value. If
    the search box counted, every text field on every screen would light up red for a
    component that has nothing to do with text fields — and the audit would be switched
    off by Friday. A `<select>` is unambiguous about where the value lives, so it wins."""
    reg = registry_of([
        node("h", "div", ds="combo"),
        control("h>in", "input", "input[type=text]", ds_host="combo", ds_host_sig="h"),
        control("h>sel", ds_host="combo", ds_host_sig="h"),
    ])
    assert reg["components"][0]["roles"] == ["select"]


def test_only_one_control_is_admitted_when_there_is_no_select_to_settle_it():
    reg = registry_of([
        node("h", "div", ds="stepper"),
        control("h>a", "input", "input[type=number]", type="number",
                ds_host="stepper", ds_host_sig="h"),
        control("h>b", "input", "input[type=text]", ds_host="stepper", ds_host_sig="h"),
    ])
    assert reg["components"][0]["roles"] == ["input[type=number]"]


def test_the_author_can_say_it_out_loud_and_that_wins():
    reg = registry_of([node("h", "div", ds="combo", ds_covers="select,input[type=search]",
                            selector="#h"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    combo, = reg["components"]
    assert combo["roles"] == ["select", "input[type=search]"]
    assert combo["provenance"] == "declared"


def test_a_component_this_screen_never_renders_is_still_in_the_registry(tmp_path):
    """The datepicker only appears on the edit form. The audit of the add form must still
    know that date inputs are spoken for, or a screen is clean purely by omission."""
    (tmp_path / "picker.component.html").write_text(
        '<div class="pc-datepicker" data-ds="datepicker">'
        '<input type="date" [(ngModel)]="value"></div>')
    reg = registry_of([], sources=[tmp_path])
    picker, = reg["components"]
    assert picker["roles"] == ["input[type=date]"]
    assert picker["provenance"] == "source"
    assert picker["detail"].endswith("picker.component.html")


def test_a_guess_from_the_name_is_taken_but_labelled_a_guess():
    """A `data-ds` host we cannot attribute a role to is better served by a marked guess
    than by silence — but the reader has to be able to see it was a guess."""
    reg = registry_of([node("h", "div", ds="combo")])
    combo, = reg["components"]
    assert combo["roles"] == ["select"] and combo["provenance"] == "lexicon"
    assert "guessed" in combo["detail"]


def test_the_lexicon_never_overrides_what_the_component_actually_wraps():
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", "textarea", "textarea",
                               ds_host="combo", ds_host_sig="h")])
    assert reg["components"][0]["roles"] == ["textarea"]


def test_an_unknown_name_with_no_implementation_claims_nothing():
    reg = registry_of([node("h", "div", ds="carousel")])
    assert reg["components"][0]["roles"] == []
    assert reg["roles"] == []


def test_a_multi_select_is_not_a_single_select_wearing_a_hat():
    """The live guard, and the one that decides whether anybody keeps the audit switched
    on. The vet-edit specialties picker is deliberately native: the design system has a
    combo and no multi-select. Reported as role `select` it comes back red every run for
    a component that cannot replace it, and by the second week the tab is ignored."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    assert [r["role"] for r in reg["roles"]] == ["select"]
    multi = control("many", role="select[multiple]", id="spec")
    verdict, = [f["verdict"] for f in ds.audit_side(snap(multi), reg, "new")]
    assert verdict == "uncovered"


def test_the_page_tells_a_multi_select_from_a_single_one():
    """`el.multiple`, not the presence of the word in the markup, and the same reading in
    the source scan so a component the screen never renders is classified the same way."""
    assert "el.multiple ? 'select[multiple]' : 'select'" in ds.SNAPSHOT_JS


def test_a_component_kit_widget_is_considered_rather_than_invisible(tmp_path):
    """Material renders its picker as a `<mat-select role="combobox">`, not a `<select>`.
    An auditor that only knew tag names would be *silent* about it — not "considered and
    let past", silent, which is the answer a reviewer cannot check and an agent cannot
    quote. It is a candidate, and it comes back `uncovered` with the reason."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    widget = control("mat", "mat-select", "role=combobox", id="spec", aria_role="combobox")
    f, = ds.audit_side(snap(widget), reg, "new")
    assert f["verdict"] == "uncovered"
    assert "No design-system component claims that role" in f["message"]
    assert "<code>select</code>" in f["message"], "it names what the registry does cover"


def test_the_widget_roles_are_read_off_aria_not_guessed_from_the_tag():
    assert "ARIA_WIDGET" in ds.SNAPSHOT_JS
    assert "'combobox', 'listbox'" in ds.SNAPSHOT_JS


def test_what_was_let_past_is_shown_rather_than_left_to_trust():
    """"Nothing was flagged" is not a claim anyone can check."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    screen = ds.build_screen(
        "Edit a vet",
        snap(control("mat", "mat-select", "role=combobox", id="spec",
                     selector="#spec", aria_role="combobox")),
        snap(control("mat", "mat-select", "role=combobox", id="spec",
                     selector="#spec", aria_role="combobox")),
        reg, sides_meta={s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")},
        delta={"dom": {"added": [], "removed": [], "changed": [], "moved": {}},
               "elements": {}})
    frag = ds.render(ds.build_result([screen], reg), "")
    assert "considered and deliberately not judged" in frag
    assert "#spec" in frag


def test_an_empty_registry_is_not_a_clean_bill_of_health():
    """No `data-ds` anywhere means no role is claimed, so nothing can be a gap — and the
    page must say that rather than show a reassuring zero."""
    reg = registry_of([control("bare", id="vetId")])
    assert reg["components"] == [] and reg["roles"] == []
    assert ds.audit_side(snap(control("bare", id="vetId")), reg, "new")[0]["verdict"] \
        == "uncovered"
    empty = {"components": [], "roles": []}
    result = _result_from_capture(registry=empty)
    assert result["summary"]["new"]["bare"] == 0
    frag = ds.render(result, "")
    assert "not a clean bill of health" in frag


# ── the absence is the product ────────────────────────────────────────────────────

def test_the_control_inside_the_component_is_not_a_finding():
    """The `<select>` a combo wraps is the combo's own machinery. Badging it would put a
    red box on the one field that is right, next to the field that is wrong."""
    nodes = [node("h", "div", ds="combo"), control("h>s", ds_host="combo", ds_host_sig="h")]
    verdicts = {f["verdict"] for f in ds.audit_side(snap(*nodes), registry_of(nodes), "new")}
    assert verdicts == {"ds", "internal"}


def test_the_bare_one_beside_it_is():
    nodes = [node("h", "div", ds="combo"), control("h>s", ds_host="combo", ds_host_sig="h"),
             control("loose", id="vetId")]
    findings = ds.audit_side(snap(*nodes), registry_of(nodes), "new")
    bare, = [f for f in findings if f["verdict"] == "bare"]
    assert bare["element"]["id"] == "vetId"
    assert bare["expected_ds"] == ["combo"]
    assert "not inside any" in bare["message"]


def test_a_control_in_a_role_nobody_claims_is_recorded_and_never_drawn():
    """An audit that reports only what it flagged cannot be argued with. The text input
    is in the JSON with the reason it was let past — and it gets no badge."""
    nodes = [node("h", "div", ds="combo"), control("h>s", ds_host="combo", ds_host_sig="h"),
             control("txt", "input", "input[type=text]", id="description")]
    findings = ds.audit_side(snap(*nodes), registry_of(nodes), "new")
    skipped, = [f for f in findings if f["verdict"] == "uncovered"]
    assert skipped["element"]["id"] == "description"
    assert ds._marks_for(findings, "new") == [m for m in ds._marks_for(findings, "new")
                                              if m["cls"] in ("ok", "bad")]
    assert not [m for m in ds._marks_for(findings, "new") if "description" in m["badge"]]


def test_a_disabled_or_hidden_control_never_reaches_python():
    """`type=hidden`, `type=submit` and a zero-sized element are filtered in the page, so
    the whole downstream never has to know about them. Pinning it here because the filter
    is the difference between four badges and forty."""
    js = ds.SNAPSHOT_JS
    assert "'hidden', 'button', 'submit', 'reset', 'image'" in js
    assert "r.width < 1 || r.height < 1" in js
    assert "cs.visibility === 'hidden'" in js


def test_the_form_id_trap():
    """`form.id` is not the id attribute — it is the child control named "id", and the
    petclinic visit form has one. Every signature under that form came out as
    `form#[object HTMLInputElement]` and matched nothing on the other side, so the whole
    screen read as rewritten. Caught on the live app, not in a test."""
    assert not re.search(r"\bel\.id\b(?![^\n]*NEVER)", ds.SNAPSHOT_JS.replace(
        "// NEVER `el.id`.", ""))
    assert "const idOf = (el) => el.getAttribute('id')" in ds.SNAPSHOT_JS


# ── DOM diff: which element, without crying wolf ──────────────────────────────────

def test_an_element_that_only_moved_is_not_a_new_element():
    """Insert a field above an unnamed wrapper and its nth-of-type index shifts. The set
    difference then reports one removed and one added — two lies where the truth was
    "nothing happened here", in the exact place the reader is meant to be looking."""
    old = snap(node("row:1>div", "div", text="Clinic"))
    new = snap(node("row:2>div", "div", text="Clinic"))
    d = ds.dom_delta(old, new)
    assert d["added"] == [] and d["removed"] == []
    assert d["moved"] == {"row:1>div": "row:2>div"}


def test_an_anonymous_element_whose_words_changed_is_honestly_a_new_one():
    """Nothing identifies it but its text. Pairing it with the one that used to be there
    would be a guess, and a wrong pairing hides a real change."""
    d = ds.dom_delta(snap(node("r:1>b", "b", text="Vet")),
                     snap(node("r:2>b", "b", text="Attending vet")))
    assert d["moved"] == {} and d["added"] == ["r:2>b"]


def test_an_ambiguous_move_is_left_alone():
    """Two identical cells that both shifted cannot be paired without guessing which went
    where. A wrong pairing is worse than an honest added/removed."""
    old = snap(node("a:1", "td", text="dog"), node("b:1", "td", text="dog"))
    new = snap(node("a:2", "td", text="dog"), node("b:2", "td", text="dog"))
    d = ds.dom_delta(old, new)
    assert d["moved"] == {} and len(d["added"]) == 2 and len(d["removed"]) == 2


def test_a_moved_element_that_also_changed_is_reported_as_changed():
    """A named control keeps its identity through a rewritten label: that is a change to
    an element, not a replacement of one. An anonymous `<td>` has only what it says, so
    it does not get the same benefit — see the test below."""
    old = snap(control("r:1>s", name="vetId", label="Vet"))
    new = snap(control("r:2>s", name="vetId", label="Attending vet"))
    d = ds.dom_delta(old, new)
    assert d["changed"] == ["r:2>s"] and d["added"] == []


def test_the_digest_ignores_where_an_element_sits():
    """Position is the signature's job. If the digest read the box too, every element
    below an insertion would be "changed" and the DOM diff would be a pixel diff with
    extra steps."""
    a = node("s", "div", text="x")
    b = dict(a, box={"x": 9, "y": 400, "w": 3, "h": 3})
    assert ds._digest(a) == ds._digest(b)


def test_angular_state_classes_never_reach_the_digest():
    """`ng-touched` flips when the capture happens to focus a field. Two runs of the same
    page would then differ, and the before/after comparison is noise from the first row."""
    assert "ng-(untouched|touched|pristine|dirty|valid|invalid" in ds.SNAPSHOT_JS


# ── pixels: corroboration, cropped on the element ─────────────────────────────────

def _png(path, blocks, size=(200, 120), bg=(255, 255, 255)):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    for (x, y, w, h, colour) in blocks:
        d.rectangle([x, y, x + w, y + h], fill=colour)
    im.save(path)
    return path


def test_an_element_that_only_moved_costs_nothing(tmp_path):
    """The claim the whole comparison rests on. Each side is cropped on *its own* box, so
    a field pushed 40px down by a paragraph above it is compared against itself."""
    old = _png(tmp_path / "o.png", [(20, 10, 60, 20, (30, 90, 180))])
    new = _png(tmp_path / "n.png", [(20, 50, 60, 20, (30, 90, 180))])
    box_o = {"x": 20, "y": 10, "w": 61, "h": 21}
    box_n = {"x": 20, "y": 50, "w": 61, "h": 21}
    assert ds.registered_churn(old, new, box_o, box_n) == 0.0
    # …and in absolute page coordinates it would have read as a total repaint.
    mask = ds.pixel_delta(old, new, None)
    assert ds.churn(mask, box_o) > 0.9


def test_a_repaint_in_place_is_seen(tmp_path):
    old = _png(tmp_path / "o.png", [(20, 10, 60, 20, (30, 90, 180))])
    new = _png(tmp_path / "n.png", [(20, 10, 60, 20, (200, 40, 40))])
    box = {"x": 20, "y": 10, "w": 61, "h": 21}
    assert ds.registered_churn(old, new, box, box) > 0.9


def test_a_box_that_changed_size_is_charged_for_the_part_that_has_no_counterpart(tmp_path):
    """Cropping only compares the overlap. Without charging the rest, a field that grew
    to twice its height would come back "identical"."""
    old = _png(tmp_path / "o.png", [(0, 0, 40, 20, (0, 0, 0))])
    new = _png(tmp_path / "n.png", [(0, 0, 40, 20, (0, 0, 0))])
    c = ds.registered_churn(old, new, {"x": 0, "y": 0, "w": 41, "h": 21},
                            {"x": 0, "y": 0, "w": 41, "h": 42})
    assert c == pytest.approx(0.5, abs=0.02)


def test_a_one_pixel_fringe_is_eroded_and_a_solid_block_is_not(tmp_path):
    """Text rendering leaves a hairline along every glyph when anything reflows. Reported
    honestly, it is a page of magenta; pixelmatch spends real effort detecting it, and the
    cheap half of that — a differing pixel needs differing neighbours — buys most of it."""
    import numpy as np
    a = np.full((40, 40, 3), 255, dtype="uint8")
    fringe = a.copy()
    fringe[20, :] = 0                        # a one-pixel line
    assert not ds._yiq_mask(a, fringe).any()
    block = a.copy()
    block[10:20, 10:20] = 0                  # a solid 10×10
    assert ds._yiq_mask(a, block).sum() > 50


def test_a_hue_shift_below_the_threshold_is_not_a_change(tmp_path):
    import numpy as np
    a = np.full((20, 20, 3), 200, dtype="uint8")
    b = np.full((20, 20, 3), 202, dtype="uint8")
    assert not ds._yiq_mask(a, b).any()


# ── the weighting, and it is not a blend ──────────────────────────────────────────

def test_the_dom_decides_membership_and_the_pixels_do_not_get_a_vote(tmp_path):
    """An element the DOM says is new is highlighted whatever the pixels say — it has no
    counterpart to compare against, so a churn number for it would be invented."""
    png = _png(tmp_path / "x.png", [])
    old = snap(node("keep", "div", text="a", leaf=True))
    new = snap(node("keep", "div", text="a", leaf=True), control("added", id="vetId"))
    dom = ds.dom_delta(old, new)
    status, _ = ds.combine(old, new, dom, png, png, 0.1)
    assert status["added"] == {"dom": "added", "pixel_churn": None, "status": "added"}


def test_pixels_get_the_one_vote_the_dom_cannot_cast(tmp_path):
    """Same signature, same attributes, different picture — a control the branch restyled.
    The DOM is blind to it and it is exactly the "looks almost right" defect this report
    is about, so the pixels are allowed to name it."""
    old = _png(tmp_path / "o.png", [(0, 0, 60, 20, (255, 255, 255))])
    new = _png(tmp_path / "n.png", [(0, 0, 60, 20, (10, 10, 10))])
    box = {"x": 0, "y": 0, "w": 61, "h": 21}
    a = snap(control("s", id="vetId", box=box))
    b = snap(control("s", id="vetId", box=box))
    status, _ = ds.combine(a, b, ds.dom_delta(a, b), old, new, 0.1)
    assert status["s"]["dom"] == "same" and status["s"]["status"] == "restyled"
    assert status["s"]["pixel_churn"] > ds.RESTYLE_CHURN


def test_a_faint_difference_does_not_reach_the_restyle_threshold(tmp_path):
    old = _png(tmp_path / "o.png", [(0, 0, 60, 20, (255, 255, 255))])
    new = _png(tmp_path / "n.png", [(0, 0, 3, 3, (0, 0, 0))])
    box = {"x": 0, "y": 0, "w": 61, "h": 21}
    a = snap(control("s", box=box))
    status, _ = ds.combine(a, a, ds.dom_delta(a, a), old, new, 0.1)
    assert status["s"]["status"] == "same"


def test_what_only_moved_is_subtracted_from_the_delta_picture(tmp_path):
    """The picture gets the same treatment as the findings. A raw whole-page diff of a
    form with one field inserted is magenta to the footer; that is one change reported a
    hundred times, and the eye cannot find the real one inside it."""
    box = {"x": 0, "y": 0, "w": 21, "h": 21}
    png = _png(tmp_path / "p.png", [(0, 0, 20, 20, (0, 0, 0))])
    a = snap(node("s", "div", leaf=True, text="x", box=box))
    _, explained = ds.combine(a, a, ds.dom_delta(a, a), png, png, 0.1)
    assert explained == [box]


def test_the_weighting_is_written_down_where_the_agent_reading_the_json_finds_it():
    result = _result_from_capture()
    assert "DOM decides" in result["summary"]["weighting"]


# ── the fixture pair: the deliberate <select>, end to end ─────────────────────────

SCREEN = "Book a visit"


def _screen_from_capture(registry=None):
    new_snap = json.loads((CAPTURE / "book-a-visit.new.dom.json").read_text())
    old_snap = json.loads((CAPTURE / "book-a-visit.old.dom.json").read_text())
    reg = registry if registry is not None else ds.derive_registry(
        {"new": new_snap, "old": old_snap}, [])
    dom = ds.dom_delta(old_snap, new_snap)
    elements, _ = ds.combine(old_snap, new_snap, dom, CAPTURE / "book-a-visit.old.png",
                             CAPTURE / "book-a-visit.new.png", 0.1)
    sides = {s: {"label": s, "url": "", "commit": "", "png": f"{s}.png",
                 "page": {"w": 1200, "h": 620}, "viewport": {"w": 1200, "h": 620}}
             for s in ("new", "old")}
    return reg, ds.build_screen(SCREEN, old_snap, new_snap, reg, sides_meta=sides,
                                delta={"dom": dom, "elements": elements})


def _settings_screen(registry):
    """The second fixture screen: a select the branch *migrated* into the combo."""
    new_snap = json.loads((CAPTURE / "clinic-settings.new.dom.json").read_text())
    old_snap = json.loads((CAPTURE / "clinic-settings.old.dom.json").read_text())
    dom = ds.dom_delta(old_snap, new_snap)
    elements, _ = ds.combine(old_snap, new_snap, dom, CAPTURE / "clinic-settings.old.png",
                             CAPTURE / "clinic-settings.new.png", 0.1)
    sides = {s: {"label": s, "url": "", "commit": "", "png": f"{s}.png",
                 "page": {"w": 1200, "h": 520}, "viewport": {"w": 1200, "h": 520}}
             for s in ("new", "old")}
    return ds.build_screen("Clinic settings", old_snap, new_snap, registry,
                           sides_meta=sides, delta={"dom": dom, "elements": elements})


def _both_screens():
    """Both fixture screens through one registry, the way a run audits them."""
    snaps = {}
    for stem in ("book-a-visit", "clinic-settings"):
        for side in ("new", "old"):
            snaps[f"{stem}:{side}"] = json.loads(
                (CAPTURE / f"{stem}.{side}.dom.json").read_text())
    reg = ds.derive_registry(snaps, [])
    _, first = _screen_from_capture(reg)
    return ds.build_result([first, _settings_screen(reg)], reg)


def _result_from_capture(registry=None):
    reg, screen = _screen_from_capture(registry)
    return ds.build_result([screen], reg)


def _findings():
    return _result_from_capture()["screens"][0]["findings"]


def test_the_auditor_catches_the_bare_select_the_branch_shipped():
    """The one that matters. The fixture branch adds a Vet field by copying an older
    template: a native `<select>`, styled to the same height and radius as the combo
    beside it, in a role the combo covers. Two combos on the same screen are correct and
    prove nothing; this is the finding."""
    result = _result_from_capture()
    gaps = [f for f in _findings() if f["verdict"] == "bare"]
    assert len(gaps) == 1, [f["selector"] for f in gaps]
    gap, = gaps
    assert gap["selector"] == "#vetId" and gap["element"]["label"] == "Vet"
    assert gap["role"] == "select" and gap["expected_ds"] == ["combo"]
    assert result["verdict"] == "gaps"


def test_it_is_ranked_above_the_components_that_are_right():
    result = _result_from_capture()
    gap, = [f for f in _findings() if f["verdict"] == "bare"]
    assert gap["severity"] == "high"
    assert gap["id"] in result["summary"]["regressions"]
    assert "never migrated" in gap["history"]


def test_the_gap_carries_what_a_branch_did_to_it():
    """A bare control the base already had is a different conversation from one this
    branch shipped. Both are worth fixing; only one is the reviewer's to block on."""
    gap, = [f for f in _findings() if f["verdict"] == "bare"]
    assert gap["delta"]["dom"] == "added"


def test_the_registry_derived_from_the_fixture_is_runtime_not_guessed():
    result = _result_from_capture()
    combo, = result["registry"]["components"]
    assert combo["ds"] == "combo" and combo["roles"] == ["select"]
    assert combo["provenance"].startswith("runtime:")


def test_the_two_correct_combos_come_back_green_on_both_sides():
    result = _result_from_capture()
    assert result["summary"]["new"]["ds"] == 2 and result["summary"]["old"]["ds"] == 2
    assert result["summary"]["old"]["bare"] == 0


def test_inserting_a_field_does_not_rewrite_the_component_below_it():
    """The clinic combo sits under the new Vet row on the branch. Nothing about it
    changed, and the whole point of anchoring a `data-ds` host on the control it wraps is
    that it does not come back as one component removed and another added."""
    result = _result_from_capture()
    clinic = [f for f in _findings()
              if f["side"] == "new" and "clinic" in f["selector"]]
    assert clinic and clinic[0]["delta"]["status"] == "same"
    assert result["screens"][0]["delta"]["dom"]["removed"] == []


# ── several screens, one registry, one verdict ────────────────────────────────────

def test_it_flags_only_the_right_one_across_the_screens_a_migration_touched():
    """The acceptance test, and it is a much stronger claim than "it flagged a select".
    A migration lands one control per form, so the run covers every form it touched: the
    migrated ones must come back green and exactly one must come back red."""
    result = _both_screens()
    gaps = [f for sc in result["screens"] for f in sc["findings"]
            if f["verdict"] == "bare" and f["side"] == "new"]
    assert [f["selector"] for f in gaps] == ["#vetId"]
    assert result["summary"]["new"]["ds"] == 3
    assert result["verdict"] == "gaps"


def test_one_registry_serves_every_screen():
    """A component is a component whichever form renders it. Deriving per screen would
    make a form that happens to render no combo audit itself clean by omission."""
    result = _both_screens()
    assert len(result["registry"]["components"]) == 1
    assert [sc["screen"] for sc in result["screens"]] == ["Book a visit", "Clinic settings"]


def test_a_migration_reads_as_an_improvement_and_not_as_another_green_box():
    """The base had a bare `<select id="timezone">`; the branch wrapped it. Matched on
    signature the two are unrelated elements — the wrapper is new — and the branch gets no
    credit for the fix. They are paired on the *field* instead."""
    result = _both_screens()
    assert len(result["summary"]["improvements"]) == 1
    settings = result["screens"][1]
    assert settings["summary"]["improvements"] == result["summary"]["improvements"]
    assert settings["summary"]["bare" if False else "new"]["bare"] == 0


def test_the_same_pairing_catches_a_component_a_branch_tore_out():
    """The mirror image, and the one a reviewer must not miss: the base had it right and
    the branch replaced it with a bare control. It is a `high`, not a pre-existing gap."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h", id="vetId")])
    old = snap(node("h", "div", ds="combo"),
               control("h>s", ds_host="combo", ds_host_sig="h", id="vetId"))
    new = snap(control("plain", id="vetId"))
    sides = {s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")}
    screen = ds.build_screen("s", old, new, reg, sides_meta=sides,
                             delta={"dom": {"added": [], "removed": [], "changed": [],
                                            "moved": {}}, "elements": {}})
    gap, = [f for f in screen["findings"] if f["verdict"] == "bare"]
    assert gap["severity"] == "high"
    assert "this branch replaced it" in gap["history"]
    assert screen["summary"]["regressions"] == [gap["id"]]


def test_a_gap_the_base_already_had_is_not_charged_to_the_branch():
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    both = snap(control("plain", id="vetId"))
    sides = {s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")}
    screen = ds.build_screen("s", both, both, reg, sides_meta=sides,
                             delta={"dom": {"added": [], "removed": [], "changed": [],
                                            "moved": {}}, "elements": {}})
    gap, = [f for f in screen["findings"] if f["side"] == "new" and f["verdict"] == "bare"]
    assert gap["severity"] == "medium" and screen["summary"]["regressions"] == []
    assert screen["summary"]["pre_existing"] == [gap["id"]]


def test_a_gap_the_branch_closed_reads_as_closed_and_not_as_one_more_to_do():
    """The base's bare select is a real gap and it is also already fixed. A plain red row
    for it sits beside the one that is not fixed and costs that one its urgency."""
    result = _both_screens()
    old_row, = [f for f in result["screens"][1]["findings"]
                if f["side"] == "old" and f["verdict"] == "bare"]
    assert old_row["resolved"] is True and old_row["severity"] == "info"
    frag = ds.render(result, "")
    assert ">fixed</span>" in frag
    assert frag.count('<tr class="bad"') == 1, "only the unfixed one is red"


def test_each_screen_gets_its_own_viewer_and_its_own_pictures():
    frag = ds.render(_both_screens(), "assets/")
    assert frag.count('class="dgmviews"') == 2
    for stem in ("book-a-visit", "clinic-settings"):
        for side in ("new", "old", "delta"):
            assert f'assets/ds-audit-{stem}-{side}.png' in frag


def test_the_registry_is_stated_once_for_the_whole_run():
    frag = ds.render(_both_screens(), "")
    assert frag.count("Roles the design system covers") == 1


def test_a_capture_remembers_what_the_screens_were_called():
    """Rebuilt from filenames alone, "Book a visit" comes back as "book-a-visit" and every
    heading in the report is a filename."""
    names = json.loads((CAPTURE / "screens.json").read_text())
    assert names == ["Book a visit", "Clinic settings"]
    for name in names:
        assert (CAPTURE / f"{ds.slug(name)}.new.dom.json").is_file()


# ── the artefact the other consumer reads ─────────────────────────────────────────

def test_the_json_is_the_primary_artefact_and_needs_no_ocr():
    """Two consumers, and only one of them has eyes. Everything on the picture has to be
    answerable from the JSON: which element, what role, what verdict, where on the shot."""
    result = _result_from_capture()
    gap, = [f for f in _findings() if f["verdict"] == "bare"]
    assert set(gap) >= {"id", "side", "verdict", "role", "expected_ds", "element",
                        "selector", "box", "message", "severity", "history", "delta"}
    assert set(gap["box"]) == {"x", "y", "w", "h"}
    assert result["schema"] == "ds-audit/1"
    assert json.loads(json.dumps(result)) == result


def test_the_pins_are_declared_rather_than_promised():
    result = _result_from_capture()
    assert len(result["determinism"]["pins"]) >= 6
    assert any("clock" in p for p in result["determinism"]["pins"])
    assert any("byte-identical" in p for p in result["determinism"]["pins"])


def test_the_fragment_carries_the_same_json_for_an_agent_reading_the_page():
    frag = ds.render(_result_from_capture(), "")
    blob = re.search(r'<script type="application/json" class="ds-audit-data">(.*?)</script>',
                     frag, re.S).group(1)
    assert json.loads(blob.replace("<\\/", "</"))["schema"] == "ds-audit/1"


def test_nothing_in_the_payload_can_close_the_script_tag():
    """A selector or a label containing `</script>` would end the block early and spill
    the rest of the JSON into the page as markup."""
    result = _result_from_capture()
    result["screens"][0]["findings"][0]["message"] = "</script><b>oops</b>"
    frag = ds.render(result, "")
    assert "</script><b>oops</b>" not in frag.split('class="ds-audit-data">')[1]


# ── the picture, and the viewer it reuses ─────────────────────────────────────────

def test_the_three_state_viewer_is_the_page_s_own_and_not_a_second_one():
    """A second viewer with different ergonomics in the same report would be the mistake.
    The Diff / New-Old control was built for exactly this shape of content."""
    source = (HERE / "ds-audit.py").read_text()
    assert "build.dgm_views_html(panes)" in source
    assert 'class="dgmviews"' not in source, "emitting the control by hand is the drift"
    frag = ds.render(_result_from_capture(), "")
    assert frag.count("<button") == 2
    for view in ("diff", "new", "old"):
        assert f'data-view="{view}"' in frag


def test_the_delta_opens_and_the_two_annotated_shots_are_behind_the_second_button():
    frag = ds.render(_result_from_capture(), "")
    assert 'data-state="diff"' in frag
    assert '<div class="dgmpane" data-view="new" hidden>' in frag
    assert '<div class="dgmpane" data-view="old" hidden>' in frag


def test_only_the_two_verdicts_worth_drawing_are_drawn():
    """A page covered in badges is unreadable, and the value is in the few that matter."""
    frag = ds.render(_result_from_capture(), "")
    assert frag.count('class="dsa-mark ok') == 4      # two combos, on both sides
    assert frag.count('class="dsa-mark bad') == 1     # the one that matters
    assert "petType" not in re.findall(r'class="dsa-mark[^>]*>(<b>[^<]*)', frag).__str__()


def test_the_overlay_is_positioned_in_percentages_so_the_picture_can_scale():
    """The report is read on a laptop and on a projector. Pixel offsets would put every
    badge off its field the moment the image is not shown at its native width."""
    frag = ds.render(_result_from_capture(), "")
    style = re.search(r'class="dsa-mark bad" style="([^"]+)"', frag).group(1)
    assert style.count("%") == 4 and "px" not in style


def test_a_badge_names_the_component_that_should_have_been_used():
    frag = ds.render(_result_from_capture(), "")
    badge = re.search(r'class="dsa-mark bad"[^>]*><b>([^<]+)</b>', frag).group(1)
    assert "select" in badge and "combo" in badge


def test_the_rows_put_the_gaps_first():
    frag = ds.render(_result_from_capture(), "")
    order = re.findall(r'<tr class="(ok|bad)"', frag)
    assert order[0] == "bad" and set(order[1:]) == {"ok"}


# ── conventions of the family ─────────────────────────────────────────────────────

def test_it_prints_its_stylesheet_and_exits_like_its_siblings():
    out = subprocess.run([sys.executable, str(HERE / "ds-audit.py"), "--css"],
                         capture_output=True, text=True, check=True).stdout
    assert ".dsa-mark" in out and "<" not in out.split("*/")[1]


def test_every_colour_the_fragment_uses_is_defined_for_both_themes():
    tokens = set(re.findall(r"var\(--dsa-([\w-]+)\)", ds.CSS))
    light = dict(re.findall(r"--dsa-([\w-]+):\s*([^;]+);", ds.CSS.split("prefers-color")[0]))
    dark = dict(re.findall(r"--dsa-([\w-]+):\s*([^;]+);", ds.CSS.split("prefers-color")[1]))
    assert tokens <= set(light), tokens - set(light)
    assert tokens <= set(dark), tokens - set(dark)
    assert light["ok"] != light["bad"] and dark["ok"] != dark["bad"]


def test_the_fragment_is_a_fragment():
    """It is pasted into a section body of the review page, not served on its own."""
    frag = ds.render(_result_from_capture(), "")
    assert not re.search(r"<(!doctype|html|head|body)\b", frag, re.I)


def test_the_stylesheet_restyles_nothing_the_page_owns():
    """`.dgmviews`, `.dgmpane` and `.diagram` belong to the report. A fragment that
    reaches into them is a second implementation of the viewer wearing a hat."""
    for owned in (".dgmviews", ".dgmpane", ".dgmbar", ".diagram"):
        assert owned not in ds.CSS


def test_the_behaviour_is_delegated_off_document_like_the_rest_of_the_page():
    assert "document.addEventListener" in ds.HL_JS
    assert "querySelectorAll('.dsa-table tr" not in ds.HL_JS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
