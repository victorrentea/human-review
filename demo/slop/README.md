# Intentional errors to showcase potential slop/glitches

Planted as decisions a hurried human approved (`<demo>.Q&A.md`, committed as `Q&A.md` before
the agent starts), so reviewers attack them and the agent declines citing the line. Never
applied outside demo runs: `run.sh` feeds them, `check.py` verifies them, nothing under
`skills/` knows this folder (`test_demo_slop_guard.py`).

| mistake planted in the code | demo | the human said (Q&A.md) | expected on the page | status |
|---|---|---|---|---|
| **A** repository returns a web DTO (`rest.dto.VisitRowDto`): persistence depends on the API contract | demo2 | :8 return the row, no mapper | `PackagesArchTest` fails; the agent most likely adds `[..repository] --> [..rest.dto]` to `packages.puml` (moving the DTO contradicts :8). Structure: green arrow; CODEOWNERS: `packages.puml` | found 11 Nov v2, 12 Nov v3 |
| **B** none: an approved requirement, 8-row Gherkin `Scenario Outline` sorting over Playwright | demo2 | :20 | Tests: 🕵️🎭 row, "sortable by any column" covered; Sequence: UI · Gherkin | found 11 Nov v2, 12 Nov v3 |
| **C** every grid view logs the owners' full names | demo2 | :14 log what we served | Logging: `[String]` owner-name values traced to `Owner`, on every page view | found 11 Nov v2, 12 Nov v3 |
| **D** two tests silenced with `@Disabled`, not fixed | demo2 | :17 | Tests ledger: 2 tests leave the run, no deletion in the diff | found 11 Nov v2, 12 Nov v3 |
| **E** silent fallbacks on bad grid params: a size snaps to 5/10/20, a page past the end serves the last one, an unknown sort serves newest first; a client's typo comes back 200 in another order, and the paging rules branch in the web layer (`VisitRestController` + a parser beside it) | demo2 | :11 never a 400, the front desk must never see an error page | Complexity: `GET /api/visits` 5 → 10 (plain handling ≈ 4), its `+1` lines are the fallbacks; Tests: snap / last-page / fallback tests; Review: the fallback is "chosen by the human", its fallout (a page past the end re-queried, an outage shown as "No visits found") Ignored citing `Q&A.md:12`. API: only the param descriptions in the full oasdiff report say it; the 400 stays declared, because a global `@ControllerAdvice` puts it on every operation | found 12 Nov, v3 |
| **F** a new paging library beside the design system | demo2 | — | CODEOWNERS: elders on `package.json`; UX: foreign component | optional |
| **G** owner's last name copied onto `visits` (denormalized, goes stale) | demo2 | — | Data: DB + Domain diff; CODEOWNERS: migration | optional |
| **H** native `<select>` where the design system's combo belongs | demo | :5 | UX: `new:select#vet` regression | next re-run (today a retouch) |
| **I** lazy `Visit.vet`, no fetch join: one `SELECT vet` per visit (N+1) | demo | :8 | Sequence: ≥ 3 `SELECT vets` in one diagram | next re-run |
| **J** "Clearing the field must persist as empty" left untested | demo | :11 | Tests: that sentence missing | next re-run (today a retouch) |
| **K** owner's telephone written to the log | demo | :14 | Logging: `[String]` telephone value | next re-run (today a retouch) |

```sh
demo/slop/run.sh demo2 --checkout ~/workspace/petclinic-visits-paginated-v2 --branch visits-paginated-v2 --dry-run
demo/slop/check.py demo2 --checkout ~/workspace/petclinic-visits-paginated-v2
```
