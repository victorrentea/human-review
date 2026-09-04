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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ART = Path(".human-review/assets")
LEDGER = HERE / "steps-ledger.py"

RAN, SKIPPED, FAILED = "ran", "skipped", "failed"


class Ctx:
    def __init__(self, base: str, cfg: dict, dry: bool):
        self.base, self.cfg, self.dry = base, cfg, dry
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


def merge_base(ctx: Ctx) -> str:
    """Where this branch forked, not where the base ref points now.

    Every producer here reads the "before" side out of a commit object, so a base that has
    moved on since the branch started would report commits that landed on the base as this
    branch's work.
    """
    got = sh(f"git merge-base {ctx.base} HEAD", ctx, capture=True, check=False).stdout.strip()
    return got or ctx.base


# --------------------------------------------------------------------------- steps

def _diagrams(ctx: Ctx):
    sh(f"{HERE}/puml-diff.sh {ctx.base} {ART}/diagrams", ctx)
    d = ctx.step_cfg("diagrams").get("drawio")
    if not d:
        ctx.notes.append("no drawio diagram configured")
        return
    sh(f"{HERE}/drawio-diff.py --base {ctx.base} --diagram {d['diagram']} "
       f"--concepts {d['concepts']} --out-dir {ART} --name {d.get('name', 'conceptual')}", ctx)


def _sequence(ctx: Ctx):
    for cmd in ctx.step_cfg("sequence").get("commands") or []:
        sh(cmd, ctx)
    # A suite that could not start leaves a generated diagram deleted, and the delta then
    # reports, in the branch's voice, that this branch removed it.
    r = sh("git status --porcelain -- '*.genseq.puml'", ctx, capture=True)
    deleted = [l.split(maxsplit=1)[-1] for l in (r.stdout or "").splitlines()
               if l.strip().startswith("D")]
    if deleted:
        ctx.notes.append(f"restored {len(deleted)} diagram(s) a failed suite deleted; "
                         "say in the guide that the suite could not run")
        sh("git checkout -- " + " ".join(deleted), ctx, check=False)
    sh(f"{HERE}/puml-diff.sh {ctx.base} {ART}/diagrams", ctx)


def _city(ctx: Ctx):
    regen = ctx.step_cfg("city").get("regenerate")
    if regen:
        sh(regen, ctx, check=False)
    r = sh(f"{HERE}/capture-codecity.sh {ART}/codecity.png highlight", ctx, capture=True)
    lit = (r.stdout or "").strip().splitlines()
    if lit:
        ctx.notes.append(f"codecity lit: {lit[-1]} (put this measured number under the "
                         "image; never type one)")


def _video(ctx: Ctx):
    out = ctx.step_cfg("video").get("out", f"{ART}/feature.webm")
    r = sh(f"{HERE}/record-feature-video.sh {out}", ctx, check=False)
    if r.returncode == 3:
        ctx.notes.append("EXIT 3 — filmed, and the feature did NOT hold. Embed it and lead "
                         "the review with what it shows; this is the most valuable film "
                         "this pipeline can make")
    elif r.returncode == 2:
        raise LookupError("no feature script, or the stack is down")
    elif r.returncode != 0:
        raise RuntimeError(f"record-feature-video.sh exit {r.returncode}")


def _complexity(ctx: Ctx):
    c = ctx.step_cfg("complexity")
    if c.get("extract"):
        sh(c["extract"], ctx)
    before, after = c.get("before"), c.get("after")
    if not (before and after):
        raise LookupError("complexity.before / complexity.after not configured")
    sh(f"{HERE}/endpoint-complexity-delta.py {before} {after} --base {ctx.base} "
       f"--out {ART}/complexity-delta.html", ctx)
    sh(f"{HERE}/endpoint-complexity-delta.py --css > {ART}/complexity-delta.css", ctx)


def _api(ctx: Ctx):
    spec = ctx.cfg.get("spec", "openapi.yaml")
    for cmd in (
        f"{HERE}/openapi-diff.py   --base {ctx.base} --spec {spec} --out {ART}/openapi-diff.html",
        f"{HERE}/openapi-diff.py   --css  >  {ART}/openapi-diff.css",
        f"{HERE}/openapi-compat.py --base {ctx.base} --spec {spec} --out {ART}/openapi-compat.html",
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


def _dsaudit(ctx: Ctx):
    c = ctx.step_cfg("dsaudit")
    screens = " ".join(f'--screen "{k}={v}"' for k, v in (c.get("screens") or {}).items())
    sources = " ".join(f"--source {s}" for s in (c.get("source") or []))
    if not (c.get("base-new") and c.get("base-old") and screens):
        raise LookupError("dsaudit needs base-new, base-old and at least one screen")
    branch = sh("git rev-parse --abbrev-ref HEAD", ctx, capture=True).stdout.strip() or "HEAD"
    sh(f"{HERE}/ds-audit.py --base-new {c['base-new']} --base-old {c['base-old']} "
       f'--label-new "{branch}" --label-old {c.get("label-old", "main")} {screens} {sources} '
       f"--assets {ART} --asset-prefix assets --json {ART}/ds-audit.json "
       f"-o {ART}/ds-audit.html", ctx)
    sh(f"{HERE}/ds-audit.py --css > {ART}/ds-audit.css", ctx)


def _owners(ctx: Ctx):
    sh(f"{HERE}/codeowners-check.py --base {ctx.base} --state", ctx, check=False)


def _tests(ctx: Ctx):
    sh(f"{HERE}/test-changes.py --base {ctx.base} --out {ART}/test-changes.json", ctx)


# name, tabs (None = feeds no tab), label, prerequisite, runner
STEPS = [
    ("diagrams",    "data,packages", "diagram deltas",            None,              _diagrams),
    ("sequence",    "sequence",      "sequence diagrams from traces",
     lambda c: bool(c.step_cfg("sequence").get("commands")) or "sequence.commands not configured",
     _sequence),
    ("city",        "city",          "Code City capture",
     lambda c: have("google-chrome") or have("chromium") or True, _city),
    ("video",       "behaviour",     "feature recording",         None,              _video),
    ("complexity",  "complexity",    "entry-point complexity",
     lambda c: bool(c.step_cfg("complexity")) or "complexity not configured", _complexity),
    ("api",         "api",           "REST contract diff",
     lambda c: Path(c.cfg.get("spec", "openapi.yaml")).is_file()
     or f"no spec at {c.cfg.get('spec', 'openapi.yaml')}", _api),
    ("specchanges", "api",           "pb33f report",
     lambda c: have("openapi-changes") or "openapi-changes not installed", _specchanges),
    ("logging",     "logging",       "structural logging scan",
     lambda c: have("ast-grep") or "ast-grep not installed", _logging),
    ("dsaudit",     "dsaudit",       "design-system audit",
     lambda c: bool(c.step_cfg("dsaudit")) or "dsaudit not configured", _dsaudit),
    ("owners",      "owners",        "codeowners check",          None,              _owners),
    ("tests",       None,            "test change manifest",      None,              _tests),
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


def run_step(name, tabs, label, prereq, fn, ctx: Ctx) -> dict:
    reason = _prereq(prereq, ctx)
    if reason:
        print(f"  - {name}: skipped ({reason})")
        return {"step": name, "tabs": tabs, "status": SKIPPED, "reason": reason}

    handle = Path(f".human-review/.step-{name}")
    idx = None
    if tabs and not ctx.dry:
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
    args = ap.parse_args(argv)

    if args.list:
        for name, tabs, label, prereq, _fn in STEPS:
            print(f"  {name:<12} {str(tabs or '-'):<15} {label}")
        return 0

    cfg = load_config(Path(args.config))
    ctx = Ctx(args.base or cfg.get("base") or "origin/main", cfg, args.dry_run)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    skip = {s.strip() for s in args.skip.split(",")} if args.skip else set()

    ART.mkdir(parents=True, exist_ok=True)
    results = []
    for name, tabs, label, prereq, fn in STEPS:
        if (only and name not in only) or name in skip:
            continue
        results.append(run_step(name, tabs, label, prereq, fn, ctx))

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
    return 1 if any(r["status"] == FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
