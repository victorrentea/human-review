#!/usr/bin/env python3
"""The Sequence tab's picture of one real trace: which trace `trace-shot.py` picks, and
how `run-steps.py` `_sequence` asks for it while the traced run's stack is still up.

No Grafana, no browser: the store is a fake that answers the two questions the script
asks of it, and the shell is a recorder. The screenshot itself is Playwright's job and is
looked at, not unit-tested.

Run with:  python3 -m pytest test_trace_shot.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shot = _load("trace-shot")
steps = _load("run-steps")

API = "petclinic-test/generated/AddVisitApiTest.java.adds-a-visit-to-an-existing-pet.genseq.puml"
OWN = ("petclinic-test/generated/OwnerListTest.java."
       "contentcarriesnestedpetstypesandvisits.genseq.puml")
UI = "petclinic-test/generated/owner-search.feature.sorting-by-city-descending.genseq.puml"


# ── which test a diagram is, and the name its run carries ───────────────────────────

def test_a_diagram_names_its_test_file_and_its_scenario():
    assert shot.scenario_of(API) == ("AddVisitApiTest.java", "adds-a-visit-to-an-existing-pet")
    assert shot.scenario_of("g/add-visit.spec.ts.add-a-visit.genseq.puml") == \
        ("add-visit.spec.ts", "add-a-visit")


def test_the_slug_matches_the_name_it_was_made_from_and_nothing_longer_or_shorter():
    """The pattern is the slug read back: words in order, any separator, any case — a
    JUnit `contentCarries…()` and a Gherkin `Sorting by city, descending` alike."""
    def hit(slug, name):
        return re.fullmatch(shot.name_pattern(slug), name) is not None
    assert hit("sorting-by-city-descending", "Sorting by city, descending")
    assert hit("contentcarriesnestedpetstypesandvisits", "contentCarriesNestedPetsTypesAndVisits()")
    assert hit("adds-a-visit-to-an-existing-pet", "adds a visit to an existing pet")
    assert not hit("adds-a-visit", "adds a visit to an existing pet")
    assert not hit("sorting-by-city-descending", "Sorting by city")
    q = shot.traceql("span.test.name", "sorting-by-city")
    assert q.startswith('{ span.test.name =~ "(?i)') and q.endswith('" }')


def test_spans_and_services_are_counted_in_either_shape_tempo_answers_in():
    v1 = {"batches": [
        {"resource": {"attributes": [{"key": "service.name",
                                      "value": {"stringValue": "petclinic-backend"}}]},
         "scopeSpans": [{"spans": [{}, {}, {}]}]},
        {"resource": {"attributes": [{"key": "service.name",
                                      "value": {"stringValue": "notification-service"}}]},
         "instrumentationLibrarySpans": [{"spans": [{}]}]}]}
    assert shot.span_stats(v1) == (4, ["petclinic-backend", "notification-service"])
    assert shot.span_stats({"resourceSpans": v1["batches"]})[0] == 4
    assert shot.span_stats({}) == (0, [])


class FakeGrafana:
    """Tempo behind Grafana, as far as `choose` asks: a search per scenario, a trace per id."""

    def __init__(self, by_slug: dict[str, list[tuple[str, int, list[str]]]]):
        self.by_slug, self.traces = by_slug, {}
        for found in by_slug.values():
            for tid, spans, services in found:
                self.traces[tid] = {"batches": [
                    {"resource": {"attributes": [{"key": "service.name",
                                                  "value": {"stringValue": s}}]},
                     "scopeSpans": [{"spans": [{}] * (spans // len(services))}]}
                    for s in services]}

    def search(self, uid, q, start, end):
        return [{"traceID": tid, "rootTraceName": "test"}
                for slug, found in self.by_slug.items()
                if shot.name_pattern(slug) in q for tid, _, _ in found]

    def trace(self, uid, tid):
        return self.traces[tid]


def _choose(g, diagrams, prefer=()):
    return shot.choose(g, "tempo", diagrams, set(prefer), "span.test.name", 0, 1,
                       log=lambda m: None)


def test_one_trace_for_one_diagram_beats_a_bigger_test_that_made_several():
    g = FakeGrafana({
        "sorting-by-city-descending": [("ui1", 10, ["front", "back"]),
                                       ("ui2", 12, ["front", "back"])],
        "adds-a-visit-to-an-existing-pet": [("api", 44, ["back", "notify"])]})
    pick = _choose(g, [UI, API])
    assert pick["diagram"] == API and pick["trace"] == "api" and pick["traces"] == 1


def test_the_branchs_own_test_beats_a_richer_one_it_did_not_write():
    g = FakeGrafana({
        "adds-a-visit-to-an-existing-pet": [("api", 44, ["back", "notify"])],
        "contentcarriesnestedpetstypesandvisits": [("own", 8, ["back"])]})
    assert _choose(g, [API, OWN])["trace"] == "api", "more services wins, all else equal"
    assert _choose(g, [API, OWN], prefer=[OWN])["trace"] == "own"


def test_a_test_with_only_several_traces_is_shown_by_its_biggest():
    g = FakeGrafana({"sorting-by-city-descending": [("small", 4, ["back"]),
                                                    ("big", 12, ["front", "back"])]})
    pick = _choose(g, [UI])
    assert pick["trace"] == "big" and pick["traces"] == 2


def test_no_trace_for_any_diagram_is_no_pick():
    assert _choose(FakeGrafana({}), [API, UI]) is None


def test_explore_opens_the_one_trace_in_kiosk_mode():
    url = shot.explore_url("http://127.0.0.1:3300/", "tempo", "abc123")
    assert url.startswith("http://127.0.0.1:3300/explore?") and "&kiosk&" in url
    panes = json.loads(urllib.parse.unquote(url.split("panes=", 1)[1]))
    assert panes["t"]["queries"][0]["query"] == "abc123"
    assert panes["t"]["queries"][0]["datasource"] == {"type": "tempo", "uid": "tempo"}


# ── asked for by the sequence step, while the stack is up ───────────────────────────

class Recorder:
    def __init__(self, answers=()):
        self.answers, self.ran = list(answers), []

    def __call__(self, cmd, ctx, check=True, capture=False):
        self.ran.append(cmd)
        for needle, code, out in self.answers:
            if needle in cmd:
                return subprocess.CompletedProcess(cmd, code, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _app(**vars_):
    app = steps.AppInstance("http://localhost:1", "", True)
    app.vars = dict(vars_)
    return app


def test_the_step_shoots_from_the_stacks_own_grafana_among_what_it_drew(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    sh = Recorder([("playwright-python.sh", 0, "/venv/bin/python\n")])
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {}}, dry=False)

    steps._trace_shot(ctx, {}, _app(GRAFANA_URL="http://127.0.0.1:52109"), [API, OWN],
                      [OWN], 1791100000)

    ran = next(c for c in sh.ran if "trace-shot.py" in c)
    assert ran.startswith("/venv/bin/python ")
    assert "--grafana http://127.0.0.1:52109" in ran and "--since 1791100000" in ran
    assert f"--diagram {API}" in ran and f"--prefer {OWN}" in ran
    assert "--out .human-review/assets/sequence.trace.png" in ran
    assert ctx.notes == []


def test_no_grafana_is_a_note_and_never_a_browser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    sh = Recorder()
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {}}, dry=False)
    steps._trace_shot(ctx, {"trace": {"attribute": "span.test.name"}}, _app(), [API], [], 0)
    assert sh.ran == [] and "no Grafana" in ctx.notes[0]

    # No trace store asked for (the pytest plugin's suites): nothing to miss, no note.
    ctx = steps.Ctx("origin/main", {"steps": {}}, dry=False)
    steps._trace_shot(ctx, {}, _app(), [API], [], 0)
    assert sh.ran == [] and ctx.notes == []

    # Turned off, or nothing drawn: not even a note.
    ctx = steps.Ctx("origin/main", {"steps": {}}, dry=False)
    steps._trace_shot(ctx, {"trace": False}, _app(GRAFANA_URL="http://g"), [API], [], 0)
    steps._trace_shot(ctx, {}, _app(GRAFANA_URL="http://g"), [], [], 0)
    assert sh.ran == [] and ctx.notes == []


def test_a_configured_grafana_and_attribute_are_the_ones_asked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sh = Recorder([("playwright-python.sh", 0, "/venv/bin/python\n")])
    monkeypatch.setattr(steps, "sh", sh)
    ctx = steps.Ctx("origin/main", {"steps": {}}, dry=False)
    steps._trace_shot(ctx, {"trace": {"grafana": "{TRACES_UI}", "attribute": "span.test"}},
                      _app(TRACES_UI="http://127.0.0.1:9"), [API], [], 0)
    ran = next(c for c in sh.ran if "trace-shot.py" in c)
    assert "--grafana http://127.0.0.1:9" in ran and "--attribute span.test" in ran


def test_the_branchs_own_diagrams_are_found_by_test_file_and_scenario_name():
    selection = {"picked": [
        {"path": "petclinic-backend/src/test/java/p/OwnerListTest.java",
         "name": "contentCarriesNestedPetsTypesAndVisits"},
        {"path": "petclinic-test/src/owner-search.feature",
         "name": "Sorting by city, descending"}]}
    assert steps.prefer_diagrams([API, OWN, UI], selection) == [OWN, UI]
    assert steps.prefer_diagrams([API], None) == []


def test_a_new_run_starts_without_the_last_runs_shot(tmp_path, monkeypatch):
    """A shot is this run's or nothing: a run that is skipped must not leave the previous
    run's trace on the tab, offered as evidence of this one."""
    (tmp_path / ".human-review/assets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for p in steps.TRACE_SHOT_FILES:
        p.write_text("old")
    monkeypatch.setattr(steps, "sh", Recorder([("run-tests", 1, "")]))
    ctx = steps.Ctx("origin/main", {"steps": {"sequence": {"commands": ["./run-tests"]}}},
                    dry=False)
    try:
        steps._sequence(ctx)
    except LookupError:
        pass
    assert not any(p.exists() for p in steps.TRACE_SHOT_FILES)


def test_the_shot_names_what_the_tab_reads():
    """Two modules, one file name: the step writes it, the tab reads it."""
    seq = importlib.import_module("hrbuild.tabs.sequence")
    assert str(steps.TRACE_SHOT) == ".human-review/" + seq.TRACE_SHOT
    assert str(steps.TRACE_SHOT_FILES[1]) == ".human-review/" + seq.TRACE_SHOT_META
    assert shot.DEFAULT_OUT == ".human-review/" + seq.TRACE_SHOT
