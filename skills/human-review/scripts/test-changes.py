#!/usr/bin/env python3
"""Which tests this change set wrote, which it edited, and which it deleted.

The Requirements tab hangs a list of tests under each requirement, and a reviewer reads
a *new* test very differently from a tweaked one: the first is evidence the requirement
was pinned, the second is evidence an existing pin was moved. That distinction is a fact
about the diff, not a judgement, so it is computed here rather than asserted by whoever
writes the content file. The model's only job is to say which requirement a test belongs
to; this script says what happened to it.

The unit is a **test case**, not a file. A file that shows up as `M` in `--name-status`
usually holds one new test and nine untouched ones, and reporting the whole file as
"modified" would bury exactly the row the reviewer came for. So each side of the diff is
parsed for its test declarations, and the two name sets decide:

    in the new tree only            -> added      (a test that did not exist before)
    in the base only                -> deleted
    in both, and the diff touched
    its body                        -> modified
    in both, untouched              -> unchanged

Line numbers are always in *working-tree* coordinates, so every row can be opened in the
editor. A deleted test has no line of its own any more, so it carries the line where its
removal landed — the point in the surviving file where the reader can see the gap. A test
in a file that was deleted outright carries no line at all and says so (`gone`).

Usage:
    test-changes.py --base origin/main [path ...] [--out assets/test-changes.json]

With no paths, the whole repository is scanned; anything that is not recognisably a test
file is skipped either way.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# what counts as a test file
# --------------------------------------------------------------------------- #
# By path, not by content: a file is a test because of where it sits and what it is
# called, which is the same rule the build systems in this repository already apply.
TEST_FILE = (
    re.compile(r"(?:^|/)src/test/"),
    re.compile(r"(?:^|/)tests?/"),
    re.compile(r"[A-Za-z0-9]Tests?\.java$"),
    re.compile(r"[A-Za-z0-9]IT\.java$"),
    re.compile(r"\.(?:spec|test)\.(?:ts|tsx|js|jsx)$"),
    re.compile(r"(?:^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.go$"),
    re.compile(r"\.feature$"),
)


def is_test_file(rel: str) -> bool:
    return any(p.search(rel) for p in TEST_FILE)


# --------------------------------------------------------------------------- #
# what counts as a test case
# --------------------------------------------------------------------------- #
# One declaration pattern per language. Each returns the *name* a human would use for
# the test, because that is what the content file names and what the page prints.
JAVA_METHOD = re.compile(r"^\s*(?:(?:public|private|protected|static|final|default)\s+)*"
                         r"(?:<[^>]+>\s*)?[\w.<>\[\], ?]+\s+(\w+)\s*\(")
JAVA_TEST_ANNOTATION = re.compile(r"^\s*@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
# `it(...)`, `test(...)`, and their modifiers — `it.only`, `test.skip`, and the
# table form `it.each([...])('name', …)`, whose title sits in the *second* call.
JS_CASE = re.compile(r"""^\s*(?:it|test)(?:\.\w+)*\s*(?:\([^;]*?\)\s*)?\(\s*(['"`])(?P<name>.+?)\1""")
PY_CASE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
GO_CASE = re.compile(r"^func\s+((?:Test|Benchmark|Fuzz|Example)\w*)\s*\(")
GHERKIN_CASE = re.compile(r"^\s*(?:Scenario|Scenario Outline|Scenario Template|Example)\s*:\s*(?P<name>\S.*?)\s*$")


def _java_cases(lines: list[str]) -> dict[str, int]:
    """A JUnit method is a test because it carries a test annotation, not because it is
    `void`. Helper methods in the same class are `void` too, and counting them would put
    a `setUp` in a requirement's coverage list."""
    out: dict[str, int] = {}
    annotated = False
    for i, line in enumerate(lines, start=1):
        if JAVA_TEST_ANNOTATION.match(line):
            annotated = True
            continue
        if not line.strip() or line.lstrip().startswith(("//", "*", "/*", "@")):
            continue
        m = JAVA_METHOD.match(line)
        if m:
            if annotated:
                out.setdefault(m.group(1), i)
            annotated = False
        elif line.strip().endswith(("{", "}", ";")):
            # Anything else that closes a statement ends the annotation's reach.
            annotated = False
    return out


def _by_regex(lines: list[str], pattern: re.Pattern, group) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, line in enumerate(lines, start=1):
        m = pattern.match(line)
        if m:
            out.setdefault(m.group(group), i)
    return out


def test_cases(rel: str, text: str) -> dict[str, int]:
    """`{test name: 1-based declaration line}` for one file's source."""
    lines = text.splitlines()
    if rel.endswith(".java"):
        return _java_cases(lines)
    if rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        return _by_regex(lines, JS_CASE, "name")
    if rel.endswith(".py"):
        return _by_regex(lines, PY_CASE, 1)
    if rel.endswith(".go"):
        return _by_regex(lines, GO_CASE, 1)
    if rel.endswith(".feature"):
        return _by_regex(lines, GHERKIN_CASE, "name")
    return {}


# --------------------------------------------------------------------------- #
# the diff, at line granularity
# --------------------------------------------------------------------------- #
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def hunk_lines(diff: str) -> tuple[set[int], dict[int, int]]:
    """`(new-file lines this diff added, {old-file line removed: where it was removed})`.

    The second half is what lets a deleted test still be clickable: the removal has no
    line of its own in the working tree, but it has a *place* in it — the line the
    reader's caret should land on to see the gap.
    """
    added: set[int] = set()
    removed: dict[int, int] = {}
    old = new = 0
    for line in diff.splitlines():
        m = HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(new)
            new += 1
        elif line.startswith("-"):
            removed[old] = new
            old += 1
        elif line.startswith(" "):
            old += 1
            new += 1
    return added, removed


def classify_file(rel: str, status: str, before: str | None, after: str | None,
                  added: set[int], removed: dict[int, int]) -> list[dict]:
    """Every test case in one changed file, with what happened to it."""
    before_cases = test_cases(rel, before) if before is not None else {}
    after_cases = test_cases(rel, after) if after is not None else {}
    touched = added | set(removed.values())
    rows: list[dict] = []

    after_starts = sorted(after_cases.values())
    for name, line in sorted(after_cases.items(), key=lambda kv: kv[1]):
        end = next((s for s in after_starts if s > line), line + 1)
        if name not in before_cases:
            state = "added"
        elif any(line <= t < end for t in touched):
            state = "modified"
        else:
            state = "unchanged"
        rows.append({"name": name, "path": rel, "status": state, "line": line})

    before_starts = sorted(before_cases.values())
    for name, line in sorted(before_cases.items(), key=lambda kv: kv[1]):
        if name in after_cases:
            continue
        end = next((s for s in before_starts if s > line), line + 1)
        anchors = [new for old, new in removed.items() if line <= old < end]
        row = {"name": name, "path": rel, "status": "deleted",
               "line": min(anchors) if anchors else None}
        if status == "D":
            # The file itself is gone, so there is nothing to open. Saying that is
            # better than emitting a link that dead-ends in the editor.
            row["gone"] = True
            row["line"] = None
        rows.append(row)
    return rows


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def collect(root: Path, base: str, paths: list[str]) -> list[dict]:
    names = git(root, "diff", "--name-status", base, "--", *paths)
    if names.returncode != 0:
        raise SystemExit(f"[test-changes] git diff failed against {base}:\n{names.stderr.strip()}")
    rows: list[dict] = []
    for entry in names.stdout.splitlines():
        parts = entry.split("\t")
        if len(parts) < 2:
            continue
        status, rel = parts[0][0], parts[-1]
        if not is_test_file(rel):
            continue
        before = None if status == "A" else git(root, "show", f"{base}:{rel}").stdout
        after = None
        if status != "D":
            f = root / rel
            after = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
        diff = git(root, "diff", "--unified=0", base, "--", rel).stdout
        added, removed = hunk_lines(diff)
        rows.extend(classify_file(rel, status, before, after, added, removed))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[], help="limit the scan to these paths")
    ap.add_argument("--base", required=True, help="the branch or commit to compare against")
    ap.add_argument("--out", help="write the JSON here (default: stdout)")
    args = ap.parse_args(argv)

    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip() or ".")
    rows = collect(root, args.base, args.paths)
    doc = {"base": args.base, "tests": rows}
    text = json.dumps(doc, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"[test-changes] {len(rows)} test cases in changed test files -> {args.out}"
              + (f" ({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))})" if rows else ""),
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
