"""The register of commands the served page may ask the server to run."""
from __future__ import annotations

import json
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
