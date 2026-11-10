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

import copy
import importlib.util
import json
import html
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
    quote. It is a candidate.

    Run 6 (hr-claude-6): it used to come back `uncovered` — "role combobox, not covered" —
    beside a registry whose combo covers `select`. A select-only combobox *is* a select,
    so it is judged as one: a gap where combo belongs, said in words."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    widget = control("mat", "mat-select", "role=combobox", id="spec", aria_role="combobox",
                     kit="mat-select")
    f, = ds.audit_side(snap(widget), reg, "new")
    assert f["verdict"] == "bare" and f["role"] == "select" and f["expected_ds"] == ["combo"]
    assert "Angular Material select" in f["message"] and "<b>combo</b>" in f["message"]
    # A text input wearing role=combobox (an autocomplete) is not a select.
    auto = control("ac", "input", "role=combobox", id="q", aria_role="combobox")
    assert ds.audit_side(snap(auto), reg, "new")[0]["verdict"] == "uncovered"


def test_the_widget_roles_are_read_off_aria_not_guessed_from_the_tag():
    assert "ARIA_WIDGET" in ds.SNAPSHOT_JS
    assert "'combobox', 'listbox'" in ds.SNAPSHOT_JS


def test_what_was_let_past_is_shown_rather_than_left_to_trust():
    """"Nothing was flagged" is not a claim anyone can check."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h")])
    screen = ds.build_screen(
        "Edit a vet",
        snap(control("sw", "div", "role=switch", id="spec", selector="#spec",
                     aria_role="switch")),
        snap(control("sw", "div", "role=switch", id="spec", selector="#spec",
                     aria_role="switch")),
        reg, sides_meta={s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")},
        delta={"dom": {"added": [], "removed": [], "changed": [], "moved": {}},
               "elements": {}})
    frag = ds.render(ds.build_result([screen], reg), "")
    assert "considered and deliberately not judged" in frag
    assert "#spec" in frag


def test_an_empty_registry_is_not_a_clean_bill_of_health():
    """No `data-ds` anywhere means no role is claimed, so nothing can be a gap — an empty
    registry must not read as a native control silently passing an audit. The "roles the
    design system covers" list used to say so in prose on the page; the fragment no
    longer shows the registry at all, but the JSON payload still carries it as `[]`,
    and the finding itself still comes back `uncovered`, never `bare`."""
    reg = registry_of([control("bare", id="vetId")])
    assert reg["components"] == [] and reg["roles"] == []
    assert ds.audit_side(snap(control("bare", id="vetId")), reg, "new")[0]["verdict"] \
        == "uncovered"
    empty = {"components": [], "roles": []}
    result = _result_from_capture(registry=empty)
    assert result["summary"]["new"]["bare"] == 0
    frag = ds.render(result, "")
    blob = re.search(r'<script type="application/json" class="ds-audit-data">(.*?)</script>',
                     frag, re.S).group(1)
    assert json.loads(blob.replace("<\\/", "</"))["registry"] == empty


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
    # The verdict in words, not left to be inferred from the evidence for it.
    assert bare["message"].startswith("not the design-system component")
    assert "where <b>combo</b> belongs" in bare["message"]


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
    assert "const rawIdOf = (el) => el.getAttribute('id')" in ds.SNAPSHOT_JS


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


def test_the_header_says_what_the_branch_is_charged_with_and_names_it_on_hover():
    """ "1 regression" read as "1 regression = ?". The count says whose gap it is, and the
    hover names the control, its screen, and that it was a component on the base."""
    reg = registry_of([node("h", "div", ds="combo"),
                       control("h>s", ds_host="combo", ds_host_sig="h", id="vetId")])
    old = snap(node("h", "div", ds="combo"),
               control("h>s", ds_host="combo", ds_host_sig="h", id="vetId"))
    new = snap(control("plain", id="vetId"))
    sides = {s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")}
    screen = ds.build_screen("Edit visit", old, new, reg, sides_meta=sides,
                             delta={"dom": {"added": [], "removed": [], "changed": [],
                                            "moved": {}}, "elements": {}})
    frag = ds.render(ds.build_result([screen], reg), "")
    hdr = frag[frag.index('<p class="dsa-hdr">'):]
    hdr = hdr[:hdr.index("</p>")]
    assert "regression" not in hdr
    # Said once, as the signed delta it is — not "1 gap · 1 introduced by this branch".
    assert "introduced by this branch" not in hdr
    assert '<span class="dsa-gap" data-tip-html=' in hdr
    assert "\u26a0 +1 gap" in hdr
    tip = ds.regression_tip(ds.build_result([screen], reg))
    assert "Edit visit: &lt;select" in tip and "a DS component on the base" in tip


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


# ── the screens the branch never touched ──────────────────────────────────────────

def _untouched(screen):
    """The same screen with nothing changed on it — what a run gets back for every form
    a one-control-per-form migration did not reach."""
    sc = copy.deepcopy(screen)
    sc["delta"]["dom"] = {"added": [], "removed": [], "changed": [], "moved": {}}
    sc["delta"]["elements"] = {k: dict(v, dom="same", status="same", pixel_churn=0.0)
                               for k, v in sc["delta"]["elements"].items()}
    sc["summary"]["regressions"] = []
    sc["summary"]["improvements"] = []
    return sc


def test_a_screen_the_branch_moved_is_the_one_drawn():
    """Every screen row folds shut on load, the way the Sequence tab's own rows do — a
    run audits the whole catalogue, and the summary line (icon, name, verdict) is the
    only thing that has to be read before deciding which ones to open."""
    result = _both_screens()
    assert [ds.screen_touched(sc) for sc in result["screens"]] == [True, True]
    frag = ds.render(result, "")
    assert frag.count('<details class="dsa-screen" id="dsa-') == 2
    # The screen rows, that is. A gap's own detail inside a screen opens with it (it is
    # the product); the screen around it still starts shut.
    assert not re.search(r'<details class="dsa-screen"[^>]*\bopen\b', frag), \
        "collapsed by default, like the Sequence tab's rows"
    assert "2 of 2 screens changed" in frag
    assert "in place" not in frag


def _counts(new_ds, old_ds, reg=0, fixed=0, pre=0):
    return {"new": {"ds": new_ds}, "old": {"ds": old_ds}, "regressions": ["r"] * reg,
            "improvements": ["i"] * fixed, "pre_existing": ["p"] * pre}


def test_only_what_the_branch_changed_is_said_as_signed_deltas():
    """"0 gaps · 1 component (+1 on this branch)" was a count of the screen with the
    delta in brackets after it. The delta is the whole message; a zero is left out."""
    txt = lambda c, **kw: re.sub(r"<[^>]+>", "", " \u00b7 ".join(ds.delta_parts(c, **kw)))
    assert txt(_counts(4, 3)) == "+1 component"
    assert txt(_counts(1, 1)) == ""
    assert txt(_counts(0, 2)) == "\u22122 components"
    assert txt(_counts(1, 1, reg=2)) == "+2 gaps"
    assert txt(_counts(2, 1, fixed=1)) == "+1 component \u00b7 \u22121 gap"
    assert txt(_counts(1, 1, pre=1)) == "1 gap already on the base"
    assert txt(_counts(4, 3, reg=1), long=True) == (
        "\u26a0 +1 gap"
        " \u00b7 +1 design-system component")
    gap, = ds.delta_parts(_counts(1, 1, reg=1))
    assert gap.startswith('<span class="dsa-gap"'), "the one warning, in the warning's yellow"


def test_the_component_tip_says_it_counts_uses_and_over_which_screens():
    """The header said `3 → 4 design-system components`, a screen `0 → 1`: both count uses
    (two combos on a screen are 2), over different scopes, and neither said either."""
    comp, = ds.delta_parts(_counts(4, 3), long=True)
    assert 'data-tip="Design-system component uses, all screens: 3 → 4"' in comp
    comp, = ds.delta_parts(_counts(1, 0))
    assert 'data-tip="Design-system component uses, this screen: 0 → 1"' in comp


def test_a_changed_screen_with_nothing_to_judge_says_so_and_folds():
    """A list that grew a column: the DOM changed, so the screen is on the page, but no
    control on it is in a role the design system covers. `0 gaps · 0 components` used to
    read as a verdict of its own; so did the ✅ that replaced it, which reads "audited and
    fine" on a screen nothing was audited on. It wears a neutral mark and says why, in the
    same muted tail a judged screen's deltas use, folded shut like every row."""
    reg, screen = _screen_from_capture()
    sc = copy.deepcopy(screen)
    sc["summary"]["new"] = dict(sc["summary"]["new"], bare=0, ds=0)
    sc["summary"]["old"] = dict(sc["summary"]["old"], bare=0, ds=0)
    sc["summary"]["regressions"] = []
    sc["delta"]["dom"]["added"] = ["x"] * 3
    assert ds.screen_has_nothing_to_judge(sc)
    frag = ds.render(ds.build_result([sc], reg), "")
    row = frag[frag.index('<details class="dsa-screen"'):]
    summary = row[:row.index("</summary>")]
    assert " open>" not in row.split("</summary>", 1)[0]
    assert "⚪" in summary and "✅" not in summary and "⚠" not in summary
    assert "all controls from the design system" not in summary, "said nothing; gone"
    tail = summary[summary.index('<span class="dsa-sumtail">'):]
    assert re.sub("<[^>]+>", "", tail).strip() == "\u00b7 nothing to judge"
    assert "elements" not in summary and "no control the design system covers" not in summary
    assert "0 gaps" not in summary


def test_a_screen_nothing_happened_on_is_named_and_not_drawn():
    """Every screen of the app is audited, and one was touched: the others would be three
    full-page screenshots each of a picture that did not change, so they get no row and
    no viewer at all. The roll-up count above still says how many of the catalogue
    changed — the per-screen listing of the ones that did not is gone."""
    reg, screen = _screen_from_capture()
    sc = _untouched(screen)
    assert ds.screen_touched(sc) is False
    frag = ds.render(ds.build_result([sc], reg), "")
    assert 'class="dsa-screen"' not in frag
    assert "0 of 1 screens changed" in frag


def test_a_changed_routed_component_no_screen_reaches_is_red_and_first():
    """The one finding the audit cannot make on its own: the DOM diff picks the changed
    screens, but only among the screens it was given. The runner works out which routed
    components the branch changed; one the catalogue does not reach is the first thing on
    the page, in red, naming the route to add."""
    result = _both_screens()
    result["unlisted"] = [{"component": "VisitEditComponent", "route": "visits/:id/edit"},
                          {"component": "PetListComponent", "route": "pets",
                           "via": "<app-visit-list>"}]
    frag = ds.render(result, "")
    first = frag.index('<p class="dsa-unlisted">')
    assert first < frag.index('<p class="dsa-hdr">')
    assert "Not audited" in frag
    assert "<code>visits/:id/edit</code> (<code>VisitEditComponent</code>)" in frag
    assert "via <code>&lt;app-visit-list&gt;</code>" in frag
    assert "steps.dsaudit.screens" in frag


def test_an_element_that_only_moved_does_not_reopen_the_screen():
    _, screen = _screen_from_capture()
    sc = _untouched(screen)
    sc["delta"]["dom"]["moved"] = {"a": "b"}
    assert ds.screen_touched(sc) is False


def test_a_drawn_screen_states_its_gaps_above_the_fold():
    """Folding a screen hides the pictures; hiding the verdict would be hiding a finding.
    The icon and the deltas live in the `<summary>` itself, on the name's own line —
    they used to be two more lines under it plus a second `pictures and findings` fold."""
    reg, screen = _screen_from_capture()
    frag = ds.render(ds.build_result([screen], reg), "")
    row = frag[frag.index('<details class="dsa-screen"'):]
    summary = row[row.index("<summary>") : row.index("</summary>")]
    assert "⚠" in summary
    # The branch added the bare select: a delta, in the warning chip. The two components
    # it did not touch are no delta, so they are not mentioned.
    assert '<span class="dsa-gap"' in summary and ">+1 gap</span>" in summary
    assert "component" not in re.sub(r"<[^>]+>", "", summary)
    assert "pictures and findings" not in frag


def test_the_route_rides_in_the_summary_next_to_the_name():
    """`build_screen`'s `route` kwarg is the same catalogue value the screen's name came
    from (`--screen NAME=PATH`, or the URL a `--new`/`--old` pair names). Parenthesised
    right after the Title-Cased name, the way a route reads in the code that serves it."""
    reg, screen = _screen_from_capture()
    with_route = copy.deepcopy(screen)
    with_route["route"] = "/pets/11/visits/add"
    frag = ds.render(ds.build_result([with_route], reg), "")
    row = frag[frag.index('<details class="dsa-screen"'):]
    summary = row[row.index("<summary>") : row.index("</summary>")]
    assert "Book A Visit" in summary
    assert '<span class="dsa-route">(/pets/11/visits/add)</span>' in summary


def test_an_unknown_route_omits_the_parenthesis():
    """`--from-capture` and a bare `--new`/`--old` URL sometimes carry no path at all —
    the screen still gets a row, just with nothing in parentheses after its name."""
    reg, screen = _screen_from_capture()
    assert screen["route"] is None
    frag = ds.render(ds.build_result([screen], reg), "")
    row = frag[frag.index('<details class="dsa-screen"'):]
    summary = row[row.index("<summary>") : row.index("</summary>")]
    assert "dsa-route" not in summary
    assert "()" not in summary


def test_build_screen_carries_the_route_it_was_given():
    reg, screen = _screen_from_capture()
    new_snap = json.loads((CAPTURE / "book-a-visit.new.dom.json").read_text())
    old_snap = json.loads((CAPTURE / "book-a-visit.old.dom.json").read_text())
    dom = ds.dom_delta(old_snap, new_snap)
    elements, _ = ds.combine(old_snap, new_snap, dom, CAPTURE / "book-a-visit.old.png",
                             CAPTURE / "book-a-visit.new.png", 0.1)
    sc = ds.build_screen(SCREEN, old_snap, new_snap, reg, sides_meta=screen["sides"],
                         delta={"dom": dom, "elements": elements}, route="/pets/11/visits/add")
    assert sc["route"] == "/pets/11/visits/add"
    # The default, when nobody passes one, stays `None` — not `""` — so `render_screen`'s
    # `if route` reads it the same way a falsy route from `_route_of` does.
    assert ds.build_screen(SCREEN, old_snap, new_snap, reg, sides_meta=screen["sides"],
                           delta={"dom": dom, "elements": elements})["route"] is None


def test_route_of_the_url():
    assert ds._route_of("http://localhost:4300/pets/11/visits/add") == "/pets/11/visits/add"
    assert ds._route_of("http://localhost:4300") == ""
    assert ds._route_of("http://localhost:4300/") == ""


def test_the_screen_name_reads_as_title_case_in_the_summary():
    reg, screen = _screen_from_capture()
    assert screen["screen"] == "Book a visit"
    frag = ds.render(ds.build_result([screen], reg), "")
    row = frag[frag.index('<details class="dsa-screen"'):]
    summary = row[row.index("<summary>") : row.index("</summary>")]
    assert "Book A Visit" in summary


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
    assert "build.dgm_views_html(panes" in source
    assert 'class="dgmviews"' not in source, "emitting the control by hand is the drift"
    frag = ds.render(_result_from_capture(), "")
    assert frag.count("<button") == 2
    for view in ("diff", "new", "old"):
        assert f'data-view="{view}"' in frag


def test_the_annotated_new_screen_opens_and_the_delta_is_one_click_away():
    """The audit’s delta is a pixel mask over a photo of a form: half-ghosted, and a
    picture the reader has to decode before it says anything. The annotated new screen
    is the one that reads at a glance, so that is what opening the screen shows. Diff
    keeps its button — it is still the answer to "what moved".
    """
    frag = ds.render(_result_from_capture(), "")
    assert 'data-state="new"' in frag
    assert '<div class="dgmpane" data-view="new">' in frag
    assert '<div class="dgmpane" data-view="diff" hidden>' in frag
    assert '<div class="dgmpane" data-view="old" hidden>' in frag


def test_the_badge_names_the_verdict_and_not_an_arrow_between_two_words():
    """"select → combo" needed the reader to already know what it meant. The red box
    on the screenshot has to say what it is claiming: this is not the component."""
    nodes = [node("h", "div", ds="combo"), control("h>s", ds_host="combo", ds_host_sig="h"),
             control("loose", id="vetId")]
    findings = ds.audit_side(snap(*nodes), registry_of(nodes), "new")
    bad, = [m for m in ds._marks_for(findings, "new") if m["cls"] == "bad"]
    assert bad["badge"] == "✗ existing plain <select> — should be the combo component"
    assert "→" not in bad["badge"]


def test_the_tip_shows_a_tag_and_not_the_letters_of_its_escape():
    """The message is HTML, the tip is text. Strip the tags and the entities are still
    there; escape that into the attribute and the bubble reads "&lt;select&gt;" out
    loud — which is what it did until someone hovered one."""
    nodes = [node("h", "div", ds="combo"), control("h>s", ds_host="combo", ds_host_sig="h"),
             control("loose", id="vetId")]
    findings = ds.audit_side(snap(*nodes), registry_of(nodes), "new")
    bad, = [m for m in ds._marks_for(findings, "new") if m["cls"] == "bad"]
    assert "<select>" in bad["tip"]
    assert "&lt;" not in bad["tip"] and "&amp;" not in bad["tip"]
    assert "<code>" not in bad["tip"], "tags stripped, entities resolved"


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
    badge = re.search(r'class="dsa-mark bad"[^>]*><b>(.*?)</b>', frag).group(1)
    assert "select" in badge and "combo" in badge
    # The fixture's combo is mounted on a <div>: named by its marker, on the chip's green end.
    assert '<i class="dsa-want">&lt;div data-ds=&quot;combo&quot;&gt;</i>' in badge


def _app_combo_gap():
    """A rendered <app-combo> teaches the registry its tag; a plain select beside it is
    the gap — the Edit a Visit screen's Vet field."""
    nodes = [node("h", "app-combo", ds="combo", name="petType"),
             control("h>s", ds_host="combo", ds_host_sig="h"),
             control("select#vet", id="vet", name="vetId", label="Vet")]
    reg = registry_of(nodes)
    findings = [dict(f, delta={"status": "added"})
                for f in ds.audit_side(snap(*nodes), reg, "new")]
    return findings, reg


def test_a_gap_tip_shows_the_found_tag_against_the_tag_that_should_be():
    """Victor, 20 Oct 2026: "what should have appeared in the HTML? put a tag". The bubble
    is two tags, the one there (red, struck) and the design system's own (green), with
    the field's name on both so they differ in the tag alone."""
    findings, reg = _app_combo_gap()
    bad, = [m for m in ds._marks_for(findings, "new", reg) if m["cls"] == "bad"]
    tip = bad["tip_html"]
    assert '<span class="dsa-vsk bad">\u2717 found</span>' in tip
    assert '<code class="bad">&lt;select name=&quot;vetId&quot;&gt;</code>' in tip
    assert '<span class="dsa-vsk good">\u2713 should be</span>' in tip
    assert '<code class="good">&lt;app-combo name=&quot;vetId&quot;&gt;</code>' in tip
    assert "the design system\u2019s combo belongs" in tip
    assert bad["badge"] == "\u2717 new plain <select> \u2014 should be <app-combo>"
    assert bad["badge_html"].endswith('<i class="dsa-want">&lt;app-combo&gt;</i>')


def test_the_mark_carries_the_pair_as_markup_for_the_bubble():
    findings, reg = _app_combo_gap()
    shot = ds.shot_html("x.png", {"w": 1200, "h": 800},
                        ds._marks_for(findings, "new", reg))
    bad = re.search(r'<div class="dsa-mark bad"[^>]*>', shot).group(0)
    tip = html.unescape(re.search(r'data-tip-html="([^"]*)"', bad).group(1))
    assert '<code class="good">&lt;app-combo name=&quot;vetId&quot;&gt;</code>' in tip
    assert "data-tip=" not in bad, "the markup tip replaces the plain one"


def test_a_component_tip_shows_its_own_tag_in_green():
    findings, reg = _app_combo_gap()
    ok, = [m for m in ds._marks_for(findings, "new", reg) if m["cls"] == "ok"]
    assert '<code class="good">&lt;app-combo name=&quot;petType&quot;&gt;</code>' in ok["tip_html"]
    assert "bad" not in ok["tip_html"]


def test_a_foreign_tip_says_there_is_no_tag_to_want():
    """A kit control with no design-system counterpart: the found tag, and in the
    should-be row the fact that the design system has none — not an invented tag."""
    pag = node("mat-paginator", "mat-paginator", role="paginator", kit="mat-paginator",
               label="Select page")
    f, = ds.audit_side(snap(pag), _combo_registry(), "new")
    assert f["verdict"] == "foreign"
    m, = ds._marks_for([f], "new", _combo_registry())
    assert '<code class="bad">&lt;mat-paginator&gt;</code>' in m["tip_html"]
    assert "nothing \u2014 the design system has no paginator component" in m["tip_html"]
    assert "An Angular Material paginator, from outside the design system." in m["tip_html"]


def test_the_bubble_pair_uses_fixed_hues_for_the_always_dark_bubble():
    """tip.js draws the bubble near-black in both schemes; a --dsa-* token flips to the
    light palette's dark red on it and goes unreadable in light mode."""
    rules = re.findall(r"\.tip \.dsa-v[^{]*\{([^}]*)\}", ds.CSS)
    assert rules and not any("var(--dsa" in r for r in rules)


def test_the_rows_put_the_gaps_first():
    frag = ds.render(_result_from_capture(), "")
    order = re.findall(r'<tr class="(ok|bad)"', frag)
    assert order[0] == "bad" and set(order[1:]) == {"ok"}


# ── conventions of the family ─────────────────────────────────────────────────────

def test_it_prints_its_stylesheet_and_exits_like_its_siblings():
    out = subprocess.run([sys.executable, str(HERE / "ds-audit.py"), "--css"],
                         capture_output=True, text=True, check=True).stdout
    assert ".dsa-mark" in out and "<" not in out.split("*/")[1]


@pytest.mark.parametrize("spelling", ["assets", "assets/"])
def test_the_asset_prefix_is_a_folder_however_it_is_spelled(tmp_path, spelling):
    """`run-steps.py` once passed `--asset-prefix assets`, the way its `--assets` flag is
    written, and every image on the UX tab came out as `assetsds-audit-…png`. Both
    spellings are the same folder; neither may produce a broken picture."""
    out = tmp_path / "ds-audit.html"
    subprocess.run([sys.executable, str(HERE / "ds-audit.py"), "--from-capture", str(CAPTURE),
                    "--assets", str(tmp_path / "assets"), "--asset-prefix", spelling,
                    "--json", str(tmp_path / "ds-audit.json"), "-o", str(out)],
                   capture_output=True, text=True, check=True)
    srcs = re.findall(r'<img[^>]*\bsrc="([^"]+)"', out.read_text())
    assert srcs and all(s.startswith("assets/ds-audit-") for s in srcs), srcs
    assert not any("assetsds-audit" in s for s in srcs)


def test_an_empty_prefix_keeps_the_pictures_beside_the_page():
    assert ds.asset_prefix("") == ""
    assert ds.asset_prefix("assets") == "assets/" == ds.asset_prefix("assets//")


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


# ── framing the changes ───────────────────────────────────────────────────────────

def _page(tmp_path, name, blocks, h=400, w=600):
    """A flat grey page with dark rectangles on it: (x, y, w, h) each."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), (240, 240, 240))
    d = ImageDraw.Draw(im)
    for x, y, bw, bh in blocks:
        d.rectangle((x, y, x + bw - 1, y + bh - 1), fill=(40, 40, 40))
    path = tmp_path / name
    im.save(path)
    return path


def test_an_inserted_row_is_one_frame_and_what_slid_down_is_not_framed(tmp_path):
    """A field inserted mid-form pushes everything under it down. Rows are aligned the
    way `diff` aligns lines, so the buttons that only moved match, and the frame wraps
    the new field alone; on the base its twin is a dashed line where it went in."""
    old = _page(tmp_path, "o.png", [(50, 50, 300, 20), (50, 120, 80, 30)])
    new = _page(tmp_path, "n.png", [(50, 50, 300, 20), (50, 100, 400, 24), (50, 160, 80, 30)])
    fr = ds.change_frames(old, new)
    assert len(fr["new"]) == len(fr["old"]) == 1
    n, o = fr["new"][0], fr["old"][0]
    assert not n["insert"] and o["insert"]
    assert n["y"] <= 100 and n["y"] + n["h"] >= 124 and n["y"] + n["h"] < 160
    assert n["x"] <= 50 and n["x"] + n["w"] >= 450


def test_two_changes_far_apart_get_two_frames_not_one_around_the_screen(tmp_path):
    old = _page(tmp_path, "o.png", [(40, 40, 100, 20), (400, 330, 100, 20)])
    new = _page(tmp_path, "n.png", [(40, 40, 140, 20), (400, 330, 150, 20)])
    fr = ds.change_frames(old, new)
    assert len(fr["new"]) == 2
    top, bottom = sorted(fr["new"], key=lambda f: f["y"])
    assert top["y"] + top["h"] < 100 and bottom["y"] > 300


def test_changes_close_together_share_one_frame(tmp_path):
    old = _page(tmp_path, "o.png", [])
    new = _page(tmp_path, "n.png", [(40, 100, 60, 20), (140, 100, 60, 20), (40, 140, 60, 20)])
    assert len(ds.change_frames(old, new)["new"]) == 1


def test_identical_screens_have_no_frames(tmp_path):
    a = _page(tmp_path, "a.png", [(10, 10, 50, 50)])
    assert ds.change_frames(a, a) == {"new": [], "old": []}


def test_frames_are_drawn_on_every_view_behind_a_checkbox_that_starts_checked():
    reg, screen = _screen_from_capture()
    screen = copy.deepcopy(screen)
    screen["frames"] = {"new": [{"x": 10, "y": 20, "w": 30, "h": 40, "insert": False}],
                        "old": [{"x": 10, "y": 20, "w": 30, "h": 0, "insert": True}]}
    frag = ds.render(ds.build_result([screen], reg), "")
    assert frag.count('class="dsa-frame"') == 2      # the Diff pane and New
    assert frag.count('class="dsa-frame insert"') == 1
    bar = frag[frag.index('<div class="dgmbar">'):]
    bar = bar[:bar.index('<div class="dgmpane"')]
    assert 'class="dsa-frameon" checked' in bar and "frame the changes" in bar
    assert ".dsa:has(.dsa-frameon:not(:checked)) .dsa-frame" in ds.CSS


def test_a_screen_without_frames_gets_no_checkbox():
    reg, screen = _screen_from_capture()
    frag = ds.render(ds.build_result([screen], reg), "")
    assert "dsa-frameon" not in frag.replace("'.dsa-frameon'", "").replace(
        "contains('dsa-frameon')", "")


# ── run 6 (hr-claude-6): a paginated, sortable owners grid built from Angular Material ──
#
# The tab said "1 of 19 screens changed", green, `clean` — while ds-audit.json's own
# `uncovered` went 41 → 42. The one new control, the paginator's page-size select, was
# listed as "#mat-select-0 — role combobox, not covered", and the paginator and matSort
# were never judged at all. Below, the same screen in miniature.

def _combo_registry():
    return registry_of([node("h", "div", ds="combo"),
                        control("h>s", ds_host="combo", ds_host_sig="h")])


def _owners_grid():
    """The new side of /owners: a table with matSort on two headers, a mat-paginator with
    its page-size mat-select inside a mat-form-field, and the last-name search input."""
    return snap(
        control("input#lastName", "input", "input[type=text]", id="lastName",
                name="lastName", label="Last name"),
        node("t", "table", kit="mat-sort", box={"x": 60, "y": 250, "w": 1160, "h": 440}),
        node("t>th:1", "th", kit="mat-sort-header", label="Name", kit_hosts=["mat-sort"]),
        node("t>th:3", "th", kit="mat-sort-header", label="City", kit_hosts=["mat-sort"]),
        node("p", "mat-paginator", kit="mat-paginator", label="Select page of owners",
             box={"x": 60, "y": 715, "w": 1160, "h": 56}),
        control("p>f>mat-select", "mat-select", "role=combobox", id="mat-select-0",
                aria_role="combobox", kit="mat-select", label="Items per page:",
                kit_hosts=["mat-form-field", "mat-paginator"],
                box={"x": 816, "y": 731, "w": 52, "h": 25}))


def _owners_screen():
    reg = _combo_registry()
    old = snap(control("input#lastName", "input", "input[type=text]", id="lastName",
                       name="lastName", label="Last name"),
               node("t0", "table"))
    sides = {"new": {"label": "hr-claude-6", "commit": "", "page": {"w": 1280, "h": 900}},
             "old": {"label": "5a97353e", "commit": "", "page": {"w": 1280, "h": 900}}}
    added = {"dom": "added", "pixel_churn": None, "status": "added"}
    screen = ds.build_screen(
        "Owners", old, _owners_grid(), reg, sides_meta=sides,
        delta={"dom": {"added": ["t", "p", "p>f>mat-select"], "removed": ["t0"],
                       "changed": [], "moved": {}},
               "elements": {"t": added, "p": added, "p>f>mat-select": added}},
        route="/owners")
    return reg, screen


def test_the_new_material_controls_are_judged_not_waved_through():
    """mat-paginator and matSort are not design-system components and the registry has
    none for their role: each is a gap, with the reason. The page-size select is the
    paginator's own machinery — not a second gap — and the paginator's reason names it:
    a mat-select where the registry's combo covers `select`."""
    reg, screen = _owners_screen()
    by_kit = {f["element"].get("kit"): f for f in screen["findings"] if f["side"] == "new"}
    paginator, sort, select = by_kit["mat-paginator"], by_kit["mat-sort"], by_kit["mat-select"]
    assert paginator["verdict"] == sort["verdict"] == "foreign"
    assert select["verdict"] == "internal"
    assert "Angular Material paginator" in paginator["message"]
    assert "“Items per page” control is a <code>&lt;mat-select&gt;</code>, not " \
           "<b>combo</b>" in paginator["message"]
    assert sort["element"]["label"] == "matSort on Name, City"
    assert "no component for a <code>sortable table header</code>" in sort["message"]
    # The rule under the row states the fact, like a `bare` one — no advice paragraph
    # ("either the design system gets one, or the team agrees…") that read model-written.
    assert ds.rule_html(paginator, reg, "Owners") == (
        "An Angular Material paginator (<code>&lt;mat-paginator&gt;</code>); the design "
        "system has no <code>paginator</code> component.")
    assert paginator["history"] == "brought in by this branch"
    assert sorted(screen["summary"]["regressions"]) == sorted([paginator["id"], sort["id"]])
    assert screen["summary"]["new"]["foreign"] == 2
    result = ds.build_result([screen], reg)
    assert result["verdict"] == "gaps", "two new outside controls are not a clean audit"


def test_a_lone_material_select_is_the_gap_where_combo_belongs():
    """Outside a kit control it is judged on its own: a select-only combobox fills the role
    combo covers, so it is `bare`, named as the Material control it is — not "plain"."""
    sel = control("s", "mat-select", "role=combobox", id="mat-select-3", kit="mat-select",
                  aria_role="combobox", label="Vet")
    f, = ds.audit_side(snap(sel), _combo_registry(), "new")
    assert f["verdict"] == "bare" and f["expected_ds"] == ["combo"]
    mark, = ds._marks_for([dict(f, delta={"status": "added"})], "new")
    assert mark["badge"] == "✗ new <mat-select> — should be the combo component"


def test_a_kit_multi_select_the_base_already_had_is_context_not_a_gap():
    """The vet form's specialties picker is a Material multi-select, left there on
    purpose: a multi-select is not the combo's `select`, and the base renders it too. It
    stays considered-and-let-past, never charged to a branch that did not touch it."""
    spec = control("mat-select#spec", "mat-select", "role=combobox", id="spec",
                   kit="mat-select", aria_role="combobox", label="Specialties",
                   cls=["mat-mdc-select", "mat-mdc-select-multiple"])
    reg = _combo_registry()
    f, = ds.audit_side(snap(spec), reg, "new")
    assert f["role"] == "select[multiple]" and f["verdict"] == "foreign"
    sides = {s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")}
    screen = ds.build_screen("Edit a vet", snap(spec), snap(spec), reg, sides_meta=sides,
                             delta={"dom": {"added": [], "removed": [], "changed": [],
                                            "moved": {}}, "elements": {}})
    assert {f["verdict"] for f in screen["findings"]} == {"uncovered"}
    assert screen["summary"]["regressions"] == [] and screen["summary"]["pre_existing"] == []


def test_an_element_is_named_by_its_accessible_name_never_a_kit_counter():
    """"#mat-select-0" is Material's counter, not a name. The page-side extractor reads
    the name the control carries (`aria-labelledby` → "Items per page:") and refuses an
    auto id as identity; the row and the considered list say the name."""
    f = {"element": {"tag": "mat-select", "id": "mat-select-0", "label": "Items per page:",
                     "kit": "mat-select"}}
    assert ds.element_name(f) == "Items per page"
    f["element"]["label"] = ""
    assert ds.element_name(f) == "Angular Material select"
    assert ds.element_name({"element": {"tag": "input", "id": "lastName"}}) == "lastName"
    assert "aria-labelledby" in ds.SNAPSHOT_JS and "!el.contains(l)" in ds.SNAPSHOT_JS
    assert "AUTO_ID.test(i) ? '' : i" in ds.SNAPSHOT_JS
    # Angular's per-build animation namespace is not a difference between two builds.
    assert "tns-[\\w-]+" in ds.SNAPSHOT_JS
    reg = _combo_registry()
    lone = control("s", "div", "role=switch", id="mat-slide-toggle-2", aria_role="switch",
                   selector="#mat-slide-toggle-2", label="Notify me")
    sides = {s: {"label": s, "page": {"w": 10, "h": 10}} for s in ("new", "old")}
    screen = ds.build_screen("s", snap(lone), snap(lone), reg, sides_meta=sides,
                             delta={"dom": {"added": [], "removed": [], "changed": ["s"],
                                            "moved": {}}, "elements": {}})
    frag = ds.render(ds.build_result([screen], reg), "")
    considered = frag[frag.index('class="dsa-considered"'):]
    considered = considered[:considered.index("</details>")]
    assert "<b>Notify me</b>" in considered and "#mat-slide-toggle-2" not in considered


def test_the_header_says_the_gaps_and_components_and_never_just_a_screen_count():
    """"1 of 19 screens changed" alone could not be told from an audit that judged nothing.
    The gaps are said in the warning chip, and a move in what was let past is said too."""
    reg, screen = _owners_screen()
    frag = ds.render(ds.build_result([screen], reg), "")
    hdr = re.sub(r"<[^>]+>", "", frag[frag.index('<p class="dsa-hdr">'):].split("</p>")[0])
    assert hdr == ("1 of 1 screens changed · ⚠ +2 gaps")
    txt = lambda c, **kw: re.sub(r"<[^>]+>", "", " · ".join(ds.delta_parts(c, **kw)))
    moved = {"new": {"ds": 3, "uncovered": 42}, "old": {"ds": 3, "uncovered": 41},
             "regressions": [], "improvements": [], "pre_existing": []}
    assert txt(moved, long=True) == "+1 control no design-system component claims"
    assert txt(moved) == "+1 not judged"
    # Nothing moved for the design system: said, not left blank.
    _, quiet = _owners_screen()
    for f in quiet["findings"]:
        f["verdict"] = "uncovered"
    quiet["summary"] = dict(quiet["summary"], regressions=[],
                            new={"ds": 0, "bare": 0, "foreign": 0, "uncovered": 1},
                            old={"ds": 0, "bare": 0, "foreign": 0, "uncovered": 1})
    hdr = ds.render(ds.build_result([quiet], reg), "")
    assert "no gap and no design-system component added or removed" in hdr


def test_a_changed_screen_always_gets_its_findings_table():
    """side / element / role / why / delta / churn, with the gap rows — and on a screen
    with nothing to judge, a row saying so instead of no table at all."""
    reg, screen = _owners_screen()
    frag = ds.render(ds.build_result([screen], reg), "")
    table = frag[frag.index('<table class="dsa-table">'):frag.index("</table>")]
    for th in ("side", "element", "role", "why", "delta", "churn"):
        assert f"<th>{th}</th>" in table
    assert table.count('<tr class="bad"') == 2 and table.count(">gap</span>") == 2
    assert "<b>Select page of owners</b>" in table and "<b>matSort on Name, City</b>" in table
    _, quiet = _owners_screen()
    quiet["findings"] = [f for f in quiet["findings"] if f["verdict"] == "uncovered"]
    frag = ds.render(ds.build_result([quiet], reg), "")
    assert "nothing to judge here" in frag[frag.index('<table class="dsa-table">'):]


def test_the_change_frame_is_labelled_and_its_colour_is_in_the_legend():
    """A purple box with no words and no key: the frame now carries a chip naming what
    was judged inside it, and the legend carries the frame's colour."""
    reg, screen = _owners_screen()
    screen["frames"] = {"new": [{"x": 52, "y": 240, "w": 1176, "h": 540, "insert": False}],
                        "old": [{"x": 52, "y": 240, "w": 1176, "h": 900, "insert": False}]}
    frag = ds.render(ds.build_result([screen], reg), "")
    # Eval run 8: the chip on the frame's edge covered the table header it framed. The
    # words are a caption under the picture now; the frame itself carries none.
    assert '<p class="dsa-framecap"><span class="dsa-fcap"><i></i>' \
           "<b>✗ matSort · ✗ mat-paginator — added</b></span></p>" in frag
    assert "<b>changed — nothing here to judge</b>" in frag     # the Old view
    assert not re.search(r'class="dsa-frame"[^>]*><b', frag), "no chip on the picture"
    assert frag.index('class="dsa-frame"') < frag.index('class="dsa-framecap"')
    assert 'class="k-frame"' in ds.LEGEND and 'class="k-frame"' in ds.DIFF_LEGEND
    assert ".dsa-legend .k-frame i" in ds.CSS and "var(--dsa-frame)" in ds.CSS
    assert "changed on this branch</span>" not in ds.LEGEND, "blue named no box in New/Old"


def test_several_frames_are_numbered_and_the_caption_is_keyed_by_number():
    """Only a frame with a verdict in it is numbered: the number keys a row of the table.
    One with nothing judged in it (Book A Visit's new table header) keeps its caption and
    gets no number — numbered, it read as a finding with its row missing."""
    reg, screen = _owners_screen()
    screen["frames"] = {"new": [{"x": 52, "y": 240, "w": 1176, "h": 300, "insert": False},
                                {"x": 52, "y": 900, "w": 600, "h": 100, "insert": False},
                                {"x": 52, "y": 700, "w": 600, "h": 100, "insert": False}],
                        "old": []}
    frag = ds.render(ds.build_result([screen], reg), "")
    assert '<b class="dsa-fnum">1</b>' in frag and '<b class="dsa-fnum">2</b>' in frag
    assert '<b class="dsa-fnum">3</b>' not in frag
    assert '<span class="dsa-fcap"><i></i>2 <b>' in frag
    assert '<span class="dsa-fcap"><i></i><b>changed — nothing here to judge</b>' in frag
    assert ".dsa:has(.dsa-frameon:not(:checked)) .dsa-framecap" in ds.CSS


def test_the_element_column_wraps_a_selector_only_between_its_steps():
    """Eval run 8: `div#ownersTable>ta / ble.mat-sort.tabl / e:1` — `break-all` cut the
    selector mid-token. It breaks only after a `>` now, and the full path is the tip."""
    reg, screen = _owners_screen()
    frag = ds.render(ds.build_result([screen], reg), "")
    cells = re.findall(r'<code class="dsa-sel" data-tip="([^"]*)">(.*?)</code>', frag)
    assert cells
    for tip, shown in cells:
        assert "<wbr>" not in tip, "the tip is the whole path, as text"
        assert shown.count("<wbr>") == shown.count("&gt;"), shown
    assert ds.selector_html("div#a>mat-x:1") == \
        '<span class="dsa-step">div#a</span>&gt;<wbr><span class="dsa-step">mat-x:1</span>'
    assert ".dsa-sel .dsa-step { white-space: nowrap; }" in ds.CSS, "no break after a hyphen"
    rule = re.search(r"\.dsa-sel \{([^}]*)\}", ds.CSS).group(1)
    assert "break-all" not in rule and "word-break: normal" in rule


def _repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], capture_output=True,
                              text=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "f").write_text("1")
    git("add", "f"); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    (tmp_path / "f").write_text("2")
    git("commit", "-qam", "branch")
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    (tmp_path / "g").write_text("main moved on")
    git("add", "g"); git("commit", "-qm", "main moves")
    git("checkout", "-q", "feature")
    return base, head


def test_each_side_records_its_commit_and_the_old_side_is_labelled_by_it(tmp_path):
    """Run 6: `sides.old.commit` and `sides.new.commit` were `''`, and the old side said
    `main` — neither the review base nor the merge-base. The commit is recorded, and a
    label that does not point at it gives way to it; the branch name over HEAD stays."""
    base, head = _repo(tmp_path)
    assert ds.page_base_commit("main", tmp_path) == base     # forked here, not main's tip
    sides = {"new": {"label": "feature", "commit": ""}, "old": {"label": "main", "commit": ""}}
    ds.stamp_sides(sides, {"new": head, "old": base}, tmp_path)
    assert sides["new"] == {"label": "feature", "commit": head}
    assert sides["old"] == {"label": base[:8], "commit": base}
    # A recorded commit is not overwritten by a later resolution.
    again = ds.stamp_sides({"old": {"label": "x", "commit": base}}, {"old": head}, tmp_path)
    assert again["old"]["commit"] == base


def test_the_count_opens_a_box_that_names_each_side_once():
    reg, screen = _owners_screen()
    screen["sides"]["new"].update(label="abc12345", commit="abc12345" + "0" * 32)
    screen["sides"]["old"].update(label="main", commit="d01c3776" + "0" * 32)
    result = ds.build_result([screen], reg)
    frag = ds.render(result, "")
    box = ds.how_box(result)
    assert 'class="dsa-count" role="button" tabindex="0" aria-expanded="false"' in frag
    assert '<div class="dsa-howbox" id="dsa-howbox" hidden>' in frag
    assert "<code>d01c3776</code>" not in box and "main (d01c3776)" in box
    assert "<code>abc12345</code>" in box and "abc12345 (abc12345)" not in box
    assert 'class="disclose"' in frag
    # As tuned live on the PR #51 page (patch 1340): headings are paragraphs, the open
    # count's underline goes solid, the box has its own air.
    assert "<p><b>How the screens were compared</b></p><ul>" in box
    assert '.dsa-count[aria-expanded="true"] { text-decoration-style: solid; }' in ds.CSS
    assert "padding: .6rem .9rem; margin: .4rem 0 .9rem;" in ds.CSS
    assert ".dsa-howbox ul:last-child" not in ds.CSS, "the last list keeps its .5rem, as tuned"


def test_the_count_says_how_the_screens_it_leaves_out_are_known_unchanged(tmp_path):
    """Victor, 10 Nov 2026, on "5 of 19 screens changed": say the other 14 are pixel-perfect
    identical, or say how it was checked. The proof is a raw count — no YIQ, no erosion, no
    threshold — and the tooltip and the box only make the claim the count supports."""
    from PIL import Image

    a, b, c = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "c.png"
    Image.new("RGB", (40, 30), (250, 250, 250)).save(a)
    Image.new("RGB", (40, 30), (250, 250, 250)).save(b)
    assert ds.pixels_differing(a, b) == 0
    img = Image.new("RGB", (40, 30), (250, 250, 250))
    img.putpixel((3, 4), (251, 250, 250))       # one unit, below any perceptual threshold
    img.save(c)
    assert ds.pixels_differing(a, c) == 1
    Image.new("RGB", (40, 35), (250, 250, 250)).save(c)
    assert ds.pixels_differing(a, c) == 40 * 5, "the area only one side has counts whole"

    reg, screen = _screen_from_capture()
    quiet = [dict(copy.deepcopy(_untouched(screen)), screen=n, pixels_differ=0)
             for n in ("Owners", "Vets")]
    result = ds.build_result(quiet, reg)
    tip = html.unescape(re.search(r'data-tip="([^"]*)"', ds.render(result, "")).group(1))
    assert tip.startswith("The other 2: every pixel identical on base and branch")
    assert "no blur" in tip
    assert "2 pixel-identical: same size, every pixel equal" in ds.how_box(result)

    quiet[1]["pixels_differ"] = 1234
    result = ds.build_result(quiet, reg)
    assert ds.count_tip(result).startswith("The other 2: 1 pixel-identical; Vets differs ")
    assert "Vets: 1,234 pixels differ" in ds.how_box(result)

    del quiet[1]["pixels_differ"]                  # a JSON from before the raw count
    result = ds.build_result(quiet, reg)
    assert ds.count_tip(result) == ds.COUNT_TIP
    assert "The other" not in ds.how_box(result)
