"""Tests for puml_diff. Run: python3 -m pytest -q, or `python3 test_puml_diff.py`."""
import os
import re

import puml_diff as m

HERE = os.path.dirname(__file__)
BEFORE = os.path.join(HERE, "testdata", "domain_before.puml")
AFTER = os.path.join(HERE, "testdata", "domain_after.puml")


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return m.parse(f.read())


def _diff():
    return m.diff(_parse(BEFORE), _parse(AFTER))


# ── Parsing ─────────────────────────────────────────────────────────────────

def test_parse_elements_and_members():
    d = _parse(BEFORE)
    assert set(d.elements) == {
        "Owner",
        "Pet",
        "PetType",
        "Role",
        "Specialty",
        "User",
        "Vet",
        "Visit",
    }
    assert d.elements["Owner"].members[0] == "id : Integer"
    assert "email : String" not in d.elements["Owner"].members


def test_end_label_holding_a_role_beside_its_multiplicity_still_names_its_element():
    """`Pet "* visits" Visit` joins Pet and Visit — not Pet and `visits" Visit`.

    An end label carries a role name next to its multiplicity now, so it holds spaces.
    Dropping only the token that opened the quote left the rest of the label glued to
    the element name, the end resolved to an element no diagram declares, and the whole
    relationship dropped out of the delta without a word.
    """
    d = m.parse('@startuml\nclass Pet\nclass Visit\n'
                'Pet "pet" <--> "~* visits" Visit\n@enduml\n')
    left, _conn, right, _label = d.relationships[0]
    assert (m._endpoint(left), m._endpoint(right)) == ("Pet", "Visit")


def test_cardinality_dots_not_mistaken_for_connector():
    d = _parse(BEFORE)
    vet_rel = next(r for r in d.relationships if r[0].startswith("Vet"))
    assert vet_rel[1] == "--"                 # connector, not the "0..*" cardinality
    assert vet_rel[3] == "specialties"        # label


# ── Added → green (solid) ───────────────────────────────────────────────────
# Both sides used to be red, and which way a mark pointed had to be read off a
# strikethrough. The colour carries the direction now; the strikethrough stays, because
# a removal has to survive being printed, screenshotted or read by someone who cannot
# separate the two hues.

ADD = m.ADDED
DEL = m.REMOVED
# An element header's compound colour takes each value without a `#` of its own — see
# `_hex`. Inline creole (`<color:#2E7D32>`) keeps it; the two spellings are not
# interchangeable, and PlantUML answers the wrong one with a picture of the words
# "Syntax Error?", which is still a valid .svg.
HEX_ADD, HEX_DEL = m._hex(ADD), m._hex(DEL)


def test_added_member_green():
    assert f"<color:{ADD}>email : String</color>" in _diff()


def test_added_class_green_solid_header():
    assert f"class Invoice #line:{HEX_ADD};text:{HEX_ADD} {{" in _diff()


def test_added_relationship_and_label_green():
    assert f'Owner "1" -[{ADD}]- "0..*" Invoice : <color:{ADD}>invoices</color>' in _diff()


# ── Removed → red + struck-through ───────────────────────────────────────────

def test_removed_member_struck():
    assert f"<color:{DEL}><s>time : LocalTime</s></color>" in _diff()


def test_removed_class_title_struck():        # struck *and* red → doubly distinct from added
    out = _diff()
    assert f'class "<color:{DEL}><s>Role</s></color>" as Role #line:{HEX_DEL};text:{HEX_DEL} {{' in out
    assert f"<color:{DEL}><s>name : String</s></color>" in out   # its members struck too


def test_removed_relationship_label_struck():
    assert f'User "1" -[{DEL}]- "0..*" Role : <color:{DEL}><s>user</s></color>' in _diff()


# ── The two hues are the page's own added/removed pair ───────────────────────
# Not a palette private to this file: the review page paints a code hunk, a line count
# and a test-state flag with the same two, and a diagram that picked its own green would
# be a second meaning for one colour on one page.

def test_the_palette_is_green_for_added_and_red_for_removed():
    assert (ADD, DEL) == ("#2E7D32", "#C62828")


# ── Changed member = removed old + added new ─────────────────────────────────

def test_changed_member_shows_both():
    out = _diff()
    assert f"<color:{ADD}>id : Long</color>" in out             # new type added
    assert f"<color:{DEL}><s>id : Integer</s></color>" in out   # old type struck


# ── No-op: identical snapshots mark no element as changed ────────────────────
# The caption and the title are excluded because they describe the *artifact* — that
# this picture is a diff rendering — which stays true on a diff that found nothing.

def test_identical_snapshots_have_no_diff_markup():
    out = m.diff(_parse(AFTER), _parse(AFTER))
    body = "\n".join(
        ln for ln in out.splitlines()
        if not ln.startswith("caption") and not ln.lstrip().lower().startswith("title")
    )
    assert f"<color:{ADD}>" not in body and f"<color:{DEL}>" not in body
    assert "#line:" not in body
    assert "<s>" not in body


# ── The title says the picture is a delta ────────────────────────────────────
# Only that. What the colours in it mean is the caption's job, in the footer band under
# the picture; an unpainted suffix, because red is half the delta's vocabulary now and a
# title written in it would read as a removal.

def test_title_is_marked_as_a_diff():
    out = m.diff(_parse(BEFORE), _parse(AFTER))
    assert "title Domain Model - Diff" in out
    assert f"<color:{DEL}>Diff" not in out


def test_title_marking_is_idempotent():
    once = m.diff(_parse(BEFORE), _parse(AFTER))
    twice = m.diff(m.parse(once), m.parse(once))
    assert twice.count("- Diff") == 1


if __name__ == "__main__":
    tests = sorted(
        n for n, v in list(globals().items())
        if n.startswith("test_") and callable(v)
    )
    for name in tests:
        globals()[name]()
        print("PASS", name)
    print(f"--- all {len(tests)} tests passed ---")


# ── Component shorthand: `[Name] <<stereotype>>`, as packages.puml uses ──────

_PKG_BEFORE = """@startuml
title Logical Architecture
[Domain] <<..domain>>
[Repository] <<..repository>>
[Repository] --> [Domain]
@enduml
"""

_PKG_AFTER = """@startuml
title Logical Architecture
[Domain] <<..domain>>
[Notification] <<..notification>>
[Notification] --> [Domain]
@enduml
"""


def _pkg_diff():
    return m.diff(m.parse(_PKG_BEFORE), m.parse(_PKG_AFTER))


def test_bracket_component_parsed_as_element_not_preamble():
    d = m.parse(_PKG_BEFORE)
    assert set(d.elements) == {"[Domain]", "[Repository]"}
    assert not any("[Domain]" in line for line in d.preamble)


def test_added_bracket_component_gets_green_header():
    assert f"[Notification] <<..notification>> #line:{HEX_ADD};text:{HEX_ADD}" in _pkg_diff()


def test_unchanged_bracket_component_keeps_its_name_and_wears_the_wash():
    """[Domain] is not added and not removed, so no colour is painted on its name — but
    both the added and the removed relationship hang off it, which makes it the box a
    reviewer reads next and so the epicentre of the ripple."""
    out = _pkg_diff()
    assert f"\n[Domain] <<..domain>> #back:{m.RIPPLE[0].lstrip('#')}\n" in out
    assert HEX_ADD not in out.split("[Domain]")[1].split("\n")[0]


def test_removed_bracket_component_struck_but_keeps_alias():
    # Aliased so relationships still pointing at [Repository] resolve to the
    # struck box instead of spawning a second, unstyled one.
    assert f'component "<color:{DEL}><s>Repository</s></color>" as Repository' in _pkg_diff()


# ── Focus levels ─────────────────────────────────────────────────────────────
# DomainModel and DB are large enough that a two-line change arrives as a wall the
# reviewer has to search for red in. `--focus` keeps what changed plus N relationships
# outwards, so the same delta can be read at whatever radius makes it legible.

def _focused(level):
    return m.diff(_parse(BEFORE), _parse(AFTER), level)


def _elements(out):
    """The elements a rendered diff draws, by name — a removed one wears its strikeout."""
    return {m._element_name(m._strip_markup(ln).rstrip("{").strip())
            for ln in out.splitlines() if ln.startswith(("class ", "enum "))}


def test_focus_zero_keeps_only_what_changed():
    kept = _elements(_focused("0"))
    # Owner gained a field and an Invoice; Vet's id changed type; Visit lost `time`;
    # Invoice is new; Role was deleted — and User with it, since the relationship
    # between them disappeared and a relationship has two ends.
    assert kept == {"Owner", "Vet", "Visit", "Invoice", "Role", "User"}
    assert "Specialty" not in kept        # untouched, and one hop from Vet
    assert "PetType" not in kept


def test_each_hop_pulls_in_the_next_ring():
    assert "Pet" not in _elements(_focused("0"))
    assert "Pet" in _elements(_focused("1"))        # Owner -- Pet
    assert "PetType" not in _elements(_focused("1"))
    assert "PetType" in _elements(_focused("2"))    # PetType -- Pet -- Owner


def test_focus_all_is_the_whole_diagram_and_the_default():
    assert _focused(m.ALL) == _diff()
    assert _elements(_focused(m.ALL)) >= _elements(_focused("3"))


# A relationship whose far end was pruned would draw an arrow into nothing.
def test_a_pruned_end_takes_its_relationship_with_it():
    out = _focused("0")
    assert "Invoice" in out
    assert "PetType" not in out
    for line in out.splitlines():
        if " -- " in line or "-[#red]-" in line:
            left, right = line.split()[0], line.split()[-1].split(":")[0]
            assert "PetType" not in (left, right)


def test_the_caption_spells_the_two_colours_out_in_words():
    """A colour is only a legend once something says so in words, and the caption is
    where a reader already looks to ask what they are being shown. The title says it too,
    for a picture met on its own — linked to, or found later in the assets directory."""
    assert f"caption <color:{ADD}>added</color> or <color:{DEL}><s>removed</s></color>" in _diff()


def _plain_caption(out):
    """The caption line with its creole stripped — the wording, without the paint."""
    line = next(ln for ln in out.splitlines() if ln.startswith("caption"))
    return re.sub(r"</?(?:back|color)[^>]*>", "", line)


def test_the_caption_says_what_is_being_shown():
    """One word per ring the focus level asked for, counting outwards — so the phrase that
    names the scope is the same phrase that keys the colours, instead of a hop count in
    prose followed by a swatch block saying the same three things again."""
    assert "the impacted elements only (6 of 9 shown)" in _focused("0")
    assert "impacted + neighbours" in _plain_caption(_focused("1"))
    assert "impacted + neighbours + neighbours" in _plain_caption(_focused("2"))
    assert "shown)" not in _diff()          # the whole diagram needs no qualifier


# ── The ripple ───────────────────────────────────────────────────────────────
# A focus level answers "how much do I want on screen?"; it does not answer, once the
# picture is on screen, "which of these is near the change?" — and in the unpruned view
# nothing did. The changed boxes are washed hardest, one hop out less, two hops barely,
# and the far field is left at PlantUML's own grey.
#
# The ladder used to start one hop *out*, which put the loudest wash on the neighbours of
# the change and left the change itself looking like background — the exact opposite of
# what the wash is for, whenever the change is a field or an edge rather than a whole new
# class (PlantUML paints no header for those, so nothing else marks them either).


def _back(out, element):
    """The `#back:` colour on one element's header, or None."""
    for ln in out.splitlines():
        if not ln.startswith(("class ", "enum ", "entity ")):
            continue
        if m._element_name(m._strip_markup(ln.rstrip("{").strip())) != element:
            continue
        mark = re.search(r"#back:([0-9A-Fa-f]{6})", ln)
        return f"#{mark[1].upper()}" if mark else None
    return None


def test_the_changed_element_wears_the_strongest_tint():
    """Owner gained a field and Visit lost one. PlantUML draws neither header any
    differently — one green line inside a box is the whole signal — so if anything on the
    picture is to be washed hardest it is these, not the boxes standing next to them."""
    out = _diff()
    assert _back(out, "Owner") == m.RIPPLE[0]
    assert _back(out, "Visit") == m.RIPPLE[0]


def test_the_ripple_fades_with_each_hop():
    out = _diff()
    assert _back(out, "Owner") == m.RIPPLE[0]        # gained `email`
    assert _back(out, "Pet") == m.RIPPLE[1]          # Owner -- Pet
    assert _back(out, "PetType") == m.RIPPLE[2]      # PetType -- Pet -- Owner
    assert m.RIPPLE[0] != m.RIPPLE[1] != m.RIPPLE[2]


def test_a_chain_leading_away_from_the_change_dims_one_rung_at_a_time():
    """A-B-C-D-E, joined in a line, with the change in A: the three rungs come off the
    ladder in order and E — four hops out — is left at PlantUML's own grey. The rule in
    one picture, on a diagram small enough to hold in the head."""
    names = ["A", "B", "C", "D", "E"]
    lines = ["@startuml"] + [f"class {n}" for n in names]
    lines += [f"{a} -- {b}" for a, b in zip(names, names[1:])] + ["@enduml"]
    chain = "\n".join(lines) + "\n"
    changed = chain.replace("class A", "class A {\n  id : Integer\n}")
    out = m.diff(m.parse(chain), m.parse(changed))
    assert [_back(out, n) for n in names] == [*m.RIPPLE, None, None]


def test_an_element_the_diff_paints_is_left_untinted():
    """A green or a red header is already the loudest thing on the picture, and a wash
    under it would be a second voice saying the same word — with the green having to stay
    legible on it, a contrast problem invented for no gain. Only *those* are skipped: an
    element that merely gained a field carries no paint at all and keeps its wash."""
    out = _diff()
    assert _back(out, "Invoice") is None             # added whole -> green header
    assert _back(out, "Role") is None                # removed whole -> red header
    assert _back(out, "Owner") is not None           # gained a field -> nothing else says so


def test_past_the_last_ring_the_box_keeps_plantuml_grey():
    """The ladder has three rungs and then stops: a fourth wash indistinguishable from
    white would claim a relationship to the change that the reader cannot see."""
    lines = ["@startuml"] + [f"class C{i}" for i in range(6)]
    lines += [f"C{i} -- C{i + 1}" for i in range(5)] + ["@enduml"]
    chain = "\n".join(lines) + "\n"
    grown = chain.replace("class C0", "class C0 {\n  id : Integer\n}")
    out = m.diff(m.parse(chain), m.parse(grown))
    assert [_back(out, f"C{i}") for i in range(5)] == [*m.RIPPLE, None, None]


def test_an_element_no_relationship_reaches_is_not_on_the_ladder():
    """Unreachable is not "far away, tinted faintly" — it is absent from the walk."""
    plain = "@startuml\nclass A\nclass B\nclass Island\nA -- B\n@enduml\n"
    grown = plain.replace("class A", "class A {\n  id : Integer\n}")
    before, after = m.parse(plain), m.parse(grown)
    assert "Island" not in m._distances(before, after)
    assert _back(m.diff(before, after), "Island") is None


def test_the_caption_says_the_shading_means_distance():
    """A colour is only a legend once something says so in words — the same rule the two
    hues above it already answer to."""
    assert "shaded by distance from the change" in _diff()
    assert "impacted + neighbours, shaded by distance" in _plain_caption(_focused("1"))
    assert "shaded" not in _focused("0")     # nothing but hop zero is on screen


def _washed(word_index, out):
    """The `<back:…>` colour the caption's nth legend word wears, or None."""
    line = next(ln for ln in out.splitlines() if ln.startswith("caption"))
    spans = re.findall(r"<back:(#[0-9A-Fa-f]{6})>|(\bimpacted\b|\bneighbours\b)", line)
    # Walk the caption, pairing each legend word with the wash opened just before it.
    words, pending = [], None
    for colour, word in spans:
        if colour:
            pending = colour
        else:
            words.append(pending)
            pending = None
    return words[word_index] if word_index < len(words) else None


def test_each_legend_word_wears_its_own_hop_colour():
    """Words alone cannot say which amber is which, and two rings sharing the word
    "neighbours" can only be told apart by the wash behind each. The nearer ring gets the
    stronger wash, so the caption descends left to right exactly as the boxes do."""
    out = _focused("2")
    assert [_washed(i, out) for i in range(3)] == list(m.RIPPLE)
    # ...and the second `neighbours` is weaker than the first, not merely different.
    assert _washed(1, out) == m.RIPPLE[1] != _washed(2, out) == m.RIPPLE[2]
    # One neighbours word per ring, and no leftover hop-count labels.
    plain = _plain_caption(out)
    assert plain.count("neighbours") == 2
    assert "1 hop" not in plain and "2 hops" not in plain and "touched" not in plain


def test_the_washed_words_wear_the_box_labels_ink():
    """A creole background compiles to an SVG *filter*, and the page's dark mode themes
    that flood like any fill — the wash goes dark amber at night. The ink on it must go
    near-white at the same time, and the one ink the themer is guaranteed to carry to
    `--dgm-fg` is the black every box label already wears. Any other hex is left as
    written and vanishes on the dark wash."""
    for out in (_diff(), _focused("2")):
        assert f"<back:{m.RIPPLE[0]}><color:{m.LEGEND_INK}>impacted</color></back>" in out
    assert m.LEGEND_INK == "#000000"


def test_the_legend_lists_only_the_rungs_the_picture_still_has():
    """A wash for a ring the focus level pruned away promises a box the reader can hunt
    for and never find, so the third ring's cream never appears at focus 1."""
    out = _focused("1")
    assert f"<back:{m.RIPPLE[1]}><color:{m.LEGEND_INK}>neighbours</color></back>" in out
    assert m.RIPPLE[2] not in out
    assert "<back:" not in _focused("0")     # one rung is not a ladder


def test_a_header_colour_carries_no_inner_hash():
    """`#line:#2E7D32` is a PlantUML syntax error, and a syntax error still writes a
    perfectly valid .svg — a picture of the words "Syntax Error?" — so nothing downstream
    can notice it. Every value in a compound element colour goes bare."""
    for out in (_diff(), _focused("1"), _pkg_diff()):
        assert not re.search(r"#(?:back|line|text):#", out)


# An identical pair has nothing impacted, and an empty diagram is a PlantUML error page.
def test_nothing_changed_at_focus_zero_still_renders():
    same = _parse(BEFORE)
    out = m.diff(same, _parse(BEFORE), "0")
    assert "nothing changed at this focus level" in out
    assert out.strip().endswith("@enduml")


# The domain-model generator hangs a `[[src://…]]` link on every class and field, and
# the line it points at moves whenever anything above it moves. Identity is what the
# diagram says; a link is how you get somewhere else.
def test_a_source_link_is_not_a_change():
    plain = m.parse("""@startuml
class Owner {
  id : Integer
}
@enduml""")
    linked = m.parse("""@startuml
class Owner [[src://a/Owner.java:12{open Owner}]] {
  id : Integer [[src://a/Owner.java:15{open id}]]
}
@enduml""")
    out = m.diff(plain, linked)
    body = "\n".join(ln for ln in out.splitlines() if not ln.startswith("caption"))
    assert f"<color:{ADD}>" not in body and f"<color:{DEL}>" not in body
    assert m._impacted(plain, linked) == set()
    # …and the link still renders, rewrapped around the member it points at
    assert "[[src://a/Owner.java:15{open id} id : Integer]]" in out


# ── Sequence diagrams: a changed statement is one marked arrow, not a pair ──
# An arrow carries `[[genseq://<id>{…} label]]`, and the id is a fingerprint of what the
# arrow reveals. When only that moves, the call is unchanged and the statement behind it
# is not — telling the reviewer that twice, once struck and once red, is twice the red
# for one fact.
import seq_puml_diff as sq


def _no_caption(out):
    """The delta minus its legend line. Every assertion below is about the conversation,
    and the legend is a fixed line that carries the very markup they check the absence
    of — `<s>removed</s>` in the delta's own red."""
    return "\n".join(ln for ln in out.splitlines() if not ln.startswith("caption"))


def _seq(old_arrow, new_arrow):
    frame = "@startuml\nparticipant Backend\nparticipant DB\n%s\n@enduml\n"
    return _no_caption(sq.diff(frame % old_arrow, frame % new_arrow))


ARROW = 'Backend -> DB: [[genseq://%s{Click for the statement} select visits]]'


def test_a_changed_statement_marks_the_arrow_it_hides_behind():
    out = _seq(ARROW % "aaa1111", ARROW % "bbb2222")
    assert "<s>" not in out                       # not a removal
    assert out.count("select visits") == 1        # not a pair
    # The arrowhead carries the mark. The label cannot: PlantUML renders no markup inside
    # a link label — the tags print as literal text and the link comes apart — so a
    # coloured label would cost the click that the whole arrow exists to offer.
    assert f"-[{sq.ADDED}]>" in out
    assert "[[genseq://bbb2222{Click for the statement} select visits]]" in out
    assert "<color" not in out.split("participant DB")[1]


def test_an_untouched_arrow_stays_plain():
    out = _seq(ARROW % "aaa1111", ARROW % "aaa1111")
    assert f"<color:{sq.ADDED}>" not in out and f"<color:{sq.REMOVED}>" not in out
    assert "-[#" not in out


# The label itself changing is a different call, not the same one restated: that is a
# genuine removal plus a genuine addition.
def test_a_changed_label_is_still_a_removal_and_an_addition():
    other = 'Backend -> DB: [[genseq://bbb2222{Click for the statement} select pets]]'
    out = _seq(ARROW % "aaa1111", other)
    assert "<s>" in out
    assert "select pets" in out and "select visits" in out


# ── A section header that learned where its test lives is not a behaviour change ──
# The generator started hanging `[[src://<file>:<line>{…} Title]]` on every `== … ==`
# header. Diffed against a base that predates it, every scenario in the picture read as
# one struck chapter followed by an identical new one — before a single call had moved.

def _sections(old_header, new_header):
    frame = "@startuml\nparticipant Backend\n%s\nBackend -> Backend: x\n@enduml\n"
    return _no_caption(sq.diff(frame % old_header, frame % new_header))


LINKED_HEADER = "== [[src://petclinic-test/src/add-visit.spec.ts:26{Click to open the test} Add a visit]] =="


def test_a_section_header_that_gained_a_source_link_is_not_a_change():
    out = _sections("== Add a visit ==", LINKED_HEADER)
    assert "<s>" not in out                       # not a removal
    assert out.count("Add a visit") == 1          # not a pair
    assert f"<color:{sq.ADDED}>" not in out      # not even a repaint
    assert LINKED_HEADER in out                   # …and the link still renders


def test_a_moved_test_does_not_move_the_conversation():
    """Only the line number differs — everything above the test shifted, that is all."""
    out = _sections(LINKED_HEADER, LINKED_HEADER.replace(":26{", ":43{"))
    assert "<s>" not in out
    assert f"<color:{sq.ADDED}>" not in out


def test_a_renamed_section_is_still_a_removal_and_an_addition():
    out = _sections(LINKED_HEADER, LINKED_HEADER.replace("Add a visit", "Book a visit"))
    assert "<s>" in out
    assert "Add a visit" in out and "Book a visit" in out


# An arrow's `src://` handle is no more a change than a header's; its `genseq://` one
# still is, because that fingerprint moving means the statement behind it was rewritten.
def test_an_arrow_link_target_never_decides_identity():
    plain = "Browser -> Backend: GET /api/vets"
    linked = "Browser -> Backend: [[src://petclinic-test/src/x.spec.ts:9{open} GET /api/vets]]"
    out = _seq(plain, linked)
    assert "<s>" not in out
    assert out.count("GET /api/vets") == 1
    assert f"<color:{sq.ADDED}>" not in out


def test_the_two_differs_paint_from_one_palette():
    """A reviewer flips between a structural delta and a sequence delta on one page.
    Two greens there would be two meanings, so the sequence differ takes its hues from
    this module rather than declaring its own."""
    assert (sq.ADDED, sq.REMOVED) == (ADD, DEL)


def test_a_sequence_delta_prints_the_same_legend_under_the_same_heading():
    """The legend goes in the footer band on both diagrams, worded and coloured the same:
    a sequence delta is the taller of the two and the one a reader scrolls, so the words
    have to ride on the picture rather than only in the page framing it."""
    out = sq.diff("@startuml\ntitle Flow\nparticipant A\n@enduml\n",
                  "@startuml\ntitle Flow\nparticipant A\nA -> A: x\n@enduml\n")
    assert f"caption <color:{ADD}>added</color> or <color:{DEL}><s>removed</s></color>" in out
    assert "title Flow - Diff" in out


def test_a_caption_already_in_the_source_is_not_read_as_a_message():
    """`caption` was not in the sequence differ's meta pattern, so a source carrying one
    dropped into the body — where it reads as a line of the conversation and the delta
    reports it as added or removed."""
    out = sq.diff("@startuml\nparticipant A\ncaption from the generator\n@enduml\n",
                  "@startuml\nparticipant A\ncaption from the generator\nA -> A: x\n@enduml\n")
    assert "from the generator" not in out
    assert out.count("caption ") == 1


# ── Member links: wrapping, whichever form the input used ────────────────────
# PlantUML prints the URL when a `[[...]]` has no label. The generator wraps the member
# text now, but the base side of a diff predates that — and a delta has to stay readable
# against a base that predates every change in it.

def _members(out, cls):
    body = out.split(f"class {cls}", 1)[1].split("\n}", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith("[[")]


OLD_FORM = """@startuml
class Owner {
  id : Integer [[src://a/Owner.java:5{open id}]]
  gone : String [[src://a/Owner.java:9{open gone}]]
}
@enduml"""

NEW_FORM = """@startuml
class Owner {
  [[src://a/Owner.java:5{open id} id : Integer]]
  [[src://a/Owner.java:12{open added} added : String]]
}
@enduml"""


def test_a_trailing_link_is_rewrapped_around_its_member():
    out = m.diff(m.parse(OLD_FORM), m.parse(NEW_FORM))
    for line in _members(out, "Owner"):
        # nothing may sit outside the link, or PlantUML renders the raw URL
        assert line.startswith("[[") and line.endswith("]]")
    assert "{open id} id : Integer]]" in out          # unchanged, still plain


def test_the_diff_colour_goes_inside_the_link():
    out = m.diff(m.parse(OLD_FORM), m.parse(NEW_FORM))
    assert f"{{open added}} <color:{ADD}>added : String</color>]]" in out
    assert f"{{open gone}} <color:{DEL}><s>gone : String</s></color>]]" in out


def test_a_member_with_no_link_is_untouched():
    plain = "@startuml\nclass X {\n  a : int\n}\n@enduml"
    assert "  a : int" in m.diff(m.parse(plain), m.parse(plain))


# ── Directives survive the diff ──────────────────────────────────────────────
# `footer domain/*.java -> DomainModel.puml` parses as a relationship on the strength of
# its arrow. Everything after it was then treated as content, so the skinparams and the
# legend never reached the delta — and the rendered diff disagreed with the diagram it
# was a diff of: underlined links, no legend, different icon sizes.

DIRECTIVES = """@startuml
title Domain Model
footer domain/*.java -> petclinic-backend/docs/generated/DomainModel.puml
hide empty members
skinparam hyperlinkUnderline false
legend bottom
  Click any class or field to jump to the source code.
end legend
class Owner {
  id : Integer
}
@enduml"""


def test_a_footer_with_an_arrow_is_not_a_relationship():
    d = m.parse(DIRECTIVES)
    assert d.relationships == []
    assert list(d.elements) == ["Owner"]


def test_every_directive_reaches_the_delta():
    out = m.diff(m.parse(DIRECTIVES), m.parse(DIRECTIVES))
    for directive in ("footer domain/*.java", "hide empty members",
                      "skinparam hyperlinkUnderline false"):
        assert directive in out


def test_a_legend_survives_body_and_all():
    out = m.diff(m.parse(DIRECTIVES), m.parse(DIRECTIVES))
    assert "legend bottom" in out
    assert "Click any class or field to jump to the source code." in out
    assert "end legend" in out


def test_caption_never_lands_inside_a_style_block():
    """A styled diagram must still render.

    `<style>` ends the preamble on its own line, so a caption appended "after the preamble"
    used to land between `<style>` and its body — which PlantUML rejects, turning the whole
    diagram into a green-on-black "Syntax Error?" image. That image is still a perfectly
    valid .svg, so nothing downstream noticed; only opening the page showed it. Every
    self-styling diagram in the reference project (packages.puml and both C4 views) was
    rendering that way."""
    src = """@startuml
title Styled
skinparam shadowing false
<style>
component {
  FontStyle bold
}
</style>
[A] <<..a>>
[B] <<..b>>
[A] --> [B]
@enduml
"""
    out = m.diff(m.parse(src.replace("[A] --> [B]\n", "")), m.parse(src))
    body = out.splitlines()
    caption_at = next(i for i, ln in enumerate(body) if ln.startswith("caption"))
    assert caption_at < body.index("<style>"), (
        "the caption must not be emitted inside the <style> block:\n" + out)


def test_style_block_survives_the_diff_intact():
    """`<style>` is CSS, and CSS looks exactly like diagram content.

    `component {` parses as an element opening a body, `}` closes it, and `</style>` is
    neither — so the block came out unterminated, with a brace missing, and PlantUML
    rendered the whole diagram as a "Syntax Error?" image. That image is a perfectly valid
    .svg, so nothing downstream could tell; only opening the page showed it."""
    src = """@startuml
title Styled
<style>
component {
  FontStyle bold
  stereotype {
    FontStyle plain
  }
}
</style>
[A] <<..a>>
[B] <<..b>>
[A] --> [B]
@enduml
"""
    out = m.diff(m.parse(src.replace("[A] --> [B]\n", "")), m.parse(src))
    assert "</style>" in out, "the style block was never closed:\n" + out
    assert out.count("{") == out.count("}"), "unbalanced braces:\n" + out
    assert "component" not in out.split("</style>")[1], (
        "the stylesheet leaked into the diagram body as an element:\n" + out)


# ── A lifeline PlantUML cannot name in one word ───────────────────────────────────
#
# A traced sequence diagram declares its lifelines by their human name — `participant
# "Notification module"` — and names them quoted in every arrow. Marking one meant moving
# that name into the display slot and putting it behind `as`, where PlantUML wants a
# single word: `as Notification module` is a syntax error, and PlantUML answers a syntax
# error with a picture of the complaint, not a failure. The pipeline drops that picture,
# so the whole pair on the Sequence tab lost its diagram — one bad word in one line.

def _frame(participants, body):
    return "@startuml\n" + "\n".join(participants) + "\n" + body + "\n@enduml\n"


SPACED = _frame(
    ['participant Backend', 'participant "Notification module"'],
    'Backend -> "Notification module": notify\nactivate "Notification module"',
)
SEQ_BEFORE = _frame(["participant Backend"], "Backend -> Backend: x")


def test_a_multi_word_participant_gets_an_alias_plantuml_can_read():
    out = sq.diff(SEQ_BEFORE, SPACED)
    decl = [l for l in out.splitlines() if l.startswith("participant") and "as " in l]
    assert decl, out
    alias = decl[0].split(" as ")[1].split()[0]
    assert sq.BARE_ALIAS_RE.match(alias), f"PlantUML cannot read {alias!r} as an alias"
    # …and the human name is still what the picture shows
    assert "Notification module</color>" in decl[0]


def test_the_body_follows_the_participant_it_renamed():
    """A declaration renamed and arrows left quoting the old name is two lifelines."""
    out = sq.diff(SEQ_BEFORE, SPACED)
    body = [l for l in out.splitlines()
            if not l.startswith(("participant", "caption", "@"))]
    assert '"Notification module"' not in "\n".join(body), body
    assert any("> Notification_module:" in l for l in body), body
    assert "activate Notification_module" in body


def test_a_one_word_participant_is_left_alone():
    """The rename is a last resort, not a policy: `Backend` stays `Backend`."""
    out = sq.diff(_frame(["participant Backend"], "Backend -> Backend: x"),
                  _frame(["participant Backend", "participant DB"],
                         "Backend -> Backend: x\nBackend -> DB: y"))
    assert "as DB " in out and "DB_2" not in out


def test_an_alias_already_in_the_diagram_is_not_handed_out_twice():
    out = sq.diff(SEQ_BEFORE, _frame(
        ['participant Backend', 'participant Notification_module',
         'participant "Notification module"'],
        'Backend -> "Notification module": notify'))
    aliases = [l.split(" as ")[1].split()[0]
               for l in out.splitlines() if l.startswith("participant") and " as " in l]
    assert len(aliases) == len(set(aliases)), aliases


def test_a_participant_declared_with_a_colour_still_reports_its_alias():
    """`"Long name" as Short #EEE` — the tint used to be read as part of the alias."""
    assert sq._participant_name('"Notification module" as Notif #EAF6EC') == "Notif"
    assert sq._participant_name('"Notification module"') == "Notification module"
    assert sq._participant_name("Backend") == "Backend"
