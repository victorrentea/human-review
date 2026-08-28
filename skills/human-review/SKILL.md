---
name: human-review
description: Build a human-facing review.html for a change set (uncommitted, given commits, or a PR) — runs /code-review and /simplify and applies the non-disputable fixes, then assembles red-annotated PlantUML deltas, a Code City screenshot, a Playwright video of the feature, the endpoint-complexity increment, and verbatim code snippets deep-linked into VS Code. Explicit invocation only — user types /human-review.
disable-model-invocation: true
---

# /human-review — assemble a review.html for a human

Produce **one page, `.human-review/review.html`** — tabbed, one question per tab — that lets
a human review a change set fast: what to look at first, diagram deltas, what the REST
contract did, where it landed in the city, a video of the feature working, what it cost in
endpoint complexity, and the tests that pin it — every code reference a clickable VS Code
deep-link, every snippet cut from the working tree at build time.

You are the assembler *and* the janitor: you run the automated reviews and apply the
fixes nobody would argue with, then hand the human only the calls that are genuinely
theirs. Do **not** commit or push.

## Where the tooling lives

Everything this skill drives sits **next to this file**, so the skill is one self-contained
thing you can read, copy, or install into any project without hunting through it for the
skill's moving parts:

- `scripts/` — the mechanics: the page builder, the snippet extractor, the diagram
  driver, the video recorder, the Code City capture, the complexity delta, the OpenAPI
  contract differ.
- `puml-diff/` — the two differs, `puml_diff.py` (class / ER / package) and
  `seq_puml_diff.py` (sequence). They diff any PlantUML diagram and know nothing about
  the project being reviewed.

Paths below are written as `${SKILL}/…`. Resolve it once, at the start of the run:

```sh
SKILL="$(dirname "$(readlink -f .claude/skills/human-review 2>/dev/null || echo .claude/skills/human-review)")/human-review"
# installed as a plugin instead? SKILL="${CLAUDE_PLUGIN_ROOT}/skills/human-review"
```

**Every script resolves the project under review from the directory it was invoked in,
never from where the script itself lives.** That is what lets the skill live in its own
repository and still review yours — so run them from the repository root, and never
"helpfully" rewrite a path to be relative to the skill.

One piece deliberately stays in the host project: a CI-facing diagram tool such as
petclinic's `scripts/architecture-diff.sh`, which answers a narrower question and predates
this skill. Point it at this skill's differs rather than keeping a second copy — a private
fork of the review pipeline drifts in silence, which is the exact failure the guardrail
tests exist to catch.

## Adapting it to your project

The steps below name the reference project's commands, because a skill that says "run your
test suite" tells an agent nothing it can act on. Five hooks are yours to substitute; the
rest of the skill is project-agnostic:

| step | what it needs from you | reference project's answer |
| --- | --- | --- |
| 3 | a test run that records traces | `cd petclinic-test && ./run-tests-with-tracing.sh` |
| 4 | a Code City render, if you have one | `petclinic-backend/generate-codecity.sh` |
| 5 | a browser suite that can be filmed | Playwright, driven by `scripts/record-feature-video.sh` |
| 6 | an entry-point complexity extractor | `mvn -q test -Dtest=EndpointComplexityExtractorTest` |
| 7 | a committed API spec, generated not hand-written | `openapi.yaml`, extracted by `OpenApiExtractorTest` |

Anything you have no answer for, drop — a tab with nothing to show is dropped and named
under the strip, which is honest. Steps 0, 1, 2, 8 and 9 need nothing but git, python3 and
plantuml; step 7 additionally needs PyYAML.

## Step 0 — Resolve the change set (from `$ARGUMENTS`)

- **empty** → uncommitted work: `BASE=HEAD`, diff = `git --no-pager diff HEAD`.
- **a ref / range / SHA(s)** → `BASE=<the older ref>`, diff = `git --no-pager diff $BASE...HEAD`.
- **a PR** (`#123`, `123`, a github URL) → `gh pr checkout <n>`,
  `BASE=$(gh pr view <n> --json baseRefName -q .baseRefName)`.

Default `BASE` to `origin/main`. Every script below diffs against the **merge-base**, so
commits that landed on the base after this branch started never show up as this branch's.

Print a one-line scope banner (mode, refs, `--stat`). Empty change set → "Nothing to
review." and stop. Create `.human-review/assets/`.

## Step 1 — Run the automated reviews, fix what is not disputable

Invoke **`/code-review`** and **`/simplify`** on the same range. Then split every finding
in two, and say out loud which pile each landed in:

- **Non-disputable → fix it now.** One obvious right answer, no behaviour change, no
  product call: a duplicated helper, a test that passes vacuously, a shared persistence
  context hiding a missing `save()`, an uninitialised model field, a positional
  selector, a dead import. Fix, then re-run the affected tests.
- **Disputable → hand it to the human.** Anything that changes an API contract, a
  migration already applied somewhere, a data-integrity trade-off, a
  performance/correctness tension, or where two reasonable engineers would pick
  differently. These become the guide's **Look here first** list, most critical first.

Never argue a finding away silently. If you skip one, it goes in the list with a
one-line reason.

### Commit the work you found; leave your own fixes uncommitted

Before touching anything, **commit whatever is already in the working tree** — the change
set under review, and any in-flight work — so the branch has a clean baseline and the
reviewer can see the "before" as history. Then make your fixes and **leave them
uncommitted**. Their whole value is that `git diff` shows, at a glance, exactly what an
agent touched and nothing else. Committing them buries that in a log entry nobody diffs.

If a file has to be committed for a later step to work (a merge refuses to run over an
uncommitted change to a file it rewrites), commit it and say which one, and why.

### Do not comment your own decisions into the code

The reasoning for what you changed belongs in the guide, not in the source. Never leave
a comment whose job is to justify an edit to the reviewer — "tagged because…", "extracted
here so both callers…", "added this flag so the review can…". The reviewer is reading a
diff; they can see what you did, and if the *why* matters it goes in **Look here first**.
A comment earns its place only when it explains something a future reader could not
recover from the code itself — a trap, a non-obvious constraint, an order that matters.
When in doubt, leave the code bare.

## Step 2 — Diagram deltas (red = added, red + struck = removed)

```sh
${SKILL}/scripts/puml-diff.sh $BASE .human-review/assets/diagrams
```

Diffs **every** `.puml` that differs from the merge-base — committed, modified or
untracked — and dispatches by diagram family:

| family | differ | semantics |
| --- | --- | --- |
| class / ER / package | `${SKILL}/puml-diff/puml_diff.py` | set of elements + relations |
| sequence | `${SKILL}/puml-diff/seq_puml_diff.py` | ordered script of messages |

Writes `<name>.diff.puml`, `<name>.diff.svg` and `MANIFEST.tsv` (name / source / kind /
status / files). Zero changed diagrams is a quiet success — drop the section.

A structural delta is **titled as one**: `title Domain Model` comes out
`Domain Model - <color:red>Diff</color>`, so the picture says what it is to anyone who
meets it outside this page — opened straight from `assets/diagrams/`, pasted into a
ticket, or reached by a link. The red says *what* changed; the title has to say that the
whole picture is a change.

Never re-implement the diffing inline, and never hand-diff the `.puml` text: a second
fork of the review pipeline drifts silently. `scripts/architecture-diff.sh` stays the
CI-facing tool for the three structural diagrams; `puml-diff-vs-git.sh` stays the
single-diagram primitive.

## Step 3 — Sequence diagrams for boundary-crossing changes

If the change crosses a **system boundary** — a new/changed API call, a new DB column
or query, a new outbound integration — then **every such interaction must be covered by
at least one `@generate_sequence`-tagged `.feature` scenario**. Check
`petclinic-test/features/*.feature`; if the interaction has no tagged scenario, add the
tag (or the scenario) — **the tag alone, with no comment explaining why you added it**.

Then regenerate from real traces:

```sh
cd petclinic-test && ./run-tests-with-tracing.sh
```

It refuses to run unless the whole stack is up **and started in the right order**
(`start-database.sh` → `start-grafana.sh` → `start-backend.sh` → `start-frontend.sh`;
the backend only attaches the OTel agent if `:4318` was already listening).

⚠️ **Verify the running stack is *this* checkout.** `~/workspace` holds several
petclinic clones, and a backend from a different one answers on `:8080` just fine while
serving the old behaviour — the diagram would then be a picture of code you are not
reviewing. Check `ps` for the process's path, and `curl` one endpoint for a field the
change introduces, before trusting the trace.

Re-run `${SKILL}/scripts/puml-diff.sh` afterwards so the regenerated diagrams land in the manifest.
A diagram that did not exist before is shown **plain**, not red — reddening every line of
something new says "all of this changed" when what happened is "this is new".

**The base diagram must come from the same renderer as the new one.** A sequence diagram
is a rendering choice as much as a recording: change what an arrow is labelled with, and
every arrow in the committed base diagram reads as a deletion with its replacement added
underneath — a wall of red that says nothing about the change under review. Whenever the
generator in `petclinic-test/src/genseq/` has moved since the base ref, regenerate the
base side too:

```sh
git stash push --include-untracked          # your fixes are uncommitted; keep them
git checkout $BASE -- petclinic-test/src/genseq petclinic-backend/src/main/resources/application.properties
cd petclinic-test && GENSEQ_REFRESH=1 ./run-tests-with-tracing.sh   # base traces, base renderer
```

…then restore the branch's generator and re-render, so both sides differ only in what
the *code* does. `npm run trace:diagram` re-renders from the cached spans in about a
second, so the expensive part is the one traced run per side, not the rendering.

If you skip it, say so in the guide next to the diagram: a red arrow the reviewer cannot
distinguish from a real change is worse than no diagram.

⚠️ The DB arrows are labelled from Hibernate's own comment on each statement
(`hibernate.use_sql_comments`, in the backend's `application.properties`). A backend
started before that property existed emits statements without it, and every DB arrow
falls back to its span name — `SELECT petclinic`, over and over. If that is what you see,
the running backend predates the property: restart it and re-record.

**Show what generated each diagram.** A sequence diagram is evidence only if the reviewer
can get to the test that produced it: give every diagram in the guide a link to the
scenario source (`file:from-to`, so VS Code opens it at the test) and a link to the `.puml`
itself. A picture with no provenance is a picture they have to trust.

## Step 4 — Code City screenshot

```sh
${SKILL}/scripts/capture-codecity.sh .human-review/assets/codecity.png highlight
```

Regenerate the city first if the branch moved (`petclinic-backend/generate-codecity.sh`);
it auto-detects the branch as the change source. The script flips the **Changes** knob to
"highlight changed" so the change set is lit and the rest of the skyline recedes.

Embed the PNG **wrapped in a link to `codecity.html`** so a click opens the live 3D view.

## Step 5 — Video of the feature

Record the feature actually working, with Playwright, straight into the guide:

```sh
${SKILL}/scripts/record-feature-video.sh .human-review/assets/<feature>.webm
```

It drives the flow through the same selectors the e2e suite uses, deliberately slowed, and
records the whole thing. Replaying the Playwright test and keeping its retained video is
the purer idea — the test *is* the demo — but headless it finishes in about a second and
the `.webm` shows only the final assertion, which teaches a reviewer nothing. Film it to be
watched; the test still guards the behaviour. Embed with `<video controls>`; skip the
section (and say so) if the stack is not up.

## Step 6 — Entry-point complexity increment

```sh
cd petclinic-backend && mvn -q test -Dtest=EndpointComplexityExtractorTest
```

Regenerates `docs/generated/endpoint-complexity.{html,json}` — the cyclomatic complexity of
the *whole flow* behind each entry point, read from bytecode. An entry point is not only a
`@RestController` handler: `@McpTool`, `@Scheduled` and the `@KafkaListener`/`@RabbitListener`/
`@JmsListener` family count too, each tagged with its `kind`.

Get the baseline by running the same test at the merge-base (or by reading the committed JSON
from that ref), then render with `${SKILL}/scripts/endpoint-complexity-delta.py before.json after.json`,
which groups by kind (HTTP → MCP → listeners → jobs) and colours **green for an increase, red
for a decrease** — colour reads as authorship (what the branch added/removed), not as judgement.
Report `before → after (Δ)` **ranked inside the full list**, so the reviewer sees whether the
change made a cheap entry point expensive or merely nudged an already-heavy one. If the baseline
predates a widening of what counts as an entry point, say so — the newly-visible kinds will
otherwise read as "added by this branch".

## Step 7 — What the REST contract did

```sh
${SKILL}/scripts/openapi-diff.py --base $BASE --out .human-review/assets/openapi-diff.html
${SKILL}/scripts/openapi-diff.py --css  >  .human-review/assets/openapi-diff.css
```

`git diff openapi.yaml` answers the question in the wrong currency: it says line 1937 gained
six lines, when what the reviewer needs is "`VisitDto` grew three read-only fields, and none
of it breaks a caller". So the differ parses both revisions **as structures** — every
operation added/removed/changed, every schema down to the property, its type and its
constraints — and classifies each individual difference by what it does to somebody already
calling the API: **breaking** (a removal, a tightened constraint, a newly-required request
field, a changed type or `$ref`), **additive**, **changed** (`readOnly`, `default`,
`deprecated`, `operationId` — real, but a judgement call), **cosmetic** (docs only). The raw
unified diff rides along in a `<details>`, because a classifier is a summary and a reviewer is
entitled to the source.

Point `--spec` at your spec if it is not `openapi.yaml` at the root; the "before" side comes
from the **merge-base**, and a spec that did not exist there is an empty one, not a crash.
Every subject deep-links into the spec at the line that defines it.

⚠️ **Say what the classifier cannot see.** It reads the contract, not the handler. A `PUT`
that starts *clearing* a field it was not sent is "an optional field appeared" to any
structural differ alive — so if the automated reviews turned up a semantic break of that
shape, put it in **Look here first** and cross-link it from the contract tab. And the spec is
only evidence if it is generated: in a project where `openapi.yaml` is hand-written, this
step diffs an intention rather than an API, and the guide should say so.

## Step 8 — Snippets, findings, and the page

Never retype code into the guide. Reference it:

```sh
${SKILL}/scripts/extract-snippet.py petclinic-backend/.../Visit.java:33-38 --caption "…"
```

It cuts the lines verbatim at build time, numbers them from the real line number, and
titles them `path:from-to` as a `vscode://file/<abs-path>:<line>:1` link.

Author the judgement into a JSON content file and render:

```sh
${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
```

The JSON holds only prose + `path:from-to` references + per-diagram notes; the renderer
owns the shell, the CSS, the inlined SVGs and the snippet extraction.

### Lay it out as tabs, not as a scroll

A review is not one argument read top to bottom. It is five or six separate questions — *what
do you want from me? does it work? does the contract still hold? what shape did it land in?
what did it cost?* — and a reviewer answers them in whatever order their doubt takes them. A
single column forces them past four answers to reach the one they wanted, so the page is a
**tab strip over panels**, driven by a `tabs` array in the content file:

```json
"tabs": [
  {"id":"review","label":"Review","count":true,
   "blocks":[{"type":"findings","title":"Look here first","body":"…"},
             {"type":"autofixes","title":"Already fixed for you","body":"…"}]},
  {"id":"behaviour","label":"Behaviour",
   "blocks":[{"type":"diagrams","kind":"sequence","title":"Sequence deltas","body":"…"},
             {"type":"section","id":"tests"},{"type":"section","id":"video"}]},
  {"id":"api","label":"API contract","badge":"+4","blocks":[{"type":"section","id":"api"}]},
  {"id":"data","label":"Data model",
   "blocks":[{"type":"diagrams","only":["DB","DomainModel"]},{"type":"section","id":"logic"}]},
  {"id":"packages","label":"Packages",
   "blocks":[{"type":"diagrams","only":["Packages"]},
             {"type":"puml","src":"petclinic-backend/docs/packages.puml","name":"Packages",
              "status":"unchanged","note":"…"}]},
  {"id":"shape","label":"Cost & shape",
   "blocks":[{"type":"section","id":"complexity"},{"type":"codecity"}]}
]
```

Block types: **`findings`** (the disputable calls), **`autofixes`** (the top-level
`autofixes` array — what you applied in step 1, same shape as a finding), **`diagrams`**
(the delta gallery, narrowed by `kind` / `only` / `except`), **`puml`** (a diagram this
branch did *not* change, rendered from source as context), **`codecity`**, **`section`**
(one entry of `sections` by `id`), **`html`**. Rules the renderer enforces:

- a tab whose every block came back empty is **dropped**, and named in a line under the
  strip — never shown as an empty page;
- a changed diagram that no tab claimed prints a **warning** at build time. A gallery that
  silently loses a diagram is the exact failure this pipeline exists to prevent;
- `count: true` puts the item count on the tab, `badge: "…"` puts a literal there. Use one
  only where the number means something — on the tab holding the findings it does; on
  "Data model" it would just count pictures;
- omit `tabs` entirely and you get the original single-column page, unchanged.

Deep links work both ways: `#<tab-id>` opens on that tab, and a link to any `id` inside a
panel switches to it first. `⌘F` searches only the open tab, so the strip carries a **show
all** toggle that reveals every panel at once — which is also what printing does.

What goes where, as a default worth departing from only with a reason:

1. **Review** — the disputable findings, most critical first, each with the failing scenario
   in one sentence and a snippet of the decisive lines; then the fixes you already applied.
   The two lists are one decision split in two, and a reviewer who cannot see the first pile
   has to take the size of the second on trust.
2. **Behaviour** — the sequence deltas, the acceptance tests that produced them, the video,
   and a plain statement of what is **not** covered.
3. **API contract** — step 7's fragment, as a `section` with `includeHtml`.
4. **Data model** — the DB and domain deltas, and the 2–5 core-logic bullets in domain
   language, each backed by a snippet.
5. **Packages** — the package delta, or the current package diagram as context.
6. **Cost & shape** — the complexity increment and the Code City shot.

## Step 9 — Hand the app back

Open the finished guide (`open .human-review/review.html`). Then make sure the app is actually
running from **this** checkout and open the screen the change affects, so the human can
exercise it. Seed extra rows if the sample data is too thin to show the feature off.
Finally start `/relay` so they can dictate UX tweaks straight into the session.

## Wrap-up

`.human-review/` is a throwaway artifact — remind the human to delete it rather than commit it.
Print the path, and list what you fixed vs what you left for them. Do not commit or push.
