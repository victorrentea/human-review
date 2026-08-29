"""The effective payload of one operation, before and after, drawn as one tree.

`$ref` indirection is the reason a YAML diff of a spec is unreadable. A property
that moved in `components.schemas.VisitDto` appears exactly once, in a place no
reviewer is looking, and never at the four operations that actually serve it. So
this resolves the refs and diffs the **effective shape** — the only shape a
client ever sees — request in one column, response in the other.

The convention is the one the PlantUML deltas already use, so a reviewer learns
it once: **grey is the contract as it was**, red is what this branch added, red
struck through is what it removed. A changed leaf shows both, old struck.

Example values are the spec's own (`example:`), never invented — a made-up
payload in a review is a liability, and the generated spec already carries the
real ones.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

MAX_DEPTH = 7
SUCCESS = tuple(str(c) for c in range(200, 300))
# Constraints worth showing on a leaf, in the order a reader scans them.
FACETS = ("format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
          "minLength", "maxLength", "minItems", "maxItems", "pattern", "default")


@dataclass
class Row:
    depth: int
    key: str
    status: str  # same | added | removed | changed
    type_html: str = ""
    facets_html: str = ""
    doc: str = ""
    example: str = ""
    required: str = ""  # "" | yes | added | removed
    flags: list = field(default_factory=list)


def esc(v) -> str:
    return html.escape(str(v))


# -- resolution -------------------------------------------------------------------
def ref_name(node):
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def deref(node, spec):
    """Follow `$ref` to the schema it names. A ref that loops back stops where it is."""
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        name = ref_name(node)
        if not name or name in seen:
            return {}
        seen.add(name)
        node = ((spec.get("components") or {}).get("schemas") or {}).get(name) or {}
    return node if isinstance(node, dict) else {}


def kind_of(node: dict) -> str:
    if node.get("type"):
        return node["type"]
    if node.get("properties"):
        return "object"
    return ""


# -- the shapes one operation exposes ---------------------------------------------
def json_schema(content: dict):
    """The schema of the body, preferring JSON where the generator offered a choice."""
    if not isinstance(content, dict):
        return None
    for media in ("application/json", "*/*"):
        if media in content:
            return (content[media] or {}).get("schema")
    for value in content.values():
        if isinstance(value, dict) and value.get("schema"):
            return value["schema"]
    return None


def operation(spec: dict, method: str, path: str) -> dict:
    return ((spec.get("paths") or {}).get(path) or {}).get(method.lower()) or {}


def request_schema(spec: dict, method: str, path: str):
    body = operation(spec, method, path).get("requestBody") or {}
    return json_schema(body.get("content") or {})


def response_schema(spec: dict, method: str, path: str):
    """The primary success response - the one a caller writes their happy path against."""
    responses = operation(spec, method, path).get("responses") or {}
    codes = sorted(str(k) for k in responses if str(k) in SUCCESS)
    for code in codes:
        schema = json_schema((responses[code] or {}).get("content") or {})
        if schema is not None:
            return schema, code
    return (None, codes[0]) if codes else (None, "")


def parameters(spec: dict, method: str, path: str) -> dict:
    """Path/query/header parameters, keyed the way a caller names them."""
    out = {}
    for p in operation(spec, method, path).get("parameters") or []:
        if isinstance(p, dict) and p.get("name"):
            out[f"{p['name']} ({p.get('in', '?')})"] = (p.get("schema") or {},
                                                        bool(p.get("required")))
    return out


# -- diffing two resolved trees ---------------------------------------------------
def merged_keys(before: list, after: list) -> list:
    """After's order, with before-only keys kept where they used to sit."""
    out, i = [], 0
    for key in after:
        if key in before:
            while i < len(before) and before[i] != key:
                if before[i] not in after:
                    out.append(before[i])
                i += 1
            i += 1
        out.append(key)
    while i < len(before):
        if before[i] not in after:
            out.append(before[i])
        i += 1
    return out


def type_phrase(node, spec: dict, depth: int = 0) -> str:
    """`VisitDto[]`, `string(date)`, `integer` - what a generator would call it."""
    if depth > 4 or not isinstance(node, dict):
        return "..." if depth > 4 else ""
    name = ref_name(node)
    if name:
        return name
    kind = kind_of(node)
    if kind == "array":
        inner = type_phrase(node.get("items") or {}, spec, depth + 1)
        return f"{inner or 'any'}[]"
    if node.get("enum"):
        return f"{kind or 'string'} enum"
    return kind or "any"


def facet_list(node: dict) -> list:
    out = []
    for key in FACETS:
        if node.get(key) is not None:
            out.append(f"{key} {node[key]}")
    if node.get("enum"):
        out.append("one of " + ", ".join(str(e) for e in node["enum"]))
    return out


def was_now(before: str, after: str) -> str:
    """Old struck, new in red - the only honest way to show a leaf that moved."""
    if not before and not after:
        return ""
    if before == after:
        return f'<span class="oat-quiet">{esc(after)}</span>'
    bits = []
    if before:
        bits.append(f'<s class="oat-gone">{esc(before)}</s>')
    if after:
        bits.append(f'<span class="oat-new">{esc(after)}</span>')
    return " ".join(bits)


def walk(key, nb, na, sb, sa, depth, rows, stack, req_b, req_a):
    """One node of the effective shape, then its children."""
    present_b, present_a = nb is not None, na is not None
    status = "same" if present_b and present_a else ("added" if present_a else "removed")
    rb = deref(nb, sb) if present_b else {}
    ra = deref(na, sa) if present_a else {}

    tb = type_phrase(nb, sb) if present_b else ""
    ta = type_phrase(na, sa) if present_a else ""
    fb, fa = ", ".join(facet_list(rb)), ", ".join(facet_list(ra))
    if status == "same" and (tb != ta or fb != fa):
        status = "changed"

    flags = []
    for name in ("readOnly", "writeOnly", "deprecated", "nullable"):
        had, has = bool(rb.get(name)), bool(ra.get(name))
        if has and had:
            flags.append((name, "same"))
        elif has:
            flags.append((name, "added"))
        elif had:
            flags.append((name, "removed"))
    if status == "same" and any(s != "same" for _, s in flags):
        status = "changed"

    required = ""
    if req_b and req_a:
        required = "yes"
    elif req_a:
        required = "added"
    elif req_b:
        required = "removed"
    if status == "same" and required in ("added", "removed"):
        status = "changed"

    node = ra or rb
    rows.append(Row(
        depth=depth, key=key, status=status,
        type_html=(esc(ta or tb) if status in ("added", "removed") else was_now(tb, ta)),
        facets_html=(esc(fa or fb) if status in ("added", "removed") else was_now(fb, fa)),
        doc=node.get("description") or "",
        example="" if node.get("example") is None else str(node["example"]),
        required=required, flags=flags,
    ))

    if depth >= MAX_DEPTH:
        return
    # A schema that contains itself (owner -> pets -> owner) is drawn once and named.
    marker = ref_name(na) or ref_name(nb)
    if marker and marker in stack:
        rows.append(Row(depth + 1, f"^ {marker}", status,
                        doc="already drawn above - recursive, not expanded"))
        return
    if marker:
        stack = stack + (marker,)

    if kind_of(ra) == "array" or kind_of(rb) == "array":
        walk("[ ]", rb.get("items") if present_b else None,
             ra.get("items") if present_a else None,
             sb, sa, depth + 1, rows, stack, False, False)
        return

    pb = rb.get("properties") or {}
    pa = ra.get("properties") or {}
    if not pb and not pa:
        return
    rq_b = set(rb.get("required") or [])
    rq_a = set(ra.get("required") or [])
    for name in merged_keys(list(pb), list(pa)):
        walk(name, pb.get(name), pa.get(name), sb, sa, depth + 1, rows, stack,
             name in rq_b, name in rq_a)


# -- rendering --------------------------------------------------------------------
def render_rows(rows: list, empty: str) -> str:
    if not rows:
        return f'<div class="oat-empty">{esc(empty)}</div>'
    out = []
    for r in rows:
        marks = "".join(
            f'<span class="oat-flag oat-f-{s}">{esc(name)}</span>' for name, s in r.flags
        )
        if r.required == "yes":
            marks = '<span class="oat-req">required</span>' + marks
        elif r.required == "added":
            marks = '<span class="oat-req oat-f-added">now required</span>' + marks
        elif r.required == "removed":
            marks = '<span class="oat-req oat-f-removed">was required</span>' + marks
        example = (f'<span class="oat-eg">e.g. <code>{esc(r.example)}</code></span>'
                   if r.example else "")
        out.append(
            f'<div class="oat-row oat-{r.status}" style="--d:{r.depth}">'
            f'<span class="oat-key">{esc(r.key)}</span>'
            + (f'<span class="oat-type">{r.type_html}</span>' if r.type_html else "")
            + marks
            + (f'<span class="oat-facets">{r.facets_html}</span>' if r.facets_html else "")
            + example
            + (f'<div class="oat-doc">{esc(r.doc)}</div>' if r.doc else "")
            + "</div>"
        )
    return "".join(out)


def column(title: str, subtitle: str, rows: list, empty: str) -> str:
    changed = sum(1 for r in rows if r.status != "same")
    tag = f'<span class="oat-n">{changed} changed</span>' if changed else ""
    return (f'<div class="oat-col"><div class="oat-col-head">{esc(title)}'
            f'<span class="oat-sub">{esc(subtitle)}</span>{tag}</div>'
            f'{render_rows(rows, empty)}</div>')


def operation_tree(method: str, path: str, before: dict, after: dict) -> str:
    """Both columns for one operation: what a caller sends, what it gets back."""
    params_b = parameters(before, method, path)
    params_a = parameters(after, method, path)
    req_rows: list = []
    for name, (schema_a, required_a) in params_a.items():
        schema_b, required_b = params_b.get(name, (None, False))
        walk(name, schema_b, schema_a, before, after, 0, req_rows, (), required_b, required_a)
    for name, (schema_b, required_b) in params_b.items():
        if name not in params_a:
            walk(name, schema_b, None, before, after, 0, req_rows, (), required_b, False)

    body_b = request_schema(before, method, path)
    body_a = request_schema(after, method, path)
    if body_b is not None or body_a is not None:
        walk("body", body_b, body_a, before, after, 0, req_rows, (), True, True)

    res_rows: list = []
    res_b, code_b = response_schema(before, method, path)
    res_a, code_a = response_schema(after, method, path)
    if res_b is not None or res_a is not None:
        walk("body", res_b, res_a, before, after, 0, res_rows, (), False, False)

    code = code_a or code_b or "-"
    return (
        '<details class="oat"><summary>The shape a caller sends and gets back</summary>'
        '<div class="oat-legend">Grey is the contract as it was. '
        '<span class="oat-new">Red is added</span>, '
        '<s class="oat-gone">red struck through is removed</s>. '
        '<code>$ref</code>s are resolved, so a schema that moved shows up here, at every '
        'operation that serves it. Example values are the spec\'s own.</div>'
        '<div class="oat-cols">'
        + column("Request", "path & query parameters, then the body", req_rows,
                 "This operation takes no parameters and no body.")
        + column("Response", f"{code} - the primary success body", res_rows,
                 "This operation returns no body.")
        + "</div></details>"
    )


CSS = """
.oat { margin:.5rem 0 0; }
.oat > summary { cursor:pointer; color:var(--muted); font-size:.8rem; }
.oat-legend { color:var(--muted); font-size:.78rem; line-height:1.7; margin:.5rem 0 .2rem; }
.oat-cols { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.9rem;
            margin-top:.5rem; }
@media (max-width:820px) { .oat-cols { grid-template-columns:minmax(0,1fr); } }
.oat-col { border:1px solid var(--line); border-radius:8px; padding:.5rem .6rem; min-width:0; }
.oat-col-head { display:flex; align-items:baseline; gap:.45rem; flex-wrap:wrap;
                border-bottom:1px solid var(--line); padding-bottom:.3rem; margin-bottom:.35rem;
                font:700 .8rem/1.6 inherit; text-transform:uppercase; letter-spacing:.05em; }
.oat-sub { font:400 .72rem/1.6 inherit; text-transform:none; letter-spacing:0; color:var(--muted); }
.oat-n { margin-left:auto; background:#fdeaea; color:#8a1c1c; border-radius:999px;
         padding:0 .45rem; font:700 .68rem/1.7 inherit; text-transform:none; letter-spacing:0; }
.oat-empty { color:var(--muted); font-size:.82rem; padding:.3rem 0; }
.oat-row { padding:.12rem 0 .12rem calc(var(--d) * .95rem); font-size:.82rem; line-height:1.65;
           display:flex; align-items:baseline; gap:.4rem; flex-wrap:wrap; }
.oat-row:hover { background:var(--code-bg); }
.oat-key { font:600 12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); }
.oat-added .oat-key, .oat-changed .oat-key { color:#c62828; }
.oat-removed .oat-key { color:#c62828; text-decoration:line-through; }
.oat-type { font:12px/1.6 ui-monospace,Menlo,monospace; color:var(--muted); }
.oat-added .oat-type { color:#c62828; }
.oat-removed .oat-type { color:#c62828; text-decoration:line-through; }
.oat-quiet { color:var(--muted); }
.oat-new { color:#c62828; font-weight:600; }
.oat-gone { color:#c62828; opacity:.75; }
.oat-facets, .oat-eg { color:var(--muted); font-size:.72rem; }
.oat-eg code { background:var(--code-bg); border-radius:3px; padding:0 .22rem;
               font:11.5px/1.5 ui-monospace,Menlo,monospace; }
.oat-doc { flex-basis:100%; color:var(--muted); font-size:.74rem; line-height:1.55; }
.oat-req, .oat-flag { font-size:.64rem; font-weight:700; text-transform:uppercase;
                      letter-spacing:.04em; border-radius:3px; padding:0 .28rem;
                      background:var(--code-bg); color:var(--muted); }
.oat-req { background:#fdeaea; color:#8a1c1c; }
.oat-f-added { background:#fdeaea; color:#8a1c1c; }
.oat-f-removed { background:#fdeaea; color:#8a1c1c; text-decoration:line-through; }
@media (prefers-color-scheme: dark) {
  .oat-n, .oat-req, .oat-f-added, .oat-f-removed { background:#3a1f1f; color:#f2a0a0; }
  .oat-added .oat-key, .oat-changed .oat-key, .oat-removed .oat-key,
  .oat-added .oat-type, .oat-removed .oat-type, .oat-new, .oat-gone { color:#f08a8a; }
}
"""
