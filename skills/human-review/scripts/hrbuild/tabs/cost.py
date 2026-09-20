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

from ..shared.util import HERE

def cost_chip(root: Path) -> dict | None:
    """What this review run consumed, asked of the run itself.

    Returns None — dropping the chip rather than showing a wrong one — whenever the answer
    cannot be trusted: no session id in the environment (the page was built outside a
    Claude Code session), or no transcript for it.
    """
    script = HERE / "review-cost.py"
    if not script.is_file():
        return None
    proc = subprocess.run([sys.executable, str(script), "--chip"],
                          cwd=root, capture_output=True, text=True)
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
    for f in (script, out_dir / ".steps.json", out_dir / ".session",
              out_dir / "phases.json"):
        try:
            st = f.stat()
            h.update(f"{f.name}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
        except OSError:
            h.update(b"\0gone\0")
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not sid:
        try:
            sid = (out_dir / ".session").read_text(encoding="utf-8").strip()
        except OSError:
            sid = ""
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
        cwd=root, capture_output=True, text=True,
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
RESIDUAL_ROWS = [
    ("guide", "assembling the guide itself — Step 9 writes every tab&rsquo;s prose in one pass"),
    ("subagent", "subagent work that fell outside every step&rsquo;s window"),
    ("conversation", "the orchestrating conversation — reading, deciding, recovering"),
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
    text = (big[0][0] if len(big) == 1 else
            " / ".join(f"{k} {v / total * 100:.0f}%" for k, v in big))
    return f'{out}<span class="costsub">{html.escape(text)}</span>'


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
    the seconds are not, because nothing here is timed to the second."""
    if not raw:
        return ""
    try:
        t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{t.day} {t.strftime('%b')} {t:%H:%M}"


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


def cost_ledger_html(led: dict | None, tabs: list[dict]) -> str:
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


def _cost_tab_rows(costs: dict, tabs: list[dict]) -> str:
    """The per-tab half of the ledger.

    Three shapes of row, because there are three honest answers: a tab with measured spend
    gets its own, biggest first; every measured-zero tab collapses into one muted row that
    names them all (a script wrote that tab, so zero is true, but ten of those stacked
    above the rows carrying the money would bury the point); and every unmeasured tab
    collapses the same way carrying the reason in words, because "we could not measure
    this" must never render identically to a measured zero.
    """
    rows = costs.get("tabs") or {}
    entries = [(t.get("label") or t.get("id"), rows[t.get("id")])
               for t in tabs if rows.get(t.get("id"))]
    if not entries:
        why = costs.get("reason") or "no step ledger, so no turn could be placed in a tab"
        return ('<tr class="costquiet"><td><span class="costnote">'
                f'{html.escape(str(why))}</span></td><td>—</td><td>—</td></tr>')

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

    out = "".join(f'<tr><td>{html.escape(str(l))}</td><td>{_cost_tokens(toks(r))}</td>'
                  f'<td>{_cost_money(spend(r))}</td></tr>' for l, r in paid)
    if free:
        out += (f'<tr class="costquiet"><td>{len(free)} tab{"s" if len(free) != 1 else ""} '
                f'with no model spend — {names(free)}</td><td>0</td><td>$0.00</td></tr>')
    if unknown:
        why = costs.get("reason") or "no step in the ledger named them"
        out += (f'<tr class="costquiet"><td>{len(unknown)} tab'
                f'{"s" if len(unknown) != 1 else ""} not measured — '
                f'{html.escape(str(why))} ({names(unknown)})</td>'
                '<td>—</td><td>—</td></tr>')
    resid = costs.get("residual") or {}
    if resid.get("measured"):
        parts = costs.get("residual_parts") or {}
        shown = [(label, parts[key]) for key, label in RESIDUAL_ROWS
                 if (parts.get(key) or {}).get("messages")]
        if shown:
            out += "".join(
                f'<tr class="costquiet"><td>{label}</td>'
                f'<td>{_cost_tokens(part.get("tokens") or 0)}</td>'
                f'<td>{_cost_money(part.get("cost") or 0.0)}</td></tr>'
                for label, part in shown)
        else:
            out += ("<tr class=\"costquiet\"><td>not one tab's — assembling the guide "
                    "itself, plus any step whose window did not cover it</td>"
                    f'<td>{_cost_tokens(resid.get("tokens") or 0)}</td>'
                    f'<td>{_cost_money(resid.get("cost") or 0.0)}</td></tr>')
    return out
