#!/usr/bin/env python3
"""Ask a model for the two artifacts a program cannot re-derive, and nothing else.

`refresh-report.py` is the machine half of `/human-review`: fixed inputs, fixed outputs,
free, safe to run again at any time. This is the other half, reduced to the smallest thing
that can still be a *command* — the requirements↔tests matrix
(`assets/requirements-map.html`) and the per-test catalogue behind it (`test-index/`).

It exists because the skill's own instruction for that half is "fork a subagent, and give
it `model: sonnet`", which is only executable from inside a Claude session. The page is
read outside one: a reader with the report open in front of them and a branch that has
moved cannot fork anything. So the instruction is spelt as a program — `claude -p --model
sonnet` over `reference/matrix-prompt.md` — and the review server can put a button on it.

Three properties are the whole point, and each of them is a thing that went wrong once:

- **Sonnet, named here.** Not the harness's default. These two artifacts are the only paid,
  non-reproducible work left in the skill and their price is a visible line on the cost
  tab; Opus writes this matrix no better and costs several times as much.
- **The previous pair is kept**, under `.human-review/.model-prev/`. This replaces a
  judgement rather than refreshing it — a second pass over the same diff words and ranks
  it differently — so the copy the reader was looking at has to survive the click that
  regenerated it. Dot-prefixed, because `publish-demo.sh` publishes what does not start
  with a dot and a demo carrying two matrices is a demo with a bug in it.
- **It refuses rather than half-writes.** A model run that ends with one of the two
  artifacts missing or empty leaves the report in the one state `refresh-report.py` is
  built to refuse, so this exits non-zero and says which one, before the build is reached.

    rerun-model.py                      # regenerate both, in .human-review/
    rerun-model.py --dir .human-review  # …explicitly
    rerun-model.py --dry-run            # print the command and the prompt's head, spend nothing

`--dry-run` is not a nicety either: every test of this file, and every check that the
button is wired to the right program, runs through it. Nothing below it may cost money.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: The prompt, beside the skill's other prose rather than inside this file. It is the thing
#: being paid for, so it is reviewed like prose and diffed like prose.
PROMPT = HERE.parent / "reference" / "matrix-prompt.md"

#: What the model owns, in `refresh-report.MODEL_OWNED`'s own spelling minus `content.json`
#: — the layout and the ledes are a human's answer to "what is this page for", and no
#: button regenerates those.
WRITES = ("assets/requirements-map.html", "test-index")

#: Where the copy being replaced goes. Dot-prefixed: see the module docstring.
PREV = ".model-prev"

#: What each paid run really cost, appended here so the button that spends it can stop
#: guessing. The label used to read `~$5 on Sonnet` — a number typed once, never measured —
#: and three real runs on the demo page came in at $4.00, $8.09 and $10.63. A reader who
#: budgeted for the label was out by a factor of two, in the direction that matters.
#:
#: Dot-prefixed like the manifest, and for the same reason: it is a record of this
#: machine's spending, not a fact about the branch, so it must not travel in the zip.
RUNS_LEDGER = ".model-runs.json"
RUNS_KEPT = 20

#: The model, named rather than defaulted. `HUMAN_REVIEW_MODEL` overrides it for the one
#: case that is not a preference — a harness where `sonnet` resolves to nothing.
MODEL = os.environ.get("HUMAN_REVIEW_MODEL") or "sonnet"


#: What makes a directory a review directory. Checked before anything is bought, because
#: `--dir` arrives from a server that computed it as a path *relative to the repository
#: root* — so the value is `.` whenever the report is served from the root itself, and `.`
#: is a directory that exists. Without this, a misconfigured server would have handed a
#: model the repository and asked it to write a matrix into it, at full price, and the
#: first sign of trouble would have been the invoice.
LOOKS_LIKE_A_REVIEW = "content.json"


def claude_argv(root: Path) -> list[str]:
    """The headless invocation, as argv, with the prompt going in on **stdin**.

    On stdin and not as the trailing argument, which is how this was first written and
    which does not work: `--add-dir` takes a *list* of directories, so a prompt after it is
    swallowed as another directory and `claude` exits with "Input must be provided" — a
    failure that looks like a broken model step and is actually a broken command line. It
    is also the safer shape, because a 4KB positional argument is a 4KB positional
    argument.

    `--permission-mode acceptEdits` because the run's whole job is to write two files and
    there is nobody at a keyboard to approve each one; `--add-dir` so the repository is
    reachable when the server's cwd and the repository are not the same directory. Built as
    data, and printed by `--dry-run`, so "which model did that page cost" is a question the
    command answers rather than one the invoice does.
    """
    extra = (os.environ.get("HUMAN_REVIEW_MODEL_ARGS") or "").split()
    return ["claude", "-p", "--model", MODEL, "--permission-mode", "acceptEdits",
            "--output-format", "json", "--add-dir", str(root), *extra]


def missing(review: Path) -> list[str]:
    """Which of the two artifacts is not on disk, or is on disk and empty.

    A directory that exists and holds nothing is the same absence as no directory: a
    `test-index/` left behind empty by a run that died is not a smaller catalogue, it is no
    catalogue, and the honest answer to both is that nobody wrote it."""
    gone = []
    for rel in WRITES:
        p = review / rel
        if p.is_dir():
            if not any(p.iterdir()):
                gone.append(rel)
        elif not p.is_file() or not p.stat().st_size:
            gone.append(rel)
    return gone


def unmapped(review: Path) -> list[str]:
    """Tests the catalogue says cover a requirement, and the matrix has no row for.

    The two artifacts are one artifact in two files: `mapping.json` says *which* test
    pins which sentence of the ticket, and `requirements-map.html` is the rendering of
    exactly that. A test named in the mapping and absent from the matrix is not a smaller
    matrix — it is the page telling the reader a requirement is uncovered while the
    catalogue beside it says who covers it.

    Checked here because a run can exit 0 having written neither: `keep_previous` copies
    today's pair into `.model-prev/` *before* the model starts, so at that moment the two
    are byte-identical, and a model that reads "diff your work against the previous copy"
    as "am I different from `.model-prev/`?" gets no for free and stops. That happened —
    a scenario added to the branch reached `test-index/` and never reached the matrix, and
    the run reported success. The prompt now says so in as many words; this is the half
    that does not depend on the model having read it.

    A missing or unparseable `mapping.json` returns nothing to complain about: this is a
    consistency check between two files, not a second opinion on either one's shape.
    """
    mapping = review / "test-index" / "mapping.json"
    matrix = review / WRITES[0]
    if not mapping.is_file() or not matrix.is_file():
        return []
    try:
        doc = json.loads(mapping.read_text(encoding="utf-8"))
        html = matrix.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []

    ids: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for t in node.get("tests") or []:
                if isinstance(t, dict) and isinstance(t.get("id"), str):
                    ids.append(t["id"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return sorted({i for i in ids if i not in html})


def keep_previous(review: Path) -> Path:
    """Put today's pair somewhere the next run can read it, and return where.

    Copied and not moved. The prompt asks the model to diff its work against what was
    there, which it can only do if what was there is still where the report says it is
    while the model is working."""
    dest = review / PREV
    dest.mkdir(parents=True, exist_ok=True)
    for rel in WRITES:
        src = review / rel
        target = dest / Path(rel).name
        if src.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
        elif src.is_file():
            shutil.copy2(src, target)
    return dest


def _priced(out: str):
    """`(cost, what the model said)` out of `claude -p --output-format json`.

    `(None, "")` for anything this does not recognise, which is not an error: the run
    happened and the artifacts are on disk either way, and a bookkeeping field that moved
    between CLI versions must never be the reason a $5 run reports as a failure.
    """
    try:
        doc = json.loads(out)
        if not isinstance(doc, dict):
            raise ValueError
    except Exception:
        return None, ""
    cost = doc.get("total_cost_usd")
    return (float(cost) if isinstance(cost, (int, float)) else None,
            doc.get("result") or "")


def record_run(review: Path, cost, seconds: float) -> None:
    """Append what this run cost, so the button can stop guessing what the next one will.

    Appended even when the cost could not be read, with `cost: null` — the *number* of runs
    is itself the answer to "has anybody ever pressed this", and a ledger that only records
    the runs it could price would quietly claim a page had never been rerun.

    Bounded, and failures are swallowed whole. This is bookkeeping running after the money
    has already been spent; an unwritable directory is not a reason to report a successful
    run as a failed one.
    """
    path = review / RUNS_LEDGER
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        runs = doc.get("runs") if isinstance(doc, dict) else doc
        if not isinstance(runs, list):
            runs = []
    except Exception:
        runs = []
    runs.append({"when": datetime.datetime.now(datetime.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "model": MODEL, "cost": cost, "seconds": round(seconds, 1)})
    try:
        path.write_text(json.dumps({"version": 1, "runs": runs[-RUNS_KEPT:]}, indent=2)
                        + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command and spend nothing")
    args = ap.parse_args(argv)

    review = Path(args.dir)
    if not review.is_dir() or not (review / LOOKS_LIKE_A_REVIEW).is_file():
        print(f"[model] {review}/ is not a review directory — no "
              f"{LOOKS_LIKE_A_REVIEW} in it, so there is no report to regenerate. "
              "Run /human-review from the repository root first.", file=sys.stderr)
        return 2
    if not PROMPT.is_file():
        print(f"[model] {PROMPT} is missing — that file *is* the step.", file=sys.stderr)
        return 2

    prompt = PROMPT.read_text(encoding="utf-8")
    root = Path.cwd().resolve()
    argvec = claude_argv(root)

    # Printed with the prompt named rather than quoted, always. It is 60 lines long and a
    # log line carrying all of it is a log line nobody reads — including the one place it
    # matters, which is a reader checking what the button they pressed is about to buy.
    print("[model] $ " + " ".join(argvec)
          + f"  < {PROMPT.name} ({len(prompt)} chars)")
    if args.dry_run:
        print(f"[model] dry run — nothing was asked of {MODEL} and nothing was paid for.")
        return 0

    if not shutil.which("claude"):
        print("[model] no `claude` on PATH. This step is the skill's model half spelt as a "
              "command; without the CLI there is nothing to spell it with.", file=sys.stderr)
        return 2

    kept = keep_previous(review)
    print(f"[model] the pair being replaced is in {kept}/ — this is a judgement, not a "
          "refresh, and the copy you were reading has to survive the click.")
    started = time.time()
    # `--output-format json` rather than plain text, for one field: `total_cost_usd`. `-p`
    # does not stream in either mode — the prose arrives when the run is over — so nothing
    # a reader watches is lost by taking it out of an object instead of off stdout, and
    # what is gained is the only honest source for what this button costs.
    proc = subprocess.run(argvec, cwd=str(root), input=prompt, text=True,
                          capture_output=True)
    cost, said = _priced(proc.stdout)
    if said:
        print(said)
    elif proc.stdout:
        # An older CLI, or a shape this does not know: print what came back rather than
        # swallowing the run's own last word over a bookkeeping field.
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    record_run(review, cost, time.time() - started)
    if proc.returncode != 0:
        print(f"[model] {MODEL} exited {proc.returncode}; the artifacts on disk are "
              "whatever it managed to write.", file=sys.stderr)
        return 1

    gone = missing(review)
    if gone:
        print("[model] the run ended with these still missing or empty: "
              + ", ".join(gone), file=sys.stderr)
        print(f"[model] the previous pair is in {kept}/ — copy it back rather than "
              "building a page whose matrix is not there.", file=sys.stderr)
        return 4
    stranded = unmapped(review)
    if stranded:
        print("[model] the catalogue names these as covering a requirement and the matrix "
              "has no row for them: " + ", ".join(stranded), file=sys.stderr)
        print("[model] the two files are one artifact; a matrix missing a mapped test "
              f"tells the reader nothing covers it. The previous pair is in {kept}/.",
              file=sys.stderr)
        return 5
    print("[model] matrix and catalogue rewritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
