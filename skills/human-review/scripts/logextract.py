#!/usr/bin/env python3
"""
Structural (AST-based) extractor for Java logging statements.

Two passes, both driven by `ast-grep --json`; no text search for the call itself.

  Pass 1  build a per-file symbol table of identifiers that really are loggers
          (declared type, logger-factory initialiser, Lombok annotation, or an
          inherited logger inferred from the file's logging imports)
  Pass 2  match every  $RECV.$METHOD($$$ARGS)  whose $METHOD is a log level and
          whose root receiver is in that file's logger set.

`System.out.println` / `System.err.println` / `printStackTrace()` are collected
separately and never mixed into the logger results.

Dependency-free: stdlib + the `ast-grep` binary.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# ast-grep rules.  These are the *whole* structural surface of the extractor.
# --------------------------------------------------------------------------- #

RULES: dict[str, str] = {}

# --- pass 1a: a field / local whose *declared type* is a logger type --------- #
RULES["logger-field"] = r"""
id: logger-field
language: java
severity: hint
message: field whose declared type is a logger type
rule:
  kind: field_declaration
  all:
    - has: {field: type, pattern: $TYPE}
    - has:
        field: declarator
        has: {field: name, pattern: $NAME}
constraints:
  TYPE:
    regex: '(^|\.)(Logger|Log|XLogger|FluentLogger|Slf4jLogger|LogAccessor)$'
"""

RULES["logger-local"] = r"""
id: logger-local
language: java
severity: hint
message: local whose declared type is a logger type
rule:
  any:
    - kind: local_variable_declaration
    - kind: formal_parameter
  all:
    - has: {field: type, pattern: $TYPE}
    - any:
        - has:
            field: declarator
            has: {field: name, pattern: $NAME}
        - has: {field: name, pattern: $NAME}
constraints:
  TYPE:
    regex: '(^|\.)(Logger|Log|XLogger|FluentLogger|Slf4jLogger|LogAccessor)$'
"""

# --- pass 1e: type declarations, for scoping and for `extends` resolution ---- #
RULES["type-decl"] = r"""
id: type-decl
language: java
severity: hint
message: type declaration
rule:
  any:
    - kind: class_declaration
    - kind: interface_declaration
    - kind: enum_declaration
    - kind: record_declaration
  has: {field: name, pattern: $CNAME}
"""

RULES["type-super"] = r"""
id: type-super
language: java
severity: hint
message: type declaration with an extends clause
rule:
  any:
    - kind: class_declaration
    - kind: enum_declaration
    - kind: record_declaration
  all:
    - has: {field: name, pattern: $CNAME}
    - has: {field: superclass, pattern: $SUPER}
"""

# --- pass 1f: method/constructor ranges ---------------------------------------------- #
# Not part of the logger-detection surface at all: this is what lets a caller ask "what
# method encloses line N", which scopes both the origin walk and the type lookup to the
# parameters and locals that are actually visible at the statement.
RULES["method-decl"] = r"""
id: method-decl
language: java
severity: hint
message: method or constructor declaration
rule:
  any:
    - kind: method_declaration
    - kind: constructor_declaration
"""

# --- pass 1g: every field declaration, not just logger-typed ones -------------------- #
# `logger-field` above is the same shape with a `constraints:` on the type. Kept as a
# separate rule rather than dropping that constraint and filtering in Python, because the
# `has:`+`constraints:` combo is exactly the pattern the gotcha docstring above warns
# about — the two rules stay independent on purpose.
RULES["any-field"] = r"""
id: any-field
language: java
severity: hint
message: field declaration
rule:
  kind: field_declaration
  all:
    - has: {field: type, pattern: $FTYPE}
    - has:
        field: declarator
        has: {field: name, pattern: $FNAME}
"""

# --- pass 1h: the three declaration shapes a logged value can be traced back to ------ #
# "Data flow to here", the syntactic half of it. A logged `vetId` is only interesting
# once the reader can see where it came from, and that is a question about declarations:
# the local that was assigned it, the parameter it arrived as, the field it was read
# from. These three rules are the whole graph the walk-back in `origins_for()` walks —
# there is no dataflow engine behind it and no model asked, so what it can follow is
# exactly what these can see, and it stops rather than guesses everywhere else.

RULES["local-decl"] = r"""
id: local-decl
language: java
severity: hint
message: local variable declared with an initialiser
rule:
  kind: local_variable_declaration
  all:
    - has: {field: type, pattern: $LTYPE}
    - has:
        field: declarator
        all:
          - has: {field: name, pattern: $LNAME}
          - has: {field: value, pattern: $LVALUE}
"""

# A parameter is a *boundary*, not a hop: nothing before it is visible from inside the
# method, so the walk shows the signature line and stops there. `catch_formal_parameter`
# is the same story for the `e` in `log.error("…", e)`.
RULES["param-decl"] = r"""
id: param-decl
language: java
severity: hint
message: method, lambda or catch parameter
rule:
  any:
    - kind: formal_parameter
    - kind: catch_formal_parameter
    - kind: spread_parameter
  has: {field: name, pattern: $PNAME}
"""

# Re-assignment after the declaration: `name = vet.name;` is where `name` actually comes
# from at the log line, and quoting the declaration instead would be a lie by omission.
RULES["assign"] = r"""
id: assign
language: java
severity: hint
message: assignment to an existing variable
rule:
  kind: assignment_expression
  all:
    - has: {field: left, pattern: $ALEFT}
    - has: {field: right, pattern: $ARIGHT}
"""

# --- pass 1i: the static type of every name a logged value can start from ----------- #
# What the Logging tab prints in front of each argument, IntelliJ-style: `[String]
# vet.getLastName()`. Answered from declarations only — the type a local, a parameter, a
# for-each variable or a catch clause was *declared* with, and the return type of the
# repo's own methods for the calls chained off it. No inference engine: a name these
# cannot see, or can see declared two different ways, gets no hint at all.

RULES["typed-local"] = r"""
id: typed-local
language: java
severity: hint
message: local variable with its declared type
rule:
  kind: local_variable_declaration
  all:
    - has: {field: type, pattern: $VTYPE}
    - has:
        field: declarator
        has: {field: name, pattern: $VNAME}
"""

RULES["typed-param"] = r"""
id: typed-param
language: java
severity: hint
message: formal parameter with its declared type
rule:
  kind: formal_parameter
  all:
    - has: {field: type, pattern: $VTYPE}
    - has: {field: name, pattern: $VNAME}
"""

RULES["typed-catch"] = r"""
id: typed-catch
language: java
severity: hint
message: catch parameter with its declared type
rule:
  kind: catch_formal_parameter
  all:
    - has: {kind: catch_type, pattern: $VTYPE}
    - has: {field: name, pattern: $VNAME}
"""

# Scoped to the loop statement itself: the variable does not exist past its closing brace.
RULES["typed-foreach"] = r"""
id: typed-foreach
language: java
severity: hint
message: enhanced-for variable with its declared type
rule:
  kind: enhanced_for_statement
  all:
    - has: {field: type, pattern: $VTYPE}
    - has: {field: name, pattern: $VNAME}
"""

# A lambda parameter written without a type. Recorded so that it *shadows*: `o -> log.info(
# "{}", o)` inside a class with a field `o` must not borrow the field's type.
RULES["lambda-param"] = r"""
id: lambda-param
language: java
severity: hint
message: lambda parameter list
rule:
  kind: lambda_expression
  has: {field: parameters, pattern: $LPARAMS}
"""

RULES["method-ret"] = r"""
id: method-ret
language: java
severity: hint
message: method with its return type
rule:
  kind: method_declaration
  all:
    - has: {field: type, pattern: $RTYPE}
    - has: {field: name, pattern: $MNAME}
    - has: {field: parameters, pattern: $MPARAMS}
"""

# `record Owner(String name)`: each component is a field *and* an accessor `name()`.
RULES["record-comp"] = r"""
id: record-comp
language: java
severity: hint
message: record component
rule:
  kind: formal_parameter
  all:
    - has: {field: type, pattern: $VTYPE}
    - has: {field: name, pattern: $VNAME}
  inside:
    kind: formal_parameters
    inside:
      kind: record_declaration
"""


# --- pass 1b: any declarator initialised from a logger factory --------------- #
# Catches `var log = LoggerFactory.getLogger(..)`, `private final Logger x =
# LogManager.getLogger()`, `Log l = LogFactory.getLog(..)`, Flogger, ...
RULES["logger-init"] = r"""
id: logger-init
language: java
severity: hint
message: declarator initialised from a logger factory
rule:
  kind: variable_declarator
  all:
    - has: {field: name, pattern: $NAME}
    - has:
        field: value
        all:
          - pattern: $OBJ.$FACTORY($$$FARGS)
          - any:
              - {kind: method_invocation}
constraints:
  FACTORY:
    regex: '^(getLogger|getLog|getXLogger|forEnclosingClass|forClass|getFormatterLogger)$'
  OBJ:
    regex: '(^|\.)(LoggerFactory|LogManager|LogFactory|Logger|XLoggerFactory|FluentLogger|Log)$'
"""

# --- pass 1c: Lombok annotations that *inject* a logger field ---------------- #
# Matched on the *type declaration* so the injected field is scoped to that
# class's source range — a file holding several annotated classes resolves each
# `log` to the right flavour.
RULES["lombok-log"] = r"""
id: lombok-log
language: java
severity: hint
message: Lombok annotation injecting a logger field
rule:
  any:
    - {kind: class_declaration}
    - {kind: enum_declaration}
    - {kind: record_declaration}
    - {kind: interface_declaration}
  all:
    - has: {field: name, pattern: $CNAME}
    - has:
        kind: modifiers
        has:
          all:
            - pattern: $ANN
            - regex: '^@(lombok\.(extern\.[a-zA-Z0-9.]+\.)?)?(Slf4j|XSlf4j|Log4j2|Log4j|CommonsLog|CustomLog|Log|Flogger|JBossLog|Apachecommonslog)(\(.*\))?$'
          stopBy: neighbor
"""

# --- pass 1d: the file's logging imports (used for flavour + inherited tier) - #
RULES["log-import"] = r"""
id: log-import
language: java
severity: hint
message: import of a logging API
rule:
  kind: import_declaration
  pattern: $IMP
constraints:
  IMP:
    regex: 'import\s+(static\s+)?(org\.slf4j|org\.apache\.logging\.log4j|org\.apache\.commons\.logging|java\.util\.logging|com\.google\.common\.flogger|lombok\.extern|org\.jboss\.logging|ch\.qos\.logback)'
"""

# --- pass 2: the call itself ------------------------------------------------- #
RULES["log-call"] = r"""
id: log-call
language: java
severity: hint
message: call to a method whose name is a log level
rule:
  pattern: $RECV.$METHOD($$$ARGS)
constraints:
  METHOD:
    regex: '^(trace|debug|info|warn|warning|error|fatal|severe|config|fine|finer|finest|log|logp|logrb|atTrace|atDebug|atInfo|atWarn|atError|atFatal|atLevel|atSevere|atWarning|atConfig|atFine|atFiner|atFinest)$'
"""

# --- separate category: not loggers at all ---------------------------------- #
RULES["console-antipattern"] = r"""
id: console-antipattern
language: java
severity: hint
message: console output / stack trace dump instead of a logger
rule:
  any:
    - pattern: System.out.println($$$ARGS)
    - pattern: System.out.print($$$ARGS)
    - pattern: System.out.printf($$$ARGS)
    - pattern: System.err.println($$$ARGS)
    - pattern: System.err.print($$$ARGS)
    - pattern: System.err.printf($$$ARGS)
    - pattern: $E.printStackTrace($$$ARGS)
"""

# --------------------------------------------------------------------------- #
# Logger vocabulary
# --------------------------------------------------------------------------- #

LOMBOK_FIELD = {
    "Slf4j": ("log", "Lombok @Slf4j"),
    "XSlf4j": ("log", "Lombok @XSlf4j"),
    "Log4j2": ("log", "Lombok @Log4j2"),
    "Log4j": ("log", "Lombok @Log4j"),
    "CommonsLog": ("log", "Lombok @CommonsLog"),
    "Log": ("log", "Lombok @Log (JUL)"),
    "JBossLog": ("log", "Lombok @JBossLog"),
    "Apachecommonslog": ("log", "Lombok @Apachecommonslog"),
    "Flogger": ("logger", "Lombok @Flogger"),
    # lombok.config: lombok.log.custom.declaration=<type> <factory>(NAME)
    "CustomLog": ("log", "Lombok @CustomLog"),
}

TYPE_FLAVOUR = [
    (re.compile(r"^org\.slf4j\.Logger$"), "SLF4J"),
    (re.compile(r"^org\.slf4j\.ext\.XLogger$"), "SLF4J-ext"),
    (re.compile(r"^org\.apache\.logging\.log4j\.Logger$"), "Log4j2"),
    (re.compile(r"^org\.apache\.log4j\.Logger$"), "Log4j1"),
    (re.compile(r"^org\.apache\.commons\.logging\.Log$"), "Commons Logging"),
    (re.compile(r"^java\.util\.logging\.Logger$"), "JUL"),
    (re.compile(r"^com\.google\.common\.flogger\.FluentLogger$"), "Flogger"),
    (re.compile(r"^XLogger$"), "SLF4J-ext"),
    (re.compile(r"^FluentLogger$"), "Flogger"),
    # Spring's own wrapper around Commons Logging (spring-core)
    (re.compile(r"(^|\.)LogAccessor$"), "Spring LogAccessor"),
]

FACTORY_FLAVOUR = [
    (re.compile(r"(^|\.)LoggerFactory$"), "SLF4J"),
    (re.compile(r"(^|\.)XLoggerFactory$"), "SLF4J-ext"),
    (re.compile(r"(^|\.)LogManager$"), "Log4j2"),
    (re.compile(r"(^|\.)LogFactory$"), "Commons Logging"),
    (re.compile(r"(^|\.)FluentLogger$"), "Flogger"),
    (re.compile(r"(^|\.)Logger$"), "JUL"),
    (re.compile(r"(^|\.)Log$"), "Commons Logging"),
]

IMPORT_FLAVOUR = [
    ("lombok.extern.slf4j", "SLF4J"),
    ("lombok.extern.log4j", "Log4j2"),
    ("lombok.extern.apachecommons", "Commons Logging"),
    ("lombok.extern.java", "JUL"),
    ("lombok.extern.flogger", "Flogger"),
    ("lombok.extern.jbosslog", "JBoss Logging"),
    ("org.slf4j", "SLF4J"),
    ("org.apache.logging.log4j", "Log4j2"),
    ("org.apache.commons.logging", "Commons Logging"),
    ("java.util.logging", "JUL"),
    ("com.google.common.flogger", "Flogger"),
    ("org.jboss.logging", "JBoss Logging"),
    ("ch.qos.logback", "Logback"),
]

# names a bare, inherited logger is plausibly called (tier-3 fallback only)
INHERITED_NAMES = {"log", "LOG", "logger", "LOGGER", "Log", "_log", "_logger", "LOGGER_"}

LEVEL_OF_METHOD = {
    "trace": "TRACE", "finest": "TRACE", "atTrace": "TRACE", "atFinest": "TRACE",
    "debug": "DEBUG", "fine": "DEBUG", "atDebug": "DEBUG", "atFine": "DEBUG",
    "finer": "DEBUG", "atFiner": "DEBUG",
    "info": "INFO", "config": "INFO", "atInfo": "INFO", "atConfig": "INFO",
    "warn": "WARN", "warning": "WARN", "atWarn": "WARN", "atWarning": "WARN",
    "error": "ERROR", "severe": "ERROR", "atError": "ERROR", "atSevere": "ERROR",
    "fatal": "FATAL", "atFatal": "FATAL",
    "log": "LOG", "logp": "LOG", "logrb": "LOG", "atLevel": "LOG",
}

# `log()` / `atLevel()` alone is too weak to accept on a bare receiver: it is the
# terminal of a fluent chain, or `Math.log`, or `Duration.log`.  Only accepted
# when the receiver resolves to a logger AND (it is a fluent chain, or the
# receiver is a known logger identifier).
WEAK_METHODS = {"log", "atLevel"}

# `LoggerFactory.getLogger(X).warn(..)` / `getLogger(pred).fatal(..)` — the
# receiver is not a name at all, it is the factory call itself.
INLINE_FACTORY_RE = re.compile(
    r"^\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*"
    r"(LoggerFactory|LogManager|LogFactory|XLoggerFactory|Logger|Log)\s*\.\s*"
    r"(getLogger|getLog|getXLogger)\s*\(")
INLINE_ACCESSOR_RE = re.compile(r"^\s*(?:this\s*\.\s*)?(getLogger|getLog|getXLogger|logger|log)\s*\(")

AT_LEVEL_RE = re.compile(r"\.at(Trace|Debug|Info|Warn|Error|Fatal|Severe|Warning|Config|Fine|Finer|Finest)\s*\(")
ROOT_RECV_RE = re.compile(r"^\s*(?:this\s*\.\s*)?([A-Za-z_$][A-Za-z0-9_$]*)")


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    file: str            # path relative to root
    abs_file: str
    line: int            # 1-based
    column: int          # 1-based
    end_line: int
    level: str
    receiver: str        # root receiver identifier, e.g. "log"
    flavour: str         # SLF4J / Lombok @Slf4j / Log4j2 / JUL / Commons Logging
    evidence: str        # how the receiver was proven to be a logger
    confidence: str      # "declared" | "lombok" | "inherited" | "heuristic"
    method: str
    format: str | None   # first argument, if it is a string literal
    args: list           # arguments after the format string
    raw_line: str        # exact source line
    text: str            # exact matched expression
    method_start: int | None  # enclosing method/constructor, 1-based inclusive range —
    method_end: int | None    # None when no enclosing method resolved (e.g. a static initializer)
    # "Data flow to here": one entry per hop back from an interpolated value to where it
    # came from — `{line, name, kind, text}` — for the renderer to *quote* rather than
    # paraphrase. Empty when every value is self-evident, or unresolvable, or capped out.
    origins: list = field(default_factory=list)
    # The declared static type of each entry in `args`, in the same order — `"String"`,
    # `"Integer"`, `"List<Pet>"` — or None where it could not be resolved from a
    # declaration. None is an answer: the page shows no hint rather than a guessed one.
    arg_types: list = field(default_factory=list)


@dataclass
class AntiHit:
    file: str
    abs_file: str
    line: int
    column: int
    kind: str            # System.out / System.err / printStackTrace
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
# ast-grep driver
# --------------------------------------------------------------------------- #

def _is_really_ast_grep(path: str) -> bool:
    """`sg` is ast-grep's own short alias — and, on every mainstream Linux distribution, it
    is also `/usr/bin/sg` from `util-linux`/`passwd`, the "run a command with a different
    group id" tool. Picking that one up is worse than finding nothing: `sg scan …` exits
    with a status this driver treats as "ran fine, matched nothing", so the extractor
    reports *no logging anywhere* with total confidence. That is precisely the wrong answer
    the docstring below swears never to give, and it is what made this scan silently empty
    on Ubuntu CI. So a candidate has to introduce itself before it is trusted."""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "ast-grep" in (proc.stdout + proc.stderr).lower()


@functools.lru_cache(maxsize=None)
def _ast_grep_bin() -> str:
    """The `ast-grep` binary, which is not a Python dependency and must not be assumed.

    `$AST_GREP_BIN` first, so a machine that keeps it somewhere unusual can say so without
    editing this file — the fallback used to be one hardcoded absolute path from the author's
    laptop, which is a portability bug in a skill whose whole point is running in your
    repository. `sg` is ast-grep's own short alias; on some systems it is a different tool
    entirely, so it is tried last — and, either way, every candidate has to answer
    `--version` with its own name before it is used."""
    for cand in (os.environ.get("AST_GREP_BIN"), "ast-grep", "sg"):
        p = shutil.which(cand) if cand else None
        if p and _is_really_ast_grep(p):
            return p
    raise SystemExit(
        "ast-grep not found on PATH — install it (`brew install ast-grep`, or "
        "`pip install ast-grep-cli`) or point $AST_GREP_BIN at it. This scan is structural "
        "on purpose; there is no grep fallback, because a wrong 'no logging found' is worse "
        "than no answer.")


def run_ast_grep(paths: Iterable[str], rule_ids: Iterable[str] | None = None) -> list[dict]:
    """Run one `ast-grep scan` with all rules; return the raw JSON matches."""
    ids = list(rule_ids) if rule_ids else list(RULES)
    paths = [str(p) for p in paths]
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / "rules"
        rd.mkdir()
        for rid in ids:
            (rd / f"{rid}.yml").write_text(RULES[rid])
        (Path(td) / "sgconfig.yml").write_text("ruleDirs:\n  - rules\n")
        cmd = [_ast_grep_bin(), "scan", "-c", str(Path(td) / "sgconfig.yml"),
               "--json=stream", "--inspect", "nothing", *paths]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            sys.stderr.write(proc.stderr[:4000])
            raise SystemExit(f"ast-grep failed ({proc.returncode})")
        out = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out


def _mv(m: dict, name: str) -> str | None:
    v = m.get("metaVariables", {}).get("single", {}).get(name)
    return v["text"] if v else None


def _mv_multi(m: dict, name: str) -> list[str]:
    vs = m.get("metaVariables", {}).get("multi", {}).get(name, [])
    return [v["text"] for v in vs if v["text"].strip() != ","]


# --------------------------------------------------------------------------- #
# pass 1 — per-file logger symbol table
# --------------------------------------------------------------------------- #

def flavour_from_type(t: str) -> str | None:
    t = t.strip()
    for rx, fl in TYPE_FLAVOUR:
        if rx.search(t):
            return fl
    return None


def flavour_from_factory(obj: str) -> str | None:
    for rx, fl in FACTORY_FLAVOUR:
        if rx.search(obj.strip()):
            return fl
    return None


class Project:
    """Everything pass 2 needs, keyed per file plus a project-wide type graph."""

    def __init__(self) -> None:
        self.symbols: dict[str, dict[str, LoggerSymbol]] = defaultdict(dict)
        self.imports: dict[str, set[str]] = defaultdict(set)
        self.lombok: dict[str, list[tuple]] = defaultdict(list)
        # file -> [(start, end, class-name)] ; innermost = largest start
        self.classes: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        # simple class name -> simple superclass name
        self.supers: dict[str, str] = {}
        # simple class name -> {field name: LoggerSymbol} for non-private fields
        self.inheritable: dict[str, dict[str, LoggerSymbol]] = defaultdict(dict)
        # file -> [(start, end)] for every method/constructor body, 1-based inclusive
        self.methods: dict[str, list[tuple[int, int]]] = defaultdict(list)
        # file -> [(line, name, type, declaration text)] for every field, any type
        self.all_fields: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
        # The three tables the origin walk reads. Kept flat and per-file rather than
        # scoped into a symbol table: the walk already knows the enclosing method's line
        # range, and filtering a short list by it is cheaper (and far less to get wrong)
        # than building a second scope tree next to `classes`/`methods`.
        # file -> [(line, name, initialiser text, whole declaration text)]
        self.locals_: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
        # file -> [(line, name, right-hand side text)]
        self.assigns: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        # file -> [(line, name)]
        self.params: dict[str, list[tuple[int, str]]] = defaultdict(list)
        # The type tables `arg_type()` reads. `typed` is every name declared with a type
        # (or, for an untyped lambda parameter, with None — a name that shadows): file ->
        # [(line, scope end or None, name, type or None)]. `members` is the repo's own
        # classes by simple name: fields and method return types, as *sets*, because two
        # classes can share a simple name and a set of two answers is no answer.
        self.typed: dict[str, list[tuple[int, int | None, str, str | None]]] = defaultdict(list)
        self.members: dict[str, dict[str, dict]] = defaultdict(
            lambda: {"fields": defaultdict(set), "methods": defaultdict(set)})
        self._pending_members: list[tuple] = []

    def enclosing(self, f: str, line: int) -> str | None:
        chain = self.enclosing_chain(f, line)
        return chain[0] if chain else None

    def enclosing_method(self, f: str, line: int) -> tuple[int, int] | None:
        """The innermost method/constructor whose range contains `line`.

        A lambda or an anonymous inner
        class's own method nests inside the outer one, so the innermost (largest
        start) match is the one actually enclosing the statement."""
        hits = [(s, e) for s, e in self.methods.get(f, []) if s <= line <= e]
        return max(hits, key=lambda t: t[0]) if hits else None

    def enclosing_chain(self, f: str, line: int) -> list[str]:
        """Lexically enclosing type names, innermost first (inner -> outer)."""
        hits = [(start, name) for start, end, name in self.classes.get(f, [])
                if start <= line <= end]
        hits.sort(key=lambda t: -t[0])
        return [n for _s, n in hits]

    def inherited(self, f: str, line: int, recv: str) -> LoggerSymbol | None:
        """Walk `extends` by simple name looking for a visible logger field.

        Tried for every lexically enclosing class, innermost first: an inner
        class routinely uses the *outer* class's inherited logger.
        """
        for cls in self.enclosing_chain(f, line):
            seen, depth = set(), 0
            while cls and cls not in seen and depth < 12:
                seen.add(cls)
                depth += 1
                sup = self.supers.get(cls)
                if not sup:
                    break
                sym = self.inheritable.get(sup, {}).get(recv)
                if sym:
                    return LoggerSymbol(recv, sym.flavour,
                                        f"inherited from `{sup}` ({sym.evidence})",
                                        "inherited", sym.line)
                cls = sup
        return None


def build_symbol_tables(matches: list[dict]) -> Project:
    p = Project()
    by_file, imports, lombok = p.symbols, p.imports, p.lombok
    field_decls: list[tuple[str, int, str, str, str]] = []  # f, line, name, type, text

    # imports first: the flavour of a bare `Logger`/`Log` type is decided by them
    for m in matches:
        if m.get("ruleId") != "log-import":
            continue
        imp = _mv(m, "IMP") or m["text"]
        for pfx, fl in IMPORT_FLAVOUR:
            if pfx in imp:
                imports[m["file"]].add(fl)

    for m in matches:
        rid = m.get("ruleId")
        f = m["file"]
        line = m["range"]["start"]["line"] + 1

        if rid in ("logger-field", "logger-local"):
            typ = (_mv(m, "TYPE") or "").strip()
            name = _mv(m, "NAME")
            if not name:
                continue
            fl = flavour_from_type(typ) or _flavour_by_import(imports.get(f), typ)
            by_file[f][name] = LoggerSymbol(name, fl or "unknown",
                                            f"declared `{typ} {name}`", "declared", line)
            if rid == "logger-field":
                field_decls.append((f, line, name, typ, m["text"]))

        elif rid == "type-decl":
            cname = _mv(m, "CNAME")
            if cname:
                p.classes[f].append((line, m["range"]["end"]["line"] + 1, cname))

        elif rid == "type-super":
            cname, sup = _mv(m, "CNAME"), _mv(m, "SUPER")
            if cname and sup:
                sup = re.sub(r"^extends\s+", "", sup.strip())
                sup = sup.split("<")[0].split(".")[-1].strip()
                # two classes may share a simple name and one may extend the
                # other by FQN — reducing to simple names makes that look
                # self-referential.  Never record a class as its own super.
                if sup and sup != cname:
                    p.supers.setdefault(cname, sup)

        elif rid == "logger-init":
            name = _mv(m, "NAME")
            obj = _mv(m, "OBJ") or ""
            fac = _mv(m, "FACTORY") or ""
            if not name:
                continue
            fl = flavour_from_factory(obj) or "unknown"
            prev = by_file[f].get(name)
            if prev is None or prev.flavour == "unknown":
                by_file[f][name] = LoggerSymbol(
                    name, fl, f"initialised from `{obj}.{fac}(…)`", "declared", line)

        elif rid == "lombok-log":
            ann = (_mv(m, "ANN") or "").strip()
            base = re.sub(r"\(.*\)$", "", ann).lstrip("@").split(".")[-1]
            if base in LOMBOK_FIELD:
                fname, fl = LOMBOK_FIELD[base]
                lombok[f].append((line, m["range"]["end"]["line"] + 1, fname, fl, ann))

        elif rid == "method-decl":
            p.methods[f].append((line, m["range"]["end"]["line"] + 1))

        elif rid == "any-field":
            typ = (_mv(m, "FTYPE") or "").strip()
            name = _mv(m, "FNAME")
            if name:
                p.all_fields[f].append((line, name, typ, m["text"]))

        elif rid == "local-decl":
            name, val = _mv(m, "LNAME"), _mv(m, "LVALUE")
            if name and val is not None:
                p.locals_[f].append((line, name, val.strip(), m["text"]))

        elif rid == "assign":
            lhs, rhs = _mv(m, "ALEFT"), _mv(m, "ARIGHT")
            # `this.x = y` and `x = y` are the same story for the walk; `a[i] = y` and
            # `o.f.g = y` are not something it can follow, so they are simply not recorded.
            lhs = root_receiver(lhs or "") if lhs and PLAIN_TARGET_RE.match(lhs.strip()) else None
            if lhs and rhs is not None:
                p.assigns[f].append((line, lhs, rhs.strip()))

        elif rid == "param-decl":
            name = _mv(m, "PNAME")
            if name:
                p.params[f].append((line, name))

        elif rid in ("typed-local", "typed-param", "typed-catch"):
            name, typ = _mv(m, "VNAME"), (_mv(m, "VTYPE") or "").strip()
            if name and typ:
                p.typed[f].append((line, None, name, typ))

        elif rid == "typed-foreach":
            name, typ = _mv(m, "VNAME"), (_mv(m, "VTYPE") or "").strip()
            if name and typ:
                p.typed[f].append((line, m["range"]["end"]["line"] + 1, name, typ))

        elif rid == "lambda-param":
            lp = (_mv(m, "LPARAMS") or "").strip()
            end = m["range"]["end"]["line"] + 1
            # `x -> …` / `(x, y) -> …` only; a typed `(Pet p) -> …` is a formal_parameter
            # and `typed-param` already has it.
            for name in re.findall(r"[A-Za-z_$][\w$]*", lp):
                if re.fullmatch(r"\(?\s*(?:[A-Za-z_$][\w$]*\s*,\s*)*[A-Za-z_$][\w$]*\s*\)?", lp):
                    p.typed[f].append((line, end, name, None))

        elif rid == "method-ret":
            name, typ = _mv(m, "MNAME"), (_mv(m, "RTYPE") or "").strip()
            params = (_mv(m, "MPARAMS") or "()").strip()[1:-1]
            if name and typ:
                p._pending_members.append(("methods", f, line, name, (_arity(params), typ)))

        elif rid == "record-comp":
            name, typ = _mv(m, "VNAME"), (_mv(m, "VTYPE") or "").strip()
            if name and typ:
                p._pending_members.append(("fields", f, line, name, typ))
                p._pending_members.append(("methods", f, line, name, (0, typ)))

    # Members are filed under their class only now that every class's range is known —
    # ast-grep reports matches in whatever order its workers finish.
    for f, fl in p.all_fields.items():
        for line, name, typ, _text in fl:
            p._pending_members.append(("fields", f, line, name, typ))
    for kind, f, line, name, val in p._pending_members:
        owner = p.enclosing(f, line)
        if owner:
            p.members[owner][kind][name].add(val)
    p._pending_members.clear()

    # project-wide inheritance table: a *non-private* logger field is visible to
    # every subclass.  Spring's `protected final Log logger = LogFactory.getLog(
    # getClass());` on ~40 base classes is the whole reason this exists.
    for f, line, name, typ, text in field_decls:
        if re.search(r"\bprivate\b", text.split("=")[0]):
            continue
        owner = p.enclosing(f, line)
        if not owner:
            continue
        sym = by_file[f].get(name)
        if sym:
            p.inheritable[owner][name] = sym

    return p


def lombok_symbol(scopes: list[tuple], name: str, line: int) -> LoggerSymbol | None:
    """Innermost enclosing Lombok scope that injects a field called `name`."""
    best = None
    for start, end, fname, fl, ann in scopes:
        if fname == name and start <= line <= end:
            if best is None or (start > best[0]):
                best = (start, end, fname, fl, ann)
    if best is None:
        return None
    return LoggerSymbol(name, best[3], f"injected by Lombok `{best[4]}`", "lombok", best[0])


FLAVOUR_PRIORITY = ["Commons Logging", "SLF4J", "Log4j2", "Log4j1", "JUL",
                    "Flogger", "JBoss Logging", "SLF4J-ext", "Logback"]


def _dominant(imps: set[str]) -> str:
    for fl in FLAVOUR_PRIORITY:
        if fl in imps:
            return fl
    return sorted(imps)[0] if imps else "unknown"


def _flavour_by_import(imps: set[str] | None, typ: str) -> str | None:
    if not imps:
        return None
    short = typ.split(".")[-1].split("<")[0]
    if short == "Log" and "Commons Logging" in imps:
        return "Commons Logging"
    if short == "Logger":
        for cand in ("SLF4J", "Log4j2", "JUL", "JBoss Logging"):
            if cand in imps:
                return cand
    if short == "XLogger":
        return "SLF4J-ext"
    if len(imps) == 1:
        return next(iter(imps))
    return None


# --------------------------------------------------------------------------- #
# pass 2 — match calls whose root receiver is a known logger
# --------------------------------------------------------------------------- #

def root_receiver(recv: str) -> str | None:
    m = ROOT_RECV_RE.match(recv)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# "Data flow to here" — the syntactic walk-back
#
# For each value a log statement interpolates, walk back to where it comes from and
# report the *lines*, so the renderer can quote them instead of a paragraph describing
# them. There is no dataflow engine here and no model is asked: the whole graph is the
# `local-decl` / `assign` / `param-decl` / `any-field` matches above, which is why the
# stop conditions are written down rather than tuned.
#
# It follows a chain, not one hop: `name` -> `var name = c.name` -> where `c` came from.
# It stops at the first thing a reader can already read off the line it lands on:
#
#   * a **parameter** (method, lambda or catch) — nothing before it is visible here;
#   * a **field** — the declaration is the answer;
#   * an **initialiser that is not a bare name path** — a call (`repo.getById(id)`), a
#     `new`, a literal, an expression. That line *is* the origin; chasing the receiver of
#     a repository call leads to `private final VetRepository vetRepository` and tells
#     the reader nothing they wanted;
#   * anything it cannot resolve in this file at all — it shows nothing rather than guess.
#
# And two things are deliberately never shown, because showing everything is the same
# failure as explaining everything: a `static final` **constant** (its value is a literal
# a reader already knows), and a value that resolves to **nothing** (an enhanced-for loop
# variable, a static import) — an obvious loop variable is not provenance.
# --------------------------------------------------------------------------- #

# How far one value may be chased. Three is the depth at which a chain still reads as a
# story ("logged -> assigned from -> came in as"); past that the snippet stops being a
# snippet.
MAX_ORIGIN_HOPS = 3
# …and the budget for the whole statement, across every value it interpolates, because
# this tab lists every touched file and a three-line entry that becomes twenty is a worse
# tab, not a better one. Values are walked in argument order and the walk stops when the
# budget is gone.
MAX_ORIGIN_LINES = 6

# An expression the walk is willing to keep following: a bare name, or a name path
# (`c.name`, `owner.address.city`). Anything with a `(`, a `new`, an operator or a
# literal in it is a boundary — see the block comment above.
NAME_PATH_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*$")
# An assignment target simple enough to record: `x` or `this.x`, never `a[i]` or `o.f.g`.
PLAIN_TARGET_RE = re.compile(r"^(?:this\s*\.\s*)?[A-Za-z_$][\w$]*$")
JAVA_WORDS = {"this", "super", "true", "false", "null", "new", "class", "return"}


def origin_root(expr: str) -> str | None:
    """The identifier a logged expression is rooted at, or None if there is nothing to
    trace: a literal, a keyword, or a Type-qualified static reference (`Instant.now()`).
    An uppercase-initial root followed by a `.` is a class name by every Java convention
    there is, and following it lands on an import, not on a value."""
    expr = (expr or "").strip()
    if not expr or expr[0] in '"\'' or expr[0].isdigit():
        return None
    m = ROOT_RECV_RE.match(expr)
    if not m:
        return None
    name = m.group(1)
    if name in JAVA_WORDS:
        return None
    rest = expr[m.end():].lstrip()
    if name[:1].isupper() and rest.startswith("."):
        return None
    return name


def origins_for(proj: "Project", f: str, line: int, args: Iterable[str],
                method: tuple[int, int] | None,
                max_hops: int = MAX_ORIGIN_HOPS,
                max_lines: int = MAX_ORIGIN_LINES) -> list[dict]:
    """Where each interpolated value comes from, as lines to quote — see the block
    comment above for the stop conditions and why each one is where it is.

    Returns one dict per hop, in walk order: `{line, name, kind, text}`, with `kind` in
    `local` / `assign` (a hop the walk continued through) and `param` / `field` (a
    boundary it stopped at). Lines inside the statement itself are dropped — the snippet
    already shows those — and so is anything past `max_lines`."""
    ms, me = method if method else (None, None)

    def in_method(n: int) -> bool:
        return ms is None or ms <= n <= me

    fields = {name: (fline, text) for fline, name, _typ, text in proj.all_fields.get(f, [])}
    out: list[dict] = []
    lines_seen: set[int] = set()
    visited: set[str] = set()

    def budget_left() -> bool:
        return len(lines_seen) < max_lines

    for arg in args:
        name = origin_root(arg)
        hops = 0
        while name and hops < max_hops and budget_left():
            if name in visited:
                break
            visited.add(name)
            hops += 1

            # 1. nearest assignment or local declaration *before* the statement, inside
            #    the enclosing method. Both are candidates and the later one wins: a
            #    variable re-assigned after its declaration comes from the assignment.
            cands: list[tuple[int, str, str, str]] = []
            for aline, aname, rhs in proj.assigns.get(f, []):
                if aname == name and aline < line and in_method(aline):
                    cands.append((aline, "assign", rhs, ""))
            for lline, lname, val, text in proj.locals_.get(f, []):
                if lname == name and lline <= line and in_method(lline):
                    cands.append((lline, "local", val, text))
            if cands:
                oline, kind, rhs, _text = max(cands, key=lambda t: t[0])
                _record(out, lines_seen, oline, name, kind, rhs, line)
                # Continue only through a bare name path; anything else is the boundary.
                name = origin_root(rhs) if NAME_PATH_RE.match(rhs) else None
                continue

            # 2. a parameter of the enclosing method (or of the catch clause) — boundary.
            plines = [pl for pl, pn in proj.params.get(f, []) if pn == name and in_method(pl)]
            if plines:
                _record(out, lines_seen, max(plines), name, "param", "", line)
                break

            # 3. a field — boundary, unless it is a `static final` constant, which is a
            #    literal wearing a name and is left out entirely.
            fld = fields.get(name)
            if fld and not (re.search(r"\bstatic\b", fld[1].split("=")[0])
                            and re.search(r"\bfinal\b", fld[1].split("=")[0])):
                _record(out, lines_seen, fld[0], name, "field", "", line)
            break

    return out


def _record(out: list, lines_seen: set, oline: int, name: str, kind: str,
            rhs: str, stmt_line: int) -> None:
    """Append one hop, unless its line is the statement's own (already on screen) or
    already pulled in by an earlier value."""
    if oline == stmt_line or oline in lines_seen:
        return
    lines_seen.add(oline)
    out.append({"line": oline, "name": name, "kind": kind, "text": rhs})


# --------------------------------------------------------------------------- #
# The static type of a logged value — the hint the Logging tab puts in front of it
#
# Read off declarations, never inferred past them. An argument is taken apart as a chain
# (`vet.getLastName()`, `this.owner.pets`, `Foo.bar(x).baz`) and each link is looked up:
# the first in the scope the statement sits in (a local, a parameter, a for-each or catch
# variable, a field of the enclosing class or one it extends), every following one as a
# member of the type the previous link produced — a field, a method's declared return
# type, or the getter convention over a field (`getLastName()` -> `lastName`, which is
# also what Lombok generates). The JDK is not on disk to be read, so a handful of its
# answers that cannot be wrong (`toString()` is a String, `size()` an int) are written
# down; everything else outside the repo resolves to nothing, and nothing means no hint.
# --------------------------------------------------------------------------- #

PRIMITIVES = {"byte", "short", "int", "long", "float", "double", "boolean", "char"}
# Answers that hold whatever the receiver is.
ANY_METHODS = {"toString": "String", "hashCode": "int", "equals": "boolean"}
STRING_METHODS = {"length": "int", "isEmpty": "boolean", "isBlank": "boolean",
                  "trim": "String", "strip": "String", "toUpperCase": "String",
                  "toLowerCase": "String", "substring": "String", "formatted": "String",
                  "repeat": "String", "replace": "String", "concat": "String"}
COLLECTION_TYPES = {"List", "Set", "Collection", "Map", "ArrayList", "LinkedList", "HashSet",
                    "TreeSet", "LinkedHashSet", "HashMap", "TreeMap", "LinkedHashMap",
                    "Queue", "Deque", "SortedSet", "SortedMap"}
COLLECTION_METHODS = {"size": "int", "isEmpty": "boolean"}
THROWABLE_METHODS = {"getMessage": "String", "getLocalizedMessage": "String"}
# A type parameter (`T`, `E`, `K2`) says nothing about what the value is at this call.
TYPE_VAR_RE = re.compile(r"^[A-Z][0-9]?$")
IDENT = r"[A-Za-z_$][\w$]*"


def _arity(params: str) -> int:
    """How many arguments a comma-separated list holds, commas inside (), <>, [] and
    string literals not counted."""
    params = params.strip()
    return len(_split_top(params, ",")) if params else 0


def _split_top(text: str, sep: str) -> list[str]:
    """`text` split on `sep` where it is not nested inside brackets or a literal."""
    out, depth, cur, quote, i = [], 0, [], None, 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c in "([{<":
            depth += 1
            cur.append(c)
        elif c in ")]}>":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [p.strip() for p in out]


def _simple(t: str) -> str:
    """`java.util.List<Pet>` -> `List`: the name a class is filed under in `members`."""
    return t.split("<")[0].strip().split(".")[-1].strip()


def _literal_type(e: str) -> str | None:
    if e.startswith('"'):
        return "String"
    if e.startswith("'"):
        return "char"
    if e in ("true", "false"):
        return "boolean"
    if re.fullmatch(r"\d[\d_]*[lL]", e) or re.fullmatch(r"0[xX][\da-fA-F_]+[lL]", e):
        return "long"
    if re.fullmatch(r"\d[\d_]*|0[xX][\da-fA-F_]+|0[bB][01_]+", e):
        return "int"
    if re.fullmatch(r"(\d[\d_]*\.?[\d_]*|\.\d[\d_]*)([eE][+-]?\d+)?[fF]", e):
        return "float"
    if re.fullmatch(r"(\d[\d_]*\.[\d_]*|\.\d[\d_]*)([eE][+-]?\d+)?[dD]?|\d+[eE][+-]?\d+[dD]?|\d+[dD]", e):
        return "double"
    return None


def _chain(e: str) -> list[tuple[str, str | None]] | None:
    """`a.b(x).c` -> `[("a", None), ("b", "x"), ("c", None)]` — each link's name and, for a
    call, its argument text. None for anything that is not a plain chain (an operator, an
    array index, a lambda, a `new` with a body)."""
    links, i, n = [], 0, len(e)
    while True:
        m = re.compile(r"\s*(" + IDENT + r")\s*").match(e, i)
        if not m:
            return None
        name, i = m.group(1), m.end()
        args = None
        if i < n and e[i] == "(":
            depth, j, quote = 0, i, None
            while j < n:
                c = e[j]
                if quote:
                    if c == "\\":
                        j += 1
                    elif c == quote:
                        quote = None
                elif c in "\"'":
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                return None
            args, i = e[i + 1:j], j + 1
        links.append((name, args))
        while i < n and e[i].isspace():
            i += 1
        if i == n:
            return links
        if e[i] != ".":
            return None
        i += 1


class TypeScope:
    """Everything `arg_type` needs to know about where one statement sits."""

    def __init__(self, proj: "Project", f: str, line: int, method: tuple[int, int] | None):
        self.proj, self.f, self.line = proj, f, line
        self.ms, self.me = method if method else (None, None)
        self.classes = proj.enclosing_chain(f, line)

    def _in_method(self, n: int) -> bool:
        return self.ms is None or self.ms <= n <= self.me

    def variable(self, name: str) -> str | None | bool:
        """The declared type of a name visible here; False when it is not declared in this
        method at all (so the caller may try fields), None when it is declared but its
        type cannot be read (untyped lambda parameter, conflicting declarations)."""
        seen = set()
        found = False
        for dline, dend, dname, dtype in self.proj.typed.get(self.f, []):
            if dname != name or dline > self.line or not self._in_method(dline):
                continue
            if dend is not None and self.line > dend:
                continue
            found = True
            seen.add(dtype)
        if not found:
            return False
        if len(seen) != 1:
            return None
        t = next(iter(seen))
        if t == "var":
            return self.var_initialiser(name)
        return t

    def var_initialiser(self, name: str) -> str | None:
        inits = {val for vline, vname, val, _t in self.proj.locals_.get(self.f, [])
                 if vname == name and vline <= self.line and self._in_method(vline)}
        if len(inits) != 1:
            return None
        return self.expr(next(iter(inits)), depth=1)

    def _class_member(self, cls: str, kind: str, name: str, arity: int | None = None,
                      _seen=None) -> str | None:
        """A field's type or a method's return type on `cls`, walking `extends`."""
        seen = _seen or set()
        while cls and cls not in seen:
            seen.add(cls)
            table = self.proj.members.get(cls)
            if table is not None:
                got = table[kind].get(name)
                if got:
                    if kind == "methods":
                        exact = {t for a, t in got if arity is None or a == arity}
                        got = exact or {t for _a, t in got}
                    return next(iter(got)) if len(got) == 1 else None
            cls = self.proj.supers.get(cls)
        return None

    def member(self, owner: str | None, name: str, args: str | None) -> str | None:
        """The type `owner.name` or `owner.name(args)` produces."""
        if owner is None:
            return None
        if owner.endswith("[]"):
            return "int" if args is None and name == "length" else None
        simple = _simple(owner)
        if args is None:
            return self._class_member(simple, "fields", name)
        ret = self._class_member(simple, "methods", name, _arity(args))
        if ret is None and simple in self.proj.members and _arity(args) == 0:
            # The getter convention, which is also what Lombok writes for `@Getter`.
            m = re.fullmatch(r"(get|is)([A-Z][\w$]*)", name)
            if m:
                ret = self._class_member(simple, "fields", m.group(2)[0].lower() + m.group(2)[1:])
        if ret is None:
            ret = ANY_METHODS.get(name)
            if simple == "String":
                ret = ret or STRING_METHODS.get(name)
            elif simple in COLLECTION_TYPES:
                ret = ret or COLLECTION_METHODS.get(name)
            elif re.search(r"(Exception|Error|Throwable)$", simple) and simple not in self.proj.members:
                ret = ret or THROWABLE_METHODS.get(name)
        return ret

    def first(self, name: str, args: str | None) -> tuple[str | None, bool]:
        """The type the first link of a chain produces, and whether it named a class
        (`Foo` in `Foo.bar()`) rather than a value."""
        if args is not None:
            for cls in self.classes:
                t = self._class_member(cls, "methods", name, _arity(args))
                if t:
                    return t, False
            return None, False
        if name == "this":
            return (self.classes[0] if self.classes else None), False
        v = self.variable(name)
        if v is not False:
            return v, False
        for cls in self.classes:
            t = self._class_member(cls, "fields", name)
            if t:
                return t, False
        if name[:1].isupper():
            return name, True
        return None, False

    def expr(self, e: str, depth: int = 0) -> str | None:
        e = (e or "").strip()
        if not e or depth > 3:
            return None
        lit = _literal_type(e)
        if lit:
            return lit
        if e == "null":
            return None
        # A string concatenation: any top-level `+` with a string literal on either side.
        plus = _split_top(e, "+")
        if len(plus) > 1:
            return "String" if any(p.startswith('"') for p in plus) else None
        m = re.fullmatch(r"\(\s*((?:" + PRIM_OR_CLASS + r"))\s*\)\s*(.+)", e, re.S)
        if m and (m.group(1) in PRIMITIVES or m.group(1)[:1].isupper()):
            return m.group(1)
        m = re.fullmatch(r"new\s+(" + IDENT + r"(?:\s*\.\s*" + IDENT + r")*)\s*(<[^()]*>)?\s*\((.*)\)",
                         e, re.S)
        if m and "{" not in m.group(3):
            gen = m.group(2) or ""
            return m.group(1) + ("" if gen.replace(" ", "") == "<>" else gen)
        if e.startswith("(") and e.endswith(")") and _split_top(e[1:-1], ",") == [e[1:-1].strip()]:
            return self.expr(e[1:-1], depth + 1)
        links = _chain(e)
        if not links:
            return None
        t, is_class = self.first(*links[0])
        if is_class:
            # A class reference is only worth anything with a member after it, and only a
            # class the repo declares (`Instant.now()` is not something we can read).
            if len(links) == 1 or t not in self.proj.members:
                return None
        for name, args in links[1:]:
            if t is None:
                return None
            t = self.member(t, name, args)
        if t is None or TYPE_VAR_RE.match(t) or t in ("void", "var"):
            return None
        return t


PRIM_OR_CLASS = IDENT + r"(?:\s*\.\s*" + IDENT + r")*(?:\s*<[^()]*>)?(?:\s*\[\s*\])*"


def arg_type(proj: "Project", f: str, line: int, method: tuple[int, int] | None,
             expr: str) -> str | None:
    """The declared static type of one logged expression, or None — see the block above."""
    try:
        return TypeScope(proj, f, line, method).expr(expr)
    except (RecursionError, ValueError):
        return None


class SourceCache:
    def __init__(self) -> None:
        self._c: dict[str, list[str]] = {}

    def line(self, path: str, n: int) -> str:
        ls = self._c.get(path)
        if ls is None:
            try:
                ls = Path(path).read_text(errors="replace").splitlines()
            except OSError:
                ls = []
            self._c[path] = ls
        return ls[n - 1].rstrip("\n") if 0 < n <= len(ls) else ""


def extract(paths: list[str], root: str | None = None,
            allow_inherited: bool = True) -> tuple[list[Hit], list[AntiHit], dict]:
    matches = run_ast_grep(paths)
    proj = build_symbol_tables(matches)
    symbols, imports, lombok = proj.symbols, proj.imports, proj.lombok
    src = SourceCache()
    # realpath, not abspath: `abs_file` below is built from `os.path.abspath()` on a
    # path relative to the process's own cwd, and POSIX `getcwd()` always resolves
    # symlinks — so on a machine where the temp dir (or any ancestor of the project) is
    # itself a symlink (macOS's /tmp -> /private/tmp is the standing example), an
    # `--root` that keeps the symlinked spelling stops matching the canonical one
    # `abs_file` carries. `rel()` then silently produces a path with no rows in
    # `changed_ranges()`'s table, and every hit in it gets diffed away — a false "no
    # logging found" from a path-spelling mismatch, which is exactly the one answer this
    # whole extractor exists to never give.
    root = os.path.realpath(root) if root else None

    def rel(p: str) -> str:
        return os.path.relpath(p, root) if root else p

    hits: list[Hit] = []
    antis: list[AntiHit] = []
    seen: set[tuple[str, int, int]] = set()

    # fluent chains yield nested matches at the same start offset — keep the
    # outermost (longest text) only.
    calls = [m for m in matches if m.get("ruleId") == "log-call"]
    best: dict[tuple[str, int, int], dict] = {}
    for m in calls:
        k = (m["file"], m["range"]["start"]["line"], m["range"]["start"]["column"])
        if k not in best or len(m["text"]) > len(best[k]["text"]):
            best[k] = m

    # Sorted by (file, line, column) rather than taken in `best`'s insertion order, which
    # is ast-grep's output order, which is the order its worker threads happened to finish
    # the files in. Two runs over an unchanged tree emitted the same statements in a
    # different sequence, so `logging.json` — and the whole Logging tab under it — changed
    # on every rebuild. That is not a cosmetic problem: it is the difference between a page
    # a reader can diff against yesterday's and one that always looks like it moved.
    for m in sorted(best.values(),
                    key=lambda m: (m["file"], m["range"]["start"]["line"],
                                   m["range"]["start"]["column"])):
        f = m["file"]
        recv = _mv(m, "RECV") or ""
        method = _mv(m, "METHOD") or ""
        rr = root_receiver(recv)
        call_line = m["range"]["start"]["line"] + 1
        inline = INLINE_FACTORY_RE.match(recv)
        sym = None
        if not inline and rr:
            sym = (symbols.get(f, {}).get(rr)
                   or lombok_symbol(lombok.get(f, []), rr, call_line)
                   or (proj.inherited(f, call_line, rr) if allow_inherited else None))
        confidence = None
        if inline:
            flavour = flavour_from_factory(inline.group(1)) or "unknown"
            evidence = f"receiver is the factory call `{inline.group(1)}.{inline.group(2)}(…)`"
            confidence = "declared"
            rr = rr or inline.group(1)
        elif sym:
            flavour, evidence, confidence = sym.flavour, sym.evidence, sym.confidence
        elif (allow_inherited and INLINE_ACCESSOR_RE.match(recv) and imports.get(f)
              and method not in WEAK_METHODS):
            flavour = _dominant(imports[f])
            evidence = (f"receiver is a call to `{INLINE_ACCESSOR_RE.match(recv).group(1)}(…)` "
                        f"in a file importing {', '.join(sorted(imports[f]))}")
            confidence = "heuristic"
            rr = rr or "?"
        elif allow_inherited and rr and rr in INHERITED_NAMES and imports.get(f):
            flavour = _dominant(imports[f])
            evidence = (f"bare `{rr}` in a file importing {', '.join(sorted(imports[f]))} "
                        f"— inherited or static logger, superclass not in scan scope")
            confidence = "heuristic"
        else:
            continue

        chain = m["text"]
        level = LEVEL_OF_METHOD.get(method, "LOG")
        at = AT_LEVEL_RE.search(chain)
        if at:
            level = LEVEL_OF_METHOD.get("at" + at.group(1), level)

        # weak terminal (`.log(..)`) is only trusted as a fluent chain or on a
        # receiver proven to be a logger by declaration/Lombok.
        if method in WEAK_METHODS and not at and confidence == "heuristic":
            continue

        args = _mv_multi(m, "ARGS")
        fmt = args[0] if args and args[0].startswith(('"', "\"\"\"")) else None
        rest = args[1:] if fmt is not None else args

        ln = m["range"]["start"]["line"] + 1
        col = m["range"]["start"]["column"] + 1
        key = (f, ln, col)
        if key in seen:
            continue
        seen.add(key)
        menc = proj.enclosing_method(f, ln)
        hits.append(Hit(
            file=rel(f), abs_file=os.path.abspath(f), line=ln, column=col,
            end_line=m["range"]["end"]["line"] + 1,
            level=level, receiver=rr, flavour=flavour, evidence=evidence,
            confidence=confidence, method=method, format=fmt, args=rest,
            raw_line=src.line(f, ln), text=chain,
            method_start=menc[0] if menc else None,
            method_end=menc[1] if menc else None,
            origins=origins_for(proj, f, ln, rest if fmt is not None else args, menc),
            arg_types=[arg_type(proj, f, ln, menc, a) for a in rest],
        ))

    # Sorted for the same reason as the loop above, and it is this list that was actually
    # caught swapping: two `System.out.println` findings traded places between consecutive
    # runs over an untouched tree.
    for m in sorted((m for m in matches if m.get("ruleId") == "console-antipattern"),
                    key=lambda m: (m["file"], m["range"]["start"]["line"],
                                   m["range"]["start"]["column"])):
        f = m["file"]
        ln = m["range"]["start"]["line"] + 1
        t = m["text"]
        kind = ("System.out" if t.startswith("System.out")
                else "System.err" if t.startswith("System.err")
                else "printStackTrace")
        antis.append(AntiHit(file=rel(f), abs_file=os.path.abspath(f), line=ln,
                             column=m["range"]["start"]["column"] + 1, kind=kind,
                             raw_line=src.line(f, ln), text=t))

    # Sorted by file, and each file's symbols by where they are declared. Both dicts are
    # filled in ast-grep's completion order, so the JSON's *key* order moved between runs
    # even when every symbol in it was identical — the same instability as the two loops
    # above, one level up.
    sym_out: dict[str, list] = {
        rel(f): [asdict(s) for s in sorted(symbols[f].values(),
                                           key=lambda s: (s.line, s.name))]
        for f in sorted(symbols)
    }
    for f, scopes in sorted(lombok.items()):
        sym_out.setdefault(rel(f), []).extend(
            asdict(LoggerSymbol(fname, fl, f"injected by Lombok `{ann}`", "lombok", start))
            for start, _end, fname, fl, ann in scopes)
    # Every field, any type, per file: a value traced to `this.ownerName` or a bare
    # inherited field needs the field's declared type, not just the method that used it.
    fields_out: dict[str, list] = {
        rel(f): [{"line": line, "name": name, "type": typ, "text": text}
                 for line, name, typ, text in sorted(fl)]
        for f, fl in sorted(proj.all_fields.items())
    }
    stats = {
        "files_with_loggers": len(sym_out),
        "symbols": sym_out,
        "fields": fields_out,
    }
    return hits, antis, stats


# --------------------------------------------------------------------------- #
# diff restriction
# --------------------------------------------------------------------------- #

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_ranges(repo: str, base: str, head: str = "HEAD") -> dict[str, list[tuple[int, int]]]:
    """Added/modified line ranges on the *new* side, per file (repo-relative).

    With the default head, the *working tree* is the new side — not `HEAD`. Step 1 of
    this skill tells you to leave the review's own fixes uncommitted so `git diff` shows
    them alone, so a commit-to-commit diff here cannot see a log line an agent just
    added, and the tab answers "no logging found" — the one answer it must never get
    wrong. Naming an explicit --head still compares two commits.
    """
    args = ["git", "-C", repo, "diff", "--unified=0", "--no-color", base]
    if head != "HEAD":
        args.append(head)
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    cur = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@") and cur:
            m = HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                cnt = int(m.group(2)) if m.group(2) is not None else 1
                if cnt:
                    ranges[cur].append((start, start + cnt - 1))
    return dict(ranges)


def in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= line <= b for a, b in ranges)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="files or directories to scan")
    ap.add_argument("--root", help="root for relative paths (default: cwd)")
    ap.add_argument("--repo", help="git repo for --since")
    ap.add_argument("--since", help="merge-base commit; keep only hits on added/modified lines")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--no-inherited", action="store_true",
                    help="drop the tier-3 inherited-logger heuristic")
    ap.add_argument("--json", help="write full JSON here")
    args = ap.parse_args(argv)

    hits, antis, stats = extract(args.paths, root=args.root,
                                 allow_inherited=not args.no_inherited)

    diff_hits, diff_antis = None, None
    if args.since:
        repo = args.repo or args.root or os.getcwd()
        rng = changed_ranges(repo, args.since, args.head)
        # realpath, matching `extract()`'s own fix above and for the same reason: `git
        # -C repo diff` reports paths relative to git's own view of the repo, which is
        # canonical, and `h.abs_file` is canonical too, so a `--repo`/`--root` that is
        # still spelled through a symlink (`/tmp/...` on macOS) must not be compared
        # against them with plain `abspath`.
        def keep(h):
            r = os.path.relpath(h.abs_file, os.path.realpath(repo))
            return r in rng and in_ranges(h.line, rng[r])
        diff_hits = [h for h in hits if keep(h)]
        diff_antis = [a for a in antis if keep(a)]

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
        print(f"  {h.file}:{h.line}:{h.column}  [{h.level:5}] {h.receiver}.{h.method}  "
              f"({h.flavour}, {h.confidence})")
        print(f"        {h.raw_line.strip()}")
    shown_a = diff_antis if diff_antis is not None else antis
    if shown_a:
        print(f"\n{len(shown_a)} console/anti-pattern statement(s) {scope}")
        for a in shown_a:
            print(f"  {a.file}:{a.line}:{a.column}  [{a.kind}]  {a.raw_line.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
