# human-review

A `/human-review` skill for [Claude Code](https://claude.com/claude-code): it assembles
**one page, tabbed one question per tab**, that lets a human review a change set fast —
what to look at first, what the diagrams say changed, what the REST contract did, where it
landed in the code, a video of the feature working, what it cost in complexity, and the
tests that pin it. Every code reference is a click into your editor, and every snippet is
cut from the working tree at build time, so the page cannot drift from the code it
describes.

It was extracted from a real project's review loop, where it was used on real branches
before it was made portable.

## Install

```sh
/plugin marketplace add victorrentea/human-review
/plugin install human-review@human-review
```

Then, from inside the repository you want reviewed:

```
/human-review              # uncommitted work
/human-review origin/main  # this branch vs a base
/human-review 123          # a pull request
```

## What it builds

`.human-review/review.html` — a throwaway artifact, regenerated, never committed. A review
is not one argument read top to bottom but five or six separate questions, answered in
whatever order the reviewer's doubt takes them, so the page is a strip of tabs:

| tab | what it answers |
| --- | --- |
| Review | the findings that are genuinely a human's call, most critical first — and, beside them, the ones the agent already fixed |
| Behaviour | sequence diagrams recorded from real traces, the tests that produced them, a Playwright recording of the feature, and what is *not* covered |
| API contract | every operation and schema the branch moved, each classified breaking / additive / changed / cosmetic |
| Data model | the DB and domain deltas — added in red, removed in red and struck — and the change in domain language |
| Packages | the package delta, or the current package diagram as context |
| Cost & shape | the before → after of every entry point, and the change lit up in a 3D Code City |

Tabs are declared in the content file, so the layout is the guide's to choose: a tab with
nothing to show is dropped and named, a changed diagram no tab claimed warns at build time,
and `show all` (or printing) reveals every panel at once so `⌘F` searches the lot.

## What it needs

The skill drives tools that belong to your project, and degrades rather than fails when
one is absent: a section with nothing to show is dropped, and says so.

- **`plantuml`** on `PATH` — for the diagram deltas
- **`python3`** — the page builder, the snippet extractor, the differs
- **PyYAML** — only for the API contract diff
- Diagrams to diff: any `.puml` your project generates and commits
- Optional: a Playwright suite (the feature video), a Code City render, an
  endpoint-complexity extractor

## The PlantUML differs

`skills/human-review/puml-diff/` diffs two versions of a diagram and renders the delta —
useful on its own, and not tied to this skill:

```sh
python3 puml_diff.py OLD.puml NEW.puml --out merged.puml           # class / ER / package
python3 puml_diff.py OLD.puml NEW.puml --focus 1                   # …trimmed to what changed
python3 seq_puml_diff.py OLD.puml NEW.puml --out merged.puml       # sequence
```

Two diagram families need two algorithms. A class diagram's meaning is a *set* of elements
and relationships, where order carries nothing. A sequence diagram is the opposite: an
ordered script of messages, where the same arrow twice is two different events.

The delta is titled as one — `title Domain Model` renders as **Domain Model - <span
style="color:red">Diff</span>** — so a picture that escapes its page still says it is a
change rather than a snapshot.

`--focus 0|1|2|3|all` answers the problem every large diagram has — a two-line change
arrives as a wall you have to search for red in. It keeps what changed plus N relationships
outwards, so the same delta can be read at whatever radius makes it legible.

```sh
python3 -m pytest skills/human-review/puml-diff
```

## The OpenAPI contract differ

`skills/human-review/scripts/openapi-diff.py` reads two revisions of an OpenAPI spec as
*structures* rather than as text, and classifies every difference by what it does to
somebody already calling the API:

```sh
python3 openapi-diff.py --base origin/main --out fragment.html   # vs the merge-base
python3 openapi-diff.py before.yaml after.yaml --json            # two files, no repo needed
```

**breaking** — a removed operation, response, or property; a tightened constraint; a
newly-required request field; a changed type or `$ref`. **additive** — anything that cannot
break an existing caller. **changed** — `readOnly`, `default`, `deprecated`, `operationId`:
real, but a judgement call. **cosmetic** — documentation only.

It reads the contract, not the handler, so it cannot see a semantic break that leaves the
spec additive — a `PUT` that starts *clearing* a field it was not sent looks like "an
optional field appeared" to any structural differ. The unified diff rides along in a
`<details>` for exactly that reason.

## Editing it in place

The skill is developed by symlinking it into a project rather than reinstalling it:

```sh
ln -s ~/workspace/human-review/skills/human-review \
      <project>/.claude/skills/human-review
```

Every script resolves the project under review from the directory it was **invoked** in,
never from where the script itself lives — which is what makes the symlink work.

## Licence

MIT
