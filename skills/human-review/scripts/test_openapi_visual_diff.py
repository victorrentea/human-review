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
    # Each level names its colour once, as --dv-lvl; the leaf, its chip and the road
    # all paint in that, so the three cannot disagree about what "l2" looks like.
    for level, var in ((1, "--dv-added"), (2, "--dv-modified"), (3, "--dv-breaking")):
        assert f".l{level} {{ --dv-lvl: var({var}); }}" in tpl
        assert f".swagger-ui .dv-hit.l{level}" in tpl
    assert "background: var(--dv-lvl); color: var(--dv-bg);" in tpl   # the chip
    assert ".dv-roads path { fill: none; stroke: var(--dv-lvl);" in tpl
    assert "road.g.setAttribute('class', 'l' + road.level)" in tpl
    # The 3.0 leaf is a table row: the article rules cannot reach it, and a row that
    # highlights nothing looks exactly like a walk that failed.
    assert "tr.dv-hit > td {" in tpl
    assert "tr.property-row.dv-hit > td:first-child" in tpl


# ── markers belong to the tree, not to the checkbox ──────────────────────────────
def test_markers_are_drawn_on_whatever_is_open_not_only_by_the_checkbox():
    """With "expand impacted" unticked, an operation opened by hand showed OwnerPageDto's
    new fields with no ADDED chip at all -- read as "nothing changed here". The checkbox
    may only decide what is opened FOR the reader; every render of the tree must mark
    whatever happens to be on screen, opening nothing."""
    tpl = ovd.TEMPLATE
    decorate = tpl[tpl.index("function decorate()"):tpl.index("let collapsedOnce")]
    assert "markVisible();" in decorate, "the render hook no longer marks what is open"
    mark = tpl[tpl.index("function markVisible()"):tpl.index("// ---- ghosts:")]
    # Every open operation, regardless of the checkbox...
    assert ".opblock.is-open" in mark and "dv-expand" not in mark
    # ...through the walker that never clicks anything.
    assert "walkVisible(op, c.target, c.target.steps)" in mark
    assert "markLeaf(" in mark and "markPath(" in mark
    walk = tpl[tpl.index("function walkVisible("):tpl.index("function markVisible()")]
    assert ".click()" not in walk and "openNode" not in walk, "a passive walk opened a node"
    assert "kit.collapsed(cur)" in walk, "a closed node must end the walk, not be opened"


# ── what is no longer there ──────────────────────────────────────────────────────
def test_a_removed_property_comes_back_as_a_ghost_where_it_used_to_be():
    """`gone` was VisitDto's second field; the page draws only the new spec, so without a
    ghost the reader sees VisitDto with nothing missing."""
    old = spec(True)
    old["components"]["schemas"]["VisitDto"]["properties"] = {
        "id": {"type": "integer"},
        "gone": {"type": "string", "description": "bye"},
        "description": {"type": "string"},
    }
    removal = [{
        "id": "response-property-removed",
        "text": "removed the optional property `items/pets/items/visits/items/gone` from "
                "the response with the `200` status",
        "level": 3, "operation": "GET", "path": "/api/owners", "section": "paths",
    }]
    _, entries, _, _ = ovd.build_model(old, spec(True), removal)
    owners = entries["GET /api/owners"]
    props = [g for g in owners["ghosts"] if g["kind"] == "prop"]
    assert len(props) == 1, owners["ghosts"]
    g = props[0]
    assert g["name"] == "gone" and g["after"] == "id"      # back between id and description
    assert g["in"] == "response" and g["status"] == "200"
    # Steps lead to the PARENT in the new tree -- the walker's own vocabulary.
    assert g["steps"] == [{"kind": "items"}, {"kind": "prop", "name": "pets"},
                          {"kind": "items"}, {"kind": "prop", "name": "visits"},
                          {"kind": "items"}]
    assert g["old"]["label"] == "string" and g["old"]["desc"] == "bye"
    # The oasdiff line is tied to its ghost, so it is not also reported as unreachable.
    assert owners["changes"][0]["ghost"] == owners["ghosts"].index(g)
    assert owners["deepSkipped"] == 0


def test_a_changed_type_keeps_its_old_shape_as_a_collapsed_ghost():
    """petclinic's GET /api/owners: `array<OwnerDto>` became `OwnerPageDto`. The new
    object is drawn by Swagger UI; the old array has to come back next to it, openable
    into its old fields -- and nothing below is diffed against the unrelated new shape."""
    new = spec(True)
    new["components"]["schemas"]["OwnerPageDto"] = {"type": "object", "properties": {
        "content": {"type": "array", "items": {"$ref": "#/components/schemas/OwnerDto"}},
        "totalPages": {"type": "integer"}}}
    new["paths"]["/api/owners"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"] = {"$ref": "#/components/schemas/OwnerPageDto"}
    _, entries, _, _ = ovd.build_model(spec(True), new, [])
    ghosts = entries["GET /api/owners"]["ghosts"]
    assert [g["kind"] for g in ghosts] == ["type"], ghosts
    g = ghosts[0]
    assert g["steps"] == [] and g["status"] == "200"        # right beside the root
    assert g["old"]["label"] == "array<OwnerDto>"
    # Expandable straight into OwnerDto's fields, and on down into the pets.
    names = [p["name"] for p in g["old"]["props"]]
    assert names == ["id", "pets"]
    assert g["old"]["props"][1]["label"] == "array<PetDto>"
    assert {p["name"] for p in g["old"]["props"][1]["props"]} == {"name", "visits"}


def test_removed_parameters_and_responses_are_ghosted_in_order():
    old = spec(True)
    get = old["paths"]["/api/owners"]["get"]
    get["parameters"] = [{"in": "query", "name": "lastName", "schema": {"type": "string"}},
                         {"in": "query", "name": "legacy", "schema": {"type": "integer"}}]
    get["responses"][404] = {"description": "Not Found"}     # YAML-style int key
    new = spec(True)
    new["paths"]["/api/owners"]["get"]["parameters"] = [
        {"in": "query", "name": "lastName", "schema": {"type": "string"}}]
    _, entries, _, _ = ovd.build_model(old, new, [])
    ghosts = {g["kind"]: g for g in entries["GET /api/owners"]["ghosts"]}
    assert ghosts["param"]["name"] == "legacy"
    assert ghosts["param"]["after"] == ["lastName", "query"]
    assert ghosts["param"]["label"] == "integer"
    assert ghosts["response"]["status"] == "404" and ghosts["response"]["after"] == "200"


def test_an_unchanged_spec_has_no_ghosts_and_a_removed_operation_none_inside():
    _, entries, _, _ = ovd.build_model(spec(True), spec(True), [])
    assert all(not e["ghosts"] for e in entries.values())
    gone = spec(True)
    del gone["paths"]["/api/visits"]
    _, entries, _, _ = ovd.build_model(spec(True), gone, [])
    assert entries["POST /api/visits"]["state"] == "removed"
    assert entries["POST /api/visits"]["ghosts"] == []


def test_the_page_draws_ghosts_as_deleted_in_the_theme_red():
    tpl = ovd.TEMPLATE
    assert "placeGhost(op, g, i)" in tpl
    for kind in ("prop", "type", "param", "response", "media", "body"):
        assert f"g.kind === '{kind}'" in tpl, f"ghost kind {kind} is never drawn"
    assert "'<span class=\"dv-ghost-mark\">deleted</span>'" in tpl
    assert ("background: var(--dv-breaking); color: var(--dv-bg);" in
            tpl[tpl.index(".dv-ghost-mark {"):])
    assert "text-decoration: line-through" in tpl[tpl.index(".dv-ghost-name {"):]
    # Swagger UI's `summary { display: list-item }` must not beat the head's flex row.
    assert ".swagger-ui .dv-ghost-head {" in tpl or ".swagger-ui .dv-ghost-head," in tpl
    # Ghosts are never mistaken for live nodes by either walker.
    assert "json-schema-2020-12-property" not in tpl[tpl.index("function ghostTree("):
                                                   tpl.index("function placeAfter(")]
    # The reveal opens the way to them as well.
    assert "openTo(op, g, g.steps, run, g.kind === 'prop')" in tpl


# ── the copy in the public repo is the same file ─────────────────────────────────
def test_an_added_operation_counts_once_toward_expand_n_impacted():
    """An added operation lists no line (its only oasdiff entry is that it exists), and
    the verdict band counts it as one change: the toggle has to as well."""
    old = {"paths": {}}
    new = {"paths": {"/api/vets": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    raw = [{"id": "endpoint-added", "operation": "GET", "path": "/api/vets", "level": 1,
            "text": "endpoint added", "section": "paths"}]
    _, entries, global_changes, _ = ovd.build_model(old, new, raw)
    assert entries["GET /api/vets"]["changes"] == []
    assert ovd.change_total(entries, global_changes) == 1


def test_the_expand_toggle_counts_changes_by_that_name():
    """Victor at Devoxx, 7 Oct 2026: "it should say expand 25 changes". The live page was
    patched by hand; the producer has to print it, or the next rebuild says "impacted"."""
    old = {"paths": {}}
    new = {"paths": {"/api/vets": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    raw = [{"id": "endpoint-added", "operation": "GET", "path": "/api/vets", "level": 1,
            "text": "endpoint added", "section": "paths"}]
    model, entries, global_changes, tags = ovd.build_model(old, new, raw)
    page = ovd.render(model, entries, global_changes, tags, "main", "branch")
    assert 'id="dv-expand"> expand 1 change</label>' in page
    assert "expand 1 impacted" not in page


def test_a_node_that_will_not_open_gets_its_tree_redrawn_once():
    """test-pr, 7 Oct 2026 rebuild: booted on Schema, an array response's `Items` ignored
    its toggle and 9 of 25 changes read "could not be opened". Redrawing the tree by a tab
    switch (Example Value, then Schema) opens it; the walk does that once, then gives up."""
    t = ovd.TEMPLATE
    assert "async function redrawSchema(host)" in t
    assert "if (!await openNode(kit, cur)) return again();" in t
    assert "!redrawn && run === revealRun && await redrawSchema(host)" in t


def test_a_quiet_controller_and_a_status_caret_look_as_they_did_live():
    """What Victor saw and approved on 7 Oct: a collapsed controller is a grey name with a
    grey rim and no band; the response-status caret is the text's colour."""
    t = ovd.TEMPLATE
    quiet = t.split(".opblock-tag-section.dv-quiet > h3.opblock-tag {", 1)[1].split("}", 1)[0]
    assert "background: transparent !important;" in quiet
    assert "border-left-color: currentColor !important;" in quiet
    caret = t.split("td.response-col_status::before {", 1)[1].split("}", 1)[0]
    assert "color: inherit;" in caret


def test_a_breaking_badge_keeps_its_change_count():
    """Run 6: the breaking endpoint's badge said only BREAKING, where a modified one says
    "3 CHANGES" — the count was gone exactly where it matters most."""
    assert "`${nb} breaking · ${many}`" in ovd.TEMPLATE
    assert "info.state === 'modified' && n ? many" in ovd.TEMPLATE


def test_expand_impacted_opens_a_folded_controller_and_folds_back_only_what_it_opened():
    """5 Oct 2026: with the controller folded, ticking the toggle opened nothing — a folded
    tag renders no operations for the loop to find. Touched tags open first, off the tag
    map, and unticking folds back the ones the toggle itself opened."""
    t = ovd.TEMPLATE
    assert "DATA.tags[tag] !== 'touched'" in t and "openedTags.add(tag)" in t
    assert "openedTags.clear()" in t


def test_the_diff_scrolls_with_its_page_and_its_roads_do_not_ratchet():
    """No height posted to a host, no stick offset taken from one: embedded or standalone,
    the window scrolls and the toolbar pins itself by plain sticky. The roads layer is
    measured with itself taken out, inside the scrollbar — sized to the old `scrollWidth`,
    it outgrew the page by the scrollbar's 15px and drew a horizontal scrollbar."""
    t = ovd.TEMPLATE
    assert "postMessage" not in t and "'dv-height'" not in t and "'dv-stick'" not in t
    assert "setAttribute('width', APP.clientWidth)" in t
    assert "scrollWidth)" not in t[t.index("function drawRoads()"):]
    assert "position: sticky; top: var(--dv-sticky-top, 0px);" in t


def test_the_script_runs_against_its_own_root_so_it_can_live_in_a_shadow_root():
    """5 Oct 2026: the review embeds this page in a shadow root rather than an iframe. The
    script finds its root off its own tag (`data-dv-host`) and never queries the document,
    Swagger UI is handed its node rather than a selector the document would resolve, and
    the whole script is one function scope so nothing leaks into the host page's globals."""
    t = ovd.TEMPLATE
    js = t[t.index("const DATA = __PAYLOAD__;"):t.index("</script>\n</body>")]
    assert "document.currentScript.dataset.dvHost" in t
    assert "const root = HOST ? HOST.shadowRoot : document;" in t
    for call in ("document.querySelector", "document.getElementById", "document.body",
                 "location.hash", "window."):
        assert call not in js, f"{call} reaches past the root"
    assert "domNode: root.getElementById('swagger-ui')" in t and "dom_id:" not in js
    assert "<script>\n// One function scope for all of it" in t
    assert t.rstrip().endswith("})();\n</script>\n</body>\n</html>")
    # The stylesheet answers to a shadow host as well as to a document.
    assert ":root, :host {" in t and ':host([data-theme="dark"])' in t
    assert "body.dv-hide-untouched" not in t and ".dv-hide-untouched .opblock" in t


def test_an_operation_reads_as_a_diff_not_as_a_console():
    """Victor, 7 Oct 2026: no spec title or description, a band per controller, bodies
    open on Schema, no "Controls Accept header.", and responses fold by status -- 2xx
    open, the rest folded unless this diff touched them."""
    t = ovd.TEMPLATE
    assert "defaultModelRendering: 'model'" in t, "bodies must open on Schema"
    assert ".swagger-ui .info .title { font-size: 0 !important;" in t
    assert ".swagger-ui .info__description" in t
    assert ".response-control-media-type__accept-message { display: none !important; }" in t
    assert "border-left: 4px solid var(--dv-attr) !important;" in t, "controller band"
    # the fold: wired into decorate, 2xx open by default, a touched status opens too,
    # and the reveal walk unfolds a line it has to walk into
    assert "foldResponses();" in t and "function foldResponses()" in t
    assert "/^2/.test(tr.dataset.code) || responseChanged(" in t
    assert "t.in === 'response' && String(t.status) === code" in t
    assert "if (host.matches('tr.response.dv-folded')) setFolded(host, false);" in t
    assert "tr.response.dv-folded td.response-col_description > :not(" in t


def test_the_changes_badge_is_the_tab_of_a_box_around_the_change_lines():
    """Victor, 8 Oct 2026: the change list was mysterious once the operation opened. The
    "N CHANGES" badge is now the folder tab of a tinted box around the lines, joined by
    concave arcs; the blue rule under an open header is gone; status lines are tinted
    (2xx green, 4xx/5xx red) with one font size; an empty "Default value :" is hidden."""
    t = ovd.TEMPLATE
    assert "function dressNote(op)" in t and "dressNote(op);" in t
    assert ".dv-rail-fil" in t and ".dv-note > .dv-rail" in t and ".dv-rail > .dv-badge" in t
    assert ".swagger-ui .opblock.is-open .opblock-summary { border-bottom: 0 !important; }" in t
    assert 'tr.response[data-code^="2"]' in t and 'tr.response:is([data-code^="4"], [data-code^="5"])' in t
    assert "function hideEmptyDefaults()" in t and ".parameter__default.dv-empty" in t


def test_a_bundle_that_never_arrives_is_retried_then_listed_plainly():
    """Victor, 9 Oct 2026: the API tab showed its bar and chips and then nothing -- the
    Swagger UI bundle from cdnjs had not run, and the page said nothing about it. A missing
    bundle is fetched once more from jsdelivr (its stylesheet too, when that never loaded),
    and if that fails the changed endpoints are listed from DATA with the reason on top."""
    t = ovd.TEMPLATE
    boot = t[t.index("function boot() {"):]
    assert "if (typeof SwaggerUIBundle !== 'function') { retryBundle(); return; }" in boot
    assert "cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.1/swagger-ui-bundle.js" in t
    assert "cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.1/swagger-ui.css" in t
    assert "s.onerror = () => { clearTimeout(timer); plainList(); };" in t
    assert "setTimeout(plainList, 15000)" in t
    assert "function plainList()" in t and "Swagger UI could not be loaded" in t
    # one retry, never a loop: the second miss goes straight to the plain list
    assert "if (retried) { plainList(); return; }" in t


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
