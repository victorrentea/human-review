#!/usr/bin/env python3
"""Bitbucket Server / Data Center as the second forge behind `hrbuild/shared/forge.py`
(issue #4): its remotes are recognised, its web links have its own shape, and its REST
answers come back in the words `gh` uses, so every caller reads them unchanged.

No Bitbucket is reached: `BitbucketServerForge.http` is the one network point, and a fake
stands in for it with answers in the shapes Bitbucket's REST API 1.0 documents.

Run with:  python3 -m pytest test_forge_bitbucket.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from hrbuild.shared import forge  # noqa: E402
from hrbuild.shared.forge import BitbucketServerForge, GitHubForge  # noqa: E402

ROOT = "https://bitbucket.acme.com/projects/SHOP/repos/petclinic"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("BITBUCKET_URL", "BITBUCKET_TOKEN", "JENKINS_USER", "JENKINS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    forge.forge_for.cache_clear()
    BitbucketServerForge._runs.clear()
    yield
    forge.forge_for.cache_clear()


# ---- recognising its remotes and URLs ---------------------------------------------------

@pytest.mark.parametrize("remote, root", [
    ("https://bitbucket.acme.com/scm/shop/petclinic.git", ROOT),
    ("https://jane@bitbucket.acme.com/scm/SHOP/petclinic.git", ROOT),
    ("https://acme.com/bitbucket/scm/shop/petclinic", "https://acme.com/bitbucket/projects/SHOP/repos/petclinic"),
    ("ssh://git@bitbucket.acme.com:7999/shop/petclinic.git", ROOT),
    ("https://bitbucket.acme.com/scm/~jane/scratch.git",
     "https://bitbucket.acme.com/users/jane/repos/scratch"),
])
def test_a_bitbucket_origin_names_its_web_root(remote, root):
    assert BitbucketServerForge.from_remote(remote) == root
    assert forge.forge_from_any_remote(remote).repo_url == root


def test_an_ssh_origin_on_another_port_needs_BITBUCKET_URL(monkeypatch):
    remote = "ssh://git@git.acme.com:22/shop/petclinic.git"
    assert BitbucketServerForge.from_remote(remote) is None
    monkeypatch.setenv("BITBUCKET_URL", "https://code.acme.com/bb/")
    assert BitbucketServerForge.from_remote(remote) is None          # another host
    monkeypatch.setenv("BITBUCKET_URL", "https://git.acme.com/bb/")
    assert BitbucketServerForge.from_remote(remote) == \
        "https://git.acme.com/bb/projects/SHOP/repos/petclinic"


def test_github_remotes_stay_github():
    for remote in ("git@github.com:victorrentea/petclinic.git",
                   "https://github.com/victorrentea/petclinic"):
        assert BitbucketServerForge.from_remote(remote) is None
        assert isinstance(forge.forge_from_any_remote(remote), GitHubForge)


def test_forge_at_picks_bitbucket_by_the_shape_of_the_url():
    assert isinstance(forge.forge_at(ROOT), BitbucketServerForge)
    assert isinstance(forge.forge_at("https://github.com/o/r"), GitHubForge)
    assert isinstance(forge.forge_at("https://ghe.acme.com/o/r"), GitHubForge)
    assert forge.forge_at(ROOT + "/pull-requests/7").repo_url == ROOT


def test_pull_request_urls_are_recognised():
    pr = ROOT + "/pull-requests/42"
    assert forge.is_pull_url(pr + "/overview")
    assert forge.pull_root(pr + "/overview?commentId=9") == pr
    assert forge.is_comment_url(pr + "/overview?commentId=9")
    assert not forge.is_comment_url(pr + " ; rm -rf /")
    found, number = forge.find_pull(f"see {pr}/diff please")
    assert (found.repo_url, found.slug, number) == (ROOT, "SHOP/petclinic", 42)
    assert forge.project_of(ROOT) == "SHOP/petclinic"
    assert forge.is_commit_comment_url(ROOT + "/commits/abc1234?commentId=3")
    assert forge.find_issue(ROOT + "/issues/3") is None


# ---- its web links ----------------------------------------------------------------------

def test_web_links_have_bitbucket_s_shape():
    f = BitbucketServerForge(ROOT)
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert f.blob_url(sha, "src/A.java") == f"{ROOT}/browse/src/A.java?at={sha}"
    assert f.blob_url(sha, "src/A.java", 57, 59) == f"{ROOT}/browse/src/A.java?at={sha}#57-59"
    assert f.blob_url("feature/x", "A.java", 3) == \
        f"{ROOT}/browse/A.java?at=refs%2Fheads%2Ffeature%2Fx#3"
    assert f.tree_url("main") == f"{ROOT}/browse?at=refs%2Fheads%2Fmain"
    assert f.commit_url(sha) == f"{ROOT}/commits/{sha}"
    assert f.compare_url("main", "feature") == (
        f"{ROOT}/compare/diff?sourceBranch=refs%2Fheads%2Ffeature"
        "&targetBranch=refs%2Fheads%2Fmain")
    assert f.issue_url(12) == ""                 # no issue tracker: nothing to link to
    assert f.name == "Bitbucket"
    assert f.of_slug("~jane/x").repo_url == "https://bitbucket.acme.com/users/jane/repos/x"


def test_github_s_links_did_not_move():
    g = GitHubForge("https://github.com/o/r")
    assert g.blob_url("abc", "A.java", 5, 7) == "https://github.com/o/r/blob/abc/A.java#L5-L7"
    assert g.blob_query("abc") == "" and g.line_anchor(5, 7) == "#L5-L7"


def test_an_issue_reference_in_a_bitbucket_pr_body_stays_text():
    from hrbuild.tabs import review
    assert review._md_inline("fixes #12", ROOT) == "fixes #12"
    assert 'href="https://github.com/o/r/issues/12"' in review._md_inline("fixes #12",
                                                                         "https://github.com/o/r")


# ---- its REST API, faked ----------------------------------------------------------------

class FakeBitbucket:
    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def __call__(self, inst, method, path, data=None, timeout=20):
        self.calls.append((method, path, data))
        for prefix, answer in self.answers.items():
            if f"{method} {path}".startswith(prefix):
                return answer(data) if callable(answer) else answer
        raise forge.GhError(f"Bitbucket {method} {path}: HTTP 404 no such thing")

    @property
    def method(self):
        """This fake as `http` on the class: bound like a method, `self` the forge."""
        return lambda inst, *a, **k: self(inst, *a, **k)


PR = {"id": 42, "version": 3, "title": "Visit has a vet", "description": "Adds a vet.",
      "fromRef": {"displayId": "visit-vet", "latestCommit": "f" * 40},
      "toRef": {"displayId": "main"},
      "reviewers": [{"user": {"name": "ann"}}, {"user": {"name": "bob"}}],
      "links": {"self": [{"href": ROOT + "/pull-requests/42"}]}}
API = "api/latest/projects/SHOP/repos/petclinic"


@pytest.fixture
def checkout(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "visit-vet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "ssh://git@bitbucket.acme.com:7999/shop/petclinic.git"], check=True)
    return tmp_path


def test_the_checkout_s_pr_reads_like_gh_s(monkeypatch, checkout):
    monkeypatch.setenv("BITBUCKET_TOKEN", "t")
    fake = FakeBitbucket({f"GET {API}/pull-requests?at=refs%2Fheads%2Fvisit-vet": {"values": [PR]}})
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    monkeypatch.chdir(checkout)
    api = forge.api_forge()
    assert isinstance(api, BitbucketServerForge)
    assert api.current_pr(checkout) == {"number": 42, "title": "Visit has a vet",
                                        "url": ROOT + "/pull-requests/42",
                                        "headRefName": "visit-vet", "baseRefName": "main"}
    assert api.repo_web_url(checkout) == ROOT
    assert "direction=OUTGOING" in fake.calls[0][1] and "state=OPEN" in fake.calls[0][1]


def test_no_token_no_question(monkeypatch, checkout):
    fake = FakeBitbucket({})
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    assert not BitbucketServerForge.available()
    assert BitbucketServerForge().current_pr(checkout) is None and not fake.calls


def test_the_description_is_rewritten_with_its_version_title_and_reviewers(monkeypatch):
    fake = FakeBitbucket({f"GET {API}/pull-requests/42": PR,
                          f"PUT {API}/pull-requests/42": {}})
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    f = BitbucketServerForge(ROOT)
    assert f.pr_body(42, None, None) == {"number": 42, "body": "Adds a vet.",
                                         "url": ROOT + "/pull-requests/42"}
    f.update_pr_body(42, "New body", "SHOP/petclinic", None)
    method, path, sent = fake.calls[-1]
    assert (method, sent) == ("PUT", {"version": 3, "title": "Visit has a vet",
                                      "description": "New body",
                                      "reviewers": [{"user": {"name": "ann"}},
                                                    {"user": {"name": "bob"}}]})
    shell = f.update_pr_body_shell(42, None, "/tmp/body.md")
    assert "BITBUCKET_TOKEN" in shell and "/rest/api/latest/projects/SHOP/repos/petclinic/pull-requests/42" in shell


def test_build_statuses_are_runs_the_preflight_gate_can_wait_on(monkeypatch):
    sha = "a" * 40
    fake = FakeBitbucket({
        f"GET build-status/1.0/commits/{sha}": {"isLastPage": True, "values": [
            {"key": "petclinic-ci", "name": "petclinic » ci", "state": "SUCCESSFUL",
             "url": "https://jenkins.acme.com/job/ci/7/", "dateAdded": 1762200000000},
            {"key": "sonar", "name": "sonar", "state": "INPROGRESS", "url": ""}]},
        f"GET {API}/commits?limit=10": {"values": [{"id": sha}]},
    })
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    f = BitbucketServerForge(ROOT)
    wfs = json.loads(f.ci_workflows().stdout)
    assert [w["path"] for w in wfs] == ["petclinic-ci", "sonar"]
    runs = json.loads(f.ci_runs(sha, None, "").stdout)
    assert [(r["status"], r["conclusion"]) for r in runs] == [("completed", "success"),
                                                              ("in_progress", "")]
    assert runs[0]["headSha"] == sha and runs[0]["createdAt"] == "2025-11-03T20:00:00Z"
    one = json.loads(f.ci_runs(sha, "petclinic-ci", "").stdout)
    assert [r["key"] for r in one] == ["petclinic-ci"]
    assert json.loads(f.ci_run(one[0]["databaseId"]).stdout)["event"] == "push"
    assert f.ci_runs(sha, "deploy", "").stdout == "[]"


def test_preflight_reads_bitbucket_runs_as_a_verdict(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("preflight", HERE / "preflight.py")
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    sha = "b" * 40
    fake = FakeBitbucket({f"GET build-status/1.0/commits/{sha}": {"values": [
        {"key": "ci", "name": "ci", "state": "FAILED", "url": "u"}]}})
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    monkeypatch.setattr(pf, "api_forge", lambda: BitbucketServerForge(ROOT))
    runs, err = pf.latest_runs(sha, "ci")
    assert not err and pf._verdict(runs[0]) == "failure"


def test_review_threads_count_resolved_and_open(monkeypatch):
    fake = FakeBitbucket({f"GET {API}/pull-requests/42/activities": {"isLastPage": True, "values": [
        {"action": "COMMENTED", "comment": {"id": 1, "threadResolved": True}},
        {"action": "COMMENTED", "comment": {"id": 2, "state": "RESOLVED"}},
        {"action": "COMMENTED", "comment": {"id": 3}},
        {"action": "APPROVED"}]}})
    monkeypatch.setattr(BitbucketServerForge, "http", fake.method)
    assert BitbucketServerForge(ROOT).review_threads("SHOP/petclinic", 42) == \
        {"resolved": [1, 2], "open": 1}


def test_pushing_comments_and_reading_issues_refuse_in_plain_words():
    f = BitbucketServerForge(ROOT)
    with pytest.raises(forge.GhError, match="not supported yet"):
        f.pr_api().list("x")
    with pytest.raises(forge.GhError, match="Jira"):
        f.issue(3, "SHOP/petclinic", "title")


def test_every_github_method_has_a_bitbucket_twin():
    """A caller holds a forge without knowing which: a method only GitHub has is a crash,
    or a `gh` call, the day the checkout is on Bitbucket."""
    public = lambda cls: {n for n in dir(cls) if not n.startswith("_")}
    assert public(GitHubForge) - public(BitbucketServerForge) == set()
    pr_api = lambda cls: {n for n in vars(cls) if not n.startswith("_")}
    assert pr_api(forge.GitHubPrApi) - public(forge.BitbucketPrApi) == set()
