#!/usr/bin/env python3
"""Diff two PlantUML class / package / ER diagrams and render the delta in green and red.

Given a previous snapshot (OLD) and a current one (NEW) — e.g. the last committed
diagram vs the working copy at review time — emit a single merged diagram built on
NEW, where:

  * added element (class/enum/entity/package) -> green header
  * added member / attribute                  -> green line
  * removed member                            -> red struck-through line (kept in place)
  * removed element                           -> red header, all members struck
  * added relationship                        -> green connector + green label
  * removed relationship                      -> red connector + struck red label (re-added)

Everything the change did *not* touch is then shaded by how far it sits from it, on a
BFS over the relationship graph (both directions, both sides' edges): the changed
elements themselves wear the strongest amber wash, their direct neighbours a weaker one,
their neighbours' neighbours the weakest, and anything three hops or further out keeps
PlantUML's plain grey. An element already painted green or red is left unwashed — it is
saying it louder already. See `RIPPLE` for why the ladder starts where it does.

Direction is the whole point of the colour. Painting both sides red said only "something
here moved" and left the reader to infer, from a strikethrough they had to look for,
which way it went; green and red say it at a glance, the way the code diff on the same
page already does.

This is the review-time counterpart to the snapshot generators: the committed
diagram stays a plain picture of current reality, and the *diff* is computed on
demand from two snapshots rather than baked into git.

Pure standard library — no third-party deps. Handles the diagram families this
repo generates: class (DomainModel), ER/entity (DB), and package/component (C4).

Usage:
    puml_diff.py OLD.puml NEW.puml [--out merged.puml]
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field

# Element headers open with one of these keywords (optionally after a modifier
# like `abstract`). Used to recognise a body-less element declaration.
ELEMENT_KEYWORDS = {
    "abstract", "class", "enum", "interface", "entity", "package", "component",
    "node", "database", "object", "struct", "protocol", "rectangle", "folder",
    "frame", "cloud", "annotation",
}

# A connector is a run of line-drawing characters; these substrings mark one.
_CONNECTOR = re.compile(r"--|\.\.|->|<-|<\||\|>|\*-|-\*|o-|-o")

# A quoted end label on a relationship: a multiplicity, a role name, or both.
_QUOTED = re.compile(r'"[^"]*"')

# PlantUML's component shorthand — `[Domain] <<..domain>>`, as packages.puml uses.
# Without this, such a declaration matches no keyword, falls through to the
# preamble, and is copied verbatim from NEW: an added component would never be
# highlighted. Relationship lines also open with `[`, but parse() tries
# _split_relationship first, so they are consumed before this is consulted.
_BRACKET_COMPONENT = re.compile(r"^\[([^\]]+)\]")


# The review page's own added/removed vocabulary — the same pair its diff gutters, its
# line counts and its test-state flags use — rather than a palette private to this file:
# a second green on a fourth surface would read as a second meaning. `seq_puml_diff`
# imports these two names so the sequence delta beside this one cannot drift off them,
# and `build-review-html.py` maps both literals to the `--dgm-diff-*` variables that
# carry them into dark mode.
ADDED = "#2E7D32"
REMOVED = "#C62828"

# How far an element sits from what changed, said as a wash behind it. Green and red
# answer "what moved?"; they say nothing about what a reviewer has to read next, which is
# whatever the moved thing is attached to. So the epicentre is tinted hardest, one hop out
# less, two hops out barely, and anything further is left at PlantUML's own grey: a ripple
# spreading from the change. It earns its keep most in the unpruned picture, where the
# focus chooser is not doing the pruning for the reader.
#
# The ladder used to start one hop *out*, on the reasoning that hop zero already wore a
# saturated colour and did not need a second voice. That reasoning only holds for an
# element the diff actually paints — one added whole, or removed whole. The far commoner
# shape of a change is an element that merely *gained a field or an edge*: PlantUML draws
# it in the same grey as every stranger on the picture, only the one green line inside it
# or the one green arrow leaving it says anything at all. Shading from one hop out then
# put the loudest wash on the box *beside* the change and left the change itself pale —
# on the demo PR, the reader's eye landed on Pet and Owner while Visit and Vet, the two
# classes the new `vet` relationship joins, looked like background. The ladder now starts
# at zero, and only an element already carrying a green or red header is skipped, because
# there the old reasoning still applies — and a wash under a green header would be a
# second thing shouting the same word, with the green having to stay legible on it.
#
# Amber, and not a paler green or red, because those two are a *direction* and this is a
# *distance*: a third meaning laid on the same pair of hues would read as a fourth
# strength of the first two. Nothing green or red is ever drawn on top of a rippled
# element — only PlantUML's plain black label, which the page carries to near-white in
# dark mode.
#
# Index i is i hops from the change; `build-review-html.py` maps all three to
# `--dgm-ripple-*`, which are numbered from 1 and so run one ahead of these.
RIPPLE = ("#F2CF8E", "#F4DCB4", "#F2EBDB")

# What each rung of the ladder means, in the words the caption prints *wearing* that rung's
# wash. A wash is only a legend once something says so: the two hues above already answer
# to that rule, and a third visual language on the same picture cannot be the exception.
#
# The words used to be hop counts ("touched · 1 hop · 2 hops") beside a swatch block. That
# made the reader translate twice — swatch to number, number to box — to answer a question
# they were already asking in words: how much of the picture am I looking at? The caption
# now says the scope itself ("impacted + neighbours + neighbours") and lets each word carry
# its own ring's wash, so naming the scope and keying the colours is one phrase, not two.
# Hence "neighbours" twice: one word per ring, the nearer ring in the stronger amber.
RIPPLE_LABELS = ("impacted", "neighbours", "neighbours")

# The ink the legend's washed words are written in: the same black every box label wears,
# so `build-review-html.py` themes it to `--dgm-fg` exactly as it themes those labels.
# The wash under it is themed too — a creole background compiles to an SVG filter
# (`<feFlood>`), and the themer rewrites `flood-color` alongside `fill` — so the pair
# flips together: dark on pale amber by day, near-white on dark amber at night, the same
# way a label sits on a rippled box. It used to be a hex the themer did not know, from
# before the flood was themed; once the wash went dark the ink stayed dark on it and the
# words vanished. Named rather than left to PlantUML's default so the pairing is on record.
LEGEND_INK = "#000000"


def _hex(colour: str) -> str:
    """`#2E7D32` -> `2E7D32`.

    PlantUML's compound element colour — `#back:…;line:…;text:…` — takes each value
    *without* a `#` of its own; `#line:#2E7D32` is a syntax error. It went unnoticed
    because a PlantUML syntax error still writes a perfectly valid .svg (a green-on-black
    "Syntax Error?" dump) and because only a whole element added or removed paints a
    header at all, which no diagram in the demo branch happened to do."""
    return colour.lstrip("#")


# An element header takes its colours as `#back:<c>;line:<c>;text:<c>`, not as inline creole.
def _paint_header(colour: str = None, back: str = None) -> str:
    keys = []
    if back:
        keys.append(f"back:{_hex(back)}")
    if colour:
        keys += [f"line:{_hex(colour)}", f"text:{_hex(colour)}"]
    return f" #{';'.join(keys)}" if keys else ""


def _strip_markup(s: str) -> str:
    """Normalise a line to its plain content: drop any diff colouring/strikeout."""
    s = re.sub(r"</?color[^>]*>", "", s)
    s = s.replace("<s>", "").replace("</s>", "")
    s = re.sub(r"\[#[0-9A-Za-z_]+\]", "", s)      # coloured connector: -[#C62828]-
    # coloured element header: `#back:FFE3AE;line:2E7D32;text:2E7D32`, in any subset and
    # in either spelling of the value — a diagram written before `_hex` still says `#2E7D32`.
    s = re.sub(r"\s*#(?:back|line|text):[^;\s]+(?:;(?:back|line|text):[^;\s]+)*", "", s)
    return s.strip()


def _added(text: str) -> str:
    return f"<color:{ADDED}>{text}</color>"


def _struck(text: str) -> str:
    return f"<color:{REMOVED}><s>{text}</s></color>"


# `title Domain Model` — the single-line form. A bare `title` opens a multi-line block
# instead, which this deliberately leaves alone rather than mangling.
TITLE_RE = re.compile(r"^(\s*title\s+)(\S.*)$", re.I)


DIFF_SUFFIX = "Diff"


def legend() -> str:
    """The two words the picture is drawn in, in the colours it draws them.

    Shared with the sequence differ, which prints the same line under its own delta: one
    legend, one wording, whichever diagram the reader happens to be looking at."""
    return f"{_added('added')} or {_struck('removed')}"


def ripple_legend(rungs=RIPPLE) -> str:
    """The distance ladder written as the scope it describes, each word in its own wash.

    `impacted + neighbours + neighbours` — the word for each ring, wearing that ring's
    amber as a text background, strongest first. Printed as real washes rather than named
    in prose, because the thing being explained is a colour and the reader has to match
    one to the other by eye; printed *as the scope wording* rather than beside it, because
    the caption would otherwise say the same thing twice, once in words and once in
    swatches. Only the rungs that actually appear in the picture are listed: a wash for a
    ring the focus level pruned away promises a box the reader can hunt for and never find.

    A creole background is an SVG *filter* (`<feFlood flood-color="…">`); the page's
    dark-mode pass rewrites that flood alongside `fill`, and the ink on top is the box
    labels' own black, so word and wash flip together — see `LEGEND_INK`.

    `rungs` names the rings to print, outward from the change. A `None` entry names a ring
    that is on screen but wears no wash — every box in it is painted green or red, or it
    lies past the last rung of the ladder — and its word is printed plain, so the phrase
    still counts the rings correctly without promising a colour nothing on the picture has."""
    labels = dict(zip(RIPPLE, RIPPLE_LABELS))
    words = []
    for i, c in enumerate(rungs):
        word = labels[c] if c else RIPPLE_LABELS[min(i, len(RIPPLE_LABELS) - 1)]
        words.append(
            f"<back:{c}><color:{LEGEND_INK}>{word}</color></back>" if c else word)
    return " + ".join(words)


def _footer(line: str, source_caption: str) -> str:
    """The source's footer, with each file named by its name alone and the source's own
    caption said after it.

    The caption slot is the legend's, so the source's `caption` — on DomainModel.puml,
    "Diagram generated from code using Java reflection" — used to be dropped outright, and
    how the picture was produced went with it. It rides in the footer now, beside the
    provenance line it belongs with. The footer's paths lose their directories: a reader
    recognises `DomainModel.puml`; `petclinic-backend/docs/generated/` in front of it is
    a line of grey they read past. A glob keeps its directory — `domain/*.java` without
    `domain/` names nothing."""
    m = re.match(r"^(\s*footer\s+)(.*)$", line, re.I)
    if not m:
        return line
    text = " ".join(t.rsplit("/", 1)[-1] if "/" in t and "*" not in t
                    and "." in t.rsplit("/", 1)[-1] else t
                    for t in m.group(2).split(" "))
    if source_caption:
        text += f" — {source_caption}"
    return m.group(1) + text


def _mark_title(line: str) -> str:
    """Say in the title that the picture is a delta, not a snapshot.

    A diff of DomainModel is still headed "Domain Model", and a reader who arrives at
    it from a link — or finds it later in `.human-review/assets/` — has only the paint
    to tell them they are not looking at the model as it stands. So the title says the
    whole picture is a change; what the colours in it mean is the caption's job, under
    the picture, and the suffix is deliberately left unpainted: red is one half of the
    delta's vocabulary now, and a title in it would read as a removal.

    Idempotent, so re-diffing an already-diffed .puml does not stack suffixes."""
    m = TITLE_RE.match(line)
    if not m or m.group(2).rstrip().endswith(f"- {DIFF_SUFFIX}"):
        return line
    return f"{m.group(1)}{m.group(2).rstrip()} - {DIFF_SUFFIX}"


def _struck_header(header: str) -> str:
    """A removed element's header: its display name struck through in red, kept
    addressable by an alias so relationships pointing at it still resolve.

    `class Role` -> `class "<struck>Role</struck>" as Role`;
    `entity "owners" as owners` -> `entity "<struck>owners</struck>" as owners`.
    """
    bracket = _BRACKET_COMPONENT.match(header)
    if bracket:                                # [Notification] <<..notification>>
        name = bracket.group(1)
        # Switch to the `component "display" as Alias` form: a struck name inside
        # the brackets would declare a *differently named* component, so the
        # relationships still pointing at [Notification] would spawn a second box.
        rest = header[bracket.end():].strip()
        return f'component "{_struck(name)}" as {name}' + (f" {rest}" if rest else "")
    if '"' in header:                          # already has a quoted display name
        before, disp, after = header.split('"', 2)
        return f'{before}"{_struck(disp)}"{after}'
    parts = header.split()                     # class Role / abstract class Foo / enum Type
    name = parts[-1]
    keyword = " ".join(parts[:-1])
    return f'{keyword} "{_struck(name)}" as {name}'


# `[[url{tooltip} label]]`, or the older `text [[url{tooltip}]]`. The domain-model
# generator hangs one on every class and field so a reviewer can click through to the
# source, and the line it points at moves whenever anything above it moves. That must not
# read as a change: identity is what the diagram *says*, and a link is how you get
# somewhere else. The tooltip holds spaces, so the url and the label are matched apart.
_LINK = re.compile(
    r"\[\[(?P<url>[^\s\[\]{]+)(?P<tip>\{[^}]*\})?(?:\s+(?P<label>[^\]]*))?\]\]"
)


def _identity(s: str) -> str:
    """A line reduced to what it means: every link replaced by the text it shows."""
    return " ".join(_LINK.sub(lambda m: m.group("label") or "", s).split())


def _endpoint(side: str) -> str:
    """The element a relationship end names, without the end label glued to it.

    `_split_relationship` hands back `User "1"` and `"0..*" Role`, because a cardinality
    change *is* a change to the relationship. It is not a change to which elements the
    relationship joins, which is what a focus level walks.

    The whole quoted span goes, not each token that opens with a quote: an end label
    holds spaces as soon as it carries a role name beside its multiplicity
    (`"* visits" Visit`), and dropping only its first token left `visits" Visit` naming
    an element no diagram declares — so the relationship resolved to nothing and
    silently vanished from the delta.
    """
    tokens = _QUOTED.sub(" ", _identity(side)).split()
    return " ".join(tokens) if tokens else _identity(side)


def _member(text: str, paint=None) -> str:
    """One member line, with its link *wrapping* the text rather than trailing it.

    PlantUML prints the URL itself when a `[[...]]` carries no label, so a member written
    as `id : Integer [[src://…]]` renders as its own name followed by sixty characters of
    absolute path. The generator emits the wrapped form now — but the base side of a diff
    was written before it did, and a delta has to stay readable against a base that
    predates every change in it. So both forms are normalised here, on the way out.

    The diff colouring goes on the label, inside the link, so a struck member is still a
    struck member and still clickable.
    """
    m = _LINK.search(text)
    if not m:
        return paint(text) if paint else text
    visible = (m.group("label") or _LINK.sub("", text)).strip()
    target = m.group("url") + (m.group("tip") or "")
    return f"[[{target} {paint(visible) if paint else visible}]]"


def _element_name(header: str) -> str:
    """Extract the identity of an element from its (clean) header line."""
    h = _identity(header)
    bracket = _BRACKET_COMPONENT.match(h)
    if bracket:                           # [Domain] <<..domain>>
        return f"[{bracket.group(1)}]"    # keyed as written, so relationships resolve
    if " as " in h:                       # entity "owners" as owners
        return h.split(" as ")[-1].strip()
    if '"' in h:                          # package "com.x.y" / entity "owners"
        return h.split('"')[1]
    return h.split()[-1]                  # class Owner / enum Type


def _is_element_header(clean: str) -> bool:
    tokens = clean.split()
    if tokens and tokens[0] in ELEMENT_KEYWORDS:
        return True
    return bool(_BRACKET_COMPONENT.match(clean))


def _split_relationship(clean: str):
    """Parse `Left "card" <conn> "card" Right : label` → (left, conn, right, label).

    Returns None when the line is not a relationship. Quoted cardinalities such as
    "0..*" are skipped so their dots aren't mistaken for the connector.
    """
    body, sep, label = clean.partition(" : ")
    label = label.strip() if sep else None
    tokens = body.split()
    conn_idx = None
    for i, tok in enumerate(tokens):
        if tok.startswith('"'):           # cardinality, not the connector
            continue
        if _CONNECTOR.search(tok):
            conn_idx = i
            break
    if conn_idx is None or conn_idx == 0 or conn_idx == len(tokens) - 1:
        return None
    left = " ".join(tokens[:conn_idx])
    conn = tokens[conn_idx]
    right = " ".join(tokens[conn_idx + 1:])
    return left, conn, right, label


def _colorize_connector(conn: str, colour: str) -> str:
    """Inject `[<colour>]` into a connector so PlantUML draws the line in it.

    With colour `#C62828`: `--` -> `-[#C62828]-`, `-->` -> `-[#C62828]->`,
    `||--o{` -> `||-[#C62828]-o{`, `..>` -> `.[#C62828].>`.
    """
    for i, ch in enumerate(conn):
        if ch in "-.":
            return conn[:i + 1] + f"[{colour}]" + conn[i + 1:]
    return conn


@dataclass
class Element:
    header: str                      # clean, e.g. "class Owner" / 'entity "owners" as owners'
    has_body: bool
    members: list = field(default_factory=list)   # clean member lines, in order


@dataclass
class Diagram:
    preamble: list = field(default_factory=list)  # directive lines before the first element
    elements: dict = field(default_factory=dict)  # name -> Element (insertion order)
    relationships: list = field(default_factory=list)  # list[(left, conn, right, label)]


# Lines that describe the diagram rather than its content. Matched *before* anything
# else, because some of them look like content: `footer domain/*.java -> DomainModel.puml`
# parses as a relationship on the strength of its arrow, and every directive after it —
# `hide empty members`, the skinparams, the legend — was then dropped from the delta as
# "past the preamble". The rendered diff quietly disagreed with the diagram it was a diff of.
DIRECTIVE_RE = re.compile(
    r"^(?:!|title|caption|footer|header|legend|endlegend|end\s+legend|hide|show|skinparam|"
    r"scale|autonumber|left\s+to\s+right\s+direction|top\s+to\s+bottom\s+direction)\b",
    re.I,
)
# A <style> block is CSS, not diagram content, and it is full of lines that look exactly
# like content: `component {` parses as an element opening a body, `}` closes it, and
# `</style>` is neither, so it was dropped. The delta then emitted an unterminated
# stylesheet with a brace missing, PlantUML gave up and rendered a "Syntax Error?" image —
# which is still a valid .svg, so every check downstream was happy. Copy the block through
# verbatim instead.
STYLE_OPEN_RE = re.compile(r"^<style\b", re.I)
STYLE_CLOSE_RE = re.compile(r"^</style>", re.I)
LEGEND_OPEN_RE = re.compile(r"^legend\b", re.I)
LEGEND_CLOSE_RE = re.compile(r"^end\s*legend\b", re.I)


# C4-PlantUML is a macro dialect, not PlantUML syntax: `Component(id, "label", $techn=…)`
# and `Rel(a, b, "uses")` mean nothing to a differ that reasons about elements and arrows,
# and `Container_Boundary(…) {` opens a body that is not an element's. Diffing one produced
# a file with the relationships gone and the braces unbalanced, which PlantUML rendered as a
# "Syntax Error?" image — a valid .svg, so nothing downstream noticed. Refusing is honest.
C4_RE = re.compile(r"^\s*(?:Person|System|Container|Component|Rel|Boundary|"
                   r"\w+_Boundary|SHOW_LEGEND|LAYOUT_\w+)\w*\s*\(", re.M)


class UnsupportedDialect(Exception):
    """The source is not something this differ can reason about."""


def parse(puml: str) -> Diagram:
    if C4_RE.search(puml):
        raise UnsupportedDialect(
            "C4-PlantUML macros (Component(…)/Rel(…)) — the structural differ does not "
            "model them, and diffing one silently produces an unrenderable diagram")
    d = Diagram()
    current = None            # name of the element whose body we're inside
    seen_content = False      # have we passed the preamble yet?
    in_legend = False         # a legend's body is prose, and prose looks like anything
    in_style = False          # a <style> block is CSS that happens to look like content

    for raw in puml.splitlines():
        clean = _strip_markup(raw.strip())

        if in_style:
            d.preamble.append(raw.rstrip())
            in_style = not STYLE_CLOSE_RE.match(clean)
            continue

        if not clean or clean.startswith("@start") or clean == "@enduml":
            continue

        if STYLE_OPEN_RE.match(clean):
            d.preamble.append(raw.rstrip())
            in_style = not STYLE_CLOSE_RE.search(clean)
            continue

        if in_legend:
            d.preamble.append(raw.rstrip())
            in_legend = not LEGEND_CLOSE_RE.match(clean)
            continue

        if DIRECTIVE_RE.match(clean):
            d.preamble.append(raw.rstrip())
            in_legend = bool(LEGEND_OPEN_RE.match(clean)) and not LEGEND_CLOSE_RE.match(clean)
            continue

        if current is not None:
            if clean == "}":
                current = None
            else:
                d.elements[current].members.append(clean)
            continue

        if clean.endswith("{"):                      # element opening a body
            header = clean[:-1].strip()
            name = _element_name(header)
            d.elements[name] = Element(header=header, has_body=True)
            current = name
            seen_content = True
            continue

        rel = _split_relationship(clean)
        if rel is not None:
            d.relationships.append(rel)
            seen_content = True
            continue

        if _is_element_header(clean):                # body-less element
            d.elements[_element_name(clean)] = Element(header=clean, has_body=False)
            seen_content = True
            continue

        if not seen_content:                         # directive: title/skinparam/…
            d.preamble.append(raw.rstrip())

    return d


def _rel_key(rel) -> str:
    left, right, label = rel[0], rel[2], rel[3]     # identity ignores connector styling
    return f"{_identity(left)} {_identity(right)} :: {_identity(label or '')}"


def _render_relationship(rel, mark) -> str:
    left, conn, right, label = rel
    if mark:
        conn = _colorize_connector(conn, ADDED if mark == "added" else REMOVED)
    line = f"{left} {conn} {right}"
    if mark == "added" and label:
        line += f" : {_added(label)}"
    elif mark == "removed":                     # struck label; label-less lines get a marker
        line += f" : {_struck(label) if label else _struck('(removed)')}"
    elif label:
        line += f" : {label}"
    return line


ALL = "all"


def _impacted(old: Diagram, new: Diagram) -> set:
    """The elements this change actually touched.

    An element is impacted when it is new, gone, has a member added or removed, or sits
    at either end of a relationship that appeared or disappeared. Everything else in the
    diagram is context — true before the change and true after it.
    """
    touched = set()
    for name, el in new.elements.items():
        if name not in old.elements:
            touched.add(name)
            continue
        before = {_identity(m) for m in old.elements[name].members}
        after = {_identity(m) for m in el.members}
        if before != after:
            touched.add(name)
    touched |= set(old.elements) - set(new.elements)

    old_keys = {_rel_key(r) for r in old.relationships}
    new_keys = {_rel_key(r) for r in new.relationships}
    for r in new.relationships:
        if _rel_key(r) not in old_keys:
            touched |= {_endpoint(r[0]), _endpoint(r[2])}
    for r in old.relationships:
        if _rel_key(r) not in new_keys:
            touched |= {_endpoint(r[0]), _endpoint(r[2])}
    return touched


def _distances(old: Diagram, new: Diagram) -> dict:
    """Every reachable element, by how many relationships it sits from the change.

    Zero is the change itself; one is what it is directly attached to; and so on outwards
    until nothing new is reached. An element no chain of relationships connects to the
    change is absent from the map entirely — not infinitely far, simply not on it.

    Both sides' relationships are walked, so an element reachable only through an edge
    this change deleted is on the map too.

    Two readers of this one walk: `_within` cuts it at a hop count to decide what the
    picture contains, and `diff` reads the same numbers back to decide how hard to tint
    what survived. They were one loop run twice before, which is how a focus level and
    the shading in it could have disagreed about what "one hop" meant.
    """
    dist = {name: 0 for name in _impacted(old, new)}
    edges = [(_endpoint(raw_left), _endpoint(raw_right))
             for raw_left, _conn, raw_right, _label in old.relationships + new.relationships]
    frontier, hop = set(dist), 0
    while frontier:
        hop += 1
        nxt = set()
        for left, right in edges:
            if left in frontier and right not in dist:
                nxt.add(right)
            if right in frontier and left not in dist:
                nxt.add(left)
        for name in nxt:
            dist[name] = hop
        frontier = nxt
    return dist


def _within(old: Diagram, new: Diagram, hops: int) -> set:
    """The impacted elements, grown outwards `hops` relationships at a time.

    The DomainModel and DB diagrams are large enough that a two-line change arrives as a
    wall the reviewer has to search for red in. Zero hops is the change alone; each hop
    adds what it is directly attached to, which is what makes a change *readable* — a new
    column means little without the table it hangs off, and a new relationship means
    little without both things it relates.
    """
    return {name for name, d in _distances(old, new).items() if d <= hops}


def diff(old: Diagram, new: Diagram, focus=ALL) -> str:
    names = set(old.elements) | set(new.elements)
    rings = _distances(old, new)
    keep = names if focus == ALL else {n for n, d in rings.items() if d <= int(focus)}

    # An element the diff paints outright — added whole, or removed whole — carries its
    # own saturated header already, and is the one place where a wash underneath would be
    # a second voice saying the same word. Every other element on the picture, including
    # one that merely gained a field or an edge, has nothing but the ripple to place it.
    painted = set(old.elements) ^ set(new.elements)

    def ripple(name):
        """The wash behind one element, or None past the last rung and under a paint job.

        Index 0 is the change itself, so a class that gained a field or an edge — the
        commonest shape of a change, and one PlantUML draws in plain grey — is the
        hardest-washed box on the picture, and the ladder fades outwards from it."""
        d = rings.get(name)
        if d is None or d >= len(RIPPLE) or name in painted:
            return None
        return RIPPLE[d]

    # Under the picture, not in the title above it: this is the footer band where a
    # reader already looks to ask "what am I being shown?", and a legend belongs where
    # it is read rather than where it is loudest. A colour is only a legend once
    # something says so in words. The scope rides along, because it answers the same
    # question at the same moment.
    caption = f"caption {legend()}"
    # A ladder is only readable as a ladder once it has two rungs on screen: at focus 0
    # every box shown is the same distance from the change, so the wash carries no
    # information and announcing it would invite the reader to compare shades that are
    # all one shade.
    tints = {ripple(n) for n in keep}
    rungs = [c for c in RIPPLE if c in tints]
    shaded = len(rungs) > 1
    # The scope and the colour key are one phrase, not two: `impacted + neighbours +
    # neighbours`, each word wearing the wash of the ring it names. Said separately — a
    # hop count in prose and a swatch block after it — the caption named the same three
    # rings twice and asked the reader to line the two lists up themselves.
    if focus != ALL:
        hops = int(focus)
        # One word per ring the focus level asked for — `impacted + neighbours +
        # neighbours` at hops 2 — washed where that ring actually has a wash on screen.
        ladder = [RIPPLE[i] if i < len(RIPPLE) and RIPPLE[i] in tints else None
                  for i in range(hops + 1)]
        scope = "the impacted elements only" if hops == 0 else ripple_legend(ladder)
        if shaded:
            scope += ", shaded by distance"
        caption += f" — {scope} ({len(keep)} of {len(names)} shown)"
    elif shaded:
        caption += f" — {ripple_legend(rungs)}, shaded by distance from the change"

    # The caption goes FIRST, not after the preamble. A source that opens a `<style>` block
    # ends its preamble on the `<style>` line itself — the block's body arrives later — so
    # appending the caption there dropped it inside the stylesheet and PlantUML rendered the
    # whole diagram as a green-on-black "Syntax Error?" dump. Every diagram in this project
    # that styles itself (packages.puml, both C4 views) was failing that way, and a failed
    # render still produces a perfectly valid .svg, so nothing downstream noticed.
    # PlantUML does not care where a top-level directive sits.
    out = ["@startuml", caption, ""]
    source_caption = next((ln.strip()[len("caption"):].strip() for ln in new.preamble
                           if ln.strip().lower().startswith("caption")), "")
    has_footer = any(ln.strip().lower().startswith("footer") for ln in new.preamble)
    out += [_footer(_mark_title(ln), source_caption) for ln in new.preamble
            if not ln.strip().lower().startswith("caption")]
    if source_caption and not has_footer:
        out.append(f"footer {source_caption}")

    # ── Elements present in NEW (red header if the whole element is new) ──────
    for name, el in new.elements.items():
        if name not in keep:
            continue
        is_new = name not in old.elements
        old_members = old.elements[name].members if not is_new else []
        current = {_identity(m) for m in el.members}
        removed = [m for m in old_members if _identity(m) not in current]

        header = el.header + _paint_header(ADDED if is_new else None, ripple(name))
        if not el.has_body and not removed:
            out.append(header)
            continue

        out.append(header + " {")
        old_set = {_identity(m) for m in old_members}
        for m in el.members:
            fresh = not is_new and _identity(m) not in old_set
            out.append("  " + _member(m, _added if fresh else None))
        for m in removed:                            # gone in NEW → struck ghost
            out.append("  " + _member(m, _struck))
        out.append("}")

    # ── Elements removed entirely (present only in OLD): struck-through ghost ─
    for name, el in old.elements.items():
        if name in new.elements or name not in keep:
            continue
        header = _struck_header(el.header) + _paint_header(REMOVED, ripple(name))
        if not el.members:
            out.append(header)
            continue
        out.append(header + " {")
        for m in el.members:
            out.append("  " + _member(m, _struck))
        out.append("}")

    out.append("")

    # ── Relationships ────────────────────────────────────────────────────────
    old_keys = {_rel_key(r) for r in old.relationships}
    new_keys = {_rel_key(r) for r in new.relationships}
    # A relationship with one end pruned away would draw an arrow to nothing, so it goes
    # with the end it lost.
    def both_ends_kept(r):
        return _endpoint(r[0]) in keep and _endpoint(r[2]) in keep

    for r in new.relationships:
        if both_ends_kept(r):
            out.append(_render_relationship(r, "added" if _rel_key(r) not in old_keys else None))
    for r in old.relationships:
        if _rel_key(r) not in new_keys and both_ends_kept(r):   # gone in NEW → red ghost
            out.append(_render_relationship(r, "removed"))

    # PlantUML renders an error page for a diagram with no content, and an empty focus
    # level is a real answer — say it in the picture rather than break it.
    if not keep:
        out.append('note as EMPTY\n  nothing changed at this focus level\nend note')

    out.append("")
    out.append("@enduml")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("old", help="Previous snapshot (.puml)")
    ap.add_argument("new", help="Current snapshot (.puml)")
    ap.add_argument("--out", help="Write merged diagram here (default: stdout)")
    ap.add_argument(
        "--focus", default=ALL, metavar="0|1|2|3|all",
        help=(
            "How much context to keep around what changed: 0 = the impacted elements "
            "alone, N = grow N relationships outwards from them, all = the whole "
            "diagram (default). For the large diagrams, where a two-line change "
            "otherwise arrives as a wall to search for red in."
        ),
    )
    args = ap.parse_args(argv)
    if args.focus != ALL and not args.focus.isdigit():
        ap.error("--focus takes a non-negative integer or 'all'")

    with open(args.old, encoding="utf-8") as f:
        old = parse(f.read())
    with open(args.new, encoding="utf-8") as f:
        new = parse(f.read())

    merged = diff(old, new, args.focus)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(merged)
    else:
        sys.stdout.write(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
