#!/usr/bin/env python3
"""The frame around the requirements↔tests matrix, which the build owns and the model does not.

`assets/requirements-map.html` is written by a paid run: which test covers which sentence
of the ticket, and how honestly. That is a judgement, and it is the model's. Where the two
columns sit, which key goes under which frame and what is written over the ticket is not a
judgement at all — it is the same answer on every branch — and a layout that came back
subtly different after each run was a page the reader had to learn again.

`hrbuild/tabs/tests.py:reqmap_layout` takes that frame back on every build. This file is
what holds it: that the pieces move where they were asked to move, that the ticket's title
is resolved from GitHub and never read off the model's HTML, that the two frames start on
one line, and — the part worth as much as the rest — that a fragment it does not recognise
comes back byte for byte.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from hrbuild.tabs import tests as T  # noqa: E402 - the path above is what makes it importable


#: The matrix, stripped to the five things the layout moves. Handwritten rather than
#: copied off a real report: the real one is 170KB of base64 avatar and two screens of
#: inlined script, and a fixture nobody can read is a fixture nobody updates.
FRAGMENT = """<div class="reqmap">
  <style>.reqmap{color:#111}</style>
  <script type="application/json" class="rm-data">{"covers":{}}</script>
  <div class="rm-body">
    <div class="rm-text">
      <div class="rm-legend"><span class="rm-lgt">Legend:</span><span class="rm-lg">fully covered</span></div>
      <div class="rm-ticket">
        <div class="rm-tkhead"><img class="rm-av" src="x"><span class="rm-who">victorrentea</span><span class="rm-when">opened on Jun 13, 2026</span></div>
        <div class="rm-issue"><p>The visit should name its vet.</p></div>
      </div>
      <div class="rm-gap" hidden></div>
    </div>
    <div class="rm-side">
      <p class="rm-cats"><span><span class="rm-cat" data-cat="e2e">UI</span>clicks the screen</span></p>
      <aside class="rm-code">
        <div class="rm-tkhead"><span class="rm-av rm-av-ai">\U0001f916</span><span class="rm-who">Covering tests</span><span class="rm-when">as matched by AI</span></div>
        <div class="rm-list"></div>
      </aside>
    </div>
  </div>
  <script>var s='<div class="rm-legend">decoy</div>';</script>
</div>"""

SPEC = {"pr": {"number": 49, "title": "Link Visit with Vet (#37), reimplemented by Opus",
               "repo": "https://github.com/victorrentea/petclinic",
               "ticket": {"number": 37, "title": "Link Visit with Vet",
                          "url": "https://github.com/victorrentea/petclinic/issues/37"}}}


def _laid_out(tmp_path, spec=None, frag=FRAGMENT):
    return T.reqmap_layout(frag, spec if spec is not None else SPEC, tmp_path)


def _order(html: str, *pieces: str) -> list[int]:
    """Where each piece sits in the document, so a test can say 'this one is below that'."""
    at = []
    for p in pieces:
        i = html.find(p)
        assert i != -1, f"{p!r} is not on the page at all"
        at.append(i)
    return at


# --- what moves ------------------------------------------------------------------------

def test_the_colour_legend_moves_under_the_ticket_it_explains(tmp_path):
    """A key is read once, and read *after* meeting a colour you cannot name.

    Over the frame it was five coloured words a reader met before they had seen a single
    covered sentence — a definition of terms nobody had needed yet."""
    out = _laid_out(tmp_path)
    ticket, legend = _order(out, 'class="rm-ticket"', 'class="rm-legend"')
    assert ticket < legend
    # …and still inside the ticket's own column, not stranded at the foot of the page.
    col = out[out.index('class="rm-text"'):out.index('class="rm-side"')]
    assert 'class="rm-legend"' in col


def test_the_surface_key_moves_under_the_card_it_explains(tmp_path):
    out = _laid_out(tmp_path)
    card, cats = _order(out, 'class="rm-code"', 'class="rm-cats"')
    assert card < cats
    assert 'class="rm-cats"' in out[out.index('class="rm-side"'):]


def test_the_ticket_title_is_a_link_over_the_ticket(tmp_path):
    """The one thing the page never said. The masthead carries the PR's title, which on
    this branch is not the issue's, and the frame opened straight into `opened on Jun 13,
    2026` with nothing saying what was opened."""
    out = _laid_out(tmp_path)
    assert ('<a class="rm-title" href="https://github.com/victorrentea/petclinic/issues/37">'
            'Link Visit with Vet <span class="rm-num">#37</span></a>') in out
    head, ticket = _order(out, 'class="rm-head"', 'class="rm-ticket"')
    assert head < ticket


def test_the_title_is_a_row_of_the_grid_and_not_a_child_of_the_left_column(tmp_path):
    """This is the whole of the alignment, so it is pinned as structure and not as a look.

    In the left column the title would push that column down and leave the card level with
    nothing; as a row of `.rm-body` above both, the row under it starts the two frames
    together — at any width, with no measured constant to keep in step."""
    out = _laid_out(tmp_path)
    body = out.index('<div class="rm-body">')
    head = out.index('class="rm-head"')
    text = out.index('class="rm-text"')
    assert body < head < text
    css = out[out.rindex("<style>"):]
    assert "grid-template-columns:1fr 50%" in css
    assert ".reqmap .rm-head{grid-column:1;grid-row:1" in css
    assert ".reqmap .rm-text{grid-column:1;grid-row:2}" in css
    assert "grid-row:2" in css[css.index(".reqmap .rm-side{"):]


def test_the_cards_own_header_strip_stays_on_the_card(tmp_path):
    """Lifted out to pair with the ticket's title it *looked* symmetrical and was not: it
    left the right-hand frame bare-topped while the left kept its strip, and stacked on a
    narrow window it stranded the byline a screen above the list it belongs to."""
    out = _laid_out(tmp_path)
    card = out[out.index('<aside class="rm-code">'):out.index("</aside>")]
    assert 'class="rm-tkhead"' in card
    assert "Covering tests" in card and "as matched by AI" in card
    assert out.count("Covering tests") == 1


def test_the_side_column_keeps_its_full_width_in_the_grid(tmp_path):
    """`max-width:50%` was half of the flex layout's `flex:0 0 50%`. Against a 50% grid
    track it is read a second time and halves the card."""
    out = _laid_out(tmp_path)
    assert "max-width:none" in out[out.rindex("<style>"):]


def test_the_gutter_does_not_become_the_gap_under_the_title(tmp_path):
    """`gap:46px` is a gutter between two columns. Inherited downwards by the grid it put
    half a screen between the title and the ticket it names."""
    out = _laid_out(tmp_path)
    assert "row-gap:0" in out[out.rindex("<style>"):]


# --- where the title comes from ----------------------------------------------------------

def test_the_title_is_never_read_off_the_model_written_fragment(tmp_path):
    """The matrix is regenerated by a paid run. A heading whose wording changed between
    two runs of the same branch would be the page disagreeing with GitHub about what the
    ticket is called — so the fragment is not a source, and a fragment that carries no
    title is still titled."""
    out = _laid_out(tmp_path)
    assert "Link Visit with Vet" not in FRAGMENT
    assert "Link Visit with Vet" in out


def test_the_ticket_number_is_read_off_the_pr_title_when_nothing_declares_it(tmp_path):
    """`Link Visit with Vet (#37), …` names its issue. `#49` there would be the PR quoting
    itself, which is why the PR's own number is not a candidate."""
    (tmp_path / T.TICKET_CACHE).write_text(
        json.dumps({"number": 37, "title": "Link Visit with Vet",
                    "url": "https://github.com/victorrentea/petclinic/issues/37"}),
        encoding="utf-8")
    spec = {"pr": {"number": 49, "title": "Link Visit with Vet (#49) closes (#37)",
                   "repo": "https://github.com/victorrentea/petclinic"}}
    assert T.ticket_ref(spec, tmp_path)["number"] == 37


def test_the_resolved_ticket_is_written_down_so_the_next_build_needs_no_network(monkeypatch,
                                                                               tmp_path):
    """`gh` is not on every machine that rebuilds this page, and none of them should have
    to buy the same answer twice. Asked once, cached, read from disk ever after."""
    calls = []

    class _Done:
        stdout = json.dumps({"number": 37, "title": "Link Visit with Vet",
                             "url": "https://github.com/victorrentea/petclinic/issues/37"})

    def fake_run(args, **kw):
        calls.append(args)
        return _Done()

    monkeypatch.setattr(T.subprocess, "run", fake_run)
    spec = {"pr": {"number": 49, "title": "Link Visit with Vet (#37)",
                   "repo": "https://github.com/victorrentea/petclinic"}}
    first = T.ticket_ref(spec, tmp_path)
    assert first["title"] == "Link Visit with Vet"
    assert (tmp_path / T.TICKET_CACHE).is_file()
    assert calls and calls[0][:4] == ["gh", "issue", "view", "37"]
    assert T.ticket_ref(spec, tmp_path) == first
    assert len(calls) == 1, "the second build asked GitHub again"


def test_a_ticket_nobody_can_resolve_costs_a_heading_and_not_the_tab(monkeypatch, tmp_path):
    """No `gh`, no token, no network — a reader still gets the matrix. Refusing the build
    over a heading would cost them the whole tab."""
    def boom(*a, **kw):
        raise OSError("gh: not found")

    monkeypatch.setattr(T.subprocess, "run", boom)
    spec = {"pr": {"number": 49, "title": "Link Visit with Vet (#37)"}}
    assert T.ticket_ref(spec, tmp_path) is None
    out = _laid_out(tmp_path, spec)
    assert 'class="rm-head"' not in out
    # …and the columns are still laid out, which is what keeps them level.
    assert "grid-template-columns:1fr 50%" in out


def test_a_pr_that_names_no_ticket_asks_nobody_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(T.subprocess, "run",
                        lambda *a, **kw: pytest.fail("asked GitHub with no number to ask about"))
    assert T.ticket_ref({"pr": {"number": 49, "title": "Tidy the vet list"}}, tmp_path) is None


# --- what it refuses to do ---------------------------------------------------------------

def test_a_fragment_that_is_not_the_matrix_comes_back_untouched(tmp_path):
    """Every `includeHtml` on the page goes through here — the contract diff, the schema
    tree, the complexity delta, the design-system audit. Only the matrix is recognised."""
    other = '<div class="oaverdict"><p class="rm-legend">a coincidence</p></div>'
    assert T.reqmap_layout(other, SPEC, tmp_path) == other


def test_a_redesigned_fragment_keeps_the_layout_the_model_shipped(tmp_path, capsys):
    """Each piece is looked up by name, and a piece that is not there aborts the whole
    rewrite rather than emitting half of it. The honest failure is the model's own layout
    with a line on stderr, not a column with its heading gone."""
    without_cats = FRAGMENT.replace('class="rm-cats"', 'class="rm-surfaces"')
    assert T.reqmap_layout(without_cats, SPEC, tmp_path) == without_cats
    assert ".rm-cats" in capsys.readouterr().err


def test_the_decoy_markup_inside_the_scripts_is_not_what_gets_moved(tmp_path):
    """The fragment carries two screens of JavaScript that builds rows out of strings, and
    some of those strings are elements with classes on them. The rewrite works inside
    `.rm-body` and nowhere else."""
    out = _laid_out(tmp_path)
    assert """var s='<div class="rm-legend">decoy</div>';""" in out


def test_the_stylesheet_lands_once_and_after_the_models_own(tmp_path):
    """Same specificity, so the later one wins — and only the later one may."""
    out = _laid_out(tmp_path)
    assert out.count(".reqmap .rm-body{display:grid") == 1
    assert out.rindex("<style>") > out.index("</div>")


# --- the shape of the whole thing ---------------------------------------------------------

def test_nothing_of_the_ticket_or_the_test_list_is_lost_in_the_move(tmp_path):
    """The rewrite reassembles `.rm-body` from its two columns. Everything that was in
    them has to still be in them."""
    out = _laid_out(tmp_path)
    for kept in ("The visit should name its vet.", "opened on Jun 13, 2026",
                 'class="rm-issue"', 'class="rm-gap"', 'class="rm-list"',
                 'class="rm-data"', "clicks the screen", "fully covered"):
        assert kept in out, kept
