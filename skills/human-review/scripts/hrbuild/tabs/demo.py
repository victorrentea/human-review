"""The Demo tab: the feature film, its captions and its verdict."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
from pathlib import Path

from ..shared.adopt import explain_button
from ..shared.commands import _app_anchor, fixtures_row_html, runtime_html
from ..shared.fixtures import demo_fixtures
from dataset_view import dataset_html, fixtures_panel_html

def _link_captions(cues, links, drive=False):
    """Put the app links *inside* the narration, on the words that already name the page.

    They used to sit in a paragraph of their own — "Pages this change touches: owner detail
    · all visits · vets" — a second list of the same screens the captions were already
    walking through, in a different order and different words. A caption that says "back on
    the owner" is the natural handle for the owner page; the separate list was a handle
    nobody needed and a thing to keep in sync.

    Returns (rendered <li> items, links that found no caption). The page does not print
    the second list: it used to close the transcript as a "Not filmed. Touched by this
    change: …" row, and Victor had it removed (7 Oct 2026) — the area was not needed."""
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


# Who a worded voice is, for a film recorded before `voices.json` carried a `tip` — the
# default Fish models in record-feature-video.sh. The 🐘 has none on purpose: guessing who
# it is is the joke, and a hover that says the name spoils it.
VOICE_TIPS = {"discovery": "David Attenborough"}


def voice_films(rel: str, out_dir: Path) -> list[tuple[str, str, str, list, str]]:
    """(key, src, label, cue times, tip) of the same film in each cloned voice the recorder cut.

    Each voice is its own cut of the take (a slow voice holds a shot longer), so each lists
    its own cue times; empty for a film recorded before that, on the shared clock.

    `record-feature-video.sh` lists in `<film>.voices.json` only the voices every spoken cue
    got, and deletes the list on a run that had no key — so each radio button under the
    player is heard when pressed, and never offers a voice the film does not have. A film
    recorded before there were several voices left a single `<film>.cloned.json` instead;
    that one is the 🐘."""
    films = []
    try:
        films = json.loads((out_dir / rel.replace(".webm", ".voices.json"))
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            old = json.loads((out_dir / rel.replace(".webm", ".cloned.json"))
                             .read_text(encoding="utf-8"))
            films = [{"key": "trump", "label": old.get("label") or "🐘",
                      "video": old.get("video")}]
        except (OSError, ValueError, AttributeError):
            pass
    out = []
    for f in films if isinstance(films, list) else []:
        src = str(Path(rel).parent / str(f.get("video") or ""))
        if f.get("video") and f.get("key") and (out_dir / src).is_file():
            times = f.get("t") if isinstance(f.get("t"), list) else []
            tip = f.get("tip") if "tip" in f else VOICE_TIPS.get(str(f["key"]), "")
            out.append((str(f["key"]), src, str(f.get("label") or f["key"]), times,
                        str(tip or "")))
    return out


#: The face each known voice wears in the title row: emoji only, so three voices fit after
#: the film's title in the transcript column (Victor, 7 Oct 2026). A voice whose label is
#: already a bare emoji (the 🐘) keeps it; an unknown worded voice keeps its word.
VOICE_EMOJI = {"": "\U0001F469", "discovery": "\U0001F30D"}
#: The standard voice's hover, which is also its spoken name: its face is a glyph now.
STANDARD_VOICE_TIP = "Standard voice"


def voice_face(key: str, label: str) -> tuple[str, str]:
    """(visible face, spoken name) of one radio. A glyph face is spoken by the voice's own
    word when it has one (Discovery), as "Standard voice" for the offline one, and as a
    cloned voice for the 🐘 — never as whose: guessing who it is is the joke."""
    face = VOICE_EMOJI.get(key) or label
    if re.search(r"\w", face):
        return html.escape(face), ""
    spoken = (label if re.search(r"\w", label) and key else
              STANDARD_VOICE_TIP if not key else "cloned voice")
    return f'<span class="vs-emoji">{html.escape(face)}</span>', spoken


def voice_switch(rel: str, voices: list[tuple[str, str, str, list, str]],
                 times: list | None = None) -> str:
    """The voice radio buttons: the offline voice first, then each cloned one.
    `data-ts` is that film's cue times, which caption.js maps the reader's moment through."""
    if not voices:
        return ""
    name = "voice-" + re.sub(r"[^A-Za-z0-9]+", "-", rel)
    opts = [("", rel, "standard", times or [], STANDARD_VOICE_TIP)] + list(voices)

    def ts(t) -> str:
        return (f' data-ts="{",".join(f"{float(x):.2f}" for x in t)}"' if t else "")

    def radio(key: str, src: str, label: str, t, tip: str) -> str:
        # A glyph gives a screen reader nothing to say, so the radio is named
        # (`voice_face`). A hover names the speaker only where the voice declares one
        # (Discovery → David Attenborough) and on the standard voice, whose face no longer
        # says "standard"; the 🐘 has none.
        face, spoken = voice_face(key, label)
        hover = f' data-tip="{html.escape(tip, quote=True)}"' if tip else ""
        return (f'<label{hover}>'
                f'<input type="radio" name="{html.escape(name)}" '
                f'value="{html.escape(key)}" data-src="{html.escape(src)}"{ts(t)}'
                + (f' aria-label="{html.escape(spoken, quote=True)}"' if spoken else "")
                + f'{" checked" if not key else ""}> {face}</label>')

    return ('<div class="voice-switch" role="radiogroup" aria-label="Narration voice">'
            + "".join(radio(*o) for o in opts) + "</div>")


# ── what the content file used to have to type ─────────────────────────────────────
#
# The Running-app row and the dotted links in the transcript were both drawn only from
# what the review's model wrote into the `video` section: `runtime` and `appLinks`. Eval
# run 5's model wrote no `video` section at all, so its Demo tab had neither — while every
# fact behind them was on disk: how the film's own instance is started, named and stopped
# (`steps.video.app`), and which screens this branch changed (the design-system audit).
# What the content file says still wins; this is what the page says when it says nothing.


def _project_root(out_dir: Path) -> Path | None:
    """The repository the review is of: the nearest directory above the page that holds
    `human-review.json`, which is where `run-steps.py` reads the same block from."""
    here = Path(out_dir).resolve()
    for d in (here, *here.parents):
        if (d / "human-review.json").is_file():
            return d
    # A page written outside the repository (`--out` elsewhere) is still built from inside
    # it: the build runs at the repository's top level.
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    return Path(top) if top and (Path(top) / "human-review.json").is_file() else None


def derived_runtime(out_dir: Path) -> dict:
    """`runtime` off `steps.video.app` — the very commands the film was made with, for the
    commit under review, so the reader's Start brings up what the film showed.

    `reset` only as a path (`/__reset`): the row's Reset button POSTs it to whatever
    instance is up, and a shell command is nothing a browser can press."""
    root = _project_root(out_dir)
    if root is None:
        return {}
    try:
        cfg = json.loads((root / "human-review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    app = ((cfg.get("steps") or {}).get("video") or {}).get("app")
    if not isinstance(app, dict) or not app.get("up"):
        return {}

    def git(*args):
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    sha = git("rev-parse", "HEAD")
    if not sha:
        return {}
    slots = {"sha": sha, "shortsha": git("rev-parse", "--short", "HEAD") or sha[:8]}

    def line(template: str) -> str:
        for name, value in slots.items():
            template = (template.replace("{" + name + "}", value)
                        .replace("{" + name.replace("sha", "SHA") + "}", value))
        return f"cd {shlex.quote(str(root))} && {template}"
    rt = {"command": line(app["up"]), "base": ""}
    if app.get("down"):
        rt["stop"] = line(app["down"])
    if app.get("url"):
        rt["urlCommand"] = line(app["url"])
    if str(app.get("reset") or "").startswith("/"):
        rt["reset"] = app["reset"]
    return rt


def _screen_changed(screen: dict) -> bool:
    """`ds-audit.py`'s own test for a screen this branch changed (`screen_changed`)."""
    delta = screen.get("delta") or {}
    dom = delta.get("dom") or {}
    if dom.get("added") or dom.get("removed") or dom.get("changed"):
        return True
    if any((st or {}).get("status") == "restyled"
           for st in (delta.get("elements") or {}).values()):
        return True
    counts = screen.get("summary") or {}
    return bool(counts.get("regressions") or counts.get("improvements"))


def derived_app_links(out_dir: Path, cues: list[dict]) -> list[dict]:
    """`appLinks` off the design-system audit: each screen it found changed, linked on the
    first caption that names it — `Owners` in "The Owners grid is now paginated…" becomes a
    link into `/owners` on whatever instance is up. A changed screen no caption names has
    no words to ride on, so it is not shown."""
    try:
        doc = json.loads((Path(out_dir) / "assets" / "ds-audit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    links = []
    for screen in doc.get("screens") or [] if isinstance(doc, dict) else []:
        name, route = str(screen.get("screen") or ""), str(screen.get("route") or "")
        if not (name and route.startswith("/") and _screen_changed(screen)):
            continue
        link = {"href": route, "label": name.lower()}
        rx = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
        for cue in cues:
            m = rx.search(str(cue.get("text") or ""))
            if m:
                link["anchor"] = m.group(0)
                break
        links.append(link)
    return links


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
    # The shell commands come from `steps.video.app` whenever it has them, over anything the
    # content file typed: those are pinned to this checkout's real path and commit, while a
    # model's hand-written `cd ~/workspace/petclinic-pr && …` went stale the day the folder
    # was renamed (3 Oct 2026) and every Start said "start failed". The content file keeps
    # what the config cannot know — `base`, `drive`, its own `reset`.
    rt = dict(s.get("runtime") or {})
    for key, value in derived_runtime(out_dir).items():
        if value and (key in ("command", "stop", "urlCommand") or not rt.get(key)):
            rt[key] = value
    links = s["appLinks"] if "appLinks" in s else derived_app_links(out_dir, cues)
    items, _ = _link_captions(cues, links, bool(rt.get("drive")))
    voices = voice_films(rel, out_dir) if (out_dir / rel).is_file() else []
    # The same take, cue for cue, in every voice, so caption.js swaps the source and keeps
    # the second the reader was at; the transcript and its timestamps are shared by all.
    switch = voice_switch(rel, voices, [c["t"] for c in cues if "t" in c]
                          if any(v[3] for v in voices) else [])
    player = (f'<video controls preload="metadata" src="{html.escape(rel)}"></video>'
              if (out_dir / rel).is_file() else
              f'<p class="embedded-note"><b>Not filmed.</b> <code>{html.escape(rel)}</code> '
              'was not produced by this run, so there is no player here — the narration '
              'below is what the recording would have shown, and it is the only part of '
              'this section that is not evidence.</p>')
    # The verdict sits OUTSIDE the wrap, not inside it beside the player: `.vidwrap` is a
    # two-column grid, so a band emitted as one of its children takes a column and stands
    # next to the picture instead of across the top of it. What it contradicts is the
    # picture, so it has to be the thing read first, full width.
    head = ""
    if rt:
        root = _project_root(out_dir)
        fixtures = demo_fixtures(root)
        can_reset = bool(rt.get("reset"))
        # The DB fixtures are a card of their own under the Running app one (Victor, 9 Oct
        # 2026), not a second row inside it: `runtime_html` still draws that row, so it is
        # cut back out here, exactly as drawn — and once it stops drawing it, this is a no-op.
        head = runtime_html(rt, fixtures=fixtures).replace(
            fixtures_row_html(fixtures, can_reset), "", 1)
        # Each fixture's tables, under its own header: computed from the project's own seed
        # and fixture SQL (`dataset_view.py`), so they need no app running.
        data = dataset_html(root)
        if data or len(fixtures) > 1 or can_reset:
            head += fixtures_panel_html(fixtures, can_reset, explain_button("behaviour.seed"))
            head += data
    # The film's title heads the transcript column, not a row of its own above the player:
    # a row across the page held two words and left the rest of it empty (Victor, 7 Oct
    # 2026), and the player now starts right under the Running app band. The tab's presses
    # (`place_tab_reruns` puts them at the end of this `h2`) re-record the film, not the app;
    # the voice switch follows them on the same row. It used to ride the Running app band,
    # but which voice narrates is a fact about the film, not about the app (Victor, 7 Oct).
    return (head + video_verdict_html(rel, out_dir)
            + f'<div class="vidwrap">{player}<div class="vidside">'
            f'<div class="vidhead"><h2 class="tabtitle">Intro video</h2>{switch}</div>'
            f'<ol class="transcript">{items}</ol></div></div>')


def embed_html(s, out_dir: Path) -> str:
    """Another tool's whole report, embedded in a shadow root rather than framed.

    It used to be an iframe, and an iframe is a second scrollbar or a second document
    sized by messages (Victor, 5 Oct 2026: one scrollbar, the page's, and the report at
    its full height). Read at build time and pasted, so the page still opens off disk:
    the report's stylesheets and markup go into a `<template>`, a few lines of script move
    them into a shadow root on the host `<div>` — the one boundary a stylesheet cannot
    cross, either way — and the report's own scripts follow, each inline one tagged
    `data-dv-host` so it knows which root is its own (`openapi-visual-diff.py` reads it off
    `document.currentScript`). An external script is loaded `defer`: a blocking CDN fetch
    in the middle of the body would hold every tab after this one, and the page's own
    scripts with them.

    `aria-label`, not `title`: a `title` is a native tooltip, and this page has exactly one
    tooltip component."""
    e = s.get("embed")
    if not e:
        return ""
    # `src` may carry a fragment — a report that reads its own hash can be opened on a
    # particular view (`…#only-touched`). Only the path in front of it is a file; the
    # fragment rides on the host as `data-hash`, since the page's own hash names its tab.
    path, _, frag = e["src"].partition("#")
    if not (out_dir / path).is_file():
        # The tool that writes it is an optional install. Say which one is missing rather
        # than embedding nothing.
        return (f'<p class="sub">No embedded report at <code>{html.escape(path)}</code>'
                + (f' — { e["missing"]}' if e.get("missing") else "")
                + ".</p>")
    doc = (out_dir / path).read_text(encoding="utf-8")
    style_re = re.compile(r'<link\b[^>]*\brel=["\']?stylesheet\b[^>]*>'
                          r'|<style\b[^>]*>.*?</style\s*>', re.S | re.I)
    body_re = re.compile(r'<body\b[^>]*>(.*)</body\s*>', re.S | re.I)
    script_re = re.compile(r'<script\b([^>]*)>(.*?)</script\s*>', re.S | re.I)
    # Attaches the shadow root and moves the report's styles and markup into it, before the
    # report's own scripts run. `__ID__` is the host's id; the template is the next sibling.
    attach = ("<script>(function(){var h=document.getElementById('__ID__'),"
              "t=document.getElementById('__ID__-tpl');if(!h||!t)return;"
              "(h.shadowRoot||h.attachShadow({mode:'open'})).appendChild(t.content);"
              "t.remove();})();</script>")
    m = body_re.search(doc)
    head, body = (doc[:m.start()], m.group(1)) if m else ("", doc)
    hid = f'embed-{s.get("id") or "report"}'
    scripts = []
    for sm in script_re.finditer(body):
        attrs = sm.group(1)
        if re.search(r'\bsrc=', attrs):
            if not re.search(r'\b(?:defer|async)\b', attrs):
                attrs += " defer"
            scripts.append(f"<script{attrs}></script>")
        else:
            scripts.append(f'<script{attrs} data-dv-host="{hid}">{sm.group(2)}</script>')
    inner = "".join(x.group(0) for x in style_re.finditer(head)) + script_re.sub("", body)
    return (f'<div class="{html.escape(e.get("class", "embed"))}" id="{hid}" role="region"'
            + (f' data-hash="#{html.escape(frag)}"' if frag else "")
            + f' aria-label="{html.escape(e.get("label", ""))}"></div>'
            + f'<template id="{hid}-tpl">{inner}</template>'
            + attach.replace("__ID__", hid) + "".join(scripts))
