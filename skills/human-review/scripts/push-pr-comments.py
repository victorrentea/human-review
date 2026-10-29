#!/usr/bin/env python3
"""Push the Review tab's three piles onto the pull request, as inline comments on the code.

The judgement is not made here. The agent that wrote `review-points.md` also writes
`.human-review/pr-comments.json` — the exact body of GitHub's *create a review* call
(`POST /repos/{owner}/{repo}/pulls/{n}/reviews`), one comment per item, each anchored on a
line of the diff and worded for the PR's own reader. This script only checks that payload
against the PR as it is *now* and sends it. No model, no rewording, no choosing.

Why the anchors are re-checked at push time and not trusted: the payload's lines are lines
of its `commit_id` (the commit `review-points.md` was recorded in), and the PR head has
usually moved since (a retouch, a merge from `main`). Every comment is sent with the PR
head as its `commit_id`, so every `line` / `start_line` is **carried from the payload's
commit to the head through the diff between the two** — the same hunks the page carries its
refs through (`review-points.py:map_line`) — and the `anchor` text is only the cross-check.
visit-has-vet posted `R__seed.sql:144-147` for a card quoting `142-145`, and
`VisitRestController.java:83` for one quoting `85`: its payload had been written against
`ce56d912`, a review commit of an earlier round, and the comments then chased that round's
anchor text to wherever it now sat. A comment on a line that is not in the diff is refused
by GitHub with a 422 that fails the whole review, so each one is resolved, in order:

  1. the carried line is in the diff                                 → posted there
  2. the diff removed the line, but the `anchor` text is in the diff → posted there ("moved")
  3. the file or the carried line is outside the diff, or the line is gone
                                                    → quoted in the review's summary body,
                                                      with a permalink to the exact lines

Every move and fallback is printed. Nothing is dropped: a finding GitHub cannot pin to a
line is in the review's summary, under *Not on a line of this diff*, with the permalink
(which GitHub renders as the code itself), and the page's *on GitHub ↗* goes there.

**Re-pushing updates, it never duplicates.** Each body ends with a hidden marker,
`<!-- hr:A:vet-id-is-on-delete-set-null -->` — the pile letter and a slug of the item's
`###` title — and before posting the script lists the PR's review comments and PATCHes the
ones already carrying a marker instead of posting them again. The review's own body carries
`<!-- hr:review:<commit> -->`, the payload's commit, so a later round of review gets a
summary of its own instead of overwriting an earlier round's. GitHub cannot move a comment:
one that now belongs on other lines (or in the summary) is **deleted and re-posted** — but
only when the previous push's record (`pr-comments.posted.json`) lists it as this
pipeline's; any other comment is left where it is, and the script says so.

After a push, `.human-review/pr-comments.posted.json` records each marker's `html_url`, so
the page can put a link to its GitHub comment beside every item — **whatever the outcome**:
`status` is `complete` or `partial`, with `present` of `expected` and a `message` in plain
words ("GitHub timed out — 30 of 43 posted; click Retry to post the remaining 13"), so the
page shows what is on GitHub and not what was meant to be.

**One review never carries more than `BATCH` comments.** PR #51's 41 inline comments went
out as one *create a review* call; GitHub gave up answering after its 10 seconds ("If GitHub
takes more than 10 seconds to process an API request, GitHub will terminate the request…
try to simplify your request", docs: *Troubleshooting the REST API*), answered 504 — and
created the review anyway, its comments appearing over the following seconds (30 visible
when the error was shown, all 41 a minute later). So the comments are sent in reviews of
`BATCH`, the first carrying the summary and the rest a one-line "continued", serially and
`GAP` seconds apart (docs: *Best practices* — serial, at least one second between
mutative requests). And a 5xx or a timeout is never simply retried: the script waits until
what that call may have made stops appearing (`settle`), lists the PR again and plans the
rest from what is there, so a re-send can only post what is missing.

`--from-review-points` writes the payload deterministically out of `review-points.md` —
for a branch whose agent predates this file. It is the fallback, not the flow: the agent's
own payload is shorter, better anchored and worded for the PR, which is why it is asked for.

Usage:
  push-pr-comments.py --check                     # validate the payload against HEAD's diff
  push-pr-comments.py --dry-run                   # print the exact gh calls; post nothing
  push-pr-comments.py                             # post / update them on the branch's PR
  push-pr-comments.py --from-review-points        # (re)write the payload from review-points.md
  push-pr-comments.py --drop-stale                # rebuild/drop a payload from another branch
  push-pr-comments.py --pr 49 --repo victorrentea/petclinic --dry-run

Exit codes: 0 ok · 2 bad payload / no PR · 3 no payload file · 4 a GitHub call failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import importlib.util
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from hrbuild.shared.forge import TRANSIENT, GhError, api_forge  # noqa: E402

#: The forge's PR comment API: `gh api` on GitHub. The plan is written against its paths.
PR_API = api_forge().pr_api()
DEFAULT_FILE = ".human-review/pr-comments.json"
POSTED_SUFFIX = ".posted.json"
REVIEW_MARKER = "<!-- hr:review -->"         # a summary from before rounds were told apart
MARKER = re.compile(r"<!--\s*hr:([A-Za-z]:[a-z0-9-]+)\s*-->")
ROUND_LEN = 12
DISCUSSION = re.compile(r"discussion_r(\d+)")


def review_marker(round_id: str | None) -> str:
    """The hidden line that says which review summary is this round's: the payload's commit,
    so a second round on the same PR gets its own instead of rewriting the first's."""
    return f"<!-- hr:review:{round_id[:ROUND_LEN]} -->" if round_id else REVIEW_MARKER
PILES = {"fixed": "F", "ignored": "I", "assumption": "A"}
PILE_OF_KEY = {"autofixes": "fixed", "findings": "ignored", "assumptions": "assumption"}
#: What every comment on the PR opens with, so its reader knows before the first word who is
#: speaking: the coding agent unsure of its own reading (an assumption), or a reviewer agent
#: against the coding agent — the finding still open, or already fixed.
PREFIX = {"assumption": "🤖💭🤷‍♂️", "ignored": "🤖⚔️🤖❌", "fixed": "🤖⚔️🤖✅"}
PILE_OF_LETTER = {letter: pile for pile, letter in PILES.items()}
#: The single emoji the bodies used to open with, before PREFIX — replaced, not stacked.
OLD_LEAD = re.compile(r"^(?:🛠|🔴|💭)\s*")


def prefixed(body: str, pile: str) -> str:
    """`body` opening with its pile's PREFIX. Applied at push time to every comment, the
    agent-written payload's as much as `comment_body`'s, so no wording path can skip it."""
    lead = PREFIX.get(pile)
    if not lead or body.startswith(lead):
        return body
    return f"{lead} {OLD_LEAD.sub('', body)}"
SLUG_MAX = 48
BODY_MAX = 700          # a PR comment is a pointer to the page, not a copy of it
BATCH = 15              # line comments per *create a review* call — 41 in one timed out
GAP = 1.0               # seconds between two mutative calls (GitHub's best practices)
SETTLE_TRIES = 10       # polls after a 5xx/timeout before deciding what it left behind
SETTLE_WAIT = 3.0       # seconds between those polls
MAX_REPLANS = 3         # 5xx/timeouts survived in one push before it stops and says so
#: The last line of `--dry-run` and of a push: one JSON object the page reads, so it never
#: has to show — or parse — anything else the script prints.
PREVIEW_TAG = "::hr-push-preview::"
RESULT_TAG = "::hr-push-result::"


def more_marker(round_id: str | None) -> str:
    """The hidden line of a review that continues a round's summary in another batch."""
    return f"<!-- hr:review-more:{round_id[:ROUND_LEN]} -->" if round_id else \
        "<!-- hr:review-more -->"


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #

def slug(title: str) -> str:
    """`vet_id is ON DELETE SET NULL, so…` → `vet-id-is-on-delete-set-null-so-deleting-a-vet`.

    Tags and entities are removed first so the page, which holds the title as HTML
    (`<code>…</code>`, `&quot;`), and the payload, which holds it as markdown, agree."""
    text = html.unescape(re.sub(r"<[^>]+>", "", title)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(s) > SLUG_MAX:
        s = s[:SLUG_MAX].rsplit("-", 1)[0]
    return s or "item"


def hr_id(pile: str, title: str) -> str:
    return f"{PILES[pile]}:{slug(title)}"


def marker(cid: str) -> str:
    return f"<!-- hr:{cid} -->"


# --------------------------------------------------------------------------- #
# the diff GitHub will accept comments on
# --------------------------------------------------------------------------- #

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> dict[str, list[tuple[int, int]]]:
    """`{path: [(first, last) right-side line of each hunk]}` for every file that still
    exists on the right. Those ranges — added lines *and* their context — are exactly the
    lines GitHub lets a `side: RIGHT` comment sit on; deleted files map to nothing."""
    files: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            if path is not None:
                files.setdefault(path, [])
        elif path is not None:
            m = HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                if count:
                    files[path].append((start, start + count - 1))
    return files


def hunk_of(ranges: list[tuple[int, int]], line: int) -> int | None:
    for i, (a, b) in enumerate(ranges):
        if a <= line <= b:
            return i
    return None


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


_RP = None


def _rp():
    """`review-points.py`, loaded once: its hunk mapping is the page's, and one copy of it
    is how the page and the PR stop disagreeing about which line a ref is."""
    global _RP
    if _RP is None:
        _RP = _load_points_parser()
    return _RP


@dataclass
class Diff:
    head: str
    base: str
    files: dict[str, list[tuple[int, int]]]
    reader: object = None          # (path) -> list[str] | None, the file at `head`
    root: Path | None = None       # the clone `carry` asks git in
    #: (path, line, frm) -> (line at head | None, carried?) — set by the tests; git otherwise
    carrier: object = None

    def lines(self, path: str) -> list[str] | None:
        return self.reader(path) if self.reader else None

    def lines_at(self, path: str, rev: str | None) -> list[str] | None:
        """The file as it was at `rev` — where a comment's `anchor` was copied from."""
        if not rev or self.root is None:
            return None
        r = subprocess.run(["git", "-C", str(self.root), "show", f"{rev}:{path}"],
                           capture_output=True, text=True)
        return r.stdout.splitlines() if r.returncode == 0 else None

    def carry(self, path: str, line: int, frm: str | None) -> tuple[int | None, bool]:
        """`line` of `path` as written at `frm`, as it reads at `head`: `(line, True)`
        through the diff between the two, `(None, True)` when that diff removed it, and
        `(line, False)` when there is nothing to carry it through — no `frm`, `frm` is the
        head, or the file did not exist at `frm` (it cannot have been written there)."""
        if self.carrier is not None:
            return self.carrier(path, line, frm)
        if not frm or self.root is None:
            return line, False
        try:
            if git(self.root, "rev-parse", f"{frm}^{{commit}}").strip() == \
                    git(self.root, "rev-parse", f"{self.head}^{{commit}}").strip():
                return line, False
        except subprocess.CalledProcessError:
            return line, False
        if subprocess.run(["git", "-C", str(self.root), "cat-file", "-e", f"{frm}:{path}"],
                          capture_output=True).returncode != 0:
            return line, False
        hunks = _rp().file_hunks(self.root, path, frm, self.head)
        if hunks is None:
            return line, False
        return _rp().map_line(hunks, line), True


def load_diff(root: Path, base: str, head: str) -> Diff:
    mb = git(root, "merge-base", base, head).strip()
    files = parse_diff(git(root, "diff", "--no-color", "--no-ext-diff", "-U3", "-M",
                           f"{mb}", head))

    cache: dict[str, list[str] | None] = {}

    def read(path: str) -> list[str] | None:
        if path not in cache:
            try:
                cache[path] = git(root, "show", f"{head}:{path}").splitlines()
            except subprocess.CalledProcessError:
                cache[path] = None
        return cache[path]
    return Diff(head=git(root, "rev-parse", head).strip(), base=mb, files=files,
                reader=read, root=root)


# --------------------------------------------------------------------------- #
# resolving each comment against the diff as it is now
# --------------------------------------------------------------------------- #

@dataclass
class Resolved:
    cid: str
    mode: str                  # "line" | "file" | "body"
    comment: dict              # what is sent (without our private keys)
    note: str = ""             # why it moved or was downgraded; empty when as written
    title: str = ""
    pile: str = ""
    #: For a "body" item: the exact lines it is about, `(rev, start, end)` — `start` None
    #: for a whole file — and the permalink to them the summary quotes.
    lines: tuple | None = None
    permalink: str | None = None


def _find_anchor(lines: list[str], anchor: str, near: int) -> int | None:
    want = anchor.strip()
    if not want:
        return None
    hits = [i + 1 for i, l in enumerate(lines) if l.strip() == want]
    return min(hits, key=lambda n: abs(n - near)) if hits else None


def permalink(repo: str | None, rev: str, path: str, start: int | None = None,
              end: int | None = None) -> str | None:
    """The exact lines at one commit, on the forge (`…/blob/<sha>/<path>#L57-L59`). GitHub
    renders such a link, alone on its line in a comment, as the code it points at."""
    if not repo or not rev:
        return None
    return api_forge().of_slug(repo).blob_url(rev, path, start, end)


def _sha(diff: Diff, rev: str | None) -> str | None:
    if not rev or diff.root is None:
        return rev
    try:
        return git(diff.root, "rev-parse", f"{rev}^{{commit}}").strip()
    except subprocess.CalledProcessError:
        return rev


def resolve(c: dict, diff: Diff, at: str | None = None, repo: str | None = None) -> Resolved:
    """Where comment `c` goes on the PR as it is at `diff.head`. `at` is the commit its
    `line` was written against — the comment's own `at`, else the payload's `commit_id`."""
    cid = c.get("hr_id") or hr_id(c["pile"], c["title"])
    body = prefixed(c["body"].rstrip(), c.get("pile") or PILE_OF_LETTER.get(cid[:1], ""))
    if not MARKER.search(body):
        body = f"{body}\n\n{marker(cid)}"
    path = c["path"]
    frm = c.get("at") or at
    out = Resolved(cid=cid, mode="line", comment={}, title=c.get("title", ""),
                   pile=c.get("pile", ""))

    def summary(why: str, rev: str | None, a: int | None, b: int | None) -> Resolved:
        """Not on a line of the diff: quoted in the review's summary, with the lines."""
        rev = _sha(diff, rev) or diff.head
        out.mode, out.note = "body", why
        out.comment = {"path": path, "body": body}
        out.lines = (rev, a, b if a else None)
        out.permalink = permalink(repo, rev, path, a, b)
        return out

    ranges = diff.files.get(path)
    if c.get("subject_type") == "file" or not c.get("line"):
        if ranges is None:
            return summary(f"{path} is not in the PR's diff — quoted in the review's summary",
                           diff.head if diff.lines(path) is not None else frm, None, None)
        out.mode = "file"
        out.comment = {"path": path, "body": body, "subject_type": "file"}
        return out

    line0, start0 = int(c["line"]), c.get("start_line")
    start0 = int(start0) if start0 else None
    anchor = (c.get("anchor") or "").strip()
    notes = []
    # The anchor is checked where it was copied from, before anything is carried: a payload
    # whose `line` is one off its own `anchor` is fixed at its own commit, by its own text.
    then = diff.lines_at(path, frm) if anchor else None
    if then is not None and not (0 < line0 <= len(then) and then[line0 - 1].strip() == anchor):
        found = _find_anchor(then, anchor, line0)
        if found is not None:
            notes.append(f"{path}:{line0} at {str(frm)[:8]} does not read the anchor; "
                         f"{found} does")
            start0 = start0 + found - line0 if start0 else None
            line0 = found
    line, carried = diff.carry(path, line0, frm)
    start = None
    if start0:
        start, _ = diff.carry(path, start0, frm)
    if carried and line is not None and line != line0:
        notes.append(f"carried {path}:{start0 or line0}" + (f"-{line0}" if start0 else "")
                     + f" from {str(frm)[:8]} to {start or line}"
                     + (f"-{line}" if start else "") + f" at {diff.head[:8]}")
    lines = diff.lines(path)
    if anchor and lines is not None:
        here = lines[line - 1].strip() if line and 0 < line <= len(lines) else None
        if here != anchor:
            moved = _find_anchor(lines, anchor, line or line0)
            if line is None and moved is not None:
                notes.append(f"the diff since {str(frm)[:8]} removed {path}:{line0}; its "
                             f"text is at {moved} now")
                line, start = moved, (moved - (line0 - start0) if start0 else None)
            elif not carried and moved is not None:
                notes.append(f"moved {path}:{line0} → {moved} (the anchor text moved)")
                if start0:
                    start = start0 + moved - line0
                line = moved
            elif not carried and line is not None:
                return summary("; ".join(notes + [
                    f"{path}:{line0} no longer reads {anchor[:50]!r} — quoted in the "
                    "review's summary, at the lines it was written against"]),
                    frm or diff.head, start0 or line0, line0)
            elif line is not None:
                # The diff placed it: the line was rewritten there, which is the finding's
                # line still. The text is a cross-check, not a vote against the diff.
                notes.append(f"{path}:{line} was rewritten since {str(frm)[:8]} "
                             f"(it read {anchor[:40]!r})")
    if line is None:
        return summary("; ".join(notes + [f"the diff since {str(frm)[:8]} removed "
                                          f"{path}:{line0} — quoted in the review's summary,"
                                          " at the lines it was written against"]),
                       frm, start0 or line0, line0)
    if start is not None and start > line:
        start = None
    if ranges is None:
        return summary("; ".join(notes + [f"{path} is not in the PR's diff — quoted in the "
                                          "review's summary"]),
                       diff.head, start or line, line)
    h = hunk_of(ranges, line)
    if h is None:
        return summary("; ".join(notes + [f"{path}:{line} is outside the diff's hunks — "
                                          "quoted in the review's summary"]),
                       diff.head, start or line, line)
    comment = {"path": path, "line": line, "side": "RIGHT", "body": body}
    if start and start < line:
        if hunk_of(ranges, start) == h:
            comment = {"path": path, "start_line": start, "start_side": "RIGHT",
                       "line": line, "side": "RIGHT", "body": body}
        else:
            notes.append(f"start_line {start} is in another hunk — narrowed to line {line}")
    out.comment = comment
    out.note = "; ".join(notes)
    return out


# --------------------------------------------------------------------------- #
# the payload file
# --------------------------------------------------------------------------- #

class BadPayload(Exception):
    pass


def validate(payload: dict) -> list[str]:
    problems = []
    if not isinstance(payload.get("comments"), list):
        return ["`comments` must be a list"]
    seen: set[str] = set()
    for i, c in enumerate(payload["comments"]):
        where = f"comments[{i}]"
        if not isinstance(c, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in ("path", "body"):
            if not c.get(key):
                problems.append(f"{where} has no `{key}`")
        if not c.get("hr_id"):
            if c.get("pile") not in PILES or not c.get("title"):
                problems.append(f"{where} needs `hr_id`, or `pile` ({'|'.join(PILES)}) and "
                                "`title` (the `###` title verbatim) to derive it from")
                continue
        cid = c.get("hr_id") or hr_id(c["pile"], c["title"])
        if cid in seen:
            problems.append(f"{where} repeats id {cid} — two items with one title")
        seen.add(cid)
        if c.get("subject_type") != "file":
            if not isinstance(c.get("line"), int):
                problems.append(f"{where} ({cid}) has no integer `line` — give one, or "
                                "`subject_type: \"file\"`")
            if c.get("start_line") is not None and not isinstance(c["start_line"], int):
                problems.append(f"{where} ({cid}) has a non-integer `start_line`")
            if c.get("side", "RIGHT") != "RIGHT":
                problems.append(f"{where} ({cid}) comments the LEFT side — anchor on the "
                                "new code, `side: \"RIGHT\"`")
    if payload.get("event", "COMMENT") != "COMMENT":
        problems.append("`event` must be COMMENT — this is a record, not an approval")
    return problems


def load_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    problems = validate(payload)
    if problems:
        raise BadPayload("\n".join(problems))
    return payload


# --------------------------------------------------------------------------- #
# plan: what to send, given what is already on the PR
# --------------------------------------------------------------------------- #

@dataclass
class Call:
    method: str
    path: str
    payload: dict
    why: str
    #: The markers this call puts on the PR — what `settle` waits for after a 5xx.
    expect: list = field(default_factory=list)

    def shell(self) -> str:
        return PR_API.shell(self.method, self.path, self.payload)


@dataclass
class Plan:
    calls: list[Call] = field(default_factory=list)
    resolved: list[Resolved] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    marker: str = REVIEW_MARKER        # the hidden line that names this round's summary
    #: Per item: "new" (not on GitHub yet), "update" (there, body edited), "move" (there,
    #: on other lines: deleted and posted again) or "same" (there as it should be).
    fate: dict = field(default_factory=dict)

    def to_send(self) -> list[Resolved]:
        return [r for r in self.resolved if self.fate.get(r.cid) != "same"]


def _face(r: Resolved) -> str:
    rev, a, b = r.lines or (None, None, None)
    return r.comment["path"] + (f":{a}" + (f"-{b}" if b and b != a else "") if a else "")


#: The merge risk's door and the blast radius's size, as the page draws them
#: (`hrbuild/tabs/review.py`: MERGE_DOORS, BLAST_GLYPHS, BLAST_SIZES — a test holds the two
#: copies equal). Here, because this script runs without the page's package.
MERGE_DOORS = {"one-way": "\u26a0\ufe0f One-way door", "two-way": "\u2705 Two-way door"}
BLAST_GLYPHS = ("\u25cb", "\u25d4", "\u25d1", "\u25d5", "\u25cf")
BLAST_SIZES = ("barely anything", "about a quarter of the system", "about half the system",
               "about three quarters of the system", "the whole system")
CONTENT_JSON = "content.json"


def merge_risk_md(risk: dict | None) -> str:
    """`mergeRisk` as the review summary's line on the PR, the page's MERGE RISK line in
    markdown: the door, the blast radius behind its circle — with the size in words, since
    GitHub has no hover to say it — and why. Empty without a known door, like the page."""
    risk = risk if isinstance(risk, dict) else {}
    door = MERGE_DOORS.get(str(risk.get("door") or "").strip().lower())
    if not door:
        return ""
    parts = [f"**{door}**"]
    blast = str(risk.get("blast") or "").strip()
    raw = risk.get("blastSize")
    try:
        n = None if isinstance(raw, bool) else int(str(raw).strip())
    except (TypeError, ValueError):
        n = None
    if n is not None and not 0 <= n < len(BLAST_GLYPHS):
        n = None
    if n is not None:
        parts.append(f"blast radius: {BLAST_GLYPHS[n]} "
                     + (f"**{blast}** ({BLAST_SIZES[n]})" if blast else f"**{BLAST_SIZES[n]}**"))
    elif blast:
        parts.append(f"blast radius: **{blast}**")
    why = str(risk.get("why") or "").strip()
    return "**Merge risk** " + " · ".join(parts) + (f" — {why}" if why else "")


def attach_merge_risk(payload: dict, file: Path) -> dict:
    """The page's `mergeRisk`, from the `content.json` beside the payload, onto the payload
    — unless the payload already carries one. Read at push time, so the summary on the PR
    says what the page says without the agent writing it twice."""
    if isinstance(payload, dict) and "mergeRisk" not in payload:
        try:
            risk = json.loads((file.parent / CONTENT_JSON).read_text(encoding="utf-8")) \
                .get("mergeRisk")
        except (OSError, ValueError, AttributeError):
            risk = None
        if isinstance(risk, dict):
            payload["mergeRisk"] = risk
    return payload


def review_body(payload: dict, folded: list[Resolved]) -> str:
    """The review's own body: the payload's line, the merge risk's, then every item GitHub
    could not take on a line of the diff, each with a permalink to its exact lines on a line
    of its own — which GitHub renders as the code. Ends with this round's marker."""
    body = (payload.get("body") or "").rstrip()
    risk = merge_risk_md(payload.get("mergeRisk"))
    if risk:
        body = f"{body}\n\n{risk}".lstrip()
    if folded:
        body += ("\n\n**Not on a line of this diff** — GitHub takes a review comment only on "
                 "a line the PR changed or shows around a change, so these are quoted here, "
                 "each with the exact lines:\n\n" + "\n\n---\n\n".join(
                     f"**`{_face(r)}`** — {MARKER.sub('', r.comment['body']).strip()}"
                     + (f"\n\n{r.permalink}" if r.permalink else "")
                     for r in folded))
    return f"{body}\n\n{review_marker(payload.get('commit_id'))}".lstrip()


def in_summary(r: Resolved, review_body_text: str) -> bool:
    """Whether a folded item is quoted in a review's body as this push would quote it."""
    text = MARKER.sub("", r.comment["body"]).strip()
    return f"**`{_face(r)}`** — {text}" in review_body_text


def _place(c: dict) -> tuple:
    """Where a comment sits, comparably for one GitHub lists and one about to be sent. A
    comment GitHub has outdated has no `line` any more, so it never matches a live place."""
    if c.get("subject_type") == "file":
        return (c.get("path"), "file")
    return (c.get("path"), "line", c.get("start_line") or None, c.get("line"))


def _where(c: dict) -> str:
    if c.get("subject_type") == "file":
        return f"{c.get('path')} (the file)"
    s, e = c.get("start_line"), c.get("line")
    return f"{c.get('path')}:{s}-{e}" if s else f"{c.get('path')}:{e}"


def owned_ids(record: dict | None) -> set[int]:
    """The comment ids the previous push's record says this pipeline posted — the only ones
    a push may delete to post again on other lines."""
    out = set()
    for c in ((record or {}).get("comments") or {}).values():
        m = DISCUSSION.search(str((c or {}).get("html_url") or ""))
        if m:
            out.add(int(m.group(1)))
    return out


def plan(payload: dict, diff: Diff, repo: str, pr: int,
         existing: list[dict], reviews: list[dict], owned: set[int] | None = None) -> Plan:
    """Pure: the calls a push would make. `existing` is the PR's review comments and
    `reviews` its reviews, as GitHub lists them; `owned` the comment ids the previous push
    recorded (`owned_ids`), which are the only ones it may delete and post again."""
    owned = owned or set()
    p = Plan()
    p.resolved = [resolve(c, diff, payload.get("commit_id"), repo) for c in payload["comments"]]
    by_marker: dict[str, dict] = {}
    for e in existing:
        m = MARKER.search(e.get("body") or "")
        if not m:
            continue
        had = by_marker.get(m.group(1))
        # This pipeline's own comment over a stranger's with the same title; the newest of
        # two equals.
        if had is None or ((e["id"] in owned, e["id"]) > (had["id"] in owned, had["id"])):
            by_marker[m.group(1)] = e
    mark = p.marker = review_marker(payload.get("commit_id"))
    ours = next((r for r in reviews if mark in (r.get("body") or "")), None)

    new_lines, folded, deletes = [], [], []
    for r in p.resolved:
        if r.note:
            p.notes.append(f"{r.cid}: {r.note}")
        old = by_marker.get(r.cid)
        if old is not None:
            moved = r.mode == "body" or _place(old) != _place(r.comment)
            # This round has no summary yet, so whatever an earlier push of it posted sits
            # under no summary (or under another round's): gathered into the new review,
            # so one review holds the round — its summary and every line of it.
            gather = not moved and ours is None and old["id"] in owned
            if (moved or gather) and old["id"] in owned:
                there = ("in the review's summary" if r.mode == "body"
                         else f"at {_where(r.comment)}"
                         + (", in this round's review" if gather else ""))
                deletes.append(Call("DELETE", PR_API.comment_path(repo, old['id']), {},
                                    f"delete {r.cid}: at {_where(old)}, it belongs {there}"))
                p.notes.append(f"{r.cid}: posted at {_where(old)}; deleted and posted again "
                               + there)
                p.fate[r.cid] = "move"
                old = None
            elif moved:
                p.notes.append(f"{r.cid}: posted earlier at {_where(old)}, which is not in "
                               "this pipeline's record of its own comments — left where it "
                               "is (GitHub cannot move a comment)")
        if old is not None:
            if (old.get("body") or "").strip() != r.comment["body"].strip():
                p.calls.append(Call("PATCH", PR_API.comment_path(repo, old['id']),
                                    {"body": r.comment["body"]}, f"update {r.cid}"))
                p.fate[r.cid] = "update"
            else:
                p.notes.append(f"{r.cid}: already posted, unchanged")
                p.fate[r.cid] = "same"
            continue
        p.fate.setdefault(r.cid, "new")
        if r.mode == "body":
            folded.append(r)
            if ours is not None and in_summary(r, ours.get("body") or ""):
                p.fate[r.cid] = "same"
        elif r.mode == "file":
            p.calls.append(Call("POST", PR_API.comments_path(repo, pr),
                                {**r.comment, "commit_id": diff.head}, f"file comment {r.cid}",
                                expect=[r.cid]))
        else:
            new_lines.append(r)

    body = review_body(payload, folded)
    if ours is not None:
        if (ours.get("body") or "").strip() != body.strip():
            p.calls.append(Call("PUT", PR_API.review_path(repo, pr, ours['id']),
                                {"body": body}, "update the review's body"))
    # In reviews of BATCH: one review with 41 comments outlived GitHub's 10 s and came back
    # 504 while it was being created anyway. The first batch carries the summary when this
    # round has none yet; every other one says it continues it.
    batches = [new_lines[i:i + BATCH] for i in range(0, len(new_lines), BATCH)] or \
        ([[]] if ours is None else [])
    more = more_marker(payload.get("commit_id"))
    for k, chunk in enumerate(batches):
        first = k == 0 and ours is None
        p.calls.append(Call("POST", PR_API.reviews_path(repo, pr), {
            "commit_id": diff.head,
            "event": "COMMENT",
            "body": body if first else
            f"Review record, continued — {len(chunk)} more comment"
            f"{'' if len(chunk) == 1 else 's'} on the lines they are about.\n\n{more}",
            "comments": [r.comment for r in chunk],
        }, f"create a review with {len(chunk)} line comment(s)"
           + (f" (part {k + 1} of {len(batches)})" if len(batches) > 1 else ""),
            expect=[r.cid for r in chunk]))
    # Last: a comment is deleted only once its replacement has been accepted.
    p.calls.extend(deletes)
    return p


# --------------------------------------------------------------------------- #
# what a failed call means (the calls themselves: `PR_API`, hrbuild/shared/forge.py)
# --------------------------------------------------------------------------- #

def is_transient(e: Exception) -> bool:
    return getattr(e, "transient", None) if isinstance(e, GhError) else \
        bool(TRANSIENT.search(str(e)))


def plain_reason(e: Exception) -> str:
    """What went wrong, in the reader's words — never the command line."""
    text = str(e)
    status = getattr(e, "status", None)
    if is_transient(e):
        return "GitHub timed out"
    if status == 422:
        return "GitHub refused a comment (a line it does not show in the diff?)"
    if status in (401, 403) and "rate limit" not in text.lower():
        return "GitHub did not accept your gh login (run `gh auth status`)"
    if "rate limit" in text.lower() or status == 429:
        return "GitHub asked us to slow down"
    if status == 404:
        return "GitHub could not find the pull request"
    return "GitHub refused the request"


def _markers(comments: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in sorted(comments, key=lambda e: e.get("id") or 0):
        m = MARKER.search(e.get("body") or "")
        if m:
            out.setdefault(m.group(1), []).append(e)
    return out


def settle(gh, repo: str, pr: int, call: Call, sleep=time.sleep,
           tries: int = SETTLE_TRIES, wait: float = SETTLE_WAIT) -> int:
    """After `call` came back as a 5xx or a timeout: wait until what it may have made anyway
    has stopped appearing, and return how many of its comments are on the PR.

    PR #51's review answered 504 and was created all the same, its comments showing up over
    the next seconds — so "is it there?" asked once, at once, would have said "30 of 41" and
    a retry would have posted the other 11 twice. Done when every comment the call carried is
    there, or when the count has not moved for two polls in a row."""
    want = set(call.expect)
    if not want:
        sleep(wait)
        return 0
    last, still = -1, 0
    seen = 0
    for _ in range(tries):
        sleep(wait)
        try:
            seen = len(want & set(_markers(gh.list(PR_API.comments_path(repo, pr)))))
        except RuntimeError:
            continue
        if seen == len(want):
            return seen
        still = still + 1 if seen == last else 0
        if still >= 2:
            return seen
        last = seen
    return seen


def execute(p: Plan, gh, repo: str, pr: int, posted_path: Path, meta: dict,
            replan=None, sleep=time.sleep) -> dict:
    """Send the plan's calls one by one, `GAP` apart, and record what is on GitHub after —
    on every outcome, so the page shows the truth.

    A 5xx or a timeout is not retried as it was: `settle` waits for what that call may
    have made anyway, then `replan()` (the PR listed again, the payload planned against it)
    yields only what is still missing. Any other refusal stops the push. Either way the
    record says `partial`, how many are on the PR, and why, in plain words."""
    calls = list(p.calls)
    error: Exception | None = None
    replans = 0
    sent = 0
    while calls:
        call = calls.pop(0)
        try:
            gh.send(call)
            sent += 1
        except RuntimeError as e:
            if not is_transient(e) or replan is None or replans >= MAX_REPLANS:
                error = e
                break
            replans += 1
            print(f"  ! {call.why}: {plain_reason(e)} — checking what GitHub made anyway",
                  file=sys.stderr)
            settle(gh, repo, pr, call, sleep)
            try:
                p2 = replan()
            except RuntimeError as e2:
                error = e2
                break
            calls = list(p2.calls)
            print(f"  · {len(calls)} call(s) left after looking again", file=sys.stderr)
            continue
        if calls:
            sleep(GAP)
    return record_state(p, gh, repo, pr, posted_path, meta, error=error, sent=sent)


def record_state(p: Plan, gh, repo: str, pr: int, posted_path: Path, meta: dict,
                 error: Exception | None = None, sent: int = 0) -> dict:
    """What is on the PR now, item by item, written to `pr-comments.posted.json`."""
    try:
        after = gh.list(PR_API.comments_path(repo, pr))
        reviews = gh.list(PR_API.reviews_path(repo, pr))
    except RuntimeError as e:
        after, reviews = [], []
        error = error or e
    marks = _markers(after)
    review = next((r for r in reviews if p.marker in (r.get("body") or "")), None)
    record = {
        **meta,
        "pushed_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "review_url": (review or {}).get("html_url"),
        "review_marker": p.marker,
        "comments": {},
    }
    present, missing = 0, []
    for r in p.resolved:
        if r.mode == "body":
            there = review is not None and in_summary(r, review.get("body") or "")
            e = {}
        else:
            e = (marks.get(r.cid) or [{}])[-1]          # the newest: what this push posted
            there = bool(e.get("html_url"))
        present += there
        if not there:
            missing.append(r.cid)
        rev, a, b = r.lines or (None, None, None)
        start = e.get("start_line") or r.comment.get("start_line") or (a if b and a != b else None)
        # `line` and `commit_id` together: the lines are lines of that commit, whether the
        # comment sits on them or the summary quotes them.
        record["comments"][r.cid] = {
            "pile": r.pile, "title": r.title, "mode": r.mode,
            "html_url": e.get("html_url") or (record["review_url"] if r.mode == "body"
                                               and there else None),
            "path": r.comment.get("path"),
            "line": e.get("line") or r.comment.get("line") or b,
            **({"start_line": start} if start else {}),
            "commit_id": e.get("commit_id") or rev or meta.get("head"),
            **({"permalink": r.permalink} if r.permalink else {}),
        }
    ours = {r.cid for r in p.resolved}
    dups = sorted(cid for cid, es in marks.items() if cid in ours and len(es) > 1)
    total = len(p.resolved)
    record.update(expected=total, present=present, missing=missing, duplicates=dups,
                  sent=sent, status="complete" if present == total and error is None
                  else "partial")
    if record["status"] == "complete":
        msg = (f"All {total} comments are on GitHub PR #{pr}." if sent else
               f"All {total} comments were already on GitHub PR #{pr}.")
    else:
        why = plain_reason(error) if error else "Not everything reached GitHub"
        left = total - present
        msg = (f"{why} — {present} of {total} posted; click Retry to post the remaining "
               f"{left}." if left else f"{why} — all {total} are on GitHub, but the last "
               "step failed; click Retry.")
        record["error"] = str(error) if error else None
    if dups:
        msg += (f" {len(dups)} appear more than once on GitHub — left as they are; "
                "delete the extra copies by hand.")
    record["message"] = msg
    posted_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def preview(p: Plan, pr: int, repo: str, url: str | None, user: str | None) -> dict:
    """What `--dry-run` hands the page: the comments a push would post or edit, readable —
    where, which item, the text — and nothing about how they are sent."""
    items = []
    for r in p.to_send():
        rev, a, b = r.lines or (None, None, None)
        where = (_where(r.comment) if r.mode != "body"
                 else _face(r) + " (in the review's summary)")
        items.append({"id": r.cid, "fate": p.fate.get(r.cid, "new"), "where": where,
                      "pile": r.pile, "title": r.title,
                      "text": MARKER.sub("", r.comment["body"]).strip()})
    return {"pr": pr, "repo": repo, "url": url, "user": user, "total": len(p.resolved),
            "already": len(p.resolved) - len(items), "items": items,
            "calls": len(p.calls)}


# --------------------------------------------------------------------------- #
# the fallback: a payload out of review-points.md, deterministically
# --------------------------------------------------------------------------- #

TOKEN = re.compile(r"\{\{(?:snippet|diff|difflink):([^}|@]+)[^}]*\}\}")


def _load_points_parser():
    spec = importlib.util.spec_from_file_location("hr_review_points", HERE / "review-points.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.inline = lambda text: text          # keep the markdown: GitHub renders it, not us
    return mod


def _short(text: str, limit: int) -> str:
    text = TOKEN.sub(lambda m: f"`{m.group(1).strip()}`", text or "").strip()
    para = re.split(r"\n\s*\n", text, 1)[0].strip()
    para = re.sub(r"\s*\n\s*", " ", para)
    if len(para) > limit:
        para = para[:limit].rsplit(" ", 1)[0].rstrip(",;:—-") + " …"
    return para


def comment_body(pile: str, item: dict, fixed_sha: str | None, page: str | None) -> str:
    title = item["title"]
    by = item.get("source")
    if pile == "fixed":
        head = f"**Auto-fixed**{f' in {fixed_sha}' if fixed_sha else ''} — {title}"
        parts = [head, _short(item.get("body", ""), BODY_MAX)]
    elif pile == "ignored":
        sev = item.get("severity", "info")
        head = f"**Open issue** · {sev} · declined by the author — {title}"
        parts = [head]
        if item.get("why"):
            parts.append(f"**Why declined:** {_short(item['why'], 300)}")
        parts.append(_short(item.get("body", ""), BODY_MAX - 300))
    else:
        conf = item.get("confidence")
        head = f"**Assumption**{f' · confidence {conf:.2f}' if conf is not None else ''} — {title}"
        parts = [head]
        if item.get("alternative"):
            parts.append(f"**Not taken:** {_short(item['alternative'], 300)}")
        if item.get("why"):
            parts.append(f"**Why:** {_short(item['why'], 300)}")
    foot = " · ".join(x for x in (f"raised by {by}" if by and pile != "assumption" else "",
                                  f"[review page]({page})" if page else "") if x)
    if foot:
        parts.append(f"<sub>{foot}</sub>")
    return "\n\n".join(p for p in parts if p)


def anchor_commit(root: Path, points_file: str) -> str:
    """The commit `review-points.md`'s `file:line`s are lines of: the commit that recorded
    its items (`review-points.py:recorded_in`, which skips a commit that only moved the
    front-matter), or the one the front-matter names, else HEAD.

    Not `review-commits.json`'s `review`: visit-has-vet's payload was written while the
    review commit was being made, when that file still named `ce56d912` — the previous
    round's review commit, still an ancestor after the revert — and every comment was then
    anchored on that round's code."""
    rp = _rp()
    try:
        front = rp.parse((root / points_file).read_text(encoding="utf-8"))["front"]
    except (OSError, ValueError, KeyError):
        return "HEAD"
    rev = rp.provenance(front, root, points_file).get("reviewCommit")
    if rev and subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                               f"{rev}^{{commit}}"], capture_output=True).returncode == 0:
        return rev
    return "HEAD"


def from_review_points(root: Path, points_file: str, at: str | None = None) -> dict:
    """The payload, out of the record. Every `line` is a line of `at` (default: the commit
    the record's items were written in, `anchor_commit`), which becomes `commit_id`."""
    rp = _load_points_parser()
    text = (root / points_file).read_text(encoding="utf-8")
    doc = rp.parse(text)
    front = doc["front"]
    at = at or anchor_commit(root, points_file)
    try:
        at_sha = git(root, "rev-parse", "--short=8", at).strip()
    except subprocess.CalledProcessError:
        at_sha = at
    comments, counts = [], {"fixed": 0, "ignored": 0, "assumption": 0}
    for key, pile in (("autofixes", "fixed"), ("findings", "ignored"), ("assumptions", "assumption")):
        for item in doc["piles"][key]:
            refs = item.get("refs") or []
            if not refs:
                continue
            counts[pile] += 1
            fixed = item.get("_fixed_in") or front.get("fixed-in")
            fixed_sha = at_sha if (fixed or "").upper() == "HEAD" else (fixed or None)
            ref = refs[0]
            m = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", ref)
            c: dict = {"hr_id": hr_id(pile, item["title"]), "pile": pile, "title": item["title"]}
            if m:
                path, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
                c.update(path=path, line=b, side="RIGHT")
                if b > a:
                    c["start_line"] = a
                try:
                    lines = git(root, "show", f"{at}:{path}").splitlines()
                    if 0 < b <= len(lines):
                        c["anchor"] = lines[b - 1].strip()
                except subprocess.CalledProcessError:
                    pass
            else:
                c.update(path=ref, subject_type="file")
            c["body"] = comment_body(pile, item, fixed_sha, None)
            comments.append(c)
    summary = (f"**Review record** from `{points_file}`"
               + (f" ({front['reviewers']})" if front.get("reviewers") else "")
               + f": {counts['fixed']} auto-fixed · {counts['ignored']} declined and still open"
               f" · {counts['assumption']} assumptions. Each is on the line it is about, or "
               "quoted below with its lines when that line is not in the diff.")
    return {"version": 1, "commit_id": git(root, "rev-parse", at).strip(), "event": "COMMENT",
            "body": summary, "comments": comments,
            "generated_by": "push-pr-comments.py --from-review-points"}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def stale_reason(root: Path, payload, points: str = "review-points.md") -> str | None:
    """Why the payload is not this record's, or None when it is.

    Run 6 carried a `pr-comments.json` pinned to `0746abc5`, a commit of the previous run's
    branch: its anchors and quoted lines were written for code this branch never had, and
    a push would have pinned them onto this PR. The commit has to be an ancestor of HEAD.

    And not older than the record: visit-has-vet's was pinned to `ce56d912`, the previous
    round's review commit — on this branch, so it passed — while `review-points.md` had been
    re-recorded at `be762955`. Its lines were the old round's lines of the old round's code."""
    cid = str((payload or {}).get("commit_id") or "").strip() if isinstance(payload, dict) \
        else ""
    if not cid:
        return None
    r = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", cid, "HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 1:
        return f"its commit {cid[:8]} is not on this branch (not an ancestor of HEAD)"
    if r.returncode != 0:
        return f"its commit {cid[:8]} is not in this clone"
    if not (root / points).is_file():
        return None
    rec = anchor_commit(root, points)
    if rec == "HEAD":
        return None
    same = subprocess.run(["git", "-C", str(root), "rev-parse", cid, rec],
                          capture_output=True, text=True).stdout.split()
    if len(same) == 2 and same[0] != same[1] and subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", cid, rec],
            capture_output=True).returncode == 0:
        return (f"its commit {cid[:8]} is older than {rec[:8]}, where {points} was recorded "
                "— its lines are an earlier round's")
    return None


def drop_stale(root: Path, file: Path, points: str, at: str | None) -> str | None:
    """Rebuild `file` from the record when its commit is not on this branch, or delete it
    when there is no record to rebuild it from. Returns what was done, None when the payload
    is current (or absent)."""
    if not file.is_file():
        return None
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except ValueError:
        payload = {"commit_id": "not-json"}
    why = stale_reason(root, payload, points)
    if not why:
        return None
    if (root / points).is_file():
        rev = at or anchor_commit(root, points)
        if stale_reason(root, {"commit_id": rev}, points):
            rev = "HEAD"
        fresh = from_review_points(root, points, rev)
        file.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return f"{file.name}: {why} — rebuilt from {points} at {fresh['commit_id'][:8]}"
    file.unlink()
    return f"{file.name}: {why} — dropped, there is no {points} to rebuild it from"


def summarize(p: Plan, out=None) -> None:
    out = out or sys.stdout
    modes = {"line": 0, "file": 0, "body": 0}
    for r in p.resolved:
        modes[r.mode] += 1
    print(f"{len(p.resolved)} comments: {modes['line']} on a line, {modes['file']} on a "
          f"file, {modes['body']} folded into the review body", file=out)
    for n in p.notes:
        print(f"  · {n}", file=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--file", default=DEFAULT_FILE, help=f"payload (default: {DEFAULT_FILE})")
    ap.add_argument("--pr", type=int, help="PR number (default: the current branch's PR)")
    ap.add_argument("--repo", help="owner/name (default: gh repo view)")
    ap.add_argument("--base", help="base ref for --check (default: origin/main)")
    ap.add_argument("--check", action="store_true",
                    help="validate the payload against HEAD's diff; no GitHub needed")
    ap.add_argument("--dry-run", action="store_true", help="print the gh calls; post nothing")
    ap.add_argument("--from-review-points", action="store_true",
                    help="write the payload out of review-points.md first")
    ap.add_argument("--points", default="review-points.md")
    ap.add_argument("--at", help="rev the refs in review-points.md were written at "
                                 "(default: the commit that recorded its items, else HEAD)")
    ap.add_argument("--drop-stale", action="store_true",
                    help="when the payload's commit is not on this branch, rebuild it from "
                         "review-points.md (or delete it when there is none); nothing else")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    file = root / a.file

    if a.drop_stale:
        done = drop_stale(root, file, a.points, a.at)
        print(done or f"{a.file}: current (or absent) — left as it is")
        return 0

    if a.from_review_points:
        payload = from_review_points(root, a.points, a.at)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {file.relative_to(root)}: {len(payload['comments'])} comments")
        if not (a.check or a.dry_run):
            return 0

    if not file.is_file():
        print(f"no {a.file} — the agent that wrote review-points.md did not prepare the "
              "comments; `--from-review-points` builds them out of the record", file=sys.stderr)
        return 3
    try:
        payload = load_payload(file)
    except (BadPayload, ValueError) as e:
        print(f"{a.file} is not a pushable payload:\n{e}", file=sys.stderr)
        return 2
    attach_merge_risk(payload, file)
    why = stale_reason(root, payload, a.points)
    if why:
        print(f"{a.file} is not this branch's: {why}. Its lines were written for other code; "
              "rebuild it with `push-pr-comments.py --drop-stale` (or --from-review-points)",
              file=sys.stderr)
        return 2

    gh = PR_API
    if a.check:
        diff = load_diff(root, a.base or "origin/main", "HEAD")
        p = plan(payload, diff, a.repo or "OWNER/REPO", a.pr or 0, [], [])
        summarize(p)
        return 0

    try:
        info = gh.pr(root, a.pr)
        repo = a.repo or gh.repo(root)
    except RuntimeError as e:
        print(f"cannot find the PR: {e}", file=sys.stderr)
        return 2
    pr, head = info["number"], info["headRefOid"]
    subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin", info["baseRefName"], head],
                   capture_output=True)
    try:
        diff = load_diff(root, f"origin/{info['baseRefName']}", head)
    except subprocess.CalledProcessError as e:
        print(f"cannot diff {info['baseRefName']}...{head[:8]}: {e.stderr}", file=sys.stderr)
        return 2
    local = git(root, "rev-parse", "HEAD").strip()
    if local != head:
        print(f"note: local HEAD {local[:8]} is not the PR head {head[:8]} — anchoring on the "
              "PR head, which is what GitHub shows", file=sys.stderr)
    try:
        existing = gh.list(PR_API.comments_path(repo, pr))
        reviews = gh.list(PR_API.reviews_path(repo, pr))
    except RuntimeError as e:
        if not a.dry_run:
            print(f"cannot list the PR's comments: {e}", file=sys.stderr)
            return 4
        print(f"note: could not list existing comments ({e}); assuming none", file=sys.stderr)
        existing, reviews = [], []
    posted_path = file.with_name(file.stem + POSTED_SUFFIX)
    try:
        before = json.loads(posted_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    # Only what the last push recorded as its own may be deleted and posted again; and only
    # on the PR that record is about.
    owned = owned_ids(before) if before.get("pr") in (None, pr) else set()
    p = plan(payload, diff, repo, pr, existing, reviews, owned)
    print(f"{info['url']} @ {head[:8]} (merge-base {diff.base[:8]})")
    summarize(p)
    if a.dry_run:
        print(f"\n{len(p.calls)} call(s) would be made:\n")
        for c in p.calls:
            print(f"# {c.why}\n{c.shell()}\n")
        # Last, on one line: what the page's confirmation shows — never the calls above.
        print(PREVIEW_TAG + " " + json.dumps(preview(p, pr, repo, info["url"], gh.login()),
                                             ensure_ascii=False))
        return 0

    def replan() -> Plan:
        return plan(payload, diff, repo, pr, gh.list(PR_API.comments_path(repo, pr)),
                    gh.list(PR_API.reviews_path(repo, pr)), owned)

    record = execute(p, gh, repo, pr, posted_path,
                     {"repo": repo, "pr": pr, "url": info["url"], "head": head,
                      # What it was pushed from: the page offers "re-publish" only when the
                      # payload on disk is no longer this file.
                      "payload_sha256": hashlib.sha256(file.read_bytes()).hexdigest()},
                     replan=replan)
    print(record["message"], file=sys.stderr if record["status"] != "complete" else sys.stdout)
    if record.get("error"):
        print(f"  ({record['error']})", file=sys.stderr)
    print(RESULT_TAG + " " + json.dumps(
        {k: record.get(k) for k in ("status", "expected", "present", "missing", "duplicates",
                                    "message", "review_url", "pr", "url")},
        ensure_ascii=False))
    return 0 if record["status"] == "complete" else 4


if __name__ == "__main__":
    sys.exit(main())
