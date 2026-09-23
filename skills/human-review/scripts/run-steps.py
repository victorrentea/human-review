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
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import re
import shlex
import subprocess
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


def sh(cmd, ctx: Ctx, check=True, capture=False) -> subprocess.CompletedProcess:
    """One command, echoed. A step's commands are shell strings because half of them are
    the project's own (`cd x && mvn …`), and quoting those into a list buys nothing."""
    print(f"    $ {cmd}", flush=True)
    if ctx.dry:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    r = subprocess.run(cmd, shell=True, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
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
      ran `/implement-ticket` on is a normal branch, and the build renders the absence as
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
GENERATED_DEFAULT = (
    "**/generated/**", "docs/generated/**", "openapi.yaml",
    "**/*.genseq.*", "**/api-types.ts", "**/*.drawio*",
)


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
        })

    def tally(picked):
        return {"files": len(picked),
                "added": sum(f["added"] for f in picked),
                "deleted": sum(f["deleted"] for f in picked)}

    out = {
        "review": review, "review_short": review[:8], "head": head,
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
    failed = []
    with app_instance(ctx, cfg.get("app"), _app_slots(ctx)) as app:
        if app.started:
            ctx.notes.append(f"the traced suites ran against {app.base}, started by this run "
                             "from the commit under review — not whatever was already listening")
        for cmd in commands:
            r = sh(f"{app.env}{cmd}", ctx, check=False)
            if r.returncode != 0:
                failed.append(f"{cmd} (exit {r.returncode})")

    # ALWAYS, and this is the whole reason the loop above does not raise. These commands
    # sweep `generated/` before they regenerate it, so a suite that dies in the middle
    # leaves diagrams DELETED — including the other suites', which it never meant to touch
    # and cannot put back. Raising on the first failure skipped both the restore below and
    # every later command, which is how one red suite took the backend's diagram with it.
    # Since ONE deleted file gets restored the branch would otherwise be reported, in its
    # own voice, as having removed a picture.
    r = sh("git status --porcelain -- '*.genseq.puml' '*.genseq.json'", ctx, capture=True)
    deleted = [l.split(maxsplit=1)[-1] for l in (r.stdout or "").splitlines()
               if l.strip().startswith("D")]
    if deleted:
        ctx.notes.append(f"restored {len(deleted)} diagram file(s) a failed suite deleted "
                         "without regenerating; say in the guide that the suite could not run")
        sh("git checkout -- " + " ".join(deleted), ctx, check=False)
    sh(f"{HERE}/puml-diff.sh {ctx.base} {ART}/diagrams", ctx)

    if failed and has_genseq():
        # A red suite is a finding for the review to carry, not a reason to lose the tab —
        # the same call `_city` makes. The pictures a passing part of the run did draw are
        # still pictures of this branch.
        ctx.notes.append("the traced suite was RED (" + "; ".join(failed)
                         + "); the diagrams below are of that run, and the guide has to say so")
    elif failed:
        raise LookupError(
            "the traced suite could not run and drew nothing: " + "; ".join(failed)
            + ". It needs the whole stack listening — a trace collector, the database, the "
            "backend started AFTER the collector so its agent attaches, and the front end. "
            "Configure `steps.sequence.app` to have this step start them itself, or start "
            "them by hand and re-run this step")


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


def _city(ctx: Ctx):
    # The city's CRAP and coverage colours are the only thing on this page that cannot be
    # read off the sources and the git log: they need a coverage report, which needs the
    # project's tests to have actually run. `city.tests` is where a project says how, and
    # a project that does not say simply gets a city without those two metrics — the
    # generator drops them rather than colouring every building "not measured".
    #
    # Failures do not stop it. A red suite is a finding for the review to carry, not a
    # reason to lose the whole city tab, and the coverage of a run with one broken test
    # is still the coverage of that run. The test command is expected to say so itself
    # (Maven: -Dmaven.test.failure.ignore=true), because a build that aborts on the first
    # failure never reaches its report goal and leaves LAST week's report on disk for the
    # city to be coloured with, which is the one outcome worse than having no colours.
    # A list, because measuring two suites is several commands that must run in order:
    # the unit run, the acceptance run against a separately started application, and the
    # merge that turns their two .exec files into the reports the city reads. A single
    # string still works for a project with only one of them.
    tests = ctx.step_cfg("city").get("tests")
    if isinstance(tests, str):
        tests = [tests]
    for cmd in tests or []:
        r = sh(cmd, ctx, check=False)
        if r.returncode != 0:
            ctx.notes.append(f"a suite behind the city's coverage colours did not pass "
                             f"({cmd}); say so next to the CRAP reading")
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
    city = ctx.step_cfg("city")
    regen, out = city.get("regenerate"), city.get("out")
    if regen:
        sh(regen, ctx, check=False)
    elif out:
        baseline = f' --baseline "{city["baseline"]}"' if city.get("baseline") else ""
        acceptance = f' --acceptance "{city["acceptance"]}"' if city.get("acceptance") else ""
        sh(f'{HERE}/regenerate-codecity.sh --out "{out}" '
           f'--title "{city.get("title", "Code City")}"{baseline}{acceptance}', ctx, check=False)
    r = sh(f"{HERE}/capture-codecity.sh {ART}/codecity.png highlight", ctx, capture=True)
    lit = (r.stdout or "").strip().splitlines()
    if lit:
        ctx.notes.append(f"codecity lit: {lit[-1]} (put this measured number under the "
                         "image; never type one)")


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


@contextlib.contextmanager
def app_instance(ctx: Ctx, cfg: dict, sha: dict):
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
        if cfg.get("up"):
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
        if inst.base:
            names = cfg.get("env") or APP_ENV_DEFAULT
            inst.env = "".join(
                f"{k}={shlex.quote(v.replace('{url}', inst.base))} " for k, v in names.items())
            inst.env += (f"HUMAN_REVIEW_APP_COMMIT={shlex.quote(sha.get('sha', ''))} "
                         "HUMAN_REVIEW_APP_STARTED=1 ")
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

    with app_instance(ctx, cfg.get("app"), _app_slots(ctx) if cfg.get("app") else {}) as app:
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
    missed = []
    for label in ("FAILED to reach: ", "not filmable: "):
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
        # `--report` is the openable twin of each fragment, written in the same run so the
        # two cannot drift. The verdict band links to them by name, and only links to the
        # ones it can see on disk — so these have to be written before the --panel call.
        f"{HERE}/openapi-diff.py   --base {ctx.base} --spec {spec} --out {ART}/openapi-diff.html"
        f" --report {ART}/openapi-diff-report.html",
        f"{HERE}/openapi-diff.py   --css  >  {ART}/openapi-diff.css",
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
    with app_instance(ctx, cfg, new_slots) as fresh, \
            app_instance(ctx, cfg, old_slots) as before:
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

    No suite is run from here by default. The browser suite has already run — `city.tests`
    runs it for the coverage colours — and running it a second time to record it would
    double the longest step on the page in exchange for a second, differently-flaky
    opinion about the same branch. What a project has to do instead is turn tracing ON in
    that run (`--trace on`, or an env knob its config reads); `commands` is here for the
    project that genuinely has no other run to attach to.
    """
    c = ctx.step_cfg("traces")
    report = c.get("report")
    if not report:
        raise LookupError("traces.report not configured")
    with app_instance(ctx, c.get("app"), _app_slots(ctx)) as app:
        if app.started:
            ctx.notes.append(f"the recorded suite ran against {app.base}, started by this run "
                             "from the commit under review")
        for cmd in c.get("commands") or []:
            r = sh(f"{app.env}{cmd}", ctx, check=False)
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
                     "tools": ("review-points.py", "review-commits.py"),
                     "outputs": ("review-points.json", "review-commits.json")},
    "aftermath":    {"paths": (),
                     "reads": ("review-commits.json",),
                     "tools": (),
                     "outputs": ("aftermath.json",)},
    "diagrams":     {"paths": ("*.puml", "*.drawio", "*.drawio.png", "*.drawio.svg"),
                     "tools": ("puml-diff.sh", "drawio-diff.py",
                               "../puml-diff/puml_diff.py", "../puml-diff/seq_puml_diff.py"),
                     "outputs": ("assets/diagrams",)},
    "c2":           {"paths": ("*.genseq.puml",),
                     "tools": ("c2-from-sequence.py",),
                     "outputs": ("assets/c2",)},
    "complexity":   {"paths": ("*.java",),
                     "tools": ("endpoint-complexity.py", "endpoint-complexity-delta.py"),
                     "outputs": ("assets/complexity-delta.html",
                                 "assets/complexity-delta.css")},
    "api":          {"paths": ("*.yaml", "*.yml", "*.json"),
                     "tools": ("openapi-diff.py", "openapi-compat.py",
                               "openapi-visual-diff.py"),
                     "outputs": ("assets/openapi-verdict.html", "assets/openapi-diff.html",
                                 "assets/openapi-compat.html", "assets/openapi-diff.css",
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
    ("city",        "city",          "Code City capture",
     lambda c: have("google-chrome") or have("chromium") or True, _city),
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
     lambda c: bool(c.step_cfg("traces").get("report")) or "traces.report not configured",
     _traces),
]


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
    before = len(ctx.notes)
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
    ctx = Ctx(args.base or cfg.get("base") or "origin/main", cfg, args.dry_run,
              args.no_ledger)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    skip = {s.strip() for s in args.skip.split(",")} if args.skip else set()

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

    results = []
    for name, tabs, label, prereq, fn in STEPS:
        if (only and name not in only) or name in skip:
            continue
        key = _step_key(name, ctx, mb, head, dirty) if incremental else None
        r = run_step(name, tabs, label, prereq, fn, ctx, key, cache.get(name))
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
