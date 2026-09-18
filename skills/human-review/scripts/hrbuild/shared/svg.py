"""Reading a diagram's SVG in: source links, titles, entity scoping, theming."""
from __future__ import annotations

import re
from pathlib import Path

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
    # puml_diff.RIPPLE — the three washes that say how far a box sits from the change,
    # one hop out to three. Same bookkeeping, sharper stakes: these are *fills*, so a
    # tint left unthemed is not merely off-palette in dark mode, it is a pale slab with
    # a near-white name written on it.
    "#F2CF8E": "--dgm-ripple-1", "#F4DCB4": "--dgm-ripple-2", "#F2EBDB": "--dgm-ripple-3",
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
