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

Fork it first, and install from your own fork:

```sh
gh repo fork victorrentea/human-review --clone=false   # or the Fork button on GitHub
```

```sh
/plugin marketplace add <your-github-user>/human-review
/plugin install human-review@human-review
```

`human-review@human-review` is the plugin name and the marketplace name, both declared
inside the repository — they stay `human-review` in your fork, whoever owns it.

The detour is worth one paragraph. Adding a marketplace tells Claude Code to run
whatever that repository holds, and to keep doing so as the repository changes; this
plugin drives a browser, a database and a Maven build, and reads your working tree, so
that is a wide standing grant to hand to a branch someone else can push to. Nothing here
is specific to this repo — it is simply how plugin marketplaces work, and it is the
reason to point Claude Code at a copy whose contents change only when you change them.

The trade is that fixes made upstream no longer arrive on their own. Pull them when you
want them:

```sh
gh repo sync <your-github-user>/human-review --source victorrentea/human-review
/plugin marketplace update human-review
```

Then, from inside the repository you want reviewed:

```
/human-review              # uncommitted work
/human-review origin/main  # this branch vs a base
/human-review 123          # a pull request
```

## No green build, no review

Before anything else runs, the skill pushes the branch and waits for CI. A review of a
tree nobody has proved compiles is the exact failure the whole page exists to prevent: a
confident-looking guide, every number on it measured from a working copy of unknown
status. The wait binds to the **commit SHA that was just pushed**, never to "the latest
run on the branch" — a branch almost always has *some* green run on it, and that is the
easy way to end up reviewing one commit while quoting another's build.

Three outcomes:

- **green for that SHA** → the review proceeds, and the guide records what it was
  measured against.
- **red, cancelled, timed out — or no run for that commit at all** → it stops, and names
  the workflow and job. An empty run list is a stop, not a pass: absence of a build is
  not evidence of a passing one, and it is the state that looks quietest while proving
  least.
- **a repository with no CI configured at all** → it continues rather than blocking
  forever, and the guide says *"no build proved this"* on its face, so the page never
  reads as a pass it did not earn.

Your own fixes are not part of the gate. The skill deliberately leaves what it changed
uncommitted for you to inspect, so what gets pushed and gated is the *branch*, not the
review's edits.

## What it builds

`.human-review/review.html` — a throwaway artifact, regenerated, never committed. A review
is not one argument read top to bottom but a dozen separate questions, answered in
whatever order the reviewer's doubt takes them, so the page is a strip of tabs:

| tab | what it answers |
| --- | --- |
| 🤖 Review | the findings that are genuinely a human's call, most critical first |
| LLM Review | everything the two automated passes raised — what was applied, and what was left for you |
| Demo | a Playwright recording of the feature, narrated |
| Sequence | sequence diagrams recorded from real traces, each beside the test that produced it — and the tests tagged for tracing that came back without one |
| Requirements | what the change set was supposed to do |
| Data | the DB and domain deltas — added in red, removed in red and struck — and the change in domain language |
| Structure | the package delta, or the current package diagram as context |
| API | every operation and schema the branch moved, each classified breaking / additive / changed / cosmetic |
| UX | native controls sitting where a design-system component belongs — a finding made of an *absence*, which the passing Playwright suite cannot produce |
| Code City | the change lit up in a 3D Code City |
| Complexity | the before → after of every entry point |
| Logging | what the change set will say for itself in production — found by syntax, not by grep |
| CODEOWNERS | who has to approve this, and whether the merge is blocked |

Tabs are declared in the content file, so the layout is the guide's to choose: a tab with
nothing to show is dropped and named, a changed diagram no tab claimed warns at build time,
and `show all` (or printing) reveals every panel at once so `⌘F` searches the lot.

## What it needs

The skill drives tools that belong to your project, and degrades rather than fails when
one is absent: a tab with nothing to show is dropped, and named under the strip.

Hard requirements — nothing runs without these:

- **`python3`**, with **Pygments** and **Pillow** — the page builder cannot render at all
  without them
- **`git`**, **`plantuml`**, and the **`gh`** CLI against a GitHub remote: the run opens by
  pushing the branch and waiting for CI to go green, and stops if it is not
  (a repository with no CI is let through, and the guide says no build proved it)

Everything else buys a tab, and its absence costs only that tab:

- **PyYAML** plus a JVM or Docker — the API contract diff; `oasdiff` (Homebrew) resolves
  `$ref`s the fallback cannot, and `openapi-changes` (pb33f) embeds a second opinion
- **`ast-grep`** — the Logging tab, the only way to tell `log.info(x)` from `Math.log(x)`
- **Playwright**, **numpy** and both branches served — the design-system audit
- **ffmpeg** and a TTF the captions can use — the feature video
- Diagrams to diff: any `.puml` your project generates and commits, and a hand-drawn
  `.drawio.png` if you keep one
- Your project's own hooks, substituted at five named points: a traced test run, a Code
  City render, a filmable browser suite, an endpoint-complexity extractor, and a committed
  OpenAPI spec

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

## The draw.io differ

`skills/human-review/scripts/drawio-diff.py` answers the same question for the one
diagram a human draws by hand — a `.drawio.png`, whose mxGraph XML rides along inside
the PNG:

```sh
./drawio-diff.py old.drawio.png new.drawio.png --out-dir assets --name conceptual
./drawio-diff.py --base origin/main --diagram docs/ConceptualModel.drawio.png \
                 --out-dir .human-review/assets --name conceptual
```

It compares by the identity each element **declares** in that XML — `concept="Owner"` on
a box, `assoc="Owner-Pet"` on a line, the mxCell id otherwise — and never by rendered
pixels. Nudging a box does not make it a different box, and neither does rewording the
label drawn on it, so a re-layout does not arrive as a page of phantom additions.

The same reading is what stops it miscounting. A caption, a title, a sticky note and the
"Please manually fix the layout." marker are all drawn as vertices, but they are parsed
as **annotations**, not as concepts — the distinction is data off the file, not a guess
from the wording. Counting a sticky note as a box is how a review page ends up announcing
a new domain class nobody added.

Two colours, and they must not be conflated: **red** is the diagram's own — the patch
script paints an element red when it drew that element itself, and it stays red until a
human lays it out by hand — while **orange** is this tool's mark for what the branch
adds. An element that is both renders red, because the to-do is the louder fact; turn it
black in draw.io and it goes orange, because it is still new.

It writes three SVGs plus a machine-readable `<name>-diff.json`. Rendering goes through
the draw.io desktop app when it is installed, which is the only faithful picture;
without it, a built-in renderer walks the mxGeometry, which is enough for this class of
diagram. It needs nothing installed either way.

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

## The design-system audit

`skills/human-review/scripts/ds-audit.py` asks whether the frontend change actually used
the design system — and it is built around the fact that **the defect is an absence**.
Labelling the components that *are* right proves nothing. The bug is that somebody copied
an older template and shipped a bare `<select>` where the standardised combo belongs, and
it looks close enough that review slides straight past it. So the audit inverts the
question: know which *roles* the design system covers, then flag native controls filling
one of those roles that sit **outside** any DS component. Green is context; red is the
product.

```sh
./ds-audit.py --base-new http://localhost:4300 --base-old http://localhost:4301 \
              --screen "Book a visit=pets/11/visits/add" \
              --screen "Edit a pet=pets/11/edit" \
              --label-new my-branch --label-old main \
              --source ../frontend/src \
              --assets assets -o assets/ds-audit.html --json assets/ds-audit.json
```

The role registry is **derived, not listed**. A hand-written table of "roles the design
system covers" is wrong the day the second component lands and nobody remembers the file
exists, so it is read from four sources in descending authority — `data-ds-covers` where
the component author said it out loud, then the control a rendered DS host actually
wraps, then the same reading taken off a template for a component this screen does not
happen to render, and last a guess from the name, marked in the output as a guess.
Adding `data-ds="datepicker"` to a component needs no code here. The one thing the design
system has to do is mark its host with `data-ds="<name>"`; nothing depends on its class
names or its DOM shape.

The registry is built from **both** revisions at once and applied to both, which is what
makes a migration read as an improvement and a straggler read as a gap. Several screens
per run, because a migration touches one control per form: *"it flagged the bare one"* is
a weak claim, *"it flagged only the bare one, and called the other three right"* is the
one worth making.

The JSON is the artefact and the picture is its rendering — a reviewing agent reads
`--json` rather than OCR-ing a PNG. Needs Playwright (`pip install playwright &&
playwright install chromium`), Pillow and numpy, and both revisions served.

## Publishing a snapshot to GitHub Pages

A review page is a throwaway artifact — but one frozen copy is worth keeping, so that
somebody can see what `/human-review` produces before installing anything. Anything under
`demo/` in this repo is published to GitHub Pages:

<https://victorrentea.github.io/human-review/>

`demo/index.html` is a hand-written landing page listing the snapshots; each snapshot lives
in `demo/<slug>/` and is a verbatim copy of a `.human-review/` output directory —
`review.html`, `content.json` and `assets/`, bytes untouched. `.github/workflows/pages.yml`
uploads the whole `demo/` directory and deploys it on every push to `main` that touches
`demo/**` (or the workflow itself), and on `workflow_dispatch`.

To add one:

```sh
skills/human-review/scripts/publish-demo.sh <slug> [source-dir]   # source-dir defaults to .human-review
```

It copies the snapshot into `demo/<slug>/` — the target checkout comes from
`HUMAN_REVIEW_REPO`, defaulting to `~/workspace/human-review` — refuses any file over
50 MB, and prints the commit-and-push commands rather than running them. Then add a card
for it in `demo/index.html`.

**One caveat.** Every code reference in a review page is a `vscode://file/...` deep link
holding an absolute path on the machine that generated it. Those links open nothing on a
stranger's machine. Diagrams, video, complexity and snippets are self-contained and work
anywhere, so a published snapshot is a faithful tour of everything except the click-into-
your-editor part.

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
