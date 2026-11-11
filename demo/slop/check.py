#!/usr/bin/env python3
"""Did each planted decision reach the page? One check per row of README.md.

Reads only what a /human-review build left in `<checkout>/.human-review/` (plus the
checkout's own files where the artifact is the code itself). Prints one line per row:

    FOUND    the signal the row promises is there
    MISSING  the build ran, the signal is not there
    NO DATA  the artifact the check reads was never produced

    demo/slop/check.py demo2                       # ~/workspace/petclinic-visits-paginated
    demo/slop/check.py demo2 --checkout DIR
    demo/slop/check.py demo --all                  # also the optional rows (F, G)

Exit 0 when every planned row of that demo is FOUND, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

HOME = Path.home()
HR = Path(__file__).resolve().parents[2]
CHECKOUT = {
    "demo": HOME / "workspace/petclinic-pr-visit-has-vet",
    "demo2": HOME / "workspace/petclinic-visits-paginated",
}


class NoData(Exception):
    pass


def load(c: Path, rel: str):
    p = c / ".human-review" / rel
    if not p.is_file():
        raise NoData(rel)
    return json.loads(p.read_text()) if p.suffix == ".json" else p.read_text()


def manifest(c: Path) -> dict:
    p = c / ".human-review/assets/diagrams/MANIFEST.tsv"
    if not p.is_file():
        raise NoData("assets/diagrams/MANIFEST.tsv")
    with p.open() as f:
        return {r["source"]: r["status"] for r in csv.DictReader(f, delimiter="\t")}


def changed_logs(c: Path) -> list[dict]:
    return load(c, "assets/logging.json").get("changed", {}).get("logging", [])


def tests(c: Path) -> list[dict]:
    return load(c, "assets/test-changes.json").get("tests", [])


# --------------------------------------------------------------------------- checks
# Each returns (found, evidence).

def a_repo_dto(c):
    status = manifest(c).get("petclinic-backend/docs/packages.puml", "absent")
    puml = (c / "petclinic-backend/docs/packages.puml").read_text()
    edge = bool(re.search(r"\[\.\.repository\]\s*-+>\s*\[\.\.rest\.dto\]", puml))
    owners = "docs/packages.puml" in load(c, "assets/codeowners.html")
    return (status == "modified" and edge and owners,
            f"packages diagram {status}, repository->rest.dto edge {edge}, CODEOWNERS {owners}")


def b_gherkin_outline(c):
    f = c / "petclinic-test/src/visits-sort.feature"
    text = f.read_text() if f.is_file() else ""
    rows = len(re.findall(r"^\s*\|.*\b(asc|desc)\b.*\|\s*$", text, re.M | re.I))
    outline = "Scenario Outline" in text and "Examples" in text
    listed = any(t["path"].endswith("visits-sort.feature") for t in tests(c))
    return (outline and rows >= 8 and listed,
            f"outline {outline}, {rows} example rows, in test ledger {listed}")


def c_owner_names_log(c):
    def says_owner(e):
        seen = {k: e[k] for k in ("args", "origins", "text", "raw_line") if k in e}
        return re.search(r"(?i)owner", json.dumps(seen)) is not None
    hits = [e for e in changed_logs(c) if "VisitRestController" in e["file"] and says_owner(e)]
    raw_sort = any(re.search(r"\bsort\b", a) for e in hits for a in e.get("args", []))
    return (bool(hits) and not raw_sort,
            f"{len(hits)} log line(s) carrying owner names, raw sort logged {raw_sort}")


def d_disabled(c):
    off = [t["name"] for t in tests(c)
           if t["path"].endswith("VisitTest.java") and t.get("silenced") == "disabled"]
    return len(off) >= 2, f"@Disabled in VisitTest: {', '.join(off) or 'none'}"


def e_sort_ladder(c):
    cc = [e.get("flowCc") or 0 for e in load(c, "assets/complexity-after.json")
          if str(e.get("handler", "")).startswith("VisitRestController.listVisits")]
    top = max(cc, default=0)
    return top >= 15, f"GET /api/visits flow complexity {top} (planted: >= 15)"


def f_dependency(c):
    html = load(c, "assets/codeowners.html")
    ok = "@victorrentea/elders" in html and "package.json" in html
    return ok, f"elders approval on package.json {ok}"


def g_denormalized(c):
    m = manifest(c)
    db, dm = (m.get(f"petclinic-backend/docs/generated/{n}", "absent")
              for n in ("DB.puml", "DomainModel.puml"))
    return db == "modified" and dm == "modified", f"DB {db}, DomainModel {dm}"


def h_raw_select(c):
    reg = load(c, "assets/ds-audit.json").get("summary", {}).get("regressions", [])
    return "new:select#vet" in reg, f"ds-audit regressions {reg}"


def i_n_plus_one(c):
    d = c / ".human-review/assets/diagrams"
    if not d.is_dir():
        raise NoData("assets/diagrams")
    worst, where = 0, ""
    for p in d.glob("*genseq.diff.puml"):
        n = sum(1 for ln in p.read_text().splitlines()
                if re.search(r"->\s*DB:", ln) and re.search(r"\bvets?\b", ln, re.I))
        if n > worst:
            worst, where = n, p.name
    return worst >= 3, f"most vet SELECTs in one diagram: {worst} {where}"


def j_unpinned_clear(c):
    body = load(c, "ticket-body.json")["body"]
    spec = importlib.util.spec_from_file_location(
        "semcov", HR / "skills/human-review/scripts/semcov.py")
    semcov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(semcov)
    sid = next((s["id"] for s in semcov.ticket_sentences(semcov.parse_ticket(body))
                if "Clearing the field" in s["text"]), None)
    cov = next((s["coverage"] for s in load(c, "assets/test-mapping.merged.json")["sentences"]
                if s["id"] == sid), "absent")
    return cov == "missing", f"'Clearing the field must persist as empty.' is {cov}"


def k_phone_log(c):
    hits = [e for e in changed_logs(c) if any(re.search(r"(?i)telephone", a)
                                              for a in e.get("args", []))]
    return bool(hits), f"{len(hits)} log line(s) carrying a telephone"


ROWS = {  # id: (demo, optional, check)
    "A": ("demo2", False, a_repo_dto),
    "B": ("demo2", False, b_gherkin_outline),
    "C": ("demo2", False, c_owner_names_log),
    "D": ("demo2", False, d_disabled),
    "E": ("demo2", False, e_sort_ladder),
    "F": ("demo2", True, f_dependency),
    "G": ("demo2", True, g_denormalized),
    "H": ("demo", False, h_raw_select),
    "I": ("demo", False, i_n_plus_one),
    "J": ("demo", False, j_unpinned_clear),
    "K": ("demo", False, k_phone_log),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("demo", choices=sorted(CHECKOUT))
    ap.add_argument("--checkout", type=Path)
    ap.add_argument("--all", action="store_true", help="also check the optional rows")
    a = ap.parse_args()
    c = (a.checkout or CHECKOUT[a.demo]).expanduser().resolve()
    missing = 0
    print(f"{a.demo} @ {c}")
    for rid, (demo, optional, check) in ROWS.items():
        if demo != a.demo or (optional and not a.all):
            continue
        try:
            ok, why = check(c)
            word = "FOUND" if ok else "MISSING"
        except NoData as e:
            ok, word, why = False, "NO DATA", f"{e} was never produced"
        missing += 0 if ok or optional else 1
        print(f"  {rid}  {word:<8} {why}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
