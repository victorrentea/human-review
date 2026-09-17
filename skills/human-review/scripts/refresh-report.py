#!/usr/bin/env python3
"""Rebuild and re-serve an existing review page, without asking a model anything.

`/human-review` has two halves that have never been separable by hand, and were therefore
run together every time somebody wanted a page refreshed:

  * **the model's half** — the findings, the prose, the requirements↔tests matrix and the
    per-test catalogue behind it. Slow, paid for, and *not reproducible*: a second pass over
    the same diff words and ranks its findings differently, so re-running it does not
    confirm the first one, it replaces it. It runs when the human asks for it, never as a
    side effect of anything else;
  * **the programs** — the diagram deltas, the Code City shot, the complexity increment, the
    contract diff, the logging scan, the test manifest, the recordings, and the build of the
    HTML itself. Fixed inputs, fixed outputs, safe to run again at any time.

This is the second half, as one command. Run it after *any* change that the page should
show: an edit to `build-review-html.py`, a fix in the branch under review, a new commit, a
tweak to `content.json`. It never writes the model's half and never calls a model — a
logging statement whose privacy verdict is not already in the cache renders as *not
evaluated* rather than quietly buying an answer.

    refresh-report.py                     # rebuild the page and serve it
    refresh-report.py --steps cheap       # …after re-running the fast producers
    refresh-report.py --steps all         # …after re-running every producer, heavy included
    refresh-report.py --steps diagrams,tests
    refresh-report.py --no-serve          # write the file, print nothing to open

The one thing it refuses to do is build a page whose model-written parts are missing. That
is not a step this program can re-run, and a page built without them is not a smaller page —
it is the same page with its argument deleted, which is worse than no page at all.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_STEPS = HERE / "run-steps.py"
BUILD = HERE / "build-review-html.py"
SERVE = HERE / "serve-review.py"

#: What only the model writes, and what it is — named so the refusal can say which part of
#: `/human-review` produces it rather than "a file is missing". Deliberately short: these
#: are the artifacts that carry a judgement. Everything else under `.human-review/` is the
#: output of a program, and the build's own validation already names those, correctly, as
#: "did the step that produces it run?".
MODEL_OWNED = {
    "content.json": "the judgement — findings, prose, the tab layout (Step 4)",
    "assets/requirements-map.html": "the requirements↔tests matrix",
    "test-index": "the per-test catalogue the matrix reads",
}


def missing_model_work(review: Path) -> list[tuple[str, str]]:
    """Which model-written artifacts are not on disk, with what each one is.

    A directory counts as present when it exists and holds something: `test-index/` left
    behind empty by a wipe is the same absence as no directory at all, and the honest
    answer to both is that nobody has written the catalogue yet."""
    gone = []
    for rel, what in MODEL_OWNED.items():
        p = review / rel
        if p.is_dir():
            if not any(p.iterdir()):
                gone.append((rel, what))
        elif not p.is_file():
            gone.append((rel, what))
    return gone


#: The producers that drive a browser, record a film or run a whole test suite. Minutes
#: each, and each needs something up — a served app, a Chrome, a tracing backend — so they
#: are not what "refresh the page" should mean. `--steps all` is how you ask for them.
HEAVY_STEPS = ("sequence", "video", "city", "dsaudit")


def steps_argv(steps: str) -> list[str] | None:
    """`--steps` as arguments for `run-steps.py`, or None for "run no producers at all".

    The default is None and that is the common case: most refreshes are a change to the
    *page*, not to the evidence, and re-deriving evidence nothing touched is a minute of
    waiting for a byte-identical result."""
    steps = (steps or "").strip()
    if not steps or steps == "none":
        return None
    if steps == "all":
        return []
    if steps == "cheap":
        return ["--skip", ",".join(HEAVY_STEPS)]
    return ["--only", steps]


def plan(review: Path, steps: str, base: str | None, serve: bool,
         allow_model: bool, session: str | None) -> list[list[str]]:
    """Every command this run will make, in order, as argv lists.

    Built as data so the decisions above are testable without running a browser, a build or
    a server — the same reason `run-steps.py` keeps its own step table as data."""
    out = []
    produce = steps_argv(steps)
    if produce is not None:
        out.append([sys.executable, str(RUN_STEPS), *produce]
                   + (["--base", base] if base else []))
    build = [sys.executable, str(BUILD), str(review / "content.json"),
             "--out", str(review / "review.html")]
    if not allow_model:
        build.append("--no-model")
    out.append(build)
    if serve:
        out.append([sys.executable, str(SERVE), str(review)])
    return out


def session_id(review: Path) -> str | None:
    """The session that did the work, so a rebuilt page keeps reporting its real cost.

    A build run in a later session cannot recompute what the first one spent, and a page
    that silently drops the number is a page that looks free. The run wrote the id down
    for exactly this; passing it back is what makes a refresh a refresh and not a new,
    cheaper-looking review."""
    p = review / ".session"
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--steps", default="none",
                    help="none (default), cheap, all, or a comma-separated list")
    ap.add_argument("--base", help="base ref for the producers (default: the config's)")
    ap.add_argument("--no-serve", dest="serve", action="store_false",
                    help="write the page and do not start the server")
    ap.add_argument("--allow-model", action="store_true",
                    help="let the build make the Logging tab's uncached privacy calls")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    args = ap.parse_args(argv)

    review = Path(args.dir)
    if not review.is_dir():
        print(f"[refresh] no {review}/ here — there is no report to refresh. "
              "Run /human-review from the repository root first.", file=sys.stderr)
        return 2

    gone = missing_model_work(review)
    if gone:
        print(f"[refresh] {review}/ is missing the parts only a model writes:",
              file=sys.stderr)
        for rel, what in gone:
            print(f"  - {rel} — {what}", file=sys.stderr)
        print("[refresh] this program does not write them, on purpose: they are a "
              "judgement, and a second pass over the same diff produces a different one "
              "at full price. Restore them, or ask for /human-review to run its own half "
              "again.", file=sys.stderr)
        return 3

    commands = plan(review, args.steps, args.base, args.serve,
                    args.allow_model, session_id(review))
    env = dict(os.environ)
    sid = session_id(review)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid

    url = ""
    for cmd in commands:
        printable = " ".join(Path(c).name if c.startswith("/") and Path(c).exists() else c
                             for c in cmd)
        print(f"[refresh] $ {printable}")
        if args.dry_run:
            continue
        is_serve = cmd[1] == str(SERVE)
        proc = subprocess.run(cmd, env=env, text=True,
                              capture_output=is_serve)
        if is_serve:
            url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        # A producer that fails is a fact about the run and the page still gets built —
        # the build names the evidence it could not find. A failing *build* is different:
        # there is no page, and serving the last one under a fresh URL would be the one
        # outcome nobody can tell from success.
        if proc.returncode != 0 and cmd[1] == str(BUILD):
            print("[refresh] the build failed — the page on disk is the previous one.",
                  file=sys.stderr)
            return 1
    if url:
        print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
