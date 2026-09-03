#!/usr/bin/env python3
"""What the page builder promises, checked without building the whole page.

Rendering a real guide needs a repository, a manifest, PlantUML, a recorded video and a
Code City — none of which belong in a unit test. Every emitter added since the tab layout
is a pure function of small inputs, so it is tested as one. `testpairs` (which shells out
to `ast-grep`) is exercised through its pure parts: the chapter parser. `logging` shells
out too (`git`, `ast-grep`, and `extract-snippet.py`'s own Pygments pass) and its GDPR
verdict is now a real model call — never exercised for real here, since that is slow,
billed, and non-deterministic. What is pinned instead is the contract the model call is
held to (given a prompt, return a verdict/trace/cost or raise) via a fake `call`, plus
the snippet rendering and context-gathering around it, which are cheap and real enough
to run directly against a checked-in Java fixture.

Run with:  python3 -m pytest test_build_review.py
"""
from __future__ import annotations

import html
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

# `_logging_listing` shells out to extract-snippet.py, which resolves paths against
# `git rev-parse --show-toplevel` — the same contract `logging_fragment` relies on in
# production (its `root` *is* that toplevel). So the fixture reference here has to be
# repo-root-relative, not scripts-dir-relative, and is computed rather than hand-typed
# so it survives the skill moving in the tree.
REPO_ROOT = Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True, check=True
).stdout.strip())
FIXTURE_REL = str((HERE.relative_to(REPO_ROOT) / "testdata" / "logextract" / "Slf4jExplicit.java"))


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


def test_a_genuine_zero_reads_as_a_sentence_not_as_silence():
    """A false 'no logging found' is the one answer this tab must never give — so a real
    zero (the scan ran and found nothing) has to look nothing like an empty page."""
    out = build._logging_listing([], REPO_ROOT)
    assert "<figure" not in out
    assert "None." in out
    assert "Not one logging statement" in out


# --------------------------------------------------------------------------- #
# "Data flow to here" on the page: the origin lines `logextract.py` walked back to,
# pulled into the same <pre> as the statement, with the file's own line numbers.
# --------------------------------------------------------------------------- #

def _extract_snippet():
    """`extract-snippet.py` is a hyphenated filename, so it is not importable by name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_snippet", HERE / "extract-snippet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_origin_lines_join_the_statement_in_one_reference():
    h = {"file": "a/A.java", "line": 93, "end_line": 93,
         "origins": [{"line": 89, "name": "vetId", "kind": "param", "text": ""}]}
    assert build._logging_ref(h) == "a/A.java:93,89"


def test_a_statement_with_nothing_to_trace_is_the_reference_it_always_was():
    h = {"file": "a/A.java", "line": 9, "end_line": 9, "origins": []}
    assert build._logging_ref(h) == "a/A.java:9"
    assert build._logging_ref({**h, "end_line": 11}) == "a/A.java:9-11"


def test_an_origin_inside_the_statement_is_not_quoted_twice():
    """`log.error("boom", e)` inside `catch (RuntimeException e)` on the same line: the
    origin is already on screen, and a second copy of it is not evidence."""
    h = {"file": "a/A.java", "line": 20, "end_line": 22,
         "origins": [{"line": 21, "name": "e", "kind": "param", "text": ""}]}
    assert build._logging_ref(h) == "a/A.java:20-22"


def test_the_page_caps_how_many_origin_lines_one_entry_may_pull_in():
    """The tab lists every touched Java file. An entry that grows from three lines to
    twenty to show a chain nobody asked about has made the tab worse in exactly the way
    the prose it replaced did — so the page caps on top of the extractor's own cap, and
    keeps the hops *nearest* the statement rather than an arbitrary slice."""
    h = {"file": "a/A.java", "line": 50, "end_line": 50,
         "origins": [{"line": n, "name": f"v{n}", "kind": "local", "text": ""}
                     for n in (10, 20, 30, 40, 45, 48)]}
    assert build.MAX_ORIGIN_LINES_SHOWN == 4
    assert build._logging_ref(h) == "a/A.java:50,30,40,45,48"   # 10 and 20 are the far ones


def test_the_quoted_origin_keeps_its_real_line_number_behind_a_gap_marker(tmp_path):
    """Truthful numbering is the point: the pulled-in line is numbered where it really
    lives, and the jump is marked rather than hidden — renumbering it to look adjacent
    would make the snippet a drawing of the code instead of the code."""
    src = "\n".join(f"line{n}" for n in range(1, 13)) + "\n"
    (tmp_path / "A.java").write_text(src, encoding="utf-8")
    # `render` directly, not `snippet_html`: the latter shells out and the child resolves
    # its root with `git rev-parse`, which a bare tmp_path is not.
    out = _extract_snippet().render("A.java:3,11", None, tmp_path, exact=True)
    assert '<span class="ln">3</span>' in out
    assert '<span class="ln">11</span>' in out
    assert '<span class="ln">7</span>' not in out          # not renumbered, not filled in
    assert "7 lines not shown" in out                       # and the jump is stated
    assert 'class="ln ln-gap"' in out


# --------------------------------------------------------------------------- #
# faking the model call — no test in this file makes a real `claude` call: it is slow,
# billed, and non-deterministic, none of which belong in a routine `pytest` run. What is
# pinned here instead is the contract `_logging_listing`/`privacy_verdict` hold the model
# call to: given a prompt, return `{"verdict","trace","cost_usd"}` or raise `RuntimeError`.
# --------------------------------------------------------------------------- #

DEBUG_HIT = {"file": FIXTURE_REL, "abs_file": str(REPO_ROOT / FIXTURE_REL), "line": 9,
             "column": 9, "end_line": 9, "level": "DEBUG",
             "raw_line": '        LOG.debug("cache miss");', "format": '"cache miss {}"',
             "text": 'LOG.debug("cache miss {}", id)',
             "args": ["id"], "method_start": 7, "method_end": 14}


def _fake_call(verdict="safe", values=None, cost=0.0021):
    """`values` defaults to the one clause `DEBUG_HIT`'s single argument needs; pass a
    list to model a statement with several, or an empty list to model a model that
    answered nothing."""
    calls = []
    if values is None:
        values = [{"name": "id", "verdict": verdict, "note": "test clause"}]

    def call(prompt):
        calls.append(prompt)
        return {"verdict": verdict, "values": list(values), "cost_usd": cost}
    call.calls = calls
    return call


def _raising_call(message="the model call timed out"):
    def call(prompt):
        raise RuntimeError(message)
    return call


@pytest.fixture
def no_verdict_disk(monkeypatch):
    """Isolate `_logging_listing`/`privacy_verdict` from real disk I/O: they otherwise
    read and write `<root>/.human-review/.privacy-verdicts.json`, and most of these
    tests pass `REPO_ROOT` (this very checkout, needed so `snippet_html` can resolve the
    fixture) rather than a throwaway `tmp_path` — this fixture is what keeps that safe."""
    monkeypatch.setattr(build, "_load_verdict_cache", lambda root: {})
    monkeypatch.setattr(build, "_save_verdict_cache", lambda root, cache: None)


def test_each_statement_renders_as_the_page_s_one_snippet_style(no_verdict_disk):
    """Item 4 of the redesign: no invented second code-block style — the same `.snippet`
    figure every other quoted line on the page uses, labelled `Class:line` instead of the
    full repo path."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    assert out.count('<figure class="snippet">') == 1
    assert '>Slf4jExplicit:9</a>' in out                        # the location, and only that
    assert 'DEBUG · ' not in out                # the level rides in the quoted code, not here
    assert f'>{html.escape(FIXTURE_REL)}:9</a>' not in out      # the old full-path label is gone
    assert 'badge sev-info">DEBUG</span>' not in out            # no more coloured level pill
    assert "cache miss" in out                                  # the real source line, verbatim
    assert "<table" not in out and "<details" not in out        # the coverage table is gone, period


def test_the_level_gets_no_line_of_its_own(no_verdict_disk):
    """Layout item 1: the level used to sit in its own <figcaption> row above the label —
    that whole element is gone, not just re-styled."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    assert "<figcaption" not in out
    assert "loglevel" not in out


def test_the_footer_puts_the_verdict_left_and_the_label_right(no_verdict_disk):
    """Final position (the third and last one): verdict + trace on the left of one row
    below the code, `LEVEL · Class:line` as a link at the right, in that DOM order so a
    narrow width wraps label-under-verdict rather than truncating either one."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    footer = out[out.index('<p class="log-footer">'):out.index("</p>", out.index('<p class="log-footer">')) + 4]
    assert footer.index('class="privacy-verdict') < footer.index('Slf4jExplicit:9')
    assert 'class="srcref"' in footer  # still the same shared link/anchor markup
    assert "log-snippet" not in out    # the old wrapper div from the previous position is gone
    assert ".log-snippet" not in build.CSS  # and so is its corner-tag CSS, not layered under a third rule


def test_the_verdict_sits_after_the_code_and_stands_alone_on_its_row(no_verdict_disk):
    """The verdict is below the <pre> block, inside the same card — and it is now the
    word and nothing else. The reasoning moved to the bullets under it, so a reader
    scanning a column of statements reads a column of verdicts, not of sentences."""
    out = build._logging_listing(
        [DEBUG_HIT], REPO_ROOT,
        call=_fake_call(values=[{"name": "id", "verdict": "SAFE",
                                 "note": "a cache key, nothing personal"}]))
    pre_end = out.index("</pre>")
    verdict_at = out.index('class="privacy-verdict')
    figure_end = out.index("</figure>")
    assert pre_end < verdict_at < figure_end          # between the code and the card's own end
    assert "✅" in out and "<b>SAFE</b>" in out
    verdict_span = out[out.index('<span class="privacy-verdict'):
                       out.index("</span>", out.index('<span class="privacy-verdict'))]
    assert verdict_span.endswith("<b>SAFE</b>")       # the word, and nothing after it
    assert "a cache key" not in verdict_span          # the clause is not fused onto it
    assert "data-tip" not in verdict_span             # nor hidden in a tooltip
    assert "a cache key, nothing personal" in out     # it is a bullet, in plain text
    assert out.index("</p>", verdict_at) < out.index("a cache key")   # below the verdict


def test_one_bullet_per_logged_value_named_as_the_source_writes_it(no_verdict_disk):
    """The shape the reader asked for: `vetId — just a numeric vet database id`, one row
    per value, so a three-value statement can be scanned for *which* value is the
    problem instead of read as one sentence that fused all three."""
    h = {**DEBUG_HIT, "args": ["vetId", "owner.getName()", "count"]}
    out = build._logging_listing([h], REPO_ROOT, call=_fake_call(verdict="privacy", values=[
        {"name": "vetId", "verdict": "SAFE", "note": "just a numeric vet database id"},
        {"name": "owner.getName()", "verdict": "PRIVACY", "note": "the owner's full name"},
        {"name": "count", "verdict": "SAFE", "note": "a row count"},
    ]))
    bullets = out[out.index('<ul class="log-values">'):out.index("</ul>")]
    assert bullets.count("<li") == 3
    assert "<code>vetId</code> — just a numeric vet database id" in bullets
    assert "<code>owner.getName()</code> — the owner&#x27;s full name" in bullets
    # The one row that is not fine carries the mark; a column of green ticks under a
    # green tick would be decoration.
    assert bullets.index("❌") < bullets.index("owner.getName()")
    assert "✅" not in bullets


def test_the_headline_verdict_is_the_worst_of_the_bullets(no_verdict_disk):
    """A per-value answer must never let the page come out *better* than its own worst
    row — so the headline is recomputed from the bullets, not taken on the model's word."""
    h = {**DEBUG_HIT, "args": ["vetId", "email"]}
    out = build._logging_listing([h], REPO_ROOT, call=_fake_call(verdict="SAFE", values=[
        {"name": "vetId", "verdict": "SAFE", "note": "a numeric id"},
        {"name": "email", "verdict": "PRIVACY", "note": "the owner's email address"},
    ]))
    box = out.split("privacy-legend")[0]            # the legend names all four words
    assert "<b>PRIVACY</b>" in box and "<b>SAFE</b>" not in box
    assert build._worst_verdict("safe", "doubt", "privacy") == "privacy"
    assert build._worst_verdict("safe", "safe") == "safe"
    assert build._worst_verdict() == "doubt"          # nothing to go on is never SAFE


def test_a_value_the_model_skipped_gets_its_own_row_and_costs_the_all_clear(no_verdict_disk):
    """The bullets are driven by `logextract.py`'s argument list, never by whatever the
    model chose to mention: a model that silently drops a value must not silently drop
    it from the page, and "nobody said" must not read like "nothing to say"."""
    h = {**DEBUG_HIT, "args": ["vetId", "ownerEmail"]}
    out = build._logging_listing([h], REPO_ROOT, call=_fake_call(verdict="SAFE", values=[
        {"name": "vetId", "verdict": "SAFE", "note": "a numeric id"},
    ]))
    bullets = out[out.index('<ul class="log-values">'):out.index("</ul>")]
    assert bullets.count("<li") == 2
    assert "val-unresolved" in bullets and "ownerEmail" in bullets
    assert "not assessed" in bullets
    box = out.split("privacy-legend")[0]            # the legend names all four words
    assert "<b>DOUBT</b>" in box and "<b>SAFE</b>" not in box


def test_a_clause_answered_by_root_name_still_lands_on_its_row(no_verdict_disk):
    """`owner` for `owner.getName()` is the right answer under a shorter name — accepted
    while exactly one row could be meant, and never guessed when two could."""
    rows, broken = build._value_bullets(["owner.getName()"],
                                        [{"name": "owner", "verdict": "PRIVACY",
                                          "note": "the owner's name"}])
    assert not broken and rows[0]["verdict"] == "privacy"
    rows, broken = build._value_bullets(["owner.getName()", "owner.getEmail()"],
                                        [{"name": "owner", "verdict": "PRIVACY",
                                          "note": "the owner's name"}])
    assert broken and [r["verdict"] for r in rows] == [None, None]


def test_a_statement_that_interpolates_nothing_gets_no_bullet_list(no_verdict_disk):
    """`log.debug("cache miss")` logs no value, so there is no row to write. The verdict
    alone is the whole answer, and an empty <ul> would be furniture."""
    h = {**DEBUG_HIT, "args": [], "text": 'LOG.debug("cache miss")'}
    out = build._logging_listing([h], REPO_ROOT, call=_fake_call(values=[]))
    assert "log-values" not in out
    assert "<b>SAFE</b>" in out


def test_two_statements_yield_two_boxes_and_nothing_else(no_verdict_disk):
    """The tab should be the snippet boxes and essentially nothing else."""
    other_hit = {**DEBUG_HIT, "line": 8, "level": "INFO",
                 "raw_line": '        LOG.info(...);', "format": '"..."',
                 "args": ["owner", "petId"]}
    out = build._logging_listing([other_hit, DEBUG_HIT], REPO_ROOT, call=_fake_call())
    assert out.count('<figure class="snippet">') == 2
    assert out.count('class="log-footer"') == 2
    assert out.count('class="privacy-verdict') == 2  # one per box, none in the legend


def test_the_legend_is_a_vertical_list_headed_ai_evaluation(no_verdict_disk):
    """One mark per line under a literal 'AI Evaluation:' heading — the user's exact
    wording, now accurate: it is a real model call (see the report for the naming
    discussion this superseded)."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    assert '<p class="privacy-legend-title">🤖 AI Evaluation:</p>' in out
    legend = out[out.index('<ul class="privacy-legend-list">'):]
    assert legend.count("<li>") == 4  # SAFE, DOUBT, PRIVACY, and NOT EVALUATED
    assert "SAFE" in out and "DOUBT" in out and "PRIVACY" in out and "NOT EVALUATED" in out
    assert "on purpose" in out  # the ambiguity-resolves-to-DOUBT clause


def test_a_model_call_that_fails_degrades_to_a_loud_not_evaluated(no_verdict_disk):
    """The one thing this must never do on a model failure: guess SAFE. It must read as
    a distinct, loud state instead — never blended into DOUBT, which means something
    different (the model looked and could not tell, not that it was never asked)."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT,
                                 call=_raising_call("the model call timed out"))
    assert "NOT EVALUATED" in out
    assert "the model call timed out" in out
    assert 'class="privacy-verdict warn"' in out
    assert "SAFE" not in out.split("privacy-legend")[0]  # not folded into SAFE either


def test_a_cache_hit_never_calls_the_model_again(tmp_path):
    """The point of the cache: a re-run on unchanged code neither flips the answer nor
    pays for it twice. `cache_root=tmp_path` isolates the cache file from this checkout
    while `root=REPO_ROOT` still lets `snippet_html` resolve the real fixture."""
    fake = _fake_call(verdict="privacy", values=[
        {"name": "id", "verdict": "PRIVACY", "note": "ownerEmail is a String field"}])
    out1 = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert len(fake.calls) == 1
    out2 = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert len(fake.calls) == 1  # the second run found the first run's cache entry
    assert "PRIVACY" in out1 and "PRIVACY" in out2
    assert "ownerEmail is a String field" in out2
    assert (tmp_path / ".human-review" / ".privacy-verdicts.json").is_file()


def test_the_cost_note_names_a_live_call_and_reused_cache_hits(tmp_path):
    hit_a = {**DEBUG_HIT, "line": 8, "text": 'LOG.debug("cache miss A")'}
    hit_b = {**DEBUG_HIT, "line": 9, "text": 'LOG.debug("cache miss B")'}
    fake = _fake_call(cost=0.0037)
    out = build._logging_listing([hit_a], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert "$0.0037" in out
    # A second, distinct statement is a fresh cache miss (its own line differs, so its
    # cache key differs) — the first is now a hit.
    out = build._logging_listing([hit_a, hit_b], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert "more reused from the cache" in out


def test_privacy_verdict_never_reads_a_raised_error_as_a_verdict(tmp_path):
    result = build.privacy_verdict(DEBUG_HIT, tmp_path, {}, call=_raising_call("boom"))
    assert result["verdict"] == "error"
    assert result["note"] == "boom"
    assert result["values"] == []
    assert result["cached"] is False
    assert result["cost_usd"] == 0.0


# --------------------------------------------------------------------------- #
# gathering the context a verdict is traced against
# --------------------------------------------------------------------------- #

def test_the_context_carries_the_real_enclosing_method_source():
    ctx = build._statement_context(DEBUG_HIT)
    assert f"Enclosing method ({FIXTURE_REL}:7-14):" in ctx
    assert "void run(String owner, int petId)" in ctx     # the signature, with parameters
    assert "LOG.debug(" in ctx                             # the statement itself, in place
    assert "Class fields in scope: none." in ctx


def test_a_hit_with_no_resolved_method_falls_back_to_the_bare_line():
    """The pathological case (a static initializer, say) still has to produce something
    to send — never a crash, and never silently skipping straight to a verdict."""
    h = {**DEBUG_HIT, "method_start": None, "method_end": None}
    ctx = build._statement_context(h)
    assert "No enclosing method could be resolved" in ctx
    assert 'LOG.debug("cache miss");' in ctx


def test_fields_in_scope_are_named_when_present():
    h = {**DEBUG_HIT, "_fields": [{"type": "String", "name": "ownerEmail", "line": 4},
                                   {"type": "int", "name": "retries", "line": 5}]}
    ctx = build._statement_context(h)
    assert "Class fields in scope" in ctx
    assert "String ownerEmail" in ctx and "int retries" in ctx
    assert "   4  String ownerEmail" in ctx  # numbered, so a chain hop can cite it


# --------------------------------------------------------------------------- #
# the real model call — subprocess and its failure modes, still no network
# --------------------------------------------------------------------------- #

def test_no_claude_binary_is_a_runtime_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(build, "_claude_bin", lambda: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        build._call_privacy_model("prompt")


def test_a_nonzero_exit_with_no_usable_output_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="exited 1"):
        build._call_privacy_model("prompt")


def test_a_good_answer_is_not_thrown_away_over_the_exit_code(monkeypatch):
    """`claude -p --json-schema --max-turns 1` stops on the structured-output tool call
    and can exit non-zero while stdout holds a complete, schema-conforming, already-paid
    -for response. Reading the exit code first put "the model could not be reached" on a
    page whose model *had* been reached — the one state reserved for never having asked.
    The answer decides; the exit code only colours the message when there is no answer."""
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False, "subtype": "success", "total_cost_usd": 0.02,
                     "structured_output": {"verdict": "SAFE", "values": [
                         {"name": "vetId", "verdict": "SAFE", "note": "a numeric id"}]}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout=ok, stderr=""))
    result = build._call_privacy_model("prompt")
    assert result["verdict"] == "safe"
    assert result["values"][0]["note"] == "a numeric id"


def test_a_bad_payload_still_raises_and_names_the_exit_code(monkeypatch):
    """Nothing is loosened: an exit code plus a response that misses the schema is still
    a failure, and the message says both halves so the cause is not guesswork."""
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    bad = json.dumps({"is_error": False, "structured_output": {"verdict": "SAFE"}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout=bad, stderr="oops"))
    with pytest.raises(RuntimeError, match="did not match.*exited 1.*oops"):
        build._call_privacy_model("prompt")


def test_a_response_missing_the_verdict_field_is_rejected(monkeypatch):
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False, "structured_output": {"trace": "x"}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    with pytest.raises(RuntimeError, match="did not match"):
        build._call_privacy_model("prompt")


def test_a_well_formed_response_is_parsed(monkeypatch):
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False, "total_cost_usd": 0.0123,
                     "structured_output": {
                         "verdict": "PRIVACY",
                         "values": [{"name": "x", "verdict": "PRIVACY",
                                     "note": "a name"}]}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    result = build._call_privacy_model("prompt")
    assert result == {"verdict": "privacy", "cost_usd": 0.0123,
                      "values": [{"name": "x", "verdict": "privacy", "note": "a name"}]}


def test_a_response_whose_values_are_the_wrong_shape_is_rejected(monkeypatch):
    """Shape only — whether the list *covers* the logged values is decided against
    `logextract.py`'s argument list at render time, not against the model's word."""
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    for values in ("not a list", [{"name": "x"}],
                   [{"name": "x", "verdict": "MAYBE", "note": "n"}]):
        ok = json.dumps({"is_error": False, "structured_output": {
            "verdict": "SAFE", "values": values}})
        monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                            subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
        with pytest.raises(RuntimeError, match="did not match"):
            build._call_privacy_model("prompt")


def test_the_model_is_no_longer_asked_where_a_value_came_from(monkeypatch):
    """The provenance chain used to be the model's answer, rendered as a list of
    `file:line` + the source line. `logextract.py` walks it syntactically now and the
    snippet quotes the real lines, so the schema asks for the one thing no line of Java
    says out loud — is this personal data — and a stray `chain` key is refused rather
    than quietly carried."""
    assert "chain" not in build.VERDICT_SCHEMA["properties"]
    assert build.VERDICT_SCHEMA["required"] == ["verdict", "values"]
    assert build.VERDICT_SCHEMA["additionalProperties"] is False
    assert not hasattr(build, "_render_chain")
    assert "chain-hops" not in build.CSS
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False, "structured_output": {
        "verdict": "SAFE",
        "values": [{"name": "id", "verdict": "SAFE", "note": "an int id"}]}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    assert build._call_privacy_model("prompt")["verdict"] == "safe"


def test_the_prompt_asks_for_one_short_clause_not_a_paragraph():
    """Item 3 of the redesign, enforced where it is actually decided. The trace on the
    page used to be four sentences retelling the declaration the reader can now see
    quoted above it; the instruction not to do that is the fix, so it is pinned."""
    prompt = build.VERDICT_SYSTEM_PROMPT
    assert "at most 15 words" in prompt
    assert "no line numbers" in prompt and "no file names" in prompt
    # ...and it is now per value, keyed to the argument as the source writes it.
    assert "one entry in `values` for EVERY value" in prompt
    assert "no more and no fewer" in prompt


def test_editing_the_prompt_invalidates_the_verdict_cache(tmp_path, monkeypatch):
    """A shortened `trace` instruction that kept serving the old paragraph out of cache
    would be a silent no-op, so the key hashes the system prompt too."""
    fake = _fake_call()
    build._logging_listing([DEBUG_HIT], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert len(fake.calls) == 1
    build._logging_listing([DEBUG_HIT], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert len(fake.calls) == 1                     # same prompt, same key: a cache hit
    monkeypatch.setattr(build, "VERDICT_SYSTEM_PROMPT", build.VERDICT_SYSTEM_PROMPT + " x")
    build._logging_listing([DEBUG_HIT], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert len(fake.calls) == 2                     # a different ask is a different answer


# --------------------------------------------------------------------------- #
# logging_fragment end to end — the one part of this module not exercised through pure
# functions, because it shells out to real `git`, `ast-grep` and `logextract.py`
# --------------------------------------------------------------------------- #

FOO_BASE = (
    "package fx;\n"
    "import org.slf4j.Logger;\n"
    "import org.slf4j.LoggerFactory;\n"
    "public class Foo {\n"
    "    private static final Logger log = LoggerFactory.getLogger(Foo.class);\n"
    "    void run(int id) {\n"
    "        int x = 1;\n"
    "    }\n"
    "}\n"
)
FOO_WITH_WARN = (
    "package fx;\n"
    "import org.slf4j.Logger;\n"
    "import org.slf4j.LoggerFactory;\n"
    "public class Foo {\n"
    "    private static final Logger log = LoggerFactory.getLogger(Foo.class);\n"
    "    void run(int id) {\n"
    '        log.warn("bad id {}", id);\n'
    "    }\n"
    "}\n"
)


def _tiny_java_repo(tmp_path, base_src: str):
    """A real, throwaway git repo with one Java file at `base_src` on a tagged commit
    `base` — enough for `logging_fragment` to run its real `git merge-base` / `git diff`
    / `logextract.py` / `ast-grep` pipeline end to end."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                              check=check)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = repo / "Foo.java"
    src.write_text(base_src, encoding="utf-8")
    git("add", "Foo.java")
    git("commit", "-q", "-m", "base")
    git("tag", "base")
    return repo, src


def test_logging_fragment_keeps_its_weight_with_no_header_or_card(tmp_path, monkeypatch):
    """Two coordinator asks landed together: delete the header bar (heading, count pill,
    provenance) and strip the card wrapping the snippets and legend. Neither may leave
    the tab's weight hanging off markup that no longer exists — pinned here against the
    real pipeline, not a mock, because that is the one part of this module a pure-function
    test cannot see. The model call itself IS mocked — `logging_fragment` has no `call`
    parameter of its own to inject one, so this patches `_call_privacy_model` directly,
    the same seam `privacy_verdict`'s default argument points at."""
    monkeypatch.setattr(build, "_call_privacy_model",
                        lambda prompt: {"verdict": "safe", "cost_usd": 0.0,
                                        "values": [{"name": "id", "verdict": "safe",
                                                    "note": "an int parameter"}]})
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    # The pipeline's own contract: review fixes stay uncommitted, so `changed_ranges`
    # reads the working tree, not a second commit.
    src.write_text(FOO_WITH_WARN, encoding="utf-8")
    block = {"paths": ["."], "base": "base"}
    frag, weight, changes = build.logging_fragment(block, repo)
    assert (weight, changes) == (1, 1)
    assert 'class="diagram"' not in frag             # the card wrapper is gone
    assert "On the lines this change set touches" not in frag  # the header heading is gone
    assert "logging statement" not in frag           # the count pill is gone
    assert '<figure class="snippet">' in frag
    assert "Foo:7" in frag           # the Class:line corner label
    assert "bad id" in frag          # the statement's own text, verbatim in the snippet
    assert "SAFE" in frag            # the verdict, visible below the code
    assert "an int parameter" in frag  # the value's clause, from the (mocked) model


def test_logging_fragment_keeps_its_weight_on_a_genuine_zero_too(tmp_path, monkeypatch):
    """Same guarantee on the other real path through the pipeline: a change set that adds
    no logging statement still has to render with weight 1 — the "None." sentence, not a
    dropped tab — once the header/card it used to lean on for that no longer exists. No
    statement means no verdict call at all, so nothing needs mocking here — asserted by
    never patching `_call_privacy_model` and still getting a clean render."""
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    src.write_text(FOO_BASE.replace("int x = 1;", "int x = 2;"), encoding="utf-8")
    block = {"paths": ["."], "base": "base"}
    frag, weight, changes = build.logging_fragment(block, repo)
    assert (weight, changes) == (1, 1)
    assert 'class="diagram"' not in frag
    assert "On the lines this change set touches" not in frag
    assert "None." in frag and "Not one logging statement" in frag


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


# --------------------------------------------------------------------------- #
# the Packages case: a diagrams block with nothing to show must not vanish
# --------------------------------------------------------------------------- #

PACKAGES_CONTEXT = {
    "title": "t", "summary": "<p>x</p>",
    "sections": [{"id": "s", "title": "S", "body": "<p>x</p>"}],
    "tabs": [
        {"id": "packages", "label": "Packages", "blocks": [
            {"type": "diagrams", "only": ["Packages"],
             "context": {"src": "no/such/packages.puml", "name": "Packages"}}]},
        {"id": "other", "label": "Other", "blocks": [{"type": "section", "id": "s"}]},
    ],
}


def test_a_diagrams_block_with_no_delta_and_a_context_is_kept_not_dropped(tmp_path):
    """Nothing changed the package diagram on this branch, and the block declares no
    manifest at all (no MANIFEST.tsv on disk — the state after `puml-diff.sh` found
    zero changed diagrams). A `diagrams` block alone would contribute no weight and the
    tab would silently disappear; the `context` fallback is what makes that impossible."""
    page, err = _build(tmp_path, PACKAGES_CONTEXT)
    assert 'id="packages"' in page
    assert "dropped empty tabs" not in err
    assert "Packages" not in err.partition("dropped empty tabs:")[2]


def test_a_diagrams_block_with_no_delta_and_a_context_is_struck_through(tmp_path):
    """Present, but honestly marked as context rather than as a change this branch made —
    the same convention every other unchanged tab uses."""
    page, err = _build(tmp_path, PACKAGES_CONTEXT)
    assert '<button type="button" class="tab quiet" role="tab" id="tabbtn-packages"' in page
    assert "tabs kept as context (struck through, no delta): Packages" in err


def test_a_diagrams_block_with_no_context_and_no_delta_is_still_droppable(tmp_path):
    """The failure mode the `context` fallback exists to close: a `diagrams` block with
    nothing selected and no fallback declared contributes no weight, so a tab built on it
    alone is dropped like any other empty tab. This is the trap — `context` is the fix,
    not a change to what a bare `diagrams` block does on its own."""
    content = json.loads(json.dumps(PACKAGES_CONTEXT))
    del content["tabs"][0]["blocks"][0]["context"]
    page, err = _build(tmp_path, content)
    assert 'id="packages"' not in page
    assert "dropped empty tabs: Packages" in err


def test_a_real_delta_wins_over_the_context_fallback(tmp_path):
    """When the family actually changed, the block renders the delta — never the
    unchanged-context picture — and the tab is a normal, non-struck delta tab."""
    assets = tmp_path / "assets" / "diagrams"
    assets.mkdir(parents=True)
    (assets / "MANIFEST.tsv").write_text(
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\n"
        "Packages\tpetclinic-backend/docs/packages.puml\tstructural\tmodified\t"
        "Packages.diff.puml\tPackages.diff.svg\t\n",
        encoding="utf-8",
    )
    page, err = _build(tmp_path, PACKAGES_CONTEXT)
    assert 'id="packages"' in page
    assert '<button type="button" class="tab quiet" role="tab" id="tabbtn-packages"' not in page
    assert "no diagram at" not in page.split('id="packages"')[1].split("</section>")[0]


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


def test_the_deep_link_offset_is_measured_from_the_strip_not_hardcoded(tmp_path):
    """The strip wraps to a second row once the pills outgrow the track — it already has.
    An offset written as a literal tall enough for one row clips the first heading of every
    deep-linked panel, and a second literal only moves the bug to the next tab. So the
    offset reads the strip's measured height, and the script publishes that measurement."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    offset = [l for l in css.splitlines() if "scroll-margin-top" in l]
    assert offset, "no deep-link offset rule at all"
    assert all("var(--strip-h" in l for l in offset), offset
    # …and the target of a hash inside a panel gets it too, not only the panel itself.
    assert any(".panel [id]" in l for l in offset), offset
    # The measurement itself: taken from whatever is actually stuck to the top of the
    # viewport — the masthead the strip travels in — and re-taken when it resizes.
    assert "setProperty('--strip-h'" in page
    assert "sticky.getBoundingClientRect().height" in page
    assert "strip.closest('.masthead')" in page
    assert "ResizeObserver" in page


# ---------------------------------------------------------------------------
# The masthead and the strip: how much of the viewport the page spends before
# its first word, and whether the strip stays reachable once it is spent.


def test_the_kinds_of_acceptance_evidence_are_cards_not_a_paragraph(tmp_path):
    """"Is there anything at this level at all?" is the question the Requirements tab is
    read with, and a run of prose with bold lead-ins answers it only for whoever reads
    every sentence. One card per kind answers it by looking — including the kind nothing
    covers, which keeps its card and says so."""
    page, _ = _build(tmp_path, dict(BARE, sections=[
        {"id": "one", "title": "One",
         "body": '<div class="evidence"><section class="evi e2e"><h4>e2e</h4></section>'
                 '<section class="evi none"><h4>unit</h4></section></div>'},
        {"id": "two", "title": "Two", "body": "<p>b</p>"}]))
    css = page[page.index("<style>"):page.index("</style>")]
    assert ".evidence { display:grid" in css
    # Each kind carries its own colour, and the one nothing covers is not a fourth
    # shade of grey.
    for kind in (".evi.e2e", ".evi.api", ".evi.unit", ".evi.none"):
        assert kind + " {" in css, kind
    assert '<section class="evi e2e">' in page


PR = dict(BARE, pr={"number": 37, "title": "Link Visit with Vet",
                    "url": "https://github.com/victorrentea/petclinic/pull/37",
                    "repo": "https://github.com/victorrentea/petclinic",
                    "branch": "test-pr", "base": "main"},
          subtitle="A visit now records the vet that attended it.")


def test_the_page_is_named_the_way_the_reviewer_s_other_tabs_name_it(tmp_path):
    """The content file's own title is a sentence about the change; the reviewer is
    looking at a pull request. `GH#37 Link Visit with Vet` is the name that matches their
    notifications, their tabs and their `gh pr` output, and the number is the link."""
    page, _ = _build(tmp_path, PR)
    head = page[page.index("<h1>"):page.index("</h1>")]
    assert "GH#37" in head and "Link Visit with Vet" in head
    assert "https://github.com/victorrentea/petclinic/pull/37" in head


def test_without_a_pr_block_the_title_is_the_one_the_content_file_wrote(tmp_path):
    page, _ = _build(tmp_path, BARE)
    assert "<h1>t</h1>" in page
    assert "GH#" not in page


def test_the_two_refs_the_page_compares_lead_the_scope_bar_and_are_clickable(tmp_path):
    """"Against what, again?" is asked halfway down the ninth tab, not while reading the
    first sentence — so the refs live in the pinned masthead. They lead the scope bar,
    which is the row that already answers *how much*, and each opens its own page on
    GitHub."""
    page, _ = _build(tmp_path, dict(PR, scope=[{"label": "files", "value": "40"}]))
    bar = page[page.index('<div class="scopebar">'):]
    bar = bar[:bar.index("</div>")]
    assert "https://github.com/victorrentea/petclinic/tree/test-pr" in bar
    assert "https://github.com/victorrentea/petclinic/tree/main" in bar
    # Before every measurement of them: two refs and six numbers about those refs are one
    # thought, and the refs are the half that says what the numbers are of.
    assert bar.index("tree/test-pr") < bar.index("tree/main") < bar.index("files")
    # The page's own tooltip, never the native one nobody waits for.
    assert "title=" not in bar
    assert "data-tip=" in bar


def test_the_refs_are_out_of_the_title_row(tmp_path):
    """The title row is the PR and its score, on one line. Anything else in it is what
    pushed the masthead onto a second row."""
    page, _ = _build(tmp_path, PR)
    head = page[page.index("<h1>"):page.index("</h1>")]
    assert "test-pr" not in head and "(main)" not in head


def test_the_note_is_not_in_the_masthead_at_all(tmp_path):
    """It is the first thing the Overview says, and a block that never scrolls cannot
    spend its width on a sentence that is read once. `subtitle` stays in the content
    file, and still renders on a page with no `pr` block."""
    page, _ = _build(tmp_path, PR)
    mast = page[page.index('<header class="masthead">'):page.index("</header>")]
    assert "A visit now records the vet that attended it." not in mast


def test_the_title_row_is_one_line_and_the_title_is_what_gives(tmp_path):
    """The score must never be pushed onto a second row by a long PR title."""
    page, _ = _build(tmp_path, PR)
    css = page[page.index("<style>"):page.index("</style>")]
    assert ".titlerow.oneline { flex-wrap:nowrap;" in css
    assert "text-overflow:ellipsis" in css[css.index(".titlerow.oneline h1 {"):]
    assert '<div class="titlerow oneline">' in page


def test_title_refs_chips_and_tabs_are_one_block_that_does_not_scroll(tmp_path):
    """Four bands that scrolled away, leaving the strip pinned alone over the text, are
    now one masthead. Every one of them answers a question a reader has *while* reading
    a tab, so they travel together — and the title starts at the top edge instead of
    behind a gutter that would then be pinned there for the whole read."""
    page, _ = _build(tmp_path, PR)
    mast = page[page.index('<header class="masthead">'):page.index("</header>")]
    for part in ('<div class="titlerow oneline">', '<div class="scopebar">',
                 '<div class="tabstrip"'):
        assert part in mast, part
    css = page[page.index("<style>"):page.index("</style>")]
    assert ".wrap:has(.masthead) { padding-top:" in css


def test_a_page_with_no_tabs_grows_no_masthead(tmp_path):
    """Nothing to pin it for, and the single-column guide keeps the heading it had."""
    page, _ = _build(tmp_path, {"title": "t", "sections": [
        {"id": "one", "title": "One", "body": "<p>a</p>"}]})
    assert '<header class="masthead">' not in page
    assert '<div class="titlerow">' in page


def test_the_show_all_button_says_what_it_does_next(tmp_path):
    """It is a toggle, and the reader pressing it is on their way to seeing every tab at
    once; the way back — one tab at a time — is what the label has to name, because that
    is the state the button is the only route out of."""
    page, _ = _build(tmp_path, BARE)
    assert "(single)</button>" in page
    assert ">show all<" not in page


def test_every_pill_still_has_to_fit_on_one_row(tmp_path):
    """The strip wrapping to a second row used to cost a scroll past it; inside a
    masthead that row is on screen for the whole read. The labels are not abbreviated,
    so the room comes out of the padding and the type."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    assert css.rindex("button.tab { padding:0 .5rem;") > css.rindex("padding:0 .85rem")
    assert css.rindex("button.allbtn { padding:0 .5rem;") > css.rindex("padding:0 .7rem;")


def test_the_masthead_is_pinned_to_the_top_of_the_viewport(tmp_path):
    """A reviewer answers a dozen questions in whatever order their doubt takes them, from
    wherever they are in a panel. `position:sticky; top:0` is the whole mechanism — a
    `position:relative` here, or a `top` that is not 0, and the tabs sail off the screen
    and every jump back costs a scroll to the top first.

    It is the *masthead* that sticks, not the strip inside it: which change this is and
    what it is against are questions asked halfway down a diff, and a strip pinned alone
    over the text answered neither."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    rule = css[css.index(".masthead { position:"):]
    rule = rule[:rule.index("}")]
    assert "position:sticky" in rule, rule
    assert "top:0" in rule, rule
    # And exactly one of the two sticks: a sticky strip inside a sticky masthead is two
    # blocks racing for the same top edge.
    inner = css[css.index(".masthead .tabstrip {"):]
    assert "position:static" in inner[:inner.index("}")], inner[:200]


def test_the_pinned_state_is_observed_not_assumed(tmp_path):
    """The strip earns an edge only while it is actually pinned over the text; in the
    masthead it must look like part of the masthead. `position:sticky` exposes no state to
    CSS, so the class is set from the strip's own rendered top — never from a scroll
    threshold, which would be a second hardcoded copy of the masthead's height and would
    go wrong the moment the masthead changes (it just did)."""
    page, _ = _build(tmp_path, BARE)
    assert ".masthead.pinned" in page
    assert "classList.toggle('pinned'" in page
    assert "sticky.getBoundingClientRect().top" in page


def test_shrinking_the_strip_did_not_turn_its_height_into_a_constant(tmp_path):
    """The companion to `test_the_deep_link_offset_is_measured_from_the_strip_not_hardcoded`,
    from the other side: the strip was made shorter by trimming padding, row gap and pill
    line-height, and every one of those is a value the browser resolves. The instant anyone
    "simplifies" this into `height:` or `--strip-h:` with a literal, the deep-link offset
    stops tracking the second row and starts clipping headings again."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    strip = css[css.index(".tabstrip { position:"):]
    strip = strip[:strip.index("}")]
    assert "height:" not in strip, "the strip must be sized by its content, not set: " + strip
    assert "--strip-h:" not in css, "the measurement belongs to the script, not the sheet"
    assert "setProperty('--strip-h', h + 'px')" in page


def test_the_masthead_does_not_reopen_its_vertical_gaps(tmp_path):
    """Four stacked rows — title, subtitle, chips, strip — used to spend 281px before a
    word of the review at 1280px. Every row carries facts and every one stayed; what went
    was the air between them. These are the four cushions that were closed, pinned so a
    later 'restore the breathing room' has to be a deliberate act rather than a merge."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]

    def decl(sel):
        at = css.index(sel + " {")
        return css[at:css.index("}", at)]

    def bottom_margin(sel):
        m = [t for t in decl(sel).split("margin:")[1].split(";")[0].split()]
        return float(m[-1].rstrip("rem")) if m[-1].endswith("rem") else 0.0

    assert float(decl(".wrap").split("padding:")[1].split()[0].rstrip("rem")) <= 1.5
    assert float(decl("h1").split("font-size:")[1].split(";")[0].rstrip("rem")) <= 1.6
    assert bottom_margin(".sub") <= 0.7
    assert bottom_margin(".scopebar") <= 0.7
    # The strip's own top margin is the fourth gap, and the largest of them.
    assert float(decl(".tabstrip").split("margin:")[1].split()[0].rstrip("rem")) <= 0.7


def test_a_page_link_the_film_never_showed_rides_in_the_transcript(tmp_path):
    """An app link whose phrase appears in no caption is a statement about the coverage of
    the film, so it belongs to the cue list rather than to a paragraph of page prose under
    it. Two things have to hold or the fact is worse off than when it had its own block:
    it must be inside the transcript, and it must be pinned to the floor of that scroller —
    the cue list overflows at six captions, and a note about what was NOT filmed is the one
    nobody scrolls down to look for."""
    content = {
        "title": "t", "summary": "<p>{{tabcount}}</p>",
        "sections": [{"id": "vid", "title": "", "body": "",
                      "video": "assets/f.webm",
                      "appLinks": [{"href": "http://localhost:4200/visits",
                                    "label": "all visits"}]}],
        "tabs": [{"id": "vid", "label": "Demo",
                  "blocks": [{"type": "section", "id": "vid"}]}],
    }
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "f.cues.json").write_text(
        json.dumps([{"t": 1.0, "text": "something else entirely"}]), encoding="utf-8")
    page, _ = _build(tmp_path, content)

    ol = page[page.index('<ol class="transcript">'):]
    ol = ol[:ol.index("</ol>")]
    assert "all visits" in ol, "the unplaced link left the transcript"
    assert 'class="uncovered"' in ol
    # Never a paragraph of its own again.
    assert "Touched but not filmed" not in page
    assert "http://localhost:4200/visits" not in page.split("</ol>")[1]

    css = page[page.index("<style>"):page.index("</style>")]
    pinned = css[css.index(".transcript li.uncovered {"):]
    pinned = pinned[:pinned.index("}")]
    assert "position:sticky" in pinned and "bottom:" in pinned, pinned


def test_the_coverage_row_is_not_a_seek_target(tmp_path):
    """It carries no `data-t`, because there is no frame to seek to. The caption handler
    must therefore select on `data-t` and not on `li`: `parseFloat(undefined)` is NaN,
    assigning NaN to `video.currentTime` throws, and a throw in that click handler kills
    every script emitted after it — the tab strip included, which would leave every panel
    on screen at once with no way to hide them."""
    page, _ = _build(tmp_path, BARE)
    assert ".transcript li[data-t]" in page
    assert "querySelectorAll('.transcript li')" not in page


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))




# ── the footer's /human-review mention becomes the repo it names ─────────────────
# A reader who wants to copy this toolset has one obvious place to look, and the
# footer is it. The slash-command spelling only means something to somebody who
# already has the skill installed, so the mention is replaced by the address rather
# than merely linked — and doing it in the builder means no author has to remember
# it on any run.
def test_the_footer_mention_becomes_the_public_repo_url():
    out = build._link_home("Built by /human-review against the running stack.")
    assert ">/human-review<" not in out
    assert ">https://github.com/victorrentea/human-review</a>" in out
    assert 'href="https://github.com/victorrentea/human-review"' in out


# ── the page does not editorialise about its own honesty ─────────────────────────
# The sentence was true and it was still the first thing a reviewer read. Stripped
# in the builder, not only in the writing guidance, because content files outlive
# the instructions that produced them.
def test_the_methodology_boilerplate_is_stripped_from_the_footer():
    out = build._link_home(
        "Built by /human-review on 2 Sep 2026. Every snippet is cut from the working "
        "tree at build time; every number on this page was measured by the step that "
        "produced it."
    )
    assert "working tree at build time" not in out
    assert "measured by the step" not in out
    assert "Built by" in out


def test_only_the_first_mention_is_linked():
    out = build._link_home("/human-review ran; see /human-review for the source.")
    assert out.count("<a href=") == 1


def test_an_already_linked_footer_is_left_alone():
    already = 'Built by <a href="https://example.com/human-review">/human-review</a>.'
    assert build._link_home(already) == already


def test_a_footer_without_the_mention_is_untouched():
    assert build._link_home("Measured by the step that produced it.") == \
        "Measured by the step that produced it."
    assert build._link_home("") == ""


# --- what this branch added, per line -------------------------------------------------
# The reviewer's first question about a quoted test is "is this new, or an old test with a
# line in it?" These pin both answers, and the third case that must not regress: a file
# git cannot be asked about renders exactly as it always did.

def _tiny_repo(tmp_path, base_lines, head_lines):
    """A two-commit repo: `main` holds base_lines, HEAD holds head_lines."""
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True,
                       capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    f = tmp_path / "a.ts"
    f.write_text("\n".join(base_lines) + "\n")
    git("add", "-A"); git("commit", "-qm", "base")
    git("branch", "-f", "origin/main", "main")      # the ref the renderer diffs against
    f.write_text("\n".join(head_lines) + "\n")
    git("add", "-A"); git("commit", "-qm", "head")
    return f


def test_added_lines_come_from_git_not_a_list(tmp_path):
    """An old block with one line added is `changed`; a wholly new block is `new`."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    old = ["const a = 1;", "const b = 2;"]
    new = ["const a = 1;", "const b = 2;", "const c = 3;"]
    _tiny_repo(tmp_path, old, new)
    lines = new

    assert es.added_lines("a.ts", tmp_path) == frozenset({3})

    changed = es.block_status("a.ts", tmp_path, [(1, 3)], lines, "test")
    assert changed["diff"] == "changed"
    assert changed["label"] == "1 line changed", changed
    assert changed["added"] == 1 and changed["total"] == 3

    # The same window, restricted to the new line only, is not "one line of three" - it is
    # the whole window, so it reads as new rather than as a change to something older.
    whole = es.block_status("a.ts", tmp_path, [(3, 3)], lines, "test")
    assert whole["diff"] == "new" and whole["label"] == "new test", whole


def test_rendered_lines_carry_the_marker_and_the_badge(tmp_path):
    """The marking is in a gutter column, never a background behind the code: green is
    already spent on coverage one column to the left."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    _tiny_repo(tmp_path, ["const a = 1;", "const b = 2;"],
               ["const a = 1;", "const b = 2;", "const c = 3;"])
    out = es.render("a.ts:1,3", None, tmp_path, exact=True)
    assert '<span class="ln-row added">' in out
    assert '<span class="dm">+</span>' in out
    assert 'class="code-badge" data-diff="changed"' in out
    assert "diff-changed" in out                      # unchanged lines recede
    # the marker never becomes a background on the code itself
    assert 'class="ln-row added" style' not in out


def test_a_snippet_git_cannot_be_asked_about_is_unmarked(tmp_path):
    """No repo, no ref, no marking - and no claim that the file is untouched."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    (tmp_path / "a.ts").write_text("const a = 1;\nconst b = 2;\n")
    out = es.render("a.ts:1-2", None, tmp_path, exact=True)
    assert "code-badge" not in out
    assert 'class="dm"' not in out
    assert '<span class="ln">1</span>' in out          # everything else unchanged


# --------------------------------------------------------------------------- #
# the entry point behind a call arrow
# --------------------------------------------------------------------------- #

CONTROLLER = '''package x.rest;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/owners")
public class OwnerRestController {
    @GetMapping(produces = "application/json")
    @ApiResponse(responseCode = "200",
            content = @Content(mediaType = "application/json",
                    array = @ArraySchema(schema = @Schema(implementation = OwnerDto.class))))
    public List<OwnerDto> listOwners() {
        return List.of();
    }

    @PostMapping("{ownerId}/pets/{petId}/visits")
    // Booking is one unit of work.
    @Transactional
    public ResponseEntity<Void> addVisitToOwner(@PathVariable int ownerId,
            @RequestBody VisitFieldsDto dto) {
        return null;
    }
}
'''


def _spring_repo(tmp_path):
    src = tmp_path / "backend/src/main/java/x/rest"
    src.mkdir(parents=True)
    (src / "OwnerRestController.java").write_text(CONTROLLER, encoding="utf-8")
    build.spring_handlers.cache_clear()
    return build.spring_handlers(tmp_path)


def test_a_route_resolves_to_the_method_that_answers_it(tmp_path):
    """Class-level base plus method-level path, and the declaration under the annotation -
    not the annotation's own line, which is where a reviewer would land on `@Transactional`."""
    handlers = _spring_repo(tmp_path)
    rel, line, name = handlers["POST /api/owners/{ownerId}/pets/{petId}/visits"]
    assert rel == "backend/src/main/java/x/rest/OwnerRestController.java"
    assert name == "OwnerRestController.addVisitToOwner"
    assert CONTROLLER.splitlines()[line - 1].strip().startswith("public ResponseEntity<Void>")


def test_a_media_type_is_not_mistaken_for_a_route(tmp_path):
    """`produces = "application/json"` is the first string literal in the arguments, and a
    wrapped `@ApiResponse` under it looks exactly like a method declaration to a line scan."""
    handlers = _spring_repo(tmp_path)
    rel, line, name = handlers["GET /api/owners"]
    assert name == "OwnerRestController.listOwners"
    assert "/api/owners/application/json" not in handlers


def test_a_call_arrow_carries_its_handler_into_the_panel(tmp_path):
    """The sidecar the generator filed knows the route and nothing about the code, so the
    entry point is hung on it here, against this checkout."""
    _spring_repo(tmp_path)
    index = build._with_handlers({"details": {
        "aaa": {"title": "Browser → Backend: POST /api/owners/{ownerId}/pets/{petId}/visits",
                "steps": [{"label": "request body", "text": "{}"}]},
        "bbb": {"title": "Backend → Browser: 200", "steps": [{"label": "response body",
                                                                  "text": "{}"}]},
    }}, tmp_path)
    handler = index["details"]["aaa"]["handler"]
    assert handler["name"] == "OwnerRestController.addVisitToOwner"
    assert handler["href"].startswith("vscode://file/")
    assert handler["href"].endswith(":1")
    assert "handler" not in index["details"]["bbb"]


def test_a_route_this_checkout_no_longer_serves_gets_no_row(tmp_path):
    """A dead link into a method that is gone is worse than the route on its own."""
    _spring_repo(tmp_path)
    index = build._with_handlers({"details": {
        "aaa": {"title": "Browser → Backend: DELETE /api/gone", "steps": []},
    }}, tmp_path)
    assert "handler" not in index["details"]["aaa"]


# --------------------------------------------------------------------------- #
# requirements, and the tests nested under them
# --------------------------------------------------------------------------- #

MANIFEST = [
    {"name": "create_withVet", "path": "src/test/VisitTest.java", "status": "added", "line": 226},
    {"name": "update_ok", "path": "src/test/VisitTest.java", "status": "modified", "line": 211},
    {"name": "vet_column_removed", "path": "src/test/VisitTest.java", "status": "deleted",
     "line": 240},
    {"name": "old_scenario", "path": "features/gone.feature", "status": "deleted",
     "line": None, "gone": True},
    {"name": "create_withVet", "path": "src/test/OtherTest.java", "status": "added", "line": 12},
]


def _idx(rows=None):
    return build.test_index(rows if rows is not None else MANIFEST[:-1])


def test_a_test_is_looked_up_by_name_alone_when_that_is_unambiguous():
    rows = build.resolve_tests([{"name": "update_ok"}], _idx(), Path("/repo"))
    assert rows[0]["status"] == "modified" and rows[0]["line"] == 211


def test_a_name_that_occurs_in_two_changed_files_asks_for_the_path():
    with pytest.raises(SystemExit) as e:
        build.resolve_tests([{"name": "create_withVet"}], build.test_index(MANIFEST), Path("/repo"))
    assert "add a 'path'" in str(e.value)
    rows = build.resolve_tests([{"name": "create_withVet", "path": "src/test/OtherTest.java"}],
                               build.test_index(MANIFEST), Path("/repo"))
    assert rows[0]["line"] == 12


def test_a_test_the_change_set_never_touched_is_found_in_the_file_and_called_unchanged(tmp_path):
    """A requirement is often pinned by a test nobody edited. Saying so is worth a row —
    but the row still has to be real, so the file is parsed for the declaration."""
    f = tmp_path / "src" / "test" / "OldTest.java"
    f.parent.mkdir(parents=True)
    f.write_text("class OldTest {\n  @Test\n  void alreadyThere() {\n  }\n}\n")
    rows = build.resolve_tests(
        [{"name": "alreadyThere", "path": "src/test/OldTest.java"}], _idx(), tmp_path)
    assert rows[0]["status"] == "unchanged" and rows[0]["line"] == 3


def test_a_coverage_claim_that_names_no_real_test_fails_the_build(tmp_path):
    """The same rule as a stale `refs` entry: a link that goes nowhere costs the reviewer's
    trust in every other link on the page."""
    f = tmp_path / "src" / "test" / "OldTest.java"
    f.parent.mkdir(parents=True)
    f.write_text("class OldTest {\n}\n")
    with pytest.raises(SystemExit) as e:
        build.resolve_tests([{"name": "imagined", "path": "src/test/OldTest.java"}],
                            _idx(), tmp_path)
    assert "no test called 'imagined'" in str(e.value)


def test_each_state_gets_the_page_s_own_added_removed_vocabulary():
    out = build.render_tests(MANIFEST[:4], Path("/repo"))
    assert '<span class="tflag added">new</span>' in out
    assert '<span class="tflag changed">modified</span>' in out
    assert out.count('<span class="tflag removed">deleted</span>') == 2


def test_a_test_row_is_the_same_editor_link_every_other_reference_on_the_page_is():
    out = build.render_tests([MANIFEST[0]], Path("/repo"))
    assert 'href="vscode://file//repo/src/test/VisitTest.java:226:1"' in out
    assert 'class="srcref testref"' in out
    assert "VisitTest.java:226" in out


def test_a_deleted_test_whose_file_survives_still_opens_at_the_gap():
    out = build.render_tests([MANIFEST[2]], Path("/repo"))
    assert 'href="vscode://file//repo/src/test/VisitTest.java:240:1"' in out


def test_a_test_whose_file_is_gone_gets_no_link_rather_than_a_dead_one():
    out = build.render_tests([MANIFEST[3]], Path("/repo"))
    assert "vscode://" not in out
    assert 'class="srcref testref tgone"' in out


def test_the_tests_are_nested_under_the_requirement_they_belong_to():
    out = build.render_requirements(
        [{"text": "<b>R1.</b> A visit names its vet.",
          "tests": [{"name": "update_ok"}]}], _idx(), Path("/repo"))
    li = out.split("<li>", 1)[1]
    assert li.index("R1.") < li.index('<ul class="req-tests">')
    assert li.index('<ul class="req-tests">') < li.index("</li>")


def test_a_requirement_list_that_declares_no_tests_renders_only_its_prose():
    out = build.render_requirements([{"text": "<b>R2.</b> Nothing pins this yet."}],
                                    _idx(), Path("/repo"))
    assert "req-tests" not in out and "R2." in out


def test_a_section_with_no_requirements_at_all_renders_exactly_as_before():
    assert build.render_requirements([], _idx(), Path("/repo")) == ""


def test_naming_tests_without_a_manifest_is_a_content_error(tmp_path):
    problems = build.validate(
        {"sections": [{"id": "requirements", "title": "Requirements",
                       "requirements": [{"text": "R1", "tests": [{"name": "x"}]}]}]}, tmp_path)
    assert any("no top-level 'testChanges' manifest" in p for p in problems)


def test_a_manifest_that_was_never_generated_is_named_before_anything_is_built(tmp_path):
    problems = build.validate({"testChanges": "assets/test-changes.json"}, tmp_path)
    assert any("run scripts/test-changes.py first" in p for p in problems)


# ── open calls and applied fixes are one list, numbered straight through ─────────
# Two lists that both start at 1 make a reviewer add them up by hand to answer the
# only question they had: how much did the automated passes find? The counter is
# continued with `counter-reset`, so the assertion is on the offset the autofix
# list starts from, not on rendered text a browser computes.
def test_the_autofix_list_continues_the_findings_numbering():
    findings = [{"title": "a", "body": "x"}, {"title": "b", "body": "y"},
                {"title": "c", "body": "z"}]
    build.render_findings(findings)
    out = build.render_autofixes([{"title": "d"}])
    assert 'counter-reset:f 3' in out


def test_an_empty_findings_list_still_starts_the_fixes_at_one():
    build.render_findings([])
    assert "counter-reset:f 0" in build.render_autofixes([{"title": "d"}])


# ── the number bubble wears the severity's colour ───────────────────────────────
# The left margin is the triage column. A uniform accent bubble made every item look
# equally urgent and left the badge doing all the work.
def test_the_number_bubble_carries_the_severity_class():
    out = build.render_findings([{"title": "a", "body": "x", "severity": "high"}])
    assert 'class="n-high"' in out
    assert 'class="badge sev-high"' in out


def test_an_item_with_no_severity_falls_back_to_info():
    out = build.render_findings([{"title": "a", "body": "x"}])
    assert 'class="n-info"' in out


# ── an applied fix recedes, but keeps its place in the list ─────────────────────
def test_applied_fixes_render_greyed_out_on_the_same_list():
    out = build.render_autofixes([{"title": "d"}])
    assert '<li class="fixed">' in out
    assert 'class="findings"' in out
    assert 'sev-fixed' in out


# ── who raised it ───────────────────────────────────────────────────────────────
# Optional, because nothing downstream of the two passes records provenance. An item
# that does not claim a source renders without one rather than being attributed to a
# guess.
def test_the_source_is_shown_when_the_content_file_names_one():
    out = build.render_findings([{"title": "a", "body": "x", "source": "/code-review"}])
    assert '<span class="f-src">/code-review</span>' in out


def test_no_source_stamp_when_none_was_recorded():
    assert 'f-src' not in build.render_findings([{"title": "a", "body": "x"}])


# ── the unified-diff parser ─────────────────────────────────────────────────────
# The line numbers are the part a reader trusts without checking, so they are what
# the test pins: both gutters have to keep counting across a hunk of mixed lines.
def test_the_diff_parser_numbers_both_sides():
    rows = build._parse_unified(
        "diff --git a/f b/f\n"
        "index 1111111..2222222 100644\n"
        "--- a/f\n+++ b/f\n"
        "@@ -10,3 +10,3 @@ void m() {\n"
        " keep\n-gone\n+new\n keep2\n"
    )
    assert [r[0] for r in rows] == ["hunk", "ctx", "del", "add", "ctx"]
    assert rows[1][1:3] == (10, 10)      # context advances both sides
    assert rows[2][1] == 11 and rows[2][2] is None    # a deletion is old-side only
    assert rows[3][1] is None and rows[3][2] == 11    # an addition is new-side only
    assert rows[4][1:3] == (12, 12)


def test_a_trailing_newline_does_not_become_a_context_row():
    rows = build._parse_unified("@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert [r[0] for r in rows] == ["hunk", "del", "add"]


# ── the GitHub remote, read rather than configured ──────────────────────────────
def test_both_github_remote_spellings_resolve_to_the_same_repo(tmp_path, monkeypatch):
    import subprocess as sp
    for url in ("https://github.com/victorrentea/petclinic.git",
                "git@github.com:victorrentea/petclinic"):
        repo = tmp_path / url.replace("/", "_").replace(":", "_")
        repo.mkdir()
        sp.run(["git", "init", "-q", str(repo)], check=True)
        sp.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
        assert build.github_blob_base(repo) == "https://github.com/victorrentea/petclinic"


def test_a_non_github_remote_gets_no_link(tmp_path):
    import subprocess as sp
    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    sp.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
            "https://gitlab.com/x/y.git"], check=True)
    assert build.github_blob_base(tmp_path) is None


# ── a committed fix shows its own commit, not everything since ──────────────────
# `base` alone diffs against the working tree, which buries a one-line fix in every
# unrelated edit that landed on the branch afterwards. `head` pins the right side.
def _repo_with_two_commits(tmp_path):
    import subprocess as sp
    r = tmp_path / "repo"
    r.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    sp.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    f = r / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True)
    f.write_text("one\nTWO\nthree\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "the fix"], check=True)
    f.write_text("one\nTWO\nthree\nunrelated\n")     # left uncommitted, on purpose
    return r


def test_head_pins_the_right_side_to_the_commit(tmp_path):
    r = _repo_with_two_commits(tmp_path)
    out = build.diff_html("a.txt", "HEAD^", r, head="HEAD")
    assert "+1</span>" in out and "&minus;1</span>" in out   # only the fix
    assert "unrelated" not in out                            # not the working tree


def test_without_head_the_working_tree_is_the_right_side(tmp_path):
    r = _repo_with_two_commits(tmp_path)
    out = build.diff_html("a.txt", "HEAD^", r)
    assert "unrelated" in out


def test_a_pinned_diff_drops_the_editor_link_that_would_show_something_else(tmp_path):
    # The editor link always diffs against the working tree, so it is only the same
    # comparison when the right side *is* the working tree.
    r = _repo_with_two_commits(tmp_path)
    assert "diffref" not in build.diff_html("a.txt", "HEAD^", r, head="HEAD")


def test_a_diff_with_no_before_state_is_dropped_rather_than_faked(tmp_path):
    r = _repo_with_two_commits(tmp_path)
    assert build.diff_html("nope.txt", "HEAD^", r) == ""
