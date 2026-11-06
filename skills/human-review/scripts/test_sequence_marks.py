"""The Sequence tab's marks (Victor, 19 Oct 2026): the open arrow in the diagram's strongest
ink, the `?`/`values` badge in the SQL marks' colours, the IDE logo where the generator wrote
`↗`, and the DB fixture each test starts from beside its file name."""
from __future__ import annotations

import html
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

ASSETS = HERE / "hrbuild" / "assets"
GENSEQ_CSS = (ASSETS / "css" / "genseq.css").read_text(encoding="utf-8")
GENSEQ_JS = (ASSETS / "genseq.js").read_text(encoding="utf-8")
TESTPAIRS = {"type": "testpairs", "title": "", "snippets": {"auto": "genseq"}}


def test_the_open_arrow_is_in_the_diagrams_own_ink_and_not_bold():
    rule = re.search(r'\.genseq-hot\.genseq-open a\[href\^="genseq:"\] text \{([^}]*)\}',
                     GENSEQ_CSS)
    assert rule and "fill:var(--dgm-fg)" in rule[1] and "font-weight:400" in rule[1]
    assert "font-weight:700; }" not in rule[0]


def test_the_values_badge_and_the_marks_share_one_colour_rule():
    assert 'class="genseq-toggle genseq-value"' in GENSEQ_JS
    toggle = re.search(r"\.genseq-panel \.genseq-toggle \{([^}]*)\}", GENSEQ_CSS)[1]
    assert "background" not in toggle and "color" not in toggle, "no second copy of it"
    colours = [c.strip() for c in re.findall(r"\.genseq-panel \.genseq-value \{([^}]*)\}", GENSEQ_CSS)]
    assert colours == ["background:var(--link); color:var(--card); font-weight:700;"]


# ── the DB fixture dot ───────────────────────────────────────────────────────────────

FEATURE = """Feature: Search owners

  @fixture:green
  Scenario: Searching on the green data
    When I open the owners page

  Scenario: Adding its own owner
    When I add an owner
"""


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                   check=True, capture_output=True)


def _puml(test_rel: str, line: int, title: str, label: str = "GET /api/owners") -> str:
    return ("@startuml\n"
            f"title [[src://{test_rel}:{line}{{Click to open the test}} {title}]]\n"
            "participant Browser\nparticipant Backend\n"
            f"Browser -> Backend: {label}\n@enduml\n")


def _repo(tmp_path: Path) -> Path:
    feat = "e2e/src/search.feature"
    (tmp_path / "e2e/src").mkdir(parents=True)
    (tmp_path / "e2e/generated").mkdir(parents=True)
    (tmp_path / "be/db/fixtures").mkdir(parents=True)
    (tmp_path / "be/db/fixtures/green.sql").write_text("-- green")
    (tmp_path / "be/db/fixtures/fixture-colors.json").write_text('{"green": "#2fa84f"}')
    (tmp_path / feat).write_text(FEATURE)
    (tmp_path / "e2e/generated/search.feature.green.genseq.puml").write_text(
        _puml(feat, 4, "Searching on the green data"))
    (tmp_path / "e2e/generated/search.feature.own.genseq.puml").write_text(
        _puml(feat, 7, "Adding its own owner"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    return review


def _summaries(out: str) -> dict[str, str]:
    return {re.search(r"Searching on the green data|Adding its own owner", s)[0]: s
            for s in re.findall(r"<summary data-tip=.*?</summary>", out, re.S)}


def test_each_pair_wears_the_tests_tab_dot_for_the_fixture_its_test_starts_from(tmp_path):
    review = _repo(tmp_path)
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    rows = _summaries(out)
    green = rows["Searching on the green data"]
    # At the right end, just before the file name — not in the middle of the sentence.
    assert re.search(r'Searching on the green data<span class="seqlang"><span class="fx-dot"'
                     r' role="img"[^>]*data-tip="DB Fixture: green"[^>]*data-fixture="green"'
                     r'[^>]*style="--fx:#2fa84f"></span><a class="srcref seqfile"', green), green
    own = rows["Adding its own owner"]
    assert "fx-dot" not in own, "declares no fixture: starts on an empty database, no dot"


def test_the_row_names_the_test_in_words_without_its_class():
    """The class is the file at the row's right end, so it is dropped, and a bare method
    name reads as words, the way `PrettyTestNames` spells one (Victor, 20 Oct 2026).
    Prose titles — E2E scenarios — have no class, and a colon in prose is not one."""
    assert build._pair_title(["VisitTest: create_withoutVet()"]) == \
        '<span class="seqtitle">create without vet</span>'
    assert build._pair_title(["AddVisitApiTest: adds a visit to an existing pet"]) == \
        '<span class="seqtitle">adds a visit to an existing pet</span>'
    assert build._pair_title(["VisitTest: a()", "VisitTest: b()"]) == \
        '<span class="seqtitle">a · b</span>'
    for prose in ("Searching with an empty last name lists every owner",
                  "Owner search: an empty name lists everyone"):
        assert build._pair_title([prose]) == html.escape(prose)
    css = (Path(build.__file__).parent / "hrbuild/assets/css/sequence.css").read_text()
    assert "details.testpair > summary::before { margin-right:-.05rem; }" in css, \
        "the caret sits half as far from the kind chip as core.css's folds do"


def test_a_test_the_verdicts_do_not_name_gets_no_dot(tmp_path, monkeypatch):
    review = _repo(tmp_path)
    monkeypatch.setattr("hrbuild.tabs.sequence.fixtures_by_test", lambda root: {})
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    assert "fx-dot" not in out


def test_a_jvm_verdict_keyed_on_the_annotation_still_reaches_the_method_the_diagram_names(
        tmp_path, monkeypatch):
    """The diagram's handle names the method's line; a verdict may be keyed on `@Test`,
    two annotation lines up. Code in between is another test's, and gets nothing."""
    rel = "be/src/test/java/AddVisitTest.java"
    (tmp_path / "be/src/test/java").mkdir(parents=True)
    (tmp_path / rel).write_text("class AddVisitTest {\n"            # 1
                                "    @Test\n"                       # 2
                                "    @Order(1)\n"                   # 3
                                "    void adds() {}\n"              # 4
                                "    void other() {}\n"             # 5
                                "    void third() {}\n")            # 6
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen/AddVisitTest.java.adds.genseq.puml").write_text(_puml(rel, 4, "adds"))
    (tmp_path / "gen/AddVisitTest.java.third.genseq.puml").write_text(_puml(rel, 6, "third"))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    monkeypatch.setattr("hrbuild.tabs.sequence.fixtures_by_test",
                        lambda root: {f"{rel}:2": "seed"})
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    rows = re.findall(r"<summary data-tip=.*?</summary>", out, re.S)
    adds = next(r for r in rows if "adds" in r)
    third = next(r for r in rows if "third" in r)
    assert 'data-tip="DB Fixture: Default"' in adds
    assert "fx-dot" not in third


# ── the `↗` goes, and no IDE logo replaces it ─────────────────────────────────────────

def test_the_tab_hands_genseq_js_no_ide_logos(tmp_path):
    """Victor, 20 Oct 2026: "no IntelliJ icon there, just a hyperlink is enough"."""
    review = _repo(tmp_path)
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    assert "seq-ide-marks" not in out


PAGE = """<!doctype html><html><head><style>{css}</style></head><body>
<div class="diagram"><div class="svgbox"><svg xmlns="http://www.w3.org/2000/svg"
  viewBox="0 0 400 100" width="400" height="100">
  <g class="message"><a href="vscode://file//x/OwnerRepository.java:19:1">
    <text font-size="13" x="10" y="30">OwnerRepository.save &#8599;</text></a></g>
  <g class="message"><a href="https://example.org/">
    <text font-size="13" x="10" y="60">elsewhere &#8599;</text></a></g>
</svg></div></div>
{tpl}
<script>window.HR = {{onready: function (fn) {{ fn(null); }}}};</script>
<script>{js}</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                       # no chromium installed on this box
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def test_the_arrow_label_loses_its_arrow_glyph_and_gets_no_logo(browser, tmp_path):
    page_file = tmp_path / "p.html"
    page_file.write_text(PAGE.format(css=GENSEQ_CSS, js=GENSEQ_JS, tpl=""), encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(page_file.as_uri())
        got = page.evaluate("""() => ({
          texts: [...document.querySelectorAll('svg text')].map(t => t.textContent),
          logos: document.querySelectorAll('.diagram svg a svg').length})""")
    finally:
        page.close()
    assert got["texts"] == ["OwnerRepository.save", "elsewhere"], "no ↗ left anywhere"
    assert got["logos"] == 0, "the label is the link; nothing is drawn after it"


def test_one_diagram_of_several_scenarios_on_one_start_still_gets_its_dot(tmp_path):
    """A per-file diagram (`== … ==` chapters) names several scenarios; on the same start
    they are one dot, not none."""
    feat = "e2e/src/two.feature"
    (tmp_path / "e2e/src").mkdir(parents=True)
    (tmp_path / feat).write_text("@fixture:default\nFeature: f\n\n  Scenario: one\n    When x\n\n"
                                 "  Scenario: two\n    When y\n")
    (tmp_path / "e2e/src/two.feature.genseq.puml").write_text(
        "@startuml\nparticipant A\nparticipant B\n"
        f"== [[src://{feat}:4{{t}} one]] ==\nA -> B: x\n"
        f"== [[src://{feat}:7{{t}} two]] ==\nA -> B: y\n@enduml\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    review = tmp_path / ".human-review"
    (review / "assets").mkdir(parents=True)
    out = build.render_testpairs(TESTPAIRS, {}, [], tmp_path, review)[0]
    assert out.count('class="fx-dot"') == 1, out[:2000]
    assert 'data-tip="DB Fixture: Default"' in out


# ── the panel's SQL and JSON are syntax-coloured ─────────────────────────────────────

COLOUR_PAGE = """<!doctype html><html><head><style>{css}</style></head><body>
<div class="diagram"><div class="svgbox"><svg xmlns="http://www.w3.org/2000/svg"
  viewBox="0 0 400 100" width="400" height="100">
  <g class="message"><a href="genseq://sql"><text font-size="13" x="10" y="30">select pets</text></a></g>
  <g class="message"><a href="genseq://json"><text font-size="13" x="10" y="60">200</text></a></g>
</svg></div>
<script type="application/json" class="genseq-details">{details}</script></div>
<script>window.HR = {{onready: function (fn) {{ fn(null); }}}};</script>
<script>{js}</script></body></html>"""


HOVER_DETAILS = {"details": {
    "sql": {"title": "select pets", "steps": [{"text": "SELECT 1"}]},
    "json": {"title": "200", "steps": [{"text": '{"id": 1}'}]}}}

#: A label wrapped in two lines is two links with one address, in one arrow.
TWO_LINES = ('<g class="message"><a href="genseq://sql"><text font-size="13" x="10" y="30">select</text></a>'
             '<a href="genseq://sql"><text font-size="13" x="10" y="44">pets</text></a></g>'
             '<g class="message"><a href="genseq://json" data-tip="Click for this call" '
             'xlink:title="Click for this call"><text font-size="13" x="10" y="80">200</text></a></g>')


#: A browser request: its words open the line that sent it, the ⊕ beside them the body.
TWO_LINKS = ('<g class="message"><a href="vscode://file/a.component.ts:22:1">'
             '<text font-size="13" x="10" y="30">POST /api/visits</text></a>'
             '<a href="genseq://sql"><text font-size="13" x="150" y="30">⊕</text></a>'
             '<line x1="10" x2="300" y1="40" y2="40" stroke="#000"/></g>')


def _hover_page(browser, tmp_path, messages=TWO_LINES, **viewport):
    import json
    src = COLOUR_PAGE.format(css=GENSEQ_CSS, js=GENSEQ_JS, details=json.dumps(HOVER_DETAILS))
    src = re.sub(r'<g class="message">.*</g>', messages, src, flags=re.S)
    src = src.replace('xmlns="http://www.w3.org/2000/svg"', 'xmlns="http://www.w3.org/2000/svg" '
                      'xmlns:xlink="http://www.w3.org/1999/xlink"')
    page_file = tmp_path / "h.html"
    page_file.write_text(src, encoding="utf-8")
    page = browser.new_page(viewport=viewport or {"width": 900, "height": 500})
    page.goto(page_file.as_uri())
    return page


def _open(page, sel):
    """The text of a panel on screen, or None."""
    return page.evaluate("""s => { const p = document.querySelector(s);
        return p && !p.hidden ? p.querySelector('pre').textContent : null; }""", sel)


def _kept(page, href):
    return page.evaluate("""h => document.querySelector(`a[href="genseq://${h}"]`)
        .closest('g').classList.contains('genseq-open')""", href)


SQL, JSON_LABEL = 'a[href="genseq://sql"] text', 'a[href="genseq://json"] text'


def test_a_resting_pointer_previews_and_only_a_click_keeps(browser, tmp_path):
    """Victor, 6 Nov 2026: "When I just hover over these labels, it should appear too, but not
    persistently … If I want it to stay, I click it, and then it turns white."."""
    page = _hover_page(browser, tmp_path)
    try:
        page.hover(SQL)
        assert _open(page, "#genseq-peek") is None, "not until the pointer rests, as tip.js"
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-peek") == "SELECT 1"
        assert _open(page, "#genseq-panel") is None and not _kept(page, "sql"), "not white"
        page.mouse.move(600, 400)
        assert _open(page, "#genseq-peek") is None, "volatile: gone with the pointer"

        page.hover(SQL)
        page.wait_for_timeout(200)
        page.click(SQL)
        assert _open(page, "#genseq-panel") == "SELECT 1" and _kept(page, "sql")
        assert _open(page, "#genseq-peek") is None, "one panel for one arrow"
        page.mouse.move(600, 400)
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-panel") == "SELECT 1" and _kept(page, "sql"), "it stays"
        page.click(SQL)
        assert _open(page, "#genseq-panel") is None and not _kept(page, "sql")
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-peek") is None, "closing it does not preview it again"
    finally:
        page.close()


CODE = 'a[href^="vscode:"] text'


def test_the_words_of_a_browser_request_open_its_code_and_only_the_marker_opens_the_body(
        browser, tmp_path):
    page = _hover_page(browser, tmp_path, messages=TWO_LINKS)
    try:
        page.evaluate("""document.addEventListener('click', ev => {
            window.reached = ev.target.closest('a')?.getAttribute('href');
            window.prevented = ev.defaultPrevented;
            ev.preventDefault(); })""")
        page.click(CODE)
        assert page.evaluate("window.reached") == "vscode://file/a.component.ts:22:1"
        assert not page.evaluate("window.prevented"), "editor.js must see an untouched click"
        assert _open(page, "#genseq-panel") is None

        page.click('a[href="genseq://sql"] text')
        assert _open(page, "#genseq-panel") == "SELECT 1"
    finally:
        page.close()


def test_a_preview_does_not_outlive_the_marker_when_the_pointer_slides_onto_the_code_link(
        browser, tmp_path):
    page = _hover_page(browser, tmp_path, messages=TWO_LINKS)
    try:
        page.hover('a[href="genseq://sql"] text')
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-peek") == "SELECT 1"
        page.hover(CODE)
        assert _open(page, "#genseq-peek") is None
    finally:
        page.close()


def test_a_preview_leaves_the_kept_panel_alone(browser, tmp_path):
    page = _hover_page(browser, tmp_path)
    try:
        page.click(SQL)
        kept = page.evaluate("document.querySelector('#genseq-panel').getBoundingClientRect().toJSON()")
        page.hover(JSON_LABEL)
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-peek") == '{"id": 1}' and not _kept(page, "json")
        assert _open(page, "#genseq-panel") == "SELECT 1" and _kept(page, "sql")
        page.mouse.move(600, 400)
        assert _open(page, "#genseq-peek") is None
        assert _open(page, "#genseq-panel") == "SELECT 1" and _kept(page, "sql")
        assert kept == page.evaluate("document.querySelector('#genseq-panel').getBoundingClientRect().toJSON()")
    finally:
        page.close()


def test_a_preview_moves_nothing_and_never_flickers(browser, tmp_path):
    """Short window, label by its foot: the kept panel scrolls the page to show itself, which
    under a resting pointer would move the label away and close the preview, in a loop."""
    page = _hover_page(browser, tmp_path, width=300, height=130)
    try:
        page.evaluate("document.body.style.paddingBottom = '40px'")
        box = page.locator(JSON_LABEL).bounding_box()
        x, y = box["x"] + 5, box["y"] + 5
        page.mouse.move(x, y)
        page.wait_for_timeout(200)
        page.evaluate("""() => { window.flips = 0;
            new MutationObserver(l => { window.flips += l.length; })
                .observe(document.querySelector('#genseq-peek'), {attributes: true}); }""")
        panel = page.evaluate("document.querySelector('#genseq-peek').getBoundingClientRect().toJSON()")
        assert panel["top"] >= 0 and panel["bottom"] <= 130, "inside the window, not below the fold"
        for i in range(10):
            page.mouse.move(x + i % 2, y)
            page.wait_for_timeout(50)
        assert page.evaluate("window.flips") == 0 and page.evaluate("scrollY") == 0
        assert page.locator(JSON_LABEL).bounding_box() == box, "the label stays under the pointer"
        assert _open(page, "#genseq-peek") == '{"id": 1}'
    finally:
        page.close()


def test_the_two_lines_of_a_wrapped_label_are_one_label(browser, tmp_path):
    """A click handler per link meant the second click closed the panel and reopened it."""
    page = _hover_page(browser, tmp_path)
    second = 'a[href="genseq://sql"]:nth-of-type(2) text'
    try:
        page.hover(SQL)
        page.wait_for_timeout(200)
        page.hover(second)
        assert _open(page, "#genseq-peek") == "SELECT 1", "no closing between the lines"
        page.click(second)
        assert _open(page, "#genseq-panel") == "SELECT 1"
        page.click(second)
        assert _open(page, "#genseq-panel") is None
    finally:
        page.close()


def test_the_preview_replaces_the_click_tip_and_reaches_the_keyboard(browser, tmp_path):
    page = _hover_page(browser, tmp_path)
    try:
        link = page.locator('a[href="genseq://json"]')
        assert link.get_attribute("data-tip") is None, "two bubbles would open over one label"
        assert link.evaluate("a => a.hasAttributeNS('http://www.w3.org/1999/xlink', 'title')") is False
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
        assert _open(page, "#genseq-peek") == "SELECT 1"
        page.keyboard.press("Enter")
        assert _open(page, "#genseq-panel") == "SELECT 1" and _open(page, "#genseq-peek") is None
        page.keyboard.press("Escape")
        assert _open(page, "#genseq-panel") is None
    finally:
        page.close()


def test_the_panel_colours_sql_and_json_and_keeps_the_value_marks(browser, tmp_path):
    """Victor, 20 Oct 2026: "add syntax coloring to this SQL snippet, as well as to the JSON".
    The tokens wear Pygments' class names inside a `pre.code`, so the page's one snippet
    palette colours them; the `?` and the bound values stay marks in both views."""
    import json
    details = {"details": {
        "sql": {"title": "select pets", "steps": [{
            "text": "SELECT p.name\nFROM pets p\nWHERE p.owner_id=? AND p.name like ? escape '\\'",
            "alternate": {"text": "SELECT p.name\nFROM pets p\nWHERE p.owner_id=1 AND p.name like 'A%' escape '\\'"}}]},
        "json": {"title": "200", "steps": [{"label": "response body",
            "text": '{\n  "id": 1,\n  "name": "Axel",\n  "vet": null,\n  "ok": true\n}'}]}}}
    page_file = tmp_path / "c.html"
    page_file.write_text(COLOUR_PAGE.format(css=GENSEQ_CSS, js=GENSEQ_JS,
                                            details=json.dumps(details)), encoding="utf-8")
    page = browser.new_page()
    read = """() => [...document.querySelector('#genseq-panel pre').childNodes]
                 .filter(n => n.nodeType === 1).map(n => [n.className, n.textContent])"""
    try:
        page.goto(page_file.as_uri())
        assert page.evaluate("document.querySelector('#genseq-panel') === null")
        page.click('a[href="genseq://sql"] text')
        assert page.evaluate("document.querySelector('#genseq-panel pre').classList.contains('code')")
        sent = page.evaluate(read)
        page.click("#genseq-panel .genseq-toggle")
        bound = page.evaluate(read)
        page.keyboard.press("Escape")
        page.click('a[href="genseq://json"] text')
        payload = page.evaluate(read)
    finally:
        page.close()
    for words in (sent, bound):
        assert ["k", "SELECT"] in words and ["k", "WHERE"] in words and ["k", "escape"] in words
        assert ["s1", "'\\'"] in words, "a string literal is a string"
        assert not any(t in ("p", "owner_id", "p.name") for _, t in words), "names stay plain"
    assert [t for c, t in sent if c == "genseq-value"] == ["?", "?"]
    assert [t for c, t in bound if c == "genseq-value"] == ["1", "'A%'"], \
        "the values view still marks what was put back, uncoloured inside the mark"
    assert ["nt", '"id"'] in payload and ["mi", "1"] in payload
    assert ["s2", '"Axel"'] in payload and ["kc", "null"] in payload and ["kc", "true"] in payload
