#!/usr/bin/env python3
"""The API tab's Diagram view: a payload as a class diagram, and its delta on the Data tab's
differ. Run directly under pytest; the rendering tests skip without PlantUML.

The delta itself belongs to `puml_diff.py` and is tested there. What is pinned here is what
is ours: which classes a schema makes, what the two sides are compared against, and how the
page's weight is kept down.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import schema_puml as sp  # noqa: E402

GREEN, RED = "<color:#2E7D32>", "<color:#C62828>"


def spec(**schemas) -> dict:
    return {"components": {"schemas": schemas}}


def ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def puml(schemas: dict, root: dict) -> str:
    return sp.schema_puml(spec(**schemas), root, "Response")


PET = {"type": "object", "required": ["id"], "properties": {
    "id": {"type": "integer", "format": "int32"},
    "birthDate": {"type": "string", "format": "date"},
    "name": {"type": "string"},
    "type": ref("PetType"),
    "visits": {"type": "array", "items": ref("Visit")},
}}
PET_TYPE = {"type": "object", "properties": {"name": {"type": "string"}}}
VISIT = {"type": "object", "properties": {"id": {"type": "integer"}}}
SCHEMAS = {"Pet": PET, "PetType": PET_TYPE, "Visit": VISIT}


# ── from a schema to classes ────────────────────────────────────────────────
def test_one_class_per_object_with_its_plain_fields_and_their_types():
    text = puml(SCHEMAS, {"type": "array", "items": ref("Pet")})
    pet = text.split("class Pet {")[1].split("}")[0].split("\n")
    assert [m.strip() for m in pet if m.strip()] == [
        "id* : int32", "birthDate : date", "name : string"]
    assert "class PetType {" in text and "class Visit {" in text


def test_a_nested_object_and_array_items_are_associations_not_fields():
    text = puml(SCHEMAS, ref("Pet"))
    assert 'Pet --> "0..1 type" PetType' in text
    assert 'Pet --> "~* visits" Visit' in text
    assert "visits :" not in text


def test_a_required_association_says_so():
    text = puml({"A": {"type": "object", "required": ["b"], "properties": {"b": ref("B")}},
                 "B": PET_TYPE}, ref("A"))
    assert 'A --> "1 b" B' in text


def test_a_schema_that_reaches_itself_is_one_class():
    node = {"type": "object", "properties": {"parent": ref("Node"), "kids": {
        "type": "array", "items": ref("Node")}}}
    text = puml({"Node": node}, ref("Node"))
    assert text.count("class Node {") == 1
    assert 'Node --> "0..1 parent" Node' in text and 'Node --> "~* kids" Node' in text


def test_all_of_folds_into_the_class_that_names_it():
    base = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    text = puml({"Base": base, "Cat": {"allOf": [ref("Base"), {
        "type": "object", "properties": {"lives": {"type": "integer"}}}]}}, ref("Cat"))
    assert "class Cat {" in text and "id* : string" in text and "lives : integer" in text
    assert "class Base" not in text


def test_a_lone_all_of_lends_its_name():
    text = puml({"Owner": {"type": "object", "properties": {"pet": {
        "allOf": [ref("Pet")], "description": "theirs"}}}, "Pet": PET_TYPE}, ref("Owner"))
    assert 'Owner --> "0..1 pet" Pet' in text


def test_one_of_reaches_every_variant():
    text = puml({"Box": {"type": "object", "properties": {"pay": {
        "oneOf": [ref("Card"), ref("Cash")]}}},
        "Card": PET_TYPE, "Cash": VISIT}, ref("Box"))
    assert 'Box --> "0..1 pay" Card' in text and 'Box --> "0..1 pay" Cash' in text


def test_an_inline_object_is_named_after_its_field():
    text = puml({"Pet": {"type": "object", "properties": {"chip": {
        "type": "object", "properties": {"code": {"type": "string"}}}}}}, ref("Pet"))
    assert 'class "Pet.chip" as Pet_chip {' in text
    assert 'Pet --> "0..1 chip" Pet_chip' in text


def test_a_named_enum_is_a_class_of_values_and_an_inline_one_rides_on_its_field():
    text = puml({"Pet": {"type": "object", "properties": {
        "kind": ref("Kind"),
        "mood": {"type": "string", "enum": ["CALM", "WILD"]}}},
        "Kind": {"type": "string", "enum": ["CAT", "DOG"]}}, ref("Pet"))
    assert "enum Kind {\n  CAT\n  DOG\n}" in text
    assert "mood : string {CALM, WILD}" in text


def test_a_map_nullable_and_an_array_of_scalars_keep_one_word_each():
    text = puml({"Pet": {"type": "object", "properties": {
        "tags": {"type": "array", "items": {"type": "string"}},
        "nick": {"type": ["string", "null"]},
        "scores": {"type": "object", "additionalProperties": {"type": "integer"}},
        "extra": {"type": "object"}}}}, ref("Pet"))
    assert "tags : string[]" in text and "nick : string?" in text
    assert "scores : map[integer]" in text and "extra : object" in text


def test_a_name_plantuml_would_split_gets_an_alias():
    text = puml({"com.acme.Pet": PET_TYPE}, ref("com.acme.Pet"))
    assert 'class "com.acme.Pet" as com_acme_Pet {' in text


@pytest.mark.parametrize("root", [{"type": "string"},
                                  {"type": "array", "items": {"type": "integer"}}, {}])
def test_a_body_with_no_object_has_no_diagram(root):
    assert puml({}, root) is None


# ── the delta ───────────────────────────────────────────────────────────────
def sides(before: dict, after: dict, root=None):
    root = root or ref("Pet")
    return puml(before, root), puml(after, root)


def test_an_arrow_enters_the_payload_saying_one_or_many():
    one = puml(SCHEMAS, ref("Pet"))
    assert 'class Response {' in one and 'Response --> "1" Pet' in one
    assert 'Response --> "~*" Pet' in puml(SCHEMAS, {"type": "array", "items": ref("Pet")})


def test_an_inline_payload_is_the_entry_itself():
    text = puml({}, {"type": "object", "properties": {"id": {"type": "integer"}}})
    assert text.count("class Response {") == 1 and "Response -->" not in text


def test_a_list_that_became_a_page_object_shows_on_the_arrow():
    page = {"type": "object", "properties": {"content": {"type": "array", "items": ref("Pet")}}}
    schemas = {**SCHEMAS, "PetPage": page}
    old = puml(schemas, {"type": "array", "items": ref("Pet")})
    text, changed = sp.delta(old, puml(schemas, ref("PetPage")))
    assert changed
    assert re.search(r'Response -\[#2E7D32[^\]]*\]-> "1" PetPage', text), text
    assert re.search(r'Response -\[#C62828[^\]]*\]-> "~\*" Pet : ', text), text


def test_a_new_field_is_green_and_the_rest_of_the_class_is_not():
    after = {**SCHEMAS, "Visit": {"type": "object", "properties": {
        "id": {"type": "integer"}, "vetId": {"type": "integer"}}}}
    text, changed = sp.delta(*sides(SCHEMAS, after))
    assert changed
    assert f"{GREEN}vetId : integer</color>" in text
    assert f"{GREEN}id : integer" not in text and RED not in text.split("hide")[1]


def test_a_removed_field_is_red_and_struck():
    after = {**SCHEMAS, "Visit": {"type": "object", "properties": {}}}
    text, changed = sp.delta(*sides(SCHEMAS, after))
    assert changed and f"{RED}<s>id : integer</s></color>" in text


def test_a_new_class_and_its_association_are_green():
    after = {**SCHEMAS, "Pet": {**PET, "properties": {**PET["properties"], "vet": ref("Vet")}},
             "Vet": PET_TYPE}
    text, _ = sp.delta(*sides(SCHEMAS, after))
    assert "class Vet #line:2E7D32;text:2E7D32 {" in text
    assert re.search(r'Pet -\[#2E7D32,thickness=2\]-> "0\.\.1 vet" Vet', text), text


def test_an_unchanged_body_is_drawn_plain():
    text, changed = sp.delta(*sides(SCHEMAS, SCHEMAS))
    assert not changed and "#" not in text and "caption" not in text


def test_a_body_the_base_never_had_is_drawn_plain():
    text, changed = sp.delta(None, puml(SCHEMAS, ref("Pet")), absent=True)
    assert not changed and "#" not in text


def test_a_removed_body_is_all_struck_and_keeps_the_preamble():
    text, changed = sp.delta(puml(SCHEMAS, ref("Pet")), None)
    assert changed and "skinparam classAttributeIconSize 0" in text
    assert text.count("<s>") >= 4


def test_a_field_named_like_a_directive_stays_in_its_class():
    problem = {"type": "object", "properties": {"title": {"type": "string"},
                                                "header": {"type": "string"}}}
    after = {"Problem": {"type": "object", "properties": {
        **problem["properties"], "detail": {"type": "string"}}}}
    text, _ = sp.delta(*sides({"Problem": problem}, after, ref("Problem")))
    body = text.split("class Problem")[1]
    assert "title : string" in body and "header : string" in body


# ── planning a page ─────────────────────────────────────────────────────────
def op(status_schema: dict, media: str = "application/json", request=None) -> dict:
    out = {"responses": {"200": {"content": {media: {"schema": status_schema}}}}}
    if request:
        out["requestBody"] = {"content": {media: {"schema": request}}}
    return out


def test_identical_diagrams_are_one_picture():
    s = spec(**SCHEMAS)
    ops = {"GET /a": op(ref("Pet")), "GET /b": op(ref("Pet")),
           "POST /a": op(ref("Pet"), request=ref("Pet"))}
    diagrams, per_op, changed = sp.plan(s, s, ops, ops)
    assert len(diagrams) == 2 and not changed, "the same Pet, as a response and as a request"
    assert set(per_op["POST /a"]) == {"request application/json", "200 application/json"}
    assert per_op["GET /a"] == per_op["GET /b"]


def test_a_body_reached_through_a_ref_is_found():
    s = {**spec(**SCHEMAS), "components": {"schemas": SCHEMAS, "responses": {
        "Ok": {"content": {"*/*": {"schema": ref("Pet")}}}}}}
    ops = {"GET /a": {"responses": {200: {"$ref": "#/components/responses/Ok"}}}}
    _, per_op, _ = sp.plan(s, s, ops, ops)
    assert list(per_op["GET /a"]) == ["200 */*"]


def test_a_swapped_media_type_is_compared_with_what_it_replaced():
    old = spec(**SCHEMAS)
    new = spec(**{**SCHEMAS, "Visit": PET_TYPE})
    diagrams, per_op, changed = sp.plan(
        old, new, {"GET /a": op(ref("Pet"), "*/*")},
        {"GET /a": op(ref("Pet"), "application/problem+json")})
    only = per_op["GET /a"]["200 application/problem+json"]
    assert only in changed and GREEN in diagrams[only]


def test_a_removed_operation_is_drawn_struck_and_an_added_one_plain():
    s = spec(**SCHEMAS)
    diagrams, per_op, changed = sp.plan(
        s, s, {"GET /gone": op(ref("Pet"))}, {"GET /new": op(ref("Pet"))})
    gone, new = per_op["GET /gone"]["200 application/json"], per_op["GET /new"]["200 application/json"]
    assert gone in changed and "<s>" in diagrams[gone]
    assert new not in changed and "<s>" not in diagrams[new]


def test_changed_bodies_are_served_first_when_the_cap_bites():
    diagrams = {"a": "plain", "b": "moved"}
    per_op = {"GET /a": {"200 */*": "a"}, "GET /b": {"200 */*": "b"}}
    svgs, kept = sp.allocate(diagrams, per_op, {"b"}, render=lambda d: dict(d), limit=1)
    assert set(svgs) == {"b"} and set(kept) == {"GET /b"}


def test_the_byte_budget_drops_what_does_not_fit():
    diagrams = {"a": "x", "b": "y"}
    per_op = {"GET /a": {"200 */*": "a", "404 */*": "b"}}
    svgs, kept = sp.allocate(diagrams, per_op, set(), render=lambda d: {k: "z" * 60 for k in d},
                             budget=100)
    assert len(svgs) == 1 and len(kept["GET /a"]) == 1


def test_no_plantuml_means_no_diagrams_and_no_failure(monkeypatch):
    monkeypatch.setattr(sp.shutil, "which", lambda _: None)
    assert sp.render_svgs({"a": sp.EMPTY}) == {}
    assert sp.allocate({"a": sp.EMPTY}, {"GET /a": {"200 */*": "a"}}, set()) == ({}, {})


def test_a_plantuml_that_dies_is_no_failure(monkeypatch):
    monkeypatch.setattr(sp.shutil, "which", lambda _: "/bin/plantuml")

    def boom(*_a, **_k):
        raise OSError("no such file")
    monkeypatch.setattr(sp.subprocess, "run", boom)
    assert sp.render_svgs({"a": sp.EMPTY}) == {}


plantuml = pytest.mark.skipif(not shutil.which("plantuml"), reason="plantuml not installed")


@plantuml
def test_a_delta_renders_themed_and_without_its_comments():
    text, _ = sp.delta(*sides(SCHEMAS, {**SCHEMAS, "Visit": PET_TYPE}))
    svg = sp.render_svgs({"a": text})["a"]
    assert "var(--dgm-diff-add)" in svg and "var(--dgm-ripple-1)" in svg
    assert "<!--" not in svg and "<?plantuml" not in svg and "</script" not in svg.lower()


@plantuml
def test_what_plantuml_cannot_parse_is_left_out():
    assert sp.render_svgs({"bad": "@startuml\nclass {{{\n@enduml\n"}) == {}


# ── the page ────────────────────────────────────────────────────────────────
def _ovd():
    spec_ = importlib.util.spec_from_file_location("ovd_schema", HERE / "openapi-visual-diff.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


def test_a_copy_opened_alone_carries_the_page_palette():
    """Embedded, the variables inherit from core.css; alone, the template has to have them."""
    template = _ovd().TEMPLATE
    core = (HERE / "hrbuild" / "assets" / "css" / "core.css").read_text(encoding="utf-8")
    light, dark = core.split("@media (prefers-color-scheme: dark)", 1)

    def values(text):
        return dict(re.findall(r"(--dgm-[a-z0-9-]+):\s*(#[0-9a-f]+)", text))

    block = template.split("The diagrams' colours")[1].split("body { margin: 0")[0]
    mine_light, mine_dark = block.split("@media (prefers-color-scheme: dark)", 1)
    for name, value in values(mine_light).items():
        assert values(light)[name] == value, name
    for name, value in values(mine_dark).items():
        assert values(dark)[name] == value, name
    # What a class diagram of ours is themed to: PlantUML's box, the differ's palette.
    assert {"--dgm-bg", "--dgm-box", "--dgm-line", "--dgm-fg", "--dgm-icon", "--dgm-diff-add",
            "--dgm-diff-del", "--dgm-ripple-1", "--dgm-ripple-2",
            "--dgm-ripple-3"} <= set(values(mine_light))


def test_no_diagrams_leaves_the_payload_without_a_switch(monkeypatch):
    ovd = _ovd()
    monkeypatch.setattr(sp.shutil, "which", lambda _: None)
    entries = {"GET /a": {"state": "untouched", "changes": [], "ghosts": []}}
    s = spec(**SCHEMAS)
    s["paths"] = {"/a": {"get": op(ref("Pet"))}}
    assert ovd.attach_diagrams(s, s, entries) == {}
    assert "diagrams" not in entries["GET /a"]
    assert '"diagrams": null' in ovd.render({"paths": {}}, entries, [], {}, "a", "b", {})
