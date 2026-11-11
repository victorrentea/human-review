#!/usr/bin/env python3
"""Nothing the skill ships knows where the demos' planted decisions live.

`demo/slop/` holds intentional mistakes — decisions a hurried human "approved" — that the
demo runner hands to the implementing agent, so the two published demos have something
for every tab to catch. A clean `/record-review` or `/human-review` run must never see
them. The runner reaches into `skills/`, never the other way round; this fails the day a
file under `skills/` names that folder, so the decisions could not leak into a real run.

Run with:  python3 -m pytest test_demo_slop_guard.py
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve()
SKILLS = HERE.parents[2]                      # skills/: human-review and record-review
#: Spelled in two halves so this file does not trip its own check.
NEEDLES = ("demo" + "/slop", "slop" + "/run.sh", "slop" + "/check.py")


def test_skills_never_name_the_planted_decisions():
    hits = []
    for p in SKILLS.rglob("*"):
        if not p.is_file() or p == HERE or "node_modules" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{p.relative_to(SKILLS)}: {n}" for n in NEEDLES if n in text]
    assert not hits, "skills/ must not read or mention the demo's planted decisions:\n" + "\n".join(hits)


def test_the_planted_decisions_exist_where_the_guard_expects_them():
    # A guard over a folder that moved guards nothing.
    slop = SKILLS.parent / "demo" / "slop"
    assert (slop / "README.md").is_file() and (slop / "run.sh").is_file()
