#!/usr/bin/env python3
"""SKILL.md's own consistency: every tab the worked schema declares must be fed by at
least one `steps-ledger.py start` call in the runbook, and every tab a step names must be
a real tab id — not a typo, not one the schema renamed out from under it.

This is the guard for the gap `steps-ledger.py` exists to close: the mechanism works, but
nothing enforced that a step's wrap in SKILL.md still points at a tab id the schema
recognises. `data,packages` in Step 2, wired against a `"tabs"` array where `"packages"`
got renamed to `"pkgs"`, would parse, run, and silently misattribute cost forever — exactly
the kind of drift this project pins with a test rather than trusting the next edit to catch
it, the same instinct behind `test_tooltips.py` and the ast-grep-rules sync test.

Those two assertions were the whole file, and they were weaker than they looked: they only
ever read `start` calls. A step wrapped with `start` and no matching `end` passed all of
them while leaving a record open for the rest of the run; so did a step whose `end` read
the *wrong* handle file, which closes somebody else's record and mis-attributes both. And
nothing checked that the ledger is cleared at the top of a run, which is the one omission
that turns "not measured" into a confident `$0.00`. Those are the invariants a real run
depends on and a reader of SKILL.md cannot verify by eye across 1200 lines, so they are
checked here too:

* every `start` writes its index to a `.human-review/.step-<name>` handle file, and some
  `end` reads that same file;
* no `end` reads a handle no `start` ever wrote;
* a handle always names the same tabs, wherever it appears (Step 9 re-quotes Step 2's
  block as its worked example — the same handle, deliberately, and it must stay the same);
* Step 0 calls `steps-ledger.py reset` before anything expensive runs.

Run with:  python3 -m pytest test_skill_tab_ledger_wiring.py
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_MD = Path(__file__).resolve().parents[1] / "SKILL.md"

HANDLE = r"\.human-review/\.step-[a-zA-Z0-9_-]+"

# Step 9 stamps its own assembly of the page against this reserved id. It is deliberately not
# in the `"tabs"` array — it is the name for the cost that belongs to no tab — so every check
# below that reasons about "tabs the page has" has to know to step around it.
GUIDE = "guide"

# Tabs the page has that no step in this runbook produces, for three different reasons worth
# keeping apart:
#   overview   — synthesised from the other tabs; never will have a step.
#   autofixed  — re-presents Step 1's output on a second surface; must NOT get a step, because
#                widening Step 1's wrap to name it would split that step's cost evenly and
#                halve the Review tab's published number in favour of a tab that did no work.
#   requirements — a tab the user asked for whose step has not been built yet; will get one.
# All three honestly report "not measured", so they are exempt from the every-tab-has-a-step
# rule — but only by being named here, so that building the missing step forces a deliberate
# edit rather than quietly satisfying a test that had stopped looking.
UNFED_BY_DESIGN = {"overview", "autofixed", "requirements"}

# A `start` and its redirect are routinely split across a `\`-continued line, so the text is
# unfolded before anything is matched — otherwise every multi-line wrap in the file (which is
# all of them) reads as a `start` with no handle at all, and the pairing checks below pass
# vacuously on a file where nothing is paired.
_CONTINUATION = re.compile(r"\\\s*\n\s*")


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _unfolded(text: str) -> str:
    return _CONTINUATION.sub(" ", text)


def _schema_tab_ids(text: str) -> set[str]:
    """The tab ids declared in the worked `"tabs"` array example under Step 9."""
    m = re.search(r'"tabs":\s*\[(.*?)\n\]', text, re.S)
    assert m, 'could not find the worked `"tabs": [...]` example in SKILL.md'
    return set(re.findall(r'\{"id"\s*:\s*"([a-zA-Z0-9_]+)"', m.group(1)))


def _starts(text: str) -> list[tuple[frozenset[str], str]]:
    """Every real `start` call, as `(tabs, handle file)`.

    The generic `<tab[,tab2]>` mention in the intro and the bare `start`/`end` mention in
    Step 0's forward pointer both fail to match — the placeholder starts with `<` and the
    bare mention is followed by a backtick, neither of which is a tab-id character."""
    pattern = re.compile(
        r"steps-ledger\.py start ([a-zA-Z][a-zA-Z0-9_,]*)[^\n]*?>\s*(" + HANDLE + ")")
    return [(frozenset(tabs.split(",")), handle)
            for tabs, handle in pattern.findall(_unfolded(text))]


def _ends(text: str) -> list[str]:
    """The handle file each real `end` call reads its index back out of."""
    return re.findall(r"steps-ledger\.py end \"\$\(cat (" + HANDLE + r")\)\"",
                      _unfolded(text))


def _ledger_start_tabs(text: str) -> set[str]:
    tabs: set[str] = set()
    for group, _handle in _starts(text):
        tabs |= group
    return tabs


def test_skill_md_is_readable():
    assert SKILL_MD.is_file(), f"expected SKILL.md at {SKILL_MD}"


def test_schema_example_has_the_tabs_this_test_expects():
    # Sanity check on the parser itself, so a regex that silently matched nothing still
    # fails loudly instead of making both real tests vacuously pass.
    schema = _schema_tab_ids(_text())
    assert schema == {
        "review", "autofixed", "behaviour", "sequence", "requirements", "data",
        "packages", "api", "city", "complexity", "logging", "dsaudit", "owners",
    }


def test_the_wrap_parser_finds_every_step_not_just_the_unindented_ones():
    # The same sanity check for the `start`/`end` parsers. Both regexes are picky enough
    # (a redirect on a continued line, a `$(cat …)` argument) that a formatting change could
    # quietly drop them to zero matches, at which point every pairing test below passes by
    # finding nothing wrong with nothing.
    text = _text()
    starts, ends = _starts(text), _ends(text)
    assert len(starts) >= 10, f"only {len(starts)} `start` wraps parsed out of SKILL.md"
    assert len(ends) >= 10, f"only {len(ends)} `end` wraps parsed out of SKILL.md"


def test_every_step_wired_tab_is_a_real_schema_tab():
    text = _text()
    schema = _schema_tab_ids(text)
    wired = _ledger_start_tabs(text)
    unknown = wired - schema - {GUIDE}
    assert not unknown, (
        f"steps-ledger.py start names tab(s) {sorted(unknown)} that are not in the worked "
        f'"tabs" example ({sorted(schema)}) — a typo, or the schema moved on without the '
        "step wrap"
    )


def test_every_schema_tab_has_a_feeding_step():
    text = _text()
    schema = _schema_tab_ids(text)
    wired = _ledger_start_tabs(text)
    unfed = schema - wired - UNFED_BY_DESIGN
    assert not unfed, (
        f"tab(s) {sorted(unfed)} appear in the worked \"tabs\" example but no step in "
        "SKILL.md stamps the ledger for them — every real run will show \"not measured\" "
        "on that tab"
    )


def test_step_1_records_the_pre_fix_revision():
    """The before side of every Auto-fixed diff. It exists for exactly one instant — before
    the reviews touch the tree — and no commit message, ledger entry or transcript can be
    trusted to reconstruct it afterwards: on the reference run the obvious candidate commit
    turned out to contain none of the fixes its message described."""
    text = _unfolded(_text())
    m = re.search(r"steps-ledger\.py start review\b[^\n]*", text)
    assert m, "Step 1 no longer opens a `review` step"
    assert "--rev" in m.group(0), (
        "Step 1's wrap does not pass --rev, so nothing records which revision the automated "
        f"fixes were applied on top of:\n  {m.group(0)}"
    )
    assert "rev-parse" in m.group(0), (
        "--rev is passed something other than a resolved git revision"
    )


def test_step_9_stamps_the_pages_own_assembly():
    """Writing `content.json` is normally the largest single stretch of a run, and it cannot
    be attributed to a tab. Unstamped it lands in the residual beside idle time, and the
    breakdown degenerates into one row carrying most of the bill."""
    text = _text()
    assert GUIDE in _ledger_start_tabs(text), (
        f"no `steps-ledger.py start {GUIDE}` in SKILL.md — Step 9's own cost disappears "
        "into an undifferentiated residual"
    )


def test_the_reserved_id_is_spelled_the_same_in_both_scripts():
    """`steps-ledger.py check` must not report it as drift and `review-cost.py` must bucket
    it separately; they hold the constant independently, so a rename in one is a silent
    regression in the other."""
    here = Path(__file__).resolve().parent
    for script in ("steps-ledger.py", "review-cost.py"):
        src = (here / script).read_text(encoding="utf-8")
        assert f'GUIDE_TAB = "{GUIDE}"' in src, f"{script} does not reserve `{GUIDE}`"


def test_the_canonical_id_table_and_the_step_wraps_name_the_same_ids():
    """The table under the tabs array is the vocabulary Step 9 is told to key its tabs from.
    If it and the wraps disagree, following the document produces a page the ledger cannot
    describe — which is exactly the drift the table was added to stop."""
    text = _text()
    m = re.search(r"\| `id` \| label on the reference page \|.*?\n\n", text, re.S)
    assert m, "could not find the canonical tab-id table in SKILL.md"
    tabled = set(re.findall(r"`([a-z][a-z0-9_]*)`", m.group(0).split("\n", 2)[2]))
    tabled = {i for i in tabled if not i.startswith("step")}
    wired = _ledger_start_tabs(text)
    assert wired - tabled == set(), (
        f"the step wraps stamp {sorted(wired - tabled)}, which the id table does not list — "
        "a wrap was re-keyed without updating the vocabulary"
    )
    assert tabled - wired == UNFED_BY_DESIGN & tabled, (
        f"the id table lists {sorted(tabled - wired)} with no step wrap behind them; only "
        f"{sorted(UNFED_BY_DESIGN)} may be unfed, and that exemption is declared in this test"
    )


def test_the_table_covers_every_tab_the_worked_example_declares():
    """The table is what Step 9 keys its tabs from, so a tab in the example it does not name
    is a tab whose id nothing pins."""
    text = _text()
    m = re.search(r"\| `id` \| label on the reference page \|.*?\n\n", text, re.S)
    assert m, "could not find the canonical tab-id table in SKILL.md"
    tabled = {i for i in re.findall(r"`([a-z][a-z0-9_]*)`", m.group(0).split("\n", 2)[2])
              if not i.startswith("step")}
    missing = _schema_tab_ids(text) - tabled
    assert not missing, f"tab(s) {sorted(missing)} are in the worked example but not the table"


def test_every_started_step_is_also_ended():
    """An open record is not a neutral omission: the tab it names reports a lower bound
    forever, and the reader is told a step crashed when in fact the runbook simply never
    said to close it."""
    text = _text()
    opened = {handle for _tabs, handle in _starts(text)}
    closed = set(_ends(text))
    dangling = opened - closed
    assert not dangling, (
        f"step handle(s) {sorted(dangling)} are written by a `start` but never read back by "
        "an `end` — those records stay open for the whole run and their tab reports "
        "'started but never recorded finishing'"
    )


def test_no_end_reads_a_handle_no_start_ever_wrote():
    """`end \"$(cat .step-typo)\"` fails its own argument parsing on an empty read and the
    record it should have closed stays open — a silent failure at both ends."""
    text = _text()
    opened = {handle for _tabs, handle in _starts(text)}
    orphan_ends = set(_ends(text)) - opened
    assert not orphan_ends, (
        f"`end` reads handle file(s) {sorted(orphan_ends)} that no `start` writes — a typo "
        "in the path, or a wrap whose halves were renamed apart"
    )


def test_a_handle_file_always_names_the_same_tabs():
    """Two steps sharing one handle overwrite each other's index, so the second `end` closes
    the first step's record and the second never closes at all. Step 9 re-quotes Step 2's
    block verbatim as its worked example, which is why this checks consistency rather than
    uniqueness — the same handle twice is fine, the same handle meaning two different things
    is not."""
    seen: dict[str, frozenset[str]] = {}
    clashes = []
    for tabs, handle in _starts(_text()):
        if handle in seen and seen[handle] != tabs:
            clashes.append((handle, sorted(seen[handle]), sorted(tabs)))
        seen.setdefault(handle, tabs)
    assert not clashes, (
        f"handle file(s) reused for different steps: {clashes} — the second `start` "
        "overwrites the first's index and one of the two records can never be closed"
    )


def test_step_0_clears_last_runs_ledger():
    """The one omission that produces a confident wrong number rather than an honest blank:
    a ledger inherited from the previous run parses fine, so every tab it names is reported
    as *measured* while this run's turns all fall outside its windows — a `$0.00` on every
    row."""
    text = _text()
    assert re.search(r"steps-ledger\.py reset", text), (
        "Step 0 never clears `.human-review/.steps.json` — a second run in the same "
        "worktree reports every tab as a measured $0.00"
    )
    reset_at = text.index("steps-ledger.py reset")
    first_start = min(m.start() for m in re.finditer(r"steps-ledger\.py start [a-zA-Z]", text))
    assert reset_at < first_start, (
        "the `reset` appears after the first `start` wrap — it would erase the very records "
        "the run had just stamped"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
