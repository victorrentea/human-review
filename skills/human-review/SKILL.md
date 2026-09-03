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
  contract differ, the backward-compatibility check that second-guesses it, the
  structural logging extractor, and the code-owners check that says whether the merge is
  blocked. `scripts/ast-grep-rules/` is the logging extractor's eleven rules as standalone
  YAML (it carries its own copies as strings so it stays dependency-free; a test keeps the
  two in step), and `scripts/testdata/` holds the Java fixtures its tests scan.
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
| 7 | **PyYAML**; a JVM **or** Docker | no `oasdiff` → the seal reads `PARTIAL LIST`; no runtime at all → the tab says "not run — no JVM and no Docker" |
| 7b | `openapi-changes` (Homebrew) | not installed → no Spec changes tab, named under the strip |
| 5 | ffmpeg, **Pillow**, and a TTF the captions can use | no `swiftc`/macOS → captioned but silent; no recording at all → notice + transcript, never a dead player |
| 4, 6 | whatever your project's generators need | — |
| 7c | `ast-grep` (Homebrew, or `pip install ast-grep-cli`) | not installed → no Logging tab, named under the strip |

Three of these are **optional Homebrew binaries**, not Python dependencies, so assume none
of them and check before you use them:

| binary | install | what it buys | without it |
| --- | --- | --- | --- |
| `oasdiff` | `brew install oasdiff` (or `go install github.com/oasdiff/oasdiff@latest`); `OASDIFF_BIN` overrides the path, `--no-oasdiff` bypasses it | the affected-operation list with `$ref`s **resolved** — 11 operations on the reference change set where the `$ref`-blind path finds 4 | the Java tool takes over as the fallback and the page says so in the open: the seal reads `COMPATIBLE · PARTIAL LIST` in amber, with a band under it stating the list is a **lower bound**. That amber is correct, not a bug |
| `openapi-changes` | `brew install pb33f/taps/openapi-changes` | pb33f's whole report, embedded as a document — its own change list, side-by-side diff and document tree | the Spec changes tab is dropped |
| `ast-grep` | `brew install ast-grep` | the logging extractor's structural pass — the only way to tell `log.info(x)` from `Math.log(x)` | the Logging tab is dropped, loudly: a false "no logging found" is the one answer that tab must never give |

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

From here on, every step that produces a tab's content brackets its own commands with
`steps-ledger.py start`/`end`, naming the tab(s) it feeds — Step 9 ("What each tab cost")
explains the mechanics and what a crashed step does to the numbers; the short version is:

```sh
${SKILL}/scripts/steps-ledger.py start <tab[,tab2]> --label "…" > .human-review/.step-<name>
# … the step's commands, and everything you do to work through them …
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-<name>)"
```

**The index goes through a file, never a shell variable.** `STEP=$(steps-ledger.py start …)`
reads naturally, but a step's real span is rarely one shell call — Step 1's is two whole
skill invocations plus the fixes between them, Step 3's is a traced test run with a
conditional branch, and even a mechanical step can turn into three or four tool calls the
moment something needs a second look. Shell state does not survive between them, only the
working tree does, so `end` reads the index the same way `start` wrote it: from a file, not
from a variable it can no longer see. A `$STEP` that silently came back empty would not
error loudly — `end` would just fail its own argument parsing, and the record would sit with
`"end": null` forever, which reads as an honestly-crashed step even though nothing crashed.
Name the file after the step (`.human-review/.step-autoreview`, `.human-review/.step-data`, …)
so two steps mid-flight at once never share a handle. This step and Step 9 itself are the two
deliberate exceptions — neither wraps anything, so their own cost lands in `residual` rather
than being pinned on a tab that did not earn it.

## Step 1 — Run the automated reviews, fix what is not disputable

```sh
${SKILL}/scripts/steps-ledger.py start autoreview --label "code-review + simplify" \
  > .human-review/.step-autoreview
```

Everything from here through the end of this step — both invocations, the classification,
and the fixes — lands on the **Autoreview** tab, so open the record now and close it once,
at the bottom of the step, rather than per command: the tab is one continuous piece of work
and a reader does not care which minute inside it a given fix landed in.

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

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-autoreview)"
```

## Step 2 — Diagram deltas (red = added, red + struck = removed)

```sh
${SKILL}/scripts/steps-ledger.py start data,packages --label "diagram deltas" \
  > .human-review/.step-data
${SKILL}/scripts/puml-diff.sh $BASE .human-review/assets/diagrams
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-data)"
```

Diffs **every** `.puml` that differs from the merge-base — committed, modified or
untracked — and dispatches by diagram family:

| family | differ | semantics |
| --- | --- | --- |
| class / ER / package | `${SKILL}/puml-diff/puml_diff.py` | set of elements + relations |
| sequence | `${SKILL}/puml-diff/seq_puml_diff.py` | ordered script of messages |

Writes `<name>.diff.puml`, `<name>.diff.svg` and `MANIFEST.tsv` (name / source / kind /
status / files). Zero changed diagrams is a quiet success — drop the section.

A structural delta is also rendered at several radii of unchanged context (`focus0`,
`focus1`, `focus2`, plus the whole diagram), and the page **opens at one hop**. The whole
DB and DomainModel deltas are forty unchanged entities with the change somewhere inside;
every reviewer who met one did the same thing, which was click `1`. One hop already carries
the neighbourhood that "is this change in the right place?" needs, and `all` is one click
away — a click the reader now makes only when the neighbourhood was not enough.

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
produces diagrams — and naming only the first is how half the gallery goes stale. Everything
from here to the end of this step lands on the **Behaviour** tab — the traced runs, the
base-diagram regeneration below when it runs, and the manifest re-run — so one ledger record
covers the whole step, opened now:

```sh
${SKILL}/scripts/steps-ledger.py start behaviour --label "sequence diagrams from traces" \
  > .human-review/.step-behaviour
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
distinguish from a real change is worse than no diagram. Close the record once this step's
diagrams (base regeneration included, whether or not you actually ran it) are in the manifest:

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-behaviour)"
```

⚠️ The DB arrows are labelled from Hibernate's own comment on each statement
(`hibernate.use_sql_comments`, in the backend's `application.properties`). A backend
started before that property existed emits statements without it, and every DB arrow
falls back to its span name — `SELECT petclinic`, over and over. If that is what you see,
the running backend predates the property: restart it and re-record.

**Show what generated each diagram.** A sequence diagram is evidence only if the reviewer
can get to the test that produced it: give every diagram in the guide a link to the
scenario source (`file:from-to`, so VS Code opens it at the test) and a link to the `.puml`
itself. A picture with no provenance is a picture they have to trust.

**Put the test and its sequence in one block**, not in two lists on the same tab. They were
a gallery of diagrams above a list of test snippets for a while, and the reader had to work
out which picture belonged to which test from the file names — nothing hidden, nothing
reliable. Use a `testpairs` block (step 9) and the renderer does the pairing from data that
already exists: `MANIFEST.tsv` says which test file each diagram was generated from, and
the `== [[src://<file>:<line>{…} <title>]] ==` chapter dividers inside that file's `.puml`
say which scenarios, at which lines. Each pair renders as the scenario titles and their
deep links, then whichever of the block's snippets quote that test file, then the diagram.

A test with **no** diagram is neither dropped nor given one it did not produce: its
snippets fall into a trailing "Tests that record no sequence" group that says exactly
that. Both halves matter — a fabricated pairing is a lie about provenance, and a dropped
one is the silent loss this pipeline exists to prevent.

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
${SKILL}/scripts/steps-ledger.py start shape --label "Code City capture" \
  > .human-review/.step-shape-codecity
LIT=$(${SKILL}/scripts/capture-codecity.sh .human-review/assets/codecity.png highlight)
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-shape-codecity)"
```

The capture now fails rather than producing an unhighlighted skyline — if the `Changes`
option it drives is ever renamed, the assignment used to be a silent no-op. `shape` is the
same tab id Step 6 feeds below — **Cost & shape** holds both the complexity delta and this
screenshot, so the two steps' records simply add up on it rather than colliding.

## Step 5 — Video of the feature

Record the feature actually working, with Playwright, straight into the guide:

```sh
${SKILL}/scripts/steps-ledger.py start video --label "feature recording" \
  > .human-review/.step-video
${SKILL}/scripts/record-feature-video.sh .human-review/assets/<feature>.webm
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-video)"
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

**Give the video its own tab, and never emit a player for a file that is not there.** The
very first thing this page got wrong was a `<video src="assets/….webm">` whose asset no
step had written: a black rectangle stuck at 0:00 under a confident heading, with nothing
to say the film was missing rather than broken. The builder now emits the player **only**
for a file on disk; when there is none it emits a notice naming the absent file and
**keeps the transcript**, because the cue list is a written account of the same walkthrough
and is worth reading on its own. `scripts/test_build_review.py` pins both halves.

**The links to the running app live inside the captions.** They used to be a paragraph of
their own — "Pages this change touches: owner detail · all visits · vets" — a second list
of the screens the narration was already walking through, in different words and a
different order. Declare them on the video section instead and the builder wraps the words
that are already in the cue:

```json
"appLinks": [
  {"href": "http://localhost:4200/owners/2", "label": "owner detail", "anchor": "visit list"},
  {"href": "http://localhost:4200/vets",     "label": "vets",  "anchor": "who will attend"},
  {"href": "http://localhost:4200/visits",   "label": "all visits"}
]
```

`anchor` is the phrase to wrap, matched in the first caption that contains it and never
nested inside a link already placed. A link whose phrase is nowhere in the narration — or
that has no `anchor` at all — is **not dropped**: it is printed after the transcript as
*"Touched but not filmed"*, which is a fact about the coverage of the film and worth
saying out loud.

**It plays on arrival and pauses on the way out.** The tab script dispatches `panelshow` /
`panelhide` on each panel as it becomes the active one; the video starts on the first and
pauses (never rewinds) on the second, so coming back resumes where the reader left off.
`play()`'s rejection is swallowed, because audible playback is only granted off a user
gesture — a click on the Video tab is one, opening the page straight on `#video` is not.
**Do not answer that by muting.** The narration is the point of the film; a silent film
here is worse than a paused one, so the reader gets the controls instead. `show all` starts
nothing, since no panel is *the* active one there.

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

Everything through the baseline run and the delta render below lands on **Cost & shape**,
the same tab Step 4 fed — open the record once, here:

```sh
${SKILL}/scripts/steps-ledger.py start shape --label "entry-point complexity" \
  > .human-review/.step-shape-complexity
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
from that ref), then render with
`${SKILL}/scripts/endpoint-complexity-delta.py before.json after.json --base $BASE_BRANCH`,
which groups by kind (HTTP → MCP → listeners → jobs) and colours **green for an increase, red
for a decrease** — colour reads as authorship (what the branch added/removed), not as judgement.

The lede no longer restates that legend, because **every bar explains itself on hover**: the
grey segment says what the flow measured on the base branch and that the number is *measured,
not estimated* (it is the committed complexity JSON at the merge-base, from the same
extractor); the coloured segment says how much this branch added or removed, with both
endpoints; the whole bar gives `before → after`, and an untouched row says so in one
sentence rather than being silent. Pass `--base` the real branch name — it defaults to
whatever `origin/HEAD` points at, and hardcoding "main" is wrong in every repository that
calls it something else. A tooltip naming the wrong branch is worse than none.
Report `before → after (Δ)` **ranked inside the full list**, so the reviewer sees whether the
change made a cheap entry point expensive or merely nudged an already-heavy one. If the baseline
predates a widening of what counts as an entry point, say so — the newly-visible kinds will
otherwise read as "added by this branch".

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-shape-complexity)"
```

## Step 7 — What the REST contract did

```sh
${SKILL}/scripts/steps-ledger.py start api --label "REST contract diff" \
  > .human-review/.step-api
${SKILL}/scripts/openapi-diff.py   --base $BASE --out .human-review/assets/openapi-diff.html
${SKILL}/scripts/openapi-diff.py   --css  >  .human-review/assets/openapi-diff.css
${SKILL}/scripts/openapi-compat.py --base $BASE --out .human-review/assets/openapi-compat.html
${SKILL}/scripts/openapi-compat.py --css  >  .human-review/assets/openapi-compat.css
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-api)"
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
`openapi-compat.py` puts a second opinion at the top of the tab: a single machine verdict,
`no_changes` · `compatible` · `incompatible`, with every incompatibility named in the currency
of the caller ("now requires `date`", "the operation is gone", "`maxLength` unset → 5").

It sources the **affected-operation list** from
**[oasdiff](https://github.com/oasdiff/oasdiff)**, because oasdiff resolves `$ref`s before
it counts. That is the whole difference: a property added to `components.schemas.VisitDto`
moves two lines of the document and reaches every operation that `$ref`s it — eleven of
them on the reference change set, where a differ that stops at the `$ref` reports four.
Install it with `brew install oasdiff`; `OASDIFF_BIN` points at another copy and
`--no-oasdiff` forces the fallback.

**[OpenAPITools/openapi-diff](https://github.com/OpenAPITools/openapi-diff)** — the Java
reference implementation, the one people put in CI — is kept as that fallback, deliberately:
it is auto-downloadable without Homebrew, so it is what keeps this tab working on a machine
that has nothing installed. The script resolves it itself: `--jar`, then
`$OPENAPI_DIFF_JAR`, then `~/.cache/human-review/`, then a download from Maven Central
**verified against Maven's own sha1**, then `--docker` for a machine with no JVM. With
oasdiff present the 20 MB jar is never fetched and no JVM ever starts.

⚠️ **In fallback mode the page says so, in amber.** The seal reads `COMPATIBLE · PARTIAL
LIST` and a band under the verdict states that the operation list is a **lower bound** —
because a `$ref`-blind pass genuinely cannot know how many operations a shared schema
reaches. It is a user-visible state, not a rendering bug: do not "fix" it in the prose, and
do not present the short list as complete. Installing `oasdiff` and re-running is the fix.

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

### Step 7b — the same two revisions, read by a third differ

This is its own tab, **Spec changes**, not more content on the API contract tab beside it —
so it gets its own ledger record rather than sharing Step 7's:

```sh
${SKILL}/scripts/steps-ledger.py start specchanges --label "pb33f report" \
  > .human-review/.step-specchanges
openapi-changes html-report --no-logo --no-explorer \
  --report-file .human-review/assets/openapi-changes.html \
  "$MERGE_BASE:openapi.yaml" ./openapi.yaml
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-specchanges)"
```

[pb33f/openapi-changes](https://pb33f.io/openapi-changes/) is not a summary — it is a whole
application in one file: its own change list, its own side-by-side diff of the two
revisions, and its own tree of the document. Three ways of asking the same question that
this page does not otherwise offer, so it is **embedded whole** rather than picked apart and
re-drawn in this page's styles:

```json
{"id": "pb33f", "title": "The same two revisions, read by a third differ",
 "body": "<p>…what it says, in this page's own words…</p>",
 "embed": {"src": "assets/openapi-changes.html",
           "label": "openapi-changes report — openapi.yaml at <sha> against the working tree",
           "missing": "run `openapi-changes html-report` (brew install pb33f/taps/openapi-changes)"}}
```

`--no-explorer` drops the document explorer and saves **32%** — 4.51 MB → 3.07 MB — and
loses nothing the tab is there for. `--no-logo` keeps the frame from advertising.

**It is an optional Homebrew binary.** If `openapi-changes` is not on `PATH`, skip the step:
the section's `embed` degrades to a line naming the missing tool, and if the tab holds
nothing else it is dropped and named under the strip. Never let a missing optional tool fail
the run.

Two things the tab must say in **this page's own words**, because an iframe is a fence — it
is a separate origin off `file://`, so `⌘F` stops at its edge and the reader's find bar
cannot reach the evidence:

- the finding itself, written out, with a full-page link (`<a href="assets/openapi-changes.html">`)
  for reading the diff at width;
- **why its numbers differ from the tab beside it.** They count at different levels and
  neither is wrong: a document-level differ reports the two shared schemas that literally
  moved, while a `$ref`-resolving one reports the eleven operations that serve them. Say
  that, rather than leaving a reader to conclude one of the two tabs is broken.

### The same verdict, on the screen everyone already knows

This embed lands on the **API contract** tab beside the compatibility verdict, not a tab of
its own — it is a second view of the same finding, not a fourth opinion — so it shares
Step 7's tab id:

```sh
${SKILL}/scripts/steps-ledger.py start api --label "contract blast radius" \
  > .human-review/.step-api-visual
${SKILL}/scripts/openapi-visual-diff.py --base $MERGE_BASE --spec openapi.yaml \
  --out .human-review/assets/openapi-visual-diff.html
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-api-visual)"
```

The two blocks above answer *what changed* in prose. This one answers **where it lands**,
on the surface every developer on the team has already spent years reading:
[Swagger UI](https://swagger.io/tools/swagger-ui/), rendering the new spec, with the
endpoints nobody touched faded to a third and the impacted ones carrying a coloured spine,
a badge and their changes inline. A controller the change set left alone starts collapsed
and grey, so the shape of the blast radius is legible before a single word is read — which
is the one question a prose changelog answers slowest.

It is not a fourth opinion: it runs `oasdiff changelog`, the same engine `openapi-compat.py`
sources its operation list from, so a disagreement with that tab would be a bug, not a
finding. What it adds is the *placement* — nine changed operations scattered across four
controllers read as a list of nine lines in the tab above, and as four lit-up rows in a
document of forty-three here.

```json
{"id": "swaggerdiff", "title": "The same verdict, on the screen everyone already knows",
 "body": "<p>…what the blast radius looks like, in this page's own words…</p>",
 "embed": {"src": "assets/openapi-visual-diff.html", "class": "oaviframe",
           "label": "openapi-visual-diff — openapi.yaml at <sha> against the working tree",
           "missing": "run scripts/openapi-visual-diff.py (needs `brew install oasdiff`)"}}
```

⚠️ **`"class": "oaviframe"`, not the default.** The default frame is painted pb33f's
near-black because pb33f's report is dark whatever the reader's system says. This one
follows the system theme like the rest of the page, so on a light desktop the default class
would put a black gutter around a white document.

Same fence, same duty as 7b: an iframe is a separate origin off `file://`, so `⌘F` stops at
its edge. State the finding in this page's words and leave the frame as the evidence. The
page also takes `?theme=dark` / `?theme=light` if you ever need to pin it against a reader's
system setting — the embed deliberately does not, so that both documents change together.

`oasdiff` is the only requirement, and it is already installed for the tab above; the
`embed` degrades to a line naming what is missing, exactly like 7b.

## Step 7c — What it will say for itself in production

```sh
${SKILL}/scripts/steps-ledger.py start logging --label "structural logging scan" \
  > .human-review/.step-logging
${SKILL}/scripts/logextract.py petclinic-backend \
  --repo . --since $MERGE_BASE --json .human-review/assets/logging.json
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-logging)"
```

The question is what a 3 a.m. pager gets when this code misbehaves, and **grep cannot
answer it**. `log.info(x)` is a logging statement, `Math.log(x)` is not, `flux.log()` is
Reactor, and a local variable that merely happens to be called `log` is neither. So this is
structural, in two `ast-grep` passes:

1. build a per-file symbol table of identifiers that really are loggers — a declared
   `Logger`/`Log`/`XLogger`/`FluentLogger`/`LogAccessor` type, a `LoggerFactory.getLogger(…)`-style
   initialiser, or one of the Lombok annotations `@Slf4j @XSlf4j @Log4j2 @Log4j @CommonsLog
   @CustomLog @Log @Flogger @JBossLog` — resolved through the `extends` chain and through
   enclosing outer classes, because an inner class routinely uses the outer one's logger;
2. accept `$RECV.$METHOD(…)` only where `$RECV` is in that table.

`System.out` / `System.err` / `printStackTrace` are collected **separately** and labelled as
an anti-pattern, never mixed into the logger counts: they have no level, no timestamp, no
MDC, no appender, and are invisible to the aggregator. Validated on seven Java projects —
100% recall on `spring-framework` (2111 hits across 9186 files) and no false positives in
140 hand-checked. `--since $MERGE_BASE` restricts the result to statements on added or
modified lines, which is the set the tab is about.

⚠️ **Three `ast-grep` gotchas are load-bearing** and are commented where they bite in
`logextract.py`. The expensive one: **a `constraints:` block attached to a `has:` does not
backtrack.** The matcher binds the first child that fits the pattern, then tests the
constraint, then gives up — it does not try the next child. That silently lost every
`@Slf4j` that sat behind another annotation, and lost it *quietly*, as a class that appeared
to log nothing. Do not "simplify" a rule by folding a `has:` and a `constraints:` together.

**The tab is the answer, not the homework.** It used to open on a table of every touched
Java file, most of them saying `0 logging` — on a twelve-file change set with two new
`log.warn` calls, that is ten rows of noise sitting on top of the two lines a reviewer
actually came for. There is no such table any more, not even collapsed behind a
`<details>` — it does not exist at any level. What proves the scan actually ran over
every touched file is simpler than an inventory of them: the tab rendering at all
(weight 1, a real sentence or a real snippet) *is* the proof, and the one case that is
not proof — `ast-grep` missing, or the extractor crashing outright — drops the whole tab
with a loud line in the build log instead, a visibly different failure from a tab that
opened and rendered a real zero (see below). So the tab is now, quite literally, the
statements themselves and nothing else: one `.snippet` figure per statement — the same
figure `extract-snippet.py` cuts everywhere else on this page, not a second, invented
code-block style. There is no separate line for the level any more either: it rides into
the box's own path tag (`WARN · Class:line`), which sits at the bottom-right of the
footer row under the code, on the same line as the verdict — the trace on the left,
because that is what a reviewer reads, the location on the right, because that is what
they click. It moved twice before landing there (a top-right corner tag, then the same
row ahead of the verdict) — say so if you are reading an older description of this tab,
and trust this one.

**The zero case is a finding, not an empty section.** A change set that logs nothing is not
silence, it is an answer, and it is often the most review-worthy thing on the tab — a branch
whose first finding is a live 500 whose only trace in production would be a generic
catch-all line, say. So a genuine zero renders as a sentence — **"None. Not one logging
statement was added or changed"** — with the same weight as the snippets it replaces. The
tab is never struck through for a zero — a zero here is a statement about this diff, not "we
looked at unrelated context and nothing moved". A scan that could not run at all is the
other thing this must never be confused with, per above: dropped tab plus stderr, not a
rendered page that merely says nothing.

**Each statement also gets a verdict from a live model call — ✅ SAFE, 🤔 DOUBT, or
❌ PRIVACY — with its provenance cut from the working tree underneath.** A word-list
heuristic was tried first and rejected: it is trivially fooled exactly where it matters,
because it can only ever look at a *name*. `log.info("{}", x)` where three lines up sits
`String x = owner.getName();` reads as nothing to a word list — `x` says nothing — so it
comes out either a shrug or, worse, a false SAFE if `x` happens to collide with a
safe-looking word by accident. Knowing what a value actually holds means following the
assignment, and that needs a model, not a list.

So `privacy_verdict()` asks one: `_statement_context()` hands it the statement's enclosing
method (`logextract.py`'s `method-decl`/`any-field` rules resolve method and field ranges
structurally, the same AST pass that finds the statement itself) plus the class's numbered
field declarations — never the whole file, never the one line alone — and the model traces
every interpolated value back through it, hop by hop, to something self-evident: a
parameter with a declared type, a literal, a field declaration, a call whose return type
settles it. **The trace is not narrated, it is shown**: each hop names a `file:line`
inside the given context, and the page — not the model — cuts that exact line fresh from
the working tree with the same snippet machinery as everything else here, so the model's
claim and the page's evidence cannot disagree. A hop the model cannot resolve is reported
unresolved, never invented, and forces the verdict to DOUBT; a hop that resolves but whose
cited line does not check out against the tree (a stale citation, or a hallucinated one)
is treated exactly as seriously — a SAFE verdict downgrades to DOUBT on the page, with a
note saying why, even though the cache still remembers what the model actually said. A
statement whose arguments are already self-evident (`vetId`, an int parameter) gets no
chain at all — the one-clause trace in the verdict line is the whole story, and the box is
not padded with a hop that has nothing to add. A chain longer than about three hops shows
its first link and its last, with a note for how many sat between them: evidence, not a wall.

Verdicts are cached by a hash of the statement plus everything sent as its context, next to
the other review artifacts (`.human-review/.privacy-verdicts.json`), so a re-run on
unchanged code neither flips the answer nor pays for it twice — the chain is re-cut from
the tree on every render regardless, cache hit or not, because a citation is only evidence
if it still points at real code *now*. A call that cannot be reached at all — no `claude`
binary, a timeout, a malformed response — renders **⚠️ NOT EVALUATED** in its own colour,
never folded into DOUBT (which means the model looked and could not tell) and never
silently SAFE. The legend under the last snippet is a vertical list, one mark per line,
headed **`🤖 AI Evaluation:`** — accurate now in a way a word list's legend could not
honestly have claimed, which is exactly why the heading survived the naming discussion
that shipped alongside this rewrite.

Two context registers sit under the finding, both authored, both checked against the
extractor's own counts (a section that quotes three of four statements draws a build
warning): **pre-existing logging** ("what does this service log today", answered straight
after "nothing new") and **console output where a logger belongs**. Their snippets are cut
with `--exact` and their `path:line` link is aimed at the **statement**, not at the first
line of the window — landing a reader on four lines of context and making them find the
`log.warn` by eye defeats the point.

## Step 8 — Who has to approve this

```sh
${SKILL}/scripts/steps-ledger.py start owners --label "codeowners check" \
  > .human-review/.step-owners
${SKILL}/scripts/codeowners-check.py --base $BASE --state
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-owners)"
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
titles them `path:from-to` as a `vscode://file/<abs-path>:<line>:1` link — note the **two**
slashes after `file`, which is how VS Code takes an absolute path; one slash silently
resolves nothing.

The range is **snapped**: it skips a leading blank or comment line and extends to whatever
brace the window opened, so a snippet always shows where the quoted method stops. That is
right when the snippet *is* the method and wrong when it is one statement in its
neighbourhood — `--exact` turns both snaps off and gives you the window you asked for. The
`--caption` is **HTML**, like every other piece of prose in the content file, so
`<code>log.warn</code>` renders as code and a literal `<` has to be written `&lt;`.

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

### What each tab cost

The scope chip answers "what did this run cost" for the run as a whole. It cannot say
which *tab* that cost went into, because nothing links one turn of the assembling
conversation to one tab of the finished page — a turn is just a timestamp in a transcript,
and a tab is a JSON object nobody stamped with a clock.

The fix is a second small artifact next to `.human-review/.started`:
`.human-review/.steps.json`, a start/end ledger that the steps you already run stamp as
they run, naming the tab(s) their output lands on. Wrap a step exactly like this — Step 2
above does precisely this:

```sh
${SKILL}/scripts/steps-ledger.py start data,packages --label "diagram deltas" \
  > .human-review/.step-data
${SKILL}/scripts/puml-diff.sh $BASE .human-review/assets/diagrams
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-data)"
```

`start` prints the record's index, which `end` needs to close the *right* record — steps
nest and interleave (a shell step that calls another script that stamps its own step), so
"the most recent record" is not a safe enough handle. The index goes through a file rather
than a shell variable **on purpose**: `STEP=$(steps-ledger.py start …)` only survives if
`end` runs in the same shell invocation, and most steps do not — Step 1's span crosses two
skill invocations and everything you do between them. Shell state does not carry across
separate tool calls; the working tree does, so the file does too. A step that crashes
between the two calls — or loses its handle, which fails the same way — leaves a record
with `"end": null` — a true account of a step that began and never finished, not silently
folded into whichever step happens to run next.

At build time `scripts/review-cost.py --tab-costs` reads that ledger alongside the same
deduped, priced transcript turns the scope chip uses, and attributes each turn to the
tab(s) whose window its timestamp falls inside. The renderer calls it once per page (not
once per tab) and hangs the result on every tab's existing `data-tip` — appended after the
struck-through-tab tooltip where both apply, never a second tooltip component. Nothing in
`content.json` opts into this: every tab gets a cost sentence on hover, automatically,
because the runner is the same skill build that runs the steps in the first place.

Three details make the number honest rather than merely present:

* **A turn that lands in nobody's window is `residual`, never dropped and never spread.**
  Step 0, Step 9, and any step that never ran through the ledger all fall here — real cost
  with no single tab to blame it on. It is not a fourth, orphaned number: it rides on the
  scope chip's own tooltip, right next to the total it is a slice of, which is why that chip
  is the residual's home rather than a UI element built to hold one fact.
* **A turn inside two tabs' windows at once splits evenly between them.** A step that feeds
  both Data model and Packages in one pass (the diagram delta step, typically) is not
  double the work twice over — splitting means the per-tab figures add back up to the run
  total instead of inflating it.
* **A missing measurement never looks like a measured zero.** A tab whose step genuinely
  produced no billed turns in its window reports `<$0.01 measured for this tab` — a fact.
  A tab the ledger never mentions reports `not measured for this tab — no step in the
  ledger named it`, and a step that started but crashed before its `end` reports `started
  but never recorded finishing`. All three read differently on hover on purpose: `$0` and
  "we never checked" are not the same claim, and collapsing them into one blank tooltip
  would have been the same silent-wrong-page failure mode Step 0 already guards against
  for the whole run.

None of this is required for the page to build — a run with no `.steps.json` still renders
every tab, each saying plainly that its cost was not measured, the same way the scope chip
itself degrades to nothing rather than a wrong number when there is no session to read.

### Lay it out as tabs, not as a scroll

A review is not one argument read top to bottom. It is five or six separate questions — *what
do you want from me? does it work? does the contract still hold? what shape did it land in?
what did it cost?* — and a reviewer answers them in whatever order their doubt takes them. A
single column forces them past four answers to reach the one they wanted, so the page is a
**tab strip over panels**, driven by a `tabs` array in the content file:

```json
"tabs": [
  {"id":"autoreview","label":"Autoreview","count":true,
   "intro":"<p class=\"sub\" id=\"review\">Both /code-review and /simplify ran, and their output was merged before it reached this page…</p>",
   "blocks":[{"type":"findings","title":"Look here first","body":"…"},
             {"type":"autofixes","title":"Already fixed for you","body":"…"}]},
  {"id":"video","label":"Video","blocks":[{"type":"section","id":"seeitwork"}]},
  {"id":"behaviour","label":"Behaviour",
   "blocks":[{"type":"section","id":"tests"},
             {"type":"testpairs","id":"sequences","title":"Sequence deltas","kind":"sequence",
              "body":"…","snippets":[{"ref":"…:41-56","caption":"…"}],
              "unpaired":{"id":"tests-nosequence","title":"Tests that record no sequence","body":"…"}},
             {"type":"section","id":"notcovered"}]},
  {"id":"data","label":"Data model",
   "blocks":[{"type":"diagrams","only":["DB","DomainModel"]},{"type":"section","id":"logic"}]},
  {"id":"packages","label":"Packages",
   "blocks":[{"type":"diagrams","only":["Packages"],
              "context":{"src":"petclinic-backend/docs/packages.puml","name":"Packages","note":"…"}}]},
  {"id":"owners","label":"Code owners","blocks":[{"type":"codeowners"}]},
  {"id":"api","label":"API contract","badge":"+4","blocks":[{"type":"section","id":"apicompat"}]},
  {"id":"specchanges","label":"Spec changes","tip":"pb33f's whole report, embedded rather than summarised.",
   "blocks":[{"type":"section","id":"pb33f"}]},
  {"id":"logging","label":"Logging","tip":"Every logging statement the change set added — found by syntax, not by grep.",
   "blocks":[{"type":"logging","base":"origin/main","paths":["petclinic-backend"],
              "id":"logging-added","title":"Logging this change set added","body":"<div class=\"lede\">…</div>",
              "existing":{"id":"logging-existing","title":"…","body":"…","snippets":[]},
              "console":{"id":"logging-console","title":"…","body":"…","snippets":[]}}]},
  {"id":"shape","label":"Cost & shape",
   "blocks":[{"type":"section","id":"complexity"},{"type":"codecity"}]}
]
```

Block types: **`findings`** (the disputable calls), **`autofixes`** (the top-level
`autofixes` array — what you applied in step 1, same shape as a finding), **`diagrams`**
(the delta gallery, narrowed by `kind` / `only` / `except`), **`testpairs`** (step 3 —
each acceptance test beside the sequence its own run recorded, paired from the manifest and
the `.puml` chapter titles, with the tests that record no sequence in a trailing group),
**`logging`** (step 7c — the structural logging scan: one reused `.snippet` box per
statement it found on changed lines, each carrying its own GDPR verdict, and the two
context registers under them), **`puml`** (a diagram this branch did *not* change, rendered
from source as context), **`codeowners`** (step 8's check, run by the renderer),
**`codecity`**, **`section`** (one entry of `sections` by `id`), **`html`**.

A `diagrams` block that finds nothing for its `only`/`kind` may carry a **`context`**
— the same shape as a `puml` block's fields (`src`, `name`, `note`, …) — and falls back to
rendering it from source, exactly as a standalone `puml` block would. This is the whole
fix for the Packages tab: without it, a `diagrams` block with nothing to show contributes
no weight, and a tab built on that one block alone is silently **dropped** rather than kept
and struck through — the trap is real even though the Packages example above used to pair
`diagrams` with a second `puml` block that happened to always weigh 1, which worked only as
long as nobody forgot the second block. `context` folds the fallback into the one block that
needs it, so the tab can no longer be dropped by omission.

A tab may also carry an **`intro`** — raw HTML about the *tab*, emitted inside the panel
before its first block and carrying no weight of its own (a tab is not kept alive by its own
preamble) — and a **`tip`**, a sentence shown on hover for a tab whose subject two words
cannot carry.

Two section keys are worth knowing: **`video`** (step 5, with `appLinks`) and **`embed`**
(step 7b, another tool's whole report in an `<iframe>` — `aria-label`, never `title`, since
this page has exactly one tooltip component).

Rules the renderer enforces:

- an **Overview** tab is synthesised as the first tab, holding the summary and the
  verdict, and the page opens on it. They used to sit above the strip, which pushed the
  questions below the fold on a laptop: a reviewer scrolled past the answers to find out
  what the answers were. Declare a tab with `id: "overview"` yourself to take it over;
- a tab whose every block came back empty is **dropped**, and named in the build log —
  never shown as an empty page. It used to be named in a `<p class="sub">` appended after
  the last panel, together with the list of struck-through tabs, and that append landed
  **outside every `<section class="panel">`** — making it the one element on the page no
  tab could hide, sitting under all eleven of them and restating a strike-through the strip
  was already drawing three inches above it. Anything the renderer wants to say about the
  strip either belongs to the strip (which already says it) or belongs to whoever is
  assembling the page. Nothing may be emitted outside a panel;
- a tab that has content but **no delta** — all context, nothing this branch touched — is
  kept and its label is **struck through**, with a tooltip saying so. "We looked, and this
  branch did not touch it" is worth as much to a reviewer as the opposite, and a dropped
  tab cannot say it. `puml` and `codecity` blocks carry content but never a delta (a
  picture of the current state is not a change), and so does a `diagrams` block that fell
  back to its `context`; a `section` counts as a delta unless it declares
  `"unchanged": true`. `noStrike: true` on a tab opts out;
- a changed diagram that no tab claimed prints a **warning** at build time. A gallery that
  silently loses a diagram is the exact failure this pipeline exists to prevent;
- `count: true` puts the item count on the tab, `badge: "…"` puts a literal there, and
  `badgeClass: "alarm"` makes it red — a badge that says something is *wrong* must not
  look like a count, which is why the code-owners block sets both itself rather than
  leaving the severity to whoever wrote the content file. Use a number
  only where it means something — on the tab holding the findings it does; on
  "Data model" it would just count pictures. An alarm renders as a single `!` in a red
  circle, not as the words it stands for: at the width of a tab pill a phrase is unreadable,
  so the phrase moves to `aria-label` (which keeps the button's accessible name "Code owners
  approval required") and to `data-tip`. `badgeLabel` sets it for a literal badge;
- **the Overview lede is checked against the strip it describes.** Write `{{tabcount}}`
  where the number of tabs goes and the build fills it in from the tabs it actually
  emitted — a hand-typed "Ten tabs" above eleven of them is exactly the sentence nobody
  re-reads. The build also warns when the lede fails to name a tab, or names them in a
  different order than the strip. It is a warning, not a failure: a lede may legitimately
  group two tabs into one clause. It may not do so by accident;
- omit `tabs` entirely and you get the original single-column page, unchanged.

⚠️ **The strip has a budget, and it is not the window.** Its inner track is pinned to
**1120 px at every viewport** by its own padding formula, and one row is 34 px at 1280 px
and up. Eleven tabs plus `show all` need about 1130 px, so three rules at the very **end**
of the stylesheet buy the room: `.tabstrip .grow { flex:1 1 0 }` (+16 px), `.tabstrip {
padding-right:1.25rem }` (+20 px at 1280, +100 at 1440 — the strip is full-bleed, so the
right padding is decorative; the **left** padding is untouched, so the first tab still
starts where the body text does), and `button.tab { padding:0 .6rem }` (+88 px across
eleven pills). They must be emitted last or the base sheet's `button.tab { padding:0
.85rem }` wins the cascade and the strip silently wraps onto two rows — no error, nothing
in the DOM to notice. `test_build_review.py` pins the ordering. **A twelfth tab does not
fit**: merge one, shorten a label, or accept two rows deliberately.

Deep links work both ways: `#<tab-id>` opens on that tab, and a link to any `id` inside a
panel switches to it first. `⌘F` searches only the open tab, so the strip carries a **show
all** toggle that reveals every panel at once — which is also what printing does.

The order, as a default worth departing from only with a reason — **Overview, Autoreview,
Video, Behaviour, Data model, Packages, Code owners, API contract, Spec changes, Logging,
Cost & shape** — with the panels in the same DOM order as the strip:

1. **Overview** — synthesised: the summary and the verdict, and the lede that walks the
   reader through the rest of the strip (see `{{tabcount}}` above).
2. **Autoreview** — the disputable findings, most critical first, each with the failing
   scenario in one sentence and a snippet of the decisive lines; then the fixes you already
   applied. The two lists are one decision split in two, and a reviewer who cannot see the
   first pile has to take the size of the second on trust. It is called *Autoreview*, not
   *Review*, because the whole page is the review — this tab is what the automated passes
   said. Its `intro` must state that `/code-review` and `/simplify` output was **merged**
   before it reached the page and is not separable from the recorded data: nothing says
   which command produced which item, so the split below is by *who decides*, not by which
   tool spoke. Give the intro `id="review"` so older `#review` links still land. The two
   header chips point in-page at `#autoreview` — not out to the docs in a new tab, which
   answered a question nobody had while the findings themselves were one click away.
3. **Video** — its own tab, not a paragraph inside Behaviour. It is the one artifact a
   reader can watch instead of read, and burying it under the sequence diagrams meant most
   of them never found it. Rename the section's `id` (`seeitwork`) so the tab can own
   `#video`.
4. **Behaviour** — each acceptance test with the sequence its run recorded directly beneath
   it, then a plain statement of what is **not** covered.
5. **Data model** — the DB and domain deltas, and the 2–5 core-logic bullets in domain
   language, each backed by a snippet.
6. **Packages** — the package delta, or the current package diagram as context.
7. **Code owners** — whether a named human has to approve this before it can merge, and
   which files put them on the critical path.
8. **API contract** — step 7's two fragments, each a `section` with `includeHtml`: the
   compatibility verdict first, because it is the one-word answer, then the classified
   change list underneath it.
9. **Spec changes** — step 7b's embedded pb33f report, beside the tab whose numbers it
   will appear to contradict.
10. **Logging** — step 7c: what this change set will say for itself in production.
11. **Cost & shape** — the complexity increment and the Code City shot.

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
