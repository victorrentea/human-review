# human-review — how to work in this repo

## Infrastructure changes are made on `main` first, then pulled into the PR branch

Anything that feeds the machinery collecting code-quality data and metadata around a
review — a guardrail test, a script under `skills/human-review/scripts/`, `human-review.json`,
a git hook, CI, CODEOWNERS, the diagram/genseq pipeline — is changed **on `main` first**,
pushed, and only then brought over to the demo PR branch. Never the other way round, and
never on the PR branch alone.

The PR branch is an exhibit: it must differ from `main` by the feature it demonstrates and
by nothing else. A guardrail or a script that exists only on `test-pr` makes every review
run measure something `main` cannot reproduce, and the branch quietly drifts from the base
it is being compared against — which is exactly the thing this whole page is built to show.

In practice, in this workspace:

```sh
cd ~/workspace/petclinic-main        # always checked out on main
# …make the change, commit…
git push                             # that checkout is on main, a bare push is right

cd ~/workspace/petclinic-pr          # on test-pr, which TRACKS origin/main
git fetch origin && git cherry-pick <sha>   # linear history is what the course PR wants
git push origin HEAD:test-pr         # NEVER a bare `git push` here: it would go to main
```

`~/workspace/petclinic` is a third checkout on whatever branch it happens to be on — ask
before touching it, and never assume it is `main`.
