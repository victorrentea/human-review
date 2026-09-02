#!/usr/bin/env python3
"""Diff two PlantUML *sequence* diagrams and render the delta in red.

The sibling `puml_diff.py` diffs diagrams whose meaning is a *set* of elements and
relationships (class, ER, package) — order carries no information there. A
sequence diagram is the opposite: it is an ordered script of messages, and the
same arrow appearing twice is two different events. So the two need different
algorithms, not one generalised one.

Given a previous snapshot (OLD) and a current one (NEW) — e.g. the diagram
committed on the base branch vs the one this branch's test run just produced —
emit a single merged diagram built on NEW, where:

  * added message / note / separator   -> red arrow + red label
  * removed message / note / separator -> red arrow + struck red label, re-inserted
                                          where it used to sit
  * added participant                  -> red-tinted lifeline box
  * removed participant                -> red-tinted box, struck name (kept, so the
                                          struck arrows still have something to point at)

`activate` / `deactivate` lines are the one thing deliberately *not* re-inserted
on the removal side: they are a balanced bracket language, and splicing an old
`activate` back into the new script unbalances it and makes PlantUML render an
error page instead of a diagram. Keeping NEW's activation bars verbatim keeps the
output renderable, and no reviewer reads a delta for its activation nesting.

Pure standard library — no third-party deps.

Usage:
    seq_puml_diff.py OLD.puml NEW.puml [--out merged.puml]
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys

RED = "#D40000"

# `Browser -> Backend: GET /api/vets`, `Backend --> Browser: 200`, `A <- B: x`.
ARROW_RE = re.compile(
    r"^(?P<src>[^-<>:]+?)\s*"
    r"(?P<arrow>-{1,2}>>?|<<?-{1,2})\s*"
    r"(?P<dst>[^-<>:]+?)\s*:\s*"
    r"(?P<text>.*)$"
)
SEPARATOR_RE = re.compile(r"^==\s*(?P<text>.*?)\s*==$")
NOTE_RE = re.compile(r"^(?P<head>note\s+(?:over|left|right)[^:]*):\s*(?P<text>.*)$", re.I)
PARTICIPANT_RE = re.compile(
    r"^(?P<kind>participant|actor|boundary|control|entity|database|collections|queue)\s+"
    r"(?P<rest>.+)$",
    re.I,
)
ACTIVATION_RE = re.compile(r"^(?:activate|deactivate|return|destroy)\b", re.I)
# Lines that describe the diagram rather than the conversation.
META_RE = re.compile(
    r"^(?:@startuml|@enduml|title\b|header\b|footer\b|hide\b|show\b|skinparam\b|"
    r"autonumber\b|scale\b|!.*)",
    re.I,
)


# PlantUML applies inline markup per rendered line: a <color>/<s> opened before a `\n`
# does not survive it, and its closing tag then prints as literal text on the last line.
# A folded SQL statement is exactly that case, so every line is wrapped on its own.
NEWLINE = "\\n"


def _per_line(text: str, wrap) -> str:
    return NEWLINE.join(wrap(part) for part in text.split(NEWLINE))


def _red(text: str) -> str:
    return _per_line(text, lambda part: f"<color:{RED}>{part}</color>")


def _struck(text: str) -> str:
    return _per_line(text, lambda part: f"<color:{RED}><s>{part}</s></color>")


# `[[target{tooltip} label]]` — an arrow whose whole label is the handle a reader clicks.
LINK = re.compile(r"^\[\[(?P<target>\S+?(?:\{[^}]*\})?)\s+(?P<label>.*?)\]\]$")


def _paint(text: str, removed: bool):
    """Mark one arrow's label as added or removed — around its link, never inside it.

    PlantUML renders no inline markup inside a link label: a `<color>` there prints as
    literal text *and* takes the link apart, so the arrow shows up as raw `[[…]]` source.
    Wrapping the link from outside is no better, because the wrap is applied per rendered
    line and a two-line label then has `[[` on one line and `]]` on the next.

    So an added arrow keeps its link and lets the red arrowhead carry the meaning, and a
    removed one gives the link up — its detail was never recorded in this diagram's
    sidecar, so the handle is dead either way — and strikes the words instead.
    """
    m = LINK.match(text.strip())
    if not m:
        return _struck(text) if removed else _red(text)
    if removed:
        return _struck(m.group("label").rstrip().removesuffix(MARKER_GLYPH).rstrip())
    return text


MARKER_GLYPH = "⊕"


def _colorize_arrow(arrow: str) -> str:
    """`->` -> `-[#red]>`, `-->` -> `-[#red]->`, `<-` -> `<[#red]-`."""
    if arrow.startswith("-"):
        return f"-[{RED}]{arrow[1:]}"
    if arrow.startswith("<"):
        return f"<[{RED}]{arrow[1:]}"
    return arrow


def _mark_line(line: str, removed: bool) -> str | None:
    """Re-render one body line as an addition (removed=False) or a removal.

    Returns None for lines that must not be re-emitted on the removal side.
    """
    stripped = line.strip()
    paint = _struck if removed else _red

    if ACTIVATION_RE.match(stripped):
        # See the module docstring: never re-inject old activation brackets.
        return None if removed else line

    m = ARROW_RE.match(stripped)
    if m:
        return (
            f"{m['src'].strip()} {_colorize_arrow(m['arrow'])} "
            f"{m['dst'].strip()}: {_paint(m['text'], removed)}"
        )

    m = SEPARATOR_RE.match(stripped)
    if m:
        return f"== {paint(m['text'])} =="

    m = NOTE_RE.match(stripped)
    if m:
        return f"{m['head']} {RED}: {paint(m['text'])}"

    # group / alt / opt / loop / end and anything unrecognised: PlantUML offers no
    # inline colour for these, so an addition passes through and a removal is
    # dropped rather than risking an unbalanced block.
    return None if removed else line


def _participant_name(rest: str) -> str:
    """`"Long Name" as X` -> X; `Backend` -> Backend."""
    m = re.search(r"\bas\s+(\S+)\s*$", rest, re.I)
    if m:
        return m.group(1)
    return rest.split("#")[0].strip().strip('"')


def _mark_participant(line: str, removed: bool) -> str:
    m = PARTICIPANT_RE.match(line.strip())
    if not m:
        return line
    rest = m["rest"].strip()
    alias = _participant_name(rest)
    label = rest.split(" as ")[0].strip().strip('"') if " as " in rest else alias
    shown = _struck(label) if removed else _red(label)
    return f'{m["kind"]} "{shown}" as {alias} #FFEBEB'


# `[[<target>{<tooltip>} <label>]]` — anywhere on a line, arrow label or section header.
# The target is excluded from carrying `{`, so the tooltip cannot be mistaken for part
# of it and swallow the label with it.
ANY_LINK = re.compile(r"\[\[(?P<target>[^\s{\]]+)(?:\{[^}]*\})?(?:\s+(?P<label>[^\]]*))?\]\]")
# The same, narrowed to the handles that point into the checkout.
SRC_LINK = re.compile(r"\[\[(?P<target>src://[^\s{\]]*)(?:\{[^}]*\})?(?:\s+(?P<label>[^\]]*))?\]\]")


def _link_label(m: "re.Match[str]") -> str:
    """What a `[[…]]` puts on the picture — the words, never the handle behind them."""
    return m.group("label") or ""


def _as_read(line: str) -> str:
    """The line as the reader sees it, with every handle behind it dropped.

    Two things move under a stable picture. A `genseq://` id fingerprints the payload the
    arrow hides, and a `src://` handle carries the file:line the test happens to sit on —
    a header gains one the moment the generator learns to link a section to its test, and
    every line below an edit shifts. Neither is part of the conversation, so neither may
    decide whether two lines are *the same line*: matching on the raw text reports one
    notation change as a deletion with its replacement underneath, which is the same fact
    told twice and twice as much red.
    """
    return ANY_LINK.sub(_link_label, line.strip())


def _as_meant(line: str) -> str:
    """The line minus only the handles that say nothing — `src://` file:line.

    Coarser than `_as_read` on purpose: a moved `genseq://` id *is* news (the statement
    behind the arrow was rewritten) and still earns a red arrowhead, while a section
    header that merely learned where its test lives has not changed at all.
    """
    return SRC_LINK.sub(_link_label, line.strip())


def _split(puml: str):
    """-> (meta, participant_lines, body_lines)"""
    meta, participants, body = [], [], []
    for raw in puml.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("'"):
            continue
        if META_RE.match(stripped):
            meta.append(line)
        elif PARTICIPANT_RE.match(stripped):
            participants.append(line)
        else:
            body.append(line)
    return meta, participants, body


def diff(old: str, new: str) -> str:
    old_meta, old_parts, old_body = _split(old)
    new_meta, new_parts, new_body = _split(new)

    out = [line for line in new_meta if line.strip().lower() != "@enduml"]
    if not out or not out[0].strip().lower().startswith("@startuml"):
        out.insert(0, "@startuml")

    # ── participants: NEW's order, then the ones this change dropped ──────────
    new_by_alias = {
        _participant_name(PARTICIPANT_RE.match(p.strip())["rest"]): p
        for p in new_parts
        if PARTICIPANT_RE.match(p.strip())
    }
    old_by_alias = {
        _participant_name(PARTICIPANT_RE.match(p.strip())["rest"]): p
        for p in old_parts
        if PARTICIPANT_RE.match(p.strip())
    }
    for alias, line in new_by_alias.items():
        out.append(_mark_participant(line, removed=False) if alias not in old_by_alias else line)
    for alias, line in old_by_alias.items():
        if alias not in new_by_alias:
            out.append(_mark_participant(line, removed=True))

    # ── body: an ordered script, so an ordered diff ──────────────────────────
    matcher = difflib.SequenceMatcher(
        a=[_as_read(l) for l in old_body], b=[_as_read(l) for l in new_body], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal",):
            # Equal as *read* — but an arrow whose detail id moved is the same call over a
            # statement this change rewrote. That earns a red label, not a strikeout: what
            # the reviewer is being shown is still there, and only what it hides differs.
            for old_line, new_line in zip(old_body[i1:i2], new_body[j1:j2]):
                changed = _as_meant(old_line) != _as_meant(new_line)
                out.append(_mark_line(new_line, removed=False) or new_line if changed else new_line)
            continue
        if tag in ("replace", "delete"):
            for line in old_body[i1:i2]:
                marked = _mark_line(line, removed=True)
                if marked is not None:
                    out.append(marked)
        if tag in ("replace", "insert"):
            for line in new_body[j1:j2]:
                marked = _mark_line(line, removed=False)
                if marked is not None:
                    out.append(marked)

    out.append("@enduml")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("old", help='previous snapshot (.puml); empty file means "brand new diagram"')
    ap.add_argument("new", help="current snapshot (.puml)")
    ap.add_argument("--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    with open(args.old, encoding="utf-8") as f:
        old = f.read()
    with open(args.new, encoding="utf-8") as f:
        new = f.read()

    merged = diff(old, new)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(merged)
    else:
        sys.stdout.write(merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
