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


def test_an_anchor_that_vanished_is_quoted_in_the_summary_at_its_written_lines():
    r = ppc.resolve(c(anchor="return owner;"), diff(), "a" * 40, REPO)
    assert r.mode == "body" and "line" not in r.comment
    assert r.permalink == f"https://github.com/{REPO}/blob/{'a' * 40}/src/A.java#L12"


def test_a_line_outside_every_hunk_is_quoted_in_the_summary_with_a_permalink():
    """GitHub refuses a review comment on a line the diff does not show; a file comment
    loses the line. The summary keeps both: the words and the exact lines."""
    r = ppc.resolve(c(line=30, start_line=28), diff(), None, REPO)
    assert r.mode == "body" and "outside" in r.note
    assert r.permalink == f"https://github.com/{REPO}/blob/{'h' * 40}/src/A.java#L28-L30"


def test_a_file_not_in_the_diff_is_folded_into_the_review_body():
    for path in ("src/Other.java", "gone.txt"):
        r = ppc.resolve(c(path=path), diff(), None, REPO)
        assert r.mode == "body" and r.permalink.endswith(f"/{path}#L12")


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
                self.comments.append({**k, "id": cid, "commit_id": p["commit_id"],
                                      "html_url": f"u/pull/7#discussion_r{cid}"})
        elif call.method == "POST":
            cid = self._next()
            self.comments.append({**p, "id": cid, "html_url": f"u/pull/7#discussion_r{cid}"})
        elif call.method == "PATCH":
            cid = int(call.path.rsplit("/", 1)[1])
            next(x for x in self.comments if x["id"] == cid)["body"] = p["body"]
        elif call.method == "PUT":
            rid = int(call.path.rsplit("/", 1)[1])
            next(x for x in self.reviews if x["id"] == rid)["body"] = p["body"]
        elif call.method == "DELETE":
            cid = int(call.path.rsplit("/", 1)[1])
            self.comments = [x for x in self.comments if x["id"] != cid]
        return {}


def push(gh: FakeGh, payload: dict, tmp: Path, d: ppc.Diff | None = None,
         owned: set[int] | None = None) -> tuple[ppc.Plan, dict]:
    p = ppc.plan(payload, d or diff(), REPO, PR, gh.list("x/comments"), gh.list("x/reviews"),
                 owned)
    rec = ppc.execute(p, gh, REPO, PR, tmp / "pr-comments.posted.json", {"pr": PR},
                      sleep=lambda _s: None)
    return p, rec


PAYLOAD = {"event": "COMMENT", "body": "Review record.", "comments": [
    c(title="Keep the vet", anchor="return vet;"),
    c(title="Constructor too long", line=30),
    c(title="Manual is stale", path="docs/manual.md"),
]}


def test_first_push_posts_one_review_with_the_rest_quoted_in_its_body(tmp_path):
    gh = FakeGh()
    p, rec = push(gh, PAYLOAD, tmp_path)
    kinds = [(x.method, x.path.split("/")[-1]) for x in gh.sent]
    assert kinds == [("POST", "reviews")]
    review = gh.sent[-1].payload
    assert review["commit_id"] == "h" * 40 and len(review["comments"]) == 1
    assert "docs/manual.md" in review["body"] and ppc.REVIEW_MARKER in review["body"]
    # Each one outside the diff, with a permalink alone on its line (GitHub embeds it).
    assert f"\nhttps://github.com/{REPO}/blob/{'h' * 40}/src/A.java#L30" in review["body"]
    assert "discussion_r" in rec["comments"]["I:keep-the-vet"]["html_url"]
    for cid in ("I:manual-is-stale", "I:constructor-too-long"):
        assert rec["comments"][cid]["html_url"] == rec["review_url"]
    assert rec["comments"]["I:constructor-too-long"]["permalink"].endswith("#L30")
    assert json.loads((tmp_path / "pr-comments.posted.json").read_text()) == rec


def test_second_push_of_the_same_payload_sends_nothing(tmp_path):
    gh = FakeGh()
    push(gh, PAYLOAD, tmp_path)
    gh.sent.clear()
    p, _ = push(gh, PAYLOAD, tmp_path)
    assert gh.sent == []
    assert len(gh.comments) == 1 and len(gh.reviews) == 1


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
    # An auto-fix is a note on its fix commit: the commit is recorded, the words are the item's.
    assert by["F:return-the-vet"]["body"].startswith("**Auto-fixed** — Return the vet")
    assert "The vet was dropped." in by["F:return-the-vet"]["body"]
    assert by["F:return-the-vet"]["fix_commit"] == _git(root, "rev-parse", "HEAD").strip()
    assert by["A:none-means-none"]["start_line"] == 2
    assert "confidence 0.50" in by["A:none-means-none"]["body"]
    assert "**Open issue** · low" in by["I:rename-the-class"]["body"]
    capsys.readouterr()

    assert ppc.main(["--root", str(root), "--check", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "2 comments: 1 on a line, 0 on a file, 1 folded" in out
    head = _git(root, "rev-parse", "HEAD").strip()
    assert "1 auto-fix: not PR comments — 1 note on its fix commit" in out
    assert f"F:return-the-vet → fix commit {head[:8]} · src/A.java:3" in out


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


def _branch_with_record(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"), _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src/A.java").write_text("class A {\n  Vet vet;\n  Vet vet() { return vet; }\n}\n")
    (root / "src/Other.java").write_text("class Other {}\n")
    (root / "review-points.md").write_text(POINTS)
    _git(root, "add", "."), _git(root, "commit", "-qm", "feature")


def test_a_payload_pinned_to_another_branch_is_rebuilt_or_refused(tmp_path, capsys):
    """Run 6 kept run 5's pr-comments.json, pinned to 0746abc5 of another branch."""
    root = tmp_path
    _branch_with_record(root)
    _git(root, "checkout", "-qb", "other")
    (root / "src/A.java").write_text("class A { int elsewhere; }\n")
    _git(root, "commit", "-qam", "another branch")
    foreign = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "checkout", "-q", "main")
    file = root / ppc.DEFAULT_FILE
    file.parent.mkdir(parents=True)
    stale = {"version": 1, "commit_id": foreign, "event": "COMMENT", "body": "x",
             "comments": [{"pile": "ignored", "title": "t", "path": "src/A.java",
                           "line": 1, "side": "RIGHT", "body": "b"}]}
    file.write_text(json.dumps(stale))
    assert "not on this branch" in ppc.stale_reason(root, stale)
    # Sending it is refused: its lines were written for code this branch never had.
    assert ppc.main(["--root", str(root), "--check"]) == 2
    assert "is not this branch's" in capsys.readouterr().err
    # --drop-stale rebuilds it from the record, at a commit of this branch.
    assert ppc.main(["--root", str(root), "--drop-stale"]) == 0
    assert "rebuilt from review-points.md" in capsys.readouterr().out
    fresh = json.loads(file.read_text())
    assert fresh["commit_id"] == _git(root, "rev-parse", "HEAD").strip()
    assert ppc.stale_reason(root, fresh) is None
    assert ppc.main(["--root", str(root), "--drop-stale"]) == 0
    assert "current" in capsys.readouterr().out
    # With no record to rebuild it from, it is dropped.
    file.write_text(json.dumps(stale))
    (root / "review-points.md").unlink()
    assert ppc.main(["--root", str(root), "--drop-stale"]) == 0
    assert not file.exists() and "dropped" in capsys.readouterr().out


# ── visit-has-vet: comments two lines off the page, one never on the PR ─────────────
#
# The payload was written against `ce56d912`, an earlier round's review commit (still an
# ancestor after the revert), so its lines and anchor texts were that round's; the push
# then chased those texts on the PR head. `R__seed.sql:142-145` went up as 144-147,
# `VisitRestController.java:85` as 83, and `combo.component.ts:57` — a file the PR never
# touched — into the body of a three-week-old review of another round.

def test_lines_are_carried_from_the_payloads_commit_to_the_pr_head():
    """A retouch after the review commit adds two lines above the ref: the comment goes on
    the lines the ref names *now*, and `commit_id` is the head those lines are lines of."""
    def carrier(path, line, frm):
        assert frm == "r" * 40
        return line + 2, True
    d = ppc.Diff(head="h" * 40, base="b" * 40, files={"src/A.java": [(1, 20)]},
                 reader=lambda p: A_LINES, carrier=carrier)
    r = ppc.resolve(c(line=10, start_line=8, anchor="return vet;"), d, "r" * 40, REPO)
    assert r.mode == "line" and (r.comment["start_line"], r.comment["line"]) == (10, 12)
    assert "carried src/A.java:8-10 from rrrrrrrr to 10-12" in r.note
    p = ppc.plan({"commit_id": "r" * 40, "comments": [c(line=10, anchor="return vet;")]},
                 d, REPO, PR, [], [])
    assert p.calls[-1].payload["commit_id"] == "h" * 40
    assert p.calls[-1].payload["comments"][0]["line"] == 12


def test_the_diff_wins_over_an_anchor_text_that_sits_elsewhere():
    """The anchor text is a cross-check. Where the diff placed the line, a copy of the old
    text two lines away is not a reason to move it there."""
    d = ppc.Diff(head="h" * 40, base="b" * 40, files={"src/A.java": [(1, 20)]},
                 reader=lambda p: A_LINES, carrier=lambda p, n, f: (n, True))
    r = ppc.resolve(c(line=10, anchor="return vet;"), d, "r" * 40, REPO)
    assert r.comment["line"] == 10 and "rewritten" in r.note


def test_a_line_the_diff_removed_is_quoted_at_the_commit_it_was_written_against():
    d = ppc.Diff(head="h" * 40, base="b" * 40, files={"src/A.java": [(1, 20)]},
                 reader=lambda p: A_LINES, carrier=lambda p, n, f: (None, True))
    r = ppc.resolve(c(line=10, anchor="gone();"), d, "r" * 40, REPO)
    assert r.mode == "body"
    assert r.permalink == f"https://github.com/{REPO}/blob/{'r' * 40}/src/A.java#L10"


def _owned_push(gh, tmp):
    p, rec = push(gh, PAYLOAD, tmp)
    return p, rec, ppc.owned_ids(rec)


def test_a_comment_this_pipeline_put_on_the_wrong_lines_is_deleted_and_posted_again(tmp_path):
    gh = FakeGh()
    _, _, owned = _owned_push(gh, tmp_path)
    wrong = next(x for x in gh.comments if "hr:I:keep-the-vet" in x["body"])
    wrong["line"] = 9                                   # where the old push put it
    gh.sent.clear()
    p, rec = push(gh, PAYLOAD, tmp_path, owned=owned)
    kinds = [(x.method, x.path.split("/")[-1]) for x in gh.sent]
    # The replacement first, the delete last: nothing is lost if GitHub refuses the post.
    assert kinds == [("POST", "reviews"), ("DELETE", str(wrong["id"]))]
    assert [k["line"] for k in gh.sent[0].payload["comments"]] == [12]
    assert [x["line"] for x in gh.comments if "hr:I:keep-the-vet" in x["body"]] == [12]
    assert rec["comments"]["I:keep-the-vet"]["line"] == 12
    assert rec["comments"]["I:keep-the-vet"]["commit_id"] == "h" * 40


def test_a_comment_the_record_does_not_vouch_for_is_never_deleted(tmp_path):
    gh = FakeGh()
    gh.comments.append({"id": 5, "path": "src/A.java", "line": 9, "html_url": "u/x",
                        "body": "someone else's\n\n<!-- hr:I:keep-the-vet -->"})
    p, _ = push(gh, PAYLOAD, tmp_path, owned=set())
    assert not [x for x in gh.sent if x.method == "DELETE"]
    assert any("left where it is" in n for n in p.notes)


def test_a_line_comment_now_outside_the_diff_moves_into_the_summary(tmp_path):
    gh = FakeGh()
    gh.comments.append({"id": 5, "path": "src/A.java", "subject_type": "file", "line": 1,
                        "html_url": "u/pull/7#discussion_r5",
                        "body": "x\n\n<!-- hr:I:constructor-too-long -->"})
    p, rec = push(gh, PAYLOAD, tmp_path, owned={5})
    assert ("DELETE", "5") in [(x.method, x.path.split("/")[-1]) for x in gh.sent]
    assert rec["comments"]["I:constructor-too-long"]["html_url"] == rec["review_url"]


def test_an_earlier_rounds_summary_is_left_alone_and_this_round_gets_its_own(tmp_path):
    """The old push found `<!-- hr:review -->` on a review of the previous round and
    rewrote its body; the page linked an item of this round to that old review."""
    gh = FakeGh()
    gh.reviews.append({"id": 1, "body": "round 1\n\n<!-- hr:review -->", "html_url": "u/r/1"})
    payload = {**PAYLOAD, "commit_id": "c" * 40}
    p, rec = push(gh, payload, tmp_path)
    assert not [x for x in gh.sent if x.method == "PUT"]
    assert gh.reviews[0]["body"] == "round 1\n\n<!-- hr:review -->"
    assert "<!-- hr:review:cccccccccccc -->" in gh.sent[-1].payload["body"]
    assert rec["review_url"] != "u/r/1"
    assert rec["comments"]["I:manual-is-stale"]["html_url"] == rec["review_url"]
    gh.sent.clear()
    push(gh, payload, tmp_path)
    assert gh.sent == [], "the second push of a round finds its own summary"


def test_a_payload_older_than_the_record_is_stale_and_rebuilt_at_the_records_commit(
        tmp_path, capsys):
    """visit-has-vet: `pr-comments.json` at `ce56d912` (round 1's review commit),
    `review-points.md` re-recorded at `be762955`, then `5f84b2cd` moved only its `base:`.
    The payload is refused and rebuilt at be762955 — not at 5f84b2cd, whose lines would
    be the same numbers read against a tree two commits later."""
    root = tmp_path
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"), _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src/A.java").write_text("class A {\n  Old old;\n}\n")
    (root / "review-points.md").write_text(POINTS.replace("src/A.java:3", "src/A.java:2"))
    _git(root, "add", "."), _git(root, "commit", "-qm", "round 1")
    old = _git(root, "rev-parse", "HEAD").strip()
    (root / "src/A.java").write_text("class A {\n  Vet vet;\n  Vet vet() { return vet; }\n}\n")
    (root / "review-points.md").write_text(POINTS)
    _git(root, "commit", "-qam", "round 2")
    rec = _git(root, "rev-parse", "HEAD").strip()
    (root / "review-points.md").write_text(POINTS.replace("reviewers:", "base: abc\nreviewers:"))
    _git(root, "commit", "-qam", "review-points: base moves")
    file = root / ppc.DEFAULT_FILE
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({**ppc.from_review_points(root, "review-points.md", old)}))
    assert ppc.anchor_commit(root, "review-points.md") == rec
    assert "older than" in ppc.stale_reason(root, json.loads(file.read_text()))
    assert ppc.main(["--root", str(root), "--check"]) == 2
    capsys.readouterr()
    assert ppc.main(["--root", str(root), "--drop-stale"]) == 0
    fresh = json.loads(file.read_text())
    assert fresh["commit_id"] == rec
    by = {x["hr_id"]: x for x in fresh["comments"]}
    assert by["F:return-the-vet"]["anchor"] == "Vet vet() { return vet; }"
    # The auto-fix's note goes on the commit that recorded it, not on the later one.
    assert by["F:return-the-vet"]["fix_commit"] == rec


def test_a_round_without_its_own_summary_gathers_its_comments_into_one_review(tmp_path):
    """visit-has-vet's first push left its line comments in a review whose body said only
    "8 more from the same review record" and its summary on another round's review. The
    push that gives the round its summary posts them again under it, then deletes them."""
    gh = FakeGh()
    gh.comments.append({"id": 5, "path": "src/A.java", "line": 12, "side": "RIGHT",
                        "html_url": "u/pull/7#discussion_r5",
                        "body": PAYLOAD["comments"][0]["body"] + "\n\n<!-- hr:I:keep-the-vet -->"})
    p, rec = push(gh, {**PAYLOAD, "commit_id": "c" * 40}, tmp_path, owned={5})
    kinds = [(x.method, x.path.split("/")[-1]) for x in gh.sent]
    assert kinds == [("POST", "reviews"), ("DELETE", "5")]
    assert [k["line"] for k in gh.sent[0].payload["comments"]] == [12]
    assert "discussion_r5" not in rec["comments"]["I:keep-the-vet"]["html_url"]


# -------------------------------------------------------------------- PR #51: the 504

def many(n: int) -> dict:
    """`n` line comments, all on a line the diff shows — PR #51 had 41."""
    return {"event": "COMMENT", "body": "Review record.", "commit_id": "c" * 40,
            "comments": [c(title=f"Item {i}", line=12) for i in range(n)]}


def replanner(gh: FakeGh, payload: dict):
    return lambda: ppc.plan(payload, diff(), REPO, PR, gh.list("x/comments"),
                            gh.list("x/reviews"))


def run(gh: FakeGh, payload: dict, tmp: Path) -> dict:
    p = ppc.plan(payload, diff(), REPO, PR, gh.list("x/comments"), gh.list("x/reviews"))
    return ppc.execute(p, gh, REPO, PR, tmp / "pr-comments.posted.json", {"pr": PR},
                       replan=replanner(gh, payload), sleep=lambda _s: None)


def test_many_comments_go_out_in_reviews_of_batch_the_first_carrying_the_summary(tmp_path):
    gh = FakeGh()
    rec = run(gh, many(41), tmp_path)
    posts = [x for x in gh.sent if x.method == "POST"]
    assert [len(x.payload["comments"]) for x in posts] == [15, 15, 11]
    assert ppc.review_marker("c" * 40) in posts[0].payload["body"]
    assert all(ppc.more_marker("c" * 40) in x.payload["body"] for x in posts[1:])
    assert all(ppc.review_marker("c" * 40) not in x.payload["body"] for x in posts[1:])
    assert rec["status"] == "complete" and rec["present"] == rec["expected"] == 41
    gh.sent.clear()
    rec = run(gh, many(41), tmp_path)
    assert gh.sent == [] and len(gh.comments) == 41
    assert rec["message"] == "All 41 comments were already on GitHub PR #7."


class SlowGh(FakeGh):
    """GitHub as PR #51 met it: the review call answers 504 after creating the review, and
    its comments appear a few at a time over the next polls."""

    def __init__(self, creates: bool = True, per_poll: int = 4):
        super().__init__()
        self.creates, self.per_poll, self.hidden, self.failed = creates, per_poll, [], False

    def list(self, path: str) -> list[dict]:
        if path.endswith("/comments") and self.hidden:
            self.comments += self.hidden[:self.per_poll]
            self.hidden = self.hidden[self.per_poll:]
        return super().list(path)

    def send(self, call):
        if call.method == "POST" and call.path.endswith("/reviews") and not self.failed:
            self.failed = True
            if self.creates:
                before = len(self.comments)
                super().send(call)
                self.hidden, self.comments = self.comments[before:], self.comments[:before]
            else:
                self.sent.append(call)
            raise ppc.GhError("gh api -X POST repos/o/r/pulls/7/reviews: gh: We couldn't "
                              "respond to your request in time. (HTTP 504)")
        return super().send(call)


def test_a_504_on_a_review_that_was_created_anyway_posts_nothing_twice(tmp_path):
    gh = SlowGh(creates=True)
    rec = run(gh, many(41), tmp_path)
    marks = [ppc.MARKER.search(x["body"]).group(1) for x in gh.comments]
    assert len(marks) == len(set(marks)) == 41, "every comment exactly once"
    assert sum(ppc.review_marker("c" * 40) in r["body"] for r in gh.reviews) == 1
    # The first batch was not sent again: only the two remaining batches followed it.
    assert [len(x.payload["comments"]) for x in gh.sent if x.method == "POST"] == [15, 15, 11]
    assert rec["status"] == "complete" and rec["duplicates"] == []


def test_a_504_on_a_review_that_was_never_created_is_sent_again(tmp_path):
    gh = SlowGh(creates=False)
    rec = run(gh, many(20), tmp_path)
    posts = [len(x.payload["comments"]) for x in gh.sent if x.method == "POST"]
    assert posts == [15, 15, 5]           # the lost first batch, then both for real
    assert len(gh.comments) == 20 and rec["status"] == "complete"


def test_a_refusal_stops_the_push_and_records_what_is_there_in_plain_words(tmp_path):
    class Refuses(FakeGh):
        def send(self, call):
            if call.method == "POST" and len(self.reviews) == 1:
                raise ppc.GhError("gh api -X POST repos/o/r/pulls/7/reviews: Validation "
                                  "Failed (HTTP 422)")
            return super().send(call)
    gh = Refuses()
    rec = run(gh, many(20), tmp_path)
    assert rec["status"] == "partial" and rec["present"] == 15 and len(rec["missing"]) == 5
    assert rec["message"].startswith("GitHub refused a comment")
    assert "15 of 20 posted; click Retry to post the remaining 5." in rec["message"]
    assert "gh api" not in rec["message"]
    on_disk = json.loads((tmp_path / "pr-comments.posted.json").read_text())
    assert on_disk["status"] == "partial"
    # Retry: only the five missing go out, in a review that continues the summary.
    good = FakeGh()
    good.comments, good.reviews, good._id = gh.comments, gh.reviews, gh._id
    rec = run(good, many(20), tmp_path)
    assert [len(x.payload["comments"]) for x in good.sent] == [5]
    assert ppc.more_marker("c" * 40) in good.sent[0].payload["body"]
    assert rec["status"] == "complete" and len(good.comments) == 20


def test_the_dry_run_preview_lists_readable_comments_not_calls():
    payload = many(3)
    pv = ppc.preview(ppc.plan(payload, diff(), REPO, PR, [], []), PR, REPO, "u", "victor")
    assert (pv["total"], pv["already"], len(pv["items"]), pv["user"]) == (3, 0, 3, "victor")
    item = pv["items"][0]
    assert item["where"] == "src/A.java:12" and "<!--" not in item["text"]
    assert "gh api" not in json.dumps(pv)


def test_settle_waits_until_the_count_stops_moving():
    gh = SlowGh(per_poll=1)
    gh.hidden = [{"id": i, "body": f"x <!-- hr:I:item-{i} -->"} for i in range(3)]
    call = ppc.Call("POST", "r/reviews", {}, "", expect=[f"I:item-{i}" for i in range(5)])
    waits = []
    assert ppc.settle(gh, REPO, PR, call, sleep=waits.append) == 3
    assert len(waits) == 5        # 1, 2, 3, then 3 twice more: unmoved for two polls


@pytest.mark.parametrize("pile, cid, body, lead", [
    ("assumption", "A:x", "**Assumption** — none means none", "🤖💭🤷‍♂️ **Assumption**"),
    ("ignored", "I:x", "🔴 **Open issue** — keep the vet", "🤖⚔️🤖❌ **Open issue**"),
    ("fixed", "F:x", "🛠 **Auto-fixed** — return the vet", "🤖⚔️🤖✅ **Auto-fixed**"),
    ("", "I:x", "an agent-written body with no pile", "🤖⚔️🤖❌ an agent-written"),
])
def test_every_pushed_comment_opens_with_its_piles_prefix(pile, cid, body, lead):
    """Whoever wrote the body, the PR's reader sees who is speaking first: the old single
    emoji is replaced, an unlabelled payload item takes its pile from its id, and a body
    already prefixed is left as it is."""
    c = {"hr_id": cid, "pile": pile, "title": "x", "path": "elsewhere.txt", "body": body}
    out = ppc.prefixed(c["body"], c["pile"] or ppc.PILE_OF_LETTER[cid[:1]])
    assert out.startswith(lead)
    assert ppc.prefixed(out, pile or ppc.PILE_OF_LETTER[cid[:1]]) == out


def test_resolve_puts_the_prefix_on_what_is_sent():
    """The prefix is applied where every comment passes on its way to GitHub — on the line
    and in the summary alike — not only in the bodies `--from-review-points` writes."""
    on_line = ppc.resolve(c(), diff())
    assert on_line.comment["body"].startswith("🤖⚔️🤖❌ **Open issue** — keep the vet")
    folded = ppc.resolve(c(pile="assumption", path="nowhere.txt", body="**Assumption** — x"),
                         diff())
    assert folded.mode == "body"
    assert folded.comment["body"].startswith("🤖💭🤷‍♂️ **Assumption** — x")


def test_the_review_summary_carries_the_merge_risk_with_its_blast_circle(tmp_path):
    """The page's MERGE RISK line, on the PR: read from the content.json beside the payload,
    the size in words after the circle because GitHub has no hover."""
    (tmp_path / "content.json").write_text(json.dumps({"mergeRisk": {
        "door": "two-way", "blast": "visits API", "blastSize": 1,
        "why": "a nullable column a revert stops using"}}), encoding="utf-8")
    payload = ppc.attach_merge_risk({"body": "**Review record**: 1 auto-fixed.",
                                     "comments": []}, tmp_path / "pr-comments.json")
    body = ppc.review_body(payload, [])
    assert body.startswith("**Review record**: 1 auto-fixed.\n\n**Merge risk:** ✅ Two-way "
                           "door · blast radius: ◔ **visits API** (about a quarter of the "
                           "system) — a nullable column a revert stops using\n\n<!-- hr:review")
    # The payload's own wins; no door, no line; no size, no circle.
    assert ppc.attach_merge_risk({"mergeRisk": {"door": "one-way"}},
                                 tmp_path / "x.json")["mergeRisk"] == {"door": "one-way"}
    assert ppc.merge_risk_md({"door": "maybe", "blast": "x"}) == ""
    assert ppc.merge_risk_md({"door": "one-way", "blast": "auth"}) == \
        "**Merge risk:** ⚠️ One-way door · blast radius: **auth**"


# ------------------------------------------------- the merge risk in the PR's description

FOOTER = "🤖 Generated with [Claude Code](https://claude.com/claude-code) · Opus 5.5"
RISK = {"door": "two-way", "blast": "visits API", "blastSize": 1, "why": "a revert stops it"}
LINE = ("**Merge risk:** ✅ Two-way door · blast radius: ◔ **visits API** (about a quarter "
        "of the system) — a revert stops it")


def test_the_merge_risk_goes_just_above_the_attribution_footer():
    out = ppc.with_merge_risk(f"Adds a vet.\r\n\r\n{FOOTER}", LINE)
    assert out == (f"Adds a vet.\n\n<!-- hr:merge-risk -->\n{LINE}\n<!-- /hr:merge-risk -->"
                   f"\n\n{FOOTER}")
    assert out.rstrip().endswith(FOOTER), "the footer stays the last line"


def test_a_re_publish_replaces_the_block_and_never_adds_a_second():
    once = ppc.with_merge_risk(f"Adds a vet.\n\n{FOOTER}", LINE)
    assert ppc.with_merge_risk(once, LINE) == once
    moved = ppc.with_merge_risk(once, LINE.replace("◔", "◑"))
    assert moved.count("<!-- hr:merge-risk -->") == 1 and "◑" in moved and "◔" not in moved
    assert moved.endswith(FOOTER) and moved.startswith("Adds a vet.\n\n<!--")


def test_without_a_footer_the_block_closes_the_body_and_without_a_line_nothing_changes():
    out = ppc.with_merge_risk("Adds a vet.\n", LINE)
    assert out == f"Adds a vet.\n\n<!-- hr:merge-risk -->\n{LINE}\n<!-- /hr:merge-risk -->"
    assert ppc.with_merge_risk(out, LINE) == out
    assert ppc.with_merge_risk("", LINE) == f"<!-- hr:merge-risk -->\n{LINE}\n<!-- /hr:merge-risk -->"
    assert ppc.with_merge_risk("Adds a vet.", "") == "Adds a vet."


class FakeForge:
    def __init__(self, body="", fail=None):
        self.body, self.fail, self.edits = body, fail, []

    def pr_body(self, number, slug, root):
        return {"number": number, "body": self.body, "url": "u"}

    def update_pr_body(self, number, body, slug, root):
        if self.fail:
            raise ppc.GhError(self.fail)
        self.edits.append((number, slug, body))
        self.body = body

    @staticmethod
    def update_pr_body_shell(number, slug, path):
        return f"edit {number} {slug} {path}"


def test_publishing_writes_the_description_once_and_says_when_it_is_already_there(tmp_path):
    f = FakeForge(f"Adds a vet.\n\n{FOOTER}")
    assert ppc.write_description(f, tmp_path, "o/r", 49, RISK) == {"fate": "new", "text": LINE}
    assert f.edits[0][:2] == (49, "o/r") and LINE in f.edits[0][2]
    assert ppc.write_description(f, tmp_path, "o/r", 49, RISK) == {"fate": "same", "text": LINE}
    assert len(f.edits) == 1, "nothing sent when it already says so"
    f.body += "\n"                     # what `gh pr edit --body-file` leaves of a file
    assert ppc.write_description(f, tmp_path, "o/r", 49, RISK)["fate"] == "same"
    assert ppc.write_description(f, tmp_path, "o/r", 49, {**RISK, "door": "one-way"})["fate"] \
        == "update"
    assert ppc.write_description(f, tmp_path, "o/r", 49, {"door": "?"}) == {}
    said = ppc.write_description(FakeForge("x", fail="gh pr edit 49: HTTP 403"), tmp_path,
                                 "o/r", 49, RISK)
    assert set(said) == {"error"}


def test_description_only_dry_run_prints_the_new_body_and_writes_nothing(tmp_path, monkeypatch,
                                                                        capsys):
    (tmp_path / ".human-review").mkdir()
    (tmp_path / ".human-review" / "content.json").write_text(json.dumps({"mergeRisk": RISK}))
    f = FakeForge(f"Adds a vet.\n\n{FOOTER}")
    monkeypatch.setattr(ppc, "FORGE", f)
    assert ppc.main(["--root", str(tmp_path), "--description-only", "--dry-run",
                     "--pr", "49", "--repo", "o/r"]) == 0
    got = capsys.readouterr()
    assert got.out == ppc.with_merge_risk(f"Adds a vet.\n\n{FOOTER}", LINE) + "\n"
    assert "edit 49 o/r" in got.err and not f.edits
    assert ppc.main(["--root", str(tmp_path), "--description-only", "--pr", "49",
                     "--repo", "o/r"]) == 0
    assert len(f.edits) == 1


def test_the_pr_and_the_page_draw_the_same_doors_and_circles():
    sys.path.insert(0, str(HERE))
    from hrbuild.tabs import review
    assert ppc.BLAST_GLYPHS == review.BLAST_GLYPHS and ppc.BLAST_SIZES == review.BLAST_SIZES
    assert ppc.MERGE_DOORS == {k: f"{review.MERGE_DOOR_MARKS[k]} {v}"
                               for k, v in review.MERGE_DOORS.items()}


# ------------------------------------------------- auto-fixes: notes on their fix commits
# Victor, 30 Oct 2026: an auto-fixed item is fixed by a commit already, so it is never a PR
# comment. Its note goes on that commit, on the fixed line; the open issues stay on the PR.

def _fix_repo(root: Path) -> tuple[str, str]:
    """An implementation commit, then the `[auto-fix]` commit that fixed `src/A.java:3` and
    recorded `review-points.md`."""
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"), _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src/A.java").write_text("class A {\n  Vet vet;\n}\n")
    (root / "src/B.java").write_text("class B {}\n")
    _git(root, "add", "."), _git(root, "commit", "-qm", "impl")
    impl = _git(root, "rev-parse", "HEAD").strip()
    (root / "src/A.java").write_text("class A {\n  Vet vet;\n  Vet vet() { return vet; }\n}\n")
    (root / "review-points.md").write_text(
        f"---\nimplementation: {impl}\nfixed-in: HEAD\n---\n\n## Fixed\n\n"
        "### Return the vet\n- file: src/A.java:3\n- source: tests reviewer\n"
        "- observation: The getter dropped the vet.\n- fix: it returns the field.\n")
    _git(root, "add", "."), _git(root, "commit", "-qm", "[auto-fix] return the vet")
    return impl, _git(root, "rev-parse", "HEAD").strip()


def test_commit_positions_count_like_github(tmp_path):
    """GitHub's `position`: 1 is the line under the file's first `@@`, and every later line
    counts — the second hunk's header included."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q"), _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    lines = [f"l{i}" for i in range(1, 31)]
    (root / "f.txt").write_text("\n".join(lines) + "\n")
    _git(root, "add", "."), _git(root, "commit", "-qm", "one")
    lines[1], lines[25] = "two", "twenty-six"
    (root / "f.txt").write_text("\n".join(lines) + "\n")
    _git(root, "commit", "-qam", "two")
    sha = _git(root, "rev-parse", "HEAD").strip()
    at, added = ppc.commit_positions(root, sha, "f.txt")
    assert added == [2, 26]
    body = _git(root, "diff", f"{sha}^1", sha, "--", "f.txt").splitlines()
    first = next(i for i, ln in enumerate(body) if ln.startswith("@@"))
    for line, pos in at.items():
        assert body[first + pos][1:] == lines[line - 1], (line, pos)
    assert at[2] == 3 and at[26] == at[5] + 2 + 4     # hunk 1 ends at 5; `@@` counts once
    assert ppc.commit_positions(root, sha, "nothing.txt") == ({}, [])


class FixGh(FakeGh):
    """`FakeGh` with each commit's comments too, as GitHub keeps them apart from the PR's."""

    def __init__(self):
        super().__init__()
        self.on_commit: dict[str, list[dict]] = {}

    def list(self, path: str) -> list[dict]:
        if "/commits/" in path:
            return list(self.on_commit.get(path.split("/commits/")[1].split("/")[0], []))
        return super().list(path)

    def send(self, call: ppc.Call) -> dict:
        if "/commits/" in call.path:
            self.sent.append(call)
            sha = call.path.split("/commits/")[1].split("/")[0]
            cid = self._next()
            self.on_commit.setdefault(sha, []).append(
                {**call.payload, "id": cid,
                 "html_url": f"https://github.com/{REPO}/commit/{sha}#commitcomment-{cid}"})
            return {}
        if call.path.startswith(f"repos/{REPO}/comments/"):
            self.sent.append(call)
            cid = int(call.path.rsplit("/", 1)[1])
            for sha, cs in self.on_commit.items():
                if call.method == "DELETE":
                    self.on_commit[sha] = [x for x in cs if x["id"] != cid]
                for x in cs:
                    if x["id"] == cid:
                        x["body"] = call.payload["body"]
            return {}
        return super().send(call)


def _fix_payload(fix: str) -> dict:
    return {"event": "COMMENT", "body": "Review record.", "commit_id": fix, "comments": [
        c(pile="fixed", title="Return the vet", path="src/A.java", line=3,
          body=f"🛠 **Auto-fixed** in {fix[:8]} — Return the vet"),
        c(title="Keep the vet", anchor="return vet;")]}


def test_an_auto_fix_is_a_note_on_its_fix_commit_never_a_pr_comment(tmp_path):
    root = tmp_path / "repo"
    impl, fix = _fix_repo(root)
    payload = _fix_payload(fix)
    fixes = ppc.resolve_fixes(payload, root, REPO)
    assert [(n.cid, n.sha, n.line) for n in fixes] == [("F:return-the-vet", fix, 3)]
    # On the fixed line of the commit's own diff: `@@ -1,3 +1,4 @@`, then lines 1, 2, 3.
    assert fixes[0].comment["path"] == "src/A.java" and fixes[0].comment["position"] == 3
    body = fixes[0].comment["body"]
    assert body.startswith("🤖⚔️🤖✅ **Auto-fixed** — Return the vet")
    assert "**Reviewer:** The getter dropped the vet." in body and "**Fix:** it returns" in body
    assert body.endswith("<!-- hr:F:return-the-vet -->")

    gh = FixGh()
    # What an older push left: this auto-fix as a PR comment, recorded as this pipeline's.
    gh.comments.append({"id": 5, "path": "src/A.java", "line": 3, "html_url":
                        f"https://github.com/{REPO}/pull/7#discussion_r5",
                        "body": "old <!-- hr:F:return-the-vet -->"})
    posted = tmp_path / "pr-comments.posted.json"
    posted.write_text(json.dumps({"pr": PR, "comments": {"F:return-the-vet": {
        "pile": "fixed", "mode": "line", "html_url": gh.comments[0]["html_url"]}}}))
    owned = ppc.owned_ids(json.loads(posted.read_text()))
    p = ppc.plan(payload, diff(), REPO, PR, gh.list("x/comments"), [], owned, fixes,
                 {fix: gh.list(f"repos/{REPO}/commits/{fix}/comments")})
    assert [r.cid for r in p.resolved] == ["I:keep-the-vet"]
    notes = [x for x in p.calls if "/commits/" in x.path]
    assert [(x.method, x.path) for x in notes] == [("POST", f"repos/{REPO}/commits/{fix}/comments")]
    assert not any(x.method == "DELETE" for x in p.calls), "the old PR comment stays"
    review = next(x for x in p.calls if x.path.endswith("/reviews"))
    assert all("hr:F:" not in k["body"] for k in review.payload["comments"])
    assert f"its fix commit, not here: [`{fix[:8]}`](https://github.com/{REPO}/commit/{fix})" \
        in review.payload["body"]
    pv = ppc.preview(p, PR, REPO, None, None)
    note = next(i for i in pv["items"] if i["target"] == "commit")
    assert note["where"] == f"fix commit {fix[:8]} · src/A.java:3" and note["commit"] == fix

    rec = ppc.execute(p, gh, REPO, PR, posted, {"pr": PR}, sleep=lambda _s: None)
    entry = rec["comments"]["F:return-the-vet"]
    assert entry["mode"] == "commit" and entry["commit_id"] == fix and entry["line"] == 3
    assert entry["html_url"].startswith(f"https://github.com/{REPO}/commit/{fix}#commitcomment-")
    assert rec["status"] == "complete" and f"notes on fix commit {fix[:8]}" in rec["message"]
    assert rec["retired"]["F:return-the-vet"]["html_url"].endswith("#discussion_r5")
    assert 5 in ppc.owned_ids(rec), "the retired comment is still known as this pipeline's"
    assert any(x["id"] == 5 for x in gh.comments)

    # A second push: the note is there as it should be, nothing is sent.
    gh.sent.clear()
    p2 = ppc.plan(payload, diff(), REPO, PR, gh.list("x/comments"), gh.list("x/reviews"),
                  ppc.owned_ids(rec) | ppc.owned_note_ids(rec), fixes,
                  {fix: gh.list(f"repos/{REPO}/commits/{fix}/comments")})
    assert p2.fate["F:return-the-vet"] == "same"
    assert not [x for x in p2.calls if "/commits/" in x.path or "/comments/" in x.path]


def test_an_auto_fix_no_commit_names_is_said_and_not_posted(tmp_path):
    root = tmp_path / "repo"
    impl, fix = _fix_repo(root)
    payload = _fix_payload(fix)
    payload["comments"][0]["title"] = "Not in the record"
    payload["comments"][0]["path"] = "elsewhere.txt"
    (root / "review-points.md").unlink()
    fixes = ppc.resolve_fixes(payload, root, REPO)
    assert fixes[0].sha is None
    p = ppc.plan(payload, diff(), REPO, PR, [], [], set(), fixes, {})
    assert not [x for x in p.calls if "/commits/" in x.path]
    assert any("not posted" in n for n in p.notes)
    assert all("hr:F:" not in str(x.payload) for x in p.calls)
