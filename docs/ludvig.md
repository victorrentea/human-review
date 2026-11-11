# human-review at Ludvig

The `ludvig` branch of this fork runs `/human-review` on Ludvig's services: Python 3.11 on
AWS Lambda (Serverless Framework v3), SQLAlchemy + Alembic, pytest, aws-lambda-powertools,
CircleCI and GitHub — and the React/Vite front end of mitt-ludvig. Pages are built and read
locally; nothing is published.

## What each tab shows on a Ludvig repository

| Tab | Source | Needs |
| --- | --- | --- |
| **Review** | `review-points.md`, written by `/record-review` in the coding session (`/code-review` + `ludvig:code-review`) | — |
| **Tests** | the test-change manifest; per-test coverage of the pytest suite (`testcov.pytest`) and of Vitest (`testcov.vitest`); the ticket from Jira | `coverage` in the service's env; Jira token |
| **API** | OpenAPI derived from `serverless*.yml` and the handlers, both sides, diffed by oasdiff | `oasdiff` |
| **Data** | ER diagram from the SQLAlchemy models, both sides; the Alembic migrations card | `plantuml` |
| **Sequence** | the branch's own tests traced by the `hr_genseq` pytest plugin | `steps.sequence` |
| **Structure** | Packages (Python imports) and Containers (`serverless*.yml`), both sides | `plantuml` |
| **Complexity** | cognitive complexity per entry point (HTTP route, queue, schedule, invoked function) | — |
| **Logs** | every log statement the branch adds or changes, with ⚠ chips from Ludvig's logging standard | — |
| **Cost** | what writing and reviewing the change cost | — |
| CODEOWNERS | hidden: no Ludvig repo has one | — |
| Demo, UX, Code City | postponed | — |

The CI gate reads CircleCI's commit statuses on GitHub and **never pushes**: an unpushed
commit is reviewed and marked unproven.

## One-time setup (WSL / Linux)

```sh
cd ~/dev/sources/human-review && git checkout ludvig

# the skill's own Python (the scripts need >= 3.12; the services stay on 3.11)
uv venv -p 3.13 .venv && VIRTUAL_ENV=.venv uv pip install -r requirements-dev.txt

# oasdiff (API tab) — a single binary
gh release download -R oasdiff/oasdiff --pattern '*linux_amd64.tar.gz' -D /tmp/oasdiff
tar xzf /tmp/oasdiff/*.tar.gz -C /tmp/oasdiff && install -m 755 /tmp/oasdiff/oasdiff ~/.local/bin/

# PlantUML (Data and Structure tabs): a JRE and the jar, no sudo; Smetana layout without Graphviz
D=~/.local/share/human-review-tools && mkdir -p $D && cd $D
curl -sSL -o jre.tgz https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jre/hotspot/normal/eclipse
tar xzf jre.tgz && rm jre.tgz
gh release download -R plantuml/plantuml --pattern 'plantuml-1*.jar' -D $D
rm -f $D/plantuml-*-javadoc.jar $D/plantuml-*-sources.jar
# then a ~/.local/bin/plantuml wrapper running `java -jar … -Playout=smetana` when `dot` is absent

# the VS Code extension: clicks on the page open the right line in the right window
code --install-extension vscode-extension/dist/human-review.vsix

# the skills, for Claude Code
ln -s ~/dev/sources/human-review/skills/human-review  ~/.claude/skills/human-review
ln -s ~/dev/sources/human-review/skills/record-review ~/.claude/skills/record-review
```

Optional:

- **Jira** (the Tests tab's ticket): `~/.config/human-review/jira.env` with
  `JIRA_BASE_URL=https://<site>.atlassian.net`, `JIRA_EMAIL=…`, `JIRA_API_TOKEN=…`.
- **Browser tests of the page itself** (only for working on this repository):
  `sudo .venv/bin/playwright install-deps chromium`.

## Per repository

Copy [`skills/human-review/human-review.ludvig.json`](../skills/human-review/human-review.ludvig.json)
to the repository's root as `human-review.json` and adjust: the CircleCI job that proves the
build (`ci.workflows`), how to reach the service's Python (`testcov.pytest[].python`,
usually `pipenv run python`), the source folders. To keep it out of the repository, list it
in `.git/info/exclude`. `.human-review/` is excluded there automatically on the first run.

For mitt-ludvig's front end, add a Vitest suite:

```json
"vitest": [{"label": "Vitest", "cwd": "frontend",
            "command": "VITE_STAGE=jest VITE_USE_MOCK_AUTH=true npx vitest run"}]
```

## Running it

In the coding session, after the change: `/record-review` (it writes `review-points.md`).
Then, from the repository root, `/human-review` — or without Claude Code driving it:

```sh
~/dev/sources/human-review/.venv/bin/python3 \
  ~/dev/sources/human-review/skills/human-review/scripts/run-review.py
```

The base is `human-review.json`'s `base`, else the first of `origin/develop`, `origin/main`,
`origin/master` that exists.

## Known limits

- The derived OpenAPI reads what a handler and its own module read from the request; a
  field read three services deep is not in it.
- The call graph behind Complexity binds a call by the class its receiver was built from;
  an untyped receiver binds only to a name one project function alone carries.
- `hr_genseq` draws calls between the service's own classes and to boto3, SQLAlchemy and
  `requests`; a call through `aiohttp` or a mock is not a boundary it sees.
- DynamoDB tables appear on Containers; the Data tab's ER diagram is Aurora's (SQLAlchemy).
