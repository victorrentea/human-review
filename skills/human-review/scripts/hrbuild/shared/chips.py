"""The scope bar: what the base is, what the diff measures, how a chip renders."""
from __future__ import annotations

import html
import urllib.parse
from pathlib import Path

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
GENERATED_PATHSPECS = [
    "*/generated/*", "generated/*",
    "*.genseq.json", "*.genseq.puml",
    "*.min.js", "*.min.css", "*.snap",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "package-lock.json", "*/package-lock.json",
    "yarn.lock", "*/yarn.lock",
    "pnpm-lock.yaml", "*/pnpm-lock.yaml",
    "go.sum", "*/go.sum",
    "Cargo.lock", "*/Cargo.lock",
    "poetry.lock", "*/poetry.lock",
    ".human-review/*",
]


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
        "localRef": None,
        "localBehind": None,
    }
    # Only meaningful when a *local* branch of that name exists beside the remote one we
    # preferred. `origin/main` given verbatim in the content file has no local twin to be
    # behind, and neither does a repository with no remote at all.
    if ref != named and _git(root, "rev-parse", "--verify", "--quiet", f"{named}^{{commit}}"):
        state["localRef"] = named
        state["localBehind"] = count(f"{named}..{ref}")
    return state


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
    if ahead:
        parts.append(f"{state['ref']} is {ahead} commit{'s' if ahead != 1 else ''} ahead of "
                     "the fork point. Merge or rebase, then rebuild.")
    behind = state.get("localBehind")
    if behind:
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


def _compare_href(pr: dict | None) -> str:
    """`<repo>/compare/<base>...<branch>` — the diff the diffstat is a count of.

    Built from the two refs the page already names rather than from the shas it
    measured: a sha pair is only a URL once the branch has been pushed, and the number
    in the chip is read off a working tree that may be a commit ahead of the remote. Two
    branch names are the comparison github.com keeps current by itself — the same pair
    the ref chips beside it link to, one page further in.

    `origin/` is stripped: it names a remote in *this* checkout, and github.com has
    never heard of it. Empty when anything is missing, and the chip stays an inert pill
    rather than linking somewhere that 404s."""
    pr = pr or {}
    repo = (pr.get("repo") or "").rstrip("/")
    base = (pr.get("base") or "").removeprefix("origin/")
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
    if not state or not state.get("mergeBase"):
        return []
    # `A...B` and `A..B` differ only when the base has moved ahead, and that is precisely
    # the case the `!` on the ref chip is about. Three dots is the pull request's own
    # reading -- what this branch did, not what has happened since it forked -- so the two
    # marks stay independent: the numbers describe the branch, the warning describes the
    # gap.
    rng = f"{state['mergeBase']}...{state['head']}"
    excludes = [f":(exclude){p}" for p in GENERATED_PATHSPECS + list(extra or [])]
    a, e, d, adds, dels = _numstat(root, rng, excludes)
    fa, fe, fd, fadds, fdels = _numstat(root, rng, [])
    hidden = (fa + fe + fd) - (a + e + d)

    where = f"vs {state['ref']}"
    # The signs are the page's, not this chip's: `+` added, `-` removed, a pencil for
    # changed, and a zero is dropped rather than printed. A row of chips is read as a row
    # of signed numbers, and `-0` is noise that costs a glance to dismiss.
    files_value = " / ".join(piece for piece in (
        f'<span class="added">+{a}</span>' if a else "",
        f'<span class="removed">−{d}</span>' if d else "",
        f'<span class="changed">{PENCIL}{e}</span>' if e else "",
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
        skipped = (f" {hidden} generated left out; with them {fa + fe + fd} files, "
                   f"+{fadds} / −{fdels}.")
    else:
        skipped = " No generated files to leave out."

    # The line count is the one number on the bar a reader wants to *open*: "+921 / −68"
    # is the size of what there is to read, and the next question is always what those
    # lines are. So it carries the compare page, and the file count beside it stays an
    # inert pill -- two identical-looking links to the same page is a row that teaches the
    # reader to ignore half of it.
    href = _compare_href(pr)
    lines_tip = f"+{adds} / −{dels} {where}.{skipped}"
    if href:
        lines_tip += " Opens the whole diff on github.com."
    return [
        {"label": "files",
         "value": files_value,
         "tip": f"{a} added, {e} edited, {d} deleted {where}.{skipped}"},
        {"label": "lines",
         "value": lines_value,
         "tip": lines_tip,
         **({"href": href} if href else {})},
    ]


def chip_face(c: dict) -> str:
    """A chip's own words: the label it was given and the value it measured. Shared by
    every renderer below so that a chip which moves house — the cost chip becoming half of
    the run chip — cannot pick up different markup on the way."""
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
