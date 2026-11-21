#!/usr/bin/env python3
"""Cognitive complexity of the whole flow behind every entry point, read from Java source.

`endpoint-complexity-delta.py` draws the derivative — which entry points this branch made
heavier — and used to be fed by a 900-line JUnit test in the reviewed project that read the
call graph out of bytecode with ASM and scored it from the AST with JavaParser. That test
was deleted from petclinic on 4 Sep 2026 as "tooling that is hard to explain and harder to
keep", and the Complexity tab has been empty ever since: a tab of this skill cannot depend
on a bespoke test living in the repository under review.

So the measurement moves here, and pays for being self-contained by being approximate:

  * **The score** is Cognitive Complexity (G. Ann Campbell / SonarSource) — `1 + nesting`
    per `if` / loop / `catch` / `switch` / ternary, a flat `+1` per `else` and per run of
    `&&` / `||`, `+1` for self-recursion, nothing for straight-line code. It is read from
    source, which is where nesting still exists.
  * **The flow** is every method reachable from the handler through calls this file can
    resolve: a bare `foo(…)` inside the class, `field.foo(…)` where the field, parameter or
    local has a declared type this project also declares, and `Type.foo(…)` — and the same
    four shapes written as method references (`field::foo`, `Type::foo`, `this::foo`,
    `Type::new` for a declared constructor), which is how a stream calls them. A call whose
    receiver is a capitalised name the project does not declare (`Integer.parseInt`) is a
    library's and is dropped. A call whose receiver has no known type resolves only when
    exactly one class in the project declares a method by that name; otherwise it is
    dropped rather than guessed at.
  * **`flowCc`** is the plain sum over the DISTINCT methods reached (cycles counted once).
    Summing needs no McCabe bookkeeping: straight-line code already scores 0.

Known limits, stated because the number is quoted to a reviewer: overloads collapse by
name; interface calls count every implementation whose class declares that name; runtime
code (Spring Data queries, proxies, generated mappers) is invisible past its leaf, so a
repository method scores 0; reflection is invisible; and a nesting level opened by a
brace-less `if` is not tracked. Both sides of the delta are measured by this same file, so
what the bars say the branch added stays honest even where the absolute number is low.

**Two engines.** Everything above describes the *regex* engine, which needs nothing but
Python. When a JDK is on the PATH the numbers come instead from `complexity/
ComplexityEngine.java` — JavaParser + its symbol solver over the same sources: a real
syntax tree (nesting is exact, brace-less `if`s included), and calls bound by type, so
overloads, chained calls, method references of all four kinds, static imports, nested
classes and interface dispatch are followed instead of guessed. A call into project code
that even the solver cannot place is not dropped in silence: it is listed per entry point
(`unresolved`) and the tab says "N calls not followed". The engine's two jars and its one
class are cached outside the reviewed repository (see `engine_classpath`). Its design and
the adversarial eval it had to pass are in `reference/complexity-engine-eval.md`.

Usage:
    endpoint-complexity.py --out after.json                      # the working tree
    endpoint-complexity.py --base origin/main --out before.json  # the merge-base
    endpoint-complexity.py --engine regex --out after.json       # force the regex engine
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Only main sources. Test sources are entry points into nothing the outside world can call,
# and generated sources are not on disk until something builds them.
SRC = re.compile(r"(?:^|/)src/main/java/.+\.java$")

# Comments and literals are blanked — not removed — so every offset in the stripped text
# still points at the same character of the original. That is what lets the structure be
# read off the blanked copy while annotation *values* (`@GetMapping("{visitId}")`) are read
# off the real one.
NOISE = re.compile(r'"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*.*?\*/', re.S)

MODIFIER = r"(?:public|protected|private|static|final|abstract|default|synchronized|native|strictfp)"
TYPE = r"[\w.$]+(?:\s*<[^<>;{}]*>)?(?:\s*\[\s*\])*"
# The parameter list tolerates three levels of nesting, with braces allowed inside them,
# because annotated parameters are ordinary in this code — `@RequestParam(name = "x")` is
# one level, and an OpenAPI `@Parameter(schema = @Schema(allowableValues = {"a", "b"}))`
# is two with an array initializer inside. A declaration this misses is a whole entry
# point missing from the tab, and worse, drawn as `gone` in the delta: that is how the
# paginated `GET /api/owners` vanished from the Complexity tab while it still existed.
# Only the top level forbids `{`, which is what tells a parameter list from a body.
_NEST3 = r"\([^();]*\)"
_NEST2 = rf"\((?:[^();]|{_NEST3})*\)"
_NEST1 = rf"\((?:[^();]|{_NEST2})*\)"
DECL = re.compile(
    rf"(?:{MODIFIER}\s+)*(?:<[^<>;{{}}]+>\s*)?(?:({TYPE})\s+)?([A-Za-z_$]\w*)\s*"
    rf"\(((?:[^;{{}}()]|{_NEST1})*)\)\s*(?:throws\s[^{{;]*)?\{{"
)
# Every construct that reads as `name (…) {` without being a method. `for` and the
# `try (…)` of a resource block carry a `;` inside the parens and never reach this set.
NOT_A_METHOD = {"if", "else", "for", "while", "do", "switch", "case", "catch", "try", "return",
                "new", "synchronized", "assert", "yield", "instanceof", "class",
                "enum", "interface", "throw", "super", "this"}
# `record` is a contextual keyword, not a reserved one: `void record(ChatResponse r)` is an
# ordinary method, and `meter.record(r)` an ordinary call. It only ever declares a type in
# the *return-type* slot — `record Leg(String model) {` reads as `<record> Leg(…) {` — so
# that is the only place it is refused. Refusing it as a name too dropped
# `TokenCostMeter.record` and both methods it calls from `GET /assistant`.
NOT_A_RETURN_TYPE = NOT_A_METHOD | {"record"}

TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|@interface)\s+(\w+)")
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
# `Type name` wherever one is declared — field, parameter or local. Scoping is ignored on
# purpose: one map per file is enough to type the receiver of nearly every call, and a name
# reused for two types in one file is rarer than the calls this resolves.
VAR = re.compile(r"\b([A-Z]\w*)(?:\s*<[^<>;{}]*>)?(?:\s*\[\s*\])?\s+([a-z_$]\w*)\s*(?=[;=,):])")
# A constant is capitalised like a type (`INSTANCE.run()`, `UUID.randomUUID()`), and a
# capitalised receiver nobody declared is read as a type from outside the project. So the
# constants are typed too — only where they are initialised, which is where `Type NAME =`
# cannot be anything else.
CONST = re.compile(r"\b([A-Z]\w*)(?:\s*<[^<>;{}]*>)?\s+([A-Z][A-Z0-9_]*)\s*(?==)")
CALL = re.compile(r"(?:(\w+)\s*\.\s*)?\b([A-Za-z_$]\w*)\s*\(")
# A method reference is a call the stream will make: `.map(ownerMapper::toOwnerDto)` runs
# the whole mapper chain exactly as `ownerMapper.toOwnerDto(o)` inside a lambda would. Read
# as `(receiver, method)` with the same typing as a dotted call — a field, a parameter, a
# class (`Owner::getId`), `this` or `super`. `Type::new` is the constructor, which the index
# files under the type's own name. Missing this dropped nine points off `GET /api/owners`
# in eval run 10 and drew a flow that had grown as one that shrank by five.
METHOD_REF = re.compile(r"\b(\w+)\s*::\s*(new\b|[A-Za-z_$]\w*)")


def calls_in(body: str) -> set[tuple[str, str]]:
    """Every `(receiver, name)` the body invokes: dotted and bare calls, lambdas' bodies
    included — they are text of the method like any other — and method references."""
    found = {(recv, called) for recv, called in CALL.findall(body) if called not in NOT_A_METHOD}
    for recv, called in METHOD_REF.findall(body):
        found.add((recv, recv if called == "new" else called))
    return found
ANNOT = re.compile(r"@(\w+)\s*(\((?:[^()]|\([^()]*\))*\))?")
STRING = re.compile(r'"([^"\\\n]*)"')

MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
            "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "ANY"}
LISTENERS = {"KafkaListener": "KAFKA", "RabbitListener": "RABBIT", "JmsListener": "JMS"}


def strip(text: str) -> str:
    return NOISE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


# ── sources ────────────────────────────────────────────────────────────────────────────

def sources(rev: str | None) -> dict[str, str]:
    """Every main Java source, keyed by repo-relative path — on disk, or at a revision.

    Reading the baseline out of git rather than checking it out keeps the "before" side
    free of a worktree and of whatever the build leaves behind in the tree."""
    if rev is None:
        root = Path(run("git", "rev-parse", "--show-toplevel"))
        # What git would commit — tracked plus untracked-but-not-ignored — never a gitignored
        # `target/generated-sources`: the merge-base side is read out of git and cannot see
        # those, so measuring them here would draw every generated handler as "new".
        listed = run("git", "-C", str(root), "ls-files", "-co", "--exclude-standard").splitlines()
        return {
            rel: (root / rel).read_text(encoding="utf-8", errors="replace")
            for rel in sorted(set(listed))
            if SRC.search(rel) and (root / rel).is_file()
            # `.human-review/.diffbase` holds the *base* copy of files this run is diffing;
            # measuring those too would file every handler twice, once per snapshot.
            and not any(part.startswith(".") for part in Path(rel).parts)
        }
    paths = [p for p in run("git", "ls-tree", "-r", "--name-only", rev).splitlines() if SRC.search(p)]
    if not paths:
        return {}
    # One batch call, because a `git show` per file over a few hundred sources is most of
    # the runtime of this script. Bytes, not text: the header counts bytes, and decoding
    # first would put the count and the offsets in different units.
    proc = subprocess.run(["git", "cat-file", "--batch"],
                          input="".join(f"{rev}:{p}\n" for p in paths).encode(),
                          capture_output=True, check=True)
    out, pos, blobs = proc.stdout, 0, {}
    for path in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("utf-8", "replace")
        pos = end + 1
        if header.endswith(("missing", "ambiguous")):
            continue  # a path git listed but cannot hand back is not worth failing over
        size = int(header.rsplit(" ", 1)[1])
        blobs[path] = out[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return blobs


def run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


# ── scoring one method ─────────────────────────────────────────────────────────────────

TOKEN = re.compile(r"\b(?:if|else|for|while|do|switch|catch)\b|&&|\|\||->|[{};?]")
NESTS = {"if", "for", "while", "do", "switch", "catch"}


def cognitive(body: str) -> int:
    """Campbell's Cognitive Complexity of an already-blanked method body."""
    return sum(inc for _, inc, _ in increments(body))


def increments(body: str) -> list[tuple[int, int, str]]:
    """Every increment the score is made of: `(offset in body, how much, what counted)`.

    The score is quoted to a reviewer as a measurement, and a measurement nobody can
    audit is a number to be argued with. So the walk that used to add to a counter now
    *files* each increment where it happened — the offset is enough to name the line,
    because the blanked body keeps every character of the original in place. `cognitive`
    is the sum of this list and nothing else, which is what keeps the breakdown under a
    row and the number beside it from ever disagreeing.

    Nesting is counted by braces: a construct that opens a block hands its level to the
    next `{`, and a `;` first cancels the hand-off, so `if (x) return;` scores its 1 without
    deepening whatever block follows it."""
    hits: list[tuple[int, int, str]] = []
    nesting = pending = 0
    stack: list[tuple[int, str]] = []
    last_bool: str | None = None
    opened_by = ""
    skip_while = skip_if = False
    for m in TOKEN.finditer(body):
        t = m.group()
        if t == "{":
            stack.append((pending, opened_by))
            nesting += pending
            pending, opened_by, last_bool = 0, "", None
        elif t == "}":
            if stack:
                added, by = stack.pop()
                nesting -= added
                skip_while = by == "do"
            last_bool = None
        elif t == ";":
            pending, last_bool = 0, None
        elif t in ("&&", "||"):
            # A run of the same operator is one thing to hold in your head; the alternation
            # is what costs. `a && b && c` scores 1, `(a && b) || c` scores 2.
            if last_bool != t:
                hits.append((m.start(), 1, t))
            last_bool = t
        elif t == "->":
            pending = 1  # a lambda body nests what is inside it, and costs nothing itself
            opened_by = "->"
        elif t == "?":
            # `List<?>` and a label-less `x ? a : b` are the same character; only the second
            # has an expression in front of it.
            if body[max(0, m.start() - 1)] not in "<," and body[m.end():m.end() + 1] not in ">,":
                hits.append((m.start(), 1 + nesting, "?:"))
        elif t == "else":
            hits.append((m.start(), 1, "else"))  # the `else` of an `else if` is the pair's whole cost
            pending, opened_by, skip_if = 1, "else", True
        elif t == "while" and skip_while:
            skip_while = False
        elif t == "if" and skip_if:
            skip_if, pending, opened_by = False, 1, "if"
        else:
            skip_if = False
            hits.append((m.start(), 1 + nesting, t))
            if t in NESTS:
                pending, opened_by = 1, t
    return hits


CYC = re.compile(r"\b(?:if|for|while|case|catch)\b|&&|\|\||\?")


def cyclomatic(body: str) -> int:
    """McCabe's number of an already-blanked method body: 1 + one per decision point.

    Shown beside the cognitive score on the call-graph nodes, because the two answer
    different questions — how many paths a test suite has to walk, and how hard the
    method is to read — and a reviewer used to Sonar asks for both. Every `if`, loop,
    `case`, `catch`, ternary and short-circuit operator is one more path; `else` and
    nesting are not. A `do … while` is its one `while`. The ternary test is the same one
    `increments` makes, so `List<?>` is not a decision."""
    n = 1
    for m in CYC.finditer(body):
        if m.group() == "?" and (body[max(0, m.start() - 1)] in "<,"
                                 or body[m.end():m.end() + 1] in ">,"):
            continue
        n += 1
    return n


def block(code: str, open_brace: int) -> int:
    """Offset just past the `}` that closes the `{` at `open_brace`."""
    depth = 0
    for i in range(open_brace, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(code)


# ── indexing a project ─────────────────────────────────────────────────────────────────

class Index:
    def __init__(self, files: dict[str, str]):
        self.methods: dict[str, dict] = {}        # "pkg.Class#name" -> method
        self.by_name: dict[str, list[str]] = {}   # "name" -> keys declaring it
        self.of_class: dict[str, dict[str, str]] = {}  # "pkg.Class" -> {name: key}
        self.classes: dict[str, str] = {}         # "Class" -> "pkg.Class"
        self.entries: list[dict] = []
        for path, text in files.items():
            self._read(path, text)

    def _read(self, path: str, text: str):
        code = strip(text)
        lines = text.splitlines()
        declared = PACKAGE.search(code)
        pkg = declared.group(1) if declared else ""
        decl = TYPE_DECL.search(code)
        if not decl:
            return
        # Methods of a nested class are filed under the file's primary type: it is the file
        # a reviewer opens, and it is what `entry_source` in the delta renderer looks up.
        simple = decl.group(1)
        fqcn = f"{pkg}.{simple}" if pkg else simple
        self.classes[simple] = fqcn
        types = {name: t for t, name in VAR.findall(code) + CONST.findall(code)}
        class_path = self._paths(text, code, decl.start()).get("path", "")
        for m in DECL.finditer(code, decl.end()):
            ret, name, params = m.group(1), m.group(2), m.group(3)
            if name in NOT_A_METHOD or (ret and ret.split("<")[0].strip() in NOT_A_RETURN_TYPE):
                continue
            at = m.end() - 1
            end = block(code, at)
            body = code[at:end]
            key = f"{fqcn}#{name}"
            calls = calls_in(body)
            method = self.methods.setdefault(
                key, {"key": key, "display": f"{simple}.{name}({_params(params)})",
                      "cc": 0, "cyc": 0, "hits": [], "calls": set(), "types": types,
                      "file": path, "spans": []})
            # Where the declaration stands, signature to closing brace — one span per
            # overload, since overloads collapse into one key here. `critical-flows.py`
            # reads it to tell whether a hunk of the diff lands inside a flow's method.
            # The span opens at the annotations, as JavaParser's does: `@Transactional`
            # added to a method changes what it does as surely as its body would.
            lead = max(code.rfind(c, 0, m.start()) for c in ";{}") + 1
            lead += len(code[lead:m.start()]) - len(code[lead:m.start()].lstrip())
            method["spans"].append((code.count("\n", 0, lead) + 1,
                                    code.count("\n", 0, max(end - 1, at)) + 1))
            hits = [_hit(path, code, lines, at + off, inc, why)
                    for off, inc, why in increments(body)]
            back = _calls_back(body, name, simple)
            if back is not None:
                # Self-recursion is charged once, at the first call that closes the loop —
                # the line a reader has to see to believe the +1.
                hits.append(_hit(path, code, lines, at + back, 1, "recursion"))
            method["cc"] += sum(h["inc"] for h in hits)
            method["cyc"] += cyclomatic(body)
            method["hits"] += hits
            method["calls"] |= calls
            self.by_name.setdefault(name, []).append(key)
            self.of_class.setdefault(fqcn, {})[name] = key
            self._entry(text, code, m.start(), key, method, class_path)

    def _entry(self, text: str, code: str, at: int, key: str, method: dict, prefix: str):
        """File this method as an entry point if its annotations make it one."""
        found = self._paths(text, code, at)
        kind_verb = found.get("verb")
        if not kind_verb:
            return
        kind, verb = kind_verb
        path = found.get("path", "")
        if kind == "http":
            # `@GetMapping` with no path is the class's own path, not a path named after the
            # method — `/api/visits`, the way the router will publish it.
            path = "/" + "/".join(p for p in f"{prefix}/{path}".split("/") if p)
        else:
            path = path or method["display"].split("(")[0]
        self.entries.append({"kind": kind, "httpMethod": verb, "path": path,
                             "handler": method["display"], "key": key})

    @staticmethod
    def _paths(text: str, code: str, at: int) -> dict:
        """Read the annotation block that sits just before `at`, off the *unblanked* text.

        The block starts after the previous member ends — the nearest `;`, `{` or `}` in the
        blanked copy, where a `;` inside an annotation's own string cannot be mistaken for
        one."""
        start = max(code.rfind(c, 0, at) for c in ";{}") + 1
        out: dict = {}
        for name, args in ANNOT.findall(text[start:at]):
            args = args or ""
            if name in MAPPINGS:
                verb = MAPPINGS[name]
                if name == "RequestMapping":
                    m = re.search(r"method\s*=\s*(?:\{\s*)?RequestMethod\.(\w+)", args)
                    verb = m.group(1) if m else "ANY"
                out["verb"] = ("http", verb)
                out["path"] = _value(args, "path", "value")
            elif name == "McpTool" or name == "Tool":
                out["verb"] = ("mcp", "MCP")
                out["path"] = _value(args, "name") or ""
            elif name in LISTENERS:
                out["verb"] = ("listener", LISTENERS[name])
                out["path"] = _value(args, "topics", "queues", "destination") or ""
            elif name == "Scheduled":
                out["verb"] = ("job", "JOB")
        return out

    def resolve(self, method: dict, recv: str, called: str) -> list[str]:
        """Which declared methods a `recv.called(…)` in `method` can mean — [] if unknowable."""
        if not recv or recv == "this":
            own = self.of_class.get(method["key"].rsplit("#", 1)[0], {}).get(called)
            return [own] if own else self._unique(called)
        owner = method["types"].get(recv) or self.classes.get(recv)
        if owner:
            fq = self.classes.get(owner, owner)
            here = self.of_class.get(fq, {}).get(called)
            if here:
                return [here]
            # An interface's own file declares the name but not the body; the work is in
            # whatever implements it, and source alone cannot say which. Count them all,
            # the same over-count the bytecode extractor admitted to.
            return [k for k in self.by_name.get(called, []) if k.rsplit("#", 1)[0] != fq] \
                if fq in self.classes.values() else []
        if recv[:1].isupper():
            # A capitalised receiver that is neither a typed variable nor a class of this
            # project is a type from somewhere else — `Integer`, `String`, `List`. Its
            # methods are not ours, however unique the name: `Integer.parseInt` bound to the
            # branch's new private `OwnerPageRequest#parseInt` in eval run 11 and put +2 on
            # four MCP tools nobody touched.
            return []
        return self._unique(called)

    def _unique(self, called: str) -> list[str]:
        keys = self.by_name.get(called, [])
        return keys if len(keys) == 1 else []

    def callees(self, key: str) -> list[str]:
        """What `key` calls, resolved, in the order `flow` discovers them — the edges of the
        call graph the Complexity tab draws under a row."""
        method = self.methods.get(key)
        if not method:
            return []
        out: list[str] = []
        for recv, called in sorted(method["calls"]):
            for target in self.resolve(method, recv, called):
                if target != key and target not in out:
                    out.append(target)
        return out

    def flow(self, key: str) -> list[str]:
        """Every method reachable from `key`, the handler first, each one once."""
        seen, queue, order = {key}, [key], []
        while queue:
            cur = queue.pop(0)
            if cur not in self.methods:
                continue
            order.append(cur)
            for target in self.callees(cur):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return order


def _calls_back(body: str, name: str, simple: str) -> int | None:
    """Offset of the first call in `body` that is the method calling itself, or None.

    Only a call that can only mean this class is one: bare `name(…)`, `this.name(…)`, or
    `Own.name(…)` (and the same as a method reference). A name match alone is not —
    `return Integer.parseInt(value);` inside a helper called `parseInt` is the JDK, and
    charging it as recursion put a phantom +1 on `GET /api/owners` in eval run 11.
    `super.name(…)` is the parent's body, not a loop back, and `a().name(…)` is a call
    on whatever `a()` returns."""
    own = {"", "this", simple}
    for c in CALL.finditer(body):
        if c.group(2) == name and (c.group(1) or "") in own \
                and not (c.group(1) is None and body[:c.start(2)].rstrip().endswith(".")):
            return c.start(2)
    for r in METHOD_REF.finditer(body):
        if r.group(2) == name and r.group(1) in own:
            return r.start(2)
    return None


MAX_LINE = 160


def _hit(path: str, code: str, lines: list[str], at: int, inc: int, why: str) -> dict:
    """One increment, told the way a reviewer can check it: the line it was counted on.

    The offset is into the *blanked* copy, which is the same length as the original, so
    counting newlines up to it names the line in the file on disk — no second parse, and
    no line table to keep in step. The text is the real source line, trimmed: it is
    evidence, not a snippet, and its indentation says nothing the nesting cost has not
    already said."""
    line = code.count("\n", 0, at) + 1
    src = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return {"file": path, "line": line,
            "code": src[:MAX_LINE] + ("…" if len(src) > MAX_LINE else ""),
            "inc": inc, "why": why}


def _params(params: str) -> str:
    """`@RequestBody @Validated VisitDto visitDto, int id` -> `VisitDto, int`.

    Annotations go first, whole: splitting on commas before they are gone turns one
    annotated parameter into two halves of a sentence."""
    bare = re.sub(r"@\w+\s*(\((?:[^()]|\([^()]*\))*\))?", " ", params).strip()
    out, depth, cur = [], 0, ""
    for ch in bare + ",":
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        if ch == "," and depth == 0:
            words = re.sub(r"<[^<>]*>", "", cur).split()
            if words:
                out.append(words[0].split(".")[-1])
            cur = ""
        else:
            cur += ch
    return ", ".join(out)


def _value(args: str, *names: str) -> str:
    """The first string in `@X("a")`, or in `@X(name = "a", …)` for one of `names`."""
    for name in names:
        m = re.search(rf"\b{name}\s*=\s*\{{?\s*\"([^\"]*)\"", args)
        if m:
            return m.group(1)
    if not re.search(r"\w+\s*=", args):
        m = STRING.search(args)
        if m:
            return m.group(1)
    return ""


def _where(method: dict) -> dict:
    """`file`, `line`, `endLine` of a flow item — the keys the JavaParser engine writes —
    plus `spans` when overloads collapsed into this one key and sit apart in the file."""
    spans = sorted(method.get("spans") or [])
    if not spans:
        return {}
    out = {"file": method["file"], "line": spans[0][0], "endLine": spans[0][1]}
    if len(spans) > 1:
        out["spans"] = [list(s) for s in spans]
    return out


def extract(files: dict[str, str]) -> list[dict]:
    index = Index(files)
    out = []
    for e in index.entries:
        flow = index.flow(e["key"])
        in_flow = set(flow)
        out.append({
            "kind": e["kind"], "httpMethod": e["httpMethod"], "path": e["path"],
            "handler": e["handler"],
            "metric": "cognitive",
            "flowCc": sum(index.methods[k]["cc"] for k in flow),
            "methods": len(flow),
            # `hits` is what the tab folds open under the row: every increment, with the
            # line it was read off. Carried per method rather than per entry point so the
            # sum stays checkable one method at a time, and so a method reached from two
            # entry points is described once, identically, under both.
            "flow": [{"method": k, "display": index.methods[k]["display"],
                      "cognitive": index.methods[k]["cc"],
                      "cyclomatic": index.methods[k]["cyc"],
                      # The edges, so the tab can draw the flow as a graph and not only
                      # as a list: every method of the flow this one calls.
                      "calls": [t for t in index.callees(k) if t in in_flow],
                      "hits": index.methods[k]["hits"],
                      **_where(index.methods[k])} for k in flow],
        })
    out.sort(key=lambda e: (-e["flowCc"], e["path"]))
    return out


# ── the JavaParser engine ──────────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
ENGINE_SRC = HERE / "complexity" / "ComplexityEngine.java"
ENGINE_CACHE = (Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                / "human-review" / "complexity-engine")
M2 = Path(os.environ.get("M2_REPO", Path.home() / ".m2" / "repository"))
MAVEN = "https://repo1.maven.org/maven2"
# Pinned: coordinates and the sha256 of each jar as published on Maven Central (checked
# against Central's own .sha1 when these were pinned, 7 Oct 2026). A jar whose bytes do not
# match is never put on a classpath — downloaded or found in ~/.m2 alike.
JARS = [
    ("com.github.javaparser", "javaparser-core", "3.28.2",
     "b5499a3b1c40b16c0671fabe478c9aafeab38160c6fde74a6c13f42d86716ecd"),
    ("com.github.javaparser", "javaparser-symbol-solver-core", "3.28.2",
     "4cb097c0834427939c01f4169dd0bab20068673913341c5b6db27be1a7445098"),
    ("org.javassist", "javassist", "3.31.0-GA",
     "44b6b900ca352f4a0048e9428efab16582a014e10f8f65b4e58f5136c704624e"),
    ("com.google.guava", "guava", "33.6.0-jre",
     "dc573e1fca4fd5454f4a5fd3d7da2df03002876a4175bafc14a95980dd7713b3"),
    ("com.google.guava", "failureaccess", "1.0.3",
     "cbfc3906b19b8f55dd7cfd6dfe0aa4532e834250d7f080bd8d211a3e246b59cb"),
]
# Which engine runs when none is asked for: "auto" is JavaParser when a JDK is on the PATH,
# the regex engine otherwise. HR_COMPLEXITY_ENGINE overrides it for a whole run.
DEFAULT_ENGINE = "auto"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jar(group: str, artifact: str, version: str, sha: str) -> Path:
    """One pinned jar: the cache, else the local Maven repository, else Maven Central."""
    name = f"{artifact}-{version}.jar"
    cached = ENGINE_CACHE / "lib" / name
    if cached.is_file() and _sha256(cached) == sha:
        return cached
    local = M2 / group.replace(".", "/") / artifact / version / name
    if local.is_file() and _sha256(local) == sha:
        return local
    url = f"{MAVEN}/{group.replace('.', '/')}/{artifact}/{version}/{name}"
    cached.parent.mkdir(parents=True, exist_ok=True)
    part = cached.with_suffix(".part")
    print(f"[complexity] fetching {name}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f)
    got = _sha256(part)
    if got != sha:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{name}: sha256 {got} is not the pinned {sha}")
    part.replace(cached)
    return cached


def have_jdk() -> bool:
    return bool(shutil.which("java") and shutil.which("javac"))


def engine_classpath() -> str:
    """The engine's classpath: the pinned jars, and its one class compiled into the user cache.

    Built, never committed — the same bargain as the test-coverage tools: a jar in the
    repository is a binary nobody can review, and the class compiles in two seconds. The
    build is keyed by the source's hash, so an edit to the engine rebuilds it and nothing
    else ever does."""
    jars = [_jar(*j) for j in JARS]
    h = hashlib.sha256(ENGINE_SRC.read_bytes())
    for *_, sha in JARS:
        h.update(sha.encode())
    out = ENGINE_CACHE / h.hexdigest()[:16]
    if not (out / "hr" / "complexity" / "ComplexityEngine.class").is_file():
        ENGINE_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=ENGINE_CACHE, prefix=".build-"))
        r = subprocess.run(["javac", "--release", "17", "-nowarn", "-encoding", "UTF-8",
                            "-d", str(tmp), "-cp", os.pathsep.join(map(str, jars)), str(ENGINE_SRC)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError("javac failed:\n" + r.stderr[-2000:])
        shutil.rmtree(out, ignore_errors=True)
        tmp.replace(out)
        print(f"[complexity] built the JavaParser engine into {out}", file=sys.stderr)
    return os.pathsep.join(map(str, [out, *jars]))


def extract_javaparser(files: dict[str, str]) -> list[dict] | None:
    """The same entries `extract` returns, read by the JavaParser engine — or None when it
    cannot run (no JDK, no network for a first fetch, a source it cannot parse), which the
    caller turns into the regex engine and a line on stderr saying so.

    The sources are written into a scratch tree first, at their repo-relative paths: the
    merge-base side has no checkout to point the engine at, and doing the same for the
    working tree keeps one code path — and keeps `.human-review/.diffbase` out of it the way
    `sources` already does."""
    if not have_jdk():
        print("[complexity] no JDK on the PATH — using the regex engine", file=sys.stderr)
        return None
    try:
        cp = engine_classpath()
    except Exception as e:  # noqa: BLE001 — any failure to get the engine means: fall back
        print(f"[complexity] JavaParser engine unavailable ({e}) — using the regex engine",
              file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory(prefix="hr-complexity-") as d:
        root = Path(d) / "src"
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        out = Path(d) / "out.json"
        root.mkdir(exist_ok=True)
        try:
            r = subprocess.run(["java", "-Xss8m", "-cp", cp, "hr.complexity.ComplexityEngine",
                                "--root", str(root), "--out", str(out)],
                               capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            print("[complexity] JavaParser engine timed out after 900 s — using the regex engine",
                  file=sys.stderr)
            return None
        sys.stderr.write(r.stderr[-4000:])
        if r.returncode != 0 or not out.is_file():
            print(f"[complexity] JavaParser engine failed (exit {r.returncode}) — "
                  "using the regex engine", file=sys.stderr)
            return None
        return json.loads(out.read_text(encoding="utf-8"))


def measure(files: dict[str, str], engine: str = "auto") -> list[dict]:
    """Entries from the engine asked for, each stamped with the engine that produced it."""
    entries = extract_javaparser(files) if engine in ("auto", "javaparser") else None
    if entries is None:
        if engine == "javaparser" and os.environ.get("HR_COMPLEXITY_STRICT"):
            raise SystemExit("[complexity] the JavaParser engine was required and did not run")
        entries = extract(files)
        for e in entries:
            e["engine"] = "regex"
    return mark_critical(entries, files)


# ── critical entry points ──────────────────────────────────────────────────────────────
# A team marks an entry point whose whole flow must not break unnoticed — payments, the
# login, the nightly settlement — by writing `@critical` in the handler's Javadoc or a
# comment above it, or by annotating it with an annotation of its own named `@Critical`
# (Victor, 21 Nov 2026). Every method the handler reaches is then critical, and a branch
# that changes any of them wears ☢️ on the page (`critical-flows.py`). The marker is read
# from the source, not configured, so it travels with the code it protects and shows up
# in `git blame` like any other line.
CRITICAL = re.compile(r"@[Cc]ritical\b")


def critical_marker(text: str, line: int, name: str) -> int | None:
    """The line of the `@critical` marker in the header of the method declared at `line`
    and named `name` — its Javadoc, comments, annotations and modifiers, from the end of the
    previous member up to the name itself — or None when it carries none."""
    code = strip(text)
    at = sum(len(l) + 1 for l in text.split("\n")[:max(line - 1, 0)])
    sig = re.compile(rf"\b{re.escape(name)}\s*\(").search(code, at)
    if not sig:
        return None
    start = max(code.rfind(c, 0, at) for c in ";{}") + 1
    hit = CRITICAL.search(text, start, sig.start())
    return text.count("\n", 0, hit.start()) + 1 if hit else None


def mark_critical(entries: list[dict], files: dict[str, str]) -> list[dict]:
    """Stamp `critical: {"file", "line"}` — where the marker stands — on every entry point
    whose handler carries one. Nothing is stamped on the others, so a snapshot from before
    this existed reads the same as one where nothing is marked."""
    for e in entries:
        head = (e.get("flow") or [{}])[0]
        path, line = head.get("file"), head.get("line")
        # `pkg.Class#name`, or `#name(String, int)` where the engine keeps overloads apart.
        name = (head.get("method") or "").rsplit("#", 1)[-1].split("(")[0].strip()
        if not (path in files and line and re.fullmatch(r"[A-Za-z_$][\w$]*", name)):
            continue
        at = critical_marker(files[path], int(line), name)
        if at:
            e["critical"] = {"file": path, "line": at}
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="measure the merge-base with this ref, not the working tree")
    ap.add_argument("--rev", help="measure this exact revision instead of the working tree")
    ap.add_argument("--out", help="where to write the JSON (default: stdout)")
    ap.add_argument("--engine", choices=("auto", "javaparser", "regex"),
                    default=os.environ.get("HR_COMPLEXITY_ENGINE", DEFAULT_ENGINE),
                    help="auto: JavaParser when a JDK is on the PATH, else regex (default: %(default)s)")
    args = ap.parse_args(argv)

    # The merge-base, never the tip of the base branch: commits that landed on main after
    # this branch started are not this branch's doing, and charging them to it is how a
    # "+4" appears next to an entry point nobody here touched.
    rev = args.rev or (run("git", "merge-base", args.base, "HEAD") if args.base else None)
    entries = measure(sources(rev), args.engine)
    body = json.dumps(entries, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        engine = entries[0]["engine"] if entries else args.engine
        print(f"[complexity] {len(entries)} entry points ({engine}) -> {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
