"""The queue of banded statements that sits above the page's first heading."""
from __future__ import annotations


#: Bands the Review tab owes the reader before its first heading: what is not recorded,
#: and what changed after the agent stopped. Module state for the same reason
#: `_LIST_OFFSET` is — they are emitted by a leaf of a render tree eight calls deep, and
#: they belong to the *tab*, not to whichever pile happens to open it. `main` fills the
#: list before rendering; the first pile drains it.
_BANDS: list[str] = []
#: Bands that go above the counts line rather than under it: the takeover note, a
#: one-row qualification of which commit every number in that line was counted at.
_TOP_BANDS: list[str] = []


def set_bands(bands, top=()) -> None:
    """Replace the pending bands. Called once per page, beside `reset_list`."""
    _BANDS[:] = [b for b in bands if b]
    _TOP_BANDS[:] = [b for b in top if b]


def _flush_bands() -> str:
    """Every pending band, once. Drained rather than read, so a tab with three piles in it
    does not print the same red band three times."""
    out = "".join(_BANDS)
    _BANDS.clear()
    return out


def _flush_top_bands() -> str:
    out = "".join(_TOP_BANDS)
    _TOP_BANDS.clear()
    return out


def _lede_above(head: str, lede: str) -> str:
    """Above the first pile's heading, not tucked under it.

    The line counts all three piles, so under `Requires human review` it reads as a
    description of the findings and the reader meets `4 coder assumptions to check` as a
    footnote to a heading that has nothing to do with them. Hoisted above, it is what it
    is: the shape of the whole list, before the list starts. Which pile happens to open
    the list is then an editorial choice that cannot move the line.

    The tab's bands land between the two: under the counts line, which is sticky and has
    to stay the topmost thing in the tab, and above the first heading, because what a band
    says ("nothing records this review", "someone changed the code after the agent
    finished") governs how every item under it should be read. The top bands go above
    the counts line: the takeover row says at which commit those counts were taken.
    """
    top = _flush_top_bands()
    return (top + lede + _flush_bands() + head) if lede else (top + _flush_bands() + head)
