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
# faking the model call — no test in this file makes a real `claude` call: it is slow,
# billed, and non-deterministic, none of which belong in a routine `pytest` run. What is
# pinned here instead is the contract `_logging_listing`/`privacy_verdict` hold the model
# call to: given a prompt, return `{"verdict","trace","cost_usd"}` or raise `RuntimeError`.
# --------------------------------------------------------------------------- #

DEBUG_HIT = {"file": FIXTURE_REL, "abs_file": str(REPO_ROOT / FIXTURE_REL), "line": 9,
             "column": 9, "end_line": 9, "level": "DEBUG",
             "raw_line": '        LOG.debug("cache miss");', "format": '"cache miss"',
             "text": 'LOG.debug("cache miss")',
             "args": [], "method_start": 7, "method_end": 14}


def _fake_call(verdict="safe", trace="test trace", cost=0.0021):
    calls = []

    def call(prompt):
        calls.append(prompt)
        return {"verdict": verdict, "trace": trace, "cost_usd": cost}
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
    assert '>DEBUG · Slf4jExplicit:9</a>' in out                # level folded into the label
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
    assert footer.index('class="privacy-verdict') < footer.index('DEBUG · Slf4jExplicit:9')
    assert 'class="srcref"' in footer  # still the same shared link/anchor markup
    assert "log-snippet" not in out    # the old wrapper div from the previous position is gone
    assert ".log-snippet" not in build.CSS  # and so is its corner-tag CSS, not layered under a third rule


def test_the_verdict_sits_after_the_code_with_its_trace_spelled_out(no_verdict_disk):
    """The verdict is below the <pre> block, inside the same card, and its trace is
    visible text — not only reachable by hovering a tooltip."""
    out = build._logging_listing(
        [DEBUG_HIT], REPO_ROOT,
        call=_fake_call(trace="vetId is an Integer parameter, nothing else"))
    pre_end = out.index("</pre>")
    verdict_at = out.index('class="privacy-verdict')
    figure_end = out.index("</figure>")
    assert pre_end < verdict_at < figure_end          # between the code and the card's own end
    assert "✅" in out and "<b>SAFE</b>" in out
    assert "vetId is an Integer parameter, nothing else" in out   # the trace, in plain text
    verdict_span = out[out.index('<span class="privacy-verdict'):out.index("</span>", out.index('<span class="privacy-verdict'))]
    assert "data-tip" not in verdict_span  # the trace is prose on the page, not tooltip-only


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
    assert out.count("<li>") == 4  # SAFE, DOUBT, PRIVACY, and NOT EVALUATED
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
    fake = _fake_call(verdict="privacy", trace="ownerEmail is a String field")
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
    assert result["trace"] == "boom"
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


def test_a_nonzero_exit_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="exited 1"):
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
    chain = [{"resolved": True, "file": "Foo.java", "line": 12, "note": "assigned here"}]
    ok = json.dumps({"is_error": False, "total_cost_usd": 0.0123,
                     "structured_output": {"verdict": "PRIVACY", "trace": "x is a name",
                                            "chain": chain}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    result = build._call_privacy_model("prompt")
    assert result == {"verdict": "privacy", "trace": "x is a name", "chain": chain,
                      "cost_usd": 0.0123}


def test_a_response_missing_the_chain_field_is_rejected(monkeypatch):
    """The chain is required in the schema, not bolted on optionally — a model that
    skips it is a model that did not do the tracing this feature exists for."""
    monkeypatch.setattr(build, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False,
                     "structured_output": {"verdict": "SAFE", "trace": "x"}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    with pytest.raises(RuntimeError, match="did not match"):
        build._call_privacy_model("prompt")


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
                        lambda prompt: {"verdict": "safe", "trace": "id is an int parameter",
                                       "cost_usd": 0.0})
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
    assert "id is an int parameter" in frag  # the trace, from the (mocked) model


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
