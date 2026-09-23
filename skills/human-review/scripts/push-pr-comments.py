#!/usr/bin/env python3
"""Push the Review tab's three piles onto the pull request, as inline comments on the code.

The judgement is not made here. The agent that wrote `review-points.md` also writes
`.human-review/pr-comments.json` — the exact body of GitHub's *create a review* call
(`POST /repos/{owner}/{repo}/pulls/{n}/reviews`), one comment per item, each anchored on a
line of the diff and worded for the PR's own reader. This script only checks that payload
against the PR as it is *now* and sends it. No model, no rewording, no choosing.

Why the anchors are re-checked at push time and not trusted: the payload was written at
the review commit, and the PR head has usually moved since (a retouch, a merge from
`main`). A comment on a line that is no longer in the diff is refused by GitHub with a 422
that fails the whole review, so every comment is resolved again, in order of preference:

  1. `line` still holds the `anchor` text and is in the diff          → posted as written
  2. the `anchor` text moved, and its new line is in the diff        → posted there ("moved")
  3. the file is in the diff but the line is not                     → a file-level comment
  4. the file is not in the diff at all (or was deleted)             → folded into the body

Every downgrade is printed. Nothing is dropped silently: a finding GitHub cannot pin to a
line still reaches the PR, one level up.

**Re-pushing updates, it never duplicates.** Each body ends with a hidden marker,
`<!-- hr:A:vet-id-is-on-delete-set-null -->` — the pile letter and a slug of the item's
`###` title — and before posting the script lists the PR's review comments and PATCHes the
ones already carrying a marker instead of posting them again. The review's own body carries
`<!-- hr:review -->` and is updated in place the same way. (GitHub cannot move a comment:
one whose anchor moved keeps its first position, and the script says so.)

After a push, `.human-review/pr-comments.posted.json` records each marker's `html_url`, so
the page can put a link to its GitHub comment beside every item.

`--from-review-points` writes the payload deterministically out of `review-points.md` —
for a branch whose agent predates this file. It is the fallback, not the flow: the agent's
own payload is shorter, better anchored and worded for the PR, which is why it is asked for.

Usage:
  push-pr-comments.py --check                     # validate the payload against HEAD's diff
  push-pr-comments.py --dry-run                   # print the exact gh calls; post nothing
  push-pr-comments.py                             # post / update them on the branch's PR
  push-pr-comments.py --from-review-points        # (re)write the payload from review-points.md
  push-pr-comments.py --pr 49 --repo victorrentea/petclinic --dry-run

Exit codes: 0 ok · 2 bad payload / no PR · 3 no payload file · 4 a GitHub call failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FILE = ".human-review/pr-comments.json"
POSTED_SUFFIX = ".posted.json"
REVIEW_MARKER = "<!-- hr:review -->"
MARKER = re.compile(r"<!--\s*hr:([A-Za-z]:[a-z0-9-]+)\s*-->")
PILES = {"fixed": "F", "ignored": "I", "assumption": "A"}
PILE_OF_KEY = {"autofixes": "fixed", "findings": "ignored", "assumptions": "assumption"}
SLUG_MAX = 48
BODY_MAX = 700          # a PR comment is a pointer to the page, not a copy of it


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


@dataclass
class Diff:
    head: str
    base: str
    files: dict[str, list[tuple[int, int]]]
    reader: object = None          # (path) -> list[str] | None, the file at `head`

    def lines(self, path: str) -> list[str] | None:
        return self.reader(path) if self.reader else None


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
    return Diff(head=head, base=mb, files=files, reader=read)


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


def _find_anchor(lines: list[str], anchor: str, near: int) -> int | None:
    want = anchor.strip()
    if not want:
        return None
    hits = [i + 1 for i, l in enumerate(lines) if l.strip() == want]
    return min(hits, key=lambda n: abs(n - near)) if hits else None


def resolve(c: dict, diff: Diff) -> Resolved:
    cid = c.get("hr_id") or hr_id(c["pile"], c["title"])
    body = c["body"].rstrip()
    if not MARKER.search(body):
        body = f"{body}\n\n{marker(cid)}"
    path = c["path"]
    out = Resolved(cid=cid, mode="line", comment={}, title=c.get("title", ""),
                   pile=c.get("pile", ""))
    ranges = diff.files.get(path)
    if ranges is None:
        out.mode, out.note = "body", f"{path} is not in the PR's diff — folded into the review body"
        out.comment = {"path": path, "body": body}
        return out
    if c.get("subject_type") == "file" or not c.get("line"):
        out.mode = "file"
        out.comment = {"path": path, "body": body, "subject_type": "file"}
        return out

    line, start = int(c["line"]), c.get("start_line")
    start = int(start) if start else None
    anchor = c.get("anchor")
    lines = diff.lines(path)
    if anchor is not None and lines is not None:
        here = lines[line - 1].strip() if 0 < line <= len(lines) else None
        if here != anchor.strip():
            moved = _find_anchor(lines, anchor, line)
            if moved is None:
                out.mode = "file"
                out.note = f"{path}:{line} no longer reads {anchor.strip()[:50]!r} — posted on the file"
                out.comment = {"path": path, "body": body, "subject_type": "file"}
                return out
            out.note = f"moved {path}:{line} → {moved} (the anchor text moved)"
            if start:
                start += moved - line
            line = moved
    h = hunk_of(ranges, line)
    if h is None:
        out.mode = "file"
        out.note = (f"{path}:{line} is outside the diff's hunks — posted on the file"
                    + (f" ({out.note})" if out.note else ""))
        out.comment = {"path": path, "body": body, "subject_type": "file"}
        return out
    comment = {"path": path, "line": line, "side": "RIGHT", "body": body}
    if start and start < line:
        if hunk_of(ranges, start) == h:
            comment = {"path": path, "start_line": start, "start_side": "RIGHT",
                       "line": line, "side": "RIGHT", "body": body}
        else:
            out.note = (out.note + "; " if out.note else "") + \
                f"start_line {start} is in another hunk — narrowed to line {line}"
    out.comment = comment
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

    def shell(self) -> str:
        return (f"gh api -X {self.method} {self.path} --input - <<'JSON'\n"
                + json.dumps(self.payload, indent=2, ensure_ascii=False) + "\nJSON")


@dataclass
class Plan:
    calls: list[Call] = field(default_factory=list)
    resolved: list[Resolved] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def review_body(payload: dict, folded: list[Resolved]) -> str:
    body = (payload.get("body") or "").rstrip()
    if folded:
        body += "\n\n**Not on a line of this diff:**\n\n" + "\n\n---\n\n".join(
            f"`{r.comment['path']}` — {MARKER.sub('', r.comment['body']).strip()}"
            for r in folded)
    return f"{body}\n\n{REVIEW_MARKER}".lstrip()


def plan(payload: dict, diff: Diff, repo: str, pr: int,
         existing: list[dict], reviews: list[dict]) -> Plan:
    """Pure: the calls a push would make. `existing` is the PR's review comments and
    `reviews` its reviews, as GitHub lists them."""
    p = Plan()
    p.resolved = [resolve(c, diff) for c in payload["comments"]]
    by_marker: dict[str, dict] = {}
    for e in existing:
        m = MARKER.search(e.get("body") or "")
        if m and m.group(1) not in by_marker:
            by_marker[m.group(1)] = e
    ours = next((r for r in reviews if REVIEW_MARKER in (r.get("body") or "")), None)

    new_lines, folded = [], []
    for r in p.resolved:
        if r.note:
            p.notes.append(f"{r.cid}: {r.note}")
        if r.mode == "body":
            folded.append(r)
            continue
        old = by_marker.get(r.cid)
        if old is not None:
            if (old.get("body") or "").strip() != r.comment["body"].strip():
                p.calls.append(Call("PATCH", f"repos/{repo}/pulls/comments/{old['id']}",
                                    {"body": r.comment["body"]}, f"update {r.cid}"))
            else:
                p.notes.append(f"{r.cid}: already posted, unchanged")
            where_now = (r.comment.get("line"), r.comment["path"])
            where_then = (old.get("line"), old.get("path"))
            if old.get("line") is not None and where_now != where_then:
                p.notes.append(f"{r.cid}: posted earlier at {old.get('path')}:{old.get('line')};"
                               " GitHub cannot move a comment, so it stays there")
            continue
        if r.mode == "file":
            p.calls.append(Call("POST", f"repos/{repo}/pulls/{pr}/comments",
                                {**r.comment, "commit_id": diff.head}, f"file comment {r.cid}"))
        else:
            new_lines.append(r)

    body = review_body(payload, folded)
    if ours is not None:
        if (ours.get("body") or "").strip() != body.strip():
            p.calls.append(Call("PUT", f"repos/{repo}/pulls/{pr}/reviews/{ours['id']}",
                                {"body": body}, "update the review's body"))
    if new_lines or ours is None:
        p.calls.append(Call("POST", f"repos/{repo}/pulls/{pr}/reviews", {
            "commit_id": diff.head,
            "event": "COMMENT",
            "body": body if ours is None else
            f"{len(new_lines)} more from the same review record.\n\n<!-- hr:review-more -->",
            "comments": [r.comment for r in new_lines],
        }, f"create a review with {len(new_lines)} line comment(s)"))
    return p


# --------------------------------------------------------------------------- #
# GitHub, through gh
# --------------------------------------------------------------------------- #

class Gh:
    """Everything this script asks of GitHub goes through `gh api`, so the credentials are
    the reader's own and nothing here ever sees a token."""

    def run(self, args: list[str], data: dict | None = None) -> str:
        r = subprocess.run(["gh", *args], input=json.dumps(data) if data is not None else None,
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"gh {' '.join(args[:4])}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def list(self, path: str) -> list[dict]:
        out = self.run(["api", "--paginate", path, "--jq", ".[]"])
        return [json.loads(l) for l in out.splitlines() if l.strip()]

    def send(self, call: Call) -> dict:
        out = self.run(["api", "-X", call.method, call.path, "--input", "-"], call.payload)
        return json.loads(out) if out.strip() else {}

    def pr(self, root: Path, pr: int | None) -> dict:
        args = ["pr", "view", *([str(pr)] if pr else []), "--json",
                "number,url,headRefOid,baseRefName,headRefName"]
        r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=root)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "no PR for this branch")
        return json.loads(r.stdout)

    def repo(self, root: Path) -> str:
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                            ".nameWithOwner"], capture_output=True, text=True, cwd=root)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()


def execute(p: Plan, gh, repo: str, pr: int, posted_path: Path, meta: dict) -> dict:
    for call in p.calls:
        gh.send(call)
    after = gh.list(f"repos/{repo}/pulls/{pr}/comments")
    reviews = gh.list(f"repos/{repo}/pulls/{pr}/reviews")
    by_marker = {}
    for e in after:
        m = MARKER.search(e.get("body") or "")
        if m:
            by_marker.setdefault(m.group(1), e)
    review = next((r for r in reviews if REVIEW_MARKER in (r.get("body") or "")), None)
    record = {
        **meta,
        "pushed_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "review_url": (review or {}).get("html_url"),
        "comments": {},
    }
    for r in p.resolved:
        e = by_marker.get(r.cid)
        record["comments"][r.cid] = {
            "pile": r.pile, "title": r.title, "mode": r.mode,
            "html_url": (e or {}).get("html_url") or (record["review_url"] if r.mode == "body" else None),
            "path": r.comment.get("path"), "line": (e or {}).get("line") or r.comment.get("line"),
        }
    posted_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


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
        head = f"🛠 **Auto-fixed**{f' in {fixed_sha}' if fixed_sha else ''} — {title}"
        parts = [head, _short(item.get("body", ""), BODY_MAX)]
    elif pile == "ignored":
        sev = item.get("severity", "info")
        head = f"🔴 **Open issue** · {sev} · declined by the author — {title}"
        parts = [head]
        if item.get("why"):
            parts.append(f"**Why declined:** {_short(item['why'], 300)}")
        parts.append(_short(item.get("body", ""), BODY_MAX - 300))
    else:
        conf = item.get("confidence")
        head = f"💭 **Assumption**{f' · confidence {conf:.2f}' if conf is not None else ''} — {title}"
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


def from_review_points(root: Path, points_file: str, at: str) -> dict:
    rp = _load_points_parser()
    text = (root / points_file).read_text(encoding="utf-8")
    doc = rp.parse(text)
    front = doc["front"]
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
               f" · {counts['assumption']} assumptions. Each is pinned on the line it is about.")
    return {"version": 1, "commit_id": git(root, "rev-parse", at).strip(), "event": "COMMENT",
            "body": summary, "comments": comments,
            "generated_by": "push-pr-comments.py --from-review-points"}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _review_commit(root: Path) -> str:
    try:
        rc = json.loads((root / ".human-review/review-commits.json").read_text())
        if rc.get("review"):
            return rc["review"]
    except (OSError, ValueError):
        pass
    return "HEAD"


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
                                 "(default: the review commit, else HEAD)")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    file = root / a.file

    if a.from_review_points:
        payload = from_review_points(root, a.points, a.at or _review_commit(root))
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

    gh = Gh()
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
        existing = gh.list(f"repos/{repo}/pulls/{pr}/comments")
        reviews = gh.list(f"repos/{repo}/pulls/{pr}/reviews")
    except RuntimeError as e:
        if not a.dry_run:
            print(f"cannot list the PR's comments: {e}", file=sys.stderr)
            return 4
        print(f"note: could not list existing comments ({e}); assuming none", file=sys.stderr)
        existing, reviews = [], []
    p = plan(payload, diff, repo, pr, existing, reviews)
    print(f"{info['url']} @ {head[:8]} (merge-base {diff.base[:8]})")
    summarize(p)
    if a.dry_run:
        print(f"\n{len(p.calls)} call(s) would be made:\n")
        for c in p.calls:
            print(f"# {c.why}\n{c.shell()}\n")
        return 0
    try:
        record = execute(p, gh, repo, pr, file.with_name(file.stem + POSTED_SUFFIX),
                         {"repo": repo, "pr": pr, "url": info["url"], "head": head})
    except RuntimeError as e:
        print(f"GitHub refused: {e}", file=sys.stderr)
        return 4
    print(f"posted {len(p.calls)} call(s); review: {record.get('review_url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
