"""Wire the quoted code on the page to itself: a step to its glue, a call to its callee.

A Gherkin scenario is four sentences that say nothing about how they run, and the page
already carries the code that runs them — one window down, fully expanded, so the reader
who came to read four sentences meets sixty lines of Playwright first. The two facts are
both wanted and they are not wanted at the same time.

So the sentence becomes the handle. `When I book a visit for that pet with "Helen Leary"
attending` is underlined, the step definition that matches it is folded to one line, and
clicking the sentence unfolds it. The same rule then applies one level down, because the
same complaint applies: the glue calls `bookVisit`, `bookVisit` is quoted three windows
away, and following that by eye means scrolling and matching a name. Every identifier a
quoted window *defines* is linked from every quoted window that *mentions* it.

Nothing here guesses at code that is not on the page. The index is built from the windows
the report already chose to quote, so a name with no window behind it stays plain text
rather than becoming a link to nowhere — the reader can tell what is followable by looking.

This runs as a post-pass over the assembled document, which is the one place that sees
every quoted window at once: the snippets `extract-snippet.py` cut, and the excerpts the
requirements map carries as JSON and renders in the browser when a row is opened. The
first are rewritten in the markup, the second inside their own JSON, and both come out
carrying the same `<a class="xref">`.

Doing it here, at build time, rather than in the page's own JavaScript, is the same trade
`extract-snippet.py` makes for highlighting: the language, the line numbers and the whole
set of quoted windows are known now, the guide must render offline as one emailed file,
and a regex over Pygments output is a thing a test can pin. What is left for the browser
is what only a browser can do — fold, unfold, scroll and flash.
"""
from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path

# --- reading one line of already-highlighted HTML -------------------------------------
# Every quoted line on this page arrives as Pygments output: text broken into <span>s at
# token boundaries that have nothing to do with where a name starts or ends. So a name is
# read off the *plain* text and written back into the *marked-up* text, and the two are
# kept in step by counting characters — an entity (`&#39;`) being one of them, because it
# is one character to the reader.
TAG = re.compile(r"<[^>]+>")
SPLIT_TAGS = re.compile(r"(<[^>]+>)")
CHAR = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);|.", re.S)


def plain(line: str) -> str:
    """The source line a marked-up line is showing, tags gone and entities resolved."""
    return _html.unescape(TAG.sub("", line))


def wrap(line: str, start: int, end: int, opener: str) -> str:
    """Wrap `[start, end)` of the plain text in `opener` … `</a>`, in the marked-up line.

    A range routinely crosses a token boundary — a Gherkin sentence with a quoted argument
    in it is three spans, and `page.locator` is two — and an anchor opened inside one span
    and closed inside the next is not a tree. So the range is wrapped **once per text run**
    instead: several anchors, the same target, sitting flush against each other. They read
    as one link because they carry one underline and one background, and the markup stays
    well-formed however Pygments happened to split the line.
    """
    if end <= start:
        return line
    out, pos, open_now = [], 0, False
    for tok in SPLIT_TAGS.split(line):
        if not tok:
            continue
        if tok.startswith("<"):
            # A tag inside the range closes the anchor and the next text run reopens it.
            if open_now:
                out.append("</a>")
                open_now = False
            out.append(tok)
            continue
        for piece in CHAR.findall(tok):
            inside = start <= pos < end
            if inside and not open_now:
                out.append(opener)
                open_now = True
            elif not inside and open_now:
                out.append("</a>")
                open_now = False
            out.append(piece)
            pos += 1
        if open_now:
            out.append("</a>")
            open_now = False
    return "".join(out)


# --- what a window defines ------------------------------------------------------------
# Deliberately shallow: a regex per language over one line, not a parser. The index only
# has to be right about names it *does* claim — a definition it misses costs a link that
# is not offered, which the reader never sees, while a definition it invents costs a link
# that lands on the wrong code, which is a lie about the source. Everything below is
# therefore anchored, requires the shape of a declaration, and refuses the keywords that
# make `if (ready) {` look like a method called `if`.
NOT_A_NAME = {
    "if", "for", "while", "switch", "catch", "do", "else", "try", "return", "new",
    "function", "class", "case", "with", "await", "yield", "typeof", "delete", "void",
    "in", "of", "super", "this", "throw", "synchronized", "record", "enum", "interface",
    "assert", "import", "package", "extends", "implements", "instanceof", "constructor",
    # The frameworks' own words. Every one of them is a call whose last argument is a
    # callback, so the line ends in `{` and reads to the pattern below exactly like a
    # method declaration — `When('…', async function (this: World) {` was offering the
    # reader a link to "where `When` is defined", which is inside node_modules and is
    # never what they were asking. They are also the one set of names a reader already
    # knows the meaning of, so there is nothing a link could add.
    "Given", "When", "Then", "And", "But", "Before", "After",
    "describe", "it", "test", "expect", "beforeEach", "afterEach", "beforeAll", "afterAll",
}

JS_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*[(<]")
# `const open_owner_detail_page = async (page) => …` — a name bound to a callable, which
# is the same thing to a reader following a call. The arrow (or the word `function`) has
# to be on the line: `const vetName = (await cell.textContent()).trim()` opens a bracket
# in the same place a parameter list would, and taking that for a definition made every
# later mention of a local variable a link to the line that computed it.
JS_BOUND_FN = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*"
    r"(?:async\s+)?(?:function\b"
    r"|\([^()]*\)\s*(?::[^=]+?)?=>"
    r"|[A-Za-z_$][\w$]*\s*=>)")
# A method inside a class or an object literal: name, parameter list, and an opening brace
# on the same line. The trailing `{` is what keeps a call on its own line out of the index.
METHOD = re.compile(
    r"^\s*(?:@[\w.]+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|private|protected|static|final|abstract|default|native|async|"
    r"synchronized|override|readonly|get|set)\s+)*"
    r"(?:<[^<>]+>\s*)?"
    r"(?:[\w.$]+(?:<[^;]*?>)?(?:\[\])?\s+)?"
    r"([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?::[^{;]+)?(?:throws\s[\w\s,.]+)?\{\s*$")
PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")

# Two characters is not a name a reader loses the thread of, and `id`, `of` and `to` turn
# up inside enough identifiers-that-are-not-calls to be pure noise.
MIN_NAME = 3

BRACE_LANGS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".java", ".kt", ".cs", ".go"}

# Which suffixes are the same language for the purpose of resolving a name. Nothing on this
# page calls across one of these boundaries, and a name that appears on both sides of one is
# a coincidence: the TypeScript glue's local `bookVisit` helper resolved to a `private int
# bookVisit(VisitDto)` quoted from a Spring controller three tabs away, and the link read
# exactly like the true ones beside it. A missed link costs the reader a search they were
# already going to do; a confident wrong one costs them the wrong answer.
FAMILY = {".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js", ".mjs": "js"}


def family(suffix: str) -> str:
    return FAMILY.get(suffix, suffix)


def defined_names(suffix: str, lines: list[str]) -> dict[str, int]:
    """`{name: 1-based index within `lines`}` for everything this window declares."""
    found: dict[str, int] = {}
    for i, text in enumerate(lines, 1):
        names = []
        if suffix == ".py":
            m = PY_DEF.match(text)
            if m:
                names.append(m[1])
        if suffix in BRACE_LANGS:
            for rx in (JS_FUNCTION, JS_BOUND_FN):
                m = rx.search(text)
                if m:
                    names.append(m[1])
            m = METHOD.match(text)
            if m:
                names.append(m[1])
        for name in names:
            if len(name) >= MIN_NAME and name not in NOT_A_NAME and name not in found:
                found[name] = i
    return found


# --- what a window binds a Gherkin sentence to -----------------------------------------
# Both dialects this pipeline meets: cucumber-js, where the pattern is the first argument
# of `When(...)`, and cucumber-jvm, where it is the argument of an `@When` annotation on
# the method below it. The keyword itself is not part of the match — Cucumber binds a step
# by its text alone, so a `Then` written as an `And` still finds its definition.
JS_STEP = re.compile(
    r"^\s*(?:Given|When|Then|And|But)\s*\(\s*"
    r"(?:(?P<q>['\"])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)"
    r"|/(?P<rx>(?:\\.|[^/])+)/)")
JAVA_STEP = re.compile(
    r"^\s*@(?:Given|When|Then|And|But)\s*\(\s*\"(?P<text>(?:\\.|[^\"])*)\"\s*\)")

# `{string}` is quoted in the scenario and the quotes belong to the argument, which is why
# the sentence a reader sees carries them and the pattern does not.
PLACEHOLDER = {
    "string": r"(?:\"[^\"]*\"|'[^']*')",
    "int": r"-?\d+",
    "float": r"-?\d+(?:[.,]\d+)?",
    "word": r"\S+",
    "": r".+?",
}


def _unescape_source_string(text: str) -> str:
    """`that pet\\'s history` as written in the source is `that pet's history` as matched."""
    return re.sub(r"\\(.)", r"\1", text)


def step_pattern(text: str, is_regex: bool) -> re.Pattern | None:
    """A Cucumber expression (or a bare regex) as something a scenario line can be tried
    against. Returns None for anything that will not compile, so a pattern this does not
    understand costs the link and never the build."""
    if is_regex:
        try:
            return re.compile(text)
        except re.error:
            return None
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "{":
            close = text.find("}", i)
            if close > -1:
                out.append(PLACEHOLDER.get(text[i + 1:close], r".+?"))
                i = close + 1
                continue
        if ch == "(":
            # Cucumber's optional text: `I have {int} cucumber(s)`.
            close = text.find(")", i)
            if close > -1:
                out.append("(?:" + re.escape(text[i + 1:close]) + ")?")
                i = close + 1
                continue
        out.append(re.escape(ch))
        i += 1
    try:
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def defined_steps(suffix: str, lines: list[str]) -> list[tuple[re.Pattern, int]]:
    """`[(pattern, 1-based index within `lines`)]` for the step definitions in a window."""
    out = []
    for i, text in enumerate(lines, 1):
        if suffix == ".java":
            m = JAVA_STEP.match(text)
            pattern = step_pattern(_unescape_source_string(m["text"]), False) if m else None
        elif suffix in (".ts", ".js", ".tsx", ".jsx", ".mjs"):
            m = JS_STEP.match(text)
            if not m:
                pattern = None
            elif m["rx"] is not None:
                pattern = step_pattern(m["rx"], True)
            else:
                pattern = step_pattern(_unescape_source_string(m["text"]), False)
        else:
            pattern = None
        if pattern is not None:
            out.append((pattern, i))
    return out


# A scenario line: the keyword, then the sentence Cucumber matches on.
GHERKIN_STEP = re.compile(r"^(?P<lead>\s*(?:Given|When|Then|And|But|\*)\s+)(?P<text>\S.*?)\s*$")
PYGMENTS_LANGS = {".feature": "gherkin"}


# --- the windows the page is quoting ---------------------------------------------------
# Two shapes, one meaning. `extract-snippet.py` writes a <figure> whose <pre> holds one
# `.ln-row` per source line; the requirements map ships the same lines as JSON and builds
# its rows in the browser when a test is opened. Both are read into the same `Window` and
# written back to where they came from.
FIGURE = re.compile(r'<figure class="snippet"(?P<attrs>[^>]*)>(?P<body>.*?)</figure>', re.S)
FIG_PRE = re.compile(r'(?P<open><pre class="code[^"]*"><code>)(?P<rows>.*?)(?P<close></code></pre>)',
                     re.S)
FIG_HREF = re.compile(r'<a class="srcref srcbar-path" href="(?P<href>[^"]*)"')
ROW = re.compile(r'^(?P<open><span class="ln-row[^"]*">'
                 r'(?:<span class="dm">[^<]*</span>)?<span class="ln">(?P<no>\d+)</span>)'
                 r'(?P<code>.*)(?P<close></span>)$')
RM_DATA = re.compile(r'(?P<open><script[^>]*class="rm-data"[^>]*>)(?P<json>.*?)(?P<close></script>)',
                     re.S)
# `vscode://file//abs/path.ts:38:1` → the path, which is all this needs: the suffix says
# which language's declarations to look for, and the line is already carried beside it.
VSCODE_PATH = re.compile(r"^vscode://file/*(?P<path>/[^:]*?)(?::\d+)?(?::\d+)?$")


class Window:
    """One quoted range of one file, wherever on the page it is quoted from."""

    def __init__(self, wid, rel, href, nums, code, group, part_index):
        self.wid = wid
        self.rel = rel
        self.href = href
        self.suffix = Path(rel).suffix
        self.nums = nums          # the file's own line numbers, one per entry in `code`
        self.code = code          # marked-up source lines, edited in place
        self.group = group        # the test whose accordion this part belongs to, or None
        self.part_index = part_index   # where it sits in that accordion; -1 for a snippet
        self.text = [plain(c) for c in code]
        self.defs = defined_names(self.suffix, self.text)
        self.steps = defined_steps(self.suffix, self.text)
        self.linked_from_elsewhere = False


def _line_href(href: str, line: int) -> str:
    """The same file, aimed at the line the link is about rather than at the window's top."""
    swapped, n = re.subn(r":\d+(?::\d+)?$", f":{line}:1", href)
    return swapped if n else href


def _face(text: list[str]) -> str:
    """The one line a folded window shows: its first line of actual code.

    A fold that says only `⋯` makes the reader open it to find out whether it was the one
    they wanted. Its signature says that without unfolding anything.

    Kept whole, not cut to fit. The stub sits in the empty half of a source bar and clips
    with an ellipsis wherever that runs out, which is a width the browser knows and this
    does not — and what is clipped is on the stub's own tooltip, so the line is never
    somewhere the reader cannot get to it. The cap is only a bound on the index."""
    for line in text:
        stripped = line.strip()
        if stripped:
            return stripped[:400]
    return ""


def _collect_figures(doc: str) -> tuple[list[tuple], list[Window]]:
    """`[(match, rows)]` and the windows read out of them, in document order."""
    found, windows = [], []
    for m in FIGURE.finditer(doc):
        body = m["body"]
        href_m = FIG_HREF.search(body)
        pre_m = FIG_PRE.search(body)
        if not href_m or not pre_m:
            continue
        href = _html.unescape(href_m["href"])
        seen = VSCODE_PATH.match(href)
        rel = seen["path"] if seen else href
        rows, nums, code = [], [], []
        for raw in pre_m["rows"].split("\n"):
            row = ROW.match(raw)
            rows.append(row)
            if row:
                nums.append(int(row["no"]))
                code.append(row["code"])
        if not code:
            continue
        window = Window("", rel, href, nums, code, None, -1)
        found.append((m, pre_m, rows, window))
        windows.append(window)
    return found, windows


def _collect_parts(doc: str) -> tuple[list[tuple], list[Window]]:
    """The excerpts the requirements map carries as JSON, and where to write them back."""
    found, windows = [], []
    for m in RM_DATA.finditer(doc):
        try:
            data = json.loads(m["json"])
        except (json.JSONDecodeError, TypeError):
            continue
        mine = []
        for tid, test in (data.get("tests") or {}).items():
            for index, part in enumerate(test.get("parts") or []):
                lines = part.get("html")
                href = part.get("href") or ""
                label = part.get("label") or ""
                first = part.get("from")
                if not lines or not href or not isinstance(first, int):
                    continue
                rel = label.rsplit(":", 1)[0] if ":" in label else label
                w = Window("", rel, href, list(range(first, first + len(lines))),
                           list(lines), tid, index)
                mine.append((part, w))
                windows.append(w)
        if mine:
            found.append((m, data, mine))
    return found, windows


# --- the index, and the links drawn from it --------------------------------------------


def _pick(candidates, here: Window):
    """Which quoted definition a mention resolves to.

    Never across languages, and then near before far: the same file first, because a name
    declared in the file being read is the one meant; then the test's own accordion, whose
    parts are what the reader has open; and only then the rest of the page, which is how a
    step in one test reaches glue quoted under another."""
    same_lang = [c for c in candidates
                 if c[0] is not here and family(c[0].suffix) == family(here.suffix)]
    for pool in ([c for c in same_lang if c[0].rel == here.rel],
                 [c for c in same_lang if here.group is not None and c[0].group == here.group],
                 same_lang):
        if pool:
            return pool[0]
    return None


def _anchor(target: Window, line: int, tip: str) -> str:
    return (f'<a class="xref" data-xref="{target.wid}" data-xref-line="{line}"'
            f' href="{_html.escape(_line_href(target.href, line), quote=True)}"'
            f' data-tip="{_html.escape(tip, quote=True)}">')


def _link(windows: list[Window]) -> int:
    names: dict[str, list] = {}
    steps: list[tuple] = []
    for w in windows:
        for name, i in w.defs.items():
            names.setdefault(name, []).append((w, w.nums[i - 1]))
        for pattern, i in w.steps:
            steps.append((pattern, w, w.nums[i - 1]))

    drawn = 0
    for w in windows:
        for i, text in enumerate(w.text):
            edits = []
            if w.suffix == ".feature":
                m = GHERKIN_STEP.match(text)
                if m:
                    for pattern, tw, tline in steps:
                        if tw is w or not pattern.match(m["text"]):
                            continue
                        edits.append((m.start("text"), m.end("text"), tw, tline,
                                      "Show the step definition this sentence runs"))
                        break
            else:
                for name, candidates in names.items():
                    if w.defs.get(name) == i + 1:
                        continue          # this line *is* the definition
                    if name not in text:
                        continue
                    target = _pick(candidates, w)
                    if target is None:
                        continue
                    for hit in re.finditer(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text):
                        edits.append((hit.start(), hit.end(), target[0], target[1],
                                      f"Show where {name} is defined"))
            # Overlaps come from one name being a substring match of another's range; the
            # first one wins, and the loser stays plain text rather than nesting.
            edits.sort(key=lambda e: (e[0], -e[1]))
            kept, last = [], -1
            for edit in edits:
                if edit[0] >= last:
                    kept.append(edit)
                    last = edit[1]
            for start, end, target, tline, tip in reversed(kept):
                w.code[i] = wrap(w.code[i], start, end, _anchor(target, tline, tip))
                target.linked_from_elsewhere = True
                drawn += 1
    return drawn


def cross_link(doc: str) -> str:
    """Draw every link the page's own quoted code justifies, and fold what they point at.

    Returns the document unchanged when there is nothing to draw — a report that quotes one
    file has no cross-references, and shipping an empty index would only make the page
    heavier."""
    figures, fig_windows = _collect_figures(doc)
    parts, part_windows = _collect_parts(doc)
    windows = fig_windows + part_windows
    if not windows:
        return doc

    # One id per quoted *range*, not per place it is quoted: the same window under two
    # tests is the same code, and giving it two ids would leave one of them naming an
    # element the page never renders.
    ids: dict[str, str] = {}
    for w in windows:
        w.wid = ids.setdefault(w.href, f"xr{len(ids) + 1}")

    if not _link(windows):
        return doc

    # Which part folds. Only inside the requirements map, and only a part something on the
    # page points at: a folded window a reader has no handle for is a window they have to
    # know to click. The first part of a test never folds — it is the test, and the reader
    # opened the row to read it.
    index = {}
    for w in part_windows:
        if w.linked_from_elsewhere and w.part_index > 0:
            index[w.href] = {"id": w.wid, "shut": True, "face": _face(w.text)}
        else:
            index.setdefault(w.href, {"id": w.wid, "shut": False, "face": _face(w.text)})

    # --- write both sides back ---------------------------------------------------------
    # As one list of splices applied from the end of the document forwards: the two kinds
    # of quoted block are interleaved in the page, and rewriting one kind first would move
    # every offset the other kind was matched at.
    splices = []
    for m, pre_m, rows, w in figures:
        out, k = [], 0
        for raw, row in zip(pre_m["rows"].split("\n"), rows):
            if not row:
                out.append(raw)
                continue
            out.append(row["open"] + w.code[k] + row["close"])
            k += 1
        body = (m["body"][:pre_m.start()] + pre_m["open"] + "\n".join(out) + pre_m["close"]
                + m["body"][pre_m.end():])
        splices.append((m.start(), m.end(),
                        f'<figure class="snippet"{m["attrs"]} data-xref-id="{w.wid}">'
                        f"{body}</figure>"))
    for m, data, mine in parts:
        for part, w in mine:
            part["html"] = w.code
        # `</script>` inside a JSON string would end the block that holds it; nothing here
        # writes one, and escaping the slash costs nothing to be sure of that.
        splices.append((m.start(), m.end(),
                        m["open"] + json.dumps(data).replace("</", "<\\/") + m["close"]))
    for start, end, text in sorted(splices, reverse=True):
        doc = doc[:start] + text + doc[end:]

    script = ('<script type="application/json" id="xref-index">'
              + json.dumps(index).replace("</", "<\\/") + "</script>")
    return doc.replace("<body>", "<body>\n" + script, 1)
