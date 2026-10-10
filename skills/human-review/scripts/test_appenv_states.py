#!/usr/bin/env python3
"""The deployed-app row has four states and none of them is in the markup.

Everything the reader sees in that row — "Offline" or an address, which of `Start` and `Stop`
are on screen, `Reset DB`, and whether each verb wears a clipboard or its own
mark — is decided at runtime by APP_ENV_JS and SERVER_JS out of two facts neither can know
at build time: whether this copy of the report is being served, and whether anything is
answering at the base. So the markup tests in `test_build_review.py` can only pin what is
*available*; what is actually *shown* needs a browser, which is what this file is.

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
           "urlCommand": "./start-docker.sh url petclinic-abc123",
           "reset": "/__reset"}
#: The fixtures the build found: the seed, then one per `db/fixtures/*.sql`.
FIXTURES = [("", "#8b929c"), ("green", "#2fa84f"), ("busy-day", "#3b82f6")]

# What the reader can see, not what the page contains: `hidden` is how the script takes a
# verb away and `display:none` is how the stylesheet takes a whole half of the row away,
# and a test that checked only one of the two would pass on a bar that shows both halves.
PROBE = """() => {
  const q = s => document.querySelector(s);
  const vis = el => !!el && !el.hidden && getComputedStyle(el).display !== 'none';
  const url = q('.appenv-url'), state = q('.appenv-state');
  // One control per verb now, and what a reader can see of it is three things: whether the
  // verb is in the row at all, what the button says, and which of its two faces is up —
  // the clipboard, or the verb's own mark. The row used to be two rows: word buttons that
  // only worked served, and under them the same three commands again as clipboards.
  const verb = name => {
    const wrap = q('.appenv-' + name);
    if (!vis(wrap)) return null;
    const copy = wrap.querySelector('.cmd-copy'), run = wrap.querySelector('.cmd-run');
    return {word: wrap.querySelector('.cmd-word').textContent,
            copies: vis(copy) ? copy.getAttribute('data-copy') : null,
            runs: vis(run) ? run.getAttribute('data-action') : null,
            mark: vis(run) ? run.querySelector('.cmd-ico').textContent : null};
  };
  return {
    state: vis(state) ? state.textContent : null,
    url: vis(url) ? [url.textContent, url.getAttribute('href'), url.target] : null,
    start: verb('start'), stop: verb('stop'), where: q('.appenv-where'),
    // The DB Fixture row: its lead words, the fixtures it names, and per fixture whether
    // its Seed can be pressed and what its hover says.
    to: vis(q('.appenv-fixtures-to')) ? q('.appenv-fixtures-to').textContent : null,
    fixtures: [...document.querySelectorAll('.appenv-fx')].filter(vis)
                .map(f => f.querySelector('.appenv-fx-name').textContent),
    seeds: [...document.querySelectorAll('.appenv-fx .appenv-reset')].filter(vis)
             .map(b => [b.dataset.fixture, b.getAttribute('aria-disabled') !== 'true',
                        b.dataset.tip]),
  };
}"""


def _page(served: bool, live: bool, hang: bool = False, slow_probe: bool = False,
          fail: bool = False, fixtures: dict | None = None, found: str = "") -> str:
    """`hang` is a command that never finishes — which is the state worth looking at, and
    the only one the settled-state tests below can never catch: `docker compose up` on a
    cold cache is minutes of the row saying "Starting…" and nothing else. `slow_probe`
    is the health check hanging, which holds the row in `checking…` to be looked at.
    `fail` is a command that exits non-zero, printing one last line. `found` is the host
    knowing of an instance at that address, the only one that answers."""
    # On `window` and not `const`: `set_content` writes into the same global scope every
    # time, and a second `const SERVED` there is a SyntaxError that kills the whole script
    # — leaving the page showing the *previous* case's row, which reads as a bug in the bar.
    stub = """<script>
window.SERVED = %s; window.LIVE = %s; window.HANG = %s; window.SLOW = %s;
window.FAIL = %s; window.FIXTURES = %s; window.FOUND = %s; window.ASKED = [];
window.HR = {onready: fn => setTimeout(fn, 0), can: () => window.SERVED,
             run: id => (window.ASKED.push(id), window.HANG) ? new Promise(() => {})
                      : window.FAIL ? Promise.resolve({state: 'failed', exit: 1, result: {},
                                                       output: 'context not found\\n'})
                                    : Promise.resolve({state: 'done',
                                                       result: {base: window.FOUND}}),
             tail: snap => (snap && snap.output || '').trim()};
// `GET <reset>` is the environment listing its fixtures; `null` is an environment from
// before fixtures, whose answer has no list in it.
window.fetch = url => window.SLOW ? new Promise(() => {})
  : window.FOUND ? (url.startsWith(window.FOUND)
                    ? Promise.resolve({ok: true, json: () => Promise.resolve({ok: true})})
                    : Promise.reject(new Error('down')))
  : !window.LIVE ? Promise.reject(new Error('down'))
  : /__reset$/.test(url) && window.FIXTURES
    ? Promise.resolve({ok: true, json: () => Promise.resolve(window.FIXTURES)})
  : Promise.resolve({ok: true, json: () => Promise.resolve({ok: true})});
</script>""" % (json.dumps(served), json.dumps(live), json.dumps(hang),
                json.dumps(slow_probe), json.dumps(fail), json.dumps(fixtures),
                json.dumps(found))
    return ("<!doctype html><meta charset=utf-8><style>" + build.CSS + "</style>"
            + stub + build.runtime_html(RUNTIME, fixtures=FIXTURES) + build.APP_ENV_JS)


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

        def read(served, live, fixtures=None, found=""):
            page.set_content(_page(served, live, fixtures=fixtures, found=found))
            page.wait_for_function(settled)
            page.wait_for_timeout(60)
            page.wait_for_function(settled)
            return page.evaluate(PROBE)

        read.page = page                 # for the one test that clicks and looks mid-flight
        read.html = _page
        yield read
        browser.close()


def test_offline_says_offline_and_shows_no_address(row):
    """A URL printed next to a dead port is the one thing in this row that can waste a
    reader's afternoon: it looks like the app and answers like nothing."""
    seen = row(served=True, live=False)
    assert seen["state"] == "Offline"
    assert seen["url"] is None
    # The verb that changes what the row just said. Stop with nothing to stop can only
    # fail, and there is no Where: the row asked the host itself before saying Offline.
    assert seen["start"]["word"] == "Start App in Docker"
    assert seen["stop"] is None and seen["where"] is None
    # The fixtures stay on screen with nothing up, every Seed greyed with the reason.
    assert seen["to"] == "DB Fixture:"
    assert seen["fixtures"] == ["Default", "green", "busy-day"]
    assert seen["seeds"] == [[n, False, "Start the app first"] for n, _ in FIXTURES]
    # Which face that verb wears is SERVER_JS's answer, per action, and this page stubs
    # SERVER_JS out on purpose — see test_command_html.py for the raising, and the real
    # served page for the effect. Here it is still the clipboard.
    assert seen["start"]["copies"] == RUNTIME["command"]
    assert seen["start"]["runs"] is None


def test_served_it_finds_an_app_already_running_elsewhere(row):
    """Nothing answers at the address this browser remembers, but the stack is up — started
    from a terminal, or from another tab. Served, the row asks the host itself and lands on
    the link; there is no Where button to press any more."""
    seen = row(served=True, live=False, found="http://localhost:53421")
    assert seen["state"] is None
    assert seen["url"] == ["http://localhost:53421", "http://localhost:53421", "_blank"]
    assert seen["stop"]["word"] == "Stop" and seen["start"] is None
    assert row.page.evaluate("window.ASKED") == ["demo-env-url"], "asked once, and only that"


def test_off_disk_the_host_is_never_asked(row):
    """A file cannot run the host's `url` command, and must not pretend to."""
    seen = row(served=False, live=False, found="http://localhost:53421")
    assert seen["state"] == "Offline" and seen["url"] is None
    assert row.page.evaluate("window.ASKED") == []


def test_live_shows_the_address_as_a_link_into_a_new_tab(row):
    """Port and all — the host picks it at start time, so this is the only place it is
    ever written down — and the pill steps out of the way, because "live at" in front of
    an address is a word the address already says."""
    seen = row(served=True, live=True)
    assert seen["state"] is None
    assert seen["url"] == ["http://localhost:4200", "http://localhost:4200", "_blank"]
    # Start is gone and Stop takes its place.
    assert seen["start"] is None
    assert seen["stop"]["word"] == "Stop" and seen["where"] is None
    # Every Seed is armed, and says what it puts back.
    assert [x[1] for x in seen["seeds"]] == [True, True, True]
    assert seen["seeds"][0][2] == "Reset the DB to the seed, the starting data"
    assert "Empty the DB, then load the \u201cgreen\u201d fixture" in seen["seeds"][1][2]


def test_the_fixtures_are_the_builds_not_the_running_apps(row):
    """Victor, 8 Oct 2026: an instance started from an older image listed only Default,
    and the row shrank to it while green.sql sat in the repository. The names are the
    build's now; the environment's list only greys a Seed it could not honour."""
    seen = row(served=True, live=True,
               fixtures={"ok": True, "fixtures": ["green"], "current": "seed"})
    assert seen["fixtures"] == ["Default", "green", "busy-day"]
    armed = {n: on for n, on, _ in seen["seeds"]}
    assert armed == {"": True, "green": True, "busy-day": False}
    tip = [t for n, _, t in seen["seeds"] if n == "busy-day"][0]
    assert "no \u201cbusy-day\u201d fixture" in tip
    # A fixture the app does not list is never added: the row is the build's alone.
    seen = row(served=True, live=True, fixtures={"ok": True, "fixtures": ["green", "extra"]})
    assert seen["fixtures"] == ["Default", "green", "busy-day"]


def test_a_fixture_the_environment_describes_carries_its_description(row):
    """A sidecar may describe a fixture — `{name, about}` or a top-level `about` map — and
    the Seed's hover carries that description."""
    seen = row(served=True, live=True,
               fixtures={"ok": True, "about": {"busy-day": "40 visits today"},
                         "fixtures": [{"name": "green", "about": "the Weasley household"},
                                      "busy-day"]})
    tips = {n: t for n, _, t in seen["seeds"]}
    assert tips["green"].endswith(": the Weasley household")
    assert tips["busy-day"].endswith(": 40 visits today")
    lead = row.page.evaluate("document.querySelector('.appenv-fixtures-to').dataset.tip")
    assert "the seed plus a fixture" in lead


def test_a_seed_press_posts_that_fixture_and_never_changes_the_buttons_width(row):
    page = row.page
    page.set_content(row.html(served=True, live=True))
    page.wait_for_function("() => document.querySelector('.appenv-state').dataset.state"
                           " === 'live'")
    page.evaluate("""() => { window.POSTS = []; const f = window.fetch;
        window.fetch = (u, o) => (o && o.method === 'POST' ? (POSTS.push(u),
            new Promise(r => setTimeout(() => r({ok: true}), 300))) : f(u, o)); }""")
    seeds = page.locator(".appenv-fx .appenv-reset")
    w = seeds.nth(1).bounding_box()["width"]
    x = seeds.nth(2).bounding_box()["x"]
    seeds.nth(1).click()
    assert page.evaluate("POSTS") == ["http://localhost:4200/__reset/green"]
    assert seeds.nth(1).text_content() == "Seed\u2026"
    assert seeds.nth(1).bounding_box()["width"] == w
    assert seeds.nth(2).bounding_box()["x"] == x, "the chips after it do not move"
    page.wait_for_function("() => document.querySelectorAll('.appenv-fx .appenv-reset')[1]"
                           ".textContent === 'Seed \u2713'")


def test_the_fixtures_stay_when_the_app_goes(row):
    """Down, there is nothing to seed: every Seed greys, and the names stay."""
    seen = row(served=True, live=False,
               fixtures={"ok": True, "fixtures": ["green"], "current": "green"})
    assert seen["fixtures"] == ["Default", "green", "busy-day"]
    assert all(not on and tip == "Start the app first" for _, on, tip in seen["seeds"])


def test_off_disk_every_verb_is_on_screen_as_its_own_clipboard(row):
    """No process here runs a command, so no verb can run and Reset has nothing to reset.
    What is left is one control per command, wearing the clipboard — which is the honest
    statement that this copy of the report cannot run it, made once instead of twice.

    Both, and not only the one that applies to the current state: the reader is pasting
    one of these into a terminal, where what is up and what is not is their business and not
    this page's. The row used to hide two thirds of them behind a health check, in a second
    row underneath, labelled `START` `STOP` `WHERE` and wearing the *rerun* mark."""
    seen = row(served=False, live=False)
    assert seen["state"] == "Offline"
    assert seen["fixtures"] == ["Default", "green", "busy-day"]
    assert not any(on for _, on, _ in seen["seeds"])
    assert [seen[v]["word"] for v in ("start", "stop")] == ["Start App in Docker", "Stop"]
    assert [seen[v]["copies"] for v in ("start", "stop")] == [RUNTIME["command"], RUNTIME["stop"]]
    assert seen["where"] is None, "the host's `url` command is never a button"
    assert all(seen[v]["runs"] is None for v in ("start", "stop")), \
        "nothing here can run, so no verb may wear a mark that says it can"


def test_off_disk_an_app_that_is_up_is_still_reported(row):
    """The transcript's links are aimed at whatever answers at the base, so a reader
    reading this file straight off disk with the app running has to be told it is running
    — otherwise every link in the narration looks dead while it works."""
    seen = row(served=False, live=True)
    assert seen["url"] == ["http://localhost:4200", "http://localhost:4200", "_blank"]
    assert seen["start"]["copies"] == RUNTIME["command"], \
        "still the only way to have started it"
    # And Stop is still on screen with the app up, which is the case the old row got
    # backwards: it hid Stop off disk, where it is the line most worth pasting.
    assert seen["stop"]["copies"] == RUNTIME["stop"]


SPINNER = """() => {
  const s = document.querySelector('.appenv-state');
  const ring = getComputedStyle(s, '::before');
  return {text: s.textContent, size: ring.width, radius: ring.borderRadius,
          opacity: ring.opacity, spins: ring.animationName.split(', ')[0],
          delay: ring.animationDelay,
          lit: ring.borderTopColor !== ring.borderLeftColor,
          turn: ring.transform};
}"""


def test_a_command_in_flight_spins_beside_the_word(row):
    """"Starting…" can stand there for a whole docker build. Text that never moves is how
    a page says it has hung, and the last line the command printed is in a tooltip you
    have to know to go looking for — so the ring is the row's only visible evidence that
    something is still happening."""
    page = row.page
    page.set_content(row.html(served=True, live=False, hang=True))
    page.wait_for_function("() => document.querySelector('.appenv-start').hidden === false")
    # SERVER_JS is what raises the run half on a real served page, and this harness stubs
    # it out — so the one line of it that matters here is done by hand, and the click lands
    # on the same button a reader's would.
    page.evaluate("() => { document.querySelector('.appenv-start .cmd-run').hidden = false;"
                  " document.querySelector('.appenv-start .cmd-copy').hidden = true; }")
    page.click(".appenv-start .cmd-run")
    page.wait_for_function("() => document.querySelector('.appenv-state')"
                           ".textContent.startsWith('Starting')")
    page.wait_for_timeout(600)                   # past the 300ms the ring stays invisible
    seen = page.evaluate(SPINNER)
    assert seen["text"].startswith("Starting")
    assert seen["radius"] == "50%" and seen["size"] != "auto", "a ring, and it has a size"
    assert seen["lit"], "one arc in the link colour, or it reads as a static circle"
    assert seen["opacity"] == "1" and seen["spins"] == "appenv-spin"
    # Drawn in CSS, so it themes with the page and this report stays one file that works
    # off disk — an <img> would need an asset beside it.
    assert "appenv-spin" in build.CSS and "<img" not in build.runtime_html(RUNTIME)
    was = seen["turn"]
    page.wait_for_timeout(150)
    assert page.evaluate(SPINNER)["turn"] != was, "and it actually turns"


def test_a_start_that_fails_says_so_and_keeps_saying_it(row):
    """A `start-docker.sh` that dies in two seconds used to leave no trace: the failure was
    written into the pill, and the health check fired right after it answered `Offline`
    a moment later, over the top of it. What the reader saw was a button that did nothing.
    The failure has to outlive that probe, and carry the line the command died on."""
    page = row.page
    page.set_content(row.html(served=True, live=False, fail=True))
    page.wait_for_function("() => document.querySelector('.appenv-start').hidden === false")
    page.evaluate("() => { document.querySelector('.appenv-start .cmd-run').hidden = false;"
                  " document.querySelector('.appenv-start .cmd-copy').hidden = true; }")
    page.click(".appenv-start .cmd-run")
    page.wait_for_function("() => document.querySelector('.appenv-state')"
                           ".textContent === 'start failed'")
    page.wait_for_timeout(200)                   # the probe's own answer has landed by now
    state = page.evaluate("() => { const s = document.querySelector('.appenv-state');"
                          " return [s.textContent, s.dataset.tip, s.hidden]; }")
    assert state == ["start failed", "context not found", False]
    assert page.evaluate(PROBE)["start"]["word"] == "Start App in Docker", \
        "and Start is still there to be pressed again"


def test_the_ring_is_invisible_for_its_first_300ms(row):
    """`unknown` is also the state of the health check on load, and that answers in
    milliseconds. A ring that appears and vanishes once per page load is the page
    twitching, not the page working — so a probe that beats the delay never reaches it,
    and only a wait long enough to be worth reporting is reported.

    Held in `checking…` by a health check that never answers, because a wait measured
    against a real one would be a test that fails on a slow morning."""
    page = row.page
    page.set_content(row.html(served=True, live=False, slow_probe=True))
    page.wait_for_function("() => document.querySelector('.appenv-state')"
                           ".textContent.startsWith('checking')")
    # The delay is read off the rule rather than sampled against the clock: a test that
    # asserts "still invisible 200ms in" is a test that fails on a busy morning.
    assert page.evaluate(SPINNER)["delay"] == "0s, 0.3s", "the ring turns from the start" \
                                                          " and fades in late"
    page.wait_for_timeout(600)
    assert page.evaluate(SPINNER)["opacity"] == "1", "a wait this long has earned it"
