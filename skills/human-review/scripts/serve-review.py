#!/usr/bin/env python3
"""Serve .human-review/ on loopback and print the report's URL.

The guide is finished work that someone reads; `open review.html` hands it to
whatever the OS thinks owns .html — another application, on whatever desktop it
happens to live on, while the terminal that built it is inside an editor. Both of
VS Code's embedded browsers can show it instead, but neither will load a
`file://` URL: the Simple Browser's iframe is bound by a `frame-src *` CSP, and a
CSP wildcard does not cover non-network schemes, so a file URL renders as a blank
panel with no error. Serving the directory is what makes the report addressable.

The server is deliberately small and mortal:

- **loopback only.** The guide quotes source, names people from CODEOWNERS, and
  deep-links a working tree. It is not for the network.
- **it exits on its own.** `.human-review/` is a throwaway, and a static server
  left running until the next reboot is a worse artifact than the folder it
  serves. Idle for `--idle-minutes` and it is gone.
- **a second run reuses the first.** Re-rendering the report and re-serving it is
  the normal loop; each run leaving another listener behind is not.
- **the open page follows the build.** Re-rendering is the normal loop, and the
  page a reader is looking at is the *previous* render until somebody presses F5
  — which, for the twenty seconds before they do, is a report that quietly
  disagrees with the disk. The server watches the directory it serves and the
  page reloads itself once the writing has stopped. `--no-watch` turns it off.

It is also, since the action server, the thing that makes three of the page's buttons
*do* what they otherwise only describe. `/__run__` runs a command — but never a command
the page sent. The page sends an **id**; the command behind it was written into
`.human-review/.actions.json` by the build, out of the same content file the button was
rendered from. So the allowlist is not a list somebody has to remember to maintain: it is
the build's own record of what this page is allowed to ask for, and a page from another
build, a page out of the zip, or a page on GitHub Pages can ask for nothing at all.

`/__rerun__` is the one exception to that indirection, and it proves the rule rather than
bending it. It takes no id, because the command is not the page's to name: it is
`refresh-report.py` with the producers that need nothing up, which is what the **Rerun** in
the masthead asks for. A page read off disk has no button there at all — the probe below
says whether this server can honour one, and the button rises only when it does.

`/__rerun_ai__` is the same exception with the model's half in front of it — `rerun-model.py`
(`claude -p --model sonnet` over the matrix prompt), then the same refresh with
`--allow-model` — and it is a **second endpoint rather than a flag** on the first, because
the two are not the same offer. One is free and reproducible; the other buys a judgement at
several dollars, and a page that forgot to send a boolean would have bought it. They share
one lock: two refreshes over one directory collide whichever button started them.

That mortality is not in tension with running commands, because every action here is a
batch: `start-docker.sh up` exits once the stack answers, the drawio rerun exits once the
picture is redrawn. Nothing here supervises a daemon — the environment reaps itself.

Usage:
  serve-review.py .human-review                      # prints the base URL
  serve-review.py .human-review --page review.html   # prints the page URL
  serve-review.py .human-review --stop
  serve-review.py .human-review --no-watch           # no live reload
"""
import argparse, collections, functools, hashlib, http.server, json, os, re, secrets, shlex, shutil, socket, socketserver, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

MARKER = "/__human_review__"
OPEN = "/__open__"
OPEN_DIFF = "/__open_diff__"
RUN = "/__run__"
RUN_STATUS = "/__run_status__"
RERUN = "/__rerun__"
# The same verb with the model's half in front of it. A second endpoint rather than a flag
# on the first, because the two are not the same offer: one is free and reproducible and
# the other buys a judgement at several dollars, and a boolean in a request body is the
# wrong place for that difference to live — a page that forgot to send it would have bought
# something. They share the lock and nothing else.
RERUN_AI = "/__rerun_ai__"
WATCH = "/__watch__"

# The page asks "is there a review server here?" and a *wrong* yes is expensive: the demo
# published on GitHub Pages is https, so the protocol check this replaced said yes, and
# four hundred and seventy-seven editor handles fetched github.io, took a 404, and toasted
# "Could not open X.java" at a reader who had done nothing wrong. So the answer has to be
# something only this server can say. GitHub's 404 page is HTML and does not parse; a
# JSON body that parses but lacks this key is somebody else's server on :7654.
MARKER_KEY = "humanReview"

# Written by build-review-html.py next to review.html. Dot-prefixed to sit on the right
# side of a split this repository already makes: `publish-demo.sh` copies everything in a
# run directory that does *not* start with a dot, and the zip offered at the foot of the
# page is built from that copy. So the published demo and the download carry no manifest,
# which is not a tidiness preference — it is the property that makes "a page out of the
# zip can ask for nothing" true by construction rather than by remembering to exclude it.
ACTIONS_FILE = ".actions.json"

# The refresh program, beside us in the skill. The header's Rerun is the one command this
# server runs that the build did NOT declare, and deliberately so: the manifest is the
# build's record of what *this page* may ask for, while "rebuild yourself from the
# repository as it is now" is a property of being served at all. A page out of the zip
# cannot ask for it because there is nobody there to ask.
REFRESH = Path(__file__).resolve().parent / "refresh-report.py"

# The model half of the skill, spelt as a command — `claude -p --model sonnet` over the
# matrix prompt. Beside us for the same reason `refresh-report.py` is: a directory somebody
# copied out of a run has no skill behind it, and the honest answer there is no button.
MODEL_STEP = Path(__file__).resolve().parent / "rerun-model.py"

# The id that rerun's Run wears in RUNS, so a second click can find the first one. Dunder
# so it can never collide with an action name out of a manifest.
RERUN_ACTION = "__rerun__"
RERUN_AI_ACTION = "__rerun_ai__"

# Both reruns, for the one question every guard here asks: "is a rerun already going?" One
# tuple rather than two comparisons, because the lock is shared and a second membership
# test added in one of the three places that ask would be a lock with a hole in it.
RERUN_ACTIONS = (RERUN_ACTION, RERUN_AI_ACTION)

# What the paid reruns on this page have actually cost, written by `rerun-model.py` after
# each run. Beside the page and dot-prefixed like the manifest, so it never travels in the
# zip: it is a record of this machine's spending, not a fact about the branch.
MODEL_RUNS_FILE = ".model-runs.json"

# What the button says when the ledger is empty. A range and not a number, because a number
# with nothing behind it is a promise — and the one that was there, "~$5 on Sonnet", was
# wrong in the direction that matters: three real runs on this page came in at $4.00, $8.09
# and $10.63, so a reader who budgeted for the label was out by a factor of two.
PRICE_UNKNOWN = "~$5\u2013$10"
PRICE_SAMPLE = 5


def price_estimate(served_root) -> dict:
    """`{"text", "last", "n"}` — what to tell a reader before they spend.

    Derived from what this page's own paid runs cost, not from a constant somebody typed
    once. The average of the last few, because the spread is real: the same branch costs
    more on a day the matrix has more tests to map, and a single last-run figure would
    swing the label by a factor of two between two presses.

    `last` rides along separately and goes in the confirmation dialog rather than in the
    hover. An average is what a reader budgets with; the last real invoice is what makes
    them believe the average, and it belongs at the point of deciding rather than on a
    tooltip they may never open.

    No ledger, an unreadable one, or one with no costs in it yields the range and `n: 0`.
    A ledger that cannot be read is not an error here: it is a page nobody has spent money
    on yet, which is also what it looks like from outside.
    """
    try:
        doc = json.loads((Path(served_root) / MODEL_RUNS_FILE).read_text(encoding="utf-8"))
        runs = doc["runs"] if isinstance(doc, dict) else doc
        costs = [float(r["cost"]) for r in runs
                 if isinstance(r, dict) and isinstance(r.get("cost"), (int, float))
                 and float(r["cost"]) > 0]
    except Exception:
        costs = []
    if not costs:
        return {"text": PRICE_UNKNOWN, "last": None, "n": 0}
    recent = costs[-PRICE_SAMPLE:]
    return {"text": f"~${sum(recent) / len(recent):.2f}", "last": round(costs[-1], 2),
            "n": len(recent)}

# A ref, and nothing that could be a flag or a second argument. `git show` is invoked
# without a shell, so this is not about quoting — it is about `--upload-pack=…` and
# friends arriving from a query string. Hence the first character: a ref may not open with
# `-`, which is the whole of the attack this guards.
#
# It used to be `[0-9a-fA-F]{7,40}` — a raw sha and nothing else. But the base every
# snippet on the page is written against is `origin/main` (HUMAN_REVIEW_DIFF_BASE), so
# every editor handle in the report hit this and came back "Not a usable git ref": the
# page's most-clicked button, refused by the one check that was supposed to let it
# through. A name is not less safe than a sha here — it is resolved through git before it
# reaches anything, and a name git cannot resolve is refused exactly as before.
REF_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/+^~{}-]{0,100}$")


def resolve_ref(base):
    """`origin/main`, `HEAD^`, a sha — to the commit it names, or None.

    Asked of git rather than pattern-matched, because "does this ref exist in this
    checkout?" is not a question a regex can answer, and the answer is what the rest of
    the diff is built from: the resolved sha names the before-image on disk, so a name
    that moved between two clicks cannot leave two different files wearing one label."""
    if ROOT is None or not REF_RE.match(base or ""):
        return None
    out = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True, text=True)
    return out.stdout.strip() or None

# Everything the server will open must live under here — the repository the guide is
# about. A loopback endpoint that opens any path in the editor is a wider door than this
# needs, and the guide only ever references its own working tree.
ROOT = None


def git_root(start):
    try:
        out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        return Path(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def owning_windows(target: Path):
    """The VS Code windows whose workspace folders contain `target`, best claim first.

    Each window's extension host publishes {port, token} under ~/.walkie-talkie/ide/ and
    answers /ping with the absolute paths of its workspace folders. We pick by longest
    matching prefix, so a window opened on the checkout beats one opened on the directory
    above it — the deeper folder is the more specific claim on the path.

    Selecting by *name* (what this used to do) is the bug this replaces. A name is not an
    address: two checkouts of the same project are both called `petclinic`, and handing a
    file to the wrong one opens the right absolute path inside a window belonging to
    another tree — where the `path:line` reference beside it, pasted into Quick Open,
    resolves to the same relative path with different content. Nothing errors; the reader
    just reads the wrong file.

    A registry file is a claim, not a fact — a window that crashed never got to delete its
    own. So an entry is trusted only once the process it names answers on its port with
    our token. An entry whose port is *refused* is deleted, that being proof the window is
    gone; a timeout proves nothing and deletes nothing, because unplugging a live window
    from the bridge would cost it until its next activation."""
    ranked = []
    for f in sorted((Path.home() / ".walkie-talkie" / "ide").glob("vscode-*.json")):
        try:
            entry = json.loads(f.read_text())
            ping = urllib.request.Request(
                f"http://127.0.0.1:{entry['port']}/ping", headers={"x-relay-token": entry["token"]})
            info = json.load(urllib.request.urlopen(ping, timeout=2))
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                f.unlink(missing_ok=True)
            continue
        except Exception:
            continue
        if not info.get("ok") or info.get("app") != "vscode":
            continue
        # Older builds of the bridge only publish the first folder's *name*. Falling back
        # to it keeps them working — worse routing, not none — while a current build wins
        # on the prefix score below.
        best = 0
        for folder in info.get("folders") or []:
            for spelling in (folder.get("path"), folder.get("realPath")):
                # A checkout reached through a symlink is one tree under two names, and
                # which one we hold is an accident of how the path was computed.
                if not spelling:
                    continue
                root = Path(spelling)
                if target == root or root in target.parents:
                    best = max(best, len(str(root)))
        if not best and not info.get("folders") and ROOT and info.get("folder") == ROOT.name:
            best = 1
        if best:
            ranked.append((best, entry, info))
    ranked.sort(key=lambda t: -t[0])
    return [(e, i) for _, e, i in ranked]


def open_in_editor(path, line):
    """Land the reader in the class. Through the VS Code window that has this file's
    folder open where the bridge is installed, and through the OS otherwise.

    The page cannot do this itself: its references are `vscode://file/...` links, and the
    embedded browser's iframe is sandboxed under a `frame-src *` CSP, so a webview cannot
    hand a custom scheme to the OS — the click does nothing whatever the anchor says. It
    *can* fetch its own origin, which is how the request gets here."""
    target = Path(path)
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    for candidate in (target, resolved):
        for entry, info in owning_windows(candidate):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{entry['port']}/open-file", method="POST",
                    # `focus`: showTextDocument moves the caret inside that window but
                    # leaves the window itself behind the browser the click came from.
                    # Measured — the file opened and the frontmost app never changed, so
                    # the click read as a no-op and the file waited to be found by
                    # accident. The bridge raises the window natively.
                    data=json.dumps({"path": str(path), "line": line, "focus": True}).encode(),
                    headers={"x-relay-token": entry["token"], "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).read()
                return "relay"
            except Exception:
                continue
        if resolved == target:
            break
    # Nobody owns it, or the owner would not take it: hand it to the OS. VS Code 1.135
    # routes `vscode://file/<abs>` to the window whose workspace contains the path and
    # raises it — measured, across three windows on three checkouts of one project — so
    # this is a good fallback, not a bad one. It degrades to the last-active window only
    # when no window owns the path at all, which is also the one case nothing better
    # exists.
    subprocess.run(["open", f"vscode://file/{path}:{line}:1"], capture_output=True)
    return "os"


def bridge_diff(target: Path, sha: str, line: int) -> bool:
    """Ask the VS Code window that owns `target` to open the diff *and* put the caret on
    `line`. True when one took it.

    `code --diff` cannot do the second half: the CLI has no way to say where in a diff to
    land, so a fix five hundred lines down opens scrolled to the top and the reader hunts
    for it — which is the hunt the handle they clicked exists to end. The extension bridge
    can, because it opens the diff from inside the editor and then reveals the range.

    The ref goes over as the resolved sha, not the name that was clicked: the bridge takes
    a sha and this side has already resolved one, so the two ends cannot disagree about
    what `origin/main` meant between one click and the next."""
    for candidate in {target, target.resolve()}:
        for entry, _info in owning_windows(candidate):
            try:
                payload = json.dumps({"file": str(target), "base": sha,
                                      "line": line, "focus": True}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{entry['port']}/open-diff", method="POST",
                    data=payload,
                    headers={"x-relay-token": entry["token"],
                             "Content-Type": "application/json"})
                body = json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")
                if body.get("ok"):
                    return True
            except Exception:
                continue
    return False


def code_cli():
    """The `code` launcher, which is not on PATH in a GUI-launched terminal on macOS."""
    found = shutil.which("code")
    if found:
        return found
    mac = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
    return str(mac) if mac.is_file() else None


def open_diff(rel, base, served_root, line=None):
    """Open `rel` as a diff — the file at `base` on the left, the working tree on the right,
    scrolled to `line` where anything on this machine can do that.

    Returns None on success, or the sentence to show the reader.

    The before-side is written out of git rather than reconstructed: `git show <ref>:<path>`
    or nothing. If the ref does not resolve, or the file did not exist in it, or the two
    sides are identical, this refuses — a diff with an invented left half would be worse
    than no diff, because it would look exactly like evidence.

    It lands beside the report, *inside the repository*, and that is deliberate. VS Code
    picks the window for a diff from the paths it is given; with the before-image in
    /tmp only the right-hand file carries a workspace, while under the served directory
    both sides do, so the window that owns this checkout wins outright. Named
    `<stem>@<short><ext>` rather than `<name>@<short>` so the extension survives and the
    left pane keeps its syntax highlighting — and so the editor tab reads
    `packages@cb0988f5.puml ↔ packages.puml`, which says what is being compared."""
    if ROOT is None:
        return "This server is not attached to a repository"
    sha = resolve_ref(base)
    if sha is None:
        return f"{base or 'that ref'} is not a ref this checkout knows"
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        return "That file is outside the repository"
    if not target.is_file():
        return f"{Path(rel).name} is no longer in the working tree"
    show = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:{rel}"],
                          capture_output=True)
    short = sha[:8]
    if show.returncode != 0:
        return f"{Path(rel).name} does not exist at {short}"
    if show.stdout == target.read_bytes():
        return f"{Path(rel).name} is unchanged since {short}"
    stem, ext = Path(rel).stem, Path(rel).suffix
    before = Path(served_root) / ".diffbase" / short / Path(rel).parent / f"{stem}@{short}{ext}"
    before.parent.mkdir(parents=True, exist_ok=True)
    before.write_bytes(show.stdout)
    # The bridge first, and only when there is a line to land on — it is the one route
    # that can place the caret inside a diff. Every other case keeps `code --diff`, which
    # is measured and known to pick the right window; swapping it out for a route with
    # nothing extra to offer would be churn.
    if line and bridge_diff(target, sha, line):
        return None
    cli = code_cli()
    if not cli:
        # No VS Code launcher: fall back to what every other reference on the page does
        # rather than leaving the click silent. The reader loses the diff, not the file.
        subprocess.run(["open", f"vscode://file/{target}:{line or 1}:1"], capture_output=True)
        return None
    # Measured across four windows on three checkouts: with a *different* window raised
    # first, `--diff` still opened in the one owning this checkout and brought it to the
    # front. So this needs no window-routing of its own — unlike /open-file, whose single
    # path leaves VS Code guessing.
    subprocess.run([cli, "--diff", str(before), str(target)], capture_output=True)
    return None


def probe(port):
    """What is on this port — our server for which directory, or something else."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{MARKER}", timeout=1) as r:
            return json.load(r)
    except Exception:
        return None


def free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- #
# watching the served tree
# --------------------------------------------------------------------------- #

# Directories whose contents say nothing about the report. `.git` is the expensive one:
# a review served out of a working tree would otherwise be re-hashed on every index
# write, and every `git status` the reader runs in the terminal beside the page would
# read as "the report changed".
WATCH_SKIP = {".git", "__pycache__", "node_modules", ".pytest_cache"}

# A ceiling rather than a promise. The served directory is a report, not a source tree,
# and the biggest one so far is a few hundred files; a walk that finds twenty thousand
# is being pointed at something this was never meant to watch, and stopping is better
# than spending a third of a second of every second on it.
WATCH_MAX_FILES = 20000

WATCHER = None


def fingerprint(root: Path) -> str:
    """One short string for "the tree, as it is right now".

    Name, size and mtime — not content. Hashing the bytes of a directory that holds a
    12MB screencast is a different order of cost, and the thing being detected is a
    build rewriting files, which cannot do so without moving an mtime."""
    h = hashlib.blake2b(digest_size=12)
    seen = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in WATCH_SKIP)
        for name in sorted(files):
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                # Caught mid-rebuild: a file that vanished between the listing and the
                # stat is itself a change, and the next tick will see the tree settled.
                h.update(b"\0gone\0")
                continue
            h.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0".encode())
            seen += 1
            if seen >= WATCH_MAX_FILES:
                return h.hexdigest()
    return h.hexdigest()


class Watcher:
    """The published stamp changes once the tree has *finished* changing.

    The page reloads itself when this string moves, so the quiet period is not a
    nicety: `build-review-html.py` writes the html, then the diagrams, then the
    manifest, over seconds. A watcher that published the first write would reload the
    reader into a half-built report and then leave them there — the second write is not
    a change *the page is still around to see*. So a fingerprint has to hold still for
    `quiet` seconds before it is handed out.

    The first fingerprint is published immediately, on purpose: it is the baseline the
    page is served against, and a stamp that arrived empty and filled in half a second
    later would spend that half-second looking like a change.

    Quiet is not enough while *we* are the ones rebuilding. `refresh-report.py` runs eight
    producers and a build: between two of them the tree holds still for whole seconds
    while a program computes, and six tenths of a second of stillness is all this asks for
    — so a rerun would reload the reader's tab several times on its way through, each time
    into a directory one step into being rewritten, and each time out from under the
    button they pressed. `hold()` is how the rerun says "I am not finished"; the stamp is
    published at the first quiet tick after it is released, which is the single reload the
    reader wanted. Held per-caller, counted, because nothing here is allowed to assume
    there is only ever one."""

    def __init__(self, root, quiet=0.6):
        self.root = Path(root)
        self.quiet = quiet
        self._lock = threading.Lock()
        self._stamp = self._seen = fingerprint(self.root)
        self._since = time.time()
        self._holds = 0

    @property
    def stamp(self) -> str:
        with self._lock:
            return self._stamp

    def hold(self) -> None:
        with self._lock:
            self._holds += 1

    def release(self) -> None:
        # Never below zero: a release that outnumbers its hold must not leave the watcher
        # permanently unable to publish, which is what a negative counter would do.
        with self._lock:
            self._holds = max(0, self._holds - 1)

    def tick(self, now=None) -> str:
        now = time.time() if now is None else now
        found = fingerprint(self.root)
        with self._lock:
            if found != self._seen:
                self._seen, self._since = found, now
            elif (found != self._stamp and not self._holds
                  and now - self._since >= self.quiet):
                self._stamp = found
            return self._stamp

    def run(self, interval=0.4):
        while True:
            try:
                self.tick()
            except Exception:
                # A watcher is a convenience; a watcher that can take the server down
                # with it is not. Whatever went wrong, the next tick tries again.
                pass
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# the action registry
# --------------------------------------------------------------------------- #

# What a declared parameter is allowed to be. The command itself never comes from the
# page, so this is not the main gate — the manifest is — but a template like
# `drive-to-cue.js --app {base} --cue {n}` still has two holes in it, and a hole a page
# can fill is a hole an attacker can fill if a page is ever tricked into filling it.
#
# Two locks, deliberately: the value has to match the shape its parameter was declared
# with, *and* it is shell-quoted on the way in. Either alone would do on a good day. The
# pair is what makes a mistake in one of them a bug rather than a shell.
PARAM_SHAPES = {
    "int": re.compile(r"^[0-9]{1,9}$"),
    # A loopback base URL and nothing else. The environments this drives are started on
    # this machine by the command in the bar above it; a `base` pointing anywhere else is
    # not a case that exists, so it is not a case that is allowed.
    "url": re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?"
                      r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?$"),
    "word": re.compile(r"^[A-Za-z0-9._/-]{1,200}$"),
}

_manifest = {"mtime": None, "actions": {}}


def actions(served_root) -> dict:
    """The declared actions, re-read whenever the file underneath has moved.

    Not cached at startup, because one of the actions *is* the rebuild: the drawio rerun
    re-runs the generator, which rewrites review.html and rewrites this manifest beside
    it. A registry read once would answer for the build before the one the reader is
    looking at — which is the exact shape of bug this whole indirection exists to kill.

    A manifest that is missing, unparseable, or not shaped like a manifest yields no
    actions at all rather than an error: the page degrades to clipboard-and-toast, which
    is what it does everywhere this server is not, and that path is the tested one."""
    path = Path(served_root) / ACTIONS_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _manifest.update(mtime=None, actions={})
        return {}
    if _manifest["mtime"] == mtime:
        return _manifest["actions"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        declared = doc["actions"]
        if not isinstance(declared, dict):
            raise TypeError
    except Exception:
        declared = {}
    good = {}
    for name, entry in declared.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue
        params = entry.get("params") or {}
        if not isinstance(params, dict) or any(p not in PARAM_SHAPES for p in params.values()):
            continue
        good[name] = entry
    _manifest.update(mtime=mtime, actions=good)
    return good


def resolve_command(entry: dict, params: dict):
    """`(argv, problem)` — the shell line to run, or the sentence to refuse with.

    Every `{name}` in the declared command must be a parameter the manifest declared, and
    every parameter the manifest declared must arrive. Both halves matter: an undeclared
    hole left unfilled would reach the shell as a literal `{base}`, and a value for a hole
    that does not exist is a caller working from a different manifest than this one."""
    shapes = entry.get("params") or {}
    command = entry["command"]
    for name, value in params.items():
        if name not in shapes:
            return None, f"{name} is not a parameter of this action"
        if not isinstance(value, str) or not PARAM_SHAPES[shapes[name]].match(value):
            return None, f"{name} is not a valid {shapes[name]}"
    for name in shapes:
        if name not in params:
            return None, f"this action needs {name}"
        command = command.replace("{" + name + "}", shlex.quote(params[name]))
    left = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", command)
    if left:
        return None, f"{left.group(0)} was never declared as a parameter"
    # `sh -c` rather than an argv: the declared commands are shell lines by nature —
    # `cd <repo> && ./start-docker.sh up`, or the rerun's three-stage `&&` chain — and
    # that shape is the one the page has always shown the reader to paste. Splitting it
    # would mean the button ran something other than what the box beside it says.
    return ["/bin/sh", "-c", command], None


# The last loopback URL a command printed. `start-docker.sh up` ends by printing the port
# it got, and that port is the one thing the page could not know at build time — the whole
# reason there is a box asking the reader to paste it. Scraping it is what turns two
# paste-and-switch-window round trips into one click.
URL_IN_OUTPUT = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?(?:/\S*)?")


class Run:
    """One command, running, with its tail readable while it runs.

    Readable *while it runs* is the whole design. `./start-docker.sh up` is a docker build:
    minutes, during which it prints layer after layer. A button that blocks until that is
    over is indistinguishable from a button that is broken — the reader clicks it again,
    then gives up and goes to the terminal they were told they would not need. So the POST
    returns a handle immediately and the page polls a tail.

    Only the last `KEEP` lines are held. A docker build prints thousands and the page
    shows six; keeping all of them would be a memory leak with a status bar in front of
    it. The URL scrape runs over every line as it arrives, so it does not depend on the
    interesting line still being inside the window at the end."""

    KEEP = 200
    DEFAULT_TIMEOUT = 40 * 60

    def __init__(self, action_id, entry, argv, cwd, on_done=None):
        self.id = secrets.token_urlsafe(9)
        self.action = action_id
        self.state = "running"
        # When it started, because "is something running" is not the question a reader
        # arrives with — "is the thing running *mine*" is, and the answer is a clock.
        self.started = time.time()
        # How many presses have been handed this run instead of one of their own. Reported
        # rather than counted for its own sake: three joins on one run is three readers who
        # each think they started it.
        self.joins = 0
        self.exit = None
        self.result = {}
        self.reload = bool(entry.get("reload"))
        self._scrape = entry.get("scrape")
        self._on_done = on_done
        try:
            # A ceiling, not a schedule. Nothing here is meant to run for forty minutes;
            # what it stops is a command that wedged on a prompt nobody can answer,
            # holding the reaper open behind it for the rest of the day.
            self._timeout = float(entry.get("timeout") or self.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            self._timeout = float(self.DEFAULT_TIMEOUT)
        self._lines = collections.deque(maxlen=self.KEEP)
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
            # Its own process group, so the timeout can take the whole tree down. A
            # `sh -c 'cd x && ./y'` killed by pid leaves ./y running and unowned.
            start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        watchdog = threading.Timer(self._timeout, self._kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in self._proc.stdout:
                line = line.rstrip("\n")
                with self._lock:
                    self._lines.append(line)
                    if self._scrape == "url":
                        found = URL_IN_OUTPUT.findall(line)
                        if found:
                            # The last one on the line, and the last line that has one:
                            # `up` prints the URL once at the end, and prints it again to
                            # say it copied it to the clipboard.
                            self.result["base"] = found[-1].rstrip(".,)")
        finally:
            watchdog.cancel()
            code = self._proc.wait()
            with self._lock:
                self.exit = code
                # Not unconditional: the watchdog may already have called this failed, and
                # a process killed by a signal reports a plausible-looking code.
                if self.state == "running":
                    self.state = "done" if code == 0 else "failed"
            # After the state is final and outside the lock: whatever this releases (the
            # watcher's hold, for a rerun) must be released on every path out, including
            # the one where the command died, and must not be able to deadlock doing it.
            if self._on_done:
                try:
                    self._on_done(self)
                except Exception:
                    pass

    def _kill(self):
        with self._lock:
            if self.exit is not None:
                return
            self.state = "failed"
            self._lines.append(f"[serve-review] giving up after {self._timeout / 60:.0f} minutes")
        try:
            os.killpg(os.getpgid(self._proc.pid), 15)
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return {"run": self.id, "action": self.action, "state": self.state,
                    "exit": self.exit, "output": "\n".join(self._lines),
                    "result": dict(self.result), "reload": self.reload,
                    "started": self.started}


# Keyed by run id, and kept after the process exits: the page polls for the final state,
# and a run that vanished the instant it finished would be indistinguishable from a run
# id the server never issued. Bounded, because this process is meant to be forgettable.
RUNS: "collections.OrderedDict[str, Run]" = collections.OrderedDict()
RUNS_LOCK = threading.Lock()
RUNS_KEEP = 40


def start_run(action_id, params, served_root):
    """`(Run, problem, status)`. The id is looked up; the command never comes from the caller."""
    if action_id in RERUN_ACTIONS:
        # They are in the manifest now, because that is where their command lives — but
        # they are not reachable here. `/__rerun__` and `/__rerun_ai__` carry the shared
        # lock, the watcher hold and, for the paid one, the confirmation in front of it;
        # a second door onto the same command through `/__run__` would have none of those,
        # and the one it would skip first is the $5.
        return None, f"{action_id} has an endpoint of its own; ask for it there", 400
    entry = actions(served_root).get(action_id)
    if entry is None:
        # Deliberately the same answer for "no manifest", "no such action" and "this build
        # stopped declaring it": all three mean the page is asking for something this
        # server is not offering, and distinguishing them only helps someone enumerating.
        return None, f"{action_id} is not an action this review declares", 404
    with RUNS_LOCK:
        for run in reversed(RUNS.values()):
            if run.action == action_id and run.state == "running":
                # A second click while the docker build is still going is not a second
                # build — it is a reader who could not tell the first one had started.
                # Hand back the run already in flight and let the page show its tail.
                return run, None, 200
    argv, problem = resolve_command(entry, params)
    if problem:
        return None, problem, 400
    cwd = entry.get("cwd") or (ROOT if ROOT else Path(served_root))
    if not Path(cwd).is_dir():
        return None, f"{cwd} is not a directory on this machine", 500
    return remember(Run(action_id, entry, argv, cwd)), None, 200


def remember(run: Run) -> Run:
    with RUNS_LOCK:
        RUNS[run.id] = run
        while len(RUNS) > RUNS_KEEP:
            _, old = RUNS.popitem(last=False)
            if old.state == "running":       # never evict one still going
                RUNS[old.id] = old
                break
    return run


def rerun_plan(served_root):
    """`(argv, cwd)` for the header's Rerun, or None when this page cannot have one.

    Both conditions are about honesty rather than safety — the command is ours, not the
    page's:

    - **`refresh-report.py` has to be beside us.** A directory somebody copied out of a
      run and served by hand has no skill behind it to rebuild it with.
    - **the review directory has to sit inside the repository.** `run-steps.py` writes to
      a *relative* `.human-review/assets` and reads `human-review.json` out of the working
      directory, so the producers only land where the page is reading from when the rerun
      is launched from the repository root. Anywhere else it would rebuild the page next
      to evidence it never refreshed — the one outcome nobody can tell from success.

    `--steps static` and not `cheap`: the producers that need nothing up (see
    `refresh-report.STATIC_STEPS`). Nothing turns the model's half on — `refresh-report.py`
    builds `--no-model` unless asked for `--allow-model`, and this never asks — so a click
    cannot buy a privacy verdict, and it refuses outright to build a page whose findings,
    requirements matrix or test catalogue are missing.
    """
    if ROOT is None or not REFRESH.is_file():
        return None
    try:
        rel = Path(served_root).resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    # The command itself comes out of the manifest the build wrote, not from here. It used
    # to be assembled in this function *and* printed, differently, by the page — the band
    # in the Review tab put `cd <repo> && refresh-report.py --dir … --steps static` on the
    # clipboard while this ran `python refresh-report.py --dir … --steps static --no-serve`.
    # Same intent, two authors, and they had already drifted by an interpreter and a flag.
    # One string now: the build declares it, the page copies it, this runs it.
    entry = actions(served_root).get(RERUN_ACTION)
    if entry:
        return (["/bin/sh", "-c", entry["command"]], ROOT)
    # A page from a build older than the declaration still reruns. Kept because the
    # alternative is a masthead button that goes away when the reader upgrades the skill
    # and has not yet rebuilt the page the upgrade is for.
    return ([sys.executable, str(REFRESH), "--dir", str(rel),
             "--steps", "static", "--no-serve"], ROOT)


def rerun_ai_command(rel) -> str:
    """The shell line behind **Rerun + AI**, as one string.

    One line and not two argv lists because it is two programs in a fixed order and the
    order is the whole claim: the model writes the matrix and the catalogue, *then* the
    build turns them into the page. Reversed, the click would rebuild the page from the
    matrix it is about to replace and the reader would be looking at the old one under a
    green tick.

    `&&` and not `;`: a model step that failed must not be followed by a build that hides
    it. `refresh-report.py` refuses to build without the pair anyway, but "refuses" and
    "was never asked" are different lines in the log a reader is about to read.

    Exposed as a function because the page prints this command beside the button (the
    parenthesised command every action on the page now carries), and a page printing a
    different line from the one the button runs is the only failure mode that control has.
    """
    return (f"{shlex.quote(sys.executable)} {shlex.quote(str(MODEL_STEP))}"
            f" --dir {shlex.quote(str(rel))}"
            f" && {shlex.quote(sys.executable)} {shlex.quote(str(REFRESH))}"
            f" --dir {shlex.quote(str(rel))} --steps static --allow-model --no-serve")


def rerun_ai_plan(served_root):
    """`(argv, cwd)` for **Rerun + AI**, or None when this page cannot have one.

    The same two honesty conditions as `rerun_plan` — the programs have to be beside us,
    and the review directory has to sit inside the repository so the producers land where
    the page is reading from — plus a third: `rerun-model.py` has to be there too. It is
    the half nothing else can fake, so a server without it offers the free button and not
    this one, rather than offering a button that quietly does less than its label."""
    if ROOT is None or not REFRESH.is_file() or not MODEL_STEP.is_file():
        return None
    try:
        rel = Path(served_root).resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    entry = actions(served_root).get(RERUN_AI_ACTION)
    if entry:
        return (["/bin/sh", "-c", entry["command"]], ROOT)
    return (["/bin/sh", "-c", rerun_ai_command(rel)], ROOT)


def start_rerun(served_root, ai=False):
    """`(Run, problem, status)` for `POST /__rerun__` and `POST /__rerun_ai__`.

    One rerun at a time — **across both endpoints** — and a second click joins the first
    rather than being refused: two `refresh-report.py` runs over one directory would have
    the second build reading assets the first is halfway through rewriting, and the AI one
    ends in a `refresh-report.py` of its own, so the free button and the paid one are two
    names for the same collision. The lock is the join — there is no second process to
    serialise — and it holds across tabs, because RUNS is per server, not per page.

    Joining across the two is deliberate rather than convenient. Pressing Rerun while
    Rerun + AI is working must not start a second build, and it must not *refuse* either:
    the reader who pressed it could not tell the first one had started, and handing them
    the run in flight is the only answer that shows them what is actually happening. The
    paid one is never started by a click that asked for the free one — a join hands back a
    running Run, it does not launch anything — so the worst case is a reader who gets more
    than they asked for and is told so by the tail they are watching.
    """
    plan = rerun_ai_plan(served_root) if ai else rerun_plan(served_root)
    if plan is None:
        return None, ("this page has no model step behind it" if ai
                      else "this page has no refresh program behind it"), 404, False
    with RUNS_LOCK:
        for run in reversed(RUNS.values()):
            if run.action in RERUN_ACTIONS and run.state == "running":
                if ai:
                    # The paid one is never joined, and this is the change. A silent join
                    # is the right answer for the free button — the reader could not tell
                    # the first one had started and the run in flight is what they wanted.
                    # For this one it is the wrong answer twice over: they are not told
                    # what they joined, so a run somebody else started an hour ago over a
                    # working tree three commits older looks like the one they just asked
                    # for; and when it turns out not to be, the only way out is to press
                    # again and pay again. It happened, and it cost eight dollars.
                    #
                    # So: refused, with the run named, and the decision handed back. 409
                    # rather than 200, because "yours did not start" is the fact the page
                    # has to be able to tell from "yours started".
                    started = time.strftime("%H:%M", time.localtime(run.started))
                    which = ("a paid run" if run.action == RERUN_AI_ACTION
                             else "a rerun")
                    return (run, f"{which} started at {started} is still going; wait for "
                            "it, then decide \u2014 it may already be doing what you want, "
                            "and it may be building from a working tree that has moved "
                            "since.", 409, False)
                run.joins += 1
                return run, None, 200, True
    argv, cwd = plan
    if WATCHER:
        WATCHER.hold()
    try:
        run = Run(RERUN_AI_ACTION if ai else RERUN_ACTION, {"reload": True}, argv, cwd,
                  on_done=lambda _r: WATCHER and WATCHER.release())
    except Exception:
        # A hold whose run never started is a watcher that never reloads anything again.
        if WATCHER:
            WATCHER.release()
        raise
    return remember(run), None, 200, False


def runs_in_flight() -> bool:
    with RUNS_LOCK:
        return any(r.state == "running" for r in RUNS.values())


# --------------------------------------------------------------------------- #
# who is allowed to ask
# --------------------------------------------------------------------------- #

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_of(value: str) -> str:
    """The host out of a Host header or an Origin, lowercased, brackets kept off IPv6."""
    value = (value or "").strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("["):
        return value.partition("]")[0].lstrip("[")
    return value.rsplit(":", 1)[0] if ":" in value else value


def refuse_reason(headers) -> str | None:
    """None when this request may act on the reader's machine; the reason otherwise.

    Any page open in the browser can `fetch('http://127.0.0.1:7654/…')`. Same-origin
    policy stops it *reading* the answer — it does not stop the request arriving, and an
    action server does not need its answer read to have already done the thing. So three
    checks, none of which is sufficient alone:

    - **Host.** A name someone else controls can be pointed at 127.0.0.1 (DNS rebinding),
      after which the attacker's page and this server are same-origin as far as the
      browser is concerned and every other check passes. The cure is to notice that the
      request arrived addressed to a name that is not loopback, and refuse it.
    - **Sec-Fetch-Site.** The browser states, unforgeably by script, where the request came
      from. `same-origin` is the page we served. `none` is a typed URL or a bookmark — no
      page involved, so nothing to be tricked. `same-site` and `cross-site` are refused.
    - **Origin**, when present, has to be this same loopback origin.

    Older browsers send no Sec-Fetch-Site, which is why POST + `Content-Type:
    application/json` matters on top of all this: it is not a CORS-simple request, so a
    cross-origin caller has to preflight, and this server answers no preflight at all."""
    host = _host_of(headers.get("Host", ""))
    if host and host not in LOOPBACK_HOSTS:
        return f"this server answers on loopback only, not to {host}"
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return f"a {site} request may not drive this machine"
    origin = headers.get("Origin")
    if origin and origin.strip().lower() != "null":
        if _host_of(origin) not in LOOPBACK_HOSTS:
            return f"{origin} is not this review"
        if headers.get("Host") and origin.split("//", 1)[-1] != headers["Host"].strip():
            return f"{origin} is not the origin this page was served from"
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    root = "."
    last_seen = time.time()
    hits = 0
    opens = 0
    runs = 0
    # Issued per process, never written to disk, and handed out only over /__human_review__
    # — which is same-origin-guarded and whose body a cross-origin caller cannot read. The
    # design note asked whether to inject this into the HTML as a <meta> at serve time
    # instead; it is not done, because serving the page through a rewriter means owning
    # Content-Length, Range and the no-store dance for the one file whose byte-for-byte
    # fidelity matters most, in exchange for nothing the probe response does not already
    # give. The page holds the token in memory for the life of the tab.
    token = ""

    def reply_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply_text(self, text, status):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Handler.last_seen = time.time()
        route = self.path.split("?")[0]
        if route not in (RUN, RERUN, RERUN_AI):
            self.reply_text("no", 404)
            return
        problem = refuse_reason(self.headers)
        if problem:
            self.reply_text(problem, 403)
            return
        # Insisted on rather than merely accepted. A form post cannot set this header, so
        # requiring it is what forces any cross-origin caller through a preflight this
        # server does not answer — the browser then never sends the request at all.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.reply_text("this endpoint takes application/json", 415)
            return
        if Handler.token and self.headers.get("X-Human-Review-Token") != Handler.token:
            self.reply_text("this page was not served by this server", 403)
            return
        # Drained either way, rerun included: an unread body on a keep-alive connection is
        # the next request as far as the parser is concerned.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) or b"{}"
        except Exception:
            self.reply_text("could not read the request body", 400)
            return
        if route in (RERUN, RERUN_AI):
            # No id and no parameters: there is exactly one thing each of these asks for,
            # and the command behind it is the server's own, not something the page named.
            # Which of the two is in the URL, not in the body — a paid verb must not be
            # reachable by a field a caller can flip.
            run, problem, status, joined = start_rerun(Handler.root, ai=route == RERUN_AI)
            if problem:
                # The refusal carries the run it is refusing for. A sentence alone would
                # leave the page unable to show the reader *what* is going on, which is
                # the whole of what they need before deciding to spend again.
                body = dict(run.snapshot(), error=problem) if run else {"error": problem}
                self.reply_json(body, status)
                return
        else:
            joined = False
            try:
                body = json.loads(raw)
                action_id, params = body["id"], body.get("params") or {}
                if not isinstance(action_id, str) or not isinstance(params, dict):
                    raise TypeError
            except Exception:
                self.reply_text("expected {id, params}", 400)
                return
            run, problem, status = start_run(action_id, params, Handler.root)
        if problem:
            self.reply_text(problem, status)
            return
        Handler.runs += 1
        # `joined` and not silence: a press that handed back somebody else's run looks
        # exactly like a press that started one, and the page has to be able to say which.
        self.reply_json(dict(run.snapshot(), joined=joined))

    def do_GET(self):
        # Everything but the watch poll. The reaper asks "is anyone using this server?",
        # and a tab parked on the report asks for the stamp every second whether or not
        # anybody is in front of it. Counting that as use would mean a page left open on
        # a second monitor keeps a server alive until the machine reboots, which is the
        # exact artifact `--idle-minutes` exists to prevent.
        watching = self.path.split("?")[0] == WATCH
        if not watching:
            Handler.last_seen = time.time()
        if watching:
            # Guarded like the rest: the answer is a fact about the reader's disk, and
            # a stamp that moves is a side channel onto when they are building.
            problem = refuse_reason(self.headers)
            if problem:
                self.reply_text(problem, 403)
                return
            self.reply_json({"stamp": WATCHER.stamp if WATCHER else ""})
            return
        if self.path.split("?")[0] == MARKER:
            # `hits` is here so a caller can tell "the panel reloaded" from "the
            # panel is showing what it already had" — the two look identical from
            # outside, and an embedded browser that quietly kept the previous
            # build is the failure this whole step exists to avoid.
            #
            # The action list rides along because this is also the page's capability
            # probe, and "am I served?" was never the question worth asking: a button
            # needs to know whether *its own verb* exists here, and a build that stopped
            # declaring one has to be able to take it away again.
            problem = refuse_reason(self.headers)
            if problem:
                # Guarded like the endpoints it advertises, and for one concrete reason
                # beyond symmetry: the body carries the token, and a refusal that still
                # answers hands the key out with the lock.
                self.reply_text(problem, 403)
                return
            declared = actions(Handler.root)
            self.reply_json({MARKER_KEY: 1,
                             "served": str(Path(Handler.root).resolve()),
                             "pid": os.getpid(), "hits": Handler.hits,
                             "opens": Handler.opens, "runs": Handler.runs,
                             "token": Handler.token,
                             # The baseline the page was served against. Empty when
                             # nothing is watching, which is how the page knows not to
                             # poll — a build that stopped watching takes the reload
                             # away from a tab that is still open, same as the actions.
                             "watch": WATCHER.stamp if WATCHER else "",
                             # Whether the header's Rerun has anything behind it here.
                             # Answered rather than assumed, for the reason every other
                             # control on this page is: a button drawn live that turns
                             # out not to apply has already been clicked by then.
                             "rerun": bool(rerun_plan(Handler.root)),
                             # And whether the paid one has anything behind it. A separate
                             # answer because it needs a program the free one does not,
                             # and a page that inferred the second from the first would
                             # draw a $5 button over a server that cannot honour it.
                             "rerunAi": bool(rerun_ai_plan(Handler.root)),
                             # What the paid button should say it costs, out of what this
                             # page's own paid runs have cost. Answered here rather than
                             # written into the markup, because the markup is built once
                             # and the price is a fact that moves every time somebody
                             # presses the button.
                             "price": price_estimate(Handler.root),
                             "actions": {name: {"params": e.get("params") or {},
                                                "reload": bool(e.get("reload")),
                                                "label": e.get("label") or ""}
                                         for name, e in declared.items()}})
            return
        if self.path.split("?")[0] == RUN_STATUS:
            problem = refuse_reason(self.headers)
            if problem:
                self.reply_text(problem, 403)
                return
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            asked = q.get("run", [""])[0]
            if not asked:
                # No id: *is anything running here at all*. There was no way to ask that,
                # and the gap cost real money — a reader pressed Rerun + AI, was silently
                # joined to a paid run somebody else had started an hour earlier over an
                # older working tree, and had to pay for a second one when it turned out
                # not to be theirs. A page cannot warn about a run it cannot see.
                with RUNS_LOCK:
                    live = [r for r in RUNS.values() if r.state == "running"]
                    run = live[-1] if live else None
                if run is None:
                    self.reply_json({"active": None, "kind": None, "started": None,
                                     "joined": 0})
                    return
                kind = ("rerun_ai" if run.action == RERUN_AI_ACTION
                        else "rerun" if run.action == RERUN_ACTION else "action")
                self.reply_json({"active": run.snapshot(), "kind": kind,
                                 "started": run.started, "joined": run.joins})
                return
            with RUNS_LOCK:
                run = RUNS.get(asked)
            if run is None:
                self.reply_text("no such run", 404)
                return
            self.reply_json(run.snapshot())
            return
        if self.path.split("?")[0] in (OPEN, OPEN_DIFF):
            # These two shipped as bare GETs with no check on who was asking — they only
            # open an editor, which reads as harmless until you notice that "open this
            # path in the reader's VS Code" is a thing any tab in the browser could make
            # happen. The guard goes on without touching the contract: same URL, same
            # query string, same 204/404, so every page already built keeps working.
            problem = refuse_reason(self.headers)
            if problem:
                self.reply_text(problem, 403)
                return
        if self.path.split("?")[0] == OPEN_DIFF:
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            # `isdigit` rather than a try/except int(): the query string is whatever a
            # page asked for, and a line number is the one thing here with no reason ever
            # to be negative, empty or a word.
            aim = q.get("line", [""])[0]
            problem = open_diff(q.get("path", [""])[0], q.get("base", [""])[0], Handler.root,
                                int(aim) if aim.isdigit() and aim != "0" else None)
            if problem is None:
                Handler.opens += 1
                self.send_response(204)
                self.end_headers()
                return
            # The reason travels back as the body, so the page can say *why* nothing
            # opened instead of the generic shrug it would otherwise have to invent.
            body = problem.encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == OPEN:
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            target = Path(q.get("path", [""])[0])
            line = int(q.get("line", ["1"])[0] or 1)
            ok = ROOT is not None and target.is_file()
            if ok:
                try:
                    target.resolve().relative_to(ROOT.resolve())
                except ValueError:
                    ok = False
            if ok:
                # Counted separately from `hits`: a click that reaches the editor is the
                # one thing about this page that cannot be seen from outside — and on the
                # night this was written, it could not be seen from *inside* either,
                # because a 3am screenshot of a sleeping display is a black rectangle.
                Handler.opens += 1
                open_in_editor(target, line)
            # 204 either way: the click must never navigate the panel away from the
            # guide, and a reader who clicked a stale reference wants the page they
            # were reading, not an error document in place of it.
            self.send_response(204 if ok else 404)
            self.end_headers()
            return
        Handler.hits += 1
        if self.headers.get("Range") and self.serve_range():
            return
        super().do_GET()

    def serve_range(self) -> bool:
        """Answer a byte-range request, so the <video> is seekable.

        SimpleHTTPRequestHandler ignores Range and always answers 200 with the whole file.
        Chromium reads that as "this stream cannot be sought": `video.seekable` comes back
        empty, the scrub bar does nothing, and every timestamp in the transcript beside the
        player silently restarts the film instead of jumping to the moment a finding is
        about. The transcript is the reason the video is worth having, so this is not a
        nicety — without it the page ships a control that lies."""
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return False
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers["Range"].strip())
        if not m:
            return False
        size = os.path.getsize(path)
        first, last = m.group(1), m.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        elif last:                      # a suffix range: the LAST n bytes
            start, end = max(0, size - int(last)), size - 1
        else:
            return False
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return True
        end = min(end, size - 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return True

    # The report is rebuilt in place and reloaded in a browser that was already
    # showing it. A 304 from the previous build is the one answer that makes the
    # reader think nothing changed.
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        # Advertise it up front: Chromium decides whether a media element is seekable from
        # the first response, before it ever sends a Range request.
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, *_):
        pass


def serve(directory, port, idle_minutes, watch=True):
    global ROOT, WATCHER
    ROOT = git_root(directory) or Path(directory).parent
    Handler.root = directory
    Handler.token = secrets.token_urlsafe(16)
    if watch:
        WATCHER = Watcher(directory)
        threading.Thread(target=WATCHER.run, daemon=True).start()
    handler = functools.partial(Handler, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)

    def reaper():
        # Idle *and* quiet. The idle clock is fed by requests, and a reader who clicked
        # "Start the environment" and went to make coffee is making no requests — but
        # `start-docker.sh up` is a child of this process, and shutting down under it
        # would orphan a docker build halfway through the thing the click asked for.
        while time.time() - Handler.last_seen < idle_minutes * 60 or runs_in_flight():
            time.sleep(30)
        httpd.shutdown()

    threading.Thread(target=reaper, daemon=True).start()
    httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", nargs="?", default=".human-review")
    ap.add_argument("--port", type=int, default=7654,
                    help="fixed by default so the URL is the same one every run, and so a "
                         "workbench.externalUriOpeners entry can name it (default: 7654)")
    ap.add_argument("--page", default="review.html")
    ap.add_argument("--idle-minutes", type=float, default=240)
    ap.add_argument("--no-watch", dest="watch", action="store_false",
                    help="do not watch the directory for changes; the page then keeps "
                         "whatever it was served until somebody reloads it by hand")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    directory = Path(args.directory).resolve()
    running = probe(args.port)

    if args.stop:
        if running:
            print(f"stopping the server on :{args.port} (pid {running['pid']})", file=sys.stderr)
            os.kill(running["pid"], 15)
        return 0

    if not directory.is_dir():
        sys.exit(f"[serve-review] not a directory: {directory}")

    port = args.port
    if running and running["served"] != str(directory):
        # Another review is already on the default port — a second checkout, a
        # second branch. Take the next free port and say so; silently serving a
        # different tree at the URL the reader has bookmarked is the worse bug.
        while not free(port):
            port += 1
        print(f"[serve-review] :{args.port} already serves {running['served']} — using :{port}",
              file=sys.stderr)
        running = None

    url = f"http://127.0.0.1:{port}/{args.page}"
    if running:
        print(url)
        return 0

    # Detach by re-exec, not by fork: the caller is a skill mid-run with more
    # steps after this one, and forking a process that has already touched
    # threaded machinery (urllib did, in `probe`) is what Python 3.12 warns about.
    if args._child:
        # Already detached by the parent's `start_new_session`; calling setsid()
        # again here fails with EPERM, which is how this exited silently once.
        serve(str(directory), port, args.idle_minutes, args.watch)
        return 0

    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), str(directory), "--port", str(port),
         "--idle-minutes", str(args.idle_minutes), "--_child"]
        + ([] if args.watch else ["--no-watch"]),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # The child has to hold the port before the caller uses the URL.
    for _ in range(50):
        if probe(port):
            break
        time.sleep(0.1)
    else:
        sys.exit(f"[serve-review] the server did not come up on :{port}")
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
