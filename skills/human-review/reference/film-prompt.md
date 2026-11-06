# The film's script, as a prompt

The second paid, non-reproducible piece of `/human-review` that a *button* may ask for,
beside the requirements↔tests matrix (`matrix-prompt.md`): the Demo film's script,
`.human-review/feature-script.js`. `rerun-film.py` hands this file to
`claude -p --model sonnet` at the repository root, so everything below the rule is
addressed to that run and not to a reader. The rules are `feature-script.md`'s, which is
the contract and the longer argument; this is the same thing, said to the one who writes.

---

You are rewriting the script that drives the Demo film of an existing `.human-review/`
report, and nothing else. Work in the repository you are started in. The harness —
launching the browser, speaking each cue, timing captions, spotlighting, annotating — is
not yours; **you own the lines that say what to click and what to say.**

**Write exactly this, and touch no other file outside `.human-review/`:**

`.human-review/feature-script.js` — CommonJS, one exported async function, the only shape
the recorder (`scripts/record-feature-video.sh`) accepts:

```js
module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  await page.goto(`${app}/some/screen`);
  const thing = page.locator("#the-new-thing");
  await thing.waitFor();                       // the assertion, before the claim
  await say("This is the new part.", thing);   // spoken, captioned, spotlit
  await pause(2000);                           // a floor; the narration may stretch it
  return {ok: true, note: "one line for the run summary"};
};
```

`page` is Playwright's, `app` the base URL of the running front end, `apiUrl` its API,
`get` a fetch. A helper the script `require`s (a route deriver, say) may sit beside it in
`.human-review/` as another `.js` file; nothing else.

**The rules that make this film worth paying for:**

- **Open cold.** The first `say()` is the climax: land on the end state of the feature (by URL,
  or with setup before that line, which the recorder cuts) and say what it means to someone,
  over a screen already showing it. The second line gives the stakes and turns back: what was
  missing, then "So how did it get there?". Only then walk through the steps, tied back to the
  opening, and let the last line close the loop. No "This is the X page", no introduction: the
  film has no title card and starts on that first line. The payoff must really be on screen
  and the "before" must be what the diff shows, never drama the ticket does not support.

- **Short.** At most ten `say()` calls, one per behaviour the ticket names, short
  sentences, about a minute of film. The reviewer is busy; a second line about the same
  screen state is a line too many.

- **Start from the script that is there.** If `.human-review/feature-script.js` exists,
  read it first and keep what still holds — its handlers, its selectors, and its narrative
  order if it opens cold. The previous copy is kept under `.human-review/.model-prev/`, made seconds
  ago, so "my file equals `.model-prev/`" is true before you have done anything and is
  never evidence that nothing needed doing. The comparison that means something is the
  script against the *diff*.
- **Derive the screens from the diff — never write the list down.** The film must pass
  through every screen the change touched, and that list is computed when the film runs:
  changed components → the routes that render them, climbing template containment to
  **every** routed ancestor. Climb *past* a routed ancestor rather than stopping at the
  first (a changed `pet-list` resolved to `/pets` and missed `/owners/:id`). Parse with the
  TypeScript compiler, not a regex: it is on the recorder's `NODE_PATH`, and a regex over
  reformatted routing modules returns an empty list — which looks exactly like "nothing
  changed". Read the diff with `git diff --name-only` against the base in
  `human-review.json` (default `origin/main`).
- **Visit every screen.** A screen with no hand-written handler is still filmed, by a plain
  default beat (navigate, spotlight the heading, say the screen changed). A handler exists
  only to make a beat *better*; "no handler" must never mean "not visited".
- **`say()` what is new on each screen, and only after waiting for it.** `say()` takes the
  element only to draw a spotlight; a locator that matches nothing still speaks the
  sentence over a screen that does not contain it. So `await locator.waitFor()` first, and
  let the throw happen when it never arrives.
- **Catch per screen, report every miss, return `{ok, note}`.** Collect the screens whose
  handler threw and the routes the URL cannot fill, and return
  `{ok: missed.length === 0, note: "<visited>/<total> changed screens filmed" + …}` with
  ` | FAILED to reach: a; b` and ` | not filmable: c` appended when there are any. Keep
  those two labels verbatim: `run-steps.py` reads them back out of the log onto the page.
  A route the URL cannot fill is reported, never skipped.
- **Pick data that shows the change.** Ask the API (`get(`${apiUrl}/…`)`) for a record the
  new thing is visible on, rather than filming the first row of a freshly seeded table.
- **Never invent a selector.** Every locator must exist in the front end's templates as
  they are on this branch; read them.
- Do **not** touch `content.json`, the matrix, `test-index/` or anything outside
  `.human-review/`; do **not** commit and do **not** push.

**Do not film, build or serve.** `refresh-report.py --steps video` runs after you, from the
same command, and it is what records the film from this script. `rerun-film.py` refuses a
result that is missing, empty, does not parse under `node --check`, or does not export a
function — so check that yourself before you stop.

Finish by printing, in one line, how many screens the script will visit and which ones it
gives a handler of their own.
