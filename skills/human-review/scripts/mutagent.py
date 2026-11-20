#!/usr/bin/env python3
"""Mutagent 🧬 — creative, targeted mutation testing of the tests a PR says prove its ticket.

The Tests tab pairs each sentence of the ticket with the tests that *claim* to prove it
(`test-mapping.json`). A claim is only as good as the assertion behind it: a test that
inserts A, B, C and expects A, B, C back passes with or without the `ORDER BY` it is said
to prove. PIT would find that eventually, slowly and noisily, by mutating every line; this
asks a model for a handful of *semantic* mutants aimed at exactly those claims, and then
runs only the tests each one is expected to break.

    mutagent.py generate --dir .human-review [--model sonnet] [--max 6]
    mutagent.py run      --dir .human-review [--config mutagent.json] [--only 01-x,02-y]

`generate` is one `claude -p` call, no tools, everything in the prompt
(`reference/mutagent-prompt.md`): the claimed sentences with their tests, and the PR's
changed production files as they are at HEAD. The model answers with search/replace edits;
this program turns each into a patch, keeps only those that `git apply --check` against
HEAD, and writes `<dir>/mutagent/mutants.json` (schema `mutagent/1`) plus one
`NN-slug.patch` per mutant. Its cost goes in `<dir>/.mutagent-runs.json`, the ledger every
paid step keeps, so the Cost tab and the press's price read it.

`run` never touches the reviewed checkout — it may hold uncommitted work. It makes one
throwaway detached `git worktree` of HEAD in a temp dir, runs the expected tests there
once unmutated (a test red without any mutant proves nothing either way), then for each
mutant applies its patch, runs its expected tests, records each one's pass/fail, and resets
the worktree; the worktree is removed at the end. Verdicts, in `<dir>/mutagent/results.json`:

  killed     an expected test that was green failed: the test proves the sentence
  survived   every expected test stayed green: the test does not prove what it claims
  broken     the patch did not apply, or the mutated code did not compile
  not-run    no runner for its tests (browser e2e, Karma) or they fail without the mutant

Runners. The skill knows nothing about the project, so it reads an optional
`steps.mutagent` block from the repository's `human-review.json` (or `--config`):

    "mutagent": {"suites": {"java": {
        "files": ["petclinic-backend/src/test/java/**/*.java"], "exclude": [],
        "cwd": "petclinic-backend", "item": "{class}#{name}", "join": ",",
        "run": "./test-on-own-db.sh mvn -o -q test -Dtest={tests} -Dsurefire.failIfNoSpecifiedTests=false",
        "reports": "target/surefire-reports"}}}

`{tests}` is the expected tests of that suite, each spelt as `item` (`{class}`, `{name}`,
`{file}`, `{line}`) and joined by `join`, shell-quoted as one word unless `"quote": false`.
`cwd` may be `{module}`: the nearest directory holding a pom.xml / build.gradle. Results
are read from the JUnit XML under `reports`. Without a block that matches, JVM tests under
`src/test/java` run with Maven (`mvn -o`, surefire XML) or Gradle; anything else is not-run,
with the reason.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fnmatch
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT = HERE.parent / "reference" / "mutagent-prompt.md"

SCHEMA = "mutagent/1"
RESULTS_SCHEMA = "mutagent-results/1"
#: Under the review directory, not dot-prefixed: the mutants and their verdicts are evidence
#: about the branch and travel with a published page, the way the matrix does.
SUBDIR = "mutagent"
MUTANTS = "mutants.json"
RESULTS = "results.json"
#: This press's own spending, beside `.model-runs.json` and `.film-runs.json`: a handful of
#: mutants is a different amount of work from the matrix, and one average over both would
#: be the right price for neither.
RUNS_LEDGER = ".mutagent-runs.json"
LOOKS_LIKE_A_REVIEW = "content.json"
CONFIG = "human-review.json"

DEFAULT_MAX = 6
#: How much production source goes in the prompt, whole files first, diffs after.
FILE_BUDGET = 40_000
TOTAL_BUDGET = 160_000
#: One suite run, at most. A mutant that loops forever is killed by this, not by a test.
RUN_TIMEOUT = 15 * 60

SOURCE_EXTS = {".java", ".kt", ".scala", ".groovy", ".ts", ".tsx", ".js", ".jsx", ".mjs",
               ".py", ".go", ".cs", ".rb", ".php", ".rs", ".swift", ".sql", ".html"}
_TEST_PATH = re.compile(r"(^|/)(src/test|test|tests|__tests__|e2e|spec|testdata)/|"
                        r"(^|/)[\w.]+-(test|tests|e2e)/|\.glue\.[jt]s$|"
                        r"\.(spec|test|e2e)\.[a-z]+$|_test\.[a-z]+$|Tests?\.(java|kt)$|"
                        r"\.feature$", re.I)


def _sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _model_step():
    """`rerun-model.py`, for its plumbing: `mapping_argv`, `_ask`, `record_run`, `parse_answer`."""
    return _sibling("hr_rerun_model_for_mutagent", "rerun-model.py")


def say(msg: str, err: bool = False) -> None:
    print(f"[mutagent] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


# ------------------------------------------------------------------------------- git

def git(root: Path, *args: str, env: dict | None = None, check: bool = False,
        input: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env=env, input=input)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def head_sha(root: Path) -> str:
    return git(root, "rev-parse", "HEAD", check=True).stdout.strip()


def review_base(root: Path) -> str:
    """The review's base: the merge-base of HEAD with `human-review.json`'s `base`
    (`origin/main` when it names none) — the same answer `run-steps.py` gives."""
    named = "origin/main"
    try:
        named = json.loads((root / CONFIG).read_text(encoding="utf-8")).get("base") or named
    except (OSError, ValueError, AttributeError):
        pass
    got = git(root, "merge-base", named, "HEAD").stdout.strip()
    return got or named


def at_head(root: Path, rel: str) -> str | None:
    proc = git(root, "show", f"HEAD:{rel}")
    return proc.stdout if proc.returncode == 0 else None


def is_test_path(rel: str) -> bool:
    return bool(_TEST_PATH.search(rel))


def changed_production_files(root: Path, base: str) -> list[str]:
    """Source files the PR added or modified, tests and generated output left out."""
    out = git(root, "diff", "--name-only", "--diff-filter=AM", f"{base}..HEAD").stdout
    keep = []
    for rel in out.splitlines():
        rel = rel.strip()
        if (not rel or rel.startswith(".human-review/") or "/generated/" in rel
                or Path(rel).suffix.lower() not in SOURCE_EXTS or is_test_path(rel)
                or "/db/migration/" in rel or "/db/seed/" in rel):
            continue
        keep.append(rel)
    return keep


# ------------------------------------------------------------------------- the claims

def load_claims(review: Path) -> list[dict]:
    """The sentences some test claims to prove — the covered and partly covered ones of the
    pairing (`test-mapping.json`, as the build re-anchored it), each with its claimed tests."""
    # The build's merged copy first: it carries each test id to the line the test is on in
    # the tree as it is now, where the model's file still names the lines it was shown —
    # after a merge from the base those point at other tests.
    for name in ("assets/test-mapping.merged.json", "test-mapping.json"):
        try:
            doc = json.loads((review / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out = []
        for s in doc.get("sentences") or []:
            tests = [t for t in s.get("tests") or [] if isinstance(t, dict) and t.get("id")]
            if s.get("id") and tests and s.get("coverage") in ("covered", "partial"):
                out.append({"id": s["id"], "coverage": s.get("coverage"), "tests": tests})
        return out
    return []


#: What the Tests tab measured: which test ran which changed line (`testcov`), and what the
#: PR did to each test (`tests`). Read to find the tests nobody claims for a sentence.
COVERAGE = "assets/test-coverage.json"
TEST_CHANGES = "assets/test-changes.json"
#: How many unclaimed tests go in the prompt, the ones running the most changed lines first.
UNCLAIMED_MAX = 15


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _say_name(title: str) -> str:
    """A test's name read as the sentence it promises: `getAll_returnsVisitsInDateOrder`
    → "get all returns visits in date order"."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ",
                   re.sub(r"[_\-]+", " ", title or ""))
    return " ".join(words.split()).lower()


def load_unclaimed(review: Path, root: Path, claimed: set[tuple[str, str]], cfg: dict,
                   limit: int = UNCLAIMED_MAX) -> list[dict]:
    """Pre-existing tests — ones this PR did not write or edit — that run lines the PR
    changed and that no ticket sentence claims. Each becomes a claim of its own, its name
    being what it promises; a mutant on the changed lines it runs asks whether it keeps it.

    `claimed` is `{(file, test name)}` of the tests the mapping already pairs."""
    cov = _json(review / COVERAGE) or {}
    changed = cov.get("changed") or {}
    moved = {}
    for t in (_json(review / TEST_CHANGES) or {}).get("tests") or []:
        if isinstance(t, dict):
            moved[(t.get("path"), t.get("name"))] = t.get("status")
    picks = []
    for t in cov.get("tests") or []:
        if not isinstance(t, dict) or t.get("status") not in (None, "passed"):
            continue
        rel, title, line = t.get("file") or "", t.get("title") or "", t.get("line")
        hits = {f: sorted(set(ls) & set(changed[f])) for f, ls in (t.get("hits") or {}).items()
                if f in changed and set(ls) & set(changed[f])}
        if not rel or not line or not hits or (rel, title) in claimed:
            continue
        if moved.get((rel, title), "unchanged") != "unchanged":
            continue
        tid = f"{rel}:{line}"
        c = classify(tid, root, cfg)
        if not c["runnable"] or (rel, c.get("name")) in claimed:
            continue
        picks.append((sum(len(ls) for ls in hits.values()), tid, title, hits, c))
    picks.sort(key=lambda p: (-p[0], p[1]))
    out, seen = [], set()
    for _, tid, title, hits, c in picks:
        if tid in seen:
            continue
        seen.add(tid)
        ran = "; ".join(f"{Path(f).name}:{','.join(map(str, ls))}" for f, ls in hits.items())
        out.append({"id": "t:" + tid, "kind": "test", "coverage": "exercised",
                    "text": _say_name(c.get("name") or title), "changed": hits,
                    "tests": [{"id": tid, "strength": "name",
                               "why": f"claims no sentence; runs lines this PR changed: {ran}"}]})
        if len(out) >= limit:
            break
    return out


def sentence_texts(review: Path, root: Path) -> dict[str, str]:
    """`{sentence id: text}` the way the Tests tab reads the ticket (`semcov.gather`).
    Empty when the ticket cannot be read — the ids still work, the card says less."""
    try:
        sc = _sibling("hr_semcov_for_mutagent", "semcov.py")
        spec = json.loads((review / LOOKS_LIKE_A_REVIEW).read_text(encoding="utf-8"))
        g = sc.gather(spec if isinstance(spec, dict) else {}, review, root)
        return {s["id"]: s.get("text") or "" for s in (g or {}).get("sentences") or []}
    except Exception as e:                     # noqa: BLE001 - the text is a nicety
        say(f"could not read the ticket's sentences ({e}); the card shows ids only", err=True)
        return {}


# ----------------------------------------------------------------------- the runners

def split_test_id(tid: str) -> tuple[str, int | None]:
    path, _, line = tid.rpartition(":")
    if path and line.isdigit():
        return path, int(line)
    return tid, None


_STRIP_STR = re.compile(r'"(?:\\.|[^"\\])*"|'r"'(?:\\.|[^'\\])*'")
_CLASS_DECL = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")
#: A method declared on this line: a type that starts with a letter, a name, a paren, and
#: no \`;\` after it — \`assertThat(x).isEqualTo(y);\` and \`return foo(x);\` are statements.
_JAVA_METHOD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|"
                          r"static|final|synchronized|abstract|default)\s+)*(?:<[^>]+>\s+)?"
                          r"[A-Za-z_][\w.\[\]?]*(?:<[^;()]*>)?(?:\[\])*\s+([a-z_]\w*)\s*\([^;]*$")


def java_names(text: str, line: int, rel: str) -> tuple[str, str]:
    """`(class, method)` of the test at `line` (1-based, often an assertion inside it):
    the FQCN, nested classes joined by `$` as surefire names them, and the nearest method
    declared at or above the line."""
    lines = text.split("\n")
    pkg = ""
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    if m:
        pkg = m.group(1)
    stack: list[tuple[str, int]] = []     # (class name, depth its body opened at)
    pending: str | None = None
    depth = 0
    method = ""
    for i, raw in enumerate(lines[:max(line, 1)], start=1):
        code = _STRIP_STR.sub('""', raw.split("//", 1)[0])
        c = _CLASS_DECL.search(code)
        if c and not code.lstrip().startswith(("*", "/*")):
            pending = c.group(1)
        mm = _JAVA_METHOD.match(code)
        if mm and mm.group(1) not in ("if", "for", "while", "switch", "catch", "return", "new"):
            method = mm.group(1)
        for ch in code:
            if ch == "{":
                depth += 1
                if pending:
                    stack.append((pending, depth))
                    pending = None
            elif ch == "}":
                while stack and stack[-1][1] >= depth:
                    stack.pop()
                depth -= 1
    names = [n for n, _ in stack] or [Path(rel).stem]
    cls = "$".join(names)
    return (f"{pkg}.{cls}" if pkg else cls), method


def scenario_name(text: str, line: int, rel: str) -> str:
    """The title of the test at `line`: a Gherkin `Scenario:`, or an `it(`/`test(` title."""
    lines = text.split("\n")
    pat = (re.compile(r"^\s*Scenario(?: Outline| Template)?:\s*(.+?)\s*$")
           if rel.endswith(".feature") else
           re.compile(r"""\b(?:it|test)(?:\.only)?\s*\(\s*(['"`])(.+?)\1"""))
    for raw in reversed(lines[:max(line, 1)]):
        m = pat.search(raw)
        if m:
            return m.group(m.lastindex).strip()
    return ""


BODY_MAX = 45


def test_body(text: str, line: int, rel: str) -> str:
    """The test's own source around `line`: from its declaration (a JVM method, a Gherkin
    scenario, an `it(`) to where it closes, at most BODY_MAX lines — what the model reads
    to aim a mutant at what the assertion does *not* check."""
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return ""
    start = line
    if rel.endswith((".java", ".kt")):
        for i in range(line, 0, -1):
            m = _JAVA_METHOD.match(lines[i - 1])
            if m and m.group(1) not in ("if", "for", "while", "switch", "catch", "return", "new"):
                start = i
                break
        while start > 1 and lines[start - 2].strip().startswith("@"):
            start -= 1
        depth, opened, end = 0, False, min(len(lines), start + BODY_MAX - 1)
        for i in range(start, end + 1):
            code = _STRIP_STR.sub('""', lines[i - 1].split("//", 1)[0])
            depth += code.count("{") - code.count("}")
            opened = opened or "{" in code
            if opened and depth <= 0:
                end = i
                break
    else:
        pat = (re.compile(r"^\s*Scenario") if rel.endswith(".feature")
               else re.compile(r"\b(?:it|test)(?:\.only)?\s*\("))
        for i in range(line, 0, -1):
            if pat.search(lines[i - 1]):
                start = i
                break
        end = min(len(lines), max(line, start) + 12, start + BODY_MAX - 1)
        if rel.endswith(".feature"):
            nxt = next((i for i in range(start + 1, end + 1)
                        if re.match(r"\s*(Scenario|Rule|Feature|Examples|@)", lines[i - 1])), None)
            end = (nxt - 1) if nxt else end
    return "\n".join(lines[start - 1:end])


def nearest_build(root: Path, rel: str) -> tuple[str, str] | None:
    """`(kind, dir)` of the nearest Maven or Gradle build above a file, repo-relative."""
    d = (root / rel).parent
    while True:
        for name, kind in (("pom.xml", "maven"), ("build.gradle", "gradle"),
                           ("build.gradle.kts", "gradle")):
            if (d / name).is_file():
                return kind, str(d.relative_to(root)) if d != root else "."
        if d == root or root not in d.parents:
            return None
        d = d.parent


BUILTIN = {
    "maven": {"item": "{class}#{name}", "join": ",",
              "run": "mvn -o -q test -Dtest={tests} -Dsurefire.failIfNoSpecifiedTests=false "
                     "-DfailIfNoTests=false",
              "reports": "target/surefire-reports"},
    "gradle": {"item": "--tests {class}.{name}", "join": " ", "quote": False,
               "run": "{gradlew} test --offline {tests}", "reports": "build/test-results/test"},
}


def load_config(root: Path, override: str | None = None) -> dict:
    """`{"suites": {...}, "defaults": bool}`: `--config`, then
    `$HUMAN_REVIEW_MUTAGENT_CONFIG`, then `steps.mutagent` in `human-review.json`."""
    path = override or os.environ.get("HUMAN_REVIEW_MUTAGENT_CONFIG")
    try:
        if path:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            doc = (json.loads((root / CONFIG).read_text(encoding="utf-8")).get("steps") or {}
                   ).get("mutagent") or {}
    except (OSError, ValueError, AttributeError):
        doc = {}
    return doc if isinstance(doc, dict) else {}


def _glob(rel: str, pats) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.replace("**/", ""))
               for p in pats or [])


def classify(tid: str, root: Path, cfg: dict) -> dict:
    """How one claimed test would be run: `{id, file, line, name, label, runnable, why,
    suite, cwd, item}` — `runnable` False with `why` when nothing here can run it."""
    rel, line = split_test_id(tid)
    out = {"id": tid, "file": rel, "line": line, "runnable": False, "why": "",
           "label": Path(rel).name + (f":{line}" if line else "")}
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = at_head(root, rel)
    if text is None:
        out["why"] = "the test file is not in the tree"
        return out
    cls = name = ""
    if rel.endswith(".java") or rel.endswith(".kt"):
        cls, name = java_names(text, line or 1, rel)
        out["label"] = f"{cls.rsplit('.', 1)[-1]}#{name}" if name else out["label"]
    else:
        name = scenario_name(text, line or 1, rel)
        cls = Path(rel).stem
        if name:
            out["label"] = f"{Path(rel).name}: {name}"
    out.update({"class": cls, "name": name, "body": test_body(text, line or 1, rel)})
    for sname, suite in (cfg.get("suites") or {}).items():
        if not isinstance(suite, dict) or not _glob(rel, suite.get("files")) \
                or _glob(rel, suite.get("exclude")):
            continue
        cwd = suite.get("cwd") or "."
        if "{module}" in cwd:
            nb = nearest_build(root, rel)
            cwd = cwd.replace("{module}", nb[1] if nb else ".")
        out.update(runnable=True, suite=sname, cwd=cwd, how=suite)
        return out
    if cfg.get("defaults", True) and "/src/test/java/" in f"/{rel}" and name:
        nb = nearest_build(root, rel)
        if nb:
            out.update(runnable=True, suite=nb[0], cwd=nb[1], how=BUILTIN[nb[0]])
            return out
        out["why"] = "a JVM test with no pom.xml or build.gradle above it"
        return out
    if rel.endswith(".feature"):
        out["why"] = ("a Cucumber scenario: no runner for it here (add a suite under "
                      "steps.mutagent in human-review.json)")
    elif re.search(r"\.(spec|test|e2e)\.[jt]sx?$", rel):
        out["why"] = ("a browser or Karma spec: it needs the app or a browser up (add a "
                      "suite under steps.mutagent in human-review.json)")
    elif not name:
        out["why"] = "no test found at that line"
    else:
        out["why"] = "no runner knows this kind of test"
    return out


def _gradlew(wt: Path, cwd: Path) -> str:
    d = cwd
    while True:
        if (d / "gradlew").is_file():
            return shlex.quote(str(d / "gradlew"))
        if d == wt or wt not in d.parents:
            return "gradle"
        d = d.parent


def suite_command(tests: list[dict], how: dict, wt: Path, cwd: Path) -> str:
    items = []
    for t in tests:
        item = how.get("item") or "{class}#{name}"
        item = (item.replace("{class}", t.get("class") or "").replace("{name}", t.get("name") or "")
                .replace("{file}", t["file"]).replace("{line}", str(t.get("line") or "")))
        if item not in items:
            items.append(item)
    joined = (how.get("join") if how.get("join") is not None else ",").join(items)
    spelt = shlex.quote(joined) if how.get("quote", True) else joined
    return (how.get("run") or "").replace("{tests}", spelt).replace("{gradlew}",
                                                                   _gradlew(wt, cwd))


def parse_junit(reports: Path) -> list[tuple[str, str, str, str]]:
    """`[(classname, name, pass|fail|skipped, failure message)]` out of every JUnit XML
    under a directory."""
    out = []
    for f in sorted(reports.rglob("*.xml")) if reports.is_dir() else []:
        try:
            tree = ET.parse(f)
        except (ET.ParseError, OSError):
            continue
        for tc in tree.iter("testcase"):
            kids = {c.tag for c in tc}
            state = ("fail" if kids & {"failure", "error"} else
                     "skipped" if "skipped" in kids else "pass")
            bad = next((c for c in tc if c.tag in ("failure", "error")), None)
            said = ""
            if bad is not None:
                said = (bad.get("message") or (bad.text or "").strip() or bad.get("type") or "")
                said = " ".join(said.split())[:240]
            out.append((tc.get("classname") or "", tc.get("name") or "", state, said))
    return out


def _bare(name: str) -> str:
    return re.split(r"[(\[]", name, maxsplit=1)[0].strip()


def match_case(test: dict, cases) -> str:
    """One expected test's outcome among the reported cases: fail, pass, or missing."""
    return match_detail(test, cases)[0]


def match_detail(test: dict, cases) -> tuple[str, str]:
    """`(fail|pass|missing, the first failure's message)` for one expected test."""
    cls, name = test.get("class") or "", test.get("name") or ""
    hits, said = [], ""
    for c_cls, c_name, state, msg in cases:
        before = len(hits)
        if test["file"].endswith((".java", ".kt")):
            same_cls = c_cls == cls or c_cls.endswith("." + cls.rsplit(".", 1)[-1]) \
                or c_cls.replace("$", ".") == cls.replace("$", ".")
            if same_cls and (_bare(c_name) == name or c_name.startswith(name + "(")):
                hits.append(state)
        elif name and (name in c_name or c_name in name):
            hits.append(state)
        if len(hits) > before and state == "fail" and not said:
            said = msg
    if not hits:
        return "missing", ""
    if "fail" in hits:
        return "fail", said
    return ("pass" if "pass" in hits else "missing"), ""


def run_tests(wt: Path, tests: list[dict], timeout: int = RUN_TIMEOUT) -> dict:
    """Run the given classified tests in a worktree, one command per suite.

    `{"results": {test id: pass|fail|missing}, "ok": bool, "tail": str}` — `ok` False when
    a suite produced no report at all and exited non-zero (it did not compile)."""
    by_suite: dict[tuple, list[dict]] = {}
    for t in tests:
        by_suite.setdefault((t["suite"], t["cwd"]), []).append(t)
    results, said, ok, tails = {}, {}, True, []
    for (suite, cwd), group in by_suite.items():
        how = group[0]["how"]
        where = (wt / cwd).resolve()
        reports = where / (how.get("reports") or "target/surefire-reports")
        shutil.rmtree(reports, ignore_errors=True)
        cmd = suite_command(group, how, wt, where)
        say(f"  $ (cd {cwd} && {cmd})")
        try:
            proc = subprocess.run(["/bin/sh", "-c", cmd], cwd=str(where), capture_output=True,
                                  text=True, timeout=timeout)
            code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            code, output = -1, f"timed out after {timeout}s\n" + str(e.stdout or "")
        cases = parse_junit(reports)
        for t in group:
            results[t["id"]], msg = match_detail(t, cases)
            if msg:
                said[t["id"]] = msg
        if code != 0 and not cases:
            ok = False
            tails.append(_tail(output))
    return {"results": results, "messages": said, "ok": ok, "tail": "\n".join(tails)}


def _tail(output: str, n: int = 12) -> str:
    keep = [ln for ln in output.splitlines() if ln.strip()]
    errs = [ln for ln in keep if re.search(r"ERROR|error:|FAIL|Exception", ln)]
    return "\n".join((errs or keep)[-n:])


def verdict(expect: list[str], tests: dict, baseline: dict, outcome: dict | None,
            applied: bool = True) -> dict:
    """The verdict of one mutant: `{"verdict", "why", "tests": {id: state}}`.

    `tests` is each expected id's classification, `baseline` the unmutated results,
    `outcome` what `run_tests` said with the mutant applied (None when nothing ran)."""
    if not applied:
        return {"verdict": "broken", "why": "the patch no longer applies to HEAD", "tests": {}}
    runnable = [t for t in expect if (tests.get(t) or {}).get("runnable")]
    if not runnable:
        reasons = sorted({(tests.get(t) or {}).get("why") or "no runner" for t in expect})
        return {"verdict": "not-run", "why": "; ".join(reasons), "tests": {}}
    green = [t for t in runnable if baseline.get(t) == "pass"]
    if not green:
        return {"verdict": "not-run", "tests": {t: baseline.get(t, "missing") for t in runnable},
                "why": "its tests do not pass even without the mutant"}
    if outcome is None:
        return {"verdict": "not-run", "why": "nothing ran", "tests": {}}
    states = {t: outcome["results"].get(t, "missing") for t in green}
    if not outcome.get("ok") and all(s == "missing" for s in states.values()):
        return {"verdict": "broken", "tests": states,
                "why": "the mutated code does not compile or the run crashed"
                       + (": " + outcome["tail"].splitlines()[-1] if outcome.get("tail") else "")}
    failed = [t for t, s in states.items() if s == "fail"]
    if failed:
        said = {t: m for t, m in (outcome.get("messages") or {}).items() if t in failed}
        return {"verdict": "killed", "tests": states, **({"messages": said} if said else {}),
                "why": f"{len(failed)} of {len(states)} expected test(s) failed"}
    passed = [t for t, s in states.items() if s == "pass"]
    if passed:
        return {"verdict": "survived", "tests": states,
                "why": f"all {len(passed)} expected test(s) stayed green with the mutant"}
    return {"verdict": "not-run", "tests": states,
            "why": "the runner reported none of the expected tests"}


# --------------------------------------------------------------------------- generate

def build_input(claims: list[dict], texts: dict, files: list[dict], tests: dict,
                max_n: int) -> dict:
    sentences = []
    for s in claims:
        sentences.append({"id": s["id"], "text": texts.get(s["id"], s.get("text", "")),
                          "coverage": s.get("coverage"),
                          **({"kind": "test", "changedLines": s["changed"]}
                             if s.get("kind") == "test" else {}),
                          "tests": [{"id": t["id"], "why": t.get("why", ""),
                                     "line": t.get("line", ""),
                                     "runnable": bool((tests.get(t["id"]) or {}).get("runnable")),
                                     "body": (tests.get(t["id"]) or {}).get("body", "")}
                                    for t in s["tests"]]})
    return {"max": max_n, "sentences": sentences, "files": files}


def prompt_files(root: Path, base: str, paths: list[str]) -> list[dict]:
    """Each changed production file: its diff, and its whole HEAD content while the budget
    lasts (the model has to quote it exactly)."""
    out, used = [], 0
    for rel in paths:
        diff = git(root, "diff", "-U3", "--no-color", f"{base}..HEAD", "--", rel).stdout
        body = at_head(root, rel) or ""
        entry = {"path": rel, "diff": diff}
        if len(body) <= FILE_BUDGET and used + len(body) + len(diff) <= TOTAL_BUDGET:
            entry["content"] = body
            used += len(body)
        used += len(diff)
        out.append(entry)
    return out


def build_prompt(asked: dict) -> str:
    return (PROMPT.read_text(encoding="utf-8").rstrip() + "\n\n## Input\n\n```json\n"
            + json.dumps(asked, indent=1, ensure_ascii=False) + "\n```\n")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "mutant").lower()).strip("-")
    return (s[:40].rstrip("-")) or "mutant"


def make_patch(rel: str, before: str, after: str) -> str:
    """A `git apply`-able unified diff of one file."""
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    out = [f"diff --git a/{rel} b/{rel}\n"]
    for i, line in enumerate(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3)):
        if i < 2:
            out.append(line if line.endswith("\n") else line + "\n")
            continue
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def apply_check(root: Path, patch: str) -> str | None:
    """Why `patch` does not apply to HEAD, or None when it does — checked against a
    throwaway index of HEAD, so neither the working tree nor the real index is read."""
    with tempfile.TemporaryDirectory(prefix="mutagent-idx-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git(root, "read-tree", "HEAD", env=env, check=True)
        proc = git(root, "apply", "--check", "--cached", "-", env=env, input=patch)
        return None if proc.returncode == 0 else (proc.stderr.strip() or "does not apply")


def validate(doc, claims: list[dict], files: list[str], root: Path,
             max_n: int) -> tuple[list[dict], list[dict]]:
    """`(kept, dropped)` — each kept mutant with its `patch` text; each dropped one with
    the reason, so the log says what the model got wrong."""
    known_s = {s["id"] for s in claims}
    known_t = {t["id"] for s in claims for t in s["tests"]}
    kept, dropped, seen = [], [], set()
    for m in (doc or {}).get("mutants") or []:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip() or "untitled mutant"

        def drop(why):
            dropped.append({"title": title, "why": why})
        rel = str(m.get("file") or "").strip()
        find, repl = m.get("find"), m.get("replace")
        expect = [t for t in m.get("expect") or [] if t in known_t]
        if m.get("sentence") not in known_s:
            drop(f"attacks no claimed sentence ({m.get('sentence')!r})")
            continue
        if rel not in files:
            drop(f"{rel or 'no file'} is not production code this PR changed")
            continue
        if not isinstance(find, str) or not isinstance(repl, str) or not find or find == repl:
            drop("no change: find and replace are missing or equal")
            continue
        if not expect:
            drop("expects no test the mapping knows")
            continue
        before = at_head(root, rel)
        if before is None:
            drop(f"{rel} is not in HEAD")
            continue
        n = before.count(find)
        if n != 1:
            drop(f"its find text occurs {n} times in {rel}, not once")
            continue
        key = (rel, find, repl)
        if key in seen:
            drop("a duplicate of another mutant")
            continue
        patch = make_patch(rel, before, before.replace(find, repl, 1))
        why = apply_check(root, patch)
        if why:
            drop(f"git apply --check refused it: {why}")
            continue
        if len(kept) >= max_n:
            drop(f"over the cap of {max_n}")
            continue
        seen.add(key)
        kept.append({"title": title, "sentence": m["sentence"], "file": rel,
                     "rationale": str(m.get("rationale") or "").strip(), "expect": expect,
                     "patch_text": patch})
    return kept, dropped


def write_mutants(review: Path, kept: list[dict], dropped: list[dict], texts: dict,
                  tests: dict, meta: dict) -> Path:
    """`<review>/mutagent/`: mutants.json and one patch per mutant; the previous set and
    its results go, since they answered other mutants."""
    out = review / SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("*.patch")) + [out / RESULTS]:
        old.unlink(missing_ok=True)
    mutants = []
    for i, m in enumerate(kept, start=1):
        mid = f"{i:02d}-{slug(m['title'])}"
        (out / f"{mid}.patch").write_text(m["patch_text"], encoding="utf-8")
        mutants.append({"id": mid, "title": m["title"],
                        "sentence": {"id": m["sentence"], "text": texts.get(m["sentence"], ""),
                                     **({"kind": "test"} if m["sentence"].startswith("t:")
                                        else {})},
                        "file": m["file"], "rationale": m["rationale"], "expect": m["expect"],
                        "patch": f"{mid}.patch"})
    used = {t for m in mutants for t in m["expect"]}
    doc = {"schema": SCHEMA, **meta, "mutants": mutants, "dropped": dropped,
           "tests": {t: {k: v for k, v in tests[t].items() if k in ("label", "runnable", "why",
                                                                   "suite", "file", "line")}
                     for t in sorted(used) if t in tests}}
    (out / MUTANTS).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    return out / MUTANTS


def _is_review(review: Path) -> bool:
    return review.is_dir() and (review / LOOKS_LIKE_A_REVIEW).is_file()


def cmd_generate(args) -> int:
    review, root = Path(args.dir), Path.cwd().resolve()
    if not _is_review(review):
        say(f"{review}/ is not a review directory (no {LOOKS_LIKE_A_REVIEW}); run it from "
            "the repository root.", err=True)
        return 2
    claims = load_claims(review)
    if not claims and not (review / COVERAGE).is_file():
        say("no sentence of the ticket is claimed by a test (test-mapping.json) and no "
            "coverage of the change was measured: nothing to attack.", err=True)
        return 2
    base = review_base(root)
    files = changed_production_files(root, base)
    if not files:
        say(f"this PR changed no production source since {base[:12]}: nothing to mutate.",
            err=True)
        return 2
    cfg = load_config(root, args.config)
    tests = {t["id"]: classify(t["id"], root, cfg) for s in claims for t in s["tests"]}
    # And the tests nobody claims: pre-existing, running lines this PR changed. Their own
    # name is the claim a mutant attacks.
    unclaimed = load_unclaimed(review, root, {(c["file"], c.get("name")) for c in tests.values()},
                               cfg)
    for s in unclaimed:
        for t in s["tests"]:
            tests[t["id"]] = classify(t["id"], root, cfg)
    claims = claims + unclaimed
    if not claims:
        say("no sentence is claimed by a test and no pre-existing test runs a changed line: "
            "nothing to attack.", err=True)
        return 2
    texts = {**{s["id"]: s["text"] for s in unclaimed}, **sentence_texts(review, root)}
    # The files beside a runnable test first: they are the ones whose whole text fits the
    # budget, and a mutant there is one this machine can actually judge.
    homes = {t["cwd"] for t in tests.values() if t["runnable"]}
    files.sort(key=lambda f: not any(h == "." or f.startswith(h.rstrip("/") + "/") for h in homes))
    asked = build_input(claims, texts, prompt_files(root, base, files), tests, args.max)
    prompt = build_prompt(asked)
    runnable = sum(1 for t in tests.values() if t["runnable"])
    say(f"{len(claims) - len(unclaimed)} claimed sentence(s) and {len(unclaimed)} unclaimed "
        f"pre-existing test(s), {len(tests)} test(s) ({runnable} "
        f"runnable here), {len(files)} changed production file(s); asking for up to "
        f"{args.max} mutants ({len(prompt)} chars).")
    if args.prompt_only:
        print(prompt)
        return 0
    ms = _model_step()
    meta = {"head": head_sha(root), "base": base,
            "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    if args.answer:
        doc = ms.parse_answer(Path(args.answer).read_text(encoding="utf-8"))
        meta.update(model="(answer file)", cost=None)
    else:
        argvec = ms.mapping_argv(args.model)
        say("$ " + " ".join(a if a else '""' for a in argvec) + f"  < {PROMPT.name} + input")
        if args.dry_run:
            say(f"dry run: nothing was asked of {args.model} and nothing was paid for.")
            return 0
        if not shutil.which("claude"):
            say("no `claude` on PATH: answer the prompt elsewhere (--prompt-only), then "
                "install it with --answer.", err=True)
            return 2
        proc, cost, said, seconds = ms._ask(argvec, prompt, root)
        ms.record_run(review, cost, seconds, ledger=RUNS_LEDGER, model=args.model,
                      out=proc.stdout)
        say(f"{args.model} answered in {seconds:.0f}s"
            + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""))
        if proc.returncode != 0:
            say(f"{args.model} exited {proc.returncode}; nothing written.", err=True)
            return 1
        doc = ms.parse_answer(said)
        meta.update(model=args.model, cost=cost)
    if doc is None:
        say("the answer holds no JSON document; nothing written.", err=True)
        return 1
    kept, dropped = validate(doc, claims, files, root, args.max)
    for d in dropped:
        say(f"dropped \"{d['title']}\": {d['why']}", err=True)
    if not kept:
        say("no mutant survived validation; the previous set is left as it was.", err=True)
        return 1
    path = write_mutants(review, kept, dropped, texts, tests, meta)
    say(f"{len(kept)} mutant(s) written to {path} ({len(dropped)} dropped). "
        "Next: mutagent.py run.")
    return 0


# -------------------------------------------------------------------------------- run

class Worktree:
    """A detached worktree of HEAD in a temp dir, removed on exit. The reviewed checkout
    is never written to: its uncommitted work stays exactly as it was."""

    def __init__(self, root: Path):
        self.root = root
        self.tmp = Path(tempfile.mkdtemp(prefix="mutagent-"))
        self.path = self.tmp / "wt"

    def __enter__(self) -> Path:
        git(self.root, "worktree", "add", "--detach", "-q", str(self.path), "HEAD", check=True)
        return self.path

    def reset(self) -> None:
        git(self.path, "reset", "--hard", "-q", "HEAD", check=True)
        git(self.path, "clean", "-fdq")

    def __exit__(self, *exc) -> None:
        git(self.root, "worktree", "remove", "--force", str(self.path))
        git(self.root, "worktree", "prune")
        shutil.rmtree(self.tmp, ignore_errors=True)


def run_all(review: Path, root: Path, cfg: dict, only: set[str] | None = None,
            runner=run_tests) -> dict:
    doc = json.loads((review / SUBDIR / MUTANTS).read_text(encoding="utf-8"))
    mutants = [m for m in doc.get("mutants") or [] if not only or m["id"] in only]
    ids = sorted({t for m in mutants for t in m["expect"]})
    tests = {t: classify(t, root, cfg) for t in ids}
    out = {"schema": RESULTS_SCHEMA, "head": head_sha(root), "mutantsHead": doc.get("head"),
           "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "baseline": {}, "mutants": {}}
    path = review / SUBDIR / RESULTS

    def save():
        path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    runnable = [tests[t] for t in ids if tests[t]["runnable"]]
    wt = Worktree(root)
    with wt as tree:
        if runnable:
            say(f"baseline: {len(runnable)} expected test(s) on HEAD, unmutated")
            base = runner(tree, runnable)
            out["baseline"] = base["results"]
            if not base["ok"]:
                out["baselineProblem"] = base["tail"]
                say("the unmutated run did not complete:\n" + base["tail"], err=True)
            save()
        for i, m in enumerate(mutants, start=1):
            started = time.time()
            patch = (review / SUBDIR / m["patch"]).read_text(encoding="utf-8")
            applied = git(tree, "apply", "-", input=patch).returncode == 0
            mine = [tests[t] for t in m["expect"]
                    if tests[t]["runnable"] and out["baseline"].get(t) == "pass"]
            outcome = runner(tree, mine) if applied and mine else None
            v = verdict(m["expect"], tests, out["baseline"], outcome, applied)
            v["seconds"] = round(time.time() - started, 1)
            out["mutants"][m["id"]] = v
            say(f"{i}/{len(mutants)} {m['id']}: {v['verdict']} — {v['why']}")
            wt.reset()
            save()
    counts: dict[str, int] = {}
    for v in out["mutants"].values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    say("done: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    return out


def cmd_run(args) -> int:
    review, root = Path(args.dir), Path.cwd().resolve()
    if not (review / SUBDIR / MUTANTS).is_file():
        say(f"no {review}/{SUBDIR}/{MUTANTS}: generate the mutants first.", err=True)
        return 2
    only = {x for x in (args.only or "").split(",") if x} or None
    run_all(review, root, load_config(root, args.config), only)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="ask a model for targeted mutants (costs money)")
    g.add_argument("--dir", default=".human-review")
    g.add_argument("--model", default=os.environ.get("HUMAN_REVIEW_MUTAGENT_MODEL") or "sonnet")
    g.add_argument("--max", type=int, default=DEFAULT_MAX)
    g.add_argument("--config", help="a runner config instead of steps.mutagent")
    g.add_argument("--dry-run", action="store_true", help="print the command, spend nothing")
    g.add_argument("--prompt-only", action="store_true", help="print the prompt, spend nothing")
    g.add_argument("--answer", help="install an answer made elsewhere instead of asking")
    r = sub.add_parser("run", help="apply each mutant in a throwaway worktree, run its tests")
    r.add_argument("--dir", default=".human-review")
    r.add_argument("--config", help="a runner config instead of steps.mutagent")
    r.add_argument("--only", help="comma-separated mutant ids")
    args = ap.parse_args(argv)
    return cmd_generate(args) if args.cmd == "generate" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
