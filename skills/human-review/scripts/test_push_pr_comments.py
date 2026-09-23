#!/usr/bin/env python3
"""What `push-pr-comments.py` promises: every comment lands on a line GitHub accepts or one
level up, and a second push updates instead of duplicating.

GitHub is replaced by `FakeGh`, which keeps the PR's comments and reviews in two lists and
answers the four calls the script makes. Nothing here touches the network.

Run with:  python3 -m pytest test_push_pr_comments.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("push_pr_comments", HERE / "push-pr-comments.py")
ppc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ppc     # @dataclass looks its module up there
_spec.loader.exec_module(ppc)

REPO, PR = "o/r", 7

DIFF = """diff --git a/src/A.java b/src/A.java
index 1..2 100644
--- a/src/A.java
+++ b/src/A.java
@@ -8,6 +8,8 @@ class A {
 x
@@ -40,3 +42,4 @@ class A {
 y
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/old/B.kt b/new/B.kt
similarity index 90%
rename from old/B.kt
rename to new/B.kt
--- a/old/B.kt
+++ b/new/B.kt
@@ -1 +1,3 @@
 z
"""

A_LINES = [f"line {i}" for i in range(1, 60)]
A_LINES[12 - 1] = "    return vet;"


def diff() -> ppc.Diff:
    files = ppc.parse_diff(DIFF)
    return ppc.Diff(head="h" * 40, base="b" * 40, files=files,
                    reader=lambda p: A_LINES if p == "src/A.java" else None)


def c(**kw) -> dict:
    base = {"pile": "ignored", "title": "Keep the vet", "path": "src/A.java", "line": 12,
            "side": "RIGHT", "body": "🔴 **Open issue** — keep the vet"}
    base.update(kw)
    return base


# -------------------------------------------------------------------- the diff

def test_parse_diff_keeps_right_side_hunks_and_drops_deleted_files():
    files = ppc.parse_diff(DIFF)
    assert files["src/A.java"] == [(8, 15), (42, 45)]
    assert "gone.txt" not in files
    assert files["new/B.kt"] == [(1, 3)]


def test_slug_agrees_between_markdown_and_the_pages_html_title():
    md = "vet_id is `ON DELETE SET NULL`, so \"none\" is normal"
    page = "vet_id is <code>ON DELETE SET NULL</code>, so &quot;none&quot; is normal"
    assert ppc.slug(md) == ppc.slug(page) == "vet-id-is-on-delete-set-null-so-none-is-normal"
    assert len(ppc.slug("word " * 40)) <= ppc.SLUG_MAX


# -------------------------------------------------------------------- anchoring

def test_a_line_in_the_diff_is_posted_as_written_with_its_marker():
    r = ppc.resolve(c(anchor="return vet;"), diff())
    assert r.mode == "line" and not r.note
    assert r.comment["line"] == 12 and r.comment["side"] == "RIGHT"
    assert r.comment["body"].endswith("<!-- hr:I:keep-the-vet -->")


def test_an_anchor_that_moved_follows_its_text():
    r = ppc.resolve(c(line=9, start_line=8, anchor="return vet;"), diff())
    assert r.mode == "line" and "moved" in r.note
    assert (r.comment["start_line"], r.comment["line"]) == (11, 12)


def test_an_anchor_that_vanished_downgrades_to_a_file_comment():
    r = ppc.resolve(c(anchor="return owner;"), diff())
    assert r.mode == "file" and r.comment["subject_type"] == "file"
    assert "line" not in r.comment


def test_a_line_outside_every_hunk_downgrades_to_a_file_comment():
    r = ppc.resolve(c(line=30), diff())
    assert r.mode == "file" and "outside" in r.note


def test_a_file_not_in_the_diff_is_folded_into_the_review_body():
    for path in ("src/Other.java", "gone.txt"):
        r = ppc.resolve(c(path=path), diff())
        assert r.mode == "body"


def test_a_range_across_two_hunks_is_narrowed_to_its_last_line():
    r = ppc.resolve(c(start_line=10, line=43), diff())
    assert r.mode == "line" and "start_line" not in r.comment and r.comment["line"] == 43


def test_validate_names_every_problem():
    bad = {"event": "APPROVE", "comments": [
        {"path": "a", "body": "x", "line": 3},
        c(line="12"), c(side="LEFT"), c(), c()]}
    problems = "\n".join(ppc.validate(bad))
    for needle in ("needs `hr_id`", "integer `line`", "LEFT", "repeats id", "COMMENT"):
        assert needle in problems


# -------------------------------------------------------------------- GitHub

class FakeGh:
    def __init__(self):
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.sent: list[ppc.Call] = []
        self._id = 100

    def _next(self) -> int:
        self._id += 1
        return self._id

    def list(self, path: str) -> list[dict]:
        return list(self.comments if path.endswith("/comments") else self.reviews)

    def send(self, call: ppc.Call) -> dict:
        self.sent.append(call)
        p = call.payload
        if call.method == "POST" and call.path.endswith("/reviews"):
            rid = self._next()
            self.reviews.append({"id": rid, "body": p["body"], "html_url": f"u/review/{rid}"})
            for k in p["comments"]:
                cid = self._next()
                self.comments.append({**k, "id": cid, "html_url": f"u/c/{cid}"})
        elif call.method == "POST":
            cid = self._next()
            self.comments.append({**p, "id": cid, "html_url": f"u/c/{cid}"})
        elif call.method == "PATCH":
            cid = int(call.path.rsplit("/", 1)[1])
            next(x for x in self.comments if x["id"] == cid)["body"] = p["body"]
        elif call.method == "PUT":
            rid = int(call.path.rsplit("/", 1)[1])
            next(x for x in self.reviews if x["id"] == rid)["body"] = p["body"]
        return {}


def push(gh: FakeGh, payload: dict, tmp: Path) -> tuple[ppc.Plan, dict]:
    p = ppc.plan(payload, diff(), REPO, PR, gh.list("x/comments"), gh.list("x/reviews"))
    rec = ppc.execute(p, gh, REPO, PR, tmp / "pr-comments.posted.json", {"pr": PR})
    return p, rec


PAYLOAD = {"event": "COMMENT", "body": "Review record.", "comments": [
    c(title="Keep the vet", anchor="return vet;"),
    c(title="Constructor too long", line=30),
    c(title="Manual is stale", path="docs/manual.md"),
]}


def test_first_push_posts_one_review_and_one_file_comment(tmp_path):
    gh = FakeGh()
    p, rec = push(gh, PAYLOAD, tmp_path)
    kinds = [(x.method, x.path.split("/")[-1]) for x in gh.sent]
    assert kinds == [("POST", "comments"), ("POST", "reviews")]
    review = gh.sent[-1].payload
    assert review["commit_id"] == "h" * 40 and len(review["comments"]) == 1
    assert "docs/manual.md" in review["body"] and ppc.REVIEW_MARKER in review["body"]
    assert rec["comments"]["I:keep-the-vet"]["html_url"].startswith("u/c/")
    assert rec["comments"]["I:manual-is-stale"]["html_url"] == rec["review_url"]
    assert json.loads((tmp_path / "pr-comments.posted.json").read_text()) == rec


def test_second_push_of_the_same_payload_sends_nothing(tmp_path):
    gh = FakeGh()
    push(gh, PAYLOAD, tmp_path)
    gh.sent.clear()
    p, _ = push(gh, PAYLOAD, tmp_path)
    assert gh.sent == []
    assert len(gh.comments) == 2 and len(gh.reviews) == 1


def test_an_edited_body_is_patched_and_a_new_item_gets_its_own_review(tmp_path):
    gh = FakeGh()
    push(gh, PAYLOAD, tmp_path)
    gh.sent.clear()
    edited = json.loads(json.dumps(PAYLOAD))
    edited["comments"][0]["body"] = "🔴 **Open issue** — keep the vet, reworded"
    edited["comments"].append(c(title="Brand new", line=44))
    push(gh, edited, tmp_path)
    kinds = [(x.method, x.path.split("/")[-2]) for x in gh.sent]
    assert ("PATCH", "comments") in kinds
    new = [x for x in gh.sent if x.method == "POST"]
    assert len(new) == 1 and [k["line"] for k in new[0].payload["comments"]] == [44]
    assert ppc.REVIEW_MARKER not in new[0].payload["body"]     # one summary, not two
    assert sum("hr:I:keep-the-vet" in x["body"] for x in gh.comments) == 1


def test_dry_run_calls_are_pasteable_gh_lines():
    p = ppc.plan(PAYLOAD, diff(), REPO, PR, [], [])
    line = p.calls[-1].shell()
    assert line.startswith(f"gh api -X POST repos/{REPO}/pulls/{PR}/reviews --input - <<'JSON'")
    assert json.loads(line.split("\n", 1)[1].rsplit("\nJSON", 1)[0])["event"] == "COMMENT"


# -------------------------------------------------------------------- against a real repo

POINTS = """---
reviewers: /code-review high
---

## Fixed

### Return the vet
- file: src/A.java:3
- fixed-in: HEAD
The vet was dropped.

## Ignored

### Rename the class
- file: src/Other.java:1
- severity: low
- why: out of scope

## Assumptions

### None means none
- file: src/A.java:2-3
- alternative: a null vet is an error
- confidence: 0.5
- why: the ticket says so
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def test_from_review_points_then_check_against_a_real_diff(tmp_path, capsys):
    root = tmp_path
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"), _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src/A.java").write_text("class A {\n}\n")
    (root / "src/Other.java").write_text("class Other {}\n")
    _git(root, "add", "."), _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "feature")
    (root / "src/A.java").write_text("class A {\n  Vet vet;\n  Vet vet() { return vet; }\n}\n")
    (root / "review-points.md").write_text(POINTS)
    _git(root, "add", "."), _git(root, "commit", "-qm", "feature")

    assert ppc.main(["--root", str(root), "--from-review-points"]) == 0
    payload = json.loads((root / ppc.DEFAULT_FILE).read_text())
    by = {x["hr_id"]: x for x in payload["comments"]}
    assert by["F:return-the-vet"]["anchor"] == "Vet vet() { return vet; }"
    assert by["F:return-the-vet"]["body"].startswith("🛠 **Auto-fixed** in ")
    assert by["A:none-means-none"]["start_line"] == 2
    assert "confidence 0.50" in by["A:none-means-none"]["body"]
    assert "🔴 **Open issue** · low" in by["I:rename-the-class"]["body"]
    capsys.readouterr()

    assert ppc.main(["--root", str(root), "--check", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "3 comments: 2 on a line, 0 on a file, 1 folded" in out


def test_the_pages_copy_of_slug_agrees_with_this_one():
    """The Review tab links an item to its comment by recomputing this id from the title it
    holds as HTML; `hrbuild/tabs/review.py` keeps its own copy of `slug` (the script is a
    dataclass module, which `shared.actions._load` cannot load). Until that copy lands,
    there is nothing to compare."""
    src = (HERE / "hrbuild/tabs/review.py").read_text(encoding="utf-8")
    if "def pr_comment_slug" not in src:
        pytest.skip("the Review tab does not link to PR comments yet")
    import importlib
    import sys as _sys
    _sys.path.insert(0, str(HERE))
    review = importlib.import_module("hrbuild.tabs.review")
    for title in ("vet_id is <code>ON DELETE SET NULL</code>, so &quot;none&quot;",
                  "An unknown vetId is a 404, not a quietly unattended visit",
                  "word " * 40, ""):
        assert review.pr_comment_slug(title) == ppc.slug(title)
