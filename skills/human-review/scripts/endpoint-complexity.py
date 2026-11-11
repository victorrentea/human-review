#!/usr/bin/env python3
"""Cognitive complexity of the whole flow behind every entry point of a Python service.

`endpoint-complexity-delta.py` draws the derivative — which entry points this branch made
heavier. This writes one side of it: every entry point, and the summed Cognitive Complexity
of every function reachable from it.

**Entry points** are what the service's Serverless Framework files deploy
(`serverless*.yml`, `functions:`): each function's `handler:` and its events —

  * `http`                        -> kind `http`, the method and path API Gateway serves;
  * `sqs`, `sns`, `eventBridge`,
    `stream`, `s3`                -> kind `listener`, the event source;
  * `schedule`                    -> kind `job`;
  * no event at all               -> kind `invoke` (another Lambda calls it).

A handler module that routes with aws_lambda_powertools' `APIGatewayRestResolver`
(`@app.get("/x")`) is one entry point per route, not one per Lambda.

**The score** is Cognitive Complexity (G. Ann Campbell / SonarSource), read off Python's
own `ast`: `+1 + nesting` per `if` / loop / `except` / `match` / conditional expression,
`+1` per `elif` and `else`, `+1` per run of like boolean operators, `+1` for recursion; a
nested function or lambda deepens the nesting of what is inside it.

**The flow** is every project function reachable from the handler through calls this file
can resolve: a name defined or imported in the module, `self.x(…)` within the class,
`Class.x(…)`, `module.x(…)`, and a call on a value whose class is known — a module-level or
local instance (`svc = Service(…)`), an instance attribute set in `__init__`, an annotated
parameter, or `container.resolve(Service)`. A call on a value of unknown class resolves
only when exactly one project function has that name. Library calls are not followed.
`flowCc` is the plain sum over the distinct functions reached.

Both sides of the delta are measured by this same file, so what the bars say the branch
added stays honest where the absolute number is approximate.

Usage:
    endpoint-complexity.py --out after.json                         # the working tree
    endpoint-complexity.py --base origin/develop --out before.json  # the merge-base
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

#: Folders whose Python is the service's own code. Tests, migrations and infrastructure
#: code are not on any request's path.
SKIP_PARTS = {"tests", "test", "alembic", "cdk", "initial-stack", "node_modules", ".venv",
              "venv", "__pycache__", ".serverless", ".human-review", "scripts", "migrations", "snippets"}

#: Serverless event -> (kind, the verb column's word)
EVENT_KIND = {"sqs": ("listener", "SQS"), "sns": ("listener", "SNS"),
              "eventBridge": ("listener", "EVENT"), "stream": ("listener", "STREAM"),
              "s3": ("listener", "S3"), "schedule": ("job", "CRON"),
              "websocket": ("http", "WS"), "alb": ("http", "ALB")}

ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}

MAX_LINE = 160

#: Method names a value of unknown class most likely got from Python or a library rather
#: than from the project: every method of the builtin types and of `datetime`, and the
#: verbs of a SQLAlchemy session or query and of a boto3 client. A call like `rows.append(x)`
#: is never bound to the one project function that happens to be called `append` —
#: measured on activities-backend, a third of all guessed edges were exactly that.
def _library_names() -> frozenset:
    import datetime
    names = set()
    for t in (list, dict, str, set, frozenset, tuple, bytes, int, float, object,
              datetime.datetime, datetime.date, datetime.timedelta):
        names |= {n for n in dir(t) if not n.startswith("_")}
    names |= {"add", "add_all", "all", "close", "commit", "delete", "execute", "filter",
              "filter_by", "first", "flush", "get", "join", "limit", "merge", "offset", "one",
              "one_or_none", "options", "order_by", "query", "refresh", "rollback", "scalar",
              "scalars", "select_from", "where", "with_for_update", "begin", "begin_nested",
              "get_item", "put_item", "update_item", "delete_item", "send_message",
              "receive_message", "publish", "put_events", "invoke", "get_object", "put_object",
              "client", "resource", "Table", "loads", "dumps", "json", "dict", "model_dump",
              "now", "utcnow", "today", "isoformat", "strftime", "strptime", "info", "debug",
              "warning", "error", "exception", "critical", "log", "set", "options"}
    return frozenset(names)


LIBRARY_NAMES = _library_names()


# ── reading the tree ───────────────────────────────────────────────────────────────────

def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def wanted(path: str) -> bool:
    parts = Path(path).parts
    return path.endswith(".py") and not any(p in SKIP_PARTS for p in parts) \
        and not Path(path).name.startswith("test_") and Path(path).name != "conftest.py"


def read_tree(base: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """`({path: python source}, {path: serverless yaml})`, at `base` or in the working tree."""
    if base:
        commit = git("merge-base", base, "HEAD").strip() or base
        names = git("ls-tree", "-r", "--name-only", commit).splitlines()
        read = lambda p: git("show", f"{commit}:{p}")
    else:
        names = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        read = lambda p: Path(p).read_text(encoding="utf-8", errors="replace") \
            if Path(p).is_file() else ""
    py = {p: read(p) for p in names if wanted(p)}
    sls = {p: read(p) for p in names
           if re.fullmatch(r"serverless(?:\.[\w-]+)?\.ya?ml", Path(p).name) and "/" not in p}
    return py, sls


# ── serverless.yml ─────────────────────────────────────────────────────────────────────

def _yaml_loader():
    import yaml

    class Loader(yaml.SafeLoader):
        pass
    # CloudFormation's short forms (`!Ref`, `!GetAtt`, `!Sub`…) are opaque values here.
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml, Loader


def serverless_functions(files: dict[str, str]) -> list[dict]:
    """`[{name, handler, events}]` from every `serverless*.yml` at the repository root."""
    yaml, Loader = _yaml_loader()
    out = []
    for path, text in sorted(files.items()):
        try:
            doc = yaml.load(text, Loader=Loader) or {}
        except yaml.YAMLError as e:
            print(f"[complexity] {path}: {e}", file=sys.stderr)
            continue
        fns = doc.get("functions") if isinstance(doc, dict) else None
        if not isinstance(fns, dict):
            continue
        for name, fn in fns.items():
            if isinstance(fn, dict) and isinstance(fn.get("handler"), str):
                out.append({"name": name, "handler": fn["handler"],
                            "events": [e for e in fn.get("events") or [] if isinstance(e, dict)]})
    return out


def handler_ref(handler: str) -> tuple[str, str]:
    """`src/handlers/caching.handler` or `src.handlers.caching.handler` -> (module, name)."""
    mod, _, name = handler.rpartition(".")
    return mod.replace("/", ".").strip("."), name


def event_entry(fn: dict, ev: dict) -> tuple[str, str, str] | None:
    """(kind, verb, path) for one Serverless event."""
    (key, val), = list(ev.items())[:1] or [(None, None)]
    if key in ("http", "httpApi"):
        if isinstance(val, str):                     # `http: GET /x`
            verb, _, path = val.partition(" ")
        else:
            val = val or {}
            verb, path = str(val.get("method", "ANY")), str(val.get("path", ""))
        return "http", verb.upper(), "/" + path.strip().lstrip("/")
    kind, verb = EVENT_KIND.get(key, ("listener", str(key).upper()))
    source = None
    if isinstance(val, dict):
        source = val.get("arn") or val.get("queueName") or val.get("topicName") \
            or val.get("rate") or val.get("pattern")
        if isinstance(source, dict):
            source = None
    elif isinstance(val, str):
        source = val
    label = fn["name"]
    if isinstance(source, str) and source and "${" not in source:
        label = f"{fn['name']} ({source.rsplit(':', 1)[-1][:60]})"
    return kind, verb, label


# ── the program: functions, classes, calls ───────────────────────────────────────────────

def module_of(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Func:
    def __init__(self, key, display, path, node, cls=None, module=None):
        self.key, self.display, self.path, self.node = key, display, path, node
        self.cls, self.module = cls, module
        self.cc, self.cyc, self.hits = 0, 1, []
        self.calls: set[str] = set()


class Program:
    """Every function and method of the service, and what each one calls."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.lines = {p: s.splitlines() for p, s in files.items()}
        self.funcs: dict[str, Func] = {}
        self.by_name: dict[str, list[str]] = {}
        self.classes: dict[str, dict] = {}          # "mod.Class" -> {methods, attrs, path}
        self.modules: dict[str, dict] = {}          # "mod" -> {path, tree, imports, defs, vars}
        for path, src in files.items():
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            self.modules[module_of(path)] = {"path": path, "tree": tree}
        for mod, m in self.modules.items():
            self._index(mod, m)
        # Only once every module is indexed: an attribute's class may live in any of them.
        for mod in self.modules:
            self._attributes(mod)
        for f in self.funcs.values():
            self._score(f)
        for f in self.funcs.values():
            f.calls = self._calls(f)

    # -- indexing --

    def _index(self, mod: str, m: dict) -> None:
        tree, path = m["tree"], m["path"]
        m["imports"], m["defs"], m["vars"], m["routes"] = {}, {}, {}, []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:
                    base = ".".join(mod.split(".")[:-node.level] + ([node.module] if node.module else []))
                for a in node.names:
                    m["imports"][a.asname or a.name] = f"{base}.{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    m["imports"][a.asname or a.name.split(".")[0]] = a.name if a.asname \
                        else a.name.split(".")[0]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add(f"{mod}#{node.name}", f"{node.name}()", path, node, None, mod)
                m["defs"][node.name] = f"{mod}#{node.name}"
                for d in node.decorator_list:
                    route = self._route(d)
                    if route:
                        m["routes"].append((*route, f"{mod}#{node.name}"))
            elif isinstance(node, ast.ClassDef):
                qual = f"{mod}.{node.name}"
                c = {"methods": {}, "attrs": {}, "path": path, "module": mod,
                     "bases": [self._dotted(b) for b in node.bases]}
                self.classes[qual] = c
                m["defs"][node.name] = qual
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        key = f"{qual}#{item.name}"
                        self._add(key, f"{node.name}.{item.name}()", path, item, qual, mod)
                        c["methods"][item.name] = key
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and node.value is not None:
                        m["vars"][t.id] = node.value
    def _attributes(self, mod: str) -> None:
        """Instance attributes, from `self.x = …` in `__init__`."""
        for qual, c in self.classes.items():
            init = self.funcs.get(c["methods"].get("__init__", ""))
            if c["module"] != mod or not init:
                continue
            params = self._param_types(init.node, mod)
            for sub in ast.walk(init.node):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    for t in targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                                and t.value.id == "self" and sub.value is not None:
                            typ = self._type_of(sub.value, mod, params)
                            if typ:
                                c["attrs"][t.attr] = typ

    def _add(self, key, display, path, node, cls, mod):
        f = Func(key, display, path, node, cls, mod)
        self.funcs[key] = f
        self.by_name.setdefault(node.name, []).append(key)

    @staticmethod
    def _dotted(node) -> str | None:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    def _route(self, deco) -> tuple[str, str] | None:
        """`@app.get("/x")` / `@app.route("/x", method=["POST"])` -> (VERB, path)."""
        if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
            return None
        verb = deco.func.attr
        path = deco.args[0].value if deco.args and isinstance(deco.args[0], ast.Constant) \
            and isinstance(deco.args[0].value, str) else None
        if path is None:
            return None
        if verb in ROUTE_VERBS:
            return verb.upper(), path
        if verb == "route":
            for k in deco.keywords:
                if k.arg == "method":
                    v = k.value
                    if isinstance(v, ast.Constant):
                        return str(v.value).upper(), path
                    if isinstance(v, (ast.List, ast.Tuple)) and v.elts \
                            and isinstance(v.elts[0], ast.Constant):
                        return "/".join(str(e.value).upper() for e in v.elts
                                        if isinstance(e, ast.Constant)), path
            return "GET", path
        return None

    # -- what a name or an expression is --

    def resolve_name(self, name: str, mod: str) -> str | None:
        """A bare name in `mod` -> a function key or a class qualname of the project."""
        m = self.modules.get(mod) or {}
        if name in m.get("defs", {}):
            return m["defs"][name]
        target = m.get("imports", {}).get(name)
        if target:
            tmod, _, tname = target.rpartition(".")
            tm = self.modules.get(tmod)
            if tm and tname in tm.get("defs", {}):
                return tm["defs"][tname]
            if target in self.modules:                 # an imported module
                return "module:" + target
        return None

    def _class_of(self, expr, mod: str) -> str | None:
        """The project class an annotation or a constructor names."""
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                expr = ast.parse(expr.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(expr, ast.Subscript):             # Optional[Foo]
            return self._class_of(expr.slice, mod)
        if isinstance(expr, ast.BinOp):                 # Foo | None
            return self._class_of(expr.left, mod) or self._class_of(expr.right, mod)
        name = self._dotted(expr)
        if not name:
            return None
        head, _, rest = name.partition(".")
        got = self.resolve_name(head, mod)
        if got and got.startswith("module:") and rest:
            tm = self.modules.get(got[7:]) or {}
            got = tm.get("defs", {}).get(rest)
        return got if got in self.classes else None

    def _type_of(self, value, mod: str, local: dict) -> str | None:
        """The project class a value is an instance of, when the expression says."""
        if isinstance(value, ast.Name):
            return local.get(value.id)
        if isinstance(value, ast.BoolOp):            # `repo or Repo()`
            return next(filter(None, (self._type_of(v, mod, local) for v in value.values)), None)
        if isinstance(value, ast.IfExp):
            return self._type_of(value.body, mod, local) or self._type_of(value.orelse, mod, local)
        if isinstance(value, ast.Call):
            cls = self._class_of(value.func, mod)
            if cls:
                return cls
            # `container.resolve(Service)` / `.get(Service)`
            if isinstance(value.func, ast.Attribute) and value.func.attr in ("resolve", "get") \
                    and value.args:
                return self._class_of(value.args[0], mod)
        return None

    def _param_types(self, fn, mod: str) -> dict:
        out = {}
        args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        for a in args:
            if a.annotation is not None:
                cls = self._class_of(a.annotation, mod)
                if cls:
                    out[a.arg] = cls
        return out

    def _method(self, cls: str, name: str, seen=None) -> str | None:
        """`cls.name`, looked up through the project's own base classes."""
        seen = seen or set()
        c = self.classes.get(cls)
        if not c or cls in seen:
            return None
        seen.add(cls)
        if name in c["methods"]:
            return c["methods"][name]
        for b in c["bases"]:
            base = self._class_of(ast.parse(b, mode="eval").body, c["module"]) if b else None
            if base:
                got = self._method(base, name, seen)
                if got:
                    return got
        return None

    # -- the calls of one function --

    def _calls(self, f: Func) -> set[str]:
        mod = f.module
        m = self.modules[mod]
        local, foreign = {}, set()
        for name, value in m.get("vars", {}).items():
            typ = self._type_of(value, mod, {})
            if typ:
                local[name] = typ
            elif isinstance(value, ast.Call):
                foreign.add(name)            # `app = APIGatewayRestResolver()`: a library's
        local.update(self._param_types(f.node, mod))
        # The body only: a decorator (`@app.get("/x")`) is not a call the function makes.
        body = [n for stmt in f.node.body for n in ast.walk(stmt)]
        # assignments in the body, in order: `svc = Service(…)`
        for sub in body:
            if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                typ = (self._class_of(sub.annotation, mod) if isinstance(sub, ast.AnnAssign)
                       else None) or (self._type_of(sub.value, mod, local)
                                      if sub.value is not None else None)
                for t in targets:
                    if isinstance(t, ast.Name) and typ:
                        local[t.id] = typ
        out = set()
        for sub in body:
            target = None
            if isinstance(sub, ast.Call):
                target = self._callee(sub.func, f, local, foreign)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                # A function handed on as a value (`map(fn, …)`, `app.handler(fn)`).
                got = self.resolve_name(sub.id, mod)
                target = got if got in self.funcs else None
            if target and target != f.key:
                out.add(target)
        return out

    def _callee(self, func, f: Func, local: dict, foreign=frozenset()) -> str | None:
        mod = f.module
        if isinstance(func, ast.Name):
            got = self.resolve_name(func.id, mod)
            if got in self.funcs:
                return got
            if got in self.classes:
                return self._method(got, "__init__")
            return None
        if not isinstance(func, ast.Attribute):
            return None
        name, recv = func.attr, func.value
        cls = None
        if isinstance(recv, ast.Name):
            if recv.id in ("self", "cls") and f.cls:
                cls = f.cls
            elif recv.id in local:
                cls = local[recv.id]
            else:
                got = self.resolve_name(recv.id, mod)
                if got in self.classes:
                    cls = got
                elif got and got.startswith("module:"):
                    tm = self.modules.get(got[7:]) or {}
                    hit = tm.get("defs", {}).get(name)
                    return hit if hit in self.funcs else (
                        self._method(hit, "__init__") if hit in self.classes else None)
        elif isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name) \
                and recv.value.id == "self" and f.cls:
            cls = (self.classes.get(f.cls) or {}).get("attrs", {}).get(recv.attr)
        elif isinstance(recv, ast.Call):
            cls = self._type_of(recv, mod, local)
        if cls:
            return self._method(cls, name)
        if isinstance(recv, ast.Name) and (recv.id in foreign or self.resolve_name(recv.id, mod) is None
                                           and recv.id in (self.modules[mod].get("imports") or {})):
            return None                      # a library object, or a library module
        # Unknown receiver: only a name exactly one project function carries, and never
        # one every list, dict, session or client also answers to.
        if name in LIBRARY_NAMES:
            return None
        cands = [k for k in self.by_name.get(name, ()) if not name.startswith("__")]
        return cands[0] if len(cands) == 1 else None

    # -- the score --

    def _score(self, f: Func) -> None:
        path, lines = f.path, self.lines[f.path]
        name = f.node.name

        def hit(node, inc, why):
            ln = getattr(node, "lineno", f.node.lineno)
            src = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
            f.hits.append({"file": path, "line": ln,
                           "code": src[:MAX_LINE] + ("…" if len(src) > MAX_LINE else ""),
                           "inc": inc, "why": why})
            f.cc += inc

        def bools(expr):
            """+1 per run of like operators in a boolean expression tree."""
            runs = 0

            def walk(e, parent_op=None):
                nonlocal runs
                if isinstance(e, ast.BoolOp):
                    if type(e.op) is not parent_op:
                        runs += 1
                    for v in e.values:
                        walk(v, type(e.op))
                elif isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
                    walk(e.operand, None)
            walk(expr)
            return runs

        def cond(node, test, nesting, why):
            hit(node, 1 + nesting, why if not nesting else f"{why} (nesting {nesting})")
            f.cyc += 1
            b = bools(test) if test is not None else 0
            if b:
                hit(test, b, "&& / || sequence" if b == 1 else f"{b} boolean sequences")
                f.cyc += b

        def visit(node, nesting):
            for child in ast.iter_child_nodes(node):
                visit_one(child, nesting)

        def visit_if(node, nesting, elif_=False):
            if elif_:
                hit(node, 1, "elif")
                f.cyc += 1
                b = bools(node.test)
                if b:
                    hit(node.test, b, "&& / || sequence")
                    f.cyc += b
            else:
                cond(node, node.test, nesting, "if")
            for s in node.body:
                visit_one(s, nesting + 1)
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                visit_if(node.orelse[0], nesting, elif_=True)
            elif node.orelse:
                hit(node.orelse[0], 1, "else")
                for s in node.orelse:
                    visit_one(s, nesting + 1)

        def visit_one(node, nesting):
            if isinstance(node, ast.If):
                visit_if(node, nesting)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                cond(node, getattr(node, "test", None), nesting,
                     "while" if isinstance(node, ast.While) else "for")
                for s in node.body:
                    visit_one(s, nesting + 1)
                if node.orelse:
                    hit(node.orelse[0], 1, "loop else")
                    for s in node.orelse:
                        visit_one(s, nesting + 1)
            elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                for s in node.body:
                    visit_one(s, nesting)
                for h in node.handlers:
                    cond(h, None, nesting, "except")
                    for s in h.body:
                        visit_one(s, nesting + 1)
                for s in node.orelse + node.finalbody:
                    visit_one(s, nesting)
            elif isinstance(node, ast.IfExp):
                cond(node, node.test, nesting, "conditional expression")
                visit(node, nesting + 1)
            elif isinstance(node, ast.Match):
                cond(node, None, nesting, "match")
                for case in node.cases:
                    for s in case.body:
                        visit_one(s, nesting + 1)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(node, nesting + 1)
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for g in node.generators:
                    for c in g.ifs:
                        cond(c, c, nesting, "comprehension if")
                visit(node, nesting)
            elif isinstance(node, ast.BoolOp):
                b = bools(node)
                hit(node, b, "&& / || sequence")
                f.cyc += b
            else:
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == name and f.cls is None:
                    hit(node, 1, "recursion")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == name and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "self":
                    hit(node, 1, "recursion")
                visit(node, nesting)

        for s in f.node.body:
            visit_one(s, 0)

    # -- the flow --

    def flow(self, key: str) -> list[str]:
        seen, order, stack = set(), [], [key]
        while stack:
            k = stack.pop()
            if k in seen or k not in self.funcs:
                continue
            seen.add(k)
            order.append(k)
            stack.extend(sorted(self.funcs[k].calls - seen, reverse=True))
        return order


# ── entry points ───────────────────────────────────────────────────────────────────────

def entries(prog: Program, functions: list[dict]) -> list[dict]:
    out = []
    for fn in functions:
        mod, name = handler_ref(fn["handler"])
        m = prog.modules.get(mod)
        if not m:
            continue
        key = m.get("defs", {}).get(name)
        synthetic = None
        if key not in prog.funcs:
            value = m.get("vars", {}).get(name)        # `handler = app.handler(do_work)`
            if value is None:
                continue
            synthetic = f"{mod}#{name}"
            node = ast.FunctionDef(name=name, args=ast.arguments(
                posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=[ast.Expr(value)], decorator_list=[], lineno=value.lineno)
            f = Func(synthetic, f"{name}", m["path"], node, None, mod)
            prog.funcs[synthetic] = f
            f.calls = prog._calls(f)
            key = synthetic
        routes = m.get("routes") or []
        events = fn["events"] or [None]
        for ev in events:
            if routes and ev and next(iter(ev)) in ("http", "httpApi"):
                for verb, path, rkey in routes:
                    out.append({"kind": "http", "httpMethod": verb, "path": path, "key": rkey})
                continue
            kind, verb, path = event_entry(fn, ev) if ev else ("invoke", "INVOKE", fn["name"])
            out.append({"kind": kind, "httpMethod": verb, "path": path, "key": key})
    seen, unique = set(), []
    for e in out:
        k = (e["kind"], e["httpMethod"], e["path"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return unique


def extract(py: dict[str, str], sls: dict[str, str]) -> list[dict]:
    prog = Program(py)
    out = []
    for e in entries(prog, serverless_functions(sls)):
        flow = prog.flow(e["key"])
        in_flow = set(flow)
        head = prog.funcs[e["key"]]
        out.append({
            "kind": e["kind"], "httpMethod": e["httpMethod"], "path": e["path"],
            "handler": f"{head.module}.{head.display}",
            "metric": "cognitive",
            "flowCc": sum(prog.funcs[k].cc for k in flow),
            "methods": len(flow),
            "flow": [{"method": k, "display": prog.funcs[k].display,
                      "file": prog.funcs[k].path, "line": prog.funcs[k].node.lineno,
                      "cognitive": prog.funcs[k].cc, "cyclomatic": prog.funcs[k].cyc,
                      "calls": sorted(t for t in prog.funcs[k].calls if t in in_flow),
                      "hits": prog.funcs[k].hits} for k in flow],
        })
    out.sort(key=lambda e: (-e["flowCc"], e["path"]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="measure the merge-base with this ref, not the working tree")
    ap.add_argument("--out", help="where to write the JSON (default: stdout)")
    args = ap.parse_args(argv)
    py, sls = read_tree(args.base)
    if not sls:
        print("[complexity] no serverless*.yml at the repository root — no entry points",
              file=sys.stderr)
    found = extract(py, sls)
    body = json.dumps(found, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"[complexity] {len(found)} entry points -> {args.out}", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
