"""The register of commands the served page may ask the server to run."""
from __future__ import annotations

import functools
import json
import os
import shlex
import shutil
import subprocess
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


@functools.lru_cache(maxsize=None)
def rebuild_interpreter() -> str:
    """How to say "python, with Pygments" on *this* machine, in a command a reader pastes
    or the server runs — every command in the manifest starts with it.

    Not `sys.executable`. Under `uv run --with pygments` that is a path inside a build
    directory uv deletes on exit, so the one command guaranteed to have worked is also the
    one guaranteed not to work again — and a command that fails when pasted is worse than
    no command, because it is tried first and believed second.

    So: plain `python3` when it can already import Pygments, which is the legible answer
    and the portable one; `uv run --with pygments --with ast-grep-cli --with pyyaml python`
    when it cannot and uv is here, which installs nothing permanently and is ~100ms warm; and only
    then the interpreter running this build, which at least names something real.

    `ast-grep-cli` and `pyyaml` ride along because the Logging tab's scan needs the
    `ast-grep` binary on PATH and the API tab's diff needs YAML, and each step fails
    without them: a press on a served page would rebuild it short of the build that drew it.
    """
    # The probe has to ask what `python3` means *in the reader's terminal*, not in this
    # process. Under `uv run` the front of PATH is two throwaway directories — the
    # interpreter's own, and the one holding the packages `--with` installed — and both
    # answer to `python3` with Pygments importable, so a naive probe cheerfully reports
    # that `python3` works about interpreters that are deleted on exit. Dropping every
    # ephemeral directory is what asks the right question; if nothing outside them answers
    # to `python3`, that is an answer too, and the uv line below is the honest one.
    env = dict(os.environ)
    throwaway = [str(Path(sys.prefix)), env.get("VIRTUAL_ENV") or "",
                 os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")]
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", "").split(os.pathsep)
        if d and not any(t and (d == t or d.startswith(t + os.sep)) for t in throwaway))
    env.pop("VIRTUAL_ENV", None)
    outside = shutil.which("python3", path=env["PATH"])
    if outside and subprocess.run([outside, "-c", "import pygments"],
                                  capture_output=True, env=env).returncode == 0:
        return "python3"
    if shutil.which("uv", path=env["PATH"]) or shutil.which("uv"):
        return "uv run --with pygments --with ast-grep-cli --with pyyaml python"
    return shlex.quote(sys.executable)


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
    py = rebuild_interpreter()
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
    line = (f"{rebuild_interpreter()} {shlex.quote(str(refresh))}"
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
#: Each entry is `(kind, tooltip, confirmation)`. The tooltip is read in passing and says
#: only the verb (and, where the probe cannot quote one, the price); the confirmation is
#: read at the moment of spending, and carries what the press will do to the branch.
TAB_AI = {
    "requirements": ("model", "AI redoes the requirements↔tests map.",
                     "AI redoes the requirements↔tests map. The current map is replaced."),
    "review": ("review", "About $15–$40 on Opus. New AI review of the whole PR; commits "
                         "and pushes its fixes.",
               "About $15–$40 on Opus. A new /code-review high pass over the whole PR "
               "applies the fixes it accepts as [auto-fix] commits, rewrites "
               "review-points.md and pushes — which clears the red band. It refuses while "
               "anything is staged; the old record goes to .human-review/.model-prev/."),
    # The Demo tab's: the film's script is the second model-written artifact, owned like
    # the matrix — `rerun-film.py` rewrites it, then the tab's own refresh (`--steps video`)
    # records the film from it.
    "behaviour": ("film", "AI rewrites the film script, then re-records it (minutes, needs "
                          "the app).",
                  "AI rewrites the film script (.human-review/feature-script.js) from the "
                  "diff, then re-records the film — minutes, needs the app. The old script "
                  "goes to .human-review/.model-prev/."),
}

#: Which program a paid press runs in front of the tab's refresh, and whether the refresh
#: then needs `--allow-model`. The matrix's does (it is what the Tests tab's build reads
#: under that flag); the review's does not — its producers read git, and nothing else; nor
#: does the film's, whose refresh is the recorder.
AI_STEPS = {"model": ("rerun-model.py", True), "review": ("rerun-review.py", False),
            "film": ("rerun-film.py", False)}

#: The paid presses whose price the probe can quote, each out of its own program's ledger
#: (`serve-review.price_estimate`). The re-review is not one: its price is in its sentence.
PRICED = ("model", "film")


#: What a tab's free ↺ does, where "re-derive the tab" would promise more than it does.
#: The Review tab's producers read git — the review commit and what landed after it — and
#: nothing else: the findings and the assumptions are a model's, and only a new review pass
#: moves them. Said on the button, so a reader does not press it hoping for a new review.
TAB_TIPS = {
    "review": "Free. Refreshes the commit list; findings unchanged.",
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
    py = rebuild_interpreter()
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
                           else ("Rewrite the film's script with a model, then film it "
                                 "again") if how[0] == "film"
                           else f"Re-derive the {tab} tab with a model")
            info.update(ai=True, aiTip=how[1], aiConfirm=how[2], priced=how[0] in PRICED,
                        price=how[0])
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
