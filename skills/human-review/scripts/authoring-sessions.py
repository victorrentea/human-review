#!/usr/bin/env python3
"""Which conversation wrote the code under review — this one, an older one, or none.

The review page can carry something no automated pass can produce: the places the agent
that wrote the code was **not sure**. A finding is found by reading the diff; an assumption
is only knowable from the side that made it, and it exists in exactly one place — the
transcript of the conversation that did the work.

That transcript is not always this one, so `/human-review` has three modes and this script
is how it finds out which it is in:

  A  this session edited the changed files — the model answers from its own working memory;
  B  another session on disk did — its transcript is read verbatim by a subagent, which is
     the only honest substitute: a summary of a session is exactly where hedges go to die;
  C  nothing on disk wrote these files — a PR from somebody else, or transcripts long since
     cleaned. The page then says the authoring conversation was not available, rather than
     printing an empty list that reads as "the agent was sure about everything".

Authorship is read from tool calls, not from prose:

  * `Edit` / `Write` / `MultiEdit` / `NotebookEdit` naming a changed file is proof — the
    tool call is the edit, recorded with a timestamp;
  * a `Bash` command naming a changed file *and* a way of writing to it (`>`, `sed -i`,
    `tee`, `patch`, a heredoc) is the same act through a different door, counted separately
    because the evidence is weaker: the path could be an argument to something read-only.
  * a subagent's edits belong to the session that spawned it, so `subagents/agent-*.jsonl`
    is scanned and attributed to the parent.

Reads are deliberately not evidence. Half the sessions on this machine have read these
files; one wrote them.

Neither is an edit older than the branch. A file this change touches has usually been
written by a dozen earlier conversations, on main or on branches long merged, and whatever
they wrote is already in the base the diff is taken against. Only writes from the fork on
count — the earlier of the merge-base's commit time and the oldest author date on the
branch, so a rebase (which moves the merge-base forward but keeps author dates) does not
cut off the conversation that wrote the first commit. Without this bound a Copilot-written
branch forked on 1 Oct was billed $1,047 for 35 Claude conversations from July to September.

Exit codes:  0 mode A · 4 mode B · 5 mode C · 2 nothing to attribute (no change set).

Usage:
  authoring-sessions.py --base origin/main           # the mode, and who, as a table
  authoring-sessions.py --base origin/main --json    # the same, as data
  authoring-sessions.py --base origin/main --paths   # transcript paths, best author first
  authoring-sessions.py --base origin/main --signature  # Opus 5.5 (effort: high, context: 1M)
  authoring-sessions.py --base origin/main --pr-footer  # the PR description's last line

`--signature` and `--pr-footer` say which model wrote the code and at what effort, read off
the transcripts (`harness_cost.coder_signature`): every assistant record carries `model`
and `effort`. The footer is Claude Code's own `🤖 Generated with [Claude Code](…)` with
` · Opus 5.5 (effort: high, context: 1M)` after it — the coder ends a PR description with
it instead of the bare line, so the PR says what its commits' trailers say.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "notebook_edit", "str_replace_editor"}
# A shell command that names a changed file is only an edit if the *same segment* of it
# also carries a way of changing that file. Anywhere-in-the-command matching is not close
# enough: `cat README.md | grep -n x > /tmp/out` names the file and contains a redirect, and
# on that reading every session that ever read the repo becomes one of its authors. Since
# mode B hands a whole transcript to a subagent on the strength of this, a false author is
# the expensive direction to be wrong in — so the redirect has to point *at* the path.
SEGMENT = re.compile(r"(?:\|\||&&|[;|&\n])")
WRITES_IN_SHELL = re.compile(
    r"(>>?\s*$|<<\s*['\"]?\w|\bsed\s+-i|\bperl\s+-i|\btee\b|\bpatch\b|\bapply_patch\b)"
)


# The other half of shell editing: a Python (or Node) script fed in on a heredoc, which
# names its target inside a string literal where no redirect can vouch for it. The write
# call is the vouch instead, and it is looked for across the whole command rather than the
# segment — a heredoc *is* one command, and the line that opens the file is never the line
# that writes it.
PY_WRITE = re.compile(r"write_text\(|\.writelines\(|writeFileSync\(|"
                      r"open\([^)]*['\"][wa]b?['\"]")


def shell_writes(cmd: str, path: str) -> bool:
    """Whether this command writes `path`, as opposed to merely naming it.

    Agents edit through the shell as often as through the edit tools — a heredoc, a
    `sed -i`, a `>` — so a scan that only counted tool calls would miss whole sessions.
    """
    if PY_WRITE.search(cmd):
        # The command writes *something*; the path counts only where it is being opened.
        # Without that second half, a script that rewrites one file while merely printing
        # the name of another makes an author out of a reader all over again.
        for line in cmd.splitlines():
            if path in line and re.search(r"(Path\(|open\(|readFileSync|writeFileSync)", line):
                return True
    for seg in SEGMENT.split(cmd):
        if path not in seg:
            continue
        before = seg.split(path)[0]
        if WRITES_IN_SHELL.search(before) or re.search(r">>?\s*['\"]?$", before):
            return True
    return False


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def changed_files(base: str) -> list[str]:
    """Every path this change set touches, repo-relative, tracked and not.

    The diff runs from the merge-base to the working tree, so uncommitted work counts —
    the common case for the review this skill writes up — and what `base` gained after the
    fork does not: against the tip of main, a day-old branch listed 116 files for 74.
    """
    fork = git("merge-base", base, "HEAD").strip() or base
    names = set(git("diff", "--name-only", fork).split("\n"))
    names |= set(git("ls-files", "--others", "--exclude-standard").split("\n"))
    return sorted(n for n in names if n.strip())


def fork_time(base: str) -> datetime | None:
    """When this branch's own work can have started, in UTC — `None` when git cannot say."""
    fork = git("merge-base", base, "HEAD").strip()
    if not fork:
        return None
    stamps = git("log", "-1", "--format=%ct", fork).split()
    stamps += git("log", "--format=%at", f"{fork}..HEAD").split()
    return datetime.fromtimestamp(min(int(t) for t in stamps), timezone.utc) if stamps else None


# Agents that sign their commits with a co-author trailer and leave no transcript under
# `~/.claude/projects`. A branch they wrote has no measurable writing bill here, and the
# page must say so instead of pricing whichever Claude session last touched a config file.
OTHER_AGENTS = re.compile(r"\b(Copilot|Codex|Cursor|Devin|Gemini|Aider|Jules|Windsurf|Junie)\b",
                          re.I)


def other_agents(base: str) -> list[str]:
    """Non-Claude agents named in the `Co-authored-by` trailers of this branch's commits."""
    fork = git("merge-base", base, "HEAD").strip()
    if not fork:
        return []
    trailers = git("log", "--format=%(trailers:key=Co-authored-by,valueonly)", f"{fork}..HEAD")
    return sorted({m.group(1).capitalize() if m.group(1).islower() else m.group(1)
                   for line in trailers.splitlines() for m in [OTHER_AGENTS.search(line)] if m})


def claimed_sessions(base: str) -> list[str]:
    """Claude session ids this branch's commits name in a `Claude-Session:` trailer.

    The only vouch a shell-only match can get. A session that ran `git` and `python` in
    the repo while another harness wrote the code — an orchestrating conversation — looks,
    from its transcript alone, exactly like a weak author; the branch saying which session
    it was written in is what tells them apart."""
    fork = git("merge-base", base, "HEAD").strip()
    if not fork:
        return []
    out = git("log", "--format=%(trailers:key=Claude-Session,valueonly)", f"{fork}..HEAD")
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _when(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def project_dirs(repo: Path) -> list[Path]:
    """The transcript folders that could hold a session which worked in this repo.

    Claude Code files a session under a slug of the directory it started in, which is the
    repo for most sessions and an ancestor of it for the rest (`~/workspace`, with the repo
    reached by path). Both are accepted; every other project on the machine is skipped,
    because a session that never had this repo as a working root did not write these files.
    """
    if not PROJECTS.is_dir():
        return []
    slug = str(repo).replace("/", "-")
    out = []
    for d in PROJECTS.iterdir():
        if d.is_dir() and (slug == d.name or slug.startswith(d.name.rstrip("-") + "-")):
            out.append(d)
    return out


def transcripts(repo: Path) -> list[tuple[str, Path, list[Path]]]:
    """`(session id, transcript, subagent transcripts)` for every session in scope."""
    out = []
    for d in project_dirs(repo):
        for jsonl in d.glob("*.jsonl"):
            subs = sorted((jsonl.parent / jsonl.stem / "subagents").glob("agent-*.jsonl"))
            out.append((jsonl.stem, jsonl, subs))
    return out


def scan(path: Path, wanted: set[str], repo: Path,
         since: datetime | None = None) -> tuple[dict[str, int], list[str]]:
    """How often this transcript wrote each wanted path, and when it was doing it.

    The raw line is substring-tested before it is parsed: a session transcript runs to
    megabytes and all but a handful of its lines mention none of the changed files, so
    `json.loads` on every one of them is the difference between a scan that takes a second
    and one nobody waits for.

    And before that, the line is asked whether it carries a `tool_use` block at all: the
    loop below reads nothing else, and most of a transcript is the model's prose and the
    tools' output. A change set of fifty files over a project with two thousand
    transcripts was thirty million `w in line` tests -- thirty seconds of a forty-five
    second cost tab -- for lines the loop would then have skipped anyway. (Not a compiled
    alternation of the fifty paths: `re` tries each branch at each position, and measured
    on the same transcripts it was slower than the plain loop, not faster.)
    """
    hits: dict[str, int] = {}
    stamps: list[str] = []
    files: set[str] = set()
    branches: set[str] = set()
    branch = ""

    def note(kind: str, rel: str, ts: str) -> None:
        when = _when(ts) if since and ts else None
        if when and when < since:
            return  # already in the base: written before this branch forked
        hits[kind] = hits.get(kind, 0) + 1
        files.add(rel)
        if branch:
            branches.add(branch)
        if ts:
            stamps.append(ts)
    try:
        raw = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return {}, []
    with raw:
        for line in raw:
            if '"tool_use"' not in line or not any(w in line for w in wanted):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp", "")
            branch = str(rec.get("gitBranch") or "")
            content = ((rec.get("message") or {}).get("content")) or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, inp = block.get("name", ""), block.get("input") or {}
                if name in WRITE_TOOLS:
                    paths = [inp.get("file_path") or inp.get("path") or inp.get("notebook_path")]
                    paths += [e.get("file_path") for e in (inp.get("edits") or [])
                              if isinstance(e, dict)]
                    for p in filter(None, paths):
                        rel = relative(str(p), repo)
                        if rel in wanted:
                            note("edits", rel, ts)
                elif name == "Bash":
                    cmd = str(inp.get("command") or "")
                    for w in wanted:
                        if w in cmd and shell_writes(cmd, w):
                            note("bash", w, ts)
    hits["files"] = sorted(files)
    hits["branches"] = sorted(branches)
    return hits, stamps


def relative(p: str, repo: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(repo))
    except (ValueError, OSError):
        return p.lstrip("./")


def sibling_branch(name: str, cache: dict) -> bool:
    """A branch git still knows that HEAD does not contain: work done on it is not this
    branch's. Eval run 8 forked from the same commit as run 7, an hour later, and run 7's
    session — 16 edits to the very same files, on `hr-claude-7` — was billed as run 8's
    implementation ($11.75). A name git cannot resolve (renamed, deleted) is not one: it
    may well be this branch under its old name."""
    if name not in cache:
        tip = git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}").strip()
        cache[name] = bool(tip) and subprocess.run(
            ["git", "merge-base", "--is-ancestor", tip, "HEAD"],
            capture_output=True).returncode != 0
    return cache[name]


def authors(base: str, repo: Path) -> tuple[list[dict], list[str]]:
    wanted = set(changed_files(base))
    if not wanted:
        return [], []
    since = fork_time(base)
    head = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    claimed = set(claimed_sessions(base))
    siblings: dict = {}
    rows = []
    for sid, jsonl, subs in transcripts(repo):
        edits = bash = 0
        files: set[str] = set()
        stamps: list[str] = []
        branches: set[str] = set()
        for source in [jsonl, *subs]:
            hits, ts = scan(source, wanted, repo, since)
            edits += hits.get("edits", 0)
            bash += hits.get("bash", 0)
            files.update(hits.get("files", []))
            branches.update(hits.get("branches", []))
            stamps += ts
        # Every write it made was on another branch, one this HEAD does not contain, and
        # no commit here names it: a sibling's author, not this branch's.
        if (branches and head not in branches and sid not in claimed
                and all(sibling_branch(b, siblings) for b in branches)):
            continue
        # One hit is enough, because a hit is no longer a mention: the redirect has to
        # point at the path, or the path has to be the thing being opened. A count-based
        # bar on top of that would only trade this script's own false positives for the
        # one-commit session it would then miss.
        if not (edits or bash):
            continue
        stamps.sort()
        rows.append({"session": sid, "transcript": str(jsonl), "edits": edits, "bash": bash,
                     "files": sorted(files), "first": stamps[0] if stamps else "",
                     "last": stamps[-1] if stamps else "",
                     "subagents": len(subs)})
    # Most edits first, and a session that used the write tools outranks one that only ever
    # went through the shell: the first is what a coding session looks like, the second is
    # also what a `git checkout` in an unrelated session looks like.
    rows.sort(key=lambda r: (r["edits"], r["bash"], r["last"]), reverse=True)
    return rows, sorted(wanted)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--paths", action="store_true",
                    help="print only the transcript paths, best author first")
    ap.add_argument("--top", type=int, default=6, metavar="N",
                    help="how many sessions the table shows (--json always shows all)")
    ap.add_argument("--signature", action="store_true",
                    help="print the coding model and effort: Opus 5.5 (effort: high)")
    ap.add_argument("--pr-footer", action="store_true",
                    help="print the PR description's footer, with the model and effort")
    args = ap.parse_args()

    repo_root = git("rev-parse", "--show-toplevel").strip()
    if not repo_root:
        print("not a git repository", file=sys.stderr)
        return 2
    repo = Path(repo_root).resolve()

    if args.signature or args.pr_footer:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import harness_cost
        sig = harness_cost.coder_signature(repo, args.base)
        if not sig:
            print(f"no Claude transcript on this machine wrote the change against "
                  f"{args.base}: model and effort unknown", file=sys.stderr)
        if sig or args.pr_footer:
            print(harness_cost.pr_footer(sig) if args.pr_footer else sig)
        return 0 if sig else 5

    rows, wanted = authors(args.base, repo)
    if not wanted:
        print(f"no change set against {args.base} — nothing to attribute", file=sys.stderr)
        return 2

    here = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    mine = [r for r in rows if r["session"] == here]
    for r in rows:
        r["current"] = r["session"] == here
    mode = "A" if mine else ("B" if rows else "C")
    code = {"A": 0, "B": 4, "C": 5}[mode]

    if args.json:
        print(json.dumps({"mode": mode, "base": args.base, "changed": len(wanted),
                          "otherAgents": other_agents(args.base),
                          "claimedSessions": claimed_sessions(args.base),
                          "sessions": rows}, indent=2))
        return code
    if args.paths:
        for r in rows:
            print(r["transcript"])
        return code

    headline = {
        "A": "mode A — this conversation wrote the code; answer from your own memory",
        "B": "mode B — an earlier conversation wrote it; read its transcript verbatim",
        "C": "mode C — no conversation on disk wrote these files; say so on the page",
    }[mode]
    print(f"{headline}   ({len(wanted)} files changed against {args.base})")
    if not rows:
        return code
    print(f"{'session':<38} {'edits':>5} {'bash':>5}  files")
    for r in rows[:args.top]:
        tag = f"{r['session'][:8]}…" + (" (this session)" if r["current"] else "")
        shown = ", ".join(r["files"][:3]) + ("…" if len(r["files"]) > 3 else "")
        print(f"{tag:<38} {r['edits']:>5} {r['bash']:>5}  {shown}")
        if r["first"]:
            print(f"{'':<38} {r['first'][:16]} → {r['last'][:16]}"
                  + (f" · {r['subagents']} subagents" if r["subagents"] else ""))
    if len(rows) > args.top:
        print(f"… and {len(rows) - args.top} more with weaker evidence (--json for all)")
    return code


if __name__ == "__main__":
    sys.exit(main())
