"""The Logging tab: every logging statement the branch adds or changes, with the declared
type of each value it logs."""
from __future__ import annotations

import functools
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ..shared.snippets import snippet_html
from ..shared.util import HERE

LOGEXTRACT = HERE / "logextract.py"


SRCREF_HREF = re.compile(r'(<a class="srcref" href="vscode://file/[^:"]*)(?::\d+){0,2}"')


def _aim_at_statement(snippet: str, ref: str, hits) -> str:
    """Point a quoted window's `path:line` link at the statement it is quoting.

    Everything else on this page links a snippet to its first line, which is right when the
    snippet *is* the thing. Here it is not: the snippet is four lines of context around one
    `log.warn(...)`, and landing the reader on the first of them makes them find it again by
    eye. The extractor already knows the line and the column, so the link uses them — and
    only when exactly one known statement falls inside the window, because two would make
    the choice a guess."""
    rel, _, span = ref.rpartition(":")
    lo = int(span.split("-")[0])
    hi = int(span.split("-")[-1])
    inside = [h for h in hits if h["file"] == rel and lo <= h["line"] <= hi]
    if len(inside) != 1:
        return snippet
    h = inside[0]
    return SRCREF_HREF.sub(lambda m: f'{m.group(1)}:{h["line"]}:{h["column"]}"', snippet, count=1)


def _logging_aside(part, found, what, root: Path, hits=()) -> str:
    """One of the two context registers under the added-logging finding.

    The prose and the snippets are the author's — a log line is only interesting once
    somebody says what is wrong with it — but the *count* is the extractor's, so a section
    that quotes three of four statements is caught here rather than by a reader."""
    if not part:
        return ""
    quoted = len(part.get("snippets", []))
    if found and quoted != found:
        print(f"[review] WARNING: the logging tab quotes {quoted} {what} statement(s) but "
              f"logextract found {found} — one of the two is out of date.", file=sys.stderr)
    return (
        f'<h2 id="{html.escape(part["id"])}">{html.escape(part["title"])}</h2>'
        + part.get("body", "")
        + "".join(_aim_at_statement(
            snippet_html(x["ref"], x.get("caption"), root, exact=True), x["ref"], hits)
            for x in part.get("snippets", []))
    )


# How many origin lines one entry may pull in. `logextract.py` already caps the walk
# (three hops per value, six lines per statement); this is the *page's* cap on top of
# that, and it is deliberately tighter, because the failure here is not a wrong answer,
# it is a tab. This tab lists every touched Java file, and an entry that grows from
# three lines to twenty to show a chain nobody asked about has made the tab worse in
# exactly the way the prose it replaced did.
MAX_ORIGIN_LINES_SHOWN = 4


def _logging_ref(h: dict) -> str:
    """The snippet reference for one statement: its own line(s), plus the lines its
    interpolated values were traced back to — `Foo.java:89,93`.

    The origins are the extractor's (`logextract.py` walks them syntactically, from the
    ast-grep graph); all that happens here is the cap and the sort. Nearest-first, so
    when the budget runs out what survives is the hop closest to the statement — the one
    a reader would have looked at first anyway — and never a far-away line with the
    intervening ones silently dropped."""
    end = h.get("end_line") or h["line"]
    spans = [f'{h["line"]}' if end == h["line"] else f'{h["line"]}-{end}']
    origins = sorted({o["line"] for o in (h.get("origins") or [])
                      if not (h["line"] <= o["line"] <= end)},
                     key=lambda n: abs(n - h["line"]))[:MAX_ORIGIN_LINES_SHOWN]
    spans += [str(n) for n in sorted(origins)]
    return f'{h["file"]}:{",".join(spans)}'


@functools.lru_cache(maxsize=1)
def _logextract():
    """`logextract.py` as a module, not a subprocess — this needs its rule table, not a
    scan. Registered in `sys.modules` *before* `exec_module`: its `@dataclass`
    declarations resolve their own annotations by looking their module up by name, and a
    module executed without being registered is not there to be found."""
    import importlib.util
    if "logextract" in sys.modules:
        return sys.modules["logextract"]
    spec = importlib.util.spec_from_file_location("logextract", str(LOGEXTRACT))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["logextract"] = mod
    spec.loader.exec_module(mod)
    return mod


# The alternation inside `log-import`'s constraint: `(org\.slf4j|org\.apache…|ch\.qos\.logback)`.
# Two or more dotted, lower-case package roots between one pair of parentheses is the only
# group in that rule shaped like this — the `(static\s+)?` before it is neither dotted nor
# an alternation — so the rule can be rewritten freely without this having to be told.
_LOG_PKGS_RE = re.compile(r"\(([a-z][\w\\.]*(?:\|[a-z][\w\\.]*)+)\)")


@functools.lru_cache(maxsize=1)
def logging_libraries() -> tuple[str, ...]:
    """The packages the scan actually searches for, in the rule's own order.

    Read out of `logextract.py`'s own `log-import` rule rather than typed here. A list of
    library names on a page is a claim about what a scan looked for, and the only version
    of that claim worth showing is the one that cannot go stale: add a logging API to the
    rule and this list gains it on the next build; nobody has to remember the page.

    The escaping is undone (`org\\.slf4j` is a regex, not a package). Returns empty when
    the regex cannot be found at all — the caller says where to look instead, because
    naming the rule beats inventing a list."""
    rule = _logextract().RULES.get("log-import", "")
    for alt in _LOG_PKGS_RE.findall(rule):
        pkgs = [p.replace("\\", "") for p in alt.split("|")]
        if any("." in p for p in pkgs):
            return tuple(pkgs)
    return ()


def logging_libraries_tip() -> str:
    """The same packages as the hover panel's markup: one per line, in code type.

    Eight dotted package roots welded into a sentence is a list pretending to be prose —
    the reader's question is "is mine in there", and answering it meant reading a
    comma-separated run to the end. One bullet per package, monospace because these are
    identifiers and not words, and the two-line tail says the part that is genuinely
    prose: a logger reached without an import still counts."""
    pkgs = logging_libraries()
    if not pkgs:
        return "<p class=\"tipfoot\">The packages named by logextract.py's log-import rule.</p>"
    items = "".join(f"<li>{html.escape(p)}</li>" for p in pkgs)
    return (f'<ul class="tiplist">{items}</ul>'
            f'<p class="tipfoot">&hellip;plus loggers reached by type, factory or Lombok.</p>')


# --------------------------------------------------------------------------- #
# Type hints, the way IntelliJ draws them
#
# Each value a statement logs gets a small grey chip in front of it naming its declared
# type — `log.debug("Attending vet: {}", [String] vet.getLastName())`. The type is
# `logextract.py`'s (`arg_types`, read off declarations in the repo, None where it could
# not be); this only puts it on the page. Nothing here asks anyone anything: a value whose
# type was not resolved simply has no chip.
#
# The chip is an empty element whose text is drawn by CSS (`::before { content:
# attr(data-type) }`). That keeps it out of the line's *text*: a reader who copies the
# statement copies the Java, not `Integer visit.getId()`, and `code_xref`, which reads
# every quoted line back as plain text to find the names in it, never sees it either.
# --------------------------------------------------------------------------- #

_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_CHAR = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);|.", re.S)
# The same two shapes `code_xref` reads: the <pre> holding one `.ln-row` per source line.
_PRE = re.compile(r'<pre class="code[^"]*"><code>(?P<rows>.*?)</code></pre>', re.S)
_ROW = re.compile(r'^(?P<open><span class="ln-row[^"]*">'
                  r'(?:<span class="dm">[^<]*</span>)?<span class="ln">(?P<no>\d+)</span>)'
                  r'(?P<code>.*)(?P<close></span>)$')


def type_hint_html(t: str) -> str:
    return (f'<span class="typehint" data-type="{html.escape(t, quote=True)}"'
            f' aria-hidden="true"></span>')


def _plain(code: str) -> str:
    return html.unescape(_TAG_SPLIT.sub("", code))


def _insert_at(code: str, inserts: dict[int, str]) -> str:
    """Put `inserts[col]` in front of the `col`-th visible character of a highlighted
    line. In front of the token's opening tag, not inside it, so the chip is never painted
    in the colour Pygments gave the name it precedes."""
    out: list[str] = []
    pos = 0
    opening_at = None          # where the run of opening tags just before the next char began
    for tok in _TAG_SPLIT.split(code):
        if not tok:
            continue
        if tok.startswith("<"):
            if tok.startswith("</"):
                opening_at = None
            elif opening_at is None:
                opening_at = len(out)
            out.append(tok)
            continue
        for ch in _CHAR.findall(tok):
            if pos in inserts:
                out.insert(opening_at if opening_at is not None else len(out), inserts[pos])
            opening_at = None
            out.append(ch)
            pos += 1
    return "".join(out)


def _hint_arguments(snippet: str, h: dict) -> str:
    """Draw each resolved argument type in front of its argument, in the quoted rows that
    hold the statement. The arguments are found in order in the rows' visible text, after
    the format string, so a name that also appears inside the message is not mistaken for
    the argument; one that cannot be found keeps its line untouched."""
    types = h.get("arg_types") or []
    args = h.get("args") or []
    if not any(types):
        return snippet
    pre = _PRE.search(snippet)
    if not pre:
        return snippet
    first, last = h["line"], h.get("end_line") or h["line"]
    lines = pre["rows"].split("\n")
    rows = []                                   # (index in `lines`, match)
    for i, raw in enumerate(lines):
        m = _ROW.match(raw)
        if m and first <= int(m["no"]) <= last:
            rows.append((i, m))
    if not rows:
        return snippet
    texts = [_plain(m["code"]) for _i, m in rows]
    joined = "\n".join(texts)
    starts, acc = [], 0
    for t in texts:
        starts.append(acc)
        acc += len(t) + 1
    anchor = (h.get("format") or "").split("\n")[0] or f'{h.get("method", "")}('
    cursor = joined.find(anchor)
    if cursor < 0:
        return snippet
    cursor += len(anchor)
    per_row: dict[int, dict[int, str]] = {}
    for arg, t in zip(args, types):
        key = (arg or "").strip().split("\n")[0].strip()
        if not key:
            continue
        pos = joined.find(key, cursor)
        if pos < 0:
            continue
        cursor = pos + len(key)
        if not t:
            continue
        r = max(k for k, s in enumerate(starts) if s <= pos)
        per_row.setdefault(r, {})[pos - starts[r]] = type_hint_html(t)
    for r, inserts in per_row.items():
        i, m = rows[r]
        lines[i] = m["open"] + _insert_at(m["code"], inserts) + m["close"]
    return (snippet[:pre.start("rows")] + "\n".join(lines) + snippet[pre.end("rows"):])


def _logging_listing(added: list, root: Path) -> str:
    """The leading answer: one code snippet per logging statement this change set
    actually added or modified — the same `.snippet` figure every other quoted line on
    this page uses (`extract-snippet.py`), not a second, invented code-block style — with
    the declaration lines of the values it logs pulled into the same block, and each
    logged value wearing its declared type as an IntelliJ-style hint.

    An empty list is not silence. `logextract.py` ran and genuinely found zero — see
    `logging_fragment`'s docstring for why that is itself the finding — so it renders as
    a sentence carrying the same weight as the snippets it replaces, never as a blank
    stretch of page that would read exactly like the scan never having run at all."""
    if not added:
        return ('<p class="lede"><b>None.</b> Not one logging statement was added or '
                'changed on the lines this change set touches.</p>')
    boxes = []
    # `new code` / `2 lines changed` — dropped on this tab only. Everywhere else the badge
    # answers "is this quoted block new, or an old one with a line in it?", which is a real
    # question about a snippet a reviewer did not choose. Here it is not: the gutter beside
    # the statement already marks the added lines with `+`, and every block on this tab is
    # here *because* the branch added or rewrote that logging line.
    BADGE_RE = re.compile(r'<span class="code-badge"[^>]*>[^<]*</span>')
    for h in added:
        # The bar's own link opens at the *first* line of the window, which with origin
        # lines pulled in is the declaration rather than the statement. Re-aimed at the
        # hit's own line and column: that is where a reader clicking a logging box expects
        # to land.
        snippet = snippet_html(_logging_ref(h), None, root, exact=True,
                               link_at=(h["line"], h.get("column", 1)))
        snippet = BADGE_RE.sub("", snippet, count=1)
        boxes.append(_hint_arguments(snippet, h))
    return "".join(boxes)

def logging_fragment(block, root: Path):
    """What this change set will say for itself at 3 a.m., found structurally.

    Grep cannot answer this question. `log.info(...)` is a hit and `Math.log(x)` is not, and
    only the syntax tree plus a symbol table of what is actually a logger can tell them
    apart — which is what `logextract.py` does, and why it is a script and not a regex.

    The zero case is the point, not an edge case: a change set that logs nothing is not an
    empty section — it is a finding, said as a plain sentence rather than shown as an
    absence a reader could mistake for the scan not having run. There is no table of every
    touched file behind this any more — a table where almost every row read `0 logging`
    was exactly the noise a reviewer had to read past to find the one or two lines that
    were the actual answer, on every change set, not just the pathological ones — so a
    reviewer who wants proof the scan ran gets that from the tab actually rendering
    (weight 1, a real sentence) rather than from an inventory of the files it walked. A
    dropped tab (ast-grep missing, or the scan failing outright) is the other thing this
    must never be confused with — that path returns `("", 0, 0)` below and the tab
    disappears with a loud line in the build log, which is a different, visible failure
    mode from a real, rendered zero."""
    paths = block.get("paths") or ["."]
    base = subprocess.run(["git", "merge-base", block.get("base", "origin/main"), "HEAD"],
                          cwd=root, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "logging.json"
        proc = subprocess.run(
            [sys.executable, str(LOGEXTRACT), *paths, "--root", str(root), "--repo", str(root),
             "--since", base, "--json", str(report)],
            cwd=root, capture_output=True, text=True)
        if proc.returncode != 0 or not report.is_file():
            # ast-grep is a binary, not a Python dependency, so a machine without it is a
            # real case. Say which tool is missing rather than quietly reporting "no
            # logging" — a false all-clear is the one answer this tab must never give.
            print("[review] logextract.py failed — dropping the logging tab:\n"
                  + proc.stderr.strip()[-500:], file=sys.stderr)
            return "", 0, 0
        payload = json.loads(report.read_text(encoding="utf-8"))

    added = payload.get("changed", payload["all"])["logging"]
    # No heading: the tab is called Logging and the panel opens with it — a `<h2>Logging
    # added/updated` under a selected `Logging` pill is the tab's own label, said twice.
    # The anchor it used to carry rides on the lede instead, so `#logging-added` still
    # lands where it always did.
    #
    # No authored lede either. What stood here was three sentences of methodology (grep
    # vs. ast-grep, `Math.log(x)`, walking the syntax tree) that a reader can see for
    # themselves in the blocks below: every one of them quotes the lines it traced. What
    # they cannot see is *which* libraries were looked for — so that is the one fact left
    # standing, in a line, with the list itself one hover away rather than spent on the
    # page. It is computed, never typed: `logging_libraries` reads the packages back out
    # of the very rule `logextract.py` runs, so the hover cannot claim a library the scan
    # does not actually search for.
    head = (f'<p class="lede" id="{html.escape(block.get("id", "logging-added"))}">'
            f'Uses of '
            f'<span class="dfn" data-tip-side="right"'
            f' data-tip-html="{html.escape(logging_libraries_tip(), quote=True)}">'
            f'common Java logging libraries</span>.</p>')
    body = ""
    # No header bar and no surrounding card any more: no heading repeating "logging", no
    # count pill, no `path, base…HEAD` provenance line — the tab's own title already says
    # "logging", and the snippets below say what they are without a caption restating it.
    # The snippets sit directly on the page, exactly like every other
    # block's content on this tab. `_logging_listing` alone decides what shows: the real
    # snippets, or the explicit "None." sentence for a genuine zero.
    listing = _logging_listing(added, root)
    frag = (
        head + body + listing
        # "What does this service log today" is the question a reader asks in the same
        # breath as "what did this branch add", and `System.out` is a third answer that must
        # not be counted as a fourth logger. Both are context, both sit under the finding.
        + _logging_aside(block.get("existing"), len(payload["all"]["logging"]),
                         "pre-existing logging", root, payload["all"]["logging"])
        + _logging_aside(block.get("console"), len(payload["all"]["antipattern"]),
                         "console-output", root, payload["all"]["antipattern"])
    )
    # Weight is 1 whenever the scan actually ran — never tied to the header bar or the
    # card that used to wrap the snippets, both gone now, and never computed from `n`
    # either. The zero is not "we looked at unrelated context and nothing moved" — the
    # tab that gets struck through — it is a statement *about this diff*: twelve touched
    # Java files, five hundred added lines, and not one of them will say anything at 3
    # a.m. Striking that through would file the finding as a non-event, and dropping the
    # tab (weight 0) would be worse: that reading is reserved for the one case that is
    # not a real answer — `ast-grep` missing or the scan crashing outright, handled above
    # by returning `("", 0, 0)` before any of this runs.
    assert listing, "logging_fragment must always have content: a real listing or the zero sentence"
    return frag, 1, 1
