#!/usr/bin/env python3
"""The container view, pinned to the sequences it is projected from.

Everything on the C2 tab is a claim about the architecture that nobody typed, so the two
ways it can be wrong are both silent: an edge that is not there (a real integration missing
from the picture) and an edge that is (an integration the system does not have). Both have
happened already in development and both are covered here:

  * `footer generated from real traces — do not edit` is the one-line form of a PlantUML
    element whose *block* form runs to `end footer`. Reading it as a block opener swallowed
    every arrow in the file and drew a diagram of four boxes and no lines — a picture that
    looked deliberate.
  * `[[genseq://0x8a{Click for the statement behind this call} select owners ⊕]]` is a
    labelled hyperlink. Stopping the URL at the first whitespace left `for the statement
    behind this call} select owners` as the message text, which sniffs as no protocol at
    all, and the Backend→DB line went out labelled `calls` instead of `SQL`.
  * The delta and a single side were two edge shapes: one counted operations, the other
    listed them, and `render` wanted the count. So the Diff pane — the one being looked at
    while developing — was right, and the New and Old panes went out with a Python list
    repr spread across the middle of the picture. There is one shape now, and the tests
    below render every pane rather than only the delta.

Run it directly (`python3 test_c2_from_sequence.py`) or under pytest.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("c2_from_sequence", HERE / "c2-from-sequence.py")
c2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c2)


def graph(text: str, containers: dict | None = None) -> c2.Graph:
    g = c2.Graph()
    containers = containers or {}
    rename = {n: v["as"] for n, v in containers.items() if v.get("as")}
    c2.parse_sequence(textwrap.dedent(text).strip() + "\n", g, "t.genseq.puml", rename)
    c2.classify(g, containers)
    return g


def edges(g: c2.Graph) -> set[tuple[str, str]]:
    return set(g.edges)


# --------------------------------------------------------------------------- projecting


def test_a_lifeline_is_a_container_and_a_message_is_a_call():
    g = graph("""
        @startuml
        participant Browser
        participant Backend
        participant DB
        Browser -> Backend: GET /api/owners
        Backend -> DB: select owners
        @enduml
    """)
    assert set(g.nodes) == {"Browser", "Backend", "DB"}
    assert edges(g) == {("Browser", "Backend"), ("Backend", "DB")}


def test_the_dashed_reply_is_not_a_second_edge():
    """It is the answer to a call already drawn. Kept, it gives every synchronous edge a
    twin pointing the wrong way — which on a C2 reads as "the database calls the backend"."""
    g = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        Backend --> Browser: 200
        @enduml
    """)
    assert edges(g) == {("Browser", "Backend")}


def test_a_container_calling_itself_is_c3s_business():
    g = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        Backend -> Backend: OwnerRepository.findById
        @enduml
    """)
    assert edges(g) == {("Browser", "Backend")}


def test_a_backwards_arrow_is_the_same_call_written_the_other_way():
    a = graph("@startuml\nBrowser -> Backend: x\n@enduml")
    b = graph("@startuml\nBackend <- Browser: x\n@enduml")
    assert edges(a) == edges(b) == {("Browser", "Backend")}


def test_the_same_call_many_times_is_one_line_with_a_count():
    g = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        Browser -> Backend: GET /api/owners
        Browser -> Backend: GET /api/pets
        @enduml
    """)
    e = g.edges[("Browser", "Backend")]
    assert sum(e["ops"].values()) == 3
    assert len(e["ops"]) == 2


def test_a_one_line_footer_is_not_a_block_and_swallows_nothing():
    """The regression that drew four boxes and no lines. `footer <text>` closes itself;
    only a bare `footer` opens a block that runs to `end footer`."""
    g = graph("""
        @startuml
        footer @generate_sequence in src/add-visit.spec.ts — generated from real traces, do not edit ❗
        participant Browser
        Browser -> Backend: GET /api/owners
        @enduml
    """)
    assert edges(g) == {("Browser", "Backend")}


def test_prose_inside_a_note_block_is_never_read_as_a_call():
    g = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        note over Backend
        Backend -> Payments: this sentence describes a call, it is not one
        end note
        @enduml
    """)
    assert edges(g) == {("Browser", "Backend")}


def test_an_alias_resolves_to_the_name_a_reader_sees():
    for decl in ('participant "Pet Clinic UI" as UI', 'participant UI as "Pet Clinic UI"'):
        g = graph(f"""
            @startuml
            {decl}
            UI -> Backend: GET /api/owners
            @enduml
        """)
        assert edges(g) == {("Pet Clinic UI", "Backend")}


# --------------------------------------------------------------------------- protocols


def test_a_route_is_recognised_wherever_the_prose_above_it_ends():
    """The third regression, and the reason the protocol is decided on the split parts
    rather than on the joined sentence: `\\b/api/` never matches after a space — neither
    character is a word character — so `List owners GET /api/owners` came out `calls`
    while `Get an owner by ID GET /api/owners/{id}` beside it came out `HTTP`. Two lines
    into the same box, labelled two different things, for no reason a reader could see."""
    for prose in ("List owners", "Get an owner by ID", "", "getPet"):
        g = graph(f"@startuml\nBrowser -> Backend: {prose}\\nGET /api/owners\n@enduml")
        assert c2.edge_protocol(g.edges[("Browser", "Backend")]) == "HTTP", prose


def test_the_protocol_is_read_off_the_message_through_plantumls_link_markup():
    """The second regression: the label a reader sees is the text after the tooltip, and
    a URL pattern that ate into the tooltip left the SQL unrecognisable."""
    g = graph("""
        @startuml
        Backend -> DB: [[genseq://0x8a{Click for the statement behind this call} select owners ⊕]]
        @enduml
    """)
    assert c2.edge_protocol(g.edges[("Backend", "DB")]) == "SQL"


def test_a_two_line_label_keeps_the_line_the_verb_is_on():
    g = graph("@startuml\nBrowser -> Backend: Get an owner by ID\\nGET /api/owners/{id}\n@enduml")
    assert c2.edge_protocol(g.edges[("Browser", "Backend")]) == "HTTP"


def test_an_edge_reached_two_ways_names_both_rather_than_voting():
    g = graph("""
        @startuml
        Orders -> Shipping: POST /internal/ship
        Orders -> Shipping: publish ShipmentRequested
        @enduml
    """)
    assert c2.edge_protocol(g.edges[("Orders", "Shipping")]) == "HTTP + message"


def test_a_message_that_says_nothing_recognisable_says_so():
    g = graph("@startuml\nA -> B: does a thing\n@enduml")
    assert c2.edge_protocol(g.edges[("A", "B")]) == "calls"


def test_an_async_arrow_is_recorded_even_when_the_words_are_silent():
    """`->>` is the only evidence of asynchrony a wordless message leaves, and it is enough
    to label the line: an edge the reader would otherwise read as a blocking call."""
    g = graph("@startuml\nOrders ->> Bus: does a thing\n@enduml")
    e = g.edges[("Orders", "Bus")]
    assert e["async"] and c2.edge_protocol(e) == "async"


# --------------------------------------------------------------------------- inference


def test_the_four_rules_fire_in_order_and_each_says_it_did():
    g = graph("""
        @startuml
        actor Vet
        participant Backend
        participant DB
        database Warehouse
        Vet -> Backend: GET /api/vets
        Backend -> DB: select vets
        Backend -> Warehouse: select facts
        @enduml
    """, containers={"Backend": {"kind": "container", "tech": "Spring Boot"}})
    assert (g.nodes["Backend"]["kind"], g.nodes["Backend"]["inferredBy"]) == ("container", "config")
    assert g.nodes["Backend"]["tech"] == "Spring Boot"
    assert (g.nodes["Vet"]["kind"], g.nodes["Vet"]["inferredBy"]) == ("person", "declaration")
    assert (g.nodes["Warehouse"]["kind"], g.nodes["Warehouse"]["inferredBy"]) == ("db", "declaration")
    assert (g.nodes["DB"]["kind"], g.nodes["DB"]["inferredBy"]) == ("db", "name")


def test_technology_is_never_guessed():
    """A box reading `PostgreSQL` because the lifeline was called DB is a fact the page
    made up. The shape may be guessed from the name; the stack may not."""
    g = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    assert g.nodes["DB"]["kind"] == "db"
    assert g.nodes["DB"]["tech"] == ""


def test_a_queue_and_a_microservice_need_no_teaching():
    """The extensibility claim, executed: nothing in the projector knows the word REST, so
    an asynchronous hop is two edges through a box that draws itself as a queue."""
    g = graph("""
        @startuml
        participant Orders
        participant Kafka
        participant Shipping
        Browser -> Orders: POST /api/orders
        Orders ->> Kafka: publish OrderPlaced
        Kafka ->> Shipping: OrderPlaced
        Shipping -> ShippingDB: insert shipment
        @enduml
    """)
    assert edges(g) == {("Browser", "Orders"), ("Orders", "Kafka"),
                        ("Kafka", "Shipping"), ("Shipping", "ShippingDB")}
    assert g.nodes["Kafka"]["kind"] == "queue"
    assert c2.KIND_MACRO[g.nodes["Kafka"]["kind"]] == "ContainerQueue"
    assert g.nodes["ShippingDB"]["kind"] == "db"
    assert g.nodes["Shipping"]["kind"] == "container"


def test_the_family_table_matches_words_and_never_substrings():
    """`Feedback` contains the letters `db`. A substring match turns a perfectly ordinary
    microservice into a database, which is the kind of wrong nobody re-reads a diagram to
    catch."""
    g = graph("""
        @startuml
        Backend -> Feedback: POST /feedback
        Backend -> ShippingDB: insert shipment
        Backend -> order-service: GET /orders
        @enduml
    """)
    assert g.nodes["Feedback"]["kind"] == "container"
    assert g.nodes["ShippingDB"]["kind"] == "db"
    assert g.nodes["order-service"]["kind"] == "container"


def test_two_lifelines_can_be_folded_into_one_container():
    """A suite names its own driver: Playwright's is `Browser`, the API test's is `Client`.
    Without this, renaming a test harness reads as a container added and one removed."""
    g = graph("""
        @startuml
        Client -> Backend: GET /api/owners
        Browser -> Backend: GET /api/pets
        @enduml
    """, containers={"Client": {"as": "Browser"}})
    assert set(g.nodes) == {"Browser", "Backend"}
    assert len(g.edges[("Browser", "Backend")]["ops"]) == 2


def test_a_lifeline_nothing_calls_is_where_the_flows_start():
    g = graph("@startuml\nBrowser -> Backend: GET /api/owners\n@enduml")
    assert g.nodes["Browser"]["descr"] == "entry point"
    assert g.nodes["Backend"]["descr"] == "1 operation"


# --------------------------------------------------------------------------- the delta


def test_the_delta_stamps_what_this_branch_did_to_the_shape():
    old = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        Backend -> DB: select owners
        @enduml
    """)
    new = graph("""
        @startuml
        Browser -> Backend: GET /api/owners
        Backend -> DB: select owners
        Backend -> Payments: POST /charge
        @enduml
    """)
    d = c2.diff(old, new)
    assert d["nodes"]["Payments"]["status"] == "added"
    assert d["nodes"]["Backend"]["status"] == "same"
    by = {(e["from"], e["to"]): e for e in d["edges"]}
    assert by[("Backend", "Payments")]["status"] == "added"
    assert by[("Browser", "Backend")]["status"] == "same"


def test_a_removed_container_survives_into_the_delta_to_be_drawn_red():
    old = graph("@startuml\nBackend -> Legacy: GET /soap\n@enduml")
    new = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    d = c2.diff(old, new)
    assert d["nodes"]["Legacy"]["status"] == "removed"
    assert [e["status"] for e in d["edges"] if e["to"] == "Legacy"] == ["removed"]


def test_a_chattier_call_is_a_count_not_a_colour():
    """Green would say the branch introduced an integration it did not. The number moved;
    the architecture did not, and the line carries the delta instead of the paint."""
    old = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    new = graph("""
        @startuml
        Backend -> DB: select owners
        Backend -> DB: select pets
        Backend -> DB: select visits
        @enduml
    """)
    e = next(e for e in c2.diff(old, new)["edges"] if e["to"] == "DB")
    assert e["status"] == "same"
    assert e["operationsDelta"] == 2


# --------------------------------------------------------------------------- the popup


def test_an_operation_is_its_name_and_its_route_kept_apart():
    """The generator writes an HTTP call on two lines — what it is called, then where it
    goes. Flattened into one string there is no bullet list to draw, only a sentence."""
    g = graph("@startuml\nBrowser -> Backend: Get an owner by ID\\nGET /api/owners/{id}\n@enduml")
    assert c2.operations(g.edges[("Browser", "Backend")]) == [
        {"name": "Get an owner by ID", "path": "GET /api/owners/{id}", "calls": 1}]


def test_a_call_with_no_prose_above_it_is_all_route():
    g = graph("@startuml\nBrowser -> Backend: GET /api/owners\n@enduml")
    assert c2.operations(g.edges[("Browser", "Backend")]) == [
        {"name": "", "path": "GET /api/owners", "calls": 1}]


def test_a_statement_has_no_route_and_says_so_rather_than_inventing_one():
    g = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    assert c2.operations(g.edges[("Backend", "DB")]) == [
        {"name": "select owners", "path": "", "calls": 1}]


def test_the_same_endpoint_hit_twice_is_one_bullet_with_a_count():
    """`GET /api/pets/1` and `GET /api/pets/2` are one operation: the generator writes the
    template, and a C2 that listed every instance would be a log, not an inventory."""
    g = graph("""
        @startuml
        Browser -> Backend: getPet\\nGET /api/pets/{petId}
        Browser -> Backend: getPet\\nGET /api/pets/{petId}
        @enduml
    """)
    d = c2.one_side(g)["edges"][0]
    assert d["operations"] == 1 and d["calls"] == 2
    assert "×2" in c2.inventory(d)


def test_the_label_becomes_a_handle_onto_its_own_inventory():
    g = graph("""
        @startuml
        Browser -> Backend: List owners\\nGET /api/owners
        Browser -> Backend: getPet\\nGET /api/pets/{petId}
        @enduml
    """)
    details = {}
    out = c2.render(**c2.one_side(g), title="", system="", caption="", coloured=False,
                    details=details)
    key, = details
    assert f"[[genseq://{key}" in out and "HTTP ⊕]]" in out
    entry = details[key]
    assert entry["title"] == "Browser → Backend"
    assert entry["steps"][0]["label"] == "2 operations, 2 calls"
    assert entry["steps"][0]["text"] == (
        "• List owners\n    GET /api/owners\n• getPet\n    GET /api/pets/{petId}")


def test_the_handles_are_themed_the_way_the_page_themes_a_diagram():
    """PlantUML's default link colour is pure #0000FF, which the page's palette does not
    name — so it would stay a hard blue on a near-black canvas while every other colour on
    the diagram followed the theme. #1A4FA0 is `--dgm-link`, and the sequence generator
    already writes it, which is also why the handles look identical across the two tabs."""
    g = graph("@startuml\nBrowser -> Backend: GET /api/owners\n@enduml")
    out = c2.render(**c2.one_side(g), title="", system="", caption="", coloured=False,
                    details={})
    assert f"skinparam hyperlinkColor {c2.LINK_COLOR}" in out
    assert "skinparam hyperlinkUnderline false" in out

    _spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
    build = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(build)
    assert build.DIAGRAM_COLOR_VARS[c2.LINK_COLOR] == "--dgm-link"


def test_no_index_no_handle():
    """The `.puml` is committed and read on its own, outside the page. A link into a popup
    that only exists inside review.html would render there as a dead affordance."""
    g = graph("@startuml\nBrowser -> Backend: GET /api/owners\n@enduml")
    out = c2.render(**c2.one_side(g), title="", system="", caption="", coloured=False)
    assert "genseq://" not in out and 'Rel(Browser, Backend, "HTTP"' in out


def test_an_untouched_line_opens_one_entry_from_either_pane():
    """The ids are derived from content precisely so the three renders in one card do not
    each need their own copy: same calls, same id, one entry after the page merges them."""
    old = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    new = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    a, b = {}, {}
    c2.render(**c2.one_side(old), title="", system="", caption="", coloured=False, details=a)
    c2.render(**c2.one_side(new), title="", system="", caption="", coloured=False, details=b)
    assert set(a) == set(b)


def test_a_line_whose_calls_changed_keeps_both_inventories_apart():
    """...and the converse: the Old pane has to open what was there, not what is there."""
    old = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    new = graph("@startuml\nBackend -> DB: select owners\nBackend -> DB: select pets\n@enduml")
    a, b = {}, {}
    c2.render(**c2.one_side(old), title="", system="", caption="", coloured=False, details=a)
    c2.render(**c2.one_side(new), title="", system="", caption="", coloured=False, details=b)
    assert set(a).isdisjoint(set(b))


# --------------------------------------------------------------------------- rendering


def test_the_delta_is_the_only_side_that_is_painted():
    """An uncoloured render is of ONE side: it contains no removed element, so tags on it
    would be a legend with a single entry, and green on a picture with nothing to compare
    against reads as "all of this is new"."""
    old = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    new = graph("@startuml\nBackend -> DB: select owners\nBackend -> Payments: POST /charge\n@enduml")
    d = c2.diff(old, new)
    painted = c2.render(d["nodes"], d["edges"], title="t", system="", caption="",
                        coloured=True)
    plain = c2.render(d["nodes"], d["edges"], title="t", system="", caption="",
                      coloured=False)
    assert '$tags="added"' in painted and c2.ADDED in painted
    assert "$tags=" not in plain and c2.ADDED not in plain


def test_a_delta_where_nothing_moved_carries_no_key_to_read():
    """A legend listing two colours over a picture that uses neither costs the reader a
    moment working out which box is the green one, and the answer is "none of them". A
    branch that changed no integration should look exactly like the system does."""
    g = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    d = c2.diff(g, g)
    out = c2.render(d["nodes"], d["edges"], title="t", system="", caption="", coloured=True)
    assert "AddElementTag" not in out and "SHOW_LEGEND" not in out


def test_only_the_half_of_the_key_a_delta_uses_is_declared():
    """A branch that only added something gets one entry, not two with one unused."""
    old = graph("@startuml\nBackend -> DB: select owners\n@enduml")
    new = graph("@startuml\nBackend -> DB: select owners\nBackend -> Pay: POST /charge\n@enduml")
    d = c2.diff(old, new)
    out = c2.render(d["nodes"], d["edges"], title="t", system="", caption="", coloured=True)
    assert 'AddElementTag("added"' in out
    assert 'AddElementTag("removed"' not in out


def test_the_render_is_c4_container_dialect():
    g = graph("""
        @startuml
        actor Vet
        Vet -> Backend: GET /api/vets
        Backend -> DB: select vets
        @enduml
    """)
    out = c2.render(**c2.one_side(g), title="Containers", system="PetClinic",
                    caption="from 1 sequence diagram", coloured=False)
    # The regression that shipped: a plain side printing `['select vets'] operations`.
    # Asserted on every render, not only the delta, because the delta was the pane that
    # was right.
    assert "[" not in out.split("System_Boundary")[1]
    assert "!include <C4/C4_Container>" in out
    assert 'Person(Vet, "Vet"' in out
    assert 'ContainerDb(DB, "DB"' in out
    assert 'System_Boundary(c2_system, "PetClinic")' in out
    assert 'Rel(Vet, Backend, "HTTP"' in out
    # The boundary holds the containers and not the person: a Person outside the system is
    # the whole convention of a C4 view, and one inside the box says the system contains
    # its own users.
    assert out.index("System_Boundary") > out.index("Person(Vet")


def test_a_lifeline_named_anything_at_all_still_yields_a_plantuml_identifier():
    g = graph('@startuml\n"Pet Clinic UI" -> "Order/Billing API": GET /x\n@enduml')
    out = c2.render(**c2.one_side(g), title="", system="", caption="", coloured=False)
    assert "Pet_Clinic_UI" in out and "Order_Billing_API" in out


# --------------------------------------------------------------------------- end to end


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "test").mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def git(*a):
        subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, env=env)
    git("init", "-q", "-b", "main")
    (root / "test" / "a.spec.ts.flow.genseq.puml").write_text(
        "@startuml\nBrowser -> Backend: GET /api/owners\nBackend -> DB: select owners\n@enduml\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return root


def test_end_to_end_writes_a_manifest_the_page_can_read(tmp_path):
    root = _repo(tmp_path)
    # The branch adds a call to a service that did not exist at the base.
    (root / "test" / "b.spec.ts.pay.genseq.puml").write_text(
        "@startuml\nBackend -> Payments: POST /charge\n@enduml\n")
    assert c2.main(["--root", str(root), "--base", "main"]) == 0

    out = root / ".human-review" / "assets" / "c2"
    header, row = (out / "MANIFEST.tsv").read_text().splitlines()
    fields = dict(zip(header.split("\t"), row.split("\t")))
    assert fields["name"] == "C2"
    assert fields["kind"] == "structural"
    assert fields["status"] == "modified"
    assert fields["diff_puml"] == "C2.diff.puml"
    # The source column is a real file, so the page's header links into the editor rather
    # than printing a path nothing opens.
    assert (root / fields["source"]).is_file()

    model = json.loads((out / "C2.json").read_text())
    assert {"Browser", "Backend", "DB", "Payments"} == set(model["new"]["nodes"])
    assert model["diff"]["nodes"]["Payments"]["status"] == "added"


def test_end_to_end_says_nothing_to_draw_rather_than_drawing_nothing(tmp_path):
    root = tmp_path / "empty"
    (root).mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True,
                   capture_output=True)
    assert c2.main(["--root", str(root), "--base", "main"]) == 3
    assert not (root / ".human-review").exists()


def test_a_branch_whose_base_had_no_sequences_is_added_not_modified(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True,
                   capture_output=True)
    (root / "README").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True,
                   capture_output=True, env=env)
    (root / "a.genseq.puml").write_text("@startuml\nBrowser -> Backend: GET /x\n@enduml\n")
    assert c2.main(["--root", str(root), "--base", "main"]) == 0
    out = root / ".human-review" / "assets" / "c2"
    row = (out / "MANIFEST.tsv").read_text().splitlines()[1].split("\t")
    assert row[3] == "added"
    assert row[8] == ""          # no old side to offer, so the control shows one word
    # Nothing to compare against means nothing to paint: a diagram where every box is
    # green says "all of this changed" when what happened is "this is the first picture".
    assert c2.ADDED not in (out / "C2.diff.puml").read_text()


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
