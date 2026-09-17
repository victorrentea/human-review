#!/usr/bin/env python3
"""`review-points.md` — what the agent fixed, declined and assumed — read into the three
piles the Review tab already renders.

The page has always been able to show what a review *found*: the passes leave findings in
the transcript and `review-passes.py` harvests them. What it could never show is what the
agent did with them — which finding it accepted, which it read and declined, and on what
grounds — because that decision is made in a conversation and then lost. Nor could it show
the readings the agent chose where the ticket was ambiguous, which are not in the diff at
all and are not findings: nobody found them, somebody decided them.

`review-points.md` is that record, written by the agent while it still has it, committed
with the fixes, and therefore visible in the PR's own file list. This script turns it into
`.human-review/review-points.json`, whose `findings` / `autofixes` / `assumptions` arrays
are exactly the shapes `build-review-html.py` already reads (`reference/content-schema.md`).
No renderer changes: the file is a new *source* for the piles, not a new pile.

Two decisions worth stating, because both are load-bearing:

* **Hand-rolled, no PyYAML.** The frontmatter is `key: value` lines and nothing else. A YAML
  parser would accept nine spellings of the same thing plus one spelling of something subtly
  different, and would be a dependency on the machine of every trainee who installs this.
* **Strict and loud, never lenient and quiet.** An unknown section heading, an unknown field
  key, a field after the prose: all hard errors, because a pile the parser skipped reads on
  the page exactly like a pile nobody wrote, and those are the two things a reviewer most
  needs told apart. `--check` runs the same parse and writes nothing, so the agent can be
  told to validate the file *before* committing it.

Exit codes:  0 parsed · 3 no such file · 4 unparseable · 5 present, and every item
unanchored — a file that says nothing, reported as that rather than as a clean review.

Usage:
  review-points.py --check                    # validate, print what was understood
  review-points.py                            # write .human-review/review-points.json
  review-points.py --root ../repo --file docs/review-points.md --out /tmp/rp.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

DEFAULT_FILE = "review-points.md"
CONFIG = "human-review.json"
DEFAULT_OUT = ".human-review/review-points.json"

# The three piles, by every heading that means one of them. `Fixed` lands in `autofixes`
# and `Ignored` in `findings` because that is what those two arrays already mean to the
# renderer: an autofix is a defect that was repaired and shows its diff, a finding is one
# still standing in front of the reader.
SECTIONS = {
    "fixed": "autofixes", "repaired": "autofixes", "applied": "autofixes",
    "ignored": "findings", "rejected": "findings", "declined": "findings",
    "not fixed": "findings",
    "assumptions": "assumptions", "assumed": "assumptions",
}
PILE_HEADING = {"autofixes": "Fixed", "findings": "Ignored", "assumptions": "Assumptions"}

FIELDS = {"file", "source", "severity", "alternative", "why", "fixed-in"}
SEVERITIES = {"high", "medium", "low", "info"}

H2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")
H3 = re.compile(r"^###\s+(.*?)\s*#*\s*$")
FIELD = re.compile(r"^[-*]\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
FRONT_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
# A ref with a line or a line range on the end — the difference between "this file" and a
# card showing those lines. `path:12` and `path:12-30` count; a bare path does not, and
# neither does a Windows drive letter or a URL, which is why the tail has to be all digits.
RANGED = re.compile(r":\d+(?:-\d+)?$")
CODE_SPAN = re.compile(r"`([^`]+)`")


class Unparseable(Exception):
    """The file is present and is not this format. Never downgraded to a warning: a
    silently skipped section is the one failure this whole file exists to prevent."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def config_path(root: Path) -> str | None:
    """`"reviewPoints"` from the repo's own `human-review.json`, if it has an opinion."""
    p = root / CONFIG
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = data.get("reviewPoints") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def inline(text: str) -> str:
    """Prose as the page can render it: escaped, with backticks as `<code>`.

    Everything is escaped first, so a `<` an agent typed in prose reaches the reader as a
    `<` instead of as markup — and the `{{snippet:…}}` / `{{diff:…}}` tokens survive it
    untouched, because they contain no character escaping touches. Paragraph breaks become
    `<br><br>`: the renderer wraps a body in a single `<p>`, so the alternative is a wall.
    """
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        one = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if one:
            paragraphs.append(CODE_SPAN.sub(
                lambda m: f"<code>{html.escape(m.group(1))}</code>", html.escape(one)))
    return "<br><br>".join(paragraphs)


def parse_front(lines: list[str], problems: list[str]) -> tuple[dict, int]:
    """The `---` fenced `key: value` block, and the line the body starts on."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return {}, 0
    front: dict[str, str] = {}
    for n in range(i + 1, len(lines)):
        raw = lines[n]
        if raw.strip() == "---":
            return front, n + 1
        if not raw.strip():
            continue
        m = FRONT_LINE.match(raw.strip())
        if not m:
            problems.append(f"line {n + 1}: frontmatter is `key: value` lines only, "
                            f"got {raw.strip()[:60]!r}")
            continue
        front[m.group(1).strip().lower()] = m.group(2).strip()
    problems.append("the frontmatter fence was opened with `---` and never closed")
    return front, len(lines)


def split_ref(value: str) -> tuple[str, str | None]:
    """`path:12-30 | caption` → `("path:12-30", "caption")`."""
    if "|" in value:
        ref, caption = value.split("|", 1)
        return ref.strip(), caption.strip() or None
    return value.strip(), None


def build_item(title: str, fields: list[tuple[str, str]], body: str, pile: str,
               front: dict, problems: list[str], where: int) -> dict:
    """One `### …` block as the renderer wants it.

    `refs` and `snippets` both come from `file:` — a ref that names lines earns a card, a
    ref that names only a file earns a link — so the agent writes one thing and the page
    decides how much room to give it.
    """
    item: dict = {"title": inline(title)}
    refs: list[str] = []
    snippets: list[dict] = []
    seen: set[str] = set()
    fixed_in = None
    for key, value in fields:
        if key not in FIELDS:
            problems.append(f"line {where}: {title[:40]!r} has an unknown field "
                            f"`{key}:` — valid fields are "
                            f"{', '.join(sorted(FIELDS))}")
            continue
        if key != "file" and key in seen:
            problems.append(f"line {where}: {title[:40]!r} sets `{key}:` twice")
            continue
        seen.add(key)
        if key == "file":
            ref, caption = split_ref(value)
            if not ref:
                problems.append(f"line {where}: {title[:40]!r} has an empty `file:`")
                continue
            refs.append(ref)
            if RANGED.search(ref):
                snippets.append({"ref": ref, **({"caption": caption} if caption else {})})
            elif caption:
                problems.append(f"line {where}: {title[:40]!r} captions `{ref}`, which "
                                "names no lines — a caption belongs to a snippet card, "
                                "and a whole-file ref does not get one")
        elif key == "severity":
            sev = value.strip().lower()
            if pile == "assumptions":
                # Deliberately fatal, not coerced away. An assumption is not a defect; a
                # page that ranked one would be inviting the reader to treat a decision
                # they are being asked to confirm as a bug somebody left in.
                problems.append(f"line {where}: {title[:40]!r} is an assumption and "
                                "carries `severity:` — an assumption is not a defect and "
                                "must not be ranked as one. Drop the field.")
            elif sev not in SEVERITIES:
                problems.append(f"line {where}: {title[:40]!r} has severity {sev!r} — "
                                f"one of {', '.join(sorted(SEVERITIES))}")
            else:
                item["severity"] = sev
        elif key == "fixed-in":
            fixed_in = value.strip()
        else:
            item[key] = inline(value)

    if pile == "findings" and "severity" not in item:
        # Declined, and nobody said how bad. `info` is the honest default: the pile is
        # "somebody looked at this and said no", and inventing a rank for it would put a
        # number on the page that no reviewer ever typed.
        item["severity"] = "info"
    if pile == "assumptions":
        item.setdefault("source", "assumption")
    if body.strip():
        item["body"] = inline(body)
    if refs:
        item["refs"] = refs
    if snippets:
        item["snippets"] = snippets
    if fixed_in:
        base = (front.get("implementation") or "").strip()
        diffs = []
        for ref in refs or []:
            path = re.sub(RANGED, "", ref)
            d: dict = {"path": path}
            if base:
                d["base"] = base
            # `fixed-in: HEAD` means "the fix is in the current head", so the head side is
            # left as the working tree: that keeps the editor link, which always compares
            # against the working tree and is dropped from a pinned diff. Any other value
            # is a rev the reader is being pointed at, so it is pinned.
            if fixed_in.upper() != "HEAD":
                d["head"] = fixed_in
            diffs.append(d)
        if diffs:
            item["diffs"] = diffs
        else:
            problems.append(f"line {where}: {title[:40]!r} says `fixed-in: {fixed_in}` "
                            "but names no `file:` — there is nothing to diff")
    item["_fixed_in"] = fixed_in
    item["_line"] = where
    return item


def parse(text: str) -> dict:
    """The whole file. Raises `Unparseable` with every problem, not just the first."""
    lines = text.splitlines()
    problems: list[str] = []
    front, start = parse_front(lines, problems)

    piles: dict[str, list[dict]] = {"findings": [], "autofixes": [], "assumptions": []}
    seen_sections: dict[str, str] = {}
    pile: str | None = None
    title: str | None = None
    fields: list[tuple[str, str]] = []
    body: list[str] = []
    at = 0
    in_body = False
    fenced = False

    def close() -> None:
        nonlocal title, fields, body, in_body
        if title is not None and pile is not None:
            piles[pile].append(build_item(title, fields, "\n".join(body), pile, front,
                                          problems, at))
        title, fields, body, in_body = None, [], [], False

    for n in range(start, len(lines)):
        raw = lines[n]
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            if title is not None:
                in_body = True
                body.append(raw)
            continue
        if fenced:
            if title is not None:
                body.append(raw)
            continue

        m2 = H2.match(raw)
        if m2:
            close()
            name = m2.group(1).strip().lower().rstrip(":")
            target = SECTIONS.get(name)
            if target is None:
                problems.append(
                    f"line {n + 1}: unknown section '## {m2.group(1).strip()}' — this "
                    f"file has exactly three piles: {', '.join(PILE_HEADING.values())} "
                    f"(aliases: {', '.join(sorted(SECTIONS))}). A section nobody reads "
                    "looks the same on the page as a section nobody wrote.")
                pile = None
                continue
            if target in seen_sections:
                problems.append(f"line {n + 1}: '## {m2.group(1).strip()}' repeats the "
                                f"{PILE_HEADING[target]} pile, already opened as "
                                f"'{seen_sections[target]}'")
            seen_sections.setdefault(target, m2.group(1).strip())
            pile = target
            continue

        m3 = H3.match(raw)
        if m3:
            close()
            if pile is None:
                problems.append(f"line {n + 1}: '### {m3.group(1).strip()[:40]}' is not "
                                "under any of the three sections — an item outside a "
                                "pile has nowhere to be rendered")
                continue
            title, at = m3.group(1).strip(), n + 1
            continue

        if title is None:
            continue

        mf = FIELD.match(stripped)
        if mf and not in_body:
            fields.append((mf.group(1).strip().lower(), mf.group(2).strip()))
            continue
        if mf and mf.group(1).strip().lower() in FIELDS:
            # A known field after the prose has started. Swallowing it into the body would
            # lose a ref — and losing a ref is what gets the item dropped by the anchoring
            # rule, so the failure would surface as a missing item, three steps away.
            problems.append(f"line {n + 1}: `{mf.group(1).strip().lower()}:` comes after "
                            f"the prose of {title[:40]!r} — every field goes directly "
                            "under the `###` line, before the body")
            continue
        if stripped or body:
            in_body = True
            body.append(raw)
    close()

    if not seen_sections and not problems:
        problems.append(
            "no '## Fixed' / '## Ignored' / '## Assumptions' section anywhere — this is "
            "prose, not review-points.md. See reference/review-points.md.")
    if problems:
        raise Unparseable(problems)

    return {"front": front, "piles": piles,
            "sections": {PILE_HEADING[k]: v for k, v in seen_sections.items()}}


def anchored(item: dict) -> bool:
    return bool(item.get("refs") or item.get("snippets") or item.get("diffs"))


def drop_unanchored(piles: dict[str, list[dict]]) -> tuple[int, list[str]]:
    """The rule the build has always applied to assumptions, applied to all three piles.

    An item a reader cannot go and look at is indistinguishable from one that was never
    true, and this file is written by the same model whose work it describes — so the
    anchor is not a formatting preference, it is the only thing separating a record from
    a recollection of a record.
    """
    warnings: list[str] = []
    dropped = 0
    for key, items in piles.items():
        kept = []
        for item in items:
            if anchored(item):
                kept.append(item)
                continue
            dropped += 1
            warnings.append(
                f"{PILE_HEADING[key]}: {item.get('title', '')[:60]!r} (line "
                f"{item.get('_line')}) names no code — dropped. Give it a `- file: "
                "path:line`; an item nobody can go and look at says nothing.")
        piles[key] = kept
    return dropped, warnings


def top_fixed_in(front: dict, piles: dict[str, list[dict]]) -> str | None:
    """Where the fixes landed, when the file gives one answer.

    Frontmatter wins; otherwise the items' own `fixed-in` do, but only if they agree. Two
    different revs is a real thing to say and not one this key can say, so it says nothing
    rather than picking the first.
    """
    if front.get("fixed-in"):
        return front["fixed-in"].strip()
    values = {i["_fixed_in"] for items in piles.values() for i in items if i.get("_fixed_in")}
    return values.pop() if len(values) == 1 else None


def document(path: Path, rel: str) -> dict:
    """The parsed file as the build reads it, plus what had to be thrown away."""
    parsed = parse(path.read_text(encoding="utf-8", errors="replace"))
    piles = parsed["piles"]
    total = sum(len(v) for v in piles.values())
    dropped, warnings = drop_unanchored(piles)
    kept = sum(len(v) for v in piles.values())
    front = parsed["front"]
    # Read before the bookkeeping keys are stripped: `fixed_in` is derived from the items'
    # own `fixed-in`, which is exactly what is about to be thrown away.
    fixed_in = top_fixed_in(front, piles)
    for items in piles.values():
        for item in items:
            item.pop("_fixed_in", None)
            item.pop("_line", None)
    return {
        "mode": "points",
        "source": rel,
        "fixed_in": fixed_in,
        "findings": piles["findings"],
        "autofixes": piles["autofixes"],
        "assumptions": piles["assumptions"],
        "meta": {k: front[k] for k in
                 ("ticket", "base", "implementation", "reviewers", "session") if k in front},
        "frontmatter": front,
        "sections": parsed["sections"],
        "items": kept, "dropped": dropped, "warnings": warnings,
        "empty": total == 0,
    }


def report(doc: dict, out: Path, write: bool) -> None:
    src = doc["source"]
    print(f"{src}: {doc['items']} item(s) in "
          + ", ".join(f"{len(doc[k])} {PILE_HEADING[k].lower()}"
                      for k in ("autofixes", "findings", "assumptions")))
    for key, heading in (("autofixes", "Fixed"), ("findings", "Ignored"),
                         ("assumptions", "Assumptions")):
        if heading not in doc["sections"]:
            print(f"  {heading:<12} — no such section in the file")
            continue
        print(f"  {heading:<12} ({doc['sections'][heading]})")
        for item in doc[key]:
            marks = []
            if item.get("refs"):
                marks.append(f"{len(item['refs'])} ref")
            if item.get("snippets"):
                marks.append(f"{len(item['snippets'])} snippet")
            if item.get("diffs"):
                marks.append(f"{len(item['diffs'])} diff")
            if item.get("severity"):
                marks.append(item["severity"])
            if item.get("source"):
                marks.append(f"from {item['source']}")
            print(f"      · {re.sub('<[^>]+>', '', item['title'])[:70]}"
                  + (f"   [{', '.join(marks)}]" if marks else ""))
    if doc["fixed_in"]:
        print(f"  fixed in     {doc['fixed_in']}")
    if doc["meta"]:
        print("  " + " · ".join(f"{k}: {v}" for k, v in doc["meta"].items()))
    print(f"  {'would write' if not write else 'wrote'}  {out}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="the repository root (default: cwd)")
    ap.add_argument("--file", help=f"the points file, relative to --root (default: "
                                   f"human-review.json's \"reviewPoints\", else {DEFAULT_FILE})")
    ap.add_argument("--out", help=f"where the JSON goes (default: <root>/{DEFAULT_OUT})")
    ap.add_argument("--check", action="store_true",
                    help="parse and print what was understood; write nothing. Run this "
                         "before committing the file — a malformed file is worth catching "
                         "while somebody can still fix it")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    rel = args.file or config_path(root) or DEFAULT_FILE
    path = root / rel
    out = Path(args.out) if args.out else root / DEFAULT_OUT

    if not path.is_file():
        print(f"[review-points] no {rel} at {root} — nothing on this branch records what "
              f"was reviewed, fixed or declined", file=sys.stderr)
        return 3
    try:
        doc = document(path, rel)
    except Unparseable as bad:
        print(f"[review-points] {rel} cannot be read:", file=sys.stderr)
        for problem in bad.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 4

    for warning in doc["warnings"]:
        print(f"[review-points] WARNING: {warning}", file=sys.stderr)

    if doc["dropped"] and doc["items"] == 0:
        print(f"[review-points] every item in {rel} was unanchored — the file is there and "
              f"says nothing checkable. Not the same thing as a clean review.",
              file=sys.stderr)
        return 5

    if args.check:
        report(doc, out, write=False)
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    report(doc, out, write=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
