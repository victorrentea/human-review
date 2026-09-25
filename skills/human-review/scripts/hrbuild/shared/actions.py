"""The register of commands the served page may ask the server to run."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# what the page is allowed to ask the server to run
# --------------------------------------------------------------------------- #

# Dot-prefixed deliberately: `publish-demo.sh` publishes everything in a run directory
# that does not begin with a dot, and the downloadable zip is built from what it
# publishes. A manifest that travelled with either would be a list of this machine's
# commands sitting next to a page on someone else's, offering to run them.
ACTIONS_FILE = ".actions.json"

# Three buttons on this page describe a command and hand it to the clipboard, because a
# file on disk cannot run anything. Served by serve-review.py they can — but the command
# must not travel from the page, or "the review guide" becomes "a shell on :7654 that any
# tab in the browser can reach". So the page sends an id and the server looks the command
# up here, in a manifest written beside review.html by this build.
#
# The consequences are the point, not a side effect:
#   * a page from an older build can only name ids the *current* build still declares;
#   * the copy in the zip and the copy on GitHub Pages sit next to no manifest at all, so
#     they can ask for nothing — which is also exactly what they could do before;
#   * every command in it was written by this build out of the content file, so reviewing
#     what the button may run is reviewing the content file, which is already reviewed.
#
# A module-level register rather than a value threaded through the emitters: the three
# declarations are made by `runtime_html` and `rerun_html`, which are leaves of a render
# tree eight calls deep whose every other node is a pure string function. Passing a
# collector down that tree would put a parameter for the action server on a dozen
# signatures that have nothing to do with it. `main` clears it before a build and writes
# it after, which is the only ordering that matters.
ACTIONS: dict[str, dict] = {}


def declare_action(action_id: str, command: str, *, params: dict[str, str] | None = None,
                   scrape: str = "", reload: bool = False, label: str = "") -> str:
    """Register one runnable command and return the id the page should send.

    `params` maps each `{name}` hole in the command to the shape its value must have
    (`int`, `url`, `word` — serve-review.py owns the patterns). A hole with no declared
    shape is refused at run time rather than interpolated, so a template can never grow a
    parameter here without someone deciding what is allowed to go in it."""
    ACTIONS[action_id] = {"command": command, "params": dict(params or {}),
                          "scrape": scrape, "reload": reload, "label": label}
    return action_id


# The two verbs the *server* owns, declared here anyway — one string, two surfaces.
#
# They used to be the exception: `serve-review.py` built its own argv for `/__rerun__`
# (`python refresh-report.py --dir … --steps static --no-serve`, run with `cwd=ROOT`) and
# the aftermath band printed its own line for the clipboard (`cd <root> && refresh-report.py
# --dir … --steps static`). Two authors, one command, and they had already drifted: the
# line a reader copied was missing `--no-serve` and the interpreter, so pasting it did
# something else than pressing it. There is no way to test that gap away while both halves
# are written twice — so neither is written twice any more.
#
# The build declares them, exactly as a reader would type them: absolute interpreter,
# absolute program, `cd <repo> &&` in front, so the string on the clipboard runs as-is in
# any terminal. The server looks them up here and runs *that string* through `sh -c`. The
# page embeds the same string in `data-cmd`. One string, three places that only read it.
#
# Dunder ids, so they can never collide with a name out of the content file, and the
# endpoints stay separate: `/__rerun__` and `/__rerun_ai__` keep the shared lock, the
# confirmation dialog and the watcher hold that `/__run__` knows nothing about. Being in
# the manifest is about *where the command lives*, not about how it is reached.
RERUN_ACTION = "__rerun__"
RERUN_AI_ACTION = "__rerun_ai__"


def declare_rerun_actions(root: Path, out_dir: Path, skill_dir: Path) -> None:
    """Put the masthead's two reruns in the register, as the lines a reader could paste.

    `--no-serve` is part of both, because it is part of what the server runs: a copy that
    left it out would start a second review server on the reader's machine, which is the
    one difference between the two surfaces nobody would notice until it had happened.

    `--steps static` is the free set of producers (see `refresh-report.STATIC_STEPS`), and
    the free button never passes `--allow-model`: a click cannot buy a privacy verdict.

    Silent when the programs are not beside us — a directory copied out of a run has no
    skill behind it to rebuild it with, and an undeclared action is a page that offers
    nothing rather than a button that fails.
    """
    refresh = skill_dir / "refresh-report.py"
    model = skill_dir / "rerun-model.py"
    if not refresh.is_file():
        return
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return
    here = shlex.quote(str(root.resolve()))
    py = shlex.quote(sys.executable)
    at = shlex.quote(rel)
    declare_action(
        RERUN_ACTION,
        f"cd {here} && {py} {shlex.quote(str(refresh))}"
        f" --dir {at} --steps static --no-serve",
        reload=True, label="Rebuild this page against the branch as it is now")
    if model.is_file():
        declare_action(
            RERUN_AI_ACTION,
            f"cd {here} && {py} {shlex.quote(str(model))} --dir {at}"
            f" && {py} {shlex.quote(str(refresh))}"
            f" --dir {at} --steps static --allow-model --no-serve",
            reload=True, label="Rewrite the matrix with a model, then rebuild this page")


#: The masthead's ↺⏳, and the Tests tab's (as `__rerun_tests__:requirements`): run what
#: takes long, then rebuild. Same id as the tab's, un-narrowed, the way `__rerun__` is the
#: masthead's and `__rerun__:<tab>` a tab's.
RERUN_TESTS_ACTION = "__rerun_tests__"


def slow_steps(skill_dir: Path) -> list[tuple[str, str]]:
    """`[(step, what it produces)]` for every producer a click on the masthead ↺ never
    runs — `run-steps.STEPS` minus `refresh-report.STATIC_STEPS`, in the table's order.
    The suites (`traces`) and everything else that drives a browser, records a film or
    needs the stack up. `[]` when either table cannot be read."""
    try:
        table = _load(skill_dir / "run-steps.py", "hr_run_steps_table_slow").STEPS
        static = set(_load(skill_dir / "refresh-report.py", "hr_refresh_table_slow").STATIC_STEPS)
    except Exception:              # noqa: BLE001 - no table, no button
        return []
    return [(row[0], str(row[2] or "")) for row in table if row[0] not in static]


def declare_rerun_tests_action(root: Path, out_dir: Path, skill_dir: Path) -> dict | None:
    """Declare the masthead's ↺⏳ and return `{"id", "steps", "tip"}` for its button, or
    None where the page cannot offer it.

    `--steps all --force`: every producer, the slow ones included, with the step cache
    bypassed — the point of the press is that the suites *run*, and `run-steps.py` would
    otherwise find `traces` unchanged and hand back the old recordings. Then the build, as
    every rerun ends. Free, like ↺; slow, which is the whole difference and what the tooltip
    leads with. A slow step whose prerequisite is not up (no stack on :4200, no Chrome) is
    skipped by `run-steps.py` with its reason, not failed."""
    refresh = skill_dir / "refresh-report.py"
    if not refresh.is_file():
        return None
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return None
    slow = slow_steps(skill_dir)
    if not slow:
        return None
    here = shlex.quote(str(root.resolve()))
    line = (f"{shlex.quote(sys.executable)} {shlex.quote(str(refresh))}"
            f" --dir {shlex.quote(rel)} --steps all --force --no-serve")
    declare_action(RERUN_TESTS_ACTION, f"cd {here} && {line}", reload=True,
                   label="Re-run the tests and every other slow step, then rebuild this page")
    # The suites first: they are what the button is for and what the wait is mostly made of.
    slow = sorted(slow, key=lambda sw: sw[0] != "traces")
    named = ", ".join(f"{s} ({what})" if what else s for s, what in slow)
    tip = ("Re-run the tests and every other long-running step — " + named
           + " — with nothing reused from the last run, then regenerate the report. "
           "Free, but it takes long (minutes, the test suites most of all), and the suites "
           "need the application stack up.")
    try:
        order = [row[0] for row in
                 _load(skill_dir / "run-steps.py", "hr_run_steps_table_slow").STEPS]
    except Exception:              # noqa: BLE001 - the button stands without the bar's list
        order = [s for s, _ in slow]
    return {"id": RERUN_TESTS_ACTION, "steps": order, "tip": tip}


def tab_rerun_id(base: str, tab: str) -> str:
    """`__rerun__:sequence` — the manifest key of one tab's rerun, for either verb."""
    return f"{base}:{tab}"


def _load(path: Path, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tab_steps(skill_dir: Path) -> dict[str, list[str]]:
    """`{tab id: [steps a press on that tab re-runs]}` — a tab with no producer is absent.

    Read from `run-steps.STEPS` rather than restated here: which producer feeds which tab
    is a fact that table already owns (the cost ledger is keyed on it), and a second copy
    is the one that would rot the day a step moves tabs.

    A tab's static producers when it has any (`refresh-report.STATIC_STEPS`), else its own
    heavy ones. The Tests tab is fed by `tests` *and* `traces`, and `traces` is a project's
    whole e2e suite: the ↻ beside the ledger re-reads the manifest, it does not run
    cucumber at you. But Sequence, Demo, Code City and UX have nothing *but* a heavy
    producer, and a reader who presses ↻ on one of those is asking for exactly that — which
    is the one thing the masthead's button must never do on their behalf."""
    try:
        table = [(row[0], row[1] or "") for row in
                 _load(skill_dir / "run-steps.py", "hr_run_steps_table").STEPS]
        static = set(_load(skill_dir / "refresh-report.py", "hr_refresh_table").STATIC_STEPS)
    except Exception:              # noqa: BLE001 - no table, no per-tab buttons
        return {}
    feeds: dict[str, list[str]] = {}
    for step, tabs in table:
        for tab in filter(None, (t.strip() for t in tabs.split(","))):
            feeds.setdefault(tab, []).append(step)
    return {tab: ([s for s in steps if s in static] or steps) for tab, steps in feeds.items()}


#: The tabs whose producer can also buy something from a model, and what the paid press
#: adds in front of (or inside) the free one. The Tests tab's matrix and catalogue are
#: `rerun-model.py`'s. The Logging tab had one too (privacy verdicts under `--allow-model`)
#: until its scan became deterministic; it keeps only the free ↺.
#:
#: The Review tab's is `rerun-review.py`: a new review of the whole PR, which — unlike every
#: other press on this page — commits and pushes, because a review commit on the branch is
#: the only thing that clears the aftermath band. The price is in the sentence, since the
#: probe's figure is the matrix's and says nothing about this run.
TAB_AI = {
    "requirements": ("model", "Rewrites this tab's requirements↔tests matrix and the "
                              "per-test catalogue with a model, then re-derives the test "
                              "manifest and rebuilds the page."),
    "review": ("review", "About $15–$40 on Opus. Re-runs a /code-review high review over "
                         "the whole PR — every commit since the merge base, re-read against "
                         "the code as it is now — applies the fixes it accepts as [auto-fix] "
                         "commits, writes a fresh review-points.md and PR comments, then "
                         "commits review-points.md with its Review-Points, Implements and "
                         "Claude-Session trailers and pushes all of it to this branch "
                         "(git push origin HEAD:<branch>). That new review commit is what "
                         "clears the red band of commits made since the review. It refuses, "
                         "before spending anything, while anything is staged; the record it "
                         "replaces is kept in .human-review/.model-prev/."),
}

#: Which program a paid press runs in front of the tab's refresh, and whether the refresh
#: then needs `--allow-model`. The matrix's does (it is what the Tests tab's build reads
#: under that flag); the review's does not — its producers read git, and nothing else.
AI_STEPS = {"model": ("rerun-model.py", True), "review": ("rerun-review.py", False)}


#: What a tab's free ↺ does, where "re-derive the tab" would promise more than it does.
#: The Review tab's producers read git — the review commit and what landed after it — and
#: nothing else: the findings and the assumptions are a model's, and only a new review pass
#: moves them. Said on the button, so a reader does not press it hoping for a new review.
TAB_TIPS = {
    "review": "Rebuilds the list of commits made since the review, from git, and rebuilds "
              "the page. Free. It does not re-run the review: the findings and the "
              "assumptions stay as they were written.",
}


def declare_tab_reruns(root: Path, out_dir: Path, skill_dir: Path,
                       tab_ids) -> dict[str, dict]:
    """One free rerun per tab that has a producer, and a paid one where a tab has a model
    half. Returns `{tab: {"steps": [...], "ai": bool, "aiTip": str}}` for the strip.

    Same command shape as the masthead's, narrowed with `--steps <that tab's producers>`:
    `refresh-report.py` still rebuilds the whole page (it is one file), but only this tab's
    evidence is re-derived. Heavy producers are included when they are the tab's own — a
    reader who presses ↻ on Sequence is asking for the sequences, which is exactly what the
    masthead's button must never do on their behalf.
    """
    refresh = skill_dir / "refresh-report.py"
    if not refresh.is_file():
        return {}
    try:
        rel = str(out_dir.resolve().relative_to(root.resolve()))
    except ValueError:
        return {}
    here = shlex.quote(str(root.resolve()))
    py = shlex.quote(sys.executable)
    at = shlex.quote(rel)
    feeds = tab_steps(skill_dir)
    out: dict[str, dict] = {}
    for tab in tab_ids:
        steps = feeds.get(tab)
        if not steps:
            continue
        only = shlex.quote(",".join(steps))
        refresh_line = (f"{py} {shlex.quote(str(refresh))} --dir {at} --steps {only}"
                        " --no-serve")
        declare_action(tab_rerun_id(RERUN_ACTION, tab), f"cd {here} && {refresh_line}",
                       reload=True, label=f"Re-derive the {tab} tab and rebuild this page")
        info = {"steps": steps, "ai": False, "aiTip": "", "tip": TAB_TIPS.get(tab, "")}
        how = TAB_AI.get(tab)
        program, allow = AI_STEPS.get(how[0], (None, True)) if how else (None, True)
        if how and (program is None or (skill_dir / program).is_file()):
            paid = f"{refresh_line} --allow-model" if allow else refresh_line
            if program:
                paid = (f"{py} {shlex.quote(str(skill_dir / program))} --dir {at}"
                        f" && {paid}")
            declare_action(tab_rerun_id(RERUN_AI_ACTION, tab), f"cd {here} && {paid}",
                           reload=True,
                           label=("Re-review the whole PR, commit and push the record, then "
                                  "rebuild this page") if how[0] == "review"
                           else f"Re-derive the {tab} tab with a model")
            info.update(ai=True, aiTip=how[1], priced=how[0] == "model")
        out[tab] = info
    return out


def write_actions(out_dir: Path) -> Path:
    """Drop the manifest beside the page, always — an empty one included.

    Always, because the file is read by mtime and the alternative to rewriting it is
    leaving the previous build's manifest in place: a page that no longer has the button
    next to a server that still offers to run the command behind it. An empty `actions`
    is a perfectly good statement and the one this build means when it renders no
    runnable control."""
    path = out_dir / ACTIONS_FILE
    path.write_text(json.dumps({"version": 1, "actions": ACTIONS}, indent=2) + "\n",
                    encoding="utf-8")
    return path
