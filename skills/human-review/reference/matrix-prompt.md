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
  them, and change only what the repository changed. **`.model-prev/` is a byte-identical
  copy of the pair you are about to rewrite, made seconds ago**, so "the current files are
  identical to `.model-prev/`" is true before you have done anything and is never evidence
  that nothing needed doing. The comparison that means something is the pair against the
  *repository*: a test the branch added is in the tree, not in the copy.
- **The two files you write are one artifact.** Every test id `mapping.json` names as
  covering a sentence must have a row in the matrix, and the matrix must name no test the
  catalogue does not. A catalogue that lists a scenario whose row never reached the matrix
  is a page telling the reader a requirement is uncovered while the file beside it says who
  covers it — and it is the failure this step actually had: a Gherkin UI scenario landed in
  `test-index/` and never appeared under *Covering tests*. `rerun-model.py` now refuses a
  run that ends that way, so a matrix left behind is a failed step rather than a quiet one.
- **The frame is not yours to place.** The build re-lays the fragment on every run
  (`hrbuild/tabs/tests.py:reqmap_layout`): the ticket's own title goes over the ticket
  frame, the colour legend moves under it, the UI/API/unit key moves under the card, and
  the two frames are put on one line. Keep the class names — `rm-body`, `rm-text`,
  `rm-side`, `rm-ticket`, `rm-legend`, `rm-cats`, `rm-code` — because they are what that
  rewrite finds its footing by; rename one and it gives up, says so on stderr, and ships
  your layout instead. Do not write a heading over the ticket: one is added, from GitHub,
  and a second would be the page saying the ticket's name twice.
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
