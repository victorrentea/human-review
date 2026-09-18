#!/usr/bin/env python3
"""One renderer for every command the page prints, and what it promises in each copy.

The page is read in two copies, and it used to say two different things about a command
depending on which one you had. Off disk it showed the line; served, it hid the line and
showed a button. Both halves were defensible on their own and together they meant the
reader could not learn the page: the same control was a sentence in one copy and a verb in
the other, and "where is the command this button runs" had no answer at all in the copy
where the button worked.

So the arrangement is now the same in both and it is three things in a fixed order — the
words, the command in a parenthesis with a copy glyph, and a play glyph that appears only
where it can run. This file pins that, and pins the one thing about it that is not
symmetrical: what a click on the words does.

Run with:  python3 -m pytest test_command_html.py
"""
from __future__ import annotations

import html
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

from conftest import page_source

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load("build_review_cmd", "build-review-html.py")


# --------------------------------------------------------------------------- #
# the parenthesis
# --------------------------------------------------------------------------- #

def test_the_command_is_not_printed_on_the_page():
    """It was, in a parenthesis, and it was the right instinct and the wrong artifact: a
    review page is prose and pictures, and a two-hundred-character absolute path in the
    middle of a sentence is a wall the eye climbs on every read — charged to all ten
    readers for the one who wanted to paste it."""
    cmd = "cd /Users/somebody/workspace/project && git revert --no-commit abc12345"
    out = build.command_html(cmd)
    assert "<code>" not in out
    assert f">{cmd}<" not in out
    assert "cmd-p" not in out, "no parentheses left to put it in either"


def test_the_command_is_in_the_copy_glyphs_hover():
    """Where the reader who wants to check the line before pasting it looks — and the only
    place on the page it appears in full. Not truncated: half a command in a tooltip is
    worse than none, because the reader cannot tell which half they have."""
    cmd = "cd /repo && make && ./build.sh --flag"
    tip = re.search(r'class="copycmd cmd-copy[^"]*"[^>]*data-tip="([^"]*)"',
                    build.command_html(cmd)).group(1)
    assert tip.startswith(build.COPY_TIP + ":")
    assert html.unescape(tip).endswith(cmd)
    assert build.COPY_TIP == "Copy command to paste in terminal"


def test_the_copy_glyph_is_there_in_every_copy_of_the_report():
    """The route every copy can honour, including off disk, out of the zip and on Pages."""
    out = build.command_html("git status")
    assert build.CMD_COPY in out
    assert 'data-copy="git status"' in out
    assert " hidden " not in out[:out.index(build.CMD_COPY)]


def test_a_command_with_no_action_behind_it_gets_no_play():
    """The honest rendering of a command the build did not declare: the line is real, and
    nothing on this page can run it. A play glyph there would be a lie in one character."""
    out = build.command_html("git status")
    assert build.CMD_PLAY not in out
    assert "cmd-run" not in out


def test_the_run_mark_is_a_play_and_never_the_rerun_arrow():
    """`\u21bb` belongs to the masthead badge, where it means "this page can rebuild itself",
    and it is green because that is what `served` is coloured. On a row of actions it was
    wrong twice: it said *rerun* over verbs that are not reruns of anything — Start, Stop,
    Where — and green on this page means a check passed, so controls turning green when a
    server happens to be up read as the branch being fine.

    The play is drawn in `--accent`, the colour everything pressable here already wears,
    and as one variable rather than a pair of hard-coded greens with a dark-mode rule
    underneath."""
    out = build.command_html("git status", "some-action", label="Do it")
    assert build.CMD_PLAY in out
    assert build.CMD_RUN not in out, "the rerun arrow is the masthead's, not an action's"
    css = build.CSS
    # The second `.cmd .cmd-run {` is the colour rule; the first sets the pill both faces
    # share. What matters is that the accent is a variable and that neither green survives.
    at = css.index(".cmd .cmd-run {", css.index(".cmd .cmd-run {") + 1)
    rule = css[at:css.index("}", at)]
    assert "color:var(--accent)" in rule and "border-color:var(--accent)" in rule
    block = css[css.index(".cmd { display:inline-flex"):css.index(".rerun a,")]
    assert "#2e7d32" not in block and "#6bd48a" not in block, \
        "the greens are gone from the command pill; they belong to a passing check"
    # The masthead keeps it, and that is the one place it means what it draws.
    assert build.CMD_RUN in build.RERUN_CHIP


def test_the_run_glyph_ships_hidden_and_is_raised_by_the_probe():
    """Every control on this page starts degraded and rises. A glyph drawn live that turns
    out not to apply has already been clicked by the time the probe corrects it — and in
    the static copy nothing ever raises it, which is how `file://` gets a clipboard and no
    run glyph without the build knowing which copy it is writing."""
    out = build.command_html("git status", "some-action")
    tag = out[out.index('class="runhere cmd-run'):]
    assert " hidden " in tag
    assert 'data-action="some-action"' in out
    assert "if (!b.classList.contains('cmd-run')) return;" in build.SERVER_JS
    assert "b.hidden = false;" in build.SERVER_JS


def test_exactly_one_glyph_is_ever_on_screen():
    """Never both. They were both visible on a served page and the pair asked the reader a
    question the page already knew the answer to — one runs the command here, the other
    hands over a line to run somewhere else, and neither glyph said which was which until
    it had been pressed. The clipboard is the mark of a copy that cannot run; the run glyph
    is the mark of one that can; raising the second takes the first away."""
    out = build.command_html("git status", "some-action")
    # Both in the markup — the build does not know which copy it is writing …
    assert 'class="copycmd cmd-copy"' in out and 'class="runhere cmd-run"' in out
    # And with a label on it, the same pair with the word on both faces — never a word on
    # one of them and a bare mark on the other, which is the arrangement this replaced.
    wordy = build.command_html("git status", "some-action", label="Do it")
    assert wordy.count('<span class="cmd-word">Do it</span>') == 2
    # … and the probe is what makes it one, in the same breath as raising the run glyph.
    js = build.SERVER_JS
    at = js.index("if (!b.classList.contains('cmd-run')) return;")
    raised = js[at:at + 400]
    assert "b.closest('.cmd')" in raised
    assert ".querySelector('.cmd-copy')" in raised
    assert "clip.hidden = true" in raised


def test_the_play_says_what_it_does_in_one_line_and_does_not_repeat_the_command():
    """A short hover, because the label under the pointer already says what this is.

    It used to end with the whole command — a forty-to-two-hundred-character absolute path
    in a tooltip over a button whose words say *Update the report*. The clipboard face is
    where a reader opens a hover to read a line, because that is the face that hands them
    one; this face runs it, and what it owes the reader is *where*."""
    tip = html.unescape(re.search(r'class="runhere cmd-run[^"]*"[^>]*data-tip="([^"]*)"',
                        build.command_html("make all", "a", label="Update the report",
                                           tip="")).group(1))
    assert tip == "Update the report \u2014 runs on the server serving this page"
    assert "make all" not in tip
    # And a caller with something a label cannot carry — that a press throws work away —
    # says it here instead, still in one line.
    other = html.unescape(re.search(r'class="runhere cmd-run[^"]*"[^>]*data-tip="([^"]*)"',
                          build.command_html("make all", "a", label="Regenerate",
                                             tip="Regenerate \u2014 banks your layout")).group(1))
    assert other == "Regenerate \u2014 banks your layout"


def test_the_copy_target_and_the_hover_are_the_same_string():
    """Two renderings of one command is how the copied one quietly stops matching the read
    one — and the copied one is the only one that gets run."""
    cmd = 'sh -c "echo <hi> & bye"'
    out = build.command_html(cmd)
    copied = html.unescape(re.search(r'data-copy="(.*?)"', out, re.S).group(1))
    hover = html.unescape(re.search(r'data-tip="(.*?)"', out, re.S).group(1))
    assert copied == cmd
    assert hover.endswith(cmd)


def test_the_markup_is_escaped_rather_than_trusted():
    out = build.command_html('echo "<script>x</script>"')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_nothing_folds_a_command_any_more():
    """The fold under each diagram held nothing but the command, and its `&&` chains were
    the longest lines on the page by a factor of five — in a box the reader had to open,
    for the sake of a paste."""
    src = page_source()
    assert "_cmdfold" not in src
    assert "cmdline" not in src
    # The class survives in one comment saying why it is gone; no markup emits it.
    assert "class=\"cmdpeek" not in src and "button.cmdpeek" not in src


def test_one_renderer_and_not_a_copy_per_caller():
    """The point of the function. Several places offer a command — the aftermath band's
    regenerate, the Demo row, the diagram offers — and one more would have been one more
    set of affordances behaving almost the same."""
    src = page_source()
    body = src[src.index("def command_html("):]
    body = body[:body.index("\ndef ", 1)]
    assert "CMD_PLAY" in body and "CMD_COPY" in body
    # The glyphs reach a button in exactly one place, bar the masthead chips, which name
    # the constant rather than retyping the character. (The play triangle still marks the
    # browser tab of a served page — a different statement, in the title, not on a
    # control — so counting characters alone would count that too.)
    for glyph in (build.CMD_RUN, build.CMD_COPY, build.CMD_PLAY):
        for at in [m for m in range(len(src)) if src.startswith(glyph, m)]:
            line = src[src.rindex("\n", 0, at) + 1:src.index("\n", at)]
            assert "<button" not in line or "CMD_" in line, \
                f"a glyph is being drawn on a button outside command_html: {line.strip()}"
    for caller in ("_regenerate_offer", "runtime_html", "rerun_html", "regenerate_html"):
        b = src[src.index(f"def {caller}("):]
        b = b[:b.index("\ndef ", 1)]
        assert "command_html(" in b, f"{caller} is drawing its own command"


# --------------------------------------------------------------------------- #
# the words and the mark are one control
# --------------------------------------------------------------------------- #

def test_the_words_and_the_mark_are_the_same_button():
    """What this replaces: a grey pill reading *Update the report* and, after it, a separate
    glyph. Two elements for one action — so a reader who pressed one had no way to know the
    other did the same thing, and the row under a diagram carried four controls for two
    offers. The label is on the button now, and the mark is on the same button."""
    out = build.command_html("git status", "some-action", label="Update the report")
    assert out.count("<button") == 2, "one pair, not a pill plus a pair"
    assert "offer-pill" not in out and "offer-words" not in out
    assert not hasattr(build, "offer_words_html"), \
        "the second renderer is gone; there is one way to draw a command"
    for face in re.findall(r"<button.*?</button>", out, re.S):
        assert face.count('<span class="cmd-word">') == 1
        assert face.count('<span class="cmd-ico">') == 1


def test_the_word_leads_and_the_mark_follows_it():
    """The label is what the reader is looking for; the mark is the footnote saying what a
    press will do. It is also the order the two-element row already read in, so nothing
    about where to look changed when the second element went away."""
    out = build.command_html("git status", "some-action", label="Start")
    face = re.search(r'class="runhere cmd-run.*?</button>', out, re.S).group(0)
    assert face.index('class="cmd-word"') < face.index('class="cmd-ico"')
    assert "margin-left:.35rem" in build.CSS or "margin-left:.32rem" in build.CSS


def test_off_disk_a_click_on_either_half_copies_the_command():
    """Off disk the click *is* the copy — it is the one thing that copy of the report can do
    with the command, so it is what the click does. A control whose whole answer is a
    sentence explaining why it did nothing is a control the reader stops pressing. And there
    is no half of the control that does something else: the word and the mark are one
    button, so there is nothing to press by mistake."""
    out = build.command_html("git revert abc", "act", label="Revert")
    copy_face = re.search(r'<button type="button" class="copycmd.*?</button>', out, re.S).group(0)
    assert 'data-copy="git revert abc"' in copy_face
    assert build.COPY_TIP in copy_face
    assert "if (runhere && !cmd.getAttribute('data-copy'))" in build.EDITOR_JS
    assert "cannot run it, so run it in a terminal" in build.EDITOR_JS


def test_the_mark_spins_rather_than_the_label_becoming_a_word():
    """'Running\u2026' over `Update the report` would reflow the row it sits in and take the
    label away from the one control that says what is running. The mark turns; the sentence
    goes to the status line and the toast."""
    assert "button.classList.add('running');" in build.EDITOR_JS
    assert "button.textContent = 'Running" not in build.EDITOR_JS
    assert "button.textContent = 'Done'" not in build.EDITOR_JS
    assert ".cmd .cmd-run.running { animation:hrspin" in build.CSS
    assert ".cmd .cmd-run.has-word.running .cmd-ico { animation:hrspin" in build.CSS


def test_both_faces_of_every_offer_in_a_block_go_down_together():
    """They run one command over one working tree, and a second press while the first is
    going is a reader who could not tell it had started. Both faces, because the probe
    decides which one is up and a disabled play beside a live clipboard for the same
    command is the pair this page spent three commits getting rid of."""
    assert "box.querySelectorAll('.cmd-run, .cmd-copy')" in build.EDITOR_JS
    assert ".offer-pill" not in build.EDITOR_JS


# --------------------------------------------------------------------------- #
# one string, two surfaces
# --------------------------------------------------------------------------- #

def test_the_line_on_the_clipboard_is_the_line_out_of_the_register():
    """The property the whole indirection exists for. `command_html` does not print what it
    was handed when the register has an entry for the id — it prints the register's, which
    is the string `serve-review.py` hands to `sh -c`.

    It had drifted, in the one place it mattered most: the aftermath band composed
    `cd <repo> && refresh-report.py --dir \u2026 --steps static` for the clipboard while the
    server ran the same program through a different interpreter with `--no-serve` on the
    end. Two authors, one command, and no test could have caught it while both halves were
    written twice."""
    build.ACTIONS.clear()
    real = "cd /repo && /usr/bin/python3 /skill/refresh-report.py --dir . --no-serve"
    build.declare_action("some-action", real)
    out = build.command_html("a stale copy of the command", "some-action")
    assert "stale" not in out
    for shown in re.findall(r'data-(?:copy|cmd)="(.*?)"', out, re.S):
        assert html.unescape(shown) == real
    assert html.unescape(re.search(r'data-tip="(.*?)"', out, re.S).group(1)).endswith(real)


def test_an_undeclared_command_still_renders_the_line_it_was_given():
    """No id, or an id this build did not declare: there is nothing in the register to read,
    the line is real, and nothing on this page can run it. A clipboard and no play."""
    build.ACTIONS.clear()
    out = build.command_html("git status", "never-declared")
    assert 'data-copy="git status"' in out
    assert 'data-cmd="git status"' in out


def test_every_command_on_a_built_page_is_the_register_s_own(tmp_path):
    """The guardrail over the page rather than over the function: walk every control a real
    build emitted, ask it what line it stands for, and compare with the manifest the server
    runs out of. `data-cmd` is on the markup for exactly this, and this is the test the
    coordinator asked for — it fires on a page, not on a call."""
    page = Path(os.environ.get("HUMAN_REVIEW_PAGE",
                "/Users/victorrentea/workspace/petclinic-pr/.human-review/review.html"))
    manifest = page.parent / ".actions.json"
    if not page.is_file() or not manifest.is_file():
        pytest.skip("no built page beside this checkout to walk")
    # The two files are written by one build, seconds apart, and the directory is live —
    # another session rebuilding it while this reads would pair a page with a manifest from
    # a different run and report a drift that never existed. Mismatched mtimes mean there
    # is nothing here to compare, not that the property failed.
    if abs(page.stat().st_mtime - manifest.stat().st_mtime) > 120:
        pytest.skip("the page and the manifest are not from the same build")
    html_text = page.read_text(encoding="utf-8")
    if abs(page.stat().st_mtime - manifest.stat().st_mtime) > 120:
        pytest.skip("the directory was rebuilt underneath this read")
    declared = json.loads(manifest.read_text(encoding="utf-8"))["actions"]
    seen = 0
    for tag in re.findall(r"<button[^>]*data-cmd=[^>]*>", html_text):
        shown = html.unescape(re.search(r'data-cmd="(.*?)"', tag, re.S).group(1))
        action = re.search(r'data-action="([^"]*)"', tag)
        if not action:
            continue
        entry = declared.get(action.group(1))
        assert entry, f"{action.group(1)} is offered on the page and not declared"
        assert shown == entry["command"], \
            f"{action.group(1)} copies a different line than the server runs"
        seen += 1
    assert seen, "the page carries no runnable command at all"


def test_the_glyphs_are_tooltipped_the_pages_own_way():
    """`data-tip`, never a native `title`: the native tooltip cannot be styled and arrives
    after half a second, by which time the reader has read the icon and moved on."""
    out = build.command_html("x", "a")
    assert "title=" not in out
    assert out.count("data-tip=") == 2


def test_the_glyphs_are_dressed_as_the_pages_other_pills():
    assert ".cmd { display:inline-flex;" in build.CSS
    assert ".cmd .cmd-copy, .cmd .cmd-run {" in build.CSS
    assert "border-radius:999px" in build.CSS[build.CSS.index(".cmd .cmd-copy, "):][:400]
