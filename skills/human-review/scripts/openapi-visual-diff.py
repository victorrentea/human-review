#!/usr/bin/env python3
"""
openapi-visual-diff — a Swagger-UI-shaped visual diff of two OpenAPI specs.

Renders the NEW spec in real Swagger UI, then fades every endpoint nobody
touched and lights up the ones the diff actually hit (breaking / modified /
added / removed), annotating each with the concrete changes from oasdiff.

    ./openapi-visual-diff.py old.yaml new.yaml -o diff.html

Requires: oasdiff on PATH (brew install oasdiff), PyYAML.
"""
import argparse
import copy
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# oasdiff levels
INFO, WARN, ERROR = 1, 2, 3


def load_spec(path: Path) -> dict:
    import yaml
    with path.open() as f:
        return yaml.safe_load(f)


def operations(spec: dict):
    """Yield (METHOD, path, operation_object) for every operation in the spec."""
    for p, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for m in METHODS:
            if m in item:
                yield m.upper(), p, item[m]


def run_oasdiff(old: Path, new: Path) -> list:
    cmd = ["oasdiff", "changelog", str(old), str(new), "-f", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"oasdiff failed:\n{proc.stderr}")
    out = proc.stdout.strip()
    return json.loads(out) if out else []


# oasdiff reports a swapped media type as a removal plus an addition — two lines,
# one shouting "breaking" and the next one shrugging "info", for what is a single
# fact: it changed. Fold each such pair back into one line, keeping the higher
# severity. Wordings below are oasdiff's own, verified against its output.
MERGE_RULES = (
    {
        "removed_id": "response-media-type-removed",
        "added_id": "response-media-type-added",
        "removed_re": r"^removed the media type `(?P<what>[^`]+)` for the response "
                      r"with the status `(?P<key>[^`]+)`$",
        "added_re": r"^added the media type `(?P<what>[^`]+)` for the response "
                    r"with the status `(?P<key>[^`]+)`$",
        "text": "the response media type for status `{key}` changed "
                "from `{old}` to `{new}`",
    },
    {
        "removed_id": "request-body-media-type-removed",
        "added_id": "request-body-media-type-added",
        "removed_re": r"^removed the media type `(?P<what>[^`]+)` from the request body$",
        "added_re": r"^added the media type `(?P<what>[^`]+)` to the request body$",
        "text": "the request body media type changed from `{old}` to `{new}`",
    },
)


# oasdiff writes a response-property change as
#     added the optional property `items/pets/items/visits/items/vetId`
#     to the response with the `200` status
# which buries the one thing the line is about -- the path -- in the middle, and then
# trails eleven characters of ceremony behind it. Two edits fix the shape. `response`
# moves in front of `property`, where it belongs: it qualifies the property, it is not
# somewhere the property was sent. And the path goes last, so a column of these lines can
# be read straight down the field names instead of hunting for them at a different offset
# on every row. The result reads the way oasdiff already writes the request side --
# "added the new optional request property `vetId`".
RESPONSE_PROPERTY_RE = re.compile(
    r"^(?P<verb>added|removed) the (?P<quals>(?:[\w-]+ )*?)property "
    r"`(?P<path>[^`]+)` (?:to|from) the response with the `(?P<status>[^`]+)` status$"
)

# A 2xx is the response everybody means; naming it on every line is ceremony, and there
# are twenty such lines on a single endpoint. Any other status IS the news in the line
# and stays -- which is rare enough that the long form costs nothing when it happens.
SUCCESS_STATUS = re.compile(r"^2(?:\d\d|XX|xx)$")

# The same clause trailing any of oasdiff's other response wordings, in either of the two
# word orders it uses ("with the `200` status", "for the status `200`").
TRAILING_STATUS_RE = re.compile(
    r"\s(?:with|for)(?: the)? (?:status `(?P<s1>[^`]+)`|`(?P<s2>[^`]+)` status)$"
)


def rephrase(text: str) -> str:
    """oasdiff's wording, with the response-status ceremony taken out of the way."""
    m = RESPONSE_PROPERTY_RE.match(text)
    if m:
        status = ("" if SUCCESS_STATUS.match(m["status"])
                  else f"`{m['status']}` ")
        return (f"{m['verb']} the {m['quals']}{status}response property "
                f"`{m['path']}`")
    m = TRAILING_STATUS_RE.search(text)
    if m and SUCCESS_STATUS.match(m["s1"] or m["s2"]):
        return text[:m.start()]
    return text


def merge_pairs(changes: list) -> list:
    """Collapse remove+add pairs on the same thing into a single 'changed' line."""
    out = list(changes)
    for rule in MERGE_RULES:
        def bucket(change_id, pattern):
            found = {}
            for c in out:
                if c["id"] != change_id:
                    continue
                m = re.match(pattern, c["text"])
                if m:
                    found.setdefault(m.groupdict().get("key", ""), []).append(
                        (c, m.group("what")))
            return found

        gone = bucket(rule["removed_id"], rule["removed_re"])
        came = bucket(rule["added_id"], rule["added_re"])
        for key in set(gone) & set(came):
            # only unambiguous 1-for-1 swaps; a real many-to-many stays verbatim
            if len(gone[key]) != 1 or len(came[key]) != 1:
                continue
            (rem, old), (add, new) = gone[key][0], came[key][0]
            merged = dict(rem)
            merged["text"] = rule["text"].format(key=key, old=old, new=new)
            merged["level"] = max(rem["level"], add["level"])
            merged["id"] = f'{rule["removed_id"]}+{rule["added_id"]}'
            out = [merged if c is rem else c for c in out if c is not add]
    return out


def inline_refs(spec: dict) -> dict:
    """Expand every internal $ref in place.

    Swagger UI resolves `#/components/schemas/X` against the *page* URL, so a
    report opened from disk (file://) can't fetch its own base document and every
    ref becomes a "Resolver error". A blob: or data: URL doesn't help — the
    resolver can't build a URL from those either. So we resolve the pointers
    ourselves and hand Swagger UI a spec with nothing left to look up.
    """
    def target(pointer: str):
        node = spec
        for part in pointer.lstrip("#/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def walk(node, stack: tuple):
        if isinstance(node, list):
            return [walk(v, stack) for v in node]
        if not isinstance(node, dict):
            return node

        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            name = ref.rsplit("/", 1)[-1]
            if ref in stack:  # self-referential schema — stop, don't recurse forever
                return {"title": name, "type": "object",
                        "description": f"↻ recursive reference to {name}"}
            resolved = target(ref)
            if resolved is None:
                return node
            expanded = walk(resolved, stack + (ref,))
            if isinstance(expanded, dict):
                # keep the schema's name visible in the UI, and any $ref siblings
                expanded = {"title": name, **expanded,
                            **{k: walk(v, stack) for k, v in node.items() if k != "$ref"}}
            return expanded

        return {k: walk(v, stack) for k, v in node.items()}

    out = walk(spec, ())
    return out


# ── from "this operation changed" to "this field, right here" ─────────────────
# "expand impacted" used to stop at the operation. That is exactly half the job: the
# fields this branch added live several levels inside a response schema, so the reader
# still had to open the Schema tab and hand-expand four nodes to find them — and a tree
# that opens to the operation and no further teaches the reader it is already showing
# them everything, which is the one lie this page must not tell.
#
# oasdiff names the property it changed, as a slash path in its own prose:
#     added the optional property `items/pets/items/visits/items/vetFirstName`
# `items` is an array descent, anything else is an object property — which happens to be
# exactly the two node kinds Swagger UI's JSON-Schema renderer draws. So the ancestor
# chain is computed HERE, against the already-inlined schema, and handed to the page as
# a list of steps to walk. Nothing is discovered by expanding and looking: Swagger UI
# rebuilds a collapsed subtree from scratch, so anything read out of the rendered tree
# is gone the moment it re-renders.
#
# Resolving against the real schema (rather than trusting the prose) is what keeps this
# honest: a path that does not resolve produces no target at all, and the change stays
# in the note list where it always was. A removed property is the everyday case — it is
# absent from the spec being rendered, so the tree genuinely cannot point at it.
MAX_REVEAL_DEPTH = 12    # steps down one schema; deeper is left closed and said so
MAX_REVEAL_PER_OP = 12   # leaves opened per operation, most severe first

BACKTICKED = re.compile(r"`([^`]+)`")
RESPONSE_STATUS = re.compile(r"with the `(\d{3})` status")


def first_media_schema(content) -> dict | None:
    """The schema Swagger UI shows first — its media-type picker opens on the first."""
    if not isinstance(content, dict):
        return None
    for media in content.values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return None


def response_schema(op: dict, status: str) -> dict | None:
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return None
    # YAML reads a bare `200:` as an int, a quoted `"200":` as a string. Both occur.
    body = responses.get(status)
    if body is None:
        try:
            body = responses.get(int(status))
        except ValueError:
            body = None
    return first_media_schema(body.get("content")) if isinstance(body, dict) else None


def request_schema(op: dict) -> dict | None:
    body = op.get("requestBody")
    return first_media_schema(body.get("content")) if isinstance(body, dict) else None


def resolve_steps(schema, tokens: list) -> list | None:
    """oasdiff's slash path -> the nodes Swagger UI actually draws, or None.

    None on anything we cannot follow exactly: a composed schema (`allOf`), a property
    that is not in the rendered spec because it was removed, or a chain that runs past
    MAX_REVEAL_DEPTH. Guessing here would open the wrong branch and mark the wrong field.
    """
    node, steps = schema, []
    for token in tokens:
        if not isinstance(node, dict) or len(steps) >= MAX_REVEAL_DEPTH:
            return None
        items = node.get("items")
        if token == "items" and isinstance(items, dict):
            steps.append({"kind": "items"})
            node = items
            continue
        props = node.get("properties")
        if isinstance(props, dict) and token in props:
            steps.append({"kind": "prop", "name": token})
            node = props[token]
            continue
        return None
    return steps or None


def change_target(op: dict, change: dict) -> dict | None:
    """Where in the rendered schema this one change landed, or None if we cannot say."""
    cid, text = change.get("id") or "", change.get("text") or ""
    if "request" in cid:
        schema, where = request_schema(op), {"in": "request"}
    elif "response" in cid:
        found = RESPONSE_STATUS.search(text)
        if not found:
            return None
        schema = response_schema(op, found.group(1))
        where = {"in": "response", "status": found.group(1)}
    else:
        return None
    if not isinstance(schema, dict):
        return None
    # The property path is whichever backticked token resolves. oasdiff writes several
    # rule wordings and they are not worth enumerating; a token that walks the schema
    # cleanly is the path, and one that does not cannot be.
    for token in BACKTICKED.findall(text):
        if re.fullmatch(r"\d{3}", token):
            continue
        steps = resolve_steps(schema, token.split("/"))
        if steps:
            return {**where, "steps": steps, "field": token.split("/")[-1]}
    return None


def change_mark(cid: str) -> str:
    """The word that goes on the leaf, in the differ's own vocabulary."""
    if "removed" in cid:
        return "removed"
    if "added" in cid or cid.startswith("new-"):
        return "added"
    return "changed"


# ── what is no longer there ─────────────────────────────────────────────────────
# Swagger UI draws the NEW spec, so everything the branch took away is simply absent:
# `GET /api/owners` used to answer `array<OwnerDto>` and the page showed only the
# OwnerPageDto that replaced it, as if nothing had ever stood there. oasdiff does say so
# in prose, but a removal you can only read about in a list above the tree is a removal
# nobody sees where it happened.
#
# So the generator diffs the two (already inlined) specs structurally and hands the
# page a list of "ghosts": each one is the old subtree plus the exact spot in the NEW
# rendered tree it is to be drawn at -- the same `steps` vocabulary `resolve_steps`
# emits, so the page finds the spot with the walker it already has. Kinds:
#   prop      a property of an object that is gone (steps lead to that object)
#   type      a node whose shape changed -- array<OwnerDto> -> object (steps lead to it)
#   param     a removed parameter
#   response  a removed response status
#   media     a removed media type of a response or of the request body
#   body      a request body that is gone altogether
# Composed schemas (allOf/oneOf/anyOf) are not descended into: which branch a property
# came from is not something the tree on screen can show, and a guess is worse than
# the prose line oasdiff already wrote.
GHOST_DEPTH = 6        # how deep the old shape is carried along to be expanded
GHOST_MAX_PROPS = 60   # per node; a ghost is a reminder, not a second schema browser
COMPOSED = ("allOf", "oneOf", "anyOf")


def _types(s: dict) -> set:
    t = s.get("type")
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str) and x != "null"}
    return {t} if isinstance(t, str) else set()


def shape_kind(s) -> str | None:
    """'array', 'object', a primitive type name, or None when we cannot say."""
    if not isinstance(s, dict) or any(k in s for k in COMPOSED):
        return None
    t = _types(s)
    if "array" in t or (not t and isinstance(s.get("items"), dict)):
        return "array"
    if "object" in t or (not t and isinstance(s.get("properties"), dict)):
        return "object"
    return "|".join(sorted(t)) or None


def type_label(s, depth: int = 0) -> str:
    """How a reader would name this shape: `array<OwnerDto>`, `OwnerDto`, `integer(int64)`."""
    if not isinstance(s, dict):
        return "any"
    kind = shape_kind(s)
    if kind == "array":
        items = s.get("items")
        return f"array<{type_label(items, depth + 1) if depth < 8 else '…'}>"
    if kind == "object" or kind is None:
        return str(s.get("title") or kind or "any")
    fmt = s.get("format")
    return f"{kind}({fmt})" if fmt else kind


def ghost_shape(s, depth: int = 0) -> dict:
    """The old subtree, reduced to what a ghost row shows: a label, the description, and
    the properties it can be expanded into. An array expands straight into its items'
    properties -- the label already says `array<OwnerDto>`, and a lone `items` row to
    click through first is a click that tells the reader nothing."""
    if not isinstance(s, dict):
        return {"label": "any"}
    out = {"label": type_label(s)}
    if s.get("description"):
        out["desc"] = str(s["description"])[:240]
    if depth >= GHOST_DEPTH:
        return out
    node = s
    while shape_kind(node) == "array" and isinstance(node.get("items"), dict):
        node = node["items"]
    props = node.get("properties") if shape_kind(node) == "object" else None
    if isinstance(props, dict) and props:
        req = set(node.get("required") or [])
        out["props"] = [{"name": str(k), "required": k in req, **ghost_shape(v, depth + 1)}
                        for k, v in list(props.items())[:GHOST_MAX_PROPS]]
        if len(props) > GHOST_MAX_PROPS:
            out["more"] = len(props) - GHOST_MAX_PROPS
    return out


def diff_schema(old, new, steps: list, where: dict, out: list) -> None:
    """Every node of `old` that is not in `new`, located by the steps into `new`."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return
    if len(steps) > MAX_REVEAL_DEPTH:
        return
    ko, kn = shape_kind(old), shape_kind(new)
    if ko is None or kn is None:
        return
    if ko != kn:
        # Nothing below a changed shape is compared: an array of owners and a page of
        # owners have no field in common worth lining up. The old shape goes up whole.
        out.append({**where, "kind": "type", "steps": steps, "old": ghost_shape(old)})
        return
    if ko == "array":
        diff_schema(old.get("items"), new.get("items"), steps + [{"kind": "items"}],
                    where, out)
        return
    if ko != "object":
        return
    old_props, new_props = old.get("properties"), new.get("properties")
    if not isinstance(old_props, dict):
        return
    new_props = new_props if isinstance(new_props, dict) else {}
    required = set(old.get("required") or [])
    prev = None
    for name, sub in old_props.items():
        if name not in new_props:
            out.append({**where, "kind": "prop", "steps": steps, "name": str(name),
                        "after": prev, "required": name in required,
                        "old": ghost_shape(sub)})
        else:
            diff_schema(sub, new_props[name], steps + [{"kind": "prop", "name": name}],
                        where, out)
        prev = str(name)


def _responses(op: dict) -> dict:
    r = op.get("responses")
    # YAML reads a bare `200:` as an int; the page knows statuses as text.
    return {str(k): v for k, v in r.items()} if isinstance(r, dict) else {}


def _content(body) -> dict:
    c = body.get("content") if isinstance(body, dict) else None
    return c if isinstance(c, dict) else {}


def _diff_content(old_c: dict, new_c: dict, where: dict, out: list) -> None:
    """Removed media types, then the schema Swagger UI shows first against its old self."""
    for media, mobj in old_c.items():
        if media not in new_c:
            schema = mobj.get("schema") if isinstance(mobj, dict) else None
            out.append({**where, "kind": "media", "media": str(media),
                        "old": ghost_shape(schema) if isinstance(schema, dict) else None})
    if not new_c:
        return
    first = next(iter(new_c))
    old_m = old_c.get(first) or next(iter(old_c.values()), None)
    new_m = new_c[first]
    if isinstance(old_m, dict) and isinstance(new_m, dict):
        diff_schema(old_m.get("schema"), new_m.get("schema"), [], where, out)


def op_ghosts(old_op: dict, new_op: dict) -> list:
    """Everything `old_op` had that `new_op` lost, each tied to where it used to be."""
    out = []

    def pkey(p):
        return [str(p.get("name")), str(p.get("in"))]

    new_params = {tuple(pkey(p)) for p in new_op.get("parameters") or []
                  if isinstance(p, dict)}
    prev = None
    for p in old_op.get("parameters") or []:
        if not isinstance(p, dict):
            continue
        if tuple(pkey(p)) not in new_params:
            g = {"kind": "param", "name": str(p.get("name")), "in": str(p.get("in")),
                 "after": prev, "required": bool(p.get("required")),
                 "label": type_label(p.get("schema") or {})}
            if p.get("description"):
                g["desc"] = str(p["description"])[:240]
            out.append(g)
        prev = pkey(p)

    old_r, new_r = _responses(old_op), _responses(new_op)
    prev = None
    for status, body in old_r.items():
        if status not in new_r:
            first = next(iter(_content(body).values()), None)
            schema = first.get("schema") if isinstance(first, dict) else None
            out.append({"kind": "response", "status": status, "after": prev,
                        "desc": str((body or {}).get("description") or "")[:240],
                        "old": ghost_shape(schema) if isinstance(schema, dict) else None})
        else:
            _diff_content(_content(body), _content(new_r[status]),
                          {"in": "response", "status": status}, out)
        prev = status

    old_b, new_b = old_op.get("requestBody"), new_op.get("requestBody")
    if isinstance(old_b, dict):
        if not isinstance(new_b, dict):
            first = next(iter(_content(old_b).values()), None)
            schema = first.get("schema") if isinstance(first, dict) else None
            out.append({"kind": "body",
                        "old": ghost_shape(schema) if isinstance(schema, dict) else None})
        else:
            _diff_content(_content(old_b), _content(new_b), {"in": "request"}, out)
    return out


def ghost_path(g: dict) -> str | None:
    """A prop ghost as oasdiff would spell it: `items/pets/items/gone`."""
    if g.get("kind") != "prop":
        return None
    tokens = ["items" if s["kind"] == "items" else s["name"] for s in g["steps"]]
    return "/".join(tokens + [g["name"]])


def change_ghost(change: dict, ghosts: list) -> int | None:
    """The ghost a removal line is about, so it is not reported as unreachable."""
    if change_mark(change.get("id") or "") != "removed":
        return None
    side = "request" if "request" in (change.get("id") or "") else "response"
    names = set(BACKTICKED.findall(change.get("text") or ""))
    for i, g in enumerate(ghosts):
        if g.get("in") == side and ghost_path(g) in names:
            return i
    return None


def build_model(old_spec: dict, new_spec: dict, changes: list):
    """Merge the two specs into one renderable spec + a per-operation diff map."""
    old_ops = {(m, p): op for m, p, op in operations(old_spec)}
    new_ops = {(m, p): op for m, p, op in operations(new_spec)}

    added = set(new_ops) - set(old_ops)
    removed = set(old_ops) - set(new_ops)

    per_op, global_changes = {}, []
    for c in changes:
        key = (c.get("operation"), c.get("path"))
        if c.get("section") == "paths" and key[0] and key[1]:
            per_op.setdefault(key, []).append(c)
        else:
            global_changes.append(c)

    # The rendered spec is the new one, with removed operations grafted back in
    # so they still get a row — greyed out and struck through.
    merged = copy.deepcopy(new_spec)
    merged.setdefault("paths", {})
    for (m, p) in sorted(removed):
        merged["paths"].setdefault(p, {})[m.lower()] = copy.deepcopy(old_ops[(m, p)])
    merged = inline_refs(merged)

    # Resolved against the *rendered* spec, which is `merged` — the one Swagger UI draws.
    merged_ops = {(m, p): op for m, p, op in operations(merged)}
    # The old side, inlined the same way, so a removed subtree carries its real fields
    # rather than a `$ref` the page could not follow.
    old_inlined = {(m, p): op for m, p, op in operations(inline_refs(old_spec))}

    entries = {}
    for (m, p) in sorted(set(new_ops) | removed):
        ch = merge_pairs(per_op.get((m, p), []))
        if (m, p) in removed:
            state = "removed"
        elif (m, p) in added:
            state = "added"
        elif any(c["level"] >= ERROR for c in ch):
            state = "breaking"
        elif ch:
            state = "modified"
        else:
            state = "untouched"
        op_obj = merged_ops.get((m, p)) or {}
        # A removed operation is drawn whole, from the old spec, struck through; there is
        # nothing inside it to ghost.
        ghosts = (op_ghosts(old_inlined[(m, p)], op_obj)
                  if (m, p) in old_inlined and (m, p) not in removed else [])
        listed = [
            # `rephrase` only here, at the point the line is written out: change_target()
            # below still reads oasdiff's own wording to work out which backticked token
            # is the schema path.
            {"text": rephrase(c["text"]), "level": c["level"], "id": c["id"],
             "mark": change_mark(c["id"]), "target": change_target(op_obj, c),
             "ghost": change_ghost(c, ghosts)}
            for c in sorted(ch, key=lambda c: -c["level"])
            # an added endpoint's only "change" is that it exists — no need to say it
            if not (state == "added" and c["id"] == "endpoint-added")
        ]
        # The cap is spent on the most severe first (the list is already in that order),
        # because a reader who can only be shown twelve fields wants the breaking ones.
        budget, skipped = MAX_REVEAL_PER_OP, 0
        for c in listed:
            names_a_field = "propert" in c["id"]   # a change about a field, not a verb
            if c["target"] and budget:
                budget -= 1
            elif c["ghost"] is not None:
                c["target"] = None     # drawn as a ghost row where it used to be
            elif names_a_field:
                c["target"] = None
                skipped += 1
        entries[f"{m} {p}"] = {
            "state": state,
            "changes": listed,
            # Said out loud on the page. A tree that quietly opens 12 of 30 fields is the
            # same lie as one that opens none, only harder to notice.
            "deepSkipped": skipped,
            "ghosts": ghosts,
        }
    # Swagger UI groups operations by tag; a tag is "quiet" when the diff left
    # every one of its operations alone. Derived here rather than read back from
    # the DOM, because Swagger UI rebuilds a section when you collapse it — and
    # a rebuilt section has no operations left to inspect.
    tags = {}
    for m, p, op in operations(merged):
        state = entries.get(f"{m} {p}", {}).get("state", "untouched")
        for tag in (op.get("tags") or ["default"]):
            if tags.get(tag) != "touched":
                tags[tag] = "touched" if state != "untouched" else "quiet"

    return merged, entries, global_changes, tags


def change_total(entries: dict, global_changes: list) -> int:
    """Every change the page lists, endpoint-level and global: the one count.

    One row per line the reader sees, after `merge_pairs` folded each swapped media type
    back into a single line — oasdiff alone counts that swap twice. An added operation
    lists no line (its only entry is that it exists) and still counts as one change.
    `openapi-compat.py` folds with the same `merge_pairs` before it counts changes. The
    verdict band above counts *endpoints* since 3806a78 ("11 endpoints changed"), so it
    and the "expand N changes" toggle here are two numbers by design."""
    return (sum(len(e["changes"]) or (1 if e["state"] == "added" else 0)
                for e in entries.values()) + len(global_changes))


def render(model, entries, global_changes, tags, old_label, new_label) -> str:
    counts = {}
    for e in entries.values():
        counts[e["state"]] = counts.get(e["state"], 0) + 1

    payload = json.dumps(
        {
            "spec": model,
            "ops": entries,
            "global": [
                {"text": rephrase(c["text"]), "level": c["level"], "id": c["id"],
                 "section": c.get("section", "")}
                for c in sorted(global_changes, key=lambda c: -c["level"])
            ],
            "tags": tags,
            "counts": counts,
            "old": old_label,
            "new": new_label,
        },
        default=str,  # YAML happily parses `2026-01-31` into a date object
    ).replace("</", "<\\/")

    n_changes = change_total(entries, global_changes)
    # Victor, 7 Oct 2026 (Devoxx): "it should say expand 25 changes" — the number is the
    # verdict band's count of changes, so the toggle names them changes, not "impacted".
    label = (f"expand {n_changes} change{'' if n_changes == 1 else 's'}" if n_changes
             else "expand changes")
    return (TEMPLATE.replace("__EXPAND_LABEL__", label)
            .replace("__TIP_JS__", _tip_js())
            .replace("__PAYLOAD__", payload))


def _tip_js() -> str:
    """The review page's tooltip component, so the diff's tooltips are the page's own.
    The standalone copy of this script has no hrbuild next to it and goes without."""
    tip = Path(__file__).resolve().parent / "hrbuild" / "assets" / "tip.js"
    return tip.read_text(encoding="utf-8").replace("</", "<\\/") if tip.is_file() else ""


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAPI visual diff</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.29.1/swagger-ui.min.css">
<style>
  /* `:host` beside every `:root`: embedded in another page (the review's API tab), this
     stylesheet is moved into a shadow root on a host element, where `:root` matches
     nothing. The shadow boundary keeps these rules and the host page's apart. */
  :root, :host {
    --dv-breaking: #d7263d;
    --dv-modified: #d98218;
    --dv-added:    #2e9e5b;
    --dv-removed:  #8a8f98;
    /* palette shared with the Human Review report, so an embedded diff doesn't
       announce itself as a foreign document */
    --dv-bg:    #fbfbfd;
    --dv-fg:    #1c1c22;
    --dv-muted: #6b6b78;
    --dv-line:  #e2e2ea;
    --dv-card:  #ffffff;
    --dv-code:  rgba(0,0,0,.06);
    /* Two more, used by the JSON-Schema block far below: `dim` is what that renderer
       paints an `x-` extension in, `attr` the blue it paints a type attribute in. */
    --dv-dim:   #8a8a95;
    --dv-attr:  #5555aa;
    /* A collapsed controller's name, and every expand/collapse arrow. */
    --dv-collapsed: #6b6b78;
    /* The toolbar, and the INFO / WARN words on the note list. The toolbar was near-black
       in both schemes -- a dark band across a light page -- and the dark-green INFO read
       2.4:1 on a dark card (UX review, 7 Oct 2026). */
    --dv-bar-bg: #e9ebf1; --dv-bar-fg: #1c1c22; --dv-bar-chip: rgba(0,0,0,.06);
    --dv-bar-chip-hover: rgba(0,0,0,.12); --dv-lvl1: #1f7a45; --dv-lvl2: #9a5b06;
  }
  /* System theme by default; ?theme=dark|light pins it (embedded: `data-theme` on the
     host), which is what an embedding page uses when it wants us to match, not guess. */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]), :host(:not([data-theme="light"])) {
      --dv-breaking: #f0757f; --dv-modified: #e0a44a; --dv-added: #6fce93;
      --dv-removed: #9aa0aa;
      --dv-bg: #15151a; --dv-fg: #e8e8ef; --dv-muted: #9a9aa8;
      --dv-line: #2c2c36; --dv-card: #1d1d24; --dv-code: rgba(255,255,255,.10);
      --dv-dim: #82828e; --dv-attr: #97a9ee; --dv-collapsed: #8b8f99;
      --dv-bar-bg: #1b1b1f; --dv-bar-fg: #eaeaea; --dv-bar-chip: rgba(255,255,255,.08);
      --dv-bar-chip-hover: rgba(255,255,255,.16); --dv-lvl1: #4ade80; --dv-lvl2: #e0a44a;
    }
  }
  :root[data-theme="dark"], :host([data-theme="dark"]) {
    --dv-breaking: #f0757f; --dv-modified: #e0a44a; --dv-added: #6fce93;
    --dv-removed: #9aa0aa;
    --dv-bg: #15151a; --dv-fg: #e8e8ef; --dv-muted: #9a9aa8;
    --dv-line: #2c2c36; --dv-card: #1d1d24; --dv-code: rgba(255,255,255,.10);
    --dv-dim: #82828e; --dv-attr: #97a9ee; --dv-collapsed: #8b8f99;
    --dv-bar-bg: #1b1b1f; --dv-bar-fg: #eaeaea; --dv-bar-chip: rgba(255,255,255,.08);
    --dv-bar-chip-hover: rgba(255,255,255,.16); --dv-lvl1: #4ade80; --dv-lvl2: #e0a44a;
  }
  body { margin: 0; background: var(--dv-bg); color: var(--dv-fg);
         color-scheme: light dark; }
  /* Embedded, the host starts from the same blank slate a document of our own would:
     `all: initial` drops the font, size, line height and colour the host page would
     otherwise pass down across the shadow boundary (inherited properties do cross it;
     custom properties are not reset by `all`, so `--dv-sticky-top` still arrives). It
     scrolls with the page -- no scrollport of its own -- and `isolation` keeps our
     z-indexes (the toolbar over the roads) inside us, under the host page's own bars. */
  :host {
    all: initial; display: block; isolation: isolate;
    background: var(--dv-bg); color: var(--dv-fg); color-scheme: light dark;
  }
  /* The roads' origin: every road is measured from this box's top-left corner, so the
     same numbers hold standalone (the top of the body) and embedded (wherever the host
     page put us). */
  #dv-app { position: relative; }

  /* ---------- toolbar ---------- */
  .dv-bar {
    /* Pinned at the top of whatever scrolls us -- the window, standalone or embedded --
       `--dv-sticky-top` below the top edge: an embedding page with a pinned header of its
       own sets it to that header's height, so the bar stops under it, not behind it. The
       filters and the counts are wanted at the fortieth endpoint, not just the first.
       Slim (Victor, 5 Oct 2026): every pixel of it is taken from the diff under it. */
    position: sticky; top: var(--dv-sticky-top, 0px); z-index: 50;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 5px 20px;
    background: var(--dv-bar-bg); color: var(--dv-bar-fg);
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    box-shadow: 0 2px 10px rgba(0,0,0,.12);
  }
  .dv-bar h1 { font-size: 14px; margin: 0 8px 0 0; font-weight: 600; letter-spacing: .01em; }
  .dv-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 1px 10px; border-radius: 999px;
    background: var(--dv-bar-chip); cursor: pointer; user-select: none;
    border: 1px solid transparent;
  }
  .dv-chip:hover { background: var(--dv-bar-chip-hover); }
  /* Filtered out: faded to .6 (at .38 "42 untouched" read 2.97:1) and its dot hollow, so
     the state is said by the mark as well as by the strength. */
  .dv-chip.off { opacity: .6; }
  .dv-chip.off .dot { background: transparent; box-shadow: inset 0 0 0 1.5px currentColor; }
  .dv-chip .dot { width: 9px; height: 9px; border-radius: 50%; }
  .dv-chip b { font-variant-numeric: tabular-nums; }
  .dv-unit { opacity: .6; margin-right: 2px; }
  .dot.breaking { background: var(--dv-breaking); }
  .dot.modified { background: var(--dv-modified); }
  .dot.added    { background: var(--dv-added); }
  .dot.removed  { background: var(--dv-removed); }
  .dot.untouched{ background: #4a4a52; }
  .dv-spacer { flex: 1; }
  .dv-toggle {
    display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    padding: 1px 10px; border-radius: 6px; background: var(--dv-bar-chip);
  }
  .dv-toggle input { accent-color: #7aa2f7; margin: 0; }

  /* ---------- global (non-path) changes ---------- */
  .dv-global {
    margin: 16px 20px 0; padding: 12px 16px; border-radius: 8px;
    background: var(--dv-card); border: 1px solid var(--dv-line);
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .dv-global h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
                  color: var(--dv-muted); margin: 0 0 8px; }
  /* Swagger UI never arrived (both CDNs unreachable): the changed endpoints as a plain
     list in the same box, rather than a bar with nothing under it. */
  .dv-plain .dv-plain-why { color: var(--dv-muted); margin: 0 0 10px; }
  .dv-plain .dv-plain-op { margin: 10px 0 0; }
  .dv-plain .dv-plain-op > b { font: 600 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .dv-plain .dv-plain-op > .dot { display: inline-block; width: 9px; height: 9px;
                                  border-radius: 50%; margin-right: 6px; }

  /* ---------- per-operation annotations ---------- */
  /* --- change list = a box with a folder tab ("N CHANGES") grown from its top-right corner --- */
  .dv-note {
    --R: 7px; --T: 21px; --c: var(--dv-modified);
    --bd: color-mix(in srgb, var(--c) 80%, var(--dv-card));
    --tint: color-mix(in srgb, var(--c) 9%, transparent);
    position: relative; margin: 4px 12px 10px; padding: 4px 0 6px;
    border: 1px solid var(--bd); border-top: 0; border-radius: 0 0 var(--R) var(--R);
    background: var(--tint);
  }
  .dv-note.dv-s-breaking { --c: var(--dv-breaking); }
  .dv-note.dv-s-added { --c: var(--dv-added); }
  .dv-note.dv-s-removed { --c: var(--dv-removed); }
  .dv-note > .dv-rail {
    position: absolute; left: -1px; right: -1px; bottom: 100%; height: var(--T);
    display: flex; align-items: flex-end; pointer-events: none;
  }
  .dv-rail-line {
    flex: 1; height: var(--R); box-sizing: border-box; background: var(--tint);
    border: 1px solid var(--bd); border-right: 0; border-bottom: 0; border-radius: var(--R) 0 0 0;
  }
  /* the join: a concave arc up from the box's top edge, then a convex one into the tab */
  .dv-rail-fil {
    flex: none; width: calc(2 * var(--R)); height: var(--T);
    background:
      radial-gradient(circle at 0 0, transparent var(--R), var(--bd) var(--R), var(--bd) calc(var(--R) + 1px), var(--tint) calc(var(--R) + 1px)) 0 var(--R) / calc(var(--R) + 1px) calc(2 * var(--R)) no-repeat,
      radial-gradient(circle at 100% 100%, var(--tint) calc(var(--R) - 1px), var(--bd) calc(var(--R) - 1px), var(--bd) var(--R), transparent var(--R)) var(--R) 0 / var(--R) var(--R) no-repeat,
      linear-gradient(var(--tint), var(--tint)) calc(var(--R) + 1px) var(--R) / calc(var(--R) - 1px) calc(2 * var(--R)) no-repeat;
  }
  /* Three classes deep, or `.dv-badge.modified` further down wins on order alone and paints
     the tab solid orange under its pale text (Victor, 10 Oct 2026: barely readable). The
     tab is the box's own tint; the words carry the colour. */
  .dv-note > .dv-rail > .dv-badge {
    margin: 0; height: var(--T); min-width: 0; box-sizing: border-box; padding: 0 12px 4px 6px;
    display: flex; align-items: center; background: var(--tint); color: var(--c);
    border: 1px solid var(--bd); border-left: 0; border-bottom: 0; border-radius: 0 var(--R) 0 0;
    font-size: 10px; font-weight: 700; letter-spacing: .06em; white-space: nowrap;
  }
  .swagger-ui .opblock.is-open .opblock-summary { border-bottom: 0 !important; }
  .opblock:has(.dv-rail) .opblock-summary > .dv-badge { display: none; }
  .dv-change {
    display: flex; gap: 8px; align-items: baseline;
    padding: 4px 14px 4px 12px; font-size: 13px; line-height: 1.45; color: var(--dv-fg);
  }
  .dv-change code {
    background: var(--dv-code); padding: 1px 5px; border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  }
  .dv-change .lvl {
    flex: none; font-size: 10px; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; padding: 1px 6px; border-radius: 4px; margin-top: 1px;
  }
  .dv-change.l3 .lvl { background: rgba(215,38,61,.12); color: var(--dv-breaking); }
  .dv-change.l2 .lvl { background: rgba(217,130,24,.14); color: var(--dv-lvl2); }
  .dv-change.l1 .lvl { background: rgba(46,158,91,.12); color: var(--dv-lvl1); }

  .dv-badge {
    font-size: 10px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    padding: 2px 8px; border-radius: 4px; margin-left: 8px; color: #fff; flex: none;
    /* One width for "1 change" and "3 changes", so the chevron left of it holds still
       from one endpoint to the next (it moved 7px). */
    display: inline-block; min-width: 80px; text-align: center; box-sizing: border-box;
  }
  .dv-badge.breaking { background: var(--dv-breaking); }
  .dv-badge.modified { background: var(--dv-modified); }
  .dv-badge.added    { background: var(--dv-added); }
  .dv-badge.removed  { background: var(--dv-removed); }

  /* ---------- the trail down to a changed field ----------
     Opening the tree is only half of it: once six levels are showing, the reader still
     has to find which of the fields on screen is the new one — and a grey hairline on
     every ancestor turned out to be as quiet as Swagger UI's own indent guides, so the
     way down was there and nobody saw it. The road is now drawn for real: one glowing
     line per changed field (`.dv-roads`, an SVG laid over the page by the script), from
     the schema root down and in, step by step, to the field itself, with a spark running
     along it so the eye is pulled down to the end. The leaf breathes and its chip
     blinks. Levels reuse the note list's l1/l2/l3 vocabulary rather than inventing a
     third scale, and every colour comes from the theme vars so dark mode needs no second
     set of rules. */
  .l1 { --dv-lvl: var(--dv-added); }
  .l2 { --dv-lvl: var(--dv-modified); }
  .l3 { --dv-lvl: var(--dv-breaking); }
  /* An ancestor on the road says so by its name, in the colour of the worst change it
     leads to — the line alone would leave the reader matching x-positions to names. */
  .swagger-ui .dv-hit-path.l1, .swagger-ui .dv-hit-path.l2, .swagger-ui .dv-hit-path.l3 {
    --dv-road-name: var(--dv-lvl);
  }
  .swagger-ui .dv-hit-path > .json-schema-2020-12-head .json-schema-2020-12__title,
  .swagger-ui tr.property-row.dv-hit-path > td:first-child {
    color: var(--dv-road-name, var(--dv-fg)); font-weight: 700;
  }
  /* Under the sticky toolbar (z-index 50), over the schema boxes, and never in the way
     of a click: it is paint, not a control. */
  svg.dv-roads {
    position: absolute; left: 0; top: 0; pointer-events: none; z-index: 40;
    overflow: visible;
  }
  .dv-roads path { fill: none; stroke: var(--dv-lvl); stroke-linecap: round;
                   stroke-linejoin: round; }
  .dv-roads .dv-road-bed {
    stroke-width: 3; opacity: .75;
    filter: drop-shadow(0 0 3px var(--dv-lvl)) drop-shadow(0 0 7px var(--dv-lvl));
  }
  .dv-roads .dv-road-spark {
    stroke-width: 5; stroke: #fff;
    filter: drop-shadow(0 0 3px var(--dv-lvl)) drop-shadow(0 0 8px var(--dv-lvl))
            drop-shadow(0 0 14px var(--dv-lvl));
  }
  @keyframes dv-breathe {
    from { box-shadow: 0 0 0 1px color-mix(in srgb, var(--dv-lvl) 35%, transparent),
                       0 0 6px color-mix(in srgb, var(--dv-lvl) 20%, transparent);
           background-color: color-mix(in srgb, var(--dv-lvl) 10%, transparent); }
    to   { box-shadow: 0 0 0 1px color-mix(in srgb, var(--dv-lvl) 80%, transparent),
                       0 0 22px color-mix(in srgb, var(--dv-lvl) 55%, transparent);
           background-color: color-mix(in srgb, var(--dv-lvl) 24%, transparent); }
  }
  @keyframes dv-blink {
    0%, 55% { opacity: 1; box-shadow: 0 0 10px var(--dv-lvl); }
    75%     { opacity: .2; box-shadow: none; }
    100%    { opacity: 1; box-shadow: 0 0 10px var(--dv-lvl); }
  }
  /* The spine is drawn OUTSIDE the box, by a pseudo-element, rather than as an inset
     shadow: the leaf article starts exactly at its property name, so an inset spine lands
     on the first letter — "vetId" reads as "etId" with a green bar over the v. Nudging
     the box left instead only fights Swagger UI's own padding rules. */
  .swagger-ui article.dv-hit { position: relative; border-radius: 5px; }
  .swagger-ui article.dv-hit::before {
    content: ""; position: absolute; left: -7px; top: 1px; bottom: 1px; width: 3px;
    border-radius: 2px; background: currentColor;
  }
  /* Swagger UI paints optional property names in a muted grey that its own light theme
     was tuned for. On the one row we are sending the reader to, that is not good enough
     in either theme — least of all sitting on a tinted background. */
  .swagger-ui .dv-hit > .json-schema-2020-12-head .json-schema-2020-12__title,
  .swagger-ui .dv-hit > .json-schema-2020-12-head .json-schema-2020-12-keyword__name {
    color: var(--dv-fg); font-weight: 700;
  }
  /* The 3.0 renderer's leaf is a table row: no box to round off, and the field name is
     in the first cell rather than inside the schema box. */
  .swagger-ui tr.property-row.dv-hit > td:first-child {
    box-shadow: inset 3px 0 0 currentColor; padding-left: 9px;
    color: var(--dv-fg); font-weight: 700;
  }
  /* The leaf breathes — a halo that swells and settles in the level's colour — so it
     is found from anywhere on the screen, not only by someone already reading that row. */
  .swagger-ui .dv-hit.l1, .swagger-ui .dv-hit.l2, .swagger-ui .dv-hit.l3 {
    color: var(--dv-lvl);
    background-color: color-mix(in srgb, var(--dv-lvl) 14%, transparent);
    animation: dv-breathe 1.3s ease-in-out infinite alternate;
  }
  .swagger-ui article.dv-hit::before {
    width: 4px; box-shadow: 0 0 6px currentColor, 0 0 12px currentColor;
  }
  .swagger-ui .dv-hit > .json-schema-2020-12-head .json-schema-2020-12__title {
    text-shadow: 0 0 10px color-mix(in srgb, var(--dv-lvl) 70%, transparent);
  }
  .swagger-ui tr.dv-hit > td {
    background: color-mix(in srgb, var(--dv-lvl) 16%, transparent);
  }
  /* A solid chip that blinks: the one word on the page that must not be read past. */
  .dv-fieldmark {
    font-size: 10px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 4px; margin-left: 8px; align-self: center;
    background: var(--dv-lvl); color: var(--dv-bg);
    animation: dv-blink 1.1s ease-in-out infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .swagger-ui .dv-hit.l1, .swagger-ui .dv-hit.l2, .swagger-ui .dv-hit.l3,
    .dv-fieldmark { animation: none; }
    .dv-roads .dv-road-spark { display: none; }
  }
  /* Fields the auto-expand deliberately did not open. Saying nothing here would teach
     the reader that what is open is everything — the exact failure this feature fixes. */
  .dv-deepmore {
    padding: 2px 14px 6px 12px; font-size: 12px; font-style: italic;
    color: var(--dv-muted);
  }

  /* ---------- ghosts: what the new spec no longer has ----------
     Drawn where the removed thing used to stand, in the breaking red, struck through,
     with a solid DELETED chip in the same shape as the ADDED/CHANGED chip on a live
     field -- so "deleted" reads as one more value of the same marker, not as a second
     vocabulary. Every colour is a theme var; the chip's text is the page background, which
     is what keeps it legible on the pale dark-mode red as well as the deep light-mode one.
     A ghost's children are the old shape it can be opened into: struck through too, but
     in the quieter --dv-removed grey, so one deleted object does not paint a red wall. */
  .dv-ghost {
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: var(--dv-fg);
  }
  .dv-ghost.dv-ghost-top {
    margin: 3px 0; padding: 2px 8px 2px 6px; border-radius: 5px;
    border-left: 3px dashed var(--dv-breaking);
    background: color-mix(in srgb, var(--dv-breaking) 9%, transparent);
  }
  /* `.swagger-ui summary { display: list-item }` would win over a bare class and glue
     every chip of a head together ("petsarray<PetDto>"), hence the scope. */
  .dv-ghost-head, .swagger-ui .dv-ghost-head {
    display: flex; align-items: center; gap: 8px; min-height: 22px;
    list-style: none;
  }
  .dv-ghost-head::-webkit-details-marker { display: none; }
  /* The caret that says "this opens", drawn in the same spot for leaves (blank) so the
     names line up down the column. */
  .dv-ghost-head::before {
    content: ""; flex: none; width: 12px; text-align: center;
    font-size: 13px; line-height: 1; color: var(--dv-fg);
  }
  summary.dv-ghost-head { cursor: pointer; }
  summary.dv-ghost-head::before { content: "\25B8"; transition: transform .12s ease; }
  details.dv-ghost[open] > summary.dv-ghost-head::before { transform: rotate(90deg); }
  .dv-ghost-name {
    font-weight: 700; color: var(--dv-breaking);
    text-decoration: line-through; text-decoration-thickness: 1.5px;
  }
  .dv-ghost-req { color: var(--dv-breaking); margin-left: -6px; }
  .dv-ghost-type {
    font-weight: 600; color: var(--dv-removed); text-decoration: line-through;
  }
  .dv-ghost-mark {
    font-size: 10px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 4px; flex: none;
    background: var(--dv-breaking); color: var(--dv-bg);
  }
  .dv-ghost-note { font-size: 12px; font-style: italic; color: var(--dv-muted); }
  .dv-ghost-desc {
    margin-left: auto; padding-left: 16px; min-width: 0;
    font-size: 12px; color: var(--dv-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .dv-ghost-body {
    margin: 1px 0 3px 4px; padding-left: 12px;
    border-left: 1px dashed color-mix(in srgb, var(--dv-removed) 70%, transparent);
  }
  .dv-ghost-body .dv-ghost-name { color: var(--dv-removed); font-weight: 600; }
  .dv-ghost-body .dv-ghost-req { color: var(--dv-removed); }
  .dv-ghost-more { font-size: 12px; font-style: italic; color: var(--dv-muted);
                   padding-left: 18px; }
  .swagger-ui li.dv-ghost-li, .swagger-ui ul.dv-ghost-list { list-style: none; }
  .swagger-ui ul.dv-ghost-list { padding: 0; margin: 0; }
  .swagger-ui tr.dv-ghost-row > td { vertical-align: top; padding-top: 6px; }
  .swagger-ui tr.dv-ghost-row .dv-ghost-desc { margin-left: 0; padding-left: 0;
                                               white-space: normal; }

  /* ---------- the whole point: fade what nobody touched ---------- */
  .swagger-ui .opblock { transition: opacity .18s ease, filter .18s ease; }
  .swagger-ui .opblock.dv-untouched {
    opacity: .32; filter: saturate(.15);
  }
  .swagger-ui .opblock.dv-untouched:hover { opacity: .85; filter: saturate(.6); }
  .dv-hide-untouched .opblock.dv-untouched,
  .dv-hide-untouched .opblock-tag-section.dv-empty { display: none; }
  .swagger-ui .opblock-tag-section.dv-quiet > h3 { opacity: .4; }

  /* operations read as children of their controller, not as siblings of it -- by the
     indent alone: the 2px spine that used to run down the left of each list was one
     more line on a tab already full of coloured ones (Victor, 7 Oct 2026). */
  .swagger-ui .opblock-tag-section > div {
    margin-left: 6px;
    padding-left: 20px;
  }
  /* Victor could not tell a collapsed controller from an open one: both headers were the
     same full-strength text with the same dark arrow. A collapsed one now reads grey, and
     the arrows are grey in both states, so the name is what carries open vs. closed. */
  .swagger-ui .opblock-tag[data-is-open="false"],
  .swagger-ui .opblock-tag[data-is-open="false"] :is(a, span, small, p, div) {
    color: var(--dv-collapsed);
  }
  .swagger-ui .opblock-tag svg.arrow { fill: var(--dv-collapsed); }

  /* impacted operations get a coloured spine */
  .swagger-ui .opblock.dv-breaking { border-color: var(--dv-breaking);
      box-shadow: inset 4px 0 0 var(--dv-breaking), 0 0 0 1px rgba(215,38,61,.25); }
  .swagger-ui .opblock.dv-modified { box-shadow: inset 4px 0 0 var(--dv-modified); }
  .swagger-ui .opblock.dv-added    { box-shadow: inset 4px 0 0 var(--dv-added); }
  .swagger-ui .opblock.dv-removed  { box-shadow: inset 4px 0 0 var(--dv-removed);
      background: var(--dv-card); }
  .swagger-ui .opblock.dv-removed .opblock-summary-path,
  .swagger-ui .opblock.dv-removed .opblock-summary-path__deprecated { text-decoration: line-through; }
  .swagger-ui .opblock.dv-removed .opblock-summary-method { background: var(--dv-removed); }

  /* Swagger UI's own copy-to-clipboard button. This page is a diff, not a console:
     there is nothing here anyone wants on their clipboard, and the icon sits exactly
     where the eye goes for the change badge. */
  .swagger-ui .copy-to-clipboard, .swagger-ui button.copy-to-clipboard { display: none; }
  /* The spec's own masthead -- its title in 36px, its "this is the REST API documentation
     of..." paragraph -- says nothing a reviewer of a diff needs (Victor, 7 Oct 2026: "useless").
     The version alone survives, small, in the corner. */
  .swagger-ui .information-container .info {
    margin: 0; padding: 0; display: flex; justify-content: flex-end;
  }
  .swagger-ui .info hgroup.main { margin: 0; }
  .swagger-ui .info .title { font-size: 0 !important; margin: 0; line-height: 1; }
  .swagger-ui .info .title .version-stamp, .swagger-ui .info__description,
  .swagger-ui .info .info__contact, .swagger-ui .info .info__license,
  .swagger-ui .info .base-url { display: none !important; }
  .swagger-ui .info .title small { top: 0; margin: 0; background: transparent; padding: 0; }
  .swagger-ui .info .title small pre.version {
    font: 11px/1.2 ui-monospace, Menlo, monospace; color: var(--dv-muted);
    background: transparent; padding: 0;
  }
  .swagger-ui .info .title small pre.version::before { content: "v"; }
  /* Swagger UI's terms-of-service link: a contract detail for a consumer browsing the
     live docs, noise in a diff of two revisions. */
  .swagger-ui .info__tos { display: none; }
  .swagger-ui .scheme-container, .swagger-ui .topbar { display: none; }

  /* ---------- Swagger UI ships light-only; repaint its surfaces ----------
     Only colour is touched — every dimension, weight and radius is left to
     Swagger UI, so the page still reads as the screen everyone knows. */
  .swagger-ui, .swagger-ui .info .title, .swagger-ui .info li,
  .swagger-ui .info p, .swagger-ui .info table, .swagger-ui .opblock-tag,
  .swagger-ui .opblock .opblock-summary-path,
  .swagger-ui .opblock .opblock-summary-path__deprecated,
  .swagger-ui .opblock .opblock-summary-operation-id,
  .swagger-ui .opblock-description-wrapper p, .swagger-ui .opblock-title_normal p,
  .swagger-ui table thead tr td, .swagger-ui table thead tr th,
  .swagger-ui .parameter__name, .swagger-ui .parameter__type,
  .swagger-ui .parameter__in, .swagger-ui .response-col_status,
  .swagger-ui .response-col_description, .swagger-ui .responses-inner h4,
  .swagger-ui .responses-inner h5, .swagger-ui .model-title, .swagger-ui .model,
  .swagger-ui .tab li, .swagger-ui label, .swagger-ui .btn,
  /* The section headings inside an operation -- `Parameters`, `Responses`, and the
     `Try it out` label riding beside them. Swagger paints all three #3b4151, which on
     the dark card we repaint the header to is very nearly the card itself: the two words
     that say what you are looking at were the least readable thing on the tab. */
  .swagger-ui .opblock .opblock-section-header h4,
  .swagger-ui .opblock .opblock-section-header h4 span,
  .swagger-ui .opblock .opblock-section-header > label {
    color: var(--dv-fg);
  }
  .swagger-ui .opblock .opblock-summary-description,
  .swagger-ui .parameter__extension, .swagger-ui .prop-format {
    color: var(--dv-muted);
  }
  .swagger-ui .opblock { border-color: var(--dv-line); }
  .swagger-ui .opblock .opblock-section-header {
    background: var(--dv-card); border-color: var(--dv-line);
  }
  .swagger-ui .opblock-tag, .swagger-ui .opblock-tag:hover,
  .swagger-ui section.models, .swagger-ui section.models .model-container {
    border-color: var(--dv-line); background: transparent;
  }
  .swagger-ui .model-box, .swagger-ui .model-toggle:after { background: transparent; }
  .swagger-ui .responses-table .response-col_description__inner div.renderedMarkdown p,
  .swagger-ui .markdown p, .swagger-ui .markdown li { color: var(--dv-fg); }
  /* the caret/expander glyphs are SVGs painted with a hard-coded dark fill */
  .swagger-ui svg:not(:root) { fill: var(--dv-fg); }
  .swagger-ui .opblock-body pre.microlight,
  .swagger-ui .highlight-code > .microlight {
    background: var(--dv-code); color: var(--dv-fg);
  }
  /* The constraint pills next to a type — `[1, 255] characters`, `≥ 0`,
     `matches ^[0-9]+$`, `int32` — ship as saturated purple and amber chips, which
     in both themes shout louder than the field names they qualify. They are
     footnotes on a type, not findings, so paint them as footnotes. */
  .swagger-ui .json-schema-2020-12__constraint,
  .swagger-ui .json-schema-2020-12__constraint--string {
    background: var(--dv-code); color: var(--dv-muted);
  }
  /* ---------- Swagger UI's JSON-Schema 2020-12 renderer ----------
     The component that draws the response body -- the tree of `firstName`, `telephone`,
     `pets` this page exists to point into -- is painted with literal light-theme hex
     values throughout: #3b4151 for a property name, #6b6b6b for its type and
     description, and a `rgba(0,0,0,.05)` tint behind the whole box. In dark mode the
     tint darkens the card and the names go down with it, so the one column a reader
     scans is the one they cannot read. Remap the palette to the page's own vars, which
     leaves light mode where it was and gives dark mode the same contrast the rest of the
     report has. Nothing but colour is touched. */
  .swagger-ui .json-schema-2020-12 { background-color: var(--dv-code); }
  .swagger-ui .json-schema-2020-12--embedded,
  .swagger-ui .model-box .json-schema-2020-12 { background-color: transparent; }
  .swagger-ui .json-schema-2020-12__title,
  .swagger-ui .json-schema-2020-12-property .json-schema-2020-12__title,
  .swagger-ui .json-schema-2020-12__attribute,
  .swagger-ui .json-schema-2020-12-keyword__name--primary,
  .swagger-ui .json-schema-2020-12-keyword__value--primary,
  .swagger-ui .json-schema-2020-12-json-viewer__name--primary,
  .swagger-ui .json-schema-2020-12-json-viewer__value--primary,
  .swagger-ui .json-schema-2020-12-keyword--const .json-schema-2020-12-json-viewer__name,
  .swagger-ui .json-schema-2020-12-keyword--const .json-schema-2020-12-json-viewer__value,
  .swagger-ui .json-schema-2020-12-keyword--default .json-schema-2020-12-json-viewer__name,
  .swagger-ui .json-schema-2020-12-keyword--default .json-schema-2020-12-json-viewer__value,
  .swagger-ui .json-schema-2020-12-keyword--enum .json-schema-2020-12-json-viewer__name,
  .swagger-ui .json-schema-2020-12-keyword--enum .json-schema-2020-12-json-viewer__value,
  .swagger-ui .json-schema-2020-12-keyword--examples .json-schema-2020-12-json-viewer__name,
  .swagger-ui .json-schema-2020-12-keyword--examples .json-schema-2020-12-json-viewer__value {
    color: var(--dv-fg);
  }
  .swagger-ui .json-schema-2020-12-keyword--description,
  .swagger-ui .json-schema-2020-12-keyword__name--secondary,
  .swagger-ui .json-schema-2020-12-keyword__value,
  .swagger-ui .json-schema-2020-12-keyword__value--secondary,
  .swagger-ui .json-schema-2020-12-json-viewer__name--secondary,
  .swagger-ui .json-schema-2020-12-json-viewer__value,
  .swagger-ui .json-schema-2020-12-json-viewer__value--secondary,
  .swagger-ui .json-schema-2020-12-expand-deep-button,
  .swagger-ui .json-schema-2020-12__attribute--muted {
    color: var(--dv-muted);
  }
  .swagger-ui .json-schema-2020-12-keyword__name--extension,
  .swagger-ui .json-schema-2020-12-keyword__value--extension,
  .swagger-ui .json-schema-2020-12-json-viewer__name--extension,
  .swagger-ui .json-schema-2020-12-json-viewer__value--extension,
  .swagger-ui .json-schema-2020-12-json-viewer-extension-keyword .json-schema-2020-12-json-viewer__name,
  .swagger-ui .json-schema-2020-12-json-viewer-extension-keyword .json-schema-2020-12-json-viewer__value {
    color: var(--dv-dim);
  }
  .swagger-ui .json-schema-2020-12__attribute--primary { color: var(--dv-attr); }
  /* ---------- Swagger UI's form controls ----------
     The parameter inputs and the enum <select>s are painted white / light grey with a
     dark caret SVG, whatever the theme: on the dark card they were the only light boxes
     on the page. Colour only, from the theme vars; the caret is redrawn in --dv-muted. */
  .swagger-ui input[type=text], .swagger-ui input[type=password],
  .swagger-ui input[type=search], .swagger-ui input[type=email],
  .swagger-ui input[type=file], .swagger-ui textarea, .swagger-ui select {
    background-color: var(--dv-card); color: var(--dv-fg);
    border: 1px solid var(--dv-line); box-shadow: none;
  }
  .swagger-ui input::placeholder, .swagger-ui textarea::placeholder { color: var(--dv-dim); }
  .swagger-ui input[disabled], .swagger-ui textarea[disabled], .swagger-ui select[disabled] {
    background-color: var(--dv-code); color: var(--dv-muted); opacity: 1;
  }
  .swagger-ui select {
    background-image: linear-gradient(45deg, transparent 50%, var(--dv-muted) 50%),
                      linear-gradient(135deg, var(--dv-muted) 50%, transparent 50%);
    background-position: calc(100% - 15px) 55%, calc(100% - 10px) 55%;
    background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
  }
  /* The required-field asterisk is literal `red`, which on the dark card reads as a
     smudge rather than a mark. The page already owns a red that survives there. */
  .swagger-ui .json-schema-2020-12-property--required
      > .json-schema-2020-12:first-of-type
      > .json-schema-2020-12-head .json-schema-2020-12__title:after {
    color: var(--dv-breaking);
  }
  /* A field's description is Swagger UI's first row of its body, so every expanded node
     grew an extra line of prose between its name and its children and the tree lost its
     steady one-field-per-row pace. `layoutDescriptions()` lifts it onto the head row
     instead, into the empty room right of the type chips: one line, cut with an ellipsis,
     the whole text in a tooltip when it was cut. The script sets `left` (just past the
     head's last chip) and the row height; a head too wide to leave room keeps the old row.
     The schema box is an inline-block that shrink-wraps its widest row, which would cut
     every description at the end of the longest type chip, so it takes the full column. */
  .swagger-ui .model-box:has(> article.json-schema-2020-12) { display: block; }
  .swagger-ui article.json-schema-2020-12 { position: relative; }
  .swagger-ui .json-schema-2020-12-body > .json-schema-2020-12-keyword--description.dv-desc-inline {
    position: absolute; top: 0; right: 8px; margin: 0; padding: 0;
    left: var(--dv-desc-left); height: var(--dv-desc-h); line-height: var(--dv-desc-h);
    text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .swagger-ui .dv-desc-inline * { display: inline; margin: 0; padding: 0; }
  .swagger-ui .dv-desc-inline p + p::before { content: " "; }
  .swagger-ui .opblock.opblock-deprecated { opacity: .7; }
  .dv-count-hidden { font-size: 12px; opacity: .6; }

  /* ---------- an operation read as a diff, not as a console (Victor, 7 Oct 2026) ----------
     The controller header is a band, so the eye finds where one controller ends. */
  .swagger-ui .opblock-tag, .swagger-ui .opblock-tag:hover {
    background: color-mix(in srgb, var(--dv-attr) 15%, var(--dv-card)) !important;
    border: 0 !important; border-left: 4px solid var(--dv-attr) !important;
    border-radius: 6px; padding: 6px 12px !important; margin: 14px 0 8px !important;
    font-size: 18px;
  }
  /* A quiet controller: grey name and rim on no band at all. That is what Victor approved
     live on 7 Oct (the patch's tinted band never drew there: its variable was undefined). */
  .swagger-ui .opblock-tag-section.dv-quiet > h3.opblock-tag {
    background: transparent !important;
    border-left-color: currentColor !important;
  }
  /* Section headings are labels, not bars: `Parameters` and `Responses` in small caps. */
  .swagger-ui .opblock .opblock-section-header {
    background: transparent !important; box-shadow: none !important; border: 0 !important;
    padding: 8px 20px 2px !important; min-height: 0 !important;
  }
  .swagger-ui .opblock .opblock-section-header h4,
  .swagger-ui .opblock .opblock-section-header h4 span {
    font-size: 11px !important; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--dv-muted) !important;
  }
  .swagger-ui .opblock .tab-header .tab-item.active h4 span:after { display: none; }
  .swagger-ui .opblock .tab-header .tab-item { padding: 0; }
  /* Parameters: one line each -- name, type, where, default. The input boxes are try-it
     controls, and "No parameters" is a section that says it is empty. */
  .swagger-ui .try-out, .swagger-ui .execute-wrapper, .swagger-ui .btn.try-out__btn {
    display: none !important;
  }
  .swagger-ui .opblock-body .opblock-section:has(> .parameters-container > .opblock-description-wrapper) {
    display: none;
  }
  .swagger-ui .parameters-container .table-container { padding: 2px 20px 4px !important; }
  .swagger-ui .parameters-container table.parameters thead { display: none; }
  .swagger-ui table.parameters > tbody > tr > td {
    padding: 2px 0 !important; vertical-align: baseline;
  }
  .swagger-ui table.parameters .parameters-col_name {
    width: auto; min-width: 0; padding-right: 16px !important; white-space: nowrap;
  }
  .swagger-ui table.parameters .parameter__name { display: inline; font-size: 13px; }
  .swagger-ui table.parameters .parameter__type, .swagger-ui table.parameters .parameter__in,
  .swagger-ui table.parameters .parameter__deprecated {
    display: inline; padding: 0 0 0 6px; font-size: 11px; color: var(--dv-muted);
  }
  .swagger-ui table.parameters .parameters-col_description { width: 100%; }
  .swagger-ui table.parameters .parameters-col_description :is(input, select, textarea, .parameter__enum),
  .swagger-ui table.parameters .parameters-col_description .json-schema-form-item {
    display: none !important;
  }
  .swagger-ui table.parameters .parameters-col_description .renderedMarkdown p,
  .swagger-ui table.parameters .parameters-col_description p {
    margin: 0; font-size: 12px; color: var(--dv-muted);
  }
  .swagger-ui table.parameters .parameter__default { display: inline-block; margin: 0; font-size: 12px; }
  .swagger-ui table.parameters .parameter__default i { font-style: normal; }
  /* Responses: a list of status lines that fold (see foldResponses), the Schema/Example
     switch first, the media type under it, and none of the console chatter -- the green
     "Controls Accept header.", the Code/Description/Links header, "No links", the
     "Example Description" block. Swagger UI sizes this table with a 1px max-width cell;
     as a grid, the description column takes the room it is given. */
  .swagger-ui .responses-inner { padding: 2px 20px 8px !important; }
  .swagger-ui .responses-table thead, .swagger-ui .responses-table .response-col_links { display: none; }
  .swagger-ui table.responses-table, .swagger-ui table.responses-table > tbody {
    display: block; width: 100%;
  }
  .swagger-ui .responses-table tr.response {
    display: grid; grid-template-columns: 62px minmax(0, 1fr); align-items: start;
    border-top: 1px solid var(--dv-line);
  }
  .swagger-ui .responses-table tr.response:first-child { border-top: 0; }
  .swagger-ui .responses-table tr.response > td { padding: 4px 0 !important; border: 0; }
  .swagger-ui .responses-table td.response-col_status {
    white-space: nowrap; cursor: pointer; user-select: none; font-weight: 600;
    min-width: 0 !important; max-width: none !important;
  }
  .swagger-ui .responses-table td.response-col_status::before {
    content: "\25BE"; display: inline-block; width: 14px; color: inherit;
    transition: transform .12s ease;
  }
  .swagger-ui .responses-table tr.response.dv-folded td.response-col_status::before {
    transform: rotate(-90deg);
  }
  .swagger-ui .responses-table .response-col_description__inner { cursor: pointer; }
  .swagger-ui .responses-table .response-col_description__inner .renderedMarkdown p { margin: 0; }
  .swagger-ui .responses-table tr.response.dv-folded td.response-col_description > :not(.response-col_description__inner) {
    display: none !important;
  }
  .swagger-ui .responses-table tr.response.dv-folded .response-col_description__inner .renderedMarkdown p {
    color: var(--dv-muted) !important;
  }
  .swagger-ui .responses-table td.response-col_description {
    display: flex; flex-direction: column; gap: 4px;
    min-width: 0 !important; width: auto !important; max-width: none !important;
  }
  .swagger-ui .responses-table td.response-col_description > * { order: 9; min-width: 0; max-width: 100%; }
  .swagger-ui .responses-table td.response-col_description > .response-col_description__inner { order: 1; }
  .swagger-ui .responses-table td.response-col_description > .model-example { display: contents; }
  .swagger-ui .responses-table td.response-col_description > .model-example > ul.tab { order: 2; margin: 2px 0 0; }
  .swagger-ui .responses-table td.response-col_description > .response-controls { order: 3; padding: 0; margin: 0; }
  .swagger-ui .responses-table td.response-col_description > .model-example > [role=tabpanel] { order: 4; }
  .swagger-ui .responses-table td.response-col_description > .example { display: none !important; }
  .swagger-ui .response-control-media-type { display: flex; align-items: center; gap: 8px; margin: 0 !important; }
  /* --- API tab: status tints, one font size, carets at the left start (live-patch-api-status) --- */
  .swagger-ui .responses-table td.response-col_status,
  .swagger-ui .responses-table .response-col_description__inner .renderedMarkdown p {
    font-size: 13px !important; font-weight: 600 !important; line-height: 20px;
  }
  .swagger-ui .responses-table td.response-col_status { padding-left: 6px !important; border-radius: 6px 0 0 6px; }
  .swagger-ui .responses-table .response-col_description__inner { padding: 4px 8px !important; border-radius: 0 6px 6px 0; }
  .swagger-ui .responses-table tr.response[data-code^="2"] :is(td.response-col_status, .response-col_description__inner) { background: rgba(46,160,67,.14); }
  .swagger-ui .responses-table tr.response:is([data-code^="4"], [data-code^="5"]) :is(td.response-col_status, .response-col_description__inner) { background: rgba(248,81,73,.14); }
  .swagger-ui .responses-table tr.response > td.response-col_description { padding-left: 0 !important; }
  .swagger-ui .responses-table td.response-col_status::before,
  .swagger-ui .opblock-tag .expand-operation::before,
  .swagger-ui .opblock-control-arrow::before,
  .swagger-ui .json-schema-2020-12-accordion__icon::before {
    content: "\25BC"; font-size: 13px; line-height: 1; color: var(--dv-muted); transform: none; display: inline-block;
  }
  .swagger-ui .responses-table td.response-col_status::before { width: 18px; }
  .swagger-ui .responses-table tr.response.dv-folded td.response-col_status::before,
  .swagger-ui .opblock-tag[data-is-open="false"] .expand-operation::before,
  .swagger-ui .opblock-control-arrow[aria-expanded="false"]::before,
  .swagger-ui .json-schema-2020-12-accordion__icon--collapsed::before { content: "\25B6"; transform: none; }
  .swagger-ui .opblock-tag .expand-operation { order: -1; margin: 0 8px 0 0 !important; width: auto; }
  .swagger-ui .opblock-summary .opblock-control-arrow { order: -1; margin: 0 8px 0 4px !important; width: auto; }
  .swagger-ui :is(.expand-operation, .opblock-control-arrow) svg,
  .swagger-ui .json-schema-2020-12-accordion__icon svg { display: none; }
  .swagger-ui .json-schema-2020-12-accordion { display: inline-flex !important; align-items: center; }
  .swagger-ui .json-schema-2020-12-accordion__icon { order: -1; margin: 0 6px 0 0 !important; transform: none !important; width: auto !important; height: auto !important; display: inline-flex; font-size: 13px; }
  .swagger-ui table.parameters .parameter__default.dv-empty { display: none !important; }
  .swagger-ui .response-control-media-type__title { display: inline !important; font-size: 11px; color: var(--dv-muted); }
  .swagger-ui .response-control-media-type__accept-message { display: none !important; }
  .swagger-ui .response-controls select {
    font-size: 12px !important; padding: 2px 26px 2px 8px !important; min-width: 0 !important;
    border: 1px solid var(--dv-line) !important; box-shadow: none !important;
    background-color: transparent;
  }
  .swagger-ui .response-control-media-type--accept-controller select {
    outline: none !important; border-color: var(--dv-line) !important;
  }
  .swagger-ui td.response-col_description:has(ul.tab li:last-child.active) .response-control-examples { display: none; }
  .swagger-ui .response-control-examples { display: flex; align-items: center; gap: 8px; }
  .swagger-ui .response-control-examples__title { font-size: 11px; color: var(--dv-muted); }
</style>
</head>
<body>
<div id="dv-app">
<div class="dv-bar">
  <h1>OpenAPI visual diff</h1>
  <!-- The pair of refs used to sit here, between the title and the chips. Embedded in the
       review -- which is how this page is read -- the masthead two inches above already
       says which branch is against which base, on every tab, and saying it again here
       only cost the chips their room. Standalone it is not lost: it is the tab title. -->
  <span id="dv-chips"></span>
  <span class="dv-spacer"></span>
  <!-- No tooltip here on purpose: the checkbox demonstrates itself the moment it is
       ticked. The count is the verdict line's count: that line
       says "25 changes" right above this diff, and a toggle that opened 11 endpoints
       with no number on it read as a second, contradicting tally. -->
  <label class="dv-toggle"><input type="checkbox" id="dv-expand"> __EXPAND_LABEL__</label>
</div>
<div id="dv-global"></div>
<div id="swagger-ui"></div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.29.1/swagger-ui-bundle.min.js"></script>
<script>
// One function scope for all of it: embedded, this script runs in the host page's global
// scope, where a top-level `const root` or `function apply` would clash with the page's own.
(() => {
// Standalone, this page is the document. Embedded (the review's API tab), the markup and
// the styles above were moved into a shadow root on a host element, and this script tag
// carries `data-dv-host` naming that host. Everything below queries `root`, never
// `document`: it finds its own nodes either way, never the host page's, and no stylesheet
// crosses the boundary in either direction. The view the URL hash picks (`#only-touched`)
// is the host's `data-hash` when embedded, since the page's own hash names its tab.
const HOST = document.currentScript && document.currentScript.dataset.dvHost
  ? document.getElementById(document.currentScript.dataset.dvHost) : null;
const root = HOST ? HOST.shadowRoot : document;
const APP = root.getElementById('dv-app');
const HASH = HOST ? (HOST.dataset.hash || '') : location.hash;

// The report's one tooltip component (hrbuild/assets/tip.js, driven by data-tip), inlined:
// a native title= is the thing the house rule exists to keep out. It is written against
// `document`; embedded, it is handed a stand-in that puts its stylesheet and its bubble
// in our root and listens there -- the host page's own copy sees our elements only as the
// host, retargeted, and a second copy listening on the page would double every page tip.
(function (document) {
__TIP_JS__
})(HOST ? { createElement: tag => document.createElement(tag), head: root, body: root,
            addEventListener: (...a) => root.addEventListener(...a) } : document);

// ?theme=dark|light pins the theme (embedded: the host's `data-theme`, which the
// stylesheet reads off `:host` directly); with neither the system decides.
const THEME = new URLSearchParams(location.search).get('theme');
if (!HOST && (THEME === 'dark' || THEME === 'light')) {
  document.documentElement.setAttribute('data-theme', THEME);
}

const DATA = __PAYLOAD__;
const LABELS = { breaking: 'breaking', modified: 'modified', added: 'added',
                 removed: 'removed', untouched: 'untouched' };
const ORDER = ['breaking', 'modified', 'added', 'removed', 'untouched'];

// The one place the two refs are still named: the browser tab, where it costs no room
// and answers "which of these did I leave open?".
if (!HOST) document.title = DATA.new + ' vs ' + DATA.old + ' \u2014 OpenAPI visual diff';

// backticked oasdiff prose -> <code>
function md(s) {
  const esc = s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  return esc.replace(/`([^`]+)`/g, '<code>$1</code>');
}

// ---- toolbar chips double as filters ----
const hidden = new Set();
const chips = root.getElementById('dv-chips');
// The chips count endpoints, one per operation; the verdict line under the tab counts
// the individual changes inside them. Without the unit "4 breaking" up here and
// "14 breaking" down there read as two answers to the same question.
if (ORDER.some(state => DATA.counts[state])) {
  chips.insertAdjacentHTML('beforeend', '<span class="dv-unit">endpoints:</span>');
}
ORDER.forEach(state => {
  const n = DATA.counts[state] || 0;
  if (!n) return;
  const el = document.createElement('span');
  el.className = 'dv-chip';
  el.innerHTML = `<span class="dot ${state}"></span><b>${n}</b> ${LABELS[state]}`;
  el.onclick = () => {
    hidden.has(state) ? hidden.delete(state) : hidden.add(state);
    el.classList.toggle('off', hidden.has(state));
    if (state === 'untouched') {
      APP.classList.toggle('dv-hide-untouched', hidden.has(state));
    }
    apply();
  };
  chips.appendChild(el);
});

// ---- non-path changes (components, servers, security...) ----
if (DATA.global.length) {
  const box = root.getElementById('dv-global');
  box.className = 'dv-global';
  box.innerHTML = '<h2>Outside the endpoints</h2>' + DATA.global.map(c =>
    `<div class="dv-change l${c.level}"><span class="lvl">${c.level === 3 ? 'breaking' : c.level === 2 ? 'warn' : 'info'}</span><span>${md(c.text)}</span></div>`
  ).join('');
}

function keyOf(op) {
  const m = op.querySelector('.opblock-summary-method');
  const p = op.querySelector('.opblock-summary-path');
  if (!m || !p) return null;
  const path = p.getAttribute('data-path') || p.textContent.trim();
  return m.textContent.trim().toUpperCase() + ' ' + path;
}

// Lift each field's description onto its head row, right of the type chips (see the CSS).
// Measured, not guessed: the head's last chip ends wherever its name, flags and
// constraints happen to end, and the field-change chip lands there too. Re-run with
// decorate(), so a node opened by hand or by the walk is laid out once it renders.
function layoutDescriptions() {
  root.querySelectorAll(
    '.swagger-ui .json-schema-2020-12-body > .json-schema-2020-12-keyword--description'
  ).forEach(d => {
    const art = d.parentElement.parentElement;
    const head = art.querySelector(':scope > .json-schema-2020-12-head');
    const last = head?.lastElementChild;
    if (!last) return;
    const a = art.getBoundingClientRect();
    const left = Math.ceil(last.getBoundingClientRect().right - a.left + 24);
    const fits = a.width - 8 - left >= 80;
    d.classList.toggle('dv-desc-inline', fits);
    if (!fits) { d.removeAttribute('data-tip'); return; }
    const h = Math.round(head.getBoundingClientRect().height);
    if (d.style.getPropertyValue('--dv-desc-left') !== left + 'px') {
      d.style.setProperty('--dv-desc-left', left + 'px');
    }
    if (d.style.getPropertyValue('--dv-desc-h') !== h + 'px') {
      d.style.setProperty('--dv-desc-h', h + 'px');
    }
    const cut = d.scrollWidth > d.clientWidth + 1;
    const text = d.textContent.trim();
    if (!cut) d.removeAttribute('data-tip');
    else if (d.getAttribute('data-tip') !== text) d.setAttribute('data-tip', text);
  });
}

// Swagger UI re-renders on expand/collapse, so decorating is idempotent and re-run.
function decorate() {
  root.querySelectorAll('.swagger-ui .opblock').forEach(op => {
    const key = keyOf(op);
    const info = key && DATA.ops[key];
    if (!info) return;
    op.dataset.dvState = info.state;
    ORDER.forEach(s => op.classList.toggle('dv-' + s, s === info.state));

    const summary = op.querySelector('.opblock-summary');
    if (summary && info.state !== 'untouched' && !summary.querySelector('.dv-badge')) {
      const b = document.createElement('span');
      b.className = 'dv-badge ' + info.state;
      const n = info.changes.length;
      const many = `${n} change${n > 1 ? 's' : ''}`;
      // A removed operation says DELETED, the word every ghost row inside the other
      // operations uses for the same fact. A breaking one keeps the count beside the
      // word: "BREAKING" alone dropped the "3 CHANGES" every modified operation shows.
      const nb = info.changes.filter(c => c.level >= 3).length;
      b.textContent = info.state === 'modified' && n ? many
        : info.state === 'breaking' && n ? `${nb} breaking · ${many}`
        : info.state === 'removed' ? 'deleted' : info.state;
      summary.appendChild(b);
    }
    if (summary && info.changes.length && !op.querySelector('.dv-note')) {
      const note = document.createElement('div');
      note.className = 'dv-note';
      const n = info.deepSkipped || 0;
      note.innerHTML = info.changes.map(c =>
        `<div class="dv-change l${c.level}"><span class="lvl">${c.level === 3 ? 'breaking' : c.level === 2 ? 'warn' : 'info'}</span><span>${md(c.text)}</span></div>`
      ).join('') + (n ? `<div class="dv-deepmore">${n} more changed field${n > 1 ? 's are' : ' is'} not opened automatically — too deep, or gone from this revision. Use the schema's own “Expand all”.</div>` : '');
      summary.insertAdjacentElement('afterend', note);
    }
    dressNote(op);
  });
  apply();
  autoCollapse();
  foldResponses();
  hideEmptyDefaults();
  markVisible();
  layoutDescriptions();
}

// Swagger prints "Default value :" even when the parameter has none.
function hideEmptyDefaults() {
  root.querySelectorAll('.parameter__default').forEach(d =>
    d.classList.toggle('dv-empty', /^Default value\s*:?$/.test(d.textContent.trim())));
}

// The "N CHANGES" badge becomes the tab of the box around the change lines (see .dv-rail).
function dressNote(op) {
  const note = op.querySelector('.dv-note');
  const badge = op.querySelector('.opblock-summary .dv-badge, .dv-note .dv-badge');
  if (!note || !badge || note.querySelector('.dv-rail')) return;
  note.classList.add('dv-s-' + (badge.classList.contains('breaking') ? 'breaking' : 'modified'));
  const rail = document.createElement('div');
  rail.className = 'dv-rail';
  rail.innerHTML = '<span class="dv-rail-line"></span><span class="dv-rail-fil"></span>';
  rail.appendChild(badge);
  note.prepend(rail);
}

// ---- a response is a line that folds ----
// Every operation lists 200, 400, 404, 500 and each one used to unfold a whole body; the
// error bodies are the same ProblemDetail on every endpoint ("usually boring", Victor,
// 7 Oct 2026). A 2xx opens; any other status stays folded to its one line -- unless this
// diff touched it, in which case it is the news and opens like a 2xx. Once the reader
// folds or unfolds a line by hand, that choice sticks across Swagger UI's re-renders.
const foldedByHand = new Map();
function responseKey(tr) {
  return keyOf(tr.closest('.opblock')) + '|' + tr.dataset.code;
}
function responseChanged(op, tr) {
  const code = tr.dataset.code;
  const info = DATA.ops[keyOf(op)];
  const hit = t => t && t.in === 'response' && String(t.status) === code;
  if (info && (info.changes.some(c => hit(c.target)) || (info.ghosts || []).some(hit))) {
    return true;
  }
  // A change with no field to walk to (a status added, a media type swapped) still names
  // its status in the prose line; and whatever got marked on the tree is a change too.
  if (tr.querySelector('.dv-hit, .dv-fieldmark, .dv-ghost')) return true;
  return [...op.querySelectorAll('.dv-note .dv-change code')]
    .some(el => el.textContent.trim() === code);
}
function foldResponses() {
  root.querySelectorAll('.swagger-ui .opblock .responses-table tr.response[data-code]')
    .forEach(tr => {
      const k = responseKey(tr);
      const open = foldedByHand.has(k) ? !foldedByHand.get(k)
        : /^2/.test(tr.dataset.code) || responseChanged(tr.closest('.opblock'), tr);
      if (tr.classList.contains('dv-folded') === open) tr.classList.toggle('dv-folded', !open);
    });
}
function setFolded(tr, folded) {
  foldedByHand.set(responseKey(tr), folded);
  tr.classList.toggle('dv-folded', folded);
}
root.addEventListener('click', e => {
  const cell = e.target.closest && e.target.closest(
    '.responses-table tr.response > td.response-col_status, '
    + '.responses-table tr.response .response-col_description__inner');
  const tr = cell && cell.closest('tr.response[data-code]');
  if (tr) setFolded(tr, !tr.classList.contains('dv-folded'));
});

// A controller nobody touched is folded away on arrival — once, so that
// re-expanding one by hand sticks.
let collapsedOnce = false;
function autoCollapse() {
  if (collapsedOnce) return;
  const sections = [...root.querySelectorAll('.opblock-tag-section')];
  if (!sections.length || !sections.some(s => s.querySelector('.opblock'))) return;
  collapsedOnce = true;
  sections.forEach(sec => {
    const h3 = sec.querySelector('.opblock-tag');
    if (h3 && DATA.tags[h3.dataset.tag] === 'quiet' && h3.dataset.isOpen === 'true') {
      h3.click();
    }
  });
}

function apply() {
  root.querySelectorAll('.swagger-ui .opblock').forEach(op => {
    const s = op.dataset.dvState;
    op.style.display = s && hidden.has(s) ? 'none' : '';
  });
  // a tag section nobody touched fades as a whole; empty ones disappear
  root.querySelectorAll('.opblock-tag-section').forEach(sec => {
    const ops = [...sec.querySelectorAll('.opblock')];
    const visible = ops.filter(o => o.style.display !== 'none');
    const tag = sec.querySelector('.opblock-tag')?.dataset.tag;
    const quiet = DATA.tags[tag] === 'quiet';
    sec.classList.toggle('dv-quiet', quiet);
    // A collapsed section holds no opblocks, so the "nothing visible left" rule below
    // can never reach it — and the untouched sections are exactly the collapsed ones.
    // Their verdict comes from the tag map instead.
    if (quiet && hidden.has('untouched')) { sec.style.display = 'none'; return; }
    // a tag whose every operation is filtered out has nothing left to say
    sec.style.display = ops.length > 0 && visible.length === 0 ? 'none' : '';
  });
}

// `#only-touched` opens on the filtered view — the same state the untouched chip
// toggles, so there is one control for it rather than two that must agree.
function setOnlyTouched(on) {
  APP.classList.toggle('dv-hide-untouched', on);
  if (on) hidden.add('untouched'); else hidden.delete('untouched');
  root.querySelectorAll('.dv-chip').forEach(c => {
    if (c.textContent.includes('untouched')) c.classList.toggle('off', on);
  });
  apply();
}
if (HASH.includes('only')) setOnlyTouched(true);

// ---- walking down to the fields that changed ----
// Swagger UI renders a collapsed subtree as an empty <div>: the children do not exist in
// the DOM until their parent is open. So every step waits for the node it asked for
// rather than assuming it is there, and ancestors are opened strictly in order.
const STEP_WAIT = 1500;

function waitFor(get, ms) {
  return new Promise(done => {
    const t0 = performance.now();
    (function tick() {
      const got = get();
      if (got) return done(got);
      if (performance.now() - t0 > ms) return done(null);
      requestAnimationFrame(tick);
    })();
  });
}

// Swagger UI draws a schema with one of TWO renderers and the difference is invisible
// from the outside: a 3.1 spec (or any `jsonSchemaDialect`) gets the JSON-Schema-2020-12
// tree of <article>s, a 3.0 spec gets the older <span class="model"> boxes. Same page,
// same version of Swagger UI, entirely different DOM. Walking only one of them would
// leave every 3.0 spec silently un-expanded — the exact "nothing happened, so there must
// be nothing there" this feature exists to kill. So each renderer gets a strategy, and
// the root node decides which one is in play.
const SCHEMA_2020 = {
  root: host => host.querySelector('.model-container article.json-schema-2020-12'),
  head: a => a.querySelector(':scope > .json-schema-2020-12-head'),
  collapsed: a => !!a.querySelector(
    ':scope > .json-schema-2020-12-head .json-schema-2020-12-accordion__icon--collapsed'),
  toggle: a => a.querySelector(
    ':scope > .json-schema-2020-12-head > .json-schema-2020-12-accordion'),
  child(a, step) {
    const body = a.querySelector(':scope > .json-schema-2020-12-body');
    if (!body) return null;
    // Told apart from grandchildren by which body they climb back to: the wrapper markup
    // between a body and its articles is Swagger UI's business and has moved before.
    const mine = sel => [...body.querySelectorAll(sel)]
      .filter(el => el.parentElement.closest('.json-schema-2020-12-body') === body);
    if (step.kind === 'items') {
      return mine('.json-schema-2020-12-keyword--items > article')[0] || null;
    }
    return mine('.json-schema-2020-12-property > article').find(el =>
      this.head(el)?.querySelector('.json-schema-2020-12__title')?.textContent.trim()
        === step.name) || null;
  },
  // The 2020-12 leaf carries its own name, so the article is the thing to light up.
  mark: a => a,
};

const SCHEMA_LEGACY = {
  root: host => host.querySelector('.model-box > span.model'),
  toggle: n => n.querySelector(':scope > span > button.model-box-control')
            || n.querySelector(':scope > button.model-box-control'),
  collapsed(n) {
    const b = this.toggle(n);
    return !!b && b.getAttribute('aria-expanded') !== 'true';
  },
  child(n, step) {
    // Same "whose child am I" test, with span.model as the boundary. `table.model` is
    // deliberately not a boundary — it is the property table, not a schema box.
    const inside = (el, sel) => {
      for (let e = el.parentElement; e && e !== n; e = e.parentElement) {
        if (e.matches(sel)) return true;
      }
      return false;
    };
    const mine = sel => [...n.querySelectorAll(sel)]
      .filter(el => el.parentElement.closest('span.model') === n);
    if (step.kind === 'items') {
      // An array renders as `[ <model> ]` with the item unnamed; the property table, if
      // any, belongs to an object and is not it.
      return mine('span.model').find(el => !inside(el, '.inner-object')) || null;
    }
    const row = mine('tr.property-row')
      .find(tr => tr.children[0]?.textContent.trim() === step.name);
    return row ? row.querySelector(':scope > td:nth-child(2) span.model') : null;
  },
  // A legacy property's NAME lives in the row's first cell, not in the model box, so
  // lighting up the box alone would highlight the type and not the field.
  mark: n => n.closest('tr.property-row') || n,
};

async function openNode(kit, n) {
  if (!kit.collapsed(n)) return true;
  kit.toggle(n)?.click();
  return !!(await waitFor(() => !kit.collapsed(n), STEP_WAIT));
}

function schemaHost(op, t) {
  if (t.in === 'request') return op.querySelector('.opblock-section-request-body');
  return [...op.querySelectorAll('.responses-table .response')].find(r =>
    r.querySelector('.response-col_status')?.textContent.trim() === t.status) || null;
}

// Booted with `defaultModelRendering: 'model'` a body opens on "Schema" already; this
// stays for one the reader switched to "Example Value" by hand. A spec
// with no example has no tab strip at all and shows the model directly, so a missing
// button is not a failure.
async function openSchemaTree(host) {
  const btn = [...host.querySelectorAll('.tab li button')]
    .find(b => b.textContent.trim() === 'Schema');
  if (btn && btn.getAttribute('aria-selected') !== 'true') btn.click();
  return await waitFor(() => {
    for (const kit of [SCHEMA_2020, SCHEMA_LEGACY]) {
      const root = kit.root(host);
      if (root) return { kit, root };
    }
    return null;
  }, STEP_WAIT);
}

function markLeaf(kit, node, c) {
  const target = kit.mark(node);
  target.classList.add('dv-hit', 'l' + c.level);
  const head = kit.head ? kit.head(node) : null;
  const slot = head || target.querySelector(':scope > td:last-child') || target;
  if (!slot.querySelector('.dv-fieldmark')) {
    const chip = document.createElement('span');
    chip.className = 'dv-fieldmark l' + c.level;
    chip.textContent = c.mark;
    slot.appendChild(chip);
  }
}

// true when the reader can now see the field, false when we ran out of tree. The caller
// counts the falses and puts the number on the page: a walk that quietly gives up is
// indistinguishable from a field that was never there.
async function revealTarget(op, c, run) {
  const got = await openTo(op, c.target, c.target.steps, run, false);
  if (!got) return false;
  got.chain.forEach(el => markPath(el, c.level));
  markLeaf(got.kit, got.node, c);
  addRoad(got.chain, got.kit.mark(got.node), c.level);
  return true;
}

// Open the tree down to the node `steps` names -- and that node too when `openLast`, which
// is what a removed property needs: its ghost row lives inside its parent's body.
// Booted on "Schema" (`defaultModelRendering: 'model'`), an `array<object>` response body's
// `Items` node ignores its own toggle: it stays collapsed however often it is clicked, and
// a walk through it gave up ("could not be opened") on 9 of test-pr's 25 changes. The same
// tree drawn again by a tab switch -- Example Value, then Schema -- opens normally, which
// is the path the walk always took before the page opened on Schema. So once per walk.
async function redrawSchema(host) {
  const tab = name => [...host.querySelectorAll('.tab li button')]
    .find(b => b.textContent.trim() === name);
  const example = tab('Example Value');
  if (!example) return false;
  example.click();
  await waitFor(() => example.getAttribute('aria-selected') === 'true', STEP_WAIT);
  tab('Schema')?.click();
  return !!(await openSchemaTree(host));
}

async function openTo(op, where, steps, run, openLast, redrawn) {
  // An opblock gets its <div class="opblock-body"> before it has a responses table, so
  // asking for the response row the instant the operation opens finds nothing. Wait for
  // the row itself, not for the box it will eventually appear in.
  const host = await waitFor(() => schemaHost(op, where), STEP_WAIT);
  if (!host) return null;
  // A response line folded by hand is opened again: the reader asked to see the change.
  if (host.matches('tr.response.dv-folded')) setFolded(host, false);
  const found = await openSchemaTree(host);
  if (!found) return null;
  const { kit } = found;
  let cur = found.root;
  const chain = [];
  const again = async () => !redrawn && run === revealRun && await redrawSchema(host)
    ? openTo(op, where, steps, run, openLast, true) : null;
  for (const step of steps) {
    if (run !== revealRun) return null;         // the reader changed their mind
    if (!await openNode(kit, cur)) return again();
    const next = await waitFor(() => kit.child(cur, step), STEP_WAIT);
    if (!next) return null;
    chain.push(kit.mark(cur));
    cur = next;
  }
  if (openLast && !await openNode(kit, cur)) return again();
  return { kit, node: cur, chain };
}

// ---- markers are the tree's business, not the checkbox's ----
// The checkbox decides only what gets opened FOR the reader. Whatever is open -- by the
// checkbox or by hand -- shows its markers: a field the reader expanded themselves and
// found unmarked would read as "this one did not change", which is a lie. So after every
// Swagger UI render the tree is walked as it stands, opening nothing, and every changed
// node that is on screen is lit up (markLeaf/markPath are idempotent), and every ghost
// whose place is on screen is drawn there.
function walkVisible(op, where, steps) {
  const host = schemaHost(op, where);
  if (!host) return null;
  for (const kit of [SCHEMA_2020, SCHEMA_LEGACY]) {
    const root = kit.root(host);
    if (!root) continue;
    let cur = root;
    const chain = [];
    for (const step of steps) {
      if (kit.collapsed(cur)) return null;
      const next = kit.child(cur, step);
      if (!next) return null;
      chain.push(kit.mark(cur));
      cur = next;
    }
    return { kit, node: cur, chain };
  }
  return null;
}

function markVisible() {
  pruneRoads();
  root.querySelectorAll('.swagger-ui .opblock.is-open').forEach(op => {
    const info = DATA.ops[keyOf(op)];
    if (!info) return;
    for (const c of info.changes) {
      if (!c.target) continue;
      const got = walkVisible(op, c.target, c.target.steps);
      if (!got) continue;
      got.chain.forEach(el => markPath(el, c.level));
      markLeaf(got.kit, got.node, c);
      addRoad(got.chain, got.kit.mark(got.node), c.level);
    }
    (info.ghosts || []).forEach((g, i) => placeGhost(op, g, i));
  });
}

// ---- ghosts: what the new spec no longer has, drawn where it used to be ----
// Swagger UI renders the new spec only, so a removed field is not grey or struck out --
// it is simply not there, and nothing on the tree says anything ever was. The generator
// diffed the two specs and sent each removed subtree with the steps to its old place;
// here it becomes a struck-through, DELETED row at that place. Our own markup, never
// Swagger UI's classes, so neither walker can mistake a ghost for a live node.
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

function ghostTree(name, shape, opts = {}) {
  const kids = shape && shape.props && shape.props.length;
  const el = document.createElement(kids ? 'details' : 'div');
  el.className = 'dv-ghost' + (opts.top ? ' dv-ghost-top' : '');
  const head = document.createElement(kids ? 'summary' : 'div');
  head.className = 'dv-ghost-head';
  head.innerHTML =
    (name != null ? `<span class="dv-ghost-name">${esc(name)}</span>` : '')
    + (opts.required ? '<span class="dv-ghost-req">*</span>' : '')
    + (shape ? `<span class="dv-ghost-type">${esc(shape.label)}</span>` : '')
    + (opts.top ? '<span class="dv-ghost-mark">deleted</span>' : '')
    + (opts.note ? `<span class="dv-ghost-note">${esc(opts.note)}</span>` : '')
    + (shape && shape.desc ? `<span class="dv-ghost-desc">${esc(shape.desc)}</span>` : '');
  el.appendChild(head);
  if (kids) {
    const body = document.createElement('div');
    body.className = 'dv-ghost-body';
    shape.props.forEach(p =>
      body.appendChild(ghostTree(p.name, p, { required: p.required })));
    if (shape.more) {
      body.insertAdjacentHTML('beforeend',
        `<div class="dv-ghost-more">…and ${shape.more} more</div>`);
    }
    el.appendChild(body);
  }
  return el;
}

// Insert `el` right after the sibling that preceded it in the old spec -- a live node or
// an earlier ghost -- or first when it led the list.
function placeAfter(parent, el, anchor) {
  if (anchor) anchor.insertAdjacentElement('afterend', el);
  else parent.insertBefore(el, parent.firstChild);
}

function ghostRow(cells) {
  const tr = document.createElement('tr');
  tr.className = 'dv-ghost-row';
  cells.forEach(([cls, node]) => {
    const td = document.createElement('td');
    if (cls) td.className = cls;
    if (node) td.appendChild(node);
    tr.appendChild(td);
  });
  return tr;
}

function placeGhost(op, g, i) {
  if (op.querySelector(`[data-dv-ghost="${i}"]`)) return;
  const tag = el => { el.dataset.dvGhost = i; return el; };
  if (g.kind === 'prop') {
    const got = walkVisible(op, g, g.steps);
    if (!got || got.kit.collapsed(got.node)) return;
    if (got.kit === SCHEMA_2020) {
      const ghost = ghostTree(g.name, g.old, { top: true, required: g.required });
      const body = got.node.querySelector(':scope > .json-schema-2020-12-body');
      if (!body) return;
      let ul = [...body.querySelectorAll('.json-schema-2020-12-keyword--properties > ul')]
        .find(u => u.closest('.json-schema-2020-12-body') === body);
      if (!ul) {                                  // every property went: start a list
        ul = body.querySelector(':scope > ul.dv-ghost-list');
        if (!ul) {
          ul = document.createElement('ul');
          ul.className = 'dv-ghost-list';
          body.appendChild(ul);
        }
      }
      const li = tag(document.createElement('li'));
      li.className = 'dv-ghost-li';
      li.dataset.dvGhostName = g.name;
      li.appendChild(ghost);
      const prev = g.after && ([...ul.children].find(x => x.dataset.dvGhostName === g.after)
        || SCHEMA_2020.child(got.node, { kind: 'prop', name: g.after })
             ?.closest('li.json-schema-2020-12-property'));
      placeAfter(ul, li, prev && prev.parentElement === ul ? prev : null);
    } else {
      const rows = [...got.node.querySelectorAll('tr.property-row')]
        .filter(tr => tr.closest('span.model') === got.node);
      const tbody = rows[0]?.parentElement
        || got.node.querySelector('table.model > tbody');
      if (!tbody) return;
      // The legacy table names a field in its first cell; the ghost keeps the rest.
      const rest = ghostTree(null, g.old, { top: true });
      const tr = tag(ghostRow([[null, null], [null, rest]]));
      tr.dataset.dvGhostName = g.name;
      tr.firstChild.innerHTML = `<span class="dv-ghost-name">${esc(g.name)}</span>`;
      const prev = g.after && ([...tbody.children].find(x => x.dataset.dvGhostName === g.after)
        || rows.find(r => r.children[0]?.textContent.trim() === g.after));
      placeAfter(tbody, tr, prev || null);
    }
    return;
  }
  if (g.kind === 'type') {
    const got = walkVisible(op, g, g.steps);
    if (!got) return;
    const name = g.steps.length ? g.steps[g.steps.length - 1].name : null;
    const ghost = ghostTree(name || null, g.old,
                            { top: true, note: 'the shape it had before' });
    const at = got.kit.mark(got.node);
    if (at.matches('tr')) {
      at.insertAdjacentElement('afterend', tag(ghostRow([[null, null], [null, ghost]])));
    } else {
      at.insertAdjacentElement('afterend', tag(ghost));
    }
    return;
  }
  if (g.kind === 'param') {
    let tbody = op.querySelector('table.parameters > tbody');
    if (!tbody) {                                 // no parameters left at all
      const box = op.querySelector('.parameters-container');
      if (!box) return;
      tbody = box.querySelector('table.dv-ghost-table > tbody');
      if (!tbody) {
        box.insertAdjacentHTML('beforeend',
          '<table class="parameters dv-ghost-table"><tbody></tbody></table>');
        tbody = box.querySelector('table.dv-ghost-table > tbody');
      }
    }
    const name = ghostTree(g.name, { label: g.label }, { top: true, required: g.required,
                                                       note: '(' + g.in + ')' });
    const desc = document.createElement('div');
    desc.className = 'dv-ghost-desc';
    desc.textContent = g.desc || '';
    const tr = tag(ghostRow([['parameters-col_name', name],
                             ['parameters-col_description', desc]]));
    tr.dataset.dvGhostName = g.name + '|' + g.in;
    const prev = g.after && [...tbody.children].find(r =>
      r.dataset.dvGhostName === g.after.join('|')
      || (r.dataset.paramName === g.after[0] && r.dataset.paramIn === g.after[1]));
    placeAfter(tbody, tr, prev || null);
    return;
  }
  if (g.kind === 'response') {
    const tbody = op.querySelector('.responses-table > tbody');
    if (!tbody) return;
    const status = ghostTree(g.status, null, { top: true });
    const what = ghostTree(g.desc || '', g.old, {});
    const tr = tag(ghostRow([['response-col_status', status],
                             ['response-col_description', what], [null, null]]));
    tr.dataset.dvGhostName = g.status;
    const prev = g.after && [...tbody.children].find(r =>
      r.dataset.dvGhostName === g.after || r.dataset.code === g.after);
    placeAfter(tbody, tr, prev || null);
    return;
  }
  if (g.kind === 'media') {
    const host = schemaHost(op, g);
    const cell = g.in === 'request' ? host
      : host?.querySelector(':scope > .response-col_description');
    if (!cell) return;
    cell.appendChild(tag(ghostTree(g.media, g.old, { top: true, note: 'media type' })));
    return;
  }
  if (g.kind === 'body') {
    const section = op.querySelector('.opblock-body .opblock-section');
    if (!section) return;
    section.insertAdjacentElement('afterend',
      tag(ghostTree('Request body', g.old, { top: true })));
  }
}

// An ancestor shared by several changed fields wears the colour of the worst of them.
function markPath(el, level) {
  el.classList.add('dv-hit-path');
  const had = [1, 2, 3].find(l => el.classList.contains('l' + l)) || 0;
  if (level > had) {
    el.classList.remove('l' + had);
    el.classList.add('l' + level);
  }
}

// ---- the road: one glowing line per changed field, root to leaf ----
// Drawn as an SVG over the page rather than as borders on the boxes, because the road
// is not the boxes' edges: it runs down an ancestor only as far as the child it leads
// into, then steps in to that child, and a box's border cannot stop half-way down.
// Positions are read off the live layout, so anything that moves the tree — another
// operation opening, the window narrowing — re-draws it on the next frame.
const ROADS = [];
const SVG_NS = 'http://www.w3.org/2000/svg';
let roadLayer = null, roadFrame = 0;

function headOf(el) {
  return el.querySelector(':scope > .json-schema-2020-12-head')
      || el.querySelector(':scope > td:first-child')
      || el.querySelector(':scope > span > button.model-box-control')
      || el;
}

// Where the road turns at this node: just left of its name — a 2020-12 box starts at
// its first letter, so its own edge would put the road through the "p" of "pets" — and
// level with the middle of the name. The leaf's spine sits exactly there, 7px out.
// Measured from #dv-app's corner, the box the layer is positioned in: standalone that is
// the top of the page, embedded it is wherever the host page put us -- and the page
// scrolling moves both rects alike, so no scroll offset enters the sum.
function roadPoint(el, origin) {
  const box = el.getBoundingClientRect(), head = headOf(el).getBoundingClientRect();
  if (!box.height) return null;                 // filtered out, collapsed, detached
  return {
    x: box.left - origin.left - 5.5,
    y: head.top - origin.top + Math.min(head.height / 2, 14),
  };
}

function roadPath(road, origin) {
  if (!road.leaf.isConnected || road.chain.some(el => !el.isConnected)) return null;
  const pts = [...road.chain, road.leaf].map(el => roadPoint(el, origin));
  if (pts.some(p => !p)) return null;
  // Down the ancestor's rail to the level of the next name, then in to it.
  let d = `M${pts[0].x} ${pts[0].y}`;
  for (const p of pts.slice(1)) d += ` V${p.y} H${p.x}`;
  return d;
}

function drawRoads() {
  cancelAnimationFrame(roadFrame);
  roadFrame = requestAnimationFrame(() => {
    if (!roadLayer) {
      roadLayer = document.createElementNS(SVG_NS, 'svg');
      roadLayer.setAttribute('class', 'dv-roads');
      roadLayer.setAttribute('aria-hidden', 'true');
      APP.appendChild(roadLayer);
    }
    // Sized to #dv-app with the layer itself taken out of the measurement. Sized to the
    // document's `scrollWidth`/`scrollHeight` as it stood, it was a ratchet: a vertical
    // scrollbar narrowed the page by 15px under a layer still as wide as before, which drew
    // a horizontal scrollbar, and a collapse left the page as tall as it had ever been.
    // `clientWidth` is the box's width inside any scrollbar; the roads never pass it.
    roadLayer.setAttribute('width', 0);
    roadLayer.setAttribute('height', 0);
    roadLayer.setAttribute('width', APP.clientWidth);
    roadLayer.setAttribute('height', APP.scrollHeight);
    const origin = APP.getBoundingClientRect();
    for (const road of ROADS) {
      const d = roadPath(road, origin);
      if (!road.g) {
        road.g = document.createElementNS(SVG_NS, 'g');
        road.g.setAttribute('class', 'l' + road.level);
        for (const cls of ['dv-road-bed', 'dv-road-spark']) {
          const path = document.createElementNS(SVG_NS, 'path');
          path.setAttribute('class', cls);
          road.g.appendChild(path);
        }
        roadLayer.appendChild(road.g);
      }
      road.g.style.display = d ? '' : 'none';
      if (!d || d === road.d) continue;
      road.d = d;
      const [bed, spark] = road.g.children;
      bed.setAttribute('d', d);
      spark.setAttribute('d', d);
      // A short bright dash with a gap longer than the whole road is a spark: sliding
      // its offset from "not yet started" to "past the end" runs it down the road and
      // into the field. Speed is per pixel, so a deep field is not reached in a blur.
      const len = spark.getTotalLength(), dash = 26;
      spark.style.strokeDasharray = `${dash} ${len + dash}`;
      road.spark?.cancel();
      road.spark = spark.animate(
        [{ strokeDashoffset: dash }, { strokeDashoffset: -len }],
        { duration: 900 + len * 2.2, iterations: Infinity,
          easing: 'cubic-bezier(.45,0,.2,1)' });
    }
  });
}

function clearRoads() {
  for (const road of ROADS) { road.spark?.cancel(); road.g?.remove(); }
  ROADS.length = 0;
}

// One road per leaf element, whoever found it first -- the reveal walk or the pass over
// what is already open -- and none for a leaf Swagger UI has since thrown away.
function addRoad(chain, leaf, level) {
  if (ROADS.some(r => r.leaf === leaf)) return;
  ROADS.push({ chain, leaf, level });
  drawRoads();
}

function pruneRoads() {
  for (let i = ROADS.length - 1; i >= 0; i--) {
    const road = ROADS[i];
    if (road.leaf.isConnected) continue;
    road.spark?.cancel();
    road.g?.remove();
    ROADS.splice(i, 1);
  }
}

// Our own box, not the window: embedded, a tab switch takes us from no size to full size
// with no window resize at all, and the descriptions laid out while hidden measured zero.
new ResizeObserver(() => { drawRoads(); layoutDescriptions(); }).observe(APP);
new MutationObserver(() => { if (ROADS.length) drawRoads(); })
  .observe(root.getElementById('swagger-ui'), { childList: true, subtree: true });

// One run at a time. A second click supersedes the first rather than racing it.
let revealRun = 0;

async function revealImpacted() {
  const run = ++revealRun;
  const ops = [...root.querySelectorAll('.swagger-ui .opblock')].filter(op => {
    const s = op.dataset.dvState;
    return s && s !== 'untouched' && op.style.display !== 'none';
  });
  for (const op of ops) {
    if (run !== revealRun) return;
    const info = DATA.ops[keyOf(op)];
    if (!info) continue;
    if (!op.classList.contains('is-open')) {
      op.querySelector('.opblock-summary-control')?.click();
      await waitFor(() => op.querySelector('.opblock-body'), STEP_WAIT);
    }
    let missed = 0;
    for (const c of info.changes) {
      if (run !== revealRun) return;
      if (c.target && !await revealTarget(op, c, run)) missed++;
    }
    // Open the way to every ghost inside a schema too: a removed field is a change like
    // any other, and its row lives in its old parent's body.
    for (const g of info.ghosts || []) {
      if (run !== revealRun) return;
      if (g.steps) await openTo(op, g, g.steps, run, g.kind === 'prop');
    }
    reportMissed(op, missed);
    markVisible();
  }
}

// A walk that ran out of tree — an unfamiliar renderer, a node that never rendered —
// has to say so where the reader is looking. Silence here reads as "there was nothing
// deeper", which is the belief this whole feature exists to correct.
function reportMissed(op, missed) {
  op.querySelector('.dv-missed')?.remove();
  if (!missed) return;
  const note = op.querySelector('.dv-note');
  if (!note) return;
  const line = document.createElement('div');
  line.className = 'dv-deepmore dv-missed';
  line.textContent = `${missed} changed field${missed > 1 ? 's' : ''} could not be opened `
    + 'in this schema view — open it by hand with the schema’s own “Expand all”.';
  note.appendChild(line);
}

// "expand impacted" now means what the reader always assumed it meant: not "open the
// operations that changed" but "show me what changed". Stopping at the operation left
// the fields four levels down inside a collapsed schema, which is where this started.
// A folded controller renders no operations at all, so with it folded the toggle used to
// open nothing (Victor, 5 Oct 2026). Every tag holding a changed operation opens first —
// read off the tag map, since a folded section has no operations to inspect — and
// unticking folds back the ones the toggle opened, and only those.
const openedTags = new Set();
function tagHead(tag) {
  return root.querySelector(`.opblock-tag[data-tag="${CSS.escape(tag)}"]`);
}
root.getElementById('dv-expand').onchange = async e => {
  const run = ++revealRun;                       // cancel anything still walking
  const on = e.target.checked;
  clearRoads();
  if (on) {
    root.querySelectorAll('.opblock-tag').forEach(h3 => {
      const tag = h3.dataset.tag;
      if (DATA.tags[tag] !== 'touched' || h3.dataset.isOpen === 'true') return;
      openedTags.add(tag);
      h3.click();
    });
    // Until each opened section has rendered its operations and decorate() has marked
    // them: the loop below picks the changed ones out by that mark.
    await waitFor(() => [...openedTags].every(tag =>
      tagHead(tag)?.closest('.opblock-tag-section')?.querySelector('.opblock[data-dv-state]')),
      STEP_WAIT);
    if (run !== revealRun) return;
  }
  root.querySelectorAll('.swagger-ui .opblock').forEach(op => {
    const s = op.dataset.dvState;
    if (!s || s === 'untouched') return;
    const open = op.classList.contains('is-open');
    if (open !== on) op.querySelector('.opblock-summary-control')?.click();
  });
  if (on) { revealImpacted(); return; }
  openedTags.forEach(tag => {
    const h3 = tagHead(tag);
    if (h3 && h3.dataset.isOpen === 'true') h3.click();
  });
  openedTags.clear();
};

// `domNode`, not `dom_id`: Swagger UI looks a `dom_id` up on the document, and embedded
// our node is in a shadow root the document's lookups never enter.
//
// Swagger UI itself comes off cdnjs, and when that one request fails nothing else on the
// page says so: the bar above renders, the space under it stays empty, and the reader is
// left asking where the rows went (Victor, 9 Oct 2026 — "I see there are no rows"). So a
// missing bundle is fetched once more from a second CDN, and if that fails too the
// changed endpoints are listed plainly from DATA, with the reason on top.
const FALLBACK = 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.1/swagger-ui-bundle.js';
const FALLBACK_CSS = 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.1/swagger-ui.css';
let booted = false, retried = false;
function boot() {
  if (booted) return;
  if (typeof SwaggerUIBundle !== 'function') { retryBundle(); return; }
  booted = true;
  SwaggerUIBundle({
    spec: DATA.spec,   // already fully dereferenced by the generator
    domNode: root.getElementById('swagger-ui'),
    docExpansion: 'list',
    defaultModelsExpandDepth: -1,
    // A diff is read for the shape of the payload, not for a made-up sample of it: every
    // body opens on Schema (Victor, 7 Oct 2026). "Example Value" is one click away.
    defaultModelRendering: 'model',
    tryItOutEnabled: false,
    supportedSubmitMethods: [],
    deepLinking: false,
    onComplete: decorate,
  });
}
function retryBundle() {
  if (retried) { plainList(); return; }
  retried = true;
  // The stylesheet came off the same CDN; a sheet that never loaded has no `.sheet`.
  const link = root.querySelector('link[href*="swagger-ui"]');
  if (!link || !link.sheet) {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = FALLBACK_CSS;
    (HOST ? root : document.head).appendChild(css);
  }
  const s = document.createElement('script');
  s.src = FALLBACK;
  const timer = setTimeout(plainList, 15000);
  s.onload = () => { clearTimeout(timer); boot(); };
  s.onerror = () => { clearTimeout(timer); plainList(); };
  document.head.appendChild(s);
}
function plainList() {
  if (booted) return;
  booted = true;
  root.getElementById('dv-expand').closest('label').style.display = 'none';
  const ops = Object.entries(DATA.ops).filter(([, e]) => e.state !== 'untouched')
    .sort((a, b) => ORDER.indexOf(a[1].state) - ORDER.indexOf(b[1].state));
  const box = root.getElementById('swagger-ui');
  box.className = 'dv-global dv-plain';
  box.innerHTML = '<h2>Changed endpoints</h2>'
    + '<p class="dv-plain-why">Swagger UI could not be loaded (cdnjs and jsdelivr both '
    + 'unreachable — offline?), so this is the plain list. Reload to try again.</p>'
    + ops.map(([key, e]) => `<div class="dv-plain-op"><span class="dot ${e.state}"></span>`
      + `<b>${md(key)}</b> · ${LABELS[e.state]}`
      + e.changes.map(c =>
        `<div class="dv-change l${c.level}"><span class="lvl">${c.level === 3 ? 'breaking' : c.level === 2 ? 'warn' : 'info'}</span><span>${md(c.text)}</span></div>`
      ).join('') + '</div>').join('');
}
// Standalone the bundle's tag above blocks, so it is already here. An embedding page may
// load it `defer` instead -- a blocking download in the middle of its body would hold up
// everything after us -- and then it has run by DOMContentLoaded.
if (typeof SwaggerUIBundle === 'function') boot();
else document.addEventListener('DOMContentLoaded', boot, { once: true });

let decorateTimer = 0;
new MutationObserver(() => {
  clearTimeout(decorateTimer);
  decorateTimer = setTimeout(decorate, 50);
}).observe(root.getElementById('swagger-ui'), { childList: true, subtree: true });

// Neither framed nor sized by anyone: it used to be an iframe, first grown to its content
// by posted heights, then one window tall and scrolling itself (5 Oct 2026). Embedded in
// a shadow root it unfolds at its full height and the window is the one scrollbar.
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", nargs="?", help="base spec (omit when --base is given)")
    ap.add_argument("new", nargs="?", help="revision (omit when --base is given)")
    ap.add_argument("-o", "--out", default="openapi-diff.html")
    ap.add_argument("--base", metavar="REF",
                    help="git revision to diff the working tree against, e.g. a "
                         "merge-base — the pipeline entry point")
    ap.add_argument("--spec", default="openapi.yaml",
                    help="spec path inside the repo, used with --base")
    ap.add_argument("--label-old", help="label for the base spec (default: filename)")
    ap.add_argument("--label-new", help="label for the revision (default: filename)")
    args = ap.parse_args()

    tmp = None
    if args.base:
        # A spec that did not exist at the base is an empty one, not a crash: a branch
        # that introduces the API should render as one big "added", not as an error.
        tmp = tempfile.TemporaryDirectory()
        old = Path(tmp.name) / "base.yaml"
        blob = subprocess.run(["git", "show", f"{args.base}:{args.spec}"],
                              capture_output=True, text=True)
        old.write_text(blob.stdout if blob.returncode == 0
                       else "openapi: 3.0.0\ninfo: {title: '', version: ''}\npaths: {}\n")
        new = Path(args.spec)
        if not new.is_file():
            sys.exit(f"no spec at {new}")
        short = args.base[:8] if re.fullmatch(r"[0-9a-f]{40}", args.base) else args.base
        args.label_old = args.label_old or short
        args.label_new = args.label_new or "working tree"
    elif args.old and args.new:
        old, new = Path(args.old), Path(args.new)
    else:
        ap.error("give two spec files, or --base REF")
    changes = run_oasdiff(old, new)
    merged, entries, global_changes, tags = build_model(
        load_spec(old), load_spec(new), changes)
    out = Path(args.out)
    out.write_text(render(merged, entries, global_changes, tags,
                          args.label_old or old.name, args.label_new or new.name))

    counts = {}
    for e in entries.values():
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    summary = "  ".join(f"{counts.get(s, 0)} {s}" for s in
                        ("breaking", "modified", "added", "removed", "untouched"))
    print(f"{out}  ({summary})")


if __name__ == "__main__":
    main()
