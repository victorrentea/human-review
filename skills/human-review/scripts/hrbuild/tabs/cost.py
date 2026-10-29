"""The cost tab: what the run spent, per pass, per phase and per tab."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from ..shared import layout
from ..shared.util import HERE

def cost_session(out_dir: Path | None) -> str:
    """Whose transcripts the bill is read from: the pinned `.session` first, then the env.

    The same order `refresh-report.py` uses, and for the same reason: `.session` is the
    conversation that did the work, and `$CLAUDE_CODE_SESSION_ID` is merely whoever
    happens to be running this build. They differ whenever the build is started by
    anything but a refresh — above all by a button on the served page, whose command line
    calls this build directly and inherits the environment of the conversation that
    started `serve-review.py`. That conversation is usually still talking, so its
    transcript grows between two presses, the ledger's fingerprint never matched the one
    on disk, and every *Update the report* under a diagram re-read two days of transcripts
    — thirty seconds of a thirty-six-second press — to print a bill for the wrong
    conversation.
    """
    if out_dir is not None:
        try:
            # Blank is pinned too: the run had no Claude session (a Copilot or Codex
            # harness), and the env id is then a stranger's — the conversation that
            # pressed refresh — not "the run, recovered".
            return (out_dir / ".session").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def _cost_env(out_dir: Path | None) -> dict:
    """The environment `review-cost.py` is asked in, with `cost_session`'s answer in it."""
    env = dict(os.environ)
    sid = cost_session(out_dir)
    if sid:
        env["CLAUDE_CODE_SESSION_ID"] = sid
    return env


def cost_chip(root: Path, out_dir: Path | None = None) -> dict | None:
    """What this review run consumed, asked of the run itself.

    Returns None — dropping the chip rather than showing a wrong one — whenever the answer
    cannot be trusted: no session id pinned or in the environment (the page was built
    outside a Claude Code session), or no transcript for it.
    """
    script = HERE / "review-cost.py"
    if not script.is_file():
        return None
    proc = subprocess.run([sys.executable, str(script), "--chip"],
                          cwd=root, capture_output=True, text=True,
                          env=_cost_env(out_dir or root / ".human-review"))
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost chip: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def tab_cost_report(root: Path, tab_ids: list[str]) -> dict | None:
    """What each tab cost, asked of the run itself — same discipline as `cost_chip`.

    Unlike `cost_chip`, this does not go quiet on a bad day: no session, no transcript, no
    step ledger, a step that never stamped — every one of those comes back as *data*
    (`report["tabs"][id]["tip"]` says so in words), because a tab whose cost silently has
    no tooltip reads exactly like a tab that measured zero. Only returns None when
    `review-cost.py` itself could not be asked at all.
    """
    script = HERE / "review-cost.py"
    if not script.is_file() or not tab_ids:
        return None
    proc = subprocess.run(
        [sys.executable, str(script), "--tab-costs", "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no per-tab cost report: {line}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


#: Where the ledger is kept between builds. Dot-prefixed like every other private file
#: beside the page: `publish-demo.sh` publishes what does not begin with a dot, and a
#: measurement of one machine's transcripts is not something to ship in a demo zip.
COST_CACHE = ".cost-ledger.json"


def _cost_inputs(root: Path, out_dir: Path, tab_ids: list[str], base: str) -> str:
    """A fingerprint of everything the ledger is computed *from*.

    Not of the answer — of the inputs, so a hit means "nothing this number depends on has
    moved" rather than "somebody said it was fine". The pieces:

      * the session id, which is whose transcripts are read;
      * that session's `.jsonl` and every `agent-*.jsonl` beside it, by size and mtime —
        a conversation that ran another turn is a different bill;
      * `.steps.json`, which is how the ledger splits the run across tabs;
      * `phases.json`, which the ledger *embeds* — rerunning `session-cost.py` and seeing
        the page still print last night's `page build` is the exact failure this list
        exists to prevent, and it happened: the phases were the one input not in it;
      * the base ref and the tab list, which are what was asked;
      * `review-cost.py` itself, so a change to the pricing invalidates every cache on
        this machine rather than being invisible until somebody deletes a file.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{base}\0{','.join(tab_ids)}\0".encode())
    script = HERE / "review-cost.py"
    # The two cost records the runs wrote of themselves (`harness_cost.py`): the committed
    # `review-cost.json` (rows 1–3) and `report-cost.json` (row 4, plus every refresh).
    for f in (script, out_dir / ".steps.json", out_dir / ".session",
              out_dir / "phases.json", root / "review-cost.json",
              out_dir / "report-cost.json", HERE / "harness_cost.py"):
        try:
            st = f.stat()
            h.update(f"{f.name}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
        except OSError:
            h.update(b"\0gone\0")
    sid = cost_session(out_dir)
    h.update(f"{sid}\0".encode())
    if sid:
        projects = Path(os.path.expanduser("~/.claude/projects"))
        # The session's own transcript and its subagents'. Sorted, because a set of paths
        # in filesystem order is a fingerprint that changes for no reason.
        for f in sorted(list(projects.glob(f"*/{sid}.jsonl"))
                        + list(projects.glob(f"*/{sid}/subagents/agent-*.jsonl"))):
            try:
                st = f.stat()
                h.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
            except OSError:
                h.update(b"\0gone\0")
    return h.hexdigest()


def cost_ledger_report(root: Path, tab_ids: list[str], base: str,
                       out_dir: Path | None = None) -> dict | None:
    """The whole bill — writing the code, the passes, every tab, the residual.

    Same discipline as `tab_cost_report`, which it supersedes: every failure comes back as
    data with a sentence explaining it, never as a silently missing number. It returns None
    only when `review-cost.py` could not be asked at all.

    **Cached, on the inputs.** This one call was 40 seconds of a 47-second build — it reads
    every turn of a conversation that wrote a feature over two days, and it does it again on
    every rebuild of a page whose bill has not moved since. That is most of what a reader
    waits through after pressing a button on the page: re-rendering a diagram takes three
    seconds and then they sit for forty, watching nothing, in front of a control they
    pressed. Re-deriving a number from transcripts nobody has appended to is not a
    measurement, it is the same measurement, and `_cost_inputs` is what says so.

    A cache that could be *wrong* would be much worse than a slow build — the cost tab is
    the one part of this page nothing else corroborates — so the key is the inputs and never
    a timestamp: the session id, the byte length and mtime of its transcript and every
    subagent's, `.steps.json`, the base, the tab list, and `review-cost.py` itself. Anything
    moves and the answer is recomputed. Nothing moves and the answer cannot have.
    """
    script = HERE / "review-cost.py"
    if not script.is_file():
        return None
    cache = (out_dir / COST_CACHE) if out_dir else None
    key = _cost_inputs(root, out_dir, tab_ids, base) if out_dir else ""
    if cache:
        try:
            held = json.loads(cache.read_text(encoding="utf-8"))
            if held.get("key") == key and "ledger" in held:
                return held["ledger"]
        except (OSError, ValueError):
            pass
    proc = subprocess.run(
        [sys.executable, str(script), "--ledger", "--base", base,
         "--tabs", ",".join(tab_ids)],
        cwd=root, capture_output=True, text=True, env=_cost_env(out_dir),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        for line in proc.stderr.strip().splitlines()[-1:]:
            print(f"[review] no cost ledger: {line}", file=sys.stderr)
        return None
    try:
        ledger = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if cache:
        # Best effort: a read-only directory is a slow build, not a failed one.
        try:
            cache.write_text(json.dumps({"key": key, "ledger": ledger}), encoding="utf-8")
        except OSError:
            pass
    return ledger


# The unattributed cost, in the order a reader wants it: the one part that has a real name
# first, then the two that are honestly leftovers. Keys come from `review-cost.py`'s
# `tab_costs`; a part with no turns in it is not rendered at all.
# Plain words: eval run 6's reader met "Step 9 writes every tab's prose" and "outside every
# step's window" — the skill's own vocabulary, which nobody reading the page has learned.
# A short name like the tab rows above them, and what it covers under it (Oct 2026: the
# bare sentence sat in a column of tab names).
RESIDUAL_ROWS = [
    ("guide", "Guide text", "every tab&rsquo;s text, written in one pass at the end"),
    ("subagent", "Subagents", "while no single tab was being produced"),
    ("conversation", "Orchestration", "reading results, deciding, recovering from failures"),
]


def _cost_money(c: float) -> str:
    """`review-cost.py`'s own `money()`, with one difference that matters in a table: a
    measured zero prints as `$0.00`, not as `<$0.01`. In a tooltip the two read the same;
    in a column of numbers, "less than a cent" claims a script-generated tab spent
    something, which is the one thing the zero rows are there to deny."""
    if c <= 0:
        return "$0.00"
    return f"${c:,.2f}" if c >= 0.01 else "<$0.01"


def _cost_tokens(n: float, models=None) -> str:
    """A row's token count, and under it the models that spent them.

    `36.9M` says how much was read and written; it does not say by what, and the same
    36.9M is $180 on Opus and $37 on Sonnet. Both numbers are already in the row, so
    without the model line the reader is left inferring it from the ratio between them —
    which is precisely the arithmetic this column exists to save them.

    One model prints as a name, several as shares: a phase is rarely a clean split (the
    conversation that built this page ran Opus with a Haiku scout beside it), and
    "Opus 5 / Haiku 4.5" with no weights would suggest something near half. `models` is
    `{printed name: tokens}`, straight from `phases.json` — the name table lives in
    `review-cost.py`, which is also where the turns were read.
    """
    n = int(round(n))
    # A conversation that wrote a feature over two days runs to ten figures, and `1044.6M`
    # is four digits the reader has to convert before the column means anything.
    if n >= 1_000_000_000:
        out = f"{n / 1_000_000_000:.1f}B"
    elif n >= 1_000_000:
        out = f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        out = f"{n / 1_000:.0f}k"
    else:
        out = str(n)
    rows = [(str(k), float(v)) for k, v in (models or {}).items()
            if isinstance(v, (int, float)) and v > 0] if isinstance(models, dict) else []
    total = sum(v for _k, v in rows)
    if not rows or total <= 0:
        return out
    rows.sort(key=lambda kv: -kv[1])
    # Under half a percent a model is a rounding error with a name, and printing
    # "Sonnet 5 0%" makes the reader parse a share too small to explain anything.
    big = [(k, v) for k, v in rows if v / total >= 0.005] or rows[:1]
    # The shares are of TOKENS, and say so. Eval run 10's guide row read `Opus 5.5 96% /
    # Sonnet 5.5 4%` beside a cost in which the Sonnet step was $0.43 of $1.58 — 27% —
    # and a bare percentage in a table of dollars reads as a share of the dollars.
    if len(big) == 1:
        return f'{out}<span class="costsub">{html.escape(big[0][0])}</span>'
    # Eval run 11: 'Opus 5.5 64% / Sonnet 5.5 36% of tokens' widened the column and wrapped
    # the table. The face names the model that spent most; the split is on hover.
    split = " / ".join(f"{k} {v / total * 100:.0f}%" for k, v in big) + " of tokens"
    return (f'{out}<span class="costsub" data-tip="{html.escape(split, quote=True)}">'
            f'{html.escape(big[0][0])}</span>')


COST_TAB_ID = "cost"

# The groups the ledger reports, in the order the money was spent: somebody wrote it,
# somebody reviewed it, and then this page was assembled. Each is a caption row, not a
# separate table — the reader is comparing magnitudes across all three, and three tables
# means three column widths and no comparison.
PASS_ROWS = [
    ("finding", "the passes that read the diff"),
    ("fixing", "the passes that applied what they found"),
]

#: The phases `session-cost.py` dates, in the order the money was spent. They are a
#: *replacement* for the two groups above, not an addition: the same dollars, cut by what
#: the work was rather than by which program billed it. The reader's question is where the
#: money went in the work — was the review the expensive part, or was acting on it — and
#: `writing the code` + `reviewing it` cannot answer it, because taking the review's advice
#: falls in neither.
#:
#: The keys are `session-cost.py`'s, and the labels are too: a second set of names here
#: would let the table and the terminal disagree about what a row is. Order is fixed here
#: rather than trusted to the file, so a phase nobody could date still holds its place in
#: the sequence instead of vanishing from the middle of it.
#:
#: `page_build` is **the last full regeneration of this report** — the one run of
#: `refresh-report.py --steps all|static` (or of `run-steps.py` and `build-review-html.py`
#: together) that produced the copy on screen. Not every rebuild the session did: the
#: conversation that writes a page rebuilds it dozens of times to check its work, and
#: counting them all made the row 118.8M tokens on PR #49 for a page whose own build is a
#: few dollars. A reader asking what this page cost means the copy in front of them.
#:
#: `not_this_report` is last and is not part of the sum. The session that builds a page is
#: rarely doing only that — on the run this was written for it spent the same evening
#: writing the skill that builds the page, mending the branch under review and answering
#: unrelated questions — and those earlier rebuilds land here too, itemised in the tooltip
#: so the reader can see what the row is made of. It is printed because hiding a measured
#: number teaches the reader the evening was cheaper than it was, and it is excluded
#: because a page may not bill for work it had no part in.
PHASE_ROWS = ["implementation", "code_review", "post_review_fixes", "review_points",
              "video", "images", "page_build", "not_this_report"]

#: What the total adds, spelled out under it. A footer number nobody can derive from the
#: column above it is a number the reader has to take on faith, and this table now has a
#: row on it that is deliberately not in the sum — which is exactly the case where faith
#: runs out. `page build` is named for what it is rather than left to sound like the whole
#: evening, because that is the row whose meaning changed under the reader.
TOTAL_FORMULA = ("implementation + code-review + post-review fixes + review-points + "
                 "model steps + the last full regeneration of this page")


def _when(raw: str | None) -> str:
    """`2026-09-02T15:41:21.4Z` as `2 Sep 15:41`. The date is there because the writing
    happened on a different day from the review and that is half the point of the row;
    the seconds are not, because nothing here is timed to the second.

    Both ends in this machine's zone. The boundaries arrive in whatever zone produced them
    — a transcript stamps UTC, `git %cI` the committer's offset — and printed as given the
    post-review fixes read `18:34 → 21:39`, three hours of fixing that were five minutes."""
    if not raw:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is not None:
        t = t.astimezone()
    return f"{t.day} {t.strftime('%b')} {t:%H:%M}"


def _span(a: str | None, b: str | None) -> str:
    """`3 Oct 23:19 → 23:36`: the second end drops the day when it is the same day
    (Victor, 4 Oct 2026)."""
    x, y = _when(a), _when(b)
    if not x:
        return ""
    if y and y.rsplit(" ", 1)[0] == x.rsplit(" ", 1)[0]:
        y = y.rsplit(" ", 1)[1]
    return f"{x} &rarr; {y}" if y else x


def _cost_cell(money: str, tokens: float, models=None, model_usd=None,
               efforts=None) -> str:
    """The one number column: the price, its tokens on the hover, the model under it.

    Tokens were a column of their own beside the price; Victor (4 Oct 2026) wanted one
    column, the tokens on the price's hover and the model name under the price. The hover
    is a list, one model per line with its tokens and its price (Victor, 14 Oct 2026: a
    percentage of tokens said nothing about where the money went); a model whose price
    could not be split off the row's (`harness_cost.model_costs`) shows its tokens alone.

    `efforts` (`harness_cost.row_efforts`) puts the effort each model ran at beside its
    name: `Opus 5.5 (high)` under the price, `Opus 5.5 (effort: high, context: 1M)` on the
    hover — Victor (30 Oct 2026) asked which model and effort wrote PR #49, and nothing on
    the page said."""
    import harness_cost
    names = [(str(k), float(v)) for k, v in (models or {}).items()
             if isinstance(v, (int, float)) and v > 0] if isinstance(models, dict) else []
    total = sum(v for _k, v in names)
    names.sort(key=lambda kv: -kv[1])
    big = [(k, v) for k, v in names if total and v / total >= 0.005] or names[:1]
    usd = model_usd if isinstance(model_usd, dict) else {}
    effs = efforts if isinstance(efforts, dict) else {}
    head = f"{_cost_tokens(tokens)} tokens"
    if names:
        items = "".join(
            f"<li>{html.escape(harness_cost.model_signature(k, effs.get(k)))}: "
            f"{_cost_tokens(v)} tokens"
            + (f" &middot; {_cost_money(usd[k])}" if isinstance(usd.get(k), (int, float)) else "")
            + "</li>" for k, v in names)
        tip = (f'<p class="tipfoot">{head}</p><ul class="tiplist prose">{items}</ul>')
        attr = f'data-tip-html="{html.escape(tip, quote=True)}"'
    else:
        attr = f'data-tip="{html.escape(head, quote=True)}"'

    def face(k: str) -> str:
        # Only the effort on the face: `(effort: …, context: …)` is the hover's, and the
        # column is the narrowest on the tab.
        got = [x for x, n in sorted(((effs.get(k) or {}).get("efforts") or {}).items(),
                                    key=lambda kv: -kv[1]) if x and n]
        return html.escape(k + (f" ({'/'.join(got)})" if got else ""))
    model = (f'<span class="costsub">{" · ".join(face(k) for k, _ in big)}</span>'
             if big else "")
    return f'<span class="costmoney" {attr}>{money}</span>' + model


def _instants(win) -> list:
    """A window's two ends as instants, so two spellings of one moment compare equal."""
    out = []
    for raw in (win or [])[:2]:
        try:
            out.append(dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            out.append(raw)
    return out


def _stamp_s(raw: str | None) -> str:
    """The seconds of a stamp, two digits — for when two ends differ by less than a minute."""
    try:
        return f"{dt.datetime.fromisoformat(str(raw).replace('Z', '+00:00')).second:02d}"
    except ValueError:
        return "00"


def phase_rows_html(phases: dict | None) -> str:
    """What each phase of the work cost, or why it could not be dated.

    This is the cut a reader actually arrives with, and nothing was in a position to make
    it until the commits started carrying trailers saying which commit was which. With
    `Implements:` and `Review-Points:` on the branch, `session-cost.py` can date the first
    edit, the implementation commit, the review's first and last turn and the review
    commit — and the four phases between them are the answer to "was the review the
    expensive part, or was acting on it".

    **A phase that cannot be dated prints its reason, never `$0.00`.** The two render
    identically to a reader and mean opposite things: one is a phase that cost nothing, the
    other is a phase whose cost is sitting in some other row of the same table.

    **Every window is printed**, for the reason the authoring row already prints its own: a
    window is a bound, not a fence. Work inside it that belonged to something else is
    counted, and a table that hid the width of the bound would imply a precision the
    transcript cannot support.
    """
    rows_by_key = {r.get("key"): r for r in (phases or {}).get("rows") or []
                   if isinstance(r, dict)}
    if not any(r.get("measured") for r in rows_by_key.values()):
        return ""
    out = []
    for key in PHASE_ROWS:
        r = rows_by_key.get(key)
        if not r:
            continue
        if key == "not_this_report":
            # Measured and kept in the ledger JSON (the total above already leaves it
            # out — see `EXCLUDED_PHASES` in `review-cost.py`), but not printed: it is
            # other work in the same pinned session, not this report, and a reader of
            # this table has no use for a row about work this page had no part in.
            continue
        label = html.escape(str(r.get("label") or key))
        if not r.get("measured"):
            why = html.escape(str(r.get("reason") or "not measured"))
            out.append('<tr class="costquiet"><td><span class="costnote">'
                       f'{label} — {why}</span></td><td>—</td><td>—</td></tr>')
            continue
        window = r.get("window") or []
        # A row that names a command names a *shortened* one — the plumbing around it is
        # the same on every run and spends the cell's width saying so — and the line
        # exactly as it ran goes on the hover of the sentence that carries it. Without
        # that hover the face was a claim the reader had to take on trust, and before the
        # shortening it was worse: an absolute path chopped mid-word at `…/refresh-rep`,
        # in prose, with nothing saying it had been cut.
        detail = html.escape(str(r.get("detail") or ""))
        if r.get("command") and detail:
            detail = (f'<span data-tip="{html.escape(str(r["command"]), quote=True)}">'
                      f'{detail}</span>')
        sub = " &middot; ".join(x for x in (
            detail,
            (f'{_when(window[0])} &rarr; {_when(window[1])}'
             if len(window) == 2 and _when(window[0]) else ""),
        ) if x)
        if r.get("excluded"):
            # Measured, printed, and grey, because it is none of this page's business. The
            # tooltip is the row's whole point: "$347 of something else" is a number the
            # reader cannot act on, and the tool calls of those turns are what turn it into
            # a sentence they can.
            tip = html.escape(
                "Not added to the total — this is the rest of the session that built the "
                "page: everything it did besides the regeneration above, the earlier "
                "rebuilds of this very page included. It was: "
                + str(r.get("detail") or "other work") + ".", quote=True)
            out.append(f'<tr class="costquiet"><td><span data-tip="{tip}">{label}</span>'
                       f'<span class="costsub">{sub} &middot; not in the total</span></td>'
                       f'<td>{_cost_tokens(r.get("tokens") or 0, r.get("models"))}</td>'
                       f'<td>{_cost_money(r.get("cost") or 0.0)}</td></tr>')
            continue
        out.append(f'<tr><td>{label}<span class="costsub">{sub}</span></td>'
                   f'<td>{_cost_tokens(r.get("tokens") or 0, r.get("models"))}</td>'
                   f'<td>{_cost_money(r.get("cost") or 0.0)}</td></tr>')
    # Anything the file dates that this table does not know the name of. Dropping it would
    # make the rows stop summing to the total, silently, the first time a phase is added.
    for key, r in rows_by_key.items():
        if key in PHASE_ROWS or not r.get("measured"):
            continue
        out.append(f'<tr><td>{html.escape(str(r.get("label") or key))}</td>'
                   f'<td>{_cost_tokens(r.get("tokens") or 0, r.get("models"))}</td>'
                   f'<td>{_cost_money(r.get("cost") or 0.0)}</td></tr>')
    return "".join(out)


def cost_ledger_html(led: dict | None, tabs: list[dict], voices: dict | None = None) -> str:
    """What this change set cost, from the first line written to this page being built.

    This was a chip in the scope bar with a breakdown hanging off it, and the chip
    answered the wrong question: *what did this page cost to make*. The question a reader
    arrives with is what the **change** cost, and producing the code is the larger half of
    it — on the branch this was built for, the conversation that wrote the feature cost
    nearly seven times the review that read it. A number that big is not a footnote on a
    bar of chips; it is its own tab, and it is the honest answer to "is this way of working
    worth it", which is the only reason anybody totals up an agent's bill at all.

    Every row is measured or says it is not. Three kinds of honesty the table has to keep:

      * **A window is not a fence.** The authoring row is costed between that conversation's
        first and last edit to these files. Work inside that window which belonged to
        something else is counted, and the row prints the window so the reader can see how
        wide it is rather than trusting a number that cannot be tightened.
      * **The passes are added once.** A review pass usually runs before the guide does, so
        its cost is outside the run's own total and is added; one fired mid-run is already
        inside it and is not. The ledger tells the two apart rather than assuming.
      * **A zero is not an absence.** A tab a script produced costs nothing to produce and
        says so in its own row; a tab nothing could measure says *that*, in words.
    """
    if not led:
        return ""
    # Every tab opens on its title (Victor, 7 Oct 2026: this one had none), and the
    # "Prompt to get this page" pill goes at its right end, as on the other tabs
    # (`adopt.PLACES`), out of the table's header where it crowded the time column.
    body = _cost_ledger_body(led, tabs, voices)
    # The title and the table in one box as wide as the table, centred: the title stands
    # over the table's left edge (Victor, 14 Oct 2026), not at the panel's.
    return (f'<div class="costwrap">{COST_TITLE}{body}</div>' + tooling_investment_html()
            if body else "")


#: The tab's title row.
COST_TITLE = '<h2 class="tabtitle">PR Cost</h2>'

#: What building human-review ITSELF has cost — the tool, not the change on this page.
#: A dated baseline, hardcoded on purpose: nothing recomputes it, because a figure that
#: crept up every build would read as this PR's bill. Measured on 20 Oct 2026 by
#: `scripts/tools/tooling-cost.py` (re-run by hand; no build step calls it): every Claude
#: Code transcript on Victor's Mac, deduped by `message.id`, priced per model at the API
#: list price (platform.claude.com/docs/en/about-claude/pricing, read 20 Oct 2026). A session
#: counts when it ran in this repo, was a `claude -p` eval run, or when at least half its
#: tool calls (subagents included) named a human-review path. The buckets are main threads;
#: every bucket's subagents are one line of their own. They sum to `usd`. The three times
#: (measured 20 Oct 2026, 11:28) are `harness_cost.claude_human_time` and
#: `claude_busy_spans` over the same sessions: the human's, the agents' added up, and the
#: agents' union — the hours in which something was being built.
TOOLING_INVESTMENT = {
    "usd": 3986, "sessions": 227, "first": "27 Aug 2026", "asof": "20 Oct 2026",
    "buckets": [("human-review repo", 1120), ("workspace sessions", 795),
                ("subagents", 1809), ("eval runs (claude -p)", 226),
                ("petclinic checkouts, after the move", 33), ("other projects", 3)],
    "models": [("Opus 5.5", 1883), ("Opus 5", 1782), ("Fable 5.1", 170),
               ("Sonnet 5", 101), ("Sonnet 5.5", 49)],
    "borderline": {"sessions": 29, "usd": 1360, "prorated": 583},
    "humanSeconds": 122285, "agentSeconds": 1587010, "wallSeconds": 795131,
}


def tooling_investment_html(t: dict = TOOLING_INVESTMENT) -> str:
    """The tool's own bill, under the table and apart from it: a muted footer box.

    Kept out of the table and its total on purpose — the table is what THIS change cost;
    this is what building the reviewer cost, and the two must never be added up. Three
    figures, each its own tile (Victor, 8 Oct 2026: the token cost, the time he put in, and
    how long it took to build, highlighted — not a sentence to read them out of)."""
    if not t:
        return ""
    money = lambda n: f"${n:,.0f}"
    hours = lambda s: f"{s / 3600:,.0f} h"
    tip = ("By bucket: " + " · ".join(f"{k} {money(v)}" for k, v in t["buckets"])
           + ". By model: " + " · ".join(f"{k} {money(v)}" for k, v in t["models"])
           + f". Not counted: {t['borderline']['sessions']} mixed sessions where human-review"
           f" was under half the tool calls ({money(t['borderline']['usd'])} whole, about"
           f" {money(t['borderline']['prorated'])} pro rata), and the petclinic era before"
           " 22 Aug 2026, whose transcripts are mostly gone. Measured by"
           " scripts/tools/tooling-cost.py.")
    # "(subsidized)": the figure is API list price, and the tool was built on a Claude
    # subscription that bills a flat fee far below it (Victor, 8 Oct 2026).
    tiles = [(money(t["usd"]), 'token cost (<a href="https://claude.com/pricing" '
              'target="_blank" rel="noopener">subsidized</a>)',
              "Claude API list price, every session counted, subagents included — what the "
              "tokens would cost on an API key; a Claude subscription bills a flat monthly "
              "fee instead. " + tip)]
    if t.get("humanSeconds"):
        tiles.append((hours(t["humanSeconds"]), "Victor&rsquo;s time, estimated",
                      "Speaking each prompt (Wispr Flow's own duration where it logged the "
                      "dictation), typing the rest, reading the agent's replies. A floor: "
                      "reviewing, testing by hand and thinking leave no trace."))
    if t.get("wallSeconds"):
        tiles.append((hours(t["wallSeconds"]), "agent time",
                      "Hours in which at least one agent was working on it — each prompt to "
                      "its turn's last record, overlapping sessions counted once. Added up "
                      f"session by session it is {hours(t.get('agentSeconds') or 0)}."))
    cells = "".join(f'<span class="costtile" tabindex="0" data-tip="{html.escape(tip_, quote=True)}">'
                    f'<b>{html.escape(v)}</b><span>{k}</span></span>'
                    for v, k, tip_ in tiles)
    return (f'<div class="costtooling"><p class="costtooling-head">Building human-review '
            f'itself <span>— the tool, not this change</span></p>'
            f'<div class="costtiles">{cells}</div>'
            f'<p class="costtooling-foot">{t["sessions"]} sessions, {t["first"]} &rarr; '
            f'{t["asof"]}</p></div>')


def _cost_ledger_body(led: dict, tabs: list[dict], voices: dict | None = None) -> str:
    four = components_html(led.get("components"), voices=voices)
    if four:
        # The four components lead, and the fold under them breaks down ONE of their rows
        # — "this guide" — tab by tab, adding up to it. It used to be the whole
        # Claude-transcript ledger again (the writing conversation, the passes, the tabs):
        # the same dollars cut a second way, over different windows, with a second total.
        # Eval run 6 printed $32.20 / 49.1M above it and $31.39 / 47.9M inside it, and
        # its "conversation" row matched none of the three rows it was standing in for.
        # The first three components need no fold: each already names its session and
        # window on its own row.
        # Opened from the "This guide" row's own name (Victor, 4 Oct 2026), not from a
        # fold under the table.
        rest = guide_breakdown_html(led, tabs)
        return components_html(led.get("components"), fold=rest, voices=voices) if rest else four
    return _legacy_ledger_html(led, tabs)


def guide_breakdown_html(led: dict, tabs: list[dict]) -> str:
    """The "this guide" component, split across the page's tabs — summing to that row.

    The rows are the run's own conversation, turn by turn, placed in the tab whose step was
    running (`review-cost.py`'s `tab_costs`), plus each `claude -p` step the run shelled out
    to, charged to the tab it wrote (`bill_model_runs`: the requirements↔tests mapping on
    Tests, the film script on Demo). The footer says it equals the row above — or, when the
    two were measured over different stretches of the conversation, by how much and why,
    so the fold never carries an unexplained second total."""
    report = led.get("tabs") or {}
    comp = led.get("components") or {}
    guide = next((r for r in comp.get("rows") or []
                  if isinstance(r, dict) and r.get("key") == "guide"), None)
    if not ((led.get("run") or {}).get("measured") or report.get("modelRuns")):
        return ""
    body = _cost_tab_rows(report, tabs, one_col=True)
    rows = report.get("tabs") or {}
    total = sum((r.get("cost") or 0.0) for r in rows.values() if r.get("measured"))
    tokens = sum((r.get("tokens") or 0) for r in rows.values() if r.get("measured"))
    resid = report.get("residual") or {}
    if resid.get("measured"):
        total += resid.get("cost") or 0.0
        tokens += resid.get("tokens") or 0
    sub = ""
    if total <= 0 and not tokens:
        # Nothing of the row was found turn by turn — the row was read off another
        # conversation (the reference page's guide is the phase cut's page build, in a
        # session the tab split never reads). Twelve zeros over "$0.39 less here" is not a
        # breakdown of anything, so there is no fold.
        return ""
    if guide and guide.get("measured") and guide.get("usd") is not None and not guide.get("aic"):
        g = guide.get("usd") or 0.0
        if abs(total - g) < 0.005:
            sub = "the same as the &ldquo;this guide&rdquo; row above"
            # Printed as that row prints it: $3.3150 is `$3.31` there and `$3.32` here
            # when summed in another order, and "the same as" over two different numbers
            # is the contradiction this footer exists to remove.
            total, tokens = g, guide.get("tokens") or tokens
        else:
            # Said with the two windows as measured, never as a description of them. Eval
            # run 10's sentence claimed these rows read the WIDER window ("from its start to
            # this build") over a total $0.48 SMALLER: they had started at `.started`,
            # 31 s after the prompt the row above starts on. `review-cost.py` now reads the
            # row's own window, so this branch is the residue of a real disagreement, and
            # the reader gets the two spans to compare instead of a story about them.
            def span(win, secs: bool = False) -> str:
                win = win or []
                if not (len(win) == 2 and _when(win[0]) and _when(win[1])):
                    return ""
                at = [_when(w) + (f":{_stamp_s(w)}" if secs else "") for w in win]
                return f"{at[0]} &rarr; {at[1]}"
            mine = next((e.get("window") for e in guide.get("entries") or []
                         if not str(e.get("session") or "").startswith("claude -p")),
                        None) or guide.get("window")
            ours_w = report.get("window")
            differ = _instants(mine) != _instants(ours_w)
            theirs, ours = span(mine), span(ours_w)
            if differ and theirs == ours:
                # Apart by seconds: at minute precision the two spans would print equal
                # and the sentence would say "the same stretch" over two different ones.
                theirs, ours = span(mine, True), span(ours_w, True)
            if theirs and ours and differ:
                why = (f"it reads the run&rsquo;s conversation over {theirs}, these rows "
                       f"over {ours}")
            elif theirs and ours:
                why = (f"over the same stretch ({ours}), priced turn by turn here and as one "
                       "window there")
            else:
                why = "the two were measured over stretches this build cannot name"
            sub = (f"the &ldquo;this guide&rdquo; row above says {_cost_money(g)}: {why}; "
                   f"{_cost_money(abs(total - g))} {'more' if total > g else 'less'} here")
    foot = (f'<tr class="costtotal"><td>Total'
            + (f'<span class="costsub">{sub}</span>' if sub else "")
            + f'</td><td>{_cost_cell(_cost_money(total), tokens)}</td></tr>')
    return ('<table class="costtab costledger costbytab">'
            '<thead><tr><th scope="col">tab</th><th scope="col">cost</th></tr></thead>'
            f'<tbody>{body}</tbody><tfoot>{foot}</tfoot></table>')


def _legacy_ledger_html(led: dict, tabs: list[dict]) -> str:
    """The ledger as it was before the four components: Claude transcripts only."""
    # Nothing measured anywhere — no transcript for the run, and no conversation on disk
    # that wrote the code — is an absence, and the page carries no tab for it. A pill
    # reading `$0` is a claim that this change was free, which is the one thing the
    # absence does not mean. A run that measured EITHER half still gets the tab, with the
    # other half saying in words why it is missing.
    if not ((led.get("writing") or {}).get("measured")
            or (led.get("run") or {}).get("measured")):
        return ""
    rows = []
    # The phase cut, when the branch's trailers made it datable. It *replaces* the two
    # groups below rather than joining them: the same money, cut by what the work was, and
    # printing both cuts of one total in one table is how a reader ends up adding a number
    # to itself. The per-tab group stays either way — it answers a different question
    # (which part of this page cost what), and it is the only one of the three that is
    # about the page rather than about the change.
    phases = phase_rows_html(led.get("phases"))

    def row(label: str, tokens, cost, cls: str = "") -> None:
        tok = _cost_tokens(tokens) if tokens is not None else "—"
        money = _cost_money(cost) if cost is not None else "—"
        rows.append(f'<tr{f' class="{cls}"' if cls else ""}><td>{label}</td>'
                    f'<td>{tok}</td><td>{money}</td></tr>')

    def group(title: str) -> None:
        rows.append(f'<tr class="costgroup"><td colspan="3">{title}</td></tr>')

    # --- writing it ---------------------------------------------------------
    writing = led.get("writing") or {}
    if phases:
        group("phase by phase, from the first edit to this build")
        rows.append(phases)
    elif writing.get("measured"):
        group("writing the code")
        for sess in writing.get("sessions") or []:
            where = " &middot; ".join(x for x in (
                f'{sess["edits"]} edits across {sess["files"]} files' if sess.get("edits")
                else f'{sess["bash"]} shell writes across {sess["files"]} files',
                f'{_when(sess.get("first"))} &rarr; {_when(sess.get("last"))}',
                # Escaped, and `<synthetic>` dropped: it is what `review-cost.py` calls a
                # turn with no model on it, it costs nothing, and unescaped it was a tag
                # the browser swallowed along with the comma in front of it.
                ", ".join(html.escape(m) for m in (sess.get("models") or [])
                          if m and m != "<synthetic>"),
            ) if x)
            name = "this conversation" if sess.get("current") else \
                f'conversation <code>{html.escape(sess["session"][:8])}</code>'
            rows.append(
                f'<tr><td>{name}<span class="costsub">{where}</span></td>'
                f'<td>{_cost_tokens(sess.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(sess.get("cost") or 0.0)}</td></tr>')
        if writing.get("weak"):
            row('<span class="costnote">no conversation used the edit tools on these '
                "files — this is the strongest shell-only match, and may be the wrong "
                "one</span>", None, None, "costquiet")
        if writing.get("otherAgents"):
            names = html.escape(", ".join(writing["otherAgents"]))
            row(f'<span class="costnote">the branch was also written with {names}, whose '
                "usage leaves no transcript here — these rows are only the Claude part of "
                "the bill</span>", None, None, "costquiet")
    else:
        group("writing the code")
        why = writing.get("reason") or "not measured"
        row(f'<span class="costnote">{html.escape(str(why))}</span>', None, None, "costquiet")

    # --- reviewing it -------------------------------------------------------
    # Skipped entirely under the phase cut, and not as a tidy-up: `the passes that read the
    # diff` and `code-review agents` are the SAME dollars counted a second way, and two
    # cuts of one total under one `total` row is how a reader ends up adding a number to
    # itself. The finding/fixing split survives where phases could not be dated.
    passes = {} if phases else (led.get("passes") or {})
    groups = passes.get("groups") or {}
    if groups or passes.get("inline"):
        group("reviewing it")
    for key, title in PASS_ROWS:
        g = groups.get(key)
        if not g:
            continue
        invoked = ", ".join(f"<code>{html.escape(i)}</code>" for i in g.get("invoked") or [])
        inside = (g.get("cost") or 0.0) - (g.get("earlier") or 0.0)
        note = (" &middot; already inside the run below, so not added twice"
                if inside > 0.005 else "")
        rows.append(
            f'<tr><td>{title}<span class="costsub">{invoked}{note}</span></td>'
            f'<td>{_cost_tokens(g.get("tokens") or 0)}</td>'
            f'<td>{_cost_money(g.get("cost") or 0.0)}</td></tr>')
    if passes.get("inline"):
        n = passes["inline"]
        row(f'<span class="costnote">{n} pass{"es" if n != 1 else ""} ran in this '
            "conversation rather than forking, so there is no transcript of their own to "
            "price — their cost is in the rows below</span>", None, None, "costquiet")

    # --- building the guide -------------------------------------------------
    group("building this guide")
    rows.append(_cost_tab_rows(led.get("tabs") or {}, tabs))

    # The total of the rows on screen, which under the phase cut is not the ledger's own.
    # `led["total"]` adds the authoring conversation, the passes and the run — three
    # overlapping measurements of one bill, reconciled by the groups that are no longer
    # being drawn. Printing it under the phases would put a number in the footer that the
    # column above it does not add up to, and the reader has no way to tell which of the
    # two is the answer.
    phase_doc = led.get("phases") or {}
    if phases and phase_doc.get("cost") is not None:
        total, total_tokens = phase_doc.get("cost") or 0.0, phase_doc.get("tokens") or 0
    else:
        total, total_tokens = led.get("total") or 0.0, led.get("total_tokens") or 0
    # Under the phase cut the total is an addition the reader can check, and one row above
    # it is deliberately left out of that addition, so the formula is printed rather than
    # implied. Without the phases there is nothing to spell out — `total` is the ledger's
    # own three-source reconciliation, explained in the caption.
    total_sub = (f'<span class="costsub">{TOTAL_FORMULA}</span>' if phases else "")
    foot = (f'<tr class="costtotal"><td>total{total_sub}</td>'
            f'<td>{_cost_tokens(total_tokens)}</td>'
            f'<td>{_cost_money(total)}</td></tr>')
    return (
        '<table class="costtab costledger">'
        '<caption>What this change cost to produce and to review, at list price — every '
        'turn priced from the transcripts that recorded it. Nobody on a subscription is '
        'billed this; it is what the same tokens would cost on the API.</caption>'
        '<thead><tr><th scope="col">where it went</th><th scope="col">tokens</th>'
        '<th scope="col">cost</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody><tfoot>{foot}</tfoot></table>'
    )


#: The hover on a fold tab's dash: scripts drew it, no model call was made.
FREE_TAB_TIP = ("Nothing spent: scripts built this tab, with no model call, so it "
                "consumed no tokens.")


def _cost_tab_rows(costs: dict, tabs: list[dict], one_col: bool = False) -> str:
    """The per-tab half of the ledger.

    Three shapes of row, because there are three honest answers: a tab with measured spend
    gets its own, biggest first; every measured-zero tab collapses into one muted row that
    names them all (a script wrote that tab, so zero is true, but ten of those stacked
    above the rows carrying the money would bury the point); and every unmeasured tab
    collapses the same way carrying the reason in words, because "we could not measure
    this" must never render identically to a measured zero.
    """
    rows = costs.get("tabs") or {}
    # The strip's own names (`City`, not `Code City`), read where the strip reads them.
    entries = [(layout.SHORT_TAB_LABELS.get(t.get("id"), t.get("label") or t.get("id")),
                rows[t.get("id")]) for t in tabs if rows.get(t.get("id"))]
    # `one_col`: the "This guide" fold — one cost column, tokens on its hover.
    dash = "<td>—</td>" if one_col else "<td>—</td><td>—</td>"

    def nums(tokens, cost, tok_face=None) -> str:
        if one_col:
            return f"<td>{_cost_cell(_cost_money(cost), tokens)}</td>"
        return f"<td>{tok_face or _cost_tokens(tokens)}</td><td>{_cost_money(cost)}</td>"
    if not entries:
        why = costs.get("reason") or "no step ledger, so no turn could be placed in a tab"
        return ('<tr class="costquiet"><td><span class="costnote">'
                f'{html.escape(str(why))}</span></td>{dash}</tr>')

    def spend(r):
        return r.get("cost") or 0.0

    def toks(r):
        return r.get("tokens") or 0

    measured = [e for e in entries if e[1].get("measured")]
    paid = sorted([e for e in measured if spend(e[1]) or toks(e[1])], key=lambda e: -spend(e[1]))
    free = [e for e in measured if not (spend(e[1]) or toks(e[1]))]
    unknown = [e for e in entries if not e[1].get("measured")]

    def names(items):
        return ", ".join(html.escape(str(l)) for l, _ in items)

    def paid_row(label, r) -> str:
        # A tab billed for a `claude -p` step (`bill_model_runs`) says which step, under
        # its name — otherwise "Tests $0.16" contradicts every sentence saying the Tests
        # tab is produced by a script. A run whose token counts were never recorded shows
        # "—", not a 0 beside a price.
        runs = r.get("runs") or []
        sub = "".join(f'<span class="costsub">{html.escape(str(x.get("what") or ""))}, '
                      "claude -p"
                      + (f' on {html.escape(str(x["model"]))}' if x.get("model") else "")
                      + ("" if x.get("tokens") else " &middot; tokens not recorded")
                      + "</span>" for x in runs)
        tok = "—" if r.get("tokensUnknown") and not toks(r) else _cost_tokens(toks(r))
        return (f'<tr><td>{html.escape(str(label))}{sub}</td>'
                + nums(toks(r), spend(r), tok) + '</tr>')

    out = "".join(paid_row(l, r) for l, r in paid)
    if free and one_col:
        # Each tab by name with a dash: plainly nothing spent there (Victor, 4 Oct 2026).
        # The dash explains itself (Victor, 14 Oct 2026): no model, so no tokens.
        out += "".join(f'<tr class="costquiet"><td>{html.escape(str(l))}</td><td>'
                       f'<span class="costnil" data-tip="{FREE_TAB_TIP}">–</span></td></tr>'
                       for l, _ in free)
    elif free:
        out += (f'<tr class="costquiet"><td>{len(free)} tab{"s" if len(free) != 1 else ""} '
                f'with no model spend — {names(free)}</td><td>0</td><td>$0.00</td></tr>')
    if unknown:
        why = costs.get("reason") or "no step in the ledger named them"
        out += (f'<tr class="costquiet"><td>{len(unknown)} tab'
                f'{"s" if len(unknown) != 1 else ""} not measured — '
                f'{html.escape(str(why))} ({names(unknown)})</td>{dash}</tr>')
    resid = costs.get("residual") or {}
    if resid.get("measured"):
        parts = costs.get("residual_parts") or {}
        shown = [(label, what, parts[key]) for key, label, what in RESIDUAL_ROWS
                 if (parts.get(key) or {}).get("messages")]
        if shown:
            out += "".join(
                f'<tr class="costquiet"><td>{label}<span class="costsub">{what}</span></td>'
                + nums(part.get("tokens") or 0, part.get("cost") or 0.0) + '</tr>'
                for label, what, part in shown)
        else:
            out += ("<tr class=\"costquiet\"><td>not one tab's</td>"
                    + nums(resid.get("tokens") or 0, resid.get("cost") or 0.0) + '</tr>')
    return out


# --------------------------------------------------------------------------------------- #
# The four components (`harness_cost.py`): implementation, review, auto-fixes, this guide —
# whichever harness spent each one. Recorded by the runs themselves where they could
# (`review-cost.json` by /record-review, `report-cost.json` by /human-review), derived from
# the stores otherwise, and every row says which.
# --------------------------------------------------------------------------------------- #

_HARNESS = {"claude-code": "Claude Code", "copilot-cli": "Copilot CLI",
            "vscode-copilot": "VS Code Copilot Chat"}


def _aic(n: float) -> str:
    return f"{n:,.1f} AIC" if n < 100 else f"{n:,.0f} AIC"


def _component_money(c: dict, rate: float) -> str:
    """Claude in dollars (list price), Copilot in credits with GitHub's billed dollars
    under them — the two are different kinds of price, and each says which it is."""
    usd, aic = c.get("usd"), c.get("aic")
    if aic is not None and usd is None:
        return (f'{_aic(aic)}<span class="costsub">≈ {_cost_money(aic * rate)} billed'
                '</span>')
    if aic is not None:
        return (f'{_cost_money((usd or 0) + aic * rate)}<span class="costsub">'
                f'{_cost_money(usd or 0)} + {_aic(aic)}</span>')
    return _cost_money(usd or 0.0)


def _minutes(secs) -> str:
    if secs is None:
        return ""
    m = secs / 60
    return f"{m:.0f} min" if m >= 1 else f"{secs:.0f} s"


#: The time column's header hover: which time it is, since two honest ones exist. "time",
#: not "agent" (Victor, 14 Oct 2026: it read as the model's thinking alone, and whether it
#: held the tools' runs was nowhere said). The "you" column beside it is gone the same day:
#: on every row the agent ran by itself it said `none`.
BUSY_TIP = ('<p class="tipfoot">How long the agent was busy, summed over its turns:</p>'
            '<ul class="tiplist prose">'
            "<li>from each prompt to the turn&rsquo;s last reply</li>"
            "<li>the model writing and thinking</li>"
            "<li>plus waiting for its tools: commands, builds, tests, subagents</li>"
            "<li>not the wait for the next prompt</li></ul>"
            '<p class="tipfoot">Hover a time for its split.</p>')


def _duration(secs) -> str:
    """`45 s`, `12 min`, `3 h 05 min` — or `—` when nothing could time it."""
    if secs is None:
        return "—"
    secs = round(secs)
    if secs < 60:
        return f"{secs} s"
    m = round(secs / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def _split(secs, model_secs) -> str:
    """How a busy time splits between the model writing and the tools it ran — the rest of
    the turn (commands, builds, tests, subagents) — as the list the hover shows."""
    model = min(float(model_secs or 0), float(secs))
    return ('<ul class="tiplist prose">'
            f"<li>total {_duration(secs)}</li>"
            f"<li>model thinking and writing {_duration(model)}</li>"
            f"<li>waiting for tools {_duration(secs - model)}</li></ul>")


def _time_cell(secs, model_secs=None, cls: str = "costtime") -> str:
    """The time column: busy time, its model/tools split on the hover (Victor, 7 Oct 2026:
    as a line under every time it doubled the rows' height for a second-order number)."""
    if secs is None:
        return "<td>—</td>"
    tip = (f' data-tip-html="{html.escape(_split(secs, model_secs), quote=True)}"'
           if model_secs else "")
    return f'<td><span class="{cls}"{tip}>{_duration(secs)}</span></td>'


#: What a recorded entry says it was, in the words a reviewer reads in a cost table. The
#: record keeps the pipeline's own phrase; the row's name already says the rest
#: ("Implementation"), and "fork → prepare" was the skill's vocabulary (Victor, 7 Oct 2026).
_WHAT_SHORT = {"the implementing session, fork → prepare": "",
               "requirements↔tests mapping (rerun-model.py)": "requirements↔tests mapping"}


def _entry_line(e: dict, alone: bool = False) -> str:
    who = _HARNESS.get(e.get("harness"), e.get("harness") or "?")
    sid = str(e.get("session") or "")
    if sid.startswith("claude -p"):
        # A model step the run shelled out to: `claude -p on Haiku 4.5`, not the harness
        # name followed by the ledger file it was read from.
        model = ", ".join(e.get("models") or {})
        head = "claude -p" + (f" on {html.escape(model)}" if model else "")
    else:
        # The id muted (Victor, 7 Oct 2026: "who cares… okay, maybe"): there to find the
        # transcript by, not to be read. The window's dates are gone too — the time column
        # says how long, and the dates only made the line long.
        head = html.escape(who) + (f' <code class="costsid">{html.escape(sid[:8])}</code>'
                                   if sid else "")
    what = str(e.get("what") or "")
    what = _WHAT_SHORT.get(what, what)
    bits = [head, html.escape(what)]
    money = (_aic(e["aic"]) if e.get("aic") is not None
             else (_cost_money(e["usd"]) if e.get("usd") is not None else ""))
    if e.get("note"):
        bits.append(html.escape(str(e["note"])))
    return " &middot; ".join(b for b in bits if b)


def _entry_row(e: dict) -> str:
    """One session of a row that has several, as a muted line of its own under the row:
    its words under the name, its time under the time, its price right-aligned under the
    row's price — a breakdown that reads as one (Victor, 7 Oct 2026: the price glued to
    the end of the line read as a second, odd total)."""
    money = (_aic(e["aic"]) if e.get("aic") is not None
             else (_cost_money(e["usd"]) if e.get("usd") is not None else ""))
    busy = e.get("busySeconds")
    when = (_time_cell(busy, e.get("modelSeconds"), "costsub costtime") if busy is not None
            else "<td></td>")
    return (f'<tr class="costpart"><td><span class="costsub">{_entry_line(e)}</span></td>'
            f'{when}<td><span class="costsub">{money}</span></td></tr>')




def _extension_line(r: dict, rate: float) -> str:
    """`extended to the last CI round (3 Oct 08:42 → 08:50): +$0.29 / +1.1M tok since the
    record, which says $1.97 / 6.5M` — on a row the build carried past what the run
    committed (`harness_cost.extend_to_last_round`).

    Eval run 10: the auto-fixes row read $2.26 / 7.7M while the committed
    `review-cost.json` said $1.97 / 6.5M, and the only trace of why was a `source` string
    no reader sees. A figure that departs from the branch's own record says so, by how
    much, and over which stretch."""
    was = r.get("recorded")
    if not isinstance(was, dict):
        return ""
    now_c = (r.get("usd") or 0.0) + (r.get("aic") or 0.0) * rate
    then_c = (was.get("usd") or 0.0) + (was.get("aic") or 0.0) * rate
    d_cost, d_tok = now_c - then_c, (r.get("tokens") or 0) - (was.get("tokens") or 0)
    win = was.get("window") or []
    since = _when(win[1]) if len(win) == 2 else ""
    to = _when(r.get("extendedTo"))
    span = f" ({since} \u2192 {to.split(' ')[-1]})" if since and to else ""
    sign = "+" if d_cost >= 0 else "\u2212"
    tsign = "+" if d_tok >= 0 else "\u2212"
    return (f"extended to the last CI round{span}: {sign}{_cost_money(abs(d_cost))} / "
            f"{tsign}{_cost_tokens(abs(d_tok))} tok since the committed record, which says "
            f"{_cost_money(then_c)} / {_cost_tokens(was.get('tokens') or 0)}")


#: Fish Audio API price, USD per million UTF-8 bytes of input text (docs.fish.audio, "API
#: Pricing", checked 8 Oct 2026). The `-free` model is free; the others bill the same.
FISH_PRICE_PER_M_BYTES = {"s2.1-pro-free": 0.0, "s2.1-pro": 15.0, "s2-pro": 15.0, "s1": 15.0}
FISH_DEFAULT_MODEL = "s2.1-pro-free"


def voice_money(c: float) -> str:
    """The voices' price. Victor wants it shown even when tiny: anything under a cent
    (a free model's exact zero included) reads `< $0.01`."""
    return "< $0.01" if c < 0.01 else f"${c:,.2f}"


def voices_cost(out_dir: Path | None) -> dict | None:
    """What the narration voices cost this report, or None when it has no cloned voice.

    Measured: `assets/*.narration-cost.json`, which `narrate-cue.py` appends to for each
    synthesis that called the API (a cache hit makes no call and costs nothing). A film
    recorded before the ledger existed has none, and is *estimated*: the UTF-8 bytes of its
    cue texts (`*.cues.json`) once per cloned voice (`*.voices.json`), at the model the
    recorder would use today."""
    if not out_dir:
        return None
    assets = out_dir / "assets"
    usd, nbytes, models, voices, estimated = 0.0, 0, set(), set(), False
    for voices_file in sorted(assets.glob("*.voices.json")):
        try:
            data = json.loads(voices_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        film = voices_file.name[:-len(".voices.json")]
        keys = {str(v.get("key")) for v in data if isinstance(v, dict) and v.get("key")}
        if not keys:
            continue
        voices |= keys
        ledger = assets / f"{film}.narration-cost.json"
        if ledger.is_file():
            try:
                rows = json.loads(ledger.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rows = []
            for r in rows if isinstance(rows, list) else []:
                if not isinstance(r, dict):
                    continue
                model, b = str(r.get("model") or FISH_DEFAULT_MODEL), int(r.get("bytes") or 0)
                nbytes += b
                models.add(model)
                usd += b * FISH_PRICE_PER_M_BYTES.get(model, 15.0) / 1e6
            continue
        try:
            cues = json.loads((assets / f"{film}.cues.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = os.environ.get("NARRATION_FISH_MODEL", FISH_DEFAULT_MODEL)
        b = sum(len(str(c.get("text") or "").encode("utf-8")) for c in cues
                if isinstance(c, dict)) * len(keys)
        nbytes += b
        models.add(model)
        usd += b * FISH_PRICE_PER_M_BYTES.get(model, 15.0) / 1e6
        estimated = True
    if not voices:
        return None
    return {"usd": usd, "bytes": nbytes, "models": sorted(models), "voices": len(voices),
            "estimated": estimated}


def voices_row_html(v: dict | None) -> str:
    """The "Voices" row: price in COST, model under it, time blank, muted like a session
    line. Tooltip: bytes, and that the figure is an estimate when no ledger recorded it."""
    if not v:
        return ""
    tips = []
    if v["usd"] <= 0:
        tips.append("free model " + ", ".join(v["models"] or [FISH_DEFAULT_MODEL]))
    tips.append(f"{v['bytes']:,} UTF-8 bytes of narration")
    if v["estimated"]:
        tips.append("estimated: the film predates the narration ledger, so this is its cue "
                    "texts once per cloned voice at the current model's price")
    n = v["voices"]
    model = " / ".join(v["models"]) or FISH_DEFAULT_MODEL
    return ('<tr class="costquiet" data-component="voices"><td>Voices'
            f'<span class="costsub">Fish Audio, {n} voice{"s" if n != 1 else ""}</span></td>'
            '<td></td>'
            f'<td><span class="costmoney" data-tip="{html.escape("; ".join(tips), quote=True)}">'
            f'{html.escape(voice_money(v["usd"]))}</span><span class="costsub">{html.escape(model)}</span>'
            '</td></tr>')


def components_html(comp: dict | None, fold: str = "", voices: dict | None = None) -> str:
    """The four rows the cost tab leads with, or "" when nothing at all was measured.

    `fold` is the "This guide" row's tab-by-tab breakdown, opened from that row's name.
    One number column (Victor, 4 Oct 2026): the price, its tokens on the hover, the model
    under it; no hint line under each name, no unit caption under the total."""
    import harness_cost
    rows = [r for r in (comp or {}).get("rows") or [] if isinstance(r, dict)]
    if not rows or not any(r.get("measured") for r in rows):
        return ""
    rate = float((comp or {}).get("aicUsd") or 0.01)
    out = []
    for r in rows:
        raw = str(r.get("label") or r.get("key"))
        label = html.escape(raw[:1].upper() + raw[1:])
        if not r.get("measured"):
            why = html.escape(str(r.get("reason") or "not measured"))
            out.append(f'<tr class="costquiet" data-component="{html.escape(r["key"])}">'
                       f'<td>{label}<span class="costsub">unmeasured — {why}</span></td>'
                       '<td>—</td><td>—</td></tr>')
            continue
        entries = r.get("entries") or []
        parts = len(entries) > 1
        lines = [] if parts else [_entry_line(e, alone=True) for e in entries]
        if r.get("key") == "guide":
            wall = (comp or {}).get("wallclock") or {}
            took = _minutes(wall.get("seconds"))
            extra = (comp or {}).get("refreshSeconds") or 0
            if r.get("busySeconds") is not None:
                # The run's time is in the time column now; only the refreshes after it,
                # which are not, still need words.
                if extra:
                    lines.append(f"plus {_minutes(extra)} of later refreshes, no model")
            elif took:
                model = wall.get("modelSeconds")
                lines.append(f"took {took}" + (f", of which model {_minutes(model)}"
                                                if model else "")
                             + (f"; plus {_minutes(extra)} of refreshes, no model"
                                if extra else ""))
        # Plain text, on the label's hover — eval run 11: as a visible line it was audit
        # trivia for a stressed reader, and the widest thing on the tab.
        ext = _extension_line(r, rate)
        if r.get("dropped"):
            gone = "; ".join(
                f"session {str(d.get('session') or '?')[:8]}"
                + (f" ({_cost_money(d['usd'])})" if d.get("usd") is not None else "")
                + f", whose work {d.get('undoneBy') or 'a later commit'} reverted to the base"
                for d in r["dropped"])
            ext = (ext + " · " if ext else "") + "left out: " + gone
        if r.get("source") == "derived":
            # Copy pass (3 Oct 2026): which record file was missing is the pipeline's
            # business; the reader needs to know the number is an estimate.
            lines.append("estimated from session logs")
        sub = "".join(f'<span class="costsub">{l}</span>' for l in lines if l)
        models: dict = {}
        model_usd: dict = {}
        for e in entries:
            for k, v in (e.get("models") or {}).items():
                models[k] = models.get(k, 0) + v
        for k in models:
            # A model's price on the row only when every entry that used it priced it.
            used = [e for e in entries if (e.get("models") or {}).get(k)]
            if used and all(k in (e.get("modelUsd") or {}) for e in used):
                model_usd[k] = sum(e["modelUsd"][k] for e in used)
        if ext:
            label = f'<span data-tip="{html.escape(ext, quote=True)}">{label}</span>'
        folds = r.get("key") == "guide" and fold
        if folds:
            # Its own sessions' lines may stand between this row and its fold. The hint
            # beside the name is CSS (`.costexp::after`): Victor (7 Oct 2026) had never
            # noticed the bare caret.
            label = (f'<button type="button" class="costexp" aria-expanded="false" '
                     'onclick="var t=this.closest(\'tr\');'
                     'do{t=t.nextElementSibling}while(t&amp;&amp;!t.classList.contains(\'costfold\'));'
                     'if(t){t.hidden=!t.hidden;this.setAttribute(\'aria-expanded\',!t.hidden)}">'
                     f'{label}<span class="costexp-ico" aria-hidden="true"></span></button>')
        # Time before the cost: the money stays the last column, where the page's
        # "Prompt to get this" button sits in the header, beside `cost`.
        out.append(f'<tr data-component="{html.escape(r["key"])}"><td>{label}{sub}</td>'
                   + _time_cell(r.get("busySeconds"), r.get("modelSeconds"))
                   + f'<td>{_cost_cell(_component_money(r, rate), r.get("tokens") or 0, models, model_usd, harness_cost.row_efforts(entries))}'
                   '</td></tr>')
        if parts:
            out.append("".join(_entry_row(e) for e in entries))
        if folds:
            out.append(f'<tr class="costfold" hidden><td colspan="3">{fold}</td></tr>')
    out.append(voices_row_html(voices))
    usd, aic = comp.get("usd") or 0.0, comp.get("aic") or 0.0
    total = usd + aic * rate + ((voices or {}).get("usd") or 0.0)
    # Under the total, only what the column cannot say by itself: two kinds of price.
    sub = ""
    if aic:
        sub = (f"Copilot {_aic(aic)} = {_cost_money(aic * rate)} at GitHub's "
               f"${rate:.2f} per AI credit" + (f" + Claude {_cost_money(usd)} at API list "
                                               "price" if usd else ""))
    if comp.get("unmeasured"):
        sub += ((" · " if sub else "") + "not counted: " + ", ".join(comp["unmeasured"]))
    tokens = sum(r.get("tokens") or 0 for r in rows if r.get("measured"))
    foot = (f'<tr class="costtotal"><td>Total'
            + (f'<span class="costsub">{html.escape(sub)}</span>' if sub else "")
            + '</td>'
            + _time_cell(comp.get("busySeconds"),
                         sum(r.get("modelSeconds") or 0 for r in rows if r.get("measured")))
            + f'<td>{_cost_cell(_cost_money(total), tokens)}</td></tr>')
    priced = [r for r in rows if r.get("measured")]
    has_copilot = any(r.get("aic") is not None for r in priced)
    caption = "Copilot in AI credits, at what GitHub bills for them." if has_copilot else ""
    # A partial bill says so before its first row, in the words the pill's name uses:
    # which parts are missing and that the total is only the rest.
    missing = [r for r in rows if not r.get("measured")]
    warn = (f'<p class="costpartial">Partial bill: {len(missing)} of {len(rows)} parts '
            f'not measured — {html.escape(", ".join(str(r.get("label") or r.get("key")) for r in missing))}. '
            f'The {_cost_money(total)} is the other {len(rows) - len(missing)} only; the '
            'real bill is larger by what nothing on this disk recorded (why, on each row '
            'below).</p>' if missing else "")
    return (warn + '<table class="costtab costledger costfour">'
            + (f'<caption>{caption}</caption>' if caption else '') +
            # "step" (Victor, 7 Oct 2026): the rows are the steps the change went through.
            '<thead><tr><th scope="col">step</th>'
            f'<th scope="col"><span data-tip-html="{html.escape(BUSY_TIP, quote=True)}">time</span>'
            '</th><th scope="col">cost</th></tr></thead>'
            f'<tbody>{"".join(out)}</tbody><tfoot>{foot}</tfoot></table>')


def cost_pill_title(led: dict) -> str:
    """The pill's hover: what its number covers, and — when part of the bill is not on
    this disk — which part, and how much of the four the number leaves out. Eval run 8's
    pill read `$2?` over a total that was one component of four; the `?` said nothing a
    reader could act on."""
    comp = led.get("components") or {}
    rows = [r for r in comp.get("rows") or [] if isinstance(r, dict)]
    if not components_html(comp):
        return "what this change cost to write and review"
    total = f'${comp.get("usdEquivalent") or 0.0:,.2f}'
    missing = [r for r in rows if not r.get("measured")]
    if not missing:
        return (f"{total} — all four parts measured: "
                + ", ".join(str(r.get("label") or r.get("key")) for r in rows))
    names = ", ".join(str(r.get("label") or r.get("key")) for r in missing)
    return (f"{total} covers {len(rows) - len(missing)} of {len(rows)} parts — not counted: "
            f"{names} ({len(missing)} of {len(rows)} missing, so the real bill is larger by "
            f"an amount nothing on this disk recorded). Why: "
            + "; ".join(f"{r.get('label') or r.get('key')}: {r.get('reason') or 'not measured'}"
                        for r in missing))


def cost_pill_label(led: dict) -> str:
    """The tab's label: the four components' total when they were measured, else the
    ledger's own — with `?` when a part of the bill is not on this disk."""
    comp = led.get("components") or {}
    if components_html(comp):
        label = f'${comp.get("usdEquivalent") or 0.0:,.0f}'
        return label + ("?" if comp.get("unmeasured") else "")
    phase_total = (led.get("phases") or {}).get("cost")
    label = f'${(phase_total if phase_total is not None and phase_rows_html(led.get("phases")) else (led.get("total") or 0.0)):,.0f}'
    if (led.get("writing") or {}).get("otherAgents"):
        label += "?"
    return label
