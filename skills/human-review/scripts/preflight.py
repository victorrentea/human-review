#!/usr/bin/env python3
"""The gate, then the wipe, then the run's start markers — in that order, or not at all.

A review of a branch that does not build is worse than no review: it is a confident-looking
guide about code nobody has proven compiles, with every number on it measured from a tree of
unknown status. So the branch is pushed and the build for the *exact commit that was pushed*
must be green before anything else happens.

The order is the substance of this script, and it is why it is a script:

  1. **gate** — push, then wait for green **for `$SHA`**, never for "the latest run on the
     branch". A branch almost always has some green run on it, which is what makes
     `gh run list --branch` so tempting and so wrong: it is the confidently-wrong signal
     this whole page exists to avoid. An empty run list is not a pass either — **absence is
     not success** — and a repository with genuinely no CI is let through only if the guide
     then says *"no build proved this"*.
  2. **wipe** — and only now. Every fragment producer writes to a fixed path and the
     renderer inlines whatever it finds with no freshness check, so a step that fails
     silently leaves the previous run's artifact in place: a green `compatible` seal for a
     diff it never saw. Because the wipe is destructive, it comes *after* the gate — a run
     that stops at the gate leaves the previous guide intact instead of destroying it on the
     way out.
  3. **reset the ledger**, for the same reason and worse. A stale `.steps.json` parses
     perfectly: every tab it names is reported as measured, none of this run's turns fall
     inside last run's windows, and the page prints a confident **`$0.00`** against every
     tab. That reads as "this run was free", not as "we did not measure".
  4. **start markers** — `.started` is what lets the page report what it cost; `.session` is
     what lets a later rebuild read the right transcript instead of publishing the wrong one.

The gate is about the branch **as pushed now, not the tree at the end of the run**. This
skill deliberately leaves the review's own fixes uncommitted for a human to inspect, so this
never means "push the review's fixes too".

Exit codes:  0 ready · 1 the gate refused · 2 misuse.

Usage:
  preflight.py --base origin/main
  preflight.py --no-gate          # local experiment; the guide must say the gate was skipped
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HR = Path(".human-review")


def run(cmd: str, capture=True, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def gate(wait_minutes: float) -> tuple[bool, str]:
    """True when the pushed commit is proven green (or the repo has no CI at all)."""
    dirty = run("git status --porcelain").stdout.strip()
    if dirty:
        print("[preflight] working tree is not clean. Decide, deliberately, what belongs "
              "in the branch before the gate runs:\n" + dirty, file=sys.stderr)

    print("[preflight] git push")
    push = run("git push", capture=True)
    if push.returncode != 0 and "up-to-date" not in (push.stderr or "").lower():
        # A push that fails is not a gate failure yet — it may need -u on a new branch.
        up = run("git push -u origin HEAD")
        if up.returncode != 0:
            return False, f"cannot push: {(up.stderr or push.stderr or '').strip()[:300]}"

    sha = run("git rev-parse HEAD").stdout.strip()
    if not sha:
        return False, "cannot resolve HEAD"
    print(f"[preflight] waiting on CI for {sha[:12]} (not for the branch)")

    if not shutil.which("gh"):
        return True, f"no `gh` — no build proved this commit ({sha[:12]}); say so in the guide"

    deadline = time.time() + wait_minutes * 60
    while True:
        # Bound to the commit, never to the branch: a green run for a different commit is
        # exactly the signal this refuses to accept.
        r = run(f'gh run list --commit "{sha}" --limit 1 '
                f'--json databaseId,status,conclusion,workflowName')
        try:
            runs = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            runs = []
        if not runs:
            # Absence is not success. Either nothing has registered yet, or nothing builds
            # this branch — and only `gh workflow list` can tell those apart.
            wf = run("gh workflow list").stdout.strip()
            if not wf:
                return True, ("no build proved this — the repository has no CI configured. "
                              "Put that in the guide; it must not read as a pass")
            if time.time() > deadline:
                return False, (f"no CI run for {sha[:12]} after {wait_minutes:g} min, and "
                               "this repository does have workflows. Absence is not success")
            print("[preflight]   no run registered yet; waiting")
            time.sleep(15)
            continue
        one = runs[0]
        if one.get("status") != "completed":
            if time.time() > deadline:
                return False, (f"{one.get('workflowName')} still running after "
                               f"{wait_minutes:g} min — treat it as stuck")
            print(f"[preflight]   {one.get('workflowName')}: {one.get('status')}")
            time.sleep(20)
            continue
        if one.get("conclusion") == "success":
            return True, f"green: {one.get('workflowName')} for {sha[:12]}"
        return False, (f"{one.get('workflowName')} concluded {one.get('conclusion')} for "
                       f"{sha[:12]} — name it and the failing job in the report, and fix "
                       "that first")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the CI gate; the guide must then say no build proved this")
    ap.add_argument("--wait-minutes", type=float, default=20.0)
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    args = ap.parse_args(argv)

    caveat = ""
    if args.no_gate:
        caveat = "the CI gate was skipped — no build proved this"
        print(f"[preflight] {caveat}", file=sys.stderr)
    else:
        ok, caveat = gate(args.wait_minutes)
        print(f"[preflight] {caveat}")
        if not ok:
            print("[preflight] refusing to review a branch CI has not proven. Nothing was "
                  "wiped; the previous guide is intact.", file=sys.stderr)
            return 1

    # Only now, because everything below this line destroys the previous run.
    assets = HR / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    assets.mkdir(parents=True, exist_ok=True)

    ledger = Path(__file__).resolve().parent / "steps-ledger.py"
    subprocess.run([sys.executable, str(ledger), "reset"], check=False)

    (HR / ".started").write_text(
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00") + "\n",
        encoding="utf-8")
    (HR / ".session").write_text(args.session + "\n", encoding="utf-8")
    if not args.session:
        print("[preflight] no CLAUDE_CODE_SESSION_ID — the cost chip will drop itself "
              "rather than publish a wrong number", file=sys.stderr)

    (HR / ".gate").write_text(caveat + "\n", encoding="utf-8")
    print(f"[preflight] ready. base={args.base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
