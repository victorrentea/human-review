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

Usage:
  serve-review.py .human-review                      # prints the base URL
  serve-review.py .human-review --page review.html   # prints the page URL
  serve-review.py .human-review --stop
"""
import argparse, functools, http.server, json, os, re, shutil, socket, socketserver, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

MARKER = "/__human_review__"
OPEN = "/__open__"
OPEN_DIFF = "/__open_diff__"

# A ref, and nothing that could be a flag or a second argument. `git show` is invoked
# without a shell, so this is not about quoting — it is about `--upload-pack=…` and
# friends arriving from a query string.
REF_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

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


def code_cli():
    """The `code` launcher, which is not on PATH in a GUI-launched terminal on macOS."""
    found = shutil.which("code")
    if found:
        return found
    mac = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
    return str(mac) if mac.is_file() else None


def open_diff(rel, base, served_root):
    """Open `rel` as a diff — the file at `base` on the left, the working tree on the right.

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
    if not REF_RE.match(base or ""):
        return "Not a usable git ref"
    target = (ROOT / rel).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        return "That file is outside the repository"
    if not target.is_file():
        return f"{Path(rel).name} is no longer in the working tree"
    show = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{rel}"],
                          capture_output=True)
    if show.returncode != 0:
        return f"{Path(rel).name} does not exist at {base[:8]}"
    if show.stdout == target.read_bytes():
        return f"{Path(rel).name} is unchanged since {base[:8]}"
    short = base[:8]
    stem, ext = Path(rel).stem, Path(rel).suffix
    before = Path(served_root) / ".diffbase" / short / Path(rel).parent / f"{stem}@{short}{ext}"
    before.parent.mkdir(parents=True, exist_ok=True)
    before.write_bytes(show.stdout)
    cli = code_cli()
    if not cli:
        # No VS Code launcher: fall back to what every other reference on the page does
        # rather than leaving the click silent. The reader loses the diff, not the file.
        subprocess.run(["open", f"vscode://file/{target}:1:1"], capture_output=True)
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


class Handler(http.server.SimpleHTTPRequestHandler):
    root = "."
    last_seen = time.time()
    hits = 0
    opens = 0

    def do_GET(self):
        Handler.last_seen = time.time()
        if self.path.split("?")[0] == MARKER:
            # `hits` is here so a caller can tell "the panel reloaded" from "the
            # panel is showing what it already had" — the two look identical from
            # outside, and an embedded browser that quietly kept the previous
            # build is the failure this whole step exists to avoid.
            body = json.dumps({"served": str(Path(Handler.root).resolve()),
                               "pid": os.getpid(), "hits": Handler.hits,
                               "opens": Handler.opens}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == OPEN_DIFF:
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            problem = open_diff(q.get("path", [""])[0], q.get("base", [""])[0], Handler.root)
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
    handler = functools.partial(Handler, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)

    def reaper():
        while time.time() - Handler.last_seen < idle_minutes * 60:
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
