Implement the ticket you were given, then record the review of your own work.

1. Read this repo's own rules first — AGENTS.md / CLAUDE.md, and REVIEW.md if there is
   one — and follow them. Do not ask for clarification: where the ticket is ambiguous,
   pick a reading, write it down in step 4, and move on.
2. When the feature works and the repo's own checks pass, commit it. Nothing else in
   that commit. End the message with:
       Claude-Session: <this session's id>
3. Then run  /code-review high  over that commit. Do NOT pass --fix: you decide what to
   accept, one finding at a time, and that decision is the artifact this flow exists for.
4. Write review-points.md at the repo root. Format: see reference/review-points.md.
     Fixed       — what you repaired because the review was right. file:line, and which
                   reviewer raised it.
     Ignored     — what you read and declined, with the reason. An empty Ignored section
                   after a five-agent review is not credible; if you accepted everything,
                   say so in one line.
     Assumptions — what you decided that the ticket did not. Not defects: the readings
                   you chose, each with the reading you did not take under `alternative:`.
                   This is the only section nobody else can write, because it is not in
                   the diff.
   Every entry names a file:line. An unanchored entry is dropped by the build.
   Check it parses before you commit:  review-points.py --check
5. Commit the fixes and review-points.md together, with the trailers:
       Review-Points: review-points.md
       Implements: <sha from step 2>
       Claude-Session: <this session's id>
6. Stop. Do not push, do not open a PR, do not build a review page.

---

The two paths above, resolved:

    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/reference/review-points.md    the format
    ${CLAUDE_PLUGIN_ROOT}/skills/human-review/scripts/review-points.py      the parser

`review-points.py --check` prints what it understood and writes nothing; it exits 4 on a
file it cannot read and 5 on one whose every entry is unanchored. Both of those mean the
file is not yet worth committing. Run it from the repository root.
