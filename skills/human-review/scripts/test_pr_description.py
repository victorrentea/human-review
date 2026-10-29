#!/usr/bin/env python3
"""The PR's own description on the Review tab: fetched, cached, rendered safely.

Run with:  python3 -m pytest test_pr_description.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from hrbuild.tabs import review  # noqa: E402

URL = "https://github.com/acme/shop/pull/51"


def test_markdown_is_escaped_and_linked():
    out = review.pr_body_html("Closes #25.\n\n- [x] **paged** grid\n- see "
                              "[spec](https://x.io/s) <script>alert(1)</script>\n\n"
                              "```\nif (a < b) {}\n```", "https://github.com/acme/shop")
    assert '<a href="https://github.com/acme/shop/issues/25"' in out
    assert "<li>☑ <b>paged</b> grid</li>" in out
    assert '<a href="https://x.io/s"' in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<pre><code>if (a &lt; b) {}</code></pre>" in out


def test_a_commit_number_is_not_an_issue_link():
    out = review.pr_body_html("Commit #1 is the code, commits #2 the fixes; closes #3.",
                              "https://github.com/acme/shop")
    assert "issues/1" not in out and "issues/2" not in out
    assert "Commit #1" in out and "commits #2" in out
    assert '<a href="https://github.com/acme/shop/issues/3"' in out


def test_the_agent_attribution_line_is_left_out():
    spec = {"_prBody": {"url": URL, "body": "Closes #25.\n\n🤖 Generated with "
                                             "[Claude Code](https://claude.com/claude-code)"}}
    out = review.pr_description_html(spec)
    assert "Closes" in out and "Generated with" not in out
    assert f'href="{URL}"' not in out   # the masthead's PR#N already links there


def test_an_empty_description_draws_nothing():
    assert review.pr_description_html({}) == ""
    assert review.pr_description_html({"_prBody": {"url": URL, "body": "  \n"}}) == ""


def test_offline_the_cache_stands_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_NO_GITHUB", "1")
    (tmp_path / review.PR_BODY_JSON).write_text(json.dumps(
        {"number": 51, "url": URL, "body": "cached words"}))
    spec = {"pr": {"number": 51, "url": URL}}
    review.attach_pr_body(spec, tmp_path)
    assert spec["_prBody"]["body"] == "cached words"
    other = {"pr": {"number": 52, "url": URL.replace("51", "52")}}
    review.attach_pr_body(other, tmp_path)
    assert "_prBody" not in other


def test_merge_risk_closes_the_pr_description_with_its_door_and_blast_radius():
    """Victor, 29 Oct 2026, after Matt Pocock's "merge danger": one-way or two-way door,
    and how far it reaches."""
    spec = {"_prBody": {"url": URL, "body": "Closes #37."},
            "mergeRisk": {"door": "two-way", "blast": "visits API", "why": "a PUT <b> clears"}}
    out = review.pr_description_html(spec)
    assert out.index("Closes") < out.index('class="mergerisk"') < out.index("</details>")
    assert '<b class="mr-door">✅ Two-way door</b> · blast radius: <b>visits API</b>' in out
    assert "a PUT &lt;b&gt; clears" in out
    assert 'mr-oneway">⚠️ One-way door' in review.merge_risk_html({"mergeRisk": {"door": "One-way"}})


def test_merge_risk_without_a_known_door_is_not_drawn_and_stands_alone_without_a_body():
    assert review.merge_risk_html({"mergeRisk": {"door": "maybe", "blast": "x"}}) == ""
    assert review.merge_risk_html({}) == ""
    alone = review.pr_description_html({"mergeRisk": {"door": "two-way"}})
    assert alone.startswith('<p class="mergerisk">') and "prdesc" not in alone


def test_the_blast_radius_wears_a_circle_that_fills_with_its_size():
    """Victor, 29 Oct 2026: ○ ◔ ◑ ◕ ●, so a reader knows at a glance what to expect."""
    spec = {"mergeRisk": {"door": "two-way", "blast": "visits API", "blastSize": 1}}
    out = review.merge_risk_html(spec)
    assert ('blast radius: <span class="mr-size" data-tip="Blast radius: visits API — about '
            'a quarter of the system"') in out
    assert "◔</span> <b>visits API</b>" in out
    assert [review.BLAST_GLYPHS[review.blast_size({"mergeRisk": {"blastSize": n}})]
            for n in (0, "2", 3, 4)] == ["○", "◑", "◕", "●"]
    # No size, or one off the scale: the words alone, no guessed circle.
    for bad in (None, 5, -1, "big", True):
        assert review.blast_size({"mergeRisk": {"blastSize": bad}}) is None
    assert "mr-size" not in review.merge_risk_html({"mergeRisk": {"door": "two-way",
                                                                  "blast": "visits API"}})
    # A size with no words says the size.
    alone = review.merge_risk_html({"mergeRisk": {"door": "one-way", "blastSize": 4}})
    assert "●</span> <b>the whole system</b>" in alone


def test_the_masthead_draws_the_circle_beside_the_grade_with_its_meaning_on_hover():
    out = review.blast_glyph_html({"mergeRisk": {"blast": "visits API", "blastSize": 1}},
                                  "titleblast")
    assert out.startswith('<span class="titleblast" data-tip="Blast radius: visits API — '
                          'about a quarter of the system"') and out.endswith("◔</span>")
    assert review.blast_glyph_html({"mergeRisk": {"blast": "visits API"}}) == ""


def test_the_masthead_shows_the_doors_mark_before_the_circle_with_its_meaning_on_hover():
    """Victor, 29 Oct 2026: the green check beside the grade too — one table for both."""
    two = review.door_mark_html({"mergeRisk": {"door": "Two-way", "why": "a revert stops it"}})
    assert 'data-tip="Two-way door: a revert simply undoes it\na revert stops it"' in two
    assert two.endswith(f"{review.MERGE_DOOR_MARKS['two-way']}</span>") and "✅" in two
    assert "⚠️</span>" in review.door_mark_html({"mergeRisk": {"door": "one-way"}})
    assert review.door_mark_html({}) == "" == review.door_mark_html({"mergeRisk": {"door": "?"}})
    # The MERGE RISK line draws the same mark from the same table.
    assert f"{review.MERGE_DOOR_MARKS['two-way']} Two-way door</b>" in \
        review.merge_risk_html({"mergeRisk": {"door": "two-way"}})
