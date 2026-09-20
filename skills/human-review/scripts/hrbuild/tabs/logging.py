"""The Logging tab: what the branch logs, and whether it is a privacy problem."""
from __future__ import annotations

import functools
import hashlib
import html
import json
import os
import re
import shutil
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


# --------------------------------------------------------------------------- #
# GDPR verdict per logging statement — a real model call, not a word list.
#
# A word list over the argument names was tried first and rejected: it comes out SAFE
# for `log.info("{}", x)` when `x` was assigned three lines up from
# `owner.getName()`, because `x` looks like nothing. Knowing what a value actually
# holds means following the assignment, and no word list does that — so this asks a
# model, and gives it enough source to trace it: the statement's enclosing method
# (parameters and locals both) plus the class's field declarations, never the whole
# file and never the one line alone. `AI Evaluation` on the legend is therefore an
# accurate label, not the aspirational one a word list would have made it.
# --------------------------------------------------------------------------- #

# One mark, one sentence, for every verdict a model produced.
AI_MARK = '<sup class="ai-mark" data-tip="LLM evaluated">\U0001F916</sup>'

PRIVACY_MARK = {
    "safe": ("✅", "SAFE", "added"),
    "doubt": ("🤔", "DOUBT", ""),
    "privacy": ("❌", "PRIVACY", "removed"),
    # Not a fourth colour on the same footing as the other three: this is what a
    # guessed SAFE would have looked like if the model could not be reached and this
    # function papered over it instead of admitting so. DOUBT means the model looked
    # and said it could not tell; this means it was never successfully asked at all.
    "error": ("⚠️", "NOT EVALUATED", "warn"),
}

# No `chain` field any more. Asking the model where a value came from, and then
# rendering its answer as a list of `file:line` + the source line, was a paraphrase of
# code standing where the code could have stood. `logextract.py` walks that back
# syntactically now and the snippet quotes the real lines, so the only thing left for
# the model is the part no line of Java says out loud: whether the value is personal
# data. `trace` is that, in one clause.
# One clause per *value*, not one sentence per statement. A statement that logs three
# things and gets one fused sentence makes the reader do the un-fusing, and the thing
# they are trying to find out — which of the three carried the risk — is exactly what
# the fusing destroyed. So the model answers per value, keyed by the argument as it is
# written in the source, and the page renders one bullet per logged value.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
        "values": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["SAFE", "DOUBT", "PRIVACY"]},
                    "note": {"type": "string"},
                },
                "required": ["name", "verdict", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "values"],
    "additionalProperties": False,
}

# SAFE < DOUBT < PRIVACY. The headline verdict is the worst of what the bullets say and
# what the model called the statement overall — never better than its own worst bullet,
# which is the one way a per-value answer could have made the page *less* honest than
# the single sentence it replaced.
VERDICT_RANK = {"safe": 0, "doubt": 1, "privacy": 2}


def _worst_verdict(*verdicts: str) -> str:
    real = [v for v in verdicts if v in VERDICT_RANK]
    return max(real, key=lambda v: VERDICT_RANK[v]) if real else "doubt"

VERDICT_SYSTEM_PROMPT = (
    "You are a precise static-analysis assistant embedded in a code review build script. "
    "You are given one Java logging statement plus enough of its surrounding source to "
    "trace where each interpolated value comes from. Decide whether the statement, once "
    "it executes, could write personal data (GDPR-relevant: a name, an email address, a "
    "phone number, a postal address, a government ID, free text about a person, or "
    "similar) to a log aggregator kept for months.\n\n"
    "Trace each interpolated value through the source you were given: where it is "
    "declared or assigned, and onward if that right-hand side is itself another "
    "variable. If you cannot resolve a value with what you were given, its verdict is "
    "DOUBT -- never guess SAFE past a value you could not follow.\n\n"
    "Answer per VALUE, not per statement. Return one entry in `values` for EVERY value "
    "the statement interpolates -- exactly those, no more and no fewer -- in the order "
    "they appear in the call. Set each entry's `name` to the argument EXACTLY as it is "
    "written in the source (`vetId`, `owner.getName()`), so the page can line your "
    "answer up with the call; do not rename, shorten or paraphrase it. A statement that "
    "interpolates nothing gets an empty `values` list. The top-level `verdict` is the "
    "worst of the individual ones.\n\n"
    "The page already shows the reader the statement AND the lines each value came "
    "from, quoted verbatim from the file. So a `note` must not retell any of that: no "
    "file names, no line numbers, no restating a declaration the reader is looking at, "
    "no naming the enclosing method, no repeating the value's own name (the bullet is "
    "already labelled with it), and no restating the verdict (`not personal data`, "
    "`safe`, `a privacy risk` -- the bullet already carries its own mark). Give only "
    "what the code cannot say for itself: WHAT that value actually holds, as ONE noun "
    "phrase of at most 15 words, no trailing full stop.\n"
    "Good: `{\"name\": \"vetId\", \"verdict\": \"SAFE\", "
    "\"note\": \"just a numeric vet database id\"}`.\n"
    "Good: `{\"name\": \"owner.getName()\", \"verdict\": \"PRIVACY\", "
    "\"note\": \"the owner's full name, straight into the log\"}`.\n"
    "Bad (restates the verdict): `\"note\": \"a numeric id, not personal data\"`.\n"
    "Bad: `{\"name\": \"vetId\", \"note\": \"vetId is the declared Integer "
    "parameter of resolveVet(Integer vetId) (VisitRestController.java:89), a numeric "
    "database identifier passed straight into the log call...\"}`.\n\n"
    "Respond only through the given JSON schema."
)


def _statement_context(h: dict) -> str:
    """Enough source for a model to trace every interpolated argument: the enclosing
    method (`logextract.py` resolves its range structurally, the same AST pass that
    finds the statement itself) so parameters and locals are both visible, plus the
    class's field declarations (numbered, so a field-rooted value can be placed)
    for a value that turns out to come from `this`. Never the whole file — a class with
    forty methods is forty methods of noise around the one that matters — and never just
    the statement alone, which is the version of this feature that cannot tell a
    parameter from a field from thin air, let alone trace a value past either of them."""
    ms, me = h.get("method_start"), h.get("method_end")
    try:
        src_lines = Path(h["abs_file"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        src_lines = []
    if ms and me and src_lines:
        window = src_lines[ms - 1:me]
        numbered = "\n".join(f"{n:>4}  {l}" for n, l in zip(range(ms, me + 1), window))
        method_block = f"Enclosing method ({h['file']}:{ms}-{me}):\n{numbered}"
    else:
        method_block = ("No enclosing method could be resolved. All that is available "
                        f"is the statement itself:\n{h['raw_line'].strip()}")
    fields = h.get("_fields") or []
    if fields:
        field_lines = "\n".join(f"{f['line']:>4}  {f['type']} {f['name']}" for f in fields)
        fields_block = f"Class fields in scope ({h['file']}):\n{field_lines}"
    else:
        fields_block = "Class fields in scope: none."
    return f"{method_block}\n\n{fields_block}"


def _verdict_prompt(h: dict, context: str) -> str:
    return (
        f"Logging statement ({h['file']}:{h['line']}):\n    {h['text']}\n\n"
        f"{context}\n\n"
        "Which value(s) does this statement log, where does each come from, and is any "
        "of it personal data? Give one entry per interpolated value, named exactly as "
        "the argument is written above, plus the statement's overall verdict."
    )


def _claude_bin() -> str | None:
    for cand in (os.environ.get("CLAUDE_BIN"), "claude"):
        p = shutil.which(cand) if cand else None
        if p:
            return p
    return None


def _call_privacy_model(prompt: str) -> dict:
    """One live model call. Returns `{"verdict","values","cost_usd"}` on success, or
    raises `RuntimeError` with a message written to go straight on the page — a missing
    binary, a non-zero exit, a timeout, or a response that does not match the schema.
    Never returns a guessed verdict; the caller turns any exception here into the loud
    `error` state, not a fallback answer. `values` is validated for *shape* only —
    whether it actually covers the values the statement logs is decided against
    `logextract.py`'s argument list at render time, not against the model's word."""
    claude_bin = _claude_bin()
    if not claude_bin:
        raise RuntimeError("the `claude` CLI is not on PATH (set $CLAUDE_BIN to point at it)")
    cmd = [claude_bin, "-p", prompt, "--output-format", "json",
           "--model", os.environ.get("PRIVACY_VERDICT_MODEL", "sonnet"),
           "--restricted", "--strict-mcp-config", "--no-session-persistence",
           "--max-turns", "1", "--system-prompt", VERDICT_SYSTEM_PROMPT,
           "--json-schema", json.dumps(VERDICT_SCHEMA)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the model call timed out")
    except OSError as e:
        raise RuntimeError(f"could not run `claude`: {e}")
    # The answer decides, not the exit code. `claude -p --json-schema --max-turns 1`
    # stops on the structured-output tool call and can exit non-zero while stdout holds
    # a complete, schema-conforming, already-paid-for response (`is_error: false`,
    # `subtype: "success"`). Reading the exit code first threw that answer away and put
    # the loud "the model could not be reached" on a page whose model *had* been
    # reached — the one state that is supposed to mean nobody was ever asked. So parse
    # first, and let a bad exit only colour the message when the payload is unusable
    # too. Nothing here is loosened: an unparsable body, `is_error`, or a response that
    # misses the schema still raises, and no verdict is ever guessed.
    exited = (f" (the CLI also exited {proc.returncode}"
              + (f": {proc.stderr.strip()[-200:]}" if proc.stderr.strip() else "")
              + ")") if proc.returncode != 0 else ""
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"the model call returned unparsable output{exited}")
    if payload.get("is_error"):
        raise RuntimeError(f"the model call failed: {str(payload.get('result'))[:300]}{exited}")
    out = payload.get("structured_output")
    values = out.get("values") if isinstance(out, dict) else None
    if (not isinstance(out, dict) or out.get("verdict") not in ("SAFE", "DOUBT", "PRIVACY")
            or not isinstance(values, list)
            or not all(isinstance(v, dict) and v.get("name") and v.get("note")
                       and v.get("verdict") in ("SAFE", "DOUBT", "PRIVACY")
                       for v in values)):
        raise RuntimeError("the model's response did not match the expected verdict "
                           f"schema{exited}")
    return {"verdict": out["verdict"].lower(),
            "values": [{"name": v["name"], "verdict": v["verdict"].lower(),
                        "note": v["note"]} for v in values],
            "cost_usd": payload.get("total_cost_usd") or 0.0}


def _verdict_cache_path(root: Path) -> Path:
    return root / ".human-review" / ".privacy-verdicts.json"


def _load_verdict_cache(root: Path) -> dict:
    p = _verdict_cache_path(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_verdict_cache(root: Path, cache: dict) -> None:
    p = _verdict_cache_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


#: Set by `--no-model`. A build that cannot ask is not the same as a model that could not
#: be reached, and the page says so in those words — the Logging tab already has a state
#: for "nobody was ever asked", and this is one honest way of arriving in it.
OFFLINE = False


def _offline_call(prompt: str) -> dict:
    raise RuntimeError("not evaluated: this build ran with --no-model, so the Logging "
                       "tab shows only verdicts an earlier run already paid for")


def privacy_verdict(h: dict, root: Path, cache: dict, call=None) -> dict:
    """SAFE / DOUBT / PRIVACY / error, with one clause per interpolated value — a live
    model call over the statement's enclosing method, cached by a hash of exactly what
    was sent (the statement plus its context) so a re-run on unchanged code neither
    flips the answer nor pays for it twice. `cache` is loaded once by the caller and
    mutated here; the file is rewritten on every new entry, not batched, so a run that
    dies partway through does not lose the calls it already paid for.

    The key hashes the system prompt alongside the statement and its context, so an edit
    to what the model is *asked* invalidates the cache the same way an edit to the code
    does — a shortened `trace` instruction that kept serving the old paragraph out of
    cache would be a silent no-op.

    `call` defaults to `None`, resolved to `_call_privacy_model` *inside* the body
    rather than as `def ...(call=_call_privacy_model)` — a default bound at def-time
    would freeze in the original function object, so patching the module-level name
    for a test (`monkeypatch.setattr(build, "_call_privacy_model", fake)`) would
    silently do nothing here; every caller that does not pass its own `call` needs the
    patch to actually take."""
    call = call or (_offline_call if OFFLINE else _call_privacy_model)
    context = _statement_context(h)
    key = hashlib.sha256(
        (VERDICT_SYSTEM_PROMPT + "\n" + h["text"] + "\n" + context).encode("utf-8")
    ).hexdigest()
    cached = cache.get(key)
    if cached:
        return {**cached, "cached": True, "cost_usd": 0.0}
    try:
        result = call(_verdict_prompt(h, context))
    except RuntimeError as e:
        return {"verdict": "error", "values": [], "note": str(e),
                "cached": False, "cost_usd": 0.0}
    entry = {"verdict": result["verdict"], "values": result.get("values") or []}
    cache[key] = entry
    _save_verdict_cache(root, cache)
    return {**entry, "cached": False, "cost_usd": result.get("cost_usd", 0.0)}


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


def _value_bullets(args: list, values: list) -> tuple[list[dict], bool]:
    """One row per value the statement actually logs — driven by `logextract.py`'s
    argument list, never by whatever the model chose to mention.

    The model is asked for one clause per interpolated value; a model that quietly drops
    one must not quietly drop it from the page, so the rows come from the *code* and the
    model's clauses are matched onto them. Anything left without a clause renders as its
    own `unresolved` row and drags the headline verdict down — that is the same rule the
    tab already applies to a value the model could not follow, and for the same reason:
    "nobody said" must never read like "nothing to say".

    Matching is by the argument text (whitespace-insensitive), then by root identifier
    when that is unambiguous among the rows still unmatched — a model that answers
    `owner` for `owner.getName()` is answering the right question with a shorter name,
    but only while there is exactly one candidate it could mean.

    Returns the rows and whether any of them came out unresolved."""
    rows = [{"arg": a, "verdict": None, "note": None} for a in args]
    pool = list(enumerate(rows))

    def norm(t):
        return re.sub(r"\s+", "", t or "")

    def claim(idx, v):
        # `_call_privacy_model` already lower-cases, but `privacy_verdict` accepts any
        # `call`, and a verdict word is a key into `PRIVACY_MARK` two functions later.
        verdict = str(v.get("verdict") or "").lower()
        rows[idx]["verdict"] = verdict if verdict in VERDICT_RANK else None
        rows[idx]["note"] = v.get("note")

    unmatched = []
    for v in values:
        hit = next((i for i, r in pool if r["verdict"] is None
                    and norm(r["arg"]) == norm(v.get("name"))), None)
        if hit is None:
            unmatched.append(v)
            continue
        claim(hit, v)
    for v in unmatched:
        root = logextract_root(v.get("name"))
        cands = [i for i, r in pool
                 if r["verdict"] is None and root and logextract_root(r["arg"]) == root]
        if len(cands) == 1:
            claim(cands[0], v)

    broken = any(r["verdict"] is None for r in rows)
    return rows, broken


def logextract_root(expr: str | None) -> str | None:
    """The leading identifier of an expression, for matching a model's `owner` onto the
    page's `owner.getName()`. Deliberately the same reading `logextract.py` uses to root
    its origin walk, imported rather than re-derived so the two cannot drift apart."""
    if not expr:
        return None
    return _logextract().origin_root(expr)


@functools.lru_cache(maxsize=1)
def _logextract():
    """`logextract.py` as a module, not a subprocess — this needs one pure function out
    of it, not a scan. Registered in `sys.modules` *before* `exec_module`: its `@dataclass`
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


def _value_bullets_html(rows: list) -> str:
    """The bullets under the verdict: one per logged value, `name — what it is`.

    The name is the argument as the source writes it, in `<code>`, so a reader scanning
    a three-value statement can see which of the three carried the risk without reading
    a sentence that fused them. A row the model never answered says exactly that."""
    if not rows:
        return ""
    items = []
    for r in rows:
        name = f'<code>{html.escape(r["arg"])}</code>'
        if r["verdict"] is None:
            items.append(f'<li class="val-unresolved">{name} — no clause came back for '
                         f'this value; it was not assessed</li>')
            continue
        emoji = PRIVACY_MARK[r["verdict"]][0]
        # The per-value mark is shown only when it differs from "fine": a column of green
        # ticks under a green tick is decoration, and the row a reader must not miss is
        # the one that is not green.
        mark = "" if r["verdict"] == "safe" else f'{emoji} '
        items.append(f'<li>{mark}{name} — {html.escape(r["note"])}</li>')
    return f'<ul class="log-values">{"".join(items)}</ul>'


def _logging_listing(added: list, root: Path, fields_by_file: dict | None = None,
                      call=None, cache_root: Path | None = None) -> str:
    """The leading answer: one code snippet per logging statement this change set
    actually added or modified — the same `.snippet` figure every other quoted line on
    this page uses (`extract-snippet.py`), not a second, invented code-block style.
    Files with nothing to say do not appear here at all.

    An empty list is not silence. `logextract.py` ran and genuinely found zero — see
    `logging_fragment`'s docstring for why that is itself the finding — so it renders as
    a sentence carrying the same weight as the snippets it replaces, never as a blank
    stretch of page that would read exactly like the scan never having run at all.

    `cache_root` defaults to `root` — they are the same directory in production (both
    the reviewed repo's checkout) — and exists as its own parameter only so a test can
    point the verdict cache at a throwaway `tmp_path` while still handing `snippet_html`
    the real repo it needs to resolve a fixture file against."""
    if not added:
        return ('<p class="lede"><b>None.</b> Not one logging statement was added or '
                'changed on the lines this change set touches.</p>')
    cache_root = cache_root or root
    fields_by_file = fields_by_file or {}
    cache = _load_verdict_cache(cache_root)
    boxes = []
    # `new code` / `2 lines changed` — dropped on this tab only. Everywhere else the badge
    # answers "is this quoted block new, or an old one with a line in it?", which is a real
    # question about a snippet a reviewer did not choose. Here it is not: the gutter beside
    # the statement already marks the added lines with `+`, and every block on this tab is
    # here *because* the branch added or rewrote that logging line. A badge repeating the
    # tab's own entry condition on every box is a word the eye has to skip. The rest of the
    # bar stays: the file it came from, and the two handles that open the change.
    BADGE_RE = re.compile(r'<span class="code-badge"[^>]*>[^<]*</span>')
    for h in added:
        ref = _logging_ref(h)
        h = {**h, "_fields": fields_by_file.get(h["file"])}
        result = privacy_verdict(h, cache_root, cache, call=call)
        # The rows come from the code (`logextract.py`'s argument list), the clauses from
        # the model, and the headline verdict from the worst of everything below it — a
        # value the model skipped counts as unassessed, not as fine.
        rows, unassessed = _value_bullets(h.get("args") or [], result.get("values") or [])
        verdict_key = result["verdict"]
        if verdict_key != "error":
            verdict_key = _worst_verdict(verdict_key,
                                         *[r["verdict"] for r in rows if r["verdict"]],
                                         *(["doubt"] if unassessed else []))
        emoji, word, css = PRIVACY_MARK[verdict_key]
        verdict_class = f"privacy-verdict {css}".strip()
        # This box used to take the snippet apart — anchor stripped off the top, a
        # hand-rebuilt copy of it glued into the footer as a bottom-right marker — from
        # back when the header was a bare path on a line of its own and the footer had
        # room to spare. It is a shared component now, carrying the file *and* the two
        # handles that open the change, and a tab that quietly rebuilds a component is a
        # tab that stops getting its fixes. So the bar stays where every other tab has it,
        # at the top, and the footer keeps only what is this tab's own: the verdict.
        # The bar's own link opens at the *first* line of the window, which with origin
        # lines pulled in is the origin rather than the statement. Re-aimed at the hit's
        # own line and column: that is where a reader clicking a logging box expects to
        # land, and it is the one thing the generic bar cannot work out for itself.
        snippet = snippet_html(ref, None, root, exact=True,
                               link_at=(h["line"], h.get("column", 1)))
        snippet = BADGE_RE.sub("", snippet, count=1)
        # The verdict, and nothing else. What used to ride on this line was one run-on
        # sentence about the whole statement; it is a bullet per logged value below,
        # because "which of the three values is the problem" is the question a reader
        # brings here and a fused sentence is precisely what destroys the answer. The
        # location left this row too, upwards, into the source bar every tab shares.
        footer = (
            f'<p class="log-footer">'
            f'<span class="{verdict_class}">{emoji} <b>{word}</b></span>'
            # Outside the span, deliberately: the verdict word ends at the word. This is a
            # note about *how the verdict was reached*, and only where one actually was --
            # NOT EVALUATED means the model was never successfully asked.
            + (AI_MARK if verdict_key != "error" else "")
            + f'</p>'
        )
        # The model-failure state has no per-value answers to show — it never got any —
        # so its one message rides under the verdict in the same place the bullets would.
        note = result.get("note")
        body = (f'<p class="log-note">{html.escape(note)}</p>' if note
                else _value_bullets_html(rows))
        snippet = snippet.replace("</figure>", f"{footer}{body}</figure>", 1)
        boxes.append(snippet)
    # "The page marks tabs by what produced them" — this legend is the disclosure for a
    # tab whose verdicts are a model's reading, not a program's. What it no longer carries
    # is the machinery behind that reading: which half of the box is a live call, how many
    # calls it took, and what they cost. That was a paragraph about the build, on a tab a
    # reviewer opened to read about *their diff*, and the 🤖 on every verdict already says
    # the only part of it they can act on — that a model, not a program, decided this one.
    legend = (
        '<div class="privacy-legend"><p class="privacy-legend-title">🤖 AI Evaluation:'
        f'</p><p class="privacy-legend-note">The code block is the evidence: alongside '
        f'each statement it quotes the lines its logged values came from, walked back '
        f'structurally by <code>ast-grep</code> and cut from the working tree with their '
        f'real line numbers.</p>'
        '<ul class="privacy-legend-list">'
        '<li>✅ <b>SAFE</b> — nothing traced reads as personal data</li>'
        '<li>🤔 <b>DOUBT</b> — could not trace it with confidence, and an unresolved '
        'case is read as DOUBT on purpose rather than guessed SAFE</li>'
        '<li>❌ <b>PRIVACY</b> — a value traced back to personal data, on its way to a '
        'log aggregator kept for months</li>'
        '<li>⚠️ <b>NOT EVALUATED</b> — the model could not be reached; never silently '
        'read as SAFE</li>'
        '</ul></div>'
    )
    return "".join(boxes) + legend


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
    # The snippets and the legend sit directly on the page, exactly like every other
    # block's content on this tab. `_logging_listing` alone decides what shows: the real
    # snippets, or the explicit "None." sentence for a genuine zero.
    listing = _logging_listing(added, root, payload.get("stats", {}).get("fields", {}))
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
