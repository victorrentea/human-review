#!/usr/bin/env python3
"""The source-only complexity extractor, pinned.

Two things here are quoted to a reviewer as measurements and are wrong in silence when
they break: the score (a number beside an endpoint) and the set of entry points (a row
that is simply absent). The second is the one that already happened — a parameter list
with an annotation in it (`listOwners(@RequestParam(…) String lastName)`) carries parens,
and the first version of the declaration pattern stopped at the first `(`, so five
handlers and every MCP tool with described parameters were missing from a tab that looked
complete.

Run it directly (`python3 test_endpoint_complexity.py`) or under pytest.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("endpoint_complexity", HERE / "endpoint-complexity.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)


def cc(body: str) -> int:
    return ec.cognitive(ec.strip("{" + body + "}"))


def test_straight_line_code_is_free():
    assert cc("int a = 1; a++; log.info(\"done\"); return a;") == 0


def test_nesting_is_what_costs():
    # Two sibling ifs are two increments; one inside the other is three, which is the
    # whole point of the metric over McCabe's count of branches.
    assert cc("if (a) { x(); } if (b) { y(); }") == 2
    assert cc("if (a) { if (b) { y(); } }") == 3
    assert cc("for (Pet p : pets) { if (p.sick()) { while (x) { y(); } } }") == 1 + 2 + 3


def test_else_costs_one_flat_and_else_if_is_not_two():
    assert cc("if (a) { x(); } else { y(); }") == 2
    assert cc("if (a) { x(); } else if (b) { y(); } else { z(); }") == 3


def test_a_braceless_branch_does_not_nest_what_follows():
    assert cc("if (a) return 1; if (b) { c(); }") == 2


def test_boolean_runs_cost_per_alternation():
    assert cc("if (a && b && c) { x(); }") == 2          # the if, then one run of &&
    assert cc("if ((a && b) || c) { x(); }") == 3        # two runs
    assert cc("boolean ok = a && b;") == 1


def test_a_ternary_costs_and_a_wildcard_does_not():
    assert cc("int n = a ? 1 : 2;") == 1
    assert cc("List<?> all = repo.findAll();") == 0


def test_do_while_is_one_construct():
    assert cc("do { x(); } while (a);") == 1


def test_catch_costs_and_try_does_not():
    assert cc("try { x(); } catch (Exception e) { log(e); }") == 1


# ── entry points ───────────────────────────────────────────────────────────────────────

CONTROLLER = """
package app.rest;

@RestController
@RequestMapping("/api/owners")
public class OwnerRestController {
    private final OwnerRepository repository;

    @GetMapping(produces = "application/json")
    public List<OwnerDto> listOwners(@RequestParam(name = "lastName", defaultValue = "") String lastName) {
        return repository.search(lastName);
    }

    @GetMapping("{ownerId}")
    public OwnerDto getOwner(@PathVariable int ownerId) {
        return repository.byId(ownerId);
    }

    @PostMapping
    public void addOwner(@RequestBody OwnerDto dto) {
        repository.save(dto);
    }
}
"""

REPOSITORY = """
package app.repo;

public class OwnerRepository {
    public List<OwnerDto> search(String lastName) {
        if (lastName.isEmpty()) {
            return all();
        }
        for (Owner o : all()) {
            if (o.matches(lastName)) { hit(o); }
        }
        return List.of();
    }
    public OwnerDto byId(int id) { return null; }
    public void save(OwnerDto dto) { }
    public List<OwnerDto> all() { return null; }
    public void hit(Owner o) { }
}
"""

MCP = """
package app.mcp;

public class PetClinicMcp {
    @McpTool(name = "create_visit", description = "Book a visit")
    public String createVisit(
            @McpToolParam(description = "Pet ID (must belong to the owner)", required = true) int petId,
            @McpToolParam(description = "Visit date") LocalDate date) {
        if (petId < 0) { throw new IllegalArgumentException("no"); }
        return "ok";
    }
}
"""


def entries():
    """The extracted entry points, keyed the way the tab identifies a row: verb and path,
    because `GET /api/owners` and `POST /api/owners` are two rows."""
    return {f'{e["httpMethod"]} {e["path"]}': e for e in ec.extract(
        {"a/src/main/java/app/rest/OwnerRestController.java": CONTROLLER,
         "a/src/main/java/app/repo/OwnerRepository.java": REPOSITORY,
         "a/src/main/java/app/mcp/PetClinicMcp.java": MCP})}


def test_the_class_prefix_is_the_path_of_a_mapping_that_carries_none():
    found = entries()
    # `@GetMapping(produces = …)` names no path of its own: the row is the class's path.
    assert set(found) >= {"GET /api/owners", "POST /api/owners",
                          "GET /api/owners/{ownerId}", "MCP create_visit"}
    assert found["GET /api/owners"]["handler"] == "OwnerRestController.listOwners(String)"


def test_an_annotated_parameter_list_does_not_hide_a_handler():
    found = entries()
    assert found["MCP create_visit"]["kind"] == "mcp"
    assert found["MCP create_visit"]["handler"] == "PetClinicMcp.createVisit(int, LocalDate)"


def test_the_flow_is_summed_over_distinct_methods_once():
    found = entries()
    # search() is 1 (the guard) + 1 (the loop) + 2 (the if inside it) = 4, and it calls
    # all() twice — the second call is free, because the method is already in the flow.
    assert found["GET /api/owners"]["flowCc"] == 4
    assert [m["method"] for m in found["GET /api/owners"]["flow"]] == [
        "app.rest.OwnerRestController#listOwners",
        "app.repo.OwnerRepository#search",
        "app.repo.OwnerRepository#all",
        "app.repo.OwnerRepository#hit",
    ]


def test_a_handler_that_reaches_nothing_expensive_scores_zero():
    assert entries()["GET /api/owners/{ownerId}"]["flowCc"] == 0


def test_a_call_this_file_cannot_place_is_dropped_rather_than_guessed():
    files = {
        "a/src/main/java/app/A.java":
            "package app;\npublic class A {\n  @GetMapping(\"/a\")\n  public void go() { helper.run(); }\n}\n",
        "a/src/main/java/app/B.java":
            "package app;\npublic class B {\n  public void run() { if (x) { y(); } }\n}\n",
        "a/src/main/java/app/C.java":
            "package app;\npublic class C {\n  public void run() { if (x) { y(); } }\n}\n",
    }
    [entry] = ec.extract(files)
    # `helper` has no declared type here and two classes offer `run`: counting both would
    # invent complexity, counting one would pick it by file order.
    assert entry["methods"] == 1 and entry["flowCc"] == 0


def test_recursion_costs_one():
    files = {"a/src/main/java/app/A.java":
             "package app;\npublic class A {\n  @GetMapping(\"/a\")\n"
             "  public void go(int n) { go(n - 1); }\n}\n"}
    assert ec.extract(files)[0]["flowCc"] == 1


def test_comments_and_strings_are_not_read_as_code():
    # A `{` in a string and an `if` in a comment used to open a block and charge for it.
    assert cc('String s = "if (a) { b(); }"; // if (c) { d(); }') == 0


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
