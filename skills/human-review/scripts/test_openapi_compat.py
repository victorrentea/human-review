#!/usr/bin/env python3
"""The contract tab's affected-operation list, pinned — because it fails silently.

The failure this guards against left no mark on the page. `openapi-compat.py` built its
list out of OpenAPITools/openapi-diff's `changedOperations` key, which is computed one
`paths` entry at a time and does not follow a `$ref`. On the change that exposed it the
whole delta lived in `components.schemas`, the `paths` section was byte-identical, and
the tab reported **4** affected operations behind a confident COMPATIBLE seal. Eleven had
moved. Nothing looked wrong: the four rows that were there rendered perfectly.

That shape — a list that is right about what it shows and silent about what it drops — is
exactly what a test has to hold, because no reviewer can catch it by reading the page. So:

  * the eleven operations of the petclinic `VisitDto` change, by name, from the real
    `$ref` topology (`OwnerDto -> PetDto -> VisitDto`, three levels of nesting);
  * every one of them rendering its *actual* added fields, not an empty row — a list that
    is long but blank is the same lie with more scrolling;
  * the honest-degradation path: with `oasdiff` gone the seal must stop claiming to be
    the whole story.

Run it directly (`python3 test_openapi_compat.py`) or under pytest. The oasdiff-backed
tests skip when the binary is not installed; the rest never touch a subprocess.
"""
from __future__ import annotations

import html as html_module
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPAT = HERE / "openapi-compat.py"


def _load(stem: str, filename: str):
    spec = importlib.util.spec_from_file_location(stem, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module
    spec.loader.exec_module(module)
    return module


oac = _load("openapi_compat", "openapi-compat.py")
oad = _load("openapi_diff", "openapi-diff.py")

HAVE_OASDIFF = bool(shutil.which(os.environ.get("OASDIFF_BIN", "oasdiff")))
SKIP_REASON = "oasdiff is not installed — the ref-resolving engine cannot be exercised"


# ── the fixture: petclinic's ref topology, reduced to what the bug needs ──────────
# Deliberately the real graph and not a flat pair: the four operations the old code
# found are the ones that name VisitDto/VisitFieldsDto directly, and the seven it lost
# are the ones that reach them through PetDto and OwnerDto. A flatter fixture would
# pass under the bug.
BASE_SPEC = """
openapi: 3.0.1
info: {title: petclinic, version: "1"}
paths:
  /api/owners:
    get:
      operationId: listOwners
      responses:
        "200":
          content:
            application/json:
              schema: {type: array, items: {$ref: '#/components/schemas/OwnerDto'}}
  /api/owners/{ownerId}:
    get:
      operationId: getOwner
      parameters:
        - {name: ownerId, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          content:
            '*/*': {schema: {$ref: '#/components/schemas/OwnerDto'}}
  /api/owners/{ownerId}/pets/{petId}:
    get:
      operationId: getOwnersPet
      parameters:
        - {name: ownerId, in: path, required: true, schema: {type: integer}}
        - {name: petId, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          content:
            '*/*': {schema: {$ref: '#/components/schemas/PetDto'}}
  /api/owners/{ownerId}/pets/{petId}/visits:
    post:
      operationId: addVisit
      parameters:
        - {name: ownerId, in: path, required: true, schema: {type: integer}}
        - {name: petId, in: path, required: true, schema: {type: integer}}
      requestBody:
        content:
          application/json: {schema: {$ref: '#/components/schemas/VisitFieldsDto'}}
      responses:
        "204": {description: done}
  /api/pets:
    get:
      operationId: listPets
      responses:
        "200":
          content:
            application/json:
              schema: {type: array, items: {$ref: '#/components/schemas/PetDto'}}
  /api/pets/{petId}:
    get:
      operationId: getPet
      parameters:
        - {name: petId, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          content:
            '*/*': {schema: {$ref: '#/components/schemas/PetDto'}}
    put:
      operationId: updatePet
      parameters:
        - {name: petId, in: path, required: true, schema: {type: integer}}
      requestBody:
        content:
          application/json: {schema: {$ref: '#/components/schemas/PetDto'}}
      responses:
        "204": {description: done}
  /api/visits:
    get:
      operationId: listVisits
      responses:
        "200":
          content:
            application/json:
              schema: {type: array, items: {$ref: '#/components/schemas/VisitDto'}}
    post:
      operationId: addStandaloneVisit
      requestBody:
        content:
          application/json: {schema: {$ref: '#/components/schemas/VisitDto'}}
      responses:
        "204": {description: done}
  /api/visits/{visitId}:
    get:
      operationId: getVisit
      parameters:
        - {name: visitId, in: path, required: true, schema: {type: integer}}
      responses:
        "200":
          content:
            '*/*': {schema: {$ref: '#/components/schemas/VisitDto'}}
    put:
      operationId: updateVisit
      parameters:
        - {name: visitId, in: path, required: true, schema: {type: integer}}
      requestBody:
        content:
          application/json: {schema: {$ref: '#/components/schemas/VisitFieldsDto'}}
      responses:
        "204": {description: done}
  /api/vets:
    get:
      operationId: listVets
      responses:
        "200":
          content:
            application/json:
              schema: {type: array, items: {$ref: '#/components/schemas/VetDto'}}
components:
  schemas:
    OwnerDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        lastName: {type: string}
        pets:
          type: array
          items: {$ref: '#/components/schemas/PetDto'}
      required: [id, lastName]
    PetDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        name: {type: string}
        visits:
          type: array
          items: {$ref: '#/components/schemas/VisitDto'}
      required: [id, name]
    VisitDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        description: {type: string}
      required: [description, id]
    VisitFieldsDto:
      type: object
      properties:
        description: {type: string}
      required: [description]
    VetDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        lastName: {type: string}
"""

# The real change, to the byte: three fields on the response DTO (two of them readOnly),
# one on the request DTO. `paths` is untouched — that is the whole point.
HEAD_SPEC = BASE_SPEC.replace(
    """    VisitDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        description: {type: string}
      required: [description, id]""",
    """    VisitDto:
      type: object
      properties:
        id: {type: integer, format: int32}
        description: {type: string}
        vetFirstName: {type: string, readOnly: true}
        vetId: {type: integer, format: int32, minimum: 0}
        vetLastName: {type: string, readOnly: true}
      required: [description, id]""",
).replace(
    """    VisitFieldsDto:
      type: object
      properties:
        description: {type: string}
      required: [description]""",
    """    VisitFieldsDto:
      type: object
      properties:
        description: {type: string}
        vetId: {type: integer, format: int32, minimum: 0}
      required: [description]""",
)

# Five name VisitDto/VisitFieldsDto themselves; six reach them only through OwnerDto or
# PetDto and are precisely the ones a differ that stops at a `$ref` cannot see.
DIRECT = {
    ("GET", "/api/visits"),
    ("POST", "/api/visits"),
    ("GET", "/api/visits/{visitId}"),
    ("PUT", "/api/visits/{visitId}"),
    ("POST", "/api/owners/{ownerId}/pets/{petId}/visits"),
}
TRANSITIVE = {
    ("GET", "/api/owners"),
    ("GET", "/api/owners/{ownerId}"),
    ("GET", "/api/owners/{ownerId}/pets/{petId}"),
    ("GET", "/api/pets"),
    ("GET", "/api/pets/{petId}"),
    ("PUT", "/api/pets/{petId}"),
}
AFFECTED = DIRECT | TRANSITIVE
ADDED_FIELDS = {"vetFirstName", "vetId", "vetLastName"}


def specs(tmp: Path):
    base, head = tmp / "before.yaml", tmp / "after.yaml"
    base.write_text(BASE_SPEC, encoding="utf-8")
    head.write_text(HEAD_SPEC, encoding="utf-8")
    return base, head


def compat(*args, **env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(COMPAT), *args],
                          capture_output=True, text=True, env={**os.environ, **env})


# ── the list ─────────────────────────────────────────────────────────────────────
def test_every_operation_the_schema_change_reaches_is_listed():
    if not HAVE_OASDIFF:
        print(f"skip {SKIP_REASON}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base, head = specs(Path(tmp))
        proc = compat(str(base), str(head), "--json", "--no-cross-check")
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)

    listed = {(o["method"], o["path"]) for o in report["breaks"] + report["additive"]}
    assert listed == AFFECTED, (
        f"expected {len(AFFECTED)} affected operations, got {len(listed)}\n"
        f"  missing: {sorted(AFFECTED - listed)}\n"
        f"  unexpected: {sorted(listed - AFFECTED)}"
    )
    # The six that a paths-only differ cannot see are the reason this test exists.
    assert TRANSITIVE <= listed
    assert report["complete"] is True and report["source"] == "oasdiff"


def test_optional_additions_stay_non_breaking():
    if not HAVE_OASDIFF:
        print(f"skip {SKIP_REASON}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base, head = specs(Path(tmp))
        proc = compat(str(base), str(head), "--state", "--no-cross-check")
        assert proc.returncode == 0, proc.stderr
    # Optional properties in both directions: a longer list must not become a scarier one.
    assert proc.stdout.strip() == oac.COMPATIBLE


def test_a_required_addition_to_a_request_body_is_breaking():
    """The verdict still has to be able to say no — a list of 11 safe rows proves nothing
    if the engine cannot fail."""
    if not HAVE_OASDIFF:
        print(f"skip {SKIP_REASON}")
        return
    breaking = HEAD_SPEC.replace("      required: [description]",
                                 "      required: [description, vetId]")
    with tempfile.TemporaryDirectory() as tmp:
        base, head = Path(tmp) / "before.yaml", Path(tmp) / "after.yaml"
        base.write_text(BASE_SPEC, encoding="utf-8")
        head.write_text(breaking, encoding="utf-8")
        proc = compat(str(base), str(head), "--state", "--no-cross-check")
        assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == oac.INCOMPATIBLE


# ── the rows ─────────────────────────────────────────────────────────────────────
def rows_by_operation(fragment: str) -> dict:
    """Each `oac-row` keyed by its operation, so a row can be asked what it drew."""
    out = {}
    for chunk in re.split(r'(?=<div class="oac-row)', fragment):
        head = re.search(r'oac-verb oac-\w+">(\w+)</span><code class="oac-path">([^<]+)',
                         chunk)
        if head:
            out[(head.group(1), head.group(2))] = chunk
    return out


def test_every_listed_operation_draws_the_fields_that_moved():
    """A row with no shape under it teaches the reader the panels are empty."""
    if not HAVE_OASDIFF:
        print(f"skip {SKIP_REASON}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base, head = specs(Path(tmp))
        proc = compat(str(base), str(head), "--no-cross-check")
        assert proc.returncode == 0, proc.stderr
        fragment = proc.stdout

    rows = rows_by_operation(fragment)
    assert set(rows) == AFFECTED, sorted(set(rows) ^ AFFECTED)
    for key, chunk in sorted(rows.items()):
        drawn = set(re.findall(r'oat-added" style="--d:\d+"><span class="oat-key">([^<]+)',
                               chunk))
        assert drawn, f"{key[0]} {key[1]} rendered an empty row — no added field is drawn"
        assert drawn <= ADDED_FIELDS, f"{key} drew something unexpected: {drawn}"
        assert "vetId" in drawn, f"{key} does not draw vetId, which every one of them gains"


# ── honest degradation ───────────────────────────────────────────────────────────
def test_a_list_that_may_be_short_never_gets_a_clean_seal():
    """The fallback engine's list is a lower bound; the seal has to say so on its face."""
    partial = {"state": oac.COMPATIBLE, "breaks": [], "deprecated": [], "elsewhere": [],
               "additive": [{"method": "GET", "path": "/api/visits", "note": "changed"}],
               "complete": False, "source": f"openapi-diff {oac.VERSION}"}
    fragment = oac.render(partial, None, "provenance", "")

    seal = re.search(r'oac-seal">([^<]+)', fragment).group(1)
    assert seal != "COMPATIBLE", "a bare COMPATIBLE seal over a list we know can be short"
    assert "PARTIAL" in seal, seal
    assert "oac-incomplete" in fragment, "no band explaining why the list may be short"
    assert "brew install oasdiff" in fragment, "the band does not say how to fix it"
    assert "$ref" in fragment, "the band does not say what the fallback cannot follow"

    whole = oac.render({**partial, "complete": True}, None, "provenance", "")
    assert re.search(r'oac-seal">([^<]+)', whole).group(1) == "COMPATIBLE"
    assert "oac-incomplete" not in whole, "the band shows up when the list is complete"


def test_no_changes_does_not_claim_two_different_specs_are_identical():
    """A reworded description moves the spec without moving the contract. Both halves of
    that sentence have to survive, or the seal contradicts the tab next to it."""
    moved = oac.render({"state": oac.NO_CHANGES, "breaks": [], "additive": [],
                        "deprecated": [], "elsewhere": [], "complete": True,
                        "identical": False}, None, "provenance", "")
    assert "structurally identical" not in moved
    assert "The specs differ" in moved

    same = oac.render({"state": oac.NO_CHANGES, "breaks": [], "additive": [],
                       "deprecated": [], "elsewhere": [], "complete": True,
                       "identical": True}, None, "provenance", "")
    assert "structurally identical" in same


def test_the_fallback_engine_declares_itself_incomplete():
    """Whatever `read_report` returns, it must never claim to be the whole list."""
    result = oac.read_report({"changedOperations": [], "newEndpoints": [],
                              "missingEndpoints": [], "incompatible": False})
    assert result["complete"] is False


def test_missing_oasdiff_is_a_fallback_not_a_crash():
    saved = oac.OASDIFF
    try:
        oac.OASDIFF = "oasdiff-that-is-not-installed"
        assert oac.oasdiff_changelog(Path("a.yaml"), Path("b.yaml")) is None
        assert oac.oasdiff_version() is None
    finally:
        oac.OASDIFF = saved


# ── truncations that used to be silent ───────────────────────────────────────────
def test_a_cut_disagreement_list_says_how_much_it_cut():
    ours = {"breaking": [f"GET /api/{n} — gone" for n in range(20)], "subjects": 20}
    fragment = oac.cross_check(oac.COMPATIBLE, ours)
    assert f"showing {oac.CROSS_CHECK_LIMIT} of 20" in re.sub("<[^>]+>", "", fragment)

    short = {"breaking": ["GET /api/one — gone"], "subjects": 1}
    assert "showing" not in re.sub("<[^>]+>", "", oac.cross_check(oac.COMPATIBLE, short))


def test_a_cut_value_says_how_much_it_cut():
    long_value = {"payload": "x" * 400}
    rendered = oad.show(long_value)
    assert "showing 160 of" in rendered, rendered
    assert oad.show({"a": 1}) == '{"a": 1}', "a value that fits must not gain a suffix"


def test_a_date_in_an_example_renders_instead_of_raising():
    """PyYAML turns `2013-01-01` into a `date`, which plain json.dumps refuses."""
    import datetime
    assert "2013-01-01" in oad.show([{"date": datetime.date(2013, 1, 1)}])


# ── the mislabel ─────────────────────────────────────────────────────────────────
def test_a_dto_on_both_sides_is_not_called_a_request_body():
    import yaml
    spec = yaml.safe_load(HEAD_SPEC)
    req, res = oad.request_side(spec), oad.response_side(spec)
    # VisitDto is the body of POST /api/visits *and* the response of two GETs.
    assert oad.side_note("VisitDto", req, res) == "request & response body"
    # VisitFieldsDto really is request-only, and must keep saying so.
    assert oad.side_note("VisitFieldsDto", req, res) == "request body"
    # OwnerDto is never posted anywhere.
    assert oad.side_note("OwnerDto", req, res) == "response body"


def test_the_paths_diff_admits_how_many_operations_a_schema_serves():
    """`paths` is byte-identical here; "0 operations moved" is true and useless alone."""
    import yaml
    spec = yaml.safe_load(HEAD_SPEC)
    served = oad.operations_touching(spec, {"VisitDto", "VisitFieldsDto"})
    assert {(p, m.upper()) for p, m in served} == {(p, m) for m, p in AFFECTED}


# ── the one line at the top of the tab ───────────────────────────────────────────
# It shipped for months as a hand-typed string in the content file: "25 changes, none
# breaking", green, over whatever the branch actually did. It happened to be right on the
# day it was written. These tests exist so it cannot go back to being a sentence somebody
# has to remember to update.
def _panel_text(fragment: str) -> str:
    stripped = re.sub("<[^>]+>", "", fragment.split("</style>")[-1])
    return re.sub(r"\s+", " ", html_module.unescape(stripped)).strip()


def _panel_class(fragment: str) -> str:
    return re.search(r'class="apiverdict (\w+)"', fragment).group(1)


def _result(state, breaks=(), additive=(), **extra):
    return {"state": state, "breaks": list(breaks), "additive": list(additive),
            "deprecated": [], "elsewhere": [], "complete": True, "source": "oasdiff",
            **extra}


def _op(method, path, n_reasons):
    return {"method": method, "path": path,
            "reasons": [([f"rule-{i}"], f"change {i}") for i in range(n_reasons)]}


def test_the_panel_has_exactly_three_colours():
    """Three states, three treatments. A fourth class means a fourth palette."""
    nothing = oac.panel(_result(oac.NO_CHANGES, identical=True), {"breaking": [], "subjects": 0})
    safe = oac.panel(_result(oac.COMPATIBLE, additive=[_op("GET", "/api/owners", 3)]),
                     {"breaking": [], "subjects": 1})
    broken = oac.panel(_result(oac.INCOMPATIBLE, breaks=[_op("GET", "/api/owners", 2)]),
                       {"breaking": ["OwnerDto — gone"], "subjects": 1})
    assert [_panel_class(f) for f in (nothing, safe, broken)] == ["none", "green", "red"]

    # Grey, and struck through — the CSS has to carry the strike, not just the class.
    assert ".apiverdict.none .v,.apiverdict.none .n{text-decoration:line-through}" in nothing
    # Both themes, through the mechanism the rest of the page uses.
    assert "@media (prefers-color-scheme:dark)" in safe
    # The green/red are the score chip's own values, not a fifth pair invented here.
    assert "rgba(46,158,91,.16)" in safe and "#6fce93" in safe
    assert "rgba(215,38,61,.16)" in broken and "#f0757f" in broken
    # Grey is not a hex at all: it is the page's tokens.
    for token in ("var(--code-bg)", "var(--muted)", "var(--line)"):
        assert token in nothing, token


def test_the_panel_counts_what_the_differ_found_and_nothing_else():
    """The count is the bug this panel was rewritten for: it must move with the result."""
    result = _result(oac.COMPATIBLE, additive=[_op("GET", "/api/owners", 3),
                                               _op("GET", "/api/pets", 22)])
    assert oac.change_count(result) == 25
    assert "25 changes, none breaking" in _panel_text(oac.panel(result, {"breaking": [],
                                                                        "subjects": 2}))
    # One more compatible change and the sentence moves with it. No fixture pins "25".
    result["additive"].append(_op("GET", "/api/vets", 1))
    assert "26 changes, none breaking" in _panel_text(oac.panel(result, {"breaking": [],
                                                                         "subjects": 3}))
    # A single change is not "1 changes".
    one = _result(oac.COMPATIBLE, additive=[_op("GET", "/api/vets", 1)])
    assert "1 change, none breaking" in _panel_text(oac.panel(one, {"breaking": [],
                                                                    "subjects": 1}))


def test_the_breaking_panel_names_both_counts():
    result = _result(oac.INCOMPATIBLE,
                     breaks=[_op("DELETE", "/api/visits/{id}", 2)],
                     additive=[_op("GET", "/api/owners", 3)])
    text = _panel_text(oac.panel(result, {"breaking": ["a", "b"], "subjects": 2}))
    # verdict · counts · who checked it — the same shape as the green line.
    assert text.startswith("Breaking changes · 5 changes, 2 breaking · checked by"), text
    assert "oasdiff" in text and "openapi-diff.py" in text

    single = _result(oac.INCOMPATIBLE, breaks=[_op("DELETE", "/api/visits/{id}", 1)])
    assert _panel_text(oac.panel(single, {"breaking": ["a"], "subjects": 1})).startswith(
        "Breaking change · 1 change, 1 breaking ·")


def test_nothing_moved_says_so_and_gets_out_of_the_way():
    same = oac.panel(_result(oac.NO_CHANGES, identical=True), {"breaking": [], "subjects": 0})
    assert _panel_text(same).startswith("No API changes · 0 changes · checked by")
    # A reworded description moves the spec without moving the contract; the panel must
    # not flatly claim the two files are the same, the way the seal below does not.
    moved = oac.panel(_result(oac.NO_CHANGES, identical=False), {"breaking": [], "subjects": 0})
    assert "0 changes a caller can see" in _panel_text(moved)


def test_a_disagreement_never_claims_both_tools_checked_it():
    """"checked by oasdiff and openapi-diff.py" is a claim that the two agreed. When they
    do not, the panel has to say the opposite — and it must not read as safe."""
    over_strict = oac.panel(_result(oac.COMPATIBLE, additive=[_op("GET", "/api/owners", 3)]),
                            {"breaking": ["VisitDto.vetId — type changed"], "subjects": 4})
    text = _panel_text(over_strict)
    assert _panel_class(over_strict) == "red", "a contested verdict rendered as safe"
    assert "checked by" not in text, text
    assert text == ("Verdict disputed · 3 changes, 1 breaking by openapi-diff.py, "
                    "none by oasdiff · the two differs disagree — one of them is wrong "
                    "about somebody's client"), text

    missed = oac.panel(_result(oac.INCOMPATIBLE, breaks=[_op("DELETE", "/api/x", 2)]),
                       {"breaking": [], "subjects": 1})
    missed_text = _panel_text(missed)
    assert _panel_class(missed) == "red"
    assert "checked by" not in missed_text
    assert "2 breaking by oasdiff, none by openapi-diff.py" in missed_text


def test_the_panel_credits_only_the_differ_that_actually_ran():
    """With oasdiff installed the Java tool is never invoked; crediting it would be a
    check that did not happen. The fallback is the other way round."""
    with_oasdiff = oac.panel(_result(oac.COMPATIBLE, additive=[_op("GET", "/api/o", 1)]),
                             {"breaking": [], "subjects": 1})
    assert "OpenAPITools/openapi-diff" not in with_oasdiff
    assert "github.com/oasdiff/oasdiff" in with_oasdiff

    fallback = _result(oac.COMPATIBLE, source=f"openapi-diff {oac.VERSION}", complete=False,
                       additive=[{"method": "GET", "path": "/api/owners", "note": "changed"}])
    text = _panel_text(oac.panel(fallback, {"breaking": [], "subjects": 1}))
    assert "OpenAPITools/openapi-diff" in text
    assert "oasdiff" in text and "lower bound" in text, (
        "a count from a list that cannot follow a $ref must not look whole")
    # An operation nobody itemised is still one change, not zero.
    assert "1 change," in text

    # Nobody to cross-check against is its own admission, not silence.
    alone = _panel_text(oac.panel(_result(oac.COMPATIBLE,
                                          additive=[_op("GET", "/api/o", 1)]), None))
    assert "the cross-check did not run" in alone


def test_the_panel_count_matches_what_oasdiff_reported():
    """End to end: the number on the page is the number the binary emitted."""
    if not HAVE_OASDIFF:
        print(f"skip {SKIP_REASON}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base, head = specs(Path(tmp))
        rendered = compat(str(base), str(head), "--panel", "--no-cross-check")
        assert rendered.returncode == 0, rendered.stderr
        raw = subprocess.run([os.environ.get("OASDIFF_BIN", "oasdiff"), "changelog",
                              str(base), str(head), "-f", "json"],
                             capture_output=True, text=True)
        entries = json.loads(raw.stdout.strip() or "[]")
    assert f"{len(entries)} changes, none breaking" in _panel_text(rendered.stdout)


def test_no_number_in_the_panel_is_typed_into_the_content_file():
    """The regression this replaced: `"25 changes, none breaking"` sat in content.json.
    Nothing in the generator may contain a literal count."""
    source = COMPAT.read_text(encoding="utf-8")
    body = source[source.index("def panel("):source.index("# ── the tool's markdown")]
    # "0 changes" is allowed: it is the no-change state's own honest constant, and it is
    # guarded by `state == NO_CHANGES`, not by somebody remembering to retype it.
    assert not re.search(r"[1-9]\d* change", body), "a count is hardcoded in the panel"


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
