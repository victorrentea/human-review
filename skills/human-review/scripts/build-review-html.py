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
import html
import json
import re
import subprocess
import sys
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
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#15151a; --fg:#e8e8ef; --muted:#9a9aa8; --line:#2c2c36; --card:#1d1d24;
          --accent:#f08a8a; --accent-soft:#3a1f1f; --code-bg:#101015; --link:#8ab4f8; }
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
.prov { margin:.5rem 0 0; display:flex; gap:.9rem; flex-wrap:wrap; }
.prov .srcref { margin-bottom:0; }
.diagram .head span { color:var(--muted); font-size:.82rem; font-family:ui-monospace,Menlo,monospace; }
.diagram .svgbox { overflow-x:auto; margin-top:.7rem; background:#fff; border-radius:6px; padding:.6rem; }
.diagram .svgbox svg { max-width:100%; height:auto; display:block; margin:0 auto; }
.diagram .svgbox[hidden] { display:none; }
/* A class name that opens the source looks exactly like one that does not. PlantUML's
    tooltip says so, but only after a second of hovering and only if you were already
    suspicious — so the name underlines the moment the pointer is over it. The class
    anchor wraps the whole box, the attribute anchors wrap just their own line, so
    hovering anywhere in a class underlines its name and hovering a field underlines
    the field. */
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
#genseq-panel pre { margin:0; max-height:24rem; overflow:auto; background:var(--code-bg);
                    border-radius:6px; padding:.5rem .6rem; white-space:pre;
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
/* Full-bleed band: the verdict is the one thing that should not sit politely inside the
    text column. It breaks out to the viewport edges and pads itself back to the column. */
.verdict { margin:1.6rem 0 2.2rem; margin-left:calc(50% - 50vw); width:100vw;
            padding:1.5rem max(1.25rem, calc(50vw - 540px + 1.25rem));
            display:grid; grid-template-columns:auto 1fr; gap:2rem; align-items:center;
            border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
.verdict .score { text-align:center; }
.verdict .score b { display:block; font-size:3.4rem; line-height:1; letter-spacing:-.04em; }
.verdict .score span { font-size:.7rem; text-transform:uppercase; letter-spacing:.09em; opacity:.75; }
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
button.tab .n.alarm { background:#c62828; color:#fff; opacity:1; border-radius:999px;
                      padding:.05rem .45rem; letter-spacing:.03em; }
button.tab[aria-selected="true"] .n.alarm { background:#fdeaea; color:#8a1c1c; }
button.tab .sev { width:6px; height:6px; border-radius:50%; background:var(--accent); }
button.allbtn { border:1px solid var(--line); background:var(--card); color:var(--muted);
                border-radius:999px; cursor:pointer; font:600 .74rem/1.9 inherit; padding:0 .7rem; }
button.allbtn:hover { color:var(--fg); border-color:var(--link); }
button.allbtn[aria-pressed="true"] { background:var(--link); border-color:var(--link); color:#fff; }
.panel[hidden] { display:none; }
.panel > h2:first-child, .panel > .paneltag + h2 { margin-top:.2rem; }
/* Only meaningful once every panel is on screen at once, which is what "show all"
    (and printing) do — otherwise the heading names the tab you are already on. */
.paneltag { display:none; margin:2.6rem 0 0; font:700 .72rem/1.6 inherit; letter-spacing:.1em;
            text-transform:uppercase; color:var(--muted); }
body.showall .paneltag { display:block; }
body.showall .panel { border-top:1px solid var(--line); }
body.showall .panel:first-of-type { border-top:0; }
@media print {
  .tabstrip { display:none; }
  .panel[hidden] { display:block !important; }
  .paneltag { display:block; }
}
"""


CAPTION_JS = """<script>
document.querySelectorAll('.vidwrap').forEach(function (wrap) {
  var video = wrap.querySelector('video');
  var items = Array.prototype.slice.call(wrap.querySelectorAll('.transcript li'));
  if (!items.length) return;
  items.forEach(function (li) {
    li.addEventListener('click', function () {
      video.currentTime = parseFloat(li.dataset.t);
      video.play();
    });
  });
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

  // The hash is the shareable handle: a reviewer sends "look at #api" and it opens there.
  // replaceState rather than location.hash, which would scroll the page out from under
  // the click that caused it.
  function select(i, remember, keepScroll) {
    if (i < 0 || i >= tabs.length) return;
    active = i;
    paint();
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
    els.title.setAttribute('data-tip', href ? '⌘-click to open the test that produced this' : '');
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


def snippet_html(ref: str, caption: str | None, root: Path) -> str:
    cmd = [sys.executable, str(EXTRACT), ref]
    if caption:
        cmd += ["--caption", caption]
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


def inline_svg(path: Path, root: Path) -> str:
    """Inline rather than <img src>: the guide must survive being emailed as one file."""
    svg = path.read_text(encoding="utf-8")
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
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
    if (root / rel).is_file():
        links.append(f'<a class="srcref" href="vscode://file/{(root / rel).resolve()}:1:1">'
                      f'{html.escape(Path(rel).name)}</a>')
    return ('<p class="prov">' + " ".join(links) + '</p>') if links else ''


# Which radius a reviewer meets first — the whole diagram.
#
# It used to open at one hop, on the reasoning that a large diagram is a wall to hunt for
# red in. But opening pruned is a claim that the rest does not matter, made before the
# reviewer has seen the rest: the question they arrive with is "is this change in the
# right place", and a view that has already deleted the neighbourhood cannot answer it.
# Narrowing is one click and reversible; the context you were never shown is neither.
DEFAULT_FOCUS = "all"


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
        '<div class="focus"><span class="lbl">unchanged context, in hops:</span>'
        + buttons + "</div>" + boxes
    )


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
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 9), r["name"]))
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
            f'<div class="head"><b>{html.escape(r["name"])}</b>'
            f'<span class="badge {"sev-high" if r["status"] == "added" else "sev-low"}">'
            f'{html.escape(r["status"])} · {html.escape(r["kind"])}</span>'
            f'<span>{html.escape(r["source"])}</span></div>'
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


REQUIRED = {
    "sections": ("id", "title"),
    "tabs": ("id", "label"),
    "findings": ("title", "body"),
    "autofixes": ("title",),
}


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
        if s.get("video") and not (out_dir / s["video"]).is_file():
            # A recording that failed leaves a dead <video> under a confident heading. Say so.
            vid = ('<p class="sub">Not filmed — <code>' + html.escape(s["video"])
                   + '</code> was not produced by this run.</p>')
        elif s.get("video"):
            # Captions live under the player, driven by the cue list the recorder wrote as it
            # ran, so the narration cannot drift from what the video shows. Plain text swap on
            # timeupdate — no track element, which file:// pages are not allowed to load.
            cues_path = out_dir / s["video"].replace(".webm", ".cues.json")
            cues = json.loads(cues_path.read_text(encoding="utf-8")) if cues_path.is_file() else []
            items = "".join(
                f'<li data-t="{c["t"]:.2f}"><span class="ts">{int(c["t"]) // 60}:'
                f'{int(c["t"]) % 60:02d}</span><span>{html.escape(c["text"])}</span></li>'
                for c in cues
            )
            vid = (
                f'<div class="vidwrap"><video controls preload="metadata" '
                f'src="{html.escape(s["video"])}"></video>'
                f'<ol class="transcript">{items}</ol></div>'
            )
        rendered = (
            f'<h2 id="{html.escape(s["id"])}">{html.escape(s["title"])}</h2>\n'
            f'{expand_snippets(s.get("body", ""), root)}\n{inc}{vid}{snips}'
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
        inner = f'{html.escape(c["label"])} <b>{c["value"]}</b>'
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
    labels = " ".join(c["label"].lower() for c in scope)
    missing = [name for name in ("/code-review", "/simplify") if name[1:] not in labels]
    if scope and missing:
        print(f"[review] WARNING: the scope bar has no chip for {', '.join(missing)} — "
              "each automated review gets its own chip, never one merged 'reviews run'",
              file=sys.stderr)

    v = spec.get("verdict")
    verdict_html = ""
    if v:
        n = int(v["score"])
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
            # Nothing of this family changed: contribute nothing rather than a paragraph
            # saying so. Whether that leaves the tab empty is the tab's business.
            if not rows:
                return "", 0, 0
            merged = dict(dspec)
            merged.pop("only", None)
            return (
                heading(block, "diagrams", dspec.get("title", "Diagram deltas"))
                + render_diagrams(merged, root, out_dir, rows),
                len(rows), len(rows),
            )
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
                auto_badge["badge"], auto_badge["class"] = "approval required", "alarm"
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
        strip, panels, dropped, quiet = [], [], [], []
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
            count = (f'<span class="n{" " + html.escape(badge_class) if badge_class else ""}">'
                     f'{html.escape(badge)}</span>') if badge else ""
            # Struck through rather than dropped: the answer "we looked, and this branch
            # did not touch it" is worth as much to a reviewer as the answer that it did.
            still = not changes and not tab.get("noStrike")
            if still:
                quiet.append(tab["label"])
            strip.append(
                f'<button type="button" class="tab{" quiet" if still else ""}" role="tab" '
                f'id="tabbtn-{tid}" aria-controls="{tid}" aria-selected="false" tabindex="-1"'
                + (' data-tip="This change set did not touch anything here — the tab holds '
                   'the current state as context."' if still else "")
                + f'>{html.escape(tab["label"])}{count}</button>'
            )
            panels.append(
                f'<section class="panel" id="{tid}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{tid}">'
                f'<p class="paneltag">{html.escape(tab["label"])}</p>{body}</section>'
            )
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
        notes = []
        if dropped:
            notes.append("Nothing to show for: "
                         + ", ".join(html.escape(d) for d in dropped) + ".")
        if quiet:
            notes.append("Unchanged by this branch, kept as context: "
                         + ", ".join(html.escape(q) for q in quiet) + ".")
        if notes:
            body_html += '<p class="sub">' + " ".join(notes) + "</p>"
    else:
        # No tab layout in the content file: the original single-column guide, unchanged.
        body_html = (
            '<h2 id="first">Look here first</h2>\n'
            + render_findings(spec.get("findings", []))
            + f'\n<h2 id="diagrams">{html.escape(dspec.get("title", "Diagram deltas"))}</h2>\n'
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
<style>{CSS}{extra_css}</style></head>
<body><div class="wrap">
<h1>{html.escape(spec.get('title', 'Review guide'))}</h1>
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
{TABS_JS}
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
