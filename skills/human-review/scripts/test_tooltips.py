#!/usr/bin/env python3
"""One tooltip component, not four.

The native `title` is the cheapest thing in the world to add and the hardest to
notice later: it cannot be styled or sized, and it waits half a second — long
enough that a reviewer reads the icon, gives up, and moves on. This pipeline has
exactly one tooltip (`TIP_JS` in `build-review-html.py`), driven by `data-tip`.

Without a guard the drift is always additive — somebody adds one native tooltip
in a hurry — so nothing ever looks broken enough to trigger a cleanup.

There are two halves, and the second is the one that bites. The source scan below can
only see files it is pointed at — and for a long time it globbed `*.py` and `*.js` in this
directory while exempting `build-review-html.py`, which is where every line of the page's
JavaScript actually lives. There are no `.js` files here at all, so the guard inspected
nothing that could plausibly carry a page tooltip and passed vacuously for months. The
product check (`test_built_page_has_no_native_tooltips`) is therefore the real guard: it
renders a page and asserts on the HTML that comes out, which is the only artifact a
reviewer ever sees.

Run it directly (`python3 test_tooltips.py`) or under pytest.
"""
from __future__ import annotations

import json
import re
import subprocess
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


def build_minimal_page(tmp_path):
    """Render a page that exercises every tooltip-emitting code path we own."""
    content = {
        "title": "tooltip guard",
        "summary": "<p>x</p>",
        "verdict": {"score": 5, "label": "x", "bullets": ["y"]},
        "scope": [{"label": "files", "value": "1", "href": "https://example.invalid"}],
        "findings": [{"title": "f", "body": "b", "severity": "high"}],
        "autofixes": [{"title": "a"}],
        "sections": [{"id": "s", "title": "S", "body": "<p>b</p>"}],
        "tabs": [{"id": "review", "label": "Review", "count": True,
                  "blocks": [{"type": "findings"}, {"type": "autofixes"},
                             {"type": "section", "id": "s"}]}],
    }
    src = tmp_path / "content.json"
    src.write_text(json.dumps(content), encoding="utf-8")
    out = tmp_path / "review.html"
    proc = subprocess.run(
        [sys.executable, str(HERE / "build-review-html.py"), str(src), "--out", str(out)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8")


def test_built_page_has_no_native_tooltips(tmp_path):
    """The guard that actually holds: assert on the rendered product.

    Inlined PlantUML is the one legitimate source of a native title (`[[url{hint}]]`
    becomes one inside the SVG), and the builder rewrites those; a page with no diagrams
    must therefore contain none at all."""
    page = build_minimal_page(tmp_path)
    hits = [m.group(0)[:80] for m in re.finditer(r'<[a-zA-Z][^>]*?\stitle="[^"]*"', page)]
    assert not hits, (
        "The built page carries native title=… attributes. Emit data-tip=\"…\" instead:\n  "
        + "\n  ".join(hits))
    assert "data-tip=" in page, (
        "The built page has no data-tip attributes at all — the tooltip component is not "
        "being exercised, so this test would pass no matter what.")


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
