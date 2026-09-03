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
* **Cache reads are a tenth of input, cache writes are 1.25x (5m) or 2x (1h).** On a long
  run the cache-read column dwarfs everything else in *tokens* while contributing almost
  nothing to *cost*, so a chip that showed only a token total would be actively misleading.

The dollar figure is **list-price equivalent**: what these tokens would cost on the API.
Nobody running this under a subscription is billed it, and the chip says so on hover.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))

# $ per 1M tokens: (input, output). Cache write is 1.25x input (5m TTL) / 2x (1h),
# cache read is 0.1x input. Kept in step with victor-skills-private/claude-usage.
PRICES = {
    "opus": (5.0, 25.0),
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
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
        + u.get("cache_read_input_tokens", 0) * inp * 0.10
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


def subagent_transcripts(session_file: Path) -> list[Path]:
    """The task files of every agent this session spawned.

    Nothing in the environment points at the tasks directory, and its parent is keyed by a
    workspace uuid that is not the session id — so going from the session to its agents by
    walking the filesystem is guesswork that would sweep up a concurrent session's agents
    in the same folder. Going the other way is exact: the parent transcript names every
    agentId it launched, and each of those is a filename.
    """
    ids = set(AGENT_ID_RE.findall(session_file.read_text(encoding="utf-8", errors="replace")))
    found = []
    for agent_id in sorted(ids):
        for base in Path("/private/tmp").glob(f"claude-*/*/*/tasks/{agent_id}.output"):
            found.append(base)
            break
        else:
            for base in Path("/tmp").glob(f"claude-*/*/*/tasks/{agent_id}.output"):
                found.append(base)
                break
    return found


def _scan(path: Path, since: dt.datetime | None, force_side: bool, best: dict) -> None:
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


def gather_turns(path: Path, since: dt.datetime | None, include_subagents: bool = True):
    """The deduped, priced turns `collect()` and `tab_costs()` both work from.

    One scan, shared, so the two never drift on what counts as a turn — the same
    dedupe-by-`message.id` and subagent-transcript discovery either would reimplement
    otherwise."""
    best: dict[str, tuple] = {}
    _scan(path, since, False, best)
    agents = subagent_transcripts(path) if include_subagents else []
    for extra in agents:
        _scan(extra, since, True, best)
    return list(best.values()), len(agents)


def collect(path: Path, since: dt.datetime | None, include_subagents: bool = True,
           turns=None, n_agents: int | None = None) -> dict:
    """`turns`/`n_agents` let a caller that already ran `gather_turns()` (the `--chip`
    path, when it also needs the residual) reuse that scan instead of reading the whole
    transcript — subagents included — a second time."""
    if turns is None:
        turns, n_agents = gather_turns(path, since, include_subagents)

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
    """
    per_tab = {t: {"cost": 0.0, "tokens": 0.0, "messages": 0,
                    "has_closed": False, "has_unclosed": False} for t in wanted}
    closed = []
    for s in steps:
        is_closed = s["end"] is not None and s["end"] >= s["start"]
        if is_closed:
            closed.append(s)
        for t in s["tabs"]:
            if t in per_tab:
                per_tab[t]["has_closed" if is_closed else "has_unclosed"] = True

    residual = {"cost": 0.0, "tokens": 0.0, "messages": 0}
    for _, model, u, _side, when in turns:
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
            residual["cost"] += c
            residual["tokens"] += tok
            residual["messages"] += 1
            continue
        share = 1.0 / len(hit)
        for t in hit:
            per_tab[t]["cost"] += c * share
            per_tab[t]["tokens"] += tok * share
            per_tab[t]["messages"] += 1
    return {"tabs": per_tab, "residual": residual}


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
                    tabs: list[str], include_subagents: bool = True) -> dict:
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

    turns, _ = gather_turns(path, since, include_subagents)
    result = tab_costs(turns, steps, tabs)
    tabs_out = {
        t: {"measured": row["has_closed"], "cost": row["cost"],
            "tokens": round(row["tokens"]), "messages": row["messages"],
            "tip": tab_cost_tip(row)}
        for t, row in result["tabs"].items()
    }
    r = result["residual"]
    residual_tip = (
        f"{money(r['cost'])} · {human(round(r['tokens']))} tok of this run's cost is "
        "not attributed to any single tab — assembling the guide itself, plus any step "
        "whose window did not cover it."
    )
    return {
        "available": True, "ledger": True, "reason": None,
        "tabs": tabs_out,
        "residual": {"measured": True, "cost": r["cost"], "tokens": round(r["tokens"]),
                    "messages": r["messages"], "tip": residual_tip},
    }


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
    args = ap.parse_args(argv)

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
