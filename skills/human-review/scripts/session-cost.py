#!/usr/bin/env python3
"""What each phase of this change cost: writing it, reviewing it, taking the review's advice.

The `$` tab has always been able to say what the review *page* cost, and lately what
writing the code cost beside it. Neither is the question a reader arrives with. They want
to know where the money went in the work — was the review the expensive part, or was
acting on it; did the write-up of what was declined cost anything at all — and that is a
cut by **phase**, which nothing was in a position to make until the commits started
carrying trailers saying which commit was which.

This script is deliberately thin. It derives the five boundaries and prints the table;
every dollar in it is priced by `review-cost.py`, which owns the model prices, the
dedupe-by-`message.id` and the subagent discovery. A second implementation of any of that
would drift, and the two numbers on the page would then disagree with no way to tell which
was wrong.

Nothing here is typed by a human:

    t0  first edit to the change set        authoring-sessions.py's own evidence rules
    t1  commit #1, the implementation       %cI of the sha in the Implements: trailer
    t2  the review's first turn             earliest turn across the forked reviewers
    t3  the review's last turn              latest turn across the same files
    t4  commit #2, the review commit        %cI of the commit with Review-Points:

**A phase that cannot be dated says so.** It prints `unmeasurable: <why>` and not `$0.00`,
because those two render identically to a reader and mean opposite things: one is a phase
that cost nothing, the other is a phase whose cost is sitting in some other row. And every
window is printed, because a window is a bound and not a fence — work inside it that
belonged to something else is still counted, and the page must not imply a precision the
transcript cannot support.

It also writes `.human-review/phases.json`, which is what `review-cost.py --ledger` picks
up as its `phases` key and the page renders.

Exit codes:  0 at least one phase measured · 2 not a git repository · 3 nothing measurable.

Usage:
  session-cost.py                                  # against origin/main, in this repo
  session-cost.py --root ../petclinic --base main
  session-cost.py --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _module("review_cost", "review-cost.py")
rcommits = _module("review_commits", "review-commits.py")

AUTHORING = HERE / "authoring-sessions.py"
SESSION_FILE = ".human-review/.session"
DEFAULT_OUT = ".human-review/phases.json"


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def committed_at(root: Path, sha: str | None) -> "dt.datetime | None":
    if not sha:
        return None
    return rc._parse_iso(git(root, "log", "-1", "--format=%cI", sha))


def authoring(root: Path, base: str) -> dict:
    """`authoring-sessions.py --json`, which already knows who wrote these files and when.

    Called rather than reimplemented: its evidence rules — an edit tool naming a changed
    file, or a shell command that demonstrably writes one, and never a read — are the whole
    of what makes `t0` a measurement instead of "when the session started".
    """
    proc = subprocess.run([sys.executable, str(AUTHORING), "--base", base, "--json"],
                          cwd=root, capture_output=True, text=True)
    if not proc.stdout.strip():
        return {"sessions": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"sessions": []}


def resolve_session(root: Path, trailer: str | None, found: dict) -> tuple[str | None, str]:
    """Which conversation wrote the code, by the most durable answer available.

    The trailer first: it is committed, so it survives a rebase, a fresh clone and the
    deletion of `.human-review/`. Then the pinned `.session`, which is gitignored and dies
    with the directory but is right while it exists. Then the strongest author on disk,
    which is a scan and therefore a guess — a good one, and named as one.
    """
    if trailer:
        return trailer, "the Claude-Session: trailer"
    pinned = root / SESSION_FILE
    if pinned.is_file():
        value = pinned.read_text(encoding="utf-8").strip()
        if value:
            return value, f"{SESSION_FILE} (gitignored — it dies with the directory)"
    rows = found.get("sessions") or []
    strong = [r for r in rows if r.get("edits")]
    if strong:
        return strong[0]["session"], ("authoring-sessions.py's strongest author — a scan, "
                                      "not a record")
    env = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env:
        return env, "$CLAUDE_CODE_SESSION_ID (this conversation, which may not be the one)"
    return None, "nothing on disk or in the commits names the coding session"


def boundaries(root: Path, base: str, session: str | None, found: dict,
               commits: dict) -> tuple[list, list[Path], list[str]]:
    """`t0..t4`, the reviewer transcripts, and every reason one of them is missing."""
    notes: list[str] = []
    row = next((r for r in (found.get("sessions") or []) if r.get("session") == session),
               None)
    if row is None:
        strong = [r for r in (found.get("sessions") or []) if r.get("edits")]
        row = strong[0] if strong else None
        if row is not None and session:
            notes.append(f"the resolved session {session[:8]}… is not among the sessions "
                         f"that edited these files; t0 comes from {row['session'][:8]}…")
    t0 = rc._parse_iso(row.get("first")) if row else None
    if t0 is None:
        notes.append("t0: no edit to the change set found in any transcript on disk")

    t1 = committed_at(root, commits.get("implementation"))
    if t1 is None:
        notes.append("t1: no Implements: trailer, so commit #1 is not identified")
    t4 = committed_at(root, commits.get("review"))
    if t4 is None:
        notes.append("t4: no Review-Points: commit, so the review commit is not identified")

    reviewers: list[Path] = []
    path = rc.transcript(session) if session else None
    if path is not None:
        reviewers = rc.review_agent_files(path)
    t2, t3 = rc.agent_span(reviewers)
    if not reviewers:
        notes.append(f"t2/t3: no subagent of this session is named "
                     f"{rc.REVIEW_AGENT_NAME!r} — the review ran inline, or in another "
                     f"conversation")
    return [t0, t1, t2, t3, t4], reviewers, notes


def report(doc: dict, notes: list[str], how: str, out: Path | None) -> None:
    b = doc["boundaries"]
    print(f"session   {doc['session'] or '— unknown'}   ({how})")
    print("windows   " + " · ".join(
        f"{k}={(v[:16] if v else '—')}" for k, v in b.items()))
    width = max(len(r["label"]) for r in doc["rows"])
    for r in doc["rows"]:
        if r["measured"]:
            line = (f"  {r['label']:<{width}}  {rc.money(r['cost']):>8}  "
                    f"{rc.human(r['tokens']):>7} tok  {r['messages']:>4} turns")
            if r["detail"]:
                line += f"   {r['detail']}"
            print(line)
        else:
            # Never a zero. "$0.00" and "we could not date this" look the same on a page
            # and mean opposite things.
            print(f"  {r['label']:<{width}}  {'unmeasurable':>8}: {r['reason']}")
    print(f"  {'total':<{width}}  {rc.money(doc['cost']):>8}  "
          f"{rc.human(doc['tokens']):>7} tok  {doc['messages']:>4} turns"
          + ("   (the measured rows only)" if doc["unmeasured"] else ""))
    for n in notes:
        print(f"  note: {n}", file=sys.stderr)
    if out:
        print(f"  wrote  {out}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="the repository (default: cwd)")
    ap.add_argument("--base", default="origin/main",
                    help="what the change set is measured against")
    ap.add_argument("--session", help="the coding session, when the trailers do not say")
    ap.add_argument("--run-session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="the session that BUILT the page (default: this one), for the "
                         "video, images and page-build rows")
    ap.add_argument("--steps-file", default=".human-review/.steps.json")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"where the build reads the phases from (default: {DEFAULT_OUT}); "
                         f"'-' writes nothing")
    ap.add_argument("--json", action="store_true", help="emit the breakdown as JSON")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    top = git(root, "rev-parse", "--show-toplevel")
    if not top:
        print(f"[session-cost] {root} is not a git repository", file=sys.stderr)
        return 2
    root = Path(top)

    commits = rcommits.detect(root, args.base)
    for w in commits["warnings"]:
        print(f"[session-cost] {w}", file=sys.stderr)
    found = authoring(root, args.base)
    session, how = resolve_session(root, args.session or commits.get("session"), found)
    stamps, reviewers, notes = boundaries(root, args.base, session, found, commits)

    doc = rc.phase_costs(session, *stamps, reviewers,
                         run_session=args.run_session,
                         steps_path=root / args.steps_file,
                         points_file=commits.get("points_file") or rc.DEFAULT_POINTS)
    doc["session_from"] = how
    doc["notes"] = notes
    doc["commits"] = {k: commits.get(k) for k in
                      ("implementation", "review", "fallback", "after")}
    doc["reviewers"] = [str(r) for r in reviewers]

    out = None
    if args.out != "-":
        out = root / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(doc, indent=1))
    else:
        report(doc, notes, how, out)
    return 0 if doc["measured"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
