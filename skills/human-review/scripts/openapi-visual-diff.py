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
        listed = [
            {"text": c["text"], "level": c["level"], "id": c["id"],
             "mark": change_mark(c["id"]), "target": change_target(op_obj, c)}
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
            elif names_a_field:
                c["target"] = None
                skipped += 1
        entries[f"{m} {p}"] = {
            "state": state,
            "changes": listed,
            # Said out loud on the page. A tree that quietly opens 12 of 30 fields is the
            # same lie as one that opens none, only harder to notice.
            "deepSkipped": skipped,
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


def render(model, entries, global_changes, tags, old_label, new_label) -> str:
    counts = {}
    for e in entries.values():
        counts[e["state"]] = counts.get(e["state"], 0) + 1

    payload = json.dumps(
        {
            "spec": model,
            "ops": entries,
            "global": [
                {"text": c["text"], "level": c["level"], "id": c["id"],
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

    return TEMPLATE.replace("__PAYLOAD__", payload)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenAPI visual diff</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.29.1/swagger-ui.min.css">
<style>
  :root {
    --dv-breaking: #d7263d;
    --dv-modified: #d98218;
    --dv-added:    #2e9e5b;
    --dv-removed:  #8a8f98;
    /* palette shared with the Human Review report, so an embedded frame doesn't
       announce itself as a foreign document */
    --dv-bg:    #fbfbfd;
    --dv-fg:    #1c1c22;
    --dv-muted: #6b6b78;
    --dv-line:  #e2e2ea;
    --dv-card:  #ffffff;
    --dv-code:  rgba(0,0,0,.06);
  }
  /* System theme by default; ?theme=dark|light pins it, which is what the
     embedding page uses when it wants the frame to match rather than guess. */
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --dv-breaking: #f0757f; --dv-modified: #e0a44a; --dv-added: #6fce93;
      --dv-removed: #9aa0aa;
      --dv-bg: #15151a; --dv-fg: #e8e8ef; --dv-muted: #9a9aa8;
      --dv-line: #2c2c36; --dv-card: #1d1d24; --dv-code: rgba(255,255,255,.10);
    }
  }
  :root[data-theme="dark"] {
    --dv-breaking: #f0757f; --dv-modified: #e0a44a; --dv-added: #6fce93;
    --dv-removed: #9aa0aa;
    --dv-bg: #15151a; --dv-fg: #e8e8ef; --dv-muted: #9a9aa8;
    --dv-line: #2c2c36; --dv-card: #1d1d24; --dv-code: rgba(255,255,255,.10);
  }
  body { margin: 0; background: var(--dv-bg); color: var(--dv-fg);
         color-scheme: light dark; }

  /* ---------- toolbar ---------- */
  .dv-bar {
    position: sticky; top: 0; z-index: 50;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 10px 20px;
    background: #1b1b1f; color: #eaeaea;
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    box-shadow: 0 2px 10px rgba(0,0,0,.25);
  }
  .dv-bar h1 { font-size: 14px; margin: 0 8px 0 0; font-weight: 600; letter-spacing: .01em; }
  .dv-bar .dv-vs { opacity: .65; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .dv-bar .dv-vs b { color: #fff; font-weight: 600; }
  .dv-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 999px;
    background: rgba(255,255,255,.08); cursor: pointer; user-select: none;
    border: 1px solid transparent;
  }
  .dv-chip:hover { background: rgba(255,255,255,.16); }
  .dv-chip.off { opacity: .38; }
  .dv-chip .dot { width: 9px; height: 9px; border-radius: 50%; }
  .dv-chip b { font-variant-numeric: tabular-nums; }
  .dot.breaking { background: var(--dv-breaking); }
  .dot.modified { background: var(--dv-modified); }
  .dot.added    { background: var(--dv-added); }
  .dot.removed  { background: var(--dv-removed); }
  .dot.untouched{ background: #4a4a52; }
  .dv-spacer { flex: 1; }
  .dv-toggle {
    display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    padding: 3px 10px; border-radius: 6px; background: rgba(255,255,255,.08);
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

  /* ---------- per-operation annotations ---------- */
  .dv-note { padding: 2px 0 10px; }
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
  .dv-change.l2 .lvl { background: rgba(217,130,24,.14); color: #9a5b06; }
  .dv-change.l1 .lvl { background: rgba(46,158,91,.12); color: #1f7a45; }

  .dv-badge {
    font-size: 10px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    padding: 2px 8px; border-radius: 4px; margin-left: 8px; color: #fff; flex: none;
  }
  .dv-badge.breaking { background: var(--dv-breaking); }
  .dv-badge.modified { background: var(--dv-modified); }
  .dv-badge.added    { background: var(--dv-added); }
  .dv-badge.removed  { background: var(--dv-removed); }

  /* ---------- the trail down to a changed field ----------
     Opening the tree is only half of it: once four levels are showing, the reader still
     has to find which of the fields on screen is the new one. The trail is deliberately
     quieter than the leaf — a hairline on every ancestor says "this is the way down",
     the leaf says "here". Levels reuse the note list's l1/l2/l3 vocabulary rather than
     inventing a third scale, and every colour comes from the theme vars so dark mode
     needs no second set of rules. */
  .swagger-ui .dv-hit-path { box-shadow: inset 2px 0 0 var(--dv-muted); }
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
  .swagger-ui .dv-hit.l1 { background: rgba(46,158,91,.10); color: var(--dv-added); }
  .swagger-ui .dv-hit.l2 { background: rgba(217,130,24,.12); color: var(--dv-modified); }
  .swagger-ui .dv-hit.l3 { background: rgba(215,38,61,.10); color: var(--dv-breaking); }
  .swagger-ui tr.dv-hit.l1 > td { background: rgba(46,158,91,.10); }
  .swagger-ui tr.dv-hit.l2 > td { background: rgba(217,130,24,.12); }
  .swagger-ui tr.dv-hit.l3 > td { background: rgba(215,38,61,.10); }
  .dv-fieldmark {
    font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
    padding: 1px 6px; border-radius: 4px; margin-left: 8px; align-self: center;
  }
  .dv-fieldmark.l1 { background: rgba(46,158,91,.14); color: var(--dv-added); }
  .dv-fieldmark.l2 { background: rgba(217,130,24,.16); color: var(--dv-modified); }
  .dv-fieldmark.l3 { background: rgba(215,38,61,.14); color: var(--dv-breaking); }
  /* Fields the auto-expand deliberately did not open. Saying nothing here would teach
     the reader that what is open is everything — the exact failure this feature fixes. */
  .dv-deepmore {
    padding: 2px 14px 6px 12px; font-size: 12px; font-style: italic;
    color: var(--dv-muted);
  }

  /* ---------- the whole point: fade what nobody touched ---------- */
  .swagger-ui .opblock { transition: opacity .18s ease, filter .18s ease; }
  .swagger-ui .opblock.dv-untouched {
    opacity: .32; filter: saturate(.15);
  }
  .swagger-ui .opblock.dv-untouched:hover { opacity: .85; filter: saturate(.6); }
  body.dv-hide-untouched .opblock.dv-untouched,
  body.dv-hide-untouched .opblock-tag-section.dv-empty { display: none; }
  .swagger-ui .opblock-tag-section.dv-quiet > h3 { opacity: .4; }

  /* operations read as children of their controller, not as siblings of it */
  .swagger-ui .opblock-tag-section > div {
    margin-left: 6px;
    padding-left: 20px;
    border-left: 2px solid var(--dv-line);
  }

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
  .swagger-ui .info { margin: 20px 0 12px; }
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
  .swagger-ui .tab li, .swagger-ui label, .swagger-ui .btn {
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
  .swagger-ui .opblock.opblock-deprecated { opacity: .7; }
  .dv-count-hidden { font-size: 12px; opacity: .6; }
</style>
</head>
<body>
<div class="dv-bar">
  <h1>OpenAPI visual diff</h1>
  <!-- revision first: the reader came to see what the branch does, and the page's own
       title says "test-pr vs main". Each label still names its true side. -->
  <span class="dv-vs"><b id="dv-new"></b> vs <b id="dv-old"></b></span>
  <span id="dv-chips"></span>
  <span class="dv-spacer"></span>
  <!-- No tooltip here on purpose. The report's one tooltip component (TIP_JS, driven by
       data-tip) lives in the host page and cannot reach into this frame, and a native
       title= is the thing the house rule exists to keep out. The checkbox demonstrates
       itself the moment it is ticked. -->
  <label class="dv-toggle"><input type="checkbox" id="dv-expand"> expand impacted</label>
</div>
<div id="dv-global"></div>
<div id="swagger-ui"></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.29.1/swagger-ui-bundle.min.js"></script>
<script>
// ?theme=dark|light pins the theme; with no parameter the system decides.
const THEME = new URLSearchParams(location.search).get('theme');
if (THEME === 'dark' || THEME === 'light') {
  document.documentElement.setAttribute('data-theme', THEME);
}

const DATA = __PAYLOAD__;
const LABELS = { breaking: 'breaking', modified: 'modified', added: 'added',
                 removed: 'removed', untouched: 'untouched' };
const ORDER = ['breaking', 'modified', 'added', 'removed', 'untouched'];

document.getElementById('dv-old').textContent = DATA.old;
document.getElementById('dv-new').textContent = DATA.new;

// backticked oasdiff prose -> <code>
function md(s) {
  const esc = s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  return esc.replace(/`([^`]+)`/g, '<code>$1</code>');
}

// ---- toolbar chips double as filters ----
const hidden = new Set();
const chips = document.getElementById('dv-chips');
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
      document.body.classList.toggle('dv-hide-untouched', hidden.has(state));
    }
    apply();
  };
  chips.appendChild(el);
});

// ---- non-path changes (components, servers, security...) ----
if (DATA.global.length) {
  const box = document.getElementById('dv-global');
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

// Swagger UI re-renders on expand/collapse, so decorating is idempotent and re-run.
function decorate() {
  document.querySelectorAll('.swagger-ui .opblock').forEach(op => {
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
      b.textContent = info.state === 'modified' && n
        ? `${n} change${n > 1 ? 's' : ''}` : info.state;
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
  });
  apply();
  autoCollapse();
}

// A controller nobody touched is folded away on arrival — once, so that
// re-expanding one by hand sticks.
let collapsedOnce = false;
function autoCollapse() {
  if (collapsedOnce) return;
  const sections = [...document.querySelectorAll('.opblock-tag-section')];
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
  document.querySelectorAll('.swagger-ui .opblock').forEach(op => {
    const s = op.dataset.dvState;
    op.style.display = s && hidden.has(s) ? 'none' : '';
  });
  // a tag section nobody touched fades as a whole; empty ones disappear
  document.querySelectorAll('.opblock-tag-section').forEach(sec => {
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
  document.body.classList.toggle('dv-hide-untouched', on);
  if (on) hidden.add('untouched'); else hidden.delete('untouched');
  document.querySelectorAll('.dv-chip').forEach(c => {
    if (c.textContent.includes('untouched')) c.classList.toggle('off', on);
  });
  apply();
}
if (location.hash.includes('only')) setOnlyTouched(true);

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

// Swagger UI opens a response on "Example Value"; the tree lives behind "Schema". A spec
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
  // An opblock gets its <div class="opblock-body"> before it has a responses table, so
  // asking for the response row the instant the operation opens finds nothing. Wait for
  // the row itself, not for the box it will eventually appear in.
  const host = await waitFor(() => schemaHost(op, c.target), STEP_WAIT);
  if (!host) return false;
  const found = await openSchemaTree(host);
  if (!found) return false;
  const { kit } = found;
  let cur = found.root;
  for (const step of c.target.steps) {
    if (run !== revealRun) return false;        // the reader changed their mind
    if (!await openNode(kit, cur)) return false;
    const next = await waitFor(() => kit.child(cur, step), STEP_WAIT);
    if (!next) return false;
    (kit.mark(cur)).classList.add('dv-hit-path');
    cur = next;
  }
  markLeaf(kit, cur, c);
  return true;
}

// One run at a time. A second click supersedes the first rather than racing it.
let revealRun = 0;

async function revealImpacted() {
  const run = ++revealRun;
  const ops = [...document.querySelectorAll('.swagger-ui .opblock')].filter(op => {
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
    reportMissed(op, missed);
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
document.getElementById('dv-expand').onchange = e => {
  revealRun++;                                   // cancel anything still walking
  document.querySelectorAll('.swagger-ui .opblock').forEach(op => {
    const s = op.dataset.dvState;
    if (!s || s === 'untouched') return;
    const open = op.classList.contains('is-open');
    if (open !== e.target.checked) op.querySelector('.opblock-summary-control')?.click();
  });
  if (e.target.checked) revealImpacted();
};

window.ui = SwaggerUIBundle({
  spec: DATA.spec,   // already fully dereferenced by the generator
  dom_id: '#swagger-ui',
  docExpansion: 'list',
  defaultModelsExpandDepth: -1,
  tryItOutEnabled: false,
  supportedSubmitMethods: [],
  deepLinking: false,
  onComplete: decorate,
});

new MutationObserver(() => {
  clearTimeout(window.__dvT);
  window.__dvT = setTimeout(decorate, 50);
}).observe(document.getElementById('swagger-ui'), { childList: true, subtree: true });

// Embedded in a page, this document should not grow a scrollbar of its own: a frame
// that scrolls internally traps the wheel and hides how much is left. Report our real
// height instead and let the host size the frame — the outer page keeps the only
// scrollbar. Cross-origin over file://, so it goes by postMessage, not by reading us.
function postHeight() {
  if (window.parent === window) return;
  const h = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  if (h !== window.__dvH) {
    window.__dvH = h;
    window.parent.postMessage({ type: 'dv-height', height: h }, '*');
  }
}
new MutationObserver(() => {
  clearTimeout(window.__dvHT);
  window.__dvHT = setTimeout(postHeight, 80);
}).observe(document.body, { childList: true, subtree: true, attributes: true });
window.addEventListener('load', postHeight);
window.addEventListener('resize', postHeight);
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
