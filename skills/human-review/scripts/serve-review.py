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
import argparse, functools, http.server, json, os, socket, socketserver, subprocess, sys, threading, time, urllib.parse, urllib.request
from pathlib import Path

MARKER = "/__human_review__"
OPEN = "/__open__"

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


def open_in_editor(path, line):
    """Land the reader in the class. Through the VS Code window that has this repo open
    where the bridge is installed, and through the OS otherwise.

    The page cannot do this itself: its references are `vscode://file/...` links, and the
    embedded browser's iframe is sandboxed under a `frame-src *` CSP, so a webview cannot
    hand a custom scheme to the OS — the click does nothing whatever the anchor says. It
    *can* fetch its own origin, which is how the request gets here."""
    folder = ROOT.name if ROOT else None
    for f in sorted((Path.home() / ".walkie-talkie" / "ide").glob("vscode-*.json")):
        try:
            entry = json.loads(f.read_text())
            ping = urllib.request.Request(
                f"http://127.0.0.1:{entry['port']}/ping", headers={"x-relay-token": entry["token"]})
            if json.load(urllib.request.urlopen(ping, timeout=2)).get("folder") != folder:
                continue
            req = urllib.request.Request(
                f"http://127.0.0.1:{entry['port']}/open-file", method="POST",
                data=json.dumps({"path": str(path), "line": line}).encode(),
                headers={"x-relay-token": entry["token"], "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            return "relay"
        except Exception:
            continue
    # No bridge: hand it to the OS, which routes it to the last-active window.
    subprocess.run(["open", f"vscode://file/{path}:{line}:1"], capture_output=True)
    return "os"


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
        super().do_GET()

    # The report is rebuilt in place and reloaded in a browser that was already
    # showing it. A 304 from the previous build is the one answer that makes the
    # reader think nothing changed.
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
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
