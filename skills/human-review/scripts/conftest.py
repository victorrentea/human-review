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

import os
import site
from pathlib import Path

import pytest

# A build reads the PR's description from GitHub (`attach_pr_body`); the suite's fixtures
# name real PR URLs, and a test must not depend on the network or on what a PR says today.
os.environ.setdefault("HR_NO_GITHUB", "1")

# A build also fetches the base (`fetch_base`) when the checkout it runs in has a network
# remote, and this suite runs in one: every page a test built paid a round trip to GitHub,
# about two thirds of the time a small page takes.
os.environ.setdefault("HUMAN_REVIEW_NO_FETCH", "1")

# The server's desktop calls (`desktop.py`) differ per OS — `open` on macOS, `wslview` on
# WSL, `xdg-open` on Linux. The tests were written against macOS's; pin it so they read the
# same on every machine, and let `test_desktop.py` cover the other two on purpose.
os.environ.setdefault("HUMAN_REVIEW_DESKTOP", "mac")

# Several tests point `HOME` at an empty folder so no real transcript leaks into a cost
# number. Python finds its user site (`~/Library/Python/3.x`) through `HOME`, and that is
# where Pygments and PyYAML live on a Homebrew Python, so such a build failed with "needs
# Pygments" instead of testing anything. Pinning the user base here keeps it found
# whatever `HOME` a test hands its subprocess.
os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())

HERE = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """The whole suite takes eleven minutes on one core and a minute and a half on all of
    them, so it spreads itself out with no flag to remember, however it was started: here
    and not in `addopts`, which would make `-n` an unknown option wherever pytest-xdist is
    not installed (`uv run --with pytest …`).

    A run aimed at one test or a `-k` stays in this process: starting the workers costs more
    than such a run does, and `print` and `--pdb` work. `-n 0` asks for the same by hand;
    `PYTEST_XDIST_AUTO_NUM_WORKERS` caps the workers when the machine is busy."""
    # A worker runs this hook too, with `-n` unset: without this line each one starts its own
    # workers, and they theirs, until the user's process limit is gone (5 Nov 2026).
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if (config.pluginmanager.hasplugin("xdist")
            and config.getoption("numprocesses", None) is None
            and not config.getoption("keyword", "")
            and not config.getoption("usepdb", False)
            and not any("::" in str(a) for a in config.args)):
        config.option.numprocesses = "auto"


def page_source() -> str:
    """The orchestrator, every module of `hrbuild/`, and every asset it inlines.

    Concatenated rather than parsed: these are grep-shaped guardrails, and a guardrail
    that has to understand Python to fire is a guardrail that stops firing the first time
    somebody writes the thing it forbids in a way it did not anticipate."""
    pkg = HERE / "hrbuild"
    parts = [(HERE / "build-review-html.py").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted(pkg.rglob("*.py"))]
    # Text assets only: a binary one (the angry robot's PNG) has no source to grep.
    parts += [p.read_text(encoding="utf-8") for p in sorted((pkg / "assets").rglob("*"))
              if p.is_file() and p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2"}]
    return "\n".join(parts)
