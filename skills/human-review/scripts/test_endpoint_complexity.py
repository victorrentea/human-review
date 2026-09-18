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
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("endpoint_complexity", HERE / "endpoint-complexity.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)

_dspec = importlib.util.spec_from_file_location(
    "endpoint_complexity_delta", HERE / "endpoint-complexity-delta.py")
delta = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(delta)


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


# --------------------------------------------------------------------------- #
# the fragment: a path that does not fit, and verbs that have to be readable
# --------------------------------------------------------------------------- #

LONGEST = "POST /api/owners/{ownerId}/pets/{petId}/visits"


def _row(**over):
    r = {"path": LONGEST, "method": "POST", "now": 12, "was": 8, "delta": 4,
         "handler": "VisitRestController.addVisit", "entry": "", "kind": "http"}
    r.update(over)
    return r


def test_a_path_too_wide_for_its_column_ends_in_an_ellipsis_and_keeps_its_full_text():
    """It was chopped mid-token — `…/pets/{petId}/vis` — with nothing saying it had been.

    `text-overflow` applies to a block container and <code> is inline, so the rule was
    there and doing nothing while the cell's own `overflow:hidden` did the cutting. The
    full route is on the cell's hover either way: this is the one column on the tab wide
    enough to reach its edge, and the row that reaches it is the endpoint the branch is
    about."""
    css = delta.CSS
    rule = re.search(r"^\.cx-path \{([^}]*)\}", css, re.S | re.M)[1]
    assert "display:block" in rule.replace(" ", ""), \
        "text-overflow does nothing on an inline <code>"
    assert "text-overflow:ellipsis" in rule.replace(" ", "")
    cell = delta._path_cell(_row())
    assert LONGEST in cell, "the label itself"
    tip = re.search(r'data-tip="([^"]*)"', cell)[1]
    assert tip.split("&#10;")[0].replace("&#x27;", "'") .startswith("POST /api/owners"), tip
    assert "{ownerId}" in tip.replace("&#123;", "{"), "the cut half comes back on the hover"
    # A row whose entry point this checkout cannot place still gets the hover: the path is
    # no shorter for being unlinkable.
    plain = delta._path_cell(_row(entry="nothing::at::all"))
    assert 'class="cx-cell"' in plain and "data-tip=" in plain


def test_every_verb_chip_reads_against_the_dark_card():
    """43 of them on this tab, at 2.9–3.3:1: the light palette was never re-themed, so
    `GET` green and `POST` blue came out as grey smudges on `--card`. Every hue is checked,
    in both themes, against the surface the chip actually sits on — `.cx-list` paints
    `var(--card)`, so that is the background, not the page's."""
    def lum(h):
        v = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        v = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in v]
        return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]

    def ratio(a, b):
        la, lb = sorted((lum(a), lum(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)

    page = (HERE / "hrbuild" / "assets" / "page.css").read_text(encoding="utf-8")
    light_page, dark_page = page.split("@media (prefers-color-scheme: dark)", 1)
    light_css, dark_css = delta.CSS.split("@media (prefers-color-scheme: dark)", 1)
    verbs = re.compile(r"\.cx-([a-z]+)\s*\{\s*color:(#[0-9a-fA-F]{6})")
    for css, page_block, theme in ((light_css, light_page, "light"),
                                   (dark_css, dark_page, "dark")):
        card = re.search(r"--card:(#[0-9a-fA-F]{6})", page_block)[1]
        found = dict(verbs.findall(css))
        assert found, f"no verb colours declared for {theme} mode"
        for verb, hexval in found.items():
            assert ratio(hexval, card) >= 4.5, \
                f".cx-{verb} is {ratio(hexval, card):.2f}:1 on {card} in {theme} mode"
    # Every verb the light palette paints is re-painted in dark; a hue left behind is
    # exactly how this happened the first time.
    assert set(dict(verbs.findall(light_css))) - {"any"} == set(dict(verbs.findall(dark_css)))


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
