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
  * **green** is this tool's. It marks what the revision adds against the base — the
    same green the rest of the report spends on "added", never a second one.

An element that is both — automation drew it *and* it is new — renders red: the
to-do is the louder fact, and it subsumes "new". Turn it black by hand in draw.io and
this tool paints it green, because it is still new. That transition is the whole
contract.

The to-do is meant to be acted on, so the verdict carries `drawio_url` — a
`drawio://<absolute path>` the report renders as a link *under* the drawing, in HTML,
where a link can look like one. Nothing is painted onto the picture to say so. macOS
ships no such scheme — `install-drawio-url-handler.sh` next to this file registers one.

Rendering goes through the draw.io desktop app when it is installed, which is the only
way to get a *faithful* picture (and, as a bonus, one that carries `light-dark()` for
both themes). Without it, a built-in renderer walks the mxGeometry — boxes, straight
edges, labels — which is enough for this class of diagram.

Requires: nothing. draw.io.app is used when present.
"""
import argparse
import base64
import binascii
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

# The green this tool owns: "new on this branch". It is deliberately the same pair the
# rest of the report spends on *added* — `puml_diff.ADDED` and the `--dgm-diff-add` it
# maps to in dark mode — because a reader who has learnt green-is-added on the PlantUML
# deltas two tabs over must not have to re-learn it here. It used to be an orange of its
# own, which made this the only surface in the page where "added" was not green.
#
# Two halves, because the report is read in both themes and the colour carries meaning in
# both: draw.io derives its own dark variant, which is right for the diagram's palette and
# wrong for the one colour this tool means something by.
ADDED_COLOR = "#2E7D32"
ADDED_COLOR_DARK = "#8FD39C"

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
    getting the red to-do, and start getting the green "still new".
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

    # Everything the file itself draws red, new or not. `already_red` on an *added*
    # element only answers "is this addition automation's", and a red element inherited
    # from the base is just as much a layout still owed — the reader looking at the
    # picture cannot tell the two apart, so the page must not either.
    red = [{"key": key, "id": cell.id, "kind": cell.kind,
            "what": describe(cell, new_cells)}
           for key, cell in new_idx.items() if is_red(cell.style)]

    def order(items):
        rank = {"node": 0, "edge": 1, "annotation": 2, "label": 3}
        return sorted(items, key=lambda i: (rank[i["kind"]], i["key"]))

    return {"added": order(added), "removed": order(removed),
            "changed": order(changed), "moved": order(moved), "red": order(red)}


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

    Only the strokes carry the mark — the border of a box, the line of an edge. Words
    stay the colour the diagram writes them in: a new box already says it is new with
    its border, and colouring its label too says the same thing twice, in ink the
    reader then has to stop and read as meaning. Text is text.

    So a text shape and an edge label change in exactly one way here, and it is not a
    colour: whatever label background they carried is dropped. draw.io's default is
    white, and a white chip behind a label inverts to a black slab in the dark theme —
    the one thing on the picture that nobody drew.

    And the colour is the *only* thing that changes on a stroke. This used to widen the
    line to 2px as well, which reads on the drawing as a heavier line rather than as a
    new one: weight is the diagram's own language for emphasis, the map's other lines
    are the weight the human drew them at, and a mark that says "new" twice leaves the
    reader deciding whether the second one meant something else.
    """
    styles = style_dict(style)
    if "text" in styles or "edgeLabel" in styles:
        updates = {"labelBackgroundColor": "none"}
    else:
        updates = {"strokeColor": color}
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


# ── the traces, laid over the drawing ─────────────────────────────────────────────

# A deployment picture is a claim about what calls what, and the sequence diagrams the
# tests recorded are the evidence. `--traces` reads the container graph
# `c2-from-sequence.py` projected from them and says, on the drawing itself, which of the
# arrows a test actually walked:
#
#   * walked  — an arrow between two boxes that declare the lifeline they answer to
#               (`traceParticipant="Backend"`), in the direction a trace called. Drawn at
#               double weight: weight is the drawing's own language for emphasis, and
#               this is the emphasis a reviewer wants — where the real traffic is.
#   * not walked — every other arrow: a human using the frontend, a hop with no agent on
#               it, or a call no test exercises any more. Thin, never removed: the
#               drawing may well be right, and the tests silent.
#   * not drawn — a call the traces recorded that no arrow carries. Added, dashed and red,
#               between the two boxes (a box no lifeline maps onto is added too, in a lane
#               right of the drawing): it is a to-do for whoever owns the picture, and red
#               is this page's colour for a hand-drawn diagram's to-do.
#
# So weight is the only thing an arrow says here, and the drawing's own line colours and
# dashes go: every arrow is the theme's default line, black on white and white on black. A
# reader took a green, an orange, a grey and a dotted arrow for four meanings, and none of
# them meant anything to do with the tests (Victor, 6 Nov 2026).
#
# Green stays "added by this branch" and is never spent here: an arrow can be both new and
# walked, and it keeps its green at double weight.
TRACE_ATTR = "traceParticipant"
#: draw.io's own name for the theme's line colour, which it exports as a `light-dark()` pair.
LINE_COLOR = "default"
UNDRAWN_COLOR = "#FF0000"
#: The weight of an arrow no test walked: 30% thinner than the 2 it was drawn at, so walked
#: and unwalked differ at a glance (Victor, 6 Nov 2026). Not 1 x 0.7. A style of 1 never
#: reached the reader as 1: draw.io writes no width for it, and `diagrams.css` draws every
#: default-width stroke at 2px. Any other number passes through that rule as written, so
#: this one is the width on the page, in the drawing's own units.
UNWALKED_WIDTH = 1.4


def _restyle(style: str, updates: dict) -> str:
    """Set style keys in place, keeping draw.io's bare keys and their order."""
    updates = dict(updates)
    out = []
    for part in (style or "").split(";"):
        if not part:
            continue
        key, sep, _ = part.partition("=")
        out.append(f"{key}={updates.pop(key)}" if sep and key in updates else part)
    out += [f"{k}={v}" for k, v in updates.items()]
    return ";".join(out) + ";"


def trace_edges(c2_json: Path, side: str = "new") -> set:
    """(from, to) of every call between two containers, as `c2-from-sequence.py` wrote it."""
    graph = json.loads(c2_json.read_text(encoding="utf-8")).get(side) or {}
    return {(e["from"], e["to"]) for e in graph.get("edges") or []
            if e.get("from") and e.get("to") and e.get("status") != "removed"}


def overlay_traces(xml: str, observed: set, attr: str = TRACE_ATTR) -> tuple[str, dict]:
    """The drawing with the traces laid over it, and what the overlay found.

    Matching is by the participant name a box *declares*, never by its drawn label — the
    label is the human's ("Frontend"), the lifeline is the trace's ("Browser"), and the
    attribute is the one place the two are tied together on purpose."""
    root = ET.fromstring(xml)
    cells = parse_model(xml)
    participant = {c.id: c.attrs.get(attr) for c in cells.values()
                   if c.kind == "node" and c.attrs.get(attr)}
    box_of = {name: cid for cid, name in participant.items()}

    def name(cid: str) -> str:
        """What the reader sees on the box — the lifeline name only for a box with no text."""
        c = cells.get(cid)
        return (_plain(c.label).strip() if c else "") or participant.get(cid) or cid or "?"

    walked, unwalked, drawn = [], [], set()
    styles = {}
    for c in cells.values():
        if c.kind != "edge":
            continue
        ends = (participant.get(c.source), participant.get(c.target))
        what = f"{name(c.source)} → {name(c.target)}"
        st = style_dict(c.style)
        upd = {"dashed": "0"}
        if (st.get("strokeColor") or "").lower() != ADDED_COLOR.lower():
            upd["strokeColor"] = LINE_COLOR
        if all(ends) and ends in observed:
            drawn.add(ends)
            walked.append(what)
            width = float(st.get("strokeWidth", 1) or 1)
            upd["strokeWidth"] = f"{max(width * 2, 3):g}"
        else:
            unwalked.append(what)
            upd["strokeWidth"] = f"{UNWALKED_WIDTH:g}"
        styles[c.id] = upd

    for node in root.iter():
        if node.tag in ("object", "UserObject") and node.get("id") in styles:
            inner = node.find("mxCell")
            if inner is not None:
                inner.set("style", _restyle(inner.get("style") or "", styles[node.get("id")]))
        elif node.tag == "mxCell" and node.get("id") in styles:
            node.set("style", _restyle(node.get("style") or "", styles[node.get("id")]))

    undrawn = sorted(observed - drawn)
    layer = next((n for n in root.iter("mxCell") if n.get("id") == "1"), None)
    holder = next((p for p in root.iter() if layer is not None and layer in list(p)), None)
    if undrawn and holder is not None:
        geo = {c.id: tuple(float(c.geometry.get(k, 0) or 0) for k in ("x", "y", "width", "height"))
               for c in cells.values() if c.kind in ("node", "annotation") and c.geometry.get("width")}
        W, H, GAP = 180.0, 70.0, 60.0

        def free(x, y):
            return all(x + W + 20 <= bx or bx + bw + 20 <= x or y + H + 20 <= by or by + bh + 20 <= y
                       for bx, by, bw, bh in geo.values())

        def place(beside: str | None):
            """Next to the box it is called from or calls, where nothing is drawn yet:
            right of it, then below, then left — the line it hangs on stays short."""
            if beside in geo:
                x, y, w, h = geo[beside]
                for cx, cy in ((x + w + GAP, y), (x + (w - W) / 2, y + h + GAP),
                               (x - W - GAP, y), (x + (w - W) / 2, y - H - GAP)):
                    if free(cx, cy):
                        return cx, cy
            right = max((x + w for x, _, w, _ in geo.values()), default=0) + GAP
            y = min((y for _, y, _, _ in geo.values()), default=0)
            while not free(right, y):
                y += H + 20
            return right, y

        ghost = 0
        for a, b in undrawn:
            for end, other in ((a, b), (b, a)):
                if end in box_of:
                    continue
                cid = f"hr-undrawn-box-{ghost}"
                box_of[end] = cid
                gx, gy = place(box_of.get(other))
                geo[cid] = (gx, gy, W, H)
                cell = ET.SubElement(holder, "mxCell", {
                    "id": cid, "parent": "1", "vertex": "1",
                    # HTML, as a label is: a lifeline name like "«module»\nCommons" carries a
                    # literal backslash-n (or a real newline), which HTML would show as text
                    "value": "<b>" + _esc(end).replace("\\n", "<br>").replace("\n", "<br>")
                             + "</b>",
                    "style": ("rounded=1;whiteSpace=wrap;html=1;dashed=1;fillColor=none;"
                              f"strokeColor={UNDRAWN_COLOR};fontColor={UNDRAWN_COLOR};"
                              "strokeWidth=2;fontSize=13;")})
                ET.SubElement(cell, "mxGeometry", {
                    "x": f"{gx:g}", "y": f"{gy:g}",
                    "width": f"{W:g}", "height": f"{H:g}", "as": "geometry"})
                ghost += 1
        for i, (a, b) in enumerate(undrawn):
            detour = _detour(geo, box_of[a], box_of[b])
            cell = ET.SubElement(holder, "mxCell", {
                "id": f"hr-undrawn-{i}", "parent": "1", "edge": "1",
                "source": box_of[a], "target": box_of[b], "value": "",
                "style": ("html=1;endArrow=block;dashed=1;rounded=0;"
                          + ("edgeStyle=orthogonalEdgeStyle;" if detour else "")
                          + f"strokeColor={UNDRAWN_COLOR};strokeWidth=3;")})
            g = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
            if detour:
                pts = ET.SubElement(g, "Array", {"as": "points"})
                for x, y in detour:
                    ET.SubElement(pts, "mxPoint", {"x": f"{x:g}", "y": f"{y:g}"})

    report = {"attr": attr,
              "walked": sorted(walked),
              "unwalked": sorted(unwalked),
              "undrawn": [f"{a} → {b}" for a, b in undrawn],
              "unmapped": sorted({e for pair in observed for e in pair} - set(participant.values()))}
    return ET.tostring(root, encoding="unicode"), report


def _crosses(box, p, q, pad: float = 4.0) -> bool:
    """Does the segment p→q pass through `box` (x, y, w, h), grown by `pad`?"""
    x, y, w, h = box
    x0, y0, x1, y1 = x - pad, y - pad, x + w + pad, y + h + pad
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for d, lo, hi, o in ((dx, x0, x1, p[0]), (dy, y0, y1, p[1])):
        if d == 0:
            if not lo <= o <= hi:
                return False
            continue
        a, b = (lo - o) / d, (hi - o) / d
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
        if t0 > t1:
            return False
    return True


def _detour(geo: dict, src: str, tgt: str, gap: float = 30.0):
    """Waypoints that take a red to-do arrow round the boxes between its two ends, or None.

    The arrow is drawn centre to centre, so a call from Notification Service to a Commons
    box placed left of Backend went straight through Backend — its dashes struck through
    the label "Backend" (UX review, 7 Oct 2026). When the straight line crosses a box that
    is neither end, the arrow leaves its source from the quarter nearest the target (the
    centre is where the source's own arrows already leave), runs along a lane just under
    every box it would have crossed — or over them, when the lane under is not clear — and
    comes up into the target's centre."""
    if src not in geo or tgt not in geo:
        return None
    def centre(b):
        x, y, w, h = geo[b]
        return x + w / 2, y + h / 2
    p, q = centre(src), centre(tgt)
    hit = [k for k, box in geo.items() if k not in (src, tgt) and _crosses(box, p, q)]
    if not hit:
        return None
    sx, sy, sw, sh = geo[src]
    ex = sx + sw * (0.25 if q[0] < p[0] else 0.75)
    involved = [geo[k] for k in hit + [src, tgt]]
    for lane in (max(y + h for _, y, _, h in involved) + gap,
                 min(y for _, y, _, _ in involved) - gap):
        legs = [((ex, p[1]), (ex, lane)), ((ex, lane), (q[0], lane)), ((q[0], lane), q)]
        if not any(_crosses(box, a, b, pad=2) for k, box in geo.items() if k not in (src, tgt)
                   for a, b in legs):
            return [(ex, lane), (q[0], lane)]
    return None


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


def drawio_url(diagram: Path) -> str:
    """`drawio://<absolute path>` — the click that opens the file in the desktop app.

    macOS ships no handler for this: draw.io's bundle declares document types and no
    `CFBundleURLTypes`, so nothing in a browser can hand it a file. The scheme is ours,
    registered by `install-drawio-url-handler.sh`, and the shim it installs does the one
    thing a `file://` link cannot — `open -a draw.io <path>` on the *original* file
    rather than on a copy Chrome downloaded.
    """
    return "drawio://" + urllib.parse.quote(str(Path(diagram).resolve()))


# draw.io's web editor takes a whole drawing in the URL fragment: `#R<data>`, where the
# data is one `<diagram>` body, which it wraps back into an `<mxfile>` on the way in. So
# the picture travels in the link itself — no server has to be able to reach the file, and
# nothing is uploaded anywhere. Sent uncompressed on purpose: draw.io also accepts the
# deflated+base64 body and it is eight times smaller, but a compression mismatch fails by
# opening an empty canvas, which reads as "the link is broken" rather than as an encoding
# bug, and a few kilobytes of fragment is the cheaper half of that trade.
DRAWIO_WEB = "https://app.diagrams.net/?splash=0&title={name}#R{data}"


def reveal_in_file_manager(diagram: Path) -> dict:
    """Show the file where it lives on disk, selected, in whatever the OS calls Finder.

    The two editor links open the drawing; neither of them answers "where *is* this file".
    That question gets asked the moment the reader wants to commit the edit, copy the
    diagram somewhere, or look at what else is in that folder — and answering it from the
    page beats reading an absolute path out of a folded shell command and pasting it.

    Recorded here and not assembled by the report, for the same reason every other command
    in this verdict is: the machine that ran this diff is the machine the review server
    will run the command on, so this is the one place that knows which OS it has to be
    phrased for. `open -R` and `explorer /select,` select the file in an already-open
    window; `xdg-open` has no such verb anywhere it is implemented, so on the rest the
    honest offer is the folder.
    """
    path = Path(diagram).resolve()
    if sys.platform == "darwin":
        return {"command": f"open -R {shlex.quote(str(path))}", "in": "the Finder"}
    if sys.platform.startswith("win"):
        return {"command": f"explorer /select,{shlex.quote(str(path))}",
                "in": "File Explorer"}
    return {"command": f"xdg-open {shlex.quote(str(path.parent))}",
            "in": "the file manager"}


def drawio_web_url(xml: str, diagram: Path) -> str:
    """The same drawing, opened in the browser editor.

    The desktop link edits the file on disk; this one edits a copy that lives in the URL.
    That is the honest difference between them and the reason both are offered: the app
    saves where the repository can see it, the web editor cannot, so it is the one to
    reach for when draw.io is not installed on this machine.
    """
    model = re.search(r"<mxGraphModel.*</mxGraphModel>", xml, re.S)
    if not model:
        return ""
    return DRAWIO_WEB.format(name=urllib.parse.quote(Path(diagram).name),
                             data=urllib.parse.quote(model.group(0), safe=""))


def link_annotations(xml: str, diagram: Path) -> str:
    """Point every annotation at the diagram it is written on.

    The annotations this diagram carries are the automation's to-dos — "Please manually
    fix the layout." is the one that exists today — and a to-do the reader cannot act on
    from where they are reading it is a caption. `parse_model` already tells an
    annotation from a concept box, so this needs no guess about the text.

    Every pane gets the link, the base pane included: "fix the layout" always means the
    file on disk, whichever revision of it you happen to be looking at.

    A note draw.io wrote as a bare `<mxCell>` — one nobody has ever attached an attribute
    to — is wrapped in a `UserObject` on the way past, because `link` lives on the wrapper
    and only on the wrapper. Setting it on the inner cell would produce no anchor and no
    error, which is the failure nobody notices until they click one.

    Nothing is written *into* the picture to advertise the click. The page carries an
    "open it in draw.io" link in HTML under the drawing, where a link can look like one
    and a cursor can change over it; a sentence painted onto the diagram to say the same
    thing sat on top of the drawing and had to be re-read every time the reader looked
    at the map, to find out it was still only an invitation.
    """
    tree = ET.fromstring(xml)
    notes = {c.id for c in parse_model(xml).values() if c.kind == "annotation"}
    if not notes:
        return xml
    url = drawio_url(diagram)
    # A snapshot, not a live walk: wrapping a cell puts a new element back into the tree
    # holding the cell we just handled, and `iter()` walks the tree as it is — it would
    # find that cell again, under its own wrapper, and wrap it forever.
    for parent in list(tree.iter()):
        if parent.tag in ("object", "UserObject"):
            continue                          # its mxCell is already inside a wrapper
        for i, child in enumerate(list(parent)):
            if child.get("id") not in notes:
                continue
            if child.tag in ("object", "UserObject"):
                child.set("link", url)
            elif child.tag == "mxCell":
                # id and label move to the wrapper, exactly the shape draw.io writes: an
                # inner cell that kept its id is read as a second, plain cell with the
                # same identity, and being the later one it wins — link and all lost.
                holder = ET.Element("UserObject", {
                    "id": child.get("id"), "link": url,
                    "label": child.get("value") or ""})
                child.attrib.pop("value", None)
                child.attrib.pop("id", None)
                holder.append(child)
                parent[i] = holder
    return ET.tostring(tree, encoding="unicode")


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
    """Keep the dark half of our green ours.

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


#: What the desktop app is asked for, every time. Part of the cache key below, so a change
#: to these flags can never be answered out of an export made with the old ones.
EXPORT_ARGS = ("-x", "-f", "svg", "--theme", "auto", "-b", "8")

# Where draw.io's own exports are kept, keyed by exactly what produced them. The desktop
# app is an Electron start-up per picture — 0.8 s each, three per run — and it is the whole
# of this tool's wall clock: the base drawing is re-exported on every run although it is
# fixed for the life of the branch, and the card's green ring is pressed after an edit that
# moved one box. A content-addressed store makes the answer to "the same XML, the same
# flags, the same app" the file the app wrote last time, byte for byte, so a hit and a
# miss cannot differ — which is the property the review page's fast refresh leans on.
# Per user rather than per checkout: the key already says everything the picture depends
# on, and a second checkout of the same branch is the same picture.
RENDER_CACHE = Path(os.environ.get("HUMAN_REVIEW_DRAWIO_CACHE")
                    or Path.home() / ".cache" / "human-review" / "drawio-svg")
RENDER_CACHE_KEEP = 400


def _app_identity() -> str:
    """Which draw.io answered: an upgrade must not be served the old version's exports."""
    plist = DRAWIO_APP.parent.parent / "Info.plist"
    try:
        st = plist.stat()
        return f"{DRAWIO_APP.resolve()}\0{st.st_size}\0{st.st_mtime_ns}"
    except OSError:
        return str(DRAWIO_APP)


def export_key(xml: str) -> str:
    h = hashlib.sha256()
    for part in (_app_identity(), "\0".join(EXPORT_ARGS), xml):
        h.update(part.encode("utf-8"))
        h.update(b"\0\1\0")
    return h.hexdigest()


def drawio_export(xml: str) -> str | None:
    """draw.io's own SVG for `xml`, untouched — out of the cache when it has been asked
    for before, else from the desktop app (and then kept). None when the app is absent or
    the export failed; the caller falls back to the built-in renderer."""
    if not DRAWIO_APP.exists():
        return None
    key = export_key(xml)
    hit = RENDER_CACHE / f"{key}.svg"
    svg = _cached(hit)
    if svg is not None:
        return svg
    # One export per picture at a time, across processes: the review server starts this
    # picture's export the moment the drawing is saved (`--warm`), and a press on the card
    # that lands while it is still going waits for that one rather than starting a second
    # Electron beside it.
    lock = None
    try:
        RENDER_CACHE.mkdir(parents=True, exist_ok=True)
        lock = open(RENDER_CACHE / f".{key}.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        svg = _cached(hit)
        return svg if svg is not None else _export(xml, key, hit)
    finally:
        if lock:
            lock.close()
            try:
                (RENDER_CACHE / f".{key}.lock").unlink()
            except OSError:
                pass


def _cached(hit: Path) -> str | None:
    try:
        svg = hit.read_text(encoding="utf-8")
        os.utime(hit)               # recently used: the pruning keeps it
        return svg
    except OSError:
        return None


def _export(xml: str, key: str, hit: Path) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.drawio", Path(tmp) / "out.svg"
        src.write_text(xml)
        proc = subprocess.run(
            [str(DRAWIO_APP), *EXPORT_ARGS, "-o", str(out), str(src), "--no-sandbox"],
            capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            print(f"draw.io export failed, falling back to the built-in renderer:\n"
                  f"{proc.stderr.strip()}", file=sys.stderr)
            return None
        svg = out.read_text(encoding="utf-8")
    try:
        RENDER_CACHE.mkdir(parents=True, exist_ok=True)
        part = RENDER_CACHE / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        part.write_text(svg, encoding="utf-8")
        os.replace(part, hit)       # atomic: a reader never sees half an export
        _prune_cache()
    except OSError:
        pass                        # a cache that cannot be written is only a slower run
    return svg


def _prune_cache() -> None:
    try:
        files = sorted(RENDER_CACHE.glob("*.svg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:max(0, len(files) - RENDER_CACHE_KEEP)]:
        try:
            old.unlink()
        except OSError:
            pass


def render_with_drawio(xml: str, out: Path) -> bool:
    svg = drawio_export(xml)
    if svg is None:
        return False
    out.write_text(dual_href(pin_added_dark(slim(svg))))
    return True


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_TAG = re.compile(r"<[^>]+>")


def _plain(label: str) -> str:
    """A label with draw.io's HTML taken out of it.

    draw.io labels are HTML — `<br>`, `<u>`, `<b>` — and draw.io renders them. This
    renderer draws one run of SVG text, and a literal `<u>` in the picture is worse
    than a missing underline.
    """
    return _TAG.sub("", re.sub(r"<br\s*/?>", " ", label))


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
              "#ff0000": "light-dark(#FF0000, #ff9090)",
              LINE_COLOR: "light-dark(#000000, #ffffff)"}

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
        st = style_dict(cell.style)
        w = float(st.get("strokeWidth", 1) or 1)
        dash = ' stroke-dasharray="6 4"' if st.get("dashed") == "1" else ""
        fade = (f' opacity="{float(st["opacity"]) / 100:g}"'
                if st.get("opacity", "").replace(".", "", 1).isdigit() else "")
        body.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" '
                    f'stroke="{stroke_of(cell)}" stroke-width="{w}"{dash}{fade}/>')
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
                        f'font-size="{size}" fill="{font_of(cell)}">{_esc(_plain(cell.label))}</text>')
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
                    f'font-weight="bold" fill="{font_of(cell)}">{_esc(_plain(cell.label))}</text>')

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


def read_many(refs: list[tuple[str, str]]) -> dict[str, str]:
    """`{ref: diagram XML}` for every `(ref, path)`, out of one `git cat-file --batch` —
    what `read_at` says for each, with an absent or unreadable blob as an empty diagram."""
    out: dict[str, str] = {}
    if not refs:
        return out
    proc = subprocess.run(["git", "cat-file", "--batch"], capture_output=True,
                          input="".join(f"{ref}:{path}\n" for ref, path in refs).encode())
    data, off = proc.stdout if proc.returncode == 0 else b"", 0
    for ref, _ in refs:
        nl = data.find(b"\n", off)
        if nl < 0:
            break
        head = data[off:nl].split()
        off = nl + 1
        if len(head) != 3 or head[1] != b"blob":
            out[ref] = EMPTY_MODEL          # `<name> missing`: absent at that revision
            continue
        size = int(head[2])
        blob, off = data[off:off + size], off + size + 1
        try:
            out[ref] = extract_xml(blob)
        except ValueError:
            out[ref] = EMPTY_MODEL
    return out


HISTORY_DEPTH = 60


def last_distinct_revision(path: str, current: str) -> dict | None:
    """The newest commit whose *drawing* is not the one on disk — the undo's real target.

    "Back to what this branch committed" was the wrong target, and it failed the first
    time it was pressed. A hand edit does not sit in the work tree waiting to be undone:
    it gets swept into the next commit that touches the file, usually one made for some
    other reason, and from then on HEAD *is* the mess. `git stash push` then finds nothing
    to save, exits 0, and the reader watches a button do nothing.

    So the undo walks back instead, and the step it takes is one *drawing*, never one
    commit. The two are not the same thing here: a `.drawio.png` is re-rendered by
    machinery — a PNG whose bytes move while every box stays where it was — and stepping
    onto one of those would be the same no-op wearing a different sha. `diff_models` is
    what already knows the difference, so it is what decides.

    That makes the offer repeatable, which is what an undo is: each press lands on a
    drawing, the next press steps past it to the one before, and a reader who has gone one
    step too far can read the sha in the fold and walk forward by hand.

    Red decides where it lands. The patch script paints what it draws red and leaves it
    red until a human lays it out, so a drawing with no red left is one somebody has
    worked on — and "undo my edits" means that work, not the commit that happens to sit
    one step below it. The walk therefore prefers the newest earlier drawing that still
    carries red, and falls back to simply the newest earlier drawing when none does.

    "Past it" is why the walk starts from where the file already stands rather than from
    the top. Once a press has landed, the drawing just left behind is the newest one that
    differs — so a search that always started at HEAD would answer the second press with
    the thing the first press undid, and the offer would rock between two revisions
    forever. Pressing undo twice means *further back*, never *never mind*.
    """
    log = subprocess.run(
        ["git", "log", f"-{HISTORY_DEPTH}", "--format=%H%x09%as%x09%s", "--", path],
        capture_output=True, text=True)
    if log.returncode != 0:
        return None
    entries = []
    rows = [line for line in log.stdout.splitlines() if line.strip()]
    # Every revision's bytes in one `git cat-file --batch`, not one `git show` each: sixty
    # processes were half a second of the green ring's wait, for the same blobs.
    blobs = read_many([(line.partition("\t")[0], path) for line in rows])
    for line in rows:
        sha, _, rest = line.partition("\t")
        date, _, subject = rest.partition("\t")
        xml = blobs.get(sha, EMPTY_MODEL)
        if xml != EMPTY_MODEL:
            entries.append(({"sha": sha, "short": sha[:8], "date": date,
                             "subject": subject}, xml))

    def differs(xml: str) -> bool:
        # `moved` counts. Dragging a box somewhere wrong is a hand edit like any other,
        # and the one an undo is asked for most.
        delta = diff_models(xml, current)
        return any(delta[k] for k in ("added", "removed", "changed", "moved"))

    # Where the file already stands in its own history, if it stands anywhere. Without
    # this the offer ping-pongs: one press lands on the previous drawing, and the next
    # press finds *the one just left* as the newest thing that differs and goes straight
    # back to it. A reader pressing undo twice means "further", never "never mind".
    start = 0
    for i, (_, xml) in enumerate(entries):
        if not differs(xml):
            start = i + 1
            break

    # What "my edits" *are*, when the drawing on disk carries no red. Red is the patch
    # script's own mark — "a machine put this here and nobody has laid it out yet" — so a
    # drawing with none left has been through a human's hands, and the thing that human
    # did is exactly what the undo is being asked to take back. Landing one commit earlier
    # does not do that: the step before a re-layout is usually a re-render, a moved label,
    # a caption removed, and the reader presses undo, watches the same laid-out line come
    # back, and concludes the button is broken. It was, for this.
    #
    # So when there is no red here, the target is the newest earlier drawing that still
    # has some: automation's own, which is what a person means by "before I touched it".
    # When there IS red here, nobody has laid this out yet, there is no re-layout to
    # undo, and stepping back one drawing is the honest answer again.
    on_red = any(is_red(c.style) for c in parse_model(current).values())
    if not on_red:
        for meta, xml in entries[start:]:
            if differs(xml) and any(is_red(c.style) for c in parse_model(xml).values()):
                return {**meta, "machine_drawn": True}

    for meta, xml in entries[start:]:
        if differs(xml):
            return {**meta, "machine_drawn": False}
    return None


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
    ap.add_argument("--concepts", metavar="PUML",
                    help="the generated domain-model PlantUML, whose class links say "
                         "where each concept is declared; every concept box in every "
                         "pane becomes a link into that class. REQUIRED: without it the "
                         "command still succeeds and quietly produces boxes that are not "
                         "links, which is the failure nobody notices until they click one")
    ap.add_argument("--traces", metavar="C2_JSON",
                    help="the container graph c2-from-sequence.py projected from the "
                         "traced sequence diagrams: each pane is drawn with the arrows a "
                         "test walked at double weight, the others thin, and the calls "
                         "no arrow carries added in red. Replaces --concepts for a diagram "
                         "of containers rather than of concepts")
    ap.add_argument("--trace-attr", default=TRACE_ATTR,
                    help="the attribute by which a box names its lifeline in the traces")
    ap.add_argument("--repo-root", default=".",
                    help="what the paths inside --concepts are relative to")
    ap.add_argument("--redraw", metavar="COMMAND",
                    help="the repository's own patch script — the thing that draws a "
                         "missing box or line in red to keep its guardrail green. Recorded "
                         "in the verdict, where the report turns it into the one offer it "
                         "cannot make on its own: throw a hand-drawn layout away and let "
                         "automation draw this diagram again. Not guessed from --base: a "
                         "script that rewrites a checked-in file is not something to "
                         "derive from a naming convention and run on a reader's click")
    ap.add_argument("--tested-against-path", metavar="DIR",
                    help="the source package that test scans (relative to --repo-root); the "
                         "`--tested-against` label links to it in VS Code. Without it the "
                         "label is plain text")
    ap.add_argument("--tested-against", metavar="WHAT",
                    help="what a unit test checks this drawing against (e.g. `Java Domain "
                         "Model`), for the sentence under the picture. Recorded, not "
                         "inferred: only the project knows which guardrail keeps it honest")
    ap.add_argument("--json", action="store_true",
                    help="print the verdict as JSON instead of a summary line")
    ap.add_argument("--warm", action="store_true",
                    help="export the three pictures into draw.io's render cache and write "
                         "nothing else — what the review server runs when it sees the "
                         "drawing saved, so the card's refresh finds them ready")
    args = ap.parse_args()
    if not args.concepts and not args.traces:
        ap.error("--concepts is required (or --traces, for a diagram of containers)")

    if args.base:
        if not args.diagram:
            ap.error("--base needs --diagram")
        source = Path(args.diagram)
        if not source.is_file():
            sys.exit(f"no diagram at {source}")
        old_xml, new_xml = read_at(args.base, args.diagram), extract_xml(source)
        stem = args.name or source.name.split(".")[0]
    elif args.old and args.new:
        source = Path(args.new)
        old_xml, new_xml = extract_xml(args.old), extract_xml(args.new)
        stem = args.name or source.name.split(".")[0]
    else:
        ap.error("give two diagrams, or --base REF --diagram PATH")

    verdict = diff_models(old_xml, new_xml)
    out_dir = Path(args.out_dir)

    # One map, from the WORKING TREE, applied to all three panes. That is what makes the
    # "old" pane behave: a concept this branch deleted is simply not in it, so its box on
    # the base diagram quietly loses its link instead of pointing at a file that is gone.
    sources = concept_sources(Path(args.concepts)) if args.concepts else {}
    unresolved = set()

    def linked(xml):
        out, missing = link_concepts(xml, sources, args.repo_root)
        unresolved.update(missing)
        return link_annotations(out, source)

    # Each pane against the traces of its own side: the base drawing against the calls
    # the base's tests made, the branch's against the branch's.
    reports = {}

    def traced(xml, side):
        if not args.traces:
            return xml
        out, reports[side] = overlay_traces(xml, trace_edges(Path(args.traces), side),
                                            args.trace_attr)
        return out

    # The three pictures' XML first, here, in order — `linked` gathers the concepts it could
    # not resolve, and that set is the verdict's — then the three exports side by side. Each
    # one is a draw.io start-up of its own, so in a row they were the whole of this tool's
    # wall clock; in parallel they cost about one. `last_distinct_revision` (a walk through
    # the file's history) runs beside them for the same reason.
    panes = {"original": linked(traced(old_xml, "old")),
             "new": linked(traced(new_xml, "new")),
             "diff": linked(traced(paint_added(new_xml, verdict), "new"))}
    if args.warm:
        # Put the pictures in the export cache and touch nothing else: the review server
        # runs this when it sees the drawing saved, so the press that follows finds them.
        with concurrent.futures.ThreadPoolExecutor(len(panes)) as pool:
            list(pool.map(drawio_export, panes.values()))
        return
    with concurrent.futures.ThreadPoolExecutor(len(panes) + 1) as pool:
        history = (pool.submit(last_distinct_revision, args.diagram, new_xml)
                   if args.base else None)
        jobs = {view: pool.submit(render, xml, out_dir / f"{stem}-{view}.svg", args.renderer)
                for view, xml in panes.items()}
        written = {view: job.result() for view, job in jobs.items()}
        revision = history.result() if history else None
    if args.traces:
        verdict["traces"] = {**reports["new"], "source": str(args.traces)}
    verdict["linked_concepts"] = sorted(sources)
    verdict["unlinked_concepts"] = sorted(unresolved)
    verdict["renderer"] = written["diff"]
    verdict["added_color"] = ADDED_COLOR
    # The click the report offers under the picture. Recorded here rather than rebuilt by
    # the page, because only this run knows which file on disk was actually diffed.
    verdict["drawio_url"] = drawio_url(source)
    verdict["diagram"] = str(source)
    verdict["drawio_web_url"] = drawio_web_url(new_xml, source)
    verdict["reveal"] = reveal_in_file_manager(source)
    if args.tested_against:
        verdict["tested_against"] = args.tested_against
        if args.tested_against_path:
            verdict["tested_against_path"] = str(
                (Path(args.repo_root) / args.tested_against_path).resolve())
    # How to run this again, recorded by the run itself. The report inlines these SVGs at
    # build time — it has to, or the links drawn inside them go inert — so a reader who
    # has just re-laid the diagram out by hand needs a command, and the reader is not the
    # person who knows this tool's flags. Nothing else on the machine knows the arguments
    # this invocation used; this line does, because it *is* this invocation.
    verdict["rerun"] = {
        "cwd": str(Path.cwd()),
        "command": " ".join(shlex.quote(a) for a in
                            [str(Path(__file__).resolve()), *sys.argv[1:]]),
    }
    # The other direction, and the only one the reader cannot reconstruct: put automation's
    # own drawing back. Re-laying the map out by hand is the whole point of the red, and it
    # is also the one step on this page with no undo — the layout is in the file, the file
    # is in the repository, and "I would like to see what the machine drew" means finding a
    # revision by hand. Two halves, and only one of them is ours: restoring the diagram to
    # its base state is derivable from the flags this run already has, and redrawing it is
    # the repository's own script, which is why it has to be passed in.
    # A traced diagram needs no script: restoring the base is enough, the rerun lays the
    # traces' red to-dos over it again.
    if args.base and (args.redraw or args.traces):
        restore = f"git checkout {shlex.quote(args.base)} -- {shlex.quote(str(source))}"
        verdict["redraw"] = {
            "cwd": str(Path.cwd()),
            "command": f"{restore} && {args.redraw}" if args.redraw else restore,
            "diagram": str(source),
            "base": args.base,
        }
    # The other way back, and the one wanted far more often than starting over: undo *my*
    # edits. The redraw above goes all the way to the base and lets the patch script stage
    # what the branch added — in red, deliberately unplaced, so the guardrail keeps failing
    # until a human drags it somewhere. That is the to-do state, and it is not a green one.
    # A reader who has just made a mess of a layout wants neither the base nor a to-do:
    # they want the last drawing that was not this one.
    #
    # Two commands, because there are two ways an edit can be in the way. Anything still
    # loose in the work tree is banked — `git stash push` and not `git checkout --`, since
    # both put the file back and only one of them keeps what it took, and a button on a web
    # page gets pressed by accident. Then the drawing itself comes out of history by sha.
    # The stash is a no-op when there is nothing loose, which is the case that broke the
    # first version of this offer: the hand edit had already been committed, so "back to
    # HEAD" was "back to the mess".
    if revision:
        stash = shlex.quote(f"human-review: hand edits to {args.diagram}")
        verdict["revert"] = {
            "cwd": str(Path.cwd()),
            "command": (f"git stash push -m {stash} -- {shlex.quote(str(source))} "
                        f"&& git checkout {revision['sha']} -- {shlex.quote(str(source))}"),
            "diagram": str(source),
            **revision,
        }
    (out_dir / f"{stem}-diff.json").write_text(json.dumps(verdict, indent=2))

    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        print(f"{out_dir}/{stem}-{{original,new,diff}}.svg  ({counted(verdict)}"
              f", rendered by {written['diff']})")
        for item in verdict["added"]:
            if item["kind"] == "label":
                continue
            colour = "red (automation's to-do)" if item["already_red"] else "green (new)"
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
