"""The scope bar: what the base is, what the diff measures, how a chip renders."""
from __future__ import annotations

import functools
import html
import json
import re
import urllib.parse
from pathlib import Path

from .changes import changed_files, files_pop
from .util import PENCIL, _git

# What a diffstat must never count. Every path below is written by a generator -- a
# sequence diagram redrawn from a trace, an API client regenerated from a spec, a lock
# file resolved by a package manager -- and none of it is code a reviewer reads.
#
# Counting them does not merely inflate the number, it inverts it. On the branch this was
# written for, one regenerated `endpoint-complexity.json` supplied 1405 of 2896 added
# lines, and the redrawn `.genseq.*` pairs supplied almost every deletion: a reviewer
# reading `-333 lines` was reading a diagram being redrawn, not a line of logic being
# removed. The chip is there to say how much there is to read, and a number dominated by
# machine output answers a different question than the one being asked.
#
# `exclude` in the content file adds to this list; it never replaces it. There is no way
# to switch the default off, because "count the generated files too" is not a reviewing
# preference -- it is the mistake this exists to prevent. The tooltip states the
# unfiltered totals anyway, so nothing is hidden, only ranked.
#
# ONE list, read by both places that ask "did a human write this?": this header and
# `run-steps.py`'s aftermath band (which imports it as `GENERATED_DEFAULT`). There used to
# be two, and eval run 8 caught them disagreeing: the band called the springdoc-written
# `openapi.yaml` generated, this chip counted its +79/−29 as hand-written lines. Spelled as
# `**` globs -- `*` stops at a slash, `**` crosses them, a leading `**/` is optional --
# which is both `run-steps.py:glob_rx` and git's own `:(glob)` pathspec magic, so the same
# string means the same paths in both readers. A project's `"generated"` in
# `human-review.json` replaces it, in both places alike (`generated_globs`).
GENERATED_GLOBS = (
    "**/generated/**", "docs/generated/**", "openapi.yaml",
    "**/*.genseq.*", "**/api-types.ts", "**/*.drawio*",
    "**/*.min.js", "**/*.min.css", "**/*.snap",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.webp", "**/*.ico", "**/*.pdf",
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/go.sum", "**/Cargo.lock", "**/poetry.lock",
    ".human-review/**",
)
# Kept under the old name: the orchestrator re-exports it, and a test or a caller reaching
# for "the list" must get this one, not a stale copy.
GENERATED_PATHSPECS = list(GENERATED_GLOBS)

# What the review itself commits beside the code: the findings record and its cost ledger.
# Not generated -- an agent wrote every line on purpose -- but not the change under review
# either, so the code chips leave it out and say so by name. It stays out of
# `GENERATED_GLOBS` because the aftermath band reads that list, and a hand edit to the
# review record after the agent finished is something that band must still see.
REVIEW_BOOKKEEPING = ("**/review-points.md", "**/review-cost.json")


def generated_globs(cfg: dict | None) -> list[str]:
    """The project's `"generated"` list when it names one, else `GENERATED_GLOBS` --
    the same rule `run-steps.py:generated_globs` applies, so the header and the aftermath
    band can never be reading two lists again."""
    got = cfg.get("generated") if isinstance(cfg, dict) else None
    if isinstance(got, list):
        named = [g for g in got if isinstance(g, str) and g.strip()]
        if named:
            return named
    return list(GENERATED_GLOBS)


def _project_cfg(root: Path) -> dict:
    """`human-review.json` at the top of the checkout, or {} when there is none."""
    try:
        cfg = json.loads((root / "human-review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _resolve_base(root: Path, named: str) -> tuple[str, str] | None:
    """Which ref the page should actually measure against, given the name it was told.

    A content file says `"base": "main"`, and on the machine the review is built on that
    is a *local* branch which may be days behind the remote it names. Comparing against it
    charges the branch under review with every commit the local ref has not pulled yet:
    on the branch this was written for, local `main` was 18 commits behind `origin/main`,
    and diffing against it reported 142 files and 4099 deleted lines for a change set that
    deletes 39. So a bare name resolves to `origin/<name>` when that exists -- the ref a
    pull request would actually merge into -- and only falls back to the local branch when
    there is no remote-tracking ref to prefer. A name that already carries a remote
    (`origin/main`, `upstream/main`) is taken at its word.

    Returns `(ref, sha)`, or None when nothing by that name resolves at all. Deliberately
    never falls back to HEAD: a base that will not resolve must not silently become the
    thing it is supposed to be compared against, or the page reports a change set of zero
    and calls it a clean review.
    """
    candidates = [named] if "/" in named else [f"origin/{named}", named]
    for ref in candidates:
        sha = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return ref, sha
    return None


def patch_equivalent(root: Path, upstream: str, head: str,
                     limit: str | None = None) -> set[str]:
    """The commits of `limit..head` whose patch `upstream` already carries — `git cherry`'s
    `-` lines: the same change, cherry-picked onto the base under another sha.

    Eval run 11 said "7 commits never reviewed" for a branch whose 5 tooling commits had
    since been cherry-picked onto origin/main: counted by sha, a cherry-pick is a commit
    the base lacks; counted by patch-id, it is already there."""
    out = _git(root, "cherry", upstream, head, *([limit] if limit else []))
    return {ln[2:].strip() for ln in (out or "").splitlines() if ln.startswith("- ")}


#: Set to anything to keep a build off the network: no `git fetch` of the base.
NO_FETCH_ENV = "HUMAN_REVIEW_NO_FETCH"


def fetch_base(root: Path, named: str, timeout: float = 20) -> bool:
    """`git fetch origin <base>` before the base is measured, when the clone has a network
    remote — so `origin/main` is the base as it stands, not as it was at the last pull.
    Quiet, bounded, never interactive; a failure (offline, no remote) keeps the refs the
    clone has. True when it fetched."""
    import os
    import subprocess
    if os.environ.get(NO_FETCH_ENV):
        return False
    branch = named.removeprefix("origin/")
    url = _git(root, "remote", "get-url", "origin") or ""
    if not re.match(r"^(?:https?://|ssh://|git@)", url) or not branch:
        return False
    try:
        p = subprocess.run(["git", "fetch", "--quiet", "--no-tags", "origin",
                            f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                           cwd=root, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def base_state(root: Path, named: str) -> dict | None:
    """Where the base sits relative to the branch -- the two ways the comparison goes stale.

    A review page is a claim about a *pair* of refs, and it keeps being rendered long after
    one of them has moved. Two distinct things can be wrong, and they need saying
    differently because the fix differs:

    `ahead` -- commits on the base that are not on the branch. The branch forked from
    behind and has stayed there, so every diagram, count and finding on the page describes
    a merge that has not been rehearsed against what main actually contains now. Merging or
    rebasing makes it zero, which is exactly why the warning disappears on its own: there
    is no flag to clear and nothing to remember.

    `localBehind` -- the *local* branch named as the base is behind its own remote. Nothing
    is wrong with the branch under review here; what is stale is the yardstick. This one is
    quieter and nastier than the first: the page looks current, the numbers look measured,
    and they are measured against a main from last week.

    Returns None when the base does not resolve, which drops the marker rather than
    inventing a reassuring absence of one.
    """
    resolved = _resolve_base(root, named)
    if not resolved:
        return None
    ref, sha = resolved
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not head:
        return None
    merge_base = _git(root, "merge-base", sha, head)

    def count(rng: str) -> int | None:
        n = _git(root, "rev-list", "--count", rng)
        return int(n) if n and n.isdigit() else None

    state = {
        "named": named,
        "ref": ref,
        "sha": sha,
        "head": head,
        "mergeBase": merge_base,
        # Commits the base has that the branch does not. Not `merge_base != sha`: the
        # count is the number a reader acts on ("nine commits behind"), and the boolean
        # falls out of it.
        "ahead": count(f"{head}..{sha}"),
        "aheadPicked": 0,
        "localRef": None,
        "localBehind": None,
    }
    # The base's commits that are this branch's own, cherry-picked: not drift to merge in
    # (eval run 11's "17 ahead" counted 5 of the branch's tooling commits picked onto main).
    if state["ahead"]:
        picked = len(patch_equivalent(root, head, sha, merge_base)) if merge_base else 0
        state["ahead"] -= picked
        state["aheadPicked"] = picked
    # Only meaningful when a *local* branch of that name exists beside the remote one we
    # preferred. `origin/main` given verbatim in the content file has no local twin to be
    # behind, and neither does a repository with no remote at all.
    if ref != named and _git(root, "rev-parse", "--verify", "--quiet", f"{named}^{{commit}}"):
        state["localRef"] = named
        state["localBehind"] = count(f"{named}..{ref}")
    return state


#: Where a run records which commit its review started from, in the order they are
#: believed. `review-points.md` is the coding agent's own record of the range its
#: reviewers read (`audited-base`, else `base`), parsed into `review-points.json` by the
#: `reviewpoints` step; the front matter itself is the fallback for a page rebuilt before
#: that step ran. `review-commits.json` is where `run-steps.py` wrote the merge-base of
#: the base it was handed — the before-side every producer of a tab measured from.
POINTS_JSON = "review-points.json"
COMMITS_JSON = "review-commits.json"
BASE_SOURCES = {
    "audited": "the base the review audited",
    "steps": "the base the producers ran against",
    "merge-base": "the fork point",
}


def _front_matter(root: Path) -> dict:
    """`review-points.md`'s front matter, as `key: value` pairs, or {}."""
    try:
        text = (root / "review-points.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return dict(re.findall(r"^([A-Za-z-]+):\s*(.*?)\s*$", m.group(1), re.M)) if m else {}


def _recorded_bases(root: Path, out_dir: Path | None) -> list[tuple[str, str]]:
    """`(source, rev)` for every base this run recorded, most authoritative first."""
    found: list[tuple[str, str]] = []

    def read(name: str) -> dict:
        if out_dir is None:
            return {}
        try:
            doc = json.loads((out_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    prov = read(POINTS_JSON).get("provenance") or {}
    front = _front_matter(root)
    for rev in (prov.get("auditedBase"), prov.get("base"),
                front.get("audited-base"), front.get("base")):
        if rev and isinstance(rev, str):
            found.append(("audited", rev.strip()))
    rev = read(COMMITS_JSON).get("base")
    if rev and isinstance(rev, str):
        found.append(("steps", rev.strip()))
    return found


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older, newer) is not None


def page_base(root: Path, out_dir: Path | None, named: str) -> dict | None:
    """The ONE commit every number on the page is measured from, and why that one.

    A page used to carry two. The tabs a producer draws (API, Tests, Complexity, Logging)
    measured from the base `run-steps.py` was handed — on a reviewed branch, the commit
    the review audited — while everything the build computes for itself (the files and
    lines chips, CODEOWNERS, the NEW FILE badge on a snippet, the Code City) measured from
    `merge-base(origin/main)`. On a branch carrying three commits from before the review
    the header said `+3927 / −386` over a change of `+2091 / −2220`, CODEOWNERS raised an
    approval alarm over a file changed before the review began, and a file the base
    already had was badged NEW FILE. Every consumer now takes its base from here.

    In order: the base the review audited (`review-points.md`), the base the producers ran
    against (`review-commits.json`), and the fork point from `named`. A recorded base is
    used only when it is an ancestor of HEAD, and not when the base branch has since
    absorbed it — measuring from a commit main already contains, past main's own fork
    point, would charge the branch with main's commits.

    Returns `base_state(named)` — the drift facts the ref chip warns about are about
    `named` and stay so — extended with `diffBase` (the sha), `diffBaseSource` (a key of
    `BASE_SOURCES`), `outside` (the commits on the branch between the fork point and the
    chosen base, oldest last, as `{sha, subject}`), and `stepsBase` when the producers ran
    against a different commit than the one chosen. None when nothing resolves at all.
    """
    state = base_state(root, named)
    head = (state or {}).get("head") or _git(root, "rev-parse", "--verify", "--quiet",
                                             "HEAD^{commit}")
    if not head:
        return None
    mb = (state or {}).get("mergeBase")
    chosen, steps = None, None
    for source, rev in _recorded_bases(root, out_dir):
        sha = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        if not sha:
            continue
        if source == "steps" and steps is None:
            steps = sha
        if chosen or not _is_ancestor(root, sha, head):
            continue
        if mb and sha != mb and _is_ancestor(root, sha, mb):
            continue
        chosen = (source, sha)
    if chosen is None:
        if not mb:
            return None
        chosen = ("merge-base", mb)
    source, sha = chosen
    out = dict(state or {"named": named, "ref": None, "sha": None, "head": head,
                         "mergeBase": None, "ahead": None, "localRef": None,
                         "localBehind": None})
    out.update({"diffBase": sha, "diffBaseSource": source, "outside": [],
                "stepsBase": steps if steps and steps != sha else None})
    if mb and sha != mb and _is_ancestor(root, mb, sha):
        log = _git(root, "log", "--format=%H%x1f%s", f"{mb}..{sha}") or ""
        out["outside"] = [dict(zip(("sha", "subject"), line.split("\x1f", 1)))
                          for line in log.splitlines() if "\x1f" in line]
        # Already on the base under another sha: not a commit the merge would bring.
        picked = patch_equivalent(root, out["sha"], sha, mb) if out.get("sha") else set()
        for c in out["outside"]:
            if c["sha"] in picked:
                c["onBase"] = True
    return out


def measured_from(state: dict | None) -> str:
    """`origin/main`, or `b12c9bdb (the base the review audited)` — what a tooltip says
    the numbers were counted against."""
    if not state:
        return ""
    source = state.get("diffBaseSource")
    if source and source != "merge-base":
        return f"{state['diffBase'][:8]} ({BASE_SOURCES[source]})"
    return state.get("ref") or (state.get("diffBase") or "")[:8]


def base_warning(state: dict | None) -> str | None:
    """The sentence behind the `!` on the base chip, or None when the pair is current.

    Both conditions are reported in one tooltip when both hold, because they compound: a
    branch forked from behind a base that is *itself* behind its remote is two hops from
    the merge it claims to describe, and a reader told only about one of them will fix
    that one and trust the rest.
    """
    if not state:
        return None
    # Short, like every other hover on the scope bar. Each clause names the gap and the
    # one command that closes it -- which is all a reader standing over the page can act
    # on. Why it matters (nothing here was measured against those commits; the page looks
    # current while its yardstick is a week old) is in this function's docstring, for
    # whoever is fixing the build rather than reading it.
    parts = []
    ahead = state.get("ahead")
    behind = state.get("localBehind")
    # When the page's counts are taken from a recorded base (`page_base`: the base the
    # review audited), the local base being behind changes nothing the page shows, so
    # it is not said. Which commit the mark measures from (eval run 6's "Measured against
    # origin/main, not the review base the counts beside it use") was a mechanics
    # sentence on a hover that exists to say one thing: merge, then rebuild.
    other = (state.get("diffBaseSource") not in (None, "merge-base")
             and state.get("diffBase") and state.get("sha"))
    picked = state.get("aheadPicked") or 0
    if ahead:
        # `ahead` leaves out the branch's own commits main took by cherry-pick; git counts
        # them, so the total is what `git rev-list` says (eval runs 14-18: "12" against 18).
        total = ahead + picked
        parts.append(f"{state['ref']} is {total} commit{'s' if total != 1 else ''} ahead of "
                     "the fork point"
                     + (f", {picked} of them this branch's own, cherry-picked" if picked else "")
                     + ". Merge or rebase, then rebuild.")
    if behind and not other:
        # Not `git fetch`: the count above was read off the remote-tracking ref, so the
        # fetch has already happened, and it never moves the local branch anyway. What
        # closes this gap is fast-forwarding the local branch onto what was fetched.
        parts.append(f"Compared against {state['ref']} ({state['sha'][:8]}); local "
                     f"{state['localRef']} is {behind} behind it. "
                     f"git branch -f {state['localRef']} {state['ref']}.")
    return " ".join(parts) or None


def _numstat(root: Path, rng: str, pathspecs: list[str]) -> tuple[int, int, int, int, int]:
    """`(files_added, files_edited, files_deleted, lines_added, lines_removed)` for a range.

    Binary files report `-` for both line counts; they are counted as files touched and
    contribute no lines, which is the only honest reading -- "a PNG changed by 14142 bytes"
    is not a number that belongs beside a count of lines a human reads.
    """
    args = ["diff", "--numstat", rng, "--", ".", *pathspecs]
    numstat = _git(root, *args) or ""
    status = _git(root, "diff", "--name-status", rng, "--", ".", *pathspecs) or ""
    adds = dels = 0
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        a, d = cols[0], cols[1]
        adds += int(a) if a.isdigit() else 0
        dels += int(d) if d.isdigit() else 0
    added = edited = deleted = 0
    for line in status.splitlines():
        code = line.split("\t", 1)[0][:1]
        if code == "A":
            added += 1
        elif code == "D":
            deleted += 1
        elif code:
            # R (renamed) and C (copied) land here with M. A rename is a file edited from
            # the reviewer's side of the desk, not one added and one removed.
            edited += 1
    return added, edited, deleted, adds, dels


def _compare_href(pr: dict | None, base: str | None = None) -> str:
    """`<repo>/compare/<base>...<branch>` — the diff the diffstat is a count of.

    Built from the two refs the page already names rather than from the shas it
    measured: a sha pair is only a URL once the branch has been pushed, and the number
    in the chip is read off a working tree that may be a commit ahead of the remote. Two
    branch names are the comparison github.com keeps current by itself — the same pair
    the ref chips beside it link to, one page further in.

    `origin/` is stripped: it names a remote in *this* checkout, and github.com has
    never heard of it. Empty when anything is missing, and the chip stays an inert pill
    rather than linking somewhere that 404s.

    `base` overrides the PR's base name when the page measures from a commit that is not
    the fork point (`page_base`): the compare page must open the range the chip counted,
    not `main...branch`, which on such a branch is a different and larger diff."""
    pr = pr or {}
    repo = (pr.get("repo") or "").rstrip("/")
    base = (base or pr.get("base") or "").removeprefix("origin/")
    branch = pr.get("branch") or ""
    if not (repo and base and branch):
        return ""
    return (f"{repo}/compare/{urllib.parse.quote(base)}..."
            f"{urllib.parse.quote(branch)}")


def diffstat_chips(root: Path, state: dict | None, extra: list[str] | None,
                   pr: dict | None = None) -> list[dict]:
    """`{"auto": "diffstat"}` -- how much there is to read, measured rather than typed.

    The fourth chip to be taken away from the author, and the one with the clearest reason
    to be. `files` and `lines` outlived the `autofixed`, `cost` and `tests` chips being
    computed because they *look* like facts: a number with a sign in front of it reads as
    something a tool produced. On the branch this was written for, the page had said
    `files +1 / ~40` and `lines +1198 / -863` for six days. The file count was roughly
    right. The line counts matched no range in the repository at all -- not the branch
    against its base (+2896 / -333), not against the merge-base recorded in the same
    content file (+5089 / -4378), not against the stale local main (+4347 / -4099). They
    had been typed once, from a branch state three commits and one `git reset` ago, and
    nothing was ever going to catch them, because nothing was looking.

    Two chips out of one measurement, so the file count and the line count can never
    describe different ranges -- which is its own class of drift, and the one a reader is
    least equipped to notice.

    Returns [] when the base will not resolve: no base, no comparison, no chip. A page that
    cannot say what it measured against must not print a number as though it could.
    """
    start = (state or {}).get("diffBase") or (state or {}).get("mergeBase")
    if not start:
        return []
    # `A...B` and `A..B` differ only when the base has moved ahead, and that is precisely
    # the case the `!` on the ref chip is about. Three dots is the pull request's own
    # reading -- what this branch did, not what has happened since it forked -- so the two
    # marks stay independent: the numbers describe the branch, the warning describes the
    # gap. The left side is `page_base`'s answer when there is one: the same commit every
    # tab below was measured from, never a second one of the header's own.
    rng = f"{start}...{state['head']}"
    # The project's list and the built-in one are `**` globs (`:(glob)` magic); a content
    # file's `exclude` keeps the plain pathspec spelling it was always written in.
    gen = [f":(exclude,glob){p}" for p in generated_globs(_project_cfg(root))]
    gen += [f":(exclude){p}" for p in (extra or [])]
    books = [f":(exclude,glob){p}" for p in REVIEW_BOOKKEEPING]
    a, e, d, adds, dels = _numstat(root, rng, gen + books)
    fa, fe, fd, fadds, fdels = _numstat(root, rng, [])
    ga, ge, gd, _, _ = _numstat(root, rng, books)
    booked = (fa + fe + fd) - (ga + ge + gd)
    hidden = (ga + ge + gd) - (a + e + d)
    booked_names = sorted({ln.split("\t")[-1] for ln in (_git(
        root, "diff", "--name-only", rng, "--", *[f":(glob){p}" for p in REVIEW_BOOKKEEPING])
        or "").splitlines() if ln.strip()})

    # The signs are the page's, not this chip's: `+` added, `-` removed, a pencil for
    # changed, and a zero is dropped rather than printed. A row of chips is read as a row
    # of signed numbers, and `-0` is noise that costs a glance to dismiss.
    # Each number opens onto the files it counts (Victor, 14 Oct 2026: "when I hover + or
    # - or edited I should see a list of what files"): one walk-in list per sign, read off
    # the same range and pathspecs as the count, each file a link to its diff.
    listed = changed_files(root, rng, gen + books)

    def counted(cls: str, face: str, n: int, code: str, what: str) -> str:
        if not n:
            return ""
        pop = files_pop(f"{n} file{'s' if n != 1 else ''} {what}",
                        [f for f in listed if f["status"] == code], start, root)
        return (f'<span class="{cls}" tabindex="0" data-tip-pop="{html.escape(pop, quote=True)}">'
                f'{face}{n}</span>')

    files_value = " / ".join(piece for piece in (
        counted("added", "+", a, "A", "added"),
        counted("removed", "−", d, "D", "deleted"),
        counted("changed", PENCIL, e, "M", "edited"),
    ) if piece) or "none"
    lines_value = " / ".join(piece for piece in (
        f'<span class="added">+{adds}</span>' if adds else "",
        f'<span class="removed">−{dels}</span>' if dels else "",
    ) if piece) or "none"

    # The unfiltered totals stay in the hover; the prose explaining them does not. A
    # filtered number with no way to see what was filtered leaves the reader taking the
    # exclusion on trust, which is the position the typed chip left them in -- so the
    # guarantee is that the generated files are *ranked below* the code, never hidden from
    # it. What went is the argument for that: a redrawn diagram is not a line written, and
    # everything here was measured with `git diff` at build time rather than typed. Both
    # true, neither actionable, and a tooltip is read standing up in one glance. Whoever
    # needs the reasoning is reading this function.
    if hidden:
        skipped = f" {hidden} generated left out"
    else:
        skipped = " No generated files to leave out"
    if booked:
        # Named, because "1 file of review bookkeeping" is a reason a reader has to take
        # on trust, and `review-points.md` is one they recognise at a glance.
        skipped += f"; review bookkeeping left out ({', '.join(booked_names)})"
    if hidden or booked:
        skipped += f"; with them {fa + fe + fd} files, +{fadds} / −{fdels}."
    else:
        skipped += "."

    # The line count is the one number on the bar a reader wants to *open*: "+921 / −68"
    # is the size of what there is to read, and the next question is always what those
    # lines are. So it carries the compare page, and the file count beside it stays an
    # inert pill -- two identical-looking links to the same page is a row that teaches the
    # reader to ignore half of it.
    off_fork = state.get("diffBaseSource") not in (None, "merge-base")
    href = _compare_href(pr, start if off_fork else None)
    # Which base the numbers count from (`measured_from`) went from the hover in the copy
    # pass of 3 Oct 2026: the face is the count, and the base is the page's business.
    lines_tip = skipped.strip()
    if href:
        lines_tip += " Click for the full diff on GitHub."
    return [
        {"label": "files",
         "value": files_value,
         "tip": f"{a} added, {e} edited, {d} deleted.{skipped}"},
        {"label": "lines",
         "value": lines_value,
         "tip": lines_tip,
         **({"href": href} if href else {})},
    ]


def chip_face(c: dict) -> str:
    """A chip's own words: the label it was given and the value it measured. Shared by
    every renderer below so that a chip which moves house — the cost chip becoming half of
    the run chip — cannot pick up different markup on the way.

    A chip may bring its own `face` instead — a sentence already set, where the bold does
    not fall on "everything after the label". The review chip is one: it names two agents,
    and `label <b>value</b>` could only ever bold the second of them along with the
    numbers, which is how `🤖coder:` came to read as a footnote to a bold `Review:`."""
    if c.get("face"):
        return c["face"]
    return f'{html.escape(c["label"])} <b>{c["value"]}</b>'


def chip_html(c: dict) -> str:
    """One resolved chip, as the scope bar renders it: a link when it has somewhere to
    send the reader, an inert pill otherwise."""
    inner = chip_face(c)
    if c.get("tip"):
        inner = f'<span data-tip="{html.escape(c["tip"])}">{inner}</span>'
    if c.get("href"):
        return (f'<a class="chip chip-link" href="{html.escape(c["href"])}"'
                f'{" target=_blank" if c["href"].startswith("http") else ""}>{inner}</a>')
    return f'<span class="chip">{inner}</span>'


def review_chip_face(open_n: int, fixed_n: int, assumed_n: int) -> str:
    """`🤖 <b>6 assumptions</b> / <angry-bot> <b>10 open</b> · <b>6 auto fixed</b>` — the masthead's review chip,
    numbers only, one robot.

    Eval run 10: `🤖Code: 6 unsure; 🤖Review: 10 open · 3 refuted, 6 fixed` was 379px of a
    1040px scope bar, and it was the chip that wrapped to a second row and took the sticky
    header from 108px to 164px on every tab. The two agent names and the second robot
    were the words a reader needs once, so they moved to the hover (`review_chip_key`);
    the counts stay, each one with the noun that says what it counts, in the order of the
    work — what the coder guessed at first, then what is left, what was already done.
    `assumed_n` is every assumption, the Review tab's `🤖: N assumptions` line's own count
    — the under-70% share is the hover's (`review_chip_key`). With none it is absent rather
    than zeroed: a zero is a claim the page cannot stand behind. The
    refuted claims are not on the page at all (Victor, 5 Oct 2026), so not here either."""
    rest = f"{_angry_bot()}<b>{open_n} open</b> · <b>{fixed_n} auto fixed</b>"
    if not assumed_n:
        return rest
    # A slash after the coder's guesses, then the angry robot for what the review left open:
    # two robots, two moods, one chip. The same three numbers, in the same order, as the
    # Review tab's line under the PR description (Victor, 20 Oct 2026).
    return f"\U0001f916 <b>{assumed_n} assumption{'' if assumed_n == 1 else 's'}</b> / {rest}"


@functools.lru_cache(maxsize=None)
def _angry_bot() -> str:
    """Victor's angry robot as an inline `<img>` with a data URI: the page stays one file."""
    import base64
    png = (Path(__file__).resolve().parent.parent / "assets" / "angry-bot.png").read_bytes()
    return ('<img class="angry-bot" alt="" aria-hidden="true" '
            f'src="data:image/png;base64,{base64.b64encode(png).decode()}">')


def review_chip_key(open_n: int, fixed_n: int, assumed_n: int, unsure_n: int = 0) -> str:
    """The hover's first sentence: the chip's face in the long words it no longer has room
    for, plus how many of the assumptions are under 70% sure — the lede's own words."""
    unsure = f", {unsure_n} under 70% sure" if unsure_n else ""
    coder = (f"Coding agent: {assumed_n} assumption{'' if assumed_n == 1 else 's'}"
             f"{unsure}. ") if assumed_n else ""
    return coder + f"Review: {open_n} open · {fixed_n} auto-fixed."


# The words a reviewer's name is spelt around: `correctness reviewer`, `reviewer correctness`,
# and the plural a shared source distributes over its list (`correctness, tests and
# ticket-fit reviewers`). None of them tells two reviewers apart.
_REVIEWER_WORD = re.compile(r"\b(?:sub-?agent|reviewers?)\b", re.I)


def reviewer_names(source: str) -> list[tuple[str, str]]:
    """`correctness, tests and ticket-fit reviewers` → three reviewers, as (key, name).

    The key is what is counted: lower-case, the parenthesised detail dropped (`CI
    (SonarCloud java:S1192)` → `ci`), and the word `reviewer` gone from either end, so
    `correctness reviewer`, `Reviewer correctness` and the `correctness` of a shared plural
    are one reviewer. The name is the first spelling met, without that word — what the
    hover prints. A source naming nothing but `reviewer` keeps it as its name."""
    src = re.sub(r"\s*\([^()]*\)", "", source or "").strip()
    out: dict[str, str] = {}
    for part in re.split(r"\s*(?:,|\+|;|&|\band\b)\s*", src):
        part = part.strip()
        if not part:
            continue
        name = re.sub(r"\s+", " ", _REVIEWER_WORD.sub("", part)).strip(" -") or part
        out.setdefault(name.lower(), name)
    return list(out.items())


def raised_by_reviewer(items, total: int) -> str:
    """`19 raised — 6 by correctness, 2 by security, 6 by tests, 5 by ticket-fit, 3 by CI
    (2 raised by more than one reviewer, so the counts add to 22)` — the review chip's
    hover, counted per reviewer, never per spelling of `source`.

    Eval run 10 listed `4 by correctness reviewer … 2 by correctness` — one reviewer under
    two labels, because `correctness reviewer` and the `correctness` of `correctness, tests
    and ticket-fit reviewers` were counted as different strings, and the parts summed to 22
    over 19 with nothing saying why. Each item now counts once for every reviewer it names
    (`reviewer_names`), the reviewers are listed in the order first met, and when an item
    was raised by several the hover says so and gives the sum, so the arithmetic is on the
    page instead of left to the reader. An item with no `source` is counted as itself."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    shared = 0
    for it in items:
        found = reviewer_names(it.get("source") or "")
        if len(found) > 1:
            shared += 1
        for key, name in found or [("", "")]:
            names.setdefault(key, name)
            counts[key] = counts.get(key, 0) + 1
    named = [f"{n} by {names[k]}" for k, n in counts.items() if k]
    if not named:
        return f"{total} raised"
    if counts.get(""):
        named.append(f'{counts[""]} with no reviewer named')
    tail = ""
    if shared:
        tail = (f" ({shared} raised by more than one reviewer, so the counts add to "
                f"{sum(counts.values())})")
    return f"{total} raised — " + ", ".join(named) + tail
