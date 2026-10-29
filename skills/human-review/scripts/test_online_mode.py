#!/usr/bin/env python3
"""Three copies of the page: off disk, served by us, and online (GitHub Pages).

Online nothing on the building Mac can be reached, so a control that needs it stays on
screen greyed (`html.hr-online` + `data-local`, server.js), a click on it explains instead
of acting, and a `vscode://file/…` reference becomes the same file on GitHub. Off disk and
served nothing of that may show. The page under test is the real scripts, inlined from the
build module, against a fake host per state.

Run with:  python3 -m pytest test_online_mode.py
"""
from __future__ import annotations

import pytest

from test_action_server import build

ROOT = "/r"
BLOB = "https://github.com/o/p/blob/abc"
BODY = (
    f'<!doctype html><html data-hr-head="abc" data-hr-root="{ROOT}" data-hr-blob="{BLOB}" '
    'data-hr-dir=".human-review"><head><style>' + build.CSS + '</style>' + build.SERVER_JS
    + '</head><body>'
    '<button type="button" class="chip chip-mode chip-serve copycmd" id="hr-serve" '
    'data-copy="cd /r" data-tip-online="To see it work: click Serve." data-tip="Copy">'
    '<span class="mode-word">Static</span><span class="mode-sep">·</span>📋 Serve</button>'
    + build.command_html("cd /r && ./start-docker.sh up", None, label="Start")
    + '<button type="button" class="adopt copycmd" data-copy="a prompt" data-tip="Prompt">'
      'Prompt</button>'
    f'<a id="src" class="srcref" href="vscode://file/{ROOT}/src/A.java:12:1" '
    'data-tip="Open in VS Code: src/A.java">A.java:12</a>'
    f'<a id="own" class="dgm-src" href="vscode://file/{ROOT}/.human-review/assets/c2/C.puml:1:1">'
    'C.puml</a>'
    '<a id="desk" class="dgm-edit" href="drawio:///r/D.drawio.png">Edit in app</a>'
    '<a id="web" class="dgm-edit" href="https://app.diagrams.net/#R">Edit on web</a>'
    '<span class="takeaway-online">Browse: <a id="online" '
    'href="https://victorrentea.github.io/human-review/demo/review.html">demo 1</a> · '
    '<a id="online2" href="https://victorrentea.github.io/human-review/demo2/review.html">'
    'demo 2</a>. </span>'
    '<div class="diskline">/r/.human-review/review.html</div>'
    + build.TIP_JS + build.EDITOR_JS + '</body></html>')

STATE = """() => {
  const q = s => document.querySelector(s), op = s => getComputedStyle(q(s)).opacity;
  return {online: document.documentElement.classList.contains('hr-online'),
          start: op('.cmd-copy'), desk: op('#desk'), web: op('#web'), adopt: op('.adopt'),
          src: q('#src').getAttribute('href'), srcTip: q('#src').getAttribute('data-tip'),
          own: q('#own').getAttribute('href'), disk: getComputedStyle(q('.diskline')).display,
          browse: !document.getElementById('online') && !!document.getElementById('online2'),
          serveTip: q('#hr-serve') && q('#hr-serve').getAttribute('data-tip'),
          mode: (q('.chip-mode') || {}).textContent};
}"""


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


def _open(browser, url, probe=None):
    """The page at `url`, the host answering the probe with `probe` (None: a 404)."""
    # The clipboard is the page's own business; headless, an ungranted write never settles.
    ctx = browser.new_context(permissions=[] if url.startswith("file:")
                              else ["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    errors, probes = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def serve(route):
        u = route.request.url
        if "/__human_review__" in u:
            probes.append(u)
            if probe:
                return route.fulfill(body=probe, content_type="application/json")
        if u.endswith("/review.html"):
            return route.fulfill(body=BODY, content_type="text/html; charset=utf-8")
        return route.fulfill(status=404, body="")
    page.route("**/*", serve)
    page.goto(url)
    page.wait_for_timeout(300)
    return page, errors, probes


def _toast(page):
    return page.evaluate("() => { const t = document.getElementById('copy-toast');"
                         " return t && t.classList.contains('shown') ? t.textContent : null }")


def _toast_says(page, text, timeout=2000):
    """The toast once it says `text`, or whatever it says when `timeout` ms have passed.

    A copy toast is shown from the `.then` of the clipboard write, so it is never there
    the instant the click returns. Chromium 151 (Playwright 1.62) happened to reject an
    ungranted write before `click` came back; Chromium 156 (Playwright 1.64) rejects it a
    few milliseconds later, and a test that read the toast at once saw nothing."""
    try:
        page.wait_for_function(
            "t => { const e = document.getElementById('copy-toast');"
            " return !!e && e.classList.contains('shown') && e.textContent === t }",
            arg=text, timeout=timeout)
    except Exception:                                # the assertion below says what it got
        pass
    return _toast(page)


def test_off_disk_every_script_runs_and_the_copy_buttons_copy(browser, tmp_path):
    """Off disk the probe settles before editor.js runs, so `onready` calls back at once:
    registered above `var connected`, it threw, and every click handler below it was lost —
    no copy button on a file:// page copied anything (19 Oct 2026)."""
    f = tmp_path / "review.html"
    f.write_text(BODY, encoding="utf-8")
    page, errors, _ = _open(browser, f.as_uri())
    assert errors == []
    s = page.evaluate(STATE)
    assert not s["online"] and s["start"] == "1" and s["src"].startswith("vscode:")
    assert s["disk"] != "none"
    assert s["serveTip"].startswith("Static copy: buttons copy commands. Click to start")
    # Off disk nothing grants the clipboard, so the write is refused and the page falls
    # back on `execCommand('copy')` over a hidden textarea: that is what has to carry the
    # prompt, and the toast only follows it.
    page.evaluate("""() => { const run = document.execCommand.bind(document);
      window.copied = [];
      document.execCommand = c => { if (c === 'copy') window.copied.push(
        document.activeElement && document.activeElement.value); return run(c); }; }""")
    page.click(".adopt")
    assert _toast_says(page, "Copied — paste it in a terminal") \
        == "Copied — paste it in a terminal"
    assert page.evaluate("window.copied") == ["a prompt"]


def test_online_greys_what_needs_the_mac_and_sends_references_to_github(browser):
    page, errors, probes = _open(
        browser, "https://victorrentea.github.io/human-review/demo/review.html")
    assert errors == []
    assert probes == [], "no review server ever listens off loopback: nothing to ask"
    s = page.evaluate(STATE)
    assert s["online"]
    assert s["start"] == "0.45" and s["desk"] == "0.45", "greyed, still on screen"
    assert s["web"] == "1" and s["adopt"] == "1", "what works online stays as it is"
    assert s["src"] == f"{BLOB}/src/A.java#L12" and s["srcTip"] == "Open on GitHub: src/A.java"
    assert s["own"].startswith("vscode:"), "the review's own folder is not on GitHub"
    assert s["disk"] == "none" and s["browse"], "no path of the Mac, no link to itself, the other demo linked"
    assert s["serveTip"] == "To see it work: click Serve."
    assert s["mode"] == "Static·📋 Serve", "one button: the state, then the way out of it"
    page.click(".cmd-copy", force=True)
    assert _toast(page) == build.LOCAL_HINT, "a click explains instead of copying"
    page.hover("#desk")
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('.tip.visible').textContent") \
        == build.LOCAL_HINT
    page.click(".adopt")
    assert _toast_says(page, "Copied — paste it in a terminal") \
        == "Copied — paste it in a terminal"


def test_served_nothing_is_greyed(browser):
    page, errors, probes = _open(browser, "http://127.0.0.1:9/review.html",
                                 probe='{"humanReview": 1, "token": "t"}')
    assert errors == [] and probes
    s = page.evaluate(STATE)
    assert not s["online"] and s["start"] == "1" and s["desk"] == "1"
    assert s["src"].startswith("vscode:")
    # The one `Static · 📋 Serve` button became the one `Served` badge, in its place.
    assert s["serveTip"] is None and s["mode"].startswith("Served")


def test_on_loopback_without_our_server_it_is_online_too(browser):
    """`python3 -m http.server` beside the page is GitHub Pages' situation exactly."""
    page, errors, probes = _open(browser, "http://127.0.0.1:9/review.html")
    assert errors == [] and probes
    s = page.evaluate(STATE)
    assert s["online"] and s["start"] == "0.45" and s["src"].startswith(BLOB)
