"""The forge that hosts the reviewed repository — GitHub, today — behind one seam.

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


#: Every forge this skill knows, asked in order. A second one goes here.
FORGES = (GitHubForge,)


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
    every such link has always been built with, GitHub Enterprise hosts included."""
    return GitHubForge(repo_url) if repo_url else None


def forge_from_any_remote(remote: str):
    """`forge_for` on a remote URL already read, taking every spelling `from_any_remote`
    does; None off a known forge."""
    for forge in FORGES:
        url = forge.from_any_remote(remote)
        if url:
            return forge(url)
    return None


def api_forge():
    """The forge whose command line is asked: `gh`, the only one there is. Never detected —
    `gh` resolves the repository from the working directory itself, and detecting first
    would put a `git` call in front of every question."""
    return GitHubForge()


# Recognisers over a URL from anywhere: a content file, an API answer, a click. Each asks
# every forge in turn.

def is_pull_url(url) -> bool:
    return any(f.is_pull_url(url) for f in FORGES)


def pull_root(url: str | None) -> str | None:
    return next((r for r in (f.pull_root(url or "") for f in FORGES) if r), None)


def find_pull(text):
    """`(forge, number)` of the first PR URL anywhere in `text`, the forge at its repo."""
    for f in FORGES:
        hit = f.find_pull(text)
        if hit:
            return f.of_slug(hit[0]), hit[1]
    return None


def is_comment_url(url: str) -> bool:
    return any(f.is_comment_url(url) for f in FORGES)


def is_commit_comment_url(url: str) -> bool:
    return any(f.is_commit_comment_url(url) for f in FORGES)


def find_issue(text: str):
    """`(forge, number)` when `text` starts with an issue URL, the forge at its repo."""
    for f in FORGES:
        hit = f.find_issue(text)
        if hit:
            return f.of_slug(hit[0]), hit[1]
    return None


def project_of(url: str) -> str:
    return next((r for r in (f.project_of(url) for f in FORGES) if r), "")


if __name__ == "__main__":
    if sys.argv[1:] == ["pr-title"]:
        api_forge().pr_title_here()
    else:
        sys.exit("usage: forge.py pr-title")
