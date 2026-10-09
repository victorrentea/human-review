#!/usr/bin/env python3
"""Run every deterministic producer of the review page, in order, ledger-wrapped.

This is the part of `/human-review` that was a runbook and should never have been one.
Steps 2-8 are programs with fixed arguments, a fixed order, a prerequisite each, and a
ledger record each; a model reading three hundred lines of prose and retyping those calls
adds nothing but the chance of getting one wrong. What is left for a model is the part a
program cannot do: deciding what the findings mean and writing them down.

The ledger invariants live here now, as code rather than as rules somebody has to follow:

  * **the prerequisite is checked before the stamp.** A gate that fails after the stamp
    leaves a record naming a tab the page will not contain, which the build then reports as
    drift. `_prereq` runs first, and a step that fails it is never stamped at all;
  * **a step that is stamped is always closed**, including one that crashes — the `end` is
    in a `finally`. An open record makes the page say a step died when the runbook simply
    never said to close it;
  * **one handle file per step**, named after the step, so two steps in flight cannot
    overwrite each other's index.

Project-specific commands come from `human-review.json` at the repo root — see
`human-review.example.json`. A step the config does not describe is skipped and named,
which is the honest rendering of "this project has no such thing"; nothing here is
petclinic-specific.

Usage:
  run-steps.py --list                     # the steps, their tabs and their prerequisites
  run-steps.py                            # run all of them
  run-steps.py --only api,logging         # run some
  run-steps.py --skip video               # run the rest
  run-steps.py --base origin/main --json  # machine-readable status
"""
from __future__ import annotations

import argparse
import copy
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
import re
import shlex
import socket
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ART = Path(".human-review/assets")
LEDGER = HERE / "steps-ledger.py"

RAN, SKIPPED, FAILED = "ran", "skipped", "failed"


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Ctx:
    """What every step needs to know about the run it is part of.

    `no_ledger` is the one field that is not about the repository, and it is here rather
    than as an argument because every step has to agree on it. A *review* stamps the
    ledger: that is how `review-cost.py` later attributes the conversation's turns to the
    tabs they paid for, by which step's window each turn falls inside. A *refresh* must
    not, for two reasons that point the same way:

      * it re-derives evidence in a different session, days later. Its windows are windows
        in which no turn of the reviewed conversation happened, so they attribute nothing
        and can only dilute what the real ones say;
      * `.steps.json` is one of the inputs the build's own cost cache is keyed on
        (`hrbuild/tabs/cost.py`). A refresh that stamps it moves it, so the cache misses,
        so the build spends forty-five seconds re-reading a conversation that has not
        gained a turn. Stamping a window that means nothing is not free — it is the single
        most expensive thing a refresh used to do.
    """

    def __init__(self, base: str, cfg: dict, dry: bool, no_ledger: bool = False):
        self.base, self.cfg, self.dry = base, cfg, dry
        self.no_ledger = no_ledger
        self.notes: list[str] = []

    def step_cfg(self, name: str) -> dict:
        return (self.cfg.get("steps") or {}).get(name) or {}


#: Where the step running on this thread writes. Steps run in parallel (see `schedule`),
#: and four steps printing into one terminal is a log nobody can read; so each one writes
#: to its own `.human-review/logs/<step>.log`, and the terminal gets one line when a step
#: starts and one when it ends. Serial runs (`--jobs 1`) keep writing to the terminal.
_LOCAL = threading.local()
_NOTES_LOCK = threading.Lock()


class _StepStdout:
    """`sys.stdout` for the whole process: the step's log on a step's thread, the real
    terminal everywhere else. The steps print a lot and were written for one stream."""

    def __init__(self, real):
        self.real = real

    def _target(self):
        return getattr(_LOCAL, "log", None) or self.real

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        self._target().flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


def sh(cmd, ctx: Ctx, check=True, capture=False) -> subprocess.CompletedProcess:
    """One command, echoed. A step's commands are shell strings because half of them are
    the project's own (`cd x && mvn …`), and quoting those into a list buys nothing."""
    print(f"    $ {cmd}", flush=True)
    if ctx.dry:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    log = getattr(_LOCAL, "log", None)
    r = subprocess.run(cmd, shell=True, text=True,
                       stdout=subprocess.PIPE if capture else log,
                       stderr=subprocess.PIPE if capture else log)
    if check and r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}: {cmd}"
                           + (f"\n{(r.stderr or '').strip()[:400]}" if capture else ""))
    return r


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def has_java() -> bool:
    """Whether this project has Java main sources — asked of the index, not of the disk,
    so `node_modules` and a build's output cost nothing to walk past."""
    listed = subprocess.run(["git", "ls-files", "*/src/main/java/*.java", "src/main/java/*.java"],
                            capture_output=True, text=True)
    return bool(listed.stdout.strip())


def has_genseq() -> bool:
    """Whether any traced sequence diagram exists, asked of the index and of the untracked
    files alike — the `sequence` step usually writes them minutes before this is asked, and
    a brand-new one is not committed yet."""
    for args in (["git", "ls-files", "*.genseq.puml"],
                 ["git", "ls-files", "--others", "--exclude-standard", "*.genseq.puml"]):
        if subprocess.run(args, capture_output=True, text=True).stdout.strip():
            return True
    return False


def answers(url: str, timeout: float = 3.0) -> bool:
    """Whether *anything* is serving at `url` — the running-app twin of `have()`.

    Any HTTP status counts, 404 included: a dev server answers every path, and the audit's
    screens are deep links the front end routes client-side anyway. What this tells apart
    is "a build is up" from "nobody is listening", which is the only distinction a
    prerequisite needs — and the one Playwright reports as a 40-line traceback ending in
    ERR_CONNECTION_REFUSED, three steps too late to say so plainly.
    """
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run_base(named: str) -> str:
    """The base every producer measures from: the one `build-review-html.py` measures from.

    `named` is the ref the branch merges into. When `review-points.md` records the base its
    reviewers audited, and that commit is on this branch past the fork point, the producers
    measure from it instead — the same choice `hrbuild/shared/chips.py:page_base` makes for
    the header, CODEOWNERS and the snippets, so no tab counts a range the others do not. A
    branch carrying commits from before the review (a plan, an AGENTS.md) otherwise had its
    API tab measured from one commit and its header from another."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from hrbuild.shared.chips import page_base
        st = page_base(Path.cwd(), None, named)
    except Exception:  # noqa: BLE001 - no answer is the named base, as before
        return named
    if st and st.get("diffBaseSource") == "audited":
        print(f"[run-steps] base {st['diffBase'][:12]}: the base the review audited "
              f"(review-points.md), inside {named}", file=sys.stderr)
        return st["diffBase"]
    return named


def merge_base(ctx: Ctx) -> str:
    """Where this branch forked, not where the base ref points now.

    Every producer here reads the "before" side out of a commit object, so a base that has
    moved on since the branch started would report commits that landed on the base as this
    branch's work.
    """
    got = sh(f"git merge-base {ctx.base} HEAD", ctx, capture=True, check=False).stdout.strip()
    return got or ctx.base


# --------------------------------------------------------------------------- steps

def _reviewpoints(ctx: Ctx):
    """Read `review-points.md` off the branch, and work out which commit is which.

    This is the step that turns the Review tab from something a model wrote at the end of
    a review into something the branch carries. `review-points.md` is committed with the
    fixes, so what the coding agent accepted, declined and assumed arrives in the pull
    request's own file list; this step parses it into `.human-review/review-points.json`
    and resolves the two commits into `.human-review/review-commits.json`.

    Both files are written, and the two halves fail differently on purpose:

    * **no points file (parser exit 3)** is a `skipped`, not a failure. A branch nobody
      ran `/record-review` on is a normal branch, and the build renders the absence as
      an absence — a band saying so — rather than as a clean review;
    * **a points file that will not parse (4) or that anchors nothing (5)** is loud. Both
      are a record that exists and says nothing checkable, and a silently skipped pile
      reads on the page exactly like a pile nobody wrote, which is the one confusion this
      whole flow exists to prevent.

    A stale `review-points.json` is deleted whenever the parse does not produce a new one.
    Leaving the previous run's copy in place would put items on the page that the branch
    no longer records — the worst of the three states, because it looks like the good one.

    `review-commits.py` runs whatever the parse did: `aftermath` needs the review commit
    to date from, and a branch can carry the trailers without the file still being there
    (a rebase that dropped it), which is itself worth reporting.
    """
    out = Path(".human-review/review-points.json")
    rp = sh(f"{HERE}/review-points.py --root . --out {out}", ctx, check=False, capture=True)
    print((rp.stdout or "") + (rp.stderr or ""), end="", flush=True)

    base = merge_base(ctx)
    rc = sh(f"{HERE}/review-commits.py --base {base} --json", ctx, check=False, capture=True)
    print(rc.stderr or "", end="", file=sys.stderr, flush=True)
    commits = Path(".human-review/review-commits.json")
    if (rc.stdout or "").strip():
        commits.parent.mkdir(parents=True, exist_ok=True)
        commits.write_text(rc.stdout, encoding="utf-8")
    elif not ctx.dry:
        # No answer at all (not a repository, an empty range): the previous run's answer
        # would date the aftermath band from a commit this range does not contain.
        commits.unlink(missing_ok=True)
    if rc.returncode == 3:
        ctx.notes.append("no commit on this branch carries a Review-Points: trailer, so "
                         "which commit the agent finished on is not recorded — the "
                         "aftermath band cannot be drawn and nothing dates the phases")
    elif rc.returncode != 0 and not ctx.dry:
        ctx.notes.append(f"review-commits.py exit {rc.returncode} — the two commits could "
                         "not be resolved; say so rather than reading the page's phases")

    if rp.returncode != 0 and not ctx.dry:
        out.unlink(missing_ok=True)
    if rp.returncode == 3:
        raise LookupError("no review-points.md on this branch — nothing records what the "
                          "agent fixed, declined or assumed. The Review tab says so; do "
                          "not write the piles by hand to fill the gap")
    if rp.returncode == 4:
        raise RuntimeError("review-points.md is on the branch and cannot be parsed — see "
                           "the problems above. A pile the parser skipped reads exactly "
                           "like a pile nobody wrote")
    if rp.returncode == 5:
        raise RuntimeError("every item in review-points.md is unanchored — the file is "
                           "there and says nothing a reader can go and look at")
    if rp.returncode != 0:
        raise RuntimeError(f"review-points.py exit {rp.returncode}")


#: What counts as a file no human wrote. A commit landing on the branch after the agent
#: finished is a fact a reviewer has to be told about — every other number on the page was
#: measured before it — but a regenerated diagram, an OpenAPI spec rewritten by a
#: pre-commit hook and a redrawn `.drawio` are not somebody editing the change under
#: review. Without this split the band would be red on every branch where the guardrails
#: did their job, which is the fastest way to teach a reader to ignore a red band.
#:
#: Overridable per project with `"generated": [...]` in `human-review.json`, because which
#: paths a repository generates is a fact about that repository and nothing here can guess
#: it. Replaces the list rather than adding to it: a project that says what it generates
#: has said it.
from hrbuild.shared.chips import GENERATED_GLOBS as GENERATED_DEFAULT  # noqa: E402 - one list, the header's too


def glob_rx(pattern: str) -> "re.Pattern[str]":
    """One `**`-aware glob as a regex over a repo-relative path.

    `fnmatch` is not enough and is wrong in the direction that hurts: its `*` crosses `/`,
    so `openapi.yaml` would match `docs/openapi.yaml` and `**/*.genseq.*` would match
    nothing it was not already matching by accident. Here `*` stops at a slash, `**`
    crosses them, and a leading `**/` is *optional* — `**/generated/**` has to cover a
    top-level `generated/` directory too, which is the spelling every project writes and
    the one a literal reading would miss.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def generated_globs(cfg: dict) -> list[str]:
    got = cfg.get("generated") if isinstance(cfg, dict) else None
    if isinstance(got, list):
        named = [g for g in got if isinstance(g, str) and g.strip()]
        if named:
            return named
    return list(GENERATED_DEFAULT)


def numstat(text: str, rxs) -> tuple[list[dict], int, int]:
    """`git --numstat` output as rows, plus the added/deleted totals.

    A binary file is `-\t-\tpath`: it counts as a file that moved and as no lines, which
    is what it is. A rename arrives as `old => new` (or `a/{b => c}/d`) and is kept
    verbatim — the path is what the reader has to recognise, and rewriting it here would
    make the row disagree with the `git show` they run next.
    """
    rows, adds, dels = [], 0, 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, path = parts[0], parts[1], "\t".join(parts[2:]).strip()
        if not path:
            continue
        added = int(a) if a.isdigit() else 0
        deleted = int(d) if d.isdigit() else 0
        # A rename's *new* side is what the classifier has to judge: a diagram moved into
        # `generated/` is generated from here on, whatever it used to be.
        judged = path.split(" => ")[-1].strip("}").strip()
        rows.append({"path": path, "added": added, "deleted": deleted,
                     "binary": not a.isdigit(),
                     "generated": any(rx.match(judged) for rx in rxs)})
        adds += added
        dels += deleted
    return rows, adds, dels


def _aftermath(ctx: Ctx):
    """What landed on the branch after the agent stopped — the page's one honesty gate.

    Every other number here is measured from the diff, and the diff cannot tell when it
    was written. So a page can be entirely accurate about a change set and entirely
    misleading about *this* change: the film, the findings, the assumptions and the costs
    all describe the branch as the agent left it, and three commits later they describe
    something nobody reviewed. The reader has no way to see that from the page, because
    the page is the thing that would have to say it.

    `review-commits.py` already knows where the agent stopped (the `Review-Points:`
    trailer) and what came after it. This measures those commits and splits them by
    whether a human wrote them, so the band the build draws is red for a hand edit and
    grey for a regenerated diagram. Without the split it would be red on every branch
    whose guardrails ran, and a red band that is always on is a red band nobody reads.

    With no review commit there is nothing to date from, and the step is skipped: a band
    reading "nothing has changed since the agent finished" would be a claim resting
    entirely on not knowing when that was.
    """
    doc = None
    try:
        doc = json.loads(Path(".human-review/review-commits.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not doc.get("review"):
        raise LookupError("no commit on this branch carries a Review-Points: trailer, so "
                          "there is no point in its history to date 'after the agent "
                          "finished' from — run the reviewpoints step first, and if it "
                          "found nothing, the branch genuinely has no such point")
    review = doc["review"]
    globs = generated_globs(ctx.cfg)
    rxs = [glob_rx(g) for g in globs]

    head = sh("git rev-parse HEAD", ctx, capture=True, check=False).stdout.strip()
    total = sh(f"git diff --numstat {review}..HEAD", ctx, capture=True, check=False)
    rows, adds, dels = numstat(total.stdout or "", rxs)

    commits = []
    for c in doc.get("after_detail") or []:
        sha = c.get("sha") or ""
        got = sh(f"git show --numstat --format= {sha}", ctx, capture=True, check=False)
        files, cadds, cdels = numstat(got.stdout or "", rxs)
        code = [f for f in files if not f["generated"]]
        commits.append({
            "sha": sha, "short": sha[:8], "when": c.get("when", ""),
            "subject": c.get("subject", ""),
            "files": files, "added": cadds, "deleted": cdels,
            # A merge commit shows no numstat at all, so "nothing but generated files"
            # would be the wrong reading of it. No files means unmeasured, not harmless.
            "generated_only": bool(files) and not code,
            "measured": bool(files),
            # Accepted by a takeover without a new pass, and the takeover's own
            # bookkeeping commit (see review-commits.py's `takeover_heading`).
            "taken_over": bool(c.get("taken_over")),
            "takeover": bool(c.get("takeover")),
        })

    def tally(picked):
        return {"files": len(picked),
                "added": sum(f["added"] for f in picked),
                "deleted": sum(f["deleted"] for f in picked)}

    out = {
        "review": review, "review_short": review[:8], "head": head,
        "takeover": doc.get("takeover"),
        "generated_globs": globs,
        "commits": commits,
        "totals": {"commits": len(commits), "files": len(rows),
                   "added": adds, "deleted": dels,
                   "code": tally([f for f in rows if not f["generated"]]),
                   "generated": tally([f for f in rows if f["generated"]])},
        "clean": not commits,
    }
    if not ctx.dry:
        path = Path(".human-review/aftermath.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    if out["totals"]["code"]["files"]:
        ctx.notes.append(
            f"{len(commits)} commit(s) landed after the review commit and "
            f"{out['totals']['code']['files']} of the files they touched are not "
            "generated — the Review tab opens with a red band naming them. Say in the "
            "guide what they were; everything else on this page describes the branch as "
            "the agent left it")
    elif commits:
        ctx.notes.append(f"{len(commits)} commit(s) after the review commit, all of them "
                         "generated files only — the band is grey, which is correct")


def _diagrams(ctx: Ctx):
    sh(f"{HERE}/puml-diff.sh {ctx.base} {ART}/diagrams", ctx)
    d = ctx.step_cfg("diagrams").get("drawio")
    if not d:
        ctx.notes.append("no drawio diagram configured")
        return
    # `redraw` is the repository's own patch script — the thing that draws what the code
    # has and the map lacks, in red, as a to-do. Passed through rather than guessed: the
    # report turns it into a button that rewrites a checked-in file, and a command derived
    # from a naming convention is not something to hand a reader a button for. Without it
    # the page simply does not offer to start over, which is the honest rendering of "this
    # project has no such script".
    redraw = f" --redraw {shlex.quote(d['redraw'])}" if d.get("redraw") else ""
    # What a unit test checks the drawing against, for the sentence under the picture.
    if d.get("tested_against"):
        redraw += f" --tested-against {shlex.quote(d['tested_against'])}"
        if d.get("tested_against_path"):
            redraw += f" --tested-against-path {shlex.quote(d['tested_against_path'])}"
    sh(f"{HERE}/drawio-diff.py --base {ctx.base} --diagram {d['diagram']} "
       f"--concepts {d['concepts']} --out-dir {ART} --name {d.get('name', 'conceptual')}"
       + redraw, ctx)


def _sequence(ctx: Ctx):
    """The traced suites, against a stack this step can start for itself.

    `sequence.app` is the same block `video.app` is, and exists for a sharper version of
    the same reason: these commands drive a browser, a backend, a database AND a collector,
    and their output is *committed* — `generated/*.genseq.puml`. A run that reached some
    other checkout listening on the same port does not fail; it draws that checkout's code,
    in this branch's name, and the diagrams go into the repository looking like everyone
    else's. Without the block nothing changes: the commands run against whatever the
    operator has up, which is how every project used this step until now.

    What the suite needs beyond the app itself — a trace collector, the agent attached to
    the backend at *its* boot — is the project's business, and its own `up` is the only
    thing that can know. When there is no `app` and the commands fail, the failure carries
    the list, so the page says what has to be listening instead of only that a step died.
    """
    cfg = ctx.step_cfg("sequence")
    commands = cfg.get("commands") or []
    verdict = SEQ_VERDICT
    # The branch's own tests, on top of the tagged ones (`steps.sequence.select`): what the
    # commands' `{tests.<suite>}` expand to. Without `select` they expand to nothing and the
    # tagged suites run exactly as they always did.
    select = cfg.get("select") if isinstance(cfg.get("select"), dict) else None
    selection = select_traced(_test_manifest(ctx), select) if select and not ctx.dry else None
    values = selection_values(selection, select or {})
    if not ctx.dry:
        write_selection(selection)
    if selection and selection["picked"]:
        ctx.notes.append(
            f"also traced {len(selection['picked'])} test(s) this branch wrote or edited, "
            "untagged: " + "; ".join(f"{Path(t['path']).name}: {t['name']}"
                                     for t in selection["picked"])
            + (f" — {sum(1 for t in selection['left'] if t.get('suite'))} more over the cap "
               f"of {selection['max']}" if any(t.get("suite") for t in selection["left"])
               else ""))
    # The previous run's drawings go before anything can fail: a run that is skipped below
    # shows the committed diagrams, and a stale copy of last time's would win over them.
    # Its trace shot with them: a picture of last run's trace, offered as this run's.
    if not ctx.dry:
        shutil.rmtree(GENSEQ_OVERLAY, ignore_errors=True)
        for p in TRACE_SHOT_FILES:
            p.unlink(missing_ok=True)
    # Asked first, and only when the step will not start the stack itself: with `app` the
    # addresses do not exist until `up` creates them (the same reasoning `_dsaudit_prereq`
    # has). In the step and not in its prerequisite because the Sequence tab has to say why
    # it was not re-traced, and the verdict file is the only thing the tab reads.
    if not cfg.get("app") and not ctx.dry:
        down = unmet_requires(cfg.get("requires"))
        if down:
            reason = ("the traced suites need " + ", ".join(down) + " and nothing answers "
                      "there, so nothing was run. Start them — or configure "
                      "`steps.sequence.app` to have this step start them — and re-run "
                      "--only sequence")
            write_seq_verdict("skipped", reason, missing=down)
            raise LookupError(reason)
    before = {} if ctx.dry else genseq_stamps()
    # The committed diagrams' bytes, held so the run can put every one of them back. What it
    # drew goes into the review directory instead (`keep_drawn_and_restore`), the way the
    # city went off the repository in 3a22a1d: eval run 6 left six tracked
    # `generated/*.genseq.*` files modified on the branch under review.
    held = {} if ctx.dry else {p: (Path(p).read_bytes() if Path(p).is_file() else None)
                               for p in genseq_files()}
    runs = []

    def shoot(app: AppInstance, since: float) -> None:
        # While the stack is up — its trace store goes down with it. Among the diagrams this
        # run drew (written since the commands started); the branch's own tests first.
        drawn_now = drawn_since(since)
        _trace_shot(ctx, cfg, app, drawn_now, prefer_diagrams(drawn_now, selection), since)

    try:
        _run_traced(ctx, cfg, commands, runs, values, after=shoot)
    finally:
        # Before the restore below, which rewrites the files it puts back and would
        # otherwise read as diagrams this run drew.
        drawn = [] if ctx.dry else sorted(p for p, st in genseq_stamps().items()
                                          if before.get(p) != st)
        kept, put_back = ([], []) if ctx.dry else keep_drawn_and_restore(held)
    if selection is not None and not ctx.dry:
        # What each command cost, beside what it was asked to trace: the price of tracing
        # the branch's tests is the `when` run's seconds plus what the shared runs grew by.
        write_selection({**selection, "runs": [{"command": r["command"],
                                                "seconds": r.get("seconds")} for r in runs]})
    failed = [f"{r['command']} (exit {r['exit']})" for r in runs if r["outcome"] == FAILED]
    for r in runs:
        if r["outcome"] == NO_TESTS:
            ctx.notes.append(f"{r['command']}: {r['detail']} — a tag filter that matched "
                             "nothing, not a red suite")

    # The restore above is ALWAYS, and that is the whole reason the loop does not raise.
    # These commands sweep `generated/` before they regenerate it, so a suite that dies in
    # the middle leaves diagrams DELETED — including the other suites', which it never meant
    # to touch and cannot put back. Unrestored, the branch would be reported, in its own
    # voice, as having removed a picture.
    gone = [p for p in put_back if p.endswith(".genseq.puml")]
    if gone:
        ctx.notes.append(f"restored {len(gone)} diagram file(s) a failed suite deleted "
                         "without regenerating; say in the guide that the suite could not run")
    sh(f"{HERE}/puml-diff.sh {ctx.base} {ART}/diagrams", ctx)
    if ctx.dry:
        return
    lost = lost_vs_committed(kept)
    if lost:
        said = "; ".join(f"{Path(x['diagram']).name}: "
                         + ", ".join(x["participants"] or x["calls"]) for x in lost)
        ctx.notes.append(f"{len(lost)} re-traced diagram(s) LOST what the committed one shows "
                         f"({said}) — the Sequence tab flags them; say in the guide whether "
                         "the code stopped making those calls or the traced stack missed them")

    # "Drew something THIS run", never "a diagram exists": the committed ones always do,
    # and asking `has_genseq()` here turned a suite that could not even start into a "ran"
    # step with a RED note — on every run of a machine without the trace collector up, and
    # the tab kept showing the committed pictures as if they were this branch's.
    if not drawn:
        # A command that exited 0 and drew nothing did not pass anything: the generator
        # that says `fetch failed — skipped` three times and `Generated 0 diagram(s)` exits
        # 0, and the band used to print it as `passed` under "drew no diagram".
        for r in runs:
            if r["outcome"] == RAN:
                r["outcome"], r["detail"] = DREW_NOTHING, drew_nothing(r.get("skips"))
        said = "; ".join(f"{r['command']} — " + (
            f"exit {r['exit']}: {r['detail']}" if r["outcome"] == FAILED else r["detail"])
            for r in runs) or "no commands configured"
        reason = ("the traced suites drew no diagram on this run, so the Sequence tab shows "
                  "the committed ones, not this branch re-traced: " + said)
        if failed:
            reason += (". It needs the whole stack listening — a trace collector, the "
                       "database, the backend started AFTER the collector so its agent "
                       "attaches, and the front end. Configure `steps.sequence.app` to have "
                       "this step start them itself (or `steps.sequence.requires` to have it "
                       "say so before running anything), or start them by hand and re-run "
                       "this step")
        write_seq_verdict("skipped", reason, runs=runs)
        raise LookupError(reason)
    if failed:
        # A red suite is a finding for the review to carry, not a reason to lose the tab —
        # the same call `_city` makes. The pictures a passing part of the run did draw are
        # still pictures of this branch.
        ctx.notes.append("the traced suite was RED (" + "; ".join(failed)
                         + "); the diagrams below are of that run, and the guide has to say so")
        write_seq_verdict("red", "; ".join(failed), runs=runs, drawn=drawn, lost=lost)
    elif any(r["outcome"] == NO_TESTS for r in runs):
        write_seq_verdict("notests", "", runs=runs, drawn=drawn, lost=lost)
    elif lost:
        write_seq_verdict("degraded", "", runs=runs, drawn=drawn, lost=lost)
    else:
        # A verdict left behind by the previous run would draw a band over diagrams that
        # are now fine — the same reason `_video` deletes its own.
        verdict.unlink(missing_ok=True)


#: Which tests `_sequence` traced beyond the tagged ones, and why — for the Sequence tab to
#: say beside each picture whether it exists because somebody tagged the test or because
#: this branch wrote it, and to name the branch's tests that were left untraced. Eval run 8:
#: only `@generate_sequence` / `@GenerateSequence` tests were traced, so none of the paging
#: and sorting scenarios the branch added got a picture, and two of the three diagrams were
#: visit flows that only touched the changed `GET /api/owners` in their setup.
SEQ_SELECTION = ART / "sequence.selection.json"

#: `steps.sequence.select` defaults. `max` bounds the run: every traced test costs a Tempo
#: fetch and a picture, and a JVM suite a context boot on top.
SELECT_MAX = 6
SELECT_STATUSES = ("added", "modified")
#: What marks a test as already traced by its tag, looked for on the lines that belong to
#: its declaration (annotations / Gherkin tags above it, a Playwright options object below).
SELECT_TAGGED = r"@generate_sequence\b|@GenerateSequence\b|GENERATE_SEQUENCE_TAG"
#: `{tests.<suite>}` in a command: that suite's selection, joined and shell-quoted.
_SEL_PLACEHOLDER = re.compile(r"\{tests\.([A-Za-z0-9_-]+)\}")


def _test_manifest(ctx: Ctx) -> dict:
    """The branch's test manifest — `test-changes.py`, the `tests` step's producer.

    Re-derived here rather than read from `assets/test-changes.json`: the two steps run in
    parallel, a `--only sequence` run has no fresh copy, and a stale one would name the tests
    of whatever HEAD was reviewed last. It costs half a second."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "test-changes.json"
        r = sh(f"{HERE}/test-changes.py --base {shlex.quote(ctx.base)} --out {out}", ctx,
               check=False, capture=True)
        try:
            doc = json.loads(out.read_text(encoding="utf-8")) if r.returncode == 0 else {}
        except (OSError, ValueError):
            doc = {}
    return doc if isinstance(doc, dict) else {}


def _carries_tag(path: str, line: int, tagged: "re.Pattern[str]") -> bool:
    """Whether the test declared at `line` (1-based) of `path` already carries the tracing
    tag: on the annotation/tag lines directly above it, or the two lines after it, where a
    Playwright `{tag: …}` options object sits."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if not 0 < line <= len(lines):
        return False
    i = line - 1
    while i < len(lines) - 1 and lines[i].strip().startswith("@"):
        i += 1                         # a manifest line on the annotation, not the declaration
    lo = i
    while lo > 0 and lines[lo - 1].strip().startswith("@"):
        lo -= 1
    return any(tagged.search(x) for x in lines[lo:i + 3])


def select_traced(manifest: dict, select: dict) -> dict:
    """Which of the branch's added or edited tests to trace on top of the tagged ones.

    A test is a candidate when the manifest says this branch added or modified it and a
    configured suite runs its file (`suites.<name>.files`, minus `exclude`); a test that
    already carries the tracing tag is traced by its tag and takes no slot. Within a suite,
    the file the branch wrote the most tests in comes first — that is the file about the
    change, where the one migration test or the edited neighbour is not — then added before
    edited, then file order. Suites take turns, in the order the config lists them, so each
    gets its best test before any gets a second, until `max` (or a suite's own `max`).

    Returns `{"max", "picked": [...], "left": [...], "suites": {name: [picked…]}}`, each
    test as `{path, name, line, status, suite, why}`; `left` says why each candidate was not
    traced — over the cap, or no traced suite runs its file."""
    suites = select.get("suites") or {}
    cap = int(select.get("max") or SELECT_MAX)
    statuses = tuple(select.get("statuses") or SELECT_STATUSES)
    tagged = re.compile(select.get("tagged") or SELECT_TAGGED)
    rxs = {name: ([glob_rx(g) for g in (s.get("files") or [])],
                  [glob_rx(g) for g in (s.get("exclude") or [])])
           for name, s in suites.items() if isinstance(s, dict)}
    pool: dict[str, list[dict]] = {name: [] for name in rxs}
    left: list[dict] = []
    for t in manifest.get("tests") or []:
        if not isinstance(t, dict) or t.get("status") not in statuses:
            continue
        path, name = str(t.get("path") or ""), str(t.get("name") or "")
        if not path or not name:
            continue
        entry = {"path": path, "name": name, "line": int(t.get("line") or 0),
                 "status": t["status"],
                 "why": "written by this branch" if t["status"] == "added"
                 else "edited by this branch"}
        suite = next((n for n, (inc, exc) in rxs.items()
                      if any(r.fullmatch(path) for r in inc)
                      and not any(r.fullmatch(path) for r in exc)), None)
        if suite is None:
            left.append({**entry, "suite": None, "left": "no traced suite runs this file"})
            continue
        if _carries_tag(path, entry["line"], tagged):
            continue                   # traced anyway, by its tag
        pool[suite].append({**entry, "suite": suite})
    for name, tests in pool.items():
        weight: dict[str, int] = {}
        for t in tests:
            weight[t["path"]] = weight.get(t["path"], 0) + 1
        tests.sort(key=lambda t: (-weight[t["path"]], t["status"] != "added", t["path"],
                                  t["line"]))
    picked: list[dict] = []
    taken = {name: 0 for name in pool}
    queues = {name: list(tests) for name, tests in pool.items()}
    while len(picked) < cap:
        moved = False
        for name in pool:
            limit = int((suites.get(name) or {}).get("max") or cap)
            if queues[name] and taken[name] < limit and len(picked) < cap:
                picked.append(queues[name].pop(0))
                taken[name] += 1
                moved = True
        if not moved:
            break
    for tests in queues.values():
        left += [{**t, "left": f"over the cap of {cap} traced tests"
                  if len(picked) >= cap else "over its suite's cap"} for t in tests]
    return {"max": cap, "picked": picked, "left": left,
            "suites": {name: [t for t in picked if t["suite"] == name] for name in pool}}


def _selection_item(test: dict, template: str) -> str:
    """One test as its suite's command spells it: `{name}`, `{file}` (basename), `{path}`,
    `{line}`, `{class}` (the file's stem — a JUnit class)."""
    path = test["path"]
    fields = {"name": test["name"], "file": Path(path).name, "path": path,
              "line": str(test.get("line") or ""), "class": Path(path).stem}
    return re.sub(r"\{(name|file|path|line|class)\}", lambda m: fields[m.group(1)], template)


def selection_values(selection: dict | None, select: dict) -> dict[str, str]:
    """`{suite: "<item><join><item>…"}` — unquoted; `expand_selection` quotes."""
    out = {}
    for name, s in (select.get("suites") or {}).items():
        if not isinstance(s, dict):
            continue
        items = [_selection_item(t, s.get("item") or "{name}")
                 for t in ((selection or {}).get("suites") or {}).get(name) or []]
        out[name] = (s.get("join") if s.get("join") is not None else "\n").join(items)
    return out


def expand_selection(command: str, values: dict[str, str]) -> str:
    """`{tests.<suite>}` → that suite's selection as ONE shell word (`''` when empty), so a
    title with a quote or a newline in it reaches the command intact."""
    return _SEL_PLACEHOLDER.sub(lambda m: shlex.quote(values.get(m.group(1), "")), command)


def traced_commands(commands, values: dict[str, str]) -> list[tuple[str, str]]:
    """`(as configured, as run)` for every command that runs.

    A command is a string, run always, or `{"run": "...", "when": "<suite>"}`, run only when
    that suite has a selection — the JVM run that exists only to trace the selected tests
    must not boot a Spring context to trace nothing."""
    out = []
    for c in commands or []:
        if isinstance(c, dict):
            when = c.get("when")
            if when and not values.get(when):
                continue
            c = c.get("run") or ""
        if c:
            out.append((c, expand_selection(c, values)))
    return out


def write_selection(selection: dict | None) -> None:
    if selection is None:
        SEQ_SELECTION.unlink(missing_ok=True)
        return
    SEQ_SELECTION.parent.mkdir(parents=True, exist_ok=True)
    SEQ_SELECTION.write_text(json.dumps({**selection, "at": _stamp()}, indent=1) + "\n",
                             encoding="utf-8")


def _run_traced(ctx: Ctx, cfg: dict, commands, runs: list[dict],
                values: dict[str, str] | None = None, after=None) -> None:
    """Start the stack (when `app` says how), probe what it must carry, run the commands —
    then `after(app, started)`, still inside the block, while the stack is up."""
    t_start = time.time()
    with app_instance(ctx, cfg.get("app"), _app_slots(ctx)) as app:
        if app.started:
            ctx.notes.append(f"the traced suites ran against {app.base}, started by this run "
                             "from the commit under review — not whatever was already listening")
        # With `app`, `requires` is asked now that the addresses exist: its `{NAME}`s are
        # the instance's own `vars` (`{GRAFANA_URL}/api/health`). An `up` that came back
        # without the collector — a commit whose stack has none — is a skip that says so,
        # not three commands that pass and draw nothing. Inside the block, so `down` runs.
        if cfg.get("app") and not ctx.dry and cfg.get("requires"):
            wanted = [{**r, "url": expand_vars(r.get("url", ""), app.vars)}
                      if isinstance(r, dict) else expand_vars(str(r), app.vars)
                      for r in cfg["requires"]]
            down = unmet_requires(wanted)
            if down:
                reason = ("the stack this step started for the traced suites has no "
                          + ", ".join(down) + ", so nothing was run. Its `up` has to bring "
                          "them, and `app.vars` has to print where — then re-run "
                          "--only sequence")
                write_seq_verdict("skipped", reason, missing=down)
                raise LookupError(reason)
        # A Playwright suite in these commands writes its html report to a folder of its
        # own. The default is the one the traced browser run (`city.tests`, run by
        # `traces`) writes and `traces` harvests: eval run 17's one-test sequence run overwrote it 6 s before
        # the harvest, and every Playwright UI row lost its 🎭 replay. The env var beats
        # the config's outputFolder; a suite that is not Playwright ignores it.
        # Outside .human-review/, which is published whole.
        scratch = Path(tempfile.gettempdir()) / ("hr-sequence-report-" + hashlib.sha1(
            str(Path.cwd()).encode()).hexdigest()[:10])
        own_report = (f"export PLAYWRIGHT_HTML_OUTPUT_DIR={shlex.quote(str(scratch))} "
                      "PLAYWRIGHT_HTML_OPEN=never; ")
        for cmd, expanded in traced_commands(commands, values or {}):
            t0 = time.monotonic()
            r = sh(app.command(own_report + expanded), ctx, check=False, capture=True)
            out = (r.stdout or "") + (r.stderr or "")
            print(out, end="", flush=True)
            outcome, detail = suite_outcome(r.returncode, out)
            runs.append({"command": cmd, "exit": r.returncode, "outcome": outcome,
                         "detail": detail, "log": _tail(out), "skips": skipped_lines(out),
                         "seconds": round(time.monotonic() - t0, 1)})
        if after is not None and not ctx.dry:
            after(app, t_start)


#: The Sequence tab's picture of one real trace (`trace-shot.py`) and what it shows. Read by
#: `hrbuild/tabs/sequence.py` (`TRACE_SHOT`, `TRACE_SHOT_META`).
TRACE_SHOT = ART / "sequence.trace.png"
TRACE_SHOT_FILES = (TRACE_SHOT, TRACE_SHOT.with_suffix(".json"))


def drawn_since(since: float) -> list[str]:
    """The traced diagrams written since `since` (epoch seconds) — what the commands that
    started then drew, asked of the files themselves while they are still the run's."""
    out = []
    for p in genseq_files():
        try:
            if p.endswith(".genseq.puml") and os.stat(p).st_mtime >= since:
                out.append(p)
        except OSError:
            continue
    return out


def prefer_diagrams(drawn: list[str], selection: dict | None) -> list[str]:
    """The drawn diagrams of the tests this branch wrote or edited (`select`'s picks).

    A diagram is named `<test file>.<scenario slug>.genseq.puml`, and the pick knows the
    test file and the scenario's name — the same slug the generator made."""
    want = {f"{Path(t.get('path') or '').name}.{_slugify(t.get('name') or '')}"
            for t in (selection or {}).get("picked") or [] if isinstance(t, dict)}
    return [p for p in drawn
            if Path(p).name.removesuffix(".genseq.puml") in want]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _trace_shot(ctx: Ctx, cfg: dict, app: AppInstance, drawn: list[str],
                prefer: list[str], since: float) -> None:
    """One trace of this run, shot in Grafana, for the Sequence tab — never a failure.

    `steps.sequence.trace` is optional: `false` turns it off, `{"grafana": …,
    "attribute": …}` says where the store is and which span attribute carries a test's name.
    Without it the Grafana is the stack's own `GRAFANA_URL` (`app.vars`, what `start-docker.sh
    ports` prints for a traced instance), else the environment's. No Grafana, no trace, no
    browser: a note, and the tab offers no picture — it never stops the diagrams."""
    tcfg = cfg.get("trace", {})
    if tcfg is False or not drawn:
        return
    tcfg = tcfg if isinstance(tcfg, dict) else {}
    grafana = expand_vars(str(tcfg.get("grafana") or ""), app.vars) if tcfg.get("grafana") \
        else app.vars.get("GRAFANA_URL") or os.environ.get("GRAFANA_URL", "")
    if not grafana or "{" in grafana:
        ctx.notes.append("no trace shot for the Sequence tab: no Grafana to take it from "
                         "(steps.sequence.trace.grafana, or GRAFANA_URL in app.vars)")
        return
    python = _playwright_python(ctx)
    if not python:
        ctx.notes.append("no trace shot for the Sequence tab: Playwright for Python could "
                         "not be provisioned (playwright-python.sh)")
        return
    args = [f"--grafana {shlex.quote(grafana)}", f"--since {int(since)}",
            f"--out {shlex.quote(str(TRACE_SHOT))}"]
    if tcfg.get("attribute"):
        args.append(f"--attribute {shlex.quote(str(tcfg['attribute']))}")
    args += [f"--diagram {shlex.quote(p)}" for p in drawn]
    args += [f"--prefer {shlex.quote(p)}" for p in prefer]
    r = sh(f"{shlex.quote(python[0])} {HERE}/trace-shot.py " + " ".join(args), ctx,
           check=False, capture=True)
    said = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    print("\n".join(said), flush=True)
    if r.returncode != 0:
        ctx.notes.append("no trace shot for the Sequence tab: "
                         + (said[-1] if said else f"trace-shot.py exit {r.returncode}"))


#: Where `_sequence` files what its traced run drew: the review directory, mirroring each
#: diagram's repository path. The committed `generated/*.genseq.*` files get their own bytes
#: back after every run, so a review never leaves the branch it reviews dirty — and every
#: reader of "this run's diagrams" (`puml-diff.sh`, `c2-from-sequence.py`, the Sequence tab)
#: reads through this copy first. `.head` pins it to the commit it was traced at: once HEAD
#: moves, the committed files are the newer truth and the copy is ignored.
GENSEQ_OVERLAY = ART / "genseq"


def genseq_files() -> list[str]:
    """Every traced diagram AND sidecar the run may rewrite, tracked or not — the
    `.genseq.json` beside each picture carries the payload ids drawn into it."""
    pats = ["*.genseq.puml", "*.genseq.json"]
    got = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          *pats], capture_output=True, text=True)
    if got.returncode == 0:
        found = [p for p in got.stdout.split("\n") if p]
    else:
        found = [str(p) for pat in pats for p in Path(".").rglob(pat)
                 if "node_modules" not in p.parts]
    return sorted(dict.fromkeys(p for p in found if not _is_review_dir(p)))


def keep_drawn_and_restore(held: dict) -> tuple[list[str], list[str]]:
    """Copy what the run changed into `GENSEQ_OVERLAY`, then put the work tree back exactly
    as `held` says it was — bytes, or absence. `(kept, put_back_deleted)`.

    Never `git checkout`: a diagram may carry somebody's uncommitted edit, and the bytes
    held here are the only copy of it. A diagram the run created where none was is copied
    and then removed, so a newly tagged test leaves nothing untracked behind either."""
    shutil.rmtree(GENSEQ_OVERLAY, ignore_errors=True)
    GENSEQ_OVERLAY.mkdir(parents=True, exist_ok=True)
    kept, put_back = [], []
    for rel in sorted(set(genseq_files()) | set(held)):
        path = Path(rel)
        now = path.read_bytes() if path.is_file() else None
        was = held.get(rel)
        if now is not None and now != was:
            dest = GENSEQ_OVERLAY / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(now)
            kept.append(rel)
        if was is None:
            if now is not None:
                path.unlink()
        elif now != was:
            if now is None:
                put_back.append(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(was)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    (GENSEQ_OVERLAY / ".head").write_text(head.stdout.strip() + "\n", encoding="utf-8")
    return kept, put_back


#: A lifeline declaration and a request arrow in a generated sequence. Responses (`-->`)
#: and self-calls (the test's own sentences, a span inside one service) are not calls
#: between two participants, and are left out of what a re-trace can be said to have lost.
_SEQ_DECL = re.compile(r'^(?:participant|actor|database|queue|collections|boundary|control'
                       r'|entity)\s+("[^"]*"|\S+)(?:\s+as\s+("[^"]*"|\S+))?\s*$')
_SEQ_CALL = re.compile(r'^("[^"]*"|[^\s"<>-]+)\s*->>?\s*("[^"]*"|[^\s"<>:-]+)\s*:\s*(.*)$')
_SEQ_LINK = re.compile(r"\[\[\S+?(?:\{[^}]*\})?\s+([^\]]*?)\s*\]\]")


def seq_inventory(text: str) -> tuple[list[str], list[str]]:
    """`(participants, calls)` of one generated sequence, each in first-seen order.

    A call is `Backend → NotificationService: POST /api/notifications/visit-booked` — its
    label with the generator's per-run handles (`[[genseq://09akplx{…} …]]`) and markers
    stripped, since those ids move on every run while the call does not."""
    unq = lambda n: n.strip().strip('"')
    parts: dict[str, None] = {}
    calls: dict[str, None] = {}
    for line in text.splitlines():
        line = line.strip()
        m = _SEQ_DECL.match(line)
        if m:
            parts[unq(m.group(2) or m.group(1))] = None
            continue
        m = _SEQ_CALL.match(line)
        if not m or unq(m.group(1)) == unq(m.group(2)):
            continue
        label = _SEQ_LINK.sub(lambda x: x.group(1), m.group(3))
        label = re.sub(r"\s+", " ", label.replace("\\n", " ").replace("⊕", "")
                       .replace("↗", "")).strip()
        calls[f"{unq(m.group(1))} → {unq(m.group(2))}: {label}"] = None
    return list(parts), list(calls)


def lost_vs_committed(kept: list[str]) -> list[dict]:
    """What each re-traced diagram no longer shows that the committed one (HEAD) does.

    Eval run 6: the backend's in-process AddVisitApiTest could not reach the traced
    instance's notification-service, the call failed best-effort, and the regenerated
    picture lost NotificationService, the SMS gateway and both calls — presented as "this
    run's diagrams" with no word that they contradicted the committed ones. A diagram the
    branch has no committed copy of has nothing to lose."""
    out = []
    for rel in kept:
        if not rel.endswith(".genseq.puml"):
            continue
        got = subprocess.run(["git", "show", f"HEAD:{rel}"], capture_output=True, text=True)
        if got.returncode != 0:
            continue
        try:
            now = (GENSEQ_OVERLAY / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        was_parts, was_calls = seq_inventory(got.stdout)
        now_parts, now_calls = seq_inventory(now)
        parts = [p for p in was_parts if p not in now_parts]
        calls = [c for c in was_calls if c not in now_calls]
        if parts or calls:
            out.append({"diagram": rel, "participants": parts, "calls": calls})
    return out


#: What `_sequence` leaves beside its diagrams whenever the run did not simply pass, for the
#: Sequence tab to draw as a band (`hrbuild/tabs/sequence.py`, `sequence_verdict_html`) —
#: the arrangement `_video` has with `feature.verdict.json`. The status table's reason used
#: to be the only record, and it reached the page only if a model copied it into the
#: guide, on the Review tab, while the Sequence tab itself was just struck through.
SEQ_VERDICT = ART / "sequence.verdict.json"

#: The two outcomes of a traced command besides passing. `NO_TESTS` is not a failure: a
#: JUnit suite class whose tag filter matched nothing (`-Dgroups=genseq` on a Cucumber
#: suite with no such scenario) throws NoTestsDiscovered and turns Maven red, and that is a
#: statement about the filter, not about the code under review.
NO_TESTS = "no-tests"
#: A command that exited 0 on a run that drew no diagram at all. Not `ran`: what the step
#: is for is the picture, and a zero exit with none is the generator skipping every scenario
#: (`fetch failed — skipped`) because the traces it reads were never recorded.
DREW_NOTHING = "drew-nothing"
_SKIP_LINE = re.compile(r"(?:\"[^\"]*\"|'[^']*')\s*:\s*(?P<why>.*\bskipped\b.*)$", re.I)
_NO_TESTS_RX = re.compile(r"NoTestsDiscovered|did not discover any tests|No tests to run"
                          r"|No tests were executed|No tests found", re.I)
_UNDISCOVERED = re.compile(r"Suite \[([^\]]+)\] did not discover any tests")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _tail(text: str, n: int = 14) -> list[str]:
    lines = [_ANSI.sub("", l).rstrip() for l in text.splitlines()]
    return [l for l in lines if l.strip()][-n:]


def suite_outcome(code: int, output: str) -> tuple[str, str]:
    """`(RAN | NO_TESTS | FAILED, what to say)` for one traced command.

    NO_TESTS only when every test the runner reports as broken is a suite that discovered
    nothing (or the runner said it found no test at all): one real failure beside it makes
    the command FAILED, because a filter that matched nothing must not launder a red test.
    """
    if code == 0:
        return RAN, "passed"
    lines = [_ANSI.sub("", l) for l in output.splitlines()]
    broken = [i for i, l in enumerate(lines)
              if re.search(r"<<< (?:FAILURE|ERROR)!", l) and "Tests run:" not in l]
    empty = sorted(set(_UNDISCOVERED.findall(output)))
    only_empty = all(any(_NO_TESTS_RX.search(x) for x in lines[i + 1:i + 3]) for i in broken)
    if _NO_TESTS_RX.search(output) and only_empty:
        named = ", ".join(s.rsplit(".", 1)[-1] for s in empty)
        return NO_TESTS, (f"{named} discovered no tests under the tag filter" if named
                          else "the runner found no test to run")
    last = _tail(output, 1)
    return FAILED, last[0].strip()[:240] if last else f"exit {code}"


def skipped_lines(output: str) -> list[str]:
    """What a generator said it skipped, one reason per scenario: `⚠️ "Add a visit…": fetch
    failed — skipped` -> `fetch failed — skipped`. The scenario names are dropped because
    the reasons are what repeat, and a reason said three times is one cause."""
    out = []
    for line in _ANSI.sub("", output or "").splitlines():
        m = _SKIP_LINE.search(line)
        if m:
            out.append(m["why"].strip())
    return out


def drew_nothing(skips) -> str:
    """`exit 0, and drew no diagram — "fetch failed — skipped" ×3`."""
    counts: dict[str, int] = {}
    for why in skips or ():
        counts[why] = counts.get(why, 0) + 1
    said = ", ".join(f"“{w}”" + (f" ×{n}" if n > 1 else "") for w, n in counts.items())
    return "exit 0, and drew no diagram" + (f" — {said}" if said else "")


def genseq_stamps() -> dict[str, tuple[int, int]]:
    """`{path: (mtime_ns, size)}` of every traced diagram on disk, tracked or not.

    What a run drew is what changed between two of these. Asked of git where there is one
    (so node_modules costs nothing), and of the directory where there is not."""
    got = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          "*.genseq.puml"], capture_output=True, text=True)
    paths = (got.stdout.split("\n") if got.returncode == 0 else
             [str(p) for p in Path(".").rglob("*.genseq.puml")
              if "node_modules" not in p.parts and not _is_review_dir(str(p))])
    out = {}
    for p in filter(None, paths):
        try:
            st = os.stat(p)
        except OSError:
            continue                   # tracked, and deleted by the run's own sweep
        out[p] = (st.st_mtime_ns, st.st_size)
    return out


def unmet_requires(requires) -> list[str]:
    """What `steps.sequence.requires` names that nothing answers at.

    Each entry is an http(s) URL (any HTTP status counts, as in `answers`), a
    `tcp://host:port`, or either of those as `{"url": …, "what": "Grafana"}`. The traced
    suites need things the app's own stack does not carry — a collector, a trace store —
    and a project that lists them here gets "nothing listening on :4318" before a single
    command runs, instead of a red step after all of them did."""
    down = []
    for item in requires or []:
        url, what = (item.get("url", ""), item.get("what", "")) if isinstance(item, dict) \
            else (str(item), "")
        m = re.match(r"tcp://([^:/]+):(\d+)", url)
        if m:
            try:
                socket.create_connection((m.group(1), int(m.group(2))), timeout=2).close()
                continue
            except OSError:
                pass
        elif answers(url):
            continue
        down.append(f"{what} ({url})" if what else url)
    return down


def write_seq_verdict(state: str, reason: str, *, missing=(), runs=(), drawn=(),
                      lost=()) -> None:
    """`lost` is `lost_vs_committed`'s list: the Sequence tab flags each of those diagrams,
    and the C2 card says it rests on them."""
    SEQ_VERDICT.parent.mkdir(parents=True, exist_ok=True)
    SEQ_VERDICT.write_text(json.dumps({
        "state": state, "reason": reason, "missing": list(missing),
        "runs": list(runs), "drawn": list(drawn), "lost": list(lost), "at": _stamp(),
    }, indent=1) + "\n", encoding="utf-8")


def _c2(ctx: Ctx):
    """The container view, projected from the sequence diagrams `_sequence` just drew.

    It runs as its own step rather than at the end of `_sequence` for two reasons that both
    come down to honesty about the run: a C2 that could not be drawn has to say so on its
    own row of the status table instead of quietly reducing the Sequence tab, and the two
    tabs are two answers a reviewer reaches for separately, so their costs must not be
    pooled. It writes to `assets/c2`, never `assets/diagrams`: `puml-diff.sh` does
    `rm -rf` on that directory on entry, and it is the LAST thing `_sequence` runs.
    """
    r = sh(f"{HERE}/c2-from-sequence.py --base {ctx.base} --out-dir {ART}/c2", ctx,
           check=False)
    if r.returncode == 3:
        raise LookupError("no traced sequence diagram to project a container view from — "
                          "the sequence step is what produces them")
    if r.returncode != 0:
        raise RuntimeError(f"c2-from-sequence.py exit {r.returncode}")
    # The project's own picture of the same thing, drawn by hand, checked against the
    # graph just projected: which of its arrows a test walked, which no test did, and
    # which calls the traces made that it does not draw. Here and not in `_diagrams`,
    # because it reads what this step wrote. The name is fixed — it is the token the
    # Structure tab's layout expands (`{{drawio:deployment}}`).
    cfg = ctx.step_cfg("c2")
    d = cfg.get("drawio")
    if d:
        graph = f"{ART}/c2/{cfg.get('name') or 'C2-Containers'}.json"
        extra = (f" --tested-against {shlex.quote(d['tested_against'])}"
                 if d.get("tested_against") else "")
        if d.get("tested_against") and d.get("tested_against_path"):
            extra += f" --tested-against-path {shlex.quote(d['tested_against_path'])}"
        if d.get("participant"):
            extra += f" --trace-attr {shlex.quote(d['participant'])}"
        sh(f"{HERE}/drawio-diff.py --base {ctx.base} --diagram {shlex.quote(d['diagram'])} "
           f"--out-dir {ART} --name deployment --traces {graph}" + extra, ctx)


def _playwright_python(ctx: Ctx) -> list:
    """`[path]` of an interpreter that can `import playwright`, or `[]` when none could be
    provisioned. `run-steps` itself may run on a Python without it (python3.14 here)."""
    py = sh(f"{HERE}/playwright-python.sh", ctx, check=False, capture=True)
    return (py.stdout or "").strip().splitlines()[-1:] if py.returncode == 0 else []


def has_dsl() -> bool:
    """Whether the repository keeps a Structurizr DSL file — the `c4` step's whole input."""
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "--", "*.dsl"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def _c4(ctx: Ctx):
    """The repository's own C4 views (Structurizr DSL), drawn by Structurizr itself.

    `structurizr-views.py` exports the workspace as Structurizr's static viewer in a
    throwaway container and asks that viewer for an SVG of every view, light and dark, at
    the work tree and at the merge-base. Soft on a machine without Docker: the step is
    skipped with the reason, and the card on the Structure tab says it in place."""
    python = _playwright_python(ctx)
    run = f"{shlex.quote(python[0])} {HERE}/structurizr-views.py" if python \
        else f"{HERE}/structurizr-views.py"
    r = sh(f"{run} --base {ctx.base} --out-dir {ART}/c4", ctx, check=False)
    if r.returncode == 3:
        raise LookupError("no Structurizr DSL workspace in this repository")
    if r.returncode == 4:
        raise LookupError("Docker (or Playwright) is not available — the Structure tab "
                          "says the C4 views were not drawn")
    if r.returncode != 0:
        raise RuntimeError(f"structurizr-views.py exit {r.returncode}")


def _city_tests(ctx: Ctx) -> None:
    """Run `city.tests` — the traced browser suite — once, for everything that reads it.

    The key keeps its old name and its old home in the config, but the run left `_city` on
    5 Oct 2026. It was here for the city's own coverage colours, which code-city stopped
    computing; what it kept feeding was the Playwright report `traces` harvests and the
    per-test browser coverage `testcov` reads. The city now colours itself from THAT
    measurement (`city-coverage.py`), so the city has to come after `testcov`, `testcov`
    after this run — and a run inside `_city` would have been a cycle. So `traces` runs
    it, first, before its own commands: the step that harvests a run is the step that
    starts it.

    Failures do not stop it. A red suite is a finding for the review to carry, and the
    coverage of a run with one broken test is still the coverage of that run. A list,
    because a project may need several commands in order; a single string still works."""
    tests = ctx.step_cfg("city").get("tests")
    if isinstance(tests, str):
        tests = [tests]
    for cmd in tests or []:
        r = sh(cmd, ctx, check=False)
        if r.returncode != 0:
            ctx.notes.append(f"the traced browser suite did not pass ({cmd}); its recordings "
                             "and its coverage are of that run")


def city_coverage(ctx: Ctx) -> Path | None:
    """The coverage JSON the city is coloured with, converted from the test step's own
    measurement (`assets/test-coverage.json`) — or None, and the city is built as it was.

    Nothing is run: this reads what `testcov` wrote. Run on its own (the Code City tab's
    ⚙️), it reuses whatever coverage is on disk; after a ⏳ the run before it has just
    rewritten that file. Coverage of another commit is still used, and said so: a stale
    colour named as stale is more use to a reviewer than a grey plate."""
    src, dest = ART / "test-coverage.json", ART / "codecity-coverage.json"
    if ctx.dry:
        return dest
    dest.unlink(missing_ok=True)       # never colour this run with a previous run's file
    r = sh(f"{HERE}/city-coverage.py --in {src} --out {dest}", ctx, check=False)
    if r.returncode != 0 or not dest.is_file():
        ctx.notes.append("no coverage colours on the city: " + (
            "the test step has not measured any (assets/test-coverage.json)"
            if not src.is_file() else
            "assets/test-coverage.json predates the whole-project map — re-run the tests"))
        return None
    try:
        measured = json.loads(dest.read_text(encoding="utf-8")).get("commit") or ""
    except ValueError:
        measured = ""
    head = sh("git rev-parse HEAD", ctx, capture=True, check=False).stdout.strip()
    if measured and head and not head.startswith(measured):
        ctx.notes.append(f"the city's coverage colours were measured on {measured[:8]}, not "
                         f"on HEAD {head[:8]} — re-run the tests (⏳) to bring them up")
    return dest


def _city(ctx: Ctx):
    # The CRAP colours still need a JaCoCo report, which nothing here produces any more.
    # Line and acceptance coverage do not: the test step (`testcov`) already measured both,
    # per test, for the Tests tab, and `city_coverage` hands that to the generator. No test
    # is run from here — the traced browser suite moved to `traces` (see `_city_tests`).
    cov = city_coverage(ctx)
    # Two ways to get the page. `regenerate` is a project's own command, for a project
    # that has one; `out` hands the job to the script here, which is the arrangement to
    # prefer — it leaves the analysed repo holding only the DATA (a committed
    # codecity.html, a committed baseline) and none of the machinery that made it.
    #
    # `baseline` names the committed coverage numbers the page compares AGAINST. It is
    # never written from here: this runs on a branch under review, and refreshing the
    # baseline with the branch's own coverage would replace the very numbers the
    # comparison needs. That belongs to a merge into the default branch
    # (regenerate-codecity.sh --write-baseline), nowhere else.
    #
    # Wherever the page is generated, it is generated into the REVIEW directory —
    # `assets/codecity/codecity.html`, the very file the tab links to — and never over the
    # repository's own committed copy. `out` used to be passed straight through as the
    # output folder, so every run rewrote `docs/generated/codecity/codecity.html` in the
    # working tree: a dirty checkout after every review, and a pre-push gate on the project
    # demanding that the review's by-product be committed. `out` is now only the switch
    # that says "use the built-in generator"; the committed copy is the project's business.
    city = ctx.step_cfg("city")
    regen, out = city.get("regenerate"), city.get("out")
    page = ART / "codecity" / "codecity.html"
    # The city lights what changed since the run's base, not since origin/main, which
    # code-city picks on its own — its "since origin/main" was the page's second base.
    mb = merge_base(ctx)
    os.environ["HEATMAP_CHANGED_BASE"] = mb[:12] if re.fullmatch(r"[0-9a-f]{40}", mb) else mb
    if regen:
        # A project's own command may only know how to write in place. `html` says where,
        # and the original bytes are put back after the new page has been copied out.
        # Its coverage, if it calls code-city's generate.sh, rides the env var that reads.
        if cov:
            os.environ["CODECITY_COVERAGE"] = str(cov.resolve())
        else:
            os.environ.pop("CODECITY_COVERAGE", None)
        inplace = city.get("html")
        with bytes_restored([inplace] if inplace else []):
            sh(regen, ctx, check=False)
            if inplace and Path(inplace).is_file() and not ctx.dry:
                page.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(inplace, page)
        if not inplace:
            ctx.notes.append("city.regenerate ran with no city.html saying where it writes "
                             "the page, so nothing guards the working tree against it — "
                             "check `git status` after this run")
    elif out:
        baseline = f' --baseline "{city["baseline"]}"' if city.get("baseline") else ""
        acceptance = f' --acceptance "{city["acceptance"]}"' if city.get("acceptance") else ""
        coverage = f' --coverage "{cov}"' if cov else ""
        sh(f'{HERE}/regenerate-codecity.sh --out "{page.parent}" '
           f'--title "{city.get("title", "Code City")}"{baseline}{acceptance}{coverage}', ctx,
           check=False)
    r = sh(f"{HERE}/capture-codecity.sh {ART}/codecity.png highlight {page}", ctx, capture=True)
    lit = (r.stdout or "").strip().splitlines()
    if lit:
        ctx.notes.append(f"codecity lit: {lit[-1]} (put this measured number under the "
                         "image; never type one)")


@contextlib.contextmanager
def bytes_restored(paths):
    """Put each path back exactly as it was when the block began — bytes, or absence.

    For a generator that can only write in place over a committed file: copy the original
    out, let it run, and copy the original back, so the review leaves the working tree as
    it found it. Never `git checkout`: the file may carry somebody's uncommitted edit, and
    the bytes on disk are the only copy of that."""
    held = {}
    for p in paths:
        path = Path(p)
        held[path] = path.read_bytes() if path.is_file() else None
    try:
        yield
    finally:
        for path, data in held.items():
            if data is None:
                path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != data:
                path.write_bytes(data)


#: A loopback URL in a command's output. `start-docker.sh up` ends by printing the port
#: the host gave this instance, and that port is the one thing nobody can know in advance:
#: several branches are up at once on this machine, so the host picks. Same expression as
#: `serve-review.py` scrapes with, for the same reason and against the same commands.
APP_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?(?:/\S*)?")

#: How many lines of the recorder's own output a verdict carries. Enough for the note, the
#: exit reason and the two or three lines before them; not the whole run.
VERDICT_TAIL = 30


def _app_slots(ctx: Ctx, ref: str = "HEAD") -> dict:
    """`{sha, shortsha}` for a commit an `app` block is to be built from.

    Read from git rather than from the config, because the whole point of the block is
    that the film is made against *this* commit: a sha written into `human-review.json`
    would be a sha that is right until the next push.

    `ref` is HEAD for every step that drives the branch, and the merge-base for the one
    step that needs the *other* side up at the same time: `dsaudit` compares two running
    builds, so it asks for two sets of slots and starts two instances from one block.

    `shortsha` is asked of git too, never sliced to a fixed width. The instance a host
    names after a ref is named with git's own abbreviation (`rev-parse --short`), whose
    length is `core.abbrev` — `auto` by default, which grows with the repository. This one
    is at eight today; a hardcoded seven would have `down` naming an instance that does
    not exist, and `down` failing is a whole stack left running after every film.
    """
    full = sh(f"git rev-parse {ref}", ctx, capture=True, check=False).stdout.strip()
    short = sh(f"git rev-parse --short {ref}", ctx, capture=True, check=False).stdout.strip()
    return {"sha": full, "shortsha": short or full[:8]}


#: What an `app` block exports by default. `video` has always set both, and the same two
#: names carry a Playwright suite; a project whose tests read something else (petclinic's
#: `API_BASE_URL`) says so in `app.env` instead of renaming its tests around this default.
APP_ENV_DEFAULT = {"BASE_URL": "{url}", "API_URL": "{url}"}


class AppInstance:
    """The stack a step started for itself: its base URL and the env that points at it.

    Empty (`started` false, `env` "") when the step has no `app` block, which is the case
    every project started from and still the default — the commands then run against
    whatever the operator has listening, exactly as before.
    """

    def __init__(self, base: str = "", env: str = "", started: bool = False):
        self.base, self.env, self.started = base, env, started
        self.vars: dict[str, str] = {}

    def command(self, cmd: str) -> str:
        """`cmd` with this instance's env in force for ALL of it.

        `NAME=v cmd` binds NAME for the first simple command only, and the project's
        commands are compound — `cd petclinic-test && ./run-tests-with-tracing.sh`. The
        prefix used to land on the `cd` and nowhere else: the suite then fell back to the
        operator's fixed ports, and a Grafana that happened to be up on :3300 answered with
        another checkout's traces. `export` covers every command after it, in the one shell
        `sh` starts, and leaks nowhere."""
        return f"export {self.env.strip()}; {cmd}" if self.env else cmd


#: One line of what `app.vars` prints: a shell-style assignment, nothing else counts.
_APP_VAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\S*)\s*$")


def app_vars(output: str) -> dict[str, str]:
    """`{NAME: value}` off a command that prints `NAME=value` lines (`start-docker.sh ports`).

    The base URL is the only address an `up` can be scraped for, and a traced run needs
    more than one: the backend, the trace store, the collector the JVM under test exports
    to. All of them are host-picked, so none can be written into the config; a line that
    is not an assignment (a banner, a warning) is ignored rather than guessed at."""
    found = {}
    for line in _ANSI.sub("", output or "").splitlines():
        m = _APP_VAR.match(line.strip())
        if m:
            found[m.group(1)] = m.group(2)
    return found


def expand_vars(template: str, found: dict[str, str]) -> str:
    """`{NAME}` → that var's value, for `requires` entries that point into the instance."""
    for name, value in found.items():
        template = template.replace("{" + name + "}", value)
    return template


#: The steps whose output is a picture of the running app's screens. Each one brings the
#: instance's database back to its seed before it shoots (`app_instance(clean=True)`), and
#: each one holds the `stack` lane in `USES`, so no step that writes into that database can
#: run between the reset and the last capture. On 3 Oct 2026 (eval run 5) the design-system
#: audit shot the stack the Playwright suite had just written into: three of its four
#: "changed" screens were test data — `Join 26` → `Join 28 happy pet owners`, an e2e visit
#: row — and the film, on the same stack, opened its sorted list on two junk owners and
#: missed a caption that counted 26 of them.
CAPTURES = ("video", "dsaudit")


def _post(url: str, timeout: float = 30.0) -> bool:
    """POST with an empty body; True on any 2xx. The reset path a review page's own Reset
    button presses (`runtime.reset`), so the run and the reader reset the same way."""
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def to_seed(ctx: Ctx, cfg: dict, inst: "AppInstance", expand) -> str:
    """Bring a running instance's database back to its seed, before anything is captured.

    `app.reset` is how, when the project says: a path (`/__reset`) is POSTed to the
    instance — the very endpoint the page's Reset button calls — and anything else is a
    command (`./start-docker.sh reset petclinic-{shortsha}`, `{url}` = the instance). It is
    run even on an instance this step started: an `up` that found a stopped instance keeps
    its old volume, and a reset costs a second.

    Without `reset`, an instance this step started is taken as seeded — nothing has
    written to it yet — and one it found running (city's suite, the traces' cucumber run,
    a previous run's leftover) is recycled: `down` and `up` again, which drops the volume.
    Slower, never wrong; the note says how to make it cheap. Returns how it got there."""
    reset = (cfg.get("reset") or "").strip()
    if reset:
        ok = (_post(inst.base.rstrip("/") + reset) if reset.startswith("/") else
              sh(expand(reset).replace("{url}", inst.base), ctx, check=False).returncode == 0)
        if ok:
            return f"reset ({reset})"
        print(f"    reset did not answer ({reset}) — recycling the instance instead", flush=True)
    elif inst.started:
        return "started by this step, so still at its seed"
    if not (cfg.get("up") and cfg.get("down")):
        raise RuntimeError(
            f"the app at {inst.base} was already running — another step may have written "
            "into its database — and there is no `app.reset`, nor `up`/`down` to start it "
            "again from its seed")
    sh(expand(cfg["down"]), ctx, check=False)
    up = sh(expand(cfg["up"]), ctx, capture=True, check=False)
    print((up.stdout or "") + (up.stderr or ""), end="", flush=True)
    if up.returncode != 0:
        raise RuntimeError(f"the app would not start again from its seed: {expand(cfg['up'])}")
    found = APP_URL.findall(up.stdout or "")
    if not found and cfg.get("url"):
        got = sh(expand(cfg["url"]), ctx, capture=True, check=False)
        found = APP_URL.findall(got.stdout or "")
    if not found:
        raise RuntimeError("the app came back from its seed and printed no URL to reach it at")
    inst.base = found[-1].rstrip(".,)")
    ctx.notes.append(f"recycled {inst.base} (down + up) so the capture starts from the seed — "
                     "an `app.reset` in human-review.json would do it in a second")
    return "recycled: down + up"


@contextlib.contextmanager
def app_instance(ctx: Ctx, cfg: dict, sha: dict, clean: bool = False):
    """`up` the app this step runs against, hand back where it landed, `down` it after.

    Extracted from `_video`, which had it inline and alone. The reason it existed there is
    not about film: the recorder's liveness check tested the PORT and not the commit, and
    this machine keeps several checkouts of one repository that can each serve :4200 — so
    a review of `test-pr` was illustrated with a film of `main`. Every step that drives the
    running application has that same hazard, and the traced suites have it worse: a
    sequence diagram of the wrong checkout is not obviously wrong the way a film is, it is
    just a picture of some other code, drawn in this branch's name and committed.

    `app` is either the block itself or the string "video", which borrows `steps.video.app`
    rather than repeating it — the usual case, where one `up` builds the one stack every
    step would want. Borrowing is spelt out and never assumed: a stack that is right for
    the film is not automatically right for a suite that needs a collector behind it.

    `{sha}`/`{shortsha}` come from `git rev-parse HEAD`, never from the config.

    `clean` is for the steps in `CAPTURES`: the database is brought back to its seed
    (`to_seed`) before the step gets the instance, whoever started it.
    """
    if isinstance(cfg, str):
        if cfg != "video":
            raise LookupError(f'app: "{cfg}" — the only name that can be borrowed is "video"')
        cfg = (ctx.step_cfg("video").get("app") or {})
        if not cfg:
            raise LookupError('app: "video" — but steps.video.app is not configured')
    if not cfg:
        yield AppInstance()
        return

    def expand(template: str) -> str:
        for name, value in sha.items():
            template = template.replace("{" + name + "}", value)
            template = template.replace("{" + name.replace("sha", "SHA") + "}", value)
        return template

    inst = AppInstance()
    try:
        # Already up — started by an earlier step of this run (the traced browser suite
        # brings up the very stack video and dsaudit need): reuse it, and leave it up. Tearing
        # down a stack this step did not create was measured costing the run a full
        # rebuild: video's `down` removed city's stack, and dsaudit then rebuilt every
        # image and container of the same commit from scratch on the critical path.
        if cfg.get("url") and not ctx.dry:
            got = sh(expand(cfg["url"]), ctx, capture=True, check=False)
            found = APP_URL.findall(got.stdout or "") if got.returncode == 0 else []
            if found and answers(found[-1].rstrip(".,)")):
                inst.base = found[-1].rstrip(".,)")
                print(f"    reusing the stack already up at {inst.base}", flush=True)
        if cfg.get("up") and not inst.base:
            up = sh(expand(cfg["up"]), ctx, capture=True, check=False)
            # Echoed: a docker build's output is what a reader asks for when the step takes
            # four minutes, and `capture` is only here to scrape the port off it.
            print((up.stdout or "") + (up.stderr or ""), end="", flush=True)
            if up.returncode != 0:
                raise RuntimeError(f"the app would not start: {expand(cfg['up'])}")
            inst.started = True
            found = APP_URL.findall(up.stdout or "")
            inst.base = found[-1].rstrip(".,)") if found else ""
        if not inst.base and cfg.get("url"):
            got = sh(expand(cfg["url"]), ctx, capture=True, check=False)
            found = APP_URL.findall(got.stdout or "")
            inst.base = found[-1].rstrip(".,)") if found else ""
        if not inst.base and not ctx.dry:
            raise RuntimeError("the app started and printed no URL to reach it at — "
                               "`app.up` has to print it, or `app.url` has to")
        # Before the env is written: a recycled instance comes back on another port.
        if clean and inst.base and not ctx.dry:
            print(f"    seed: {to_seed(ctx, cfg, inst, expand)}", flush=True)
        if inst.base:
            names = cfg.get("env") or APP_ENV_DEFAULT
            inst.env = "".join(
                f"{k}={shlex.quote(v.replace('{url}', inst.base))} " for k, v in names.items())
            inst.env += (f"HUMAN_REVIEW_APP_COMMIT={shlex.quote(sha.get('sha', ''))} "
                         "HUMAN_REVIEW_APP_STARTED=1 ")
        # Every other address the instance published, exported under the names the
        # project's tooling reads. After `env`, so a var it also names wins: `vars` is read
        # off the running instance, `{url}` only off what `up` happened to print. A `vars`
        # that fails is fatal — the commands would otherwise fall back to the operator's
        # fixed ports, which is the very hazard the block exists to remove.
        if cfg.get("vars") and not ctx.dry:
            got = sh(expand(cfg["vars"]), ctx, capture=True, check=False)
            inst.vars = app_vars(got.stdout) if got.returncode == 0 else {}
            if not inst.vars:
                raise RuntimeError(f"the app is up but `{expand(cfg['vars'])}` printed no "
                                   "NAME=value line to point the commands at it")
            inst.env += "".join(f"{k}={shlex.quote(v)} " for k, v in inst.vars.items())
        yield inst
    finally:
        if inst.started and cfg.get("down"):
            # In a `finally`, and never `check`ed: a stack left up outlives the run, and the
            # reason the step failed is a better thing to report than the teardown.
            sh(expand(cfg["down"]), ctx, check=False)


def _video(ctx: Ctx):
    """Film the feature — and, where the project says how, start the stack it is filmed
    against and stop it afterwards.

    Without `steps.video.app` this behaves as it always did: the recorder checks that
    *something* answers on the URLs it was given and films whatever that is. That is the
    arrangement that produced a review of `test-pr` illustrated with a film of `main`:
    this machine keeps several checkouts of the same repository, each able to serve
    :4200, and nothing in the pipeline asked which one. The captions said what the frames
    denied, and the film is the one artifact on the page a reader believes without
    checking.

    With `app`, the run owns the instance: `up` builds the commit under review into its
    own container set, the host's port comes out of what it printed, and `down` runs in a
    `finally` so a film that crashed does not leave a stack behind. `{sha}`/`{shortsha}`
    are filled from `git rev-parse HEAD`, never from the config.

    And whatever happens, the recorder's own output is kept: `assets/feature.run.log`
    always, plus `assets/feature.verdict.json` when it exited non-zero. Exit 3 — filmed,
    and the feature did *not* hold — used to reach the page as a note in a status table a
    human had to read and carry into the prose, and on 17 Sep 2026 it did not: the step
    captured nothing, so three missed screens became a film that looked like any other
    demo. The verdict file is what makes that impossible to lose; `video_html` draws it
    over the player.
    """
    cfg = ctx.step_cfg("video")
    out = cfg.get("out", f"{ART}/feature.webm")
    logs = Path(f"{ART}/feature.run.log")
    verdict_path = Path(f"{ART}/feature.verdict.json")

    with app_instance(ctx, cfg.get("app"), _app_slots(ctx) if cfg.get("app") else {},
                      clean=True) as app:
        if app.base:
            ctx.notes.append(f"filmed against {app.base}, started by this run from the commit "
                             "under review — not whatever was already listening on :4200")
        r = sh(f"{app.env}{HERE}/record-feature-video.sh {out}", ctx, check=False, capture=True)

    # Written before anything is raised or noted, so the log survives every exit from here.
    tail = ((r.stdout or "") + (r.stderr or ""))
    print(tail, end="", flush=True)
    logs.parent.mkdir(parents=True, exist_ok=True)
    logs.write_text(tail, encoding="utf-8")

    if r.returncode == 0:
        # A verdict left behind by the previous run is worse than none: it would draw a
        # red banner over a film that is now fine.
        verdict_path.unlink(missing_ok=True)
        return

    lines = [l for l in tail.splitlines() if l.strip()]
    note = next((l for l in reversed(lines) if "changed screens filmed" in l), "")
    # Only exit 3 carries a film's own account of what it missed. Exit 2's output is the
    # recorder's help text, which *explains* the marker — and read for it, that text put
    # a false "Never reached: …`, which is the string run-steps.py reads back…" on the page.
    missed = []
    for label in ("FAILED to reach: ", "not filmable: ") if r.returncode == 3 else ():
        for line in lines:
            if label in line:
                missed += [p.strip() for p in line.split(label, 1)[1].split(";") if p.strip()]
    verdict_path.write_text(json.dumps({
        "exit": r.returncode,
        "note": note,
        "missed": missed,
        "log": lines[-VERDICT_TAIL:],
    }, indent=1), encoding="utf-8")

    if r.returncode == 3:
        ctx.notes.append("EXIT 3 — filmed, and the feature did NOT hold. Embed it and lead "
                         "the review with what it shows; this is the most valuable film "
                         "this pipeline can make"
                         + (f" ({len(missed)} screen(s) missed)" if missed else ""))
        return
    if r.returncode == 2:
        raise LookupError("no feature script, or the stack the film needs is not the "
                          "commit under review — see assets/feature.run.log")
    raise RuntimeError(f"record-feature-video.sh exit {r.returncode}")


def _complexity(ctx: Ctx):
    """Both sides of the entry-point complexity, then the bars between them.

    A project that measures this itself keeps doing so — `complexity.extract` runs, and
    `before`/`after` name its two snapshots. With nothing configured, the built-in
    extractor reads both sides out of the Java sources, the baseline straight from the
    merge-base, and the step needs no build, no plugin and no test in the project."""
    c = ctx.step_cfg("complexity")
    if c.get("extract"):
        sh(c["extract"], ctx)
    before, after = c.get("before"), c.get("after")
    if not (before and after):
        before, after = f"{ART}/complexity-before.json", f"{ART}/complexity-after.json"
        sh(f"{HERE}/endpoint-complexity.py --base {ctx.base} --out {before}", ctx)
        sh(f"{HERE}/endpoint-complexity.py --out {after}", ctx)
    sh(f"{HERE}/endpoint-complexity-delta.py {before} {after} --base {ctx.base} "
       f"--out {ART}/complexity-delta.html", ctx)
    sh(f"{HERE}/endpoint-complexity-delta.py --css > {ART}/complexity-delta.css", ctx)


def _api(ctx: Ctx):
    spec = ctx.cfg.get("spec", "openapi.yaml")
    for cmd in (
        # `--report` is the openable twin of the fragment, written in the same run so the
        # two cannot drift. The verdict band links to it by name, and only when it can see
        # it on disk — so it has to be written before the --panel call. oasdiff is the one
        # differ: our own openapi-diff.py was dropped on 7 Oct 2026, see
        # reference/openapi-differ-eval.md.
        f"{HERE}/openapi-compat.py --base {ctx.base} --spec {spec} --out {ART}/openapi-compat.html"
        f" --report {ART}/openapi-compat-report.html",
        f"{HERE}/openapi-compat.py --css  >  {ART}/openapi-compat.css",
        f"{HERE}/openapi-compat.py --base {ctx.base} --spec {spec} --panel "
        f"--out {ART}/openapi-verdict.html",
        f"{HERE}/openapi-visual-diff.py --base {ctx.base} --spec {spec} "
        f"--out {ART}/openapi-visual-diff.html",
    ):
        sh(cmd, ctx)
    if not have("oasdiff"):
        ctx.notes.append("no oasdiff — the seal reads COMPATIBLE · PARTIAL LIST in amber "
                         "and the list is a lower bound. That amber is correct; do not "
                         "'fix' it in the prose")


def _specchanges(ctx: Ctx):
    spec = ctx.cfg.get("spec", "openapi.yaml")
    base = merge_base(ctx)
    sh(f"openapi-changes html-report --no-logo --no-explorer "
       f"--report-file {ART}/openapi-changes.html '{base}:{spec}' ./{spec}", ctx)


def _logging(ctx: Ctx):
    paths = ctx.step_cfg("logging").get("paths") or []
    if not paths:
        raise LookupError("logging.paths not configured")
    base = merge_base(ctx)
    sh(f"{HERE}/logextract.py {' '.join(paths)} --repo . --since {base} "
       f"--json {ART}/logging.json", ctx)


# ── which screens did the branch change? ──────────────────────────────────────────
#
# The audit's `screens` used to be two entries somebody guessed the branch touched, copied
# from the example, and the one screen that mattered ("Edit a visit", changed by the
# branch) was not among them. Nothing looked at the diff. Now `screens` is the whole
# app's catalogue, the audit draws only the ones whose DOM changed, and the runner does the
# one check the audit cannot: it reads the diff for changed Angular components, follows
# them to their routes, and shouts about any route the catalogue does not reach.

ROUTE_TOKEN = re.compile(
    r"""(?P<open>\{)|(?P<close>\})|(?P<path>\bpath\s*:\s*(['"`])(?P<p>.*?)\4)"""
    r"""|(?P<comp>\bcomponent\s*:\s*(?P<c>[A-Za-z_]\w*))|(?P<children>\bchildren\s*:)"""
    r"""|(?P<comment>//[^\n]*|/\*.*?\*/)""", re.S)


def angular_routes(text: str) -> list[tuple[str, str]]:
    """`(route pattern, component class)` pairs out of one routing source, nested children
    joined onto their parent's path. A stack of open `{`s, not a TypeScript parser: each
    route object records its own `path:` and `component:`, and a `children:` array under
    it opens objects whose paths are prefixed with its own. Comments are skipped so a
    route somebody commented out is not a route."""
    out, stack = [], []          # one entry per open `{`: {"path", "full", "component"}
    for m in ROUTE_TOKEN.finditer(text):
        if m["comment"]:
            continue
        if m["open"]:
            # A child opened under a route inherits that route's full path; an object
            # opened before its parent's `path:` (or a `resolve: {…}`) inherits the
            # grandparent's, which is the right answer for both.
            stack.append({"path": None, "component": None,
                          "full": stack[-1]["full"] if stack else ""})
        elif m["close"]:
            if not stack:
                continue
            r = stack.pop()
            if r["component"] and r["path"] is not None:
                out.append((r["full"], r["component"]))
        elif m["path"] is not None and stack:
            r = stack[-1]
            r["path"] = m["p"].replace("\\/", "/").strip("/")
            r["full"] = "/".join(x for x in (r["full"], r["path"]) if x)
        elif m["comp"] and stack:
            stack[-1]["component"] = m["c"]
    return out


def route_matches(pattern: str, url: str) -> bool:
    """`pets/:id/visits/add` reaches `pets/11/visits/add`. A parameter takes one segment; a
    `**` takes the rest; the catalogue's URL is compared without its origin, query or hash."""
    path = re.sub(r"^https?://[^/]+", "", url).split("?", 1)[0].split("#", 1)[0].strip("/")
    rx = "/".join("[^/]+" if seg.startswith(":") else ".*" if seg == "**" else re.escape(seg)
                  for seg in pattern.strip("/").split("/") if seg)
    return re.fullmatch(rx, path) is not None


def _component_ts(rel: str) -> str | None:
    """The `.component.ts` behind any file of an Angular component; None for a spec or
    for a file that is not a component's."""
    m = re.match(r"(.*\.component)\.(ts|html|scss|css|less)$", rel)
    if not m or rel.endswith(".spec.ts"):
        return None
    return m.group(1) + ".ts"


def unlisted_screens(changed: list[str], catalogue: dict, sources: list[str],
                     root: Path = Path(".")) -> list[dict]:
    """The changed, routed components no catalogue screen reaches.

    `changed` is the diff's file list; a changed component that owns a route is checked
    directly, and one that does not (a child like `<app-visit-list>`) is followed one hop
    to the routed components whose templates embed its selector. Anything further — a
    pipe, a service, a shared stylesheet — is left to the DOM diff, which sees it on
    whichever catalogue screen renders it. The result is what the audit prints in red."""
    roots = [root / s for s in sources] or [root]
    routes: list[tuple[str, str]] = []
    for r in roots:
        for f in r.rglob("*.ts"):
            if f.name.endswith(".spec.ts") or "node_modules" in f.parts:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"\bRouter(Module\.for(Root|Child)|Config)\b|:\s*Routes\b", text):
                routes += angular_routes(text)
    by_class = {}
    for pattern, cls in routes:
        by_class.setdefault(cls, []).append(pattern)

    changed_ts = sorted({t for t in map(_component_ts, changed) if t})
    candidates: dict[str, str | None] = {}          # class -> via selector, or None
    templates = None
    for ts in changed_ts:
        f = root / ts
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        cls = re.search(r"export\s+class\s+(\w+)", text)
        if not cls:
            continue
        if cls.group(1) in by_class:
            candidates.setdefault(cls.group(1), None)
            continue
        sel = re.search(r"selector\s*:\s*['\"]([^'\"]+)['\"]", text)
        if not sel:
            continue
        if templates is None:
            templates = [h for r in roots for h in r.rglob("*.component.html")
                         if "node_modules" not in h.parts]
        for h in templates:
            if f"<{sel.group(1)}" not in h.read_text(encoding="utf-8", errors="replace"):
                continue
            host = h.parent / (h.name[: -len(".html")] + ".ts")
            if not host.is_file():
                continue
            hc = re.search(r"export\s+class\s+(\w+)",
                           host.read_text(encoding="utf-8", errors="replace"))
            if hc and hc.group(1) in by_class:
                candidates.setdefault(hc.group(1), f"<{sel.group(1)}>")

    urls = list((catalogue or {}).values())
    out = []
    for cls, via in candidates.items():
        for pattern in by_class[cls]:
            if pattern in ("", "**") or any(route_matches(pattern, u) for u in urls):
                continue
            out.append({"component": cls, "route": pattern, **({"via": via} if via else {})})
    return out


@contextlib.contextmanager
def _dsaudit_origins(ctx: Ctx, c: dict, mb: str):
    """The two origins the audit shoots — and, where the project says how, the two stacks
    behind them.

    This is the only step that needs *both* sides of the branch running at the same time,
    and that is exactly why it was the only step that never ran. `base-new`/`base-old`
    named `:4300` and `:4301`, which is not a configuration: it is a promise about somebody
    else's machine. Nothing in the pipeline started those builds, so `_dsaudit_prereq`
    found nothing answering, the step was skipped on every single run, and the UX tab
    arrived empty carrying a note explaining that it was empty. On 18 Sep 2026 Victor read
    one of those pages — of a branch whose whole subject is a vet field moved onto the
    design system's own combo — and said the obvious thing: *"there is no audit for design
    system I see right now"*.

    So the run owns both instances, the way `_video` owns the one it films: `steps.dsaudit.app`
    is the same block (`"video"` borrows it), expanded twice — once with HEAD's slots, once
    with the merge-base's. The ports are ephemeral by design, which is what lets two builds
    of one repository be up together, so they can only be read back out of what each `up`
    printed; they can never be written down.

    Two `with`s and not one `try/finally` around the pair: the second `up` is the one most
    likely to fail — the images for the older commit are the ones not in the cache — and a
    single block would raise past the first instance's teardown, leaving 700MB and a port
    behind for the *next* run to find answering.
    """
    cfg = c.get("app")
    if not cfg:
        if not (c.get("base-new") and c.get("base-old")):
            raise LookupError(
                "dsaudit has neither `app` — the block that has this step start both builds "
                "itself, the same one `steps.video.app` is — nor `base-new`/`base-old`, the "
                "two origins of builds somebody else keeps served")
        yield c["base-new"], c["base-old"]
        return

    new_slots = _app_slots(ctx)
    old_slots = _app_slots(ctx, mb)
    if new_slots["sha"] and new_slots["sha"] == old_slots["sha"]:
        raise LookupError(
            "this branch IS its own merge-base, so there is no second build to compare it "
            "against — and two instances of one commit report 'no screen changed', which is "
            "the same sentence a clean audit prints")
    with app_instance(ctx, cfg, new_slots, clean=True) as fresh, \
            app_instance(ctx, cfg, old_slots, clean=True) as before:
        if fresh.started or before.started:
            ctx.notes.append(
                f"the design-system audit compared {fresh.base} (this branch) against "
                f"{before.base} ({old_slots['shortsha']}, the merge-base) — both started by "
                "this run, not whatever happened to be listening")
        yield fresh.base, before.base


def _dsaudit(ctx: Ctx):
    c = ctx.step_cfg("dsaudit")
    catalogue = c.get("screens") or {}
    screens = " ".join(f'--screen "{k}={v}"' for k, v in catalogue.items())
    sources = " ".join(f"--source {s}" for s in (c.get("source") or []))
    # Asked before anything is started. Booting two stacks to then discover there is
    # nothing to shoot is four minutes spent on a step that was never going to run.
    if not screens:
        raise LookupError("dsaudit needs at least one screen in steps.dsaudit.screens — "
                          "the list is the app's whole catalogue, not the screens somebody "
                          "guessed the branch touched")
    branch = sh("git rev-parse --abbrev-ref HEAD", ctx, capture=True).stdout.strip() or "HEAD"
    mb = merge_base(ctx)
    changed = sh(f"git diff --name-only {mb} HEAD -- "
                 + " ".join(f"'{s}'" for s in (c.get("source") or [])),
                 ctx, capture=True, check=False).stdout.split()
    unlisted = unlisted_screens(changed, catalogue, c.get("source") or [])
    for u in unlisted:
        ctx.notes.append(f"UNLISTED SCREEN — {u['component']} changed and renders "
                         f"{u['route']}" + (f" (through {u['via']})" if u.get("via") else "")
                         + ", which no steps.dsaudit.screens entry reaches: the audit never "
                         "looked at it. Add the screen to human-review.json and re-run "
                         "--only dsaudit; until then say so in the guide")
    flags = " ".join(
        f'--unlisted "{u["component"]}={u["route"]}' + (f'={u["via"]}' if u.get("via") else "") + '"'
        for u in unlisted)
    with _dsaudit_origins(ctx, c, mb) as (base_new, base_old):
        sh(f"{HERE}/ds-audit.py --base-new {base_new} --base-old {base_old} "
           f'--label-new "{branch}" --label-old {c.get("label-old", "main")} {screens} {sources} '
           f"{flags} --assets {ART} --asset-prefix assets --json {ART}/ds-audit.json "
           f"-o {ART}/ds-audit.html", ctx)
    # Outside the block: the stylesheet is printed by the script itself and needs no
    # application at all, so it must not hold two stacks up while it is written.
    sh(f"{HERE}/ds-audit.py --css > {ART}/ds-audit.css", ctx)


def _basestack(ctx: Ctx):
    """Start the merge-base's stack at the beginning of the run, so dsaudit finds it up.

    The audit is the run's critical path (city → dsaudit on the shared stack) and the
    merge-base build is the part of it that waits on nothing: on petclinic dsaudit spent
    most of its 131 s building the older commit's images while every other lane was idle
    or done. Started here, it builds alongside city; dsaudit then reuses it through
    `app_instance`, which leaves up what it did not start — and the stack's own TTL takes
    it down. Only when dsaudit will actually run: `main` sets `prewarm_base` from the
    step cache, because a two-minute docker build for a cached audit is pure waste.
    """
    if not getattr(ctx, "prewarm_base", False):
        raise LookupError("dsaudit is unchanged or not running — no stack to start early")
    cfg = ctx.step_cfg("dsaudit").get("app")
    if cfg == "video":
        cfg = ctx.step_cfg("video").get("app") or {}
    if not isinstance(cfg, dict) or not cfg.get("up"):
        raise LookupError("dsaudit starts no stack of its own")
    slots = _app_slots(ctx, merge_base(ctx))
    up = cfg["up"]
    for name, value in slots.items():
        up = up.replace("{" + name + "}", value).replace("{" + name.replace("sha", "SHA") + "}", value)
    r = sh(up, ctx, capture=True, check=False)
    print((r.stdout or "") + (r.stderr or ""), end="", flush=True)
    if r.returncode != 0:
        raise RuntimeError(f"the merge-base stack would not start: {up}")
    ctx.notes.append(f"started the merge-base stack ({slots.get('shortsha')}) early, "
                     "for the design-system audit")


def _dsaudit_prereq(ctx: Ctx):
    """Configured — and, for a project that serves its own two builds, both of them up.

    With `app` there is nothing to probe, and probing anyway would be worse than useless:
    the addresses do not exist until this step creates them, so the check would skip the
    step for the absence of a build it was about to start. An `up` that fails is then a
    failure of the step, reported with its own output — the same arrangement `video` has,
    whose prerequisite is `None`.

    Without `app` the old guard stands. The audit compares two running apps screen by
    screen, so a missing app is its missing binary: a precondition, checked before the
    stamp, with the URL that did not answer and what it stands for in the reason — not a
    defect of the run to be dug out of a Playwright traceback and a 400-character command
    line.
    """
    c = ctx.step_cfg("dsaudit")
    if not c:
        return "dsaudit not configured"
    if c.get("app"):
        return True
    if ctx.dry or not (c.get("base-new") and c.get("base-old")):
        return True                    # the step itself names what is missing
    down = [f"{url} ({what})" for url, what in ((c["base-new"], "this branch"),
                                                (c["base-old"], c.get("label-old", "main")))
            if not answers(url)]
    if not down:
        return True
    return (f"no app answering at {' and '.join(down)} — the audit compares two running "
            "builds. Configure `steps.dsaudit.app` to have this step start both itself "
            "(the same block steps.video.app is), or start them by hand and re-run "
            "--only dsaudit")


def _owners(ctx: Ctx):
    sh(f"{HERE}/codeowners-check.py --base {ctx.base} --state", ctx, check=False)


def _tests(ctx: Ctx):
    sh(f"{HERE}/test-changes.py --base {ctx.base} --out {ART}/test-changes.json", ctx)


def _traces(ctx: Ctx):
    """The Playwright recordings of the run, copied next to the page that shows them.

    The browser suite in `city.tests` is run from here, first (`_city_tests`), and only
    once: running it a second time to record it would double the longest step on the page
    in exchange for a second, differently-flaky opinion about the same branch. What a project has to do instead is turn tracing ON in
    that run (`--trace on`, or an env knob its config reads); `commands` is here for the
    project that genuinely has no other run to attach to.
    """
    c = ctx.step_cfg("traces")
    report = c.get("report")
    # The browser suite first, whether or not there is a report to harvest from it: its
    # per-test coverage is `testcov`'s, and through it the Code City's.
    _city_tests(ctx)
    if not report:
        raise LookupError("traces.report not configured")
    with app_instance(ctx, c.get("app"), _app_slots(ctx)) as app:
        if app.started:
            ctx.notes.append(f"the recorded suite ran against {app.base}, started by this run "
                             "from the commit under review")
        for cmd in c.get("commands") or []:
            r = sh(app.command(cmd), ctx, check=False)
            if r.returncode != 0:
                ctx.notes.append(f"the traced suite did not pass ({cmd}); the recordings below "
                                 "are of that run, which is exactly when they are worth most")
    project = f' --project-dir "{c["projectDir"]}"' if c.get("projectDir") else ""
    cucumber = f' --cucumber "{c["cucumber"]}"' if c.get("cucumber") else ""
    r = sh(f'{HERE}/playwright-traces.py --report "{report}" --out {ART} '
           f"--json {ART}/traces.json --limit {c.get('limit', 12)}{project}{cucumber}",
           ctx, check=False)
    if r.returncode == 2:
        raise LookupError(f"no Playwright HTML report at {report} — did the suite run?")
    if r.returncode == 3:
        raise LookupError("the suite ran with tracing off — nothing to step through. "
                          "Record with `--trace on` to get this tab")
    if r.returncode != 0:
        raise RuntimeError(f"playwright-traces.py exit {r.returncode}")


def _testcov(ctx: Ctx):
    """Which tests execute which changed lines — per test, measured, not paired by a model.

    `testcov.py` runs the JUnit and Karma suites itself, each with a per-test hook this
    skill ships (a JUnit Platform listener that dumps and resets JaCoCo around every test, a
    Karma reporter that diffs Istanbul's counters around every spec), and harvests what the
    browser suites left in `steps.testcov.e2e.dir` when `city.tests` / `traces.commands`
    (both run by `traces`) ran them with COVERAGE_DIR. It never runs the browser suites a second time: they are
    the longest run on the page, and they have already run, traced, for the recordings.

    Free, never a model: the Tests tab's right-hand column is drawn from what this writes,
    and the AI's pairing is reduced to a chip on the rows it named.
    """
    if not ctx.step_cfg("testcov"):
        raise LookupError("steps.testcov not configured — the Tests tab keeps the AI's "
                          "pairing and says coverage was not measured")
    r = sh(f"{HERE}/testcov.py --base {ctx.base} --out {ART}/test-coverage.json", ctx,
           check=False)
    if r.returncode == 3:
        raise LookupError("steps.testcov not configured")
    if r.returncode != 0:
        raise RuntimeError(f"testcov.py exit {r.returncode}")


# ─────────────────────────────────────────────────────── what each step reads and writes
#
# A refresh re-derives every producer's answer from inputs that, nine times out of ten,
# nobody has touched. The page is rebuilt after an edit to one test body and the diagram
# deltas are redrawn, the container view re-projected, the contract re-diffed — thirty-eight
# seconds to arrive at the bytes already on disk. Worse than the wait: `run-steps.py` stamps
# the ledger for every step it runs, so `.steps.json` moves, so the cost ledger's own cache
# (`hrbuild/tabs/cost.py`, keyed on that file among others) misses and the *build* spends
# another forty-eight seconds re-reading a conversation that has not gained a turn. One
# needless step poisons a cache two programs away.
#
# So each step declares what it reads. `_step_key` turns that into a hash; a step whose hash
# matches the one recorded next to its last successful run is not run again, and — crucially
# — is not stamped either, which is what lets the build's cache hold.
#
# The three kinds of input are separate because they are fingerprinted differently:
#
#   `paths`   git pathspecs in the repository under review. Fingerprinted from the *index*
#             (`git ls-files -s`, which is blob shas and costs 20ms for a whole repo)
#             plus the content of anything git reports dirty. Exact, not mtime-based: a
#             checkout that rewrites every mtime must not invalidate every step.
#             `("*",)` means "the whole repository", which is the honest declaration for
#             `tests` and `owners` — they read the entire change set.
#   `tools`   the producers themselves, relative to this directory. Editing
#             `endpoint-complexity.py` has to re-run `complexity` and nothing else; this is
#             the whole reason the list is per-step and not "every script in the skill".
#   `reads`   artifacts under `.human-review/` that an *earlier step* wrote. They are
#             gitignored, so no pathspec sees them, and `aftermath` is built entirely out
#             of one of them.
#
# `outputs` is the other half of correctness and not an optimisation at all. Step 0 of a
# fresh review wipes `.human-review/assets/`; a cache that only knew about inputs would
# then skip every step and leave the page asserting evidence that had just been deleted. A
# hit therefore also requires the step's own outputs to be where it left them.
STEP_INPUTS = {
    "reviewpoints": {"paths": ("*review-points.md",),
                     "tools": ("review-points.py", "review-commits.py",
                               "review_points_schema.py",
                               "../reference/review-points.schema.json"),
                     "outputs": ("review-points.json", "review-commits.json")},
    "aftermath":    {"paths": (),
                     "reads": ("review-commits.json",),
                     "tools": (),
                     "outputs": ("aftermath.json",)},
    "diagrams":     {"paths": ("*.puml", "*.drawio", "*.drawio.png", "*.drawio.svg"),
                     "tools": ("puml-diff.sh", "drawio-diff.py",
                               "../puml-diff/puml_diff.py", "../puml-diff/seq_puml_diff.py"),
                     "outputs": ("assets/diagrams",)},
    "c2":           {"paths": ("*.genseq.puml", "*.drawio", "*.drawio.png", "*.drawio.svg"),
                     # whether those were re-traced on this run: the card says so
                     "reads": ("assets/sequence.verdict.json",),
                     "tools": ("c2-from-sequence.py", "drawio-diff.py"),
                     "outputs": ("assets/c2",)},
    # The DSL, and the tests the cards quote: which C4 level a test checks is read off its
    # source (`structurizr-views.py` `tests_reading`). Something a DSL `!include`s by a
    # path that does not end in .dsl is rare enough to leave out.
    "c4":           {"paths": ("*.dsl", "*Test.java", "*Tests.java", "*Test.kt", "*Tests.kt"),
                     "tools": ("structurizr-views.py",),
                     "outputs": ("assets/c4",)},
    "complexity":   {"paths": ("*.java",),
                     "tools": ("endpoint-complexity.py", "endpoint-complexity-delta.py",
                               "complexity/ComplexityEngine.java"),
                     "outputs": ("assets/complexity-delta.html",
                                 "assets/complexity-delta.css")},
    "api":          {"paths": ("*.yaml", "*.yml", "*.json"),
                     "tools": ("openapi-compat.py", "openapi-visual-diff.py"),
                     "outputs": ("assets/openapi-verdict.html",
                                 "assets/openapi-compat.html",
                                 "assets/openapi-compat.css",
                                 "assets/openapi-visual-diff.html")},
    "specchanges":  {"paths": ("*.yaml", "*.yml"),
                     "tools": (),
                     "outputs": ("assets/openapi-changes.html",)},
    "logging":      {"paths": ("*.java", "*.ts", "*.js", "*.kt"),
                     "tools": ("logextract.py", "ast-grep-rules"),
                     "outputs": ("assets/logging.json",)},
    "owners":       {"paths": ("*",),
                     "tools": ("codeowners-check.py",),
                     "outputs": ()},
    "tests":        {"paths": ("*",),
                     "tools": ("test-changes.py",),
                     "outputs": ("assets/test-changes.json",)},
    # The whole repository, like `tests`: a test anywhere can reach a changed line. The
    # browser run's own record is read too — the step harvests it, and a new run of the
    # traced suite has to be a miss even when no source moved.
    "testcov":      {"paths": ("*",),
                     "reads": ("coverage/playwright/run.json", "coverage/cucumber/run.json"),
                     "tools": ("testcov.py", "testcov"),
                     "outputs": ("assets/test-coverage.json",)},
}

#: Where the per-step hashes live. Beside `.steps.json` and deliberately not inside it: the
#: ledger is a record of what a *run* did and is read by the cost attribution, while this is
#: a derived, throwaway index that any run may rebuild from scratch.
STEP_CACHE = Path(".human-review/.steps-cache.json")

#: Bumped when the shape of a key changes, so an upgrade of this file cannot hit a cache
#: entry computed by an older, differently-meaning hash. Cheaper than migrating and much
#: harder to get wrong than remembering to delete a file.
CACHE_VERSION = 3


def _hash_file(path: Path, h) -> None:
    """Fold one file's *content* into `h`, or the fact that it is not there.

    Content and not `(size, mtime)` because the cheap fingerprint is wrong in the one
    direction that matters: `git checkout` rewrites mtimes on files it restores byte for
    byte, and a refresh after a branch switch would re-run every producer to arrive at what
    was already on disk. Only dirty files are hashed this way — a handful — so the cost is
    a few kilobytes of reading against the thirty-eight seconds it saves.
    """
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        h.update(b"\0absent\0")


def _tree_stamp(path: Path, h) -> None:
    """Fold an output — a file or a whole directory — into `h` by name, size and mtime.

    Outputs are stamped rather than hashed, and the asymmetry with `_hash_file` is
    deliberate. An input's fingerprint has to survive a checkout; an output's has to notice
    a wipe, and `assets/diagrams/` is ninety SVGs somebody's rebuild rewrites wholesale.
    Reading all of it every run would cost more than the step being skipped.
    """
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_file():
                try:
                    st = child.stat()
                    h.update(f"{child}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
                except OSError:
                    h.update(b"\0gone\0")
    elif path.is_file():
        try:
            st = path.stat()
            h.update(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
        except OSError:
            h.update(b"\0gone\0")
    else:
        h.update(f"{path}\0absent\0".encode())


def _outputs_present(review: Path, outputs) -> bool:
    """Whether every artifact the step claims to write is still there.

    A directory counts only when it holds something: `assets/diagrams/` emptied by Step 0's
    wipe is the same absence as no directory at all, and skipping the step over it would
    leave the page naming evidence that had just been deleted.
    """
    for rel in outputs:
        p = review / rel
        if p.is_dir():
            if not any(p.iterdir()):
                return False
        elif not p.is_file():
            return False
    return True


def _git_out(args: list[str]) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def dirty_paths() -> list[str]:
    """Every path git reports as not matching the index — modified, staged, untracked.

    Untracked included, and that is not thoroughness for its own sake: `*.genseq.puml` is
    written by the `sequence` step minutes before `c2` reads it and is not committed yet, so
    a fingerprint taken from the index alone would call the repository unchanged while the
    file `c2` exists to project from had just appeared.
    """
    out = []
    for line in _git_out(["status", "--porcelain", "-z", "--untracked-files=all"]).split("\0"):
        if len(line) > 3:
            path = line[3:]
            # A rename's record is `R  new\0old`; the split already gave us each side.
            if not _is_review_dir(path):
                out.append(path)
    return out


def _is_review_dir(path: str) -> bool:
    """Whether a path is inside the review directory this program is writing into.

    Excluded from every fingerprint, and not as an optimisation. `.human-review/` holds
    each step's *output*, plus the status table and the cache file itself — all of which
    this run rewrites — so a project that does not gitignore it would see `git status`
    report a different tree after every run and no whole-repo step (`tests`, `owners`)
    would ever cache. petclinic gitignores it and the bug was invisible there; a project
    that commits its reports would have found the cache permanently disabled and nothing
    saying why.
    """
    return path == ART.parent.name or path.startswith(ART.parent.name + "/")


def _fnmatch_any(path: str, specs) -> bool:
    """Whether a path matches any of the git pathspecs, the way git itself matches them.

    `fnmatch` with `*` crossing `/`, which is what git's wildmatch does without the
    `:(glob)` magic — `git ls-files '*.puml'` finds one at any depth, and `has_genseq()`
    has always relied on exactly that. `"*"` is the whole repository."""
    return any(spec == "*" or fnmatch.fnmatch(path, spec)
               or fnmatch.fnmatch("/" + path, "*/" + spec.lstrip("*/"))
               for spec in specs)


def _step_key(name: str, ctx: Ctx, mb: str, head: str, dirty: list[str]) -> str | None:
    """The fingerprint of everything `name` reads, or None when it declares nothing.

    None is the signal for "this step is not cacheable" and is the correct answer for a step
    nobody has declared inputs for: an undeclared step must run, every time, because the
    alternative is a page quietly built from a stale artifact that nothing on it admits to.
    A new step is therefore slow until somebody describes it, which is the right way round.
    """
    spec = STEP_INPUTS.get(name)
    if spec is None:
        return None
    h = hashlib.blake2b(digest_size=16)
    h.update(f"v{CACHE_VERSION}\0{name}\0{ctx.base}\0{mb}\0{head}\0".encode())
    # The checkout's own path: fragments bake absolute `vscode://file/...` links into the
    # page, so a renamed folder (petclinic-pr -> petclinic-pr-visit-has-vet, 3 Oct 2026)
    # must re-run them, or the page keeps 56 links to a path that no longer exists.
    h.update(f"{Path.cwd().resolve()}\0".encode())
    h.update(json.dumps(ctx.step_cfg(name), sort_keys=True).encode())
    # The project's own `generated` globs steer `aftermath`'s split and nothing else reads
    # them, but they are config the step consults, so a change to them has to be a miss.
    h.update(json.dumps(ctx.cfg.get("generated"), sort_keys=True).encode())
    h.update(json.dumps(ctx.cfg.get("spec"), sort_keys=True).encode())

    paths = tuple(spec.get("paths") or ())
    if paths:
        pathspecs = [] if "*" in paths else list(paths)
        h.update(b"\0index\0")
        for row in _git_out(["ls-files", "-s", "--", *pathspecs]).splitlines():
            # `<mode> <sha> <stage>\t<path>` — the review directory drops out here for the
            # same reason it drops out of `dirty_paths`, for a project that commits it.
            if not _is_review_dir(row.split("\t", 1)[-1]):
                h.update((row + "\n").encode())
        h.update(b"\0dirty\0")
        for p in sorted(d for d in dirty if _fnmatch_any(d, paths)):
            h.update(f"{p}\0".encode())
            _hash_file(Path(p), h)

    review = STEP_CACHE.parent
    for rel in spec.get("reads") or ():
        h.update(f"\0reads\0{rel}\0".encode())
        _hash_file(review / rel, h)

    for rel in spec.get("tools") or ():
        h.update(f"\0tool\0{rel}\0".encode())
        tool = HERE / rel
        if tool.is_dir():
            _tree_stamp(tool, h)
        else:
            _hash_file(tool, h)

    h.update(b"\0outputs\0")
    for rel in spec.get("outputs") or ():
        _tree_stamp(review / rel, h)
    return h.hexdigest()


def load_step_cache() -> dict:
    try:
        held = json.loads(STEP_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(held, dict) or held.get("version") != CACHE_VERSION:
        return {}
    steps = held.get("steps")
    return steps if isinstance(steps, dict) else {}


def save_step_cache(steps: dict) -> None:
    """Write the index atomically — a half-written cache would be read as no cache at all,
    which is merely slow, but a truncated one that still parses would be read as a hit."""
    STEP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STEP_CACHE.with_name(STEP_CACHE.name + ".tmp")
    tmp.write_text(json.dumps({"version": CACHE_VERSION, "steps": steps}, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, STEP_CACHE)


# name, tabs (None = feeds no tab), label, prerequisite, runner
STEPS = [
    # First, because it is the only step whose subject is the *branch's own record* of the
    # review rather than the code: everything below measures what the change did, and this
    # reads what the agent said it decided. It also resolves the two commits the later
    # steps and the cost phases date themselves from.
    # No tab and nothing to show: it only starts the build the design-system audit will
    # compare against, early, because that build waits on nothing (see `_basestack`).
    ("basestack",   None,            "merge-base stack, started early for dsaudit",
     None,                                                                        _basestack),
    ("reviewpoints", "review",       "review-points.md and the two commits",
     None,                                                                        _reviewpoints),
    # Straight after it, and never before: it reads the review commit that step resolved.
    # Same tab, because "what changed after the agent stopped" is the first thing the
    # Review tab has to say — it governs how everything under it should be read.
    ("aftermath",   "review",        "what landed after the review commit",
     None,                                                                        _aftermath),
    ("diagrams",    "data,packages", "diagram deltas",            None,              _diagrams),
    ("sequence",    "sequence",      "sequence diagrams from traces",
     lambda c: bool(c.step_cfg("sequence").get("commands")) or "sequence.commands not configured",
     _sequence),
    # Straight after `sequence`, and never before it: it reads what that step wrote. It
    # stamps `packages` because the container view is the third diagram on the Structure
    # tab, not a tab of its own — the id is a contract with the strip, and a step naming a
    # tab the page does not contain reports its cost as "not measured".
    ("c2",          "packages",      "container view projected from the sequences",
     lambda c: has_genseq() or "no *.genseq.puml in this repository — nothing to project "
                               "a container view from",
     _c2),
    # The repository's own C4 model, drawn by Structurizr: also a Structure-tab picture,
    # and independent of every other step — it reads the DSL and nothing a step wrote.
    ("c4",          "packages",      "C4 views drawn by Structurizr",
     lambda c: has_dsl() or "no *.dsl in this repository — no Structurizr workspace to draw",
     _c4),
    ("video",       "behaviour",     "feature recording",         None,              _video),
    ("complexity",  "complexity",    "entry-point complexity",
     lambda c: bool(c.step_cfg("complexity")) or has_java()
     or "no Java main sources here — nothing this step knows how to read entry points from",
     _complexity),
    ("api",         "api",           "REST contract diff",
     lambda c: Path(c.cfg.get("spec", "openapi.yaml")).is_file()
     or f"no spec at {c.cfg.get('spec', 'openapi.yaml')}", _api),
    ("specchanges", "api",           "pb33f report",
     lambda c: have("openapi-changes") or "openapi-changes not installed", _specchanges),
    ("logging",     "logging",       "structural logging scan",
     lambda c: have("ast-grep") or "ast-grep not installed", _logging),
    ("dsaudit",     "dsaudit",       "design-system audit",      _dsaudit_prereq,   _dsaudit),
    ("owners",      "owners",        "codeowners check",          None,              _owners),
    ("tests",       "requirements",  "test change manifest",      None,              _tests),
    # Last, and on the tab the manifest above already feeds: the recordings are read after
    # the reader knows which tests moved, and they are only worth copying once the run
    # they belong to is over.
    ("traces",      "requirements",  "Playwright trace recordings",
     lambda c: bool(c.step_cfg("traces").get("report") or c.step_cfg("city").get("tests"))
     or "traces.report not configured", _traces),
    # After `traces`, and never before: the browser suites' per-test coverage is written by
    # the run `city.tests` / `traces.commands` do, and this is the step that reads it.
    ("testcov",     "requirements",  "per-test coverage of the change",
     lambda c: bool(c.step_cfg("testcov")) or "testcov not configured", _testcov),
    # After `testcov`, and never before: the city is coloured by the coverage that step
    # measured (line coverage, and what the end-to-end tests alone reach).
    ("city",        "city",          "Code City capture",
     lambda c: have("google-chrome") or have("chromium") or True, _city),
]


#: What a step reads that another step writes: it starts only after those have finished
#: (whatever their status — a failed `sequence` still leaves `c2` something to say). The
#: order of STEPS above is the serial order and stays a valid one; this is the part of it
#: that is a real dependency rather than habit.
NEEDS = {
    "aftermath": {"reviewpoints"},     # reads the review commit reviewpoints resolved
    "c2":        {"sequence"},         # projects the diagrams sequence drew
    "traces":    {"tests"},            # after the manifest; runs the browser suites itself
    "testcov":   {"traces"},           # reads the per-test coverage those suites dumped
    "city":      {"testcov"},          # colours the plate with what testcov measured
    "dsaudit":   {"basestack"},        # reuses the merge-base stack it started
}

#: What a step harvests from another step's run, and so has to be re-run with it: a subset
#: of NEEDS, not all of it. `traces` NEEDS `tests` only to start after it — pulling the
#: cucumber run into every `--only tests` re-read would turn a one-second press into two
#: minutes of Docker — while `testcov` reads the per-test coverage the browser suites
#: `traces` runs dumped, and `city` colours its plate with what `testcov` wrote: re-running
#: the tests re-colours the city (Victor, 5 Oct 2026: "when we run the tests, everything
#: that has to do with the tests should follow").
HARVESTS = {"testcov": {"traces"}, "city": {"testcov"}}


def downstream(names: set[str]) -> set[str]:
    """`names` and every step that harvests what one of them writes (`HARVESTS`).

    Eval run 12: the first run's browser suite never started (the stack would not come up),
    so `testcov` harvested the Playwright coverage another run had left in
    `.human-review/coverage/` — measured on a commit of another branch — and dropped the
    suite as stale. The suite was then re-run with `--only sequence,city`; city rewrote the
    coverage, and nothing re-read it: the page kept the stale verdict. A harvesting step is
    re-run with the step it reads — its cache key (`STEP_INPUTS`' `reads`) makes that free
    when the output did not move."""
    out = set(names)
    # To a fixed point: a harvester can be harvested in turn (traces -> testcov -> city).
    while True:
        grown = out | {step for step, sources in HARVESTS.items() if sources & out}
        if grown == out:
            return out
        out = grown


#: What a step holds that no other step may hold at the same time. Measured on petclinic:
#: `traces`' browser suites (Playwright and cucumber), `video` and `dsaudit` all run against
#: the one Docker stack of
#: the commit under review (`petclinic-<sha>`), and the step that started it tears it down
#: when it ends — under another step still using it. `sequence` and `testcov` both run
#: `mvn test` in the same module, and two Maven builds in one `target/` corrupt each
#: other. Everything else reads git and files, and runs alongside anything.
#: The lane is also what keeps a capture honest: `video` and `dsaudit` (`CAPTURES`) reset
#: the database first, and only because they hold `stack` can no writer — the traced
#: suites — land between that reset and their last screenshot. `city` held it too while it
#: ran the Playwright suite; since that moved to `traces` it reads files and runs beside
#: anything.
USES = {
    "traces":   {"stack"},             # its browser suites write to that stack's database
    "video":    {"stack"},
    "dsaudit":  {"stack"},
    "sequence": {"maven", "devports"},
    "testcov":  {"maven"},
}


def schedule(names: list[str], jobs: int, run,
             estimate: dict[str, float] | None = None) -> dict[str, dict]:
    """Run `run(name)` for every name, as many at once as `jobs`, honouring NEEDS and USES.

    Serial before: the producers took 8.3 min on petclinic, of which design-system audit
    3m14, Code City 1m54, per-test coverage 1m12, the traces 49 s and the sequence suite
    38 s — and only some of those wait for one another. A step is started as soon as what
    it NEEDS has finished and nothing it USES is held, in STEPS order, so the result is
    deterministic for a given set of durations and `--jobs 1` is exactly the old run.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    # Longest chain first. Each step is ranked by its own expected duration plus the
    # longest chain of steps that wait for it, so when two steps want the same lane the one
    # with more work behind it goes first. Taken in plain STEPS order, dsaudit (46 s, nothing
    # after it) took the shared stack ahead of traces, which testcov (56 s) waits for: 164 s
    # wall clock where the chains allow ~115. Expectations are the durations the step cache
    # recorded last run; an unknown step counts as 5 s. With one job none of this applies,
    # so `--jobs 1` stays the serial run, step for step.
    est = estimate or {}
    after = {n: [d for d, needs in NEEDS.items() if n in needs and d in names] for n in names}
    rank: dict[str, float] = {}

    def chain(n: str) -> float:
        if n not in rank:
            rank[n] = (est.get(n) or 5.0) + max((chain(d) for d in after[n]), default=0.0)
        return rank[n]

    pending = list(names) if jobs <= 1 else sorted(
        names, key=lambda n: (-chain(n), names.index(n)))
    done: dict[str, dict] = {}
    held: set[str] = set()
    running = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        while pending or running:
            for name in list(pending):
                if len(running) >= max(1, jobs):
                    break
                needs = NEEDS.get(name, set()) & set(names)
                uses = USES.get(name, set())
                if needs - done.keys() or uses & held:
                    continue
                pending.remove(name)
                held |= uses
                running[pool.submit(run, name)] = name
            if not running:            # nothing runnable: a NEEDS cycle, which is a bug here
                raise RuntimeError(f"cannot schedule {pending}: NEEDS has a cycle")
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                name = running.pop(fut)
                held -= USES.get(name, set())
                done[name] = fut.result()
    return done


def _prereq(spec, ctx: Ctx) -> str | None:
    """None when the step may run, else the reason it may not.

    Called *before* the ledger stamp, which is the whole point: a step gated on an optional
    binary must never leave a record naming a tab the page will not contain.
    """
    if spec is None:
        return None
    got = spec(ctx)
    return None if got is True else (got if isinstance(got, str) else "prerequisite not met")


def run_step(name, tabs, label, prereq, fn, ctx: Ctx,
             key: str | None = None, cached: dict | None = None) -> dict:
    """One step, timed, and skipped when nothing it reads has moved.

    `seconds` is on every row, including the skipped ones.

    A skipped row's time is not noise: half the prerequisites shell out to git (`has_java`,
    `has_genseq`) or open a socket (`answers`), and a prerequisite that takes two seconds to
    say "no" costs exactly as much as one that takes two seconds to say "yes". Without the
    number on the row, the only place that cost could be seen was the wall clock of the
    whole run, which is where it hid for a year.
    """
    t0 = time.monotonic()
    reason = _prereq(prereq, ctx)
    if reason:
        print(f"  - {name}: skipped ({reason})")
        return {"step": name, "tabs": tabs, "status": SKIPPED, "reason": reason,
                "seconds": round(time.monotonic() - t0, 2)}

    # A cached step reports `ran`, not a status of its own, and the reason is not
    # tidiness: `ran` is the truthful answer to the only question anything downstream asks
    # of this table — "is this tab's evidence on disk and current?" — and it is, byte for
    # byte, because nothing it is derived from has moved. A fourth status would have every
    # reader of `.steps-status.json` treat a fresh artifact as a missing one. The `cached`
    # flag is beside it for the humans and for the status line the page prints.
    if key and cached and cached.get("key") == key and _outputs_present(STEP_CACHE.parent,
                                                                       STEP_INPUTS[name]["outputs"]):
        saved = cached.get("seconds") or 0
        print(f"  = {name}: unchanged, {saved:.1f} s saved")
        return {"step": name, "tabs": tabs, "status": RAN, "reason": None,
                "cached": True, "saved": round(saved, 2),
                "seconds": round(time.monotonic() - t0, 2), "notes": []}

    handle = Path(f".human-review/.step-{name}")
    idx = None
    if tabs and not ctx.dry and not ctx.no_ledger:
        idx = subprocess.run([sys.executable, str(LEDGER), "start", tabs, "--label", label],
                             text=True, capture_output=True).stdout.strip()
        handle.parent.mkdir(parents=True, exist_ok=True)
        handle.write_text(idx, encoding="utf-8")
    print(f"  * {name} -> {tabs or '(no tab)'}")
    # Its own notes list: steps run on several threads, and slicing one shared list by
    # "what was appended since I started" hands a step its neighbours' notes.
    shared, ctx = ctx, copy.copy(ctx)
    ctx.notes = []
    before = 0
    try:
        fn(ctx)
        status, reason = RAN, None
    except LookupError as e:           # a prerequisite only the step itself could see
        status, reason = SKIPPED, str(e)
        print(f"    skipped: {e}")
    except Exception as e:             # noqa: BLE001 - one failing step must not end the run
        status, reason = FAILED, str(e)
        print(f"    FAILED: {e}", file=sys.stderr)
    finally:
        # Always closed, including on the failure path: an open record makes the page report
        # a step that died when in fact nothing said to close it.
        if idx:
            subprocess.run([sys.executable, str(LEDGER), "end", idx], check=False)
        # And back onto the run's own list, in one append, for whoever reads it there.
        with _NOTES_LOCK:
            shared.notes.extend(ctx.notes)
    return {"step": name, "tabs": tabs, "status": status, "reason": reason,
            "seconds": round(time.monotonic() - t0, 2),
            "notes": ctx.notes[before:]}


def load_config(path: Path) -> dict:
    if not path.is_file():
        print(f"[run-steps] no {path} — every project-specific step will be skipped and "
              f"named. Copy {HERE.parent}/human-review.example.json to start.",
              file=sys.stderr)
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="human-review.json")
    ap.add_argument("--base", help="base ref (default: config's, else origin/main)")
    ap.add_argument("--only", help="comma-separated step names")
    ap.add_argument("--skip", help="comma-separated step names")
    ap.add_argument("--list", action="store_true", help="list the steps and stop")
    ap.add_argument("--dry-run", action="store_true", help="echo the commands, run nothing")
    ap.add_argument("--json", action="store_true", help="emit the status table as JSON")
    ap.add_argument("--timing", action="store_true",
                    help="print what each step cost, slowest first, after the status table")
    ap.add_argument("--jobs", type=int, default=6,
                    help="steps run at once, within NEEDS and USES (default 6; 1 = the "
                         "old serial run, output straight to the terminal)")
    ap.add_argument("--force", action="store_true",
                    help="re-run every step even if nothing it reads has changed")
    ap.add_argument("--no-ledger", action="store_true",
                    help="do not stamp .steps.json — for a refresh, whose step windows "
                         "attribute no conversation turn and whose stamps cost the build "
                         "its cost-ledger cache (see Ctx)")
    args = ap.parse_args(argv)

    if args.list:
        for name, tabs, label, prereq, _fn in STEPS:
            print(f"  {name:<12} {str(tabs or '-'):<15} {label}")
        return 0

    cfg = load_config(Path(args.config))
    ctx = Ctx(run_base(args.base or cfg.get("base") or "origin/main"), cfg, args.dry_run,
              args.no_ledger)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    skip = {s.strip() for s in args.skip.split(",")} if args.skip else set()
    if only:
        pulled = downstream(only) - only - skip
        if pulled:
            only |= pulled
            print(f"[run-steps] also running {', '.join(sorted(pulled))}: it reads what "
                  "the chosen steps' suites write", file=sys.stderr)

    ART.mkdir(parents=True, exist_ok=True)

    # The three git questions every key is built on, asked once for the whole run rather
    # than once per step: they are the same answer ten times over, and `git status` on a
    # repository with a node_modules in it is not free.
    # `--force` suppresses the *lookup* and nothing else. The fingerprints are still
    # computed and still stored, because a forced run is the most reliable moment there is
    # to record what the answer was derived from — and a `--force` that wrote keys taken
    # against an empty git context would leave every later refresh a guaranteed miss, which
    # is how a cache turns into a permanent tax.
    incremental = not args.dry_run
    cache = load_step_cache() if (incremental and not args.force) else {}
    head = _git_out(["rev-parse", "HEAD"]).strip() if incremental else ""
    mb = merge_base(ctx) if incremental else ""
    dirty = dirty_paths() if incremental else []

    chosen = [st for st in STEPS if not ((only and st[0] not in only) or st[0] in skip)]
    spec = {st[0]: st for st in chosen}
    jobs = 1 if args.dry_run else args.jobs
    logs = Path(".human-review/logs")
    if jobs > 1:
        logs.mkdir(parents=True, exist_ok=True)
        sys.stdout = _StepStdout(sys.stdout)

    t_wall = time.monotonic()

    def one(name: str) -> dict:
        _n, tabs, label, prereq, fn = spec[name]
        key = _step_key(name, ctx, mb, head, dirty) if incremental else None
        if jobs == 1:
            return run_step(name, tabs, label, prereq, fn, ctx, key, cache.get(name))
        # On stderr: stdout is the status table, and with --json it must parse.
        print(f"  > {name}: started at +{time.monotonic() - t_wall:.0f} s "
              f"(log: {logs / (name + '.log')})", file=sys.stderr, flush=True)
        with open(logs / f"{name}.log", "w", encoding="utf-8") as log:
            _LOCAL.log = log
            try:
                r = run_step(name, tabs, label, prereq, fn, ctx, key, cache.get(name))
            finally:
                _LOCAL.log = None
        print(f"  < {name}: {r['status']} in {r.get('seconds', 0):.1f} s, "
              f"at +{time.monotonic() - t_wall:.0f} s"
              + (f" — {r['reason']}" if r.get("reason") else ""), file=sys.stderr, flush=True)
        return r

    # The audit's merge-base stack is worth starting early only for an audit that runs.
    if "dsaudit" in spec and "basestack" in spec:
        dkey = _step_key("dsaudit", ctx, mb, head, dirty) if incremental else None
        dhit = (dkey and cache.get("dsaudit", {}).get("key") == dkey
                and _outputs_present(STEP_CACHE.parent, STEP_INPUTS["dsaudit"]["outputs"]))
        ctx.prewarm_base = not dhit and not args.dry_run
    t_wall = time.monotonic()
    seen = load_step_cache() if incremental else {}
    by_name = schedule([st[0] for st in chosen], jobs, one,
                       {n: (seen.get(n) or {}).get("seconds") or 0 for n in spec})
    if jobs > 1:
        sys.stdout = sys.stdout.real
        print(f"\n[run-steps] {len(chosen)} step(s) in {time.monotonic() - t_wall:.0f} s "
              f"wall clock, {jobs} at a time; each step's output is in {logs}/",
              file=sys.stderr)

    results = []
    for name in (st[0] for st in chosen):
        r = by_name[name]
        results.append(r)
        # Only a step that actually ran to completion records a key, and the key is
        # recomputed *after* it ran rather than reused from before: the fingerprint covers
        # the step's own outputs, which is precisely what the run just changed. Storing the
        # pre-run key would make the very next refresh a miss, for ever.
        if r["status"] == RAN and not r.get("cached") and not args.dry_run:
            fresh = _step_key(name, ctx, mb, head, dirty)
            if fresh:
                cache[name] = {"key": fresh, "seconds": r.get("seconds") or 0,
                               "at": _stamp()}
        elif r["status"] != RAN:
            # A failed or skipped step must not leave last run's key behind it: the next
            # refresh would find a hit, skip the step, and report as current an artifact
            # this run had just proved it could not produce.
            cache.pop(name, None)

    if not args.dry_run:
        save_step_cache(cache)

    status_path = Path(".human-review/.steps-status.json")
    if not args.dry_run:
        status_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("\n  step         status   why / what to say")
        for r in results:
            print(f"  {r['step']:<12} {r['status']:<8} {r.get('reason') or ''}")
            for n in r.get("notes") or []:
                print(f"  {'':<12} note     {n}")
        dropped = [r["tabs"] for r in results if r["status"] != RAN and r["tabs"]]
        if dropped:
            print(f"\n  tabs with no content, to be named under the strip: "
                  f"{', '.join(sorted(set(dropped)))}")
    # One line, last, and phrased for the status band on the served page rather than for
    # this terminal: pressing **Rerun** and watching nothing happen for two seconds is
    # indistinguishable from pressing a button that does not work. Saying which steps were
    # skipped, and what that saved, is what makes a fast rerun legible as a fast rerun.
    cached_rows = [r for r in results if r.get("cached")]
    if cached_rows and not args.json:
        saved = sum(r.get("saved") or 0 for r in cached_rows)
        ran_n = len(results) - len(cached_rows)
        print(f"\n[run-steps] {ran_n} step(s) re-run, {len(cached_rows)} unchanged and "
              f"skipped — about {saved:.0f} s saved. `--force` re-runs everything.")
    if args.timing:
        print_timing(results)
    return 1 if any(r["status"] == FAILED for r in results) else 0


def print_timing(results: list[dict], out=None) -> None:
    """The steps by what they cost, slowest first, with the share of the run each took.

    Sorted by time rather than by the order they ran in, because the question this answers
    is never "what happened" — the status table above already says that — but "what do I
    fix first". The total is the sum of the rows, not the wall clock of the process: the
    difference between the two is the runner's own overhead, and if it ever grows into
    something worth seeing, it should be a row and not a rounding error.
    """
    out = out or sys.stdout
    rows = sorted(results, key=lambda r: -(r.get("seconds") or 0))
    total = sum(r.get("seconds") or 0 for r in rows)
    print("\n  step         seconds   share  status", file=out)
    for r in rows:
        secs = r.get("seconds") or 0
        share = (secs / total * 100) if total else 0
        print(f"  {r['step']:<12} {secs:7.2f}  {share:5.1f}%  {r['status']}", file=out)
    print(f"  {'TOTAL':<12} {total:7.2f}  100.0%", file=out)


if __name__ == "__main__":
    raise SystemExit(main())
