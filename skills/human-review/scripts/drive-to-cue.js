#!/usr/bin/env node
// Drive the running app to the state one caption describes, then hand over the browser.
//
// This does not re-implement the walkthrough. It runs the *same* feature-script.js that
// filmed it, under the same {page, say, pause, get, app, apiUrl, baseUrl} harness the
// recorder uses, and stops after the say() that produced the caption you clicked. So the
// screen you land on is the screen the film showed at that moment by construction — there
// is no second description of the journey to drift out of step with the first.
//
// Headed, and deliberately never closed: the point is to leave you in the app, at that
// point, with the keyboard.
//
//   node drive-to-cue.js --app http://localhost:5123 --script .human-review/feature-script.js --cue 4
//
// --cue is 1-based and matches the transcript's numbering. --reset (default on when the
// environment offers one) puts the data back first, so driving here twice lands in the
// same place rather than piling up rows.
const path = require("path");
const Module = require("module");

// This script lives in the skill — installed as a plugin, somewhere under ~/.claude —
// while everything it loads (playwright, and whatever the project's own feature script
// pulls in) is a dependency of the repository being reviewed. Node resolves modules next
// to the *file* doing the requiring, so both this file and .human-review/feature-script.js
// look in the wrong place. Teaching the whole process where the project keeps its modules
// fixes it once, for every depth, instead of the pasted command carrying a NODE_PATH.
const CANDIDATES = ["node_modules", "petclinic-test/node_modules", "e2e/node_modules",
                    "test/node_modules"].map((d) => path.resolve(process.cwd(), d));
process.env.NODE_PATH = CANDIDATES.concat(process.env.NODE_PATH || []).join(path.delimiter);
Module._initPaths();

let chromium;
try {
  ({chromium} = require("playwright"));
} catch {
  console.error("[drive] cannot find the `playwright` module. Run this from the directory "
                + "that has it installed — the e2e project — or `npm i -D playwright`.\n"
                + "        looked in: " + CANDIDATES.join(", "));
  process.exit(2);
}

const arg = (name, fallback) => {
  const i = process.argv.indexOf("--" + name);
  return i === -1 ? fallback : process.argv[i + 1];
};
const has = (name) => process.argv.includes("--" + name);

const APP = (arg("app") || "").replace(/\/+$/, "");
const SCRIPT = arg("script", ".human-review/feature-script.js");
const CUE = parseInt(arg("cue", "1"), 10);
const API = (arg("api") || APP).replace(/\/+$/, "");
const RESET = arg("reset", "/__reset");

if (!APP) {
  console.error("--app is required: the URL the environment is answering on");
  process.exit(2);
}

// Arriving is a suspension, not an exception. The flow wraps each screen in its own
// try/catch — a screen that cannot be reached is reported rather than fatal — so a thrown
// sentinel is swallowed as "this screen failed" and the walkthrough carries on past the
// point you asked for. Never returning from say() stops it dead instead, with the page
// exactly as the caption found it, which is the state you wanted the keyboard in.
const NEVER = () => new Promise(() => {});

const get = async (url) => {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`GET ${url}: HTTP ${res.status}`);
  return res;
};

(async () => {
  if (RESET && !has("no-reset")) {
    // Same starting point every time, so the second click on a caption lands where the
    // first one did instead of tripping a unique constraint.
    const r = await fetch(APP + RESET, {method: "POST"}).catch(() => null);
    console.error(r && r.ok ? "[drive] data reset to the seed"
                            : "[drive] no reset endpoint answered — data is as you left it");
  }

  // The Angular router only matches routes under the <base href> in the served index.html;
  // the recorder resolves it the same way rather than assuming "/".
  const indexHtml = await (await get(APP + "/")).text();
  const app = APP + ((indexHtml.match(/<base href="([^"]*)"/) || [, "/"])[1]).replace(/\/$/, "");

  // Headed is the whole point — you are meant to take the keyboard. --headless exists so
  // the driver itself can be tested without a window appearing.
  const browser = await chromium.launch({headless: has("headless"),
                                         args: ["--start-maximized"]});
  const page = await (await browser.newContext({viewport: null})).newPage();

  let seen = 0;
  const say = async (text) => {
    seen += 1;
    console.error(`[drive] ${seen}. ${text}`);
    if (seen >= CUE) {
      console.error(`\n[drive] arrived at cue ${CUE}: ${text}`);
      console.error(`[drive] the app is at ${page.url()}`);
      console.error("[drive] the browser is yours — close it when you are done.");
      if (has("headless")) { await browser.close(); process.exit(0); }
      await NEVER();
    }
  };
  // Every pause in the film is there to let narration finish over a still shot, never for
  // the app's benefit — Playwright waits on the elements itself. Driving does not narrate,
  // so they shrink to a beat that lets Angular settle.
  const pause = (ms) => page.waitForTimeout(Math.min(ms, 150));

  const flow = require(path.resolve(SCRIPT));
  const outcome = (await flow({page, say, pause, get, app, apiUrl: API, baseUrl: APP})) || {};
  // Only reachable when the walkthrough ran out of captions before reaching the one asked
  // for. The flow swallows a screen it could not reach into its own note, so that note is
  // the only place the real reason exists — printing it is the difference between "as far
  // as it goes" and knowing which step broke.
  console.error(`\n[drive] the walkthrough ended after ${seen} captions — cue ${CUE} is ` +
                `past the end, so this is as far as it goes`);
  if (outcome.note) console.error("[drive] " + outcome.note);
  if (has("headless")) { await browser.close(); process.exit(0); }
})();
