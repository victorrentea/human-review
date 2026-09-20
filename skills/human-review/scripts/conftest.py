"""What the tests next door share.

There is one thing, and it exists because the page builder stopped being one file. Half a
dozen tests are *guardrails over the source text* — "only one place emits this control",
"the green in the palette is the same green the diff painter uses", "every key
`review-points.py` writes is a key the renderer reads". They used to read
`build-review-html.py` and get everything the page is made of, because everything the
page is made of was in it.

It is now a package (`hrbuild/`), so reading that one file gets the orchestration and
none of the rendering. `page_source()` gives those tests what they were actually asking
for: every line the page is built from, in one string.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent


def page_source() -> str:
    """The orchestrator, every module of `hrbuild/`, and every asset it inlines.

    Concatenated rather than parsed: these are grep-shaped guardrails, and a guardrail
    that has to understand Python to fire is a guardrail that stops firing the first time
    somebody writes the thing it forbids in a way it did not anticipate."""
    pkg = HERE / "hrbuild"
    parts = [(HERE / "build-review-html.py").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted(pkg.rglob("*.py"))]
    parts += [p.read_text(encoding="utf-8") for p in sorted((pkg / "assets").rglob("*")) if p.is_file()]
    return "\n".join(parts)
