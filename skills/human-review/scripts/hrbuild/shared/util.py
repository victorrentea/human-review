"""Small facts and helpers every other module leans on."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: The directory the scripts live in — `.../skills/human-review/scripts`, three levels up
#: from this file. Everything the build shells out to (extract-snippet.py,
#: codeowners-check.py, serve-review.py, …) is found relative to it, so it has to keep
#: naming the scripts directory now that the page's code lives in a package under it.
HERE = Path(__file__).resolve().parents[2]

EXTRACT = HERE / "extract-snippet.py"
CODEOWNERS = HERE / "codeowners-check.py"
TESTCHANGES = HERE / "test-changes.py"

# The scope bar's third sign, beside `+` and `−`. It was `±`, which everywhere else a
# reader has met it means a *range* — "forty, give or take" — while the count of edited
# files is exact. A pencil says "someone went in and changed these", which is the fact,
# and it is the mark the page already uses for an edited row further down. ✏️, not the
# ✍️ writing hand it was until 20 Oct 2026 (Victor: "replace the hand with a pencil").
PENCIL = "\u270f\ufe0f"

#: The order the kinds of test are listed in, wherever the page lists them by kind: the
#: Tests card's chapters (sent to `testchapters.js` in its `rm-data` blob) and the Sequence
#: tab's pairs. One tuple, so the two tabs cannot drift apart (Victor, 29 Oct 2026: E2E
#: first, then API, "the same order" on both).
TEST_KIND_ORDER = ("e2e", "api", "unit")


#: A diagram whose file name is an abbreviation a heading should spell out: the card
#: drawn from `DB.puml` is titled "Database" (Victor, 7 Oct 2026). The file link beside
#: the title keeps the file's own name.
SPELLED_OUT = {"DB": "Database"}


def _pretty(name: str) -> str:
    """`DomainModel` is a filename; `Domain Model` is a heading. Split the camel hump,
    which leaves other acronyms and already-spaced names untouched."""
    return SPELLED_OUT.get(name) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _git(root: Path, *args: str) -> str | None:
    """A git command whose failure is an answer, not an exception.

    Every caller here is asking a question that can legitimately have no answer -- a ref
    that does not exist locally, a range that cannot be walked -- and each of them turns
    `None` into a dropped chip or a dropped warning rather than a wrong one.
    """
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None
