#!/usr/bin/env python3
"""The CODEOWNERS matcher, pinned.

The whole flag rests on one question — *does this path match that rule* — and the
answer is gitignore's, not fnmatch's. The failure mode is silent in both directions: a
pattern that matches too little drops the warning the reviewer needed, and one that
matches too much cries wolf until the tab is ignored. Neither shows up by looking at
the page.

Run it directly (`python3 test_codeowners.py`) or under pytest.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("codeowners_check", HERE / "codeowners-check.py")
co = importlib.util.module_from_spec(spec)
spec.loader.exec_module(co)


def match(pattern: str, path: str) -> bool:
    return bool(co.pattern_to_regex(pattern).match(path))


PATTERN_CASES = [
    # (pattern, path, expected)
    ("/openapi.yaml", "openapi.yaml", True),
    ("/openapi.yaml", "sub/openapi.yaml", False),           # a leading slash anchors
    ("**/pom.xml", "pom.xml", True),                        # `**/` includes "no directory"
    ("**/pom.xml", "backend/pom.xml", True),
    ("**/pom.xml", "a/b/c/pom.xml", True),
    ("**/pom.xml", "backend/pom.xml.bak", False),
    ("/.github/workflows/*", ".github/workflows/ci.yml", True),
    ("/.github/workflows/*", ".github/workflows/nested/ci.yml", True),  # a dir under it
    ("/.github/workflows/*", ".github/dependabot.yml", False),
    ("/src/db/migration/", "src/db/migration/V1__init.sql", True),      # trailing slash
    ("/src/db/migration/", "src/db/migrationX/V1.sql", False),
    ("/a/guardrail/**", "a/guardrail/x/y/Test.java", True),
    ("/a/guardrail/**", "a/guardrail", False),              # `a/**` is what is *inside* a
    ("/docs", "docs/adr/0001.md", True),                    # a bare dir claims its contents
    ("docs", "backend/docs/adr.md", True),                  # unanchored: at any depth
    ("*.sql", "backend/DB.sql", True),                      # unanchored wildcard
    ("/*.sql", "backend/DB.sql", False),                    # anchored: root only
    ("/backend/*.sql", "backend/DB.sql", True),
    ("/backend/*.sql", "backend/db/DB.sql", False),         # `*` never crosses a slash
    ("/a/**/z.txt", "a/b/c/z.txt", True),
    ("/a/**/z.txt", "a/z.txt", True),
    ("file?.txt", "file1.txt", True),
    ("file?.txt", "file12.txt", False),
]


def test_patterns():
    for pattern, path, expected in PATTERN_CASES:
        assert match(pattern, path) is expected, f"{pattern!r} vs {path!r}"


FLAT = """
# a flat GitHub file
/openapi.yaml        @org/api
**/pom.xml           @org/build
/backend/**          @org/backend
/backend/README.md
"""


def test_last_match_wins():
    rules, sectioned, problems = co.parse(FLAT)
    assert not sectioned and not problems

    def owners(path):
        return sorted({o for r in co.owners_for(path, rules, sectioned) for o in r.owners})

    assert owners("openapi.yaml") == ["@org/api"]
    # `/backend/**` sits below `**/pom.xml`, so it takes the backend's pom off @org/build.
    # That override is the most surprising thing about the format; pin it.
    assert owners("backend/pom.xml") == ["@org/backend"]
    assert owners("frontend/pom.xml") == ["@org/build"]
    # A pattern with no owner un-owns what an earlier rule claimed.
    assert owners("backend/README.md") == []
    assert owners("README.md") == []


SECTIONED = """
[API] @org/api
/openapi.yaml
^[Docs] @org/writers
/docs/
[Build]
**/pom.xml @org/build
"""


def test_sections_are_all_required():
    rules, sectioned, problems = co.parse(SECTIONED)
    assert sectioned and not problems
    # A section header's trailing owners are the default for rules that name none.
    hits = co.owners_for("openapi.yaml", rules, sectioned)
    assert [o for r in hits for o in r.owners] == ["@org/api"]
    # Unlike the flat file, a later section does not override an earlier one.
    rules2, s2, _ = co.parse(SECTIONED + "\n[Everything]\n/** @org/all\n")
    assert sorted({o for r in co.owners_for("openapi.yaml", rules2, s2) for o in r.owners}) == [
        "@org/all", "@org/api"]
    # `^[Docs]` suggests, it does not block.
    docs = co.owners_for("docs/adr.md", rules, sectioned)
    assert docs and all(r.optional for r in docs)


BROKEN = """
!/secrets.env  @org/sec
/a.txt         not-an-owner
"""


def test_lines_a_host_would_skip_are_reported():
    rules, _, problems = co.parse(BROKEN)
    whys = " ".join(w for _, _, w in problems)
    assert "negation" in whys and "not a user, team or email" in whys
    # The negated line claims nothing — it must not end up protecting secrets.env.
    assert not any(r.matches("secrets.env") for r in rules)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
