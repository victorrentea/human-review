#!/usr/bin/env python3
"""The Diff / New-Old control: three states, two buttons, one implementation.

It exists because the delta is not always right. A generated sequence diagram is built
from traces whose call *order* is not stable between runs, so `seq_puml_diff.py` reports
messages as moved that nobody moved. Rather than pretend that away, the page gives the
reader a one-click escape to the undiffed before/after and lets them judge.

Two things are worth pinning here, and they are the two that would rot quietly:

* **one component, two callers.** The generated PlantUML deltas and the hand-written
  conceptual-model section both go through `dgm_views_html`, and the behaviour is
  delegated off `document` so neither needs to register anything. A second copy of this
  in a section body would drift within a week and nothing would notice.
* **the header toggles, the body never does.** A sequence diagram is taller than the
  viewport; a stray click while scrolling or selecting text must not swap the picture.

Run with:  python3 -m pytest test_diagram_views.py
"""
from __future__ import annotations

import importlib.util
import re
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


def test_the_frame_is_thick_enough_to_read_without_looking_at_it():
    width = re.search(r"\.dgmpane \{[^}]*border:(\d+)px", build.CSS)
    assert width and int(width.group(1)) >= 4


def test_all_three_colours_are_defined_and_none_is_another_one():
    light = dict(re.findall(r"--view-(\w+):([^;]+);", build.CSS))
    assert set(light) == {"diff", "new", "old"}
    assert len(set(light.values())) == 3
    # red is the delta's own red by reference, so the frame and the strokes inside it
    # cannot drift apart, and it follows the palette into dark mode for free
    assert light["diff"] == "var(--dgm-diff)"
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
    """The conceptual model's widget is written into a section body, not generated here.
    A per-widget listener bound at build time would leave it dead."""
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
    assert "\\tnew_svg\\told_svg\\n' > \"$MANIFEST\"" in sh
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
