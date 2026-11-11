#!/usr/bin/env python3
"""What the branch does to the database, migration by migration — the Data tab's card
under the `DB` delta, for a service that migrates with Alembic.

The ER diagram says what the schema looks like after the branch; this says how it gets
there, and where that is risky. For every Alembic revision the branch adds, read off its
source (no database, nothing imported):

  * its revision, the revision it follows and its message;
  * every `op.*` call of `upgrade()`, one line each, linked to the line that makes it;
  * a flag on the ones a reviewer has to look at twice —
      - **destructive**: `drop_table`, `drop_column`, `drop_constraint`, `drop_index`;
      - **fails on rows**: a column added `nullable=False` with no `server_default`, or a
        column altered to `nullable=False` (on a table that has rows, the migration stops);
      - **raw SQL**: `op.execute(...)` — data moved by hand, or DDL Alembic cannot see;
      - **no downgrade**: `downgrade()` does nothing.

And across the change set:

  * an **edited migration**: a revision file that exists at the base and was changed — it
    may already have run on an environment, and an edit never runs again there;
  * **two heads**: two revisions of the branch follow the same one;
  * **models moved, no migration**: the ER diagram changed (the diagrams step wrote
    `DB.puml` and `DB.base.puml` and they differ) and no revision was added — or the
    other way round, a migration with no model change, which is said and not flagged.

Usage:
    alembic-review.py --base origin/develop --out .human-review/assets/migrations.html
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import subprocess
import sys
from pathlib import Path

DESTRUCTIVE = {"drop_table", "drop_column", "drop_constraint", "drop_index"}
GENERATED = Path(".human-review/generated-puml")

CSS = """
.mg-card { margin:1rem 0; }
.mg-card h3 { margin:.2rem 0 .5rem; font-size:1rem; }
.mg-band { padding:.45rem .7rem; border-radius:6px; margin:.4rem 0; font-size:.92rem;
  border:1px solid currentColor; }
.mg-red { color:var(--dgm-diff-del); }
.mg-amber { color:var(--drift-fg); }
.mg-quiet { color:var(--muted); }
.mg-rev { border:1px solid var(--line); border-radius:8px; padding:.55rem .8rem; margin:.5rem 0;
  background:var(--card); }
.mg-rev-head { display:flex; gap:.6rem; align-items:baseline; flex-wrap:wrap; }
.mg-rev-head code { font-size:.85rem; }
.mg-msg { font-weight:600; }
.mg-ops { list-style:none; margin:.35rem 0 0; padding:0; font:12.5px/1.6 ui-monospace,Menlo,monospace; }
.mg-ops a { color:inherit; text-decoration:none; }
.mg-ops a:hover { text-decoration:underline; }
.mg-flag { font:600 11px/1.4 system-ui,sans-serif; padding:0 .4rem; margin-left:.4rem;
  border-radius:999px; border:1px solid currentColor; white-space:nowrap; cursor:help; }
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def versions_dirs() -> list[str]:
    """Every `…/alembic/versions` (or `migrations/versions`) the repository tracks."""
    dirs = set()
    for p in git("ls-files", "*/versions/*.py", "versions/*.py").splitlines():
        parent = str(Path(p).parent)
        if Path(parent).parent.name in ("alembic", "migrations"):
            dirs.add(parent)
    return sorted(dirs)


def changed(base: str, dirs: list[str]) -> list[tuple[str, str]]:
    """`(status, path)` for every revision file the branch added (A), modified (M) or
    deleted (D), the working tree included (an uncommitted new migration counts)."""
    mb = git("merge-base", base, "HEAD").strip() or base
    out = []
    for line in git("diff", "--name-status", "--no-renames", mb, "--", *dirs).splitlines():
        status, _, path = line.partition("\t")
        if path.endswith(".py"):
            out.append((status[:1], path))
    for path in git("ls-files", "--others", "--exclude-standard", "--", *dirs).splitlines():
        if path.endswith(".py"):
            out.append(("A", path))
    return sorted(set(out), key=lambda t: t[1])


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _func(tree: ast.Module, name: str):
    return next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None)


def describe(call: ast.Call) -> tuple[str, list[tuple[str, str, str]]]:
    """One `op.*` call -> (its line of text, [(flag class, label, why)])."""
    op = call.func.attr
    args = [_const(a) for a in call.args]
    flags = []
    text = op
    if op in ("add_column", "alter_column", "drop_column"):
        table = args[0] if args else "?"
        col = call.args[1] if len(call.args) > 1 else None
        if op == "add_column" and isinstance(col, ast.Call):
            cname = _const(col.args[0]) if col.args else "?"
            ctype = ast.unparse(col.args[1]) if len(col.args) > 1 else ""
            nullable = _const(_kw(col, "nullable"))
            text = f"add_column {table}.{cname} {ctype}" + (" NOT NULL" if nullable is False else "")
            if nullable is False and _kw(col, "server_default") is None:
                flags.append(("mg-amber", "fails on rows",
                              "NOT NULL with no server_default: on a table that has rows, "
                              "the ALTER stops"))
        elif op == "alter_column":
            cname = args[1] if len(args) > 1 else "?"
            changes = [f"{k.arg}={ast.unparse(k.value)}" for k in call.keywords]
            text = f"alter_column {table}.{cname} " + ", ".join(changes)
            if _const(_kw(call, "nullable")) is False and _kw(call, "server_default") is None:
                flags.append(("mg-amber", "fails on rows",
                              "made NOT NULL: any row holding a NULL stops the migration"))
        else:
            text = f"drop_column {table}.{args[1] if len(args) > 1 else '?'}"
    elif op in ("create_table", "drop_table", "create_index", "drop_index",
                "create_foreign_key", "drop_constraint", "rename_table"):
        names = [str(a) for a in args if a is not None][:3]
        text = f"{op} " + " ".join(names)
    elif op == "execute":
        sql = call.args[0] if call.args else None
        snippet = _const(sql) if isinstance(_const(sql), str) else ast.unparse(sql) if sql else ""
        snippet = " ".join(str(snippet).split())
        text = "execute " + (snippet[:110] + ("…" if len(snippet) > 110 else ""))
        flags.append(("mg-amber", "raw SQL", "data moved by hand, or DDL Alembic does not see "
                                             "— read it, it is not in the ER diagram"))
    else:
        text = f"{op} " + " ".join(str(a) for a in args if a is not None)
    if op in DESTRUCTIVE:
        flags.append(("mg-red", "destructive", "removes data or structure; a deploy cannot "
                                                "take it back"))
    return text.strip(), flags


def revision(path: str, text: str) -> dict:
    tree = ast.parse(text)
    meta = {}
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            target = n.targets[0] if isinstance(n, ast.Assign) else n.target
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision"):
                v = n.value
                meta[target.id] = ([_const(e) for e in v.elts] if isinstance(v, (ast.Tuple, ast.List))
                                   else _const(v))
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    ops = []
    up = _func(tree, "upgrade")
    for node in ast.walk(up) if up else ():
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "op":
            line, flags = describe(node)
            ops.append({"line": node.lineno, "text": line, "flags": flags})
    ops.sort(key=lambda o: o["line"])
    down = _func(tree, "downgrade")
    empty_down = down is None or all(isinstance(s, ast.Pass)
                                     or (isinstance(s, ast.Expr) and isinstance(_const(s.value), str))
                                     for s in down.body)
    return {"path": path, "revision": meta.get("revision"), "down": meta.get("down_revision"),
            "message": doc[0] if doc else Path(path).stem, "ops": ops, "noDowngrade": empty_down}


def model_moved() -> bool | None:
    """Whether the ER diagram the diagrams step generated differs between the two sides;
    None when it generated none."""
    head, base = GENERATED / "DB.puml", GENERATED / "DB.base.puml"
    if not head.is_file():
        return None
    strip = lambda p: [l for l in p.read_text(encoding="utf-8").splitlines() if "[[src://" not in l] \
        if p.is_file() else []
    return strip(head) != strip(base)


def render(revs: list[dict], edited: list[str], deleted: list[str], moved, root: Path) -> str:
    out = [f"<style>{CSS}</style>", '<div class="mg-card">', '<h3 class="mg-title">Migrations</h3>']
    bands = []
    if moved and not revs:
        bands.append(("mg-red", "The models changed and the branch adds no migration: the "
                                "schema the code expects is not the one the database will have."))
    if edited:
        bands.append(("mg-red", "Edits a migration that exists at the base — it may already "
                                "have run on an environment, where the edit never runs: "
                      + ", ".join(f"<code>{html.escape(Path(p).name)}</code>" for p in edited)))
    if deleted:
        bands.append(("mg-red", "Deletes a migration: " + ", ".join(
            f"<code>{html.escape(Path(p).name)}</code>" for p in deleted)))
    downs = [r["down"] for r in revs if isinstance(r["down"], str)]
    twice = sorted({d for d in downs if downs.count(d) > 1})
    if twice:
        bands.append(("mg-red", "Two revisions follow <code>" + html.escape(", ".join(twice))
                      + "</code>: the history has two heads, and <code>alembic upgrade head</code> "
                        "stops."))
    if revs and moved is False:
        bands.append(("mg-quiet", "The migrations change no mapped table or column (the ER "
                                  "diagram is the same on both sides) — data, indexes or SQL only."))
    if not revs and not edited and not deleted and not moved:
        bands.append(("mg-quiet", "No migration on this branch, and the models did not change."))
    out += [f'<div class="mg-band {c}">{t}</div>' for c, t in bands]
    for r in revs:
        href = f"vscode://file/{(root / r['path']).resolve()}"
        head = (f'<div class="mg-rev-head"><span class="mg-msg">{html.escape(r["message"])}</span>'
                f'<a href="{href}:1:1"><code>{html.escape(Path(r["path"]).name)}</code></a>'
                f'<span class="mg-quiet">{html.escape(str(r["revision"]))} ← '
                f'{html.escape(str(r["down"]))}</span>')
        if r["noDowngrade"]:
            head += ('<span class="mg-flag mg-amber" tabindex="0" data-tip="downgrade() does '
                     'nothing: rolling this back means writing it then">no downgrade</span>')
        head += "</div>"
        items = []
        for o in r["ops"]:
            flags = "".join(f'<span class="mg-flag {c}" tabindex="0" '
                            f'data-tip="{html.escape(why, quote=True)}">{html.escape(label)}</span>'
                            for c, label, why in o["flags"])
            items.append(f'<li><a href="{href}:{o["line"]}:1">{html.escape(o["text"])}</a>{flags}</li>')
        body = f'<ul class="mg-ops">{"".join(items)}</ul>' if items else \
            '<p class="mg-quiet">upgrade() makes no <code>op.*</code> call.</p>'
        out.append(f'<div class="mg-rev">{head}{body}</div>')
    out.append("</div>")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/develop")
    ap.add_argument("--out", help="the HTML fragment (default: stdout)")
    ap.add_argument("--json", action="store_true", help="the findings as JSON instead")
    args = ap.parse_args(argv)
    root = Path(git("rev-parse", "--show-toplevel").strip() or ".")
    dirs = versions_dirs()
    if not dirs:
        print("[migrations] no alembic versions folder in this repository", file=sys.stderr)
        return 3
    files = changed(args.base, dirs)
    revs, edited, deleted = [], [], []
    for status, path in files:
        if status == "D":
            deleted.append(path)
            continue
        if status == "M":
            edited.append(path)
        try:
            revs.append(revision(path, (root / path).read_text(encoding="utf-8")))
        except (OSError, SyntaxError) as e:
            print(f"[migrations] {path}: {e}", file=sys.stderr)
    new = [r for r in revs if r["path"] not in edited]
    moved = model_moved()
    if args.json:
        body = json.dumps({"revisions": revs, "edited": edited, "deleted": deleted,
                           "modelMoved": moved}, indent=1, default=str)
    else:
        body = render(new, edited, deleted, moved, root)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"[migrations] {len(new)} new, {len(edited)} edited, {len(deleted)} deleted "
              f"revision(s) -> {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
