#!/usr/bin/env python3
"""What the Playwright traces step promises: the right recording under the right test.

Two halves, tested apart. The harvester reads a run's own HTML report and copies what it
finds; the renderer turns that manifest into rows that open the viewer. The seam between
them is the manifest, so a change to either that forgets the other fails here.

The report in these tests is built by hand — envelope, embedded zip and all — which pins
*our reading* of Playwright's format, not Playwright's format itself. That distinction is
the point of exit code 3: when the envelope changes under us, the step finds no trace and
says so in one line, rather than writing an empty manifest the page would render as a
suite that recorded nothing.

Run with:  python3 -m pytest test_playwright_traces.py
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("pw_traces", HERE / "playwright-traces.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

_bspec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(build)


# --------------------------------------------------------------------------- #
# a report, the way the html reporter writes one
# --------------------------------------------------------------------------- #

def _test(title, *, line, status="passed", traced=True, retry=0, error="", path=None):
    attachments = []
    if traced:
        attachments.append({"name": "trace", "contentType": "application/zip",
                            "path": f"data/{title.replace(' ', '-')}.zip"})
    return {
        "title": title, "projectName": "chromium", "path": path or ["Owner page"],
        "location": {"file": "src/owners.spec.ts", "line": line, "column": 5},
        "duration": 1658, "outcome": "expected" if status == "passed" else "unexpected",
        "results": [{"status": status, "retry": retry, "duration": 1658,
                     "attachments": attachments,
                     "errors": [{"message": error}] if error else [],
                     "steps": [{"title": 'Navigate to "/"', "duration": 14},
                               {"title": "Click getByRole('button')", "duration": 43}]}],
    }


def report(tmp_path: Path, tests, *, viewer=True) -> Path:
    """A Playwright HTML report on disk: the single index.html, plus data/ and trace/."""
    out = tmp_path / "playwright-report"
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    doc = {"fileId": "abc", "fileName": "owners.spec.ts", "tests": tests}
    summary = {"stats": {"total": len(tests)}, "files": [doc], "options": {}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("report.json", json.dumps(summary))
        z.writestr("abc.json", json.dumps(doc))
    payload = base64.b64encode(buf.getvalue()).decode()
    (out / "index.html").write_text(
        "<html><body><template id=\"playwrightReportBase64\">"
        f"data:application/zip;base64,{payload}</template></body></html>", encoding="utf-8")
    for t in tests:
        for a in t["results"][0]["attachments"]:
            # Padded, so the size the manifest reports is one this test can assert on.
            (out / a["path"]).write_bytes(b"PK\x03\x04" + b"0" * 1020)
    if viewer:
        (out / "trace").mkdir()
        (out / "trace" / "index.html").write_text("<html>viewer</html>", encoding="utf-8")
        (out / "trace" / "sw.bundle.js").write_text("// service worker", encoding="utf-8")
    # The config the report's test paths are relative to.
    (tmp_path / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "owners.spec.ts").write_text("// the test file\n" * 40, encoding="utf-8")
    return out


def harvest(tmp_path, tests, *, limit=12, **kw):
    out = tmp_path / "assets"
    out.mkdir(exist_ok=True)
    return pw.harvest(report(tmp_path, tests, **kw), out, "assets", tmp_path, None, limit)


# --------------------------------------------------------------------------- the harvester

def test_the_manifest_names_the_test_the_recording_belongs_to(tmp_path):
    """The whole reason the report is read at all. A trace zip does not carry the title,
    the file or the line of the test that produced it, and the artifact directory encodes
    them only as a truncated slug — so this mapping exists in the report or nowhere."""
    doc = harvest(tmp_path, [_test("lists the visits of an owner", line=15)])
    [row] = doc["tests"]
    assert row["title"] == "lists the visits of an owner"
    assert row["path"] == ["Owner page"]
    assert row["file"] == "src/owners.spec.ts" and row["line"] == 15
    assert row["status"] == "passed" and row["project"] == "chromium"
    assert [s["title"] for s in row["steps"]] == ['Navigate to "/"', "Click getByRole('button')"]


def test_the_zip_is_copied_into_the_pages_own_assets(tmp_path):
    """The report directory is wiped by the next run — before it, in fact — so a page that
    pointed into it would go blank the first time anyone re-ran the suite."""
    doc = harvest(tmp_path, [_test("lists the visits of an owner", line=15)])
    [row] = doc["tests"]
    assert row["trace"].startswith("assets/traces/")
    assert row["trace"].endswith(".zip")
    copied = tmp_path / "assets" / "traces"
    assert [p.name for p in copied.iterdir()] == [Path(row["trace"]).name]
    assert row["bytes"] == 1024


def test_the_viewer_travels_with_the_traces(tmp_path):
    """From the report's own `trace/`, so the viewer is the build that recorded them."""
    doc = harvest(tmp_path, [_test("a", line=1)])
    assert doc["viewer"] == "assets/traceviewer/index.html"
    assert (tmp_path / "assets" / "traceviewer" / "sw.bundle.js").is_file()


def test_a_report_with_no_viewer_still_yields_the_traces(tmp_path):
    """A viewer this machine cannot find costs the frame, never the recording: the page
    falls back to the command that opens the same zip natively."""
    doc = harvest(tmp_path, [_test("a", line=1)], viewer=False)
    assert doc["viewer"] is None and doc["recorded"] == 1


def test_results_that_ran_untraced_are_counted_not_hidden(tmp_path):
    """A suite that recorded two of its five tests has not shown the reader the run, and a
    page listing two with nothing said would read exactly as if it had."""
    doc = harvest(tmp_path, [_test("a", line=1),
                             _test("b", line=9, traced=False),
                             _test("c", line=17, traced=False)])
    assert doc["recorded"] == 1 and doc["untraced"] == 2


def test_the_limit_keeps_the_failures(tmp_path):
    """The one thing a size limit must never drop is the recording of the test that
    failed — which is precisely what dropping by order of appearance would do."""
    doc = harvest(tmp_path, [_test("green one", line=1),
                             _test("green two", line=9),
                             _test("the red one", line=17, status="failed")], limit=1)
    assert [t["title"] for t in doc["tests"]] == ["the red one"]
    assert doc["omitted"] == 2
    assert len(list((tmp_path / "assets" / "traces").iterdir())) == 1


def test_a_second_run_does_not_leave_the_first_runs_traces_behind(tmp_path):
    """The directory is rebuilt, not added to. Stale zips would be recordings of a branch
    state nobody is reviewing, sitting under names from a page that no longer exists."""
    harvest(tmp_path, [_test("gone next time", line=1)])
    doc = harvest(tmp_path, [_test("the only one now", line=1)])
    assert [p.name for p in (tmp_path / "assets" / "traces").iterdir()] \
        == [Path(doc["tests"][0]["trace"]).name]


def test_the_error_is_cut_to_its_first_line_with_the_colours_stripped(tmp_path):
    doc = harvest(tmp_path, [_test(
        "the red one", line=3, status="failed",
        error="\x1b[2mexpect(\x1b[22mlocator).toHaveText failed\n\nCall log:\n  - waiting")])
    assert doc["tests"][0]["error"] == "expect(locator).toHaveText failed"


def test_a_run_with_tracing_off_is_reported_rather_than_rendered_empty(tmp_path, capsys):
    """Exit 3, not an empty manifest: the step turns it into a skip that names the reason,
    and the tab is dropped and named under the strip instead of appearing with nothing in
    it — which a reader would take for a suite that has no tests."""
    out = report(tmp_path, [_test("a", line=1, traced=False)])
    code = pw.main(["--report", str(out), "--out", str(tmp_path / "assets"),
                    "--json", str(tmp_path / "assets" / "traces.json")])
    assert code == 3
    assert "tracing off" in capsys.readouterr().err


def test_a_test_that_never_ran_is_not_counted_as_one_that_ran_untraced(tmp_path):
    """A skipped test recorded nothing because it never started. Counting it would make the
    page report tracing as off for tests that were never asked to do anything."""
    doc = harvest(tmp_path, [_test("a", line=1),
                             _test("skipped one", line=9, traced=False, status="skipped")])
    assert doc["recorded"] == 1 and doc["untraced"] == 0


def test_a_run_with_tracing_off_clears_the_previous_runs_viewer(tmp_path):
    """Not copying it is only half the saving: the previous run's three megabytes would
    otherwise travel in the zip beside a page that no longer names them."""
    harvest(tmp_path, [_test("a", line=1)])
    assert (tmp_path / "assets" / "traceviewer").is_dir()
    harvest(tmp_path, [_test("a", line=1, traced=False)])
    assert not (tmp_path / "assets" / "traceviewer").exists()


def test_a_missing_report_still_writes_the_manifest_it_was_asked_for(tmp_path):
    """A content file that names a manifest and does not find it fails the whole build.
    Finding it and reading "nothing was recorded" costs one tab, which is what a step that
    could not run is supposed to cost."""
    out = tmp_path / "assets"
    code = pw.main(["--report", str(tmp_path / "nowhere"), "--out", str(out),
                    "--json", str(out / "traces.json")])
    assert code == 2
    assert json.loads((out / "traces.json").read_text()) == {
        "report": str(tmp_path / "nowhere"), "viewer": None, "recorded": 0,
        "omitted": 0, "untraced": 0, "tests": []}


def test_a_run_with_tracing_off_leaves_no_viewer_behind(tmp_path):
    """Three megabytes of browser application, copied into the assets the downloadable zip
    is built from, for a tab that is about to be dropped."""
    doc = harvest(tmp_path, [_test("a", line=1, traced=False)])
    assert doc["viewer"] is None
    assert not (tmp_path / "assets" / "traceviewer").exists()


def test_no_report_at_all_is_a_different_answer_from_an_untraced_one(tmp_path):
    assert pw.main(["--report", str(tmp_path / "nowhere"), "--out", str(tmp_path / "a")]) == 2


def test_an_unreadable_envelope_says_so_instead_of_harvesting_nothing(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "index.html").write_text("<html>a report from the future</html>")
    with pytest.raises(ValueError, match="no embedded report data"):
        pw.report_data(tmp_path / "r")


# --------------------------------------------------------------------------- the renderer

def _manifest(tmp_path, **over):
    doc = {"viewer": "assets/traceviewer/index.html", "recorded": 1, "omitted": 0,
           "untraced": 0,
           "tests": [{"title": "lists the visits of an owner", "path": ["Owner page"],
                      "file": "src/owners.spec.ts", "line": 15, "project": "chromium",
                      "status": "passed", "outcome": "expected", "retry": 0,
                      "duration": 1658, "error": "",
                      "steps": [{"title": 'Navigate to "/"', "duration": 14}],
                      "trace": "assets/traces/001-owner-page.zip", "bytes": 1024}]}
    doc.update(over)
    return doc


def test_a_row_carries_the_test_the_recording_is_of(tmp_path):
    frag, weight = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert weight == 1
    assert "lists the visits of an owner" in frag
    assert '<span class="trpath">Owner page › </span>' in frag
    assert "owners.spec.ts:15" in frag and "1.7s" in frag
    assert 'Navigate to &quot;/&quot;<b>14ms</b>' in frag


def test_the_frame_is_a_path_on_the_row_and_never_an_iframe_in_the_markup(tmp_path):
    """Eleven rows would otherwise boot eleven copies of a browser application on load,
    each fetching its own multi-megabyte zip, to show the one the reader asked for."""
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert "<iframe" not in frag
    assert 'data-trace="assets/traces/001-owner-page.zip"' in frag
    assert 'data-viewer="assets/traceviewer/index.html"' in frag


def test_every_row_offers_the_native_command_for_a_page_read_off_disk(tmp_path):
    """From the zip or from `file://` nothing can be fetched, so the frame would be blank.
    The command opens the same recording, and it is written whether or not that reader
    exists — the build cannot know which of the two is reading."""
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert "npx playwright show-trace .human-review/assets/traces/001-owner-page.zip" in frag


def test_the_command_names_the_directory_the_page_was_built_into(tmp_path):
    """`.human-review` is only the default. A command naming a directory the reader does
    not have is worse than no command at all."""
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / "out/review")
    assert "npx playwright show-trace out/review/assets/traces/001-owner-page.zip" in frag


def test_a_failed_run_is_flagged_and_its_headline_shown(tmp_path):
    doc = _manifest(tmp_path)
    doc["tests"][0].update(status="failed", error="expect(locator).toHaveText failed")
    frag, _ = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    assert '<span class="tflag removed">failed</span>' in frag
    assert 'class="trerr">expect(locator).toHaveText failed' in frag


def test_a_retry_is_stamped_on_the_row(tmp_path):
    """The row is one attempt. Attempt 2 shown unmarked reports a flaky test as a green
    one, which is the single most expensive thing this tab could get wrong."""
    doc = _manifest(tmp_path)
    doc["tests"][0]["retry"] = 1
    frag, _ = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    assert "retry 1" in frag


def test_the_lede_states_what_the_run_did_not_record(tmp_path):
    frag, _ = build.render_traces(_manifest(tmp_path, untraced=38, omitted=4), tmp_path, tmp_path / '.human-review')
    assert "38 result(s) ran with tracing off" in frag
    assert "4 more not carried onto this page" in frag


def test_a_manifest_with_no_recordings_renders_nothing_at_all(tmp_path):
    """Weight zero, so a tab built on this block alone is dropped and named rather than
    kept as an empty panel under a pill that promises a recording."""
    assert build.render_traces({"tests": []}, tmp_path, tmp_path / '.human-review') == ("", 0)


def test_a_title_from_the_suite_cannot_close_the_row_it_sits_in(tmp_path):
    doc = _manifest(tmp_path)
    doc["tests"][0]["title"] = '</summary><script>alert(1)</script>'
    frag, _ = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    assert "<script>alert(1)</script>" not in frag
    assert "&lt;script&gt;" in frag


def test_the_test_file_is_openable_when_it_is_still_on_disk(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "owners.spec.ts").write_text("// here", encoding="utf-8")
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert f'href="vscode://file/{(tmp_path / "src/owners.spec.ts").resolve()}:15:1"' in frag


def test_a_test_file_that_is_gone_leaves_no_dead_link(tmp_path):
    """The one thing this page never emits is a custom URL that opens nothing."""
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert "vscode://file/" not in frag


# --------------------------------------------------------------------------- the wiring

def test_a_traces_block_without_a_manifest_is_refused_at_build_time(tmp_path):
    problems = build.validate(
        {"tabs": [{"id": "requirements", "blocks": [{"type": "traces"}]}]}, tmp_path)
    assert any("playwrightTraces" in p for p in problems)


def test_a_manifest_that_was_never_written_is_named(tmp_path):
    problems = build.validate({"playwrightTraces": "assets/traces.json"}, tmp_path)
    assert any("playwright-traces.py" in p for p in problems)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
