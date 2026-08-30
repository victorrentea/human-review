#!/usr/bin/env bash
# Film the feature working, in a real browser, for the review guide.
#
# The first version of this replayed the Playwright acceptance test and kept its video.
# That is the purer idea — the test IS the demo — but headless it finishes in ~1s and the
# retained .webm shows only the final assertion, which tells a reviewer nothing about the
# interaction. So this drives the flow through the same selectors the e2e suite uses,
# deliberately slowed, and records the whole thing: the point of the video is to be
# watched, and the test still guards the behaviour.
#
# It tours every page the change touched, not just the one where the feature is entered —
# a reviewer's next question after "does it work" is always "where else does this show up".
#
# Playwright does not record the mouse pointer, so raw footage never tells you WHERE the
# thing being narrated is. Each say() therefore takes the element it is about and stores
# its on-screen box on the cue; annotate-feature-video.py turns those into a spotlight and
# burns the narration into the frame. Coordinates are viewport-relative and the video is
# recorded at the viewport size, so they are frame pixels 1:1 — the run prints the
# devicePixelRatio and viewport it actually got, which is what makes that safe to assume.
#
# Each cue is also SPOKEN, by the offline macOS speech synthesizer, before it is filmed. That
# is not decoration: the synthesizer reports when it says each word, which is what lets the
# captions light up word by word in time with the voice. It also fixes the pacing problem the
# hardcoded pauses below could never solve — a pause tuned for reading a sentence is not the
# time it takes to say it — so every pause() is now a MINIMUM, stretched when the narration
# needs longer. Set NARRATION=off to film silently; NARRATION_VOICE / NARRATION_RATE pick the
# voice (`say -v "?"` lists them) and its speed.
#
# Usage:
#   .claude/skills/human-review/scripts/record-feature-video.sh <out.webm>
#
# Writes, next to <out.webm>:
#   <out>.cues.json  the narration, timestamped as the run happens (plus each cue's box, its
#                    spoken .wav and the time of every word in it), so the guide can build a
#                    transcript that seeks the player
#   <out>.raw.webm   the same film without the annotations or the voice
#   <out>.narration/ one .wav per cue, kept so the film can be re-annotated without re-filming
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The project under review, not wherever the skill happens to be installed — every other
# script in the skill resolves the root from the working directory, and when the skill is
# a symlink or a plugin cache the two are not the same repository.
ROOT="$(git rev-parse --show-toplevel)"

OUT="${1:?usage: record-feature-video.sh <out.webm>}"
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
RAW="${OUT%.webm}.raw.webm"
CUES="${OUT%.webm}.cues.json"
VOICEDIR="${OUT%.webm}.narration"

# 127.0.0.1, never "localhost": Node 18+ puts ::1 first, an IPv4-only dev server refuses it,
# and undici reports that as the bare string "fetch failed". curl hides it (Happy Eyeballs).
BASE_URL="${BASE_URL:-http://127.0.0.1:4200}"
API_URL="${API_URL:-http://127.0.0.1:8080}"

curl -fsS -o /dev/null "$BASE_URL/" || { echo "[video] frontend not up at $BASE_URL" >&2; exit 2; }
curl -fsS -o /dev/null "$API_URL/api/pettypes" || { echo "[video] backend not up at $API_URL" >&2; exit 2; }

mkdir -p "$(dirname "$OUT")" "$VOICEDIR"
rm -f "$VOICEDIR"/*.wav
# The flow being filmed belongs to the PROJECT, not to this skill. The skill owns the
# harness — launching, narrating, timing the cues, spotlighting, annotating — and the
# project owns the twenty lines that say what to click. Before this split the narration
# and selectors of one project's feature lived in here, so filming the next feature meant
# editing another git repository (or a plugin cache that the next update overwrites), and
# the selectors were generic enough to keep resolving: you got a polished, correctly
# captioned film of the wrong feature.
FEATURE="${HUMAN_REVIEW_FEATURE_SCRIPT:-}"
if [ -z "$FEATURE" ]; then
  for candidate in "$ROOT/.human-review/feature-script.js" "$ROOT/human-review-feature.js"; do
    [ -f "$candidate" ] && { FEATURE="$candidate"; break; }
  done
fi
if [ -z "$FEATURE" ] || [ ! -f "$FEATURE" ]; then
  cat >&2 <<'MSG'
[video] no feature script — nothing to film, so step 5 is skipped (say so in the guide).

Write one at .human-review/feature-script.js (or human-review-feature.js at the repo
root, or point $HUMAN_REVIEW_FEATURE_SCRIPT at it). It exports one async function:

  module.exports = async ({page, say, pause, get, app, apiUrl}) => {
    await page.goto(`${app}/some/screen`);
    const thing = page.locator("#the-new-thing");
    await say("This is the new part.", thing);   // spoken, captioned, spotlit
    await pause(2000);                           // a FLOOR; the narration may stretch it
    return {ok: true, note: "one line for the run summary"};
  };

Return {ok:false} when the feature did not work: the film is still kept and the recorder
exits 3, because a film of the feature NOT working is the most valuable one it can make.
MSG
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

set +e
NODE_PATH="$ROOT/petclinic-test/node_modules" node -e '
const {chromium} = require("playwright");
const [baseUrl, apiUrl, videoDir, raw, cuesPath, voiceDir, narrator, featurePath] =
    process.argv.slice(1);
const fs = require("fs");
const path = require("path");
const {execFileSync} = require("child_process");

const narrationOn = process.env.NARRATION !== "off";
const voice = process.env.NARRATION_VOICE || "Samantha";
const speechRate = process.env.NARRATION_RATE || "0.5";
// The synthesizer is deliberately run BEFORE the cue is timestamped: it takes a fraction of a
// second, and a fraction of a second of frozen screen belongs to the shot that just ended, not
// to the one about to be narrated.
const speak = (text, wav) => {
  if (!narrationOn) return null;
  try {
    const out = execFileSync("python3", [narrator, "--text", text, "--out", wav,
        "--voice", voice, "--rate", speechRate], {encoding: "utf8"});
    const res = JSON.parse(out);
    return res.error ? null : res;
  } catch (e) { return null; }
};

// Everything network goes through this. undici throws a TypeError whose entire message is
// the string "fetch failed" — no URL, no errno — and the cause hides on `.cause`. A feature
// script that cannot reach the app should say which URL it could not reach.
const get = async (url) => {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res;
  } catch (e) {
    throw new Error(`GET ${url}: ${e.cause?.code || e.message}`);
  }
};

(async () => {
  const owners = await (await fetch(apiUrl + "/api/owners")).json();
  const owner = owners.find(o => (o.pets || []).length > 0);
  if (!owner) throw new Error("no owner with a pet in the database");

  // The dev server answers on every path, but the Angular router only matches routes under
  // the <base href> the served index.html carries. Hardcoding "/" films an empty shell.
  const indexHtml = await (await get(baseUrl + "/")).text();
  const app = baseUrl + ((indexHtml.match(/<base href="([^"]*)"/) || [, "/"])[1])
      .replace(/\/$/, "");

  const browser = await chromium.launch({slowMo: 450});
  const context = await browser.newContext({
    baseURL: baseUrl,
    viewport: {width: 1280, height: 800},
    recordVideo: {dir: videoDir, size: {width: 1280, height: 800}},
  });
  const page = await context.newPage();

  // Recording starts with the page, so every cue is timed from here. The narration is
  // written as the run happens rather than guessed afterwards, so it can never drift
  // from what the video actually shows.
  const t0 = Date.now();
  const cues = [];
  // A cue may name the element it is about. boundingBox() is viewport-relative, so it is
  // read at the moment the cue is spoken — after any scrolling — never earlier.
  let spokenUntil = 0;
  const say = async (text, target) => {
    const box = target ? await target.boundingBox() : null;
    // The warning glyph is a caption device, not something to read out loud.
    const wav = path.join(voiceDir, `cue${String(cues.length).padStart(2, "0")}.wav`);
    const speech = speak(text.replace(/^⚠\s*/, ""), wav);
    const cue = {t: (Date.now() - t0) / 1000, text};
    if (box) {
      cue.box = {
        x: Math.round(box.x),
        y: Math.round(box.y),
        width: Math.round(box.width),
        height: Math.round(box.height),
      };
    }
    if (speech) {
      cue.audio = path.basename(voiceDir) + "/" + path.basename(wav);
      cue.speech = speech.duration;
      cue.words = speech.words;
      spokenUntil = Date.now() + speech.duration * 1000;
    }
    cues.push(cue);
  };
  // Every hardcoded pause is a floor, never a ceiling: the shot also has to last long enough
  // for the sentence being spoken over it to finish, plus a beat before the next one starts.
  const pause = (ms) => page.waitForTimeout(Math.max(ms, spokenUntil + 350 - Date.now()));

  // Everything above is the harness; everything the film SHOWS comes from the project.
  const flow = require(featurePath);
  if (typeof flow !== "function") {
    throw new Error(`${featurePath} must module.exports = async ({page, say, pause, …}) => {…}`);
  }
  const outcome = (await flow({page, say, pause, get, app, apiUrl, baseUrl})) || {};
  const saved = outcome.ok !== false;
  const note = outcome.note || "";

  // The boxes are frame pixels only if the page was rendered 1:1 at the recorded size.
  const geom = await page.evaluate(
      () => ({dpr: devicePixelRatio, w: innerWidth, h: innerHeight}));

  await context.close();
  await browser.close();

  const webm = fs.readdirSync(videoDir).filter(f => f.endsWith(".webm"))
      .map(f => videoDir + "/" + f).sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0];
  if (!webm) throw new Error("playwright produced no .webm");
  fs.copyFileSync(webm, raw);
  fs.writeFileSync(cuesPath, JSON.stringify(cues, null, 1));
  const boxed = cues.filter(c => c.box).length;
  console.error(`[video] ${path.basename(featurePath)}${note ? ": " + note : ""}, `
      + `${cues.length} cues (${boxed} with a box) -> ${raw}`);
  console.error(`[video] viewport ${geom.w}x${geom.h} @ dpr ${geom.dpr}`);
  const spoken = cues.filter(c => c.audio);
  console.error(spoken.length
      ? `[video] narration: ${spoken.length}/${cues.length} cues, `
        + `${spoken.reduce((a, c) => a + c.speech, 0).toFixed(1)}s of speech, voice "${voice}"`
      : "[video] narration: none (NARRATION=off, or the synthesizer is unavailable)");
  if (!saved) {
    // Exit 3 is not a failure to handle — it is the most review-worthy film the pipeline
    // can produce. Embed it, and put what it shows at the top of "Look here first".
    console.error("[video] NOTE: the feature did NOT hold — the film says so out loud. "
        + "Embed it anyway and lead the review with it.");
    process.exitCode = 3;
  }
})().catch(e => { console.error("[video] " + e.message); process.exit(1); });
' "$BASE_URL" "$API_URL" "$TMP" "$RAW" "$CUES" "$VOICEDIR" "$SCRIPT_DIR/narrate-cue.py" "$FEATURE"
RC=$?
set -e
if [ "$RC" != 0 ] && [ "$RC" != 3 ]; then exit "$RC"; fi

python3 "$SCRIPT_DIR/annotate-feature-video.py" "$RAW" "$CUES" "$OUT"

if command -v ffprobe >/dev/null 2>&1; then
  echo "[video] $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")s, $(du -h "$OUT" | cut -f1)" >&2
fi
exit "$RC"
