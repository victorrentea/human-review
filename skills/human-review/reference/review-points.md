# `review-points.md` — what the agent fixed, declined and assumed

The file the coding agent writes about its **own** work, at the **repository root**, and
commits. It is the one artifact in this flow that no later pass can reconstruct: a finding
is found by reading the diff, but *which findings were accepted, which were declined and
why* exists only in the conversation that triaged them — and the reading of an ambiguous
ticket exists nowhere at all unless the agent that chose it writes it down.

**Root, not `.human-review/`.** `.human-review/` is gitignored in every repo that uses this
skill, and this file must be committed: at the root it shows up in the PR's own file list,
so a reviewer sees the artifact arrive in the diff instead of taking a page's word for it.
Per branch, overwritten, never accumulated.

Override the path with `"reviewPoints": "docs/review-points.md"` in `human-review.json`.

`review-points.py` turns it into `.human-review/review-points.json`, whose three arrays are
the three the Review tab already renders (`content-schema.md` — *A finding / autofix item*
and *An assumption*). The parser is deliberately strict and loud: it is run with `--check`
before the file is committed, so a malformed file is rejected while the agent is still in a
position to fix it, rather than silently rendering as an empty pile.

## The format

````markdown
---
ticket: victorrentea/petclinic#37
base: 2a45c210
implementation: 7f3c1a9
reviewers: /code-review high
session: 16a1e790-2c96-4f1b-8a4f-2ddcf2d10a8e
---

## Fixed

### The seed hard-coded the number of vets in its round-robin
- file: petclinic-backend/src/main/resources/db/seed/R__seed.sql:143
- source: /code-review agent 2 (shallow bug scan)
- fixed-in: HEAD
Both bounds now come from the vets table, so adding a seventh vet cannot
leave it unassigned.

## Ignored

### Collapse the two divergent booking implementations
- file: petclinic-backend/src/main/java/victor/training/VisitRestController.java:66
- source: /code-review agent 1 (AGENTS.md adherence)
- severity: medium
- why: out of scope for #37, and deleting the flat endpoint is an API break.

## Assumptions

### @Transactional went on the public endpoints, not on bookVisit
- file: petclinic-backend/src/main/java/victor/training/VisitRestController.java:62-68
- alternative: annotate `bookVisit` as the ticket asked — a silent no-op
- why: Spring AOP ignores self-invoked private methods.
The ticket named the inner method; the annotation only does anything on the
two callers, so that is where it went.
````

### Frontmatter

`key: value` lines between two `---` fences. **Not YAML** — no nesting, no lists, no
quoting rules — because a parser that accepted YAML would also accept nine ways of writing
the same thing and one way of writing something subtly different. Every key is optional and
unknown keys are carried through untouched.

| key | what it is for |
| --- | --- |
| `ticket` | what was asked for, `owner/repo#n` or a URL |
| `base` | the rev the change set is measured against |
| `implementation` | sha of commit #1, the implementation-only commit. It becomes the default `base` of every `fixed-in` diff — the only left side that shows a review fix *on its own* |
| `reviewers` | what was run, verbatim (`/code-review high`) |
| `session` | the coding session's id, the same value as the `Claude-Session:` trailer |
| `fixed-in` | where the fixes landed, when every item shares one answer |

### Sections

Exactly three piles, as H2, case-insensitive, each at most once:

| heading | aliases | lands in |
| --- | --- | --- |
| `## Fixed` | `Repaired`, `Applied` | `autofixes` — repaired because the review was right |
| `## Ignored` | `Rejected`, `Declined`, `Not fixed` | `findings` — read and declined, with the reason |
| `## Assumptions` | `Assumed` | `assumptions` — decided, where the ticket did not decide |

**An unrecognised H2 is a hard error, never a silently dropped section.** A pile the parser
does not recognise reads on the page exactly like a pile that was never written, and those
are the two things a reviewer most needs told apart.

An empty `## Ignored` after a multi-agent review is not credible, and the parser cannot
know that — so it accepts it and the skill's prompt demands a sentence instead.

### Items

`### <title>` opens an item. The `- key: value` lines directly under it are its fields; from
the first line that is not a field to the next `###` or `##` is its **body**, free prose.
A field line that appears *after* the body has started is an error, not a field — the
ordering is what lets ordinary markdown bullets live in the body without being mistaken for
fields.

| field | repeatable | becomes |
| --- | --- | --- |
| `file:` | yes | `refs[]`, plus a `snippets[]` card when the value carries a line range (`path:12-30`). `\| caption` after the ref captions the card |
| `source:` | no | `source` — the pass that raised it, verbatim. `/code-review` and `/simplify` render as links to their own docs |
| `severity:` | no | `severity` — `high\|medium\|low\|info`. Defaults to `info` in `Ignored`; **rejected outright on an assumption**, which is not a defect and must not be ranked as one |
| `alternative:` | no | `alternative` — the reading that was *not* taken. What makes an assumption checkable at a glance |
| `why:` | no | `why` — the reason to decline, or the reason the reading was chosen |
| `fixed-in:` | no | `diffs[]`, one per `file:`, based at the frontmatter's `implementation`. `fixed-in: HEAD` leaves the head side as the working tree, which keeps the editor link; any other value pins both sides |

An unknown field key is an error too. `- fille:` typed once would otherwise drop a ref, and
a dropped ref is what gets the whole item deleted by the rule below.

A field wraps onto the line(s) right after it as long as each is indented (2+ spaces) and
is not itself a `- key:` line; the continuation joins the value with a space, so it reads
as one sentence. The first unindented line, as always, starts the body instead:

```markdown
- alternative: annotate `bookVisit` as the ticket asked — which would be a
  silent no-op
- why: Spring AOP ignores self-invoked private methods.
```

reads as `alternative: annotate \`bookVisit\` as the ticket asked — which would be a silent no-op`.

Bodies may carry the same inline tokens as any other body on the page:
`{{snippet:path:12-30|caption}}`, `{{diff:path@<sha>|caption}}`, `{{difflink:path@<sha>}}`.
Backticked spans become `<code>`; everything else is escaped, so a stray `<` in prose cannot
reach the page as markup.

### The anchoring rule

**An item with no `file:`, no snippet and no diff is dropped, with a warning naming it.**
It is inherited from the assumptions pile, where the build has always applied it, and it now
covers all three: an item a reader cannot go and look at is indistinguishable from one that
was never true. A model asked at the end of a long session what it fixed, declined or
assumed will produce fluent sentences of exactly this shape either way, and the anchor is
the whole difference.

## Running it

```sh
review-points.py --check                 # validate and print what it understood; writes nothing
review-points.py                         # write .human-review/review-points.json
review-points.py --root ../petclinic --file docs/review-points.md --out /tmp/rp.json
```

| exit | means |
| --- | --- |
| 0 | parsed |
| 3 | no such file — nobody recorded what was reviewed on this branch |
| 4 | present and unparseable: an unknown H2, an unknown field, a field after the prose, a duplicate section, no section at all |
| 5 | present, parsed, and **every** item was unanchored — which is a file that says nothing, reported as such rather than as an empty review |

Exit 3 and exit 5 are distinct on purpose. "Nobody wrote one" and "somebody wrote one with
nothing checkable in it" are different failures, and the page says different things about
them; a single "no points" code would let the second hide behind the first.
