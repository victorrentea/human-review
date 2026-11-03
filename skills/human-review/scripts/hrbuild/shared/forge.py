"""The forge that hosts the reviewed repository — GitHub, or Bitbucket Server / Data Center
(issue #4) — behind one seam.

Everything the skill knows about the place the reviewed code is pushed to lives here and
nowhere else: the shape of its web URLs (a file at a commit, a folder, a commit, a
comparison, a pull request, an issue, an author's face), how to recognise one of those
URLs when a content file or an API answer hands one over, and every question asked of it
over its command line (`gh`): a PR's description (read, and rewritten for its
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

    @staticmethod
    def ci_workflows() -> subprocess.CompletedProcess:
        return _sh("gh workflow list --json id,name,path,state")

    @staticmethod
    def ci_runs(sha: str, workflow: str | None, fields: str) -> subprocess.CompletedProcess:
        sel = f' --workflow "{workflow}"' if workflow else ""
        return _sh(f'gh run list --commit "{sha}"{sel} --limit 50 --json {fields}')

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


# GitHub's half of the `(forge, number)` recognisers, beside Bitbucket's.
GitHubForge.pull_in = classmethod(
    lambda cls, text: (lambda h: (cls.of_slug(h[0]), h[1]) if h else None)(cls.find_pull(text)))
GitHubForge.issue_in = classmethod(
    lambda cls, text: (lambda h: (cls.of_slug(h[0]), h[1]) if h else None)(cls.find_issue(text)))
GitHubForge.blob_query = lambda self, ref: ""
GitHubForge.line_anchor = staticmethod(
    lambda line, end=None: f"#L{line}" + (f"-L{end}" if end and end > line else ""))


#: Every forge this skill knows, asked in order. GitHub first: its URLs are unambiguous,
#: while Bitbucket's recognisers are shapes any self-hosted host could have.
FORGES = (GitHubForge, BitbucketServerForge)


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
    a URL in Bitbucket's own shape (`…/projects/KEY/repos/r`, `…/users/u/repos/r`), which
    no GitHub repository has."""
    if not repo_url:
        return None
    if re.match(_BB_ROOT, repo_url):
        return BitbucketServerForge(re.match(_BB_ROOT, repo_url).group(0))
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
    """The forge whose API is asked: Bitbucket when the working directory's `origin` is on
    one, else `gh`, which resolves the repository from the working directory itself (so
    GitHub's instance carries no URL). `forge_for` is cached: one `git` call per process."""
    found = forge_for(Path.cwd().resolve())
    return found if isinstance(found, BitbucketServerForge) else GitHubForge()


# Recognisers over a URL from anywhere: a content file, an API answer, a click. Each asks
# every forge in turn.

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
