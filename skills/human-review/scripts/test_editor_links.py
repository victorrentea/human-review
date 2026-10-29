"""Every link to source code goes through ONE mechanism: an `<a href="vscode://file/…">` whose
click editor.js takes on `document` and sends to the IDE picked in the masthead (`hr-ide`) —
the server with `ide=`, IntelliJ's `jetbrains://` off disk, or a copied `path:line` with none
connected. A script that opens code some other way opens VS Code whatever the pick.

Victor, 29 Oct 2026: IntelliJ picked, and the Sequence tab's statement panel opened
`NotificationController.visitBooked()` in VS Code. The panel kept its clicks to itself
(`stopPropagation`), so the handler link fell through to its bare `vscode://` href; and a
diagram's scenario header set `location.href` itself. "It should never happen again" — so
this guards the source, every generated page within reach, and the panel at runtime."""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ASSETS = HERE / "hrbuild" / "assets"

#: The mechanism itself: the only script allowed to hand the browser an editor URL.
MECHANISM = ASSETS / "editor.js"

#: An editor URL in markup that is not a code link: the PR line's *in VS Code* (`a.pr-vsc`)
#: is built with the GitHub Pull Requests extension's URI as its address. editor.js re-aims
#: it at the picked IDE and takes its click (`aimPr`, `openPr`), as it does a code link's.
NOT_CODE_HREF = ("vscode://github.vscode-pull-request-github/",)

NAVIGATES = re.compile(r"\blocation\.href\s*=(?!=)|\blocation\.(?:assign|replace)\s*\(|\bwindow\.open\s*\(")
FOREIGN = re.compile(r"(?:jetbrains|idea)://")


def page_sources() -> list[Path]:
    """Everything that ends up as script or markup on a page: the assets, the build package,
    and every producer next to it (the includeHtml fragments: API, Data model, Structure, UX,
    Complexity, CODEOWNERS…). Not the server (`serve-review.py`): it opens files through the
    OS, which is where IntelliJ's `idea://` belongs."""
    out = sorted(ASSETS.rglob("*.js")) + sorted((HERE / "hrbuild").rglob("*.py"))
    out += [p for p in sorted(HERE.glob("*.py"))
            if not p.name.startswith(("test_", "conftest")) and p.name != "serve-review.py"]
    return out


def bypasses_in_script(text: str, allow_mechanism: bool = False) -> list[str]:
    """Lines of script that send the browser to an editor without the mechanism."""
    bad = []
    for line in text.splitlines():
        code = line.strip()
        if code.startswith(("//", "#", "*", "/*")):
            continue
        if NAVIGATES.search(code) and not allow_mechanism:
            bad.append(code)
        if FOREIGN.search(code) and not allow_mechanism:
            bad.append(code)
    return bad


def test_no_script_but_the_mechanism_navigates_or_names_another_ide():
    """`location.href = …`, `window.open(…)` and friends are how a link skips the pick: the
    mechanism (editor.js) is the one script that may hand the browser an editor URL, and the
    one that knows IntelliJ's. Anything else opening code calls `HR.openCode(href)`, or
    clicks a real `vscode://file/` anchor."""
    found = {}
    for path in page_sources():
        bad = bypasses_in_script(path.read_text(encoding="utf-8"), allow_mechanism=path == MECHANISM)
        if bad:
            found[str(path.relative_to(HERE))] = bad
    assert not found, ("open code through HR.openCode (editor.js), not by navigating:\n"
                       + json.dumps(found, indent=1))


def test_no_click_handler_near_code_swallows_the_click():
    """The bug's other half: a container that stops every click (`stopPropagation` as the
    whole listener) hides the code links inside it from editor.js's listener on `document`."""
    blanket = re.compile(r"addEventListener\(\s*'click'\s*,\s*function\s*\(\s*(\w+)\s*\)\s*\{\s*"
                         r"\1\.stopPropagation\(\);\s*\}\s*\)")
    # The one left: the 🎭 trace link opens the trace viewer in a tab, not code.
    allowed = {"trace.js"}
    found = [p.name for p in page_sources() if p.name not in allowed
             and blanket.search(p.read_text(encoding="utf-8"))]
    assert not found, f"a blanket stopPropagation hides code links from the mechanism: {found}"


def test_the_sequence_panel_opens_code_through_the_mechanism():
    js = (ASSETS / "genseq.js").read_text(encoding="utf-8")
    assert "window.HR.openCode(href)" in js
    assert "panel.addEventListener('click', function (ev) { ev.stopPropagation(); });" not in js


# ── generated pages ───────────────────────────────────────────────────────────────

class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bad, self.scripts, self._in, self.code = [], [], False, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._in = True
            self.scripts.append("")
        for k, v in a.items():
            if k.startswith("on") and v and re.search(r"vscode:|jetbrains:|idea:|location", v):
                self.bad.append(f"<{tag} {k}={v[:80]!r}>")
            if k in ("data-tip-pop", "data-tip-html") and v:
                inner = _Links()
                inner.feed(v)
                self.bad += inner.bad
                self.code += inner.code
        if tag != "a":
            return
        for k in ("href", "xlink:href"):
            h = (a.get(k) or "").strip()
            if FOREIGN.match(h):
                self.bad.append(f"<a {k}={h[:90]!r}> names an IDE itself")
            elif h.startswith("vscode:"):
                self.code += 1
                if not h.startswith("vscode://file/") and not h.startswith(NOT_CODE_HREF):
                    self.bad.append(f"<a {k}={h[:90]!r}> is not a vscode://file/ reference")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in = False

    def handle_data(self, data):
        if self._in:
            self.scripts[-1] += data


def page_bypasses(text: str, scripts: bool = True) -> tuple[list[str], int]:
    """What on a built page reaches code outside the mechanism, and how many code links it has.
    `scripts`: also hold its inline scripts to the source rule (editor.js's own is exempt)."""
    p = _Links()
    p.feed(text)
    bad = list(p.bad)
    if scripts:
        mechanism = MECHANISM.read_text(encoding="utf-8")
        for s in p.scripts:
            bad += bypasses_in_script(s, allow_mechanism=mechanism.strip()[:400] in s)
    return bad, p.code


def _pages() -> list[Path]:
    """The pages within reach: the published demos, and the local review pages beside them."""
    out = [REPO / "demo" / "demo" / "review.html", REPO / "demo" / "demo2" / "review.html"]
    work = REPO.parent
    out += [work / d / ".human-review" / "review.html" for d in
            ("petclinic-pr-owner-grid-paginated", "petclinic-pr-visit-has-vet",
             "petclinic-visits-paginated")]
    return [p for p in out if p.is_file()]


@pytest.mark.parametrize("page", _pages(), ids=lambda p: "/".join(p.parts[-3:]))
def test_a_built_page_has_no_code_link_outside_the_mechanism(page):
    """Markup only: a committed demo carries the scripts of the day it was published, and
    those are guarded at their source above. The markup is the generators' output."""
    text = page.read_text(encoding="utf-8")
    bad, code = page_bypasses(text, scripts=False)
    assert code, "a review page with no code link at all: the parser no longer sees them"
    assert not bad, "\n".join(bad[:20])
    # Every asset the page loads beside it (fragments, the API diff) holds to the same rule.
    for frag in sorted((page.parent / "assets").glob("*.html")):
        fbad, _ = page_bypasses(frag.read_text(encoding="utf-8"), scripts=True)
        assert not fbad, f"{frag.name}: " + "\n".join(fbad[:20])


def test_the_rule_catches_what_it_is_for():
    bad, _ = page_bypasses('<a href="jetbrains://idea/human-review/open?file=x">x</a>'
                           '<a href="vscode://somebody.ext/open?x">y</a>'
                           '<span data-tip-pop="&lt;a href=&quot;idea://open?file=x&quot;&gt;z&lt;/a&gt;">p</span>'
                           '<button onclick="location.href=\'vscode://file//x:1:1\'">b</button>'
                           "<script>function f(h) { window.location.href = h; }</script>")
    assert len(bad) == 5, bad
    # The PR line's link used to navigate itself, in VS Code whatever the pick.
    assert bypasses_in_script("function go() { window.location.href = href; }")
    assert bypasses_in_script("location.href = 'jetbrains://idea/human-review/open-pr?url=' + u;")
    ok, code = page_bypasses('<a class="srcref" href="vscode://file//r/A.java:3:1">A.java:3</a>'
                             '<svg><a xlink:href="vscode://file//r/A.java:4:1" href="vscode://file//r/A.java:4:1">'
                             '<text>A</text></a></svg>')
    assert ok == [] and code == 3


# ── at runtime: the Sequence panel, with each IDE picked ────────────────────────────

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


def _page(build):
    details = {"details": {"booked": {
        "title": "Backend → NotificationService",
        "handler": {"name": "NotificationController.visitBooked",
                    "href": "vscode://file//r/src/NotificationController.java:21:1"},
        "steps": [{"label": "request body", "text": "{}"}]}}}
    return ('<!doctype html><html data-hr-root="/r"><head><style>' + build.CSS + '</style>'
            + build.SERVER_JS + '</head><body><div class="titlerow"><span class="titleside">'
            + build.IDE_CHIPS + '</span></div>'
            '<p><a class="dgm-src" id="src" href="vscode://file//r/docs/c2.puml:1:1">c2.puml</a>'
            '<svg><a id="svgsrc" xlink:href="vscode://file//r/src/A.java:4:1" href="vscode://file//r/src/A.java:4:1">'
            '<text>A</text></a></svg></p>'
            '<div class="diagram" data-test-src="vscode://file//r/src/BookTest.java:1:1">'
            '<div class="svgbox"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 100"'
            ' width="400" height="100">'
            '<a href="genseq-scenario://12"><text font-size="13" x="10" y="15">books a visit</text></a>'
            '<g class="message"><a href="genseq://booked"><text font-size="13" x="10" y="60">'
            'POST /api/visits</text></a></g></svg></div>'
            '<script type="application/json" class="genseq-details">' + html.escape(json.dumps(details), quote=False)
            + '</script></div><p class="pilelede">' + build.vscode_pr_link(PR, "pr-vsc") + '</p>'
            + build.TIP_JS + build.EDITOR_JS + build.GENSEQ_JS + '</body></html>')


PR = "https://github.com/o/r/pull/51"
#: …and the IntelliJ plugin's link to it, off disk: the PR, and the checkout to find it in.
IJ_PR = ("jetbrains://idea/human-review/open-pr?url=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F51"
         "&root=%2Fr")
#: The PR line's link: its href, and the IDE its face names.
PR_FACE = """() => { const a = document.querySelector('a.pr-vsc');
  return [a.getAttribute('href'), a.querySelector('.ide-word').textContent]; }"""

HREFS = """() => {
  const svg = document.getElementById('svgsrc');
  return {src: document.getElementById('src').getAttribute('href'),
          svg: svg.getAttribute('href'),
          xlink: svg.getAttributeNS('http://www.w3.org/1999/xlink', 'href'),
          handler: document.querySelector('#genseq-panel .genseq-handler').getAttribute('href')};
}"""


def test_the_hrefs_follow_a_change_of_pick(browser, tmp_path):
    """IntelliJ, then none, then VS Code, pressed on the masthead's logos: every code link's
    href is re-aimed at each press, the panel's handler drawn later included."""
    from test_action_server import build
    f = tmp_path / "review.html"
    f.write_text(_page(build), encoding="utf-8")
    page = browser.new_page()
    page.add_init_script("localStorage.setItem('hr-ide', 'idea')")
    try:
        page.goto(f.as_uri())
        page.click('a[href="genseq://booked"] text')
        seen, pr = [page.evaluate(HREFS)], [page.evaluate(PR_FACE)]
        page.click("#hr-ide-idea")                  # green: disconnect
        seen.append(page.evaluate(HREFS)), pr.append(page.evaluate(PR_FACE))
        page.click("#hr-ide-vscode")                # red: connect VS Code
        seen.append(page.evaluate(HREFS)), pr.append(page.evaluate(PR_FACE))
        page.click("#hr-ide-vscode")                # disconnect
        page.click("#hr-ide-idea")                  # and IntelliJ again
        seen.append(page.evaluate(HREFS)), pr.append(page.evaluate(PR_FACE))
    finally:
        page.close()
    lead = [{h.split(":", 1)[0] for h in s.values()} for s in seen]
    assert lead == [{"jetbrains"}, {"vscode"}, {"vscode"}, {"jetbrains"}], seen
    # The PR line's link too: IntelliJ's plugin and its name, else the extension's URI.
    ij = [IJ_PR, "IntelliJ"]
    vsc = build.vscode_pr_uri(PR)
    assert pr == [ij, [vsc, "your IDE"], [vsc, "VS Code"], ij], pr


@pytest.mark.parametrize("pick", ["idea", "vscode"])
@pytest.mark.parametrize("served", [True, False], ids=["served", "off-disk"])
def test_the_statement_panel_and_the_scenario_header_follow_the_picked_ide(browser, tmp_path, pick, served):
    from test_action_server import build
    went, sent = [], []
    ctx = browser.new_context()
    # Never let an editor URL out of the browser: recorded, then aborted.
    ctx.route(lambda u: u.startswith(("vscode:", "jetbrains:", "idea:")),
              lambda r: (went.append(r.request.url), r.abort()))
    page = ctx.new_page()
    # Off disk a custom scheme never reaches the router: the navigation request is where it shows.
    cdp = ctx.new_cdp_session(page)
    cdp.on("Page.frameRequestedNavigation",
           lambda e: went.append(e["url"]) if e["url"].startswith(("vscode:", "jetbrains:", "idea:")) else None)
    cdp.send("Page.enable")
    page.add_init_script(f"localStorage.setItem('hr-ide', '{pick}')")
    body = _page(build)
    if served:
        def serve(route):
            url = route.request.url
            if url.endswith("/review.html"):
                return route.fulfill(body=body, content_type="text/html")
            sent.append(url.split("127.0.0.1:9", 1)[1])
            if "/__human_review__" in url:
                return route.fulfill(body='{"humanReview": 1, "token": "t"}', content_type="application/json")
            if "/__open__" in url:
                return route.fulfill(status=204, body="")
            return route.fulfill(status=404, body="")
        page.route("http://127.0.0.1:9/**", serve)
        page.goto("http://127.0.0.1:9/review.html")
        page.wait_for_function("!document.documentElement.classList.contains('hr-online')")
    else:
        f = tmp_path / "review.html"
        f.write_text(body, encoding="utf-8")
        page.goto(f.as_uri())
    try:
        page.click('a[href="genseq://booked"] text')
        hrefs = page.evaluate(HREFS)
        page.click("#genseq-panel .genseq-handler")
        page.wait_for_timeout(150)
        assert page.is_visible("#genseq-panel"), "a click inside the panel does not close it"
        page.click('a[href="genseq-scenario://12"] text')
        page.wait_for_timeout(150)
    finally:
        ctx.close()
    opens = [s for s in sent if s.startswith("/__open__")]
    went = list(dict.fromkeys(went))
    # What hover, middle-click and Copy Link Address see: the href itself names the pick.
    scheme = "jetbrains://idea/human-review/" if pick == "idea" else "vscode://file/"
    assert all(h.startswith(scheme) for h in hrefs.values()), hrefs
    assert "NotificationController.java" in hrefs["handler"] and "line=21" in hrefs["handler"] \
        if pick == "idea" else hrefs["handler"].endswith("NotificationController.java:21:1")
    if served:
        assert went == [], f"served, nothing navigates to an editor: {went}"
        assert len(opens) == 2 and all(f"ide={pick}" in s for s in opens), opens
        assert "NotificationController.java" in opens[0] and "line=21" in opens[0]
        assert "BookTest.java" in opens[1] and "line=12" in opens[1]
    else:
        scheme = "jetbrains://" if pick == "idea" else "vscode://"
        assert len(went) == 2 and all(u.startswith(scheme) for u in went), went
        assert "NotificationController.java" in went[0] and "BookTest.java" in went[1]


@pytest.mark.parametrize("pick", ["idea", "vscode"])
@pytest.mark.parametrize("served", [True, False], ids=["served", "off-disk"])
def test_the_pr_link_opens_the_pr_in_the_picked_ide(browser, tmp_path, pick, served):
    """The PR line's *in IntelliJ*: served, the server asks the plugin (`/__open_pr__`, the
    PR's URL in the body) and nothing navigates; off disk, the plugin's own `open-pr` link.
    *in VS Code*: served, the checkout's window first (`/__editor_open__`), then the
    extension's URI — the one way it went before IntelliJ could open a PR."""
    from test_action_server import build
    went, sent = [], []
    ctx = browser.new_context()
    ctx.route(lambda u: u.startswith(("vscode:", "jetbrains:", "idea:")),
              lambda r: (went.append(r.request.url), r.abort()))
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.on("Page.frameRequestedNavigation",
           lambda e: went.append(e["url"]) if e["url"].startswith(("vscode:", "jetbrains:", "idea:")) else None)
    cdp.send("Page.enable")
    page.add_init_script(f"localStorage.setItem('hr-ide', '{pick}')")
    body = _page(build)
    if served:
        def serve(route):
            req = route.request
            url = req.url
            if url.endswith("/review.html"):
                return route.fulfill(body=body, content_type="text/html")
            sent.append((req.method, url.split("127.0.0.1:9", 1)[1], req.post_data,
                         req.headers.get("x-human-review-token")))
            if "/__human_review__" in url:
                return route.fulfill(body='{"humanReview": 1, "token": "t", "openPr": true}',
                                     content_type="application/json")
            if "/__open_pr__" in url:
                return route.fulfill(body='{"how": "opened", "window": "petclinic", "number": 51}',
                                     content_type="application/json")
            if "/__editor_open__" in url:
                return route.fulfill(body='{"how": "focused", "window": "petclinic"}',
                                     content_type="application/json")
            return route.fulfill(status=404, body="")
        page.route("http://127.0.0.1:9/**", serve)
        page.goto("http://127.0.0.1:9/review.html")
        page.wait_for_function("!document.documentElement.classList.contains('hr-online')")
    else:
        f = tmp_path / "review.html"
        f.write_text(body, encoding="utf-8")
        page.goto(f.as_uri())
    try:
        page.click("a.pr-vsc")
        page.wait_for_timeout(500)
        toast = page.locator("#copy-toast").text_content() if page.locator("#copy-toast").count() else ""
    finally:
        ctx.close()
    posts = [(path, data, token) for method, path, data, token in sent if method == "POST"]
    went = list(dict.fromkeys(went))
    if pick == "idea" and served:
        assert posts == [("/__open_pr__", json.dumps({"url": PR}).replace(" ", ""), "t")], posts
        assert went == [], f"served, IntelliJ is asked through the server: {went}"
        assert toast == "Opened PR #51 in IntelliJ \u2014 petclinic", toast
    elif pick == "idea":
        assert went == [IJ_PR], went
    elif served:
        assert [p for p, _, _ in posts] == ["/__editor_open__"], posts
        assert went == [build.vscode_pr_uri(PR)], went
    else:
        assert went == [build.vscode_pr_uri(PR)], went
