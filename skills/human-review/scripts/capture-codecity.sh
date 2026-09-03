#!/usr/bin/env bash
# Screenshot the Code City with this branch's change set lit up.
#
# codecity.html opens in "show everything" mode, which is the right default for
# exploring but the wrong one for a review: the reviewer wants the branch's
# buildings to jump out of the skyline. This drives the page headlessly, flips the
# Changes knob to "highlight changed", waits for the WebGL frame to settle, and
# saves the initial render as a PNG for the review guide to embed — the image is
# the map, the click-through to codecity.html is the territory.
#
# The guide's prose used to explain what a building is, what its height means and
# what its colour means, right next to the picture. That is a caption doing the
# picture's job. codecity.html already carries that explanation itself: a first-run
# "What each building tells you" tour that draws AREA/HEIGHT/COLOR (and CHANGED, when
# there is one) as callout cards wired by dashed leaders to a real building on one end
# and the control that sets it on the other. It builds itself automatically — this
# script used to click it away ("Dismiss the first-run intro card") so it would not
# sit on the skyline; now it leaves it up and shoots with it on screen instead, so the
# picture is self-explanatory without a drawn-on legend that could drift from what the
# renderer actually does.
#
# Regenerate the city first if the branch moved: petclinic-backend/docs/generate-codecity.sh
#
# Usage:
#   scripts/capture-codecity.sh [out.png] [mode]
#     mode: highlight (default) | hide | off
set -euo pipefail

# The project under review is where this was *invoked*, never where the script lives:
# the skill is installed from its own repository and reached through a symlink, so
# resolving from `$BASH_SOURCE` would find the skill's checkout instead of the project.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel)"

OUT="${1:-$ROOT/.human-review/assets/codecity.png}"
MODE="${2:-highlight}"

CITY="$ROOT/petclinic-backend/docs/generated/codecity/codecity.html"
NODE_PATHS="$ROOT/petclinic-test/node_modules"

[ -f "$CITY" ] || { echo "[codecity] $CITY missing — run petclinic-backend/docs/generate-codecity.sh" >&2; exit 2; }
[ -d "$NODE_PATHS/playwright" ] || { echo "[codecity] playwright not installed (cd petclinic-test && npm install)" >&2; exit 2; }

mkdir -p "$(dirname "$OUT")"

NODE_PATH="$NODE_PATHS" node -e '
const {chromium} = require("playwright");
const [city, out, mode] = process.argv.slice(1);

(async () => {
  // WebGL in headless Chromium needs the SwiftShader fallback, otherwise the
  // canvas comes back blank and the screenshot is an empty grey rectangle.
  const browser = await chromium.launch({args: ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]});
  const page = await browser.newPage({viewport: {width: 1600, height: 1000}, deviceScaleFactor: 2});
  const problems = [];
  page.on("pageerror", e => problems.push(String(e)));

  await page.goto("file://" + city, {waitUntil: "load"});
  await page.waitForSelector("#changeMode");

  // codecity.html builds its "What each building tells you" tour synchronously while
  // its own module script runs (buildIntro() is a direct call, not just a scheduled
  // one) — by the time `load` fires and this evaluate() runs, #intro already exists,
  // built against the pages own DEFAULT #changeMode option ("highlight", the same
  // default this script uses). There is no query flag or localStorage key to ask for
  // the tour some other way — that IS how the page intends it to be driven: land on
  // it with data already loaded, and it is up.
  //
  // The tour is dismiss-ON-INTERACTION: the "change" listener on #changeMode itself
  // calls dismissIntro() unconditionally, before it does anything else. So the driving move
  // here is restraint — only touch the control when the requested mode actually
  // differs from what is already selected. For the default "highlight" mode that means
  // not dispatching a change event at all; for "hide"/"off" it means accepting that the
  // page itself will drop the tour the moment the mode is switched, same as it would
  // for a person.
  const result = await page.evaluate((mode) => {
    const sel = document.getElementById("changeMode");
    const valid = [...sel.options].map(o => o.value);
    // A renamed <option> makes this a silent no-op: the event still fires, the screenshot is
    // still taken, and the guide embeds a city with nothing highlighted at all.
    if (!valid.includes(mode)) {
      throw new Error("#changeMode has no option " + JSON.stringify(mode) + " (has: " + valid.join(", ") + ")");
    }
    let interacted = false;
    if (sel.value !== mode) {
      sel.value = mode;
      sel.dispatchEvent(new Event("change", {bubbles: true}));
      interacted = true;
    }
    return { changed: document.getElementById("changeCount")?.textContent?.trim() || "", interacted };
  }, mode);
  const changed = result.changed;

  // Two rAF-worth of settle time for the layout animation plus the label pass.
  await page.waitForTimeout(2500);

  // Fail loudly rather than silently shipping a shot without the legend — the same
  // rule the #changeMode check above already follows.
  const introVisible = await page.evaluate(() =>
    document.getElementById("intro")?.classList.contains("visible") || false);
  if (!introVisible) {
    if (result.interacted) {
      throw new Error(`switching #changeMode to ${JSON.stringify(mode)} dismisses the built-in tour `
          + "by design (its change listener calls dismissIntro() unconditionally) — the tour and a "
          + "non-default Changes mode cannot both be in one shot; capture mode \"highlight\" (the "
          + "pages own default) to get the tour");
    }
    throw new Error("#intro (the built-in \"What each building tells you\" tour) is not showing "
        + "— codecity.html may have changed how/when it builds itself");
  }

  await page.screenshot({path: out});
  await browser.close();

  if (problems.length) console.error("[codecity] page errors: " + problems.join(" | "));
  if (mode !== "off" && !/[1-9]/.test(changed)) {
    throw new Error(`the city highlighted nothing (changeCount="${changed}") — regenerate it `
        + "for this branch before capturing");
  }
  // stdout, so the caller can put a MEASURED number under the image instead of typing one.
  console.log(changed);
  console.error(`[codecity] ${changed || "?"} — wrote ${out}`);
})().catch(e => { console.error("[codecity] " + e.message); process.exit(1); });
' "$CITY" "$OUT" "$MODE"

# The guide is served with .human-review/ as the document root (see serve-review.py), and
# SimpleHTTPRequestHandler collapses "..", so a link to ../petclinic-backend/... 404s. Copy
# the live city in beside the PNG: the click-through works, and the folder survives being
# zipped and mailed as one thing.
CITY_DIR="$(dirname "$OUT")/codecity"
mkdir -p "$CITY_DIR"
cp "$CITY" "$CITY_DIR/codecity.html"
echo "[codecity] copied the live view -> $CITY_DIR/codecity.html (link to assets/codecity/codecity.html)" >&2
