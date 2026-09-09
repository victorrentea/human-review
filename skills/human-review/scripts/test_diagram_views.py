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
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

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
    source = (HERE / "build-review-html.py").read_text()
    emitters = [line for line in source.splitlines()
                if 'class="dgmviews"' in line and "CSS" not in line]
    assert len(emitters) == 1, emitters


# ── the renders the control needs ─────────────────────────────────────────────────

def test_puml_diff_writes_the_two_extra_columns():
    sh = (HERE / "puml-diff.sh").read_text()
    assert "\\tnew_svg\\told_svg\\told_details\\n' > \"$MANIFEST\"" in sh
    assert 'render_plain "$new" "$OUT_DIR/$name.new"' in sh
    assert 'render_plain "$old" "$OUT_DIR/$name.old"' in sh


def test_puml_diff_drops_a_side_plantuml_could_not_draw():
    """PlantUML answers a diagram it cannot parse with a valid SVG reading "Syntax
    Error?". Shown behind a New button that is the loudest thing on the page."""
    sh = (HERE / "puml-diff.sh").read_text()
    body = sh.split("render_plain() {")[1].split("\n}")[0]
    assert "Syntax Error" in body and 'rm -f "$2.svg"' in body
    assert '[ -s "$1" ] || return 1' in body, "an empty side is not a diagram"


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


def test_the_instructions_sit_above_the_viewer_not_inside_one_pane(tmp_path):
    """Inserted before the first `.svgbox`, the hint lands inside the Diff pane and
    disappears on New and Old — the same one-copy assumption, in the prose."""
    assert "querySelector('.dgmviews') || diagram.querySelector('.svgbox')" \
        in build.GENSEQ_JS


def test_puml_diff_carries_the_base_sidecar_for_the_old_render():
    sh = (HERE / "puml-diff.sh").read_text()
    assert 'git show "$MERGE_BASE:${rel%.puml}.json"' in sh
    assert "\\told_details\\n' > \"$MANIFEST\"" in sh
    assert '*.genseq.puml)' in sh, "only generated sequence diagrams have a sidecar"


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


def test_a_pair_leads_with_the_scenario_and_its_deep_link(tmp_path):
    html = _pairs_fixture(tmp_path)
    assert "A scenario" in html and "vscode://file/" in html and ":2:1" in html


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
    assert "still waiting for a hand-drawn layout" not in drawn

    owed = build.drawio_widget_html(
        "conceptual",
        _drawio_set(tmp_path / "b", red=["Vet-Visit"], added=[("Vet-Visit", True)]), tmp_path)
    assert "still waiting for a hand-drawn layout" in owed
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
    assert "still waiting for a hand-drawn layout" in pane


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
    shown = re.search(r'<code>(.*?)</code>', out, re.S).group(1)
    copied = re.search(r'data-copy="(.*?)" data-tip', out, re.S).group(1)
    assert shown == copied


def test_a_run_that_recorded_nothing_offers_no_half_command(tmp_path):
    """`rerun` is written by `drawio-diff.py`; a verdict from before it did carries none.
    A command assembled from guesses is worse than no command — it is tried first."""
    out = build.drawio_widget_html("conceptual", _drawio_set(tmp_path / "assets"),
                                   tmp_path, REBUILD)
    assert "cmdline" not in out


def test_the_page_says_why_a_reload_is_not_enough(tmp_path):
    """Without the reason, the block reads as a chore. With it, it reads as the answer to
    the question the reader has just asked by pressing F5."""
    assets = _drawio_set(tmp_path / "assets")
    (assets / "conceptual-diff.json").write_text(json.dumps({
        "added": [], "removed": [], "changed": [], "moved": [], "red": [], "rerun": RERUN}))
    out = build.drawio_widget_html("conceptual", assets, tmp_path, REBUILD)
    assert "inlined" in out and "reload" in out


def test_the_copy_button_reuses_the_one_clipboard_and_the_one_toast():
    """A second clipboard-and-toast implementation for one button is how two of them end
    up behaving differently."""
    js = build.EDITOR_JS
    assert "button.copycmd" in js
    assert js.count("function copy(") == 1
    assert js.count("toast.id = 'copy-toast'") == 1


def test_the_rebuild_command_is_never_an_interpreter_that_is_about_to_vanish():
    """`sys.executable` under `uv run` is a build directory uv deletes on exit: the one
    command guaranteed to have worked would be the one guaranteed not to work again."""
    got = build.rebuild_interpreter()
    assert "/.cache/uv/builds" not in got
    assert got.startswith("python3") or got.startswith("uv run") or Path(got).exists()
