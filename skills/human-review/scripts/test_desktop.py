"""`desktop.py`: one place that knows which desktop the server runs on.

The suite pins `HUMAN_REVIEW_DESKTOP=mac` (conftest) so the server's tests read the same
everywhere; these cover WSL and Linux on purpose."""
from __future__ import annotations

import subprocess

import pytest

import desktop


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
    return seen


def _on(monkeypatch, kind, have=()):
    monkeypatch.setenv("HUMAN_REVIEW_DESKTOP", kind)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.setattr(desktop.shutil, "which", lambda b: f"/usr/bin/{b}" if b in have else None)


def test_a_wsl_path_opens_through_the_remote_authority(monkeypatch):
    _on(monkeypatch, "wsl")
    assert desktop.vscode_file_url("/home/u/repo/a.py", 12) == \
        "vscode://vscode-remote/wsl+Ubuntu-22.04/home/u/repo/a.py:12:1"
    assert desktop.to_os_url("vscode://file//home/u/repo/a.py:3:1") == \
        "vscode://vscode-remote/wsl+Ubuntu-22.04/home/u/repo/a.py:3:1"


@pytest.mark.parametrize("kind", ["mac", "linux"])
def test_elsewhere_a_file_url_is_left_alone(monkeypatch, kind):
    _on(monkeypatch, kind)
    assert desktop.vscode_file_url("/r/a.py", 2) == "vscode://file//r/a.py:2:1"
    assert desktop.to_os_url("vscode://file//r/a.py:2:1") == "vscode://file//r/a.py:2:1"


def test_wsl_hands_urls_to_wslview(monkeypatch, calls):
    _on(monkeypatch, "wsl", have=("wslview",))
    assert desktop.open_url("vscode://file//home/u/a.py:1:1")
    assert calls == [["wslview", "vscode://vscode-remote/wsl+Ubuntu-22.04/home/u/a.py:1:1"]]


def test_linux_hands_urls_to_xdg_open(monkeypatch, calls):
    _on(monkeypatch, "linux", have=("xdg-open",))
    assert desktop.open_url("http://127.0.0.1:1/review.html")
    assert calls == [["xdg-open", "http://127.0.0.1:1/review.html"]]


def test_with_nothing_to_open_with_it_says_so(monkeypatch, calls):
    _on(monkeypatch, "linux")
    assert desktop.open_url("http://x") is False and calls == []


def test_off_macos_the_editor_is_started_by_its_cli(monkeypatch, calls):
    _on(monkeypatch, "wsl", have=("code",))
    desktop.launch_editor("/home/u/repo")
    assert calls == [["/usr/bin/code", "/home/u/repo"]]


def test_on_macos_the_editor_is_started_as_an_application(monkeypatch, calls):
    _on(monkeypatch, "mac")
    desktop.launch_editor("/r", "vscode")
    assert calls == [["open", "-a", "Visual Studio Code", "/r"]]
