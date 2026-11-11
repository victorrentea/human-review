You implemented a change in this conversation. Review it adversarially and record what you
decided. A script does everything mechanical; your turns go to judgement only. Do not
search the disk, re-derive the base, or compose reviewer prompts — the script has.

`RR` is `record-review.py`, in the folder this prompt is in. Run it from the repo root.

1. `RR prepare [--base <ref>]` — pass `--impl-subject "<what it implements>"` if it says
   the implementation is uncommitted, and `--ticket "<the ticket text>"` if you have it.
   `--base` is the base of the range you actually review, not the widest one; if HEAD is
   housekeeping that landed after the feature, pass `--implements <feature sha>`.
   It prints the base, the implementation commit, the diff (code, tests), four briefs, and
   whether the repository's own pre-push checks pass. A failing **push gate** is a finding
   you must fix (`source: pre-push hook`): /human-review cannot run until it passes.
   When the gate passes it pushes the branch, so CI starts reviewing in parallel.
2. Start one **read-only** reviewer subagent per brief, all four in parallel, each given
   only the content of its brief file. In Claude Code you may run `/code-review high`
   instead. Do NOT pass --fix and never let a reviewer edit: you decide every finding.
   If you can pick the reviewers' model, pick one other than yours.
   **Never end your turn waiting for anything.** Run every command — `RR`, a test suite, a
   traced run, a build — in the foreground and wait for it (give it a timeout long enough;
   poll with `sleep` in the same turn if it must run in the background). In a headless run
   (`claude -p`, `copilot -p`) the session ends with the turn and no notification ever
   brings you back: eval run 7 ended "waiting for `ci`", eval run 10 "waiting for the
   traced run", and both left the fixes uncommitted with `finish` never run.
   When the reviewers are done, run `RR ci` — at once: it stamps the reviewers' end,
   which is where the review's cost stops and the fixes' starts. It waits for CI and prints what
   failed — SonarCloud's new issues included — as findings with `source: CI`. A BUG or
   VULNERABILITY line fails the quality gate, so /human-review stops on it: fix it or
   decline it in the record. Code smells are yours to judge.
3. Decide each finding: fix it (edit the code), or decline it. Do not re-run a pass to
   "confirm" anything. Run only the tests that cover what you edited.
4. Write review-points.md at the repo root — the three piles only, the front-matter is
   filled for you. Format: ../human-review/reference/review-points.md (in the shipping
   repo: skills/human-review/reference/review-points.md). **Terse** — no prose under an
   item, no preamble, no summary:

       ### <what it is, 15 words at most>
       - file: path:line                   (Fixed: one per place the fix changed, tests too)
       - source: <which reviewer, or pre-push hook>
       - severity: high|medium|low|info    (Fixed and Ignored; never on an assumption)
       - observation: <what the reviewer found wrong, 1-3 sentences>   (Fixed and Ignored)
       - fix: <how you repaired it, one sentence>        (Fixed only, optional)
       - why: <Ignored: why declined, 15 words at most.
               Assumptions: 1-2 sentences — why this reading, and what holds the
               confidence where it is (what pushes it up, what pulls it down)>
       - alternative: <the reading not taken, 15 words at most>    (Assumptions only)
       - confidence: 0.xx                                         (Assumptions only)

     Fixed       — what you repaired because the review was right. The `observation:` is
                   what makes it readable: a title and a one-line diff say *that* something
                   changed, never *what was wrong* — "`router.navigate` returns a promise
                   nobody awaits; a failed navigation is swallowed silently."
     Ignored     — what you read and declined. An empty Ignored section after a
                   multi-agent review is not credible; if you accepted everything, say so
                   in one line. A finding you **refute** — the reviewer was simply wrong —
                   goes here too, never under Fixed and never dropped: `severity: info`,
                   and `why:` names the evidence that disproves it (the line, the test).
                   Left at medium it is counted on the page as "worth a look".
                   A `why:` that rests on a decision recorded elsewhere — the design,
                   proposal or tasks of the change, the Q&A, the ticket — gives the
                   `file:line` it rests on (`openspec/changes/<change>/design.md:50`,
                   `Q&A.md:28`), never just "design.md decided it" or "Q3 decided by the
                   human": the page links the line and quotes it, and a reason nobody can
                   open is a reason nobody can check.
     Assumptions — what YOU decided that neither the ticket nor the human did: the only
                   section nobody else can write. A choice the human made in this
                   conversation is not your assumption — give it `source: human` and
                   confidence 1.0, or leave it out.
                   A reading a reviewer attacked and you kept is NOT an assumption: it is
                   the Ignored item, with the reason you kept it in its `why:`. One
                   decision, one item — never an Ignored item plus an assumption on the
                   same line that says "see the assumption".
   Every entry names a file:line. An unanchored entry is dropped by the build. **Anchor
   the line that does the thing** — the call that throws, not the `.toList();` that ends
   its statement; a class or method by the line that matters in it, never its opening
   line (that quotes the whole body). 12 lines at most: the page opens 12 and folds the
   rest. Write each
   line number as it reads now, with your fixes on disk; `finish` carries an assumption
   written at the implementation commit across the fixes, and prints a WARNING for any
   ref whose line is gone or blank — a card the page cannot show the code for. On a Fixed
   entry the lines are what the page uses to show each card its own hunks of the fix
   commit: a hunk no Fixed entry's lines reach is listed apart, as another change.
   On an assumption, `source:` is only `human` (the human chose it here) or left out;
   the page labels every one `assumption`, so do not invent a label of your own.

   `confidence:` is what you actually think, in [0, 1]: 1.0 no other reading, 0.5 a coin
   flip, below 0.3 you expect to be corrected. **0.9 on everything is a lie the page will
   show** — the one call you were unsure about must stand out from the ones you were not.
5. `RR finish --subject "<what the fixes do>" --reviewers "<how the review ran>"` — it
   turns the file into the structured report the page is built from, validated against
   `review-points.schema.json` (fix what it names and re-run), commits the fixes with it
   as `[auto-fix] …` with the Review-Points:, Implements:, Audited: and Claude-Session:
   trailers, and writes and checks the PR comments. It carries and checks every
   `file:line` **before** committing: an anchor whose line is gone or blank stops it
   with `nothing committed` — point that line where it reads now and run `finish` again.
   Run again before `ci --push`, it amends its own commit: a round ends in ONE
   `[auto-fix]` commit, never a fix commit plus re-anchor follow-ups (eval run 10 left
   three). A harness that needs its own
   attribution adds `--commit-trailer "Co-authored-by: <who>"` (repeatable); a harness
   other than Claude Code names itself with `--harness <name>` (`copilot-cli`,
   `vscode-copilot`). It also measures what the implementation, the review and the fixes
   cost, in your harness's own logs, and commits it as `review-cost.json` — nothing for
   you to write. Commit nothing yourself.
6. `RR ci --push` — pushes the `[auto-fix]` commit and waits for CI **on that commit**.
   The review is not done until it is green: CI on the implementation commit may have
   failed early (a Spectral error) and never reached SonarCloud, so your fixes are
   unanalysed until now. Exit 0 is green. Exit 1 lists findings (`source: CI`): fix
   each, add it under Fixed in review-points.md, `RR finish` again (a new `[auto-fix]`
   commit), and `RR ci --push` again — **three rounds at most**; still red after the
   third, stop and say so in one line, naming what is left. Exit 2 (no run, or not
   finished in time) is not red: say so and stop.
7. Stop. Do not open a PR, do not build a review page.
