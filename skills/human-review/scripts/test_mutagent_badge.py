"""The 🧬 on a test row: tests.py says which tests met which mutants, testchapters.js draws it."""
import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("build_review_html", HERE / "build-review-html.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def _mutants(tmp_path, results):
    d = tmp_path / B.MUTAGENT_DIR
    d.mkdir()
    (d / "mutants.json").write_text(json.dumps({"head": "h", "tests": {}, "mutants": [
        {"id": "01-drop order", "title": "drop ORDER BY", "expect": ["T.java:10", "T.java:20"],
         "rationale": "unsorted"},
        {"id": "02-other", "title": "other", "expect": ["T.java:20"]},
        {"id": "03-broken", "title": "broken", "expect": ["T.java:30"]},
    ]}))
    (d / "results.json").write_text(json.dumps({"mutantsHead": "h", "mutants": results}))
    return tmp_path


def _marks(out):
    html = B.mutagent_marks(out)
    return json.loads(re.search(r'id="rm-mutagent">(.*)</script>', html).group(1)) if html else {}


def test_a_test_is_marked_by_each_mutant_it_met_worst_first(tmp_path):
    out = _mutants(tmp_path, {
        "01-drop order": {"verdict": "survived", "tests": {"T.java:10": "pass", "T.java:20": "pass"}},
        "02-other": {"verdict": "killed", "tests": {"T.java:20": "fail"}},
        "03-broken": {"verdict": "broken"},
    })
    got = _marks(out)
    assert got["T.java:10"] == [{"how": "survived", "title": "drop ORDER BY", "change": "",
                                 "why": "unsorted"}]
    assert [m["how"] for m in got["T.java:20"]] == ["survived", "killed"]
    assert "T.java:30" not in got                    # a broken mutant says nothing


def test_a_killed_mutant_marks_its_test_too(tmp_path):
    out = _mutants(tmp_path, {"01-drop order": {"verdict": "killed", "tests": {"T.java:10": "fail"}}})
    assert [m["how"] for m in _marks(out)["T.java:10"]] == ["killed"]
    assert B.mutagent_marks(tmp_path / "nowhere") == ""


def test_results_of_other_mutants_are_not_read(tmp_path):
    out = _mutants(tmp_path, {"01-drop order": {"verdict": "survived", "tests": {"T.java:10": "pass"}}})
    res = out / B.MUTAGENT_DIR / "results.json"
    res.write_text(res.read_text().replace('"h"', '"older"'))
    assert B.mutagent_marks(out) == ""


def test_the_row_draws_the_badge_before_its_file_link_red_or_green():
    js = (HERE / "hrbuild" / "assets" / "testchapters.js").read_text(encoding="utf-8")
    assert "getElementById('rm-mutagent')" in js
    assert "insertBefore(b, at)" in js and "'.rm-thead .rm-tw'" in js
    assert "b.dataset.mg = colour" in js
