#!/usr/bin/env python3
"""Both complexity engines against a corpus of hand-scored Java, side by side.

`testdata/complexity-corpus/` is a small project of tricky shapes — every kind of method
reference, overloads, nested/anonymous/local classes, records, enums with bodies, static
imports, brace-less nesting, switch expressions, Spring Data calls — each behind its own
entry point, with the expected flow written down in `expected.json` and the arithmetic in a
comment on every line that costs. The JavaParser engine must match all of it; the regex
engine is run too, so the table in `reference/complexity-engine-eval.md` can say where the
two part ways.

    python3 test_complexity_engines.py          # print the markdown table
    pytest test_complexity_engines.py           # assert the JavaParser engine is exact
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "testdata" / "complexity-corpus"
spec = importlib.util.spec_from_file_location("endpoint_complexity", HERE / "endpoint-complexity.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


def corpus_files() -> dict[str, str]:
    return {str(p.relative_to(CORPUS)): p.read_text(encoding="utf-8")
            for p in sorted(CORPUS.glob("**/src/main/java/**/*.java"))}


def expected() -> list[dict]:
    return json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))["cases"]


def by_key(entries: list[dict]) -> dict[str, dict]:
    return {f'{e["httpMethod"]} {e["path"]}': e for e in entries}


def method_names(entry: dict) -> Counter:
    """`Class.name` of every method in the flow, from its display (`Class.name(params)`)."""
    return Counter(f["display"].split("(")[0] for f in entry.get("flow") or [])


@lru_cache(maxsize=1)
def run_both() -> tuple[dict, dict | None]:
    files = corpus_files()
    old = by_key(ec.extract(files))
    new = ec.extract_javaparser(files)
    return old, (by_key(new) if new is not None else None)


def verdict(case: dict, got: dict | None) -> str:
    if got is None:
        return "missing"
    bad = []
    if got["flowCc"] != case["flowCc"]:
        bad.append(f'cc {got["flowCc"]}')
    if method_names(got) != Counter(case["methods"]):
        bad.append("flow")
    if "unresolved" in case and got.get("unresolvedCount", 0) != case["unresolved"]:
        bad.append(f'unresolved {got.get("unresolvedCount", "-")}')
    return "ok" if not bad else ", ".join(bad)


def table() -> str:
    old, new = run_both()
    rows = ["| case | shape | expected | regex | JavaParser |", "|---|---|---|---|---|"]
    for c in expected():
        o, n = old.get(c["key"]), (new or {}).get(c["key"])
        cell = lambda got: ("—" if got is None else f'{got["flowCc"]} · {verdict(c, got)}')  # noqa: E731
        rows.append(f'| `{c["key"]}` | {c["what"]} | {c["flowCc"]} | {cell(o)} | {cell(n)} |')
    ok_old = sum(verdict(c, old.get(c["key"])) == "ok" for c in expected())
    ok_new = sum(verdict(c, (new or {}).get(c["key"])) == "ok" for c in expected())
    rows.append(f"\n{len(expected())} cases — regex exact on {ok_old}, JavaParser exact on {ok_new}.")
    extra_old = sorted(set(old) - {c["key"] for c in expected()})
    extra_new = sorted(set(new or {}) - {c["key"] for c in expected()})
    if extra_old or extra_new:
        rows.append(f"Entry points not in the expectations — regex: {extra_old}, JavaParser: {extra_new}")
    return "\n".join(rows)


needs_jdk = pytest.mark.skipif(not ec.have_jdk(), reason="no JDK on the PATH")


@needs_jdk
@pytest.mark.parametrize("case", expected(), ids=lambda c: c["key"])
def test_the_javaparser_engine_matches_the_hand_computed_flow(case):
    _, new = run_both()
    assert new is not None, "the JavaParser engine did not run"
    assert verdict(case, new.get(case["key"])) == "ok", json.dumps(new.get(case["key"]), indent=1)[:3000]


@needs_jdk
def test_the_javaparser_engine_finds_exactly_the_expected_entry_points():
    old, new = run_both()
    assert set(new) == {c["key"] for c in expected()}


@needs_jdk
def test_every_entry_carries_the_shape_the_tab_reads():
    _, new = run_both()
    for e in new.values():
        assert {"kind", "httpMethod", "path", "handler", "metric", "flowCc", "methods", "flow",
                "engine", "unresolvedCount", "unresolved"} <= set(e)
        assert e["engine"] == "javaparser" and e["methods"] == len(e["flow"])
        assert e["flowCc"] == sum(f["cognitive"] for f in e["flow"])
        keys = {f["method"] for f in e["flow"]}
        for f in e["flow"]:
            assert f["cognitive"] == sum(h["inc"] for h in f["hits"])
            assert set(f["calls"]) <= keys
            for h in f["hits"]:
                assert {"file", "line", "code", "inc", "why"} <= set(h)
                assert (CORPUS / h["file"]).is_file() and h["line"] >= 1


_dspec = importlib.util.spec_from_file_location("endpoint_complexity_delta",
                                                HERE / "endpoint-complexity-delta.py")
delta = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(delta)


def _lede(page: str) -> str:
    lede = page[page.index('<p class="tabsub cx-lede">'):]
    return lede[:lede.index("</p>")]


def test_the_lede_names_the_engine_that_produced_the_numbers():
    assert "regular expressions" in _lede(delta.render([], ("regex", "regex")))
    jp = _lede(delta.render([], ("javaparser", "javaparser")))
    assert "JavaParser" in jp and "regular expressions" not in jp
    mixed = _lede(delta.render([], ("regex", "javaparser")))
    assert "different engines" in mixed


def _jp_row(unresolved):
    flow = [{"method": "p.A#h", "display": "A.h()", "cognitive": 1, "cyclomatic": 2,
             "calls": ["p.B#f(int)"], "hits": [], "file": "src/main/java/p/A.java", "line": 3},
            {"method": "p.B#f(int)", "display": "B.f(int)", "cognitive": 0, "cyclomatic": 1,
             "calls": [], "hits": [], "file": "src/main/java/p/B.java", "line": 9}]
    e = {"kind": "http", "httpMethod": "GET", "path": "/a", "handler": "A.h()", "flowCc": 1,
         "methods": 2, "flow": flow, "engine": "javaparser", "unresolved": unresolved,
         "unresolvedCount": len(unresolved)}
    return delta.compare({("http", "GET", "/a"): e}, {("http", "GET", "/a"): e})[0]


def test_calls_not_followed_are_counted_on_the_row_and_named_on_hover():
    lost = [{"from": "p.A#h", "call": "t.process()", "file": "src/main/java/p/A.java",
             "line": 7, "reason": "receiver type unknown"}]
    html_ = delta.render_row(_jp_row(lost), 10)
    assert '<span class="cx-nf"' in html_ and ">1 not followed</span>" in html_
    assert "A.java:7  t.process()  (receiver type unknown)" in html_
    assert "cx-nf" not in delta.render_row(_jp_row([]), 10)


def test_an_overload_keeps_its_parameters_in_the_graph_node():
    row = _jp_row([])
    row["graph"] = [dict(n, cognitive=1) for n in row["graph"]]  # draw both nodes
    out = delta._why_panel(row)
    assert "f(int)</span>" in out and "f(int)()" not in out


def test_the_declaration_line_from_the_snapshot_wins_over_a_search_by_name(tmp_path, monkeypatch):
    src = tmp_path / "src/main/java/p"
    src.mkdir(parents=True)
    (src / "B.java").write_text("package p;\nclass B {\n int f(String s) {return 0;}\n"
                                " int f(int i) {return 1;}\n}\n")
    monkeypatch.setattr(delta, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(delta, "DECL_AT", {"p.B#f(int)": ("src/main/java/p/B.java", 4)})
    delta.entry_source.cache_clear()
    try:
        assert delta.entry_source("p.B#f(int)") == (tmp_path / "src/main/java/p/B.java", 4)
    finally:
        delta.entry_source.cache_clear()


def test_two_entry_points_with_one_name_stay_two_rows_and_an_untouched_one_does_not_churn(tmp_path):
    one = {"kind": "listener", "httpMethod": "KAFKA", "path": "orders", "handler": "C.onA(String)",
           "flowCc": 1, "flow": []}
    two = {**one, "handler": "Audit.onB(String)", "flowCc": 2}
    (tmp_path / "b.json").write_text(json.dumps([one]))
    (tmp_path / "a.json").write_text(json.dumps([one, two]))
    rows = delta.compare(*delta.load_pair(tmp_path / "b.json", tmp_path / "a.json"))
    by = {r["path"]: r for r in rows}
    assert set(by) == {"orders · C.onA", "orders · Audit.onB"}
    assert by["orders · C.onA"]["delta"] == 0 and by["orders · Audit.onB"]["was"] is None


def test_a_link_opens_the_line_where_the_method_is_now(tmp_path, monkeypatch):
    flow = lambda line: [{"method": "p.C#f", "display": "C.f()", "cognitive": 0, "calls": [],  # noqa: E731
                          "hits": [], "file": "src/main/java/p/C.java", "line": line}]
    for name, line in (("before.json", 7), ("after.json", 17)):
        (tmp_path / name).write_text(json.dumps([{"kind": "http", "httpMethod": "GET", "path": "/c",
                                                  "flowCc": 0, "flow": flow(line)}]))
    monkeypatch.setattr(delta, "DECL_AT", {})
    delta.load(tmp_path / "before.json")
    delta.load(tmp_path / "after.json")
    assert delta.DECL_AT["p.C#f"] == ("src/main/java/p/C.java", 17)


def test_without_a_jdk_the_regex_engine_answers_and_says_so(monkeypatch):
    monkeypatch.setattr(ec, "have_jdk", lambda: False)
    entries = ec.measure(corpus_files(), "auto")
    assert entries and {e["engine"] for e in entries} == {"regex"}


if __name__ == "__main__":
    print(table())
    sys.exit(0)
