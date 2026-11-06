"""Small facts and helpers every other module leans on."""
from __future__ import annotations

import contextlib
import contextvars
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


#: What git has answered during this build, or None outside one.
_GIT_ANSWERS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("git_answers",
                                                                           default=None)


@contextlib.contextmanager
def git_answers_held():
    """Ask git each question once for as long as this runs.

    A page asks the same few things from every card: 93 of the 237 git processes of a build
    were repeats. The tree and the refs hold still for the length of a build, and only for
    that, so this wraps `main` and nothing else: a test or a tool that commits between two
    calls gets two answers."""
    token = _GIT_ANSWERS.set({})
    try:
        yield
    finally:
        _GIT_ANSWERS.reset(token)


def forget_git_answers() -> None:
    """The one thing a build does that moves a ref is fetching the base."""
    held = _GIT_ANSWERS.get()
    if held is not None:
        held.clear()


def _git_run(root: Path, *args: str, binary: bool = False,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    """A read-only git command, whole: for a caller that needs the exit code itself or the
    file's bytes. Git that cannot start reads as a failed command (127)."""
    held = _GIT_ANSWERS.get()
    key = (str(root), args, binary)
    if held is not None and key in held:
        return held[key]
    decoding = {} if binary else {"text": True, "errors": "replace"}
    try:
        got = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                             timeout=timeout, **decoding)
    except (OSError, subprocess.SubprocessError):
        nothing = b"" if binary else ""
        got = subprocess.CompletedProcess(args, 127, nothing, nothing)
    if held is not None:
        held[key] = got
    return got


def _git(root: Path | None, *args: str, strip: bool = True,
         timeout: float | None = None) -> str | None:
    """A git command whose failure is an answer, not an exception.

    Every caller here is asking a question that can legitimately have no answer -- a ref
    that does not exist locally, a range that cannot be walked -- and each of them turns
    `None` into a dropped chip or a dropped warning rather than a wrong one. `strip=False`
    keeps a file's own leading lines, and with them its line numbers.
    """
    if root is None:
        return None
    got = _git_run(root, *args, timeout=timeout)
    if got.returncode != 0:
        return None
    return got.stdout.strip() if strip else got.stdout


def repo_root() -> Path:
    """The checkout the current directory sits in. There is nothing to do outside one."""
    top = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if top is None:
        raise SystemExit("[review] not inside a git repository")
    return Path(top)
