# The re-review, as a prompt

This is the paid pass behind the Review tab's 🤖: a fresh review of the **whole pull
request**, re-read against the code as it now stands. `rerun-review.py` fills the `@@…@@`
holes, hands the result to `claude -p --model opus` at the repository root, and does the
git work itself afterwards — so everything below the rule is addressed to that run, and
the run never commits.

It is a file rather than a string inside the script for the same reason
`matrix-prompt.md` is: it is the thing being paid for, so it is reviewed like prose and
diffed like prose.

---

You are re-reviewing a pull request that has moved since it was last reviewed, and
recording the review in the format this repository's review page reads. Work in the
repository you are started in. Nobody is at a keyboard: do not ask questions — where
something is ambiguous, decide, and record the decision.

**The change set**

- branch `@@BRANCH@@`, reviewed against `@@BASE@@`; merge base `@@MERGE_BASE@@`
- the whole PR is `git diff @@MERGE_BASE@@ HEAD` — every commit since the merge base, not
  only the ones since the last review. `git log --oneline @@MERGE_BASE@@..HEAD` lists them.
- the feature's implementation commit is `@@IMPLEMENTATION@@`
- the ticket: `@@TICKET@@` (read it with `gh issue view` when it is a GitHub issue and
  `gh` answers; carry on without it when it does not)
- this session's id is `@@SESSION@@`

**The previous record, as input — not as an answer.** The review this one replaces is in
`@@PREV_DIR@@/review-points.md` (and its PR comments in `@@PREV_DIR@@/pr-comments.json`
when there are any). Read it before you start: its **Assumptions** are the coder's
readings of the ticket, which no diff shows, and its **Ignored** items are decisions
somebody argued for. Re-check each against the current code. Carry forward what still
holds, re-anchored to where the code is now; drop what the branch has since made untrue.
It may end in a `## Taken over …` note — that note described the old record and **must
not** appear in yours.

## 1. Review

Run the harness's built-in **`code-review`** skill at effort **`high`** over the whole
change set above (target the branch, or the range `@@MERGE_BASE@@..HEAD`). Do **not** pass
`--fix` and do not pass `--comment`: you decide what to accept, one finding at a time, and
that decision is the artifact. If the skill is not available in this harness, do the same
review yourself: read every changed file whole, not just its hunks, and look for
correctness bugs, security and authorisation mistakes, data-loss and migration hazards,
and what the ticket asked for that the code does not do.

Run it **once**. A second pass over the same diff words and ranks its findings
differently; it does not confirm the first, it replaces it at full price.

## 2. Fix what you accept — and nothing else

For each finding you accept, change the code. Keep each fix minimal and on-topic; do not
refactor around it, reformat files, or touch anything the finding does not need. Run the
narrowest check that proves the fix when one is cheap (a single test class); do not run
whole suites.

**You never stage, commit, stash, reset, check out or push.** The script that started you
commits, by explicit path, after you stop — and it refuses to commit a changed file that
no fix below claims. So every file you change must be listed in exactly one fix group in
`@@FIXES@@`:

```json
{
  "fixes": [
    {
      "id": "FIX-1",
      "subject": "let owner admins read the vet list",
      "why": "One paragraph: the finding, and why this change is the fix.",
      "files": ["petclinic-backend/src/main/java/…/VetRestController.java"]
    }
  ]
}
```

- `id` is `FIX-1`, `FIX-2`, … in the order the commits should land. One group per
  independent fix; two findings fixed by one edit are one group.
- `subject` is an imperative phrase of at most 60 characters **without** the `[auto-fix]`
  tag — the script adds it.
- `files` are paths relative to the repository root, exactly as `git status` prints them.
- Nothing accepted: write `{"fixes": []}` and change no file.

Do not change `@@POINTS@@`'s neighbours, generated files, or anything under
`.human-review/` other than the two files named here.

## 3. Write `@@POINTS@@`

Overwrite `@@POINTS@@` at the repository root with the new record. The format is
`@@SKILL@@/reference/review-points.md` — read it; the parser is strict. In short:

- frontmatter keys `ticket`, `base`, `implementation`, `reviewers`, `session` — write them,
  but the script overwrites `base`, `implementation`, `reviewers` and `session` with the
  facts it computed, so do not agonise over them;
- exactly the three piles `## Fixed`, `## Ignored`, `## Assumptions`, and **no other H2** —
  in particular no `## Taken over …` note: this is a real pass, and the page tells a
  takeover from a review by that note alone;
- **terse**: a `###` title of at most 15 words, then only its fields (`file:`, `source:`,
  `severity:`, `why:`, `alternative:`, `confidence:`, `fixed-in:`); `why:` and
  `alternative:` at most 15 words; no prose under an item;
- every item names a `file:` with a line — an unanchored item is dropped by the build;
- **Fixed** — what *this* pass fixed, each with `fixed-in: FIX-<n>` naming its fix group
  (the script replaces it with the commit's sha); plus the earlier review's fixes that the
  current code still carries, each with `fixed-in:` the short sha of the commit that made
  it (`git log --grep='\[auto-fix\]'` and the previous record find them);
- **Ignored** — what you read and declined, with the reason. An empty Ignored after a
  multi-agent review is not credible; if you accepted everything, say so on one item;
- **Assumptions** — the readings of the ticket the code embodies, carried forward and
  re-checked, plus any new ones. `confidence:` on every one, in `[0, 1]`, and honest:
  0.9 on everything is a lie the page will show;
- `source:` is the pass that raised it, e.g. `/code-review high`.

Check it before you stop:

    @@PYTHON@@ @@SKILL@@/scripts/review-points.py --check

Exit 4 or 5 means the file is not worth committing — fix it.

## 4. Write `@@PR_COMMENTS@@`

The same items as inline PR comments, in the format of
`@@SKILL@@/reference/pr-comments.md` — one comment per item, `title` verbatim, anchored on
a line of `git diff @@MERGE_BASE@@ HEAD`. Two things differ from that document, because
the commits do not exist yet while you write:

- set `"commit_id": "REVIEW-COMMIT"` — the script fills in the review commit's sha;
- a fixed item of this pass opens `🛠 **Auto-fixed** in FIX-<n>` — the script replaces the
  group id with its commit's short sha.

Check it:

    @@PYTHON@@ @@SKILL@@/scripts/push-pr-comments.py --check --base @@MERGE_BASE@@

It reads the anchors off the *committed* HEAD, which does not hold your fixes yet — so an
anchor on a line one of your fixes added may be reported as moved or off the diff. That
one is expected (the script runs the check again once it has committed); fix every other
downgrade it reports. Do not post it.

## 5. Stop

Your final message is three lines: how many items went to each pile, how many fix groups
you wrote, and anything you could not do. Do not commit, do not push, do not build a page.
