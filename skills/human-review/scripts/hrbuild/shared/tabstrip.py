"""The tab strip's own arithmetic: {{tabcount}} and the enumeration check."""
from __future__ import annotations

import html
import sys

# The summary walks the reader through the strip — "Eleven tabs, one question each, start
# on Review, then …". Written by hand it is a second copy of the strip, and the second copy
# is the one that rots: a tab added at the end of `tabs` leaves the sentence saying "Ten"
# and skipping the newcomer, and nothing anywhere complains. So the number is a token the
# build fills in from the tabs it actually emitted, and the names are checked against the
# same list.
TAB_COUNT_TOKEN = "{{tabcount}}"
NUMBER_WORDS = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve "
                "Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen Twenty").split()


def spelled(n: int) -> str:
    return NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)


def check_tab_enumeration(lede: str, labels: list[str]) -> None:
    """Warn when the lede's walk-through has drifted from the strip it describes.

    Not a build failure: prose is judgement, and a lede may legitimately group two tabs
    into one clause or leave a self-evident one out. But it may not do so *by accident*,
    which is what silence would make indistinguishable from a rotted sentence."""
    if not lede:
        return
    seen, missing = [], []
    for label in labels:
        # A label may carry a marker the prose has no business repeating — "🤖 Review"
        # on the pill, "Review" in the sentence. Match on the words, not the badge.
        words = label.lstrip("".join(c for c in label if not c.isalnum())).strip()
        at = lede.find(html.escape(words or label))
        (seen if at >= 0 else missing).append((at, label))
    if missing:
        print("[review] WARNING: the summary never names these tabs: "
              + ", ".join(l for _, l in missing)
              + f" — the strip has {len(labels)} of them and the summary walks "
                f"through {len(seen)}.",
              file=sys.stderr)
    out_of_order = [l for (a, l), (b, _) in zip(seen[1:], seen) if a < b]
    if out_of_order:
        print("[review] WARNING: the summary names tabs in a different order than the "
              "strip does, from: " + ", ".join(out_of_order), file=sys.stderr)
