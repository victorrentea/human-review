"""The Sequence panel's Bruno button (Victor, 10 Nov 2026).

A request arrow's panel (sidecar version 3: `request: {method, url, headers?, body?}`) offers
the request to Bruno. Served by `serve-review.py` on a Mac with Bruno, the server writes it into
a collection under the checkout's `.human-review/bruno/` and runs `open -a Bruno` on that
folder — Bruno has no deep link to one request, but it opens a folder handed to it as a
collection. Anywhere else the button copies the request as cURL, which Bruno imports.

The traced URL is never replayed as is: its host:port was the test run's ephemeral frontend
port, or a Docker hostname. The `.bru` keeps the path and query behind a variable of the
collection's `local` environment — `{{baseUrl}}` for the browser, the called service's own
name for anything else.

Run with:  python3 -m pytest test_bruno.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_action_server import _call, server, srv  # noqa: F401  (fixture)

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "hrbuild" / "assets"
GENSEQ_JS = (ASSETS / "genseq.js").read_text(encoding="utf-8")
GENSEQ_CSS = (ASSETS / "css" / "genseq.css").read_text(encoding="utf-8")

GET = {"method": "GET", "url": "http://127.0.0.1:63285/api/owners/1",
       "headers": {"Accept": "application/json, text/plain, */*",
                   "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                   "tracestate": "x=1", "baggage": "k=v", "Origin": "http://127.0.0.1:63285",
                   "Host": "127.0.0.1:63285"}}
POST = {"method": "POST", "url": "http://notification:8090/api/notifications/visit-booked?retry=1",
        "headers": {"Content-Type": "application/json", "Content-Length": "48"},
        "body": '{"ownerPhone":"8474461990","petName":"Axel"}'}


# ---- the .bru writer ----------------------------------------------------------------------

def test_a_browser_get_is_written_behind_base_url_without_its_trace_headers():
    req = srv.bruno_request(GET, "Browser → Backend")
    assert srv.bru_text(req, 1) == (
        "meta {\n  name: GET /api/owners/1\n  type: http\n  seq: 1\n}\n\n"
        "get {\n  url: {{baseUrl}}/api/owners/1\n  body: none\n  auth: none\n}\n\n"
        "headers {\n  Accept: application/json, text/plain, */*\n}\n")
    assert (req["var"], req["default"]) == ("baseUrl", "http://localhost:8080")


def test_a_service_post_is_named_after_the_service_with_its_json_body_and_query():
    req = srv.bruno_request(POST, "Backend → NotificationService")
    assert srv.bru_text(req, 2) == (
        "meta {\n  name: POST /api/notifications/visit-booked\n  type: http\n  seq: 2\n}\n\n"
        "post {\n  url: {{notificationServiceUrl}}/api/notifications/visit-booked?retry=1\n"
        "  body: json\n  auth: none\n}\n\n"
        "params:query {\n  retry: 1\n}\n\n"
        "headers {\n  Content-Type: application/json\n}\n\n"
        "body:json {\n  {\n    \"ownerPhone\": \"8474461990\",\n    \"petName\": \"Axel\"\n  }\n}\n")
    assert req["default"] == "", "no compose file publishes it: the reader fills it in"


def test_a_body_that_is_not_json_is_text_and_cannot_close_its_block_early():
    req = srv.bruno_request({"method": "put", "url": "http://h/x", "body": "a\n}\nb"})
    text = srv.bru_text(req, 1)
    assert "  body: text\n" in text and "body:text {\n  a\n  }\n  b\n}\n" in text


def test_a_key_bruno_would_split_is_quoted():
    req = srv.bruno_request({"method": "GET", "url": "http://h/x?a%20b=1",
                             "headers": {"X Odd": "v", 'q"t': "w"}})
    text = srv.bru_text(req, 1)
    assert '  "a b": 1\n' in text and '  "X Odd": v\n' in text and '  "q\\"t": w\n' in text


@pytest.mark.parametrize("raw", [
    None, "GET /x", {"method": "GET"}, {"method": "BREW", "url": "http://h/x"},
    {"method": "GET", "url": "file:///etc/passwd"}, {"method": "GET", "url": "/api/owners"},
    {"method": "GET", "url": "http://h/x\nHost: evil"}, {"method": "GET", "url": "http://h/x",
                                                          "headers": ["a"]},
    {"method": "GET", "url": "http://h/x", "body": {"a": 1}},
])
def test_only_an_absolute_http_request_is_taken(raw):
    assert srv.bruno_request(raw) is None


def test_the_same_request_traced_on_another_port_is_the_same_file(tmp_path):
    again = dict(GET, url="http://127.0.0.1:50111/api/owners/1")
    one, two = srv.bruno_request(GET, "Browser → Backend"), srv.bruno_request(again, "Browser → Backend")
    assert srv.bruno_slug(one) == srv.bruno_slug(two)
    assert re.fullmatch(r"get-api-owners-1-[0-9a-f]{6}", srv.bruno_slug(one))
    other = srv.bruno_request(dict(POST, body='{"petName":"Max"}'), "Backend → NotificationService")
    assert srv.bruno_slug(other) != srv.bruno_slug(srv.bruno_request(POST, "Backend → NotificationService"))


def test_the_collection_is_written_once_and_kept_out_of_git(tmp_path):
    folder = tmp_path / "bruno"
    first = srv.write_bruno_request(folder, srv.bruno_request(GET, "Browser → Backend"))
    second = srv.write_bruno_request(folder, srv.bruno_request(POST, "Backend → NotificationService"))
    again = srv.write_bruno_request(folder, srv.bruno_request(GET, "Browser → Backend"))
    assert json.loads((folder / "bruno.json").read_text()) == {
        "version": "1", "name": "human-review", "type": "collection"}
    assert (folder / ".gitignore").read_text() == "*\n"
    assert again == first and sorted(p.name for p in folder.glob("*.bru")) == sorted(
        [first.name, second.name])
    assert "  seq: 1\n" in first.read_text() and "  seq: 2\n" in second.read_text()
    assert (folder / "environments" / "local.bru").read_text() == (
        "vars {\n  baseUrl: http://localhost:8080\n  notificationServiceUrl:\n}\n")


def test_a_value_the_reader_set_in_the_environment_is_kept(tmp_path):
    folder = tmp_path / "bruno"
    env = folder / "environments" / "local.bru"
    env.parent.mkdir(parents=True)
    env.write_text("vars {\n  baseUrl: http://localhost:9999\n}\n")
    srv.write_bruno_request(folder, srv.bruno_request(GET, "Browser → Backend"))
    srv.write_bruno_request(folder, srv.bruno_request(POST, "Backend → NotificationService"))
    assert env.read_text() == "vars {\n  baseUrl: http://localhost:9999\n  notificationServiceUrl:\n}\n"


def test_a_service_a_compose_file_publishes_gets_its_host_port(tmp_path):
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "docker-compose.yml").write_text(
        "services:\n  backend:\n    ports:\n      - \"127.0.0.1::8080\"\n"
        "  notification:\n    image: n\n    ports:\n      - \"18090:8090\"   # published\n")
    req = srv.bruno_request(POST, "Backend → NotificationService", tmp_path)
    assert (req["var"], req["default"]) == ("notificationServiceUrl", "http://localhost:18090")
    ephemeral = srv.bruno_request(dict(POST, url="http://backend:8080/x"), "Gateway → Backend",
                                  tmp_path)
    assert (ephemeral["var"], ephemeral["default"]) == ("backendUrl", ""), "Docker picks it"
    sms = srv.bruno_request(dict(POST, url="http://sms:80/send"), "NotificationService → SMS gateway")
    assert sms["var"] == "smsGatewayUrl"


# ---- the endpoint -------------------------------------------------------------------------

@pytest.fixture
def bruno(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(srv, "bruno_app", lambda: Path("/Applications/Bruno.app"))
    monkeypatch.setattr(srv, "open_in_bruno", opened.append)
    monkeypatch.setattr(srv, "ROOT", tmp_path)
    return opened


def test_the_endpoint_writes_the_request_and_opens_the_collection_in_bruno(server, bruno, tmp_path):
    status, payload = _call(server, "POST", srv.OPEN_BRUNO,
                            {"request": GET, "title": "Browser → Backend"})
    assert status == 200, payload
    got = json.loads(payload)
    folder = tmp_path / ".human-review" / "bruno"
    assert got == {"collection": str(folder), "file": got["file"], "name": "GET /api/owners/1",
                   "environment": "local", "variable": "baseUrl",
                   "value": "http://localhost:8080"}
    text = (folder / got["file"]).read_text()
    assert "url: {{baseUrl}}/api/owners/1" in text
    assert not re.search(r"traceparent|tracestate|baggage|Origin|Host|63285", text)
    assert bruno == [folder]


def test_the_endpoint_refuses_what_is_not_a_request_and_a_page_it_did_not_serve(server, bruno):
    status, _ = _call(server, "POST", srv.OPEN_BRUNO, {"request": {"method": "GET", "url": "/x"}})
    assert status == 400
    status, _ = _call(server, "POST", srv.OPEN_BRUNO, {"request": GET},
                      {"X-Human-Review-Token": "guess"})
    assert status == 403
    status, _ = _call(server, "POST", srv.OPEN_BRUNO, {"request": GET},
                      {"Sec-Fetch-Site": "cross-site"})
    assert status == 403 and bruno == []


def test_without_bruno_the_endpoint_says_so_and_the_probe_does_not_offer_it(
        server, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "bruno_app", lambda: None)
    monkeypatch.setattr(srv, "open_in_bruno", lambda f: pytest.fail("opened"))
    status, payload = _call(server, "POST", srv.OPEN_BRUNO, {"request": GET})
    assert status == 409 and "Bruno is not installed" in payload
    status, payload = _call(server, "GET", srv.MARKER)
    assert status == 200 and json.loads(payload)["openBruno"] is False
    monkeypatch.setattr(srv, "bruno_app", lambda: Path("/Applications/Bruno.app"))
    assert json.loads(_call(server, "GET", srv.MARKER)[1])["openBruno"] is True


# ---- the page -----------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><style>{css}</style></head><body>
<div class="diagram"><div class="svgbox"><svg xmlns="http://www.w3.org/2000/svg"
  viewBox="0 0 400 100" width="400" height="100">
  <g class="message"><a href="genseq://req"><text font-size="13" x="10" y="30">GET /api/owners/1</text></a></g>
  <g class="message"><a href="genseq://svc"><text font-size="13" x="10" y="50">visit-booked</text></a></g>
  <g class="message"><a href="genseq://sql"><text font-size="13" x="10" y="70">select owners</text></a></g>
</svg></div>
<script type="application/json" class="genseq-details">{details}</script></div>
<script>window.flashed = []; window.copied = [];
window.HR = {{onready: function (fn) {{ fn({caps}); }},
  flash: function (m) {{ window.flashed.push(m); }},
  copy: function (t) {{ window.copied.push(t); return Promise.resolve(); }}}};</script>
<script>{js}</script></body></html>"""

REQUEST_TEXT = ("GET http://127.0.0.1:63285/api/owners/1\nAccept: application/json\n"
                "traceparent: 00-ab-cd-01\n\n{\n  \"id\": 1\n}")
DETAILS = {"version": 3, "details": {
    "req": {"title": "Browser → Backend", "steps": [{"label": "request", "text": REQUEST_TEXT}],
            "request": {"method": "GET", "url": "http://127.0.0.1:63285/api/owners/1",
                        "headers": {"Accept": "application/json", "traceparent": "00-ab-cd-01"}},
            "handler": {"name": "OwnerRestController.getOwner", "href": "vscode://file//x.java:4:1"}},
    "svc": {"title": "Backend → NotificationService", "steps": [{"label": "request", "text":
            "POST http://notification:8090/api/n\nContent-Type: application/json\n\n{}"}],
            "request": {"method": "POST", "url": "http://notification:8090/api/n",
                        "headers": {"Content-Type": "application/json"}, "body": "{\"a\":\"it's\"}"}},
    "sql": {"title": "select owners", "steps": [{"text": "SELECT 1"}]}}}


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


def _page(browser, caps="null"):
    """The panel on a page at a fake http origin, so the served case can fetch for real."""
    html = PAGE.format(css=GENSEQ_CSS, js=GENSEQ_JS, details=json.dumps(DETAILS), caps=caps)
    page = browser.new_page(viewport={"width": 1000, "height": 600})
    sent = []

    def fulfil(route):
        if route.request.url.endswith("/__open_bruno__"):
            sent.append((route.request.headers, json.loads(route.request.post_data)))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"name": "GET /api/owners/1", "environment": "local", "variable": "baseUrl",
                 "value": "http://localhost:8080"}))
        else:
            route.fulfill(status=200, content_type="text/html", body=html)
    page.route("http://hr.test/**", fulfil)
    page.goto("http://hr.test/review.html")
    return page, sent


def _button(page):
    return page.evaluate("""() => { const b = document.querySelector('#genseq-panel .genseq-bruno');
        return b && {hidden: b.hidden, tip: b.getAttribute('data-tip'),
                     logo: !!b.querySelector('svg path[fill="#F4AA41"]'),
                     beforeClose: b.nextElementSibling.classList.contains('genseq-close')}; }""")


def test_off_a_server_the_button_copies_the_request_as_curl(browser):
    page, sent = _page(browser)
    try:
        page.click('a[href="genseq://req"] text')
        b = _button(page)
        assert b == {"hidden": False, "logo": True, "beforeClose": True,
                     "tip": "Copy this request as cURL, to paste into Bruno "
                            "(New Request \u25B8 from cURL)"}
        page.click("#genseq-panel .genseq-bruno")
        page.wait_for_function("window.copied.length === 1")
        assert page.evaluate("window.copied") == [
            "curl 'http://localhost:8080/api/owners/1' -H 'Accept: application/json'"]
        assert page.evaluate("window.flashed") == [
            "cURL copied \u2014 in Bruno: New Request \u25B8 from cURL"]
        page.keyboard.press("Escape")
        page.click('a[href="genseq://svc"] text')
        page.click("#genseq-panel .genseq-bruno")
        page.wait_for_function("window.copied.length === 2")
        assert page.evaluate("window.copied[1]") == (
            "curl -X POST 'http://notification:8090/api/n' -H 'Content-Type: application/json' "
            "--data-raw '{\"a\":\"it'\\''s\"}'"), "a service call keeps its traced address"
        page.keyboard.press("Escape")
        page.click('a[href="genseq://sql"] text')
        assert _button(page)["hidden"] is True, "no request, no button"
        assert sent == []
    finally:
        page.close()


def test_served_the_button_hands_the_request_to_the_server_and_says_where_it_went(browser):
    page, sent = _page(browser, caps='{"openBruno": true, "token": "tok"}')
    try:
        page.click('a[href="genseq://req"] text')
        assert _button(page)["tip"] == "Open this request in Bruno"
        page.click("#genseq-panel .genseq-bruno")
        page.wait_for_function("window.flashed.length === 1")
        headers, body = sent[0]
        assert headers["x-human-review-token"] == "tok"
        assert headers["content-type"] == "application/json"
        assert body == {"request": DETAILS["details"]["req"]["request"], "title": "Browser → Backend"}
        assert page.evaluate("window.flashed") == [
            "Opened in Bruno \u2014 pick GET /api/owners/1 in the sidebar, environment local"]
        assert page.evaluate("window.copied") == []
        assert page.evaluate("!document.querySelector('#genseq-panel').hidden"), "panel stays"
    finally:
        page.close()


def test_the_preview_shows_no_button_it_could_not_press(browser):
    page, _ = _page(browser)
    try:
        page.hover('a[href="genseq://req"] text')
        page.wait_for_timeout(250)
        shown = page.evaluate("""() => getComputedStyle(
            document.querySelector('#genseq-peek .genseq-bruno')).display""")
        assert shown == "none"
    finally:
        page.close()


def test_a_request_step_reads_as_an_http_message(browser):
    page, _ = _page(browser)
    try:
        page.click('a[href="genseq://req"] text')
        parts = page.evaluate("""() => [...document.querySelectorAll('#genseq-panel pre span')]
            .map(s => [s.className, s.textContent])""")
        text = page.evaluate("document.querySelector('#genseq-panel pre').textContent")
        label = page.evaluate("document.querySelector('#genseq-panel .genseq-label').textContent")
        head = page.evaluate("""() => [document.querySelector('#genseq-panel .genseq-title').textContent,
            document.querySelector('#genseq-panel .genseq-handler').textContent]""")
    finally:
        page.close()
    assert text == REQUEST_TEXT, "every character as the sidecar wrote it"
    assert label == "request"
    assert head == ["Browser → Backend:", "OwnerRestController.getOwner()"]
    assert parts[:2] == [["genseq-reqline", "GET http://127.0.0.1:63285/api/owners/1"],
                         ["genseq-verb", "GET"]]
    assert [t for c, t in parts if c == "genseq-hname"] == ["Accept:", "traceparent:"]
    assert ["nt", '"id"'] in parts and ["mi", "1"] in parts, "the body as every JSON payload"
