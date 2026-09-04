---
name: human-review
description: Assemble .human-review/review.html — a tabbed reviewer's guide for a change set — from review passes that have ALREADY run in this conversation (/code-review, /simplify, or your own), plus diagram deltas, a Code City shot, a feature video, the endpoint-complexity increment, the REST contract diff and deep-linked snippets. Explicit invocation only — user types /human-review.
disable-model-invocation: true
---

# /human-review — assemble a review.html for a human

**This skill does not review the code. It writes up a review that already happened.**

The passes that find things — `/code-review`, `/simplify`, a security pass, your own
adversarial multi-agent review — are the human's to run, when *they* think they are done.
This skill harvests what they produced, runs the deterministic evidence-gatherers around it,
and assembles one page. Almost all of that is scripts; your job is the judgement and the
prose. Do **not** commit or push.

Resolve the skill's own directory once — a plugin install, an env override, a project
symlink and a gitignored CI clone are all real layouts:

```sh
for candidate in "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/skills/human-review" \
    "${HUMAN_REVIEW_HOME:-/nonexistent}" \
    "$(readlink -f .claude/skills/human-review 2>/dev/null)" ".claude/skills/human-review"; do
  [ -x "$candidate/scripts/run-steps.py" ] && { SKILL="$candidate"; break; }
done
[ -n "${SKILL:-}" ] || { echo "cannot locate the human-review skill"; exit 1; }
```

Run everything from the repository root. Project-specific commands live in
`human-review.json` there, not in this file — `human-review.example.json` is the template.

## Step 1 — What review already ran? (do this before anything destructive)

```sh
${SKILL}/scripts/review-passes.py --require --extract .human-review/passes
```

**Exit 3 means nothing has been reviewed, and you stop here.** Do not run `/code-review` or
`/simplify` yourself to fill the gap. Tell the human, in your own words:

> I can't write up a review that hasn't happened. I don't see `/code-review`, `/simplify` or
> any other review pass in this conversation. Run the ones you want — `/code-review`,
> `/simplify`, your own adversarial pass, in any combination — and call `/human-review`
> again. **Or say the word and I'll run them for you now.**

Then wait. If they say yes, run the passes they name, in separate turns, and start again at
Step 1 — the page is built from a transcript, so the passes have to be *in* it.

Otherwise read the files it extracted into `.human-review/passes/`. Those are your findings.
Each carries the pass it came from; that becomes the item's `"source"`, and it is the only
honest way to fill that field — provenance exists only where the pass actually ran.

⚠️ **Never re-run a pass that already ran.** Two runs over the same diff word and rank their
findings differently, so a second invocation does not confirm the first — it produces a
*different* review at full price, and whichever ran last wins. The findings on this page are
the ones the human watched happen.

## Step 2 — Resolve the change set, then gate and wipe

From `$ARGUMENTS`: **empty** → uncommitted work, `BASE=HEAD`. **A ref / range / SHA** →
`BASE=<the older ref>`. **A PR** (`#123`, a github URL) → `gh pr checkout <n>`, and
`BASE=$(gh pr view <n> --json baseRefName -q .baseRefName)`. Default `origin/main`. Print a
one-line scope banner; an empty change set is "Nothing to review." and stop.

```sh
${SKILL}/scripts/preflight.py --base "$BASE"
```

It pushes, waits for CI **on the pushed commit**, and only then wipes `assets/`, resets the
ledger and writes the run's start markers. Exit 1 means the branch is not proven and nothing
was destroyed — report which workflow failed and stop. Whatever it prints on its last line
about the gate goes in the guide verbatim; *"no build proved this"* must never read as a pass.

If `.human-review/review.html` already exists, ask what changed first — see *Iterating*.

## Step 3 — Produce the evidence

```sh
${SKILL}/scripts/run-steps.py --base "$BASE"
```

One command runs every deterministic producer — diagram deltas, sequence diagrams, Code
City, the feature film, complexity, the REST contract and its two second opinions, the
logging scan, the design-system audit, code owners, the test manifest — each gated on its
own prerequisite, each ledger-wrapped, none of them able to skip its `end`.

Read the status table it prints. Three things in it are yours:

- **`skipped`** — a missing optional binary or an unconfigured step. Its tab is dropped and
  named under the strip. That is honest; do not work around it.
- **`failed`** — say so in the guide. A failed producer is a fact about the run.
- **`note`** — things only the run knows: the measured Code City count to put under the
  image, an amber `PARTIAL LIST` seal that is *correct*, a suite that could not start, and
  above all **video exit 3 — filmed, and the feature did not hold.** Lead the whole review
  with that one.

## Step 4 — Write the judgement

This is the only step that is yours, and it is why a model is here at all.

```sh
${SKILL}/scripts/steps-ledger.py start guide --label "assemble content.json and build the page" \
  > .human-review/.step-guide
```

Split every harvested finding in two, and say out loud which pile each landed in:

- **Non-disputable → fix it now.** One obvious right answer, no behaviour change, no product
  call: a duplicated helper, a test that passes vacuously, a shared persistence context
  hiding a missing `save()`, an uninitialised model field, a positional selector, a dead
  import. Fix, then re-run the affected tests. These become **Already fixed for you**.
- **Disputable → hand it to the human.** Anything that changes an API contract, a migration
  already applied somewhere, a data-integrity trade-off, a performance/correctness tension,
  or where two reasonable engineers would pick differently. These become **Look here first**,
  most critical first.

Never argue a finding away silently. If you skip one, it goes in the list with a reason.

**The writing rule: show the code, do not narrate it.** A finding is not a story about a
defect, it is the defect, quoted.

- **Two or three sentences of prose, hard ceiling.** `title` says what is wrong; `body` says
  what breaks and under which input; `why` says what the human has to decide. Past that, the
  reviewer is reading your reasoning instead of their code.
- **Then the code, and most of the item is code.** Prefer several short captioned snippets
  over one long one; a caption names the *one thing* in the lines (`"the null branch that
  never runs"`), not the block.
- **An applied fix shows its diff.** A fix described in a sentence with no diff is a claim
  the reader has to take on trust; the build warns when it finds one.
- **Never retype code** — `extract-snippet.py path:from-to` cuts it verbatim at build time.
- **Never type a number the page computes** (the cost chip, the auto-fixed count, the Code
  City count, tab costs). A hand-typed number goes stale with nothing noticing.
- **Never name a specific artefact's absence.** *"`add-visit.genseq.puml` does not exist"*
  stopped being true while it was being written. Describe the **case**; let the renderer say
  which instances hit it.

Write `.human-review/content.json` against **`reference/content-schema.md`** — every shape,
every block type, the tab vocabulary, and everything the renderer already enforces so you do
not restate it.

Then commit whatever was already in the working tree, and **leave your own fixes
uncommitted**: their whole value is that `git diff` shows exactly what an agent touched. Do
not comment your decisions into the code — that belongs in **Look here first**.

## Step 5 — Close, check, build, serve

In this order, and only this order:

```sh
${SKILL}/scripts/steps-ledger.py end "$(cat .human-review/.step-guide)"
${SKILL}/scripts/steps-ledger.py check          # exits non-zero on a renamed tab
${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
URL=$(${SKILL}/scripts/serve-review.py .human-review)
```

`end` before `check`, so the guide record is closed when the check reads it. `check` before
the build, so a `DRIFT:` line is still actionable — once the page is written, a renamed tab
is a column of blanks nobody can tell from a step never instrumented.

**Never `open review.html`** — that hands it to whatever the OS thinks owns `.html`, on
another desktop. With `$TERM_PROGRAM = vscode` and
[victor-vsc](https://github.com/victorrentea/victor-vsc)'s bridge, `open-in-browser.py "$URL"`
opens it beside the code (it matches by workspace **folder, not focus**). Otherwise print the
URL to ⌘-click.

**Print `$URL` as the last line of the run.** Then make sure the app runs from *this*
checkout, open the screen the change affects, and start `/relay` so they can dictate tweaks.

## Iterating on a review that already exists

Most invocations after the first are not reviews — the page is on disk and what is wanted is
a change to the *page*. **Has the code changed since the page was built?** New commits, a
force-push, a finding fixed → re-review, start at Step 1. Only the report changed →
iteration, and an iteration runs almost nothing:

1. Edit the keys in `content.json` the request names, and nothing else.
2. Re-run only the producers that read files you touched: `run-steps.py --only <step>`.
3. Rebuild. The server is sticky, so print the same URL again.

**Do not re-run Step 1's passes, do not `steps-ledger.py reset`, do not rewrite `.started`,
do not wipe `assets/`, and do not open ledger records for the iteration's own edits.** They
belong to the run they timed. In a *new session* the cost cannot be recomputed and will not
say so, so pin the build to the session that did the work:

```sh
CLAUDE_CODE_SESSION_ID=$(cat .human-review/.session) \
  ${SKILL}/scripts/build-review-html.py .human-review/content.json --out .human-review/review.html
```

If that id is gone, publish no number rather than a measured-looking one: delete
`.human-review/.steps.json` and drop `{"auto": "cost"}` from `scope`.

## Wrap-up

`.human-review/` is a throwaway artifact — remind the human to delete it rather than commit
it (`serve-review.py --stop` first). Print the path and the URL, and list what you fixed vs
what you left for them. Do not commit or push.
