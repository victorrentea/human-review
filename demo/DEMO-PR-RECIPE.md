# The demo PR: how it was produced, and how to redo it

This file is about **one exhibit**: `victorrentea/petclinic` PR #49 (ticket
[#37 "Link Visit with Vet"](https://github.com/victorrentea/petclinic/issues/37),
branch `test-pr`, base `main`), served on Pages as the
[`demo`](https://victorrentea.github.io/human-review/demo/review.html)
snapshot. It exists to demonstrate `/human-review`, so every tab of the page has
to have something worth looking at. `/implement-ticket` alone did not get there —
after the agent finished, Victor made a handful of **deliberate retouches** on
top of it, each aimed at one tab. This file is the list, so they can be redone
if the implementation and/or the review step is ever re-run.

It lives in `demo/`, next to `index.html` and the `demo/`
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
the PR branch"). The aftermath band used to show all of it in red, because it
could not tell the two apart; it now asks `git` per commit and folds the
cherry-picks into one grey row, so what stays red is the retouches this file
describes.

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

### 4 — Sequence tab: the SMS arrow, the HTTP hop that was reverted, and the JDBC noise

**Precondition, not a retouch**: the Notification module and the SMS gateway
already exist as two separate lifelines in the generated sequence diagrams
because of `5dcf16c4` ("Notification module, drawn as its own participant in
the sequence diagrams") — that landed on `main` before this branch's diff
starts, so it never shows in `origin/main..test-pr` and there is nothing to
redo for it; it just has to already be on `main`.

**Where it ended up**: `"Notification module" -> "SMS gateway"` is a plain
in-process arrow, drawn from the `send-sms` span — one `@WithSpan` and one
`genseq.participant` attribute on `FakeSmsNotificationSender.sendSms`, linking
back into that method. That costs two annotations and leaves the application
exactly as it was. **Nothing to redo here**: it is how `main` already is.

#### The detour, and why it is not in the recipe

For a day the branch made that send a real HTTP round-trip, so the arrow would
cross an actual socket instead of being asserted by a `@WithSpan` on a private
method. It was then **reverted**, and the revert is the decision worth
recording: *no production code for test instrumentation.* Drawing the arrow the
other way cost a `FakeSmsGatewayController` endpoint, a `RestClient` in the
sender, a `Lifeline` bean learning the bound port from
`WebServerInitializedEvent`, the route repeated as a literal in
`BasicAuthenticationConfig.permitAll`, and three `@SpringBootTest`s moved to
`RANDOM_PORT` so there was a server to post to — an endpoint, a security
exception and a lifecycle listener whose only reader is a picture. The picture
was already honest without them.

Both halves are on the branch, in order, which is why the log reads the way it
does:

| commit | | what |
| --- | --- | --- |
| `4aea32fc` | pick of `fff6bd15` | "Send the fake SMS over HTTP, so the trace crosses a real socket" — the hop itself, plus `AddVisitApiTest`/`OwnerCreateTest` on `RANDOM_PORT` |
| `abf1c951` | branch-only | "Give `VisitTest` a server to post the fake SMS to" — the same move for the one test class that does not exist on `main` in this shape |
| `e9e23b6a` | branch-only | "Record the sequence diagrams with the SMS gateway self-call" — the pictures showing `POST /api/fake-sms` and a `200` coming back |
| `d0d57219` | pick of `b5606c1b` | "Send the fake SMS in-process again: keep the test instrumentation out of production code" — the revert |
| `0a3541e5` | branch-only | "`VisitTest` goes back to a plain `@SpringBootTest`, with no server to post to" — the tail of the revert |

Two things from the HTTP commit **stayed**, because neither depends on the hop:
a crossing arrow with nothing to reveal now links to the method it was opened on
(`Backend -> "Notification module": notify-visit-booked` was the only arrow in
the picture with no way back to the code — PlantUML gives a message label exactly
one link, and a ⊕ arrow already spends it), and its test. What went back with the
revert is the rule that an HTTP CLIENT span stays on its caller's lifeline: it
existed only for the self-call, and nothing else in these traces has both ends of
one HTTP call under the same `service.name`.

**Redo**: don't. If the branch is rebuilt from scratch, skip the hop entirely —
`4aea32fc`, `abf1c951`, `e9e23b6a`, `d0d57219` and `0a3541e5` cancel out to
nothing, and the arrow is already drawn by the `send-sms` span `main` carries.

#### The JDBC arrows that named no call, and the final redraw

The same diagrams were also carrying six arrows that said nothing. The OTel agent
times every call *into* the JDBC driver, not only the ones that run SQL — borrowing
a pooled connection, validating it, `setAutoCommit` — and each arrived as a CLIENT
span named after the database rather than after a statement, wearing the whole
database semconv with `db.statement`/`db.query.text` present and empty. The
generator drew them, because its participant rule only asked whether a span looked
like a database call: `Backend -> DB: petclinic`, an arrow naming no call, revealing
no statement behind its ⊕, sitting right beside the query it was opened for.

- `c4b0df32` — **tooling cherry-pick** of `5434391d`, "genseq: drop JDBC spans
  that carry no statement". A DB span with no statement is dropped whole — no
  arrow, no activation, no note. A DB span whose *name* is a statement
  (`SELECT petclinic.owners`) is kept even without statement text: that is a real
  query recorded by an agent that was not asked to capture the SQL.
- `7ea4affd` — **branch-only**, "Redraw the sequences without the empty JDBC
  arrows". The last re-recording, and the one the published page shows: a full run
  of the tagged suites (Playwright, Cucumber, and the `@GenerateSequence`
  `@SpringBootTest`) against an isolated stack, so the pictures match both changes
  at once — the six `Backend -> DB: petclinic` arrows gone (three in
  add-visit-attended-by-a-vet, two in add-visit-to-an-existing-pet, one in
  owner-search), and the SMS hop back to a plain `send-sms` arrow linking into
  `FakeSmsNotificationSender.sendSms` instead of `POST /api/fake-sms`. The rest is
  what a re-recording always moves: the detail ids of the JSON payloads, and the
  order of the two independent XHRs the page fires on load.
- **redo**: cherry-pick the generator fix from `main`, then re-run the three
  tagged suites against a freshly seeded database and commit the regenerated
  `.genseq.puml`/`.json` pairs. This has to be the **last** diagram commit — any
  later retouch that changes a traced path needs its own redraw after it.

### 5 — Review tab: the aftermath band itself

No commit of its own — this is the fact that every retouch above (and the tooling
cherry-picks below) lands *after* `ce56d912`, the review commit. That's what
populates the aftermath band: red for the hand-written commits (code a human
added after the agent's review), grey for the ones `human-review.json`'s
`"generated"` globs classify as regenerated artifacts (diagrams, Code City).

The band splits the same commits a second way, orthogonal to that: the eight
cherry-picks are folded into one grey `8 tooling commits merged from main` row
and left out of every count in the headline, and the two `Merge main` commits are
dropped outright — a merge carries no patch of its own, and everything it brought
is already listed beside it. So what the headline counts is the retouches, which
is the point of the band and the reason this file has to stay accurate.

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

### 7 — UX tab (design-system audit): the vet field on the edit form left bare

The UX tab compares every screen of the branch against the same screen on the
merge-base and flags native controls sitting in a role the design system covers.
With both visit forms on `<app-combo>` the tab was honest but dull — **0 gaps**,
nothing to point at. So one field was deliberately downgraded: on **Edit a
visit** the vet picker is a plain `<select>`, while **Book a visit** keeps the
`<app-combo>`. Two screens, the same field, one right and one wrong — which is
the contrast the tab exists to show.

- **commit**: `226755c3` — "Use a raw `<select>` for the vet on the edit form,
  on purpose, so the design-system audit has a gap to show"
- **file**: `petclinic-frontend/src/app/visits/visit-edit/visit-edit.component.html`
  (the template carries an HTML comment saying the mistake is planted, so nobody
  "fixes" it by accident)
- **what the audit says afterwards**: verdict `gaps`, `new.bare = 1`,
  regression `new:select#vet` on *Edit a visit* — "not the design-system
  component — a plain `<select>` where **combo** belongs", severity `high`.
  Every other screen stays as it was (`old.bare = 0`, no pre-existing gaps), and
  the two `<input type=text>` fields on the same form stay `uncovered`/info,
  because no DS component claims that role.
- **behaviour is unchanged**: `name="vetId"` + `[(ngModel)]="visit.vetId"` with
  `[ngValue]` options, and `-- none --` bound to `null`, so the form still
  submits the same payload; `visit-edit.component.spec.ts` needed no edit
  (21/21 Karma specs under `visits/` green) and there is no Playwright scenario
  on the edit screen to adapt.
- **redo**: replace the `<app-combo inputId="vet" name="vetId" …>` on
  `visit-edit.component.html` with

  ```html
  <select id="vet" name="vetId" class="form-control" [(ngModel)]="visit.vetId">
    <option [ngValue]="null">-- none --</option>
    <option *ngFor="let vetOption of vetOptions" [ngValue]="vetOption.id">{{ vetOption.name }}</option>
  </select>
  ```

  then re-run the audit — `run-steps.py --only dsaudit --force` from
  `~/workspace/petclinic-pr` (it starts its own two Docker instances, one per
  side, and `down`s both) — and rebuild the page (`Rerun` on the served page, or
  `refresh-report.py --steps static`). Leave `visit-add.component.html` alone:
  the combo there is half the exhibit.

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
| `d0d57219` | `b5606c1b` | Send the fake SMS in-process again: keep the test instrumentation out of production code |
| `c4b0df32` | `5434391d` | genseq: drop JDBC spans that carry no statement |

Eight, and the whole eight: that is what the Review tab's aftermath band folds
away into `8 tooling commits merged from main`. To re-derive the list rather
than trust this table, ask `git` per commit — `git cherry origin/main <sha>` and
read `<sha>`'s own line, or `git merge-base --is-ancestor <sha> origin/main`.
Per commit, not once for the branch: `git cherry origin/main test-pr` answers
only 2 of the 8, because the branch has also *merged* `main` twice and a merged
base has nothing left on the other side of the symmetric difference to match
against.

(`f96259dc`/`2f5947ea` is a guardrail that was tried and reverted on `main`
itself — both halves rode over to `test-pr` together, which is why the pair
shows up here rather than as a retouch. `4aea32fc`/`d0d57219` is the same shape:
the SMS-over-HTTP hop and its revert, both `main`'s, both riding over. See
retouch 4 for why it was undone.)

## If you redo the implementation

Order matters — each step depends on the branch state the previous one left:

1. `/implement-ticket` → the two commits (feature, then review fixes +
   `review-points.md`).
2. Nothing else to write by hand for the review-points file — it's the coding
   session's own record.
3. Cherry-pick whatever tooling `main` carries at that point (the table above,
   or its current equivalents — check with the `git log --grep` recipe in
   Step 0).
4. Apply the retouches in this file — items 1, 2, 3, 6 and 7 — in roughly the
   order listed (Sequence/Tests before Logging/red-matrix is how it happened,
   but the only real constraint is: all of them after the review commit). Item 4
   is a precondition plus a decision: there is nothing in it to apply, and the
   HTTP hop it describes is the one thing in this file **not** to redo.
   Whichever retouch lands last and changes a traced path, redraw the sequence
   diagrams after it — `7ea4affd` is that redraw on the branch as it stands.
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
