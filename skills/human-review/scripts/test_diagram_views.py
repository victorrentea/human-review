#!/usr/bin/env python3
"""The Diff / New-Old control: three states, two buttons, one implementation.

It exists because the delta is not always right. A generated sequence diagram is built
from traces whose call *order* is not stable between runs, so `seq_puml_diff.py` reports
messages as moved that nobody moved. Rather than pretend that away, the page gives the
reader a one-click escape to the undiffed before/after and lets them judge.

Two things are worth pinning here, and they are the two that would rot quietly:

* **one component, two callers.** The generated PlantUML deltas and the hand-drawn
  conceptual model both go through `dgm_views_html`, and the behaviour is delegated off
  `document` so neither needs to register anything. A second copy of this in a section
  body would drift within a week and nothing would notice.
* **the header toggles, the body never does.** A sequence diagram is taller than the
  viewport; a stray click while scrolling or selecting text must not swap the picture.

Run with:  python3 -m pytest test_diagram_views.py
"""
from __future__ import annotations

import importlib.util
import html
import json
import re
import subprocess
from pathlib import Path

from conftest import page_source

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

# The page builder is a package now (`hrbuild/`), and `build-review-html.py` re-exports
# every name in it so the rest of the skill still finds them here. A *patch* is the one
# thing a re-export cannot carry: rebinding `build.X` leaves the module that defines `X`
# calling the original. So the handful of tests below that replace a function reach for
# the module that owns it.
diagrams = importlib.import_module("hrbuild.shared.diagrams")

PANES = [("diff", "<i>D</i>"), ("new", "<i>N</i>"), ("old", "<i>O</i>")]


# ── the markup ────────────────────────────────────────────────────────────────────

def test_three_states_come_off_two_buttons():
    out = build.dgm_views_html(PANES)
    assert out.count("<button") == 2
    assert 'data-go="diff"' in out and 'data-go="newold"' in out
    # both words live in the second button, each tagged with the state it selects
    pair = re.search(r'class="dgm-newold".*?</button>', out, re.S).group(0)
    assert '<u data-view="new">New</u>' in pair and '<u data-view="old">Old</u>' in pair


def test_the_word_is_old_not_original():
    """The user's word. "Original" is a different, longer claim about the same file."""
    assert ">Old</u>" in build.dgm_views_html(PANES)
    assert "Original" not in build.dgm_views_html(PANES)


def test_the_delta_is_what_opens_and_the_other_two_are_inert():
    out = build.dgm_views_html(PANES)
    assert 'data-state="diff"' in out
    assert '<div class="dgmpane" data-view="diff">' in out
    for view in ("new", "old"):
        assert f'<div class="dgmpane" data-view="{view}" hidden>' in out


def test_the_caller_can_say_which_state_opens():
    """The UX audit opens on New: its delta is a pixel mask over a screenshot, not a
    two-colour drawing, so the annotated shot is the one that reads at a glance. The
    control is the same one — only which pane starts visible changes."""
    out = build.dgm_views_html(PANES, initial="new")
    assert 'data-state="new"' in out
    assert '<div class="dgmpane" data-view="new">' in out
    assert '<div class="dgmpane" data-view="diff" hidden>' in out
    # the button states have to agree with the pane, or the widget opens lying
    assert '<u data-view="new" class="on">New</u>' in out
    assert 'data-go="diff" aria-pressed="false"' in out
    assert 'data-go="newold" aria-pressed="true"' in out


def test_opening_on_a_side_that_never_rendered_falls_back_to_the_delta():
    """A diagram this branch added has no "old" pane. Honouring the request would open
    the widget on nothing at all."""
    out = build.dgm_views_html([("diff", "<i>D</i>"), ("new", "<i>N</i>")], initial="old")
    assert 'data-state="diff"' in out
    assert '<div class="dgmpane" data-view="diff">' in out


def test_a_lone_side_still_gets_a_button_with_one_word():
    """A diagram this branch added has no "old" side, and one this branch deleted has no
    "new" one. Neither is an error; the button simply offers the word that exists."""
    out = build.dgm_views_html([("diff", "<i>D</i>"), ("new", "<i>N</i>")])
    assert '<u data-view="new">New</u>' in out and "data-view=\"old\"" not in out
    assert out.count("<button") == 2


def test_a_diff_with_nothing_to_compare_grows_no_control():
    out = build.dgm_views_html([("diff", "<i>D</i>")])
    assert out == "<i>D</i>", "no bar, no frame, no pane wrapper — just the picture"


# ── wiring to the manifest ────────────────────────────────────────────────────────

def _svg(path: Path, text: str) -> str:
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg"><text>{text}</text></svg>')
    return path.name


def test_the_undiffed_pair_is_picked_up_from_the_manifest_row(tmp_path):
    row = {"name": "DB", "svg": _svg(tmp_path / "d.svg", "delta"), "focus": "",
           "new_svg": _svg(tmp_path / "n.svg", "after"),
           "old_svg": _svg(tmp_path / "o.svg", "before")}
    out, toggles = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert toggles is True
    assert "dgmviews" in out and "after" in out and "before" in out


def test_a_manifest_row_without_the_pair_renders_exactly_as_it_used_to(tmp_path):
    """The columns are new. A manifest written by an older puml-diff.sh — or one where
    plantuml could not render a side — must still produce a plain diagram, not a crash."""
    row = {"name": "DB", "svg": _svg(tmp_path / "d.svg", "delta"), "focus": ""}
    out, toggles = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert toggles is False and "dgmviews" not in out and "delta" in out


def test_a_named_but_missing_side_is_ignored_rather_than_inlined(tmp_path):
    row = {"name": "DB", "svg": _svg(tmp_path / "d.svg", "delta"), "focus": "",
           "new_svg": "gone.svg", "old_svg": ""}
    _, toggles = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert toggles is False


def test_only_a_diagram_that_toggles_advertises_its_header(tmp_path):
    """The header grows a pointer, a hover and an arrow. On a diagram with nothing to
    switch to that is a promise the page cannot keep."""
    src = "\n".join([
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\tnew_svg\told_svg",
        f"Pair\tp.puml\tsequence\tmodified\tp.diff.puml\t{_svg(tmp_path / 'p.svg', 'd')}\t\t"
        f"{_svg(tmp_path / 'p.new.svg', 'n')}\t{_svg(tmp_path / 'p.old.svg', 'o')}",
        f"Lone\tl.puml\tsequence\tmodified\tl.diff.puml\t{_svg(tmp_path / 'l.svg', 'd')}\t\t\t",
    ])
    (tmp_path / "MANIFEST.tsv").write_text(src + "\n")
    rows = build.read_manifest(tmp_path / "MANIFEST.tsv")
    out = build.render_diagrams({"manifest": "MANIFEST.tsv"}, tmp_path, tmp_path, rows=rows)
    assert out.count('class="diagram dgm-toggles"') == 1
    assert out.count('class="diagram"') == 1


def test_a_sequence_diagram_opens_on_the_recording_not_on_the_delta(tmp_path):
    """A sequence delta is the difference between two RECORDINGS, and a run that reorders
    two concurrent calls produces marks nobody made. The reader meets the picture that is
    simply true, and reaches for the delta deliberately."""
    row = {"name": "add-visit.spec.ts.genseq", "kind": "sequence", "focus": "",
           "svg": _svg(tmp_path / "d.svg", "delta"),
           "new_svg": _svg(tmp_path / "n.svg", "after"),
           "old_svg": _svg(tmp_path / "o.svg", "before")}
    out, _ = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert 'class="dgmviews" data-state="new"' in out
    assert '<div class="dgmpane" data-view="diff" hidden>' in out


def test_a_structural_diagram_still_opens_on_the_delta(tmp_path):
    """DB and DomainModel are diffed from two files a human wrote: every mark in that
    delta is a change somebody made, so it is the answer rather than a caveat."""
    row = {"name": "DB", "kind": "structural", "focus": "",
           "svg": _svg(tmp_path / "d.svg", "delta"),
           "new_svg": _svg(tmp_path / "n.svg", "after"),
           "old_svg": _svg(tmp_path / "o.svg", "before")}
    out, _ = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert 'class="dgmviews" data-state="diff"' in out


def test_a_sequence_diagram_whose_new_side_did_not_render_falls_back_to_the_delta(tmp_path):
    """Opening on a side plantuml never wrote would open on nothing at all."""
    row = {"name": "gone.genseq", "kind": "sequence", "focus": "",
           "svg": _svg(tmp_path / "d.svg", "delta"),
           "old_svg": _svg(tmp_path / "o.svg", "before")}
    out, _ = build._diagram_views(row, tmp_path, tmp_path / "d.svg", tmp_path)
    assert 'class="dgmviews" data-state="diff"' in out


def test_an_empty_title_drops_the_heading_instead_of_printing_a_blank_one(tmp_path):
    """A pair names its own scenarios and prints its own source path, so the heading over
    them can only repeat the tab label — above the fold, where the first picture goes."""
    src = "\n".join([
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\tnew_svg\told_svg",
        f"P\tp.puml\tsequence\tmodified\tp.diff.puml\t{_svg(tmp_path / 'p.svg', 'd')}\t\t"
        f"{_svg(tmp_path / 'p.new.svg', 'n')}\t{_svg(tmp_path / 'p.old.svg', 'o')}",
    ])
    (tmp_path / "MANIFEST.tsv").write_text(src + "\n")
    rows = build.read_manifest(tmp_path / "MANIFEST.tsv")

    block = {"type": "testpairs", "id": "sequences", "kind": "sequence", "title": ""}
    out, _, _ = build.render_testpairs(block, {"manifest": "MANIFEST.tsv"}, rows,
                                       tmp_path, tmp_path)
    assert "<h3" not in out, out
    assert "dgmviews" in out                     # the pairs themselves are untouched

    # An absent title is not an empty one: it still gets the default heading.
    kept, _, _ = build.render_testpairs({"type": "testpairs", "id": "sequences",
                                         "kind": "sequence"},
                                        {"manifest": "MANIFEST.tsv"}, rows,
                                        tmp_path, tmp_path)
    assert "<h3" in kept and "Sequence deltas" in kept


# ── the frame that says which picture you are on ──────────────────────────────────

def test_each_state_paints_the_frame_a_different_colour():
    for state, token in (("diff", "--view-diff"), ("new", "--view-new"), ("old", "--view-old")):
        rule = f'.dgmviews[data-state="{state}"] .dgmpane {{ border-color:var({token}); }}'
        assert rule in build.CSS, rule


def test_the_delta_opens_on_the_change_alone():
    """A product choice, not an implementation detail: the level a reader meets first
    decides what they think the change *is*. Zero shows the impacted elements and nothing
    else, so no unchanged neighbour has to be ruled out by eye first."""
    assert build.DEFAULT_FOCUS == "0"


def test_the_frame_is_thick_enough_to_read_without_looking_at_it():
    width = re.search(r"\.dgmpane \{[^}]*border:(\d+)px", build.CSS)
    assert width and int(width.group(1)) >= 4


def test_all_three_colours_are_defined_and_none_is_another_one():
    light = dict(re.findall(r"--view-(\w+):([^;]+);", build.CSS))
    assert set(light) == {"diff", "new", "old"}
    assert len(set(light.values())) == 3
    # red is the delta's own removal red by reference, so the frame and the strokes
    # inside it cannot drift apart, and it follows the palette into dark mode for free
    assert light["diff"] == "var(--dgm-diff-del)"
    # the other two are re-stated for dark, where #1a4fa0/#1f7a45 fall under 3:1
    dark = build.CSS.split("prefers-color-scheme: dark")[1]
    assert "--view-new:" in dark and "--view-old:" in dark


# ── the behaviour ─────────────────────────────────────────────────────────────────

def test_the_header_toggles_and_the_diagram_body_does_not():
    js = build.DGM_VIEWS_JS
    assert ".diagram.dgm-toggles > .head" in js, "the hit area is the header, and only it"
    assert "'.svgbox'" not in js and '".svgbox"' not in js


def test_a_link_in_the_header_still_opens_instead_of_toggling():
    assert "ev.target.closest('a')" in build.DGM_VIEWS_JS


def test_the_control_is_delegated_so_hand_written_markup_works_too():
    """The conceptual model's widget is expanded into a section body from a token, and
    a `.dgmviews` may also be written into one by hand. A per-widget listener bound where
    `render_diagrams` runs would leave both dead."""
    assert "document.addEventListener('click'" in build.DGM_VIEWS_JS
    assert "querySelectorAll('.dgmviews')" not in build.DGM_VIEWS_JS


def test_there_is_exactly_one_place_that_emits_the_control():
    """Two implementations of this would be the mistake worth failing a build over."""
    source = page_source()
    emitters = [line for line in source.splitlines()
                if 'class="dgmviews"' in line and "CSS" not in line]
    assert len(emitters) == 1, emitters


# ── the renders the control needs ─────────────────────────────────────────────────

def test_puml_diff_writes_the_two_extra_columns():
    sh = (HERE / "puml-diff.sh").read_text()
    assert "\\tnew_svg\\told_svg\\told_details\\tnew_details\\n' > \"$MANIFEST\"" in sh
    assert 'render_plain "$new" "$OUT_DIR/$name.new"' in sh
    assert 'render_plain "$old" "$OUT_DIR/$name.old"' in sh


def test_puml_diff_drops_a_side_plantuml_could_not_draw():
    """PlantUML answers a diagram it cannot parse with a valid SVG reading "Syntax
    Error?". Shown behind a New button that is the loudest thing on the page."""
    sh = (HERE / "puml-diff.sh").read_text()
    body = sh.split("render_plain() {")[1].split("\n}")[0]
    assert "Syntax Error" in body and 'rm -f "$2.svg"' in body
    assert '[ -s "$1" ] || return 1' in body, "an empty side is not a diagram"


def test_puml_diff_keeps_the_reason_it_dropped_a_delta():
    """Dropping the picture is right; dropping the complaint with it is what made the
    page say "not rendered" — a sentence that sounds like a step nobody ran."""
    sh = (HERE / "puml-diff.sh").read_text()
    assert "capture_render_failure()" in sh
    body = sh.split("  svg=\"\"\n")[1].split("  # A structural diagram")[0]
    assert 'capture_render_failure "${diff_puml%.puml}.svg" "$diff_puml" "$diff_puml.err"' in body
    assert 'rm -f "$diff_puml.err"' in body, "a delta that draws this time must not keep " \
                                             "the last run's excuse beside it"


def test_a_delta_that_did_not_draw_says_what_plantuml_said(tmp_path):
    (tmp_path / "x.diff.puml").write_text('participant "A B" as A B\n', encoding="utf-8")
    (tmp_path / "x.diff.puml.err").write_text(json.dumps(
        {"message": "Syntax Error? (Assumed diagram type: sequence)",
         "line": 11, "source": 'participant "A B" as A B'}), encoding="utf-8")
    out = diagrams._why_not_drawn({"diff_puml": "x.diff.puml"}, tmp_path)
    assert "diagram could not be drawn" in out
    assert "Syntax Error?" in out                      # PlantUML's words, not ours
    assert "at line 11" in out                         # …and where
    assert html.escape('participant "A B" as A B') in out
    assert f'href="vscode://file/{tmp_path.resolve()}/x.diff.puml:11:1"' in out
    assert "not rendered" not in out


def test_a_delta_with_no_recorded_reason_still_points_at_its_source(tmp_path):
    """Nothing was captured — an older run, or plantuml simply absent. Say the little
    that is true rather than inventing a cause."""
    (tmp_path / "x.diff.puml").write_text("@startuml\n@enduml\n", encoding="utf-8")
    out = diagrams._why_not_drawn({"diff_puml": "x.diff.puml"}, tmp_path)
    assert "not rendered" in out and "x.diff.puml" in out
    assert "could not be drawn" not in out


def test_the_header_arrow_survived_python_before_it_reached_css():
    """The CSS hex escape for this arrow, written into a plain (non-raw) Python string,
    is read as an octal escape by Python first: it shipped as the text "94" on every
    diagram header. Nothing in the stylesheet should contain a control character."""
    assert '.head b::after { content:"↔"' in build.CSS
    assert not [c for c in build.CSS if ord(c) < 32 and c != "\n"]


# ── every pane must be as live as every other ─────────────────────────────────────
#
# The defect this guards is not "Old is broken", it is "wiring that assumes one copy".
# A sequence diagram's expandable arrows carry generation-time ids, and the payloads for
# those ids ride in `script.genseq-details` carriers inlined next to the picture. Draw the
# diagram three times and only one side's payloads are in the page: the other panes look
# wired — cursor, hit area — and expand nothing. These tests are written against however
# many panes exist, so a fourth pane added and left inert fails them.

import json as _json
import re as _re


def _handles(html: str) -> dict:
    """pane name → the genseq ids its SVG draws a handle for."""
    out, parts = {}, _re.split(r'<div class="dgmpane" data-view="', html)
    for chunk in parts[1:]:
        pane, _, body = chunk.partition('"')
        out[pane] = set(_re.findall(r'href="genseq://([^"]+)"', body))
    return out


def _payload_ids(html: str) -> set:
    ids = set()
    for blob in _re.findall(
            r'<script type="application/json" class="genseq-details">(.*?)</script>', html, _re.S):
        ids |= set((_json.loads(blob.replace("\\u003c", "<")) or {}).get("details", {}))
    return ids


def _seq_page(tmp_path, *, base_details: bool):
    """A two-sided sequence diagram whose sides use different payload ids — which is what
    happens whenever a request or response body changed on the branch."""
    def svg(path, ids):
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + "".join(
            f'<a href="genseq://{i}"><text>200</text></a>' for i in ids) + "</svg>")
        return path.name

    def sidecar(path, ids):
        path.write_text(_json.dumps({"version": 1, "details": {
            i: {"title": i, "steps": [{"text": "{}"}]} for i in ids}}))
        return path.name

    # the work tree's, which the page has always carried, and the base ref's, which it
    # did not — both emitted by production code, from the two places they really live
    sidecar(tmp_path / "t.genseq.json", ["new1"])
    sidecar(tmp_path / "s.old.json", ["old1"])
    row = {"name": "Seq", "source": "t.genseq.puml", "kind": "sequence",
           "status": "modified", "diff_puml": "s.diff.puml", "focus": "",
           "svg": svg(tmp_path / "s.svg", ["new1"]),
           "new_svg": svg(tmp_path / "s.new.svg", ["new1"]),
           "old_svg": svg(tmp_path / "s.old.svg", ["old1"]),
           "old_details": "s.old.json" if base_details else ""}
    return build.render_diagrams({"manifest": "M.tsv"}, tmp_path, tmp_path, rows=[row])


def test_every_pane_has_a_payload_for_every_handle_it_draws(tmp_path):
    """The invariant, stated over whatever panes exist. Add a fourth and forget its
    payloads and this fails, naming the pane."""
    page = _seq_page(tmp_path, base_details=True)
    known = _payload_ids(page)
    panes = _handles(page)
    assert len(panes) >= 2, panes
    for pane, ids in panes.items():
        assert ids, f"{pane} draws no handles at all"
        assert ids <= known, f"{pane} draws handles with no payload: {sorted(ids - known)}"


def test_the_test_above_fails_when_a_side_brings_no_payloads(tmp_path):
    """Proof the guard bites: this is the shipped bug, and it must not pass."""
    page = _seq_page(tmp_path, base_details=False)
    known = _payload_ids(page)
    dead = {p: sorted(ids - known) for p, ids in _handles(page).items() if ids - known}
    assert dead == {"old": ["old1"]}, dead


def test_the_page_reads_every_payload_carrier_not_just_the_first():
    """`querySelector` was right when a diagram appeared once. With three panes it means
    two of them silently lose their payloads."""
    js = build.GENSEQ_JS
    assert "querySelectorAll('script.genseq-details')" in js
    assert "querySelector('script.genseq-details')" not in js


def test_nothing_in_the_expander_resolves_by_document_wide_id():
    """The other way duplicated content breaks: an id lookup finds the first copy. The
    handles are per-element listeners and `closest()` — keep it that way."""
    js = build.GENSEQ_JS
    assert "getElementById" not in js
    assert not _re.search(r"""querySelector(?:All)?\(['"]#""", js)


def test_no_line_of_instructions_is_printed_above_the_picture(tmp_path):
    """There used to be one — "Click any arrow marked ⊕ for the SQL or the JSON behind
    it" — written once per diagram, which on a tab that is now one picture per test is
    once per test: a column of identical sentences down the page. The ⊕ is on the label
    itself, the cursor changes over it, and its own tooltip says what a click will get,
    at the moment the reader is looking at it."""
    assert "genseq-hint" not in build.GENSEQ_JS
    assert "genseq-hint" not in _pairs_fixture(tmp_path)


def test_puml_diff_carries_the_base_sidecar_for_the_old_render():
    sh = (HERE / "puml-diff.sh").read_text()
    assert 'git show "$MERGE_BASE:${rel%.puml}.json"' in sh
    assert "\\told_details\\tnew_details\\n' > \"$MANIFEST\"" in sh
    assert '*.genseq.puml)' in sh, "only generated sequence diagrams have a sidecar"


def test_puml_diff_pins_the_work_tree_sidecar_to_the_picture_it_drew():
    """A copy beside the SVG, not a path back to a file that keeps moving."""
    sh = (HERE / "puml-diff.sh").read_text()
    assert 'cp "$ROOT/${rel%.puml}.json" "$OUT_DIR/$name.new.json"' in sh
    assert "\\told_details\\tnew_details\\n' > \"$MANIFEST\"" in sh


def _drifted_page(tmp_path, *, pinned: bool):
    """The shipped failure: the suite ran again after the picture was drawn.

    A generated payload carries per-run values, so a re-run re-ids every arrow whose body
    holds one — here the `200`, whose JSON body has a fresh timestamp in it — and leaves
    the SQL arrow beside it alone. The SVG on disk still draws the ids of the run it was
    rendered from; the work tree's sidecar knows only the newer ones.
    """
    def sidecar(path, ids):
        path.write_text(_json.dumps({"version": 1, "details": {
            i: {"title": i, "steps": [{"text": "{}"}]} for i in ids}}))
        return path.name

    svg = tmp_path / "s.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + "".join(
        f'<a href="genseq://{i}"><text>200</text></a>' for i in ("sql", "drawn")) + "</svg>")
    sidecar(tmp_path / "s.new.json", ["sql", "drawn"])       # taken when the SVG was drawn
    sidecar(tmp_path / "t.genseq.json", ["sql", "rerun"])    # the work tree, a run later
    row = {"name": "Seq", "source": "t.genseq.puml", "kind": "sequence",
           "status": "modified", "diff_puml": "s.diff.puml", "focus": "",
           "svg": svg.name, "new_svg": "", "old_svg": "", "old_details": "",
           "new_details": "s.new.json" if pinned else ""}
    return build.render_diagrams({"manifest": "M.tsv"}, tmp_path, tmp_path, rows=[row])


def test_a_re_run_of_the_suite_does_not_kill_the_handles_on_the_drawn_picture(tmp_path):
    page = _drifted_page(tmp_path, pinned=True)
    known = _payload_ids(page)
    drawn = set(_re.findall(r'href="genseq://([^"]+)"', page))
    assert drawn == {"sql", "drawn"}
    assert drawn <= known, f"dead handles: {sorted(drawn - known)}"


def test_the_test_above_fails_when_the_sidecar_is_read_live(tmp_path):
    """Proof the guard bites. Without the snapshot the response handle resolves to
    nothing and the page unwraps it — `200 ⊕` stops opening its body while the `select`
    beside it still works, which is exactly how the bug was reported."""
    page = _drifted_page(tmp_path, pinned=False)
    dead = set(_re.findall(r'href="genseq://([^"]+)"', page)) - _payload_ids(page)
    assert dead == {"drawn"}, dead


def test_a_manifest_written_before_the_snapshot_still_finds_the_work_tree_sidecar(tmp_path):
    """The fallback. An old `.human-review/` has no `new_details` column, and the page it
    built before this change must keep building."""
    page = _drifted_page(tmp_path, pinned=False)
    assert "sql" in _payload_ids(page)


# ── testpairs: the test beside the sequence its own run recorded ───────────────────
#
# The gallery became pairs so the diagrams read as study material rather than a lookup
# exercise. Two things must survive that restructure: the three-state viewer on each
# diagram, and BOTH payload carriers — the work tree's and the base ref's — because the
# panes are rendered through the same `render_diagrams` and losing a carrier is how the
# Old pane went inert the first time.

def _pairs_fixture(tmp_path, *, quote_test=True, test_on_disk=True):
    # `extract-snippet.py` resolves the project from git, and refuses to guess: a snippet
    # whose line numbers came from outside a repository could not be diffed against the
    # base, and it would rather say so than render a badge it cannot stand behind.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    def svg(path, ids):
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + "".join(
            f'<a href="genseq://{i}"><text>200</text></a>' for i in ids) + "</svg>")
        return path.name

    def sidecar(path, ids):
        path.write_text(_json.dumps({"version": 1, "details": {
            i: {"title": i, "steps": [{"text": "{}"}]} for i in ids}}))
        return path.name

    src = tmp_path / "spec.ts.genseq.puml"
    src.write_text("@startuml\n== [[src://spec.ts:2{Click to open the test} A scenario]] ==\n@enduml\n")
    if test_on_disk:
        (tmp_path / "spec.ts").write_text("// header\ntest('A scenario', () => {\n  ok();\n});\n")
    sidecar(tmp_path / "spec.ts.genseq.json", ["new1"])
    sidecar(tmp_path / "s.old.json", ["old1"])
    row = {"name": "Spec", "source": "spec.ts.genseq.puml", "kind": "sequence",
           "status": "modified", "diff_puml": "s.diff.puml", "focus": "",
           "svg": svg(tmp_path / "s.svg", ["new1"]),
           "new_svg": svg(tmp_path / "s.new.svg", ["new1"]),
           "old_svg": svg(tmp_path / "s.old.svg", ["old1"]),
           "old_details": "s.old.json"}
    block = {"type": "testpairs", "id": "sequences", "kind": "sequence",
             "snippets": ([{"ref": "spec.ts:2-4"}] if quote_test else [])}
    html, weight, _ = build.render_testpairs(block, {"manifest": "M.tsv"}, [row],
                                             tmp_path, tmp_path)
    return html


def test_a_pair_keeps_the_three_state_viewer(tmp_path):
    html = _pairs_fixture(tmp_path)
    assert 'class="dgmviews"' in html
    assert html.count('<div class="dgmpane"') == 3
    for view in ("diff", "new", "old"):
        assert f'data-view="{view}"' in html


def test_a_pair_carries_both_payload_sidecars(tmp_path):
    """The regression that made the Old pane inert. Rendered inside a pair, the diagram
    must still ship the base ref's payloads as well as the work tree's."""
    html = _pairs_fixture(tmp_path)
    known = _payload_ids(html)
    assert known == {"new1", "old1"}, known
    for pane, ids in _handles(html).items():
        assert ids <= known, f"{pane} draws handles with no payload: {sorted(ids - known)}"


def test_a_pair_does_not_list_the_scenarios_the_diagram_already_titles(tmp_path):
    """The scenario names were printed above the snippet as deep links, and the diagram
    below prints the same titles, linked to the same lines, as its own section headers.
    One list, on the picture, where the reader is already looking."""
    html = _pairs_fixture(tmp_path)
    assert '<p class="testlead">' not in html
    assert "dgmviews" in html and "snippet" in html      # the pair itself is intact


def test_the_whole_pair_folds_away_and_starts_open(tmp_path):
    """Open, because the pair is what the reader came for. Foldable, because the tab's
    argument is made by comparing exhibits, and then the rest is what is in the way.

    Both halves fold: a sequence whose test has been put away is a picture of nothing, so
    the diagram must be *inside* the fold, not left standing under a closed summary."""
    html_out = _pairs_fixture(tmp_path)
    assert '<details class="testpair" open id=' in html_out
    # Both inside the fold: to the pair's own closing tag, which is the last one on the
    # block — only the registry the tab writes for the 🕵️ comes after it.
    pair = html_out[html_out.index('<details class="testpair"'):html_out.rindex("</details>")]
    assert "snippet" in pair and 'class="diagram' in pair


def test_the_quoted_test_starts_closed_and_the_diagram_is_in_view(tmp_path):
    """The tab is called Sequence. A thirty-line block of the spec above every diagram put
    the picture below the fold on each exhibit; now the source is a closed fold inside the
    open pair, its one row is the block's own source bar, and the diagram is outside it."""
    html_out = _pairs_fixture(tmp_path)
    assert '<details class="testsrc">' in html_out
    assert '<details class="testsrc" open>' not in html_out
    src = html_out[html_out.index('<details class="testsrc">'):]
    src = src[:src.index("</details>")]
    assert "snippet" in src and 'class="diagram' not in src
    assert '<summary><span class="foldlbl"></span><div class="srcbar">' in src
    assert "the test · lines " not in html_out
    after = html_out[html_out.index('<details class="testsrc">'):]
    after = after[after.index("</details>"):]
    assert 'class="diagram' in after


def test_the_fold_is_labelled_with_the_scenario_and_keeps_the_path_as_its_tooltip(tmp_path):
    """`spec.ts` is the name of the box; the reader is looking for "A scenario", which is
    what the Tests tab calls it and what the diagram's own chapter header says. The path
    is not lost — it is the summary's tooltip — and it is not printed a third time here,
    because the fold's row under it and the diagram below both already carry it."""
    html_out = _pairs_fixture(tmp_path)
    assert '<summary data-tip="spec.ts">A scenario</summary>' in html_out


def test_a_paired_diagram_does_not_repeat_the_name_the_fold_just_said(tmp_path):
    """Three answers to one question in four centimetres: the fold's summary, a card title
    that was that file name with `.genseq` on it, and a `generated by` line under it. Only
    the .puml path is news, so it is the only one left — and the href the provenance line
    used to carry for the scenario links now rides on the card."""
    html_out = _pairs_fixture(tmp_path)
    assert "<b>" not in html_out
    assert "generated by" not in html_out
    assert 'class="prov"' not in html_out
    assert "data-test-src=" in html_out


def test_a_diagram_nobody_quoted_says_so_instead_of_saying_nothing(tmp_path):
    """Silence would read as "this diagram has no test", which is never true — the
    manifest knows about it only because a test generated it."""
    html = _pairs_fixture(tmp_path, quote_test=False)
    assert "not excerpted here" in html
    assert "spec.ts" in html


def test_a_diagram_whose_test_left_the_checkout_says_that_instead(tmp_path):
    html = _pairs_fixture(tmp_path, quote_test=False, test_on_disk=False)
    assert "not in this checkout" in html
    assert "not excerpted here" not in html


def test_a_test_with_no_diagram_is_never_dropped(tmp_path):
    """The more interesting absence: a tagged test whose trace never came back is a fact
    about the evidence. It goes to a named group, not to the floor."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "orphan.ts").write_text("// x\ntest('untraced', () => {\n  ok();\n});\n")
    src = tmp_path / "spec.ts.genseq.puml"
    src.write_text("@startuml\n@enduml\n")
    (tmp_path / "spec.ts.genseq.json").write_text('{"version":1,"details":{}}')
    (tmp_path / "s.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    row = {"name": "Spec", "source": "spec.ts.genseq.puml", "kind": "sequence",
           "status": "modified", "diff_puml": "s.diff.puml", "focus": "",
           "svg": "s.svg", "new_svg": "", "old_svg": "", "old_details": ""}
    block = {"type": "testpairs", "id": "sequences", "kind": "sequence",
             "snippets": [{"ref": "orphan.ts:2-4"}],
             "unpaired": {"id": "tests-nosequence", "title": "No diagram came back"}}
    html, _, _ = build.render_testpairs(block, {"manifest": "M.tsv"}, [row],
                                        tmp_path, tmp_path)
    assert "No diagram came back" in html and "untraced" in html
    assert 'id="tests-nosequence"' in html


def test_a_test_whose_sequence_did_not_change_is_paired_and_marked_not_orphaned(tmp_path):
    """The manifest lists only diagrams the branch changed. A quoted test whose
    `.genseq.puml` sits beside it, identical to the base, used to land in "no diagram
    came back" — false, and corrected by hand on every page it happened to. It is a pair:
    the picture drawn from the committed source, pilled UNCHANGED, with no Diff to offer,
    and it weighs as context rather than as a delta."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "same.ts").write_text("// x\ntest('untouched', () => {\n  ok();\n});\n")
    (tmp_path / "same.ts.genseq.puml").write_text("@startuml\n@enduml\n")
    (tmp_path / "same.ts.genseq.json").write_text('{"version":1,"details":{"h1":{}}}')
    # The render the `puml` block's own cache would hold, so no PlantUML runs here.
    cache = tmp_path / "assets" / "same.ts.genseq.context.svg"
    cache.parent.mkdir()
    cache.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>same</text></svg>')
    block = {"type": "testpairs", "id": "sequences", "kind": "sequence",
             "snippets": [{"ref": "same.ts:2-4"}],
             "unpaired": {"id": "tests-nosequence", "title": "No diagram came back"}}
    html, weight, changes = build.render_testpairs(block, {"manifest": "M.tsv"}, [],
                                                   tmp_path, tmp_path)
    assert "No diagram came back" not in html
    # No chapter in that .puml, so the summary falls back to the basename — all there is.
    assert '<summary data-tip="same.ts">same.ts</summary>' in html
    assert '<span class="badge sev-info">unchanged</span>' in html
    assert "<text>same</text>" in html and "untouched" in html
    assert "dgmviews" not in html and "dgm-diff" not in html
    assert "data-test-src=" in html and 'class="genseq-details"' in html
    assert (weight, changes) == (1, 0)


def _base_repo(tmp_path, *, base_body, work_body):
    """A checkout whose `origin/main` really exists, with one `.genseq.puml` on it.

    The pair rendering asks git whether a diagram it has no delta for is what the base
    has, so a fixture that wants that question answered has to give it a base to answer
    about: a commit, a branch named the way the build spells its base ref, and a second
    commit on top that only the branch has.
    """
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (tmp_path / "same.ts").write_text("// x\ntest('untouched', () => {\n  ok();\n});\n")
    (tmp_path / "same.ts.genseq.puml").write_text(base_body)
    run("add", "-A")
    run("commit", "-qm", "base")
    # The ref the build measures against, spelled as a remote-tracking branch because
    # that is what `HUMAN_REVIEW_DIFF_BASE` defaults to.
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "-q", "-b", "feature")
    (tmp_path / "same.ts.genseq.puml").write_text(work_body)
    cache = tmp_path / "assets" / "same.ts.genseq.context.svg"
    cache.parent.mkdir(exist_ok=True)
    cache.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>same</text></svg>')
    block = {"type": "testpairs", "id": "sequences", "kind": "sequence",
             "snippets": [{"ref": "same.ts:2-4"}],
             "unpaired": {"id": "tests-nosequence", "title": "No diagram came back"}}
    return build.render_testpairs(block, {"manifest": "M.tsv"}, [],
                                  tmp_path, tmp_path)


def test_a_sequence_that_differs_from_the_base_is_never_pilled_unchanged(tmp_path):
    """The bug this guards: the page said UNCHANGED over a diagram `git diff` calls
    modified.

    A pair is told it has no delta by the *absence of a row* in `MANIFEST.tsv` — and that
    manifest is written by a producer that does not run on every build (`refresh-report.py`
    runs none by default). Regenerate a `.genseq.puml`, rebuild the page without re-running
    `diagrams`, and the pair came out pilled UNCHANGED over a picture whose every lifeline
    differs from the base. The claim is now checked against the repository, so it cannot
    survive the manifest going stale."""
    build._moved_since_base.cache_clear()
    html_, weight, changes = _base_repo(
        tmp_path,
        base_body="@startuml\nA -> B : hello\n@enduml\n",
        work_body="@startuml\nA -> B : hello\nA -> SMS : send\n@enduml\n")
    assert '<span class="badge sev-info">unchanged</span>' not in html_
    assert "delta not drawn" in html_
    assert "MANIFEST.tsv" in html_ and "Rerun" in html_
    # Still a pair, still weighed as context: the evidence is stale, not a delta.
    assert "No diagram came back" not in html_
    assert (weight, changes) == (1, 0)


def test_a_sequence_identical_to_the_base_is_still_pilled_unchanged(tmp_path):
    """The other half of the same check, so the guard above cannot be satisfied by
    pilling every pair as stale."""
    build._moved_since_base.cache_clear()
    body = "@startuml\nA -> B : hello\n@enduml\n"
    html_, weight, changes = _base_repo(tmp_path, base_body=body, work_body=body)
    assert '<span class="badge sev-info">unchanged</span>' in html_
    assert "delta not drawn" not in html_
    assert (weight, changes) == (1, 0)


# ── the hand-drawn diagram, read off disk on every build ──────────────────────────
#
# The conceptual model is the only picture on this page the report *asks the reader to go
# and change*: it tells them to open draw.io and re-lay the thing out by hand. Its markup
# used to be pasted into content.json, which meant the reader did exactly that, rebuilt,
# and got back the drawing that was current when a model last wrote the section — the one
# surface where "refresh" was guaranteed not to show the change just made.

def _drawio_set(assets: Path, name="conceptual", red=(), added=()):
    assets.mkdir(parents=True, exist_ok=True)
    for suffix, text in (("diff", "delta"), ("new", "after"), ("original", "before")):
        _svg(assets / f"{name}-{suffix}.svg", text)
    (assets / f"{name}-diff.json").write_text(json.dumps({
        "added": [{"what": w, "already_red": r} for w, r in added],
        "removed": [], "changed": [], "moved": [],
        "red": [{"what": w} for w in red],
    }))
    return assets


def test_the_token_inlines_the_pictures_that_are_on_disk_now(tmp_path):
    out = build.drawio_widget_html("conceptual", _drawio_set(tmp_path / "assets"), tmp_path)
    assert "delta" in out and "after" in out and "before" in out
    assert 'class="dgmviews"' in out


def test_re_running_the_differ_is_the_whole_refresh(tmp_path):
    """The bug this token exists for: re-lay the diagram out, rebuild, see the new one."""
    assets = _drawio_set(tmp_path / "assets")
    _svg(assets / "conceptual-new.svg", "hand-drawn at last")
    out = build.drawio_widget_html("conceptual", assets, tmp_path)
    assert "hand-drawn at last" in out and "after" not in out


def test_it_opens_on_new_while_a_layout_is_still_owed(tmp_path):
    """`New` is the pane that answers first while anything is red: the delta is then a
    picture of automation's routing rather than of the change."""
    assets = _drawio_set(tmp_path / "assets", red=["Vet-Visit"], added=[("Vet-Visit", True)])
    assert 'data-state="new"' in build.drawio_widget_html("conceptual", assets, tmp_path)


def test_it_opens_on_the_delta_once_the_layout_has_been_drawn(tmp_path):
    assets = _drawio_set(tmp_path / "assets", added=[("Vet-Visit", False)])
    assert 'data-state="diff"' in build.drawio_widget_html("conceptual", assets, tmp_path)


def test_the_legend_names_only_the_colours_the_drawing_actually_uses(tmp_path):
    """A legend row for a colour that is not on screen is a row the reader has to rule
    out by eye — and the to-do row outliving the to-do is how the page told Victor his
    hand-drawn layout had not been picked up."""
    drawn = build.drawio_widget_html(
        "conceptual", _drawio_set(tmp_path / "a", added=[("Vet-Visit", False)]), tmp_path)
    assert "added by this PR" in drawn
    assert "still waiting for a manual re-layout" not in drawn

    owed = build.drawio_widget_html(
        "conceptual",
        _drawio_set(tmp_path / "b", red=["Vet-Visit"], added=[("Vet-Visit", True)]), tmp_path)
    assert "still waiting for a manual re-layout" in owed
    assert "added by this PR" not in owed, "an addition drawn red renders red, not green"


def test_the_to_do_row_is_repeated_under_the_undiffed_new_pane(tmp_path):
    """The red is drawn in the diagram itself, so it is on screen there too — with
    nothing else on that pane to explain it. The green is not: nothing in the undiffed
    drawing is green."""
    out = build.drawio_widget_html(
        "conceptual",
        _drawio_set(tmp_path / "a", red=["Vet-Visit"], added=[("Vet-Visit", True)]), tmp_path)
    pane = re.search(r'<div class="dgmpane" data-view="new".*?(?=<div class="dgmpane")',
                     out, re.S).group(0)
    assert "still waiting for a manual re-layout" in pane


def test_a_section_body_expands_the_token(tmp_path):
    out_dir = tmp_path / ".human-review"
    _drawio_set(out_dir / "assets")
    body = build.expand_drawio("<p>prose</p>{{drawio:conceptual}}", out_dir, tmp_path, "")
    assert "<p>prose</p>" in body and "delta" in body


def test_a_missing_render_says_which_step_writes_it(tmp_path):
    """Better than a traceback in the middle of a build, and better than a silent gap:
    the diagrams step is the one thing that fixes it."""
    out = build.drawio_widget_html("conceptual", tmp_path / "assets", tmp_path)
    assert "diagrams" in out and "conceptual-diff.svg" in out


def test_the_legend_takes_its_green_from_the_report_s_added_colour(tmp_path):
    """A private hex here would be the one place on the page where "added" is a second
    green. `drawio-diff.py` paints the strokes with the same one."""
    assert "color:var(--dgm-diff-add)" in build.CSS


# ── the command under the picture ─────────────────────────────────────────────────
#
# The panes are inlined into the HTML, and they have to be: the boxes are links into the
# classes they name and the to-do note is a link into draw.io, and an SVG loaded through
# `<img src>` renders both as decoration. So the file on disk and the picture in the page
# are two artefacts, a reload only ever refreshes the second, and the page owes the reader
# the command that refreshes the first — at the moment they need it, in a form they can run.

RERUN = {"cwd": "/repo", "command": "/tools/drawio-diff.py --name conceptual"}
REBUILD = "python3 /tools/build-review-html.py content.json --out review.html"


def test_the_command_names_every_step_the_reader_has_to_take(tmp_path):
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    assert "cd /repo" in out and RERUN["command"] in out and REBUILD in out


def test_the_button_copies_exactly_what_the_page_shows(tmp_path):
    """Two renderings of one command is how the copied one quietly stops matching the read
    one — and the copied one is the only one that gets run."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    # The command is on the page exactly twice, on two attributes of two controls — the
    # offer, whose click copies it off disk, and the copy glyph beside it — plus once more
    # inside that glyph's hover. All of them one string, because two renderings of one
    # command is how the one that gets run stops matching the one that gets read.
    copied = set(re.findall(r'data-copy="(.*?)"', out, re.S))
    assert len(copied) == 1
    line = copied.pop()
    assert build.ACTIONS["drawio:conceptual"]["command"] == html.unescape(line)
    tip = re.search(r'class="copycmd cmd-copy[^"]*"[^>]*data-tip="([^"]*)"', out, re.S).group(1)
    assert tip.endswith(line)
    # And the same line again on `data-cmd`, on both faces: it is the handle the one-string
    # guardrail reads, and it has to say what the register says.
    assert {html.unescape(c) for c in re.findall(r'data-cmd="(.*?)"', out, re.S)} == \
        {build.ACTIONS["drawio:conceptual"]["command"]}


def test_a_run_that_recorded_nothing_offers_no_half_command(tmp_path):
    """`rerun` is written by `drawio-diff.py`; a verdict from before it did carries none.
    A command assembled from guesses is worse than no command — it is tried first."""
    out = build.drawio_widget_html("conceptual", _drawio_set(tmp_path / "assets"),
                                   tmp_path, REBUILD)
    assert "cmd-copy" not in out and "cmd-run" not in out


def test_the_command_says_what_it_is_for(tmp_path):
    """Where to edit, and how to pick the edit up, are one sentence — not a paragraph, a
    sentence and a code block stacked under the picture they explain."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN,
        "drawio_url": "drawio:///repo/C.drawio.png"}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    line = re.search(r'<p class="dgm-open">(.*?)</p>', out, re.S).group(1)
    # The sentence says where to edit, and that is all it says. The offer is a pill under
    # it — the line used to carry both, and by the time each offer had grown its glyphs it
    # was seven underlined runs of text with no rank between them.
    assert "Edit " in line and "in draw.io" in line
    assert "offer-pill" not in line and "cmd-copy" not in line
    acts = re.search(r'<div class="rerun-acts">(.*?)</div>', out, re.S).group(1)
    # One control, not a pill and a mark beside it: the words and the glyph are the same
    # button, which is the rule every command on this page follows.
    assert '<span class="cmd-word">Update the report</span>' in acts
    assert "offer-pill" not in acts
    assert "cmd-copy" in acts and "cmd-run" in acts
    assert "<code>" not in out, "the command is in a hover, not on the page"


def test_the_command_is_in_a_hover_and_not_in_a_block(tmp_path):
    """A code block under a diagram is read once and then sits in front of the picture on
    every look after that. It used to be behind a fold, which cost the reader who wanted
    it one click and everyone else a control to ignore — and these are the longest lines
    on the page by a factor of five. Now the offer carries a clipboard and the line is in
    its hover, which is the only place a reader who wants to paste it looks."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    assert "cmdline" not in out and "cmdpeek" not in out
    assert "<code>" not in out
    tip = re.search(r'class="copycmd cmd-copy[^"]*"[^>]*data-tip="([^"]*)"', out).group(1)
    assert "Copy command to paste in terminal" in tip
    assert "drawio-diff.py" in tip and "build-review-html.py" in tip


def test_each_copy_of_the_report_shows_the_route_it_can_actually_take(tmp_path):
    """Both routes are in the markup and CSS picks: `run this` until the probe finds a
    server for that action, `click here` after. Off disk the button is a promise the page
    cannot keep; served, the shell line is noise beside a control that already runs it."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    # One control in both copies, and it always sends the same id: the difference is what
    # the page does with it, which the probe decides. The last offer with two renderings is
    # `reveal_html`, whose subject is the sentence's own noun.
    assert 'data-action="drawio:conceptual"' in out
    assert ".rerun .offer .runhere { display:none; }" in build.CSS, "reveal, static"
    assert ".rerun .offer.served .runhere { display:inline; }" in build.CSS
    assert ".rerun .offer.served .plainword { display:none; }" in build.CSS
    assert "offer.classList.add('served')" in build.SERVER_JS, "the probe is what flips it"
    # And the run glyph beside it is the visible statement that this copy can run it —
    # raised by the probe, which takes the clipboard beside it away in the same breath.
    assert 'class="runhere cmd-run has-word" hidden' in out
    assert "if (!b.classList.contains('cmd-run')) return;" in build.SERVER_JS
    assert "clip.hidden = true" in build.SERVER_JS


def test_both_editors_are_offered_and_named(tmp_path):
    """Two links, not one: the app edits the file on disk, the web editor edits a copy in
    the URL. A reader has to be able to tell which is which before clicking."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [],
        "drawio_url": "drawio:///repo/C.drawio.png",
        "drawio_web_url": "https://app.diagrams.net/?splash=0&title=C#R%3Cmx%3E"}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    assert "in draw.io " in out
    assert ">App ↗</a>" in out and ">Web ↗</a>" in out
    assert out.count("draw.io ") == 1, "the brand is named once, the two editors after it"
    assert "drawio:///repo/C.drawio.png" in out and "app.diagrams.net" in out


REVEAL = {"command": "open -R /repo/docs/C.drawio.png", "in": "the Finder"}


def test_the_subject_of_the_sentence_shows_the_file_on_disk(tmp_path):
    """The line says how to edit the drawing and how to pick the edit up, and said nothing
    about where the file is — while the two words naming it sat unclickable at the front."""
    out = _widget_with(tmp_path, rerun=RERUN, reveal=REVEAL,
                       drawio_url="drawio:///repo/docs/C.drawio.png")
    assert '>this diagram</button>' in out
    assert build.ACTIONS["drawio-reveal:conceptual"]["command"] == REVEAL["command"]
    assert not build.ACTIONS["drawio-reveal:conceptual"]["reload"], \
        "revealing a file changes nothing on the page"
    tip = re.search(r'data-action="drawio-reveal:conceptual"[^>]*'
                    r'data-tip-served="([^"]*)"', out).group(1)
    assert "the Finder" in tip


def test_a_verdict_with_no_reveal_leaves_the_words_as_words(tmp_path):
    """An older verdict, or a run from before the command was recorded: the sentence reads
    exactly as it did, with nothing half-rendered where the control would have been."""
    out = _widget_with(tmp_path, rerun=RERUN, drawio_url="drawio:///repo/docs/C.drawio.png")
    assert "Edit this diagram in" in out and "drawio-reveal" not in out


def test_the_web_link_is_dropped_when_the_verdict_has_none(tmp_path):
    """An older verdict, written before the web link existed, still gets the app link —
    and no half-rendered "or" hanging off the end of the sentence."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [],
        "drawio_url": "drawio:///repo/C.drawio.png"}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    assert ">App ↗</a>" in out
    assert ">Web ↗</a>" not in out and " or " not in out


def test_the_copy_button_reuses_the_one_clipboard_and_the_one_toast():
    """A second clipboard-and-toast implementation for one button is how two of them end
    up behaving differently — and it happened: there were two, they had drifted, and the
    one *without* the `execCommand` fallback was the one on the control that only exists
    off disk, where `navigator.clipboard` is the half that may not be there."""
    js = build.EDITOR_JS
    assert "button.copycmd" in js
    assert js.count("toast.id = 'copy-toast'") == 1
    assert "var copy = window.HR.copy;" in js
    # One implementation, on HR, and it has the fallback a `file://` page needs.
    assert build.SERVER_JS.count("function copy(") == 1
    assert "document.execCommand('copy')" in build.SERVER_JS
    assert "function copy(" not in js
    assert build.APP_ENV_JS.count("navigator.clipboard") == 0


def test_the_rebuild_command_is_never_an_interpreter_that_is_about_to_vanish():
    """`sys.executable` under `uv run` is a build directory uv deletes on exit: the one
    command guaranteed to have worked would be the one guaranteed not to work again."""
    got = build.rebuild_interpreter()
    assert "/.cache/uv/builds" not in got
    assert got.startswith("python3") or got.startswith("uv run") or Path(got).exists()


# ── the file named in a diagram's header ──────────────────────────────────────────
#
# The card's title already said *Conceptual Model*; the header's second half exists to
# answer "which file is that?". A repo-relative path answers it in its last segment and
# spends everything before that on where the repository keeps its documents.

def test_the_header_names_the_file_and_keeps_the_path_on_hover(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ConceptualModel.drawio.png").write_bytes(b"")
    out = build._source_link("docs/ConceptualModel.drawio.png", tmp_path)
    assert ">ConceptualModel.drawio.png</a>" in out
    assert 'data-tip="Open in VS Code: docs/ConceptualModel.drawio.png"' in out
    assert ">docs/" not in out, "the path is the hover, not the face"


def test_the_hand_written_header_in_a_section_body_is_shortened_too(tmp_path):
    """The conceptual model's header is pasted into content.json rather than emitted, so
    a rule enforced only where diagrams are rendered would hold for every card except the
    one the reader is being asked to go and edit."""
    body = build.shorten_dgm_src(
        '<div class="head"><b>Conceptual Model</b>'
        '<a class="dgm-src" href="vscode://file//r/b/docs/ConceptualModel.drawio.png:1:1">'
        'b/docs/ConceptualModel.drawio.png</a></div>')
    assert ">ConceptualModel.drawio.png</a>" in body
    assert 'data-tip="Open in VS Code: b/docs/ConceptualModel.drawio.png"' in body
    assert 'href="vscode://file//r/b/docs/ConceptualModel.drawio.png:1:1"' in body, \
        "the link still opens the file it always opened"


def test_a_file_at_the_repository_root_is_left_alone(tmp_path):
    """Nothing to move, and a tip repeating the name says nothing."""
    (tmp_path / "Model.drawio.png").write_bytes(b"")
    out = build._source_link("Model.drawio.png", tmp_path)
    assert ">Model.drawio.png</a>" in out and "data-tip" not in out


def test_an_unchanged_diagrams_header_links_into_vs_code_too(tmp_path, monkeypatch):
    """The `puml` block draws the diagrams a branch did NOT touch, and its header used to
    be the one on the page that was plain text: the full path, spelled out, opening
    nothing. A reader who wants the rule behind `Packages` has to go and read the file."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "packages.puml").write_text("@startuml\n@enduml\n")
    monkeypatch.setattr(diagrams, "_context_svg",
                        lambda rel, root, out_dir: (tmp_path / "x.svg", ""))
    monkeypatch.setattr(diagrams, "inline_svg", lambda cache, root: "<svg/>")
    out = build.render_puml({"src": "docs/packages.puml", "name": "Packages"},
                            tmp_path, tmp_path)
    assert 'href="vscode://file/' + str(tmp_path / "docs" / "packages.puml") + ':1:1"' in out
    assert ">packages.puml</a>" in out
    assert 'data-tip="Open in VS Code: docs/packages.puml"' in out
    assert ">docs/packages.puml<" not in out, "the path is the hover, not the face"


def test_shortening_twice_changes_nothing(tmp_path):
    once = build.shorten_dgm_src(
        '<a class="dgm-src" href="x">b/docs/Model.puml</a>')
    assert build.shorten_dgm_src(once) == once


# ── the way back: automation's own drawing ────────────────────────────────────────
#
# Re-laying the map out by hand is what the red asks for, and it was also the only step
# on this page with no way back: the layout is in the file, the file is in the repository,
# and "let me see what the machine drew" meant going and finding a revision by hand.

REVERT = {"cwd": "/repo", "diagram": "docs/C.drawio.png",
          "sha": "450db720c62af1b8783f5c3e26606ead0bbe01ca", "short": "450db720",
          "date": "2026-09-16", "subject": "Centre the re-layout note under the map",
          "command": "git stash push -m 'human-review: hand edits to docs/C.drawio.png' "
                     "-- docs/C.drawio.png && git checkout 450db720c62af1b8783f5c3e26606"
                     "ead0bbe01ca -- docs/C.drawio.png"}

REDRAW = {"cwd": "/repo", "base": "origin/main", "diagram": "docs/CM.drawio.png",
          "command": "git checkout origin/main -- docs/CM.drawio.png && docs/patch.py"}


def _as_read(html_out: str, served: bool) -> str:
    """What a reader actually sees, once the CSS has hidden the route they cannot take.

    Both are in the markup — `click here` and `run this`, the reveal control and the two
    plain words behind it — so stripping the tags off the raw line renders every label
    twice. The tests read the sentence the way the browser lays it out, in one world or
    the other, because that is the thing being asserted about."""
    # `reveal_html` is the one offer with two renderings left — the subject of the
    # sentence is a control served and two plain words off disk — so it is the only thing
    # CSS still picks between. Everything else is one control in both copies. The glyphs
    # come out either way: they carry no text a reader reads in the sentence.
    drop = [r'class="plainword"'] if served else [r'class="runhere"']
    drop += [r'class="copycmd cmd-copy"', r'class="runhere cmd-run"']
    for cls in drop:
        html_out = re.sub(r'<(button|span)[^>]*' + cls + r'[^>]*>.*?</\1>', '',
                          html_out, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_out)).strip()


def _widget_with(tmp_path, **verdict):
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps(
        {"added": [], "removed": [], "changed": [], "moved": [], "red": [], **verdict}))
    return build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)


def test_each_offer_carries_its_own_command_and_not_the_neighbours(tmp_path):
    """Two commands under one picture, and they used to live in two folds keyed by id
    because "the first .cmdline in here" would open the one that re-renders when the reader
    asked for the one that starts over. There are no folds now — each offer wears its own
    clipboard — so the thing to pin is that the two clipboards differ."""
    out = _widget_with(tmp_path, rerun=RERUN, redraw=REDRAW)
    copied = {html.unescape(c) for c in re.findall(r'data-copy="(.*?)"', out, re.S)}
    assert len(copied) == 2, "one command per offer, and they are not the same command"
    assert any("git checkout" in c for c in copied)
    assert sum("drawio-diff.py" in c for c in copied) == 2, "both end in the re-render"


def test_a_repository_that_declared_no_redraw_is_offered_none(tmp_path):
    """The patch script is the reviewed repository's, not this tool's. Guessing it from a
    naming convention and running it on a reader's click is not a trade worth making."""
    out = _widget_with(tmp_path, rerun=RERUN)
    assert "start over" not in out and "redraw-conceptual" not in out


# ── The one way back ──────────────────────────────────────────────────────────────
#
# The page tells the reader to go and drag boxes around in draw.io, and dragging boxes
# around is how a guardrail that was green stops being green. So there is a way back, and
# it is an offer beside the one that sent them to draw.io in the first place.
#
# There used to be two, and to the reader they were the same offer. *Undo your edits*
# walked back to the newest committed drawing; *start over* restored the base and re-ran
# the repository's patch script. Two commands, two tooltips, and one question behind every
# press of either: give me back the diagram the machine makes. Only the second answers it —
# the first hands back a human's layout from an earlier commit, which is a different
# drawing and is not generated in any sense the reader meant.

def test_there_is_one_way_back_and_it_is_the_generated_drawing(tmp_path):
    out = _widget_with(tmp_path, rerun=RERUN, revert=REVERT, redraw=REDRAW)
    assert '<span class="cmd-word">Revert the diagram</span>' in out
    # Named after what it produces, not after the gesture that gets you there: "start over"
    # is a direction and not a destination, and "undo your edits" is gone entirely.
    assert "start over" not in out and "undo your edits" not in out
    assert "drawio-undo" not in out and "drawio-undo:conceptual" not in build.ACTIONS
    assert "drawio-redraw:conceptual" in build.ACTIONS


def test_a_verdict_that_still_records_a_revert_builds_and_ignores_it(tmp_path):
    """`revert` is what an older `drawio-diff.py` wrote about a command this page no longer
    offers. A page that refused to build on one would make a tool upgrade a migration."""
    out = _widget_with(tmp_path, rerun=RERUN, revert=REVERT)
    assert "drawio-undo" not in out
    assert "Update the report" in out


def test_the_one_way_back_runs_every_stage_and_banks_the_layout(tmp_path):
    """Restoring the file is not enough on its own: the picture in the page is inlined at
    build time, so a regeneration that stopped at the file would leave the reader looking
    at their own layout with the machine's on disk. And it is destructive, so it banks the
    work first — the command that went away was the survivable one, and losing that along
    with it would be a bad trade for a simpler line."""
    out = _widget_with(tmp_path, rerun=RERUN, redraw=REDRAW)
    # The register is where the command lives, and it is what the control renders: the
    # clipboard's `data-copy`, both faces' `data-cmd` and the hover are all this string.
    line = build.ACTIONS["drawio-redraw:conceptual"]["command"]
    shown = {html.unescape(c) for c in re.findall(r'data-cmd="(.*?)"', out, re.S)}
    assert line in shown
    assert "cd /repo" in line
    assert "git stash push" in line and REDRAW["diagram"] in line
    assert "git checkout origin/main -- docs/CM.drawio.png" in line
    assert "docs/patch.py" in line
    assert RERUN["command"] in line, "the picture on the page is re-rendered too"
    assert REBUILD in line
    # The stash comes first, or the checkout has already thrown the layout away.
    assert line.index("git stash push") < line.index("git checkout")


def test_the_hover_says_where_the_layout_goes_before_it_is_clicked(tmp_path):
    """The reassurance is the whole reason this is a stash and not a bare checkout, and a
    reader weighing it needs that *before* pressing, not in a paragraph underneath."""
    out = _widget_with(tmp_path, rerun=RERUN, redraw=REDRAW)
    # One control, one hover per face. The play's is a short sentence naming the action and
    # where it runs, plus the one thing a label cannot carry: that this throws the layout
    # away, and where it goes. What the command *is* lives on the clipboard face, which is
    # the one a reader opens to read a line before pasting it.
    tip = re.search(r'data-action="drawio-redraw:conceptual"[^>]*? data-tip="([^"]*)"',
                    out).group(1)
    assert "stash" in tip and "server" in tip
    assert REDRAW["base"] not in tip, "the base is in the command, not in a three-line hover"
    copy_tip = re.search(r'data-copy="[^"]*" data-cmd="[^"]*" data-tip="([^"]*)"',
                         out).group(1)
    assert "Copy command to paste in terminal" in copy_tip


def test_the_row_carries_a_place_for_the_command_to_report_itself(tmp_path):
    """The complaint that produced it: pressing *Update the report* re-renders a diagram
    and rebuilds the page, which was seconds of nothing whatever in front of a control
    that gave no sign it had been pressed. A reader with no feedback presses it again."""
    out = _widget_with(tmp_path, rerun=RERUN, redraw=REDRAW)
    assert '<p class="runstatus" hidden' in out
    assert 'aria-live="polite"' in out
    # Filled from the command's own output, not from a script's guess at what stage it is on.
    assert "say(status, REBUILDING.test(line)" in build.EDITOR_JS
    assert "window.HR.tail(snap)" in build.EDITOR_JS
    # And taken away by the attribute — which `display:flex` on the class would beat, so
    # the rule that restores it is load-bearing. Without it the line is on screen in every
    # copy of the page, empty, under every diagram, for ever.
    assert ".runstatus[hidden] { display:none; }" in build.CSS
    body = build.CSS[build.CSS.index(".runstatus {"):]
    assert body.index("[hidden]") < body.index(".rs-phase"), \
        "after the rule it has to beat, or specificity decides it the other way"


def test_the_reload_keeps_the_tab_and_the_scroll(tmp_path):
    """Landing at the top of the first tab after pressing a button three screens into the
    fifth one is its own small betrayal. The tab is in `location.hash`, which a reload
    keeps on its own; the scroll is the place-keeper the masthead's Rerun already uses."""
    assert "window.HR.keepPlace();" in build.EDITOR_JS
    assert "function keepPlace()" in build.SERVER_JS
    assert build.SERVER_JS.count("sessionStorage.setItem(PLACE") == 1
    # One implementation: the masthead's Rerun reaches for the same one.
    assert "var remember = window.HR.keepPlace;" in build.RERUN_JS


def test_no_way_back_is_offered_for_a_repository_that_declared_no_script(tmp_path):
    """The patch script is the reviewed repository's, not this tool's. Guessing it from a
    naming convention and running it on a reader's click is not a trade worth making, so a
    diagram with no `redraw` gets the sentence and one offer."""
    out = _widget_with(tmp_path, rerun=RERUN)
    assert "Revert the diagram" not in out and "drawio-redraw" not in out
    assert '<span class="cmd-word">Update the report</span>' in out


def test_the_play_glyph_is_hidden_by_the_attribute_and_not_by_a_class(tmp_path):
    """What this replaces: a fold whose `display:flex` beat the browser's own
    `[hidden] { display:none }`, so both commands were folded in the markup and open on
    the screen, one under the other, reading as the same line printed twice.

    The lesson survives the fold. `.cmd` is `inline-flex`, and the play glyph inside it is
    hidden with the `hidden` attribute — so no rule in this stylesheet may give the glyph
    itself a `display`, or the attribute stops working and every static copy of the report
    grows a button that cannot do anything."""
    # Comments stripped first. A rule's neighbouring comment may quote the very thing this
    # forbids — `[hidden] { display:none }` is the reason the rule exists — and a guardrail
    # that fires on its own explanation is a guardrail somebody deletes.
    css = re.sub(r"/\*.*?\*/", "", build.CSS, flags=re.S)
    glyph_rules = [r for r in css.split("}") if ".cmd-run" in r and "@media" not in r]
    assert glyph_rules, "the glyph has to be styled somewhere"
    for rule in glyph_rules:
        assert "display:" not in rule, f"a display on the glyph defeats [hidden]: {rule}"
    out = _widget_with(tmp_path, rerun=RERUN)
    assert 'class="runhere cmd-run has-word" hidden' in out
