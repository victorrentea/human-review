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
    local has a declared type this project also declares, and `Type.foo(…)`. A call whose
    receiver has no known type resolves only when exactly one class in the project declares
    a method by that name; otherwise it is dropped rather than guessed at.
  * **`flowCc`** is the plain sum over the DISTINCT methods reached (cycles counted once).
    Summing needs no McCabe bookkeeping: straight-line code already scores 0.

Known limits, stated because the number is quoted to a reviewer: overloads collapse by
name; interface calls count every implementation whose class declares that name; runtime
code (Spring Data queries, proxies, generated mappers) is invisible past its leaf, so a
repository method scores 0; reflection is invisible; and a nesting level opened by a
brace-less `if` is not tracked. Both sides of the delta are measured by this same file, so
what the bars say the branch added stays honest even where the absolute number is low.

Usage:
    endpoint-complexity.py --out after.json                      # the working tree
    endpoint-complexity.py --base origin/main --out before.json  # the merge-base
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
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
# The parameter list tolerates one level of nesting, because annotated parameters are
# ordinary in this code — `listOwners(@RequestParam(name = "lastName") String lastName)` —
# and a declaration this misses is a whole entry point missing from the tab.
DECL = re.compile(
    rf"(?:{MODIFIER}\s+)*(?:<[^<>;{{}}]+>\s*)?(?:({TYPE})\s+)?([A-Za-z_$]\w*)\s*"
    rf"\(((?:[^;{{}}()]|\([^()]*\))*)\)\s*(?:throws\s[^{{;]*)?\{{"
)
# Every construct that reads as `name (…) {` without being a method. `for` and the
# `try (…)` of a resource block carry a `;` inside the parens and never reach this set.
NOT_A_METHOD = {"if", "else", "for", "while", "do", "switch", "case", "catch", "try", "return",
                "new", "synchronized", "assert", "yield", "instanceof", "record", "class",
                "enum", "interface", "throw", "super", "this"}

TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|@interface)\s+(\w+)")
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
# `Type name` wherever one is declared — field, parameter or local. Scoping is ignored on
# purpose: one map per file is enough to type the receiver of nearly every call, and a name
# reused for two types in one file is rarer than the calls this resolves.
VAR = re.compile(r"\b([A-Z]\w*)(?:\s*<[^<>;{}]*>)?(?:\s*\[\s*\])?\s+([a-z_$]\w*)\s*(?=[;=,):])")
CALL = re.compile(r"(?:(\w+)\s*\.\s*)?\b([A-Za-z_$]\w*)\s*\(")
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
        return {
            str(rel): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.glob("**/src/main/java/**/*.java"))
            for rel in [p.relative_to(root)]
            # `.human-review/.diffbase` holds the *base* copy of files this run is diffing;
            # measuring those too would file every handler twice, once per snapshot.
            if not any(part.startswith(".") for part in rel.parts)
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
    """Campbell's Cognitive Complexity of an already-blanked method body.

    Nesting is counted by braces: a construct that opens a block hands its level to the
    next `{`, and a `;` first cancels the hand-off, so `if (x) return;` scores its 1 without
    deepening whatever block follows it."""
    score = nesting = pending = 0
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
                score += 1
            last_bool = t
        elif t == "->":
            pending = 1  # a lambda body nests what is inside it, and costs nothing itself
            opened_by = "->"
        elif t == "?":
            # `List<?>` and a label-less `x ? a : b` are the same character; only the second
            # has an expression in front of it.
            if body[max(0, m.start() - 1)] not in "<," and body[m.end():m.end() + 1] not in ">,":
                score += 1 + nesting
        elif t == "else":
            score += 1  # the `else` of an `else if` is the whole cost of the pair
            pending, opened_by, skip_if = 1, "else", True
        elif t == "while" and skip_while:
            skip_while = False
        elif t == "if" and skip_if:
            skip_if, pending, opened_by = False, 1, "if"
        else:
            skip_if = False
            score += 1 + nesting
            if t in NESTS:
                pending, opened_by = 1, t
    return score


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
        types = {name: t for t, name in VAR.findall(code)}
        class_path = self._paths(text, code, decl.start()).get("path", "")
        for m in DECL.finditer(code, decl.end()):
            ret, name, params = m.group(1), m.group(2), m.group(3)
            if name in NOT_A_METHOD or (ret and ret.split("<")[0].strip() in NOT_A_METHOD):
                continue
            body = code[m.end() - 1:block(code, m.end() - 1)]
            key = f"{fqcn}#{name}"
            calls = {(recv, called) for recv, called in CALL.findall(body)
                     if called not in NOT_A_METHOD}
            method = self.methods.setdefault(
                key, {"key": key, "display": f"{simple}.{name}({_params(params)})",
                      "cc": 0, "calls": set(), "types": types})
            method["cc"] += cognitive(body) + (1 if any(c == name for _, c in calls) else 0)
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
        if not recv:
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
        return self._unique(called)

    def _unique(self, called: str) -> list[str]:
        keys = self.by_name.get(called, [])
        return keys if len(keys) == 1 else []

    def flow(self, key: str) -> list[str]:
        """Every method reachable from `key`, the handler first, each one once."""
        seen, queue, order = {key}, [key], []
        while queue:
            cur = queue.pop(0)
            method = self.methods.get(cur)
            if not method:
                continue
            order.append(cur)
            for recv, called in sorted(method["calls"]):
                for target in self.resolve(method, recv, called):
                    if target not in seen:
                        seen.add(target)
                        queue.append(target)
        return order


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


def extract(files: dict[str, str]) -> list[dict]:
    index = Index(files)
    out = []
    for e in index.entries:
        flow = index.flow(e["key"])
        out.append({
            "kind": e["kind"], "httpMethod": e["httpMethod"], "path": e["path"],
            "handler": e["handler"],
            "metric": "cognitive",
            "flowCc": sum(index.methods[k]["cc"] for k in flow),
            "methods": len(flow),
            "flow": [{"method": k, "cognitive": index.methods[k]["cc"]} for k in flow],
        })
    out.sort(key=lambda e: (-e["flowCc"], e["path"]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="measure the merge-base with this ref, not the working tree")
    ap.add_argument("--rev", help="measure this exact revision instead of the working tree")
    ap.add_argument("--out", help="where to write the JSON (default: stdout)")
    args = ap.parse_args(argv)

    # The merge-base, never the tip of the base branch: commits that landed on main after
    # this branch started are not this branch's doing, and charging them to it is how a
    # "+4" appears next to an entry point nobody here touched.
    rev = args.rev or (run("git", "merge-base", args.base, "HEAD") if args.base else None)
    entries = extract(sources(rev))
    body = json.dumps(entries, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"[complexity] {len(entries)} entry points -> {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
