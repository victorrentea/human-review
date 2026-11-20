#!/usr/bin/env python3
"""Draw a found message as the lifeline activating itself.

    seq_found_as_self.py <in.puml> <out.puml>     (the same path twice rewrites in place)

A browser request starts in a frontend method, and the tracer writes that method as a
*found* message, an arrow from the diagram's edge: `[-> Browser: OwnerService.getOwners`.
On the page that reads as an arrow out of nowhere. Victor, 20 Nov 2026: draw it like any
other method call on a lifeline, a self-call that opens its own activation bar —
`Browser -> Browser: OwnerService.getOwners` — the shape `book-visit` has on the Backend.

Only the picture changes. The `.puml` the generator wrote keeps its `[->`, because every
reader of it (`c2-from-sequence.py`, `run-steps.py`, the Sequence tab) knows that edge as
"where the frontend method starts", not as a lifeline. So this runs on the copy that is
rendered, the New/Old renders and the delta alike, after the delta has marked its lines:
a green `[-[#2E7D32]> Browser` keeps its colour as `Browser -[#2E7D32]> Browser`.
A message back to the edge (`[<- Browser`, `Browser ->]`) is left as it is.
"""
from __future__ import annotations

import re
import sys

FOUND = re.compile(r'^(?P<lead>\s*)\[(?P<arrow>-+(?:\[[^\]]*\])?-*>{1,2})\s*'
                   r'(?P<to>"[^"]*"|[^\s:"]+)\s*:(?P<rest>.*)$')


def rewrite(text: str) -> str:
    def one(line: str) -> str:
        m = FOUND.match(line)
        return f'{m["lead"]}{m["to"]} {m["arrow"]} {m["to"]}:{m["rest"]}' if m else line
    out = "\n".join(one(l) for l in text.splitlines())
    return out + ("\n" if text.endswith("\n") else "")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    src = open(argv[1], encoding="utf-8").read()
    with open(argv[2], "w", encoding="utf-8") as f:
        f.write(rewrite(src))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
