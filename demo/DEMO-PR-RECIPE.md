# The demo PR: how it was produced, and how to redo it

This file is about **one exhibit**: `victorrentea/petclinic` PR #49 (ticket
[#37 "Link Visit with Vet"](https://github.com/victorrentea/petclinic/issues/37),
branch `test-pr`, base `main`), served on Pages as the
[`petclinic-visit-vet`](https://victorrentea.github.io/human-review/petclinic-visit-vet/review.html)
snapshot. It exists to demonstrate `/human-review`, so every tab of the page has
to have something worth looking at. `/implement-ticket` alone did not get there —
after the agent finished, Victor made a handful of **deliberate retouches** on
top of it, each aimed at one tab. This file is the list, so they can be redone
if the implementation and/or the review step is ever re-run.

It lives in `demo/`, next to `index.html` and the `petclinic-visit-vet/`
snapshot it describes, rather than at the repo root: it is documentation *about*
that one published exhibit, not about the skill in general. It is safe here —
`.github/workflows/demo-zip.yml` only zips directories under `demo/` that carry
a `review.html` (`for snap in demo/*/`), so a loose file at `demo/`'s own root is
never swept into a release; `pages.yml` does publish it verbatim to Pages, which
is fine, since it holds nothing private.

## Step 0 — produce the PR

`/implement-ticket`, per `skills/implement-ticket/SKILL.md`, unattended and on Opus:

```sh
cd ~/workspace/petclinic-pr        # test-pr, tracking origin/main
git checkout -b test-pr            # only if starting fresh from main
claude -p "$(cat skills/implement-ticket/prompt.md)

Ticket: victorrentea/petclinic#37 — read it with: gh issue view 37" \
  --model opus --permission-mode acceptEdits --output-format json
git push origin HEAD:test-pr       # never a bare `git push` on this checkout
```

That produced the two commits the skill always produces, both trailered
`Claude-Session: 47d2ba31-61ef-4ef6-ac2f-7d969121ce34`:

| commit | what |
| --- | --- |
| `a8cd9973` | `feat(visits): link a visit to the vet that attended it` — the feature alone |
| `ce56d912` | `fix(visits): let owner admins read the vet list, and stop over-fetching` — the accepted `/code-review high` findings, plus `review-points.md` (`Review-Points: review-points.md`, `Implements: a8cd9973`) |

Everything below is what happened **after** `ce56d912` — the aftermath band the
Review tab shows in red. Some of it is Victor's retouches (this recipe); the
rest is infrastructure cherry-picked from `main` per the rule in this repo's own
`CLAUDE.md` ("infrastructure changes are made on `main` first, then pulled into
the PR branch"), which the aftermath band still shows in red because the band
doesn't know the difference — that's *why* it needs this file.

To tell the two apart on a fresh checkout:

```sh
git -C ~/workspace/petclinic-pr log --format='%h %ad %s' --date=short origin/main..test-pr
```

then for each commit, `git diff <sha>^..<sha>` — if the same diff exists on a
`main` commit (`git log --oneline --all --grep="<subject>" -i`), it's a tooling
cherry-pick, not a retouch.

## The retouches

Each row: the tab it lights up, what was done, where, the commit on `test-pr`
(hashes as of 2026-09-19 — re-run the log command above if the branch moved),
and how to redo it.

### 1 — Logging tab: two log statements

Two lines added to `OwnerRestController.bookVisit`, one `log.info` carrying a
number (the visit id), one `log.debug` carrying a string (the vet's name) — so
the Logging tab has both severities and both value shapes to classify.

- **commit**: `9ce98ab9` — "Log the booked visit id and the attending vet"
- **file**: `petclinic-backend/src/main/java/victor/training/petclinic/rest/OwnerRestController.java`
- **redo**: add a `Logger log` field to the controller, then after
  `visitRepository.save(visit)` in `bookVisit`:
  ```java
  log.info("Booked visit {} for pet {}", visit.getId(), petId);
  if (vet != null) {
      log.debug("Attending vet: {}", vet.getLastName());
  }
  ```

### 2 — Tests tab (requirements↔tests matrix): one row left red on purpose

The ticket says "Clearing the field must persist as empty." The implementation
already satisfies it (see `a8cd9973`'s message: the controller assigns the vet
from the request unconditionally, on purpose), but the tests that proved it were
deliberately dropped, so the matrix shows one requirement with no green test
under it.

- **commit**: `6aac04b4` — "Drop the tests that proved clearing the vet
  persists, to show an unproven requirement"
- **files**: `petclinic-backend/src/test/java/victor/training/petclinic/rest/VisitTest.java`,
  `petclinic-frontend/.../visits/visit-edit/visit-edit.component.spec.ts`
- **redo**: remove the assertion(s) in those two files that book/edit a visit
  with a vet, then clear the vet field and assert it comes back `null`/empty.
  After dropping them, **re-run the model step** (`Rerun + AI` on the served
  page, or `rerun-model.py` — see `skills/human-review/SKILL.md`, "The matrix,
  and the model that writes it") so `assets/requirements-map.html` re-reads the
  test suite and the row actually turns red — the matrix is model-written and
  does not update on its own when a `--steps static` refresh runs.

### 3 — Sequence + Tests tabs: a Gherkin UI scenario, and diagrams re-recorded

A browser-level Cucumber (Gherkin) + Playwright scenario, so Tests gets one row
tagged 🕵️ (Gherkin) *and* 📺 (Playwright) instead of only a TypeScript DSL spec,
and Sequence gets a UI · Gherkin pairing alongside the UI · Playwright one that
was already there.

- **commits**:
  - `21680c35` — "Record the sequence diagrams of the vet-linking tests" (first
    pass — the committed `.genseq.puml`/`.json` pairs were still byte-identical
    to `origin/main`, i.e. drawing the *reverted* implementation, because the
    reimplementation never re-ran the traced suites)
  - `0bb6f069` — "test: Gherkin UI scenario for the vet-linking story" — adds
    `petclinic-test/src/book-visit-with-vet.feature` + `book-visit-with-vet.feature.glue.ts`,
    tagged `@generate_sequence`, plus a `VisitsPage` locator for the new Vet
    column on `/visits`
- **redo**: write the `.feature` + glue over the same DSL `add-visit.spec.ts`
  uses (same clicks, same selectors, same waits), tag it `@generate_sequence`,
  then re-run the traced suites (Playwright, Cucumber, the backend's
  `@GenerateSequence` suite through Tempo — "the documented way", per
  `21680c35`'s message) and commit the regenerated `.genseq.puml`/`.json` pairs.

### 4 — Sequence tab: the SMS gateway as a real HTTP hop (in progress)

**Precondition, not a retouch**: the Notification module and the SMS gateway
already exist as two separate lifelines in the generated sequence diagrams
because of `5dcf16c4` ("Notification module, drawn as its own participant in
the sequence diagrams") — that landed on `main` before this branch's diff
starts, so it never shows in `origin/main..test-pr` and there is nothing to
redo for it; it just has to already be on `main`.

What *is* on the branch, and still marked "in progress" as of 2026-09-19: making
the SMS send a real HTTP call instead of a same-JVM method call, so the arrow
between the two lifelines crosses an actual socket instead of being asserted by
a `@WithSpan` on a private method.

- `4aea32fc` — "Send the fake SMS over HTTP, so the trace crosses a real
  socket" — **this one is a tooling cherry-pick from `main`** (`fff6bd15`,
  identical diff), not a branch-only retouch; it's listed here because it's the
  commit that makes the rest of this item possible, not because it needs
  redoing separately
- `abf1c951` — "Give `VisitTest` a server to post the fake SMS to" — branch-only:
  `VisitTest` (booking through MockMvc) needs `webEnvironment = RANDOM_PORT` too,
  the same move `4aea32fc` already made for `AddVisitApiTest`/`OwnerCreateTest`
  on `main`, because this test class doesn't exist on `main` in this shape
- `e9e23b6a` — "Record the sequence diagrams with the SMS gateway self-call" —
  branch-only: re-runs the three tagged suites against a freshly seeded
  database and commits the diagrams that now show
  `Notification module -> SMS gateway : POST /api/fake-sms` as two real arrows
- **redo**: first make sure `4aea32fc`'s change (or its equivalent) is cherry-picked
  from `main`, then add the `webEnvironment = RANDOM_PORT` twin for whatever
  branch-only test class books a visit through MockMvc, then re-run the traced
  suites and commit the regenerated diagrams.

### 5 — Review tab: the aftermath band itself

No commit of its own — this is the fact that items 1–4 (and the tooling
cherry-picks below) all land *after* `ce56d912`, the review commit. That's what
populates the aftermath band: red for the hand-written commits (code a human
added after the agent's review), grey for the ones `human-review.json`'s
`"generated"` globs classify as regenerated artifacts (diagrams, Code City).
Nothing to redo here beyond making sure the retouches keep landing after, not
before, the review commit.

### 6 — one more retouch, not named above

- **commit**: `7cad86b1` — "test(visits): click the vet through the browser,
  not just past the API" (2026-09-18 17:16, the first retouch chronologically,
  before the Gherkin/logging/tests ones above)
- **what**: the branch had no end-to-end test of the attending vet at all — the
  REST tests prove the API stores it, the Angular specs prove the form sends
  it, nothing joined the two through a browser. Restores the Playwright
  scenario over `add-visit.dsl.ts`/`add-visit.spec.ts` (the stale
  `add-visit.spec.ts.add-a-visit-attended-by-a-vet.genseq.puml` was still on the
  branch, titled with a link to a scenario that no longer existed). Also
  regenerated `petclinic-backend/docs/ConceptualModel.drawio.png` as a
  byproduct of the commit.
- **files**: `petclinic-test/src/add-visit.dsl.ts`, `petclinic-test/src/add-visit.spec.ts`,
  `petclinic-backend/docs/ConceptualModel.drawio.png`
- **redo**: add the Playwright scenario back, matching the DSL clicks/selectors
  the rest of `add-visit.spec.ts` already uses; regenerate the draw.io PNG if it
  drifted.

`31297ed4` — "chore(codecity): commit the regenerated city the guardrail asks
for" (2026-09-18 17:19) — **not a retouch either**, just bookkeeping: the
pre-push guardrail regenerates Code City and diffs it against the committed
copy, and refuses the push until the artifact catches up with the branch's own
change (it still listed `Visit.java`/`VisitMapper.java`/the three controllers as
`changed: false` and knew nothing of the notification package). Nothing to redo
by hand — it's what `git push` forces the next time the guardrail runs against a
stale `codecity.html`.

## Tooling cherry-picks, not part of the recipe

These commits also land after `ce56d912`, on `test-pr`, but each is a byte-for-byte
cherry-pick of a commit already on `main` (verified with `git diff <sha>^..<sha>`
on both sides) — infrastructure per this repo's `CLAUDE.md` rule, not something
Victor added for the demo. Nothing here needs to be reproduced on the branch by
hand; it arrives by cherry-picking whatever `main` has at the time.

| `test-pr` commit | `main` commit | subject |
| --- | --- | --- |
| `753f724c` | `d13449ea` | Let the review run start the stack its film is recorded against |
| `f96259dc` | `71b7cb2e` | Require one Gherkin scenario through the UI per feature |
| `2f5947ea` | `3a504b69` | Revert "Require one Gherkin scenario through the UI per feature" |
| `619902c3` | `2265f333` | pre-commit: spotless only on the staged files |
| `4aea32fc` | `fff6bd15` | Send the fake SMS over HTTP, so the trace crosses a real socket |
| `b0203286` | `bddf3e4f` | human-review: let the design-system audit start its own two builds |

(`f96259dc`/`2f5947ea` is a guardrail that was tried and reverted on `main`
itself — both halves rode over to `test-pr` together, which is why the pair
shows up here rather than as a retouch.)

## If you redo the implementation

Order matters — each step depends on the branch state the previous one left:

1. `/implement-ticket` → the two commits (feature, then review fixes +
   `review-points.md`).
2. Nothing else to write by hand for the review-points file — it's the coding
   session's own record.
3. Cherry-pick whatever tooling `main` carries at that point (the table above,
   or its current equivalents — check with the `git log --grep` recipe in
   Step 0).
4. Apply the retouches in this file, items 1–4 and 6, in roughly the order
   listed (Sequence/Tests before Logging/red-matrix is how it happened, but the
   only real constraint is: all of them after the review commit).
5. Re-run the model pass (`Rerun + AI` / `rerun-model.py`) so the requirements
   matrix picks up the dropped tests from item 2.
6. Regenerate: Code City (`git push` will force this if it's stale), the
   sequence diagrams (already covered per-item above), and the draw.io PNG if
   it drifted.

## If you redo only the review step

Re-running `/human-review` (or `/implement-ticket`'s review half) does **not**
require redoing any of the above. Reset the branch to `ce56d912` (the review
commit itself) or leave it where it is — either is fine, since `/human-review`
only reads `review-points.md`, the trailers, and the diff; it does not write
code. What to keep:

- **Keep**: `review-points.md`, the `Review-Points:`/`Implements:` trailers on
  `ce56d912`, and everything in the "Tooling cherry-picks" table above — none
  of that is Victor's retouch, all of it is either the agent's own record or
  infrastructure `main` still carries.
- **Reset** (only if the goal is specifically to re-demo the *implementation*
  step, not the review step): the branch to `a8cd9973`, drop `ce56d912` onward,
  and start over from `/code-review high` against `a8cd9973`.
- **Redo**: only the retouches in this file — they are what makes each tab of
  the review page have something to show, and they are the one part of the
  branch's history that `/implement-ticket` and `/human-review` never produce
  on their own.
