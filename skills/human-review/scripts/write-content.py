#!/usr/bin/env python3
"""Write `.human-review/content.json` without an agent in the loop.

Step 4 of `/human-review` used to be an Opus session reading the 25k-token
`content-schema.md` and typing a file that is, line for line, mostly the same on every run:
the schema itself says of the tab strip *copy this strip; do not compose it*, the three piles
are `{"auto": "review-points"}`, the scope bar is computed chips only, and the build rebuilds
every script-owned tab from its own table whatever the file says. What is left is split here
by who can know it:

- **the layout** is copied — from the `content.json` already on disk (a project keeps the
  layout it tuned), else `"contentLayout"` in `human-review.json`, else
  `reference/content.example.json` — and pruned of any block naming a file this repository
  does not have;
- **the facts** are asked of `gh` and git: the PR block, the ticket, the title and
  subtitle, the footer;
- **the verdict** — a score and at most two lines under the grade — is the one judgement, and
  the one model call: a lean `claude -p` (no tools, no settings, no CLAUDE.md, its own
  system prompt) over `reference/verdict-prompt.md` and the run's facts as JSON. A few
  thousand tokens, about two cents on Sonnet, where the agent's start-up alone was $0.38.

    write-content.py                   # layout + facts + the verdict (one claude -p call)
    write-content.py --skeleton-only   # layout + facts, keep the verdict on disk, spend nothing
    write-content.py --dry-run         # print the verdict prompt, spend nothing

Its spending goes in its own ledger, `.human-review/.content-runs.json`, which
`harness_cost.py` bills to the page's "this guide" row beside the matrix and the film.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from hrbuild.shared.forge import api_forge  # noqa: E402


def _sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: The matrix's program, for its plumbing: `_priced`, `record_run`, `parse_answer`, `MODEL`.
MODEL_STEP = _sibling("hr_rerun_model_for_content", "rerun-model.py")

PROMPT = HERE.parent / "reference" / "verdict-prompt.md"
EXAMPLE = HERE.parent / "reference" / "content.example.json"
WRITES = "content.json"
RUNS_LEDGER = ".content-runs.json"

#: The keys this program computes or asks for. Everything else in a layout is copied.
COMPUTED = ("title", "subtitle", "pr", "footer")
#: How much of a long field goes into the prompt: enough to judge by, not a transcript.
CLIP = 400

SYSTEM = ("You grade code changes for a human reviewer. You answer with exactly one JSON "
          "object and no prose around it.")


def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lean_argv(model: str) -> list[str]:
    """A `claude -p` that starts with nothing it does not need: no tools, no MCP servers,
    no settings (so no hooks, plugins or CLAUDE.md), no saved session, and a one-line system
    prompt. Measured on 22 Oct 2026: $0.0024 to say "ok" on Sonnet, against $0.107 for a
    default `claude -p` in the same directory — 37k tokens of start-up, every call."""
    extra = (os.environ.get("HUMAN_REVIEW_MODEL_ARGS") or "").split()
    return ["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            "--system-prompt", SYSTEM, *extra]


# ----------------------------------------------------------------------------- the layout

def layout_source(review: Path, root: Path) -> tuple[dict, str]:
    """`(layout, where it came from)`: the page on disk, the project's, else the example."""
    here = read_json(review / WRITES)
    if isinstance(here, dict) and here.get("tabs"):
        return here, str(review / WRITES)
    cfg = read_json(root / "human-review.json") or {}
    named = cfg.get("contentLayout") if isinstance(cfg, dict) else None
    if named:
        doc = read_json(root / named)
        if isinstance(doc, dict) and doc.get("tabs"):
            return doc, named
        print(f"[content] contentLayout -> {named} is not a content file with tabs; "
              "using the example", file=sys.stderr)
    return json.loads(EXAMPLE.read_text(encoding="utf-8")), str(EXAMPLE)


def prune(spec: dict, root: Path) -> list[str]:
    """Drop, in place, every block that names a file this repository does not have — the
    example is petclinic's, and a `Java packages` diagram on a Go project is a hole the build
    would render as a card of nothing. Returns what it dropped, for the log."""
    dropped = []
    for tab in spec.get("tabs") or []:
        kept = []
        for b in tab.get("blocks") or []:
            src = (b.get("context") or {}).get("src") if b.get("type") == "diagrams" else \
                b.get("src") if b.get("type") == "puml" else None
            if src and not (root / src).exists():
                dropped.append(f"{tab.get('id')}: {b.get('type')} {src}")
                continue
            if b.get("type") == "logging" and b.get("paths"):
                paths = [p for p in b["paths"] if (root / p).exists()]
                if paths != b["paths"]:
                    dropped.append(f"{tab.get('id')}: logging paths "
                                   + ", ".join(p for p in b["paths"] if p not in paths))
                b = {**b, "paths": paths} if paths else {k: v for k, v in b.items()
                                                          if k != "paths"}
            kept.append(b)
        tab["blocks"] = kept
    return dropped


# ----------------------------------------------------------------------------- the facts

def facts(root: Path, review: Path) -> dict:
    """The PR, its ticket, the branch: everything the masthead says that a program can ask."""
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    pr = api_forge().current_pr(root)
    repo = api_forge().repo_web_url(root)
    tb = read_json(review / "ticket-body.json") or read_json(review / "ticket.json") or {}
    ticket = None
    if tb.get("number"):
        ticket = {"number": int(tb["number"]), "title": tb.get("title") or "",
                  "url": tb.get("url") or ""}
        ticket = {k: v for k, v in ticket.items() if v}
    out = {"branch": branch, "ticket": ticket, "pr": None}
    if pr and pr.get("number"):
        block = {"number": pr["number"], "title": pr.get("title"), "url": pr.get("url"),
                 "repo": repo, "branch": pr.get("headRefName") or branch,
                 "base": pr.get("baseRefName")}
        if ticket and ticket.get("number") != pr["number"]:
            block["ticket"] = ticket
        out["pr"] = {k: v for k, v in block.items() if v}
    return out


def apply_facts(spec: dict, f: dict, title: str | None) -> None:
    if f["pr"]:
        spec["pr"] = f["pr"]
    spec["title"] = (f["pr"] or {}).get("title") or title or (f["ticket"] or {}).get("title") \
        or spec.get("title") or f["branch"]
    t = f["ticket"]
    spec["subtitle"] = f["branch"] + (f" — issue #{t['number']}" if t else "")
    spec["footer"] = (f"Built by /human-review from {f['branch']} on "
                      f"{dt.date.today().strftime('%-d %b %Y')}.")
    for pile in ("findings", "autofixes", "assumptions"):
        spec[pile] = {"auto": "review-points"}
    spec.pop("summary", None)


# ----------------------------------------------------------------------------- the verdict

def _clip(s, n: int = CLIP) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def verdict_input(root: Path, review: Path, f: dict) -> dict:
    """What the grader reads: the recorded review, compact; the run's own facts."""
    rp = read_json(review / "review-points.json") or {}
    points = {
        "open": [{"title": x.get("title"), "severity": x.get("severity"),
                  "source": x.get("source"), "why": _clip(x.get("why") or x.get("observation"))}
                 for x in rp.get("findings") or []],
        "fixed": [{"title": x.get("title"), "severity": x.get("severity")}
                  for x in rp.get("autofixes") or []],
        "assumed": [{"title": x.get("title"), "confidence": x.get("confidence"),
                     "alternative": _clip(x.get("alternative"))}
                    for x in rp.get("assumptions") or []],
    } if rp else None
    steps = [{"step": s.get("step"), "status": s.get("status"),
              "why": _clip(s.get("reason"), 200),
              "notes": [_clip(n, 240) for n in s.get("notes") or []]}
             for s in read_json(review / ".steps-status.json") or []
             if isinstance(s, dict) and (s.get("status") != "ran" or s.get("notes"))]
    am = read_json(review / "aftermath.json") or {}
    human = [c.get("subject") for c in am.get("commits") or []
             if any(not x.get("generated") for x in c.get("files") or [])]
    verdicts = {name: _clip(json.dumps(doc), 600) for name in ("feature", "sequence")
                if (doc := read_json(review / "assets" / f"{name}.verdict.json"))}
    try:
        gate = (review / ".gate").read_text(encoding="utf-8").strip()
    except OSError:
        gate = None
    base = (rp.get("frontmatter") or {}).get("audited-base") or \
        (rp.get("frontmatter") or {}).get("base") or "origin/main"
    return {
        "needTitle": not f["pr"],
        "pr": (f["pr"] or {}).get("title"),
        "ticket": f["ticket"],
        "ticketBody": _clip((read_json(review / "ticket-body.json") or {}).get("body"), 1500),
        "commits": git(root, "log", "--format=%s", f"{base}..HEAD").splitlines()[:30],
        "reviewPoints": points if points else "no review-points.md on this branch",
        "gate": gate,
        "steps": steps,
        "aftermath": {"humanCommits": human[:10]},
        "verdicts": verdicts,
    }


def build_prompt(asked: dict) -> str:
    return (PROMPT.read_text(encoding="utf-8").split("\n---\n", 1)[-1].strip()
            + "\n\n## Input\n\n```json\n" + json.dumps(asked, indent=1, ensure_ascii=False)
            + "\n```\n")


def check_verdict(doc) -> list[str]:
    if not isinstance(doc, dict):
        return ["the reply holds no JSON object"]
    bad = []
    score = doc.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 10:
        bad.append(f"score must be an integer 0-10, got {score!r}")
    bullets = doc.get("bullets", [])
    if not isinstance(bullets, list) or not all(isinstance(b, str) and b.strip()
                                                for b in bullets):
        bad.append("bullets must be a list of sentences")
    return bad


def ask_verdict(root: Path, review: Path, asked: dict) -> tuple[dict | None, str]:
    """`(verdict, why not)` — one call, recorded in the ledger whatever it answered."""
    model = MODEL_STEP.MODEL
    argv = lean_argv(model)
    prompt = build_prompt(asked)
    print(f"[content] $ claude -p --model {model} (lean)  < {PROMPT.name} + the run's facts "
          f"({len(prompt)} chars)")
    started = time.time()
    proc = subprocess.run(argv, cwd=str(root), input=prompt, text=True, capture_output=True)
    cost, said = MODEL_STEP._priced(proc.stdout)
    MODEL_STEP.record_run(review, cost, time.time() - started, ledger=RUNS_LEDGER,
                          model=model, out=proc.stdout)
    if proc.returncode != 0:
        return None, f"{model} exited {proc.returncode}: {_clip(proc.stderr, 300)}"
    doc = MODEL_STEP.parse_answer(said or proc.stdout)
    bad = check_verdict(doc)
    if bad:
        return None, "; ".join(bad) + f" — reply: {_clip(said or proc.stdout, 300)}"
    price = f"${cost:.4f}" if isinstance(cost, (int, float)) else "an unpriced run"
    print(f"[content] verdict {doc['score']}/10, {len(doc.get('bullets') or [])} bullet(s), "
          f"for {price}")
    return doc, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--skeleton-only", action="store_true",
                    help="layout and facts only; keep the verdict on disk, spend nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the verdict prompt, write nothing, spend nothing")
    args = ap.parse_args(argv)

    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel") or ".").resolve()
    review = Path(args.dir)
    if not review.is_absolute():
        review = root / review
    if not review.is_dir():
        print(f"[content] no {review}/ — run preflight.py first", file=sys.stderr)
        return 2

    spec, source = layout_source(review, root)
    spec = json.loads(json.dumps(spec))
    print(f"[content] layout from {source}")
    for d in prune(spec, root):
        print(f"[content] dropped {d} — not in this repository")
    f = facts(root, review)
    asked = verdict_input(root, review, f)

    if args.dry_run:
        print(build_prompt(asked))
        return 0

    verdict = None
    if not args.skeleton_only:
        if not shutil.which("claude"):
            print("[content] no `claude` on PATH — the verdict is the one judgement in this "
                  "file and nothing else can write it.", file=sys.stderr)
            return 2
        verdict, why = ask_verdict(root, review, asked)
        if verdict is None:
            print(f"[content] the verdict is refused: {why}", file=sys.stderr)
            return 5
        spec["verdict"] = {"score": verdict["score"],
                           "bullets": [b.strip() for b in verdict.get("bullets") or []][:2]}
        if not spec["verdict"]["bullets"]:
            spec["verdict"].pop("bullets")
    apply_facts(spec, f, (verdict or {}).get("title"))
    (review / WRITES).write_text(json.dumps(spec, indent=1, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    print(f"[content] {review / WRITES} written"
          + (" (no verdict yet)" if "verdict" not in spec else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
