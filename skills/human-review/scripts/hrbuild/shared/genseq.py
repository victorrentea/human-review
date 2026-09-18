"""Generated-sequence sidecars: Spring handlers, call details, the test behind a diagram."""
from __future__ import annotations

import functools
import json
import os
import re
from pathlib import Path

# `Browser → Backend: POST /api/owners/{ownerId}/pets/{petId}/visits` — a call arrow's
# title, as the generator writes it. The route is the contract; the method that answers it
# is where a reviewer actually has to go, and nothing in a trace knows its name.
GENSEQ_CALL_TITLE = re.compile(
    r"(?:\u2192|->)\s*\w+\s*:\s*"
    r"(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?P<route>/\S*)\s*$"
)

MAPPING_ANNOTATION = re.compile(r"@(?P<kind>Get|Post|Put|Patch|Delete|Request)Mapping\b")
# `@GetMapping(produces = "application/json")` names a media type, not a route, so the
# first string literal in the arguments is only the path when it is positional or spelled
# `value =`/`path =`. Anything else leaves the mapping at its class-level base.
MAPPING_NAMED_PATH = re.compile(r'(?:value|path)\s*=\s*\{?\s*"(?P<path>[^"]*)"')
MAPPING_POSITIONAL_PATH = re.compile(r'^\s*\{?\s*"(?P<path>[^"]*)"')
REQUEST_METHOD = re.compile(r"RequestMethod\.(?P<verb>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)")
TYPE_DECL = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed)\s+)*"
    r"(?:class|interface|record|enum)\s+(?P<name>\w+)"
)
METHOD_NAME = re.compile(r"(?P<name>\w+)\s*\(")
HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".idea", ".gradle"}


def _annotation_span(lines, i: int):
    """The parenthesised argument text of the annotation on line `i`, and where it ends.

    Read as one balanced span rather than per line: a mapping wraps as soon as it carries
    `produces`, and an `@ApiResponse` above it wraps over four — whose continuation lines
    look exactly like a method declaration to a line scan, which is how `@Content(` once
    became the name of the method under it."""
    text, depth, started = "", 0, False
    for j in range(i, len(lines)):
        for ch in lines[j]:
            if ch == "(":
                depth += 1
                started = True
                if depth == 1:
                    continue
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text, j
            if started:
                text += ch
        if not started and lines[j].strip():
            return "", j
    return text, len(lines) - 1


def _mapping_path(args: str) -> str:
    m = MAPPING_NAMED_PATH.search(args) or MAPPING_POSITIONAL_PATH.match(args)
    return m["path"] if m else ""


def _join_route(base: str, sub: str) -> str:
    parts = [p for p in (base.strip("/"), sub.strip("/")) if p]
    return "/" + "/".join(parts)


def _controller_routes(source: str, rel: str, found: dict) -> None:
    """Every `VERB /route` this file handles, mapped to `Class.method` and its line.

    A line scan, not a parser: the three things it needs — the class-level base path, a
    method's mapping annotation, and the declaration under it — are each one line of Java,
    and a mapping annotation is never anywhere else in a file. Annotations pile up until a
    declaration consumes them, so the comments and the other annotations Spring code puts
    between `@PostMapping` and its method cost nothing."""
    lines = source.splitlines()
    base, cls, pending, i = "", Path(rel).stem, None, 0
    while i < len(lines):
        stripped, at = lines[i].strip(), i
        i += 1
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if stripped.startswith("@"):
            args, end = _annotation_span(lines, at)
            i = end + 1
            mapping = MAPPING_ANNOTATION.match(stripped)
            if mapping:
                verbs = ([mapping["kind"].upper()] if mapping["kind"] != "Request"
                         else [m["verb"] for m in REQUEST_METHOD.finditer(args)] or list(HTTP_VERBS))
                pending = (verbs, _mapping_path(args))
            continue
        declared = TYPE_DECL.match(stripped)
        if declared:
            cls = declared["name"]
            base, pending = (pending[1] if pending else ""), None
            continue
        name = METHOD_NAME.search(stripped) if pending else None
        if name:
            verbs, sub = pending
            for verb in verbs:
                found[f"{verb} {_join_route(base, sub)}"] = (rel, at + 1, f"{cls}.{name['name']}")
        pending = None


@functools.lru_cache(maxsize=None)
def spring_handlers(root: Path) -> dict:
    """`VERB /route` → (repo-relative file, line, `Class.method`) for this checkout.

    Only files that declare themselves controllers are opened, so a repo with no Spring in
    it pays one directory walk and nothing else. Resolved here rather than in the diagram
    generator because that generator sees a trace, which carries the route and no code."""
    found: dict = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".java"):
                continue
            path = Path(folder) / name
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "@RestController" not in source and "@Controller" not in source:
                continue
            _controller_routes(source, str(path.relative_to(root)), found)
    return found


def _with_handlers(index: dict, root: Path) -> dict:
    """Hang the entry point on every call arrow whose route this checkout still serves.

    A route the branch deleted simply gets no row — the base-ref sidecar is resolved
    against the work tree too, and a link into a method that is no longer there would be
    worse than the route alone."""
    handlers = None
    for entry in (index.get("details") or {}).values():
        called = GENSEQ_CALL_TITLE.search(entry.get("title", ""))
        if not called:
            continue
        if handlers is None:
            handlers = spring_handlers(root)
        hit = handlers.get(f'{called["verb"]} {called["route"]}')
        if hit:
            rel, line, name = hit
            entry["handler"] = {"name": name,
                                "href": f"vscode://file/{(root / rel).resolve()}:{line}:1"}
    return index


def genseq_details(rel: str, root: Path) -> str:
    """The sidecar the generator filed beside the diagram, carried into the page.

    Inlined rather than fetched: review.html is opened from file://, where fetch() of a
    neighbouring file is blocked, and the guide has to survive being mailed as one file."""
    if not rel.endswith(".genseq.puml"):
        return ""
    sidecar = root / (rel[: -len(".puml")] + ".json")
    if not sidecar.is_file():
        return ""
    return _details_carrier(sidecar, root)


def _details_carrier(sidecar: Path, root: Path) -> str:
    # `<` is the only character that can end a <script> block early, and a JSON string
    # may legally spell it \\u003c — so the payload stays valid JSON and inert to the
    # HTML parser without any un-escaping step on the other side.
    index = _with_handlers(json.loads(sidecar.read_text(encoding="utf-8")), root)
    payload = json.dumps(index, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" class="genseq-details">{payload}</script>'


def genseq_details_at_render(row, assets: Path, root: Path) -> str:
    """The sidecar as it stood when this diagram was drawn — not as it stands now.

    The handles PlantUML drew into the picture are generation-time ids derived from the
    payload behind each arrow, and a generated payload carries per-run values: a
    timestamp inside a description, a database-assigned row id. So every arrow whose body
    holds one is re-identified by the next run of the suite, while its SQL neighbours —
    whose statement text does not move — keep the id they had.

    That matters because the picture is rendered once, early, and the report is built
    later. Anything in between that re-runs the acceptance suite (the trace capture does)
    advances the sidecar a generation past the pictures. Read live, it then hands the page
    ids that no arrow in the SVG carries, and `GENSEQ_JS` unwraps each of them as a dead
    handle: the `200 ⊕` on an HTTP response stops opening its JSON body and becomes plain
    text, while `select owners ⊕` beside it still works. The failure is silent and reads
    as "the response handles were never wired".

    So the renderer copies the sidecar next to the SVG it drew from it, and this prefers
    that copy. The work tree stays the fallback, for a manifest written before the
    renderer took the snapshot.
    """
    name = (row.get("new_details") or "").strip()
    if name and (assets / name).is_file():
        return _details_carrier(assets / name, root)
    return genseq_details(row["source"], root)


def genseq_details_at_base(row, assets: Path, root: Path) -> str:
    """The same sidecar as of the base ref, for the diagram's `Old` pane.

    The handles PlantUML draws are generation-time ids, and an id is derived from the
    payload — so a request body that changed on this branch has a different id on each
    side. With only the work tree's sidecar in the page, every such handle on the old
    render resolved to nothing and `GENSEQ_JS` dropped it as a dead one: the pane looked
    wired (cursor, hit area) and expanded nothing. The ids that happened to survive —
    SQL whose statement did not change — kept working, which is what made the failure
    read as "bodies are broken" rather than "the payloads for that side are missing".

    Both carriers go into the page and the reader merges them, work tree first. Ids are
    content-derived, so an id in both sides means the same payload on both.
    """
    name = (row.get("old_details") or "").strip()
    return _details_carrier(assets / name, root) if name and (assets / name).is_file() else ""


#: `[[src://<test>:<line>{tip} <scenario title>]]` — the handle the sequence generators
#: leave on the scenario a picture draws. It is the only record of *which* tests in a file
#: carry `@generate_sequence`: the tag is in the source, but the generator is what decides
#: it produced a drawing, and the drawing is what this page can link to.
#:
#: Two places carry it, and both are read. `title …` is where it lives now that a picture
#: is one scenario — the scenario names the diagram. `== … ==` is the chapter divider a
#: picture drawn per *file* put above each scenario inside it, which is still what the
#: committed diagrams of a repository that has not been through the generator split look
#: like, and what `renderDiagram` still writes when handed several scenarios at once.
GENSEQ_HANDLE = re.compile(
    r"^(?:title|==)\s*\[\[src://(?P<path>[^\s:{\]]+):(?P<line>\d+)"
    r"(?:\{(?P<tip>[^}]*)\})?\s*(?P<title>[^\]]*?)\s*\]\]")


@functools.lru_cache(maxsize=None)
def _declared_test(puml: Path, root: Path) -> str | None:
    """The test a diagram names on its own title line, if it names one this checkout has.

    The generator writes `title [[src://<repo-relative test>:<line>{…} <scenario>]]`, which
    is the only statement of provenance that survives the file being moved: a generator is
    free to file its output wherever it likes — beside the test, or in one `generated/`
    directory — and the picture still says what drew it.
    """
    try:
        with puml.open(encoding="utf-8") as fh:
            for line in fh:
                m = GENSEQ_HANDLE.match(line.strip())
                if m and (root / m["path"]).is_file():
                    return m["path"]
                if line.startswith(("participant ", "actor ")):
                    break     # past the header: the arrows' handles are not the test
    except OSError:
        pass
    return None


def test_of_genseq(source: str, root: Path) -> str:
    """Which test a generated sequence was drawn from.

    The diagram is asked first, because it says so itself — see `_declared_test`. That is
    what lets a repository move its generated diagrams out of the source tree without the
    pairing on this page quietly pointing at files that are not there.

    Everything below is the older, path-based derivation, kept for diagrams that carry no
    handle (a run whose working tree the generator could not read) and for pages built
    before the handle existed. It used to be the file name with `.genseq.puml` cut off,
    because there was one diagram per test file. There is now one per scenario, named
    `<test>.<scenario-slug>.genseq.puml` — and a test file has dots of its own
    (`add-visit.spec.ts`), so the slug cannot be told from the extension by looking. The
    checkout is asked instead: cut the suffix, and if what is left is not a file, cut one
    more dotted segment and try again.

    A diagram whose test is not in this checkout at all — the case `_unquoted_note` exists
    for — cannot be checked that way, so the slug is cut on its shape: all lowercase,
    digits and dashes, over something that still has an extension.
    """
    declared = _declared_test(root / source, root) if source.endswith(".genseq.puml") else None
    if declared:
        return declared
    rel = source[: -len(".genseq.puml")] if source.endswith(".genseq.puml") else source
    if (root / rel).is_file():
        return rel
    head, dot, slug = rel.rpartition(".")
    if dot and (root / head).is_file():
        return head
    if dot and "." in head and re.fullmatch(r"[a-z0-9-]+", slug):
        return head
    return rel


@functools.lru_cache(maxsize=None)
def genseq_by_test(root: Path) -> dict[str, tuple[str, ...]]:
    """Every generated sequence in this checkout, grouped by the test that drew it.

    It used to be a glob next to the test — `<test>.*.genseq.puml` in the test's own
    directory — which was true of the only generator that existed and false the moment a
    repository filed its diagrams anywhere else (petclinic moved both suites' output into
    `petclinic-test/generated/`). The unchanged diagrams then silently stopped appearing
    on the page: not an error, just a tab that showed the changed ones and quietly dropped
    the rest, which is the worst way for a page of evidence to be wrong.

    One walk per build, shared by every tab, skipping the directories nothing generated
    ever lives in. Sorted, so the page orders the pictures the same way on every run.
    """
    found: dict[str, list[str]] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if not name.endswith(".genseq.puml"):
                continue
            rel = str((Path(folder) / name).relative_to(root))
            found.setdefault(test_of_genseq(rel, root), []).append(rel)
    return {test: tuple(sorted(pumls)) for test, pumls in found.items()}


def pair_anchor(rel: str) -> str:
    """The id of the pair a sequence is drawn in, derived from the diagram's own path.

    Derived rather than counted, because the thing that links to it — the 🕵️ on a
    covering-tests row, a tab away — knows the test and nothing else about this tab. A
    path is unique inside a checkout, so the slug is too. It is the *diagram's* path that
    is passed now, one per scenario, so two scenarios of one file get two anchors."""
    return "seq-" + re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")
