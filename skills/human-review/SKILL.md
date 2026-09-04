---
name: human-review
description: Build a human-facing review.html for a change set (uncommitted, given commits, or a PR) — runs /code-review and /simplify and applies the non-disputable fixes, then assembles red-annotated PlantUML deltas, a Code City screenshot, a Playwright video of the feature, the endpoint-complexity increment, and verbatim code snippets deep-linked into VS Code. Explicit invocation only — user types /human-review.
disable-model-invocation: true
---

# /human-review — assemble a review.html for a human

Produce **one page, `.human-review/review.html`** — tabbed, one question per tab — that lets
a human review a change set fast. You are the assembler *and* the janitor: you run the
automated reviews and apply the fixes nobody would argue with, then hand the human only the
calls that are genuinely theirs. Do **not** commit or push.

## Setup

Resolve `${SKILL}` once, at the start of the run — a plugin install, an env override, a
project symlink and a gitignored CI clone are all real layouts:

```sh
for candidate in "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/skills/human-review" \
    "${HUMAN_REVIEW_HOME:-/nonexistent}" \
    "$(readlink -f .claude/skills/human-review 2>/dev/null)" \
    ".claude/skills/human-review" \
    "petclinic-backend/.tools/human-review/skills/human-review"; do
  [ -x "$candidate/scripts/build-review-html.py" ] && { SKILL="$candidate"; break; }
done
[ -n "${SKILL:-}" ] || { echo "cannot locate the human-review skill"; exit 1; }
```

**Every script resolves the project under review from the directory it was invoked in**, so
run them from the repository root and never rewrite a path to be relative to the skill.

`reference/content-schema.md` holds every `content.json` shape; read it when you write the
content file in Step 9, not before.

## Project hooks

Five things are yours to substitute; the rest is project-agnostic. Anything you have no
answer for, drop — a tab with nothing to show is dropped and named under the strip.

| step | what it needs from you | reference project's answer |
| --- | --- | --- |
| 3 | a test run that records traces | `cd petclinic-test && ./run-tests-with-tracing.sh` |
| 4 | a Code City render, if you have one | `petclinic-backend/docs/generate-codecity.sh` |
| 5 | a browser suite that can be filmed | Playwright, driven by `scripts/record-feature-video.sh` |
| 6 | an entry-point complexity extractor | `mvn -q test -Dtest=EndpointComplexityExtractorTest` |
| 7 | a committed API spec, generated not hand-written | `openapi.yaml`, extracted by `OpenApiExtractorTest` |

Check the optional binaries before you use them; each missing one drops its tab, named under
the strip, and never fails the run:

| missing | consequence |
| --- | --- |
| `oasdiff` (`brew install oasdiff`) | the Java fallback takes over and the seal reads `COMPATIBLE · PARTIAL LIST` in amber, with a band saying the list is a **lower bound**. That amber is correct — do not "fix" it |
| `openapi-changes` (`brew install pb33f/taps/openapi-changes`) | no Spec changes embed (7b) |
| `ast-grep` (`brew install ast-grep`) | no Logging tab (7c) — a false "no logging found" is the one answer that tab must never give |
| Playwright / Pillow / numpy, or both branches unserved | no UX tab (7d) |
| a JVM **and** Docker | the API tab says "not run — no JVM and no Docker" |
| `swiftc`/macOS | step 5 is captioned but silent |

Pygments and Pillow are top-level imports, not optional: without Pygments **step 9 cannot
build a page at all**. `annotate-feature-video.py` looks for a font in macOS paths and exits
if it finds none — add a DejaVu/Liberation path before relying on it on Linux.

## Step 0 — Resolve the change set (from `$ARGUMENTS`)

- **empty** → uncommitted work: `BASE=HEAD`, diff = `git --no-pager diff HEAD`.
- **a ref / range / SHA(s)** → `BASE=<the older ref>`, diff = `git --no-pager diff $BASE...HEAD`.
- **a PR** (`#123`, `123`, a github URL) → `gh pr checkout <n>`,
  `BASE=$(gh pr view <n> --json baseRefName -q .baseRefName)`.

Default `BASE` to `origin/main`. Every script diffs against the **merge-base**, so commits
that landed on the base after this branch started never show up as this branch's.

Print a one-line scope banner (mode, refs, `--stat`). Empty change set → "Nothing to
review." and stop.

**If `.human-review/review.html` already exists, ask what changed before you run any of
this** — see *Iterating on a review that already exists* below.

### The gate: push, then wait for green — no green build, no review

**Nothing below this line runs until the branch is pushed and the build for the exact commit
you pushed has gone green.** This is a gate, not a warm-up — it comes before the wipe, so a
run that stops here leaves the previous guide intact.

```sh
git status --porcelain                 # decide, deliberately, what belongs in the branch
git push                               # -u origin HEAD the first time
SHA="$(git rev-parse HEAD)"            # the commit under review, from here on
```

⚠️ **The gate is about the branch as pushed *now*, not the tree at the end of the run.** This
skill deliberately leaves its own fixes uncommitted for a human to inspect (Step 1), so do
not read this as "push the review's fixes too". Push what belongs to the *branch*, gate on
that, and let the review's own edits stay in the working tree.

**Bind the wait to `$SHA`, never to "the latest run on the branch"** — a branch almost always
has *some* green run on it, and a green run for a different commit is exactly the
confidently-wrong signal this page exists to avoid.

```sh
gh run list --commit "$SHA" --limit 1 --json databaseId,status,conclusion,workflowName
```

| what comes back | what to do |
| --- | --- |
| a run, `status` not `completed` | `gh run watch <databaseId> --exit-status` and wait |
| a run, `conclusion` = `success` | proceed |
| a run, any other `conclusion` | **stop.** Name the workflow and the failing job in the report; fix that first |
| **`[]` — no run at all** | **stop. Absence is not success** — either the push has not registered a run yet (wait and re-ask), or nothing triggers on this branch |

**A repository with genuinely no CI is allowed through, but it must say so.** If
`gh workflow list` is empty, continue and put *"no build proved this — the repository has no
CI configured"* in the guide. It must not read as a pass.

Give a long build up to **20 minutes** before treating it as stuck, and say in the terminal
that you are waiting and on which SHA — a silent runner looks hung to the human watching it.

**Then wipe `.human-review/assets/` and recreate it**, and reset the ledger with it. Every
fragment producer writes to a fixed path and the renderer inlines whatever it finds there
with no freshness check, so a step that fails silently leaves the *previous run's* artifact
in place — a green `compatible` seal for a diff it never saw. A stale `.steps.json` is worse
because it parses: every tab reports a confident **`$0.00`** instead of "not measured".

```sh
rm -rf .human-review/assets && mkdir -p .human-review/assets
${SKILL}/scripts/steps-ledger.py reset
date -u +%Y-%m-%dT%H:%M:%S+00:00 > .human-review/.started
echo "$CLAUDE_CODE_SESSION_ID"                  > .human-review/.session
python3 -m pytest -q "${SKILL}/scripts" "${SKILL}/puml-diff"     # ~2 s; nothing else runs them
```

`.started` is what lets the page report what it cost; `.session` is what lets a later
rebuild read the right transcript.

Every step that produces a tab's content brackets its commands with a ledger wrap naming the
tab(s) it feeds:

```sh
${SKILL}/scripts/steps-ledger.py start <tab[,tab2]> --label "…" > .human-review/.step-<name>
# … the step's commands, and everything you do to work through them …
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-<name>)"
```

**The index goes through a file, never a shell variable** — a step's span routinely crosses
several tool calls, and shell state does not survive between them. Name the file after the
step. Step 0 wraps nothing; Step 9 wraps itself against the reserved id `guide`.

Two rules for the runs that do not go to plan, which is most of them:

1. **Check a step's prerequisites *before* you stamp `start`.** A gate that fails after the
   stamp leaves a record naming a tab the page will not contain.
2. **A step you abandon still gets its `end`.** Leave `end` off only for a step that
   genuinely died mid-flight — that is what `"end": null` means, and the page says so.

## Iterating on a review that already exists

Most invocations after the first are not reviews. The page is on disk and what is wanted is
a change to the *page*: a finding reworded, a snippet re-cut, a caption naming the wrong
file. That is an **iteration**, and it runs almost nothing.

⚠️ **On an iteration you do not invoke `/code-review`, and you do not invoke `/simplify`.**
The code they would read has not changed, so a second pass re-derives text the page already
contains, at full price — and not identically: two runs word and rank their findings
differently, so a request to fix one caption comes back with a reshuffled **Look here first**
list nobody asked for. Every other producer here is a program that reads files already on
disk; re-running those costs CPU, not tokens.

**Which is it?** One question decides: *has the code under review changed since the page was
built?* New commits, a force-push, the human fixed a finding, the PR moved → **re-review**,
start at Step 0. Only the report changed → **iteration**. When the request is ambiguous, the
tree answers:

```sh
python3 -c 'import json,sys; print([r.get("rev") for r in json.load(open(".human-review/.steps.json")) if "rev" in r])'
git --no-pager log --oneline <that rev>..HEAD
git status --short
```

Commits this run did not make, or working-tree changes beyond the fixes Step 1 left
uncommitted, mean re-review. If it stays unclear, **ask**.

An iteration, in order, skipping every line the request did not touch:

1. **Edit the keys in `.human-review/content.json` the request names, and nothing else.**
   Snippets are extracted at build time, so widening a range is an edit to a `path:from-to`
   string, not a re-extraction.
2. **If you applied a fix, re-run only the producers that read the files you touched**, with
   the same arguments Steps 2–8 give them.
3. **Rebuild**, then **serve** — the listener from the first run is sticky, so this is the
   same URL. Print it again as the last line: the reader closed the panel.

A prose-only iteration is 1 and 3 — one edit and one build.

**Do not `steps-ledger.py reset`, do not rewrite `.human-review/.started`, do not wipe
`assets/`, and do not open ledger records for the iteration's own edits.** They belong to the
run they timed. The iteration's own spend is not a tab's cost and has no row on this page.

⚠️ **An iteration in a *new session* cannot recompute the cost, and it will not say so** —
the first run's turns sit in a transcript nobody is reading, so the chip reports the
iteration's couple of dollars under a label that says what the review cost, and every tab
prints `$0.00`. Pin the build to the session that did the work:

```sh
CLAUDE_CODE_SESSION_ID=$(cat .human-review/.session) \
  ${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
```

**When the id cannot be recovered**, publish no number rather than a measured-looking one:
delete `.human-review/.steps.json` and drop `{"auto": "cost"}` from `scope`. A blank where a
measurement used to be is a smaller lie than `$0.00`.

## Step 1 — Run the automated reviews, fix what is not disputable

```sh
${SKILL}/scripts/steps-ledger.py start review --label "code-review + simplify" \
  --rev "$(git rev-parse HEAD)" \
  > .human-review/.step-review
```

⚠️ **`--rev` is the pre-fix HEAD, and this is the only chance to record it.** Every applied
fix renders as a real before/after diff, and a diff needs a left side; it is also the default
`base` for every `diffs` entry. Reconstructing it afterwards is archaeology that gives wrong
answers — and on the ordinary run it is impossible, because this skill leaves its fixes
uncommitted and there is no commit to find.

The whole step lands on **🤖 Review**, so open the record now and close it once, at the
bottom.

Invoke **`/code-review`** and **`/simplify`** on the same range — in that order, one after
the other, **never in the same turn**. They are not two opinions on one question:
`/code-review` hunts correctness bugs, `/simplify` shrinks the solution and applies its own
cleanups to the tree. Step 9 reports what each did on its own, and two runs whose edits land
interleaved can no longer be told apart.

⚠️ **Provenance exists only while the pass is running, so record it there.** No raw output
survives the step. Each item gets `"source": "/code-review"` or `"source": "/simplify"` **as
it is recorded**, before the merge. Never write a source you did not watch happen in the same
turn.

Then split every finding in two, and say out loud which pile each landed in:

- **Non-disputable → fix it now.** One obvious right answer, no behaviour change, no product
  call: a duplicated helper, a test that passes vacuously, a shared persistence context
  hiding a missing `save()`, an uninitialised model field, a positional selector, a dead
  import. Fix, then re-run the affected tests.
- **Disputable → hand it to the human.** Anything that changes an API contract, a migration
  already applied somewhere, a data-integrity trade-off, a performance/correctness tension,
  or where two reasonable engineers would pick differently. These become the guide's **Look
  here first** list, most critical first.

Never argue a finding away silently. If you skip one, it goes in the list with a one-line
reason. `/code-review` and `/simplify` are replaceable — a project with its own passes
substitutes them here; the contract is only the item shape in `reference/content-schema.md`.

**The writing-down has one rule: show the code, do not narrate it.** A finding is not a story
about a defect, it is the defect, quoted:

- **Two or three sentences of prose, hard ceiling.** `title` says what is wrong in one line;
  `body` says what breaks and under which input; `why` says what the human has to decide.
  Anything past that is the reviewer reading your reasoning instead of their code.
- **Then the code, and most of the item is code.** `snippets` for an open call — the decisive
  lines, with a `caption` pointing at the *one thing* in them (`"the null branch that never
  runs"`), not a summary of the block. Prefer several short captioned snippets over one long
  one.
- **An applied fix shows its diff.** A fix described in a sentence with no diff is a claim the
  reader has to take on trust; the build warns when it finds one.
- **No preamble, no restating the title, no "as we can see".** The reader is a senior engineer
  holding their own code.

**Commit whatever is already in the working tree first** — the change set under review and
any in-flight work — so the branch has a clean baseline. Then make your fixes and **leave
them uncommitted**: their whole value is that `git diff` shows exactly what an agent touched.
If a file must be committed for a later step to work, commit it and say which one, and why.

**Do not comment your own decisions into the code.** Never leave a comment whose job is to
justify an edit to the reviewer — that belongs in **Look here first**. A comment earns its
place only when it explains something a future reader could not recover from the code itself.

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-review)"
```

## Step 2 — Diagram deltas (red = added, red + struck = removed)

```sh
${SKILL}/scripts/steps-ledger.py start data,packages --label "diagram deltas" \
  > .human-review/.step-data
${SKILL}/scripts/puml-diff.sh $BASE .human-review/assets/diagrams
${SKILL}/scripts/drawio-diff.py --base $BASE \
  --diagram petclinic-backend/docs/ConceptualModel.drawio.png \
  --concepts petclinic-backend/docs/generated/DomainModel.puml \
  --out-dir .human-review/assets --name conceptual
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-data)"
```

`puml-diff.sh` diffs **every** `.puml` that differs from the merge-base and writes
`<name>.diff.puml`, `<name>.diff.svg` and `MANIFEST.tsv`. Zero changed diagrams is a quiet
success — drop the section. The page **opens at one hop** of unchanged context, with `all`
one click away. Never re-implement the diffing inline and never hand-diff the `.puml` text.

`drawio-diff.py` is the hand-drawn map's differ: it matches elements by the identity they
**declare** in the mxGraph XML, never by rendered text or pixels, so dragging a box is
reported as *moved* — position belongs to the human. It writes
`conceptual-{original,new,diff}.svg` plus `conceptual-diff.json`. Feed the three SVGs to a
`Diff | New | Original` tab widget and **inline** the SVG rather than linking it, so
`light-dark()` resolves against the reader's own theme.

A concept the extractor cannot resolve gets **no anchor at all**, never a broken one, and is
reported on stderr as `! concept <Name> resolves to no class`. That is a finding about the
branch — it should be impossible while `ConceptualModelDiagramTest` passes — not a rendering
detail to paper over.

## Step 3 — Sequence diagrams for boundary-crossing changes

If the change crosses a **system boundary** — a new/changed API call, a new DB column or
query, a new outbound integration — then **every such interaction must be covered by at least
one `@generate_sequence`-tagged `.feature` scenario**. If it has none, add the tag (or the
scenario) — the tag alone, with no comment explaining why you added it.

```sh
${SKILL}/scripts/steps-ledger.py start sequence --label "sequence diagrams from traces" \
  > .human-review/.step-sequence
cd petclinic-test && ./run-tests-with-tracing.sh      # the browser suites (Playwright, Cucumber)
cd petclinic-backend && mvn -o -Pgenseq test -Dgroups=genseq   # the @SpringBootTest suites
cd petclinic-test && GENSEQ_REFRESH=1 npm run trace:diagram    # render what the JVM run recorded
```

**There are two runs, not one**, and naming only the first is how half the gallery goes
stale. The third line is not optional: a `@SpringBootTest` only *records* a trace window, and
without `GENSEQ_REFRESH=1` the renderer re-uses its cached spans and the Java diagrams never
appear.

The suite refuses to run unless the whole stack is up **and started in the right order**
(`start-database.sh` → `start-grafana.sh` → `start-backend.sh` → `start-frontend.sh`).

⚠️ **Verify the running stack is *this* checkout.** `~/workspace` holds several petclinic
clones, and a backend from a different one answers on `:8080` just fine while serving the old
behaviour. Check `ps` for the process's path, and `curl` one endpoint for a field the change
introduces, before trusting the trace.

If `puml-diff.sh` warns that a `*.genseq.puml` is missing from the work tree but present in
`HEAD`, a suite failed to start and left it deleted — the delta would then report, in your
branch's voice, that this branch removed a diagram. `git checkout --` it and say in the guide
that the suite could not run.

Re-run `puml-diff.sh` afterwards so the regenerated diagrams land in the manifest. A diagram
that did not exist before is shown **plain**, not red. If the generator in
`petclinic-test/src/genseq/` has moved since the base ref, the base side has to be
regenerated too — `reference/sequence-base-regen.md`.

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-sequence)"
```

**Show what generated each diagram.** A sequence diagram is evidence only if the reviewer can
reach the test that produced it. Use a `testpairs` block and the renderer does the pairing
from `MANIFEST.tsv` and the `.puml` chapter dividers — you quote the tests, never the
diagrams.

⚠️ **Do not write prose that names a specific artefact's absence.** *"`add-visit.feature
.genseq.puml` does not exist"* stopped being true while it was being written. In a generated
page, "X is missing" is a fact with a shelf life of minutes: describe the **case**, and let
the renderer say which instances hit it.

## Step 4 — Code City screenshot

Regenerate the city first if the branch moved (`petclinic-backend/docs/generate-codecity.sh`).

```sh
${SKILL}/scripts/steps-ledger.py start city --label "Code City capture" \
  > .human-review/.step-city
LIT=$(${SKILL}/scripts/capture-codecity.sh .human-review/assets/codecity.png highlight)
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-city)"
```

It prints the **measured** change count on stdout — put that number under the image rather
than typing one, because a city that highlighted nothing looks exactly like a city that
highlighted everything. The script copies `codecity.html` in beside the PNG, and that copy
is what the page links to (`assets/codecity/codecity.html`): step 10 serves `.human-review/`
as the document root, so a relative link out to `../petclinic-backend/…` 404s.

## Step 5 — Video of the feature

```sh
${SKILL}/scripts/steps-ledger.py start behaviour --label "feature recording" \
  > .human-review/.step-video
${SKILL}/scripts/record-feature-video.sh .human-review/assets/<feature>.webm
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-video)"
```

The flow being filmed belongs to your project — `reference/feature-script.md` has the script
contract, the screens-to-visit rule and the environment variables.

**The exit code carries the verdict, and 3 is not a failure:** `0` filmed and the feature
held → embed it; `2` no script or the stack is down → skip the section and say why; **`3`
filmed and the feature did *not* hold → embed it and lead the review with what it shows.**
That is the most valuable film this pipeline can make.

⚠️ **Verify the film against frames plus `silencedetect`, never against its own metadata.** A
film can be perfectly in sync with its own cue file — caption, voice and timestamps all
agreeing — and still talk over a loading screen, and no amount of self-consistency will tell
you. Pull a frame at the moment the narration starts and look at what is on it.

## Step 6 — Entry-point complexity increment

```sh
${SKILL}/scripts/steps-ledger.py start complexity --label "entry-point complexity" \
  > .human-review/.step-complexity
cd petclinic-backend && mvn -q test -Dtest=EndpointComplexityExtractorTest
${SKILL}/scripts/endpoint-complexity-delta.py before.json after.json --base $BASE_BRANCH
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-complexity)"
```

Get `before.json` by running the same test at the merge-base, or by reading the committed
JSON from that ref. The delta groups by kind (HTTP → MCP → listeners → jobs) and colours
**green for an increase, red for a decrease** — colour reads as authorship, not judgement.
Every bar explains itself on hover, so do not restate the legend in the lede.

Pass `--base` the real branch name: it defaults to whatever `origin/HEAD` points at, and a
tooltip naming the wrong branch is worse than none. Report `before → after (Δ)` **ranked
inside the full list**. If the baseline predates a widening of what counts as an entry point,
say so — the newly-visible kinds otherwise read as "added by this branch".

⚠️ **Every inlined fragment needs its stylesheet in the content file's `extraCss`.** Forget
one and the tab renders fully populated and completely unstyled. The one exception is
`openapi-compat.py --panel`, which carries its own `<style>`.

## Step 7 — What the REST contract did

```sh
${SKILL}/scripts/steps-ledger.py start api --label "REST contract diff" \
  > .human-review/.step-api
${SKILL}/scripts/openapi-diff.py   --base $BASE --out .human-review/assets/openapi-diff.html
${SKILL}/scripts/openapi-diff.py   --css  >  .human-review/assets/openapi-diff.css
${SKILL}/scripts/openapi-compat.py --base $BASE --out .human-review/assets/openapi-compat.html
${SKILL}/scripts/openapi-compat.py --css  >  .human-review/assets/openapi-compat.css
${SKILL}/scripts/openapi-compat.py --base $BASE --panel \
  --out .human-review/assets/openapi-verdict.html
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-api)"
```

All six lines run together. `--panel` renders the verdict banner the API tab opens with, and
the `swaggerdiff` section pulls it in — skip it and the reader sees whatever stale file
survived on disk, green for a diff it never read. Point `--spec` at your spec if it is not
`openapi.yaml` at the root.

`openapi-diff.py` is *our* reading; `openapi-compat.py` puts a second opinion at the top —
`no_changes` · `compatible` · `incompatible` — sourced from **oasdiff**, which resolves
`$ref`s before it counts.

**The cross-check is the point.** The compat script also asks `openapi-diff.py` for its
verdict and prints whether the two agree. Agreement is a footnote. A **disagreement is the
most review-worthy line on the whole page** — exactly one of them is wrong, so say which
lines caused it rather than quietly showing whichever answer is prettier. Never reconcile it
by editing the prose. Name **`openapi-diff.py`** as the cross-check partner, never
OpenAPITools/openapi-diff, which never runs at all on a machine with `oasdiff` on `PATH`.

It is **not** a gate — plenty of contract breaks are deliberate and agreed.

⚠️ **Say what the classifier cannot see.** It reads the contract, not the handler. A `PUT`
that starts *clearing* a field it was not sent is "an optional field appeared" to any
structural differ alive — so if the automated reviews turned up a semantic break of that
shape, put it in **Look here first**, cross-link it from the contract tab, and say in the tab
that a green seal means the *shape* of the contract holds, not the behaviour behind it. And
the spec is only evidence if it is generated: where `openapi.yaml` is hand-written, this step
diffs an intention rather than an API.

### Step 7b — the same two revisions, read by a third differ

Two embeds on this same tab, not tabs of their own — a fourth reading of the contract on a
fourth surface reads as a fourth *disagreement* rather than corroboration. The gate comes
before the stamp: a ledger record naming a tab the page will not contain is drift the build
has to shout about.

```sh
if command -v openapi-changes >/dev/null 2>&1; then
  ${SKILL}/scripts/steps-ledger.py start api --label "pb33f report" \
    > .human-review/.step-specchanges
  openapi-changes html-report --no-logo --no-explorer \
    --report-file .human-review/assets/openapi-changes.html \
    "$MERGE_BASE:openapi.yaml" ./openapi.yaml
  ${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-specchanges)"
else
  echo "no openapi-changes — embed dropped, and deliberately never stamped"
fi

${SKILL}/scripts/steps-ledger.py start api --label "contract blast radius" \
  > .human-review/.step-api-visual
${SKILL}/scripts/openapi-visual-diff.py --base $MERGE_BASE --spec openapi.yaml \
  --out .human-review/assets/openapi-visual-diff.html
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-api-visual)"
```

pb33f is embedded whole — its own change list, side-by-side diff and document tree.
`openapi-visual-diff.py` renders the new spec in Swagger UI with untouched endpoints faded
and impacted ones lit, so the **shape of the blast radius** is legible before a word is read.
It runs the same `oasdiff changelog` engine as the tab above, so a disagreement there would
be a bug, not a finding.

**An iframe is a fence** — `⌘F` stops at its edge — so both embeds need this page's own words
around them: the finding itself, written out with a full-page link, and **why the numbers
differ from the tab beside them**. A document-level differ reports the two shared schemas
that literally moved; a `$ref`-resolving one reports the eleven operations that serve them.
Neither is wrong, and a reader left to work that out concludes one of the tabs is broken.

## Step 7c — What it will say for itself in production

Same gate-before-stamp rule: no `ast-grep`, no Logging tab, so no ledger record either.

```sh
if command -v ast-grep >/dev/null 2>&1; then
  ${SKILL}/scripts/steps-ledger.py start logging --label "structural logging scan" \
    > .human-review/.step-logging
  ${SKILL}/scripts/logextract.py petclinic-backend \
    --repo . --since $MERGE_BASE --json .human-review/assets/logging.json
  ${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-logging)"
fi
```

It builds a per-file symbol table of identifiers that really are loggers — resolved through
the `extends` chain and enclosing outer classes — and accepts a call only where the receiver
is in that table, so `log.info(x)` is separated from `Math.log(x)`. `System.out` / `err` /
`printStackTrace` are collected **separately** and labelled an anti-pattern, never mixed into
the logger counts.

**The tab is the answer, not the homework.** Do not write a table of touched files; the tab
is the statements themselves, one `.snippet` figure each. **A genuine zero is a finding, not
an empty section** — render it as the sentence *"None. Not one logging statement was added or
changed"*, with the same weight as the snippets it replaces, and never struck through. A scan
that could not run at all is the opposite case: the tab drops, loudly, in the build log.

Each statement carries a verdict from a live model call — ✅ SAFE, 🤔 DOUBT, ❌ PRIVACY — with
its provenance cut from the working tree underneath. The trace is **not narrated, it is
shown**: each hop names a `file:line`, and the page cuts that exact line fresh from the tree,
so the model's claim and the page's evidence cannot disagree. A hop that cannot be resolved
is reported unresolved and forces DOUBT; a hop whose cited line does not check out downgrades
a SAFE the same way. A call that cannot be reached at all renders **⚠️ NOT EVALUATED**, never
folded into DOUBT and never silently SAFE.

Two context registers sit under the finding, both authored, both checked against the
extractor's own counts: **pre-existing logging** and **console output where a logger
belongs**. Cut their snippets with `--exact` and aim the `path:line` link at the
**statement**, not the first line of the window.

## Step 7d — Did the frontend use the design system

```sh
${SKILL}/scripts/steps-ledger.py start dsaudit --label "design-system audit" \
  > .human-review/.step-dsaudit
${SKILL}/scripts/ds-audit.py \
  --base-new http://localhost:4300 --base-old http://localhost:4301 \
  --label-new "$(git rev-parse --abbrev-ref HEAD)" --label-old main \
  --screen "Book a visit=pets/11/visits/add" \
  --screen "Edit a pet=pets/11/edit" \
  --source petclinic-frontend/src \
  --assets .human-review/assets --asset-prefix assets \
  --json .human-review/assets/ds-audit.json \
  -o .human-review/assets/ds-audit.html
${SKILL}/scripts/ds-audit.py --css > .human-review/assets/ds-audit.css
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-dsaudit)"
```

The question no other tab asks: **did the frontend use the standardised components where it
should have?** The defect is an **absence** — a bare `<select>` where the DS combo belongs —
so the audit learns which *roles* the design system covers and flags native controls filling
one of those roles outside any DS component.

Name several screens: *"it flagged the bare one"* is a weak claim next to *"it flagged **only**
the bare one, and called the other three right."* `ds-audit.json` is **the artefact** — a
reviewing agent reads it and never has to OCR a PNG; the HTML is the `includeHtml` fragment
and its `--css` needs an `extraCss` entry.

**A green badge is context; the red one is the finding. An empty registry means no `data-ds`
component was found, which is not a pass.**

⚠️ **Nothing else in this repository can catch this, including the tests that look like they
should.** On the demonstration branch the Playwright spec **passes**, because the DS combo
renders an inner `<select>` carrying the same id — `page.locator('select#vetId')` matches
either implementation. A green suite is not evidence against these findings.

## Step 8 — Who has to approve this

```sh
${SKILL}/scripts/steps-ledger.py start owners --label "codeowners check" \
  > .human-review/.step-owners
${SKILL}/scripts/codeowners-check.py --base $BASE --state
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-owners)"
```

The host enforces `CODEOWNERS` at merge time, which is far too late to help the person doing
the review. This answers in one word — `approval_required` · `no_owners_touched` ·
`no_codeowners` — and renders the owners, which files pulled them in, and which rule claimed
each file.

It is **not a gate**: it says "budget for a second reviewer", never "you did something
wrong". If an owned file was touched *incidentally* (a generated diagram, a formatter sweep),
say so in **Look here first** — the cheapest fix for a blocked merge is often to not touch
the file. Wire it as its own tab right after **Review**.

## Step 9 — Snippets, findings, and the page

```sh
${SKILL}/scripts/steps-ledger.py start guide --label "assemble content.json and build the page" \
  > .human-review/.step-guide
${SKILL}/scripts/test-changes.py --base "$BASE" --out .human-review/assets/test-changes.json
```

`guide` is **not a tab** — it is the one reserved id. Writing this file is normally the most
expensive stretch of the run and the one stretch no shell call can bracket, so it gets a name
instead of disappearing into the residual.

`test-changes.py` classifies every test as new / edited / deleted / untouched, per **test
case** rather than per file. Name it at the top level as `"testChanges":
"assets/test-changes.json"`; without it, a requirement that names tests fails validation.

Never retype code into the guide — reference it:

```sh
${SKILL}/scripts/extract-snippet.py petclinic-backend/.../Visit.java:33-38 --caption "…"
```

The range is **snapped** — it skips a leading blank or comment line and extends to whatever
brace the window opened; `--exact` turns both snaps off. The `--caption` is **HTML**, so a
literal `<` has to be written `&lt;`.

Write `.human-review/content.json` against `reference/content-schema.md`, then build:

```sh
${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
```

The JSON holds only prose + `path:from-to` references + per-diagram notes; the renderer owns
the shell, the CSS, the inlined SVGs and the snippet extraction. Everything the renderer
enforces by itself is listed at the end of the schema file — do not restate it in prose, and
do not type a number the page computes.

### Lay it out as tabs, not as a scroll

A review is five or six separate questions, answered in whatever order the reader's doubt
takes them, so the page is a **tab strip over panels**:

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
  {"id":"requirements","label":"Requirements","blocks":[{"type":"section","id":"requirements"}]},
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

⚠️ **A tab's `id` is a contract with the step ledger; its `label` is not.** The two are joined
by nothing but the string being identical, so relabel freely — *Packages* became *Structure*
with no consequence — but re-key a tab without re-keying its `start` wrap in the same edit and
the step goes on stamping the old id: attribution finds no row, its tokens fall into the
residual, and the tab reports *not measured*. **The stamp follows the work, not the surface**
— a tab that takes the work with it takes the stamp too; a tab that only re-presents work
already done gets none. Adding a tab is fine (it reports *not measured* until a step stamps
it) and so is dropping one.

| `id` | label on the reference page | fed by | handle file |
| --- | --- | --- | --- |
| `review` | 🤖 Review | Step 1 | `.step-review` |
| `behaviour` | Demo | Step 5 | `.step-video` |
| `sequence` | Sequence | Step 3 | `.step-sequence` |
| `requirements` | Requirements | *no step yet* | — |
| `data`, `packages` | Data, Structure | Step 2 | `.step-data` |
| `api` | API | Step 7, its visual diff, and 7b | `.step-api`, `.step-api-visual`, `.step-specchanges` |
| `city` | Code City | Step 4 | `.step-city` |
| `complexity` | Complexity | Step 6 | `.step-complexity` |
| `logging` | Logging | Step 7c | `.step-logging` |
| `dsaudit` | UX | Step 7d | `.step-dsaudit` |
| `owners` | CODEOWNERS | Step 8 | `.step-owners` |
| `guide` | *(not a tab)* | Step 9 | `.step-guide` |

Two steps can feed one tab, and one step can feed two — but a step naming two tabs has its
cost **split evenly**, so never widen a wrap to a tab that did none of the work.

Default order, worth departing from only with a reason — **Overview, 🤖 Review, Demo,
Sequence, Requirements, Data, Structure, API, Code City, Complexity, Logging, UX,
CODEOWNERS** — with the panels in the same DOM order as the strip. Four tabs need something
said about how they are written:

- **🤖 Review** — **one list**: the open calls first, most critical first, then the fixes
  already applied, numbered straight through and greyed out. Two lists that both start at 1
  make the reader do arithmetic. Its `intro` must state that `/code-review` and `/simplify`
  both ran, and in which order.
- **Requirements** — lead with **what kinds of test the change set offers as acceptance
  evidence**, as cards (`<div class="evidence">` holding one `<section class="evi
  e2e|api|unit">` per level), not a run of prose with bold lead-ins. A level **nothing**
  covers keeps its card and says so in `class="evi none"` — "there is no unit test at this
  level" is a finding, and a paragraph nobody wrote looks identical to one nobody thought to
  write. Each requirement carries a `tests` list beneath its own text; **you attach, the diff
  classifies**. Say `new` and `modified` apart: a new test is evidence the requirement was
  pinned, an edited one is evidence a pin moved and is worth reading for what it stopped
  asserting.
- **Data** — the DB and domain deltas, and 2–5 core-logic bullets in domain language, each
  backed by a snippet.
- **UX** — the only tab whose finding is an absence, and the only one no other check in the
  repository can produce.

### Close the ledger, check it, then build

In this order, and only in this order:

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-guide)"
${SKILL}/scripts/steps-ledger.py check          # exits non-zero on a renamed tab
${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
```

`end` before `check`, so the guide record is closed when the check reads it. `check` before
the build, so a `DRIFT:` line is something you can still act on — once the page is written,
a renamed tab is a column of blanks nobody can distinguish from a step never instrumented.

## Step 10 — Hand the guide over, then the app

**Never `open .human-review/review.html`**: that hands the page to whatever the OS thinks
owns `.html`, on another desktop, while the terminal that built it sits inside an editor.

```sh
URL=$(${SKILL}/scripts/serve-review.py .human-review)     # http://127.0.0.1:7654/review.html
```

A loopback static server on a fixed port, detached, idle-reaped after four hours; a second
run reuses it. It is what makes the guide *addressable* — both of VS Code's embedded browsers
refuse `file://`.

⚠️ **That reuse bites the moment you edit `serve-review.py` itself.** A second start prints
`:7654 already serves … — using it` and hands back the old listener, still running the *old*
code, so a route you just added answers 404 while the file on disk plainly contains it.
`serve-review.py .human-review --stop` and start it again; reading the source harder will not
help, because the source is not what is answering.

Then open it where the reader already is. With `$TERM_PROGRAM = vscode` and
[victor-vsc](https://github.com/victorrentea/victor-vsc)'s bridge available, run
`open-in-browser.py "$URL"` — it matches by workspace **folder, not focus**, which matters
because the window a human is watching is usually not the one an agent is working in.
Otherwise print the URL and say to ⌘-click it.

Either way **print `$URL` as the last line of the run** — the reader closes the panel and
wants it back an hour later.

Then make sure the app is running from **this** checkout and open the screen the change
affects, so the human can exercise it. Seed extra rows if the sample data is too thin.
Finally start `/relay` so they can dictate UX tweaks straight into the session.

## Wrap-up

`.human-review/` is a throwaway artifact — remind the human to delete it rather than commit
it (`serve-review.py --stop` first). Print the path and the URL, and list what you fixed vs
what you left for them. Do not commit or push.
