# #37 Link Visit with Vet: answers to the open questions

Answered by the team lead before implementation. Decided; build to them.

## Q1. Which control picks the vet?
Book a visit: our `<app-combo>`, like the other pickers. Edit a visit: a plain `<select id="vet">` with `-- none --` and the vet names; it is an admin screen with six vets, the combo is overkill there.

## Q2. How is the vet loaded with a visit?
`@ManyToOne(fetch = FetchType.LAZY)` on `Visit.vet`, and leave the existing visit queries as they are: no extra join fetch. The vet is optional and most screens never touch it.

## Q3. Which tests for the vet on a visit?
Book with a vet, book without one, change it on edit. Skip a test for clearing it: clearing is just setting null.

## Q4. Do we log anything?
Yes, support keeps asking where the booking SMS went. In `OwnerRestController.bookVisit`, after the save: `log.info("Visit {} booked, SMS to {}", visit.getId(), owner.getTelephone())`.
