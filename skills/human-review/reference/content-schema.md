# `content.json` — the shapes

Loaded on demand from Step 9. The runbook names the keys; this file says what goes in them.

**This file is the page's layout and its ledes — not its judgement.** The three piles on
the Review tab are the *branch's*: `/implement-ticket` writes `review-points.md` at the
repository root, commits it with the fixes, and `review-points.py` parses it into
`.human-review/review-points.json`. A content file asks for that with

```json
"findings":    {"auto": "review-points"},
"autofixes":   {"auto": "review-points"},
"assumptions": {"auto": "review-points"}
```

and writes no item of its own. The shapes below are still exactly what arrives — the
parser emits them, which is why no renderer changed — so they remain the contract for
anyone reading the page's markup or writing a pile by hand. What is left for a model is
`assets/requirements-map.html` and `test-index/`: the requirements↔tests matrix, which is
the one claim on the page a reviewer cannot check by hand and which `review-points.md`
says nothing about.

`{"auto": "review-points"}` behaves like the other `auto` keys — computed, never typed —
with one addition: **an absent file empties the piles and says so.** The Review tab grows
a grey band reading *no `review-points.md` on this branch — nothing records what was
reviewed or declined*, and the assumptions block is forced to mode C whatever it declares.
It must never render as *"Nothing outstanding — the automated passes came back clean"*: a
missing record is not a clean review, and those are the two states a reviewer most needs
told apart. An empty pile in a file that *is* there says which kind of empty it is — no
such section, or a section with nothing in it.

With the piles delegated, the counts line reads **`3 fixed · 6 declined · 7 assumptions`**
— `declined` because the items are closed, by the agent, and the reader's job is to agree
or disagree rather than to triage. A content file that writes its own piles keeps the old
wording (`6 open LLM review issues · 4 auto-fixed · 6 assumptions`), which is right for a
list nobody has answered yet.

## A finding / autofix item

```json
{"severity":"high|medium|low|info", "source":"/code-review", "title":"…", "body":"…",
 "why":"…", "refs":["path:line"], "snippets":[{"ref":"path:12-30","caption":"…"}],
 "diffs":[{"path":"…","base":"<sha>","caption":"…"}]}
```

`source` is the pass that raised it, stamped while that pass is running — the only moment
the information exists. An item with no `source` renders without the stamp rather than
being attributed to a guess. `/code-review` and `/simplify` render as **links to their own
documentation** (`PASS_DOCS` in the renderer), so the page never needs a line of prose
introducing the two commands; anything else is a plain stamp.

`diffs` entries carry their own `base`, and an item parsed out of `review-points.md` always
does: the file's frontmatter names the `implementation:` sha, so the diff shows the fix *on
its own* and keeps doing so after a rebase. Written by hand, `{"path": "…"}` alone falls
back to the rev the ledger recorded for the `review` tab. Add `"head"` when the fix is one commit and the file moved for other reasons
afterwards: `{"path":"…","base":"bf26a0de^","head":"bf26a0de"}`. A pinned diff drops the
editor link (which always compares against the working tree) and keeps the github.com one.
Both drop the block and say so on stderr when the before-side is not real — a base that does
not resolve, a file that did not exist in it, or an empty diff. **No diff beats a wrong one.**

## Inline tokens, expanded in any `body`

| token | renders |
| --- | --- |
| `{{snippet:path:12-30\|caption}}` | the lines, verbatim, as a captioned card |
| `{{diff:path@<sha>\|caption}}` | the file's change since `<sha>`, GitHub-style: two line-number gutters, green and red bands, three lines of context, a link under it that opens the same comparison in the editor — and on github.com when the change is committed and `origin` is a GitHub repo |
| `{{difflink:path@<sha>}}` | only the link, for when the diff itself is not the point |
| `{{drawio:conceptual}}` | the hand-drawn diagram's Diff / New / Old widget, inlined from the `conceptual-*.svg` `drawio-diff.py` wrote, with its legend and opening pane read off `conceptual-diff.json` — so a re-layout shows up on the next build instead of being frozen into this file |
| `{{tabcount}}` | the number of tabs actually emitted (for the summary's walk-through) |

`{{diff:…}}` and the `diffs` array are the same renderer; the array is the way to write it
on a finding, the token the way to write it mid-paragraph.

## An assumption

```json
{"source":"assumption", "title":"…", "body":"…", "alternative":"…", "why":"…",
 "refs":["path:line"], "snippets":[{"ref":"path:12-30","caption":"…"}]}
```

Same shape as a finding minus `severity` — an assumption is not a defect and must not be
ranked as one, and the parser refuses the field outright rather than coercing it away. It renders with its own violet card, a `your call` badge and the stamp
`assumption` where a finding carries `/code-review`, because it did not come from a pass:
it came from the agent that wrote the code, and it arrives from `review-points.md`'s
`## Assumptions` section. `alternative` is the reading that was not taken, and it is what
makes the item checkable at a glance.

The block's `"mode"` (A / B / C) is only ever read for an **empty** pile, to say which kind
of empty it is. With `{"auto": "review-points"}` you do not write it: a record on the branch
says so itself, and an absent one is forced to mode C — *nobody could be asked* — because
the thing that would have answered was never written down.

**An assumption with no `snippets`, `refs` or `diffs` is dropped**, with a warning naming
it. It is the one item on the page a reader cannot go and verify: a model asked at the end
of a long session what it was unsure about will produce fluent sentences of exactly this
shape whether or not it ever hesitated, and the anchor is the whole difference.

## Top level

```json
"pr": {"number": 37, "title": "Link Visit with Vet",
       "url": "https://github.com/victorrentea/petclinic/pull/37",
       "repo": "https://github.com/victorrentea/petclinic",
       "branch": "test-pr", "base": "main"}
```

Every field is optional and so is the block. With it, the heading becomes `GH#37 Link Visit
with Vet` and **`subtitle` does not render in the masthead at all** — keep `subtitle` for the
`<title>` and for a page built without `pr`, and do not write the refs into it. `branch` and
`base` render as the first two chips on the scope bar.

`base` is resolved to `origin/<name>` when such a ref exists — the ref a pull request would
actually merge into — and the base chip grows a **`!`** when the two refs have drifted: the
base has moved ahead of the fork point, or the local branch named here is behind its own
remote. The mark is recomputed on every build from the refs as they stand, so merging main
in (or fetching) clears it by itself; there is nothing to reset.

```json
"scope": [
  {"label":"commits","value":"2 (pushed to main)","href":"https://github.com/…/compare/…"},
  {"auto":"diffstat"},
  {"auto":"tests","href":"#requirements"},
  {"label":"diagrams","value":"3","href":"#diagrams"},
  {"auto":"autofixed","href":"#review"}
]
```

The scope bar is read as a row of signed numbers, so the signs are a convention and not
a per-chip choice: **`+` added, `−` removed, `✍️` changed** (`files <span class="added">+1</span>
/ ✍️40`). Never `~` or `±` for the changed ones — both read as an approximation, and "about
forty files were touched" is not what the number means: the pencil says somebody went in
and edited exactly that many. It is `build.PENCIL`, so the two chips that use it cannot
drift apart.

`value` is raw HTML on purpose; `href` makes the chip a link. The three `auto` chips are
**computed, never typed** — `diffstat` measures the change set with `git diff`, `autofixed`
counts the page's own two lists, `tests` reads the manifest `test-changes.py` already built
for the tab below. All three drop themselves rather than print a wrong number.

`{"auto":"cost"}` is **retired and ignored**, not an error: the cost is the last tab on the
strip now (see *The cost tab*), because what a chip in this bar could say was only ever the
review's own share of the bill — and the review is the cheaper half of what a change costs.
Leaving it in a content file does nothing. A chip whose number is typed by hand goes
stale without anything noticing: the `tests` chip exists because `unit tests · 125 green
(20 new)` used to be typed here, and was true until somebody wrote the next test.

`diffstat` renders **two** chips from one measurement — `files` and `lines` — so the two can
never end up describing different ranges. It exists because they did, and worse: a page
carried `files +1 / ~40` and `lines +1198 / −863` for six days, and the line counts matched
no range in the repository at all — not the branch against its base, not against the
merge-base recorded three lines above them in the same content file, not against the stale
local `main`. They had been typed once, from a branch state three commits and one `git
reset` ago. **Do not write `files` or `lines` by hand.**

It counts code and leaves generated files out — redrawn `.genseq.*` diagrams, anything under
a `generated/` directory, lock files, minified bundles, images. Not to flatter the number
but to keep it answering its question: on the branch it was written for, one regenerated
`endpoint-complexity.json` was 1405 of 2896 added lines, and the redrawn diagrams supplied
almost every deletion, so `−333` described a picture being redrawn rather than a line of
logic being removed. `{"auto":"diffstat","exclude":["*.pb.go"]}` adds project-specific
pathspecs; the built-in list cannot be switched off, and the tooltip states the unfiltered
totals regardless — the generated files are ranked below the code, never hidden from it.

`autofixed` names the model that did the reviewing, taken from the run's own transcript, so
the chip reads `🤖Opus 5 review: 9 open, 3 auto-fixed` rather than needing a second,
unverifiable `reviewed by` chip beside it. `{"by":"Opus 5"}` is the fallback for a page
rebuilt outside the session that reviewed it. The pill is written as a **sentence** — robot,
model, colon, then the two numbers with a comma between them — because a label followed by a
gap and a row of figures reads as a measurement, and this is a claim somebody made about the
diff. The applied half stays grey: it is there to be checked, not acted on.

The `tests` chip is a **balance**, not a count — `+10 / −4 / ✍️4` — because the
question it answers is whether the branch left fewer tests running than it found. The
loss is one number over three causes (deleted, commented out, left standing under an
`@Disabled`), split only in its tooltip: all three cost the run the same test, only
deletion is visible in a diff, and a face carrying all three would invite reading the
smallest of them as the answer.

```json
"extraCss": ["assets/openapi-diff.css", "assets/openapi-compat.css",
             "assets/complexity-delta.css", "assets/ds-audit.css"],
"testChanges": "assets/test-changes.json",
"playwrightTraces": "assets/traces.json",
"footer": "Built by /human-review against the running stack on 2 Sep 2026."
```

The footer names the toolset and when, and stops — a sentence about the page's own honesty
is stripped by the build if it reappears. `/human-review` in it is replaced by the repo URL.
Housekeeping ("`.human-review/` is a throwaway artifact — delete it rather than commit it")
is not footer material either: it is an instruction to whoever ran the review, and the
reviewer reading the page can do nothing with it. Say it to them in the wrap-up instead.

## Block types

**`findings`** (the disputable calls) · **`assumptions`** (the top-level `assumptions`
array; the block carries `"mode": "A"|"B"|"C"` so an empty pile can say *which* kind of
empty it is — asked and had nothing, read back and found nothing, or nobody left to ask.
**Declare it on every page, empty or not**: the lede counts this pile at zero as readily
as at nine, and the build warns when no tab declares the block, because a page that never
mentions the coder's guesses and a coder who made none look the same from the outside) ·
**`autofixes`** (the top-level `autofixes` array, same shape as a finding) · **`diagrams`** (the delta gallery, narrowed by `kind` / `only` /
`except`) · **`testpairs`** (Step 3) · **`tests`** (the ledger: every test the change set
moved, grouped by what happened to it) · **`traces`** (the Playwright recordings of the run,
each opening on the trace viewer itself) · **`logging`** (Step 7c) · **`puml`** (a diagram this
branch did not change, rendered from source as context) · **`codeowners`** (Step 8, run by
the renderer) · **`codecity`** · **`section`** (one entry of `sections` by `id`) · **`html`**.

**`logging`**'s own lede is written by the renderer, not by you: `title` and `body` on
that block are ignored. The tab is called *Logging* and the panel opens with it, so a
heading repeating the word is the tab's label said twice; and the methodology paragraph
that used to sit under it (grep vs. `ast-grep`, `Math.log(x)`, walking the tree) described
what every block below already shows. What is left is one line naming what was searched
for, with the package list itself on hover — read out of `logextract.py`'s own rule, so it
cannot go stale. The nested `existing` and `console` asides still take a `title` and
`body`.

**`codecity`** has no heading and **no lede**: `title` and `body` are both ignored, and the
picture starts immediately under the tab strip. The tab says *Code City*, so a heading above
the shot is its label said twice — and the line `body` used to hold was always some version
of *"10 buildings lit — the classes this change set touched, in a city of the whole
backend"*, which is three claims a reader can already see: the count is legible in the
picture, the lit slice is what lit means, and the city being the whole backend is what a
city is. It was also a hand-typed number in a file nothing revalidates, so it went stale
silently the first time somebody added a class. The panel and the hover card are inside the
shot, in the renderer's own words. Write `caption` if there is genuinely something under the
picture to say; there usually is not. **`codeowners`** has no default heading either: the pill says
*CODEOWNERS*, its badge says *Code owners approval required* and the seal under it says
*APPROVAL REQUIRED*, so a fourth `Code owners` above the first filename is the label said
again. It still renders a `title` you write on purpose.

**`tests`** takes no configuration — it renders `testChanges` — and you do not have to
declare it: a page that has a manifest and no `tests` block gets one appended to the
`requirements` tab. Declare it only to put it somewhere else in that panel.

**`traces`** is the same arrangement over `playwrightTraces`, and appends *under* the
ledger for a reason worth keeping in that order: the ledger is what the branch did to the
tests, the recordings are what the run did, and that is the order the two questions arrive
in. It takes a `title` (default *Step through what the tests did*) and a `body`, and
nothing else — which test each recording belongs to is read out of the run's own HTML
report by `playwright-traces.py`, never written here.

A tab may carry an **`intro`** (raw HTML before its first block, carrying no weight of its
own) and a **`tip`** (hover sentence for a tab whose subject two words cannot carry).

A `diagrams` block that finds nothing for its `only`/`kind` may carry a **`context`** — the
same fields as a `puml` block (`src`, `name`, `note`) — and falls back to rendering from
source. Without it, a tab built on that one block alone is silently dropped rather than kept
and struck through.

### `testpairs` (Step 3)

```json
{"type":"testpairs","id":"sequences","kind":"sequence",
 "title":"Each test, beside the sequence its own run recorded",
 "body":"<p>…what the deltas amount to, in this page's own words…</p>",
 "snippets":[{"ref":"petclinic-test/features/add-visit.feature:12-27"}],
 "unpaired":{"id":"tests-nosequence",
             "title":"Tagged for tracing, and no diagram came back","body":"…"}}
```

**One snippet per test file, and no caption.** A pair is a fold: its summary is the file
name, the quoted test is inside it, and the diagram is inside it too. Two entries for one
file split that into two code blocks under one heading, and a reader counting blocks counts
two tests. A file with several tagged scenarios is therefore *one* multi-span reference —
`Test.java:60-61,70-94,124-155` — which prints a `⋯ N lines not shown` row between spans and
keeps the file's real line numbers throughout.

Span each scenario **from its tag**: `@generate_sequence` in a `.feature`, `@Test` and the
`@Order`/`@WithMockUser` above the method in Java. That tag is why the diagram exists, and a
range opening below it quotes a test that looks untagged. Open with the class's own tag line
where there is one (`@GenerateSequence` and its `class` line, as a first span).

A caption on one of these is prose nobody asked for: the fold already names the file, the
source bar already prints the path and the lines, and the diagram below is the same scenario
drawn. Say it in the block's `body` if it is worth saying once for the tab.

**`title: ""`** drops the heading altogether, and the block opens straight on the first
pair. Worth reaching for: each pair already names its scenarios and prints its own source
path, so a heading over them restates the tab label in the one place the first diagram
should be. Omitting `title` is not the same thing — that still gets the default.

**`snippets`** is the pool the pairing draws from — you quote the tests, the block works out
which diagram each belongs to, and you never name a diagram. **`unpaired`** names the group
the leftovers land in; omitting it accepts the defaults rather than turning the group off.

**The kind of test — `UI` / `API` / `unit` — is not authored.** Every pair leads with the
Tests tab's own chip, read off the diagram it holds: the first lifeline is the end the run
was driven from, `Browser` through the screens, `Client` from a `@SpringBootTest` in the
same JVM, one lifeline alone for a test nobody else could observe. That is the distinction
the tab's tip already draws, so reading it off the picture keeps badge and tip from
disagreeing, and a `.spec.ts` that is a Playwright run in one module and a component test
in the next is not settled by a guess at its path. A suite whose driver goes by a name this
has never heard of can say so — **`"cat": "e2e" | "api" | "unit"`** on the snippet, one kind
per test file — and a diagram this can read nothing out of gets no chip rather than a
wrong one.

A quoted test lands in one of three places, and the block decides which from the manifest
and the checkout, never from the content file: **changed** — its diagram has a manifest row,
so the pair opens on `New` with `Diff` and `Old` one click away; **unchanged** — no manifest
row, because the manifest lists only what the branch changed, but `<test>.genseq.puml` is
beside the test, so it is still a pair, drawn from the committed source, pilled `UNCHANGED`
in the neutral colour a `puml` block wears, with no `Diff` to offer and no delta counted
against the tab; **missing** — neither, so the test falls into the trailing `unpaired`
group. Only the last is "no diagram came back"; an unchanged sequence is a recording that
matched, and filing it under that heading was a claim the page had to retract by hand.

A diagram with no quoted test says either *"Generated by `<path>`, not excerpted here"* or
*"…which is not in this checkout — the diagram is the only record of it left."* A fabricated
pairing is a lie about provenance; a silent one is the loss this pipeline exists to prevent.

Each pair's diagram **opens on `New`**, not on the delta — the one place the two diagram
families are treated differently. A structural delta is the difference between two files a
human wrote, so every mark in it is a change somebody made; a sequence delta is the
difference between two *recordings*, and a run that reorders concurrent calls (or a
generator that relabels an arrow) marks lines nobody touched. The reader meets the picture
that is simply true, and reaches for `Diff` deliberately. Nothing in `content.json` selects
this; it follows the manifest's `kind`.

### `traces` (the Playwright recordings)

```json
{"type":"traces","title":"Step through what the tests did",
 "body":"<p>…what these runs are, in this page's own words…</p>"}
```

Fed by `"playwrightTraces": "assets/traces.json"`, written by
`scripts/playwright-traces.py` out of the run's own HTML report. Each row is one *attempt*
— title, the describe() path in front of it, the file and line, the duration, and the
top-level steps as a strip — and opens on Playwright's own trace viewer, in the panel:
every action with the page either side of it, the DOM, the console, and the network.

Three things about it are decisions, not details:

- **The frame is built on open, never in the markup.** A tab holding a dozen rows would
  otherwise boot a dozen copies of a browser application on page load, each fetching its
  own multi-megabyte zip, to show the one row the reader asked for.
- **A page read off disk gets the command instead.** The viewer reads the trace with
  `fetch`, and a page opened from `file://` — out of the downloadable zip, straight from
  `.human-review/` — can fetch nothing, not even a file beside it. There the row offers
  `npx playwright show-trace …`, which opens the same recording natively. Serve the page
  (`scripts/serve-review.py`) and the frame comes back.
- **A retry is stamped on the row.** The row is one attempt; attempt 2 shown unmarked
  reports a flaky test as a green one.

The block carries weight and **no changes**, like `codecity` and `puml`: a recording is
evidence about how the code behaves, not something the branch moved. The counts in its
lede — recorded, not carried, ran untraced — are the harvester's, never typed: a suite
that recorded four of its forty tests has not shown the reader the run, and a list of four
with nothing said would read exactly as if it had.

Which recordings the harvester's `--limit` carries is a ranking, not the report's order:
failures first and never cut; then the tests of the files the change set touched — the
rows the Tests tab marks as covering the change, read off `test-changes.json` (written by
the `tests` step, which runs first) — then the rest by file and line; skipped results are
cut first, since a test that never ran recorded nothing. So a covering test's 📺 is only
missing when the run genuinely did not trace it, and whatever fell past the limit is still
in the *not carried* count.

A Cucumber suite records nothing by itself — cucumber-js writes no Playwright report — so
a project that wants its `.feature` scenarios here records them in its own `Before`/`After`
hooks (`context.tracing.start/stop`), one zip per scenario plus a sidecar JSON naming the
test (`title`, `path`, `file`, `line`, `status`, `duration`, `error`, `trace`), and names
that directory as `steps.traces.cucumber`. The harvester files those rows with the
report's, in the same shape; the page tells them apart by nothing but the `.feature` in
the location.

Tracing has to be **on** for the run that produced the report (`--trace on`, or a config
knob the project's own test command sets). A suite left on `trace: 'on-first-retry'`
records nothing on a green run, the step skips with that reason, and the tab is dropped and
named under the strip.

### `video` section (Step 5)

```json
"appLinks": [
  {"href": "http://localhost:4200/owners/2", "label": "owner detail", "anchor": "visit list"},
  {"href": "http://localhost:4200/vets",     "label": "vets",  "anchor": "who will attend"},
  {"href": "http://localhost:4200/visits",   "label": "all visits"}
]
```

`anchor` is the phrase to wrap, matched in the first caption containing it and never nested
inside a link already placed. A link whose phrase is nowhere in the narration — or that has
no `anchor` — is **not dropped**: it prints after the transcript as *"Touched but not
filmed"*, which is a fact about the film's coverage.

An `href` that starts with `/` is a **path into whichever instance is running**, not a URL.
The port cannot be known when the page is built — the host picks one per instance so that
several branches can be up at once — so the path is kept in `data-app` and resolved at
click time. An absolute `href` is left exactly as written: it names one specific server on
purpose.

```json
"runtime": {
  "command": "cd ~/workspace/petclinic && ./start-docker.sh up --ref 9f3c1ab",
  "stop": "cd ~/workspace/petclinic && ./start-docker.sh down petclinic-9f3c1ab",
  "urlCommand": "cd ~/workspace/petclinic && ./start-docker.sh url petclinic-9f3c1ab",
  "base": "http://localhost:4200",
  "reset": "/__reset"
}
```

`stop` and `urlCommand` name the **instance**, not the ref: `start-docker.sh up --ref <sha>`
calls what it creates `petclinic-<shortsha>`, and `down`/`url` take that name. This example
said `down --ref <sha>` for a while, which the host refuses — and it is refused *later*, on
the click, not at build time, so the button looked fine and did nothing.

The `runtime` block above is what the *reader* presses. What the **film** is recorded against
is `steps.video.app` in `human-review.json` — see the recording section below — and the two
are worth keeping in step, because a reader who presses Start expects the instance the film
was made on.

`drive` is optional and turns each caption into a way *into* the app at that moment: a
`▸` on the row copies this command with `{n}` set to the caption's number (1-based, as
shown) and `{base}` to the URL in the bar. `drive-to-cue.js` replays the project's own
`feature-script.js` — the same file that filmed the walkthrough, under the same harness —
and stops after the nth `say()`, leaving a headed browser on that screen for you to take
over. Nothing describes the journey twice, so nothing can drift.

`runtime` puts a **Deployed app** bar above the player — one row of verbs, and under it the
same offer spelled out for a terminal:

```
Deployed app  [Start] [Open ↗] [Stop] [Reset data]        live at http://localhost:53421
or run this terminal command yourself:  cd ~/workspace/petclinic && ./start…   [Copy]
```

The address at the end of the row is where every relative `appLinks` href resolves against;
it is remembered per page, so a reload keeps it. `base` is the fallback the links use before
anything is known — with neither, they render grey and unclickable rather than pretending to
lead somewhere.

The bar asks `GET <base>/healthz` whether anything is listening, so an environment that
wants the `live at` / `offline` pill must answer it with CORS open.

Each control is gated on the thing it actually needs, because a control that can be pressed
while its precondition is missing is one that lies:

| control | drawn when | pressable when |
|---|---|---|
| `Start` | `command` | served, and nothing is up |
| `Open ↗` | always | something answers at the address |
| `Stop` | `stop` | served, and something is up |
| `Reset data` | `reset` | something answers at the address |

`stop` and `reset` are **optional and opt-in**, and neither is ever derived from `command`:
turning `… up --ref abc` into `… down --ref abc` by string surgery works for one host and
fails silently on the next. `stop` is a command the host runs; `reset` is a path the
environment answers on `POST` to put the data back to its starting point. Omit either and
no button is drawn. Resetting is never automatic — doing it on every link click would throw
away work the reviewer was in the middle of — and `Stop` empties the address box, since
leaving it behind would leave every link in the transcript pointing confidently at nothing.

`command`, `stop` and `drive` are **run** on a page served by `serve-review.py` and are
otherwise inert: the build writes them into `.human-review/.actions.json` and the button
sends the id of the one it wants, never the command itself. Served, `Start` has the server
scrape the `http://localhost:<port>` line the command prints and fills the address box
itself — which flips the pill to `live at`, unlocks `Open ↗`, `Reset data` and every `▸`,
and makes the box **read-only**, since it is output then rather than something to type into.

Off disk none of that exists, and the row says so instead of disappearing: it dims, `Start`
and `Stop` carry a tooltip pointing at the command below, the address box stays typeable so
pasting a URL by hand still aims the transcript's links, and the second line drops its
"or …" and reads as the instruction it is. `drive` falls back to the clipboard the same way.

`urlCommand` is **optional** and is the same host asked where the environment already
*is* — `url` rather than `up`. It is run once when a served page loads with an empty box,
which covers the reader who opens a guide somebody else already started the environment
for, and the browser with site data blocked where the remembered base was never there. It
is never derived from `command`: turning `up` into `url` by string surgery works for one
host and fails silently on the next, at load time, where nobody sees it fail.

Keep the film's section id `video` (it has outlived two tab reshuffles, so `#video` still
lands). The builder emits the player only for a file on disk; with none it emits a notice
naming the absent file and keeps the transcript.

### `embed` (Step 7b, 7b-visual)

```json
{"id": "pb33f", "title": "The same two revisions, read by a third differ",
 "body": "<p>…what it says, in this page's own words…</p>",
 "embed": {"src": "assets/openapi-changes.html",
           "label": "openapi-changes report — openapi.yaml at <sha> against the working tree",
           "missing": "run `openapi-changes html-report` (brew install pb33f/taps/openapi-changes)"}}
```

`"class": "oaviframe"` on the `openapi-visual-diff.html` embed. `aria-label`, never `title` —
this page has exactly one tooltip component, and `data-tip` is dead inside a cross-document
frame. A control inside an embedded frame gets **neither**: make it self-explanatory, or
explain it in the host page's prose.

### `codeowners` (Step 8)

```json
{"id":"owners","label":"Code owners","blocks":[{"type":"codeowners","base":"origin/main"}]}
```

The renderer runs the check itself rather than including a fragment somebody remembered to
regenerate, pulls in its own stylesheet, and hangs the red **approval required** badge on the
tab when the state says so. No `CODEOWNERS` drops the tab; nothing owned being touched keeps
it and strikes the label through.

## Rules the renderer enforces (so you do not have to)

- **`verdict.bullets` render nowhere.** `verdict.score` is the pill beside the title (and
  the band of colour it wears); the bullets are kept in the file and never drawn. The
  full-bleed amber band that used to hold them said the pill again one screenful lower and
  pushed the findings below the fold. Reasons a reader needs before the list go in
  `summary`, in a sentence.
- `summary` **opens the first tab**, above that tab's own `intro`. They are
  not a tab of their own: a tab is a question the reader chooses, and *what is this change,
  and is it mergeable* is not chosen — it is what the page opens with. So do not declare an
  Overview tab, and do not repeat the summary in the first tab's `intro`.
- **Both `summary` and `verdict.bullets` are optional, and leaving them out is the normal
  case once the findings say it better.** A lede that restates the first finding, and
  bullets that restate the next three, are the same page twice — and the copy on top is
  the one the reader has to get past to reach the list they came for. Write a summary only
  for what the findings cannot say: what the change *is*, in a sentence. `{{tabcount}}` and
  the walk-through check apply to a summary that exists; a page without one is not warned
  about tabs it never promised to name.
- The score beside the title **links to the tab that renders `findings`** — the reasons
  behind `5/10 not yet mergeable` — so keep the findings in one tab.
- A tab whose every block came back empty is **dropped** and named in the build log.
- A tab with content but **no delta** is kept and its label **struck through**, with a
  tooltip saying so. `noStrike: true` opts out. A `puml` block never carries a delta — a
  context diagram can be the same picture at both ends of a branch — while **`codecity`
  always does**: the lit buildings *are* the classes the change set touched, so a city with
  anything in it is a city this branch changed, and the tab was being struck through over a
  picture whose whole subject is the change. A `section` counts as a delta unless it
  declares `"unchanged": true`.
- A changed diagram no tab claimed prints a **warning**.
- `count: true` puts the item count on the tab, `badge: "…"` a literal, `badgeClass`
  paints that badge; `badgeLabel` names it for `aria-label` and `data-tip`.
- **A blocked tab is red, it does not wear a mark.** `tabClass: "alarm"` colours the pill's
  own label — that is what the CODEOWNERS tab gets when the branch touches somebody else's
  files. It used to grow a `!` in a red circle beside its name, which is the same fact said
  as an ornament: a glyph the reader decodes hung off a word that could carry the colour
  itself. With the mark gone the words have nowhere on screen to live, so `badgeLabel`
  becomes the button's `aria-label` — which has to restate the tab's own label too, because
  `aria-label` replaces the accessible name rather than adding to it.
- **A coloured dot on a tab is a verdict, and only a computed verdict may wear one.**
  `badgeClass: "dot-green" | "dot-amber" | "dot-red"` paints the badge as a traffic light,
  which is the strongest claim anything on this page makes: it is read before the tab's own
  name, from across the room, by someone who will not open the tab to check it. So it is
  reserved for a verdict some tool reached and the tab itself shows its working for — the
  API tab's green comes from `oasdiff` and the UX tab's red from `ds-audit.py`'s own
  `summary.new.bare`, and in both cases the reader can open the tab and find the report the
  dot is quoting.
  A conclusion **you** reached by reading is not one of those, however sure you are of it,
  and it does not become one by being written into `badgeClass`. The Tests tab wore a red
  dot saying *one requirement has no test* — true, arguably, and entirely a judgement about
  which sentence of a ticket counts as a requirement and which test counts as covering it.
  Beside a green dot that oasdiff computed, it read as the same kind of fact, which is what
  made it worth removing rather than rewording. Say it where a claim can carry its
  reasoning: in the tab, in the findings, in the requirements map's own colours.
- The summary's walk-through is checked against the strip: `{{tabcount}}` is filled in, and
  the build warns when it fails to name a tab or names them out of order.
- Deep links work both ways (`#<tab-id>`, or any `id` inside a panel). The scroll offset is
  derived from the strip's own height, never typed — the strip wraps to two rows at every
  width, which is its normal state.
- Omit `tabs` entirely and you get the original single-column page.

## The cost tab

The **last pill on the strip, past CODEOWNERS, labelled with the money** — `$749`, no
decimals. The build appends it; a content file neither declares it nor can move it, for the
same two reasons the diffstat is not typed: its label is a measurement, and where it goes is
a fact about the page rather than about any one review. It is also left out of `{{tabcount}}`
and of the lede's walk-through, so no content file has to recite the page's own furniture.

It answers *what did this change cost*, which is not the question the old cost chip answered.
The chip could only report the review run — and on the branch this was built for, the
conversation that **wrote** the feature cost $648 against the review's $96. A page that
totals an agent's bill exists so somebody can decide whether this way of working pays for
itself, and reporting only the cheaper half of it answers nothing.

Four sources, in the order the money was spent, each measured by whatever can actually see
it and each saying so when it cannot:

| group | where the number comes from |
|---|---|
| writing the code | `authoring-sessions.py` finds the conversation that edited these files; `review-cost.py` prices it between its first and last edit to them |
| reviewing it | the review passes that **forked** — a subagent has a transcript of its own, so `/code-review` and `/simplify` are priced exactly, and split into finding and fixing by how each was invoked |
| building this guide | the per-tab rows, every turn charged to whichever step's window was open, plus the residual (`assembling the guide`, `subagent`, `the orchestrating conversation`) |
| total | writing + the run + any pass that predates the run's start marker |

Three kinds of honesty the table has to keep, all of them things it got wrong first:

- **A window is not a fence.** The authoring row is costed between that conversation's first
  and last edit to these files. Work inside that window belonging to something else is
  counted, so the row prints the window and the edit count and lets the reader judge its
  width rather than trusting a number that cannot be tightened.
- **Nothing is added twice.** A pass usually runs *before* `/human-review` does — that is the
  design, the guide harvests a review rather than paying to re-derive one — so its cost is
  outside the run's own total and is added. One fired mid-run is already inside it, is not
  added, and its row says so.
- **A zero is not an absence.** A tab a script produced costs nothing and says `$0.00`; a tab
  nothing could measure says *that*, in words, and so does an authoring conversation that is
  not on disk. `$0.00` under `writing the code` would read as "writing this was free".

If **neither** half is measurable — no transcript for the run, and no conversation on disk
that wrote the code — the tab drops itself rather than rendering `$0`, which would be a claim
that this change was free. Either half alone is enough to keep it.

A pass that ran **inline** rather than forking cannot be priced — its turns are interleaved
with the conversation that invoked it, with no marker saying where it stopped. Those are
counted and named, never estimated, and their money stays in the residual where it landed.

## The tab strip

A review is five or six separate questions, answered in whatever order the reader's doubt
takes them, so the page is a **tab strip over panels**, driven by a `tabs` array:

```json
"tabs": [
  {"id":"review","label":"Review","count":true,
   "blocks":[{"type":"findings","title":"Requires human review","body":"…"},
             {"type":"autofixes","title":"Auto-fixed","body":"…"}]},
  {"id":"behaviour","label":"Demo","blocks":[{"type":"section","id":"video"}]},
  {"id":"api","label":"API","badge":"+4","blocks":[{"type":"section","id":"swaggerdiff"}]},
  {"id":"data","label":"Data",
   "blocks":[{"type":"diagrams","only":["DomainModel","DB"]},{"type":"section","id":"conceptual"}]},
  {"id":"requirements","label":"Tests",
   "blocks":[{"type":"section","id":"requirements"},{"type":"tests"}]},
  {"id":"sequence","label":"Sequence",
   "blocks":[{"type":"section","id":"sequences-note"},
             {"type":"testpairs","id":"sequences","kind":"sequence",
              "title":"Each test, beside the sequence its own run recorded",
              "snippets":[{"ref":"petclinic-test/features/add-visit.feature:12-27","caption":"…"}],
              "unpaired":{"id":"tests-nosequence",
                          "title":"Tagged for tracing, and no diagram came back","body":"…"}}]},
  {"id":"packages","label":"Structure",
   "blocks":[{"type":"section","id":"packages-note"},
             {"type":"diagrams","only":["Java packages"],
              "context":{"src":"petclinic-backend/docs/packages.puml","name":"Java packages","note":"…"}},
             {"type":"puml","src":"petclinic-backend/docs/generated/MavenModules.puml",
              "name":"Maven modules","status":"unchanged"},
             {"type":"diagrams","manifest":"assets/c2/MANIFEST.tsv","only":["C2-Containers"],
              "id":"c2-containers","title":"C2 Containers"}]},
  {"id":"city","label":"Code City","blocks":[{"type":"codecity"}]},
  {"id":"dsaudit","label":"UX","tip":"Native controls sitting where a standardised component belongs — found by absence, not by labelling.",
   "blocks":[{"type":"section","id":"ds-audit"}]},
  {"id":"complexity","label":"Complexity","tip":"Cognitive complexity of the whole flow behind each entry point, read from source at both ends of the branch.",
   "blocks":[{"type":"section","id":"complexity-delta"}]},
  {"id":"logging","label":"Logging","tip":"Every logging statement the change set added — found by syntax, not by grep.",
   "blocks":[{"type":"logging","base":"origin/main","paths":["petclinic-backend"],
              "id":"logging-added",
              "existing":{"id":"logging-existing","title":"…","body":"…","snippets":[]},
              "console":{"id":"logging-console","title":"…","body":"…","snippets":[]}}]},
  {"id":"owners","label":"CODEOWNERS","blocks":[{"type":"codeowners"}]}
]
```

A tab's `id` becomes the panel's DOM id, so the Review tab's `intro` must **not** also carry
`id="review"`, and the Complexity tab's section is `complexity-delta`, not `complexity`.

⚠️ **A tab's `id` is a contract with the step ledger; its `label` is not.** The two are
joined by nothing but the string being identical, so relabel freely — *Packages* became
*Structure* with no consequence — but re-key a tab and `run-steps.py` goes on stamping the
old id: attribution finds no row, the tokens fall into the residual, and the tab reports
*not measured*. The step that feeds each tab is declared in `scripts/run-steps.py`'s `STEPS`
table, and `test_tab_ledger_wiring.py` fails until the two agree.

| `id` | label on the reference page | produced by |
| --- | --- | --- |
| `review` | Review | the harvested passes (Step 1) |
| `behaviour` | Demo | `video` |
| `sequence` | Sequence | `sequence` |
| `requirements` | Tests | `tests` |
| `data`, `packages` | Data, Structure | `diagrams`, and `c2` for the container view |
| `api` | API | `api`, `specchanges` |
| `city` | Code City | `city` |
| `complexity` | Complexity | `complexity` |
| `logging` | Logging | `logging` |
| `dsaudit` | UX | `dsaudit` |
| `owners` | CODEOWNERS | `owners` |
| `guide` | *(not a tab)* | Step 4, stamped by the model |

A step naming two tabs has its cost **split evenly**, so never widen a step to a tab that did
none of the work.

**Complexity is a fragment and nothing else.** Its section carries
`"includeHtml": "assets/complexity-delta.html"` and no `embed`. The page once framed an
`endpoint-complexity-explained.html` that the reviewed project's own extractor test wrote
beside the JSON; that test was deleted from the project in September 2026 and the tab went
with it, so the measurement now lives in `scripts/endpoint-complexity.py` and the bars are
the whole tab. A section that embeds a report nothing produces renders as an apology.

Default order, worth departing from only with a reason — **Review, Demo, API, Data,
Tests, Sequence, Structure, Code City, UX, Complexity, Logging, CODEOWNERS**. It is the
order a review actually goes: what the passes raised, then the feature as a user meets it
(the film, then the contract and the shape behind it), then what pins it — the tests, then
the traces those runs recorded — then the code's own shape, where *Structure* and *Code
City* are one question asked twice and stay adjacent, and CODEOWNERS last, because it is
the one thing no amount of reading changes.

**The container view is the last diagram on Structure, and it is not drawn.** It is a C4
*container* view — boxes for the browser, the backend, the database, a queue, a
microservice, and a line wherever one calls another — **projected from the sequence diagrams
on the Sequence tab** by `scripts/c2-from-sequence.py`. That is the only reason it can be
trusted: an architecture diagram somebody maintains by hand is a claim about the system, and
this one is a *measurement* of it, taken from the same traces the sequences came from. A
call to a service nobody documented shows up here the first time a test makes it. Its
caption links out to **c4model.com**, because "C2" is jargon the page has no room to teach
and Simon Brown's own site explains the four levels in a paragraph.

It sits **third on Structure, after Java packages and Maven modules**, and not on a tab of its
own. Structure is already the question *what shape is this thing*, asked at two altitudes —
packages inside one module, then the modules themselves — and the containers are the third
and widest. A pill of its own put one question in two places on the strip and made the
reader choose between them; three diagrams down one panel is the ladder they were already
climbing.

**A test harness is not a container.** A `@SpringBootTest` driving controllers through
MockMvc is a lifeline in every sequence it records and is deployed nowhere — no browser, no
socket, no server — so `"drop": true` in `steps.c2.containers` keeps it out. A Playwright
suite is the opposite and stays: it drives the real front end in a real browser, so that box
is a container that genuinely ships. The rule is what runs in production, not what appears
in a trace.

**A line into a datastore says what it speaks and stops.** No inventory, no `⊕`, no counts,
and nothing under the database box either. Between two systems the operations are a contract
— a finite, named list, and knowing it is most of what the picture is for. Into a database
it is not: one screen fires a hundred statements, the list is unbounded and half-generated,
and `237 calls` on the arrow says only that the ORM did its job. That is the Sequence tab's
question, asked at the wrong altitude.

**Every line between two systems carries a `⊕`, and it opens the calls behind it.** A C2 line says *Backend
talks HTTP to Payments*, which is the right altitude for the picture and exactly one level
too coarse for the reviewer who then asks *which endpoints?*. The answer is already in the
traces, so the protocol on each line is a handle: clicking it opens a bullet list of every
operation that line stands for, the name above the route. Distinct operations only, and no
tally of how often each ran: a route hit seven times instead of three is a fact about which
test happened to run, not about the architecture. For the same reason the arrow reads
`4 ops` and not `4 operations, 13 calls` — it also has to fit between two boxes. On the
delta, a line whose count moved reads `4 ops (was 5)`: `(-1)` is shorter and needs a legend
the picture has no room for, since the first question anyone asks of it is *minus one what,
against what?*. It is the page's existing `genseq://` affordance and `GENSEQ_JS` drives it
unchanged, which is the point: a reader who learnt it one tab earlier, on the sequence
diagrams, does not learn it again here. The inventories ride in two sidecars the manifest
names in `new_details` / `old_details`, keyed by ids derived from content, so an untouched
line opens one entry from whichever pane it is clicked in, while a line whose calls changed
opens the old inventory on `Old` and the new one on `Diff` and `New`.

Four things follow from its being derived, and all four are visible on the page:

- **Its rows are in a manifest of its own**, `assets/c2/MANIFEST.tsv`, named by the block —
  `{"type":"diagrams","manifest":"assets/c2/MANIFEST.tsv","only":["C2-Containers"]}`. The
  name in `only` is `steps.c2.name` from `human-review.json` (default `C2-Containers`),
  which is the *view key* rather than the level: `C2` alone names the level in the C4 model
  the way `C3` does, and a card titled with it says which shelf the picture came off rather
  than what is on it. A project whose C4 lives in a Structurizr DSL already has that key —
  petclinic's reads `container petClinic "C2-Containers" …` and exports
  `C2-Containers.puml` — so spelling it the same gives one name for one picture across the
  model, the export and this page. Give the block an
  `id` of its own (`c2-containers`): a diagrams block with a title and no id heads itself
  `id="diagrams"`, and two of those on one page is one anchor pointing at two panels. It cannot file
  them in the shared gallery's `assets/diagrams/MANIFEST.tsv`, because `puml-diff.sh` does
  `rm -rf` on that directory on entry, and the `sequence` step runs it *after* the
  `diagrams` step wrote it. A block naming its own manifest is otherwise an ordinary
  diagrams block: same Diff / New / Old control, same colours.
- **The delta is taken on the graph, not on the PlantUML.** The two sides are not two files
  but two whole *sets* of sequence diagrams — the work tree's and the merge-base's — and
  `puml_diff.py` refuses the C4-PlantUML dialect outright. Green is a container or a call
  this branch introduced; red is one it removed; a surviving edge whose traffic changed
  stays neutral and says `(+3)` on its technology line, because a chattier call is news and
  is not a change of shape.
- **The label is decided on the parts of a message, never on the sentence.** A generated
  HTTP call is two lines — what it is called, then where it goes — and the route is
  whichever line is a verb followed by a path. `split_operation` finds it, the popup prints
  it under the name, and finding one *is* the protocol. Deciding it by regex over the
  joined string is what once labelled `List owners GET /api/owners` as `calls` and
  `Get an owner by ID GET /api/owners/{id}` beside it as `HTTP`: two lines into the same
  box, labelled two different things, for no reason a reader could see.
- **What is guessed is written down.** A sequence diagram does not say whether a lifeline is
  a datastore or a queue, so `steps.c2.containers` in `human-review.json` decides it first,
  the PlantUML keyword (`database`, `queue`, `actor`) second, the name third, and a plain
  container last — and `C2.json` records which of the four rules fired for every box, under
  `inferredBy`. Technology (`Angular`, `PostgreSQL`) is never guessed: it comes from the
  config or the box goes without one. `{"as": "Browser"}` folds two lifelines into one
  container, which is what a suite that names its own driver `Client` needs so a renamed
  test harness does not read as an architecture change.

Four tabs need something said about how they are written:

- **Review** — the label is the word alone. The 🤖 it used to carry announced that
  the tab was machine-produced, which the `source` stamp on every item inside it already
  says, one item at a time. **One list** of three piles, numbered straight through: what only the
  reader can answer (`assumptions`), then the open calls, most critical first, then the
  fixes already applied, greyed out. Lists that each start at 1 make the reader do
  arithmetic. The numbering follows the order the blocks appear in here, so that order is
  an editorial choice — with one rule the build enforces: work already done is the tail.
  Whichever pile opens the list writes its lede — `9 open LLM review issues · 3
  auto-fixed · 2 assumptions` — computed, so restate none of it. It **pins under the
  masthead** for the whole panel, because the three chapters it names are thousands of
  pixels apart and the reader wants the second one from inside the first. Each
  clause links to the chapter it counts (`#first`, `#fixed`, `#assumed`, or the block's own
  `id`), so the line doubles as the tab's contents; a pile the layout never lays out keeps
  its count as plain text rather than offering a dead anchor. The coder's clause is the one
  that renders at zero (`0 assumptions`), because that zero is a result: it says
  the authoring conversation was asked. Mode C prints `coder could not be asked` instead,
  the one case where a zero would be claiming an answer nobody was there to give. It is counts and one ordering fact,
  and nothing that describes what is directly under it: the applied fixes are visibly
  grey, an assumption visibly says *your call*, and every item carries its source beside
  its own title, so `greyed out`, `yours to confirm` and `each stamped with the pass that
  raised it` were all cut, one at a time, from the line that replaced the paragraph.
  **Give this tab no `intro` at all.** It carried one sentence naming the passes that ran
  (`/code-review ran against origin/main.`) and that sentence was both redundant — every
  item is stamped with the pass that raised it, and the scope bar names the base — and
  wrong as often as not: which passes ran, and what against, is not something the line was
  ever computed from. The tab opens on the counts line, which *is* computed. The paragraph
  that used to stand above the list ("Twelve items came back. They are one list: the nine
  that need your judgement first…") described the list it was standing on.
- **Tests** — two columns that read as a question and its answer, both built the same way:
  **a heading in the page's voice, then a framed card**.
  - **Left, the question**: the ticket the branch answers, its sentences coloured by
    coverage, pasted in as the section's `includeHtml` and hoisted above everything with
    **`"includeFirst": true`**. Head it the way the issue heads itself — **title, then
    `#number`, the number linking to the issue**: that is the handle the work is quoted by,
    and a page showing the ticket's text without its number sends the reader hunting.
  - **Right, the answer**: **every acceptance test the branch offers, listed always** —
    not a slot that fills when something is clicked. One row each, badged with its kind
    (`UI` — it clicks the screens; `API` — the contract under them), the location a link
    into the editor, and the source on an **accordion** (one open at a time). Selecting a
    sentence marks the rows that cover it and steps the rest back rather than hiding them:
    *what did not get picked* is half of what the column is for. Counts per kind ("×4
    asserted") head nothing — nobody acts on them.
  - **Head the tests card the way the ticket heads itself** — one strip, an icon and a
    name — and give the ticket no title of its own: the masthead already carries `GH#37
    <title>`, and repeating it above the card pushed the ticket a screen down. In the
    avatar's place goes the 🤖: the whole column is inference.
  - **A test draws the sentences it pins only when asked to**, through a control of its
    own: a **◀ arrowhead leading the row**, pointing across the gap at the ticket it draws
    into. The relation is many-to-many in both directions — a sentence has several tests,
    a test pins several sentences — so it has to be drawable from this side too; but
    *opening* a test is a request to read its source, not a claim about coverage, and
    hanging the outline on that click left the reader looking at three lit sentences they
    never asked for. Opening is now only opening. Pressing the arrow outlines the
    sentences in the same green the row itself wears, pressing it again releases them, and
    only one row draws at a time.
  - **The two directions are exclusive.** Selecting a sentence releases whatever a row had
    drawn, and pressing a row's arrow releases the sentence — "these tests cover that
    sentence" and "that test pins these sentences" are two different relations, and both
    on screen in the same green leaves the reader working out which is which. On the
    ticket's side the sentence *is* the control, so it needs no arrow of its own — and
    **clicking it a second time puts the ticket back**: there was no way out of a selection
    except by making another one, so the column stayed sorted into "these prove it" and a
    dimmed rest long after the reader had stopped asking. The dimming is an answer, not a
    resting state.
  - **The relation is drawn in the gutter, not only implied by colour.** An outline says
    *which* sentences a test pins; on a ticket four screens long it never says which of
    them belong together, and with the card sticky the row that pinned them is usually
    beside none of them. So a pressed row draws one **wire** per sentence it pins, from a
    **dot on the ticket's own border** — on the line the sentence sits on, at the middle of
    the whole wrapped block of it — across the gutter to a matching dot on the card's edge.
    That is what the 46px gutter is for; it was 20px of nothing, and the left column pays
    about three characters a line for it. Both ends are the same mark, so the drawing says
    *this row, those places* and claims nothing else: a brace tried to claim a vertical
    extent as well, and two sentences sharing a line of prose overlapped into a tangle no
    reader could read a span out of.
  - The wire hangs off the row's **title**, never off the row. An open row is its header
    plus however many screens of source it quotes, and the middle of that is somewhere down
    in the code — the wire has to land on the name of the test, which is the part that
    stays put and the part the reader is looking at.
  - The wires are **geometry, not state**: they redraw from wherever the two columns
    currently are, on page scroll, on resize, and when a row opens and moves the rows under
    it. A wire's landing point is still **clamped to the card's visible edge** rather than
    left pointing off into nothing — where it lands is then the direction the reader has to
    scroll. Under 900px the columns stack, there is no gutter, and nothing is drawn.
  - **The card has no scrollbar of its own.** Capped at the viewport it grew one, a few
    pixels in from the window's own, and the two bars moved different things: reading forty
    lines of an opened test meant scrolling the inner one while the page stayed put, then
    finding the outer one again to leave. No `max-height`, no `overflow-y` — the card grows
    to whatever it holds and the window scrolls it, which is the one bar the reader already
    knows. (`overflow:hidden` stays, to keep the corners round; there is no height left for
    it to clip against. Wide code still scrolls inside its own block.) The column's
    `position:sticky` goes with it **while a row is open** — `:has(.rm-t[data-open=yes])` —
    because a sticky element taller than the window pins its top and puts its own bottom
    permanently out of reach.
  - The control's face is **the picture of what pressing it draws**: one trunk on the right,
    where the test is, fanning into three branches reaching left at the ticket. An
    arrowhead only said *that way*. Drawn as inline SVG rather than borrowed from a font —
    nothing in any stack is this shape, and the glyphs that come close are a different
    picture at 12px.
  - A row whose test pins nothing renders the arrow **hidden, not merely faded** — the
    badges behind it stay in one column, and a control that would do nothing is worse than
    no control.
  - The **disclosure arrow leads the title**, after the ◀ and the badge: at the far
    right of a variable-width row it is a control nobody finds. It points *down* into the
    row's own body while the ◀ points *left* at the ticket, so the two say which way they
    act before they say anything else. Opening animates (a height transition); a pane that
    appears by snapping makes the rows below look like they moved somewhere else. A **diff badge** on a
    quoted file (`new file`, `2 lines changed`) is small and sits immediately before the
    path it describes, not left-aligned as a banner of its own. Every line of a part the
    branch **added** carries its `+`, including a whole new file: the marks answer "what
    is new here", and a new file where nothing is marked reads as a file nothing touched.
  - **One cursor convention, page-wide**: a mark that only explains itself on hover gets
    `cursor:help` — the question mark says both "there is something to read" and "clicking
    does nothing". Anything actionable keeps the hand.
  - The list is **every test the change set touched**, not only the ones that cover a
    sentence, and **the deleted ones belong in it too** — a page that lists a test
    somewhere else and not here makes the reader hold two lists and diff them. Tests the
    manifest knows but the map has no excerpt for get their excerpt cut at build time.
  - **The kind of test is read off the file name, in amber.** The location on a row
    (`add-visit.feature:20`, `visits-page.component.spec.ts:78`, `VisitTest.java:197`)
    wraps the run of extensions after the name in `<span class="rm-tk">`, coloured with
    `--drift` — `.feature` is a Gherkin scenario, `.spec.ts` a Playwright test,
    `.component.spec.ts` an Angular component test, `.java` a JUnit one. The badge says
    UI/API/unit; the suffix says *which framework*, and thirteen rows of near-identical
    monospace names hide it. The name and the `:line` keep the link colour.
  - **An untouched test wears a mark too**, not silence. On a list headed *every test this
    change set touched*, a row with no stamp reads as a gap the build left, not as a fact
    — the reader stops to ask whether it was forgotten. So a test the branch never edited
    (no `status` in the data, or `"unchanged"`) gets the page glyph alone, no badge, in the
    muted grey the excerpt's own `unchanged` badge already wears one level down, and the
    hover says why the row is here at all: *unchanged — this branch did not touch this
    test; it is listed because it runs through code the change set edited*. The subject of
    that glyph really is a file this time, which is why it is the page and not one of the
    three blown-up badges.
  - A **deleted** row is listed and inert: this checkout has no source to quote, so it does
    not open (no disclosure arrow, no tab stop, no `role="button"`), its name is struck
    through the way the run struck it, and its location keeps the words but loses the link
    — a dead `vscode://` URL is the one thing this page never emits.
  - Once the card carries all four states, **the ledger at the foot of the tab is the same
    rows a second time**, grouped by a question the stamps already answer. Turn it off with
    **`"testLedger": false`** at the top level of the content file. The header strip then
    reads *as matched by AI*, and nothing else. It has said three things in turn and each
    replaced a worse one: first how many tests the branch **left alone** — a count of the
    tests a change set did not touch is not a fact about that change set, it is the size of
    the suite; then *every test this change set touched*, which was true and was the one
    thing the 🤖 beside it had already said was uncertain. The line under a heading is where
    a reader looks for the caveat, and the caveat here is that which test covers which
    requirement is a model's reading, not a measurement — the states on the rows come from
    `test-changes.py` and are, but the pairing is not. Said out loud rather
    than inferred: the build cannot read a hand-authored fragment and know what is in it,
    so with the flag off it prints a reminder that every moved test, deleted ones included,
    now has to be listed some other way.
  - **The excerpts link to each other, and the build draws the links — do not author them.**
    `code_xref.py` runs over the assembled page, reads every quoted window there is (the
    snippets `extract-snippet.py` cut *and* the `parts` this fragment carries as JSON), and
    writes the cross-references back into both. A Gherkin sentence becomes a link to the step
    definition that matches it; any identifier one window *defines* becomes a link from every
    window that *mentions* it. A plain click unfolds the window it points at, flashes the
    line and stays on the page; ⌘-click opens it in the editor, which is the gesture the rest
    of the page already uses. Nothing is invented: a name no quoted window defines stays plain
    text, so what is underlined is exactly what is followable.
    - **A part something links to is folded, and the first part of a test never is.** The
      reader opened the row to read the test; the glue and the helpers under it are what they
      reach *through* the test, and expanded they are three screens of Playwright standing
      between the row and the four sentences it was opened for. A folded part costs **one
      row**: its first line of code goes in the empty left half of the source bar it already
      had, so it says what it is without being unfolded and without spending a row to do it.
      What the bar clips is on the stub's own tooltip, because reading the end of a
      signature should not cost a click that changes the page.
    - The wiring needs two things from the markup, and they are both things this fragment
      already emits: each excerpt in a `.rm-part`, and inside it the editor link from the
      source bar (`a.srcref` that is not a `.rm-diff`) carrying the same `href` as the part's
      `href` in the JSON. That link is how a part rendered in the browser says which window it
      is. Rename either and the excerpts render exactly as before, silently unlinked.

- **Head every quoted block with the page's source bar — do not author your own.** It is
    one component, `.srcbar`, and its rules ship in the snippet stylesheet the page already
    injects, so a fragment gets it by using the class names:

    ```html
    <div class="srcbar">
      <a class="srcref srcbar-diff" …><svg class="ico ico-vsc" …></a>
      <a class="srcref srcbar-diff" …><svg class="ico ico-gh" …></a>
      <a class="srcref srcbar-path" href="vscode://file/…" data-tip="<repo-relative path> — open in VS Code">Name.java:76-79</a>
      <span class="code-badge" data-diff="new" data-tip="…">new file</span>
    </div>
    ```

    Read left to right, as one group at the right end: the two ways to open it, the file
    they open, then what changed in it. **The handles lead and touch the name**, because
    the name is what each of them opens — a handle a bar's width from the only word saying
    what it would open is a pairing the reader has to make. **The badge trails**, because
    `new file` is a fact *about* a file and leading with it makes the reader hold it in
    mind across the whole row before the row says which file is new. **The face is the
    file's name and the full path is on hover** — "which file is this?" is the only question the row exists to answer, and a
    repo-relative Java path spends five segments on module, `src/main/java` and the org
    package before it gets there. A file at the repo root has no path to move and gets no
    tooltip repeating its own name.

    A fragment that rolls its own version of this row is a fourth header the reader has to
    learn and the one place a fix to the other three will not reach. The Tests, Review and
    Logging tabs all emit this exact markup; a hand-authored map is not the exception.
  - **The badge over a quoted block is the same file glyph, one level down.** The row's
    stamp says what the branch did to the *test*; this says what it did to the *window of
    code* under it — green file-and-plus for a whole new file, orange file-and-pencil for
    new or rewritten lines inside an older one, a plain grey file for a window nothing
    touched. `new file` / `new code` / `2 lines changed` / `unchanged` were four widths of
    shouty caps stacked down the left edge of every excerpt, and the reader read them
    instead of the code. Nothing is lost: the words lead the hover, and the `+` marks in
    the gutter give the count line by line.
  - **A tooltip says what it does before it says what it opens** — `Open in VS Code:
    <path>`, never `<path> — open in VS Code`. A tip that opens with a sixty-character
    path makes the reader parse the path to find out whether the sentence at the end is
    worth reading, and the four words that never change are cheaper to skip than to hunt
    for. The bubble also needs `overflow-wrap:anywhere`: a repo-relative path is one
    unbreakable token as far as line breaking goes — no spaces, and a slash is not a break
    opportunity — so the longest tip on the page overflowed its own `max-width` and was
    clipped at the edge, which reads as the page running off the screen.
  - **Every quoted file offers its diff, and each handle wears the mark of where it
    opens** — the VS Code ribbon (the base on the left, the working tree on the right,
    through the served page or the editor's URI handler) and the GitHub octocat (the same
    file inside the open pull request, anchored by the sha-256 of its path). A logo, not
    initials: `⇆ VSC` and `⇆ GH` were three monospace letters the width of a short file
    name, so a row whose whole job is to say *which file is this?* read as three words of
    equal weight, two of which had to be decoded first. The marks are 14px, **grey at rest
    and lit under the cursor** — two logos in full colour beside a file name are the
    brightest thing in the row and the least of what it says, so at rest they sit at the
    weight of furniture and the name reads first; on hover the octocat comes up in the
    page's own ink (its mark is monochrome by its own brand — never the link blue it would
    otherwise inherit, which reads as a decorated arrow) and the ribbon in its blue, which
    is the only thing that makes it VS Code. **No underline** under either: the dotted rule
    was there to keep three letters from reading as prose. They sit at **two thirds of the
    row's gap** from what follows them — a mark is a smaller thing than the words around it
    and floats off the name given the same air. The `aria-label` still spells out what each
    opens, for a reader who cannot see the mark; the **tooltip is two or three words**
    (`Open Diff in VSC`, `GitHub`), because a mark in a row of three is hovered to answer
    *what is this?*, and the sentence a prose link can afford is a paragraph in a corner.
    Each is emitted only where that side can really show it: no editor diff for a file with no
    before-state, and no github.com link for work github.com has not seen — the file dirty
    at HEAD, the branch unpushed, or no pull request open on it. **Take the pull request
    number from `content.json`'s `pr` block**, never from whatever number was in the last
    map you copied — a link to somebody else's pull request looks exactly like a working one.
  - **A github.com link ends in a line, never in a bare file anchor** — `#diff-<sha256 of
    the path>R<line>`, aimed at the first line the branch added in that window (the top of
    the window when it added none). Not a nicety: github.com's Files-changed view renders
    lazily, so `#diff-<sha>` alone **does not scroll at all** on a large pull request — the
    reader lands wherever the page happened to be, several files away, which is
    indistinguishable from a broken link. With `R<line>` the same URL scrolls to the hunk.
    `L<line>` is the left side, and the only landing a pure deletion has.
  - **Quote the whole test method, always.** A window that stops at the last assertion and
    never shows the closing brace is a method the reviewer cannot see the end of — and they
    cannot tell whether that is the range or the code, having no file open to count
    against. Cut with `extract-snippet.py`'s own snapping (`_first_code_line` then
    `_closing_line`), which skips the previous construct's trailing brace at the top and
    extends the bottom until whatever the range opened is closed. **The line count must
    equal the label's span**: `a.ts:27-43` is seventeen entries, not sixteen. A whole map's
    excerpts were once cut `lines[a-1:b-1]`, one line short each, and every method on the
    tab lost its closing brace — invisibly, because a short window looks exactly like a
    complete one. The build now says so on stderr (`check_baked_excerpts`); it warns rather
    than refuses, so **read the build's output**.
  - **Any code baked into the fragment is cut against the working tree, and its line
    numbers are checked.** A snippet quoted from an older revision keeps text that still
    looks right beside numbers that are two lines out, and every link into the editor
    lands in the wrong place — worse than no quote, because nothing about it looks wrong.
    Take the line a row links to from the test's own declaration, never from the first
    line of the window quoted around it.
  - **Unpack identifier test names in the list** — `getById_exposesTheAttendingVet` reads
    as "Get by id exposes the attending vet". Scripted, not written: split on `_` and on
    the camel humps, lowercase everything that is not an acronym, capitalise the first
    word. A name that already contains a space is a sentence someone wrote (a Gherkin
    scenario, a Playwright title) and is left exactly as it is — including whatever
    `{placeholders}` or paths it carries.
  - A **blind spot** says *whose* hole it is — `in the tests` (orange: a claim the ticket
    makes and nothing reaches) or `in the requirement` (violet: the ticket never said, and
    no test can be written for a sentence nobody wrote). They are different findings, go
    to different people, and "blind spot" alone leaves the reader deciding which.
  - **Stamp each row with what the branch did to that test** — a **big green plus** for one
    this branch wrote, a **yellow pencil** for one it changed, a **red cross** for one it
    deleted, and *nothing at all* for one it left alone. **The mark stands alone here: no
    page around it.** The subject of the row is a test, and wrapping the plus in VS Code's
    `new-file` page said *this branch added a file* — false for one test among nine in a
    file that already existed, and the `aria-label` said it out loud too (`new file`, on a
    test). The page shape belongs one level down, on the excerpt's own badge, where the
    subject really is a file; the three badges are shared between the two so a reader learns
    plus/pencil/cross once. **The whole mark takes the colour** — this started out grey with
    only a 4px badge coloured, which put the entire distinction on the smallest part of the
    glyph and left the row reading as grey furniture. Colour says *what happened*, the shape
    says the same thing again, and the cross is the plus turned 45° because it is that fact
    with the sign flipped. Icons, not words: `NEW` in a pill was a label loud enough to be
    read before the test's own name, on a row whose subject is the test. The words survive
    as the `aria-label` (`new test`, not `new file`) and the hover. A new test is evidence a requirement was
    pinned; an edited one is evidence a pin moved, and is worth opening for what it
    stopped asserting. Silence on the untouched rows is the point: a stamp on all of them
    would say nothing, and this is the fact the old evidence cards carried in prose. The
    stamp reads from the **manifest**, never from the diff of the quoted lines: a test
    edited three lines above its excerpt is edited, and a pencil is the whole word. **It
    closes the row, to the right of the location** — the same order the source bar inside
    the row reads in, and the same reason: `new` is a fact *about* a file, so it comes after
    the file has been named. Between the title and the location it sat between two things
    that belong together and read as part of the sentence naming the test.
  - A **gap** is a criticism of the *requirement*, so it belongs on the requirement's side:
    render it under the ticket as a **blind spot**, never under the code on the right where
    it reads as a verdict on the test that happens to be open.
  - **Colour legend: the word *is* the swatch.** One row under the ticket's frame, one
    **word** per state (**full · partially · executed · missing · N/A**), each set at the
    ticket's own font size and wearing the **exact fill that state wears up in the prose** —
    same declaration, written once for both (`.rm-f[data-cov=x], .rm-lg[data-cov=x]`), so
    the legend cannot drift into a colour the ticket does not use. No chip beside the word:
    a 15×9 swatch and a highlighted phrase are different surfaces, the same gradient reads
    darker in the small one, and the hatch for `partially` had barely two bands to show.
    Lead the row with a muted **`Legend:`** — five framed words under a ticket are five
    things the reader has to recognise as a key before they can use it as one.
    **Spread the row edge to edge** (`justify-content:space-between`) under the frame, so
    the five words sit under the width of text they explain instead of bunching at the
    left. **It shrinks along x, it never folds** (`flex-wrap:nowrap`, and a font size that
    falls with the column: `container-type:inline-size` on `.rm-text`, then
    `font-size:min(1em,calc(3.2cqi - 0.5px))` on the row, with the pills' padding and the
    gaps in `em` so the whole thing shrinks together). Wrapping put `N/A` on a second row
    directly over the ticket's first sentence, where a badge reads as part of the prose
    rather than as the key above it — and it cost the ticket a line of height exactly on
    the narrow window that had least to give. The coefficient is the measurement: the five
    words, their padding and the four gaps are 30.6em wide, `100/30.6` shaved to `3.2`
    because the five 1px borders do not shrink with the font. Give `.rm-text` an explicit
    `width:100%` in the stacked (`max-width:900px`) layout — `align-items:flex-start`
    sizes it from its content there, and a content-based width is the one thing
    `container-type:inline-size` refuses to compute, so the column measures 0 and the
    legend inside it renders at a 0px font. **A thin grey frame around all five**, so the row reads as five badges rather
    than five words that happen to be coloured — on all of them and not only the filled
    ones, because `N/A`'s state *is* no fill and without an edge it is a bare word the
    reader cannot tell is the same kind of thing as `full`. Name the states, not their colours. **The hover is one short sentence** — *Only
    part of this claim is covered by tests* — not a phrase-and-em-dash restating the word
    the reader just hovered. The kinds get their own one-liner under the tests card.
  - **Nothing repeats the fill.** `partial` and `executed` used to carry a dashed underline
    as well, repeating "incomplete" for anyone reading the shade wrong. The hatch and the
    orange are already a different *kind* of fill; the second mark only made two of the
    five states look like a vocabulary of their own. Background alone, in both places.
  - **Say what a model inferred.** A heading over inferred content carries a 🤖 superscript
    reading *as inferred by AI*; the logging tab's verdicts carry the same mark reading
    *LLM evaluated* — and the one verdict meaning "the model was never reached" carries
    none. A hover on a sentence carries **the badges themselves** (`data-tip-html`, so the
    tip renders the same pill the list on the right does) with a count each, **and nothing
    else**. Naming the kinds in prose makes the reader translate back into the badge they
    are already looking at. It used to end `click to highlight`, on the reasoning that
    nothing else on the page announces a sentence is clickable — but a tooltip that names
    the badges and then tells you to click is two sentences in a bubble read standing up,
    and the second one is about the tooltip rather than about the tests. The row a reader
    is pointing at already looks pressable. A sentence with no test is the exception and
    keeps its one line, because *that* is a fact rather than an instruction.
  - With that column on the page, **do not also write an evidence-cards section** listing
    what the branch wrote — it is the same list, in fewer words, further down.
- **Data** — the DB and domain deltas, and 2–5 core-logic bullets in domain language, each
  backed by a snippet. It also carries the **conceptual model**, the one diagram on the
  page a human drew, and four rules go with it:
  - **It goes last on the tab, under the generated diagrams.** The machine-extracted
    pictures — `DomainModel`, `DB` — are what the branch did; the hand-drawn map is what
    the team means, and it is the one the reader is asked to act on. Walking the tab ends
    on it rather than opening on it, which is also the order it gets presented in.
  - **Head it the way the file names itself** — `Conceptual Model`, both words capital.
    It is the name of an artefact the team maintains, not a description of one.
  - **Write `{{drawio:conceptual}}` for the picture — never paste the SVG in.** The token
    expands at build time from the `<name>-{diff,new,original}.svg` and `<name>-diff.json`
    that `drawio-diff.py` wrote, and it is the one diagram on the page whose whole point
    is that *the reader is being asked to go and change it*: red is automation's to-do,
    and the picture is supposed to look different once they have re-laid it out in
    draw.io. Markup pasted into this file freezes it — they re-draw the map, rebuild, and
    get back the drawing that was current when you wrote the section. Re-running the
    `diagrams` step and rebuilding is then the whole refresh.
  - **The legend and the opening pane come with the token**, because both are readings of
    the verdict rather than choices: the *added by this PR* row appears when something is
    added and not drawn red, the *still waiting for a manual re-layout* row when anything
    in the drawing is still red (repeated under the **New** pane, where the red is on
    screen with nothing else to explain it), and the widget opens on `New` while a layout
    is owed — the delta is a picture of automation's routing until someone draws it — and
    on `Diff` once it is not. Do not restate any of that in prose that will outlive it.
  - **The colours explain themselves, so do not explain them in prose.** The legend names
    the state and the map itself carries the instruction, written in red by
    `conceptual-model-patch.py` next to the red it is about — and both are gone the moment
    the layout is drawn. The same sentences written above the picture outlive the red: the
    colour goes, and the paragraph keeps telling the next reader to go and turn lines black.
  - **One sentence above the picture, about the artefact and not about this build.** What
    the drawing is and what holds it honest — *Hand-drawn in draw.io, but checked to match
    the code by `ConceptualModelDiagramTest`* — and nothing that a rebuild can falsify.
- **UX** — the only tab whose finding is an absence, and the only one no other check in the
  repository can produce.
