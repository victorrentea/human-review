#!/usr/bin/env python3
"""What the page builder promises, checked without building the whole page.

Rendering a real guide needs a repository, a manifest, PlantUML, a recorded video and a
Code City — none of which belong in a unit test. Every emitter added since the tab layout
is a pure function of small inputs, so it is tested as one. The two that are not (`logging`
and `testpairs`, which shell out to git and to `ast-grep`) are exercised through their
pure parts: the chapter parser and the path shortener.

Run with:  python3 -m pytest test_build_review.py
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

# The module is `build-review-html.py`; a hyphen is not an identifier, so it is loaded by
# path rather than imported by name.
_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


# --------------------------------------------------------------------------- #
# the transcript, and the app links that live inside it
# --------------------------------------------------------------------------- #

CUES = [
    {"t": 1.65, "text": "Every pet’s visit list now carries a Vet column."},
    {"t": 6.89, "text": "The booking form asks who will attend — and lets you say nobody yet."},
    {"t": 16.09, "text": "Back on the owner, the new visit names the vet."},
]


def test_a_link_wraps_the_words_already_in_the_caption():
    items, unplaced = build._link_captions(
        CUES, [{"href": "http://app/owners/2", "label": "owner detail", "anchor": "visit list"}])
    assert '<a href="http://app/owners/2">visit list</a>' in items
    assert unplaced == []
    # …and only there: the other two captions are untouched.
    assert items.count("<a href=") == 1


def test_a_link_with_no_home_in_the_narration_is_reported_not_dropped():
    """The failure mode this exists to prevent: a page the change touches, quietly gone."""
    items, unplaced = build._link_captions(
        CUES, [{"href": "http://app/visits", "label": "all visits"}])
    assert "<a href=" not in items
    assert [u["label"] for u in unplaced] == ["all visits"]


def test_two_links_never_nest():
    """`owner` occurs only inside the anchor `the owner` already made. Nesting <a> is
    invalid and the inner one is unclickable, so the second link is refused that spot and
    reported as unplaced instead — never silently swallowed."""
    items, unplaced = build._link_captions(CUES, [
        {"href": "http://app/a", "anchor": "the owner", "label": "a"},
        {"href": "http://app/b", "anchor": "owner", "label": "b"},
    ])
    assert items.count("</a>") == 1
    assert '<a href="http://app/a">the owner</a>' in items
    assert [u["label"] for u in unplaced] == ["b"]


def test_the_same_phrase_in_a_later_caption_is_still_available():
    cues = CUES + [{"t": 20.0, "text": "And the owner list again."}]
    items, unplaced = build._link_captions(cues, [
        {"href": "http://app/a", "anchor": "the owner", "label": "a"},
        {"href": "http://app/b", "anchor": "the owner list", "label": "b"},
    ])
    assert items.count("</a>") == 2 and unplaced == []


def test_captions_are_escaped_before_the_anchors_go_in():
    cues = [{"t": 0.0, "text": "A <script> tag & an ampersand"}]
    items, _ = build._link_captions(cues, [])
    assert "&lt;script&gt;" in items and "&amp;" in items
    assert "<script>" not in items


def test_timestamps_render_as_minutes_and_seconds():
    items, _ = build._link_captions([{"t": 75.4, "text": "x"}], [])
    assert '<span class="ts">1:15</span>' in items


# --------------------------------------------------------------------------- #
# the video that was never recorded
# --------------------------------------------------------------------------- #

def _video_dir(tmp_path, *, filmed: bool):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "f.cues.json").write_text(json.dumps(CUES), encoding="utf-8")
    if filmed:
        (tmp_path / "assets" / "f.webm").write_bytes(b"\x1aE\xdf\xa3")
    return {"video": "assets/f.webm"}


def test_a_recorded_video_gets_a_player(tmp_path):
    out = build.video_html(_video_dir(tmp_path, filmed=True), tmp_path)
    assert '<video controls preload="metadata" src="assets/f.webm">' in out
    assert out.count("<li ") == 3


def test_an_unrecorded_video_gets_a_notice_and_keeps_its_transcript(tmp_path):
    """The first bug this page ever shipped: a <video> pointing at a file no step wrote —
    a black rectangle at 0:00 with nothing to say the film was missing rather than broken."""
    out = build.video_html(_video_dir(tmp_path, filmed=False), tmp_path)
    assert "<video" not in out
    assert "Not filmed" in out and "assets/f.webm" in out
    assert out.count("<li ") == 3, "the narration is not held hostage to the recording"


# --------------------------------------------------------------------------- #
# the embedded report
# --------------------------------------------------------------------------- #

def test_an_embed_uses_aria_label_never_title(tmp_path):
    (tmp_path / "r.html").write_text("<p>x</p>", encoding="utf-8")
    out = build.embed_html({"embed": {"src": "r.html", "label": "a report"}}, tmp_path)
    assert 'aria-label="a report"' in out
    assert ' title=' not in out, "this page has exactly one tooltip component"


def test_a_missing_embed_names_the_tool_instead_of_framing_a_404(tmp_path):
    out = build.embed_html(
        {"embed": {"src": "gone.html", "label": "x", "missing": "brew install thing"}}, tmp_path)
    assert "<iframe" not in out
    assert "gone.html" in out and "brew install thing" in out


def test_no_embed_declared_emits_nothing(tmp_path):
    assert build.embed_html({"id": "s"}, tmp_path) == ""


# --------------------------------------------------------------------------- #
# pairing a test with the sequence its run recorded
# --------------------------------------------------------------------------- #

PUML = """@startuml
participant Browser
== <color:#D40000><s>An old scenario this branch removed</s></color> ==
== [[src://petclinic-test/src/add-visit.spec.ts:26{Click to open the test} Add a visit]] ==
Browser -> Backend: listVets
== <color:#D40000>[[src://petclinic-test/src/add-visit.spec.ts:43{hint} Add a visit with a vet]]</color> ==
@enduml
"""


def test_chapters_are_read_in_the_order_the_diagram_draws_them(tmp_path):
    p = tmp_path / "d.puml"
    p.write_text(PUML, encoding="utf-8")
    assert build.chapters(p) == [
        ("petclinic-test/src/add-visit.spec.ts", 26, "Add a visit"),
        ("petclinic-test/src/add-visit.spec.ts", 43, "Add a visit with a vet"),
    ]


def test_a_struck_out_chapter_carries_no_handle_and_is_not_paired(tmp_path):
    """A scenario the branch deleted is redrawn from the base diagram without a src link.
    Pairing it with a line number would point the reader at a test that is not there."""
    p = tmp_path / "d.puml"
    p.write_text(PUML, encoding="utf-8")
    assert not any("old scenario" in title for _, _, title in build.chapters(p))


def test_a_diagram_with_no_chapters_at_all_is_not_an_error(tmp_path):
    p = tmp_path / "d.puml"
    p.write_text("@startuml\nA -> B: x\n@enduml\n", encoding="utf-8")
    assert build.chapters(p) == []
    assert build.chapters(tmp_path / "absent.puml") == []


# --------------------------------------------------------------------------- #
# the logging tab
# --------------------------------------------------------------------------- #

def test_a_java_path_shortens_to_the_layer_and_the_class():
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/domain/Visit.java"
    assert build._short_label(rel, "victor/training/petclinic/") == "main · domain/Visit.java"
    rel = "backend/src/test/java/victor/training/petclinic/guardrail/DeployTest.java"
    assert build._short_label(rel, "victor/training/petclinic/") == "test · guardrail/DeployTest.java"


def test_a_path_outside_a_java_source_root_is_left_alone():
    assert build._short_label("openapi.yaml", "") == "openapi.yaml"


def test_a_quoted_window_is_aimed_at_the_statement_inside_it():
    snippet = ('<a class="srcref" href="vscode://file//abs/A.java:46:1" '
               'data-tip="Open in VS Code">a/A.java:46-53</a>')
    hits = [{"file": "a/A.java", "line": 51, "column": 9}]
    out = build._aim_at_statement(snippet, "a/A.java:46-53", hits)
    assert "/abs/A.java:51:9" in out


def test_two_statements_in_one_window_leave_the_link_where_it_was():
    """With two candidates the choice would be a guess, and a guess is worse than the
    honest first-line link every other snippet on the page uses."""
    snippet = '<a class="srcref" href="vscode://file//abs/A.java:46:1" >a/A.java:46-53</a>'
    hits = [{"file": "a/A.java", "line": 48, "column": 5},
            {"file": "a/A.java", "line": 51, "column": 9}]
    assert build._aim_at_statement(snippet, "a/A.java:46-53", hits) == snippet


def test_a_hit_outside_the_window_is_not_used():
    snippet = '<a class="srcref" href="vscode://file//abs/A.java:46:1" >a/A.java:46-53</a>'
    hits = [{"file": "a/A.java", "line": 90, "column": 9}]
    assert build._aim_at_statement(snippet, "a/A.java:46-53", hits) == snippet


def test_quoting_fewer_statements_than_were_found_is_reported(capsys):
    part = {"id": "x", "title": "T", "body": "<p>b</p>", "snippets": []}
    build._logging_aside(part, 4, "pre-existing logging", HERE)
    assert "quotes 0 pre-existing logging statement(s) but logextract found 4" \
        in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the Overview lede, which describes the strip it sits above
# --------------------------------------------------------------------------- #

def test_the_tab_count_is_spelled_out():
    assert build.spelled(11) == "Eleven"
    assert build.spelled(7) == "Seven"
    assert build.spelled(99) == "99"


def test_a_lede_that_skips_a_tab_says_so(capsys):
    build.check_tab_enumeration("<b>Video</b> then <b>Logging</b>.", ["Video", "Behaviour", "Logging"])
    assert "never names these tabs: Behaviour" in capsys.readouterr().err


def test_a_lede_that_walks_the_tabs_out_of_order_says_so(capsys):
    build.check_tab_enumeration("<b>Logging</b> then <b>Video</b>.", ["Video", "Logging"])
    assert "different order than the strip" in capsys.readouterr().err


def test_a_lede_that_matches_the_strip_is_silent(capsys):
    build.check_tab_enumeration("<b>Video</b>, <b>Cost &amp; shape</b>.", ["Video", "Cost & shape"])
    assert capsys.readouterr().err == "", "labels are compared as they are written in HTML"


# --------------------------------------------------------------------------- #
# what the whole page must never do
# --------------------------------------------------------------------------- #

def _build(tmp_path, content) -> str:
    src = tmp_path / "content.json"
    src.write_text(json.dumps(content), encoding="utf-8")
    out = tmp_path / "review.html"
    proc = subprocess.run(
        [sys.executable, str(HERE / "build-review-html.py"), str(src), "--out", str(out)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8"), proc.stderr


BARE = {
    "title": "t", "summary": "<p>{{tabcount}} tabs: <b>Two</b>.</p>",
    "sections": [{"id": "one", "title": "One", "body": "<p>a</p>"},
                 {"id": "two", "title": "Two", "body": "<p>b</p>"}],
    "tabs": [{"id": "one", "label": "One", "blocks": [{"type": "section", "id": "one"}]},
             {"id": "two", "label": "Two", "blocks": [{"type": "section", "id": "two"}]}],
}


def test_the_tab_count_token_is_filled_in_from_the_tabs_that_were_emitted(tmp_path):
    page, _ = _build(tmp_path, BARE)
    assert "Three tabs" in page, "two declared tabs plus the synthesised Overview"
    assert "{{tabcount}}" not in page


def test_no_element_sits_outside_every_panel(tmp_path):
    """A note appended after the last </section> is on screen under all eleven tabs at
    once — the one thing on this page no tab can hide. There must not be one."""
    page, _ = _build(tmp_path, BARE)
    tail = page.split("</section>")[-1]
    assert "<p class=\"sub\">" not in tail


def test_a_section_id_that_collides_with_a_tab_id_yields_one_id_not_two(tmp_path):
    page, err = _build(tmp_path, BARE)
    assert page.count('id="one"') == 1, "the panel keeps it; the heading gives it up"
    assert "shares its id with a tab" in err


def test_the_capacity_rules_are_the_last_thing_in_the_stylesheet(tmp_path):
    """They exist to outrank `button.tab { padding:0 .85rem }`. Emitted anywhere earlier —
    behind a fragment's own stylesheet, say — the cascade silently reverts them and the
    strip quietly wraps onto two rows."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    assert css.rindex("button.tab { padding:0 .6rem; }") > css.rindex("padding:0 .85rem")
    assert css.rindex(".tabstrip .grow { flex:1 1 0; }") > css.rindex("flex:1 1 1rem")


def test_the_panels_can_be_deep_linked_past_the_sticky_strip(tmp_path):
    page, _ = _build(tmp_path, BARE)
    assert "scroll-margin-top" in page


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
