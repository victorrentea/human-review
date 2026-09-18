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

# The page builder is a package now (`hrbuild/`), and `build-review-html.py` re-exports
# every name in it so the rest of the skill still finds them here. A *patch* is the one
# thing a re-export cannot carry: rebinding `build.X` leaves the module that defines `X`
# calling the original. So the handful of tests below that replace a function reach for
# the module that owns it.
logging_tab = importlib.import_module("hrbuild.tabs.logging")
snippets = importlib.import_module("hrbuild.shared.snippets")


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


def test_an_absolute_app_link_is_left_exactly_as_written():
    """An absolute href names one specific server on purpose — a colleague's box, a staging
    deploy — and must not be re-pointed at whatever happens to be running locally."""
    items, _ = build._link_captions(
        CUES, [{"href": "http://app/owners/2", "anchor": "visit list"}])
    assert '<a href="http://app/owners/2">visit list</a>' in items
    assert "data-app" not in items


def test_a_relative_app_link_carries_its_path_for_the_runtime_to_resolve():
    """The port is picked by the host when the instance starts, so it cannot be in the page.
    The path survives in data-app; the href is only the best guess until one is pasted in."""
    items, _ = build._link_captions(
        CUES, [{"href": "/petclinic/owners/2", "anchor": "visit list"}])
    assert '<a data-app="/petclinic/owners/2" href="/petclinic/owners/2">visit list</a>' in items


def test_a_relative_link_that_found_no_caption_is_still_resolvable():
    """The "Touched but not filmed" row is a link like any other — it went dead once because
    that row built its anchor by hand instead of going through the same helper."""
    _, unplaced = build._link_captions(CUES, [{"href": "/petclinic/vets", "label": "vets"}])
    assert unplaced
    out = build.video_html({"video": "assets/none.webm",
                            "appLinks": [{"href": "/petclinic/vets", "label": "vets"}]},
                           Path("/nonexistent"))
    assert 'data-app="/petclinic/vets"' in out


def test_a_section_with_no_runtime_gets_no_bar(tmp_path):
    assert "appenv" not in build.video_html(_video_dir(tmp_path, filmed=True), tmp_path)


def test_the_runtime_bar_carries_the_command_and_the_fallback(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "./start-docker.sh up --ref abc123",
                    "base": "http://localhost:4200"}
    out = build.video_html(s, tmp_path)
    assert '<div class="appenv" data-fallback="http://localhost:4200"' in out
    assert "./start-docker.sh up --ref abc123" in out
    # The address is the front door, in a tab of its own — and it is not in the page:
    # the port is the host's to pick, so the script fills href and text alike, and only
    # once something has answered there.
    assert '<a class="appenv-url" target="_blank" rel="noopener" hidden></a>' in out
    # Both opt-in controls stay away until the environment says it offers them: a button
    # that always fails is worse than no button.
    assert "appenv-stop" not in out
    assert "appenv-reset" not in out and "data-reset" not in out


def test_the_row_reads_the_state_first_then_the_verbs_that_act_on_it(tmp_path):
    """"Offline", and the verb that changes that — or the address, and the verbs that act
    on what is answering there. The state is the subject of the row, so it leads it.

    The address used to be pushed to the far right in a text box of its own, which made
    the bar read as a form with a field waiting to be filled in."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "reset": "/__reset"}
    out = build.video_html(s, tmp_path)
    order = ["appenv-title", "appenv-state", "appenv-url", "appenv-start",
             "appenv-stop", "appenv-reset"]
    assert [out.index(c) for c in order] == sorted(out.index(c) for c in order)
    assert ">Deployed app<" in out
    rule = build.CSS[build.CSS.index(".appenv .appenv-at"):]
    assert "margin-left:auto" not in rule[:rule.index("}")], "not off in the corner"


def test_every_verb_starts_hidden_and_is_raised_by_the_script(tmp_path):
    """A verb drawn live that turns out not to apply has already been clicked by the time
    the probe corrects it, so nothing in this row ships visible.

    The `hidden` sits on the *wrapper* for the three commands and on the button for Reset,
    and that split is the design: inside a wrapper are the two faces of one verb — the
    clipboard and the play — and SERVER_JS owns which of the two is up. Two owners of
    `hidden` on one element is how a control ends up flickering between two truths."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where",
                    "reset": "/__reset"}
    out = build.video_html(s, tmp_path)
    for verb in ("start", "stop", "where"):
        assert f'<span class="appenv-act appenv-{verb}" hidden>' in out
    at = out.index("appenv-reset")
    tag = out[out.rindex("<button", 0, at):out.index(">", at) + 1]
    assert " hidden " in tag and 'aria-disabled="true"' in tag


def test_off_disk_every_verb_is_the_clipboard_for_its_own_command(tmp_path):
    """No process stands behind a page on disk, so none of these verbs can run — and the
    row says so by wearing the clipboard on all three, not by deleting them.

    It used to delete them and put a *second* row underneath carrying the same three
    commands again, as clipboards labelled `START`, `STOP`, `WHERE`. Six controls for three
    offers — and the lower row wore the rerun mark, so a page that *was* being served still
    looked like it was handing out lines to paste somewhere else. Both faces of each verb
    are in the markup of every copy, because the same file is opened both ways and only the
    script knows which."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where",
                    "reset": "/__reset"}
    out = build.video_html(s, tmp_path)
    # Nothing hides a verb off disk any more. Reset keeps its rule and is the one
    # exception: it is a POST the application answers, not a line anybody can paste, so
    # there is nothing for a clipboard there to be the honest form of.
    for verb in ("start", "stop", "where"):
        assert f".appenv:not(.appenv-served) .appenv-{verb}" not in build.CSS
    assert ".appenv:not(.appenv-served) .appenv-reset { display:none; }" in build.CSS
    # The second row is gone, name and all.
    for dead in ("appenv-manual", "appenv-cmd", "appenv-verb"):
        assert dead not in out and f".{dead}" not in build.CSS


def test_the_three_verbs_are_one_row_of_one_control_each(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where"}
    out = build.video_html(s, tmp_path)
    # All three, not just `up`. `stop` and `where` were declared for the buttons and never
    # offered to anybody, so the one reader who needed to know how the host is asked where
    # the stack is answering had to go and read the manifest.
    for verb, word in (("start", "Start"), ("stop", "Stop"), ("where", "Where")):
        assert f'<span class="appenv-act appenv-{verb}"' in out
        assert f'<span class="cmd-word">{word}</span>' in out
    # Each is the page's one command renderer with a word on it — not a fourth kind of
    # button with its own clipboard and its own idea of what a glyph means.
    assert out.count('class="copycmd cmd-copy has-word"') == 3
    assert out.count('class="runhere cmd-run has-word" hidden') == 3
    # One row. The verbs are inside it, after the state they act on.
    assert out.count('class="appenv-run"') == 1
    for verb in ("start", "stop", "where"):
        assert out.index("appenv-state") < out.index(f"appenv-{verb}")


def test_none_of_the_three_verbs_wears_the_rerun_mark(tmp_path):
    """`↻` means *this one comes round again* everywhere else on the page, which is the
    opposite of what all three of these do: Start begins something that then keeps running,
    Stop ends it, Where goes to it. The old second row wore it on all three."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where"}
    out = build.video_html(s, tmp_path)
    assert build.CMD_RUN not in out
    for glyph in (build.CMD_PLAY, build.CMD_STOP, build.CMD_OPEN):
        assert f'<span class="cmd-ico">{glyph}</span>' in out
    # Red, because Stop is the only verb in the row that takes something away — and only
    # on the play half, since copying a line destroys nothing.
    assert ".appenv .appenv-stop .cmd-run" in build.CSS


def test_the_command_is_a_clipboard_and_not_a_line_of_text(tmp_path):
    """It used to be printed here, and a `cd … && ./start-docker.sh up --ref abc123` was
    the widest thing in the Demo tab and was read exactly once. What the row carries now is
    the affordance, with the verb on it; the line is in the glyph's hover."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "./start-docker.sh up"}
    out = build.video_html(s, tmp_path)
    assert out.index("appenv-title") < out.index("appenv-start")
    assert "Run this in a terminal" not in out
    assert "<code>./start-docker.sh up</code>" not in out
    # The clipboard is `command_html`'s now, through the page's one copy-and-toast handler.
    # A second implementation for one button is how two of them end up behaving differently.
    assert "appenv-copy" not in out
    assert 'data-copy="./start-docker.sh up"' in out
    assert "Copy command to paste in terminal" in out


def test_the_stop_control_appears_only_when_a_command_is_declared(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "./start-docker.sh down --ref abc"}
    out = build.video_html(s, tmp_path)
    assert "appenv-stop" in out and '<span class="cmd-word">Stop</span>' in out
    # The command is printed for the reader now — but the *button* still sends only the id
    # of the action, and the server holds the line. Showing a command and accepting one
    # from the page are different things, and it is the second that was never on offer.
    assert "start-docker.sh down" in out
    assert build.ACTIONS["demo-env-stop"]["command"] == "./start-docker.sh down --ref abc"
    assert 'data-action="demo-env-stop"' in out


def test_the_reset_control_appears_only_when_an_endpoint_is_declared(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    out = build.video_html(dict(s, runtime={"command": "x", "base": "http://localhost:4200"}),
                           tmp_path)
    assert "appenv-reset" not in out
    s["runtime"] = {"command": "x", "base": "http://localhost:4200", "reset": "/__reset"}
    out = build.video_html(s, tmp_path)
    # "Reset DB" and not "Reset data": what it puts back is the database the demo runs
    # on, and the reviewer who is about to press it is deciding whether they mind.
    assert 'data-reset="/__reset"' in out and ">Reset DB<" in out


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


def test_a_snippet_header_shows_the_name_and_keeps_the_path_on_hover(tmp_path):
    """A snippet header answers one question — which file is this? — and a repo-relative
    Java path spends five segments on module, `src/main/java` and the org package before
    it gets there. The path is not dropped, it moves to the tooltip."""
    deep = tmp_path / "petclinic-backend/src/main/java/victor/training/petclinic/rest"
    deep.mkdir(parents=True)
    (deep / "VetRestController.java").write_text("one\ntwo\nthree\n", encoding="utf-8")
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/rest/VetRestController.java"
    out = _extract_snippet().render(f"{rel}:1-2", None, tmp_path, exact=True)
    assert ">VetRestController.java:1-2</a>" in out
    assert f'data-tip="Open in VS Code: {rel}"' in out
    assert f">{rel}:1-2<" not in out, "the ceremony is on hover, not in the face"


def test_a_snippet_of_a_file_at_the_repo_root_says_only_that_it_opens(tmp_path):
    """A tooltip repeating the name is a tooltip saying nothing."""
    (tmp_path / "README.md").write_text("one\ntwo\n", encoding="utf-8")
    out = _extract_snippet().render("README.md:1-2", None, tmp_path, exact=True)
    assert '>README.md:1-2</a>' in out
    assert 'data-tip="Open in VS Code"' in out


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
    monkeypatch.setattr(logging_tab, "_load_verdict_cache", lambda root: {})
    monkeypatch.setattr(logging_tab, "_save_verdict_cache", lambda root, cache: None)


def test_each_statement_renders_as_the_page_s_one_snippet_style(no_verdict_disk):
    """Item 4 of the redesign: no invented second code-block style — the same `.snippet`
    figure every other quoted line on the page uses, headed by the source bar every quoted
    block on this page wears, naming the file rather than the full repo path."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    assert out.count('<figure class="snippet">') == 1
    assert out.count('<div class="srcbar">') == 1               # the shared header, once
    assert '>Slf4jExplicit.java:9</a>' in out                   # the location, and only that
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


def test_the_location_leaves_the_footer_for_the_bar_every_tab_shares(no_verdict_disk):
    """The location has moved for the last time. It spent three positions private to this
    tab — top-left caption, then ahead of the verdict, then pinned to the footer's right —
    and it is now in the source bar heading the block, which is where the Tests and Review
    tabs put it too. What is left below the code is the verdict, alone."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call())
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert "Slf4jExplicit.java:9" in bar
    footer = out[out.index('<p class="log-footer">'):out.index("</p>", out.index('<p class="log-footer">')) + 4]
    assert 'class="privacy-verdict' in footer
    assert "Slf4jExplicit" not in footer          # not said twice, once per position
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
    # The verdict came from a model, and the page says so on the verdict itself - after
    # the word, never inside it, so the word is still the word.
    after = out[out.index("</span>", verdict_at) + len("</span>"):]
    assert after.startswith('<sup class="ai-mark"'), "no AI mark after the verdict"
    assert 'data-tip="LLM evaluated"' in out


def test_a_verdict_the_model_never_gave_carries_no_ai_mark(no_verdict_disk):
    """NOT EVALUATED is the state where the model could not be reached at all. Marking it
    'LLM evaluated' would say the opposite of what it means."""
    out = build._logging_listing([DEBUG_HIT], REPO_ROOT, call=_fake_call(verdict="error"))
    assert "NOT EVALUATED" in out
    assert "ai-mark" not in out


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


def test_the_legend_prices_nothing_and_explains_no_machinery(tmp_path):
    """What the run cost, how many calls it took and which half of the box is a live one
    are all gone from the legend. They were a paragraph about the build on a tab opened to
    read about the diff, and the ai-mark on each verdict already carries the part a reader
    can act on. Pinned on the path that used to print a price: a real, paid cache miss."""
    hit_a = {**DEBUG_HIT, "line": 8, "text": 'LOG.debug("cache miss A")'}
    hit_b = {**DEBUG_HIT, "line": 9, "text": 'LOG.debug("cache miss B")'}
    fake = _fake_call(cost=0.0037)
    out = build._logging_listing([hit_a], REPO_ROOT, call=fake, cache_root=tmp_path)
    assert "$" not in out
    assert "live" not in out and "cost" not in out
    out = build._logging_listing([hit_a, hit_b], REPO_ROOT, call=fake, cache_root=tmp_path)
    legend = out[out.index("privacy-legend-note"):]
    assert "cache" not in legend       # neither the price nor the bookkeeping behind it
    assert "AI Evaluation" in out      # the disclosure itself still stands


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
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        build._call_privacy_model("prompt")


def test_a_nonzero_exit_with_no_usable_output_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
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
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
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
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
    bad = json.dumps({"is_error": False, "structured_output": {"verdict": "SAFE"}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout=bad, stderr="oops"))
    with pytest.raises(RuntimeError, match="did not match.*exited 1.*oops"):
        build._call_privacy_model("prompt")


def test_a_response_missing_the_verdict_field_is_rejected(monkeypatch):
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
    ok = json.dumps({"is_error": False, "structured_output": {"trace": "x"}})
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, stdout=ok, stderr=""))
    with pytest.raises(RuntimeError, match="did not match"):
        build._call_privacy_model("prompt")


def test_a_well_formed_response_is_parsed(monkeypatch):
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
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
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
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
    monkeypatch.setattr(logging_tab, "_claude_bin", lambda: "/usr/bin/true")
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
    monkeypatch.setattr(logging_tab, "VERDICT_SYSTEM_PROMPT", logging_tab.VERDICT_SYSTEM_PROMPT + " x")
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
    monkeypatch.setattr(logging_tab, "_call_privacy_model",
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
    assert "Foo.java:7" in frag      # the location, in the bar every quoted block wears
    assert "bad id" in frag          # the statement's own text, verbatim in the snippet
    assert "SAFE" in frag            # the verdict, visible below the code
    assert "an int parameter" in frag  # the value's clause, from the (mocked) model


def test_the_logging_tab_opens_on_one_computed_line_and_no_heading(tmp_path, monkeypatch):
    """Two things the content file used to write and no longer can: a `<h2>` repeating the
    tab's own label, and three sentences of methodology under it. What is left is one line
    naming what the scan looked for, with the package list on hover — and the list is read
    out of `logextract.py`'s own rule, so a library added there turns up here with nobody
    remembering the page. The anchor the heading carried moves onto the line."""
    monkeypatch.setattr(logging_tab, "_call_privacy_model",
                        lambda prompt: {"verdict": "safe", "cost_usd": 0.0,
                                        "values": [{"name": "id", "verdict": "safe",
                                                    "note": "an int parameter"}]})
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    src.write_text(FOO_WITH_WARN, encoding="utf-8")
    frag, _, _ = build.logging_fragment(
        {"paths": ["."], "base": "base", "id": "logging-added",
         "title": "Logging added/updated", "body": "<p>Found structurally with ast-grep…</p>"},
        repo)
    assert "<h2" not in frag, "the tab is called Logging; a heading says it twice"
    assert "Logging added/updated" not in frag and "not by grepping" not in frag, \
        "title and body on the logging block are the renderer's now, not the author's"
    assert 'id="logging-added"' in frag, "the deep link the heading carried still lands"
    assert "Found structurally searching for" in frag
    assert ">common logging libraries</span>." in frag
    # The hover, and the fact that it is read rather than typed.
    assert "org.slf4j" in frag and "ch.qos.logback" in frag
    assert r"org\.slf4j" in build._logextract().RULES["log-import"]  # escaped there
    # A panel of bullets floated over the line would cover the sentence being read.
    assert 'data-tip-side="right"' in frag


def test_the_library_hover_is_a_list_beside_the_line_not_a_sentence_over_it():
    """Eight dotted package roots are a list, and a reader's question is "is mine there".

    Welded into one comma-separated sentence that question is answered only by reading to
    the end, and floated above the phrase the panel covers the sentence being read. So:
    one `<li>` per package in code type, and `data-tip-side="right"` so it opens beside
    the line. The tail stays prose, because a logger reached without an import — by type,
    by factory, by Lombok — is genuinely a sentence and not a ninth package."""
    tip = build.logging_libraries_tip()
    assert tip.startswith('<ul class="tiplist">')
    assert tip.count("<li>") == len(build.logging_libraries()) >= 2
    assert "<li>org.slf4j</li>" in tip
    assert ", " not in tip.split("</ul>")[0], "the packages are bullets, never a run-on"
    assert '<p class="tipfoot">' in tip and "Lombok" in tip


def test_a_logging_box_does_not_badge_what_its_own_gutter_already_marks(tmp_path, monkeypatch):
    """`new code` on every box restates the tab's entry condition: a block is here because
    the branch added or rewrote that logging line, and the `+` in the gutter marks exactly
    which lines. The badge stays everywhere else, where the reader did not choose the
    snippet and "is this new?" is a real question."""
    monkeypatch.setattr(logging_tab, "_call_privacy_model",
                        lambda prompt: {"verdict": "safe", "cost_usd": 0.0, "values": []})
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    src.write_text(FOO_WITH_WARN, encoding="utf-8")
    frag, _, _ = build.logging_fragment({"paths": ["."], "base": "base"}, repo)
    assert "code-badge" not in frag
    assert "new code" not in frag
    assert '<figure class="snippet">' in frag  # and the snippet itself is untouched


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

def _build(tmp_path, content, env=None) -> str:
    """A page built the way the skill builds it, with two things held still.

    `HOME` is redirected at an empty directory, and the session id is dropped. Both are
    about the cost tab, which asks the machine it is running on what this change cost to
    write and to review — and the machine it is running on, for this suite, is the very
    repo whose transcripts it would find. Left alone, the assertions here depend on how
    much the developer happened to spend in this checkout last week: the suite started
    taking six minutes and one test began counting seventeen mentions of `Opus 5` that
    came out of real conversations rather than out of the content file. An empty `HOME`
    makes every transcript lookup find nothing, instantly and identically everywhere.
    Tests that are ABOUT the cost tab build their own `HOME` with a transcript in it.
    """
    import os
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = dict(env if env is not None else os.environ)
    base["HOME"] = str(home)
    base.pop("CLAUDE_CODE_SESSION_ID", None)
    src = tmp_path / "content.json"
    src.write_text(json.dumps(content), encoding="utf-8")
    out = tmp_path / "review.html"
    proc = subprocess.run(
        [sys.executable, str(HERE / "build-review-html.py"), str(src), "--out", str(out)],
        capture_output=True, text=True, env=base)
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8"), proc.stderr


def _sessionless_env():
    """The environment of a page rebuilt outside the run that reviewed it.

    Without this the suite's own answer depends on who is running it: inside a Claude Code
    session `review-cost.py` finds a transcript and names a real model, so a test asserting
    the fallback would pass on CI and fail on the machine the feature was written on.
    """
    import os
    return {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}


BARE = {
    "title": "t", "summary": "<p>{{tabcount}} tabs: <b>Two</b>.</p>",
    "sections": [{"id": "one", "title": "One", "body": "<p>a</p>"},
                 {"id": "two", "title": "Two", "body": "<p>b</p>"}],
    "tabs": [{"id": "one", "label": "One", "blocks": [{"type": "section", "id": "one"}]},
             {"id": "two", "label": "Two", "blocks": [{"type": "section", "id": "two"}]}],
}


def test_a_blocked_tab_is_red_rather_than_wearing_an_exclamation_mark(tmp_path):
    """The CODEOWNERS tab, when the branch touches somebody else's files.

    It used to grow a `!` in a red circle beside its name — the same fact said as an
    ornament, a glyph the reader has to decode hung off a word that could have carried the
    colour itself. The word is the alarm now, and the `!` has to be gone rather than
    merely recoloured: two marks for one fact is what this change exists to end.
    """
    content = {**BARE, "tabs": [
        {**BARE["tabs"][0], "tabClass": "alarm", "badgeLabel": "approval required"},
        BARE["tabs"][1]]}
    page, _ = _build(tmp_path, content)
    assert 'class="tab alarm"' in page
    assert 'aria-label="One — approval required"' in page, \
        "aria-label replaces the accessible name, so it has to restate the tab's own label"
    assert '<span class="n alarm"' not in page and ">!</span>" not in page
    assert "button.tab.alarm { color:#c62828; }" in page, \
        "the pill's own colour, not a badge's"


def test_the_tab_count_token_is_filled_in_from_the_tabs_that_were_emitted(tmp_path):
    page, _ = _build(tmp_path, BARE)
    assert "Two tabs" in page, "the two declared tabs, and no synthesised Overview"
    assert "{{tabcount}}" not in page


def test_the_summary_opens_the_first_tab_instead_of_owning_one(tmp_path):
    """A tab is a question the reader chooses. "What is this change" is not chosen — it is
    what the page opens with, so it cost a pill in the strip, a click to leave and a click
    to come back. It is the first panel's lede now, above that tab's own intro, and it
    brings no tab of its own."""
    page, _ = _build(tmp_path, dict(
        BARE, verdict={"score": 5, "label": "not yet mergeable", "bullets": ["<b>why</b>"]},
        tabs=[{"id": "one", "label": "One", "intro": "<p class=sub>about this tab</p>",
               "blocks": [{"type": "section", "id": "one"}]},
              {"id": "two", "label": "Two", "blocks": [{"type": "section", "id": "two"}]}]))
    assert ">Overview<" not in page and 'id="overview"' not in page
    panel = page[page.index('<section class="panel" id="one"'):]
    panel = panel[:panel.index("</section>")]
    assert panel.index('class="lede"') < panel.index("about this tab"), \
        "the summary is about the change; an intro is about the tab"
    assert panel.index("about this tab") < panel.index("<h2"), "then the tab's own blocks"
    assert 'class="verdict' not in panel and "why" not in panel, \
        "the bullets are kept in the content file and rendered nowhere"
    # Outside every panel is where it used to sit, above the strip, pushing the questions
    # below the fold — and that is the one place it must not come back to.
    assert 'class="lede"' not in page[:page.index('<section class="panel"')]


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
    assert ".tabstrip .grow" not in css, \
        "the spacer existed to push `show all` to the far end; the button left the strip"


def test_the_stacked_panels_are_never_painted_on_the_way_in(tmp_path):
    """Every panel is visible while the earlier scripts measure it — getBBox() inside a
    display:none subtree returns zeros — so the document spends the first few hundred
    milliseconds of every load as all twelve panels stacked end to end, and the browser
    paints that. Measured on the demo page: six frames, 37,000px tall, before the strip
    collapsed it at ~490ms. Harmless-looking on a first load, and a flicker under the
    reader's eyes on the reload the watcher fires after a rebuild.

    The hold keeps the panels out of the paint without taking them out of layout, which is
    the one thing the measuring scripts cannot lose."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    rule = [l for l in css.splitlines() if "tabs-pending" in l]
    assert rule, "nothing holds the stacked panels out of the first paint"
    assert all("visibility:hidden" in l for l in rule), rule
    assert not any("display:none" in l for l in rule), \
        "display:none costs every sequence diagram its click targets: getBBox() is zeros"
    assert all(l.lstrip().startswith("@media screen") for l in rule), \
        "print shows every panel; a print racing the load would come out blank"

    head = page[:page.index("</head>")]
    assert "classList.add('tabs-pending')" in head, \
        "the hold has to be on before the first frame, which is already past a panel"
    # And off again on the far side of the strip, in its own tag: TABS_JS returns early on
    # a page with no strip, and a throw inside it would strand the hold.
    tail = page[page.index("</head>"):]
    assert tail.count("classList.remove('tabs-pending')") >= 1
    assert page.rindex("classList.remove('tabs-pending')") > page.rindex("var strip = document.querySelector('.tabstrip')")
    # The load event is the net under all of it: whatever happens to the scripts at the
    # foot of the page, the content comes back.
    assert "addEventListener('load', function () { h.classList.remove('tabs-pending'); })" in page


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
    """"Is there anything at this level at all?" is the question the Tests tab is
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


def test_an_include_follows_the_prose_unless_the_section_asks_for_it_first(tmp_path):
    """The Tests tab is read ticket-first: what was asked for, then what the branch
    wrote to pin it. Everywhere else the include is a generated fragment the prose
    introduces, so the default stays prose-first and only `includeFirst` flips it."""
    (tmp_path / "frag.html").write_text('<div class="reqmap">the ticket</div>', encoding="utf-8")
    page, _ = _build(tmp_path, dict(BARE, sections=[
        {"id": "one", "title": "One", "body": "<p>a</p>", "includeHtml": "frag.html",
         "includeFirst": True},
        {"id": "two", "title": "Two", "body": "<p>b</p>", "includeHtml": "frag.html"}]))
    first, second = page.index('id="one"'), page.index('id="two"')
    one, two = page[first:second], page[second:]
    assert one.index("reqmap") < one.index("<p>a</p>")
    assert two.index("<p>b</p>") < two.index("reqmap")
    # And it is pasted once, not once at each end.
    assert one.count("reqmap") == 1 and two.count("reqmap") == 1


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


def test_the_pr_number_says_on_hover_that_it_leaves_for_github(tmp_path):
    """The one link on the page a reader cannot spot by where it sits: it is the first
    word of the `<h1>`, wearing heading weight rather than a link's. So it says where it
    goes before it is clicked."""
    page, _ = _build(tmp_path, PR)
    head = page[page.index("<h1>"):page.index("</h1>")]
    assert 'data-tip="Open #37 on GitHub"' in head


def test_the_score_opens_the_tab_that_holds_the_findings_behind_it(tmp_path):
    """`5/10 not yet mergeable` states a conclusion and shows none of the reasoning, so
    the click a reader tries on it has to land on the findings. The target is found by
    which tab renders them, not by its id, so a page that arranges its tabs differently
    still sends the score where its reasons are."""
    page, _ = _build(tmp_path, dict(
        BARE, verdict={"score": 5, "label": "not yet mergeable", "bullets": ["why"]},
        findings=[{"title": "f", "body": "<p>b</p>"}],
        tabs=[{"id": "one", "label": "One", "blocks": [{"type": "section", "id": "one"}]},
              {"id": "verdicts", "label": "🤖 Review",
               "blocks": [{"type": "findings"}]}]))
    row = page[page.index('<div class="titlerow'):page.index("</div>")]
    assert '<a class="titlescore v-mid" href="#verdicts"' in row
    assert 'data-tip="Open the Review tab"' in row, "the hover says where the click goes"
    assert "🤖" not in row.split("data-tip=")[1][:40], "and says it without the emoji"


def test_a_score_on_a_page_with_no_findings_tab_stays_a_plain_pill(tmp_path):
    """The fallback is the first tab — the panel the page already opens on — and with no
    tabs at all the score links nowhere rather than to a dead anchor."""
    page, _ = _build(tmp_path, dict(
        {k: v for k, v in BARE.items() if k != "tabs"},
        summary="<p>no tabs here</p>",
        verdict={"score": 9, "label": "ship it", "bullets": ["why"]}))
    assert '<span class="titlescore v-good">' in page
    assert "titlescore" in page and 'href="#' not in page[page.index("titlescore"):
                                                          page.index("</h1>") + 200]


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


def test_the_show_all_button_sits_after_the_footer_not_in_the_strip(tmp_path):
    """A permanent control for an occasional act. In the strip it was in the corner of
    every screenful for the whole read; at the foot of the page it is where the reader
    arrives having finished, which is when "show me all of it at once" is worth wanting."""
    page, _ = _build(tmp_path, BARE)
    strip = page[page.index('class="tabstrip"'):]
    assert "allbtn" not in strip[:strip.index("</div>")]
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert "allbtn" in foot and "show single page" in foot


def test_the_footer_is_one_centred_line_with_the_control_under_it(tmp_path):
    """The provenance and the offer are one sentence, centred — not two blocks pushed to
    opposite ends of a flex row, which on a wide screen read as a header bar rather than as
    the page's closing line. The control is not a word of that sentence: it stands on its
    own line below, centred, so it reads as the page's one control."""
    page, _ = _build(tmp_path, BARE)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert '<p class="footrow">' in foot
    assert foot.index("</p>") < foot.index("allbar"), "the line closes before the button"
    assert "footer .footrow { text-align:center; }" in page
    assert "footer .footrow > span { display:inline; }" in page
    assert "footer .allbar { display:flex; justify-content:center;" in page
    # One line, so nothing between the two halves but a space.
    line = foot[foot.index('<p class="footrow">'):foot.index("</p>")]
    assert line.count("<span") >= 2 and "<div" not in line


def test_the_footer_offers_both_ways_to_take_the_page_away(tmp_path):
    """A review page is nearly always read on someone else's screen — projected in a
    room, or shared for the length of a call. The reader who reaches the bottom has
    nothing afterwards unless the page tells them where a copy lives, so the build says it
    on every page, and it says it twice because the two copies are not the same page: the
    zip is read off disk, where a review cannot reliably fetch its own content and every
    request it makes is cross-origin, while the container serves it the way it is being
    demoed."""
    page, _ = _build(tmp_path, BARE)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert ">Download zip</a> · or " in foot
    assert ">a runnable docker of this report</a>." in foot
    assert "https://github.com/victorrentea/human-review/releases/tag/demo" in foot
    assert "pkgs/container/human-review" in foot
    # The links are the nouns. `Download here a standalone demo zip` put the verb in the
    # link and the noun after it, so the eye landed on words that said nothing about what
    # arrives and had to read on to find out.
    assert "Download here" not in foot
    # After the sentence and before the control, so the row still reads sentence-first.
    assert foot.index("takeaway") < foot.index("allbar")


def test_the_docker_hover_carries_the_command_the_link_cannot(tmp_path):
    """A footer is a place to send somebody, not a place to print a command they cannot
    run from a browser — but the command is the thing they will want ten seconds later."""
    page, _ = _build(tmp_path, BARE)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    tip = re.search(r'pkgs/container/human-review"[^>]*data-tip="([^"]*)"', foot).group(1)
    assert "docker run" in tip and "ghcr.io/victorrentea/human-review" in tip
    assert "8642" in tip


def test_the_zip_offer_does_not_depend_on_what_the_content_file_says(tmp_path):
    """The footer sentence is the author's and may be missing entirely; the offer is the
    build's. A page with no `footer` in its content file still tells its reader where to
    get one."""
    spec = {k: v for k, v in BARE.items() if k != "footer"}
    page, _ = _build(tmp_path, spec)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert ">Download zip</a>" in foot


def test_the_show_all_button_says_what_it_does_next(tmp_path):
    """It is a toggle, and both of its states need a name now: the pressed styling alone
    carried the state while the button sat among the tabs, and at the foot of the page
    there is nothing beside it to read a highlight against."""
    page, _ = _build(tmp_path, BARE)
    assert 'data-label-off="show single page"' in page
    assert 'data-label-on="back to one tab at a time"' in page
    assert ">show single page</button>" in page, "the unpressed label is also the markup"
    assert "(single)" not in page


def test_every_pill_still_has_to_fit_on_one_row(tmp_path):
    """The strip wrapping to a second row used to cost a scroll past it; inside a
    masthead that row is on screen for the whole read. The labels are not abbreviated,
    so the room comes out of the padding and the type."""
    page, _ = _build(tmp_path, BARE)
    css = page[page.index("<style>"):page.index("</style>")]
    assert css.rindex("button.tab { padding:0 .5rem;") > css.rindex("padding:0 .85rem")


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
    # And the other half of the comparison. A rendered top of 0 means "pinned" only for
    # something that sits lower unpinned; the masthead is the first thing in `.wrap`, whose
    # top padding `:has(.masthead)` zeroes, so it reads 0 at rest as well — and the page
    # wore the pinned edge before anything had scrolled under it. `pageYOffset` is not a
    # threshold and copies no height: it is "has this scrolled at all".
    assert "window.pageYOffset > 0.5" in page


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


# The footer line is the address the page came from and the date it was built, and that is
# all it is for. It carried an instruction for a while — "Tell your agent to adapt this to
# your environment" — on the reasoning that a GitHub link in a footer reads as provenance
# and gets skipped. Right about the reading, wrong about the cure: the two links beside it
# already *are* the things to do, and the sentence was a third voice in a line with room
# for two.
def test_the_footer_line_is_provenance_and_nothing_else():
    out = build._link_home("Built by /human-review against the running stack on 2 Sep 2026.")
    assert out.endswith("on 2 Sep 2026.")
    assert "Tell your agent" not in out
    # The footer is emitted as HTML and not escaped on the way out, so a bare `&` in it
    # would be a lone ampersand in the markup.
    assert " & " not in out


def test_an_older_footer_that_carries_the_instruction_is_cleaned(tmp_path):
    """Content files outlive the instructions that produced them, and a page rebuilt from
    one would otherwise be the single place the sentence survives."""
    for old in ("Tell your agent to adapt this to your environment.",
                "Fork, Clone and Port with your Agent."):
        out = build._link_home(f"Built by /human-review on 2 Sep 2026. {old}")
        assert out.endswith("on 2 Sep 2026."), old


# "against the running stack" describes the build, not anything the reader can act on,
# and it is what every build does now. Out of the footer wherever a content file still
# carries it.
def test_the_running_stack_phrase_is_dropped():
    out = build._link_home("Built by /human-review against the running stack on 2 Sep 2026.")
    assert "running stack" not in out
    assert "Built by" in out and "on 2 Sep 2026." in out


# The sentence is appended once, when there is one. Written against the constant and not
# against a copy of today's wording: it is meant to be rewritten — and emptied, which is
# what it is now — so a test that pinned its words would fail on the rewrite instead of on
# the doubling it exists to catch.
def test_the_invitation_is_not_doubled():
    if not build.INVITATION:
        pytest.skip("the footer carries no invitation")
    out = build._link_home(f"Built by /human-review on 2 Sep 2026. {build.INVITATION}")
    assert out.count(build.INVITATION) == 1


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


def test_work_tree_edits_count_as_this_branch_s_changes(tmp_path):
    """The lines are read off the work tree, so the diff has to be too.

    A review is written *while* the branch is being worked on: the test it quotes is
    routinely still uncommitted when the page is built. Diffing to HEAD described a text
    nobody was looking at and badged a minutes-old @SpringBootTest `UNCHANGED`."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    f = _tiny_repo(tmp_path, ["const a = 1;"], ["const a = 1;", "const b = 2;"])
    f.write_text("const a = 1;\nconst b = 2;\nconst c = 3;\n")   # never committed

    assert es.added_lines("a.ts", tmp_path) == frozenset({2, 3})
    whole = es.block_status("a.ts", tmp_path, [(3, 3)], f.read_text().splitlines(), "test")
    assert whole["diff"] == "new", whole


def test_an_untracked_file_is_new_rather_than_untouched(tmp_path):
    """`git diff` cannot see an untracked file at either end, so asking it alone reports
    the one file that is certainly new as the one file that certainly did not change."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    _tiny_repo(tmp_path, ["const a = 1;"], ["const a = 1;", "const z = 9;"])
    (tmp_path / "b.ts").write_text("const b = 2;\nconst c = 3;\n")

    assert es.added_lines("b.ts", tmp_path) == frozenset({1, 2})
    out = es.render("b.ts:1-2", None, tmp_path, exact=True)
    assert 'data-diff="new"' in out, out


def test_a_file_the_branch_really_did_not_touch_still_reads_unchanged(tmp_path):
    """The point of the badge is that it can say no. Moving the comparison to the work
    tree must not turn every snippet green."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    _tiny_repo(tmp_path, ["const a = 1;"], ["const a = 1;", "const z = 9;"])
    # committed on the base and never touched since — a.ts moved, this did not
    stable = tmp_path / "stable.ts"
    stable.write_text("const s = 1;\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "stable"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "branch", "-f", "origin/main", "HEAD"], cwd=tmp_path,
                   check=True, capture_output=True)
    stable.write_text("const s = 1;\n")            # rewritten, byte-identical

    assert es.added_lines("stable.ts", tmp_path) == frozenset()
    status = es.block_status("stable.ts", tmp_path, [(1, 1)], ["const s = 1;"], "test")
    assert status["diff"] == "unchanged", status


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


# --------------------------------------------------------------------------- #
# tests that are still written and no longer run
# --------------------------------------------------------------------------- #

def test_a_test_that_no_longer_runs_says_so_beside_its_own_row():
    """The flag column answers "what did the branch do to this test"; this answers "does
    it still run". A test that arrives `new` and `@Disabled` needs both said at once, and
    saying only the first is the failure this exists to prevent."""
    out = build.render_tests(
        [{"name": "flaky", "path": "src/test/VisitTest.java", "status": "added",
          "line": 12, "silenced": "disabled"}], Path("/repo"))
    assert '<span class="tflag added">new</span>' in out
    assert ">disabled</span>" in out and 'class="tsilenced"' in out


def test_a_commented_out_test_is_not_reported_as_a_plain_deletion():
    out = build.render_tests(
        [{"name": "parked", "path": "src/test/VisitTest.java", "status": "deleted",
          "line": 40, "silenced": "commented"}], Path("/repo"))
    assert '<span class="tflag removed">deleted</span>' in out, \
        "what it costs the run is a deletion, and that is the flag column's question"
    assert 'class="tsilenced"' in out and ">commented out</span>" in out, \
        "how it was done is the stamp's question, and only one of the two is a git revert"
    assert 'href="vscode://file//repo/src/test/VisitTest.java:40:1"' in out, \
        "the body is still in the file, so the row still has somewhere to go"


def test_a_test_switched_back_on_is_credited_for_it():
    out = build.render_tests(
        [{"name": "revived", "path": "src/test/VisitTest.java", "status": "modified",
          "line": 12, "wasSilenced": "disabled"}], Path("/repo"))
    assert 'class="tback"' in out and ">back on</span>" in out


def test_an_untouched_test_that_does_not_run_still_says_so(tmp_path):
    """The worst case for a coverage claim: the requirement names a test, the test is
    real, the branch never touched it — and somebody disabled it months ago. Nothing in
    the diff can catch that, so the declaration is read off the working tree."""
    f = tmp_path / "src" / "test" / "OldTest.java"
    f.parent.mkdir(parents=True)
    f.write_text("class OldTest {\n  @Disabled\n  @Test\n  void alreadyThere() {\n  }\n}\n")
    rows = build.resolve_tests(
        [{"name": "alreadyThere", "path": "src/test/OldTest.java"}], _idx(), tmp_path)
    assert rows[0]["status"] == "unchanged" and rows[0]["silenced"] == "disabled"


# --------------------------------------------------------------------------- #
# the ledger: every test the change set moved, in one list
# --------------------------------------------------------------------------- #
LEDGER_ROWS = [
    {"name": "create_withVet", "path": "src/test/VisitTest.java", "status": "added", "line": 10},
    {"name": "arrives_off", "path": "src/test/VisitTest.java", "status": "added",
     "line": 11, "silenced": "disabled"},
    {"name": "update_ok", "path": "src/test/VisitTest.java", "status": "modified", "line": 12},
    {"name": "parked", "path": "src/test/VisitTest.java", "status": "deleted",
     "line": 14, "silenced": "commented"},
    {"name": "obsolete", "path": "src/test/VisitTest.java", "status": "deleted", "line": 15},
    {"name": "untouched", "path": "src/test/VisitTest.java", "status": "unchanged", "line": 16},
]


def test_the_ledger_lists_the_tests_no_requirement_happens_to_name():
    """A deleted test is under no requirement by definition, and a test that pins nothing
    anybody wrote down is under none either — so without this list they are counted in
    the chip and named nowhere."""
    out, moved = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert moved == 5, "everything but the untouched one"
    for name in ("create_withVet", "arrives_off", "update_ok", "parked", "obsolete"):
        assert f">{name} <" in out
    assert ">untouched <" not in out


def test_a_test_that_never_runs_is_filed_under_that_and_not_under_new():
    """`new` and `@Disabled` is not news about coverage, it is news about a test that has
    never run — and filing it under "new" hides it among the twenty-one that do run."""
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    off = out[out.index("stopped running"):out.index("<h3>new")]
    assert "arrives_off" in off and "create_withVet" not in off
    assert '<span class="tflag added">new</span>' in off, \
        "the one group whose rows do not share a fate keeps the flag saying which it is"


def test_the_untouched_rest_are_counted_rather_than_listed():
    """A reviewer scrolling past a hundred unchanged names to find the two that went away
    is a reviewer who stops scrolling."""
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert "1 more test in the files this change set touched" in out


def test_a_group_heading_spares_its_rows_from_repeating_the_same_word():
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    gone = out[out.index("<h3>gone"):]
    assert "obsolete" in gone and '<span class="tflag' not in gone[:gone.index("</section>")]


def test_a_page_with_a_manifest_and_no_tests_block_still_shows_the_ledger(tmp_path):
    """The ledger is derived data, like the requirement lists it sits under: a content
    file written before the block existed must not leave the manifest computed and
    unread."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "tc.json").write_text(json.dumps(
        {"totals": {k: 0 for k in ("added", "modified", "deleted", "unchanged", "commented",
                                   "disabled", "reenabled", "runningBefore", "runningAfter",
                                   "gained", "lost")} | {"added": 1, "gained": 1},
         "tests": [{"name": "create_withVet", "path": "src/test/VisitTest.java",
                    "status": "added", "line": 10}]}), encoding="utf-8")
    page, _ = _build(tmp_path, dict(
        BARE, testChanges="assets/tc.json",
        summary="<p>{{tabcount}} tabs: <b>Tests</b>, <b>Two</b>.</p>",
        sections=[{"id": "requirements", "title": "R", "body": "<p>a</p>"},
                  {"id": "two", "title": "Two", "body": "<p>b</p>"}],
        tabs=[{"id": "requirements", "label": "Tests",
               "blocks": [{"type": "section", "id": "requirements"}]},
              {"id": "two", "label": "Two", "blocks": [{"type": "section", "id": "two"}]}]))
    panel = page[page.index('id="requirements"'):]
    assert "create_withVet" in panel[:panel.index("</section>")]


# --------------------------------------------------------------------------- #
# the chip that states what the branch did to the test run
# --------------------------------------------------------------------------- #
TOTALS = {"added": 9, "modified": 4, "deleted": 3, "unchanged": 40, "commented": 1,
          "disabled": 2, "reenabled": 1, "runningBefore": 46, "runningAfter": 52,
          "gained": 10, "lost": 4}


def test_the_chip_states_the_balance_of_the_run_not_just_what_was_added():
    chip = build.tests_chip({"totals": TOTALS})
    assert chip["label"] == "tests"
    assert '<span class="added">+10</span>' in chip["value"]
    assert '<span class="removed">\u22124</span>' in chip["value"]
    assert f'{build.PENCIL}4' in chip["value"], \
        "the scope bar's third sign — a pencil, because `~` and `±` both read as an " \
        "approximation, which is not the claim"


def test_the_chip_splits_the_loss_only_in_the_tooltip():
    """One number on the face, three causes behind it. A reviewer shown "2 deleted" and
    not shown the three `@Disabled`d has been handed the smallest of the three truths —
    but a face carrying all three would invite reading one of them as the answer."""
    tip = build.tests_chip({"totals": TOTALS})["tip"]
    assert "2 deleted" in tip and "1 commented out" in tip
    assert "2 disabled" in tip and "1 back on" in tip


def test_a_new_test_that_arrives_disabled_is_named_rather_than_left_as_a_discrepancy():
    """`22 new` over a chip reading `+21` looks like an arithmetic bug. It is the finding:
    one of the new tests was committed with an `@Disabled` on it and has never run."""
    tip = build.tests_chip({"totals": dict(TOTALS, added=22, gained=22)})["tip"]
    assert "22 new (1 disabled on arrival)" in tip


def test_the_chip_drops_itself_rather_than_printing_a_zero_it_did_not_count():
    """`tests none` on a page built without the manifest reads as "this branch wrote no
    tests", which is a finding — and would be a lie."""
    assert build.tests_chip({}) is None
    assert build.tests_chip(None) is None


def test_a_branch_that_touched_no_test_file_says_so_rather_than_showing_nothing():
    chip = build.tests_chip({"totals": dict.fromkeys(TOTALS, 0)})
    assert chip["value"] == "none touched"


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
    build.reset_list()
    build.render_findings(findings)
    out = build.render_autofixes([{"title": "d"}])
    assert 'counter-reset:f 3' in out


def test_an_empty_findings_list_still_starts_the_fixes_at_one():
    build.reset_list()
    build.render_findings([])
    assert "counter-reset" not in build.render_autofixes([{"title": "d"}])


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
    """A documented pass is stamped *and* linked: the stamp is the question a reader who
    has never run it asks, so it is where the answer belongs."""
    out = build.render_findings([{"title": "a", "body": "x", "source": "/code-review"}])
    assert 'class="f-src" href="' + build.PASS_DOCS["/code-review"] + '"' in out
    assert ">/code-review</a>" in out


def test_a_source_with_no_documentation_stays_a_plain_stamp():
    """`assumption` is not a command anyone can go and read about, and a stamp linked to
    something that does not describe it is worse than a stamp that stays quiet."""
    out = build.render_findings([{"title": "a", "body": "x", "source": "assumption"}])
    assert '<span class="f-src">assumption</span>' in out


def test_a_sourced_effort_and_detail_split_off_the_chip():
    """`review-points.md` writes one field for three facts: the pass, the effort it filed
    at, and which of several same-titled findings this one is. The chip stays the pass
    alone — a face that says `/code-review high (the PUT-clears-the-vet scenario)` reads
    as a broken command, not as three facts about one."""
    out = build.render_findings([{
        "title": "a", "body": "x",
        "source": "/code-review high (the PUT-clears-the-vet scenario)"}])
    assert ">/code-review</a>" in out
    assert "high (the PUT-clears-the-vet scenario)" not in out.split(">/code-review</a>")[0]
    assert 'data-tip="What /code-review does, in the Claude Code docs — filed at high ' \
           'effort"' in out
    assert '<span class="f-src-detail">(the PUT-clears-the-vet scenario)</span>' in out


def test_a_sourced_pass_with_no_detail_still_gets_a_tip_for_its_effort():
    out = build.render_findings([{"title": "a", "body": "x", "source": "/simplify medium"}])
    assert ">/simplify</a>" in out
    assert "f-src-detail" not in out
    assert "filed at medium effort" in out


def test_a_sourced_pass_with_a_detail_but_no_effort_still_splits(tmp_path):
    out = build.render_findings([{
        "title": "a", "body": "x", "source": "/code-review (duplication)"}])
    assert ">/code-review</a>" in out
    assert '<span class="f-src-detail">(duplication)</span>' in out
    assert "filed at" not in out


def test_the_verdict_never_draws_a_band_however_many_reasons_it_carries(tmp_path):
    """The band is gone, bullets and all. It held the masthead's own pill a second time,
    one screenful lower, at 3.4rem and on a full-bleed amber ground — so the first
    screenful of a review was spent on the conclusion and the list of findings the reader
    came for started below the fold. The score keeps its pill; the bullets stay in the
    content file and are drawn nowhere."""
    page, _ = _build(tmp_path, dict(
        BARE, verdict={"score": 5, "label": "not yet mergeable",
                       "bullets": ["because of the thing", "and the other thing"]}))
    assert 'class="verdict' not in page and ".verdict {" not in page, \
        "no band, and no stylesheet for one"
    assert "because of the thing" not in page and "and the other thing" not in page
    assert '<i class="on">' not in page, "the ten-pip dial went with it"
    assert not hasattr(build, "verdict_band_html"), "and so did the function that built it"
    assert '<b>5</b><small>/10</small>' in page, "the pill is where the score lives now"


def test_a_bare_ref_shows_the_name_and_keeps_the_path_on_hover():
    """The same trade the diff header makes. Three references on one line, each spelling
    out `src/main/java/victor/training/petclinic/...`, is a wall nobody reads."""
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/rest/VetRestController.java"
    out = build._ref_link({"label": f"{rel}:96-100", "abs": "/tmp/x:96:1"})
    assert ">VetRestController.java:96-100</a>" in out
    assert f'data-tip="{rel}"' in out


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


# --------------------------------------------------------------------------- #
# the two chips that say how much there is to read, and the mark on the ref
# they are measured against
# --------------------------------------------------------------------------- #
def _drifting_repo(tmp_path, *, base_moves_ahead=False, local_base_stale=False,
                   with_generated=True):
    """A repository shaped like the one this was written for.

    A fork point, a remote-tracking `origin/main` beside the local branch of that name,
    and a feature branch that edits two source files and regenerates two machine-written
    ones. The flags turn on the two ways the pair of refs goes stale, one at a time,
    because the page has to tell them apart: a base that moved ahead is a fact about the
    branch, a local ref behind its remote is a fact about the machine the page was built
    on, and they are fixed by different commands.
    """
    import subprocess as sp
    r = tmp_path / "repo"
    r.mkdir()
    run = lambda *a: sp.run(["git", "-C", str(r), *a], check=True,
                            capture_output=True, text=True)
    sp.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (r / "src").mkdir()
    (r / "docs" / "generated").mkdir(parents=True)
    (r / "src" / "Visit.java").write_text("class Visit {\n}\n")
    (r / "docs" / "generated" / "model.json").write_text("[]\n")
    (r / "src" / "flow.genseq.puml").write_text("@startuml\n@enduml\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    fork = run("rev-parse", "HEAD").stdout.strip()
    run("update-ref", "refs/remotes/origin/main", fork)

    if local_base_stale:
        # The remote moves and the branch is built on top of where it moved to, so the
        # branch is perfectly current — only the *local* ref named as the base is behind.
        # This is the quiet case: nothing on the page looks wrong.
        (r / "src" / "Owner.java").write_text("class Owner {\n}\n")
        run("add", "-A")
        run("commit", "-qm", "something else on main")
        fork = run("rev-parse", "HEAD").stdout.strip()
        run("update-ref", "refs/remotes/origin/main", fork)
        run("update-ref", "refs/heads/main", fork + "^")

    run("checkout", "-q", "-b", "feature", fork)
    # Six lines of real code against seventy-odd of regenerated diagram and model: the
    # shape that made the hand-typed chip unbelievable in the first place.
    (r / "src" / "Visit.java").write_text("class Visit {\n  Vet vet;\n  Vet getVet() {\n"
                                          "    return vet;\n  }\n}\n")
    (r / "src" / "VetPicker.java").write_text("class VetPicker {\n}\n")
    if with_generated:
        (r / "docs" / "generated" / "model.json").write_text(
            "[\n" + "".join(f'  "line {i}",\n' for i in range(40)) + "]\n")
        (r / "src" / "flow.genseq.puml").write_text(
            "@startuml\n" + "".join(f"a -> b : {i}\n" for i in range(30)) + "@enduml\n")
    run("add", "-A")
    run("commit", "-qm", "link a visit to its vet")

    if base_moves_ahead:
        run("checkout", "-q", "main")
        (r / "src" / "Owner.java").write_text("class Owner {\n}\n")
        run("add", "-A")
        run("commit", "-qm", "something else on main")
        run("update-ref", "refs/remotes/origin/main", run("rev-parse", "HEAD").stdout.strip())
        run("checkout", "-q", "feature")
    return r


def test_the_diffstat_counts_the_code_and_leaves_the_generated_files_out(tmp_path):
    """The whole reason the chip exists. Two source files moved, and two machine-written
    ones were redrawn ten times as wide. A chip that counted both would report a change
    set an order of magnitude bigger than the one there is to read."""
    r = _drifting_repo(tmp_path)
    files, lines = build.diffstat_chips(r, build.base_state(r, "main"), None)
    assert files["label"] == "files" and lines["label"] == "lines"
    assert '<span class="added">+1</span>' in files["value"]   # VetPicker.java, new
    assert f'{build.PENCIL}1' in files["value"]            # Visit.java, edited
    assert "±" not in files["value"], "the pencil replaced the range sign, it did not join it"
    assert '<span class="added">+6</span>' in lines["value"]
    assert "−" not in lines["value"], "a zero is dropped, not printed as −0"
    assert "1 added, 1 edited, 0 deleted" in files["tip"]


def test_the_tooltip_still_states_what_the_unfiltered_diff_would_have_said(tmp_path):
    """Ranked, not hidden. A reviewer who wonders why the number looks small gets the
    other number, and the reason, without having to re-run git."""
    r = _drifting_repo(tmp_path)
    _, lines = build.diffstat_chips(r, build.base_state(r, "main"), None)
    assert "2 generated left out" in lines["tip"]
    assert "4 files" in lines["tip"]      # the two source files and the two generated
    # The rationale that used to follow ("a diagram being redrawn is not a line written",
    # "measured, never typed") is gone from the bubble on purpose: a hover is read in one
    # glance, and neither sentence is one a reader acts on. The numbers are the ranking.


def test_a_change_set_with_no_generated_files_says_so_rather_than_going_quiet(tmp_path):
    """Silence here reads as "the filter never ran", which is the one thing a page whose
    numbers were once fiction must not leave ambiguous."""
    r = _drifting_repo(tmp_path, with_generated=False)
    _, lines = build.diffstat_chips(r, build.base_state(r, "main"), None)
    assert "No generated files to leave out." in lines["tip"]


def test_the_content_file_can_add_exclusions_but_never_drop_the_default_ones(tmp_path):
    r = _drifting_repo(tmp_path)
    files, _ = build.diffstat_chips(r, build.base_state(r, "main"), ["*/VetPicker.java"])
    assert "+1" not in files["value"], "the extra pathspec was not applied"
    assert "3 generated left out" in files["tip"], \
        "an `exclude` list must add to the built-in list, not replace it"


def test_the_line_count_opens_the_compare_page_the_two_numbers_describe(tmp_path):
    """The size of the change set is the one chip a reader wants to click through: "how
    much is there to read" is followed by "show me". It links to the two *branches*, not
    to the shas measured — a sha pair is only a URL once the branch has been pushed."""
    r = _drifting_repo(tmp_path)
    pr = {"repo": "https://github.com/victorrentea/petclinic",
          "base": "origin/main", "branch": "test-pr"}
    files, lines = build.diffstat_chips(r, build.base_state(r, "main"), None, pr)
    assert lines["href"] == "https://github.com/victorrentea/petclinic/compare/main...test-pr", \
        "`origin/` names a remote in this checkout; github.com has never heard of it"
    assert "github.com" in lines["tip"], "a chip that links says where the click goes"
    assert "href" not in files, \
        "two identical-looking links to the same page teach the reader to ignore half the row"
    assert 'class="chip chip-link"' in build.chip_html(lines)


def test_a_diffstat_with_no_repository_to_point_at_stays_an_inert_pill(tmp_path):
    """A 404 is worse than no link: the number is still true, and a click that lands
    nowhere is read as the page being wrong about the rest of it too."""
    r = _drifting_repo(tmp_path)
    st = build.base_state(r, "main")
    assert "href" not in build.diffstat_chips(r, st, None, None)[1]
    assert "href" not in build.diffstat_chips(r, st, None, {"base": "main"})[1], \
        "a base with no repo and no branch is not half a comparison, it is none"


def test_no_base_means_no_chip_rather_than_a_number_measured_against_nothing(tmp_path):
    r = _drifting_repo(tmp_path)
    assert build.base_state(r, "does-not-exist") is None
    assert build.diffstat_chips(r, None, None) == []


def test_the_base_resolves_to_the_remote_the_pull_request_would_merge_into(tmp_path):
    """`"base": "main"` on a machine whose local main is days old measures the branch
    against last week and charges it with every commit not yet pulled — on the branch this
    was written for, 4099 deleted lines for a change set that deletes 39."""
    r = _drifting_repo(tmp_path, local_base_stale=True)
    st = build.base_state(r, "main")
    assert st["ref"] == "origin/main"
    assert st["localRef"] == "main" and st["localBehind"] == 1
    assert st["ahead"] == 0, "the branch itself is current; only the local ref is not"


def test_a_base_that_has_moved_ahead_is_reported_as_commits_not_as_a_boolean(tmp_path):
    r = _drifting_repo(tmp_path, base_moves_ahead=True)
    st = build.base_state(r, "main")
    assert st["ahead"] == 1
    warning = build.base_warning(st)
    assert "origin/main is 1 commit ahead of the fork point" in warning
    assert "Merge or rebase, then rebuild." in warning
    assert len(warning) < 90, "a tooltip is read standing up: the gap and the command"


def test_a_stale_local_ref_is_a_different_sentence_from_a_base_that_moved(tmp_path):
    """Two failures, two fixes: one is `git merge main`, the other is fast-forwarding the
    local base. Told the wrong one, a reader does the wrong thing and the mark stays —
    and `git fetch` was the wrong one: the count itself came off the fetched ref, and a
    fetch never moves the local branch."""
    r = _drifting_repo(tmp_path, local_base_stale=True)
    warning = build.base_warning(build.base_state(r, "main"))
    assert "local main is 1 behind it" in warning
    assert "git branch -f main origin/main" in warning
    assert "git fetch" not in warning, "the command a reader has already run is not the fix"
    assert "ahead of the fork point" not in warning, \
        "the branch is not forked from behind in this one"


def test_the_warning_clears_itself_once_the_base_is_merged_in(tmp_path):
    """No flag to reset and nothing to remember: the mark is a fact about the two refs,
    recomputed on every build."""
    import subprocess as sp
    r = _drifting_repo(tmp_path, base_moves_ahead=True)
    assert build.base_warning(build.base_state(r, "main")) is not None
    sp.run(["git", "-C", str(r), "merge", "-q", "--no-edit", "origin/main"],
           check=True, capture_output=True, text=True)
    assert build.base_warning(build.base_state(r, "main")) is None


def test_a_current_pair_of_refs_carries_no_mark_at_all(tmp_path):
    r = _drifting_repo(tmp_path)
    out = build.ref_badges({"pr": {"branch": "feature", "base": "main"}},
                           build.base_state(r, "main"))
    assert "drift" not in out and ">!<" not in out


def test_the_mark_lands_on_the_base_chip_alone_and_carries_its_own_tooltip(tmp_path):
    r = _drifting_repo(tmp_path, base_moves_ahead=True)
    out = build.ref_badges({"pr": {"branch": "feature", "base": "main"}},
                           build.base_state(r, "main"))
    branch_chip, base_chip = out.split('<span class="chip refchip')[1:]
    assert "drift" not in branch_chip, "the branch is not the ref that went stale"
    assert 'class="drift"' in base_chip and ">!<" in base_chip
    assert "1 commit ahead of the fork point" in base_chip
    # The chip's own tooltip is gone: this spec names no repo, so the chip links nowhere
    # and has nothing to say that its own label does not. The mark keeps a tooltip of its
    # own, which is now the only one on the chip — and the only one worth a hover.
    assert base_chip.count("data-tip") == 1


def test_the_review_chip_leads_with_what_is_left_to_do(tmp_path):
    """`12 raised` is the sum of the other two numbers, so it is the one nobody acts on.
    Open first, because that is the work; auto-fixed second, because it is the fact a
    reader cannot get anywhere else without opening the tab.

    Written as a sentence — `🤖Opus 5 review: 9 open, 3 auto-fixed` — rather than as a
    label, a gap and a row of figures: the second shape is what a measurement looks like,
    and this is a claim a model made about the diff."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one"}],
        findings=[{"title": f"f{i}", "body": "<p>b</p>", "source": "/code-review"}
                  for i in range(9)],
        autofixes=[{"title": f"a{i}", "source": "/simplify"} for i in range(3)]))
    assert '9 open, <span class="sub">3 auto-fixed</span>' in page, \
        "the half that needs nothing from the reader is greyed, not equal-weight"
    assert "12 raised" in page, "the total is in the hover, not on the face"
    assert "9 by /code-review, 3 by /simplify" in page, \
        "the hover splits the total by the pass that raised each item"


def test_the_review_chip_names_the_model_instead_of_a_second_chip_beside_it(tmp_path):
    """`LLM review  …` next to a hand-typed `reviewed by  Opus 5` was two chips carrying
    one thought, and only one of them was checkable."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one", "by": "Opus 5"}],
        findings=[{"title": "f", "body": "<p>b</p>", "source": "/code-review"}]),
        env=_sessionless_env())
    assert "\U0001f916Opus 5 review:" in page, \
        "robot, model, colon — the pill is one sentence, not a label beside a number"
    # And nowhere else: the chip's face already reads `Opus 5 review`, so the hover
    # restating it taught the reader that hovers here are not worth the trouble.
    assert "running on Opus 5" not in page
    assert page.count("Opus 5") == 1


def test_a_page_rebuilt_with_no_idea_who_reviewed_it_says_exactly_that_much(tmp_path):
    """No session, no `by`, no name — and the label says only what it knows rather than
    guessing at the model that is most likely to have been used."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one"}],
        findings=[{"title": "f", "body": "<p>b</p>", "source": "/code-review"}]),
        env=_sessionless_env())
    assert "LLM review" in page
    # Scoped to the masthead on purpose. The claim is about the chip's own words, and
    # the page inlines `server.js` verbatim — prose about what is "running on this
    # server" is a different sentence in a different place, and a whole-page search
    # cannot tell the two apart. It failed on exactly that.
    mast = page[page.index('<header class="masthead">'):page.index("</header>")]
    assert "running on" not in mast


def test_a_hand_typed_diffstat_is_called_out_rather_than_silently_rendered(tmp_path):
    """The check that was missing for six days. It does not fail the build — a page that
    renders beats a build that refuses — but the author cannot now not see it."""
    _, err = _build(tmp_path, dict(
        BARE, scope=[{"label": "files", "value": "25"},
                     {"label": "lines", "value": "+1198 / −863"}]))
    assert "files and lines typed by hand" in err
    assert '{"auto": "diffstat"}' in err


def test_a_computed_diffstat_draws_no_such_warning(tmp_path):
    _, err = _build(tmp_path, dict(BARE, scope=[{"auto": "diffstat"}]))
    assert "typed by hand" not in err


# --------------------------------------------------------------------------- #
# The third pile: what the agent that wrote the code decided without being told
# --------------------------------------------------------------------------- #

REF = "skills/human-review/scripts/build-review-html.py:1"


def _assumption(**kw):
    base = {"title": "Visits inherit the owner's vet", "body": "<p>b</p>", "refs": [REF]}
    base.update(kw)
    return base


def test_an_assumption_wears_one_purple_chip_naming_where_it_came_from(tmp_path):
    """`/code-review` is a provenance a pass earns by running. Nothing ran here — this came
    from the side that wrote the code — so the chip says `assumption`, and it wears the
    purple that used to be a second badge (`your call`) beside it. Two chips on every card
    in the pile spent its whole first line on the one thing all of them have in common."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption()],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    item = re.search(r'<li class="n-assumed">.*?</li>', page, re.S).group(0)
    assert '<span class="badge sev-assumed">assumption</span>' in item
    assert "your call" not in item, "one chip, not two"
    assert "f-src" not in item, "and the grey monospaced stamp is the one that went"
    assert "sev-high" not in item and "sev-med" not in item


def test_an_assumption_with_no_confidence_declared_shows_no_chip_at_all(tmp_path):
    """Not `n/a` — a scale nobody was asked to fill in is a different fact from a model
    that filled it in at the middle, and a page that shows `n/a` for both hides that."""
    assert build._confidence_chip(_assumption()) == ""


def test_an_assumptions_confidence_reads_verbatim_with_its_tooltip(tmp_path):
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(confidence=0.85)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    item = re.search(r'<li class="n-assumed">.*?</li>', page, re.S).group(0)
    # `_confidence_chip` writes a native `title=`; the assembled page's own
    # `one_tooltip_only` postprocess turns every native title into `data-tip`, the same
    # rewrite PlantUML's own hints go through — one tooltip mechanism, page-wide.
    # The tooltip is `CONFIDENCE_TIP`, fixed — Victor's own words, verbatim — not a
    # sentence composed around this item's own number.
    assert '<span class="f-confidence" data-tip="Confidence ∈ [0.9 .. 0.1]">0.85</span>' \
        in item
    assert "sev-med" not in item, "0.85 is not a low confidence"


def test_a_low_confidence_assumption_wears_the_page_own_worth_a_look_amber(tmp_path):
    """Below 0.5 reuses `.sev-med` rather than a colour of its own — the same "worth a
    second look" the rest of the page already spends amber on."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(confidence=0.3)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    item = re.search(r'<li class="n-assumed">.*?</li>', page, re.S).group(0)
    assert 'class="f-confidence sev-med"' in item
    assert ">0.3</span>" in item


def test_assumptions_are_ordered_least_sure_first(tmp_path):
    """The reader's attention goes where the agent itself was least sure, before the
    cards it already trusted — and an item with no confidence at all is neither, so it
    sits after every measured one, in the order the file already put them in."""
    out = build.render_assumptions([
        _assumption(title="sure", confidence=0.9),
        _assumption(title="unsure", confidence=0.4),
        _assumption(title="unmeasured"),
    ])
    assert (out.index("unsure") < out.index("sure") < out.index("unmeasured"))


def test_the_three_piles_are_one_numbered_list(tmp_path):
    """A reader shown three lists that all start at 1 has to add them up by hand. Each
    pile opens where the last one stopped, in the order the content file puts them."""
    page, _ = _build(tmp_path, dict(
        BARE,
        findings=[{"title": f"f{i}", "body": "<p>b</p>"} for i in range(2)],
        assumptions=[_assumption(title=f"a{i}") for i in range(3)],
        autofixes=[{"title": "fixed"}],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "findings"}, {"type": "assumptions", "mode": "A"},
                          {"type": "autofixes"}]}]))
    starts = re.findall(r"counter-reset:f (\d+)", page)
    assert starts == ["2", "5"], "assumptions open at 3, the applied fix lands on 6"


def test_the_order_in_the_content_file_is_the_order_of_the_numbers(tmp_path):
    """The offset is read, not assumed: put the piles the other way round and the numbering
    follows rather than the two of them both starting at 1."""
    page, _ = _build(tmp_path, dict(
        BARE,
        findings=[{"title": "f", "body": "<p>b</p>"}],
        assumptions=[_assumption(title=f"a{i}") for i in range(2)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"}]}]))
    assert re.findall(r"counter-reset:f (\d+)", page) == ["2"]


def test_an_assumption_with_no_code_under_it_is_dropped_and_named(tmp_path):
    """The one item on this page a reader cannot check. A model asked at the end of a long
    session what it was unsure about will write fluent sentences of exactly this shape
    whether or not it ever hesitated, so the anchor is what makes it evidence."""
    page, err = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(refs=[]), _assumption(title="anchored")],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    assert "names no code" in err
    assert "Visits inherit" not in page
    assert "anchored" in page


def test_a_snippet_anchors_an_assumption_just_as_well_as_a_ref(tmp_path):
    page, err = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(refs=[], snippets=[{"ref": REF + "-3",
                                                           "caption": "the branch taken"}])],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    assert "names no code" not in err
    assert "the branch taken" in page


def test_the_three_ways_of_having_no_assumptions_read_differently(tmp_path):
    """"Nothing was assumed" is a claim; "nobody could be asked" is an admission. A blank
    space would read as the friendlier of the two, which is the one that is not known."""
    said = {}
    for mode in ("A", "B", "C"):
        page, _ = _build(tmp_path, dict(
            BARE, tabs=[{"id": "review", "label": "Review",
                         "blocks": [{"type": "assumptions", "mode": mode}]}]))
        said[mode] = page
    assert "named nothing" in said["A"]
    assert "read back in full" in said["B"]
    assert "nobody having been in a position to ask" in said["C"]
    assert "was sure" not in said["A"]


def test_the_alternative_reading_renders_beside_the_assumption(tmp_path):
    """What makes one checkable at a glance: the reader recognises their own intent in one
    of the two readings without opening anything."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(alternative="that a visit carries its own vet")],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    assert "Read the other way" in page
    assert "carries its own vet" in page


def test_piles_out_of_canonical_order_are_called_out(tmp_path):
    """Open, then fixed, then assumed — the same order the counts line above already
    reads them in. `autofixes` before `assumptions` is not itself a violation (it never
    was the rule; the rule is that open leads and assumed trails), so that pair alone is
    left silent below."""
    _, err = _build(tmp_path, dict(
        BARE, autofixes=[{"title": "fixed"}], assumptions=[_assumption()],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "autofixes"}, {"type": "assumptions", "mode": "A"}]}]))
    assert "the piles render as" not in err, \
        "autofixes before assumptions, with no findings block at all, is already canonical"


def test_findings_after_autofixes_is_out_of_canonical_order(tmp_path):
    _, err = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "x"}], autofixes=[{"title": "fixed"}],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "autofixes"}, {"type": "findings"}]}]))
    assert "['autofixes', 'findings']" in err and "['findings', 'autofixes']" in err


def test_assumptions_before_the_defect_piles_is_out_of_canonical_order(tmp_path):
    _, err = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "x"}], assumptions=[_assumption()],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"}]}]))
    assert "['assumptions', 'findings']" in err and "['findings', 'assumptions']" in err


# ── the aftermath band folds tooling commits away from the branch's own ─────────────
def _tooling_repo(tmp_path):
    """`main` with one commit, and a `feature` branch off it — the two refs the aftermath
    band's `base_ref` and `head` name in every real project this runs against."""
    repo = tmp_path / "toolrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feature"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"],
                   check=True, capture_output=True)
    return repo


def _tgit(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _tooling_and_own_commits(repo):
    """One commit on `main` after the fork, cherry-picked onto `feature` (a new sha, the
    same patch — what `git cherry` catches), followed by one commit `feature` made on its
    own. Returns `(cherry_sha, own_sha)`."""
    (repo / "TOOLING.md").write_text("guardrail\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: tooling change on main")
    tooling_on_main = _tgit(repo, "rev-parse", "HEAD")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "cherry-pick", tooling_on_main)
    cherry_sha = _tgit(repo, "rev-parse", "HEAD")
    (repo / "feature.txt").write_text("mine\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "feat: my own change")
    own_sha = _tgit(repo, "rev-parse", "HEAD")
    return cherry_sha, own_sha


def test_a_cherry_picked_tooling_commit_is_told_apart_from_the_branch_own(tmp_path):
    """`git cherry` catches it: a new sha, the same patch as one already on `main`."""
    repo = _tooling_repo(tmp_path)
    cherry_sha, own_sha = _tooling_and_own_commits(repo)
    tooling = build._tooling_commit_shas(repo, "main", [cherry_sha, own_sha])
    assert cherry_sha in tooling
    assert own_sha not in tooling


def test_a_merged_ancestor_commit_is_told_apart_too(tmp_path):
    """`git cherry base head` restricts itself to `base..head` from the start, so a
    commit that is already reachable from `base` — carried across by a merge rather than
    picked — never appears in its output at all, `-` or `+`. `merge-base --is-ancestor`
    is the check that still catches it."""
    repo = _tooling_repo(tmp_path)
    (repo / "TOOLING.md").write_text("guardrail\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: tooling change on main")
    ancestor_sha = _tgit(repo, "rev-parse", "HEAD")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    (repo / "feature.txt").write_text("mine\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "feat: my own change")
    own_sha = _tgit(repo, "rev-parse", "HEAD")
    tooling = build._tooling_commit_shas(repo, "main", [ancestor_sha, own_sha])
    assert ancestor_sha in tooling
    assert own_sha not in tooling


def _picked_then_merged_repo(tmp_path):
    """The shape the demo branch is actually in, and the one that broke the fold: a pick
    off `main`, *then* a merge of `main`, then the branch's own work. Returns
    `(repo, early_pick_sha, late_pick_sha, own_sha)`."""
    repo = _tooling_repo(tmp_path)
    # One on main, picked onto the feature before anything is merged.
    (repo / "EARLY.md").write_text("early guardrail\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: early tooling on main")
    early_on_main = _tgit(repo, "rev-parse", "HEAD")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "cherry-pick", early_on_main)
    early_pick = _tgit(repo, "rev-parse", "HEAD")
    # main moves again, and the feature takes it as a merge rather than a pick.
    _tgit(repo, "checkout", "-q", "main")
    (repo / "MID.md").write_text("mid\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: mid tooling on main")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "merge", "-q", "--no-ff", "-m", "Merge main: mid tooling", "main")
    # And one more pick, after the merge.
    _tgit(repo, "checkout", "-q", "main")
    (repo / "LATE.md").write_text("late guardrail\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: late tooling on main")
    late_on_main = _tgit(repo, "rev-parse", "HEAD")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "cherry-pick", late_on_main)
    late_pick = _tgit(repo, "rev-parse", "HEAD")
    (repo / "feature.txt").write_text("mine\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "feat: my own change")
    return repo, early_pick, late_pick, _tgit(repo, "rev-parse", "HEAD")


def test_a_pick_made_before_the_branch_merged_the_base_is_still_tooling(tmp_path):
    """The bug the demo branch showed: 8 commits picked off `main`, 2 recognised.

    `git cherry <base> <head>` is a symmetric difference — it matches `base..head`
    against `head..base` — and a branch that has merged the base has emptied the right
    half, because the base's commits are now reachable from the head. Every pick older
    than the first merge came back `+`. Asked per commit, the window is `<sha>..<base>`,
    which is the base's history that *this* commit cannot see, and the match is there.
    """
    repo, early_pick, late_pick, own_sha = _picked_then_merged_repo(tmp_path)
    tooling = build._tooling_commit_shas(repo, "main", [early_pick, late_pick, own_sha])
    assert early_pick in tooling, "picked before the merge, and folded all the same"
    assert late_pick in tooling
    assert own_sha not in tooling


def test_the_merge_that_brought_the_base_in_is_not_a_commit_of_its_own(tmp_path):
    """It carries no patch, so it is neither a pick nor an ancestor of the base and falls
    through both tests — landing in the branch's own list as a row that changed nothing.
    Everything it brought is already listed beside it, folded."""
    repo, early_pick, late_pick, own_sha = _picked_then_merged_repo(tmp_path)
    merge_sha = _tgit(repo, "rev-list", "--merges", "-n", "1", "feature")
    assert build._merge_seam_shas(repo, [merge_sha, own_sha]) == {merge_sha}
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    def entry(sha, subject, path):
        return {"sha": sha, "short": sha[:8], "when": "2026-01-01T00:00:00+00:00",
                "subject": subject, "added": 1, "deleted": 0,
                "files": ([{"path": path, "added": 1, "deleted": 0, "binary": False,
                            "generated": False}] if path else []),
                "generated_only": False, "measured": True}
    (out_dir / build.AFTERMATH_JSON).write_text(json.dumps({
        "review": "deadbeef", "review_short": "deadbee", "head": own_sha,
        "commits": [entry(early_pick, "chore: early tooling on main", "EARLY.md"),
                    entry(merge_sha, "Merge main: mid tooling", None),
                    entry(late_pick, "chore: late tooling on main", "LATE.md"),
                    entry(own_sha, "feat: my own change", "feature.txt")]}),
        encoding="utf-8")
    out = build.aftermath_html(out_dir, repo, base_ref="main")
    assert "2 tooling commits merged from main" in out, \
        "both picks, and the merge counted in neither half"
    assert "Merge main: mid tooling" not in out
    assert "1 commit, 1 line changed since the agent finished" in out


def test_no_base_ref_folds_nothing(tmp_path):
    """Best-effort: nothing to ask means every commit stays the branch's own rather than
    a guess — a tooling commit left in the list is a smaller lie than a branch commit
    folded away."""
    assert build._tooling_commit_shas(tmp_path, None, ["abc123"]) == set()


def test_aftermath_band_folds_tooling_and_excludes_it_from_the_headline(tmp_path):
    """The count and the lines in the band's own headline are the branch's, not the
    aftermath step's raw total: a page that still says "2 commits, 4 lines" with one of
    them a guardrail cherry-pick is the drift this fold exists to stop."""
    repo = _tooling_repo(tmp_path)
    cherry_sha, own_sha = _tooling_and_own_commits(repo)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = {
        "review": "deadbeef", "review_short": "deadbee", "head": own_sha,
        "commits": [
            {"sha": cherry_sha, "short": cherry_sha[:8], "when": "2026-01-01T00:00:00+00:00",
             "subject": "chore: tooling change on main",
             "files": [{"path": "TOOLING.md", "added": 1, "deleted": 0, "binary": False,
                        "generated": False}],
             "added": 1, "deleted": 0, "generated_only": False, "measured": True},
            {"sha": own_sha, "short": own_sha[:8], "when": "2026-01-02T00:00:00+00:00",
             "subject": "feat: my own change",
             "files": [{"path": "feature.txt", "added": 3, "deleted": 0, "binary": False,
                        "generated": False}],
             "added": 3, "deleted": 0, "generated_only": False, "measured": True},
        ],
    }
    (out_dir / build.AFTERMATH_JSON).write_text(json.dumps(doc), encoding="utf-8")
    out = build.aftermath_html(out_dir, repo, base_ref="main")
    assert "1 commit, 3 lines changed since the agent finished" in out
    assert '<details class="toolcommits">' in out
    assert "1 tooling commit merged from main" in out
    assert "feat: my own change" in out
    assert "chore: tooling change on main" in out  # still named, inside the fold


def test_aftermath_band_says_only_tooling_when_nothing_else_moved(tmp_path):
    """Every commit since the review is `main`'s own: the headline drops the alarm
    entirely rather than reporting `0 commits, 0 lines changed`, which would read as a
    measurement rather than as the good news it is."""
    repo = _tooling_repo(tmp_path)
    (repo / "TOOLING.md").write_text("guardrail\n")
    _tgit(repo, "add", "-A")
    _tgit(repo, "commit", "-qm", "chore: tooling change on main")
    tooling_on_main = _tgit(repo, "rev-parse", "HEAD")
    _tgit(repo, "checkout", "-q", "feature")
    _tgit(repo, "cherry-pick", tooling_on_main)
    cherry_sha = _tgit(repo, "rev-parse", "HEAD")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = {
        "review": "deadbeef", "review_short": "deadbee", "head": cherry_sha,
        "commits": [
            {"sha": cherry_sha, "short": cherry_sha[:8], "when": "2026-01-01T00:00:00+00:00",
             "subject": "chore: tooling change on main",
             "files": [{"path": "TOOLING.md", "added": 1, "deleted": 0, "binary": False,
                        "generated": False}],
             "added": 1, "deleted": 0, "generated_only": False, "measured": True},
        ],
    }
    (out_dir / build.AFTERMATH_JSON).write_text(json.dumps(doc), encoding="utf-8")
    out = build.aftermath_html(out_dir, repo, base_ref="main")
    assert "Only tooling from main since the agent finished" in out
    assert "1 tooling commit merged from main" in out
    assert "0 commit" not in out


def test_an_assumptions_block_nobody_configured_a_mode_for_weighs_nothing(tmp_path):
    """A content file that declares the block and fills in neither items nor mode has
    nothing to say, and a tab built on it alone is dropped rather than kept and empty."""
    page, err = _build(tmp_path, dict(
        BARE, tabs=[*BARE["tabs"],
                    {"id": "review", "label": "Review",
                     "blocks": [{"type": "assumptions"}]}]))
    assert ">Review<" not in page


def test_the_list_lede_counts_all_three_piles(tmp_path):
    """It described two piles and the page grew a third. Every number in it is counted."""
    page, _ = _build(tmp_path, dict(
        BARE,
        findings=[{"title": f"f{i}", "body": "<p>b</p>"} for i in range(9)],
        assumptions=[_assumption(title=f"a{i}") for i in range(2)],
        autofixes=[{"title": "x"} for _ in range(3)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"},
                          {"type": "autofixes"}]}]))
    assert "2 implementation assumptions" in page
    assert "yours to confirm" not in page, \
        "the card's own purple chip already says which pile it is"
    assert "9 open LLM review issues" in page
    assert "3 auto-fixed" in page
    assert ('<a href="#first">9 open LLM review issues</a> &middot; '
            '<a href="#fixed">3 auto-fixed</a> &middot; '
            '<a href="#assumed">2 implementation assumptions</a>') in page, \
        "what a pass found comes first; what no pass could find comes after it — and every "\
        "clause is the jump to the chapter it counts"
    assert page.index('class="sub counts pilelede"') < page.index("Requires human review"), \
        "the line counts all three piles, so it cannot sit under the heading of one"
    assert "greyed out" not in page, \
        "the applied fixes are visibly grey"
    assert "stamped with" not in page, \
        "every item carries its source beside its own title"


def test_the_lede_counts_the_coder_pile_at_zero_too(tmp_path):
    """A pile that renders only when it is non-empty disappears exactly where the reader
    needs it: a page silent about what the coder guessed at and a coder who guessed at
    nothing look identical."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"}]}]))
    assert "0 implementation assumptions" in page
    assert "1 open LLM review issue" in page


def test_the_lede_does_not_count_a_pile_nobody_could_be_asked_for(tmp_path):
    """Mode C is the one case where the zero would be the lie: no transcript survived, so
    nothing was asked and `0 coder assumptions` would be the page claiming an answer it never got."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "C"}, {"type": "findings"}]}]))
    assert "coder could not be asked" in page
    assert "0 coder assumption" not in page


def test_a_page_that_declares_no_assumptions_block_is_told_so(tmp_path):
    """The third pile is the one part of the page no later pass can reconstruct, so its
    absence is noisy at build time rather than silent on the page."""
    _, err = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        tabs=[{"id": "review", "label": "Review", "blocks": [{"type": "findings"}]}]))
    assert "no 'assumptions' block" in err


def test_the_lede_lands_on_the_pile_that_opens_the_list_whichever_it_is(tmp_path):
    """Pinned to `findings`, a lede describing three piles renders underneath one the
    reader has already walked past."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        assumptions=[_assumption()],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"}]}]))
    assert page.count("1 implementation assumption</a>") == 1, "said once, not once per pile"
    assert page.index("1 implementation assumption</a>") < page.index("Requires human review")


def test_the_lede_is_counts_and_nothing_else(tmp_path):
    """The stamp clause was the last of the three that described how the list looks, and
    `worst first` was the last of *those*: an ordering the reader can see, in a line whose
    whole job is the numbers they cannot. What names a pile now names who raised it."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        tabs=[{"id": "review", "label": "Review", "blocks": [{"type": "findings"}]}]))
    assert ('<p class="sub counts pilelede">'
            '<a href="#first">1 open LLM review issue</a></p>') in page
    assert "stamped with" not in page and "worst first" not in page


def test_the_pilelede_carries_a_scroll_spy_that_marks_the_chapter_in_view(tmp_path):
    """`.here` is set by the script beside the row, never by CSS alone — with JS off (or
    in a caller that renders a pile bare, with no `<script>` at all) the links stay plain
    and clickable, exactly as they were before the spy existed."""
    build.reset_list()
    lede = build.opening_lede({
        "findings": [{"title": "f"}],
        "tabs": [{"id": "review", "label": "R", "blocks": [{"type": "findings"}]}]})
    assert lede.rstrip().endswith("</script>")
    assert "IntersectionObserver" in lede
    assert "querySelector('.pilelede')" in lede
    assert "classList.toggle('here'" in lede
    assert "addEventListener('resize'" in lede
    # It rides only on the one paragraph it belongs beside, never printed on its own.
    assert lede.count("<script>") == 1
    # `_lede_above` prints the lede *before* the pile's own heading, so this script's tag
    # lands in the document ahead of `#first`/`#fixed`/`#assumed` — a bare top-level
    # `getElementById` at that point finds none of them and the whole thing would
    # silently no-op. Deferred to `DOMContentLoaded` (or run at once if that already
    # fired), the same three ids exist wherever the tag sits.
    assert "document.readyState" in lede and "DOMContentLoaded" in lede


def test_a_count_with_no_chapter_to_jump_to_is_not_a_link(tmp_path):
    """The lede counts `autofixes` from the spec, but the jump belongs to the block. A page
    that carries applied fixes and never lays them out has nothing to scroll to, and a dead
    anchor is worse than a number that never claimed to be clickable."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "f", "body": "<p>b</p>"}],
        autofixes=[{"title": "x"}],
        tabs=[{"id": "review", "label": "Review", "blocks": [{"type": "findings"}]}]))
    assert '<a href="#first">1 open LLM review issue</a>' in page
    assert "1 auto-fixed" in page and '<a href="#fixed">' not in page


def test_the_github_link_tooltip_says_only_what_its_label_cannot():
    """The face is `\u2197 on GitHub`. A tooltip that opens with "Open" and closes with
    "on github.com" spends its whole width restating that, and the one thing a reader
    cannot see — that the link lands on a single file of a compare page that can be forty
    long — was the clause in the middle."""
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE,
                               capture_output=True, text=True).stdout.strip())
    link = build._github_compare_link("README.md", "HEAD~1", root, head="HEAD")
    tip = re.search(r'data-tip="([^"]*)"', link).group(1)
    assert "github" not in tip.lower(), "the label already says where it goes"
    assert not tip.startswith("Open"), "every link opens something"
    assert "compare page" in tip


def _repo_with_a_buried_file(tmp_path):
    """One commit deep inside a Java-shaped path, which is where the ceremony lives."""
    import subprocess as sp
    r = tmp_path / "repo"
    deep = r / "petclinic-backend/src/main/java/victor/training/petclinic/repository"
    deep.mkdir(parents=True)
    sp.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    sp.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    (deep / "VetRepository.java").write_text("one\ntwo\n")
    (r / "README.md").write_text("one\ntwo\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True)
    (deep / "VetRepository.java").write_text("one\nTWO\n")
    (r / "README.md").write_text("one\nTWO\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "the change"], check=True)
    return r


def test_the_diff_header_shows_the_name_and_keeps_the_path_on_hover(tmp_path):
    """A repo-relative Java path spends five segments on ceremony before it reaches the
    one word that says which file this is, and the header is where a reader looks to
    answer exactly that."""
    r = _repo_with_a_buried_file(tmp_path)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    head = build.diff_html(rel, "HEAD^", r, head="HEAD").split("</div>")[0]
    assert ">VetRepository.java<" in head
    assert f'data-tip="Open in VS Code: {rel}"' in head
    assert f">{rel}<" not in head, "the ceremony is on hover, not in the face"


def test_a_text_tooltip_stays_on_one_row_and_wraps_only_when_the_row_would_not_fit():
    """The 22rem wrap folded "Open in VS Code: petclinic-test/src/add-visit.spec.ts" in
    the middle of the file name. A path is read at a glance or not at all, so a plain-text
    tip goes on one row as wide as the screen allows; only a row wider than the viewport
    falls back to the wrapping box, and markup tips (lists) always wrap."""
    js = build.TIP_JS
    assert ".tip.oneline{white-space:nowrap;max-width:calc(100vw - 16px)}" in js
    assert "bubble.classList.toggle('oneline', !html)" in js
    assert "bubble.scrollWidth > window.innerWidth - 16) bubble.classList.remove('oneline')" in js


def test_a_file_at_the_repo_root_gets_no_tooltip_repeating_its_own_name(tmp_path):
    r = _repo_with_a_buried_file(tmp_path)
    head = build.diff_html("README.md", "HEAD^", r, head="HEAD").split("</div>")[0]
    assert ">README.md<" in head
    assert 'data-tip="Open in VS Code"' in head, "no bubble restating the name"


def test_a_diff_carries_both_ways_out_in_its_own_header(tmp_path):
    """The reader who wants this diff somewhere they can scroll it wants it before reading
    the excerpt, not after — a link under forty lines of diff is one they have to come
    back up from. Both destinations named, in the header's corner, and nothing left in a
    footer row underneath."""
    import subprocess as sp
    r = _repo_with_a_buried_file(tmp_path)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    out = build.diff_html(rel, "HEAD^", r, head="HEAD")
    corner = out.split('<div class="ghdiff-scroll">')[0]
    assert "ico-vsc" in corner and "ico-gh" in corner
    # And it is the page's one source bar doing it, not a header private to this block.
    assert corner.startswith('<div class="ghdiff"><div class="srcbar">')
    assert "srcref" not in out.split('</table></div>')[-1], \
        "the links moved into the header; nothing links from a footer under the diff"


def test_the_three_tabs_head_a_quoted_block_with_the_same_bar(tmp_path, monkeypatch):
    """One component, not three headers that happen to look alike.

    The Tests tab's snippets, the Review tab's applied fixes and the Logging tab's
    statements each grew their own version of this row, and they disagreed on every part
    of it — which end the file sat at, whether the diff handles were there, whether the
    face was the path or the name. A reader crossing tabs had to relearn it each time. So
    the shape is asserted once, across all three producers: same class, same file-name
    face, same full path on hover."""
    r = _repo_with_a_buried_file(tmp_path)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")

    bars = [
        build.diff_html(rel, "HEAD^", r, head="HEAD"),                    # Review
        build.snippet_html(f"{rel}:1-2", None, r, exact=True),            # Tests
    ]
    for out in bars:
        bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
        assert 'class="srcref srcbar-path"' in bar
        assert ">VetRepository.java" in bar, "the name is the face"
        assert f'data-tip="Open in VS Code: {rel}"' in bar, "the path is the hover"


def test_an_excerpt_short_of_its_own_label_is_called_out(capsys):
    """A window that stops early looks exactly like one that does not.

    The lines a fragment bakes in and the label saying which lines they are can disagree,
    and when they do the page shows a test method with no closing brace — which the reader
    cannot tell from a method that has none, having no file open to count against. Every
    excerpt in one map was cut a line short and shipped that way. The arithmetic is
    something the build can do and the author cannot."""
    short = json.dumps({"tests": {"t": {"parts": [
        {"label": "src/a.ts:10-14", "html": ["a", "b", "c", "d"]}]}}})
    build.check_baked_excerpts(f'<script class="rm-data">{short}</script>')
    err = capsys.readouterr().err
    assert "src/a.ts:10-14" in err and "labelled 5 lines but quotes 4" in err

    whole = json.dumps({"tests": {"t": {"parts": [
        {"label": "src/a.ts:10-14", "html": ["a", "b", "c", "d", "e"]}]}}})
    build.check_baked_excerpts(f'<script class="rm-data">{whole}</script>')
    assert capsys.readouterr().err == "", "a excerpt that adds up says nothing"


def test_the_bar_reads_handles_then_file_then_badge(tmp_path, monkeypatch):
    """One order, top to bottom of the page: how to open it, which file, what changed.

    The handles sit against the name because the name is what they open — parked at the
    other end of the row they were a bar's width from the only word saying what they would
    open. The badge trails for the opposite reason: "new file" is a fact *about* a file,
    and leading with it makes the reader hold it in mind until the bar finally says which
    file is new. Asserted on both producers, because a bar that only the Tests tab obeys
    is the three-headers problem coming back."""
    r = _repo_with_a_buried_file(tmp_path)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/victorrentea/petclinic.git"], check=True)
    badged = 0
    for out in (build.diff_html(rel, "HEAD^", r, head="HEAD"),
                build.snippet_html(f"{rel}:1-2", None, r, exact=True)):
        bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
        handles, name = bar.index("ico-vsc"), bar.index("srcbar-path")
        assert handles < name, bar
        # A bar without a badge is a legal bar — the block is simply not claiming to be
        # new or changed. Where there is one, it trails; and one of the two producers here
        # always has one, so the assertion cannot pass by never running.
        mark = next((m for m in ("code-badge", 'class="stat"') if m in bar), None)
        if mark:
            badged += 1
            assert name < bar.index(mark), bar
    assert badged, "neither producer emitted a badge — the order went untested"


def test_a_quoted_snippet_offers_the_same_two_ways_out_a_diff_does(tmp_path, monkeypatch):
    """A snippet used to be a dead end: it showed what the code says now and left "what
    changed?" to the reader's imagination. It carries the same two handles the Review
    tab's diffs carry — the editor, and github.com — because it is quoting the same
    change, and the badge beside them already claims the lines are new."""
    import subprocess as sp
    r = _repo_with_a_buried_file(tmp_path)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    out = build.snippet_html(f"{rel}:1-2", None, r, exact=True)
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert "ico-vsc" in bar and "ico-gh" in bar
    assert 'class="srcref diffref srcbar-diff"' in bar   # the pill face, not the prose one


def _repo_whose_change_is_far_from_the_quote(tmp_path):
    """A file changed at the top *and* in the middle — the shape that made this wrong.

    Line 2 is an import; line 10 is the statement a snippet would quote. Anything that
    aims at "the first line that differs" lands on the import."""
    import subprocess as sp
    r = tmp_path / "repo"
    r.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    sp.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    body = ["package p;", "import a.B;"] + [f"  int f{n}() {{ return {n}; }}" for n in range(3, 13)]
    (r / "A.java").write_text("\n".join(body) + "\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True)
    body[1] = "import a.C;"                                  # line 2 — the decoy
    body[9] = "  int f10() { return 999; }"                  # line 10 — what a box quotes
    (r / "A.java").write_text("\n".join(body) + "\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "the change"], check=True)
    return r


def test_a_snippets_handles_open_where_its_own_face_says(tmp_path, monkeypatch):
    """The bug this fixes: a bar reading `A.java:10` whose two buttons went to line 2.

    Each handle used to pick its own landing — the editor "the first line that differs",
    which in a real class is an import fifty lines above the finding; github.com the top
    of the file inside a compare page. Both are now the line the face names, because the
    three parts of one bar may not be three answers to one question."""
    r = _repo_whose_change_is_far_from_the_quote(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    monkeypatch.setenv("HUMAN_REVIEW_DIFF_URI_HANDLER", "victorrentea.victor-vsc")
    build.diff_uri_handler.cache_clear()
    out = build.snippet_html("A.java:10", None, r, exact=True)
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert ">A.java:10</a>" in bar
    assert "/A.java:2:1" not in bar, "the decoy import, which is where this used to land"
    assert bar.count("/A.java:10:1") == 2, "the editor handle and the bar's own link"
    assert "line=10" in bar, "and the URI the extension gets, for the diff itself"
    assert re.search(r"#diff-[0-9a-f]{64}R10", bar), "github.com anchors the same line"
    build.diff_uri_handler.cache_clear()


def test_a_quoted_line_the_compare_page_never_draws_keeps_the_file_anchor(tmp_path,
                                                                          monkeypatch):
    """`#diff-<sha>R7` on a line github.com does not render is worse than no line at all.

    A fragment that matches no id does not fall back to the file anchor it was appended
    to — the browser simply does not scroll, and the reader lands at the top of a compare
    page that can be forty files long. So an untouched line far from any hunk keeps the
    file anchor and gives up the line."""
    r = _repo_whose_change_is_far_from_the_quote(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    build._shown_in_compare.cache_clear()
    out = build.snippet_html("A.java:6", None, r, exact=True)
    href = re.search(r'href="(https://[^"]*/compare/[^"]*)"', out).group(1)
    assert re.search(r"#diff-[0-9a-f]{64}$", href), href
    # …and the editor, which can open any line of a file, still goes to the quoted one.
    assert "/A.java:6:1" in out
    build._shown_in_compare.cache_clear()


def test_the_logging_boxs_handles_follow_it_to_the_statement(tmp_path, monkeypatch):
    """`link_at` moves the whole bar, not just its face.

    A logging window pulls in the lines a logged value came *from*, so the window opens
    above the statement the box is about. The face already said `:10`; the two handles
    beside it were still aiming at the file's first change."""
    r = _repo_whose_change_is_far_from_the_quote(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    build._shown_in_compare.cache_clear()
    out = build.snippet_html("A.java:8-11", None, r, exact=True, link_at=(10, 5))
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert ">A.java:10</a>" in bar
    assert "/A.java:8:1" not in bar, "the window's first line is not what the box is about"
    assert "/A.java:10:1" in bar and "/A.java:10:5" in bar
    assert re.search(r"#diff-[0-9a-f]{64}R10", bar)
    build._shown_in_compare.cache_clear()


def test_a_snippet_whose_base_is_not_there_still_gets_its_bar(tmp_path, monkeypatch):
    """Each handle is emitted only where that side can really open what it promises — a
    base that does not resolve has no diff to show, and a dead button is worse than no
    button. What must not happen is the bar going with it: the file it came from is a
    fact regardless of what git can be asked."""
    r = _repo_with_a_buried_file(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "no/such/ref")
    out = build.snippet_html("README.md:1-2", None, r, exact=True)
    assert '<div class="srcbar">' in out
    assert "ico-vsc" not in out and "ico-gh" not in out
    assert ">README.md:1-2</a>" in out


def test_a_pinned_fix_the_file_has_moved_off_gets_no_editor_link(tmp_path):
    """The editor can only compare a ref against the file on disk. Pinned to a commit the
    file has since moved off, `base -> disk` is a different comparison than the one the
    block is showing — so that link is dropped rather than pointed at it, and github.com,
    which can show the pinned pair, is left to stand alone."""
    import subprocess as sp
    r = _repo_with_a_buried_file(tmp_path)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    (r / rel).write_text("one\nTWO\nand something later\n")
    out = build.diff_html(rel, "HEAD^", r, head="HEAD")
    assert "ico-vsc" not in out
    assert "ico-gh" in out


def test_the_github_link_lands_on_the_line_the_change_is_on(tmp_path):
    """A file in a compare page opens at its own first line, which for a long class is
    nowhere near the four lines the review is about — so the reader arrives on github.com
    and starts hunting a second time, having been sent there to stop hunting."""
    r = _repo_with_a_buried_file(tmp_path)
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    out = build.diff_html(rel, "HEAD^", r, head="HEAD")
    href = re.search(r'href="([^"]*compare[^"]*)"', out).group(1)
    assert re.search(r"#diff-[0-9a-f]{64}R2$", href), href
    # Short-faced in a diff header, the tip is short too: the mark is hovered to ask what
    # it is, not for a sentence about where in a forty-file compare page it lands. That
    # sentence still stands on the prose link, which has room for it.
    assert 'data-tip="GitHub"' in out
    prose = build._github_compare_link(rel, "HEAD^", r, "HEAD", 2, "R")
    assert "This change, in the compare page" in prose


def test_a_pure_deletion_lands_on_the_left_side(tmp_path):
    """The one case with no right side to land on."""
    import subprocess as sp
    r = tmp_path / "repo"
    r.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    sp.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    (r / "a.txt").write_text("one\ntwo\nthree\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True)
    (r / "a.txt").write_text("one\nthree\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "drop a line"], check=True)
    out = build.diff_html("a.txt", "HEAD^", r, head="HEAD")
    href = re.search(r'href="([^"]*compare[^"]*)"', out).group(1)
    assert href.endswith("L2"), href


def test_the_review_tab_label_is_the_word_alone():
    """The 🤖 announced that the tab was machine-produced, which the source stamp on every
    item inside it already says, one item at a time."""
    schema = (HERE.parent / "reference" / "content-schema.md").read_text(encoding="utf-8")
    assert "🤖 Review" not in schema


def test_shift_wheel_scrolls_a_wide_block_sideways():
    """The only way to reach the right-hand end of a long quoted line used to be dragging
    the block's own scrollbar, which makes the reader leave the line they were reading.
    Shift+wheel has to move the box under the cursor — and only when that box has
    somewhere left to go, so the page still scrolls once it is at the end."""
    js = build.HSCROLL_JS
    assert "ev.shiftKey" in js
    assert "{ passive: false }" in js       # a passive listener cannot claim the gesture
    assert "scrollLeft" in js
    assert "if (box.scrollLeft !== before) ev.preventDefault();" in js
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert "{HSCROLL_JS}" in src            # and it is actually emitted into the page


def test_the_recordings_are_a_registry_the_tv_reads_not_a_list(tmp_path):
    """There used to be a list of rows here, one per recording, each framing the viewer.
    Every word on it was already on the covering-tests map, so what is emitted now is
    only the registry the 📺 on those rows reads: the viewer, and per test the key the
    map uses, the zip, and the line that opens it natively off disk."""
    doc = {"recorded": 3, "omitted": 0, "untraced": 0, "viewer": "assets/tv/index.html",
           "tests": [
               {"title": "guards the reset route", "file": "guard.spec.ts", "line": 5,
                "status": "passed", "duration": 40, "trace": "t/1.zip"},
               {"title": "adds a visit with a vet", "file": "src/add-visit.spec.ts",
                "line": 52, "status": "passed", "duration": 4000, "trace": "t/2.zip"},
               {"title": "ran with tracing off", "file": "chat.spec.ts", "line": 20,
                "status": "skipped", "duration": 0},
           ]}
    out, n = build.render_traces(doc, tmp_path, tmp_path / ".human-review",
                                 {("add-visit.spec.ts", 52)})
    assert out.startswith('<script type="application/json" id="hr-traces">')
    assert "<details" not in out and "Step through" not in out and "adds a visit" not in out
    reg = json.loads(re.search(r">(\{.*\})</script>", out).group(1))
    assert reg["viewer"] == "assets/tv/index.html"
    assert [e["test"] for e in reg["tests"]] == ["guard.spec.ts:5", "add-visit.spec.ts:52"]
    assert n == 2, "a test with no trace is not a recording"
    assert reg["tests"][1]["cmd"] == "npx playwright show-trace .human-review/t/2.zip"


def test_a_trace_row_is_addressed_by_its_test_and_the_header_says_which_page_this_is(tmp_path):
    """The covering-tests map names a test by file and declaration line; a trace row
    carries the same key, so the 📺 the page hangs on the map's row is a lookup. And the
    header carries one chip that says whether this copy is served or static, emitted as
    static and promoted by the probe — never drawn live and demoted later."""
    doc = {"recorded": 1, "omitted": 0, "untraced": 0, "viewer": "assets/tv/index.html",
           "tests": [{"title": "adds a visit", "file": "src/add-visit.spec.ts", "line": 52,
                      "status": "passed", "duration": 10, "trace": "t/1.zip"}]}
    out, _ = build.render_traces(doc, tmp_path, tmp_path / ".human-review")
    assert '"test": "add-visit.spec.ts:52"' in out
    page, _ = _build(tmp_path, BARE)
    row = page[page.index('<div class="titlerow'):page.index("</div>", page.index('<div class="titlerow'))]
    assert '<button type="button" class="chip chip-mode copycmd" id="hr-mode"' in row, \
        "in the title row, against the score"
    assert ">static</button>" in row
    assert "hr-mode" not in page[page.index("<footer>"):page.index("</footer>")]
    # The badge copies the way out of static: serve this directory, open the page from
    # the URL the server prints — never a port assumed in advance.
    m = re.search(r'id="hr-mode" data-copy="([^"]+)"', row)
    line = html.unescape(m.group(1))
    assert line.startswith("cd ") and "serve-review.py" in line and "--page review.html" in line
    assert 'u="$(' in line and 'open "$u"' in line and "7654" not in line
    assert "chip.removeAttribute('data-copy')" in page, "served: nothing left to copy"
    assert "chip.textContent = 'served'" in page
    # And in the tab strip, where a reader picks between several of these — one per
    # branch, a static copy beside a live one — long before anything in the page is on
    # screen. Play and not a green dot: green here means a check passed, and a tab that
    # turned green because a server is up would be saying the branch is fine.
    assert "document.title = '\u25b6\ufe0f ' + document.title" in page
    assert "document.title.indexOf('\u25b6') !== 0" in page, "prepended once, not per probe"
    # Served, the diagram block stops sending the reader to a terminal: the first offer
    # in its sentence becomes one that does the job, and the wording is rewritten to match.
    # The diagram block reads the same in both worlds; only the hover changes, from what
    # the button needs to what it does.
    assert "b.getAttribute('data-tip-served')" in page
    assert "querySelectorAll('.rm-t[data-id]')" in page
    # Served, the 📺 is a link into the viewer in a new window; off disk it copies the
    # show-trace line. The page carries no trace list, no frame, no rows.
    assert "'Open test replay in a new window'" in page
    assert "tv.target = '_blank'" in page and "copy(t.cmd)" in page
    assert "traceview" not in page and 'class="traces"' not in page


# --------------------------------------------------------------------------- #
# the 🕵️: from a test on the Tests tab to the sequence it drew
# --------------------------------------------------------------------------- #
def _genseq_fixture(tmp_path: Path) -> tuple[str, str]:
    """A feature file with two scenarios, one of them tagged, and the picture the
    generator left beside it — one per scenario, named after the one it drew."""
    rel = "test/add-visit.feature"
    puml = rel + ".remembers-the-vet.genseq.puml"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(
        "Feature: visits\n\n  @generate_sequence\n  Scenario: remembers the vet\n"
        "    Then it does\n\n  Scenario: says nobody attended\n    Then it does\n",
        encoding="utf-8")
    (tmp_path / puml).write_text(
        "@startuml\ntitle test/add-visit.feature\n"
        "== [[src://test/add-visit.feature:4{Click to open the test} remembers the vet]] ==\n"
        "Browser -> Backend: [[src://src/main/java/Owner.java:14{tip} Owner.find]]\n"
        "@enduml\n", encoding="utf-8")
    return rel, puml


def test_the_test_behind_a_diagram_is_found_by_asking_the_checkout(tmp_path):
    """`<test>.<scenario>.genseq.puml` cannot be split by looking: a test file has dots of
    its own. The checkout answers it — and for a test that is not in this checkout, which
    is a real case the page has a paragraph for, the slug is cut on its shape."""
    rel, puml = _genseq_fixture(tmp_path)
    assert build.test_of_genseq(puml, tmp_path) == rel
    assert build.test_of_genseq(rel + ".genseq.puml", tmp_path) == rel, "the older spelling"
    gone = "gone/add-visit.spec.ts.adds-a-visit.genseq.puml"
    assert build.test_of_genseq(gone, tmp_path) == "gone/add-visit.spec.ts"


def test_a_diagram_filed_away_from_its_test_still_names_it(tmp_path):
    """The pairing cannot be a fact about directories: a generator is free to collect its
    output somewhere else, and petclinic's does — both suites write into
    `petclinic-test/generated/`, where no arithmetic on the path leads back to the test.
    The picture says which test drew it, on the handle the generator puts on its title."""
    (tmp_path / "petclinic-test" / "src").mkdir(parents=True)
    (tmp_path / "petclinic-test" / "generated").mkdir()
    test_rel = "petclinic-test/src/add-visit.spec.ts"
    (tmp_path / test_rel).write_text("test('Add a visit', () => {});\n", encoding="utf-8")
    puml = "petclinic-test/generated/add-visit.spec.ts.add-a-visit.genseq.puml"
    (tmp_path / puml).write_text(
        "@startuml\n"
        f"title [[src://{test_rel}:1{{Click to open the test}} Add a visit]]\n"
        "participant Browser\n"
        "Browser -> Backend: [[src://petclinic-backend/src/main/java/X.java:9{tip} X.y]]\n"
        "@enduml\n", encoding="utf-8")

    build._declared_test.cache_clear()
    build.genseq_by_test.cache_clear()

    assert build.test_of_genseq(puml, tmp_path) == test_rel
    assert build.genseq_by_test(tmp_path) == {test_rel: (puml,)}
    # …and the reviewer gets the link back to it, which is the whole point of knowing.
    assert 'generated by add-visit.spec.ts' in build._provenance(puml, tmp_path)


def test_an_arrows_handle_is_never_mistaken_for_the_test(tmp_path):
    """Every class and endpoint in the picture carries the same `src://` scheme. Only the
    header names the test, so the read stops where the conversation starts — a diagram
    whose header lost its handle falls back to its name rather than claiming the first
    production class it happens to draw."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Owner.java").write_text("class Owner {}\n", encoding="utf-8")
    puml = "generated/add-visit.spec.ts.add-a-visit.genseq.puml"
    (tmp_path / "generated").mkdir()
    (tmp_path / puml).write_text(
        "@startuml\nparticipant Browser\n"
        "Browser -> Backend: [[src://src/Owner.java:14{tip} Owner.find]]\n@enduml\n",
        encoding="utf-8")
    build._declared_test.cache_clear()
    assert build.test_of_genseq(puml, tmp_path) == "generated/add-visit.spec.ts"


def test_only_the_scenarios_the_generator_drew_are_addressable(tmp_path):
    """`@generate_sequence` is a request; the chapter in the committed .puml is the record
    that it was granted and that there is a picture on this page to point at. So the
    chapters are what is read — and only this file's own, since the same `src://` handle
    is on every class the diagram names."""
    rel, puml = _genseq_fixture(tmp_path)
    assert build._scenarios_drawn(puml, rel, tmp_path) == [(4, "remembers the vet")]
    assert build._scenarios_drawn("test/nothing.genseq.puml", rel, tmp_path) == []


def test_a_pair_is_named_by_its_scenarios_and_addressed_by_its_test(tmp_path):
    """Folded shut, the summary is the whole tab: it has to say which test this is, in the
    words the Tests tab uses for it. The file name is the box those live in and answers a
    question nobody asked — it stays as the tooltip."""
    rel, puml = _genseq_fixture(tmp_path)
    out = build._folded_pair(puml, rel, ["<p>picture</p>"],
                             scenarios=[(4, "remembers the vet")])
    assert f'id="{build.pair_anchor(puml)}"' in out
    assert "<summary data-tip=\"test/add-visit.feature\">remembers the vet</summary>" in out
    assert "add-visit.feature<" not in out, "the basename is not the heading any more"
    # Two chapters in one file are two lines of the contents, on one row.
    two = build._folded_pair(puml, rel, [""], scenarios=[(4, "one"), (9, "two")])
    assert ">one · two</summary>" in two
    # Nothing recorded — the basename is all there is to call it.
    assert ">add-visit.feature</summary>" in build._folded_pair(puml, rel, [""])


def test_a_pair_says_what_kind_of_test_drew_it(tmp_path):
    """Shut, this tab is a list of sentences, and "which of these went through a browser?"
    had no answer short of opening every one. The kind leads the row, in the Tests tab's
    own chip and the Tests tab's own three words — one vocabulary across the page."""
    rel, puml = _genseq_fixture(tmp_path)
    out = build._folded_pair(puml, rel, [""], scenarios=[(4, "remembers the vet")],
                             cat=build._pair_cat(puml, tmp_path))
    assert '<span class="testcat" data-cat="e2e"' in out
    assert ">UI · Gherkin</span>remembers the vet</summary>" in out, "it leads the sentence"
    # The tip is now composed, so it is escaped as one string: a literal em dash, not the
    # `&mdash;` entity that used to be concatenated in after the escaping.
    assert "UI · Gherkin — clicks the screen" in out, "the legend is on the hover"
    # The same three words the requirements map's legend uses, and no fourth.
    assert [c[0] for c in build.TEST_CATS.values()] == ["UI", "API", "unit"]
    # Same three colours as the evidence cards a few hundred lines up the stylesheet.
    assert ".testcat[data-cat=api]" in build.CSS and ".testcat[data-cat=unit]" in build.CSS


def test_a_pair_also_says_what_wrote_the_test(tmp_path):
    """`UI` covered a Playwright spec and a Cucumber feature alike, and the shut row gave
    a reader no way to tell which was which — though only one of them is written in a
    language a non-programmer reads. The runner qualifies the kind inside the same pill."""
    assert ">UI · Gherkin<" in build._cat_chip("e2e", "petclinic-test/src/book.feature")
    assert ">UI · Playwright<" in build._cat_chip("e2e", "petclinic-test/src/add.spec.ts")
    assert ">API · JUnit<" in build._cat_chip("api", "src/test/java/AddVisitApiTest.java")
    # Unlike the kind, this IS the file: no diagram is consulted, and none is needed.
    assert build._pair_runner("a/b.feature") == ("Gherkin", "a Cucumber scenario")
    # Longest suffix first, or a Playwright spec would answer to a bare `.ts` rule.
    assert build._pair_runner("a/b.spec.ts")[0] == "Playwright"
    # An extension this has never heard of leaves the chip exactly as it was.
    assert build._cat_chip("e2e", "a/b.rb") == build._cat_chip("e2e")
    assert ">UI<" in build._cat_chip("e2e", "a/b.rb")
    # …and a runner never conjures a chip where the kind could not be read.
    assert build._cat_chip(None, "a/b.feature") == ""


def test_the_kind_is_read_off_the_diagram_not_guessed_from_the_path(tmp_path):
    """A `.spec.ts` is a Playwright run in one module and a component test in the next, so
    the path settles nothing. The diagram is a record of what the run did, and its first
    lifeline is the end it was driven from — `Browser` through the screens, `Client` from a
    @SpringBootTest, one lifeline alone for a test nobody else could observe."""
    def puml(body, name="x"):
        rel = f"test/{name}.spec.ts.s.genseq.puml"
        (tmp_path / "test").mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("@startuml\n" + body + "\n@enduml\n", encoding="utf-8")
        return rel

    ui = puml("participant Browser\nparticipant Backend\nBrowser -> Backend: x", "a")
    api = puml("participant Client\nparticipant Backend\nClient -> Backend: x", "b")
    unit = puml("participant Component\nComponent -> Component: x", "c")
    assert build._pair_cat(ui, tmp_path) == "e2e"
    assert build._pair_cat(api, tmp_path) == "api"
    assert build._pair_cat(unit, tmp_path) == "unit"
    # `participant "Pet Clinic UI" as UI` — the quoted side is the label, either way round.
    quoted = puml('participant "Pet Clinic UI" as U\nparticipant Backend\nU -> Backend: x', "d")
    assert build._pair_cat(quoted, tmp_path) == "e2e"
    # Declared nothing: PlantUML lets a sender exist from its first message.
    bare = puml("Browser -> Backend: x", "e")
    assert build._pair_cat(bare, tmp_path) == "e2e"
    # Nothing to read at all is no chip, never a guessed one.
    assert build._pair_cat("test/gone.genseq.puml", tmp_path) is None
    assert build._cat_chip(None) == ""
    # An author's say wins: a suite may name its driver something this never heard of.
    assert build._pair_cat(ui, tmp_path, "unit") == "unit"
    assert build._pair_cat(ui, tmp_path, "nonsense") == "e2e", "…but only one of the three"


def test_the_fold_over_a_quoted_test_is_the_blocks_own_source_bar(tmp_path):
    """`the test · lines 60–61,70–94,124–155` above a bar reading `AddVisitApiTest.java:
    60-61,70-94,124-155` said the line numbers twice and the second copy said them beside
    the file they belong to. What is left of the row is the one thing the bar does not
    carry — whether the test is open — so the control and the bar are one line."""
    rel, puml = _genseq_fixture(tmp_path)
    fig = ('<figure class="snippet"><div class="srcbar"><a>x.feature:4</a>'
           '<span class="code-badge">2 lines changed</span></div><pre>code</pre></figure>')
    out = build._folded_pair(puml, rel, ["<p>picture</p>"], [fig],
                             scenarios=[(4, "remembers the vet")])
    assert '<summary><span class="foldlbl"></span><div class="srcbar">' in out
    assert "the test · lines" not in out, "the row it replaced"
    assert out.count('<div class="srcbar">') == 1, "hoisted, not copied"
    assert "<pre>code</pre>" in out
    # Two words for two states, and neither is in the markup: the <details> knows which.
    assert 'content:"Show Test"' in build.CSS and 'content:"Hide Test"' in build.CSS
    # A second excerpt of the same file is a different window and still names itself.
    two = build._folded_pair(puml, rel, [""], [fig, fig])
    assert two.count('<div class="srcbar">') == 2


def test_the_fold_row_draws_what_happened_to_the_file_instead_of_shouting_it(tmp_path):
    """`NEW FILE` in caps beside a file name is read before the name it is a fact about.
    The Tests tab settled this already, one level down, with a page glyph marked in its
    corner — so the row uses that same drawing, and the words move to the hover."""
    rel, puml = _genseq_fixture(tmp_path)

    def row(label, diff="new"):
        fig = ('<figure class="snippet"><div class="srcbar"><a>x.feature:4</a>'
               f'<span class="code-badge" data-diff="{diff}" data-tip="since origin/main">'
               f'{label}</span></div><pre>code</pre></figure>')
        return build._folded_pair(puml, rel, [""], [fig])

    new = row("new file")
    assert "code-badge" not in new and 'class="filemark" data-kind="new"' in new
    assert build.FILE_PLUS in new and build.FILE_PAGE in new
    assert 'aria-label="new file"' in new, "the word is kept for a screen reader"
    assert "New file &mdash; since origin/main" in new, "…and for the hover"
    # `new file` and `new code` are both `new` to git and are not the same fact.
    assert 'data-kind="edited"' in row("new code") and build.FILE_PENCIL in row("new code")
    assert 'data-kind="edited"' in row("2 lines changed", "changed")
    assert 'data-kind="unchanged"' in row("unchanged", "unchanged")
    assert build.FILE_PLUS not in row("unchanged", "unchanged")
    assert ".filemark" in build.CSS


def test_which_pair_is_open_is_in_the_url(tmp_path):
    """A reader who opens a sequence and sends the address sends the picture, not the tab
    it is on. And a click on one of the bar's own links must not fold away the block it
    was about — without silencing the event the editor handler is waiting for."""
    js = build.SEQFOLD_JS
    assert "history.replaceState" in js and "remember(pair.id)" in js
    assert "pair.closest('.panel')" in js, "closing gives the hash back to the tab"
    assert "requestAnimationFrame" in js and "stopPropagation()" not in js


def test_a_pair_is_born_open_and_folded_by_a_script_that_runs_after_the_measuring(tmp_path):
    """The tab opens on its table of contents, but the markup cannot say so: the click
    targets inside every diagram are sized with getBBox(), which returns zeros inside a
    closed <details>. Born shut, a sequence would silently lose every handle on it."""
    rel, puml = _genseq_fixture(tmp_path)
    assert '<details class="testpair" open' in build._folded_pair(puml, rel, [""])
    js = build.SEQFOLD_JS
    assert "details.testpair[open]" in js and "pair.open = false" in js
    assert "pair.id === wanted" in js, "the pair a deep link names stays open"
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert src.index("{SEQFOLD_JS}") > src.index("{GENSEQ_JS}"), "after the measuring"
    assert src.index("{SEQFOLD_JS}") < src.index("{TABS_JS}")


def test_the_sequences_are_a_registry_the_detective_reads(tmp_path):
    """The map addresses a row by repo-relative path and declaration line. The registry
    carries the same key per drawn scenario, so the 🕵️ is a lookup and not a guess."""
    js = build.SEQLINK_JS
    assert "document.getElementById('hr-genseq')" in js
    assert "querySelectorAll('.rm-t[data-id]')" in js
    assert "'.rm-seq'" in js and "\\uD83D\\uDD75\\uFE0F" in js
    # It must not toggle the row it sits on, and it must open a pair a reader folded away.
    assert "ev.stopPropagation()" in js and "target.open = true" in js
    assert "'seq-hit'" in js, "the pair says once that it is the one that was asked for"
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert "{SEQLINK_JS}" in src


def test_the_paired_card_draws_its_controls_and_its_name_on_one_row():
    """A paired card had two half-empty rows stacked: buttons hard left on one, the .puml
    path hard right on the other. The bar stays inside .dgmviews — the stylesheet paints
    the buttons off [data-state] there — and the header's contents move into it."""
    js = build.DGM_VIEWS_JS
    assert "document.querySelectorAll('.diagram.dgm-bare > .head')" in js
    assert "while (head.firstChild) bar.appendChild(head.firstChild);" in js
    # Both lookups that used to start from `views` now start from the card.
    assert "(views.closest('.diagram') || views).querySelectorAll('.dgmbar button[data-go]')" in js
    assert "bar.closest('.diagram').querySelector('.dgmviews')" in js, \
        "the merged row keeps the large hit area the header used to be"


def test_one_files_excerpts_are_shared_out_among_its_scenarios(tmp_path):
    """One diagram per test file needed no sharing. Per scenario, four pictures of one
    .feature would each have repeated the same thirty lines — or, the way it first worked
    out, the first would have taken all of them and the rest shown none."""
    rel = "test/two.feature"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("x\n" * 40, encoding="utf-8")
    pumls = []
    for slug, line, title in (("one", 10, "one"), ("two", 30, "two")):
        puml = f"{rel}.{slug}.genseq.puml"
        (tmp_path / puml).write_text(
            f"@startuml\n== [[src://{rel}:{line}{{t}} {title}]] ==\nA -> B: x\n@enduml\n",
            encoding="utf-8")
        pumls.append(puml)
    entries = [(p, None) for p in pumls]
    snippets = [{"ref": f"{rel}:8-14"},          # around the first scenario
                {"ref": f"{rel}:28-34"},          # around the second
                {"ref": f"{rel}:1-4"}]            # a Background: nobody's in particular
    used = set()
    quoted = build._share_excerpts(rel, entries, snippets, used, tmp_path)
    assert len(used) == 3, "every excerpt of a paired file counts as used"
    assert [x["ref"] for x in quoted[pumls[0]]] == [f"{rel}:8-14", f"{rel}:1-4"], \
        "the unattached block leads, under the first"
    assert [x["ref"] for x in quoted[pumls[1]]] == [f"{rel}:28-34"]


def test_the_review_pill_counts_open_issues_and_not_the_render_weight(tmp_path):
    """The pill said **10** while the row under it said `6 open · 3 auto-fixed ·
    7 assumptions` and the masthead said `9 raised`. Ten was findings + auto-fixes + the
    assumptions pile's fixed weight of 1 — a layout sentinel that keeps an empty pile's
    "which kind of empty this is" sentence alive, read out to the reader as a count of
    something. It also sat next to the `6 /10` score chip, where 10 read as a denominator.
    """
    spec = {"findings": [{"title": f"f{i}", "body": "b"} for i in range(6)],
            "autofixes": [{"title": f"a{i}", "body": "b"} for i in range(3)],
            "assumptions": [{"title": f"s{i}", "body": "b"} for i in range(7)]}
    assert build.review_tab_badge(spec)["count"] == 6
    assert build.pile_numbers(spec)[0] == 6, "the same arrays the header chip reads"
    assert build.review_tab_badge(spec)["label"].startswith("6 open review issues")

    page, _ = _build(tmp_path, {
        "title": "t", "summary": "<p>s</p>",
        "sections": [{"id": "s", "title": "S", "body": "<p>b</p>"}],
        **spec,
        "tabs": [{"id": "review", "label": "Review", "count": True,
                  "blocks": [{"type": "findings"}, {"type": "autofixes"},
                             {"type": "assumptions"}]},
                 {"id": "other", "label": "Other",
                  "blocks": [{"type": "section", "id": "s"}]}]})
    pill = re.search(r'id="tabbtn-review"[^>]*>Review<span class="n"([^>]*)>(\d+)</span>',
                     page)
    assert pill, "the Review tab still carries a number"
    assert pill.group(2) == "6", "the open pile, which is the one number a reader can find"
    # And it says what it counts: a bare number beside a score chip is a number to guess at.
    assert "open review issue" in pill.group(1)
    # Nothing open, and two piles of work already done: the pill says 0 and the tab is
    # still on the strip. Weight keeps a tab alive; the badge says what is left to read.
    clean, _ = _build(tmp_path, {
        "title": "t", "summary": "<p>s</p>",
        "sections": [{"id": "s", "title": "S", "body": "<p>b</p>"}],
        "findings": [], "autofixes": [{"title": "a", "body": "b"}],
        "assumptions": [{"title": "s", "body": "b"}],
        "tabs": [{"id": "review", "label": "Review", "count": True,
                  "blocks": [{"type": "findings"}, {"type": "autofixes"},
                             {"type": "assumptions"}]},
                 {"id": "other", "label": "Other",
                  "blocks": [{"type": "section", "id": "s"}]}]})
    assert 'id="tabbtn-review"' in clean, "the tab is kept by its weight, not by its badge"
    assert re.search(r'id="tabbtn-review"[^>]*>Review<span class="n"[^>]*>0</span>', clean)


def test_an_excerpt_quoting_two_scenarios_goes_under_both(tmp_path):
    """`35-48,52-65` is ONE snippet in the content file, not two. Giving it to one pair
    leaves the other claiming its test is "not excerpted here", which is false."""
    assert build._line_spans("35-48,52-65") == [(35, 48), (52, 65)]
    assert build._line_spans("12") == [(12, 12)]
    assert build._line_spans("what") == []
    rel = "test/two.feature"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("x\n" * 70, encoding="utf-8")
    pumls = []
    for slug, line in (("one", 35), ("two", 52)):
        puml = f"{rel}.{slug}.genseq.puml"
        (tmp_path / puml).write_text(
            f"@startuml\n== [[src://{rel}:{line}{{t}} {slug}]] ==\nA -> B: x\n@enduml\n",
            encoding="utf-8")
        pumls.append(puml)
    quoted = build._share_excerpts(
        rel, [(p, None) for p in pumls], [{"ref": f"{rel}:35-48,52-65"}], set(), tmp_path)
    assert quoted[pumls[0]] and quoted[pumls[1]]
    # …but each side quotes ITS OWN range: the content file already split them.
    assert quoted[pumls[0]][0]["ref"] == f"{rel}:35-48"
    assert quoted[pumls[1]][0]["ref"] == f"{rel}:52-65"


def test_a_shared_excerpt_is_cut_so_each_pair_quotes_its_own_test(tmp_path):
    """`add-visit.spec.ts` holds two tagged Playwright tests, at 34 and at 51, and one
    excerpt in the content file quotes both. Handed whole to both pairs, the vet's
    diagram — the one this PR is about — sat beside the *other* scenario's code, both
    "Show Test" folds opening on `test('Add a visit to an existing pet…'` at 34. The
    generator's own title handle says which line each picture was drawn from, so the
    excerpt is cut at the next declaration and each pair gets its own test."""
    rel = "test/add-visit.spec.ts"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("x\n" * 70, encoding="utf-8")
    pumls = []
    for slug, line in (("to-an-existing-pet", 34), ("attended-by-a-vet", 51)):
        puml = f"{rel}.{slug}.genseq.puml"
        (tmp_path / puml).write_text(
            f"@startuml\ntitle [[src://{rel}:{line}{{t}} {slug}]]\nA -> B: x\n@enduml\n",
            encoding="utf-8")
        pumls.append(puml)
    entries = [(p, None) for p in pumls]

    # One range swallowing both declarations is cut at the second one.
    one = build._share_excerpts(rel, entries, [{"ref": f"{rel}:30-64"}], set(), tmp_path)
    assert one[pumls[0]][0]["ref"] == f"{rel}:30-50"
    assert one[pumls[1]][0]["ref"] == f"{rel}:51-64"

    # Two ranges, one per test: the comment on 49-50 that introduces the vet test is
    # inside the second range, so it travels with the vet test and not with the first.
    two = build._share_excerpts(rel, entries, [{"ref": f"{rel}:34-47,49-64"}], set(), tmp_path)
    assert two[pumls[0]][0]["ref"] == f"{rel}:34-47"
    assert two[pumls[1]][0]["ref"] == f"{rel}:49-64"

    # A range with no declaration in it is shared setup, and stays under both.
    both = build._share_excerpts(
        rel, entries, [{"ref": f"{rel}:1-10,34-47,49-64"}], set(), tmp_path)
    assert both[pumls[0]][0]["ref"] == f"{rel}:1-10,34-47"
    assert both[pumls[1]][0]["ref"] == f"{rel}:1-10,49-64"

    # The content file's own dict is never edited: the same object is handed to both.
    original = {"ref": f"{rel}:34-64", "caption": "the two tests"}
    cut = build._share_excerpts(rel, entries, [original], set(), tmp_path)
    assert original["ref"] == f"{rel}:34-64"
    assert cut[pumls[1]][0]["caption"] == "the two tests", "the caption rides along"


def test_an_excerpt_with_one_owner_is_passed_through_untouched(tmp_path):
    """A file with one picture per excerpt must go through the cutting byte for byte —
    the same dict object, not a copy with a rebuilt ref."""
    rel = "test/one.feature"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("x\n" * 40, encoding="utf-8")
    puml = f"{rel}.only.genseq.puml"
    (tmp_path / puml).write_text(
        f"@startuml\ntitle [[src://{rel}:10{{t}} only]]\nA -> B: x\n@enduml\n",
        encoding="utf-8")
    snippet = {"ref": f"{rel}:8-14"}
    quoted = build._share_excerpts(rel, [(puml, None)], [snippet], set(), tmp_path)
    assert quoted[puml][0] is snippet


def test_the_scenario_handle_is_read_from_the_title_and_from_a_divider(tmp_path):
    """A picture is one scenario, so the scenario names it: the handle moved from the
    `== divider ==` up into `title`. Both are read — a repository that has not been
    through the generator split still has the dividers, and so does any diagram drawn
    from several scenarios at once."""
    rel = "test/add-visit.feature"
    (tmp_path / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("x\n" * 30, encoding="utf-8")
    titled = rel + ".remembers.genseq.puml"
    (tmp_path / titled).write_text(
        "@startuml\n"
        f"title [[src://{rel}:4{{Click to open the test}} remembers the vet]]\n"
        f"footer @generate_sequence in {rel} — generated from real traces\n"
        "Browser -> Backend: [[src://src/main/java/Owner.java:14{tip} Owner.find]]\n"
        "@enduml\n", encoding="utf-8")
    assert build._scenarios_drawn(titled, rel, tmp_path) == [(4, "remembers the vet")]
    divided = rel + ".genseq.puml"
    (tmp_path / divided).write_text(
        "@startuml\n"
        f"title {rel}\n"
        f"== [[src://{rel}:4{{t}} remembers the vet]] ==\nA -> B: x\n"
        f"== [[src://{rel}:9{{t}} nobody attended]] ==\nA -> B: y\n"
        "@enduml\n", encoding="utf-8")
    assert build._scenarios_drawn(divided, rel, tmp_path) == [
        (4, "remembers the vet"), (9, "nobody attended")]


def test_the_pictures_tooltip_is_the_heading_not_the_markup_that_made_it():
    """PlantUML copies a title verbatim into the SVG's own <title>, which is what a
    browser shows on hover. With the heading now a creole link, that tooltip was the raw
    `[[src://…{…} Add a visit]]` — markup, over a heading already on screen."""
    svg = ('<svg><title>[[src://petclinic-test/src/add-visit.spec.ts:52'
           '{Click to open the test} Add a visit attended by a vet]]</title>'
           '<text>[[keep this]]</text></svg>')
    out = build._plain_svg_title(svg)
    assert "<title>Add a visit attended by a vet</title>" in out
    assert "<text>[[keep this]]</text>" in out, "only the <title> element is rewritten"
    # The colours a structural delta puts in its title are still stripped, as before.
    assert build._plain_svg_title("<svg><title>DB - <color:red>Diff</color></title></svg>") \
        == "<svg><title>DB - Diff</title></svg>"


def test_a_sequence_the_branch_deleted_gets_no_frame(tmp_path):
    """A test that loses its `@generate_sequence` leaves a deleted row in the manifest.
    There is no picture behind it, so a frame for it would be a heading with nothing under
    it — on a tab whose frames are now the list of tests."""
    rows = [{"kind": "sequence", "status": "deleted", "name": "gone.genseq",
             "source": "test/gone.feature.gone.genseq.puml"}]
    out, weight, changes = build.render_testpairs(
        {"title": ""}, {}, rows, tmp_path, tmp_path / ".human-review")
    assert out == "" and weight == 0 and changes == 0


# ── the three piles, read off the branch instead of out of the content file ─────────
# `{"auto": "review-points"}` is the point at which content.json stops being the
# judgement. What is checked here is the part a reader cannot check: that an absent
# record renders as an absence rather than as a clean review, which is the one
# substitution that would make the page confidently wrong.

POINTS_SPEC = {
    "findings": {"auto": "review-points"},
    "autofixes": {"auto": "review-points"},
    "assumptions": {"auto": "review-points"},
    "tabs": [{"id": "review", "label": "Review", "blocks": [
        {"type": "assumptions"}, {"type": "findings"}, {"type": "autofixes"}]}],
}

POINTS_DOC = {
    "mode": "points", "source": "review-points.md", "fixed_in": "HEAD",
    "sections": {"Fixed": "Fixed", "Ignored": "Ignored", "Assumptions": "Assumptions"},
    "autofixes": [{"title": "fixed one", "refs": ["a.py:1"]}],
    "findings": [{"title": "declined one", "why": "out of scope", "refs": ["b.py:2"],
                  "severity": "medium"}],
    "assumptions": [{"title": "assumed one", "alternative": "the other reading",
                     "refs": ["c.py:3"]}, {"title": "assumed two", "why": "because",
                                           "refs": ["c.py:9"]}],
    "warnings": [],
}


def _points_spec(doc, tmp_path):
    """A spec whose piles are delegated, and the parser's JSON beside it (or not)."""
    import copy
    spec = copy.deepcopy(POINTS_SPEC)
    if doc is not None:
        (tmp_path / "review-points.json").write_text(json.dumps(doc), encoding="utf-8")
    return spec, build.resolve_review_points(spec, tmp_path)


def test_the_piles_come_from_the_parsers_json(tmp_path):
    spec, points = _points_spec(POINTS_DOC, tmp_path)
    assert points["missing"] is False
    assert [f["title"] for f in spec["autofixes"]] == ["fixed one"]
    assert [f["title"] for f in spec["findings"]] == ["declined one"]
    assert len(spec["assumptions"]) == 2


def test_a_content_file_that_writes_its_own_piles_is_untouched(tmp_path):
    """The delegation is opt-in: an older content file keeps rendering as it always did,
    and must not be handed an empty pile because a file it never asked for is absent."""
    spec = {"findings": [{"title": "typed by hand", "body": "x"}], "tabs": []}
    assert build.resolve_review_points(spec, tmp_path) is None
    assert spec["findings"][0]["title"] == "typed by hand"


def test_an_absent_record_empties_the_piles_rather_than_keeping_stale_ones(tmp_path):
    """A page rendering last week's findings under this week's diff is worse than one
    rendering none, and it is what would happen if the delegation silently fell back to
    whatever the content file carried."""
    spec = {"findings": {"auto": "review-points"},
            "autofixes": [{"title": "left over from an older run"}],
            "assumptions": {"auto": "review-points"}, "tabs": []}
    points = build.resolve_review_points(spec, tmp_path)
    assert points["missing"] is True
    assert spec["findings"] == [] and spec["assumptions"] == []
    # Not delegated, so not touched: the author still owns what they typed.
    assert spec["autofixes"][0]["title"] == "left over from an older run"


def test_an_absent_record_never_reads_as_a_clean_review(tmp_path):
    """`render_findings([])` says *the automated passes came back clean*. That sentence is
    true of a review that found nothing and false — confidently, unfalsifiably — of a
    branch whose record is simply not there."""
    spec, points = _points_spec(None, tmp_path)
    build.reset_list()
    build.set_bands([build.POINTS_MISSING_BAND])
    out = "".join(build.render_pile_block(spec, b)[0]
                  for b in spec["tabs"][0]["blocks"])
    assert "the automated passes came back clean" not in out
    assert "Nothing was applied automatically" not in out
    assert "rband-none" in out
    assert "nothing records what was reviewed or declined" in out
    assert "Not recorded" in out


def test_an_absent_record_forces_the_assumptions_block_to_mode_c(tmp_path):
    """So the counts line and the pile agree. `0 assumptions` says the conversation *was*
    asked and named nothing, which is good news; mode C says nobody could be asked."""
    spec = {"assumptions": {"auto": "review-points"},
            "tabs": [{"id": "review", "label": "Review",
                      "blocks": [{"type": "assumptions", "mode": "A"}]}]}
    build.resolve_review_points(spec, tmp_path)
    assert build._assumptions_block(spec)["mode"] == "C"
    build.reset_list()
    assert "coder could not be asked" in build.opening_lede(spec)


def test_an_empty_pile_says_which_kind_of_empty_it_is(tmp_path):
    """A section that is not in the file and a section that is there and empty are two
    different facts about the review, and only the second one is news about the code."""
    doc = dict(POINTS_DOC, findings=[], autofixes=[],
               sections={"Fixed": "Fixed", "Assumptions": "Assumptions"})
    spec, points = _points_spec(doc, tmp_path)
    no_section = build.points_empty_html("findings", points)
    assert "has no <b>Ignored</b> section" in no_section
    was_empty = build.points_empty_html("autofixes", points)
    assert "records no fix" in was_empty


def test_the_counts_line_says_open_not_declined(tmp_path):
    """Read out of review-points.md, `findings` is what the agent read and said no to —
    a closed decision the reviewer is invited to disagree with, not an untriaged item —
    but the counts line now names it the same as the plain-content-file vocabulary does:
    `open`, because that is what the *reader's* job on it still is, whichever pile wrote
    it. Only the word `LLM` is the plain-content-file's own, so it is the one thing this
    vocabulary still leaves out."""
    spec, points = _points_spec(POINTS_DOC, tmp_path)
    build.reset_list()
    lede = build.opening_lede(spec)
    assert "1 open review issue" in lede and "1 auto-fixed" in lede
    assert "2 implementation assumptions" in lede
    assert "declined" not in lede and "open LLM review issue" not in lede
    # Open leads: it is the pile the reader still owes a decision to, in both
    # vocabularies, and the fixed pile is what is already done either way.
    assert lede.index("1 open review issue") < lede.index("1 auto-fixed")


def test_a_content_file_that_writes_its_own_piles_keeps_the_old_wording():
    spec = {"findings": [{"title": "a", "body": "x"}] * 6,
            "autofixes": [{"title": "b"}] * 4,
            "tabs": [{"id": "review", "label": "R", "blocks": [
                {"type": "findings"}, {"type": "autofixes"},
                {"type": "assumptions", "mode": "A"}]}],
            "assumptions": [{"title": "c", "body": "y"}] * 6}
    build.reset_list()
    lede = build.opening_lede(spec)
    assert "6 open LLM review issues" in lede
    assert "4 auto-fixed" in lede
    assert "6 implementation assumptions" in lede


def test_a_band_is_drained_not_repeated():
    """Three piles on one tab each ask for the lede; only the first gets it, and the band
    rides with it. A band printed once per pile would be the same alarm three times."""
    build.set_bands(["<div class='rband'>once</div>"])
    assert "once" in build._lede_above("<h2>a</h2>", "<p>lede</p>")
    assert "once" not in build._lede_above("<h2>b</h2>", "")


# ── an item has to say something past its title, wherever it says it ───────────────
# `body` used to be the only accepted place, which is where a model writing content.json
# put its prose. An item parsed out of review-points.md often carries its whole argument
# in `why:` (declined, with a reason) or `alternative:` (a reading not taken).
def test_a_declined_item_may_argue_in_why_instead_of_body():
    assert build.validate({"findings": [{"title": "a", "why": "out of scope"}]},
                          Path(".")) == []


def test_an_assumption_may_argue_in_alternative_alone():
    assert build.validate({"assumptions": [{"title": "a", "alternative": "the other"}]},
                          Path(".")) == []


def test_an_item_that_is_only_a_title_is_still_refused():
    problems = build.validate({"findings": [{"title": "a"}]}, Path("."))
    assert any("has none of" in p and "something past its title" in p for p in problems)


def test_the_piles_are_named_for_what_they_are_in_each_mode(tmp_path):
    """A content file's `findings` are untriaged and a branch's are declined; a pass's
    fixes were applied automatically and a branch's were chosen one at a time. The
    headings read the same in both vocabularies now — "Open review issues", "Auto-fixed"
    — so only each item's own badge still says which is which."""
    spec, points = _points_spec(POINTS_DOC, tmp_path)
    build.reset_list()
    build.set_bands([])
    out = "".join(build.render_pile_block(spec, b, heading=lambda b, i, t: f"<h2>{t}</h2>")[0]
                  for b in spec["tabs"][0]["blocks"])
    assert "<h2>Open review issues</h2>" in out
    assert "<h2>Auto-fixed</h2>" in out
    assert ">fixed<" in out and ">auto-fixed<" not in out
    # A content file that writes its own piles keeps both words.
    old = {"findings": [{"title": "a", "body": "x"}], "autofixes": [{"title": "b"}],
           "tabs": [{"id": "review", "label": "R", "blocks": [
               {"type": "findings"}, {"type": "autofixes"}]}]}
    build.resolve_review_points(old, tmp_path)
    build.reset_list()
    out = "".join(build.render_pile_block(old, b, heading=lambda b, i, t: f"<h2>{t}</h2>")[0]
                  for b in old["tabs"][0]["blocks"])
    assert "<h2>Requires human review</h2>" in out and "<h2>Auto-fixed</h2>" in out
    assert ">auto-fixed<" in out


def test_the_scope_chip_says_the_same_thing_as_the_counts_line(tmp_path):
    """Two numbers over one review, in two places on the same screen. `3 fixed · 6
    declined` beside `6 open, 3 auto-fixed` used to ask the reader which of them to
    believe; both now read `pile_numbers`, so a mismatch cannot recur."""
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert '"value": scope_chip_value(spec)' in src
    spec = {"findings": [{"title": f"f{i}"} for i in range(6)],
            "autofixes": [{"title": f"a{i}"} for i in range(3)],
            "assumptions": [{"title": f"s{i}"} for i in range(7)]}
    # PR #49's own numbers — the boundary case `SCOPE_CHIP_MAX_LEN` was picked against:
    # three numbers do not fit, so the chip stays at the two the reader can act on.
    assert build.scope_chip_value(spec) == \
        '6 open, <span class="sub">3 auto-fixed</span>'
    small = {"findings": [{"title": "f"}], "autofixes": [{"title": "a"}],
             "assumptions": [{"title": "s"}]}
    assert build.scope_chip_value(small) == \
        '1 open, <span class="sub">1 auto-fixed, 1 assumption</span>'
    build.reset_list()
    lede = build.opening_lede(dict(spec, tabs=[{"id": "review", "label": "R", "blocks": [
        {"type": "findings"}, {"type": "autofixes"}, {"type": "assumptions", "mode": "A"}]}]))
    assert "6 open LLM review issues" in lede and "3 auto-fixed" in lede
    assert lede.index("open") < lede.index("auto-fixed")


def test_the_counts_line_is_printed_once_even_when_every_pile_is_empty(tmp_path):
    """The offset used to answer "am I the top of the list?" on its own — it is zero
    exactly until the first pile renders an `<ol>`. A pile with no items renders no list,
    so with all three empty (a branch carrying no record, which is the common case for a
    branch nobody ran the flow on) the line appeared three times down one short tab."""
    spec, _ = _points_spec(None, tmp_path)
    build.reset_list()
    build.set_bands([])
    out = "".join(build.render_pile_block(spec, b)[0]
                  for b in spec["tabs"][0]["blocks"])
    # Not a bare `.count("pilelede")`: the scroll-spy script that rides along with the
    # line also names the class, as a selector, so that substring alone appears twice
    # even when the line itself renders once.
    assert out.count('<p class="sub counts pilelede">') == 1
    assert out.count("<script>(function(){") == 1, "the spy script rides along once, not once per pile"


def test_the_review_chip_drops_itself_when_nothing_records_a_review(tmp_path):
    """`🤖 LLM review: 0 open, 0 auto-fixed` is the whole failure this flow exists to end,
    in eleven characters: two measured-looking zeros asserting a review that found nothing.
    Every other computed chip drops itself rather than print a number it cannot stand
    behind."""
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    i = src.index('if c.get("auto") == "autofixed":')
    head = src[i:i + 900]
    assert '(spec.get("_reviewPoints") or {}).get("missing")' in head
    assert "continue" in head


# --------------------------------------------------------------------------- #
# the Code City shot
# --------------------------------------------------------------------------- #

def _city(tmp_path, **over):
    """A page whose only tab is the Code City shot, with the two files it names on disk."""
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "codecity.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    city = {"png": "assets/codecity.png", "href": "assets/codecity/codecity.html", **over}
    return {**BARE, "codecity": city,
            "tabs": [*BARE["tabs"],
                     {"id": "city", "label": "Code City",
                      "blocks": [{"type": "codecity"}]}]}


def test_the_shot_is_headed_by_what_it_is_for(tmp_path):
    """Not by what it is. The tab pill says *Code City*, the name of the visualisation; the
    heading says what the reader is being shown it to answer. The trailing ellipsis is
    deliberate: the three named axes are the ones the panel inside the shot switches
    between, and they are not all of them."""
    page, _ = _build(tmp_path, _city(tmp_path))
    assert ('<h2 id="codecity">Code impact of this PR: size, complexity, coupling, …</h2>'
            in page)
    assert build.CITY_HEADING.endswith("…")
    # The anchor is on the heading, so `#codecity` still lands at the top of the picture.
    assert 'class="city" href=' in page and 'class="city" id=' not in page


def test_the_shot_carries_no_lede(tmp_path):
    """It held *"10 buildings lit — the classes this change set touched, in a city of the
    whole backend"*: three claims a reader can see, one of them a hand-typed number in a
    file nothing revalidates, which went stale the first time somebody added a class."""
    page, err = _build(tmp_path, _city(tmp_path, body="<p>10 buildings lit.</p>"))
    assert "10 buildings lit" not in page
    # Named rather than silently dropped, so whoever wrote it learns it is not wanted.
    assert "codecity.body is no longer rendered" in err


def test_a_page_that_means_something_else_by_the_picture_can_say_so(tmp_path):
    page, _ = _build(tmp_path, _city(tmp_path, title="Where the weight moved"))
    assert '<h2 id="codecity">Where the weight moved</h2>' in page
    assert build.CITY_HEADING not in page


def test_the_city_tab_is_never_struck_through(tmp_path):
    """The strike means "we looked and this branch did not touch it", which a `puml` card
    can earn honestly — a context diagram is often the same picture at both ends of a
    branch. This shot cannot: the lit buildings *are* the classes the change set touched,
    so the tab was being struck over a picture whose whole subject is the change."""
    page, err = _build(tmp_path, _city(tmp_path))
    at = page.index('id="tabbtn-city"')
    tag = page[page.rindex("<button", 0, at):page.index(">", at)]
    assert "quiet" not in tag
    assert "Code City" not in err.split("kept as context")[-1].split("\n")[0]


# --------------------------------------------------------------------------- #
# the cost ledger, cached on its inputs
# --------------------------------------------------------------------------- #

def test_the_ledger_is_recomputed_when_anything_it_reads_has_moved(tmp_path):
    """The key is the inputs and never a timestamp: a cache that could be *wrong* would be
    much worse than a slow build, because the cost tab is the one part of this page nothing
    else corroborates."""
    out = tmp_path / ".human-review"
    (out / "assets").mkdir(parents=True)
    (out / ".steps.json").write_text("{}", encoding="utf-8")
    (out / ".session").write_text("abc-123", encoding="utf-8")
    key = lambda tabs=("a", "b"), base="origin/main": build._cost_inputs(
        tmp_path, out, list(tabs), base)

    same = key()
    assert key() == same, "nothing moved, so the answer cannot have"
    assert key(base="origin/release") != same, "a different base is a different question"
    assert key(tabs=("a",)) != same, "a different tab list is a different answer"
    import time
    time.sleep(0.01)
    (out / ".steps.json").write_text('{"x": 1}', encoding="utf-8")
    assert key() != same, "the step ledger is how the bill is split across tabs"


def test_the_ledger_is_read_back_instead_of_recomputed(tmp_path):
    """This one call was forty seconds of a forty-seven-second build — every turn of a
    conversation that wrote a feature over two days, read again for a page whose bill had
    not moved. Most of what a reader waited through after pressing a button on the page."""
    out = tmp_path / ".human-review"
    out.mkdir()
    cache = out / build.COST_CACHE
    cache.write_text(json.dumps({
        "key": build._cost_inputs(tmp_path, out, ["one"], "origin/main"),
        "ledger": {"tabs": {"one": {"cost": 1.5}}}}), encoding="utf-8")
    got = build.cost_ledger_report(tmp_path, ["one"], "origin/main", out)
    assert got == {"tabs": {"one": {"cost": 1.5}}}
    # Dot-prefixed: `publish-demo.sh` publishes what does not start with a dot, and a
    # measurement of one machine's transcripts is not something to ship in a demo zip.
    assert build.COST_CACHE.startswith(".")


def _frozen_session(monkeypatch, tmp_path):
    """A session whose transcript does not move while the test runs.

    `_cost_inputs` fingerprints the session's `.jsonl` by size and mtime — which is the
    whole point of it — and the suite is normally run *inside* a Claude Code session, so
    the live transcript grows by a turn somewhere between the call that writes the cache
    and the call that checks the key it was written under. The test then failed on the
    feature working exactly as designed. A synthetic `$HOME` with one empty transcript in
    it answers the same question deterministically: `review-cost.py` reads it (both
    `PROJECTS` there and `_cost_inputs` here resolve `~` at call time), finds nothing, and
    nothing about the answer can shift underneath the assertion.
    """
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-synthetic").mkdir(parents=True)
    (home / ".claude" / "projects" / "-synthetic" / "frozen-session.jsonl").write_text(
        "", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "frozen-session")


def test_a_cache_written_for_another_question_is_ignored(tmp_path, monkeypatch):
    _frozen_session(monkeypatch, tmp_path)
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / build.COST_CACHE).write_text(
        json.dumps({"key": "not-this-one", "ledger": {"tabs": {}}}), encoding="utf-8")
    # It falls through to the program, whatever the program then answers — the point is
    # that it did not serve the stale one back.
    got = build.cost_ledger_report(tmp_path, ["one"], "origin/main", out)
    assert got != {"tabs": {}}
    # And the cache it leaves behind is keyed to the question that was actually asked.
    held = json.loads((out / build.COST_CACHE).read_text())
    assert held["key"] == build._cost_inputs(tmp_path, out, ["one"], "origin/main")


def test_an_unreadable_cache_is_a_slow_build_and_not_a_failed_one(tmp_path):
    """A half-written file — a build killed mid-`write_text` — must cost the next one a
    recomputation and nothing else."""
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / build.COST_CACHE).write_text("{not json", encoding="utf-8")
    build.cost_ledger_report(tmp_path, ["one"], "origin/main", out)
    json.loads((out / build.COST_CACHE).read_text())  # and it is valid JSON again


def test_the_diagram_rebuild_can_never_buy_a_privacy_verdict(tmp_path):
    """A reader pressing *Update the report* under a picture is asking for the picture to be
    picked up. It is also most of why the command is quick."""
    page, _ = _build(tmp_path, BARE)
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    body = src[src.index("rebuild_cmd = "):]
    assert '"--no-model"' in body[:body.index("\n\n")]
