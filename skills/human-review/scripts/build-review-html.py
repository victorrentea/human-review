#!/usr/bin/env python3
"""Render a self-contained HTML review guide from a small JSON content file.

Splits the review into the half a machine should own and the half a human should:

  * this script owns the *mechanics* — page shell, styling, inlining the delta
    SVGs, laying out the diagram gallery, cutting every code snippet out of the
    working tree at build time via extract-snippet.py;
  * the JSON owns the *judgement* — what changed, what is risky, in what order a
    reviewer should look.

No snippet text ever lives in the JSON: only a `path:from-to` reference, so a
guide can never drift from the code it quotes.

Usage:
    build-review-html.py content.json --out .human-review/review.html
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import code_xref  # noqa: E402 - resolved from next to this file, not from site-packages

EXTRACT = HERE / "extract-snippet.py"
CODEOWNERS = HERE / "codeowners-check.py"
TESTCHANGES = HERE / "test-changes.py"

# The scope bar's third sign, beside `+` and `−`. It was `±`, which everywhere else a
# reader has met it means a *range* — "forty, give or take" — while the count of edited
# files is exact. A pencil says "someone went in and changed these", which is the fact,
# and it is the mark the page already uses for an edited row further down.
PENCIL = "\u270d\ufe0f"


# --------------------------------------------------------------------------- #
# what the page is allowed to ask the server to run
# --------------------------------------------------------------------------- #

# Dot-prefixed deliberately: `publish-demo.sh` publishes everything in a run directory
# that does not begin with a dot, and the downloadable zip is built from what it
# publishes. A manifest that travelled with either would be a list of this machine's
# commands sitting next to a page on someone else's, offering to run them.
ACTIONS_FILE = ".actions.json"

# Three buttons on this page describe a command and hand it to the clipboard, because a
# file on disk cannot run anything. Served by serve-review.py they can — but the command
# must not travel from the page, or "the review guide" becomes "a shell on :7654 that any
# tab in the browser can reach". So the page sends an id and the server looks the command
# up here, in a manifest written beside review.html by this build.
#
# The consequences are the point, not a side effect:
#   * a page from an older build can only name ids the *current* build still declares;
#   * the copy in the zip and the copy on GitHub Pages sit next to no manifest at all, so
#     they can ask for nothing — which is also exactly what they could do before;
#   * every command in it was written by this build out of the content file, so reviewing
#     what the button may run is reviewing the content file, which is already reviewed.
#
# A module-level register rather than a value threaded through the emitters: the three
# declarations are made by `runtime_html` and `rerun_html`, which are leaves of a render
# tree eight calls deep whose every other node is a pure string function. Passing a
# collector down that tree would put a parameter for the action server on a dozen
# signatures that have nothing to do with it. `main` clears it before a build and writes
# it after, which is the only ordering that matters.
ACTIONS: dict[str, dict] = {}


def declare_action(action_id: str, command: str, *, params: dict[str, str] | None = None,
                   scrape: str = "", reload: bool = False, label: str = "") -> str:
    """Register one runnable command and return the id the page should send.

    `params` maps each `{name}` hole in the command to the shape its value must have
    (`int`, `url`, `word` — serve-review.py owns the patterns). A hole with no declared
    shape is refused at run time rather than interpolated, so a template can never grow a
    parameter here without someone deciding what is allowed to go in it."""
    ACTIONS[action_id] = {"command": command, "params": dict(params or {}),
                          "scrape": scrape, "reload": reload, "label": label}
    return action_id


def write_actions(out_dir: Path) -> Path:
    """Drop the manifest beside the page, always — an empty one included.

    Always, because the file is read by mtime and the alternative to rewriting it is
    leaving the previous build's manifest in place: a page that no longer has the button
    next to a server that still offers to run the command behind it. An empty `actions`
    is a perfectly good statement and the one this build means when it renders no
    runnable control."""
    path = out_dir / ACTIONS_FILE
    path.write_text(json.dumps({"version": 1, "actions": ACTIONS}, indent=2) + "\n",
                    encoding="utf-8")
    return path


SEVERITIES = {
    "high": ("sev-high", "must look"),
    "medium": ("sev-med", "worth a look"),
    "low": ("sev-low", "nit"),
    "info": ("sev-info", "context"),
}

CSS = """
:root {
  /* The one declaration that tells the browser this page has two skins, so everything it
     paints itself follows them: scrollbars first of all. Without it a code block that
     scrolls sideways on a dark page gets the light grey bar Chrome paints by default —
     a white stripe across the middle of a dark card, which reads as a rendering fault
     rather than as a control. Form controls and the caret come along for free. */
  color-scheme: light dark;
  --bg:#fbfbfd; --fg:#1c1c22; --muted:#6b6b78; --line:#e2e2ea; --card:#ffffff;
  --accent:#8a1c1c; --accent-soft:#fdeaea; --code-bg:#f6f6fa; --link:#1a4fa0;
  --drift:#b5730a; --drift-fg:#ffffff;
  /* PlantUML's own fixed palette, named — not reused from --bg/--fg/etc above, because
     those are tuned for prose and would visibly change every diagram's light-mode look.
     Each one equals exactly what PlantUML already emits, so light mode is pixel-identical
     to an unthemed render; only the dark block below diverges. `inline_svg`
     (`_theme_diagram_colors`) rewrites the SVG's own fill/stroke/background literals to
     `var(--dgm-*)` at inline time — the .puml sources stay generator output, undecorated,
     since driving this from PlantUML's own `!theme`/`skinparam` would mean regenerating
     every diagram (out of reach here) rather than restyling the one already rendered. */
  --dgm-bg:#ffffff; --dgm-box:#f1f1f1; --dgm-frame:#eeeeee; --dgm-legend:#dddddd;
  --dgm-line:#181818; --dgm-fg:#000000; --dgm-icon:#add1b2; --dgm-activation:#e2e2f0;
  --dgm-muted:#888888; --dgm-link:#1a4fa0;
  /* PlantUML's default palette is not the only one on the page: a hand-authored
     .puml may carry its own `<style>` block, and packages.puml does — Material
     blue-grey, chosen so the architecture diagram reads as a diagram of *layers*
     rather than of classes. Those three literals get variables of their own for
     the same reason as the twelve above, and for one more: the text inside those
     boxes is plain #000000, so it follows --dgm-fg into near-white in dark mode.
     A fill left un-themed there is not merely off-palette, it is the box that
     swallows its own label. */
  --dgm-box-accent:#eceff1; --dgm-line-accent:#546e7a; --dgm-arrow-accent:#78909c;
  /* The diff renderers' two hues — both puml_diff.py and seq_puml_diff.py paint an
     addition ADDED and a removal REMOVED, one palette across both deltas. They equal the
     page's own `.added`/`.removed` pair on purpose: a diagram and a code hunk on the same
     page must not mean different things by the same green. Kept as their own variables
     rather than folded into --accent because they mark a direction, not emphasis, and
     must stay legible against whichever diagram surface they are drawn on. The two
     tints are what a sequence delta fills a lifeline box or a note with — a full-strength
     hue there is a slab that swallows the black label PlantUML draws on it. */
  --dgm-diff-add:#2e7d32; --dgm-diff-del:#c62828;
  --dgm-diff-add-bg:#eaf6ec; --dgm-diff-del-bg:#ffebeb;
  /* Which of the three pictures you are looking at, said by the frame around it rather
     than by reading the buttons. Red is the delta's own removal red, taken by reference
     so the border and the strokes inside it can never disagree; blue and green are the
     page's link and "added" hues, which already mean "the current thing" and "the good
     side" everywhere else on the page. */
  --view-diff:var(--dgm-diff-del); --view-new:#1a4fa0; --view-old:#1f7a45;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#15151a; --fg:#e8e8ef; --muted:#9a9aa8; --line:#2c2c36; --card:#1d1d24;
          --accent:#f08a8a; --accent-soft:#3a1f1f; --code-bg:#101015; --link:#8ab4f8;
          --drift:#e0a33c; --drift-fg:#15151a;
          /* PlantUML draws these as flat, fully-opaque shapes, so each is picked to read
             the way its light counterpart does on white — not lifted from --bg/--card,
             whose contrast ratios were tuned for text, not a diagram's fills and hairline
             strokes. --dgm-icon is deliberately absent: the stereotype ellipse's pale
             green already sits at ~9:1 against a dark box, better than it does on white,
             so it is left un-overridden. Both diff hues lift to the page's own dark
             added/removed pair: #2e7d32 and #c62828 sit near 4:1 against a near-black
             canvas, under the 4.5:1 text minimum, where #8fd39c and #f08a8a clear it
             comfortably and still read as green and red. Their two tints invert
             outright — a pale wash behind a near-white label is the box that swallows
             its own name — landing beside --accent-soft on the removal side. */
          --dgm-bg:#1d1d24; --dgm-box:#26262e; --dgm-frame:#202028; --dgm-legend:#2c2c36;
          --dgm-line:#8f8fa0; --dgm-fg:#e8e8ef; --dgm-activation:#2e2e42;
          --dgm-muted:#9a9aa8; --dgm-link:#8ab4f8;
          /* The blue-grey trio, kept blue-grey: the hue is what tells the
             architecture diagram apart from the class diagrams beside it, so it
             is preserved and only the lightness is flipped. The fill lands a
             touch cooler than --dgm-box for exactly that reason, and reads at
             ~11:1 against --dgm-fg; both strokes clear 3:1 on --dgm-bg. */
          --dgm-box-accent:#29323a; --dgm-line-accent:#93a9b5;
          --dgm-arrow-accent:#7f97a6;
          --dgm-diff-add:#8fd39c; --dgm-diff-del:#f08a8a;
          --dgm-diff-add-bg:#1f3329; --dgm-diff-del-bg:#3a1f1f;
          /* --view-diff is not repeated: it is `var(--dgm-diff-del)`, so it follows the
             line above on its own. These two are lifted to the same footing as the
             page's dark link colour — #1a4fa0 and #1f7a45 are both under 3:1 on a
             near-black ground, and a border nobody can see is not a signal. */
          --view-new:#8ab4f8; --view-old:#6fce93; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
/* The masthead used to spend 276px before a word of the review: a 2.5rem gutter, a
   1.9rem headline, then three separated rows (subtitle, chips, strip) each holding a
   1.4-1.6rem cushion. Every one of those rows carries facts and stays; what goes is the
   air between them. The rhythm is now one tight stack -- title, subtitle, chips, tabs --
   read as a single masthead rather than four independent bands. */
.wrap { max-width:1080px; margin:0 auto; padding:1.4rem 1.25rem 5rem; }
/* With a masthead the strip is no longer the only thing that survives a scroll: title,
   refs, chips and tabs travel together, so the page opens with the title against the top
   edge rather than behind a gutter that would then be pinned there for the whole read. */
.wrap:has(.masthead) { padding-top:.25rem; }
h1 { font-size:1.5rem; margin:0 0 .15rem; letter-spacing:-.02em; }
h2 { font-size:1.3rem; margin:2.8rem 0 .8rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
h3 { font-size:1.02rem; margin:1.8rem 0 .5rem; }
p { margin:.6rem 0; }
a { color:var(--link); }
.sub { color:var(--muted); margin:0 0 .55rem; font-size:.93rem; }
/* The one `.sub` that is not an aside. Every other subtitle on the page repeats or
   qualifies something the reader can already see, which is what earns them the muted
   grey; this line is the only place the size of each pile is stated at all. Grey filed
   the page's opening fact under "small print" — it reads at full text weight instead. */
.sub.counts { color:var(--fg); }
.scopebar { display:flex; flex-wrap:wrap; gap:.45rem; margin:0 0 .55rem; }
.chip { background:var(--card); border:1px solid var(--line); border-radius:999px;
        padding:.15rem .7rem; font-size:.82rem; color:var(--muted); }
.chip b { color:var(--fg); font-weight:600; }
a.chip-link { text-decoration:none; }
a.chip-link:hover { border-color:var(--link); background:var(--accent-soft); }
button.chip-mode { font:inherit; font-size:.82rem; cursor:copy; }
.chip-served { color:#2e7d32; border-color:#2e7d32; cursor:default; }
table.costtab { border-collapse:collapse; width:100%; font-size:.85rem; }
table.costtab caption { caption-side:top; text-align:left; color:var(--muted);
             font-size:.8rem; line-height:1.5; margin:0 0 .55rem; }
table.costtab th { text-align:left; font:700 .68rem/1.7 inherit; letter-spacing:.09em;
             text-transform:uppercase; color:var(--muted);
             border-bottom:1px solid var(--line); padding:0 0 .2rem; }
table.costtab td { padding:.24rem 0; border-bottom:1px solid var(--line); color:var(--fg); }
table.costtab th + th, table.costtab td + td { text-align:right; padding-left:1.2rem;
             font-variant-numeric:tabular-nums; white-space:nowrap; }
table.costtab td:last-child { font-weight:600; }
/* The bill's own tab, where the table is the panel rather than a drawer under a chip: it
   gets the page's reading width and a size a reader can sit with, not the compressed
   footnote a popover has to be. */
.costledger { font-size:.95rem; max-width:64rem; }
.costledger caption { font-size:.88rem; margin-bottom:.9rem; }
/* The three acts — writing it, reviewing it, building this guide. A caption row rather
   than three tables, because the whole point of the tab is comparing magnitudes ACROSS
   them, and three tables means three column widths and nothing lines up. */
table.costtab tr.costgroup td { padding:1.1rem 0 .3rem; border-bottom:0;
             font:700 .68rem/1.7 inherit; letter-spacing:.09em; text-transform:uppercase;
             color:var(--muted); }
table.costtab tr.costgroup:first-child td { padding-top:.4rem; }
/* What the row is measuring, under the row's own name. On its own line because these are
   the caveats that keep the number honest — which window was costed, which command was
   priced — and a reader scanning the money column should be able to skip them, not have
   them wrapped into the label. */
.costsub { display:block; color:var(--muted); font-size:.8rem; font-weight:400;
             line-height:1.45; margin-top:.1rem; }
.costsub code { font-size:.95em; }
.costnote { color:var(--muted); font-weight:400; }
/* A measured zero is an answer, not a gap — the tab was produced by a script, so it cost
   nothing. Muted and folded onto one row so the answer is on the page without a wall of
   zeros burying the three rows that carry the actual spend. */
table.costtab tr.costquiet td, table.costtab tr.costquiet td:last-child {
             color:var(--muted); font-weight:400; }
table.costtab tfoot td { border-bottom:0; }
table.costtab tfoot tr.costtotal td { border-top:1px solid var(--line);
             padding-top:.35rem; font-weight:700; }
/* The page's diff vocabulary, and the only three colours a signed number is allowed to
   take: green added, red removed, yellow changed. The third joined the other two once
   the scope bar started counting edited files beside `+` and `−` — a number left the
   colour of the text beside two coloured ones reads as a different KIND of number, not
   as the third member of a set. The pencil in front of it is a glyph, not a colour, and
   the number behind it still needs to be the same yellow as everything else that
   changed. */
.added { color:#2e7d32; } .removed { color:#c62828; } .changed { color:#9a6700; }
@media (prefers-color-scheme: dark) {
  .added{color:#8fd39c} .removed{color:#f08a8a} .changed{color:#d29922}
}
/* The kinds of test a change set offers as acceptance evidence: e2e through the
   browser, at the API, unit. They were one run of prose with bold lead-ins, and the
   reader's question -- "is there anything at this level at all?" -- was answered only by
   whoever read every sentence. As cards it is answered by looking: one panel per kind,
   the kind's own colour on its edge, and a kind with nothing behind it is a card that
   says so rather than a paragraph that never got written. The palette is the one the
   requirements map already uses for the same three words, so a card and the map below
   it do not disagree about what "e2e" is coloured. */
.evidence { display:grid; gap:.7rem; margin:.8rem 0 1.1rem;
        grid-template-columns:repeat(auto-fit,minmax(17rem,1fr)); }
.evi { --evi:var(--muted); background:var(--card); border:1px solid var(--line);
        border-top:3px solid var(--evi); border-radius:8px; padding:.65rem .9rem .8rem; }
.evi > h4 { margin:0 0 .3rem; font-size:.95rem; display:flex; align-items:baseline;
        gap:.4rem; }
.evi > h4 .evi-ic { font-size:1rem; line-height:1; }
.evi > h4 .evi-n { margin-left:auto; color:var(--evi); white-space:nowrap;
        font:700 .7rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
.evi p { margin:.3rem 0; font-size:.9rem; }
.evi ul { margin:.35rem 0 0; padding-left:1.05rem; display:grid; gap:.28rem; font-size:.89rem; }
.evi.e2e { --evi:#1c5aaf; } .evi.api { --evi:#703aa8; } .evi.unit { --evi:#147878; }
/* A level nothing covers is the finding, not a gap in the page: it keeps its card and
   says what is missing, in the colour the rest of the page uses for that. */
.evi.none { --evi:#c62828; }
.evi.none p { color:var(--muted); }
@media (prefers-color-scheme: dark) {
  .evi.e2e { --evi:#78afff; } .evi.api { --evi:#c39bff; } .evi.unit { --evi:#5ad7d7; }
  .evi.none { --evi:#f08a8a; }
}
/* Requirements, each with the tests that pin it nested underneath its own text. The
   requirement is prose and keeps no bullet — the marker would compete with the flags on
   the tests below it, which are the part carrying information. */
ul.reqlist { list-style:none; margin:.6rem 0 1rem; padding:0; display:grid; gap:1.1rem; }
ul.reqlist > li { border-left:2px solid var(--line); padding-left:.85rem; }
.req-text { font-size:.96rem; }
ul.req-tests { margin:.4rem 0 0; padding-left:1.05rem; display:grid; gap:.22rem;
        font-size:.9rem; }
ul.req-tests li { list-style:none; }
/* The flag reuses `.added` / `.removed` for its colour rather than declaring its own, so
   new/deleted read here exactly as they read in the diagram deltas and the line counts.
   "modified" and "unchanged" get no colour: only the two ends of the scale are news. */
.tflag { display:inline-block; min-width:5.4rem; margin-right:.5rem;
        font:700 .64rem/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;
        letter-spacing:.09em; text-transform:uppercase; vertical-align:baseline; }
.tflag.added { color:#2e7d32; } .tflag.removed { color:#c62828; }
.tflag.changed { color:var(--fg); } .tflag.same { color:var(--muted); }
@media (prefers-color-scheme: dark) {
  .tflag.added { color:#8fd39c; } .tflag.removed { color:#f08a8a; }
}
a.srcref.testref, span.srcref.testref { margin-bottom:0; font-size:11.5px; }
span.srcref.testref.tgone { color:var(--muted); text-decoration:line-through;
        text-decoration-thickness:1px; cursor:default; }
.tloc { color:var(--muted); font-weight:400; }
/* "does it still run" — amber, because it is neither a gain nor a removal but a warning:
   the test is right there in the file, fully written, asserting nothing. Outlined rather
   than filled so it reads as a stamp on the row instead of competing with the flag that
   opens it. */
.tsilenced, .tback { display:inline-block; margin-left:.45rem; padding:0 .34rem;
        border:1px solid currentColor; border-radius:3px;
        font:700 .62rem/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
        letter-spacing:.07em; text-transform:uppercase; vertical-align:baseline; }
.tsilenced { color:#b26a00; }
.tback { color:#2e7d32; }
@media (prefers-color-scheme: dark) {
  .tsilenced { color:#e0a458; } .tback { color:#8fd39c; }
}
.tnote { color:var(--muted); font-size:.86rem; }
/* The ledger: the same rows as the requirement lists, grouped by what happened instead
   of by what they pin. One column, because the groups are wildly uneven — twenty-two new
   beside one that stopped running — and a grid would give the one that matters most the
   least room. The group that matters most is first and carries the amber edge the row
   stamps already use, so it reads as a warning band rather than a fourth heading. */
.tledger { display:grid; gap:1.4rem; margin:.9rem 0 1rem; }
.tgroup h3 { margin:0; font-size:.98rem; letter-spacing:.01em; }
.tgroup h3 b { margin-left:.3rem; color:var(--muted);
        font:700 .8rem/1 ui-monospace,SFMono-Regular,Menlo,monospace;
        font-variant-numeric:tabular-nums; }
.tgroup > p.sub { margin:.15rem 0 .3rem; }
.tgroup ul.req-tests { padding-left:0; }
.tgroup-off { border-left:2px solid #b26a00; padding-left:.85rem; }
.tgroup-off h3 { color:#b26a00; }
@media (prefers-color-scheme: dark) {
  .tgroup-off { border-left-color:#e0a458; } .tgroup-off h3 { color:#e0a458; }
}
/* The traces: one row per recorded test, each opening on the Playwright trace viewer
   itself. The row is deliberately the same furniture as a ledger row — the same flag
   column, the same `.tloc` — because it is the same test seen from a different angle,
   and a second vocabulary for "this one failed" would be a second thing to learn. */
/* The 📺 a covering-tests row wears when the run recorded that test. Served, it opens
   the replay in a window of its own; off disk it copies the line that does. */
.rm-tv { flex:0 0 auto; text-decoration:none; font-size:12px; line-height:1; }
.rm-tv:hover { filter:brightness(1.2); }
/* The 🕵️ beside it, on a row whose test is tagged @generate_sequence: the way from the
   test to the sequence that test's own run drew, in the tab that holds it. Deliberately
   the same furniture as the 📺 — both are doors out of the row into a recording of the
   same run, and a second vocabulary for "there is more of this elsewhere" would be a
   second thing to learn. */
.rm-seq { flex:0 0 auto; text-decoration:none; font-size:12px; line-height:1; }
.rm-seq:hover { filter:brightness(1.2); }
ul.fixlist { margin:.5rem 0 .8rem; padding-left:1.1rem; display:grid; gap:.3rem; }
ul.fixlist li { font-size:.93rem; }
ul.fixlist .srcref { margin-bottom:0; font-size:11.5px; }
/* The edge is --link, not --accent. --accent is this page's red, and red is spent
   everywhere else on something being wrong — a removed line, a failing test, a level
   nothing covers. A lede states what the tab found; stamping it with the same red made
   every tab open on what read as a warning. Blue is the page's other structural colour
   and carries no verdict. */
.lede { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--link);
        border-radius:6px; padding:.9rem 1.1rem; }
/* A phrase that is short because the long version is one hover away. It has to *look*
   hoverable or the short version is simply less information: dotted underline, the help
   cursor, and the page's own link colour on hover — the same vocabulary `.srcref` uses,
   without pretending to be a link, because nothing navigates. */
.dfn { border-bottom:1px dotted currentColor; cursor:help; }
.dfn:hover { color:var(--link); }
figure { margin:1rem 0; }
figcaption { color:var(--muted); font-size:.86rem; }
.snippet { background:var(--card); border:1px solid var(--line); border-radius:8px;
            padding:.7rem .9rem; margin:.9rem 0; overflow:hidden; }
.snippet-note { margin:0 0 .45rem; color:var(--fg); font-size:.9rem; }
.srcref { display:inline-block; font:600 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
          color:var(--link); text-decoration:none; border-bottom:1px dotted currentColor; margin-bottom:.5rem; }
.srcref:hover { background:var(--accent-soft); }
/* The diff handle belongs to the snippet *above* it — a fix is a change, and this opens
   the change. Left to flow normally it lands in the gap between two cards, equidistant
   from both, and reads as a stray line belonging to neither. Pulled up tight under its
   own card and indented to that card's text column so it reads as its footer. Boxed
   rather than underlined, because it does something different from every other srcref on
   the page: those open a file, this opens a comparison. */
a.srcref.diffref { display:inline-block; margin:-.35rem 0 1.15rem 1rem; padding:.1rem .5rem;
          border:1px solid var(--line); border-bottom:1px solid var(--line); border-radius:5px;
          background:var(--card); }
a.srcref.diffref:hover { border-color:var(--link); background:var(--accent-soft); }
pre.code { margin:0; background:var(--code-bg); border-radius:6px; padding:.6rem .2rem .6rem 0;
            overflow-x:auto; font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
pre.code code { white-space:pre; }
.ln { display:inline-block; width:3.4em; padding-right:.9em; text-align:right; color:var(--muted);
      user-select:none; opacity:.65; }
.diagram { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:1rem; margin:1.1rem 0; }
.diagram .head { display:flex; justify-content:space-between; align-items:baseline; gap:1rem; flex-wrap:wrap; }
.diagram .head b { font-size:1rem; }
/* The logging tab: no separate line for the level and no second coloured pill — the
   `log.warn(` in the code above says the level, in the colour Pygments gives every other
   call. What is left under the code is the verdict alone. The location used to be pinned
   to this row's far right as the box's corner marker; it has gone up into the source bar
   that heads every quoted block on this page, which is where a reader now looks for it on
   all three tabs instead of only here. */
.log-footer { display:flex; flex-wrap:wrap; align-items:baseline;
              column-gap:.8rem; row-gap:.25rem; margin:.6rem 0 0; font-size:.85rem; }
/* One bullet per value the statement interpolates, under the verdict that sums them up.
   The name is the argument as the source writes it, so a three-value statement can be
   scanned for *which* value is the problem instead of read as one fused sentence. */
.log-values { margin:.35rem 0 0; padding-left:1.15rem; display:grid; gap:.22rem;
              font-size:.82rem; color:var(--muted); }
.log-values code { background:var(--code-bg); border-radius:4px; padding:.05rem .3rem;
                    font-size:.9em; color:var(--fg); }
.log-values .val-unresolved { color:#8a4b00; font-style:italic; }
/* The model-failure message: no per-value answers exist to bullet, so its one line sits
   where they would have. */
.log-note { margin:.35rem 0 0; font-size:.82rem; color:#8a4b00; font-style:italic; }
@media (prefers-color-scheme: dark) {
  .log-values .val-unresolved { color:#f0b558; }
  .log-note { color:#f0b558; }
}
/* Where a logged value came from is shown as *code* now -- the origin lines pulled into
   the same <pre>, with their real line numbers and a "N lines not shown" row across the
   jump (`extract-snippet.py` renders both; the muting lives in its own stylesheet). The
   list of hops that used to sit here, retyping those same lines as prose with a note
   attached, is gone -- it said nothing the block above it does not now say itself. */
/* The verdict's trace is spelled out right here, in the same row -- not a hover-only
   tooltip, because it is the point of the mark. `.warn` is the loud "not evaluated"
   state (the model could not be reached) -- never styled like DOUBT's muted amber, so
   a reader cannot mistake "never asked" for "asked, and could not tell". */
.privacy-verdict { color:var(--fg); }
.privacy-verdict.added b { color:#2e7d32; }
.privacy-verdict.removed { color:#8a1c1c; }
.privacy-verdict.removed b { color:#c62828; }
.privacy-verdict.warn { color:#8a4b00; font-weight:700; }
@media (prefers-color-scheme: dark) {
  .privacy-verdict.added b { color:#8fd39c; }
  .privacy-verdict.removed { color:#f2a0a0; }
  .privacy-verdict.removed b { color:#f08a8a; }
  .privacy-verdict.warn { color:#f0b558; }
}
/* Where a verdict was reached by asking a model, the mark says so on the verdict itself
   -- one glyph, hover for the words. A page that mixes measured facts with inferred ones
   and marks neither leaves the reader to guess which is which. */
.ai-mark { font-size:.62em; vertical-align:super; margin-left:.28em; cursor:help;
           text-decoration:none; }
.privacy-legend { margin:1rem 0 0; }
.privacy-legend-title { margin:0 0 .35rem; font-weight:700; font-size:.85rem; }
.privacy-legend-note { margin:0 0 .5rem; color:var(--muted); font-size:.78rem; line-height:1.6; }
.privacy-legend-list { list-style:none; margin:0; padding:0; display:grid; gap:.35rem;
                        color:var(--muted); font-size:.8rem; line-height:1.6; }
.prov { margin:.5rem 0 0; display:flex; gap:.9rem; flex-wrap:wrap; }
.prov .srcref { margin-bottom:0; }
.diagram .head span { color:var(--muted); font-size:.82rem; font-family:ui-monospace,Menlo,monospace; }
/* --dgm-bg, not --card: the SVG's own canvas is already recoloured to --dgm-bg by
    `_theme_diagram_colors` (build-review-html.py), and this box is what shows through
    its margins — the same colour, or the diagram would sit in a visibly mismatched
    frame in dark mode. */
.diagram .svgbox { overflow-x:auto; margin-top:.7rem; background:var(--dgm-bg); border-radius:6px; padding:.6rem; }
.diagram .svgbox svg { max-width:100%; height:auto; display:block; margin:0 auto; }
.diagram .svgbox[hidden] { display:none; }
/* A class name that opens the source looks exactly like one that does not. PlantUML's
    tooltip says so, but only after a second of hovering and only if you were already
    suspicious — so the name underlines the moment the pointer is over it. PlantUML draws
    the class anchor around the whole box — icon, name and every field — which would make
    a field look and act like the class; `_scope_entity_links` (build-review-html.py)
    narrows it at inline time to the icon and the name, so only those two underline and
    only those two are clickable. A field is inert: no link, no underline. */
.diagram svg a[href^="vscode:"], .diagram svg a[href^="drawio:"] { cursor:pointer; }
.diagram svg a[href^="vscode:"]:hover text,
.diagram svg a[href^="drawio:"]:hover text { text-decoration:underline; }
/* A removed element carries its strikethrough as a presentation attribute, which a CSS
    declaration would silently outrank — underlining it would erase the one mark that
    says it was deleted. Keep both. */
.diagram svg a[href^="vscode:"]:hover text[text-decoration="line-through"] {
  text-decoration:line-through underline; }
/* How much unchanged context to draw around what changed. DomainModel and DB are big
    enough that the whole diagram is a wall to hunt the delta in, and how much context
    makes a given change legible is the reviewer's call, not the generator's. */
.focus { display:flex; align-items:center; gap:.35rem; margin-top:.7rem; flex-wrap:wrap; }
.focus .lbl { color:var(--muted); font-size:.78rem; margin-right:.15rem; }
.focus button { border:1px solid var(--line); background:var(--card); color:var(--muted);
                border-radius:999px; cursor:pointer; font:600 .74rem/1.7 inherit;
                padding:0 .6rem; }
.focus button:hover { border-color:var(--link); color:var(--fg); }
.focus button[aria-pressed="true"] { background:var(--link); border-color:var(--link); color:#fff; }
/* Diff / New / Old, in two buttons and three states. The second button holds both
   words and toggles between them, because a third pill would cost as much room as the
   two that matter and this control has to fit above a sequence diagram as easily as
   above a small one. Why it exists at all: a sequence diagram is generated from traces
   whose call ORDER is not stable between runs, so the delta reports moves nobody made.
   The reader needs a one-click escape to the raw before/after — this is it. */
.dgmviews { margin-top:.7rem; }
.dgmbar { display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; margin-bottom:.5rem; }
.dgmbar button { border:2px solid var(--line); background:var(--card); color:var(--muted);
                 border-radius:999px; cursor:pointer; font:600 .78rem/1.75 inherit;
                 padding:0 .75rem; }
.dgmbar button:hover { border-color:currentColor; color:var(--fg); }
.dgmbar .dgm-newold u { text-decoration:none; opacity:.55; }
/* The active word, underlined inside the button — the only thing that separates the two
   states the one button stands for. Thick and offset so it survives being read at a
   glance next to the border. */
.dgmbar .dgm-newold u.on { text-decoration:underline; text-decoration-thickness:2px;
                           text-underline-offset:3px; opacity:1; }
.dgmviews[data-state="diff"] .dgm-diff { background:var(--view-diff); border-color:var(--view-diff); color:#fff; }
.dgmviews[data-state="new"] .dgm-newold { background:var(--view-new); border-color:var(--view-new); color:#fff; }
.dgmviews[data-state="old"] .dgm-newold { background:var(--view-old); border-color:var(--view-old); color:#fff; }
/* The frame. Deliberately loud: it is meant to be read peripherally, while the eye is
   still on the picture, so nobody argues with a diagram they only think is the delta. */
.dgmpane { border:5px solid var(--line); border-radius:8px; }
.dgmpane[hidden] { display:none; }
.dgmviews[data-state="diff"] .dgmpane { border-color:var(--view-diff); }
.dgmviews[data-state="new"] .dgmpane { border-color:var(--view-new); }
.dgmviews[data-state="old"] .dgmpane { border-color:var(--view-old); }
.dgmpane > .svgbox { margin-top:0; border-radius:3px; }
.dgmpane > .focus { margin:.5rem .6rem 0; }
/* The header is a second, larger hit area for the same New/Old toggle — a sequence
   diagram is tall, and reaching back up to a pill after scrolling is the friction this
   removes. The BODY is deliberately not clickable: a stray click while scrolling or
   selecting must never swap the picture out from under the reader.
   Keyboard users get the two real buttons; the header is not focusable because it
   already contains a link, and a focusable control wrapping a link is a worse trade. */
.diagram.dgm-toggles .head { cursor:pointer; border-radius:6px; margin:-.25rem -.4rem .25rem;
                             padding:.25rem .4rem; transition:background .12s; }
.diagram.dgm-toggles .head:hover { background:var(--code-bg); }
/* The literal character, never the CSS hex escape for it. This stylesheet is a plain
   (non-raw) Python string, so a backslash-two-one-nine-four is read as an octal escape
   by Python and eaten before CSS ever sees it: the arrow shipped as the text "94" on
   every diagram header until someone looked. */
.diagram.dgm-toggles .head b::after { content:"↔"; margin-left:.45rem; font-weight:400;
                                      color:var(--muted); font-size:.8em; }
.diagram.dgm-toggles .head:hover b::after { color:var(--fg); }
/* The hand-drawn diagram's legend. Two colours inside the drawing, and they are not the
   frame around it: the frame says which of the three pictures you are on, these say what
   the picture means. The green is the report's own `--dgm-diff-add`, the one every other
   delta on the page spends on "added", rather than a hex of this legend's own. It lives
   here rather than in a <style> inside a section body, because the markup it dresses is
   generated by `{{drawio:...}}` and a stylesheet the author has to paste alongside is one
   more copy to keep in step. */
.cmlegend { display:flex; gap:1rem; flex-wrap:wrap; margin:.55rem .6rem .1rem;
            color:var(--muted); font-size:.78rem; line-height:1.6; }
/* An entry is a run of text with a swatch at its head, not a flex row: made a flex
   container, it turns each inline <b> in the sentence into a cell of its own, so a row
   whose sentence emphasised a word mid-way came out as four columns with that word
   standing alone in one of them. The swatch sits on the text baseline as an
   inline-block, and the ordinary space after it is the gap. */
.cmlegend i { display:inline-block; width:1.1rem; height:0; border-top:3px solid currentColor;
              border-radius:2px; vertical-align:middle; margin-right:.25rem; }
.cmlegend .new { color:var(--dgm-diff-add); }
.cmlegend .todo { color:#d7263d; }
.cmlegend b { color:var(--fg); font-weight:600; }
@media (prefers-color-scheme:dark) { .cmlegend .todo { color:#ff9090; } }
/* "Open it in draw.io": under the drawing, never on it. The invitation used to be
   painted into the picture itself, which put a sentence about tooling on top of the map
   and made the reader read it again on every look. In HTML it is a link — it looks like
   one, the cursor says so, and it stays out of the diagram's way. */
.dgm-open { margin:.35rem .6rem .1rem; font-size:.82rem; color:var(--fg); }
.dgm-open a { color:var(--fg); font-weight:600; text-decoration:underline;
              text-underline-offset:2px; }
.dgm-open a:hover { text-decoration-thickness:2px; }
/* The command that re-draws the picture above. It sits under the diagram rather than in
   a README because the reader who needs it is the reader who has just been told, inside
   the picture, to go and re-lay the thing out by hand — and a rebuild step they have to
   go and look up is a rebuild step that does not happen. One line, selectable, with the
   button that puts it on the clipboard. */
.rerun { margin:.6rem .6rem .1rem; font-size:.78rem; color:var(--muted); line-height:1.6; }
.rerun .dgm-open { margin:0; }
.rerun .cmdline { display:flex; align-items:flex-start; gap:.5rem; margin-top:.35rem; }
/* `display:flex` on a class beats the browser's own `[hidden] { display:none }`, so a
   folded command was folded in the markup and open on the screen — both of them, one
   under the other, reading as the same line printed twice. Specificity, not the fold. */
.rerun .cmdline[hidden] { display:none; }
.rerun code { flex:1; min-width:0; overflow-x:auto; white-space:pre; display:block;
              background:var(--code-bg); border:1px solid var(--rule); border-radius:5px;
              padding:.4rem .55rem; font-size:.94em; }
.rerun .cmdline button { flex:none; cursor:pointer; font:inherit; color:var(--muted);
                background:var(--code-bg); border:1px solid var(--rule); border-radius:5px;
                padding:.4rem .6rem; }
.rerun .cmdline button:hover { color:var(--fg); border-color:var(--muted); }
/* The two offers inside the sentence are worded as things you do, not as things you
   press, so they are dressed as the draw.io links beside them and not as buttons: three
   boxed controls in one line under a picture read as a toolbar, which is exactly what
   this line stopped being. `button` and not `a` because neither goes anywhere. */
.rerun .dgm-open .runhere, .rerun .cmdpeek { cursor:pointer; font:inherit; color:var(--fg);
              font-weight:600; background:none; border:0; padding:0;
              text-decoration:underline; text-underline-offset:2px; }
.rerun .dgm-open .runhere:hover, .rerun .cmdpeek:hover { text-decoration-thickness:2px; }
/* A sentence of its own, and a sentence's worth of air before it: the offer that throws
   work away is found by the reader who goes looking for it rather than met by the reader
   who does not. */
.rerun .rerun-back::before { content:"\\00a0\\00a0"; }
/* One route on show, and it is the one that works where the page is being read. `click
   here (or run this)` offered both at once, which meant every reader was shown the route
   they could not take: off disk the button is a promise the page cannot keep, and served,
   the shell command is a line of noise beside a control that already runs it. So the pair
   is rendered and the probe picks — `run this` until a server answers for that action,
   `click here` after. No chevron on the fold: its state is the command box itself, which
   is either under the sentence or not, and a mark repeating that is the page narrating
   itself. */
.rerun .offer .runhere { display:none; }
.rerun .offer.served .runhere { display:inline; }
.rerun .offer.served .cmdpeek, .rerun .offer.served .plainword { display:none; }
/* Progressive disclosure: the diagram arrives simplified, and an arrow that has more
    to say is clickable. The hit area is a transparent rect the script lays under each
    such arrow, so the whole band — label, line, marker — answers to one click. */
/* The section header names the scenario the picture is of, so it is the reader's handle on
   the test behind it. Underlined because it is a link and nothing else on a sequence diagram
   is — dotted at rest so it reads as an offer rather than as emphasis, solid under the
   pointer. The colour is PlantUML's own hyperlink colour, or the delta's green or red where
   the header itself was added or removed. */
.diagram svg a[href^="genseq-scenario:"] { cursor:pointer; }
.diagram svg a[href^="genseq-scenario:"] text { text-decoration:underline;
                                                text-decoration-style:dotted; }
.diagram svg a[href^="genseq-scenario:"]:hover text { text-decoration-style:solid; }
.genseq-hot { cursor:pointer; }
.genseq-hit { fill:transparent; }
.genseq-hot:hover .genseq-hit { fill:#1a4fa0; fill-opacity:.07; }
.genseq-hot.genseq-open .genseq-hit { fill:#1a4fa0; fill-opacity:.12; }
.genseq-hot.genseq-open a[href^="genseq:"] text { font-weight:700; }
#genseq-panel { position:absolute; z-index:40; max-width:min(38rem,92vw); min-width:16rem;
                background:var(--card); color:var(--fg); border:1px solid var(--line);
                border-left:3px solid var(--link); border-radius:8px;
                box-shadow:0 8px 28px rgba(0,0,0,.22); padding:.55rem .7rem .7rem; }
#genseq-panel[hidden] { display:none; }
#genseq-panel .genseq-head { display:flex; align-items:baseline; gap:.5rem; }
/* The title takes only the room it needs, and the spacer after the toggle is what
   pushes the step counter and the close button to the far edge. The toggle used to ride
   out there with them, a hand's width from the statement it switches — it belongs
   against the end of the title, where the eye already is. */
#genseq-panel .genseq-title { font:600 12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
                              flex:0 1 auto; min-width:0; word-break:break-all; }
#genseq-panel .genseq-grow { flex:1 1 auto; }
#genseq-panel .genseq-head { gap:.6rem; }
#genseq-panel .genseq-step { color:var(--muted); font-size:.76rem; white-space:nowrap; }
#genseq-panel .genseq-close { border:0; background:none; color:var(--muted); cursor:pointer;
                        font-size:1.1rem; line-height:1; padding:0 .1rem; }
#genseq-panel .genseq-close:hover { color:var(--fg); }
#genseq-panel .genseq-label { color:var(--link); font-size:.8rem; margin:.15rem 0 .4rem; }
#genseq-panel .genseq-label[hidden] { display:none; }
/* The method that answers the route, at the same level as `request body`: both are
   sub-headings of the arrow, one naming what travelled and the other where it landed. */
#genseq-panel .genseq-handler { display:flex; align-items:baseline; gap:.4rem;
                                font-size:.8rem; margin:.15rem 0 .4rem; }
#genseq-panel .genseq-handler[hidden] { display:none; }
#genseq-panel .genseq-handler .genseq-key { color:var(--muted); }
#genseq-panel .genseq-handler .srcref { margin-bottom:0; }
#genseq-panel .genseq-toggle { border:1px solid var(--line); background:var(--code-bg);
                        color:var(--muted); cursor:pointer; border-radius:999px;
                        font:600 .7rem/1.6 inherit; padding:0 .55rem; white-space:nowrap; }
#genseq-panel .genseq-toggle:hover { color:var(--fg); border-color:var(--link); }
#genseq-panel .genseq-toggle[hidden] { display:none; }
/* pre-wrap, not pre: a real controller's SELECT is far wider than the panel, and
    `white-space:pre` cut it mid-statement behind an overlay scrollbar nobody sees on a
    Mac. The payloads here are read, not copied into a terminal, so wrapping wins. */
#genseq-panel pre { margin:0; max-height:24rem; overflow:auto; background:var(--code-bg);
                    border-radius:6px; padding:.5rem .6rem; white-space:pre-wrap;
                    overflow-wrap:anywhere;
                    font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
.embedded-note { margin:0 0 1rem; padding:.6rem .8rem; border-radius:8px; font-size:.85rem;
                 background:var(--code-bg); border:1px solid var(--line); color:var(--muted); }
#copy-toast { position:fixed; left:50%; bottom:1.4rem; transform:translateX(-50%) translateY(.6rem);
              background:var(--fg); color:var(--card); border-radius:999px; z-index:60;
              padding:.45rem .9rem; font-size:.8rem; opacity:0; pointer-events:none;
              transition:opacity .16s, transform .16s; max-width:80vw; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }
#copy-toast.shown { opacity:1; transform:translateX(-50%) translateY(0); }
.badge { border-radius:4px; padding:.1rem .45rem; font-size:.74rem; font-weight:600; text-transform:uppercase;
          letter-spacing:.04em; background:var(--accent-soft); color:var(--accent); }
.city { display:block; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin:1rem 0; }
.city img { display:block; width:100%; height:auto; }
ol.findings { list-style:none; counter-reset:f; padding:0; margin:1rem 0; }
ol.findings > li { counter-increment:f; background:var(--card); border:1px solid var(--line);
                    border-radius:8px; padding:.9rem 1.1rem; margin:.7rem 0; }
/* The number carries the severity's own colour, so the bubble and the badge beside it say
   the same thing. A uniform accent bubble made every item look equally urgent at a glance
   and left the badge doing all the work; now the left margin *is* the triage column.
   `--num-bg` falls back to the accent so an item with no severity still renders. */
ol.findings > li::before { content:counter(f); float:left; margin:.1rem .7rem 0 0; width:1.6rem; height:1.6rem;
    border-radius:50%; background:var(--num-bg,var(--accent)); color:#fff; font-size:.8rem; font-weight:700;
    display:grid; place-items:center; }
ol.findings > li.n-high { --num-bg:#8a1c1c; }
ol.findings > li.n-med  { --num-bg:#8a5a12; }
ol.findings > li.n-low  { --num-bg:#26518f; }
ol.findings > li.n-info { --num-bg:#245c30; }
/* An applied fix is still an item on the same list — same numbering, same shape — but it
   is *done*, and the page must not spend a reviewer's attention on it the way it spends it
   on an open call. Grey is the whole difference: the card recedes, the number keeps its
   place, and the code inside is rendered exactly as loudly as everywhere else. */
ol.findings > li.fixed { background:var(--code-bg); border-style:dashed; --num-bg:#6b7280; }
ol.findings > li.fixed .f-title { font-weight:600; }
@media (prefers-color-scheme: dark) {
  ol.findings > li.n-high { --num-bg:#c05555; }
  ol.findings > li.n-med  { --num-bg:#9a7a2a; }
  ol.findings > li.n-low  { --num-bg:#3f68ad; }
  ol.findings > li.n-info { --num-bg:#33724a; }
  ol.findings > li.fixed  { --num-bg:#565e6b; }
  ol.findings > li.n-assumed { --num-bg:#8b6fd4; border-left-color:#8b6fd4; }
}
ol.findings > li.n-assumed { --num-bg:#5b3fa8; border-left:3px solid #5b3fa8; }
/* The road not taken, which is what makes an assumption checkable at a glance: the reader
   recognises their own intent in one of the two readings without opening anything. */
.f-alt { color:var(--muted); font-size:.9rem; margin:.35rem 0 0; }
.f-alt b { color:var(--fg); font-weight:650; }
/* Who raised it: a provenance stamp, so it is quiet and monospaced, and it sits after the
   badge where the eye is already looking. It is a link where the pass has documentation —
   the page names two slash commands a reader may never have run, and the alternative was a
   line of prose under the verdict explaining them, which every reader after the first has
   to scroll past. The stamp is the thing being asked about, so it is the thing that
   answers. Same face either way; only the hover and the cursor say it opens. */
.f-src { font:600 11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted);
          border:1px solid var(--line); border-radius:4px; padding:.05rem .35rem; margin-left:.35rem;
          vertical-align:.05em; }
a.f-src { text-decoration:none; }
a.f-src:hover, a.f-src:focus-visible { color:var(--link); border-color:var(--link); }
.f-title { font-weight:650; }
.f-why { color:var(--muted); font-size:.9rem; margin:.35rem 0 0; }
.sev-high { background:#fdeaea; color:#8a1c1c; }
.sev-med  { background:#fdf3e2; color:#8a5a12; }
.sev-low  { background:#eef3fb; color:#26518f; }
.sev-info { background:#eef7ef; color:#245c30; }
.sev-fixed { background:#eceef1; color:#4b5563; }
/* An assumption is not a severity. The reviewer's job on one of these is to confirm an
   intent, not to weigh a risk, so it must not borrow red/amber/blue/green — a reader who
   sees an assumption in amber reads "medium bug". Violet is the one hue the page had left,
   and the left edge marks the card as a different *kind* of item without taking it off the
   shared list. */
.sev-assumed { background:#f1ecfb; color:#4c3391; }
@media (prefers-color-scheme: dark) {
  .sev-high{background:#3a1f1f;color:#f2a0a0}.sev-med{background:#3a3018;color:#e6c07b}
  .sev-low{background:#1c2738;color:#9dc0f5}.sev-info{background:#1b2c1f;color:#9ad3a5}
  .sev-fixed{background:#24282e;color:#9aa3af}.sev-assumed{background:#241f38;color:#c3b1f2}
}
/* A unified diff, drawn the way GitHub draws one: two gutters of line numbers, a colour
   band per side, and the +/- marker inside the code column rather than as a third gutter.
   It is a <table> because the gutters must not scroll away from their line when the code
   overflows horizontally, and because copying a hunk should copy the code and not the
   numbers — `user-select:none` on the gutters is what buys that. */
:root { --diff-add-bg:#e6ffec; --diff-add-gutter:#ccffd8;
        --diff-del-bg:#ffebe9; --diff-del-gutter:#ffd7d5; }
@media (prefers-color-scheme: dark) {
  :root { --diff-add-bg:#12261e; --diff-add-gutter:#1b4721;
          --diff-del-bg:#2d1214; --diff-del-gutter:#5c2225; }
}
.ghdiff { border:1px solid var(--line); border-radius:8px; overflow:hidden; margin:.9rem 0; background:var(--card); }
/* The bar itself is the shared `.srcbar` (layout, pills, the file on the right — all of
   it in extract-snippet.py's stylesheet, which is where the snippets get theirs). What is
   local to a diff card is only the skin: this one is a card *header*, so it takes the
   card's edge and its code background, where the same bar over a snippet floats on the
   card's own paper. Styling the seam and not the component is what keeps the two the same
   row wearing one set of rules. */
.ghdiff > .srcbar { margin:0; padding:.45rem .7rem; border-bottom:1px solid var(--line);
               background:var(--code-bg); font:600 12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
.ghdiff > .srcbar .stat { font-weight:700; white-space:nowrap; }
.ghdiff-scroll { overflow-x:auto; }
table.ghdiff-body { border-collapse:collapse; width:100%;
                    font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
table.ghdiff-body td { padding:0 .5rem; vertical-align:top; white-space:pre; }
table.ghdiff-body td.gln { width:1%; min-width:2.6rem; text-align:right; user-select:none;
                          color:var(--muted); opacity:.7; border-right:1px solid var(--line); }
table.ghdiff-body td.code { width:100%; }
table.ghdiff-body tr.add td.code { background:var(--diff-add-bg); }
table.ghdiff-body tr.add td.gln  { background:var(--diff-add-gutter); }
table.ghdiff-body tr.del td.code { background:var(--diff-del-bg); }
table.ghdiff-body tr.del td.gln  { background:var(--diff-del-gutter); }
table.ghdiff-body tr.hunk td { background:var(--code-bg); color:var(--muted); font-style:italic;
                               border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
.ghdiff-note { margin:0; padding:.4rem .7rem .55rem; color:var(--muted); font-size:.86rem;
               border-top:1px solid var(--line); }
.ghdiff-note a.srcref { margin:0 .6rem 0 0; }
table.stat { border-collapse:collapse; width:100%; font-size:.88rem; }
table.stat td { border-bottom:1px solid var(--line); padding:.35rem .5rem; }
table.stat td.n { text-align:right; color:var(--muted); font-family:ui-monospace,Menlo,monospace; white-space:nowrap; }
.titlerow { display:flex; align-items:baseline; justify-content:space-between; gap:1.5rem;
             flex-wrap:wrap; }
.titlerow h1 { margin-bottom:0; }
/* One block that never scrolls away: which change this is, what it is against, how big
   it is, and where in it you are. Those four answers are the ones a reader needs *while*
   reading a tab -- "which branch is this, again?" is asked halfway down a diff, not at
   the top -- and the strip alone, pinned over a masthead that had scrolled off, answered
   none of them. Full-bleed like the strip was, padded back to the text column. */
.masthead { position:sticky; top:0; z-index:31; background:var(--bg);
            margin-left:calc(50% - 50vw); width:100vw;
            padding:.35rem max(1.25rem, calc(50vw - 540px + 1.25rem)) 0;
            border-bottom:1px solid var(--line); }
.masthead .titlerow { align-items:baseline; gap:.3rem .9rem; }
/* One line, always: the PR and its name on the left, the score hard right. Nothing here
   is allowed to wrap the masthead onto a second row, so the title is the part that gives
   — it ellipsises rather than pushing the score down, and the full text is in the tab
   title anyway. `min-width:0` is what lets a flex child shrink below its content. */
.titlerow.oneline { flex-wrap:nowrap; align-items:baseline; }
.titlerow.oneline h1 { min-width:0; overflow:hidden; text-overflow:ellipsis;
                        white-space:nowrap; }
.titlerow.oneline .titlescore { flex:0 0 auto; }
/* The served/static badge rides the title row, against the score: pushed hard right
   with the pill, so the title keeps the whole left and the two facts read as one. */
.titlerow .chip-mode { flex:0 0 auto; margin-left:auto; align-self:center; }
.titlerow .chip-mode + .titlescore { margin-left:0; }
.masthead .scopebar { margin:.3rem 0 .05rem; }
/* Inside the masthead the strip is no longer its own sticky, full-bleed band: the block
   around it does the bleeding, the pinning and the edge. */
.masthead .tabstrip { position:static; margin:.1rem 0 0; width:auto;
            padding:.1rem 0 .15rem; background:transparent; border-bottom:0; }
/* The two refs the whole page is a comparison of: the first two chips on the scope bar,
   each one click from its own page on GitHub. Monospaced names, because a git ref is a
   name to be matched character for character against a terminal, and the branch under
   review takes the link colour so the pair reads head-then-base at a glance. */
.chip.refchip b.refname { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:.95em; }
.chip.refchip b.refname.head { color:var(--link); }
/* The `!` on the base chip: the pair of refs this page compares has drifted since it was
   built. Amber and not the page's red — a stale base is not a finding about the code, it
   is a caveat about the whole page, and dressing it in the same colour as a bug would
   have it read as the thirteenth thing /code-review raised. It is a filled disc rather
   than a bare glyph so it survives being skimmed past at the end of a row of chips, and
   the chip it sits on keeps its border so the ref itself stays the thing being read. */
.chip.refchip .drift { display:inline-flex; align-items:center; justify-content:center;
            width:1.05em; height:1.05em; margin-left:.35rem; border-radius:50%;
            background:var(--drift); color:var(--drift-fg); font-weight:700;
            font-size:.78em; line-height:1; font-style:normal; cursor:help; }
.chip.refchip.drifted { border-color:var(--drift); }
h1 .prref { text-decoration:none; }
h1 .prref:hover { text-decoration:underline; }
.titlescore { display:inline-flex; align-items:baseline; gap:.35rem; padding:.3rem .8rem;
              border-radius:999px; white-space:nowrap; }
.titlescore b { font-size:1.5rem; line-height:1; letter-spacing:-.02em; }
.titlescore small { font-size:.8rem; opacity:.6; }
.titlescore i { font-style:normal; font-size:.82rem; opacity:.85; margin-left:.25rem; }
/* The score is a link now — to the tab holding the findings that produced it, which is
   the next thing anyone reading `5/10 not yet mergeable` wants. It keeps every colour it
   had: an underline or a link colour on a pill that is already coloured by its verdict
   would read as a second state, not as an affordance. The cursor and a hover lift say it
   is clickable, and the tooltip says where it goes. */
a.titlescore { text-decoration:none; cursor:pointer; }
a.titlescore:hover { filter:brightness(1.06); box-shadow:0 0 0 1px currentColor inset; }
.titlescore.v-good { background:rgba(46,158,91,.16); color:#1f7a45; }
.titlescore.v-mid  { background:rgba(217,130,24,.18); color:#9a5b06; }
.titlescore.v-bad  { background:rgba(215,38,61,.16); color:#d7263d; }
@media (prefers-color-scheme:dark) {
  .titlescore.v-good { color:#6fce93; } .titlescore.v-mid { color:#e0a44a; }
  .titlescore.v-bad { color:#f0757f; } }

/* Full-bleed band: the verdict is the one thing that should not sit politely inside the
    text column. It breaks out to the viewport edges and pads itself back to the column. */
.verdict { margin:1.6rem 0 2.2rem; margin-left:calc(50% - 50vw); width:100vw;
            padding:1.5rem max(1.25rem, calc(50vw - 540px + 1.25rem));
            display:grid; grid-template-columns:auto 1fr; gap:2rem; align-items:center;
            border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
.verdict .score { text-align:center; max-width:16rem; }
/* The label sits in the score column, so a long one used to stretch that column across
    most of the band — leaving the bullets in a ~180px gutter beside 600px of empty
    gradient. Cap the column, and stop tracking-out a sentence: uppercase letter-spacing
    is for a two-word verdict, not for a paragraph. */
.verdict .score span { max-width:16rem; margin:.45rem auto 0; }
.verdict .score b { display:block; font-size:3.4rem; line-height:1; letter-spacing:-.04em; }
.verdict .score span { display:block; font-size:.78rem; line-height:1.35; opacity:.8;
                        text-transform:none; letter-spacing:0; }
.verdict .scale { display:flex; gap:2px; margin:.6rem 0 0; }
.verdict .scale i { width:9px; height:9px; border-radius:2px; background:currentColor; opacity:.18; }
.verdict .scale i.on { opacity:1; }
.verdict ul { margin:0; padding:0; list-style:none; display:grid; gap:.42rem; }
.verdict li { color:var(--fg); font-size:.95rem; padding-left:1rem; position:relative; }
.verdict li::before { content:""; position:absolute; left:0; top:.62em; width:5px; height:5px;
                      border-radius:50%; background:currentColor; }
.v-bad  { color:#c62828; background:linear-gradient(90deg,#fbdcdc 0%,#fdefef 42%,transparent 88%); }
.v-mid  { color:#b56b00; background:linear-gradient(90deg,#fbe8c9 0%,#fdf5e6 42%,transparent 88%); }
.v-good { color:#2e7d32; background:linear-gradient(90deg,#d6ecd8 0%,#eef7ef 42%,transparent 88%); }
@media (prefers-color-scheme: dark) {
  .v-bad {color:#f08a8a;background:linear-gradient(90deg,#4a2020 0%,#2a1818 42%,transparent 88%)}
  .v-mid {color:#e6b566;background:linear-gradient(90deg,#453515 0%,#282010 42%,transparent 88%)}
  .v-good{color:#8fd39c;background:linear-gradient(90deg,#1e3d24 0%,#172318 42%,transparent 88%)}
}
.vidwrap { display:grid; grid-template-columns:minmax(0,1fr) 19rem; gap:.9rem;
            align-items:start; margin:1rem 0; }
.vidwrap video { width:100%; display:block; border:1px solid var(--line);
                  border-radius:8px; background:#000; }
.transcript { margin:0; padding:.3rem; list-style:none; border:1px solid var(--line);
              border-radius:8px; background:var(--card); max-height:26rem; overflow-y:auto; }
.transcript li { display:grid; grid-template-columns:2.9rem 1fr; gap:.5rem; align-items:baseline;
                  padding:.4rem .5rem; border-radius:5px; cursor:pointer; font-size:.88rem; }
.transcript li:hover { background:var(--accent-soft); }
.transcript li.on { background:var(--accent-soft); font-weight:600; }
/* The coverage row is not a cue: no frame to seek to, so no pointer and no hover
   highlight, and a ruled edge above it to say the walkthrough has ended.
   Stuck to the floor of the scroller rather than merely last in it. The cue list is a
   fixed-height box that already overflows at six captions, so "last child" is another way
   of saying "below the fold" — and a statement about what the film does NOT cover is
   exactly the one a reader will never scroll to look for. Pinned, it is always the last
   thing under the captions and still costs the page no height at all. */
.transcript li.uncovered { position:sticky; bottom:-.3rem; z-index:1;
                cursor:default; color:var(--muted); border-top:1px solid var(--line);
                border-radius:0; margin-top:.3rem; padding:.55rem .5rem .4rem;
                background:var(--card); }
.transcript li.uncovered:hover { background:var(--card); }
.transcript li.uncovered .ts { color:var(--muted); opacity:.6; }
.transcript li.uncovered b { color:var(--fg); }
.transcript .ts { font:600 11.5px/1.5 ui-monospace,Menlo,monospace; color:var(--link); }
/* The bar that turns a static page into a live one. It sits above the player rather
    than inside .vidwrap, which is a two-column grid the video and the transcript own. */
.appenv { border:1px solid var(--line); border-radius:var(--r); background:var(--card);
    padding:.55rem .7rem; margin:0 0 .7rem; display:flex; flex-direction:column;
    gap:.4rem; font-size:12.5px; }
.appenv code { font:12px/1.6 ui-monospace,Menlo,monospace; background:var(--accent-soft);
    border-radius:4px; padding:.15rem .4rem; user-select:all; }
/* The row of verbs, then the address they all act on. Two lines and not one: the command
    underneath is the same offer spelled out for a terminal, and the reader chooses a
    route rather than reading one long line of mixed controls. */
.appenv .appenv-run { display:flex; flex-wrap:wrap; gap:.4rem .5rem; align-items:center; }
.appenv .appenv-title { font-weight:600; margin-right:.2rem; }
/* The state and the address sit where the row starts reading, not off at the far right:
    there is at most one of them at a time, and it is the subject of the verbs after it. */
.appenv .appenv-at { display:flex; gap:.35rem; align-items:center; min-width:0; }
.appenv .appenv-manual { display:flex; gap:.4rem; align-items:center; margin:0;
    min-width:0; color:var(--fg); }
.appenv .appenv-manual code { overflow-x:auto; white-space:nowrap; flex:1; min-width:0; }
.appenv button { font:inherit; cursor:pointer; border:1px solid var(--line); border-radius:4px;
    background:var(--bg); color:var(--fg); padding:.2rem .55rem; white-space:nowrap; }
.appenv button:hover { background:var(--accent-soft); }
/* The address *is* the link — one thing to read and one thing to click, carrying the
    port this instance happened to get. Shown only while something answers there. */
.appenv .appenv-url { font:12px/1.6 ui-monospace,Menlo,monospace; color:var(--link);
    text-decoration:none; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.appenv .appenv-url:hover { text-decoration:underline; }
/* Off disk no verb in this row can run: there is no process here to run a command, and
    Reset needs one to have been started. So the command takes their place rather than
    standing beside them greyed — one route offered once, instead of the same offer made
    twice in two registers. Served, the command goes the other way for the same reason. */
.appenv:not(.appenv-served) .appenv-start,
.appenv:not(.appenv-served) .appenv-stop,
.appenv:not(.appenv-served) .appenv-reset { display:none; }
.appenv.appenv-served .appenv-manual { display:none; }
/* Three states, and the page must never claim the third without having asked: unknown
    until the probe answers, then live — where the address speaks for it and the pill
    steps out of the way — or down, where "Offline" is the whole truth there is. */
.appenv .appenv-state { font-weight:600; }
.appenv .appenv-state[data-state="down"] { color:var(--muted); }
/* "Starting…" can stand there for a whole docker build, and a line of text that never
    moves is how a page says it has hung. The spinner is the row's only evidence that
    something is still happening — the last line the command printed is in the tooltip,
    which you have to know to go looking for.

    A `::before` and not an element: the pill's text is written with textContent, which
    would wipe any child out from under the script on the next status line. A ring drawn
    in CSS and not a .gif: it inherits the pill's own colours, so it themes with the page
    in dark mode, stays sharp on a retina screen, and keeps this report one file that
    works off disk with no asset beside it.

    Invisible for its first 300ms, because `unknown` is also the state of the probe on
    every page load — a fetch that answers in milliseconds — and a spinner that flashes
    once per load is the page twitching, not the page working. */
@keyframes appenv-spin { to { transform:rotate(360deg); } }
@keyframes appenv-reveal { to { opacity:1; } }
.appenv .appenv-state[data-state="unknown"]::before {
    content:""; display:inline-block; width:.8em; height:.8em; margin-right:.4em;
    vertical-align:-.1em; box-sizing:border-box; border-radius:50%;
    border:2px solid var(--line); border-top-color:var(--link); opacity:0;
    animation:appenv-spin .7s linear infinite, appenv-reveal .12s linear .3s forwards; }
/* Slowed rather than stopped: it is the only thing on the page saying the wait is not a
    hang, and a still ring says the opposite of what it is there for. */
@media (prefers-reduced-motion: reduce) {
  .appenv .appenv-state[data-state="unknown"]::before { animation-duration:2.6s, .12s; }
}
/* A link the running app can answer looks like the caption links around it; one that has
    nowhere to point yet must not look clickable, because it is not. */
.transcript a[data-app].dead { color:var(--muted); text-decoration:none; cursor:default; }
/* The way into the app at this exact moment. Quiet until the row is hovered: the reader
    is here to read the narration, and six play glyphs down the margin would compete with
    the timestamps for the same job. */
.cue-drive { float:right; margin-left:.4rem; border:1px solid var(--line); border-radius:3px;
    background:var(--bg); color:var(--muted); cursor:pointer; line-height:1;
    padding:.05rem .3rem; font-size:11px; opacity:0; transition:opacity .12s; }
.transcript li:hover .cue-drive, .cue-drive:focus, .cue-drive.copied { opacity:1; }
.cue-drive:hover, .cue-drive.copied { color:var(--link); border-color:var(--link); }
/* Nothing is running, so there is nowhere to drive to. Shown rather than hidden: the row
    still says this moment is reachable, once something is up. */
.cue-drive[aria-disabled="true"] { cursor:not-allowed; opacity:.35; }
.transcript li:hover .cue-drive[aria-disabled="true"] { opacity:.35; }
/* A verb the row is mid-way through running: hidden a moment later by the same call that
    disarms it, but greyed for the frame in between rather than flickering live. */
.appenv button[aria-disabled="true"] { cursor:not-allowed; opacity:.45; }
/* A caption is a seek target, so a link inside one has to read as a *different* affordance
    without shouting: the page's own link colour and the dotted underline it already uses
    for .srcref, solid on hover. The click separation is in the script, not here. */
.transcript a { color:var(--link); text-decoration:none;
                border-bottom:1px dotted currentColor; }
.transcript a:hover { border-bottom-style:solid; }
@media (max-width:820px) { .vidwrap { grid-template-columns:1fr; } }
video.vid { width:100%; max-width:900px; border:1px solid var(--line); border-radius:8px; display:block; margin:1rem 0; background:#000; }
footer { margin-top:3.5rem; padding-top:1rem; border-top:1px solid var(--line); color:var(--muted); font-size:.85rem; }
/* The tab strip. A review is not one argument read top to bottom — it is five or six
    separate questions ("does the contract still hold?", "where did it land?"), and a
    reviewer answers them in whatever order their doubt takes them. Full-bleed and
    sticky, so the questions stay reachable from anywhere in an answer. */
/* Two rows is the strip's normal state at 1280px and 1440px -- its inner track is pinned
   at ~1140px by the padding formula and fifteen pills need ~1210px -- so the height that
   matters is the height of a row, paid twice, every scroll. Trimmed at three points: the
   strip's own vertical padding, the row gap, and the pill line-height below. Nothing here
   is a fixed height: `--strip-h` is still measured off the rendered box. */
.tabstrip { position:sticky; top:0; z-index:30; margin:.5rem 0 .85rem;
            margin-left:calc(50% - 50vw); width:100vw;
            padding:.3rem max(1.25rem, calc(50vw - 540px + 1.25rem));
            background:var(--bg); border-bottom:1px solid var(--line);
            display:flex; gap:.2rem .3rem; flex-wrap:wrap; align-items:center; }
/* The right padding is relaxed by ~80px into the full-bleed the strip already has, so
   the pills get a track wider than the text column and stay on one row for one tab
   longer. The left padding is untouched, so the first tab stays aligned with the body
   text. It was relaxed originally to keep `show all` from dropping to a second row; that
   button has since moved to the foot of the page, and the room it was making is now the
   tabs' own — which is why there is no longer a `.grow` spacer pushing anything right. */
.tabstrip { padding-right: max(1.25rem, calc(50vw - 620px + 1.25rem)); }
/* The strip is the top of the page once the masthead has scrolled away, so it carries
   the edge that used to be the masthead's: a shadow under the border, only while it is
   actually pinned, so content passing beneath it reads as passing *under* something. */
.tabstrip.pinned, .masthead.pinned { box-shadow:0 2px 6px rgba(0,0,0,.13); }
/* Struck through, not hidden: the tab still holds the current state as context, and a
   reviewer who cannot see that it exists cannot tell it was considered. */
button.tab.quiet { text-decoration:line-through; text-decoration-thickness:1px; opacity:.5; }
button.tab.quiet:hover, button.tab.quiet[aria-selected="true"] { opacity:.85; }
button.tab { border:1px solid transparent; background:none; color:var(--muted); border-radius:999px;
             cursor:pointer; font:600 .87rem/1.8 inherit; padding:0 .85rem; white-space:nowrap;
             display:inline-flex; align-items:center; gap:.42rem; }
button.tab:hover { color:var(--fg); background:var(--card); border-color:var(--line); }
button.tab[aria-selected="true"] { background:var(--fg); color:var(--bg); border-color:var(--fg); }
button.tab .n { font:700 .7rem/1 ui-monospace,Menlo,monospace; opacity:.6;
                font-variant-numeric:tabular-nums; }
/* A tab the reviewer must not skip — a blocked merge — is red. It used to wear a `!` in
   a red circle beside its name, which is the same fact said as an ornament: a glyph the
   reader has to decode hung off a word that could simply have carried the colour itself.
   The label *is* the alarm now. Selected, the strip inverts everything, so the red moves
   to the fill and the word turns white rather than losing the one thing marking it. */
button.tab.alarm { color:#c62828; }
button.tab.alarm:hover { color:#a41f1f; background:var(--card); border-color:#c62828; }
button.tab[aria-selected="true"].alarm { background:#c62828; color:#fff; border-color:#c62828; }
/* Same alarm, one notch down: a required approval that is not the escalation of last
   resort (an ordinary CODEOWNERS reviewer, not the team guarding the guardrails) reads
   as amber rather than red, so the strip does not cry wolf on a routine sign-off. */
button.tab.warn { color:#b56b00; }
button.tab.warn:hover { color:#8f5500; background:var(--card); border-color:#b56b00; }
button.tab[aria-selected="true"].warn { background:#b56b00; color:#fff; border-color:#b56b00; }
/* A verdict the strip can carry without words: green nothing changed, amber changed
   but nothing breaks, red a caller breaks. A number there ("+3") counted changes,
   which is not the question anyone opens that tab with. */
button.tab .n.dot-green, button.tab .n.dot-amber, button.tab .n.dot-red {
  width:9px; height:9px; border-radius:50%; opacity:1; font-size:0; padding:0;
  display:inline-block; vertical-align:middle; }
@media (prefers-color-scheme:dark) {
  button.tab.alarm { color:#ff8a8a; }
  button.tab.alarm:hover { color:#ffb0b0; border-color:#ff8a8a; }
  button.tab[aria-selected="true"].alarm { background:#ff8a8a; color:#1d1d24;
                                           border-color:#ff8a8a; }
  button.tab.warn { color:#e6c07b; }
  button.tab.warn:hover { color:#f0d39a; border-color:#e6c07b; }
  button.tab[aria-selected="true"].warn { background:#e6c07b; color:#1d1d24;
                                          border-color:#e6c07b; }
}
button.tab .n.dot-green { background:#2e9e5b; }
button.tab .n.dot-amber { background:#d98218; }
button.tab .n.dot-red   { background:#d7263d; }
button.tab .sev { width:6px; height:6px; border-radius:50%; background:var(--accent); }
/* This used to be the last pill on the tab strip, where it sat in the corner of every
   screenful for the whole read and was pressed roughly never — a permanent control for an
   occasional act. It lives at the foot of the page now, which is where you arrive having
   finished reading and is the moment the thing it offers ("show me all of it at once, so
   ⌘F works") is actually worth wanting.
   Under the footer's line, centred: it sat at the far end of that line for a while and
   read as one more word of it. Alone on its own line, in the middle, it is unmistakably
   the page's one control — the sentence above keeps its natural width and `flex-wrap`
   still folds the two halves of that sentence on a narrow screen. */
footer .footrow { display:flex; align-items:baseline; gap:.6rem 1.2rem; flex-wrap:wrap; }
footer .allbar { display:flex; justify-content:center; align-items:center; gap:.6rem;
                 margin-top:.9rem; }
button.allbtn { border:1px solid var(--line); background:var(--card); color:var(--muted);
                border-radius:999px; cursor:pointer; font:600 .74rem/1.9 inherit; padding:0 .7rem; }
button.allbtn:hover { color:var(--fg); border-color:var(--link); }
button.allbtn[aria-pressed="true"] { background:var(--link); border-color:var(--link); color:#fff; }
.panel[hidden] { display:none; }
/* A hash lands the panel top flush against the viewport, where the sticky strip sits
   on top of it and eats the first line. Push the scroll target down past the strip.
   The push is the strip's OWN measured height (TABS_JS keeps `--strip-h` in step with
   it, on load and on every resize), not a constant: the strip wraps to a second row
   whenever the pills outgrow the track, and a literal tall enough for one row clips
   every deep-linked heading the moment a tab is added. The fallback in the calc() is
   one row, for the instant before the script runs and if it never does.
   It applies to anything addressable inside a panel, not just the panel: a hash may
   name a heading halfway down one, and `scrollIntoView` honours the target's own
   scroll-margin, not its ancestor's. */
.panel, .panel [id] { scroll-margin-top: calc(var(--strip-h, 2.6rem) + .6rem); }
.panel > h2:first-child, .panel > .paneltag + h2 { margin-top:.2rem; }
/* Only meaningful once every panel is on screen at once, which is what "show all"
    (and printing) do — otherwise the heading names the tab you are already on. */
.paneltag { display:none; margin:2.6rem 0 0; font:700 .72rem/1.6 inherit; letter-spacing:.1em;
            text-transform:uppercase; color:var(--muted); }
body.showall .paneltag { display:block; }
body.showall .panel { border-top:1px solid var(--line); }
body.showall .panel:first-of-type { border-top:0; }
/* A test and the sequence its run recorded are one exhibit, not two: the diagram is
   evidence for the test directly above it. One rounded card holds the pair together, and
   one fold puts the whole exhibit away — closing over the test alone used to leave its
   diagram standing there with nothing above it to explain what it draws.
   A card rather than the ruled left edge it used to be, because shut is the state this tab
   opens in: six edges down the left of a page are six marks in a margin, while six cards
   are six things to click, which is what they are. */
.testpair { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:.4rem .95rem; margin:.55rem 0; }
/* The same margin open and shut, deliberately. They used to differ — a shut pair took
   .3rem and an open one 1.5rem above it — and opening the FIRST pair therefore pushed the
   whole list down by the difference, which read as a gap appearing above a list nobody had
   touched. The air an open exhibit needs goes inside the card, where it cannot move
   anything but its own contents. */
details.testpair[open] { padding-bottom:.8rem; }
/* Where the 🕵️ landed. A pair looks exactly like the three pairs around it, and the
   reader arrives a whole tab away from the row they clicked, so the pair says once that
   it is the one that was asked for. Its own border does the saying — nothing moves,
   nothing is added, and a second click on a second row can say it again. A permanent
   mark would still be there then, pointing at the previous question. */
@keyframes seq-flash { from { border-color:var(--link); } to { border-color:var(--line); } }
.testpair.seq-hit { animation:seq-flash 2s ease-out 1; }
.testpair > .snippet { margin:.5rem 0 0; }
/* Inside the card the picture needs no frame of its own: a bordered box inside a bordered
   box is two statements where the reader is looking at one thing. The card is the frame,
   and what is left of the diagram is the diagram. */
.testpair > .diagram { background:transparent; border:0; border-radius:0;
  padding:0; margin:.55rem 0 0; }
/* The quoted test, above the picture it explains, behind one control.
   Closed on load: the tab is about the drawing, and the source is one click away.
   That control is a single row — `Show Test`, then the file the bar names, the lines it
   quotes and what changed in them — because the row that used to sit above the bar said
   the line numbers a second time and nothing else. `SEQFOLD_JS` keeps a click on one of
   the bar's own links from toggling the fold under it. */
details.testsrc { margin:.55rem 0 0; }
details.testsrc > summary { cursor:pointer; list-style:none; display:flex; gap:.5rem;
  align-items:center; color:var(--muted); font-size:.82rem;
  font-family:ui-monospace,Menlo,monospace; padding:.15rem 0; }
details.testsrc > summary::-webkit-details-marker { display:none; }
details.testsrc > summary::before { content:"▾"; font-size:.75rem; }
details.testsrc:not([open]) > summary::before { content:"▸"; }
details.testsrc > summary:hover > .foldlbl { color:var(--link); }
/* Two words for two states, in the element's own content rather than in the markup: the
   fold is a `<details>`, so the browser already knows which state it is in and nothing has
   to be told about it when it changes. */
.foldlbl { flex:0 0 auto; }
details.testsrc > summary > .foldlbl::after { content:"Show Test"; }
details.testsrc[open] > summary > .foldlbl::after { content:"Hide Test"; }
/* The hoisted bar keeps its own right alignment and takes the rest of the row. */
details.testsrc > summary > .srcbar { flex:1 1 auto; margin:0; }
/* What the branch did to the quoted file, drawn rather than spelled — the Tests tab's own
   glyph, at the size the marks beside it are. `NEW FILE` in caps was read before the file
   name it is a fact about; the words are on the hover, and the `+` column in the gutter
   below counts the lines the caps used to. Green for a file that did not exist, amber for
   one this branch edited, the page's own quiet grey for one it left alone. */
.srcbar .filemark { display:inline-flex; flex:0 0 auto; cursor:help; margin-left:-2px;
  --fm:#1a7f37; }
.srcbar .filemark[data-kind=edited] { --fm:var(--drift); }
.srcbar .filemark[data-kind=unchanged] { --fm:var(--muted); }
.srcbar .filemark svg { display:block; width:15px; height:15px; overflow:visible; }
.srcbar .filemark .fm-page, .srcbar .filemark .fm-mark { fill:var(--fm); }
@media (prefers-color-scheme: dark) {
  .srcbar .filemark { --fm:#56d364; }
}
details.testsrc > .snippet { margin:.4rem 0 0; }
.testlead { margin:.7rem 0 0; }
/* The fold's own summary: the names of the scenarios drawn inside it. It used to be the
   file name, set quiet and monospace because it was a control beside a picture that was
   already on screen. Folded shut, it is the only thing on screen — the tab's table of
   contents, one line per test — so it is set as what it now is: prose, at the page's own
   weight, in the page's own face. Monospace was for a path; these are sentences. */
details.testpair > summary { cursor:pointer; list-style:none; display:inline-flex; gap:.4rem;
  align-items:baseline; color:var(--fg); font-size:.95rem; font-weight:600;
  padding:.25rem 0; }
details.testpair > summary::-webkit-details-marker { display:none; }
details.testpair > summary::before { content:"▾"; font-size:.75rem; }
details.testpair:not([open]) > summary::before { content:"▸"; }
details.testpair > summary:hover { color:var(--link); }
/* What kind of test drew the sequence, in the chip the Tests tab wears for the same three
   words and in the same three colours — `.evi.e2e` / `.api` / `.unit` a few hundred lines
   up are that palette, and the requirements map's own `--rm-*-bg` are the same hues again.
   One vocabulary for "what level is this test at", drawn the same way wherever the page
   says it, so a reader who learnt the blue pill on one tab is not taught it twice.
   `baseline` alignment on the summary would hang the pill off the sentence's baseline and
   leave its rounded bottom below the row; it is a mark ABOUT the line, so it centres.
   No `cursor:help`, deliberately: it lives inside the summary, and a click on it opens the
   fold like a click anywhere else on the row. The question mark is for a mark that only
   explains itself — TIP_JS says so, and excludes everything inside a summary for it. */
.testcat { flex:0 0 auto; align-self:center; min-width:2.6rem;
  text-align:center; padding:2px 7px; border-radius:999px;
  font:700 .62rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.08em; text-transform:uppercase;
  background:color-mix(in srgb, var(--tc) 13%, transparent); color:var(--tc); }
.testcat[data-cat=e2e]  { --tc:#164691; }
.testcat[data-cat=api]  { --tc:#5c2c8e; }
.testcat[data-cat=unit] { --tc:#0f5f5f; }
/* The hover is the legend, so the chip must not also take the summary's link colour when
   the row under it is hovered — a pill that changes colour reads as a second control. */
details.testpair > summary:hover > .testcat { color:var(--tc); }
@media (prefers-color-scheme: dark) {
  .testcat { background:color-mix(in srgb, var(--tc) 16%, transparent); }
  .testcat[data-cat=e2e]  { --tc:#a0c8ff; }
  .testcat[data-cat=api]  { --tc:#d2b4ff; }
  .testcat[data-cat=unit] { --tc:#87e4e4; }
}
/* With the title gone, the .puml path is the only thing left in a paired diagram's
   header row, and `space-between` would park it on the left under the fold arrow. */
.diagram.dgm-bare .head .dgm-src { margin-left:auto; }
/* …and then the header row went away entirely. A paired card had two rows of furniture
   stacked on top of each other, each half empty: Diff/New/Old hard left on one, the
   `.puml` path hard right on the other, a band of nothing between them. They are one
   line about one picture — the controls, then what the picture is — so `DGM_VIEWS_JS`
   moves the header's contents into the button bar and drops the empty row. The pieces
   keep their own alignment; only the row they sit on changed. */
.dgmbar .badge { margin-left:.2rem; }
.dgmbar .dgm-src { margin-left:auto; color:var(--muted); font:inherit; text-decoration:none;
  border-bottom:1px dotted var(--line); }
.dgmbar .dgm-src:hover { color:var(--link); border-bottom-color:currentColor; }
.diagram.dgm-bare > .head:empty { display:none; }
@media print {
  .tabstrip { display:none; }
  .panel[hidden] { display:block !important; }
  .paneltag { display:block; }
}
"""


# Emitted *after* every other stylesheet — the fragments' own CSS included — because these
# rules exist to outrank the base sheet's `button.tab { padding:0 .85rem }`. Anywhere
# earlier in the block and the cascade quietly reverts them, with no error and no visible
# clue beyond a tab strip that has silently wrapped onto two rows.
LATE_CSS = """
/* An eleventh tab does not fit, and no amount of window is going to help: the strip's
   inner track is pinned to 1120px at every viewport by its own padding formula, and the
   ten tabs plus `show all` already needed 1115.5px of it — 4.5px of slack. "Spec changes"
   is 113.7px wide and needs 118.5px with its gap, so the strip wrapped to two rows (34px
   → 55.8px) at 1280px, 1440px and 1920px alike. Three shavings, cheapest first: the
   spacer stopped reserving a 1rem basis it never drew (+16px); the right padding drops to
   its floor, which the strip's full bleed already covers (+20px at 1280, +100px at 1440);
   and every pill gives up .25rem of horizontal padding (+88px across eleven of them).
   Budget at 1280px, the narrowest width that has to hold: 1140px of track, 1130px used.
   The LEFT padding is deliberately untouched — the first tab still starts exactly where
   the body text does.
   Two of those three are now free money: `show all` moved to the foot of the page and
   took its ~72px and the spacer with it, so the strip is a row of tabs and nothing else.
   The shavings stay — they are what buys the row back the next time a tab is added. */
.tabstrip { padding-right:1.25rem; }
button.tab { padding:0 .6rem; }
/* A thirteenth tab, and the row the strip wraps onto is no longer paid once on the way
   past: the masthead keeps it on screen for the whole read. Nothing is abbreviated --
   a tab is named or it is not there -- so the last of the room comes out of the gap
   between a label and its badge and a hair of the type. (There used to be a third
   source, the `show all` button beside them; it is not in the strip any more.) */
button.tab { padding:0 .5rem; font-size:.83rem; line-height:1.7; gap:.32rem; }
/* The strip is the masthead's third row, so it ends where the other two do: at the right
   edge of the text column, in line with the score pill above it and with the panels the
   whole page is made of. It used to be let out to 1.25rem from the window edge -- room
   borrowed when fifteen pills would not fit the column -- and once the pills grew to fill
   their track (below) that borrowed width stopped being invisible: the tab row alone ran
   on past the right edge of everything else on the page. The room is not needed any more.
   Measured at 1280/1385/1512/1728/1920px, the text column is 1040px at every one of them
   (`.wrap` is capped at 1080px) and the twelve tabs measure 931px, so they fit the column
   itself with 109px to spare -- about one more tab's worth of headroom before the strip
   wraps to a second row. Left untouched, as ever: the first tab starts exactly where the
   body text does. */
.masthead .tabstrip { margin-right:0; }

/* The 109px is the growth. A row of pills that stops short of the column's right edge
   reads as a row that broke, next to a title and a scope bar that both run the full
   width, so the slack is shared out over the tabs instead of left in a gap at the end:
   ~9px a pill, at every window width, since the column does not change. Grown from each
   pill's own text width (`auto` basis), so the labels keep their relative sizes rather
   than being squared off into equal columns, and a row that does wrap fills itself the
   same way -- which `justify-content:space-between` could not do without flinging a short
   last row's two tabs to opposite edges of the page. */
button.tab { flex:1 1 auto; justify-content:center; }

/* pb33f's report is a whole application in one file — its own tabs, its own diff view,
   its own theme — so it is embedded as a document rather than picked apart and re-drawn
   in this page's styles. A document is also a fence: the frame is a separate origin off
   `file://`, so nothing in it can reach this page and this page's find bar cannot reach
   into it. That is the trade, and it is why the finding is stated above in text and the
   frame is left to be the evidence. */
.diagram .head .dgm-src { color:var(--muted); font:inherit; text-decoration:none;
  border-bottom:1px dotted var(--line); }
.diagram .head .dgm-src:hover { color:var(--link); border-bottom-color:currentColor; }
.oacframe { display:block; width:100%; height:760px; margin:1rem 0 1.4rem;
            border:1px solid var(--line); border-radius:8px; background:#12111a; }
/* The Swagger-shaped diff is framed the same way, but it is not a dark-only
   application the way pb33f's is: it follows the system theme, exactly as this
   page does. So the frame gets the page's own card colour rather than pb33f's
   near-black, and the two documents agree at the seam in both themes. */
.oaviframe { display:block; width:100%; height:820px; margin:1rem 0 1.4rem;
             border:1px solid var(--line); border-radius:8px; background:var(--card); }
"""


CAPTION_JS = """<script>
document.querySelectorAll('.vidwrap').forEach(function (wrap) {
  var video = wrap.querySelector('video');
  var items = Array.prototype.slice.call(wrap.querySelectorAll('.transcript li[data-t]'));
  if (!items.length) return;
  // A run that failed to record still ships the transcript, with a notice where the player
  // would be. There is nothing to seek, so the captions stay plain text — and nothing here
  // may throw, or the scripts after it never run.
  if (!video) return;
  items.forEach(function (li) {
    li.addEventListener('click', function (ev) {
      // Captions carry links to the pages they describe. A click on one opens that page
      // and nothing else — seeking as well would yank the video out from under a reader
      // who was only following the link.
      if (ev.target.closest && ev.target.closest('a, .cue-drive')) return;
      // Seek, and stop there. Clicking a caption is how a reader *finds* a moment — often
      // one they want to look at, or read around, before watching. Starting playback on
      // that click takes the decision away from them and starts talking; the same rule
      // that keeps the film paused when the tab opens applies to every click after it.
      // The play button is right there, and the frame they asked for is now under it.
      // Seeking alone keeps whatever state the video was in: paused stays paused, and a
      // film already running keeps running from the new point.
      video.currentTime = parseFloat(li.dataset.t);
    });
  });

  // It does NOT play on arrival. A film that starts talking the moment a tab opens
  // interrupts the reader instead of serving them — they may be here for the transcript,
  // or reading with someone next to them. The play button is right there.
  // Leaving the tab still pauses it: sound following you to another tab is worse.
  var panel = wrap.closest && wrap.closest('.panel');
  if (panel) {
    panel.addEventListener('panelhide', function () { video.pause(); });
  }
  video.addEventListener('timeupdate', function () {
    var active = null;
    items.forEach(function (li) {
      if (parseFloat(li.dataset.t) <= video.currentTime) active = li;
    });
    items.forEach(function (li) { li.classList.toggle('on', li === active); });
    if (!active) return;
    // Measured against the panel's own box: offsetTop is relative to the nearest
    // positioned ancestor, which is not necessarily the scroller.
    var panel = active.parentNode;
    var a = active.getBoundingClientRect();
    var p = panel.getBoundingClientRect();
    if (a.top < p.top) panel.scrollTop += a.top - p.top - 8;
    else if (a.bottom > p.bottom) panel.scrollTop += a.bottom - p.bottom + 8;
  });
});
</script>"""


FOCUS_JS = """<script>
// The focus chooser: every level is already in the page, so switching is a visibility
// flip, not a fetch — the guide must keep working as a single emailed file.
(function () {
  document.querySelectorAll('.diagram .focus').forEach(function (bar) {
    var diagram = bar.closest('.diagram');
    bar.addEventListener('click', function (ev) {
      var button = ev.target.closest('button[data-level]');
      if (!button) return;
      var level = button.getAttribute('data-level');
      bar.querySelectorAll('button[data-level]').forEach(function (b) {
        b.setAttribute('aria-pressed', String(b === button));
      });
      diagram.querySelectorAll('.svgbox[data-level]').forEach(function (box) {
        box.hidden = box.getAttribute('data-level') !== level;
      });
    });
  });
})();
</script>"""

DGM_VIEWS_JS = """<script>
// Diff / New / Old. Delegated on `document` rather than bound per widget, so a
// `.dgmviews` that reaches a section body some other way — expanded from `{{drawio:...}}`,
// or written by hand — picks up the identical behaviour with no registration step.
(function () {
  function show(views, state) {
    views.setAttribute('data-state', state);
    views.querySelectorAll(':scope > .dgmpane').forEach(function (pane) {
      pane.hidden = pane.getAttribute('data-view') !== state;
    });
    var pair = (views.closest('.diagram') || views).querySelector('.dgm-newold');
    if (pair) {
      pair.querySelectorAll('u[data-view]').forEach(function (word) {
        word.classList.toggle('on', word.getAttribute('data-view') === state);
      });
    }
    // From the card, not from `views`: on a paired diagram the bar has been lifted out
    // of `.dgmviews` and into the header's row (see the merge below).
    (views.closest('.diagram') || views).querySelectorAll('.dgmbar button[data-go]').forEach(function (b) {
      var go = b.getAttribute('data-go');
      b.setAttribute('aria-pressed',
        String(go === 'diff' ? state === 'diff' : state !== 'diff'));
    });
  }
  // From the delta, the first click lands on New; from New it lands on Old, and back.
  // Two words, one button, and the same answer whichever control you reached for.
  function flip(views) {
    if (!views) return;
    var has = function (v) { return !!views.querySelector(':scope > .dgmpane[data-view="' + v + '"]'); };
    var now = views.getAttribute('data-state');
    var next = now === 'new' ? 'old' : 'new';
    if (!has(next)) next = next === 'new' ? 'old' : 'new';
    if (has(next)) show(views, next);
  }
  document.addEventListener('click', function (ev) {
    var button = ev.target.closest('.dgmbar button[data-go]');
    if (button) {
      var views = button.closest('.dgmviews')
        || (button.closest('.diagram') || document).querySelector('.dgmviews');
      if (button.getAttribute('data-go') === 'diff') show(views, 'diff');
      else flip(views);
      return;
    }
    // The header, but never a link inside it: the source path opens an editor.
    var head = ev.target.closest('.diagram.dgm-toggles > .head');
    if (head && !ev.target.closest('a')) flip(head.parentElement.querySelector('.dgmviews'));
    // Same bargain for the merged row: its empty middle is the large hit area the header
    // used to be, and a click on the path still opens the editor rather than swapping the
    // picture out from under it.
    var bar = ev.target.closest('.diagram.dgm-toggles .dgmbar');
    if (bar && !ev.target.closest('a')) flip(bar.closest('.diagram').querySelector('.dgmviews'));
  });

  // The merge. Only on a paired card (`dgm-bare`), which is the one that lost its title
  // and was left with a header row holding a single right-aligned path — a whole row of
  // page for one filename. Elsewhere the header still carries a real heading and earns
  // its own line.
  //
  // The bar keeps its place inside `.dgmviews`, because the stylesheet paints the buttons
  // off `[data-state]` on that element; what travels is the header's contents. `show` and
  // the click handler are the two places that then have to look the bar up from the card
  // instead of from `views`, and they do.
  document.querySelectorAll('.diagram.dgm-bare > .head').forEach(function (head) {
    var bar = head.parentElement.querySelector('.dgmviews > .dgmbar');
    if (!bar) return;
    while (head.firstChild) bar.appendChild(head.firstChild);
  });
})();
</script>"""


SERVER_JS = """<script>
// Is there a review server behind this page, and what will it run for me?
//
// This replaces `location.protocol === 'http:'`, which answered a different question and
// answered it wrongly in the one place it mattered most. The demo published on GitHub
// Pages is https, so the protocol test said "served": four hundred and seventy-seven
// editor handles on that page each fetched `/__open__` against github.io, collected a
// 404, and toasted "Could not open OwnerController.java" at a reader who had clicked a
// perfectly good link. A page cannot tell a web server from *its* web server by looking
// at the scheme. It has to ask, and the answer has to be one only ours can give — hence
// a JSON body carrying our own key: GitHub's 404 page is HTML and does not parse, and
// something else on :7654 parses but says nothing about human review.
//
// The probe is asynchronous, and everything downstream of it is written to start in the
// degraded state and *rise* when it answers. Never the other way round. A button drawn
// as live that falls back to the clipboard 30ms later has already been clicked by then,
// and has already lied; one that appears capable a moment after the page paints has cost
// nobody anything.
//
// It carries a list of actions rather than a boolean for the same reason. "Am I served?"
// is not what a button needs to know — `cue-drive` needs to know whether *drive-to-cue*
// is on offer here, and a build that stopped declaring it has to be able to take the
// verb away from a page that is still open.
window.HR = (function () {
  var caps = null, settled = false, waiting = [];

  var ready = fetch('/__human_review__', {cache: 'no-store'})
    .then(function (r) { return r.ok ? r.json() : null; })
    // `humanReview` present, or this is somebody else's JSON on somebody else's port.
    .then(function (j) { return (j && j.humanReview) ? j : null; })
    // A file:// page cannot fetch at all, and that throw is the *normal* path for a
    // guide read off disk or out of the zip. It is not a failure to report.
    .catch(function () { return null; })
    .then(function (j) {
      caps = j; settled = true;
      waiting.splice(0).forEach(function (fn) { try { fn(j); } catch (e) {} });
      return j;
    });

  function can(id) { return !!(caps && caps.actions && caps.actions[id]); }

  // Fires once, with the answer, whenever it arrives — before or after registration.
  // `settled` and not `caps !== null`, because "no server" is an answer and null is how
  // it is spelt.
  function onready(fn) { if (settled) fn(caps); else waiting.push(fn); }

  // Poll rather than stream. `./start-docker.sh up` is a docker build: minutes of output
  // this page shows one line of. An EventSource would be a second protocol, a second
  // failure mode and a connection held open across the reap, to deliver six words a
  // second more promptly than a timer does.
  function poll(snap, onprogress) {
    if (onprogress) { try { onprogress(snap); } catch (e) {} }
    if (snap.state !== 'running') return Promise.resolve(snap);
    return new Promise(function (resolve, reject) {
      setTimeout(function () {
        fetch('/__run_status__?run=' + encodeURIComponent(snap.run), {cache: 'no-store'})
          .then(function (r) {
            if (!r.ok) throw new Error('the review server lost track of that run');
            return r.json();
          })
          .then(function (next) { resolve(poll(next, onprogress)); })
          .catch(reject);
      }, 700);
    });
  }

  // Resolves with the finished snapshot ({state, exit, output, result}) whatever the
  // exit code — a command that ran and failed is an answer, not an exception. It rejects
  // only when the *request* could not be made or the run could not be followed, which is
  // the case where the caller has to fall back to the clipboard.
  function run(id, params, onprogress) {
    if (!can(id)) return Promise.reject(new Error(id + ' is not available here'));
    return fetch('/__run__', {
      method: 'POST', cache: 'no-store',
      // Both halves deliberate. POST + a non-simple Content-Type is not a request a
      // cross-origin page may send without a preflight, and the server answers no
      // preflight — so the browser refuses on our behalf before anything arrives. The
      // token is the belt to that pair of braces: it is minted per server process and
      // handed out only over the same-origin-guarded probe above, so a page that never
      // read the probe cannot produce it.
      headers: {'Content-Type': 'application/json',
                'X-Human-Review-Token': (caps && caps.token) || ''},
      body: JSON.stringify({id: id, params: params || {}})
    }).then(function (r) {
      if (r.ok) return r.json();
      // The server explains its refusals in the body — "n is not a valid int",
      // "cue-drive is not an action this review declares" — and a reader who sees the
      // sentence can act on it where a generic shrug leaves them nothing.
      return r.text().then(function (t) { throw new Error(t || 'the review server refused'); });
    }).then(function (first) { return poll(first, onprogress); });
  }

  // The last line the command has printed, for a control with room for one line.
  function tail(snap) {
    var lines = (snap.output || '').split('\\n');
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
    return lines.length ? lines[lines.length - 1].trim() : '';
  }

  // Live reload, the way a dev server does it: the build rewrites `.human-review/`, and
  // the tab showing it catches up on its own.
  //
  // It matters more here than on a dev server. This page is read *while* it is being
  // rebuilt — a reviewer reads a finding, asks for the diagram to be re-rendered or the
  // report to be regenerated, and goes on reading. Until now the only page that reloaded
  // itself was the one whose own button did the rebuilding; a rebuild from the terminal
  // beside it left the reader looking at a report that no longer matched the disk, with
  // nothing on screen to say so. Silent staleness is the failure mode this whole page is
  // built against.
  //
  // Polled, not streamed, and for the reason the run poller gives above: an EventSource
  // is a second protocol and a connection held open across the reap, in exchange for a
  // second of promptness on a page nobody is timing. The server does the debouncing —
  // the stamp only moves once the tree has stopped being written — so a rebuild that
  // takes twenty seconds reloads this tab once, at the end, and not on its first file.
  onready(function (j) {
    if (!j || !j.watch) return;
    var seen = j.watch, misses = 0;
    (function next() {
      // Slower when the tab is in the background: it will be reloaded before anyone
      // looks at it either way, and a dozen parked reports are a dozen pollers.
      setTimeout(function () {
        fetch('/__watch__', {cache: 'no-store'})
          .then(function (r) { if (!r.ok) throw new Error('refused'); return r.json(); })
          .then(function (w) {
            misses = 0;
            // `reload()` and not a cache-buster: the server sends no-store.
            if (w.stamp && w.stamp !== seen) { location.reload(); return; }
            next();
          })
          // The server is mortal by design — idle for `--idle-minutes` and it is gone,
          // under a tab that is still open. That is not an error to report, it is the
          // end of the poll: three tries so a blip does not end it, then silence.
          .catch(function () { if (++misses < 3) next(); });
      }, document.hidden ? 5000 : 1000);
    })();
  });

  onready(function (j) {
    if (!j) return;
    // A play mark in front of the tab's title, where the favicon already is. A reader
    // keeps several of these open — one per branch, a static copy of an old one beside a
    // live one — and the tab strip is where they pick between them, long before anything
    // in the page is on screen. The badge in the title row says the same thing, but only
    // to someone already looking at the page.
    //
    // Play and not a green dot: green on this page means a check passed, and a report
    // whose tab turns green when a server happens to be up would be saying the branch is
    // fine. This says one thing only — something is running behind it.
    if (document.title.indexOf('▶') !== 0) {
      document.title = '▶️ ' + document.title;
    }
    var chip = document.getElementById('hr-mode');
    if (!chip) return;
    chip.textContent = 'served';
    chip.classList.add('chip-served');
    // Nothing left to copy: the line it offered is the one that got the reader here.
    chip.classList.remove('copycmd');
    chip.removeAttribute('data-copy');
    chip.setAttribute('data-tip', 'Served by the review server: buttons run their command '
      + 'from this page, and recordings play in it.');
    // Where an action can actually run, the offer under the diagram changes from `run
    // this` to `click here` and the command stops being shown: the button does the job,
    // and a shell line beside it is for a reader who is not here. Per action and not per
    // page — one block can carry four, and a server that answers for the re-render does
    // not necessarily answer for the rest.
    [].forEach.call(document.querySelectorAll('.rerun button.runhere[data-action]'),
        function (b) {
      if (!can(b.getAttribute('data-action'))) return;
      b.setAttribute('data-tip', b.getAttribute('data-tip-served')
        || b.getAttribute('data-tip'));
      var offer = b.closest ? b.closest('.offer') : null;
      if (offer) offer.classList.add('served');
    });
  });

  return {ready: ready, can: can, onready: onready, run: run, tail: tail};
})();
</script>"""


APP_ENV_JS = """<script>
// The deployed-app row: what is up, and the one or two things you can do about it.
//
// Two copies of this report exist and the row has to be honest in both. Served, it is a
// row of verbs whose state it keeps for you: nothing answering, so Start; something
// answering, so its address as a link, Stop, and Reset DB. Off disk there is no process
// here to run a command, so there are no verbs at all and the bash command stands in
// their place — the only route there is, offered once instead of twice.
//
// A control is hidden when it cannot be used, not greyed. Greying is for a thing you
// could have had under a condition worth teaching; Stop before anything has started is
// not that, it is noise in the four-item row a reader scans in one glance.
//
// Everything is written to start in the degraded state and *rise*. A button drawn as live
// that falls back 30ms later has already been clicked by then, and has already lied.
(function () {
  var bar = document.querySelector('.appenv');
  if (!bar) return;
  var state = bar.querySelector('.appenv-state');
  var addr = bar.querySelector('.appenv-url');
  var startBtn = bar.querySelector('.appenv-start');
  var stopBtn = bar.querySelector('.appenv-stop');
  var reset = bar.querySelector('.appenv-reset');
  var copy = bar.querySelector('.appenv-copy');
  // Raised by the probe in SERVER_JS, never assumed: a page on GitHub Pages is https and
  // is not served by us, and the buttons here must not believe otherwise.
  var served = false;
  var links = Array.prototype.slice.call(document.querySelectorAll('a[data-app]'));
  // Per page, not per machine: two review pages describe two branches, and each branch
  // gets its own instance on its own port.
  var KEY = 'human-review:appbase:' + location.pathname;

  function stored() {
    // A browser with site data blocked throws on read; the page must still work.
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function remember(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  // The address the row is about. There is no box to type one into any more — served,
  // Start prints it and we scrape it; off disk, the build's own `base` is the only guess
  // anyone had — so it lives here and in localStorage, which is what survives a reload.
  var current = stored();
  function base() {
    return (current || bar.dataset.fallback || '').replace(/\\/+$/, '');
  }

  function apply() {
    var b = base();
    links.forEach(function (a) {
      var path = a.dataset.app;
      if (b) { a.href = b + path; a.classList.remove('dead'); }
      // No base and no fallback: the link has nowhere to point, and saying so by going
      // grey is honest where a live-looking link that 404s is not.
      else { a.removeAttribute('href'); a.classList.add('dead'); }
    });
  }

  var blocked = function (el) { return el.getAttribute('aria-disabled') === 'true'; };
  function arm(el, on, tip) {
    if (!el) return;
    el.setAttribute('aria-disabled', on ? 'false' : 'true');
    el.dataset.tip = tip;
  }
  // Disarmed *and* gone. Both, and in that order, because `hidden` is a style and a
  // stylesheet that failed to load would otherwise leave a live button behind.
  function gate(el, on, tip) { if (!el) return; arm(el, on, tip); el.hidden = !on; }

  // The row's whole truth, in one call. Each verb is gated on the thing it actually needs
  // — Start and Stop on a server to run them, Reset on something being up to reset —
  // because a control that can be pressed while its precondition is missing is a control
  // that lies: Reset would fail, and \\u25b8 would drive an app that is not there.
  function setLive(live, why) {
    gate(startBtn, served && !live,
         'Start the app and fill the address in from what it prints');
    gate(stopBtn, served && live, 'Stop the app and free its port');
    gate(reset, live, 'Put the demo data back to its seed');
    [].forEach.call(document.querySelectorAll('.cue-drive'), function (el) {
      arm(el, live, live ? 'Drive the app to this point' : why);
    });
    // The address is shown only while something answers at it. A URL on a page next to a
    // dead port is the one thing here that can waste a reader's afternoon.
    if (addr) {
      addr.hidden = !live;
      if (live) { addr.href = base(); addr.textContent = base(); }
      else { addr.removeAttribute('href'); addr.textContent = ''; }
    }
  }

  // Live has nothing to say that the address does not say better, so the pill empties and
  // disappears; every other state is a word in its place. `Offline` and not `offline at
  // http://…`: the URL of a thing that is not answering is an invitation to click it.
  function say(kind, text) {
    state.dataset.state = kind;
    state.textContent = text || '';
    state.hidden = !text;
  }

  function probe() {
    var b = base();
    if (!b) {
      say('down', 'Offline');
      setLive(false, 'Nothing is running yet');
      return;
    }
    say('unknown', 'checking\\u2026');
    setLive(false, 'Checking whether anything is listening\\u2026');
    // /healthz answers with CORS open, so this works from a file:// page too. A failure
    // here means "nothing is listening", which is the normal case for an old report.
    fetch(b + '/healthz', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw 0;
      say('live', '');
      setLive(true);
    }).catch(function () {
      say('down', 'Offline');
      setLive(false, 'Nothing is answering at ' + b + ' \\u2014 start it first');
    });
  }

  apply(); probe();

  // Learned once the environment answers on its own, and only then. `adopt` is the whole
  // reason the Start button is worth more than the clipboard: `start-docker.sh` ends by
  // printing the port the host gave it, the server scrapes that line, and the address the
  // reader would otherwise have had to hunt for appears in the row. Two round trips to
  // the terminal become none — the second being the one nobody counts, where you go
  // back to find the URL again because the clipboard has moved on.
  function adopt(url) {
    if (!url) return false;
    current = url.replace(/\\/+$/, '');
    remember(current); apply(); probe();
    return true;
  }

  if (copy) copy.addEventListener('click', function () {
    var cmd = bar.querySelector('.appenv-manual code').textContent;
    navigator.clipboard.writeText(cmd).then(function () {
      copy.textContent = 'Copied'; setTimeout(function () { copy.textContent = 'Copy'; }, 1200);
    }).catch(function () { copy.textContent = 'Copy failed'; });
  });

  // While a command is in flight nothing else in the row may be pressed — a Stop sent
  // into the middle of a docker build is a half-torn-down instance nobody asked for —
  // and the pill carries the last line the command printed, which is where a reader who
  // wants to know it is still moving can look. A docker build prints thousands.
  function drive(id, face, then) {
    setLive(false, 'Waiting for the app \\u2014 ' + face.toLowerCase() + '\\u2026');
    say('unknown', face + '\\u2026');
    return window.HR.run(id, {}, function (snap) {
      var line = window.HR.tail(snap);
      if (line) state.dataset.tip = line;
    }).then(function (done) {
      state.removeAttribute('data-tip');
      then(done);
    }).catch(function (e) {
      say('down', face + ' failed');
      state.dataset.tip = e.message || 'the review server is no longer running';
      setLive(false, state.dataset.tip);
    });
  }

  if (startBtn) startBtn.addEventListener('click', function () {
    if (blocked(startBtn)) return;
    drive('demo-env', 'Starting', function (done) {
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      // It ran and printed no URL we recognised, or it failed. Either way the reader is
      // back where they started rather than stuck: `probe` re-reads whatever base we
      // have, and the command below is still there to be run by hand.
      probe();
      if (done.state !== 'done') {
        say('down', 'start failed');
        state.dataset.tip = window.HR.tail(done) || 'the command exited ' + done.exit;
      }
    });
  });

  // Stop forgets the address as well as freeing the port. Remembering it would leave
  // every link in the transcript pointing confidently at nothing, and the next Start will
  // hand us a different port anyway.
  if (stopBtn) stopBtn.addEventListener('click', function () {
    if (blocked(stopBtn)) return;
    drive('demo-env-stop', 'Stopping', function (done) {
      if (done.state === 'done') { current = ''; remember(''); apply(); }
      probe();
      if (done.state !== 'done') {
        say('down', 'stop failed');
        state.dataset.tip = window.HR.tail(done) || 'the command exited ' + done.exit;
      }
    });
  });

  // One command per caption. Off disk it is copied, because a file cannot drive a browser
  // on your machine; served, it is run, and what the reader gets back is the app already
  // sitting on the screen the caption describes. Same template either way — the one in
  // `data-drive` for the clipboard, the one the build declared for the server — so the
  // two paths cannot drift into doing different things.
  var tmpl = bar.dataset.drive;
  if (tmpl) document.querySelectorAll('.cue-drive').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (blocked(btn)) return;
      var was = btn.innerHTML;
      function tick(mark, hold) {
        btn.classList.add('copied'); btn.innerHTML = mark;
        setTimeout(function () { btn.classList.remove('copied'); btn.innerHTML = was; }, hold);
      }
      if (window.HR.can('cue-drive')) {
        btn.innerHTML = '&#8943;';
        window.HR.run('cue-drive', {n: btn.dataset.n, base: base()})
          .then(function (done) {
            if (done.state === 'done') { tick('&#10003;', 1400); return; }
            btn.dataset.tip = window.HR.tail(done) || 'the driver exited ' + done.exit;
            tick('&#10007;', 2600);
          })
          .catch(function (e) { btn.dataset.tip = e.message; tick('&#10007;', 2600); });
        return;
      }
      var cmd = tmpl.replace(/\\{n\\}/g, btn.dataset.n).replace(/\\{base\\}/g, base());
      navigator.clipboard.writeText(cmd).then(function () {
        tick('&#10003;', 1400);
      }).catch(function () { btn.title = 'could not copy'; });
    });
  });

  window.HR.onready(function () {
    // `demo-env` and not merely "is there a server": a build that stopped declaring the
    // start command has to be able to take the verb away from a page that is still open.
    served = window.HR.can('demo-env');
    if (served) {
      // Which swaps the whole row: the verbs come back and the terminal command steps
      // out, since pressing Start here does the same thing without leaving the page.
      bar.classList.add('appenv-served');
      // Re-ask rather than re-deriving from whatever the pill happens to say: the first
      // probe may still be in flight, and `served` has just changed the answer for two
      // of the three verbs.
      probe();
    }
    // Nothing remembered and a host that can be asked: ask it. The base normally survives
    // a reload in localStorage, so this is for the first reader of a page whose
    // environment somebody else already started — and for the browser with site data
    // blocked, where `stored()` has always come back empty by design.
    if (!current && window.HR.can('demo-env-url')) {
      window.HR.run('demo-env-url', {}).then(function (done) {
        if (done.state === 'done') adopt(done.result && done.result.base);
      }).catch(function () { /* nothing was running; the bar already says so */ });
    }
  });

  // Explicit, never automatic. Resetting on every link click would throw away work the
  // reviewer was in the middle of; the duplicate rows and unique-constraint collisions it
  // exists to prevent are the reviewer's own repeated form submissions, and they know
  // when they have made a mess.
  if (reset) reset.addEventListener('click', function () {
    if (blocked(reset)) return;
    var b = base();
    if (!b) return;
    reset.disabled = true; reset.textContent = 'Resetting\\u2026';
    fetch(b + bar.dataset.reset, {method: 'POST', cache: 'no-store'}).then(function (r) {
      reset.textContent = r.ok ? 'Reset' : 'Reset failed';
    }).catch(function () { reset.textContent = 'Reset failed'; }).then(function () {
      setTimeout(function () { reset.textContent = 'Reset DB'; reset.disabled = false; }, 1400);
    });
  });
})();
</script>"""

TIP_JS = """<script>
// One tooltip for the whole page. The native `title` is unstyleable, unresizable and
// waits ~500ms — long enough that a reviewer reads the icon, gives up, and moves on.
// Listeners are delegated on `document` so markup written later by any of the other
// scripts picks the behaviour up with no registration step.
(function () {
  var css = document.createElement('style');
  css.textContent =
    // 15px, the page's own body size, at normal weight. It was 1.05rem/600 -- a hint
    // set LARGER and heavier than the sentence it explains, which reads as the page
    // shouting an aside.
    '.tip{position:fixed;z-index:9999;pointer-events:none;background:rgba(20,20,22,.96);' +
    'color:#fff;font:400 15px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;' +
    'padding:.6rem .9rem;border-radius:.6rem;max-width:22rem;box-shadow:0 10px 30px rgba(0,0,0,.35);' +
    // A repo-relative path is one long token as far as line breaking is concerned: no
    // spaces, and a slash is not a break opportunity. So the longest tip on the page --
    // the one naming a Java file five packages deep -- overflowed the bubble and was
    // clipped at its edge, which reads as the page running off the screen. `anywhere`
    // gives the browser leave to break inside the token; the max-width then holds, and
    // `place()` can keep a box it has correctly measured on screen.
    'overflow-wrap:anywhere;' +
    'opacity:0;transform:translateY(4px);transition:opacity 120ms ease,transform 120ms ease}' +
    '.tip.visible{opacity:1;transform:translateY(0)}' +
    // `.oneline` is the first thing tried for a plain-text tip: the whole label on one
    // row, as wide as it needs to be, up to the viewport. The 22rem wrap above had been
    // folding "Open in VS Code: petclinic-test/src/add-visit.spec.ts" in the middle of
    // the file name -- and a path broken across rows is a path the reader cannot read
    // at a glance, which is the one job that tip has. `show()` drops the class again
    // when the row would not fit, so a paragraph-sized hint still wraps.
    '.tip.oneline{white-space:nowrap;max-width:calc(100vw - 16px)}' +
    // A tip that lists identifiers lists them: one per line, in code type, with a marker
    // -- not welded into a comma-separated sentence the reader has to parse to find out
    // whether their own library is in it. `.tipfoot` is for the sentence that genuinely
    // is one, set apart and quieter so the list stays the thing being read.
    '.tip ul.tiplist{margin:0;padding-left:1.1rem;list-style:disc;' +
    'font:400 13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}' +
    '.tip ul.tiplist li{margin:0}' +
    '.tip p.tipfoot{margin:.5rem 0 0;font-size:13px;opacity:.72}' +
    '.tip p.tipfoot:first-child{margin:0;opacity:1}' +
    // One convention for the pointer: a mark that only explains itself gets the question
    // mark, so hovering tells you there is something to read AND that clicking does
    // nothing. Anything you can act on -- a link, a button, a row that opens -- keeps
    // the hand it already had.
    //
    // The last clause is about marks that live *inside* something actionable, which the
    // element-level list above cannot see. The tab strip's badges are the case that found
    // it: `Tests 1` is a <span role=img> with its own tooltip sitting inside the tab
    // <button>, so the cursor turned into a question mark over the badge and back into a
    // hand a pixel to its left -- while a click anywhere in there, badge included, opens
    // the tab. Excluding them is the whole fix: `cursor` inherits, so a badge that gets no
    // rule of its own simply keeps the pointer its button already set.
    '[data-tip]:not(a):not(button):not([role=button]):not(summary):not(label)' +
    ':not(:is(a,button,[role=button],summary,label) *)' +
    '{cursor:help}';
  document.head.appendChild(css);

  var bubble = document.createElement('div');
  bubble.className = 'tip';
  bubble.setAttribute('role', 'tooltip');
  document.body.appendChild(bubble);
  var timer = null, current = null;

  function hide() {
    clearTimeout(timer);
    current = null;
    bubble.classList.remove('visible');
  }

  function place(el) {
    var r = el.getBoundingClientRect(), b = bubble.getBoundingClientRect(), left, top;
    // `data-tip-side="right"` is for a tip tall enough to be a panel rather than a
    // label: a stack of bullets floated above the phrase covers the sentence the reader
    // is in the middle of, and pushes the page's own content out of view. Beside it, the
    // sentence stays readable. Flips to the left margin when the right one is too narrow.
    if (el.getAttribute('data-tip-side') === 'right') {
      left = r.right + 12;
      if (left + b.width > window.innerWidth - 8) left = r.left - b.width - 12;
      top = r.top + r.height / 2 - b.height / 2;
    } else {
      left = r.left + r.width / 2 - b.width / 2;
      // Above by default; below when the top of the viewport is in the way.
      top = r.top - b.height - 10;
      if (top < 8) top = r.bottom + 10;
    }
    left = Math.max(8, Math.min(left, window.innerWidth - b.width - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - b.height - 8));
    bubble.style.left = left + 'px';
    bubble.style.top = top + 'px';
  }

  function show(el) {
    // `data-tip-html` is for a tip that has to SHOW a component rather than name it -- a
    // coverage badge, say, where "UI x2" in prose makes the reader translate back to the
    // badge they are looking at. The markup is the page's own; nothing user-supplied
    // reaches here. Plain `data-tip` stays the default and stays escaped.
    var html = el.getAttribute('data-tip-html'), text = el.getAttribute('data-tip');
    if (!html && !text) return;              // data-tip="" shows nothing, by design
    current = el;
    if (html) bubble.innerHTML = html; else bubble.textContent = text;
    bubble.classList.remove('visible');
    // One row when the row fits the screen; otherwise back to the wrapping box. Measured,
    // not guessed from the character count: a path and a sentence of the same length are
    // very different widths. Markup tips (lists) always wrap.
    bubble.classList.toggle('oneline', !html);
    if (!html && bubble.scrollWidth > window.innerWidth - 16) bubble.classList.remove('oneline');
    place(el);
    timer = setTimeout(function () {
      if (current !== el) return;
      place(el);
      bubble.classList.add('visible');
    }, 150);
  }

  function trigger(ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip],[data-tip-html]');
    if (!el || el === current) return;
    hide();
    show(el);
  }

  document.addEventListener('pointerover', trigger);
  document.addEventListener('focusin', trigger);   // focus/blur do not bubble
  document.addEventListener('pointerout', function (ev) {
    if (current && !current.contains(ev.relatedTarget)) hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('touchstart', hide, {passive: true});
  window.addEventListener('scroll', hide, true);   // a fixed bubble would float away
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') hide(); });
})();
</script>"""


FRAME_JS = """<script>
// A framed report sizes itself: it posts its height and we grow the frame to fit, so
// the page keeps the only scrollbar. A frame that scrolls internally traps the wheel
// and hides how much of it is left.
var dvFrames = [];
window.addEventListener('message', function (e) {
  var d = e.data;
  if (!d || d.type !== 'dv-height' || !d.height) return;
  Array.prototype.forEach.call(document.querySelectorAll('iframe'), function (f) {
    if (f.contentWindow !== e.source) return;
    f.style.height = (d.height + 4) + 'px';
    if (dvFrames.indexOf(f) < 0) dvFrames.push(f);
  });
  dvStick();
});

// The price of that bargain: a frame grown to its full height has no scrollport, so a
// toolbar inside it cannot pin itself -- `sticky` never fires and `fixed` pins to the
// same full-height box. We are the document that scrolls, so we are the one that knows.
// Post how far our own pinned masthead has run past the top of each frame and let it
// slide its toolbar down by that much; the frame clamps the number to its own height.
// Only frames that have announced themselves with a `dv-height` are talked to, so an
// embedded report that knows nothing of this is never sent anything.
function dvStick() {
  if (!dvFrames.length) return;
  var top = parseFloat(getComputedStyle(document.documentElement)
                       .getPropertyValue('--strip-h')) || 0;
  dvFrames.forEach(function (f) {
    var r = f.getBoundingClientRect();
    // A frame in a hidden panel measures as nothing; there is nothing to pin over.
    if (!r.height) return;
    // `clientTop` is the frame's own top border: the rect is the border box, but the
    // offset we are posting is measured from inside it, and the one pixel between the
    // two is a pixel of the frame's content showing above a bar that looked flush.
    // Floored, not rounded, for the same reason -- at a fractional scroll position the
    // bar is better a hair under the masthead than a hair below it.
    var y = Math.floor(top - r.top - (f.clientTop || 0));
    f.contentWindow.postMessage({type: 'dv-stick', top: Math.max(0, y)}, '*');
  });
}
var dvStickQueued = false;
function dvQueueStick() {
  if (dvStickQueued) return;
  dvStickQueued = true;
  requestAnimationFrame(function () { dvStickQueued = false; dvStick(); });
}
window.addEventListener('scroll', dvQueueStick, {passive: true});
window.addEventListener('resize', dvQueueStick);
// Switching tabs, or opening `show all`, moves a frame without scrolling the page. The
// listener is on capture so it is queued before the tab handler runs; the frame is
// re-measured in the animation frame after, by which time the panel has swapped.
document.addEventListener('click', dvQueueStick, true);
</script>"""


TRACE_JS = """<script>
// The 📺 on a covering-tests row whose test was recorded: the way into the recording.
//
// Served, it is a link to the Playwright trace viewer copied beside this page, opened in
// a window of its own — the viewer is a three-pane application and gets the whole
// screen, where the review page keeps its place in this one. The viewer reads the
// recording with `fetch`, so a page opened off disk — out of the downloadable zip, or
// straight from `.human-review/` — has nothing to hand it, not even a file sitting
// beside it. There the 📺 copies the line that opens the same recording natively.
//
// The registry is written per build by `render_traces`; the map's rows are drawn by its
// own inline script and do not know what the run recorded, and should not have to. So
// the pairing is done here, from the outside, after the map has drawn.
(function () {
  var el = document.getElementById('hr-traces');
  if (!el) return;
  var reg; try { reg = JSON.parse(el.textContent); } catch (e) { return; }
  var byKey = {};
  (reg.tests || []).forEach(function (t) { byKey[t.test] = t; });
  var served = !!reg.viewer && location.protocol !== 'file:';

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    var ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  function decorate() {
    var rows = document.querySelectorAll('.rm-t[data-id]');
    Array.prototype.forEach.call(rows, function (row) {
      if (row.querySelector('.rm-tv')) return;
      var id = row.getAttribute('data-id') || '';
      var m = /^(.*?)(?::(\\d+))?$/.exec(id);
      var key = (m[1] || '').split('/').pop() + (m[2] ? ':' + m[2] : '');
      var t = byKey[key];
      if (!t) return;
      var where = row.querySelector('.rm-tw');
      if (!where) return;
      var tv = document.createElement('a');
      tv.className = 'rm-tv';
      tv.textContent = '📺';
      tv.setAttribute('aria-label', 'open the recording of this test');
      if (served) {
        // Absolute, because the viewer resolves `?trace=` against its own document and
        // not against ours: a relative path would be looked for inside the viewer's folder.
        tv.href = reg.viewer + '?trace=' + encodeURIComponent(new URL(t.trace, location.href).href);
        tv.target = '_blank'; tv.rel = 'noopener';
        tv.setAttribute('data-tip', 'Open test replay in a new window');
        tv.addEventListener('click', function (ev) { ev.stopPropagation(); });
      } else {
        tv.href = '#';
        tv.setAttribute('data-tip', 'Recorded. Copy the command that opens the replay natively'
          + (reg.viewer ? ' \\u2014 or serve this page (scripts/serve-review.py) to open it from here' : ''));
        tv.addEventListener('click', function (ev) {
          ev.preventDefault(); ev.stopPropagation();
          copy(t.cmd).then(function () {
            var was = tv.getAttribute('data-tip');
            tv.setAttribute('data-tip', 'Copied \\u2014 run it in a terminal');
            setTimeout(function () { tv.setAttribute('data-tip', was); }, 2000);
          });
        });
      }
      where.parentNode.insertBefore(tv, where);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decorate);
  else decorate();
})();
</script>"""


SEQLINK_JS = """<script>
// The 🕵️ on a covering-tests row whose test drew a sequence: the way from the test to
// the picture of what its run actually did, over on the Sequence tab.
//
// The 📺 beside it opens the same run as a Playwright recording — frame by frame, from
// the browser's side. This opens the other account of it: the calls the run made, in
// order, across the stack. Two doors out of one row, which is why they look alike.
//
// Same shape as TRACE_JS, and for the same reason: the map's rows are drawn by the
// requirements map's own inline script, which knows nothing about diagrams and should
// not have to. The pairing is done from the outside, after the map has drawn, off a
// registry `render_testpairs` writes while it renders the pairs — so a row can only
// ever link to a pair that is really on the page.
(function () {
  var el = document.getElementById('hr-genseq');
  if (!el) return;
  var reg; try { reg = JSON.parse(el.textContent); } catch (e) { return; }
  var byKey = {}, byName = {}, dup = {};
  reg.forEach(function (e) {
    if (!byKey[e.test]) byKey[e.test] = e;
    // The map addresses a row by repo-relative path, and so does the registry, so the
    // lookup above is the one that fires. The basename is a fallback for a map that
    // addresses rows the short way — taken only while it is unambiguous, because two
    // `add-visit.feature` under different modules are two different tests.
    var name = e.test.split('/').pop();
    if (byName[name] && byName[name].pair !== e.pair) dup[name] = true;
    else byName[name] = e;
  });

  function lookup(id) {
    if (byKey[id]) return byKey[id];
    var name = id.split('/').pop();
    return dup[name] ? null : byName[name];
  }

  // Everything the click has to do that a plain hash link would do for us, minus the one
  // thing it cannot: the row it sits on toggles open on a click anywhere that is not the
  // editor link, so the event has to stop here — and stopping it also keeps it from
  // reaching the tab strip's own document-level handler for links into another panel.
  function jump(pair) {
    var target = document.getElementById(pair);
    if (!target) return;
    var panel = target.closest && target.closest('.panel');
    if (panel && !document.body.classList.contains('showall')) {
      var tab = document.querySelector('.tabstrip button.tab[aria-controls="' + panel.id + '"]');
      if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
    }
    // A pair the reader folded away earlier is still the answer to this click.
    if (target.tagName === 'DETAILS') target.open = true;
    // After the tab has painted: scrolling to a panel that is still `hidden` measures
    // nothing and lands at the top of the page.
    requestAnimationFrame(function () {
      target.scrollIntoView({block: 'start'});
      target.classList.remove('seq-hit');
      void target.offsetWidth;            // restart the flash on a second click
      target.classList.add('seq-hit');
      if (history.replaceState) history.replaceState(null, '', '#' + pair);
    });
  }

  function decorate() {
    var rows = document.querySelectorAll('.rm-t[data-id]');
    Array.prototype.forEach.call(rows, function (row) {
      if (row.querySelector('.rm-seq')) return;
      var entry = lookup(row.getAttribute('data-id') || '');
      if (!entry) return;
      var where = row.querySelector('.rm-tw');
      if (!where) return;
      var a = document.createElement('a');
      a.className = 'rm-seq';
      a.textContent = '\\uD83D\\uDD75\\uFE0F';
      a.href = '#' + entry.pair;
      a.setAttribute('aria-label', 'open the sequence this test drew');
      a.setAttribute('data-tip', 'Trace it: the calls this test made, on the Sequence tab');
      a.addEventListener('click', function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        jump(entry.pair);
      });
      where.parentNode.insertBefore(a, where);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decorate);
  else decorate();
})();
</script>"""


SEQFOLD_JS = """<script>
// The Sequence tab opens on its table of contents: every test pair folded shut, so the
// tab's first screen is one line per test instead of the top of whichever sequence happens
// to be first. A sequence is three or four screens of arrows; four of them stacked is a
// wall the reader has to scroll past to find out what is on the tab at all.
//
// Why here and not `<details>` without `open` in the markup, which is the obvious way: the
// click targets inside every diagram are transparent rects sized from `getBBox()`, and
// `getBBox()` on anything inside a `display:none` subtree returns zeros. A pair born shut
// would cost its sequence every handle on it, silently. So the pairs are born open, every
// script that measures them runs, and the folding happens after — the same bargain TABS_JS
// makes with the panels, for the same reason, and this runs before it for the same one.
//
// A reader arriving on a deep link is the exception: the pair the link names is what they
// asked for, and it stays open.
//
// Which pair is open is in the URL, both ways round. A reader who opens one and sends the
// address sends the picture they are looking at, not the tab it is on — the same handle
// the 🕵️ on the Tests tab already jumps through. `replaceState`, not `location.hash`,
// because assigning the hash scrolls the page out from under the click that caused it.
(function () {
  var wanted = decodeURIComponent((location.hash || '').slice(1));
  // Not scoped to a panel id: the tab this block lands on is named by the content file.
  document.querySelectorAll('details.testpair[open]').forEach(function (pair) {
    if (pair.id && pair.id === wanted) return;
    pair.open = false;
  });

  function remember(id) {
    if (history.replaceState) history.replaceState(null, '', '#' + id);
    else location.hash = id;
  }

  document.querySelectorAll('details.testpair[id]').forEach(function (pair) {
    // `toggle` fires asynchronously, so the folding above arrives here too — harmlessly:
    // a pair being shut only rewrites the hash when the hash is naming that very pair,
    // which at load is true of the one pair this loop leaves open.
    pair.addEventListener('toggle', function () {
      var hash = decodeURIComponent((location.hash || '').slice(1));
      if (pair.open) remember(pair.id);
      else if (hash === pair.id) {
        var panel = pair.closest('.panel');
        remember(panel && panel.id ? panel.id : '');
      }
    });
  });

  // The fold's row carries the quoted test's source bar, and those are links: to VS Code,
  // to the compare page on github.com. A click on one is also a click inside a <summary>,
  // which a browser may read as a request to toggle the fold — so the reader would open
  // the file AND have the block they were reading fold away under them.
  //
  // The fold is put back rather than the click stopped. `stopPropagation` is the usual
  // remedy and it is wrong here: the handler that turns a `vscode://` reference into an
  // *open diff* is a listener on `document`, so silencing the event on its way there would
  // trade one bug for a better-hidden one. This runs after the browser's own activation
  // behaviour and before the next paint, so a fold that never moved is left alone and one
  // that did is put back with nothing drawn in between.
  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('details.testsrc > summary a');
    if (!link) return;
    var fold = link.closest('details.testsrc');
    var was = fold.open;
    requestAnimationFrame(function () { if (fold.open !== was) fold.open = was; });
  }, true);
})();
</script>"""

HSCROLL_JS = """<script>
// Shift+wheel scrolls sideways. Chrome on macOS hands a shifted wheel to the page as an
// ordinary vertical scroll, so a line that runs past the right edge of a `pre.code` — a
// long Gherkin step, a wide diagram — can only be reached by dragging its scrollbar,
// the one gesture that makes the reader leave the line they were reading. We walk up
// from whatever is under the cursor to the first box that can actually move
// horizontally and move it ourselves. If nothing under the cursor can, or it is already
// at the end, the event is left alone and the page scrolls as it always did.
(function () {
  function scroller(el) {
    for (; el && el !== document.body; el = el.parentElement) {
      if (el.scrollWidth <= el.clientWidth + 1) continue;
      var ox = getComputedStyle(el).overflowX;
      if (ox === 'auto' || ox === 'scroll') return el;
    }
    return null;
  }
  window.addEventListener('wheel', function (ev) {
    if (!ev.shiftKey) return;
    // Some pointing devices already report a shifted wheel on the X axis: take whichever
    // axis actually moved, so both kinds of input travel the same distance.
    var delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
    if (!delta) return;
    if (ev.deltaMode === 1) delta *= 16;                 // lines, not pixels
    else if (ev.deltaMode === 2) delta *= 320;           // pages
    var box = scroller(ev.target instanceof Element ? ev.target : null);
    if (!box) return;
    var before = box.scrollLeft;
    box.scrollLeft = before + delta;
    // Only claim the gesture if it moved something; at either end it falls back to the
    // page, so a reader who keeps scrolling past the last column is not stuck.
    if (box.scrollLeft !== before) ev.preventDefault();
  }, { passive: false });
})();
</script>"""


TABS_JS = """<script>
// The tab strip. Runs *last* on purpose: every panel is in the document and visible
// while the earlier scripts measure it, because getBBox() on anything inside a
// display:none subtree returns zeros — which would silently cost every sequence
// diagram its click targets. This script is what hides them, after the measuring.
(function () {
  var strip = document.querySelector('.tabstrip');
  if (!strip) return;
  var tabs = Array.prototype.slice.call(strip.querySelectorAll('button.tab'));
  var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute('aria-controls')); });
  // Outside the strip now — at the foot of the page — so it is looked up on the document.
  var showAll = document.querySelector('button.allbtn');
  var active = 0;
  // What is actually stuck to the top of the viewport. The strip travels inside the
  // masthead, so the block that pins -- and whose height a deep link has to clear -- is
  // the masthead, not the strip inside it. A page built without one falls back to the
  // strip, which is what every measurement below used to be about.
  var sticky = strip.closest('.masthead') || strip;

  // How far a deep link has to clear the sticky strip is the strip's rendered height,
  // and that is not a constant: the pills wrap to a second row as soon as they outgrow
  // the track, which happens on a narrow window and happened for good the day the
  // thirteenth tab landed. Publish the measurement as `--strip-h` and let the CSS do
  // the arithmetic, so one row, two rows and whatever the next tab does are all correct
  // without anyone editing a number.
  function syncStripHeight() {
    var h = sticky.getBoundingClientRect().height;
    if (h > 0) document.documentElement.style.setProperty('--strip-h', h + 'px');
  }
  syncStripHeight();
  if (window.ResizeObserver) new ResizeObserver(syncStripHeight).observe(sticky);
  else window.addEventListener('resize', syncStripHeight);

  // `position:sticky` gives no state to style against: the strip looks identical whether
  // it is sitting in the masthead or pinned over the text. Compare its rendered top with
  // where it would sit unpinned -- offsetTop is relative to `.wrap`, which is static, so
  // the difference IS the scroll the strip has absorbed. Marks the pinned state so the
  // stylesheet can put an edge under it; nothing here measures or sets a height.
  function syncPinned() {
    var pinned = sticky.getBoundingClientRect().top <= 0.5;
    sticky.classList.toggle('pinned', pinned);
  }
  syncPinned();
  window.addEventListener('scroll', syncPinned, {passive: true});
  window.addEventListener('resize', syncPinned);

  function paint() {
    var all = document.body.classList.contains('showall');
    tabs.forEach(function (t, i) {
      // In show-all there is no selected tab: leaving one lit makes the strip claim a
      // filter is applied while every panel is on screen.
      t.setAttribute('aria-selected', String(!all && i === active));
      t.tabIndex = i === active ? 0 : -1;
      if (panels[i]) panels[i].hidden = !all && i !== active;
    });
    if (showAll) {
      showAll.setAttribute('aria-pressed', String(all));
      var label = showAll.getAttribute(all ? 'data-label-on' : 'data-label-off');
      if (label) showAll.textContent = label;
    }
  }

  // A panel holding live media has to know when it comes and when it goes — the Video
  // panel starts its narration on the way in and pauses it on the way out, because a
  // voice-over playing under a panel nobody is looking at is a bug, not a feature.
  // In show-all no panel is *the* active one, so every panel counts as off and nothing
  // starts talking while the reader is somewhere else on the page.
  function announce() {
    var all = document.body.classList.contains('showall');
    panels.forEach(function (p, i) {
      if (!p) return;
      var on = !all && i === active;
      if (p.__panelOn === on) return;
      p.__panelOn = on;
      p.dispatchEvent(new CustomEvent(on ? 'panelshow' : 'panelhide'));
    });
  }

  // The hash is the shareable handle: a reviewer sends "look at #api" and it opens there.
  // replaceState rather than location.hash, which would scroll the page out from under
  // the click that caused it.
  function select(i, remember, keepScroll) {
    if (i < 0 || i >= tabs.length) return;
    active = i;
    paint();
    announce();
    // Panels differ in height by thousands of pixels, so keeping the scroll offset across
    // a tab change drops the reader at an arbitrary point in the new panel — usually its
    // tail. Clicking "Review" and landing in the middle of "already fixed for you" reads
    // as if those were the open findings. Deep links (keepScroll) still scroll to their
    // target, which is the whole point of a deep link.
    if (!keepScroll) {
      var top = sticky.getBoundingClientRect().top + window.pageYOffset - 8;
      window.scrollTo(0, Math.max(0, top));
    }
    if (remember && history.replaceState) {
      history.replaceState(null, '', '#' + tabs[i].getAttribute('aria-controls'));
    }
  }

  function panelIndexOf(node) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i] && panels[i].contains(node)) return i;
    }
    return -1;
  }

  tabs.forEach(function (t, i) {
    t.addEventListener('click', function () { select(i, true); });
  });

  strip.addEventListener('keydown', function (ev) {
    var step = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
    if (!step) return;
    ev.preventDefault();
    var next = (active + step + tabs.length) % tabs.length;
    select(next, true);
    tabs[next].focus();
  });

  if (showAll) {
    showAll.addEventListener('click', function () {
      document.body.classList.toggle('showall');
      paint();
      announce();
      // The page just changed length by an order of magnitude; the old offset means nothing.
      window.scrollTo(0, 0);
    });
  }

  // A link into a section that lives on another tab has to switch tabs first, or it
  // scrolls to something the browser is not showing.
  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="#"]');
    if (!link) return;
    var target = document.getElementById(decodeURIComponent(link.getAttribute('href').slice(1)));
    if (!target) return;
    var i = panelIndexOf(target);
    if (i >= 0 && i !== active) select(i, false, true);
  });

  // Opening on a deep link: the hash may name a tab, or anything inside one.
  var wanted = decodeURIComponent((location.hash || '').slice(1));
  var start = 0;
  if (wanted) {
    var byTab = tabs.findIndex(function (t) { return t.getAttribute('aria-controls') === wanted; });
    if (byTab >= 0) start = byTab;
    else {
      var node = document.getElementById(wanted);
      var inPanel = node ? panelIndexOf(node) : -1;
      if (inPanel >= 0) {
        start = inPanel;
        setTimeout(function () { node.scrollIntoView(); }, 0);
      }
    }
  }
  select(start, false, Boolean(wanted));
})();
</script>"""

XREF_CSS = """
/* --- following the code the page already quotes ----------------------------------
   A cross-reference is a word inside a code block, so it cannot wear the page's link
   colour: syntax highlighting has already spent colour on that word to say what kind of
   thing it is, and repainting it blue would trade a fact for an affordance. A dotted rule
   under it says "followable" without touching the token's own colour, and the cursor and
   the hover wash say the rest. */
a.xref { color:inherit; text-decoration:none; cursor:pointer;
  border-bottom:1px dotted var(--link,#1a4fa0); }
/* Several anchors in a row are one sentence: Pygments splits a Gherkin step at its quoted
   argument, so `with "Helen Leary" attending` is three links that have to read as one.
   No gap, no double underline where they meet. */
a.xref + a.xref { border-left:0; }
a.xref:hover, a.xref:focus-visible { background:var(--accent-soft,#eef2fb);
  border-bottom-style:solid; }
/* The folded state. The source bar stays — a reader scanning an opened test still wants
   to see which files it went through — and what goes is the body, replaced by the one
   line that says what is in it.

   That line goes INSIDE the bar, not under it. The bar is right-aligned, so the whole left
   half of it is empty: a folded excerpt was costing two rows, one of them a file name with
   nothing beside it, and stacking four of them under a scenario put the next test a screen
   away. `flex:1` fills that space, which also pushes nothing — the name, the badge and the
   two handles were already against the right edge and stay exactly where they were. */
.rm-part.xr-shut > .rm-scroll, .rm-part.xr-shut > pre.code { display:none; }
/* `flex-basis:0`, not `auto`: a signature is a hundred characters wide, so basing the stub
   on its own content made it ask for the whole row and the file name paid for it — the
   name wrapped into three lines mid-word and the folded excerpt came out taller than the
   two rows it replaced. At zero the name is measured first, as it always was, and the stub
   grows into whatever the bar has left. */
.xr-stub { flex:1 1 0; min-width:0; display:flex; align-items:baseline; gap:.35rem;
  margin:0; padding:0; border:0; background:transparent; color:var(--muted,#6b6b78);
  font:400 11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; text-align:left;
  cursor:pointer; overflow:hidden; }
.xr-stub:hover { color:var(--fg,#1c1c22); }
.xr-stub .xr-face { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.xr-caret { flex:0 0 auto; transition:transform .12s ease; }
/* Unfolded, the signature is the first line of the code right underneath, so the stub
   keeps only its handle — the one thing that folds the excerpt away again. */
.rm-part:not(.xr-shut) .xr-stub .xr-face { display:none; }
.rm-part:not(.xr-shut) .xr-stub .xr-caret { transform:rotate(90deg); }
/* Where the click landed. The window a link opens is usually a screen away and folded, so
   it arrives with nothing saying which of its lines the link was about. The flash says it
   once and gets out of the way — a permanent mark would still be there on the next click,
   pointing at the last question instead of this one. */
@keyframes xr-flash { from { background:var(--accent-soft,#eef2fb); } to { background:transparent; } }
pre.code .ln-row.xr-hit { animation:xr-flash 1.8s ease-out 1; }
@media (prefers-color-scheme: dark) {
  a.xref { border-bottom-color:#7aa2f7; }
  a.xref:hover, a.xref:focus-visible { background:rgba(122,162,247,.16); }
  @keyframes xr-flash { from { background:rgba(122,162,247,.22); } to { background:transparent; } }
}
"""


XREF_JS = r"""<script>
// The other half of `code_xref.py`. Every link on the page is already drawn and every
// window already knows its own id; what is left is the four things only a browser can do:
// fold a window, unfold it, scroll to it, and say which line the link was about.
//
// It is placed BEFORE the editor handler on purpose. Both listen for a click on an anchor
// whose href is `vscode://…`, and a cross-reference has one so that holding Cmd still
// opens the file in VS Code, which is the gesture a reader already has for "not here, in
// my editor". Registering first means this handler runs first, and calling preventDefault
// is what tells the editor handler (which checks `defaultPrevented`) to leave the plain
// click alone. Cmd-, Ctrl- and Shift-clicks fall through untouched to it, and to the
// browser behind it.
(function () {
  var node = document.getElementById('xref-index');
  var INDEX = {};
  try { INDEX = JSON.parse((node && node.textContent) || '{}'); } catch (e) { INDEX = {}; }

  function fold(part, shut) {
    part.classList.toggle('xr-shut', shut);
    var stub = part.querySelector('.xr-stub');
    if (!stub) return;
    stub.setAttribute('aria-expanded', String(!shut));
    // What it does, then what it does it to — and the second half is the whole first line,
    // which is the half the stub itself cannot always show: it sits in whatever the source
    // bar has left over, so a long signature is clipped at a width only the browser knows.
    // Reading the clipped end should not cost a click that changes the page.
    stub.setAttribute('data-tip', shut
      ? 'Unfold this excerpt: ' + (stub.dataset.face || '')
      : 'Fold this excerpt away');
  }

  // The requirements map builds a test's excerpts the first time its row is opened, so
  // there is nothing to fold at load: the parts arrive later, in a batch, and each one is
  // recognised by the editor link in its own source bar — the one thing the map already
  // emits that says exactly which window it is.
  function adopt(root) {
    if (!root.querySelectorAll) return;
    // The map sets `.rm-tinner`'s innerHTML in one go, so the nodes the observer hands
    // over ARE the parts — and `querySelectorAll` on an element never returns the element
    // itself. Looking only inside them found nothing, every time.
    var parts = Array.prototype.slice.call(root.querySelectorAll('.rm-part'));
    if (root.matches && root.matches('.rm-part')) parts.unshift(root);
    parts.forEach(function (part) {
      if (part.dataset.xrefId) return;
      var link = part.querySelector('a.srcref:not(.rm-diff)[href^="vscode:"]');
      var seen = link && INDEX[link.getAttribute('href')];
      if (!seen) { part.dataset.xrefId = ''; return; }
      part.dataset.xrefId = seen.id;
      if (!seen.shut) return;
      var stub = document.createElement('button');
      stub.type = 'button';
      stub.className = 'xr-stub';
      stub.dataset.face = seen.face || '';
      stub.innerHTML = '<span class="xr-caret" aria-hidden="true">▸</span>'
        + '<span class="xr-face"></span>';
      stub.querySelector('.xr-face').textContent = seen.face || 'folded';
      // Into the bar's own empty left half, ahead of everything it already holds.
      var bar = part.querySelector(':scope > .rm-srcbar, :scope > .srcbar');
      if (bar) bar.insertBefore(stub, bar.firstChild);
      else part.insertBefore(stub, part.firstChild);
      fold(part, true);
    });
  }

  // Nearest first: one file can be quoted under two tests, and the copy the reader is
  // looking at is the one inside the accordion they have open. Only when the link points
  // outside it does the search widen to the page.
  function target(link) {
    var id = link.getAttribute('data-xref');
    if (!id) return null;
    var sel = '[data-xref-id="' + id.replace(/"/g, '') + '"]';
    var near = link.closest('.rm-tinner') || link.closest('.panel');
    var box = (near && near.querySelector(sel)) || document.querySelector(sel);
    // A window quoted on a tab the reader is not on is not somewhere to send them: the
    // scroll would land on a hidden panel and the page would look like it ignored the
    // click. Nothing rendered means nothing to open here, and the anchor's own href takes
    // over — which opens the file in the editor, the way every other reference does.
    return box && box.offsetParent !== null ? box : null;
  }

  function flash(box, line) {
    Array.prototype.forEach.call(box.querySelectorAll('.ln-row.xr-hit'), function (row) {
      row.classList.remove('xr-hit');
    });
    if (!line) return;
    Array.prototype.forEach.call(box.querySelectorAll('.ln-row'), function (row) {
      var no = row.querySelector('.ln, .rm-ln');
      if (!no || no.textContent.trim() !== String(line)) return;
      // Restarting an animation needs the class gone and a reflow read before it is put
      // back, or a second click on the same line does nothing at all.
      row.classList.remove('xr-hit');
      void row.offsetWidth;
      row.classList.add('xr-hit');
    });
  }

  document.addEventListener('click', function (ev) {
    var stub = ev.target.closest && ev.target.closest('.xr-stub');
    if (stub) {
      var part = stub.closest('.rm-part');
      if (part) fold(part, !part.classList.contains('xr-shut'));
      return;
    }
    var link = ev.target.closest && ev.target.closest('a.xref');
    if (!link) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
    var box = target(link);
    if (!box) return;          // nothing quoted here: the href opens it in the editor
    ev.preventDefault();
    var part = box.closest('.rm-part');
    // A second click on a link whose window is already open puts it back, so the reader
    // who followed a step and read its glue has the same handle to be rid of it again.
    if (part && part.classList.contains('xr-shut')) fold(part, false);
    else if (part && link.dataset.xrefOpen === 'yes') {
      link.dataset.xrefOpen = 'no';
      fold(part, true);
      return;
    }
    if (part) link.dataset.xrefOpen = 'yes';
    flash(box, link.getAttribute('data-xref-line'));
    box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  });

  adopt(document);
  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        Array.prototype.forEach.call(record.addedNodes, function (added) {
          if (added.nodeType === 1) adopt(added);
        });
      });
    }).observe(document.body, {childList: true, subtree: true});
  }
})();
</script>"""


EDITOR_JS = r"""<script>
// Click-to-source depends on the OS handing `vscode://` to the editor, and only a real
// browser tab can ask it to. VS Code's own Simple Browser is a webview: its iframe is
// sandboxed without `allow-top-navigation` under a `frame-src *` CSP, so it cannot launch
// an external scheme at all and the click does nothing whatever the anchor says — a
// target="_blank" does not help either, because there is no tab to open it in.
//
// **But a sandboxed iframe can still fetch its own origin.** When the guide is served by
// serve-review.py rather than opened off disk, the click becomes a request back to that
// server, which opens the file in the VS Code window that has this repository — and the
// reader lands in the class, embedded or not. That is the whole reason the guide is
// served instead of opened as a file.
//
// So there are three cases, and only the last one is a consolation prize:
//   served    → ask the server; it puts the caret in the file.
//   top level → navigate in place, handing off to the editor with no tab stranded behind.
//   embedded, unserved → copy the reference and say what to do with it, once, in a banner.
(function () {
  var EMBEDDED = window.self !== window.top;
  // False until the probe in SERVER_JS says otherwise, and never back. It used to be
  // `location.protocol === 'http:'`, which is not the same question: the demo on GitHub
  // Pages is https, so every handle on it fetched `/__open__` against github.io and
  // toasted a failure at the reader. Starting false costs the first few tens of
  // milliseconds after paint, during which a click falls through to the href — which is
  // the same thing it does on a machine with no server at all, and therefore already
  // tested.
  var SERVED = false;
  window.HR.onready(function (caps) { SERVED = !!caps; });

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(fallback);
    }
    return Promise.resolve(fallback());
    function fallback() {
      var box = document.createElement('textarea');
      box.value = text;
      box.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(box);
      box.select();
      try { document.execCommand('copy'); } catch (e) { /* nothing else to try */ }
      box.remove();
    }
  }

  var toast = null;
  // `sticky` is for a command that is still running: a docker build outlasts 2.6 seconds
  // many times over, and a progress line that fades while the thing is still going reads
  // as the thing having stopped. The next flash replaces it; a plain one after it ends
  // clears it.
  function flash(message, sticky) {
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'copy-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('shown');
    clearTimeout(flash.timer);
    if (sticky) return;
    flash.timer = setTimeout(function () { toast.classList.remove('shown'); }, 2600);
  }

  // `vscode://file//abs/path.java:487:1` → the two halves the server wants.
  function parse(href) {
    var m = /^vscode:\/\/file\/*(\/[^:]*?)(?::(\d+))?(?::\d+)?$/.exec(decodeURIComponent(href));
    return m ? { path: m[1], line: m[2] || '1' } : null;
  }

  // The one button on the page that copies something that is not a source reference:
  // the command that re-renders the hand-drawn diagram. It lives in this handler because
  // `copy` and `flash` do, and a second clipboard-and-toast implementation for one button
  // is how two of them end up behaving differently.
  //
  // Served, it runs the command instead \u2014 the same command, looked up by the id the build
  // stamped on the button. Its last stage rewrites *this file*, which is why the reload
  // is part of the action and not left to the reader: the alternative is a green tick
  // beside a picture that is still the old one, which is precisely the confusion the
  // "then reload this page" in the copy message exists to prevent.
  document.addEventListener('click', function (ev) {
    var cmd = ev.target.closest &&
      ev.target.closest('button.copycmd, button.runhere, button.cmdpeek');
    if (!cmd) return;
    // The fold at the end of the sentence. The command is one click away and costs the
    // page nothing until someone asks for it.
    if (cmd.classList.contains('cmdpeek')) {
      // By id, not by position: a diagram block carries two of these folds — the one that
      // re-renders and the one that throws the layout away — and "the first .cmdline in
      // here" would open the wrong command as soon as the second offer exists.
      var box = document.getElementById(cmd.getAttribute('aria-controls'));
      if (!box) return;
      var opening = box.hidden;
      box.hidden = !opening;
      cmd.setAttribute('aria-expanded', opening ? 'true' : 'false');
      return;
    }
    var action = cmd.getAttribute('data-action');
    if (action && window.HR.can(action)) { rerun(cmd, action); return; }
    // Static: the offer stays on the page and says what it needs, rather than vanishing
    // between two copies of the same report. The line that gets the reader to served mode
    // is already on the badge in the title row, so this points at it instead of growing a
    // second copy of it here.
    if (cmd.classList.contains('runhere')) {
      flash('This copy of the report is static, so nothing in it can run. Serve the page '
        + '\u2014 the "static" badge at the top copies the line that does \u2014 and this '
        + 'will re-render the diagram and reload.');
      return;
    }
    // The static badge copies a different kind of line: not one that changes this page
    // and wants a reload, but one that starts the server and opens the page from it.
    var serve = cmd.id === 'hr-mode';
    copy(cmd.getAttribute('data-copy') || '')
      .then(function () { flash(serve
        ? 'Copied \u2014 run it in a terminal: it starts the review server and opens this page served'
        : 'Copied \u2014 run it in a terminal, then reload this page'); });
  });

  function rerun(button, action) {
    var was = button.textContent, last = '';
    button.disabled = true;
    button.textContent = 'Running\u2026';
    // Two offers under the same picture run through here, and "Re-rendering the diagram"
    // over a click that has just thrown the layout away would be the page describing the
    // wrong half of what it is doing.
    flash(button.getAttribute('data-run-say') || 'Re-rendering the diagram\u2026', true);
    window.HR.run(action, {}, function (snap) {
      var line = window.HR.tail(snap);
      // Only on change: the poll is every 700ms and a quiet command would otherwise
      // repaint the same sentence eighty times while nothing happened.
      if (line && line !== last) { last = line; flash(line, true); }
    }).then(function (done) {
      if (done.state === 'done') {
        button.textContent = 'Done';
        flash('Rebuilt \u2014 reloading this page', true);
        // A beat, so the sentence is readable before the page goes. `reload()` and not a
        // cache-busting navigation: the server sends no-store for exactly this.
        setTimeout(function () { location.reload(); }, 800);
        return;
      }
      button.disabled = false; button.textContent = was;
      flash(window.HR.tail(done) || ('The command exited ' + done.exit));
    }).catch(function (e) {
      button.disabled = false; button.textContent = was;
      flash(e.message || 'The review server is no longer running');
    });
  }

  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="vscode:"]');
    if (!link || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
    ev.preventDefault();
    // A fix is a change, so the reference to it opens as a change. Only the served path
    // can do that — the diff needs the *before* file materialised out of git, which is
    // work no page can do for itself. Everywhere else this link keeps the href it was
    // given, which is the same `vscode://file/…` every other reference carries, so the
    // worst case is today's behaviour (the file, at the first differing line) and never
    // a dead custom URL on a machine without the helper.
    var base = link.getAttribute('data-diff-base');
    var dpath = link.getAttribute('data-diff-path');
    if (SERVED && base && dpath) {
      // The line travels too. The href already carries it — it is the same line the face
      // beside this handle names — and a diff that opens scrolled to the top of a
      // five-hundred-line class leaves the reader hunting for the four lines the box is
      // about, which is the hunt this button exists to end.
      var aim = parse(link.getAttribute('href'));
      fetch('/__open_diff__?path=' + encodeURIComponent(dpath) + '&base=' + encodeURIComponent(base)
            + (aim ? '&line=' + encodeURIComponent(aim.line) : ''))
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { flash(t || 'Could not open the diff'); });
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    // Not served, and at top level: the extension's URI handler is the only channel left
    // that can materialise a before-image out of git. It is emitted only where the build
    // found that extension installed, so a portable guide never carries a URL that would
    // dead-end instead of degrading.
    var duri = link.getAttribute('data-diff-uri');
    if (!SERVED && !EMBEDDED && duri) { window.location.href = duri; return; }
    var ref2 = SERVED && parse(link.getAttribute('href'));
    if (ref2) {
      fetch('/__open__?path=' + encodeURIComponent(ref2.path) + '&line=' + ref2.line)
        .then(function (r) {
          // 404 means the server would not open it — a reference outside the repository,
          // or a file that has since moved. Say so rather than leave the click silent.
          if (!r.ok) flash('Could not open ' + ref2.path.split('/').pop());
        })
        .catch(function () { flash('The review server is no longer running'); });
      return;
    }
    if (!EMBEDDED) {
      // A diff link with no channel to open a diff through still opens the file, which is
      // the right thing — but silently, it reads as the feature being broken rather than
      // unavailable. That is exactly how this landed the first time, so it says so.
      if (base) flash('No diff channel here \u2014 opening the file at the change.');
      window.location.href = link.getAttribute('href');
      return;
    }
    // `path:line`, which is what Quick Open takes
    var ref = (link.textContent || '').trim().split('-')[0]
      || decodeURIComponent(link.getAttribute('href')).replace(/^vscode:\/\/file\/*/, '/').replace(/:\d+$/, '');
    copy(ref).then(function () { flash('Copied ' + ref + ' — paste into Quick Open (\u2318P)'); });
  });

  // The banner is the consolation prize, so it must not be printed until we know there
  // is nothing better on offer — which is now something we learn after the page has
  // painted rather than from the URL. Waiting for the probe also means it is never shown
  // and then withdrawn, which would be a paragraph of apology flashing past for no reason.
  if (!EMBEDDED) return;
  window.HR.onready(function (caps) {
    if (caps) return;
    var note = document.createElement('p');
    note.className = 'embedded-note';
    note.innerHTML = 'You are reading this inside an embedded browser, opened straight off '
      + 'disk, so the links cannot reach the editor. Clicking a <code>path:line</code> '
      + 'copies it instead. Serve the guide with <code>serve-review.py</code> and they open '
      + 'the file for real.';
    var body = document.querySelector('.wrap') || document.body;
    body.insertBefore(note, body.firstChild);
  });
})();
</script>"""

GENSEQ_JS = """<script>
// Progressive disclosure over the inlined sequence-diagram SVGs. The generator wrapped
// the label of every arrow that has more to say in a PlantUML link, which PlantUML
// rendered as <a href="genseq://<id>"> — a stable, generation-time handle, so nothing
// here has to match rendered label text. The detail itself rides in the sidecar next to
// each diagram. One click reveals, another closes.
//
// Where a step has a second rendering of the same fact — a statement as sent vs. the
// same statement with its bound values put back — the panel offers it as a toggle. It
// used to be a second click on the arrow, which swapped the text under the reader and
// counted itself "1 / 2": both the affordance and the fact that there *was* one were
// invisible until you had already found them by accident.
(function () {
  var PREFIX = 'genseq://';
  // The section header's handle. The generator writes a line number rather than a path — the
  // .puml is committed and read on other machines — and the file it belongs to is the one the
  // diagram is already labelled `generated by`.
  var SCENARIO_PREFIX = 'genseq-scenario://';
  // `?` or the bound values is a way of reading, not a property of one arrow: a reviewer
  // who asked for values once is reading the whole page in values. So the choice is the
  // page's, and every panel opened after it honours it.
  var panel = null, els = null, current = null, step = null, showValues = false;
  // The test a diagram was generated from, computed at build time. Inside a test pair the
  // heading above the picture already names that file, so the card prints no `generated by`
  // line of its own and the href rides on the card instead; a diagram standing alone still
  // carries the provenance paragraph, and that is the fallback.
  function sourceOf(diagram) {
    var direct = diagram.getAttribute('data-test-src');
    if (direct) return direct;
    var links = diagram.querySelectorAll('.prov .srcref');
    // The test first, explicitly: the second provenance link is the .puml, and a scenario's
    // line number resolved against a generated file would point at nothing.
    for (var i = 0; i < links.length; i++) {
      if (/^generated by /.test(links[i].textContent || '')) return links[i].getAttribute('href');
    }
    return links.length ? links[0].getAttribute('href') : null;
  }

  // The provenance link opens the file at its first line; a section header knows better.
  function atLine(href, line) {
    return href ? href.replace(/(:\\d+){0,2}$/, '') + ':' + line + ':1' : null;
  }

  // Give an element's children back to its parent and drop it — used where a handle turns out
  // to lead nowhere, since removing the <a> would take the text it wraps with it.
  function unwrap(node) {
    while (node.firstChild) node.parentNode.insertBefore(node.firstChild, node);
    node.remove();
  }

  function openSource(href) {
    if (href) window.location.href = href;
  }

  function build() {
    if (panel) return;
    panel = document.createElement('div');
    panel.id = 'genseq-panel';
    panel.hidden = true;
    panel.innerHTML =
      '<div class="genseq-head"><span class="genseq-title"></span>' +
      '<button type="button" class="genseq-toggle" hidden></button>' +
      '<span class="genseq-grow"></span><span class="genseq-step"></span>' +
      '<button type="button" class="genseq-close" data-tip="close (Esc)" aria-label="close">&times;</button></div>' +
      '<div class="genseq-handler" hidden></div>' +
      '<div class="genseq-label"></div><pre></pre>';
    document.body.appendChild(panel);
    els = {
      title: panel.querySelector('.genseq-title'),
      step: panel.querySelector('.genseq-step'),
      handler: panel.querySelector('.genseq-handler'),
      label: panel.querySelector('.genseq-label'),
      toggle: panel.querySelector('.genseq-toggle'),
      body: panel.querySelector('pre'),
    };
    panel.querySelector('.genseq-close').addEventListener('click', close);
    // The title is a label, not a control. ⌘-click on it used to open the diagram's test
    // — a door with no handle, opening what the section header above the picture opens on
    // a plain click, and at the right line rather than at line 1.
    els.toggle.addEventListener('click', function () { showValues = !showValues; render(); });
    panel.addEventListener('click', function (ev) { ev.stopPropagation(); });
  }

  function close() {
    if (current) current.reset();
    current = null;
    if (panel) panel.hidden = true;
  }

  // Page coordinates, and anchored to the arrow rather than to the pointer: the panel
  // must stay put while the page scrolls, and still be readable next to what it explains.
  function place(target) {
    var box = target.getBoundingClientRect();
    panel.hidden = false;
    var width = panel.offsetWidth;
    var left = Math.min(box.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - width - 12);
    panel.style.left = Math.max(window.scrollX + 8, left) + 'px';
    panel.style.top = (box.bottom + window.scrollY + 8) + 'px';
  }

  // The button always names the *other* rendering, so it reads as what a click will get
  // you. A step with no alternate — a JSON payload — simply has no button.
  function render() {
    var on = showValues && !!step.alternate;
    var view = on ? step.alternate : step;
    els.label.textContent = view.label || '';
    els.label.hidden = !view.label;
    els.body.textContent = view.text;
    els.toggle.hidden = !step.alternate;
    if (step.alternate) {
      // One word, because it sits against the end of the title and the sentence it
      // completes is the title: `OwnerRepository.findById` … `values`. "show values"
      // spent half its width restating that a button is a thing you press.
      els.toggle.textContent = on ? '?' : 'values';
      els.toggle.setAttribute('aria-label', on ? 'show ?' : 'show values');
      els.toggle.setAttribute('data-tip', on
        ? 'the statement as sent, with ? for each bound value'
        : 'the same statement with the bound values put back');
      els.toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  // The entry point that answered the call. The arrow already carries the route, which is
  // the contract; a reviewer reading a request body next wants the method that receives
  // it, and no trace knows its name — the generator resolved it against this checkout's
  // controllers, so the link lands on the real declaration line.
  function renderHandler(entry) {
    var handler = entry.handler;
    els.handler.textContent = '';
    els.handler.hidden = !handler;
    if (!handler) return;
    var key = document.createElement('span');
    key.className = 'genseq-key';
    key.textContent = 'handler';
    var name = document.createElement(handler.href ? 'a' : 'span');
    name.className = 'srcref';
    name.textContent = handler.name;
    if (handler.href) {
      name.href = handler.href;
      name.setAttribute('data-tip', 'Open in VS Code');
    }
    els.handler.appendChild(key);
    els.handler.appendChild(name);
  }

  function show(entry, index, target) {
    build();
    step = entry.steps[index];
    els.title.textContent = entry.title;
    els.step.textContent = entry.steps.length > 1 ? (index + 1) + ' / ' + entry.steps.length : '';
    renderHandler(entry);
    render();
    place(target);
  }

  // A transparent rect under the arrow, so the click lands anywhere across the band
  // instead of only on the glyph PlantUML made into a link.
  function addHitArea(group) {
    var box;
    try { box = group.getBBox(); } catch (e) { return; }
    if (!box || !box.width) return;
    var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('class', 'genseq-hit');
    rect.setAttribute('x', box.x - 4);
    rect.setAttribute('y', box.y - 1);
    rect.setAttribute('width', box.width + 8);
    rect.setAttribute('height', Math.max(box.height - 2, 6));
    rect.setAttribute('rx', '3');
    group.insertBefore(rect, group.firstChild);
  }

  document.querySelectorAll('.diagram').forEach(function (diagram) {
    // Every carrier, not the first: a diagram is now drawn three times in one card
    // (Diff / New / Old), and each side's handles are keyed by ids derived from that
    // side's payloads. One carrier covered one side, and the other panes' handles were
    // dropped as dead. Merged work-tree-first; the ids are content-derived, so an id
    // present on both sides carries the same payload either way.
    var carriers = diagram.querySelectorAll('script.genseq-details');
    if (!carriers.length) return;
    var details = {};
    for (var c = carriers.length - 1; c >= 0; c--) {
      var part = (JSON.parse(carriers[c].textContent) || {}).details || {};
      for (var key in part) details[key] = part[key];
    }
    var href = sourceOf(diagram);

    diagram.querySelectorAll('svg a[href^="' + SCENARIO_PREFIX + '"]').forEach(function (link) {
      var target = atLine(href, (link.getAttribute('href') || '').slice(SCENARIO_PREFIX.length));
      // Nothing to open — a diagram whose test is not in this checkout. Leave the title as
      // the plain text it would have been rather than an underline that leads nowhere.
      if (!target) { unwrap(link); return; }
      link.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openSource(target);
      });
    });

    diagram.querySelectorAll('svg a[href^="' + PREFIX + '"]').forEach(function (link) {
      var entry = details[(link.getAttribute('href') || '').slice(PREFIX.length)];
      var group = link.closest('g.message') || link.parentNode;
      // An arrow this change *removed* is re-inserted from the base diagram, and its
      // detail was never recorded here. Drop the handle rather than offer a dead one —
      // by unwrapping it, since the link is now around the label itself and removing
      // the element would take the arrow's text with it.
      if (!entry || !entry.steps.length) {
        unwrap(link);
        return;
      }

      var index = -1;
      var state = {reset: function () { index = -1; group.classList.remove('genseq-open'); }};
      group.classList.add('genseq-hot');
      addHitArea(group);
      group.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // One arrow, one gesture. ⌘-click used to open the test from here as well, which
        // was a second way to reach what the section header above already opens — and a
        // chord the page had to spend a sentence explaining.
        if (current && current !== state) current.reset();
        index++;
        if (index >= entry.steps.length) { close(); return; }
        current = state;
        group.classList.add('genseq-open');
        show(entry, index, link);
      });
    });

    // No instructions above the picture. There used to be a line here — "Click any arrow
    // marked \u2295 for the SQL or the JSON behind it" — printed once per diagram, and on a
    // tab that is now one picture per test that is once per test. The \u2295 is on the label
    // itself, the cursor changes over it, and its tooltip says what a click will get you,
    // at the moment the reader is looking at it. A sentence above the diagram says the
    // same thing to everyone, including the reader who has already clicked one.
  });

  // The panel is placed in page coordinates, so a diagram scrolled sideways under it
  // would leave it pointing at the wrong arrow.
  document.querySelectorAll('.diagram .svgbox').forEach(function (box) {
    box.addEventListener('scroll', close);
  });
  document.addEventListener('click', close);
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') close(); });
})();
</script>"""


# The caption is prose, and prose about this project says things like `{{ visit | vetName }}`.
# Stopping it at the first `}` cut the directive in half there and spilled the rest onto the
# page as literal text, so the caption now runs to the last `}}` on the line — stopping
# only at a following `{{snippet:`, so two directives in one paragraph stay two
# directives while a caption may still quote a template expression.
SNIPPET_TOKEN = re.compile(
    r"\{\{snippet:(?P<ref>[^|}]+)(?:\|(?P<caption>(?:(?!\{\{snippet:).)*))?\}\}"
)


# `{{difflink:<repo-relative path>@<ref>}}` — the fix as a *change*, not as a result.
# A snippet shows the code that is there now, which is the answer to "what does it say"
# and not to "what did you do": the reader has to imagine the delta. This opens the real
# before/after in the editor instead.
DIFF_TOKEN = re.compile(r"\{\{difflink:(?P<path>[^|}@]+)@(?P<base>[0-9a-fA-F]{7,40})\}\}")

# The same handle, one letter shorter, for the diff a reader should not have to click to
# see. `{{diff:path@base}}` renders the hunks inline, GitHub-style; `{{difflink:…}}` only
# links out to them. The rule of thumb the page follows: an applied fix shows its diff,
# because the diff *is* the whole finding, and an open call links to one, because the
# reviewer is going to open the file anyway.
# The base accepts a trailing `^` or `~n`, because the usual left side of a committed fix
# is the commit before it, and spelling that out as a second sha is one more thing to get
# wrong.
DIFF_INLINE_TOKEN = re.compile(
    r"\{\{diff:(?P<path>[^|}@]+)@(?P<base>[0-9a-fA-F]{7,40}(?:\^|~\d+)?)"
    r"(?:\|(?P<caption>[^}]*))?\}\}"
)

# How much unchanged code to keep around each hunk. Three is git's own default and what
# GitHub shows before you click "expand": enough to place a hunk in its method, short
# enough that the diff stays the thing being read.
DIFF_CONTEXT = 3


@functools.lru_cache(maxsize=None)
def github_blob_base(root: Path) -> str | None:
    """`https://github.com/<owner>/<repo>` for this checkout, or None when it is not one.

    Read from `origin` rather than configured, because a URL that has to be maintained by
    hand in a content file is a URL that eventually points at somebody else's fork. Both
    remote spellings are accepted; anything that is not github.com returns None and the
    caller falls back to the editor link, which always works."""
    proc = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
                 proc.stdout.strip())
    return f"https://github.com/{m['slug']}" if m else None


def _github_compare_link(rel: str, base: str, root: Path, head: str | None = None,
                         line: int | None = None, side: str = "R",
                         face: str | None = None) -> str:
    """The same comparison on github.com — the link a reviewer forwards to somebody else.

    `face` shortens the label for somewhere there is no room for a sentence — a diff
    header, beside the editor link that opens the same comparison. It moves what the label
    stops saying into the tooltip rather than dropping it.

    Only emitted when the *after* side is something GitHub can be expected to have. A fix
    still sitting uncommitted in the working tree has no URL there at all, and inventing
    one that 404s is worse than the editor link rendered beside it."""
    host = github_blob_base(root)
    if not host:
        return ""
    if head is None:
        # The right side is the working tree. That has a URL on github.com only if the
        # file is clean at HEAD; otherwise the page is showing something github.com has
        # never seen, and inventing a link to it would be a confident lie.
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
        if rev.returncode != 0:
            return ""
        if subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", rel],
                          capture_output=True, text=True).stdout.strip():
            return ""
        head = rev.stdout.strip()
    url = f"{host}/compare/{base}...{head}"
    # A compare page can be forty files long, and landing at its top makes the reader hunt
    # for the one file the finding is about. GitHub anchors each file by the sha-256 of its
    # path exactly as the diff header spells it — no `a/`/`b/` prefix — so jump straight
    # there. Without a path there is nothing to hash, and the bare compare URL stands.
    if rel:
        url += "#diff-" + hashlib.sha256(rel.encode()).hexdigest()
        # And then the line. A file in a compare page opens at its own first line, which
        # for a five-hundred-line class is nowhere near the four lines the review is
        # about — so the reader arrives on github.com and starts hunting a second time,
        # having already been sent there to stop hunting. GitHub numbers the two sides
        # separately inside the file anchor: `R<n>` is the right side, `L<n>` the left,
        # which is the only landing a pure deletion has.
        if line:
            url += f"{side}{line}"
    # The face already says "on GitHub" and the arrow already says it opens elsewhere, so
    # a tooltip repeating either is a sentence the reader can see. What it cannot see is
    # where in a forty-file compare page it lands.
    tip = ("This change, in the compare page" if line else
           "Just this file, inside the compare page") if rel else "The whole compare page"
    # Short-faced in a bar, the tip is short too. The prose link can afford a sentence
    # about where in a forty-file compare page it lands; a mark in a row of three is
    # hovered to answer "what is this?", and a sentence there is a paragraph in a corner.
    if face:
        tip = "GitHub"
    return (f'<a class="srcref{" diffref srcbar-diff" if face else ""}" target="_blank"'
            f' rel="noopener" href="{html.escape(url)}"'
            f' data-tip="{tip}">{face or (_icon("GH") + " on GitHub")}</a>')


def _first_changed(rows) -> tuple[int | None, str]:
    """The line a reader of this diff is actually looking at, and which side it is on.

    The first added line, because that is what the change *did*; a pure deletion has no
    right side to land on, so it falls back to the first removed line on the left. Context
    lines are never it — landing three lines above the change is the same hunt in miniature.
    """
    for kind, old_no, new_no, _ in rows:
        if kind == "add":
            return new_no, "R"
    for kind, old_no, new_no, _ in rows:
        if kind == "del":
            return old_no, "L"
    return None, "R"


def _parse_unified(diff_text: str):
    """Unified diff text -> rows of `(kind, old_no, new_no, text)`, one per rendered line.

    Only the hunks: the `diff --git`/`index`/`---`/`+++` preamble says nothing a reader of
    a single-file diff needs, and the header above the table already names the file."""
    rows, old_no, new_no = [], 0, 0
    # `.split` on text that ends in a newline leaves an empty tail element, and an empty
    # line in a unified diff is a *context* line — so without this the table grows a blank
    # row and every number after it is off by one.
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file",
                            "old mode", "new mode", "similarity", "rename ")):
            continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$", line)
        if m:
            old_no, new_no = int(m[1]), int(m[2])
            rows.append(("hunk", None, None, m[3].strip()))
        elif line.startswith("+"):
            rows.append(("add", None, new_no, line[1:])); new_no += 1
        elif line.startswith("-"):
            rows.append(("del", old_no, None, line[1:])); old_no += 1
        elif line.startswith("\\"):
            rows.append(("hunk", None, None, line[1:].strip()))
        else:
            rows.append(("ctx", old_no, new_no, line[1:] if line else ""))
            old_no += 1; new_no += 1
    return rows


def review_step_rev(out_dir: Path) -> str | None:
    """The revision the review pass started from, as the ledger recorded it.

    This is the left side of every applied-fix diff on the page, and step 1 takes it with
    one `git rev-parse HEAD` before `/code-review` touches anything precisely because it
    cannot be reconstructed afterwards. Absent — an older ledger, or a page re-rendered
    somewhere the ledger did not travel — the caller drops the diffs rather than guessing
    at a before-state."""
    ledger = out_dir / ".steps.json"
    if not ledger.is_file():
        return None
    try:
        records = json.loads(ledger.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for rec in records:
        if "review" in (rec.get("tabs") or []) and rec.get("rev"):
            return rec["rev"]
    return None


def _unmoved_since(rel: str, rev: str, root: Path) -> bool:
    """Is the working tree's copy of `rel` still byte-for-byte the one at `rev`?

    The question a pinned diff has to answer before it may also offer an editor link: the
    editor can only compare a ref against the file on disk, so `base -> rev` and
    `base -> disk` are the same comparison exactly while the file has not moved since
    `rev`. Asked of git rather than of the clock, because "the fix was the last commit" is
    not the same claim as "nobody has touched it since"."""
    proc = subprocess.run(["git", "-C", str(root), "diff", "--quiet", rev, "--", rel])
    return proc.returncode == 0


def diff_html(rel: str, base: str, root: Path, caption: str | None = None,
              head: str | None = None) -> str:
    """One file's change, rendered as a GitHub-style two-gutter table.

    `head` is the right-hand side, and defaults to the working tree. Name it when the fix
    is one commit and the file moved for other reasons since `base`: `base` alone would
    show the fix buried in every unrelated edit that landed in between, which is precisely
    the noise this block exists to cut. A committed fix is usually `base: "<sha>^"`,
    `head: "<sha>"` — the change on its own.

    Same contract as `diff_link_html`: the before-side has to be real. A base that does not
    resolve, a file that did not exist in it, or a diff that comes back empty drops the
    whole block and says which on stderr — a fix illustrated with a diff of nothing is the
    page lying about its own work, which is the one failure it exists to prevent."""
    src = root / rel
    if not src.is_file():
        print(f"[review] diff: no file at {rel} — block dropped", file=sys.stderr)
        return ""
    show = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"], capture_output=True)
    if show.returncode != 0:
        print(f"[review] diff: {rel} does not exist at {base} — block dropped, because a "
              "diff needs a before-state that was actually recorded", file=sys.stderr)
        return ""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", f"-U{DIFF_CONTEXT}", "--no-color", base]
        + ([head] if head else []) + ["--", rel],
        capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"[review] diff: {rel} is unchanged since {base} — block dropped rather than "
              "rendering an empty diff", file=sys.stderr)
        return ""
    rows = _parse_unified(proc.stdout)
    adds = sum(1 for r in rows if r[0] == "add")
    dels = sum(1 for r in rows if r[0] == "del")
    body = []
    for kind, old_no, new_no, code in rows:
        if kind == "hunk":
            body.append('<tr class="hunk"><td class="gln"></td><td class="gln"></td>'
                        f'<td class="code">{html.escape(code) or "&nbsp;"}</td></tr>')
            continue
        marker = {"add": "+", "del": "-", "ctx": " "}[kind]
        body.append(
            f'<tr class="{kind}">'
            f'<td class="gln">{old_no or ""}</td><td class="gln">{new_no or ""}</td>'
            f'<td class="code">{html.escape(marker + code)}</td></tr>')
    # Two ways out of this block, in the header's own corner rather than in a footer under
    # it: the reader who wants the diff somewhere they can scroll it wants that *before*
    # reading the excerpt, not after, and a link below a forty-line diff is a link they
    # have to come back up from. Each wears the mark of what it opens — VS Code, and
    # github.com — rather than initials for it, because the corner is shared with the
    # file's stat and a logo is recognised in less room than a word is read.
    # The editor link diffs against the working tree, so it is the same comparison when
    # `head` is the working tree — and also when `head` is a commit the file has not moved
    # off since, which is the ordinary state of a fix applied and left alone. Checked
    # rather than assumed: a file edited after the commit it is pinned to would open in the
    # editor showing that later edit too, which is a different diff wearing this one's
    # label. Then, and only then, the GitHub link stands alone.
    link = ("" if head and not _unmoved_since(rel, head, root)
            else diff_link_html(rel, base, root, face=_icon("VSC")))
    gh = _github_compare_link(rel, base, root, head, *_first_changed(rows), face=_icon("GH"))
    # The name, and the path on hover. A repo-relative Java path spends five segments on
    # ceremony -- module, `src/main/java`, the org package -- before it reaches the one
    # word that says which file this is, and the header is where a reader looks to answer
    # exactly that. The full path is not lost, it is moved to where the face could not fit
    # it, which is what this page's tooltips are for. A file at the repo root has no path
    # to move, and a tooltip repeating the name is a tooltip saying nothing.
    # The same source bar every other quoted block on this page wears, built by the same
    # function: the two handles, the file they open, then the stat as this block's badge —
    # `+8 -4 vs 5acf2472` is exactly the "what changed in it" a snippet's `new code` answers.
    stat = (f'<span class="stat"><span class="added">+{adds}</span> '
            f'<span class="removed">&minus;{dels}</span> vs '
            f'<code>{html.escape(base[:8])}</code></span>')
    bar = _extract_module().srcbar_html(
        f"vscode://file/{src.resolve()}", rel, "", stat, link + gh)
    return (
        '<div class="ghdiff">'
        f'{bar}'
        f'<div class="ghdiff-scroll"><table class="ghdiff-body"><tbody>{"".join(body)}</tbody></table></div>'
        + (f'<p class="ghdiff-note">{caption}</p>' if caption else "")
        + '</div>'
    )



@functools.lru_cache(maxsize=None)
def diff_uri_handler() -> str | None:
    """The extension id that can open a diff for a guide read off disk, if it is installed.

    A guide served over http asks its own origin for a diff. Read from `file://` — which is
    how it is actually read — there is no origin to ask and no reach to loopback, so the
    only channel left is a `vscode://<publisher>.<extension>/...` URL that VS Code routes
    to an extension. That URL is *not* portable: on a machine without the extension it
    dead-ends instead of degrading, which is worse than no diff at all.

    So it is emitted only where it will work, and the check is for the thing itself rather
    than for a flag someone has to remember to set. `HUMAN_REVIEW_DIFF_URI_HANDLER` forces
    it either way — an id to use, or empty to suppress it — for building a guide meant to
    be read on another machine."""
    override = os.environ.get("HUMAN_REVIEW_DIFF_URI_HANDLER")
    if override is not None:
        return override.strip() or None
    for store in (Path.home() / ".vscode" / "extensions",
                  Path.home() / ".vscode-insiders" / "extensions"):
        try:
            if any(d.name.startswith("victorrentea.victor-vsc-") for d in store.iterdir()):
                return "victorrentea.victor-vsc"
        except OSError:
            continue
    return None


def diff_link_html(rel: str, base: str, root: Path, face: str | None = None,
                   line: int | None = None) -> str:
    """A link that opens `<rel>` as a diff: the file at `base` on the left, the working
    tree on the right.

    `face` shortens the label to a pill for a diff header, where the sentence it renders in
    prose ("diff vs 5acf2472") would fight the file name beside it. The tooltip already
    carried the whole comparison, so nothing is lost by the shorter face.

    **Emitted only when the before-side is real.** The ref has to resolve, the file has to
    exist in it, and the two sides have to actually differ. A diff whose left half is a
    guess would be this page telling a confident lie about history — the exact failure it
    exists to prevent — so a base that is missing, unreadable, or identical to the working
    tree drops the link and says which on stderr. No link is strictly better than a wrong
    one here; the snippet beside it still stands on its own.

    The `href` is the ordinary `vscode://file/…` every other reference on this page emits,
    aimed at the first line that actually differs. That is the whole portability story: a
    reader with none of this installed, or reading the file off disk, gets today's
    behaviour — the file, at the interesting line — rather than a dead custom URL. The
    upgrade to a real diff happens in the click handler, and only where a server is
    running that can perform it."""
    src = root / rel
    if not src.is_file():
        print(f"[review] difflink: no file at {rel} — link dropped", file=sys.stderr)
        return ""
    show = subprocess.run(["git", "-C", str(root), "show", f"{base}:{rel}"],
                          capture_output=True)
    if show.returncode != 0:
        print(f"[review] difflink: {rel} does not exist at {base} "
              f"({show.stderr.decode(errors='replace').strip()}) — link dropped, because a "
              "diff needs a before-state that was actually recorded", file=sys.stderr)
        return ""
    before, after = show.stdout, src.read_bytes()
    if before == after:
        print(f"[review] difflink: {rel} is identical at {base} and in the working tree — "
              "link dropped rather than opening an empty diff", file=sys.stderr)
        return ""
    # The first line that differs, so the fallback (and the diff itself) opens where the
    # change is instead of at the top of a file the reader then has to scan.
    #
    # `line` overrides it, and a snippet's bar always passes one. "The first line that
    # differs" is the right answer for a link standing for the *whole file* — a prose
    # `diff vs 5acf2472` — and the wrong one for a handle sitting against
    # `VisitRestController.java:102`, where it opened the file at its first changed import
    # while the face beside it said 102. One bar, one line.
    if line is None:
        b_lines, a_lines = before.split(b"\n"), after.split(b"\n")
        line = next((i + 1 for i, (x, y) in enumerate(zip(b_lines, a_lines)) if x != y),
                    min(len(b_lines), len(a_lines)) + 1)
    short = base[:8]
    # The extra handle for the unserved case. The `href` stays the ordinary
    # `vscode://file/...`, so dropping this attribute costs the diff and nothing else.
    handler = diff_uri_handler()
    uri = ""
    if handler:
        q = urllib.parse.urlencode({"file": str(src.resolve()), "base": base, "line": line})
        uri = f' data-diff-uri="{html.escape(f"vscode://{handler}/diff?{q}")}"'
    return (
        f'<a class="srcref diffref{" srcbar-diff" if face else ""}"'
        f' href="vscode://file/{src.resolve()}:{line}:1"{uri}'
        f' data-diff-path="{html.escape(rel)}" data-diff-base="{html.escape(base)}"'
        f' data-tip="{"Open Diff in VSC" if face else html.escape(f"Open this fix as a diff in VS Code — {short} on the left, the working tree on the right")}"'
        f'>{face or _icon("VSC") + f" diff vs {html.escape(short)}"}</a>'
    )


def expand_snippets(text: str, root: Path) -> str:
    """Let prose interleave with code: `{{snippet:path:12-14|caption}}` inside any body."""
    text = DIFF_INLINE_TOKEN.sub(
        lambda m: diff_html(m["path"].strip(), m["base"].strip(), root,
                            (m["caption"] or "").strip() or None),
        text,
    )
    text = DIFF_TOKEN.sub(
        lambda m: diff_link_html(m["path"].strip(), m["base"].strip(), root), text
    )
    return SNIPPET_TOKEN.sub(
        lambda m: snippet_html(m["ref"].strip(), (m["caption"] or "").strip() or None, root), text
    )


#: The ref every snippet on this page is implicitly a claim about. `extract-snippet.py`
#: reads the same environment variable to decide which of a snippet's lines are new, so
#: reading it here keeps one answer behind both halves of the source bar: the badge says
#: "new code *since this*", and the handle beside it opens exactly that comparison. Two
#: bases would let the badge and the button disagree in a way nothing on the page shows.
SNIPPET_BASE = os.environ.get("HUMAN_REVIEW_DIFF_BASE", "origin/main")


@functools.lru_cache(maxsize=1)
def _extract_module():
    """`extract-snippet.py` is hyphenated, so it is not importable by name.

    Loaded rather than shelled out to, which it used to be, because the source bar needs
    something a command line cannot carry: the two diff handles are built here — only this
    side knows the review's base ref and whether github.com can be expected to have the
    file — and they have to arrive *inside* the bar, not be glued onto its markup
    afterwards. The interpreter is the same one either way, so the Pygments requirement is
    unchanged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_snippet", str(EXTRACT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _icon(which: str) -> str:
    """The github.com / VS Code mark, from the module that also ships the CSS sizing it.

    Both halves of an icon are a unit — the `<svg>` and the rule that gives it 14px and a
    baseline — and the rule lives in the snippet stylesheet, because that is the sheet
    every page carrying a source bar already loads. Keeping the markup next to it means
    the two cannot drift into a mark rendered at whatever the browser guesses."""
    return getattr(_extract_module(), f"ICON_{which}")


@functools.lru_cache(maxsize=None)
def _shown_in_compare(rel: str, base: str, root: str, line: int) -> bool:
    """Would github.com's compare page have a row — and therefore an `R<line>` anchor — here?

    Asked because an anchor is a promise, and a fragment that matches no id does not fall
    back to the file anchor it was appended to: the browser simply does not scroll, and the
    reader lands at the top of a forty-file compare page. A snippet quotes whatever window
    its author chose, most of whose lines are untouched context nowhere near the change, so
    unlike the diff blocks — which aim at a line they just rendered as added — this one has
    to be checked before it is offered.

    `-U3` and three dots, because that is exactly what the compare page renders unexpanded
    and exactly what a compare URL compares. Anything wider vouches for a row that is
    behind an "expand" button over there. A base this checkout cannot resolve answers no,
    which costs the line and keeps the file."""
    proc = subprocess.run(["git", "-C", root, "diff", "-U3", "--no-color",
                           f"{base}...HEAD", "--", rel], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    return any(new_no == line for _, _, new_no, _ in _parse_unified(proc.stdout))


def _snippet_links(rel: str, root: Path, line: int | None = None) -> str:
    """The VS Code and github.com handles for a quoted block, against the review's own base.

    Both are optional and for the same reason: each is emitted only where that side can
    really open what it promises. `diff_link_html` drops itself when the base does not
    resolve, when the file did not exist in it, or when the two sides are identical;
    `_github_compare_link` drops itself when the file is dirty in the working tree, which
    is precisely when github.com has never seen what the snippet is showing. A bar with
    one handle, or none, is the honest rendering — a dead button is worse than no button.

    `origin/` is stripped for github.com only: it is a name for a ref in *this* checkout,
    and a compare URL spelling it 404s.

    `line` is the line the bar's own face names, and both handles are aimed at it. Without
    it each side picked its own landing — the editor the file's first changed line, github
    the top of the file inside the compare page — so a bar reading `:102` carried three
    buttons that went to three different places, and the two that were there to open the
    change went nowhere near the statement the box was about."""
    vsc = diff_link_html(rel, SNIPPET_BASE, root, face=_icon("VSC"), line=line)
    # The editor can open any line of the file; github.com can only anchor one it draws.
    # Checked against the ref as *this* checkout spells it (`origin/main`), which is the
    # thing github calls `main` — the stripped spelling below is for the URL alone, and
    # asking git about it would compare against a local branch that may be days behind.
    at = line if line and _shown_in_compare(rel, SNIPPET_BASE, str(root), line) else None
    gh = _github_compare_link(rel, SNIPPET_BASE.removeprefix("origin/"), root,
                              line=at, face=_icon("GH"))
    return vsc + gh


def snippet_html(ref: str, caption: str | None, root: Path, exact: bool = False,
                 link_at: tuple[int, int] | None = None) -> str:
    rel = ref.rsplit(":", 1)[0] if ":" in ref else ref
    # Deferred, not built here: the line this snippet actually opens at is settled inside
    # `render` — the window may snap past a leading comment, and `link_at` replaces it
    # outright — so the handles are built once that answer exists rather than from the
    # reference as it was typed.
    return _extract_module().render(
        ref, caption, root, exact,
        links=functools.partial(_snippet_links, rel, root), link_at=link_at)


# `src://<repo-relative path>[:line]` — the handle the diagram generators leave on a
# class, a field, an endpoint. They cannot emit `vscode://file/<abs>` themselves: their
# .puml is committed, and an absolute path in it is a diff on every machine that
# regenerates the diagram. Resolving it here, against this checkout, is the last moment
# where the absolute path is a fact rather than a guess.
SRC_HANDLE = re.compile(r'href="src://(?P<path>[^"#:]+)(?::(?P<line>\d+))?"')


def resolve_source_links(svg: str, root: Path) -> str:
    def fix(m):
        target = (root / m.group("path")).resolve()
        line = m.group("line") or "1"
        return f'href="vscode://file/{target}:{line}:1"'

    return SRC_HANDLE.sub(fix, svg)


# PlantUML renders a title's creole into coloured <text>, and *also* copies the title
# verbatim into the SVG's own <title> element — which is what the browser shows as a
# tooltip over the diagram background. A title that says `- <color:red>Diff</color>`
# therefore reads correctly on the page and as raw markup in the tooltip. Escaped there,
# hence both spellings.
CREOLE_IN_TITLE = re.compile(r"(?:<|&lt;)/?(?:color(?::[^>&]*)?|s|b|i|u)(?:>|&gt;)", re.I)
#: `[[src://path:12{Click to open the test} Add a visit]]` → `Add a visit`. Since a
#: sequence diagram's title became the scenario — clickable, straight into the test — the
#: verbatim copy PlantUML drops into `<title>` is the whole creole link, so hovering the
#: picture showed a reader the markup that made the heading they were already looking at.
CREOLE_LINK = re.compile(r"\[\[[^\]\s{]+(?:\{[^}]*\})?\s*([^\]]*?)\s*\]\]")
SVG_TITLE = re.compile(r"(<title>)(.*?)(</title>)", re.S | re.I)


def _plain_svg_title(svg: str) -> str:
    def plain(text: str) -> str:
        return CREOLE_IN_TITLE.sub("", CREOLE_LINK.sub(r"\1", text)).strip()

    return SVG_TITLE.sub(lambda m: m[1] + plain(m[2]) + m[3], svg)


# A class/entity PlantUML draws as `<g class="entity">`: a `<rect>` box, an optional
# stereotype icon (the ellipse+letter for `class`/`interface`/…), the name as one or more
# `<text>` runs (split when the diff colours part of it), a `<line>` under the title, and
# then the field rows. When the element carries `[[link]]`, PlantUML wraps the *entire*
# group's content in one `<a>` — box, name and every field alike — so a reviewer who
# ⌘-clicks or hovers a field lands on the class, not the field. Fields carry none of their
# own; the generator puts exactly one link per element, on the element itself.
ENTITY_BLOCK = re.compile(r'(<g class="entity"[^>]*>)(.*?)(</g>)', re.S)
ENTITY_SOLE_ANCHOR = re.compile(r'^<a\b(?P<attrs>[^>]*)>(?P<inner>.*)</a>$', re.S)
ENTITY_TITLE_BAND = re.compile(
    r'^(?P<rect><rect\b[^>]*/>)'
    r'(?P<icon>(?:<(?!text\b|line\b)[^>]*/>)*)'      # stereotype icon: ellipse, path, …
    r'(?P<title>(?:<text\b[^>]*>.*?</text>)+)'       # the name — one run, or several if coloured
    r'(?P<line><line\b[^>]*/>)'
    r'(?P<fields>.*)$',
    re.S,
)


def _scope_entity_links(svg: str) -> str:
    """Re-scope a class/entity's `<a>` to the title band (icon + name) it should be.

    Restructures the markup rather than overlaying a rect: the icon and name are already
    exactly the shapes that should answer to a click, so wrapping just them in the `<a>`
    — and moving the box and field rows outside it — gets the right hit area for free,
    with no coordinates to compute or keep in sync with the box's own size.

    Touches only a `<g class="entity">` whose entire content is one `<a>…</a>` shaped
    exactly as PlantUML draws it (rect, optional icon, name text(s), divider line, then
    fields). Anything else — no link, more than one `<a>`, an unrecognised inner shape —
    is left byte-for-byte alone; guessing at a diagram family this generator doesn't
    produce is worse than leaving its box fully clickable.
    """

    def fix_block(m):
        open_tag, content, close_tag = m.group(1), m.group(2), m.group(3)
        stripped = content.strip()
        if content.count("<a ") != 1 or not stripped.startswith("<a ") or not stripped.endswith("</a>"):
            return m.group(0)
        anchor = ENTITY_SOLE_ANCHOR.match(stripped)
        if not anchor:
            return m.group(0)
        band = ENTITY_TITLE_BAND.match(anchor["inner"])
        if not band:
            return m.group(0)
        title_anchor = f'<a{anchor["attrs"]}>{band["icon"]}{band["title"]}</a>'
        return f'{open_tag}{band["rect"]}{title_anchor}{band["line"]}{band["fields"]}{close_tag}'

    return ENTITY_BLOCK.sub(fix_block, svg)


# PlantUML paints every diagram it draws for this repo (class, ER, sequence — none of
# them set `!theme` or a colour skinparam beyond `hyperlinkColor`) in one fixed, hardcoded
# palette — plus, for a .puml that carries a `<style>` block of its own, whatever that
# block names. Both kinds are enumerated below by exact literal, and between them they
# cover every fill/stroke/background this pipeline inlines. Mapping each to a `--dgm-*`
# custom property (declared in CSS, above) — rather than a blanket `filter:invert()` on
# the diagram, which would flatten these into a wash and turn the diff renderer's
# deliberate reds into cyans — lets dark mode restyle exactly these shapes and nothing
# else, and lets the reds stay red (just brighter) instead of getting fought by a filter.
# `stroke` and `background` only ever appear as literal colours inside a `style="…"`
# attribute in this generator's output (never `fill`, and there is no `<style>` block to
# collide with); `fill` only ever appears as a bare attribute. Case-insensitive because
# PlantUML is consistent within one render but not guaranteed to be across versions.
DIAGRAM_COLOR_VARS = {
    "#FFFFFF": "--dgm-bg", "#F1F1F1": "--dgm-box", "#EEEEEE": "--dgm-frame",
    "#DDDDDD": "--dgm-legend", "#181818": "--dgm-line", "#000000": "--dgm-fg",
    "#ADD1B2": "--dgm-icon", "#E2E2F0": "--dgm-activation", "#888888": "--dgm-muted",
    "#1A4FA0": "--dgm-link",
    # The two differs' shared palette — puml_diff.ADDED / .REMOVED and the sequence
    # differ's lifeline and note tints. Kept in step with those constants by
    # test_diagram_dark_mode.py rather than by memory.
    "#2E7D32": "--dgm-diff-add", "#C62828": "--dgm-diff-del",
    "#EAF6EC": "--dgm-diff-add-bg", "#FFEBEB": "--dgm-diff-del-bg",
    # packages.puml's own <style> block (Material blue-grey): component fill,
    # component border, arrow. Same treatment, different source — see the CSS.
    "#ECEFF1": "--dgm-box-accent", "#546E7A": "--dgm-line-accent",
    "#78909C": "--dgm-arrow-accent",
}
DIAGRAM_FILL_ATTR = re.compile(r'\bfill="(#[0-9A-Fa-f]{6})"')
DIAGRAM_STYLE_COLOR = re.compile(r'\b(stroke|background):(#[0-9A-Fa-f]{6})\b')


def _theme_diagram_colors(svg: str) -> str:
    """Rewrite PlantUML's hardcoded palette to the page's `--dgm-*` variables.

    A colour this generator is not known to emit is left exactly as written — degrading
    to an unthemed shape in the unlikely event PlantUML's defaults change, rather than
    guessing at what a var name for it should mean."""

    def fix_fill(m):
        var = DIAGRAM_COLOR_VARS.get(m[1].upper())
        return f'fill="var({var})"' if var else m[0]

    def fix_style(m):
        var = DIAGRAM_COLOR_VARS.get(m[2].upper())
        return f'{m[1]}:var({var})' if var else m[0]

    svg = DIAGRAM_FILL_ATTR.sub(fix_fill, svg)
    return DIAGRAM_STYLE_COLOR.sub(fix_style, svg)


def inline_svg(path: Path, root: Path) -> str:
    """Inline rather than <img src>: the guide must survive being emailed as one file."""
    svg = path.read_text(encoding="utf-8")
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
    svg = _scope_entity_links(svg)
    svg = _theme_diagram_colors(svg)
    return _plain_svg_title(resolve_source_links(svg, root))


# `Browser → Backend: POST /api/owners/{ownerId}/pets/{petId}/visits` — a call arrow's
# title, as the generator writes it. The route is the contract; the method that answers it
# is where a reviewer actually has to go, and nothing in a trace knows its name.
GENSEQ_CALL_TITLE = re.compile(
    r"(?:\u2192|->)\s*\w+\s*:\s*"
    r"(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?P<route>/\S*)\s*$"
)

MAPPING_ANNOTATION = re.compile(r"@(?P<kind>Get|Post|Put|Patch|Delete|Request)Mapping\b")
# `@GetMapping(produces = "application/json")` names a media type, not a route, so the
# first string literal in the arguments is only the path when it is positional or spelled
# `value =`/`path =`. Anything else leaves the mapping at its class-level base.
MAPPING_NAMED_PATH = re.compile(r'(?:value|path)\s*=\s*\{?\s*"(?P<path>[^"]*)"')
MAPPING_POSITIONAL_PATH = re.compile(r'^\s*\{?\s*"(?P<path>[^"]*)"')
REQUEST_METHOD = re.compile(r"RequestMethod\.(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)")
TYPE_DECL = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed)\s+)*"
    r"(?:class|interface|record|enum)\s+(?P<name>\w+)"
)
METHOD_NAME = re.compile(r"(?P<name>\w+)\s*\(")
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".idea", ".gradle"}


def _annotation_span(lines, i: int):
    """The parenthesised argument text of the annotation on line `i`, and where it ends.

    Read as one balanced span rather than per line: a mapping wraps as soon as it carries
    `produces`, and an `@ApiResponse` above it wraps over four — whose continuation lines
    look exactly like a method declaration to a line scan, which is how `@Content(` once
    became the name of the method under it."""
    text, depth, started = "", 0, False
    for j in range(i, len(lines)):
        for ch in lines[j]:
            if ch == "(":
                depth += 1
                started = True
                if depth == 1:
                    continue
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text, j
            if started:
                text += ch
        if not started and lines[j].strip():
            return "", j
    return text, len(lines) - 1


def _mapping_path(args: str) -> str:
    m = MAPPING_NAMED_PATH.search(args) or MAPPING_POSITIONAL_PATH.match(args)
    return m["path"] if m else ""


def _join_route(base: str, sub: str) -> str:
    parts = [p for p in (base.strip("/"), sub.strip("/")) if p]
    return "/" + "/".join(parts)


def _controller_routes(source: str, rel: str, found: dict) -> None:
    """Every `VERB /route` this file handles, mapped to `Class.method` and its line.

    A line scan, not a parser: the three things it needs — the class-level base path, a
    method's mapping annotation, and the declaration under it — are each one line of Java,
    and a mapping annotation is never anywhere else in a file. Annotations pile up until a
    declaration consumes them, so the comments and the other annotations Spring code puts
    between `@PostMapping` and its method cost nothing."""
    lines = source.splitlines()
    base, cls, pending, i = "", Path(rel).stem, None, 0
    while i < len(lines):
        stripped, at = lines[i].strip(), i
        i += 1
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("@"):
            args, end = _annotation_span(lines, at)
            i = end + 1
            mapping = MAPPING_ANNOTATION.match(stripped)
            if mapping:
                verbs = ([mapping["kind"].upper()] if mapping["kind"] != "Request"
                         else [m["verb"] for m in REQUEST_METHOD.finditer(args)] or list(HTTP_VERBS))
                pending = (verbs, _mapping_path(args))
            continue
        declared = TYPE_DECL.match(stripped)
        if declared:
            cls = declared["name"]
            base, pending = (pending[1] if pending else ""), None
            continue
        name = METHOD_NAME.search(stripped) if pending else None
        if name:
            verbs, sub = pending
            for verb in verbs:
                found[f"{verb} {_join_route(base, sub)}"] = (rel, at + 1, f"{cls}.{name['name']}")
        pending = None


@functools.lru_cache(maxsize=None)
def spring_handlers(root: Path) -> dict:
    """`VERB /route` → (repo-relative file, line, `Class.method`) for this checkout.

    Only files that declare themselves controllers are opened, so a repo with no Spring in
    it pays one directory walk and nothing else. Resolved here rather than in the diagram
    generator because that generator sees a trace, which carries the route and no code."""
    found: dict = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".java"):
                continue
            path = Path(folder) / name
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "@RestController" not in source and "@Controller" not in source:
                continue
            _controller_routes(source, str(path.relative_to(root)), found)
    return found


def _with_handlers(index: dict, root: Path) -> dict:
    """Hang the entry point on every call arrow whose route this checkout still serves.

    A route the branch deleted simply gets no row — the base-ref sidecar is resolved
    against the work tree too, and a link into a method that is no longer there would be
    worse than the route alone."""
    handlers = None
    for entry in (index.get("details") or {}).values():
        called = GENSEQ_CALL_TITLE.search(entry.get("title", ""))
        if not called:
            continue
        if handlers is None:
            handlers = spring_handlers(root)
        hit = handlers.get(f'{called["verb"]} {called["route"]}')
        if hit:
            rel, line, name = hit
            entry["handler"] = {"name": name,
                                "href": f"vscode://file/{(root / rel).resolve()}:{line}:1"}
    return index


def genseq_details(rel: str, root: Path) -> str:
    """The sidecar the generator filed beside the diagram, carried into the page.

    Inlined rather than fetched: review.html is opened from file://, where fetch() of a
    neighbouring file is blocked, and the guide has to survive being mailed as one file."""
    if not rel.endswith(".genseq.puml"):
        return ""
    sidecar = root / (rel[: -len(".puml")] + ".json")
    if not sidecar.is_file():
        return ""
    return _details_carrier(sidecar, root)


def _details_carrier(sidecar: Path, root: Path) -> str:
    # `<` is the only character that can end a <script> block early, and a JSON string
    # may legally spell it \\u003c — so the payload stays valid JSON and inert to the
    # HTML parser without any un-escaping step on the other side.
    index = _with_handlers(json.loads(sidecar.read_text(encoding="utf-8")), root)
    payload = json.dumps(index, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" class="genseq-details">{payload}</script>'


def genseq_details_at_render(row, assets: Path, root: Path) -> str:
    """The sidecar as it stood when this diagram was drawn — not as it stands now.

    The handles PlantUML drew into the picture are generation-time ids derived from the
    payload behind each arrow, and a generated payload carries per-run values: a
    timestamp inside a description, a database-assigned row id. So every arrow whose body
    holds one is re-identified by the next run of the suite, while its SQL neighbours —
    whose statement text does not move — keep the id they had.

    That matters because the picture is rendered once, early, and the report is built
    later. Anything in between that re-runs the acceptance suite (the trace capture does)
    advances the sidecar a generation past the pictures. Read live, it then hands the page
    ids that no arrow in the SVG carries, and `GENSEQ_JS` unwraps each of them as a dead
    handle: the `200 ⊕` on an HTTP response stops opening its JSON body and becomes plain
    text, while `select owners ⊕` beside it still works. The failure is silent and reads
    as "the response handles were never wired".

    So the renderer copies the sidecar next to the SVG it drew from it, and this prefers
    that copy. The work tree stays the fallback, for a manifest written before the
    renderer took the snapshot.
    """
    name = (row.get("new_details") or "").strip()
    if name and (assets / name).is_file():
        return _details_carrier(assets / name, root)
    return genseq_details(row["source"], root)


def genseq_details_at_base(row, assets: Path, root: Path) -> str:
    """The same sidecar as of the base ref, for the diagram's `Old` pane.

    The handles PlantUML draws are generation-time ids, and an id is derived from the
    payload — so a request body that changed on this branch has a different id on each
    side. With only the work tree's sidecar in the page, every such handle on the old
    render resolved to nothing and `GENSEQ_JS` dropped it as a dead one: the pane looked
    wired (cursor, hit area) and expanded nothing. The ids that happened to survive —
    SQL whose statement did not change — kept working, which is what made the failure
    read as "bodies are broken" rather than "the payloads for that side are missing".

    Both carriers go into the page and the reader merges them, work tree first. Ids are
    content-derived, so an id in both sides means the same payload on both.
    """
    name = (row.get("old_details") or "").strip()
    return _details_carrier(assets / name, root) if name and (assets / name).is_file() else ""


def read_manifest(path: Path):
    rows = []
    if not path.is_file():
        return rows
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            rows.append(dict(zip(header, line.split("\t"))))
    return rows


VIEW_WORDS = {"new": "New", "old": "Old"}


def dgm_views_html(panes, initial: str = "diff") -> str:
    """The Diff / New-Old control and its panes — the ONE implementation of it.

    `panes` is an ordered list of `(view, inner-html)` with `"diff"` first. Three states
    off two buttons: the second holds both words and toggles between them, with the live
    one underlined, because a third pill costs as much room as the two that carry the
    argument and this control has to sit above a tall sequence diagram as comfortably as
    above a small structural one.

    `initial` is which of the three the widget opens on. It is "diff" for a *structural*
    diagram, where the delta is a clean two-colour drawing and the whole point of the
    section. Two callers pass "new" instead, for the same reason in two guises: the delta
    is not trustworthy enough to be the first thing read. The UX audit's is a pixel mask
    over a screenshot, and a half-ghosted photo of a form has to be decoded before it says
    anything; a sequence diagram's is drawn from traces, and a run that reorders two
    concurrent calls — or a generator that relabels an arrow — is reported as a change
    nobody made. In both cases the undiffed picture is the one that reads at a glance, and
    the delta stays one click away for when the question is what moved.

    Callers: `render_diagrams` below, for every PlantUML delta, and `drawio_widget_html`,
    for the hand-drawn conceptual model. Behaviour and styling live in `DGM_VIEWS_JS` / the
    stylesheet and are delegated off `document`, so a widget that lands in a section body
    is driven by the same code as one emitted here — a second implementation of this would
    drift within a week.
    """
    pair = [v for v, _ in panes if v in VIEW_WORDS]
    # Nothing to switch to: a lone "Diff" button is a control that does nothing, and a
    # frame colour-coding one state is a legend for a single entry. Emit the picture.
    if not pair:
        return "".join(body for _, body in panes)
    # A caller asking to open on a side that did not render would open on nothing at all,
    # so an absent side falls back to the delta rather than blanking the widget.
    if initial not in {v for v, _ in panes}:
        initial = "diff"
    buttons = ['<button type="button" class="dgm-diff" data-go="diff" '
               f'aria-pressed="{str(initial == "diff").lower()}" '
               'data-tip="the delta &mdash; green for what this branch added, '
               'red and struck through for what it removed">Diff</button>']
    if pair:
        buttons.append(
            '<button type="button" class="dgm-newold" data-go="newold" '
            f'aria-pressed="{str(initial != "diff").lower()}" '
            'data-tip="the diagram itself, undiffed. Click again to swap sides. '
            'Worth reaching for whenever the delta looks wrong: a generated sequence '
            'diagram reorders concurrent calls between runs, and the differ reports that '
            'as a change.">'
            + "/".join(f'<u data-view="{v}"{' class="on"' if v == initial else ""}>'
                       f'{VIEW_WORDS[v]}</u>' for v in pair)
            + "</button>")
    panels = "".join(
        f'<div class="dgmpane" data-view="{v}"{"" if v == initial else " hidden"}>{body}</div>'
        for v, body in panes)
    return (f'<div class="dgmviews" data-state="{initial}"><div class="dgmbar">'
            + "".join(buttons) + "</div>" + panels + "</div>")


def _diagram_views(row, assets: Path, full_svg: Path, root: Path):
    """One diagram's panes: the delta (with its focus chooser inside it) plus whichever
    of the undiffed pair `puml-diff.sh` managed to render. Returns the markup and whether
    a New/Old pair exists — the header only advertises itself as a toggle when it does.

    A sequence diagram opens on `New`, a structural one on `Diff`. Not a preference: a
    structural delta is derived from two files a human wrote, so every mark in it is a
    change somebody made, while a sequence delta is derived from two *recordings*. The
    order of concurrent calls is not stable between runs and the generator's own labels
    move under it, so the differ reliably reports arrows nobody touched — which is what
    the New/Old button's tooltip has always warned about, and what the guide keeps having
    to say out loud next to the picture. Opening on the recording itself puts the reader
    in front of something true first; the delta is one click away, where the claim it
    makes can be taken with the caveat it needs."""
    panes = [("diff", _focus_views(row, assets, full_svg, root))]
    for view, column in (("new", "new_svg"), ("old", "old_svg")):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'))
    if len(panes) == 1:
        return panes[0][1], False
    return dgm_views_html(panes, initial="new" if row.get("kind") == "sequence" else "diff"), True


# `{{drawio:conceptual}}` — the hand-drawn diagram's three pictures, read off
# `.human-review/assets/` at build time.
#
# It exists because this one diagram is the only thing on the page the report *asks the
# reader to go and change*: red is automation's to-do, and the picture is supposed to look
# different once a human has re-laid it out in draw.io. Markup pasted into content.json
# freezes it — the reader re-draws the map, rebuilds the page, and still sees the drawing
# that was current when a model last wrote the section, with a legend still promising a
# layout that has since been drawn. The token keeps the pictures in the files
# `drawio-diff.py` writes, so re-running that step and rebuilding is the whole refresh.
DRAWIO_TOKEN = re.compile(r"\{\{drawio:(?P<name>[A-Za-z0-9_.-]+)\}\}")

# Meanings, not colours: the swatch is already the colour, so the bold goes on the one
# thing the reader cannot see.
#
# The to-do row names the state and stops. It used to carry the whole instruction as well
# — what the red is, and to go open it in draw.io and turn every line black — and that
# sentence read as a puzzle at the size a legend is read at: the reader is standing in
# front of a picture, and the legend was explaining a workflow. The instruction is not
# lost, it is written on the map itself, in red, by `conceptual-model-patch.py`, where
# the person who can act on it is already looking. Both rows are read off the verdict, so
# the page stops saying it the moment it stops being true.
#
# The green row stops at "added by this PR" for the same reason. "New against the base
# branch" was the definition of "added by this PR" — the same fact, restated for a reader
# who has evidently understood it, since they are reading a legend on a diff.
CM_LEGEND_NEW = '<span class="new"><i></i><b>added by this PR</b></span>'

CM_LEGEND_TODO = ('<span class="todo"><i></i>'
                  "<b>still waiting for a manual re-layout</b></span>")


def drawio_widget_html(name: str, assets: Path, root: Path, rebuild: str = "") -> str:
    """The Diff / New / Old widget for one `drawio-diff.py` output set.

    Which pane it opens on is not a style choice, it is a reading of the verdict: while
    anything in the drawing is still red, the delta is a picture of automation's routing
    rather than of the change, and `New` is the pane that answers first. Once the layout
    has been drawn by hand there is no red left, and `Diff` earns the open — which is the
    same rule the author used to have to remember and re-type after every re-layout.
    """
    verdict = {}
    vfile = assets / f"{name}-diff.json"
    if vfile.is_file():
        verdict = json.loads(vfile.read_text(encoding="utf-8"))
    red = bool(verdict.get("red"))
    green = any(not a.get("already_red") for a in verdict.get("added") or [])

    legend = (f'<p class="cmlegend">{CM_LEGEND_NEW if green else ""}'
              f'{CM_LEGEND_TODO if red else ""}</p>') if (green or red) else ""
    # Repeated under `New`, where the red is on screen with nothing else to explain it.
    # The green is not: nothing is coloured green in the undiffed drawing.
    todo_only = f'<p class="cmlegend">{CM_LEGEND_TODO}</p>' if red else ""

    panes = []
    for view, suffix, tail in (("diff", "diff", legend),
                               ("new", "new", todo_only),
                               ("old", "original", "")):
        svg = assets / f"{name}-{suffix}.svg"
        if svg.is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(svg, root)}</div>{tail}'))
    if not panes:
        print(f"[review] no {name}-*.svg under {assets} — run the diagrams step",
              file=sys.stderr)
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-diff.svg</code></p>')
    return (dgm_views_html(panes, initial="new" if red else "diff")
            + rerun_html(verdict.get("rerun"), rebuild, name,
                         verdict.get("drawio_url") or "",
                         verdict.get("drawio_web_url") or "",
                         verdict.get("redraw"), verdict.get("revert"),
                         verdict.get("reveal")))


def drawio_open_html(app_url: str, web_url: str = "") -> str:
    """The two ways to edit the drawing, as links.

    Under the picture and not inside it: a rendered diagram cannot show a cursor, so an
    invitation painted onto the map has to spell out in words that it is clickable — and
    then it is a sentence about tooling sitting on the drawing, re-read every time the
    reader looks at the boxes. In HTML it is just a link, and it can afford to be two.

    They are not the same offer, which is why both are named rather than one being "the"
    link. The **App** opens the file on disk, so an edit lands where the rerun command
    can pick it up. The **Web** editor opens a copy carried in the URL — nothing is
    uploaded, and nothing it saves reaches the repository either. It is the answer when
    draw.io is not installed on this machine, and the reader can tell which is which
    before clicking rather than after.
    """
    links = []
    if app_url:
        links.append(f'<a href="{html.escape(app_url, quote=True)}">App ↗</a>')
    if web_url:
        links.append(f'<a href="{html.escape(web_url, quote=True)}" '
                     'target="_blank" rel="noopener">Web ↗</a>')
    # The product is named once and the two editors are named after it — `draw.io App or
    # draw.io Web` said the brand twice in six words, which is the half of the phrase that
    # carries no information: the choice the reader is making is App or Web.
    return "draw.io " + " or ".join(links) if links else ""


# What a click-to-run offer says on a static page — where it stays visible and explains
# itself rather than being absent from one copy of the report and present in the other.
# `data-tip-served` is what the same button says where it actually works; the probe swaps
# them, so the two readings live next to each other here instead of in the script.
STATIC_RUN_TIP = ("This copy of the report is static, so nothing here can run: serve the "
                  "page — the static badge at the top copies the line that does — and "
                  "this button does the job.")


def reveal_html(reveal: dict | None, name: str) -> str:
    """"this diagram" as a handle on the file, rather than as a noun.

    The sentence already says *edit* it and *re-render* it; the one thing it says nothing
    about is where the thing actually is. And the two words that name it were sitting right
    there, unclickable, at the front of the line. So the subject of the sentence became the
    control: press it and the file is selected on disk, in the window the reader would have
    gone looking for it in.

    Not folded, and not paired with a command to read: revealing a file changes nothing and
    costs nothing to press, which is the one offer under this picture that needs no second
    click and no `reload`. Where it cannot run — a static copy, or a verdict written before
    the command was recorded — the two words are two words again, and the sentence reads
    exactly as it did before any of this.
    """
    if not reveal or not reveal.get("command") or not name:
        return "this diagram"
    aid = declare_action(f"drawio-reveal:{name}", reveal["command"],
                         label=f"Show {name} on disk")
    where = reveal.get("in") or "the file manager"
    # Two words and a plain copy of them, and the same rule as every other offer on this
    # line picks: where nothing can run, the subject of the sentence is a noun again rather
    # than a control that explains why it does not work. There is no `run this` fallback
    # here because there is nothing to fall back to — `open -R` is not a step in anyone's
    # workflow, it is a shortcut for one, and a reader without a server has their own.
    return ('<span class="offer">'
            f'<button type="button" class="runhere" '
            f'data-action="{html.escape(aid, quote=True)}" '
            f'data-tip="{html.escape(STATIC_RUN_TIP, quote=True)}" '
            f'data-tip-served="Selects the file on disk, in {html.escape(where, quote=True)}"'
            '>this diagram</button>'
            '<span class="plainword">this diagram</span></span>')


def _cmdfold(fold_id: str, line: str) -> str:
    """One shell command, folded away: the box, the line, and the button that copies it.

    Nothing runs from in here. The offer to run is up in the sentence, where the reader
    who does not want to read a shell command never has to scroll past one."""
    return (f'<div class="cmdline" id="{html.escape(fold_id, quote=True)}" hidden>'
            f'<code>{html.escape(line)}</code>'
            f'<button type="button" class="copycmd" '
            f'data-copy="{html.escape(line, quote=True)}" '
            'data-tip="Copy the command">Copy</button></div>')


def _run_or_read(fold_id: str, act: str, static_tip: str, served_tip: str,
                 running: str = "", run_label: str = "click here",
                 read_label: str = "run this") -> str:
    """One offer, worded for the copy of the report it is being read in.

    Both routes are in the markup and only one of them is on screen. Off disk the page
    says `run this` and opens the command to copy; served, the probe finds the action and
    the words become `click here`, which runs it — and the command is not shown at all,
    because a shell line beside a control that already runs it is noise.

    It used to offer both at once, `click here (or run this)`, on the reasoning that a
    control missing from one copy of the report teaches the reader the report is
    unreliable. What it actually taught them was that half of every offer on the page was
    for somebody else: on a static copy the button is a promise the page cannot keep, and
    a reader who has a server does not want a command to paste. The honest version of that
    principle is that *an* offer is always there, in the same place, in the same words'
    worth of line — not that both are.

    The two labels are the same word for most callers: `undo your edits` names what the
    offer does, so it reads correctly whether the click runs the command or opens it. Only
    the re-render offer, whose words are a place to press rather than a name, has to say
    `click here` served and `run this` off disk.

    `static_tip` is therefore the tooltip of nothing: the button carrying it is not on
    screen where it would apply. It stays in the signature because the probe swaps it in
    the same pass either way, and a served page that loses its server mid-visit falls back
    to a button that explains itself rather than to one that lies.
    """
    return ('<span class="offer">'
            f'<button type="button" class="runhere"{act} '
            f'data-tip="{html.escape(static_tip, quote=True)}" '
            f'data-tip-served="{html.escape(served_tip, quote=True)}"'
            + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
            + f'>{html.escape(run_label)}</button>'
            f'<button type="button" class="cmdpeek" aria-expanded="false" '
            f'aria-controls="{html.escape(fold_id, quote=True)}" '
            'data-tip="Show the command, to read or to paste in a terminal">'
            f'{html.escape(read_label)}</button></span>')


def revert_html(revert: dict | None, rerun: dict, rebuild: str,
                name: str) -> tuple[str, str]:
    """Undo my edits: back to the drawing this branch committed, which is the green one.

    The sibling offer below this one starts over — base plus the repository's patch script
    — and that lands on a diagram whose new boxes are staged and red *on purpose*, with the
    guardrail still failing. It is the to-do state, and a reader who has just dragged a box
    somewhere wrong is not asking for a to-do; they are asking for the last drawing that
    was not this one. Nothing but the path is needed to find that, which is why this offer
    needs no flag while the redraw needs `--redraw`.

    It used to aim at HEAD, and that failed the first time it was pressed: a hand edit does
    not wait in the work tree to be undone, it gets swept into the next commit that touches
    the file, and from then on HEAD is the mess. `drawio-diff.py` walks back for the newest
    commit whose *drawing* differs, so the step is one picture rather than one sha — and
    the offer is repeatable, which is the thing an undo has to be.

    Both offers are worded by where they land, and neither says "undo" or "start over" on
    its own: to a reader who has not read this file those are the same four words, and the
    two of them land in opposite places — one on the branch's own layout, the other on the
    base with the script's to-do restaged in red.

    Folded like the redraw, and for the same reason: it throws a layout away. The
    difference is where the layout goes. `drawio-diff.py` records a `git stash push`
    rather than a `git checkout --`, so the second click on this control is survivable —
    the sentence says so, because a reader weighing an undo needs to know that before
    pressing it, not afterwards.
    """
    if not revert or not revert.get("command"):
        return "", ""
    line = (f'cd {shlex.quote(revert["cwd"])} \\\n  && {revert["command"]} \\\n'
            f'  && {rerun["command"]} \\\n  && {rebuild}')
    act = ""
    if name:
        aid = declare_action(f"drawio-undo:{name}", line, reload=True,
                             label=f"Undo hand edits to {name} and rebuild this page")
        act = f' data-action="{html.escape(aid, quote=True)}"'
    where = revert.get("short") or revert.get("sha", "")[:8]
    subject = revert.get("subject") or ""
    tip = ("Steps back one drawing, to " + (f"{where} — {subject} — " if where else "")
           + "the newest commit whose picture is not the one on disk. It runs no script "
           "and does not go near the base. Anything still loose in the work tree is "
           "banked, not binned: `git stash pop` brings it back. Press it again to step "
           "back another drawing.")
    fold = f"undo-{name or 'diagram'}"
    # The served hover names the target too. It is the one most readers ever see — the
    # probe swaps it in wherever the button can actually run — and "reloads with the
    # committed drawing back" told them the least at the moment they most needed to know
    # *which* drawing they were about to land on.
    served = ("Runs it here and reloads, with "
              + (f"{where} — {subject} — " if where else "the previous drawing ")
              + "back. Your own edits go to the git stash.")
    return (_run_or_read(fold, act, tip, served,
                         "Putting the previous drawing back…",
                         run_label="undo your edits", read_label="undo your edits"),
            _cmdfold(fold, line))


def redraw_html(redraw: dict | None, rerun: dict, rebuild: str,
                name: str) -> tuple[str, str]:
    """The one offer under this picture that the reader cannot reconstruct: start over.

    Re-laying the map out by hand is what the red asks for, and it is also the only step
    on this page with no way back — the layout is in the file, the file is in the
    repository, and "let me see what the machine drew again" means going and finding a
    revision by hand. The command is half derived and half declared: restoring the diagram
    to its base state falls out of the flags `drawio-diff.py` already ran with, and
    redrawing it is the repository's own patch script, which is why it has to be passed
    in with `--redraw` rather than guessed from a naming convention.

    It throws work away, so it is the only control in this block whose button is inside
    the fold: opening it shows the `git checkout` that discards the layout, and the click
    that runs it is the second click, on a line the reader has by then read.

    Returns the offer and its fold separately — the offer belongs in the sentence, and a
    `<div>` inside a `<p>` closes the paragraph out from under it.
    """
    if not redraw or not redraw.get("command"):
        return "", ""
    # Four stages, and the middle two are the reason this is not two separate offers:
    # restoring the file and redrawing it change the drawing on disk, and the picture in
    # this page is an inlined SVG that only `drawio-diff.py` rewrites. Stopping after the
    # patch script would leave the reader looking at their own layout with a green tick
    # beside it — the same trap the re-render offer exists to close, in the one direction
    # where the reader has just thrown their layout away and has nothing to compare
    # against.
    line = (f'cd {shlex.quote(redraw["cwd"])} \\\n  && {redraw["command"]} \\\n'
            f'  && {rerun["command"]} \\\n  && {rebuild}')
    act = ""
    if name:
        aid = declare_action(f"drawio-redraw:{name}", line, reload=True,
                             label=f"Restore {name} to its base state and redraw it")
        act = f' data-action="{html.escape(aid, quote=True)}"'
    base = redraw.get("base") or "the base branch"
    tip = (f"Throws the hand-drawn layout away: restores the drawing to {base} and runs "
           "the repository's own script over it, which draws what the code has and the "
           "map lacks — in red, as a to-do — again.")
    fold = f"redraw-{name or 'diagram'}"
    return (_run_or_read(fold, act, tip,
                         "Runs it here, then reloads with automation's drawing back",
                         "Putting automation's drawing back…",
                         run_label="start over", read_label="start over"),
            _cmdfold(fold, line))


def _ways_back(undo: str, over: str) -> str:
    """`You can undo your edits or start over.` — both ways back in one short sentence.

    They were a clause each, and each clause spelled its destination out: *to put the
    drawing back as this branch committed it*, *to start over from the base and let the
    script redraw it in red*. That was written to answer a reader who could not tell the
    two apart from `undo` and `start over` alone — and it answered them by putting two
    lines of tooling under a picture, permanently, for the one visit in twenty where
    anything goes back at all.

    The distinction belongs in the hover, where it is read once by the reader who is
    actually choosing, and the line stays a line. What the sentence owes them is that the
    two offers are *different* and that both are here; which one they want is a question
    they are already asking by the time they are pointing at it.
    """
    ways = [w for w in (undo, over) if w]
    if not ways:
        return ""
    return '<span class="rerun-back">You can ' + " or ".join(ways) + ".</span>"


def rerun_html(rerun: dict | None, rebuild: str, name: str = "",
               app_url: str = "", web_url: str = "", redraw: dict | None = None,
               revert: dict | None = None, reveal: dict | None = None) -> str:
    """One line under the drawing: where to edit it, and the two ways to pick the edit up.

    The command is not a convenience. The picture above is inlined into the HTML, and it
    has to be: the boxes are links into the classes they name and the to-do note is a link
    into draw.io, and an SVG loaded through `<img src>` renders those as decoration — the
    reader can see them and cannot click them. So the file on disk and the picture in the
    page are two artefacts, and reloading the browser only ever refreshes the second one.
    That is a thing the page owes the reader an answer to, at the moment they need it, in
    the form of something they can run.

    What it does *not* owe them is three stacked lines of tooling under a diagram. Where
    to edit, an offer to re-render, and a shell command used to be a paragraph, a sentence
    and a code block — read once and then permanently in the way of the picture they sit
    under. They are one sentence now, and the command is folded away behind the end of it:
    the reader who wants to run it here clicks four words, and the reader who wants to
    paste it in a terminal opens the fold. Both are one click; only one of them costs the
    page a code block on every look.

    `rerun` is what `drawio-diff.py` recorded about its own invocation; `rebuild` is how
    this build was started. Neither is reconstructed here — a guessed command that does
    not work is worse than no command, because it is tried first.
    """
    edit = drawio_open_html(app_url, web_url)
    it = reveal_html(reveal, name)
    if not rerun or not rerun.get("command"):
        return f'<p class="dgm-open">Edit {it} in {edit}</p>' if edit else ""
    line = f'cd {shlex.quote(rerun["cwd"])} \\\n  && {rerun["command"]} \\\n  && {rebuild}'
    # Per diagram, because a page can carry several and each one reruns its own. The id
    # is the diagram's name for the same reason every other handle on this page is: so a
    # button that has been on screen since the last build cannot end up running the
    # command belonging to a different picture.
    #
    # `reload`, because the last stage of this line rewrites the very file the browser is
    # displaying. Leaving the reader on the old bytes with a green tick beside them would
    # be the worst possible outcome: the page would look like it had picked the edit up.
    act = ""
    if name:
        aid = declare_action(f"drawio:{name}", line, reload=True,
                             label=f"Re-render {name} and rebuild this page")
        act = f' data-action="{html.escape(aid, quote=True)}"'
    # `runhere` is rendered on the static page too, and says so when pressed rather than
    # being absent from it. A control that disappears between two copies of the same
    # report teaches the reader that the report is unreliable; one that explains what it
    # needs teaches them what served mode is — and the `static` badge in the title row is
    # already holding the line that gets them there.
    #
    # One wording in both worlds. The sentence used to be rewritten when the probe found a
    # server — "pick your edit up" off disk, "update this report" served — on the reasoning
    # that a static page must not promise what it cannot do. But the offer is what the
    # reader wants either way, and a button that says what it needs when pressed teaches
    # them what served mode is; a sentence that quietly reads differently in the two copies
    # of the same report teaches them the report is unreliable.
    fold = f"cmd-{name or 'diagram'}"
    # Gentlest first. The three offers on this line go one way only — refresh the report,
    # undo my edits, start over — and a reader who stops reading partway through has
    # stopped on the milder of the two ways back, not on the one that discards the branch's
    # drawing as well as their own.
    undo, undo_fold = revert_html(revert, rerun, rebuild, name)
    over, over_fold = redraw_html(redraw, rerun, rebuild, name)
    return ('<div class="rerun">'
            f'<p class="dgm-open">{f"Edit {it} in {edit}, then " if edit else ""}'
            + _run_or_read(fold, act, STATIC_RUN_TIP,
                           "Runs it here, then reloads with the new picture")
            + " to update the report."
            # Second sentence, same line: it is the same subject — this drawing, and what
            # you can do to it — and a paragraph of its own would put the offer nobody
            # takes on most visits on a line of its own under the picture.
            + _ways_back(undo, over) + '</p>'
            + _cmdfold(fold, line) + undo_fold + over_fold + '</div>')


def expand_drawio(text: str, out_dir: Path, root: Path, rebuild: str) -> str:
    return DRAWIO_TOKEN.sub(
        lambda m: drawio_widget_html(m["name"], out_dir / "assets", root, rebuild), text)


def _pretty(name: str) -> str:
    """`DomainModel` is a filename; `Domain Model` is a heading. Split the camel hump,
    which leaves acronyms (DB) and already-spaced names untouched."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


DGM_SRC_ANCHOR = re.compile(r'<a class="dgm-src"(?P<attrs>[^>]*)>(?P<face>[^<]+)</a>')


def shorten_dgm_src(markup: str) -> str:
    """A diagram header names its file by **name**, with the path on hover.

    `petclinic-backend/docs/ConceptualModel.drawio.png` spends two segments on where the
    repository keeps its documents before reaching the one word that answers "which
    drawing is this?" — and it does it in the header of a card whose title already said
    *Conceptual Model*. The name alone is the same answer in a quarter of the width; the
    path is still one hover away, which is where a reader goes only when they want to
    find the file rather than read the picture.

    This is the rule `srcbar_html` already applies to every quoted block on the page,
    down to the wording of the tip, so the two rows read the same way. It runs over
    rendered markup rather than at each call site because the conceptual model's header
    is written by hand into `content.json` — a rule enforced only in `_source_link`
    would hold for the generated PlantUML cards and quietly not for the one card the
    reader is being asked to go and edit.

    Idempotent: a face with no slash left in it is already short (or is a file at the
    repository root, which has no path to move)."""
    def one(m: re.Match) -> str:
        rel = html.unescape(m["face"])
        if "/" not in rel:
            return m.group(0)
        attrs = re.sub(r'\s+data-tip="[^"]*"', "", m["attrs"])
        tip = html.escape(f"Open in VS Code: {rel}", quote=True)
        return (f'<a class="dgm-src"{attrs} data-tip="{tip}">'
                f'{html.escape(Path(rel).name)}</a>')
    return DGM_SRC_ANCHOR.sub(one, markup)


def _source_link(rel: str, root: Path) -> str:
    """The path already shown on the right of the header, made the link to the file.

    It used to be plain text with a second `<a>name.puml</a>` under the title — two
    controls for one destination, and the shorter of the two said less."""
    if (root / rel).is_file():
        return shorten_dgm_src(
            f'<a class="dgm-src" href="vscode://file/{(root / rel).resolve()}:1:1">'
            f'{html.escape(rel)}</a>')
    return f'<span>{html.escape(rel)}</span>'


def _provenance(rel: str, root: Path) -> str:
    """Links back to what produced a diagram: the test that generated it, and the .puml.

    A sequence diagram is evidence only if the reviewer can reach the scenario behind it.
    The generator files each diagram as `<test-file>.genseq.puml` next to its test, so the
    source is derivable rather than something the guide has to be told."""
    links = []
    if rel.endswith('.genseq.puml'):
        test = rel[: -len('.genseq.puml')]
        if (root / test).is_file():
            links.append(f'<a class="srcref" href="vscode://file/{(root / test).resolve()}:1:1">'
                          f'generated by {html.escape(Path(test).name)}</a>')
    return ('<p class="prov">' + " ".join(links) + '</p>') if links else ''


# Which radius a reviewer meets first — the change and nothing else.
#
# It opened on the whole diagram for a while, on the reasoning that a pruned view is a
# claim that the rest does not matter. In practice the whole DB and DomainModel deltas are
# a wall of forty unchanged entities with the change somewhere inside, so it moved to one
# hop of context, and then to none: the first question a delta has to answer is *what
# changed*, and every element beside it is a candidate the eye still has to rule out. Zero
# is the only level that cannot mislead about that. Each wider radius is one click away,
# and the reader takes it the moment the change alone does not explain itself.
DEFAULT_FOCUS = "0"


def _focus_views(row, assets: Path, full_svg: Path, root: Path) -> str:
    """The delta at each focus level, one visible at a time, with the chooser above them.

    All of them are inlined rather than fetched on demand: the guide has to survive being
    emailed as a single file, and a chooser whose other options 404 is worse than none.
    """
    levels = []
    for pair in (row.get("focus") or "").split(","):
        level, sep, name = pair.partition(":")
        svg = assets / name
        if sep and svg.is_file():
            levels.append((level, svg))
    levels.append(("all", full_svg))

    if len(levels) == 1:                       # sequence diagrams, and anything unpruned
        return f'<div class="svgbox">{inline_svg(full_svg, root)}</div>'

    default = DEFAULT_FOCUS if any(l == DEFAULT_FOCUS for l, _ in levels) else "all"
    buttons = "".join(
        f'<button type="button" data-level="{html.escape(level)}" '
        f'aria-pressed="{"true" if level == default else "false"}">{html.escape(level)}</button>'
        for level, _ in levels
    )
    boxes = "".join(
        f'<div class="svgbox" data-level="{html.escape(level)}"'
        f'{"" if level == default else " hidden"}>{inline_svg(svg, root)}</div>'
        for level, svg in levels
    )
    return (
        '<div class="focus"><span class="lbl">Diff + extra neighbours:</span>'
        + buttons + "</div>" + boxes
    )


#: `[[src://<test>:<line>{tip} <scenario title>]]` — the handle the sequence generators
#: leave on the scenario a picture draws. It is the only record of *which* tests in a file
#: carry `@generate_sequence`: the tag is in the source, but the generator is what decides
#: it produced a drawing, and the drawing is what this page can link to.
#:
#: Two places carry it, and both are read. `title …` is where it lives now that a picture
#: is one scenario — the scenario names the diagram. `== … ==` is the chapter divider a
#: picture drawn per *file* put above each scenario inside it, which is still what the
#: committed diagrams of a repository that has not been through the generator split look
#: like, and what `renderDiagram` still writes when handed several scenarios at once.
GENSEQ_HANDLE = re.compile(
    r"^(?:title|==)\s*\[\[src://(?P<path>[^\s:{\]]+):(?P<line>\d+)"
    r"(?:\{(?P<tip>[^}]*)\})?\s*(?P<title>[^\]]*?)\s*\]\]")


def test_of_genseq(source: str, root: Path) -> str:
    """Which test a generated sequence was drawn from.

    It used to be the file name with `.genseq.puml` cut off, because there was one diagram
    per test file. There is now one per scenario, named `<test>.<scenario-slug>.genseq.puml`
    — and a test file has dots of its own (`add-visit.spec.ts`), so the slug cannot be told
    from the extension by looking. The checkout is asked instead: cut the suffix, and if
    what is left is not a file, cut one more dotted segment and try again.

    A diagram whose test is not in this checkout at all — the case `_unquoted_note` exists
    for — cannot be checked that way, so the slug is cut on its shape: all lowercase,
    digits and dashes, over something that still has an extension.
    """
    rel = source[: -len(".genseq.puml")] if source.endswith(".genseq.puml") else source
    if (root / rel).is_file():
        return rel
    head, dot, slug = rel.rpartition(".")
    if dot and (root / head).is_file():
        return head
    if dot and "." in head and re.fullmatch(r"[a-z0-9-]+", slug):
        return head
    return rel


def pair_anchor(rel: str) -> str:
    """The id of the pair a sequence is drawn in, derived from the diagram's own path.

    Derived rather than counted, because the thing that links to it — the 🕵️ on a
    covering-tests row, a tab away — knows the test and nothing else about this tab. A
    path is unique inside a checkout, so the slug is too. It is the *diagram's* path that
    is passed now, one per scenario, so two scenarios of one file get two anchors."""
    return "seq-" + re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")


#: The three kinds of test the page names, and what each one is — the Tests tab's own
#: vocabulary, word for word, because a reader who learnt `UI` over there must not have to
#: learn it again here. The words that follow each chip are that tab's legend line; they
#: ride on the hover rather than being printed again under the tab title, since a second
#: copy of a legend is a second place for the two to disagree.
TEST_CATS = {
    "e2e":  ("UI", "clicks the screen"),
    "api":  ("API", "REST/MCP"),
    "unit": ("unit", "one isolated component"),
}

#: A lifeline declaration in a generated sequence: `participant Browser`,
#: `actor "A vet" as Vet`, `participant UI as "Pet Clinic UI"`. Only the declarations are
#: read — an arrow can name a lifeline that was never declared, and the ORDER of the
#: declarations is the part that matters here.
SEQ_DECL = re.compile(
    r'^(?:participant|actor|database|queue|collections|boundary|control|entity)\s+'
    r'(?P<first>"[^"]*"|\S+)(?:\s+as\s+(?P<second>"[^"]*"|\S+))?\s*$')

#: The first arrow, for a diagram that declares no lifelines at all — PlantUML lets a
#: sender spring into existence on its first message, and a generator that leans on that
#: still has a driver; it is just never announced.
SEQ_ARROW = re.compile(r'^(?P<from>"[^"]*"|[^\s"<>-]+)\s*-+>+\s*'
                       r'(?P<to>"[^"]*"|[^\s"<>:-]+)\s*:')

#: What a driver lifeline is called when the run went through the screens. The generators
#: name their own driver — Playwright's is `Browser`, a `@SpringBootTest`'s is `Client` —
#: and this page already writes that convention down in `c2-from-sequence.py`. Anything
#: not on this list is a synthetic client calling the contract directly, which is `api`.
SEQ_UI_DRIVERS = re.compile(r"\b(browser|ui|frontend|front-end|chrome|playwright|page)\b",
                            re.I)


def _pair_cat(puml_rel: str, root: Path, authored: str | None = None) -> str | None:
    """Which kind of test drew this sequence: `e2e`, `api` or `unit`.

    Derived from the diagram, not from the file name, and for the same reason the pairing
    itself is derived: a `.spec.ts` is a Playwright run in one module and a component test
    in the next, and the page must not settle that by guessing at a path. The diagram is a
    record of what the run actually did, and its first lifeline is the end the test was
    driving from — `Browser` for a run through the screens, `Client` for a `@SpringBootTest`
    calling the contract in the same JVM. That is the very distinction the tab's own tip
    draws, so reading it off the picture keeps the badge and the tip from ever disagreeing.

    One lifeline is `unit`: a test that made no call anyone else could observe is a test of
    one isolated component, which is exactly what the Tests tab means by the word.

    A diagram that declares nothing is read off its first arrow instead — PlantUML lets a
    sender exist from its first message, and such a diagram has a driver too; it just never
    announced one. A diagram with neither gets no chip rather than a guessed one: an empty
    space says "not classified", and a wrong pill says something false in the page's own
    confident voice.

    `authored` wins when it is given. The classification above is a reading of a generated
    file, and a suite that names its driver something this has never heard of should be
    able to say so in the content file rather than wait for this list to grow.
    """
    if authored in TEST_CATS:
        return authored
    try:
        text = (root / puml_rel).read_text(encoding="utf-8")
    except OSError:
        return None
    lifelines = []
    for raw in text.splitlines():
        m = SEQ_DECL.match(raw.strip())
        if not m:
            continue
        first, second = m["first"].strip('"'), (m["second"] or "").strip('"')
        # `participant "Pet Clinic UI" as UI` and `participant UI as "Pet Clinic UI"` both
        # exist; the quoted side is the label whichever order it came in, and with neither
        # quoted PlantUML's own rule applies — the alias is the second.
        label = first if not second or m["first"].startswith('"') else second
        if label not in lifelines:
            lifelines.append(label)
    if not lifelines:
        for raw in text.splitlines():
            m = SEQ_ARROW.match(raw.strip())
            if m:
                lifelines = [m["from"].strip('"'), m["to"].strip('"')]
                break
    if not lifelines:
        return None
    if len(lifelines) == 1:
        return "unit"
    return "e2e" if SEQ_UI_DRIVERS.search(lifelines[0]) else "api"


def _cat_chip(cat: str | None) -> str:
    """The kind, as the chip the Tests tab wears — same words, same palette, same pill.

    Copied rather than shared, like `FILE_PAGE` above it: the Tests tab is an included
    asset that builds its own markup, and the two will not be made to import from each
    other. What is shared is the decision, which is written down in `TEST_CATS`."""
    if cat not in TEST_CATS:
        return ""
    label, what = TEST_CATS[cat]
    return (f'<span class="testcat" data-cat="{cat}"'
            f' data-tip="{html.escape(label, quote=True)} &mdash; '
            f'{html.escape(what, quote=True)}">{html.escape(label)}</span>')


def _scenarios_drawn(puml_rel: str, test_rel: str, root: Path) -> list[tuple[int, str]]:
    """Which scenarios this diagram actually drew, as (line, title).

    Read from the committed `.puml` rather than by looking for `@generate_sequence` in the
    test: the tag is a request, the chapter is the record that the request was granted and
    that there is a picture on this page to link to. A scenario the generator skipped has
    no chapter and gets no 🕵️. One per file since the generator started drawing a picture
    per scenario — the list survives because a diagram this page was built before that
    still has several, and reading it is how this page keeps working on both.

    Only the test's own handles count, and only on a `title` or `==` line. The same
    `src://` scheme is on every class and endpoint the diagram names, and a line number
    from `OwnerRepository.java` resolved against a feature file would point at nothing."""
    try:
        text = (root / puml_rel).read_text(encoding="utf-8")
    except OSError:
        return []
    found = {}
    for line in text.splitlines():
        m = GENSEQ_HANDLE.match(line.strip())
        if m and m["path"] == test_rel:
            found.setdefault(int(m["line"]), (m["title"] or "").strip())
    return sorted(found.items())


#: The Tests tab's file glyph, and the corner mark that says what the branch did to that
#: file. Copied here from `requirements-map.html` deliberately: one vocabulary for "what
#: happened to this file" across the page, drawn the same way in both places, so a reader
#: who has learnt it on one tab is not taught it again on another. A page with a `+` is a
#: file that did not exist, a page with a pencil is one this branch edited, a bare page is
#: one it left alone — and the words those glyphs replace are on the hover, never dropped.
FILE_PAGE = ('<path class="fm-page" d="M9.5 1.1l3.4 3.5.1.4v2h-1V6H8V2H3v11h4v1H2.5l-.5-.5'
             'v-12l.5-.5h6.7l.3.1zM9 2v3h2.9L9 2z"/>')
FILE_PLUS = '<path class="fm-mark" d="M13 16h-1v-3H9v-1h3V9h1v3h3v1h-3v3z"/>'
FILE_PENCIL = ('<path class="fm-mark" d="M8.65 13.65 13.65 8.65 15.55 10.55 10.55 15.55Z'
               'M8.65 13.65 10.55 15.55 7.9 16.3Z"/>'
               '<path class="fm-mark" d="M12.5 9.8 14.4 11.7 13.75 12.35 11.85 10.45Z"/>')

#: `<span class="code-badge" data-diff="new" data-tip="…">new file</span>` — the words
#: `srcbar_html` prints at the end of a source bar.
CODE_BADGE = re.compile(
    r'<span class="code-badge"[^>]*data-tip="(?P<tip>[^"]*)"[^>]*>(?P<label>[^<]*)</span>')


def _badge_as_glyph(bar: str) -> str:
    """The bar's own `new file` / `2 lines changed` badge, drawn instead of spelled.

    `NEW FILE` in caps beside a file name is read before the name is — a label louder than
    its subject, on a row whose subject is the file. The Tests tab settled this one level
    down already: the same page glyph, marked `+` or pencil in its corner, with the words
    it replaces moved into the hover. This is that decision applied to the row the Sequence
    tab puts above a quoted test, and it is the same drawing, not a lookalike.

    Keyed off the badge's words rather than its `data-diff`, exactly as the Tests tab keys
    it: `new file` and `new code` are both `new` to git and are not the same fact — one is
    a file that did not exist, the other is fresh lines inside one that did.
    """
    def swap(m):
        label = m["label"]
        kind = ("new" if label.startswith("new file")
                else "unchanged" if label.startswith("unchanged") else "edited")
        mark = {"new": FILE_PLUS, "edited": FILE_PENCIL, "unchanged": ""}[kind]
        tip = m["tip"]
        return (f'<span class="filemark" data-kind="{kind}" role="img"'
                f' aria-label="{html.escape(label, quote=True)}"'
                f' data-tip="{label[:1].upper()}{label[1:]} &mdash; {tip}">'
                f'<svg viewBox="0 0 16 16" aria-hidden="true">{FILE_PAGE}{mark}</svg></span>')

    return CODE_BADGE.sub(swap, bar, count=1)


#: The header `extract-snippet.py` puts at the top of every quoted block: the two handles,
#: the file name with the lines it quotes, and what changed in it. Matched rather than
#: rebuilt, because only that module knows what the bar says — the window may have snapped
#: past a leading comment, and the badge is computed against the review's own base. It has
#: no nested `<div>`, so the first `</div>` is its own; anything else would need a parser.
SRCBAR = re.compile(r'<div class="srcbar">.*?</div>', re.S)


def _fold_over(quoted: list[str]) -> tuple[str, list[str]]:
    """Move the first quoted block's source bar out of the block and onto the fold's row.

    The row above a quoted test used to read `the test · lines 60–61,70–94,124–155`, and
    the bar immediately below it read `AddVisitApiTest.java:60-61,70-94,124-155`. The same
    line numbers twice, the second time beside the file they belong to — so the first copy
    was saying nothing the second did not say better, and it cost a row on a tab whose
    whole shape is one row per thing.

    What is left of that row is the only thing it ever said that the bar does not: whether
    the test is open. So the control and the bar become one line — `Show Test`, then the
    file, its lines, and what changed in it — and the block underneath keeps the code
    alone. Only the first block's bar moves: a second excerpt of the same file is a
    different window and still has to name itself.
    """
    if not quoted:
        return "", []
    m = SRCBAR.search(quoted[0])
    if not m:
        return "", list(quoted)
    return (_badge_as_glyph(m.group(0)),
            [quoted[0][: m.start()] + quoted[0][m.end():], *quoted[1:]])


def _folded_pair(puml_rel: str, test_rel: str, pieces: list[str],
                 quoted: list[str] = (),
                 scenarios: list[tuple[int, str]] = (),
                 cat: str | None = None) -> str:
    """The test and the sequence its run recorded, foldable together — with the quoted
    test folded closed inside it, and the whole pair folded closed too.

    Closed, because a sequence is tall. One of them is three or four screens of arrows, and
    a tab that opens on four of those opens on a wall: the reader scrolls past pictures
    they did not ask for to find out what is even on the tab. Closed, the tab opens on its
    own table of contents — one line per test — and the reader picks. The folding is done
    by `SEQFOLD_JS` rather than by leaving out this `open`, because the click targets
    inside every diagram are measured with `getBBox()` while the page loads, and
    `getBBox()` inside a closed `<details>` returns zeros; see that script.

    Both halves fold, which is the older correction: the fold used to close over the quoted
    test alone and leave the diagram standing underneath, orphaned. A sequence is a drawing
    of one test — without the test above it, it is a picture of nothing.

    The summary names the *scenarios*, not the file — `AddVisitApiTest: remembers the vet
    who attended it`, which is what the generator wrote into the diagram's own title and
    what the Tests tab calls it. The path is not lost: it is the summary's tooltip, and the
    fold's row under it names the file, the lines it quotes and what changed in them. A
    pair whose generator recorded no chapter falls back to the basename, all there is.

    It leads with the kind of test this is, in the Tests tab's own chip. Shut, this tab is
    a list of sentences, and "which of these went through a browser?" was a question
    the reader could only answer by opening each one and looking at the top lifeline —
    which is where the chip reads it from anyway. Leading, not trailing: the chips line up
    into a column the eye can run down, and a kind that arrives after the sentence arrives
    after it was needed.
    """
    titles = [t for _, t in scenarios if t]
    name = (" · ".join(html.escape(t) for t in titles) if titles
            else html.escape(test_rel.rsplit("/", 1)[-1]))
    src = ""
    bar, blocks = _fold_over(list(quoted))
    if blocks:
        src = ('<details class="testsrc">'
               f'<summary><span class="foldlbl"></span>{bar}</summary>'
               + "\n".join(x.strip("\n") for x in blocks)
               + "</details>\n")
    return (f'<details class="testpair" open id="{pair_anchor(puml_rel)}"'
            f' data-test="{html.escape(test_rel)}">'
            f'<summary data-tip="{html.escape(test_rel)}">'
            f'{_cat_chip(cat)}{name}</summary>'
            + src
            + "\n".join(x.strip("\n") for x in pieces)
            + "</details>")


def _line_spans(ref_tail: str) -> list[tuple[int, int]]:
    """`35-48,52-65` → [(35, 48), (52, 65)]; `12` → [(12, 12)].

    One snippet reference can carry several ranges — that is how a test is quoted without
    the forty lines of setup between its two halves. Anything unparseable yields nothing,
    which is the honest answer: a reference this cannot read is a reference that cannot be
    said to cover any scenario."""
    spans = []
    for part in ref_tail.split(","):
        lo, _, hi = part.strip().partition("-")
        try:
            spans.append((int(lo), int(hi or lo)))
        except ValueError:
            continue
    return spans


def _share_excerpts(test_rel: str, entries, snippets, used: set, root: Path):
    """Hand each of one test file's pictures the excerpts that belong to it.

    One diagram per test file needed no sharing: every excerpt quoting the file went under
    the one picture. Per scenario, four pictures of `owner-search.feature` would each have
    repeated the same thirty lines, or — the way it worked out before this — the first
    would have taken all of them and the rest would have shown none.

    The split is derived, not authored. An excerpt is a set of line ranges in the content
    file, and a range that contains a scenario's declaration line is an excerpt *of* that
    scenario; the generator already told us which line each picture starts at. What matches
    nothing — a Background, a set of imports, a helper below the last scenario — goes under
    the first picture, where a reader meets it before the scenarios that use it.

    One excerpt often quotes two scenarios at once (`35-48,52-65` is one snippet in the
    content file, not two), and then it goes under *both*. The alternative is to pick one
    and leave the other pair claiming its test is "not excerpted here", which is false —
    and the pairs are folded shut, so a block quoted twice costs the reader nothing until
    they ask for it.

    The excerpts come back as they went in, unrendered: which excerpt belongs to which
    picture is the decision this function exists to make, and it is one a test can check
    by reading refs rather than by searching rendered HTML for line numbers.
    """
    mine = [x for x in snippets if x["ref"].rpartition(":")[0] == test_rel]
    used.update(id(x) for x in mine)
    quoted = {rel: [] for rel, _ in entries}
    lines_of = {rel: [ln for ln, _ in _scenarios_drawn(rel, test_rel, root)]
                for rel, _ in entries}
    first = entries[0][0]
    for x in mine:
        spans = _line_spans(x["ref"].rpartition(":")[2])
        owners = [rel for rel, _ in entries
                  if any(lo <= ln <= hi for lo, hi in spans for ln in lines_of[rel])]
        for owner in (owners or [first]):
            quoted[owner].append(x)
    return quoted


def _unquoted_note(test_rel: str, root: Path) -> str:
    """What to say beside a diagram no snippet quotes.

    Silence would read as "this diagram has no test", which is never true — the manifest
    only knows about a diagram *because* a test generated it. The two things that are
    true are said instead, and they are different: nobody chose an excerpt, or the file
    the generator recorded is not in this checkout. The second one is the reader's cue
    that the pairing is real but the source is not here to read."""
    if (root / test_rel).is_file():
        return (f'<p class="testlead"><span>Generated by '
                f'<a class="srcref" href="vscode://file/{(root / test_rel).resolve()}:1:1" '
                f'data-tip="Open in VS Code">{html.escape(test_rel)}</a>, '
                f'not excerpted here.</span></p>')
    return (f'<p class="testlead"><span>Generated by <code>{html.escape(test_rel)}</code>, '
            f'which is not in this checkout — the diagram is the only record of it '
            f'left.</span></p>')


def _unchanged_sequence(puml_rel: str, test_rel: str, root: Path, out_dir: Path) -> str:
    """The card for a sequence the branch left alone, inside its test's pair.

    The same bare card `render_diagrams` draws for a delta — no title, no provenance line,
    the test's href on the card for the scenario links — with two differences that are
    the whole point: the pill says UNCHANGED in the neutral colour a `puml` context block
    wears, and there is no Diff/New/Old bar, because there is nothing to diff against.
    The picture is drawn from the committed `.puml` by the `puml` block's own renderer,
    and the generator's sidecar rides along so the handles in it still expand."""
    rel = puml_rel
    cache, why_not = _context_svg(rel, root, out_dir)
    body = f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not
    return (f'<div class="diagram dgm-bare"'
            f' data-test-src="vscode://file/{(root / test_rel).resolve()}:1:1">'
            '<div class="head"><span class="badge sev-info">unchanged</span>'
            + _source_link(rel, root) + '</div>'
            + genseq_details(rel, root)
            + body + '</div>')


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path):
    """Each acceptance test next to the sequence its own run recorded.

    They used to be two lists on the same tab — a gallery of diagrams, then a list of test
    snippets — and the reader had to work out which picture belonged to which test from the
    file names. Nothing was hidden and nothing was reliable. The pairing is not a judgement
    call, either: the manifest says which test file each diagram came from, and the
    diagram's own chapter titles say which scenarios inside that file, at which lines. So it
    is derived, not authored.

    Neither side is ever dropped, and neither is ever given a partner it did not produce.
    The manifest lists only the diagrams this branch *changed*, so a quoted test with no
    row in it has two different stories, and they are told apart on disk: its
    `<test>.genseq.puml` is either there, identical to the base — a sequence the branch
    left alone, paired and marked as such, counted as no delta — or it is not there at
    all, and the test goes to a trailing group that says exactly that. The second is the
    more interesting absence, because a tagged test with no recorded trace is a fact about
    the *evidence* rather than a gap in the page; the first used to be filed under it, and
    every such page had to be corrected by hand. A diagram whose test is not quoted, or
    not in this checkout at all, says so under its own heading rather than sitting there
    looking like it came from nowhere."""
    # A diagram the branch DELETED has no picture to pair with a test, and a frame
    # titled by the file it used to be drawn from is the worst of both: it reads as a
    # test, on a tab whose frames ARE the list of tests. It happens whenever a test
    # loses its `@generate_sequence` / `@GenerateSequence` — a fact about the source,
    # visible where the source is quoted rather than as an empty exhibit here.
    rows = [r for r in select_rows(manifest_rows, block)
            if r["kind"] == "sequence" and r.get("status") != "deleted"]
    snippets = list(block.get("snippets", []))
    parts, used = [], set()
    # The registry the 🕵️ on the covering-tests rows reads: one entry per scenario the
    # generator drew, keyed the way that map addresses a row, so the jump is a lookup and
    # not a guess. Filled as the pairs are rendered — a pair that is not on this tab must
    # not be linkable from the other one.
    index = []

    def register(puml_rel, scenarios):
        for line, title in scenarios:
            index.append({"test": f"{test_of_genseq(puml_rel, root)}:{line}",
                          "pair": pair_anchor(puml_rel), "title": title})

    # Every diagram on this tab, in page order, grouped by the test it was drawn from —
    # because the excerpts are quoted per test file and have to be shared out among that
    # file's pictures. A changed one is a manifest row; an unchanged one is a `.puml` on
    # disk that no row mentions, found below.
    plan: dict[str, list[tuple[str, object]]] = {}

    def plan_add(puml_rel, what):
        plan.setdefault(test_of_genseq(puml_rel, root), []).append((puml_rel, what))

    merged = dict(dspec)
    merged.pop("only", None)
    for r in rows:
        plan_add(r["source"], r)

    unchanged = 0
    for test_rel in dict.fromkeys(x["ref"].rpartition(":")[0] for x in snippets):
        here = (root / test_rel).parent
        # Both spellings: one picture per scenario, and the one-per-file a page built
        # before the generator was split still has beside it.
        found = set(here.glob(Path(test_rel).name + ".*.genseq.puml"))
        legacy = root / (test_rel + ".genseq.puml")
        if legacy.is_file():
            found.add(legacy)
        for puml in sorted(found):
            rel = str(puml.relative_to(root))
            if any(rel == q for q, _ in plan.get(test_rel, [])):
                continue          # this branch changed it: it is already a row above
            plan_add(rel, None)
            unchanged += 1

    # An author's say on what kind of test a file holds, keyed by the file the snippet
    # quotes — one kind per test file, which is the grain the content file already writes
    # its references at. Absent, `_pair_cat` reads it off the diagram.
    authored_cat = {x["ref"].rpartition(":")[0]: x.get("cat")
                    for x in snippets if x.get("cat")}

    for test_rel, entries in plan.items():
        quoted_by_pair = _share_excerpts(test_rel, entries, snippets, used, root)
        for puml_rel, row in entries:
            scenarios = _scenarios_drawn(puml_rel, test_rel, root)
            quoted = [snippet_html(x["ref"], x.get("caption"), root)
                      for x in quoted_by_pair[puml_rel]]
            # No lead. The scenario names used to be printed here as deep links, and every
            # one of them was said again a few hundred pixels lower: the diagram's own
            # section headers are those same titles, linked to those same lines, drawn by
            # the generator. Two copies of one list, and the one on the picture is the one
            # that sits where the reader is already looking.
            pieces = [] if quoted else [_unquoted_note(test_rel, root)]
            pieces.append(render_diagrams(merged, root, out_dir, [row], bare=test_rel)
                          if row is not None
                          else _unchanged_sequence(puml_rel, test_rel, root, out_dir))
            parts.append(_folded_pair(puml_rel, test_rel, pieces, quoted, scenarios,
                                      _pair_cat(puml_rel, root,
                                                authored_cat.get(test_rel))))
            register(puml_rel, scenarios)

    orphaned = [x for x in snippets if id(x) not in used]
    tail = block.get("unpaired") or {}
    if orphaned:
        pieces = [""] + [snippet_html(x["ref"], x.get("caption"), root).strip("\n")
                         for x in orphaned]
        parts.append(
            f'<h3 id="{html.escape(tail.get("id", "tests-nosequence"))}">'
            f'{html.escape(tail.get("title", "Tests that record no sequence"))}</h3>'
            + (f'<p>{tail["body"]}</p>' if tail.get("body") else "")
        )
        parts.append('<div class="testpair">' + "\n".join(pieces) + "</div>")

    if not parts:
        return "", 0, 0
    # `"title": ""` means no heading at all, and is worth having: every pair below already
    # names its own scenarios and carries its own source path, so a heading over them can
    # only restate what the tab label said — and it does it above the fold, where the
    # first picture should be. An absent title still gets the default; only an author who
    # typed an empty one is asking for the space back.
    title = block.get("title", "Sequence deltas")
    head = ((f'<h3 id="{html.escape(block.get("id", "sequences"))}">'
             f'{html.escape(title)}</h3>') if title else "")
    head += f'<p>{block["body"]}</p>' if block.get("body") else ""
    # Invisible, and last: nothing to look at, only the map's way back in. `</` cannot
    # appear inside a script element, whatever its type.
    if index:
        parts.append('<script type="application/json" id="hr-genseq">'
                     + json.dumps(index).replace("</", "<\\/") + "</script>")
    # Weight counts every exhibit; changes count only the manifest's rows. An unchanged
    # pair is context, exactly as a `puml` block is, and must not un-strike the tab.
    return ("\n".join(([head] if head else []) + parts) + "\n",
            len(rows) + unchanged + len(orphaned), len(rows))


def select_rows(rows, block) -> list:
    """The manifest rows one diagram block is responsible for.

    Tabs split the gallery by what a diagram *answers* — sequence diagrams sit next to
    the tests that generated them, DB and DomainModel next to each other — so a block
    names either the families it takes (`kind`) or the diagrams themselves (`only`)."""
    kinds = block.get("kind")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        rows = [r for r in rows if r["kind"] in kinds]
    only = block.get("only")
    if only:
        rows = [r for r in rows if r["name"] in only]
    if block.get("except"):
        rows = [r for r in rows if r["name"] not in block["except"]]
    return rows


def render_diagrams(spec, root: Path, out_dir: Path, rows=None, bare: str = "") -> str:
    """`bare` is the test file a pair's heading already names.

    A sequence diagram inside a test pair used to print three answers to one question in
    four centimetres: a title that was the test's file name with `.genseq` on the end, a
    `generated by <test>` line under it, and the `.puml` path on the right — above a fold
    whose summary was that same file name. Only the last is news. So the title and the
    provenance line go, and the href they carried for the scenario links rides on the card
    as `data-test-src` instead."""
    manifest = out_dir / spec.get("manifest", "assets/diagrams/MANIFEST.tsv")
    if rows is None:
        rows = read_manifest(manifest)
        if spec.get("only"):
            rows = [r for r in rows if r["name"] in spec["only"]]
    if not rows:
        return '<p class="sub">No PlantUML diagram changed on this branch.</p>'
    notes = spec.get("notes", {})
    order = {"structural": 0, "sequence": 1}
    # An explicit `only` is a running order, not just a filter: an author who writes
    # ["DomainModel", "DB"] means the domain first. Alphabetical only decides the rest.
    wanted = spec.get("only") or []
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 9),
                                       wanted.index(r["name"]) if r["name"] in wanted else 99,
                                       r["name"]))
    parts = []
    for r in rows:
        note = notes.get(r["name"], "")
        svg_rel = manifest.parent / r["svg"] if r.get("svg") else None
        if svg_rel and svg_rel.is_file():
            body, toggles = _diagram_views(r, manifest.parent, svg_rel, root)
        else:
            body, toggles = (f'<p class="sub">not rendered — see '
                             f'<code>{html.escape(r["diff_puml"])}</code></p>', False)
        test_src = (f' data-test-src="vscode://file/{(root / bare).resolve()}:1:1"'
                    if bare and (root / bare).is_file() else "")
        parts.append(
            f'<div class="diagram{" dgm-toggles" if toggles else ""}{" dgm-bare" if bare else ""}"'
            f'{test_src}>'
            f'<div class="head">'
            + ("" if bare else f'<b>{html.escape(_pretty(r["name"]))}</b>')
            # A badge earns its place by saying something surprising. "modified" is what
            # a diagram in a delta gallery always is, and "structural" is legible from the
            # picture — so only the states that carry information get one.
            + (f'<span class="badge {"sev-high" if r["status"] == "added" else "sev-low"}">'
               f'{html.escape(r["status"])}</span>' if r["status"] != "modified" else "")
            + _source_link(r["source"], root) + '</div>'
            + (f"<p>{note}</p>" if note else "")
            + ("" if bare else _provenance(r["source"], root))
            + genseq_details_at_render(r, manifest.parent, root)
            + genseq_details_at_base(r, manifest.parent, root)
            + body + '</div>'
        )
    return "\n".join(parts)


# The one list. Everything the reviewer has to act on, numbered straight through, because
# a reviewer asking "how much is there for me here?" should get one answer and not three.
# What separates the piles is the card's colour and one badge — not a restart of the
# counter, and not a second surface. There are three of them, in the order the reader can
# act on them: what only they can answer (assumptions), what they have to judge (findings),
# and what is already done (applied fixes).
#
# The list is rendered a pile at a time, and each pile has to know where the one before it
# stopped. That is this counter. It is module state, and state is a thing to justify: the
# alternative is threading a number through `render_block`, which renders blocks one at a
# time by type and has no notion that three of them belong to the same list. Reading the
# offset here rather than hard-coding "fixes come after findings" also means the numbering
# follows the order the content file puts the blocks in, whatever that order is.
_LIST_OFFSET = 0


def reset_list() -> None:
    """Start the numbering over, once per page.

    The offset is module state, so without this the second page built in one process
    continues the first one's numbering — which no build does, and every test that renders
    a pile directly would otherwise have to know about."""
    global _LIST_OFFSET
    _LIST_OFFSET = 0


def _open_list(n: int) -> str:
    """The `<ol>` for the next pile, numbered on from wherever the last one stopped.

    `counter-reset` sets the counter to N so the first `counter-increment` lands on N+1 —
    the number straight after the last item already on the page."""
    global _LIST_OFFSET
    start = _LIST_OFFSET
    _LIST_OFFSET += n
    return (f'<ol class="findings" style="counter-reset:f {start}">' if start
            else '<ol class="findings">')


#: Where a reader can go to find out what a pass actually does. Only the two commands this
#: skill runs are in it, because those are the two it stamps — a source it does not
#: recognise (`assumption`, a human name, a linter) renders as the plain stamp it always
#: was rather than being sent somewhere that does not describe it.
PASS_DOCS = {
    "/code-review": "https://code.claude.com/docs/en/code-review#review-a-diff-locally",
    "/simplify": "https://code.claude.com/docs/en/commands#all-commands",
}


def _finding_source(f, default: str = "") -> str:
    """Which pass raised it, when the content file says so.

    Optional by design: nothing downstream of the two runs records provenance, so an item
    that does not claim a source renders without one rather than being attributed to a
    guess. See SKILL.md, step 1 — a `source` here has to be stamped while the pass that
    produced it is the one running.

    The `default` is for the one pile whose provenance is not a pass and never varies: an
    assumption came from the agent that wrote the code, so it is stamped `assumption` where
    a finding is stamped `/code-review`, and the stamp is not left to be remembered.

    A stamp naming a documented pass is the link to that documentation. The stamp already
    is the question — *what is `/code-review`?* — and answering it in place costs the page
    nothing, where answering it in prose costs a line under the verdict that every reader
    who already knows has to read past."""
    src = (f.get("source") or default).strip()
    if not src:
        return ""
    href = PASS_DOCS.get(src)
    if not href:
        return f'<span class="f-src">{html.escape(src)}</span>'
    # The face is the command, which says nothing about where the link goes; the tooltip
    # spends itself on that half, as every other tooltip on this page does.
    return (f'<a class="f-src" href="{html.escape(href)}" target="_blank" rel="noopener"'
            f' data-tip="What {html.escape(src)} does, in the Claude Code docs">'
            f'{html.escape(src)}</a>')


def _raised_by(items, total: int) -> str:
    """`12 raised — 9 by /code-review, 3 by /simplify`: the review chip's hover.

    Counted off each item's own `source`, the same string the stamp beside it renders, so
    a reader who hovers the chip and then counts the stamps gets the same answer twice.
    Passes appear in the order the content file first mentions them.

    `source` is optional (see `_finding_source`), and an item without one is counted as
    itself rather than folded into whichever pass happens to be first — an unattributed
    finding is a real state, and a hover that hides it is a hover that lies by rounding.
    With nothing attributed at all the breakdown is dropped entirely: `12 raised, 12 of
    them unattributed` is the total said twice."""
    counts: dict[str, int] = {}
    for it in items:
        src = (it.get("source") or "").strip()
        counts[src] = counts.get(src, 0) + 1
    named = [f"{n} by {src}" for src, n in counts.items() if src]
    if not named:
        return f"{total} raised"
    if counts.get(""):
        named.append(f'{counts[""]} with no pass named')
    return f"{total} raised — " + ", ".join(named)


def _finding_refs(f) -> str:
    """The bare `file:line` links — only when nothing else already carries them.

    An item that shows a snippet or a diff already links the file, with a line RANGE, from
    that block's own header. Repeating a bare `file:line` link above it says the same thing
    twice and worse."""
    if f.get("_snippets") or f.get("_diffs"):
        return ""
    return "".join(_ref_link(r) for r in f.get("_refs", []))


def _ref_link(r) -> str:
    """`VetRestController.java:96-100`, with the path it came from on hover.

    The same trade a diff header makes: a repo-relative Java path spends five segments on
    module, `src/main/java` and the org package before it reaches the one word that says
    which file this is, and a line of three such references is a wall no reader parses.
    The path is not dropped, it is moved to the tooltip — and a file at the repo root has
    no path to move, so it gets no tooltip repeating its own name."""
    label = r["label"]
    rel, _, lines = label.rpartition(":")
    name = f"{Path(rel).name}:{lines}" if rel else label
    tip = f' data-tip="{html.escape(rel)}"' if "/" in rel else ""
    return (f'<a class="srcref" href="vscode://file/{r["abs"]}"{tip}>'
            f'{html.escape(name)}</a> ')


def _assumptions_block(spec):
    """The `assumptions` block as the layout declared it, or None if the page declares none.

    The lede counts the coder's pile even when it is empty, and the only sentence it can
    honestly print about an empty one depends on the mode — so it has to find the block
    itself, not infer the pile from the items that happen to be in it."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == "assumptions":
                return b
    return None


def _pile_anchor(spec, kind, fallback):
    """The id the pile's own heading will carry, or None when no such block is laid out.

    Read from the layout rather than assumed: a block may name itself, and a lede whose
    links point at ids no heading has is worse than a lede with no links at all."""
    for t in spec.get("tabs") or []:
        for b in t.get("blocks", []):
            if b.get("type") == kind:
                return b.get("id", fallback)
    return None


def opening_lede(spec) -> str:
    """The shape of the whole list, for whichever pile opens it — and only for that one.

    Computed, and deliberately a line. What stood here was three sentences of prose
    restating the shape of the list directly beneath it ("They are one list: the nine that
    need your judgement first, then the three I applied, greyed out and numbered straight
    on"), which a reader can see. The reader is a developer who came for the findings; the
    counts are the only part of that paragraph they could not have got by looking.

    It is asked for by all three piles and answers only the first, because the piles are
    one list and their order is the content file's to choose. Pinned to `findings`, a lede
    describing three piles renders underneath one the reader has already walked past.
    `_LIST_OFFSET` is still zero exactly until the first pile renders, so the question
    "am I the top of the list?" is already answered and does not need a second flag.
    """
    if _LIST_OFFSET:
        return ""
    # Counts, and the one ordering fact that counting cannot give. Every clause that
    # described how the list *looks* has been cut: the applied fixes are visibly grey and
    # an assumption visibly says "your call", so "greyed out" and "yours to confirm" were
    # the paragraph-the-reader-can-see rule reappearing one clause at a time, inside the
    # line that replaced the paragraph.
    block = _assumptions_block(spec)
    parts = []

    def clause(text, kind, fallback):
        """A count, and the way to the pile it counts.

        The line is the first thing read in the tab and names three chapters further down
        it, so every clause is the jump to its own — the reader was going to scroll looking
        for them anyway. A pile with no block laid out keeps its count as plain text: a
        dead anchor that silently does nothing is worse than a number that never claimed
        to be clickable."""
        at = _pile_anchor(spec, kind, fallback)
        return f'<a href="#{html.escape(at)}">{text}</a>' if at else text

    if spec.get("findings"):
        parts.append(clause(f"{len(spec['findings'])} open, worst first",
                            "findings", "first"))
    if spec.get("autofixes"):
        parts.append(clause(f"{len(spec['autofixes'])} auto-applied",
                            "autofixes", "fixed"))
    # Last, because the first two clauses count what a review pass produced and this one
    # counts what it could not: a reader who has just been told how many items are open
    # and how many were applied is at exactly the point where "and here is what nobody
    # checked" lands. Leading with it puts the softest pile in front of the defects.
    if block is not None:
        # Zero is a number the reader came for, so this clause renders at zero too. A pile
        # that appears only when it is non-empty disappears exactly where it matters most:
        # "the page says nothing about what the coder guessed at" and "the coder was asked
        # and guessed at nothing" are the same blank line, and only one of them is good
        # news. Mode C is the case where a zero would be the lie instead — nobody was in a
        # position to be asked — so it says that rather than counting an empty pile.
        assumed = len(spec.get("assumptions", []))
        # "to check" rather than "assumed by the coder": the first two clauses count work
        # that is done, and this one counts work the reader still owes. Naming the pile
        # after who produced it described its provenance, which the `your call` badge on
        # every card already does; naming it after what is left to do says why it is in a
        # line the reader skims on the way to the list.
        parts.append(clause(
            "coder could not be asked"
            if block.get("mode") == "C" and not assumed
            else f"{assumed} coder assumption{'' if assumed == 1 else 's'} to check",
            "assumptions", "assumed"))
    if not parts:
        return ""
    # The stamp clause went the same way as "greyed out" and "yours to confirm", and it
    # was the last of them: every item carries its source beside its own title, so a line
    # announcing that they do describes the thing directly under it. What is left is
    # counts and one ordering fact — the two things counting the list yourself would not
    # have told you.
    return '<p class="sub counts">' + " &middot; ".join(parts) + "</p>"


def _lede_above(head: str, lede: str) -> str:
    """Above the first pile's heading, not tucked under it.

    The line counts all three piles, so under `Requires human review` it reads as a
    description of the findings and the reader meets `4 coder assumptions to check` as a
    footnote to a heading that has nothing to do with them. Hoisted above, it is what it
    is: the shape of the whole list, before the list starts. Which pile happens to open
    the list is then an editorial choice that cannot move the line."""
    return lede + head if lede else head


def render_findings(findings) -> str:
    if not findings:
        return '<p class="sub">Nothing outstanding \u2014 the automated passes came back clean.</p>'
    items = []
    for f in findings:
        cls, label = SEVERITIES.get(f.get("severity", "info"), SEVERITIES["info"])
        refs = _finding_refs(f)
        items.append(
            f'<li class="{cls.replace("sev-", "n-")}">'
            f'<span class="badge {cls}">{html.escape(label)}</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(findings)) + "\n".join(items) + "</ol>"


def render_assumptions(items, mode: str = "") -> str:
    """What the agent that wrote the code decided without being told — and its alternative.

    This is the one pile on the page no pass can produce. A finding is found by reading the
    diff; an assumption is knowable only from the side that made it, and it lives in exactly
    one place: the transcript of the conversation that did the work. `authoring-sessions.py`
    says whether that conversation is the one running (mode A), an older one on disk whose
    transcript a subagent reads verbatim (mode B), or gone (mode C).

    An empty pile still renders, because the three ways of being empty are not the same
    fact and a blank space would read as the friendliest of them. "Nothing was assumed" is
    a claim; "nobody could be asked" is an admission; and the reviewer has to be able to
    tell which one they are looking at."""
    if not items:
        return {
            "A": '<p class="sub">The conversation that wrote this code was asked what it '
                 'had to guess at, and named nothing.</p>',
            "B": '<p class="sub">The transcript of the conversation that wrote this code '
                 'was read back in full, and it recorded no open question.</p>',
            "C": '<p class="sub">No transcript of the conversation that wrote this code '
                 'survives, so it could not be asked. This is not the agent saying it was '
                 'sure \u2014 it is nobody having been in a position to ask.</p>',
        }.get(mode, '<p class="sub">Nothing was assumed.</p>')
    out = []
    for f in items:
        refs = _finding_refs(f)
        out.append(
            '<li class="n-assumed">'
            '<span class="badge sev-assumed">your call</span>'
            + _finding_source(f, default="assumption")
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f'<p class="f-alt"><b>Read the other way:</b> {f["alternative"]}</p>'
               if f.get("alternative") else "")
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + (f.get("_diffs", "") or "")
            + "</li>"
        )
    return _open_list(len(items)) + "\n".join(out) + "</ol>"


def render_autofixes(fixes) -> str:
    """What the agent already fixed \u2014 the tail of the same list.

    It continues the open findings' numbering on purpose. The two piles are one decision
    split in two: everything with a single obvious right answer was applied, everything a
    second engineer could reasonably disagree about was left. A reviewer who cannot see the
    first pile has to take the size of the second on trust \u2014 and a reviewer shown two lists
    that both start at 1 has to add them up by hand.

    Each item shows its diff rather than describing it. That is the whole difference between
    this and a changelog: the reader sees what was done to their code without leaving the
    page or trusting a sentence about it."""
    if not fixes:
        return '<p class="sub">Nothing was applied automatically \u2014 every finding needed a human.</p>'
    items = []
    for f in fixes:
        refs = _finding_refs(f)
        items.append(
            '<li class="fixed">'
            '<span class="badge sev-fixed">auto-fixed</span>'
            + _finding_source(f)
            + f' <span class="f-title">{f["title"]}</span>'
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_diffs", "") or "")
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return _open_list(len(fixes)) + "\n".join(items) + "</ol>"


# What happened to a test, and what the page calls it. The colour classes are the page's
# existing added/removed vocabulary — the same green and red the diff gutters, the line
# counts in the scope bar and the diagram deltas already use, dark mode included — because
# a fourth palette for the fourth surface would read as a fourth meaning.
# The tab the test ledger belongs to when no block asks for it by hand. It is the tab id
# `run-steps.py` already attributes the `tests` step to; the label above it reads "Tests".
LEDGER_TAB = "requirements"

TEST_STATES = {
    "added":     ("added", "new"),
    "modified":  ("changed", "modified"),
    "deleted":   ("removed", "deleted"),
    "unchanged": ("same", "unchanged"),
}
# A test that is still written but no longer runs. It keeps its diff state — a disabled
# test that was also edited is both — because the two answer different questions: what
# the branch did to the code, and whether the code still holds anything up. A commented-out
# test is flagged `deleted`, which is what it costs the run, and stamped `commented out`,
# which is what it costs to undo.
SILENCED_LABEL = {
    "disabled":  "disabled",
    "commented": "commented out",
}


def _test_changes_module():
    """`test-changes.py`, loaded by path — a hyphen is not an identifier."""
    import importlib.util
    if "test_changes" in sys.modules:
        return sys.modules["test_changes"]
    spec = importlib.util.spec_from_file_location("test_changes", str(TESTCHANGES))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_changes"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_index(rows) -> dict:
    """The manifest, keyed both ways: by `(path, name)` and by name alone.

    Naming the path in the content file is optional, because most test names are unique
    across a change set and repeating the path for each is noise. When one is not unique
    the build says so rather than picking a side."""
    by_key, by_name = {}, {}
    for r in rows:
        by_key[(r["path"], r["name"])] = r
        by_name.setdefault(r["name"], []).append(r)
    return {"key": by_key, "name": by_name}


def resolve_tests(entries, index: dict, root: Path) -> list[dict]:
    """Attach each named test to what the diff says happened to it.

    A test the manifest does not mention is not an error: a requirement is often covered
    by a test nobody touched, and saying so is worth a row. But it has to *exist* — the
    file is parsed for the declaration, and a name that is nowhere in it fails the build,
    for the same reason `resolve_refs` fails on a stale path. A coverage claim that links
    to nothing is worse than no claim."""
    out = []
    for e in entries:
        name, rel = e["name"], e.get("path")
        if rel:
            row = index["key"].get((rel, name))
        else:
            hits = index["name"].get(name, [])
            if len(hits) > 1:
                raise SystemExit(
                    f"[review] the test name {name!r} occurs in {len(hits)} changed files "
                    f"({', '.join(sorted(h['path'] for h in hits))}) — add a 'path' to the "
                    "entry so the page links the right one."
                )
            row = hits[0] if hits else None
        if row is None:
            if not rel:
                raise SystemExit(
                    f"[review] test {name!r} is not in the change set, so it needs a 'path' "
                    "saying which existing file it lives in."
                )
            f = root / rel
            if not f.is_file():
                raise SystemExit(f"[review] test {name!r} names a file that does not exist: {rel}")
            found = _test_changes_module().scan_cases(
                rel, f.read_text(encoding="utf-8", errors="replace")).get(name)
            if found is None:
                raise SystemExit(
                    f"[review] no test called {name!r} in {rel} — the change set did not touch "
                    "it and the file does not declare it either. Fix the name, or the path."
                )
            # Untouched by this branch, but the page still has to say whether it runs: a
            # requirement pinned by a test somebody `@Disabled`d last month is not pinned,
            # and the branch that inherits the claim is where a reader will see it.
            line, silenced = found
            row = {"name": name, "path": rel, "status": "unchanged", "line": line}
            if silenced:
                row["silenced"] = silenced
        out.append(dict(row, note=e.get("note", "")))
    return out


def render_tests(rows, root: Path, flags: bool = True) -> str:
    """The sub-list under one requirement: what pins it, and what the diff did to each.

    The link is the page's ordinary `vscode://file/…` reference, so it inherits the whole
    fallback chain in EDITOR_JS for free — served, top level, or embedded in a webview.
    A test whose *file* was deleted gets no link, deliberately: there is nothing on disk
    to open, and a dead custom URL is the one thing this page never emits."""
    if not rows:
        return ""
    items = []
    for r in rows:
        cls, label = TEST_STATES.get(r["status"], TEST_STATES["unchanged"])
        where = Path(r["path"]).name + (f':{r["line"]}' if r.get("line") else "")
        inner = (f'{html.escape(r["name"])} '
                 f'<span class="tloc">{html.escape(where)}</span>')
        if r.get("line") and not r.get("gone"):
            target = (root / r["path"]).resolve()
            body = (f'<a class="srcref testref" href="vscode://file/{target}:{r["line"]}:1"'
                    f' data-tip="{html.escape(r["path"])}">{inner}</a>')
        else:
            why = ("the file is gone" if r.get("gone") else "no line left to open it at")
            body = (f'<span class="srcref testref tgone"'
                    f' data-tip="{html.escape(r["path"])} — {why}">{inner}</span>')
        # Said after the link rather than in front of it, and in a second vocabulary. The
        # flag column answers "what did the branch do to this test"; the stamp answers
        # "does it still run", which is a different question and can contradict the first
        # — a row flagged `new` and stamped `disabled` is the loudest case on this page,
        # and the one a single fixed-width column would have had to choose between. It
        # also keeps that column aligned: "commented out" is twice the width of the words
        # around it, and a flag that shoves its own row sideways costs more than it says.
        state = ""
        if r.get("silenced"):
            state = (f'<span class="tsilenced" data-tip="Still written; never runs.">'
                     f'{SILENCED_LABEL.get(r["silenced"], r["silenced"])}</span>')
        elif r.get("wasSilenced") and r["status"] != "deleted":
            state = ('<span class="tback" data-tip="Was disabled; runs now.">'
                     "back on</span>")
        note = f' <span class="tnote">{r["note"]}</span>' if r.get("note") else ""
        # Off inside the ledger below, where the group heading already says the word and
        # a column repeating `NEW` twenty-two times is a column of noise. Kept everywhere
        # else, and kept even in the ledger's one mixed group.
        flag = f'<span class="tflag {cls}">{label}</span>' if flags else ""
        items.append(f'<li>{flag}{body}{state}{note}</li>')
    return '<ul class="req-tests">' + "\n".join(items) + "</ul>"


def render_test_ledger(rows, root: Path) -> tuple[str, int]:
    """Every test the change set moved, as one list — `(html, how many it moved)`.

    The requirement lists above answer "is *this* sentence pinned, and by what". They
    cannot answer the question a reviewer asks next, which is the blunt one: *what did
    this branch do to the tests?* A test that pins no requirement anybody wrote down —
    and a deleted one, which by definition is no longer under any requirement — appears
    in no list on the page otherwise. The chip at the top states the count; this is where
    the count is spelled out into names you can click.

    Each test appears exactly once, under the most consequential thing that happened to
    it. Silenced comes first for that reason: a test that is *new* and `@Disabled` is not
    news about coverage, it is news about a test that has never run, and filing it under
    "new" would hide it among twenty-one that do run. The untouched rest are counted in a
    sentence rather than listed — a reviewer scrolling past a hundred unchanged names to
    find the two that went away is a reviewer who stops scrolling.
    """
    groups = [
        ("stopped running", "Still written, and no longer part of any run — nothing "
                            "under them is asserted on any build.", []),
        ("new", "Tests this change set wrote.", []),
        ("gone", "Tests the run has lost — deleted outright, or commented out in place.", []),
        ("edited", "Tests whose body this change set moved: worth reading for what they "
                   "stopped asserting, not only for what they now do.", []),
    ]
    untouched = 0
    for r in rows:
        if r.get("silenced") and r["status"] != "deleted":
            groups[0][2].append(r)
        elif r["status"] == "added":
            groups[1][2].append(r)
        elif r["status"] == "deleted":
            groups[2][2].append(r)
        elif r["status"] == "modified":
            groups[3][2].append(r)
        else:
            untouched += 1

    moved = sum(len(g[2]) for g in groups)
    if not moved and not untouched:
        return "", 0
    blocks = []
    for name, why, items in groups:
        if not items:
            continue
        # The one group whose rows do not share a fate: a silenced test may be new,
        # edited or untouched, and which it is changes what the reader does about it.
        mixed = name == "stopped running"
        blocks.append(
            f'<section class="tgroup{" tgroup-off" if mixed else ""}">'
            f'<h3>{html.escape(name)} <b>{len(items)}</b></h3>'
            f'<p class="sub">{html.escape(why)}</p>'
            + render_tests(items, root, flags=mixed)
            + "</section>"
        )
    rest = (f'<p class="sub">{untouched} more test'
            f'{"s" if untouched != 1 else ""} in the files this change set touched, '
            "left exactly as they were.</p>") if untouched else ""
    return '<div class="tledger">' + "".join(blocks) + "</div>" + rest, moved




def _ms(value) -> str:
    """`1658` → `1.7s`. Under a second stays in milliseconds: a step that took 43ms and
    one that took 430ms are a different kind of fast, and `0.0s` says neither."""
    ms = int(value or 0)
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def render_traces(doc: dict, root: Path, out_dir: Path,
                  touched: set[tuple[str, int]] | None = None) -> tuple[str, int]:
    """What the run recorded, as a registry the 📺 on the covering-tests rows reads.

    Nothing visible. There used to be a list here — "Step through what the tests did",
    one collapsible row per recording with the viewer framed inside it — and every word
    on it was already on the covering-tests map above: the test's title, its file and
    line, whether it passed. The one thing the row added was the way into the recording,
    and that is now the 📺 itself: served, it opens the viewer in a window of its own,
    where a three-pane application belongs, instead of in 78vh of a text column that has
    to be scrolled to keep the snapshot pane in view.

    The registry keys a recording by the test's file basename and declaration line, which
    is exactly how the map addresses a row, so the pairing is a lookup and not a guess.
    `viewer` is the copied trace viewer, relative to the page; `trace` is the zip; `cmd`
    is the line that opens the same recording natively, for a reader holding the page as
    a file — from the zip, from Pages — where the viewer cannot fetch anything. It is
    written whether or not that reader exists, because the build cannot know which of
    the two is reading. `touched` is accepted for the caller's sake and no longer changes
    what is emitted: with no rows there is no order to put the branch's own tests in.
    """
    tests = doc.get("tests") or []
    if not tests:
        return "", 0
    # Where this page was built, said the way a terminal at the repo root would say it:
    # `.human-review` is only the default, and a command naming a directory the reader does
    # not have is worse than no command at all.
    try:
        here = out_dir.resolve().relative_to(root.resolve())
    except ValueError:
        here = out_dir.resolve()
    entries = []
    for t in tests:
        if not t.get("trace"):
            continue
        key = Path(t.get("file", "")).name + (f':{t["line"]}' if t.get("line") else "")
        entries.append({"test": key, "trace": t["trace"], "status": t.get("status", ""),
                        "cmd": f"npx playwright show-trace {shlex.quote(str(here / t['trace']))}"})
    reg = {"viewer": doc.get("viewer") or "", "tests": entries}
    # `</` cannot appear inside a script element, whatever its type.
    return ('<script type="application/json" id="hr-traces">'
            + json.dumps(reg).replace("</", "<\\/") + "</script>", len(entries))


def render_requirements(items, index: dict, root: Path) -> str:
    """Each requirement, with the tests that pin it nested under its own text.

    Nested rather than tabulated on purpose: the question a reviewer is asking here is
    "is *this* requirement covered, and by what", and a table elsewhere on the page makes
    them hold the requirement in their head while they go and look it up."""
    if not items:
        return ""
    lis = []
    for it in items:
        lis.append(
            '<li>'
            + f'<div class="req-text">{it.get("text", "")}</div>'
            + render_tests(resolve_tests(it.get("tests", []), index, root), root)
            + '</li>'
        )
    return '<ul class="reqlist">' + "\n".join(lis) + "</ul>"


def render_puml(block, root: Path, out_dir: Path) -> str:
    """A diagram this branch did *not* change, drawn as context rather than as a delta.

    Package structure is the case that asks for it: a reviewer wants to see the shape
    the change landed in even on the \u2014 common, and good \u2014 branches that left it alone.
    Rendered here from the committed source, so the page carries no stale SVG."""
    src = root / block["src"]
    cache, why_not = _context_svg(block["src"], root, out_dir)
    if not cache:
        return why_not
    return (
        '<div class="diagram">'
        f'<div class="head"><b>{html.escape(block.get("name", src.stem))}</b>'
        f'<span class="badge sev-info">{html.escape(block.get("status", "unchanged"))}</span>'
        + _source_link(block["src"], root) + '</div>'
        + (f'<p>{block["note"]}</p>' if block.get("note") else "")
        + _provenance(block["src"], root)
        + f'<div class="svgbox">{inline_svg(cache, root)}</div></div>'
    )


def _context_svg(rel: str, root: Path, out_dir: Path):
    """The committed `.puml` drawn as it stands, cached under assets/ by mtime.

    Returns `(svg_path, "")`, or `(None, <the paragraph to print instead>)`: a missing
    file and a missing PlantUML are the two ways there is no picture, and each is named
    rather than swallowed. Shared by the `puml` block and by a test pair whose sequence
    exists but carried no delta \u2014 the same picture, drawn the same way."""
    src = root / rel
    if not src.is_file():
        return None, f'<p class="sub">no diagram at <code>{html.escape(rel)}</code></p>'
    cache = out_dir / "assets" / (Path(rel).stem + ".context.svg")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        out = subprocess.run(["plantuml", "-tsvg", "-pipe"],
                             input=src.read_bytes(), capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return None, (f'<p class="sub">plantuml could not render '
                          f'<code>{html.escape(rel)}</code> \u2014 is it installed?</p>')
        cache.write_bytes(out.stdout)
    return cache, ""


# The guide is one of forty tabs the reviewer has open, all of them named after the
# branch. At that width the strip has room for the favicon and nothing else — so the
# mark that makes the guide findable belongs on the icon, not in front of the title,
# where it was only legible in the hover card you reach after already finding the tab.
#
# Base64 rather than percent-encoding the SVG: the payload is full of quotes and angle
# brackets, and one missed escape is a silently blank icon rather than an error.
FAVICON_EMOJI = "\U0001F471\U0001F3FB\u200D\u2642\uFE0F"
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<text y=".9em" font-size="90" font-family="Apple Color Emoji,Segoe UI Emoji,'
    'Noto Color Emoji,sans-serif">' + FAVICON_EMOJI + "</text></svg>"
)
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(FAVICON_SVG.encode()).decode()


def _app_anchor(href: str) -> str:
    """The opening tag for a link into the running app.

    A root-relative href is a *path into whatever instance is up right now*, and the port
    that instance got is not knowable when this page is built — the host picks it, so that
    several branches can be running at once. So the path is kept verbatim in `data-app` and
    the `href` is only ever a best guess, rewritten by the script once a base URL is known.
    An absolute href is left exactly as written: it names a specific server on purpose."""
    if not href.startswith("/"):
        return f'<a href="{html.escape(href)}">'
    return f'<a data-app="{html.escape(href)}" href="{html.escape(href)}">'


def runtime_html(rt) -> str:
    """The app the walkthrough was filmed against: start it, open it, stop it, reset it.

    This page is a file on disk that outlives the branch it describes, so it cannot hold a
    live URL: by the time anyone opens it the environment is long gone, and the next one
    will come up on a different port. What it *can* hold is a way to bring the environment
    back \u2014 and there are two of those, which is the whole shape of this bar.

    Served, the row is verbs, and it keeps its own state: nothing answering, so `Start`;
    something answering, so the address it answers at \u2014 a link, into a new tab, port and
    all \u2014 then `Stop` and `Reset DB`. The page asks its own server to run the commands
    the build declared and scrapes the URL out of what `Start` printed, so there is nothing
    for the reader to paste anywhere.

    Off disk none of that can happen: no process here runs a command, and Reset has
    nothing to reset until one does. So the row carries no verbs at all and the terminal
    command takes their place. Both are in the markup either way \u2014 the same file is
    opened both ways and only the script knows which \u2014 and CSS hides the half that is
    lying. One offer, in the register that copy of the report can actually honour, rather
    than the same offer twice: greyed buttons above the command that replaces them.

    Every control except the command is opt-in on something the environment actually
    provides \u2014 `stop`, `reset` \u2014 because a button that always fails is worse than no
    button, and none of these can be derived from `command` by string surgery without
    working for the one host this was written against and failing silently on the next.
    """
    if not rt:
        return ""
    cmd = rt.get("command", "")
    fallback = rt.get("base", "")

    def btn(cls: str, face: str, tip: str) -> str:
        # `hidden` from the start, and raised by the script once the probe has answered:
        # a verb drawn live that turns out not to apply has already been clicked by then.
        # aria-disabled rides along so the guard in the script survives a stylesheet that
        # never loaded, where `hidden` alone would have left a live button behind.
        return (f'<button type="button" class="{cls}" hidden aria-disabled="true"'
                f' data-tip="{html.escape(tip, quote=True)}">{face}</button>')

    # The state first, because it is the subject of everything after it: "Offline", and
    # then the one verb that changes that — or the address, and then the two verbs that
    # act on what is answering there. Exactly one of the pill and the link is ever shown.
    at = ('<span class="appenv-at">'
          '<span class="appenv-state" data-state="unknown">checking\u2026</span>'
          '<a class="appenv-url" target="_blank" rel="noopener" hidden></a></span>')

    controls = btn("appenv-start", "Start",
                   "Start the app and fill the address in from what it prints") if cmd else ""
    if rt.get("stop"):
        controls += btn("appenv-stop", "Stop", "Stop the app and free its port")
    if rt.get("reset"):
        controls += btn("appenv-reset", "Reset DB", "Put the demo data back to its seed")

    if cmd:
        declare_action("demo-env", cmd, scrape="url",
                       label="Start the environment the walkthrough was filmed against")
    if rt.get("stop"):
        declare_action("demo-env-stop", rt["stop"],
                       label="Stop the environment the walkthrough was filmed against")
    # Optional and never guessed. Turning `… up --ref abc` into `… url --ref abc` by
    # string surgery would work for the one host this was written against and fail
    # silently on the next, at probe time, where nobody would see it fail. Declared or
    # absent — and absent costs only the re-discovery of a base the browser had already
    # remembered in localStorage.
    if rt.get("urlCommand"):
        declare_action("demo-env-url", rt["urlCommand"], scrape="url",
                       label="Ask the host where the environment is already answering")
    if rt.get("drive"):
        declare_action("cue-drive", rt["drive"], params={"n": "int", "base": "url"},
                       label="Drive the app to one caption of the walkthrough")

    # The command, with no sentence introducing it. Where it shows it is the only thing
    # in the row that does anything, and "run this in a terminal to start it" in front of
    # a line that is visibly a shell command was the page reading itself out loud.
    manual = (f'<p class="appenv-manual"><code>{html.escape(cmd)}</code>'
              '<button type="button" class="appenv-copy" data-tip="Copy the command">'
              'Copy</button></p>') if cmd else ""

    return (f'<div class="appenv" data-fallback="{html.escape(fallback)}"'
            f'{f' data-reset="{html.escape(rt["reset"])}"' if rt.get("reset") else ""}'
            f'{f' data-drive="{html.escape(rt["drive"])}"' if rt.get("drive") else ""}>'
            '<div class="appenv-run"><span class="appenv-title">Deployed app</span>'
            + at + controls + '</div>' + manual + '</div>')


def _link_captions(cues, links, drive=False):
    """Put the app links *inside* the narration, on the words that already name the page.

    They used to sit in a paragraph of their own — "Pages this change touches: owner detail
    · all visits · vets" — a second list of the same screens the captions were already
    walking through, in a different order and different words. A caption that says "back on
    the owner" is the natural handle for the owner page; the separate list was a handle
    nobody needed and a thing to keep in sync.

    Returns (rendered <li> items, links that found no caption). A link is *never* dropped:
    one whose phrase is not in the narration is reported back to be printed after the
    transcript, because a page this change touches and the film did not show is a fact
    about the coverage of the film."""
    texts = [c["text"] for c in cues]
    # Each caption is escaped once, then the anchors are spliced into the escaped text —
    # so the phrase has to be escaped the same way to be found in it.
    cells = [html.escape(t) for t in texts]
    unplaced = []
    for link in links:
        phrase = html.escape(link.get("anchor") or "")
        href = link["href"]
        for i, cell in enumerate(cells):
            at = cell.find(phrase) if phrase else -1
            # Never inside an anchor already spliced in: nested <a> is invalid, and the
            # second link would be unclickable. An unbalanced count of open tags before the
            # match is exactly "we are inside one".
            if at < 0 or cell[:at].count("<a ") != cell[:at].count("</a>"):
                continue
            cells[i] = (cell[:at] + _app_anchor(href) + phrase + "</a>"
                        + cell[at + len(phrase):])
            break
        else:
            unplaced.append(link)
    # 1-based, and the same numbering the reader is looking at: the driver replays the
    # walkthrough and stops after the nth caption, so "cue 3" has to mean the third row.
    drive_btn = (lambda n: f'<button type="button" class="cue-drive" data-n="{n}" aria-disabled="true" '
                           f'data-tip="Start the app first">&#9656;</button>') \
        if drive else (lambda n: "")
    items = "".join(
        f'<li data-t="{c["t"]:.2f}"><span class="ts">{int(c["t"]) // 60}:'
        f'{int(c["t"]) % 60:02d}</span><span>{cell}{drive_btn(i)}</span></li>'
        for i, (c, cell) in enumerate(zip(cues, cells), 1)
    )
    return items, unplaced


def video_html(s, out_dir: Path) -> str:
    """The player and its transcript — or, when the recording failed, the transcript alone.

    The first thing this page ever got wrong was a `<video src="assets/….webm">` whose
    asset no step had written: a black rectangle stuck at 0:00 under a confident heading,
    with nothing on the page to say the film was missing rather than broken. So the player
    is only ever emitted for a file that is on disk. The narration is *not* held hostage to
    it: the cue list is a written account of the same walkthrough and stays on the page,
    under a notice that names the file that is absent."""
    rel = s["video"]
    cues_path = out_dir / rel.replace(".webm", ".cues.json")
    cues = json.loads(cues_path.read_text(encoding="utf-8")) if cues_path.is_file() else []
    rt = s.get("runtime") or {}
    items, unplaced = _link_captions(cues, s.get("appLinks", []), bool(rt.get("drive")))
    player = (f'<video controls preload="metadata" src="{html.escape(rel)}"></video>'
              if (out_dir / rel).is_file() else
              f'<p class="embedded-note"><b>Not filmed.</b> <code>{html.escape(rel)}</code> '
              'was not produced by this run, so there is no player here — the narration '
              'below is what the recording would have shown, and it is the only part of '
              'this section that is not evidence.</p>')
    # A screen the branch touched and the film never showed is a fact about the *coverage
    # of the film*, so it belongs to the transcript, not to the page under it. It used to
    # be a paragraph of its own below the player — a full block of vertical space, in the
    # page's own prose voice, for a footnote. As the transcript's last row it costs no
    # height at all (the cue list is a fixed-height scroller) and it is read where the
    # question it answers is actually asked: "is that everything the film covered?".
    # No `data-t`: there is no frame to seek to, which is the whole point of the row.
    if unplaced:
        items += ('<li class="uncovered"><span class="ts">--:--</span><span>'
                  '<b>Not filmed.</b> Touched by this change: '
                  + " · ".join(_app_anchor(l["href"])
                               + f'{html.escape(l.get("label") or l["href"])}</a>'
                               for l in unplaced) + ".</span></li>")
    return (runtime_html(rt)
            + f'<div class="vidwrap">{player}<ol class="transcript">{items}</ol></div>')


def embed_html(s, out_dir: Path) -> str:
    """Another tool's whole report, framed rather than re-drawn.

    `aria-label`, not `title`: a `title` on an iframe is a native tooltip, and this page has
    exactly one tooltip component. The label is the same string either way, and a screen
    reader reads it from `aria-label` just as happily."""
    e = s.get("embed")
    if not e:
        return ""
    # `src` may carry a fragment — a framed report that reads its own hash can be opened
    # on a particular view (`…#only-touched`). Only the path in front of it is a file.
    path = e["src"].split("#", 1)[0]
    if not (out_dir / path).is_file():
        # The tool that writes it is an optional install. Say which one is missing rather
        # than framing a 404.
        return (f'<p class="sub">No embedded report at <code>{html.escape(path)}</code>'
                + (f' — { e["missing"]}' if e.get("missing") else "")
                + ".</p>")
    return (f'<iframe class="{html.escape(e.get("class", "oacframe"))}" '
            f'src="{html.escape(e["src"])}" '
            f'aria-label="{html.escape(e.get("label", ""))}"></iframe>')


def resolve_refs(items, root: Path):
    """Turn `path:from-to` strings into {label, abs} so the renderer can link them.

    A reference to a file that is not there is a build failure, not a link. A snippet
    already fails loudly — `extract-snippet.py` cannot cut lines out of nothing — but a
    bare ref used to render whatever it was given, so a path that went stale (a file
    renamed on the base branch, say) reached the reviewer as a deep link that silently
    did nothing when clicked. Failing here costs one build; failing there costs the
    reviewer's trust in every other link on the page.
    """
    out = []
    missing = []
    for ref in items:
        rel, _, pos = ref.rpartition(":")
        start = pos.split("-")[0]
        target = (root / rel).resolve()
        if not target.is_file():
            missing.append(ref)
        out.append({"label": ref, "abs": f"{target}:{start}:1"})
    if missing:
        raise SystemExit(
            "[review] these references point at files that do not exist:\n  "
            + "\n  ".join(missing)
            + "\nFix the path in the content file (a base-branch rename is the usual cause)."
        )
    return out


ANCHOR = re.compile(r'<a\s+([^>]*?)href="(?P<href>[^"]*)"([^>]*)>', re.I)
TARGET_ATTR = re.compile(r'\s+target="[^"]*"', re.I)


def one_tooltip_only(doc: str) -> str:
    """Every native `title` becomes the page's own tooltip.

    Our own generators emit `data-tip` directly. PlantUML does not: it turns a
    `[[url{hint}]]` in the diagram source into `title="hint"` inside the SVG we inline,
    and that generator is not ours to change. Rewriting the assembled document is the
    one place that catches both. `<title>` *elements* are a different thing and are
    left alone — the regex only matches the attribute."""
    return re.sub(r'(<[a-zA-Z][^>]*?)\stitle="', r'\1 data-tip="', doc)


def check_baked_excerpts(doc: str) -> None:
    """Does every excerpt a fragment baked in actually hold the lines its label claims?

    A fragment that quotes code carries the lines as data — one entry per source line —
    beside a label saying which lines they are. Nothing forces the two to agree, and when
    they disagree it is invisible: `visits.spec.ts:27-43` showing sixteen lines looks
    exactly like `visits.spec.ts:27-43` showing seventeen, because the reader does not have
    the file open to count against. What they see is a test method with no closing brace —
    a method they cannot see the end of, and cannot tell whether that is the range or the
    code.

    Which is what happened: every one of a map's twenty-seven excerpts was cut one line
    short, `lines[a-1:b-1]` instead of `lines[a-1:b]`, and the page shipped that way. The
    check is arithmetic the build can do and the author cannot, so it belongs here — and
    it only warns, because a fragment is hand-authored and the build is not the place to
    refuse one."""
    for data in re.findall(r'class="rm-data">(.*?)</script>', doc, re.S):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        for test in (parsed.get("tests") or {}).values():
            for part in test.get("parts") or []:
                m = re.search(r":(\d+)(?:-(\d+))?$", part.get("label") or "")
                if not m:
                    continue
                want = int(m[2] or m[1]) - int(m[1]) + 1
                got = len(part.get("html") or [])
                if want != got:
                    print(f"[review] excerpt {part['label']} is labelled {want} lines but "
                          f"quotes {got} — the reader sees a window that stops early, with "
                          f"nothing on the page to say so", file=sys.stderr)


def open_links_in_new_tabs(doc: str) -> str:
    """Every outbound link leaves the guide in a new tab — a reviewer reading this page
    should never lose their place in it. In-page anchors keep the current tab (a new tab
    for a jump to a section is nonsense).

    `vscode://` is deliberately *not* given one. A new tab was tried, for the guide read
    inside VS Code's Simple Browser — and it made things worse: the browser opened another
    Simple Browser tab, pointed it at the `vscode://` URL and rendered a blank page, so
    every click left a dead tab behind. A webview cannot hand a custom scheme to the OS at
    all; no anchor markup changes that. EDITOR_JS handles both cases instead — navigating
    in place at top level, copying the reference where it cannot."""

    def fix(m):
        whole = m.group(0)
        href = m.group("href")
        if href.startswith("vscode:"):
            # PlantUML stamps `target="_top"` on the links it renders into an SVG, which
            # inside a webview navigates the whole frame to a scheme it cannot open and
            # leaves a blank page where the guide was. Strip any target: these links are
            # driven by EDITOR_JS, never by the browser's own navigation.
            return TARGET_ATTR.sub("", whole)
        if href.startswith("#") or "target=" in whole.lower():
            return whole
        return whole[:-1] + ' target="_blank" rel="noopener">'

    return ANCHOR.sub(fix, doc)


# The code-owners flag is the one thing on this page whose severity is *discovered* at
# build time rather than authored: whether a merge is blocked depends on the diff, not on
# what we wrote about it. So the renderer runs the check itself instead of including a
# fragment somebody remembered to regenerate — a stale "no owner touched this" is worse
# than no tab at all.
def codeowners_fragment(block, root: Path, out_dir: Path):
    dest = out_dir / block.get("out", "assets/codeowners.html")
    cmd = [sys.executable, str(CODEOWNERS), "--base", block.get("base", "origin/main"),
           "--out", str(dest), "--json"]
    if block.get("noUntracked"):
        cmd.append("--no-untracked")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "[review] codeowners-check.py failed")
    return dest.read_text(encoding="utf-8"), json.loads(proc.stdout)


# The summary walks the reader through the strip — "Eleven tabs, one question each, start
# on Review, then …". Written by hand it is a second copy of the strip, and the second copy
# is the one that rots: a tab added at the end of `tabs` leaves the sentence saying "Ten"
# and skipping the newcomer, and nothing anywhere complains. So the number is a token the
# build fills in from the tabs it actually emitted, and the names are checked against the
# same list.
TAB_COUNT_TOKEN = "{{tabcount}}"
NUMBER_WORDS = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve "
                "Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen Twenty").split()


def spelled(n: int) -> str:
    return NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)


def check_tab_enumeration(lede: str, labels: list[str]) -> None:
    """Warn when the lede's walk-through has drifted from the strip it describes.

    Not a build failure: prose is judgement, and a lede may legitimately group two tabs
    into one clause or leave a self-evident one out. But it may not do so *by accident*,
    which is what silence would make indistinguishable from a rotted sentence."""
    if not lede:
        return
    seen, missing = [], []
    for label in labels:
        # A label may carry a marker the prose has no business repeating — "🤖 Review"
        # on the pill, "Review" in the sentence. Match on the words, not the badge.
        words = label.lstrip("".join(c for c in label if not c.isalnum())).strip()
        at = lede.find(html.escape(words or label))
        (seen if at >= 0 else missing).append((at, label))
    if missing:
        print("[review] WARNING: the summary never names these tabs: "
              + ", ".join(l for _, l in missing)
              + f" — the strip has {len(labels)} of them and the summary walks "
                f"through {len(seen)}.",
              file=sys.stderr)
    out_of_order = [l for (a, l), (b, _) in zip(seen[1:], seen) if a < b]
    if out_of_order:
        print("[review] WARNING: the summary names tabs in a different order than the "
              "strip does, from: " + ", ".join(out_of_order), file=sys.stderr)


LOGEXTRACT = HERE / "logextract.py"


SRCREF_HREF = re.compile(r'(<a class="srcref" href="vscode://file/[^:"]*)(?::\d+){0,2}"')


def _aim_at_statement(snippet: str, ref: str, hits) -> str:
    """Point a quoted window's `path:line` link at the statement it is quoting.

    Everything else on this page links a snippet to its first line, which is right when the
    snippet *is* the thing. Here it is not: the snippet is four lines of context around one
    `log.warn(...)`, and landing the reader on the first of them makes them find it again by
    eye. The extractor already knows the line and the column, so the link uses them — and
    only when exactly one known statement falls inside the window, because two would make
    the choice a guess."""
    rel, _, span = ref.rpartition(":")
    lo = int(span.split("-")[0])
    hi = int(span.split("-")[-1])
    inside = [h for h in hits if h["file"] == rel and lo <= h["line"] <= hi]
    if len(inside) != 1:
        return snippet
    h = inside[0]
    return SRCREF_HREF.sub(lambda m: f'{m.group(1)}:{h["line"]}:{h["column"]}"', snippet, count=1)


def _logging_aside(part, found, what, root: Path, hits=()) -> str:
    """One of the two context registers under the added-logging finding.

    The prose and the snippets are the author's — a log line is only interesting once
    somebody says what is wrong with it — but the *count* is the extractor's, so a section
    that quotes three of four statements is caught here rather than by a reader."""
    if not part:
        return ""
    quoted = len(part.get("snippets", []))
    if found and quoted != found:
        print(f"[review] WARNING: the logging tab quotes {quoted} {what} statement(s) but "
              f"logextract found {found} — one of the two is out of date.", file=sys.stderr)
    return (
        f'<h2 id="{html.escape(part["id"])}">{html.escape(part["title"])}</h2>'
        + part.get("body", "")
        + "".join(_aim_at_statement(
            snippet_html(x["ref"], x.get("caption"), root, exact=True), x["ref"], hits)
            for x in part.get("snippets", []))
    )


# --------------------------------------------------------------------------- #
# GDPR verdict per logging statement — a real model call, not a word list.
#
# A word list over the argument names was tried first and rejected: it comes out SAFE
# for `log.info("{}", x)` when `x` was assigned three lines up from
# `owner.getName()`, because `x` looks like nothing. Knowing what a value actually
# holds means following the assignment, and no word list does that — so this asks a
# model, and gives it enough source to trace it: the statement's enclosing method
# (parameters and locals both) plus the class's field declarations, never the whole
# file and never the one line alone. `AI Evaluation` on the legend is therefore an
# accurate label, not the aspirational one a word list would have made it.
# --------------------------------------------------------------------------- #

# One mark, one sentence, for every verdict a model produced.
AI_MARK = '<sup class="ai-mark" data-tip="LLM evaluated">\U0001F916</sup>'

PRIVACY_MARK = {
    "safe": ("✅", "SAFE", "added"),
    "doubt": ("🤔", "DOUBT", ""),
    "privacy": ("❌", "PRIVACY", "removed"),
    # Not a fourth colour on the same footing as the other three: this is what a
    # guessed SAFE would have looked like if the model could not be reached and this
    # function papered over it instead of admitting so. DOUBT means the model looked
    # and said it could not tell; this means it was never successfully asked at all.
    "error": ("⚠️", "NOT EVALUATED", "warn"),
}

# No `chain` field any more. Asking the model where a value came from, and then
# rendering its answer as a list of `file:line` + the source line, was a paraphrase of
# code standing where the code could have stood. `logextract.py` walks that back
# syntactically now and the snippet quotes the real lines, so the only thing left for
# the model is the part no line of Java says out loud: whether the value is personal
# data. `trace` is that, in one clause.
# One clause per *value*, not one sentence per statement. A statement that logs three
# things and gets one fused sentence makes the reader do the un-fusing, and the thing
# they are trying to find out — which of the three carried the risk — is exactly what
# the fusing destroyed. So the model answers per value, keyed by the argument as it is
# written in the source, and the page renders one bullet per logged value.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
                    "note": {"type": "string"},
                },
                "required": ["name", "verdict", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "values"],
    "additionalProperties": False,
}

# SAFE < DOUBT < PRIVACY. The headline verdict is the worst of what the bullets say and
# what the model called the statement overall — never better than its own worst bullet,
# which is the one way a per-value answer could have made the page *less* honest than
# the single sentence it replaced.
VERDICT_RANK = {"safe": 0, "doubt": 1, "privacy": 2}


def _worst_verdict(*verdicts: str) -> str:
    real = [v for v in verdicts if v in VERDICT_RANK]
    return max(real, key=lambda v: VERDICT_RANK[v]) if real else "doubt"

VERDICT_SYSTEM_PROMPT = (
    "You are a precise static-analysis assistant embedded in a code review build script. "
    "You are given one Java logging statement plus enough of its surrounding source to "
    "trace where each interpolated value comes from. Decide whether the statement, once "
    "it executes, could write personal data (GDPR-relevant: a name, an email address, a "
    "phone number, a postal address, a government ID, free text about a person, or "
    "similar) to a log aggregator kept for months.\n\n"
    "Trace each interpolated value through the source you were given: where it is "
    "declared or assigned, and onward if that right-hand side is itself another "
    "variable. If you cannot resolve a value with what you were given, its verdict is "
    "DOUBT -- never guess SAFE past a value you could not follow.\n\n"
    "Answer per VALUE, not per statement. Return one entry in `values` for EVERY value "
    "the statement interpolates -- exactly those, no more and no fewer -- in the order "
    "they appear in the call. Set each entry's `name` to the argument EXACTLY as it is "
    "written in the source (`vetId`, `owner.getName()`), so the page can line your "
    "answer up with the call; do not rename, shorten or paraphrase it. A statement that "
    "interpolates nothing gets an empty `values` list. The top-level `verdict` is the "
    "worst of the individual ones.\n\n"
    "The page already shows the reader the statement AND the lines each value came "
    "from, quoted verbatim from the file. So a `note` must not retell any of that: no "
    "file names, no line numbers, no restating a declaration the reader is looking at, "
    "no naming the enclosing method, no repeating the value's own name (the bullet is "
    "already labelled with it), and no restating the verdict (`not personal data`, "
    "`safe`, `a privacy risk` -- the bullet already carries its own mark). Give only "
    "what the code cannot say for itself: WHAT that value actually holds, as ONE noun "
    "phrase of at most 15 words, no trailing full stop.\n"
    "Good: `{\"name\": \"vetId\", \"verdict\": \"SAFE\", "
    "\"note\": \"just a numeric vet database id\"}`.\n"
    "Good: `{\"name\": \"owner.getName()\", \"verdict\": \"PRIVACY\", "
    "\"note\": \"the owner's full name, straight into the log\"}`.\n"
    "Bad (restates the verdict): `\"note\": \"a numeric id, not personal data\"`.\n"
    "Bad: `{\"name\": \"vetId\", \"note\": \"vetId is the declared Integer "
    "parameter of resolveVet(Integer vetId) (VisitRestController.java:89), a numeric "
    "database identifier passed straight into the log call...\"}`.\n\n"
    "Respond only through the given JSON schema."
)


def _statement_context(h: dict) -> str:
    """Enough source for a model to trace every interpolated argument: the enclosing
    method (`logextract.py` resolves its range structurally, the same AST pass that
    finds the statement itself) so parameters and locals are both visible, plus the
    class's field declarations (numbered, so a field-rooted value can be placed)
    for a value that turns out to come from `this`. Never the whole file — a class with
    forty methods is forty methods of noise around the one that matters — and never just
    the statement alone, which is the version of this feature that cannot tell a
    parameter from a field from thin air, let alone trace a value past either of them."""
    ms, me = h.get("method_start"), h.get("method_end")
    try:
        src_lines = Path(h["abs_file"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        src_lines = []
    if ms and me and src_lines:
        window = src_lines[ms - 1:me]
        numbered = "\n".join(f"{n:>4}  {l}" for n, l in zip(range(ms, me + 1), window))
        method_block = f"Enclosing method ({h['file']}:{ms}-{me}):\n{numbered}"
    else:
        method_block = ("No enclosing method could be resolved. All that is available "
                        f"is the statement itself:\n{h['raw_line'].strip()}")
    fields = h.get("_fields") or []
    if fields:
        field_lines = "\n".join(f"{f['line']:>4}  {f['type']} {f['name']}" for f in fields)
        fields_block = f"Class fields in scope ({h['file']}):\n{field_lines}"
    else:
        fields_block = "Class fields in scope: none."
    return f"{method_block}\n\n{fields_block}"


def _verdict_prompt(h: dict, context: str) -> str:
    return (
        f"Logging statement ({h['file']}:{h['line']}):\n    {h['text']}\n\n"
        f"{context}\n\n"
        "Which value(s) does this statement log, where does each come from, and is any "
        "of it personal data? Give one entry per interpolated value, named exactly as "
        "the argument is written above, plus the statement's overall verdict."
    )


def _claude_bin() -> str | None:
    for cand in (os.environ.get("CLAUDE_BIN"), "claude"):
        p = shutil.which(cand) if cand else None
        if p:
            return p
    return None


def _call_privacy_model(prompt: str) -> dict:
    """One live model call. Returns `{"verdict","values","cost_usd"}` on success, or
    raises `RuntimeError` with a message written to go straight on the page — a missing
    binary, a non-zero exit, a timeout, or a response that does not match the schema.
    Never returns a guessed verdict; the caller turns any exception here into the loud
    `error` state, not a fallback answer. `values` is validated for *shape* only —
    whether it actually covers the values the statement logs is decided against
    `logextract.py`'s argument list at render time, not against the model's word."""
    claude_bin = _claude_bin()
    if not claude_bin:
        raise RuntimeError("the `claude` CLI is not on PATH (set $CLAUDE_BIN to point at it)")
    cmd = [claude_bin, "-p", prompt, "--output-format", "json",
           "--model", os.environ.get("PRIVACY_VERDICT_MODEL", "sonnet"),
           "--restricted", "--strict-mcp-config", "--no-session-persistence",
           "--max-turns", "1", "--system-prompt", VERDICT_SYSTEM_PROMPT,
           "--json-schema", json.dumps(VERDICT_SCHEMA)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the model call timed out")
    except OSError as e:
        raise RuntimeError(f"could not run `claude`: {e}")
    # The answer decides, not the exit code. `claude -p --json-schema --max-turns 1`
    # stops on the structured-output tool call and can exit non-zero while stdout holds
    # a complete, schema-conforming, already-paid-for response (`is_error: false`,
    # `subtype: "success"`). Reading the exit code first threw that answer away and put
    # the loud "the model could not be reached" on a page whose model *had* been
    # reached — the one state that is supposed to mean nobody was ever asked. So parse
    # first, and let a bad exit only colour the message when the payload is unusable
    # too. Nothing here is loosened: an unparsable body, `is_error`, or a response that
    # misses the schema still raises, and no verdict is ever guessed.
    exited = (f" (the CLI also exited {proc.returncode}"
              + (f": {proc.stderr.strip()[-200:]}" if proc.stderr.strip() else "")
              + ")") if proc.returncode != 0 else ""
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"the model call returned unparsable output{exited}")
    if payload.get("is_error"):
        raise RuntimeError(f"the model call failed: {str(payload.get('result'))[:300]}{exited}")
    out = payload.get("structured_output")
    values = out.get("values") if isinstance(out, dict) else None
    if (not isinstance(out, dict) or out.get("verdict") not in ("SAFE", "DOUBT", "PRIVACY")
            or not isinstance(values, list)
            or not all(isinstance(v, dict) and v.get("name") and v.get("note")
                       and v.get("verdict") in ("SAFE", "DOUBT", "PRIVACY")
                       for v in values)):
        raise RuntimeError("the model's response did not match the expected verdict "
                           f"schema{exited}")
    return {"verdict": out["verdict"].lower(),
            "values": [{"name": v["name"], "verdict": v["verdict"].lower(),
                        "note": v["note"]} for v in values],
            "cost_usd": payload.get("total_cost_usd") or 0.0}


def _verdict_cache_path(root: Path) -> Path:
    return root / ".human-review" / ".privacy-verdicts.json"


def _load_verdict_cache(root: Path) -> dict:
    p = _verdict_cache_path(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_verdict_cache(root: Path, cache: dict) -> None:
    p = _verdict_cache_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


#: Set by `--no-model`. A build that cannot ask is not the same as a model that could not
#: be reached, and the page says so in those words — the Logging tab already has a state
#: for "nobody was ever asked", and this is one honest way of arriving in it.
OFFLINE = False


def _offline_call(prompt: str) -> dict:
    raise RuntimeError("not evaluated: this build ran with --no-model, so the Logging "
                       "tab shows only verdicts an earlier run already paid for")


def privacy_verdict(h: dict, root: Path, cache: dict, call=None) -> dict:
    """SAFE / DOUBT / PRIVACY / error, with one clause per interpolated value — a live
    model call over the statement's enclosing method, cached by a hash of exactly what
    was sent (the statement plus its context) so a re-run on unchanged code neither
    flips the answer nor pays for it twice. `cache` is loaded once by the caller and
    mutated here; the file is rewritten on every new entry, not batched, so a run that
    dies partway through does not lose the calls it already paid for.

    The key hashes the system prompt alongside the statement and its context, so an edit
    to what the model is *asked* invalidates the cache the same way an edit to the code
    does — a shortened `trace` instruction that kept serving the old paragraph out of
    cache would be a silent no-op.

    `call` defaults to `None`, resolved to `_call_privacy_model` *inside* the body
    rather than as `def ...(call=_call_privacy_model)` — a default bound at def-time
    would freeze in the original function object, so patching the module-level name
    for a test (`monkeypatch.setattr(build, "_call_privacy_model", fake)`) would
    silently do nothing here; every caller that does not pass its own `call` needs the
    patch to actually take."""
    call = call or (_offline_call if OFFLINE else _call_privacy_model)
    context = _statement_context(h)
    key = hashlib.sha256(
        (VERDICT_SYSTEM_PROMPT + "\n" + h["text"] + "\n" + context).encode("utf-8")
    ).hexdigest()
    cached = cache.get(key)
    if cached:
        return {**cached, "cached": True, "cost_usd": 0.0}
    try:
        result = call(_verdict_prompt(h, context))
    except RuntimeError as e:
        return {"verdict": "error", "values": [], "note": str(e),
                "cached": False, "cost_usd": 0.0}
    entry = {"verdict": result["verdict"], "values": result.get("values") or []}
    cache[key] = entry
    _save_verdict_cache(root, cache)
    return {**entry, "cached": False, "cost_usd": result.get("cost_usd", 0.0)}


# How many origin lines one entry may pull in. `logextract.py` already caps the walk
# (three hops per value, six lines per statement); this is the *page's* cap on top of
# that, and it is deliberately tighter, because the failure here is not a wrong answer,
# it is a tab. This tab lists every touched Java file, and an entry that grows from
# three lines to twenty to show a chain nobody asked about has made the tab worse in
# exactly the way the prose it replaced did.
MAX_ORIGIN_LINES_SHOWN = 4


def _logging_ref(h: dict) -> str:
    """The snippet reference for one statement: its own line(s), plus the lines its
    interpolated values were traced back to — `Foo.java:89,93`.

    The origins are the extractor's (`logextract.py` walks them syntactically, from the
    ast-grep graph); all that happens here is the cap and the sort. Nearest-first, so
    when the budget runs out what survives is the hop closest to the statement — the one
    a reader would have looked at first anyway — and never a far-away line with the
    intervening ones silently dropped."""
    end = h.get("end_line") or h["line"]
    spans = [f'{h["line"]}' if end == h["line"] else f'{h["line"]}-{end}']
    origins = sorted({o["line"] for o in (h.get("origins") or [])
                      if not (h["line"] <= o["line"] <= end)},
                     key=lambda n: abs(n - h["line"]))[:MAX_ORIGIN_LINES_SHOWN]
    spans += [str(n) for n in sorted(origins)]
    return f'{h["file"]}:{",".join(spans)}'


def _value_bullets(args: list, values: list) -> tuple[list[dict], bool]:
    """One row per value the statement actually logs — driven by `logextract.py`'s
    argument list, never by whatever the model chose to mention.

    The model is asked for one clause per interpolated value; a model that quietly drops
    one must not quietly drop it from the page, so the rows come from the *code* and the
    model's clauses are matched onto them. Anything left without a clause renders as its
    own `unresolved` row and drags the headline verdict down — that is the same rule the
    tab already applies to a value the model could not follow, and for the same reason:
    "nobody said" must never read like "nothing to say".

    Matching is by the argument text (whitespace-insensitive), then by root identifier
    when that is unambiguous among the rows still unmatched — a model that answers
    `owner` for `owner.getName()` is answering the right question with a shorter name,
    but only while there is exactly one candidate it could mean.

    Returns the rows and whether any of them came out unresolved."""
    rows = [{"arg": a, "verdict": None, "note": None} for a in args]
    pool = list(enumerate(rows))

    def norm(t):
        return re.sub(r"\s+", "", t or "")

    def claim(idx, v):
        # `_call_privacy_model` already lower-cases, but `privacy_verdict` accepts any
        # `call`, and a verdict word is a key into `PRIVACY_MARK` two functions later.
        verdict = str(v.get("verdict") or "").lower()
        rows[idx]["verdict"] = verdict if verdict in VERDICT_RANK else None
        rows[idx]["note"] = v.get("note")

    unmatched = []
    for v in values:
        hit = next((i for i, r in pool if r["verdict"] is None
                    and norm(r["arg"]) == norm(v.get("name"))), None)
        if hit is None:
            unmatched.append(v)
            continue
        claim(hit, v)
    for v in unmatched:
        root = logextract_root(v.get("name"))
        cands = [i for i, r in pool
                 if r["verdict"] is None and root and logextract_root(r["arg"]) == root]
        if len(cands) == 1:
            claim(cands[0], v)

    broken = any(r["verdict"] is None for r in rows)
    return rows, broken


def logextract_root(expr: str | None) -> str | None:
    """The leading identifier of an expression, for matching a model's `owner` onto the
    page's `owner.getName()`. Deliberately the same reading `logextract.py` uses to root
    its origin walk, imported rather than re-derived so the two cannot drift apart."""
    if not expr:
        return None
    return _logextract().origin_root(expr)


@functools.lru_cache(maxsize=1)
def _logextract():
    """`logextract.py` as a module, not a subprocess — this needs one pure function out
    of it, not a scan. Registered in `sys.modules` *before* `exec_module`: its `@dataclass`
    declarations resolve their own annotations by looking their module up by name, and a
    module executed without being registered is not there to be found."""
    import importlib.util
    if "logextract" in sys.modules:
        return sys.modules["logextract"]
    spec = importlib.util.spec_from_file_location("logextract", str(LOGEXTRACT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["logextract"] = mod
    spec.loader.exec_module(mod)
    return mod


# The alternation inside `log-import`'s constraint: `(org\.slf4j|org\.apache…|ch\.qos\.logback)`.
# Two or more dotted, lower-case package roots between one pair of parentheses is the only
# group in that rule shaped like this — the `(static\s+)?` before it is neither dotted nor
# an alternation — so the rule can be rewritten freely without this having to be told.
_LOG_PKGS_RE = re.compile(r"\(([a-z][\w\\.]*(?:\|[a-z][\w\\.]*)+)\)")


@functools.lru_cache(maxsize=1)
def logging_libraries() -> tuple[str, ...]:
    """The packages the scan actually searches for, in the rule's own order.

    Read out of `logextract.py`'s own `log-import` rule rather than typed here. A list of
    library names on a page is a claim about what a scan looked for, and the only version
    of that claim worth showing is the one that cannot go stale: add a logging API to the
    rule and this list gains it on the next build; nobody has to remember the page.

    The escaping is undone (`org\\.slf4j` is a regex, not a package). Returns empty when
    the regex cannot be found at all — the caller says where to look instead, because
    naming the rule beats inventing a list."""
    rule = _logextract().RULES.get("log-import", "")
    for alt in _LOG_PKGS_RE.findall(rule):
        pkgs = [p.replace("\\", "") for p in alt.split("|")]
        if any("." in p for p in pkgs):
            return tuple(pkgs)
    return ()


def logging_libraries_tip() -> str:
    """The same packages as the hover panel's markup: one per line, in code type.

    Eight dotted package roots welded into a sentence is a list pretending to be prose —
    the reader's question is "is mine in there", and answering it meant reading a
    comma-separated run to the end. One bullet per package, monospace because these are
    identifiers and not words, and the two-line tail says the part that is genuinely
    prose: a logger reached without an import still counts."""
    pkgs = logging_libraries()
    if not pkgs:
        return "<p class=\"tipfoot\">The packages named by logextract.py's log-import rule.</p>"
    items = "".join(f"<li>{html.escape(p)}</li>" for p in pkgs)
    return (f'<ul class="tiplist">{items}</ul>'
            f'<p class="tipfoot">&hellip;plus loggers reached by type, factory or Lombok.</p>')


def _value_bullets_html(rows: list) -> str:
    """The bullets under the verdict: one per logged value, `name — what it is`.

    The name is the argument as the source writes it, in `<code>`, so a reader scanning
    a three-value statement can see which of the three carried the risk without reading
    a sentence that fused them. A row the model never answered says exactly that."""
    if not rows:
        return ""
    items = []
    for r in rows:
        name = f'<code>{html.escape(r["arg"])}</code>'
        if r["verdict"] is None:
            items.append(f'<li class="val-unresolved">{name} — no clause came back for '
                         f'this value; it was not assessed</li>')
            continue
        emoji = PRIVACY_MARK[r["verdict"]][0]
        # The per-value mark is shown only when it differs from "fine": a column of green
        # ticks under a green tick is decoration, and the row a reader must not miss is
        # the one that is not green.
        mark = "" if r["verdict"] == "safe" else f'{emoji} '
        items.append(f'<li>{mark}{name} — {html.escape(r["note"])}</li>')
    return f'<ul class="log-values">{"".join(items)}</ul>'


def _logging_listing(added: list, root: Path, fields_by_file: dict | None = None,
                      call=None, cache_root: Path | None = None) -> str:
    """The leading answer: one code snippet per logging statement this change set
    actually added or modified — the same `.snippet` figure every other quoted line on
    this page uses (`extract-snippet.py`), not a second, invented code-block style.
    Files with nothing to say do not appear here at all.

    An empty list is not silence. `logextract.py` ran and genuinely found zero — see
    `logging_fragment`'s docstring for why that is itself the finding — so it renders as
    a sentence carrying the same weight as the snippets it replaces, never as a blank
    stretch of page that would read exactly like the scan never having run at all.

    `cache_root` defaults to `root` — they are the same directory in production (both
    the reviewed repo's checkout) — and exists as its own parameter only so a test can
    point the verdict cache at a throwaway `tmp_path` while still handing `snippet_html`
    the real repo it needs to resolve a fixture file against."""
    if not added:
        return ('<p class="lede"><b>None.</b> Not one logging statement was added or '
                'changed on the lines this change set touches.</p>')
    cache_root = cache_root or root
    fields_by_file = fields_by_file or {}
    cache = _load_verdict_cache(cache_root)
    boxes = []
    # `new code` / `2 lines changed` — dropped on this tab only. Everywhere else the badge
    # answers "is this quoted block new, or an old one with a line in it?", which is a real
    # question about a snippet a reviewer did not choose. Here it is not: the gutter beside
    # the statement already marks the added lines with `+`, and every block on this tab is
    # here *because* the branch added or rewrote that logging line. A badge repeating the
    # tab's own entry condition on every box is a word the eye has to skip. The rest of the
    # bar stays: the file it came from, and the two handles that open the change.
    BADGE_RE = re.compile(r'<span class="code-badge"[^>]*>[^<]*</span>')
    for h in added:
        ref = _logging_ref(h)
        h = {**h, "_fields": fields_by_file.get(h["file"])}
        result = privacy_verdict(h, cache_root, cache, call=call)
        # The rows come from the code (`logextract.py`'s argument list), the clauses from
        # the model, and the headline verdict from the worst of everything below it — a
        # value the model skipped counts as unassessed, not as fine.
        rows, unassessed = _value_bullets(h.get("args") or [], result.get("values") or [])
        verdict_key = result["verdict"]
        if verdict_key != "error":
            verdict_key = _worst_verdict(verdict_key,
                                         *[r["verdict"] for r in rows if r["verdict"]],
                                         *(["doubt"] if unassessed else []))
        emoji, word, css = PRIVACY_MARK[verdict_key]
        verdict_class = f"privacy-verdict {css}".strip()
        # This box used to take the snippet apart — anchor stripped off the top, a
        # hand-rebuilt copy of it glued into the footer as a bottom-right marker — from
        # back when the header was a bare path on a line of its own and the footer had
        # room to spare. It is a shared component now, carrying the file *and* the two
        # handles that open the change, and a tab that quietly rebuilds a component is a
        # tab that stops getting its fixes. So the bar stays where every other tab has it,
        # at the top, and the footer keeps only what is this tab's own: the verdict.
        # The bar's own link opens at the *first* line of the window, which with origin
        # lines pulled in is the origin rather than the statement. Re-aimed at the hit's
        # own line and column: that is where a reader clicking a logging box expects to
        # land, and it is the one thing the generic bar cannot work out for itself.
        snippet = snippet_html(ref, None, root, exact=True,
                               link_at=(h["line"], h.get("column", 1)))
        snippet = BADGE_RE.sub("", snippet, count=1)
        # The verdict, and nothing else. What used to ride on this line was one run-on
        # sentence about the whole statement; it is a bullet per logged value below,
        # because "which of the three values is the problem" is the question a reader
        # brings here and a fused sentence is precisely what destroys the answer. The
        # location left this row too, upwards, into the source bar every tab shares.
        footer = (
            f'<p class="log-footer">'
            f'<span class="{verdict_class}">{emoji} <b>{word}</b></span>'
            # Outside the span, deliberately: the verdict word ends at the word. This is a
            # note about *how the verdict was reached*, and only where one actually was --
            # NOT EVALUATED means the model was never successfully asked.
            + (AI_MARK if verdict_key != "error" else "")
            + f'</p>'
        )
        # The model-failure state has no per-value answers to show — it never got any —
        # so its one message rides under the verdict in the same place the bullets would.
        note = result.get("note")
        body = (f'<p class="log-note">{html.escape(note)}</p>' if note
                else _value_bullets_html(rows))
        snippet = snippet.replace("</figure>", f"{footer}{body}</figure>", 1)
        boxes.append(snippet)
    # "The page marks tabs by what produced them" — this legend is the disclosure for a
    # tab whose verdicts are a model's reading, not a program's. What it no longer carries
    # is the machinery behind that reading: which half of the box is a live call, how many
    # calls it took, and what they cost. That was a paragraph about the build, on a tab a
    # reviewer opened to read about *their diff*, and the 🤖 on every verdict already says
    # the only part of it they can act on — that a model, not a program, decided this one.
    legend = (
        '<div class="privacy-legend"><p class="privacy-legend-title">🤖 AI Evaluation:'
        f'</p><p class="privacy-legend-note">The code block is the evidence: alongside '
        f'each statement it quotes the lines its logged values came from, walked back '
        f'structurally by <code>ast-grep</code> and cut from the working tree with their '
        f'real line numbers.</p>'
        '<ul class="privacy-legend-list">'
        '<li>✅ <b>SAFE</b> — nothing traced reads as personal data</li>'
        '<li>🤔 <b>DOUBT</b> — could not trace it with confidence, and an unresolved '
        'case is read as DOUBT on purpose rather than guessed SAFE</li>'
        '<li>❌ <b>PRIVACY</b> — a value traced back to personal data, on its way to a '
        'log aggregator kept for months</li>'
        '<li>⚠️ <b>NOT EVALUATED</b> — the model could not be reached; never silently '
        'read as SAFE</li>'
        '</ul></div>'
    )
    return "".join(boxes) + legend


def logging_fragment(block, root: Path):
    """What this change set will say for itself at 3 a.m., found structurally.

    Grep cannot answer this question. `log.info(...)` is a hit and `Math.log(x)` is not, and
    only the syntax tree plus a symbol table of what is actually a logger can tell them
    apart — which is what `logextract.py` does, and why it is a script and not a regex.

    The zero case is the point, not an edge case: a change set that logs nothing is not an
    empty section — it is a finding, said as a plain sentence rather than shown as an
    absence a reader could mistake for the scan not having run. There is no table of every
    touched file behind this any more — a table where almost every row read `0 logging`
    was exactly the noise a reviewer had to read past to find the one or two lines that
    were the actual answer, on every change set, not just the pathological ones — so a
    reviewer who wants proof the scan ran gets that from the tab actually rendering
    (weight 1, a real sentence) rather than from an inventory of the files it walked. A
    dropped tab (ast-grep missing, or the scan failing outright) is the other thing this
    must never be confused with — that path returns `("", 0, 0)` below and the tab
    disappears with a loud line in the build log, which is a different, visible failure
    mode from a real, rendered zero."""
    paths = block.get("paths") or ["."]
    base = subprocess.run(["git", "merge-base", block.get("base", "origin/main"), "HEAD"],
                          cwd=root, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "logging.json"
        proc = subprocess.run(
            [sys.executable, str(LOGEXTRACT), *paths, "--root", str(root), "--repo", str(root),
             "--since", base, "--json", str(report)],
            cwd=root, capture_output=True, text=True)
        if proc.returncode != 0 or not report.is_file():
            # ast-grep is a binary, not a Python dependency, so a machine without it is a
            # real case. Say which tool is missing rather than quietly reporting "no
            # logging" — a false all-clear is the one answer this tab must never give.
            print("[review] logextract.py failed — dropping the logging tab:\n"
                  + proc.stderr.strip()[-500:], file=sys.stderr)
            return "", 0, 0
        payload = json.loads(report.read_text(encoding="utf-8"))

    added = payload.get("changed", payload["all"])["logging"]
    # No heading: the tab is called Logging and the panel opens with it — a `<h2>Logging
    # added/updated` under a selected `Logging` pill is the tab's own label, said twice.
    # The anchor it used to carry rides on the lede instead, so `#logging-added` still
    # lands where it always did.
    #
    # No authored lede either. What stood here was three sentences of methodology (grep
    # vs. ast-grep, `Math.log(x)`, walking the syntax tree) that a reader can see for
    # themselves in the blocks below: every one of them quotes the lines it traced. What
    # they cannot see is *which* libraries were looked for — so that is the one fact left
    # standing, in a line, with the list itself one hover away rather than spent on the
    # page. It is computed, never typed: `logging_libraries` reads the packages back out
    # of the very rule `logextract.py` runs, so the hover cannot claim a library the scan
    # does not actually search for.
    head = (f'<p class="lede" id="{html.escape(block.get("id", "logging-added"))}">'
            f'Found structurally searching for '
            f'<span class="dfn" data-tip-side="right"'
            f' data-tip-html="{html.escape(logging_libraries_tip(), quote=True)}">'
            f'common logging libraries</span>.</p>')
    body = ""
    # No header bar and no surrounding card any more: no heading repeating "logging", no
    # count pill, no `path, base…HEAD` provenance line — the tab's own title already says
    # "logging", and the snippets below say what they are without a caption restating it.
    # The snippets and the legend sit directly on the page, exactly like every other
    # block's content on this tab. `_logging_listing` alone decides what shows: the real
    # snippets, or the explicit "None." sentence for a genuine zero.
    listing = _logging_listing(added, root, payload.get("stats", {}).get("fields", {}))
    frag = (
        head + body + listing
        # "What does this service log today" is the question a reader asks in the same
        # breath as "what did this branch add", and `System.out` is a third answer that must
        # not be counted as a fourth logger. Both are context, both sit under the finding.
        + _logging_aside(block.get("existing"), len(payload["all"]["logging"]),
                         "pre-existing logging", root, payload["all"]["logging"])
        + _logging_aside(block.get("console"), len(payload["all"]["antipattern"]),
                         "console-output", root, payload["all"]["antipattern"])
    )
    # Weight is 1 whenever the scan actually ran — never tied to the header bar or the
    # card that used to wrap the snippets, both gone now, and never computed from `n`
    # either. The zero is not "we looked at unrelated context and nothing moved" — the
    # tab that gets struck through — it is a statement *about this diff*: twelve touched
    # Java files, five hundred added lines, and not one of them will say anything at 3
    # a.m. Striking that through would file the finding as a non-event, and dropping the
    # tab (weight 0) would be worse: that reading is reserved for the one case that is
    # not a real answer — `ast-grep` missing or the scan crashing outright, handled above
    # by returning `("", 0, 0)` before any of this runs.
    assert listing, "logging_fragment must always have content: a real listing or the zero sentence"
    return frag, 1, 1


REQUIRED = {
    "sections": ("id", "title"),
    "tabs": ("id", "label"),
    "findings": ("title", "body"),
    "assumptions": ("title", "body"),
    "autofixes": ("title",),
}


def cost_chip(root: Path) -> dict | None:
    """What this review run consumed, asked of the run itself.

    Returns None — dropping the chip rather than showing a wrong one — whenever the answer
    cannot be trusted: no session id in the environment (the page was built outside a
    Claude Code session), or no transcript for it.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file():
        return None
    proc = subprocess.run([sys.executable, str(script), "--chip"],
                          cwd=root, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost chip: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def tests_chip(doc: dict | None) -> dict | None:
    """`{"auto":"tests"}` — what the branch did to the test run, counted off the test
    code itself by `test-changes.py`.

    It replaces a chip that used to be typed by hand (`unit tests · 125 green (20 new)`),
    which could only ever be true for as long as nobody wrote another test. This one
    states the number a reviewer acts on, and states it as a balance: how many tests
    entered the run, how many left it. Both halves matter, and the second is the reason
    the chip exists — a branch that adds nine tests and quietly `@Disabled`s three has
    not added nine.

    The loss is deliberately one number over three causes. Deleting a test, commenting it
    out and disabling it cost the run the same test, and only deletion is visible to
    someone skimming a diff; splitting them on the chip's face would invite reading the
    smallest one as the answer. The split is in the tooltip, where it belongs.

    Returns None when there is no manifest — dropping the chip rather than printing a
    zero, which would read as "this branch touched no tests" when the truth is "nobody
    counted".
    """
    t = (doc or {}).get("totals")
    if not t:
        return None
    balance = " / ".join(
        piece for piece in (
            f'<span class="added">+{t["gained"]}</span>' if t["gained"] else "",
            f'<span class="removed">\u2212{t["lost"]}</span>' if t["lost"] else "",
        ) if piece
    )
    # `PENCIL` for the edited ones, beside `+` and `−`; see the constant for why it is
    # not `±` any more. It retired a `~` before that, for the same reason: an
    # approximation standing in for a number that was never approximate.
    edited = f'<span class="changed">{PENCIL}{t["modified"]}</span>' if t["modified"] else ""
    value = " / ".join(x for x in (balance, edited) if x) or "none touched"

    # A hover is read standing up, one glance, hand on the mouse. It gets the numbers the
    # face could not fit and stops — the reasoning behind them is in this docstring, where
    # whoever needs it is already reading. Three clauses at the outside.
    gone = [f'{t["deleted"] - t["commented"]} deleted' if t["deleted"] - t["commented"] else "",
            f'{t["commented"]} commented out' if t["commented"] else "",
            f'{t["disabled"]} disabled' if t["disabled"] else ""]
    gone = ", ".join(x for x in gone if x) or "none lost"
    # A new test that arrives `@Disabled` is written but never ran, so it is in `added`
    # and not in `gained`. Without this the two numbers look like a bug — "22 new" over a
    # chip reading `+21` — when they are in fact the finding.
    inert = t["added"] - (t["gained"] - t["reenabled"])
    tip = (f'{t["added"]} new'
           + (f' ({inert} disabled on arrival)' if inert else "")
           + f', {t["modified"]} edited, {gone}'
           # The one clause that has to survive the cut: it is why `+10` can stand over
           # `9 new`, and without it the face looks like it cannot add up.
           + (f', {t["reenabled"]} back on' if t["reenabled"] else ""))
    return {"label": "tests", "value": value, "tip": tip}


# What a diffstat must never count. Every path below is written by a generator -- a
# sequence diagram redrawn from a trace, an API client regenerated from a spec, a lock
# file resolved by a package manager -- and none of it is code a reviewer reads.
#
# Counting them does not merely inflate the number, it inverts it. On the branch this was
# written for, one regenerated `endpoint-complexity.json` supplied 1405 of 2896 added
# lines, and the redrawn `.genseq.*` pairs supplied almost every deletion: a reviewer
# reading `-333 lines` was reading a diagram being redrawn, not a line of logic being
# removed. The chip is there to say how much there is to read, and a number dominated by
# machine output answers a different question than the one being asked.
#
# `exclude` in the content file adds to this list; it never replaces it. There is no way
# to switch the default off, because "count the generated files too" is not a reviewing
# preference -- it is the mistake this exists to prevent. The tooltip states the
# unfiltered totals anyway, so nothing is hidden, only ranked.
GENERATED_PATHSPECS = [
    "*/generated/*", "generated/*",
    "*.genseq.json", "*.genseq.puml",
    "*.min.js", "*.min.css", "*.snap",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "package-lock.json", "*/package-lock.json",
    "yarn.lock", "*/yarn.lock",
    "pnpm-lock.yaml", "*/pnpm-lock.yaml",
    "go.sum", "*/go.sum",
    "Cargo.lock", "*/Cargo.lock",
    "poetry.lock", "*/poetry.lock",
    ".human-review/*",
]


def _git(root: Path, *args: str) -> str | None:
    """A git command whose failure is an answer, not an exception.

    Every caller here is asking a question that can legitimately have no answer -- a ref
    that does not exist locally, a range that cannot be walked -- and each of them turns
    `None` into a dropped chip or a dropped warning rather than a wrong one.
    """
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def _resolve_base(root: Path, named: str) -> tuple[str, str] | None:
    """Which ref the page should actually measure against, given the name it was told.

    A content file says `"base": "main"`, and on the machine the review is built on that
    is a *local* branch which may be days behind the remote it names. Comparing against it
    charges the branch under review with every commit the local ref has not pulled yet:
    on the branch this was written for, local `main` was 18 commits behind `origin/main`,
    and diffing against it reported 142 files and 4099 deleted lines for a change set that
    deletes 39. So a bare name resolves to `origin/<name>` when that exists -- the ref a
    pull request would actually merge into -- and only falls back to the local branch when
    there is no remote-tracking ref to prefer. A name that already carries a remote
    (`origin/main`, `upstream/main`) is taken at its word.

    Returns `(ref, sha)`, or None when nothing by that name resolves at all. Deliberately
    never falls back to HEAD: a base that will not resolve must not silently become the
    thing it is supposed to be compared against, or the page reports a change set of zero
    and calls it a clean review.
    """
    candidates = [named] if "/" in named else [f"origin/{named}", named]
    for ref in candidates:
        sha = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return ref, sha
    return None


def base_state(root: Path, named: str) -> dict | None:
    """Where the base sits relative to the branch -- the two ways the comparison goes stale.

    A review page is a claim about a *pair* of refs, and it keeps being rendered long after
    one of them has moved. Two distinct things can be wrong, and they need saying
    differently because the fix differs:

    `ahead` -- commits on the base that are not on the branch. The branch forked from
    behind and has stayed there, so every diagram, count and finding on the page describes
    a merge that has not been rehearsed against what main actually contains now. Merging or
    rebasing makes it zero, which is exactly why the warning disappears on its own: there
    is no flag to clear and nothing to remember.

    `localBehind` -- the *local* branch named as the base is behind its own remote. Nothing
    is wrong with the branch under review here; what is stale is the yardstick. This one is
    quieter and nastier than the first: the page looks current, the numbers look measured,
    and they are measured against a main from last week.

    Returns None when the base does not resolve, which drops the marker rather than
    inventing a reassuring absence of one.
    """
    resolved = _resolve_base(root, named)
    if not resolved:
        return None
    ref, sha = resolved
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not head:
        return None
    merge_base = _git(root, "merge-base", sha, head)

    def count(rng: str) -> int | None:
        n = _git(root, "rev-list", "--count", rng)
        return int(n) if n and n.isdigit() else None

    state = {
        "named": named,
        "ref": ref,
        "sha": sha,
        "head": head,
        "mergeBase": merge_base,
        # Commits the base has that the branch does not. Not `merge_base != sha`: the
        # count is the number a reader acts on ("nine commits behind"), and the boolean
        # falls out of it.
        "ahead": count(f"{head}..{sha}"),
        "localRef": None,
        "localBehind": None,
    }
    # Only meaningful when a *local* branch of that name exists beside the remote one we
    # preferred. `origin/main` given verbatim in the content file has no local twin to be
    # behind, and neither does a repository with no remote at all.
    if ref != named and _git(root, "rev-parse", "--verify", "--quiet", f"{named}^{{commit}}"):
        state["localRef"] = named
        state["localBehind"] = count(f"{named}..{ref}")
    return state


def base_warning(state: dict | None) -> str | None:
    """The sentence behind the `!` on the base chip, or None when the pair is current.

    Both conditions are reported in one tooltip when both hold, because they compound: a
    branch forked from behind a base that is *itself* behind its remote is two hops from
    the merge it claims to describe, and a reader told only about one of them will fix
    that one and trust the rest.
    """
    if not state:
        return None
    # Short, like every other hover on the scope bar. Each clause names the gap and the
    # one command that closes it -- which is all a reader standing over the page can act
    # on. Why it matters (nothing here was measured against those commits; the page looks
    # current while its yardstick is a week old) is in this function's docstring, for
    # whoever is fixing the build rather than reading it.
    parts = []
    ahead = state.get("ahead")
    if ahead:
        parts.append(f"{state['ref']} is {ahead} commit{'s' if ahead != 1 else ''} ahead of "
                     "the fork point. Merge or rebase, then rebuild.")
    behind = state.get("localBehind")
    if behind:
        # Not `git fetch`: the count above was read off the remote-tracking ref, so the
        # fetch has already happened, and it never moves the local branch anyway. What
        # closes this gap is fast-forwarding the local branch onto what was fetched.
        parts.append(f"Compared against {state['ref']} ({state['sha'][:8]}); local "
                     f"{state['localRef']} is {behind} behind it. "
                     f"git branch -f {state['localRef']} {state['ref']}.")
    return " ".join(parts) or None


def _numstat(root: Path, rng: str, pathspecs: list[str]) -> tuple[int, int, int, int, int]:
    """`(files_added, files_edited, files_deleted, lines_added, lines_removed)` for a range.

    Binary files report `-` for both line counts; they are counted as files touched and
    contribute no lines, which is the only honest reading -- "a PNG changed by 14142 bytes"
    is not a number that belongs beside a count of lines a human reads.
    """
    args = ["diff", "--numstat", rng, "--", ".", *pathspecs]
    numstat = _git(root, *args) or ""
    status = _git(root, "diff", "--name-status", rng, "--", ".", *pathspecs) or ""
    adds = dels = 0
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        a, d = cols[0], cols[1]
        adds += int(a) if a.isdigit() else 0
        dels += int(d) if d.isdigit() else 0
    added = edited = deleted = 0
    for line in status.splitlines():
        code = line.split("\t", 1)[0][:1]
        if code == "A":
            added += 1
        elif code == "D":
            deleted += 1
        elif code:
            # R (renamed) and C (copied) land here with M. A rename is a file edited from
            # the reviewer's side of the desk, not one added and one removed.
            edited += 1
    return added, edited, deleted, adds, dels


def _compare_href(pr: dict | None) -> str:
    """`<repo>/compare/<base>...<branch>` — the diff the diffstat is a count of.

    Built from the two refs the page already names rather than from the shas it
    measured: a sha pair is only a URL once the branch has been pushed, and the number
    in the chip is read off a working tree that may be a commit ahead of the remote. Two
    branch names are the comparison github.com keeps current by itself — the same pair
    the ref chips beside it link to, one page further in.

    `origin/` is stripped: it names a remote in *this* checkout, and github.com has
    never heard of it. Empty when anything is missing, and the chip stays an inert pill
    rather than linking somewhere that 404s."""
    pr = pr or {}
    repo = (pr.get("repo") or "").rstrip("/")
    base = (pr.get("base") or "").removeprefix("origin/")
    branch = pr.get("branch") or ""
    if not (repo and base and branch):
        return ""
    return (f"{repo}/compare/{urllib.parse.quote(base)}..."
            f"{urllib.parse.quote(branch)}")


def diffstat_chips(root: Path, state: dict | None, extra: list[str] | None,
                   pr: dict | None = None) -> list[dict]:
    """`{"auto": "diffstat"}` -- how much there is to read, measured rather than typed.

    The fourth chip to be taken away from the author, and the one with the clearest reason
    to be. `files` and `lines` outlived the `autofixed`, `cost` and `tests` chips being
    computed because they *look* like facts: a number with a sign in front of it reads as
    something a tool produced. On the branch this was written for, the page had said
    `files +1 / ~40` and `lines +1198 / -863` for six days. The file count was roughly
    right. The line counts matched no range in the repository at all -- not the branch
    against its base (+2896 / -333), not against the merge-base recorded in the same
    content file (+5089 / -4378), not against the stale local main (+4347 / -4099). They
    had been typed once, from a branch state three commits and one `git reset` ago, and
    nothing was ever going to catch them, because nothing was looking.

    Two chips out of one measurement, so the file count and the line count can never
    describe different ranges -- which is its own class of drift, and the one a reader is
    least equipped to notice.

    Returns [] when the base will not resolve: no base, no comparison, no chip. A page that
    cannot say what it measured against must not print a number as though it could.
    """
    if not state or not state.get("mergeBase"):
        return []
    # `A...B` and `A..B` differ only when the base has moved ahead, and that is precisely
    # the case the `!` on the ref chip is about. Three dots is the pull request's own
    # reading -- what this branch did, not what has happened since it forked -- so the two
    # marks stay independent: the numbers describe the branch, the warning describes the
    # gap.
    rng = f"{state['mergeBase']}...{state['head']}"
    excludes = [f":(exclude){p}" for p in GENERATED_PATHSPECS + list(extra or [])]
    a, e, d, adds, dels = _numstat(root, rng, excludes)
    fa, fe, fd, fadds, fdels = _numstat(root, rng, [])
    hidden = (fa + fe + fd) - (a + e + d)

    where = f"vs {state['ref']}"
    # The signs are the page's, not this chip's: `+` added, `-` removed, a pencil for
    # changed, and a zero is dropped rather than printed. A row of chips is read as a row
    # of signed numbers, and `-0` is noise that costs a glance to dismiss.
    files_value = " / ".join(piece for piece in (
        f'<span class="added">+{a}</span>' if a else "",
        f'<span class="removed">−{d}</span>' if d else "",
        f'<span class="changed">{PENCIL}{e}</span>' if e else "",
    ) if piece) or "none"
    lines_value = " / ".join(piece for piece in (
        f'<span class="added">+{adds}</span>' if adds else "",
        f'<span class="removed">−{dels}</span>' if dels else "",
    ) if piece) or "none"

    # The unfiltered totals stay in the hover; the prose explaining them does not. A
    # filtered number with no way to see what was filtered leaves the reader taking the
    # exclusion on trust, which is the position the typed chip left them in -- so the
    # guarantee is that the generated files are *ranked below* the code, never hidden from
    # it. What went is the argument for that: a redrawn diagram is not a line written, and
    # everything here was measured with `git diff` at build time rather than typed. Both
    # true, neither actionable, and a tooltip is read standing up in one glance. Whoever
    # needs the reasoning is reading this function.
    if hidden:
        skipped = (f" {hidden} generated left out; with them {fa + fe + fd} files, "
                   f"+{fadds} / −{fdels}.")
    else:
        skipped = " No generated files to leave out."

    # The line count is the one number on the bar a reader wants to *open*: "+921 / −68"
    # is the size of what there is to read, and the next question is always what those
    # lines are. So it carries the compare page, and the file count beside it stays an
    # inert pill -- two identical-looking links to the same page is a row that teaches the
    # reader to ignore half of it.
    href = _compare_href(pr)
    lines_tip = f"+{adds} / −{dels} {where}.{skipped}"
    if href:
        lines_tip += " Opens the whole diff on github.com."
    return [
        {"label": "files",
         "value": files_value,
         "tip": f"{a} added, {e} edited, {d} deleted {where}.{skipped}"},
        {"label": "lines",
         "value": lines_value,
         "tip": lines_tip,
         **({"href": href} if href else {})},
    ]


def tab_cost_report(root: Path, tab_ids: list[str]) -> dict | None:
    """What each tab cost, asked of the run itself — same discipline as `cost_chip`.

    Unlike `cost_chip`, this does not go quiet on a bad day: no session, no transcript, no
    step ledger, a step that never stamped — every one of those comes back as *data*
    (`report["tabs"][id]["tip"]` says so in words), because a tab whose cost silently has
    no tooltip reads exactly like a tab that measured zero. Only returns None when
    `review-cost.py` itself could not be asked at all.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file() or not tab_ids:
        return None
    proc = subprocess.run(
        [sys.executable, str(script), "--tab-costs", "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no per-tab cost report: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def cost_ledger_report(root: Path, tab_ids: list[str], base: str) -> dict | None:
    """The whole bill — writing the code, the passes, every tab, the residual.

    Same discipline as `tab_cost_report`, which it supersedes: every failure comes back as
    data with a sentence explaining it, never as a silently missing number. It returns None
    only when `review-cost.py` could not be asked at all.
    """
    script = Path(__file__).resolve().parent / "review-cost.py"
    if not script.is_file():
        return None
    proc = subprocess.run(
        [sys.executable, str(script), "--ledger", "--base", base,
         "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost ledger: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


# The unattributed cost, in the order a reader wants it: the one part that has a real name
# first, then the two that are honestly leftovers. Keys come from `review-cost.py`'s
# `tab_costs`; a part with no turns in it is not rendered at all.
RESIDUAL_ROWS = [
    ("guide", "assembling the guide itself — Step 9 writes every tab&rsquo;s prose in one pass"),
    ("subagent", "subagent work that fell outside every step&rsquo;s window"),
    ("conversation", "the orchestrating conversation — reading, deciding, recovering"),
]


def _cost_money(c: float) -> str:
    """`review-cost.py`'s own `money()`, with one difference that matters in a table: a
    measured zero prints as `$0.00`, not as `<$0.01`. In a tooltip the two read the same;
    in a column of numbers, "less than a cent" claims a script-generated tab spent
    something, which is the one thing the zero rows are there to deny."""
    if c <= 0:
        return "$0.00"
    return f"${c:,.2f}" if c >= 0.01 else "<$0.01"


def _cost_tokens(n: float) -> str:
    n = int(round(n))
    # A conversation that wrote a feature over two days runs to ten figures, and `1044.6M`
    # is four digits the reader has to convert before the column means anything.
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)



COST_TAB_ID = "cost"

# The groups the ledger reports, in the order the money was spent: somebody wrote it,
# somebody reviewed it, and then this page was assembled. Each is a caption row, not a
# separate table — the reader is comparing magnitudes across all three, and three tables
# means three column widths and no comparison.
PASS_ROWS = [
    ("finding", "the passes that read the diff"),
    ("fixing", "the passes that applied what they found"),
]


def _when(raw: str | None) -> str:
    """`2026-09-02T15:41:21.4Z` as `2 Sep 15:41`. The date is there because the writing
    happened on a different day from the review and that is half the point of the row;
    the seconds are not, because nothing here is timed to the second."""
    if not raw:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{t.day} {t.strftime('%b')} {t:%H:%M}"


def cost_ledger_html(led: dict | None, tabs: list[dict]) -> str:
    """What this change set cost, from the first line written to this page being built.

    This was a chip in the scope bar with a breakdown hanging off it, and the chip
    answered the wrong question: *what did this page cost to make*. The question a reader
    arrives with is what the **change** cost, and producing the code is the larger half of
    it — on the branch this was built for, the conversation that wrote the feature cost
    nearly seven times the review that read it. A number that big is not a footnote on a
    bar of chips; it is its own tab, and it is the honest answer to "is this way of working
    worth it", which is the only reason anybody totals up an agent's bill at all.

    Every row is measured or says it is not. Three kinds of honesty the table has to keep:

      * **A window is not a fence.** The authoring row is costed between that conversation's
        first and last edit to these files. Work inside that window which belonged to
        something else is counted, and the row prints the window so the reader can see how
        wide it is rather than trusting a number that cannot be tightened.
      * **The passes are added once.** A review pass usually runs before the guide does, so
        its cost is outside the run's own total and is added; one fired mid-run is already
        inside it and is not. The ledger tells the two apart rather than assuming.
      * **A zero is not an absence.** A tab a script produced costs nothing to produce and
        says so in its own row; a tab nothing could measure says *that*, in words.
    """
    if not led:
        return ""
    # Nothing measured anywhere — no transcript for the run, and no conversation on disk
    # that wrote the code — is an absence, and the page carries no tab for it. A pill
    # reading `$0` is a claim that this change was free, which is the one thing the
    # absence does not mean. A run that measured EITHER half still gets the tab, with the
    # other half saying in words why it is missing.
    if not ((led.get("writing") or {}).get("measured")
            or (led.get("run") or {}).get("measured")):
        return ""
    rows = []

    def row(label: str, tokens, cost, cls: str = "") -> None:
        tok = _cost_tokens(tokens) if tokens is not None else "—"
        money = _cost_money(cost) if cost is not None else "—"
        rows.append(f'<tr{f' class="{cls}"' if cls else ""}><td>{label}</td>'
                    f'<td>{tok}</td><td>{money}</td></tr>')

    def group(title: str) -> None:
        rows.append(f'<tr class="costgroup"><td colspan="3">{title}</td></tr>')

    # --- writing it ---------------------------------------------------------
    writing = led.get("writing") or {}
    group("writing the code")
    if writing.get("measured"):
        for sess in writing.get("sessions") or []:
            where = " &middot; ".join(x for x in (
                f'{sess["edits"]} edits across {sess["files"]} files' if sess.get("edits")
                else f'{sess["bash"]} shell writes across {sess["files"]} files',
                f'{_when(sess.get("first"))} &rarr; {_when(sess.get("last"))}',
                # Escaped, and `<synthetic>` dropped: it is what `review-cost.py` calls a
                # turn with no model on it, it costs nothing, and unescaped it was a tag
                # the browser swallowed along with the comma in front of it.
                ", ".join(html.escape(m) for m in (sess.get("models") or [])
                          if m and m != "<synthetic>"),
            ) if x)
            name = "this conversation" if sess.get("current") else \
                f'conversation <code>{html.escape(sess["session"][:8])}</code>'
            rows.append(
                f'<tr><td>{name}<span class="costsub">{where}</span></td>'
                f'<td>{_cost_tokens(sess.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(sess.get("cost") or 0.0)}</td></tr>')
        if writing.get("weak"):
            row('<span class="costnote">no conversation used the edit tools on these '
                "files — this is the strongest shell-only match, and may be the wrong "
                "one</span>", None, None, "costquiet")
    else:
        why = writing.get("reason") or "not measured"
        row(f'<span class="costnote">{html.escape(str(why))}</span>', None, None, "costquiet")

    # --- reviewing it -------------------------------------------------------
    passes = led.get("passes") or {}
    groups = passes.get("groups") or {}
    if groups or passes.get("inline"):
        group("reviewing it")
    for key, title in PASS_ROWS:
        g = groups.get(key)
        if not g:
            continue
        invoked = ", ".join(f"<code>{html.escape(i)}</code>" for i in g.get("invoked") or [])
        inside = (g.get("cost") or 0.0) - (g.get("earlier") or 0.0)
        note = (" &middot; already inside the run below, so not added twice"
                if inside > 0.005 else "")
        rows.append(
            f'<tr><td>{title}<span class="costsub">{invoked}{note}</span></td>'
            f'<td>{_cost_tokens(g.get("tokens") or 0)}</td>'
            f'<td>{_cost_money(g.get("cost") or 0.0)}</td></tr>')
    if passes.get("inline"):
        n = passes["inline"]
        row(f'<span class="costnote">{n} pass{"es" if n != 1 else ""} ran in this '
            "conversation rather than forking, so there is no transcript of their own to "
            "price — their cost is in the rows below</span>", None, None, "costquiet")

    # --- building the guide -------------------------------------------------
    group("building this guide")
    rows.append(_cost_tab_rows(led.get("tabs") or {}, tabs))

    total = led.get("total") or 0.0
    foot = (f'<tr class="costtotal"><td>total</td>'
            f'<td>{_cost_tokens(led.get("total_tokens") or 0)}</td>'
            f'<td>{_cost_money(total)}</td></tr>')
    return (
        '<table class="costtab costledger">'
        '<caption>What this change cost to produce and to review, at list price — every '
        'turn priced from the transcripts that recorded it. Nobody on a subscription is '
        'billed this; it is what the same tokens would cost on the API.</caption>'
        '<thead><tr><th scope="col">where it went</th><th scope="col">tokens</th>'
        '<th scope="col">cost</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody><tfoot>{foot}</tfoot></table>'
    )


def _cost_tab_rows(costs: dict, tabs: list[dict]) -> str:
    """The per-tab half of the ledger.

    Three shapes of row, because there are three honest answers: a tab with measured spend
    gets its own, biggest first; every measured-zero tab collapses into one muted row that
    names them all (a script wrote that tab, so zero is true, but ten of those stacked
    above the rows carrying the money would bury the point); and every unmeasured tab
    collapses the same way carrying the reason in words, because "we could not measure
    this" must never render identically to a measured zero.
    """
    rows = costs.get("tabs") or {}
    entries = [(t.get("label") or t.get("id"), rows[t.get("id")])
               for t in tabs if rows.get(t.get("id"))]
    if not entries:
        why = costs.get("reason") or "no step ledger, so no turn could be placed in a tab"
        return ('<tr class="costquiet"><td><span class="costnote">'
                f'{html.escape(str(why))}</span></td><td>—</td><td>—</td></tr>')

    def spend(r):
        return r.get("cost") or 0.0

    def toks(r):
        return r.get("tokens") or 0

    measured = [e for e in entries if e[1].get("measured")]
    paid = sorted([e for e in measured if spend(e[1]) or toks(e[1])], key=lambda e: -spend(e[1]))
    free = [e for e in measured if not (spend(e[1]) or toks(e[1]))]
    unknown = [e for e in entries if not e[1].get("measured")]

    def names(items):
        return ", ".join(html.escape(str(l)) for l, _ in items)

    out = "".join(f'<tr><td>{html.escape(str(l))}</td><td>{_cost_tokens(toks(r))}</td>'
                  f'<td>{_cost_money(spend(r))}</td></tr>' for l, r in paid)
    if free:
        out += (f'<tr class="costquiet"><td>{len(free)} tab{"s" if len(free) != 1 else ""} '
                f'with no model spend — {names(free)}</td><td>0</td><td>$0.00</td></tr>')
    if unknown:
        why = costs.get("reason") or "no step in the ledger named them"
        out += (f'<tr class="costquiet"><td>{len(unknown)} tab'
                f'{"s" if len(unknown) != 1 else ""} not measured — '
                f'{html.escape(str(why))} ({names(unknown)})</td>'
                '<td>—</td><td>—</td></tr>')
    resid = costs.get("residual") or {}
    if resid.get("measured"):
        parts = costs.get("residual_parts") or {}
        shown = [(label, parts[key]) for key, label in RESIDUAL_ROWS
                 if (parts.get(key) or {}).get("messages")]
        if shown:
            out += "".join(
                f'<tr class="costquiet"><td>{label}</td>'
                f'<td>{_cost_tokens(part.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(part.get("cost") or 0.0)}</td></tr>'
                for label, part in shown)
        else:
            out += ("<tr class=\"costquiet\"><td>not one tab's — assembling the guide "
                    "itself, plus any step whose window did not cover it</td>"
                    f'<td>{_cost_tokens(resid.get("tokens") or 0)}</td>'
                    f'<td>{_cost_money(resid.get("cost") or 0.0)}</td></tr>')
    return out


def chip_face(c: dict) -> str:
    """A chip's own words: the label it was given and the value it measured. Shared by
    every renderer below so that a chip which moves house — the cost chip becoming half of
    the run chip — cannot pick up different markup on the way."""
    return f'{html.escape(c["label"])} <b>{c["value"]}</b>'


def chip_html(c: dict) -> str:
    """One resolved chip, as the scope bar renders it: a link when it has somewhere to
    send the reader, an inert pill otherwise."""
    inner = chip_face(c)
    if c.get("tip"):
        inner = f'<span data-tip="{html.escape(c["tip"])}">{inner}</span>'
    if c.get("href"):
        return (f'<a class="chip chip-link" href="{html.escape(c["href"])}"'
                f'{" target=_blank" if c["href"].startswith("http") else ""}>{inner}</a>')
    return f'<span class="chip">{inner}</span>'


def validate(spec: dict, out_dir: Path) -> list[str]:
    """Every problem in the content file, named, in one pass.

    A bare ``KeyError: 'png'`` from 300 lines further down tells the author nothing about
    which entry was wrong. ``resolve_refs`` already collects and names its failures; this is
    the same courtesy for the rest of the file, and it runs before any subprocess so a bad
    content file costs a second rather than a full page build."""
    problems = []
    for key, fields in REQUIRED.items():
        for i, item in enumerate(spec.get(key) or []):
            for f in fields:
                # An explicit empty title is a decision, not an omission: a section whose
                # content announces itself does not need a heading repeating the tab name
                # above it. A *missing* key is still the mistake it always was.
                if f == "title" and f in item and not item[f]:
                    continue
                if not item.get(f):
                    problems.append(f"{key}[{i}] is missing {f!r}")
    v = spec.get("verdict")
    if v is not None and "score" not in v:
        problems.append("verdict is missing 'score' (0-10, drives the pip scale)")
    city = spec.get("codecity")
    if city is not None:
        for f in ("png", "href"):
            if f not in city:
                problems.append(f"codecity is missing {f!r}")
        if city.get("png") and not (out_dir / city["png"]).is_file():
            problems.append(f"codecity.png -> {city['png']} does not exist — did step 4 run?")
    for i, s in enumerate(spec.get("sections") or []):
        inc = s.get("includeHtml")
        if inc and not (out_dir / inc).is_file():
            problems.append(f"sections[{i}] ({s.get('id')}) includeHtml -> {inc} "
                            "does not exist — did the step that produces it run?")
    # A requirement may name tests only when the page can say what happened to them. The
    # alternative — rendering every one as "unchanged" because no manifest was loaded —
    # would be the page inventing an answer, which is the one thing it must never do.
    tc = spec.get("testChanges")
    if tc and not (out_dir / tc).is_file():
        problems.append(f"testChanges -> {tc} does not exist — run scripts/test-changes.py first")
    pt = spec.get("playwrightTraces")
    if pt and not (out_dir / pt).is_file():
        problems.append(f"playwrightTraces -> {pt} does not exist — "
                        "run scripts/playwright-traces.py first")
    if any(b.get("type") == "traces" for t_ in spec.get("tabs") or []
           for b in t_.get("blocks") or []) and not pt:
        problems.append("a tab declares a 'traces' block, but no top-level "
                        "'playwrightTraces' manifest says which recordings to show")
    for i, s in enumerate(spec.get("sections") or []):
        for j, req in enumerate(s.get("requirements") or []):
            if not req.get("text"):
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] is missing 'text'")
            for k, t in enumerate(req.get("tests") or []):
                if not t.get("name"):
                    problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] "
                                    f"tests[{k}] is missing 'name'")
            if req.get("tests") and not tc:
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] names tests, "
                                "but no top-level 'testChanges' manifest says what the change "
                                "set did to them")
    for c in spec.get("extraCss") or []:
        if not (out_dir / c).is_file():
            problems.append(f"extraCss -> {c} does not exist "
                            "(the fragment's --css was never written)")
    ids = {s.get("id") for s in spec.get("sections") or []}
    for t_ in spec.get("tabs") or []:
        for b in t_.get("blocks") or []:
            if b.get("type") == "section" and b.get("id") not in ids:
                problems.append(f"tabs[{t_.get('id')}] references section {b.get('id')!r}, "
                                "which is not in 'sections'")
    return problems

HOME_URL = "https://github.com/victorrentea/human-review"

# Where a finished page lives once the projector is off. The `demo zip` workflow rebuilds
# a zip per snapshot under `demo/` on every push to main and clobbers it onto a rolling
# release under a fixed tag, so this address never moves and never goes stale — it is the
# one URL worth reading out to a room, and the one worth forwarding afterwards.
#
# The tag page rather than a particular `.zip`: which snapshot a reader wants is theirs to
# pick, the release notes there name the commit the assets actually stand on, and a link
# that starts a download the instant it is clicked is a poor thing to paste into a chat.
DEMO_ZIP_URL = "https://github.com/victorrentea/human-review/releases/tag/demo"

# The footer's own line is where the offer goes. A reader still reading has no use for it;
# a reader who has reached the bottom is precisely the one who wants to keep a copy — and
# the page they are looking at is usually on somebody else's screen, so "keep a copy" is
# the only thing they can act on at all.
#
# A sibling of the footer sentence, not a clause inside it: that sentence belongs to the
# content file and an author may write anything there or nothing, while this offer is the
# build's and is owed to every page it produces. The link is the verb — `Download here` —
# for the same reason the button under it says `show single page`: down here a reader is
# scanning for a thing to do, not a sentence to read.
TAKEAWAY = (
    f'<span class="takeaway"><a href="{DEMO_ZIP_URL}" target="_blank" rel="noopener" '
    'data-tip="Sample review pages on GitHub, one zip each. Unzip it and open '
    'review.html — no install, no server.">Download here</a> a standalone demo zip.</span>'
)


# The footer used to end on a sentence about the page's own honesty — "every snippet is cut
# from the working tree at build time; every number on this page was measured by the step
# that produced it". It is true, and it is the build's job to *be* true rather than to say
# so; a reviewer opening a review does not want a paragraph of methodology before they have
# seen a line of code. Stripped here rather than only in the writing guidance, because
# content files outlive the instructions that produced them.
FOOTER_BOILERPLATE = re.compile(
    r"\s*(?:Every snippet is cut from the working tree at build time;?\s*"
    r"every number on this page was measured by the step that produced it\.?"
    r"|Snippets were cut from the working tree at build time by\s*"
    r"<code>scripts/extract-snippet\.py</code>;?\s*"
    r"every\s*<code>path:line</code>\s*link opens VS Code at that line\.?)",
    re.I,
)


# "against the running stack" says how the page was built. That was worth saying while the
# alternative was a report written from memory; it is now what the build always does, and a
# reader who cannot run the thing has no use for the distinction. Out, and the room it
# leaves goes to the one sentence a stranger holding this page can act on.
RUNNING_STACK = re.compile(r"\s+against the running stack", re.I)

# A sentence, not a title-cased list of verbs. "Fork, Clone and Port with your Agent" read
# as a feature name and left the reader to work out who does which of the three; naming the
# agent as the one doing the work is the whole point — this is a page you hand to your own
# agent, not a repository you sit down and re-implement.
# Whatever it says, it is emitted as HTML and not escaped on the way out, so any `&` put
# back into it has to be written `&amp;`.
INVITATION = "Tell your agent to adapt this to your environment."


def _link_home(footer: str) -> str:
    """Turn the footer's `/human-review` into the repository it names.

    The slash-command spelling only means something to a reader who already has the skill
    installed; everyone else is holding a page produced by a tool they cannot find. So the
    mention is *replaced* by the URL, not merely linked — the address is the useful thing,
    and it survives the page being printed, pasted or mailed on.

    Only the first occurrence, and never one already inside an <a>.

    The URL alone says where the page came from but not what a reader is meant to do with
    it, and a GitHub link in a footer is read as provenance and skipped. So the address
    is followed by the invitation: this page is a thing you take and re-point at your own
    code, not a credit you note. Appended here rather than asked of every author, for the
    same reason the boilerplate is stripped here.
    """
    footer = RUNNING_STACK.sub("", FOOTER_BOILERPLATE.sub("", footer or "")).strip()
    if not footer or "/human-review" not in footer or 'human-review"' in footer:
        return footer
    linked = footer.replace(
        "/human-review",
        f'<a href="{HOME_URL}" target="_blank" rel="noopener">{HOME_URL}</a>', 1)
    return f"{linked} {INVITATION}" if INVITATION not in linked else linked


def page_title(spec: dict) -> str:
    """The one line that says which change this is.

    A content file’s own `title` is a sentence about the change ("Attending vet on a
    visit"). The reviewer, though, is looking at a pull request, and the name that
    matches what is in their tabs, their notifications and their `gh pr` output is
    `GH#37 <the PR’s own title>`. So when the content file names a PR, that wins, and
    the number is the link to it. With no `pr` block nothing changes.

    `GH#37` is the only link on this page a reader cannot recognise as one by where it
    sits: it is the first word of the `<h1>`, so it wears the page's heading weight, not
    a link's. The hover says where it goes — the one thing a reader wants before clicking
    away from the review they just opened.
    """
    pr = spec.get("pr") or {}
    if pr.get("number") and pr.get("title"):
        num = f'GH#{html.escape(str(pr["number"]))}'
        if pr.get("url"):
            num = (f'<a class="prref" href="{html.escape(pr["url"])}" '
                   f'data-tip="Open #{html.escape(str(pr["number"]), quote=True)} '
                   f'on GitHub">{num}</a>')
        return f'{num} {html.escape(pr["title"])}'
    return html.escape(spec.get("title", "Review guide"))


def ref_badges(spec: dict, state: dict | None = None) -> str:
    """`branch test-pr` `base main` — the two refs every number on this page is a
    comparison of, each one click from its own page on GitHub.

    They lead the scope bar rather than trailing the title, and both moves are the same
    decision. "Against what, again?" is a question asked halfway down the ninth tab, so
    the answer has to be somewhere the masthead still shows — and the row it belongs in
    is the one that already answers *how much*: `files`, `lines`, what the review found,
    what it cost. Two refs and six numbers about them are one thought, and the title row
    is then free to be a title.

    They are chips, not parenthesised asides, because in that row `(test-pr)` beside
    `files +1 / ✍️40` reads as an unlabelled number. The label is what makes the pair
    legible in one pass, and it costs four characters.

    The base chip carries a `!` when the two refs have drifted apart — the base has moved
    ahead of the fork point, or the local branch named here is behind the remote actually
    measured. It is on *this* chip and not in a banner because the question it answers is
    "compared against what, exactly?", which is the question the chip already exists to
    answer; a page-wide warning would be read once and dismissed, while a mark on the ref
    is there every time a reader comes back to check the pair. It is computed on every
    build from the refs as they stand, so it clears itself the moment main is merged in —
    there is no state to reset and nothing to remember.
    """
    pr = spec.get("pr") or {}
    repo = (pr.get("repo") or "").rstrip("/")
    warning = base_warning(state)
    out = []
    for key, label, cls, why in (("branch", "branch", "head", "the branch under review"),
                                 ("base", "base", "base", "the base it is compared against")):
        ref = pr.get(key)
        if not ref:
            continue
        inner = (f'{label} <b class="refname {cls}">{html.escape(ref)}</b>')
        # The chip is a link, so the hover's job is to say where the click goes — not to
        # re-describe a ref whose name is already the thing being read. A chip that does
        # not link anywhere gets no bubble at all rather than a sentence about itself.
        tip = "Open in GitHub" if repo else ""
        cls_extra = ""
        if key == "base" and warning:
            # The mark carries its own tooltip rather than extending the chip's: the chip
            # says what the ref is, the mark says what is wrong with it, and a reader who
            # hovers the `!` is asking the second question, not the first.
            inner += (f'<span class="drift" role="img" aria-label="stale base" '
                      f'data-tip="{html.escape(warning)}">!</span>')
            cls_extra = " drifted"
        tip = html.escape(tip)
        if repo:
            href = html.escape(f"{repo}/tree/{urllib.parse.quote(ref)}")
            out.append(f'<a class="chip chip-link refchip{cls_extra}" href="{href}" '
                       f'data-tip="{tip}">{inner}</a>')
        else:
            out.append(f'<span class="chip refchip{cls_extra}">{inner}</span>')
    return "".join(out)


def verdict_band_html(v: dict, n: int, cls: str) -> str:
    """The full-bleed band under the masthead: the score, and the reasons for it.

    **No reasons, no band.** The bullets are the reasons, and without them all the band
    renders is `5/10 not yet mergeable` — the pill beside the title said a second time, one
    screenful lower, inside two rules and beside a viewport of empty grid. The score is not
    lost by dropping it: it is in the masthead, where it is the first thing on the page and
    already links to the findings the band would have summarised."""
    if not v.get("bullets"):
        return ""
    pips = "".join(f'<i class="{"on" if i < n else ""}"></i>' for i in range(10))
    return (
        f'<div class="verdict {cls}">'
        f'<div class="score"><b>{n}<small style="font-size:.42em;opacity:.5">/10</small></b>'
        f'<span>{html.escape(v.get("label", ""))}</span>'
        f'<div class="scale">{pips}</div></div>'
        + "<ul>" + "".join(f"<li>{b}</li>" for b in v["bullets"]) + "</ul>"
        + "</div>"
    )


def _score_target(spec) -> tuple[str, str]:
    """`("review", "Review")` — the tab the verdict's reasons live in, for the score to
    link to, and the name to say in the hover.

    Found by what a tab renders, not by its id: `findings` is the block that holds the
    calls behind a score, wherever the content file puts it. The first tab is the fallback
    — it is the panel the page opens on, so a score linking there at worst goes where the
    reader already was. The emoji a label may lead with is dropped from the hover: `Open
    the 🤖 Review tab` reads as a glyph the sentence has to step over, and the pill in the
    strip is recognisable by its word.
    """
    tabs = spec.get("tabs") or []
    for tab in tabs:
        if any(b.get("type") == "findings" for b in tab.get("blocks") or []):
            return tab.get("id", ""), (tab.get("label") or tab.get("id", "")).lstrip("🤖 ")
    if tabs:
        return tabs[0].get("id", ""), (tabs[0].get("label") or "").lstrip("🤖 ")
    return "", ""


def masthead_html(spec: dict, title_score: str, chips: str, strip_html: str,
                  base_st: dict | None = None) -> str:
    """Title, refs, note, scope chips and the tab strip — as one block that stays put.

    They used to be four bands that scrolled away, leaving the strip pinned alone over
    the text. Every one of them answers a question a reader has *while* reading a tab,
    so they travel together, and the title sits against the top edge rather than behind
    a gutter that would then be pinned there for the whole read.

    Only a tabbed page gets one: without a strip there is nothing to pin the masthead
    for, and the plain single-column guide keeps the heading it always had.
    """
    heading = f'<h1>{page_title(spec)}</h1>'
    if spec.get("pr"):
        # One line, and only the two things a reader navigates by: which change this is,
        # and how it scored. The refs moved down to the scope bar (`ref_badges`), and the
        # sentence describing the change is gone from here entirely — it is the first
        # thing the summary says, and a block that never scrolls cannot spend its width
        # on a sentence that is read once. `subtitle` is still in the content file and
        # still renders on a page with no `pr` block.
        rows = [f'<div class="titlerow oneline">{heading}{title_score}</div>']
        chips = ref_badges(spec, base_st) + chips
    else:
        rows = [f'<div class="titlerow">{heading}{title_score}</div>',
                f'<p class="sub">{spec.get("subtitle", "")}</p>']
    rows.append(f'<div class="scopebar">{chips}</div>')
    if not strip_html:
        return "\n".join(rows)
    return '<header class="masthead">\n' + "\n".join(rows + [strip_html]) + "\n</header>"


def rebuild_interpreter() -> str:
    """How to say "python, with Pygments" on *this* machine, in a command a reader pastes.

    Not `sys.executable`. Under `uv run --with pygments` that is a path inside a build
    directory uv deletes on exit, so the one command guaranteed to have worked is also the
    one guaranteed not to work again — and a command that fails when pasted is worse than
    no command, because it is tried first and believed second.

    So: plain `python3` when it can already import Pygments, which is the legible answer
    and the portable one; `uv run --with pygments python` when it cannot and uv is here,
    which installs nothing permanently and is ~100ms warm; and only then the interpreter
    running this build, which at least names something real.
    """
    # The probe has to ask what `python3` means *in the reader's terminal*, not in this
    # process. Under `uv run` the front of PATH is two throwaway directories — the
    # interpreter's own, and the one holding the packages `--with` installed — and both
    # answer to `python3` with Pygments importable, so a naive probe cheerfully reports
    # that `python3` works about interpreters that are deleted on exit. Dropping every
    # ephemeral directory is what asks the right question; if nothing outside them answers
    # to `python3`, that is an answer too, and the uv line below is the honest one.
    env = dict(os.environ)
    throwaway = [str(Path(sys.prefix)), env.get("VIRTUAL_ENV") or "",
                 os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")]
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", "").split(os.pathsep)
        if d and not any(t and (d == t or d.startswith(t + os.sep)) for t in throwaway))
    env.pop("VIRTUAL_ENV", None)
    outside = shutil.which("python3", path=env["PATH"])
    if outside and subprocess.run([outside, "-c", "import pygments"],
                                  capture_output=True, env=env).returncode == 0:
        return "python3"
    if shutil.which("uv", path=env["PATH"]) or shutil.which("uv"):
        return "uv run --with pygments python"
    return shlex.quote(sys.executable)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("content", help="JSON content file")
    ap.add_argument("--out", required=True, help="where to write the HTML")
    # The one thing in a build that is not a program: the Logging tab asks a model whether
    # a logged value is a privacy problem. Cached by a hash of what was sent, so a rebuild
    # of unchanged code neither re-asks nor re-pays — but a *new* statement would, and a
    # refresh that quietly buys an answer is exactly the thing `refresh-report.py` exists
    # to keep separate from the half a human asked for.
    ap.add_argument("--no-model", action="store_true",
                    help="use only cached privacy verdicts; render the rest as "
                         "not evaluated instead of calling the model")
    args = ap.parse_args(argv)

    global OFFLINE
    OFFLINE = args.no_model

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    spec = json.loads(Path(args.content).read_text(encoding="utf-8"))
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Emptied here rather than trusted to be empty: the register is module state, and the
    # module is imported and driven directly by the test suite, where two builds in one
    # process would otherwise leave the second one declaring the first one's actions.
    ACTIONS.clear()
    # How to start this build again, for the copy button under the hand-drawn diagram.
    rebuild_cmd = " ".join([rebuild_interpreter(), shlex.quote(str(Path(__file__).resolve())),
                            shlex.quote(args.content), "--out", shlex.quote(args.out)])

    problems = validate(spec, out_dir)
    if problems:
        print(f"[review] {Path(args.content).name} cannot be rendered:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # The base every `diffs` entry is measured against when the entry does not name its
    # own: the rev the ledger recorded before the review pass touched anything. That is the
    # only left side that shows a fix on its own, and step 1 exists to record it.
    default_diff_base = review_step_rev(out_dir)
    for f in spec.get("findings", []) + spec.get("assumptions", []) + spec.get("autofixes", []):
        f["_refs"] = resolve_refs(f.get("refs", []), root)
        f["_snippets"] = "".join(
            snippet_html(s["ref"], s.get("caption"), root) for s in f.get("snippets", [])
        )
        # An applied fix that shows no diff is a claim with nothing behind it, so the build
        # says so — loudly enough to fix, quietly enough not to block a page whose ledger
        # has no rev (an older run, or a review re-rendered from a fresh clone).
        diffs = f.get("diffs", [])
        if diffs and not all(d.get("base") for d in diffs) and not default_diff_base:
            print("[review] WARNING: %r asks for a diff with no base, and the ledger's "
                  "review step recorded no rev \u2014 that diff is dropped"
                  % f.get("title", "")[:60], file=sys.stderr)
        f["_diffs"] = "".join(
            diff_html(d["path"], d.get("base") or default_diff_base, root, d.get("caption"),
                      d.get("head"))
            for d in diffs
            if d.get("base") or default_diff_base
        )

    # An assumption with no code under it is the one item on this page that cannot be
    # checked at all. A finding without a snippet is at least a claim about a defect a
    # reader can go and look for; "I assumed the tenant is always the caller's" points at
    # nothing, and a model asked at the end of a long session what it was unsure about will
    # produce fluent sentences of exactly that shape whether or not it ever hesitated. The
    # anchor is what separates a recollection from a guess about a recollection, so an
    # unanchored one is dropped rather than printed with a shrug.
    floating = [a for a in spec.get("assumptions", [])
                if not (a.get("_snippets") or a.get("_diffs") or a.get("_refs"))]
    for a in floating:
        print("[review] WARNING: assumption %r names no code — dropped. Give it a "
              "'snippets' entry (or 'refs'/'diffs') pointing at the line the decision "
              "landed on; an assumption a reader cannot go and look at is indistinguishable "
              "from one that was never made." % (a.get("title", "")[:60]), file=sys.stderr)
    if floating:
        spec["assumptions"] = [a for a in spec["assumptions"] if a not in floating]

    # A page that never declares the block says nothing at all about the third pile, and
    # nothing reads as "there was nothing to say". The pile is the one part of this page no
    # pass can reconstruct afterwards, so its absence has to be noisy at build time rather
    # than silent on the page — declare it with its mode (`authoring-sessions.py` says
    # which) and the lede prints the count, zero included.
    if _assumptions_block(spec) is None:
        print("[review] WARNING: no 'assumptions' block in any tab — the page will say "
              "nothing about what the agent that wrote the code had to guess at, which a "
              "reader cannot tell from it having guessed at nothing. Declare the block "
              "with its mode (authoring-sessions.py --base ... says A, B or C) even when "
              "the pile is empty.", file=sys.stderr)

    # Every panel is `id="<tab id>"`, so a section that happens to share a tab's id puts the
    # same id on two elements — `id="api"` on the API contract panel and on the <h2> inside
    # it, which is what this page shipped for months. Nothing looked broken, because
    # getElementById returns the first match and the first match is the panel, which is
    # where `#api` should land anyway. It is still invalid HTML and still a trap for the
    # next person. The panel's id is not negotiable (the strip's aria-controls points at
    # it), so the duplicate is resolved on the heading, which loses nothing.
    tab_ids = {t.get("id") for t in (spec.get("tabs") or [])}

    # What the change set did to each test, computed by `test-changes.py` from the diff
    # itself. Loaded once: the content file only says which requirement a test belongs to.
    test_doc = (json.loads((out_dir / spec["testChanges"]).read_text(encoding="utf-8"))
                if spec.get("testChanges") else {})
    # The recordings the same run left behind, harvested by `playwright-traces.py`. Loaded
    # the same way and for the same reason: what happened in a test is measured, never
    # written down by hand.
    traces_doc = (json.loads((out_dir / spec["playwrightTraces"]).read_text(encoding="utf-8"))
                  if spec.get("playwrightTraces") else {})
    tests_idx = test_index(test_doc.get("tests", []))

    sections, by_id, unchanged_ids = [], {}, {}
    for s in spec.get("sections", []):
        unchanged_ids[s["id"]] = bool(s.get("unchanged"))
        snips = "".join(
            snippet_html(x["ref"], x.get("caption"), root) for x in s.get("snippets", [])
        )
        # An include is a fragment another generator produced (e.g. the complexity delta):
        # rendered by whoever owns that data, pasted in here rather than re-derived.
        inc = ""
        if s.get("includeHtml"):
            inc = (out_dir / s["includeHtml"]).read_text(encoding="utf-8")
        # Usually the include is commentary on the prose, so it follows it. `includeFirst`
        # is for the one shape where it is the other way round: the fragment *is* what the
        # section is about — the Tests tab opens on the ticket the branch answers —
        # and the prose reads as the reply to it. Off by default: every other tab wants a
        # sentence of its own before a generated fragment lands.
        include_first = bool(s.get("includeFirst"))
        vid = ""
        if s.get("video"):
            vid = video_html(s, out_dir)
        body = shorten_dgm_src(
            expand_drawio(expand_snippets(s.get("body", ""), root), out_dir, root,
                          rebuild_cmd))
        collides = s["id"] in tab_ids
        if collides:
            print(f'[review] section {s["id"]!r} shares its id with a tab: the heading drops '
                  f'its id, so #{s["id"]} lands on the panel (which is where it was already '
                  "going). Rename the section to get an anchor of its own.", file=sys.stderr)
        h2_id = "" if collides else f' id="{html.escape(s["id"])}"'
        rendered = (
            # An empty title means the section speaks for itself; emit no heading rather
            # than an empty one, which would still take the vertical space of a heading.
            (f'<h2{h2_id}>{html.escape(s["title"])}</h2>\n' if s.get("title") else "")
            + (f"{inc}\n" if include_first and inc else "")
            # A section with no prose of its own — the video tab is one — must not open with
            # a blank line where the paragraph would have been.
            + (f"{body}\n" if body else "")
            # The requirement list sits between the prose and anything else the section
            # carries: it *is* the section's answer, and a snippet or an include is
            # commentary on it.
            + render_requirements(s.get("requirements") or [], tests_idx, root)
            + f'{"" if include_first else inc}{vid}{snips}{embed_html(s, out_dir)}'
        )
        sections.append(rendered)
        by_id[s["id"]] = rendered

    city = spec.get("codecity")
    city_html = ""
    if city:
        # No heading, and `title` on the block is ignored — same reason `logging` has none.
        # The tab is called *Code City* and the picture is the first thing under it, so a
        # heading above it is the tab's label said a second time. The anchor moves to the
        # line under it so `#codecity` still lands here.
        city_html = (
            f'<p id="codecity">{city.get("body", "")}</p>\n'
            f'<a class="city" href="{html.escape(city["href"])}" target="_blank" rel="noopener"'
            f' data-tip="Open the interactive Code City in a new tab">'
            f'<img src="{html.escape(city["png"])}" alt="Code City with the branch change set highlighted"></a>\n'
            # Only when there is one: the empty <p> still took a paragraph's margin under
            # the picture, on every page that never wrote a caption.
            + (f'<p class="sub">{city["caption"]}</p>' if city.get("caption") else "")
        )

    # Chips carry HTML on purpose: a chip is often a link (to the branch on GitHub, to a
    # section further down) or coloured (+added / -removed), and escaping would kill both.
    chips = []
    scope = spec.get("scope", [])
    # Where the base actually is, asked once: the diffstat chip measures against it and
    # the ref chip warns about it, and those two must never be talking about different
    # commits. `origin/main` is the default because it is what a pull request merges into;
    # a content file naming something else is taken at its word.
    base_st = base_state(root, (spec.get("pr") or {}).get("base") or "origin/main")

    # The cost of the run answers two chips now -- what it cost, and which model did the
    # reviewing -- and they can appear in either order in the content file, so the answer
    # is memoised rather than fetched where it happens to be needed first. A list, not a
    # variable, so `None` (a real answer: no session to ask) is distinguishable from
    # "not asked yet".
    cost_memo: list = []

    def resolved_cost() -> dict | None:
        if not cost_memo:
            cost_memo.append(cost_chip(root))
        return cost_memo[0]

    def emit(c: dict) -> None:
        """Render one resolved chip. Shared so that a chip which expands into several --
        `diffstat` becomes `files` and `lines` -- cannot pick up different markup than
        the ones written by hand beside it."""
        chips.append(chip_html(c))

    for c in scope:
        # The other chip that must never be typed. `{"auto": "autofixed"}` counts the two
        # lists this page actually renders — the open findings and the applied fixes — so
        # the chip and the LLM Review tab can never disagree with each other. The reason
        # it exists is that they already did: the hand-typed `/code-review 8 findings`
        # outlived the ninth finding being added, and nothing caught it, because nothing
        # was looking. `href` (and any label or tip) still comes from the content file.
        if c.get("auto") == "autofixed":
            fixed = len(spec.get("autofixes", []))
            total = len(spec.get("findings", [])) + fixed
            # Who reviewed is half of what this chip says, and it used to sit in a
            # second chip beside it (`reviewed by  Opus 5`) that nobody could check. The
            # run knows: `review-cost.py` returns the models it spent money on, most
            # expensive first. Two chips carrying one thought become one carrying it
            # fully -- `Opus 5 review  9 open · 3 autofixed` -- and the name is now as
            # measured as the numbers next to it. `by` in the content file is the fallback
            # for a page rebuilt outside the session that reviewed it; "LLM review" is the
            # last resort, and says exactly as much as it knows.
            paid = resolved_cost() or {}
            reviewer = next((m for m in paid.get("models") or [] if m and m != "synthetic"),
                            None) or c.get("by")
            computed = {
                # A colon, not a gap. The pill reads as one sentence — `🤖 Fable 5 review:
                # 6 open, 4 auto-fixed` — where before it was a label, a gap and a row of
                # numbers, which is the shape of a measurement rather than of a statement.
                # The robot is the page's own mark for "a model produced this", the same
                # one the inferred headings wear, and it is what makes the chip legible as
                # a claim by a machine rather than as another count of the diff.
                "label": f"\U0001f916{reviewer} review:" if reviewer else "\U0001f916LLM review:",
                # Both halves computed. The chip used to read `auto-fixed <n>`, and the
                # label did the lying the tooltip then had to walk back: only three of the
                # twelve were fixed, and a reader who never hovers was told all twelve
                # were. Neither number here can drift from the lists behind it.
                #
                # Open leads, and the total is gone from the face: `12 raised` is the sum
                # of the other two, so it is the one number on the chip nobody acts on,
                # while `3 autofixed` is the fact a reader cannot get anywhere else
                # without opening the tab. The order is the order of the work — what is
                # left to do first, what was already done for you second. The total is
                # still one hover away.
                # The applied half is greyed: it is on the page so the reader can check
                # it, not so they can act on it, and at full contrast it competes with the
                # number that IS the work. Grey is the page's own "already handled" —
                # the same treatment the fixes themselves get in the list below.
                "value": f'{total - fixed} open, '
                         f'<span class="sub">{fixed} auto-fixed</span>',
                # The total, which the face no longer carries, split by the pass that
                # raised each item. `by /code-review and /simplify` named the two passes
                # and left the reader to guess the split — which is the only thing the
                # hover could add, since the chip is already about a number. It is counted
                # off each item's own `source`, so the breakdown cannot disagree with the
                # stamps in the list below it.
                #
                # What is not here any more: `running on <model>`. The chip's own face
                # reads `Opus 5 review` — a hover restating the word next to it is a hover
                # that taught the reader not to bother with the next one.
                "tip": _raised_by(spec.get("findings", []) + spec.get("autofixes", []),
                                  total),
            }
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}
        # A chip that has to be kept up to date by hand is a chip that will be wrong. The
        # cost of the run is the extreme case: it is still changing while the page is being
        # written, so it is computed here, at build time, and never typed into the content
        # file. `{"auto": "cost"}` is the whole declaration; label, value and tooltip all
        # come back from the script.
        # The same discipline for the test count: the page already parses every changed
        # test file to classify the rows under each requirement, so the number at the top
        # is read off that same manifest and cannot disagree with the list below it.
        # `files` and `lines` -- one measurement, two chips, so they can never end up
        # describing different ranges. `exclude` adds pathspecs to the generated-file list
        # this always applies; there is no way to turn that list off, because a diffstat
        # dominated by regenerated diagrams is not a stricter answer, it is a wrong one.
        if c.get("auto") == "diffstat":
            for computed in diffstat_chips(root, base_st, c.get("exclude"),
                                           spec.get("pr")):
                emit({**computed, **{k: v for k, v in c.items()
                                     if k not in ("auto", "exclude")}})
            continue

        if c.get("auto") == "tests":
            computed = tests_chip(test_doc)
            if computed is None:
                continue
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}

        if c.get("auto") == "cost":
            # Dropped, not rendered and not an error. The cost is the last tab on the strip
            # now; a chip in the bar could only ever carry the review's own share of it,
            # which turned out to be the smaller half of what the reader wanted. Content
            # files in the wild still ask for the chip, and silence is the right answer to
            # them: the number they wanted is on the page, one pill further right.
            continue
        emit(c)
    chips = "".join(chips)

    # This used to require a chip per automated pass — /code-review hunts bugs, /simplify
    # shrinks the solution, so one number over both reports neither. The page now carries a
    # single merged chip instead, by decision, and the old check fired on every run: a
    # warning that is always on is a warning nobody reads.
    #
    # The concern behind it survives in a form the merged chip can actually fail. One number
    # over two questions is honest only while the split it summarises is still visible. That
    # split used to be a second tab with a chapter per pass; it is now the `source` stamp on
    # each item, which is a better home for it — the reader sees who raised a finding beside
    # the finding, rather than in a parallel listing that can fall out of step with this one.
    # So two things are checked: that the chip's target exists, and that the stamps are
    # actually there. A merged count with nothing behind it is the real regression.
    # The check that was missing for six days. `files` and `lines` are computed now, so a
    # content file still typing them is not merely redundant — it is the exact failure this
    # release exists to end, and it fails silently, because a number with a sign in front of
    # it reads as something a tool produced. Loud, and not fatal: a page that still renders
    # is better than a build that refuses, and the author sees this the moment they run it.
    typed = [c.get("label") for c in scope
             if not c.get("auto") and c.get("label") in ("files", "lines")]
    if typed:
        print(f"[review] WARNING: {' and '.join(typed)} typed by hand in 'scope' — replace "
              'them with {"auto": "diffstat"}, which measures the change set at build time. '
              "A typed diffstat is the one number on this page nothing can catch going "
              "stale: it looks measured, and it outlives every commit made after it.",
              file=sys.stderr)

    if any(c.get("auto") == "autofixed" for c in scope):
        target = next((c.get("href", "") for c in scope if c.get("auto") == "autofixed"), "")
        anchor = target.lstrip("#")
        known = {tb.get("id") for tb in spec.get("tabs") or []} | \
                {sec.get("id") for sec in spec.get("sections", [])}
        if anchor and anchor not in known:
            print(f"[review] WARNING: the LLM-review chip deep-links to {target} but nothing "
                  "on the page has that id — the one number on the scope bar opens nothing",
                  file=sys.stderr)
        items = spec.get("findings", []) + spec.get("autofixes", [])
        if items and not any((i.get("source") or "").strip() for i in items):
            print("[review] WARNING: the chip merges /code-review and /simplify into one "
                  "number and not one item carries a 'source' — the split the chip "
                  "summarises is nowhere on the page behind it", file=sys.stderr)

    # The piles are one numbered list and each starts where the last stopped, so the order
    # in this file is the order on the page — including the order of the numbers. That
    # makes the ordering an editorial choice rather than a bug waiting to happen, and leaves
    # one rule worth enforcing: work that is already done is the tail. An `autofixes` block
    # anywhere but last opens the reviewer's list with items they have nothing to do about.
    _blocks = [b.get("type") for tb in (spec.get("tabs") or []) for b in (tb.get("blocks") or [])
               if b.get("type") in ("findings", "assumptions", "autofixes")]
    if "autofixes" in _blocks and _blocks[-1] != "autofixes":
        after = _blocks[_blocks.index("autofixes") + 1]
        print(f"[review] WARNING: the autofixes block renders before {after!r}, so the list "
              "opens with work that is already done — the piles are one list, and "
              "the applied fixes are its tail", file=sys.stderr)

    v = spec.get("verdict")
    verdict_html = ""
    title_score = ""
    if v:
        n = int(v["score"])
        # The score belongs beside the title: it is the one thing a reader wants before
        # they have decided whether to read anything. The band below keeps the reasons.
        band = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")
        face = (f'<b>{n}</b><small>/10</small>'
                f'<i>{html.escape(v.get("label", ""))}</i>')
        # `5/10 not yet mergeable` states a conclusion and shows none of the reasoning, so
        # the click every reader tries on it is the one that goes to the findings. It is
        # not a decoration with a link bolted on: the tab it opens is found by *content* —
        # whichever tab renders the findings — so a page that arranges its tabs
        # differently still sends the score where its reasons actually are.
        target, target_label = _score_target(spec)
        title_score = (
            f'<a class="titlescore {band}" href="#{html.escape(target, quote=True)}" '
            f'data-tip="Open the {html.escape(target_label, quote=True)} tab">{face}</a>'
            if target else f'<span class="titlescore {band}">{face}</span>')
        verdict_html = verdict_band_html(v, n, band)

    extra_css = "".join((out_dir / c).read_text(encoding="utf-8") for c in spec.get("extraCss", []))
    # The snippet extractor owns its own token colours, so the page asks it for them
    # rather than keeping a second copy that would drift from the highlighter.
    extra_css += subprocess.run(
        [sys.executable, str(EXTRACT), "--css"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout
    # Same rule for the code-owners check: the block is rendered by the script, so the
    # content file never has to remember to list a stylesheet it does not own.
    if any(b.get("type") == "codeowners"
           for t in spec.get("tabs") or [] for b in t.get("blocks", [])):
        extra_css += subprocess.run(
            [sys.executable, str(CODEOWNERS), "--css"],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout

    dspec = spec.get("diagrams", {})
    manifest_rows = read_manifest(out_dir / dspec.get("manifest", "assets/diagrams/MANIFEST.tsv"))
    placed = set()

    def heading(block, fallback_id, fallback_title):
        title = block.get("title", fallback_title)
        if not title:
            return ""
        head = (f'<h2 id="{html.escape(block.get("id", fallback_id))}">'
                f'{html.escape(title)}</h2>')
        return head + (f'<p>{block["body"]}</p>' if block.get("body") else "")

    # A block can hang a badge on the tab that holds it — filled in per tab, below.
    auto_badge = {}
    # Whether any tab asked for the test ledger. If none did and there is a manifest, the
    # page appends it to the tab the manifest belongs to rather than dropping it: the
    # whole point of computing what happened to the tests is that a reviewer sees it, and
    # a content file written before this block existed must not silently lose it.
    placed_ledger: list[bool] = []
    reset_list()

    def render_block(block):
        """One block of a tab, as (html, weight, changes).

        `weight` is "is there anything at all to show" — a tab whose every block weighs
        nothing is dropped. `changes` is the narrower question "did *this branch* move
        anything here" — a tab that is all context and no delta is kept, and struck
        through on the strip. A picture of the current state is not a change; that is
        why `puml` and `codecity` carry weight but no changes."""
        kind = block.get("type", "section")
        if kind == "findings":
            items = spec.get("findings", [])
            head = _lede_above(heading(block, "first", "Requires human review"), opening_lede(spec))
            return (head + render_findings(items), len(items), len(items))
        if kind == "assumptions":
            items = spec.get("assumptions", [])
            mode = block.get("mode", "")
            head = _lede_above(heading(block, "assumed", "Decided without asking you"),
                              opening_lede(spec))
            # Weight 1 even with nothing in it: an empty pile still carries the sentence
            # saying *which* kind of empty it is, and that sentence is the point.
            return (head + render_assumptions(items, mode),
                    1 if (items or mode) else 0, len(items))
        if kind == "autofixes":
            items = spec.get("autofixes", [])
            head = _lede_above(heading(block, "fixed", "Auto-fixed"),
                              opening_lede(spec))
            return (head + render_autofixes(items), len(items), len(items))
        if kind == "diagrams":
            # A block may name a manifest of its own. One producer does: the C2 view is
            # projected from the sequence diagrams rather than diffed out of a .puml that
            # changed, and it cannot file its row in `assets/diagrams/MANIFEST.tsv`
            # because `puml-diff.sh` does `rm -rf` on that whole directory every time it
            # runs — which the `sequence` step makes it do AFTER the `diagrams` step
            # wrote it. Its rows stay out of `placed` on purpose: `placed` answers "did
            # every row of the SHARED gallery find a tab", and a private manifest has no
            # orphans to warn about.
            own = block.get("manifest")
            source_rows = read_manifest(out_dir / own) if own else manifest_rows
            rows = select_rows(source_rows, block)
            if not own:
                placed.update(r["name"] for r in rows)
            # Nothing of this family changed. A block that names a `context` diagram
            # (the Packages case: no delta, but the current package shape is still
            # worth showing) falls back to rendering it from source — exactly like a
            # standalone `puml` block, and `render_puml` never returns zero weight, not
            # even for a missing file. That is what makes a tab built on this one block
            # *reliably* struck-through-but-present rather than droppable: the guarantee
            # lives here, not in the discipline of remembering to pair it with a second
            # block that happens to always weigh 1.
            if not rows:
                context = block.get("context")
                if context:
                    return (heading(block, "diagrams", dspec.get("title", ""))
                            + render_puml(context, root, out_dir), 1, 0)
                return "", 0, 0
            merged = dict(dspec)
            # The rows are already filtered; `only` survives purely as the running order
            # the author asked for. Popping it here is what used to make
            # `only: ["DomainModel", "DB"]` come out alphabetical anyway.
            merged["only"] = block.get("only") or dspec.get("only") or []
            if own:
                # Every SVG a row names is resolved relative to its own manifest, so this
                # has to travel with the rows or the pictures 404 next to the gallery's.
                merged["manifest"] = own
            return (
                heading(block, "diagrams", dspec.get("title", ""))
                + render_diagrams(merged, root, out_dir, rows),
                len(rows), len(rows),
            )
        if kind == "testpairs":
            rows = [r for r in select_rows(manifest_rows, block) if r["kind"] == "sequence"]
            placed.update(r["name"] for r in rows)
            return render_testpairs(block, dspec, manifest_rows, root, out_dir)
        if kind == "logging":
            return logging_fragment(block, root)
        if kind == "puml":
            return (heading(block, "puml", block.get("title", ""))
                    + render_puml(block, root, out_dir), 1, 0)
        if kind == "codeowners":
            frag, summary = codeowners_fragment(block, root, out_dir)
            state, owned = summary["state"], summary["owned"]
            # No CODEOWNERS in the repository is not a finding, it is an absence: drop
            # the tab rather than teach the reviewer to ignore a permanent grey box.
            if state == "no_codeowners":
                print("[review] no CODEOWNERS file — dropping the code-owners tab",
                      file=sys.stderr)
                return "", 0, 0
            if state == "approval_required":
                auto_badge["tabClass"] = "alarm" if summary.get("severity") == "critical" else "warn"
                auto_badge["label"] = "approval required"
            # No default heading, for the reason `codecity` has none: the tab pill says
            # CODEOWNERS, its badge says "Code owners approval required", and the seal
            # under it says APPROVAL REQUIRED. A fourth `Code owners` above the first
            # filename is the label said again. An explicit `title` still renders.
            return (heading(block, "codeowners", "") + frag,
                    1, len(owned))
        if kind == "tests":
            frag, moved = render_test_ledger(test_doc.get("tests", []), root)
            placed_ledger.append(True)
            if not frag:
                return "", 0, 0
            return (heading(block, "test-ledger",
                            block.get("title", "What this change set did to the tests"))
                    + frag, 1, moved)
        if kind == "traces":
            touched = {(Path(t["path"]).name, t.get("line"))
                       for t in test_doc.get("tests", []) if t.get("status") != "unchanged"}
            frag, n = render_traces(traces_doc, root, out_dir, touched)
            if not frag:
                return "", 0, 0
            # No heading: the block is a registry the 📺 on the covering-tests rows read,
            # not a thing to look at. Weight, and no changes — the same call `codecity`
            # and `puml` make. A trace is a recording of how the code behaves now; it is
            # evidence *about* the branch, not a thing the branch moved.
            return frag, n, 0
        if kind == "codecity":
            return city_html, 1 if city_html else 0, 0
        if kind == "section":
            body = by_id.get(block["id"])
            if body is None:
                raise SystemExit(f'[review] tab block references no section: {block["id"]}')
            # A section is prose we wrote about the change, so it counts as a change
            # unless it declares itself context.
            return body, 1, 0 if unchanged_ids.get(block["id"]) else 1
        if kind == "html":
            has = 1 if block.get("html") else 0
            return block.get("html", ""), has, 0 if block.get("unchanged") else has
        raise SystemExit(f"[review] unknown tab block type: {kind}")

    tabs = spec.get("tabs")
    lede_html =f'<div class="lede">{spec.get("summary", "")}</div>' if spec.get("summary") else ""
    summary_html = lede_html
    overview_html = ""
    if tabs:
        # The summary and the verdict used to sit above the strip, which pushed the
        # questions below the fold on a laptop — a reviewer scrolled past the answers to
        # find out what the answers were. So they became a tab, and that was one move too
        # far: a tab is a question a reader chooses, and *"what is this change, and is it
        # mergeable"* is not chosen — it is what the page opens with. It bought a pill in
        # the strip, a click to leave, and a second click to come back for a summary
        # nobody returns to twice.
        #
        # They open the first tab instead, as its lede. The strip still lands in the first
        # screenful, the reader still reads the summary before anything else — and the tab
        # they are standing in when they finish it is the one they were going to open
        # next. It rides on `intro` rather than as a block, so it sits above the tab's own
        # preamble (the summary is about the change; an intro is about the tab) and, like
        # every intro, carries no weight: the panel it opens is kept alive by its own
        # content, never by the page's lede leaning on it.
        overview_html = lede_html + verdict_html
        summary_html = lede_html
        if overview_html:
            first, *rest = tabs
            tabs = [{**first, "intro": overview_html + first.get("intro", "")}, *rest]
            lede_html = verdict_html = ""
    # The ledger is derived data, like the requirement lists it sits under: nobody writes
    # it, and a content file that predates the block would otherwise leave the manifest
    # computed and unread. So a page that has a manifest and no `tests` block gets one,
    # appended to the tab the `tests` step feeds. Declaring the block explicitly is still
    # how you put it somewhere else in the panel — this only fills a gap, it never moves
    # a block the author placed.
    #
    # `"testLedger": false` turns the gap-filling off, and is for the one page shape that
    # does not have the gap: a Tests tab whose own card already lists every test the change
    # set moved — new, edited and deleted alike — which is what the requirements map does.
    # There the ledger is the same rows a second time, grouped by a question the stamps on
    # those rows already answer, and a reader made to hold two lists and diff them is a
    # reader the second list cost something. Off is a claim the author is making, so it is
    # said out loud rather than inferred: the build cannot read a hand-authored fragment
    # and know what is in it.
    if tabs and spec.get("testChanges") and not any(
        b.get("type") == "tests" for tab in tabs for b in tab.get("blocks", [])
    ):
        if spec.get("testLedger") is False:
            print("[review] testLedger:false — no ledger. Every test the change set moved "
                  "has to be listed on the page some other way, deleted ones included.",
                  file=sys.stderr)
        else:
            host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
            if host is None:
                print(f'[review] WARNING: there is a test manifest and no tab carries a '
                      f'"tests" block — and no tab is called {LEDGER_TAB!r} to append it '
                      "to, so what the change set did to the tests is on no page.",
                      file=sys.stderr)
            else:
                host["blocks"] = list(host.get("blocks", [])) + [{"type": "tests"}]

    # The recordings fill their gap the same way, and for the identical reason: the step
    # that harvests them runs whether or not a content file mentions them, and a run that
    # copied eleven traces onto disk for nobody to open has spent the reader's disk and
    # given them nothing. They go *under* the ledger — the ledger is what the branch did,
    # the recordings are what the run did, and that is the order the questions arrive in.
    if tabs and spec.get("playwrightTraces") and not any(
        b.get("type") == "traces" for tab in tabs for b in tab.get("blocks", [])
    ):
        host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
        if host is None:
            print("[review] WARNING: traces were harvested and no tab carries a "
                  f'"traces" block — and no tab is called {LEDGER_TAB!r} to append one '
                  "to, so the recordings are on no page.", file=sys.stderr)
        else:
            host["blocks"] = list(host.get("blocks", [])) + [{"type": "traces"}]

    # Only a tabbed page grows a masthead; the plain single-column guide keeps the
    # heading it always had.
    strip_html = allbtn_html = mode_html = ""
    if tabs:
        # Measured once, for every tab, before the loop: one subprocess and one transcript
        # scan rather than one per tab. `led` is None only when review-cost.py itself
        # could not be asked; a tab's own entry inside it is never missing (see
        # `tab_cost_report`'s docstring) — a bad day comes back as a "not measured"
        # sentence, not as a tab silently getting no number at all.
        led = cost_ledger_report(root, [t["id"] for t in tabs],
                                 (spec.get("pr") or {}).get("base") or "origin/main")
        costs = (led or {}).get("tabs")
        strip, panels, dropped, quiet, emitted = [], [], [], [], []
        for tab in tabs:
            body, weight, changes = "", 0, 0
            auto_badge.clear()
            for block in tab.get("blocks", []):
                chunk, w, c = render_block(block)
                body += chunk
                weight += w
                changes += c
            if not weight and not tab.get("keepEmpty"):
                dropped.append(tab["label"])
                continue
            tid = html.escape(tab["id"])
            # A number on a tab is a promise that it means something. It does on the tab
            # holding the findings; on "Data model" it would just count pictures.
            badge = (tab.get("badge") or auto_badge.get("badge")
                     or (str(weight) if tab.get("count") else ""))
            badge_class = tab.get("badgeClass") or (
                auto_badge.get("class", "") if not tab.get("badge") else "")
            # An alarm is a colour, not a mark: the tab's own label goes red rather than
            # growing a `!` beside it. That leaves the words it stands for with nowhere on
            # screen to live, so they go where a machine still finds them — the button's
            # `aria-label`, which has to restate the label too, because `aria-label`
            # replaces the accessible name rather than adding to it.
            badge_label = tab.get("badgeLabel") or (
                auto_badge.get("label", "") if not tab.get("badge") else "")
            # A class on the pill itself, for a fact about the whole tab rather than about
            # a number on it. `tabClass` in the content file overrides, the same way
            # `badgeClass` does.
            tab_class = tab.get("tabClass") or auto_badge.get("tabClass", "")
            count = (
                f'<span class="n{" " + html.escape(badge_class) if badge_class else ""}"'
                + (f' role="img" aria-label="{html.escape(badge_label)}"'
                   f' data-tip="{html.escape(badge_label[:1].upper() + badge_label[1:])}"'
                   if badge_label else "")
                + f'>{html.escape(badge)}</span>'
            ) if badge else ""
            # Struck through rather than dropped: the answer "we looked, and this branch
            # did not touch it" is worth as much to a reviewer as the answer that it did.
            still = not changes and not tab.get("noStrike")
            if still:
                quiet.append(tab["label"])
            # No `data-tip` on a tab header, on purpose. The strip used to carry two
            # sentences on hover — why a tab is struck through, and what it cost — and both
            # were removed: a hover hint on a pill is unfindable, and the strip is the one
            # part of the page a reviewer navigates by, not reads. Both facts still reach
            # the reader, elsewhere and visibly: the strike-through itself says the branch
            # did not touch that tab, and the cost moved into the breakdown the cost chip
            # opens (`cost_ledger_html`), where every tab's number can be read at once.
            strip.append(
                f'<button type="button" class="tab{" quiet" if still else ""}'
                f'{" " + html.escape(tab_class) if tab_class else ""}" role="tab" '
                f'id="tabbtn-{tid}" aria-controls="{tid}" aria-selected="false" tabindex="-1"'
                + (f' aria-label="{html.escape(tab["label"])} — {html.escape(badge_label)}"'
                   if tab_class and badge_label else "")
                + f'>{html.escape(tab["label"])}{count}</button>'
            )
            # `intro` is prose about the *tab*, not about any one block in it — where the
            # data behind a whole panel came from, or what it deliberately does not say. It
            # is raw HTML and carries no weight: a tab is not kept alive by its own preamble.
            panels.append(
                f'<section class="panel" id="{tid}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{tid}">'
                f'<p class="paneltag">{html.escape(tab["label"])}</p>'
                f'{tab.get("intro", "")}{body}</section>'
            )
            emitted.append(tab)
        # A diagram in the manifest that no tab claimed would vanish without a word —
        # the exact silent drop this pipeline exists to prevent.
        orphans = [r["name"] for r in manifest_rows if r["name"] not in placed]
        if orphans:
            print(f"[review] WARNING: no tab claims these changed diagrams: {', '.join(orphans)}",
                  file=sys.stderr)
        if dropped:
            print(f"[review] dropped empty tabs: {', '.join(dropped)}", file=sys.stderr)
        # The cost tab is the build's own, not the content file's, and it is appended
        # after every declared tab — the last pill on the strip, past CODEOWNERS. Two
        # reasons it cannot be declared: its label is a measured number (`$744`), and this
        # page's whole discipline is that a number nobody can keep up to date is a number
        # that will be wrong; and its position is a fact about the page rather than about
        # any one review — the bill goes at the end, where a bill goes.
        cost_tab_body = cost_ledger_html(led, emitted)
        if cost_tab_body:
            # No decimals. `$744.18` on a tab pill invites reading the cents of a
            # list-price estimate whose error bars are the width of a whole session; `$744`
            # says the size, which is the only thing a label has room to say. The cents are
            # one click away, in the table the tab opens.
            cost_label = f'${(led.get("total") or 0.0):,.0f}'
            strip.append(
                f'<button type="button" class="tab" role="tab" id="tabbtn-{COST_TAB_ID}" '
                f'aria-controls="{COST_TAB_ID}" aria-selected="false" tabindex="-1" '
                f'aria-label="cost — {cost_label} to write and review this change">'
                f'{cost_label}</button>')
            panels.append(
                f'<section class="panel" id="{COST_TAB_ID}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{COST_TAB_ID}">'
                '<p class="paneltag">Cost</p>'
                f'{cost_tab_body}</section>')

        # The strip leaves the body: it belongs to the masthead now, and the masthead is
        # assembled around it below. `body_html` is the panels alone, which is what every
        # rewrite downstream of here (the tab count, the enumeration check) is about.
        strip_html = (
            '<div class="tabstrip" role="tablist" aria-label="Review sections">'
            + "".join(strip) + "</div>"
        )
        # The show-everything toggle is not part of the strip any more (see the CSS): it
        # is emitted at the foot of the page, centred on its own line under the footer's.
        # The label says what it does *next* and therefore has to change with the state,
        # which is what the pressed styling alone could no longer carry once the button
        # left the strip — down here there is nothing beside it to read the highlight
        # against.
        # And which of the two pages this is — served by scripts/serve-review.py, where
        # buttons run and recordings play in the page, or a static copy (a file, the zip,
        # Pages), where they copy their command and hand over a `show-trace` line. Every
        # control on the page already degrades on its own; this is the one place that
        # says which world the reader is in, so it goes in the title row, beside the
        # score: the two things a reader wants before pressing anything are how the
        # branch did and what this copy can do. (It sat in the footer for an evening; a
        # fact nobody scrolls down for is a fact nobody reads.) Emitted as static: the
        # probe in SERVER_JS promotes it, never the other way round.
        # The static badge is a button, and what it copies is the way out of static: one
        # line that starts the server on this directory and opens this page from it.
        # `serve-review.py` prints the URL it ends up serving on — the next free port when
        # :7654 is already serving another checkout — so the line has to *read* that URL
        # rather than assume it, which is what the `$(…)` is. Absolute paths on purpose:
        # this is a fact about the machine the page was built on, like the show-trace
        # command on every trace row, and a reader on another machine has the zip's own
        # README for the general recipe.
        try:
            here = out_dir.resolve().relative_to(root.resolve())
        except ValueError:
            here = out_dir.resolve()
        serve_cmd = (f'cd {shlex.quote(str(root.resolve()))} && u="$('
                     f'{shlex.quote(str(HERE / "serve-review.py"))} {shlex.quote(str(here))}'
                     f' --page {shlex.quote(out_path.name)})" && (open "$u" 2>/dev/null'
                     ' || xdg-open "$u")')
        mode_html = (
            '<button type="button" class="chip chip-mode copycmd" id="hr-mode" '
            f'data-copy="{html.escape(serve_cmd, quote=True)}" '
            'data-tip="A static copy of the page: buttons copy their command instead of '
            'running it, and recordings open natively, not here. Click to copy the line '
            'that serves this directory and opens the page from it.">'
            'static</button>')
        allbtn_html = (
            '<div class="allbar">'
            '<button type="button" class="allbtn" aria-pressed="false" '
            'data-label-off="show single page" data-label-on="back to one tab at a time" '
            'data-tip="Every tab on one page. Makes \u2318F search all of it.">'
            "show single page</button></div>"
        )
        body_html = "\n".join(panels)
        # These two facts used to be appended to the page as a `<p class="sub">` — and the
        # append landed OUTSIDE every `<section class="panel">`, so the only element on the
        # page that no tab could hide sat under all eleven of them, restating a strike-
        # through the strip was already drawing three inches above it. Nothing in the
        # markup is a good home for it: a note about the strip belongs to the strip, and the
        # strip already says it (struck-through label, tooltip on hover; a dropped tab is
        # absent, which is the honest rendering of "nothing to show"). So it is said to the
        # build log, where the person assembling the page is the one who needs it.
        if quiet:
            print("[review] tabs kept as context (struck through, no delta): "
                  + ", ".join(quiet), file=sys.stderr)
        # Filled in from the tabs that survived, not from the tabs that were asked for: a
        # tab dropped for having nothing to show must not be counted in the walk-through
        # that promises the reader eleven of them.
        # The cost tab is deliberately absent from this list. `{{tabcount}}` and the
        # lede's walk-through are about the tabs that carry the review; requiring the
        # summary to also name `$744` would make every content file recite the page's own
        # furniture back at the reader.
        tab_labels = [t["label"] for t in emitted]
        body_html = body_html.replace(TAB_COUNT_TOKEN, spelled(len(tab_labels)))
        # The summary alone, not the whole overview: the walk-through is prose, and the
        # verdict beside it is a score and a label. Handed both, a page that dropped its
        # summary still arrives here with a non-empty string and gets warned that its
        # score dial forgot to name twelve tabs.
        check_tab_enumeration(summary_html, tab_labels)
    else:
        # No tab layout in the content file: the original single-column guide, unchanged.
        body_html = (
            '<h2 id="first">Requires human review</h2>\n'
            + render_findings(spec.get("findings", []))
            + f'\n<h2 id="diagrams">{html.escape(dspec.get("title", ""))}</h2>\n'
            + f'<p>{dspec.get("body", "")}</p>\n'
            + render_diagrams(dspec, root, out_dir)
            + f"\n{city_html}\n"
            + "".join(sections)
        )


    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(spec.get('title', 'Review guide'))}</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<style>{CSS}{extra_css.rstrip()}
{LATE_CSS}{XREF_CSS}</style></head>
<body><div class="wrap">
{masthead_html(spec, mode_html + title_score, chips, strip_html, base_st)}
{lede_html}
{verdict_html}

{body_html}
<footer><div class="footrow"><span>{_link_home(spec.get('footer', ''))}</span>{TAKEAWAY}</div>{allbtn_html}</footer>
</div>
{SERVER_JS}
{CAPTION_JS}
{APP_ENV_JS}
{GENSEQ_JS}
{FOCUS_JS}
{DGM_VIEWS_JS}
{XREF_JS}
{EDITOR_JS}
{FRAME_JS}\n{TRACE_JS}\n{SEQLINK_JS}\n{SEQFOLD_JS}\n{HSCROLL_JS}\n{TABS_JS}
{TIP_JS}
</body></html>
"""
    doc = code_xref.cross_link(doc)
    doc = open_links_in_new_tabs(doc)
    doc = one_tooltip_only(doc)
    check_baked_excerpts(doc)
    out_path.write_text(doc, encoding="utf-8")
    # After the page, so the manifest can never promise a verb for a build that failed to
    # write its own HTML — and every declaration is in by now, the register being filled
    # as the emitters run.
    write_actions(out_dir)
    print(f"[review] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
