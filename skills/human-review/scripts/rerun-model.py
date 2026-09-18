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
import os
import shutil
import subprocess
import sys
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
            "--add-dir", str(root), *extra]


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
    proc = subprocess.run(argvec, cwd=str(root), input=prompt, text=True)
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
    print("[model] matrix and catalogue rewritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
