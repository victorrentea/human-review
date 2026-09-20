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

## Where the page is built: one module per tab

`build-review-html.py` was one file of ten and a half thousand lines: the Logging tab's
privacy prompts forty lines from the cost table, and the whole stylesheet in the middle of
both. It is now an orchestrator over a package next to it.

```
skills/human-review/scripts/
  build-review-html.py     the orchestrator: read the content file, validate it, render
                           each tab's blocks in order, assemble the document, write it
  hrbuild/
    tabs/                  one module per tab of the page
      review.py            findings, assumptions, applied fixes, the aftermath band
      sequence.py          a sequence diagram paired with the test that draws it
      tests.py             the test ledger, the requirement lists, the recordings
      demo.py              the feature film, its captions, its verdict
      city.py              the Code City shot
      logging.py           what the branch logs, and whether it is a privacy problem
      owners.py            the CODEOWNERS verdict
      cost.py              what the run spent — per pass, per phase, per tab
    shared/                what two or more tabs need: snippets and diffs, the diagram
                           gallery, the commands the page offers, the masthead, the
                           footer, the scope bar, the post-render rewrites
    assets/                late.css, xref.css and every script, as real files, inlined
                           verbatim by shared/assets.py
      css/                 the base stylesheet, one file per module: core.css (the page's
                           vocabulary), one <tab>.css per tab, one <module>.css per shared
                           module, frame.css (tab strip, panels, footer) emitted last
```

**A change to one tab is made in that tab's module, and its style in that tab's
stylesheet, `assets/css/<tab>.css`.** That is the whole point of the split: two agents
working on two tabs are editing two Python files and two CSS files and never rebase over
each other. `page.css` used to be the one place they still collided — fifteen hundred
lines with every tab's rules interleaved — so it is gone: `shared/assets.py` concatenates
`css/*.css` in the order `CSS_FILES` declares, core first, frame last, shared modules
before the tabs that reuse their classes. A rule goes in the file of the module that
*emits* the class; a class two tabs emit goes in `core.css`. A new file is on the page only
once it is named in `CSS_FILES` — `test_build_split_identity.py` fails a file that is not.
**`shared/` and `css/core.css` are touched by one agent at a time** — they are the part
where agents can still collide, and a change there is a change to every tab at once.

Five tabs have no module, and that is not an omission. **API contract**, **Data model**,
**Structure**, **UX** and **Complexity** are `includeHtml` fragments rendered whole by
their own producer (`openapi-compat.py`, `schema_tree.py`, `c2-from-sequence.py`,
`ds-audit.py`, `endpoint-complexity.py`) and pasted into the panel. A change to what one
of those tabs shows is a change to its producer.

`build-review-html.py` re-exports every name the package defines, because it is the import
surface the rest of the skill has always used — `ds-audit.py`, `serve-review.py` and two
dozen test modules load it by path and reach for a function on it. Three names are the
exception and must be read and patched on the module that owns them, never through the
re-export: `OFFLINE` (`tabs/logging.py`) and `_LIST_OFFSET` / `_LEDE_SHOWN`
(`tabs/review.py`). A re-export copies a value; those three move while a build runs.

`test_build_split_identity.py` holds the split to all of this: the assets round-trip byte
for byte, the CSS and the scripts are emitted in the order they always were, every tab
module is reachable, no name has two homes, and nothing the package defines fell off the
orchestrator.
