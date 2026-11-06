"""Which DB fixture each E2E or Spring test starts from, and the colour each fixture wears.

A fixture is a named dataset the review environment can reset its database to — a dataset
of its own, written on an empty database, never a delta on the seed: a ``<name>.sql`` in a
folder called ``fixtures`` (petclinic keeps them in
``petclinic-backend/src/main/resources/db/fixtures``, next to the seed, and its reset
sidecar draws one Demo-tab button per file). The seed itself is the fixture called Default. The colours are the
project's to choose, in a ``fixture-colors.json`` beside those files::

    {"minimal": "#2fa84f"}

Beside the SQL and not in ``.human-review/``: whoever adds ``blue.sql`` is standing in
that folder, and the reset sidecar only ever reads ``*.sql`` from it, so the map rides
along harmlessly. The map is optional: a fixture it does not name takes a free colour of
``FIXTURE_PALETTE`` picked by a hash of its name (`fixture_hue`), so it wears the same
colour on every build and on every page, whatever else the folder holds. The seed is
always ``FIXTURE_SEED`` grey.

Per test the answer is read off the test's own code, never guessed from its name — and
the answer is only ever what the test DECLARES (Victor, 21 Oct 2026: "If a test does not use
a certain DB fixture, it should start from an empty slate"). A test that declares nothing
starts on an empty database and gets no dot; one that declares a fixture wears its dot:

1. an E2E test: a ``@fixture:<name>`` tag (Cucumber, on the scenario or its Feature; or
   Playwright's ``{tag: ...}``), a ``test.use({dbFixture: '<name>'})`` in scope (the file's,
   or the innermost ``test.describe`` around the test), or a call that loads one —
   ``__reset/<name>``, ``loadFixture('<name>')``, ``resetTo('<name>')``, ``fixture('<name>')``.
   ``default``/``seed`` name the seed;
2. a JVM test on a Spring context (``@SpringBootTest``, ``@DataJpaTest``, Zonky's
   ``@AutoConfigureEmbeddedDatabase``…): ``@Sql`` of a ``fixtures/<name>.sql`` or of the
   seed (the method's, else the class's), directly or through a meta-annotation declared in
   the module's tests (petclinic's ``@DefaultFixture``); otherwise what Flyway loads at
   boot — the seed when ``spring.flyway.locations`` (application.properties, the test
   resources' copy shadowing it, ``config/application.*`` on top, each ``@ActiveProfiles``
   file, the annotation's own ``properties``) names a seed location, nothing when it does
   not. That is configuration, read, not assumed. A test with no Spring context touches no
   database and is not listed;
3. a JVM Cucumber scenario (a ``.feature`` in a module whose Flyway config is read as in 2):
   its tag, else the seed when Flyway loads one and no ``@Before`` hook truncates tables
   before each scenario (petclinic's old ``functional/DatabaseHooks.java``), else nothing.

Two fixtures named by one test is the one case left unsaid: not ours to pick.

"The code the test runs" is, for a Cucumber scenario, the feature's tags and the step
definitions its Background and its own steps match; for a Playwright test, its body, the
file's hooks and helpers, and the local modules it imports (DSLs, page objects) — not
``support/``, which every test imports and which says nothing about any one of them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .util import _git

FIXTURE_SEED = "#8b929c"
# Seven, readable on the dark and the light theme alike, and far enough apart in hue that
# two dots side by side never read as one colour.
FIXTURE_PALETTE = ("#2fa84f", "#3b82f6", "#e5a50a", "#d9485f", "#9b59d0", "#14a3a3", "#e8772e")
FIXTURE_COLORS_FILE = "fixture-colors.json"

_FX_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_FX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|(?:rgb|hsl)a?\([0-9\s.,%]+\)")
_FX_SPEC = re.compile(r"\.(?:spec|test|e2e)\.[cm]?[jt]sx?$")


def _fx_ls(root: Path) -> list[str]:
    listed = _git(root, "ls-files")
    if listed:
        return listed.splitlines()
    return [str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts]


def fixture_hue(name: str, palette) -> str:
    """The palette colour a fixture's name hashes to. Spelt out rather than ``hash()``, which
    Python salts per process, and mirrored by ``fixtures.js`` for a name the build never saw."""
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return palette[h % len(palette)]


def fixture_dirs(files: list[str]) -> list[str]:
    return sorted({str(Path(f).parent) for f in files
                   if Path(f).parent.name == "fixtures" and f.endswith(".sql")})


def fixture_colors(root: Path, files: list[str] | None = None) -> dict:
    """``{"seed": grey, "colors": {name: colour}}`` for every fixture in the project.

    Configured colours first; the rest hash their name onto the palette, minus any colour
    the configuration already spent, so a fallback never repeats a chosen one."""
    files = _fx_ls(root) if files is None else files
    names, chosen = [], {}
    for d in fixture_dirs(files):
        for f in sorted(files):
            p = Path(f)
            if str(p.parent) == d and p.suffix == ".sql" and _FX_NAME.fullmatch(p.stem):
                if p.stem not in names:
                    names.append(p.stem)
        try:
            cfg = json.loads((root / d / FIXTURE_COLORS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        for k, v in (cfg.items() if isinstance(cfg, dict) else ()):
            if isinstance(v, str) and _FX_COLOUR.fullmatch(v.strip()):
                chosen[str(k)] = v.strip()
    seed = chosen.pop("seed", None) or chosen.pop("default", None) or FIXTURE_SEED
    free = [c for c in FIXTURE_PALETTE if c.lower() not in {v.lower() for v in chosen.values()}]
    free = free or list(FIXTURE_PALETTE)
    colors = {n: chosen.get(n) or fixture_hue(n, free) for n in names}
    for n, c in chosen.items():
        colors.setdefault(n, c)
    return {"seed": seed, "colors": colors, "palette": free}


def demo_fixtures(root: Path | None, files: list[str] | None = None) -> list[tuple[str, str]]:
    """``[("", seed colour), (name, colour), …]``: the seed (Default) first, then every
    ``<name>.sql`` the project's fixtures folders hold, in folder order — the Demo tab's
    "DB Fixture:" row. Read off the files at build time, never off the running app: the
    row is on screen with the app down, and an instance started from an older image must
    not shrink the list (8 Oct 2026: :7655 listed only Default while green.sql, now minimal.sql, was there).
    A name only ``fixture-colors.json`` mentions has no SQL to load and is not listed."""
    if root is None:
        return [("", FIXTURE_SEED)]
    root = Path(root)
    files = _fx_ls(root) if files is None else files
    reg = fixture_colors(root, files)
    on_disk = {Path(f).stem for f in files
               if Path(f).parent.name == "fixtures" and f.endswith(".sql")}
    return [("", reg["seed"])] + [(n, c) for n, c in reg["colors"].items() if n in on_disk]


def _fx_strip_comments(src: str) -> str:
    """Comments out, strings kept: a comment is the author musing, a string is what runs."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1]); i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i); i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2); i = n if j < 0 else j + 2
        else:
            out.append(c); i += 1
    return "".join(out)


def _fx_explicit(text: str, known: set[str]) -> set[str]:
    found = set()
    for m in re.finditer(r"@fixture[:=-]([a-z0-9][a-z0-9-]*)", text):
        found.add(m.group(1))
    for m in re.finditer(r"__reset/([a-z0-9][a-z0-9-]*)", text):
        found.add(m.group(1))
    for m in re.finditer(r"\b(?:loadFixture|resetTo|useFixture|fixture)\(\s*['\"`]"
                         r"([a-z0-9][a-z0-9-]*)['\"`]", text):
        found.add(m.group(1))
    for m in re.finditer(r"\bdbFixture\s*:\s*['\"`]([a-z0-9][a-z0-9-]*)['\"`]", text):
        found.add(m.group(1))
    out = set()
    for f in found:
        if f in ("seed", "default"):
            out.add("seed")
        elif f in known:
            out.add(f)
    return out


def _fx_verdict(tags: str, code: str, known: set[str]) -> str | None:
    """The fixture the test declares — its name, or ``"seed"`` — ``""`` when it declares
    none (it starts empty), None when it names two."""
    hits = _fx_explicit(tags + "\n" + code, known)
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        return None                          # two fixtures named: not ours to pick
    if re.search(r"(?<![\w-])@seed(?![\w-])", tags):
        return "seed"
    return ""


# --- Cucumber ----------------------------------------------------------------------------

_FX_STEPDEF = re.compile(r"\b(?:Given|When|Then|And|But|defineStep)\(\s*(['\"`])((?:\\.|(?!\1).)*)\1")


def _fx_expr_to_regex(expr: str) -> re.Pattern | None:
    out, i = [], 0
    while i < len(expr):
        c = expr[i]
        if c == "{":
            j = expr.find("}", i)
            kind = expr[i + 1:j] if j > 0 else ""
            out.append({"word": r"\S+", "string": r"(?:\"[^\"]*\"|'[^']*')",
                        "int": r"-?\d+", "float": r"-?[\d.]+"}.get(kind, r".*"))
            i = j + 1 if j > 0 else len(expr)
        else:
            out.append(re.escape(c)); i += 1
    try:
        return re.compile("^" + "".join(out) + "$")
    except re.error:
        return None


def _fx_step_defs(root: Path, files: list[str]) -> list[tuple[re.Pattern, str]]:
    defs = []
    for f in files:
        if not re.search(r"\.[cm]?[jt]s$", f) or "node_modules" in f:
            continue
        try:
            src = (root / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "@cucumber/cucumber" not in src:
            continue
        code = _fx_strip_comments(src)
        ms = list(_FX_STEPDEF.finditer(code))
        for k, m in enumerate(ms):
            rx = _fx_expr_to_regex(m.group(2).replace("\\'", "'").replace('\\"', '"'))
            if rx is not None:
                end = ms[k + 1].start() if k + 1 < len(ms) else len(code)
                defs.append((rx, code[m.start():end]))
    return defs


_FX_KW = re.compile(r"^\s*(?:Given|When|Then|And|But|\*)\s+(.*)$")
_FX_BLOCK = re.compile(r"^\s*(Feature|Background|Scenario Outline|Scenario Template|Scenario|"
                    r"Example|Rule|Examples|Scenarios):")


def _fx_feature_tests(rel: str, src: str, defs, known: set[str]) -> dict[str, str]:
    lines = src.splitlines()
    feature_tags, pending, background = "", "", []
    scenarios = []                              # (line, tags, steps)
    cur = None
    for no, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("@"):
            pending += " " + s
            continue
        m = _FX_BLOCK.match(line)
        if m:
            kind = m.group(1)
            if kind == "Feature":
                feature_tags, cur = pending, None
            elif kind == "Background":
                cur = background
            elif kind.startswith("Scenario") or kind == "Example":
                cur = []
                scenarios.append((no, pending, cur))
            else:
                cur = None if kind in ("Examples", "Scenarios") else cur
            pending = ""
            continue
        k = _FX_KW.match(line)
        if k and cur is not None:
            cur.append(re.sub(r"<[^>]+>", "X", k.group(1).strip()))
    out = {}
    for no, tags, steps in scenarios:
        code = []
        for step in background + steps:
            for rx, body in defs:
                if rx.match(step):
                    code.append(body)
                    break
        own = _fx_explicit(tags, known)           # the scenario's own tag beats its Feature's
        v = own.pop() if len(own) == 1 else _fx_verdict(feature_tags + " " + tags,
                                                         "\n".join(code), known)
        if v is not None:
            out[f"{rel}:{no}"] = v
    return out


# --- Playwright --------------------------------------------------------------------------

_FX_TEST = re.compile(r"^\s*test(?:\.(?:only|fixme|slow))?\(\s*['\"`]", re.M)


def _fx_block_end(code: str, start: int) -> int:
    """End of the `(...)` call opened at the first `(` from `start`."""
    i = code.find("(", start)
    depth = 0
    while 0 <= i < len(code):
        c = code[i]
        if c in "'\"`":
            j = i + 1
            while j < len(code) and code[j] != c:
                j += 2 if code[j] == "\\" else 1
            i = j
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(code)


def _fx_local_imports(root: Path, rel: str, code: str) -> str:
    here = (root / rel).parent
    texts = []
    for m in re.finditer(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""", code):
        spec = m.group(1)
        if "/support/" in spec + "/" or spec.startswith("./support"):
            continue
        for ext in ("", ".ts", ".js", ".tsx", ".mjs", "/index.ts", "/index.js"):
            p = (here / (spec + ext)).resolve()
            if p.is_file():
                try:
                    texts.append(_fx_strip_comments(p.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError):
                    pass
                break
    return "\n".join(texts)


_FX_DESCRIBE = re.compile(r"\btest\.describe(?:\.(?:serial|parallel|only|skip|fixme))?\(\s*['\"`]")
_FX_USE = re.compile(r"\btest\.use\(")


def _fx_blank(code: str, a: int, b: int) -> str:
    """`code` with [a, b) spaced out, newlines kept: offsets and line numbers stay put."""
    return code[:a] + re.sub(r"[^\n]", " ", code[a:b]) + code[b:]


def _fx_spec_tests(root: Path, rel: str, src: str, known: set[str]) -> dict[str, str]:
    if "@playwright/test" not in src and "trace-fixture" not in src:
        return {}
    code = _fx_strip_comments(src)
    # `test.use({...})` is scoped: the file's when at top level, else the innermost
    # `test.describe` around it, and an inner one wins. So each one is lifted out of the text
    # every test shares and handed only to the tests in its scope.
    describes = [(m.start(), _fx_block_end(code, m.start())) for m in _FX_DESCRIBE.finditer(code)]
    uses = []                                     # (scope start, scope end, text)
    for m in _FX_USE.finditer(code):
        end = _fx_block_end(code, m.start())
        scope = min((d for d in describes if d[0] < m.start() < d[1]),
                    key=lambda d: d[1] - d[0], default=(0, len(code)))
        uses.append((scope[0], scope[1], code[m.start():end]))
        code = _fx_blank(code, m.start(), end)
    # The comment-stripped text keeps every newline, so offsets map back to source lines
    # through the original — count them in `src` by matching the same test openers there.
    src_lines = [src[:m.start()].count("\n") + 1 + (m.group(0)[:1] == "\n")
                 for m in _FX_TEST.finditer(src)]
    spans = [(m.start(), _fx_block_end(code, m.start())) for m in _FX_TEST.finditer(code)]
    if len(spans) != len(src_lines):
        return {}
    shared, last = [], 0
    for a, b in spans:
        shared.append(code[last:a]); last = b
    shared.append(code[last:])
    shared_text = "".join(shared) + "\n" + _fx_local_imports(root, rel, code)
    out = {}
    for (a, b), line in zip(spans, src_lines):
        body = code[a:b]
        tags = " ".join(re.findall(r"@[\w:=-]+", body))
        use = min((u for u in uses if u[0] <= a < u[1] and _fx_explicit(u[2], known)),
                  key=lambda u: u[1] - u[0], default=None)
        v = _fx_verdict(tags + "\n" + (use[2] if use else ""), body + "\n" + shared_text, known)
        if v is not None:
            out[f"{rel}:{line}"] = v
    return out


# --- JVM (JUnit on Spring) -----------------------------------------------------------------
#
# Petclinic's API tests are `@SpringBootTest` + Zonky's embedded Postgres: Flyway builds the
# database at boot from `spring.flyway.locations`, which the tests' config/application.properties
# points at the schema and the reference rows only, and Zonky hands each test method a fresh
# copy. So a test starts empty — read off the configuration, not assumed — unless it loads a
# dataset itself: `@Sql(".../fixtures/<name>.sql")`, `@Sql` of the seed, or `@DefaultFixture`,
# the module's own annotation carrying that `@Sql`. On a configuration that does load the seed,
# every test starts on it, and says so with the seed's dot. A test that boots no Spring context
# touches no database and gets nothing.

_FX_JVM_TEST = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
#: A Spring test context, and so a datasource Flyway migrates. `@WebMvcTest` and friends
#: slice the database away and are not listed.
_FX_JVM_DB = re.compile(r"@(?:SpringBootTest|DataJpaTest|DataJdbcTest|JdbcTest|"
                        r"AutoConfigureEmbeddedDatabase|AutoConfigureTestDatabase)\b")
_FX_JVM_DECL = re.compile(r"(\w+)\s*\(")
_FX_PROP_FILES = ("application.properties", "application.yml", "application.yaml")


def _fx_props(text: str, yaml: bool) -> dict[str, str]:
    """The two Flyway keys out of a properties or YAML file — just those, by line."""
    out = {}
    if not yaml:
        for line in text.splitlines():
            m = re.match(r"\s*(spring\.flyway\.(?:locations|enabled))\s*[=:]\s*(.*?)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
        return out
    m = re.search(r"^\s*flyway:\s*\n((?:[ \t]+.*\n?)+)", text, re.M)
    if m:
        for key in ("locations", "enabled"):
            k = re.search(r"^\s*%s:\s*(.+?)\s*$" % key, m.group(1), re.M)
            if k:
                out[f"spring.flyway.{key}"] = k.group(1).strip("[]\"' ")
    return out


def _fx_inline_props(header: str) -> dict[str, str]:
    """`@SpringBootTest(properties = "spring.flyway.locations=…")`, `@TestPropertySource`."""
    out = {}
    for m in re.finditer(r"[\"'](spring\.flyway\.(?:locations|enabled))\s*[=:]\s*([^\"']*)[\"']",
                         header):
        out[m.group(1)] = m.group(2).strip()
    return out


def _fx_module_props(root: Path, module: str, profiles: list[str]) -> dict[str, str]:
    """What the test's context reads: `src/test/resources/application.*` shadows the main
    one on the classpath (the test classes come first), `config/application.*` is read on
    top of whichever won (Spring Boot's `classpath:/config/`, also first match), then each
    active profile's file."""
    props: dict[str, str] = {}
    for sub in ("", "config/"):
        for res in ("src/test/resources", "src/main/resources"):
            got = None
            for name in _FX_PROP_FILES:
                p = root / module / res / (sub + name)
                if p.is_file():
                    try:
                        got = _fx_props(p.read_text(encoding="utf-8"), p.suffix != ".properties")
                    except (OSError, UnicodeDecodeError):
                        got = {}
                    break
            if got is not None:
                props.update(got)
                break
    for prof in profiles:
        for res in ("src/main/resources", "src/test/resources"):
            for ext in (".properties", ".yml", ".yaml"):
                p = root / module / res / f"application-{prof}{ext}"
                if p.is_file():
                    try:
                        props.update(_fx_props(p.read_text(encoding="utf-8"), ext != ".properties"))
                    except (OSError, UnicodeDecodeError):
                        pass
    return props


def _fx_on_seed(root: Path, module: str, files: list[str], props: dict[str, str]) -> bool:
    """Whether Flyway, as configured, loads a seed: a location named `seed`, or one holding
    a `*seed*.sql` (petclinic: `classpath:db/seed` → `R__seed.sql`)."""
    if props.get("spring.flyway.enabled", "true").strip().lower() == "false":
        return False
    locs = props.get("spring.flyway.locations") or "classpath:db/migration"
    for loc in (x.strip() for x in locs.split(",") if x.strip()):
        path = re.sub(r"^(?:classpath\*?|filesystem):", "", loc).strip("/")
        if re.search(r"(?<![a-z])seed", Path(path).name, re.I):
            return True
        for res in ("src/main/resources", "src/test/resources"):
            d = f"{module}/{res}/{path}/"
            if any(f.startswith(d) and f.endswith(".sql") and "seed" in Path(f).name.lower()
                   for f in files):
                return True
    return False


def _fx_brace_end(code: str, start: int) -> int:
    """End of the `{...}` block opened at the first `{` from `start`."""
    i = code.find("{", start)
    if i < 0:
        return len(code)
    depth = 0
    while i < len(code):
        c = code[i]
        if c in "'\"":
            j = i + 1
            while j < len(code) and code[j] != c:
                j += 2 if code[j] == "\\" else 1
            i = j
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(code)


def _fx_sql_script(path: str, known: set[str]) -> str | None:
    """The fixture an `@Sql` script is: a project fixture's name, ``"seed"`` for the seed's
    own file (`db/seed/R__seed.sql`), None for any other script — not ours to name."""
    p = Path(path.split(":", 1)[-1])
    if p.parent.name == "fixtures" and p.stem in known:
        return p.stem
    if re.search(r"(?<![a-z])seed", p.name, re.I) or p.parent.name == "seed":
        return "seed"
    return None


def _fx_sql_names(text: str, known: set[str],
                  metas: dict[str, set[str] | None] | None = None) -> set[str] | None:
    """The fixtures `@Sql` loads before the test: a set of names, empty when there is no
    `@Sql`, None when it runs a script that is not one of the project's fixtures (not ours
    to name). Cleanup scripts (`AFTER_TEST_…`) say nothing about the start. An annotation
    in `metas` (one the module declares with `@Sql` on it) counts as the `@Sql` it carries."""
    names: set[str] = set()
    for m in re.finditer(r"@Sql\b(?!Config|Group)", text):
        end = _fx_block_end(text, m.end()) if text[m.end():].lstrip().startswith("(") else m.end()
        ann = text[m.end():end]
        if "AFTER_TEST" in ann:
            continue
        for sc in re.findall(r"[\"']([^\"']+\.sql)[\"']", ann):
            got = _fx_sql_script(sc, known)
            if got is None:
                return None
            names.add(got)
    for m in re.finditer(r"@(\w+)\b", text):
        if metas and m.group(1) in metas:
            got = metas[m.group(1)]
            if got is None:
                return None
            names |= got
    return names


def _fx_meta_annotations(root: Path, module: str, files: list[str],
                         known: set[str]) -> dict[str, set[str] | None]:
    """``{name: fixtures}`` for every annotation type the module's tests declare with an
    `@Sql` on it — petclinic's ``@DefaultFixture``, `@Sql` of the seed."""
    out: dict[str, set[str] | None] = {}
    for f in files:
        if not f.startswith(module + "/src/") or "/src/main/" in f or not f.endswith((".java", ".kt")):
            continue
        try:
            src = (root / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "@interface" not in src and "annotation class" not in src:
            continue
        code = _fx_strip_comments(src)
        m = re.search(r"(?:@interface|annotation\s+class)\s+(\w+)", code)
        if not m:
            continue
        got = _fx_sql_names(code[:m.start()], known)
        if got is None or got:
            out[m.group(1)] = got
    return out


def _fx_annotations_above(lines: list[str], idx: int) -> int:
    """The first line of the annotation block that ends just above line `idx` (0-based)."""
    start, balance = idx, 0
    for j in range(idx - 1, -1, -1):
        s = lines[j].strip()
        balance += s.count(")") - s.count("(")
        if s.startswith("@") or balance > 0 or not s:
            start = j
            if s.startswith("@") and balance <= 0:
                balance = 0
            continue
        break
    return start


def _fx_jvm_tests(root: Path, rel: str, src: str, files: list[str],
                  known: set[str]) -> dict[str, str | None]:
    """``{"<file>:<line of the method's declaration>": verdict}`` for one JUnit class on a
    Spring context. The line is testcov's (`decl_line`): the first `name(` that is not a
    comment or an annotation, the key the Tests tab's rows carry."""
    code = _fx_strip_comments(src)
    cm = re.search(r"^[ \t]*(?:[\w@().=\", ]+\s)?(?:class|object)\s+\w+", code, re.M)
    if not cm or not _FX_JVM_TEST.search(code):
        return {}
    header = code[:cm.end()]
    # One level of inheritance: an abstract base class carrying the annotations.
    ext = re.search(r"\bextends\s+(\w+)", code[cm.start():code.find("{", cm.start())])
    if ext:
        module = rel.split("/src/")[0]
        for f in files:
            if f.startswith(module + "/") and Path(f).stem == ext.group(1) \
                    and f.endswith((".java", ".kt")):
                try:
                    base = _fx_strip_comments((root / f).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    break
                bm = re.search(r"\b(?:class|object)\s+\w+", base)
                header = (base[:bm.end()] if bm else "") + "\n" + header
                break
    if not _FX_JVM_DB.search(header):
        return {}
    module = rel.split("/src/")[0]
    profiles = [p for m in re.finditer(r"@ActiveProfiles\s*\(([^)]*)\)", header)
                for p in re.findall(r"[\"']([^\"']+)[\"']", m.group(1))]
    props = _fx_module_props(root, module, profiles)
    props.update(_fx_inline_props(header))
    on_seed = _fx_on_seed(root, module, files, props)
    metas = _fx_meta_annotations(root, module, files, known)
    class_sql = _fx_sql_names(header, known, metas)

    lines, src_lines = code.splitlines(), src.splitlines()
    offsets, at = [], 0
    for ln in lines:
        offsets.append(at)
        at += len(ln) + 1
    methods = []                                  # (name, annotations start, decl, body end)
    for i, ln in enumerate(lines):
        if not _FX_JVM_TEST.search(ln) or not ln.strip().startswith("@"):
            continue
        # Down past the annotations (one may span lines: `@Sql({…,\n …})`) to the method.
        j, balance = i, 0
        while j < len(lines):
            s = lines[j].strip()
            if balance <= 0 and s and not s.startswith("@"):
                break
            balance += s.count("(") - s.count(")")
            j += 1
        if j >= len(lines):
            continue
        m = _FX_JVM_DECL.search(lines[j])
        if not m or m.group(1) in ("if", "for", "while", "switch", "return", "new"):
            continue
        a = _fx_annotations_above(lines, j)
        methods.append((m.group(1), offsets[a], offsets[j], _fx_brace_end(code, offsets[j])))
    if not methods:
        return {}
    shared, last = [], 0
    for _, a, _, b in sorted(set(methods), key=lambda t: t[1]):
        shared.append(code[last:a]); last = max(last, b)
    shared.append(code[last:])
    shared_text = "".join(shared)
    out: dict[str, str | None] = {}
    for name, a, d, b in methods:
        rx = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        line = next((k + 1 for k, t in enumerate(src_lines)
                     if rx.search(t) and not t.strip().startswith(("//", "*", "@"))), None)
        if line is None:
            continue
        own = _fx_sql_names(code[a:d], known, metas)
        # A method's own @Sql overrides the class's (Spring's default merge mode).
        sql = own if own is None or own else class_sql
        if sql is None:
            out[f"{rel}:{line}"] = None
        elif len(sql) == 1:
            out[f"{rel}:{line}"] = next(iter(sql))
        elif sql:
            out[f"{rel}:{line}"] = None              # two fixtures: not ours to pick
        else:
            # Nothing loaded by the test itself: a fixture its code names, else what
            # Flyway boots the context with — the seed, or an empty database.
            v = _fx_verdict("", code[d:b] + "\n" + shared_text, known)
            out[f"{rel}:{line}"] = "seed" if v == "" and on_seed else v
    return out


def _fx_jvm_module(root: Path, rel: str) -> str | None:
    """The JVM module a `.feature` belongs to — the path before `/src/`, when it builds with
    Maven or Gradle — or None for an E2E feature."""
    if "/src/" not in rel:
        return None
    module = rel.split("/src/")[0]
    return module if any((root / module / b).is_file()
                         for b in ("pom.xml", "build.gradle", "build.gradle.kts")) else None


def _fx_scan(root: Path, files: list[str], known: set[str]) -> dict[str, str]:
    """Every E2E test and every JVM test on a Spring context read: ``{id: fixture | "seed" |
    ""}`` (see `_fx_verdict`)."""
    out: dict[str, str] = {}
    features = [f for f in files if f.endswith(".feature") and "node_modules" not in f]
    defs = _fx_step_defs(root, files) if features else []
    emptied = fixtures_emptied(root, files) if features else {}
    on_seed: dict[str, bool] = {}
    for f in features:
        try:
            got = _fx_feature_tests(f, (root / f).read_text(encoding="utf-8"), defs, known)
        except (OSError, UnicodeDecodeError):
            continue
        # A JVM Cucumber scenario that declares nothing starts on what its Spring context's
        # Flyway loads, unless a hook empties the tables before it.
        module = _fx_jvm_module(root, f)
        if module and module not in on_seed:
            on_seed[module] = _fx_on_seed(root, module, files, _fx_module_props(root, module, []))
        for k, v in got.items():
            out[k] = "seed" if v == "" and module and on_seed[module] and k not in emptied else v
    for f in files:
        if _FX_SPEC.search(f) and "node_modules" not in f:
            try:
                out.update(_fx_spec_tests(root, f, (root / f).read_text(encoding="utf-8"), known))
            except (OSError, UnicodeDecodeError):
                pass
    for f in files:
        if f.endswith((".java", ".kt")) and "/src/" in f and "/src/main/" not in f:
            try:
                got = _fx_jvm_tests(root, f, (root / f).read_text(encoding="utf-8"), files, known)
            except (OSError, UnicodeDecodeError):
                continue
            out.update({k: v for k, v in got.items() if v is not None})
    return out


def fixtures_by_test(root: Path, files: list[str] | None = None) -> dict[str, str]:
    """``{"<repo-relative file>:<line>": fixture}`` — ``"seed"`` or a fixture's name —
    for every E2E or Spring test that starts on a dataset. Absent = it starts on an empty
    database (it declares none), or the answer is not ours to give."""
    root = Path(root)
    files = _fx_ls(root) if files is None else files
    known = set(fixture_colors(root, files)["colors"])
    return {k: v for k, v in _fx_scan(root, files, known).items() if v}


_FX_HOOK_TRUNCATE = re.compile(r"TRUNCATE\s+(?:TABLE\s+)?([\w.\s,\"]+?)(?:\s+(?:RESTART|CONTINUE|"
                               r"CASCADE|RESTRICT)\b|[\"';)]|$)", re.I)


def fixtures_emptied(root: Path, files: list[str] | None = None) -> dict[str, dict]:
    """``{scenario id: {"tables": [...], "hook": "<Class>"}}`` for every scenario of a JVM
    Cucumber suite whose ``@Before`` hook truncates tables — the suite's own module, the
    part of the path before ``/src/``. A scenario that names a fixture is left out."""
    root = Path(root)
    files = _fx_ls(root) if files is None else files
    hooks: dict[str, tuple[str, list[str]]] = {}
    for f in files:
        if not re.search(r"\.(?:java|kt)$", f) or "/src/" not in f:
            continue
        try:
            src = (root / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "io.cucumber" not in src or "@Before" not in src:
            continue
        m = _FX_HOOK_TRUNCATE.search(_fx_strip_comments(src).replace('" +', " ").replace('+ "', " "))
        if not m:
            continue
        tables = [t.strip().strip('"').split(".")[-1] for t in m.group(1).split(",") if t.strip()]
        hooks.setdefault(f.split("/src/")[0], (Path(f).stem, tables))
    out: dict[str, dict] = {}
    for f in files:
        if not f.endswith(".feature") or "/src/" not in f or f.split("/src/")[0] not in hooks:
            continue
        hook, tables = hooks[f.split("/src/")[0]]
        try:
            lines = (root / f).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        tags = ""
        feature_tags = ""
        for no, line in enumerate(lines, 1):
            st = line.strip()
            if st.startswith("@"):
                tags += " " + st
                continue
            m = _FX_BLOCK.match(line)
            if not m:
                continue
            if m.group(1) == "Feature":
                feature_tags = tags
            elif (m.group(1).startswith("Scenario") or m.group(1) == "Example") \
                    and "@fixture" not in feature_tags + tags:
                out[f"{f}:{no}"] = {"tables": tables, "hook": hook}
            tags = ""
    return out


def fixture_registry(root: Path) -> dict:
    """The colours, and ``tests``: every test that starts on a dataset, by id. A test that
    starts empty is not in it, and its row gets no dot."""
    root = Path(root)
    files = _fx_ls(root)
    reg = fixture_colors(root, files)
    reg["tests"] = {k: v for k, v in _fx_scan(root, files, set(reg["colors"])).items() if v}
    return reg


def render_fixture_registry(root: Path) -> str:
    """The ``hr-fixtures`` JSON block FIXTURES_JS reads, or "" when the project has no
    fixtures and no test starts on the seed — then neither tab draws a dot."""
    reg = fixture_registry(root)
    if not reg["colors"] and not reg["tests"]:
        return ""
    return ('<script type="application/json" id="hr-fixtures">'
            + json.dumps(reg).replace("</", "<\\/") + "</script>")


if __name__ == "__main__":
    import sys
    print(json.dumps(fixture_registry(Path(sys.argv[1] if len(sys.argv) > 1 else ".")), indent=1))
