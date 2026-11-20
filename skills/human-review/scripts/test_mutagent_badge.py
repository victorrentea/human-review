"""The 🧬 on a test row a mutant survived: tests.py says which tests, testchapters.js draws it."""
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
        {"id": "01-drop order", "title": "drop ORDER BY", "expect": ["T.java:10", "T.java:20"]},
        {"id": "02-other", "title": "other", "expect": ["T.java:20"]},
    ]}))
    (d / "results.json").write_text(json.dumps({"mutantsHead": "h", "mutants": results}))
    return tmp_path


def test_a_survivor_names_the_tests_that_stayed_green(tmp_path):
    out = _mutants(tmp_path, {
        "01-drop order": {"verdict": "survived", "tests": {"T.java:10": "pass"}},
        "02-other": {"verdict": "killed", "tests": {"T.java:20": "fail"}},
    })
    html = B.mutagent_survivors(out)
    body = re.search(r'id="rm-mutagent">(.*)</script>', html).group(1)
    # Anchor, title, and the change as the card draws it — empty here: these mutants have
    # no patch on disk.
    assert json.loads(body) == {"T.java:10": [["mg-01-drop-order", "drop ORDER BY", ""]]}
    # The anchor the badge jumps to is the card's row for that mutant.
    assert 'id="mg-01-drop-order"' in B.mutagent_card(out, tmp_path)


def test_nothing_survived_writes_nothing(tmp_path):
    out = _mutants(tmp_path, {"01-drop order": {"verdict": "killed", "tests": {"T.java:10": "fail"}}})
    assert B.mutagent_survivors(out) == ""
    assert B.mutagent_survivors(tmp_path / "nowhere") == ""


def test_results_of_other_mutants_are_not_read(tmp_path):
    out = _mutants(tmp_path, {"01-drop order": {"verdict": "survived", "tests": {"T.java:10": "pass"}}})
    res = out / B.MUTAGENT_DIR / "results.json"
    res.write_text(res.read_text().replace('"h"', '"older"'))
    assert B.mutagent_survivors(out) == ""


def test_the_row_draws_the_badge_before_its_file_link():
    js = (HERE / "hrbuild" / "assets" / "testchapters.js").read_text(encoding="utf-8")
    assert "getElementById('rm-mutagent')" in js
    assert "insertBefore(b, at)" in js and "'.rm-thead .rm-tw'" in js
