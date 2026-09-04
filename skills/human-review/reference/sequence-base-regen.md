# Regenerating the base sequence diagrams (Step 3)

Only when the generator in `petclinic-test/src/genseq/` has moved since the base ref.

**The base diagram must come from the same renderer as the new one.** A sequence diagram is a
rendering choice as much as a recording: change what an arrow is labelled with, and every
arrow in the committed base diagram reads as a deletion with its replacement added underneath
— a wall of red that says nothing about the change under review.

```sh
git stash push --include-untracked          # your fixes are uncommitted; keep them
git checkout $BASE -- petclinic-test/src/genseq petclinic-backend/src/main/resources/application.properties
cd petclinic-test && GENSEQ_REFRESH=1 ./run-tests-with-tracing.sh   # base traces, base renderer
```

…then **commit those regenerated base diagrams onto a throwaway ref and pass that ref as
`$BASE`**. Not optional: `puml-diff.sh` reads the "before" side with `git show
"$MERGE_BASE:$path"`, straight from the commit object — never from the work tree. Regenerating
into the working directory and then restoring the branch throws the work away and diffs
against the committed base diagram exactly as before, at the cost of a full traced run.

```sh
git checkout -b throwaway-base $BASE && git commit -am "base diagrams, same renderer" \
  && BASE=throwaway-base
```

`npm run trace:diagram` re-renders from the cached spans in about a second, so the expensive
part is the one traced run per side.

If you skip it, say so in the guide next to the diagram: a red arrow the reviewer cannot
distinguish from a real change is worse than no diagram.

## The Hibernate SQL-comment gotcha

DB arrows are labelled from Hibernate's own comment on each statement
(`hibernate.use_sql_comments`, in the backend's `application.properties`). A backend started
before that property existed emits statements without it, and every DB arrow falls back to its
span name — `SELECT petclinic`, over and over. If that is what you see, the running backend
predates the property: restart it and re-record.
