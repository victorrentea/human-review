#!/usr/bin/env python3
"""`review-points.md` — what the agent fixed, declined and assumed — read into the three
piles the Review tab already renders.

The page has always been able to show what a review *found*: the passes leave findings in
the transcript and `review-passes.py` harvests them. What it could never show is what the
agent did with them — which finding it accepted, which it read and declined, and on what
grounds — because that decision is made in a conversation and then lost. Nor could it show
the readings the agent chose where the ticket was ambiguous, which are not in the diff at
all and are not findings: nobody found them, somebody decided them. Each of those carries
an optional `confidence:` — how sure the agent is that the reading it chose is the right
one — because "I decided this" and "I decided this and I may well be wrong" send a reviewer
to two different places, and only the agent that decided it can tell them apart.

`review-points.md` is that record, written by the agent while it still has it, committed
with the fixes, and therefore visible in the PR's own file list. This script turns it into
`.human-review/review-points.json`, whose `findings` / `autofixes` / `assumptions` arrays
are exactly the shapes `build-review-html.py` already reads (`reference/content-schema.md`).
No renderer changes: the file is a new *source* for the piles, not a new pile.

Two decisions worth stating, because both are load-bearing:

* **Hand-rolled, no PyYAML.** The frontmatter is `key: value` lines and nothing else. A YAML
  parser would accept nine spellings of the same thing plus one spelling of something subtly
  different, and would be a dependency on the machine of every trainee who installs this.
* **Strict and loud, never lenient and quiet.** An unknown section heading, an unknown field
  key, a field after the prose: all hard errors, because a pile the parser skipped reads on
  the page exactly like a pile nobody wrote, and those are the two things a reviewer most
  needs told apart. `--check` runs the same parse and writes nothing, so the agent can be
  told to validate the file *before* committing it.

Exit codes:  0 parsed · 3 no such file · 4 unparseable · 5 present, and every item
unanchored — a file that says nothing, reported as that rather than as a clean review ·
6 the report does not match `reference/review-points.schema.json`.

Usage:
  review-points.py --check                    # validate, print what was understood
  review-points.py                            # write .human-review/review-points.json
  review-points.py --root ../repo --file docs/review-points.md --out /tmp/rp.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

# Loaded by path from push-pr-comments.py and the tests, so its own directory is not
# necessarily on sys.path yet.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_points_schema  # noqa: E402

DEFAULT_FILE = "review-points.md"
CONFIG = "human-review.json"
DEFAULT_OUT = ".human-review/review-points.json"

# The three piles, by every heading that means one of them. `Fixed` lands in `autofixes`
# and `Ignored` in `findings` because that is what those two arrays already mean to the
# renderer: an autofix is a defect that was repaired and shows its diff, a finding is one
# still standing in front of the reader.
SECTIONS = {
    "fixed": "autofixes", "repaired": "autofixes", "applied": "autofixes",
    "ignored": "findings", "rejected": "findings", "declined": "findings",
    "not fixed": "findings",
    "assumptions": "assumptions", "assumed": "assumptions",
}
PILE_HEADING = {"autofixes": "Fixed", "findings": "Ignored", "assumptions": "Assumptions"}

# The one section that is not a pile: a note, in prose, that the review point was moved
# past commits nobody re-reviewed — `## Taken over without a new pass — 21 Sep 2026`. It
# exists because a second `Review-Points:` commit resets where the aftermath band counts
# from, and a reset with no sentence beside it would let the piles above read as a review
# of commits they never saw. Matched on its opening words, so the heading can carry a
# date; prose only — a `###` under it is refused, because an item filed here is an item
# on no pile, and that is exactly the silent drop the three-pile rule forbids.
NOTE_HEADINGS = ("taken over", "carried over", "not re-reviewed")

FIELDS = {"file", "source", "severity", "alternative", "why", "fixed-in", "confidence",
          "observation", "fix"}
# What the reviewer saw, in its own words, at most this many sentences — on a Fixed or
# Ignored item. A Fixed card with a title and a one-line diff (`void this.router.navigate`)
# told the reader *that* something changed and never *what was wrong*; the observation is
# that missing sentence. `fix:` is the optional note on the repair itself.
OBSERVATION_SENTENCES = 3
#: The most lines a `file:` range should span: the page opens this many of a card's quote
#: and folds the rest (`hrbuild/tabs/review.py:SNIPPET_LINES`). Eval run 11 anchored an
#: assumption on a whole 41-line class.
ANCHOR_LINES = 12
SEVERITIES = {"high", "medium", "low", "info"}
# How sure the agent is that the reading it chose is the right one. Only an assumption
# can carry it: 1.0 = the ticket left no other reading, 0.5 = a coin flip between two,
# below 0.3 = the author expects to be corrected. Two decimals, because the third would
# claim a precision nobody has about their own guess.
CONFIDENCE_DECIMALS = 2
# An assumption's `source:` names who decided it. `human` is the one value that changes the
# card; the rest are the spellings of "the agent did" already in the wild.
HUMAN_SOURCES = {"human", "user", "the human"}
AGENT_SOURCES = {"assumption", "agent", "the agent", "coder", "implementation decision"}
# Front-matter keys that say which commit is which, and the `provenance` key each becomes.
# Four commits a reviewer must not confuse: the range the reviewers read, the commit that
# implements the feature (not the housekeeping after it), HEAD when the review was
# recorded, and the commit that recorded it.
PROVENANCE = {"ticket": "ticket", "base": "base", "audited-base": "auditedBase",
              "audited-head": "auditedHead", "implementation": "implementation",
              "head": "head", "review-commit": "reviewCommit", "reviewers": "reviewers",
              "harness": "harness", "session": "session"}

# A finding the agent refuted, in its own words: "wrong — handleError rethrows", "the
# reviewer was wrong", "false positive". Such an item is not a defect left open and not a
# fix — it belongs under Ignored at `severity: info`, so the page does not count it as
# "worth a look" (run 5 filed one at medium, and the grade counted it).
REFUTED = re.compile(
    r"\b(?:reviewer|finding|claim)\s+(?:was|is)\s+(?:wrong|mistaken|incorrect)\b"
    r"|\bfalse\s+positive\b|\brefuted\b|\bnot\s+a\s+(?:real\s+)?(?:bug|defect)\b", re.I)
# The same verdict as a `why:` that opens on it — "wrong — handleError rethrows". Only on
# `why:`: an observation may well open on "Wrong status code…", which is the defect.
REFUTED_WHY = re.compile(r"^\s*(?:the\s+reviewer\s+(?:was|is)\s+)?wrong\b", re.I)
# A declined finding's `why:` that rests on a decision recorded somewhere else — the
# change's design, proposal or tasks, the planning Q&A, a numbered question or task, or
# "decided by the human". Eval run 10 dismissed four open issues this way ("design.md
# Decision 3 and task 2.1 specify them", "Q3 decided by the human") with no line to open.
DECIDED_ELSEWHERE = re.compile(
    r"\b(?:design|proposal|tasks)\.md\b|Q&A(?:\.md)?|\bQ\d+\b|\btasks?\s+\d+\.\d+\b"
    r"|\bdecided\s+by\b", re.I)
# The `file:line` such a reason must carry for the page to link and quote what it cites.
CITED_LINE = re.compile(r"[\w&.-]+\.md:\d+")


def is_refuted(item: dict) -> bool:
    """A declined finding the agent says was never true — an Ignored item at `severity:
    info` whose `why:` opens on the verdict ("refuted — this spec asserts %2B", "wrong —
    handleError rethrows") or names it ("false positive"). The page's one reading of it:
    the Review tab counts these apart from the open pile (eval run 8 counted four of them
    as "11 open" when seven were). Only `why:` decides: it is where the agent answers, and
    an observation that says "refuted" is quoting someone, not ruling."""
    if not isinstance(item, dict) or (item.get("severity") or "info") != "info":
        return False
    why = re.sub(r"<[^>]+>", "", html.unescape(str(item.get("why") or "")))
    return bool(REFUTED_WHY.search(why) or REFUTED.search(why))

H2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")
H3 = re.compile(r"^###\s+(.*?)\s*#*\s*$")
FIELD = re.compile(r"^[-*]\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
FRONT_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
# A ref with a line or a line range on the end — the difference between "this file" and a
# card showing those lines. `path:12` and `path:12-30` count; a bare path does not, and
# neither does a Windows drive letter or a URL, which is why the tail has to be all digits.
RANGED = re.compile(r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")   # `path:89,93-95` too: one card
CODE_SPAN = re.compile(r"`([^`]+)`")


class Unparseable(Exception):
    """The file is present and is not this format. Never downgraded to a warning: a
    silently skipped section is the one failure this whole file exists to prevent."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def config_path(root: Path) -> str | None:
    """`"reviewPoints"` from the repo's own `human-review.json`, if it has an opinion."""
    p = root / CONFIG
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = data.get("reviewPoints") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def inline(text: str) -> str:
    """Prose as the page can render it: escaped, with backticks as `<code>`.

    Everything is escaped first, so a `<` an agent typed in prose reaches the reader as a
    `<` instead of as markup — and the `{{snippet:…}}` / `{{diff:…}}` tokens survive it
    untouched, because they contain no character escaping touches. Paragraph breaks become
    `<br><br>`: the renderer wraps a body in a single `<p>`, so the alternative is a wall.
    """
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        one = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if one:
            # The line is escaped once, as a whole: a code span's `&` is already `&amp;`
            # by the time the backticks are swapped for tags, and escaping the group again
            # put `&amp;amp;` in the JSON and `&amp;` on the reader's screen (run 6).
            paragraphs.append(CODE_SPAN.sub(
                lambda m: f"<code>{m.group(1)}</code>", html.escape(one)))
    return "<br><br>".join(paragraphs)


def note_html(text: str) -> str:
    """A note's prose as the band renders it: paragraphs through `inline`, and a block
    whose every line is a `- ` bullet as a `<ul>` — the takeover note is mostly a list of
    commits, and forty shas glued into one paragraph is not a list anyone can read."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if lines and all(ln.startswith("- ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{inline(ln[2:])}</li>" for ln in lines)
                       + "</ul>")
        else:
            out.append(f"<p>{inline(block)}</p>")
    return "".join(out)


def parse_front(lines: list[str], problems: list[str]) -> tuple[dict, int]:
    """The `---` fenced `key: value` block, and the line the body starts on."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return {}, 0
    front: dict[str, str] = {}
    for n in range(i + 1, len(lines)):
        raw = lines[n]
        if raw.strip() == "---":
            return front, n + 1
        if not raw.strip():
            continue
        m = FRONT_LINE.match(raw.strip())
        if not m:
            problems.append(f"line {n + 1}: frontmatter is `key: value` lines only, "
                            f"got {raw.strip()[:60]!r}")
            continue
        front[m.group(1).strip().lower()] = m.group(2).strip()
    problems.append("the frontmatter fence was opened with `---` and never closed")
    return front, len(lines)


def split_ref(value: str) -> tuple[str, str | None]:
    """`path:12-30 | caption` → `("path:12-30", "caption")`."""
    if "|" in value:
        ref, caption = value.split("|", 1)
        return ref.strip(), caption.strip() or None
    return value.strip(), None


# Only line numbers: what follows the comma in `path:89,93-95` — more spans of the same
# file, which `extract-snippet.py` quotes as one card — rather than a second ref.
SPANS_ONLY = re.compile(r"^\d+(?:-\d+)?$")
# A line tail with more text after it: `a.ts:12 b.ts:30`, two refs glued with a space.
GLUED_BY_SPACE = re.compile(r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*\s+\S")
# One part of a comma-split `file:`: no whitespace, and a path (a `/` or a `.`) or lines.
PATHLIKE = re.compile(r"^(?=\S*[./]|\S*:\d)[^\s|]+$")


def split_refs(text: str) -> tuple[list[str], list[str]]:
    """`a.ts:12, b.ts:30` → `(["a.ts:12", "b.ts:30"], [])`: one `file:` value as the refs
    it names, and what is wrong with any of them.

    A comma between two refs is how a model writes two places on one line, and the build
    used to take the pair as one path — `a.ts:12, b.ts` — and abort on a file that does
    not exist. A part that is only line numbers stays on the ref before it (`a.py:89,93-95`
    is one ref with two spans). Every part is held to what a single ref is held to."""
    parts = [p.strip() for p in text.split(",")]
    refs: list[str] = []
    bad: list[str] = []
    for part in parts:
        if refs and SPANS_ONLY.match(part) and RANGED.search(refs[-1]):
            refs[-1] += "," + part
        elif not part:
            bad.append(f"{text!r} has an empty ref between its commas")
        elif SPANS_ONLY.match(part):
            bad.append(f"{text!r}: `{part}` is a line number with no file before it")
        else:
            refs.append(part)
    for ref in refs:
        if GLUED_BY_SPACE.search(ref):
            bad.append(f"`{ref}` is more than one ref glued together — write one "
                       "`- file:` line per ref")
        elif len(refs) > 1 and not PATHLIKE.match(ref):
            # Split on a comma, each part has to look like a ref on its own; otherwise the
            # comma was prose (`a.ts:12, the guard`), and guessing which is worse than asking.
            bad.append(f"{text!r} reads as {len(refs)} refs, and `{ref}` is not a path — "
                       "write one `- file:` line per ref, prose goes in the body")
    return refs, bad


def build_item(title: str, fields: list[tuple[str, str]], body: str, pile: str,
               front: dict, problems: list[str], warnings: list[str], where: int) -> dict:
    """One `### …` block as the renderer wants it.

    `refs` and `snippets` both come from `file:` — a ref that names lines earns a card, a
    ref that names only a file earns a link — so the agent writes one thing and the page
    decides how much room to give it.
    """
    item: dict = {"title": inline(title)}
    refs: list[str] = []
    snippets: list[dict] = []
    seen: set[str] = set()
    fixed_in = None
    for key, value in fields:
        if key not in FIELDS:
            problems.append(f"line {where}: {title[:40]!r} has an unknown field "
                            f"`{key}:` — valid fields are "
                            f"{', '.join(sorted(FIELDS))}")
            continue
        if key != "file" and key in seen:
            problems.append(f"line {where}: {title[:40]!r} sets `{key}:` twice")
            continue
        seen.add(key)
        if key == "file":
            text, caption = split_ref(value)
            if not text:
                problems.append(f"line {where}: {title[:40]!r} has an empty `file:`")
                continue
            parts, bad = split_refs(text)
            if bad:
                problems.extend(f"line {where}: {title[:40]!r} `file:` — {b}" for b in bad)
                continue
            if caption and len(parts) > 1:
                problems.append(f"line {where}: {title[:40]!r} captions `{text}`, which is "
                                f"{len(parts)} refs — a caption belongs to one snippet card; "
                                "give each ref its own `- file:` line")
                continue
            for ref in parts:
                refs.append(ref)
                if RANGED.search(ref):
                    snippets.append({"ref": ref, **({"caption": caption} if caption else {})})
                    span = sum(int(b or a) - int(a) + 1 for a, b in
                               re.findall(r"(\d+)(?:-(\d+))?", RANGED.search(ref).group(0)))
                    if span > ANCHOR_LINES:
                        warnings.append(
                            f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) — `file: "
                            f"{ref}` spans {span} lines; anchor the lines that do the thing "
                            f"(the page opens {ANCHOR_LINES} and folds the rest).")
                elif caption:
                    problems.append(f"line {where}: {title[:40]!r} captions `{ref}`, which "
                                    "names no lines — a caption belongs to a snippet card, "
                                    "and a whole-file ref does not get one")
        elif key == "severity":
            sev = value.strip().lower()
            if pile == "assumptions":
                # Deliberately fatal, not coerced away. An assumption is not a defect; a
                # page that ranked one would be inviting the reader to treat a decision
                # they are being asked to confirm as a bug somebody left in.
                problems.append(f"line {where}: {title[:40]!r} is an assumption and "
                                "carries `severity:` — an assumption is not a defect and "
                                "must not be ranked as one. Drop the field.")
            elif sev not in SEVERITIES:
                problems.append(f"line {where}: {title[:40]!r} has severity {sev!r} — "
                                f"one of {', '.join(sorted(SEVERITIES))}")
            else:
                item["severity"] = sev
        elif key == "confidence":
            # The mirror image of `severity:`: severity ranks a defect and is refused on an
            # assumption, confidence rates a *reading* and is meaningless anywhere else.
            # Refused there only with a warning, though, not fatally — a stray confidence on
            # a fix is a misplaced field, while a severity on an assumption is a category
            # error that would put a defect's rank on a decision the reader must confirm.
            raw = value.strip()
            if pile != "assumptions":
                warnings.append(
                    f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) carries "
                    f"`confidence: {raw}` — only an assumption has a reading to be unsure "
                    "about; a fix is either in the diff or it is not. Ignored.")
                continue
            try:
                number = float(raw)
            except ValueError:
                problems.append(f"line {where}: {title[:40]!r} has confidence {raw!r} — "
                                "confidence is a number between 0 and 1 (1.0 = the ticket "
                                "left no other reading, 0.5 = a coin flip between two, "
                                "below 0.3 = you expect to be corrected)")
                continue
            if not 0.0 <= number <= 1.0:
                problems.append(f"line {where}: {title[:40]!r} has confidence {raw} — "
                                "confidence is a number between 0 and 1; there is no being "
                                "surer than certain, and no being less sure than not")
                continue
            item["confidence"] = round(number, CONFIDENCE_DECIMALS)
        elif key == "fixed-in":
            fixed_in = value.strip()
        else:
            item[key] = inline(value)

    if pile in ("autofixes", "findings"):
        said = item.get("observation", "")
        if not said:
            warnings.append(
                f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) has no "
                "`observation:` — the card shows the change but not what the reviewer "
                "found wrong. One to three sentences.")
        elif len(re.findall(r"[.!?](?:\s|$)", re.sub(r"<[^>]+>", "", said))) > OBSERVATION_SENTENCES:
            warnings.append(
                f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) — `observation:` runs "
                f"past {OBSERVATION_SENTENCES} sentences; say what was wrong, not the story.")
    def plain(key: str) -> str:
        return re.sub(r"<[^>]+>", "", html.unescape(str(item.get(key) or "")))
    said_wrong = "why" if REFUTED_WHY.search(plain("why")) else next(
        (k for k in ("why", "observation", "fix") if REFUTED.search(plain(k))), None)
    if said_wrong is None and REFUTED.search(body or ""):
        said_wrong = "body"
    if said_wrong and (pile == "autofixes"
                       or (pile == "findings" and item.get("severity", "info") != "info")):
        warnings.append(
            f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) says in its `{said_wrong}` "
            "that the reviewer was wrong — a refuted finding is not a fix and not an open "
            "defect: file it under Ignored with `severity: info` and the evidence in "
            "`why:`, or it is counted as worth a look.")
    cites = DECIDED_ELSEWHERE.search(plain("why"))
    if pile == "findings" and cites and not CITED_LINE.search(plain("why")):
        warnings.append(
            f"Ignored: {title[:60]!r} (line {where}) — `why:` rests on {cites.group(0)!r} "
            "without the file:line it cites. Give it (`openspec/changes/<change>/design.md:50`, "
            "`Q&A.md:28`) so the page can link the line and quote it.")
    if pile == "assumptions" and not item.get("why"):
        warnings.append(
            f"Assumptions: {title[:60]!r} (line {where}) has no `why:` — one or two "
            "sentences on why this reading, and what holds the confidence where it is.")
    if pile != "autofixes" and item.get("fix"):
        warnings.append(f"{PILE_HEADING[pile]}: {title[:60]!r} (line {where}) carries "
                        "`fix:` — only a Fixed item has a repair to comment on. Ignored.")
        item.pop("fix")
    if pile == "findings" and "severity" not in item:
        # Declined, and nobody said how bad. `info` is the honest default: the pile is
        # "somebody looked at this and said no", and inventing a rank for it would put a
        # number on the page that no reviewer ever typed.
        item["severity"] = "info"
    if pile == "assumptions":
        # Who decided it is the only provenance an assumption has, and it has two values.
        # `source:` used to be carried through verbatim and rendered as the card's badge,
        # so a model that wrote `source: implementation decision` relabelled the whole
        # pile on the page. The badge is the builder's now; the field only says whether
        # the human made the call in the conversation (`source: human`) or the agent did.
        said = (item.pop("source", "") or "").strip().lower()
        item["decidedBy"] = "human" if said in HUMAN_SOURCES else "agent"
        if said and said not in HUMAN_SOURCES and said not in AGENT_SOURCES:
            warnings.append(
                f"Assumptions: {title[:60]!r} (line {where}) says `source: {said}` — an "
                "assumption's source is who decided it, `agent` or `human`; read as agent.")
    if body.strip():
        item["body"] = inline(body)
    if refs:
        item["refs"] = refs
    if snippets:
        item["snippets"] = snippets
    if fixed_in:
        base = (front.get("implementation") or "").strip()
        diffs = []
        for ref in refs or []:
            path = re.sub(RANGED, "", ref)
            d: dict = {"path": path}
            if base:
                d["base"] = base
            # `fixed-in: HEAD` means "the fix is in the current head", so the head side is
            # left as the working tree: that keeps the editor link, which always compares
            # against the working tree and is dropped from a pinned diff. Any other value
            # is a rev the reader is being pointed at, so it is pinned.
            if fixed_in.upper() != "HEAD":
                d["head"] = fixed_in
            diffs.append(d)
        if diffs:
            item["diffs"] = diffs
        else:
            problems.append(f"line {where}: {title[:40]!r} says `fixed-in: {fixed_in}` "
                            "but names no `file:` — there is nothing to diff")
    item["_fixed_in"] = fixed_in
    item["_line"] = where
    return item


def parse(text: str) -> dict:
    """The whole file. Raises `Unparseable` with every problem, not just the first."""
    lines = text.splitlines()
    problems: list[str] = []
    warnings: list[str] = []
    front, start = parse_front(lines, problems)

    piles: dict[str, list[dict]] = {"findings": [], "autofixes": [], "assumptions": []}
    seen_sections: dict[str, str] = {}
    note: dict | None = None
    note_lines: list[str] = []
    pile: str | None = None
    title: str | None = None
    fields: list[tuple[str, str]] = []
    body: list[str] = []
    at = 0
    in_body = False
    fenced = False

    def close() -> None:
        nonlocal title, fields, body, in_body
        if title is not None and pile is not None and pile in piles:
            piles[pile].append(build_item(title, fields, "\n".join(body), pile, front,
                                          problems, warnings, at))
        title, fields, body, in_body = None, [], [], False

    for n in range(start, len(lines)):
        raw = lines[n]
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            if title is not None:
                in_body = True
                body.append(raw)
            continue
        if fenced:
            if title is not None:
                body.append(raw)
            continue

        m2 = H2.match(raw)
        if m2:
            close()
            name = m2.group(1).strip().lower().rstrip(":")
            target = SECTIONS.get(name)
            if target is None and name.startswith(NOTE_HEADINGS):
                if note is not None:
                    problems.append(f"line {n + 1}: '## {m2.group(1).strip()}' is a second "
                                    f"note section; the first was '{note['heading']}' — "
                                    "one takeover note per file, or the page cannot say "
                                    "which one the review point was moved to")
                note = {"heading": m2.group(1).strip(), "line": n + 1}
                note_lines = []
                pile = "note"
                continue
            if target is None:
                problems.append(
                    f"line {n + 1}: unknown section '## {m2.group(1).strip()}' — this "
                    f"file has exactly three piles: {', '.join(PILE_HEADING.values())} "
                    f"(aliases: {', '.join(sorted(SECTIONS))}). A section nobody reads "
                    "looks the same on the page as a section nobody wrote.")
                pile = None
                continue
            if target in seen_sections:
                problems.append(f"line {n + 1}: '## {m2.group(1).strip()}' repeats the "
                                f"{PILE_HEADING[target]} pile, already opened as "
                                f"'{seen_sections[target]}'")
            seen_sections.setdefault(target, m2.group(1).strip())
            pile = target
            continue

        m3 = H3.match(raw)
        if m3 and pile == "note":
            problems.append(f"line {n + 1}: '### {m3.group(1).strip()[:40]}' sits under "
                            f"the note '{note['heading']}' — a note is prose only; an "
                            "item filed there is on no pile and would never be rendered")
            continue
        if pile == "note":
            note_lines.append(raw)
            continue
        if m3:
            close()
            if pile is None:
                problems.append(f"line {n + 1}: '### {m3.group(1).strip()[:40]}' is not "
                                "under any of the three sections — an item outside a "
                                "pile has nowhere to be rendered")
                continue
            title, at = m3.group(1).strip(), n + 1
            continue

        if title is None:
            continue

        mf = FIELD.match(stripped)
        if mf and not in_body:
            fields.append((mf.group(1).strip().lower(), mf.group(2).strip()))
            continue
        if mf and mf.group(1).strip().lower() in FIELDS:
            # A known field after the prose has started. Swallowing it into the body would
            # lose a ref — and losing a ref is what gets the item dropped by the anchoring
            # rule, so the failure would surface as a missing item, three steps away.
            problems.append(f"line {n + 1}: `{mf.group(1).strip().lower()}:` comes after "
                            f"the prose of {title[:40]!r} — every field goes directly "
                            "under the `###` line, before the body")
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if not in_body and fields and stripped and indent >= 2:
            # A wrapped field: the line right after `- key: value` continues it as long as
            # it is indented and is not itself another `- key:` — the agent word-wraps a
            # long `why:`/`alternative:` the way it wraps any other sentence, and the field
            # is not done just because the line is. Glued with a space, not a newline: the
            # field is one sentence, not a paragraph.
            key, value = fields[-1]
            fields[-1] = (key, f"{value} {stripped}".strip())
            continue
        if stripped or body:
            in_body = True
            body.append(raw)
    close()

    if not seen_sections and not problems:
        problems.append(
            "no '## Fixed' / '## Ignored' / '## Assumptions' section anywhere — this is "
            "prose, not review-points.md. See reference/review-points.md.")
    if problems:
        raise Unparseable(problems)

    if note is not None:
        body = "\n".join(note_lines).strip()
        if not body:
            problems.append(f"line {note['line']}: the note '{note['heading']}' says "
                            "nothing — a takeover with no sentence beside it is the "
                            "silent reset it exists to prevent")
            raise Unparseable(problems)
        note = {"heading": note["heading"], "html": note_html(body)}
    return {"front": front, "piles": piles, "warnings": warnings, "note": note,
            "sections": {PILE_HEADING[k]: v for k, v in seen_sections.items()}}


def anchored(item: dict) -> bool:
    return bool(item.get("refs") or item.get("snippets") or item.get("diffs"))


def drop_unanchored(piles: dict[str, list[dict]]) -> tuple[int, list[str]]:
    """The rule the build has always applied to assumptions, applied to all three piles.

    An item a reader cannot go and look at is indistinguishable from one that was never
    true, and this file is written by the same model whose work it describes — so the
    anchor is not a formatting preference, it is the only thing separating a record from
    a recollection of a record.
    """
    warnings: list[str] = []
    dropped = 0
    for key, items in piles.items():
        kept = []
        for item in items:
            if anchored(item):
                kept.append(item)
                continue
            dropped += 1
            warnings.append(
                f"{PILE_HEADING[key]}: {item.get('title', '')[:60]!r} (line "
                f"{item.get('_line')}) names no code — dropped. Give it a `- file: "
                "path:line`; an item nobody can go and look at says nothing.")
        piles[key] = kept
    return dropped, warnings


def top_fixed_in(front: dict, piles: dict[str, list[dict]]) -> str | None:
    """Where the fixes landed, when the file gives one answer.

    Frontmatter wins; otherwise the items' own `fixed-in` do, but only if they agree. Two
    different revs is a real thing to say and not one this key can say, so it says nothing
    rather than picking the first.
    """
    if front.get("fixed-in"):
        return front["fixed-in"].strip()
    values = {i["_fixed_in"] for items in piles.values() for i in items if i.get("_fixed_in")}
    return values.pop() if len(values) == 1 else None


def _body_of(text: str) -> str:
    """The file without its front-matter: the part whose `file:line`s are anchors."""
    lines = text.splitlines()
    _, start = parse_front(lines, [])
    return "\n".join(lines[start:]).strip()


#: How far back `recorded_in` looks for the commit that last wrote the items.
RECORDED_SCAN = 200


def recorded_in(root: Path, rel: str) -> str | None:
    """The commit that recorded the items as they are on disk, or None.

    The last commit that changed the file's *items* — but only when the working copy
    matches it, because at `record-review.py finish` time the file is about to be committed
    and the last commit that touched it is the *previous* review's.

    A commit that only rewrote the front-matter is skipped: its `file:line`s were written
    at the commit before it. visit-has-vet's `5f84b2cd` moved `base:` after merging main,
    and taking it as the review commit carried every ref from the wrong tree — the page
    quoted `@WithSpan` for a finding about `visit.setVet(…)`, and called the seven retouch
    commits between the review and that one "fix commits"."""
    def git(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    def show(rev: str) -> str | None:
        r = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{rel}"],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    if git("status", "--porcelain", "--", rel):
        return None
    shas = git("log", f"-{RECORDED_SCAN}", "--format=%H", "--", rel).split()
    for sha in shas:
        now, before = show(sha), show(f"{sha}^")
        if now is None or before is None or _body_of(now) != _body_of(before):
            return sha
    return shas[0] if shas else None


def provenance(front: dict, root: Path | None, rel: str) -> dict:
    """Which commit is which, out of the front-matter, plus the recording commit.

    `auditedBase` falls back to `base` and `auditedHead` to `implementation`: a file
    written before the two were told apart said one range and one commit, and those were
    what the reviewers read."""
    out = {key: front[k].strip() for k, key in PROVENANCE.items() if front.get(k, "").strip()}
    if "base" in out:
        out.setdefault("auditedBase", out["base"])
    if "implementation" in out:
        out.setdefault("auditedHead", out["implementation"])
    if root is not None and "reviewCommit" not in out:
        sha = recorded_in(root, rel)
        if sha:
            out["reviewCommit"] = sha
    return out


# --------------------------------------------------------------------------- anchors
# A `file:line` is written against one version of the file and read against another. The
# assumptions are written while coding, at the implementation commit; the fixes and the
# declined findings after the fixes, at the review commit; the page reads all of them off
# HEAD. Run 6's `[auto-fix]` commit added two lines above an assumption's anchor, and the
# card quoted the blank line that now sat at 85 under an `unchanged` badge. A ref is
# carried across those commits through the diff's own hunks, never by trusting the number.

HUNK_HEAD = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: "Written against the working tree" — a rev `reanchor` can be told about, at
#: `record-review.py finish` time, when the fixes are on disk and not yet committed.
WORKTREE = "@worktree"
REF_LINES = re.compile(r"^(?P<path>.*?):(?P<spans>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)$")


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def file_hunks(root: Path, rel: str, frm: str, to: str | None = None) -> list[dict] | None:
    """`git diff -U0 frm [to] -- rel` as `[{a, b, c, d, old, new}]` (old start/count, new
    start/count, the removed and added lines); `to=None` is the working tree. None when
    git cannot diff it."""
    r = _run_git(root, "diff", "-U0", "--no-color", "--no-ext-diff", frm,
                 *([to] if to else []), "--", rel)
    if r.returncode != 0:
        return None
    hunks: list[dict] = []
    cur = None
    for line in r.stdout.splitlines():
        m = HUNK_HEAD.match(line)
        if m:
            cur = {"a": int(m[1]), "b": int(m[2]) if m[2] is not None else 1,
                   "c": int(m[3]), "d": int(m[4]) if m[4] is not None else 1,
                   "old": [], "new": []}
            hunks.append(cur)
        elif cur is not None and line.startswith("-"):
            cur["old"].append(line[1:])
        elif cur is not None and line.startswith("+"):
            cur["new"].append(line[1:])
    return hunks


#: How alike a rewritten line must be to its old self to be called the same line.
REWRITE_LIKE = 0.6


def _rewritten(old: str, new: list[str]) -> int | None:
    """The index of the line in `new` that `old` was most plausibly rewritten into."""
    key = old.strip()
    if not key:
        return None
    hits = [i for i, t in enumerate(new) if t.strip() == key]
    if hits:
        return hits[0]
    from difflib import SequenceMatcher
    scored = [(SequenceMatcher(None, key, t.strip()).ratio(), -i, i)
              for i, t in enumerate(new) if t.strip()]
    best = max(scored, default=None)
    return best[2] if best and best[0] >= REWRITE_LIKE else None


def map_line(hunks: list[dict], n: int) -> int | None:
    """Line `n` of the old side, on the new side — None when the hunk that covers it
    removed it. A rewritten line inside a hunk of equal size keeps its place; inside an
    uneven one it is found again by its text — the same text nearest its old place, else
    the most similar line the hunk wrote (visit-has-vet's `20e1df32` split
    `visit.setVet(vetRepository.findByIdOrNull(…))` into `Vet vet = vetRepository…` and
    `visit.setVet(vet)`: the finding about it is about the first) — or it is gone."""
    off = 0
    for h in hunks:
        a, b, c, d = h["a"], h["b"], h["c"], h["d"]
        if b == 0:                       # pure insertion after old line `a`
            if n > a:
                off += d
                continue
            break
        if n < a:
            break
        if n <= a + b - 1:
            if b == d:
                return c + (n - a)
            key = (h["old"][n - a] if n - a < len(h["old"]) else "").strip()
            hits = [i for i, t in enumerate(h["new"]) if key and t.strip() == key]
            if hits:
                return c + min(hits, key=lambda i: abs(i - (n - a)))
            near = _rewritten(key, h["new"])
            return c + near if near is not None else None
        off += d - b
    return n + off


def ref_spans(ref: str) -> tuple[str, list[tuple[int, int]] | None]:
    m = REF_LINES.match(ref)
    if not m:
        return ref, None
    spans = []
    for part in m["spans"].split(","):
        lo, _, hi = part.partition("-")
        spans.append((int(lo), int(hi or lo)))
    return m["path"], spans


def spans_ref(path: str, spans: list[tuple[int, int]]) -> str:
    return path + ":" + ",".join(f"{a}-{b}" if b != a else f"{a}" for a, b in spans)


def remap_ref(root: Path, ref: str, frm: str, to: str | None = None) -> str | None:
    """`ref`, written at `frm`, as it reads at `to` (None: the working tree). Unchanged when
    it names no lines, or the file did not exist at `frm` (it cannot have been written
    there); None when no line of it survived."""
    path, spans = ref_spans(ref)
    if frm == WORKTREE and to is None:
        return ref
    if spans is None or frm == WORKTREE \
            or _run_git(root, "cat-file", "-e", f"{frm}:{path}").returncode != 0:
        return ref
    hunks = file_hunks(root, path, frm, to)
    if not hunks:                        # git could not say, or the file did not move
        return ref
    out = []
    for lo, hi in spans:
        got = [x for x in (map_line(hunks, n) for n in range(lo, hi + 1)) if x is not None]
        if got:
            out.append((min(got), max(got)))
    return spans_ref(path, out) if out else None


def ref_lines(root: Path, ref: str, at: str | None = None) -> list[str] | None:
    """The lines `ref` names, at `at` (None: the working tree) — None when the file is not
    there or the ref starts past its end."""
    path, spans = ref_spans(ref)
    if at:
        r = _run_git(root, "show", f"{at}:{path}")
        if r.returncode != 0:
            return None
        text = r.stdout
    else:
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    lines = text.splitlines()
    if spans is None:
        return lines
    if spans[0][0] > len(lines):
        return None
    return [lines[n - 1] for lo, hi in spans for n in range(lo, min(hi, len(lines)) + 1)]


def blank_ref(root: Path, ref: str, at: str | None = None) -> bool:
    """Whether every line `ref` names is blank (or past the end of the file)."""
    got = ref_lines(root, ref, at)
    return got is None or not any(x.strip() for x in got)


def reanchor(root: Path, ref: str, written: list[str], to: str | None = None) -> dict:
    """Where `ref` points at `to`, given the revs it may have been written at, most likely
    first. `{"ref": new or None, "from": the rev it was read as, "moved": bool,
    "blank": bool}`.

    The first rev wins unless it lands on a blank line while a later one lands on code:
    the pile says which commit a ref was most likely written against, and a blank line is
    the one answer that is certainly wrong. A line the first rev's diff *removed* is not
    retried — the later rev would quote whatever now has that number, which is exactly
    the unrelated line this exists to stop showing."""
    tried = []
    for rev in [w for w in written if w]:
        new = remap_ref(root, ref, rev, to)
        tried.append({"ref": new, "from": rev, "moved": new != ref,
                      "blank": new is None or blank_ref(root, new, to)})
        if new is None and len(tried) == 1:
            return tried[0]
    if not tried:
        return {"ref": ref, "from": None, "moved": False, "blank": blank_ref(root, ref, to)}
    return next((t for t in tried if t["ref"] and not t["blank"]), tried[0])


# ------------------------------------------------------------------ the commit that fixed it
# Every Fixed item was fixed by a commit on the branch, and its buttons on the page — and its
# note on the forge — go to that commit (Victor, 30 Oct 2026: an auto-fix is not a PR
# comment, it is a commit). The round's fix commit has always been recorded (`fixed-in`, the
# review commit); which commit fixed *each* item is read off the history here, once, for the
# page and for `push-pr-comments.py`, and written into `review-points.json` as `fixCommit`.

#: The subject an `[auto-fix]` commit opens on, and the trailer a review commit carries.
FIX_GREP = (r"--grep=^\[auto-fix\]", "--grep=^Review-Points:")


def _sha_of(root: Path, rev: str | None) -> str | None:
    if not rev or rev == WORKTREE:
        return None
    r = _run_git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return r.stdout.strip() or None if r.returncode == 0 else None


def _is_ancestor(root: Path, a: str, b: str) -> bool:
    return _run_git(root, "merge-base", "--is-ancestor", a, b).returncode == 0


def round_fix_commit(root: Path | None, prov: dict) -> str | None:
    """The commit whose hunks are the round's fixes, or None when there is none after the
    implementation commit: the review commit when it descends from the implementation and
    is not it, else the newest commit after the implementation whose subject opens on
    `[auto-fix]` or whose message carries a `Review-Points:` trailer."""
    impl = (prov or {}).get("implementation") or (prov or {}).get("auditedHead")
    impl_sha = _sha_of(root, impl) if root else None
    if not impl_sha:
        return None
    review = _sha_of(root, (prov or {}).get("reviewCommit"))
    if review and review != impl_sha and _is_ancestor(root, impl_sha, review):
        return review
    r = _run_git(root, "log", "--format=%H", *FIX_GREP, f"{impl_sha}..HEAD")
    return next((ln.strip() for ln in r.stdout.splitlines() if ln.strip()), None)


def _blamed(root: Path, ref: str, at: str) -> list[str]:
    """The commits that last wrote each line `ref` names, as of `at`."""
    path, spans = ref_spans(ref)
    out: list[str] = []
    for lo, hi in spans or []:
        r = _run_git(root, "blame", "--porcelain", "-L", f"{lo},{hi}", at, "--", path)
        out += [ln.split()[0] for ln in r.stdout.splitlines()
                if re.match(r"^[0-9a-f]{40} \d+ \d+", ln)]
    return out


def item_fix_commit(root: Path | None, refs: list[str], impl: str | None, head: str | None,
                    explicit: str | None = None) -> str | None:
    """The commit that fixed one item: full sha, or None when nothing after the
    implementation can have.

    `explicit` (the item's own `fixed-in: <sha>`, not `HEAD`) wins. Otherwise the commits in
    `impl..head` that touched the item's files: one is the answer; several are told apart by
    who last wrote the item's own lines at `head` (`git blame`), the newest winning a tie;
    none (a fix that only deleted lines, or refs elsewhere) leaves the round's commit, `head`.
    `head` is the round's fix commit (`round_fix_commit`)."""
    if root is None:
        return None
    pinned = _sha_of(root, explicit) if explicit and explicit.upper() != "HEAD" else None
    if pinned:
        return pinned
    impl_sha, head_sha = _sha_of(root, impl), _sha_of(root, head)
    if not (impl_sha and head_sha) or impl_sha == head_sha:
        return None
    files = sorted({ref_spans(r)[0] for r in refs or [] if r})
    touched = _run_git(root, "log", "--format=%H", f"{impl_sha}..{head_sha}", "--",
                       *files).stdout.split() if files else []
    if len(touched) <= 1:
        return touched[0] if touched else head_sha
    votes: dict[str, int] = {}
    for ref in refs or []:
        for sha in _blamed(root, ref, head_sha):
            if sha in touched:
                votes[sha] = votes.get(sha, 0) + 1
    if not votes:
        return touched[0]                # newest first: the last word on those files
    best = max(votes.values())
    return next(sha for sha in touched if votes.get(sha) == best)


def fix_commit_of(root: Path | None, refs: list[str], prov: dict, own: str | None = None,
                  pinned: str | None = None) -> str | None:
    """The commit that fixed one Fixed item, by the whole rule: `item_fix_commit` over the
    round (`round_fix_commit`, or the rev the front-matter's `fixed-in` pins). A record with
    no implementation commit has no range to read: there `fixed-in: HEAD` — the item's own
    (`own`) or the file's (`pinned`) — names the commit that recorded it, the review commit."""
    if root is None:
        return None
    prov = prov or {}
    head = (_sha_of(root, pinned) if pinned and pinned.upper() != "HEAD" else None) \
        or round_fix_commit(root, prov)
    impl = prov.get("implementation") or prov.get("auditedHead")
    sha = item_fix_commit(root, refs, impl, head, own)
    if not sha and "HEAD" in {(own or "").strip().upper(), (pinned or "").strip().upper()}:
        sha = _sha_of(root, prov.get("reviewCommit"))
    return sha


def stamp_fix_commits(doc: dict, root: Path | None, fixed_in: str | None = None) -> None:
    """`fixCommit` on every Fixed item whose fixing commit the history names."""
    for item in doc.get("autofixes") or [] if root is not None else []:
        sha = fix_commit_of(root, item.get("refs") or [], doc.get("provenance") or {},
                            item.get("_fixed_in") or None, fixed_in)
        if sha:
            item["fixCommit"] = sha


def document(path: Path, rel: str, root: Path | None = None) -> dict:
    """The parsed file as the build reads it, plus what had to be thrown away."""
    parsed = parse(path.read_text(encoding="utf-8", errors="replace"))
    piles = parsed["piles"]
    total = sum(len(v) for v in piles.values())
    dropped, unanchored = drop_unanchored(piles)
    # Field warnings first: they name a line the author can still go and fix, whereas the
    # anchoring ones name an item that is already gone from the page.
    warnings = parsed["warnings"] + unanchored
    kept = sum(len(v) for v in piles.values())
    front = parsed["front"]
    # Read before the bookkeeping keys are stripped: `fixed_in` is derived from the items'
    # own `fixed-in`, which is exactly what is about to be thrown away.
    fixed_in = top_fixed_in(front, piles)
    prov = provenance(front, root, rel)
    # Which commit fixed each Fixed item, read while each still has its own `fixed-in`.
    stamp_fix_commits({"provenance": prov, "autofixes": piles["autofixes"]}, root, fixed_in)
    for items in piles.values():
        for item in items:
            item.pop("_fixed_in", None)
            item.pop("_line", None)
    return {
        "schema": review_points_schema.SCHEMA_VERSION,
        "mode": "points",
        "source": rel,
        "provenance": prov,
        "fixed_in": fixed_in,
        "findings": piles["findings"],
        "autofixes": piles["autofixes"],
        "assumptions": piles["assumptions"],
        "meta": {k: front[k] for k in
                 ("ticket", "base", "implementation", "reviewers", "session") if k in front},
        "frontmatter": front,
        "sections": parsed["sections"],
        "note": parsed.get("note"),
        "items": kept, "dropped": dropped, "warnings": warnings,
        "empty": total == 0,
    }


def report(doc: dict, out: Path, write: bool) -> None:
    src = doc["source"]
    print(f"{src}: {doc['items']} item(s) in "
          + ", ".join(f"{len(doc[k])} {PILE_HEADING[k].lower()}"
                      for k in ("autofixes", "findings", "assumptions")))
    for key, heading in (("autofixes", "Fixed"), ("findings", "Ignored"),
                         ("assumptions", "Assumptions")):
        if heading not in doc["sections"]:
            print(f"  {heading:<12} — no such section in the file")
            continue
        print(f"  {heading:<12} ({doc['sections'][heading]})")
        for item in doc[key]:
            marks = []
            if item.get("refs"):
                marks.append(f"{len(item['refs'])} ref")
            if item.get("snippets"):
                marks.append(f"{len(item['snippets'])} snippet")
            if item.get("diffs"):
                marks.append(f"{len(item['diffs'])} diff")
            if item.get("severity"):
                marks.append(item["severity"])
            if "confidence" in item:
                marks.append(f"confidence {item['confidence']:g}")
            if item.get("source"):
                marks.append(f"from {item['source']}")
            if item.get("decidedBy") == "human":
                marks.append("decided by the human")
            print(f"      · {re.sub('<[^>]+>', '', item['title'])[:70]}"
                  + (f"   [{', '.join(marks)}]" if marks else ""))
    if doc.get("note"):
        print(f"  Note         ({doc['note']['heading']}) — prose, shown as a band")
    if doc["fixed_in"]:
        print(f"  fixed in     {doc['fixed_in']}")
    if doc["meta"]:
        print("  " + " · ".join(f"{k}: {v}" for k, v in doc["meta"].items()))
    print(f"  {'would write' if not write else 'wrote'}  {out}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="the repository root (default: cwd)")
    ap.add_argument("--file", help=f"the points file, relative to --root (default: "
                                   f"human-review.json's \"reviewPoints\", else {DEFAULT_FILE})")
    ap.add_argument("--out", help=f"where the JSON goes (default: <root>/{DEFAULT_OUT})")
    ap.add_argument("--check", action="store_true",
                    help="parse and print what was understood; write nothing. Run this "
                         "before committing the file — a malformed file is worth catching "
                         "while somebody can still fix it")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    rel = args.file or config_path(root) or DEFAULT_FILE
    path = root / rel
    out = Path(args.out) if args.out else root / DEFAULT_OUT

    if not path.is_file():
        print(f"[review-points] no {rel} at {root} — nothing on this branch records what "
              f"was reviewed, fixed or declined", file=sys.stderr)
        return 3
    try:
        doc = document(path, rel, root)
    except Unparseable as bad:
        print(f"[review-points] {rel} cannot be read:", file=sys.stderr)
        for problem in bad.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 4

    for warning in doc["warnings"]:
        print(f"[review-points] WARNING: {warning}", file=sys.stderr)

    if doc["dropped"] and doc["items"] == 0:
        print(f"[review-points] every item in {rel} was unanchored — the file is there and "
              f"says nothing checkable. Not the same thing as a clean review.",
              file=sys.stderr)
        return 5

    # The report is a contract, checked before anything relies on it — the build checks
    # it again on the way in. A failure here is this parser producing a shape the schema
    # refuses, never something the agent's file can be blamed for in prose.
    bad = review_points_schema.problems(doc)
    if bad:
        print(f"[review-points] the report built from {rel} does not match "
              f"{review_points_schema.SCHEMA_PATH.name}:", file=sys.stderr)
        for problem in bad:
            print(f"  - {problem}", file=sys.stderr)
        return 6

    if args.check:
        report(doc, out, write=False)
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    report(doc, out, write=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
