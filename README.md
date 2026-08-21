# human-review

A `/human-review` skill for [Claude Code](https://claude.com/claude-code): it assembles
**one page** that lets a human review a change set fast — what to look at first, what the
diagrams say changed, where it landed in the code, a video of the feature working, what it
cost in complexity, and the tests that pin it. Every code reference is a click into your
editor, and every snippet is cut from the working tree at build time, so the page cannot
drift from the code it describes.

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

`.human-review/review.html` — a throwaway artifact, regenerated, never committed:

| section | what it answers |
| --- | --- |
| Look here first | the findings that are genuinely a human's call, most critical first |
| Diagram deltas | what changed structurally — added in red, removed in red and struck |
| Where it landed | the change lit up in a 3D Code City of the codebase |
| See it work | a Playwright recording of the feature, deliberately slowed |
| Entry point complexity | the before → after of every entry point, ranked in context |
| Core business logic | the change in domain language, each claim backed by a snippet |
| Acceptance tests | the tests that pin the behaviour — and what is *not* covered |

## What it needs

The skill drives tools that belong to your project, and degrades rather than fails when
one is absent: a section with nothing to show is dropped, and says so.

- **`plantuml`** on `PATH` — for the diagram deltas
- **`python3`** — the page builder, the snippet extractor, the differs
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

`--focus 0|1|2|3|all` answers the problem every large diagram has — a two-line change
arrives as a wall you have to search for red in. It keeps what changed plus N relationships
outwards, so the same delta can be read at whatever radius makes it legible.

```sh
python3 -m pytest skills/human-review/puml-diff
```

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
