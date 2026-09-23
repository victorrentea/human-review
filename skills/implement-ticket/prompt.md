Implement the ticket you were given, then record the review of your own work.

1. Read this repo's own rules first — AGENTS.md / CLAUDE.md, and REVIEW.md if there is
   one — and follow them. Do not ask for clarification: where the ticket is ambiguous,
   pick a reading, write it down in step 4, and move on.
2. When the feature works and the repo's own checks pass, commit it. Nothing else in
   that commit. The last lines you write in the message are:
       Claude-Session: <this session's id>
3. Then run  /code-review high  over that commit. Do NOT pass --fix: you decide what to
   accept, one finding at a time, and that decision is the artifact this flow exists for.
4. Write review-points.md at the repo root. Format: see reference/review-points.md.
   **Terse.** The reader skims it and jumps into the code; every extra sentence is one
   they have to wade through. One item per finding, and only its fields:

       ### <what it is, 15 words at most>
       - file: path:line
       - severity: high|medium|low         (Fixed and Ignored; never on an assumption)
       - why: <15 words at most>           (Ignored: why declined. Assumptions: why this reading)
       - alternative: <the reading not taken, 15 words at most>    (Assumptions only)
       - confidence: 0.xx                                         (Assumptions only)

   No prose under an item, no preamble, no closing summary, no restating the diff or the
   ticket. More detail only if the human asks for it.
     Fixed       — what you repaired because the review was right.
     Ignored     — what you read and declined. An empty Ignored section after a
                   five-agent review is not credible; if you accepted everything, say so
                   in one line.
     Assumptions — what you decided that the ticket did not: the only section nobody
                   else can write, because it is not in the diff.
   Every entry names a file:line. An unanchored entry is dropped by the build.
   Check it parses before you commit:  review-points.py --check

   `confidence:` — a number in [0, 1] on every assumption, two decimals at most:

       1.0         the ticket left no other reading
       0.5         a coin flip between two readings
       below 0.3   you expect to be corrected

   The number is rendered to the reviewer beside the word "assumption", and it is what
   decides where they spend their attention — so it has to be what you actually think,
   not what is comfortable to hand over. **0.9 on everything is a lie the page will
   show**: a pile of assumptions that are all nearly certain reads as an agent that
   never noticed it was guessing, and the one reading you were genuinely unsure about
   becomes indistinguishable from the six you were not. If a call was close, write 0.5
   and let the reviewer go and look. Being told where to look is the entire point of
   the section; a flat 0.9 tells them nothing and costs you nothing, which is exactly
   what makes it worthless.
5. Commit the fixes and review-points.md together. The subject line starts with
   `[auto-fix]` — e.g. `[auto-fix] apply 3 review findings on visit/vet` — and so does
   every later commit that applies a reviewer's finding, so `git log --grep='\[auto-fix\]'`
   finds everything you changed on the review's say-so.
   The last lines you write in the message are:
       Review-Points: review-points.md
       Implements: <sha from step 2>
       Claude-Session: <this session's id>
   Write them as the final lines of your message, one key per line, nothing between
   them. If the harness then appends a paragraph of its own — it adds
   `Co-Authored-By: Claude …` — leave it alone: the parser reads these keys out of the
   whole message body, not only out of git's trailer block, so a paragraph after them
   changes nothing. Do not move them, do not repeat them below it.
5b. Then prepare the pull-request comments: write .human-review/pr-comments.json, the exact
   body of GitHub's create-a-review call, one inline comment per item of review-points.md,
   each on the line of the diff it is about. Format and rules: reference/pr-comments.md.
   Check it:  push-pr-comments.py --check   — and fix every anchor it had to downgrade.
   Do not post it; the human presses the button that does.
6. Stop. Do not push, do not open a PR, do not build a review page.

---

The two paths above, resolved:

    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/reference/review-points.md    the format
    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/scripts/review-points.py      the parser
    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/reference/pr-comments.md      the PR comments
    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/scripts/push-pr-comments.py   their check

`review-points.py --check` prints what it understood and writes nothing; it exits 4 on a
file it cannot read and 5 on one whose every entry is unanchored. Both of those mean the
file is not yet worth committing. Run it from the repository root.
