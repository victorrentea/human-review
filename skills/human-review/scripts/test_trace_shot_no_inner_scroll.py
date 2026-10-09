"""The Grafana trace under "These diagrams were captured from OpenTelemetry traces…" is shown
at its own height, never in a frame that scrolls inside the page (Victor, 9 Oct 2026: "the
scroll bar shouldn't be here"). Neither on the tab nor inside the (i) box it moves into."""
import re
from pathlib import Path

CSS = (Path(__file__).parent / "hrbuild" / "assets" / "css" / "sequence.css").read_text(encoding="utf-8")


def _rules(selector_part: str) -> list[str]:
    body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    return [decl for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", body)
            if selector_part in sel]


def test_the_trace_frame_has_no_height_cap_and_does_not_scroll():
    rules = _rules(".seqhow-img")
    assert rules, "the trace frame lost its rule"
    for decl in rules:
        assert "max-height" not in decl, f"the trace frame is capped again: {decl.strip()}"
        assert not re.search(r"overflow(-y)?\s*:\s*(auto|scroll)", decl), \
            f"the trace frame scrolls inside the page again: {decl.strip()}"
