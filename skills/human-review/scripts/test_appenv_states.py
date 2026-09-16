#!/usr/bin/env python3
"""The deployed-app row has four states and none of them is in the markup.

Everything the reader sees in that row — "Offline" or an address, `Start` or `Stop` and
`Reset DB`, the terminal command or none of it — is decided at runtime by APP_ENV_JS out
of two facts it cannot know at build time: whether this copy of the report is being
served, and whether anything is answering at the base. So the markup tests in
`test_build_review.py` can only pin what is *available*; what is actually *shown* needs a
browser, which is what this file is.

The stubs are the two facts and nothing else: `window.HR.can()` decides served, and a
replaced `fetch` decides live. The script under test is the real one, inlined from the
build module, so a change to it is caught here rather than in a review of the diff.

Run with:  python3 -m pytest test_appenv_states.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

RUNTIME = {"command": "./start-docker.sh up --ref abc123",
           "base": "http://localhost:4200",
           "stop": "./start-docker.sh down --ref abc123",
           "reset": "/__reset"}

# What the reader can see, not what the page contains: `hidden` is how the script takes a
# verb away and `display:none` is how the stylesheet takes a whole half of the row away,
# and a test that checked only one of the two would pass on a bar that shows both halves.
PROBE = """() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && !el.hidden && getComputedStyle(el).display !== 'none';
  const url = q('.appenv-url'), state = q('.appenv-state');
  return {
    state: vis(state) ? state.textContent : null,
    url: vis(url) ? [url.textContent, url.getAttribute('href'), url.target] : null,
    start: vis(q('.appenv-start')),
    stop: vis(q('.appenv-stop')),
    reset: vis(q('.appenv-reset')) ? q('.appenv-reset').textContent : null,
    command: vis(q('.appenv-manual')) ? q('.appenv-manual code').textContent : null,
  };
}"""


def _page(served: bool, live: bool) -> str:
    # On `window` and not `const`: `set_content` writes into the same global scope every
    # time, and a second `const SERVED` there is a SyntaxError that kills the whole script
    # — leaving the page showing the *previous* case's row, which reads as a bug in the bar.
    stub = """<script>
window.SERVED = %s; window.LIVE = %s;
window.HR = {onready: fn => setTimeout(fn, 0), can: () => window.SERVED,
             run: () => Promise.resolve({state: 'done', result: {}}), tail: () => ''};
window.fetch = () => window.LIVE ? Promise.resolve({ok: true})
                                 : Promise.reject(new Error('down'));
</script>""" % (json.dumps(served), json.dumps(live))
    return ("<!doctype html><meta charset=utf-8><style>" + build.CSS + "</style>"
            + stub + build.runtime_html(RUNTIME) + build.APP_ENV_JS)


@pytest.fixture(scope="module")
def row():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:                       # no chromium installed on this box
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page()

        # `data-state` is "unknown" while the probe is in flight and only ever leaves it
        # for an answer, so it is the one honest signal that the row has settled. Waited
        # for twice: `onready` lands after the first probe and re-probes, because learning
        # the page is served changes the answer for two of the three verbs.
        settled = ("() => document.querySelector('.appenv-state').dataset.state"
                   " !== 'unknown'")

        def read(served, live):
            page.set_content(_page(served, live))
            page.wait_for_function(settled)
            page.wait_for_timeout(60)
            page.wait_for_function(settled)
            return page.evaluate(PROBE)

        yield read
        browser.close()


def test_offline_says_offline_and_shows_no_address(row):
    """A URL printed next to a dead port is the one thing in this row that can waste a
    reader's afternoon: it looks like the app and answers like nothing."""
    seen = row(served=True, live=False)
    assert seen["state"] == "Offline"
    assert seen["url"] is None
    # The one verb that changes what the row just said, and nothing that acts on an app
    # that is not there.
    assert seen["start"] and not seen["stop"] and seen["reset"] is None


def test_live_shows_the_address_as_a_link_into_a_new_tab(row):
    """Port and all — the host picks it at start time, so this is the only place it is
    ever written down — and the pill steps out of the way, because "live at" in front of
    an address is a word the address already says."""
    seen = row(served=True, live=True)
    assert seen["state"] is None
    assert seen["url"] == ["http://localhost:4200", "http://localhost:4200", "_blank"]
    assert not seen["start"] and seen["stop"] and seen["reset"] == "Reset DB"


def test_off_disk_the_command_stands_where_the_verbs_would_be(row):
    """No process here runs a command, so Start and Stop cannot work and Reset has nothing
    to reset. One offer, in the register this copy of the report can honour."""
    seen = row(served=False, live=False)
    assert seen["state"] == "Offline"
    assert not seen["start"] and not seen["stop"] and seen["reset"] is None
    assert seen["command"] == RUNTIME["command"]


def test_off_disk_an_app_that_is_up_is_still_reported(row):
    """The transcript's links are aimed at whatever answers at the base, so a reader
    reading this file straight off disk with the app running has to be told it is running
    — otherwise every link in the narration looks dead while it works."""
    seen = row(served=False, live=True)
    assert seen["url"] == ["http://localhost:4200", "http://localhost:4200", "_blank"]
    assert seen["command"] == RUNTIME["command"], "still the only way to have started it"
