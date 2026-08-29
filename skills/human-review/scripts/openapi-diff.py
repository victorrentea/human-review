#!/usr/bin/env python3
"""What this branch did to the REST contract, read from the OpenAPI spec itself.

`git diff openapi.yaml` answers the question in the wrong currency. A reviewer does
not care that line 1937 gained six lines; they care that `VisitDto` grew three
read-only fields, that `VisitFieldsDto` now accepts a `vetId`, and — the only
question that actually decides a review — whether any of it *breaks a client*.

So this compares the two specs as **structures**, not as text:

  * every operation (`METHOD /path`) added, removed or changed;
  * every schema in `components.schemas` added, removed or changed, down to the
    property, its type, and its constraints;
  * each individual change classified — **breaking** / **additive** / **changed**
    / **cosmetic** — by what it does to somebody already calling this API.

The raw unified diff is still carried, folded away at the bottom, because a
classifier is a summary and a reviewer is entitled to the source.

Emits an HTML fragment for `build-review-html.py` to `includeHtml`, in the same
shape as `endpoint-complexity-delta.py`: `--css` prints the stylesheet it needs.

Usage (from the repository root — the project is resolved from the CWD):
    openapi-diff.py --base origin/main --out .human-review/assets/openapi-diff.html
    openapi-diff.py before.yaml after.yaml
    openapi-diff.py --css > .human-review/assets/openapi-diff.css
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - the message is the whole handling
    raise SystemExit("[openapi-diff] needs PyYAML: python3 -m pip install pyyaml")

METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

# How each kind of change lands on somebody already calling this API. The chip colour
# is that verdict and nothing else — it is not a judgement of the change.
BREAKING, ADDITIVE, CHANGED, COSMETIC = "breaking", "additive", "changed", "cosmetic"
LEVEL_ORDER = {BREAKING: 0, ADDITIVE: 1, CHANGED: 2, COSMETIC: 3}
LEVEL_LABEL = {
    BREAKING: "breaking",
    ADDITIVE: "additive",
    CHANGED: "changed",
    COSMETIC: "cosmetic",
}

# Keys whose only readership is a human reading the docs.
DOC_KEYS = {"description", "summary", "title", "example", "examples", "externalDocs"}

# Constraints, and which direction of movement tightens the contract. A tightened
# constraint rejects a payload that used to be accepted, which is a break; loosening
# one cannot break anybody.
TIGHTENS_WHEN_UP = {"minimum", "minLength", "minItems", "exclusiveMinimum", "multipleOf"}
TIGHTENS_WHEN_DOWN = {"maximum", "maxLength", "maxItems", "exclusiveMaximum"}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def repo_root() -> Path:
    out = run(["git", "rev-parse", "--show-toplevel"], check=True)
    return Path(out.stdout.strip())


def load_yaml(text: str):
    return yaml.safe_load(text) or {}


def spec_at_ref(ref: str, rel: str, root: Path):
    """The spec as of `ref`. A spec that did not exist there is an empty one, not a crash."""
    out = run(["git", "show", f"{ref}:{rel}"], cwd=root)
    if out.returncode != 0:
        return {}
    return load_yaml(out.stdout)


# ── locating a key in the working-tree YAML, so every subject can be clicked open ──
# The spec is generated with plain block mappings at two-space indent, so an
# indentation walk is exact here and degrades to "no link" anywhere it is not.
def line_index(text: str) -> dict:
    index, stack = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(\s*)(?:- )?(\"[^\"]+\"|'[^']+'|[^\s#][^:]*?):(?:\s|$)", line)
        if not m:
            continue
        depth = len(m.group(1))
        key = m.group(2).strip("\"'")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack.append((depth, key))
        index[tuple(k for _, k in stack)] = n
    return index


# ── rendering a schema fragment as one readable phrase ────────────────────────────
def type_of(node) -> str:
    if not isinstance(node, dict):
        return ""
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    kind = node.get("type") or ("object" if "properties" in node else "")
    if kind == "array":
        return f"{type_of(node.get('items') or {})}[]" or "array"
    if node.get("format"):
        kind = f"{kind}({node['format']})"
    if node.get("enum"):
        kind = f"{kind} enum[{', '.join(str(e) for e in node['enum'])}]"
    return kind or "any"


def facets(node) -> str:
    """The constraints worth showing next to a type, in the order a reader scans them."""
    if not isinstance(node, dict):
        return ""
    bits = []
    for key in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
        if key in node:
            bits.append(f"{key} {node[key]}")
    if node.get("readOnly"):
        bits.append("read-only")
    if node.get("writeOnly"):
        bits.append("write-only")
    if node.get("deprecated"):
        bits.append("deprecated")
    return ", ".join(bits)


# ── the comparison ────────────────────────────────────────────────────────────────
class Change:
    __slots__ = ("level", "text", "detail")

    def __init__(self, level, text, detail=""):
        self.level, self.text, self.detail = level, text, detail


def classify(trail: tuple, old, new) -> str:
    """What a single leaf change does to a caller, from where it sits in the spec."""
    key = trail[-1] if trail else ""
    if key in DOC_KEYS or (len(trail) > 1 and trail[-2] in DOC_KEYS):
        return COSMETIC
    if key == "$ref" or key == "type" or key == "format":
        return BREAKING
    if key in TIGHTENS_WHEN_UP:
        return BREAKING if _gt(new, old) else ADDITIVE
    if key in TIGHTENS_WHEN_DOWN:
        return BREAKING if _gt(old, new) else ADDITIVE
    if key == "enum":
        removed = set(map(str, old or [])) - set(map(str, new or []))
        return BREAKING if removed else ADDITIVE
    if key == "deprecated":
        return CHANGED
    if key in ("nullable", "readOnly", "writeOnly", "default", "operationId"):
        return CHANGED
    if old is None:
        return ADDITIVE
    if new is None:
        return BREAKING
    return CHANGED


def _gt(a, b) -> bool:
    try:
        return float(a) > float(b)
    except (TypeError, ValueError):
        return str(a) > str(b)


def show(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)[:160]
    return str(v)


def deep_changes(before, after, trail=()) -> list:
    """Every leaf that differs, as a trail plus its two values.

    Deliberately generic: an OpenAPI document is a big open map, and a
    hand-written walk over the shapes we happen to have seen would go quiet
    exactly when the spec grows something new."""
    out = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            out += deep_changes(before.get(key), after.get(key), trail + (str(key),))
    elif isinstance(before, list) and isinstance(after, list):
        if before != after:
            out.append((trail, before, after))
    elif before != after:
        out.append((trail, before, after))
    return out


TRAIL_NOISE = ("content", "application/json", "schema", "properties")


def pretty_trail(trail: tuple) -> str:
    """The trail as a reader would say it, with the plumbing segments dropped."""
    kept = [t for t in trail if t not in TRAIL_NOISE]
    return ".".join(kept) or ".".join(trail)


def collapse_subtrees(leaves: list) -> list:
    """A whole response (or requestBody) that appeared is one fact, not eight.

    `deep_changes` bottoms out at scalars, which is right for a field whose type
    moved and wrong for a status code that did not exist before: the reviewer gets
    `responses.404.description added: Not found` when what happened is "there is a
    404 now". Where every leaf under one response code is a pure addition (or a pure
    removal), it is reported as that one line."""
    out, folded = [], set()
    roots = {}
    for trail, old, new in leaves:
        if len(trail) >= 2 and trail[0] == "responses":
            roots.setdefault(trail[:2], []).append((old, new))
    for root, pairs in roots.items():
        if all(o is None for o, _ in pairs):
            folded.add(root)
            out.append((root, None, WHOLE))
        elif all(n is None for _, n in pairs):
            folded.add(root)
            out.append((root, WHOLE, None))
    out += [l for l in leaves if l[0][:2] not in folded]
    return out


class _Whole:
    """Stands for "this entire subtree", so a folded change reads as one sentence."""


WHOLE = _Whole()


def changes_from_leaves(before, after, trail=()) -> list:
    changes = []
    for path, old, new in collapse_subtrees(deep_changes(before, after, trail)):
        level = classify(path, old, new)
        label = pretty_trail(path)
        if new is WHOLE or old is WHOLE:
            what = "response" if path[0] == "responses" else path[0]
            verb = "added" if new is WHOLE else "removed"
            changes.append(Change(level, f"{what} <code>{html.escape(path[-1])}</code> {verb}"))
            continue
        if old is None:
            text = f"<code>{html.escape(label)}</code> added: {html.escape(show(new))}"
        elif new is None:
            text = f"<code>{html.escape(label)}</code> removed (was {html.escape(show(old))})"
        else:
            text = (
                f"<code>{html.escape(label)}</code> {html.escape(show(old))} "
                f"&rarr; {html.escape(show(new))}"
            )
        changes.append(Change(level, text))
    return changes


def diff_properties(before: dict, after: dict, in_request: bool) -> list:
    """Property-level diff of one schema — the level a reviewer actually reads."""
    bp, ap = before.get("properties") or {}, after.get("properties") or {}
    breq, areq = set(before.get("required") or []), set(after.get("required") or [])
    changes = []

    for name in sorted(set(ap) - set(bp)):
        node = ap[name]
        required = name in areq
        # A new required field in a request rejects every payload written before it.
        level = BREAKING if (required and in_request) else ADDITIVE
        extra = facets(node)
        changes.append(
            Change(
                level,
                f'<b class="oad-add">+</b> <code>{html.escape(name)}</code> '
                f'<span class="oad-type">{html.escape(type_of(node))}</span>'
                + (f' <span class="oad-facets">{html.escape(extra)}</span>' if extra else "")
                + (' <span class="oad-req">required</span>' if required else ""),
                (node.get("description") or "") if isinstance(node, dict) else "",
            )
        )

    for name in sorted(set(bp) - set(ap)):
        changes.append(
            Change(
                BREAKING,
                f'<b class="oad-del">&minus;</b> <code>{html.escape(name)}</code> '
                f'<span class="oad-type">{html.escape(type_of(bp[name]))}</span> removed',
            )
        )

    for name in sorted(set(bp) & set(ap)):
        for change in changes_from_leaves(bp[name], ap[name], (name,)):
            changes.append(change)

    for name in sorted(areq - breq):
        if name in ap and name not in bp:
            continue  # already said, on the line that added the property
        changes.append(
            Change(
                BREAKING if in_request else CHANGED,
                f'<code>{html.escape(name)}</code> is now required',
            )
        )
    for name in sorted(breq - areq):
        changes.append(
            Change(ADDITIVE, f'<code>{html.escape(name)}</code> is no longer required')
        )
    return changes


def operations(spec: dict) -> dict:
    out = {}
    for path, node in (spec.get("paths") or {}).items():
        if not isinstance(node, dict):
            continue
        for method, op in node.items():
            if method.lower() in METHODS:
                out[(path, method.lower())] = op or {}
    return out


class Subject:
    """One thing a reviewer looks at: an operation, or a schema."""

    def __init__(self, group, name, status, changes, anchor=None, note=""):
        self.group, self.name, self.status = group, name, status
        self.changes = sorted(changes, key=lambda c: LEVEL_ORDER[c.level])
        self.anchor, self.note = anchor, note

    @property
    def level(self):
        if self.status == "removed":
            return BREAKING
        if self.status == "added":
            return ADDITIVE
        return self.changes[0].level if self.changes else COSMETIC


def compare(before: dict, after: dict, index: dict) -> list:
    subjects = []
    ob, oa = operations(before), operations(after)

    for key in sorted(set(oa) - set(ob)):
        path, method = key
        op = oa[key]
        subjects.append(
            Subject(
                "paths",
                f"{method.upper()} {path}",
                "added",
                [],
                index.get(("paths", path, method)),
                op.get("summary") or op.get("operationId") or "",
            )
        )
    for key in sorted(set(ob) - set(oa)):
        path, method = key
        subjects.append(
            Subject("paths", f"{method.upper()} {path}", "removed", [], None,
                    ob[key].get("summary") or "")
        )
    for key in sorted(set(ob) & set(oa)):
        path, method = key
        changes = changes_from_leaves(ob[key], oa[key])
        if changes:
            subjects.append(
                Subject("paths", f"{method.upper()} {path}", "modified", changes,
                        index.get(("paths", path, method)),
                        oa[key].get("summary") or "")
            )

    sb = (before.get("components") or {}).get("schemas") or {}
    sa = (after.get("components") or {}).get("schemas") or {}
    # A schema reachable from a request body is one a *client* fills in, so adding a
    # required field to it breaks callers; the same addition to a response schema
    # only breaks a client that validates strictly.
    request_schemas = request_side(after)

    for name in sorted(set(sa) - set(sb)):
        subjects.append(
            Subject("schemas", name, "added", [],
                    index.get(("components", "schemas", name)),
                    f"{len((sa[name] or {}).get('properties') or {})} properties")
        )
    for name in sorted(set(sb) - set(sa)):
        subjects.append(Subject("schemas", name, "removed", [], None, ""))
    for name in sorted(set(sb) & set(sa)):
        changes = diff_properties(sb[name] or {}, sa[name] or {}, name in request_schemas)
        # Everything about the schema that is not a property: its own type, its docs.
        rest_b = {k: v for k, v in (sb[name] or {}).items() if k not in ("properties", "required")}
        rest_a = {k: v for k, v in (sa[name] or {}).items() if k not in ("properties", "required")}
        changes += changes_from_leaves(rest_b, rest_a)
        if changes:
            subjects.append(
                Subject("schemas", name, "modified", changes,
                        index.get(("components", "schemas", name)),
                        "request body" if name in request_schemas else "")
            )

    return subjects


def request_side(spec: dict) -> set:
    """Schema names reachable from any requestBody or parameter, transitively."""
    schemas = (spec.get("components") or {}).get("schemas") or {}
    seen, queue = set(), []

    def refs(node):
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                yield node["$ref"].rsplit("/", 1)[-1]
            for v in node.values():
                yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)

    for op in operations(spec).values():
        for part in (op.get("requestBody"), op.get("parameters")):
            queue += list(refs(part))
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue += list(refs(schemas.get(name) or {}))
    return seen


# ── rendering ─────────────────────────────────────────────────────────────────────
VERB_CLASS = {"GET": "get", "POST": "post", "PUT": "put", "PATCH": "put", "DELETE": "delete"}


def render(subjects: list, raw_diff: str, spec_abs: Path, spec_rel: str) -> str:
    if not subjects:
        return (
            '<p class="oad-lede">The REST contract is byte-identical to the base — '
            "no operation, schema or constraint moved.</p>"
            + raw_block(raw_diff)
        )

    tally = {}
    for s in subjects:
        for c in (s.changes or [Change(s.level, "")]):
            tally[c.level] = tally.get(c.level, 0) + 1

    counts = " ".join(
        f'<span class="oad-chip oad-{level}">{tally[level]} {LEVEL_LABEL[level]}</span>'
        for level in (BREAKING, ADDITIVE, CHANGED, COSMETIC)
        if tally.get(level)
    )
    ops = sum(1 for s in subjects if s.group == "paths")
    schemas = sum(1 for s in subjects if s.group == "schemas")
    lede = (
        f'<p class="oad-lede">Read from <code>{html.escape(spec_rel)}</code> as a structure, not as '
        f"text: <b>{ops}</b> operation{'s' if ops != 1 else ''} and <b>{schemas}</b> "
        f"schema{'s' if schemas != 1 else ''} moved. Each line is classified by what it does to "
        f"somebody already calling this API — {counts}</p>"
    )

    parts = [lede]
    for group, heading in (("paths", "Operations"), ("schemas", "Schemas")):
        rows = [s for s in subjects if s.group == group]
        if not rows:
            continue
        rows.sort(key=lambda s: (LEVEL_ORDER[s.level], s.name))
        parts.append(f'<div class="oad-group"><div class="oad-kind">{heading} '
                     f'<span class="oad-count">{len(rows)}</span></div>')
        for s in rows:
            parts.append(render_subject(s, spec_abs))
        parts.append("</div>")
    parts.append(raw_block(raw_diff))
    return "\n".join(parts)


def render_subject(s: Subject, spec_abs: Path) -> str:
    name = html.escape(s.name)
    if s.group == "paths":
        verb, _, path = s.name.partition(" ")
        name = (f'<span class="oad-verb oad-{VERB_CLASS.get(verb, "get")}">{html.escape(verb)}</span>'
                f'<code class="oad-path">{html.escape(path)}</code>')
    else:
        name = f'<code class="oad-path">{name}</code>'
    if s.anchor:
        name = (f'<a class="oad-link" href="vscode://file/{spec_abs}:{s.anchor}:1" '
                f'data-tip="open the spec at this definition">{name}</a>')

    items = "".join(
        f'<li class="oad-{c.level}"><span class="oad-chip oad-{c.level}">'
        f'{LEVEL_LABEL[c.level]}</span>{c.text}'
        + (f'<span class="oad-note">{html.escape(c.detail)}</span>' if c.detail else "")
        + "</li>"
        for c in s.changes
    )
    return (
        f'<div class="oad-subject oad-is-{s.status}">'
        f'<div class="oad-head">{name}'
        f'<span class="oad-chip oad-{s.level}">{html.escape(s.status)}</span>'
        + (f'<span class="oad-note">{html.escape(s.note)}</span>' if s.note else "")
        + "</div>"
        + (f'<ul class="oad-changes">{items}</ul>' if items else "")
        + "</div>"
    )


def raw_block(raw: str) -> str:
    if not raw.strip():
        return ""
    lines = []
    for line in raw.splitlines():
        cls = ""
        if line.startswith("+") and not line.startswith("+++"):
            cls = " class=oad-add"
        elif line.startswith("-") and not line.startswith("---"):
            cls = " class=oad-del"
        elif line.startswith("@@"):
            cls = " class=oad-hunk"
        lines.append(f"<span{cls}>{html.escape(line)}</span>")
    return (
        '<details class="oad-raw"><summary>The unified diff this was read from</summary>'
        "<pre>" + "\n".join(lines) + "</pre></details>"
    )


CSS = """
.oad-lede, .oad-group { --oad-break:#c62828; --oad-add:#2e7d32; --oad-chg:#b56b00; --oad-cos:#6b6b78; }
.oad-lede { margin:.4rem 0 1rem; color:var(--muted); font-size:.9rem; line-height:1.7; }
.oad-lede b { color:var(--fg); }
.oad-group { margin:1.2rem 0; }
.oad-kind { font:600 .82rem/1.6 inherit; text-transform:uppercase; letter-spacing:.06em;
            color:var(--muted); border-bottom:1px solid var(--line); padding-bottom:.3rem; }
.oad-count { background:var(--code-bg); border-radius:999px; padding:0 .4rem; margin-left:.3rem;
             font-size:.72rem; }
.oad-subject { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--line);
               border-radius:8px; padding:.6rem .8rem; margin:.6rem 0; }
.oad-subject.oad-is-added { border-left-color:var(--oad-add); }
.oad-subject.oad-is-removed { border-left-color:var(--oad-break); }
.oad-head { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
.oad-link { text-decoration:none; display:inline-flex; align-items:center; gap:.45rem; }
.oad-link:hover .oad-path { text-decoration:underline; }
.oad-verb { font:700 10.5px/1.7 ui-monospace,Menlo,monospace; border-radius:4px; padding:0 .38rem;
            color:#fff; letter-spacing:.04em; }
.oad-get { background:#2f6fb5; } .oad-post { background:#2e7d32; }
.oad-put { background:#b56b00; } .oad-delete { background:#c62828; }
.oad-path { font:600 12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg); }
.oad-chip { border-radius:4px; padding:.06rem .4rem; font-size:.68rem; font-weight:700;
            text-transform:uppercase; letter-spacing:.04em; white-space:nowrap; }
.oad-chip.oad-breaking { background:#fdeaea; color:#8a1c1c; }
.oad-chip.oad-additive { background:#eef7ef; color:#245c30; }
.oad-chip.oad-changed  { background:#fdf3e2; color:#8a5a12; }
.oad-chip.oad-cosmetic { background:#f0f0f4; color:#5d5d6b; }
.oad-chip.oad-modified, .oad-chip.oad-added, .oad-chip.oad-removed { background:var(--code-bg); color:var(--muted); }
.oad-note { color:var(--muted); font-size:.8rem; }
.oad-changes { list-style:none; margin:.5rem 0 0; padding:0; display:grid; gap:.3rem; }
.oad-changes li { display:flex; align-items:baseline; gap:.5rem; font-size:.88rem; flex-wrap:wrap; }
.oad-changes code { font:600 12px/1.5 ui-monospace,Menlo,monospace; background:var(--code-bg);
                    border-radius:3px; padding:0 .25rem; }
.oad-type { color:var(--link); font:12px/1.5 ui-monospace,Menlo,monospace; }
.oad-facets { color:var(--muted); font-size:.78rem; }
.oad-req { color:#8a1c1c; font-size:.72rem; font-weight:700; text-transform:uppercase; }
.oad-changes b.oad-add { color:#2e7d32; } .oad-changes b.oad-del { color:#c62828; }
.oad-raw { margin:1.4rem 0 0; }
.oad-raw summary { cursor:pointer; color:var(--muted); font-size:.85rem; }
.oad-raw pre { margin:.6rem 0 0; background:var(--code-bg); border-radius:6px; padding:.6rem .8rem;
               overflow-x:auto; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
               display:flex; flex-direction:column; }
.oad-raw span.oad-add { color:#2e7d32; } .oad-raw span.oad-del { color:#c62828; }
.oad-raw span.oad-hunk { color:var(--muted); }
@media (prefers-color-scheme: dark) {
  .oad-chip.oad-breaking { background:#3a1f1f; color:#f2a0a0; }
  .oad-chip.oad-additive { background:#1b2c1f; color:#9ad3a5; }
  .oad-chip.oad-changed  { background:#3a3018; color:#e6c07b; }
  .oad-chip.oad-cosmetic { background:#26262f; color:#a5a5b4; }
  .oad-changes b.oad-add, .oad-raw span.oad-add { color:#8fd39c; }
  .oad-changes b.oad-del, .oad-raw span.oad-del { color:#f08a8a; }
  .oad-req { color:#f2a0a0; }
}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("before", nargs="?", help="spec file to compare from (default: --base's copy)")
    ap.add_argument("after", nargs="?", help="spec file to compare to (default: the working tree)")
    ap.add_argument("--base", default="origin/main", help="git ref to read the base spec from")
    ap.add_argument("--spec", default="openapi.yaml", help="repo-relative path to the spec")
    ap.add_argument("--out", help="write the HTML fragment here instead of stdout")
    ap.add_argument("--json", action="store_true", help="emit the classified changes as JSON")
    ap.add_argument("--css", action="store_true", help="print the stylesheet this fragment needs")
    args = ap.parse_args(argv)

    if args.css:
        print(CSS)
        return 0

    spec_rel = args.spec
    if args.before and args.after:
        # Two files on the command line: a self-contained comparison that needs no
        # repository, which is how the tests drive it.
        before = load_yaml(Path(args.before).read_text(encoding="utf-8"))
        after_text = Path(args.after).read_text(encoding="utf-8")
        spec_abs = Path(args.after).resolve()
        spec_rel = args.after
        raw = ""
    else:
        root = repo_root()
        target = root / spec_rel
        if not target.is_file():
            raise SystemExit(f"[openapi-diff] no spec at {target} — pass --spec")
        merge_base = run(["git", "merge-base", args.base, "HEAD"], cwd=root)
        base = merge_base.stdout.strip() if merge_base.returncode == 0 else args.base
        before = spec_at_ref(base, spec_rel, root)
        after_text = target.read_text(encoding="utf-8")
        spec_abs = target.resolve()
        raw = run(["git", "--no-pager", "diff", "--no-color", base, "--", spec_rel],
                  cwd=root).stdout

    after = load_yaml(after_text)
    subjects = compare(before, after, line_index(after_text))

    if args.json:
        body = json.dumps(
            [
                {
                    "group": s.group, "name": s.name, "status": s.status, "level": s.level,
                    "changes": [
                        {"level": c.level, "text": html.unescape(re.sub("<[^>]+>", "", c.text))}
                        for c in s.changes
                    ],
                }
                for s in subjects
            ],
            indent=1,
        )
    else:
        body = render(subjects, raw, spec_abs, spec_rel)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        breaking = sum(1 for s in subjects for c in s.changes if c.level == BREAKING)
        print(f"[openapi-diff] wrote {out} — {len(subjects)} subjects, {breaking} breaking",
              file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
