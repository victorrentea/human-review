#!/usr/bin/env python3
"""The PR's own description on the Review tab: fetched, cached, rendered safely.

Run with:  python3 -m pytest test_pr_description.py
"""
from __future__ import annotations

import importlib.util
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


def test_the_attribution_line_naming_the_model_and_effort_is_left_out_too():
    """`harness_cost.pr_footer`'s line: the same attribution, the coder's model and effort
    after it. The page says who coded elsewhere (the review chip, the `$` tab)."""
    spec = {"_prBody": {"url": URL, "body": "Closes #25.\n\n🤖 Generated with "
                                             "[Claude Code](https://claude.com/claude-code)"
                                             " · Opus 5.5 (effort: high, context: 1M)\n"}}
    out = review.pr_description_html(spec)
    assert "Closes" in out and "Generated with" not in out and "effort" not in out


def test_the_merge_risk_is_taken_from_the_description_a_push_wrote_it_into():
    """Victor, 4 Nov 2026: "take it straight from the PR": a title and a line per fact, once,
    and the PR's words win over the content file's."""
    spec = {"mergeRisk": {"door": "one-way", "blast": "stale"},
            "_prBody": {"url": URL, "body": "Closes #25.\n\n<!-- hr:merge-risk -->\n"
                        "**Merge risk**\n- Two-way door: ✅\n- Blast radius: ◔ **visits API**\n"
                        "<!-- /hr:merge-risk -->\n\n🤖 Generated with "
                        "[Claude Code](https://claude.com/claude-code)"}}
    out = review.pr_description_html(spec)
    assert ('<div class="mergerisk" id="mergerisk"><p><strong>Merge risk</strong></p><ul>'
            '<li>Two-way door: ✅</li><li>Blast radius: ◔ <strong>visits API</strong></li></ul>'
            '</div></div></details>') in out or \
        ('<div class="mergerisk" id="mergerisk"><p><b>Merge risk</b></p><ul>'
         '<li>Two-way door: ✅</li><li>Blast radius: ◔ <b>visits API</b></li></ul>'
         '</div></div></details>') in out, out
    assert out.count("Two-way door") == 1 and "hr:merge-risk" not in out and "stale" not in out
    assert out.index("Closes") < out.index('class="mergerisk"')
    only = {"_prBody": {"url": URL, "body": "<!-- hr:merge-risk -->\nx\n<!-- /hr:merge-risk -->"}}
    assert review.pr_description_html(only).startswith('<div class="mergerisk"')


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


def test_merge_risk_closes_a_description_a_push_has_not_written_it_into():
    """Victor, 29 Oct 2026, after Matt Pocock's "merge danger": one-way or two-way door,
    and how far it reaches."""
    spec = {"_prBody": {"url": URL, "body": "Closes #37."},
            "mergeRisk": {"door": "two-way", "blast": "visits API", "why": "a PUT <b> clears"}}
    out = review.pr_description_html(spec)
    assert out.index("Closes") < out.index('class="mergerisk"') < out.index("</details>")
    assert "Two-way door: ✅ a PUT &lt;b&gt; clears" in out
    assert "Blast radius: " in out and "visits API" in out
    assert "- One-way door: ⚠️" in review.merge_risk_md({"mergeRisk": {"door": "One-way"}})


def test_merge_risk_without_a_known_door_is_not_drawn_and_stands_alone_without_a_body():
    assert review.merge_risk_md({"mergeRisk": {"door": "maybe", "blast": "x"}}) == ""
    assert review.merge_risk_md({}) == "" == review.merge_risk_html("")
    alone = review.pr_description_html({"mergeRisk": {"door": "two-way"}})
    assert alone.startswith('<div class="mergerisk" id="mergerisk">') and "prdesc" not in alone


def test_the_page_says_the_merge_risk_in_the_words_a_push_would_write():
    """Two copies of one sentence, in two scripts that do not import each other."""
    spec_ = importlib.util.spec_from_file_location("ppc_md", HERE / "push-pr-comments.py")
    ppc = importlib.util.module_from_spec(spec_)
    sys.modules[spec_.name] = ppc               # its dataclasses look themselves up there
    spec_.loader.exec_module(ppc)
    for risk in ({"door": "two-way"}, {"door": "one-way", "why": "w"},
                 {"door": "two-way", "blast": "visits API"},
                 {"door": "two-way", "blast": "visits API", "blastSize": 1, "why": "w"},
                 {"door": "one-way", "blastSize": 4}, {"door": "maybe"}):
        assert review.merge_risk_md({"mergeRisk": risk}) == ppc.merge_risk_md(risk), risk


def test_the_blast_radius_wears_a_circle_that_fills_with_its_size():
    """Victor, 29 Oct 2026: ○ ◔ ◑ ◕ ●, so a reader knows at a glance what to expect."""
    md = review.merge_risk_md({"mergeRisk": {"door": "two-way", "blast": "visits API",
                                             "blastSize": 1}})
    assert "- Blast radius: ◔ **visits API** (≈25% of the system)" in md
    assert [review.BLAST_GLYPHS[review.blast_size({"mergeRisk": {"blastSize": n}})]
            for n in (0, "2", 3, 4)] == ["○", "◑", "◕", "●"]
    # No size, or one off the scale: the words alone, no guessed circle.
    for bad in (None, 5, -1, "big", True):
        assert review.blast_size({"mergeRisk": {"blastSize": bad}}) is None
    # A size with no words says the size.
    assert "● **≈100% of the system**" in review.merge_risk_md(
        {"mergeRisk": {"door": "one-way", "blastSize": 4}})


def test_the_masthead_draws_the_circle_beside_the_grade_with_its_meaning_on_hover():
    out = review.blast_glyph_html({"mergeRisk": {"blast": "visits API", "blastSize": 1}},
                                  "titleblast")
    assert out.startswith('<span class="titleblast" data-tip="Blast radius ≈25%: visits API"') and out.endswith("◔</span>")
    assert review.blast_glyph_html({"mergeRisk": {"blast": "visits API"}}) == ""


def test_the_masthead_shows_the_doors_mark_before_the_circle_with_its_meaning_on_hover():
    """Victor, 29 Oct 2026: the green check beside the grade too — one table for both."""
    two = review.door_mark_html({"mergeRisk": {"door": "Two-way", "why": "a revert stops it"}})
    assert 'data-tip="Two-way door: revertable"' in two
    assert two.endswith(f"{review.MERGE_DOOR_MARKS['two-way']}</span>") and "✅" in two
    assert "⚠️</span>" in review.door_mark_html({"mergeRisk": {"door": "one-way"}})
    assert review.door_mark_html({}) == "" == review.door_mark_html({"mergeRisk": {"door": "?"}})
    # The MERGE RISK line draws the same mark from the same table.
    assert f'Two-way door: {review.MERGE_DOOR_MARKS["two-way"]}' in \
        review.merge_risk_md({"mergeRisk": {"door": "two-way"}})


def test_the_door_and_the_circle_ride_inside_the_grade_pill_with_one_hover_for_all_three():
    """Victor, 29 Oct 2026: one pill, `✅ ◔ 7/10`; the marks go to the MERGE RISK line.
    11 Nov 2026: a superscript 🤖 after each line of the hover — all three are the AI's."""
    spec = {"mergeRisk": {"door": "two-way", "blast": "visits API", "blastSize": 1}}
    pill = ('<a class="titlescore v-mid" href="#review" data-tip="Overall quality: 7/10">'
            '<b>7</b><small>/10</small><i></i></a>')
    out = review.grade_pill_with_risk(spec, pill)
    bot = "&lt;sup&gt;🤖&lt;/sup&gt;"
    assert out.startswith('<a class="titlescore v-mid" href="#review" data-tip-html="✅ Two-way '
                          f'door: revertable{bot}&lt;br&gt;&lt;span class=&quot;tipblast&quot;&gt;◔&lt;/span&gt; Blast radius ≈25%: visits '
                          f'API{bot}&lt;br&gt;🔍 Overall quality: 7/10{bot}"')
    assert 'data-tip="' not in out, "one hover, not the grade's alone"
    assert ('<span class="titledoor">✅</span><span class="titleblast">◔</span></span>'
            '<b>7</b><small>/10</small>') in out and out.count('class="ts-risk"') == 1
    assert review.grade_pill_with_risk({}, pill) == pill
    alone = review.grade_pill_with_risk({"mergeRisk": {"door": "one-way"}}, "")
    assert alone.startswith('<span class="titlescore ts-alone"') and alone.count(bot) == 1
    # The line the marks go to.
    assert 'id="mergerisk"' in review.pr_description_html(spec)
