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
import re
from pathlib import Path

import pytest

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
    tip = re.search(r'class="copycmd cmd-copy"[^>]*data-tip="([^"]*)"',
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
    assert build.CMD_RUN not in out
    assert "cmd-run" not in out


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
    # … and the probe is what makes it one, in the same breath as raising the run glyph.
    js = build.SERVER_JS
    at = js.index("if (!b.classList.contains('cmd-run')) return;")
    raised = js[at:at + 400]
    assert "b.closest('.cmd')" in raised
    assert ".querySelector('.cmd-copy')" in raised
    assert "clip.hidden = true" in raised


def test_the_play_says_what_it_will_run_and_where():
    """"Run it here" leaves out both halves a reader is asking about: *what*, and *which*
    here. The answer is the caller's sentence, then the server that served them the page,
    then the line."""
    tip = html.unescape(re.search(r'class="runhere cmd-run"[^>]*data-tip="([^"]*)"',
                        build.command_html("make all", "a",
                                           tip="Rebuilds the picture")).group(1))
    assert tip.startswith("Rebuilds the picture.")
    assert "server serving this page" in tip
    assert tip.endswith("make all")


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
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    assert "_cmdfold" not in src
    assert "cmdline" not in src
    # The class survives in one comment saying why it is gone; no markup emits it.
    assert "class=\"cmdpeek" not in src and "button.cmdpeek" not in src


def test_one_renderer_and_not_a_copy_per_caller():
    """The point of the function. Several places offer a command — the aftermath band's
    regenerate, the Demo row, the diagram offers — and one more would have been one more
    set of affordances behaving almost the same."""
    src = (HERE / "build-review-html.py").read_text(encoding="utf-8")
    body = src[src.index("def command_html("):]
    body = body[:body.index("\ndef ", 1)]
    assert "{CMD_RUN}" in body and "{CMD_COPY}" in body
    # The glyphs reach a button in exactly one place, bar the masthead chips, which name
    # the constant rather than retyping the character. (The play triangle still marks the
    # browser tab of a served page — a different statement, in the title, not on a
    # control — so counting characters alone would count that too.)
    for glyph in (build.CMD_RUN, build.CMD_COPY):
        for at in [m for m in range(len(src)) if src.startswith(glyph, m)]:
            line = src[src.rindex("\n", 0, at) + 1:src.index("\n", at)]
            assert "<button" not in line or "CMD_" in line, \
                f"a glyph is being drawn on a button outside command_html: {line.strip()}"
    for caller in ("_regenerate_offer", "runtime_html", "rerun_html"):
        b = src[src.index(f"def {caller}("):]
        b = b[:b.index("\ndef ", 1)]
        assert "command_html(" in b, f"{caller} is drawing its own command"


# --------------------------------------------------------------------------- #
# the words beside it
# --------------------------------------------------------------------------- #

def test_the_words_are_a_live_control_in_both_copies():
    out = build.offer_words_html("revert it", "act", "static", "served", cmd="git revert")
    assert ">revert it</button>" in out
    assert 'data-action="act"' in out
    assert 'data-copy="git revert"' in out
    assert 'data-tip-served="served"' in out


def test_off_disk_a_click_on_the_words_copies_the_command():
    """The clarification that this whole arrangement turns on. Off disk the click *is* the
    copy — it is the one thing this copy of the report can do with the command, so it is
    what the click does. A control whose whole answer is a sentence explaining why it did
    nothing is a control the reader learns to stop pressing."""
    assert "if (runhere && !cmd.getAttribute('data-copy'))" in build.EDITOR_JS
    assert "cannot run it, so run it in a terminal" in build.EDITOR_JS
    tip = re.search(r'data-tip="([^"]*)"',
                    build.offer_words_html("x", "a", "It stages an inverse.", "s",
                                           cmd="git revert")).group(1)
    assert "It stages an inverse." in tip
    assert "clicking here copies the command" in tip


def test_the_words_say_nothing_about_copying_when_they_carry_no_command():
    """A tooltip that promises a copy the element cannot make is worse than a short one."""
    tip = re.search(r'data-tip="([^"]*)"',
                    build.offer_words_html("x", "a", "Does a thing.", "s")).group(1)
    assert tip == "Does a thing."


def test_the_glyph_spins_rather_than_becoming_a_word():
    """A play glyph is a pill one character wide. 'Running…' in it would reflow the line it
    sits in, and 'Done' would leave a word where the reader learnt to find a mark."""
    assert "var glyph = button.classList.contains('cmd-run');" in build.EDITOR_JS
    assert "if (glyph) button.classList.add('running');" in build.EDITOR_JS
    assert ".cmd .cmd-run.running { animation:hrspin" in build.CSS
    assert "@media (prefers-reduced-motion:reduce) { .cmd .cmd-run.running" in build.CSS


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
