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


def test_the_title_row_over_the_card_offers_all_tests_where_the_filters_stood(tmp_path):
    """Victor, 7 Oct 2026: the card is split into E2E/API/UNIT chapters, so the three
    filter checkboxes went; the row over the card keeps its place. Victor, 20 Oct 2026: the
    card's title is the choice, "Tests: [covering the change set]" by default, then
    "running the change set" and "all"."""
    out = _laid_out(tmp_path)
    head, cats, side = _order(out, 'class="tabtitle rm-head"', 'class="rm-cats"',
                              'class="rm-side"')
    assert head < cats < side
    js = T.REQMAP_CHAPTERS_JS
    # Victor, 29 Oct 2026: "tests" inside each option, no "Tests:" label, and a short
    # line beside the choice saying what it means.
    assert "var MODES = [['cover', 'Tests covering \\uD83E\\uDD16 the change'," in js
    assert "['run', 'Tests running through the change'," in js
    assert "['all', 'All tests'," in js
    assert "who.hidden = true;" in js and "whyEl.textContent = m[3];" in js
    assert ".rm-tkhead > .rm-mode-why{" in T.REQMAP_CSS
    assert "return mode === 'cover' ? paired(row) : !folded(row);" in js
    assert "sec.hidden = !n;" in js                       # an emptied chapter goes
    assert ".reqmap .rm-list .rm-t[data-off=yes]{display:none}" in T.REQMAP_CSS
    assert "Show all tests" not in js
    assert '<label class="rm-catf"' not in out and "data-catoff=yes" not in out
    assert T.REQMAP_CHAPTERS_JS in out


def test_the_title_row_keeps_its_paragraph_and_drops_the_chips():
    cats = T.all_tests_toggle('<p class="rm-cats"><span><span class="rm-cat" data-cat="e2e">UI'
                              '</span>clicks the screen</span></p>')
    assert cats == '<p class="rm-cats"></p>'


def test_the_card_is_read_in_chapters_by_kind_not_by_sentence():
    """The pairing to ticket sentences moved between runs and the card's shape with it
    (Victor, 7 Oct 2026). The chapters are the badge's three kinds, closed by default with
    their own counts on the heading (a summary by kind), and a sentence click opens the
    chapters holding the tests it lights."""
    js = T.REQMAP_CHAPTERS_JS
    for words in ("'end-to-end, from the browser'", "'calling the API directly'"):
        assert words in js
    assert "var OPEN = {};" in js
    assert "el.classList.contains('rm-tgroup')" in js             # sentence headings go
    assert "list.querySelectorAll('.rm-t[data-hit=yes]')" in js   # the selection follows
    # The hooks other decorators (the fixture dot) find rows by.
    for hook in ("row.dataset.cat", "row.dataset.file", "row.dataset.testName",
                 'data-test-name="', 'data-file="'):
        assert hook in js
    css = T.REQMAP_CSS
    assert ".reqmap .rm-list > .rm-tgroup,.reqmap .rm-list > .rm-fold{display:none}" in css
    assert ".reqmap .rm-ch[data-open=no] > .rm-chb{display:none}" in css
    assert ".reqmap .rm-list:not([data-all=yes]) .rm-cho{display:none}" in css


def test_a_chapter_heading_is_caret_badge_subtitle_then_its_own_counts():
    """Victor, 7 Oct 2026: the caret leads, in the rows' wire column; the heading has a
    tint of its own; the card title's +added / -deleted / edited counts, per chapter, in
    the same right-hand column, counted off the stamps the rows wear. Victor, 19 Oct 2026:
    no blank arrow between the badge and the subtitle ("remove this space")."""
    js, css = T.REQMAP_CHAPTERS_JS, T.REQMAP_CSS
    order = [js.index(x) for x in ('rm-chev rm-chcaret', '<span class="rm-cat" data-cat="',
                                   'rm-chsub', '<span class="rm-chhit"', 'rm-chled')]
    assert order == sorted(order)
    assert "rm-chpad" not in js and "rm-chpad" not in css
    assert "sec.querySelectorAll('.rm-t .rm-st[data-st]')" in js
    for cls in ('class="added"', 'class="removed"', 'class="changed"'):
        assert cls in js
    assert "linear-gradient(rgba(127,127,127,.12),rgba(127,127,127,.12)),var(--card,#fff)" in css
    assert ".reqmap .rm-chh{display:flex;align-items:center;gap:8px;padding:7px 16px 7px 6px;" in css
    # The card's title strip (and the ticket's, to stay level) a fifth shorter.
    assert (".reqmap .rm-ticket > .rm-tkhead,.reqmap .rm-code > .rm-tkhead"
            "{padding-top:4.5px;padding-bottom:4.5px}") in css


def test_the_chapter_heading_sticks_to_the_top_of_the_list_and_is_opaque():
    """Victor, 19 Oct 2026: "this header should stay fixed as I scroll down, so I always
    know what category I'm looking at". Sticky inside the list (the scroller), over the
    card's own colour so the rows do not show through; a pick scrolls its first test to
    below the stuck heading, not behind it."""
    css = T.REQMAP_CSS
    assert ".reqmap .rm-chh{position:sticky;top:0;z-index:2}" in css
    js = T.REQMAP_CHAPTERS_JS
    assert "hh = sec ? sec.querySelector('.rm-chh').offsetHeight : 0" in js
    rm = (HERE / "reqmap" / "reqmap.js").read_text(encoding="utf-8")
    assert "pad=hh?hh.offsetHeight:0" in rm


def test_a_row_names_its_kind_by_colour_not_by_a_badge():
    """Victor, 19 Oct 2026: the per-row E2E/API/UNIT pill goes; the name wears the kind's
    colour. The pill stays in the markup, hidden, because the pick still writes how hard a
    test pins the sentence on it - and the name shows that now."""
    css = T.REQMAP_CSS
    assert ".reqmap .rm-t .rm-thead > .rm-cat{display:none}" in css
    for cat, ink in (("e2e", "--rm-e2e-ink"), ("api", "--rm-api-fg"), ("unit", "--rm-unit-fg")):
        assert f".reqmap .rm-t[data-cat={cat}] .rm-tt{{color:var({ink})}}" in css
    assert ".rm-thead:has(> .rm-cat[data-strength=exercised]) .rm-tt" in css
    # The chapter heading keeps its badge.
    assert ".reqmap .rm-chh .rm-cat{" in css


def test_the_fixture_dot_slides_into_the_file_columns_blank():
    """Victor, 19 Oct 2026: the DB-fixture dot further right, to leave the name more room -
    into the blank a `.java` link leaves in its `.feature`-wide column, never over a longer
    file name the clash rule wrote there."""
    css, js = T.REQMAP_CSS, T.REQMAP_CHAPTERS_JS
    assert ('.reqmap .rm-thead > .fx-dot:has(+ .rm-tw[data-face=".java"]:not([data-clash])){'
            '\n  margin-right:-21px;') in css
    assert "if (clash) tw.dataset.clash = 'yes'; else delete tw.dataset.clash;" in js


def test_the_title_row_carries_the_grand_total():
    """Victor, 19 Oct 2026: the masthead's `tests +20` was the chapters' `+11` and `+9`
    and nothing added them up. The title row ends in the chapters' four columns, summed."""
    js, css = T.REQMAP_CHAPTERS_JS, T.REQMAP_CSS
    assert "grand.className = 'rm-chled rm-grand';" in js
    assert "head.insertBefore(grand, head.querySelector('.tledger'));" in js
    assert "grand.innerHTML = led(sum, total, '', 'listed, all kinds');" in js
    assert ".reqmap .rm-code > .rm-tkhead > .rm-grand{flex:0 0 auto;margin-left:auto;" in css
    # One row: the totals never wrap under the title, the title is cut instead.
    assert ".reqmap .rm-code > .rm-tkhead:not(:has(> .tledger .toff)){flex-wrap:nowrap}" in css
    # The ledger's own counts under it would say the same numbers twice.
    assert ".reqmap .rm-code > .rm-tkhead:has(> .rm-grand) > .tledger .tmoves{display:none}" in css


def test_a_picked_sentence_counts_its_tests_on_every_chapter_heading():
    """"I'd want to know that there are no unit tests covering this": while a sentence is
    picked each heading says how many of its tests cover it, `none` in red; it follows the
    list's `data-sel`, which every way out of a pick takes off."""
    js, css = T.REQMAP_CHAPTERS_JS, T.REQMAP_CSS
    assert "observe(list, {attributes: true, attributeFilter: ['data-sel']})" in js
    assert "mark.innerHTML = n ? FAN + n : 'none';" in js
    assert '.reqmap .rm-ch[data-hits="0"] .rm-chhit{color:var(--rm-r-edge)}' in css


def test_a_picked_sentence_is_wired_to_every_test_that_covers_it():
    """The first direction drawn too: one wire per covering test, whichever chapter; into
    the heading of a closed chapter; onto the card's edge, counted, when scrolled away."""
    rm = (HERE / "reqmap" / "reqmap.js").read_text(encoding="utf-8")
    assert "if(!row&&cur&&list.dataset.sel==='yes'){drawSel();return;}" in rm
    assert "if(!sec||sec.dataset.open==='yes'||!head)return;" in rm   # folded: nowhere
    assert "if(up)d+=edge(up,lo+9,'\\u25B2');" in rm
    assert "if(down)d+=edge(down,hi-9,'\\u25BC');" in rm
    # Taking the pick back takes the wires with it.
    assert "rejMark(null);redraw();" in rm
    assert ".reqmap .rm-wires .rm-edge rect{" in T.REQMAP_CSS


def test_the_panes_scrollbars_take_no_width():
    """Victor, 19 Oct 2026: the scrollbar must not push the pane left - hidden until the
    pane is pointed at or scrolled, then semi-transparent. The native bar is hidden and
    rmscroll.js draws a thumb over the content, on both panes."""
    css = T.REQMAP_CSS
    assert ".reqmap .rm-ovs{scrollbar-width:none}" in css
    assert ".reqmap .rm-ovs::-webkit-scrollbar{display:none}" in css
    assert ".reqmap .rm-ovbar{position:absolute;" in css and ".reqmap .rm-ovbar.near{opacity:.3}" in css
    assert "document.querySelectorAll('.reqmap .rm-issue, .reqmap .rm-list')" in T.REQMAP_SCROLL_JS
    assert T.REQMAP_SCROLL_JS.startswith('<script data-hr-swap="rerun">') \
        and T.REQMAP_SCROLL_JS.endswith("</script>")


def test_the_blind_spot_box_moves_under_the_card(tmp_path):
    """Victor, 19 Oct 2026: "this panel should show up on the other side" - the box says
    what no test shows, so it stands under the tests, right after the card."""
    out = _laid_out(tmp_path)
    side = out[out.index('class="rm-side"'):]
    assert '<div class="rm-gap" hidden></div>' in side
    text = out[out.index('class="rm-text"'):out.index('class="rm-side"')]
    assert "rm-gap" not in text
    assert side.index("</aside>") < side.index('class="rm-gap"')
    assert T.REQMAP_SCROLL_JS in out


def test_a_fragment_without_a_blind_spot_box_keeps_its_layout(tmp_path):
    out = _laid_out(tmp_path, frag=FRAGMENT.replace('<div class="rm-gap" hidden></div>', ""))
    assert 'class="rm-legend"' in out and "rm-gap" not in out.split("<style>")[0]


def test_the_shield_opens_a_popover_of_the_code_the_test_runs():
    """Victor, 7 Oct 2026: "what part of the code is this test really covering?" - a list
    of files to open, so a popover the pointer can travel into, not the one-line tip."""
    js, css = T.REQMAP_CHAPTERS_JS, T.REQMAP_CSS
    assert "document.getElementById('rm-test-cover')" in js
    assert "run.removeAttribute('data-tip');" in js
    assert "pop.addEventListener('pointerenter', function () { clearTimeout(hideT); });" in js
    assert "Changed code it runs" in js
    # Victor, 19 Oct 2026: the unchanged files a test also runs are noise under the shield.
    assert "Also runs" not in js
    assert ".rm-covpop{position:fixed;" in css and ".rm-covpop.on{display:block}" in css


def test_the_cover_list_names_only_changed_files_and_caps_them(tmp_path):
    """Victor, 19 Oct 2026: drop "Also runs" - the shield answers which changed code."""
    frag = ('<script type="application/json" class="rm-data">'
            '{"tests":{"t/ATest.java:5":{},"t/Via.java:3":{}}}</script>')
    hits = {f"src/F{i}.java": [1, 2] for i in range(15)}       # changed, one line each
    hits["src/Changed.java"] = [7, 8, 9]
    hits["src/Big.java"] = list(range(1, 50))                  # ran a lot, changed nothing
    changed = {f"src/F{i}.java": [2] for i in range(15)}
    changed["src/Changed.java"] = [8, 9, 30]
    doc = {"changed": changed,
           "tests": [{"file": "t/ATest.java", "line": 5, "hits": hits},
                     {"file": "t/Via.java", "line": 3, "hits": {"src/Big.java": [1]}},
                     {"file": "t/Other.java", "line": 1, "hits": {"src/X.java": [1]}}]}
    out = T.test_cover_files(frag, doc, tmp_path)
    assert out.startswith('<script type="application/json" id="rm-test-cover">')
    data = json.loads(out[out.index(">") + 1:-len("</script>")])
    # Only tests on the card, and only those that ran a changed file: Via.java reached the
    # change through a proxy and keeps its shield's one-line tip.
    assert list(data) == ["t/ATest.java:5"]
    c = data["t/ATest.java:5"]
    assert c["files"][0][:3] == ["src/Changed.java", 8, 2]    # most changed lines first
    assert c["files"][0][3] == f"vscode://file/{(tmp_path / 'src/Changed.java').resolve()}:8:1"
    assert "src/Big.java" not in [f[0] for f in c["files"]]  # no unchanged file
    assert len(c["files"]) == T.TEST_COVER_MAX and c["more"] == 16 - T.TEST_COVER_MAX
    assert T.test_cover_files(frag, None, tmp_path) == ""


def test_a_row_of_the_rest_of_the_run_keeps_every_column_and_opens_nothing():
    """Name, badge and file link only; the arrow's slot is kept, blank (`data-shut`), so
    the titles stay in one column; the link is an ordinary `vscode://` href, which the
    page's one click handler opens in VS Code or hands on."""
    js = T.REQMAP_CHAPTERS_JS
    assert '<div class="rm-t rm-other" data-shut="yes"' in js
    assert '<span class="rm-chev" aria-hidden="true">&#9654;</span>' in js
    # Not `disabled`: a disabled button takes no pointer, so its "why" could not show.
    assert '<button class="rm-link" type="button" aria-disabled="true"' in js
    assert "var NOLINK = 'Paired with no sentence';" in js
    assert '<span class="rm-st rm-st-none" aria-hidden="true">' in js
    assert ".reqmap .rm-st-none{width:15px;height:15px}" in T.REQMAP_CSS
    # Built on the first check, not at load.
    assert "if (mode === 'all') buildOthers();" in js


def test_the_inventory_is_every_test_that_ran_minus_the_card(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/OwnerTest.java").write_text("class OwnerTest { MockMvc mvc; }")
    frag = ('<script type="application/json" class="rm-data">'
            '{"tests":{"src/OwnerTest.java:10":{},"gone.java:3@base":{}}}</script>')
    doc = {"tests": [
        {"suite": "Backend JUnit", "title": "onCard", "file": "src/OwnerTest.java", "line": 10},
        {"suite": "Backend JUnit", "title": "other", "file": "src/OwnerTest.java", "line": 20},
        {"suite": "E2E Cucumber", "title": "Outline </script>", "file": "x.feature", "line": 5,
         "source": "jacoco+v8"},
        {"suite": "E2E Cucumber", "title": "Outline </script>", "file": "x.feature", "line": 5,
         "source": "jacoco+v8"},
        {"suite": "Frontend Karma", "title": "renders", "file": "a.spec.ts", "line": 7,
         "source": "karma"},
        {"suite": "Backend ArchUnit", "title": "adheres", "file": None, "line": None},
    ]}
    out = T.all_tests_inventory(frag, doc, tmp_path)
    assert out.startswith('<script type="application/json" id="rm-all-tests">')
    assert "</script>" not in out[len('<script'):-len("</script>")]   # a title cannot close it
    body = json.loads(out[out.index(">") + 1:-len("</script>")].replace("<\\/", "</"))
    got = [(t["title"], t["cat"], t["file"], t.get("href")) for t in body["tests"]]
    assert got == [
        ("adheres", "unit", "", None),
        ("renders", "unit", "a.spec.ts", f"vscode://file/{(tmp_path / 'a.spec.ts').resolve()}:7:1"),
        ("other", "api", "src/OwnerTest.java",
         f"vscode://file/{(tmp_path / 'src/OwnerTest.java').resolve()}:20:1"),
        ("Outline </script>", "e2e", "x.feature",
         f"vscode://file/{(tmp_path / 'x.feature').resolve()}:5:1"),
    ]
    assert T.all_tests_inventory(frag, None, tmp_path) == ""


def test_the_ticket_title_is_a_link_over_the_ticket(tmp_path):
    """The one thing the page never said. The masthead carries the PR's title, which on
    this branch is not the issue's, and the frame opened straight into `opened on Jun 13,
    2026` with nothing saying what was opened."""
    out = _laid_out(tmp_path)
    assert ('<span class="rm-title" data-tip="Link Visit with Vet"><span class="rm-ref">Issue <a class="rm-num" '
            'href="https://github.com/victorrentea/petclinic/issues/37">#37</a></span>: '
            'Link Visit with Vet</span>') in out
    head, ticket = _order(out, 'class="tabtitle rm-head"', 'class="rm-ticket"')
    assert head < ticket


def test_the_title_is_a_row_of_the_grid_and_not_a_child_of_the_left_column(tmp_path):
    """This is the whole of the alignment, so it is pinned as structure and not as a look.

    In the left column the title would push that column down and leave the card level with
    nothing; as a row of `.rm-body` above both, the row under it starts the two frames
    together — at any width, with no measured constant to keep in step."""
    out = _laid_out(tmp_path)
    body = out.index('<div class="rm-body">')
    head = out.index('class="tabtitle rm-head"')
    text = out.index('class="rm-text"')
    assert body < head < text
    css = out[out.rindex("<style>"):]
    assert "grid-template-columns:minmax(0,474fr) minmax(0,520fr)" in css
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
    assert T.CARD_WHO in card and T.CARD_WHEN in card
    assert out.count(T.CARD_WHO) == 1


def test_the_card_names_the_pairing_as_the_ai_part_not_the_tests(tmp_path):
    """*Covering tests — as matched by AI* put the doubt on the tests, which are real and
    resolve in the tree. What a model decided is the pairing, so the strip says that,
    whatever words the paid run left there — and the robot's hover says the same."""
    out = _laid_out(tmp_path)
    card = out[out.index('<aside class="rm-code">'):out.index("</aside>")]
    assert "Covering tests" not in card and "as matched by AI" not in card
    assert '<span class="rm-who">Semantic test coverage</span>' in card
    # The ticket's own strip is not the card's: its byline stays the author's.
    assert '<span class="rm-who">victorrentea</span>' in out


def test_the_card_head_is_left_alone_without_a_strip():
    side = '<div class="rm-side"><aside class="rm-code"><div class="rm-list"></div></aside></div>'
    assert T.card_head(side) == side


def test_the_run_tests_press_runs_every_producer_of_the_tab_forced(tmp_path):
    """The third mode: the tab's producers, the suite-running `traces` included, with the
    step cache bypassed — a press that is about running the tests must run them."""
    from hrbuild.shared import actions as A
    skill = HERE
    root = tmp_path / "repo"
    (root / ".human-review").mkdir(parents=True)
    saved = dict(A.ACTIONS)
    try:
        info = T.declare_run_tests_rerun(root, root / ".human-review", skill)
        assert info and info["steps"] == T.run_tests_steps(skill)
        assert "traces" in info["steps"] and "tests" in info["steps"]
        cmd = A.ACTIONS[info["id"]]["command"]
        assert info["id"] == "__rerun_tests__:requirements"
        assert "--steps tests,traces,testcov --force --no-serve" in cmd
        assert A.ACTIONS[info["id"]]["reload"] is True
        btn = T.run_tests_button(info)
        assert 'data-rerun="__rerun_tests__"' in btn and 'data-tab="requirements"' in btn
        assert "hidden" in btn and "\u23F3" in btn
        assert T.run_tests_button(None) == ""
        # Outside the repository there is nothing to rebuild with.
        assert T.declare_run_tests_rerun(root, tmp_path / "elsewhere", skill) is None
    finally:
        A.ACTIONS.clear()
        A.ACTIONS.update(saved)


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
    assert 'class="rm-title"' not in out and 'class="rm-num"' not in out
    # The row itself stays: the coverage switch lives on it and must not come and go
    # with `gh`'s mood.
    assert 'class="tabtitle rm-head"' in out
    # …and the columns are still laid out, which is what keeps them level.
    assert "grid-template-columns:minmax(0,474fr) minmax(0,520fr)" in out


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
    out = T.reqmap_layout(without_cats, SPEC, tmp_path)
    # The layout is abandoned; the hover on a cut name is not. It hangs off one class the
    # fragment's own stylesheet declares, so it survives a redesign this function no
    # longer recognises — which is the matrix most likely to be cutting names somewhere
    # new.
    assert out == without_cats + T.REQMAP_TIP_JS
    assert ".rm-cats" in capsys.readouterr().err


def test_a_name_the_matrix_had_to_cut_gets_the_rest_of_it_on_hover(tmp_path):
    """Nine covering-test names were cut on this project's own PR and none of them carried
    `title`, `data-tip` or `aria-label`: *"The vet chosen while booking is named everywhere
    the visit i…"* needed 372 px and got 156, and two UNIT rows for two different
    components collapsed to nearly the same visible string. Every other truncation on this
    page has a hover; these did not.

    Measured on the way in rather than stamped at build time, because whether a name fits
    is a question about the reader's window — and because the panel is `display:none`
    until the tab is opened, where everything measures 0 and nothing looks truncated."""
    out = _laid_out(tmp_path)
    assert out.count(T.REQMAP_TIP_JS) == 1
    assert T.REQMAP_CUT == ".reqmap .rm-tt"
    js = T.REQMAP_TIP_JS
    assert T.REQMAP_CUT in js, "the selector is said once and used"
    assert "scrollWidth > el.clientWidth + 1" in js, "only a name that is actually cut"
    assert "removeAttribute('data-tip')" in js, \
        "a name that fits must not hover with a copy of itself"
    # Capture phase, which is what puts it ahead of TIP_JS's own delegated `pointerover`
    # on the same document: by the time the tooltip asks for the attribute, it is there.
    assert "addEventListener('pointerover', measure, true)" in js
    assert "addEventListener('focusin', measure, true)" in js
    # The page's one tooltip component, not a native title it cannot style or size.
    assert "title" not in js


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
                 'class="rm-data"', "fully covered"):
        assert kept in out, kept


def test_the_coverage_switch_sits_on_the_ticket_header_and_starts_checked(tmp_path):
    """`Semantic coverage`, checked, at the far end of the ticket frame's header
    strip, beside `opened on …`: the reader opens
    the tab to the matrix saying what it was built to say, and unchecks it to read the
    ticket as its author wrote it. The stylesheet takes the fills off under
    `data-semcov="off"`, which the one listener sets and clears."""
    out = _laid_out(tmp_path)
    head = out[out.index('class="tabtitle rm-head"'):out.index('class="rm-text"')]
    assert "rm-semcov" not in head, "not on the title row any more"
    assert ('opened on Jun 13, 2026</span><label class="rm-semcov" data-tip="Claim ↔ test, '
            'as matched by AI"><input '
            'type="checkbox" checked> Semantic coverage \U0001F916</label></div>') in out
    assert out.count('class="rm-semcov"') == 1, "only the ticket's header, not the tests'"
    css = out[out.rindex("<style>"):]
    assert ".reqmap[data-semcov=off] .rm-f[data-cov]{background:none}" in css
    assert ".reqmap[data-semcov=off] .rm-legend{visibility:hidden}" in css
    assert "box.matches('.rm-semcov input')" in out
    assert "setAttribute('data-semcov', 'off')" in out


# --- the legend fits its column, measured in a browser ------------------------------------

def _all_states_fragment() -> str:
    """The matrix as `semcov.py` draws it, with every legend state on: the five, plus
    `unconfirmed` and `narrowed`. Its own stylesheet inlined, as on the page."""
    sc = importlib.util.spec_from_file_location("semcov_layout", HERE / "semcov.py")
    S = importlib.util.module_from_spec(sc)
    sc.loader.exec_module(S)
    legend = S.LEGEND.replace("</div>", "".join(S.LEGEND_EXTRA.values()) + "</div>")
    css = (HERE / "reqmap" / "reqmap.css").read_text(encoding="utf-8")
    return FRAGMENT.replace('<style>.reqmap{color:#111}</style>', f"<style>{css}</style>") \
        .replace('<div class="rm-legend"><span class="rm-lgt">Legend:</span>'
                 '<span class="rm-lg">fully covered</span></div>', legend)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [986, 1100, 940])
def test_the_legend_holds_every_state_inside_its_column(width, scheme, tmp_path):
    """Eval run 10: eight items in a `nowrap` row, scrollWidth 617 against a 470px column
    at a 1440px window — `unconfirmed` ran under the card, and `narrowed`, the only key to
    the grey-hatched sentence, was hidden behind it. `986` is that page's body: two 470px
    columns and the 46px gutter."""
    sync = pytest.importorskip("playwright.sync_api")
    from hrbuild.shared import assets
    out = T.reqmap_layout(_all_states_fragment(), SPEC, tmp_path)
    assert out.count('class="rm-lg"') == 7
    page_file = tmp_path / "page.html"
    page_file.write_text(f'<!doctype html><html><head><meta charset="utf-8"><style>{assets.CSS}'
                         f'</style></head><body><div style="width:{width}px">{out}</div>'
                         "</body></html>", encoding="utf-8")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      color_scheme=scheme)
            page = ctx.new_page()
            page.goto(page_file.as_uri())
            got = page.evaluate("""() => {
              const L = document.querySelector('.rm-legend'),
                    side = document.querySelector('.rm-side').getBoundingClientRect(),
                    box = L.getBoundingClientRect();
              return {sw: L.scrollWidth, cw: L.clientWidth, right: box.right,
                      sideLeft: side.left,
                      pills: [...L.querySelectorAll('.rm-lg')].map(e => {
                        const r = e.getBoundingClientRect();
                        return [e.textContent, r.left, r.right, r.width,
                                getComputedStyle(e).visibility];})};
            }""")
        finally:
            browser.close()
    assert got["sw"] <= got["cw"] + 1, f"legend overflows: {got['sw']} > {got['cw']}"
    for name, left, right, w, vis in got["pills"]:
        assert w > 0 and vis == "visible", name
        assert right <= got["right"] + 1, f"{name!r} runs past its column"
        assert right <= got["sideLeft"], f"{name!r} sits under the tests card"
    assert {p[0] for p in got["pills"]} >= {"unconfirmed", "narrowed"}


#: Resolves once the layout has stopped moving: the fit script's `--rm-tail-h`, the page's
#: height and the footer's edge read the same over three animation frames running. A fixed
#: 100 ms wait instead raced the ResizeObserver on a loaded machine.
SETTLED = """() => new Promise(done => {
  let last = null, same = 0;
  (function tick() {
    const now = JSON.stringify([innerWidth, innerHeight, document.documentElement.scrollHeight,
      document.documentElement.style.getPropertyValue('--rm-tail-h'),
      document.querySelector('footer').getBoundingClientRect().bottom]);
    same = now === last ? same + 1 : 0;
    last = now;
    if (same >= 3) done(); else requestAnimationFrame(tick);
  })();
})"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_tab_is_exactly_one_window_tall_footer_included(scheme, tmp_path):
    """Victor, 7 Oct 2026, at 1152x625: the matrix filled the window under the masthead and
    the footer hung 81px below it, so the page grew a scrollbar of its own next to the two
    panes'. The matrix now gives up what the page lays out under it (`--rm-tail-h`,
    REQMAP_FIT_JS): no page scroll at any height, after a resize, with the footer on
    screen."""
    sync = pytest.importorskip("playwright.sync_api")
    from hrbuild.shared import assets
    out = T.reqmap_layout(_all_states_fragment(), SPEC, tmp_path)
    page_file = tmp_path / "page.html"
    page_file.write_text(
        '<!doctype html><html style="--strip-h:87.5px"><head><meta charset="utf-8">'
        f'<style>{assets.CSS}</style></head><body><div class="wrap">'
        '<div class="masthead" style="height:87.5px"></div>'
        f'<section class="panel">{out}</section>'
        '<footer><p class="footrow">Built by somebody.</p><p class="diskline">/a/path</p></footer>'
        '</div></body></html>', encoding="utf-8")
    measure = """() => ({sh: document.documentElement.scrollHeight, ih: innerHeight,
      foot: document.querySelector('footer').getBoundingClientRect().bottom,
      gap: document.querySelector('.rm-side').getBoundingClientRect().left
           - document.querySelector('.rm-text').getBoundingClientRect().right,
      gutter: parseFloat(getComputedStyle(document.querySelector('.wrap')).paddingLeft)})"""
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser downloaded for this interpreter
            pytest.skip(f"chromium unavailable: {e}")
        try:
            page = browser.new_page(viewport={"width": 1152, "height": 625},
                                    color_scheme=scheme)
            page.goto(page_file.as_uri())
            page.evaluate(SETTLED)
            got = [page.evaluate(measure)]
            for w, h in ((1152, 900), (1152, 480), (1500, 900), (1152, 625)):
                page.set_viewport_size({"width": w, "height": h})
                page.evaluate(SETTLED)
                got.append(page.evaluate(measure))
        finally:
            browser.close()
    for g in got:
        assert g["sh"] <= g["ih"], f"the page scrolls: {g}"
        assert g["foot"] <= g["ih"], f"the footer is under the fold: {g}"
        # The cards are as far apart as they are from the window's edge (Victor, 7 Oct
        # 2026), at every width: the cards take what the window adds, not the gap.
        assert abs(g["gap"] - g["gutter"]) < 0.5, f"gap {g['gap']} != gutter {g['gutter']}"


def test_the_fit_script_rides_with_the_matrix_and_the_sheet_reads_it(tmp_path):
    out = _laid_out(tmp_path)
    assert "setProperty('--rm-tail-h', v)" in out
    assert "calc(100dvh - var(--strip-h, 7rem) - var(--rm-tail-h, 0px))" in T.REQMAP_CSS
    assert out.index("--rm-tail-h', v)") > out.index('class="rm-body"')


def test_the_row_pills_are_one_width():
    """Victor, 7 Oct 2026: E2E was wider than API/UNIT and broke the titles' column."""
    css = (HERE / "reqmap" / "reqmap.css").read_text(encoding="utf-8")
    assert ".reqmap .rm-thead .rm-cat{display:inline-flex;justify-content:center;min-width:4.5em}" in css
    # The chapter headings wear the same pill, the same width.
    assert ".reqmap .rm-chh .rm-cat{display:inline-flex;justify-content:center;min-width:4.5em}" in T.REQMAP_CSS


def test_the_extension_is_underlined_alone_and_the_preview_is_the_body_alone():
    """Victor, 7 Oct 2026: the srcref's dotted border ran the whole right-aligned 51px box,
    a stray line in front of `.java`; and the open row's file:line bar was noise."""
    css = (HERE / "reqmap" / "reqmap.css").read_text(encoding="utf-8")
    assert (".reqmap .rm-tw.srcref{border-bottom:0;text-decoration:underline dotted 1px;"
            "text-underline-offset:3px}") in css
    assert ".reqmap .rm-tinner .rm-srcbar{display:none}" in css


def test_a_models_ui_label_is_renamed_e2e_at_build_time():
    """The end-to-end level is `E2E` everywhere; a fragment a model drew says `UI` and is
    relabelled by the builder, never regenerated."""
    frag = ('<p class="rm-cats"><span><span class="rm-cat" data-cat="e2e">UI</span>clicks the '
            'screen</span><span><span class="rm-cat" data-cat="unit">unit</span>x</span></p>'
            '<script type="application/json" class="rm-data">'
            '{"cats": {"e2e": "UI", "api": "API", "unit": "unit"}}</script>')
    out = T.relabel_cats(frag)
    assert 'data-cat="e2e">E2E</span>end to end: clicks the screen' in out
    assert 'data-cat="unit">Unit</span>' in out
    assert '"cats": {"e2e": "E2E", "api": "API", "unit": "Unit"}' in out
    assert ">UI<" not in out and T.relabel_cats(out) == out


def test_the_card_title_says_how_the_list_was_computed():
    tip = T.covcard_tip({"suites": [{"name": "A", "source": "jacoco", "tests": 222},
                                    {"name": "B", "source": "karma", "tests": 123}]})
    assert tip == "Tests that ran a changed line, as captured by a coverage probe."
    # The checkbox swaps it while the card lists the whole run.
    js = T.REQMAP_CHAPTERS_JS
    assert "'Tests that ran a changed line, as captured by a coverage probe.'" in js
    assert "'All tests, as captured by a coverage probe.'" in js


def test_a_traced_test_is_never_folded_out_of_the_covering_card():
    """An end-to-end run walks the whole stack, so the coverage join ranks every one as a
    pass-through. A test on the Sequence tab (or with a recording) gets its own open group."""
    blob = {"ranks": {"0": "p", "1": "b", "2": "own", "3": "gone", "4": "aimed", "5": "pass"},
            "fold": {"from": 4, "label": "x"}, "foldOwn": {"from": 2, "to": 3, "label": "o"},
            "foldGone": {"from": 3, "to": 4, "label": "g", "min": 3},
            "tests": {"app/src/add-visit.spec.ts:31": {"rank": 5},
                      "app/src/other.spec.ts:9": {"rank": 5},
                      "app/src/Own.java:5": {"rank": 2}, "app/src/Paired.java:1": {}}}
    page = ('<script type="application/json" id="hr-genseq">[{"test": '
            '"app/src/add-visit.spec.ts:31", "pair": "x"}]</script>'
            '<script type="application/json" class="rm-data">' + json.dumps(blob) + '</script>')
    out = T.promote_traced(page)
    d = json.loads(out.split('class="rm-data">')[1].split("</script>")[0])
    assert d["tests"]["app/src/add-visit.spec.ts:31"]["rank"] == 2
    assert d["tests"]["app/src/other.spec.ts:9"]["rank"] == 6, "untraced pass-through moves down"
    assert d["tests"]["app/src/Own.java:5"]["rank"] == 3
    assert d["ranks"]["2"] == T.TRACED_LABEL and d["ranks"]["6"] == "pass"
    assert (d["fold"]["from"], d["foldOwn"]["from"], d["foldOwn"]["to"],
            d["foldGone"]["from"], d["foldGone"]["to"]) == (5, 3, 4, 4, 5)
    assert T.promote_traced(out) == out, "idempotent"
