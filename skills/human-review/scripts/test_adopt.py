"""The "Prompt to get this" buttons: one per piece — inside the card it is about, or at the
right end of the tab's title row when the piece is the whole tab."""
import html
import json
import re
from pathlib import Path

import pytest

from hrbuild.shared.adopt import (PIECES, PLACES, _close, adopt_html, adopt_prompt,
                                  place_prompts)
from hrbuild.shared.layout import LAYOUT_TABS
from hrbuild.tabs.cost import COST_TAB_ID

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent


def _prompts(out):
    return [html.unescape(p) for p in re.findall(r'data-copy="([^"]*)"', out)]


def test_every_tab_the_build_knows_has_a_place_for_its_buttons():
    assert set(LAYOUT_TABS) | {"review", COST_TAB_ID} <= set(PLACES)


@pytest.mark.parametrize("piece", sorted(PIECES))
def test_every_file_a_prompt_points_at_is_in_the_skill(piece):
    for start in PIECES[piece][1].split(", "):
        if not start.startswith("http"):
            assert (SKILL / start).exists(), start


@pytest.mark.parametrize("anchor", sorted({a for p in PLACES.values() for _, _, a, _ in p
                                            if a}))
def test_every_class_a_button_anchors_on_is_still_emitted_somewhere(anchor):
    """The producers do not know about the button, so a renamed class would silently move
    it to the foot of the panel. This is where that rename shows up."""
    cls = re.search(r'(class|id)="([\w-]+)', anchor).group(2)
    sources = [p.read_text(encoding="utf-8") for p in SCRIPTS.rglob("*.py")
               if not p.name.startswith("test_")]
    assert any(f'"{cls}' in s or f' {cls}"' in s or f"'{cls}" in s for s in sources), cls


def test_a_piece_without_a_prompt_gets_no_button():
    assert adopt_prompt("nope") is None
    assert adopt_html("nope") == ""
    assert place_prompts("nope", "<p>x</p>") == "<p>x</p>"


def test_the_button_says_prompt_to_get_this_and_copies_the_prompt_escaped():
    out = adopt_html("api")
    assert 'class="adopt copycmd"' in out and "\U0001F916 Prompt to get this</button>" in out
    assert "Adopt" not in out
    assert _prompts(out)[0].startswith("Get the REST contract")


def _card(title, src, inner=""):
    return (f'<div class="diagram"><div class="head"><b>{title}</b>'
            f'<span>{src}</span></div><div class="svgbox"><svg></svg></div>{inner}</div>')


def test_a_diagram_cards_i_sits_beside_its_pill_not_in_its_header():
    # Victor, 14 Oct 2026: the (i) goes next to the button it explains, on every card.
    card = ('<div class="diagram"><div class="head"><b>Domain Model</b>'
            '<a class="dgm-src" href="x">DomainModel.puml</a></div><div class="svgbox"></div></div>')
    out = place_prompts("data", card)
    head = out[out.index('<div class="head">'):out.index('<div class="svgbox">')]
    assert "hrx-i" not in head
    foot = out[out.index('<div class="adoptline'):]
    assert re.search(r'Prompt to get this</button><button type="button" class="hrx-i"', foot)


def test_each_data_card_gets_its_own_button_inside_its_border_with_its_own_prompt():
    body = (_card("Domain Model", "DomainModel.puml")
            + _card("DB", "DB.puml", '<p class="sub dgm-stale">Also changed</p>')
            + _card("Conceptual Model", "ConceptualModel.drawio.png"))
    out = place_prompts("data", body)
    cards = out.split('<div class="diagram">')[1:]
    assert len(cards) == 3
    for card in cards:
        assert card.count("Prompt to get this") == 1
        assert card.rstrip().endswith("</button></div></div>") or card.rstrip().endswith(
            "</button></div></div></div>"), "the button closes the card"
    # A card that ends on a caption line gives that line the pill, not a row under it.
    assert '<div class="adoptfoot"><p class="sub dgm-stale">Also changed</p>' \
           '<div class="adoptline adopt-title">' in cards[1]
    assert "adoptfoot" not in cards[0] + cards[2]
    first = [p.split(" into this repository")[0] for p in _prompts(out)]
    assert first == [
        "Get the domain-model class diagram generated from your domain classes by Java "
        "reflection, committed as PlantUML and diffed base against branch",
        "Get the ERD generated from your DB migration scripts, committed as PlantUML and "
        "diffed base against branch",
        "Get the hand-drawn draw.io Conceptual Model diagram, checked against the code by a "
        "test and diffed base against branch"]


def test_a_drawio_card_whose_head_holds_its_action_buttons_is_still_a_drawio_piece():
    """16fe638 moved a card's action buttons into its head, before the file name. Read up
    to the first `</div>`, the head ended at the buttons, the `.drawio` file name was never
    seen, and the Deployment card got the PlantUML prompt and the PlantUML (i) — found by
    tonight's from-scratch rebuild against the hand-polished page."""
    head_buttons = ('<div class="rerun-acts"><a class="dgm-edit" href="#">Edit</a></div>')
    body = ('<div class="diagram"><div class="head"><b>Deployment</b>' + head_buttons
            + '<a class="dgm-src">Deployment.drawio.png</a></div>'
            '<div class="svgbox"><svg></svg></div></div>')
    out = place_prompts("packages", body)
    assert 'data-piece="diagram.drawio' in out, out


def test_a_diagram_inside_a_test_pair_is_part_of_the_pair_not_a_piece_of_its_own():
    body = '<div class="diagram dgm-bare"><div class="head"></div></div>'
    assert place_prompts("packages", body) == body


def test_the_demo_button_sits_under_the_transcript_in_a_column_of_its_own():
    body = ('<div class="vidwrap"><div class="vidcol"><video></video></div>'
            '<ol class="transcript"><li>cue</li></ol></div>')
    out = place_prompts("behaviour", body)
    assert re.search(r'<div class="adoptcol"><ol class="transcript">.*?</ol>'
                     r'<div class="adoptline adopt-after">.*?</div></div></div>$', out)


def test_the_whole_review_rides_the_sticky_counts_line_at_its_right_end():
    """Victor, 20 Oct 2026: the page's pill on the line that counts assumptions, open and
    fixed, on its right. Inside the line and not around it — the line is sticky, and a
    wrapper would be the box it sticks in — so it is a span, and the (i) beside it opens
    the workflow drawing under the line."""
    line = '<p class="sub counts pilelede"><a href="#assumed">6 assumptions</a></p>'
    out = place_prompts("review", line + '<p class="pileround">Round I</p><h2 id="assumed">A</h2>')
    assert out.count("Prompt to get this") == 1, "one pill for the review, none per pile"
    assert out.startswith('<p class="sub counts pilelede" data-adoptrow><a href="#assumed">'
                          '6 assumptions</a><span class="adoptline adopt-row">')
    row = out[:out.index("</p>")]
    assert "Prompt to get this page</button>" in row and 'data-explain="review"' in row
    assert row.endswith("</button></span>")
    assert _prompts(row)[0].startswith("Get the whole review workflow")
    assert "<svg" in EXPLAIN["review"]["img"] and "record-review" in EXPLAIN["review"]["img"]


def test_the_tests_button_shares_the_filter_cell_on_the_title_row():
    """The panes fill the window, so the button used to float over their corner; now it
    sits on the title row, after the UI/API/unit filter, in the cell over the card."""
    body = ('<div class="reqmap"><div class="rm-body"><h2 class="tabtitle rm-head">Issue</h2>'
            '<div class="rm-text"></div><p class="rm-cats"><label class="rm-catf">UI</label>'
            '</p><div class="rm-side"><aside class="rm-code"><div>x</div></aside></div>'
            '</div></div>')
    out = place_prompts("requirements", body)
    assert re.search(r'<div class="adopthead"><p class="rm-cats">.*?</p>'
                     r'<div class="adoptline adopt-title">.*?</button></div></div>'
                     r'<div class="rm-side">', out)
    assert out.count("Prompt to get this") == 1


def test_a_script_inside_a_card_cannot_close_it_early():
    body = ('<div class="diagram"><script>var s = "</div><div>";</script>'
            '<div class="head"><b>Domain Model</b></div></div><p>after</p>')
    out = place_prompts("data", body)
    assert out.endswith('</button></div></div><p>after</p>')
    assert _close(body, 0) == (len(body) - len("</div><p>after</p>"),
                               len(body) - len("<p>after</p>"))


def test_a_tab_whose_anchor_moved_still_offers_its_button_at_the_end():
    out = place_prompts("requirements", "<p>renamed markup</p>")
    assert out.startswith("<p>renamed markup</p>") and out.count("Prompt to get this") == 1


#: The tabs whose prompt is about the whole tab, not one card of it (5 Oct 2026, Victor),
#: each with the first row its button joins: the tab's title wherever it has one.
WHOLE_TAB = {"sequence": '<h2 class="tabtitle" id="sequences">Sequence diagrams</h2>',
             "city": '<h2 class="tabtitle" id="codecity">Impact on code size</h2>',
             "complexity": '<h2 class="tabtitle cx-title"><a href="#">Cognitive</a> per</h2>',
             "logging": '<h2 class="tabtitle" id="logging-added">Uses of logging</h2>',
             # Victor, 7 Oct 2026: the cost tab got a title, and its pill left the table.
             "cost": '<h2 class="tabtitle">PR Cost</h2>',
             "owners": '<h2 class="tabtitle cow-title">Needs approval as per</h2>',
             "api": '<div class="apiverdict red"><span class="dot"></span>Breaking</div>',
             "dsaudit": '<h2 class="tabtitle">UX design system</h2>'}


@pytest.mark.parametrize("tid", sorted(WHOLE_TAB))
def test_a_whole_tab_prompt_sits_at_the_right_end_of_the_tabs_title_row(tid):
    """If the tab holds one consistent piece of knowledge, one generation, its prompt goes on
    the right side of that tab's title (Victor, 5 Oct 2026): the first row and the button
    become one flex row, outside every card — never the first card, never the last."""
    first = WHOLE_TAB[tid]
    rest = ('<p class="tabsub">from where</p>'
            '<div class="dsa"><details>Book A Visit</details></div>'
            '<div class="cx-group"><div class="cx-list">JOBS</div></div>'
            '<div class="cow"><div class="cow-row">approval</div></div>'
            '<script>var s = "</div>";</script>')
    out = place_prompts(tid, '<p class="paneltag">X</p>' + first + rest)
    assert out.count("Prompt to get this") == 1
    head = re.escape('<p class="paneltag">X</p><div class="adopthead">' + first)
    assert re.match(head + r'<div class="adoptline adopt-title">.*?</button></div></div>'
                    + re.escape(rest) + "$", out, re.S)
    assert all(how == "title" for _, how, _, _ in PLACES[tid])


def test_only_the_first_title_takes_the_button():
    body = ('<h2 class="tabtitle">One</h2><p>x</p><h2 class="tabtitle">Two</h2>')
    out = place_prompts("logging", body)
    assert out.startswith('<div class="adopthead"><h2 class="tabtitle">One</h2>')
    assert out.count("Prompt to get this") == 1


def test_a_whole_tab_without_its_title_row_still_closes_the_panel_with_its_button():
    out = place_prompts("city", '<a class="city"><img></a>')
    assert out.startswith('<a class="city"><img></a><div class="adoptline adopt-end">')


def test_every_tab_is_either_per_card_or_whole_tab_or_one_of_the_agreed_exceptions():
    per_card = {"data", "packages"}
    # The Tests filter row, the Demo's transcript column, the Review tab's sticky counts line.
    agreed = {"requirements": "title", "behaviour": "col", "review": "row"}
    assert set(PLACES) == per_card | set(WHOLE_TAB) | set(agreed)
    for tid, how in agreed.items():
        assert [h for _, h, _, _ in PLACES[tid]] == [how]


def test_tab_title_pills_say_page_and_smaller_areas_keep_the_short_label():
    """Victor, 7 Oct 2026: a pill on a whole tab's title row reads "…this page"; the ones
    on a diagram card or a part of a screen stay "…this"."""
    tab = place_prompts("city", '<h2 class="tabtitle">City</h2><p>x</p>')
    assert "Prompt to get this page</button>" in tab
    api = place_prompts("api", '<div class="apiverdict ok">v</div>')
    assert "Prompt to get this page</button>" in api
    card = place_prompts("data", _card("Domain Model", "DomainModel.puml",
                                       '<p class="sub dgm-stale">Also changed</p>'))
    assert "adopt-title" in card and "Prompt to get this</button>" in card
    assert "this page" not in card


# The (i) beside every pill (7 Oct 2026, Victor): what the reader is looking at, read
# before they copy a prompt for it.
from hrbuild.shared.adopt import EXPLAIN, explain_data, explain_key  # noqa: E402

ASSETS = SCRIPTS / "hrbuild" / "assets"


@pytest.mark.parametrize("piece", sorted(PIECES))
def test_every_piece_with_a_prompt_explains_itself(piece):
    assert explain_key(piece), f"{piece} has a prompt but no EXPLAIN entry"


@pytest.mark.parametrize("key", sorted(EXPLAIN))
def test_each_explainer_is_a_few_one_sentence_bullets_and_names_a_real_piece(key):
    """Victor, 7 Oct 2026: "be brief in the details you give in the (i) modals"."""
    t = EXPLAIN[key]
    assert key.split(":")[0] in PIECES
    assert 1 <= len(t["li"]) <= 6, "ADHD-friendly: a few bullets"
    for line in t["li"]:
        plain = re.sub(r"<[^>]+>", "", line)
        assert len(plain) <= 90, line
        assert not re.search(r"[.!?]\s+[A-Z$]", plain), f"one sentence per bullet: {line}"
    assert set(t) <= {"li", "ex", "code", "move", "img"}
    assert not ("ex" in t and "code" in t), "at most one tiny example"
    if "ex" in t:
        assert t["ex"].startswith("e.g."), "an example is marked as one: the texts are static"
        assert len(re.sub(r"<[^>]+>", "", t["ex"])) <= 90, t["ex"]
    if "code" in t:
        assert len(t["code"].split("\n")) <= 3, "a tiny snippet, not a listing"


def test_the_i_sits_right_after_the_pill_inside_its_row():
    row = adopt_html("diagram.domain", "Domain Model")
    assert re.search(r'Prompt to get this</button><button type="button" class="hrx-i" '
                     r'aria-pressed="false" data-explain="diagram\.domain"', row)
    assert row.endswith("</button></div>")


def test_a_card_title_picks_its_own_explainer_when_one_piece_covers_unlike_cards():
    assert 'data-explain="diagram.drawio:Deployment"' in adopt_html("diagram.drawio", "Deployment")
    assert 'data-explain="diagram.drawio"' in adopt_html("diagram.drawio", "Conceptual Model")


def test_every_placed_pill_gets_an_i():
    out = place_prompts("data", _card("Domain Model", "DomainModel.puml")
                        + _card("Database", "DB.puml"))
    assert out.count('class="adopt copycmd"') == out.count('class="hrx-i"') == 2


def test_the_texts_ride_as_json_that_no_text_can_close():
    block = explain_data()
    body = block[block.index(">") + 1:block.rindex("</script>")]
    assert "</" not in body
    assert json.loads(body) == EXPLAIN


def test_the_moved_intro_lines_still_exist_on_their_tabs():
    """A `move` selector whose line is gone leaves the box short, silently."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in SCRIPTS.rglob("*.py")
                        if not p.name.startswith("test_"))
    for key, t in EXPLAIN.items():
        if "move" in t:
            cls = t["move"].split(".")[-1]
            assert cls in sources, f"{key}: nothing emits .{cls} any more"


def test_the_explainer_script_reads_the_block_and_is_on_the_page():
    js = (ASSETS / "explain.js").read_text(encoding="utf-8")
    assert "getElementById('hr-explain')" in js and "button.hrx-i[data-explain]" in js
    assert "localStorage" in js and "try" in js
    page = (SCRIPTS / "build-review-html.py").read_text(encoding="utf-8")
    assert "{explain_data()}\n{EXPLAIN_JS}" in page


def test_the_pill_burst_is_a_square_so_no_angle_leaves_a_corner_bare():
    """It was 260% x 700% of the pill: at 90 degrees narrower than the pill, so the hover
    tint showed through at its ends (a black wedge in dark mode, 7 Oct 2026)."""
    css = (ASSETS / "css" / "adopt.css").read_text(encoding="utf-8")
    before = re.search(r"\.adoptline \.adopt::before\{([^}]*)\}", css).group(1)
    assert "aspect-ratio:1/1" in before and "height:700%" not in before
    hover = re.search(r"\.adoptline \.adopt:hover,[^{]*\{([^}]*)\}", css).group(1)
    assert "background:#7c3aed" in hover


def test_a_card_with_green_fill_bars_explains_them_left_of_its_pill():
    # Victor, 18 Oct 2026: "it's not clear what the progress bar means".
    bar = '<svg><rect class="pkg-impact" x="1" y="1" width="5" height="9"/></svg>'
    out = place_prompts("packages", _card("Java packages", "packages.puml", bar))
    foot = out[out.index('<div class="adoptline'):]
    assert foot.index('class="impact-legend"') < foot.index("Prompt to get this")
    assert "share of the lines this PR added" in foot
    plain = place_prompts("packages", _card("Java packages", "packages.puml"))
    assert "impact-legend" not in plain


def test_show_only_impacted_moves_from_the_head_to_the_bottom_row_left_of_the_pill():
    # Victor, 29 Oct 2026: the filter sits just left of "Prompt to get this", the blue
    # fill's legend at the left end of that row; the head keeps the title and the badges.
    from hrbuild.shared.folders import ONLY_IMPACTED
    bar = '<svg><rect class="pkg-impact" x="1" y="1" width="5" height="9"/></svg>'
    card = _card("Java packages", "packages.puml", bar).replace(
        "<span>packages.puml</span>", ONLY_IMPACTED + "<span>packages.puml</span>")
    out = place_prompts("packages", card)
    head = out[out.index('<div class="head">'):out.index('<div class="svgbox">')]
    assert "dgm-only" not in head and "packages.puml" in head
    foot = out[out.index('<div class="adoptline'):]
    assert out.count('class="dgm-only"') == 1
    assert (foot.index('class="impact-legend"') < foot.index('class="dgm-only"')
            < foot.index("Prompt to get this"))
    assert ">Show only impacted</label>" in foot
