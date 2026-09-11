#!/usr/bin/env python3
"""Every Playwright trace the run recorded, harvested next to the page that shows it.

The Tests tab says what the branch did to the tests and whether they passed. It cannot
answer the question a reviewer asks the moment one of them is interesting: *what did that
test actually do?* A Playwright trace holds that answer in full — every action with the
screenshot before and after it, the DOM at each step, the console, and every request the
browser made — and Playwright ships the viewer for it as a static page. So the review page
does not have to describe a run: it can carry the recording and let the reader step through
it, in the tab, offline, months later.

What this script does is only plumbing, and deliberately so:

  * it reads the run's own HTML report — the one the `html` reporter already wrote — for
    the mapping from **test** to **trace**. That mapping exists nowhere else. A trace zip
    does not carry the title, the file or the line of the test that produced it (the
    `context-options` event names the browser and the SDK, nothing more), and the artifact
    directory encodes them only as a sanitised, truncated slug. Guessing the test from the
    slug would put a wrong name over a real recording, which is worse than no recording;
  * it copies each zip and the viewer **into the page's own assets**, so the page keeps
    working when the test-results directory is wiped by the next run — which is the normal
    fate of that directory, and it is wiped before the run that would replace it;
  * it writes one manifest the builder renders. No HTML is produced here.

The viewer comes from the report itself (`<report>/trace/`): the html reporter copies it in
whenever any test attached a trace, so it is the same build of the viewer that recorded the
traces — a pairing that cannot drift. `node_modules/playwright-core/lib/vite/traceViewer` is
the fallback for a report written without one.

Tracing has to have been ON for the run. A project that leaves `trace: 'on-first-retry'`
(the default worth having in CI) records nothing on a green local run, and this exits 3
saying so rather than writing an empty manifest the page would render as "no tests here".

Usage:
    playwright-traces.py --report petclinic-test/test-results/playwright-report \\
        --out .human-review/assets --json .human-review/assets/traces.json

Exit codes: 0 harvested · 2 no report where one was named · 3 a report, and no trace in it.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

# The report is one self-contained HTML file with its own data zipped, base64'd and parked
# in a <template> the app deletes from the DOM as soon as it has read it. That is a private
# arrangement between the reporter and its own viewer, so it is pinned here by a test with a
# real report in it: when Playwright changes the envelope, that test says so in one line
# instead of the step quietly harvesting nothing.
REPORT_DATA = re.compile(
    r'<template id="playwrightReportBase64">data:application/zip;base64,'
    r"([A-Za-z0-9+/=\s]+)</template>")

# Enough of a slug to recognise the test in a filename, and no more: these land in a zip
# somebody may download on a machine whose path limit is not this one's.
SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str, limit: int = 60) -> str:
    return SLUG.sub("-", text.lower()).strip("-")[:limit].strip("-") or "test"


def report_data(report: Path) -> dict:
    """The report's own JSON, out of the single HTML file it writes.

    Returns `{fileName: <per-file doc>}` merged with the top-level summary, which is the
    only shape the caller here needs: the summary knows the run, the per-file docs know the
    attachments, and neither knows both.
    """
    index = report / "index.html"
    if not index.is_file():
        raise FileNotFoundError(index)
    m = REPORT_DATA.search(index.read_text(encoding="utf-8", errors="replace"))
    if not m:
        raise ValueError(
            f"{index} is not a Playwright HTML report this can read — no embedded report "
            "data. A report written with `attachmentsBaseURL`, or by a Playwright old "
            "enough to write data/ as loose files, needs the loose form handled here.")
    zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(m.group(1))))
    summary = json.loads(zf.read("report.json"))
    files = [json.loads(zf.read(n)) for n in zf.namelist() if n != "report.json"]
    return {"summary": summary, "files": files}


def project_dir(report: Path) -> Path | None:
    """Where the test paths in the report are relative to.

    Playwright reports a test's location relative to the config's `rootDir`, which is the
    directory holding the config. The report is written somewhere under that tree, so
    walking up for the config finds it; a project that keeps its report elsewhere passes
    `--project-dir` and this is never called.
    """
    for parent in [report, *report.parents]:
        if any(parent.glob("playwright*.config.*")):
            return parent
    return None


def viewer_source(report: Path, base: Path | None) -> Path | None:
    """The static trace viewer to copy, or None if this machine has none to offer."""
    shipped = report / "trace"
    if (shipped / "index.html").is_file():
        return shipped
    for parent in ([base, *base.parents] if base else []):
        got = parent / "node_modules/playwright-core/lib/vite/traceViewer"
        if (got / "index.html").is_file():
            return got
    return None


def first_line(text: str) -> str:
    """The headline of a Playwright error, with its terminal colours stripped.

    The full message is in the trace, one click away, complete with the call log and the
    diff. What the row needs is the sentence that says which assertion gave up."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    for line in plain.splitlines():
        if line.strip():
            return line.strip()
    return ""


def harvest(report: Path, out: Path, prefix: str, root: Path,
            base: Path | None, limit: int) -> dict:
    doc = report_data(report)
    base = base or project_dir(report)
    rows, untraced = [], 0
    for f in doc["files"]:
        for test in f.get("tests") or []:
            for result in test.get("results") or []:
                trace = next((a for a in result.get("attachments") or []
                              if a.get("name") == "trace" and a.get("path")), None)
                if trace is None:
                    # A skipped test recorded nothing because it never ran, which is a
                    # fact about the suite and not about tracing. Counting it here would
                    # make the page report tracing as off for tests that never started.
                    if result.get("status") != "skipped":
                        untraced += 1
                    continue
                where = test.get("location") or {}
                src = where.get("file") or f.get("fileName") or ""
                on_disk = (base / src).resolve() if base and src else None
                rows.append({
                    "title": test.get("title", ""),
                    # The describe() blocks above it. Shown in front of the title, muted:
                    # three tests called "is rejected" are told apart by nothing else.
                    "path": test.get("path") or [],
                    "file": _relative(on_disk, root) if on_disk and on_disk.is_file() else src,
                    "line": where.get("line"),
                    "project": test.get("projectName", ""),
                    "status": result.get("status", ""),
                    "outcome": test.get("outcome", ""),
                    "retry": result.get("retry", 0),
                    "duration": result.get("duration", 0),
                    # Only the top level. A trace of a dozen actions nests four deep, and
                    # the strip under the row is a table of contents, not the trace.
                    "steps": [{"title": s.get("title", ""), "duration": s.get("duration", 0)}
                              for s in result.get("steps") or []],
                    "error": first_line((result.get("errors") or [{}])[0].get("message", "")),
                    "_zip": report / trace["path"],
                })

    # Failures first, and never dropped. A limit exists because traces are megabytes each
    # and the page is offered as a downloadable zip; a limit that could drop the recording
    # of the one test that failed would be a limit on exactly the wrong thing.
    rows.sort(key=lambda r: (r["status"] == "passed", r["file"], r["line"] or 0))
    omitted = max(0, len(rows) - limit) if limit else 0
    kept = rows[:limit] if limit else rows

    traces = out / "traces"
    if traces.exists():
        shutil.rmtree(traces)
    traces.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(kept, 1):
        src = r.pop("_zip")
        name = f"{i:03d}-{slug('-'.join(r['path'] + [r['title']]))}.zip"
        shutil.copyfile(src, traces / name)
        r["trace"] = f"{prefix}/traces/{name}"
        r["bytes"] = (traces / name).stat().st_size
    for r in rows[len(kept):]:
        r.pop("_zip", None)

    # Only when something was kept. A run with tracing off would otherwise leave three
    # megabytes of browser application in the assets directory for a tab that is about to
    # be dropped — and those assets are what the downloadable zip is built from.
    viewer = None
    dest = out / "traceviewer"
    # Removed first, always. Copying it only when something was kept is what stops a run
    # with tracing off spending three megabytes on a tab about to be dropped — but leaving
    # the PREVIOUS run's copy in place would put that weight in the downloadable zip
    # anyway, beside a page that no longer names it.
    if dest.exists():
        shutil.rmtree(dest)
    src = viewer_source(report, base) if kept else None
    if src:
        shutil.copytree(src, dest)
        viewer = f"{prefix}/traceviewer/index.html"

    return {
        "report": _relative(report.resolve(), root),
        "viewer": viewer,
        "recorded": len(kept),
        "omitted": omitted,
        # Not "tests that failed to record": a suite normally traces some and not others,
        # and the page says how many ran without one so nobody reads the list as the run.
        "untraced": untraced,
        "tests": kept,
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="the Playwright HTML report directory")
    ap.add_argument("--out", default=".human-review/assets",
                    help="where the zips and the viewer are copied")
    ap.add_argument("--prefix", default="assets",
                    help="how the page addresses --out (default: assets)")
    ap.add_argument("--json", dest="json_out", help="write the manifest here (default: stdout)")
    ap.add_argument("--project-dir", help="what the report's test paths are relative to")
    ap.add_argument("--limit", type=int, default=12,
                    help="how many traces to carry, failures first (0 = all)")
    args = ap.parse_args(argv)

    report = Path(args.report)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    missing = not (report / "index.html").is_file()
    # Written on every path, the empty one included — the same reason the actions manifest
    # beside the page is. A content file that names this manifest and does not find it
    # fails the whole build; finding it and reading "nothing was recorded" costs the page
    # one tab, which is what a step that could not run is supposed to cost.
    doc = ({"report": str(report), "viewer": None, "recorded": 0, "omitted": 0,
            "untraced": 0, "tests": []} if missing else
           harvest(report, out, args.prefix.rstrip("/"), Path.cwd(),
                   Path(args.project_dir) if args.project_dir else None, args.limit))

    text = json.dumps(doc, indent=2) + "\n"
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if missing:
        print(f"[traces] no Playwright report at {report} — did the suite run?",
              file=sys.stderr)
        return 2

    if not doc["recorded"]:
        print(f"[traces] {doc['untraced']} test result(s) in {report} and not one trace — "
              "the suite ran with tracing off (`trace: 'on'` / `--trace on`)", file=sys.stderr)
        return 3
    size = sum(t["bytes"] for t in doc["tests"]) / 1e6
    print(f"[traces] {doc['recorded']} trace(s), {size:.1f} MB -> {out}/traces"
          + (f"; {doc['omitted']} more not carried" if doc["omitted"] else "")
          + (f"; {doc['untraced']} result(s) ran untraced" if doc["untraced"] else "")
          + ("" if doc["viewer"] else "; NO viewer found — the page can only offer the "
                                      "`npx playwright show-trace` command"),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
