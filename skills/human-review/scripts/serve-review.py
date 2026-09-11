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

It is also, since the action server, the thing that makes three of the page's buttons
*do* what they otherwise only describe. `/__run__` runs a command — but never a command
the page sent. The page sends an **id**; the command behind it was written into
`.human-review/.actions.json` by the build, out of the same content file the button was
rendered from. So the allowlist is not a list somebody has to remember to maintain: it is
the build's own record of what this page is allowed to ask for, and a page from another
build, a page out of the zip, or a page on GitHub Pages can ask for nothing at all.

That mortality is not in tension with running commands, because every action here is a
batch: `start-docker.sh up` exits once the stack answers, the drawio rerun exits once the
picture is redrawn. Nothing here supervises a daemon — the environment reaps itself.

Usage:
  serve-review.py .human-review                      # prints the base URL
  serve-review.py .human-review --page review.html   # prints the page URL
  serve-review.py .human-review --stop
"""
import argparse, collections, functools, http.server, json, os, re, secrets, shlex, shutil, socket, socketserver, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

MARKER = "/__human_review__"
OPEN = "/__open__"
OPEN_DIFF = "/__open_diff__"
RUN = "/__run__"
RUN_STATUS = "/__run_status__"

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

    def __init__(self, action_id, entry, argv, cwd):
        self.id = secrets.token_urlsafe(9)
        self.action = action_id
        self.state = "running"
        self.exit = None
        self.result = {}
        self.reload = bool(entry.get("reload"))
        self._scrape = entry.get("scrape")
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
                    "result": dict(self.result), "reload": self.reload}


# Keyed by run id, and kept after the process exits: the page polls for the final state,
# and a run that vanished the instant it finished would be indistinguishable from a run
# id the server never issued. Bounded, because this process is meant to be forgettable.
RUNS: "collections.OrderedDict[str, Run]" = collections.OrderedDict()
RUNS_LOCK = threading.Lock()
RUNS_KEEP = 40


def start_run(action_id, params, served_root):
    """`(Run, problem, status)`. The id is looked up; the command never comes from the caller."""
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
    run = Run(action_id, entry, argv, cwd)
    with RUNS_LOCK:
        RUNS[run.id] = run
        while len(RUNS) > RUNS_KEEP:
            _, old = RUNS.popitem(last=False)
            if old.state == "running":       # never evict one still going
                RUNS[old.id] = old
                break
    return run, None, 200


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
        if self.path.split("?")[0] != RUN:
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
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
        self.reply_json(run.snapshot())

    def do_GET(self):
        Handler.last_seen = time.time()
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
            with RUNS_LOCK:
                run = RUNS.get(q.get("run", [""])[0])
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


def serve(directory, port, idle_minutes):
    global ROOT
    ROOT = git_root(directory) or Path(directory).parent
    Handler.root = directory
    Handler.token = secrets.token_urlsafe(16)
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
        serve(str(directory), port, args.idle_minutes)
        return 0

    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), str(directory), "--port", str(port),
         "--idle-minutes", str(args.idle_minutes), "--_child"],
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
