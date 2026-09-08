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
being attributed to a guess.

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
| `{{tabcount}}` | the number of tabs actually emitted (Overview lede) |

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
empty it is — asked and had nothing, read back and found nothing, or nobody left to ask) ·
**`autofixes`** (the top-level `autofixes` array, same shape as a finding) · **`diagrams`** (the delta gallery, narrowed by `kind` / `only` /
`except`) · **`testpairs`** (Step 3) · **`tests`** (the ledger: every test the change set
moved, grouped by what happened to it) · **`logging`** (Step 7c) · **`puml`** (a diagram this
branch did not change, rendered from source as context) · **`codeowners`** (Step 8, run by
the renderer) · **`codecity`** · **`section`** (one entry of `sections` by `id`) · **`html`**.

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

- An **Overview** tab is synthesised first and the page opens on it; declare `id: "overview"`
  yourself to take it over.
- A tab whose every block came back empty is **dropped** and named in the build log.
- A tab with content but **no delta** is kept and its label **struck through**, with a
  tooltip saying so. `noStrike: true` opts out. `puml`/`codecity` blocks never carry a delta;
  a `section` counts as one unless it declares `"unchanged": true`.
- A changed diagram no tab claimed prints a **warning**.
- `count: true` puts the item count on the tab, `badge: "…"` a literal, `badgeClass: "alarm"`
  makes it a red `!` (the phrase moves to `aria-label` and `data-tip`); `badgeLabel` sets it.
- The Overview lede is checked against the strip: `{{tabcount}}` is filled in, and the build
  warns when the lede fails to name a tab or names them out of order.
- Deep links work both ways (`#<tab-id>`, or any `id` inside a panel). The scroll offset is
  derived from the strip's own height, never typed — the strip wraps to two rows at every
  width, which is its normal state.
- Omit `tabs` entirely and you get the original single-column page.

## The tab strip

A review is five or six separate questions, answered in whatever order the reader's doubt
takes them, so the page is a **tab strip over panels**, driven by a `tabs` array:

```json
"tabs": [
  {"id":"review","label":"🤖 Review","count":true,
   "intro":"<p class=\"sub\">Both /code-review and /simplify ran, and their output was merged before it reached this page…</p>",
   "blocks":[{"type":"findings","title":"Look here first","body":"…"},
             {"type":"autofixes","title":"Already fixed for you","body":"…"}]},
  {"id":"behaviour","label":"Demo","blocks":[{"type":"section","id":"video"}]},
  {"id":"sequence","label":"Sequence",
   "blocks":[{"type":"section","id":"sequences-note"},
             {"type":"testpairs","id":"sequences","kind":"sequence",
              "title":"Each test, beside the sequence its own run recorded",
              "snippets":[{"ref":"petclinic-test/features/add-visit.feature:12-27","caption":"…"}],
              "unpaired":{"id":"tests-nosequence",
                          "title":"Tagged for tracing, and no diagram came back","body":"…"}}]},
  {"id":"requirements","label":"Tests",
   "blocks":[{"type":"section","id":"requirements"},{"type":"tests"}]},
  {"id":"data","label":"Data",
   "blocks":[{"type":"section","id":"conceptual"},{"type":"diagrams","only":["DomainModel","DB"]}]},
  {"id":"packages","label":"Structure",
   "blocks":[{"type":"section","id":"packages-note"},
             {"type":"diagrams","only":["Packages"],
              "context":{"src":"petclinic-backend/docs/packages.puml","name":"Packages","note":"…"}}]},
  {"id":"api","label":"API","badge":"+4","blocks":[{"type":"section","id":"swaggerdiff"}]},
  {"id":"city","label":"Code City","blocks":[{"type":"codecity"}]},
  {"id":"complexity","label":"Complexity","blocks":[{"type":"section","id":"complexity-delta"}]},
  {"id":"logging","label":"Logging","tip":"Every logging statement the change set added — found by syntax, not by grep.",
   "blocks":[{"type":"logging","base":"origin/main","paths":["petclinic-backend"],
              "id":"logging-added","title":"Logging this change set added","body":"<div class=\"lede\">…</div>",
              "existing":{"id":"logging-existing","title":"…","body":"…","snippets":[]},
              "console":{"id":"logging-console","title":"…","body":"…","snippets":[]}}]},
  {"id":"dsaudit","label":"UX","tip":"Native controls sitting where a standardised component belongs — found by absence, not by labelling.",
   "blocks":[{"type":"section","id":"ds-audit"}]},
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
| `review` | 🤖 Review | the harvested passes (Step 1) |
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

Default order, worth departing from only with a reason — **Overview, 🤖 Review, Demo,
Sequence, Tests, Data, Structure, API, Code City, Complexity, Logging, UX,
CODEOWNERS**. Four tabs need something said about how they are written:

- **🤖 Review** — **one list** of three piles, numbered straight through: what only the
  reader can answer (`assumptions`), then the open calls, most critical first, then the
  fixes already applied, greyed out. Lists that each start at 1 make the reader do
  arithmetic. The numbering follows the order the blocks appear in here, so that order is
  an editorial choice — with one rule the build enforces: work already done is the tail.
  The open pile writes its own lede — `9 open, worst first · 3 already applied, greyed out
  · each stamped with the pass that raised it` — computed, so restate none of it. Its
  `intro` names which passes ran and in which order, in a line, and stops there. The
  paragraph that used to stand above the list ("Twelve items came back. They are one list:
  the nine that need your judgement first…") described the list it was standing on.
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
  - A **gap** is a criticism of the *requirement*, so it belongs on the requirement's side:
    render it under the ticket as a **blind spot**, never under the code on the right where
    it reads as a verdict on the test that happens to be open.
  - **Colour legend**: one row under the ticket's frame — swatch, name, nothing else, the
    sentence on hover. Name the states, not their colours (**Fully covered · Partial
    coverage · Executed, not asserted · Missing · Not a claim**); the swatch is the colour.
    The kinds get their own one-liner under the tests card.
  - **Say what a model inferred.** A heading over inferred content carries a 🤖 superscript
    reading *as inferred by AI*; the logging tab's verdicts carry the same mark reading
    *LLM evaluated* — and the one verdict meaning "the model was never reached" carries
    none. A hover explanation on a sentence is its badge counts (`UI ×2 · API ×3`) and
    nothing more: prose in a tooltip is prose nobody can quote back.
  - With that column on the page, **do not also write an evidence-cards section** listing
    what the branch wrote — it is the same list, in fewer words, further down.
- **Data** — the DB and domain deltas, and 2–5 core-logic bullets in domain language, each
  backed by a snippet.
- **UX** — the only tab whose finding is an absence, and the only one no other check in the
  repository can produce.
