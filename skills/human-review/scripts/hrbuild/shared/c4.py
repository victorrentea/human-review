"""The repository's own C4 views, as Structurizr draws them (`structurizr-views.py`).

One card per view, on the Structure tab, in the gallery's card: title, the view's own
description, the UNCHANGED badge when the branch left it alone, the DSL file on the right,
and — when the DSL moved — the New/Old switch every diagram card wears. There is no Diff
pane: Structurizr draws a workspace, not the difference between two, and a delta drawn by
anything else would no longer be Structurizr's picture. The two sides are its two renders.

The pictures are `<img>`s of the SVGs Structurizr exported, not inlined markup. Its SVG is
a JointJS document with a `<style>` of its own and white text on purpose-picked fills; the
page's inliner (`svg.py`) recolours PlantUML's palette to the page's variables, and run
over this it would repaint Structurizr's boxes in the page's colours. As an image it stays
exactly what Structurizr drew. Both of Structurizr's own renderings travel — its light
mode and its dark mode — and the page's colour scheme picks one (`css/c4.css`), so a dark
page shows Structurizr's dark render rather than a white slab.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
from pathlib import Path

from .diagrams import (UNCHANGED, UNCHANGED_BADGE, _source_link, dgm_views_html,
                       read_manifest, shorten_dgm_src)

#: Where `structurizr-views.py` writes, relative to the review directory.
C4_DIR = "assets/c4"

C4_VIEWBOX = re.compile(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)\s*"')

#: The scale a Structurizr render is shown at, at most. Its boxes are drawn for a canvas
#: (24px type in a 450px box); at full size one container view is three screens wide. Half
#: size is the size the type reads at on a page; a narrower card shrinks it further.
C4_SCALE = 0.5

#: …but never below this: a wide view (petclinic's C2 is 3020 units across) fitted to a
#: 1078px card set its labels at 8px. Past this the card scrolls sideways instead.
C4_MIN_SCALE = 0.45


def _c4_img(path: Path, cls: str, alt: str) -> str:
    svg = path.read_text(encoding="utf-8")
    m = C4_VIEWBOX.search(svg)
    size = (f' width="{round(float(m[1]) * C4_SCALE)}" height="{round(float(m[2]) * C4_SCALE)}"'
            f' style="min-width:{round(float(m[1]) * C4_MIN_SCALE)}px"' if m else "")
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return (f'<img class="{cls}" src="data:image/svg+xml;base64,{data}"{size} '
            f'alt="{html.escape(alt, quote=True)}" decoding="async">')


def _c4_picture(row: dict, side: str, assets: Path) -> str:
    """Structurizr's light and dark render of one side, the page's scheme picking one."""
    imgs = []
    for mode in ("light", "dark"):
        name = (row.get(f"{side}_{mode}") or "").strip()
        if name and (assets / name).is_file():
            imgs.append(_c4_img(assets / name, f"c4-{mode}",
                                f"{row['name']} — drawn by Structurizr"))
    if not imgs:
        return ""
    if len(imgs) == 1:          # one mode only: show it in both schemes
        imgs[0] = imgs[0].replace('class="c4-light"', 'class="c4-only"') \
                         .replace('class="c4-dark"', 'class="c4-only"')
    return f'<div class="svgbox c4box">{"".join(imgs)}</div>'


def _c4_body(row: dict, assets: Path) -> tuple[str, bool]:
    """The picture(s) of one view, and whether the card toggles between two of them."""
    new, old = _c4_picture(row, "new", assets), _c4_picture(row, "old", assets)
    if row.get("status") == "modified" and new and old:
        views = dgm_views_html([("new", new), ("old", old)], initial="new")
        # Two renders and no delta: the Diff button would switch to a pane that is not
        # there. The New/Old button alone is the whole control.
        views = re.sub(r'<button type="button" class="dgm-diff"[^>]*>Diff</button>', "",
                       views, count=1)
        views = views.replace('data-tip="The diagram without diff. Click again to swap."',
                              'data-tip="Structurizr\'s render on this branch, and at the '
                              'merge-base. Click to swap."', 1)
        return views, True
    return (new or old
            or '<p class="sub">not rendered — re-run the <code>c4</code> step</p>'), False


def _c4_badge(status: str) -> str:
    if status == UNCHANGED:
        return UNCHANGED_BADGE
    if status == "added":
        return '<span class="badge sev-high">new</span>'
    if status == "modified":
        return ""
    return f'<span class="badge sev-low">{html.escape(status)}</span>'


#: The DSL keywords that open a view (`views { component backend "C3" { … } }`).
_VIEW_KEYWORDS = ("systemLandscape", "systemContext", "container", "component", "dynamic",
                 "deployment", "filtered", "custom", "image")

#: `!include other.dsl` — the closure a view may be defined anywhere in.
_DSL_INCLUDE = re.compile(r'^[ \t]*!include[ \t]+(?:"([^"\n]+)"|(\S+))', re.M)


def _view_definition(root: Path, source: str, key: str) -> tuple[str, int] | None:
    """`(file, line)` of the DSL line that defines view `key`, following `!include`s.

    The header's file link opens the DSL *at the view*, not at line 1 of a workspace whose
    views sit sixty lines down, possibly in another file. The line goes into the link only:
    a line number printed on the page is noise (Victor). None for a key Structurizr made up
    (a view with no key in the DSL) — the link then opens the workspace file."""
    rx = re.compile(r'^[ \t]*(?:' + "|".join(_VIEW_KEYWORDS) + r')\b'
                    r'(?:[ \t]+(?:"[^"\n]*"|[^\s"{]+)){0,2}?[ \t]+'
                    r'(?:"' + re.escape(key) + r'"|' + re.escape(key) + r')(?=[\s{]|$)', re.M)
    seen, todo = set(), [Path(source)]
    while todo:
        rel = todo.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = rx.search(text)
        if m:
            return rel.as_posix(), text.count("\n", 0, m.start()) + 1
        for inc in _DSL_INCLUDE.finditer(text):
            target = inc[1] or inc[2]
            if "://" not in target:
                todo.append(Path(os.path.normpath(rel.parent / target)))
    return None


def _dsl_link(root: Path, source: str, key: str) -> str:
    """The file on the right of the header, opening VS Code at the view's definition."""
    where = _view_definition(root, source, key)
    if not where:
        return _source_link(source, root)
    rel, line = where
    href = html.escape(f"vscode://file/{(root / rel).resolve()}:{line}:1", quote=True)
    tip = html.escape(f"Open in VS Code where view {key} is defined: {rel}", quote=True)
    return shorten_dgm_src(f'<a class="dgm-src" href="{href}" data-tip="{tip}">'
                           f'{html.escape(rel)}</a>')


#: `structurizr-views.py:tested_note`'s sentence for a view a test checks. Parsed back
#: here, so its wording is pinned by `test_structurizr_views.py`.
_CHECKED_NOTE = re.compile(r"^Its (?:components|containers)(?: and their arrows)? are checked "
                          r"against the code by (?P<names>.+?)(?:; its arrows are not)?\.(?=\s|$)"
                          r"(?P<rest>.*)$", re.S)


def _test_file(root: Path, name: str) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "--", name, f"*/{name}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hits = [p for p in out.splitlines() if p]
    return root / hits[0] if hits else None


def _checked_label(note: str, root: Path) -> tuple[str, str]:
    """`(header label, what stays under the card)` for one view's test note.

    *Its components and their arrows are checked against the code by C3ArchTest.java.* was a
    sentence under the picture, read after the picture; the fact is the card's credential,
    so it goes in the header as *ArchUnit-checked by C3ArchTest* (Victor, 9 Oct 2026), the
    whole sentence on hover and the test's name the way into it. A note that is not that
    sentence (hand-maintained, uncompared) stays under the card, where it was."""
    m = _CHECKED_NOTE.match(note or "")
    if not m:
        return "", note
    links, archunit = [], True
    for name in (n.strip() for n in m["names"].split(",")):
        path = _test_file(root, name)
        text = ""
        if path:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        archunit &= "com.tngtech.archunit" in text
        face = html.escape(Path(name).stem)
        links.append(f'<a href="{html.escape(f"vscode://file/{path.resolve()}:1:1", quote=True)}">'
                     f'{face}</a>' if path else face)
    first = note[:len(note) - len(m["rest"])].strip()
    label = (f'<span class="c4-check" data-tip="{html.escape(first, quote=True)}">'
             f'{"ArchUnit-checked" if archunit else "Checked"} by {", ".join(links)}</span>')
    return label, m["rest"].strip()


def render_c4(block: dict, root: Path, out_dir: Path) -> tuple[str, int, int]:
    """The `c4` block: (html, weight, changes), as `render_block` wants them."""
    assets = out_dir / (block.get("dir") or C4_DIR)
    try:
        verdict = json.loads((assets / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        verdict = {}
    if verdict.get("state") == "none":
        return "", 0, 0
    rows = read_manifest(assets / "MANIFEST.tsv")
    # What went wrong, above whatever was drawn: a branch whose DSL no longer parses has
    # no picture, and saying nothing would read as "no views".
    problem = (f'<p class="sub c4-problem">Structurizr: {html.escape(verdict["reason"])}</p>'
               if rows and verdict.get("reason") else "")
    if not rows:
        if not verdict:
            return "", 0, 0
        why = verdict.get("reason") or "the c4 step drew nothing"
        dsl = ", ".join(Path(w).name for w in verdict.get("workspaces") or [])
        broken = verdict.get("state") == "failed"
        return (f'<p class="sub c4-none">C4 views{f" in <code>{html.escape(dsl)}</code>" if dsl else ""}'
                f' not drawn by Structurizr — {html.escape(why)}.'
                + ("" if broken else " The next refresh tries again.") + '</p>',
                1, 1 if broken else 0)
    parts, changed = [], 0
    for r in rows:
        status = r.get("status") or ""
        changed += status != UNCHANGED
        body, toggles = _c4_body(r, assets)
        desc = r.get("description") or r.get("title") or ""
        check, note = _checked_label((r.get("note") or "").strip(), root)
        # What the card is: one of the workspace's views, the DSL's description on hover.
        # The description used to be printed in the header (*Repository Layer — nearest
        # neighbours*) and said nothing the picture below does not (Victor, 9 Oct 2026).
        kind = (f'<span class="c4-view" data-tip="'
                + html.escape(f"A view of the Structurizr workspace{': ' + desc if desc else ''}",
                              quote=True) + '">Structurizr view</span>')
        parts.append(
            f'<div class="diagram dgm-c4{" dgm-toggles" if toggles else ""}" '
            f'id="c4-{html.escape(re.sub(r"[^A-Za-z0-9_-]+", "-", Path(r["source"]).stem + "-" + r["name"]), quote=True)}">'
            # "Structurizr view" right after the title: the projected card above is called
            # C2-Containers too, and the two may well disagree — they are two claims.
            f'<div class="head"><b>{html.escape(r["name"])}</b>' + kind + check
            + _c4_badge(status)
            + _dsl_link(root, r["source"], r["name"]) + '</div>'
            + body
            + (f'<p class="sub dgm-stale">{html.escape(note)}</p>' if note else "")
            + '</div>')
    return problem + "\n".join(parts), len(rows), changed
