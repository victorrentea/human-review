#!/usr/bin/env python3
"""What this review cost, in tokens and in list-price dollars.

A review guide asks a human to spend their attention, and every other chip in the scope
bar quantifies what it is asking about. This one quantifies what producing it consumed —
the one number the page could always have known about itself and never reported.

Where the numbers come from
---------------------------
Claude Code appends every turn to `~/.claude/projects/<slug>/<session-id>.jsonl`, and each
assistant record carries `message.usage`. That is the same source `/claude-usage` reads;
this is the single-run, single-session slice of it.

Three details matter and are easy to get wrong:

* **Dedupe by `message.id`.** A streamed message is written more than once, with the usage
  repeated. Summing rows rather than messages roughly doubles the bill.
* **Subagents count, and they are not in the session file.** A subagent's turns go to
  `/tmp/claude-<uid>/<slug>/<workspace>/tasks/<agentId>.output`, and nothing in the
  environment names that directory. The link is made from the other end: the parent
  transcript records the `agentId` of every agent it spawned, so those ids are read out of
  it and only the matching task files are counted. `/simplify` alone spawns four reviewers,
  so a number that skipped them would be wrong by most of the bill — and they are reported
  separately, because "of which N% was subagents" is the interesting half of it.
* **Cache writes are 1.25x (5m) or 2x (1h) of input; cache reads are a tenth of it — except
  on Fable, where they are $0.25 against a $10 input, a fortieth.** On a long run the
  cache-read column dwarfs everything else in *tokens* while contributing almost nothing to
  *cost*, so a chip that showed only a token total would be actively misleading — and a flat
  tenth applied to a Fable run overcharges the largest token column by four.

The dollar figure is **list-price equivalent**: what these tokens would cost on the API.
Nobody running this under a subscription is billed it, and the chip says so on hover.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# $ per 1M tokens: (input, output). Cache write is 1.25x input (5m TTL) / 2x (1h).
# Kept in step with victor-skills-private/claude-usage.
PRICES = {
    "opus": (5.0, 25.0),
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "sonnet": (2.0, 10.0),
    "haiku": (1.0, 5.0),
}
# Cache read as a fraction of *that family's* input price, because it is not one fraction.
# The usual rate is a tenth of input, but Fable reads cache at $0.25 against a $10 input —
# 0.025x, not 0.1x. Applying the flat tenth to a Fable run overcharges its cache reads
# fourfold, and on a long agentic run cache reads are most of the tokens, so that lands on
# the total rather than in the noise. A family with no entry keeps the tenth.
CACHE_READ = {"fable": 0.025}
CACHE_READ_DEFAULT = 0.10
LABELS = [
    ("claude-opus-5", "Opus 5"), ("claude-opus-4-8", "Opus 4.8"),
    ("claude-fable-5", "Fable 5"), ("claude-mythos-5", "Mythos 5"),
    ("claude-sonnet-5", "Sonnet 5"), ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-haiku-4-5", "Haiku 4.5"),
]
WEB_SEARCH_PER_1K = 10.0


def family(model: str | None) -> str | None:
    m = (model or "").lower()
    return next((k for k in PRICES if k in m), None)


def label(model: str | None) -> str:
    m = (model or "").lower()
    return next((name for pre, name in LABELS if m.startswith(pre)), m or "synthetic")


def price(fam: str | None, u: dict) -> float:
    if fam not in PRICES:
        return 0.0
    inp, out = PRICES[fam]
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", 0)
    w1 = cc.get("ephemeral_1h_input_tokens", 0)
    if not (w5 or w1):                      # older records carry only the flat total
        w5 = u.get("cache_creation_input_tokens", 0)
    return (
        u.get("input_tokens", 0) * inp
        + u.get("output_tokens", 0) * out
        + w5 * inp * 1.25
        + w1 * inp * 2.0
        + u.get("cache_read_input_tokens", 0) * inp * CACHE_READ.get(fam, CACHE_READ_DEFAULT)
    ) / 1e6


def transcript(session_id: str) -> Path | None:
    """The session's own file, wherever the project slug put it.

    Resolved by id rather than by the working directory on purpose: a review run moves
    around (the skill is invoked from the repo under review, which is often not the folder
    the session started in), so the cwd names the wrong slug about half the time.
    """
    hits = sorted(PROJECTS.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


AGENT_ID_RE = __import__("re").compile(r"agentId[\"\':\s]+([0-9a-f]{12,})")


def subagent_dir(session_file: Path) -> Path:
    """`<session>/subagents/`, beside the parent transcript."""
    return session_file.parent / session_file.stem / "subagents"


def subagent_transcripts(session_file: Path) -> list[Path]:
    """Every agent this session spawned, durable copy first.

    Claude Code writes a subagent twice: `<session>/subagents/agent-<id>.jsonl` beside the
    parent transcript, and a `/tmp/claude-*/…/tasks/<id>.output` that is a symlink into it
    — which a tmp sweep or a reboot takes away, leaving a dangling path this used to count
    as an absent subagent. The durable copy is preferred for that reason and because it is
    the only one with a `.meta.json` naming the agent's type, which is what tells a review
    pass apart from an implementation errand.

    The tmp files remain the fallback for a session whose project folder was cleaned
    instead, and they are found the exact way, not by walking: nothing in the environment
    points at the tasks directory and its parent is keyed by a workspace uuid that is not
    the session id, so a filesystem walk would sweep up a concurrent session's agents from
    the same folder. The parent transcript names every agentId it launched, and each of
    those is a filename. `review-passes.py:subagent_files` resolves them the same way.
    """
    home = subagent_dir(session_file)
    if home.is_dir():
        found = sorted(home.glob("agent-*.jsonl"))
        if found:
            return found
    ids = set(AGENT_ID_RE.findall(session_file.read_text(encoding="utf-8", errors="replace")))
    found = []
    for agent_id in sorted(ids):
        for root in ("/private/tmp", "/tmp"):
            hit = next(Path(root).glob(f"claude-*/*/*/tasks/{agent_id}.output"), None)
            if hit:
                found.append(hit)
                break
    return found


# The `name` a forked `/code-review` carries in its `.meta.json`. It is the phase-2
# boundary for free: a reviewer's transcript is that pass and nothing else, so it needs no
# window at all — and the earliest and latest timestamps across those files ARE when the
# review started and stopped.
REVIEW_AGENT_NAME = "code-review"


def review_agent_files(session_file: Path, name: str = REVIEW_AGENT_NAME) -> list[Path]:
    """The forked reviewers of this session, by what their `.meta.json` says they are.

    By `name`, not by prose in the description: an agent type is recorded by the harness,
    a description is written by whoever spawned it. `pass_costs` learned this the hard way
    — matching on description billed two implementation tasks to the review.
    """
    home = subagent_dir(session_file)
    if not home.is_dir():
        return []
    out = []
    for jsonl in sorted(home.glob("agent-*.jsonl")):
        meta_path = jsonl.with_suffix(".meta.json")
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(meta.get("name") or "") == name:
            out.append(jsonl)
    return out


def agent_span(files) -> tuple["dt.datetime | None", "dt.datetime | None"]:
    """The earliest and latest timestamp across a set of agent transcripts."""
    stamps = []
    for path in files:
        path = Path(path)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"timestamp"' not in line:
                continue
            try:
                when = _parse_iso((json.loads(line) or {}).get("timestamp"))
            except json.JSONDecodeError:
                continue
            if when is not None:
                stamps.append(when)
    return (min(stamps), max(stamps)) if stamps else (None, None)


def _scan(path: Path, since: dt.datetime | None, force_side: bool, best: dict,
          until: dt.datetime | None = None) -> None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        stamp = d.get("timestamp")
        when = None
        if stamp:
            try:
                when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                when = None
        if since is not None and when is not None and when < since:
            continue
        # The far end of the window. It exists for the one caller that measures a session
        # it does not own -- the conversation that WROTE the code, costed between its
        # first and last edit to the change set. Without it that row would charge this
        # review for everything that session ever did afterwards, which is a different
        # question and a much larger number.
        if until is not None and when is not None and when > until:
            continue
        mid = msg.get("id") or f"{d.get('uuid')}"
        prev = best.get(mid)
        # `key` is the dedup tie-break only (a missing timestamp must still lose to a real
        # one, so it sorts last); `when` rides along separately and stays None when the
        # stamp was missing or unparseable, because a consumer that places turns in time
        # (tab_costs, below) has to tell "no timestamp" apart from "very late timestamp" —
        # collapsing them into one value the way `key` does would silently misfile a
        # timestamp-less turn into whichever window happens to be open-ended.
        key = when or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        if prev is None or key < prev[0]:
            best[mid] = (key, msg.get("model"), usage,
                         force_side or bool(d.get("isSidechain")), when)


def gather_turns(path: Path, since: dt.datetime | None, include_subagents: bool = True,
                 until: dt.datetime | None = None):
    """The deduped, priced turns `collect()` and `tab_costs()` both work from.

    One scan, shared, so the two never drift on what counts as a turn — the same
    dedupe-by-`message.id` and subagent-transcript discovery either would reimplement
    otherwise."""
    best: dict[str, tuple] = {}
    _scan(path, since, False, best, until)
    agents = subagent_transcripts(path) if include_subagents else []
    for extra in agents:
        _scan(extra, since, True, best, until)
    return list(best.values()), len(agents)


def collect(path: Path, since: dt.datetime | None, include_subagents: bool = True,
           turns=None, n_agents: int | None = None, until: dt.datetime | None = None) -> dict:
    """`turns`/`n_agents` let a caller that already ran `gather_turns()` (the `--chip`
    path, when it also needs the residual) reuse that scan instead of reading the whole
    transcript — subagents included — a second time."""
    if turns is None:
        turns, n_agents = gather_turns(path, since, include_subagents, until)

    totals = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    per_model: dict[str, dict] = {}
    cost = sub_cost = 0.0
    msgs = subagent_msgs = searches = 0
    for _, model, u, side, _when in turns:
        c = price(family(model), u)
        cost += c
        msgs += 1
        if side:
            sub_cost += c
            subagent_msgs += 1
        totals["in"] += u.get("input_tokens", 0)
        totals["out"] += u.get("output_tokens", 0)
        totals["cache_write"] += u.get("cache_creation_input_tokens", 0)
        totals["cache_read"] += u.get("cache_read_input_tokens", 0)
        row = per_model.setdefault(label(model), {"tokens": 0, "cost": 0.0, "messages": 0})
        row["tokens"] += sum(u.get(k, 0) for k in
                             ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens"))
        row["cost"] += c
        row["messages"] += 1
        n = (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        if n:
            searches += n
            cost += n * WEB_SEARCH_PER_1K / 1000
    return {
        "subagents": n_agents,
        "tokens": sum(totals.values()), "breakdown": totals, "cost": cost,
        "subagent_cost": sub_cost, "messages": msgs, "subagent_messages": subagent_msgs,
        "web_searches": searches,
        "models": dict(sorted(per_model.items(), key=lambda kv: -kv[1]["cost"])),
    }


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def money(c: float) -> str:
    return f"${c:.2f}" if c >= 0.01 else "<$0.01"


# --------------------------------------------------------------------------------------- #
# Per-tab cost. Nothing today links a turn to a tab, so the link is made by whoever runs
# the pipeline: `steps-ledger.py start <tabs> …` / `… end <index>` stamps a start/end
# window into `.human-review/.steps.json` as each step runs, naming the tab(s) it feeds.
# This turns that ledger, plus the same deduped/priced turns `collect()` uses, into a
# cost per tab — and a residual bucket for whatever fell outside every window.
# --------------------------------------------------------------------------------------- #

# The one tab id that is not a tab. Step 9 — writing `content.json`: the findings prose, the
# sections, every tab's body — is the single most expensive stretch of a real run, and it is
# also the one stretch that cannot be attributed to a tab, because all ten tabs' prose is
# written in one interleaved go. Left unstamped it lands in the residual along with genuine
# dead time, which is how a breakdown ends up 90%+ "not one tab's" and teaches the reader to
# ignore the rows above it. Stamping it against a reserved pseudo-tab moves the largest and
# most explicable chunk out of the mystery bucket without pretending it belongs to a tab.
GUIDE_TAB = "guide"


def _parse_iso(raw) -> "dt.datetime | None":
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_steps(path: Path) -> tuple[list[dict], bool]:
    """The ledger `steps-ledger.py` writes, parsed into `{tabs, label, start, end}` records.

    Returns `(records, found)`. `found` is False for "the file is not there, or is not
    something this ever wrote" — the whole feature was never wired up on this run. That
    reads differently from "found, and simply says nothing about this tab", which is one
    uninstrumented step on a run that otherwise measures itself. Conflating the two would
    make an adopted-but-partial ledger look identical to one that was never adopted."""
    if not path.is_file():
        return [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], False
    if not isinstance(data, list):
        return [], False
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        tabs = [t for t in (r.get("tabs") or []) if isinstance(t, str) and t]
        start = _parse_iso(r.get("start"))
        if not tabs or start is None:
            continue
        end = _parse_iso(r.get("end"))
        out.append({"tabs": tabs, "label": r.get("label") or "", "start": start, "end": end})
    return out, True


def tab_costs(turns, steps: list[dict], wanted: list[str]) -> dict:
    """Attribute each turn's cost to the tab(s) whose step window it falls in.

    A turn inside more than one matching tab's window — one step feeding two tabs at
    once, or two windows that happen to overlap — has its cost split evenly across every
    tab it falls into. Not because the work was literally divisible, but so the per-tab
    numbers add up to the run total instead of double-counting it. A turn inside none of
    the windows, or with no timestamp to place it, goes to `residual`: real cost with no
    single tab to blame it on (Step 0, assembling `content.json`, or any step that never
    ran through the ledger). Nothing is dropped and nothing is spread beyond the tabs a
    step actually named.

    `wanted` fixes which tabs get an entry in the return value even when the ledger never
    mentions them — that absence (`has_closed: False, has_unclosed: False`) is itself the
    "never stamped" signal a caller needs, not something it has to infer from a missing key.

    The reverse mismatch is the dangerous one and is reported as `unknown`: a tab id the
    ledger names that is not in `wanted`. Attribution has to ignore those turns (there is no
    row to put them in, so they land in `residual`), and ignoring them *quietly* is how the
    binding rots — rename a tab in `content.json` without touching its step wrap and every
    number for it silently becomes "not measured", which reads as "we forgot to instrument
    it" rather than "these two files disagree". Naming the orphaned ids is what lets the
    caller say which of the two it is.
    """
    per_tab = {t: {"cost": 0.0, "tokens": 0.0, "messages": 0,
                    "has_closed": False, "has_unclosed": False}
               for t in [*wanted, GUIDE_TAB]}
    unknown: set[str] = set()
    closed = []
    for s in steps:
        is_closed = s["end"] is not None and s["end"] >= s["start"]
        if is_closed:
            closed.append(s)
        for t in s["tabs"]:
            if t in per_tab:
                per_tab[t]["has_closed" if is_closed else "has_unclosed"] = True
            else:
                unknown.add(t)

    # The unattributed cost is split by where the turn came from rather than reported as one
    # lump. `side` is already carried on every turn, and the two halves answer different
    # questions: subagent turns outside a step are work that was delegated but not bracketed
    # (instrumentable, in principle), while the parent's are the orchestrating conversation —
    # reading, deciding, recovering — which never belonged to a step in the first place.
    parts = {k: {"cost": 0.0, "tokens": 0.0, "messages": 0}
             for k in ("subagent", "conversation")}
    for _, model, u, side, when in turns:
        c = price(family(model), u)
        tok = sum(u.get(k, 0) for k in
                  ("input_tokens", "output_tokens",
                   "cache_creation_input_tokens", "cache_read_input_tokens"))
        hit = set()
        if when is not None:
            for s in closed:
                if s["start"] <= when <= s["end"]:
                    hit |= {t for t in s["tabs"] if t in per_tab}
        if not hit:
            bucket = parts["subagent" if side else "conversation"]
            bucket["cost"] += c
            bucket["tokens"] += tok
            bucket["messages"] += 1
            continue
        share = 1.0 / len(hit)
        for t in hit:
            per_tab[t]["cost"] += c * share
            per_tab[t]["tokens"] += tok * share
            per_tab[t]["messages"] += 1

    guide = per_tab.pop(GUIDE_TAB)
    parts = {"guide": {k: guide[k] for k in ("cost", "tokens", "messages")}, **parts}
    # `residual` stays the sum of the parts, so the invariant every caller relies on —
    # tabs + residual == the scope chip's total — survives the decomposition untouched.
    residual = {k: sum(p[k] for p in parts.values()) for k in ("cost", "tokens", "messages")}
    return {"tabs": per_tab, "residual": residual, "residual_parts": parts,
            "unknown": sorted(unknown)}


def tab_cost_tip(row: dict) -> str:
    """Plain text — `data-tip` is read with `textContent`, not innerHTML."""
    if not row["has_closed"]:
        why = (" its step started but never recorded finishing" if row["has_unclosed"]
               else " no step in the ledger named it")
        return f"cost: not measured for this tab —{why}."
    caveat = (" One of its steps started but never recorded finishing, so this is a "
              "lower bound." if row["has_unclosed"] else "")
    return (f"{money(row['cost'])} · {human(round(row['tokens']))} tok measured for "
            f"this tab (list-price).{caveat}")


def tab_cost_report(session: str | None, since: "dt.datetime | None", steps_path: Path,
                    tabs: list[str], include_subagents: bool = True,
                    turns=None) -> dict:
    """Everything a page builder needs to put an honest cost tooltip on every tab.

    Always returns an entry for every tab in `tabs` — never an empty dict a caller has to
    special-case — because "we could not measure this" has to reach the reader as a
    sentence on the tab, not as a quietly absent tooltip that looks the same as a measured
    zero. `available` is about the transcript (no session id, no transcript on disk);
    `ledger` is the separate, later question of whether `.steps.json` exists at all.
    """
    def blank(reason: str) -> dict:
        tab_tip = f"cost: not measured for this tab — {reason}."
        return {
            "available": False, "ledger": False, "reason": reason,
            "tabs": {t: {"measured": False, "cost": 0.0, "tokens": 0, "messages": 0,
                        "tip": tab_tip} for t in tabs},
            "residual": {"measured": False, "cost": 0.0, "tokens": 0, "messages": 0,
                        "tip": f"cost: not measured — {reason}."},
        }

    if not session:
        return blank("no session id ($CLAUDE_CODE_SESSION_ID unset)")
    path = transcript(session)
    if path is None:
        return blank(f"no transcript for session {session}")

    steps, ledger_found = load_steps(steps_path)
    if not ledger_found:
        return {**blank(f"no step ledger at {steps_path}"), "available": True}

    # `turns` lets `ledger()` hand over the scan it already paid for. A transcript this
    # size is read in seconds and the ledger needs the same turns twice -- once priced per
    # tab, once totalled -- so scanning it again is the whole of that second wait.
    if turns is None:
        turns, _ = gather_turns(path, since, include_subagents)
    result = tab_costs(turns, steps, tabs)
    tabs_out = {
        t: {"measured": row["has_closed"], "cost": row["cost"],
            "tokens": round(row["tokens"]), "messages": row["messages"],
            "tip": tab_cost_tip(row)}
        for t, row in result["tabs"].items()
    }
    r = result["residual"]
    parts = {
        k: {"measured": True, "cost": v["cost"], "tokens": round(v["tokens"]),
            "messages": v["messages"]}
        for k, v in (result.get("residual_parts") or {}).items()
    }
    residual_tip = (
        f"{money(r['cost'])} · {human(round(r['tokens']))} tok of this run's cost is "
        "not attributed to any single tab — assembling the guide itself, plus any step "
        "whose window did not cover it."
    )
    # Drift between a step wrap and the tab list is reported as the *reason* a tab came
    # back unmeasured, because that is the sentence the page already prints in that case —
    # so the mismatch reaches a human reading the breakdown, not just a log nobody opens.
    # Stderr as well, for the run that is watching its own output.
    orphans = result.get("unknown") or []
    reason = None
    if orphans:
        reason = (f"the step ledger names tab(s) {', '.join(orphans)}, which this page does "
                  "not have — a step wrap and the tab list have drifted apart, or a step "
                  "stamped a tab whose content was then dropped")
        print(f"[review-cost] {reason}. Wanted: {', '.join(tabs) or '(none)'}",
              file=sys.stderr)
    return {
        "available": True, "ledger": True, "reason": reason,
        "unknown_tabs": orphans,
        "tabs": tabs_out,
        "residual": {"measured": True, "cost": r["cost"], "tokens": round(r["tokens"]),
                    "messages": r["messages"], "tip": residual_tip},
        "residual_parts": parts,
    }


AUTHORING = Path(__file__).resolve().parent / "authoring-sessions.py"


def _stamp(raw: str | None) -> "dt.datetime | None":
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def authoring_cost(base: str, root: Path, exclude: str | None = None) -> dict:
    """What the conversation that WROTE the code cost — the other half of the bill.

    A review page has always been able to say what reviewing cost, because it is the run
    doing the reviewing. The number beside it, what producing the thing under review cost,
    was on the same disk the whole time and never asked for. It is the more interesting of
    the two: a reader deciding whether this way of working pays for itself needs both.

    Who wrote it is not guessed here. `authoring-sessions.py` already answers it from tool
    calls — an `Edit` naming a changed file, or a shell command that demonstrably writes
    one — and that script's evidence rules are the ones that matter, so this is a caller,
    not a second implementation.

    **Only sessions that used the edit tools count.** The scan also returns sessions whose
    sole evidence is a shell command, and on a repo this large that is a dozen of them: a
    `git checkout`, a `sed` in a script, an unrelated conversation that happened to write
    a file with the same path. Charging the feature for all of those would turn a measured
    number into an accumulation of other people's afternoons. Where *nothing* used the edit
    tools the strongest shell-only session is taken instead, flagged `weak`, so the page can
    hedge in words rather than print a confident wrong total.

    **Each session is costed between its first and its last edit to these files**, not over
    its whole length. A conversation that wrote this feature in the morning and did
    something else all afternoon is charged for the morning. That window is a bound, not a
    fence — work inside it that belonged to something else is still counted — and the page
    says so rather than implying a precision the transcript cannot support.
    """
    proc = subprocess.run(
        [sys.executable, str(AUTHORING), "--base", base, "--json"],
        cwd=root, capture_output=True, text=True,
    )
    if not proc.stdout.strip():
        line = (proc.stderr.strip().splitlines() or ["authoring-sessions.py said nothing"])[-1]
        return {"measured": False, "reason": line, "sessions": [], "cost": 0.0, "tokens": 0}
    try:
        found = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"measured": False, "reason": "authoring-sessions.py returned no JSON",
                "sessions": [], "cost": 0.0, "tokens": 0}

    rows = [r for r in found.get("sessions") or [] if r.get("session") != exclude]
    strong = [r for r in rows if r.get("edits")]
    weak = not strong
    picked = strong or rows[:1]
    if not picked:
        return {"measured": False, "mode": found.get("mode"), "sessions": [],
                "cost": 0.0, "tokens": 0,
                "reason": "no conversation on disk wrote these files — a branch from "
                          "somebody else, or transcripts since cleaned up"}

    out, total_cost, total_tokens = [], 0.0, 0
    for r in picked:
        path = Path(r.get("transcript") or "")
        if not path.is_file():
            continue
        c = collect(path, _stamp(r.get("first")), until=_stamp(r.get("last")))
        total_cost += c["cost"]
        total_tokens += c["tokens"]
        out.append({"session": r["session"], "cost": c["cost"], "tokens": c["tokens"],
                    "messages": c["messages"], "subagents": c["subagents"],
                    "models": list(c["models"]), "first": r.get("first"),
                    "last": r.get("last"), "edits": r.get("edits", 0),
                    "bash": r.get("bash", 0), "files": len(r.get("files") or []),
                    "current": r.get("current", False)})
    return {"measured": bool(out), "mode": found.get("mode"), "weak": weak,
            "sessions": out, "cost": total_cost, "tokens": total_tokens,
            "reason": None if out else "the authoring transcripts are no longer on disk"}


PASSES = Path(__file__).resolve().parent / "review-passes.py"

# A pass that applies what it finds, told apart from one that only reports. `/simplify`
# rewrites by definition; `/code-review` only does with `--fix`. Name-based, because the
# invocation is the only record of intent there is -- a transcript cannot be asked whether
# an edit came from a finding or from the conversation around it.
FIXING = ("--fix", "/simplify", "simplify")


def pass_costs(session: str, since: "dt.datetime | None" = None) -> dict:
    """What the review passes cost, split into finding and fixing.

    `review-passes.py` already answers which passes ran and where each one's turns are;
    this prices them. A pass that **forked** is priced exactly — a subagent has a
    transcript of its own, and everything in it is that pass and nothing else. A pass that
    ran **inline** cannot be: its turns are interleaved with the conversation that invoked
    it, with no marker saying where it stopped. Those are counted and named, not estimated,
    and their money stays in the residual where it actually landed.

    Each group carries `earlier` beside `cost`: the part spent *before* the run's start
    marker. That is the part the run's own total cannot already contain, and it is the only
    part a grand total may add — a pass fired after the guide started is inside the run's
    number, and adding it again would bill it twice. Usually `earlier` IS the whole cost,
    because the human runs the passes and then asks for the page; the split exists so the
    arithmetic does not quietly depend on that habit.
    """
    proc = subprocess.run(
        [sys.executable, str(PASSES), "--session", session, "--json"],
        capture_output=True, text=True,
    )
    if not proc.stdout.strip():
        return {"measured": False, "groups": {}, "inline": 0}
    try:
        found = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"measured": False, "groups": {}, "inline": 0}

    groups: dict[str, dict] = {}
    inline = 0
    seen: set[str] = set()
    for p in found.get("passes") or []:
        src = Path(p.get("source") or "")
        forked = str(p.get("how", "")).startswith("subagent")
        if not forked:
            inline += 1
            continue
        if not src.is_file() or str(src) in seen:
            continue
        invoked = str(p.get("invoked_as") or "")
        # Only an actual invocation counts. `review-passes.py` also recognises a forked
        # agent by its *type*, which sweeps up every subagent a code-review-shaped agent
        # ever ran -- on this repo that meant two implementation tasks ("Fix ds-audit
        # asset-prefix bug") being billed to the review. A pass the human asked for is
        # spelled `/code-review` or `/simplify`; a description in prose is somebody else's
        # errand.
        if not invoked.startswith("/"):
            continue
        # Deliberately NOT filtered by the run's start marker. The passes run *before*
        # `/human-review` does -- that is the whole design, the guide harvests a review
        # rather than paying to re-derive one -- so every pass on this change set is
        # earlier than the run whose page reports it.
        seen.add(str(src))
        kind = "fixing" if any(m in invoked for m in FIXING) else "finding"
        # `include_subagents=False`: a subagent transcript is a leaf. Asking for its own
        # agents would send `subagent_transcripts` hunting for ids in a file that names
        # none, and any it did find would be double-counted against the parent run.
        c = collect(src, None, include_subagents=False)
        earlier = (collect(src, None, include_subagents=False, until=since)["cost"]
                   if since is not None else c["cost"])
        row = groups.setdefault(kind, {"cost": 0.0, "earlier": 0.0, "tokens": 0,
                                       "messages": 0, "passes": 0, "invoked": []})
        row["cost"] += c["cost"]
        row["earlier"] += earlier
        row["tokens"] += c["tokens"]
        row["messages"] += c["messages"]
        row["passes"] += 1
        if invoked and invoked not in row["invoked"]:
            row["invoked"].append(invoked)
    return {"measured": True, "groups": groups, "inline": inline}


# --------------------------------------------------------------------------------------- #
# Cost per PHASE, which is a different question from cost per tab. A tab is a piece of the
# page; a phase is a piece of the work — writing the code, reviewing it, taking the review's
# advice, writing down what was declined. The reader arriving at the `$` tab wants the
# second breakdown, and until now the page could only offer the first.
# --------------------------------------------------------------------------------------- #

DEFAULT_POINTS = "review-points.md"

PHASE_LABELS = {
    "implementation": "implementation",
    "code_review": "code-review agents",
    "post_review_fixes": "post-review fixes",
    "review_points": "review-points",
    "video": "demo video",
    "images": "view images",
    "page_build": "page build",
}


@__import__("functools").lru_cache(maxsize=1)
def _authoring_module():
    """`authoring-sessions.py` as a module, for its evidence rules and nothing else.

    Hyphenated, so not importable by name. Imported rather than reimplemented because
    `WRITE_TOOLS` and `shell_writes` are the answer to "did this turn write that file",
    and a second copy of that answer is a second thing to keep in step with the harness.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("authoring_sessions", AUTHORING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_window(path: Path, rel: str) -> tuple["dt.datetime | None", "dt.datetime | None"]:
    """When this transcript first and last wrote `rel`.

    The same evidence `authoring-sessions.py` uses to decide who wrote the code, pointed at
    one file: an edit tool naming it, or a shell command that demonstrably writes it. Turns
    that merely *read* it are not evidence, which is the whole reason the review-points row
    can be told apart from the fixes around it.
    """
    auth = _authoring_module()
    stamps: list[dt.datetime] = []
    base = Path(rel).name
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    with handle:
        for line in handle:
            if base not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _parse_iso(rec.get("timestamp"))
            content = ((rec.get("message") or {}).get("content")) or []
            if when is None or not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, inp = block.get("name", ""), block.get("input") or {}
                if name in auth.WRITE_TOOLS:
                    paths = [inp.get("file_path") or inp.get("path")
                             or inp.get("notebook_path")]
                    paths += [e.get("file_path") for e in (inp.get("edits") or [])
                              if isinstance(e, dict)]
                    if any(p and str(p).endswith(rel) or (p and Path(str(p)).name == base)
                           for p in paths):
                        stamps.append(when)
                elif name == "Bash":
                    cmd = str(inp.get("command") or "")
                    if rel in cmd and auth.shell_writes(cmd, rel):
                        stamps.append(when)
    return (min(stamps), max(stamps)) if stamps else (None, None)


def _price_turns(turns) -> dict:
    cost = tokens = 0.0
    for _key, model, u, _side, _when in turns:
        cost += price(family(model), u)
        tokens += sum(u.get(k, 0) for k in
                      ("input_tokens", "output_tokens",
                       "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"cost": cost, "tokens": round(tokens), "messages": len(turns)}


def _window(path: Path, since, until, skip=()) -> dict:
    """The parent's turns in a window, plus its own agents' — but never the reviewers'.

    A reviewer's transcript is priced exactly, as its own row, so counting it here as well
    would bill the review twice. Everything else the session forked belongs to whichever
    phase it ran in, and is charged there.
    """
    best: dict[str, tuple] = {}
    _scan(path, since, False, best, until)
    skip = {str(s) for s in skip}
    for extra in subagent_transcripts(path):
        if str(extra) in skip:
            continue
        _scan(extra, since, True, best, until)
    return _price_turns(list(best.values()))


def _row(key: str, measured: bool, data: dict | None = None, reason: str | None = None,
         window=None, detail: str | None = None) -> dict:
    """One line of the breakdown. An unmeasurable phase carries a reason, never a zero.

    `$0.00` and "we could not date this" render identically to a reader and mean opposite
    things — one is a phase that cost nothing, the other is a phase whose cost is sitting
    in some other row. The reason is what stops the table from quietly balancing itself.
    """
    out = {"key": key, "label": PHASE_LABELS.get(key, key), "measured": measured,
           "cost": 0.0, "tokens": 0, "messages": 0, "reason": reason, "detail": detail,
           "window": [w.isoformat() if w else None for w in (window or (None, None))]}
    if data:
        out.update({k: data[k] for k in ("cost", "tokens", "messages")})
    return out


def phase_costs(session: str | None, t0, t1, t2, t3, t4, reviewer_files=(),
                run_session: str | None = None, steps_path: Path | None = None,
                points_file: str = DEFAULT_POINTS) -> dict:
    """What each phase of the work cost, from the boundaries somebody else derived.

    The boundaries are `session-cost.py`'s job — they come out of git trailers, the step
    ledger and the reviewers' own transcripts — and the pricing is this module's, so the
    two never disagree about what a turn costs. Rows:

      1. **implementation** — the coding session between its first edit and commit #1.
      2. **code-review agents** — each forked reviewer's whole transcript. Exact, needing
         no window: a subagent's file is that pass and nothing else.
      3. **post-review fixes** — the parent's turns between the last reviewer turn and
         commit #2, less row 4.
      4. **review-points** — the turns that wrote `review-points.md`, found by the same
         evidence `authoring-sessions.py` uses. A sub-window of row 3, subtracted so the
         rows still add up to the total.
      5/6. **demo video / view images** — not the coding session at all: the `video` and
         `dsaudit` steps of the run that built the page, priced by `tab_costs`.
      7. **page build** — that run's `guide` pseudo-tab and residual.

    **Every window is a bound, not a fence** — the caveat `authoring_cost` already carries.
    Work inside a window that belonged to something else is still counted, and where rows 3
    and 4 interleave the split between them is arbitrary. The windows are returned so the
    page can print them instead of implying a precision the transcript cannot support.
    """
    rows: list[dict] = []
    path = transcript(session) if session else None
    reviewer_files = [Path(f) for f in reviewer_files or ()]

    if path is None:
        why = ("no session id — nothing names the conversation that wrote the code"
               if not session else f"no transcript on disk for session {session}")
        for key in ("implementation", "code_review", "post_review_fixes", "review_points"):
            rows.append(_row(key, False, reason=why))
    else:
        if t0 and t1:
            rows.append(_row("implementation", True,
                             _window(path, t0, t1, skip=reviewer_files), window=(t0, t1),
                             detail="first edit to the change set → commit #1"))
        else:
            rows.append(_row("implementation", False, window=(t0, t1), reason=(
                "no first edit found in the transcript" if not t0 else
                "which commit is the implementation is not recorded — no Implements "
                "trailer, so the phase has no end")))

        live = [f for f in reviewer_files if f.is_file()]
        if live:
            data = {"cost": 0.0, "tokens": 0, "messages": 0}
            for f in live:
                # include_subagents=False: a subagent transcript is a leaf, and any agent
                # id it happens to mention belongs to the parent, which already paid.
                c = collect(f, None, include_subagents=False)
                data["cost"] += c["cost"]
                data["tokens"] += c["tokens"]
                data["messages"] += c["messages"]
            rows.append(_row("code_review", True, data, window=(t2, t3),
                             detail=f"{len(live)} forked reviewer(s), whole transcripts"))
        else:
            rows.append(_row("code_review", False, window=(t2, t3), reason=(
                "no forked reviewer transcript — the review ran inline, interleaved with "
                "the conversation that invoked it, and its turns cannot be separated from "
                "the turns around them")))

        points_from, points_to = write_window(path, points_file)
        points = None
        if points_from and points_to:
            points = _window(path, points_from, points_to, skip=reviewer_files)

        if t3 and t4:
            fixes = _window(path, t3, t4, skip=reviewer_files)
            detail = "last reviewer turn → commit #2"
            if points:
                for k in ("cost", "tokens", "messages"):
                    fixes[k] = max(fixes[k] - points[k], 0)
                detail += f", less the {points['messages']} turn(s) that wrote {points_file}"
            rows.append(_row("post_review_fixes", True, fixes, window=(t3, t4),
                             detail=detail))
        else:
            rows.append(_row("post_review_fixes", False, window=(t3, t4), reason=(
                "the review's end and commit #2 are not both dated, so the stretch "
                "between them is not a window")))

        if points:
            rows.append(_row("review_points", True, points,
                             window=(points_from, points_to),
                             detail=f"first → last write of {points_file}"))
        else:
            rows.append(_row("review_points", False, reason=(
                f"no turn in this session wrote {points_file} — it was written somewhere "
                "else, or not at all")))

    # The three rows that belong to the run which BUILT the page, not to the one that wrote
    # the code. They are already measured, by the step ledger; this only puts them in the
    # same table, so the reader sees one bill instead of two halves that never meet.
    if run_session and steps_path is not None:
        # Every tab the ledger names, not just the two that get their own row: asking for a
        # narrow list makes `tab_cost_report` report all the others as drift, and that
        # warning would then be printed as the *reason* the video row is unmeasured. The
        # other tabs are not dropped either — their work is page building, and that is the
        # row it lands in.
        steps, _found = load_steps(Path(steps_path))
        wanted = sorted({t for s in steps for t in s["tabs"] if t != GUIDE_TAB}
                        | {"video", "dsaudit"})
        report = tab_cost_report(run_session, None, Path(steps_path), wanted)
        for key, tab in (("video", "video"), ("images", "dsaudit")):
            row = report["tabs"].get(tab) or {}
            if row.get("measured"):
                rows.append(_row(key, True, {"cost": row["cost"], "tokens": row["tokens"],
                                             "messages": row["messages"]},
                                 detail=f"the '{tab}' step of the page-building run"))
            else:
                rows.append(_row(key, False, reason=(row.get("tip")
                                                     or report.get("reason")
                                                     or "no step named it")))
        residual = report.get("residual") or {}
        if residual.get("measured"):
            page = {k: residual[k] for k in ("cost", "tokens", "messages")}
            others = [t for t in wanted if t not in ("video", "dsaudit")]
            for tab in others:
                row = report["tabs"].get(tab) or {}
                for k in ("cost", "tokens", "messages"):
                    page[k] += row.get(k) or 0
            rows.append(_row("page_build", True, page, detail=(
                "assembling the guide, the other " f"{len(others)} tab(s), and whatever no "
                "step covered" if others else
                "assembling the guide, plus whatever no step covered")))
        else:
            rows.append(_row("page_build", False,
                             reason=residual.get("tip") or "the page-building run is not "
                                                           "measured"))
    else:
        why = ("no step ledger for the run that built the page — pass --steps-file and the "
               "building run's session id")
        for key in ("video", "images", "page_build"):
            rows.append(_row(key, False, reason=why))

    measured = [r for r in rows if r["measured"]]
    return {
        "rows": rows,
        "measured": bool(measured),
        "unmeasured": [r["key"] for r in rows if not r["measured"]],
        "cost": sum(r["cost"] for r in measured),
        "tokens": sum(r["tokens"] for r in measured),
        "messages": sum(r["messages"] for r in measured),
        "session": session, "run_session": run_session,
        "points_file": points_file,
        "boundaries": {name: (w.isoformat() if w else None) for name, w in
                       (("t0", t0), ("t1", t1), ("t2", t2), ("t3", t3), ("t4", t4))},
    }


PHASES_FILE = ".human-review/phases.json"


def load_phases(path: Path) -> dict:
    """The phase breakdown `session-cost.py` left behind, if it ran.

    Read from a file rather than recomputed here on purpose: the boundaries come out of git
    trailers and the reviewers' transcripts, which is `session-cost.py`'s subject and not
    this module's, and deriving them twice is how the `$` tab and the table Victor reads in
    the terminal would come to disagree. Absent, the page falls back to the writing/review
    split it has always drawn — and is told *why*, so it can say so.
    """
    if not Path(path).is_file():
        return {"measured": False, "rows": [],
                "reason": f"no {path} — run session-cost.py to date the phases"}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"measured": False, "rows": [], "reason": f"{path} is unreadable ({exc})"}
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        return {"measured": False, "rows": [],
                "reason": f"{path} is not something session-cost.py wrote"}
    return doc


def ledger(session: str | None, since: "dt.datetime | None", steps_path: Path,
           tabs: list[str], base: str, root: Path,
           include_subagents: bool = True, phases_file: str = PHASES_FILE) -> dict:
    """The whole bill for this change set, in the order the money was spent.

    The page used to state one number — what the review run cost — in a chip, with a
    per-tab breakdown hanging off it. That answered "what did this page cost to make",
    which is the least interesting of the questions a reader has. The one they actually
    arrive with is "what did this change cost", and the review is the smaller half of it:
    somebody wrote the code first, in a conversation that is on the same disk.

    Four sources, each measured by whatever can actually see it, and each saying so:

      * **writing** — `authoring_cost`, the conversation that edited these files;
      * **finding / fixing** — `pass_costs`, the review passes that forked;
      * **the tabs** — `tab_cost_report`, every step the ledger timed;
      * **the rest of the run** — the residual, already broken into guide, subagent and
        conversation by `tab_costs`.

    They are not disjoint by construction and the report does not pretend otherwise:
    `run` is the review run's own measured total, and the tab and residual rows sum to it.
    The passes are a *view inside* that total, not an addition to it, which is why they are
    reported separately rather than added — the grand total is writing plus the run.
    """
    run = {"measured": False, "cost": 0.0, "tokens": 0, "messages": 0, "models": []}
    passes = {"measured": False, "groups": {}, "inline": 0}
    turns = n_agents = None
    path = transcript(session) if session else None
    if path is not None:
        turns, n_agents = gather_turns(path, since, include_subagents)
        c = collect(path, since, include_subagents=include_subagents,
                    turns=turns, n_agents=n_agents)
        run = {"measured": True, "cost": c["cost"], "tokens": c["tokens"],
               "messages": c["messages"], "subagent_cost": c["subagent_cost"],
               "subagents": c["subagents"], "models": list(c["models"])}
        passes = pass_costs(session, since)
    tabs_report = tab_cost_report(session, since, steps_path, tabs,
                                  include_subagents=include_subagents, turns=turns)
    writing = authoring_cost(base, root, exclude=session)
    # Only the part of the passes that predates the run is added; the rest is already
    # inside `run`. See `pass_costs` for why that distinction is kept rather than assumed.
    earlier = sum(g.get("earlier") or 0.0 for g in (passes.get("groups") or {}).values())
    total = (writing.get("cost") or 0.0) + (run.get("cost") or 0.0) + earlier
    return {"writing": writing, "run": run, "passes": passes, "tabs": tabs_report,
            "passes_added": earlier, "total": total,
            "total_tokens": (writing.get("tokens") or 0) + (run.get("tokens") or 0),
            # A view *inside* the same money, cut by phase of the work rather than by piece
            # of the page. Not added to `total`: implementation + review + fixes is the same
            # bill as writing + run, counted a second way, and a table that summed both
            # would double every dollar on it.
            "phases": load_phases(root / phases_file)}


def _resolve_since(since_file: str, since_raw: str | None) -> "dt.datetime | None":
    """The run's own start marker, or an explicit override — shared by every mode, so the
    `{"auto":"cost"}` chip and the per-tab report never disagree about where "this run"
    begins."""
    raw = since_raw
    if not raw:
        marker = Path(since_file)
        if marker.is_file():
            raw = marker.read_text(encoding="utf-8").strip()
    if not raw:
        print("[review-cost] no start marker — counting the WHOLE session, which is more "
              "than this review", file=sys.stderr)
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        print(f"[review-cost] unparseable timestamp {raw!r} — counting the whole session",
              file=sys.stderr)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--since-file", default=".human-review/.started",
                    help="file whose contents are an ISO timestamp marking the run's start")
    ap.add_argument("--since", help="ISO timestamp, overriding --since-file")
    ap.add_argument("--chip", action="store_true", help="emit the scope chip as JSON")
    ap.add_argument("--json", action="store_true", help="emit the full breakdown as JSON")
    ap.add_argument("--no-subagents", action="store_true",
                    help="count only the parent session's turns")
    ap.add_argument("--tab-costs", action="store_true",
                    help="emit a cost report per tab (needs --tabs) as JSON")
    ap.add_argument("--tabs", help="comma-separated tab ids to report on, with --tab-costs")
    ap.add_argument("--steps-file", default=".human-review/.steps.json",
                    help="the start/end ledger steps-ledger.py writes")
    ap.add_argument("--ledger", action="store_true",
                    help="emit the whole bill — writing the code, the review passes, "
                         "every tab and the residual — as JSON (needs --tabs)")
    ap.add_argument("--base", default="origin/main",
                    help="what the change set is measured against, for --ledger")
    ap.add_argument("--phases", action="store_true",
                    help="emit the per-phase breakdown as JSON. The boundaries are given, "
                         "not guessed — session-cost.py derives them and is the usual "
                         "caller; this mode exists for a boundary somebody has in hand")
    for name, helptext in (("t0", "first edit to the change set"),
                           ("t1", "commit #1, the implementation"),
                           ("t2", "the review's first turn"),
                           ("t3", "the review's last turn"),
                           ("t4", "commit #2, the review commit")):
        ap.add_argument(f"--{name}", help=f"ISO timestamp: {helptext}")
    ap.add_argument("--reviewer", action="append", default=[], metavar="JSONL",
                    help="a forked reviewer's transcript; repeatable. Default: every "
                         "subagent of --session whose .meta.json is named code-review")
    ap.add_argument("--run-session", help="the session that BUILT the page, for the video, "
                                          "images and page-build rows")
    ap.add_argument("--points-file", default=DEFAULT_POINTS,
                    help="the review-points file whose writing is its own row")
    args = ap.parse_args(argv)

    if args.phases:
        path = transcript(args.session) if args.session else None
        reviewers = [Path(r) for r in args.reviewer]
        if not reviewers and path is not None:
            reviewers = review_agent_files(path)
        stamps = [_parse_iso(getattr(args, n)) for n in ("t0", "t1", "t2", "t3", "t4")]
        print(json.dumps(phase_costs(args.session, *stamps, reviewers,
                                     run_session=args.run_session or args.session,
                                     steps_path=Path(args.steps_file),
                                     points_file=args.points_file), indent=1))
        return 0

    if args.ledger:
        since = _resolve_since(args.since_file, args.since)
        tabs = [t.strip() for t in (args.tabs or "").split(",") if t.strip()]
        print(json.dumps(ledger(args.session, since, Path(args.steps_file), tabs,
                                args.base, Path.cwd(),
                                include_subagents=not args.no_subagents), indent=1))
        return 0

    if args.tab_costs:
        since = _resolve_since(args.since_file, args.since)
        tabs = [t.strip() for t in (args.tabs or "").split(",") if t.strip()]
        report = tab_cost_report(args.session, since, Path(args.steps_file), tabs,
                                 include_subagents=not args.no_subagents)
        print(json.dumps(report, indent=1))
        return 0

    if not args.session:
        print("[review-cost] no session id ($CLAUDE_CODE_SESSION_ID unset) — "
              "the run cannot identify its own transcript", file=sys.stderr)
        return 2
    path = transcript(args.session)
    if path is None:
        print(f"[review-cost] no transcript for session {args.session} under {PROJECTS}",
              file=sys.stderr)
        return 2

    since = _resolve_since(args.since_file, args.since)

    turns, n_agents = gather_turns(path, since, not args.no_subagents)
    r = collect(path, since, include_subagents=not args.no_subagents,
               turns=turns, n_agents=n_agents)
    r["session"] = args.session
    r["since"] = since.isoformat() if since else None
    r["whole_session"] = since is None

    if args.json:
        print(json.dumps(r, indent=1))
        return 0

    if args.chip:
        share = (100 * r["subagent_cost"] / r["cost"]) if r["cost"] else 0
        top = ", ".join(f"{m} {money(v['cost'])}" for m, v in list(r["models"].items())[:3])
        tip = (f"{r['messages']} assistant turns"
               + (f", {r['subagent_messages']} of them from {r['subagents']} subagents "
                  f"({share:.0f}% of the cost)" if r["subagent_messages"] else "")
               + f". {human(r['breakdown']['cache_read'])} of the tokens are cache reads, "
                 f"billed at a tenth of input. {top}. "
               + ("Counted from the start of this run. " if not r["whole_session"]
                  else "No start marker, so this is the WHOLE session, not just the review. ")
               + "List-price equivalent — a subscription is not billed this.")
        # If the pipeline kept a step ledger, the per-tab breakdown has a residual — the
        # cost that landed in no single tab's window (assembling the guide itself, mostly).
        # The scope chip already explains the total, so it is the residual's honest home
        # rather than a number left to float free with nowhere to be shown at all.
        steps, ledger_found = load_steps(Path(args.steps_file))
        if ledger_found:
            wanted = sorted({t for s in steps for t in s["tabs"]})
            residual = tab_costs(turns, steps, wanted)["residual"]
            if residual["messages"]:
                tip += (f" {money(residual['cost'])} of that is not attributed to any "
                       "single tab — assembling the guide itself, plus any step whose "
                       "window did not cover it.")
        print(json.dumps({
            "label": "this review cost",
            "value": f'{money(r["cost"])} <span class="sub">· {human(r["tokens"])} tok</span>',
            "tip": tip,
            # Who actually did the reviewing, most expensive first. The page used to carry
            # a separate `reviewed by Opus 5` chip, typed by hand beside a `LLM review`
            # chip that was computed -- two chips, one of them unverifiable, saying one
            # thing. The run already knows which models it spent its money on, so the name
            # travels with the cost and the two chips become one.
            "models": list(r["models"]),
        }))
        return 0

    print(f"session {args.session}")
    print(f"  {'whole session' if r['whole_session'] else 'since ' + str(since)}")
    print(f"  {money(r['cost'])} over {human(r['tokens'])} tokens, {r['messages']} turns"
          + (f" ({r['subagent_messages']} from {r['subagents']} subagents)"
             if r["subagent_messages"] else ""))
    for name, v in r["models"].items():
        print(f"    {name:<12} {money(v['cost']):>8}  {human(v['tokens']):>7} tok  "
              f"{v['messages']:>3} turns")
    b = r["breakdown"]
    print(f"    in {human(b['in'])} · out {human(b['out'])} · "
          f"cache write {human(b['cache_write'])} · cache read {human(b['cache_read'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
