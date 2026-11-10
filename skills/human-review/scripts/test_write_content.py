"""write-content.py: the layout copied and pruned, the facts computed, the verdict checked."""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wc = _load("write_content_under_test", "write-content.py")


def test_the_example_is_a_layout_and_nothing_else():
    doc = json.loads(wc.EXAMPLE.read_text(encoding="utf-8"))
    assert doc["tabs"] and doc["tabs"][0]["id"] == "review"
    for key in ("title", "subtitle", "pr", "verdict", "footer", "summary"):
        assert key not in doc, f"the example carries {key}, which a run computes or judges"
    for pile in ("findings", "autofixes", "assumptions"):
        assert doc[pile] == {"auto": "review-points"}


def test_prune_drops_blocks_naming_files_the_repository_lacks(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "packages.puml").write_text("@startuml\n@enduml\n")
    (tmp_path / "backend").mkdir()
    spec = {"tabs": [{"id": "packages", "blocks": [
        {"type": "diagrams", "context": {"src": "docs/packages.puml"}},
        {"type": "diagrams", "context": {"src": "docs/gone.puml"}},
        {"type": "puml", "src": "docs/also-gone.puml"},
        {"type": "diagrams", "manifest": "assets/c2/MANIFEST.tsv"},
    ]}, {"id": "logging", "blocks": [
        {"type": "logging", "paths": ["backend", "frontend"]},
    ]}]}
    dropped = wc.prune(spec, tmp_path)
    kept = spec["tabs"][0]["blocks"]
    assert [b.get("context", {}).get("src") or b.get("manifest") for b in kept] == \
        ["docs/packages.puml", "assets/c2/MANIFEST.tsv"]
    assert spec["tabs"][1]["blocks"][0]["paths"] == ["backend"]
    assert len(dropped) == 3


def test_prune_css_drops_sheets_of_retired_producers_and_keeps_the_known_ones(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "custom.css").write_text("")
    spec = {"extraCss": ["assets/openapi-diff.css", "assets/openapi-compat.css",
                         "assets/custom.css"]}
    dropped = wc.prune_css(spec, tmp_path)
    assert spec["extraCss"] == ["assets/openapi-compat.css", "assets/custom.css"]
    assert dropped == ["extraCss assets/openapi-diff.css"]


def test_facts_fill_the_masthead_and_the_piles_are_always_the_branchs():
    spec = {"findings": [{"title": "typed by hand", "body": "x"}], "summary": "prose"}
    f = {"branch": "visits-paginated", "ticket": {"number": 52, "title": "Add pagination"},
         "pr": {"number": 53, "title": "Visits grid: paging"}}
    wc.apply_facts(spec, f, title="ignored when there is a PR")
    assert spec["title"] == "Visits grid: paging"
    assert spec["subtitle"] == "visits-paginated — issue #52"
    assert spec["findings"] == {"auto": "review-points"}
    assert "summary" not in spec
    assert spec["footer"].startswith("Built by /human-review from visits-paginated on ")


def test_without_a_pr_the_model_names_the_change():
    spec = {}
    wc.apply_facts(spec, {"branch": "b", "ticket": None, "pr": None}, title="Paged visits")
    assert spec["title"] == "Paged visits" and "pr" not in spec


def test_a_verdict_is_an_integer_score_and_a_list_of_sentences():
    assert wc.check_verdict({"score": 8, "bullets": ["one"]}) == []
    assert wc.check_verdict({"score": 8}) == []
    assert wc.check_verdict(None)
    assert wc.check_verdict({"score": "8"})
    assert wc.check_verdict({"score": 11})
    assert wc.check_verdict({"score": True})
    assert wc.check_verdict({"score": 7, "bullets": "one"})


def test_the_verdict_call_carries_nothing_it_does_not_need():
    argv = wc.lean_argv("sonnet")
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--system-prompt"):
        assert flag in argv


def test_the_prompt_is_addressed_to_the_model_not_to_a_reader():
    prompt = wc.build_prompt({"needTitle": False})
    assert prompt.startswith("You grade a change set")
    assert '"needTitle": false' in prompt
