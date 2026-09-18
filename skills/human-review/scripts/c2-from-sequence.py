#!/usr/bin/env python3
"""The C4 container view (C2), computed from the sequence diagrams the tests recorded.

Every other picture of "the architecture" in a repository is a drawing somebody made and
then stopped updating. This one is not drawn at all: it is **projected** from the
`*.genseq.puml` sequence diagrams, which are themselves generated from real traces of real
test runs. If the branch adds a call to a new microservice, a box and a line appear here
because the call *happened*, not because anyone remembered to redraw the diagram — and if a
call disappears, the line goes with it.

The projection is three rules and nothing else:

  * **a lifeline is a container.** `participant Backend` in a sequence is a box in C2.
  * **a message between two different lifelines is a call**, drawn once however many times
    it was sent, with the number of distinct operations and the raw call count on the line.
  * **nothing else survives.** A reply (`-->`, the dashed answer to a call already drawn)
    would double every edge and reverse half of them. A self-call (`Backend -> Backend`) is
    a container talking to itself, which is C3's business, not C2's. Both are dropped.

That is why this generalises past HTTP without being taught to. A queue is a lifeline; a
microservice is a lifeline; `Orders -> Kafka: publish OrderPlaced` and `Kafka -> Shipping:
OrderPlaced` are two edges through a box, which is exactly the C2 picture of asynchronous
messaging. Nothing here knows the word "REST".

**What is inferred, and how far.** The one thing a sequence diagram does not carry is what
*kind* of thing a lifeline is, so the shape of each box is decided in this order, and the
sidecar JSON records which rule fired for every node:

  1. `steps.c2.containers` in `human-review.json` — what a human declared, always wins;
  2. the PlantUML declaration keyword — `actor` is a person, `database` a datastore,
     `queue`/`collections` a queue. Free evidence, and the generator may start emitting it;
  3. the name, against the table in `NAME_KINDS` below — the only guess in the file;
  4. failing all three, a plain container.

Technology (`Angular`, `PostgreSQL`) is **never** guessed: it comes from the config or the
box goes without. A protocol label on an edge is not a guess either — it is read off the
messages themselves (`GET /api/owners` is HTTP, `select owners` is SQL, `->>` is async),
and an edge whose messages say nothing recognisable is labelled `calls`.

**The delta.** `puml-diff.sh` cannot help here: its structural differ refuses the
C4-PlantUML dialect outright, and the two sides of this comparison are not two files but
two whole *sets* of sequence diagrams — the work tree's, and the merge-base's, read out of
git. So the diff is taken on the graph, before any PlantUML exists, and rendered with the
page's own two colours: green for a container or a call this branch introduced, red for one
it removed. An edge that survived with a different set of operations behind it keeps its
neutral colour and says so in its technology line — the count moved, the architecture did
not.

Output (default `.human-review/assets/c2/`, which is NOT `assets/diagrams/`: `puml-diff.sh`
does `rm -rf` on that directory every time it runs):

    C2.new.puml / .svg    the container view as the work tree's traces draw it
    C2.old.puml / .svg    the same, from the merge-base's traces (absent if it had none)
    C2.diff.puml / .svg   the two, merged and coloured
    C2.json               the graph itself: nodes, edges, evidence, and what was inferred
    MANIFEST.tsv          one row, in the columns build-review-html.py's read_manifest wants

Usage:
    c2-from-sequence.py                                  # base from human-review.json
    c2-from-sequence.py --base origin/main --out-dir .human-review/assets/c2
    c2-from-sequence.py --print                          # the graph, to stdout, nothing written

Exit codes: 0 = drawn, 3 = no sequence diagrams to project from (a normal, quiet skip),
2 = bad usage.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# The page's diff palette, and the reason it is duplicated rather than imported: this file
# emits C4-PlantUML, which `puml_diff.py` explicitly refuses, so there is no import of it
# here to hang the constants off. They must stay equal to `puml-diff/puml_diff.py`'s
# ADDED/REMOVED — one colour meaning two things on one page is the failure mode.
ADDED = "#2E7D32"
REMOVED = "#C62828"
ADDED_DARK = "#1B5E20"
REMOVED_DARK = "#8E0000"
#: What the page themes a diagram's hyperlinks with (`--dgm-link`), and what the sequence
#: generator already writes. Anything else is a colour dark mode does not know to move.
LINK_COLOR = "#1A4FA0"
#: Simon Brown's own description of the four levels. The page links out to it rather than
#: explaining C4 in a caption, because the caption has one line and the site has the answer.
C4_URL = "https://c4model.com/diagrams/container"

#: What this picture is called, on the page and in the filenames — and it is a *view key*,
#: not a level. `C2` on its own names the level in the C4 model, the way `C3` does; it is
#: not the name of a diagram any more than "page 2" is the name of a chapter, and a card
#: titled with it tells a reader which shelf it came off rather than what is on it.
#:
#: `C2-Containers` is Structurizr's own convention for the key of a container view, and a
#: project whose C4 lives in a DSL already has one — petclinic's reads
#: `container petClinic "C2-Containers" …` and exports `C2-Containers.puml`. Matching it is
#: the whole point: one name for one picture across the model, the export and this page.
#: `steps.c2.name` overrides it for a project that spells its own view key differently.
DEFAULT_NAME = "C2-Containers"

# Where the sequence diagrams are, and where they are not. The defaults name the generator's
# own convention (`<test file>.genseq.puml`, filed beside the test) and then rule out every
# place a build copies one to: a diagram under `target/` is last week's run, and projecting
# it would draw a container the branch no longer talks to.
DEFAULT_SOURCES = ["**/*.genseq.puml"]
DEFAULT_EXCLUDE = [
    "**/target/**", "**/build/**", "**/out/**", "**/bin/**",
    "**/node_modules/**", "**/dist/**", "**/.human-review/**", "**/.git/**",
]

#: Rule 3, and the only guess in this file: a lifeline's name against a family of names.
#: Ordered — the first pattern that matches wins — so `queue` is tested before the generic
#: service words. Matched against `_words()` below rather than the raw name, so the test is
#: on whole words and never on a substring: `ShippingDB` is a datastore, `Feedback` — which
#: contains the letters `db` — is not. Everything here is overridable per project in
#: `steps.c2.containers`, and whatever it decides is written into C2.json under
#: `inferredBy`, so a wrong guess is visible in the artifact rather than only in the picture.
NAME_KINDS: list[tuple[str, str]] = [
    (r"\b(?:db|dbs|database|datastore|postgres|postgresql|mysql|mariadb|oracle|"
     r"mssql|sqlserver|sqlite|h2|mongo|mongodb|redis|cassandra|dynamo|dynamodb|"
     r"elastic|elasticsearch|opensearch)\b", "db"),
    (r"\b(?:queue|kafka|rabbit|rabbitmq|topic|broker|bus|sqs|sns|jms|amqp|"
     r"pubsub|eventhub|kinesis|outbox|stream)\b", "queue"),
    (r"\b(?:user|actor|customer|operator|visitor|person)\b", "person"),
]


def _words(name: str) -> str:
    """A lifeline name as space-separated lowercase words, camelCase included.

    `ShippingDB` -> `shipping db`, `PetClinicUI` -> `pet clinic ui`, `order-service` ->
    `order service`. Without this the family table has to choose between missing the
    camelCase half of every naming convention in use and matching substrings, and matching
    substrings is how `Feedback` becomes a database."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    return re.sub(r"[^a-z0-9]+", " ", spaced.lower()).strip()

#: Which C4-PlantUML macro draws each kind. `person` is the one that is not a container at
#: all; the rest are boxes that differ only in shape.
KIND_MACRO = {
    "person": "Person",
    "db": "ContainerDb",
    "queue": "ContainerQueue",
    "external": "System_Ext",
    "container": "Container",
}

#: Rule 2: what a PlantUML lifeline declaration already says about its own kind. The
#: generator writes `participant` for everything today, which is why rules 3 and 1 exist —
#: but the day it learns to write `queue`, this picks it up for free.
DECL_KINDS = {
    "actor": "person",
    "database": "db",
    "queue": "queue",
    "collections": "queue",
    "participant": "",     # says nothing
    "boundary": "",
    "control": "",
    "entity": "",
}

# A lifeline declaration, in both orders PlantUML accepts:
#   participant Backend
#   participant "Pet Clinic UI" as UI
#   participant UI as "Pet Clinic UI"
DECL = re.compile(
    r'^(?P<kw>participant|actor|database|queue|collections|boundary|control|entity)\s+'
    r'(?P<first>"[^"]*"|[^\s"]+)'
    r'(?:\s+as\s+(?P<second>"[^"]*"|[^\s"]+))?\s*(?:#\S+)?\s*$'
)

# A message. Two shapes, because both are legal and generated PlantUML uses the first:
# `A -> B: label` and the space-less `A->B: label`. The arrow itself is validated by
# `_is_message_arrow` rather than by the regex, so a colour (`-[#red]>`) or a half arrow
# (`-\`) is matched here and judged there.
MSG_SPACED = re.compile(r'^(?P<a>"[^"]*"|\S+)\s+(?P<arrow>\S*-\S*)\s+(?P<b>"[^"]*"|\S+)$')
MSG_TIGHT = re.compile(
    r'^(?P<a>"[^"]*"|[A-Za-z_][\w.$]*)\s*'
    r'(?P<arrow>[-<>ox\\/]*(?:\[[^\]]*\])?-{1,2}(?:\[[^\]]*\])?[-<>ox\\/]*)\s*'
    r'(?P<b>"[^"]*"|[A-Za-z_][\w.$]*)$'
)

# `[[src://path/File.java:14{tooltip} OwnerRepository.findById ↗]]` — a PlantUML hyperlink.
# The label a reader sees is the text after the tooltip, and that is the only part that
# describes the call; the URL is a handle into the reviewed checkout and the tooltip is
# instructions for clicking it. Both would poison the protocol sniffing below.
LINK = re.compile(r'\[\[[^\]\s{]*(?:\{[^}]*\})?\s*([^\]]*?)\s*\]\]')

#: An HTTP request line: a verb, a space, and a path. It does double duty — `split_operation`
#: uses it to tell the route apart from the prose above it, and `sniff_protocol` treats
#: having found one as the answer. That is the whole reason the two are the same regex: a
#: message the popup lists a route for and the diagram labels `calls` would be two parts of
#: the page disagreeing about the same line.
ROUTE = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S")

# What a message announces when it carries no request line. Ordered, and matched against the
# NAME rather than the whole label, so `publish OrderPlaced` does not read as SQL and a URL
# is recognised without relying on a word boundary before a slash — `\b/api/` never matches
# after a space, since neither character is a word character, which is precisely how
# `List owners GET /api/owners` came out labelled `calls`.
PROTOCOLS: list[tuple[str, str]] = [
    (r"(?:^|\s)https?://|(?:^|\s)/[\w{]", "HTTP"),
    (r"^(?:select|insert|update|delete|merge|upsert|call|begin|commit|rollback)\b", "SQL"),
    (r"\b(?:publish|publishes|produce|produces|emit|emits|send to|enqueue|dequeue|"
     r"consume|consumes|subscribe|subscribes|ack|nack)\b", "message"),
    (r"\bgrpc\b", "gRPC"),
    (r"\bgraphql\b", "GraphQL"),
]


# --------------------------------------------------------------------------- the model


class Graph:
    """One side of the comparison: the containers and the calls one set of traces drew.

    Deliberately not a dataclass tree. Everything downstream — the diff, the PlantUML, the
    JSON sidecar and the tests — wants plain dicts keyed by name, and a layer of objects
    between them would earn nothing but a `to_dict`."""

    def __init__(self) -> None:
        #: name -> {"kind", "tech", "descr", "inferredBy", "decl"}
        self.nodes: dict[str, dict] = {}
        #: (src, dst) -> {"ops": Counter((name, path) -> times), "protocols": Counter,
        #:                "async": bool}
        self.edges: dict[tuple[str, str], dict] = {}
        #: which diagrams this was projected from, for the caption
        self.sources: list[str] = []

    def node(self, name: str) -> dict:
        return self.nodes.setdefault(
            name, {"kind": "", "tech": "", "descr": "", "inferredBy": "", "decl": ""})

    def edge(self, src: str, dst: str) -> dict:
        return self.edges.setdefault(
            (src, dst), {"ops": Counter(), "protocols": Counter(), "async": False})

    def as_dict(self) -> dict:
        """The graph as the sidecar records it — edges in the SAME shape `render` reads.

        Not a second shape that happens to look similar. The first version had one: its
        `operations` was the list of calls while the delta's was their count, `render`
        wanted the count, and the plain New and Old sides therefore went out with a Python
        list repr spread across the middle of the picture while the Diff pane — the one
        being looked at — was perfectly fine. There is now exactly one edge dict in this
        file, built in one place, and `diff(g, g)` is how a single side gets it."""
        return {
            "sources": self.sources,
            "nodes": {n: dict(v) for n, v in sorted(self.nodes.items())},
            "edges": one_side(self)["edges"],
        }


def operations(e: dict) -> list[dict]:
    """The calls behind one line of the diagram, as the popup lists them.

    Sorted by name rather than by frequency: the panel is read as an inventory of what one
    container asks another for, and a list that reorders itself between two runs because
    one endpoint was hit twice more cannot be compared against the run before it."""
    return [{"name": name, "path": path, "calls": n}
            for (name, path), n in sorted(e["ops"].items())]


def clean_label(text: str) -> str:
    """The words a message actually says, with PlantUML's decoration taken off.

    Links collapse to their visible text and the generator's `⊕`/`↗` affordance glyphs go —
    they mean "there is a payload behind this", not anything about the call. The line break
    STAYS: the generator writes an HTTP call as `Get an owner by ID\\nGET /api/owners/{id}`,
    two facts on two lines, and flattening them into one string is what made the popup's
    inventory unreadable — a bullet list has to be able to put the name above the route."""
    text = LINK.sub(r"\1", text)
    text = text.replace("\\n", "\n")
    text = re.sub(r"[⊕↗]", "", text)
    text = re.sub(r"<[^>]+>", "", text)          # <b>, <color:red>, PlantUML creole
    return "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()


def split_operation(label: str) -> tuple[str, str]:
    """One message label as (what it is called, where it goes).

    The route is whichever line starts with an HTTP verb — the generator puts it second,
    but nothing guarantees that, and a `GET /api/owners` with no prose above it is a whole
    label. Everything that is not the route is the name, which for a database call is the
    statement's own summary (`select owners`) and for a queue is the event."""
    lines = [l for l in label.split("\n") if l]
    for i, line in enumerate(lines):
        if ROUTE.match(line):
            return " ".join(lines[:i] + lines[i + 1:]), line
    return " ".join(lines), ""


def sniff_protocol(name: str, path: str = "") -> str:
    """What one message announces, decided on the parts rather than on the sentence.

    A route found is the answer: `split_operation` only calls something a route when it is
    a verb followed by a path, and there is nothing else that can be. Everything else is
    read off the name, where the words are."""
    if path:
        return "HTTP"
    for pattern, protocol in PROTOCOLS:
        if re.search(pattern, name, re.I):
            return protocol
    return ""


def edge_protocol(e: dict) -> str:
    """What to write on the line: what the messages said, or an honest shrug.

    A mixed edge names both (`HTTP + message`) rather than picking the winner — a container
    that is reached two different ways is a fact about the architecture, and hiding the
    quieter half behind a majority vote is how a C2 stops being true."""
    named = [p for p, _ in e["protocols"].most_common() if p]
    if not named:
        return "async" if e["async"] else "calls"
    return " + ".join(named[:2])


def _is_message_arrow(arrow: str) -> bool:
    """A message arrow, not a line of a class diagram.

    `-->` and `<-` are messages; `--` is a separator and `..` is a note anchor. The test is
    "has a dash and has a head", which is what separates the two."""
    if "-" not in arrow:
        return False
    return any(c in arrow for c in "<>ox\\/")


def _unquote(s: str) -> str:
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' else s


def parse_sequence(text: str, graph: Graph, source: str = "",
                   rename: dict[str, str] | None = None,
                   drop: set[str] | None = None) -> None:
    """Fold one sequence diagram into the graph.

    Skips what is not the script: comments, and the free text inside `note`/`legend`
    blocks, where a line reading `A -> B: something` is prose about a call rather than a
    call. Getting that wrong invents an edge, and an invented edge on an architecture
    diagram is the most expensive kind of wrong this page can be.

    `rename` is `steps.c2.containers[…].as`, applied here rather than to a finished graph
    so that two lifelines folding into one container also fold their edges. It exists
    because a suite names its own driver: the Playwright tests call the entry point
    `Browser`, the API tests call it `Client`, and untouched that draws two boxes for one
    container — plus, the moment one suite is renamed, a box "added" and a box "removed"
    on a C2 that in truth did not move."""
    if source:
        graph.sources.append(source)
    rename = rename or {}
    drop = drop or set()
    aliases: dict[str, str] = {}
    skipping = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("'"):
            continue
        low = line.lower()
        if skipping:
            if low.startswith(("end note", "end legend", "endlegend", "end header",
                               "end footer", "end title")):
                skipping = False
            continue
        if _opens_free_text(low):
            skipping = True
            continue
        if re.match(r"^(note|legend|header|footer|title|caption)\b", low):
            continue                          # the one-line form: `footer drawn by …`

        m = DECL.match(line)
        if m:
            first, second = _unquote(m["first"]), _unquote(m["second"] or "")
            # `participant "Pet Clinic UI" as UI` and `participant UI as "Pet Clinic UI"`
            # both exist. The quoted side is the label whichever order it came in; with
            # neither quoted, PlantUML's own rule applies — the alias is the second.
            if second:
                if (m["first"] or "").startswith('"'):
                    label, alias = first, second
                else:
                    alias, label = first, second
            else:
                label = alias = first
            aliases[alias] = label
            if rename.get(label, label) in drop:
                continue
            node = graph.node(rename.get(label, label))
            node["decl"] = m["kw"]
            continue

        head, sep, label = line.partition(":")
        if not sep:
            continue
        head = head.strip()
        mm = MSG_SPACED.match(head) or MSG_TIGHT.match(head)
        if not mm or not _is_message_arrow(mm["arrow"]):
            continue
        a, b, arrow = _unquote(mm["a"]), _unquote(mm["b"]), mm["arrow"]
        a, b = aliases.get(a, a), aliases.get(b, b)
        a, b = rename.get(a, a), rename.get(b, b)
        # A lifeline the project declared `"drop": true` never enters the graph, and
        # neither do its calls. A C2 is a picture of what is DEPLOYED, and a MockMvc test
        # is not deployed anywhere: it is a synthetic client standing where a real one
        # would, calling controllers in-process with no browser, no socket and no server.
        # It draws a beautiful sequence — that is the Sequence tab's business — and a box
        # on the container diagram that no operator could point at.
        if a in drop or b in drop:
            continue
        # `B <- A` is the same call as `A -> B`; normalise before the edge is keyed, or the
        # same relationship lands in the graph twice pointing opposite ways.
        if "<" in arrow and ">" not in arrow:
            a, b = b, a
        # The dashed reply is the answer to a call already in the graph. Drawing it would
        # give every synchronous edge a twin pointing the wrong way.
        if _looks_like_reply(arrow):
            continue
        if a == b:
            continue                          # a container calling itself is C3's business
        graph.node(a), graph.node(b)
        e = graph.edge(a, b)
        text_label = clean_label(label)
        name, path = split_operation(text_label)
        # "(unlabelled)" only when there is nothing at all. A bare `GET /api/owners` is
        # not unlabelled: the route IS the label, and the popup prints it as one.
        e["ops"][(name or ("" if path else "(unlabelled)"), path)] += 1
        e["protocols"][sniff_protocol(name, path)] += 1
        if ">>" in arrow:
            e["async"] = True


#: The block form of PlantUML's free-text elements — everything up to the matching `end`
#: is prose, and a line of prose reading `A -> B: something` is a sentence about a call
#: rather than a call. Telling the block form from the one-line form matters more than it
#: looks: `footer generated from real traces — do not edit` opens no block, and reading it
#: as one swallowed every arrow in the file and drew an empty architecture.
FREE_TEXT_BLOCK = re.compile(
    r"^(?:legend|header|footer)(?:\s+(?:left|right|top|bottom|center|centre))?$"
    r"|^note\b(?![^:]*:)")


def _opens_free_text(low: str) -> bool:
    return bool(FREE_TEXT_BLOCK.match(low))


def _looks_like_reply(arrow: str) -> bool:
    """A dashed arrow, which in a sequence diagram means "returning".

    Stripping any `[#colour]` first: `-[#green]->` is a coloured *solid* arrow, and the
    brackets contain dashes of their own that would otherwise read as dashes of the line."""
    bare = re.sub(r"\[[^\]]*\]", "", arrow)
    return "--" in bare


# --------------------------------------------------------------------------- inference


def classify(graph: Graph, containers: dict) -> None:
    """Decide what each box is, in the four-rule order the module docstring states.

    Writes `inferredBy` next to every decision. That field is the whole reason this is a
    separate pass over a finished graph rather than a line inside the parser: a reader who
    disagrees with the picture needs to see *which* rule drew it before they know whether
    to edit `human-review.json` or the generator."""
    for name, node in graph.nodes.items():
        cfg = containers.get(name) or {}
        if cfg.get("kind"):
            node["kind"], node["inferredBy"] = cfg["kind"], "config"
        elif DECL_KINDS.get(node["decl"]):
            node["kind"], node["inferredBy"] = DECL_KINDS[node["decl"]], "declaration"
        else:
            words = _words(name)
            for pattern, kind in NAME_KINDS:
                if re.search(pattern, words):
                    node["kind"], node["inferredBy"] = kind, "name"
                    break
            else:
                node["kind"], node["inferredBy"] = "container", "default"
        # Technology is never guessed. A box with no `techn` reads as "we did not say";
        # a box reading "PostgreSQL" because the lifeline was called DB reads as a fact.
        node["tech"] = cfg.get("tech", "")
        # The description line used to be auto-filled from the traces — "N operations"
        # under a box the traces called into, "entry point" under one nothing called. It
        # duplicated what the arrows already say: an edge's own `[N ops]` label is the
        # same count, drawn where the reader is already looking, and "entry point" was
        # only ever "nothing points at this box", visible from the picture's own shape.
        # Left with a job under every box on a branch that has many, it was ink the box
        # spent on a fact the diagram had already drawn. A container still gets a
        # description when the config says one explicitly (`containers.<name>.descr`) —
        # that is authored, not guessed, and stays.
        node["descr"] = cfg.get("descr", "")


# --------------------------------------------------------------------------- the delta


def diff(old: Graph, new: Graph) -> dict:
    """The two graphs merged, every node and edge stamped added / removed / same.

    Set semantics, which is the right model for boxes and lines and the wrong one for a
    sequence (where order is the content — hence the separate `seq_puml_diff.py`). The
    operation *counts* behind a surviving edge are carried through as a signed delta rather
    than as a colour: a call that got chattier is news, but it is not a change of shape, and
    painting it green would say the branch introduced an integration it did not."""
    nodes: dict[str, dict] = {}
    for name in sorted(set(old.nodes) | set(new.nodes)):
        side = new.nodes.get(name) or old.nodes[name]
        nodes[name] = {**side,
                       "status": "same" if name in old.nodes and name in new.nodes
                       else "added" if name in new.nodes else "removed"}
    edges = []
    for key in sorted(set(old.edges) | set(new.edges)):
        o, n = old.edges.get(key), new.edges.get(key)
        side = n or o
        ops_new = len(n["ops"]) if n else 0
        ops_old = len(o["ops"]) if o else 0
        edges.append({
            "from": key[0], "to": key[1],
            "status": "same" if o and n else "added" if n else "removed",
            "protocol": edge_protocol(side),
            "operations": len(side["ops"]),
            "operationsDelta": ops_new - ops_old if (o and n) else 0,
            "calls": sum(side["ops"].values()),
            "detail": operations(side),
            # What the line points AT, because that decides whether any of the above is
            # worth printing — see `is_datastore_edge`.
            "toKind": nodes[key[1]]["kind"],
        })
    return {"nodes": nodes, "edges": edges}


def is_datastore_edge(e: dict) -> bool:
    """Whether this line goes into a datastore, where counting is noise.

    A C2 line between two *systems* is a contract: the endpoints one asks the other for are
    a finite, named list, and knowing it is most of what the picture is for. A line into a
    database is not that. One page of one screen fires a hundred statements, the list is
    unbounded and half-generated, and `237 calls` on the arrow says only that the ORM did
    its job — it is the Sequence tab's question, asked at the wrong altitude. So the line
    to a datastore says what it speaks and stops: no inventory, no handle, no counts."""
    return e.get("toKind") == "db"


def one_side(graph: Graph) -> dict:
    """The undiffed picture of a single graph, in the shape `render` reads.

    `diff(g, g)` and not a second shape built by hand: the two used to be separate, and the
    plain sides went out with a Python list repr where the operation count belonged —
    `['List owners GET /api/owners', …] operations, 13 calls` across the middle of the
    diagram. One producer of the edge shape is what makes that unrepresentable."""
    return diff(graph, graph)


# --------------------------------------------------------------------------- rendering


def _pid(name: str) -> str:
    """A PlantUML identifier for a lifeline name that may be anything at all."""
    ident = re.sub(r"\W", "_", name)
    return ident if ident and not ident[0].isdigit() else f"n_{ident}"


def _q(s: str) -> str:
    return (s or "").replace('"', "'")


def op_id(e: dict) -> str:
    """A stable handle for one line's inventory of calls, derived from its CONTENT.

    Content and not position, for the reason the sequence generator's own ids are: the same
    card holds three renders of this diagram — Diff, New and Old — and each carries its own
    copy of the handle. An id derived from the content means an edge nothing touched
    resolves to ONE entry across all three, while a line whose calls did change gets two,
    and the Old pane opens the inventory as it was rather than as it is now."""
    body = "\u0000".join(f'{o["name"]}\u0001{o["path"]}' for o in e.get("detail") or [])
    return "c2-" + hashlib.sha1(
        f'{e["from"]}\u0002{e["to"]}\u0002{body}'.encode()).hexdigest()[:10]


def inventory(e: dict) -> str:
    """The bullet list behind one line: every call it stands for, name above route.

    This is the whole reason the edge label is a handle at all. A C2 line says *Backend
    talks HTTP to Payments*, which is the right altitude for the picture and exactly one
    level too coarse for the reviewer asking "yes, but which endpoints?". The answer is
    already in the traces; before this it was either absent or — briefly, and much worse —
    spilled across the middle of the diagram as a Python list repr."""
    lines = []
    for o in e.get("detail") or []:
        row = f'• {o["name"] or "(unlabelled)"}'
        if o["path"]:
            row += f'\n    {o["path"]}'
        lines.append(row)
    return "\n".join(lines)


def handle(e: dict, details: dict | None) -> str:
    """The edge's label, as a PlantUML link when there is an inventory to open.

    The ⊕ and the `genseq://` scheme are not a coincidence and not a copy: they are the
    page's existing affordance for "there is more behind this", already wired by
    `GENSEQ_JS` and already styled. A reader who has learnt it one tab earlier, on the
    sequence diagrams, does not have to learn it again here."""
    if details is None or is_datastore_edge(e) or not (e.get("detail") or []):
        return e["protocol"]
    key = op_id(e)
    details[key] = {
        "title": f'{e["from"]} → {e["to"]}',
        "steps": [{
            "label": f'{e["operations"]} operation'
                     f'{"s" if e["operations"] != 1 else ""}',
            "text": inventory(e),
        }],
    }
    return (f'[[genseq://{key}{{Click for the calls behind this line}} '
            f'{e["protocol"]} ⊕]]')


def render(nodes: dict, edges: list, *, title: str, system: str, caption: str,
           coloured: bool, details: dict | None = None) -> str:
    """One C4 container view.

    `details` is the popup index this render fills as it goes — pass a dict and every edge
    label becomes a `⊕` handle onto its own inventory of calls; pass None and the labels are
    plain text. It is an out-parameter because the ids are content-derived and the same
    entry is legitimately written by two of the three renders; letting them collide in one
    dict is the merge.

    `coloured` is what separates the delta from the two plain sides. It is not "add colours
    to the same drawing": an uncoloured render is of ONE side and has no removed elements in
    it at all, so the legend, the tags and the dashed removed lines only exist in the delta
    and would be a legend for a single entry anywhere else.

    And a delta where nothing moved is uncoloured too, tags and legend included. A key
    listing two colours over a picture that uses neither is a reader spending a moment
    working out which box is the green one — the answer being "none of them" — and a
    branch that changed no integration should say so by looking exactly like the system."""
    used = ({v.get("status") for v in nodes.values()}
            | {e.get("status") for e in edges}) & {"added", "removed"}
    coloured = coloured and bool(used)
    out = ["@startuml",
           "' ⚠️  GENERATED — projected from the sequence diagrams by c2-from-sequence.py.",
           "!include <C4/C4_Container>",
           "HIDE_STEREOTYPE()",
           # The same two lines the sequence generator writes, for the same two reasons.
           # PlantUML's default link colour is pure #0000FF, which is not in the page's
           # `DIAGRAM_COLOR_VARS`, so it would stay a hard blue on a near-black canvas in
           # dark mode while every other colour on the diagram moved. #1A4FA0 *is* mapped
           # (`--dgm-link`). And the underline is the page's to draw, not PlantUML's: it
           # styles genseq handles itself, so a diagram that brings its own arrives with
           # two.
           "skinparam hyperlinkUnderline false",
           f"skinparam hyperlinkColor {LINK_COLOR}"]
    if coloured and "added" in used:
        out += [
            f'AddElementTag("added", $bgColor="{ADDED}", $fontColor="#FFFFFF", '
            f'$borderColor="{ADDED_DARK}", $legendText="added by this branch")',
            f'AddRelTag("added", $textColor="{ADDED}", $lineColor="{ADDED}", '
            f'$legendText="call this branch introduced")',
        ]
    if coloured and "removed" in used:
        out += [
            f'AddElementTag("removed", $bgColor="{REMOVED}", $fontColor="#FFFFFF", '
            f'$borderColor="{REMOVED_DARK}", $legendText="removed by this branch")',
            f'AddRelTag("removed", $textColor="{REMOVED}", $lineColor="{REMOVED}", '
            f'$lineStyle=DashedLine(), $legendText="call this branch removed")',
        ]
    if title:
        out.append(f"title {_q(title)}")

    people = [(n, v) for n, v in nodes.items() if v["kind"] == "person"]
    inside = [(n, v) for n, v in nodes.items() if v["kind"] != "person"]

    def _element(name, v) -> str:
        macro = KIND_MACRO.get(v["kind"], "Container")
        tag = (f', $tags="{v["status"]}"'
               if coloured and v.get("status") in ("added", "removed") else "")
        if macro == "Person":
            return f'{macro}({_pid(name)}, "{_q(name)}", "{_q(v["descr"])}"{tag})'
        return (f'{macro}({_pid(name)}, "{_q(name)}", "{_q(v["tech"])}", '
                f'"{_q(v["descr"])}"{tag})')

    for name, v in people:
        out.append(_element(name, v))
    if system and inside:
        out.append(f'System_Boundary(c2_system, "{_q(system)}") {{')
        out += [f"  {_element(n, v)}" for n, v in inside]
        out.append("}")
    else:
        out += [_element(n, v) for n, v in inside]

    for e in edges:
        tag = (f', $tags="{e["status"]}"'
               if coloured and e.get("status") in ("added", "removed") else "")
        # How many DISTINCT operations, and nothing about how often each ran. The call
        # count went out with the ×N in the popup: a route hit seven times instead of
        # three is a fact about which test happened to run, not about the architecture,
        # and it was the longest thing on the busiest label. `ops` rather than
        # `operations` for the same reason — the label has to fit between two boxes.
        #
        # A line whose count moved says what it moved FROM, not by how much. `(-1)` is
        # four characters shorter than `(was 5)` and needs a legend the picture has no
        # room for: the first question anyone asks of it is "minus one what, against
        # what?". `was 5` answers both without being told, and only ever appears on the
        # delta — a single side compares against itself and prints nothing.
        delta = e.get("operationsDelta") or 0
        techn = "" if is_datastore_edge(e) else \
            f'{e["operations"]} ops' + (f' (was {e["operations"] - delta})' if delta else '')
        out.append(f'Rel({_pid(e["from"])}, {_pid(e["to"])}, "{_q(handle(e, details))}", '
                   f'"{_q(techn)}"{tag})')

    if coloured:
        out.append("SHOW_LEGEND(true)")
    if caption:
        out.append(f"caption {_q(caption)}")
    out.append("@enduml")
    return "\n".join(out) + "\n"


def plantuml(puml: Path) -> str:
    """Render beside the source, and refuse the picture PlantUML draws when it gives up.

    PlantUML emits a perfectly valid SVG reading "Syntax Error?" rather than failing, and
    embedding that is worse than embedding nothing: it looks like a diagram, and it is the
    loudest thing on the page. Same rule, and the same reason, as in `puml-diff.sh`."""
    svg = puml.with_suffix(".svg")
    svg.unlink(missing_ok=True)
    try:
        subprocess.run(["plantuml", "-tsvg", str(puml)],
                       capture_output=True, check=False, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if not svg.is_file():
        return ""
    body = svg.read_text(encoding="utf-8", errors="replace")
    if "Syntax Error" in body:
        print(f"[c2] WARNING: PlantUML could not render {puml.name} — dropping it",
              file=sys.stderr)
        svg.unlink(missing_ok=True)
        return ""
    svg.write_text(_external_links_open_away(body), encoding="utf-8")
    return svg.name


#: PlantUML writes `target="_top"` on every link it draws, which for the caption's link out
#: to c4model.com means the review page is *replaced* by it — a reviewer three tabs deep
#: loses where they were to a click they made to look something up. Only outward links are
#: touched: `genseq://` and `src://` are handled inside the page and must stay where they are.
EXTERNAL_LINK = re.compile(r'(<a\b[^>]*\bhref="https?://[^"]*"[^>]*)\btarget="_top"')


def _external_links_open_away(svg: str) -> str:
    return EXTERNAL_LINK.sub(r'\1target="_blank" rel="noopener noreferrer"', svg)


# --------------------------------------------------------------------------- collecting


def sh(args: list[str], root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)


def matches(rel: str, sources: list[str], exclude: list[str]) -> bool:
    if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    # `**/*.genseq.puml` has to match `a.genseq.puml` at the root too, which fnmatch's
    # `**/` does not: it wants at least one directory. Test the basename form as well.
    return any(fnmatch.fnmatch(rel, pat)
               or (pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]))
               for pat in sources)


def worktree_sources(root: Path, sources: list[str], exclude: list[str]) -> list[str]:
    """Tracked and untracked alike. A sequence diagram regenerated a minute ago by the
    `sequence` step is usually not committed yet, and it is the whole point of the run."""
    seen: dict[str, None] = {}
    for args in (["git", "ls-files", "--", "*.puml"],
                 ["git", "ls-files", "--others", "--exclude-standard", "--", "*.puml"]):
        for rel in sh(args, root).stdout.splitlines():
            if rel and matches(rel, sources, exclude) and (root / rel).is_file():
                seen[rel] = None
    return sorted(seen)


def base_sources(root: Path, base: str, sources: list[str], exclude: list[str]) -> list[str]:
    got = sh(["git", "ls-tree", "-r", "--name-only", base], root)
    if got.returncode != 0:
        return []
    return sorted(rel for rel in got.stdout.splitlines()
                  if rel and matches(rel, sources, exclude))


def folds(containers: dict) -> tuple[dict[str, str], set[str]]:
    """The two lifeline rewrites a project declares: fold these into one, drop those.

    `drop` is resolved THROUGH `as`, so a lifeline can be folded into a name and that name
    dropped — which is how a suite that renamed its own participant between the base and
    the branch is excluded with one entry instead of one per name it has ever used."""
    rename = {name: cfg["as"] for name, cfg in containers.items()
              if isinstance(cfg, dict) and cfg.get("as")}
    drop = {rename.get(name, name) for name, cfg in containers.items()
            if isinstance(cfg, dict) and cfg.get("drop")}
    return rename, drop


def build(root: Path, rels: list[str], read, containers: dict) -> Graph:
    rename, drop = folds(containers)
    g = Graph()
    for rel in rels:
        text = read(rel)
        if text:
            parse_sequence(text, g, rel, rename, drop)
    classify(g, containers)
    return g


# --------------------------------------------------------------------------- main


def load_config(root: Path, path: str) -> dict:
    cfg_file = root / path
    if not cfg_file.is_file():
        return {}
    try:
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[c2] {path} is not valid JSON ({exc}) — continuing without it",
              file=sys.stderr)
        return {}


def merge_base(root: Path, base: str) -> str:
    """Where the branch forked, not where the base ref points now — the same rule every
    other producer on this page follows, so the "before" side is this branch's before."""
    got = sh(["git", "merge-base", base, "HEAD"], root)
    return got.stdout.strip() or base


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="the project under review")
    ap.add_argument("--base", default="", help="base ref (default: human-review.json's)")
    ap.add_argument("--config", default="human-review.json")
    ap.add_argument("--out-dir", default=".human-review/assets/c2")
    ap.add_argument("--name", default="",
                    help="manifest name and file stem (default: steps.c2.name, "
                         f"else {DEFAULT_NAME!r})")
    ap.add_argument("--print", action="store_true", dest="dump",
                    help="print the projected graph as JSON and write nothing")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    cfg = load_config(root, a.config)
    c2 = (cfg.get("steps") or {}).get("c2") or {}
    base = a.base or cfg.get("base", "origin/main")
    sources = c2.get("sources") or DEFAULT_SOURCES
    exclude = (c2.get("exclude") or []) + DEFAULT_EXCLUDE
    containers = c2.get("containers") or {}
    # The name is the card's title on the review page, so it is a name and not a level.
    # `C2` alone is the *level* in the C4 model — "the container diagram of this system"
    # is a view, and a view has a key. Structurizr projects write that key in the DSL
    # (`container petClinic "C2-Containers" …`) and export it as the filename, so a
    # project whose C4 is in a DSL should spell it the same here and get one name for one
    # picture across the model, the export and this page. `steps.c2.name` is how; the
    # default is the same convention for a project that has no DSL to copy from.
    name = a.name or c2.get("name") or DEFAULT_NAME

    rels = worktree_sources(root, sources, exclude)
    if not rels:
        print("[c2] no sequence diagrams to project from — nothing to draw", file=sys.stderr)
        return 3

    new = build(root, rels, lambda r: (root / r).read_text(encoding="utf-8", errors="replace"),
                containers)
    if not new.edges:
        print(f"[c2] {len(rels)} sequence diagram(s), and no call between two containers "
              "in any of them — nothing to draw", file=sys.stderr)
        return 3

    mb = merge_base(root, base)
    old_rels = base_sources(root, mb, sources, exclude)
    old = build(root, old_rels,
                lambda r: sh(["git", "show", f"{mb}:{r}"], root).stdout, containers)

    delta = diff(old, new)
    if a.dump:
        json.dump({"new": new.as_dict(), "old": old.as_dict(), "diff": delta},
                  sys.stdout, indent=2)
        print()
        return 0

    out = root / a.out_dir
    out.mkdir(parents=True, exist_ok=True)
    system = c2.get("system", "")
    title = c2.get("title", "C2 Containers")
    # The link used to live on the caption's "C4 model — level 2, Containers" text. It
    # moved onto the title instead: the title is the label a reader meets FIRST, and it is
    # the jargon the page cannot teach in the room it has — c4model.com is Simon Brown's
    # own site and explains the four levels in a paragraph, so a reader meeting "C2" here
    # has one click to the thing that defines it rather than a guess at what the boxes
    # mean. The caption is left with the one fact it is for: where the picture's contents
    # came from — and no count. A reviewer does not act differently on 4 traces than on 5,
    # and the number moves on every unrelated test added or renamed on either side of the
    # branch, which is not news this caption is trying to report.
    title = (f"[[{C4_URL}{{What a container diagram is, on Simon Brown's own site}} "
             f"{title}]]")
    caption = "projected from sequence diagrams generated from test traces"

    # Two popup indexes, not one, and they are the two the manifest's `new_details` /
    # `old_details` columns name. The page inlines both and merges them work-tree-first,
    # keyed by ids this file derives from content — so a line the branch did not touch
    # resolves to one entry from either carrier, and a line whose calls changed opens the
    # NEW inventory on Diff and New, and the OLD one on Old. The diff render writes into
    # the new carrier because that is the pane it belongs to, removed edges included:
    # their inventory is the only record left of what this branch stopped calling.
    new_details: dict = {}
    old_details: dict = {}

    def side(graph: Graph, stem: str, into: dict) -> str:
        if not graph.edges:
            return ""
        d = one_side(graph)
        (out / f"{stem}.puml").write_text(
            render(d["nodes"], d["edges"], title=title, system=system,
                   caption=caption, coloured=False, details=into), encoding="utf-8")
        return plantuml(out / f"{stem}.puml")

    new_svg = side(new, f"{name}.new", new_details)
    old_svg = side(old, f"{name}.old", old_details)

    changed = any(n["status"] != "same" for n in delta["nodes"].values()) or \
        any(e["status"] != "same" or e["operationsDelta"] for e in delta["edges"])
    (out / f"{name}.diff.puml").write_text(
        render(delta["nodes"], delta["edges"], title=title, system=system,
               caption=caption, coloured=bool(old.edges), details=new_details),
        encoding="utf-8")
    diff_svg = plantuml(out / f"{name}.diff.puml")

    def carrier(stem: str, index: dict) -> str:
        if not index:
            return ""
        (out / f"{stem}.json").write_text(
            json.dumps({"version": 2, "details": index}, ensure_ascii=False, indent=1)
            + "\n", encoding="utf-8")
        return f"{stem}.json"

    new_json = carrier(f"{name}.details.new", new_details)
    old_json = carrier(f"{name}.details.old", old_details)

    (out / f"{name}.json").write_text(
        json.dumps({"new": new.as_dict(), "old": old.as_dict(), "diff": delta,
                    "base": mb}, indent=2) + "\n", encoding="utf-8")

    status = "added" if not old.edges else ("modified" if changed else "unchanged")
    src_rel = (out / f"{name}.new.puml").relative_to(root).as_posix()
    manifest = out / "MANIFEST.tsv"
    manifest.write_text(
        "name\tsource\tkind\tstatus\tdiff_puml\tsvg\tfocus\tnew_svg\told_svg\t"
        "old_details\tnew_details\n"
        + "\t".join([name, src_rel, "structural", status,
                     f"{name}.diff.puml", diff_svg, "", new_svg, old_svg,
                     old_json, new_json])
        + "\n", encoding="utf-8")

    print(f"[c2] {len(new.nodes)} container(s), {len(new.edges)} call(s), projected from "
          f"{len(rels)} sequence diagram(s) -> {out}")
    if old.edges:
        moved = [f'{e["from"]}→{e["to"]}' for e in delta["edges"] if e["status"] != "same"]
        gone = [n for n, v in delta["nodes"].items() if v["status"] != "same"]
        print(f"[c2] against {mb[:12]}: "
              + (", ".join(gone + moved) if (gone or moved) else "no change of shape"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
