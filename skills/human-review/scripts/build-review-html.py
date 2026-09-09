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
EXTRACT = HERE / "extract-snippet.py"
CODEOWNERS = HERE / "codeowners-check.py"
TESTCHANGES = HERE / "test-changes.py"

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
/* The per-tab cost breakdown, hung off the one chip that already states the total.
   A caret, not a hover hint: tab headers deliberately carry no tooltips, and a number
   that only appears when a pointer happens to rest on the right pill is a number nobody
   reads. Closed by default — the subject of this page is the diff, not what measuring it
   cost — and one click from being a table you can scan in a single pass. */
button.chip-cost { font:inherit; font-size:.82rem; cursor:pointer; }
button.chip-cost .caret { display:inline-block; margin-left:.3rem; font-size:.62em;
        opacity:.65; transform:rotate(0deg); transition:transform 120ms ease; }
button.chip-cost:hover { border-color:var(--link); }
button.chip-cost[aria-expanded="true"] { border-color:var(--link); background:var(--accent-soft); }
button.chip-cost[aria-expanded="true"] .caret { transform:rotate(90deg); }
/* `order` rather than markup position: the panel is emitted right after its own chip so
   the two travel together, but a chip authored *after* the cost chip must not be shoved
   onto a second line by a full-width block landing between them. */
.costbreak { order:2; flex:1 0 100%; margin:.35rem 0 0; background:var(--card);
             border:1px solid var(--line); border-radius:8px; padding:.75rem .95rem; }
.costbreak[hidden] { display:none; }
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
   the scope bar started stating `±` counts beside `+` and `−` — a number left the colour
   of the text beside two coloured ones reads as a different KIND of number, not as the
   third member of a set. */
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
.cmlegend span { display:inline-flex; align-items:center; gap:.4rem; }
.cmlegend i { width:1.1rem; height:0; border-top:3px solid currentColor;
              border-radius:2px; flex:none; }
.cmlegend .new { color:var(--dgm-diff-add); }
.cmlegend .todo { color:#d7263d; }
.cmlegend b { color:var(--fg); font-weight:600; }
@media (prefers-color-scheme:dark) { .cmlegend .todo { color:#ff9090; } }
/* "Open it in draw.io": under the drawing, never on it. The invitation used to be
   painted into the picture itself, which put a sentence about tooling on top of the map
   and made the reader read it again on every look. In HTML it is a link — it looks like
   one, the cursor says so, and it stays out of the diagram's way. */
.dgm-open { margin:.35rem .6rem .1rem; font-size:.78rem; color:var(--muted); }
.dgm-open a { color:inherit; text-decoration:underline; text-underline-offset:2px; }
.dgm-open a:hover { color:var(--fg); }
/* The command that re-draws the picture above. It sits under the diagram rather than in
   a README because the reader who needs it is the reader who has just been told, inside
   the picture, to go and re-lay the thing out by hand — and a rebuild step they have to
   go and look up is a rebuild step that does not happen. One line, selectable, with the
   button that puts it on the clipboard. */
.rerun { margin:.6rem .6rem .1rem; font-size:.78rem; color:var(--muted); line-height:1.6; }
.rerun .cmdline { display:flex; align-items:flex-start; gap:.5rem; margin-top:.35rem; }
.rerun code { flex:1; min-width:0; overflow-x:auto; white-space:pre; display:block;
              background:var(--code-bg); border:1px solid var(--rule); border-radius:5px;
              padding:.4rem .55rem; font-size:.94em; }
.rerun button { flex:none; cursor:pointer; font:inherit; color:var(--muted);
                background:var(--code-bg); border:1px solid var(--rule); border-radius:5px;
                padding:.4rem .6rem; }
.rerun button:hover { color:var(--fg); border-color:var(--muted); }
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
.genseq-hint { margin:.45rem 0 0; color:var(--muted); font-size:.82rem; }
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
/* A badge that says something is *wrong* cannot look like a count. This one is worn by
   the tab the reviewer must not skip — a blocked merge — so it keeps its colour even
   while the tab is selected, where the strip inverts everything else. */
button.tab .n.alarm { background:#c62828; color:#fff; opacity:1; border-radius:50%;
                      flex:0 0 auto; width:.9rem; height:.9rem; padding:0;
                      display:inline-flex; align-items:center; justify-content:center;
                      letter-spacing:0; text-indent:.02em; }
button.tab[aria-selected="true"] .n.alarm { background:#fdeaea; color:#8a1c1c; }
/* A verdict the strip can carry without words: green nothing changed, amber changed
   but nothing breaks, red a caller breaks. A number there ("+3") counted changes,
   which is not the question anyone opens that tab with. */
button.tab .n.dot-green, button.tab .n.dot-amber, button.tab .n.dot-red {
  width:9px; height:9px; border-radius:50%; opacity:1; font-size:0; padding:0;
  display:inline-block; vertical-align:middle; }
button.tab .n.dot-green { background:#2e9e5b; }
button.tab .n.dot-amber { background:#d98218; }
button.tab .n.dot-red   { background:#d7263d; }
button.tab .sev { width:6px; height:6px; border-radius:50%; background:var(--accent); }
/* This used to be the last pill on the tab strip, where it sat in the corner of every
   screenful for the whole read and was pressed roughly never — a permanent control for an
   occasional act. It lives at the foot of the page now, which is where you arrive having
   finished reading and is the moment the thing it offers ("show me all of it at once, so
   ⌘F works") is actually worth wanting.
   On the footer's own line, not under it: a button alone on the last line of the page
   reads as the page's conclusion, which it is not — it is a control, and the far end of
   the line the footer already occupies is where a page puts one. `margin-left:auto` does
   the aligning, so the sentence keeps its natural width and the button keeps the right
   edge at every width; `flex-wrap` drops it under the sentence on a narrow screen rather
   than squeezing either. */
footer .footrow { display:flex; align-items:baseline; gap:.6rem 1.2rem; flex-wrap:wrap; }
footer .allbar { margin-left:auto; }
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
   evidence for the test directly above it. One ruled edge holds the pair together. */
.testpair { border-left:2px solid var(--line); padding-left:1rem; margin:1.5rem 0 2.4rem; }
.testpair > .snippet, .testpair > .diagram { margin-top:.7rem; margin-bottom:0; }
.testlead { margin:0; }
.testlead b { display:block; font-size:1.02rem; margin:.5rem 0 .25rem; }
.testlead b:first-child { margin-top:0; }
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
      if (ev.target.closest && ev.target.closest('a')) return;
      video.currentTime = parseFloat(li.dataset.t);
      video.play();
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
    var pair = views.querySelector('.dgm-newold');
    if (pair) {
      pair.querySelectorAll('u[data-view]').forEach(function (word) {
        word.classList.toggle('on', word.getAttribute('data-view') === state);
      });
    }
    views.querySelectorAll('.dgmbar button[data-go]').forEach(function (b) {
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
      var views = button.closest('.dgmviews');
      if (button.getAttribute('data-go') === 'diff') show(views, 'diff');
      else flip(views);
      return;
    }
    // The header, but never a link inside it: the source path opens an editor.
    var head = ev.target.closest('.diagram.dgm-toggles > .head');
    if (head && !ev.target.closest('a')) flip(head.parentElement.querySelector('.dgmviews'));
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


COST_JS = """<script>
// The aggregate cost chip is a disclosure button: it opens the per-tab breakdown that
// sits directly after it in the scope bar. Deliberately not a tooltip — the tab strip
// carries no hover hints by design, and a decomposition is something you scan, not
// something you discover one pill at a time. Escape closes it, like every other
// transient surface on this page.
(function () {
  var btn = document.querySelector('button.chip-cost');
  var panel = btn && document.getElementById(btn.getAttribute('aria-controls'));
  if (!btn || !panel) return;                 // no breakdown was emitted: nothing to open
  function set(open) {
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    panel.hidden = !open;
  }
  btn.addEventListener('click', function () {
    set(btn.getAttribute('aria-expanded') !== 'true');
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && btn.getAttribute('aria-expanded') === 'true') set(false);
  });
})();
</script>"""


FRAME_JS = """<script>
// A framed report sizes itself: it posts its height and we grow the frame to fit, so
// the page keeps the only scrollbar. A frame that scrolls internally traps the wheel
// and hides how much of it is left.
window.addEventListener('message', function (e) {
  var d = e.data;
  if (!d || d.type !== 'dv-height' || !d.height) return;
  Array.prototype.forEach.call(document.querySelectorAll('iframe'), function (f) {
    if (f.contentWindow === e.source) f.style.height = (d.height + 4) + 'px';
  });
});
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
  var SERVED = location.protocol === 'http:' || location.protocol === 'https:';

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
  function flash(message) {
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'copy-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('shown');
    clearTimeout(flash.timer);
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
  document.addEventListener('click', function (ev) {
    var cmd = ev.target.closest && ev.target.closest('button.copycmd');
    if (!cmd) return;
    copy(cmd.getAttribute('data-copy') || '')
      .then(function () { flash('Copied \u2014 run it in a terminal, then reload this page'); });
  });

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
      fetch('/__open_diff__?path=' + encodeURIComponent(dpath) + '&base=' + encodeURIComponent(base))
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

  if (!EMBEDDED || SERVED) return;
  document.addEventListener('DOMContentLoaded', function () {
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
  // Where ⌘-click goes from the panel: the source of whichever diagram is open.
  var source = null;

  // The test a diagram was generated from — already computed at build time and sitting
  // under the picture as `generated by <test>`, so there is nothing to resolve here and
  // nothing that can disagree with the link a reader can see. Falls back to the .puml
  // when the test file could not be found (the second provenance link is always there).
  function sourceOf(diagram) {
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

  // ⌘ on a Mac, Ctrl elsewhere — the same chord that opens a link in a new tab, which is
  // the habit this borrows: the arrow is a reference, and this follows it.
  function wantsSource(ev) {
    return ev.metaKey || ev.ctrlKey;
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
    els.title.addEventListener('click', function (ev) {
      if (wantsSource(ev)) { ev.preventDefault(); openSource(source); }
    });
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

  function show(entry, index, target, href) {
    build();
    source = href;
    els.title.style.cursor = href ? 'pointer' : '';
    // No tooltip on the title: the cursor already says it is clickable, and a hint that
    // pops over the heading you are reading costs more than it explains.
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
    var revealable = 0;

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
      revealable++;

      var index = -1;
      var state = {reset: function () { index = -1; group.classList.remove('genseq-open'); }};
      group.classList.add('genseq-hot');
      addHitArea(group);
      group.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // ⌘-click reads the arrow as a reference to the code rather than as something to
        // expand: a reviewer who wants the SQL clicks, and one who wants the test that
        // caused it holds ⌘. Checked before anything else, so the panel neither opens
        // nor advances its step counter on the way out.
        if (wantsSource(ev)) { openSource(href); return; }
        if (current && current !== state) current.reset();
        index++;
        if (index >= entry.steps.length) { close(); return; }
        current = state;
        group.classList.add('genseq-open');
        show(entry, index, link, href);
      });
    });

    if (!revealable) return;
    var hint = document.createElement('p');
    hint.className = 'genseq-hint';
    hint.textContent = 'Simplified on purpose — click any arrow marked \u2295 to reveal its SQL '
      + 'or its JSON payload. Switching a statement to its bound values switches them all. '
      + 'Click a section header to open its scenario in the test; \u2318-click an arrow '
      + '(Ctrl elsewhere) to open the test file.';
    // Above the whole viewer where there is one, not above the first .svgbox — that one
    // lives inside the Diff pane, so the instructions vanished on New and Old.
    (diagram.querySelector('.dgmviews') || diagram.querySelector('.svgbox'))
      .insertAdjacentElement('beforebegin', hint);
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


def diff_link_html(rel: str, base: str, root: Path, face: str | None = None) -> str:
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


def _snippet_links(rel: str, root: Path) -> str:
    """The VS Code and github.com handles for a quoted block, against the review's own base.

    Both are optional and for the same reason: each is emitted only where that side can
    really open what it promises. `diff_link_html` drops itself when the base does not
    resolve, when the file did not exist in it, or when the two sides are identical;
    `_github_compare_link` drops itself when the file is dirty in the working tree, which
    is precisely when github.com has never seen what the snippet is showing. A bar with
    one handle, or none, is the honest rendering — a dead button is worse than no button.

    `origin/` is stripped for github.com only: it is a name for a ref in *this* checkout,
    and a compare URL spelling it 404s."""
    vsc = diff_link_html(rel, SNIPPET_BASE, root, face=_icon("VSC"))
    gh = _github_compare_link(rel, SNIPPET_BASE.removeprefix("origin/"), root,
                              face=_icon("GH"))
    return vsc + gh


def snippet_html(ref: str, caption: str | None, root: Path, exact: bool = False,
                 link_at: tuple[int, int] | None = None) -> str:
    rel = ref.rsplit(":", 1)[0] if ":" in ref else ref
    return _extract_module().render(ref, caption, root, exact,
                                    links=_snippet_links(rel, root), link_at=link_at)


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
SVG_TITLE = re.compile(r"(<title>)(.*?)(</title>)", re.S | re.I)


def _plain_svg_title(svg: str) -> str:
    return SVG_TITLE.sub(lambda m: m[1] + CREOLE_IN_TITLE.sub("", m[2]).strip() + m[3], svg)


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

    `initial` is which of the three the widget opens on. It is "diff" for a diagram,
    where the delta is a clean two-colour drawing and the whole point of the section.
    The UX audit passes "new": its delta is a pixel mask over a screenshot, and a
    half-ghosted photo of a form is a picture a reader has to decode before it says
    anything. The annotated *new* screen is the one that reads at a glance — the mask
    stays one click away, for when the question is what moved.

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
    a New/Old pair exists — the header only advertises itself as a toggle when it does."""
    panes = [("diff", _focus_views(row, assets, full_svg, root))]
    for view, column in (("new", "new_svg"), ("old", "old_svg")):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'))
    if len(panes) == 1:
        return panes[0][1], False
    return dgm_views_html(panes), True


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
CM_LEGEND_NEW = '<span class="new"><i></i><b>added by this PR</b></span>'
CM_LEGEND_TODO = ('<span class="todo"><i></i><b>still waiting for a hand-drawn layout</b> '
                  "— drawn by automation to keep the guardrail green</span>")


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
            + drawio_open_html(verdict.get("drawio_url") or "",
                               verdict.get("diagram") or "")
            + rerun_html(verdict.get("rerun"), rebuild))


def drawio_open_html(url: str, diagram: str = "") -> str:
    """The one click the drawing asks for, rendered as a link under it.

    It is here and not inside the SVG on purpose: a rendered diagram cannot show a
    cursor, so an invitation painted into the picture has to spell out that it is
    clickable — and then it is a sentence about tooling sitting on the map, re-read
    every time the reader looks at the boxes. Under the picture it is just a link.
    """
    if not url:
        return ""
    what = html.escape(Path(diagram).name) if diagram else "the diagram"
    return (f'<p class="dgm-open"><a href="{html.escape(url, quote=True)}">'
            f'Open {what} in draw.io ↗</a></p>')


def rerun_html(rerun: dict | None, rebuild: str) -> str:
    """The command that re-renders this diagram and rebuilds this page, ready to paste.

    Not a convenience. The picture above is inlined into the HTML, and it has to be: the
    boxes are links into the classes they name and the to-do note is a link into draw.io,
    and an SVG loaded through `<img src>` renders those as decoration — the reader can see
    them and cannot click them. So the file on disk and the picture in the page are two
    artefacts, and reloading the browser only ever refreshes the second one. That is a
    thing the page owes the reader an answer to, at the moment they need it, in the form
    of something they can run — not a paragraph explaining that they are out of luck.

    `rerun` is what `drawio-diff.py` recorded about its own invocation; `rebuild` is how
    this build was started. Neither is reconstructed here — a guessed command that does
    not work is worse than no command, because it is tried first.
    """
    if not rerun or not rerun.get("command"):
        return ""
    line = f'cd {shlex.quote(rerun["cwd"])} \\\n  && {rerun["command"]} \\\n  && {rebuild}'
    return ('<div class="rerun">Re-drawn it in draw.io? This picture is inlined into the '
            'page at build time, so reloading cannot pick it up — run this, then reload:'
            f'<div class="cmdline"><code>{html.escape(line)}</code>'
            f'<button type="button" class="copycmd" data-copy="{html.escape(line, quote=True)}" '
            'data-tip="Copy the command">Copy</button></div></div>')


def expand_drawio(text: str, out_dir: Path, root: Path, rebuild: str) -> str:
    return DRAWIO_TOKEN.sub(
        lambda m: drawio_widget_html(m["name"], out_dir / "assets", root, rebuild), text)


def _pretty(name: str) -> str:
    """`DomainModel` is a filename; `Domain Model` is a heading. Split the camel hump,
    which leaves acronyms (DB) and already-spaced names untouched."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _source_link(rel: str, root: Path) -> str:
    """The path already shown on the right of the header, made the link to the file.

    It used to be plain text with a second `<a>name.puml</a>` under the title — two
    controls for one destination, and the shorter of the two said less."""
    if (root / rel).is_file():
        return (f'<a class="dgm-src" href="vscode://file/{(root / rel).resolve()}:1:1">'
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


# `== <creole> [[src://<rel>:<line>{hint} <title>]] <creole> ==` — a chapter divider in a
# generated sequence diagram. The generator writes one per scenario, carrying the test file
# and the line the scenario starts at, so the diagram already knows which test produced
# which stretch of itself. The delta .puml colours and strikes these; the committed .puml
# next to the test does not, which is why the titles are read from the committed one.
CHAPTER = re.compile(
    r"^==.*?\[\[src://(?P<path>[^\s:\]]+):(?P<line>\d+)(?:\{[^}]*\})?\s+(?P<title>[^\]]*)\]\]"
)


def chapters(puml: Path):
    """The scenarios a generated sequence diagram is made of, in the order it draws them."""
    if not puml.is_file():
        return []
    found = []
    for line in puml.read_text(encoding="utf-8").splitlines():
        m = CHAPTER.match(line.strip())
        if m:
            found.append((m["path"], int(m["line"]), m["title"].strip()))
    return found


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


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path):
    """Each acceptance test next to the sequence its own run recorded.

    They used to be two lists on the same tab — a gallery of diagrams, then a list of test
    snippets — and the reader had to work out which picture belonged to which test from the
    file names. Nothing was hidden and nothing was reliable. The pairing is not a judgement
    call, either: the manifest says which test file each diagram came from, and the
    diagram's own chapter titles say which scenarios inside that file, at which lines. So it
    is derived, not authored.

    Neither side is ever dropped, and neither is ever given a partner it did not produce.
    A test with no diagram goes to a trailing group that says exactly that — the more
    interesting of the two absences, because a tagged test with no recorded trace is a
    fact about the *evidence* rather than a gap in the page. A diagram whose test is not
    quoted, or not in this checkout at all, says so under its own heading rather than
    sitting there looking like it came from nowhere."""
    rows = [r for r in select_rows(manifest_rows, block) if r["kind"] == "sequence"]
    snippets = list(block.get("snippets", []))
    parts, used = [], set()

    def take(test_rel):
        """The snippets that quote this test file, removed from the pool."""
        mine = [x for x in snippets if x["ref"].rpartition(":")[0] == test_rel]
        used.update(id(x) for x in mine)
        return [snippet_html(x["ref"], x.get("caption"), root) for x in mine]

    merged = dict(dspec)
    merged.pop("only", None)
    for r in rows:
        test_rel = r["source"][: -len(".genseq.puml")] if r["source"].endswith(".genseq.puml") \
            else r["source"]
        lead = "".join(
            f'<b>{html.escape(title)}</b>'
            f'<a class="srcref" href="vscode://file/{(root / path).resolve()}:{line}:1" '
            f'data-tip="Open in VS Code">{html.escape(path)}:{line}</a>'
            for path, line, title in chapters(root / r["source"])
        )
        pieces = ([f'<p class="testlead">{lead}</p>'] if lead else [""])
        quoted = take(test_rel)
        pieces += quoted or [_unquoted_note(test_rel, root)]
        pieces.append(render_diagrams(merged, root, out_dir, [r]))
        # Each piece already ends its own last tag; extract-snippet also ends with a
        # newline, and joining on one more turns the ruled block into a gappy list.
        parts.append('<div class="testpair">'
                     + "\n".join(x.strip("\n") for x in pieces) + "</div>")

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
    head = (f'<h3 id="{html.escape(block.get("id", "sequences"))}">'
            f'{html.escape(block.get("title", "Sequence deltas"))}</h3>'
            + (f'<p>{block["body"]}</p>' if block.get("body") else ""))
    return "\n".join([head] + parts) + "\n", len(rows) + len(orphaned), len(rows)


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


def render_diagrams(spec, root: Path, out_dir: Path, rows=None) -> str:
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
        parts.append(
            f'<div class="diagram{" dgm-toggles" if toggles else ""}">'
            f'<div class="head"><b>{html.escape(_pretty(r["name"]))}</b>'
            # A badge earns its place by saying something surprising. "modified" is what
            # a diagram in a delta gallery always is, and "structural" is legible from the
            # picture — so only the states that carry information get one.
            + (f'<span class="badge {"sev-high" if r["status"] == "added" else "sev-low"}">'
               f'{html.escape(r["status"])}</span>' if r["status"] != "modified" else "")
            + _source_link(r["source"], root) + '</div>'
            + (f"<p>{note}</p>" if note else "")
            + _provenance(r["source"], root)
            + genseq_details(r["source"], root)
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
    if block is not None:
        # Zero is a number the reader came for, so this clause renders at zero too. A pile
        # that appears only when it is non-empty disappears exactly where it matters most:
        # "the page says nothing about what the coder guessed at" and "the coder was asked
        # and guessed at nothing" are the same blank line, and only one of them is good
        # news. Mode C is the case where a zero would be the lie instead — nobody was in a
        # position to be asked — so it says that rather than counting an empty pile.
        assumed = len(spec.get("assumptions", []))
        parts.append("coder could not be asked"
                     if block.get("mode") == "C" and not assumed
                     else f"{assumed} assumed by the coder")
    if spec.get("findings"):
        parts.append(f"{len(spec['findings'])} open, worst first")
    if spec.get("autofixes"):
        parts.append(f"{len(spec['autofixes'])} auto-applied")
    if not parts:
        return ""
    # The stamp clause went the same way as "greyed out" and "yours to confirm", and it
    # was the last of them: every item carries its source beside its own title, so a line
    # announcing that they do describes the thing directly under it. What is left is
    # counts and one ordering fact — the two things counting the list yourself would not
    # have told you.
    return '<p class="sub counts">' + " &middot; ".join(parts) + "</p>"


def _lede_into(head: str, lede: str) -> str:
    """Between the heading and the block's own prose, not after it.

    The counts are what the heading is asking about, and anything authored here is a
    footnote to them. Appended after the body they read as an afterthought to a sentence
    nobody needed."""
    if not lede:
        return head
    at = head.find("<p>")
    return head[:at] + lede + head[at:] if at != -1 else head + lede


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
    if not src.is_file():
        return f'<p class="sub">no diagram at <code>{html.escape(block["src"])}</code></p>'
    cache = out_dir / "assets" / (Path(block["src"]).stem + ".context.svg")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        out = subprocess.run(["plantuml", "-tsvg", "-pipe"],
                             input=src.read_bytes(), capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return (f'<p class="sub">plantuml could not render '
                    f'<code>{html.escape(block["src"])}</code> \u2014 is it installed?</p>')
        cache.write_bytes(out.stdout)
    return (
        '<div class="diagram">'
        f'<div class="head"><b>{html.escape(block.get("name", src.stem))}</b>'
        f'<span class="badge sev-info">{html.escape(block.get("status", "unchanged"))}</span>'
        f'<span>{html.escape(block["src"])}</span></div>'
        + (f'<p>{block["note"]}</p>' if block.get("note") else "")
        + _provenance(block["src"], root)
        + f'<div class="svgbox">{inline_svg(cache, root)}</div></div>'
    )


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


def _link_captions(cues, links):
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
            cells[i] = (cell[:at] + f'<a href="{html.escape(href)}">' + phrase + "</a>"
                        + cell[at + len(phrase):])
            break
        else:
            unplaced.append(link)
    items = "".join(
        f'<li data-t="{c["t"]:.2f}"><span class="ts">{int(c["t"]) // 60}:'
        f'{int(c["t"]) % 60:02d}</span><span>{cell}</span></li>'
        for c, cell in zip(cues, cells)
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
    items, unplaced = _link_captions(cues, s.get("appLinks", []))
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
                  + " · ".join(f'<a href="{html.escape(l["href"])}">'
                               f'{html.escape(l.get("label") or l["href"])}</a>'
                               for l in unplaced) + ".</span></li>")
    return f'<div class="vidwrap">{player}<ol class="transcript">{items}</ol></div>'


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
    call = call or _call_privacy_model
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
    # `±` for the edited ones, beside `+` and `−`, because the scope bar is read as a row
    # of signed numbers and a third sign is read in the same glance a word is not. It also
    # retires the `~` that used to be typed for the same thing one chip to the left: a
    # tilde is an approximation, and "about forty files changed" is not what was meant.
    edited = f'<span class="changed">±{t["modified"]}</span>' if t["modified"] else ""
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
        parts.append(f"Compared against {state['ref']} ({state['sha'][:8]}); local "
                     f"{state['localRef']} is {behind} behind it. git fetch.")
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


def diffstat_chips(root: Path, state: dict | None, extra: list[str] | None) -> list[dict]:
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
    # The signs are the page's, not this chip's: `+` added, `-` removed, `±` changed, and
    # a zero is dropped rather than printed. A row of chips is read as a row of signed
    # numbers, and `-0` is noise that costs a glance to dismiss.
    files_value = " / ".join(piece for piece in (
        f'<span class="added">+{a}</span>' if a else "",
        f'<span class="removed">−{d}</span>' if d else "",
        f'<span class="changed">±{e}</span>' if e else "",
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

    return [
        {"label": "files",
         "value": files_value,
         "tip": f"{a} added, {e} edited, {d} deleted {where}.{skipped}"},
        {"label": "lines",
         "value": lines_value,
         "tip": f"+{adds} / −{dels} {where}.{skipped}"},
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


# The chip's place in the scope bar, held open until the tabs are known. Whether the chip
# is an inert pill or a button that opens a breakdown depends on a measurement that has not
# run yet when the bar is built (it needs the final, post-drop tab list), so the bar keeps
# the slot and the chip is rendered into it further down.
COST_CHIP_TOKEN = "{{costchip}}"
COST_PANEL_ID = "cost-breakdown"

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
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def cost_breakdown_html(costs: dict | None, tabs: list[dict]) -> str:
    """The per-tab ledger, as a panel the aggregate cost chip opens.

    This measurement spent a while with no surface at all. It was born as a `data-tip` on
    each tab header; when tab-header tooltips were removed the emission went with them, so
    the subprocess kept running on every build and its answer reached nobody — the exact
    silent-nothing this pipeline pins with tests everywhere else. A tooltip was the wrong
    home anyway. A per-tab number is something a reader wants to *scan* — all rows at once,
    ordered, adding up — not to discover one pill at a time by pointing at it, and a hover
    hint is invisible to anyone who never happens to hover. So it hangs off the chip that
    already states the total, which is the only place on the page that raises the question
    "and where did that go?" in the first place.

    Three shapes of row, because there are three honest answers:
      * a tab with measured spend gets its own row, biggest first;
      * every measured-zero tab collapses into ONE muted row that names them all — a script
        wrote that tab, so zero is the true answer, but ten of those stacked above the three
        rows that carry the actual money would bury the point;
      * every unmeasured tab collapses the same way, carrying the reason in words, because
        "we could not measure this" must never render identically to a measured zero.
    Returns "" only when there is nothing at all to say (no report, no tabs).
    """
    if not costs or not tabs:
        return ""
    rows = costs.get("tabs") or {}
    entries = []
    for tab in tabs:
        row = rows.get(tab.get("id"))
        if row:
            entries.append((tab.get("label") or tab.get("id"), row))
    if not entries:
        return ""

    def spend(row):
        return row.get("cost") or 0.0

    def toks(row):
        return row.get("tokens") or 0

    measured = [e for e in entries if e[1].get("measured")]
    paid = sorted([e for e in measured if spend(e[1]) or toks(e[1])],
                  key=lambda e: -spend(e[1]))
    free = [e for e in measured if not (spend(e[1]) or toks(e[1]))]
    unknown = [e for e in entries if not e[1].get("measured")]

    def names(items):
        return ", ".join(html.escape(str(l)) for l, _ in items)

    body = "".join(
        f'<tr><td>{html.escape(str(l))}</td><td>{_cost_tokens(toks(r))}</td>'
        f'<td>{_cost_money(spend(r))}</td></tr>'
        for l, r in paid
    )
    if free:
        body += (
            f'<tr class="costquiet"><td>{len(free)} tab'
            f'{"s" if len(free) != 1 else ""} with no model spend — {names(free)}</td>'
            f'<td>0</td><td>$0.00</td></tr>'
        )
    if unknown:
        why = costs.get("reason") or "no step in the ledger named them"
        body += (
            f'<tr class="costquiet"><td>{len(unknown)} tab'
            f'{"s" if len(unknown) != 1 else ""} not measured — {html.escape(str(why))}'
            f' ({names(unknown)})</td><td>—</td><td>—</td></tr>'
        )

    total_cost = sum(spend(r) for _, r in entries)
    total_toks = sum(toks(r) for _, r in entries)
    foot = ""
    resid = costs.get("residual") or {}
    if resid.get("measured"):
        total_cost += resid.get("cost") or 0.0
        total_toks += resid.get("tokens") or 0
        # One undifferentiated "not one tab's" row routinely carried 90%+ of the bill, which
        # does not read as a caveat — it reads as an instruction to ignore the rows above it.
        # Where the report can name the parts, name them: the largest is Step 9 writing the
        # page, which is a real answer, not a leftover.
        parts = costs.get("residual_parts") or {}
        shown = [(label, parts[key]) for key, label in RESIDUAL_ROWS
                 if (parts.get(key) or {}).get("messages")]
        if shown:
            foot += "".join(
                f'<tr class="costquiet"><td>{label}</td>'
                f'<td>{_cost_tokens(part.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(part.get("cost") or 0.0)}</td></tr>'
                for label, part in shown
            )
        else:
            foot += (
                '<tr class="costquiet"><td>not one tab\'s — assembling the guide itself, plus '
                'any step whose window did not cover it</td>'
                f'<td>{_cost_tokens(resid.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(resid.get("cost") or 0.0)}</td></tr>'
            )
    # No total on a run that measured nothing. A `$0.00 total` sitting under a chip that
    # says $308.64 does not read as "unmeasured", it reads as "wrong" — and the row above
    # has already said, in words, why there is no number to add up.
    if paid or free or resid.get("measured"):
        foot += (f'<tr class="costtotal"><td>total</td><td>{_cost_tokens(total_toks)}</td>'
                 f'<td>{_cost_money(total_cost)}</td></tr>')

    return (
        f'<div class="costbreak" id="{COST_PANEL_ID}" hidden>'
        '<table class="costtab">'
        '<caption>Which steps burned model time — every turn charged to whichever step was '
        'running when it happened, at list price. A tab a script produced costs nothing to '
        'produce, and says so.</caption>'
        '<thead><tr><th scope="col">tab</th><th scope="col">tokens</th>'
        '<th scope="col">cost</th></tr></thead>'
        f'<tbody>{body}</tbody><tfoot>{foot}</tfoot></table></div>'
    )


def cost_chip_html(c: dict, panel: str) -> str:
    """The aggregate cost chip — a plain pill on its own, a disclosure button once there
    is a breakdown behind it. The caret is the whole point: the chip has to *look* like it
    opens something, because nothing else on the page announces that the number decomposes.
    """
    tip = c.get("tip") or ""
    inner = f'{html.escape(c["label"])} <b>{c["value"]}</b>'
    if not panel:
        if tip:
            inner = f'<span data-tip="{html.escape(tip)}">{inner}</span>'
        return f'<span class="chip">{inner}</span>'
    tip = f"{tip} Click to break it down per tab." if tip else "The cost, tab by tab."
    return (
        f'<button type="button" class="chip chip-cost" aria-expanded="false" '
        f'aria-controls="{COST_PANEL_ID}" data-tip="{html.escape(tip)}">{inner}'
        f'<span class="caret" aria-hidden="true">▸</span></button>{panel}'
    )


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
INVITATION = "Tell your agent to clone and port this to your environment and needs."


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
    `files +1 / ±40` reads as an unlabelled number. The label is what makes the pair
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
    args = ap.parse_args(argv)

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    spec = json.loads(Path(args.content).read_text(encoding="utf-8"))
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
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
        body = expand_drawio(expand_snippets(s.get("body", ""), root), out_dir, root,
                             rebuild_cmd)
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
        city_html = (
            f'<h2 id="codecity">{html.escape(city.get("title", "Where it landed in the city"))}</h2>\n'
            f'<p>{city.get("body", "")}</p>\n'
            f'<a class="city" href="{html.escape(city["href"])}" target="_blank" rel="noopener"'
            f' data-tip="Open the interactive Code City in a new tab">'
            f'<img src="{html.escape(city["png"])}" alt="Code City with the branch change set highlighted"></a>\n'
            f'<p class="sub">{city.get("caption", "")}</p>'
        )

    # Chips carry HTML on purpose: a chip is often a link (to the branch on GitHub, to a
    # section further down) or coloured (+added / -removed), and escaping would kill both.
    chips = []
    cost_scope_chip = None      # resolved here, rendered once the tab list is final
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
        inner = f'{html.escape(c["label"])} <b>{c["value"]}</b>'
        if c.get("tip"):
            inner = f'<span data-tip="{html.escape(c["tip"])}">{inner}</span>'
        if c.get("href"):
            chips.append(
                f'<a class="chip chip-link" href="{html.escape(c["href"])}"'
                f'{" target=_blank" if c["href"].startswith("http") else ""}>{inner}</a>'
            )
        else:
            chips.append(f'<span class="chip">{inner}</span>')

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
                "label": f"{reviewer} review" if reviewer else "LLM review",
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
                "value": f'{total - fixed} open &middot; '
                         f'<span class="sub">{fixed} autofixed</span>',
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
            for computed in diffstat_chips(root, base_st, c.get("exclude")):
                emit({**computed, **{k: v for k, v in c.items()
                                     if k not in ("auto", "exclude")}})
            continue

        if c.get("auto") == "tests":
            computed = tests_chip(test_doc)
            if computed is None:
                continue
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}

        if c.get("auto") == "cost":
            computed = cost_chip(root)
            if computed is None:
                continue
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}
            # Dollars are the number a reader acts on; the token count is the one they
            # ask for second. The script hands both over with the tokens already wrapped
            # in a <span class="sub">, so lift that span out rather than splitting on the
            # separator inside it.
            m = re.search(r'\s*<span class="sub">(.*?)</span>\s*', str(c["value"]))
            if m:
                tokens = re.sub(r"^[\s·]+", "", m.group(1)).strip()
                c["value"] = str(c["value"])[:m.start()].strip()
                c["tip"] = f'{tokens} — {c["tip"]}' if c.get("tip") else tokens
            c["label"] = c["label"].replace("this review cost", "review cost")
            # Held, not rendered: the chip becomes a button that opens the per-tab
            # breakdown, and whether there is a breakdown to open is only known after the
            # tab list has been built and its empty tabs dropped.
            cost_scope_chip = c
            chips.append(COST_CHIP_TOKEN)
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
            head = _lede_into(heading(block, "first", "Requires human review"), opening_lede(spec))
            return (head + render_findings(items), len(items), len(items))
        if kind == "assumptions":
            items = spec.get("assumptions", [])
            mode = block.get("mode", "")
            head = _lede_into(heading(block, "assumed", "Decided without asking you"),
                              opening_lede(spec))
            # Weight 1 even with nothing in it: an empty pile still carries the sentence
            # saying *which* kind of empty it is, and that sentence is the point.
            return (head + render_assumptions(items, mode),
                    1 if (items or mode) else 0, len(items))
        if kind == "autofixes":
            items = spec.get("autofixes", [])
            head = _lede_into(heading(block, "fixed", "Auto-fixed"),
                              opening_lede(spec))
            return (head + render_autofixes(items), len(items), len(items))
        if kind == "diagrams":
            rows = select_rows(manifest_rows, block)
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
                auto_badge["badge"], auto_badge["class"] = "!", "alarm"
                auto_badge["label"] = "approval required"
            return (heading(block, "codeowners", block.get("title", "Code owners")) + frag,
                    1, len(owned))
        if kind == "tests":
            frag, moved = render_test_ledger(test_doc.get("tests", []), root)
            placed_ledger.append(True)
            if not frag:
                return "", 0, 0
            return (heading(block, "test-ledger",
                            block.get("title", "What this change set did to the tests"))
                    + frag, 1, moved)
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
    cost_panel_html = ""    # stays empty for the tabless single-column layout
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

    # Only a tabbed page grows a masthead; the plain single-column guide keeps the
    # heading it always had.
    strip_html = allbtn_html = ""
    if tabs:
        # Measured once, for every tab, before the loop: one subprocess and one transcript
        # scan rather than one per tab. `costs` is None only when review-cost.py itself
        # could not be asked; a tab's own entry inside it is never missing (see
        # `tab_cost_report`'s docstring) — a bad day comes back as a "not measured"
        # sentence, not as a tab silently getting no tooltip at all.
        costs = tab_cost_report(root, [t["id"] for t in tabs])
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
            # An alarm is a mark, not a word: it has to survive being read at the width of a
            # tab pill, so it is a single glyph in a red circle. The words it stands for are
            # not dropped, they move to where a machine and a pointer can still find them —
            # `aria-label`, which becomes part of the tab button's accessible name ("Code
            # owners approval required"), and `data-tip`, which is the page's own tooltip.
            badge_label = tab.get("badgeLabel") or (
                auto_badge.get("label", "") if not tab.get("badge") else "")
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
            # opens (`cost_breakdown_html`), where every tab's number can be read at once.
            strip.append(
                f'<button type="button" class="tab{" quiet" if still else ""}" role="tab" '
                f'id="tabbtn-{tid}" aria-controls="{tid}" aria-selected="false" tabindex="-1"'
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
        # The strip leaves the body: it belongs to the masthead now, and the masthead is
        # assembled around it below. `body_html` is the panels alone, which is what every
        # rewrite downstream of here (the tab count, the enumeration check) is about.
        strip_html = (
            '<div class="tabstrip" role="tablist" aria-label="Review sections">'
            + "".join(strip) + "</div>"
        )
        # The show-everything toggle is not part of the strip any more (see the CSS): it
        # is emitted at the foot of the page, on the footer's own line and at the far
        # right of it. The label says what it does *next* and therefore has to change with
        # the state, which is what the pressed styling alone could no longer carry once the
        # button left the strip — down here there is nothing beside it to read the
        # highlight against.
        allbtn_html = (
            '<div class="allbar"><button type="button" class="allbtn" aria-pressed="false" '
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
        tab_labels = [t["label"] for t in emitted]
        # Built from `emitted` for the same reason: a tab that was dropped for having
        # nothing to show must not turn up in the ledger claiming to have cost something.
        cost_panel_html = cost_breakdown_html(costs, emitted)
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

    # The slot the scope bar left open. A build that measured nothing still gets its chip —
    # as the inert pill it always was — so a missing breakdown costs the reader the
    # breakdown, never the total.
    if cost_scope_chip is not None:
        chips = chips.replace(COST_CHIP_TOKEN,
                              cost_chip_html(cost_scope_chip, cost_panel_html))

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(spec.get('title', 'Review guide'))}</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<style>{CSS}{extra_css.rstrip()}
{LATE_CSS}</style></head>
<body><div class="wrap">
{masthead_html(spec, title_score, chips, strip_html, base_st)}
{lede_html}
{verdict_html}

{body_html}
<footer><div class="footrow"><span>{_link_home(spec.get('footer', ''))}</span>{allbtn_html}</div></footer>
</div>
{CAPTION_JS}
{GENSEQ_JS}
{FOCUS_JS}
{DGM_VIEWS_JS}
{EDITOR_JS}
{FRAME_JS}\n{TABS_JS}
{COST_JS}
{TIP_JS}
</body></html>
"""
    doc = open_links_in_new_tabs(doc)
    doc = one_tooltip_only(doc)
    check_baked_excerpts(doc)
    out_path.write_text(doc, encoding="utf-8")
    print(f"[review] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
