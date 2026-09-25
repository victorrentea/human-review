#!/usr/bin/env python3
"""Re-run the review of the whole pull request, and commit the record it leaves.

The Review tab's piles are the branch's own `review-points.md`, and the red band above them
is every commit that landed after the commit carrying it. Only a new *review* moves that
point — a takeover note moves it without anybody reading the code, and the page says so.
This is the button that buys the real thing: a headless `claude -p` pass over the whole PR
(`git diff <merge base> HEAD`, re-read against the code as it now stands), the fixes it
accepts, a fresh `review-points.md` and `.human-review/pr-comments.json` — and then the git
work, done here and not by the model:

    [auto-fix] <subject>             one commit per fix group the run declared
    review-points: re-review …       review-points.md alone, carrying
        Review-Points: review-points.md
        Implements: <the implementation commit, per review-commits.py>
        Claude-Session: <the headless run's session id>

then `git push <the branch's remote> HEAD:<branch>` — never a bare `git push`: a demo
branch whose upstream is `origin/main` would publish the review onto main.

**Why the model does not commit.** It is told not to, and `git add`/`commit`/`push` and
friends are on its `--disallowedTools` list besides; the commits are made here, by explicit
path, out of a manifest the run writes (`.human-review/.review-fixes.json`). A model with a
shell and a staged file it did not put there is one `git commit -a` away from publishing
somebody's half-finished work under a review trailer. So this program:

- **refuses before spending anything** while the index holds staged changes or unmerged
  paths, while HEAD is detached, or while `review-points.md` itself has uncommitted edits;
- snapshots every dirty and untracked path before the run, and after it commits only the
  paths the run changed *and* a fix group claims — a changed path no group claims, or one
  that was already dirty before the run (whose diff would mix somebody's edits into a fix),
  stops it before the first commit, with the working tree left exactly as the run left it;
- keeps the record being replaced — `review-points.md` and `pr-comments.json` — under
  `.human-review/.model-prev/`, like `rerun-model.py` keeps the matrix.

    rerun-review.py                      # review, fix, commit, push
    rerun-review.py --no-push            # …and leave the push to you
    rerun-review.py --dry-run            # print the command, the prompt's head and the
                                         # commits it would make; spend nothing

`--dry-run` is what every test of this file and every check of the button's wiring runs
through. Nothing below it may cost money.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent

#: The prompt, beside the skill's other prose (see `matrix-prompt.md` for why a file).
PROMPT = SKILL / "reference" / "review-prompt.md"

#: The model, named rather than defaulted — and Opus, not the Sonnet `rerun-model.py` uses.
#: The matrix is a rendering of facts already on disk; Opus writes it no better. This is the
#: opposite kind of work: a judgement over a whole PR whose accepted findings are committed
#: as code, whose declines become the record the reviewer signs off on, and whose absence of
#: a finding reads on the page as "the review saw nothing". A missed cross-file bug or a
#: wrong fix here costs a human far more than the difference in price, and the button is
#: pressed once per round of review, not once per build. `HUMAN_REVIEW_REVIEW_MODEL`
#: overrides it for a harness where `opus` resolves to nothing.
MODEL = os.environ.get("HUMAN_REVIEW_REVIEW_MODEL") or "opus"

#: What the button says it costs, until it has a ledger of its own to say better. An
#: estimate, and named as one: a `/code-review high` pass fans out to several reviewer
#: agents over the whole diff, then the run edits and re-checks; on the demo PR (~40
#: commits) that is a few million tokens in, mostly cached, at Opus's $4–$5 in / $20–$25
#: out per million. `RUNS_LEDGER` records what each press really cost.
PRICE = "~$15–$40 on Opus"

#: A ceiling, passed to `claude --max-budget-usd`, so a run that loops cannot turn one
#: press into an invoice. Above the estimate on purpose: a run cut off halfway leaves a
#: working tree this program then refuses to commit, which is the right outcome only for a
#: run that has really gone wrong.
BUDGET = os.environ.get("HUMAN_REVIEW_REVIEW_BUDGET") or "60"

#: Where the replaced record goes. Shared with `rerun-model.py`, which keeps the matrix
#: there under other names; dot-prefixed so `publish-demo.sh` never ships it.
PREV = ".model-prev"
#: The run's declaration of which changed file belongs to which fix (see the prompt).
FIXES = ".review-fixes.json"
#: What each paid run really cost — a separate ledger from the matrix's, because the
#: probe's price on the Tests tab is the matrix's price and must not average this in.
RUNS_LEDGER = ".review-runs.json"
RUNS_KEPT = 20
PR_COMMENTS = "pr-comments.json"
LOOKS_LIKE_A_REVIEW = "content.json"
AUTO_FIX_TAG = "[auto-fix]"
FIX_ID = re.compile(r"\bFIX-(\d+)\b")

#: What the run may do without asking: read, edit, the read-only half of git, the two
#: checks the prompt names, the review skill and the subagents it fans out to.
READ_ONLY_GIT = ("diff", "log", "show", "merge-base", "status", "blame", "grep",
                 "rev-parse", "ls-files")
#: …and what it may not do even if a project's own settings would let it. Every verb that
#: stages, commits, rewrites or publishes, and `gh pr` — posting is the human's button.
DENIED = ("git add", "git commit", "git push", "git reset", "git checkout", "git switch",
          "git stash", "git restore", "git rebase", "git merge", "git cherry-pick",
          "git rm", "git mv", "git revert", "git am", "git apply", "gh pr")


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def out(root: Path, *args: str) -> str:
    p = git(root, *args)
    return p.stdout.strip() if p.returncode == 0 else ""


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def config_base(root: Path) -> str:
    """The branch's base, the way `run-steps.py` reads it: `human-review.json`, else main."""
    try:
        data = json.loads((root / "human-review.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("base"), str) and data["base"]:
            return data["base"]
    except (OSError, ValueError):
        pass
    return "origin/main"


def current_branch(root: Path) -> str:
    return out(root, "symbolic-ref", "--short", "-q", "HEAD")


def remote_of(root: Path, branch: str) -> str:
    """The branch's own remote *name* — `origin` for the demo — never its upstream branch.

    `test-pr` tracks `origin/main`, so the upstream is exactly the wrong thing to push to;
    the remote is only where, and the ref is spelt out as the branch's own name."""
    remote = out(root, "config", f"branch.{branch}.remote")
    return remote if remote and remote != "." else "origin"


def push_argv(remote: str, branch: str) -> list[str]:
    return ["git", "push", remote, f"HEAD:{branch}"]


def index_problems(root: Path) -> tuple[list[str], list[str]]:
    """`(unmerged, staged)` — the two states a commit made here would sweep in."""
    unmerged = out(root, "diff", "--name-only", "--diff-filter=U").splitlines()
    staged = out(root, "diff", "--cached", "--name-only").splitlines()
    return [p for p in unmerged if p], [p for p in staged if p]


def _fingerprint(root: Path, rel: str) -> str:
    p = root / rel
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else "-"
    except OSError:
        return "?"


def snapshot(root: Path) -> dict[str, str]:
    """Every dirty or untracked path, with a fingerprint of its content.

    Compared before and after the run, this is how "the files the run changed" is known
    without asking the run: a path that appears, disappears or changes content is the run's
    doing, because nothing else is working in the tree while it does."""
    raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    tokens = raw.split("\0")
    found: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        code, path = tok[:2], tok[3:]
        if code[0] in "RC":
            i += 1                                  # the rename's source path follows
        found[path] = f"{code}:{_fingerprint(root, path)}"
    return found


def changed_since(root: Path, before: dict[str, str]) -> list[str]:
    after = snapshot(root)
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


# --------------------------------------------------------------------------- #
# the prompt and the model call
# --------------------------------------------------------------------------- #

def fill(text: str, values: dict[str, str]) -> str:
    """`@@NAME@@` holes, not `{name}`: the prompt carries JSON and shell, full of braces."""
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    return text


def prompt_body(text: str) -> str:
    """What the model is handed: the file below its first `---` rule, which is the part
    addressed to the run rather than to whoever reads the prompt in the repository."""
    parts = re.split(r"^---\s*$", text, maxsplit=1, flags=re.MULTILINE)
    return parts[1].lstrip("\n") if len(parts) == 2 else text


def claude_argv(root: Path, session: str) -> list[str]:
    """The headless invocation as argv, prompt on stdin (see `rerun-model.claude_argv` for
    why stdin: `--add-dir` and the tool lists are variadic and would swallow a positional).

    `--session-id` is ours, chosen before the run, so the `Claude-Session:` trailer and the
    frontmatter can name the conversation that did the review — which `review-cost.py` and
    `review-passes.py` then find by id."""
    py = sys.executable
    allowed = ["Read", "Grep", "Glob", "Edit", "Write", "Skill", "Agent", "Task",
               *(f"Bash(git {verb}:*)" for verb in READ_ONLY_GIT),
               "Bash(gh issue view:*)",
               f"Bash({py} {HERE / 'review-points.py'}:*)",
               f"Bash({py} {HERE / 'push-pr-comments.py'}:*)"]
    denied = [f"Bash({verb}:*)" for verb in DENIED]
    extra = (os.environ.get("HUMAN_REVIEW_REVIEW_ARGS") or "").split()
    return ["claude", "-p", "--model", MODEL, "--session-id", session,
            "--permission-mode", "acceptEdits", "--output-format", "json",
            "--max-budget-usd", str(BUDGET), "--add-dir", str(root),
            "--allowedTools", *allowed, "--disallowedTools", *denied, *extra]


def _priced(stdout: str):
    """`(cost, what the model said, session id)` out of `--output-format json`."""
    try:
        doc = json.loads(stdout)
        if not isinstance(doc, dict):
            raise ValueError
    except Exception:
        return None, "", None
    cost = doc.get("total_cost_usd")
    return (float(cost) if isinstance(cost, (int, float)) else None,
            doc.get("result") or "", doc.get("session_id") or None)


def record_run(review: Path, cost, seconds: float, session: str) -> None:
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
                 "model": MODEL, "cost": cost, "seconds": round(seconds, 1),
                 "session": session})
    try:
        path.write_text(json.dumps({"version": 1, "runs": runs[-RUNS_KEPT:]}, indent=2)
                        + "\n", encoding="utf-8")
    except OSError:
        pass


def keep_previous(root: Path, review: Path, points_rel: str) -> Path:
    dest = review / PREV
    dest.mkdir(parents=True, exist_ok=True)
    for src in (root / points_rel, review / PR_COMMENTS):
        if src.is_file():
            shutil.copy2(src, dest / src.name)
    return dest


# --------------------------------------------------------------------------- #
# the record: frontmatter, the takeover note, the fix ids
# --------------------------------------------------------------------------- #

FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def frontmatter(text: str) -> dict[str, str]:
    m = FRONT.match(text)
    if not m:
        return {}
    pairs = (line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def set_frontmatter(text: str, facts: dict[str, str]) -> str:
    """The run's frontmatter with the facts this program computed written over it.

    `base`, `implementation`, `session` and `reviewers` are not the model's to decide: they
    are what git and this program know, and a model asked to copy a sha copies it wrong
    often enough that every diff on the page would be based on a typo."""
    m = FRONT.match(text)
    lines = m.group(1).splitlines() if m else []
    body = text[m.end():] if m else text
    seen = set()
    kept = []
    for line in lines:
        key = line.split(":", 1)[0].strip() if ":" in line else None
        if key in facts:
            kept.append(f"{key}: {facts[key]}")
            seen.add(key)
        else:
            kept.append(line)
    kept += [f"{k}: {v}" for k, v in facts.items() if k not in seen]
    return "---\n" + "\n".join(kept) + "\n---\n\n" + body.lstrip("\n")


def strip_takeover(text: str, headings) -> tuple[str, bool]:
    """Drop a `## Taken over …` note the run left in, whole, up to the next `##`.

    The note is what tells a takeover from a review; left in, this commit would read as
    another takeover and the band would not clear — the one thing the press was for."""
    note = re.compile(r"^##\s+(?:" + "|".join(map(re.escape, headings)) + r")\b",
                      re.IGNORECASE)
    out_lines, skipping, dropped = [], False, False
    for line in text.splitlines(keepends=True):
        if line.startswith("## ") or line.startswith("##\t"):
            skipping = bool(note.match(line))
            dropped = dropped or skipping
        if not skipping:
            out_lines.append(line)
    return "".join(out_lines), dropped


def read_fixes(review: Path) -> list[dict] | None:
    """The run's fix groups, or None when it wrote no manifest at all."""
    path = review / FIXES
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        return None
    groups = doc.get("fixes") if isinstance(doc, dict) else doc
    good = []
    for n, g in enumerate(groups if isinstance(groups, list) else [], 1):
        if not isinstance(g, dict):
            continue
        files = [f for f in (g.get("files") or []) if isinstance(f, str) and f.strip()]
        m = FIX_ID.fullmatch(str(g.get("id") or ""))
        good.append({"id": f"FIX-{m.group(1)}" if m else f"FIX-{n}",
                     "subject": str(g.get("subject") or "apply a review finding").strip(),
                     "why": str(g.get("why") or "").strip(),
                     "files": [f.strip() for f in files]})
    return good


def fix_subject(subject: str) -> str:
    s = re.sub(r"^\s*\[auto-fix\]\s*", "", subject, flags=re.IGNORECASE).strip()
    return f"{AUTO_FIX_TAG} {s[:72]}"


def trailers(points_rel: str, implementation: str, session: str) -> str:
    return (f"Review-Points: {points_rel}\nImplements: {implementation}\n"
            f"Claude-Session: {session}")


def review_message(counts: dict, reviewers: str, merge_base: str, commits: int,
                   replaced: str, points_rel: str, implementation: str,
                   session: str) -> str:
    subject = (f"review-points: re-review the whole PR — {counts['autofixes']} fixed "
               f"· {counts['findings']} declined · {counts['assumptions']} assumed")
    body = (f"A fresh {reviewers} pass over {merge_base[:8]}..HEAD ({commits} commit(s)), "
            f"run headless by rerun-review.py on {MODEL}, re-read against the code as it "
            f"now stands. It replaces {replaced}, so the commits since then are reviewed "
            "and the page's aftermath band counts from this commit.")
    return f"{subject}\n\n{body}\n\n{trailers(points_rel, implementation, session)}\n"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def say(msg: str, err: bool = False) -> None:
    print(f"[review] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def plan_lines(branch: str, remote: str, points_rel: str, implementation: str,
               session: str, review_rel: str) -> list[str]:
    return [
        f"would commit on {branch}, staging by explicit path only:",
        f"  {AUTO_FIX_TAG} <subject>   one commit per fix group the run declares in "
        f"{review_rel}/{FIXES} (none when it accepts no finding)",
        f"  review-points: re-review the whole PR — …   {points_rel} alone, with",
        *("      " + t for t in trailers(points_rel, implementation, session).splitlines()),
        f"then: {' '.join(push_argv(remote, branch))}",
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".human-review", help="the review directory")
    ap.add_argument("--base", help="what the PR is measured against (default: the config's)")
    ap.add_argument("--implements", help="the implementation commit, when the branch's "
                    "trailers do not name one")
    ap.add_argument("--no-push", dest="push", action="store_false",
                    help="commit, and leave the push to you")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command, the prompt and the commits; spend nothing")
    args = ap.parse_args(argv)

    review = Path(args.dir)
    if not review.is_dir() or not (review / LOOKS_LIKE_A_REVIEW).is_file():
        say(f"{review}/ is not a review directory — no {LOOKS_LIKE_A_REVIEW} in it. Run "
            "/human-review from the repository root first.", err=True)
        return 2
    if not PROMPT.is_file():
        say(f"{PROMPT} is missing — that file *is* the review being paid for.", err=True)
        return 2
    top = out(Path.cwd(), "rev-parse", "--show-toplevel")
    if not top:
        say(f"{Path.cwd()} is not inside a git repository.", err=True)
        return 2
    root = Path(top)
    review = review.resolve()
    try:
        review_rel = str(review.relative_to(root))
    except ValueError:
        review_rel = str(review)

    branch = current_branch(root)
    if not branch:
        say("HEAD is detached — there is no branch to commit the review on or to push.",
            err=True)
        return 3
    base = args.base or config_base(root)
    merge_base = out(root, "merge-base", base, "HEAD")
    if not merge_base:
        say(f"no merge base between {base} and HEAD — fetch {base} first.", err=True)
        return 2
    rc = _load("hr_review_commits_rerun", "review-commits.py")
    points_rel = rc.points_file(root)
    found = rc.detect(root, base, "HEAD", points_rel)
    implementation = (out(root, "rev-parse", "--verify", "--quiet",
                          f"{args.implements}^{{commit}}") if args.implements
                      else found.get("implementation"))
    if not implementation:
        say("which commit is the feature's implementation is not recorded — no "
            "`Implements:` trailer on a review commit in "
            f"{base}..HEAD. Pass --implements <sha>.", err=True)
        return 2
    remote = remote_of(root, branch)
    session = str(uuid.uuid4())
    previous = root / points_rel
    ticket = frontmatter(previous.read_text(encoding="utf-8")).get("ticket", "") \
        if previous.is_file() else ""
    commits = len(out(root, "rev-list", f"{merge_base}..HEAD").split())

    prompt = fill(prompt_body(PROMPT.read_text(encoding="utf-8")), {
        "BRANCH": branch, "BASE": base, "MERGE_BASE": merge_base,
        "IMPLEMENTATION": implementation, "TICKET": ticket or "not recorded",
        "SESSION": session, "PREV_DIR": f"{review_rel}/{PREV}", "FIXES": f"{review_rel}/{FIXES}",
        "POINTS": points_rel, "PR_COMMENTS": f"{review_rel}/{PR_COMMENTS}",
        "SKILL": str(SKILL), "PYTHON": sys.executable,
    })
    argvec = claude_argv(root, session)
    say("$ " + " ".join(shlex.quote(a) for a in argvec)
        + f"  < {PROMPT.name} ({len(prompt)} chars)")
    say(f"the whole PR: {merge_base[:8]}..HEAD on {branch}, {commits} commit(s); "
        f"implementation {implementation[:8]}; costs money — {PRICE}, capped at "
        f"${BUDGET}")
    for line in plan_lines(branch, remote, points_rel, implementation, session, review_rel):
        say(line)

    # Refused before anything is bought — the dry run says so too, since "what would the
    # button do right now" is exactly what it is asked.
    unmerged, staged = index_problems(root)
    dirty_points = git(root, "status", "--porcelain", "--", points_rel).stdout.strip()
    problems = []
    if unmerged:
        problems.append("unmerged paths: " + ", ".join(unmerged))
    if staged:
        problems.append("staged changes this program did not make: " + ", ".join(staged)
                        + " — commit them or `git restore --staged` them first")
    if dirty_points:
        problems.append(f"{points_rel} has uncommitted edits the run would overwrite")

    if args.dry_run:
        say("prompt head:")
        for line in prompt.splitlines()[:14]:
            print("    " + line, flush=True)
        if problems:
            say("a real run would refuse right now: " + "; ".join(problems), err=True)
            return 3
        say(f"dry run — nothing was asked of {MODEL}, nothing was paid for, "
            "nothing committed.")
        return 0
    if problems:
        say("refusing, before spending anything: " + "; ".join(problems), err=True)
        return 3
    if not shutil.which("claude"):
        say("no `claude` on PATH — there is nothing to run the review with.", err=True)
        return 2

    kept = keep_previous(root, review, points_rel)
    say(f"the record being replaced is in {kept}/.")
    (review / FIXES).unlink(missing_ok=True)
    before = snapshot(root)
    started = time.time()
    proc = subprocess.run(argvec, cwd=str(root), input=prompt, text=True,
                          capture_output=True)
    cost, said, ran_as = _priced(proc.stdout)
    session = ran_as or session
    print(said or proc.stdout, end="" if (said or proc.stdout).endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    record_run(review, cost, time.time() - started, session)
    if proc.returncode != 0:
        say(f"{MODEL} exited {proc.returncode}; nothing was committed. What it wrote is in "
            f"the working tree; the previous record is in {kept}/.", err=True)
        return 1

    # --- what did the run leave? ---------------------------------------------------- #
    changed = [p for p in changed_since(root, before)
               if not (p == review_rel or p.startswith(review_rel.rstrip("/") + "/"))]
    points = root / points_rel
    if not points.is_file():
        say(f"the run left no {points_rel}; nothing was committed.", err=True)
        return 4
    text = points.read_text(encoding="utf-8")
    text, dropped_note = strip_takeover(text, rc.NOTE_HEADINGS)
    if dropped_note:
        say("the run wrote a takeover note into a real review — dropped it: a note is "
            "what makes the page read the commit as a takeover.", err=True)
    reviewers = f"/code-review high (rerun-review.py, {MODEL})"
    text = set_frontmatter(text, {"base": merge_base[:8], "implementation": implementation[:8],
                                  "reviewers": reviewers, "session": session})
    points.write_text(text, encoding="utf-8")
    check = subprocess.run([sys.executable, str(HERE / "review-points.py"), "--check",
                            "--root", str(root), "--file", points_rel],
                           capture_output=True, text=True)
    if check.returncode != 0:
        print(check.stdout + check.stderr, file=sys.stderr)
        say(f"{points_rel} does not parse (exit {check.returncode}); nothing was committed. "
            f"The previous record is in {kept}/.", err=True)
        return 4

    groups = read_fixes(review)
    fix_files = [p for p in changed if p != points_rel]
    if groups is None:
        if fix_files:
            say(f"the run changed {', '.join(fix_files)} and wrote no {FIXES} saying why; "
                "nothing was committed.", err=True)
            return 6
        groups = []
    claimed: dict[str, str] = {}
    for g in groups:
        stale = [f for f in g["files"] if f not in fix_files]
        if stale:
            say(f"{g['id']} claims {', '.join(stale)}, which the run did not change — "
                "left out of its commit.", err=True)
        g["files"] = [f for f in g["files"] if f in fix_files and f not in claimed]
        for f in g["files"]:
            claimed[f] = g["id"]
    groups = [g for g in groups if g["files"]]
    unclaimed = [p for p in fix_files if p not in claimed]
    mixed = [p for p in claimed if p in before]
    if unclaimed or mixed:
        if unclaimed:
            say("the run changed files no fix group claims: " + ", ".join(unclaimed),
                err=True)
        if mixed:
            say("these were already dirty before the run, so a commit of them would mix "
                "somebody's edits into a fix: " + ", ".join(mixed), err=True)
        say("nothing was committed; the working tree is as the run left it.", err=True)
        return 6
    unmerged, staged = index_problems(root)
    if unmerged or staged:
        say("something was staged during the run (" + ", ".join(unmerged + staged)
            + "); nothing was committed.", err=True)
        return 6

    # --- the commits ---------------------------------------------------------------- #
    def commit(message: str, files: list[str]) -> str | None:
        add = git(root, "add", "--", *files)
        made = git(root, "commit", "-q", "-m", message, "--", *files) \
            if add.returncode == 0 else add
        if made.returncode != 0:
            print(made.stdout + made.stderr, file=sys.stderr)
            return None
        return out(root, "rev-parse", "HEAD")

    shas: dict[str, str] = {}
    for g in groups:
        msg = (fix_subject(g["subject"]) + "\n\n"
               + (g["why"] + "\n\n" if g["why"] else "")
               + f"Applied by the re-run review (rerun-review.py, {MODEL}).\n\n"
               + f"Claude-Session: {session}\n")
        sha = commit(msg, g["files"])
        if not sha:
            say(f"committing {g['id']} failed; the commits before it stand, nothing after "
                "it was made.", err=True)
            return 7
        shas[g["id"]] = sha
        say(f"{sha[:8]}  {fix_subject(g['subject'])}")

    def resolve_ids(s: str) -> str:
        """`FIX-<n>` → the short sha of that group's commit; an unknown id is left alone."""
        return FIX_ID.sub(lambda m: shas[m.group(0)][:8] if m.group(0) in shas
                          else m.group(0), s)

    points.write_text(resolve_ids(points.read_text(encoding="utf-8")), encoding="utf-8")
    doc = _load("hr_review_points_rerun", "review-points.py").document(points, points_rel)
    counts = {k: len(doc[k]) for k in ("autofixes", "findings", "assumptions")}
    replaced = (f"the takeover at {found['takeover']['sha'][:8]} (and the review at "
                f"{found['review'][:8]} before it)" if found.get("takeover")
                else f"the review at {found['review'][:8]}" if found.get("review")
                else "no earlier review")
    sha = commit(review_message(counts, reviewers, merge_base, commits, replaced, points_rel,
                                implementation, session), [points_rel])
    if not sha:
        say(f"committing {points_rel} failed.", err=True)
        return 7
    say(f"{sha[:8]}  review-points: re-review the whole PR "
        f"({counts['autofixes']} fixed · {counts['findings']} declined · "
        f"{counts['assumptions']} assumed)")

    # --- the PR comments: the call, with the shas it could not know ------------------ #
    comments = review / PR_COMMENTS
    if comments.is_file():
        try:
            payload = json.loads(resolve_ids(comments.read_text(encoding="utf-8")))
            payload["commit_id"] = sha
            comments.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
        except (OSError, ValueError, TypeError) as e:
            say(f"{comments} is not usable JSON ({e}); regenerate it with "
                "push-pr-comments.py --from-review-points.", err=True)
    else:
        say(f"the run wrote no {PR_COMMENTS}; deriving the blunter one from the record.",
            err=True)
        subprocess.run([sys.executable, str(HERE / "push-pr-comments.py"),
                        "--from-review-points", "--root", str(root),
                        "--points", points_rel], cwd=str(root))
    if comments.is_file():
        subprocess.run([sys.executable, str(HERE / "push-pr-comments.py"), "--check",
                        "--root", str(root), "--base", merge_base], cwd=str(root))

    if not args.push:
        say(f"committed, not pushed (--no-push). To publish: {' '.join(push_argv(remote, branch))}")
        return 0
    pushed = subprocess.run(push_argv(remote, branch), cwd=str(root),
                            capture_output=True, text=True)
    if pushed.returncode != 0:
        print(pushed.stdout + pushed.stderr, file=sys.stderr)
        say(f"the commits are made and the push failed; run "
            f"`{' '.join(push_argv(remote, branch))}` when the remote will take it.",
            err=True)
        return 8
    say(f"pushed: {' '.join(push_argv(remote, branch))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
