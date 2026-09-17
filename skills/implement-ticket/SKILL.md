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
* which reading of an ambiguous ticket it **chose**, and which reading it did not take.

Both live in a conversation and die with it. `review-points.md` is where they are written
down instead, at the repository root, committed with the fixes — so a reviewer sees the
artifact arrive in the PR's own file list rather than taking a generated page's word for
it. The page then renders it; it does not invent it.

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
