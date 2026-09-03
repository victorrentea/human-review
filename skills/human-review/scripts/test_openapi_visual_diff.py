#!/usr/bin/env python3
"""The path from "this operation changed" to "this field, right here", pinned.

The complaint this guards: the branch added `vetId` / `vetFirstName` / `vetLastName` to
`VisitDto`, and on the visual diff they were findable only by opening an operation,
switching to the Schema tab, and hand-expanding four nested nodes. "expand impacted"
looked like the control for exactly that and was not — it opened the *operations* and
stopped, leaving the schema collapsed underneath.

The fix computes the ancestor chain in the generator, from oasdiff's own property path,
resolved against the already-inlined schema. That is the part worth testing without a
browser: if the chain is wrong, the page opens the wrong branch and marks the wrong
field, and no amount of DOM cleverness saves it.

The failure mode to fear is the half-open tree — a chain that resolves for four of five
fields and silently stops for the fifth teaches the reader that what is on screen is
everything. So the counting of what was *not* opened is pinned as hard as the opening.

Run directly (`python3 test_openapi_visual_diff.py`) or under pytest.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(stem: str, filename: str):
    spec = importlib.util.spec_from_file_location(stem, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module
    spec.loader.exec_module(module)
    return module


ovd = _load("openapi_visual_diff", "openapi-visual-diff.py")


# ── the fixture: petclinic's real ref topology, three levels of nesting ──────────
# GET /api/owners returns an ARRAY of OwnerDto; OwnerDto.pets is an array of PetDto;
# PetDto.visits is an array of VisitDto. So the field the branch added sits behind
# items -> pets -> items -> visits -> items -> vetId, which is precisely the chain the
# reader was being asked to walk by hand.
def spec(with_vet: bool) -> dict:
    visit = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "description": {"type": "string"}},
        "required": ["description"],
    }
    if with_vet:
        visit["properties"]["vetId"] = {"type": "integer"}
        visit["properties"]["vetFirstName"] = {"type": "string"}
        visit["properties"]["vetLastName"] = {"type": "string"}
    return {
        "openapi": "3.0.1",
        "info": {"title": "petclinic", "version": "1"},
        "paths": {
            "/api/owners": {
                "get": {
                    "tags": ["owner"],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"schema": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/OwnerDto"}}}}}},
                }
            },
            "/api/visits": {
                "post": {
                    "tags": ["visit"],
                    "requestBody": {"content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/VisitDto"}}}},
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"schema": {
                            "$ref": "#/components/schemas/VisitDto"}}}}},
                }
            },
        },
        "components": {"schemas": {
            "OwnerDto": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "pets": {"type": "array", "items": {"$ref": "#/components/schemas/PetDto"}}}},
            "PetDto": {"type": "object", "properties": {
                "name": {"type": "string"},
                "visits": {"type": "array",
                           "items": {"$ref": "#/components/schemas/VisitDto"}}}},
            "VisitDto": visit,
        }},
    }


VET_FIELDS = ("vetId", "vetFirstName", "vetLastName")
DEEP_PATH = "items/pets/items/visits/items/{}"


def owners_response_schema(inlined: dict) -> dict:
    return (inlined["paths"]["/api/owners"]["get"]["responses"]["200"]
            ["content"]["application/json"]["schema"])


def changelog(with_vet: bool = True) -> list:
    """oasdiff's own shape, wording taken verbatim from its output on this change."""
    out = []
    for field in VET_FIELDS:
        out.append({
            "id": "response-optional-property-added",
            "text": f"added the optional property `{DEEP_PATH.format(field)}` "
                    "to the response with the `200` status",
            "level": 1, "operation": "GET", "path": "/api/owners", "section": "paths",
        })
        out.append({
            "id": "new-optional-request-property",
            "text": f"added the new optional request property `{field}`",
            "level": 1, "operation": "POST", "path": "/api/visits", "section": "paths",
        })
    return out


# ── the chain ────────────────────────────────────────────────────────────────────
def test_the_chain_reaches_a_field_three_arrays_down():
    """The whole complaint in one assertion: the generator knows the way down."""
    inlined = ovd.inline_refs(spec(True))
    steps = ovd.resolve_steps(owners_response_schema(inlined),
                              DEEP_PATH.format("vetId").split("/"))
    assert steps == [
        {"kind": "items"},
        {"kind": "prop", "name": "pets"},
        {"kind": "items"},
        {"kind": "prop", "name": "visits"},
        {"kind": "items"},
        {"kind": "prop", "name": "vetId"},
    ], steps
    # Six nodes is six clicks by hand, which is why nobody found the field.
    assert len(steps) == 6


def test_a_path_that_does_not_resolve_yields_no_target():
    """A removed property is not in the spec being rendered. Pointing at where it used
    to be would open a branch and mark nothing — worse than leaving it in the note."""
    inlined = ovd.inline_refs(spec(False))          # the base spec: no vet fields
    schema = owners_response_schema(inlined)
    assert ovd.resolve_steps(schema, DEEP_PATH.format("vetId").split("/")) is None
    # A typo'd intermediate hop fails too, rather than resolving to something near it.
    assert ovd.resolve_steps(schema, ["items", "pet", "items"]) is None
    # An array descent that the path forgot to spell out is not guessed at.
    assert ovd.resolve_steps(schema, ["items", "pets", "visits"]) is None


def test_a_recursive_schema_stops_instead_of_expanding_forever():
    """Owner -> Pet -> Owner. `inline_refs` already plants a stub at the loop, and the
    stub has no properties, so a path through it simply does not resolve."""
    looping = spec(True)
    looping["components"]["schemas"]["PetDto"]["properties"]["owner"] = {
        "$ref": "#/components/schemas/OwnerDto"}
    inlined = ovd.inline_refs(looping)               # must terminate at all
    schema = owners_response_schema(inlined)
    # One hop into the loop is real and resolves...
    assert ovd.resolve_steps(schema, ["items", "pets", "items", "owner"]) is not None
    # ...and the second time round is the stub, which carries no properties to descend.
    deeper = ["items", "pets", "items", "owner", "pets"]
    assert ovd.resolve_steps(schema, deeper) is None


def test_a_chain_longer_than_the_bound_is_refused_whole():
    """A half-walked chain is the failure this feature exists to remove, so a path past
    the bound produces no target rather than a partial one."""
    node = {"type": "object"}
    root = node
    for i in range(ovd.MAX_REVEAL_DEPTH + 4):
        child = {"type": "object", "properties": {}}
        node["properties"] = {f"p{i}": child}
        node = child
    within = [f"p{i}" for i in range(ovd.MAX_REVEAL_DEPTH)]
    assert len(ovd.resolve_steps(root, within)) == ovd.MAX_REVEAL_DEPTH
    assert ovd.resolve_steps(root, within + ["p12"]) is None


# ── which schema the chain starts in ─────────────────────────────────────────────
def test_the_target_names_the_side_and_the_status_it_belongs_to():
    inlined = ovd.inline_refs(spec(True))
    get_owners = inlined["paths"]["/api/owners"]["get"]
    post_visits = inlined["paths"]["/api/visits"]["post"]

    resp = ovd.change_target(get_owners, changelog()[0])
    assert resp["in"] == "response" and resp["status"] == "200"
    assert resp["field"] == "vetId"

    req = ovd.change_target(post_visits, changelog()[1])
    assert req["in"] == "request" and "status" not in req
    assert req["steps"] == [{"kind": "prop", "name": "vetId"}]

    # The status is read off the prose, not assumed to be 200.
    other = dict(changelog()[0])
    other["text"] = other["text"].replace("`200`", "`201`")
    assert ovd.change_target(get_owners, other) is None, "no 201 response to point at"


def test_a_change_that_names_no_field_gets_no_target():
    """"the response media type changed" is about the operation, not a field. A target
    on it would open a schema for no reason."""
    inlined = ovd.inline_refs(spec(True))
    op = inlined["paths"]["/api/owners"]["get"]
    assert ovd.change_target(op, {
        "id": "response-media-type-removed+response-media-type-added",
        "text": "the response media type for status `200` changed from `a` to `b`",
        "level": 3}) is None


def test_the_leaf_word_comes_from_the_rule_not_from_prose():
    assert ovd.change_mark("response-optional-property-added") == "added"
    assert ovd.change_mark("new-optional-request-property") == "added"
    assert ovd.change_mark("response-property-removed") == "removed"
    assert ovd.change_mark("response-property-type-changed") == "changed"


# ── the model the page is handed ─────────────────────────────────────────────────
def build():
    return ovd.build_model(spec(False), spec(True), changelog())


def test_every_added_field_arrives_with_a_way_to_reach_it():
    _, entries, _, _ = build()
    owners = entries["GET /api/owners"]
    assert owners["state"] == "modified"
    assert len(owners["changes"]) == 3
    for c in owners["changes"]:
        assert c["target"], f"no way down to {c['text']}"
        assert c["target"]["steps"][-1]["kind"] == "prop"
        assert c["target"]["steps"][-1]["name"] in VET_FIELDS
        assert c["mark"] == "added"
    assert owners["deepSkipped"] == 0, "nothing was dropped, so nothing may be claimed"

    visits = entries["POST /api/visits"]
    # The same three fields, reached through the request body this time.
    assert {c["target"]["in"] for c in visits["changes"]} == {"request"}


def test_what_is_not_opened_is_counted_rather_than_hidden():
    """Past the per-operation cap the tree stops opening. A reader looking at twelve
    open fields must not conclude there were twelve."""
    over = ovd.MAX_REVEAL_PER_OP + 5
    wide = spec(True)
    props = wide["components"]["schemas"]["VisitDto"]["properties"]
    for i in range(over):
        props[f"extra{i}"] = {"type": "string"}
    changes = [{
        "id": "response-optional-property-added",
        "text": f"added the optional property `{DEEP_PATH.format('extra' + str(i))}` "
                "to the response with the `200` status",
        "level": 1, "operation": "GET", "path": "/api/owners", "section": "paths",
    } for i in range(over)]

    _, entries, _, _ = ovd.build_model(spec(False), wide, changes)
    owners = entries["GET /api/owners"]
    opened = [c for c in owners["changes"] if c["target"]]
    assert len(opened) == ovd.MAX_REVEAL_PER_OP
    assert owners["deepSkipped"] == over - ovd.MAX_REVEAL_PER_OP
    # Every change is still listed in prose; only the auto-opening is rationed.
    assert len(owners["changes"]) == over


def test_a_removed_field_is_listed_but_never_pretends_to_be_reachable():
    removal = [{
        "id": "response-property-removed",
        "text": "removed the property `items/pets/items/visits/items/gone` from the "
                "response with the `200` status",
        "level": 3, "operation": "GET", "path": "/api/owners", "section": "paths",
    }]
    _, entries, _, _ = ovd.build_model(spec(True), spec(True), removal)
    owners = entries["GET /api/owners"]
    assert owners["changes"][0]["target"] is None
    assert owners["deepSkipped"] == 1, "a field with no way down has to be admitted to"


# ── the two halves have to stay wired to each other ──────────────────────────────
def test_the_page_walks_the_steps_the_generator_emits():
    """The chain is useless if the template stopped calling the walker. Both kinds of
    step the resolver can emit must be handled on the page."""
    tpl = ovd.TEMPLATE
    assert "revealImpacted()" in tpl, "the expand toggle no longer reveals anything"
    assert "c.target" in tpl and "target.steps" in tpl
    assert tpl.count("step.kind === 'items'") == 2, "array descents in both renderers"
    # Lazily rendered children: the walker must wait for a node, never assume it.
    assert "waitFor(" in tpl and "STEP_WAIT" in tpl
    # A superseded run has to stop rather than fight the reader.
    assert "revealRun" in tpl
    # Both counts of what stayed shut have to reach the page: the ones the generator knew
    # it could not reach, and the ones the walk itself gave up on.
    assert "deepSkipped" in tpl and "dv-deepmore" in tpl
    assert "reportMissed(" in tpl and "dv-missed" in tpl


def test_both_of_swagger_uis_schema_renderers_are_walked():
    """Same Swagger UI, two completely different schema trees: a 3.1 spec gets <article>s
    (json-schema-2020-12), a 3.0 spec gets <span class="model"> boxes. Walking only one
    leaves every 3.0 spec silently unexpanded, which reads as "there is nothing deeper" —
    the exact belief this feature exists to correct."""
    tpl = ovd.TEMPLATE
    assert "SCHEMA_2020" in tpl and "SCHEMA_LEGACY" in tpl
    # 3.1: articles, the properties keyword, the accordion's own collapsed state.
    assert "json-schema-2020-12-property > article" in tpl
    assert "json-schema-2020-12-accordion__icon--collapsed" in tpl
    # 3.0: model boxes, the property table, aria-expanded on the control.
    assert "button.model-box-control" in tpl
    assert "tr.property-row" in tpl and "aria-expanded" in tpl
    # The root picks the strategy; neither may be hard-wired into the walk itself.
    assert "for (const kit of [SCHEMA_2020, SCHEMA_LEGACY])" in tpl
    assert "kit.child(cur, step)" in tpl


def test_the_leaf_mark_reuses_the_differs_own_severity_scale():
    """l1/l2/l3 are the note list's classes. A fourth colour scale for the same fact is
    how a page ends up with five palettes."""
    tpl = ovd.TEMPLATE
    assert "'dv-hit', 'l' + c.level" in tpl
    assert "dv-fieldmark l' + c.level" in tpl
    for level, var in ((1, "--dv-added"), (2, "--dv-modified"), (3, "--dv-breaking")):
        assert f".dv-fieldmark.l{level} {{" in tpl
        assert f".dv-hit.l{level} {{" in tpl
        assert f"var({var})" in tpl
    # The 3.0 leaf is a table row: the article rules cannot reach it, and a row that
    # highlights nothing looks exactly like a walk that failed.
    assert "tr.dv-hit.l1 > td" in tpl
    assert "tr.property-row.dv-hit > td:first-child" in tpl


# ── the copy in the public repo is the same file ─────────────────────────────────
def test_the_skill_copy_and_the_public_repo_copy_have_not_drifted():
    """`openapi-visual-diff.py` lives twice: here, and as its own public repo. A fix in
    one and not the other is a trap for whoever reads the other one."""
    sibling = HERE.parents[3] / "openapi-visual-diff" / "openapi-visual-diff.py"
    if not sibling.is_file():
        print(f"skip — no sibling checkout at {sibling}")
        return
    mine = (HERE / "openapi-visual-diff.py").read_text(encoding="utf-8")
    assert sibling.read_text(encoding="utf-8") == mine, (
        f"{sibling} has drifted from the skill's copy — sync them before shipping")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
