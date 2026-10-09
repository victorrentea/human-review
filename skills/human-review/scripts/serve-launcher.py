#!/usr/bin/env python3
"""The one listener a review page read off disk can ask to serve it.

A page opened as `file://…/review.html` can do nothing but copy commands: its buttons run
their action only when `serve-review.py` serves it. The masthead's **Serve** chip used to
copy the line that starts that server, and the reader then had to find a terminal, paste
it, and go to the tab it opened. This makes the click do it: the page POSTs its own path
here, this runs `serve-review.py` on that directory, and the page `location.replace`s
itself with the URL it gets back — the same tab, now served.

Why a listener on loopback and not a `human-review://` URL scheme (the route
`install-drawio-url-handler.sh` took for draw.io): from a `file://` page Chrome asks
"Open <app>?" on **every** click of a custom scheme — the "always allow" box is offered
only to http(s) origins, as the drawio pair stored for `http://127.0.0.1:7655` shows —
and a page cannot tell whether a scheme handler exists at all: an unknown scheme fails
silently. A fetch answers both questions at once: it works without a dialog, and a refused
connection is the "not installed" that sends the click back to copying the command.

It only ever runs one program, `serve-review.py`, over a page that already exists and
already carries the Serve chip, and it takes the request only from a page with no origin
— which is what Chrome sends from `file://` — so a web site cannot have it start
anything. It does not rebuild: the page on disk is served as it is.

Installed as a LaunchAgent by `install-serve-launcher.sh`, which is also how it is removed.

    serve-launcher.py               # listen on 127.0.0.1:7653 (what the agent runs)
    serve-launcher.py --port 17653  # elsewhere, for a test
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVE = HERE / "serve-review.py"
#: Below serve-review's 7654: that one walks *up* to the next free port, never down here.
PORT = 7653
#: The marker the page looks for — a JSON body only this program writes.
MARKER = "humanReviewLauncher"
#: The string every page with a Serve chip carries (`build-review-html.py`).
SERVE_CHIP = 'id="hr-serve"'


def check_page(raw: str) -> tuple[Path | None, str]:
    """(the page, "") when `raw` names a review page this may serve, else (None, why)."""
    if not raw or not os.path.isabs(raw):
        return None, "not an absolute path"
    page = Path(os.path.realpath(raw))
    if page.suffix != ".html" or not page.is_file():
        return None, f"no page at {raw}"
    try:
        page.relative_to(Path.home().resolve())
    except ValueError:
        return None, "only pages under the home directory are served"
    try:
        with page.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(4 << 20)
    except OSError as exc:
        return None, str(exc)
    if SERVE_CHIP not in head:
        return None, f"{page.name} is not a review page"
    return page, ""


def serve(page: Path) -> tuple[int, dict]:
    """Run serve-review.py on the page's directory; (status, body). It prints the URL it
    serves on last — a new server's, or the one already serving that directory. `--no-open`:
    the tab that asked becomes the served page, so a second tab would be a duplicate."""
    cmd = [sys.executable, str(SERVE), str(page.parent), "--page", page.name, "--no-open"]
    try:
        r = subprocess.run(cmd, cwd=str(page.parent), capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 504, {"error": "serve-review.py did not answer within 30 s"}
    lines = (r.stdout or "").strip().splitlines()
    url = lines[-1].strip() if lines else ""
    if r.returncode != 0 or not url.startswith("http://127.0.0.1:"):
        why = ((r.stderr or "").strip().splitlines() or ["serve-review.py failed"])[-1]
        return 500, {"error": why}
    return 200, {"url": url}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # `null` and nothing else: the origin Chrome gives a page read off disk.
        self.send_header("Access-Control-Allow-Origin", "null")
        self.end_headers()
        self.wfile.write(data)

    def _from_disk(self) -> bool:
        return self.headers.get("Origin") == "null"

    def do_OPTIONS(self):  # noqa: N802 — the preflight a JSON POST needs
        if not self._from_disk():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "POST, GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome's private-network preflight, should it ever count a file:// page as public.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/ping":
            self._send(200, {MARKER: 1, "serve": str(SERVE)})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/serve":
            self._send(404, {"error": "unknown path"})
            return
        if not self._from_disk():
            self._send(403, {"error": "only a page opened from disk may ask"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1 << 16)) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"error": "a JSON body naming the page"})
            return
        page, why = check_page(str(body.get("page") or ""))
        if page is None:
            self._send(400, {"error": why})
            return
        status, out = serve(page)
        print(f"[serve-launcher] {page} -> {out.get('url') or out.get('error')}", flush=True)
        self._send(status, out)

    def log_message(self, *_):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"[serve-launcher] listening on 127.0.0.1:{args.port}", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
