#!/usr/bin/env python3
"""Screenshot every tab of a finished /human-review report and build a static gallery.

review.html is an interactive page: thirteen tabs, only one panel in the DOM at a time
(`panel.hidden`), a sticky masthead, and a feature video. A single screenshot of it shows
one tab and nothing else, so this driver walks the tabstrip — clicking each tab, waiting
for its panel to stop growing rather than for a fixed number of milliseconds — and takes
one full-page shot per tab.

The video is the one thing a screenshot cannot carry, so it is copied verbatim next to the
images and embedded in the gallery. That is a deliberate deviation from "just images".

Playwright is imported lazily: CI runs `--help` on every script in this directory with
nothing but pytest and pillow installed.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import time
import sys
from pathlib import Path

VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900
DEVICE_SCALE = 2

# How long a panel is allowed to keep changing size before we shoot it anyway. Not a sleep:
# every poll awaits two animation frames, so a page that settles early leaves immediately.
SETTLE_TIMEOUT_MS = 20000
STABLE_READINGS = 2

# Once the images and fonts are in, the only thing that can still move the page is a widget
# arguing with itself — the OpenAPI diff's iframe autosizes four pixels per frame and never
# converges. Give that case a short grace and shoot, instead of burning the full timeout.
QUIET_GRACE_MS = 3000

# Chromium cannot compose a texture taller than this. A panel that overruns it is retried
# at CSS scale, which costs sharpness but produces a whole image instead of an exception.
MAX_DEVICE_PIXELS = 16000


# --------------------------------------------------------------------------------------
# page-side helpers


# Kills the transitions and the caret, so two runs of the same page produce the same bytes.
FREEZE_CSS = """
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    caret-color: transparent !important;
  }
  html { scroll-behavior: auto !important; }
"""

# One reading of everything that would make a screenshot land mid-layout. Resolved after two
# animation frames so it describes a painted state, not a queued one.
PROBE_JS = """
(sel) => new Promise((resolve) => {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const panel = sel ? document.querySelector(sel) : null;
    const scope = panel || document.body;
    const imgs = Array.from(scope.querySelectorAll('img'));
    // `complete` flips true on error too, which is what we want: a broken src is settled,
    // it is just never going to paint. Waiting on naturalWidth would hang on it forever.
    const pending = imgs.filter((i) => !i.complete).length;
    resolve({
      docHeight: document.documentElement.scrollHeight,
      panelHeight: panel ? panel.scrollHeight : 0,
      pendingImages: pending,
      fonts: document.fonts ? document.fonts.status : 'loaded',
    });
  }));
})
"""

# A `loading="lazy"` image below the fold never starts fetching, and a panel that was
# `hidden` until a moment ago is nothing but below the fold — the UX tab's 21 before/after
# shots would all have been blank. Opt every image in the panel back into eager loading
# before anything waits on them.
EAGER_IMAGES_JS = """
(sel) => {
  const scope = sel ? document.querySelector(sel) : document.body;
  if (!scope) return 0;
  const imgs = Array.from(scope.querySelectorAll('img'));
  for (const img of imgs) {
    img.loading = 'eager';
    img.removeAttribute('loading');
  }
  return imgs.length;
}
"""

# Videos default to a black rectangle: nothing has been decoded, so there is no first frame
# to paint. Seek into the body of the clip and wait for the `seeked` event — the timeouts are
# escape hatches for a codec that never answers, not pacing.
NEUTRALIZE_VIDEO_JS = """
async (sel) => {
  const scope = sel ? document.querySelector(sel) : document.body;
  if (!scope) return 0;
  const videos = Array.from(scope.querySelectorAll('video'));
  for (const v of videos) {
    v.autoplay = false;
    v.loop = false;
    v.removeAttribute('autoplay');
    v.removeAttribute('loop');
    v.controls = true;
    try { v.pause(); } catch (e) { /* not playable, nothing to pause */ }
    if (v.readyState < 1) {
      v.preload = 'auto';
      try { v.load(); } catch (e) { /* ignore */ }
      await new Promise((r) => {
        const done = () => r();
        v.addEventListener('loadedmetadata', done, {once: true});
        setTimeout(done, 5000);
      });
    }
    const d = v.duration;
    if (isFinite(d) && d > 0.2) {
      const target = Math.min(d * 0.3, d - 0.05);
      await new Promise((r) => {
        const done = () => r();
        v.addEventListener('seeked', done, {once: true});
        try { v.currentTime = target; } catch (e) { done(); }
        setTimeout(done, 5000);
      });
    }
    try { v.pause(); } catch (e) { /* ignore */ }
  }
  // Narration is an <audio> the Demo panel starts on `panelshow`; silence it too.
  for (const a of Array.from(document.querySelectorAll('audio'))) {
    try { a.pause(); a.muted = true; } catch (e) { /* ignore */ }
  }
  return videos.length;
}
"""

# The visible label of a tab, minus the little count/dot badge it carries.
TAB_META_JS = """
() => {
  const strip = document.querySelector('.tabstrip');
  if (!strip) return [];
  return Array.from(strip.querySelectorAll('button.tab')).map((b) => {
    const clone = b.cloneNode(true);
    clone.querySelectorAll('span').forEach((s) => s.remove());
    return {
      title: (clone.textContent || '').trim(),
      panel: b.getAttribute('aria-controls') || '',
    };
  });
}
"""


def slugify(text: str) -> str:
    """Lowercase, ASCII-ish, dash-separated — safe as a file name and as a URL segment."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "tab"


def wait_settled(page, panel_selector: str | None) -> None:
    """Block until the layout stops moving, images are decoded and fonts are loaded."""
    previous = None
    stable = 0
    deadline = time.monotonic() + SETTLE_TIMEOUT_MS / 1000
    grace_deadline = None
    # Each probe costs two animation frames; the deadlines are ceilings, not a schedule — a
    # page that is already still exits on the second reading.
    while time.monotonic() < deadline:
        reading = page.evaluate(PROBE_JS, panel_selector)
        key = (reading["docHeight"], reading["panelHeight"])
        quiet = reading["pendingImages"] == 0 and reading["fonts"] != "loading"
        if quiet:
            if grace_deadline is None:
                grace_deadline = time.monotonic() + QUIET_GRACE_MS / 1000
            if key == previous:
                stable += 1
                if stable >= STABLE_READINGS:
                    return
            else:
                stable = 0
            if time.monotonic() > grace_deadline:
                print(
                    "  ~ content is in but the layout keeps creeping; shooting it",
                    file=sys.stderr,
                )
                return
        else:
            stable = 0
        previous = key
    print("  ! layout never settled; shooting anyway", file=sys.stderr)


def shoot(page, out_path: Path) -> None:
    """Full-page screenshot, degrading to CSS scale when the page overruns Chromium."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    height = page.evaluate("() => document.documentElement.scrollHeight")
    if height * DEVICE_SCALE > MAX_DEVICE_PIXELS:
        page.screenshot(path=str(out_path), full_page=True, scale="css")
        return
    try:
        page.screenshot(path=str(out_path), full_page=True)
    except Exception as exc:  # noqa: BLE001 — any capture failure is worth a whole image
        print(f"  ! {out_path.name}: {exc}; retrying at CSS scale", file=sys.stderr)
        page.screenshot(path=str(out_path), full_page=True, scale="css")


def capture(playwright, source: Path, out_dir: Path, scheme: str) -> list[dict]:
    """Shoot every tab of `source` under one colour scheme; returns the tab manifest."""
    browser = playwright.chromium.launch()
    context = browser.new_context(
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        device_scale_factor=DEVICE_SCALE,
        color_scheme=scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    shots: list[dict] = []
    try:
        page.goto(source.resolve().as_uri(), wait_until="load")
        page.add_style_tag(content=FREEZE_CSS)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 — a page that never idles is still shootable
            pass

        tabs = page.evaluate(TAB_META_JS)
        if not tabs:
            print(f"  [{scheme}] no tabstrip — one full-page shot", file=sys.stderr)
            page.evaluate(EAGER_IMAGES_JS, None)
            page.evaluate(NEUTRALIZE_VIDEO_JS, None)
            wait_settled(page, None)
            name = "01-review.png"
            shoot(page, out_dir / scheme / name)
            shots.append({"title": "Review", "file": name, "panel": ""})
            return shots

        buttons = page.locator(".tabstrip button.tab")
        for index, tab in enumerate(tabs):
            selector = f"#{tab['panel']}" if tab["panel"] else None
            buttons.nth(index).click()
            if selector:
                page.wait_for_selector(selector, state="visible", timeout=15000)
            # Lazy content inside a panel only lays out once the panel is unhidden, so the
            # video pass and the settle probe both run after the click, never before.
            page.evaluate(EAGER_IMAGES_JS, selector)
            page.evaluate(NEUTRALIZE_VIDEO_JS, selector)
            page.evaluate("() => window.scrollTo(0, 0)")
            wait_settled(page, selector)
            name = f"{index + 1:02d}-{slugify(tab['title'])}.png"
            shoot(page, out_dir / scheme / name)
            size = (out_dir / scheme / name).stat().st_size
            print(f"  [{scheme}] {name}  ({size // 1024} KB)", file=sys.stderr)
            shots.append({"title": tab["title"], "file": name, "panel": tab["panel"]})
        return shots
    finally:
        context.close()
        browser.close()


# --------------------------------------------------------------------------------------
# gallery


GALLERY_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --panel: #ffffff; --fg: #1c1b1a; --muted: #6b6864;
  --line: #e3e0dc; --accent: #9a3412;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #17161a; --panel: #201f24; --fg: #eceaf0; --muted: #a09da8;
          --line: #34323a; --accent: #f0a06a; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 3rem 1.25rem 5rem; background: var(--bg); color: var(--fg);
       font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 68rem; margin: 0 auto; }
h1 { font-size: 1.9rem; letter-spacing: -0.02em; margin: 0 0 .35rem; }
.lede { color: var(--muted); margin: 0 0 1.5rem; }
.lede code { font-size: .9em; }
h2 { font-size: 1.15rem; margin: 2.75rem 0 .6rem; }
h2 .num { color: var(--muted); font-weight: 500; margin-right: .5rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em;
       background: color-mix(in srgb, var(--fg) 8%, transparent); padding: .1em .35em; border-radius: 4px; }
a { color: var(--accent); }
figure { margin: 0 0 1rem; }
figure img { display: block; width: 100%; height: auto; border: 1px solid var(--line);
             border-radius: 10px; background: var(--panel); }
.note { border-left: 3px solid var(--accent); background: var(--panel);
        padding: .85rem 1.1rem; border-radius: 0 8px 8px 0; color: var(--muted); font-size: .93rem;
        margin: 0 0 2rem; }
.toc { list-style: none; display: flex; flex-wrap: wrap; gap: .4rem .5rem; padding: 0; margin: 0 0 2rem; }
.toc a { font-size: .85rem; text-decoration: none; border: 1px solid var(--line);
         border-radius: 999px; padding: .2rem .7rem; color: var(--muted); background: var(--panel); }
.toc a:hover { border-color: var(--accent); color: var(--accent); }
video { width: 100%; border: 1px solid var(--line); border-radius: 10px; background: #000; }
.dark-only { display: none; }
@media (prefers-color-scheme: dark) {
  .light-only { display: none; }
  .dark-only { display: block; }
}
footer { margin-top: 3.5rem; color: var(--muted); font-size: .88rem;
         border-top: 1px solid var(--line); padding-top: 1rem; }
"""


def strip_tags(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", markup or "").strip()


def build_gallery(
    out_dir: Path,
    title: str,
    subtitle_html: str,
    shots_by_scheme: dict[str, list[dict]],
    videos: list[str],
    slug: str,
) -> Path:
    schemes = [s for s in ("light", "dark") if s in shots_by_scheme]
    primary = shots_by_scheme[schemes[0]]

    toc = "".join(
        f'<li><a href="#{slugify(s["title"])}">{html.escape(s["title"])}</a></li>'
        for s in primary
    )

    sections = []
    for index, shot in enumerate(primary):
        anchor = slugify(shot["title"])
        figures = []
        for scheme in schemes:
            match = next(
                (s for s in shots_by_scheme[scheme] if s["title"] == shot["title"]), None
            )
            if not match:
                continue
            cls = f"{scheme}-only" if len(schemes) > 1 else ""
            src = f"{scheme}/{match['file']}"
            figures.append(
                f'<a class="{cls}" href="{src}">'
                f'<img src="{src}" alt="{html.escape(shot["title"])} tab, {scheme} theme" '
                f'loading="lazy"></a>'
            )
        sections.append(
            f'<section id="{anchor}">\n'
            f'<h2><span class="num">{index + 1:02d}</span>{html.escape(shot["title"])}</h2>\n'
            f"<figure>{''.join(figures)}</figure>\n"
            f"</section>"
        )

    video_block = ""
    if videos:
        clips = "".join(
            f'<video controls preload="metadata" src="assets/{html.escape(v)}"></video>'
            for v in videos
        )
        video_block = (
            '<section id="feature-video">\n'
            '<h2><span class="num">▶</span>The feature, filmed</h2>\n'
            "<p>The Demo tab plays this clip. A screenshot cannot carry it, so the file "
            "itself travels with the snapshot.</p>\n"
            f"{clips}\n</section>"
        )

    scheme_note = (
        "Every tab was shot twice — light and dark — and the page below follows your system theme."
        if len(schemes) > 1
        else "The report is not theme-aware, so there is a single set of shots."
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — /human-review snapshot</title>
<style>{GALLERY_CSS}</style>
</head>
<body>
<main>
  <h1>{html.escape(title)}</h1>
  <p class="lede">{subtitle_html}</p>
  <p class="note">This is a <b>static snapshot</b> of an interactive page: one full-page
  screenshot per tab of the <code>review.html</code> that
  <a href="https://github.com/victorrentea/human-review"><code>/human-review</code></a> built
  locally. Nothing here is clickable the way the real page is — no tab switching, no VS Code
  deep links, no tooltips. {scheme_note} Click any screenshot to open it full size.</p>
  <ul class="toc">{toc}</ul>
{video_block}
{chr(10).join(sections)}
  <footer>Snapshot <code>{html.escape(slug)}</code> · {len(primary)} tabs ×
  {len(schemes)} theme{"s" if len(schemes) > 1 else ""} · published from the
  <code>demo/</code> directory of
  <a href="https://github.com/victorrentea/human-review">victorrentea/human-review</a>.</footer>
</main>
</body>
</html>
"""
    target = out_dir / "index.html"
    target.write_text(page, encoding="utf-8")
    return target


# --------------------------------------------------------------------------------------


def resolve_source(raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_dir():
        path = path / "review.html"
    if not path.is_file():
        raise SystemExit(f"shoot-review: no review.html at {raw}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shoot-review.py",
        description=(
            "Screenshot every tab of a /human-review review.html and write a static "
            "gallery (index.html + shots/) next to the images."
        ),
    )
    parser.add_argument(
        "source", nargs="?", default=".human-review",
        help="review.html, or the directory holding it (default: .human-review)",
    )
    parser.add_argument("--out", required=False, help="output directory (created/replaced)")
    parser.add_argument("--slug", default="", help="name of the snapshot, used in the footer")
    parser.add_argument("--title", default="", help="override the title read from content.json")
    parser.add_argument(
        "--manifest", default="",
        help="also write the shot manifest as JSON to this path",
    )
    args = parser.parse_args(argv)

    if not args.out:
        parser.error("--out is required")

    source = resolve_source(args.source)
    src_dir = source.parent
    out_dir = Path(args.out).expanduser()
    slug = args.slug or slugify(src_dir.parent.name)

    title = args.title
    subtitle = ""
    content = src_dir / "content.json"
    if content.is_file():
        try:
            data = json.loads(content.read_text(encoding="utf-8"))
            title = title or strip_tags(data.get("title", ""))
            subtitle = data.get("subtitle", "") or ""
        except (ValueError, OSError) as exc:
            print(f"shoot-review: ignoring content.json ({exc})", file=sys.stderr)
    title = title or slug

    markup = source.read_text(encoding="utf-8", errors="replace")
    theme_aware = "prefers-color-scheme" in markup
    schemes = ["light", "dark"] if theme_aware else ["light"]
    print(
        f"shoot-review: {source} — {'theme-aware' if theme_aware else 'single theme'}",
        file=sys.stderr,
    )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "shoot-review: playwright is not installed for this interpreter.\n"
            "              run publish-demo-shots.sh, which provisions it, or:\n"
            "              pip install playwright && playwright install chromium"
        )

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    shots_by_scheme: dict[str, list[dict]] = {}
    with sync_playwright() as playwright:
        for scheme in schemes:
            shots_by_scheme[scheme] = capture(playwright, source, out_dir, scheme)

    # The feature video: the one part of the report an image cannot stand in for. The
    # `.raw.webm` beside it is the unannotated take, so it stays behind.
    videos = []
    assets = src_dir / "assets"
    if assets.is_dir():
        for clip in sorted(assets.glob("*.webm")):
            if clip.name.endswith(".raw.webm"):
                continue
            (out_dir / "assets").mkdir(exist_ok=True)
            shutil.copy2(clip, out_dir / "assets" / clip.name)
            videos.append(clip.name)

    build_gallery(out_dir, title, subtitle, shots_by_scheme, videos, slug)

    manifest = {
        "slug": slug,
        "title": title,
        "subtitle": subtitle,
        "themeAware": theme_aware,
        "schemes": schemes,
        "videos": videos,
        "tabs": [s["title"] for s in shots_by_scheme[schemes[0]]],
        "shots": sum(len(v) for v in shots_by_scheme.values()),
    }
    if args.manifest:
        Path(args.manifest).expanduser().write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
