# `pr-comments.json` — the review, prepared as the exact GitHub call

The agent that writes `review-points.md` writes this file too, in the same sitting, while
it still knows *which line* each item is about. It is the body of GitHub's *create a
review* call — `POST /repos/{owner}/{repo}/pulls/{n}/reviews` — with every Fixed, Ignored
and Assumptions item as an inline comment on the code it concerns. The Review tab's
**Push to GitHub PR** button runs `scripts/push-pr-comments.py`, which re-checks the anchors
against the PR as it is at that moment and sends the file. It does not write a word of it:
no model runs at push time, so what lands on the PR is what you wrote here.

**Where:** `.human-review/pr-comments.json` (gitignored — it is a call, not a record; the
record is `review-points.md`). Write it **after** commit #2, so `commit_id` is that commit.

## The shape

```json
{
  "version": 1,
  "commit_id": "<git rev-parse HEAD, right after the review commit>",
  "event": "COMMENT",
  "body": "**Review record** (/code-review high): 3 auto-fixed · 6 declined and still open · 7 assumptions.",
  "comments": [
    {
      "pile": "fixed",
      "title": "The vet picker could not load on the screens that need it",
      "path": "petclinic-backend/src/main/java/victor/training/petclinic/rest/VetRestController.java",
      "line": 50,
      "side": "RIGHT",
      "anchor": "@PreAuthorize(\"hasAnyRole(@roles.OWNER_ADMIN, @roles.VET_ADMIN)\")",
      "body": "🛠 **Auto-fixed** in db6b67ec — `GET /api/vets` was VET_ADMIN-only, so the owner-admin booking form got an empty vet picker. Only `listVets()` widens; writes stay VET_ADMIN.\n\n<sub>raised by /code-review high</sub>"
    },
    {
      "pile": "assumption",
      "title": "vet_id is ON DELETE SET NULL, so deleting a vet is never blocked",
      "path": "petclinic-backend/src/main/resources/db/migration/V4__visit_vet.sql",
      "start_line": 3,
      "line": 5,
      "side": "RIGHT",
      "anchor": "ALTER TABLE visits ADD COLUMN vet_id INT REFERENCES vets (id) ON DELETE SET NULL;",
      "body": "💭 **Assumption** · confidence 0.55 — a deleted vet leaves their visits unattended rather than blocking the delete.\n\n**Not taken:** RESTRICT, which makes `DELETE /api/vets/{id}` fail on real data."
    },
    {
      "pile": "ignored",
      "title": "The user manual still describes the visit screens without a vet",
      "path": "user-manual/manual.md",
      "subject_type": "file",
      "body": "🔴 **Open issue** · low · declined — the manual is regenerated from the running UI; a hand edit would disagree with its own screenshots."
    }
  ]
}
```

Everything outside `comments[]` except `version` is GitHub's own field. Inside a comment,
`path`, `line`, `start_line`, `side`, `body` and `subject_type` are GitHub's; `pile`,
`title` and `anchor` are for the script and are stripped before sending.

## The rules

1. **One comment per item of `review-points.md`, and only those.** `pile` is `fixed`,
   `ignored` or `assumption`; `title` is the item's `###` title *verbatim*. The two make its
   id (`A:vet-id-is-on-delete-set-null-so-deleting-a-vet`), which becomes a hidden
   `<!-- hr:… -->` marker in the body: a second push finds it and **updates** that comment
   instead of posting a copy, and the page links each item to its comment by it. Renaming a
   title therefore posts a new comment — keep titles stable once pushed.
2. **Anchor on a line that is in the PR's diff**, on the new side (`side: "RIGHT"`) — an
   added line or one of the three context lines around it. Check with
   `git diff "$(git merge-base origin/main HEAD)" HEAD -- <path>`. Pick the line the
   reviewer should be *looking at*: the changed call, the new annotation, the migration
   statement — not the class declaration, and not where the `file:` ref happened to point
   if a better line is two rows away. A range (`start_line`..`line`) must sit in one hunk.
3. **`anchor` is the exact text of `line`** (leading/trailing space ignored). It is what
   lets the push follow the line when a later commit shifts it; without it a moved line is
   only caught by falling outside the diff.
4. **Code outside the diff** — the finding is about a file the PR did not touch, or a line
   of it far from any hunk: give `subject_type: "file"` and no `line` when the file *is* in
   the diff; when it is not, keep its `path` anyway and the script folds the comment into
   the review body under *Not on a line of this diff*. Never invent a nearby changed line
   just to get it inline — a comment on the wrong line is worse than one on the file.
5. **Bodies are short and start with their kind.** The PR thread is not the page: two or
   three sentences, the decision and its reason, GitHub markdown.

   | pile | opens with |
   | --- | --- |
   | `fixed` | `🛠 **Auto-fixed** in <short sha>` — the commit that holds the fix |
   | `ignored` | `🔴 **Open issue** · <severity> · declined` — then *why* it was declined |
   | `assumption` | `💭 **Assumption** · confidence <0.00>` — then the reading **not** taken |

   Do not paste the `review-points.md` body. Do not write the marker; the script adds it.
6. **`body`** (the review's own) is one line: what was run and the three counts.
7. **`event` is always `COMMENT`.** This records a review; it never approves or blocks.

## Check it before you stop

```sh
push-pr-comments.py --check                 # anchors vs HEAD's diff, against origin/main
push-pr-comments.py --check --base <ref>    # when the PR will target something else
```

It prints how many comments land on a line, how many on a file, how many fold into the
body, and one line for every comment it had to move or downgrade. A downgrade you did not
intend is an anchor to fix now — at push time it will be too late to ask you. It exits 2 on
a payload it cannot use (a missing `line`, a `LEFT` side, two items with one title).

Do **not** push. Posting publishes under the human's GitHub account; that is their button.

## When the file is missing

`push-pr-comments.py --from-review-points` writes it out of `review-points.md`
deterministically — each item's first `file:` ref becomes the anchor, its prose is cut to
a paragraph. That is the fallback for branches recorded before this file existed, and it
reads like one: bodies are excerpts rather than sentences written for the PR, and a ref
that pointed near a hunk rather than into it lands on the file. Write the file yourself.
