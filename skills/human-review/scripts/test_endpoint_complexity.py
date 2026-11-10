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


PAGED = """
package app.rest;

@RestController
@RequestMapping("/api/owners")
public class OwnerRestController {
    @GetMapping(produces = "application/json")
    public OwnerPageDto listOwners(
            @RequestParam(name = "lastName", defaultValue = "") String lastName,
            @Parameter(description = "Rows per page",
                    schema = @Schema(type = "integer", defaultValue = "10")) @RequestParam(name = "size",
                            required = false) String size,
            @Parameter(schema = @Schema(allowableValues = {"name,asc", "city,desc"},
                    defaultValue = "name,asc")) @RequestParam(name = "sort", required = false) String sort) {
        if (size == null) { return null; }
        return null;
    }
}
"""


def test_an_openapi_parameter_two_levels_deep_with_an_array_does_not_hide_a_handler():
    """`@Parameter(schema = @Schema(allowableValues = {…}))` is two levels of parens with a
    brace inside. The one-level pattern missed it, and the delta drew the paginated
    `GET /api/owners` as `gone` while it still existed."""
    found = {f'{e["httpMethod"]} {e["path"]}': e for e in ec.extract(
        {"a/src/main/java/app/rest/OwnerRestController.java": PAGED})}
    assert found["GET /api/owners"]["handler"] == \
        "OwnerRestController.listOwners(String, String, String)"
    assert found["GET /api/owners"]["flowCc"] == 1


METER = """
package app.chat;

@RestController
public class Assistant {
    private final TokenCostMeter tokenCostMeter;

    @GetMapping("/assistant")
    public String assistant(String message) {
        tokenCostMeter.record(message);
        return message;
    }
}
"""

TOKEN_METER = """
package app.chat;

public class TokenCostMeter {
    private record Leg(String model, long tokens) {
        Leg {
            if (tokens < 0) throw new IllegalArgumentException();
        }
    }

    public void record(String response) {
        if (response == null) { return; }
        recordLeg(priceFor(response));
    }

    private double priceFor(String model) {
        return model.isEmpty() ? 0 : 1;
    }

    private void recordLeg(double price) { }
}
"""


def test_a_method_named_record_is_filed_and_called_and_a_record_type_is_not_a_method():
    """`record` is a contextual keyword. Refused as a name, it dropped `TokenCostMeter.record`
    — and `priceFor` behind it — from `GET /assistant` on petclinic; the `record Leg(…) {`
    declaration must still not be read as a method called `Leg`."""
    found = {f'{e["httpMethod"]} {e["path"]}': e for e in ec.extract(
        {"a/src/main/java/app/chat/Assistant.java": METER,
         "a/src/main/java/app/chat/TokenCostMeter.java": TOKEN_METER})}
    flow = [m["method"] for m in found["GET /assistant"]["flow"]]
    assert flow == ["app.chat.Assistant#assistant", "app.chat.TokenCostMeter#record",
                    "app.chat.TokenCostMeter#priceFor", "app.chat.TokenCostMeter#recordLeg"]
    # record(): the guard is 1; priceFor(): the ternary is 1.
    assert found["GET /assistant"]["flowCc"] == 2
    index = ec.Index({"a/src/main/java/app/chat/TokenCostMeter.java": TOKEN_METER})
    assert "app.chat.TokenCostMeter#Leg" not in index.methods


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


def test_only_a_call_that_can_only_mean_this_method_is_recursion():
    """Eval run 11: `return Integer.parseInt(value);` inside a helper named `parseInt` was
    charged +1 'recursion' — the name matched, the receiver was the JDK."""
    def rec(body: str) -> int:
        files = {"a/src/main/java/app/A.java":
                 "package app;\npublic class A {\n  @GetMapping(\"/a\")\n"
                 f"  public int parseInt(String v) {{ {body} }}\n}}\n"}
        return ec.extract(files)[0]["flowCc"]
    assert rec("return Integer.parseInt(v);") == 0, "a library type's method of the same name"
    assert rec("return super.parseInt(v);") == 0, "the parent's body, not a loop back"
    assert rec("return v.trim().parseInt(v);") == 0, "a call on whatever trim() returns"
    assert rec("return parseInt(v);") == 1
    assert rec("return this.parseInt(v);") == 1
    assert rec("return A.parseInt(v);") == 1, "a static call through the own class"


MCP_SECURITY = """
package app.mcp;
public class McpTools {
    @McpTool(name = "cancel_visit")
    public void cancel() { int id = Integer.parseInt(name()); java.util.List.of(id); UUID.randomUUID(); }
}
"""
PAGE_REQUEST = """
package app.rest;
public class OwnerPageRequest {
    private static int parseInt(String name, String value) { try { return 1; } catch (Exception e) { return 0; } }
    public static UUID randomUUID() { if (x) { y(); } return null; }
}
"""


def test_a_library_type_s_method_never_binds_to_a_project_method_of_the_same_name():
    """Eval run 11: McpSecurity's `Integer.parseInt(…)` resolved by unique simple name to the
    branch's new private `OwnerPageRequest#parseInt`, and four untouched MCP tools grew +2."""
    [entry] = ec.extract({"a/src/main/java/app/mcp/McpTools.java": MCP_SECURITY,
                          "a/src/main/java/app/rest/OwnerPageRequest.java": PAGE_REQUEST})
    assert [m["method"] for m in entry["flow"]] == ["app.mcp.McpTools#cancel"]
    assert entry["flowCc"] == 0


def test_a_capitalised_constant_is_still_typed_and_an_untyped_lowercase_receiver_still_guesses():
    files = {
        "a/src/main/java/app/A.java":
            "package app;\npublic class A {\n  private static final B INSTANCE = new B();\n"
            "  @GetMapping(\"/a\")\n  public void go() { INSTANCE.run(); helper.walk(); }\n}\n",
        "a/src/main/java/app/B.java":
            "package app;\npublic class B {\n  public void run() { if (x) { y(); } }\n"
            "  public void walk() { if (x) { y(); } }\n}\n",
    }
    [entry] = ec.extract(files)
    assert [m["method"] for m in entry["flow"]] == ["app.A#go", "app.B#run", "app.B#walk"]


STREAMED = """
package app.rest;

public class OwnerRestController {
    private final OwnerMapper ownerMapper;

    @GetMapping("/api/owners")
    public List<OwnerDto> listOwners(List<Owner> owners) {
        List<Integer> ids = owners.stream().map(Owner::getId).toList();
        owners.forEach(o -> audit(o));
        return owners.stream().map(ownerMapper::toOwnerDto).map(this::trim).map(Box::new).toList();
    }
    private OwnerDto trim(OwnerDto d) { if (d == null) { return null; } return d; }
    private void audit(Owner o) { if (o.isNew()) { log(); } }
}
"""
STREAMED_MAPPER = """
package app.rest;

public class OwnerMapper {
    public OwnerDto toOwnerDto(Owner o) { for (Pet p : o.pets()) { if (p.sick()) { flag(p); } } return null; }
}
"""
STREAMED_BOX = """
package app.rest;

public class Box {
    public Box(OwnerDto d) { if (d == null) { throw new IllegalStateException(); } }
}
"""
STREAMED_OWNER = """
package app.rest;

public class Owner {
    public Integer getId() { return id == null ? 0 : id; }
}
"""


def test_a_method_reference_is_a_call_and_so_is_a_call_inside_a_lambda():
    """`.map(ownerMapper::toOwnerDto)` runs the mapper exactly as a dotted call would. The
    call pattern only read `name(`, so eval run 10 lost the whole mapper chain behind
    `GET /api/owners` and drew a flow that grew by four as one that shrank by five."""
    [entry] = ec.extract({"a/src/main/java/app/rest/OwnerRestController.java": STREAMED,
                          "a/src/main/java/app/rest/OwnerMapper.java": STREAMED_MAPPER,
                          "a/src/main/java/app/rest/Box.java": STREAMED_BOX,
                          "a/src/main/java/app/rest/Owner.java": STREAMED_OWNER})
    reached = {m["method"] for m in entry["flow"]}
    assert "app.rest.OwnerMapper#toOwnerDto" in reached, "field::method, typed like field.method("
    assert "app.rest.Owner#getId" in reached, "Type::method"
    assert "app.rest.OwnerRestController#trim" in reached, "this::method"
    assert "app.rest.Box#Box" in reached, "Type::new is the declared constructor"
    assert "app.rest.OwnerRestController#audit" in reached, "a call inside a lambda's body"
    # mapper: the for 1 + the if inside it 2; trim, Box, getId's ternary and audit 1 each.
    assert entry["flowCc"] == 3 + 1 + 1 + 1 + 1


def test_this_dot_call_resolves_to_the_own_class_even_when_another_declares_the_name():
    files = {
        "a/src/main/java/app/A.java":
            "package app;\npublic class A {\n  @GetMapping(\"/a\")\n"
            "  public void go() { this.run(); }\n  void run() { if (x) { y(); } }\n}\n",
        "a/src/main/java/app/B.java":
            "package app;\npublic class B {\n  public void run() { if (x) { y(); } }\n}\n",
    }
    [entry] = ec.extract(files)
    assert [m["method"] for m in entry["flow"]] == ["app.A#go", "app.A#run"]


def test_comments_and_strings_are_not_read_as_code():
    # A `{` in a string and an `if` in a comment used to open a block and charge for it.
    assert cc('String s = "if (a) { b(); }"; // if (c) { d(); }') == 0


# ── the breakdown behind a number ──────────────────────────────────────────────────────

def test_the_increments_add_up_to_the_number_on_the_row():
    """The fold under a row is the row's own arithmetic, so it has to be the same sum.

    A breakdown that comes to 3 under a bar labelled 4 is worse than no breakdown: the
    reviewer now distrusts both. `cognitive` is defined as the sum of `increments`, and
    `flowCc` as the sum over the flow — this pins the whole chain end to end, on the same
    fixture the rest of the file measures."""
    for label, e in entries().items():
        counted = sum(h["inc"] for f in e["flow"] for h in f["hits"])
        assert counted == e["flowCc"], f"{label}: {counted} lines vs flowCc {e['flowCc']}"
        for f in e["flow"]:
            assert sum(h["inc"] for h in f["hits"]) == f["cognitive"], f["method"]
    assert entries()["GET /api/owners"]["flowCc"] == 4


def test_every_increment_names_the_line_it_was_read_off():
    flow = {f["method"]: f for f in entries()["GET /api/owners"]["flow"]}
    hits = flow["app.repo.OwnerRepository#search"]["hits"]
    assert [(h["why"], h["inc"]) for h in hits] == [("if", 1), ("for", 1), ("if", 2)]
    for h in hits:
        assert h["file"] == "a/src/main/java/app/repo/OwnerRepository.java"
        # The line is the line in the file, and the text is what is on it: the fold is
        # evidence, and evidence that points a reader at the wrong line is a bug they
        # cannot see. The nested `if` costs 2 — 1 for itself, 1 for the loop around it.
        assert REPOSITORY.splitlines()[h["line"] - 1].strip() == h["code"]
    assert hits[2]["code"].startswith("if (o.matches(")


def test_recursion_is_charged_on_the_line_that_calls_back():
    files = {"a/src/main/java/app/A.java":
             "package app;\npublic class A {\n  @GetMapping(\"/a\")\n"
             "  public void go(int n) { go(n - 1); }\n}\n"}
    [hit] = ec.extract(files)[0]["flow"][0]["hits"]
    assert (hit["why"], hit["inc"], hit["line"]) == ("recursion", 1, 4)


def test_a_bar_folds_open_onto_its_own_lines():
    """`+1` beside the real source line, each one a link into the editor — and the bar
    is the handle, so it wears a hand rather than the `?` `tip.js` gives a plain mark."""
    row = _row(why=[{"method": "app.repo.OwnerRepository#search",
                     "display": "OwnerRepository.search(String)", "cognitive": 2,
                     "hits": [{"file": "a/src/main/java/app/repo/OwnerRepository.java",
                               "line": 6, "code": "if (lastName.isEmpty()) {",
                               "inc": 1, "why": "if", "new": False},
                              {"file": "a/src/main/java/app/repo/OwnerRepository.java",
                               "line": 10, "code": "for (Owner o : all()) {",
                               "inc": 1, "why": "for", "new": True}]}])
    out = delta.render_row(row, 12)
    assert out.startswith("<details"), "closed by default, and a <details> without JS"
    assert "<summary class=\"cx-head\">" in out
    assert '<span class="cx-why-inc">+1</span>' in out and "[+1]" not in out and "OwnerRepository.search(String)" in out
    href = re.search(r'href="(vscode://file/[^"]*OwnerRepository[^"]*)"', out)[1]
    assert href.endswith(":6:1") and "/a/src/main/java/" in href, href
    assert href.split("vscode://file/")[1].startswith("/"), "absolute, resolved at build time"
    assert "cx-why-new" in out, "a line the merge-base did not have is marked"
    assert re.search(r"details\.cx-row \.cx-bar[^{]*\{[^}]*cursor:pointer", delta.CSS), \
        "the bar opens the fold, so it must not wear tip.js's question mark"
    # A row with nothing to count still opens — onto the sentence that says why it is 0.
    assert "cx-why-none" in delta.render_row(_row(now=0, was=0, delta=0, why=[]), 12)
    # And a deleted entry point does not pretend to have a breakdown.
    assert delta.render_row(_row(gone=True, why=[]), 12).startswith("<div")


def test_the_fold_needs_nothing_the_fragment_does_not_carry():
    """The page pastes this fragment into a tab whole. A stylesheet or a script it had to
    fetch from `hrbuild/assets` would be a tab that only works inside one builder."""
    page = delta.render([_row(why=[])])
    assert '<script data-hr-swap="keep">' in page and "cx-head" in page
    assert "src=" not in page and "hrbuild" not in page
    for name in ("cx-why", "cx-why-line", "cx-why-inc", "cx-head"):
        assert f".{name}" in delta.CSS, f"{name} is emitted but never styled"


# --------------------------------------------------------------------------- #
# the fragment: a path that does not fit, and verbs that have to be readable
# --------------------------------------------------------------------------- #

LONGEST = "POST /api/owners/{ownerId}/pets/{petId}/visits"


def _row(**over):
    r = {"path": LONGEST, "method": "POST", "now": 12, "was": 8, "delta": 4,
         "handler": "VisitRestController.addVisit", "entry": "", "kind": "http"}
    r.update(over)
    return r


def test_a_simpler_flow_says_so_in_words_and_is_never_drawn_in_alarm_red():
    """Green is what the branch added. A shrink drawn in `#c62828` read to every eval-run-10
    judge as an alarm: what it removed is a neutral slate ghost, and the badge says
    `simpler` beside a real minus sign."""
    row = delta.render_row(_row(now=6, was=11, delta=-5, why=[]), 12)
    assert 'class="cx-row cx-down"' in row
    assert '>−5<small> simpler</small></span>' in row
    for block in re.findall(r"--cx-removed:(#[0-9a-fA-F]{6})", delta.CSS):
        r, g, b = (int(block[i:i + 2], 16) for i in (1, 3, 5))
        assert not (r > g + 60 and r > b + 60), f"{block} is a red; the removed colour is neutral"
    assert "--cx-added:#2e9e5b" in delta.CSS, "an increase stays green, as on the reference"
    # The badge column holds `−12 simpler` without the word wrapping under the number.
    cols = _head_columns()
    assert float(cols[4].rstrip("rem")) >= 4.2


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

    page = (HERE / "hrbuild" / "assets" / "css" / "core.css").read_text(encoding="utf-8")
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


# --------------------------------------------------------------------------- #
# the call graph under a row, and the header that opens it
# --------------------------------------------------------------------------- #

def test_cyclomatic_counts_decision_points_not_nesting():
    def cyc(body):
        return ec.cyclomatic(ec.strip("{" + body + "}"))
    assert cyc("a(); b();") == 1
    # if + for + if + && = 4 decisions; `else` and nesting add paths to read, not to walk.
    assert cyc("if (a) { for (X x : xs) { if (b && c) { d(); } } } else { e(); }") == 5
    assert cyc("switch (k) { case 1: a(); break; case 2: b(); }") == 3
    assert cyc("List<?> l = x ? y : z;") == 2, "a wildcard is not a ternary"
    assert cyc("do { a(); } while (b);") == 2


def test_every_flow_node_carries_its_edges_and_both_scores():
    flow = {f["method"]: f for f in entries()["GET /api/owners"]["flow"]}
    assert flow["app.rest.OwnerRestController#listOwners"]["calls"] == [
        "app.repo.OwnerRepository#search"]
    assert set(flow["app.repo.OwnerRepository#search"]["calls"]) >= {
        "app.repo.OwnerRepository#all", "app.repo.OwnerRepository#hit"}
    for f in flow.values():
        assert f["cyclomatic"] >= 1
        assert set(f["calls"]) <= set(flow), "an edge never leaves the flow"


def _gnode(key, cog=0, calls=(), delta=0, cyc=1):
    return {"method": key, "display": key.split(".")[-1].replace("#", ".") + "()",
            "cognitive": cog, "cyclomatic": cyc, "calls": list(calls), "delta": delta}


def test_the_graph_draws_what_costs_and_leaves_the_getters_out():
    nodes = [_gnode("a.Ctl#go", calls=["a.Map#toDto", "a.Owner#getId", "a.Owner#getName"]),
             _gnode("a.Map#toDto", cog=3, cyc=4, delta=1),
             _gnode("a.Owner#getId"), _gnode("a.Owner#getName")]
    out, drawn = delta._graph(nodes)
    assert out.index("Ctl") < out.index("Map"), "left to right, the handler first"
    assert re.search(r'<span class="cg-c"[^>]*>Map</span>', out)
    assert '<span class="cg-m">toDto()</span>' in out
    assert '<span class="cg-cog">3</span>' in out
    assert "cyclomatic" not in out and "cg-cyc" not in out, "one score, the one the bar sums"
    assert "cg-add" in out and "+1" in out, "the method the branch made heavier is marked"
    assert "getId()</span>" not in out, "a getter is not a node of its own"
    assert drawn == {"a.Ctl#go", "a.Map#toDto"}
    assert delta._graph([]) == ("", set())


def test_a_node_folds_open_onto_its_own_lines():
    """The list under the graph repeated the boxes above it in another order. Each box now
    holds its own lines, opened by a click on the box."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Map#toDto"]), _gnode("a.Map#toDto", cog=1)]
    hit = {"file": "a/src/main/java/a/Map.java", "line": 3, "code": "if (x) {",
           "inc": 1, "why": "if", "new": False}
    row = _row(graph=nodes, why=[{"method": "a.Map#toDto", "display": "Map.toDto()",
                                  "cognitive": 1, "hits": [hit]}])
    out = delta.render_row(row, 12)
    node = re.search(r'<div class="cg-n[^"]*cg-has[^"]*".*?</div></div>', out, re.S)[0]
    assert "if (x) {" in node and 'class="cg-lines"' in node, "the line lives in its box"
    assert 'role="button"' in node and not node.startswith("<a"), "the box is a toggle"
    assert "cx-why-m" not in out, "no second list of the same methods under the graph"
    # The meaning of the score is linked once, from the tab's lede — not under every row.
    assert "Call graph, left to right" not in out and "cg-key" not in out
    page = delta.render([row])
    assert re.search(r'<h2 class="tabtitle cx-title"><a href="[^"]*sonarsource'
                     r'\.com/resources/cognitive-complexity[^"]*"[^>]*>Cognitive Complexity</a>'
                     r' per Entry Point</h2>', page)
    # The subtitle names what actually computes the number, linked, and says it is no parser.
    lede = page[page.index('<p class="tabsub cx-lede">'):]
    lede = lede[:lede.index("</p>")]
    assert lede.startswith('<p class="tabsub cx-lede">Computed by <a href="https://github.com/'
                           'victorrentea/human-review/blob/main/skills/human-review/scripts/'
                           'endpoint-complexity.py" target="_blank" rel="noopener">'
                           'endpoint-complexity.py</a>')
    assert "regular expressions over the Java sources (no parser, no syntax tree)" in lede
    assert lede.endswith("(no parser, no syntax tree).")
    assert "scored as" not in lede
    assert "Click ▸" not in page
    assert re.search(r"\.cg-open > \.cg-lines \{[^}]*display:block", delta.CSS)
    assert ".cg-svg .cg-ah" in delta.CSS and "'cg-ah'" in delta.TOGGLE_JS, "edges end in arrowheads"



def test_every_class_is_the_same_faded_box_and_its_methods_are_plain_cards():
    """A hue per class said nothing the name above the box did not (Victor, 4 Nov 2026: "less
    rainbow, one constant colour"). Every box is one neutral tint; the method blocks inside
    keep a plain frame, so green on a frame still means only "raised"."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Map#all"]), _gnode("a.Map#all", cog=1)]
    out, _ = delta._graph(nodes)
    assert out.count('class="cg-k"') == 2, "two classes, one look"
    assert "cg-hue" not in delta.CSS and not re.search(r"\.cg-h\d", delta.CSS)
    k = re.search(r"\.cg-k \{([^}]*)\}", delta.CSS).group(1)
    assert re.search(r"background:color-mix\(in srgb, var\(--muted\) [0-9]%", k), \
        "the box is a faint tint, under 10%"
    name = re.search(r"\.cg-c \{([^}]*)\}", delta.CSS).group(1)
    assert "color:var(--muted)" in name
    frame = re.search(r"\.cg-n \{([^}]*)\}", delta.CSS).group(1)
    assert "background:var(--card)" in frame, "a method is a card"
    assert re.search(r"\.cg-n\.cg-add \{[^}]*border-color:var\(--cx-added\)", delta.CSS), \
        "a raised method keeps its green frame"


def test_class_and_method_names_are_the_links_and_the_graph_has_no_arrow(tmp_path, monkeypatch):
    """Victor, 4 Nov 2026: the ↗ goes; both names underline under the pointer and open the
    editor, the class at its declaration, the method at its own line."""
    java = tmp_path / "Map.java"
    java.write_text("package a;\n\n// class Map, in a comment\npublic class Map {\n"
                    "  int toDto() { return 1; }\n}\n", encoding="utf-8")
    monkeypatch.setattr(delta, "entry_source", lambda key: (java, 5))
    out, _ = delta._graph([_gnode("a.Map#go", calls=["a.Map#toDto"]),
                           _gnode("a.Map#toDto", cog=1)])
    assert "↗" not in out and "cg-go\"" not in out
    assert re.search(rf'<a class="cg-c" href="vscode://file/{java}:4:1"[^>]*>Map</a>', out)
    assert re.search(rf'<a class="cg-go-m" href="vscode://file/{java}:5:1"[^>]*>toDto\(\)</a>', out)
    assert re.search(r"a\.cg-c:hover, a\.cg-go-m:hover, a\.cx-path:hover \{"
                     r"[^}]*text-decoration:underline", delta.CSS)


def test_a_class_is_drawn_once_however_many_of_its_methods_are_in_the_flow():
    """`PetMapper` was three boxes in a row (Victor, 20 Oct 2026: "why does PetMapper
    appear three times?"). One box per class, named once, its methods inside in
    the order the flow met them; a call that stays in the class is an edge between two
    blocks of one box, a call into another class an edge to the next column."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Map#all"]),
             _gnode("a.Map#all", cog=1, calls=["a.Map#one", "a.Pet#toDto"]),
             _gnode("a.Map#one", cog=2), _gnode("a.Pet#toDto", cog=1, calls=["a.Map#type"]),
             _gnode("a.Map#type", cog=1)]
    out, drawn = delta._graph(nodes)
    assert len(re.findall(r'<span class="cg-c"[^>]*>Map</span>', out)) == 1, "Map, once"
    assert out.count('class="cg-k"') == 3, "three classes, three boxes"
    boxes = out.split('<div class="cg-k"')[1:]
    inside = {re.search(r'data-k="(\w+)"', b)[1]: re.findall(r'class="cg-m">(\w+)\(\)', b)
              for b in boxes}
    assert inside["Map"] == ["all", "one", "type"], inside
    cols = [re.findall(r'data-k="(\w+)"', c) for c in out.split('<div class="cg-col">')[1:]]
    assert cols == [["Ctl"], ["Map"], ["Pet"]], "a column per class, left to right"
    edges = re.search(r'data-edges="([^"]*)"', out)[1].split()
    # Depth first: go, all, one, toDto, type.
    assert {e.rstrip("*") for e in edges} == {"0>1", "1>2", "1>3", "3>4"}, edges
    assert drawn == {"a.Ctl#go", "a.Map#all", "a.Map#one", "a.Pet#toDto", "a.Map#type"}
    assert 'class="cg-svg"' in out and "createElementNS" in delta.TOGGLE_JS, "arrows are drawn"


def test_a_callee_is_right_of_every_class_that_calls_it():
    """The column is the longest run of class boundaries to a class, so a class reached
    both directly and through another sits right of both its callers."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Svc#run", "a.Repo#find"]),
             _gnode("a.Svc#run", cog=1, calls=["a.Repo#save"]),
             _gnode("a.Repo#find", cog=1), _gnode("a.Repo#save", cog=1)]
    out, _ = delta._graph(nodes)
    cols = [re.findall(r'data-k="(\w+)"', c) for c in out.split('<div class="cg-col">')[1:]]
    assert cols == [["Ctl"], ["Svc"], ["Repo"]], cols


def test_opening_a_row_shows_what_the_branch_added_without_another_click():
    """Victor, 20 Oct 2026: "if I open GET /api/owners it must already be expanded to show
    me what is newly added because of this +1". The method the branch raised starts open on
    its lines, and every edge on the way to it is marked; the rest stays shut."""
    def hit(line, new):
        return {"file": "a/src/main/java/a/V.java", "line": line, "code": f"if (x{line}) {{",
                "inc": 1, "why": "if", "new": new}
    nodes = [_gnode("a.Ctl#go", calls=["a.Pet#toDto", "a.Type#name"]),
             _gnode("a.Pet#toDto", cog=1, calls=["a.Vis#toDto"]),
             _gnode("a.Vis#toDto", cog=2, delta=1), _gnode("a.Type#name", cog=1)]
    groups = [{"method": "a.Pet#toDto", "display": "Pet.toDto()", "cognitive": 1,
               "hits": [hit(1, False)]},
              {"method": "a.Vis#toDto", "display": "Vis.toDto()", "cognitive": 2,
               "hits": [hit(2, False), hit(3, True)]},
              {"method": "a.Type#name", "display": "Type.name()", "cognitive": 1,
               "hits": [hit(4, False)]}]
    out, _ = delta._graph(nodes, groups)
    classes = re.findall(r'<div class="(cg-n[^"]*)"', out)
    opened = [c for c in classes if "cg-open" in c]
    assert opened == ["cg-n cg-add cg-has cg-open"], classes
    assert re.search(r'<div class="cg-n cg-add cg-has cg-open"[^>]*aria-expanded="true"', out)
    assert "if (x3) {" in out and "cx-why-new" in out
    edges = re.search(r'data-edges="([^"]*)"', out)[1].split()
    assert sorted(e for e in edges if e.endswith("*")) == ["0>1*", "1>2*"], edges
    assert len(edges) == 3, "and the edge away from the change is drawn, unmarked"
    assert re.search(r"\.cg-n\.cg-add\.cg-open \{", delta.CSS), "an open raised block stays green"
    # A brand-new entry point marks nothing, so nothing opens on its own.
    fresh, _ = delta._graph([dict(n, delta=0) for n in nodes], groups)
    assert "cg-open" not in fresh


def test_a_path_to_the_change_is_never_folded_into_plus_n():
    """`+N` keeps the costliest callees and folds the rest; the one that leads to the
    change is drawn even when it is the cheapest, or the open row would hide its answer."""
    cheap = "a.Z#changed"
    nodes = ([_gnode("a.Ctl#go", calls=[f"a.S{i}#f" for i in range(7)] + [cheap])]
             + [_gnode(f"a.S{i}#f", cog=5) for i in range(7)] + [_gnode(cheap, cog=0, delta=1)])
    out, drawn = delta._graph(nodes)
    assert cheap in drawn and ">+2</span>" in out, "the change drawn, two others folded"


def test_recursion_draws_each_method_once():
    """A method that calls itself, or two that call each other, is still a finite graph:
    each method is drawn once, under whoever reached it first, and a call back into one
    already drawn adds no edge."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Tree#walk"]),
             _gnode("a.Tree#walk", cog=2, calls=["a.Tree#walk", "a.Tree#visit", "a.Ctl#go"]),
             _gnode("a.Tree#visit", cog=1, calls=["a.Tree#walk"])]
    out, drawn = delta._graph(nodes)
    assert drawn == {"a.Ctl#go", "a.Tree#walk", "a.Tree#visit"}
    assert out.count("walk()</span>") == 1 and out.count("visit()</span>") == 1
    assert out.count("go()</span>") == 1


def test_a_graph_needs_edges_and_a_new_entry_point_marks_nothing():
    old_snapshot = {"flow": [{"method": "a.X#go", "cognitive": 1}]}
    assert delta.graph_nodes(old_snapshot, None) == [], "no `calls`: an old snapshot"
    cur = {"flow": [{"method": "a.X#go", "cognitive": 3, "calls": []}]}
    assert delta.graph_nodes(cur, None)[0]["delta"] == 0
    assert delta.graph_nodes(cur, old_snapshot)[0]["delta"] == 2


def test_the_row_offers_a_caret_and_the_group_is_not_titled_http_slash():
    row = delta.render_row(_row(why=[], graph=[]), 12)
    assert '<summary class="cx-head"><span class="cx-caret"' in row
    # The full-size ▼ (U+25BC), as text: the small ▾ stayed a speck at any font size.
    assert re.search(r"details\.cx-row\[open\][^{]*\.cx-caret::before\s*\{\s*content:\"\\25BC\\FE0E\"",
                     delta.CSS)
    size = re.search(r"\.cx-caret \{[^}]*font-size:(\d+)px", delta.CSS)[1]
    assert int(size) >= 13, "the caret is a handle, not punctuation"
    assert "HTTP /" not in delta.render([_row(why=[])])
    cols = _head_columns()
    assert cols[1] == "2.45rem", "the verb column is as wide as DELETE and no wider"


def _css_vars() -> dict[str, str]:
    return dict(re.findall(r"(--cx-[\w-]+):\s*([^;}]+?)\s*[;}]", delta.CSS))


def _columns(selector: str) -> list[str]:
    """The grid tracks of `selector`, with the shared `var(--cx-…)` widths resolved."""
    raw = re.search(rf"{re.escape(selector)} \{{[^}}]*grid-template-columns:([^;]*);", delta.CSS)[1]
    names = _css_vars()
    return [names.get(m[1], m[0]) if (m := re.fullmatch(r"var\((--[\w-]+)\)", t)) else t
            for t in raw.split()]


def _head_columns() -> list[str]:
    return _columns(".cx-head")


def test_the_group_header_names_the_two_numbers_over_their_own_columns():
    """`+1` beside `12` had no names and was read as one number. The group's header line
    says which is "added" and which is the "total", and its last two tracks are the row's
    last two — the same variables, not a copy that drifts the day one is widened."""
    page = delta.render([_row(why=[])])
    assert ('<div class="cx-kind cx-kind-cols"><span>REST APIs <span class="cx-count">1</span>'
            '</span><span class="cx-colh cx-colh-added" data-tip="Complexity this branch added to '
            'the flow">added</span><span class="cx-colh cx-colh-total" data-tip="Complexity of the '
            'whole flow, on this branch">total</span></div>'
            in page)
    head, kind = _columns(".cx-head"), _columns(".cx-kind-cols")
    assert kind[-2:] == head[-2:] and len(kind) == 3
    raw = re.search(r"\.cx-head \{[^}]*grid-template-columns:([^;]*);", delta.CSS)[1]
    assert "var(--cx-col-added) var(--cx-col-total)" in raw, "the widths are shared, not copied"
    # The right edges line up: the row's right padding plus the list's border.
    assert re.search(r"\.cx-kind-cols \{[^}]*padding-right:calc\(\.8rem \+ 1px\)", delta.CSS)
    assert re.search(r"\.cx-head \{[^}]*padding:[^;]*\.8rem [^;]*;", delta.CSS)


def test_the_straight_line_callees_are_not_listed_under_their_caller():
    """`+ VisitDto×8, Owner×2, Vet×3 +1` at the foot of a box was read by nobody: the
    getters it named are exactly what the graph leaves out to stay readable. Neither the
    line nor the legend that explained it is drawn any more — and which callees are left
    out, and the score, are untouched."""
    nodes = [_gnode("a.Ctl#go", calls=["a.Map#toDto", "a.Owner#getId", "a.Owner#getName"]),
             _gnode("a.Map#toDto", cog=3), _gnode("a.Owner#getId"), _gnode("a.Owner#getName")]
    out, drawn = delta._graph(nodes)
    assert "cg-also" not in out and "Owner×2" not in out and "getName" not in out
    assert "straight-line callees" not in out and "Class×N" not in out, "nor in the legend"
    assert drawn == {"a.Ctl#go", "a.Map#toDto"}, "the same methods are drawn as before"
    assert ".cg-also" not in delta.CSS, "no rule left for a class nothing emits"


def test_the_path_is_the_link_into_the_editor_and_the_rest_of_the_head_folds():
    """Victor, 4 Nov 2026: the ↗ goes. The path underlines under the pointer like the names
    in the call graph and opens the editor; the caret and the rest of the head fold."""
    found = ("/repo/a/src/main/java/app/VisitRestController.java", 42)
    orig = delta.entry_source
    delta.entry_source = lambda key: found
    try:
        row = delta.render_row(_row(entry="app.VisitRestController#addVisit",
                                    why=[], graph=[]), 12)
    finally:
        delta.entry_source = orig
    head = re.search(r"<summary.*?</summary>", row, re.S)[0]
    [a] = re.findall(r"<a\b[^>]*>.*?</a>", head, re.S)
    assert 'class="cx-path"' in a and f'href="vscode://file/{found[0]}:{found[1]}:1"' in a
    assert "↗" not in row and "cg-go\"" not in delta.CSS
    assert 'class="cx-caret"' in head.split("<a ")[0], "the caret is no part of the link"
    assert "a.cx-path:hover" in delta.CSS
    # The script no longer swallows clicks on the head: only a link keeps the fold still.
    assert "preventDefault(); " not in delta.TOGGLE_JS.split("cg-n")[0]
    assert "closest('a')" in delta.TOGGLE_JS
    # An entry point this checkout cannot place has no link.
    assert "<a " not in delta._path_cell(_row(entry="nothing::at::all"))


def test_the_open_rows_are_named_in_the_address_bar():
    """Open two rows, reload, and both are open again: every open row is one `cx` in the
    query string, beside the tab strip's `#complexity` rather than inside it — the tab
    strip rewrites the hash on every click, and would wipe anything riding in it."""
    row = delta.render_row(_row(path="/api/owners", method="GET", why=[]), 12)
    assert row.startswith('<details class="cx-row cx-up" data-cx="GET /api/owners">'), row[:90]
    js = delta.render([_row(why=[])])
    assert "history.replaceState" in js, "the page's one way of writing the address"
    assert "location.hash" in js and "location.search" in js, "the hash is left as it was"
    assert "getAll(P)" in js and "q.append(P" in js and "var P = 'cx'" in js
    # Heard on `document` in the capture phase (`toggle` does not bubble), so a row swapped
    # in by a rebuild (`data-hr-swap="keep"`) is remembered without re-running the script.
    assert "document.addEventListener('toggle'" in js, "closing one takes it out again"
    assert 'data-hr-swap="keep"' in js
    # A deleted entry point opens onto nothing, so it has nothing to remember.
    assert "data-cx" not in delta.render_row(_row(gone=True, why=[]), 12)



def test_the_bar_the_delta_and_the_total_carry_no_tooltip():
    """The row already shows the total, the green part and the +1; a tooltip repeating them
    was noise (Victor, 10 Nov 2026)."""
    for row in (_row(now=12, was=11, delta=1, why=[]), _row(now=6, was=11, delta=-5, why=[]),
                _row(now=4, was=None, delta=None, why=[]), _row(now=7, was=7, delta=0, why=[]),
                _row(gone=True, why=[])):
        html = delta.render_row(row, 20)
        assert not re.search(r'class="cx-(?:bar|badge|n)" data-tip', html), html
        assert "Total complexity of this flow" not in html
