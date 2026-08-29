#!/usr/bin/env python3
"""One tooltip component, not four.

The native `title` is the cheapest thing in the world to add and the hardest to
notice later: it cannot be styled or sized, and it waits half a second — long
enough that a reviewer reads the icon, gives up, and moves on. This pipeline has
exactly one tooltip (`TIP_JS` in `build-review-html.py`), driven by `data-tip`.

Without a guard the drift is always additive — somebody adds one native tooltip
in a hurry — so nothing ever looks broken enough to trigger a cleanup.

Run it directly (`python3 test_tooltips.py`) or under pytest.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The attribute and the property: people grep for `title="` and never for `.title =`.
OFFENDERS = (
    (re.compile(r'<[a-zA-Z][^>]*?\stitle="'), 'a native title="…" attribute'),
    (re.compile(r"\.title\s*="), "a .title = … assignment"),
)
# build-review-html.py owns the rewrite that removes them, so it names them on purpose.
EXEMPT = {"test_tooltips.py", "build-review-html.py"}


def offences() -> list:
    found = []
    for path in sorted(HERE.glob("*.py")) + sorted(HERE.glob("*.js")):
        if path.name in EXEMPT:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, what in OFFENDERS:
                if pattern.search(line):
                    found.append(f"{path.name}:{n} — {what}: {line.strip()[:90]}")
    return found


def test_no_native_tooltips():
    found = offences()
    assert not found, (
        "Native tooltips found. Emit data-tip=\"…\" instead — the page's own tooltip "
        "component picks it up with no registration step:\n  " + "\n  ".join(found)
    )


if __name__ == "__main__":
    hits = offences()
    for hit in hits:
        print(hit, file=sys.stderr)
    print(f"[tooltips] {len(hits)} native tooltip(s)", file=sys.stderr)
    raise SystemExit(1 if hits else 0)
