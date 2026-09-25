#!/usr/bin/env python3
"""Which tests execute which changed lines — measured, per test, by coverage.

The Tests tab used to answer "what tests cover this change?" with a model's reading of the
ticket: a list of tests paired with its sentences, sixteen long on the PR it was built for.
Fifty-seven tests on that same PR actually execute a changed line. The pairing says which
tests were *meant* for the change; only a run can say which tests *reach* it, and those are
different questions with different answers. This is the program that asks the second.

It runs each suite once with a per-test coverage hook — nothing in the project changes:

  * **JUnit** (`steps.testcov.junit`): a JUnit Platform listener this skill ships, put on
    the test classpath from the command line, dumps and resets JaCoCo's counters around
    every test. The project's own JaCoCo agent does the measuring (`prepare-agent`);
  * **Karma** (`steps.testcov.karma`): a Karma config this skill ships loads the project's
    own and adds a framework + reporter that snapshot Istanbul's counters around every spec;
  * **browser suites** (`steps.testcov.e2e`): not run from here. They already ran, traced,
    for the Tests tab's recordings (`city.tests`, `traces.commands`); run with COVERAGE_DIR
    set, the project's fixtures leave per-test Chromium coverage and per-test JaCoCo dumps
    of a backend started under the agent. This harvests what they left, and refuses what a
    run of another commit left.

Writes `.human-review/assets/test-coverage.json`:

  changed       {file: [lines]}   changed lines of production code worth a reader's eye
                                  (blank lines, comments, braces and imports dropped)
  executable    {file: [lines]}   of those files, every line some probe can see run
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
             [--out .human-review/assets/test-coverage.json] [--only junit,karma,e2e]

Exit 0 with the file written; 3 when `steps.testcov` is not configured; 1 on a failure that
left nothing to write.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "testcov"
WORK = Path(".human-review/coverage")
DEFAULT_OUT = Path(".human-review/assets/test-coverage.json")
CACHE = Path(os.environ.get("HR_TESTCOV_CACHE")
             or Path.home() / ".cache" / "human-review" / "testcov")
M2 = Path(os.environ.get("MAVEN_REPO") or Path.home() / ".m2" / "repository")

ENGINES = {"junit-jupiter": "JUnit", "cucumber": "Cucumber", "junit-vintage": "JUnit 4",
           "testng": "TestNG", "spock": "Spock", "archunit": "ArchUnit"}
STATUS = {"SUCCESSFUL": "passed", "FAILED": "failed", "ABORTED": "skipped"}


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
DECL = re.compile(r"^\s*(?!(?:return|new|throw|if|for|while|switch|catch|else)\b)"
                  r"(?:[\w<>\[\],.?@]+\s+)*?(\w+)\s*\(")
ANNOT = re.compile(r"^\s*@\w")


def owner_declaration(src: list[str], line: int, lookahead: int = 25) -> int | None:
    """The line of the method a non-executable changed line belongs to — itself, when it is
    the declaration; the declaration under it, when it is an annotation or a line inside one
    (a `@Query` whose text block spans five lines). None for a field or a class
    line, which have no method to stand in for them."""
    start = line
    for k in range(line - 1, max(0, line - 16) - 1, -1):      # the annotation it sits in
        t = src[k]
        if ANNOT.match(t):
            start = k + 1
            break
        if k < line - 1 and t.rstrip().endswith((";", "{", "}")):
            break
    depth, block = 0, False
    for i in range(start - 1, min(len(src), start - 1 + lookahead)):
        t = src[i]
        inside = block or depth > 0
        if inside or ANNOT.match(t) or re.match(r'^\s*("|\+|\))', t):
            depth += t.count("(") - t.count(")")
            if t.count('"""') % 2:
                block = not block
            continue
        if DECL.match(t) and not t.strip().startswith(("//", "*")):
            return i + 1
        if i > start - 1 or ";" in t or "=" in t:
            return None
    return None


def classify(path: str, src: list[str], lines: list[int], executable: set[int],
             callers, init: set[int] | frozenset = frozenset()) -> list[dict]:
    """The changed lines of `path` that no probe sees run, grouped by why.

    `callers(name)` returns `{file: [lines]}` of executable call sites of a method name,
    and is only asked for a Java method with no body of its own (a Spring Data repository
    method, an interface's declaration): the only thing that runs "because of" such a line
    is the code that calls it. `init` are the lines only a constructor runs, which no test
    is charged for (see TestcovAnalyze)."""
    if not lines:
        return []
    ext = Path(path).suffix.lower()
    java = ext in (".java", ".kt")
    if not executable and not java:
        why = {".sql": "SQL — runs inside the database, where no probe looks",
               ".html": "template — no browser run mapped it",
               ".ts": "types — erased when compiled",
               ".java": "no bytecode on these lines",
               ".properties": "configuration", ".yml": "configuration",
               ".yaml": "configuration", ".xml": "configuration",
               ".json": "configuration"}.get(ext, "no probe for this kind of file")
        return [{"file": path, "lines": lines, "reason": why}]
    if not java:
        why = "types — erased when compiled" if ext == ".ts" else "declarations — no code of their own"
        return [{"file": path, "lines": lines, "reason": why}]
    groups: dict[tuple, dict] = {}
    for ln in lines:
        reason, proxy = "declaration — no code of its own", None
        owner = None if ln in init else owner_declaration(src, ln)
        if ln in init:
            reason = "runs while the object is built — charged to no test"
        if owner:
            body = None
            for n in range(owner, min(owner + 40, len(src) + 1)):
                if n in executable:
                    body = n
                    break
                if n > owner and DECL.match(src[n - 1]):
                    break
            decl = src[owner - 1]
            m = DECL.match(decl)
            name = m.group(1) if m else None
            if name and name == Path(path).stem:
                reason = "constructor — runs while the object is built, charged to no test"
            elif body is not None and not decl.rstrip().endswith(";"):
                reason = ("annotation — runs as its method runs" if ANNOT.match(src[ln - 1])
                          or ln != owner else SIGNATURE)
                proxy = {path: [body]}
            else:
                sites = callers(name) if name else {}
                reason = "no body of its own — runs where it is called"
                proxy = sites or None
                if not sites:
                    reason = "no body of its own, and nothing that runs calls it"
        key = (reason, json.dumps(proxy, sort_keys=True))
        g = groups.setdefault(key, {"file": path, "lines": [], "reason": reason,
                                    **({"proxy": proxy} if proxy else {})})
        g["lines"].append(ln)
    return list(groups.values())


# ------------------------------------------------------------------------------- tools

def _versions(d: Path) -> list[str]:
    def key(v: str):
        return [int(x) if x.isdigit() else -1 for x in re.split(r"[.-]", v)]
    stable = [p.name for p in d.iterdir() if p.is_dir() and not re.search(r"[A-Za-z]", p.name)] \
        if d.is_dir() else []
    return sorted(stable, key=key)


def m2_jar(group: str, artifact: str, version: str | None = None,
           classifier: str = "") -> Path:
    """A jar out of the local Maven repository, fetched into it when missing."""
    d = M2 / group.replace(".", "/") / artifact
    got = _versions(d)
    ver = version if version in got else (got[-1] if got and not version else version)
    if not ver:
        raise RuntimeError(f"no {group}:{artifact} in {M2} and no version to fetch")
    suffix = f"-{classifier}" if classifier else ""
    jar = d / ver / f"{artifact}-{ver}{suffix}.jar"
    if not jar.is_file():
        coord = f"{group}:{artifact}:{ver}" + (f":jar:{classifier}" if classifier else "")
        log(f"fetching {coord}")
        subprocess.run(["mvn", "-q", "dependency:get", f"-Dartifact={coord}"], check=False)
    if not jar.is_file():
        raise RuntimeError(f"{jar} is missing and Maven could not fetch it")
    return jar


def jacoco_classpath(version: str = "0.8.13") -> list[Path]:
    core = m2_jar("org.jacoco", "org.jacoco.core", version)
    asm = "9.8"
    pom = M2 / "org/jacoco/org.jacoco.build" / core.parent.name / \
        f"org.jacoco.build-{core.parent.name}.pom"
    if pom.is_file():
        m = re.search(r"<asm.version>([^<]+)</asm.version>", pom.read_text(errors="replace"))
        asm = m.group(1) if m else asm
    return [core] + [m2_jar("org.ow2.asm", a, asm) for a in ("asm", "asm-commons", "asm-tree")]


def build_tools() -> dict:
    """Compile the listener and the analyzer into the user cache, once per source revision.

    Built, never committed: a jar in the repository is a binary nobody can review, and the
    two classes compile in two seconds against jars every Maven user already has."""
    srcs = sorted((TOOLS / "java").glob("*.java"))
    jacoco = jacoco_classpath()
    platform = [m2_jar("org.junit.platform", a, "1.8.2") if (M2 / "org/junit/platform" / a / "1.8.2").is_dir()
                else m2_jar("org.junit.platform", a)
                for a in ("junit-platform-launcher", "junit-platform-engine", "junit-platform-commons")]
    extra = [m2_jar("org.opentest4j", "opentest4j")]
    h = hashlib.sha256()
    for s in srcs:
        h.update(s.read_bytes())
    for j in jacoco + platform:
        h.update(str(j).encode())
    out = CACHE / h.hexdigest()[:16]
    listener = out / "hr-testcov-listener.jar"
    classes = out / "classes"
    if not listener.is_file():
        shutil.rmtree(out, ignore_errors=True)
        classes.mkdir(parents=True)
        cp = os.pathsep.join(map(str, jacoco + platform + extra))
        r = subprocess.run(["javac", "--release", "11", "-nowarn", "-Xlint:none", "-d", str(classes),
                            "-cp", cp, *map(str, srcs)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("javac failed:\n" + r.stderr[-2000:])
        with zipfile.ZipFile(listener, "w") as z:
            z.write(classes / "hr/testcov/PerTestJacoco.class", "hr/testcov/PerTestJacoco.class")
            z.writestr("META-INF/services/org.junit.platform.launcher.TestExecutionListener",
                       "hr.testcov.PerTestJacoco\n")
        log(f"built the JUnit listener and the JaCoCo analyzer into {out}")
    return {"listener": listener, "analyze_cp": os.pathsep.join(map(str, [classes] + jacoco))}


def analyze(tools: dict, classes: list[str], include: list[str], execs: list[Path]) -> dict:
    """Run TestcovAnalyze over `execs`; its JSON, with ranges expanded."""
    args = ["java", "-cp", tools["analyze_cp"], "hr.testcov.TestcovAnalyze"]
    for c in classes:
        args += ["--classes", c]
    for p in include:
        args += ["--include", p]
    r = subprocess.run(args + [str(e) for e in execs], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("TestcovAnalyze failed:\n" + r.stderr[-2000:])
    doc = json.loads(r.stdout)
    return {"classes": doc.get("classes", 0),
            "executable": {k: unranges(v) for k, v in doc["executable"].items()},
            "init": {k: unranges(v) for k, v in doc.get("init", {}).items()},
            "execs": {name: {k: unranges(v) for k, v in m.items()}
                      for name, m in doc["execs"].items()}}


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


def spec_line(src: list[str], description: str) -> int | None:
    for quote in ("'", '"', "`"):
        needle = quote + description.replace(quote, "\\" + quote) + quote
        for i, t in enumerate(src):
            if needle in t and re.search(r"\b(f|x)?it\s*\(", t):
                return i + 1
    for i, t in enumerate(src):
        if description in t:
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


def junit_suites(cfg: list[dict], repo: Repo, tools: dict,
                 init: dict) -> tuple[list[dict], list[dict], dict]:
    tests, suites, executable = [], [], {}
    for i, run in enumerate(cfg):
        label = run.get("label", "JUnit")
        out = (WORK / f"junit-{i}").resolve()
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        t0 = time.monotonic()
        slot = (f"-Dmaven.test.additionalClasspath={shlex.quote(str(tools['listener']))} "
                f"-Dhr.testcov.dir={shlex.quote(str(out))}")
        rc = run_logged(expand(run["command"], junit=slot, listener=str(tools["listener"]),
                               dir=str(out)),
                        repo.root / run.get("cwd", "."), WORK / f"junit-{i}.log")
        rows = []
        idx = out / "index.jsonl"
        if idx.is_file():
            rows = [json.loads(l) for l in idx.read_text(encoding="utf-8").splitlines() if l.strip()]
        note = f"exit {rc}, see {WORK}/junit-{i}.log" if rc else ""
        if not rows:
            suites.append({"name": label, "source": "jacoco", "status": "failed",
                           "tests": 0, "seconds": round(time.monotonic() - t0, 1),
                           "note": note or "no test reported per-test coverage — is JaCoCo's "
                                           "agent on the test JVM (jacoco:prepare-agent)?"})
            continue
        classes = [str(repo.root / c) for c in run.get("classes") or []]
        got = analyze(tools, classes, run.get("include") or [], [out / f"{int(r['n']):05d}.exec" for r in rows])
        prefer = run.get("cwd", "")
        for src, lines in got["executable"].items():
            path = repo.by_suffix(src, prefer)
            if path:
                executable.setdefault(path, set()).update(lines)
        for src, lines in got["init"].items():
            path = repo.by_suffix(src, prefer)
            if path:
                init.setdefault(path, set()).update(lines)
        per_suite: dict[str, int] = {}
        for r in rows:
            # The innermost engine: a Cucumber scenario run through a @Suite class is
            # `[engine:junit-platform-suite]/…/[engine:cucumber]/…`, and it is a scenario.
            engines = re.findall(r"\[engine:([^\]]+)\]", r.get("uid", ""))
            engine = engines[-1] if engines else ""
            suite = f"{label} {ENGINES.get(engine, engine)}".strip()
            hits = {}
            for src, lines in got["execs"].get(f"{int(r['n']):05d}.exec", {}).items():
                path = repo.by_suffix(src, prefer)
                if path:
                    hits[path] = lines
            file, line, title = None, None, r.get("name", "")
            if r.get("class"):
                outer = r["class"].split("$")[0]
                file = repo.by_suffix(outer.replace(".", "/") + ".java", prefer) \
                    or repo.by_suffix(outer.split(".")[-1] + ".java", prefer)
                title = r.get("method") or title
                line = decl_line(repo.text(file), title) if file else None
            elif r.get("resource") or r.get("file"):
                res = r.get("resource") or r.get("file")
                file = repo.by_suffix(res.lstrip("/"), prefer)
                line = int(r["line"]) if r.get("line") else None
            per_suite[suite] = per_suite.get(suite, 0) + 1
            tests.append({"id": f"{suite}:{r.get('uid')}", "suite": suite, "title": title,
                          "file": file, "line": line,
                          "status": STATUS.get(r.get("status"), (r.get("status") or "").lower()),
                          "source": "jacoco", "hits": hits})
        for name, n in per_suite.items():
            suites.append({"name": name, "source": "jacoco", "status": "ran", "tests": n,
                           "seconds": round(time.monotonic() - t0, 1), "note": note})
    return tests, suites, executable


def karma_suites(cfg: list[dict], repo: Repo) -> tuple[list[dict], list[dict], dict]:
    tests, suites, executable = [], [], {}
    conf = TOOLS / "karma" / "karma.conf.js"
    for i, run in enumerate(cfg):
        label = run.get("label", "Karma")
        cwd = repo.root / run.get("cwd", ".")
        out = (WORK / f"karma-{i}.json").resolve()
        out.unlink(missing_ok=True)
        t0 = time.monotonic()
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
        specs = [f for f in repo.files if f.endswith(".spec.ts")
                 and f.startswith(run.get("cwd", "").rstrip("/") + "/" if run.get("cwd") else "")]
        texts = {f: repo.text(f) for f in specs}
        for t in doc.get("tests", []):
            desc, full = t.get("description", ""), t.get("id", "")
            top = full[: max(0, len(full) - len(desc))].strip().split(" ")[0]
            cands = [f for f, src in texts.items() if any(desc in l for l in src)]
            if len(cands) > 1 and top:
                cands = [f for f in cands if any(top in l for l in texts[f])] or cands
            file = cands[0] if cands else None
            hits = {r: v for p, v in t.get("hits", {}).items() if (r := rel(p))}
            tests.append({"id": f"{label}:{full}", "suite": label, "title": desc or full,
                          "file": file, "line": spec_line(texts[file], desc) if file else None,
                          "status": t.get("status", ""), "source": "karma", "hits": hits})
        suites.append({"name": label, "source": "karma", "status": "ran",
                       "tests": len(doc.get("tests", [])),
                       "seconds": round(time.monotonic() - t0, 1),
                       "note": f"exit {rc}, see {WORK}/karma-{i}.log" if rc else ""})
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
        execs = sorted(d.glob("*.exec"))
        jc = {"execs": {}, "executable": {}}
        if execs and jar.is_file() and tools:
            jc = analyze(tools, [str(jar)], cfg.get("include") or [], execs)
            for src, lines in jc["executable"].items():
                path = repo.by_suffix(src, cfg.get("backend", ""))
                if path:
                    executable.setdefault(path, set()).update(lines)
            for src, lines in jc["init"].items():
                path = repo.by_suffix(src, cfg.get("backend", ""))
                if path:
                    init.setdefault(path, set()).update(lines)
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


def callers_finder(repo: Repo, sources, exclude, executable: dict[str, set[int]]):
    java = [f for f in repo.files if f.endswith((".java", ".kt")) and in_scope(f, sources, exclude)]
    texts: dict[str, list[str]] = {}

    def find(name: str) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        rx = re.compile(r"[.:]\s*" + re.escape(name) + r"\s*\(|::" + re.escape(name) + r"\b")
        for f in java:
            ex = executable.get(f)
            if not ex:
                continue
            src = texts.setdefault(f, repo.text(f))
            got = [i + 1 for i, t in enumerate(src) if rx.search(t) and i + 1 in ex]
            if got:
                out[f] = got
        return out
    return find


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--config", default="human-review.json")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--only", help="comma-separated: junit,karma,e2e")
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
    only = set(args.only.split(",")) if args.only else {"junit", "karma", "e2e"}
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

    WORK.mkdir(parents=True, exist_ok=True)
    tools = None
    if (cfg.get("junit") and "junit" in only) or cfg.get("e2e"):
        try:
            tools = build_tools()
        except Exception as e:                   # noqa: BLE001 - the other suites still run
            log(f"no JaCoCo tools: {e}")
    tests, suites, executable, init = [], [], {}, {}

    def merge(got):
        t, s, ex = got
        tests.extend(t)
        suites.extend(s)
        for k, v in ex.items():
            executable.setdefault(k, set()).update(v)

    if cfg.get("junit") and "junit" in only:
        if tools:
            merge(junit_suites(cfg["junit"], repo, tools, init))
        else:
            suites.append({"name": "JUnit", "source": "jacoco", "status": "failed", "tests": 0,
                           "note": "the listener could not be built"})
    if cfg.get("karma") and "karma" in only:
        merge(karma_suites(cfg["karma"], repo))
    if cfg.get("e2e") and "e2e" in only:
        mods = [str(root / p / "node_modules") for p in
                (cfg["e2e"].get("project"), cfg["e2e"].get("frontend"),
                 *[k.get("cwd") for k in cfg.get("karma") or []]) if p]
        merge(e2e_suites(cfg["e2e"], repo, tools, head, mods, init))

    unmeasurable = []
    find = callers_finder(repo, sources, exclude, executable)
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
