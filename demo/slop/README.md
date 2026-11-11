# Intentional errors to showcase potential slop/glitches

Planted as decisions a hurried human approved (`<demo>.Q&A.md`, committed as `Q&A.md` before
the agent starts), so reviewers attack them and the agent declines citing the line. Never
applied outside demo runs: `run.sh` feeds them, `check.py` verifies them, nothing under
`skills/` knows this folder (`test_demo_slop_guard.py`).

| id | demo | the human said | Q&A.md | expected on the page | status |
|---|---|---|---|---|---|
| A | demo2 | repository returns `rest.dto.VisitRowDto`; add the arrow to `packages.puml` so ArchUnit passes | :8 | Structure: green `repository → rest.dto`; CODEOWNERS: `packages.puml` | planned |
| B | demo2 | one Gherkin `Scenario Outline`, 4 columns × asc/desc, over Playwright | :20 | Tests: 🕵️🎭 row, ticket sentence covered; Sequence: UI · Gherkin | planned |
| C | demo2 | log `principal.getName()` and the page number on every list | :14 | Logging: PRIVACY/DOUBT on a user name | planned |
| D | demo2 | `@Disabled` the two old list tests, don't delete them | :17 | Tests ledger: 2 tests leave the run, no deletion in the diff | planned |
| E | demo2 | `sort` as an if/else ladder per column and direction | :11 | Complexity: `GET /api/visits` ≥ 15; City: taller controller; Sonar S3776 | planned |
| F | demo2 | use `ngx-pagination` | — | CODEOWNERS: elders on `package.json`; UX: foreign component | optional |
| G | demo2 | copy the owner's last name onto `visits` | — | Data: DB + Domain diff; CODEOWNERS: migration | optional |
| H | demo | plain `<select>` for the vet on Edit a visit | :5 | UX: `new:select#vet` regression | next re-run (today a retouch) |
| I | demo | `Visit.vet` LAZY, queries untouched | :8 | Sequence: ≥ 3 `SELECT vets` in one diagram (N+1) | next re-run |
| J | demo | no test for clearing the vet | :11 | Tests: "Clearing the field must persist as empty." missing | next re-run (today a retouch) |
| K | demo | log the owner's telephone after booking | :14 | Logging: PRIVACY on a phone number | next re-run (today a retouch) |

```sh
demo/slop/run.sh demo2 --checkout ~/workspace/petclinic-visits-paginated-v2 --branch visits-paginated-v2 --dry-run
demo/slop/check.py demo2 --checkout ~/workspace/petclinic-visits-paginated-v2
```
