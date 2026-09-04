# The feature script (Step 5)

The skill owns the harness — launching, speaking each cue, timing the captions to the voice,
spotlighting the element a cue is about, annotating the footage. **You own the twenty lines
that say what to click**, in `.human-review/feature-script.js`, `human-review-feature.js` at
the repo root, or wherever `$HUMAN_REVIEW_FEATURE_SCRIPT` points:

```js
module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  await page.goto(`${app}/some/screen`);
  const thing = page.locator("#the-new-thing");
  await say("This is the new part.", thing);   // spoken, captioned, spotlit on the frame
  await pause(2000);                           // a FLOOR — the narration may stretch it
  return {ok: true, note: "one line for the run summary"};
};
```

No script → step 5 is skipped with a stated reason, like any other missing hook.

This split exists because the narration and selectors of one project's feature used to live
inside the harness: filming the next feature meant editing *another git repository*, and the
selectors were generic enough to keep resolving — so you got a polished, correctly captioned
film of the **wrong feature**, under the one heading a reviewer trusts without reading.

## Which screens the film must visit — derive them, never write them down

A rule about *your* script: the harness films whatever you drive it through and has no
opinion about which screens those are. **The film must pass through every screen the change
touched, and that list has to be derived from the diff at film time.** A hand-written list is
correct once and quietly incomplete the next time somebody adds a field — and a screen
missing from the film looks exactly like a screen the change did not affect.

Derive it: changed components → the routes that render them, climbing template containment to
**every** routed ancestor. Two details, each a real bug rather than a precaution:

- **Climb *past* a routed ancestor, do not stop at the first one.** A changed `pet-list`
  resolved to `/pets` and missed `/owners/:id` entirely. A component can be both routed and
  embedded, and stopping at the first hit is how one of the two screens goes unfilmed.
- **Parse with the TypeScript compiler, not a regex.** It is already on the recorder's
  `NODE_PATH`. A regex over routing modules returns an empty list after somebody reformats
  them, and **an empty list is indistinguishable from "nothing changed"** — so the film
  silently covers nothing and reports success.

**A route the URL cannot fill is reported, never skipped.** An `@Input`-driven component in
an app without `withComponentInputBinding()` has no URL that reaches it; say so in the run
summary. A gap in the film's coverage is the one thing a reviewer cannot see for themselves.

Give a screen a handler only to make its beat better: a screen with none is still filmed. The
default has to be *filmed plainly*, because "no handler" must never quietly mean "not visited".

## Environment

`TITLE_CARD=off` films without a card; `$HUMAN_REVIEW_VIDEO_TITLE` and
`$HUMAN_REVIEW_VIDEO_SUBTITLE` override the two lines. `NARRATION=off` films silently and
spreads the words across the cue at a reading rate; `NARRATION_VOICE` (any name from
`say -v '?'`) and `NARRATION_RATE` (0..1, default 0.5) pick the voice and its speed. All of
it is local: nothing about the change is sent to a TTS service.

The title card is **filmed, not spliced**, because the cue clock *is* the video clock —
prepend N seconds of picture and the captions, the mixed narration and the transcript's seek
targets are each wrong by N, each a separate place to forget one. The one number the footage
cannot reveal to a later pass is how long the card holds; the recorder measures it into
`<out>.narration/lead` and hands it to `annotate-feature-video.py --lead`, so a re-annotation
gets it right without re-filming.

Both halves are plain programs — `narrate-cue.py` over `tts-cue.swift`, then
`annotate-feature-video.py` over ffmpeg — so re-cutting the film costs CPU, not tokens. The
per-cue `.wav`s stay in `<out>.narration/` beside `<out>.raw.webm`.
