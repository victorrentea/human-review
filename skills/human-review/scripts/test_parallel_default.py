#!/usr/bin/env python3
"""The suite spreads itself over the cores, and only the top-level run does.

The hook lives in `conftest.py` and runs wherever pytest does, including inside each xdist
worker. Unguarded, a worker starts workers: the first version of it did, and took the
machine to its process limit in under a minute (5 Nov 2026). What is pinned here is that
guard, in a real run, because a stubbed `config` would only pin the stub.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

pytest.importorskip("xdist")


def _run(tmp_path, *args, **env):
    """Pytest over one throwaway test beside a copy of the real conftest, printing how many
    workers it ended up with. `-p no:cacheprovider`: no cache dir left in the checkout."""
    (tmp_path / "conftest.py").write_text((HERE / "conftest.py").read_text(encoding="utf-8"),
                                          encoding="utf-8")
    (tmp_path / "test_probe.py").write_text(
        "import os\n"
        "def test_probe():\n"
        "    print('WORKER', os.environ.get('PYTEST_XDIST_WORKER', 'none'),\n"
        "          os.environ.get('PYTEST_XDIST_WORKER_COUNT', '0'))\n",
        encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rP", *args],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, env={**clean, **env})
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_a_plain_run_uses_the_workers_it_is_given_and_no_more(tmp_path):
    out = _run(tmp_path, "test_probe.py", PYTEST_XDIST_AUTO_NUM_WORKERS="2")
    assert "WORKER gw" in out and out.rstrip().count("\n") < 40
    assert "WORKER gw0 2" in out or "WORKER gw1 2" in out, out


def test_a_worker_does_not_start_workers_of_its_own(tmp_path):
    """Exactly what a worker sees: the variable set and `-n` unset. It stays in-process."""
    out = _run(tmp_path, "test_probe.py", PYTEST_XDIST_WORKER="gw0")
    assert "WORKER gw0 0" in out, "the probe ran in this process, under no controller"


@pytest.mark.parametrize("args", [("test_probe.py::test_probe",), ("test_probe.py", "-k", "probe"),
                                  ("test_probe.py", "-n", "0")])
def test_a_run_aimed_at_one_test_stays_in_this_process(tmp_path, args):
    assert "WORKER none 0" in _run(tmp_path, *args)
