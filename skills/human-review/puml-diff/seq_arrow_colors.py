#!/usr/bin/env python3
"""Paint a sequence diagram's arrows by what crosses: a network, the database, or nothing.

    seq_arrow_colors.py <in.puml> <out.puml>

Three kinds of message, three strokes (Victor, 11 Nov 2026):

- **between systems** (`Browser -> Backend`, `Backend -> NotificationService`, and their
  replies) — `NETWORK`;
- **to or from the database** (`Backend -> DB: select owners`) — `SQL`;
- **inside one process** (`Backend -> Backend`, a call into a `«module»` library, a
  found message `[-> Browser`) — left as PlantUML draws it.

A lifeline is the database when it is declared `database`, is named like one (`DB`,
`Database`, `Postgres`…), or receives an arrow whose link is a traced statement
(`{Click for the statement behind this call}`). A lifeline is a library when its label
carries `«module»`. Every other lifeline is a system of its own, so an arrow between two
different ones crossed a wire.

An arrow that already names a colour (`-[#…]->`) keeps it: a generator that chose one
chose it on purpose. Only the undiffed New/Old renders go through this; the delta keeps
its green and red, which are the one thing a delta is for.
"""
from __future__ import annotations

import re
import sys

#: Kept in step with `hrbuild/shared/svg.py`'s DIAGRAM_COLOR_VARS, which gives each a
#: dark-mode value of its own (test_diagram_dark_mode.py checks it).
NETWORK = "#6A1B9A"
SQL = "#E65100"

DECL = re.compile(
    r'^\s*(?P<kw>participant|actor|database|queue|collections|boundary|control|entity)\s+'
    r'(?P<first>"[^"]*"|\S+)(?:\s+as\s+(?P<second>"[^"]*"|\S+))?')
#: `A -> B: x`, `A --> B: x`, `A ->> B`, `A <- B`. `[` / `]` (the diagram's edge) never
#: match a lifeline, so found and lost messages are left alone.
ARROW = re.compile(
    r'^(?P<lead>\s*)(?P<a>"[^"]*"|[^\s"<>\[\]:-]+)\s*'
    r'(?P<arrow><?-+(?:\[[^\]]*\])?-*>{0,2}|<{1,2}-+)'
    r'\s*(?P<b>"[^"]*"|[^\s"<>:\[\]-]+)\s*(?P<rest>:.*)?$')
DB_NAME = re.compile(r"^(db|database|postgres(ql)?|oracle|mysql|sql|.*\bdb)$", re.I)
SQL_LINK = "{Click for the statement behind this call}"


def _key(token: str) -> str:
    return token.strip().strip('"')


def colour(text: str) -> str:
    lines = text.splitlines()
    alias, kinds = {}, {}
    for line in lines:
        m = DECL.match(line)
        if not m:
            continue
        first, second = m["first"], m["second"]
        # `participant "label" as Alias` and `participant Alias as "label"` both exist.
        name, label = (second, first) if second and first.startswith('"') else (first, second or first)
        key = _key(name)
        alias[_key(label)] = key
        if m["kw"] == "database" or DB_NAME.match(key) or DB_NAME.match(_key(label)):
            kinds[key] = "db"
        elif "«module»" in label or "<<module>>" in label:
            kinds[key] = "module"
    for line in lines:
        m = ARROW.match(line)
        if m and SQL_LINK in (m["rest"] or ""):
            kinds[alias.get(_key(m["b"]), _key(m["b"]))] = "db"

    def kind_of(token: str) -> str:
        key = alias.get(_key(token), _key(token))
        return kinds.get(key) or ("db" if DB_NAME.match(key) else "system")

    out = []
    for line in lines:
        m = ARROW.match(line)
        if not m or "[" in m["arrow"]:
            out.append(line)
            continue
        a, b = alias.get(_key(m["a"]), _key(m["a"])), alias.get(_key(m["b"]), _key(m["b"]))
        ka, kb = kind_of(m["a"]), kind_of(m["b"])
        if "db" in (ka, kb) and a != b:
            hue = SQL
        elif a != b and ka == kb == "system":
            hue = NETWORK
        else:
            out.append(line)
            continue
        arrow = m["arrow"]
        # PlantUML takes the colour right after the first dash: `-[#hex]->`, `-[#hex]-->`.
        i = arrow.index("-")
        arrow = arrow[:i + 1] + f"[{hue}]" + arrow[i + 1:]
        out.append(f'{m["lead"]}{m["a"]} {arrow} {m["b"]}{m["rest"] or ""}')
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    src = open(argv[1], encoding="utf-8").read()
    with open(argv[2], "w", encoding="utf-8") as f:
        f.write(colour(src))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
