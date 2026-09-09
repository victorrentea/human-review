# `content.json` — the shapes

Loaded on demand from Step 9. The runbook names the keys; this file says what goes in them.

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

`diffs` entries are `{"path": "…"}` and nothing more in the ordinary case — `base` defaults
to the rev Step 1 recorded in the ledger, which is the only left side that shows a fix *on
its own*. Add `"head"` when the fix is one commit and the file moved for other reasons
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
ranked as one. It renders with its own violet card, a `your call` badge and the stamp
`assumption` where a finding carries `/code-review`, because it did not come from a pass:
it came from the agent that wrote the code, via one of the three modes in SKILL.md's *third
pile*. `alternative` is the reading that was not taken, and it is what makes the item
checkable at a glance.

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
  {"auto":"autofixed","href":"#review"},
  {"auto":"cost"}
]
```

The scope bar is read as a row of signed numbers, so the signs are a convention and not
a per-chip choice: **`+` added, `−` removed, `±` changed** (`files <span class="added">+1</span>
/ ±40`). Never `~` for the changed ones — a tilde reads as an approximation, and "about
forty files were touched" is not what the number means.

`value` is raw HTML on purpose; `href` makes the chip a link. The four `auto` chips are
**computed, never typed** — `diffstat` measures the change set with `git diff`, `autofixed`
counts the page's own two lists, `cost` runs `review-cost.py` over the run's transcript,
`tests` reads the manifest `test-changes.py` already built for the tab below. All four drop
themselves rather than print a wrong number. A chip whose number is typed by hand goes
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
the chip reads `Opus 5 review  12 raised · 9 open` rather than needing a second, unverifiable
`reviewed by` chip beside it. `{"by":"Opus 5"}` is the fallback for a page rebuilt outside
the session that reviewed it.

The `tests` chip is a **balance**, not a count — `+10 / −4 / ±4` — because the
question it answers is whether the branch left fewer tests running than it found. The
loss is one number over three causes (deleted, commented out, left standing under an
`@Disabled`), split only in its tooltip: all three cost the run the same test, only
deletion is visible in a diff, and a face carrying all three would invite reading the
smallest of them as the answer.

```json
"extraCss": ["assets/openapi-diff.css", "assets/openapi-compat.css",
             "assets/complexity-delta.css", "assets/ds-audit.css"],
"testChanges": "assets/test-changes.json",
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
moved, grouped by what happened to it) · **`logging`** (Step 7c) · **`puml`** (a diagram this
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

**`codecity`** has no heading either, and its `title` is ignored for the same reason: the
tab says *Code City* and the picture is the first thing under it. What is left is `body`,
and it is **one line** — the measured count and what the lit slice is. The panel and the
hover card are inside the shot, in the renderer's own words, so a paragraph explaining
what a building, its height or its colour mean is the picture read aloud to someone who
is already looking at it. **`codeowners`** has no default heading either: the pill says
*CODEOWNERS*, its badge says *Code owners approval required* and the seal under it says
*APPROVAL REQUIRED*, so a fourth `Code owners` above the first filename is the label said
again. It still renders a `title` you write on purpose.

**`tests`** takes no configuration — it renders `testChanges` — and you do not have to
declare it: a page that has a manifest and no `tests` block gets one appended to the
`requirements` tab. Declare it only to put it somewhere else in that panel.

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
 "snippets":[{"ref":"petclinic-test/features/add-visit.feature:12-27","caption":"…"}],
 "unpaired":{"id":"tests-nosequence",
             "title":"Tagged for tracing, and no diagram came back","body":"…"}}
```

**`snippets`** is the pool the pairing draws from — you quote the tests, the block works out
which diagram each belongs to, and you never name a diagram. **`unpaired`** names the group
the leftovers land in; omitting it accepts the defaults rather than turning the group off.

Both absences render, and they are different absences: a test with no diagram falls into the
trailing `unpaired` group; a diagram with no test says either *"Generated by `<path>`, not
excerpted here"* or *"…which is not in this checkout — the diagram is the only record of it
left."* A fabricated pairing is a lie about provenance; a silent one is the loss this
pipeline exists to prevent.

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
  "base": "http://localhost:4200",
  "reset": "/__reset"
}
```

`runtime` puts a bar above the player: the command that brings the environment back, and a
box for the URL that command prints. Paste it once and every relative `appLinks` href
points into the running app; it is remembered per page, so a reload keeps it. `base` is the
fallback the links use before anything is pasted — with neither, they render grey and
unclickable rather than pretending to lead somewhere.

The bar asks `GET <base>/healthz` whether anything is listening, so an environment that
wants the live/not-running pill must answer it with CORS open. `reset` is **optional and
opt-in**: give it a path the environment answers on `POST` to put the data back to its
starting point, and a "Reset data" button appears. Omit it and no button is drawn — which
is the right thing whenever nothing is there to answer, since a button that always fails is
worse than no button. Resetting is never automatic: doing it on every link click would
throw away work the reviewer was in the middle of.

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

- `summary` and `verdict` **open the first tab**, above that tab's own `intro`. They are
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
  tooltip saying so. `noStrike: true` opts out. `puml`/`codecity` blocks never carry a delta;
  a `section` counts as one unless it declares `"unchanged": true`.
- A changed diagram no tab claimed prints a **warning**.
- `count: true` puts the item count on the tab, `badge: "…"` a literal, `badgeClass: "alarm"`
  makes it a red `!` (the phrase moves to `aria-label` and `data-tip`); `badgeLabel` sets it.
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

## The tab strip

A review is five or six separate questions, answered in whatever order the reader's doubt
takes them, so the page is a **tab strip over panels**, driven by a `tabs` array:

```json
"tabs": [
  {"id":"review","label":"Review","count":true,
   "intro":"<p class=\"sub\">Both /code-review and /simplify ran, and their output was merged before it reached this page…</p>",
   "blocks":[{"type":"findings","title":"Requires human review","body":"…"},
             {"type":"autofixes","title":"Auto-fixed","body":"…"}]},
  {"id":"behaviour","label":"Demo","blocks":[{"type":"section","id":"video"}]},
  {"id":"api","label":"API","badge":"+4","blocks":[{"type":"section","id":"swaggerdiff"}]},
  {"id":"data","label":"Data",
   "blocks":[{"type":"section","id":"conceptual"},{"type":"diagrams","only":["DomainModel","DB"]}]},
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
             {"type":"diagrams","only":["Packages"],
              "context":{"src":"petclinic-backend/docs/packages.puml","name":"Packages","note":"…"}}]},
  {"id":"city","label":"Code City","blocks":[{"type":"codecity"}]},
  {"id":"dsaudit","label":"UX","tip":"Native controls sitting where a standardised component belongs — found by absence, not by labelling.",
   "blocks":[{"type":"section","id":"ds-audit"}]},
  {"id":"complexity","label":"Complexity","blocks":[{"type":"section","id":"complexity-delta"}]},
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
| `data`, `packages` | Data, Structure | `diagrams` |
| `api` | API | `api`, `specchanges` |
| `city` | Code City | `city` |
| `complexity` | Complexity | `complexity` |
| `logging` | Logging | `logging` |
| `dsaudit` | UX | `dsaudit` |
| `owners` | CODEOWNERS | `owners` |
| `guide` | *(not a tab)* | Step 4, stamped by the model |

A step naming two tabs has its cost **split evenly**, so never widen a step to a tab that did
none of the work.

Default order, worth departing from only with a reason — **Review, Demo, API, Data,
Tests, Sequence, Structure, Code City, UX, Complexity, Logging, CODEOWNERS**. It is the
order a review actually goes: what the passes raised, then the feature as a user meets it
(the film, then the contract and the shape behind it), then what pins it — the tests, then
the traces those runs recorded — then the code's own shape, where *Structure* and *Code
City* are one question asked twice and stay adjacent, and CODEOWNERS last, because it is
the one thing no amount of reading changes. Four tabs need something said about how they are written:

- **Review** — the label is the word alone. The 🤖 it used to carry announced that
  the tab was machine-produced, which the `source` stamp on every item inside it already
  says, one item at a time. **One list** of three piles, numbered straight through: what only the
  reader can answer (`assumptions`), then the open calls, most critical first, then the
  fixes already applied, greyed out. Lists that each start at 1 make the reader do
  arithmetic. The numbering follows the order the blocks appear in here, so that order is
  an editorial choice — with one rule the build enforces: work already done is the tail.
  Whichever pile opens the list writes its lede — `2 assumed by the coder · 9 open, worst
  first · 3 auto-applied` — computed, so restate none of it. The coder's clause is the one
  that renders at zero (`0 assumed by the coder`), because that zero is a result: it says
  the authoring conversation was asked. Mode C prints `coder could not be asked` instead,
  the one case where a zero would be claiming an answer nobody was there to give. It is counts and one ordering fact,
  and nothing that describes what is directly under it: the applied fixes are visibly
  grey, an assumption visibly says *your call*, and every item carries its source beside
  its own title, so `greyed out`, `yours to confirm` and `each stamped with the pass that
  raised it` were all cut, one at a time, from the line that replaced the paragraph. Its `intro` names the passes **in the order
  they ran, and stops on the last one** — `/code-review then /simplify.` A `then` is the
  order, so *"in that order"* says it twice, and *"in separate turns"* is how the review
  was operated, which is nothing the reader can act on. The paragraph that used to stand
  above the list ("Twelve items came back. They are one list: the nine that need your
  judgement first…") described the list it was standing on.
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
  - A **deleted** row is listed and inert: this checkout has no source to quote, so it does
    not open (no disclosure arrow, no tab stop, no `role="button"`), its name is struck
    through the way the run struck it, and its location keeps the words but loses the link
    — a dead `vscode://` URL is the one thing this page never emits.
  - Once the card carries all four states, **the ledger at the foot of the tab is the same
    rows a second time**, grouped by a question the stamps already answer. Turn it off with
    **`"testLedger": false`** at the top level of the content file, and move the one fact it
    carried that no row can — how many tests the branch left alone — into the card's header
    strip (*every test this change set touched · 24 more left alone*). Said out loud rather
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
      between the row and the four sentences it was opened for. A folded part keeps its source
      bar and shows its own first line, so it says what it is without being unfolded.
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
    **word** per state (**full · partial · executed · missing · N/A**), each set at the
    ticket's own font size and wearing the **exact fill that state wears up in the prose** —
    same declaration, written once for both (`.rm-f[data-cov=x], .rm-lg[data-cov=x]`), so
    the legend cannot drift into a colour the ticket does not use. No chip beside the word:
    a 15×9 swatch and a highlighted phrase are different surfaces, the same gradient reads
    darker in the small one, and the hatch for `partial` had barely two bands to show.
    Lead the row with a muted **`Legend:`** — five framed words under a ticket are five
    things the reader has to recognise as a key before they can use it as one.
    **Spread the row edge to edge** (`justify-content:space-between`) under the frame, so
    the five words sit under the width of text they explain instead of bunching at the
    left. **A thin grey frame around all five**, so the row reads as five badges rather
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
    tip renders the same pill the list on the right does) with a count each, then what
    clicking does — `click to highlight`. Naming the kinds in prose makes the reader
    translate back into the badge they are already looking at, and nothing else on the
    page announces that a sentence is clickable.
  - With that column on the page, **do not also write an evidence-cards section** listing
    what the branch wrote — it is the same list, in fewer words, further down.
- **Data** — the DB and domain deltas, and 2–5 core-logic bullets in domain language, each
  backed by a snippet. It also carries the **conceptual model**, the one diagram on the
  page a human drew, and three rules go with it:
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
    added and not drawn red, the *still waiting for a hand-drawn layout* row when anything
    in the drawing is still red (repeated under the **New** pane, where the red is on
    screen with nothing else to explain it), and the widget opens on `New` while a layout
    is owed — the delta is a picture of automation's routing until someone draws it — and
    on `Diff` once it is not. Do not restate any of that in prose that will outlive it.
  - **Say the mechanism, not the state.** The paragraph above the picture is the place for
    what red *means* and what clicking the note in it does; a sentence saying red is
    currently there is a sentence that goes stale the moment someone acts on it.
- **UX** — the only tab whose finding is an absence, and the only one no other check in the
  repository can produce.
