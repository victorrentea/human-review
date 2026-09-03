#!/usr/bin/env python3
"""A start/end ledger mapping pipeline steps to the tabs they fed.

Nothing today links one turn of the assembling conversation to one tab of the finished
page — the cost chip (`review-cost.py --chip`) only ever answers for the whole run. This
is the missing link: as each step in SKILL.md runs, it stamps when it started and when it
finished into `.human-review/.steps.json`, naming the tab(s) the step's output lands on.
`review-cost.py --tab-costs` then attributes transcript turns to tabs by which step's
window their timestamp falls inside.

Usage, around any step that produces a tab's content:

    STEP=$(steps-ledger.py start data,packages --label "diagram deltas")
    ${SKILL}/scripts/puml-diff.sh $BASE .human-review/assets/diagrams
    steps-ledger.py end "$STEP"

`start` prints the record's index — the handle `end` needs to close the right one, since
two steps can be mid-flight at once (nested shell calls, a step that shells out to another
script that itself stamps a step). A step that crashes between `start` and `end` leaves a
record with `"end": null` in the ledger — an honest trace of a step that began and never
finished, which `--tab-costs` reports as "started but never recorded finishing" rather than
folding it into whichever step happens to run next.

The ledger is a plain JSON array, read-modify-written on every call — but never with a
bare `write_text`, and never without holding a lock, for two reasons that both showed up
as real risks rather than theoretical ones:

* **A truncated write loses the whole run.** `load()` treats unparseable JSON as an empty
  ledger, so a process killed halfway through rewriting the file does not corrupt one
  record, it silently discards every record stamped so far and the page then reports every
  tab as "not measured". The write therefore goes to a sibling temp file and lands with
  `os.replace`, which is atomic on the same filesystem: a reader sees the old array or the
  new one, never half of either.
* **"One step at a time" is a property of SKILL.md, not of this file.** The runbook does
  order its steps, but `end` for one step and `start` for the next are separate processes,
  and nothing stops a future step from being backgrounded or run from a subagent that also
  stamps. Two overlapping read-modify-writes would drop whichever record lost the race —
  invisibly, since the survivor is perfectly valid JSON. An `flock` on a sidecar file makes
  the read, the append and the replace one critical section, so the loser waits instead of
  overwriting. Locking is advisory and costs nothing on the uncontended path this normally
  takes.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_PATH = ".human-review/.steps.json"

# A reserved tab id that is deliberately not a tab: Step 9 stamps its own assembly of
# `content.json` against it, so the biggest unattributable chunk of a run gets a name instead
# of vanishing into the residual. `check` must not report it as drift, and `review-cost.py`
# holds the same constant — `test_steps_ledger.py` pins the two together.
GUIDE_TAB = "guide"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[steps-ledger] {path} is not valid JSON — starting a fresh ledger, "
              "the old file is left in place unread", file=sys.stderr)
        return []
    return data if isinstance(data, list) else []


def save(path: Path, records: list[dict]) -> None:
    """Write the array so a reader never sees a half-written one (see the module docstring).

    `tempfile.mkstemp` in the *same* directory, not `/tmp`: `os.replace` is only atomic
    within one filesystem, and `.human-review/` is routinely on a different mount from the
    system temp dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(records, indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: Path):
    """Hold the ledger's lock for one read-modify-write.

    The lock lives on `<ledger>.lock` rather than on the ledger itself, because `os.replace`
    swaps the ledger's inode out from under any descriptor held on it — a lock taken on the
    file being replaced protects nothing after the first write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def start(path: Path, tabs: list[str], label: str = "", rev: str = "") -> int:
    """`rev` records the revision the step began from — the *before* side of whatever it is
    about to change. Step 1 uses it for the pre-fix HEAD, because a diff needs a left side and
    reconstructing one afterwards is archaeology, not lookup (see SKILL.md, Step 1).

    Omitted from the record entirely when empty, so a ledger from a step that has no such
    revision stays the shape it always was."""
    with locked(path):
        records = load(path)
        rec = {"tabs": tabs, "label": label, "start": _now(), "end": None}
        if rev:
            rec["rev"] = rev
        records.append(rec)
        save(path, records)
        return len(records) - 1


def end(path: Path, index: int) -> bool:
    with locked(path):
        records = load(path)
        if not (0 <= index < len(records)):
            print(f"[steps-ledger] no record #{index} in {path} — nothing stamped",
                  file=sys.stderr)
            return False
        if records[index].get("end") is not None:
            print(f"[steps-ledger] record #{index} in {path} already has an end — "
                  "overwriting it rather than leaving the earlier one to look authoritative",
                  file=sys.stderr)
        records[index]["end"] = _now()
        save(path, records)
        return True


def reset(path: Path) -> list[str]:
    """Clear last run's ledger *and* its handle files, and say what was removed.

    Step 0 wipes `.human-review/assets/` for exactly this reason and the ledger needs the
    same treatment, but the failure it prevents is nastier than a stale artifact. A ledger
    left over from the previous run parses fine, so `--tab-costs` reports `ledger: True` and
    every tab as `has_closed: True` — measured — while this run's turns all fall outside
    last run's windows. The page then prints a confident `$0.00` against every tab and dumps
    the entire bill in the residual. A *measured zero* is the one answer the whole design
    goes out of its way never to give by accident.

    The `.step-<name>` handle files have to go with it, and that is not tidiness either: a
    handle surviving into a run whose ledger was cleared points at an index that now belongs
    to some *other* step, so `end` would close a record it never opened.
    """
    removed = []
    for victim in [path, path.with_name(path.name + ".lock"),
                   *sorted(path.parent.glob(".step-*"))]:
        if victim.exists():
            victim.unlink()
            removed.append(str(victim))
    return removed


def content_tab_ids(path: Path) -> list[str] | None:
    """The `tabs[].id` list the finished page will actually have, or None if unreadable."""
    if not path.is_file():
        return None
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict):
        return None
    return [t["id"] for t in (spec.get("tabs") or [])
            if isinstance(t, dict) and isinstance(t.get("id"), str) and t["id"]]


def check(path: Path, content: Path) -> tuple[int, list[str]]:
    """Does every tab id the ledger names still exist on the page? Returns `(exit, lines)`.

    The step-to-tab binding is a bare string written in two places — a `start` call in the
    runbook and a `tabs[].id` in `content.json` — and nothing but this makes them agree.
    Rename the tab and the ledger goes on stamping the old id: attribution finds no row for
    it, the tokens fall into the residual, and the tab reports "not measured", which is
    indistinguishable from a step nobody instrumented. Every other check in this pipeline is
    a build-time comparison of two artifacts that must agree, and this is the one pair that
    had none.

    Run it in Step 9, after `content.json` is written and before the page is built, so the
    drift is a line in the terminal at the moment it can still be fixed — not a silently
    empty column in the finished guide.
    """
    lines: list[str] = []
    ids = content_tab_ids(content)
    if ids is None:
        return 2, [f"[steps-ledger] cannot read tab ids from {content} — nothing to check "
                   "the ledger against"]
    records = load(path)
    if not records:
        return 1, [f"[steps-ledger] {path} holds no step records — every tab on the page "
                   "will report 'not measured'. Did Step 0 run `reset` and then no step "
                   "stamp itself?"]

    named = {t for r in records for t in (r.get("tabs") or [])}
    orphans = sorted(named - set(ids) - {GUIDE_TAB})
    unfed = sorted(t for t in ids if t not in named)
    open_records = [r for r in records if r.get("end") is None]

    exit_code = 0
    if orphans:
        exit_code = 1
        lines.append(
            f"[steps-ledger] DRIFT: the ledger stamps tab(s) {', '.join(orphans)} that "
            f"{content} does not have (it has: {', '.join(ids)}). Either a step wrap in "
            "SKILL.md names an id the page renamed, or a step stamped a tab whose content "
            "was dropped. Every one of those tabs' tokens is now in the residual.")
    if unfed:
        lines.append(
            f"[steps-ledger] tab(s) {', '.join(unfed)} have no step in the ledger and will "
            "read 'not measured' — correct only if they are synthesised from other tabs.")
    for r in open_records:
        lines.append(
            f"[steps-ledger] step {r.get('label') or r.get('tabs')} started and never "
            "recorded finishing — its tab reports a lower bound.")
    # The pre-fix revision is the one piece of a run that cannot be recovered after the fact:
    # once fixes are applied, squashed, or folded into a feature commit, no commit message or
    # transcript reliably says what the tree looked like before. Warn rather than fail — a
    # project with no Auto-fixed tab needs no such rev — but warn loudly, because the cost of
    # noticing later is reconstructing it by hand and getting it wrong.
    if any("review" in (r.get("tabs") or []) for r in records) and not any(
            r.get("rev") for r in records if "review" in (r.get("tabs") or [])):
        lines.append(
            "[steps-ledger] the `review` step recorded no --rev — nothing says which revision "
            "the automated fixes were applied on top of, so every before/after diff on the "
            "Auto-fixed tab has to be reconstructed by guesswork.")

    if GUIDE_TAB not in named:
        lines.append(
            f"[steps-ledger] Step 9 never stamped itself against `{GUIDE_TAB}` — assembling "
            "the page is normally the most expensive stretch of a run, and unstamped it is "
            "indistinguishable from dead time in the residual.")
    if not lines:
        lines.append(f"[steps-ledger] {len(records)} steps, {len(named) - 1} tabs plus "
                     f"`{GUIDE_TAB}`, all matching {content}.")
    return exit_code, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH, help="the ledger file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="stamp the start of a step")
    p_start.add_argument("tabs", help="comma-separated tab ids this step's output feeds")
    p_start.add_argument("--label", default="", help="a short name for the step, for humans")
    p_start.add_argument("--rev", default="",
                         help="the revision this step starts from, recorded so a later diff "
                              "has a left side (Step 1: the pre-fix HEAD)")

    p_end = sub.add_parser("end", help="stamp the end of a step")
    p_end.add_argument("index", type=int, help="the index `start` printed")

    sub.add_parser("reset", help="clear last run's ledger and its .step-* handle files")

    p_check = sub.add_parser(
        "check", help="fail if the ledger names a tab content.json does not have")
    p_check.add_argument("--content", default=".human-review/content.json",
                         help="the content file whose tabs[].id list is the truth")

    args = ap.parse_args(argv)
    path = Path(args.path)

    if args.cmd == "check":
        code, lines = check(path, Path(args.content))
        for line in lines:
            print(line, file=sys.stderr if code else sys.stdout)
        return code

    if args.cmd == "reset":
        for gone in reset(path):
            print(f"[steps-ledger] cleared {gone}", file=sys.stderr)
        return 0

    if args.cmd == "start":
        tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
        if not tabs:
            print("[steps-ledger] no tab ids given — nothing to attribute this step to",
                  file=sys.stderr)
            return 2
        print(start(path, tabs, args.label, args.rev))
        return 0

    return 0 if end(path, args.index) else 1


if __name__ == "__main__":
    raise SystemExit(main())
