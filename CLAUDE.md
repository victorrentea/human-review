# human-review (Ludvig fork) — how to work in this repo

This is the **Ludvig fork** of `/human-review` (upstream: victorrentea/human-review). Its
branch `ludvig` ports the skill from the Java/Spring/Maven project it was born on to
Ludvig's stack: Python 3.11 on AWS Lambda (Serverless Framework v3, CDK), SQLAlchemy +
Alembic on Aurora Postgres, pytest, aws-lambda-powertools, CircleCI, GitHub
(`LRFKonsult`), and the React/Vite/TypeScript frontend in `mitt-ludvig`. The pilot target
is `activities-backend`. The Ludvig repos are checked out next to this one
(`~/dev/sources/<repo>`).

Decisions that hold on this branch:

- **Java support is removed, not kept beside Python.** Staying mergeable with upstream is
  not a goal. When a tab is ported, its Java reader goes.
- **Pages are local only.** Ludvig code is private: never run `publish-demo.sh`, never push
  a page or a screenshot of one anywhere public.
- **The review tool never pushes a Ludvig branch.** `preflight.py` reads CI (CircleCI
  commit statuses / check runs) for a commit that is already on the remote and calls an
  unpushed one unproven; `"ci": {"push": true}` is the only way to let it push.
- **Kept:** the VS Code extension. **Postponed:** the IntelliJ plugin, Code City, the Demo
  film and the UX audit — their tabs fade when they have nothing.

Setup, configuration and what each tab shows at Ludvig: [`docs/ludvig.md`](docs/ludvig.md).

## The suite: Python ≥ 3.12, in `.venv`

The scripts use PEP 701 f-strings, so they need Python 3.12+ (the Ludvig services run 3.11;
the tool does not run inside them). One venv at the repo root, gitignored:

```sh
uv venv -p 3.13 .venv && VIRTUAL_ENV=.venv uv pip install -r requirements-dev.txt
export PATH=$PWD/.venv/bin:$PATH        # python3, pytest and ast-grep come from here
cd skills/human-review/scripts && python3 -m pytest -q
```

`conftest.py` spreads the suite over every core; the whole run is under a minute. Run it
all, not only the tests near a change. On this machine `sg` is shadow-utils, not ast-grep —
the venv's `ast-grep` is the one the logging scan calls.

## Desktop calls go through `scripts/desktop.py`

The server opens URLs, starts the editor and reveals files through `desktop.py`, which knows
macOS (`open`), WSL (`wslview`, `explorer.exe`, `vscode://vscode-remote/wsl+<distro>/…`) and
Linux (`xdg-open`). Never call `open`/`osascript` directly. The suite pins
`HUMAN_REVIEW_DESKTOP=mac` so the server's tests read the same everywhere; `test_desktop.py`
covers WSL and Linux.

## Every visible change is explained in its card's ⓘ panel

Anything a reviewer can newly see on a card — a colour, a bar, a column, a control, what a
hover or a click does — gets a line in that card's ⓘ explainer (`EXPLAIN` in
`scripts/hrbuild/shared/adopt.py`) in the same commit, and a tooltip that shows numbers
says in its first line what they mean (Victor, 14 Oct 2026: "they won't fully get it").
`test_adopt.py` keeps the panel short: at most six one-sentence bullets of 90 characters.

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
dozen test modules load it by path and reach for a function on it. Four names are the
exception and must be read and patched on the module that owns them, never through the
re-export: `OFFLINE` (`tabs/logging.py`), `_LIST_OFFSET` / `_LEDE_SHOWN`
(`tabs/review.py`) and `SNIPPET_BASE` (`shared/snippets.py`, moved by `set_diff_base` to the
page's one base, `shared/chips.py:page_base`). A re-export copies a value; those four move
while a build runs.

`test_build_split_identity.py` holds the split to all of this: the assets round-trip byte
for byte, the CSS and the scripts are emitted in the order they always were, every tab
module is reachable, no name has two homes, and nothing the package defines fell off the
orchestrator.

## Everything about the forge goes through `hrbuild/shared/forge.py`

The forge hosting the *reviewed* repository (GitHub; GitLab through `glab`, `GitLabForge`,
all but the IDE's PR links; or Bitbucket Server / Data Center, `BitbucketServerForge`, comment push and Jira
still missing; issue #4) is one module: its web URLs (`forge.blob_url`, `commit_url`, `tree_url`, `compare_url`,
`issue_url`…), the recognisers for its URLs (`find_pull`, `pull_root`, `is_comment_url`…)
and every `gh` or `glab` call (`api_forge().issue(…)`, `.ci_runs(…)`, `.pr_api()`…). Get a forge
with `forge_for(root)` (from `origin`; None off a forge, keep the editor link) or
`forge_at(pr["repo"])`, and the word for a link from `forge.name`. New code that touches
GitHub goes there, never into a tab or a script: `test_forge_seam.py` fails a `gh` or `glab` call or
a `https://github.com/` URL of the reviewed repo spelled anywhere else. A method added to
`GitHubForge` gets its GitLab and Bitbucket twins in the same commit
(`test_forge_gitlab.py`, `test_forge_bitbucket.py`), or such a checkout hits `gh` with a
GitHub-shaped question. A GitHub-shaped comment call stays in that shape: `GitLabPrApi`
translates it, so `push-pr-comments.py` keeps planning in GitHub's words. Links to the
skill's own home (`victorrentea/human-review`) are not the forge and stay where they are.

## Everything a build asks git goes through `hrbuild/shared/util.py`

`_git(root, …)` answers with stdout, or None; `_git_run(root, …)` with the whole result, for a
caller that needs the exit code or a file's bytes. `main` wraps the build in
`git_answers_held()`, so a question asked from every card reaches git once. That holds only
because a build moves nothing: anything it does that moves a ref has to call
`forget_git_answers()`, as `fetch_base` does. `test_git_seam.py` fails a `subprocess.run(["git"`
anywhere else in the build's code; `forge.py` keeps its own, and `run-steps.py` is another
program, whose answers must not outlive a step.
