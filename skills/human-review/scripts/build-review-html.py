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
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXTRACT = HERE / "extract-snippet.py"
CODEOWNERS = HERE / "codeowners-check.py"

SEVERITIES = {
    "high": ("sev-high", "must look"),
    "medium": ("sev-med", "worth a look"),
    "low": ("sev-low", "nit"),
    "info": ("sev-info", "context"),
}

CSS = """
:root {
  --bg:#fbfbfd; --fg:#1c1c22; --muted:#6b6b78; --line:#e2e2ea; --card:#ffffff;
  --accent:#8a1c1c; --accent-soft:#fdeaea; --code-bg:#f6f6fa; --link:#1a4fa0;
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
  /* The diff renderer's reds (puml_diff.py's `<color:red>` and seq_puml_diff.py's
     literal #D40000) are kept as their own variables rather than folded into --accent:
     they mark *added/removed*, a different signal than the page's own accent color, and
     must stay legible against whichever diagram surface they are drawn on. */
  --dgm-diff:#ff0000; --dgm-diff-seq:#d40000;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#15151a; --fg:#e8e8ef; --muted:#9a9aa8; --line:#2c2c36; --card:#1d1d24;
          --accent:#f08a8a; --accent-soft:#3a1f1f; --code-bg:#101015; --link:#8ab4f8;
          /* PlantUML draws these as flat, fully-opaque shapes, so each is picked to read
             the way its light counterpart does on white — not lifted from --bg/--card,
             whose contrast ratios were tuned for text, not a diagram's fills and hairline
             strokes. --dgm-icon is deliberately absent: the stereotype ellipse's pale
             green already sits at ~9:1 against a dark box, better than it does on white,
             so it is left un-overridden. Both diff reds converge on one brighter red —
             #ff0000/#d40000 sit at ~4.2:1 and worse against a near-black canvas, under
             the 4.5:1 text minimum; #ff6b6b clears ~6:1 while still reading as "red". */
          --dgm-bg:#1d1d24; --dgm-box:#26262e; --dgm-frame:#202028; --dgm-legend:#2c2c36;
          --dgm-line:#8f8fa0; --dgm-fg:#e8e8ef; --dgm-activation:#2e2e42;
          --dgm-muted:#9a9aa8; --dgm-link:#8ab4f8;
          --dgm-diff:#ff6b6b; --dgm-diff-seq:#ff6b6b; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1080px; margin:0 auto; padding:2.5rem 1.25rem 5rem; }
h1 { font-size:1.9rem; margin:0 0 .3rem; letter-spacing:-.02em; }
h2 { font-size:1.3rem; margin:2.8rem 0 .8rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
h3 { font-size:1.02rem; margin:1.8rem 0 .5rem; }
p { margin:.6rem 0; }
a { color:var(--link); }
.sub { color:var(--muted); margin:0 0 1.4rem; font-size:.93rem; }
.scopebar { display:flex; flex-wrap:wrap; gap:.5rem; margin:0 0 1.5rem; }
.chip { background:var(--card); border:1px solid var(--line); border-radius:999px;
        padding:.2rem .7rem; font-size:.82rem; color:var(--muted); }
.chip b { color:var(--fg); font-weight:600; }
a.chip-link { text-decoration:none; }
a.chip-link:hover { border-color:var(--link); background:var(--accent-soft); }
.added { color:#2e7d32; } .removed { color:#c62828; }
@media (prefers-color-scheme: dark) { .added{color:#8fd39c} .removed{color:#f08a8a} }
ul.fixlist { margin:.5rem 0 .8rem; padding-left:1.1rem; display:grid; gap:.3rem; }
ul.fixlist li { font-size:.93rem; }
ul.fixlist .srcref { margin-bottom:0; font-size:11.5px; }
.lede { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent);
        border-radius:6px; padding:.9rem 1.1rem; }
figure { margin:1rem 0; }
figcaption { color:var(--muted); font-size:.86rem; }
.snippet { background:var(--card); border:1px solid var(--line); border-radius:8px;
            padding:.7rem .9rem; margin:.9rem 0; overflow:hidden; }
.snippet-note { margin:0 0 .45rem; color:var(--fg); font-size:.9rem; }
.srcref { display:inline-block; font:600 12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
          color:var(--link); text-decoration:none; border-bottom:1px dotted currentColor; margin-bottom:.5rem; }
.srcref:hover { background:var(--accent-soft); }
pre.code { margin:0; background:var(--code-bg); border-radius:6px; padding:.6rem .2rem .6rem 0;
            overflow-x:auto; font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
pre.code code { white-space:pre; }
.ln { display:inline-block; width:3.4em; padding-right:.9em; text-align:right; color:var(--muted);
      user-select:none; opacity:.65; }
.diagram { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:1rem; margin:1.1rem 0; }
.diagram .head { display:flex; justify-content:space-between; align-items:baseline; gap:1rem; flex-wrap:wrap; }
.diagram .head b { font-size:1rem; }
/* The logging tab: no separate line for the level, no second coloured pill, and no
   top-left label line either. Both ride below the code on one row: the verdict and its
   reason at the left, since that is what a reviewer reads, and the level/location
   ("WARN - Class:line", still the vscode:// link) pinned to the far right as the box's
   corner marker, since that is what they click. `space-between` holds that layout when
   both fit; `flex-wrap` lets the label drop under the verdict at a narrow width, in
   markup order, rather than truncating either one. This spot has now moved three times
   (top-right corner, sharing the line ahead of the verdict, here) -- this is the one
   rule left; the two positions before it left nothing behind. */
.log-footer { display:flex; flex-wrap:wrap; align-items:baseline; justify-content:space-between;
              column-gap:.8rem; row-gap:.25rem; margin:.6rem 0 0; font-size:.85rem; }
.log-footer .srcref { margin:0; }
/* The provenance chain: where each logged value actually came from, one hop per line,
   between the code and the verdict it supports. Compact on purpose -- "a few lines, not
   a wall" -- numbered because the order of the hops is the story. */
.chain-hops { margin:.5rem 0 0; padding-left:1.4rem; display:grid; gap:.25rem;
              font-size:.8rem; color:var(--muted); }
.chain-hops .srcref { font-size:11px; margin-bottom:0; }
.chain-hops code { background:var(--code-bg); border-radius:4px; padding:.05rem .35rem;
                    font-size:.85em; color:var(--fg); }
.chain-hops .chain-note { color:var(--muted); }
.chain-hops .chain-unresolved { color:#8a4b00; font-style:italic; }
.chain-hops .chain-skip { font-style:italic; }
@media (prefers-color-scheme: dark) {
  .chain-hops .chain-unresolved { color:#f0b558; }
}
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
.diagram svg a[href^="vscode:"] { cursor:pointer; }
.diagram svg a[href^="vscode:"]:hover text { text-decoration:underline; }
/* A removed element carries its strikethrough as a presentation attribute, which a CSS
    declaration would silently outrank — underlining it would erase the one mark that
    says it was deleted. Keep both. */
.diagram svg a[href^="vscode:"]:hover text[text-decoration="line-through"] {
  text-decoration:line-through underline; }
/* How much unchanged context to draw around what changed. DomainModel and DB are big
    enough that the whole diagram is a wall to hunt for red in, and how much context
    makes a given change legible is the reviewer's call, not the generator's. */
.focus { display:flex; align-items:center; gap:.35rem; margin-top:.7rem; flex-wrap:wrap; }
.focus .lbl { color:var(--muted); font-size:.78rem; margin-right:.15rem; }
.focus button { border:1px solid var(--line); background:var(--card); color:var(--muted);
                border-radius:999px; cursor:pointer; font:600 .74rem/1.7 inherit;
                padding:0 .6rem; }
.focus button:hover { border-color:var(--link); color:var(--fg); }
.focus button[aria-pressed="true"] { background:var(--link); border-color:var(--link); color:#fff; }
/* Progressive disclosure: the diagram arrives simplified, and an arrow that has more
    to say is clickable. The hit area is a transparent rect the script lays under each
    such arrow, so the whole band — label, line, marker — answers to one click. */
/* The section header names the scenario the picture is of, so it is the reader's handle on
   the test behind it. Underlined because it is a link and nothing else on a sequence diagram
   is — dotted at rest so it reads as an offer rather than as emphasis, solid under the
   pointer. The colour is PlantUML's own hyperlink colour, or the delta's red where the header
   itself changed. */
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
ol.findings > li::before { content:counter(f); float:left; margin:.1rem .7rem 0 0; width:1.6rem; height:1.6rem;
    border-radius:50%; background:var(--accent); color:#fff; font-size:.8rem; font-weight:700;
    display:grid; place-items:center; }
.f-title { font-weight:650; }
.f-why { color:var(--muted); font-size:.9rem; margin:.35rem 0 0; }
.sev-high { background:#fdeaea; color:#8a1c1c; }
.sev-med  { background:#fdf3e2; color:#8a5a12; }
.sev-low  { background:#eef3fb; color:#26518f; }
.sev-info { background:#eef7ef; color:#245c30; }
@media (prefers-color-scheme: dark) {
  .sev-high{background:#3a1f1f;color:#f2a0a0}.sev-med{background:#3a3018;color:#e6c07b}
  .sev-low{background:#1c2738;color:#9dc0f5}.sev-info{background:#1b2c1f;color:#9ad3a5}
}
table.stat { border-collapse:collapse; width:100%; font-size:.88rem; }
table.stat td { border-bottom:1px solid var(--line); padding:.35rem .5rem; }
table.stat td.n { text-align:right; color:var(--muted); font-family:ui-monospace,Menlo,monospace; white-space:nowrap; }
.titlerow { display:flex; align-items:baseline; justify-content:space-between; gap:1.5rem;
             flex-wrap:wrap; }
.titlerow h1 { margin-bottom:0; }
.titlescore { display:inline-flex; align-items:baseline; gap:.35rem; padding:.3rem .8rem;
              border-radius:999px; white-space:nowrap; }
.titlescore b { font-size:1.5rem; line-height:1; letter-spacing:-.02em; }
.titlescore small { font-size:.8rem; opacity:.6; }
.titlescore i { font-style:normal; font-size:.82rem; opacity:.85; margin-left:.25rem; }
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
.tabstrip { position:sticky; top:0; z-index:30; margin:1.6rem 0 1.4rem;
            margin-left:calc(50% - 50vw); width:100vw;
            padding:.5rem max(1.25rem, calc(50vw - 540px + 1.25rem));
            background:var(--bg); border-bottom:1px solid var(--line);
            display:flex; gap:.3rem; flex-wrap:wrap; align-items:center; }
.tabstrip .grow { flex:1 1 1rem; }
/* Ten tabs no longer fit the 1040px track the strip inherits from the text column:
   the tabs themselves still make one row, but `show all` dropped to a second, doubling
   the strip from 34px to 56px. The strip is already full-bleed, so the room is there —
   only the RIGHT padding is relaxed, which lets `show all` sit ~80px further into the
   bleed. The left padding is untouched, so the first tab stays aligned with the body
   text exactly as before. */
.tabstrip { padding-right: max(1.25rem, calc(50vw - 620px + 1.25rem)); }
/* Struck through, not hidden: the tab still holds the current state as context, and a
   reviewer who cannot see that it exists cannot tell it was considered. */
button.tab.quiet { text-decoration:line-through; text-decoration-thickness:1px; opacity:.5; }
button.tab.quiet:hover, button.tab.quiet[aria-selected="true"] { opacity:.85; }
button.tab { border:1px solid transparent; background:none; color:var(--muted); border-radius:999px;
             cursor:pointer; font:600 .87rem/2 inherit; padding:0 .85rem; white-space:nowrap;
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
button.allbtn { border:1px solid var(--line); background:var(--card); color:var(--muted);
                border-radius:999px; cursor:pointer; font:600 .74rem/1.9 inherit; padding:0 .7rem; }
button.allbtn:hover { color:var(--fg); border-color:var(--link); }
button.allbtn[aria-pressed="true"] { background:var(--link); border-color:var(--link); color:#fff; }
.panel[hidden] { display:none; }
/* A hash lands the panel top flush against the viewport, where the sticky strip sits
   on top of it and eats the first line. Push the scroll target down past the strip. */
.panel { scroll-margin-top: 3.2rem; }
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
   ten tabs plus `show all` already need 1115.5px of it — 4.5px of slack. "Spec changes"
   is 113.7px wide and needs 118.5px with its gap, so the strip wraps to two rows (34px
   → 55.8px) at 1280px, 1440px and 1920px alike. Three shavings, cheapest first: the
   spacer stops reserving a 1rem basis it never draws (+16px); the right padding drops to
   its floor, which the strip's full bleed already covers (+20px at 1280, +100px at 1440);
   and every pill gives up .25rem of horizontal padding (+88px across eleven of them).
   Budget at 1280px, the narrowest width that has to hold: 1140px of track, 1130px used.
   The LEFT padding is deliberately untouched — the first tab still starts exactly where
   the body text does. */
.tabstrip .grow { flex:1 1 0; }
.tabstrip { padding-right:1.25rem; }
button.tab { padding:0 .6rem; }

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
  var items = Array.prototype.slice.call(wrap.querySelectorAll('.transcript li'));
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

TIP_JS = """<script>
// One tooltip for the whole page. The native `title` is unstyleable, unresizable and
// waits ~500ms — long enough that a reviewer reads the icon, gives up, and moves on.
// Listeners are delegated on `document` so markup written later by any of the other
// scripts picks the behaviour up with no registration step.
(function () {
  var css = document.createElement('style');
  css.textContent =
    '.tip{position:fixed;z-index:9999;pointer-events:none;background:rgba(20,20,22,.96);' +
    'color:#fff;font:600 1.05rem/1.25 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;' +
    'padding:.6rem .9rem;border-radius:.6rem;max-width:22rem;box-shadow:0 10px 30px rgba(0,0,0,.35);' +
    'opacity:0;transform:translateY(4px);transition:opacity 120ms ease,transform 120ms ease}' +
    '.tip.visible{opacity:1;transform:translateY(0)}';
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
    var r = el.getBoundingClientRect(), b = bubble.getBoundingClientRect();
    var left = r.left + r.width / 2 - b.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - b.width - 8));
    // Above by default; below when the top of the viewport is in the way.
    var top = r.top - b.height - 10;
    if (top < 8) top = r.bottom + 10;
    bubble.style.left = left + 'px';
    bubble.style.top = top + 'px';
  }

  function show(el) {
    var text = el.getAttribute('data-tip');
    if (!text) return;                       // data-tip="" shows nothing, by design
    current = el;
    bubble.textContent = text;
    bubble.classList.remove('visible');
    place(el);
    timer = setTimeout(function () {
      if (current !== el) return;
      place(el);
      bubble.classList.add('visible');
    }, 150);
  }

  function trigger(ev) {
    var el = ev.target.closest && ev.target.closest('[data-tip]');
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
  var showAll = strip.querySelector('button.allbtn');
  var active = 0;

  function paint() {
    var all = document.body.classList.contains('showall');
    tabs.forEach(function (t, i) {
      // In show-all there is no selected tab: leaving one lit makes the strip claim a
      // filter is applied while every panel is on screen.
      t.setAttribute('aria-selected', String(!all && i === active));
      t.tabIndex = i === active ? 0 : -1;
      if (panels[i]) panels[i].hidden = !all && i !== active;
    });
    if (showAll) showAll.setAttribute('aria-pressed', String(all));
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
      var top = strip.getBoundingClientRect().top + window.pageYOffset - 8;
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

  document.addEventListener('click', function (ev) {
    var link = ev.target.closest && ev.target.closest('a[href^="vscode:"]');
    if (!link || ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
    ev.preventDefault();
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
    if (!EMBEDDED) { window.location.href = link.getAttribute('href'); return; }
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
      '<div class="genseq-label"></div><pre></pre>';
    document.body.appendChild(panel);
    els = {
      title: panel.querySelector('.genseq-title'),
      step: panel.querySelector('.genseq-step'),
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

  function show(entry, index, target, href) {
    build();
    source = href;
    els.title.style.cursor = href ? 'pointer' : '';
    // No tooltip on the title: the cursor already says it is clickable, and a hint that
    // pops over the heading you are reading costs more than it explains.
    step = entry.steps[index];
    els.title.textContent = entry.title;
    els.step.textContent = entry.steps.length > 1 ? (index + 1) + ' / ' + entry.steps.length : '';
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
    var carrier = diagram.querySelector('script.genseq-details');
    if (!carrier) return;
    var details = (JSON.parse(carrier.textContent) || {}).details || {};
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
    diagram.querySelector('.svgbox').insertAdjacentElement('beforebegin', hint);
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


def expand_snippets(text: str, root: Path) -> str:
    """Let prose interleave with code: `{{snippet:path:12-14|caption}}` inside any body."""
    return SNIPPET_TOKEN.sub(
        lambda m: snippet_html(m["ref"].strip(), (m["caption"] or "").strip() or None, root), text
    )


def snippet_html(ref: str, caption: str | None, root: Path, exact: bool = False) -> str:
    cmd = [sys.executable, str(EXTRACT), ref]
    if caption:
        cmd += ["--caption", caption]
    if exact:
        cmd.append("--exact")
    out = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if out.returncode != 0:
        raise SystemExit(out.stderr.strip() or f"extract-snippet failed for {ref}")
    return out.stdout


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
# palette. That is the whole of it: every fill/stroke/background literal below is one of
# these twelve values, in every .svg this pipeline inlines. Mapping each to a `--dgm-*`
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
    "#1A4FA0": "--dgm-link", "#FF0000": "--dgm-diff", "#D40000": "--dgm-diff-seq",
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


def genseq_details(rel: str, root: Path) -> str:
    """The sidecar the generator filed beside the diagram, carried into the page.

    Inlined rather than fetched: review.html is opened from file://, where fetch() of a
    neighbouring file is blocked, and the guide has to survive being mailed as one file."""
    if not rel.endswith(".genseq.puml"):
        return ""
    sidecar = root / (rel[: -len(".puml")] + ".json")
    if not sidecar.is_file():
        return ""
    # `<` is the only character that can end a <script> block early, and a JSON string
    # may legally spell it \\u003c — so the payload stays valid JSON and inert to the
    # HTML parser without any un-escaping step on the other side.
    payload = sidecar.read_text(encoding="utf-8").replace("<", "\\u003c")
    return f'<script type="application/json" class="genseq-details">{payload}</script>'


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


# Which radius a reviewer meets first — one hop of unchanged context around the change.
#
# It opened on the whole diagram for a while, on the reasoning that a pruned view is a
# claim that the rest does not matter. In practice the whole DB and DomainModel deltas are
# a wall of forty unchanged entities with the change somewhere inside, and every reviewer
# who opened them did the same thing: clicked `1`. One hop already carries the
# neighbourhood the "is this in the right place" question needs, and `all` is one click
# away — a click the reader now makes only when the neighbourhood was not enough.
DEFAULT_FOCUS = "1"


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


def render_testpairs(block, dspec, manifest_rows, root: Path, out_dir: Path):
    """Each acceptance test next to the sequence its own run recorded.

    They used to be two lists on the same tab — a gallery of diagrams, then a list of test
    snippets — and the reader had to work out which picture belonged to which test from the
    file names. Nothing was hidden and nothing was reliable. The pairing is not a judgement
    call, either: the manifest says which test file each diagram came from, and the
    diagram's own chapter titles say which scenarios inside that file, at which lines. So it
    is derived, not authored.

    A test with no diagram is never dropped and never given one it did not produce: it goes
    to a trailing group that says exactly that."""
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
        pieces += take(test_rel)
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
        body = (
            _focus_views(r, manifest.parent, svg_rel, root)
            if svg_rel and svg_rel.is_file()
            else f'<p class="sub">not rendered — see <code>{html.escape(r["diff_puml"])}</code></p>'
        )
        parts.append(
            f'<div class="diagram">'
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
            + body + '</div>'
        )
    return "\n".join(parts)


def render_findings(findings) -> str:
    if not findings:
        return '<p class="sub">Nothing outstanding — the automated passes came back clean.</p>'
    items = []
    for f in findings:
        cls, label = SEVERITIES.get(f.get("severity", "info"), SEVERITIES["info"])
        # A finding that shows a snippet already links the file — with a line RANGE — from
        # the snippet's own header. Repeating a bare file:line link just above it says the
        # same thing twice and worse, so the standalone refs only render when there is no
        # snippet to carry them.
        refs = (
            ""
            if f.get("_snippets")
            else "".join(
                f'<a class="srcref" href="vscode://file/{r["abs"]}">{html.escape(r["label"])}</a> '
                for r in f.get("_refs", [])
            )
        )
        items.append(
            f'<li><span class="badge {cls}">{html.escape(label)}</span> '
            f'<span class="f-title">{f["title"]}</span>'
            f'<p>{f["body"]}</p>'
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f"<p>{refs}</p>" if refs else "")
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return '<ol class="findings">' + "\n".join(items) + "</ol>"


def render_autofixes(fixes) -> str:
    """What the agent already fixed \u2014 the other half of the same review.

    It sits next to the open findings on purpose. The two lists are one decision split
    in two: everything with a single obvious right answer was applied, everything a
    second engineer could reasonably disagree about was left. A reviewer who cannot see
    the first pile has to take the size of the second on trust."""
    if not fixes:
        return '<p class="sub">Nothing was applied automatically \u2014 every finding needed a human.</p>'
    items = []
    for f in fixes:
        items.append(
            f'<li><span class="f-title">{f["title"]}</span>'
            + (f'<p class="f-why">{f["why"]}</p>' if f.get("why") else "")
            + (f'<p>{f["body"]}</p>' if f.get("body") else "")
            + "".join(
                f'<a class="srcref" href="vscode://file/{r["abs"]}">{html.escape(r["label"])}</a> '
                for r in f.get("_refs", [])
            )
            + (f.get("_snippets", "") or "")
            + "</li>"
        )
    return '<ul class="fixlist">' + "\n".join(items) + "</ul>"


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
    out = f'<div class="vidwrap">{player}<ol class="transcript">{items}</ol></div>'
    if unplaced:
        out += ('\n<p class="sub"><b>Touched but not filmed</b> — opens the running app: '
                + " · ".join(f'<a href="{html.escape(l["href"])}">'
                             f'{html.escape(l.get("label") or l["href"])}</a>'
                             for l in unplaced) + ".</p>")
    return out


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


# The Overview lede walks the reader through the strip — "Eleven tabs, one question each,
# start on Autoreview, then …". Written by hand it is a second copy of the strip, and the
# second copy is the one that rots: a tab added at the end of `tabs` leaves the sentence
# saying "Ten" and skipping the newcomer, and nothing anywhere complains. So the number is
# a token the build fills in from the tabs it actually emitted, and the names are checked
# against the same list.
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
        print("[review] WARNING: the Overview lede never names these tabs: "
              + ", ".join(l for _, l in missing)
              + f" — the strip has {len(labels) + 1} of them and the lede walks "
                f"through {len(seen)}.",
              file=sys.stderr)
    out_of_order = [l for (a, l), (b, _) in zip(seen[1:], seen) if a < b]
    if out_of_order:
        print("[review] WARNING: the Overview lede names tabs in a different order than the "
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

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
        "trace": {"type": "string"},
        "chain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resolved": {"type": "boolean"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["resolved", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "trace", "chain"],
    "additionalProperties": False,
}

VERDICT_SYSTEM_PROMPT = (
    "You are a precise static-analysis assistant embedded in a code review build script. "
    "You are given one Java logging statement plus enough of its surrounding source to "
    "trace where each interpolated value comes from. Decide whether the statement, once "
    "it executes, could write personal data (GDPR-relevant: a name, an email address, a "
    "phone number, a postal address, a government ID, free text about a person, or "
    "similar) to a log aggregator kept for months.\n\n"
    "For each interpolated value, trace its provenance as a chain of hops: where it is "
    "declared or assigned and, if that right-hand side is itself another variable, "
    "continue from there. Stop a chain the moment it reaches something self-evident: a "
    "method parameter with a declared type, a literal, a field declaration, or a call "
    "whose return type settles the question. Report the full chain you found -- do not "
    "shorten it yourself, the page decides how much of it to show.\n\n"
    "Every hop you report must cite a file and a line number that appears in the context "
    "you were given -- you have no visibility beyond it, so never invent a location or "
    "guess one outside what was shown to you. If you cannot resolve a hop with what you "
    "were given, mark that hop `resolved: false`, explain why in its `note`, omit "
    "`file`/`line` for it, and the verdict must be DOUBT -- never guess SAFE past an "
    "unresolved hop. A statement whose arguments are already self-evident (an int "
    "parameter, say) needs no chain at all: report an empty `chain` and put the one "
    "clause of reasoning in `trace`.\n\n"
    "Respond only through the given JSON schema."
)


def _statement_context(h: dict) -> str:
    """Enough source for a model to trace every interpolated argument: the enclosing
    method (`logextract.py` resolves its range structurally, the same AST pass that
    finds the statement itself) so parameters and locals are both visible, plus the
    class's field declarations (numbered, so a field-rooted chain can cite a real line)
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
        "Which value(s) does this statement log, trace each one's provenance as a chain "
        "of hops back to something self-evident, and give your verdict."
    )


def _claude_bin() -> str | None:
    for cand in (os.environ.get("CLAUDE_BIN"), "claude"):
        p = shutil.which(cand) if cand else None
        if p:
            return p
    return None


def _call_privacy_model(prompt: str) -> dict:
    """One live model call. Returns `{"verdict","trace","chain","cost_usd"}` on success,
    or raises `RuntimeError` with a message written to go straight on the page — a
    missing binary, a non-zero exit, a timeout, or a response that does not match the
    schema. Never returns a guessed verdict; the caller turns any exception here into
    the loud `error` state, not a fallback answer. `chain` is validated only for shape
    here (a list of `{resolved, note, file?, line?}`) — whether a *resolved* hop's cited
    line actually exists is the renderer's job, against the working tree at render time,
    not this function's."""
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
    if proc.returncode != 0:
        raise RuntimeError(f"the model call exited {proc.returncode}: "
                           f"{proc.stderr.strip()[-300:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("the model call returned unparsable output")
    if payload.get("is_error"):
        raise RuntimeError(f"the model call failed: {str(payload.get('result'))[:300]}")
    out = payload.get("structured_output")
    chain = out.get("chain") if isinstance(out, dict) else None
    if (not isinstance(out, dict) or out.get("verdict") not in ("SAFE", "DOUBT", "PRIVACY")
            or not out.get("trace") or not isinstance(chain, list)
            or not all(isinstance(hop, dict) and "resolved" in hop and "note" in hop
                       for hop in chain)):
        raise RuntimeError("the model's response did not match the expected verdict schema")
    return {"verdict": out["verdict"].lower(), "trace": out["trace"], "chain": chain,
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
    """SAFE / DOUBT / PRIVACY / error, with a trace naming what was resolved — a live
    model call over the statement's enclosing method, cached by a hash of exactly what
    was sent (the statement plus its context) so a re-run on unchanged code neither
    flips the answer nor pays for it twice. `cache` is loaded once by the caller and
    mutated here; the file is rewritten on every new entry, not batched, so a run that
    dies partway through does not lose the calls it already paid for.

    `call` defaults to `None`, resolved to `_call_privacy_model` *inside* the body
    rather than as `def ...(call=_call_privacy_model)` — a default bound at def-time
    would freeze in the original function object, so patching the module-level name
    for a test (`monkeypatch.setattr(build, "_call_privacy_model", fake)`) would
    silently do nothing here; every caller that does not pass its own `call` needs the
    patch to actually take."""
    call = call or _call_privacy_model
    context = _statement_context(h)
    key = hashlib.sha256((h["text"] + "\n" + context).encode("utf-8")).hexdigest()
    cached = cache.get(key)
    if cached:
        return {**cached, "cached": True, "cost_usd": 0.0}
    try:
        result = call(_verdict_prompt(h, context))
    except RuntimeError as e:
        return {"verdict": "error", "trace": str(e), "chain": [], "cached": False, "cost_usd": 0.0}
    # The chain is cached too — it is exactly as much a part of what the model was paid
    # for as the verdict and the trace are, and a cache hit that dropped it would render
    # a verdict with no evidence under it, indistinguishable from one that never needed any.
    entry = {"verdict": result["verdict"], "trace": result["trace"],
             "chain": result.get("chain") or []}
    cache[key] = entry
    _save_verdict_cache(root, cache)
    return {**entry, "cached": False, "cost_usd": result.get("cost_usd", 0.0)}


MAX_CHAIN_HOPS_SHOWN = 3


def _chain_hop_line(root: Path, file: str, line) -> str | None:
    """The exact source line a hop cites, read fresh from the working tree — never the
    model's own retelling of it, so its claim and the page's evidence cannot disagree.
    None on anything that does not check out: a bad line number, a file that is not
    there, a value of the wrong shape. The model supplies the *address*; this is what
    verifies the address is real before the page repeats it as fact."""
    try:
        line = int(line)
        path = (root / file).resolve()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not (1 <= line <= len(lines)):
            return None
        return lines[line - 1].strip()
    except (OSError, ValueError, TypeError):
        return None


def _render_chain(chain: list, root: Path) -> tuple[str, bool]:
    """The provenance list under the code: the statement's own line is already the
    `<pre>` block above this, so here it is each hop the model traced, in order — a
    `file:line` link plus the real source line at that spot, cut fresh from the working
    tree rather than retyped by the model. An empty chain (every interpolated value was
    already self-evident) renders nothing: evidence belongs next to a claim that needs
    it, not padding a box that does not.

    Long chains are cut to the first hop and the last, with a note for what sat between
    them — "a few lines, not a wall" — never to a single line that hides how long the
    real chain was.

    Returns the HTML and whether any *resolved* hop's cited line failed to check out
    against the working tree — a hallucinated or now-stale citation, which the caller
    treats exactly as seriously as an unresolved one."""
    if not chain:
        return "", False
    shown, skipped = chain, 0
    if len(chain) > MAX_CHAIN_HOPS_SHOWN:
        skipped = len(chain) - 2
        shown = [chain[0], None, chain[-1]]
    items, broken = [], False
    for hop in shown:
        if hop is None:
            items.append(f'<li class="chain-skip">… {skipped} more hop'
                        f'{"" if skipped == 1 else "s"} …</li>')
            continue
        note = html.escape((hop.get("note") or "").strip())
        if not hop.get("resolved") or not hop.get("file") or not hop.get("line"):
            items.append(f'<li class="chain-unresolved">{note or "could not be traced further"}'
                        f'</li>')
            continue
        line_text = _chain_hop_line(root, hop["file"], hop["line"])
        if line_text is None:
            broken = True
            items.append(f'<li class="chain-unresolved">cited {html.escape(str(hop["file"]))}:'
                        f'{html.escape(str(hop["line"]))}, which could not be located</li>')
            continue
        abs_path = (root / hop["file"]).resolve()
        items.append(
            f'<li><a class="srcref" href="vscode://file/{abs_path}:{hop["line"]}:1" '
            f'data-tip="Open in VS Code">{html.escape(str(hop["file"]))}:{hop["line"]}</a> '
            f'<code>{html.escape(line_text)}</code>'
            + (f' <span class="chain-note">— {note}</span>' if note else '')
            + '</li>'
        )
    return f'<ol class="chain-hops">{"".join(items)}</ol>', broken


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
    total_cost, live_calls, cached_hits = 0.0, 0, 0
    ANCHOR_RE = re.compile(r'<a class="srcref" href="([^"]+)"[^>]*>[^<]*</a>\n')
    for h in added:
        end = h.get("end_line") or h["line"]
        ref = f'{h["file"]}:{h["line"]}' if end == h["line"] else f'{h["file"]}:{h["line"]}-{end}'
        label = f'{h["level"]} · {Path(h["file"]).stem}:{h["line"]}'
        h = {**h, "_fields": fields_by_file.get(h["file"])}
        result = privacy_verdict(h, cache_root, cache, call=call)
        total_cost += result.get("cost_usd") or 0.0
        cached_hits += 1 if result.get("cached") else 0
        live_calls += 0 if result.get("cached") else 1
        # The chain is re-cut from the *current* working tree on every render, cache hit
        # or not: the verdict can be reused because the statement and its context hashed
        # the same, but a citation is only evidence if it still points at real code now.
        chain_html, chain_broken = _render_chain(result.get("chain") or [], root)
        verdict_key = result["verdict"]
        trace = result["trace"]
        if chain_broken and verdict_key == "safe":
            verdict_key = "doubt"
            trace += " (a cited line in its trace could not be located — treated as unresolved)"
        emoji, word, css = PRIVACY_MARK[verdict_key]
        verdict_class = f"privacy-verdict {css}".strip()
        # extract-snippet.py always captions and labels a snippet with the full
        # repo-relative path, on its own line above the code — right when the path *is*
        # the point (a diagram, a test pairing). Here neither is true: there is no
        # caption line, and the label is pulled out of its default spot entirely and
        # rebuilt below the code as the box's bottom-right corner marker (still the same
        # vscode:// link) — the verdict and its trace sit on the same row, at the left,
        # because that is the part a reviewer reads; the location is the part they
        # click. `justify-content:space-between` pushes them to opposite ends when both
        # fit on the row, and `flex-wrap` drops the label under the verdict — verdict
        # first, since it is first in the markup — rather than truncating either one.
        snippet = snippet_html(ref, None, root, exact=True)
        m = ANCHOR_RE.search(snippet)
        href = m.group(1) if m else ""
        snippet = snippet[:m.start()] + snippet[m.end():] if m else snippet
        footer = (
            f'<p class="log-footer">'
            f'<span class="{verdict_class}">{emoji} <b>{word}</b> — '
            f'{html.escape(trace)}</span>'
            f'<a class="srcref" href="{href}" data-tip="Open in VS Code">'
            f'{html.escape(label)}</a>'
            f'</p>'
        )
        # The chain sits between the code and the verdict — the statement itself (the
        # code above) first, then each hop, in order, then the verdict it adds up to.
        snippet = snippet.replace("</figure>", f"{chain_html}{footer}</figure>", 1)
        boxes.append(snippet)
    # "The page marks tabs by what produced them" — this legend is the disclosure for a
    # tab whose verdicts are now a model's reading, not a program's, plus what that
    # reading cost this run: a re-run on unchanged code pays nothing (cache hits), so
    # the number here is honest about *this* build, not a standing per-run price.
    cost_note = (f' A live model call per statement this run cost ${total_cost:.4f}'
                if live_calls else ' Every verdict this run came from the cache (no charge).')
    if cached_hits and live_calls:
        cost_note = cost_note[:-1] + f' ({cached_hits} more reused from the cache).'
    legend = (
        '<div class="privacy-legend"><p class="privacy-legend-title">🤖 AI Evaluation:'
        f'</p><p class="privacy-legend-note">Each verdict above is a live '
        f'<code>claude</code> call tracing the statement and its enclosing method — '
        f'never a guessed answer, and never asserted without its evidence: a value worth '
        f'tracing gets its provenance cut from the working tree, line by line, under the '
        f'code.{cost_note}</p>'
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
    head = (f'<h2 id="{html.escape(block.get("id", "logging-added"))}">'
            f'{html.escape(block.get("title", "Logging this change set added"))}</h2>')
    # Raw, not wrapped in a <p>: the lede here is several paragraphs — what was found, why
    # it is a finding, and how it was measured — and it is the author's prose, not ours.
    body = block.get("body", "")
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

    problems = validate(spec, out_dir)
    if problems:
        print(f"[review] {Path(args.content).name} cannot be rendered:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    for f in spec.get("findings", []) + spec.get("autofixes", []):
        f["_refs"] = resolve_refs(f.get("refs", []), root)
        f["_snippets"] = "".join(
            snippet_html(s["ref"], s.get("caption"), root) for s in f.get("snippets", [])
        )

    # Every panel is `id="<tab id>"`, so a section that happens to share a tab's id puts the
    # same id on two elements — `id="api"` on the API contract panel and on the <h2> inside
    # it, which is what this page shipped for months. Nothing looked broken, because
    # getElementById returns the first match and the first match is the panel, which is
    # where `#api` should land anyway. It is still invalid HTML and still a trap for the
    # next person. The panel's id is not negotiable (the strip's aria-controls points at
    # it), so the duplicate is resolved on the heading, which loses nothing.
    tab_ids = {t.get("id") for t in (spec.get("tabs") or [])} | {"overview"}

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
        vid = ""
        if s.get("video"):
            vid = video_html(s, out_dir)
        body = expand_snippets(s.get("body", ""), root)
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
            # A section with no prose of its own — the video tab is one — must not open with
            # a blank line where the paragraph would have been.
            + (f"{body}\n" if body else "")
            + f'{inc}{vid}{snips}{embed_html(s, out_dir)}'
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
    scope = spec.get("scope", [])
    for c in scope:
        # A chip that has to be kept up to date by hand is a chip that will be wrong. The
        # cost of the run is the extreme case: it is still changing while the page is being
        # written, so it is computed here, at build time, and never typed into the content
        # file. `{"auto": "cost"}` is the whole declaration; label, value and tooltip all
        # come back from the script.
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
    chips = "".join(chips)

    # /code-review hunts bugs, /simplify shrinks the solution — different questions, so a
    # single chip over both reports neither. Warn rather than fail: the page still builds
    # for a run that legitimately skipped one, as long as it says which and why.
    labels = " ".join((c.get("label") or c.get("auto") or "").lower() for c in scope)
    missing = [name for name in ("/code-review", "/simplify") if name[1:] not in labels]
    if scope and missing:
        print(f"[review] WARNING: the scope bar has no chip for {', '.join(missing)} — "
              "each automated review gets its own chip, never one merged 'reviews run'",
              file=sys.stderr)

    v = spec.get("verdict")
    verdict_html = ""
    title_score = ""
    if v:
        n = int(v["score"])
        # The score belongs beside the title: it is the one thing a reader wants before
        # they have decided whether to read anything. The band below keeps the reasons.
        title_score = (f'<span class="titlescore {"v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")}">'
                       f'<b>{n}</b><small>/10</small>'
                       f'<i>{html.escape(v.get("label", ""))}</i></span>')
        cls = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")
        pips = "".join(f'<i class="{"on" if i < n else ""}"></i>' for i in range(10))
        verdict_html = (
            f'<div class="verdict {cls}">'
            f'<div class="score"><b>{n}<small style="font-size:.42em;opacity:.5">/10</small></b>'
            f'<span>{html.escape(v.get("label", ""))}</span>'
            f'<div class="scale">{pips}</div></div>'
            + "<ul>"
            + "".join(f"<li>{b}</li>" for b in v.get("bullets", []))
            + "</ul></div>"
        )

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

    def render_block(block):
        """One block of a tab, as (html, weight, changes).

        `weight` is "is there anything at all to show" — a tab whose every block weighs
        nothing is dropped. `changes` is the narrower question "did *this branch* move
        anything here" — a tab that is all context and no delta is kept, and struck
        through on the strip. A picture of the current state is not a change; that is
        why `puml` and `codecity` carry weight but no changes."""
        kind = block.get("type", "section")
        if kind == "overview":
            return overview_html, 1, 1
        if kind == "findings":
            items = spec.get("findings", [])
            return (heading(block, "first", "Look here first") + render_findings(items),
                    len(items), len(items))
        if kind == "autofixes":
            items = spec.get("autofixes", [])
            return (heading(block, "fixed", "Already fixed for you") + render_autofixes(items),
                    len(items), len(items))
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
    lede_html = f'<div class="lede">{spec.get("summary", "")}</div>' if spec.get("summary") else ""
    overview_html = ""
    if tabs and not any(tab.get("id") == "overview" for tab in tabs):
        # The summary and the verdict used to sit above the strip, which pushed the
        # questions below the fold on a laptop — a reviewer scrolled past the answers to
        # find out what the answers were. They are a tab now: the first one, so the page
        # opens on them, and the strip lands in the first screenful.
        overview_html = lede_html + verdict_html
        if overview_html:
            tabs = [{"id": "overview", "label": "Overview", "keepEmpty": True,
                     "noStrike": True, "blocks": [{"type": "overview"}]}] + list(tabs)
            lede_html = verdict_html = ""
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
            # A tab whose subject is not obvious from two words gets a sentence on hover.
            # The "we looked and found nothing" tooltip wins where both apply: it is the
            # more surprising fact about the tab.
            tip = ('This change set did not touch anything here — the tab holds '
                   'the current state as context.') if still else tab.get("tip", "")
            # What this tab cost, appended rather than swapped in: a reviewer who hovers a
            # struck-through tab should learn both why it is struck and what it cost, not
            # have the cost silently win the one tooltip slot this page has.
            cost_tip = (costs.get("tabs", {}).get(tab["id"], {}).get("tip", "")
                       if costs else "")
            if cost_tip:
                tip = f"{tip} {cost_tip}" if tip else cost_tip
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
        body_html = (
            '<div class="tabstrip" role="tablist" aria-label="Review sections">'
            + "".join(strip)
            + '<span class="grow"></span>'
            + '<button type="button" class="allbtn" aria-pressed="false" '
            'data-tip="Reveal every tab at once — makes ⌘F search the whole guide">'
            "show all</button></div>\n" + "\n".join(panels)
        )
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
        body_html = body_html.replace(TAB_COUNT_TOKEN, spelled(len(tab_labels)))
        check_tab_enumeration(overview_html, [l for l in tab_labels if l != "Overview"])
    else:
        # No tab layout in the content file: the original single-column guide, unchanged.
        body_html = (
            '<h2 id="first">Look here first</h2>\n'
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
{LATE_CSS}</style></head>
<body><div class="wrap">
<div class="titlerow"><h1>{html.escape(spec.get('title', 'Review guide'))}</h1>{title_score}</div>
<p class="sub">{spec.get('subtitle', '')}</p>
<div class="scopebar">{chips}</div>
{lede_html}
{verdict_html}

{body_html}
<footer>{spec.get('footer', '')}</footer>
</div>
{CAPTION_JS}
{GENSEQ_JS}
{FOCUS_JS}
{EDITOR_JS}
{FRAME_JS}\n{TABS_JS}
{TIP_JS}
</body></html>
"""
    doc = open_links_in_new_tabs(doc)
    doc = one_tooltip_only(doc)
    out_path.write_text(doc, encoding="utf-8")
    print(f"[review] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
