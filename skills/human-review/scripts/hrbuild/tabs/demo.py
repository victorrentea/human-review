"""The Demo tab: the feature film, its captions and its verdict."""
from __future__ import annotations

import html
import json
from pathlib import Path

from ..shared.commands import _app_anchor, runtime_html

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


#: What `_video` writes beside the film when the recorder exited non-zero, named after the
#: film so two sections cannot read each other's verdict.
VIDEO_VERDICT = ".verdict.json"

#: What the recorder's exit codes mean, in the words the banner uses. Exit 3 is the one
#: worth the machinery: the film was made, it is on the page, and it shows the feature not
#: working. Nothing about the footage says so — it is a normal-looking demo of a screen
#: that did not do what the caption claims — which is why the page has to.
VERDICT_FACE = {
    3: ("The feature did not hold on film.",
        "The recorder drove this branch's own walkthrough and it did not complete. "
        "Everything below is the film of that: watch it before reading anything else "
        "on this page."),
    2: ("Nothing was filmed.",
        "The recorder refused: no feature script, or the application answering was not "
        "the commit under review. A film of another branch's screens under this branch's "
        "narration is worse than no film."),
}


def video_verdict_html(rel: str, out_dir: Path) -> str:
    """The recorder's non-zero exit, said over the player — or nothing at all.

    This exists because the loudest thing this pipeline can produce was also the easiest
    to lose. `record-feature-video.sh` exits 3 when the walkthrough did not complete, and
    the film is kept *on purpose*: it is the most review-worthy artifact the whole page can
    carry. But the footage of a feature failing looks like footage of a feature working —
    a browser, a form, a list — and the exit code used to live only in a status table that
    a human read once and then had to remember to write about. On 17 Sep 2026 that is
    exactly what went wrong: exit 3 with three screens missed, and the page showed the
    player with no mark on it at all.

    So the step writes the verdict next to the film and this draws it. Missing file means
    the recorder exited 0, which is the common case and renders nothing.
    """
    path = out_dir / (rel.replace(".webm", "") + VIDEO_VERDICT)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    code = doc.get("exit")
    title, why = VERDICT_FACE.get(code, (
        f"The recorder failed (exit {code}).",
        "There is no complete film of this change. Treat whatever plays below as partial."))
    missed = [m for m in (doc.get("missed") or []) if isinstance(m, str)]
    parts = [f'<p class="vv-head"><b>{html.escape(title)}</b> {html.escape(why)}</p>']
    if missed:
        # Named, not counted: "three screens missed" sends the reader back to the log,
        # and the names are what tells them whether the gap is the one that matters.
        parts.append('<p class="vv-missed"><b>Never reached:</b> '
                     + " · ".join(f'<code>{html.escape(m)}</code>' for m in missed)
                     + "</p>")
    if doc.get("note"):
        parts.append(f'<p class="vv-note">{html.escape(str(doc["note"]))}</p>')
    log = [str(l) for l in (doc.get("log") or [])]
    if log:
        # Folded: the lines are the answer for whoever is fixing it and furniture for
        # everybody else, and the summary above already says what happened.
        parts.append('<details class="vv-log"><summary>the recorder’s last words'
                     "</summary><pre>"
                     + html.escape("\n".join(log[-14:])) + "</pre></details>")
    return '<div class="vidverdict" role="alert">' + "".join(parts) + "</div>"


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
    # The verdict sits OUTSIDE the wrap, not inside it beside the player: `.vidwrap` is a
    # two-column grid, so a band emitted as one of its children takes a column and stands
    # next to the picture instead of across the top of it. What it contradicts is the
    # picture, so it has to be the thing read first, full width.
    return (runtime_html(rt) + video_verdict_html(rel, out_dir)
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
