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
  contract differ, the backward-compatibility check that second-guesses it, and the
  code-owners check that says whether the merge is blocked.
- `puml-diff/` — the two differs, `puml_diff.py` (class / ER / package) and
  `seq_puml_diff.py` (sequence). They diff any PlantUML diagram and know nothing about
  the project being reviewed.

Paths below are written as `${SKILL}/…`. Resolve it once, at the start of the run:

```sh
# A cascade, because there are three real layouts and only one of them is a symlink:
# a plugin install (no .claude/skills entry at all), an env override, a project symlink,
# and the gitignored clone a project fetches for CI.
for candidate in \
    "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/skills/human-review" \
    "${HUMAN_REVIEW_HOME:-/nonexistent}" \
    "$(readlink -f .claude/skills/human-review 2>/dev/null)" \
    ".claude/skills/human-review" \
    "petclinic-backend/.tools/human-review/skills/human-review"; do
  [ -x "$candidate/scripts/build-review-html.py" ] && { SKILL="$candidate"; break; }
done
[ -n "${SKILL:-}" ] || { echo "cannot locate the human-review skill"; exit 1; }
```

**Every script resolves the project under review from the directory it was invoked in,
never from where the script itself lives.** That is what lets the skill live in its own
repository and still review yours — so run them from the repository root, and never
"helpfully" rewrite a path to be relative to the skill.

**Tooltips go through the page's one component**, never a native `title`: emit
`data-tip="…"` and `TIP_JS` picks it up anywhere, including markup written later. The
assembled page is rewritten once at the end to catch what we do not own — PlantUML turns
`[[url{hint}]]` into a `title` inside the SVG we inline. `scripts/test_tooltips.py` fails
the day somebody adds a native one back.

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
| 4 | a Code City render, if you have one | `petclinic-backend/docs/generate-codecity.sh` |
| 5 | a browser suite that can be filmed | Playwright, driven by `scripts/record-feature-video.sh` |
| 6 | an entry-point complexity extractor | `mvn -q test -Dtest=EndpointComplexityExtractorTest` |
| 7 | a committed API spec, generated not hand-written | `openapi.yaml`, extracted by `OpenApiExtractorTest` |

Anything you have no answer for, drop — a tab with nothing to show is dropped and named
under the strip, which is honest. Step 10 opens the finished guide inside VS Code where a
bridge for that is installed, and falls back to a URL you ⌘-click — never a hard requirement.

What each step actually needs:

| step | hard requirement (fails without it) | degrades gracefully |
| --- | --- | --- |
| 0, 1, 2, 8, 10 | git, python3, plantuml | — |
| 9 | python3, **Pygments** (`pip install pygments`) | — |
| 7 | **PyYAML**; a JVM **or** Docker | no runtime → the tab says "not run — no JVM and no Docker" |
| 5 | ffmpeg, **Pillow**, and a TTF the captions can use | no `swiftc`/macOS → captioned but silent |
| 4, 6 | whatever your project's generators need | — |

Pygments and Pillow are top-level imports, not optional ones: without Pygments **step 9
cannot build a page at all**, even one with no snippets in it. `annotate-feature-video.py`
looks for a font in a list of macOS paths and exits if it finds none, so on Linux step 5
produces no video rather than a silent one — add a DejaVu/Liberation path before relying on it.

## Step 0 — Resolve the change set (from `$ARGUMENTS`)

- **empty** → uncommitted work: `BASE=HEAD`, diff = `git --no-pager diff HEAD`.
- **a ref / range / SHA(s)** → `BASE=<the older ref>`, diff = `git --no-pager diff $BASE...HEAD`.
- **a PR** (`#123`, `123`, a github URL) → `gh pr checkout <n>`,
  `BASE=$(gh pr view <n> --json baseRefName -q .baseRefName)`.

Default `BASE` to `origin/main`. Every script below diffs against the **merge-base**, so
commits that landed on the base after this branch started never show up as this branch's.

Print a one-line scope banner (mode, refs, `--stat`). Empty change set → "Nothing to
review." and stop.

**Then wipe `.human-review/assets/` and recreate it.** This is not tidiness. Every fragment
producer below writes to a fixed path and the renderer inlines whatever it finds there, with
no freshness check — so a step that fails silently leaves the *previous run's* artifact in
place, and the page shows a green `compatible` seal for a diff it never saw, or a Code City
of somebody else's branch. Every other tab around it is correct, which is what makes it
undetectable. It is the only failure mode here that produces a confident, wrong page.

```sh
rm -rf .human-review/assets && mkdir -p .human-review/assets
date -u +%Y-%m-%dT%H:%M:%S+00:00 > .human-review/.started
```

That second line is what lets the page report **what it cost to produce**. Without it the
cost chip falls back to the whole session — which, on a session that also built the feature,
is a much bigger number than the review, and the tooltip has to say so instead of being
useful. Write it once, here, before anything expensive runs.

Run the skill's own tests once while you are here (~2 s). They are the only thing standing
between the two differs and silent drift, and nothing else runs them:

```sh
python3 -m pytest -q "${SKILL}/scripts" "${SKILL}/puml-diff"
```

## Step 1 — Run the automated reviews, fix what is not disputable

Invoke **`/code-review`** and **`/simplify`** on the same range — in that order, one after
the other, **never in the same turn**. The two are not two opinions on the same question:
`/code-review` hunts correctness bugs, `/simplify` does not look for bugs at all and instead
shrinks the solution, applying its own cleanups to the working tree. Step 9 has to report
what each one did on its own, and two runs whose edits land interleaved can no longer be
told apart.

**Take a measurement between them and after them**, or the numbers in step 9 are a guess:

```sh
git diff --numstat | awk '{i+=$1; d+=$2} END {print i, d}'   # insertions deletions
```

Run it once after the `/code-review` fixes are in, once after `/simplify` has finished.
`/simplify`'s net effect is `(d₂−d₁) − (i₂−i₁)` — the lines it removed minus the ones it
added back. It is allowed to come out negative; a cleanup that grew the code is worth
saying so.

Then split every finding in two, and say out loud which pile each landed in:

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

Then regenerate from real traces. **There are two runs, not one** — one per suite that
produces diagrams — and naming only the first is how half the gallery goes stale:

```sh
cd petclinic-test && ./run-tests-with-tracing.sh      # the browser suites (Playwright, Cucumber)
cd petclinic-backend && mvn -o -Pgenseq test -Dgroups=genseq   # the @SpringBootTest suites
cd petclinic-test && GENSEQ_REFRESH=1 npm run trace:diagram    # render what the JVM run recorded
```

The last line is not optional and not the same as the first. A `@SpringBootTest` only
**records a trace window**; the rendering happens in `petclinic-test`, and without
`GENSEQ_REFRESH=1` the renderer re-uses its cached spans and quietly re-emits only the
diagrams it already had — so the Java ones never appear and nothing says they are missing.

⚠️ **Check afterwards that the run did not delete a diagram it then failed to rebuild.**
`run-tests-with-tracing.sh` removes every `*.genseq.puml` up front and regenerates them; a
suite that cannot start (wrong Node major, a missing browser) leaves the file deleted, and
the delta then reports, in your branch's voice, that this branch removed a diagram:

```sh
git status --porcelain -- '*.genseq.puml'      # any ` D` here is the tool's doing, not the branch's
```

`puml-diff.sh` now warns when a diagram is missing from the work tree but still present in
`HEAD`, which is exactly this case. Restore it with `git checkout --` and say in the guide
that the suite could not run.

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

…then **commit those regenerated base diagrams onto a throwaway ref and pass that ref as
`$BASE`**. This last step is not optional: `puml-diff.sh` reads the "before" side with
`git show "$MERGE_BASE:$path"`, straight from the commit object — never from the work tree.
Regenerating the base side into the working directory and then restoring the branch (as an
earlier version of these instructions said to do) throws the work away and diffs against the
committed base diagram exactly as before, at the cost of a full traced run.

```sh
git checkout -b throwaway-base $BASE && git commit -am "base diagrams, same renderer" \
  && BASE=throwaway-base
```

`npm run trace:diagram` re-renders from the cached spans in about a second, so the expensive
part is the one traced run per side, not the rendering.

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

Regenerate the city first if the branch moved (`petclinic-backend/docs/generate-codecity.sh`);
it auto-detects the branch as the change source. The script flips the **Changes** knob to
"highlight changed" so the change set is lit and the rest of the skyline recedes.

The script also **copies `codecity.html` in beside the PNG**, and that copy is what the page
must link to (`assets/codecity/codecity.html`). A relative link out to
`../petclinic-backend/…` looks right and 404s: step 10 serves `.human-review/` as the
document root, and the server collapses `..` — so the one click the image invites went
nowhere, on every run, and nothing checked.

It prints the **measured** change count on stdout. Put that number under the image rather
than typing one: "20 classes lit" in a content file is an assertion, and a city that
highlighted nothing looks exactly like a city that highlighted everything.

```sh
LIT=$(${SKILL}/scripts/capture-codecity.sh .human-review/assets/codecity.png highlight)
```

The capture now fails rather than producing an unhighlighted skyline — if the `Changes`
option it drives is ever renamed, the assignment used to be a silent no-op.

## Step 5 — Video of the feature

Record the feature actually working, with Playwright, straight into the guide:

```sh
${SKILL}/scripts/record-feature-video.sh .human-review/assets/<feature>.webm
```

**The flow being filmed belongs to your project, not to this skill.** The skill owns the
harness — launching, speaking each cue, timing the captions to the voice, spotlighting the
element a cue is about, annotating the footage. You own the twenty lines that say what to
click, in `.human-review/feature-script.js`, `human-review-feature.js` at the repo root, or
wherever `$HUMAN_REVIEW_FEATURE_SCRIPT` points:

```js
module.exports = async ({page, say, pause, get, app, apiUrl}) => {
  await page.goto(`${app}/some/screen`);
  const thing = page.locator("#the-new-thing");
  await say("This is the new part.", thing);   // spoken, captioned, spotlit on the frame
  await pause(2000);                           // a FLOOR — the narration may stretch it
  return {ok: true, note: "one line for the run summary"};
};
```

No script → step 5 is skipped with a stated reason, like any other missing hook. This split
exists because the narration and selectors of one project's feature used to live inside the
script: filming the next feature meant editing *another git repository* (or a plugin cache
that the next update silently reverts), and the selectors were generic enough to keep
resolving — so you got a polished, correctly captioned film of the **wrong feature**, under
the one heading a reviewer trusts without reading.

**The exit code carries the verdict, and 3 is not a failure:**

| code | meaning | what to do |
| --- | --- | --- |
| 0 | filmed, and the feature held | embed it |
| 2 | no feature script, or the stack is down | skip the section and say why |
| 3 | filmed, and **the feature did not hold** (`{ok:false}`) | **embed it and lead the review with what it shows** — this is the most valuable film the pipeline can make |

It drives the flow deliberately slowed, and records the whole thing. Replaying the Playwright test and keeping its retained video is
the purer idea — the test *is* the demo — but headless it finishes in about a second and
the `.webm` shows only the final assertion, which teaches a reviewer nothing. Film it to be
watched; the test still guards the behaviour. Embed with `<video controls>` (it now carries
sound); skip the section (and say so) if the stack is not up.

**The film narrates itself.** Every `say()` in the recorder is spoken by the offline macOS
speech synthesizer before it is filmed, and the captions are karaoke: short chunks, one word
arriving at a time, already-said words white and the word being said yellow. Two things fall
out of doing it that way rather than pasting a sentence into a box:

* The synthesizer reports *when it says each word*, so the highlight is locked to the voice
  instead of estimated from string length — no drift inside a sentence, which is exactly
  where a reviewer is looking.
* It fixes the pacing. A hardcoded `pause(1500)` was a guess at how long a sentence takes to
  read and could never be right for how long it takes to *say*; every pause is now a floor
  that the narration stretches when it needs to. The film gets longer and stays in sync.

`NARRATION=off` films silently and falls back to spreading the words across the cue at a
reading rate — the captions still work, they just are not voiced. `NARRATION_VOICE` (any name
from `say -v '?'`) and `NARRATION_RATE` (0..1, default 0.5) pick the voice and its speed. All
of it is local: nothing about the change is sent to a TTS service.

Both halves are plain programs — `narrate-cue.py` over `tts-cue.swift`, then
`annotate-feature-video.py` over ffmpeg — so re-cutting the film costs CPU, not tokens. The
per-cue `.wav`s stay in `<out>.narration/` beside `<out>.raw.webm`, so the annotation can be
re-run on the same footage without filming or re-speaking anything.

## Step 6 — Entry-point complexity increment

```sh
cd petclinic-backend && mvn -q test -Dtest=EndpointComplexityExtractorTest
```

⚠️ **Every fragment needs its stylesheet listed in the content file's `extraCss`.** The
renderer pulls in the snippet and code-owners stylesheets by itself, but the OpenAPI and
complexity fragments are inlined HTML and their CSS is not: forget one and the tab renders
fully populated and completely unstyled — no bars, no green/red — so the authorship
convention the lede explains in words is simply absent from the page.

```json
"extraCss": ["assets/openapi-diff.css", "assets/openapi-compat.css",
             "assets/complexity-delta.css"]
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
${SKILL}/scripts/openapi-diff.py   --base $BASE --out .human-review/assets/openapi-diff.html
${SKILL}/scripts/openapi-diff.py   --css  >  .human-review/assets/openapi-diff.css
${SKILL}/scripts/openapi-compat.py --base $BASE --out .human-review/assets/openapi-compat.html
${SKILL}/scripts/openapi-compat.py --css  >  .human-review/assets/openapi-compat.css
```

Two scripts, because they answer to different authorities.

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

### The verdict, from a tool that is not ours

Under every operation the verdict names, a `<details>` opens the **effective shape** the
operation exposes — request in one column, response in the other, `$ref`s resolved. That
resolution is the whole point: a property that moved in `components.schemas.VisitDto`
appears once in the YAML, in a place nobody is looking, and never at the four operations
that serve it. Grey is the contract as it was, red is added, red struck through is
removed — the same convention as the PlantUML deltas, learned once. Example values are
the spec's own `example:` fields, **never invented**: a made-up payload in a review is a
liability, and a generated spec already carries the real ones.

`openapi-diff.py` is *our* reading, and a reviewer is entitled to ask who checked the checker.
`openapi-compat.py` runs **[OpenAPITools/openapi-diff](https://github.com/OpenAPITools/openapi-diff)**
— the reference implementation, a Java library with its own rule set, the same one people put in
CI — over the same two revisions and puts its single machine verdict at the top of the tab:
`no_changes` · `compatible` · `incompatible`, with every incompatibility named in the currency of
the caller ("now requires `date`", "the operation is gone", "`maxLength` unset → 5").

It resolves the tool itself: `--jar`, then `$OPENAPI_DIFF_JAR`, then `~/.cache/human-review/`,
then a download from Maven Central **verified against Maven's own sha1**, then `--docker` for a
machine with no JVM. Nothing to install by hand, and nothing that runs an unverified jar.

**The cross-check is the point.** The script also asks `openapi-diff.py` for its verdict and
prints, under the seal, whether the two agree. Agreement is a footnote. A **disagreement** is the
most review-worthy line on the whole page — one classifier says a client breaks and the other says
it does not, so exactly one of them is wrong, and the guide must say which lines caused it rather
than quietly showing whichever answer is prettier. Never suppress it, and never reconcile it by
editing the prose: if we are over-strict, say so; if the reference tool found a gap in ours, say
that and treat its list as the real one.

It is **not** a gate. Plenty of contract breaks are deliberate and agreed, so the verdict is
evidence for a human, never a build failure — `--state` prints the one word if you want it in a
script, and that is as far as it goes.

⚠️ **Say what the classifier cannot see.** It reads the contract, not the handler. A `PUT`
that starts *clearing* a field it was not sent is "an optional field appeared" to any
structural differ alive — so if the automated reviews turned up a semantic break of that
shape, put it in **Look here first** and cross-link it from the contract tab — and say in
the tab that a green seal means the *shape* of the contract holds, not the behaviour behind
it. Two differs agreeing is still two structural differs agreeing. And the spec is
only evidence if it is generated: in a project where `openapi.yaml` is hand-written, this
step diffs an intention rather than an API, and the guide should say so.

## Step 8 — Who has to approve this

```sh
${SKILL}/scripts/codeowners-check.py --base $BASE --state
```

A `CODEOWNERS` file is a standing decision somebody already made: *these paths do not
move without a named human agreeing*. The host enforces it at merge time — which is far
too late to help the person doing the review, who by then has read the whole diff without
knowing that a line of it was load-bearing enough to be owned.

So ask it at review time. The script intersects the change set with the rules and answers
in one word — `approval_required` · `no_owners_touched` · `no_codeowners` — then renders
the tab: a red flag, the owners who must approve, and under each one **which files pulled
them in and which rule claimed each file**, every path a link into the editor. The rest of
the change set, matching nothing, collapses into a `<details>` so the tab is only the part
that blocks.

It is **not a gate**. It cannot approve anything and it does not guess whether the owner
already looked; it answers *will this be blocked waiting for somebody, and for whom*,
early enough to matter. Plenty of owned paths are touched deliberately and get approved
in a minute — the flag says "budget for a second reviewer", never "you did something
wrong". If an owned file was touched *incidentally* (a generated diagram, a formatter
sweep), say so in **Look here first**: the cheapest fix for a blocked merge is often to
not touch the file.

Matching is gitignore's, not `fnmatch`'s, and the semantics differ by host — flat GitHub
files are **last match wins**, GitLab `[Section]` files require *every* matching section
and treat `^[Section]` as advisory. The script implements both, picks by whether the file
has sections, and prints on the page which it applied. It also names what a host would
silently skip: a `!negation` (unsupported in CODEOWNERS — it protects nothing while
looking like it does), an owner that is not a user/team/email, and a second `CODEOWNERS`
sitting at a path the host never reads. `scripts/test_codeowners.py` pins the matcher,
because both failure modes are invisible on the page: too narrow drops the warning, too
broad cries wolf until the tab is ignored.

Wire it into the content file as its own tab, right after **Review** — the answer to
"can this merge?" belongs next to the answer to "should it?":

```json
{"id":"owners","label":"Code owners","blocks":[{"type":"codeowners","base":"origin/main"}]}
```

The renderer runs the check itself rather than including a fragment somebody remembered
to regenerate, pulls in the stylesheet on its own, and hangs the red **approval required**
badge on the tab when the state says so — a stale "no owner touched this" is worse than
no tab at all. No `CODEOWNERS` in the repository drops the tab entirely; nothing owned
being touched keeps it and strikes the label through, which is the honest answer to a
question worth asking.

## Step 9 — Snippets, findings, and the page

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

### The scope bar — one chip per fact, and a chip per review

`"scope"` is a list of `{label, value, href?}` chips across the top of the page: the
change set in numbers, before any argument about it. `value` is **raw HTML** on purpose
(`<span class="added">+2256</span> / <span class="removed">−34</span>`), and `href` makes
the chip a link — an external one opens in a new tab.

```json
"scope": [
  {"label":"commits","value":"2 (pushed to main)","href":"https://github.com/…/compare/…"},
  {"label":"files","value":"25 (16 changed, 9 new)"},
  {"label":"lines","value":"<span class=\"added\">+2256</span> / <span class=\"removed\">−34</span>"},
  {"label":"unit tests","value":"125 green (20 new)"},
  {"label":"diagrams","value":"3","href":"#diagrams"},
  {"label":"/code-review","value":"6 findings",
   "href":"https://code.claude.com/docs/en/code-review#review-a-diff-locally"},
  {"label":"/simplify","value":"<span class=\"removed\">−118 lines</span> · 2 findings",
   "href":"https://code.claude.com/docs/en/commands#all-commands"}
]
```

### The chip that computes itself

```json
"scope": [ …, {"auto": "cost"} ]
```

That is the whole declaration: no label, no value. The renderer runs
`scripts/review-cost.py` at build time and fills in **what this review run consumed** —
list-price dollars and a token count, with the breakdown on hover. It is the one number on
the page that is still changing while the page is being written, so it is the one number
that must never be typed into the content file.

What it counts, and why each part was easy to get wrong:

* **Subagents.** They are not in the session transcript — a subagent's turns go to
  `/tmp/claude-<uid>/…/tasks/<agentId>.output`, and nothing in the environment names that
  directory. The script reads the agent ids out of the parent transcript instead, so the
  link is exact rather than a filesystem guess that would sweep up a concurrent session's
  agents. On the reference run this was **300 of 602 turns** — a chip without them would
  have been wrong by most of the work.
* **Deduped by `message.id`**, because a streamed message is written more than once with
  its usage repeated, and summing rows roughly doubles the bill.
* **Cache reads priced at a tenth of input.** They dominate the *token* count on any long
  run (108M of 111M on the reference run) while contributing almost nothing to the *cost*,
  which is exactly why the chip leads with dollars and keeps tokens as the smaller half.

The figure is **list-price equivalent** — what these tokens would cost on the API. A
subscription is not billed it, and the tooltip says so rather than letting the page imply
somebody paid $78 for a code review. Drop the chip and nothing else changes; the renderer
also drops it by itself, rather than printing a wrong number, when there is no session id
in the environment or no transcript to read.

⚠️ **The two automated reviews get one chip each — never a single merged "reviews run".**
They answer different questions, so one number over both says neither. Each chip is
labelled with the command that produced it and links to that command's own page in the
Claude Code documentation, so a reviewer who has never run either can find out in one
click what the number is a number *of*:

| chip | value | measured in step 1 |
| --- | --- | --- |
| `/code-review` | `N findings` — **everything it found**, the disputable ones and the ones you fixed yourself. A chip that counted only the leftovers would shrink as you did more work. | count the findings it reported |
| `/simplify` | `−N lines · M findings` — how much code it removed, *then* how many cleanups it raised. Its job is to shrink the solution, so the line count is the headline and goes first. | the `(d₂−d₁) − (i₂−i₁)` arithmetic above |

Wrap the line count in `<span class="removed">…</span>` when it is a removal and
`<span class="added">…</span>` on the rare run that grew the code, so the sign is legible
at chip size. A review that found nothing still gets its chip, with `0 findings` — the
absence of findings is a result, and a missing chip reads as "we never ran it". If one
genuinely did not run, say so in its own chip (`not run — <reason>`) rather than dropping it.

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
  {"id":"owners","label":"Code owners","blocks":[{"type":"codeowners"}]},
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
branch did *not* change, rendered from source as context), **`codeowners`** (step 8's
check, run by the renderer), **`codecity`**, **`section`** (one entry of `sections` by
`id`), **`html`**. Rules the renderer enforces:

- an **Overview** tab is synthesised as the first tab, holding the summary and the
  verdict, and the page opens on it. They used to sit above the strip, which pushed the
  questions below the fold on a laptop: a reviewer scrolled past the answers to find out
  what the answers were. Declare a tab with `id: "overview"` yourself to take it over;
- a tab whose every block came back empty is **dropped**, and named in a line under the
  strip — never shown as an empty page;
- a tab that has content but **no delta** — all context, nothing this branch touched — is
  kept and its label is **struck through**, with a tooltip saying so. "We looked, and this
  branch did not touch it" is worth as much to a reviewer as the opposite, and a dropped
  tab cannot say it. `puml` and `codecity` blocks carry content but never a delta (a
  picture of the current state is not a change); a `section` counts as a delta unless it
  declares `"unchanged": true`. `noStrike: true` on a tab opts out;
- a changed diagram that no tab claimed prints a **warning** at build time. A gallery that
  silently loses a diagram is the exact failure this pipeline exists to prevent;
- `count: true` puts the item count on the tab, `badge: "…"` puts a literal there, and
  `badgeClass: "alarm"` makes it red — a badge that says something is *wrong* must not
  look like a count, which is why the code-owners block sets both itself rather than
  leaving the severity to whoever wrote the content file. Use a number
  only where it means something — on the tab holding the findings it does; on
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
2. **Code owners** — whether a named human has to approve this before it can merge, and
   which files put them on the critical path. It sits second because it is the only
   answer on the page that is about the *merge* rather than the code, and a reviewer who
   learns at the end that they are not the last signature has read the diff in the wrong
   frame of mind.
3. **Behaviour** — the sequence deltas, the acceptance tests that produced them, the video,
   and a plain statement of what is **not** covered.
4. **API contract** — step 7's two fragments, each a `section` with `includeHtml`: the
   compatibility verdict first, because it is the one-word answer, then the classified
   change list underneath it.
5. **Data model** — the DB and domain deltas, and the 2–5 core-logic bullets in domain
   language, each backed by a snippet.
6. **Packages** — the package delta, or the current package diagram as context.
7. **Cost & shape** — the complexity increment and the Code City shot.

## Step 10 — Hand the guide over, then the app

Serve the guide and open it. **Never `open .human-review/review.html`**: that hands the page
to whatever the OS thinks owns `.html`, which is another application on another desktop,
while the terminal that just built it is sitting inside an editor.

```sh
URL=$(${SKILL}/scripts/serve-review.py .human-review)     # http://127.0.0.1:7654/review.html
```

A loopback static server on a fixed port, detached, idle-reaped after four hours, and a
second run of the skill reuses the first one rather than leaving a listener behind. It is
what makes the guide *addressable* — the report has to have a URL before anything can show
it, and both of VS Code's embedded browsers refuse `file://` (the Simple Browser's iframe
is bound by a `frame-src *` CSP, and a CSP wildcard does not cover non-network schemes, so
a file URL renders as a blank panel with no error).

Then open it where the reader already is:

- **`$TERM_PROGRAM = vscode`** → in that VS Code window, beside the code. From a terminal
  you cannot aim at the window you are running in — nothing in the environment identifies
  it — so this needs a bridge inside the editor. Where `open-in-browser.py` from
  [victor-vsc](https://github.com/victorrentea/victor-vsc) is available, run
  `open-in-browser.py "$URL"`: it asks every window's extension host who it is, picks the
  one whose workspace folder is this git root, and opens the page in its Simple Browser
  beside the editor. Note it matches by **folder, not focus** — while an agent works, the
  window Victor is watching is usually a different one.
- **anything else, or no bridge** → print the URL and say to ⌘-click it. Any VS Code since
  1.134 offers its own Browser View for a localhost link clicked in the terminal
  (`workbench.browser.openLocalhostLinks`), so the fallback is still an embedded page and
  not a trip to Chrome.

Either way **print `$URL` as the last line of the run**, because the reader closes the panel
and wants it back an hour later. It stays valid until the server idles out; re-running
`serve-review.py` revives it at the same address.

Then make sure the app is actually running from **this** checkout and open the screen the
change affects, so the human can exercise it. Seed extra rows if the sample data is too thin
to show the feature off. Finally start `/relay` so they can dictate UX tweaks straight into
the session.

## Wrap-up

`.human-review/` is a throwaway artifact — remind the human to delete it rather than commit it
(`serve-review.py --stop` first, or the server holds the folder open until it idles out).
Print the path and the URL, and list what you fixed vs what you left for them. Do not commit
or push.
