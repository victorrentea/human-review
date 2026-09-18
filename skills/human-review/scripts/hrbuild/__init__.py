"""The review page, one module per tab.

`build-review-html.py` next door is the orchestrator: it reads the content file,
validates it, renders each tab's blocks in the order the content file asks for and
writes the document. Everything it renders lives here:

  * `hrbuild/tabs/` — one module per tab of the page. A change to what the Sequence tab
    shows is an edit to `tabs/sequence.py` and to nothing else;
  * `hrbuild/shared/` — what two or more tabs need: snippets and diffs, the diagram
    gallery, the commands the page offers, the masthead, the footer, the scope bar;
  * `hrbuild/assets/` — the stylesheet and the scripts, as real .css and .js files,
    inlined into the page verbatim by `shared/assets.py`.

The rule that keeps the split worth having: **a change to one tab is made in that tab's
module**. `shared/` is the part two agents can collide in, so it is touched by one at a
time.
"""
