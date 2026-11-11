"""What the review server asks of the desktop it runs on: open a URL, start an editor on a
folder, reveal a file in the file manager.

The skill was born on macOS and spoke `open`, `open -a` and `open -R` directly. On Linux, and
above all on WSL (Ludvig's laptops: the checkout and the server in WSL, the browser and VS Code
on Windows), none of those exist. Every desktop call goes through here instead, so the server
reads the same on all three and only this module knows which one it is on.

WSL detail: a `vscode://file/<abs>` URL handed to the Windows side names a Windows path, so a
WSL path in it opens nothing. A WSL checkout is opened through the remote authority instead,
`vscode://vscode-remote/wsl+<distro>/<abs>:<line>:<col>` — the URL VS Code itself writes for
a file in a WSL window.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def kind() -> str:
    """`mac`, `wsl` or `linux` (anything else that is not macOS reads as linux).

    `HUMAN_REVIEW_DESKTOP` overrides the guess — the test suite pins it, so a test written
    against one desktop's commands reads the same on every machine that runs it."""
    forced = os.environ.get("HUMAN_REVIEW_DESKTOP", "").strip().lower()
    if forced in ("mac", "wsl", "linux"):
        return forced
    return _detected()


@lru_cache(maxsize=1)
def _detected() -> str:
    if sys.platform == "darwin":
        return "mac"
    if os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    try:
        if "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


def wsl_distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"


def vscode_file_url(path, line: int = 1, col: int = 1) -> str:
    """The URL that opens `path` at `line` in VS Code, from whatever runs the browser."""
    p = str(path)
    if kind() == "wsl" and p.startswith("/"):
        return f"vscode://vscode-remote/wsl+{wsl_distro()}{p}:{line}:{col}"
    return f"vscode://file/{p}:{line}:{col}"


def to_os_url(url: str) -> str:
    """A `vscode://file/<abs>` URL the build baked in, made to open on this desktop."""
    prefix = "vscode://file/"
    if kind() == "wsl" and url.startswith(prefix + "/"):
        return f"vscode://vscode-remote/wsl+{wsl_distro()}{url[len(prefix):]}"
    return url


def _opener() -> list[str] | None:
    if kind() == "mac":
        return ["open"]
    if kind() == "wsl":
        for cand in ("wslview", "explorer.exe"):
            if shutil.which(cand):
                return [cand]
        return ["cmd.exe", "/c", "start", ""] if shutil.which("cmd.exe") else None
    return ["xdg-open"] if shutil.which("xdg-open") else None


def open_url(url: str) -> bool:
    """Hand `url` (an http page, or an editor's own link) to the desktop. False when
    this machine has nothing to hand it to."""
    cmd = _opener()
    if not cmd:
        return False
    subprocess.run([*cmd, to_os_url(url)], capture_output=True)
    return True


#: The application `open -a` starts for each editor, on macOS.
MAC_APP = {"vscode": "Visual Studio Code", "idea": "IntelliJ IDEA"}


def code_cli() -> str | None:
    """The `code` launcher. On WSL it is VS Code's own shim, which opens a WSL remote
    window on a WSL folder; on macOS it is often missing from a GUI-launched PATH."""
    found = shutil.which("code")
    if found:
        return found
    mac = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
    return str(mac) if mac.is_file() else None


def launch_editor(folder, app: str = "vscode") -> None:
    """Open `folder` as a project in the editor, in the running instance where possible."""
    if kind() == "mac":
        subprocess.Popen(["open", "-a", MAC_APP.get(app, app), str(folder)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    cli = code_cli() if app == "vscode" else shutil.which("idea")
    if cli:
        subprocess.Popen([cli, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(path) -> None:
    """A file-manager window on the folder with `path` selected (macOS, WSL), or on its
    folder (Linux, where no common file manager takes a selection)."""
    p = Path(path)
    if kind() == "mac":
        subprocess.Popen(["open", "-R", str(p)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind() == "wsl" and shutil.which("explorer.exe") and shutil.which("wslpath"):
        win = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True).stdout.strip()
        subprocess.Popen(["explorer.exe", f"/select,{win}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
