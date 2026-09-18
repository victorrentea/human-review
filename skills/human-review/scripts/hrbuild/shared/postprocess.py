"""Rewrites over the finished document, after every tab has rendered."""
from __future__ import annotations

import json
import re
import sys

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
