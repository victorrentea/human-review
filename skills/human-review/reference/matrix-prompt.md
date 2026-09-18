# The model step, as a prompt

This is the one paid, non-reproducible piece of `/human-review` that a *button* is
allowed to ask for: the requirements↔tests matrix and the per-test catalogue behind it.
`rerun-model.py` hands this file to `claude -p --model sonnet` at the repository root, so
everything below is addressed to that run and not to a reader.

It is a file rather than a string inside the script for the same reason `content.json` is
a file: it is the prompt, it is reviewed, and a prompt assembled inside a 40-line function
is a prompt nobody reads before it is paid for.

---

You are regenerating the two model-written artifacts of an existing `.human-review/`
report, and nothing else. Work in the repository you are started in.

**Write exactly these, and touch no other file:**

1. `.human-review/test-index/*.json` — the per-test catalogue. One file per suite family
   (`rest.json`, `cucumber.json`, `frontend.json`, `security.json`, …) plus
   `mapping.json`. Each suite file is a JSON array of objects with these keys:
   `id` (`path:line`, exactly as the test is addressable in the tree), `suite`, `title`,
   `asserts` (array of sentences — what the test actually pins, not what its name says),
   `surfaces` (array — the layer it exercises), `strength` (`asserted`, `executed`,
   `selected`, `missing`, or `n/a`), `notes` (what a reviewer cannot see from the test
   body: which requirement it guards, where it is weaker than it looks).
2. `.human-review/assets/requirements-map.html` — the matrix. A single
   `<div class="reqmap">` carrying its own `<style>`, rendering the ticket's requirements
   down one side and the tests that cover each of them on the other, coloured by
   `strength`.

**The rules that make this artifact worth paying for:**

- **Read the existing pair first** and keep their structure, their CSS tokens and their
  vocabulary. This is a regeneration, not a redesign: a matrix that looks different from
  the one the reader saw an hour ago is a matrix they have to learn again. The previous
  copies are kept under `.human-review/.model-prev/` — read them, diff your work against
  them, and change only what the repository changed.
- **Never say a test's state yourself.** Whether a test is new, edited, deleted, commented
  out or sitting under an `@Disabled` is read out of the code by
  `scripts/test-changes.py`, and its manifest is already on the page. Name the test under
  the requirement it pins; let the manifest say what happened to it.
- **Never retype code** and never invent a test id. Every `id` must resolve in the tree.
- **`strength` is a claim about coverage, and the honest answer is usually not the
  flattering one.** A test that executes a path without asserting on it is `executed`, not
  `asserted`. A requirement with nothing behind it is `missing`, and saying so is the whole
  value of the matrix.
- **The requirements come from the ticket**, whose number and body the content file's `pr`
  block names. Read it (`gh issue view`, `gh pr view`) rather than inferring the ticket
  from the diff — a matrix built out of the diff proves only that the diff is
  self-consistent.
- Do **not** touch `content.json`, do **not** write a finding, do **not** run
  `/code-review` or `/simplify`, do **not** commit and do **not** push.

**Do not run the build or the server.** `refresh-report.py` runs after you, from the same
command, and it is what turns these two files into the page.

Finish by printing, in one line, which suites you wrote and how many requirements came
back `missing`.
