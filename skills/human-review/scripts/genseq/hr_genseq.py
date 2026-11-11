"""A pytest plugin that draws, for each traced test, the sequence of calls it made — the
Sequence tab's `*.genseq.puml`, for a Python service.

    PYTHONPATH=<skill>/scripts/genseq python -m pytest -p hr_genseq …

Which tests: every test marked `@pytest.mark.genseq`, and those `HR_GENSEQ_SELECT` names
(one `path::name` per line — the Sequence step fills it with the tests the branch wrote or
edited); when `HR_GENSEQ_SELECT` is set, every other test is deselected, so the run costs
what the traced tests cost.

What is drawn, while the test's body runs:

  * a lifeline per **service class** of the project (or per module, for a plain function)
    that runs, under `HR_GENSEQ_ROOTS` (default: `src,lambdas,backend,src_py`);
  * an arrow per call that crosses from one lifeline to another, labelled with the function
    called — a call inside one class is that class's own business and is not drawn;
  * an arrow to **AWS <service>** per botocore API call (`SQS: SendMessage → RpaQueue`),
    to **DB** per SQL statement (`SELECT activities`), to **HTTP <host>** per `requests`
    call — the boundaries a reviewer asks about;
  * a run of identical arrows collapsed into one (`×12`), and at most `HR_GENSEQ_MAX`
    arrows (default 120) — past that, a note says how many were left out.

Each diagram is `<HR_GENSEQ_OUT>/<test file>.<test slug>.genseq.puml` (default `generated/`),
and names its test on its title line — `title [[src://<test file>:<line>{…} <test>]]` —
which is how the page pairs a picture with the test that drew it.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

import pytest

MAX_ARROWS = int(os.environ.get("HR_GENSEQ_MAX") or 120)
ROOTS = [r.strip() for r in (os.environ.get("HR_GENSEQ_ROOTS") or "src,lambdas,backend,src_py").split(",")
         if r.strip()]


def pytest_configure(config):
    config.addinivalue_line("markers", "genseq: draw this test's call sequence (human-review)")
    config.pluginmanager.register(Tracer(config), "hr-genseq-tracer")


def _value_type(cls) -> bool:
    """A dataclass, a pydantic model, an enum or a NamedTuple: data passed along, not a
    collaborator — its methods (`to_payload`, `from_payload`) are drawn as nobody's."""
    import dataclasses
    import enum
    if dataclasses.is_dataclass(cls) or issubclass(cls, enum.Enum):
        return True
    if hasattr(cls, "model_fields") or hasattr(cls, "__fields__"):
        return True
    return issubclass(cls, tuple) and hasattr(cls, "_fields")


def _selected() -> set[tuple[str, str]]:
    raw = os.environ.get("HR_GENSEQ_SELECT") or ""
    out = set()
    for line in raw.splitlines():
        path, _, name = line.strip().partition("::")
        if path and name:
            out.add((path, name.split("::")[-1].split("[")[0]))
    return out


class Tracer:
    def __init__(self, config):
        self.root = Path(str(config.rootpath)).resolve()
        self.roots = [str(self.root / r) + os.sep for r in ROOTS if (self.root / r).is_dir()]
        self.out = self.root / (os.environ.get("HR_GENSEQ_OUT") or "generated")
        self.select = _selected()
        self.active = False

    # -- which tests --

    def _wanted(self, item) -> bool:
        if item.get_closest_marker("genseq"):
            return True
        path = os.path.relpath(str(item.path), self.root)
        return (path, item.originalname or item.name) in self.select

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        if not os.environ.get("HR_GENSEQ_SELECT"):
            return
        keep = [i for i in items if self._wanted(i)]
        drop = [i for i in items if not self._wanted(i)]
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = keep

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        if not self._wanted(item):
            yield
            return
        self._start()
        try:
            yield
        finally:
            self._stop()
            self._write(item)

    # -- recording --

    def _start(self):
        self.arrows: list[list] = []
        self.order: list[str] = []
        self.labels: dict[str, str] = {}
        self.stack: list[str] = []
        self.dropped = 0
        self._lifeline("Test", "Test")          # the test drives; it is drawn first
        self._patches = []
        self._patch_boundaries()
        self.active = True
        sys.setprofile(self._profile)
        threading.setprofile(self._profile)

    def _stop(self):
        sys.setprofile(None)
        threading.setprofile(None)
        self.active = False
        for undo in reversed(self._patches):
            undo()

    def _lifeline(self, key: str, label: str) -> str:
        if key not in self.labels:
            self.labels[key] = label
            self.order.append(key)
        return key

    def _here(self) -> str:
        return self.stack[-1] if self.stack else self._lifeline("Test", "Test")

    def _arrow(self, src: str, dst: str, label: str):
        if self.arrows and self.arrows[-1][:3] == [src, dst, label]:
            self.arrows[-1][3] += 1
            return
        if len(self.arrows) >= MAX_ARROWS:
            self.dropped += 1
            return
        self.arrows.append([src, dst, label, 1])

    def _participant(self, frame) -> tuple[str, str] | None:
        filename = frame.f_code.co_filename
        if not any(filename.startswith(r) for r in self.roots):
            return None
        local = frame.f_locals
        owner = local.get("self") if "self" in local else None
        cls = type(owner) if owner is not None else local.get("cls") if isinstance(local.get("cls"), type) else None
        if cls is not None and (getattr(cls, "__module__", "") or "").split(".")[0] in ROOTS:
            if _value_type(cls):
                return None
            return f"{cls.__module__}.{cls.__qualname__}", cls.__name__
        module = frame.f_globals.get("__name__", "") or Path(filename).stem
        return module, module.rsplit(".", 1)[-1]

    def _profile(self, frame, event, arg):
        if not self.active or event not in ("call", "return"):
            return
        who = self._participant(frame)
        if who is None:
            return
        key, label = who
        if event == "call":
            name = frame.f_code.co_name
            if name.startswith("<") or (name.startswith("__") and name.endswith("__")):
                self.stack.append(self._lifeline(key, label))
                return
            caller = self._here()
            callee = self._lifeline(key, label)
            if caller != callee:
                self._arrow(caller, callee, f"{name}()")
            self.stack.append(callee)
        elif self.stack:
            self.stack.pop()

    # -- the boundaries --

    def _patch(self, owner, attr, wrapper):
        original = getattr(owner, attr)
        setattr(owner, attr, wrapper(original))
        self._patches.append(lambda: setattr(owner, attr, original))

    def _patch_boundaries(self):
        tracer = self
        try:
            from botocore.client import BaseClient

            def aws(original):
                def call(client, operation, params):
                    service = client.meta.service_model.service_name
                    target = ""
                    for k in ("QueueUrl", "TopicArn", "TableName", "Bucket", "FunctionName",
                              "StateMachineArn"):
                        if isinstance(params.get(k), str):
                            target = " → " + re.split(r"[/:]", params[k])[-1]
                            break
                    if operation == "PutEvents":
                        kinds = sorted({e.get("DetailType", "") for e in params.get("Entries") or []})
                        target = " → " + ", ".join(k for k in kinds if k)[:60] if kinds else ""
                    dst = tracer._lifeline(f"aws:{service}", f"AWS {service.upper()}")
                    tracer._arrow(tracer._here(), dst, f"{operation}{target}")
                    return original(client, operation, params)
                return call
            self._patch(BaseClient, "_make_api_call", aws)
        except Exception:
            pass
        try:
            from sqlalchemy import event
            from sqlalchemy.engine import Engine

            def sql(conn, cursor, statement, parameters, context, executemany):
                words = statement.split()
                verb = words[0].upper() if words else "SQL"
                m = re.search(r"\b(?:FROM|INTO|UPDATE|TABLE)\s+[\"`]?(\w+)", statement, re.I)
                dst = tracer._lifeline("db", "DB")
                tracer._arrow(tracer._here(), dst, f"{verb} {m.group(1) if m else ''}".strip())
            event.listen(Engine, "before_cursor_execute", sql)
            self._patches.append(lambda: event.remove(Engine, "before_cursor_execute", sql))
        except Exception:
            pass
        try:
            import requests
            from urllib.parse import urlsplit

            def http(original):
                def request(session, method, url, *a, **kw):
                    u = urlsplit(str(url))
                    dst = tracer._lifeline(f"http:{u.netloc}", f"HTTP {u.netloc}")
                    tracer._arrow(tracer._here(), dst, f"{str(method).upper()} {u.path or '/'}")
                    return original(session, method, url, *a, **kw)
                return request
            self._patch(requests.Session, "request", http)
        except Exception:
            pass

    # -- the picture --

    def _write(self, item):
        if len(self.order) <= 1 and not self.arrows:
            return
        test_file = os.path.relpath(str(item.path), self.root)
        line = (item.location[1] or 0) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", item.name.lower()).strip("-")[:80]
        target = self.out / f"{test_file}.{slug}.genseq.puml"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Left to right in the order the story meets them: the test, then each lifeline at
        # the first arrow that reaches or leaves it.
        order = ["Test"]
        for src, dst, *_ in self.arrows:
            order += [k for k in (src, dst) if k not in order]
        self.order = order
        alias = {k: f"p{i}" for i, k in enumerate(self.order)}
        kind = {k: ("database" if k == "db" else "queue" if k.startswith("aws:")
                    else "boundary" if k.startswith("http:") else "actor" if k == "Test"
                    else "participant") for k in self.order}
        lines = ["@startuml",
                 f"title [[src://{test_file}:{line}{{Open the test}} {item.name}]]",
                 "' drawn by human-review's hr_genseq pytest plugin — do not edit"]
        lines += [f'{kind[k]} "{self.labels[k]}" as {alias[k]}' for k in self.order]
        for src, dst, label, n in self.arrows:
            arrow = "->" if not (dst == "db" or dst.startswith(("aws:", "http:"))) else "->>"
            lines.append(f"{alias[src]} {arrow} {alias[dst]} : {label}"
                         + (f" ×{n}" if n > 1 else ""))
        if self.dropped:
            lines.append(f"note across: {self.dropped} more calls not drawn (HR_GENSEQ_MAX={MAX_ARROWS})")
        lines.append("@enduml")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
