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
