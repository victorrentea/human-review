#!/usr/bin/env python3
"""The one arithmetic in the video pipeline that can go wrong silently.

`record-feature-video.sh` opens the film on a title card, filmed inside the take. Because the
card is on the same clock as everything else, no timestamp needs shifting — which is the whole
argument for filming it rather than splicing it on. The single exception is the first cue:
it is deliberately placed at the first frame it is *allowed* on rather than at its own `t`, so
that the opening shot is not silent, and "the first frame" would be the title card.

That failure is invisible in every check short of watching the film — the caption is right, the
voice is right, the picture is right, they are just over the wrong shot — so it is pinned here.

Run with:  python3 -m pytest test_feature_video_lead.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "annotate_feature_video", HERE / "annotate-feature-video.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


annotate = _load()
CUES = [{"t": 4.9}, {"t": 10.1}, {"t": 15.4}]


def test_without_a_title_card_the_first_cue_still_opens_the_film():
    windows = annotate.cue_windows(CUES, duration=20.0)
    assert windows[0] == (4.9 - annotate.OPENING_GRACE, 10.1)


def test_a_slow_first_page_load_cannot_drag_the_opening_line_over_a_blank_screen():
    """A cold lazy-route compile once put the first cue 10s in; the opening sentence was then
    spoken over ten seconds of loading screen."""
    windows = annotate.cue_windows([{"t": 13.5}, {"t": 20.5}], duration=42.0, lead=3.4)
    assert windows[0][0] == pytest.approx(13.5 - annotate.OPENING_GRACE)


def test_a_cue_that_lands_inside_the_grace_still_opens_at_the_card():
    windows = annotate.cue_windows([{"t": 4.0}, {"t": 9.0}], duration=20.0, lead=3.4)
    assert windows[0][0] == 3.4, "never earlier than the title card"


def test_a_title_card_holds_the_first_caption_and_its_voice_off_the_screen():
    windows = annotate.cue_windows([{"t": 4.0}, {"t": 10.1}], duration=20.0, lead=3.4)
    assert windows[0][0] == 3.4, "the opening caption would be printed over the title"


def test_no_offset_can_drag_a_cue_in_front_of_the_card():
    windows = annotate.cue_windows(CUES, duration=20.0, offset=-9.0, lead=3.4)
    assert [w[0] for w in windows] == [3.4, 3.4, 6.4]


def test_windows_never_run_backwards_or_past_the_footage():
    windows = annotate.cue_windows(CUES, duration=12.0, lead=3.4)
    assert all(start <= end <= 12.0 for start, end in windows)


def test_a_lead_longer_than_the_footage_is_clamped_not_trusted():
    windows = annotate.cue_windows(CUES, duration=6.0, lead=99.0)
    assert all(start == 6.0 and end == 6.0 for start, end in windows)


def test_the_opening_caption_never_precedes_the_title_card():
    for t in (0.5, 3.0, 4.0, 12.0):
        windows = annotate.cue_windows([{"t": t}, {"t": 30.0}], duration=40.0, lead=3.4)
        assert windows[0][0] >= 3.4


@pytest.mark.parametrize("lead", [0.0, -1.0])
def test_absent_or_nonsense_lead_reproduces_the_pre_title_behaviour(lead):
    assert annotate.cue_windows(CUES, duration=20.0, lead=lead) == \
        annotate.cue_windows(CUES, duration=20.0)
