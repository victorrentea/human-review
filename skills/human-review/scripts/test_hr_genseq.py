"""The hr_genseq pytest plugin: a Python service's test drawn as a call sequence."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SERVICE = '''
from dataclasses import dataclass


@dataclass
class Order:
    id: int

    def total(self):
        return 3


class Repo:
    def load(self, order_id):
        return Order(order_id)


class Service:
    def __init__(self):
        self.repo = Repo()

    def handle(self, order_id):
        order = self.repo.load(order_id)
        self._check(order)
        for _ in range(3):
            self.repo.load(order_id)
        return order.total()

    def _check(self, order):
        return True


def handler(event, _):
    return Service().handle(event["id"])
'''

TESTS = '''
import pytest
from src.service import handler


@pytest.mark.genseq
def test_marked():
    assert handler({"id": 1}, None) == 3


def test_selected():
    assert handler({"id": 2}, None) == 3


def test_neither():
    assert handler({"id": 3}, None) == 3
'''


def _run(tmp_path, select=None):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "service.py").write_text(SERVICE)
    (tmp_path / "tests" / "test_x.py").write_text(TESTS)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(HERE / "genseq"), str(tmp_path)]))
    env.pop("HR_GENSEQ_SELECT", None)
    if select:
        env["HR_GENSEQ_SELECT"] = select
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "hr_genseq", "-q",
                        "-p", "no:cacheprovider", "-p", "no:xdist"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    return r, sorted((tmp_path / "generated").rglob("*.genseq.puml"))


def test_a_marked_test_is_drawn_between_the_service_s_classes(tmp_path):
    r, files = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [f.name for f in files] == ["test_x.py.test-marked.genseq.puml"]
    text = files[0].read_text()
    assert text.splitlines()[1] == "title [[src://tests/test_x.py:6{Open the test} test_marked]]"
    assert 'actor "Test" as p0' in text
    lines = [l for l in text.splitlines() if "->" in l]
    # The test calls the module's handler, which drives the service, which loads from the
    # repository four times in a row, folded into ×4: `_check` stays inside the service and
    # draws nothing, and the Order dataclass is data, not a lifeline.
    assert lines == ["p0 -> p1 : handler()", "p1 -> p2 : handle()", "p2 -> p3 : load() ×4"]
    assert '"Order"' not in text and "_check" not in text


def test_a_selection_traces_those_tests_and_deselects_the_rest(tmp_path):
    r, files = _run(tmp_path, select="tests/test_x.py::test_selected")
    assert "2 passed, 1 deselected" in r.stdout, r.stdout
    assert [f.name for f in files] == ["test_x.py.test-marked.genseq.puml",
                                       "test_x.py.test-selected.genseq.puml"]
