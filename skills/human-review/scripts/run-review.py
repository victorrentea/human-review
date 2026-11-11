#!/usr/bin/env python3
"""The whole of `/human-review` as one program: control flipped, the model called three times.

Measured on the two demos (21 Oct 2026), a clean `/human-review` run cost $2.01 and $3.28,
and nearly none of it was the AI work inside the tabs. It was the Opus agent driving the
run: $0.38 to start, a 25k-token schema read in chunks, and a dozen turns whose whole
content was "Waiting." while the producers ran — each re-reading 100k tokens of context.
When a harness step broke, the same agent debugged it live, on the page's bill.

Here a program drives, and a model is asked only where a judgement is the output:

  1. `review-points.py --check`   the branch's own review record (exit 4 stops the run)
  2. `preflight.py`               push, wait for CI, wipe — a red gate stops the run
  3. `write-content.py --skeleton-only`   layout and facts, free
  4. `rerun-film.py`              🤖 the film's script, only when the branch has none
  5. `run-steps.py`               every producer; a failed one is reported, not fatal
  6. `rerun-model.py`             🤖 the requirements↔tests pairing
  7. `write-content.py`           🤖 the verdict, one lean call
  8. `steps-ledger.py end/check`, `report-cost.py`, `refresh-report.py`

Every model call is a lean `claude -p` on Sonnet: no MCP servers, no user settings, no
CLAUDE.md, no saved session. A step that fails stops the run with its exit code and the
command to re-run, and nothing tries to fix it: fixing the harness is a separate session,
billed as harness work, and never on the page.

    run-review.py                     # base from human-review.json, else origin/develop|main|master
    run-review.py --base origin/main
    run-review.py --no-serve          # build, do not serve or open the page
    run-review.py --dry-run           # print the plan, run nothing

Launched from a terminal there is no Claude session, so the page's "this guide" row is the
three model calls and nothing else. Launched from inside a Claude Code session, preflight
pins that session as it always has, and its turns are billed too — they were spent.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVIEW = Path(".human-review")
FEATURE_SCRIPTS = (REVIEW / "feature-script.js", Path("human-review-feature.js"))


class Stop(Exception):
    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code, self.why = code, why


def script(name: str) -> str:
    return str(HERE / name)


def say(msg: str) -> None:
    print(f"[run-review] {msg}", flush=True)


def run(argv: list[str], *, ok=(0,), tee: Path | None = None, capture=False,
        dry: bool = False) -> subprocess.CompletedProcess:
    """One step, its output streamed as it comes; a code outside `ok` stops the run."""
    say("$ " + " ".join(shlex.quote(a) if a else '""' for a in argv))
    if dry:
        return subprocess.CompletedProcess(argv, 0, "", "")
    if capture:
        proc = subprocess.run(argv, text=True, capture_output=True)
        sys.stderr.write(proc.stderr)
    else:
        out = tee.open("w", encoding="utf-8") if tee else None
        p = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = []
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
            if out:
                out.write(line)
        p.wait()
        if out:
            out.write(f"exit={p.returncode}\n")
            out.close()
        proc = subprocess.CompletedProcess(argv, p.returncode, "".join(lines), "")
    if proc.returncode not in ok:
        raise Stop(proc.returncode, f"{Path(argv[0]).name} exited {proc.returncode}")
    return proc


#: Where a branch is measured from when nothing says: Ludvig's feature branches come off
#: `develop`; a repository without one (ludvig-shared) is on `main` or `master`.
BASE_CANDIDATES = ("origin/develop", "origin/main", "origin/master")


def default_base() -> str:
    try:
        configured = json.loads(Path("human-review.json").read_text(encoding="utf-8")).get("base")
    except (OSError, ValueError, AttributeError):
        configured = None
    if configured:
        return configured
    for ref in BASE_CANDIDATES:
        if subprocess.run(["git", "rev-parse", "-q", "--verify", ref + "^{commit}"],
                          capture_output=True).returncode == 0:
            return ref
    return "origin/main"


def video_configured() -> bool:
    """Whether `human-review.json` sets up the film (`steps.video`). The Ludvig fork has
    postponed the Demo tab: without that block nothing pays a model for a script nobody
    will film."""
    try:
        cfg = json.loads(Path("human-review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(cfg, dict) and (cfg.get("steps") or {}).get("video"))


def has_feature_script() -> bool:
    env = os.environ.get("HUMAN_REVIEW_FEATURE_SCRIPT")
    return any(p.is_file() for p in FEATURE_SCRIPTS) or bool(env and Path(env).is_file())


def model_cost() -> float:
    """What the run's `claude -p` calls cost so far, from their own ledgers."""
    total = 0.0
    started = (REVIEW / ".started").read_text(encoding="utf-8").strip() \
        if (REVIEW / ".started").is_file() else ""
    for name in (".film-runs.json", ".model-runs.json", ".content-runs.json"):
        try:
            doc = json.loads((REVIEW / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in doc.get("runs") or []:
            if isinstance(r.get("cost"), (int, float)) and str(r.get("when", "")) >= started[:19]:
                total += r["cost"]
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="the base ref (default: human-review.json's, else origin/main)")
    ap.add_argument("--no-serve", action="store_true", help="build the page, do not serve it")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    args = ap.parse_args(argv)

    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    if not top:
        print("[run-review] not inside a git repository", file=sys.stderr)
        return 2
    os.chdir(top)
    base = args.base or default_base()
    dry = args.dry_run
    began = time.time()
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    say(f"base {base} · {'inside Claude session ' + session if session else 'no Claude session'}")

    try:
        rp = run([script("review-points.py"), "--check"], ok=(0, 3, 5), dry=dry)
        if rp.returncode == 3:
            say("no review-points.md on this branch — the page will say so in a band")

        run([script("preflight.py"), "--base", base, "--session", session], dry=dry)
        run([script("write-content.py"), "--skeleton-only"], dry=dry)

        if not video_configured():
            say("steps.video not configured — no film, and no film script asked for")
        elif has_feature_script():
            say("the branch has a film script; not asking for a new one")
        else:
            film = run([script("rerun-film.py")], ok=(0, 1, 4), dry=dry)
            if film.returncode:
                say(f"no usable film script (rerun-film.py exit {film.returncode}) — "
                    "the Demo tab will say nothing was filmed")

        steps = run([script("run-steps.py"), "--base", base], ok=(0, 1),
                    tee=REVIEW / "run-steps.out", dry=dry)
        if steps.returncode == 1:
            say("a producer failed — its tab says so; the run goes on (see the table above)")

        guide = run([script("steps-ledger.py"), "start", "guide", "--label",
                     "assemble content.json and build the page"], capture=True, dry=dry)
        if not dry:
            (REVIEW / ".step-guide").write_text(guide.stdout, encoding="utf-8")
        run([script("rerun-model.py")], dry=dry)
        run([script("write-content.py")], dry=dry)
        run([script("steps-ledger.py"), "end",
             (REVIEW / ".step-guide").read_text(encoding="utf-8").strip() if not dry else "N"],
            dry=dry)
        run([script("steps-ledger.py"), "check"], dry=dry)
        run([script("report-cost.py")] + ([] if session else ["--harness", "script"]),
            dry=dry)
        refresh = run([script("refresh-report.py")] + (["--no-serve"] if args.no_serve
                                                            else []), dry=dry)
    except Stop as e:
        print(f"\n[run-review] STOPPED: {e.why}. Nothing was fixed or retried — the command "
              "above is the one to run again once its cause is fixed.", file=sys.stderr)
        return e.code or 1

    mins = (time.time() - began) / 60
    say(f"done in {mins:.0f} min; model calls ${model_cost():.2f}"
        + (" (plus this Claude session's own turns)" if session else ""))
    url = (refresh.stdout or "").strip().splitlines()[-1:] if not dry else []
    if url:
        print(url[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
