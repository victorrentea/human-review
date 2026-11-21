#!/usr/bin/env python3
"""Does this branch change a method that a critical entry point runs?

A team marks an entry point critical in its own source — `@critical` in the handler's
Javadoc or a comment above it, or an annotation of its own named `@Critical` — and from
then on the handler's whole call graph is critical: every method it reaches, however many
calls down (Victor, 21 Nov 2026). A branch whose diff lands inside any of those methods is
touching a sensitive area, and the page says so with ☢️ in the grade pill, top right,
before the reader has opened a single tab.

The call graphs are the ones `endpoint-complexity.py` already draws for the Complexity
tab, read from both of its snapshots:

  * a hunk's NEW lines are checked against the flows of the working tree (`after`), so an
    edited or added line inside a method of a critical flow counts;
  * its OLD lines against the flows of the merge-base (`before`), so a line DELETED from
    such a method counts too, and so does a method the branch removed altogether.

An entry point is critical when either side marks it: a branch that deletes the marker is
not let off by its own deletion.

"Proven" here means: the diff hunk's line range intersects the declaration span (its
annotations to its closing brace) of a method the static call graph reaches. The graph is
the Complexity tab's, with its limits — interface calls fan out to every implementation
(over-counting, which errs toward ☢️), and what runs only by reflection, proxies or
generated code past its leaf is invisible. A snapshot without spans (an older one, or a
project's own producer) proves nothing and the page shows no ☢️ rather than a guessed one.

Usage:
    critical-flows.py before.json after.json --base origin/main [--out critical-flows.json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def hunks(diff: str) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """`{path: {"old": [(a, b)…], "new": [(c, d)…]}}` from a `-U0` diff — the line ranges
    each hunk removes from the base and writes into the working tree, inclusive. A hunk
    that only adds has no old range, one that only deletes has no new range."""
    out: dict[str, dict[str, list[tuple[int, int]]]] = {}
    old_path = new_path = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            old_path = None if line[4:] == "/dev/null" else line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            new_path = None if line[4:] == "/dev/null" else line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2) or 1)
            c, d = int(m.group(3)), int(m.group(4) or 1)
            if b and old_path:
                out.setdefault(old_path, {"old": [], "new": []})["old"].append((a, a + b - 1))
            if d and new_path:
                out.setdefault(new_path, {"old": [], "new": []})["new"].append((c, c + d - 1))
    return out


def spans(item: dict) -> list[tuple[int, int]]:
    """The declaration span(s) of one flow item, or [] when the snapshot does not say."""
    if item.get("spans"):
        return [(int(a), int(b)) for a, b in item["spans"]]
    if item.get("line") and item.get("endLine"):
        return [(int(item["line"]), int(item["endLine"]))]
    return []


def entry_key(e: dict) -> tuple[str, str, str]:
    return (e.get("kind") or "http", e.get("httpMethod") or "", e.get("path") or "")


def entry_name(e: dict) -> str:
    """`POST /api/visits`, `MCP createVisit`, `JOB RagIngestion.poll` — what the hover says."""
    verb = e.get("httpMethod") or ""
    return f"{verb} {e.get('path') or e.get('handler') or ''}".strip()


def _overlap(ranges: list[tuple[int, int]], span: tuple[int, int]) -> list[int]:
    """The changed lines of `ranges` that fall inside `span`, each range clipped to it."""
    lo, hi = span
    out: list[int] = []
    for a, b in ranges:
        a, b = max(a, lo), min(b, hi)
        out.extend(range(a, b + 1))
    return out


def touched(before: list[dict], after: list[dict], changed: dict) -> dict:
    """Which critical entry points the change reaches, and through which of their methods."""
    sides = {"before": {entry_key(e): e for e in before},
             "after": {entry_key(e): e for e in after}}
    critical = sorted({k for side in sides.values() for k, e in side.items() if e.get("critical")})
    report = []
    for k in critical:
        e = sides["after"].get(k) or sides["before"][k]
        hits: dict[str, dict] = {}
        for side, which in (("after", "new"), ("before", "old")):
            for item in (sides[side].get(k) or {}).get("flow") or []:
                ranges = (changed.get(item.get("file") or "") or {}).get(which) or []
                lines = sorted({n for s in spans(item) for n in _overlap(ranges, s)})
                if not lines:
                    continue
                h = hits.setdefault(item.get("method") or item.get("display"), {
                    "method": item.get("method"), "display": item.get("display"),
                    "file": item.get("file"), "line": item.get("line"),
                    "added": [], "removed": []})
                h["added" if which == "new" else "removed"] = lines
        marker = (sides["after"].get(k) or {}).get("critical") or \
                 (sides["before"].get(k) or {}).get("critical")
        report.append({"kind": k[0], "httpMethod": k[1], "path": k[2],
                       "name": entry_name(e), "handler": e.get("handler", ""),
                       "marker": marker, "touched": list(hits.values())})
    return {"critical": report,
            "touched": [r for r in report if r["touched"]],
            "provable": any(spans(i) for side in (before, after) for e in side
                            for i in e.get("flow") or [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--base", default="origin/main",
                    help="the ref the branch is compared to; its merge-base is used")
    ap.add_argument("--out", help="where to write the JSON (default: stdout)")
    args = ap.parse_args(argv)

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    # The merge-base and the working tree: the same two sides the snapshots were taken on.
    mb = git("merge-base", args.base, "HEAD").strip()
    diff = git("diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", mb, "--", "*.java")
    result = touched(before, after, hunks(diff))
    body = json.dumps(result, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"[critical] {len(result['critical'])} critical entry point(s), "
              f"{len(result['touched'])} touched -> {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
