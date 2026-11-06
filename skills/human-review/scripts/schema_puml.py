#!/usr/bin/env python3
"""An OpenAPI payload as a PlantUML class diagram, and its delta against the base.

The API tab's schema tree answers "what is in this field?", one level at a time. It cannot
answer "what is the shape of this payload?": on `GET /api/pets` the three new vet fields sit
two levels down a tree that has to be opened by hand. This module draws that shape, from the
two specs, for each request and response body: an arrow in from a small Response or Request
box, then one class per object schema, its plain fields as members, and every nested or
referenced object (array items too) as an association.

**No second differ.** The two sides are written as plain `.puml` text, and from there it is
the Data tab's machinery, `puml-diff/puml_diff.py`, unchanged: same green, same struck red,
same amber ripple, same caption. A body with nothing to mark is drawn plain, which is what
the Data tab draws for an unchanged diagram, and so is a body the base did not have at all
(`puml-diff.sh` does the same for an added diagram: all green says "all of this changed",
and what happened is "this is new"). An operation the branch removed has no new side and is
drawn all struck.

The classes are read off the raw specs, not off the inlined one Swagger UI draws: a `$ref`
is what names a class, and one followed `$ref` is one class however many fields reach it.

**Many operations share a schema**, so a diagram is keyed by the hash of its text, and the
page carries each distinct one once. `allocate` caps them (changed bodies are served first),
and a body past the cap, or one PlantUML refused, simply has no diagram: the page does not
offer the switch there and the tree goes on working.

`plantuml` is optional, as it is on the Data tab: missing, `render_svgs` answers nothing and
the tab is what it was.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "puml-diff"))
import puml_diff  # noqa: E402 - ships with the skill, beside this directory

REQUEST = "request"

ENUM_SHOWN = 8           # an inline enum's values, on the field's own line
MAX_REF_HOPS = 32        # a chain of `$ref`s longer than this is a loop
MAX_DIAGRAMS = 150       # distinct diagrams one page may carry; each one is a JVM's work
MAX_SVG_BYTES = 2_500_000  # and their weight, against a page of 3-4 MB
RENDER_TIMEOUT = 300     # seconds, for the one JVM that draws all of them

# Left to right, unlike the Data tab's: this picture sits inside a response row, where the
# width is there for the taking and every pixel of height pushes the next status down.
PREAMBLE = ("left to right direction", "hide empty members",
            "skinparam classAttributeIconSize 0")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# PlantUML draws its failures: both of these arrive as a valid .svg, exit code 0.
_RENDER_FAILED = ("Syntax Error", "Cannot find Graphviz", "Dot executable")
# Dead weight, and a `<!--` inside a <script> is a parser state nobody wants to be in.
_SVG_NOISE = re.compile(r'<!--.*?-->|<\?plantuml.*?\?>|\sdata-source-line="\d+"', re.S)


def _alias(name: str) -> str:
    return name if _PLAIN_NAME.match(name) else re.sub(r"\W", "_", name)


def _types(schema: dict) -> list:
    """The schema's types, without `null`: 3.1 spells nullable as `[string, 'null']`."""
    t = schema.get("type")
    return [x for x in (t if isinstance(t, list) else [t]) if x and x != "null"]


def _nullable(schema: dict) -> bool:
    t = schema.get("type")
    return bool(schema.get("nullable")) or (isinstance(t, list) and "null" in t)


def _primitive(schema: dict) -> str:
    """The most specific word: `date` rather than `string`, `int64` rather than `integer`."""
    types = _types(schema)
    word = schema.get("format") if isinstance(schema.get("format"), str) else None
    word = word or (" | ".join(types) if types else "any")
    values = schema.get("enum")
    if isinstance(values, list) and values:
        shown = ", ".join(str(v) for v in values[:ENUM_SHOWN])
        more = len(values) - ENUM_SHOWN
        word = f"{word} {{{shown}{f', +{more}' if more > 0 else ''}}}"
    return word + ("?" if _nullable(schema) else "")


class Model:
    """The classes one payload reaches, in the order the payload reaches them."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.classes: dict = {}   # name -> {"kind": "class"|"enum", "members": [...]}
        self.links: list = []     # (from class, to class, end label)

    def follow(self, node) -> tuple:
        """`node` with every `$ref` in front of it followed, and the name the last one gave."""
        name = None
        for _ in range(MAX_REF_HOPS):
            ref = node.get("$ref") if isinstance(node, dict) else None
            if not isinstance(ref, str):
                break
            name = ref.rsplit("/", 1)[-1]
            node = self._at(ref)
            if node is None:         # an external ref: the name is all there is to say
                return {}, name
        return (node if isinstance(node, dict) else {}), name

    def _at(self, ref: str):
        if not ref.startswith("#/"):
            return None
        node = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def flat(self, schema, depth: int = 0) -> tuple:
        """`schema` with its `allOf` folded in, and its name.

        `allOf: [{$ref: X}]` beside a description is how a generator says "an X, nullable",
        so a lone part with no properties of its own lends its name; two parts make a new
        shape, named for its owner, which is the only name it has."""
        s, name = self.follow(schema)
        parts = s.get("allOf")
        if not isinstance(parts, list) or depth > MAX_REF_HOPS:
            return s, name
        merged = {k: v for k, v in s.items() if k not in ("allOf", "properties", "required")}
        props, required = {}, []
        for part in parts:
            sub, sub_name = self.flat(part, depth + 1)
            if len(parts) == 1 and not s.get("properties"):
                name = name or sub_name
            props.update(sub.get("properties") or {})
            required += sub.get("required") or []
            for key in ("type", "format", "enum", "items", "additionalProperties",
                        "oneOf", "anyOf"):
                if key in sub:
                    merged.setdefault(key, sub[key])
        props.update(s.get("properties") or {})
        required += s.get("required") or []
        return {**merged, "properties": props, "required": required}, name

    def reach(self, schema, hint: str) -> tuple:
        """What a schema is, as a field sees it: `(type word, classes it lands on, many?)`."""
        s, name = self.flat(schema)
        variants = s.get("oneOf") or s.get("anyOf")
        if isinstance(variants, list) and variants:
            found = [self.reach(v, f"{hint}{i + 1}") for i, v in enumerate(variants)]
            words = list(dict.fromkeys(f[0] for f in found))
            classes = list(dict.fromkeys(c for f in found for c in f[1]))
            return " | ".join(words), classes, any(f[2] for f in found)
        types = _types(s)
        if "array" in types or "items" in s:
            word, classes, _ = self.reach(s.get("items") or {}, hint)
            return f"{word}[]", classes, True
        if s.get("properties") or (name and "object" in types):
            return self._class(name or s.get("title") or hint, s)
        extra = s.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            word, classes, _ = self.reach(extra, hint)
            return f"map[{word}]", classes, True
        if name and isinstance(s.get("enum"), list) and s["enum"]:
            self.classes.setdefault(name, {"kind": "enum",
                                           "members": [str(v) for v in s["enum"]]})
            return name, [name], False
        if name and not s:           # an external ref: nothing to open, still a type
            return name, [], False
        if "object" in types:
            return "object", [], False
        return _primitive(s), [], False

    def _class(self, name: str, s: dict) -> tuple:
        if name not in self.classes:     # registered first: a schema may reach itself
            members: list = []
            self.classes[name] = {"kind": "class", "members": members}
            required = s.get("required") or []
            for prop, sub in (s.get("properties") or {}).items():
                word, classes, many = self.reach(sub, f"{name}.{prop}")
                must = prop in required
                if not classes:
                    members.append(f"{prop}{'*' if must else ''} : {word}")
                for target in classes:
                    # The multiplicity sits in the end label, as on the Data tab's diagram:
                    # the differ reads a changed one as a changed association.
                    end = "~*" if many else "1" if must else "0..1"
                    self.links.append((name, target, f"{end} {prop}"))
        return name, [name], False

    def puml(self) -> str:
        lines = ["@startuml", *PREAMBLE, ""]
        for name, c in self.classes.items():
            alias = _alias(name)
            head = name if alias == name else f'"{name}" as {alias}'
            lines += [f"{c['kind']} {head} {{", *(f"  {m}" for m in c["members"]), "}"]
        lines.append("")
        lines += [f'{_alias(a)} --> "{end}" {_alias(b)}' for a, b, end in self.links]
        return "\n".join([*lines, "", "@enduml"]) + "\n"


EMPTY = Model({}).puml()


def schema_puml(spec: dict, schema, root: str) -> str | None:
    """One payload as PlantUML, or None when it holds no object at all (a bare string).

    A small `root` box (Response, Request) points at where the payload starts, with the
    arrow's end saying one or many. It is what makes a change of the payload's own shape
    visible: a list that became a page object is that arrow, red to the old class and green
    to the new one, and no class in between has a field to mark it. An inline payload is
    already named `root`, so it needs no box."""
    model = Model(spec)
    _, classes, many = model.reach(schema, root)
    if not model.classes:
        return None
    if root not in model.classes:
        model.classes = {root: {"kind": "class", "members": []}, **model.classes}
        model.links[:0] = [(root, target, "~*" if many else "1") for target in classes]
    return model.puml()


def delta(old: str | None, new: str | None, absent: bool = False) -> tuple:
    """`(puml, changed)`: the delta of two sides, or `new` plain when there is nothing to mark.

    `absent` says the base had no such body at all, not merely no class in it."""
    if absent or old == new:
        return new, False
    before, after = puml_diff.parse(old or EMPTY), puml_diff.parse(new or EMPTY)
    if not puml_diff._impacted(before, after):
        return new, False
    return puml_diff.diff(before, after), True


def bodies(spec: dict, op: dict) -> dict:
    """Every body of one operation: `{"request application/json": (schema, root), …}`.

    The key is what the page can read off a block: `request`, or the `data-code` of the
    response row, and the value of the media type select."""
    model = Model(spec)
    out: dict = {}

    def add(slot: str, holder, root: str):
        body, _ = model.follow(holder)
        content = body.get("content")
        if not isinstance(content, dict):
            return
        for media, entry in content.items():
            if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
                out[f"{slot} {media}"] = (entry["schema"], root)

    if op.get("requestBody"):
        add(REQUEST, op["requestBody"], "Request")
    responses = op.get("responses")
    for status, response in (responses.items() if isinstance(responses, dict) else ()):
        add(str(status), response, "Response")
    return out


def _old_body(old: dict, key: str):
    """The base's side of a body. A swapped media type (`*/*` to `application/problem+json`)
    is still the same response, so the same slot under another media type will do."""
    if key in old:
        return old[key]
    slot = key.split(" ", 1)[0]
    return next((v for k, v in old.items() if k.split(" ", 1)[0] == slot), None)


def plan(old_spec: dict, new_spec: dict, old_ops: dict, new_ops: dict) -> tuple:
    """What every body of the rendered spec would draw.

    `(diagrams, per_op, changed)`: `{id: puml}`, each distinct text once;
    `{"GET /x": {"200 */*": id}}`; and the ids that show a change."""
    diagrams: dict = {}
    per_op: dict = {}
    changed: set = set()
    for key in sorted(set(old_ops) | set(new_ops)):
        was = bodies(old_spec, old_ops[key]) if key in old_ops else {}
        gone = key not in new_ops      # a removed operation is drawn off its old side
        now = was if gone else bodies(new_spec, new_ops[key])
        for slot, (schema, root) in now.items():
            before = _old_body(was, slot)
            old_text = schema_puml(old_spec, before[0], before[1]) if before else None
            new_text = None if gone else schema_puml(new_spec, schema, root)
            text, moved = delta(old_text, new_text, absent=before is None)
            if not text:
                continue
            ident = hashlib.sha1(text.encode()).hexdigest()[:10]  # noqa: S324 - a cache key
            diagrams[ident] = text
            per_op.setdefault(key, {})[slot] = ident
            if moved:
                changed.add(ident)
    return diagrams, per_op, changed


def _queue(diagrams: dict, changed: set) -> list:
    """The order the budget is spent in: what moved first, a reviewer's reason to be here."""
    return sorted(diagrams, key=lambda ident: ident not in changed)


def allocate(diagrams: dict, per_op: dict, changed: set,
             render=None, limit: int = MAX_DIAGRAMS, budget: int = MAX_SVG_BYTES) -> tuple:
    """`(svgs, per_op)`: the drawn pictures, and only the bodies that have one."""
    render = render or render_svgs
    wanted = _queue(diagrams, changed)[:limit]
    drawn = render({ident: diagrams[ident] for ident in wanted})
    svgs: dict = {}
    spent = 0
    for ident in wanted:
        svg = drawn.get(ident)
        if svg and spent + len(svg) <= budget:
            svgs[ident] = svg
            spent += len(svg)
    kept = {}
    for op, slots in per_op.items():
        mine = {slot: ident for slot, ident in slots.items() if ident in svgs}
        if mine:
            kept[op] = mine
    return svgs, kept


def render_svgs(pumls: dict) -> dict:
    """`{id: puml}` to `{id: themed inline svg}`, in one JVM. Whatever failed is left out.

    Nothing here is allowed to fail a build: a diagram is a second view of a tree that is
    already on the page."""
    exe = shutil.which("plantuml")
    if not exe or not pumls:
        return {}
    try:
        # Beside this file, except in the copy of the script that travels alone.
        sys.path.insert(0, str(HERE))
        from hrbuild.shared.svg import inline_svg
    except ImportError:
        return {}
    with tempfile.TemporaryDirectory(prefix="schema-puml-") as tmp:
        folder = Path(tmp)
        sources = []
        for ident, text in pumls.items():
            source = folder / f"{ident}.puml"
            source.write_text(text, encoding="utf-8")
            sources.append(str(source))
        try:
            subprocess.run([exe, "-tsvg", "-nometadata", "-charset", "UTF-8", *sources],
                           capture_output=True, timeout=RENDER_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[schema-puml] plantuml failed: {e}", file=sys.stderr)
            return {}
        out = {}
        for ident in pumls:
            path = folder / f"{ident}.svg"
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8")
            if any(word in raw for word in _RENDER_FAILED):
                continue
            out[ident] = _SVG_NOISE.sub("", inline_svg(path, folder))
    return out
