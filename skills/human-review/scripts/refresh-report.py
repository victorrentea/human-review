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
    refresh-report.py --steps static      # …after re-running the ones that need nothing up
    refresh-report.py --steps all         # …after re-running every producer, heavy included
    refresh-report.py --steps diagrams,tests
    refresh-report.py --no-serve          # write the file, print nothing to open

It refuses to build in two cases, and both are the same principle: a page must not assert
something the repository contradicts. The first is a **missing model-written part** — the
layout, the requirements↔tests matrix, the per-test catalogue. Those are not steps this
program can re-run, and a page without them is not a smaller page, it is the same page
with its argument deleted. The second is a **commit claiming a `review-points.md` that is
not on disk**: the branch says it recorded its own review and the file is gone, so the
band reading *nothing records what was reviewed* would be true of the disk and false about
the run.
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
REVIEW_COMMITS = HERE / "review-commits.py"
CONFIG = "human-review.json"

#: What only the model writes, and what it is — named so the refusal can say which part of
#: `/human-review` produces it rather than "a file is missing". Deliberately short: these
#: are the artifacts nothing else can reconstruct. Everything else under `.human-review/`
#: is the output of a program, and the build's own validation already names those,
#: correctly, as "did the step that produces it run?".
#:
#: `content.json` is on the list and no longer for the reason it used to be. It was *the
#: judgement* — the findings, which ones were fixed, which were left, what the coder
#: assumed — written by a model at the end of a review and corroborated by nothing outside
#: itself. That record now belongs to the branch: `/implement-ticket` writes
#: `review-points.md`, commits it with the fixes, and the content file asks for the three
#: piles with `{"auto": "review-points"}`. What is left in it is the *layout* and the
#: *ledes* — which tabs the page has, in what order, and the sentences over them. Still a
#: model's work, still not reproducible, still not something this program will invent; a
#: much smaller claim.
MODEL_OWNED = {
    "content.json": "the layout and the ledes — which tabs, in what order, and the "
                    "sentences over them (the three Review piles come from the branch's "
                    'own review-points.md, via {"auto": "review-points"})',
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


def config_base(explicit: str | None) -> str:
    """What the branch is measured against: the flag, the repo's config, else origin/main.

    Read here as well as in `run-steps.py` because the check below asks git a question
    about a *range*, and a range this program guessed differently from the producers would
    refuse a build over a commit the page never claimed to cover."""
    if explicit:
        return explicit
    try:
        cfg = json.loads(Path(CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "origin/main"
    base = cfg.get("base") if isinstance(cfg, dict) else None
    return base if isinstance(base, str) and base.strip() else "origin/main"


def broken_points_promise(base: str) -> tuple[str, str] | None:
    """`(sha, path)` when a commit says it carries a points file that is not on disk.

    A `Review-Points:` trailer is a commit asserting that this branch records what was
    reviewed, fixed and declined. When the named file is not there, the page would render
    the absence honestly — a grey band — and be wrong about *why*: the record was not
    skipped, it was lost. That happens in exactly the ways nobody notices: a rebase that
    dropped the file while keeping the commit message, a cherry-pick of the fixes without
    it, a `git checkout --` over the working tree.

    So it is a refusal rather than a warning. Every other missing input on this page
    degrades to a named absence, because "this project has no such thing" is a real state;
    this one cannot be, because the commit already said otherwise. The two claims cannot
    both stand, and the build is not the place to choose between them.
    """
    proc = subprocess.run([sys.executable, str(REVIEW_COMMITS), "--base", base, "--json"],
                          capture_output=True, text=True)
    if not (proc.stdout or "").strip():
        return None
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return None
    rel = doc.get("points_file") or "review-points.md"
    # Only the trailer counts, never the fallback: the fallback *is* "the one commit that
    # touches the points file", so a fallback plus a missing file is a contradiction in
    # terms and would refuse every build on a branch that has neither.
    if doc.get("review") and not doc.get("fallback") and not Path(rel).is_file():
        return doc["review"], rel
    return None


#: The producers that drive a browser, record a film or run a whole test suite. Minutes
#: each, and each needs something up — a served app, a Chrome, a tracing backend — so they
#: are not what "refresh the page" should mean. `--steps all` is how you ask for them.
HEAVY_STEPS = ("sequence", "video", "city", "dsaudit")

#: The producers that need nothing but the repository: no served app, no browser, no film,
#: no test suite. This is the set a *button* may re-run — the Rerun in the page's header
#: (`serve-review.py`) asks for exactly this — and it is named here rather than there
#: because which producers are safe to fire off a click is a fact about the producers.
#:
#: It is not `cheap` minus the video, and the difference is the whole reason it exists.
#: `cheap` skips the four in HEAVY_STEPS and keeps `traces`, whose configured `commands`
#: are a project's own e2e suite — in petclinic, a cucumber run that needs the stack up on
#: :4200. Minutes, and a failure when nothing is listening. A reader who presses Rerun
#: after editing a test body is asking for the page to catch up with the repository, not
#: for a browser suite to be run at them.
STATIC_STEPS = ("reviewpoints", "aftermath", "diagrams", "c2", "complexity", "api",
                "specchanges", "logging", "owners", "tests")


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
    if steps == "static":
        return ["--only", ",".join(STATIC_STEPS)]
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
                    help="none (default), static, cheap, all, or a comma-separated list")
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

    base = config_base(args.base)
    promise = broken_points_promise(base)
    if promise:
        sha, rel = promise
        print(f"[refresh] {sha[:8]} carries a `Review-Points: {rel}` trailer and {rel} is "
              f"not on disk.", file=sys.stderr)
        print("[refresh] that commit says this branch records what was reviewed, fixed "
              "and declined. The record is gone — a rebase or a cherry-pick dropped the "
              "file and kept the message. A page built now would say nothing was ever "
              "recorded, which is not what happened. Restore the file (`git checkout "
              f"{sha} -- {rel}`), or drop the trailer if the claim was never true.",
              file=sys.stderr)
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
