#!/usr/bin/env python3
"""Which tests execute which changed lines — measured, per test, by coverage.

The Tests tab used to answer "what tests cover this change?" with a model's reading of the
ticket: a list of tests paired with its sentences, sixteen long on the PR it was built for.
Fifty-seven tests on that same PR actually execute a changed line. The pairing says which
tests were *meant* for the change; only a run can say which tests *reach* it, and those are
different questions with different answers. This is the program that asks the second.

It runs each suite once with a per-test coverage hook — nothing in the project changes:

  * **pytest** (`steps.testcov.pytest`): the service's own suite, in its own environment,
    under coverage.py with per-test contexts (`pytest_suites`);
  * **Karma** (`steps.testcov.karma`): a Karma config this skill ships loads the project's
    own and adds a framework + reporter that snapshot Istanbul's counters around every spec;
  * **browser suites** (`steps.testcov.e2e`): not run from here. They already ran, traced,
    for the Tests tab's recordings (`city.tests`, `traces.commands`); run with COVERAGE_DIR
    set, the project's fixtures leave per-test Chromium coverage. This harvests what they
    left, and refuses what a
    run of another commit left.

Writes `.human-review/assets/test-coverage.json`:

  changed       {file: [lines]}   changed lines of production code worth a reader's eye
                                  (blank lines, comments, braces and imports dropped)
  executable    {file: [lines]}   of those files, every line some probe can see run
  executableAll {file: [lines]}   the same, for EVERY production file in `sources` — the
                                  denominator of a whole-project line coverage, which is
                                  what the Code City colours by (`city-coverage.py`)
  unmeasurable  [{file, lines, reason, proxy?}]   changed lines no probe can see: an
                                  annotation, a query string, a migration. `proxy` names
                                  the lines that DO run when it matters — the annotated
                                  method's body, a repository method's call sites
  suites        [{name, source, status, tests, seconds, note}]
  tests         [{id, suite, title, file, line, status, source, hits: {file: [lines]}}]

`hits` is everything each test ran in the project's sources, not only the changed lines:
the join with the diff is redone by the page at every build, so a rebuild is free.

Usage:
  testcov.py --base origin/main [--config human-review.json]
             [--out .human-review/assets/test-coverage.json] [--only pytest,karma,e2e]
             [--reuse]

`--reuse` runs no suite: it re-reads what the last run left in `.human-review/coverage/`
(the JUnit dumps, `karma-N.json`, the browser suites' dumps — those are never run from
here anyway) and writes the file again. For a change to what this program *derives*, not
to what the tests do; seconds instead of minutes, and nothing in that folder is touched.

Exit 0 with the file written; 3 when `steps.testcov` is not configured; 1 on a failure that
left nothing to write.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "testcov"
WORK = Path(".human-review/coverage")
DEFAULT_OUT = Path(".human-review/assets/test-coverage.json")
CACHE = Path(os.environ.get("HR_TESTCOV_CACHE")
             or Path.home() / ".cache" / "human-review" / "testcov")


#: The open lock file of this process's measurement (`hold_measuring_lock`); released when
#: the process exits, however it exits.
_HELD = None


def hold_measuring_lock(work: Path = WORK) -> None:
    """Hold `<work>/.lock` for the rest of this process — waiting, and saying so, while
    another run measures the same checkout.

    Two runs at once on one checkout share everything this program measures with: the
    JUnit dump directory (`junit-0/`, wiped and refilled per run, files numbered from 1 in
    both), the Karma output, the module's `target/`. 20 Oct 2026 on petclinic: two
    refreshes overlapped and one coverage file listed 386 JUnit tests of 193, and the
    candidate set the Tests tab offers its model moved between runs. `run-steps.py`'s
    `traces` takes the same lock while the browser suites write their dumps here."""
    global _HELD
    work.mkdir(parents=True, exist_ok=True)
    fh = open(work / ".lock", "a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another run is measuring this checkout ({work}/.lock) — waiting for it to end")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    _HELD = fh


def log(msg: str) -> None:
    print(f"[testcov] {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------ lines

def ranges(lines) -> str:
    """`[1,2,3,7]` -> `"1-3,7"`."""
    out, run = [], []
    for n in sorted(set(lines)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(out)


def unranges(text: str) -> list[int]:
    """`"1-3,7"` -> `[1,2,3,7]`."""
    out = []
    for part in (text or "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def glob_rx(pattern: str) -> "re.Pattern[str]":
    """A `**`-aware glob: `*` stops at a slash, `**` crosses them, a leading `**/` is optional."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def in_scope(path: str, sources, exclude) -> bool:
    if sources and not any(glob_rx(g).match(path) for g in sources):
        return False
    return not any(glob_rx(g).match(path) for g in exclude or ())


#: A changed line nobody needs told whether a test ran: blank, a comment, a lone brace or
#: bracket, an import. Dropped from the diff before anything is counted, so "12 of 40
#: changed lines" is 40 lines a reader would actually look at.
TRIVIAL = re.compile(
    r"^\s*$"
    r"|^\s*(//|/\*|\*|\*/|<!--|-->|#)"
    r"|^\s*[{}()\[\];,]+\s*$"
    r"|^\s*(import|package)\b"
    r"|^\s*</[\w-]+>\s*$")


def parse_diff(text: str) -> dict[str, list[int]]:
    """`git diff -U0` -> {new path: [added or modified line numbers]}. A pure deletion
    has no line on the new side for a test to run, and is not a line of this map."""
    out: dict[str, list[int]] = {}
    path = None
    for ln in text.splitlines():
        if ln.startswith("+++ "):
            path = None if ln.strip() == "+++ /dev/null" else ln[4:].removeprefix("b/")
            continue
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", ln)
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                out.setdefault(path, []).extend(range(start, start + count))
    return out


# ------------------------------------------------------------------------ what cannot run

SIGNATURE = "signature — runs as its body runs"


def classify(path: str, src: list[str], lines: list[int], executable: set[int],
             callers=None, init: set[int] | frozenset = frozenset()) -> list[dict]:
    """The changed lines of `path` that no probe sees run, grouped by why: a Python module's
    by `classify_python`, anything else by what kind of file it is."""
    if not lines:
        return []
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return classify_python(path, src, lines, executable, init)
    if not executable:
        why = {".sql": "SQL — runs inside the database, where no probe looks",
               ".html": "template — no browser run mapped it",
               ".ts": "types — erased when compiled",
               ".properties": "configuration", ".yml": "configuration",
               ".yaml": "configuration", ".xml": "configuration",
               ".json": "configuration"}.get(ext, "no probe for this kind of file")
        return [{"file": path, "lines": lines, "reason": why}]
    why = "types — erased when compiled" if ext in (".ts", ".tsx") else \
        "declarations — no code of their own"
    return [{"file": path, "lines": lines, "reason": why}]


IMPORT_TIME = "runs when the module is imported — charged to no test"


def python_owners(src: list[str]) -> dict[int, int]:
    """`{line: first body line}` for every line of a `def`'s header — its decorators, the
    `def` and a signature spread over several lines. A changed header line runs as its body
    runs (`fold_signature`), the way a Java signature does."""
    import ast
    try:
        tree = ast.parse("\n".join(src))
    except SyntaxError:
        return {}
    out: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            # A docstring is not a line that runs; the body starts after it.
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str) and len(node.body) > 1:
                first = node.body[1]
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            for ln in range(start, first.lineno):
                out.setdefault(ln, first.lineno)
    return out


def classify_python(path: str, src: list[str], lines: list[int], executable: set[int],
                    init: set[int] | frozenset = frozenset()) -> list[dict]:
    """The changed lines of a Python module that no test is seen to run, grouped by why: a
    function's header (runs as its body runs), a line run only at import (a module-level
    assignment, a `class` line, a decorator call), or a line with no code of its own (a
    docstring, a line inside a multi-line expression)."""
    owners = python_owners(src)
    groups: dict[tuple, dict] = {}
    for ln in lines:
        proxy = None
        body = owners.get(ln)
        if body is not None:
            body = next((n for n in range(body, body + 40) if n in executable), None)
        if body is not None:
            reason, proxy = SIGNATURE, {path: [body]}
        elif ln in init:
            reason = IMPORT_TIME
        else:
            reason = "no code of its own — a docstring, or a line of a longer statement"
        key = (reason, json.dumps(proxy, sort_keys=True))
        g = groups.setdefault(key, {"file": path, "lines": [], "reason": reason,
                                    **({"proxy": proxy} if proxy else {})})
        g["lines"].append(ln)
    return list(groups.values())


# ------------------------------------------------------------------------------- tools











# ------------------------------------------------------------------------------ the repo

class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.files = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                                    cwd=root).stdout.splitlines()
        self._by_name: dict[str, list[str]] = {}
        for f in self.files:
            self._by_name.setdefault(Path(f).name, []).append(f)

    def by_suffix(self, suffix: str, prefer: str = "") -> str | None:
        """The one tracked file ending in `suffix`, the one under `prefer` when there are
        several — JaCoCo names a source `package/File.java`, which is a suffix of its path."""
        suffix = suffix.lstrip("/")
        hits = [f for f in self._by_name.get(Path(suffix).name, [])
                if f == suffix or f.endswith("/" + suffix)]
        if len(hits) > 1:
            main = [f for f in hits if "/src/main/" in f] or hits
            pref = [f for f in main if prefer and f.startswith(prefer.rstrip("/") + "/")]
            hits = pref or main
        return hits[0] if hits else None

    def text(self, path: str) -> list[str]:
        try:
            return (self.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []


def decl_line(src: list[str], name: str) -> int | None:
    """Where a test method or a spec is declared: `void name(` / `name(` for Java, the
    quoted description inside `it(` for Jasmine."""
    rx = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    for i, t in enumerate(src):
        if rx.search(t) and not t.strip().startswith(("//", "*", "@")):
            return i + 1
    return None


def js_unescaped(line: str) -> str:
    r"""A source line with its JS string escapes decoded (`\'` `\"` `\`` `\\`), so the
    title a runner reports — `the newer one's loading` — is found in the literal that wrote
    it, `'the newer one\'s loading'`. Eval run 12 lost a spec this way: no file, no line,
    no row in the matrix."""
    return re.sub(r"\\(.)", r"\1", line) if "\\" in line else line


def spec_line(src: list[str], description: str) -> int | None:
    for quote in ("'", '"', "`"):
        needle = quote + description + quote
        for i, t in enumerate(src):
            if needle in js_unescaped(t) and re.search(r"\b(f|x)?it\s*\(", t):
                return i + 1
    for i, t in enumerate(src):
        if description in js_unescaped(t):
            return i + 1
    return None


# ------------------------------------------------------------------------------- suites

def expand(cmd: str, **slots) -> str:
    for k, v in slots.items():
        cmd = cmd.replace("{" + k + "}", v)
    return cmd


def run_logged(cmd: str, cwd: Path, logfile: Path, env: dict | None = None) -> int:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    log(f"$ (cd {cwd} && {cmd})  > {logfile}")
    with logfile.open("w", encoding="utf-8") as fh:
        r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                           env={**os.environ, **(env or {})})
    return r.returncode




def karma_tests(doc: dict, repo: Repo, label: str, cwd: str, rel) -> list[dict]:
    """The rows of one Karma run (`karma-N.json`), each placed at the spec that declares it:
    the file whose source holds the description (escapes decoded — see `js_unescaped`),
    the describe's first word breaking a tie, and the `it(` line in it."""
    specs = [f for f in repo.files if f.endswith(".spec.ts")
             and f.startswith(cwd.rstrip("/") + "/" if cwd else "")]
    texts = {f: [js_unescaped(l) for l in repo.text(f)] for f in specs}
    out = []
    for t in doc.get("tests", []):
        desc, full = t.get("description", ""), t.get("id", "")
        top = full[: max(0, len(full) - len(desc))].strip().split(" ")[0]
        cands = [f for f, src in texts.items() if any(desc in l for l in src)]
        if len(cands) > 1 and top:
            cands = [f for f in cands if any(top in l for l in texts[f])] or cands
        file = cands[0] if cands else None
        hits = {r: v for p, v in t.get("hits", {}).items() if (r := rel(p))}
        out.append({"id": f"{label}:{full}", "suite": label, "title": desc or full,
                    "file": file, "line": spec_line(repo.text(file), desc) if file else None,
                    "status": t.get("status", ""), "source": "karma", "hits": hits})
    return out


def karma_suites(cfg: list[dict], repo: Repo,
                 reuse: bool = False) -> tuple[list[dict], list[dict], dict]:
    tests, suites, executable = [], [], {}
    conf = TOOLS / "karma" / "karma.conf.js"
    for i, run in enumerate(cfg):
        label = run.get("label", "Karma")
        cwd = repo.root / run.get("cwd", ".")
        out = (WORK / f"karma-{i}.json").resolve()
        t0 = time.monotonic()
        rc = 0
        if not reuse:
            out.unlink(missing_ok=True)
            rc = run_logged(expand(run["command"], karma=f"--karma-config={shlex.quote(str(conf))}"),
                            cwd, WORK / f"karma-{i}.log",
                            env={"HR_TESTCOV_KARMA_BASE": str((cwd / run.get("config", "karma.conf.js")).resolve()),
                                 "HR_TESTCOV_OUT": str(out)})
        if not out.is_file():
            suites.append({"name": label, "source": "karma", "status": "failed", "tests": 0,
                           "seconds": round(time.monotonic() - t0, 1),
                           "note": f"exit {rc} and no per-spec coverage — see {WORK}/karma-{i}.log"})
            continue
        doc = json.loads(out.read_text(encoding="utf-8"))

        def rel(p: str) -> str | None:
            try:
                return str(Path(p).resolve().relative_to(repo.root.resolve()))
            except ValueError:
                return None

        for p, lines in doc.get("executable", {}).items():
            r = rel(p)
            if r:
                executable.setdefault(r, set()).update(lines)
        tests.extend(karma_tests(doc, repo, label, run.get("cwd", ""), rel))
        suites.append({"name": label, "source": "karma", "status": "ran",
                       "tests": len(doc.get("tests", [])),
                       "seconds": round(time.monotonic() - t0, 1),
                       "note": f"exit {rc}, see {WORK}/karma-{i}.log" if rc else ""})
    return tests, suites, executable


#: coverage.py's settings for a per-test run, written fresh each time beside its data.
#: `test_function` names every line's context after the test that ran it
#: (`tests.core.test_x.TestK.test_m`); lines run at import carry the empty context.
PYTEST_RC = """[run]
dynamic_context = test_function
relative_files = True
data_file = {data}
source = .
omit =
    tests/*
    */tests/*
    test_*.py
    */test_*.py
    conftest.py
    */conftest.py
    .venv/*
    */site-packages/*
"""


def pytest_tests(xml: Path, contexts: dict[str, dict[str, list[int]]], label: str) -> list[dict]:
    """One row per `<testcase>` of a pytest JUnit report (xunit1: it carries `file` and
    `line`), its hits the lines run under its test function's context — shared by the
    parameters of one parametrised test, which coverage does not tell apart."""
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return out
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname") or "", tc.get("name") or ""
        status = "passed"
        if tc.find("failure") is not None or tc.find("error") is not None:
            status = "failed"
        elif tc.find("skipped") is not None:
            status = "skipped"
        file = tc.get("file")
        line = int(tc.get("line")) + 1 if (tc.get("line") or "").isdigit() else None
        # `tests.core.test_x.TestK` -> the part after the module is the class path.
        mod = Path(file).with_suffix("").as_posix().replace("/", ".") if file else ""
        classes = cls[len(mod) + 1:].split(".") if mod and cls.startswith(mod + ".") else []
        func = name.split("[", 1)[0]
        test_id = "::".join([file or cls, *classes, name])
        out.append({"id": test_id, "suite": label, "title": name, "file": file, "line": line,
                    "status": status, "source": "pytest",
                    "hits": contexts.get(f"{cls}.{func}", {})})
    return out


def pytest_suites(cfg: list[dict], repo: Repo, init: dict, reuse: bool = False,
                  in_scope_fn=lambda p: True) -> tuple[list[dict], list[dict], dict]:
    """Each configured pytest run, once, under coverage.py with per-test contexts.

    `steps.testcov.pytest` is a list of `{label, python, args, cwd}`: `python` is how to
    reach the service's own interpreter (`pipenv run python`, a venv's `bin/python`) — the
    suite runs in the service's environment, never this skill's. It needs `coverage`
    there (pytest-cov brings it). Lines run only at import go to `init`, charged to no
    test."""
    tests, suites, executable = [], [], {}
    for i, run in enumerate(cfg):
        label = run.get("label", "pytest")
        cwd = (repo.root / run.get("cwd", ".")).resolve()
        work = WORK.resolve()
        data, rc_file = work / f"pytest-{i}.coverage", work / f"pytest-{i}.coveragerc"
        xml, js = work / f"pytest-{i}.xml", work / f"pytest-{i}.json"
        py = run.get("python") or "python3"
        t0 = time.monotonic()
        rc = 0
        if not reuse:
            for f in (data, xml, js):
                f.unlink(missing_ok=True)
            rc_file.write_text(PYTEST_RC.format(data=data), encoding="utf-8")
            rc = run_logged(f"{py} -m coverage run --rcfile={shlex.quote(str(rc_file))} -m pytest "
                            f"{run.get('args', '-q -p no:cacheprovider')} -o junit_family=xunit1 "
                            f"--junitxml={shlex.quote(str(xml))}",
                            cwd, WORK / f"pytest-{i}.log")
            run_logged(f"{py} -m coverage json --rcfile={shlex.quote(str(rc_file))} "
                       f"--show-contexts -q -o {shlex.quote(str(js))}", cwd,
                       WORK / f"pytest-{i}.json.log")
        if not js.is_file():
            suites.append({"name": label, "source": "pytest", "status": "failed", "tests": 0,
                           "seconds": round(time.monotonic() - t0, 1),
                           "note": f"exit {rc} and no per-test coverage — see {WORK}/pytest-{i}.log"})
            continue
        doc = json.loads(js.read_text(encoding="utf-8"))
        contexts: dict[str, dict[str, list[int]]] = {}
        prefix = cwd.relative_to(repo.root.resolve()).as_posix()
        for f, v in (doc.get("files") or {}).items():
            path = f if prefix in ("", ".") else f"{prefix}/{f}"
            if not in_scope_fn(path):
                continue
            ran = set(v.get("executed_lines") or []) | set(v.get("missing_lines") or [])
            at_import = set()
            for ln, ctxs in (v.get("contexts") or {}).items():
                named = [c for c in ctxs if c]
                if not named:
                    at_import.add(int(ln))
                for c in named:
                    contexts.setdefault(c, {}).setdefault(path, []).append(int(ln))
            if at_import:
                init.setdefault(path, set()).update(at_import)
            executable.setdefault(path, set()).update(ran - at_import)
        for hits in contexts.values():
            for p in hits:
                hits[p] = sorted(set(hits[p]))
        rows = pytest_tests(xml, contexts, label)
        tests.extend(rows)
        failed = sum(1 for t in rows if t["status"] == "failed")
        suites.append({"name": label, "source": "pytest", "status": "ran", "tests": len(rows),
                       "seconds": round(time.monotonic() - t0, 1),
                       "note": (f"{failed} failed — " if failed else "")
                       + (f"exit {rc}, see {WORK}/pytest-{i}.log" if rc else "")})
    return tests, suites, executable


#: The project's own Vite/Vitest config, with this skill's per-test hook added to its
#: setupFiles. Written under the project's node_modules so `vitest/config` resolves from it
#: and nothing tracked changes.
VITEST_WRAPPER = """import base from {config};
import {{defineConfig, mergeConfig}} from 'vitest/config';
export default defineConfig(async (env) => {{
  const b = typeof base === 'function' ? await base(env) : base;
  const own = (b.test && b.test.setupFiles) || [];
  return mergeConfig({{...b, root: {root}, test: {{...(b.test || {{}}), setupFiles: []}}}}, {{test: {{
    setupFiles: [...(Array.isArray(own) ? own : [own]).map(f => {root} + '/' + f.replace(/^\\.\\//, '')), {setup}],
    includeTaskLocation: true, sequence: {{concurrent: false}}, reporters: ['default'],
  }}}});
}});
"""


def vitest_suites(cfg: list[dict], repo: Repo, reuse: bool = False) -> tuple[list[dict], list[dict], dict]:
    """Each configured Vitest run (`steps.testcov.vitest`: `{label, cwd, command, config}`),
    with `vitest-percov.mjs` taking V8's coverage around every test, joined to source lines
    by `v8-join.js` through the inline source maps Vite writes."""
    tests, suites, executable = [], [], {}
    for i, run in enumerate(cfg):
        label = run.get("label", "Vitest")
        cwd = (repo.root / run.get("cwd", ".")).resolve()
        prefix = run.get("cwd", "").strip("/")
        out = (WORK / f"vitest-{i}").resolve()
        t0 = time.monotonic()
        rc = 0
        if not reuse:
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            hook = cwd / "node_modules" / ".cache" / "hr"
            hook.mkdir(parents=True, exist_ok=True)
            shutil.copy(TOOLS / "vitest-percov.mjs", hook / "vitest-percov.mjs")
            config = cwd / run.get("config", next((c for c in ("vitest.config.ts", "vitest.config.mts",
                                                               "vite.config.ts", "vite.config.mts",
                                                               "vite.config.js") if (cwd / c).is_file()),
                                                  "vite.config.ts"))
            (hook / "vitest.config.mjs").write_text(VITEST_WRAPPER.format(
                config=json.dumps(str(config)), root=json.dumps(str(cwd)),
                setup=json.dumps(str(hook / "vitest-percov.mjs"))), encoding="utf-8")
            rc = run_logged(f"{run.get('command', 'npx vitest run')} --config "
                            f"{shlex.quote(str(hook / 'vitest.config.mjs'))}",
                            cwd, WORK / f"vitest-{i}.log",
                            env={"CI": "true", "HR_TESTCOV_VITEST_OUT": str(out),
                                 "HR_TESTCOV_VITEST_ROOT": str(cwd), **(run.get("env") or {})})
        files = sorted(f for f in out.glob("*.json")) if out.is_dir() else []
        if not files:
            suites.append({"name": label, "source": "vitest", "status": "failed", "tests": 0,
                           "seconds": round(time.monotonic() - t0, 1),
                           "note": f"exit {rc} and no per-test coverage — see {WORK}/vitest-{i}.log"})
            continue
        r = subprocess.run(["node", str(TOOLS / "v8-join.js"), str(out), prefix,
                            str(cwd / "node_modules")], capture_output=True, text=True)
        joined = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else \
            {"tests": {}, "executable": {}}
        if r.returncode:
            log(f"v8-join failed for {label}: {r.stderr[-800:]}")
        for p, lines in joined.get("executable", {}).items():
            executable.setdefault(p, set()).update(lines)
        for f in files:
            t = json.loads(f.read_text(encoding="utf-8"))
            file = f"{prefix}/{t['file']}" if prefix and t.get("file") else t.get("file")
            tests.append({"id": f"{label}:{t.get('id')}", "suite": label, "title": t.get("title", ""),
                          "file": file, "line": t.get("line"), "status": t.get("status", ""),
                          "source": "vitest", "hits": joined.get("tests", {}).get(f.name, {})})
        failed = sum(1 for t in tests if t["suite"] == label and t["status"] == "failed")
        suites.append({"name": label, "source": "vitest", "status": "ran",
                       "tests": sum(1 for t in tests if t["suite"] == label),
                       "seconds": round(time.monotonic() - t0, 1),
                       "note": (f"{failed} failed — " if failed else "")
                       + (f"exit {rc}, see {WORK}/vitest-{i}.log" if rc else "")})
    return tests, suites, executable


def e2e_suites(cfg: dict, repo: Repo, tools: dict | None, head: str,
               node_modules: list[str], init: dict) -> tuple[list[dict], list[dict], dict]:
    tests, suites, executable = [], [], {}
    base = repo.root / cfg.get("dir", str(WORK))
    project = cfg.get("project", "")
    names = cfg.get("suites") or {"playwright": "E2E Playwright", "cucumber": "E2E Cucumber"}
    jar = base / cfg.get("jar", "backend-app.jar")
    for sub, label in names.items():
        d = base / sub
        run = d / "run.json"
        if not run.is_file():
            suites.append({"name": label, "source": "jacoco+v8", "status": "skipped", "tests": 0,
                           "note": f"no {d.relative_to(repo.root) if d.is_relative_to(repo.root) else d}"
                                   "/run.json — the browser suite did not run with COVERAGE_DIR set"})
            continue
        meta = json.loads(run.read_text(encoding="utf-8"))
        if meta.get("commit") and head and not head.startswith(meta["commit"]) \
                and not meta["commit"].startswith(head):
            suites.append({"name": label, "source": "jacoco+v8", "status": "stale", "tests": 0,
                           "note": f"measured on {meta['commit'][:8]}, not on HEAD {head[:8]} — "
                                   "re-run the traced suite"})
            continue
        t0 = time.monotonic()
        files = sorted(f for f in d.glob("*.json") if f.name != "run.json")
        # The backend's side (JaCoCo dumps of a JVM under the agent) went with Java support;
        # what a browser suite leaves of its own (Chromium's V8 coverage) is still read.
        jc = {"execs": {}, "executable": {}, "init": {}}
        v8 = {"tests": {}, "executable": {}}
        if (d / "scripts").is_dir():
            r = subprocess.run(["node", str(TOOLS / "v8-join.js"), str(d), cfg.get("frontend", ""),
                                *node_modules], capture_output=True, text=True)
            if r.returncode == 0:
                v8 = json.loads(r.stdout)
            else:
                log(f"v8-join failed for {sub}: {r.stderr[-800:]}")
        for path, lines in v8.get("executable", {}).items():
            executable.setdefault(path, set()).update(lines)
        n = 0
        for f in files:
            t = json.loads(f.read_text(encoding="utf-8"))
            hits: dict[str, list[int]] = {}
            for src, lines in jc["execs"].get(f.stem + ".exec", {}).items():
                path = repo.by_suffix(src, cfg.get("backend", ""))
                if path:
                    hits[path] = lines
            for path, lines in v8["tests"].get(f.name, {}).items():
                hits[path] = sorted(set(hits.get(path, [])) | set(lines))
            file = f"{project}/{t['file']}" if project and t.get("file") else t.get("file")
            n += 1
            tests.append({"id": f"{label}:{t.get('id')}", "suite": label, "title": t.get("title", ""),
                          "file": file, "line": t.get("line"), "status": t.get("status", ""),
                          "source": "+".join(s for s, on in (("jacoco", f.stem + ".exec" in jc["execs"]),
                                                             ("v8", f.name in v8["tests"])) if on),
                          "hits": hits})
        missing = [] if jar.is_file() else [f"no {jar.name} — the backend side is not measured"]
        suites.append({"name": label, "source": "jacoco+v8", "status": "ran", "tests": n,
                       "seconds": round(time.monotonic() - t0, 1),
                       "note": "; ".join(missing), "measuredAt": meta.get("startedAt", "")})
    return tests, suites, executable


# --------------------------------------------------------------------------------- main

def fold_signature(group: dict, executable: dict, tests: list[dict]) -> None:
    """A method's own signature line, changed, counts as run whenever its body's first line
    runs. Reporting it as "not measurable, see its body" would put every renamed parameter
    on the list of things no probe can see, when every probe that sees the body sees it."""
    path = group["file"]
    body = group["proxy"][path][0]
    executable.setdefault(path, set()).update(group["lines"])
    for t in tests:
        got = t["hits"].get(path)
        if got and body in got:
            t["hits"][path] = sorted(set(got) | set(group["lines"]))




def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--config", default="human-review.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only", help="comma-separated: pytest,vitest,karma,e2e")
    ap.add_argument("--reuse", action="store_true",
                    help="run no suite; re-read what the last run left in .human-review/coverage")
    args = ap.parse_args(argv)

    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True).stdout.strip() or ".")
    os.chdir(root)
    try:
        cfg = (json.loads(Path(args.config).read_text(encoding="utf-8")).get("steps") or {}).get("testcov") or {}
    except (OSError, ValueError):
        cfg = {}
    if not cfg:
        log("steps.testcov is not configured in human-review.json — nothing to measure")
        return 3
    only = set(args.only.split(",")) if args.only else {"pytest", "vitest", "junit", "karma", "e2e"}
    repo = Repo(root)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    mb = subprocess.run(["git", "merge-base", args.base, "HEAD"], capture_output=True,
                        text=True).stdout.strip() or args.base
    sources, exclude = cfg.get("sources") or [], cfg.get("exclude") or []

    diff = subprocess.run(["git", "diff", "-U0", "--no-color", "--no-ext-diff", mb, "HEAD"],
                          capture_output=True, text=True).stdout
    changed: dict[str, list[int]] = {}
    for path, lines in parse_diff(diff).items():
        if not in_scope(path, sources, exclude):
            continue
        src = repo.text(path)
        keep = [n for n in lines if n <= len(src) and not TRIVIAL.match(src[n - 1])]
        if keep:
            changed[path] = keep

    hold_measuring_lock(WORK)
    tools = None
    tests, suites, executable, init = [], [], {}, {}

    def merge(got):
        t, s, ex = got
        tests.extend(t)
        suites.extend(s)
        for k, v in ex.items():
            executable.setdefault(k, set()).update(v)

    if cfg.get("pytest") and "pytest" in only:
        WORK.mkdir(parents=True, exist_ok=True)
        merge(pytest_suites(cfg["pytest"], repo, init, args.reuse,
                            lambda p: in_scope(p, sources, exclude)))
    if cfg.get("junit"):
        suites.append({"name": "JUnit", "source": "jacoco", "status": "skipped", "tests": 0,
                       "note": "JUnit/JaCoCo is not measured in the Ludvig fork — "
                               "configure steps.testcov.pytest"})
    if cfg.get("vitest") and "vitest" in only:
        WORK.mkdir(parents=True, exist_ok=True)
        merge(vitest_suites(cfg["vitest"], repo, args.reuse))
    if cfg.get("karma") and "karma" in only:
        merge(karma_suites(cfg["karma"], repo, args.reuse))
    if cfg.get("e2e") and "e2e" in only:
        mods = [str(root / p / "node_modules") for p in
                (cfg["e2e"].get("project"), cfg["e2e"].get("frontend"),
                 *[k.get("cwd") for k in cfg.get("karma") or []]) if p]
        merge(e2e_suites(cfg["e2e"], repo, tools, head, mods, init))

    unmeasurable = []
    find = None
    for path, lines in changed.items():
        ex = executable.get(path, set())
        dark = [n for n in lines if n not in ex]
        for g in classify(path, repo.text(path), dark, ex, find, init.get(path, set()) - ex):
            if g["reason"] == SIGNATURE:
                fold_signature(g, executable, tests)
            else:
                unmeasurable.append(g)

    doc = {
        "version": 1, "head": head, "base": mb,
        "changed": changed,
        "executable": {p: sorted(executable[p]) for p in changed if p in executable},
        # Every production file's, not only the changed ones': the Tests tab joins the
        # diff, the Code City colours the whole plate, and both divide by this.
        "executableAll": {p: sorted(v) for p, v in sorted(executable.items())
                          if v and in_scope(p, sources, exclude)},
        "unmeasurable": unmeasurable,
        "suites": suites,
        "tests": tests,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=0, separators=(",", ":")) + "\n", encoding="utf-8")
    ran = sum(1 for s in suites if s["status"] == "ran")
    log(f"{len(tests)} test(s) in {ran} suite(s); {sum(map(len, changed.values()))} changed "
        f"line(s) in {len(changed)} file(s) -> {out}")
    for s in suites:
        log(f"  {s['name']:<22} {s['status']:<8} {s.get('tests', 0):>4} test(s)  "
            f"{s.get('seconds', 0):>6}s  {s.get('note', '')}")
    return 0 if tests or ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
