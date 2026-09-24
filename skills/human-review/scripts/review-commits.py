#!/usr/bin/env python3
"""Which commit is the implementation, which one is the review, and what came after.

The flow this reads is two commits wide. The first is the feature and nothing else; the
second carries the fixes the agent accepted plus the `review-points.md` that records what
it accepted, declined and assumed. Everything the page wants to say about phases — what
writing the code cost against what reviewing it cost, and what a human changed *after* the
agent was finished — hangs off knowing which commit is which.

**Trailers, not a message convention.** The three that matter are

    Review-Points: review-points.md     ← this commit is the review commit
    Implements: <sha>                   ← and that one was the implementation
    Claude-Session: <session id>        ← the conversation that did both

because trailers survive a cherry-pick, a rebase and a squash-into-a-branch, which is
exactly what happens to a demo branch between the run and the page. A subject-line
convention ("fix: review fixes") survives none of it, and `.human-review/.session` is
gitignored and dies with the directory.

**Read out of the body, not only out of `%(trailers)`.** Git's trailer parser only looks
at the *last* paragraph of the message, and the harness appends its own paragraph —
`Co-Authored-By: Claude …` — after whatever the agent wrote. The three keys then sit in
the penultimate paragraph and `%(trailers:key=…)` returns empty for all of them, which is
what the first `/implement-ticket` run produced: two perfectly trailered commits reported
as "not recorded". So `%(trailers)` is the first pass and the message body is the second,
matched line by line anywhere in the message. That is the normal case, not a degraded one,
and it warns about nothing: a key on its own line IS the record, wherever the harness
ended up putting it.

**`[auto-fix]` in the subject marks what the agent fixed on its own.** `/implement-ticket`
puts it on every commit that applies a reviewer's findings, so `git log --grep='\[auto-fix\]'`
finds that work later — including a second round after the review commit, or a branch
whose trailers a squash lost. Every such commit is listed under `auto_fixes`, a commit
after the review carries `auto_fix: true`, and with no trailer the last `[auto-fix]`
commit is the review commit (a tagged commit is the agent saying so, which beats guessing
from the file).

**The fallback is a guess and says so.** With no trailer and no `[auto-fix]` tag, the single
commit in the range that touches the points file is taken as the review commit, with a
warning. Two such commits, or none, is not guessed at all: an ambiguous answer here silently mis-bases
every fix diff on the page, and "I could not tell" is a thing the page can render.

Exit codes:  0 found · 2 not a git repository / no range · 3 no review commit and no
usable fallback.

Usage:
  review-commits.py --base origin/main            # the two commits, as a table
  review-commits.py --base origin/main --json     # the same, as data
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_FILE = "review-points.md"
CONFIG = "human-review.json"

# One record per commit, NUL-ish delimited: a trailer value can itself contain newlines,
# so neither field nor record separator may be one. `%B` is last because it is the only
# multi-line field and keeping it at the end makes a short record obvious.
FIELDS = ("sha", "when", "points", "implements", "session", "subject", "body")
FMT = ("%H%x1F%cI%x1F"
       "%(trailers:key=Review-Points,valueonly,separator=%x2C)%x1F"
       "%(trailers:key=Implements,valueonly,separator=%x2C)%x1F"
       "%(trailers:key=Claude-Session,valueonly,separator=%x2C)%x1F"
       "%s%x1F%B%x1E")

# The same three keys, read off any line of the message. `git interpret-trailers` would
# not help here: it has the same last-paragraph rule that loses them.
BODY_KEYS = {"Review-Points": "points", "Implements": "implements",
             "Claude-Session": "session"}
BODY_RE = re.compile(r"^(Review-Points|Implements|Claude-Session):[ \t]*(\S.*?)[ \t]*$",
                     re.MULTILINE)


#: The subject tag the coding agent puts on a commit of fixes it applied from a review.
AUTO_FIX_TAG = "[auto-fix]"

#: How a takeover announces itself in the points file: `## Taken over without a new pass —
#: 21 Sep 2026`. The same opening words `review-points.py` reads the note by
#: (`NOTE_HEADINGS` there — `test_review_commits_takeover.py` holds the two equal).
NOTE_HEADINGS = ("taken over", "carried over", "not re-reviewed")
NOTE_RE = re.compile(r"^##\s+((?:" + "|".join(map(re.escape, NOTE_HEADINGS)) + r")\b.*?)\s*$",
                     re.IGNORECASE | re.MULTILINE)


def is_auto_fix(commit: dict) -> bool:
    return AUTO_FIX_TAG in (commit.get("subject") or "").lower()


def git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout


def points_file(root: Path) -> str:
    p = root / CONFIG
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        value = data.get("reviewPoints") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_FILE


def from_body(body: str) -> dict[str, str]:
    """The three keys as they appear on their own lines, anywhere in the message.

    Multiple values for one key are joined with a comma, which is exactly what
    `%(trailers:…,separator=%x2C)` emits, so every reader downstream keeps working
    unchanged — `.split(",")[0]` means the same thing either way.
    """
    found: dict[str, list[str]] = {}
    for key, value in BODY_RE.findall(body or ""):
        bucket = found.setdefault(BODY_KEYS[key], [])
        if value not in bucket:
            bucket.append(value)
    return {k: ",".join(v) for k, v in found.items()}


def log(root: Path, base: str, head: str = "HEAD") -> list[dict]:
    """`base..head`, oldest first, with the three trailers read out of each message.

    Git first, because when the keys really are the last paragraph its parser handles
    continuation lines and folding that a regex would not. The body second, because the
    harness's own `Co-Authored-By:` paragraph routinely lands after them and git then sees
    no trailer block at all.
    """
    rng = f"{base}..{head}" if base else head
    code, out = git(root, "log", "--reverse", f"--format={FMT}", rng)
    if code != 0:
        return []
    rows = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split("\x1f")
        if len(parts) < len(FIELDS):
            continue
        row = dict(zip(FIELDS, (p.strip() for p in parts)))
        scanned = from_body(row.get("body", ""))
        for field, value in scanned.items():
            if not row.get(field):
                row[field] = value
        rows.append(row)
    return rows


def resolve(root: Path, rev: str) -> str | None:
    code, out = git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out.strip() or None if code == 0 else None


def takeover_heading(root: Path, sha: str, rel: str) -> str | None:
    """The takeover note's heading, when the points file *at this commit* carries one.

    A takeover is a `Review-Points:` commit that moves the point forward without anybody
    re-reading the code: Victor decides the commits since the review are accepted as they
    are, and a note in the points file says so. It needs the trailer — without it the
    aftermath band keeps counting from the old point — but it is not a review, and read as
    one it did two kinds of damage: it became "the review commit", so the `Implements:` it
    carries (the previous takeover, or the last feature commit) was reported as the
    implementation, and the commits it took over dropped out of the scripted list and
    survived only as a list an agent typed into the note by hand. Read at the commit and
    not at the head, so a later real pass that drops the note is a review again."""
    code, out = git(root, "show", f"{sha}:{rel}")
    if code != 0:
        return None
    m = NOTE_RE.search(out)
    return m.group(1).strip() if m else None


def touching(root: Path, base: str, head: str, rel: str) -> list[str]:
    code, out = git(root, "log", "--reverse", "--format=%H",
                    f"{base}..{head}" if base else head, "--", rel)
    return [s for s in out.split() if s] if code == 0 else []


def detect(root: Path, base: str, head: str = "HEAD", rel: str | None = None) -> dict:
    """The two commits, what came after them, and every doubt about the answer.

    `after` is the honesty requirement: commits landing on the branch once the agent has
    stopped are invisible on a page built from the diff alone, and they are the one thing
    that can make every other claim on it stale.
    """
    rel = rel or points_file(root)
    commits = log(root, base, head)
    warnings: list[str] = []

    marked = [c for c in commits if c["points"]]
    # Takeovers out of the running for "the review commit" (see `takeover_heading`). The
    # newest of them is kept: it is where the reader's sign-off now sits, and the band
    # splits what came after the review into what it took over and what came after it.
    takeover = None
    reviews = []
    for c in marked:
        heading = takeover_heading(root, c["sha"], rel)
        if heading:
            takeover = {"sha": c["sha"], "when": c["when"], "subject": c["subject"],
                        "heading": heading}
        else:
            reviews.append(c)
            takeover = None          # a real pass after a takeover supersedes it
    takeover_shas = {c["sha"] for c in marked} - {c["sha"] for c in reviews}
    if reviews:
        marked = reviews
    else:
        takeover = None              # nothing earlier to take over from: read as before
    tagged = [c for c in commits if is_auto_fix(c)]
    fallback = False
    review = None
    if not marked and tagged:
        review = tagged[-1]
        warnings.append(
            f"no Review-Points trailer in {base}..{head} — taking {review['sha'][:8]}, the "
            f"last commit tagged {AUTO_FIX_TAG}, as the review commit. Add the trailer and "
            "the Implements: link comes with it.")
    elif marked:
        # The last, not the first: a second round of review fixes is a real thing, and the
        # newest record is the one describing the file as it now stands.
        review = marked[-1]
        if len(marked) > 1:
            warnings.append(
                f"{len(marked)} commits carry a Review-Points trailer "
                f"({', '.join(c['sha'][:8] for c in marked)}); taking the last one. Two "
                "rounds of review on one branch is legitimate, but the page reports one.")
    else:
        hits = touching(root, base, head, rel)
        if len(hits) == 1:
            review = next((c for c in commits if c["sha"] == hits[0]), None)
            fallback = review is not None
            if fallback:
                warnings.append(
                    f"no Review-Points trailer in {base}..{head} — falling back to "
                    f"{hits[0][:8]}, the only commit that touches {rel}. That is a guess: "
                    "add the trailer and it becomes a fact that survives a rebase.")
        elif len(hits) > 1:
            warnings.append(
                f"no Review-Points trailer, and {len(hits)} commits touch {rel} "
                f"({', '.join(h[:8] for h in hits)}) — which of them is the review commit "
                "cannot be guessed, and guessing it would mis-base every fix diff.")
        else:
            warnings.append(
                f"no Review-Points trailer in {base}..{head}, and nothing in the range "
                f"touches {rel} — nobody recorded what was reviewed on this branch.")

    implementation = None
    if review is not None and review["implements"]:
        raw = review["implements"].split(",")[0].strip()
        implementation = resolve(root, raw)
        if implementation is None:
            warnings.append(f"Implements: {raw} does not resolve to a commit here — a "
                            "rebase rewrote it, or it was typed by hand")
    elif review is not None:
        # Deliberately not "the commit before the review commit". That is right often
        # enough to be trusted and wrong silently: on a branch with three commits of
        # implementation it names the last of them, and every fix diff on the page is then
        # based one commit too late, showing part of the feature as if it were a fix.
        warnings.append("the review commit carries no Implements trailer, so which commit "
                        "is the implementation is not recorded — the implementation phase "
                        "cannot be dated")

    session = None
    for candidate in (review, next((c for c in commits if c["sha"] == implementation), None)):
        if candidate and candidate["session"]:
            session = candidate["session"].split(",")[0].strip()
            break
    if session is None and commits:
        warnings.append("no Claude-Session trailer on either commit — the coding session "
                        "is only findable through .human-review/.session or by scanning "
                        "transcripts, and both of those outlive their accuracy")

    after: list[dict] = []
    if review is not None:
        seen = False
        # Up to and including the takeover commit, a commit was accepted without a pass;
        # after it, nobody has signed it off at all.
        taken = takeover is not None
        for c in commits:
            if seen:
                row = {"sha": c["sha"], "when": c["when"], "subject": c["subject"],
                       # The agent's own second round, not a human's hand edit.
                       "auto_fix": is_auto_fix(c)}
                if takeover is not None:
                    row["taken_over"] = taken
                    # The bookkeeping commits themselves: they touch the points file and
                    # nothing else, so the band leaves them off its list.
                    row["takeover"] = c["sha"] in takeover_shas
                after.append(row)
                if takeover is not None and c["sha"] == takeover["sha"]:
                    taken = False
            seen = seen or c["sha"] == review["sha"]

    return {
        "base": base, "head": head, "points_file": rel,
        "commits": len(commits),
        "implementation": implementation,
        "review": review["sha"] if review else None,
        "session": session,
        "fallback": fallback,
        "after": [c["sha"] for c in after],
        "after_detail": after,
        "review_when": review["when"] if review else None,
        "takeover": takeover if review is not None else None,
        "auto_fixes": [{"sha": c["sha"], "when": c["when"], "subject": c["subject"]}
                       for c in tagged],
        "warnings": warnings,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="the repository (default: cwd)")
    ap.add_argument("--base", default="origin/main", help="what the branch is measured from")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--file", help="the points file, when it is not review-points.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    code, top = git(root, "rev-parse", "--show-toplevel")
    if code != 0 or not top.strip():
        print(f"[review-commits] {root} is not a git repository", file=sys.stderr)
        return 2
    root = Path(top.strip())

    found = detect(root, args.base, args.head, args.file)
    if not found["commits"]:
        print(f"[review-commits] no commits in {args.base}..{args.head} — nothing to "
              "attribute", file=sys.stderr)
        return 2

    for w in found["warnings"]:
        print(f"[review-commits] WARNING: {w}", file=sys.stderr)

    if args.json:
        print(json.dumps(found, indent=1))
    else:
        print(f"{found['commits']} commit(s) in {args.base}..{args.head}")
        print(f"  implementation  {found['implementation'] or '— not recorded'}")
        print(f"  review          {found['review'] or '— not found'}"
              + ("   (fallback: the only commit touching the points file)"
                 if found["fallback"] else ""))
        print(f"  session         {found['session'] or '— not recorded'}")
        if found["takeover"]:
            t = found["takeover"]
            print(f"  taken over at   {t['sha']}  ({t['heading']})")
        if found["auto_fixes"]:
            print(f"  {AUTO_FIX_TAG}      {len(found['auto_fixes'])} commit(s):")
            for c in found["auto_fixes"]:
                print(f"      {c['sha'][:8]}  {c['when'][:16]}  {c['subject'][:60]}")
        if found["after_detail"]:
            print(f"  after the review  {len(found['after_detail'])} commit(s):")
            for c in found["after_detail"]:
                print(f"      {c['sha'][:8]}  {c['when'][:16]}  {c['subject'][:60]}")
        elif found["review"]:
            print("  after the review  nothing — the branch is as the agent left it")
        else:
            # Without a review commit there is no "after", and printing one anyway would
            # say the branch is untouched on the strength of not knowing when it stopped.
            print("  after the review  — nothing to date from")

    return 0 if found["review"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
