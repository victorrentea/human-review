#!/usr/bin/env python3
"""Which tests this change set wrote, which it edited, and which it stopped running.

The Tests tab hangs a list of tests under each requirement, and a reviewer reads a *new*
test very differently from a tweaked one: the first is evidence the requirement was
pinned, the second is evidence an existing pin was moved. That distinction is a fact
about the diff, not a judgement, so it is computed here rather than asserted by whoever
writes the content file. The model's only job is to say which requirement a test belongs
to; this script says what happened to it.

The question underneath all of it is whether the branch left fewer tests running than it
found, and that has three answers, not one: a test can be deleted, commented out, or
left in place under an `@Disabled`. All three cost the run the same test, and only the
first shows up as a removal in a diff -- the other two read as "still there" to anyone
skimming. So each side is scanned for what is *declared* and for what is *switched off*,
and the page states one reconciled pair of numbers: how many tests entered the run and
how many left it, by whatever route.

The unit is a **test case**, not a file. A file that shows up as `M` in `--name-status`
usually holds one new test and nine untouched ones, and reporting the whole file as
"modified" would bury exactly the row the reviewer came for. So each side of the diff is
parsed for its test declarations, and the two name sets decide:

    in the new tree only            -> added      (a test that did not exist before)
    in the base only                -> deleted
    in both, and the diff touched
    its body                        -> modified
    in both, untouched              -> unchanged

and carries, alongside that, whether it runs now and whether it ran before:

    @Disabled / it.skip / t.Skip() -> silenced: "disabled"
    declaration only inside a comment -> silenced: "commented" (reported as deleted,
                                       because the run has lost it either way)

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
JAVA_TYPE = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed)\s+)*"
                       r"(?:class|interface|record|enum)\s+\w+")
# `it(...)`, `test(...)`, and their modifiers -- `it.only`, `test.skip`, `xit`, and the
# table form `it.each([...])('name', ...)`, whose title sits in the *second* call.
JS_CASE = re.compile(r"""^\s*(?P<head>x?(?:it|test)(?:\.\w+)*)\s*(?:\([^;]*?\)\s*)?\(\s*(?P<q>['"`])(?P<name>.+?)(?P=q)""")
JS_SUITE = re.compile(r"""^(?P<indent>\s*)(?P<head>x?(?:describe|context|suite)(?:\.\w+)*)\s*\(""")
PY_CASE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
PY_CLASS = re.compile(r"^(\s*)class\s+\w+")
GO_CASE = re.compile(r"^func\s+((?:Test|Benchmark|Fuzz|Example)\w*)\s*\(")
GHERKIN_CASE = re.compile(r"^\s*(?:Scenario|Scenario Outline|Scenario Template|Example)\s*:\s*(?P<name>\S.*?)\s*$")

# --------------------------------------------------------------------------- #
# what counts as a silenced test
# --------------------------------------------------------------------------- #
# A test stops running for three reasons, and only one of them is deletion. The other two
# leave the code in the file -- an `@Disabled` on top of it, or a `//` in front of every
# line -- and both read as "still there" in a diff a human skims. The reviewer needs the
# same warning for all three, so silencing is read off the syntax exactly like the
# declarations above, and nobody has to remember to declare it.
JAVA_SKIP = re.compile(r"^\s*@(?:Disabled|Ignore)\b")
# Applied to the matched call head, not to the line: `xit`, `it.skip`, `test.todo`.
JS_SKIP = re.compile(r"^x|\.(?:skip|todo|failing)\b")
PY_SKIP = re.compile(r"^\s*@(?:\w+\.)*(?:skip|skipif|skipIf|skipUnless|xfail)\b")
# Go has no annotation for it: the test runs, and the first thing it does is bail out.
GO_SKIP = re.compile(r"\.Skip(?:Now|f)?\s*\(")
GHERKIN_TAGS = re.compile(r"^\s*@\S")
GHERKIN_SKIP = re.compile(r"(?i)@(?:ignore|skip|wip|disabled|manual)\b")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _java_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """A JUnit method is a test because it carries a test annotation, not because it is
    `void`. Helper methods in the same class are `void` too, and counting them would put
    a `setUp` in a requirement's coverage list.

    The `@Disabled` that silences one may sit on the method or on the class around it, so
    a disabled type declaration hands its state down to everything indented under it --
    which is also what keeps a `@Disabled` on a `@Nested` class from silencing its
    siblings."""
    out: dict[str, tuple[int, str | None]] = {}
    annotated = skip = False
    off_from: int | None = None      # indent of the innermost type declaration turned off
    for i, line in enumerate(lines, start=1):
        if JAVA_TEST_ANNOTATION.match(line):
            annotated = True
            continue
        if JAVA_SKIP.match(line):
            skip = True
            continue
        if not line.strip() or line.lstrip().startswith(("//", "*", "/*", "@")):
            continue
        indent = _indent(line)
        if JAVA_TYPE.match(line):
            if off_from is not None and indent <= off_from:
                off_from = None      # that class closed before this one opened
            if skip and off_from is None:
                off_from = indent
            annotated = skip = False
            continue
        m = JAVA_METHOD.match(line)
        if m:
            if annotated:
                inside = off_from is not None and indent > off_from
                out.setdefault(m.group(1), (i, "disabled" if skip or inside else None))
            annotated = skip = False
        elif line.strip().endswith(("{", "}", ";")):
            # Anything else that closes a statement ends the annotation's reach.
            annotated = skip = False
    return out


def _js_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """`xdescribe` / `describe.skip` silences every case nested in it, so a suite that is
    switched off is tracked by the indent it opened at, the same way Java's class is."""
    out: dict[str, tuple[int, str | None]] = {}
    off_from: int | None = None
    for i, line in enumerate(lines, start=1):
        s = JS_SUITE.match(line)
        if s:
            indent = len(s.group("indent"))
            if off_from is not None and indent <= off_from:
                off_from = None
            if JS_SKIP.search(s.group("head")) and off_from is None:
                off_from = indent
            continue
        m = JS_CASE.match(line)
        if m:
            inside = off_from is not None and _indent(line) > off_from
            out.setdefault(m.group("name"),
                           (i, "disabled" if JS_SKIP.search(m.group("head")) or inside else None))
    return out


def _py_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    out: dict[str, tuple[int, str | None]] = {}
    skip = False
    off_from: int | None = None
    for i, line in enumerate(lines, start=1):
        if PY_SKIP.match(line):
            skip = True
            continue
        if not line.strip():
            continue
        c = PY_CLASS.match(line)
        if c:
            indent = len(c.group(1))
            if off_from is not None and indent <= off_from:
                off_from = None
            if skip and off_from is None:
                off_from = indent
            skip = False
            continue
        m = PY_CASE.match(line)
        if m:
            inside = off_from is not None and _indent(line) > off_from
            out.setdefault(m.group(1), (i, "disabled" if skip or inside else None))
            skip = False
        elif not line.lstrip().startswith(("@", "#")):
            skip = False
    return out


def _go_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """`t.Skip()` is a statement, not a marker, so the whole body has to be read. A test
    that skips itself conditionally is still reported as silenced: the reviewer is the
    one who should decide whether the condition holds on CI."""
    starts = [(i, m.group(1)) for i, line in enumerate(lines, start=1)
              if (m := GO_CASE.match(line))]
    out: dict[str, tuple[int, str | None]] = {}
    for n, (i, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines) + 1
        body = "\n".join(lines[i - 1:end - 1])
        out.setdefault(name, (i, "disabled" if GO_SKIP.search(body) else None))
    return out


def _gherkin_scan(lines: list[str]) -> dict[str, tuple[int, str | None]]:
    """A `@wip` on the Feature turns off every scenario under it; one on a Scenario turns
    off that scenario. Whether the runner is told to exclude the tag is a build-file
    question this script cannot see -- but a tag whose whole job is to exclude is worth
    the reviewer's attention either way."""
    out: dict[str, tuple[int, str | None]] = {}
    tagged = feature_off = False
    for i, line in enumerate(lines, start=1):
        if GHERKIN_TAGS.match(line):
            tagged = tagged or bool(GHERKIN_SKIP.search(line))
            continue
        if not line.strip():
            continue
        if re.match(r"^\s*Feature\s*:", line):
            feature_off, tagged = tagged, False
            continue
        m = GHERKIN_CASE.match(line)
        if m:
            out.setdefault(m.group("name"),
                           (i, "disabled" if tagged or feature_off else None))
        tagged = False
    return out


def scan_cases(rel: str, text: str) -> dict[str, tuple[int, str | None]]:
    """`{test name: (1-based declaration line, why it does not run | None)}`."""
    lines = text.splitlines()
    if rel.endswith(".java"):
        return _java_scan(lines)
    if rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        return _js_scan(lines)
    if rel.endswith(".py"):
        return _py_scan(lines)
    if rel.endswith(".go"):
        return _go_scan(lines)
    if rel.endswith(".feature"):
        return _gherkin_scan(lines)
    return {}


def test_cases(rel: str, text: str) -> dict[str, int]:
    """`{test name: 1-based declaration line}` for one file's source."""
    return {name: line for name, (line, _) in scan_cases(rel, text).items()}


# --------------------------------------------------------------------------- #
# a test that is still there, only commented out
# --------------------------------------------------------------------------- #
# Commenting a test out is deleting it with the body left behind as an alibi: the run
# loses it exactly as if the lines were gone, but the file still contains every word of
# it, so a reviewer skimming the diff reads "kept, just parked". The scanners above are
# therefore run a second time over the source with one layer of comment marker stripped,
# and a declaration that turns up there and nowhere in the live code is a test that was
# switched off in the quietest way there is.
UNCOMMENT_SLASH = re.compile(r"^(\s*)(?://+|\*(?!/))[ \t]?")
UNCOMMENT_HASH = re.compile(r"^(\s*)#+[ \t]?")


def _uncomment(rel: str, text: str) -> str:
    """The same source with its comment markers taken off, line for line -- so anything
    found in it keeps the line number it has on disk and stays clickable."""
    if rel.endswith((".py", ".feature")):
        pat = UNCOMMENT_HASH
    elif rel.endswith((".java", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go")):
        pat = UNCOMMENT_SLASH
    else:
        return text
    return "\n".join(pat.sub(r"\1", line, count=1) for line in text.splitlines())


def commented_cases(rel: str, text: str) -> dict[str, int]:
    """`{test name: line}` for the cases that exist in this file only inside a comment."""
    live = scan_cases(rel, text)
    return {n: ln for n, (ln, _) in scan_cases(rel, _uncomment(rel, text)).items()
            if n not in live}


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


def _row(name: str, rel: str, status: str, line: int | None,
         silenced: str | None, was_silenced: str | None) -> dict:
    """One row of the manifest. The two silence fields are written only when they say
    something -- an absent key means "it ran before and it runs now", which is true of
    almost every row and does not need repeating on all of them."""
    row = {"name": name, "path": rel, "status": status, "line": line}
    if silenced:
        row["silenced"] = silenced
    if was_silenced:
        row["wasSilenced"] = was_silenced
    return row


def classify_file(rel: str, status: str, before: str | None, after: str | None,
                  added: set[int], removed: dict[int, int]) -> list[dict]:
    """Every test case in one changed file, with what happened to it."""
    before_cases = scan_cases(rel, before) if before is not None else {}
    after_cases = scan_cases(rel, after) if after is not None else {}
    commented = commented_cases(rel, after) if after is not None else {}
    touched = added | set(removed.values())
    rows: list[dict] = []

    after_starts = sorted(ln for ln, _ in after_cases.values())
    for name, (line, silenced) in sorted(after_cases.items(), key=lambda kv: kv[1][0]):
        end = next((s for s in after_starts if s > line), line + 1)
        was = before_cases.get(name)
        if was is None:
            state = "added"
        elif any(line <= t < end for t in touched):
            state = "modified"
        else:
            state = "unchanged"
        rows.append(_row(name, rel, state, line, silenced, was[1] if was else None))

    before_starts = sorted(ln for ln, _ in before_cases.values())
    for name, (line, was_silenced) in sorted(before_cases.items(), key=lambda kv: kv[1][0]):
        if name in after_cases:
            continue
        end = next((s for s in before_starts if s > line), line + 1)
        anchors = [new for old, new in removed.items() if line <= old < end]
        # Commented out rather than removed: the body is still in the file, so the row
        # still has somewhere to go -- the comment itself, which is the thing the
        # reviewer has to judge.
        parked = commented.get(name)
        row = _row(name, rel, "deleted",
                   parked if parked is not None else (min(anchors) if anchors else None),
                   "commented" if parked is not None else None, was_silenced)
        if status == "D":
            # The file itself is gone, so there is nothing to open. Saying that is
            # better than emitting a link that dead-ends in the editor.
            row["gone"] = True
            row["line"] = None
        rows.append(row)
    return rows


def totals(rows: list[dict]) -> dict:
    """The arithmetic the chip at the top of the page states: how many tests this change
    set put into the run, and how many it took out of it.

    A test counts as *running* when it is declared and not silenced, which is what makes
    the three ways of losing one commensurable: deleting it, commenting it out and
    hanging an `@Disabled` on it all cost the run exactly one test, and a reviewer told
    only about the first has been told the smallest of the three truths. It also stops
    the count from moving on a test that was already switched off before the branch
    touched it -- deleting a test nobody was running changes nothing.

    `gained - lost == runningAfter - runningBefore` by construction, and the tests hold
    it to that: two numbers on a chip that do not reconcile with the rows behind them
    are worse than no chip."""
    t = dict.fromkeys(("added", "modified", "deleted", "unchanged", "commented",
                       "disabled", "reenabled", "runningBefore", "runningAfter",
                       "gained", "lost"), 0)
    for r in rows:
        t[r["status"]] += 1
        ran_before = r["status"] != "added" and not r.get("wasSilenced")
        runs_now = r["status"] != "deleted" and not r.get("silenced")
        t["runningBefore"] += ran_before
        t["runningAfter"] += runs_now
        if r.get("silenced") == "commented":
            t["commented"] += 1
        if runs_now and not ran_before:
            t["gained"] += 1
            if r["status"] != "added":
                t["reenabled"] += 1
        elif ran_before and not runs_now:
            t["lost"] += 1
            if r["status"] != "deleted":
                t["disabled"] += 1
    return t


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
    t = totals(rows)
    doc = {"base": args.base, "totals": t, "tests": rows}
    text = json.dumps(doc, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        detail = ", ".join(f"{t[k]} {k}" for k in
                           ("added", "modified", "deleted", "commented", "disabled", "reenabled")
                           if t[k])
        print(f"[test-changes] {len(rows)} test cases in changed test files -> {args.out}"
              + (f" ({detail})" if detail else "")
              + f"; the run goes from {t['runningBefore']} to {t['runningAfter']} "
                f"(+{t['gained']} / -{t['lost']})",
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
