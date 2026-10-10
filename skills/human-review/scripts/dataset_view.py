"""The Demo tab's DB Fixture card: the rows behind each fixture, under its own header.

The fixtures say *that* a dataset exists — Default, green — and a reviewer had to open
R__seed.sql and green.sql and read INSERT … SELECT … JOIN to learn *what* is in it
(Victor, 7 Oct 2026). This computes the tables, at build time and with no app running, so
each fixture's header can open onto them as small grids (`fixtures_panel_html` draws the
card, `hrbuild/assets/dataset-view.js` fills it).

The source is the project's own SQL, the same files the environment loads:

  * the schema — Flyway's versioned migrations (`db/migration/V*.sql`), in version order;
  * the seed — Flyway's repeatable scripts (`R__*.sql`), which run after every version;
  * the fixtures — `db/fixtures/<name>.sql`, each a dataset of its own, run on the empty
    schema without the seed, exactly as the reset sidecar does it.

They are *executed*, not parsed: a fixture that writes `INSERT … SELECT … FROM (VALUES …)
JOIN owners …` has no rows in it to read, only rows it computes. The executor is an
in-memory SQLite, fed the Postgres dialect through a handful of rewrites (`_to_sqlite`):
enough for seed files, which are INSERTs and UPDATEs of literals. A statement it cannot
run is skipped and named in `skipped`, never fatal — a partial view still beats none.

Foreign keys come off the same schema: SQLite's own `foreign_key_list` for the ones declared
inline, plus the `ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY` ones SQLite cannot execute,
read off the statement. They order the tables: the most outgoing keys first, because a
table that points at many others (visits → pets, vets) is the one a reader needs to see
the dataset's shape, and the lookup tables it points at are the footnotes.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import subprocess
from pathlib import Path

#: Rows kept per table. The view is for reading, not for paging a 100k-row table; the true
#: count is still reported.
MAX_ROWS = 300

_SKIP = re.compile(r"^\s*(?:CREATE\s+(?:UNIQUE\s+)?INDEX|CREATE\s+(?:SEQUENCE|EXTENSION|TYPE|"
                   r"FUNCTION|TRIGGER|VIEW|SCHEMA)|ALTER\s+SEQUENCE|COMMENT\s+ON|SET\s|GRANT\s|"
                   r"REVOKE\s|DO\s|BEGIN\b|COMMIT\b|START\s+TRANSACTION|SELECT\s+(?:pg_catalog\.)?"
                   r"setval|ANALYZE\b|VACUUM\b|REFRESH\b)", re.I)
_ALTER_FK = re.compile(r"^\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?([\w.\"]+)\s+ADD\s+"
                       r"(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+"
                       r"([\w.\"]+)", re.I)


def _name(s: str) -> str:
    return s.strip().strip('"').split(".")[-1].strip('"')


def split_sql(text: str) -> list[str]:
    """Statements, with comments gone and `;` inside a literal or a `$$` body left alone."""
    out, cur, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"\$\w*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                cur.append(text[i:j])
                i = j
                continue
        if c == ";":
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    stmt = "".join(cur).strip()
    if stmt:
        out.append(stmt)
    return out


def _close(code: str, open_at: int) -> int:
    depth = 0
    for k in range(open_at, len(code)):
        if code[k] == "(":
            depth += 1
        elif code[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return -1


def _values_alias(code: str) -> str:
    """`(VALUES …) AS p(a, b)` → `(SELECT column1 AS a, column2 AS b FROM (VALUES …)) AS p`:
    SQLite names a VALUES list's columns column1, column2…, and has no column alias list."""
    pos = 0
    while True:
        m = re.compile(r"\(\s*VALUES\b", re.I).search(code, pos)
        if not m:
            return code
        end = _close(code, m.start())
        if end < 0:
            return code
        alias = re.compile(r"\s*(?:AS\s+)?(\w+)\s*\(([^()]*)\)").match(code, end + 1)
        if not alias:
            pos = end
            continue
        cols = [c.strip() for c in alias.group(2).split(",") if c.strip()]
        sel = ", ".join(f"column{k} AS {c}" for k, c in enumerate(cols, 1))
        repl = f"(SELECT {sel} FROM {code[m.start():end + 1]}) AS {alias.group(1)}"
        code = code[:m.start()] + repl + code[alias.end():]
        pos = m.start() + len(repl)


def _to_sqlite(stmt: str) -> list[str]:
    """One Postgres statement as the SQLite statements that do the same to the rows."""
    lits: list[str] = []

    def keep(m):
        lits.append(m.group(0))
        return f"\x00{len(lits) - 1}\x00"
    code = re.sub(r"'(?:[^']|'')*'", keep, stmt)
    code = re.sub(r"\bpublic\.", "", code)
    code = re.sub(r"\b(?:DATE|TIME|TIMESTAMP|TIMESTAMPTZ)\s+(\x00\d+\x00)", r"\1", code, flags=re.I)
    code = re.sub(r"::\s*\w+(?:\s*\(\d+(?:,\s*\d+)?\))?(?:\[\])?", "", code)
    code = re.sub(r"\b(?:SMALLINT|INT|INTEGER|BIGINT|INT[248])\s+GENERATED\s+(?:BY\s+DEFAULT|ALWAYS)"
                  r"\s+AS\s+IDENTITY(?:\s*\([^)]*\))?\s+PRIMARY\s+KEY", "INTEGER PRIMARY KEY",
                  code, flags=re.I)
    code = re.sub(r"\b(?:BIG)?SERIAL\s+PRIMARY\s+KEY", "INTEGER PRIMARY KEY", code, flags=re.I)
    code = re.sub(r"\bALTER\s+TABLE\s+ONLY\b", "ALTER TABLE", code, flags=re.I)
    code = _values_alias(code)
    t = re.match(r"\s*TRUNCATE\s+(?:TABLE\s+)?(.*)$", code, re.I | re.S)
    if t:
        body = re.sub(r"\b(?:RESTART|CONTINUE)\s+IDENTITY\b|\b(?:CASCADE|RESTRICT)\b", "",
                      t.group(1), flags=re.I)
        out = [f"DELETE FROM {_name(x)}" for x in body.split(",") if x.strip()]
    else:
        out = [code]
    return [re.sub(r"\x00(\d+)\x00", lambda m: lits[int(m.group(1))], s) for s in out]


def _run(db: sqlite3.Connection, text: str, label: str, fks: dict, skipped: list) -> None:
    for stmt in split_sql(text):
        fk = _ALTER_FK.match(stmt)
        if fk:
            src, dst = _name(fk.group(1)), _name(fk.group(3))
            for col in fk.group(2).split(","):
                fks.setdefault(src, {})[_name(col)] = dst
            continue
        if _SKIP.match(stmt):
            continue
        for s in _to_sqlite(stmt):
            try:
                db.execute(s)
            except sqlite3.Error as e:
                skipped.append({"file": label, "sql": " ".join(stmt.split())[:160], "error": str(e)})
                break


def _version(p: Path) -> tuple:
    m = re.match(r"V([\d._]+)__", p.name)
    return tuple(int(x) for x in re.split(r"[._]", m.group(1)) if x) if m else ()


def sources(root: Path) -> dict:
    """The schema, seed and fixture files of `root`, the ones git tracks (so nothing under
    node_modules, target or a virtualenv), in the order they apply."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "*.sql"], capture_output=True, text=True)
    files = [root / f for f in r.stdout.split()] if r.returncode == 0 else []
    if not files:
        files = [p for p in root.rglob("*.sql")
                 if not any(x in p.parts for x in ("node_modules", "target", "build", ".venv"))]
    versioned = sorted((p for p in files if re.match(r"V[\d._]+__.*\.sql$", p.name)
                        and "migration" in str(p.parent)), key=_version)
    repeatable = sorted(p for p in files if re.match(r"R__.*\.sql$", p.name))
    fixtures = sorted(p for p in files if p.parent.name == "fixtures")
    return {"schema": versioned, "seed": repeatable, "fixtures": fixtures}


def _load(src: dict, fixture: Path | None, root: Path, fks: dict, skipped: list):
    """The schema, then the seed — or, for a fixture, the fixture alone on the empty tables."""
    db = sqlite3.connect(":memory:")
    data = src["seed"] if fixture is None else [fixture]
    for p in src["schema"] + data:
        _run(db, p.read_text(encoding="utf-8", errors="replace"),
             str(p.relative_to(root)), fks, skipped)
    return db


def _snapshot(db: sqlite3.Connection) -> dict:
    out = {}
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        info = db.execute(f'PRAGMA table_info("{t}")').fetchall()
        cols = [r[1] for r in info]
        boolean = [i for i, r in enumerate(info) if "BOOL" in (r[2] or "").upper()]
        rows = []
        for row in db.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            row = list(row)
            for i in boolean:
                if row[i] in (0, 1):
                    row[i] = bool(row[i])
            rows.append(row)
        out[t] = {"cols": cols, "rows": rows}
    return out


def build(root: Path) -> dict | None:
    """The dataset blob the page embeds, or None when the project has no seed to show."""
    root = Path(root)
    src = sources(root)
    if not src["schema"] or not (src["seed"] or src["fixtures"]):
        return None
    fks: dict = {}
    skipped: list = []
    db = _load(src, None, root, fks, skipped)
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for r in db.execute(f'PRAGMA foreign_key_list("{t}")'):
            fks.setdefault(t, {})[r[3]] = r[2]
    seed = _snapshot(db)
    db.close()
    if not seed:
        return None
    incoming = {t: 0 for t in seed}
    for t, m in fks.items():
        for dst in set(m.values()):
            if dst in incoming and dst != t:
                incoming[dst] += 1
    out_n = {t: len(set(fks.get(t, {}).values())) for t in seed}
    order = sorted(seed, key=lambda t: (-out_n[t], -incoming[t], t))
    sets = {"": {t: _cap(seed[t]["rows"]) for t in seed}}
    counts = {"": {t: len(seed[t]["rows"]) for t in seed}}
    # Every table of a fixture, the empty ones too: it starts from nothing, so a table it
    # does not write is empty, never the seed's.
    for f in src["fixtures"]:
        db = _load(src, f, root, {}, skipped)
        snap = _snapshot(db)
        db.close()
        rows = {t: snap.get(t, {"rows": []})["rows"] for t in order}
        sets[f.stem] = {t: _cap(rows[t]) for t in order}
        counts[f.stem] = {t: len(rows[t]) for t in order}
    tables = [{"name": t, "cols": seed[t]["cols"], "fk": fks.get(t, {}),
               "in": incoming[t]} for t in order]
    return {"tables": tables, "sets": sets, "counts": counts,
            "sources": [str(p.relative_to(root)) for g in ("schema", "seed", "fixtures")
                        for p in src[g]],
            "skipped": skipped}


def _cap(rows: list) -> list:
    return [[v if isinstance(v, (int, float, bool)) or v is None else str(v) for v in r]
            for r in rows[:MAX_ROWS]]


def fixtures_panel_html(fixtures: list | None, can_reset: bool, info: str = "") -> str:
    """The Demo tab's "DB Fixture" panel: its own card under the Running app one (Victor,
    9 Oct 2026 — the fixtures used to be a second row inside that band), one fixture per
    line, one under the other, so two seeds can be compared top to bottom.

    Each line is a header — a disclosure caret, the fixture's dot, its name, and its
    **Seed** — and `dataset-view.js` draws that fixture's tables into the band under it
    when the caret is opened, from rows computed at build time (`dataset_view.py`). Any
    number can be open at once. A fixture the data does not know keeps a caret that says
    so and opens nothing.

    It lives here and not in `hrbuild/shared/fixtures.py` because it is this view's frame:
    the panel and the script that fills it are one piece.

    The class names are the ones APP_ENV_JS arms the Seed buttons by (`.appenv-fixtures`,
    `.appenv-fx`, `.appenv-reset`), and the ones `fixtures_row_html` drew before: the Seed
    contract did not change, only where it sits. `info` is the section's (i), or "".

    `fixtures` is `[(name, colour), …]` with the seed as `""`; None is "the seed only"."""
    from hrbuild.shared.commands import SEED_OFFLINE_TIP
    from hrbuild.shared.fixtures import FIXTURE_SEED
    items = list(fixtures) if fixtures else [("", FIXTURE_SEED)]

    def item(name: str, colour: str) -> str:
        label = name or "Default"
        key = html.escape(name)
        seed = (f'<button type="button" class="appenv-reset" data-fixture="{key}"'
                f' aria-disabled="true" aria-label="Seed the DB with {html.escape(label)}"'
                f' data-tip="{SEED_OFFLINE_TIP}">Seed</button>') if can_reset else ""
        body = f"dbfx-body-{re.sub(r'[^a-z0-9-]', '', name) or 'default'}"
        return (f'<div class="appenv-fx" data-fixture="{key}"><div class="dbfx-row">'
                f'<button type="button" class="dbfx-tog" data-fixture="{key}"'
                f' aria-expanded="false" aria-controls="{body}">'
                '<span class="disclose" aria-hidden="true"></span>'
                f'<span class="fx-dot" aria-hidden="true" style="--fx:{html.escape(colour)}">'
                f'</span><span class="appenv-fx-name">{html.escape(label)}</span></button>'
                f'{seed}<span class="dbfx-sum"></span></div>'
                f'<div class="dbfx-body" id="{body}" hidden></div></div>')

    head = ('<div class="dbfx-head adopthead"><span class="dbfx-title appenv-fixtures-to" '
            'data-tip="The datasets the demo DB can be reset to: the seed, or the seed plus '
            'a fixture">DB Fixture</span>'
            + (f'<div class="adoptline adopt-in">{info}</div>' if info else "") + '</div>')
    return ('<div class="dbfx">' + head
            + '<div class="appenv-fixtures" role="group" aria-label="DB fixtures">'
            + "".join(item(n, c) for n, c in items) + '</div></div>')


_ASSETS = Path(__file__).resolve().parent / "hrbuild" / "assets"


def dataset_html(root: Path | None) -> str:
    """The blob and the script that fills the DB Fixture card's headers from it; nothing
    when the project has no seed. The markup is the script's to write, so the page a live patch
    produces and the page a build produces are one and the same."""
    if root is None:
        return ""
    try:
        data = build(root)
    except (OSError, sqlite3.Error):
        return ""
    if not data:
        return ""
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    # The rules are the Demo tab's (`css/demo.css`); the script is the tab's alone, so it
    # rides here with its data rather than on every page the build writes.
    js = (_ASSETS / "dataset-view.js").read_text(encoding="utf-8")
    return (f'<script type="application/json" id="dsv-data">{blob}</script>'
            f"<script>\n{js}</script>")


if __name__ == "__main__":  # python3 dataset_view.py <repo> — the blob, for a look
    import sys
    print(json.dumps(build(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=1)[:4000])
