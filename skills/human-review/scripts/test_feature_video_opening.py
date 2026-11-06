#!/usr/bin/env python3
"""The film opens on its first spoken line, on the first frame.

A reviewer gives a film two seconds. Silence at the head is invisible in every check short of
watching the film (Victor, 6 Nov 2026: a silent title card, then a page loading, then the
story), so the arithmetic that removes it is pinned here, with the rule the script is written to.

Run with:  python3 -m pytest test_feature_video_opening.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


annotate = _load("annotate_feature_video", "annotate-feature-video.py")
cut_idle = _load("cut_idle", "cut-idle.py")
CUES = [{"t": 0.0}, {"t": 10.1}, {"t": 15.4}]


def test_everything_before_the_first_line_is_cut_and_it_lands_on_frame_zero():
    cuts = cut_idle.merged([[0, 4.09], [11.0, 12.0]], duration=60.0)
    assert cuts[0] == (0.0, 4.09)
    assert cut_idle.remap(4.09, cuts) == 0.0
    assert cut_idle.remap(10.09, cuts) == 6.0


def test_the_recorder_cuts_up_to_the_first_cue():
    recorder = (HERE / "record-feature-video.sh").read_text(encoding="utf-8")
    assert "if (!cues.length) idleSpans.push([0, cue.t]);" in recorder


def test_the_first_window_opens_the_film():
    assert annotate.cue_windows(CUES, duration=20.0) == [
        (0.0, 10.1), (10.1, 15.4), (15.4, 20.0)]


def test_a_window_stays_on_the_footage():
    assert annotate.cue_windows(CUES, duration=12.0)[-1] == (12.0, 12.0)
    assert annotate.cue_windows(CUES, duration=20.0, offset=-9.0)[0][0] == 0.0


def test_both_script_writers_are_told_to_open_cold():
    for name in ("feature-script.md", "film-prompt.md"):
        guide = (HERE.parent / "reference" / name).read_text(encoding="utf-8")
        assert "Open cold" in guide, name
