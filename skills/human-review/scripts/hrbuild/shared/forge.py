"""The forge that hosts the reviewed repository — GitHub, GitLab, or Bitbucket Server / Data
Center (issue #4) — behind one seam.

Everything the skill knows about the place the reviewed code is pushed to lives here and
nowhere else: the shape of its web URLs (a file at a commit, a folder, a commit, a
comparison, a pull request, an issue, an author's face), how to recognise one of those
URLs when a content file or an API answer hands one over, and every question asked of it
over its command line (`gh`, `glab`): a PR's description (read, and rewritten for its
merge-risk line), an issue, the PR's comments and review
threads, the CI runs of a commit. `test_forge_seam.py` fails a `gh` call or a
`https://github.com/` URL of the reviewed repo built anywhere else.

A second forge is a second class with the same methods, added to `FORGES`: `forge_for`
picks it by the checkout's `origin`, `forge_at` by a repo URL, and the module-level
recognisers ask every class in turn. Nothing outside this file should have to change.

Every method does exactly what the call site it came from did — the same argv, the same
timeouts, the same failures raised or swallowed — because moving a call here is not a
reason for it to behave differently.

Also a tiny CLI, for the shell scripts: `python3 forge.py pr-title` prints the current
branch's PR title, or nothing.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------------------- #
# errors a `gh api` call can come back with
# --------------------------------------------------------------------------------------- #

#: What a 5xx or a timeout looks like in `gh`'s words — a call GitHub may have carried out
#: anyway, so the next step is to look, not to send it again.
TRANSIENT = re.compile(r"\(HTTP 5\d\d\)|HTTP 5\d\d|timed? ?out|couldn't respond to your request "
                       r"in time|bad gateway|connection reset|unexpected EOF", re.I)


class GhError(RuntimeError):
    """A `gh` call that failed, with GitHub's HTTP status when it said one."""

    def __init__(self, text: str):
        super().__init__(text)
        m = re.search(r"\(HTTP (\d{3})\)|HTTP (\d{3})", text)
        self.status = int(m.group(1) or m.group(2)) if m else None

    @property
    def transient(self) -> bool:
        return (self.status or 0) >= 500 or bool(TRANSIENT.search(str(self)))


def _sh(cmd: str) -> subprocess.CompletedProcess:
    """A shell command line, both streams captured — how `preflight.py` has always run `gh`."""
    return subprocess.run(cmd, shell=True, text=True, check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class GitHubPrApi:
    """A PR's review comments and reviews, created, edited and deleted through `gh api`, so
    the credentials are the reader's own and nothing here ever sees a token. What
    `push-pr-comments.py` posts with; its tests stand a fake with `list`/`send` in for it."""

    # The REST paths the push plans its calls against.
    @staticmethod
    def comments_path(repo: str, pr: int) -> str:
        return f"repos/{repo}/pulls/{pr}/comments"

    @staticmethod
    def comment_path(repo: str, comment_id) -> str:
        return f"repos/{repo}/pulls/comments/{comment_id}"

    @staticmethod
    def reviews_path(repo: str, pr: int) -> str:
        return f"repos/{repo}/pulls/{pr}/reviews"

    @staticmethod
    def review_path(repo: str, pr: int, review_id) -> str:
        return f"repos/{repo}/pulls/{pr}/reviews/{review_id}"

    # A commit's own comments: where an auto-fix's note goes, on the commit that fixed it,
    # not on the PR (Victor, 30 Oct 2026: a fixed item on the PR is clutter in its diff).
    @staticmethod
    def commit_comments_path(repo: str, sha: str) -> str:
        return f"repos/{repo}/commits/{sha}/comments"

    @staticmethod
    def commit_comment_path(repo: str, comment_id) -> str:
        return f"repos/{repo}/comments/{comment_id}"

    @staticmethod
    def shell(method: str, path: str, payload: dict) -> str:
        """The call as a reader could paste it — what a dry run prints."""
        return (f"gh api -X {method} {path} --input - <<'JSON'\n"
                + json.dumps(payload, indent=2, ensure_ascii=False) + "\nJSON")

    def run(self, args: list[str], data: dict | None = None) -> str:
        r = subprocess.run(["gh", *args], input=json.dumps(data) if data is not None else None,
                           capture_output=True, text=True)
        if r.returncode:
            raise GhError(f"gh {' '.join(args[:4])}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def login(self) -> str | None:
        try:
            return self.run(["api", "user", "--jq", ".login"]).strip() or None
        except RuntimeError:
            return None

    def list(self, path: str) -> list[dict]:
        out = self.run(["api", "--paginate", path, "--jq", ".[]"])
        return [json.loads(l) for l in out.splitlines() if l.strip()]

    def send(self, call) -> dict:
        """`call`: anything with `method`, `path` and `payload` (push-pr-comments' `Call`)."""
        out = self.run(["api", "-X", call.method, call.path, "--input", "-"], call.payload)
        return json.loads(out) if out.strip() else {}

    def pr(self, root: Path, pr: int | None) -> dict:
        args = ["pr", "view", *([str(pr)] if pr else []), "--json",
                "number,url,headRefOid,baseRefName,headRefName"]
        r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=root)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "no PR for this branch")
        return json.loads(r.stdout)

    def repo(self, root: Path) -> str:
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                            ".nameWithOwner"], capture_output=True, text=True, cwd=root)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()


#: Review threads of one PR, first comment id and whether it is resolved. GraphQL, because
#: REST does not say whether a thread is resolved.
_REVIEW_THREADS_QUERY = """query($owner:String!,$name:String!,$pr:Int!,$after:String){
  repository(owner:$owner,name:$name){pullRequest(number:$pr){
    reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor}
      nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}"""


class GitHubForge:
    """GitHub: its web URLs, its URL spellings, and `gh`.

    `repo_url` is the repository's web root, `https://github.com/<owner>/<repo>`. Empty
    for the instance `api_forge()` hands out: `gh` finds the repository from the working
    directory, and the CLI methods need no URL."""

    #: What a reader is told a link opens on: "Open on GitHub".
    name = "GitHub"
    #: How a reader fixes a refused login, in the words of a sentence ending "your …".
    auth_hint = "gh login (run `gh auth status`)"

    def __init__(self, repo_url: str = ""):
        self.repo_url = repo_url

    def __repr__(self) -> str:
        return f"GitHubForge({self.repo_url!r})"

    # ---- recognising its URLs ------------------------------------------------------- #

    @staticmethod
    def from_remote(remote: str) -> str | None:
        """The web root for an `origin` URL — `https://github.com/o/r(.git)` or
        `git@github.com:o/r(.git)` — or None when it is not one of those."""
        m = re.match(r"(?:https://github\.com/|git@github\.com:)(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
                     remote.strip())
        return f"https://github.com/{m['slug']}" if m else None

    @staticmethod
    def from_any_remote(remote: str) -> str | None:
        """`from_remote`, also accepting `ssh://git@github.com/o/r`, a user in an https
        remote, `http` and a trailing slash — the spellings `test-changes.py` has always
        taken. Kept apart from `from_remote` so neither caller's links change."""
        m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^@/]+@)?github\.com/)"
                     r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$", remote.strip())
        return f"https://github.com/{m.group(1)}" if m else None

    @staticmethod
    def project_of(url: str) -> str:
        """`owner/repo` when `url` is a repository's web root (`.git` and a trailing slash
        allowed), else ""."""
        m = re.match(r"https?://github\.com/([^/]+/[^/#?]+?)(?:\.git)?/?$", url or "")
        return m[1] if m else ""

    @staticmethod
    def is_pull_url(url: str) -> bool:
        """Whether a URL is a pull request's — all a page needs to call it a PR."""
        return "/pull/" in str(url or "")

    @staticmethod
    def pull_root(url: str) -> str | None:
        """`https://github.com/o/r/pull/7` out of any URL starting with it (a comment's
        `#discussion_r…` cut off), else None."""
        m = re.match(r"^(https://github\.com/[^/#?]+/[^/#?]+/pull/\d+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    def find_pull(text: str) -> tuple[str, int] | None:
        """`(owner/repo, number)` of the first PR URL anywhere in `text`."""
        m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", str(text or ""))
        return (m.group(1), int(m.group(2))) if m else None

    @staticmethod
    def is_comment_url(url: str) -> bool:
        """A comment, a review or the PR itself, anchored at both ends: the only URL a click
        may hand an editor to open in a browser."""
        return bool(re.match(r"^https://github\.com/[^/\s?#]+/[^/\s?#]+/pull/\d+([/?#]\S*)?$",
                             url))

    @staticmethod
    def is_commit_comment_url(url: str) -> bool:
        """A comment on a commit (`…/commit/<sha>#commitcomment-<id>`) — where an
        auto-fix's note lives, as against a PR comment."""
        return bool(re.match(r"^https://github\.com/[^/\s?#]+/[^/\s?#]+/commit/[0-9a-f]{7,40}"
                             r"#commitcomment-\d+$", str(url or "")))

    @staticmethod
    def find_issue(text: str) -> tuple[str, int] | None:
        """`(owner/repo, number)` when `text` starts with an issue URL."""
        m = re.match(r"^https?://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)\b", text, re.I)
        return (m.group(1), int(m.group(2))) if m else None

    # ---- its web URLs --------------------------------------------------------------- #

    @classmethod
    def of_slug(cls, slug: str) -> "GitHubForge":
        """The forge at `owner/repo` — what `gh repo view` and a PR URL name."""
        return cls(f"https://github.com/{slug}")

    @property
    def slug(self) -> str:
        """`owner/repo`, as `gh -R` takes it. A URL elsewhere is passed through whole,
        as it always was."""
        return re.sub(r"^https?://github\.com/", "", self.repo_url or "").strip("/")

    def blob_prefix(self, ref: str) -> str:
        """Every file at `ref` is this plus `/<path>`."""
        return f"{self.repo_url}/blob/{ref}"

    def blob_url(self, ref: str, path: str, line: int | None = None,
                 end: int | None = None) -> str:
        """A file at `ref`; at `line`, or the lines `line`-`end` (`#L57-L59`, which GitHub
        renders as the code itself when the link stands alone on a comment's line)."""
        url = f"{self.repo_url}/blob/{ref}/{path}"
        if line:
            url += f"#L{line}" + (f"-L{end}" if end and end > line else "")
        return url

    def tree_url(self, ref: str) -> str:
        """The tree at `ref` (a branch name, quoted by the caller, or a commit)."""
        return f"{self.repo_url}/tree/{ref}"

    def commit_url(self, sha: str) -> str:
        return f"{self.repo_url}/commit/{sha}"

    def compare_url(self, base: str, head: str) -> str:
        """The diff between two refs, both already quoted for a URL."""
        return f"{self.repo_url}/compare/{base}...{head}"

    def issue_url(self, number) -> str:
        return f"{self.repo_url}/issues/{number}"

    @staticmethod
    def avatar_url(login: str) -> str:
        return f"https://github.com/{login}.png?size=48"

    # ---- asked over `gh` ------------------------------------------------------------ #

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("gh"))

    @staticmethod
    def pr_api() -> GitHubPrApi:
        return GitHubPrApi()

    @staticmethod
    def pr_body(number, slug: str | None, root: Path | None) -> dict:
        """`{number, body, url}` of a PR, raising on any failure (no `gh`, no network, a
        timeout, a refusal)."""
        args = ["gh", "pr", "view", str(number), "--json", "number,body,url"]
        if slug:
            args += ["-R", slug]
        out = subprocess.run(args, capture_output=True, text=True, timeout=15,
                             cwd=root, check=True)
        return json.loads(out.stdout)

    @staticmethod
    def update_pr_body(number, body: str, slug: str | None, root: Path | None) -> None:
        """Replace a PR's description with `body`, raising `GhError` when GitHub refused
        and `subprocess.TimeoutExpired` when it did not answer."""
        args = ["gh", "pr", "edit", str(number), "--body-file", "-"]
        if slug:
            args += ["-R", slug]
        r = subprocess.run(args, input=body, capture_output=True, text=True, timeout=30,
                           cwd=root)
        if r.returncode:
            raise GhError(f"gh pr edit {number}: {r.stderr.strip() or r.stdout.strip()}")

    @staticmethod
    def update_pr_body_shell(number, slug: str | None, path) -> str:
        """`update_pr_body` as a reader could paste it, the body read from `path` — what a
        dry run prints."""
        return (f"gh pr edit {number}" + (f" -R {shlex.quote(slug)}" if slug else "")
                + f" --body-file {shlex.quote(str(path))}")

    @staticmethod
    def pr_title_here() -> None:
        """The current branch's PR title on stdout, or nothing — for the shell scripts."""
        try:
            subprocess.run(["gh", "pr", "view", "--json", "title", "--jq", ".title"],
                           stderr=subprocess.DEVNULL)
        except OSError:
            pass

    @staticmethod
    def _json(root: Path, *args: str) -> dict | None:
        if not shutil.which("gh"):
            return None
        try:
            p = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True,
                               timeout=60)
            return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
        except (subprocess.TimeoutExpired, ValueError):
            return None

    def current_pr(self, root: Path) -> dict | None:
        """The checkout's branch's PR — number, title, url, head and base names — or None."""
        return self._json(root, "pr", "view", "--json",
                          "number,title,url,headRefName,baseRefName")

    def repo_web_url(self, root: Path) -> str | None:
        """The checkout's repository's web root as the forge itself states it, or None."""
        return (self._json(root, "repo", "view", "--json", "url") or {}).get("url")

    @staticmethod
    def issue(number: int, slug: str | None, fields: str) -> dict:
        """Issue `number`'s `fields`, raising on any failure."""
        args = ["gh", "issue", "view", str(number), "--json", fields]
        if slug:
            args += ["-R", slug]
        out = subprocess.run(args, capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout)

    @staticmethod
    def review_threads(slug: str, number: int) -> dict:
        """`{"resolved": [first comment id, …], "open": n}` over a PR's review threads, or
        `{"error": …}` when GitHub did not answer."""
        owner, _, name = slug.partition("/")
        resolved, still_open, after = [], 0, None
        while True:
            args = ["gh", "api", "graphql", "-f", f"query={_REVIEW_THREADS_QUERY}",
                    "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={number}"]
            if after:
                args += ["-F", f"after={after}"]
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=20)
                page = json.loads(out.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
                return {"error": "GitHub did not answer"}
            for t in page["nodes"]:
                first = (t.get("comments") or {}).get("nodes") or [{}]
                if not first[0].get("databaseId"):
                    continue
                if t.get("isResolved"):
                    resolved.append(first[0]["databaseId"])
                else:
                    still_open += 1
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return {"resolved": resolved, "open": still_open}

    # CI: GitHub Actions, through `gh run`. Shell lines, run as `preflight.py` always ran
    # them; each returns the finished process and the caller reads it.
    #
    # A repository built elsewhere (CircleCI at Ludvig) has no Actions workflows at all: its
    # CI reports to GitHub as commit statuses (`ci/circleci: test`, the OAuth integration)
    # or as check runs (the CircleCI GitHub App). Those are read in the same run shape, so
    # the gate asks one question whichever CI built the commit — as Bitbucket's build
    # statuses already are.

    #: A commit status's `state` → (`status`, `conclusion`), in `gh run`'s words.
    _STATUS_STATES = {"success": ("completed", "success"), "failure": ("completed", "failure"),
                      "error": ("completed", "failure"), "pending": ("in_progress", "")}

    @staticmethod
    def _status_runs(sha: str) -> list[dict] | None:
        """Commit statuses and non-Actions check runs on `sha`, as `gh run list` rows. None
        when GitHub could not be asked (or does not know the commit: it is not pushed)."""
        st = _sh(f'gh api "repos/{{owner}}/{{repo}}/commits/{sha}/status"')
        ck = _sh(f'gh api "repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100"')
        if st.returncode != 0 and ck.returncode != 0:
            return None
        runs: list[dict] = []
        try:
            statuses = json.loads(st.stdout).get("statuses") or [] if st.returncode == 0 else []
            checks = json.loads(ck.stdout).get("check_runs") or [] if ck.returncode == 0 else []
        except (ValueError, AttributeError):
            return None
        for x in statuses:
            status, conclusion = GitHubForge._STATUS_STATES.get(x.get("state") or "",
                                                                ("in_progress", ""))
            runs.append({"databaseId": x.get("id"), "workflowName": x.get("context") or "?",
                         "status": status, "conclusion": conclusion, "headSha": sha,
                         "event": "push", "createdAt": x.get("created_at") or "",
                         "url": x.get("target_url") or ""})
        for x in checks:
            if ((x.get("app") or {}).get("slug") or "") == "github-actions":
                continue                 # an Actions job: `gh run list` already has it
            runs.append({"databaseId": x.get("id"), "workflowName": x.get("name") or "?",
                         "status": x.get("status") or "", "conclusion": x.get("conclusion") or "",
                         "headSha": sha, "event": "push",
                         "createdAt": x.get("started_at") or "", "url": x.get("html_url") or ""})
        return runs

    @staticmethod
    def _named(run: dict, workflow: str) -> bool:
        """A configured name matches a status by its whole context, or by the job after
        `ci/circleci: ` — so `"test"` names CircleCI's `ci/circleci: test`."""
        name = run.get("workflowName") or ""
        return name == workflow or name.endswith(": " + workflow)

    @staticmethod
    def ci_workflows() -> subprocess.CompletedProcess:
        return _sh("gh workflow list --json id,name,path,state")

    @staticmethod
    def ci_runs(sha: str, workflow: str | None, fields: str) -> subprocess.CompletedProcess:
        if workflow:
            mine = [r for r in GitHubForge._status_runs(sha) or [] if GitHubForge._named(r, workflow)]
            if mine:
                return subprocess.CompletedProcess(["gh"], 0, json.dumps(mine), "")
        sel = f' --workflow "{workflow}"' if workflow else ""
        actions = _sh(f'gh run list --commit "{sha}"{sel} --limit 50 --json {fields}')
        if workflow or actions.returncode != 0 or (actions.stdout or "").strip() not in ("", "[]"):
            return actions
        statuses = GitHubForge._status_runs(sha)
        return subprocess.CompletedProcess(["gh"], 0, json.dumps(statuses or []), "")

    @staticmethod
    def ci_run(run_id: int) -> subprocess.CompletedProcess:
        return _sh(f"gh run view {int(run_id)} --json jobs,event,headBranch")

    @staticmethod
    def ci_run_pr(run_id: int) -> str:
        """The number of the PR a `pull_request` run was for, or ""."""
        return _sh(f"gh api repos/{{owner}}/{{repo}}/actions/runs/{int(run_id)} "
                   "--jq '.pull_requests[0].number // empty'").stdout.strip()

    @staticmethod
    def pr_for_branch(branch: str) -> str:
        """The number of the newest PR from `branch`, open or not, or ""."""
        return _sh(f"gh pr list --head {shlex.quote(branch)} --state all "
                   "--limit 1 --json number --jq '.[0].number // empty'").stdout.strip()

    @staticmethod
    def ci_runs_of(sha: str) -> str:
        """The runs of `sha` as `gh` prints them (`databaseId,name,status,conclusion`)."""
        return subprocess.run(["gh", "run", "list", "--commit", sha, "--json",
                               "databaseId,name,status,conclusion"],
                              capture_output=True, text=True).stdout

    @staticmethod
    def ci_failed_log(run_id) -> str:
        """The log of a run's failed steps."""
        return subprocess.run(["gh", "run", "view", str(run_id), "--log-failed"],
                              capture_output=True, text=True).stdout


# --------------------------------------------------------------------------------------- #
# Bitbucket Server / Data Center (issue #4)
# --------------------------------------------------------------------------------------- #

#: A Bitbucket repository's web root: `<base>/projects/<KEY>/repos/<slug>`, or
#: `<base>/users/<user>/repos/<slug>` for a personal one. `<base>` may carry a context path
#: (`https://acme.com/bitbucket`), which is why it is matched lazily up to `/projects/`.
_BB_ROOT = (r"(?P<base>https?://[^/\s?#]+(?:/[^\s?#]*?)?)/(?P<kind>projects|users)/"
            r"(?P<owner>[^/\s?#]+)/repos/(?P<repo>[^/\s?#]+)")


def _bb_env(name: str) -> str:
    import os
    return (os.environ.get(name) or "").strip()


def _bb_configured_base() -> str:
    """`BITBUCKET_URL` without a trailing slash: the web base of the instance, needed when
    the `origin` is an ssh remote (whose host and port say nothing about the web's)."""
    return _bb_env("BITBUCKET_URL").rstrip("/")


class BitbucketPrApi:
    """Pushing the Review tab's piles as PR comments is not built for Bitbucket yet: its
    comment model (anchors, versions, tasks) is not GitHub's review API, and
    `push-pr-comments.py` is written against the latter. Every call says so plainly instead
    of sending a `gh` call to a repository that is not on GitHub."""

    WHY = ("pushing review comments to a Bitbucket pull request is not supported yet "
           "(human-review issue #4) — the page, its links and the CI gate are")

    @staticmethod
    def comments_path(repo: str, pr: int) -> str:
        return f"projects/{repo}/pull-requests/{pr}/comments"

    comment_path = reviews_path = review_path = commit_comments_path = \
        commit_comment_path = staticmethod(lambda *a: "")

    @staticmethod
    def shell(method: str, path: str, payload: dict) -> str:
        return f"# {BitbucketPrApi.WHY}"

    def _refuse(self, *_a, **_k):
        raise GhError(self.WHY)

    run = list = send = pr = repo = _refuse

    def login(self) -> str | None:
        return None


class BitbucketServerForge:
    """Bitbucket Server / Data Center (tested against the REST API 1.0 shapes of 8.x–10.x):
    its web URLs, its URL spellings, and its REST API.

    There is no CLI like `gh` to lean on, so the questions go over HTTPS with the reader's
    own HTTP access token, `BITBUCKET_TOKEN` (a personal one, *Manage account › HTTP access
    tokens*, read for the page, write for the merge-risk line on the PR description). An
    ssh `origin` says nothing about the web host, so `BITBUCKET_URL` names it; an https
    `origin` (`…/scm/KEY/repo.git`) needs neither.

    CI is whatever posts build statuses to the commit — Jenkins, through the Bitbucket
    Server Integration plugin, in the usual Atlassian setup. Each status key stands for a
    GitHub workflow, so the preflight gate waits on them unchanged."""

    name = "Bitbucket"
    #: How a reader fixes a refused login, in the words of a sentence ending "your …".
    auth_hint = "token (`BITBUCKET_TOKEN`)"

    def __init__(self, repo_url: str = ""):
        self.repo_url = (repo_url or "").rstrip("/")

    def __repr__(self) -> str:
        return f"BitbucketServerForge({self.repo_url!r})"

    # ---- the repository's coordinates ---------------------------------------------- #

    @property
    def _parts(self) -> dict:
        m = re.match(_BB_ROOT + r"/?$", self.repo_url)
        return m.groupdict() if m else {}

    @property
    def base(self) -> str:
        return self._parts.get("base") or _bb_configured_base()

    @property
    def slug(self) -> str:
        """`KEY/repo`, or `~user/repo` for a personal repository — the path the REST API
        and an ssh remote both use."""
        p = self._parts
        if not p:
            return ""
        owner = ("~" + p["owner"]) if p["kind"] == "users" else p["owner"]
        return f"{owner}/{p['repo']}"

    @staticmethod
    def _web_root(base: str, owner: str, repo: str) -> str:
        if owner.startswith("~"):
            return f"{base}/users/{owner[1:]}/repos/{repo}"
        return f"{base}/projects/{owner.upper()}/repos/{repo}"

    def of_slug(self, slug: str) -> "BitbucketServerForge":
        """The repository `KEY/repo` on the same instance as this one."""
        owner, _, repo = (slug or "").partition("/")
        return BitbucketServerForge(self._web_root(self.base, owner, repo))

    # ---- recognising its URLs ------------------------------------------------------- #

    @staticmethod
    def from_remote(remote: str) -> str | None:
        """The web root for an `origin` URL, or None when it is not a Bitbucket one:
        `https://host[/ctx]/scm/KEY/repo.git` (always), `ssh://git@host:7999/key/repo.git`
        (Bitbucket's own ssh port), or any remote on the host `BITBUCKET_URL` names."""
        r = remote.strip()
        m = re.match(r"^(?P<scheme>https?)://(?:[^@/]+@)?(?P<host>[^/]+)(?P<ctx>/.*?)?/scm/"
                     r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", r)
        if m:
            base = f"{m['scheme']}://{m['host']}{m['ctx'] or ''}"
            return BitbucketServerForge._web_root(base, m["owner"], m["repo"])
        m = (re.match(r"^ssh://(?:[^@/]+@)?(?P<host>[^/:]+)(?::(?P<port>\d+))?/"
                      r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$", r)
             or re.match(r"^(?:[^@/]+@)(?P<host>[^/:]+):(?P<port>)(?P<owner>[^/]+)/"
                         r"(?P<repo>[^/]+?)(?:\.git)?/?$", r))
        if not m:
            return None
        configured = _bb_configured_base()
        conf_host = re.sub(r"^https?://([^/:]+).*$", r"\1", configured) if configured else ""
        if configured and m["host"] == conf_host:
            return BitbucketServerForge._web_root(configured, m["owner"], m["repo"])
        if m["port"] == "7999":
            return BitbucketServerForge._web_root(configured or f"https://{m['host']}",
                                                  m["owner"], m["repo"])
        return None

    from_any_remote = from_remote

    @staticmethod
    def project_of(url: str) -> str:
        m = re.match(_BB_ROOT + r"(?:\.git)?/?$", url or "")
        if not m:
            return ""
        return ("~" if m["kind"] == "users" else "") + f"{m['owner']}/{m['repo']}"

    @staticmethod
    def is_pull_url(url: str) -> bool:
        return "/pull-requests/" in str(url or "")

    @staticmethod
    def pull_root(url: str) -> str | None:
        m = re.match(r"^(" + _BB_ROOT + r"/pull-requests/\d+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    def find_pull(text: str) -> tuple[str, int] | None:
        """`(repository web root, number)` of the first PR URL in `text`."""
        m = re.search(r"(" + _BB_ROOT + r")/pull-requests/(\d+)", str(text or ""))
        return (m.group(1), int(m.groups()[-1])) if m else None

    @classmethod
    def pull_in(cls, text):
        hit = cls.find_pull(text)
        return (cls(hit[0]), hit[1]) if hit else None

    @staticmethod
    def is_comment_url(url: str) -> bool:
        return bool(re.match(r"^" + _BB_ROOT + r"/pull-requests/\d+([/?#]\S*)?$", url or ""))

    @staticmethod
    def is_commit_comment_url(url: str) -> bool:
        return bool(re.match(r"^" + _BB_ROOT + r"/commits/[0-9a-f]{7,40}\?commentId=\d+$",
                             str(url or "")))

    @staticmethod
    def find_issue(text: str) -> tuple[str, int] | None:
        """Bitbucket has no issues; tickets live in Jira, which is not read yet."""
        return None

    @classmethod
    def issue_in(cls, text):
        return None

    # ---- its web URLs --------------------------------------------------------------- #

    @staticmethod
    def _at(ref: str) -> str:
        """A ref for `?at=`: a commit as it is, a branch as `refs/heads/<name>` (the
        caller has already quoted a branch name; its slashes are quoted here too)."""
        ref = str(ref)
        if re.fullmatch(r"[0-9a-f]{7,40}", ref) or ref.startswith(("refs/", "refs%2F")):
            return ref.replace("/", "%2F")
        return "refs%2Fheads%2F" + ref.replace("/", "%2F")

    def blob_prefix(self, ref: str) -> str:
        """Every file is this plus `/<path>`; the ref goes after the path, in `blob_query`."""
        return f"{self.repo_url}/browse"

    def blob_query(self, ref: str) -> str:
        return f"?at={self._at(ref)}"

    #: How a line, or a range, is written after `#`: `#57`, `#57-59`.
    line_anchor = staticmethod(lambda line, end=None:
                               f"#{line}" + (f"-{end}" if end and end > line else ""))

    def blob_url(self, ref: str, path: str, line: int | None = None,
                 end: int | None = None) -> str:
        url = f"{self.repo_url}/browse/{path}{self.blob_query(ref)}"
        return url + (self.line_anchor(line, end) if line else "")

    def tree_url(self, ref: str) -> str:
        return f"{self.repo_url}/browse{self.blob_query(ref)}"

    def commit_url(self, sha: str) -> str:
        return f"{self.repo_url}/commits/{sha}"

    def compare_url(self, base: str, head: str) -> str:
        """What `head` adds over `base`, both already quoted for a URL."""
        return (f"{self.repo_url}/compare/diff?sourceBranch={self._at(head)}"
                f"&targetBranch={self._at(base)}")

    def issue_url(self, number) -> str:
        """None to link to: Bitbucket has no issue tracker of its own."""
        return ""

    def avatar_url(self, login: str) -> str:
        return f"{self.base}/users/{login}/avatar.png?s=48"

    # ---- asked over its REST API ---------------------------------------------------- #

    @staticmethod
    def available() -> bool:
        return bool(_bb_env("BITBUCKET_TOKEN"))

    def http(self, method: str, path: str, data: dict | None = None,
             timeout: float = 20) -> dict:
        """One REST call under `<base>/rest/`, the token the reader's own. Raises `GhError`
        (its `status` the HTTP one) on a refusal and on no answer; `{}` for an empty body."""
        import urllib.error
        import urllib.request
        url = f"{self.base}/rest/{path.lstrip('/')}"
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(data).encode() if data is not None else None)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = _bb_env("BITBUCKET_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise GhError(f"Bitbucket {method} {path}: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise GhError(f"Bitbucket {method} {path}: {e} (timed out or unreachable)") from None
        return json.loads(body) if body.strip() else {}

    def _pages(self, path: str, limit: int = 1000) -> list[dict]:
        out, start = [], 0
        sep = "&" if "?" in path else "?"
        while len(out) < limit:
            page = self.http("GET", f"{path}{sep}start={start}&limit=100")
            out += page.get("values") or []
            if page.get("isLastPage", True) or page.get("nextPageStart") is None:
                break
            start = page["nextPageStart"]
        return out

    def _repo_api(self) -> str:
        return f"api/latest/projects/{self.slug.partition('/')[0]}/repos/{self.slug.partition('/')[2]}"

    def _here(self, root: Path | None = None, slug: str | None = None) -> "BitbucketServerForge":
        """The forge for this question: `slug` on this instance, else `root`'s origin, else
        the working directory's, else this one."""
        if slug:
            return self.of_slug(slug)
        if self.repo_url:
            return self
        found = forge_for(Path(root or ".").resolve())
        return found if isinstance(found, BitbucketServerForge) else self

    @staticmethod
    def _pr_shape(pr: dict) -> dict:
        """A Bitbucket PR in the words `gh pr view --json` uses, which every caller reads."""
        link = ((pr.get("links") or {}).get("self") or [{}])[0].get("href") or ""
        return {"number": pr.get("id"), "title": pr.get("title") or "", "url": link,
                "body": pr.get("description") or "",
                "headRefName": (pr.get("fromRef") or {}).get("displayId") or "",
                "baseRefName": (pr.get("toRef") or {}).get("displayId") or "",
                "headRefOid": (pr.get("fromRef") or {}).get("latestCommit") or ""}

    def _branch_prs(self, branch: str, state: str) -> list[dict]:
        ref = _quote_all("refs/heads/" + branch)
        return self.http("GET", f"{self._repo_api()}/pull-requests?at={ref}"
                                f"&direction=OUTGOING&state={state}&order=NEWEST&limit=1"
                         ).get("values") or []

    def current_pr(self, root: Path) -> dict | None:
        """The checkout's branch's open PR — number, title, url, head and base names."""
        forge = self._here(root)
        branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        if not forge.slug or not branch or branch == "HEAD" or not self.available():
            return None
        try:
            prs = forge._branch_prs(branch, "OPEN")
        except (GhError, ValueError):
            return None
        if not prs:
            return None
        shaped = self._pr_shape(prs[0])
        return {k: shaped[k] for k in ("number", "title", "url", "headRefName", "baseRefName")}

    def repo_web_url(self, root: Path) -> str | None:
        return self._here(root).repo_url or None

    def pr_body(self, number, slug: str | None, root: Path | None) -> dict:
        got = self._pr_shape(self._here(root, slug).http(
            "GET", f"{self._here(root, slug)._repo_api()}/pull-requests/{int(number)}",
            timeout=15))
        return {k: got[k] for k in ("number", "body", "url")}

    def update_pr_body(self, number, body: str, slug: str | None, root: Path | None) -> None:
        """Replace a PR's description. Bitbucket refuses an update without the version it
        is replacing, and resets what a PUT leaves out on some versions, so the title and
        the reviewers are sent back as they were read."""
        forge = self._here(root, slug)
        path = f"{forge._repo_api()}/pull-requests/{int(number)}"
        pr = forge.http("GET", path, timeout=15)
        forge.http("PUT", path, {
            "version": pr.get("version", 0), "title": pr.get("title") or "",
            "description": body,
            "reviewers": [{"user": {"name": (r.get("user") or {}).get("name")}}
                          for r in pr.get("reviewers") or [] if (r.get("user") or {}).get("name")],
        }, timeout=30)

    def update_pr_body_shell(self, number, slug: str | None, path) -> str:
        forge = self._here(None, slug)
        url = f"{forge.base}/rest/{forge._repo_api()}/pull-requests/{int(number)}"
        auth = '-H "Authorization: Bearer $BITBUCKET_TOKEN"'
        return (f"curl -sS {auth} {shlex.quote(url)} | jq --rawfile d {shlex.quote(str(path))} "
                "'{version, title, description: $d, reviewers: [.reviewers[] | {user: "
                "{name: .user.name}}]}' | curl -sS -X PUT " + auth
                + f" -H 'Content-Type: application/json' --data @- {shlex.quote(url)}")

    def pr_title_here(self) -> None:
        pr = self.current_pr(Path(".").resolve())
        if pr and pr.get("title"):
            print(pr["title"])

    @staticmethod
    def pr_api() -> BitbucketPrApi:
        return BitbucketPrApi()

    @staticmethod
    def issue(number, slug: str | None, fields: str) -> dict:
        raise GhError("Bitbucket has no issues, and reading a Jira ticket is not supported "
                      "yet (human-review issue #4) — give the requirement as `ticket:` text")

    def review_threads(self, slug: str, number: int) -> dict:
        """`{"resolved": [first comment id, …], "open": n}` over a PR's top-level
        comments — a thread is resolved when Bitbucket says so (`threadResolved`, 8.x) or
        it is a task marked done — or `{"error": …}`."""
        forge = self._here(None, slug)
        try:
            acts = forge._pages(f"{forge._repo_api()}/pull-requests/{int(number)}/activities")
        except (GhError, ValueError):
            return {"error": "Bitbucket did not answer"}
        resolved, still_open = [], 0
        for a in acts:
            c = a.get("comment") if a.get("action") == "COMMENTED" else None
            if not c or not c.get("id"):
                continue
            if c.get("threadResolved") or c.get("state") == "RESOLVED":
                resolved.append(c["id"])
            else:
                still_open += 1
        return {"resolved": resolved, "open": still_open}

    # CI: the build statuses on a commit (Jenkins, Bamboo, anything that posts them), in
    # the shape `gh run list --json` prints, so `preflight.py` reads them unchanged. A
    # status key is a workflow; its id is a stable number made from the key and the commit.

    #: Bitbucket's states as GitHub's (status, conclusion).
    _STATES = {"SUCCESSFUL": ("completed", "success"), "FAILED": ("completed", "failure"),
               "CANCELLED": ("completed", "cancelled"), "INPROGRESS": ("in_progress", ""),
               "UNKNOWN": ("pending", "")}
    _runs: dict = {}

    def _statuses(self, sha: str) -> list[dict]:
        return self._pages(f"build-status/1.0/commits/{sha}", limit=500)

    def _as_runs(self, sha: str) -> list[dict]:
        import zlib
        runs = []
        for s in self._statuses(sha):
            key = s.get("key") or s.get("name") or "?"
            status, conclusion = self._STATES.get(s.get("state") or "", ("pending", ""))
            when = s.get("dateAdded")
            created = (_dt_iso(when) if isinstance(when, (int, float)) else str(when or ""))
            run = {"databaseId": zlib.crc32(f"{key}@{sha}".encode()), "key": key,
                   "name": s.get("name") or key, "workflowName": s.get("name") or key,
                   "status": status, "conclusion": conclusion, "headSha": sha,
                   "event": "push", "createdAt": created, "url": s.get("url") or "",
                   "headBranch": "", "attempt": 1}
            BitbucketServerForge._runs[run["databaseId"]] = run
            runs.append(run)
        return runs

    @staticmethod
    def _done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["bitbucket"], code, stdout, stderr)

    def ci_workflows(self) -> subprocess.CompletedProcess:
        """The build keys seen on the branch's last commits: what CI this repository has.
        None seen is "no CI", as an empty `gh workflow list` is."""
        forge = self._here()
        try:
            shas = [c["id"] for c in forge.http(
                "GET", f"{forge._repo_api()}/commits?limit=10").get("values") or []]
            seen: dict[str, dict] = {}
            for sha in shas:
                for s in forge._statuses(sha):
                    key = s.get("key") or s.get("name") or "?"
                    seen.setdefault(key, {"id": key, "name": s.get("name") or key,
                                          "path": key, "state": "active"})
                if seen:
                    break
        except (GhError, ValueError, KeyError) as e:
            return self._done(code=1, stderr=str(e))
        return self._done(json.dumps(list(seen.values())))

    def ci_runs(self, sha: str, workflow: str | None, fields: str) -> subprocess.CompletedProcess:
        try:
            runs = self._here()._as_runs(sha)
        except (GhError, ValueError) as e:
            return self._done(code=1, stderr=str(e))
        if workflow:
            runs = [r for r in runs if workflow in (r["key"], r["name"], Path(r["key"]).name)]
        return self._done(json.dumps(runs))

    def ci_run(self, run_id: int) -> subprocess.CompletedProcess:
        run = self._runs.get(int(run_id))
        if not run:
            return self._done(code=1, stderr=f"build {run_id} not seen in this process")
        return self._done(json.dumps({"jobs": [], "event": run["event"],
                                      "headBranch": run["headBranch"]}))

    @staticmethod
    def ci_run_pr(run_id: int) -> str:
        return ""

    def pr_for_branch(self, branch: str) -> str:
        try:
            prs = self._here()._branch_prs(branch, "ALL")
        except (GhError, ValueError):
            return ""
        return str(prs[0]["id"]) if prs else ""

    def ci_runs_of(self, sha: str) -> str:
        try:
            return json.dumps(self._here()._as_runs(sha))
        except (GhError, ValueError):
            return ""

    def ci_failed_log(self, run_id) -> str:
        """A Jenkins build's console text, when the status links to one and `JENKINS_USER`
        / `JENKINS_TOKEN` let it be read; "" otherwise."""
        import base64
        import urllib.request
        run = self._runs.get(int(run_id)) or {}
        url = (run.get("url") or "").rstrip("/")
        if not url or run.get("conclusion") != "failure":
            return ""
        req = urllib.request.Request(url + "/consoleText")
        user, token = _bb_env("JENKINS_USER"), _bb_env("JENKINS_TOKEN")
        if user and token:
            req.add_header("Authorization", "Basic "
                           + base64.b64encode(f"{user}:{token}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except OSError:
            return ""


def _quote_all(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def _dt_iso(millis: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(millis / 1000, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------- #
# GitLab, gitlab.com or self-managed (issue #4)
# --------------------------------------------------------------------------------------- #

_GL_REMOTE = re.compile(r"^(?:(?P<scheme>https?)://(?:[^@/]+@)?(?P<web>[^/\s]+)/"
                        r"|ssh://(?:[^@/]+@)?(?P<ssh>[^/:\s]+)(?::\d+)?/"
                        r"|[^@/\s]+@(?P<scp>[^/:\s]+):)"
                        r"(?P<path>[^\s?#]+?)(?:\.git)?/?$")
_GL_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
#: Everything after a project's path in a web URL: `/-/` starts GitLab's own routes, which is
#: what tells `group/sub/repo` from a page of it, however deep the groups go.
_GL_ROOT = r"(?P<root>https?://[A-Za-z0-9.-]+(?::\d+)?/[\w.~%+/-]+?)/-/"


def _gl_configured_base() -> str:
    """The instance glab is pointed at, `GITLAB_HOST` or its shorter `GL_HOST`, as a web base
    with no trailing slash. glab takes either a host or a URL there."""
    import os
    base = (os.environ.get("GITLAB_HOST") or os.environ.get("GL_HOST") or "").strip().rstrip("/")
    return base if not base or "://" in base else f"https://{base}"


def _gl_host_of(url: str) -> str:
    """The host of a URL, or of a bare `host[:port]`, without its port."""
    return re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].lower()


def _gl_is_named(host: str) -> bool:
    """Whether a host is known for GitLab, not guessed: gitlab.com, or the instance named by
    `GITLAB_HOST`."""
    host = _gl_host_of(host)
    configured = _gl_configured_base()
    return host == "gitlab.com" or (bool(configured) and host == _gl_host_of(configured))


def _gl_is_host(host: str) -> bool:
    """Whether a host is GitLab's: named (`_gl_is_named`), or with a label that says so
    (`gitlab.acme.com`, `gitlab-int.acme.com`, but not `gitlabels.acme.com`). A self-managed
    instance called anything else is reached by setting `GITLAB_HOST`."""
    return _gl_is_named(host) or any(re.fullmatch(r"gitlab(?:[-_\d].*)?", label)
                                     for label in _gl_host_of(host).split("."))


class _GlDiff:
    """One file's unified diff, read the way a position on GitLab has to be written: a line
    the diff did not add needs its old number as well, or GitLab answers 400, and a multi-line
    comment names both numbers of its ends. Unchanged lines outside every hunk are numbered
    too, which GitLab accepts as an expanded line. `positions` is GitHub's diff position (the
    line under the first `@@` is 1, later `@@` count) for each new line, which is what a
    commit comment's payload is planned in."""

    def __init__(self, text: str):
        self.added: set[int] = set()
        self.old_at: dict[int, int] = {}
        self.positions: dict[int, int] = {}
        self.shifts: list[tuple[int, int]] = []     # (new line, new - old from there on)
        old = new = pos = 0
        for line in (text or "").split("\n"):
            m = _GL_HUNK.match(line)
            if m:
                pos += 1 if self.shifts else 0
                old, new = int(m[1]), int(m[2])
                self.shifts.append((new, new - old))
                continue
            if not self.shifts or not line or line.startswith("\\"):
                continue
            pos += 1
            if line.startswith("+"):
                self.added.add(new)
                self.old_at[new], self.positions[new] = old, pos
                new += 1
            elif line.startswith("-"):
                old += 1
            else:
                self.old_at[new], self.positions[new] = old, pos
                old, new = old + 1, new + 1
            self.shifts.append((new, new - old))

    def old_of(self, new: int) -> int:
        """The old file's number at new line `new`: a line's own when it was kept, and for an
        added line the one GitLab's `line_code` carries, the next old line."""
        if new in self.old_at:
            return self.old_at[new]
        return new - next((d for at, d in reversed(self.shifts) if at <= new), 0)

    def line_at(self, position: int) -> int | None:
        return next((n for n, p in self.positions.items() if p == position), None)


class GitLabPrApi:
    """A merge request's comments, spoken in GitHub's words, because `push-pr-comments.py`
    plans its calls in them: paths, `list` and `send`, with `glab api` underneath so the
    credentials are the reader's own. What differs is translated here and only here:

    - A GitHub review (one summary and its line comments in one call) has no twin: its body
      becomes a note on the MR, and each comment a discussion. `settle` and `replan` already
      treat a review as something that may have landed in part, so nothing depends on one
      call being atomic. The "continued" summaries of later batches are dropped: they exist
      for GitHub's 10 seconds, and on an MR they would only be noise.
    - GitHub addresses a comment by its id alone, GitLab by its project, MR or commit, and
      discussion too, so `list` remembers each note's path for `comment_path` & co. A plan
      is always made from a listing, which is what makes that safe.
    - A comment's place is a `position` with the diff's three refs and both line numbers,
      built from GitLab's own diff (`_GlDiff`)."""

    def __init__(self, forge: "GitLabForge"):
        self.forge = forge
        self.notes: dict[int, str] = {}
        self.diffs: dict[str, tuple[dict, dict]] = {}

    # The REST paths the push plans its calls against. Comments and reviews both read from
    # `/discussions`; the last word tells a line comment from an MR's general note.
    @staticmethod
    def _project(repo: str) -> str:
        return f"projects/{_quote_all(repo)}"

    def comments_path(self, repo: str, pr: int) -> str:
        return f"{self._project(repo)}/merge_requests/{pr}/discussions"

    def reviews_path(self, repo: str, pr: int) -> str:
        return f"{self._project(repo)}/merge_requests/{pr}/notes"

    def commit_comments_path(self, repo: str, sha: str) -> str:
        return f"{self._project(repo)}/repository/commits/{sha}/discussions"

    def comment_path(self, repo: str, comment_id) -> str:
        return self.notes[int(comment_id)]

    commit_comment_path = comment_path

    def review_path(self, repo: str, pr: int, review_id) -> str:
        return self.notes[int(review_id)]

    @staticmethod
    def shell(method: str, path: str, payload: dict) -> str:
        """The call as a reader could paste it — what a dry run prints. A review is several
        calls on GitLab, and says so."""
        verb = "PUT" if method == "PATCH" else method
        n = len(payload.get("comments") or [])
        head = (f"# on GitLab: this note, then one discussion for each of the {n} comments\n"
                if path.endswith("/notes") and n else "")
        return (f"{head}glab api -X {verb} {path} --input - <<'JSON'\n"
                + json.dumps(payload, indent=2, ensure_ascii=False) + "\nJSON")

    def run(self, args: list[str], data: dict | None = None) -> str:
        r = subprocess.run(["glab", *args], input=json.dumps(data) if data is not None else None,
                           capture_output=True, text=True)
        if r.returncode:
            raise GhError(f"glab {' '.join(args[:4])}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def login(self) -> str | None:
        try:
            return self.forge.api("GET", "user").get("username") or None
        except (RuntimeError, ValueError):
            return None

    def _web(self, container: str) -> str:
        """The page a note's `#note_<id>` hangs off."""
        import urllib.parse
        m = re.match(r"projects/([^/]+)/(?:merge_requests/(\d+)|repository/commits/(\w+))",
                     container)
        forge = self.forge.of_slug(urllib.parse.unquote(m[1]))
        return f"{forge.repo_url}/-/merge_requests/{m[2]}" if m[2] else forge.commit_url(m[3])

    def list(self, path: str) -> list[dict]:
        """The notes under `path`, in the shape GitHub lists them: the first note of each
        discussion, its replies being no pipeline's. An MR's `/discussions` are its line
        comments, its `/notes` the general ones; a commit's are all of its comments."""
        container, _, kind = path.rpartition("/")
        is_mr = "/merge_requests/" in container
        web, out = self._web(container), []
        head = None
        for d in self.forge._pages(f"{container}/discussions"):
            n = next((x for x in d.get("notes") or [] if not x.get("system")), None)
            if n is None:
                continue
            pos = n.get("position") or {}
            self.notes[n["id"]] = f"{container}/discussions/{d['id']}/notes/{n['id']}"
            if is_mr and bool(pos) != (kind == "discussions"):
                continue
            shaped = {"id": n["id"], "body": n.get("body") or "",
                      "html_url": f"{web}#note_{n['id']}",
                      "user": {"login": (n.get("author") or {}).get("username")}}
            if pos:
                first = ((pos.get("line_range") or {}).get("start") or {}).get("new_line")
                line = pos.get("new_line")
                shaped.update(path=pos.get("new_path"), commit_id=pos.get("head_sha"),
                              side="RIGHT")
                if is_mr and head is None:
                    head = self._diff_of(container)[0].get("head_sha") or ""
                # GitLab carries a note along with the diff, its `head_sha` with it, and
                # leaves one whose line a later commit changed at the old head and the old
                # number. Outdated, then: GitHub says so by having no `line`, and the push
                # reads that to post the comment again where it now belongs.
                outdated = is_mr and head and pos.get("head_sha") != head
                if pos.get("position_type") == "file":
                    shaped["subject_type"] = "file"
                elif not outdated:
                    shaped["line"] = line
                    if first and first != line:
                        shaped["start_line"] = first
                if not is_mr and line:
                    _, files = self._diff_of(container)
                    shaped["position"] = self._file(files, pos.get("new_path"))[1] \
                        .positions.get(line)
            out.append(shaped)
        return out

    def _diff_of(self, container: str) -> tuple[dict, dict]:
        """`(the three refs, {new path: (old path, _GlDiff)})` of an MR or a commit."""
        if container not in self.diffs:
            got = self.forge.api("GET", container)
            if "/merge_requests/" in container:
                refs, files = got.get("diff_refs") or {}, f"{container}/diffs"
            else:
                parent = (got.get("parent_ids") or [None])[0]
                refs = {"base_sha": parent, "start_sha": parent, "head_sha": got.get("id")}
                files = f"{container}/diff"
            self.diffs[container] = (refs, {
                f.get("new_path"): (f.get("old_path"), _GlDiff(f.get("diff") or ""))
                for f in self.forge._pages(files)})
        return self.diffs[container]

    @staticmethod
    def _file(files: dict, path: str) -> tuple[str, _GlDiff]:
        return files.get(path) or (path, _GlDiff(""))

    def _position(self, container: str, c: dict) -> dict | None:
        """Where comment `c` sits, for GitLab; None for one with no path, a plain note."""
        path = c.get("path")
        if not path:
            return None
        refs, files = self._diff_of(container)
        old_path, diff = self._file(files, path)
        pos = {**refs, "old_path": old_path, "new_path": path, "position_type": "text"}
        line = c.get("line") or diff.line_at(c.get("position") or 0)
        if c.get("subject_type") == "file" or not line:
            return {**pos, "position_type": "file"}
        pos["new_line"] = line
        if line not in diff.added:
            pos["old_line"] = diff.old_of(line)
        start = c.get("start_line")
        if start and start != line:
            sha = hashlib.sha1(path.encode()).hexdigest()

            def end(n: int) -> dict:
                added = n in diff.added
                return {"line_code": f"{sha}_{diff.old_of(n)}_{n}",
                        "type": "new" if added else None,
                        "old_line": None if added else diff.old_of(n), "new_line": n}
            pos["line_range"] = {"start": end(start), "end": end(line)}
        return pos

    def _discussion(self, path: str, c: dict) -> dict:
        container = path.rpartition("/")[0]
        where = self._position(container, c)
        return self.forge.api("POST", f"{container}/discussions",
                              {"body": c["body"], **({"position": where} if where else {})},
                              timeout=30)

    def send(self, call) -> dict:
        """`call`: anything with `method`, `path` and `payload` (push-pr-comments' `Call`)."""
        path, data = call.path, call.payload
        if call.method == "DELETE":
            return self.forge.api("DELETE", path) or {}
        if call.method in ("PATCH", "PUT"):
            return self.forge.api("PUT", path, {"body": data["body"]}, timeout=30)
        if not path.endswith("/notes"):
            return self._discussion(path, data)
        body = data.get("body") or ""
        if body and "hr:review-more" not in body:
            self.forge.api("POST", path, {"body": body}, timeout=30)
        for c in data.get("comments") or []:
            self._discussion(path, c)
        return {}

    def pr(self, root: Path, pr: int | None) -> dict:
        forge = self.forge._here(root)
        if not pr:
            found = forge.current_pr(root)
            if not found:
                raise RuntimeError("no merge request for this branch")
            pr = found["number"]
        got = forge._mr_shape(forge.api("GET", f"{forge._project}/merge_requests/{int(pr)}"))
        return {k: got[k] for k in ("number", "url", "headRefOid", "baseRefName", "headRefName")}

    def repo(self, root: Path) -> str:
        slug = self.forge._here(root).slug
        if not slug:
            raise RuntimeError("this checkout's origin is not on GitLab")
        return slug


class GitLabForge:
    """GitLab, gitlab.com or self-managed: its web URLs, its URL spellings, and `glab`.

    Every question goes through `glab api`, so the credentials are the reader's own
    (`glab auth login`, or `GITLAB_TOKEN`) and no token is seen here. `repo_url` is the
    project's web root; a project may sit in nested groups, so `slug` is `group/sub/repo`
    and only a URL's `/-/` tells where it ends. A pipeline is the one CI run of a commit:
    GitLab has no separate workflows, so `.gitlab-ci.yml` stands for all of them and a job
    is what a GitHub step would be."""

    name = "GitLab"
    #: How a reader fixes a refused login, in the words of a sentence ending "your …".
    auth_hint = "glab login (run `glab auth status`)"

    def __init__(self, repo_url: str = ""):
        self.repo_url = (repo_url or "").rstrip("/")

    def __repr__(self) -> str:
        return f"GitLabForge({self.repo_url!r})"

    # ---- the project's coordinates -------------------------------------------------- #

    @property
    def base(self) -> str:
        """The instance's web base. The configured one when this project is on it, since
        only that can name a relative URL root (`https://acme.com/gitlab`)."""
        configured = _gl_configured_base()
        if configured and (not self.repo_url or self.repo_url.startswith(configured + "/")):
            return configured
        m = re.match(r"^https?://[^/]+", self.repo_url)
        return m.group(0) if m else "https://gitlab.com"

    @property
    def slug(self) -> str:
        """`group/sub/repo`, what `glab -R` and the REST API's project path both take."""
        return self.repo_url[len(self.base):].strip("/") if self.repo_url else ""

    @property
    def _project(self) -> str:
        return f"projects/{_quote_all(self.slug)}"

    def of_slug(self, slug: str) -> "GitLabForge":
        """The project `group/repo` on the same instance as this one."""
        return GitLabForge(f"{self.base}/{(slug or '').strip('/')}")

    # ---- recognising its URLs ------------------------------------------------------- #

    @staticmethod
    def from_remote(remote: str) -> str | None:
        """The web root for an `origin` URL on a GitLab host (`_gl_is_host`), or None: https
        (a user allowed), ssh, or scp-style, with any depth of groups. An ssh remote says
        nothing of the web's scheme or port, so `GITLAB_HOST` wins for its host."""
        m = _GL_REMOTE.match(remote.strip())
        if not m or "/" not in m["path"]:
            return None
        host = m["web"] or m["ssh"] or m["scp"]
        if not _gl_is_host(host):
            return None
        # A host's name is only a guess, and `/scm/` or Bitbucket's ssh port is a fact.
        if not _gl_is_named(host) and BitbucketServerForge.from_remote(remote):
            return None
        configured = _gl_configured_base()
        on_it = configured and _gl_host_of(configured) == _gl_host_of(host)
        path = m["path"]
        if on_it:
            prefix = re.sub(r"^https?://[^/]+/?", "", configured)
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix) + 1:]
            return f"{configured}/{path}"
        return f"{m['scheme']}://{m['web']}/{path}" if m["web"] else f"https://{host}/{path}"

    from_any_remote = from_remote

    @staticmethod
    def project_of(url: str) -> str:
        """`group/sub/repo` when `url` is a project's web root on a GitLab host, else ""."""
        m = re.match(r"^https?://([^/\s?#]+)/([^\s?#]+?)(?:\.git)?/?$", url or "")
        if not m or "/" not in m[2] or "/-/" in m[2] or not _gl_is_host(m[1]):
            return ""
        return GitLabForge(f"{url}".rstrip("/").removesuffix(".git")).slug

    @staticmethod
    def is_pull_url(url: str) -> bool:
        return "/-/merge_requests/" in str(url or "")

    @staticmethod
    def pull_root(url: str) -> str | None:
        m = re.match(r"^(" + _GL_ROOT + r"merge_requests/\d+)", url or "")
        return m.group(1) if m else None

    @staticmethod
    def find_pull(text: str) -> tuple[str, int] | None:
        """`(project web root, number)` of the first MR URL in `text`."""
        m = re.search(_GL_ROOT + r"merge_requests/(\d+)", str(text or ""))
        return (m["root"], int(m.groups()[-1])) if m else None

    @classmethod
    def pull_in(cls, text):
        hit = cls.find_pull(text)
        return (cls(hit[0]), hit[1]) if hit else None

    @staticmethod
    def is_comment_url(url: str) -> bool:
        return bool(re.match(r"^" + _GL_ROOT + r"merge_requests/\d+([/?#]\S*)?\Z", url or ""))

    @staticmethod
    def is_commit_comment_url(url: str) -> bool:
        return bool(re.match(r"^" + _GL_ROOT + r"commit/[0-9a-f]{7,40}#note_\d+\Z",
                             str(url or "")))

    @staticmethod
    def find_issue(text: str) -> tuple[str, int] | None:
        """`(project web root, number)` when `text` starts with an issue URL, in either of
        the spellings GitLab has (`/-/issues/`, and `/-/work_items/` on recent versions)."""
        m = re.match(_GL_ROOT + r"(?:issues|work_items)/(\d+)\b", str(text or ""), re.I)
        return (m["root"], int(m.groups()[-1])) if m else None

    @classmethod
    def issue_in(cls, text):
        hit = cls.find_issue(text)
        return (cls(hit[0]), hit[1]) if hit else None

    # ---- its web URLs --------------------------------------------------------------- #

    def blob_prefix(self, ref: str) -> str:
        """Every file at `ref` is this plus `/<path>`."""
        return f"{self.repo_url}/-/blob/{ref}"

    @staticmethod
    def blob_query(ref: str) -> str:
        return ""

    #: How a line, or a range, is written after `#`: `#L57`, `#L57-59`.
    line_anchor = staticmethod(lambda line, end=None:
                               f"#L{line}" + (f"-{end}" if end and end > line else ""))

    def blob_url(self, ref: str, path: str, line: int | None = None,
                 end: int | None = None) -> str:
        url = f"{self.repo_url}/-/blob/{ref}/{path}"
        return url + (self.line_anchor(line, end) if line else "")

    def tree_url(self, ref: str) -> str:
        return f"{self.repo_url}/-/tree/{ref}"

    def commit_url(self, sha: str) -> str:
        return f"{self.repo_url}/-/commit/{sha}"

    def compare_url(self, base: str, head: str) -> str:
        """The diff between two refs, both already quoted for a URL."""
        return f"{self.repo_url}/-/compare/{base}...{head}"

    def issue_url(self, number) -> str:
        return f"{self.repo_url}/-/issues/{number}"

    def avatar_url(self, login: str) -> str:
        """The URL GitLab gives for a user's avatar. It has no address that can be built
        from a login alone, so it is asked for; "" when it does not say, and `_avatar`
        draws an initial instead."""
        try:
            users = self._here().api("GET", f"users?username={_quote_all(login)}")
        except (GhError, ValueError):
            return ""
        return ((users or [{}])[0].get("avatar_url") or "") if isinstance(users, list) else ""

    # ---- asked over `glab api` ------------------------------------------------------ #

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("glab"))

    def _glab_env(self) -> dict:
        """The environment of a `glab` call. The instance goes in as `GITLAB_HOST`, relative
        URL root included, because `--hostname` refuses a `host:port` and takes no path.

        That variable is also what makes glab send a `GITLAB_TOKEN` to a host, and this one
        comes from the checkout's `origin`: so a token in the environment stays out of the
        call unless it is for gitlab.com or the reader named this instance. A token exported
        for gitlab.com must not travel to whatever host a cloned repository points at. glab's
        own stored login for that host, which is per host, still applies."""
        import os
        env = {**os.environ, "GITLAB_HOST": re.sub(r"^https?://", "", self.base)}
        if not _gl_is_named(self.base):
            for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
                env.pop(name, None)
        return env

    def glab(self, method: str, path: str, data: dict | None = None,
             timeout: float = 20) -> str:
        """One REST call under `api/v4/`: the one network point. Raises `GhError` (its
        `status` the HTTP one) on a refusal and on no answer."""
        args = ["glab", "api", "-X", method, path]
        if data is not None:
            args += ["--input", "-"]
        try:
            r = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                               capture_output=True, text=True, timeout=timeout,
                               env=self._glab_env())
        except subprocess.TimeoutExpired:
            raise GhError(f"GitLab {method} {path}: timed out") from None
        except OSError as e:
            raise GhError(f"GitLab {method} {path}: {e}") from None
        if r.returncode:
            raise GhError(f"GitLab {method} {path}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def api(self, method: str, path: str, data: dict | None = None, timeout: float = 20):
        """`glab` with its JSON answer read; `{}` for an empty body."""
        out = self.glab(method, path, data, timeout)
        return json.loads(out) if out.strip() else {}

    def _pages(self, path: str, limit: int = 2000) -> list[dict]:
        """Every page of a list, by page number: `glab api --paginate` prints its pages as
        separate documents, and only the page number works on all of GitLab's list routes."""
        out: list[dict] = []
        sep = "&" if "?" in path else "?"
        page = 1
        while len(out) < limit:
            got = self.api("GET", f"{path}{sep}per_page=100&page={page}")
            out += got if isinstance(got, list) else []
            if not isinstance(got, list) or len(got) < 100:
                break
            page += 1
        return out

    def _here(self, root: Path | None = None, slug: str | None = None) -> "GitLabForge":
        """The forge for this question: `slug` on this instance, else this one, else
        `root`'s origin, else the working directory's."""
        if slug:
            return self.of_slug(slug)
        if self.repo_url:
            return self
        found = forge_for(Path(root or ".").resolve())
        return found if isinstance(found, GitLabForge) else self

    @staticmethod
    def _mr_shape(mr: dict) -> dict:
        """A merge request in the words `gh pr view --json` uses, which every caller reads."""
        return {"number": mr.get("iid"), "title": mr.get("title") or "",
                "url": mr.get("web_url") or "", "body": mr.get("description") or "",
                "headRefName": mr.get("source_branch") or "",
                "baseRefName": mr.get("target_branch") or "",
                "headRefOid": mr.get("sha") or ""}

    def _branch_mrs(self, branch: str, state: str) -> list[dict]:
        got = self.api("GET", f"{self._project}/merge_requests?source_branch="
                              f"{_quote_all(branch)}&state={state}"
                              "&order_by=created_at&sort=desc&per_page=1")
        return got if isinstance(got, list) else []

    def current_pr(self, root: Path) -> dict | None:
        """The checkout's branch's open MR — number, title, url, head and base names."""
        forge = self._here(root)
        branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        if not forge.slug or not branch or not self.available():
            return None
        try:
            mrs = forge._branch_mrs(branch, "opened")
        except (GhError, ValueError):
            return None
        if not mrs:
            return None
        shaped = self._mr_shape(mrs[0])
        return {k: shaped[k] for k in ("number", "title", "url", "headRefName", "baseRefName")}

    def repo_web_url(self, root: Path) -> str | None:
        return self._here(root).repo_url or None

    def pr_body(self, number, slug: str | None, root: Path | None) -> dict:
        forge = self._here(root, slug)
        got = self._mr_shape(forge.api("GET", f"{forge._project}/merge_requests/{int(number)}",
                                       timeout=15))
        return {k: got[k] for k in ("number", "body", "url")}

    def update_pr_body(self, number, body: str, slug: str | None, root: Path | None) -> None:
        forge = self._here(root, slug)
        forge.api("PUT", f"{forge._project}/merge_requests/{int(number)}",
                  {"description": body}, timeout=30)

    def update_pr_body_shell(self, number, slug: str | None, path) -> str:
        forge = self._here(None, slug)
        return (f"glab api -X PUT {forge._project}/merge_requests/{int(number)} "
                f"-F description=@{shlex.quote(str(path))}")

    def pr_title_here(self) -> None:
        pr = self.current_pr(Path(".").resolve())
        if pr and pr.get("title"):
            print(pr["title"])

    def pr_api(self) -> GitLabPrApi:
        return GitLabPrApi(self)

    def issue(self, number: int, slug: str | None, fields: str) -> dict:
        """Issue `number`'s `fields`, in `gh issue view --json`'s words, raising on any
        failure."""
        forge = self._here(None, slug)
        it = forge.api("GET", f"{forge._project}/issues/{int(number)}")
        got = {"number": it.get("iid"), "title": it.get("title") or "",
               "body": it.get("description") or "", "url": it.get("web_url") or "",
               "state": it.get("state") or "", "createdAt": it.get("created_at") or "",
               "author": {"login": (it.get("author") or {}).get("username")},
               "labels": [{"name": label} for label in it.get("labels") or []]}
        return {k: got[k] for k in fields.split(",") if k in got}

    def review_threads(self, slug: str, number: int) -> dict:
        """`{"resolved": [first note id, …], "open": n}` over an MR's resolvable discussions,
        or `{"error": …}` when GitLab did not answer."""
        forge = self._here(None, slug)
        try:
            found = forge._pages(f"{forge._project}/merge_requests/{int(number)}/discussions")
        except (GhError, ValueError):
            return {"error": "GitLab did not answer"}
        resolved, still_open = [], 0
        for d in found:
            first = (d.get("notes") or [{}])[0]
            if not first.get("resolvable"):
                continue
            if first.get("resolved"):
                resolved.append(first["id"])
            else:
                still_open += 1
        return {"resolved": resolved, "open": still_open}

    # CI: a commit's pipelines, in the shape `gh run list --json` prints, so `preflight.py`
    # reads them unchanged. The workflow is always `.gitlab-ci.yml`.

    WORKFLOW = ".gitlab-ci.yml"
    WORKFLOW_NAME = "GitLab CI pipeline"

    #: A pipeline's status as GitHub's (status, conclusion). `manual` is a pipeline stopped
    #: at a blocking manual job: finished, and not green. `skipped` says nothing of the code.
    _STATES = {"success": ("completed", "success"), "failed": ("completed", "failure"),
               "canceled": ("completed", "cancelled"), "skipped": ("completed", "skipped"),
               "manual": ("completed", "action_required"), "running": ("in_progress", ""),
               "canceling": ("in_progress", "")}

    @staticmethod
    def _done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["glab"], code, stdout, stderr)

    def _as_run(self, p: dict, sha: str = "") -> dict:
        status, conclusion = self._STATES.get(p.get("status") or "", ("queued", ""))
        return {"databaseId": p.get("id"), "name": self.WORKFLOW_NAME,
                "workflowName": self.WORKFLOW_NAME, "status": status, "conclusion": conclusion,
                "headSha": p.get("sha") or sha,
                "event": ("pull_request" if p.get("source") == "merge_request_event"
                          else p.get("source") or "push"),
                "createdAt": p.get("created_at") or "", "url": p.get("web_url") or "",
                "headBranch": p.get("ref") or "", "attempt": 1}

    def _runs(self, sha: str) -> list[dict]:
        forge = self._here()
        found = forge.api("GET", f"{forge._project}/pipelines?sha={sha}"
                                 "&order_by=id&sort=desc&per_page=50")
        return [forge._as_run(p, sha) for p in found] if isinstance(found, list) else []

    def ci_workflows(self) -> subprocess.CompletedProcess:
        """One workflow when the project has ever run a pipeline. None is "no CI", as an
        empty `gh workflow list` is: a `.gitlab-ci.yml` in the tree proves less, since a
        project may keep its CI configuration elsewhere."""
        forge = self._here()
        try:
            seen = forge.api("GET", f"{forge._project}/pipelines?per_page=1")
        except (GhError, ValueError) as e:
            return self._done(code=1, stderr=str(e))
        return self._done(json.dumps(
            [{"id": self.WORKFLOW, "name": self.WORKFLOW_NAME, "path": self.WORKFLOW,
              "state": "active"}] if seen else []))

    def ci_runs(self, sha: str, workflow: str | None, fields: str) -> subprocess.CompletedProcess:
        """The pipelines of `sha`. Any workflow but `.gitlab-ci.yml` is not found, in the
        words `preflight.py` reads as a misconfigured gate, not as a wait."""
        if workflow and workflow not in (self.WORKFLOW, self.WORKFLOW_NAME):
            return self._done(code=1, stderr=f"workflow {workflow!r} not found: GitLab has one "
                                             f"pipeline per commit, named {self.WORKFLOW}")
        try:
            return self._done(json.dumps(self._runs(sha)))
        except (GhError, ValueError) as e:
            return self._done(code=1, stderr=str(e))

    def _jobs(self, run_id) -> list[dict]:
        forge = self._here()
        return forge._pages(f"{forge._project}/pipelines/{int(run_id)}/jobs")

    def ci_run(self, run_id: int) -> subprocess.CompletedProcess:
        """A pipeline as `gh run view --json jobs,event,headBranch` prints a run. A job has no
        steps, so each stands as its own one step, which is where a Sonar job is looked for."""
        forge = self._here()
        try:
            run = forge._as_run(forge.api("GET", f"{forge._project}/pipelines/{int(run_id)}"))
            jobs = []
            for j in self._jobs(run_id):
                status, conclusion = self._STATES.get(j.get("status") or "", ("queued", ""))
                jobs.append({"name": j.get("name"), "status": status, "conclusion": conclusion,
                             "url": j.get("web_url"),
                             "steps": [{"name": j.get("name"), "status": status,
                                        "conclusion": conclusion}]})
        except (GhError, ValueError) as e:
            return self._done(code=1, stderr=str(e))
        return self._done(json.dumps({"jobs": jobs, "event": run["event"],
                                      "headBranch": run["headBranch"]}))

    def ci_run_pr(self, run_id: int) -> str:
        """The MR a merge-request pipeline was for, or "": its ref says so."""
        forge = self._here()
        try:
            ref = forge.api("GET", f"{forge._project}/pipelines/{int(run_id)}").get("ref") or ""
        except (GhError, ValueError):
            return ""
        m = re.match(r"refs/merge-requests/(\d+)/", ref)
        return m.group(1) if m else ""

    def pr_for_branch(self, branch: str) -> str:
        try:
            mrs = self._here()._branch_mrs(branch, "all")
        except (GhError, ValueError):
            return ""
        return str(mrs[0]["iid"]) if mrs else ""

    def ci_runs_of(self, sha: str) -> str:
        try:
            return json.dumps(self._runs(sha))
        except (GhError, ValueError):
            return ""

    def ci_failed_log(self, run_id) -> str:
        """The traces of a pipeline's failed jobs, each under its name."""
        forge = self._here()
        out = []
        try:
            for j in self._jobs(run_id):
                if j.get("status") == "failed":
                    trace = forge.glab("GET", f"{forge._project}/jobs/{j['id']}/trace", timeout=30)
                    out.append(f"==> {j.get('name')}\n{trace}")
        except (GhError, ValueError, KeyError):
            pass
        return "\n".join(out)


# GitHub's half of the `(forge, number)` recognisers, beside Bitbucket's.
GitHubForge.pull_in = classmethod(
    lambda cls, text: (lambda h: (cls.of_slug(h[0]), h[1]) if h else None)(cls.find_pull(text)))
GitHubForge.issue_in = classmethod(
    lambda cls, text: (lambda h: (cls.of_slug(h[0]), h[1]) if h else None)(cls.find_issue(text)))
GitHubForge.blob_query = lambda self, ref: ""
GitHubForge.line_anchor = staticmethod(
    lambda line, end=None: f"#L{line}" + (f"-L{end}" if end and end > line else ""))


#: Every forge this skill knows, asked in order. GitHub first: its URLs are unambiguous.
#: GitLab before Bitbucket: its remotes go by the host's name, while Bitbucket's recognisers
#: are shapes any self-hosted host could have.
FORGES = (GitHubForge, GitLabForge, BitbucketServerForge)


@functools.lru_cache(maxsize=None)
def forge_for(root: Path):
    """The forge this checkout's `origin` is on, with its web root; None when `origin` is
    missing or on no forge in `FORGES` — callers then keep the editor link, which always
    works. Read from `origin` rather than configured, because a URL that has to be
    maintained by hand in a content file eventually points at somebody else's fork."""
    proc = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    for forge in FORGES:
        url = forge.from_remote(proc.stdout.strip())
        if url:
            return forge(url)
    return None


def repo_web_root(root: Path) -> str | None:
    """`forge_for(root)`'s web root, or None off a forge."""
    forge = forge_for(root)
    return forge.repo_url if forge else None


#: The name every caller used before there was a seam; tests still reach for it.
github_blob_base = repo_web_root


def forge_at(repo_url: str | None):
    """The forge for a repository web root a content file names (`pr.repo`), or None for
    none. GitHub's URL layout for any host until a second forge claims one — which is what
    every such link has always been built with, GitHub Enterprise hosts included — except
    a URL on a GitLab host, and one in Bitbucket's own shape (`…/projects/KEY/repos/r`,
    `…/users/u/repos/r`), which no GitHub repository has."""
    if not repo_url:
        return None
    gitlab = re.match(_GL_ROOT, repo_url)
    if gitlab:
        return GitLabForge(gitlab["root"])
    if re.match(_BB_ROOT, repo_url) and not _gl_is_named(repo_url):
        return BitbucketServerForge(re.match(_BB_ROOT, repo_url).group(0))
    if GitLabForge.project_of(repo_url):
        return GitLabForge(repo_url.rstrip("/").removesuffix(".git"))
    return GitHubForge(repo_url)


def forge_from_any_remote(remote: str):
    """`forge_for` on a remote URL already read, taking every spelling `from_any_remote`
    does; None off a known forge."""
    for forge in FORGES:
        url = forge.from_any_remote(remote)
        if url:
            return forge(url)
    return None


def api_forge():
    """The forge whose API is asked: GitLab or Bitbucket when the working directory's
    `origin` is on one, else `gh`, which resolves the repository from the working directory
    itself (so GitHub's instance carries no URL). `forge_for` is cached: one `git` call per
    process."""
    found = forge_for(Path.cwd().resolve())
    return found if isinstance(found, (GitLabForge, BitbucketServerForge)) else GitHubForge()


# Recognisers over a URL from anywhere: a content file, an API answer, a click. Each asks
# every forge in turn.

def same_forge(found, here) -> bool:
    """Whether `here` can answer about `found`, a forge recognised in a URL: the same kind,
    on the same instance. `of_slug` would otherwise re-root a project of one instance onto
    another, where a project of that name may well be a different one."""
    return type(found) is type(here) and here.of_slug(found.slug).repo_url == found.repo_url


def is_pull_url(url) -> bool:
    return any(f.is_pull_url(url) for f in FORGES)


def pull_root(url: str | None) -> str | None:
    return next((r for r in (f.pull_root(url or "") for f in FORGES) if r), None)


def find_pull(text):
    """`(forge, number)` of the first PR URL anywhere in `text`, the forge at its repo."""
    return next((hit for hit in (f.pull_in(text) for f in FORGES) if hit), None)


def is_comment_url(url: str) -> bool:
    return any(f.is_comment_url(url) for f in FORGES)


def is_commit_comment_url(url: str) -> bool:
    return any(f.is_commit_comment_url(url) for f in FORGES)


def find_issue(text: str):
    """`(forge, number)` when `text` starts with an issue URL, the forge at its repo."""
    return next((hit for hit in (f.issue_in(text) for f in FORGES) if hit), None)


def project_of(url: str) -> str:
    return next((r for r in (f.project_of(url) for f in FORGES) if r), "")


if __name__ == "__main__":
    if sys.argv[1:] == ["pr-title"]:
        api_forge().pr_title_here()
    else:
        sys.exit("usage: forge.py pr-title")
