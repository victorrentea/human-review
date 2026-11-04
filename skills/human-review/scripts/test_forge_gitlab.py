#!/usr/bin/env python3
"""GitLab as a forge behind `hrbuild/shared/forge.py` (issue #4): its remotes are recognised,
its web links have its own shape, its REST answers come back in the words `gh` uses, and
`push-pr-comments.py`'s GitHub-worded plan lands on a merge request.

No GitLab is reached: `GitLabForge.glab` is the one network point, and a fake stands in for
it. The answers are copied from a real GitLab CE 19.5 through glab 1.128, the same instance
the whole flow was first run against end to end, so these pin what was seen there: in
particular the position a line comment needs, which only a real server refuses.

Run with:  python3 -m pytest test_forge_gitlab.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from hrbuild.shared import forge  # noqa: E402
from hrbuild.shared.forge import (BitbucketServerForge, GitHubForge, GitLabForge,  # noqa: E402
                                  GitLabPrApi, _GlDiff)

ROOT = "https://gitlab.com/acme/shop/petclinic"
PROJECT = "projects/acme%2Fshop%2Fpetclinic"
MR = f"{PROJECT}/merge_requests/1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GITLAB_HOST", "GL_HOST", "BITBUCKET_URL"):
        monkeypatch.delenv(k, raising=False)
    forge.forge_for.cache_clear()
    yield
    forge.forge_for.cache_clear()


# ---- recognising its remotes and URLs ---------------------------------------------------

@pytest.mark.parametrize("remote, root", [
    ("https://gitlab.com/acme/shop/petclinic.git", ROOT),
    ("https://oauth2:tok@gitlab.com/acme/shop/petclinic", ROOT),
    ("git@gitlab.com:acme/shop/petclinic.git", ROOT),
    ("ssh://git@gitlab.com/acme/shop/petclinic.git", ROOT),
    ("ssh://git@gitlab.com:22/acme/shop/petclinic", ROOT),
    ("https://gitlab.acme.com/shop/petclinic.git", "https://gitlab.acme.com/shop/petclinic"),
    ("git@gitlab-int.acme.com:shop/petclinic.git", "https://gitlab-int.acme.com/shop/petclinic"),
    ("http://gitlab.acme.com:8080/a/b.git", "http://gitlab.acme.com:8080/a/b"),
])
def test_a_gitlab_origin_names_its_web_root(remote, root):
    assert GitLabForge.from_remote(remote) == root
    assert forge.forge_from_any_remote(remote).repo_url == root


def test_an_instance_with_another_name_is_reached_by_GITLAB_HOST(monkeypatch):
    remote = "git@code.acme.com:shop/petclinic.git"
    assert GitLabForge.from_remote(remote) is None
    monkeypatch.setenv("GITLAB_HOST", "code.acme.com")
    assert GitLabForge.from_remote(remote) == "https://code.acme.com/shop/petclinic"
    monkeypatch.setenv("GITLAB_HOST", "http://code.acme.com:8929/")
    assert GitLabForge.from_remote(remote) == "http://code.acme.com:8929/shop/petclinic"


def test_a_relative_url_root_is_not_part_of_the_slug(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "https://acme.com/gitlab")
    root = GitLabForge.from_remote("https://acme.com/gitlab/shop/petclinic.git")
    assert root == "https://acme.com/gitlab/shop/petclinic"
    assert GitLabForge(root).slug == "shop/petclinic"


@pytest.mark.parametrize("remote, kind", [
    ("https://github.com/o/r.git", GitHubForge),
    ("git@github.com:o/r.git", GitHubForge),
    ("https://bitbucket.acme.com/scm/shop/petclinic.git", BitbucketServerForge),
    ("ssh://git@bitbucket.acme.com:7999/shop/petclinic.git", BitbucketServerForge),
])
def test_the_other_forges_keep_their_remotes(remote, kind):
    assert GitLabForge.from_remote(remote) is None
    assert isinstance(forge.forge_from_any_remote(remote), kind)


def test_a_host_that_merely_contains_the_word_is_not_gitlab():
    assert GitLabForge.from_remote("https://notgitlab.com/a/b.git") is None
    assert GitLabForge.from_remote("https://gitlabels.acme.com/a/b.git") is None
    assert forge.forge_from_any_remote("git@code.acme.com:a/b.git") is None


@pytest.mark.parametrize("remote", ["https://gitlab.acme.com/scm/KEY/repo.git",
                                    "ssh://git@gitlab-bb.acme.com:7999/key/repo.git"])
def test_a_bitbucket_shape_beats_a_name_that_only_suggests_gitlab(remote):
    assert isinstance(forge.forge_from_any_remote(remote), BitbucketServerForge)


def test_a_group_called_scm_on_gitlab_com_stays_on_gitlab():
    assert isinstance(forge.forge_from_any_remote("https://gitlab.com/scm/tools/x.git"),
                      GitLabForge)


def test_forge_at_keeps_bitbucket_s_shape_on_a_guessed_host():
    url = "https://gitlab-old.acme.com/projects/K/repos/r"
    assert isinstance(forge.forge_at(url), BitbucketServerForge)


def test_forge_at_finds_the_project_however_deep_its_groups_go():
    assert isinstance(forge.forge_at(ROOT), GitLabForge)
    assert forge.forge_at(ROOT + "/-/merge_requests/7").repo_url == ROOT
    assert forge.forge_at("https://code.acme.com/g/s/r/-/blob/main/A.java").slug == "g/s/r"
    assert isinstance(forge.forge_at("https://ghe.acme.com/o/r"), GitHubForge)
    assert forge.project_of(ROOT) == "acme/shop/petclinic"


def test_a_merge_request_url_is_a_pull_url():
    mr = ROOT + "/-/merge_requests/42"
    assert forge.is_pull_url(mr)
    assert forge.pull_root(mr + "#note_9") == mr
    assert forge.is_comment_url(mr + "#note_9")
    assert not forge.is_comment_url(mr + " ; rm -rf /")
    found, number = forge.find_pull(f"see {mr}/diffs please")
    assert (found.repo_url, found.slug, number) == (ROOT, "acme/shop/petclinic", 42)
    assert forge.is_commit_comment_url(ROOT + "/-/commit/abc1234#note_3")


@pytest.mark.parametrize("url", [
    "https://gitlab.com@evil.example/g/r/-/merge_requests/1",
    "https://gitlab.com/g/$(id)/r/-/merge_requests/1",
    "https://gitlab.com/g/r/-/merge_requests/1\n",
    "javascript://gitlab.com/g/r/-/merge_requests/1",
])
def test_what_a_click_may_open_is_a_plain_url(url):
    assert not GitLabForge.is_comment_url(url)


def test_a_ticket_on_another_forge_is_not_this_forge_s_issue(monkeypatch):
    """Its number on this forge would be some other ticket, read without a word."""
    spec = importlib.util.spec_from_file_location("semcov", HERE / "semcov.py")
    semcov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(semcov)
    monkeypatch.setattr(semcov, "api_forge", lambda: GitHubForge())
    assert semcov._ticket_value(ROOT + "/-/issues/25", "o/r") == (None, "")
    assert semcov._ticket_value("https://github.com/o/r/issues/25", "o/r") == ((25, "o/r"), "")
    monkeypatch.setattr(semcov, "api_forge", lambda: GitLabForge(ROOT))
    assert semcov._ticket_value(ROOT + "/-/issues/25", "") == ((25, "acme/shop/petclinic"), "")
    other = "https://gitlab.acme.com/acme/shop/petclinic/-/issues/25"
    assert semcov._ticket_value(other, "") == (None, "")


@pytest.mark.parametrize("url", [ROOT + "/-/issues/3", ROOT + "/-/work_items/3"])
def test_both_spellings_of_an_issue_url_are_one(url):
    found, number = forge.find_issue(url)
    assert (found.slug, number) == ("acme/shop/petclinic", 3)


# ---- its web links ----------------------------------------------------------------------

def test_web_links_have_gitlab_s_shape():
    f = GitLabForge(ROOT)
    assert f.blob_url("abc", "src/A.java") == f"{ROOT}/-/blob/abc/src/A.java"
    assert f.blob_url("abc", "A.java", 57, 59) == f"{ROOT}/-/blob/abc/A.java#L57-59"
    assert f.blob_url("abc", "A.java", 3) == f"{ROOT}/-/blob/abc/A.java#L3"
    assert f.blob_prefix("abc") + "/A.java" == f.blob_url("abc", "A.java")
    assert f.tree_url("main") == f"{ROOT}/-/tree/main"
    assert f.commit_url("abc") == f"{ROOT}/-/commit/abc"
    assert f.compare_url("main", "x") == f"{ROOT}/-/compare/main...x"
    assert f.issue_url(12) == f"{ROOT}/-/issues/12"
    assert f.name == "GitLab" and f.blob_query("abc") == ""
    assert f.of_slug("other/x").repo_url == "https://gitlab.com/other/x"


def test_the_page_is_stamped_with_gitlab_s_line_spelling(checkout):
    from hrbuild.shared import snippets
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    stamp = snippets.commit_stamp(checkout)
    assert 'data-hr-blob-l="L-"' in stamp and 'data-hr-forge="GitLab"' in stamp
    assert f'data-hr-blob="{ROOT}/-/blob/' in stamp


def test_a_card_s_link_names_gitlab_and_wears_no_octocat():
    from hrbuild.tabs import review
    link = review.gh_comment_link({"_ghUrl": ROOT + "/-/merge_requests/1#note_3",
                                   "_ghForge": "GitLab"})
    assert 'data-tip="Open the PR comment on GitLab"' in link and 'aria-label="On GitLab"' in link
    assert "gh-ico" not in link and "↗" in link
    plain = review.gh_comment_link({"_ghUrl": "https://github.com/o/r/pull/1#discussion_r3"})
    assert "gh-ico" in plain and "Open the PR comment on GitHub" in plain


def test_an_mr_is_not_offered_to_github_s_vs_code_extension():
    from hrbuild.tabs import review
    assert review.vscode_pr_link(ROOT + "/-/merge_requests/1#note_3") == ""
    assert "open-pull-request-webview" in review.vscode_pr_link("https://github.com/o/r/pull/1")


def test_the_publish_button_names_the_forge():
    from hrbuild.tabs import review
    spec = {"_prPush": {"count": 1, "posted": 0, "forge": "GitLab",
                        "counts": {"ignored": 1, "assumption": 0, "fixed": 0}}}
    assert ">Publish on GitLab</button>" in review.push_pr_button(spec)


# ---- `_GlDiff`: the numbers a position needs ---------------------------------------------

DIFF = """@@ -7,9 +7,9 @@ line 6 of A
 line 7 of A
 line 8 of A
 line 9 of A
-line 10 of A
+changed line 10 of A
 line 11 of A
 line 12 of A
 line 13 of A
@@ -18,6 +18,8 @@ line 17 of A
 line 18 of A
 line 19 of A
 line 20 of A
+added 21a
+added 21b
 line 21 of A
 line 22 of A
 line 23 of A
"""


def test_a_kept_line_knows_its_old_number_and_an_added_line_has_none():
    d = _GlDiff(DIFF)
    assert 10 in d.added and {21, 22} <= d.added
    assert d.old_of(12) == 12 and 12 not in d.added
    assert d.old_of(23) == 21


def test_a_line_outside_every_hunk_is_numbered_too():
    d = _GlDiff(DIFF)
    assert d.old_of(1) == 1               # before the first hunk
    assert d.old_of(15) == 15             # between hunks, nothing shifted yet
    assert d.old_of(40) == 38             # after the two added lines


def test_positions_count_as_github_s_do():
    """The same walk as `commit_positions`, on the same text: a later `@@` counts."""
    d = _GlDiff(DIFF)
    assert d.positions[7] == 1 and d.positions[10] == 5
    assert d.positions[18] == 10            # 9 lines above it, and the second `@@`
    assert d.line_at(5) == 10 and d.line_at(999) is None


# ---- its REST API, faked ----------------------------------------------------------------

class FakeGitLab:
    """`GitLabForge.glab` as a dict of `"METHOD path-prefix"` to an answer."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def __call__(self, inst, method, path, data=None, timeout=20):
        self.calls.append((method, path, data))
        for prefix, answer in self.answers.items():
            if f"{method} {path}".startswith(prefix):
                got = answer(data) if callable(answer) else answer
                return got if isinstance(got, str) else json.dumps(got)
        raise forge.GhError(f"GitLab {method} {path}: glab: 404 Not found (HTTP 404)")

    @property
    def method(self):
        return lambda inst, *a, **k: self(inst, *a, **k)

    def posted(self):
        return [(m, p, d) for m, p, d in self.calls if m != "GET"]


AN_MR = {"iid": 1, "title": "Visit has a vet", "description": "Adds a vet.",
         "web_url": ROOT + "/-/merge_requests/1", "source_branch": "visit-vet",
         "target_branch": "main", "sha": "f" * 40,
         "diff_refs": {"base_sha": "b" * 40, "start_sha": "s" * 40, "head_sha": "f" * 40}}


@pytest.fixture
def checkout(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "visit-vet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "git@gitlab.com:acme/shop/petclinic.git"], check=True)
    return tmp_path


def fake(monkeypatch, answers) -> FakeGitLab:
    f = FakeGitLab(answers)
    monkeypatch.setattr(GitLabForge, "glab", f.method)
    return f


def test_the_call_names_the_instance_in_the_environment(monkeypatch):
    """`--hostname` refuses `host:port`, and without `GITLAB_HOST` glab keeps a
    `GITLAB_TOKEN` from any host but gitlab.com: both seen on a real run."""
    seen = {}

    def run(args, **kw):
        seen.update(args=args, kw=kw)
        return subprocess.CompletedProcess(args, 0, '{"ok": 1}', "")
    monkeypatch.setattr(forge.subprocess, "run", run)
    assert GitLabForge("https://gitlab.acme.com:8443/a/b").api("POST", "x", {"k": 1}) == {"ok": 1}
    assert seen["args"] == ["glab", "api", "-X", "POST", "x", "--input", "-"]
    assert seen["kw"]["env"]["GITLAB_HOST"] == "gitlab.acme.com:8443"
    assert seen["kw"]["input"] == '{"k": 1}'


def test_a_relative_url_root_reaches_glab(monkeypatch):
    monkeypatch.setenv("GITLAB_HOST", "https://acme.com/gitlab")
    assert GitLabForge("https://acme.com/gitlab/g/r")._glab_env()["GITLAB_HOST"] == \
        "acme.com/gitlab"


@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"])
def test_a_token_does_not_follow_origin_to_a_host_nobody_named(monkeypatch, name):
    """`GITLAB_HOST` is what makes glab send a token, and the host comes from `origin`."""
    monkeypatch.setenv(name, "for-gitlab-com")
    assert name not in GitLabForge("https://gitlab.evil.example/a/b")._glab_env()


def test_a_token_goes_to_gitlab_com_and_to_the_instance_the_reader_named(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    assert GitLabForge(ROOT)._glab_env()["GITLAB_TOKEN"] == "t"
    monkeypatch.setenv("GITLAB_HOST", "gitlab.acme.com")
    assert GitLabForge("https://gitlab.acme.com/a/b")._glab_env()["GITLAB_TOKEN"] == "t"


def test_a_refusal_carries_its_http_status(monkeypatch):
    monkeypatch.setattr(forge.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 1, "", "glab: 404 Not found (HTTP 404)"))
    with pytest.raises(forge.GhError) as e:
        GitLabForge(ROOT).api("GET", "x")
    assert e.value.status == 404 and not e.value.transient


def test_a_list_is_read_to_its_last_page(monkeypatch):
    pages = {1: [{"n": i} for i in range(100)], 2: [{"n": 100}]}
    f = fake(monkeypatch, {"GET x": lambda _: None})
    f.answers = {f"GET x?per_page=100&page={k}": v for k, v in pages.items()}
    assert len(GitLabForge(ROOT)._pages("x")) == 101


def test_the_checkout_s_mr_reads_like_gh_s(monkeypatch, checkout):
    f = fake(monkeypatch, {f"GET {PROJECT}/merge_requests?source_branch=visit-vet": [AN_MR]})
    monkeypatch.setattr(GitLabForge, "available", staticmethod(lambda: True))
    monkeypatch.chdir(checkout)
    api = forge.api_forge()
    assert isinstance(api, GitLabForge)
    assert api.current_pr(checkout) == {"number": 1, "title": "Visit has a vet",
                                        "url": ROOT + "/-/merge_requests/1",
                                        "headRefName": "visit-vet", "baseRefName": "main"}
    assert api.repo_web_url(checkout) == ROOT
    assert "state=opened" in f.calls[0][1]


def test_no_glab_no_question(monkeypatch, checkout):
    f = fake(monkeypatch, {})
    monkeypatch.setattr(GitLabForge, "available", staticmethod(lambda: False))
    assert GitLabForge().current_pr(checkout) is None and not f.calls


def test_the_description_is_read_and_replaced(monkeypatch):
    f = fake(monkeypatch, {f"GET {MR}": AN_MR, f"PUT {MR}": {}})
    g = GitLabForge(ROOT)
    assert g.pr_body(1, None, None) == {"number": 1, "body": "Adds a vet.",
                                        "url": ROOT + "/-/merge_requests/1"}
    g.update_pr_body(1, "New body", "acme/shop/petclinic", None)
    assert f.calls[-1] == ("PUT", MR, {"description": "New body"})
    assert "description=@/tmp/b.md" in g.update_pr_body_shell(1, None, "/tmp/b.md")


def test_an_issue_reads_like_gh_s(monkeypatch):
    fake(monkeypatch, {f"GET {PROJECT}/issues/3": {
        "iid": 3, "title": "Every visit names a vet", "description": "Body",
        "web_url": ROOT + "/-/work_items/3", "created_at": "2026-11-04T19:57:05Z",
        "author": {"username": "ann"}}})
    assert GitLabForge(ROOT).issue(3, "acme/shop/petclinic", "number,title,author,createdAt") == {
        "number": 3, "title": "Every visit names a vet", "author": {"login": "ann"},
        "createdAt": "2026-11-04T19:57:05Z"}


def test_an_avatar_is_asked_for_and_absent_is_empty(monkeypatch):
    fake(monkeypatch, {"GET users?username=ann": [{"avatar_url": "https://x/a.png"}],
                       "GET users?username=ghost": []})
    g = GitLabForge(ROOT)
    assert g.avatar_url("ann") == "https://x/a.png" and g.avatar_url("ghost") == ""
    assert g.avatar_url("nobody-answers") == ""


def test_review_threads_count_resolvable_discussions(monkeypatch):
    fake(monkeypatch, {f"GET {MR}/discussions": [
        {"notes": [{"id": 1, "resolvable": True, "resolved": True}]},
        {"notes": [{"id": 2, "resolvable": True, "resolved": False}]},
        {"notes": [{"id": 3, "resolvable": False, "system": True}]}]})
    assert GitLabForge(ROOT).review_threads("acme/shop/petclinic", 1) == \
        {"resolved": [1], "open": 1}


def test_review_threads_say_when_nobody_answered(monkeypatch):
    fake(monkeypatch, {})
    assert "error" in GitLabForge(ROOT).review_threads("acme/shop/petclinic", 1)


# ---- CI: a pipeline is the commit's one run ----------------------------------------------

SHA = "a" * 40


def pipeline(status, **more):
    return {"id": 7, "sha": SHA, "ref": "visit-vet", "status": status, "source": "push",
            "created_at": "2026-11-04T19:57:05Z", "web_url": ROOT + "/-/pipelines/7", **more}


@pytest.mark.parametrize("status, run", [
    ("success", ("completed", "success")), ("failed", ("completed", "failure")),
    ("canceled", ("completed", "cancelled")), ("skipped", ("completed", "skipped")),
    ("manual", ("completed", "action_required")), ("running", ("in_progress", "")),
    ("pending", ("queued", "")), ("created", ("queued", "")),
    ("waiting_for_resource", ("queued", "")),
])
def test_a_pipeline_s_status_reads_as_github_s(monkeypatch, status, run):
    fake(monkeypatch, {f"GET {PROJECT}/pipelines?sha={SHA}": [pipeline(status)]})
    got = json.loads(GitLabForge(ROOT).ci_runs(SHA, ".gitlab-ci.yml", "").stdout)[0]
    assert (got["status"], got["conclusion"]) == run


def _preflight():
    spec = importlib.util.spec_from_file_location("preflight", HERE / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    return pf


def test_the_gate_detects_the_one_workflow_and_reads_a_verdict(monkeypatch):
    pf = _preflight()
    fake(monkeypatch, {f"GET {PROJECT}/pipelines?per_page=1": [pipeline("failed")],
                       f"GET {PROJECT}/pipelines?sha={SHA}": [pipeline("failed")]})
    monkeypatch.setattr(pf, "api_forge", lambda: GitLabForge(ROOT))
    assert pf.auto_detect(pf.list_workflows()) == [".gitlab-ci.yml"]
    runs, err = pf.latest_runs(SHA, ".gitlab-ci.yml")
    assert not err and pf._verdict(runs[0]) == "failure"


def test_a_project_with_no_pipeline_has_no_ci(monkeypatch):
    fake(monkeypatch, {f"GET {PROJECT}/pipelines?per_page=1": []})
    assert json.loads(GitLabForge(ROOT).ci_workflows().stdout) == []


def test_a_workflow_that_cannot_exist_is_not_found_not_a_wait():
    """`preflight.py` tells a misconfigured gate from a slow one by these words."""
    r = GitLabForge(ROOT).ci_runs(SHA, "deploy.yml", "")
    assert r.returncode == 1 and "not found" in r.stderr


def test_a_job_stands_as_its_own_step_so_a_sonar_job_is_found(monkeypatch):
    fake(monkeypatch, {f"GET {PROJECT}/pipelines/7/jobs": [
        {"id": 1, "name": "sonar-scan", "status": "success"},
        {"id": 2, "name": "test", "status": "failed"}],
        f"GET {PROJECT}/pipelines/7": pipeline("failed")})
    view = json.loads(GitLabForge(ROOT).ci_run(7).stdout)
    assert view["event"] == "push" and view["headBranch"] == "visit-vet"
    assert view["jobs"][0]["steps"] == [{"name": "sonar-scan", "status": "completed",
                                         "conclusion": "success"}]
    scan, _ = _preflight().sonar_steps(view["jobs"])
    assert scan["name"] == "sonar-scan"


def test_a_merge_request_pipeline_names_its_mr(monkeypatch):
    fake(monkeypatch, {f"GET {PROJECT}/pipelines?sha={SHA}": [
        pipeline("success", source="merge_request_event", ref="refs/merge-requests/4/head")],
        f"GET {PROJECT}/pipelines/7": pipeline("success", ref="refs/merge-requests/4/head")})
    g = GitLabForge(ROOT)
    assert json.loads(g.ci_runs_of(SHA))[0]["event"] == "pull_request"
    assert g.ci_run_pr(7) == "4"


def test_a_failed_log_is_the_traces_of_the_failed_jobs_only(monkeypatch):
    fake(monkeypatch, {f"GET {PROJECT}/pipelines/7/jobs": [
        {"id": 1, "name": "lint", "status": "success"},
        {"id": 2, "name": "test", "status": "failed"}],
        f"GET {PROJECT}/jobs/2/trace": "boom at line 3"})
    log = GitLabForge(ROOT).ci_failed_log(7)
    assert "==> test" in log and "boom at line 3" in log and "lint" not in log


# ---- the push's GitHub-worded calls, on an MR ---------------------------------------------

DIFFS = [{"old_path": "A.java", "new_path": "A.java", "diff": DIFF},
         {"old_path": "src/Old.java", "new_path": "src/New.java",
          "diff": "@@ -1,2 +1,2 @@\n old\n-name\n+new name\n"}]


def a_note(id_, body, position=None, **more):
    return {"id": id_, "body": body, "position": position, "system": False,
            "author": {"username": "ann"}, **more}


def call(method, path, payload):
    return SimpleNamespace(method=method, path=path, payload=payload)


@pytest.fixture
def lab(monkeypatch):
    f = fake(monkeypatch, {f"GET {MR}/diffs": DIFFS, f"GET {MR}/discussions": [],
                           f"GET {MR}": AN_MR, "POST ": {"id": 1}, "PUT ": {}, "DELETE ": ""})
    return f, GitLabForge(ROOT).pr_api()


REPO = "acme/shop/petclinic"


def positions(f):
    return [d["position"] for m, _, d in f.posted() if d and d.get("position")]


def test_an_added_line_needs_only_its_new_number(lab):
    f, api = lab
    api.send(call("POST", api.comments_path(REPO, 1), {"path": "A.java", "line": 10, "body": "b"}))
    pos = positions(f)[0]
    assert (pos["new_line"], pos["new_path"], pos["old_path"]) == (10, "A.java", "A.java")
    assert "old_line" not in pos
    assert (pos["base_sha"], pos["start_sha"], pos["head_sha"]) == ("b" * 40, "s" * 40, "f" * 40)


def test_an_unchanged_line_needs_both_numbers(lab):
    """Only `new_line` on a context line is a 400 from a real GitLab."""
    f, api = lab
    api.send(call("POST", api.comments_path(REPO, 1), {"path": "A.java", "line": 23, "body": "b"}))
    assert (positions(f)[0]["old_line"], positions(f)[0]["new_line"]) == (21, 23)


def test_a_renamed_file_keeps_its_old_path(lab):
    f, api = lab
    api.send(call("POST", api.comments_path(REPO, 1),
                  {"path": "src/New.java", "line": 2, "body": "b"}))
    assert (positions(f)[0]["old_path"], positions(f)[0]["new_path"]) == \
        ("src/Old.java", "src/New.java")


def test_a_range_names_both_of_its_ends(lab):
    f, api = lab
    api.send(call("POST", api.comments_path(REPO, 1),
                  {"path": "A.java", "start_line": 21, "line": 22, "body": "b"}))
    pos = positions(f)[0]
    start, end = pos["line_range"]["start"], pos["line_range"]["end"]
    assert (start["new_line"], end["new_line"], pos["new_line"]) == (21, 22, 22)
    assert start["type"] == end["type"] == "new" and start["old_line"] is None
    assert start["line_code"].endswith("_21_21") and end["line_code"].endswith("_21_22")


def test_a_file_comment_has_no_line(lab):
    f, api = lab
    api.send(call("POST", api.comments_path(REPO, 1),
                  {"path": "A.java", "subject_type": "file", "body": "b"}))
    assert positions(f)[0]["position_type"] == "file" and "new_line" not in positions(f)[0]


def test_a_review_is_a_note_and_a_discussion_per_comment(lab):
    f, api = lab
    api.send(call("POST", api.reviews_path(REPO, 1), {
        "commit_id": "f" * 40, "event": "COMMENT", "body": "Summary",
        "comments": [{"path": "A.java", "line": 10, "body": "one"},
                     {"path": "A.java", "line": 12, "body": "two"}]}))
    sent = f.posted()
    assert sent[0] == ("POST", f"{MR}/notes", {"body": "Summary"})
    assert [(p, d["body"]) for _, p, d in sent[1:]] == [(f"{MR}/discussions", "one"),
                                                        (f"{MR}/discussions", "two")]


def test_a_continued_summary_is_github_s_problem_alone(lab):
    f, api = lab
    api.send(call("POST", api.reviews_path(REPO, 1), {
        "body": "Review record, continued — 1 more comment.\n\n<!-- hr:review-more:abc -->",
        "comments": [{"path": "A.java", "line": 10, "body": "one"}]}))
    assert [p for _, p, _ in f.posted()] == [f"{MR}/discussions"]


def listed(monkeypatch):
    position = {"position_type": "text", "new_path": "A.java", "new_line": 22,
                "head_sha": "f" * 40, "line_range": {"start": {"new_line": 21},
                                                     "end": {"new_line": 22}}}
    f = fake(monkeypatch, {f"GET {MR}/diffs": [], f"GET {MR}/discussions": [
        {"id": "d1", "notes": [a_note(11, "line <!-- hr:I:x -->", position)]},
        {"id": "d2", "notes": [a_note(12, "summary <!-- hr:review:abc -->")]},
        {"id": "d3", "notes": [{"id": 13, "system": True, "body": "added 1 commit"}]},
        {"id": "d4", "notes": [a_note(14, "file", {"position_type": "file", "new_path": "N.txt",
                                                   "head_sha": "f" * 40})]},
    ], f"GET {MR}": AN_MR, "PUT ": {}, "DELETE ": ""})
    return f, GitLabForge(ROOT).pr_api()


def test_comments_and_reviews_are_listed_apart_in_github_s_words(monkeypatch):
    _, api = listed(monkeypatch)
    comments = api.list(api.comments_path(REPO, 1))
    assert [c["id"] for c in comments] == [11, 14]
    assert (comments[0]["path"], comments[0]["line"], comments[0]["start_line"]) == ("A.java", 22, 21)
    assert comments[0]["html_url"] == f"{ROOT}/-/merge_requests/1#note_11"
    assert comments[1]["subject_type"] == "file" and "line" not in comments[1]
    assert [r["id"] for r in api.list(api.reviews_path(REPO, 1))] == [12]


def test_an_outdated_note_lists_with_no_line_as_github_s_does(monkeypatch):
    """Seen on a real GitLab: a note whose line a later commit changed keeps its old head
    and number, and `_place` would call it "already posted"."""
    stale = {"position_type": "text", "new_path": "A.java", "new_line": 21, "head_sha": "0" * 40}
    live = {"position_type": "text", "new_path": "A.java", "new_line": 13, "head_sha": "f" * 40}
    fake(monkeypatch, {f"GET {MR}/diffs": [], f"GET {MR}/discussions": [
        {"id": "d1", "notes": [a_note(7, "gone", stale)]},
        {"id": "d2", "notes": [a_note(4, "kept", live)]}], f"GET {MR}": AN_MR})
    api = GitLabForge(ROOT).pr_api()
    gone, kept = api.list(api.comments_path(REPO, 1))
    assert "line" not in gone and gone["path"] == "A.java" and kept["line"] == 13
    assert api.comment_path(REPO, 7) == f"{MR}/discussions/d1/notes/7"     # still deletable


def test_an_id_from_a_listing_finds_its_note_again(monkeypatch):
    """GitHub addresses a comment by id alone; GitLab needs its discussion too."""
    f, api = listed(monkeypatch)
    api.list(api.comments_path(REPO, 1))
    assert api.comment_path(REPO, 11) == f"{MR}/discussions/d1/notes/11"
    api.send(call("PATCH", api.comment_path(REPO, 11), {"body": "edited"}))
    api.send(call("DELETE", api.comment_path(REPO, 11), {}))
    assert f.posted() == [("PUT", f"{MR}/discussions/d1/notes/11", {"body": "edited"}),
                          ("DELETE", f"{MR}/discussions/d1/notes/11", None)]


def test_a_commit_note_is_planned_in_positions_and_sits_on_a_line(monkeypatch):
    sha = "c" * 40
    commit = f"{PROJECT}/repository/commits/{sha}"
    f = fake(monkeypatch, {f"GET {commit}/diff": DIFFS[:1], f"GET {commit}/discussions": [],
                           f"GET {commit}": {"id": sha, "parent_ids": ["p" * 40]},
                           f"POST {commit}/discussions": {"id": "d"}})
    api = GitLabForge(ROOT).pr_api()
    api.send(call("POST", api.commit_comments_path(REPO, sha),
                  {"path": "A.java", "position": 5, "body": "fixed"}))
    pos = positions(f)[0]
    assert (pos["new_line"], pos["base_sha"], pos["head_sha"]) == (10, "p" * 40, sha)


def test_a_commit_note_with_no_line_is_a_plain_note(monkeypatch):
    sha = "c" * 40
    commit = f"{PROJECT}/repository/commits/{sha}"
    f = fake(monkeypatch, {f"POST {commit}/discussions": {"id": "d"}})
    api = GitLabForge(ROOT).pr_api()
    api.send(call("POST", api.commit_comments_path(REPO, sha), {"body": "fixed"}))
    assert f.posted()[0][2] == {"body": "fixed"}


def test_a_commit_note_lists_back_at_the_position_it_was_planned_in(monkeypatch):
    """`plan_notes` compares positions: a note that lists back at another one is moved on
    every push."""
    sha = "c" * 40
    commit = f"{PROJECT}/repository/commits/{sha}"
    fake(monkeypatch, {f"GET {commit}/diff": DIFFS[:1],
                       f"GET {commit}/discussions": [{"id": "d", "notes": [a_note(
                           6, "n", {"position_type": "text", "new_path": "A.java",
                                    "new_line": 10})]}],
                       f"GET {commit}": {"id": sha, "parent_ids": ["p" * 40]}})
    api = GitLabForge(ROOT).pr_api()
    got = api.list(api.commit_comments_path(REPO, sha))[0]
    assert (got["path"], got["position"]) == ("A.java", 5)
    assert got["html_url"] == f"{ROOT}/-/commit/{sha}#note_6"


def test_the_page_knows_a_gitlab_comment_link():
    spec = importlib.util.spec_from_file_location("ppc", HERE / "push-pr-comments.py")
    ppc = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ppc            # its dataclasses look themselves up there
    spec.loader.exec_module(ppc)
    record = {"comments": {"I:x": {"html_url": ROOT + "/-/merge_requests/1#note_11"},
                           "F:y": {"mode": "commit", "html_url": ROOT + "/-/commit/abc1234#note_12"}}}
    assert 11 in ppc.owned_ids(record)
    assert ppc.owned_note_ids(record) == {12}


def test_every_github_method_has_a_gitlab_twin():
    """A caller holds a forge without knowing which: a method only GitHub has is a crash,
    or a `gh` call, the day the checkout is on GitLab."""
    public = lambda cls: {n for n in dir(cls) if not n.startswith("_")}
    assert public(GitHubForge) - public(GitLabForge) == set()
    pr_api = lambda cls: {n for n in vars(cls) if not n.startswith("_")}
    assert pr_api(forge.GitHubPrApi) - public(GitLabPrApi) == set()
