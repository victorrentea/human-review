"""One module per tab of the review page.

    review.py     the findings, the assumptions, the applied fixes, the aftermath band
    sequence.py   a sequence diagram paired with the test that draws it
    tests.py      the test ledger, the requirement lists, the Playwright recordings
    demo.py       the feature film, its captions and its verdict
    city.py       the Code City shot
    logging.py    what the branch logs, and whether a logged value is a privacy problem
    owners.py     the CODEOWNERS verdict
    cost.py       what the run spent — per pass, per phase, per tab

Five tabs have no module here, and that is not an omission: **API contract**, **Data
model**, **Structure**, **UX** and **Complexity** are `includeHtml` fragments, rendered
whole by their own producer next door (`openapi-compat.py`, `schema_tree.py`,
`c2-from-sequence.py`, `ds-audit.py`, `endpoint-complexity.py`) and pasted into the panel
by the orchestrator. A change to what one of those tabs shows is a change to its
producer, not to this package.
"""
