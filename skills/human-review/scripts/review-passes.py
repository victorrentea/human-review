#!/usr/bin/env python3
"""Which review passes already ran in this conversation, and where their output is.

`/human-review` assembles a page *about* a review; it is not the review. The passes that
find things — `/code-review`, `/simplify`, a security pass, somebody's own adversarial
multi-agent review — are the human's to run, when they think they are done. This script is
how the skill finds out whether that happened, so it can harvest what is already in the
conversation instead of paying to re-derive it.

Re-deriving is the failure this exists to prevent. Two runs of the same pass over the same
diff word their findings differently and rank them differently, so a second invocation does
not confirm the first — it produces a *different* review, at full price, and whichever one
reaches the page is decided by which ran last.

Detection is exact; harvesting is best-effort and says which it is:

  * a slash command is a `<command-name>/x</command-name>` row in the transcript — an exact
    record of what the human asked for, with a timestamp;
  * a skill invocation is a `Skill` tool_use naming it;
  * a forked pass runs in a subagent, whose `.meta.json` names its type, description and
    model — that is how an adversarial multi-agent review is recognised without guessing
    from prose;
  * `ReportFindings` carries findings as *data*, and where a pass used it the findings are
    harvested structurally rather than read out of prose.

Where none of that is present the pass still reports its text, extracted from the turns
between the invocation and the next human turn, and labelled `prose` so nobody mistakes a
best-effort read for a structured one.

Exit codes:  0 at least one pass found · 3 none found (with --require) · 2 no transcript.

Usage:
  review-passes.py                                  # what ran, as a table
  review-passes.py --json                           # the same, as data
  review-passes.py --require                        # exit 3 when nothing has been run
  review-passes.py --extract .human-review/passes   # write each pass's output to a file
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# A slash command is recorded verbatim, so the mapping from command to pass is a lookup and
# not a guess. Anything else whose name contains "review" is still reported — as `custom`,
# because a project's own pass is exactly the case this skill must not be blind to.
KNOWN = {
    "code-review": "code-review",
    "review": "code-review",
    "simplify": "simplify",
    "security-review": "security-review",
    "gratar": "custom",
    "multi-review": "custom",
}

COMMAND_RE = re.compile(r"<command-name>\s*/([a-zA-Z0-9:_-]+)\s*</command-name>")
# Subagent descriptions and agent types that mean "somebody ran a review pass out of band".
CUSTOM_HINT = re.compile(r"\b(review|critique|adversarial|audit|red[- ]team)\b", re.I)


def transcript(session_id: str) -> Path | None:
    """The session's own file, wherever the project slug put it.

    By id rather than by working directory: the skill is invoked from the repo under
    review, which is often not the folder the session started in.
    """
    hits = sorted(PROJECTS.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def subagent_files(session_file: Path) -> list[tuple[Path, dict]]:
    """Every subagent this session spawned, as `(transcript, meta)`.

    Claude Code writes a subagent twice: a durable `<session>/subagents/agent-<id>.jsonl`
    beside the parent transcript, and a `/tmp/claude-*/…/tasks/<id>.output` that a machine
    reboot or a tmp sweep takes away. Only the durable one carries a `.meta.json` naming the
    agent's type, description and model, which is what makes a forked review pass
    recognisable at all — so that is the one read here, with the tmp copy as the fallback
    for a session whose project folder has been cleaned instead.
    """
    out: list[tuple[Path, dict]] = []
    home = session_file.parent / session_file.stem / "subagents"
    if home.is_dir():
        for jsonl in sorted(home.glob("agent-*.jsonl")):
            meta_path = jsonl.with_suffix(".meta.json")
            meta = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
            out.append((jsonl, meta))
    if out:
        return out
    ids = set(re.findall(r"agentId[\"':\s]+([0-9a-f]{12,})", session_file.read_text(
        encoding="utf-8", errors="replace")))
    for agent_id in sorted(ids):
        for root in ("/private/tmp", "/tmp"):
            hit = next(Path(root).glob(f"claude-*/*/*/tasks/{agent_id}.output"), None)
            if hit:
                out.append((hit, {}))
                break
    return out


def _rows(path: Path):
    for n, line in enumerate(path.open(encoding="utf-8", errors="replace")):
        line = line.strip()
        if not line:
            continue
        try:
            yield n, json.loads(line)
        except json.JSONDecodeError:
            continue


def _blocks(row: dict) -> list:
    content = (row.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _text_of(row: dict) -> str:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
    return "\n".join(parts)


def _when(row: dict) -> str:
    return row.get("timestamp") or ""


def _typed_text(row: dict) -> str:
    """What the person typed in this row, with tool results left out.

    The harness feeds tool output back as `user` rows, so anything that reads a whole
    `user` row as "the human said this" also reads every file the run ever printed.
    """
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text") or "" for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


def _is_human_turn(row: dict) -> bool:
    """A turn the person typed, as against a tool result the harness fed back.

    Tool results arrive as `user` rows too, so role alone would end every extraction at the
    first tool call the pass made — which is to say, immediately.
    """
    if row.get("type") != "user":
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in content or [])


def scan_main(path: Path, since: dt.datetime | None) -> list[dict]:
    """Passes invoked from the conversation itself: slash commands and Skill calls."""
    rows = list(_rows(path))
    found = []
    for i, (lineno, row) in enumerate(rows):
        stamp = _when(row)
        if since and stamp:
            try:
                if dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) < since:
                    continue
            except ValueError:
                pass
        name = None
        how = None
        # Only what the human actually typed. Tool results are `user` rows too, and a run
        # that greps its own transcript for `<command-name>` puts that very tag inside one
        # — so a whole-row search reports the search as the pass it was looking for.
        if _is_human_turn(row):
            m = COMMAND_RE.search(_typed_text(row))
            if m:
                name, how = m.group(1), "slash command"
        if name is None:
            for b in _blocks(row):
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    skill = str((b.get("input") or {}).get("skill") or "")
                    if skill:
                        name, how = skill.split(":")[-1], "Skill tool"
                    break
        if not name:
            continue
        kind = KNOWN.get(name.split(":")[-1])
        if kind is None:
            if not CUSTOM_HINT.search(name):
                continue
            kind = "custom"
        found.append({"pass": kind, "invoked_as": f"/{name}", "how": how,
                      "at": stamp, "line": lineno, "row": i, "source": str(path)})
    # Each pass's output is everything between its invocation and the next human turn.
    for hit in found:
        text, findings = [], []
        for _lineno, row in rows[hit["row"] + 1:]:
            if _is_human_turn(row):
                break
            if row.get("type") == "assistant":
                t = _text_of(row).strip()
                if t:
                    text.append(t)
            findings += report_findings_in(row)
        hit["output"] = "\n\n".join(text).strip()
        hit["findings"] = findings
        hit["fidelity"] = "structured" if findings else ("prose" if hit["output"] else "empty")
        hit.pop("row", None)
    return found


def report_findings_in(row: dict) -> list[dict]:
    """`ReportFindings` carries the review's findings as data, so take them as data."""
    out = []
    for b in _blocks(row):
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "ReportFindings":
            for f in (b.get("input") or {}).get("findings") or []:
                if isinstance(f, dict):
                    out.append(f)
    return out


def scan_subagents(session_file: Path, since: dt.datetime | None) -> list[dict]:
    """Passes that ran forked, which is how `/code-review` runs by default."""
    found = []
    for jsonl, meta in subagent_files(session_file):
        desc = str(meta.get("description") or "")
        atype = str(meta.get("agentType") or "")
        blob = f"{desc} {atype}"
        kind = None
        for name, mapped in KNOWN.items():
            if re.search(rf"\b{re.escape(name)}\b", blob, re.I):
                kind = mapped
                break
        if kind is None and CUSTOM_HINT.search(blob):
            kind = "custom"
        if kind is None:
            continue
        text, findings, stamp = [], [], ""
        for _lineno, row in _rows(jsonl):
            stamp = stamp or _when(row)
            if row.get("type") == "assistant":
                t = _text_of(row).strip()
                if t:
                    text.append(t)
            findings += report_findings_in(row)
        if since and stamp:
            try:
                if dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) < since:
                    continue
            except ValueError:
                pass
        found.append({"pass": kind, "invoked_as": desc or atype or jsonl.stem,
                      "how": f"subagent ({meta.get('model') or 'unknown model'})",
                      "at": stamp, "source": str(jsonl),
                      "output": "\n\n".join(text[-6:]).strip(), "findings": findings,
                      "fidelity": "structured" if findings else "prose"})
    return found


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "pass"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--since", help="ISO timestamp; ignore passes older than this. The "
                                    "default is the WHOLE session on purpose — the passes "
                                    "this looks for ran before /human-review started")
    ap.add_argument("--json", action="store_true", help="emit the passes as JSON")
    ap.add_argument("--extract", metavar="DIR",
                    help="write each pass's output to DIR/<pass>-<n>.md and print the paths")
    ap.add_argument("--require", action="store_true",
                    help="exit 3 when no review pass ran, so a runbook can stop on it")
    args = ap.parse_args(argv)

    if not args.session:
        print("[review-passes] no session id — cannot tell what ran in this conversation",
              file=sys.stderr)
        return 2
    path = transcript(args.session)
    if not path:
        print(f"[review-passes] no transcript for session {args.session} under {PROJECTS}",
              file=sys.stderr)
        return 2

    since = None
    if args.since:
        try:
            since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print(f"[review-passes] unparseable --since {args.since!r} — ignoring",
                  file=sys.stderr)

    passes = scan_main(path, since) + scan_subagents(path, since)
    passes.sort(key=lambda h: h.get("at") or "")

    if args.extract and passes:
        out_dir = Path(args.extract)
        out_dir.mkdir(parents=True, exist_ok=True)
        for n, hit in enumerate(passes, 1):
            f = out_dir / f"{n:02d}-{slugify(hit['pass'])}.md"
            body = [f"# {hit['invoked_as']} ({hit['pass']}, {hit['fidelity']})",
                    f"<!-- {hit['how']} at {hit['at']} — {hit['source']} -->", ""]
            if hit["findings"]:
                body.append("```json")
                body.append(json.dumps(hit["findings"], indent=2))
                body.append("```")
                body.append("")
            body.append(hit["output"])
            f.write_text("\n".join(body), encoding="utf-8")
            hit["extracted_to"] = str(f)

    if args.json:
        print(json.dumps({"session": args.session, "transcript": str(path),
                          "passes": passes}, indent=2))
    else:
        if not passes:
            print("no review pass found in this conversation")
        else:
            width = max(len(h["pass"]) for h in passes)
            for h in passes:
                n = len(h["findings"])
                detail = f"{n} structured finding(s)" if n else f"{len(h['output'])} chars of prose"
                print(f"  {h['pass']:<{width}}  {h['invoked_as']:<28} {h['how']:<22} "
                      f"{h['at'][:19]}  {detail}")
                if h.get("extracted_to"):
                    print(f"  {'':<{width}}  -> {h['extracted_to']}")
        kinds = sorted({h["pass"] for h in passes})
        print(f"\npasses: {', '.join(kinds) if kinds else 'none'}", file=sys.stderr)

    if args.require and not passes:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
