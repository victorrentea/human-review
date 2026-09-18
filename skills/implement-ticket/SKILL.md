---
name: implement-ticket
description: Implement a ticket, review your own work with /code-review, and record what you fixed, declined and assumed in a committed review-points.md. Explicit invocation only — user types /implement-ticket <ticket>.
disable-model-invocation: true
---

# /implement-ticket — build it, review it, write down what you decided

This is the **other end** of `/human-review`. That skill writes up a review that already
happened; this one is what happens. It exists because the most valuable thing a coding
agent knows about its own change is the part that never reaches the diff:

* which review findings it accepted, and which it read and **declined** — and why;
* which reading of an ambiguous ticket it **chose**, which reading it did not take, and
  **how sure it is** that it chose right.

That last number — `confidence:`, in `[0, 1]` — is the cheapest thing on the page and the
one nothing else can supply: a reviewer with an hour and eleven assumptions needs to know
which three were close calls, and only the agent that made them knows. It is honest or it
is noise, which is why `prompt.md` says so in as many words: *0.9 on everything is a lie
the page will show.* The scale is fixed so the numbers mean the same thing across runs —
`1.0` the ticket left no other reading, `0.5` a coin flip, below `0.3` expecting to be
corrected — and it is refused outside that range rather than clamped.

All of it lives in a conversation and dies with it. `review-points.md` is where it is
written down instead, at the repository root, committed with the fixes — so a reviewer
sees the artifact arrive in the PR's own file list rather than taking a generated page's
word for it. The page then renders it; it does not invent it.

## One text, two entry points

The instructions are in `prompt.md` beside this file, so the interactive and the unattended
run cannot drift:

```sh
for c in "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/skills/implement-ticket" \
         "${HUMAN_REVIEW_HOME:-/nonexistent}/../implement-ticket" \
         "$(readlink -f .claude/skills/implement-ticket 2>/dev/null)" \
         "$HOME/workspace/human-review/skills/implement-ticket"; do
  [ -f "$c/prompt.md" ] && { PROMPT="$c/prompt.md"; break; }
done
```

**Interactively** — `/implement-ticket #37`: read `$PROMPT` and follow it, with
`$ARGUMENTS` as the ticket. **Unattended**:

```sh
claude -p "$(cat "$PROMPT")

Ticket: victorrentea/petclinic#37 — read it with: gh issue view 37" \
  --model opus --permission-mode acceptEdits --output-format json
```

`acceptEdits`, not `bypassPermissions`: a repo's own `deny` rules (petclinic refuses a
hand-edited `openapi.yaml`) are part of what the run is supposed to prove it can work
inside. If a run had to fall back to `bypassPermissions`, that changes what it
demonstrates and belongs on the page.

`--output-format json` puts `session_id` in the result envelope, but the durable copy is
the trailer — read it back with:

```sh
git log -1 --format='%(trailers:key=Claude-Session,valueonly)'
```

## What the flow downstream depends on

Three things, and nothing else. Each is read by a script, so getting one wrong is a silent
loss of exactly one row on the page:

| what | read by | if it is missing |
| --- | --- | --- |
| `review-points.md` at the repo root | `scripts/review-points.py` | the Review tab has no Fixed / Ignored / Assumptions piles, and says so rather than rendering "nothing outstanding" |
| `Review-Points:` + `Implements:` trailers | `scripts/review-commits.py` | which commit is the implementation and which is the review is guessed from the file's history, or not at all |
| `Claude-Session:` on both commits | `scripts/session-cost.py` | the phase costs fall back to `.human-review/.session`, which is gitignored and dies with the directory |

**The trailers are the last lines the agent writes, and the harness writes after them.**
Claude Code appends `Co-Authored-By: Claude …` as a paragraph of its own, which puts the
three keys in the *penultimate* paragraph — and git's `%(trailers:key=…)` only parses the
last one, so it returns empty for all three. The first real run of this flow produced two
correctly trailered commits that the page reported as "not recorded" for exactly that
reason. `review-commits.py` therefore reads the keys off any line of `%B`, with
`%(trailers)` as the first pass only. Nothing is asked of the agent beyond one key per
line: a paragraph landing after them is expected and harmless.

**Two commits, not one and not three.** The first is the feature alone; the second is the
accepted fixes plus `review-points.md`. That split is what makes "what did reviewing cost,
against writing it" a measurement instead of an estimate — the two commits are the only
timestamps in the whole flow that a rebase cannot move without also moving the work.

## Which `/code-review`

The **harness built-in** (effort levels `low`…`ultra`, `--fix`, `--comment`) — not the
`code-review:code-review` plugin, which needs a real GitHub PR and ends by posting a
`gh pr comment`.

`--fix` is refused deliberately. It applies the findings to the working tree, which
destroys the accept/decline information this whole flow is built to capture: afterwards
there is a diff, and no record of which of its lines the agent would have argued with.

Never re-run a pass to "confirm" a finding. Two runs over the same diff word and rank
their findings differently, so a second invocation does not confirm the first — it produces
a different review at full price, and whichever ran last wins.

## The demo PR is this flow plus hand retouches

`petclinic`'s `test-pr` branch runs this flow, then has a handful of deliberate retouches
on top — one per review-page tab, so the demo has something to show everywhere.
`demo/DEMO-PR-RECIPE.md` lists them, with the commit each one landed in and how to redo it.
