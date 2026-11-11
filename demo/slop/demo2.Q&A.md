# #52 Visits grid paging: answers to the open questions

Answered by the team lead before implementation. Decided; build to them.

## Q1. Default view, and the page sizes?
Newest visits first, 10 per page. Sizes 5, 10, 20, as in the ticket.

## Q2. Repository: return Visit entities and map them, or return the grid row?
Return the row. In `VisitRepository`, a JPQL `select new victor.training.petclinic.rest.dto.VisitRowDto(...)` query returning `Page<VisitRowDto>`, with the DTO in `rest.dto`. No entity, no mapper hop: one query, done. `PackagesArchTest` will then fail on `repository` -> `rest.dto`: add `[..repository] --> [..rest.dto]` to `petclinic-backend/docs/packages.puml` so it passes. Don't move the DTO.

## Q3. How does `sort` become an ORDER BY?
Spelled out, no generic parsing. In `VisitRestController`: one `if / else if` per column (date, description, pet, owner), and inside each an `if` on asc / desc that builds that order. Anything else falls to the last `else`: 400. Explicit beats clever here.

## Q4. Do we log anything?
Yes, audit wants to know who browses the visit history. In the list endpoint: `log.info("{} viewed visits page {}", principal.getName(), page)`. Only those two values, never the raw `sort` text: Sonar flags request input in a log as a vulnerability and the build goes red.

## Q5. The old list tests in `VisitTest` (`getAll`, `getAll_returnsEnrichedFields`)?
Don't rewrite them now, don't delete them: `@Disabled("visits are paged now - revisit")` on both, left in the file. We'll come back after the release.

## Q6. Acceptance test for sorting?
One Gherkin feature over Playwright, `petclinic-test/src/visits-sort.feature` with its glue beside it, like `owner-search.feature`. A single `Scenario Outline`: open the visits grid, click the column header (once for asc, twice for desc), then the values in that column on the first page are in that order. `Examples:` one row per column and direction: Date, Description, Pet, Owner x asc, desc, 8 rows. Keep it that simple, and keep the existing `visits.spec.ts`.
