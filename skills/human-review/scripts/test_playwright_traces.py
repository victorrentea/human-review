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

def _test(title, *, line, status="passed", traced=True, retry=0, error="", path=None,
          file="src/owners.spec.ts"):
    attachments = []
    if traced:
        attachments.append({"name": "trace", "contentType": "application/zip",
                            "path": f"data/{title.replace(' ', '-')}.zip"})
    return {
        "title": title, "projectName": "chromium", "path": path or ["Owner page"],
        "location": {"file": file, "line": line, "column": 5},
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


def harvest(tmp_path, tests, *, limit=12, changed=None, **kw):
    out = tmp_path / "assets"
    out.mkdir(exist_ok=True)
    return pw.harvest(report(tmp_path, tests, **kw), out, "assets", tmp_path, None, limit,
                      changed=changed)


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


def test_the_limit_keeps_the_tests_of_the_files_the_branch_touched_and_cuts_skipped_first(tmp_path):
    """Twenty recorded, sixteen carried — and the four cut were the tests of the file the
    branch changed, while a skipped suite's recordings stayed. The covering test on the
    Tests tab then had no 📺, and the reader asked why. Failures still outrank everything;
    below them the change set's own tests come before the rest, and a skipped result is
    the first thing to go. The cut rows are still counted."""
    doc = harvest(tmp_path, [_test("skipped but recorded", line=1, status="skipped",
                                   file="src/chatbot.spec.ts"),
                             _test("green elsewhere", line=9, file="src/no-reset.spec.ts"),
                             _test("shows all visits", line=17, file="src/visits.spec.ts"),
                             _test("the red one", line=25, status="failed",
                                   file="src/no-reset.spec.ts")],
                  limit=2, changed={"visits.spec.ts"})
    assert [t["title"] for t in doc["tests"]] == ["the red one", "shows all visits"]
    assert doc["omitted"] == 2 and doc["untraced"] == 0


def test_the_change_set_is_read_off_the_manifest_the_tests_step_wrote(tmp_path):
    """`test-changes.py` runs before this step and leaves its manifest beside the output;
    the basenames of its added/modified rows are the preference, an unchanged row is not."""
    (tmp_path / "test-changes.json").write_text(json.dumps({"tests": [
        {"name": "a", "path": "petclinic-test/src/visits.spec.ts", "status": "modified"},
        {"name": "b", "path": "petclinic-test/src/owners.spec.ts", "status": "unchanged"},
    ]}))
    assert pw.changed_test_files(tmp_path / "test-changes.json") == {"visits.spec.ts"}
    assert pw.changed_test_files(tmp_path / "absent.json") == set()


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
#
# `render_traces` draws nothing. The list it used to write — one collapsible row per
# recording, the viewer framed inside it — repeated the covering-tests map above it row
# for row, so it was dropped; what it writes now is a registry the 📺 on those rows reads,
# keyed by the file basename and declaration line the map already addresses a row with.
# These tests therefore read the JSON, not markup: what the page *shows* for a recording
# is the covering-tests row's business and is pinned in test_build_review.py.


def _registry(frag):
    """The registry as the 📺 parses it: the JSON inside the one script element.

    No unescaping step here on purpose — the `<\\/` the renderer writes so that nothing in
    the payload can close the element is a plain JSON escape, and a parser puts it back."""
    return json.loads(frag[frag.index(">") + 1: frag.rindex("</script>")])


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


def test_a_recording_is_keyed_by_the_test_the_map_addresses_it_by(tmp_path):
    """`owners.spec.ts:15` is how the covering-tests row names its test, so pairing a row
    with its recording is a lookup and never a guess at a title."""
    frag, weight = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert weight == 1
    reg = _registry(frag)
    assert reg["viewer"] == "assets/traceviewer/index.html"
    assert [t["test"] for t in reg["tests"]] == ["owners.spec.ts:15"]
    assert reg["tests"][0]["trace"] == "assets/traces/001-owner-page.zip"


def test_a_result_that_recorded_nothing_is_not_in_the_registry(tmp_path):
    """The 📺 is drawn from this list. An entry with no zip behind it would be a door
    onto a viewer with nothing to show."""
    doc = _manifest(tmp_path)
    doc["tests"][0]["trace"] = ""
    frag, weight = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    assert weight == 0 and _registry(frag)["tests"] == []


def test_the_viewer_is_named_once_and_no_frame_is_ever_written(tmp_path):
    """Eleven rows would otherwise boot eleven copies of a browser application on load,
    each fetching its own multi-megabyte zip, to show the one the reader asked for. The
    📺 opens the viewer in a window of its own, so the page carries no frame at all."""
    frag, _ = build.render_traces(_manifest(tmp_path), tmp_path, tmp_path / '.human-review')
    assert "<iframe" not in frag
    assert _registry(frag)["viewer"] == "assets/traceviewer/index.html"


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


def test_the_entry_carries_how_the_run_ended(tmp_path):
    """The recording is of an attempt, and the page has to be able to say which attempt
    it is about to open — a failure shown as a green replay is the single most expensive
    thing this registry could get wrong."""
    doc = _manifest(tmp_path)
    doc["tests"][0].update(status="failed", error="expect(locator).toHaveText failed")
    frag, _ = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    assert _registry(frag)["tests"][0]["status"] == "failed"


def test_a_manifest_with_no_recordings_renders_nothing_at_all(tmp_path):
    """Weight zero, so a tab built on this block alone is dropped and named rather than
    kept as an empty panel under a pill that promises a recording."""
    assert build.render_traces({"tests": []}, tmp_path, tmp_path / '.human-review') == ("", 0)


def test_a_path_from_the_run_cannot_close_the_script_it_rides_in(tmp_path):
    """The escaping the page depends on, and the one JSON does not do for us: inside a
    script element `</script>` ends the element wherever it appears, whatever quoting it
    is under, and the rest of the registry is then parsed as markup."""
    doc = _manifest(tmp_path)
    doc["tests"][0]["trace"] = "assets/traces/</script><b>oops</b>.zip"
    frag, _ = build.render_traces(doc, tmp_path, tmp_path / '.human-review')
    payload = frag[frag.index(">") + 1: frag.rindex("</script>")]
    assert "</" not in payload
    assert frag.count("</script>") == 1
    # And it is still the path the harvester wrote, once the reader parses it back.
    assert _registry(frag)["tests"][0]["trace"].endswith("</script><b>oops</b>.zip")


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


def test_cucumber_scenarios_recorded_by_the_projects_own_hooks_join_the_list(tmp_path):
    """cucumber-js writes no Playwright report, so a project records its scenarios itself:
    a zip each and a sidecar naming the test. The harvester reads the sidecars — never
    guesses a name from a zip — and files them with the report's rows, in the same shape,
    so the page tells them apart by nothing but the `.feature` in the location."""
    rep = report(tmp_path, [_test("lists the visits of an owner", line=15)])
    cuc = tmp_path / "cucumber-traces"
    cuc.mkdir()
    (cuc / "add-visit-remembers.zip").write_bytes(b"PK\x03\x04" + b"0" * 100)
    (cuc / "add-visit-remembers.json").write_text(json.dumps({
        "title": "A visit remembers the vet who attended it", "path": ["Add a visit"],
        "file": "src/add-visit.feature", "line": 16, "status": "passed", "duration": 2300,
        "error": "", "trace": "add-visit-remembers.zip"}), encoding="utf-8")
    (cuc / "orphan.zip").write_bytes(b"PK")            # no sidecar: nobody knows whose
    (cuc / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "add-visit.feature").write_text("Feature: Add a visit\n", encoding="utf-8")
    out = tmp_path / ".human-review" / "assets"
    doc = pw.harvest(rep, out, "assets", tmp_path, tmp_path, 12, cuc)
    assert doc["recorded"] == 2
    feature = next(t for t in doc["tests"] if t["file"].endswith(".feature"))
    assert feature["file"] == "src/add-visit.feature" and feature["line"] == 16
    assert feature["path"] == ["Add a visit"] and feature["status"] == "passed"
    assert (out / "traces" / Path(feature["trace"]).name).is_file()
    assert not any("orphan" in t["trace"] for t in doc["tests"])
    # Nothing to read is nothing to add — not an error, and not a row.
    assert pw.harvest(rep, out, "assets", tmp_path, tmp_path, 12, tmp_path / "nope")["recorded"] == 1
