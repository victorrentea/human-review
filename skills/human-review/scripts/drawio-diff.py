#!/usr/bin/env python3
"""
drawio-diff — what a branch adds to a hand-drawn draw.io diagram.

Compares two `.drawio.png` files (or the mxGraph XML inside them) by the identity
each element *declares* in the XML — `concept="Owner"` on a box, `assoc="Owner-Pet"`
on a line, the mxCell `id` otherwise — never by rendered pixels. Moving a box does
not make it a different box, and renaming its drawn label does not either.

    ./drawio-diff.py old.drawio.png new.drawio.png --out-dir assets --name conceptual
    ./drawio-diff.py --base origin/main --diagram docs/ConceptualModel.drawio.png \
        --out-dir .human-review/assets --name conceptual

Writes three SVGs — `<name>-original.svg`, `<name>-new.svg`, `<name>-diff.svg` — plus
`<name>-diff.json` with the machine-readable verdict, and prints a one-line summary.

Two colours, two meanings, and they must not be conflated:

  * **red** is the diagram's own. The patch script paints an element red when it draws
    it to keep `ConceptualModelDiagramTest` green, and it stays red until a human
    re-lays it out by hand. It is a to-do, and it is read off the file, not inferred.
  * **orange** is this tool's. It marks what the revision adds against the base.

An element that is both — automation drew it *and* it is new — renders red: the
to-do is the louder fact, and it subsumes "new". Turn it black by hand in draw.io and
this tool paints it orange, because it is still new. That transition is the whole
contract.

Rendering goes through the draw.io desktop app when it is installed, which is the only
way to get a *faithful* picture (and, as a bonus, one that carries `light-dark()` for
both themes). Without it, a built-in renderer walks the mxGeometry — boxes, straight
edges, labels — which is enough for this class of diagram.

Requires: nothing. draw.io.app is used when present.
"""
import argparse
import base64
import binascii
import json
import re
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

# The orange this tool owns: "new on this branch". Two halves, because the report is
# read in both themes and the colour carries meaning in both. draw.io derives its own
# dark variant (a muted #cd6b11) which is legible but dim next to the red's dark
# variant; we pin a brighter amber instead, so orange-vs-red stays a glance apart on a
# near-black ground the same way it is on a near-white one.
ADDED_COLOR = "#E8760D"
ADDED_COLOR_DARK = "#FFA53D"

DRAWIO_APP = Path("/Applications/draw.io.app/Contents/MacOS/draw.io")

# An empty diagram: what a base that never had the file is compared as, so a branch
# introducing a diagram reads as one big "added" instead of a crash.
EMPTY_MODEL = ('<mxfile host="drawio-diff"><diagram id="empty" name="empty">'
               '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               "</root></mxGraphModel></diagram></mxfile>")


# ── getting the XML out of a .drawio.png ──────────────────────────────────────────

def png_text_chunks(data: bytes):
    """Yield (keyword, text) for every tEXt/zTXt/iTXt chunk, in file order.

    Verified against the real files rather than a recipe: draw.io writes one `zTXt`
    with keyword `mxGraphModel`, deflate-compressed, holding percent-encoded XML.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return
    off = 8
    while off + 8 <= len(data):
        (length,) = struct.unpack(">I", data[off:off + 4])
        kind = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + length]
        off += 12 + length
        if kind == b"IEND":
            return
        if kind not in (b"tEXt", b"zTXt", b"iTXt"):
            continue
        keyword, _, rest = body.partition(b"\0")
        try:
            if kind == b"tEXt":
                text = rest.decode("latin-1")
            elif kind == b"zTXt":
                # one byte of compression method, then a zlib stream
                text = zlib.decompress(rest[1:]).decode("utf-8", "replace")
            else:  # iTXt: compression flag, method, language tag, translated keyword
                flag = rest[0]
                payload = rest[3:].split(b"\0", 2)[-1]
                text = (zlib.decompress(payload) if flag else payload).decode("utf-8", "replace")
        except (zlib.error, IndexError, UnicodeDecodeError):
            continue
        yield keyword.decode("latin-1"), text


def _inflate_maybe(blob: str) -> str | None:
    """Undo draw.io's compressed-<diagram> wrapping: base64, then *raw* deflate,
    then percent-encoding. Returns None when `blob` is not that."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return None
    for wbits in (-15, 15, 47):
        try:
            return urllib.parse.unquote(zlib.decompress(raw, wbits).decode("utf-8"))
        except (zlib.error, UnicodeDecodeError):
            continue
    return None


def uncompress_diagrams(xml: str) -> str:
    """Expand every `<diagram>` whose body is base64+deflate back into plain mxGraphModel."""

    def expand(m):
        body = m.group(2).strip()
        if not body or body.startswith("<"):
            return m.group(0)
        plain = _inflate_maybe(body)
        return f"{m.group(1)}{plain}</diagram>" if plain else m.group(0)

    return re.sub(r"(<diagram\b[^>]*>)(.*?)</diagram>", expand, xml, flags=re.S)


def extract_xml(source) -> str:
    """The mxGraph XML behind a path, bytes, or an already-plain XML string."""
    if isinstance(source, (str, Path)) and Path(source).exists():
        data = Path(source).read_bytes()
    elif isinstance(source, bytes):
        data = source
    else:
        data = str(source).encode()

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        for keyword, text in png_text_chunks(data):
            if keyword not in ("mxGraphModel", "mxfile"):
                continue
            text = urllib.parse.unquote(text)
            if "<mxGraphModel" in text or "<mxfile" in text:
                return uncompress_diagrams(text)
        raise ValueError("no mxGraph XML in the PNG — was it saved from draw.io?")

    text = data.decode("utf-8", "replace")
    if "<mxfile" in text or "<mxGraphModel" in text:
        return uncompress_diagrams(text)
    raise ValueError("not a draw.io PNG and not mxGraph XML")


# ── the model ─────────────────────────────────────────────────────────────────────

def style_dict(style: str) -> dict:
    out = {}
    for part in (style or "").split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def is_red(style: str) -> bool:
    """Is this element already red *in the file*?

    Read off the drawn colour, never off `addedBy=…`: the marker outlives the fix,
    the colour does not. A human who re-routes the line and turns it black must stop
    getting the red to-do, and start getting the orange "still new".
    """
    styles = style_dict(style)
    for key in ("strokeColor", "fontColor"):
        value = (styles.get(key) or "").strip().lower()
        if value in ("red", "#f00", "#ff0000"):
            return True
        m = re.fullmatch(r"#([0-9a-f]{6})", value)
        if m:
            r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
            if r >= 150 and r >= 2 * g and r >= 2 * b:
                return True
    return False


class Cell:
    """One drawn thing, flattened out of `<object>`/`<UserObject>` wrappers."""

    __slots__ = ("id", "kind", "label", "style", "source", "target", "parent",
                 "attrs", "geometry")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    def __repr__(self):
        return f"<Cell {self.kind} {self.id!r}>"


def parse_model(xml: str) -> dict:
    """Cell id → Cell, for every vertex, edge and edge label in the first diagram."""
    root = ET.fromstring(xml)
    cells = {}
    for node in root.iter():
        if node.tag not in ("mxCell", "object", "UserObject"):
            continue
        if node.tag == "mxCell":
            # a plain cell, unless it is the inner half of an <object> we already took
            if node.get("id") is None:
                continue
            inner, attrs, cid = node, dict(node.attrib), node.get("id")
            label = node.get("value") or ""
        else:
            inner = node.find("mxCell")
            if inner is None:
                continue
            attrs, cid = dict(node.attrib), node.get("id")
            label = node.get("label") or ""
        if cid in ("0", "1") or cid is None:
            continue
        geometry = inner.find("mxGeometry")
        style = inner.get("style") or ""
        styles = style_dict(style)
        if inner.get("edge") == "1":
            kind = "edge"
        elif "edgeLabel" in style:
            kind = "label"
        elif "text" in styles or styles.get("shape") == "note":
            # A caption, a title, a sticky note. It is drawn as a vertex, but it is not a
            # concept — counting one as a box is how a review page ends up announcing a
            # new domain class that nobody added.
            kind = "annotation"
        else:
            kind = "node"
        cells[cid] = Cell(
            id=cid, kind=kind, label=label, style=style,
            source=inner.get("source"), target=inner.get("target"),
            parent=inner.get("parent"), attrs=attrs,
            geometry=dict(geometry.attrib) if geometry is not None else {},
        )
    # an edge's label cell is parented to the edge; nothing else re-parents
    for cell in cells.values():
        if cell.kind == "label" and cell.parent in cells:
            cells[cell.parent].kind = "edge" if cells[cell.parent].kind == "edge" else cells[cell.parent].kind
    return cells


def identity(cell: Cell, cells: dict) -> str:
    """What this element declares itself to be, most-specific claim first.

    `concept=` / `assoc=` are the diagram's own contract with the guardrail test, so
    they win: a box keeps its identity through a rename, a resize and a drag. The
    mxCell `id` is the next-best declaration. Only past both do we fall back to
    something structural, and never to the drawn text of a box.
    """
    if cell.kind == "annotation":
        return f"note#{cell.id}"
    if cell.kind == "node":
        if cell.attrs.get("concept"):
            return f"node:{cell.attrs['concept']}"
        return f"node#{cell.id}"
    if cell.kind == "edge":
        if cell.attrs.get("assoc"):
            return f"edge:{cell.attrs['assoc']}"
        ends = tuple(sorted(
            identity(cells[e], cells) if e in cells else f"?{e}"
            for e in (cell.source, cell.target) if e))
        if ends:
            return "edge:" + "--".join(ends)
        return f"edge#{cell.id}"
    parent = cells.get(cell.parent)
    where = "src" if str(cell.geometry.get("x", "0")).startswith("-") else "tgt"
    if parent is not None:
        return f"label:{identity(parent, cells)}@{where}"
    return f"label#{cell.id}"


def index(cells: dict) -> dict:
    """identity → Cell. A duplicate identity keeps the first; the guardrail test is
    the place that shouts about duplicates, not this one."""
    out = {}
    for cell in cells.values():
        out.setdefault(identity(cell, cells), cell)
    return out


def describe(cell: Cell, cells: dict) -> str:
    if cell.kind == "annotation":
        return cell.label or cell.id
    if cell.kind == "node":
        return cell.attrs.get("concept") or cell.label or cell.id
    if cell.kind == "edge":
        if cell.attrs.get("assoc"):
            return cell.attrs["assoc"]
        ends = [describe(cells[e], cells) if e in cells else e
                for e in (cell.source, cell.target) if e]
        return "–".join(ends) or cell.id
    return f"label on {describe(cells[cell.parent], cells)}" if cell.parent in cells else cell.id


def diff_models(old_xml: str, new_xml: str) -> dict:
    """What `new` adds, drops and reworks relative to `old`."""
    old_cells, new_cells = parse_model(old_xml), parse_model(new_xml)
    old_idx, new_idx = index(old_cells), index(new_cells)

    added, removed, changed, moved = [], [], [], []

    for key, cell in new_idx.items():
        if key in old_idx:
            continue
        added.append({"key": key, "id": cell.id, "kind": cell.kind,
                      "what": describe(cell, new_cells),
                      "already_red": is_red(cell.style)})
    for key, cell in old_idx.items():
        if key not in new_idx:
            removed.append({"key": key, "id": cell.id, "kind": cell.kind,
                            "what": describe(cell, old_cells)})
    for key, cell in new_idx.items():
        was = old_idx.get(key)
        if was is None:
            continue
        deltas = []
        if (was.label or "") != (cell.label or ""):
            deltas.append(f'label "{was.label}" → "{cell.label}"')
        for end in ("source", "target"):
            a, b = getattr(was, end), getattr(cell, end)
            aa = identity(old_cells[a], old_cells) if a in old_cells else a
            bb = identity(new_cells[b], new_cells) if b in new_cells else b
            if aa != bb:
                deltas.append(f"{end} {aa} → {bb}")
        if style_dict(was.style) != style_dict(cell.style):
            deltas.append("style")
        if deltas:
            changed.append({"key": key, "id": cell.id, "kind": cell.kind,
                            "what": describe(cell, new_cells), "changes": deltas})
        elif {k: v for k, v in was.geometry.items() if k in ("x", "y", "width", "height")} \
                != {k: v for k, v in cell.geometry.items() if k in ("x", "y", "width", "height")}:
            # position is the human's — a drag is news, but it is not a change of content
            moved.append({"key": key, "id": cell.id, "kind": cell.kind,
                          "what": describe(cell, new_cells)})

    def order(items):
        rank = {"node": 0, "edge": 1, "annotation": 2, "label": 3}
        return sorted(items, key=lambda i: (rank[i["kind"]], i["key"]))

    return {"added": order(added), "removed": order(removed),
            "changed": order(changed), "moved": order(moved)}


def counted(verdict: dict) -> str:
    def by_kind(items):
        n = sum(1 for i in items if i["kind"] == "node")
        e = sum(1 for i in items if i["kind"] == "edge")
        a = sum(1 for i in items if i["kind"] == "annotation")
        out = f"{n} box{'es' if n != 1 else ''}, {e} line{'s' if e != 1 else ''}"
        return out + (f", {a} note{'s' if a != 1 else ''}" if a else "")

    return ("added " + by_kind(verdict["added"])
            + " · removed " + by_kind(verdict["removed"])
            + f" · {len(verdict['changed'])} changed · {len(verdict['moved'])} moved")


# ── painting the diff ─────────────────────────────────────────────────────────────

def _paint(style: str, color: str) -> str:
    """Recolour one element's mxGraph style.

    A text shape and an edge label are painted by their font alone: draw.io reads a
    `strokeColor` on them as "draw a border", so colouring the stroke would frame the
    note in a box that the diagram never had.
    """
    styles = style_dict(style)
    updates = {"fontColor": color}
    if "text" not in styles and "edgeLabel" not in styles:
        updates["strokeColor"] = color
        updates["strokeWidth"] = str(max(2, int(float(styles.get("strokeWidth") or 1))))
    # rebuilt in place, so draw.io's own bare keys (`text`, `rounded=0`, `edgeLabel`)
    # and their order survive: a style is not a dict to draw.io, it is a recipe
    out = []
    for part in (style or "").split(";"):
        if not part:
            continue
        key, sep, _ = part.partition("=")
        out.append(f"{key}={updates.pop(key)}" if sep and key in updates else part)
    out += [f"{k}={v}" for k, v in updates.items()]
    return ";".join(out) + ";"


def paint_added(xml: str, verdict: dict) -> str:
    """Recolour, in the XML, every element the revision adds — unless the file already
    draws it red, in which case the red to-do stays and wins."""
    targets = {a["id"] for a in verdict["added"] if not a["already_red"]}
    if not targets:
        return xml
    root = ET.fromstring(xml)
    cells = parse_model(xml)
    # a fresh edge's cardinality label is part of the edge, so it follows the same paint
    for cell in cells.values():
        if cell.kind == "label" and cell.parent in targets:
            targets.add(cell.id)
    for node in root.iter():
        if node.tag in ("object", "UserObject"):
            if node.get("id") in targets:
                inner = node.find("mxCell")
                if inner is not None:
                    inner.set("style", _paint(inner.get("style") or "", ADDED_COLOR))
        elif node.tag == "mxCell" and node.get("id") in targets:
            node.set("style", _paint(node.get("style") or "", ADDED_COLOR))
    return ET.tostring(root, encoding="unicode")


# ── linking a box to the class it names ───────────────────────────────────────────

# `class Owner [[src://petclinic-backend/.../Owner.java:32{Click to open in editor}]] {`
# — one line of the PlantUML that `DomainModelExtractorTest` regenerates from the code.
CLASS_LINK = re.compile(
    r"^\s*(?:abstract\s+|final\s+)?(?:class|entity|enum|interface)\s+(?P<name>\w+)\s*"
    r"\[\[src://(?P<path>[^\s:\]]+)(?::(?P<line>\d+))?(?:\{[^}]*\})?[^\]]*\]\]",
    re.M)


def concept_sources(puml: Path) -> dict:
    """concept name → (repo-relative path, line), read off the generated domain model.

    Deliberately not a second name-matching scheme. `DomainModelExtractor` is the one
    thing that decides what a domain class is, and its javadoc says so out loud: two
    guardrails compare a drawing against it — `DomainModelExtractorTest`, which
    regenerates this PlantUML, and `ConceptualModelDiagramTest`, which checks the
    hand-laid-out draw.io map against the same extractor. So the PlantUML *is* that
    resolution, already run, already carrying the line each class is declared on.

    A concept the map declares and this file does not name therefore cannot happen while
    the guardrail is green. If it ever does, the diagram and the test disagree and the
    test is the one that is right — so the box loses its link and the caller says so,
    rather than the page shipping an anchor pointing at a class that is not there.
    """
    if not puml or not Path(puml).is_file():
        return {}
    out = {}
    for m in CLASS_LINK.finditer(Path(puml).read_text(encoding="utf-8")):
        out[m["name"]] = (m["path"], int(m["line"] or 1))
    return out


def link_concepts(xml: str, sources: dict, root: Path):
    """Point every concept box at its class. Returns the XML and the concepts it could
    not resolve.

    The links go in HERE, never into the `.drawio` file: that file is hand-edited, and
    asking a human to keep a path and a line number correct by hand is asking for a link
    that rots silently. The SVGs are generated, so they can carry what the source of
    truth says today, every time they are rendered.

    Only the concept boxes. An edge, an edge label and the "Please manually fix the
    layout." annotation are not concepts — `parse_model` already separates the last one
    out as `annotation`, so the distinction is data, not a naming guess here.
    """
    cells = parse_model(xml)
    linked = {c.id: c.attrs["concept"] for c in cells.values()
              if c.kind == "node" and c.attrs.get("concept")}
    missing = sorted({name for name in linked.values() if name not in sources})
    if not sources:
        return xml, missing
    root_at = Path(root).resolve()
    tree = ET.fromstring(xml)
    for node in tree.iter():
        if node.tag not in ("object", "UserObject"):
            continue
        name = linked.get(node.get("id"))
        where = sources.get(name) if name else None
        if not where:
            continue          # unresolved: no anchor at all, never a broken one
        rel, line = where
        node.set("link", f"vscode://file/{root_at / rel}:{line}:1")
    return ET.tostring(tree, encoding="unicode"), missing


# ── rendering ─────────────────────────────────────────────────────────────────────

_SWITCH = re.compile(r"<switch>\s*(<foreignObject\b.*?</foreignObject>)\s*"
                     r"(?:<image\b[^>]*/>)?\s*</switch>", re.S)


# The last thing draw.io writes: a "Text is not SVG - cannot display" line, shown only
# where `foreignObject` is missing, linking to drawio.com. Invisible in a browser, but it
# is still an off-site link inside a review page, and nothing in the page explains it.
_NOT_SVG = re.compile(r"<switch>\s*<g requiredFeatures=[^>]*/>\s*<a\b.*?</a>\s*</switch>", re.S)


def slim(svg: str) -> str:
    """Drop draw.io's `<switch>` fallbacks — a base64 PNG of every label, for renderers
    with no `foreignObject`. Every browser has one, and they are 80% of the bytes."""
    return _NOT_SVG.sub("", _SWITCH.sub(r"\1", svg))


def _rgb(hexcolor: str) -> str:
    h = hexcolor.lstrip("#")
    return "rgb({}, {}, {})".format(*(int(h[i:i + 2], 16) for i in (0, 2, 4)))


def pin_added_dark(svg: str) -> str:
    """Keep the dark half of our orange ours.

    draw.io writes every colour as `light-dark(light, dark)` and picks the dark half
    itself. That is right for the diagram's own palette and wrong for the one colour
    this tool means something by, so we overwrite just that pair — in both the hex and
    the `rgb()` spelling draw.io uses.
    """
    for light, dark in ((ADDED_COLOR, ADDED_COLOR_DARK),
                        (_rgb(ADDED_COLOR), _rgb(ADDED_COLOR_DARK))):
        pattern = (r"light-dark\(\s*" + re.escape(light)
                   + r"\s*,\s*(?:#[0-9a-fA-F]{3,8}|rgba?\([^()]*\))\s*\)")
        svg = re.sub(pattern, f"light-dark({light}, {dark})", svg, flags=re.I)
    return svg


# `(?:[^>]*\s)?href=` and not `[^>]*\shref=`: the optional branch is what catches an
# anchor whose href is the FIRST attribute, where there is no preceding whitespace to
# match — without it, every such anchor was given a second, duplicate href.
_XLINK_ONLY = re.compile(r'<a (?!(?:[^>]*\s)?href=)([^>]*?)xlink:href="([^"]*)"')


def dual_href(svg: str) -> str:
    """Give every anchor a plain `href` beside draw.io's `xlink:href`.

    draw.io exports SVG 1.1 anchors, which carry `xlink:href` alone. The report routes
    editor links through one delegated listener selecting `a[href^="vscode:"]`, and an
    attribute selector matches the attribute that is written, not the one the browser
    resolves — so an xlink-only anchor is inert on this page. PlantUML's diagrams needed
    both spellings for the same reason.
    """
    return _XLINK_ONLY.sub(lambda m: f'<a {m.group(1)}xlink:href="{m.group(2)}" '
                                     f'href="{m.group(2)}"', svg)


def render_with_drawio(xml: str, out: Path) -> bool:
    if not DRAWIO_APP.exists():
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.drawio"
        src.write_text(xml)
        proc = subprocess.run(
            [str(DRAWIO_APP), "-x", "-f", "svg", "--theme", "auto", "-b", "8",
             "-o", str(out), str(src), "--no-sandbox"],
            capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        print(f"draw.io export failed, falling back to the built-in renderer:\n"
              f"{proc.stderr.strip()}", file=sys.stderr)
        return False
    out.write_text(dual_href(pin_added_dark(slim(out.read_text()))))
    return True


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_builtin(xml: str, out: Path) -> None:
    """A faithful-enough SVG from the mxGeometry alone: boxes, straight edges clipped
    to the boxes they join, and labels. Used when the draw.io app is not installed."""
    cells = parse_model(xml)
    boxes = {}
    for cell in cells.values():
        g = cell.geometry
        if cell.kind in ("node", "annotation") and g.get("width"):
            boxes[cell.id] = tuple(float(g.get(k, 0)) for k in ("x", "y", "width", "height"))

    xs = [b[0] for b in boxes.values()] + [b[0] + b[2] for b in boxes.values()]
    ys = [b[1] for b in boxes.values()] + [b[1] + b[3] for b in boxes.values()]
    pad = 20
    minx, miny = (min(xs) - pad if xs else 0), (min(ys) - pad if ys else 0)
    width = (max(xs) - min(xs) + 2 * pad) if xs else 100
    height = (max(ys) - min(ys) + 2 * pad) if ys else 100

    # the two colours that carry meaning get a dark half of their own, like draw.io's
    themed = {ADDED_COLOR.lower(): f"light-dark({ADDED_COLOR}, {ADDED_COLOR_DARK})",
              "#ff0000": "light-dark(#FF0000, #ff9090)"}

    def stroke_of(cell, default="light-dark(#333333, #c8c8d2)"):
        c = style_dict(cell.style).get("strokeColor")
        return themed.get((c or "").lower(), c) or default

    def font_of(cell, default="light-dark(#111111, #e8e8ef)"):
        c = style_dict(cell.style).get("fontColor")
        return themed.get((c or "").lower(), c) or default

    body = []
    for cell in cells.values():  # edges first, so boxes sit on top
        if cell.kind != "edge":
            continue
        a, b = boxes.get(cell.source), boxes.get(cell.target)
        if not a or not b:
            continue
        ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
        bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
        w = float(style_dict(cell.style).get("strokeWidth", 1) or 1)
        body.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                    f'stroke="{stroke_of(cell)}" stroke-width="{w}"/>')
    for cell in cells.values():
        if cell.kind not in ("node", "annotation") or cell.id not in boxes:
            continue
        x, y, w, h = boxes[cell.id]
        styles = style_dict(cell.style)
        fill = styles.get("fillColor")
        size = styles.get("fontSize", "14")
        # both spellings, for the same reason `dual_href` exists on the draw.io path
        href = cell.attrs.get("link")
        if href:
            body.append(f'<a xlink:href="{_esc(href)}" href="{_esc(href)}" '
                        f'style="cursor:pointer">')
        if fill and not styles.get("text"):
            body.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" '
                        f'stroke="{stroke_of(cell)}" stroke-width="'
                        f'{styles.get("strokeWidth", 1)}"/>')
        if cell.label:
            body.append(f'<text x="{x + w / 2}" y="{y + h / 2}" text-anchor="middle" '
                        f'dominant-baseline="central" font-family="Helvetica,sans-serif" '
                        f'font-size="{size}" fill="{font_of(cell)}">{_esc(cell.label)}</text>')
        if href:
            body.append("</a>")
    for cell in cells.values():
        if cell.kind != "label" or not cell.label:
            continue
        parent = cells.get(cell.parent)
        if parent is None:
            continue
        a, b = boxes.get(parent.source), boxes.get(parent.target)
        if not a or not b:
            continue
        t = 0.5 + float(cell.geometry.get("x", 0) or 0) / 2
        ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
        bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
        size = style_dict(cell.style).get("fontSize", "14")
        body.append(f'<text x="{ax + (bx - ax) * t}" y="{ay + (by - ay) * t}" '
                    f'text-anchor="middle" dominant-baseline="central" '
                    f'font-family="Helvetica,sans-serif" font-size="{size}" '
                    f'font-weight="bold" fill="{font_of(cell)}">{_esc(cell.label)}</text>')

    out.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" '
        f'viewBox="{minx} {miny} {width} {height}" '
        f'style="color-scheme: light dark; background: transparent;">'
        + "".join(body) + "</svg>")


def render(xml: str, out: Path, renderer: str = "auto") -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    if renderer in ("auto", "drawio") and render_with_drawio(xml, out):
        return "drawio"
    if renderer == "drawio":
        sys.exit("draw.io.app is not installed; use --renderer builtin")
    render_builtin(xml, out)
    return "builtin"


# ── the CLI ───────────────────────────────────────────────────────────────────────

def read_at(ref: str, path: str) -> str:
    """The diagram as of a git ref. Absent there means an empty diagram, not a crash:
    a branch that introduces the map should read as one big "added"."""
    blob = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    if blob.returncode != 0:
        return EMPTY_MODEL
    return extract_xml(blob.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", nargs="?", help="base diagram (omit when --base is given)")
    ap.add_argument("new", nargs="?", help="revision (omit when --base is given)")
    ap.add_argument("--base", metavar="REF",
                    help="git revision to diff the working tree against, e.g. a "
                         "merge-base — the pipeline entry point")
    ap.add_argument("--diagram", help="diagram path inside the repo, used with --base")
    ap.add_argument("--out-dir", default=".", help="where the three SVGs are written")
    ap.add_argument("--name", help="stem for the written files (default: the diagram's)")
    ap.add_argument("--renderer", choices=("auto", "drawio", "builtin"), default="auto")
    ap.add_argument("--concepts", metavar="PUML", required=True,
                    help="the generated domain-model PlantUML, whose class links say "
                         "where each concept is declared; every concept box in every "
                         "pane becomes a link into that class. REQUIRED: without it the "
                         "command still succeeds and quietly produces boxes that are not "
                         "links, which is the failure nobody notices until they click one")
    ap.add_argument("--repo-root", default=".",
                    help="what the paths inside --concepts are relative to")
    ap.add_argument("--json", action="store_true",
                    help="print the verdict as JSON instead of a summary line")
    args = ap.parse_args()

    if args.base:
        if not args.diagram:
            ap.error("--base needs --diagram")
        source = Path(args.diagram)
        if not source.is_file():
            sys.exit(f"no diagram at {source}")
        old_xml, new_xml = read_at(args.base, args.diagram), extract_xml(source)
        stem = args.name or source.name.split(".")[0]
    elif args.old and args.new:
        old_xml, new_xml = extract_xml(args.old), extract_xml(args.new)
        stem = args.name or Path(args.new).name.split(".")[0]
    else:
        ap.error("give two diagrams, or --base REF --diagram PATH")

    verdict = diff_models(old_xml, new_xml)
    out_dir = Path(args.out_dir)

    # One map, from the WORKING TREE, applied to all three panes. That is what makes the
    # "old" pane behave: a concept this branch deleted is simply not in it, so its box on
    # the base diagram quietly loses its link instead of pointing at a file that is gone.
    sources = concept_sources(Path(args.concepts))
    unresolved = set()

    def linked(xml):
        out, missing = link_concepts(xml, sources, args.repo_root)
        unresolved.update(missing)
        return out

    written = {
        "original": render(linked(old_xml), out_dir / f"{stem}-original.svg", args.renderer),
        "new": render(linked(new_xml), out_dir / f"{stem}-new.svg", args.renderer),
        "diff": render(linked(paint_added(new_xml, verdict)),
                       out_dir / f"{stem}-diff.svg", args.renderer),
    }
    verdict["linked_concepts"] = sorted(sources)
    verdict["unlinked_concepts"] = sorted(unresolved)
    verdict["renderer"] = written["diff"]
    verdict["added_color"] = ADDED_COLOR
    (out_dir / f"{stem}-diff.json").write_text(json.dumps(verdict, indent=2))

    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        print(f"{out_dir}/{stem}-{{original,new,diff}}.svg  ({counted(verdict)}"
              f", rendered by {written['diff']})")
        for item in verdict["added"]:
            if item["kind"] == "label":
                continue
            colour = "red (automation's to-do)" if item["already_red"] else "orange (new)"
            print(f"  + {item['kind']} {item['what']} — {colour}")
        for item in verdict["removed"]:
            if item["kind"] != "label":
                print(f"  - {item['kind']} {item['what']}")
        for item in verdict["changed"]:
            print(f"  ~ {item['kind']} {item['what']}: {'; '.join(item['changes'])}")
        if args.concepts:
            print(f"  {len(sources)} concept(s) linked to their class")
        # Impossible while ConceptualModelDiagramTest passes — it refuses a box whose
        # concept no longer exists. Loud, because it means the map and the guardrail
        # disagree, and that is a finding about the branch rather than about this script.
        for name in sorted(unresolved):
            print(f"  ! concept {name} resolves to no class — left unlinked; the "
                  f"conceptual-model guardrail and the diagram disagree", file=sys.stderr)


if __name__ == "__main__":
    main()
