#!/usr/bin/env python3
"""
Structural extractor for Python logging statements — the Logging tab's scan.

Parsed with Python's own `ast`, not grepped: `logger.info(...)` is a hit and
`math.log(x)` or `self.log_entry.info` on a dataclass is not, and only a syntax tree
plus a table of which names really are loggers can tell them apart.

  Pass 1  per file, the names bound to a logger: `Logger(...)` from
          aws_lambda_powertools, `logging.getLogger(...)`, `structlog.get_logger()`,
          loguru's `logger`, a class attribute or `self.x` assigned one, a parameter
          annotated `Logger`, and a logger *imported* from another scanned module
          (`from src.shared.log import logger`) — resolved through that module's own table.
  Pass 2  every call `<receiver>.<level>(...)` whose receiver is one of those names, plus
          the root logger through the module itself (`logging.info(...)`).

`print(...)`, `traceback.print_exc()` and writes to `sys.stdout` / `sys.stderr` are
collected separately as console output and never counted as logging.

The JSON it writes is the one the Logging tab has always read (`all` / `changed`, each
`logging` + `antipattern`, and `stats`): the scanner was rewritten for Ludvig's Python
services when the fork dropped Java, the tab was not.

Dependency-free: stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

#: What the scan looks for, as the Logging tab's hover lists it. The tab reads this tuple
#: rather than typing its own list, so the page can never claim a library the scan does
#: not actually recognise.
LIBRARIES = ("logging", "aws_lambda_powertools", "structlog", "loguru")

#: A call on a logger is a log statement when its method is one of these.
LEVEL_OF_METHOD = {
    "debug": "DEBUG", "info": "INFO", "warning": "WARN", "warn": "WARN",
    "error": "ERROR", "exception": "ERROR", "critical": "FATAL", "fatal": "FATAL",
    "log": "LOG",
}

#: Names a logger is conventionally bound to. A name imported from a module outside the
#: scan (`from ludvig_shared.tools import logger`) is taken as a logger on its name alone,
#: and says so (`heuristic`).
INHERITED_NAMES = {"log", "LOG", "logger", "LOGGER", "_log", "_logger", "Logger"}

#: Where a Ludvig service keeps its code, in the order they are looked for: activities-backend
#: (`src`, `lambdas`), mitt-ludvig (`backend`), pog-backend-api (`src_py`). Tests are left
#: out on purpose — a log line in a test never reaches CloudWatch.
DEFAULT_PATHS = ("src", "lambdas", "backend", "src_py")


def default_paths(root) -> list[str]:
    """The folders to scan when nothing is configured: those of `DEFAULT_PATHS` that exist
    under `root`, else the whole checkout."""
    return [p for p in DEFAULT_PATHS if (Path(root) / p).is_dir()] or ["."]


#: Folders never walked: dependencies, build output, caches.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".serverless",
             ".mypy_cache", ".pytest_cache", "build", "dist", "cdk.out", ".human-review"}

FLAVOUR_POWERTOOLS = "Powertools"
FLAVOUR_STDLIB = "logging"
FLAVOUR_STRUCTLOG = "structlog"
FLAVOUR_LOGURU = "loguru"

# Where a scope-less statement sits: a module-level call has no enclosing function.


@dataclass
class Hit:
    file: str            # path relative to root
    abs_file: str
    line: int            # 1-based
    column: int          # 1-based
    end_line: int
    level: str
    receiver: str        # the receiver as written, e.g. "logger" or "self.logger"
    flavour: str         # Powertools / logging / structlog / loguru / imported logger
    evidence: str        # how the receiver was proven to be a logger
    confidence: str      # "declared" | "imported" | "heuristic"
    method: str
    format: str | None   # the message argument, if it is a string or f-string literal
    args: list           # the other arguments, as source (keywords as `name=value`)
    raw_line: str        # exact source line
    text: str            # exact call expression
    method_start: int | None  # enclosing function, 1-based inclusive range, or None at
    method_end: int | None    # module level
    # "Data flow to here": one `{line, name, kind, text}` per value the statement logs
    # that was bound earlier in the same function (an assignment or a parameter).
    origins: list = field(default_factory=list)
    # One entry per `args` item. Python carries no declared types to read in general, so
    # this is an annotation where one is written (`order_id: str`) and None otherwise.
    arg_types: list = field(default_factory=list)
    # What Ludvig's logging standard says to look at twice (`concerns_for`): a value whose
    # name says personal data, a whole payload logged, a message built by an f-string.
    concerns: list = field(default_factory=list)
    change: str = ""      # under --since: "added" or "modified"
    was: str = ""
    same_output: bool = False


@dataclass
class AntiHit:
    file: str
    abs_file: str
    line: int
    column: int
    kind: str            # print / print_exc / sys.stdout / sys.stderr
    raw_line: str
    text: str


@dataclass
class LoggerSymbol:
    name: str
    flavour: str
    evidence: str
    confidence: str
    line: int = 0


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #

def python_files(paths: Iterable[str]) -> list[str]:
    out = []
    for p in paths:
        if os.path.isfile(p):
            if p.endswith(".py"):
                out.append(p)
            continue
        for d, dirs, files in os.walk(p):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            out += [os.path.join(d, f) for f in sorted(files) if f.endswith(".py")]
    return sorted(set(out))


def module_name(path: str, root: str) -> str | None:
    """`src/shared/log.py` -> `src.shared.log`; `src/shared/__init__.py` -> `src.shared`."""
    try:
        rel = os.path.relpath(os.path.realpath(path), root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    parts = rel[:-3].split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


# --------------------------------------------------------------------------- #
# pass 1: which names are loggers
# --------------------------------------------------------------------------- #

class Imports:
    """What one module imported, as the names it can be reached by."""

    def __init__(self, tree: ast.Module):
        self.modules: dict[str, str] = {}      # local name -> module (`import logging as l`)
        self.names: dict[str, tuple[str, str]] = {}   # local name -> (module, original)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.modules[a.asname or a.name.split(".")[0]] = \
                        a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    self.names[a.asname or a.name] = (node.module, a.name)

    def module_of(self, name: str) -> str | None:
        return self.modules.get(name)

    def origin(self, name: str) -> tuple[str, str] | None:
        return self.names.get(name)


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def factory_flavour(call: ast.AST, imp: Imports) -> tuple[str, str] | None:
    """`(flavour, how)` when `call` creates a logger, else None."""
    if not isinstance(call, ast.Call):
        return None
    name = _dotted(call.func)
    if not name:
        return None
    head, _, last = name.rpartition(".")
    if head:
        mod = imp.module_of(head.split(".")[0])
        full = (mod + head[len(head.split(".")[0]):]) if mod else head
        if full.startswith("aws_lambda_powertools") and last == "Logger":
            return FLAVOUR_POWERTOOLS, f"`{name}(…)` from aws_lambda_powertools"
        if full == "logging" and last in ("getLogger", "Logger", "LoggerAdapter"):
            return FLAVOUR_STDLIB, f"`{name}(…)` from the logging module"
        if full == "structlog" and last in ("get_logger", "getLogger", "wrap_logger"):
            return FLAVOUR_STRUCTLOG, f"`{name}(…)` from structlog"
        return None
    origin = imp.origin(name)
    if not origin:
        return None
    mod, orig = origin
    if mod.startswith("aws_lambda_powertools") and orig == "Logger":
        return FLAVOUR_POWERTOOLS, f"`{name}(…)`, aws_lambda_powertools' Logger"
    if mod == "logging" and orig in ("getLogger", "Logger", "LoggerAdapter"):
        return FLAVOUR_STDLIB, f"`{name}(…)` from the logging module"
    if mod == "structlog" and orig in ("get_logger", "getLogger", "wrap_logger"):
        return FLAVOUR_STRUCTLOG, f"`{name}(…)` from structlog"
    return None


def annotation_flavour(ann: ast.AST | None, imp: Imports) -> str | None:
    """The flavour of a parameter or attribute annotated with a logger type."""
    if ann is None:
        return None
    name = _dotted(ann) or (ann.value if isinstance(ann, ast.Constant)
                            and isinstance(ann.value, str) else None)
    if not name:
        return None
    last = name.rsplit(".", 1)[-1]
    if last not in ("Logger", "LoggerAdapter", "BoundLogger"):
        return None
    origin = imp.origin(name.split(".")[0])
    mod = origin[0] if origin else imp.module_of(name.split(".")[0]) or ""
    if mod.startswith("aws_lambda_powertools"):
        return FLAVOUR_POWERTOOLS
    if mod.startswith("structlog"):
        return FLAVOUR_STRUCTLOG
    return FLAVOUR_STDLIB


class FileScan:
    """One module: its tree, its imports, its logger symbols, its functions."""

    def __init__(self, path: str, root: str):
        self.path = path
        self.abs = os.path.realpath(path)
        self.root = root
        self.source = Path(path).read_text(encoding="utf-8", errors="replace")
        self.lines = self.source.splitlines()
        self.tree = ast.parse(self.source, filename=path)
        self.imports = Imports(self.tree)
        self.module = module_name(path, root)
        self.symbols: dict[str, LoggerSymbol] = {}
        self._functions: list[tuple[int, int, ast.AST]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._functions.append((node.lineno, node.end_lineno or node.lineno, node))
        self._functions.sort(key=lambda t: (t[0], -t[1]))

    def rel(self) -> str:
        return os.path.relpath(self.abs, self.root) if self.root else self.path

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""

    def segment(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or ""

    def enclosing(self, line: int) -> ast.AST | None:
        best = None
        for start, end, node in self._functions:
            if start <= line <= end and (best is None or start >= best.lineno):
                best = node
        return best

    def declare(self, name: str, flavour: str, evidence: str, confidence: str, line: int):
        self.symbols.setdefault(name, LoggerSymbol(name, flavour, evidence, confidence, line))

    def collect_declared(self) -> None:
        """Names this module binds to a logger it creates itself."""
        imp = self.imports
        # loguru's `logger` is an instance, imported ready-made.
        for local, (mod, orig) in imp.names.items():
            if mod == "loguru" and orig == "logger":
                self.declare(local, FLAVOUR_LOGURU, "loguru's `logger`, imported", "declared", 0)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    target, value, ann = _assignment(stmt)
                    if target is None:
                        continue
                    fl = factory_flavour(value, imp) or (
                        (annotation_flavour(ann, imp), "annotated") if annotation_flavour(ann, imp)
                        else None)
                    if fl and isinstance(target, ast.Name):
                        ev = f"class attribute `{node.name}.{target.id}` = {fl[1]}"
                        for spelt in (f"self.{target.id}", f"cls.{target.id}",
                                      f"{node.name}.{target.id}"):
                            self.declare(spelt, fl[0], ev, "declared", stmt.lineno)
            target, value, ann = _assignment(node)
            if target is not None:
                fl = factory_flavour(value, imp)
                af = annotation_flavour(ann, imp)
                if fl or af:
                    name = _dotted(target)
                    if name:
                        ev = (f"`{name}` = {fl[1]}" if fl
                              else f"`{name}` is annotated as a logger")
                        self.declare(name, fl[0] if fl else af, ev, "declared", node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                    af = annotation_flavour(a.annotation, imp)
                    if af:
                        self.declare(a.arg, af, f"parameter `{a.arg}` is annotated as a logger",
                                     "declared", node.lineno)

    def resolve_imports(self, by_module: dict[str, "FileScan"], allow_inherited: bool) -> None:
        """Names imported from another module that binds them to a logger."""
        for local, (mod, orig) in self.imports.names.items():
            if local in self.symbols:
                continue
            other = by_module.get(mod)
            sym = other.symbols.get(orig) if other else None
            if sym:
                self.declare(local, sym.flavour,
                             f"imported from `{mod}`, where {sym.evidence}", "imported", 0)
            elif allow_inherited and orig in INHERITED_NAMES and mod not in ("logging",):
                self.declare(local, "imported logger",
                             f"`{orig}` imported from `{mod}`, outside the scan — "
                             "taken as a logger by its name", "heuristic", 0)


def _assignment(node: ast.AST):
    """`(target, value, annotation)` for a one-target assignment, else (None, None, None)."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0], node.value, None
    if isinstance(node, ast.AnnAssign):
        return node.target, node.value, node.annotation
    return None, None, None


# --------------------------------------------------------------------------- #
# pass 2: the calls
# --------------------------------------------------------------------------- #

def _root_logger(recv: str, imp: Imports) -> bool:
    """`logging.info(...)`: the root logger, through the module itself."""
    return imp.module_of(recv) == "logging"


def _values_named(nodes: Iterable[ast.AST]) -> list[str]:
    """The bare names a set of expressions reads, in order, once each."""
    seen, out = set(), []
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Name) and sub.id not in seen:
                seen.add(sub.id)
                out.append(sub.id)
    return out


#: How many origin lines one statement may pull in.
MAX_ORIGIN_LINES = 6


def origins_for(scan: FileScan, func: ast.AST | None, line: int, names: list[str]) -> list:
    """Where each name the statement logs was last bound before it, inside its function:
    an assignment (`local`), a loop or `with` target (`local`), or a parameter (`param`)."""
    if func is None or not names:
        return []
    params = {a.arg for a in func.args.args + func.args.kwonlyargs + func.args.posonlyargs}
    binds: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for node in ast.walk(func):
        if getattr(node, "lineno", line) >= line:
            continue
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    stmt_line = getattr(node, "lineno", None) or getattr(t, "lineno", 0)
                    binds[sub.id].append((stmt_line, scan.line(stmt_line).strip()))
    out, seen = [], set()
    for name in names:
        if len(out) >= MAX_ORIGIN_LINES:
            break
        if binds.get(name):
            oline, text = max(binds[name])
            kind = "local"
        elif name in params:
            oline, text = func.lineno, scan.line(func.lineno).strip()
            kind = "param"
        else:
            continue
        if oline == line or oline in seen:
            continue
        seen.add(oline)
        out.append({"line": oline, "name": name, "kind": kind, "text": text})
    return out


# --------------------------------------------------------------------------- #
# what Ludvig's standard asks of a log line (ludvig-claude STANDARDS.md):
#   "No PII in logs where avoidable. An organisation number can be a personal number."
#   "Log identifying info … Use one consistent (JSON) format per log stream."
# Read off the names the statement logs — a variable, an attribute, a dict key, a
# `.get("…")` — never off runtime values, so a hit is a question for the reviewer, not a
# verdict.
# --------------------------------------------------------------------------- #

PII_NAME = re.compile(
    r"(?:^|_)(?:e_?mail|phone|mobile|telephone|personnummer|personal_?(?:number|id|no)|ssn|"
    r"social_?security|first_?name|last_?name|full_?name|surname|given_?name|address|street|"
    r"postal_?code|zip_?code|birth|date_?of_?birth|dob|iban|bank_?account|account_?number|"
    r"password|passwd|secret|token|api_?key|credential)s?(?:$|_)", re.I)
ORG_NUMBER = re.compile(r"(?:^|_)(?:org_?(?:nr|no|num|number)|organi[sz]ation_?(?:nr|number)|"
                        r"registreringsnummer|corporate_?id)(?:$|_)", re.I)
WHOLE_OBJECT = re.compile(r"^(?:payload|body|event|record|records|request|req|response|"
                          r"resp|data|message|item|items|kwargs|params|headers|context|"
                          r"user|customer|person|contact)$", re.I)


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _names_in(expr: ast.AST) -> list[str]:
    """Every identifier a logged expression spells: names, attributes, dict keys,
    subscript keys and `.get("…")` keys."""
    out = []
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name):
            out.append(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.append(sub.attr)
        elif isinstance(sub, ast.Dict):
            out += [k.value for k in sub.keys if isinstance(k, ast.Constant)
                    and isinstance(k.value, str)]
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                and isinstance(sub.slice.value, str):
            out.append(sub.slice.value)
        elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "get" and sub.args and isinstance(sub.args[0], ast.Constant) \
                and isinstance(sub.args[0].value, str):
            out.append(sub.args[0].value)
    return out


def _whole(expr: ast.AST) -> str | None:
    """The name of a value logged whole — `payload`, `event["body"]`, `record.get("body")`,
    `json.dumps(payload)` — rather than a field picked out of it."""
    if isinstance(expr, ast.Call) and _dotted(expr.func) in ("json.dumps", "str", "repr") \
            and expr.args:
        return _whole(expr.args[0])
    if isinstance(expr, ast.Name):
        return expr.id if WHOLE_OBJECT.match(expr.id) else None
    if isinstance(expr, ast.Attribute):
        return expr.attr if WHOLE_OBJECT.match(expr.attr) else None
    key = None
    if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant):
        key = expr.slice.value
    elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) \
            and expr.func.attr == "get" and expr.args and isinstance(expr.args[0], ast.Constant):
        key = expr.args[0].value
    return key if isinstance(key, str) and WHOLE_OBJECT.match(key) else None


def concerns_for(msg: ast.AST | None, values: list[ast.AST]) -> list[dict]:
    """`[{kind, name, why}]`, once per kind and name, in the order found."""
    out, seen = [], set()

    def add(kind, name, why):
        if (kind, name) not in seen:
            seen.add((kind, name))
            out.append({"kind": kind, "name": name, "why": why})
    exprs = list(values)
    if isinstance(msg, ast.JoinedStr):
        exprs = [v.value for v in msg.values if isinstance(v, ast.FormattedValue)] + exprs
    for e in exprs:
        for n in _names_in(e):
            sn = _snake(n)
            if PII_NAME.search(sn):
                add("pii", n, "the name says personal data — Ludvig logs no PII where avoidable")
            elif ORG_NUMBER.search(sn):
                add("org-number", n, "an organisation number can be a personal number")
        parts = [e] + (list(e.values) if isinstance(e, ast.Dict) else [])
        for whole in filter(None, map(_whole, parts)):
            add("whole-object", whole,
                "logged whole: every field in it reaches CloudWatch, personal data included")
    if isinstance(msg, ast.JoinedStr) and any(isinstance(v, ast.FormattedValue)
                                              for v in msg.values):
        add("f-string", "", "values interpolated into the message: put them in extra= so "
                            "the line stays one JSON shape CloudWatch Insights can query")
    return out


def _arg_type(func: ast.AST | None, expr: ast.AST) -> str | None:
    """A parameter's written annotation, for a bare-name argument; None otherwise."""
    if func is None or not isinstance(expr, ast.Name):
        return None
    for a in func.args.args + func.args.kwonlyargs + func.args.posonlyargs:
        if a.arg == expr.id and a.annotation is not None:
            return ast.unparse(a.annotation)
    return None


def scan_calls(scan: FileScan, allow_inherited: bool) -> tuple[list[Hit], list[AntiHit]]:
    hits, antis = [], []
    imp = scan.imports
    print_shadowed = "print" in imp.names or any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "print"
        for n in scan.tree.body)
    for node in ast.walk(scan.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # console output
        if isinstance(func, ast.Name) and func.id == "print" and not print_shadowed:
            antis.append(_anti(scan, node, "print"))
            continue
        name = _dotted(func) or ""
        if name in ("traceback.print_exc", "traceback.print_exception") \
                and imp.module_of("traceback") == "traceback":
            antis.append(_anti(scan, node, "print_exc"))
            continue
        if name in ("sys.stdout.write", "sys.stderr.write") and imp.module_of("sys") == "sys":
            antis.append(_anti(scan, node, name.rsplit(".", 1)[0]))
            continue
        if not isinstance(func, ast.Attribute) or func.attr not in LEVEL_OF_METHOD:
            continue
        recv = _dotted(func.value)
        if not recv:
            continue
        sym = scan.symbols.get(recv)
        if sym is None and _root_logger(recv, imp):
            sym = LoggerSymbol(recv, FLAVOUR_STDLIB, "the root logger, through the logging module",
                               "declared")
        if sym is None and allow_inherited and recv.rsplit(".", 1)[-1] in INHERITED_NAMES \
                and recv.startswith(("self.", "cls.")):
            sym = LoggerSymbol(recv, "inherited logger",
                               f"`{recv}`, a logger attribute set outside this module",
                               "heuristic")
        if sym is None or (sym.confidence == "heuristic" and not allow_inherited):
            continue
        # `log(level, msg, …)`: the message is the second argument
        args = list(node.args)
        if func.attr == "log" and args:
            args = args[1:]
        msg = args[0] if args and isinstance(args[0], (ast.Constant, ast.JoinedStr)) \
            and (not isinstance(args[0], ast.Constant) or isinstance(args[0].value, str)) else None
        rest = args[1:] if msg is not None else args
        rest_src = [scan.segment(a) for a in rest] + \
            [f"{k.arg}={scan.segment(k.value)}" if k.arg else f"**{scan.segment(k.value)}"
             for k in node.keywords]
        enclosing = scan.enclosing(node.lineno)
        logged = list(rest) + [k.value for k in node.keywords]
        if isinstance(msg, ast.JoinedStr):
            logged = [v.value for v in msg.values if isinstance(v, ast.FormattedValue)] + logged
        hits.append(Hit(
            file=scan.rel(), abs_file=scan.abs, line=node.lineno, column=node.col_offset + 1,
            end_line=node.end_lineno or node.lineno, level=LEVEL_OF_METHOD[func.attr],
            receiver=recv, flavour=sym.flavour, evidence=sym.evidence,
            confidence=sym.confidence, method=func.attr,
            format=scan.segment(msg) if msg is not None else None, args=rest_src,
            raw_line=scan.line(node.lineno), text=scan.segment(node),
            method_start=enclosing.lineno if enclosing else None,
            method_end=enclosing.end_lineno if enclosing else None,
            origins=origins_for(scan, enclosing, node.lineno, _values_named(logged)),
            arg_types=[_arg_type(enclosing, a) for a in rest] + [None] * len(node.keywords),
            concerns=concerns_for(msg, list(rest) + [k.value for k in node.keywords
                                                     if k.arg not in ("exc_info", "stack_info",
                                                                      "stacklevel")]),
        ))
    key = lambda h: (h.file, h.line, h.column)
    return sorted(hits, key=key), sorted(antis, key=key)


def _anti(scan: FileScan, node: ast.Call, kind: str) -> AntiHit:
    return AntiHit(file=scan.rel(), abs_file=scan.abs, line=node.lineno,
                   column=node.col_offset + 1, kind=kind, raw_line=scan.line(node.lineno),
                   text=scan.segment(node))


def extract(paths: list[str], root: str | None = None,
            allow_inherited: bool = True) -> tuple[list[Hit], list[AntiHit], dict]:
    root = os.path.realpath(root or os.getcwd())
    scans: list[FileScan] = []
    for p in python_files(paths):
        try:
            scans.append(FileScan(p, root))
        except (SyntaxError, ValueError, OSError) as e:
            print(f"[logextract] skipped {p}: {e}", file=sys.stderr)
    for s in scans:
        s.collect_declared()
    by_module = {s.module: s for s in scans if s.module}
    # Imports resolve to a fixed point: `a` imports `logger` from `b`, which imports it from `c`.
    for _ in range(3):
        for s in scans:
            s.resolve_imports(by_module, allow_inherited)
    hits, antis = [], []
    for s in scans:
        h, a = scan_calls(s, allow_inherited)
        hits += h
        antis += a
    stats = {
        "files_with_loggers": sum(1 for s in scans if s.symbols),
        "symbols": {s.rel(): [asdict(x) for x in sorted(s.symbols.values(),
                                                        key=lambda x: (x.line, x.name))]
                    for s in sorted(scans, key=lambda s: s.rel()) if s.symbols},
        "fields": {},
    }
    return hits, antis, stats


# --------------------------------------------------------------------------- #
# diff restriction
# --------------------------------------------------------------------------- #

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _diff(repo: str, base: str, head: str) -> str:
    """The working tree against `base` (the review's own fixes may be uncommitted), or two
    commits when `head` is named."""
    args = ["git", "-C", repo, "diff", "--unified=0", "--no-color", base]
    if head != "HEAD":
        args.append(head)
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def changed_ranges(repo: str, base: str, head: str = "HEAD") -> dict[str, list[tuple[int, int]]]:
    """Added/modified line ranges on the new side, per repo-relative file."""
    return {f: [(a, b) for a, b, _ in hs] for f, hs in changed_hunks(repo, base, head).items()}


def in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= line <= b for a, b in ranges)


# A logging call on a changed line is **modified** when the same diff hunk deletes a call
# to the same receiver and method, and **added** otherwise; deleted calls pair with new
# ones one-to-one, in order. `same_output` marks the subset that provably logs the same
# text: arguments equal up to whitespace, or a string literal replaced by a constant the
# new file binds to exactly that literal.

def changed_hunks(repo: str, base: str, head: str = "HEAD") -> dict[str, list[tuple]]:
    hunks: dict[str, list] = defaultdict(list)
    cur, hunk = None, None
    for line in _diff(repo, base, head).splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            hunk = None
        elif line.startswith("@@") and cur:
            m = HUNK_RE.match(line)
            hunk = None
            if m:
                start = int(m.group(1))
                cnt = int(m.group(2)) if m.group(2) is not None else 1
                if cnt:
                    hunk = [start, start + cnt - 1, []]
                    hunks[cur].append(hunk)
        elif hunk is not None and line.startswith("-") and not line.startswith("---"):
            hunk[2].append(line[1:])
    return {f: [(a, b, "\n".join(rm)) for a, b, rm in hs] for f, hs in hunks.items()}


def _split_top(text: str, sep: str) -> list[str]:
    """`text` split on `sep` outside brackets and string literals."""
    out, depth, quote, cur, i = [], 0, None, [], 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def calls_in(text: str, receiver: str, method: str) -> list[tuple[str, list[str] | None]]:
    """Every `receiver.method(…)` in `text`, as `(call text, its arguments)`; arguments None
    when the call does not close inside `text`."""
    out = []
    pat = re.compile(rf"(?<![\w.]){re.escape(receiver)}\s*\.\s*{re.escape(method)}\s*\(")
    for m in pat.finditer(text):
        depth, quote, i = 1, None, m.end()
        while i < len(text) and depth:
            c = text[i]
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            i += 1
        if depth:
            out.append((re.sub(r"\s+", " ", text[m.start():]).strip(), None))
        else:
            inner = text[m.end():i - 1]
            out.append((re.sub(r"\s+", " ", text[m.start():i]).strip(),
                        _split_top(inner, ",") if inner.strip() else []))
    return out


_PY_STRING = r'''(?:[rRbBuU]?"(?:[^"\\]|\\.)*"|[rRbBuU]?'(?:[^'\\]|\\.)*')'''


def constant_value(source: str, name: str) -> str | None:
    """The string literal `name` is bound to at the start of a line (`NAME = "…"`,
    `NAME: str = "…"`), or None."""
    m = re.search(rf"^\s*{re.escape(name)}\s*(?::[^=\n]+)?=\s*({_PY_STRING})\s*(?:#.*)?$",
                  source, re.M)
    return m.group(1) if m else None


def _same_literal(a: str, b: str) -> bool:
    try:
        return ast.literal_eval(a) == ast.literal_eval(b)
    except (ValueError, SyntaxError):
        return False


def same_output(old: list[str] | None, new: list[str] | None, source: str) -> bool:
    if old is None or new is None or len(old) != len(new):
        return False
    for a, b in zip(old, new):
        a, b = a.strip(), b.strip()
        if " ".join(a.split()) == " ".join(b.split()):
            continue
        if re.fullmatch(_PY_STRING, a) and re.fullmatch(r"[A-Za-z_]\w*", b):
            const = constant_value(source, b)
            if const is not None and _same_literal(a, const):
                continue
        return False
    return True


def pair_with_old_calls(hits: list, hunks: dict, repo: str) -> None:
    """Set `change` (and `was`, `same_output`) on every hit, in place."""
    sources: dict[str, str] = {}
    taken: dict[tuple, int] = defaultdict(int)
    for h in sorted(hits, key=lambda x: (x.abs_file, x.line, x.column)):
        rel = os.path.relpath(h.abs_file, os.path.realpath(repo))
        h.change, h.was, h.same_output = "added", "", False
        for first, last, removed in hunks.get(rel, ()):
            if not first <= h.line <= last:
                continue
            old = calls_in(removed, h.receiver, h.method)
            key = (rel, first, h.receiver, h.method)
            if taken[key] < len(old):
                was, old_args = old[taken[key]]
                taken[key] += 1
                if h.abs_file not in sources:
                    try:
                        sources[h.abs_file] = Path(h.abs_file).read_text(encoding="utf-8")
                    except OSError:
                        sources[h.abs_file] = ""
                new = calls_in(h.text, h.receiver, h.method)
                h.change, h.was = "modified", was
                h.same_output = bool(new) and same_output(old_args, new[0][1],
                                                          sources[h.abs_file])
            break


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="files or directories to scan")
    ap.add_argument("--root", help="root for relative paths and module names (default: cwd)")
    ap.add_argument("--repo", help="git repo for --since")
    ap.add_argument("--since", help="merge-base commit; keep only hits on added/modified lines")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--no-inherited", action="store_true",
                    help="drop loggers taken on their name alone (imported from outside the "
                         "scan, or a self.logger set elsewhere)")
    ap.add_argument("--json", help="write full JSON here")
    args = ap.parse_args(argv)

    root = args.root or args.repo or os.getcwd()
    # Relative paths are the repository's when one is named (`--root` / `--repo`), the
    # current directory's otherwise.
    paths = [p if os.path.isabs(p) or not (args.root or args.repo) else os.path.join(root, p)
             for p in args.paths]
    hits, antis, stats = extract(paths, root=root, allow_inherited=not args.no_inherited)

    diff_hits, diff_antis = None, None
    if args.since:
        repo = args.repo or root
        rng = changed_ranges(repo, args.since, args.head)

        def keep(h):
            r = os.path.relpath(h.abs_file, os.path.realpath(repo))
            return r in rng and in_ranges(h.line, rng[r])
        diff_hits = [h for h in hits if keep(h)]
        diff_antis = [a for a in antis if keep(a)]
        pair_with_old_calls(diff_hits, changed_hunks(repo, args.since, args.head), repo)

    payload = {
        "all": {"logging": [asdict(h) for h in hits],
                "antipattern": [asdict(a) for a in antis]},
        "stats": stats,
    }
    if diff_hits is not None:
        payload["changed"] = {"logging": [asdict(h) for h in diff_hits],
                              "antipattern": [asdict(a) for a in diff_antis]}
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2))

    shown = diff_hits if diff_hits is not None else hits
    scope = "on added/modified lines" if diff_hits is not None else "in scope"
    print(f"{len(shown)} logging statement(s) {scope}")
    for h in shown:
        tag = f"  {h.change}" if h.change else ""
        if h.change == "modified":
            tag += " (same text logged)" if h.same_output else ""
        print(f"  {h.file}:{h.line}:{h.column}  [{h.level:5}] {h.receiver}.{h.method}  "
              f"({h.flavour}, {h.confidence}){tag}")
        if h.was:
            print(f"        was: {h.was}")
        print(f"        {h.raw_line.strip()}")
    shown_a = diff_antis if diff_antis is not None else antis
    if shown_a:
        print(f"\n{len(shown_a)} console statement(s) {scope}")
        for a in shown_a:
            print(f"  {a.file}:{a.line}:{a.column}  [{a.kind}]  {a.raw_line.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
