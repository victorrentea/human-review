# Backlog — Victor's review-page requests (23–24 Sep 2026)

Status: ✅ done · 🔄 in progress · ⏸ blocked / needs a decision · 💬 discussion only

## Code City (repo `code-city`)
- ✅ Change DNA colour: axes of change as a bar on the near edge of each package; top level first, children on zoom (`17e33d0`)
- ✅ Breadcrumb moved to bottom centre (it slid over the panel)
- ✅ "Build for your own repo" overlay removed; corner = link to the repo + credit, no shadow
- ✅ petclinic main + test-pr cities regenerated with it

## Masthead / tab strip / run buttons
- ✅ ↻ / ↻+AI buttons: emoji only, fix contrast (hover, no hover, green arrow)
- ✅ tab headers look like real tabs (another session is on frame.css / masthead.css)
- ✅ red "checkbox" → a normal white checkbox
- ✅ grade badge gets a label ("grade 6/10"); click → Review tab, which shows a ~7-words-per-bullet "why this grade" panel
- ✅ "Start" → "Start App in Docker"
- ✅ three refresh modes, one icon each: ↺ regenerate report · ⏳ re-run tests · 🤖 re-evaluate (semantic test mapping; full code review $$$). The Tests side of ⏳ is in (`3d97b4a`); the shared wiring comes after the masthead work

## Review tab
- ✅ numbering restarts at 1 in every section (auto-fixed, assumptions, …)
- ✅ open review issues sorted by severity (worst first)
- ✅ rounds made explicit (Round III appears once a second review is recorded): I assumptions while coding · II code review + auto-fix/raise · ± human corrections · III (optional) round-2 review + auto-fix/raise; round 1 stays visible
- ✅ review-points.md much shorter (`dc58cd1`) (Victor skims it and jumps into the code)
- ✅ skill rule: the coder's auto-fix commits carry `[auto-fix]` in the message (do NOT re-run review & fix now)
- ✅ post the findings as inline PR comments: "Push to GitHub PR" button + push-pr-comments.py (`f5e47a5`); never pressed yet on GitHub, linked from the page

## Tests tab
- ✅ "🤖 Semantic test coverage — paired with the ticket by AI" (`3d97b4a`)
- ⏸ deterministic "tests executing code changed by this PR" (per-test coverage), see the proposal from the Tests agent (~1.5–5 days)

## Logs tab
- ✅ no LLM at all (`b625935`): list the added/modified log lines, each argument with an IntelliJ-style inline type hint (`[String] vet.getLastName()`), plus the expression that declared the variable

## Complexity tab
- ✅ left-to-right call graph with cognitive/cyclomatic on each node, ▸ in front of GET, tighter gap, "HTTP /" gone, badges on methods whose complexity went up (`04acbf8`)

## UX tab
- ✅ one-line collapsible header per screen; "frame the changes" checkbox (row-diff clustering, no AI) (`51884d0`)
- 💬 pick one of three framing renderings (A frames · B spotlight · C gutter)

## API / Structure / City wording
- ✅ "PR impact on code size, complexity, coupling, …" · C2 "Diagram synthesized from sequence diagrams of the test traces" · "expand 25 impacted" (`27219fd`, `30ba748`)
- ✅ "All boxes and lines are ArchUnit-tested vs code" (`19e93bcc`), on main and test-pr
- ⏸ `openapi-visual-diff.py` has to be copied to the public `OpenAPI-Visual-Diff` repo (drift test fails until then)

## Data tab — conceptual model
- ✅ Vet + Specialty drawn twice (`9037413e`); the draw.io on disk ≠ the rendered one; make the draw.io test pass
- ✅ caption: "[This diagram] unit-tested against Java Domain Model. Update it in draw.io"

## Cost tab
- ✅ "post-review fixes $7.45 / 12.7M": how much went on the actual fixes vs writing review-points?
- ✅ the "view images $7.29" row is the UX step, which uses no model; the cost is charged by wall-clock overlap with other turns, so rename the row and charge only turns that actually ran the step

## Sequences / C2 (petclinic)
- ✅ notification-service merged into main (`61998a8d`) and test-pr (`c0b9503a`); sequences re-recorded in docker (`32c943ad`); was: notification-service is on the unmerged branch `origin/notification-service`, not on main. Once it lands on main: merge main into test-pr, re-run the traced tests, re-render the sequences and C2 (a new container between Backend and the SMS gateway)

## Later additions (24 Sep)
- ✅ UX: "frame the changes" right after New/Old · intro text removed · "1 introduced by this branch" (`334afe0`)
- ✅ Complexity: bigger ▶/▼ (`38d9102`) · CODEOWNERS: file heading linking to it (`db98413`)
- ✅ Served badge, Serve tooltip, ↺ tooltip, counter-clockwise spin · red checkbox → plain (`bf22549`)
- ⏸ Review tab: a real "🤖 full code review" re-run command does not exist yet
- ⏸ book-visit-with-vet.feature's sequence lost its opening GET /api/owners/{ownerId} (already thin before)
