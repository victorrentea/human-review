#!/usr/bin/env python3
"""What the page builder promises, checked without building the whole page.

Rendering a real guide needs a repository, a manifest, PlantUML, a recorded video and a
Code City — none of which belong in a unit test. Every emitter added since the tab layout
is a pure function of small inputs, so it is tested as one. `testpairs` (which shells out
to `ast-grep`) is exercised through its pure parts: the chapter parser. `logging` shells
out too (`git`, `ast-grep`, and `extract-snippet.py`'s own Pygments pass), cheap and real
enough to run directly against a checked-in Java fixture.

Run with:  python3 -m pytest test_build_review.py
"""
from __future__ import annotations

import html
import importlib.util
import html as html_mod
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
    assert ">Running app<" in out
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
    for verb in ("start", "stop"):
        assert f'<span class="appenv-act appenv-{verb}" hidden>' in out
    # Seed is the exception: it is on screen from the start, greyed, with the reason as
    # its tip — the DB Fixture row is always there (8 Oct 2026), and only the script arms it.
    at = out.index('class="appenv-reset"')
    tag = out[out.rindex("<button", 0, at):out.index(">", at) + 1]
    assert " hidden" not in tag and 'aria-disabled="true"' in tag
    assert 'data-tip="Start the app first"' in tag


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
    # Nothing hides a verb off disk any more — not even the fixtures' Seed, which stays
    # on screen greyed until something answers (8 Oct 2026).
    for verb in ("start", "stop", "resets", "fixtures"):
        assert f".appenv:not(.appenv-served) .appenv-{verb}" not in build.CSS
    # The second row is gone, name and all.
    for dead in ("appenv-manual", "appenv-cmd", "appenv-verb"):
        assert dead not in out and f".{dead}" not in build.CSS


def test_the_verbs_are_one_row_of_one_control_each(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where"}
    out = build.video_html(s, tmp_path)
    for verb, word in (("start", "Start App in Docker"), ("stop", "Stop")):
        assert f'<span class="appenv-act appenv-{verb}"' in out
        assert f'<span class="cmd-word">{word}</span>' in out
    # `urlCommand` is declared for the row to run on its own, and is not a button: as
    # "Where ↗" nobody could tell what it would do.
    assert "appenv-where" not in out and '<span class="cmd-word">Where</span>' not in out
    assert build.ACTIONS["demo-env-url"]["command"] == "where"
    # Each is the page's one command renderer with a word on it — not a fourth kind of
    # button with its own clipboard and its own idea of what a glyph means.
    assert out.count('class="copycmd cmd-copy has-word"') == 2
    assert out.count('class="runhere cmd-run has-word" hidden') == 2
    # One row. The verbs are inside it, after the state they act on.
    assert out.count('class="appenv-run"') == 1
    for verb in ("start", "stop"):
        assert out.index("appenv-state") < out.index(f"appenv-{verb}")


def test_neither_verb_wears_the_rerun_mark(tmp_path):
    """`↻` means *this one comes round again* everywhere else on the page, which is the
    opposite of what these do: Start begins something that then keeps running, Stop ends
    it. The old second row wore it on every verb."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "urlCommand": "where"}
    out = build.video_html(s, tmp_path)
    assert build.CMD_RUN not in out
    for glyph in (build.CMD_PLAY, build.CMD_STOP):
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
    # One Seed per fixture in the DB Fixture row; with no project to read, the seed alone.
    assert 'data-reset="/__reset"' in out and ">Seed</button>" in out
    assert '<span class="appenv-fx-name">Default</span>' in out


def test_the_fixture_row_lists_every_fixture_the_build_found_under_the_running_app_row():
    """Victor, 8 Oct 2026: the fixtures are the build's, not the running app's, and they
    sit in a row of their own under "Running app", whether anything is up or not. Each is
    its dot, its name and a Seed; the 👁 is dataset-view.js's to add."""
    rt = {"command": "up", "stop": "down", "reset": "/__reset"}
    out = build.runtime_html(rt, fixtures=[("", "#8b929c"), ("green", "#2fa84f"),
                                           ("busy-day", "#3b82f6")])
    run_end = out.index("</div>", out.index('<div class="appenv-run">'))
    row = out.index('<div class="appenv-fixtures"')
    assert run_end < row, "a row of its own, after the Running app one"
    assert "appenv-reset" not in out[:run_end], "nothing of the fixtures in the app row"
    assert ">DB Fixture:<" in out
    names = [out[i + len('<span class="appenv-fx-name">'):out.index("<", i + 1)]
             for i in [j for j in range(len(out))
                       if out.startswith('<span class="appenv-fx-name">', j)]]
    assert names == ["Default", "green", "busy-day"]
    assert out.count(">Seed</button>") == 3
    assert 'style="--fx:#2fa84f"' in out and 'style="--fx:#8b929c"' in out
    assert 'data-fixture="green"' in out
    # Without a reset endpoint the row still lists the data, with nothing to press.
    bare = build.runtime_html({"command": "up"}, fixtures=[("", "#8b929c"), ("green", "#0f0")])
    assert ">green<" in bare and "appenv-reset" not in bare
    # And the seed alone with nothing to do to it is no row at all.
    assert "appenv-fixtures" not in build.runtime_html({"command": "up"})


def test_demo_fixtures_are_read_off_the_sql_files_not_the_colour_map(tmp_path):
    demo_fixtures = build.demo_fixtures
    d = tmp_path / "db" / "fixtures"
    d.mkdir(parents=True)
    (d / "green.sql").write_text("select 1;")
    (d / "blue.sql").write_text("select 1;")
    (d / "fixture-colors.json").write_text('{"green": "#2fa84f", "gone": "#123456"}')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    got = demo_fixtures(tmp_path)
    assert [n for n, _ in got] == ["", "blue", "green"]
    assert dict(got)["green"] == "#2fa84f"
    assert demo_fixtures(None) == [("", "#8b929c")]


def test_a_recorded_video_gets_a_player(tmp_path):
    out = build.video_html(_video_dir(tmp_path, filmed=True), tmp_path)
    assert '<video controls preload="metadata" src="assets/f.webm">' in out
    assert out.count("<li ") == 3


def test_each_voice_the_recorder_cut_is_a_radio_button_under_the_player(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    for key in ("trump", "discovery"):
        (tmp_path / "assets" / f"f.voice-{key}.webm").write_bytes(b"\x1aE\xdf\xa3")
    (tmp_path / "assets" / "f.voices.json").write_text(json.dumps([
        {"key": "trump", "label": "🐘", "video": "f.voice-trump.webm"},
        {"key": "discovery", "label": "Discovery", "video": "f.voice-discovery.webm"},
        {"key": "ghost", "label": "Ghost", "video": "f.voice-ghost.webm"}]), encoding="utf-8")
    out = build.video_html(s, tmp_path)
    radios = re.findall(r'<input type="radio"[^>]*data-src="([^"]+)"[^>]*> (.+?)</label>', out)
    # Emoji faces only, in the film's title row (Victor, 7 Oct 2026): 👩 standard, the 🐘,
    # 🌍 Discovery — each named for a screen reader, the 🐘 never by whose voice it is.
    assert radios == [("assets/f.webm", '<span class="vs-emoji">👩</span>'),
                      ("assets/f.voice-trump.webm", '<span class="vs-emoji">🐘</span>'),
                      ("assets/f.voice-discovery.webm", '<span class="vs-emoji">🌍</span>')], \
        "a voice whose film is not on disk is never offered"
    assert "guess who" not in out and ">standard<" not in out and "> Discovery<" not in out
    assert 'value="" data-src="assets/f.webm" aria-label="Standard voice"' in out
    assert '<label data-tip="Standard voice"><input' in out
    assert 'value="discovery" data-src="assets/f.voice-discovery.webm" aria-label="Discovery"' in out
    head = out[out.index('<div class="vidhead">'):out.index('<ol class="transcript"')]
    assert head.index("Intro video</h2>") < head.index('class="voice-switch"'), \
        "the voices follow the title (and the presses placed at its end)"
    assert out.index("<video") < out.index('class="voice-switch"') < out.index("transcript")
    assert re.search(r'value=""[^>]* checked>', out), "the standard voice is the one playing"
    # The 🐘 face is Victor's design and stays; a bare emoji gets a spoken name, but never
    # whose voice it is — not to a screen reader, not on hover: guessing is the point.
    assert 'value="trump" data-src="assets/f.voice-trump.webm" aria-label="cloned voice"' in out
    assert re.search(r'<label><input[^>]*value="trump"', out), "no hover on the 🐘"
    assert "Trump" not in out
    assert out.count("aria-label=\"cloned voice") == 1, "Discovery and standard are named by their word"
    # Discovery names its narrator on hover — from VOICE_TIPS here, since this voices.json
    # predates the `tip` field; a `tip` in the file wins.
    assert '<label data-tip="David Attenborough"><input type="radio"' in out
    assert out.count("data-tip=") == 2, "Discovery's narrator and the standard voice's name"


def test_the_film_title_heads_the_transcript_column_not_a_row_above_the_player(tmp_path):
    """Victor, 7 Oct 2026: a row across the page for two words wasted a screenful's worth
    of height; the title (and the tab's presses after it) head the cue list instead, and
    the column is held to the player's height so the cues scroll inside it."""
    s = _video_dir(tmp_path, filmed=True)
    out = build.video_html(s, tmp_path)
    side = out[out.index('<div class="vidside">'):]
    assert side.index('<h2 class="tabtitle">Intro video</h2>') < side.index('<ol class="transcript"')
    assert out.index("<video") < out.index('<div class="vidside">'), "the player first, then the column"
    assert ".vidwrap > .vidside { display:flex;" in build.CSS and "contain:size" in build.CSS
    placed = build.place_tab_reruns("behaviour", "Demo", out, '<span class="tabre"></span>')
    assert 'Intro video<span class="tabre"></span></h2>' in placed


def test_with_a_deployed_app_row_the_voices_still_sit_in_the_film_title_row(tmp_path):
    """They rode the Running app band's right end for a while; which voice narrates is a
    fact about the film, not the app, so they moved after the film's title (Victor, 7 Oct
    2026) — the band keeps only the app's own controls."""
    s = _video_dir(tmp_path, filmed=True)
    s["runtime"] = {"command": "up", "stop": "down", "reset": "/__reset"}
    (tmp_path / "assets" / "f.voice-discovery.webm").write_bytes(b"\x1aE\xdf\xa3")
    (tmp_path / "assets" / "f.voices.json").write_text(json.dumps([
        {"key": "discovery", "label": "Discovery", "video": "f.voice-discovery.webm"}]),
        encoding="utf-8")
    out = build.video_html(s, tmp_path)
    assert out.count('class="voice-switch"') == 1
    assert out.index("appenv-reset") < out.index("vidwrap") < out.index('class="voice-switch"')
    assert out.index('<div class="vidhead">') < out.index('class="voice-switch"') \
        < out.index('<ol class="transcript"')
    assert "vidcol" not in out, "nothing left under the player"
    rule = build.CSS[build.CSS.index(".vidhead > .voice-switch"):]
    assert "gap:3px" in rule[:rule.index("}")], "the pills 3px apart"
    head = build.CSS[build.CSS.index(".vidside > .vidhead"):]
    assert "column-gap:.6rem" in head[:head.index("}")], ".6rem after the presses"
    # caption.js finds the group by the film it switches, not as a child of the wrap.
    assert "wrap.querySelectorAll('.voice-switch" not in build.CAPTION_JS


def test_a_voice_declares_its_own_hover_name(tmp_path):
    s = _video_dir(tmp_path, filmed=True)
    (tmp_path / "assets" / "f.voice-discovery.webm").write_bytes(b"\x1aE\xdf\xa3")
    (tmp_path / "assets" / "f.voices.json").write_text(json.dumps([
        {"key": "discovery", "label": "Discovery", "tip": "Morgan Freeman",
         "video": "f.voice-discovery.webm"}]), encoding="utf-8")
    assert '<label data-tip="Morgan Freeman">' in build.video_html(s, tmp_path)


def test_a_film_with_no_cloned_voice_has_no_voice_switch(tmp_path):
    assert "voice-switch" not in build.video_html(_video_dir(tmp_path, filmed=True), tmp_path)


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


def test_an_embed_is_pasted_into_a_shadow_root_not_framed(tmp_path):
    """5 Oct 2026: one scrollbar, the page's. The report's styles and markup go into a
    template a shadow root is filled from, its inline script is told its host, its CDN
    script is deferred so it holds up nothing after it, and the fragment rides as data."""
    (tmp_path / "r.html").write_text(
        '<!doctype html><html><head><title>t</title><link rel="stylesheet" href="x.css">'
        '<style>body{color:red}</style></head><body><div id="dv-app">hi</div>'
        '<script src="https://cdn/x.js"></script><script>go()</script></body></html>',
        encoding="utf-8")
    out = build.embed_html({"id": "api1", "embed": {"src": "r.html#only-touched",
                                                    "class": "oavhost", "label": "r"}},
                           tmp_path)
    assert "<iframe" not in out
    assert '<div class="oavhost" id="embed-api1" role="region" data-hash="#only-touched"' in out
    tpl = out[out.index('<template id="embed-api1-tpl">'):out.index("</template>")]
    assert '<link rel="stylesheet" href="x.css">' in tpl and "body{color:red}" in tpl
    assert '<div id="dv-app">hi</div>' in tpl and "<script" not in tpl and "<title>" not in tpl
    assert "attachShadow({mode:'open'})" in out
    assert '<script src="https://cdn/x.js" defer></script>' in out
    assert '<script data-dv-host="embed-api1">go()</script>' in out
    assert out.index("attachShadow") < out.index("go()"), "the root exists before its script"


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
    assert ">VetRestController.java</a>" in out, "the name, no line number on the face"
    assert f'data-tip="Open in VS Code: {rel}"' in out
    assert f">{rel}:1-2<" not in out, "the ceremony is on hover, not in the face"


def test_a_snippet_of_a_file_at_the_repo_root_says_only_that_it_opens(tmp_path):
    """A tooltip repeating the name is a tooltip saying nothing."""
    (tmp_path / "README.md").write_text("one\ntwo\n", encoding="utf-8")
    out = _extract_snippet().render("README.md:1-2", None, tmp_path, exact=True)
    assert '>README.md</a>' in out
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
# the listing: one snippet per statement, each logged value wearing its declared type
# --------------------------------------------------------------------------- #

INFO_HIT = {"file": FIXTURE_REL, "abs_file": str(REPO_ROOT / FIXTURE_REL), "line": 8,
            "column": 9, "end_line": 8, "level": "INFO", "method": "info",
            "format": '"Booking visit for owner {} pet {}"',
            "text": 'LOG.info("Booking visit for owner {} pet {}", owner, petId)',
            "args": ["owner", "petId"], "arg_types": ["String", "int"],
            "origins": [{"line": 7, "name": "owner", "kind": "param", "text": ""}]}


def test_each_statement_renders_as_the_page_s_one_snippet_style():
    """No invented second code-block style — the same `.snippet` figure every other quoted
    line on the page uses, headed by the source bar every quoted block wears."""
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    assert out.count('<figure class="snippet">') == 1
    assert out.count('<div class="srcbar">') == 1
    assert '>Slf4jExplicit.java</a>' in out
    assert "Booking visit for owner" in out
    assert "<figcaption" not in out and "<table" not in out


def test_a_rewritten_statement_is_not_shown_as_new_logging():
    """Run 5: three `log.warn` lines that only swapped a literal for a constant read as new
    logging. The extractor now says which hits it paired with a deleted call; the tab lists
    the new ones first and says what each rewritten one was."""
    rewritten = {**INFO_HIT, "change": "modified", "same_output": True,
                 "was": 'LOG.info("Booking {}", owner)'}
    fresh = {**INFO_HIT, "change": "added"}
    out = build._logging_listing([rewritten, fresh], REPO_ROOT)
    # The new one is listed first, and carries no caption: its `+` gutter says it.
    assert "New log statement" not in out
    cards = out.split('<figure class="snippet">')[1:]
    assert "lg-change" not in cards[0] and "Rewritten, not new" in cards[1]
    assert "it logs the same text as before" in out
    assert "Was <code>LOG.info(&quot;Booking {}&quot;, owner)</code>" in out
    # Without a base there is no claim either way — no label at all.
    assert "lg-change" not in build._logging_listing([INFO_HIT], REPO_ROOT)
    # Eval run 6: the label floated between two cards. It is each card's own caption now.
    assert cards[1].lstrip().startswith('<figcaption class="snippet-note"><span class="lg-change')


def test_a_rewritten_statement_wears_a_neutral_mark_not_the_added_plus():
    """"Rewritten, not new" over a green `+` contradicted itself (eval run 6). The
    statement's own lines get `~`; a line pulled in beside it that is really new keeps `+`."""
    def row(n):
        return (f'<span class="ln-row added"><span class="dm">+</span>'
                f'<span class="ln">{n}</span>x</span>')
    snippet = "\n".join(row(n) for n in (40, 56, 57))
    h = {"change": "modified", "line": 56, "end_line": 57}
    out = build._mark_rewritten(snippet, h)
    assert out.count('<span class="ln-row added rewritten"><span class="dm">~</span>') == 2
    assert row(40) in out, "the new constant it now logs is still a new line"
    assert build._mark_rewritten(snippet, {**h, "change": "added"}) == snippet


def test_the_declaration_of_a_logged_value_is_quoted_with_the_statement():
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    assert '<span class="ln">7</span>' in out and '<span class="ln">8</span>' in out


def test_every_resolved_value_wears_an_intellij_style_type_hint_in_front_of_it():
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    row = next(r for r in out.split("\n") if '<span class="ln">8</span>' in r)
    assert row.count('class="typehint"') == 2
    # In argument order, each right in front of its own value — not inside the message.
    s = row.index('<span class="typehint" data-type="String"')
    i = row.index('<span class="typehint" data-type="int"')
    assert s < i
    assert build.code_xref.plain(row[s:]).lstrip().startswith("owner")
    assert build.code_xref.plain(row[i:]).lstrip().startswith("petId")
    assert build.code_xref.plain(row[:s]).rstrip().endswith('pet {}",')


def test_the_hint_is_not_part_of_the_line_s_text():
    """Drawn by CSS from `data-type`: copying the line copies the Java, and `code_xref`,
    which reads every quoted line back as plain text, never sees a type name there."""
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    row = next(r for r in out.split("\n") if '<span class="ln">8</span>' in r)
    assert "String" not in build.code_xref.plain(row)


def test_an_unresolved_type_gets_no_hint_rather_than_a_guess():
    out = build._logging_listing([{**INFO_HIT, "arg_types": [None, "int"]}], REPO_ROOT)
    assert out.count('class="typehint"') == 1 and 'data-type="int"' in out


def test_a_name_that_also_appears_in_the_message_is_not_mistaken_for_the_argument():
    """`owner` is a word of the format string too; the hint goes on the argument."""
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    row = next(r for r in out.split("\n") if '<span class="ln">8</span>' in r)
    before = build.code_xref.plain(row[:row.index('<span class="typehint" data-type="String"')])
    assert "Booking visit for owner {} pet {}" in before


def test_the_hint_is_placed_before_the_token_not_inside_its_colour():
    assert build._insert_at('<span class="n">ab</span>', {0: "X"}) == 'X<span class="n">ab</span>'
    assert build._insert_at('a<span class="n">b</span>', {1: "X"}) == 'aX<span class="n">b</span>'
    assert build._insert_at('a&amp;b', {2: "X"}) == 'a&amp;Xb'


def test_nothing_on_the_tab_is_a_model_s_reading():
    out = build._logging_listing([INFO_HIT], REPO_ROOT)
    for gone in ("privacy", "SAFE", "DOUBT", "AI Evaluation", "🤖", "log-footer", "log-values"):
        assert gone not in out
    assert not hasattr(logging_tab, "OFFLINE")
    assert not hasattr(logging_tab, "privacy_verdict")


def test_two_statements_yield_two_boxes_and_nothing_else():
    other = {**INFO_HIT, "line": 11, "end_line": 11, "method": "trace",
             "format": '"payload={}"', "args": ["owner"], "arg_types": ["String"],
             "origins": []}
    out = build._logging_listing([INFO_HIT, other], REPO_ROOT)
    assert out.count('<figure class="snippet">') == 2
    assert out.rstrip().endswith("</figure>")

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
    test cannot see."""
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
    assert 'data-type="int"' in frag  # the logged value's declared type, as a hint


def test_the_logging_tab_opens_on_one_computed_heading(tmp_path, monkeypatch):
    """Two things the content file used to write and no longer can: a `<h2>` repeating the
    tab's own label, and three sentences of methodology under it. What is left is one
    heading naming what the scan looked for — an `<h2>` like Code City's, not a boxed lede —
    with the package list on hover, read out of `logextract.py`'s own rule, so a library
    added there turns up here with nobody remembering the page."""
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    src.write_text(FOO_WITH_WARN, encoding="utf-8")
    frag, _, _ = build.logging_fragment(
        {"paths": ["."], "base": "base", "id": "logging-added",
         "title": "Logging added/updated", "body": "<p>Found structurally with ast-grep…</p>"},
        repo)
    # The one heading is the computed line itself, formatted as Code City's is — never
    # the authored "Logging added/updated", which is the tab's label said twice.
    assert frag.count("<h2") == 1 and '<h2 class="tabtitle" id="logging-added">Uses of <span' in frag
    # Under it, how the uses were found, and no underline between the two.
    assert ('</h2><p class="tabsub">Found by syntax-aware search over Java sources</p>') in frag
    assert "Logging added/updated" not in frag and "not by grepping" not in frag, \
        "title and body on the logging block are the renderer's now, not the author's"
    assert 'id="logging-added"' in frag, "the deep link the heading carried still lands"
    assert "Uses of <span" in frag
    assert ">common Java logging libraries</span></h2>" in frag
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


def test_an_inlay_type_hint_paints_where_it_sits_not_over_the_code(tmp_path):
    """`.ln-row` hangs its wrapped tail with `text-indent:-5.5em`, and `text-indent` is
    inherited: the inline-block `.typehint` applied it again to its own `::before`, which
    landed ~72px left, on top of `log.warn` — eval run 10 read 'Ştoingarn(' on every card,
    in both themes. The hint's text must start inside the hint's own box."""
    assert "pre.code .ln-row * { text-indent:0; }" in build.CSS
    sync = pytest.importorskip("playwright.sync_api")
    row = (f'<pre class="code"><code><span class="ln-row added"><span class="dm">+</span>'
           f'<span class="ln">93</span>        log.warn(VALIDATION_FAILED_LOG, '
           f'{build.type_hint_html("List<String>")}errors);</span>\n</code></pre>')
    page = tmp_path / "hint.html"
    page.write_text(f'<!doctype html><meta charset="utf-8"><style>{build.CSS}</style>'
                    f'<body><div class="wrap">{row}</div></body>', encoding="utf-8")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        try:
            for scheme in ("light", "dark"):
                pg = browser.new_page(color_scheme=scheme)
                pg.goto(page.as_uri())
                # The hint's box is placed right either way; only its painted text moves,
                # and what moves it is this one computed value (−71.75px without the reset).
                indent, row = pg.evaluate("""() => [
                    getComputedStyle(document.querySelector('.typehint')).textIndent,
                    getComputedStyle(document.querySelector('.ln-row')).textIndent]""")
                assert indent == "0px", f"{scheme}: the hint inherits the row's hang ({indent})"
                assert row != "0px", "the row itself still hangs its wrapped tail"
                pg.close()
        finally:
            browser.close()


def test_a_logging_box_does_not_badge_what_its_own_gutter_already_marks(tmp_path, monkeypatch):
    """`new code` on every box restates the tab's entry condition: a block is here because
    the branch added or rewrote that logging line, and the `+` in the gutter marks exactly
    which lines. The badge stays everywhere else, where the reader did not choose the
    snippet and "is this new?" is a real question."""
    repo, src = _tiny_java_repo(tmp_path, FOO_BASE)
    src.write_text(FOO_WITH_WARN, encoding="utf-8")
    frag, _, _ = build.logging_fragment({"paths": ["."], "base": "base"}, repo)
    assert "filemark" not in frag
    assert "new code" not in frag
    assert '<figure class="snippet">' in frag  # and the snippet itself is untouched


def test_logging_fragment_keeps_its_weight_on_a_genuine_zero_too(tmp_path, monkeypatch):
    """Same guarantee on the other real path through the pipeline: a change set that adds
    no logging statement still has to render with weight 1 — the "None." sentence, not a
    dropped tab — once the header/card it used to lean on for that no longer exists."""
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


def test_a_breaking_contract_turns_the_api_pill_red(tmp_path):
    """Run 5: the API band said *Breaking change* in red, and the pill above it was the
    only one in the strip that did not say so — Sequence was amber, CODEOWNERS red."""
    def page_with(band):
        (tmp_path / "assets").mkdir(exist_ok=True)
        (tmp_path / "assets" / "openapi-verdict.html").write_text(band, encoding="utf-8")
        content = {**BARE, "tabs": [
            BARE["tabs"][0],
            {"id": "api", "label": "API", "blocks": [{"type": "section", "id": "swaggerdiff"}]}]}
        page, _ = _build(tmp_path, content)
        return re.search(r'<button[^>]*id="tabbtn-api"[^>]*>', page).group(0)

    red = page_with('<div class="apiverdict red"><span class="v">Breaking change</span></div>')
    assert "alarm" in red, red
    green = page_with('<div class="apiverdict green"><span class="v">Backwards compatible'
                      '</span></div>')
    assert "alarm" not in green and "warn" not in green, green


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
    # And it is pasted once, not once at each end. Counted on the fragment's own opening
    # tag rather than on the bare word: `reqmap_layout` appends a hover for the names the
    # matrix has to cut, and its selector names the class too.
    assert one.count('class="reqmap"') == 1 and two.count('class="reqmap"') == 1


PR = dict(BARE, pr={"number": 37, "title": "Link Visit with Vet",
                    "url": "https://github.com/victorrentea/petclinic/pull/37",
                    "repo": "https://github.com/victorrentea/petclinic",
                    "branch": "test-pr", "base": "main"},
          subtitle="A visit now records the vet that attended it.")


def test_the_page_is_named_the_way_the_reviewer_s_other_tabs_name_it(tmp_path):
    """The content file's own title is a sentence about the change; the reviewer is
    looking at a pull request. `PR#37 Link Visit with Vet` is the name that matches their
    notifications, their tabs and their `gh pr` output, and the number is the link."""
    page, _ = _build(tmp_path, PR)
    head = page[page.index("<h1>"):page.index("</h1>")]
    assert "PR#37" in head and "Link Visit with Vet" in head
    assert "https://github.com/victorrentea/petclinic/pull/37" in head


def test_the_pr_number_says_on_hover_that_it_leaves_for_github(tmp_path):
    """The one link on the page a reader cannot spot by where it sits: it is the first
    word of the `<h1>`, wearing heading weight rather than a link's. So it says where it
    goes before it is clicked."""
    page, _ = _build(tmp_path, PR)
    head = page[page.index("<h1>"):page.index("</h1>")]
    assert 'data-tip="Open on GitHub"' in head


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
    assert 'data-tip="Why 5/10? Open the Review tab"' in row, "the hover says where the click goes"
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
    assert "PR#" not in page


def test_the_two_refs_the_page_compares_lead_the_scope_bar_and_are_clickable(tmp_path):
    """"Against what, again?" is asked halfway down the ninth tab, not while reading the
    first sentence — so the refs live in the pinned masthead. They lead the scope bar,
    which is the row that already answers *how much*, and each opens its own page on
    GitHub."""
    page, _ = _build(tmp_path, dict(PR, scope=[{"auto": "autofixed", "href": "#one",
                                               "by": "Opus 5"}]))
    bar = page[page.index('<div class="scopebar">'):]
    bar = bar[:bar.index("</div>")]
    assert "https://github.com/victorrentea/petclinic/tree/test-pr" in bar
    assert "https://github.com/victorrentea/petclinic/tree/main" in bar
    # Before every measurement of them: two refs and six numbers about those refs are one
    # thought, and the refs are the half that says what the numbers are of.
    assert bar.index("tree/test-pr") < bar.index("tree/main") < bar.index("Opus 5")
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
    assert "allbtn" in foot and "Single page" in foot


def test_the_footer_is_one_centred_line_ending_on_its_control_then_the_path(tmp_path):
    """The provenance and the offer are one sentence, centred — not two blocks pushed to
    opposite ends of a flex row, which on a wide screen read as a header bar rather than as
    the page's closing line. The one control is that sentence's last word, not a line of its
    own (5 Oct 2026: every footer line is a line the tab above does not get), and the
    page's path on disk is the second and last line, its 📋 after it."""
    page, _ = _build(tmp_path, BARE)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert '<p class="footrow">' in foot
    assert "footer .footrow { text-align:center; margin:0; }" in page
    assert "footer .footrow > span { display:inline; }" in page
    line = foot[foot.index('<p class="footrow">'):foot.index("</p>")]
    assert line.count("<span") >= 2 and "<div" not in line
    assert 'class="allbtn"' in line and "allbar" not in foot
    # The path, then the clipboard: text first, the mark that acts on it after.
    disk = foot[foot.index('class="diskline"'):]
    assert disk.index('id="hr-diskpath"') < disk.index('class="diskcopy copycmd"')
    assert disk.index("</span>") < disk.index("\U0001F4CB")
    # Off disk the path is plain text: no hover promising a click until a server says so.
    span = disk[disk.index("<span"):disk.index("</span>")]
    assert "data-tip" not in span and "button" not in span


def test_the_footer_says_where_the_page_came_from_and_where_to_see_it(tmp_path):
    """A review page is nearly always read on someone else's screen. The reader who reaches
    the bottom gets, in Victor's words (2 Oct 2026): the repo it was built by, the online
    demo, an invitation to take what they like, and the way back to the author."""
    page, _ = _build(tmp_path, BARE)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    text = re.sub(r"<[^>]+>", "", foot)
    assert ("Built by https://github.com/victorrentea/human-review. Browse it online. "
            "Adopt what you like. Bug or idea → Open an issue.") in text
    assert 'href="https://github.com/victorrentea/human-review"' in foot
    assert "Browse it <a" in foot and ">online</a>." in foot
    assert "https://victorrentea.github.io/human-review/" in foot
    assert 'href="https://github.com/victorrentea/human-review/issues/new/choose"' in foot
    # The docker offer and the old closing line are gone.
    assert "run it locally" not in foot and "pkgs/container/human-review" not in foot
    assert "adapt it to your liking" not in foot
    assert "Download here" not in foot and "Download zip" not in foot
    # The sentence first, the control as its last word.
    assert foot.index("takeaway") < foot.index("allbtn")


def test_the_content_files_provenance_sentence_is_not_said_twice(tmp_path):
    """The build says "Built by …" itself now; the content file's own "Report built by
    /human-review from db27oct." would be the same fact twice, in two wordings."""
    spec = dict(BARE, footer="Report built by /human-review from db27oct.")
    page, _ = _build(tmp_path, spec)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert "from db27oct" not in foot and "Report built by" not in foot
    assert foot.count("Built by") == 1


def test_the_offer_does_not_depend_on_what_the_content_file_says(tmp_path):
    """The footer sentence is the author's and may be missing entirely; the offer is the
    build's. A page with no `footer` in its content file still tells its reader where to
    find it again."""
    spec = {k: v for k, v in BARE.items() if k != "footer"}
    page, _ = _build(tmp_path, spec)
    foot = page[page.index("<footer>"):page.index("</footer>")]
    assert "Built by" in foot and "Browse it <a" in foot and ">online</a>." in foot


def test_the_show_all_button_says_what_it_does_next(tmp_path):
    """It is a toggle, and both of its states need a name now: the pressed styling alone
    carried the state while the button sat among the tabs, and at the foot of the page
    there is nothing beside it to read a highlight against."""
    page, _ = _build(tmp_path, BARE)
    assert 'data-label-off="⇄ Single page"' in page
    assert 'data-label-on="⇄ back to one tab at a time"' in page
    assert ">⇄ Single page</button>" in page, "the unpressed label is also the markup"
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


def test_a_page_link_the_film_never_showed_is_not_printed(tmp_path):
    """An app link whose phrase appears in no caption used to close the transcript as a
    "Not filmed. Touched by this change: …" row. Victor had it removed (7 Oct 2026): the
    area was not needed. The link must not resurface anywhere else either."""
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
    (tmp_path / "assets" / "f.webm").write_bytes(b"\x1aE\xdf\xa3")
    page, _ = _build(tmp_path, content)

    assert 'class="uncovered"' not in page
    assert "Touched by this change" not in page and "Not filmed" not in page
    assert "http://localhost:4200/visits" not in page
    assert "li.uncovered" not in page, "its stylesheet went with it"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))




# ── the footer's /human-review mention becomes the repo it names ─────────────────
# A reader who wants to copy this toolset has one obvious place to look, and the
# footer is it. The slash-command spelling only means something to somebody who
# already has the skill installed, so the mention is replaced by the address rather
# than merely linked — and doing it in the builder means no author has to remember
# it on any run.
def test_the_provenance_sentence_is_stripped_in_every_spelling():
    for old in ("Built by /human-review against the running stack.",
                "Built by /human-review on 2 Sep 2026.",
                "Report built by /human-review from db27oct.",
                "Built by /human-review on 2 Sep 2026. Every snippet is cut from the working "
                "tree at build time; every number on this page was measured by the step "
                "that produced it.",
                "Built by /human-review on 2 Sep 2026. Tell your agent to adapt this to "
                "your environment.",
                "Built by /human-review on 2 Sep 2026. Fork, Clone and Port with your Agent."):
        assert build._link_home(old) == "", old


def test_an_authors_other_sentence_survives_and_its_mention_is_linked():
    out = build._link_home("Report built by /human-review from db27oct. "
                           "Reviewed live with /human-review in the room.")
    assert out.startswith("Reviewed live with")
    assert 'href="https://github.com/victorrentea/human-review"' in out


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
    assert 'class="filemark" data-kind="edited"' in out
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
    assert 'class="filemark" data-kind="new"' in out, out


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
    assert "filemark" not in out
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


def test_a_deleted_test_links_to_where_it_stood_at_the_base_commit():
    """Run 6: every gone test linked a HEAD file:line where unrelated code now sits — its
    `line` is only where the removal landed. It is shown at its base line and opens the
    blob at the base commit; a vscode:// into HEAD is never emitted for it."""
    url = "https://github.com/acme/clinic/blob/5a97353e1234/src/test/VisitTest.java#L189"
    row = dict(MANIFEST[2], baseLine=189, baseSha="5a97353e1234", baseUrl=url)
    out = build.render_tests([row], Path("/repo"))
    assert "vscode://" not in out
    assert f'href="{url}"' in out and 'class="srcref testref tbase"' in out
    assert "VisitTest.java:189" in out and "VisitTest.java:240" not in out
    assert "at the base commit 5a97353e" in out


def test_a_deleted_test_with_no_github_remote_says_how_to_see_it_and_links_nothing():
    """An older manifest, or a repository not on github.com: no guessed URL, and still no
    HEAD link — the hover names the `git show` that prints it."""
    out = build.render_tests([dict(MANIFEST[2], baseLine=189, baseSha="5a97353e1234")],
                             Path("/repo"))
    assert "vscode://" not in out and "href=" not in out
    assert "git show 5a97353e:src/test/VisitTest.java" in out
    old = build.render_tests([MANIFEST[2]], Path("/repo"))
    assert "vscode://" not in old and 'class="srcref testref tgone"' in old


def test_a_stale_rename_key_in_an_old_manifest_says_nothing():
    """Renames are not paired any more (`test-changes.py` keys on the name): an old
    manifest's `renamedFrom` / `rewrittenFrom` is ignored, never printed."""
    out = build.render_tests(
        [{"name": "shows the first page", "path": "f/o.feature", "status": "modified",
          "line": 26, "renamedFrom": "lists every owner", "rewrittenFrom": "x"}],
        Path("/repo"))
    assert '<span class="tflag changed">modified</span>' in out
    assert "renamed" not in out and "ewritten" not in out


def test_a_test_edited_through_a_helper_is_filed_under_edited_and_names_the_helper():
    """Eval run 10: AddVisitApiTest kept every line of its own while the helper it calls
    was rewritten into a page-walking loop, and the ledger counted it among the tests
    "left exactly as they were". It is one of the edited, said apart from a body edit,
    with the helper on the hover."""
    row = {"name": "addsAVisitToAnExistingPet", "path": "src/test/AddVisitApiTest.java",
           "status": "modified", "line": 76,
           "viaHelper": [{"name": "anOwnerWithAPet", "line": 105, "added": 13, "removed": 6}]}
    out, moved = build.render_test_ledger([row, LEDGER_ROWS[-1]], Path("/repo"))
    assert moved == 1 and f'data-tip="1 test edited">{build.PENCIL}1</span>' in out
    assert "1 more test in the files this change set touched" in out
    # Its row, wherever a list renders it, names the helper.
    rows = build.render_tests([row], Path("/repo"))
    assert 'class="tnote tvia"' in rows and f">{build.VIA_HELPER_LABEL}</span>" in rows
    assert "it calls anOwnerWithAPet() (line 105, +13/−6)" in rows
    # A plain body edit carries no such note.
    assert "tvia" not in build.render_tests([LEDGER_ROWS[2]], Path("/repo"))


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


def test_the_tests_no_list_shows_are_named_on_their_counts_hover():
    """A deleted test is under no requirement by definition and runs nothing, so no
    covering list names it; a test that stopped running is in no covering list either.
    Their names are on their count's hover. New and edited ones are only counted: the
    covering card beside the line lists them."""
    out, moved = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert moved == 5, "everything but the untouched one"
    gone = out[out.index('class="removed"'):]
    assert 'data-tip="2 tests gone: VisitTest.java: parked, obsolete"' in gone
    off = out[out.index('class="toff"'):]
    assert 'data-tip="Still written; never runs: VisitTest.java: arrives_off"' in off
    for name in ("create_withVet", "update_ok", "untouched"):
        assert name not in out


def test_a_test_that_never_runs_is_counted_under_that_and_not_under_new():
    """`new` and `@Disabled` is not news about coverage, it is news about a test that has
    never run — and counting it under "new" hides it among the twenty-one that do run."""
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert ">1 stopped running</span>" in out and 'data-tip="1 new test">+1</span>' in out


def test_the_untouched_rest_are_counted_rather_than_listed():
    """A reviewer scrolling past a hundred unchanged names to find the two that went away
    is a reviewer who stops scrolling."""
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert "1 more test in the files this change set touched" in out


def test_the_ledger_is_one_line_of_counts_and_nothing_to_unfold():
    """Eval run 11 folded the ledger's four lists behind one line of counts; Victor
    dropped the fold (5 Oct 2026): on the requirements map the card beside it already
    lists the tests. One line of plain counts, the word each sign stands for on its hover."""
    out, _ = build.render_test_ledger(LEDGER_ROWS, Path("/repo"))
    assert out.startswith('<p class="tledger" id="test-ledger"') and out.endswith("</p>")
    for gone in ("<details", "<summary", "tledger-body", "tgroup", "<h3", "<li"):
        assert gone not in out, gone
    # Signs and counts only (Victor, 5 Oct 2026); the word each stands for is its hover.
    assert ('>1 stopped running</span> · '
            '<span class="added" data-tip="1 new test">+1</span> '
            '<span class="removed" data-tip="2 tests gone: VisitTest.java: parked, obsolete">'
            '\u22122</span> '
            f'<span class="changed" data-tip="1 test edited">{build.PENCIL}1</span></p>') in out
    # The untouched rest is a hover on the counts, not a sentence under them.
    assert 'data-tip="1 more test in the files this change set touched' in out
    css = (HERE / "hrbuild" / "assets" / "css" / "tests.css").read_text(encoding="utf-8")
    assert "tledger-body" not in css and "summary" not in css[css.index(".tledger"):][:400]


def test_the_ledger_has_no_heading_of_its_own(tmp_path):
    """The summary line is the section's title; an explicit `title` still renders one."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "tc.json").write_text(json.dumps(
        {"totals": {k: 0 for k in ("added", "modified", "deleted", "unchanged", "commented",
                                   "disabled", "reenabled", "runningBefore", "runningAfter",
                                   "gained", "lost")} | {"added": 1, "gained": 1},
         "tests": [{"name": "create_withVet", "path": "src/test/VisitTest.java",
                    "status": "added", "line": 10}]}), encoding="utf-8")
    page, _ = _build(tmp_path, dict(
        BARE, testChanges="assets/tc.json",
        sections=[{"id": "requirements", "title": "R", "body": "<p>a</p>"}],
        tabs=[{"id": "requirements", "label": "Tests",
               "blocks": [{"type": "section", "id": "requirements"}]}]))
    assert "What this change set did to the tests" not in page
    assert '<p class="tledger" id="test-ledger">' in page


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
    assert 'id="test-ledger"' in panel[:panel.index("</section>")]
    assert 'data-tip="1 new test">+1</span>' in panel[:panel.index("</section>")]


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


def test_the_chip_never_counts_renames_apart():
    """A retitled test is one deleted and one new (`test-changes.py`): the hover has no
    "renamed" clause, even fed an old manifest's totals."""
    chip = build.tests_chip({"totals": dict(TOTALS, modified=5, renamed=1, rewritten=2)})
    assert f'{build.PENCIL}5' in chip["value"]
    assert "renamed" not in chip["tip"] and "rewritten" not in chip["tip"]


def test_a_test_edited_through_a_helper_is_counted_once_as_edited_and_named_in_the_tooltip():
    chip = build.tests_chip({"totals": dict(TOTALS, modified=7, viaHelper=3)})
    assert f'{build.PENCIL}7' in chip["value"]
    assert "7 edited (3 via a helper)" in chip["tip"]
    assert "helper" not in build.tests_chip({"totals": TOTALS})["tip"]


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


# ── each pile is numbered from 1 ────────────────────────────────────────────────
# One list numbered straight through put "Auto-fixed" on 7 under a counts line that said
# "3 auto-fixed", and the reader went looking for the other six. Each pile has its own
# heading and its own count, so each counts from 1: no `counter-reset` offset on any of
# them, and the stylesheet's own `counter-reset:f` restarts the counter per `<ol>`.
def test_the_autofix_list_restarts_at_one_after_the_findings():
    findings = [{"title": "a", "body": "x"}, {"title": "b", "body": "y"},
                {"title": "c", "body": "z"}]
    build.reset_list()
    build.render_findings(findings)
    out = build.render_autofixes([{"title": "d"}])
    assert out.startswith('<ol class="findings">') and "counter-reset" not in out


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
    assert 'data-tip="/code-review docs · filed at high effort"' in out
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
    assert ">VetRestController.java</a>" in out, "lines 96-100 are in the href, not the face"
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


def _commit_on_feature(r, files: dict[str, str]):
    for name, text in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "more"], check=True)


def test_the_header_and_the_aftermath_band_read_one_generated_list():
    """Eval run 8: `run-steps.py` called the springdoc-written `openapi.yaml` generated, the
    header counted its +79/−29 as hand-written lines. Two lists had drifted; now there is
    one, and run-steps imports it rather than keeping a copy."""
    chips = importlib.import_module("hrbuild.shared.chips")
    src = (HERE / "run-steps.py").read_text(encoding="utf-8")
    assert "from hrbuild.shared.chips import GENERATED_GLOBS as GENERATED_DEFAULT" in src
    assert re.search(r"^GENERATED_DEFAULT\s*=\s*\(", src, re.M) is None, \
        "a second literal list in run-steps.py is the drift this test exists to stop"
    assert "openapi.yaml" in chips.GENERATED_GLOBS
    assert list(chips.GENERATED_PATHSPECS) == list(chips.GENERATED_GLOBS)


def test_a_regenerated_openapi_spec_is_not_counted_as_hand_written(tmp_path):
    r = _drifting_repo(tmp_path, with_generated=False)
    _commit_on_feature(r, {"openapi.yaml": "".join(f"p{i}: x\n" for i in range(79)),
                           "api/openapi.yaml": "a: b\n"})
    files, lines = build.diffstat_chips(r, build.base_state(r, "main"), None)
    # Top-level openapi.yaml is the springdoc output; `*` stops at a slash, so a nested
    # api/openapi.yaml is somebody's hand-written file and is still counted.
    assert '<span class="added">+2</span>' in files["value"], files["value"]
    assert '<span class="added">+7</span>' in lines["value"], lines["value"]
    assert "1 generated left out" in lines["tip"]


def test_review_bookkeeping_is_left_out_of_the_code_chips_and_named(tmp_path):
    """review-points.md and review-cost.json are the review's own record: not generated,
    not the change either. Out of the count, and said so by name in the hover."""
    r = _drifting_repo(tmp_path, with_generated=False)
    _commit_on_feature(r, {"review-points.md": "# Review\n" * 196,
                           "review-cost.json": "{}\n"})
    files, lines = build.diffstat_chips(r, build.base_state(r, "main"), None)
    assert '<span class="added">+1</span>' in files["value"]
    assert '<span class="added">+6</span>' in lines["value"]
    assert "review bookkeeping left out" in lines["tip"]
    assert "review-cost.json, review-points.md" in lines["tip"]
    assert "No generated files to leave out" in lines["tip"]
    assert "with them 4 files, +203" in lines["tip"]


def test_the_projects_generated_list_is_honoured_by_the_header(tmp_path):
    """`"generated"` in human-review.json replaces the default for the aftermath band, so
    it replaces it here too -- the same list in both readers, or none."""
    r = _drifting_repo(tmp_path)
    (r / "human-review.json").write_text(json.dumps({"generated": ["src/VetPicker.java"]}))
    files, _ = build.diffstat_chips(r, build.base_state(r, "main"), None)
    assert "+1" not in files["value"], "the project's list was not applied"
    # The project said what it generates: docs/generated/** and the genseq pair are its
    # own call now, and are counted.
    assert "1 generated left out" in files["tip"]


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
    assert "GitHub" in lines["tip"], "a chip that links says where the click goes"
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
    assert "drift" not in out and "\u26a0" not in out
    assert out.count('class="chip refchip') == 1, "one chip for the pair"
    assert 'branch <b class="refname head">feature</b> from <b class="refname base">main</b>' in out


def test_the_mark_ends_the_ref_chip_and_carries_its_own_tooltip(tmp_path):
    r = _drifting_repo(tmp_path, base_moves_ahead=True)
    out = build.ref_badges({"pr": {"branch": "feature", "base": "main"}},
                           build.base_state(r, "main"))
    assert out.count('class="chip refchip') == 1
    assert ('from <span class="drift" role="img" aria-label="stale base"') in out
    assert '1\u2193</span><b class="refname base">main</b>' in out and "\u26a0" not in out
    assert "1 commit ahead of the fork point" in out
    # No repo named, so the refs link nowhere and carry no hover; the mark's is the only one.
    assert out.count("data-tip") == 1


# --------------------------------------------------------------------------- #
# one base per page: the review's audited base, when the branch records one
# --------------------------------------------------------------------------- #
def _reviewed_repo(tmp_path, audited: str = "plan"):
    """`_drifting_repo`, plus the shape eval run 5 had: a plan committed on the branch
    before the review, the review's `audited-base` recorded at that plan, and the change
    the reviewers read on top of it. `audited` names which commit the record points at:
    `plan`, `main-ahead` (a commit that is not on the branch at all), or None."""
    import subprocess as sp
    r = _drifting_repo(tmp_path, with_generated=False)

    def run(*args):
        return sp.run(["git", "-C", str(r), *args], check=True, capture_output=True, text=True)

    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "plan.md").write_text("# plan\nstep one\nstep two\n")
    run("add", "-A")
    run("commit", "-qm", "plan the owners grid")
    plan = run("rev-parse", "HEAD").stdout.strip()
    (r / "src" / "Clinic.java").write_text("class Clinic {\n  int size;\n}\n")
    (r / "docs" / "plan.md").write_text("# plan\nstep one\nstep two, done\n")
    run("add", "-A")
    run("commit", "-qm", "implement the owners grid")
    review = r / ".human-review"
    review.mkdir()
    rev = {"plan": plan, "main-ahead": None, None: None}[audited]
    if audited == "main-ahead":
        run("checkout", "-q", "main")
        (r / "src" / "Owner.java").write_text("class Owner {\n}\n")
        run("add", "-A")
        run("commit", "-qm", "something else on main")
        rev = run("rev-parse", "HEAD").stdout.strip()
        run("checkout", "-q", "feature")
    if rev:
        (review / "review-points.json").write_text(json.dumps(
            {"provenance": {"auditedBase": rev, "base": rev}}))
    return r, review, plan


def test_the_page_measures_from_the_base_the_review_audited(tmp_path):
    """Eval run 5: three commits sat on the branch before the review's audited base. The
    header measured from origin/main's fork point (+3927 / -386) while every tab below it
    measured from the audited base (+2091 / -2220). One base now, the audited one."""
    r, review, plan = _reviewed_repo(tmp_path)
    st = build.page_base(r, review, "main")
    assert st["diffBase"] == plan and st["diffBaseSource"] == "audited"
    assert st["mergeBase"] != plan, "the fork point is still known — the ref chip warns off it"
    files, lines = build.diffstat_chips(r, st, None)
    assert '<span class="added">+1</span>' in files["value"]      # Clinic.java
    assert f"{build.PENCIL}1" in files["value"]                   # plan.md, edited
    assert "Visit.java" not in files["tip"] and "1 added, 1 edited" in files["tip"], \
        "VetPicker.java and Visit.java changed before the review: not this page's to count"
    assert plan[:8] not in lines["tip"], "which base it counts from is not the hover's business"


def test_the_commits_before_the_audited_base_are_named_under_the_chips(tmp_path):
    """Measuring from the audited base leaves a gap against GitHub's main...branch. It is
    said — as a `+2` on the branch chip, opening the list — rather than left for a reader
    to stumble on."""
    r, review, plan = _reviewed_repo(tmp_path)
    st = build.page_base(r, review, "main")
    assert [c["subject"] for c in st["outside"]] == ["plan the owners grid",
                                                     "link a visit to its vet"]
    note = build.outside_note(st, "https://github.com/acme/shop")
    # Eval run 6: six hashes spelled across the masthead pushed the tab strip down. Eval
    # run 10: even folded to one line, that line plus a wrapped chip row made the sticky
    # header 164px against 108px. The list is hidden; the count rides on the branch chip.
    assert note.startswith('<div class="scopenote" id="hr-outside" hidden><p class="sn-head">'
                           '2 earlier commits outside the review</p>')
    assert "plan the owners grid</li>" in note and "link a visit to its vet</li>" in note
    assert f'href="https://github.com/acme/shop/commit/{plan}"' in note
    page = build.masthead_html({"pr": {"branch": "feature", "base": "main"}}, "", "",
                               '<div class="tabstrip"></div>', st)
    chip = re.search(r'<span class="chip refchip[^"]*">.*?</span>(?=</div>)', page).group(0)
    assert '<button type="button" class="sn-badge" aria-expanded="false" ' \
           'aria-controls="hr-outside"' in chip and ">+2<" in chip, \
        "the count is a badge on the branch chip, not a line of its own"
    assert "2 earlier commits on this branch outside the review" in chip, "said in its hover"
    assert page.index('<div class="tabstrip">') < page.index('id="hr-outside"') \
        < page.index("</header>"), "the list opens in flow under the strip, inside the header"


def test_the_changes_own_spec_commit_is_listed_as_its_spec_not_counted_as_unreviewed():
    """Eval run 10: b12c9bdb — this change's OpenSpec proposal, design and spec — sat among
    the tooling commits as one of "8 earlier commits outside the review"."""
    st = {"diffBase": "a" * 40, "diffBaseSource": "audited", "outside": [
        {"sha": "1" * 40, "subject": "tooling"},
        {"sha": "2" * 40, "subject": "Document owners pagination plan",
         "spec": "openspec/changes/paginate-owners"}]}
    badge = build.outside_badge(st)
    assert ">+1<" in badge, "the spec commit is not counted among the unreviewed"
    assert "the spec this change was built against (22222222)" in badge
    note = build.outside_note(st, "https://github.com/acme/shop")
    assert "1 earlier commit outside the review" in note
    assert ("The spec this change was built against "
            "(<code>openspec/changes/paginate-owners/</code>)") in note
    assert note.index("tooling") < note.index("built against") < note.index(
        f'href="https://github.com/acme/shop/commit/{"2" * 40}"'), "linked, under its heading"
    only_spec = {**st, "outside": st["outside"][1:]}
    assert ">spec<" in build.outside_badge(only_spec) and "+0" not in build.outside_badge(only_spec)
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert "_before_range_commits(spec, root, base_st.get(\"ref\"))" in src, \
        "the build marks them off the Review tab's own reading, so the two agree"


def test_the_earlier_commits_fold_never_lies_over_the_tab_strip():
    """Eval run 8: opened, the fold dropped over the page as an absolute popover, covered
    11 of 13 tabs, and closed only on its own summary. It sits in flow now, and Esc or a
    click outside it closes it."""
    css = (HERE / "hrbuild" / "assets" / "css" / "masthead.css").read_text(encoding="utf-8")
    rule = re.search(r"\.scopenote \.sn-list \{([^}]*)\}", css).group(1)
    assert "position:absolute" not in rule and "position:fixed" not in rule
    assert "position:static" in rule
    js = (HERE / "hrbuild" / "assets" / "tabs.js").read_text(encoding="utf-8")
    assert "querySelector('.sn-badge')" in js and "'Escape'" in js
    assert "list.contains(ev.target)" in js, "a click inside the list must not close it"
    assert ".scopenote[hidden] { display:none; }" in css


def test_the_drift_mark_says_only_the_gap_and_the_fix(tmp_path):
    """Eval run 6: the chips counted from the audited base 5a97353e, the ⚠️ beside them
    from origin/main, and nothing in the row said the two were different yardsticks."""
    r, review, plan = _reviewed_repo(tmp_path)
    st = build.page_base(r, review, "main")
    warning = build.base_warning({**st, "ahead": 14})
    # Copy pass (3 Oct 2026): which commit the mark measures from was mechanics; the
    # hover keeps only the gap and the fix.
    assert warning == "origin/main is 14 commits ahead of the fork point. Merge or rebase, then rebuild."
    assert "Measured against" not in warning and plan[:8] not in warning
    assert build.base_warning({**st, "localBehind": 3}) is None, \
        "a stale local base changes nothing a page counted off the review base"


def test_the_lines_chip_opens_the_range_it_counted(tmp_path):
    """`compare/main...feature` on such a branch is the wider diff the chip does NOT
    count; the link has to open the audited range."""
    r, review, plan = _reviewed_repo(tmp_path)
    pr = {"repo": "https://github.com/acme/shop", "base": "main", "branch": "feature"}
    _, lines = build.diffstat_chips(r, build.page_base(r, review, "main"), None, pr)
    assert lines["href"] == f"https://github.com/acme/shop/compare/{plan}...feature"


def test_with_no_review_record_the_page_measures_from_the_fork_point(tmp_path):
    r, review, _ = _reviewed_repo(tmp_path, audited=None)
    st = build.page_base(r, review, "main")
    assert st["diffBaseSource"] == "merge-base" and st["diffBase"] == st["mergeBase"]
    assert st["outside"] == [] and build.outside_note(st) == ""
    assert "generated" in build.diffstat_chips(r, st, None)[1]["tip"]


def test_an_audited_base_that_is_not_on_the_branch_is_not_believed(tmp_path):
    """A record that points off the branch (a rebase since, a hand-edited front matter)
    would measure the change against a tree it never grew from."""
    r, review, _ = _reviewed_repo(tmp_path, audited="main-ahead")
    st = build.page_base(r, review, "main")
    assert st["diffBaseSource"] == "merge-base"


def test_producers_that_ran_against_another_base_are_flagged(tmp_path):
    """The tabs were drawn by `run-steps.py` before the build; if it was handed a different
    base than the one the page settles on, the page says so instead of mixing silently."""
    r, review, plan = _reviewed_repo(tmp_path)
    fork = build.base_state(r, "main")["mergeBase"]
    (review / "review-commits.json").write_text(json.dumps({"base": fork}))
    st = build.page_base(r, review, "main")
    assert st["diffBase"] == plan and st["stepsBase"] == fork
    (review / "review-commits.json").write_text(json.dumps({"base": plan}))
    assert build.page_base(r, review, "main")["stepsBase"] is None


def test_a_snippet_badge_measures_from_the_page_base(tmp_path):
    """`tasks.md` badged NEW FILE: it existed at the audited base, not at origin/main's
    fork point, and the badge was asking origin/main."""
    r, review, plan = _reviewed_repo(tmp_path)
    ext = build._extract_module()
    held = snippets.set_diff_base("origin/main")
    try:
        assert ext.block_status("docs/plan.md", r, [(1, 3)],
                                (r / "docs/plan.md").read_text().splitlines())["diff"] == "new"
        build.set_diff_base(plan)
        assert snippets.SNIPPET_BASE == plan
        assert importlib.import_module("hrbuild.tabs.sequence").SNIPPET_BASE == plan, \
            "a module that imported the name holds a copy, and it moves too"
        status = ext.block_status("docs/plan.md", r, [(1, 3)],
                                  (r / "docs/plan.md").read_text().splitlines())
        assert status["diff"] == "changed", "one line of three was edited after the plan"
        quiet = ext.block_status("docs/plan.md", r, [(1, 2)],
                                 (r / "docs/plan.md").read_text().splitlines())
        assert quiet["diff"] == "unchanged" and quiet["tip"] == "", \
            "copy pass: the badge's word is the whole message"
    finally:
        snippets.set_diff_base(held)


def test_codeowners_asks_the_page_base_not_origin_main(tmp_path):
    """Eval run 5's false alarm: `@elders` approval demanded for a file changed before the
    review's base. The tab is asked about the range the page counts."""
    import subprocess as sp
    r, review, plan = _reviewed_repo(tmp_path)
    (r / ".github").mkdir()
    (r / ".github" / "CODEOWNERS").write_text("/src/VetPicker.java @acme/elders\n")
    sp.run(["git", "-C", str(r), "add", "-A"], check=True)
    sp.run(["git", "-C", str(r), "commit", "-qm", "owners"], check=True)
    _, wide = build.codeowners_fragment({}, r, review)
    assert wide["state"] == "approval_required", "measured from the fork point, it fires"
    _, narrow = build.codeowners_fragment({}, r, review, plan)
    assert narrow["state"] != "approval_required", \
        "VetPicker.java changed before the audited base: not this review's to approve"


def test_without_a_pr_number_the_title_names_the_ticket():
    """The reference reads `PR#49 Link Visit with Vet (#37)`; with no PR yet, the
    ticket is the only number that says which request this answers."""
    ticket = {"number": 25, "title": "Add pagination", "url": "https://x/issues/25"}
    out = build.page_title({"title": "Owners grid", "pr": {"ticket": ticket}})
    nopr = out[:out.index("Owners grid")]
    assert ">no PR</span> " in nopr, "where PR#N would stand, the page says there is none"
    out = out[len(nopr):]
    assert out.startswith("Owners grid (<a class=\"prref ticketref\" href=\"https://x/issues/25\"")
    assert out.endswith(">#25</a>)")
    assert build.page_title({"title": "Owners grid (#25)", "pr": {"ticket": ticket}}) \
        .endswith("</span> Owners grid (#25)"), "a title that already names it is not told twice"
    assert build.page_title({"title": "Owners grid"}) == "Owners grid"


def test_the_review_chip_leads_with_what_is_left_to_do(tmp_path):
    """`12 raised` is the sum of the other two numbers, so it is the one nobody acts on.
    Open first, because that is the work; auto-fixed second, because it is the fact a
    reader cannot get anywhere else without opening the tab.

    Written as a sentence — `🤖Opus 5 reviewer: 9 open, 3 fixed` — rather than as a
    label, a gap and a row of figures: the second shape is what a measurement looks like,
    and this is a claim a model made about the diff.

    `fixed`, not `auto-fixed`: how the fix arrived is a word for the hover, and the pill
    needs the characters for the coder's half of the sentence."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one"}],
        findings=[{"title": f"f{i}", "body": "<p>b</p>", "source": "/code-review"}
                  for i in range(9)],
        autofixes=[{"title": f"a{i}", "source": "/simplify"} for i in range(3)]))
    # Eval run 10: the agent names moved to the hover, so the scope bar keeps one row.
    assert '"angry-bot" alt="" aria-hidden="true" src="data:image/png;base64,' in page and \
        '<b>9 open</b> · <b>3 fixed</b>' in page, \
        "every count is bold together with what it counts"
    assert "auto-fixed" not in page[page.index('<header class="masthead">'):
                                    page.index("</header>")], \
        "the long word stays on the counts line under the tab, which has room for it"
    # Copy pass (3 Oct 2026): the hover spells out the face and names the reviewer; the
    # per-pass split of the total left it.
    assert "Review: 9 open · 3 fixed." in page
    assert "12 raised" not in page


def test_the_review_chip_names_the_model_instead_of_a_second_chip_beside_it(tmp_path):
    """`LLM review  …` next to a hand-typed `reviewed by  Opus 5` was two chips carrying
    one thought, and only one of them was checkable."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one", "by": "Opus 5"}],
        findings=[{"title": "f", "body": "<p>b</p>", "source": "/code-review"}]),
        env=_sessionless_env())
    assert "<b>1 open</b>" in page and "Review: 1 open" in page, \
        "the face counts, the hover names the role"
    # The model has one home, the hover.
    assert "Reviewed by Opus 5." in page
    assert page.count("Opus 5") == 1


def test_a_page_rebuilt_with_no_idea_who_reviewed_it_says_exactly_that_much(tmp_path):
    """No session, no `by`, no name — and the label says only what it knows rather than
    guessing at the model that is most likely to have been used."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one"}],
        findings=[{"title": "f", "body": "<p>b</p>", "source": "/code-review"}]),
        env=_sessionless_env())
    assert "<b>1 open</b>" in page and "Reviewed by" not in page, \
        "the robot already says a model did it; `LLM` was three letters saying it again"
    assert "LLM review" not in page
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
    # The tooltip is `CONFIDENCE_TIP`, fixed — not a sentence composed around this item's
    # own number — and the face is a percentage, not a rate, said as what it measures:
    # `85% confident`, so the number beside `assumption` cannot be read as a score.
    assert ('<span class="f-confidence sev-info">'
            '85% confident</span>') in item
    assert "sev-med" not in item, "0.85 is not a low confidence"


@pytest.mark.parametrize("confidence, band", [
    (0.3, "sev-high"), (0.49, "sev-high"), (0.5, "sev-med"), (0.65, "sev-med"),
    (0.69, "sev-med"), (0.7, "sev-info"), (0.95, "sev-info")])
def test_a_confidence_chip_is_coloured_red_amber_green_by_band(tmp_path, confidence, band):
    """Red under 50%, amber under the lede's own "under 70% sure" line, green from 70% —
    so the chips and the lede count the same items as unsure (Victor, 7 Oct 2026)."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(confidence=confidence)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    item = re.search(r'<li class="n-assumed">.*?</li>', page, re.S).group(0)
    assert f'class="f-confidence {band}"' in item
    assert f">{round(confidence * 100)}% confident</span>" in item


def test_the_confidence_chip_is_as_big_as_the_title_and_on_its_baseline(tmp_path):
    """The one number the reviewer must not miss reads at the title's own size, coloured
    in dark and light alike."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(confidence=0.6)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    rule = re.search(r'\.f-confidence \{([^}]*)\}', page).group(1)
    assert "1em/" in rule and "vertical-align:baseline" in rule
    dark = page[page.index("@media (prefers-color-scheme: dark) {\n  .f-confidence.sev-high"):]
    for band in ("sev-high", "sev-med", "sev-info"):
        assert re.search(rf"\.f-confidence\.{band}\s*\{{", page)
        assert re.search(rf"\.f-confidence\.{band}\s*\{{", dark)


def test_an_inline_snippet_names_its_file_on_the_left_like_a_folded_one(tmp_path):
    """A one-line excerpt stays unfolded, its bar inside the figure; on the Review tab
    that bar starts left, where a folded card's summary puts the same file name."""
    page, _ = _build(tmp_path, dict(
        BARE, assumptions=[_assumption(confidence=0.6)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}]}]))
    assert re.search(r'#review figure\.snippet > \.srcbar \{ justify-content:flex-start;', page)
    assert "#review figure.snippet > .srcbar .srcbar-path { text-align:left; }" in page


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


def test_the_three_piles_are_numbered_separately(tmp_path):
    """Each pile opens on 1, whatever came before it: its last number is its own count."""
    page, _ = _build(tmp_path, dict(
        BARE,
        findings=[{"title": f"f{i}", "body": "<p>b</p>"} for i in range(2)],
        assumptions=[_assumption(title=f"a{i}") for i in range(3)],
        autofixes=[{"title": "fixed"}],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "findings"}, {"type": "assumptions", "mode": "A"},
                          {"type": "autofixes"}]}]))
    assert re.findall(r"counter-reset:f (\d+)", page) == []
    assert page.count('<ol class="findings">') == 3


def test_the_order_in_the_content_file_does_not_carry_numbers_across(tmp_path):
    """Put the piles the other way round and each still opens on 1."""
    page, _ = _build(tmp_path, dict(
        BARE,
        findings=[{"title": "f", "body": "<p>b</p>"}],
        assumptions=[_assumption(title=f"a{i}") for i in range(2)],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "assumptions", "mode": "A"}, {"type": "findings"}]}]))
    assert re.findall(r"counter-reset:f (\d+)", page) == []


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


def test_the_piles_render_as_rounds_whatever_the_content_file_order(tmp_path):
    """Round I (assumptions, while coding), round II (open, the review) and round III
    (auto-fixed, the pass that applied the fixes), each heading wearing its kicker once."""
    page, _ = _build(tmp_path, dict(
        BARE, findings=[{"title": "ff", "body": "x"}], autofixes=[{"title": "fixed"}],
        assumptions=[_assumption(title="aa")],
        tabs=[{"id": "review", "label": "Review",
               "blocks": [{"type": "autofixes"}, {"type": "findings"},
                          {"type": "assumptions", "mode": "A"}]}]))
    assert page.index('id="assumed"') < page.index('id="first"') < page.index('id="fixed"')
    assert page.count("<b>Round I</b>") == 1 and page.count("<b>Round II</b>") == 1
    assert page.count("<b>Round III</b>") == 1
    assert page.index("<b>Round III</b>") < page.index('id="fixed"')


def test_open_issues_are_listed_worst_first():
    out = build.render_findings([{"title": "n1", "severity": "low"},
                                 {"title": "w1", "severity": "medium"},
                                 {"title": "n2", "severity": "low"},
                                 {"title": "m1", "severity": "high"}])
    assert out.index("m1") < out.index("w1") < out.index("n1") < out.index("n2")


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
    # Truthful after a *Regenerate*: the measured tabs are current, the model's half is
    # not, and the list is cleared by a review pass, never by the button under it.
    assert "have not seen them; the other tabs are current" in out
    assert "Clears with a new review pass, not with Regenerate." in out
    assert "describes the branch as it was" not in out


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
    assert ('<a href="#assumed">2 implementation assumptions</a> &middot; '
            '<a href="#first">9 open LLM review issues</a> &middot; '
            '<a href="#fixed">3 auto-fixed</a>') in page, \
        "in round order — what was assumed while coding, then what the review raised and "\
        "fixed — and every clause is the jump to the chapter it counts"
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


def test_the_assumptions_intro_does_not_deny_the_code_quoted_under_it():
    """Eval run 8: the intro said "nothing here is in the diff" over ten assumption cards
    out of eleven that each quoted NEW CODE from the diff. The choice is not in the diff;
    the code it shaped is, and is quoted."""
    intro = build.pile_intro("assumptions", None)
    assert "nothing here is in the diff" not in intro


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
    # No pull request: the line's only extra is that fact, as its hover (eval run 11).
    assert ('<p class="sub counts pilelede" data-tip="No pull request yet — no GitHub '
            'links or publishing."><a href="#first">1 open LLM review issue</a></p>') in page
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


def test_a_diffs_header_is_the_shared_bar_with_no_icons_before_the_name(tmp_path):
    """The VS Code and github.com marks that led the bar are gone (Victor, 5 Oct 2026):
    the file name is the link. The header is still the page's one source bar, and nothing
    links from a footer under the diff."""
    import subprocess as sp
    r = _repo_with_a_buried_file(tmp_path)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    out = build.diff_html(rel, "HEAD^", r, head="HEAD")
    corner = out.split('<div class="ghdiff-scroll">')[0]
    assert "<svg" not in corner and "github.com" not in corner and "diffref" not in corner
    assert corner.startswith('<div class="ghdiff"><div class="srcbar"><a class="srcref srcbar-path"')
    assert "srcref" not in out.split('</table></div>')[-1]


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


def test_the_bar_reads_file_then_badge(tmp_path, monkeypatch):
    """One order, top to bottom of the page: which file, then what changed. The badge
    trails because "new file" is a fact *about* a file, and leading with it makes the
    reader hold it in mind until the bar finally says which file is new. Asserted on both
    producers, because a bar that only the Tests tab obeys is the three-headers problem
    coming back."""
    r = _repo_with_a_buried_file(tmp_path)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/victorrentea/petclinic.git"], check=True)
    badged = 0
    for out in (build.diff_html(rel, "HEAD^", r, head="HEAD"),
                build.snippet_html(f"{rel}:1-2", None, r, exact=True)):
        bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
        assert bar.startswith('<div class="srcbar"><a class="srcref srcbar-path"'), bar
        name = bar.index("srcbar-path")
        mark = next((m for m in ('class="filemark"', 'class="stat"') if m in bar), None)
        if mark:
            badged += 1
            assert name < bar.index(mark), bar
    assert badged, "neither producer emitted a badge — the order went untested"


def test_a_quoted_snippet_has_no_icons_before_its_file_name(tmp_path, monkeypatch):
    """The VS Code and github.com marks in front of the name are gone; the name itself is
    the link, and it is the bar's only one."""
    import subprocess as sp
    r = _repo_with_a_buried_file(tmp_path)
    sp.run(["git", "-C", str(r), "remote", "add", "origin",
            "https://github.com/victorrentea/petclinic.git"], check=True)
    rel = "petclinic-backend/src/main/java/victor/training/petclinic/repository/VetRepository.java"
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    out = build.snippet_html(f"{rel}:1-2", None, r, exact=True)
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert "ico-vsc" not in bar and "ico-gh" not in bar and "diffref" not in bar
    assert bar.count("<a ") == 1 and 'class="srcref srcbar-path"' in bar


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


def test_a_snippets_link_opens_where_its_own_face_says(tmp_path, monkeypatch):
    """A bar quoting line 10 of `A.java` opens line 10 — not the first line that differs, which in
    a real class is an import fifty lines above the finding."""
    r = _repo_whose_change_is_far_from_the_quote(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    out = build.snippet_html("A.java:10", None, r, exact=True)
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert ">A.java</a>" in bar, "the face is the file; the line is in the href"
    assert "/A.java:2:1" not in bar, "the decoy import"
    assert bar.count("/A.java:10:1") == 1, "the bar's own link, and nothing in front of it"


def test_the_logging_boxs_link_follows_it_to_the_statement(tmp_path, monkeypatch):
    """`link_at` moves the whole bar, not just its face. A logging window pulls in the
    lines a logged value came *from*, so the window opens above the statement the box is
    about."""
    r = _repo_whose_change_is_far_from_the_quote(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "HEAD^")
    out = build.snippet_html("A.java:8-11", None, r, exact=True, link_at=(10, 5))
    bar = out[out.index('<div class="srcbar">'):out.index("</div>", out.index('<div class="srcbar">'))]
    assert ">A.java</a>" in bar, "the face is the file; the line is in the href"
    assert "/A.java:8:1" not in bar, "the window's first line is not what the box is about"
    assert "/A.java:10:5" in bar


def test_a_snippet_whose_base_is_not_there_still_gets_its_bar(tmp_path, monkeypatch):
    """Each handle is emitted only where that side can really open what it promises — a
    base that does not resolve has no diff to show, and a dead button is worse than no
    button. What must not happen is the bar going with it: the file it came from is a
    fact regardless of what git can be asked."""
    r = _repo_with_a_buried_file(tmp_path)
    monkeypatch.setattr(snippets, "SNIPPET_BASE", "no/such/ref")
    out = build.snippet_html("README.md:1-2", None, r, exact=True)
    assert '<div class="srcbar">' in out
    assert "<svg" not in out.split("</div>")[0]
    assert ">README.md</a>" in out


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
    only the registry the 🎭 on those rows reads: the viewer, and per test the key the
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
    # `cd <repo> &&` and an absolute zip: the contract every command this page hands out
    # keeps, and the `cd` is not redundant beside it — `npx` resolves `playwright` out of
    # the project's own node_modules.
    assert reg["tests"][1]["cmd"] == (
        f"cd {tmp_path.resolve()} && npx playwright show-trace "
        f"{(tmp_path / '.human-review/t/2.zip').resolve()}")


def test_a_trace_row_is_addressed_by_its_test_and_the_header_says_which_page_this_is(tmp_path):
    """The covering-tests map names a test by file and declaration line; a trace row
    carries the same key, so the 🎭 the page hangs on the map's row is a lookup. And the
    header carries one chip that says whether this copy is served or static, emitted as
    static and promoted by the probe — never drawn live and demoted later."""
    doc = {"recorded": 1, "omitted": 0, "untraced": 0, "viewer": "assets/tv/index.html",
           "tests": [{"title": "adds a visit", "file": "src/add-visit.spec.ts", "line": 52,
                      "status": "passed", "duration": 10, "trace": "t/1.zip"}]}
    out, _ = build.render_traces(doc, tmp_path, tmp_path / ".human-review")
    assert '"test": "add-visit.spec.ts:52"' in out
    page, _ = _build(tmp_path, BARE)
    row = page[page.index('<div class="titlerow'):page.index("</div>", page.index('<div class="titlerow'))]
    assert '<span class="chip chip-mode" id="hr-mode">Static</span>' in row, \
        "in the title row, against the score, a label with no hover"
    assert '<button type="button" class="chip chip-serve copycmd" id="hr-serve"' in row
    assert "\U0001F4CB Serve</button>" in row
    assert "hr-mode" not in page[page.index("<footer>"):page.index("</footer>")]
    # Serve copies the way out of static: serve this directory, open the page from
    # the URL the server prints — never a port assumed in advance.
    m = re.search(r'id="hr-serve" data-copy="([^"]+)"', row)
    line = html.unescape(m.group(1))
    assert line.startswith("cd ") and "serve-review.py" in line and "--page review.html" in line
    assert 'u="$(' in line and 'open "$u"' in line and "7654" not in line
    assert line.endswith(' && exit'), "the server is detached: the terminal closes behind it"
    assert "if (serveChip) serveChip.hidden = true;" in page, "served: nothing left to copy"
    assert "chip.textContent = 'Served'" in page
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
    # Served, the 🎭 is a link into the viewer in a new window; off disk it copies the
    # show-trace line. The page carries no trace list, no frame, no rows.
    assert "'Open test replay'" in page
    assert "tv.target = '_blank'" in page and "copy(t.cmd)" in page
    assert "traceview" not in page and 'class="traces"' not in page


# --------------------------------------------------------------------------- #
# the 🎼: from a test on the Tests tab to the sequence it drew
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
    assert "<summary data-tip=\"test/add-visit.feature\">remembers the vet<span class=\"seqlang\">add-visit.feature</span></summary>" in out
    assert ">add-visit.feature</span></summary>" in out, "the basename is the label, not the heading"
    # Two chapters in one file are two lines of the contents, on one row.
    two = build._folded_pair(puml, rel, [""], scenarios=[(4, "one"), (9, "two")])
    assert ">one · two<span class=\"seqlang\">add-visit.feature</span></summary>" in two
    # Nothing recorded — the basename is all there is to call it.
    assert ">add-visit.feature<span class=\"seqlang\">add-visit.feature</span></summary>" in build._folded_pair(puml, rel, [""])


def test_a_pair_says_what_kind_of_test_drew_it(tmp_path):
    """Shut, this tab is a list of sentences, and "which of these went through a browser?"
    had no answer short of opening every one. The kind leads the row, in the Tests tab's
    own chip and the Tests tab's own three words — one vocabulary across the page."""
    rel, puml = _genseq_fixture(tmp_path)
    out = build._folded_pair(puml, rel, [""], scenarios=[(4, "remembers the vet")],
                             cat=build._pair_cat(puml, tmp_path))
    assert '<span class="testcat" data-cat="e2e"' in out
    assert ">E2E</span>remembers the vet" in out, "it leads the sentence"
    assert '<span class="seqlang">add-visit.feature</span></summary>' in out, "file at the right end"
    # The tip is now composed, so it is escaped as one string: a literal em dash, not the
    # `&mdash;` entity that used to be concatenated in after the escaping.
    assert 'data-tip="end to end: clicks the screen' in out, "the legend is on the hover"
    # The same three words the requirements map's legend uses, and no fourth.
    assert [c[0] for c in build.TEST_CATS.values()] == ["E2E", "API", "Unit"]
    # Same three colours as the evidence cards a few hundred lines up the stylesheet.
    assert ".testcat[data-cat=api]" in build.CSS and ".testcat[data-cat=unit]" in build.CSS


def test_a_pair_also_says_what_wrote_the_test(tmp_path):
    """`UI` covered a Playwright spec and a Cucumber feature alike, and the shut row gave
    a reader no way to tell which was which — though only one of them is written in a
    language a non-programmer reads. The runner qualifies the kind inside the same pill."""
    assert ">E2E<" in build._cat_chip("e2e", "petclinic-test/src/book.feature")
    assert build._lang_label("petclinic-test/src/add.spec.ts") == '<span class="seqlang">add.spec.ts</span>'
    assert ">AddVisitApiTest.java<" in build._lang_label("src/test/java/AddVisitApiTest.java")
    assert ">API<" in build._cat_chip("api", "src/test/java/AddVisitApiTest.java")
    # Unlike the kind, this IS the file: no diagram is consulted, and none is needed.
    assert build._pair_runner("a/b.feature") == ("Gherkin", "a Cucumber scenario")
    # Longest suffix first, or a Playwright spec would answer to a bare `.ts` rule.
    assert build._pair_runner("a/b.spec.ts")[0] == "TypeScript"
    # An extension this has never heard of leaves the chip exactly as it was.
    assert build._cat_chip("e2e", "a/b.rb") == build._cat_chip("e2e")
    assert ">E2E<" in build._cat_chip("e2e", "a/b.rb")
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


def test_the_row_links_the_test_file_instead_of_quoting_the_test(tmp_path):
    """The pair used to hold a "Show Test" fold: the excerpt under a bar reading
    `AddVisitApiTest.java:60-61,70-94`. Victor, 7 Oct 2026, "in the spirit of not rendering
    the code ourselves": the row's right end names the file, opens it in the editor at the
    scenario's own line, and wears the bar's file glyph — and the code is not on the page."""
    rel, puml = _genseq_fixture(tmp_path)
    fig = ('<figure class="snippet"><div class="srcbar"><a>x.feature:4</a>'
           + build.filemark("2 lines changed") + '</div><pre>code</pre></figure>')
    out = build._folded_pair(puml, rel, ["<p>picture</p>"], [fig],
                             scenarios=[(4, "remembers the vet")], root=tmp_path)
    label = out[out.index('<span class="seqlang">'):out.index("</summary>")]
    href = f'vscode://file/{(tmp_path / rel).resolve()}:4:1'
    assert f'<a class="srcref seqfile" href="{href}"' in label, "a srcref, at the scenario's line"
    assert 'data-tip="Open in VS Code: add-visit.feature (line 4)">add-visit.feature</a>' in label
    assert 'class="filemark" data-kind="edited"' in label, "the bar's glyph, beside the name"
    assert "testsrc" not in out and "<pre>code</pre>" not in out and "srcbar" not in out
    assert "Show Test" not in build.CSS
    # Not in this checkout: nothing to open, so the name is plain text.
    gone = build._folded_pair(puml, "test/gone.feature", [""], root=tmp_path)
    assert '<span class="seqlang">gone.feature</span>' in gone
    # The click opens the file and leaves the row as it was — put back, never stopped,
    # because the served page's editor handler listens on `document`.
    assert "details.testpair > summary a" in build.SEQFOLD_JS


def test_a_source_bar_draws_what_happened_to_the_file_instead_of_shouting_it():
    """`NEW FILE` / `NEW CODE` in caps beside a file name are read before the name they are
    a fact about (Victor: "the text badges are distracting"). Every source bar on the page
    — not only the Sequence tab's — wears the one file glyph instead, right after the
    name, with the words moved to the hover."""
    es = _extract_snippet()

    def badge(label, diff="new"):
        return es.diff_badge({"diff": diff, "label": label, "tip": "since origin/main"})

    new = badge("new file")
    assert "code-badge" not in new and 'class="filemark" data-kind="new"' in new
    assert build.FILE_PLUS in new and build.FILE_PAGE in new
    assert 'aria-label="new file"' in new, "the word is kept for a screen reader"
    assert "New file &mdash; since origin/main" in new, "…and for the hover"
    # `new file` and `new code` are both `new` to git and are not the same fact.
    assert 'data-kind="edited"' in badge("new code") and build.FILE_PENCIL in badge("new code")
    assert 'data-tip="New code' in badge("new code")
    assert 'data-kind="edited"' in badge("2 lines changed", "changed")
    assert 'data-kind="unchanged"' in badge("unchanged", "unchanged")
    assert build.FILE_PLUS not in badge("unchanged", "unchanged")
    assert ".srcbar .filemark" in build.CSS
    # The glyph trails the name, as the Sequence tab always drew it.
    bar = es.srcbar_html("vscode://file/x/A.java:1:1", "A.java", "1", new)
    assert bar.index("srcbar-path") < bar.index("filemark")


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
    carries the same key per drawn scenario, so the ⇥ is a lookup and not a guess."""
    js = build.SEQLINK_JS
    assert "document.getElementById('hr-genseq')" in js
    assert "querySelectorAll('.rm-t[data-id]')" in js
    assert "'.rm-seq'" in js and "\\u21E5" in js
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


# ── the Sequence tab says why it was not re-traced ──────────────────────────────────
# hr-try-4 (Copilot CLI, 2 Oct 2026): the traced suites need a trace collector and the
# dev stack, which were not up, so nothing was drawn — and the tab showed the committed
# diagrams, struck through, as though the branch had left its sequences alone. The reason
# lived in the status table and nowhere on the tab.

def _seq_verdict(review_dir: Path, **doc) -> None:
    (review_dir / "assets").mkdir(parents=True, exist_ok=True)
    (review_dir / "assets" / "sequence.verdict.json").write_text(
        json.dumps(doc), encoding="utf-8")


HR_TRY_4_RUNS = [
    {"command": "cd petclinic-test && ./run-tests-with-tracing.sh", "exit": 1,
     "outcome": "failed", "detail": "[tracing] aborting — nothing was started or stopped.",
     "log": ["   • OTLP collector (:4318)    → ./start-grafana.sh",
             "[tracing] aborting — nothing was started or stopped."]},
    {"command": "cd petclinic-backend && mvn -o -Pgenseq test -Dgroups=genseq", "exit": 1,
     "outcome": "no-tests",
     "detail": "FunctionalCucumberTest discovered no tests under the tag filter",
     "log": ["FunctionalCucumberTest » NoTestsDiscovered"]},
]


def test_a_tab_that_was_not_re_traced_says_so_even_when_it_has_nothing_else(tmp_path):
    review = tmp_path / ".human-review"
    _seq_verdict(review, state="skipped", reason="drew nothing", runs=HR_TRY_4_RUNS)
    out, weight, changes = build.render_testpairs({"title": ""}, {}, [], tmp_path, review)
    assert "Not re-traced on this run." in out and 'class="rband rband-warn' in out
    assert "./run-tests-with-tracing.sh</code>: exit 1" in out
    assert "discovered no tests under the tag filter" in out, \
        "a tag filter that matched nothing is named as such, not as a red suite"
    assert "<details" in out and "OTLP collector (:4318)" in out, "the last words, folded"
    # On the page, and not struck: a strike says the branch left its sequences alone,
    # which a run that drew nothing cannot know.
    assert weight == 1 and changes == 1


def test_the_band_names_what_did_not_answer_before_anything_ran(tmp_path):
    review = tmp_path / ".human-review"
    _seq_verdict(review, state="skipped", reason="x",
                 missing=["OTLP collector (tcp://127.0.0.1:4318)"])
    band = build.sequence_verdict_html(review)
    assert "<code>OTLP collector (tcp://127.0.0.1:4318)</code>" in band
    assert "steps.sequence.app" in band


def test_a_red_suite_is_an_alarm_and_a_tag_filter_alone_is_not(tmp_path):
    review = tmp_path / ".human-review"
    _seq_verdict(review, state="red", reason="x", runs=HR_TRY_4_RUNS)
    assert 'rband-alert' in build.sequence_verdict_html(review)
    assert build.sequence_verdict_alarm(review) == "traced suite red"
    _seq_verdict(review, state="notests", reason="", runs=HR_TRY_4_RUNS[1:])
    assert 'rband-none' in build.sequence_verdict_html(review)
    assert build.sequence_verdict_alarm(review) is None
    (review / "assets" / "sequence.verdict.json").unlink()
    assert build.sequence_verdict_html(review) == "", "a clean run draws nothing"


def test_a_tab_not_re_traced_wears_an_amber_pill_on_the_whole_page(tmp_path):
    _seq_verdict(tmp_path, state="skipped", reason="drew nothing", runs=HR_TRY_4_RUNS)
    page, _ = _build(tmp_path, {**BARE, "tabs": BARE["tabs"] + [
        {"id": "sequence", "label": "Sequence",
         "blocks": [{"type": "testpairs", "title": ""}]}]})
    pill = re.search(r'<button[^>]*id="tabbtn-sequence"[^>]*>', page).group(0)
    assert "warn" in pill and "quiet" not in pill
    assert 'aria-label="Sequence — not re-traced on this run"' in pill
    panel = page[page.index('<section class="panel" id="sequence"'):]
    assert "Not re-traced on this run." in panel[:panel.index("</section>")]


# ── eval run 6: a re-trace that lost calls, under "the diagrams below are this run's" ───
# The backend's in-process AddVisitApiTest could not reach the traced stack's
# notification-service; its regenerated picture lost NotificationService, the SMS gateway
# and both calls, and nothing on the tab said it contradicted the committed diagram.

LOST_ADD_VISIT = {"diagram": "generated/AddVisitApiTest.java.adds-a-visit.genseq.puml",
                  "participants": ["NotificationService", "SMS gateway"],
                  "calls": ["Backend → NotificationService: POST /api/notifications/visit-booked",
                            "NotificationService → SMS gateway: send-sms"]}


def test_a_trace_that_lost_calls_is_an_alarm_even_beside_a_tag_filter(tmp_path):
    review = tmp_path / ".human-review"
    _seq_verdict(review, state="notests", reason="", runs=HR_TRY_4_RUNS[1:],
                 lost=[LOST_ADD_VISIT])
    band = html.unescape(build.sequence_verdict_html(review))
    assert "rband-warn" in band and 'role="alert"' in band
    assert "this run's." not in band, "never 'this run's diagrams' over a degraded trace, silently"
    assert "Lost vs the committed diagrams" in band and "<b>NotificationService</b>" in band
    assert "AddVisitApiTest.java" in band
    assert build.sequence_verdict_alarm(review) == "trace lost calls the committed diagrams show"
    _seq_verdict(review, state="degraded", reason="", runs=[], lost=[LOST_ADD_VISIT])
    assert "This run's trace lost what the committed diagrams show." in \
        html.unescape(build.sequence_verdict_html(review))


def test_a_tag_filter_that_matched_nothing_says_its_cause_in_one_plain_line(tmp_path):
    """Maven prints it as `[ERROR] … MojoFailureException`, and the fold under the band was
    a red dump under a reassuring headline. The plain cause is said in the open; the raw
    lines stay folded."""
    review = tmp_path / ".human-review"
    runs = [{**HR_TRY_4_RUNS[1], "log": ["[ERROR] Failed to execute goal … MojoFailureException"]}]
    _seq_verdict(review, state="notests", reason="", runs=runs)
    band = build.sequence_verdict_html(review)
    fold = band.index("<details")
    cause = band.index("The cause, in plain words: FunctionalCucumberTest discovered no tests")
    assert cause < fold, "the plain cause is above the fold"
    assert band.index("MojoFailureException") > fold, "the raw log stays folded"


def test_the_pair_whose_retrace_lost_calls_is_flagged_on_its_own_picture(tmp_path):
    rel = LOST_ADD_VISIT["diagram"]
    svg = tmp_path / "p.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>d</text></svg>')
    (tmp_path / "MANIFEST.tsv").write_text(
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\tnew_svg\told_svg\n"
        f"P\t{rel}\tsequence\tmodified\tp.diff.puml\tp.svg\t\tp.svg\tp.svg\n")
    _seq_verdict(tmp_path, state="degraded", reason="", runs=[], lost=[LOST_ADD_VISIT])
    rows = build.read_manifest(tmp_path / "MANIFEST.tsv")
    out, _, changes = build.render_testpairs(
        {"type": "testpairs", "id": "sequences", "kind": "sequence", "title": ""},
        {"manifest": "MANIFEST.tsv"}, rows, tmp_path, tmp_path)
    pair = out[out.index('class="testpair"'):]
    assert "Lost vs the committed diagram:" in pair
    assert "<b>NotificationService</b>" in pair and "POST /api/notifications/visit-booked" in pair
    assert "A whole participant missing is usually the traced stack" in pair
    assert changes == 1


def test_the_tab_reads_this_runs_copy_of_a_diagram_not_the_restored_file(tmp_path):
    """The Sequence step gives the committed diagrams their bytes back and files what it
    drew in `.human-review/assets/genseq/`, pinned to the HEAD it traced."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "c"], cwd=tmp_path, check=True)
    rel = "generated/a.spec.ts.s.genseq.puml"
    (tmp_path / "generated").mkdir()
    (tmp_path / rel).write_text("@startuml\nparticipant Browser\n@enduml\n")
    overlay = tmp_path / ".human-review/assets/genseq"
    (overlay / "generated").mkdir(parents=True)
    (overlay / rel).write_text("@startuml\nparticipant Test\n@enduml\n")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True).stdout.strip()
    (overlay / ".head").write_text(head + "\n")
    assert build.genseq_file(rel, tmp_path) == overlay / rel
    (overlay / ".head").write_text("0" * 40 + "\n")
    assert build.genseq_file(rel, tmp_path) == tmp_path / rel, "a copy of another HEAD is ignored"
    assert ".human-review" in build.SKIP_DIRS, "the copies are never paired a second time"


def test_a_card_drawn_from_the_committed_puml_carries_the_committed_sidecar(tmp_path):
    """`delta not drawn` and `unchanged` draw the work tree's `.puml`. A later run of the
    suite re-identified the arrows whose payload moved, so the overlay's sidecar names ids
    that picture never drew — and the `200 ⊕` on it went dead while `select pets ⊕` worked."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "c"], cwd=tmp_path, check=True)
    rel = "generated/a.spec.ts.s.genseq.puml"
    side = rel[: -len(".puml")] + ".json"
    (tmp_path / "generated").mkdir()
    (tmp_path / rel).write_text("@startuml\nBackend --> Browser: [[genseq://committed 200]]\n@enduml\n")
    (tmp_path / side).write_text('{"details": {"committed": {"title": "200"}}}')
    overlay = tmp_path / ".human-review/assets/genseq"
    (overlay / "generated").mkdir(parents=True)
    (overlay / rel).write_text("@startuml\nBackend --> Browser: [[genseq://rerun 200]]\n@enduml\n")
    (overlay / side).write_text('{"details": {"rerun": {"title": "200"}}}')
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True).stdout.strip()
    (overlay / ".head").write_text(head + "\n")

    assert '"rerun"' in build.genseq_details(rel, tmp_path), "a pair drawn from the run reads the run"
    carried = build.genseq_details(rel, tmp_path, work_tree=True)
    assert '"committed"' in carried and '"rerun"' not in carried
    src = (HERE / "hrbuild/tabs/sequence.py").read_text(encoding="utf-8")
    for card in ("def _stale_sequence", "def _unchanged_sequence"):
        body = src[src.index(card):]
        body = body[:body.index("\ndef ")]
        assert "_context_svg(rel, root" in body and "genseq_details(rel, root, work_tree=True)" in body


# ── eval run 8: why each picture is there, and which of the branch's tests have none ────
# The Sequence step now traces the tests a branch wrote, untagged; their pictures exist only
# in the overlay (the step removes from the work tree what the run created), and the tab
# has to say of every picture whether it is there by tag or because the branch wrote it.

SEARCH_FEATURE = """Feature: Search owners

  @generate_sequence
  Scenario: Searching with an empty last name shows the first page
    When I open the owners page

  Scenario: Sorting by city, then reversing it
    When I sort the owners by "City"
"""


def _seq_puml(test_rel: str, line: int, title: str) -> str:
    return ("@startuml\n"
            f"title [[src://{test_rel}:{line}{{Click to open the test}} {title}]]\n"
            "participant Browser\nparticipant Backend\n"
            "Browser -> Backend: GET /api/owners\n@enduml\n")


def test_a_picture_says_whether_it_is_there_by_tag_or_because_the_branch_wrote_the_test(
        tmp_path):
    feat = "petclinic-test/src/owner-search.feature"
    tagged = "petclinic-test/generated/owner-search.feature.searching.genseq.puml"
    picked = "petclinic-test/generated/owner-search.feature.sorting-by-city.genseq.puml"
    (tmp_path / "petclinic-test/src").mkdir(parents=True)
    (tmp_path / "petclinic-test/generated").mkdir(parents=True)
    (tmp_path / feat).write_text(SEARCH_FEATURE)
    (tmp_path / tagged).write_text(
        _seq_puml(feat, 4, "Searching with an empty last name shows the first page"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
                   cwd=tmp_path, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True).stdout.strip()
    review = tmp_path / ".human-review"
    overlay = review / "assets/genseq"
    (overlay / "petclinic-test/generated").mkdir(parents=True)
    (overlay / picked).write_text(_seq_puml(feat, 7, "Sorting by city, then reversing it"))
    (overlay / ".head").write_text(head + "\n")
    sel = {"max": 6, "picked": [
        {"path": feat, "name": "Sorting by city, then reversing it", "line": 7,
         "status": "added", "suite": "e2e", "why": "written by this branch"},
        {"path": feat, "name": "Paging forward", "line": 30, "status": "added",
         "suite": "e2e", "why": "written by this branch"}],
        "left": [{"path": "petclinic-backend/src/test/java/OwnerListTest.java",
                  "name": "getAll", "status": "modified", "suite": "java",
                  "left": "over the cap of 6 traced tests"},
                 {"path": "petclinic-frontend/src/app/owner-list.component.spec.ts",
                  "name": "opens on the first page", "status": "added", "suite": None,
                  "left": "no traced suite runs this file"}]}
    (review / "assets/sequence.selection.json").write_text(json.dumps(sel))

    out, weight, _ = build.render_testpairs(
        {"type": "testpairs", "title": "", "snippets": dict(build.AUTO_SNIPPETS)}, {}, [],
        tmp_path, review)

    pairs = re.findall(r'<details class="testpair".*?</summary>', out, re.S)
    assert len(pairs) == 2 and weight == 2, "the overlay-only picture is on the tab too"
    by_title = {("Sorting" in p): p for p in pairs}
    assert 'data-why="added"' in by_title[True] and '<span class="sw-add">+</span></span>' in by_title[True]
    assert 'data-why="tagged"' in by_title[False] and '<span class="sw-at">@</span></span>' in by_title[False]
    assert "Decided to trace it because" in by_title[True]
    # The branch's own scenario is linked from its row at its own line, not left "not
    # excerpted here" — and not quoted: the page no longer renders the test's code.
    sorting = out[out.index(f'id="{build.pair_anchor(picked)}"'):]
    sorting = sorting.split('<details class="testpair"')[0]
    assert f'{feat}:7:1" data-tip="Open in VS Code: ' in sorting
    assert "I sort the owners by" not in html.unescape(sorting)
    assert "not excerpted here" not in sorting
    assert "seqsel" not in out, "the badges say why each diagram is there; no summary line"

    # Tagged AND written by this branch: the row says both, as the Tests tab does.
    ledger = [{"path": feat, "line": 4, "status": "added",
               "name": "Searching with an empty last name shows the first page"}]
    out2, _, _ = build.render_testpairs(
        {"type": "testpairs", "title": "", "snippets": dict(build.AUTO_SNIPPETS)}, {}, [],
        tmp_path, review, test_changes=ledger)
    both = [p for p in re.findall(r'<details class="testpair".*?</summary>', out2, re.S)
            if "Searching" in p][0]
    assert 'data-why="tagged"' in both
    assert '<span class="sw-at">@</span></span>' in both and "sw-add" not in both, "one badge"
    assert "carries the tracing tag" in both


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                   check=True, capture_output=True)


# ── "How were these captured?" ──────────────────────────────────────────────────────
# Victor, 4 Oct 2026: the page's readers do not know what an OpenTelemetry trace is, and
# even he, looking at the Sequence tab, could not tell how its diagrams had been made.

SEARCH_PUML = ("petclinic-test/generated/owner-search.feature."
               "searching-with-an-empty-last-name-shows-the-first-page.genseq.puml")
TESTPAIRS = {"type": "testpairs", "title": "", "snippets": {"auto": "genseq"}}


def _one_traced_pair(tmp_path) -> Path:
    feat = "petclinic-test/src/owner-search.feature"
    (tmp_path / "petclinic-test/src").mkdir(parents=True)
    (tmp_path / "petclinic-test/generated").mkdir(parents=True)
    (tmp_path / feat).write_text(SEARCH_FEATURE)
    (tmp_path / SEARCH_PUML).write_text(
        _seq_puml(feat, 4, "Searching with an empty last name shows the first page"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    return review


def _shot(review: Path, **meta) -> None:
    (review / "assets/sequence.trace.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    if meta:
        (review / "assets/sequence.trace.json").write_text(json.dumps(meta))


def _how(out: str) -> str:
    return out[:out.index('<details class="testpair"')]


def test_the_tab_opens_on_how_its_pictures_were_made_shut_and_without_a_shot_offers_none(
        tmp_path):
    review = _one_traced_pair(tmp_path)
    out, weight, _ = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)

    assert out.startswith('<details class="seqhow tabsub">'), \
        "with no title, first thing on the tab, and shut"
    titled = build.render_testpairs(dict(TESTPAIRS, title="Sequence diagrams of tests"), {}, [],
                                    tmp_path, review)[0]
    assert titled.startswith('<h2 class="tabtitle" id="sequences">Sequence diagrams of tests'
                             '</h2><details class="seqhow tabsub">'), \
        "with one, the title's subtitle: right under it"
    how = _how(out)
    summary = how[how.index("<summary>"):how.index("</summary>")]
    # Victor, 5 Oct 2026: one sentence, the fold's handle, reading without being opened —
    # and counting the tests the pictures came from. One pair here: singular.
    assert summary == ('<summary><span class="seqhow-ans">These diagrams were captured from '
                       '<a href="https://opentelemetry.io/" target="_blank" rel="noopener">'
                       'OpenTelemetry</a> traces of 1 test.</span>'), summary
    assert "How were these captured?" not in out
    assert how.count("<li>") == 3, "three steps, no more"
    assert "OpenTelemetry" in how and "<dfn>span</dfn>" in how and "<dfn>trace</dfn>" in how
    # No machinery in it: the reviewer is busy, and the two words are what they lack.
    assert not re.search(r"\.(sh|py|ts|json|puml)\b|run-tests|trace-shot", how)
    assert "seqhow-shot" not in out and "sequence.trace.png" not in out, \
        "no picture on disk, no fold offering one"
    assert weight == 1, "an explanation is not an exhibit"


def test_the_subtitle_counts_every_test_the_tab_draws_in_the_plural(tmp_path):
    review = _one_traced_pair(tmp_path)
    feat = "petclinic-test/src/owner-search.feature"
    other = SEARCH_PUML.replace("shows-the-first-page", "shows-the-second-page")
    (tmp_path / other).write_text(_seq_puml(feat, 4, "Searching with an empty last name "
                                                     "shows the first page"))
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    pairs = out.count('<details class="testpair"')
    assert pairs == 2, pairs
    # Both diagrams name the same scenario line: one test, drawn twice, is still one test.
    assert "OpenTelemetry</a> traces of 1 test." in _how(out)
    (tmp_path / other).write_text(_seq_puml(feat, 9, "Another scenario"))
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    assert "OpenTelemetry</a> traces of 2 tests." in _how(out)


def test_the_trace_closes_the_fold_full_size_and_names_the_pair_it_was_drawn_as(
        tmp_path):
    review = _one_traced_pair(tmp_path)
    _shot(review, diagram=SEARCH_PUML, traces=1, test="owner-search.feature")
    how = _how(build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0])

    shot = how[how.index('<div class="seqhow-shot">'):]
    assert how.count("<details") == 1, "no fold inside the fold: opened, the picture shows"
    assert how.index("</ol>") < how.index("seqhow-shot"), "at the END of the explanation"
    assert '<img src="assets/sequence.trace.png"' in shot
    assert 'href="assets/sequence.trace.png" target="_blank"' in shot, \
        "a click opens it full size, never a thumbnail"
    assert f'href="#{build.pair_anchor(SEARCH_PUML)}"' in shot
    assert "The trace of" in shot
    assert "Searching with an empty last name shows the first page" in shot, \
        "named as its pair is, so the two can be read side by side"
    assert "Grafana Tempo" in shot


def test_a_shot_of_a_test_with_several_traces_says_it_is_one_of_them(tmp_path):
    review = _one_traced_pair(tmp_path)
    _shot(review, diagram=SEARCH_PUML, traces=4)
    how = _how(build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0])
    assert "One of the 4 traces of" in how


def test_a_shot_whose_test_is_not_on_the_tab_links_nowhere(tmp_path):
    """A shot left from a run whose pictures this page no longer shows still explains what
    a trace is; it must not link to a pair that is not there."""
    review = _one_traced_pair(tmp_path)
    _shot(review, diagram="x/generated/Gone.java.gone.genseq.puml", traces=1,
          test="Gone.java")
    shot = _how(build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0])
    assert "seqhow-shot" in shot and 'href="#seq-' not in shot
    assert "The trace of Gone.java" in shot
    # And a PNG with no record beside it is still offered, said plainly.
    (review / "assets/sequence.trace.json").unlink()
    shot = _how(build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0])
    assert "A trace of this run, in Grafana Tempo." in shot


def test_a_tab_with_no_pictures_has_nothing_to_explain(tmp_path):
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    _shot(review, diagram=SEARCH_PUML, traces=1)
    out, weight, _ = build.render_testpairs({"title": ""}, {}, [], tmp_path, review)
    assert out == "" and weight == 0


def test_a_tagged_test_whose_dsl_helper_the_branch_edited_says_touched(tmp_path, monkeypatch):
    """Eval run 11: 'Add a visit to an existing pet…' is tagged, the branch edited the
    add-visit.dsl.ts it imports and its picture moved +20/−20 — and its row said only
    `tagged`. A direct import (or the test file itself) changed on the branch is `touched`;
    a file only reachable through another import is not, and the tooltip names the file."""
    spec = "petclinic-test/src/add-visit.spec.ts"
    puml = "petclinic-test/generated/add-visit.spec.ts.add-a-visit.genseq.puml"
    src = tmp_path / "petclinic-test/src"
    (src / "support").mkdir(parents=True)
    (tmp_path / "petclinic-test/generated").mkdir(parents=True)
    (tmp_path / spec).write_text(
        "import {test} from './support/trace-fixture';\n"
        "import * as sentences from './add-visit.dsl';\n\n"
        "test('Add a visit', {tag: '@generate_sequence'}, async () => {\n"
        "  await sentences.addVisit();\n});\n")
    (src / "add-visit.dsl.ts").write_text("import {deep} from './deep';\nexport const a = 1;\n")
    (src / "deep.ts").write_text("export const deep = 1;\n")
    (src / "support/trace-fixture.ts").write_text("export const test = 1;\n")
    (tmp_path / puml).write_text(_seq_puml(spec, 4, "Add a visit"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    seq = importlib.import_module("hrbuild.tabs.sequence")
    monkeypatch.setattr(seq, "SNIPPET_BASE", "HEAD^")
    review = tmp_path / ".human-review"
    review.mkdir()
    block = {"type": "testpairs", "title": "", "snippets": dict(build.AUTO_SNIPPETS)}

    def chip(msg):
        (src / "deep.ts").write_text(f"export const deep = {msg!r};\n")
        _git(tmp_path, "commit", "-qam", msg)
        seq._branch_changed.cache_clear()
        out, _, _ = build.render_testpairs(block, {}, [], tmp_path, review)
        return re.findall(r'<span class="seqwhy".*?</span></span>', out, re.S)[0]

    # Only a file reached through the helper changed: not a direct import, still `tagged`.
    assert '<span class="sw-at">@</span></span>' in chip("deep only")

    (src / "add-visit.dsl.ts").write_text("import {deep} from './deep';\nexport const a = 2;\n")
    touched = chip("edit the dsl")
    assert '<span class="sw-at">@</span></span>' in touched and "sw-edit" not in touched

    # A ledger entry still wins: the branch EDITED this scenario says `edited test`.
    ledger = [{"path": spec, "line": 4, "status": "modified", "name": "Add a visit"}]
    out, _, _ = build.render_testpairs(block, {}, [], tmp_path, review, test_changes=ledger)
    assert 'sw-at">@</span></span>' in out and "sw-edit" not in out


def test_direct_imports_resolve_java_classes_and_relative_ts_modules(tmp_path):
    seq = importlib.import_module("hrbuild.tabs.sequence")
    java = "app/src/test/java/p/AddVisitApiTest.java"
    for rel in (java, "app/src/test/java/p/genseq/GenerateSequence.java",
                "app/src/main/java/p/domain/Visit.java"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("class X {}\n")
    (tmp_path / java).write_text(
        "package p;\nimport p.genseq.GenerateSequence;\nimport p.domain.Visit;\n"
        "import static p.domain.Visit.of;\nimport java.util.List;\nclass AddVisitApiTest {}\n")
    assert seq._direct_imports(java, tmp_path) == [
        "app/src/test/java/p/genseq/GenerateSequence.java",
        "app/src/main/java/p/domain/Visit.java", "app/src/main/java/p/domain/Visit.java"]
    feature = "t/src/owner-search.feature"
    (tmp_path / feature).parent.mkdir(parents=True)
    (tmp_path / feature).write_text("Feature: x\n")
    assert seq._direct_imports(feature, tmp_path) == []


def test_without_a_selection_the_tab_says_nothing_new(tmp_path):
    assert build.picked_for("a.java", [(9, "defaultRequest_returnsFirstTen()")], {"picked": [
        {"path": "a.java", "name": "defaultRequest_returnsFirstTen", "line": 3}]}) is not None, \
        "a JUnit display name is matched by the method's name, whatever line it was found on"


def test_a_struck_tab_carries_no_explanatory_line(tmp_path):
    """The struck tab used to open on "Nothing on this tab changed on this branch. Shown for
    context." Victor, 5 Oct 2026: useless text; the strike on the pill already says it."""
    page, _ = _build(tmp_path, PACKAGES_CONTEXT)
    assert '<button type="button" class="tab quiet" role="tab" id="tabbtn-packages"' in page
    assert "Nothing on this tab changed" not in page and 'class="quietline"' not in page


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
    "schema": "review-points/2",
    "mode": "points", "source": "review-points.md", "fixed_in": "HEAD",
    "provenance": {"base": "2a45c210", "implementation": "7f3c1a9e"},
    "sections": {"Fixed": "Fixed", "Ignored": "Ignored", "Assumptions": "Assumptions"},
    "autofixes": [{"title": "fixed one", "refs": ["a.py:1"]}],
    "findings": [{"title": "declined one", "why": "out of scope", "refs": ["b.py:2"],
                  "severity": "medium"}],
    "assumptions": [{"title": "assumed one", "alternative": "the other reading",
                     "decidedBy": "agent", "refs": ["c.py:3"]},
                    {"title": "assumed two", "why": "because", "decidedBy": "agent",
                     "refs": ["c.py:9"]}],
    "warnings": [], "items": 4, "dropped": 0, "empty": False,
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


def test_a_takeover_note_becomes_an_amber_band_above_the_piles(tmp_path):
    """The file's own sentence about commits the piles never saw, rendered where the
    reader meets the piles — amber, like generated-only drift, because it is the same
    kind of qualification and neither an absence nor an alarm."""
    doc = dict(POINTS_DOC, note={"heading": "Taken over without a new pass — 21 Sep 2026",
                                 "html": "<p>folded in.</p><ul><li>6ef4ae6b x</li></ul>"})
    spec, points = _points_spec(doc, tmp_path)
    band = build.points_note_band(points)
    assert 'rband rband-warn' in band
    assert "Taken over without a new pass" in band and "<li>6ef4ae6b x</li>" in band
    # With no reviewed commit named, the prose stays and the list folds under it.
    assert "<p>folded in.</p><details" in band and "<summary>1 commit</summary>" in band
    # Naming the reviewed commit, the whole note folds to one row, hashes linked.
    doc = dict(POINTS_DOC, note={
        "heading": "Taken over without a new pass — 21 Sep 2026",
        "html": "<p>after <code>ce56d912</code>, folded in.</p>"
                "<ul><li>6ef4ae6b Rename it</li><li>f9f9faa4 Chain it</li></ul>"})
    band = build.points_note_band(_points_spec(doc, tmp_path)[1], "https://github.com/o/r")
    assert ('<summary><b>2 commits</b> made after the reviewed version '
            '(<a href="https://github.com/o/r/commit/ce56d912"') in band
    assert '<a href="https://github.com/o/r/commit/6ef4ae6b"' in band and "Rename it" in band
    assert "folded in." not in band, "the paragraph is gone; the heading rides in the hover"
    assert 'title="Taken over without a new pass — 21 Sep 2026"' in band
    assert build.points_note_band(_points_spec(POINTS_DOC, tmp_path)[1]) == ""
    assert build.points_note_band(None) == ""


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
    # A content file that writes its own piles keeps both words — as long as no report
    # sits beside it: a report, once there, is the Review tab whatever the file typed.
    old = {"findings": [{"title": "a", "body": "x"}], "autofixes": [{"title": "b"}],
           "tabs": [{"id": "review", "label": "R", "blocks": [
               {"type": "findings"}, {"type": "autofixes"}]}]}
    (tmp_path / "legacy").mkdir()
    build.resolve_review_points(old, tmp_path / "legacy")
    build.reset_list()
    out = "".join(build.render_pile_block(old, b, heading=lambda b, i, t: f"<h2>{t}</h2>")[0]
                  for b in old["tabs"][0]["blocks"])
    assert "<h2>Requires human review</h2>" in out and "<h2>Auto-fixed</h2>" in out
    assert ">auto-fixed<" in out


def test_the_scope_chip_says_the_same_thing_as_the_counts_line(tmp_path):
    """Two numbers over one review, in two places on the same screen. `3 fixed · 6
    declined` beside `6 open, 3 fixed` used to ask the reader which of them to
    believe; both now read `pile_numbers`, so a mismatch cannot recur."""
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert '"face": review_chip_face(open_n, fixed, assumed)' in src
    assert "open_n, fixed, assumed = pile_numbers(spec)" in src
    spec = {"findings": [{"title": f"f{i}"} for i in range(6)],
            "autofixes": [{"title": f"a{i}"} for i in range(3)],
            "assumptions": [{"title": f"s{i}"} for i in range(7)]}
    # PR #49's own numbers, which the old width cut used to drop the third of. The
    # assumptions are not an extra on the review's sentence any more — they are a second
    # sentence, by the agent that wrote the code, and it is never traded away for room.
    assert build.scope_chip_face(spec) == \
        ('\U0001f916Code: <b>7 unsure</b>; '
         '\U0001f916Review: <b>6 open</b>, <b>3 fixed</b>')
    small = {"findings": [{"title": "f"}], "autofixes": [{"title": "a"}],
             "assumptions": [{"title": "s"}]}
    assert build.scope_chip_face(small, "Opus 5") == \
        ('\U0001f916Code: <b>1 unsure</b>; '
         '\U0001f916Review: <b>1 open</b>, <b>1 fixed</b>')
    build.reset_list()
    lede = build.opening_lede(dict(spec, tabs=[{"id": "review", "label": "R", "blocks": [
        {"type": "findings"}, {"type": "autofixes"}, {"type": "assumptions", "mode": "A"}]}]))
    assert "6 open LLM review issues" in lede and "3 auto-fixed" in lede
    assert lede.index("open") < lede.index("auto-fixed")


def test_refuted_claims_are_not_on_the_page_and_not_counted_as_open(tmp_path):
    """Eval run 8: the header chip, the counts line and the tab pill all said `11 open`,
    four of them CONTEXT cards whose own `why:` read "refuted — …". A refuted claim is
    settled, so it is not counted as open and does not grade — and since 5 Oct 2026
    (Victor) it is not shown at all: no `N refuted` count, pill or pile."""
    live = [{"title": f"f{i}", "severity": "low", "why": "deliberate"} for i in range(7)]
    refuted = [{"title": f"r{i}", "severity": "info",
                "why": "refuted — this spec asserts <code>%2B</code>"} for i in range(3)]
    refuted.append({"title": "r3", "severity": "info", "why": "wrong — handleError rethrows"})
    # Said at a rank above info it is not a CONTEXT card, and the parser warns about it
    # instead: still open on the page until somebody files it right.
    misfiled = {"title": "m", "severity": "medium", "why": "refuted — but filed at medium"}
    spec = {"findings": live + refuted + [misfiled],
            "autofixes": [{"title": "a"}], "assumptions": []}
    assert build.pile_numbers(spec)[0] == 8
    assert build.refuted_number(spec) == 4, "the data still knows; the page does not say"
    badge = build.review_tab_badge(spec)
    assert badge == {"count": 8, "label": "8 open review issues"}
    assert build.scope_chip_face(spec) == '\U0001f916Review: <b>8 open</b>, <b>1 fixed</b>'
    build.reset_list()
    lede = build.opening_lede(dict(spec, tabs=[{"id": "review", "label": "R", "blocks": [
        {"type": "findings"}, {"type": "autofixes"}]}]))
    assert "8 open LLM review issues" in build._plain_text(lede)
    assert "refuted" not in lede.lower()
    signal = next(s for s in build._pile_signals(spec) if s["key"].startswith("open"))
    assert signal["short"].startswith("8 open review issues"), signal


def test_the_chip_carries_the_coders_assumptions_as_its_own_sentence(tmp_path):
    """`; 🤖coder: 7 assumptions` — a second claim, by a second agent. The reviewer's half
    is about the diff; this half is about what the agent that wrote the diff had to guess
    at, which is the one thing on this page no later pass can reconstruct. Its own robot,
    because the page's robot means "a model produced this" and the producer here is not
    the reviewer.

    Absent, not zeroed, when nothing was assumed: a chip that prints `0 assumptions`
    asserts an agent that guessed at nothing, which is not what an empty pile means."""
    page, _ = _build(tmp_path, dict(
        BARE, scope=[{"auto": "autofixed", "href": "#one", "by": "Opus 5"}],
        findings=[{"title": f"f{i}", "body": "<p>b</p>", "source": "/code-review"}
                  for i in range(6)],
        autofixes=[{"title": f"a{i}", "source": "/simplify"} for i in range(3)],
        # Seven anchored and one floating: the unanchored one is dropped before the
        # masthead is built, and the chip has to count what survived, not what was
        # written. A number on the masthead that the tab cannot show is a hand-typed
        # number by another route.
        assumptions=[_assumption(title=f"s{i}") for i in range(7)]
        + [_assumption(title="floating", refs=[])]),
        env=_sessionless_env())
    # Eval run 10: one robot, the counts only; the two agents are named in the hover.
    assert ('\U0001f916 <b>7 unsure</b> / <img class="angry-bot" alt="" aria-hidden="true" '
            'src="data:image/png;base64,') in page and '<b>6 open</b> · <b>3 fixed</b>' in page, \
        "every count bold with its noun, the coder's first"
    assert "Coding agent: 7 unsure. Review: 6 open · 3 fixed." in page, \
        "the hover's first sentence spells out what the face abbreviates"
    assert "recorded while implementing" not in page, \
        "copy pass: the face counts them and the Review tab lists them"
    # Singular, so the chip reads as a sentence rather than as a field with a value in it.
    assert build.scope_chip_face({"findings": [], "autofixes": [],
                                  "assumptions": [{"title": "s"}]}) == \
        ('\U0001f916Code: <b>1 unsure</b>; '
         '\U0001f916Review: <b>0 open</b>, <b>0 fixed</b>')
    # And gone entirely at zero — no `; 🤖coder: 0 assumptions`.
    assert build.scope_chip_face({"findings": [{"title": "f"}], "autofixes": []}) == \
        '\U0001f916Review: <b>1 open</b>, <b>0 fixed</b>'
    assert "coder" not in build.scope_chip_face(
        {"findings": [{"title": "f"}], "autofixes": [], "assumptions": []})


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
    """`🤖reviewer: 0 open, 0 fixed` is the whole failure this flow exists to end: two
    measured-looking zeros asserting a review that found nothing. Every other computed
    chip drops itself rather than print a number it cannot stand behind."""
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
    assert ('<h2 class="tabtitle" id="codecity">Impact on code size, complexity, coupling, …</h2>'
            in page), "the tab title every tab opens on, with no underline over the card"
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
    assert '<h2 class="tabtitle" id="codecity">Where the weight moved</h2>' in page
    assert build.CITY_HEADING not in page


def test_the_sequence_tab_is_never_struck_through(tmp_path, monkeypatch):
    """Victor, 5 Oct 2026: "The Sequence tab should never be crossed out. It isn't about the
    diff, it's about the overview of what really happens." A tab of nothing but unchanged
    pairs used to be struck; the pairs still count no delta, the pill just ignores it."""
    review = _one_traced_pair(tmp_path)
    monkeypatch.chdir(tmp_path)               # the build's root is the cwd's repository
    page, err = _build(tmp_path, {**BARE, "tabs": BARE["tabs"] + [
        {"id": "sequence", "label": "Sequence", "blocks": [TESTPAIRS]}]})
    assert '<span class="badge sev-info">unchanged</span>' in page, "no delta on it"
    pill = re.search(r'<button[^>]*id="tabbtn-sequence"[^>]*>', page).group(0)
    assert "quiet" not in pill, pill
    assert "Sequence" not in err.split("kept as context")[-1].split("\n")[0]
    # Every other tab still is, by the same rule as before.
    assert build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[2] == 0


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


# ── the rerun's own progress band ───────────────────────────────────────────────
# A press used to turn one glyph and put the producer's last line in a hover; on a
# fifty-second rebuild that is a page that looks exactly as it did, and a refresh of the
# tab lost even the glyph. The band is the run's progress -- the step, the count, an
# estimate -- and it comes back on a reload because the server still knows the run.


def test_the_progress_band_carries_last_runs_timings_as_its_expectations(tmp_path):
    (tmp_path / ".steps-cache.json").write_text(json.dumps({
        "version": 3, "steps": {"diagrams": {"key": "x", "seconds": 32.7},
                                "c2": {"key": "y", "seconds": 4.06}}}), encoding="utf-8")
    page, _ = _build(tmp_path, BARE)
    band = re.search(r'<div class="rerunprog" id="hr-rerun-progress"[^>]*>', page)
    assert band, "no progress band under the masthead"
    assert 'hidden' in band.group(0) and 'role="status"' in band.group(0)
    expect = json.loads(html_mod.unescape(re.search(r'data-expect="([^"]*)"', band.group(0))[1]))
    # In the order they ran, at the precision a reader would believe.
    assert list(expect["steps"]) == ["diagrams", "c2"]
    assert expect["steps"] == {"diagrams": 32.7, "c2": 4.1}
    assert expect["build"] > 0 and expect["default"] > 0
    # Going, done, failed: the three states of one press, in one row of the masthead.
    assert page.index('id="hr-rerun-progress"') < page.index('id="hr-rerun-done"') \
        < page.index('id="hr-rerun-fail"')


def test_a_page_with_no_step_history_still_gets_the_band(tmp_path):
    page, _ = _build(tmp_path, BARE)
    expect = json.loads(html_mod.unescape(re.search(r'data-expect="([^"]*)"', page)[1]))
    assert expect["steps"] == {}


def test_the_band_reads_the_runner_s_step_lines_and_survives_a_reload():
    """Grep-shaped, like every other guardrail on the page's scripts."""
    js = (HERE / "hrbuild" / "assets" / "rerun.js").read_text(encoding="utf-8")
    # The line `run-steps.py` prints as it starts a step, and the build line after them.
    assert "([*=-]) ([\\w-]+)" in js
    assert "build-review-html\\.py" in js
    # A run already going when the page loads is adopted: asked of the server, followed
    # to its end, and reloaded like a press would be.
    assert "function adopt()" in js
    assert "window.HR.status()" in js and "window.HR.follow(" in js
    server = (HERE / "hrbuild" / "assets" / "server.js").read_text(encoding="utf-8")
    assert "follow: poll" in server
    # The estimate never claims the end before the run reaches it.
    assert "cap * 0.95" in js


# ── the grade says what it is, and why ──────────────────────────────────────────
def test_the_grade_reasons_are_short_and_come_from_the_content():
    """Counts by severity, the assumptions pile, then each verdict bullet cut to its first
    clause — nothing the build writes itself. `verdict.why` wins when it is there."""
    spec = {"verdict": {"score": 6, "bullets": [
                "No build proved this commit: <code>ci</code> failed.",
                "One commit landed after the agent finished, and it touches 3 files."]},
            "findings": [{"title": "a", "severity": "medium"}, {"title": "b", "severity": "low"},
                         {"title": "c", "severity": "low"}],
            "assumptions": [{"title": "x", "confidence": 0.5}, {"title": "y", "confidence": 0.9}]}
    short = [s for s, _ in build.grade_reasons(spec)]
    assert short == ["3 open review issues: 1 worth a look, 2 nits",
                     "2 implementation assumptions unconfirmed, 1 under 70% sure",
                     "No build proved this commit",
                     "One commit landed after the agent finished"]
    out = build.grade_reasons_html(spec)
    assert 'id="grade-why"' in out and '<span class="gradewhy-n"><b>6</b>/10</span>' in out
    # The grade sits beside the bullets, not in a heading above them.
    assert "gradewhy-t" not in out and out.index("<ul>") < out.index("gradewhy-score")
    # `why` no longer replaces the computed reasons: it is the model's line under them.
    spec["verdict"]["why"] = ["CI never ran on <code>0746abc5</code>"]
    assert [s for s, _ in build.grade_reasons(spec)][-1] == "CI never ran on 0746abc5"
    assert [s for s, _ in build.grade_reasons(spec)][0].startswith("3 open review issues")
    assert build.grade_reasons_html({}) == ""


def test_the_model_adds_at_most_two_lines_to_the_computed_reasons(capsys):
    spec = {"verdict": {"score": 8, "bullets": ["Step 1.", "Step 2.", "Step 3.", "Step 4."]},
            "findings": [{"title": "a", "severity": "low"}]}
    short = [s for s, _ in build.grade_reasons(spec)]
    assert short == ["1 open review issue: 1 nit", "Step 1", "Step 2"]
    assert "shows the first 2" in capsys.readouterr().err


def _signals_dir(tmp_path, gate="green", seq="skipped", api=True):
    out = tmp_path / ".human-review"
    (out / "assets").mkdir(parents=True)
    (out / "assets" / "c2").mkdir()
    if gate:
        (out / ".gate.json").write_text(json.dumps({
            "sha": "0746abc56242b1d8", "verdict": gate,
            "caveat": f"{gate}: CI (run 37) for 0746abc56242",
            "workflows": [{"name": "CI", "runId": 37,
                           "verdict": "success" if gate == "green" else gate}]}))
    if seq:
        (out / "assets" / "sequence.verdict.json").write_text(json.dumps({"state": seq}))
    if api:
        (out / "assets" / "openapi-verdict.html").write_text(
            '<style>.x{}</style><div class="apiverdict red"><span class="v">Breaking '
            'change</span> <span class="n">· 1 endpoint broken '
            '(<a class="rep">report&nbsp;&#8599;</a>)</span></div>')
    return out


def test_the_grade_reasons_are_computed_from_what_the_page_measured(tmp_path):
    """Run 5: a green 8/10 whose two reasons were counts — nothing about the CI run, the
    breaking API change, or the Sequence tab nobody re-traced (and C2 drawn from it)."""
    out = _signals_dir(tmp_path)
    spec = {"verdict": {"score": 8}, "findings": [{"title": "a", "severity": "medium"}],
            "assumptions": [{"title": "x", "confidence": 0.5}]}
    keys = [s["key"] for s in build.grade_signals(spec, out, root=None)]
    assert keys == ["ci-green", "open", "assumptions", "api-breaking", "no-evidence"]
    assert build.cap_grade(spec) == 7
    assert spec["verdict"] == {"score": 7, "modelScore": 8}
    reasons = dict(build.grade_reasons(spec))
    assert reasons["CI green on 0746abc5"] == "CI green on 0746abc5", "no hover: the face links the run"
    assert "1 API endpoint broken (caps the grade at 7)" in reasons
    seq = next(full for short, full in reasons.items() if short.startswith("One tab carries"))
    assert "Sequence: not re-traced" in seq and "C2 view on Structure" in seq
    assert "report" not in reasons["1 API endpoint broken (caps the grade at 7)"]
    panel = build.grade_reasons_html(spec)
    assert '<b>7</b>/10' in panel and 'class="gradewhy-was"' in panel and ">capped<" in panel \
        and "AI graded it 8/10" in panel


def test_ci_green_links_to_the_run_it_rests_on(tmp_path):
    """Eval run 8: `CI green on 7fe310ab` was plain text, the run URL only in a hover.
    The run is the evidence, so it is a link a reader can click."""
    out = _signals_dir(tmp_path, seq=None, api=False)
    gate = json.loads((out / ".gate.json").read_text())
    gate["workflows"][0]["url"] = "https://github.com/acme/shop/actions/runs/37"
    (out / ".gate.json").write_text(json.dumps(gate))
    spec = {"verdict": {"score": 8}}
    build.grade_signals(spec, out, root=None)
    panel = build.grade_reasons_html(spec)
    assert ('\u2705 CI green on 0746abc5 — <a href="https://github.com/acme/shop/actions/runs/37" '
            'target="_blank" rel="noopener">CI run 37 ↗</a>') in panel


@pytest.mark.parametrize("gate,cap", [("failure", 4), ("skipped", 6), ("green", None)])
def test_the_ci_gate_caps_the_grade_and_never_raises_it(tmp_path, gate, cap):
    out = _signals_dir(tmp_path, gate=gate, seq=None, api=False)
    spec = {"verdict": {"score": 9}}
    build.grade_signals(spec, out, root=None)
    assert build.cap_grade(spec) == cap
    assert spec["verdict"]["score"] == (cap or 9)
    low = {"verdict": {"score": 3}}
    build.grade_signals(low, out, root=None)
    assert build.cap_grade(low) is None and low["verdict"]["score"] == 3


def test_a_page_with_no_measurements_keeps_the_models_grade(tmp_path):
    """No `.gate.json`, no verdict files: nothing is asserted, nothing is capped."""
    spec = {"verdict": {"score": 8}}
    assert build.grade_signals(spec, tmp_path, root=None) == []
    assert build.cap_grade(spec) is None and spec["verdict"] == {"score": 8}


def test_the_summary_no_longer_opens_the_review_tab(tmp_path, capsys):
    spec = {"summary": "<p>Model prose.</p>",
            "tabs": [{"id": "review", "blocks": [{"type": "findings"}]}]}
    build.drop_model_summary(spec)
    assert "summary" not in spec and "is not rendered" in capsys.readouterr().err
    other = {"summary": "<p>x</p>", "tabs": [{"id": "data", "blocks": [{"type": "section"}]}]}
    build.drop_model_summary(other)
    assert other["summary"] == "<p>x</p>"


def test_no_pull_request_means_no_publish_button(tmp_path):
    """Run 5 offered 'Publish comment on GitHub PR' on a branch with no PR."""
    assert not build.pr_exists({"pr": {"branch": "hr-claude-5"}}, tmp_path)
    assert build.pr_exists({"pr": {"number": 49}}, tmp_path)
    assert build.pr_exists({"pr": {"url": "https://github.com/o/r/pull/49"}}, tmp_path)
    (tmp_path / build.PR_POSTED_JSON).write_text("{}")
    assert build.pr_exists({"pr": {}}, tmp_path)


def test_no_pull_request_declares_no_push_action(tmp_path):
    repo = tmp_path
    out = repo / ".human-review"
    out.mkdir()
    (out / build.PR_COMMENTS_JSON).write_text(json.dumps({"comments": [{"pile": "fixed"}]}))
    spec = {"pr": {"branch": "b"}}
    assert build.prepare_pr_push(spec, out, repo, HERE) is None
    assert spec["_prPush"] is None and build.push_pr_button(spec) == ""
    spec = {"pr": {"number": 7}}
    assert build.prepare_pr_push(spec, out, repo, HERE)["count"] == 1


def test_an_assumption_says_why_it_is_as_sure_as_its_chip_says():
    out = build.render_assumptions([
        {"title": "t", "confidence": 0.55, "why": "Inferred from a sentence about booking.",
         "refs": []},
        {"title": "u", "why": "No number given.", "refs": []}])
    assert '<p class="f-why"><b>Why 55%:</b> Inferred from a sentence' in out
    assert '<p class="f-why"><b>Why:</b> No number given.</p>' in out


def _fix_repo(tmp_path):
    """An implementation commit, then one `[auto-fix]` commit carrying two fixes in one
    file (lines 3 and 40), and a third file no card names."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    lines = [f"line {i}" for i in range(1, 51)]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "c.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    lines[2] = "line 3 fixed"
    lines[39] = "line 40 fixed"
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "c.py").write_text("x = 2\n")
    (tmp_path / "review-points.md").write_text("## Fixed\n")
    git("add", ".")
    git("commit", "-qm", "[auto-fix]")
    return impl, git("rev-parse", "HEAD")


def test_each_fixed_card_shows_only_its_own_hunks_and_the_rest_follow_the_pile(tmp_path):
    """Run 5: one `[auto-fix]` commit, every card showing whole files against the
    implementation — the same file under two fixes, and files no card named under none."""
    impl, fix = _fix_repo(tmp_path)
    out = tmp_path / ".human-review"
    out.mkdir()
    first = {"title": "first", "refs": ["a.py:3"], "snippets": [{"ref": "a.py:3"}],
             "diffs": [{"path": "a.py", "base": impl}]}
    second = {"title": "second", "refs": ["a.py:41"], "diffs": [{"path": "a.py", "base": impl}]}
    spec = {"autofixes": [first, second],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, out, root=tmp_path)
    assert "line 3 fixed" in first["_fixDiffs"] and "line 40 fixed" not in first["_fixDiffs"]
    assert "line 40 fixed" in second["_fixDiffs"] and "line 3 fixed" not in second["_fixDiffs"]
    # Each card's stat counts its own hunk, not the file.
    assert '<span class="added">+1</span>' in first["_fixDiffs"]
    # The card no longer draws the whole file, and the snippet of lines its hunk shows goes.
    assert first["diffs"] == [] and first["snippets"] == []
    other = spec["_reviewPoints"]["fixOther"]
    assert "Other changes in the fix commit" in other and "x = 2" in other
    assert "review-points.md" not in other
    built = build.render_pile_block(spec, {"type": "autofixes"})[0]
    assert built.index("line 3 fixed") < built.index("Other changes in the fix commit")


def test_a_hunk_beyond_reach_of_every_anchor_is_nobodys(tmp_path):
    impl, fix = _fix_repo(tmp_path)
    card = {"title": "far", "refs": ["a.py:3"], "diffs": [{"path": "a.py", "base": impl}]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert "line 40 fixed" not in card["_fixDiffs"]
    assert "line 40 fixed" in spec["_reviewPoints"]["fixOther"]


def test_a_hunk_two_fixed_cards_share_is_drawn_once_under_the_first(tmp_path):
    """Eval run 8: two Fixed cards anchored at lines 179 and 192 of one spec, and the fix
    commit's single +25 hunk there was drawn in full under both, one after the other. It
    is drawn once now, under the first card; the second points at it. Eval run 11: the
    caption over it ('One hunk serves 2 fixes … shown once, here.') is gone — the pointer
    under the second card is the one line that says it."""
    impl, fix = _fix_repo(tmp_path)
    first = {"title": "first fix", "refs": ["a.py:3"], "snippets": [{"ref": "a.py:3"}]}
    second = {"title": "second fix", "refs": ["a.py:3-4"], "snippets": [{"ref": "a.py:3-4"}]}
    spec = {"autofixes": [first, second],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    both = first["_fixDiffs"] + second["_fixDiffs"]
    assert both.count("line 3 fixed") == 1, "one hunk, drawn once"
    assert "line 3 fixed" in first["_fixDiffs"]
    assert "One hunk serves" not in first["_fixDiffs"] and "fixshared" not in first["_fixDiffs"]
    assert "diff shown under <b>first fix</b>" in second["_fixDiffs"]
    assert "<b>first fix</b>" in second["_fixDiffs"]
    assert second["snippets"] == [], "its lines are on the page already, in the shared hunk"


def test_a_fix_hunk_goes_only_to_a_card_whose_line_is_on_it(tmp_path):
    """Attributed by overlap with the hunk as drawn (its change plus the context it shows),
    not by being somewhere in the same file: an anchor ten lines off takes nothing."""
    impl, fix = _fix_repo(tmp_path)
    near = {"title": "near", "refs": ["a.py:30"]}
    spec = {"autofixes": [near],
            "_reviewPoints": {"provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert "line 40 fixed" not in near["_fixDiffs"]
    assert "line 40 fixed" in spec["_reviewPoints"]["fixOther"]


def test_a_file_the_branch_added_keeps_its_diff_link(tmp_path, monkeypatch):
    """Eval run 12 dropped the diff link of every file the branch added — OwnerListPaging,
    OwnerListTest — with 'does not exist at <base>'. A new file's before-state is recorded
    too: nothing. The link stays (an all-additions diff, never through the extension URI,
    which reads the base side out of git); only a base that is not a commit drops it."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "old.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "New.java").write_text("class New {}\n")
    git("add", ".")
    git("commit", "-qm", "add")
    monkeypatch.setenv("HUMAN_REVIEW_DIFF_URI_HANDLER", "victorrentea.victor-vsc")
    link = build.diff_link_html("New.java", base, tmp_path, face="diff")
    assert 'class="srcref diffref srcbar-diff"' in link and 'data-diff-base="' + base in link
    assert ":1:1" in link and "data-diff-uri" not in link
    assert build.diff_link_html("New.java", "no-such-ref", tmp_path) == ""
    block = build.diff_html("New.java", base, tmp_path, None, "HEAD")
    assert "class New {}" in block, "the inline diff of a new file is all additions"


def test_an_extracted_constant_takes_its_use_sites_onto_its_own_card(tmp_path):
    """Eval run 12's Sonar S1192 fix: the card showed the two `static final` declarations
    and the eight lines that swap the literal for the constant sat under *Other changes*,
    where nothing said they were the same fix. A hunk of the same file that replaces the
    literal with the constant's name now goes to the card that declares it; a hunk that
    swaps nothing stays where it was."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    body = ["class Advice {", "    Logger log;"] + [f"    // filler {i}" for i in range(30)] \
        + ['    void a() { log.warn("Validation failed: {}", x); }'] \
        + [f"    // more {i}" for i in range(30)] + ["    int unrelated = 1;", "}"]
    (tmp_path / "Advice.java").write_text("\n".join(body) + "\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    body.insert(2, '    private static final String VALIDATION_FAILED = "Validation failed: {}";')
    body = [ln.replace('"Validation failed: {}", x', "VALIDATION_FAILED, x") for ln in body]
    body[body.index("    int unrelated = 1;")] = "    int unrelated = 2;"
    (tmp_path / "Advice.java").write_text("\n".join(body) + "\n")
    git("add", ".")
    git("commit", "-qm", "[auto-fix]")
    fix = git("rev-parse", "HEAD")
    card = {"title": "Validation literals duplicated", "refs": ["Advice.java:3"]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert "VALIDATION_FAILED = " in card["_fixDiffs"]
    assert "log.warn(VALIDATION_FAILED, x)" in card["_fixDiffs"], "the use site, on its card"
    other = spec["_reviewPoints"].get("fixOther") or ""
    assert "VALIDATION_FAILED, x" not in other
    assert "unrelated = 2" in other, "a hunk that swaps no literal is still nobody's"
    assert build.constants_declared(['  const MAX_SIZE = 20;', "  const x = 3;"]) == \
        {"MAX_SIZE": "20"}
    assert not build.replaces_constant(["y = 2026"], ["y = MAX"], {"MAX": "20"})


# ── eval run 10: the Review tab's judges ────────────────────────────────────────

def test_refuted_findings_are_left_off_the_page():
    """Run 10 drew three refuted claims inside the open pile; run 11 gave them a pile of
    their own under it. Victor (5 Oct 2026): not on the page at all — the open pile,
    numbered, and nothing after it."""
    live = [{"title": "live high", "severity": "medium", "why": "deliberate"},
            {"title": "live info", "severity": "info", "why": "the spec asks for it"}]
    refuted = [{"title": "plus sign", "severity": "info",
                "why": "refuted — Angular 16 encodes '+'"}]
    out = build.render_findings(live + refuted)
    ol, rest = out.split("</ol>", 1)
    assert ol.count("<li") == 2 and "plus sign" not in out
    assert rest == "" and "refuted" not in out.lower()
    only = build.render_findings(refuted)
    assert "plus sign" not in only and "refuted" not in only.lower()


def test_the_review_pill_hover_says_where_the_left_out_piles_are_without_lying():
    """Run 10's hover said the refuted were "further down the tab" while they sat inside the
    open pile, and the assumptions — the tab's first pile — are not below anything."""
    spec = {"findings": [{"title": "a", "severity": "low", "why": "x"},
                         {"title": "r", "severity": "info", "why": "refuted — no"}],
            "autofixes": [{"title": "f"}], "assumptions": [{"title": "s"}, {"title": "t"}]}
    label = build.review_tab_badge(spec)["label"]
    assert "further down" not in label
    assert label == "1 open review issue", "copy pass: the count, nothing else"


def _three_fix_commits(tmp_path):
    """Run 10's shape: the implementation, one `[auto-fix]` commit with the fixes and a
    re-recorded genseq trace, then two that only touch review-points.md / review-cost.json."""
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    lines = [f"line {i}" for i in range(1, 51)]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "c.py").write_text("x = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "s.genseq.json").write_text('{"id": "0ywzps7"}\n')
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    lines[2] = "line 3 fixed"
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "c.py").write_text("x = 2\n")
    (tmp_path / "docs" / "s.genseq.json").write_text('{"id": "0xnovwo"}\n')
    (tmp_path / "review-points.md").write_text("## Fixed\n")
    git("add", ".")
    git("commit", "-qm", "[auto-fix] the fixes")
    fixes = git("rev-parse", "HEAD")
    for i, msg in enumerate(("[auto-fix] anchor", "[auto-fix] re-anchor")):
        (tmp_path / "review-points.md").write_text(f"## Fixed\n<!-- {i} -->\n")
        (tmp_path / "review-cost.json").write_text("{}\n")
        git("add", ".")
        git("commit", "-qm", msg)
    return impl, fixes, git("rev-parse", "HEAD")


def test_every_fix_commit_is_named_and_the_re_anchors_are_said_to_be_bookkeeping(tmp_path):
    """Run 10 called `91905dff..e7b807e8` "the fix commit" — e7b807e8 a one-line re-anchor
    — and never named 6b14c32b, where the fixes were. Its own review-commits warning
    ("3 commits carry a Review-Points trailer…") never reached the page."""
    impl, fixes, last = _three_fix_commits(tmp_path)
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / "review-commits.json").write_text(json.dumps({"warnings": [
        f"3 commits carry a Review-Points trailer ({fixes[:8]}, x, {last[:8]}); taking the "
        "last one.", "no Claude-Session trailer on either commit — …"]}))
    card = {"title": "first", "refs": ["a.py:3"]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": last}}}
    build.attribute_fix_hunks(spec, out, root=tmp_path)
    points = spec["_reviewPoints"]
    assert [c["sha"] for c in points["fixCommits"]][0] == fixes
    assert [c["bookkeeping"] for c in points["fixCommits"]] == [False, True, True]
    intro = build.pile_intro("autofixes", points)
    assert f"<code>{fixes[:8]}</code>" in intro and f"<code>{last[:8]}</code>" in intro
    assert "3 fix commits" in intro and "(the fixes)" in intro
    assert "only re-record <code>review-cost.json</code> and <code>review-points.md</code>" \
        in intro
    assert "3 commits carry a <code>Review-Points:</code> trailer" in intro
    assert "Claude-Session" not in intro, "a cost-tab doubt, not the fixes'"
    assert "line 3 fixed" in card["_fixDiffs"]


def test_review_commits_lists_every_trailered_commit_and_says_they_all_count(tmp_path):
    """`review-commits.py` names every commit carrying the trailer (`review_commits`), and
    its warning no longer says "the page reports one" — the page reads them all."""
    spec_ = importlib.util.spec_from_file_location("review_commits", HERE / "review-commits.py")
    rc = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(rc)

    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "a.py").write_text("x = 2\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    shas = []
    for i in range(2):
        (tmp_path / "review-points.md").write_text(f"## Fixed\n<!-- {i} -->\n")
        git("add", ".")
        git("commit", "-qm", f"[auto-fix] round {i}\n\nReview-Points: review-points.md\n"
                             f"Implements: {impl}")
        shas.append(git("rev-parse", "HEAD"))
    found = rc.detect(tmp_path, base)
    assert found["review"] == shas[-1] and found["review_commits"] == shas
    assert found["implementation"] == impl
    w = next(w for w in found["warnings"] if "Review-Points trailer" in w)
    assert w.startswith("2 commits carry a Review-Points trailer")
    assert "every one of them counts as a fix commit" in w and "reports one" not in w


def test_generated_files_are_not_drawn_as_fixes_and_the_rest_is_folded(tmp_path):
    """Run 10 drew the re-recorded `*.genseq.json` traces as 4 KB one-line diffs (~3,000 px)
    under *Other changes in the fix commit*. A generated file is one line now; what no card
    reaches is folded, its count on the fold."""
    impl, fixes, last = _three_fix_commits(tmp_path)
    card = {"title": "first", "refs": ["a.py:3"]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": last}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    other = spec["_reviewPoints"]["fixOther"]
    assert "0xnovwo" not in other, "the generated trace's diff is not drawn"
    assert "1 generated file re-recorded in the fix commits" in other
    assert 'data-tip="docs/s.genseq.json"' in other
    assert other.startswith('<details class="fixother"><summary'), "folded by default"
    assert "<details class=\"fixother\" open" not in other
    assert "1 hunk in 1 file</summary>" in other and "x = 2" in other
    assert "review-cost.json" not in other.split("generated file")[0]


def _spec_repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("remote", "add", "origin", "https://github.com/o/r.git")
    (tmp_path / "README").write_text("x\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-qb", "feature")
    ch = tmp_path / "openspec" / "changes" / "page-owners"
    ch.mkdir(parents=True)
    (ch / "design.md").write_text(
        "# Design\n\n## Decisions\n\n### 1. Envelope\nOwn record.\n\n"
        "### 3. New indexes, verified\nAdd V4 with three indexes.\n\n## Risks / Trade-offs\n"
        "- Offsets drift under concurrent writes.\n")
    (ch / "tasks.md").write_text("# Tasks\n\n## 2. Indexes\n- [x] 2.1 Author V4 indexes\n")
    (tmp_path / "Q&A.md").write_text("# Q&A\n\n### Q3. Which columns are sortable?\n"
                                     "**Name and City only.**\n\n### Q5. Defaults?\nTen.\n")
    git("add", ".")
    git("commit", "-qm", "Document the plan")
    spec_sha = git("rev-parse", "HEAD")
    (tmp_path / "tool.sh").write_text("echo\n")
    git("add", ".")
    git("commit", "-qm", "tooling")
    audited = git("rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "feature")
    return spec_sha, audited, git("rev-parse", "HEAD")


def test_a_reason_citing_the_spec_links_the_line_and_quotes_it(tmp_path):
    """Run 10 dismissed open issues with "design.md Decision 3 and task 2.1 specify them"
    and "Q3 decided by the human" — and no link to any of those documents was on the page."""
    _spec_repo(tmp_path)
    item = {"title": "V4 indexes nobody asked for", "severity": "info",
            "why": "design.md Decision 3 and task 2.1 specify them; Q3 decided by the human; "
                   "<code>design.md</code> stays code."}
    spec = {"findings": [item], "autofixes": [], "assumptions": []}
    assert build.link_spec_citations(spec, tmp_path, root=tmp_path) == 3
    why = item["why"]
    design = re.search(r'<a class="specref" href="([^"]+)" data-tip="([^"]+)">design.md '
                       r'Decision 3</a>', why)
    assert design and design[1].endswith("openspec/changes/page-owners/design.md:8:1")
    assert "design.md:8 — 3. New indexes, verified — Add V4 with three indexes." in \
        html_mod.unescape(design[2])
    assert ">task 2.1</a>" in why and "tasks.md:4 — 2.1 Author V4 indexes" in \
        html_mod.unescape(why)
    assert ">Q3</a>" in why and "Q&amp;A.md:3 — Q3. Which columns are sortable?" in why
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert (f'href="https://github.com/o/r/blob/{head}/openspec/changes/page-owners/'
            'design.md#L8"') in why
    assert "<code>design.md</code> stays code." in why, "never inside code"


def test_a_citation_with_a_line_links_the_line_but_shows_only_the_file(tmp_path):
    """`design.md:8` in a reason reads `design.md` on the page; the editor link and the
    GitHub link still land on line 8 (Victor, 9 Oct 2026: no line numbers as labels)."""
    _spec_repo(tmp_path)
    item = {"title": "t", "severity": "info", "why": "As design.md:8 decided."}
    assert build.link_spec_citations({"findings": [item]}, tmp_path, root=tmp_path) == 1
    why = item["why"]
    assert re.search(r'<a class="specref" href="[^"]*design\.md:8:1"[^>]*>design\.md</a>', why)
    assert "design.md#L8" in why
    assert ">design.md:8<" not in why


def test_the_changes_own_spec_commit_is_not_an_unreviewed_commit(tmp_path):
    """Run 10 listed b12c9bdb — the OpenSpec proposal of this very change — among tooling
    commits as "never reviewed", and it capped the grade. It is the spec the change was
    built against: its own reason line, linked, no cap."""
    spec_sha, audited, _ = _spec_repo(tmp_path)
    spec = {"_reviewPoints": {"provenance": {"auditedBase": audited}}}
    sig = build._out_of_range_signal(spec, tmp_path, "origin/main", tmp_path)
    assert sig["short"] == "1 commit on the branch before the reviewed range"
    assert "tooling" in sig["full"] and "Document the plan" not in sig["full"]
    found = build._spec_commit_signals(spec, tmp_path, "origin/main")
    assert [s["short"] for s in found] == [f"Built against the spec in {spec_sha[:8]}"]
    assert found[0]["href"] == f"https://github.com/o/r/commit/{spec_sha}"
    assert found[0]["linkText"] == "openspec/changes/page-owners/" and not found[0]["cap"]


def test_a_model_line_that_contradicts_the_record_is_dropped_from_the_grade(capsys):
    """Run 10's hand-typed bullet tied Q5–Q15 to the coder's assumptions; Q&A.md says the
    planner adopted those answers, and none of the six assumptions is about any of them.
    A model line may not restate what the page computes unless it agrees with it."""
    spec = {"findings": [{"title": "a", "severity": "low", "why": "x"}],
            "autofixes": [], "assumptions": [{"title": "Empty ?size= means omitted"}],
            "verdict": {"score": 7, "bullets": [
                "GET /api/owners now answers <code>{content}</code> instead of an array.",
                "Built before Q5–Q15 were confirmed, so the assumptions below are the "
                "coder's answers to them.",
                "Leaves 4 open review issues to a human."]}}
    shorts = [s for s, _ in build.grade_reasons(spec)]
    assert any(s.startswith("GET /api/owners now answers") for s in shorts)
    assert not any("Q5" in s for s in shorts) and not any("4 open" in s for s in shorts)
    err = capsys.readouterr().err
    assert "no assumption on the page cites any of those questions" in err
    assert "it says '4 open review issues'; the page counts 1" in err
    build.grade_reasons(spec)
    assert capsys.readouterr().err.count("dropped") == 0, "said once per build"
    spec["assumptions"][0]["why"] = "Q5 left the empty value undefined."
    assert build.model_line_conflict(spec["verdict"]["bullets"][1], spec) is None


def test_the_pr_button_says_publish_whether_or_not_it_was_pushed_before():
    for posted in (0, 3):
        face = build.push_pr_button({"_prPush": {
            "count": 16, "posted": posted, "pushedAt": "2026-09-25T10:00:00",
            "counts": {"fixed": 3, "ignored": 6, "assumption": 7}}})
        assert ">Publish on GitHub</button>" in face
        assert "Push to GitHub PR" not in face and "Update GitHub PR" not in face


# ── eval run 6: the Review tab's judges ─────────────────────────────────────────

def test_a_verdict_bullet_is_never_cut_inside_code_or_braces():
    """Run 6: `…from an array to {content` — cut at the comma inside the <code> span."""
    bullet = ("GET /api/owners changes shape from an array to <code>{content, "
              "totalElements}</code>: backend and frontend must ship and roll back together.")
    assert build._first_clause(bullet) == ("GET /api/owners changes shape from an array "
                                           "to {content, totalElements}")
    assert build._first_clause("Calls f(a, b) twice") == "Calls f(a, b) twice"
    assert build._first_clause("No build proved this commit: <code>ci</code> failed.") \
        == "No build proved this commit"
    long = "word " * 60
    cut = build._first_clause(long)
    assert len(cut) <= build.CLAUSE_CAP + 1 and cut.endswith("…") and "  " not in cut
    spec = {"verdict": {"score": 7, "bullets": [bullet]}}
    assert ("GET /api/owners changes shape from an array to {content, totalElements}",
            html_mod.unescape(build._plain_text(bullet))) in build.grade_reasons(spec)


def _git_in(root):
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    return git


def _drift_repo(tmp_path):
    """An implementation commit, then an `[auto-fix]` commit that inserts two lines near
    the top of `A.java` (moving everything below) and deletes the line `gone();`."""
    git = _git_in(tmp_path)
    lines = [f"  line{i}();" for i in range(1, 31)]
    lines[19] = "  @Handler(Mismatch.class)"     # line 20
    lines[24] = "  gone();"                      # line 25
    (tmp_path / "A.java").write_text("class A {\n" + "\n".join(lines) + "\n}\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    after = lines[:2] + ["  static final String X = \"x\";", ""] + lines[2:]
    after.remove("  gone();")
    (tmp_path / "A.java").write_text("class A {\n" + "\n".join(after) + "\n}\n")
    (tmp_path / "review-points.md").write_text("## Fixed\n")
    git("add", ".")
    git("commit", "-qm", "[auto-fix] constants")
    return impl, git("rev-parse", "HEAD")


def test_an_assumption_written_at_the_implementation_is_carried_to_head(tmp_path, capsys):
    """Run 6: `ExceptionControllerAdvice.java:85`, written at the implementation, rendered
    at HEAD after the `[auto-fix]` commit added two lines above it — one blank line under
    an UNCHANGED badge. The ref is carried through the diff; a deleted line says so."""
    impl, fix = _drift_repo(tmp_path)
    moved = {"title": "Mismatch is a 400", "refs": ["A.java:21"],
             "snippets": [{"ref": "A.java:21"}]}
    deleted = {"title": "Gone", "refs": ["A.java:26"], "snippets": [{"ref": "A.java:26"}]}
    finding = {"title": "Read at the review commit", "refs": ["A.java:23"],
               "snippets": [{"ref": "A.java:23"}]}
    spec = {"assumptions": [moved, deleted], "findings": [finding],
            "_reviewPoints": {"source": "review-points.md", "frontmatter": {},
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.reanchor_refs(spec, tmp_path, root=tmp_path)
    assert moved["refs"] == ["A.java:23"] and moved["snippets"] == [{"ref": "A.java:23"}]
    assert (tmp_path / "A.java").read_text().splitlines()[22].strip() == \
        "@Handler(Mismatch.class)"
    # The deleted line keeps its file as a link, loses the snippet, and the card says why.
    assert deleted["refs"] == ["A.java"] and deleted["snippets"] == []
    card = build.render_assumptions([{**deleted, "_refs": []}])
    assert "a later commit removed that line" in card and "A.java:26" in card
    # A finding is read at the review commit: already where it belongs.
    assert finding["refs"] == ["A.java:23"] and "_anchorNotes" not in finding
    assert "no longer exists" in capsys.readouterr().err
    # Once `record-review.py finish` carried every ref, the page trusts the review commit.
    pinned = {"title": "x", "refs": ["A.java:21"]}
    spec = {"assumptions": [pinned],
            "_reviewPoints": {"frontmatter": {"anchors": "review-commit"},
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.reanchor_refs(spec, tmp_path, root=tmp_path)
    assert pinned["refs"] == ["A.java:21"]


def test_a_fixed_card_shows_the_fix_commits_hunks_whenever_one_follows_the_implementation(
        tmp_path):
    """Run 6: `fixed-in: HEAD` was in the front-matter only, so no item had `diffs` and
    every Fixed card fell back to a NEW CODE snapshot against main."""
    impl, fix = _fix_repo(tmp_path)
    card = {"title": "first", "refs": ["a.py:3"], "snippets": [{"ref": "a.py:3"}]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert "line 3 fixed" in card["_fixDiffs"] and card["snippets"] == []
    assert "vs <code>" not in card["_fixDiffs"] and '<details class="ghfold">' in card["_fixDiffs"]
    # No review commit recorded (an uncommitted record, an older report): the newest
    # `[auto-fix]` commit after the implementation is the fix commit.
    assert build.fix_commit({"provenance": {"implementation": impl}}, tmp_path) == fix
    bare = {"title": "first", "refs": ["a.py:3"]}
    spec = {"autofixes": [bare], "_reviewPoints": {"provenance": {"implementation": impl}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert "line 3 fixed" in bare["_fixDiffs"]
    # The implementation is HEAD: nothing after it, the card keeps its snapshot.
    assert build.fix_commit({"provenance": {"implementation": fix}}, tmp_path) is None


def test_the_review_chip_counts_each_reviewer_once():
    """Run 6: `1 by reviewer correctness, reviewer ticket-fit, reviewer tests, 3 by
    reviewer security, … 2 by reviewer correctness` — one reviewer, three groups."""
    items = [{"source": "reviewer correctness, reviewer ticket-fit"},
             {"source": "reviewer correctness"}, {"source": "reviewer security"},
             {"source": "/code-review high (the PUT scenario)"},
             {"source": "/code-review high (the GET scenario)"}, {}]
    tip = build._raised_by(items, 6)
    assert tip == ("6 raised — 2 by reviewer correctness, 1 by reviewer ticket-fit, "
                   "1 by reviewer security, 2 by /code-review high, 1 with no pass named "
                   "(1 raised by more than one reviewer)")


def test_the_review_chip_hover_counts_one_reviewer_under_one_name():
    """Eval run 10: `4 by correctness reviewer, … 2 by correctness, 1 by tests, 2 by
    ticket-fit reviewers, 3 by CI` — one reviewer under two labels, because `correctness
    reviewer` and the `correctness` of a shared plural were different strings, and the
    parts summed to 22 over 19 with nothing saying why."""
    srcs = (["correctness reviewer"] * 4 + ["security reviewer"] + ["tests reviewer"] * 5
            + ["ticket-fit reviewer"] * 3
            + ["correctness, tests and ticket-fit reviewers",
               "correctness and ticket-fit reviewers", "security reviewer",
               "CI (SonarCloud java:S1192)", "CI (SonarCloud typescript:S5906)",
               "CI (SonarCloud typescript:S2933)"])
    tip = build.raised_by_reviewer([{"source": x} for x in srcs], 19)
    assert tip == ("19 raised — 6 by correctness, 2 by security, 6 by tests, 5 by "
                   "ticket-fit, 3 by CI (2 raised by more than one reviewer, so the counts "
                   "add to 22)")
    # Word order and case are spelling, not a second reviewer.
    assert build.reviewer_names("Reviewer Correctness") == [("correctness", "Correctness")]
    assert [k for k, _ in build.reviewer_names("reviewer correctness, correctness reviewer")] \
        == ["correctness"]
    assert build.raised_by_reviewer([{"source": "/code-review high (the PUT scenario)"},
                                     {}], 2) == \
        "2 raised — 1 by /code-review high, 1 with no reviewer named"


def test_the_review_chip_face_is_counts_only_so_the_scope_bar_keeps_one_row():
    """Eval run 10: `🤖Code: 6 unsure; 🤖Review: 10 open · 3 refuted, 6 fixed` was 379px of
    a 1040px bar and wrapped it, taking the sticky header from 108px to 164px. And the
    refuted count is gone from it altogether (Victor, 5 Oct 2026)."""
    face = build.review_chip_face(10, 6, 6)
    assert face.startswith('\U0001f916 <b>6 unsure</b> / <img class="angry-bot"')
    assert face.endswith('<b>10 open</b> · <b>6 fixed</b>')
    face = build.review_chip_face(1, 0, 0)
    assert "unsure" not in face and face.endswith('<b>1 open</b> · <b>0 fixed</b>'), \
        "no assumptions: that count is absent, not zeroed"
    assert build.review_chip_key(10, 6, 6) == \
        "Coding agent: 6 unsure. Review: 10 open · 6 fixed."


def test_without_a_pull_request_the_page_says_so_on_hover_only(tmp_path):
    """Eval run 11: a visible line under the counts, among the one-liners a busy reviewer
    reads past. It is the counts line's hover now — where the publish button would be."""
    spec = {"pr": {"branch": "hr-claude-6"}}
    build.prepare_pr_push(spec, tmp_path, tmp_path, HERE)
    line = build.no_pr_line(spec)
    assert line == ' data-tip="No pull request yet — no GitHub links or publishing."'
    spec = {"pr": {"number": 49}}
    build.prepare_pr_push(spec, tmp_path, tmp_path, HERE)
    assert build.no_pr_line(spec) == ""


def test_commits_before_the_reviewed_range_are_on_the_branch_when_there_is_no_pr(tmp_path):
    """Run 6 said `6 commits in the PR before the reviewed range` with no PR open."""
    git = _git_in(tmp_path)
    (tmp_path / "a").write_text("0\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    for i in (1, 2):
        (tmp_path / "a").write_text(f"{i}\n")
        git("commit", "-qam", f"before {i}")
    audited = git("rev-parse", "HEAD")
    spec = {"pr": {"branch": "b"},
            "_reviewPoints": {"provenance": {"auditedBase": audited}}}
    sig = build._out_of_range_signal(spec, tmp_path, "origin/main", tmp_path)
    assert sig["short"] == "2 commits on the branch before the reviewed range"
    spec["pr"]["number"] = 7
    sig = build._out_of_range_signal(spec, tmp_path, "origin/main", tmp_path)
    assert sig["short"] == "2 commits in the PR before the reviewed range"


def test_a_narrowed_ticket_sentence_is_a_grade_reason_that_links_its_decision(tmp_path):
    """Run 6: 'sortable by any column' delivered for Name and City only, recorded in the
    OpenSpec proposal — and visible only as a hatched sentence on the Tests tab."""
    _git_in(tmp_path)
    prop = tmp_path / "openspec/changes/paginate/proposal.md"
    prop.parent.mkdir(parents=True)
    prop.write_text("# Why\n\n- Sorting is limited to Name and City, narrowing #25.\n")
    out = tmp_path / ".human-review"
    (out / "assets").mkdir(parents=True)
    (out / "assets" / "test-mapping.merged.json").write_text(json.dumps({"sentences": [
        {"id": "s1", "coverage": "narrowed", "decision": "d2", "tests": [],
         "gap": "Only Name and City sort.",
         "decisionText": "Scope (paginate/proposal.md): Sorting is limited to Name and "
                         "City, narrowing #25."},
        {"id": "s2", "coverage": "covered", "tests": []}]}))
    (out / "assets" / "requirements-map.html").write_text(
        '<span class="rm-f" data-s="s1" data-cov="narrowed">The grid should be sortable '
        'by <code>any</code> column</span>')
    spec = {"verdict": {"score": 8}}
    sig = next(s for s in build.grade_signals(spec, out, root=tmp_path)
               if s["key"] == "narrowed")
    assert sig["short"] == "Ticket narrowed on purpose: “The grid should be sortable by " \
                           "any column”"
    assert sig["linkText"] == "proposal.md:3" and sig["cap"] is None
    assert sig["href"].endswith("proposal.md:3:1")       # no github remote: the editor
    assert "Sorting is limited to Name and City" in sig["full"]
    panel = build.grade_reasons_html(spec)
    assert '— <a href="vscode://file/' in panel and ">proposal.md</a></li>" in panel, \
        "the link lands on line 3; its face is the file"
    assert spec["verdict"] == {"score": 8}, "a decided narrowing does not lower the grade"


def test_an_unchanged_badge_never_sits_over_a_plus_gutter(tmp_path):
    """Run 6: one added blank line, badged UNCHANGED (blank lines are not counted) and
    drawn with a `+` beside it. The gutter says what the badge says."""
    es = _extract_snippet()
    es._diff_state.cache_clear()
    _tiny_repo(tmp_path, ["const a = 1;", "const b = 2;"],
               ["const a = 1;", "", "const b = 2;"])
    out = es.render("a.ts:2", None, tmp_path, exact=True)
    assert 'class="filemark" data-kind="unchanged"' in out
    assert '<span class="dm">+</span>' not in out and "ln-row added" not in out


def test_the_build_drops_a_pr_payload_pinned_to_another_branch(tmp_path, capsys):
    """Run 6's review directory still held run 5's pr-comments.json (0746abc5)."""
    git = _git_in(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "c")
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / build.PR_COMMENTS_JSON).write_text(json.dumps(
        {"version": 1, "commit_id": "0746abc56242b1d8" + "0" * 24, "event": "COMMENT",
         "comments": [{"pile": "fixed", "title": "t", "path": "a.py", "line": 1,
                       "side": "RIGHT", "body": "b"}]}))
    build.prepare_pr_push({"pr": {"number": 7}}, out, tmp_path, HERE)
    assert not (out / build.PR_COMMENTS_JSON).exists()
    assert "0746abc5 is not in this clone" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the Data tab: every diagram the block names is on it, changed or not
# --------------------------------------------------------------------------- #

def test_the_data_tab_shows_every_named_diagram_and_says_what_the_erd_cannot(tmp_path):
    """hr-try-4: `only: ["DomainModel", "DB"]`, the Domain Model's .puml moved only its
    link line numbers (filed `unchanged` by `puml-diff.sh`), and DB.puml did not move at
    all because the migration only added an index — so the tab showed an empty delta and
    no DB. Both are on it now, as plain UNCHANGED cards, and the index is named under the
    DB card, which is what keeps the tab from being struck through."""
    import os
    run = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                    capture_output=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "DomainModel.puml").write_text("@startuml\nclass Owner {\n}\n@enduml\n")
    (docs / "DB.puml").write_text("@startuml\nentity owners {\n}\n@enduml\n")
    (docs / "DB.sql").write_text("CREATE TABLE public.owners (\n    id integer\n);\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    (docs / "DB.sql").write_text("CREATE TABLE public.owners (\n    id integer\n);\n\n"
                                 "CREATE INDEX owners_id_idx ON public.owners USING btree (id);\n")
    review = tmp_path / ".human-review"
    dg = review / "assets" / "diagrams"
    dg.mkdir(parents=True)
    svg = lambda p, t: p.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg"><text>{t}</text></svg>')
    svg(dg / "dm.new.svg", "domain picture")
    svg(dg / "dm.diff.svg", "domain delta")
    (dg / "MANIFEST.tsv").write_text(
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\tnew_svg\told_svg\n"
        "DomainModel\tdocs/DomainModel.puml\tstructural\tunchanged\tdm.diff.puml\t"
        "dm.diff.svg\t\tdm.new.svg\t\n")
    # The render `_context_svg` would cache, so no PlantUML has to run here.
    svg(review / "assets" / "DB.context.svg", "erd picture")
    content = {"title": "t", "summary": "<p>x</p>",
               "sections": [{"id": "s", "title": "S", "body": "<p>x</p>"}],
               "tabs": [{"id": "data", "label": "Data", "blocks": [
                            {"type": "diagrams", "only": ["DomainModel", "DB"]}]},
                        {"id": "other", "label": "Other",
                         "blocks": [{"type": "section", "id": "s"}]}]}
    src = review / "content.json"
    src.write_text(json.dumps(content))
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    proc = subprocess.run([sys.executable, str(HERE / "build-review-html.py"), str(src),
                           "--out", str(review / "review.html")],
                          cwd=tmp_path, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    page = (review / "review.html").read_text(encoding="utf-8")
    panel = page.split('id="data"', 1)[1].split("</section>", 1)[0]
    assert panel.index("<b>Domain Model</b>") < panel.index("<b>Database</b>"), "the asked order"
    # The Domain Model is UNCHANGED; the DB picture is too, over a schema that did change —
    # eval run 5's judges read UNCHANGED beside "DB.sql changed" as a contradiction, so
    # that card wears its own word.
    assert panel.count('<span class="badge sev-info">unchanged</span>') == 2
    assert ">schema only</span>" not in panel
    assert "domain picture" in panel and "erd picture" in panel
    assert "domain delta" not in panel and "dgmviews" not in panel
    assert "indexes added on owners (id)" in panel
    assert '<button type="button" class="tab quiet" role="tab" id="tabbtn-data"' not in page


# ── eval run 11: the Review tab, for a reviewer who is busy and stressed ────────────

STREAM_JAVA = """class C {
    List<Owner> load(List<Integer> ids) {
        return ids.stream()
                .map(id -> Optional.ofNullable(byId.get(id))
                        .orElseThrow(() -> new IllegalStateException("gone")))
                .toList();
    }
    int one() { return 1; }
}
"""


def test_a_one_line_anchor_on_the_tail_of_a_statement_quotes_the_whole_statement(tmp_path):
    """Run 11 pinned two findings to OwnerRestController.java:142 — `.toList();` — and the
    throwing `.orElseThrow(…)` on 141 was off the card."""
    (tmp_path / "C.java").write_text(STREAM_JAVA)
    assert build.widen_anchor("C.java:6", tmp_path) == "C.java:3-6"
    assert build.widen_anchor("C.java:4", tmp_path) == "C.java:3-6", "open both ways"
    assert build.widen_anchor("C.java:8", tmp_path) == "C.java:8", "a whole statement stays"
    assert build.widen_anchor("C.java:3-6", tmp_path) == "C.java:3-6", "a range is the author's"
    (tmp_path / "n.md").write_text("a,\n.b\n")
    assert build.widen_anchor("n.md:2", tmp_path) == "n.md:2", "only code files"
    chain = "x = a\n" + "".join("    .f()\n" for _ in range(20)) + "    .g();\n"
    (tmp_path / "L.java").write_text(chain)
    lo, hi = map(int, build.widen_anchor("L.java:22", tmp_path).split(":")[1].split("-"))
    assert hi == 22 and hi - lo + 1 == build.STATEMENT_LINES, "never more than six"


def test_a_long_quote_opens_on_its_anchored_lines_and_folds_the_rest(tmp_path):
    """Run 11: OwnerPageRequest.java:20 — a class opener — was closed down to its brace and
    drew all 41 lines. The card opens what the ref names and folds the rest."""
    body = "".join(f"    int f{i}() {{ return {i}; }}\n" for i in range(30))
    (tmp_path / "K.java").write_text("final class K {\n" + body + "}\n")
    card = build.snippet_card("K.java:1", None, tmp_path)
    shown, folded = card.split('<details class="snipmore">')
    assert '<span class="ln">1</span>' in shown and '<span class="ln">2</span>' not in shown
    assert "<summary>31 more lines</summary>" in folded and '<span class="ln">32</span>' in folded
    long = build.snippet_card("K.java:2-25", None, tmp_path)
    assert long.startswith('<details class="ghfold snipfold">'), "2+ lines fold"
    assert long.split('<details class="snipmore">')[0].count('class="ln-row') == build.SNIPPET_LINES
    assert "12 more lines" in long
    assert "snipmore" not in build.snippet_card("K.java:3-5", None, tmp_path)


def _rewrap_repo(tmp_path):
    """An implementation with two long lines, then a fix commit that only re-wraps them —
    a string split with `+` and a doc comment opened onto three lines."""
    git = _git_in(tmp_path)
    (tmp_path / "A.java").write_text(
        "class A {\n"
        "    /** Records every statement, once plugged in. */\n"
        "    String s = \"a long sentence here\";\n"
        "    int x = 1;\n"
        "}\n")
    git("add", ".")
    git("commit", "-qm", "impl")
    impl = git("rev-parse", "HEAD")
    (tmp_path / "A.java").write_text(
        "class A {\n"
        "    /**\n"
        "     * Records every statement,\n"
        "     * once plugged in.\n"
        "     */\n"
        "    String s = \"a long \"\n"
        "            + \"sentence here\";\n"
        "    int x = 1;\n"
        "}\n")
    git("add", ".")
    git("commit", "-qm", "[auto-fix] wrap")
    return impl, git("rev-parse", "HEAD")


def test_a_hook_fix_that_only_rewraps_lines_is_one_folded_line_labelled_hook(tmp_path):
    """Run 11: a pre-push hook's line-length fix drew 5 full diff blocks (~1,100px) under
    `Reviewer:`, though its source chip said `pre-push hook`."""
    impl, fix = _rewrap_repo(tmp_path)
    card = {"title": "Lines over 119 characters blocked the push", "source": "pre-push hook",
            "observation": "the hook refused two lines.", "fix": "wrapped each line",
            "refs": ["A.java:2-7"]}
    spec = {"autofixes": [card],
            "_reviewPoints": {"source": "review-points.md",
                              "provenance": {"implementation": impl, "reviewCommit": fix}}}
    build.attribute_fix_hunks(spec, tmp_path, root=tmp_path)
    assert card["_fixDiffs"].startswith(
        '<details class="fmtonly"><summary data-tip="wrapped each line">2 lines re-wrapped'
        '</summary><details class="ghfold">')
    page = build.render_autofixes([card], badge="fixed")
    assert "<b>Hook:</b> the hook refused" in page and "Reviewer:" not in page
    assert "f-fix" not in page, "the fix is the fold's hover"
    assert build.format_only_hunk(["a = 1;"], ["a = 2;"]) is None
    assert build.format_only_hunk(["  a(b, c);"], ["a(b,", "  c);"]) == (1, True)
    assert build.format_only_hunk(["  a();"], ["    a();"]) == (1, False)


def test_a_fix_line_that_only_restates_the_title_is_dropped():
    card = {"title": "Retry on a failed owners page", "fix": "retry on a failed page."}
    assert "f-fix" not in build.render_autofixes([card])
    card["fix"] = "a Retry button in the error alert reloads the same filter and sort."
    assert '<p class="f-fix"><b>Fix:</b> a Retry button' in build.render_autofixes([card])


RUN11_FINDINGS = [
    {"title": "Owner deleted between the page query and the graph fetch returns 500",
     "severity": "medium"},
    {"title": "Name sorts by last name while the cell shows First Last", "severity": "medium"},
    {"title": "Huge page index", "severity": "info", "why": "refuted — offset is capped"}]


def test_the_grade_panel_holds_six_lines_computed_first(capsys):
    """Run 11: nine bullets against the reference's five. Computed lines first, the
    informational ones (the spec commit, then a narrowed sentence) dropped past six — and
    the grader's own reason always keeps one line (runs 15, 17, 18 lost it to six computed
    lines, and with it the one risk behind the grade)."""
    sig = build._signal
    spec = {"verdict": {"score": 7, "bullets": ["<code>OwnerPageRequest</code> parses page=abc."]},
            "findings": RUN11_FINDINGS, "assumptions": [{"title": "x", "confidence": .5}],
            "_gradeSignals": [sig("ci-green", "CI green on 17ad7118", ""),
                              sig("api-breaking", "2 breaking API changes", "", 7),
                              sig("out-of-range", "2 commits on the branch before …", "", 7),
                              sig("spec-commit", "Built against the spec in b12c9bdb", ""),
                              sig("narrowed", "Ticket narrowed on purpose: “sortable”", "")]}
    short = [s for s, _ in build.grade_reasons(spec)]
    assert len(short) == build.GRADE_LINES_MAX == 6
    assert short[0].startswith("CI green") and "Built against the spec in b12c9bdb" not in short
    assert short[-1] == "OwnerPageRequest parses page=abc", "the model keeps one line"
    assert not any(s.startswith("Ticket narrowed") for s in short), "informational lines go first"
    spec["_gradeSignals"] = spec["_gradeSignals"][:2]
    assert [s for s, _ in build.grade_reasons(spec)][-1] == "OwnerPageRequest parses page=abc"


def test_a_model_line_that_repeats_names_nothing_or_misplaces_a_pile_is_dropped(capsys):
    """Run 11's two model lines: `GET /api/owners now answers {content…}` under the computed
    `2 breaking API changes`, and `Two declined items are product calls` over two items the
    page shows as open WORTH A LOOK — a word the page never uses, naming nothing visible."""
    spec = {"findings": RUN11_FINDINGS, "autofixes": [{"title": "Retry on a failed page"}],
            "_gradeSignals": [build._signal("api-breaking", "2 breaking API changes", "", 7)]}
    conflict = build.model_line_conflict
    assert "repeats the computed api-breaking line" in conflict(
        "GET /api/owners now answers <code>{content}</code> instead of an array.", spec)
    declined = ("Two declined items are product calls, not code defects: Name sorts by last "
                "name while the cell reads First Last, and an owner deleted between the page "
                "query and the graph fetch answers 500.")
    assert "and the page shows it open" in conflict(declined, spec)
    assert conflict("Nothing here worries me much.", spec) == \
        "it names no item, number, file or link"
    assert "repeats the computed count" in conflict("Leaves 2 open review issues.", spec)
    assert "shows it refuted" in conflict("Huge page index was fixed in the end.", spec)
    assert conflict("Retry on a failed page was fixed; the 500 is product's call.", spec) is None
    assert conflict("The stale-page race stays: see <code>load()</code>.", spec) is None


def _picked_repo(tmp_path):
    """`main` ← base; the feature branch carries two tooling commits before the reviewed
    range, and the first of them has since been cherry-picked onto origin/main."""
    git = _git_in(tmp_path)
    git("checkout", "-qb", "main")
    (tmp_path / "README").write_text("x\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")
    (tmp_path / "tool.sh").write_text("echo 1\n")
    git("add", ".")
    git("commit", "-qm", "tooling one")
    one = git("rev-parse", "HEAD")
    (tmp_path / "other.sh").write_text("echo 2\n")
    git("add", ".")
    git("commit", "-qm", "tooling two")
    audited = git("rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "feature")
    git("checkout", "-q", "main")
    # `-x`: a different message, so a different sha even within the same second (the
    # same parent, tree, author and timestamp would rebuild `one` itself).
    git("cherry-pick", "-x", one)
    git("update-ref", "refs/remotes/origin/main", "main")
    git("checkout", "-q", "feature")
    return one, audited


def test_commits_already_on_main_as_cherry_picks_are_not_counted_as_unreviewed(tmp_path):
    """Run 11: `7 commits never reviewed` and `+7▸` — 5 of them patch-identical to commits
    already on origin/main (`git cherry`). A merge brings none of them."""
    one, audited = _picked_repo(tmp_path)
    spec = {"_reviewPoints": {"provenance": {"auditedBase": audited}}}
    rows = build._before_range_commits(spec, tmp_path, "origin/main")
    assert [c["subject"] for c in rows] == ["tooling two"]
    sig = build._out_of_range_signal(spec, tmp_path, "origin/main", tmp_path)
    assert sig["short"] == "1 commit on the branch before the reviewed range"
    out = tmp_path / ".human-review"
    out.mkdir()
    (out / "review-points.json").write_text(json.dumps(
        {"provenance": {"auditedBase": audited}}))
    state = build.page_base(tmp_path, out, "main")
    assert [c.get("onBase", False) for c in state["outside"]] == [False, True]
    badge = build.outside_badge(state)
    assert ">+1<" in badge and "1 more already on origin/main." in badge
    assert "Already on origin/main (cherry-picked)" in build.outside_note(state)
    # The cherry-pick is origin/main's only commit the branch lacks: no drift to merge in.
    assert state["ahead"] == 0 and state["aheadPicked"] == 1
    assert build.base_warning(state) is None
    assert build.fetch_base(tmp_path, "main") is False, "no network remote, no fetch"


def test_a_capabilitys_spec_cited_by_its_path_is_linked_like_its_siblings(tmp_path):
    """Run 11's CONTEXT card left `specs/owner-list/spec.md:123` plain beside linked
    design.md citations."""
    _spec_repo(tmp_path)
    cap = tmp_path / "openspec" / "changes" / "page-owners" / "specs" / "owner-list"
    cap.mkdir(parents=True)
    (cap / "spec.md").write_text("# Owner list\n- The paginator stays to go back.\n")
    item = {"title": "t", "severity": "info", "why": "specified: specs/owner-list/spec.md:2."}
    assert build.link_spec_citations({"findings": [item]}, tmp_path, root=tmp_path) == 1
    assert 'class="specref"' in item["why"] and "The paginator stays to go back." in item["why"]
