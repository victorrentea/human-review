"""A run whose producers skipped a tab builds a page without it, instead of refusing."""
from __future__ import annotations

from hrbuild.shared.validate import drop_unproduced, validate

SPEC = {
    "codecity": {"png": "assets/codecity.png", "href": "assets/codecity/codecity.html"},
    "playwrightTraces": "assets/traces.json",
    "extraCss": ["assets/openapi-compat.css", "assets/kept.css"],
    "sections": [{"id": "api", "title": "", "includeHtml": "assets/openapi-compat.html"}],
    "tabs": [{"id": "requirements", "label": "Tests", "blocks": [{"type": "traces"}]}],
}


def _spec():
    import json
    return json.loads(json.dumps(SPEC))


def test_before_any_run_a_missing_artifact_is_still_a_problem(tmp_path):
    spec = _spec()
    assert drop_unproduced(spec, tmp_path) == []
    assert validate(spec, tmp_path)


def test_after_a_run_what_it_did_not_write_is_dropped_and_named(tmp_path):
    (tmp_path / ".steps-status.json").write_text("[]")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "kept.css").write_text("")
    spec = _spec()
    dropped = drop_unproduced(spec, tmp_path)
    assert "codecity" not in spec and "playwrightTraces" not in spec
    assert spec["extraCss"] == ["assets/kept.css"]
    assert "includeHtml" not in spec["sections"][0]
    assert spec["tabs"][0]["blocks"] == []
    assert len(dropped) == 5
    assert validate(spec, tmp_path) == []
