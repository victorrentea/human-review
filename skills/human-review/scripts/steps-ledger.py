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

The ledger is a plain JSON array, read-modify-written on every call. That is safe here
because the pipeline that writes it runs one step at a time in one conversation — there is
no concurrent writer to race.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

DEFAULT_PATH = ".human-review/.steps.json"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=1) + "\n", encoding="utf-8")


def start(path: Path, tabs: list[str], label: str = "") -> int:
    records = load(path)
    records.append({"tabs": tabs, "label": label, "start": _now(), "end": None})
    save(path, records)
    return len(records) - 1


def end(path: Path, index: int) -> bool:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH, help="the ledger file")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="stamp the start of a step")
    p_start.add_argument("tabs", help="comma-separated tab ids this step's output feeds")
    p_start.add_argument("--label", default="", help="a short name for the step, for humans")

    p_end = sub.add_parser("end", help="stamp the end of a step")
    p_end.add_argument("index", type=int, help="the index `start` printed")

    args = ap.parse_args(argv)
    path = Path(args.path)

    if args.cmd == "start":
        tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
        if not tabs:
            print("[steps-ledger] no tab ids given — nothing to attribute this step to",
                  file=sys.stderr)
            return 2
        print(start(path, tabs, args.label))
        return 0

    return 0 if end(path, args.index) else 1


if __name__ == "__main__":
    raise SystemExit(main())
