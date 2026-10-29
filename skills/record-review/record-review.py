#!/usr/bin/env python3
"""The mechanical half of /record-review, so the agent spends its turns on judgement only.

The first run in VS Code Copilot Chat took 332 tool calls. Most of them were not the review:
finding where the format file lives, working out the base, re-reading the diff a hunk at a
time, composing four reviewer prompts, wiring trailers, and hand-writing seventeen PR
comments. Every one of those has exactly one right answer, so a program gives it.

  record-review.py prepare [--base REF] [--impl-subject "…"]
      Resolves the change set, commits uncommitted implementation work if asked to,
      writes the whole diff to one file and one brief per reviewer lens, and prints what
      the agent needs next — nothing else to look up.

  record-review.py finish --subject "…" [--implements SHA] [--commit-trailer "K: v"]…
      Fills review-points.md's front-matter (the audited range, the implementation
      commit, HEAD — kept apart), converts it into the structured report
      .human-review/review-points.json and validates it against
      reference/review-points.schema.json, commits the fixes with it under `[auto-fix]`
      and the trailers (plus any --commit-trailer), then derives and checks the PR
      comments. Every `file:line` is carried and checked first: one whose line is gone
      or blank refuses the finish with nothing written or committed. Re-run before
      the commit was pushed, it amends that commit — one `[auto-fix]` commit per round.

  record-review.py ci [--push]
      Waits for CI on HEAD. Run first right after the reviewers — which also stamps the
      moment they were done — then with --push after each finish.

What the review cost is measured here too, by the harness that ran it, because only now
are its boundaries known rather than guessed: `prepare` stamps the start of the review,
the first `ci` the reviewers' end, `finish` the end of the fixes. `finish` writes the
three components — implementation (everything up to prepare, in any harness), review,
auto-fixes — to `review-cost.json` beside review-points.md, checked against
reference/review-cost.schema.json and committed with the fixes; every later `finish` (a CI
round) rewrites it with the auto-fix window run on to that round. /human-review reads it
and adds the fourth component, the guide itself (scripts/harness_cost.py).

What stays with the agent is what only it can do: run the reviewers on the briefs, accept or
decline each finding, write the assumptions it made, and edit the code it accepts.

Exit codes: 0 ok · 2 not a git repo / bad base · 3 uncommitted implementation and no
--impl-subject · 4 review-points.md missing, does not parse, its report does not match
the schema, or an anchor points at a line that is gone or blank.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HR = (HERE.parent / "human-review").resolve()
SCRIPTS = HR / "scripts"
WORK = ".human-review/review"
POINTS = "review-points.md"
#: The structured record the Review tab is rendered from (reference/review-points.schema.json).
REPORT = ".human-review/review-points.json"
#: What the change cost up to this commit, committed beside POINTS
#: (reference/review-cost.schema.json, written by scripts/harness_cost.py:record).
COST = "review-cost.json"
DEFAULT_GENERATED = ["**/generated/**", "docs/generated/**", "openapi.yaml",
                     "**/*.genseq.*", "**/api-types.ts", "**/*.drawio*"]

#: Files a reviewer of the *code* does not need. Three of the four lenses read only the
#: code diff: in the second VS Code run the four reviewers paged through one 88 KB patch
#: 66 times between them, and half of it was tests. `"tests"` in human-review.json adds
#: project-specific patterns.
DEFAULT_TESTS = ["**/src/test/**", "**/test/**", "**/tests/**", "**/*.spec.*", "**/*.test.*",
                 "**/*Test.java", "**/*Tests.java", "**/*IT.java", "**/e2e/**",
                 "**/*.feature"]
READS = {"correctness": ["code"], "security": ["code"], "ticket-fit": ["code"],
         "tests": ["code", "tests"]}

LENSES = {
    "correctness": "what input or sequence of actions makes this code return the wrong "
                   "thing, lose state, or crash. Race conditions, off-by-one, null and empty "
                   "cases, error paths.",
    "security": "what an attacker or a careless caller can do with this change: injection, "
                "missing authorization, data leaked into logs or responses, unbounded input.",
    "ticket-fit": "what the ticket asked for that the code does not do, and what the code "
                  "does that nobody asked for. Read the ticket text below first.",
    "tests": "what this change can break while every test stays green: behaviour no test "
             "pins, tests that assert too little, tests that would pass against the old code.",
}

BRIEF = """You are a read-only reviewer. Do not edit any file. Lens: **{lens}** — {what}

The change set (base {base_short}..{head_short}) is in {patches}. Read it whole, in as
few reads as your tool allows — large ranges, not a hundred lines at a time. Open other
files only to confirm a suspicion, and only the lines you need.
{ticket}
Try to BREAK the change, not to approve it. Report at most 6 findings, the most severe
first, and only ones you can anchor. Answer with nothing but this, one block per finding:

### <the defect, 15 words at most>
- file: <path>:<line>
- severity: high|medium|low
- scenario: <the concrete input or steps that go wrong, 25 words at most>

If you find nothing worth reporting, answer `none`.
"""


def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _cost_modules():
    """`harness_cost` and the schema checker, from the human-review skill beside this one."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import harness_cost
    import review_points_schema
    return harness_cost, review_points_schema


def _forge():
    """The forge CI runs on, asked through the human-review skill beside this one."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from hrbuild.shared.forge import api_forge
    return api_forge()


def record_cost(repo: Path, state: dict, harness_name: str) -> tuple[dict | None, str]:
    """Measure the three components and write COST. Never stops the commit: a measurement
    that fails is a row that says so, and the review it measures is still recorded."""
    try:
        hc, schema = _cost_modules()
        doc = hc.record(repo, state.get("base") or resolve_base(None), state,
                        hc.normalize_harness(harness_name))
    except Exception as exc:  # noqa: BLE001 — the cost must never cost the review
        return None, f"not measured ({type(exc).__name__}: {exc})"
    bad = schema.cost_problems(doc)
    if bad:
        return None, "not written — " + "; ".join(bad[:3])
    # Two spaces and a final newline: the repository's own .editorconfig is the one that
    # usually decides, and petclinic's pre-commit refused the 1-space file twice on hr-try-4.
    (repo / COST).write_text(json.dumps(doc, indent=2) + "\n")
    parts = []
    for c in doc["components"]:
        money = " + ".join(x for x in (
            f"${c['usd']:.2f}" if c.get("usd") is not None else "",
            f"{c['aic']:.1f} AIC" if c.get("aic") is not None else "") if x)
        parts.append(f"{c['label']} {money or 'unmeasured'}")
    return doc, " · ".join(parts)


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], check=check, capture_output=True,
                          text=True).stdout.strip()


def root() -> Path:
    try:
        return Path(git("rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError:
        sys.exit("record-review: not inside a git repository")


def config(repo: Path) -> dict:
    try:
        return json.loads((repo / "human-review.json").read_text())
    except (OSError, ValueError):
        return {}


def generated_globs(repo: Path) -> list[str]:
    return config(repo).get("generated") or DEFAULT_GENERATED


def test_globs(repo: Path) -> list[str]:
    return DEFAULT_TESTS + list(config(repo).get("tests") or [])


def resolve_base(given: str | None) -> str:
    ref = given or "origin/main"
    try:
        return git("merge-base", "HEAD", ref) if not given else git("rev-parse", ref)
    except subprocess.CalledProcessError:
        sys.exit(f"record-review: cannot resolve base {ref!r}")


def dirty(repo: Path) -> list[str]:
    lines = git("status", "--porcelain").splitlines()
    return [l for l in lines if not l[3:].startswith(".human-review")]


def stage_all() -> None:
    """Everything but the throwaway directory, which most repos ignore anyway — and an
    exclude pathspec naming an ignored path makes `git add` fail outright."""
    git("add", "-A")
    git("reset", "-q", "--", ".human-review", check=False)


def push_gate() -> str:
    """The repository's own pre-push checks, run now instead of at /human-review's push.

    They are the cheapest reviewer there is — deterministic, free, already written — and
    the one the first VS Code run never asked: four model reviewers passed a commit whose
    openapi.yaml the Spectral hook refuses, and the page then stopped at its push. A
    dry-run push runs the hook without sending anything. Returns the lines that say why
    it refused, or "" when it passes."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    r = subprocess.run(["git", "push", "--dry-run", "origin", f"HEAD:refs/heads/{branch}"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return ""
    lines = [l.rstrip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    keep = [l for l in lines if re.search(r"\berror\b|❌|rejected|denied|fatal", l, re.I)]
    return "\n".join((keep or lines[-5:])[:12])


def session_id(given_harness: str | None = None) -> str:
    """The Claude Code session that recorded the review — none when another harness did.

    `CLAUDE_CODE_SESSION_ID` is inherited: a `copilot -p` started from a Claude session
    carries the parent's id, and hr-try-3 recorded a Copilot review under a Claude session
    that never ran it. So a declared non-Claude harness wins over the environment."""
    if given_harness and given_harness != "claude-code":
        return ""
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def harness(given: str | None) -> str:
    """Which agent harness recorded the review: what the caller says, else Claude Code
    when its session id is in the environment, else nothing — never a guess. An inherited
    id makes this guess wrong for a Copilot child process, which is why a non-Claude
    caller must pass `--harness`."""
    if given:
        return given
    return "claude-code" if session_id() else ""


def rev(ref: str | None) -> str | None:
    """`ref` as a full sha, or exit 2 naming it — a typo'd sha must not become a trailer."""
    if not ref:
        return None
    try:
        return git("rev-parse", "--verify", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        sys.exit(f"record-review: cannot resolve {ref!r} to a commit")


#: One `Key: value` line, as git's own trailer parser reads one.
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S.*$")


def trailers(**keys: str) -> str:
    return "\n".join(f"{k}: {v}" for k, v in keys.items() if v)


def extra_trailers(given: list[str]) -> list[str]:
    """`--commit-trailer` values, checked: a harness that needs its own attribution
    (`Co-authored-by: Copilot <…>`) passes it here instead of patching this script."""
    out = []
    for t in given or []:
        t = t.strip()
        if not TRAILER.match(t) or "\n" in t:
            sys.exit(f"record-review: --commit-trailer {t!r} is not one `Key: value` line")
        out.append(t)
    return out


# --------------------------------------------------------------------------- prepare

def prepare(args) -> int:
    repo = root()
    os.chdir(repo)
    base = resolve_base(args.base)
    if dirty(repo):
        if not args.impl_subject:
            print("The implementation is not committed. Re-run with "
                  "--impl-subject \"<what it implements>\" and it is committed for you, "
                  "alone, before the review.")
            for l in dirty(repo)[:20]:
                print("  ", l)
            return 3
        stage_all()
        msg = args.impl_subject + ("\n\n" + trailers(**{"Claude-Session": session_id(args.harness)})
                                   if session_id(args.harness) else "")
        git("commit", "-q", "-m", msg)
    head = git("rev-parse", "HEAD")
    commits = git("log", "--format=%h %s", f"{base}..HEAD").splitlines()
    if not commits:
        print("Nothing to review: HEAD is the base.")
        return 2

    excludes = [f":(exclude,glob){g}" for g in generated_globs(repo)]
    tests = [f":(glob){g}" for g in test_globs(repo)]
    files = git("diff", "--name-only", f"{base}..HEAD", "--", ".", *excludes).splitlines()
    test_files = set(git("diff", "--name-only", f"{base}..HEAD", "--", *tests).splitlines())
    groups = {"code": [f for f in files if f not in test_files],
              "tests": [f for f in files if f in test_files]}
    work = repo / WORK
    work.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, paths in groups.items():
        patch = git("diff", "--no-ext-diff", "-U3", f"{base}..HEAD", "--", *paths) if paths else ""
        (work / f"diff-{name}.patch").write_text(patch + "\n")
        sizes[name] = (len(paths), len(patch) // 1024)
    ticket = ""
    if args.ticket:
        ticket = f"\nThe ticket, as the human gave it:\n\n{args.ticket.strip()}\n"
    for lens, what in LENSES.items():
        patches = " and ".join(f"`{WORK}/diff-{g}.patch`" for g in READS[lens] if groups[g])
        (work / f"brief-{lens}.md").write_text(BRIEF.format(
            lens=lens, what=what, patches=patches,
            base_short=base[:8], head_short=head[:8], ticket=ticket))
    gate = push_gate()
    if gate:
        (work / "gate.txt").write_text(gate + "\n")
    pushed = False
    if not gate and not args.no_ci:
        # CI is a reviewer too, and the slowest one: start it now, so it reviews while
        # the model reviewers do. /human-review would push this branch anyway.
        pushed = subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"],
                                capture_output=True, text=True).returncode == 0
    # Two different commits, kept apart: what the reviewers read (`auditedHead`, HEAD
    # now) and what implements the feature — by default the same, but not when the
    # branch tip is housekeeping that landed after the feature (`--implements <sha>`).
    implementation = rev(args.implements) or head
    # `reviewStartedAt` is the boundary between writing the code and reviewing it — the
    # one moment only this command knows. `review-cost.json` is cut at it.
    state = {"base": base, "auditedHead": head, "implementation": implementation,
             "session": session_id(args.harness), "harness": harness(args.harness),
             "reviewStartedAt": _now(),
             "sessions": [s for s in [session_id(args.harness)] if s]}
    (work / "state.json").write_text(json.dumps(state, indent=2) + "\n")

    print(f"base            {base[:8]}  ({args.base or 'merge-base with origin/main'})")
    print(f"audited         {base[:8]}..{head[:8]}  ({len(commits)} commit(s): {commits[0]}"
          + (" …" if len(commits) > 1 else "") + ")")
    print(f"implementation  {implementation[:8]}"
          + ("  (--implements)" if implementation != head else "  (HEAD)"))
    print("diff            " + "  ".join(
        f"{WORK}/diff-{g}.patch ({n} files, {kb} KB)" for g, (n, kb) in sizes.items())
        + " — generated files left out")
    print("briefs          " + "  ".join(f"{WORK}/brief-{l}.md" for l in LENSES))
    if gate:
        print("push gate       FAILS — the repository's own pre-push checks refuse this "
              f"commit ({WORK}/gate.txt). Each error is a finding with `source: pre-push "
              "hook`; /human-review cannot run until it is fixed:")
        for line in gate.splitlines():
            print("                " + line)
    else:
        print("push gate       passes (the repository's pre-push checks, run as a dry-run)")
    if pushed:
        print(f"CI              started on {head[:8]} — after the reviewers, run "
              f"`{Path(__file__).resolve()} ci`: it waits for the result and lists what "
              "failed, SonarCloud's new issues included, as findings")
    print()
    print("Next: start one read-only reviewer subagent per brief, in parallel, each given "
          "only its brief file's content. Then decide every finding, edit what you accept, "
          f"write {POINTS} (piles only; front-matter is filled for you) and run:")
    print(f"  {Path(__file__).resolve()} finish --subject \"<what the fixes do>\" "
          "--reviewers \"<how the review ran>\"")
    return 0


# --------------------------------------------------------------------------- finish

def fill_frontmatter(text: str, values: dict[str, str]) -> str:
    """Add the keys the program knows, keep every key the agent wrote."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    have = dict(re.findall(r"^([A-Za-z-]+):\s*(.*)$", m.group(1), re.M)) if m else {}
    merged = {**{k: v for k, v in values.items() if v}, **have}
    head = "---\n" + "\n".join(f"{k}: {v}" for k, v in merged.items()) + "\n---\n"
    return head + (text[m.end():] if m else "\n" + text.lstrip())


def _points_module():
    """`review-points.py` — hyphenated, so loaded by path: the parser and the one
    implementation of carrying a `file:line` across commits (`reanchor`)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("rr_review_points", SCRIPTS / "review-points.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: The front-matter key that says every `file:line` was carried to the committed tree.
ANCHORS = "anchors"


def reanchor_points(repo: Path, text: str, implementation: str) -> tuple[str, list[str]]:
    """Every `file:line` in review-points.md, carried to the tree about to be committed;
    returns the file's new text and what was said about it.

    The assumptions are written while coding, against the implementation commit; the
    fixes and declined findings after the fixes, against the working tree. Run 6 committed
    both as they were, and the page quoted a blank line under one assumption and an
    unrelated statement under another once the fixes had moved them. Here, before the
    commit, each ref is mapped through the diff from where it was most likely written
    (`review-points.py:reanchor`, which falls back to the other rev when the first lands on
    a blank line) and rewritten in place; a ref whose line is gone or blank is a warning,
    for the agent to fix before `finish` is run again. A later CI round's file was already
    carried once (`anchors:` is set): its refs read at HEAD, new ones at the working tree."""
    rp = _points_module()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    again = bool(m and re.search(rf"^{ANCHORS}:", m.group(1), re.M))
    prior = "HEAD" if again else implementation
    said: list[str] = []
    out, pile = [], None
    for raw in text.splitlines(keepends=True):
        h2 = rp.H2.match(raw.rstrip("\n"))
        if h2:
            pile = rp.SECTIONS.get(h2.group(1).strip().lower())
        f = rp.FIELD.match(raw.strip())
        if not (pile and f and f.group(1).lower() == "file"):
            out.append(raw)
            continue
        refs, _ = rp.split_refs(rp.split_ref(f.group(2))[0])
        line = raw
        for ref in refs:
            if rp.ref_spans(ref)[1] is None:
                continue
            first = rp.WORKTREE if pile != "assumptions" else prior
            other = prior if first == rp.WORKTREE else rp.WORKTREE
            got = rp.reanchor(repo, ref, [first, other])
            if got["ref"] is None:
                # Read at the first rev the line is one the fixes removed — but prompt.md
                # tells the agent to write every line as it reads NOW, fixes on disk, and
                # eval run 10 did exactly that: `ExceptionControllerAdvice.java:88` was the
                # handler in the working tree and a removed line at the implementation. Two
                # extra `[auto-fix]` commits followed, re-pointing it by hand. The other
                # reading, when it lands on code, is the one the agent meant.
                alt = rp.reanchor(repo, ref, [other])
                if alt["ref"] is not None and not alt["blank"]:
                    got = alt
            if got["ref"] is None:
                said.append(f"WARNING {ref}: written at {str(got['from'])[:8]}, and the "
                            "fixes removed that line — point it at the line it means now")
                continue
            if got["moved"]:
                line = re.sub(rf"(?<![\w/.-]){re.escape(ref)}(?!\d|[-,]\d)",
                              lambda _m, new=got["ref"]: new, line)
                said.append(f"re-anchored {ref} -> {got['ref']} (written at "
                            f"{str(got['from'])[:8]})")
            if got["blank"]:
                said.append(f"WARNING {got['ref']}: points at a blank line, or past the end "
                            "of the file — anchor it on the statement it is about")
        out.append(line)
    return "".join(out), said


def on_remote(sha: str) -> bool:
    """Whether any remote-tracking branch already holds `sha` — once pushed, a commit is
    never amended."""
    out = subprocess.run(["git", "branch", "-r", "--contains", sha],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def is_auto_fix_head() -> bool:
    return git("log", "-1", "--format=%s", "HEAD").lower().startswith("[auto-fix]")


def finish(args) -> int:
    repo = root()
    os.chdir(repo)
    points = repo / POINTS
    if not points.is_file():
        print(f"{POINTS} is missing at the repository root.")
        return 4
    extra = extra_trailers(args.commit_trailer)
    try:
        state = json.loads((repo / WORK / "state.json").read_text())
    except (OSError, ValueError):
        state = {"base": resolve_base(None), "implementation": git("rev-parse", "HEAD"),
                 "session": session_id(args.harness)}
    head = git("rev-parse", "HEAD")
    implementation = rev(args.implements) or state["implementation"]
    audited_head = state.get("auditedHead") or state["implementation"]
    # Every ref carried to the tree this commit records, and checked, BEFORE anything is
    # written or committed. A ref whose line is gone or blank refuses the finish: eval
    # run 10's first finish committed with such a warning, and the agent then spent two
    # more `[auto-fix]` commits (1338ed9e, e7b807e8) re-pointing one assumption by hand,
    # so the page called a one-line re-anchor "the fix commit". Refused here, the agent
    # fixes the line and re-runs, and the round still ends in one commit.
    original = points.read_text()
    text, said = reanchor_points(repo, original, implementation)
    for line in said:
        print(f"anchors         {line}")
    bad = [line for line in said if line.startswith("WARNING")]
    if bad:
        print(f"nothing committed — {len(bad)} anchor{'s' if len(bad) != 1 else ''} above "
              f"cannot be shown on the page. Point each at the line it means as it reads "
              f"now (fixes on disk), then run finish again; {POINTS} was left as you wrote "
              "it.")
        return 4
    # A CI round may run in a new conversation; its session is the auto-fixes' too.
    sid = session_id(args.harness or state.get("harness"))
    if sid and sid not in state.setdefault("sessions", []):
        state["sessions"].append(sid)
    cost, cost_line = record_cost(repo, {**state, "implementation": implementation},
                                  args.harness or state.get("harness") or "")
    # Kept where the page looks: a record refused here (eval run 8 — a key the schema did
    # not know) left the cost tab blaming the branch for predating the record.
    if cost is None:
        state["costError"] = cost_line
    else:
        state.pop("costError", None)
    # The Copilot sessions the measurement found, by id, on the commit: the CLI exports no
    # session id, so the match by cwd, branch and time is made once, here, while it is
    # fresh, and kept where a rebase keeps it.
    copilot = sorted({e["session"] for c in (cost or {}).get("components", [])[1:]
                      for e in c.get("entries", []) if e.get("harness") == "copilot-cli"})
    points.write_text(text)
    # The structured record's provenance: four commits a reviewer must not confuse.
    # `review-commit` is not among them on purpose — this commit cannot name itself; the
    # report derives it from the commit that recorded the file.
    points.write_text(fill_frontmatter(points.read_text(), {
        "base": state["base"], "audited-base": state["base"],
        "audited-head": audited_head, "implementation": implementation, "head": head,
        "reviewers": args.reviewers or "", "harness": harness(args.harness)
        or state.get("harness", ""), "session": state.get("session", ""),
        "fixed-in": "HEAD", ANCHORS: "review-commit"}))

    # The structured report, written where /human-review reads it and checked against
    # reference/review-points.schema.json on the way out: a file that does not convert to
    # a valid report is refused here, while the agent that wrote it can still fix it.
    report = subprocess.run([sys.executable, str(SCRIPTS / "review-points.py"),
                             "--out", str(repo / REPORT)],
                            capture_output=True, text=True)
    print(report.stdout.strip().splitlines()[0] if report.stdout.strip() else "")
    if report.returncode != 0:
        print(report.stdout + report.stderr)
        # Back to what the agent wrote: re-anchored and stamped `anchors:`, a second run
        # would read the assumptions at HEAD instead of the implementation commit.
        points.write_text(original)
        return 4

    stage_all()
    # One `[auto-fix]` commit per round. A finish re-run before its commit ever left the
    # machine (no `ci --push` since) is the same round being corrected, so it amends that
    # commit, keeping its subject, instead of stacking a cosmetic follow-up on it.
    amend = (state.get("reviewCommit") == head and is_auto_fix_head()
             and not on_remote(head))
    subject = git("log", "-1", "--format=%s", "HEAD") if amend else f"[auto-fix] {args.subject}"
    msg = f"{subject}\n\n" + "\n".join(filter(None, [trailers(**{
        "Review-Points": POINTS, "Implements": implementation,
        "Audited": f"{state['base']}..{audited_head}",
        "Claude-Session": state.get("session", "")}),
        *[f"Copilot-Session: {c}" for c in copilot], *extra]))
    git("commit", "-q", *(["--amend"] if amend else []), "-m", msg)
    sha = git("rev-parse", "HEAD")
    state["reviewCommit"] = sha
    finishes = state.setdefault("finishes", [])
    if amend and finishes:
        finishes.pop()
    finishes.append((cost or {}).get("recordedAt") or _now())
    (repo / WORK).mkdir(parents=True, exist_ok=True)
    (repo / WORK / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    print(f"{'amended' if amend else 'committed'}       {sha[:8]} {subject}"
          + (f"  (same round: {head[:8]} was not pushed yet)" if amend else ""))
    print(f"provenance      audited {state['base'][:8]}..{audited_head[:8]} · implements "
          f"{implementation[:8]} · head {head[:8]} · recorded in {sha[:8]}")
    print(f"report          {REPORT} (validated against review-points.schema.json)")
    print(f"cost            {COST}: {cost_line}")

    for extra in (["--from-review-points"], ["--check", "--base", state["base"]]):
        r = subprocess.run([sys.executable, str(SCRIPTS / "push-pr-comments.py"), *extra],
                           capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip().splitlines()
        print("pr comments     " + (out[0] if out else f"exit {r.returncode}"))
    print("Next: `record-review.py ci --push` — the review is done when CI is green on this "
          "commit.")
    return 0


# --------------------------------------------------------------------------- ci

def sonar_issues(repo: Path, branch: str) -> list[str]:
    """SonarCloud's issues on the branch's new code — the quality gate's reasons, which the
    CI log reduces to "Quality Gate has FAILED". Public projects answer without a token."""
    props = repo / "sonar-project.properties"
    if not props.is_file():
        return []
    conf = dict(re.findall(r"^\s*([\w.]+)\s*=\s*(.+?)\s*$", props.read_text(), re.M))
    key = conf.get("sonar.projectKey")
    if not key:
        return []
    host = conf.get("sonar.host.url", "https://sonarcloud.io").rstrip("/")
    cmd = ["curl", "-sf", f"{host}/api/issues/search?componentKeys={key}&branch={branch}"
           "&inNewCodePeriod=true&resolved=false&ps=50"]
    if os.environ.get("SONAR_TOKEN"):
        cmd[2:2] = ["-u", os.environ["SONAR_TOKEN"] + ":"]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "{}")
    except ValueError:
        return []
    # Bugs and vulnerabilities first: they are what a quality gate on reliability and
    # security ratings fails on; code smells are listed for completeness, after them.
    rank = {"BUG": 0, "VULNERABILITY": 1}
    issues = sorted(data.get("issues", []), key=lambda i: rank.get(i.get("type"), 2))
    return [f"{i.get('type', '?'):<13} {i['component'].split(':', 1)[-1]}:{i.get('line', '?')}"
            f"  {i['message']}  ({i['rule']})" for i in issues]


def ci(args) -> int:
    """Wait for CI on HEAD — the commit, never the branch — and print why it failed.

    Exit 0 green · 1 red (a failed run, or a SonarCloud BUG/VULNERABILITY on new code) ·
    2 no verdict (no run registered, or not finished in time). The exit code is the loop's
    condition: the review is not done until CI is green on the `[auto-fix]` commit itself.
    Run once on the implementation commit, a CI that fails early (Spectral) never reaches
    Sonar, the fixes go in unanalysed, and /human-review's preflight then stops on a Sonar
    BUG the review could have fixed — hr-try-3, a wasted page run.

    `--push` sends HEAD first, so the run is the one for this commit."""
    import time
    repo = root()
    os.chdir(repo)
    sha = git("rev-parse", "HEAD")
    try:
        st_path = repo / WORK / "state.json"
        state = json.loads(st_path.read_text())
        # The first `ci` comes right after the reviewers: that is the end of the review,
        # and the start of deciding and fixing. Stamped once.
        if (not getattr(args, "push", False) and state.get("reviewStartedAt")
                and not state.get("reviewersDoneAt")):
            state["reviewersDoneAt"] = _now()
        # Every `ci` moves the end of the auto-fix window. A CI round fixed and committed
        # without another `finish` (hr-try-4's second round) is then still billed: the
        # cost tab re-measures the window to here when the record is older than this.
        state["lastCiAt"] = _now()
        st_path.write_text(json.dumps(state, indent=2) + "\n")
    except (OSError, ValueError):
        pass
    if getattr(args, "push", False):
        pushed = subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"],
                                capture_output=True, text=True)
        if pushed.returncode != 0:
            lines = [l for l in (pushed.stdout + pushed.stderr).splitlines() if l.strip()]
            keep = [l for l in lines if re.search(r"\berror\b|❌|rejected|denied|fatal", l, re.I)]
            print("push            FAILED — each line is a finding (`source: pre-push hook`):")
            for l in (keep or lines[-5:])[:12]:
                print("    " + l.strip()[:200])
            return 1
        print(f"pushed          {sha[:8]} — waiting for its CI")
    # Nothing to wait for when the commit never reached origin (prepare's push gate
    # refused it): eval run 7 sat out the full wait for a CI run that could not exist,
    # backgrounded it, and the headless session ended before `finish` ever ran.
    on_remote = subprocess.run(["git", "branch", "-r", "--contains", sha],
                               capture_output=True, text=True).stdout.strip()
    if not on_remote:
        print(f"CI              {sha[:8]} is not on origin — nothing to wait for. Push it "
              "(`RR ci --push`), or fix what the push gate refused first.")
        return 2
    deadline = time.time() + args.wait_minutes * 60
    runs: list = []
    while time.time() < deadline:
        out = _forge().ci_runs_of(sha)
        runs = json.loads(out or "[]")
        if runs and all(r["status"] == "completed" for r in runs):
            break
        time.sleep(20)
    else:
        print(f"CI              not finished on {sha[:8]} after {args.wait_minutes} min"
              if runs else f"CI              no run registered for {sha[:8]}")
        return 2
    failed = [r for r in runs if r["conclusion"] not in ("success", "skipped", "neutral")]
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    sonar = sonar_issues(repo, branch)
    blocking = [i for i in sonar if i.startswith(("BUG", "VULNERABILITY"))]
    if not failed and not blocking:
        print(f"CI              green on {sha[:8]}")
        for issue in sonar:
            print("  sonar " + issue)
        return 0
    print(f"CI              FAILED on {sha[:8]} — each line is a finding (`source: CI`):")
    for r in failed:
        log = _forge().ci_failed_log(r["databaseId"])
        plain = re.sub(r"\x1b\[[0-9;]*m|\^\[\[[0-9;]*m", "", log)
        errors = [l.split("\t")[-1][29:] if "\t" in l else l for l in plain.splitlines()
                  if re.search(r"##\[error\]|ERROR|FAILED|✖", l)]
        print(f"  {r['name']}:")
        for e in errors[-6:]:
            print("    " + e.strip()[:200])
    for issue in sonar:
        print("  sonar " + issue)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--base")
    p.add_argument("--impl-subject", help="commit uncommitted work as the implementation")
    p.add_argument("--ticket", help="the ticket text, handed to every reviewer")
    p.add_argument("--no-ci", action="store_true", help="do not push to start CI early")
    p.add_argument("--implements", help="the commit that implements the feature, when "
                   "HEAD is housekeeping after it (default: HEAD)")
    p.add_argument("--harness", help="the agent harness, recorded in the report "
                   "(default: claude-code when its session id is set)")
    c = sub.add_parser("ci")
    c.add_argument("--wait-minutes", type=float, default=20)
    c.add_argument("--push", action="store_true",
                   help="push HEAD first, so CI runs on this commit (after finish)")
    f = sub.add_parser("finish")
    f.add_argument("--subject", required=True)
    f.add_argument("--reviewers", default="")
    f.add_argument("--implements", help="override the implementation commit prepare "
                   "recorded — `Implements:` names it, never the latest housekeeping")
    f.add_argument("--harness", help="the agent harness, recorded in the report")
    f.add_argument("--commit-trailer", action="append", default=[], metavar="KEY: VALUE",
                   help="an extra trailer on the review commit, e.g. `Co-authored-by: "
                        "Copilot <…>`; repeatable")
    args = ap.parse_args(argv)
    return {"prepare": prepare, "finish": finish, "ci": ci}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
