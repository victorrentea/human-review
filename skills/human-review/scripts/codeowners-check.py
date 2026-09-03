#!/usr/bin/env python3
"""Does this change set need a second pair of eyes? — asked of CODEOWNERS, not of us.

A repository's `CODEOWNERS` file is a standing decision somebody already made: *these
paths do not move without a named human agreeing*. The API contract, the migrations,
the guardrail tests, the dependency manifests. GitHub enforces it at merge time — which
is far too late to be useful to the person doing the review, who by then has already
read the diff without knowing that a line of it was load-bearing enough to be owned.

So this script asks the question at review time: intersect the change set with the
rules, and if anything owned was touched, raise a flag the reviewer cannot miss and
name **which files caused it** and **which rule claimed them**.

It is not a gate. It cannot approve anything and it does not try to guess whether the
owner already looked. It answers one question — *will this pull request be blocked
waiting for somebody, and for whom* — early enough to matter.

Semantics, which differ by host and are stated on the page rather than assumed:

  * **GitHub** — one flat list, and for each file the **last matching rule wins**. A
    later broad rule silently overrides an earlier narrow one, which is the single most
    surprising thing about the format and worth showing.
  * **GitLab** — `[Section]` headers; each section matches independently and every
    matching section's owners are required. `^[Section]` is optional: it suggests
    reviewers, it does not block. Applied automatically when the file has sections.

Emits an HTML fragment for `build-review-html.py` to include, in the same shape as its
siblings; `--css` prints the stylesheet it needs, `--json` the machine summary, and
`--state` the one word: `approval_required` · `no_owners_touched` · `no_codeowners`.

Usage (from the repository root — the project is resolved from the CWD):
    codeowners-check.py --base origin/main --out .human-review/assets/codeowners.html
    codeowners-check.py --css > .human-review/assets/codeowners.css
    codeowners-check.py --state
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

APPROVAL_REQUIRED, CLEAR, NO_FILE = "approval_required", "no_owners_touched", "no_codeowners"
# Where hosts look, in the order GitHub resolves them. The first one that exists wins:
# a repository with two of these has one that is being quietly ignored, and we say so.
LOCATIONS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")

# `[Name]`, `^[Name]`, `[Name][2]`, `[Name] @default-owner` — GitLab's section header.
SECTION = re.compile(r"^(?P<optional>\^)?\[(?P<name>[^\]]+)\](?:\[(?P<count>\d+)\])?(?P<rest>.*)$")
OWNER = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9._/-]*|[^@\s]+@[^@\s]+\.[A-Za-z]{2,})$")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def repo_root() -> Path:
    out = run(["git", "rev-parse", "--show-toplevel"])
    if out.returncode != 0:
        raise SystemExit("[codeowners] not inside a git repository")
    return Path(out.stdout.strip())


# ── the pattern language ──────────────────────────────────────────────────────────
def pattern_to_regex(pattern: str) -> re.Pattern:
    """CODEOWNERS patterns are gitignore's, minus negation.

    The two rules people get wrong, and the reason this is not `fnmatch`:
    a `*` never crosses a `/`, and a pattern containing a slash anywhere but at its
    end is anchored to the repository root — `docs/api` means *that* directory, while
    a bare `api` means one at any depth.
    """
    p = pattern
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]
    anchored = p.startswith("/") or "/" in p.rstrip("/")
    p = p.lstrip("/")

    out, i = [], 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out.append("(?:.*/)?")  # any number of leading directories, including none
            i += 3
        elif p.startswith("/**", i) and i + 3 == len(p):
            out.append("/.*")  # `a/**` is everything under a
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1

    body = "".join(out)
    if not anchored:
        body = "(?:.*/)?" + body
    # A rule naming a directory claims everything inside it — whether it was written
    # with a trailing slash, as `a/**`, or as a bare `a`.
    return re.compile("^" + body + "(?:/.*)?$")


class Rule:
    def __init__(self, pattern: str, owners: list, line: int, section: str | None,
                 optional: bool):
        self.pattern, self.owners, self.line = pattern, owners, line
        self.section, self.optional = section, optional
        self.regex = pattern_to_regex(pattern)

    def matches(self, path: str) -> bool:
        return bool(self.regex.match(path))


def parse(text: str):
    """→ (rules, sectioned, problems). `problems` are lines a host would also ignore."""
    rules, problems, sectioned = [], [], False
    section, optional, defaults = None, False, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head = SECTION.match(line)
        if head:
            sectioned = True
            section = head.group("name").strip()
            optional = bool(head.group("optional"))
            defaults = [t for t in head.group("rest").split() if OWNER.match(t)]
            continue
        if line.startswith("!"):
            # gitignore has negation; CODEOWNERS does not, and GitHub ignores the line
            # rather than failing — so a `!` rule protects nothing while looking like it does.
            problems.append((n, line, "negation is not supported in CODEOWNERS — the line is ignored"))
            continue
        parts = line.split()
        pattern, owners = parts[0], parts[1:]
        bad = [o for o in owners if not OWNER.match(o)]
        if bad:
            problems.append((n, line, "not a user, team or email: " + ", ".join(bad)))
        owners = [o for o in owners if OWNER.match(o)] or list(defaults)
        if not owners:
            # A pattern with no owner is how GitHub *un*-owns a path claimed above it.
            rules.append(Rule(pattern, [], n, section, optional))
            continue
        rules.append(Rule(pattern, owners, n, section, optional))
    return rules, sectioned, problems


def owners_for(path: str, rules: list, sectioned: bool) -> list:
    """The rules that claim one file, in the host's own semantics.

    Flat (GitHub): the last match wins, and only that one — a later broad rule
    overrides an earlier narrow one. Sectioned (GitLab): every section gets its own
    last match, and all of them are required.
    """
    if not sectioned:
        hit = None
        for r in rules:
            if r.matches(path):
                hit = r
        return [hit] if hit and hit.owners else []
    per_section = {}
    for r in rules:
        if r.matches(path):
            per_section[r.section] = r
    return [r for r in per_section.values() if r.owners]


# ── the change set ────────────────────────────────────────────────────────────────
def changed_files(root: Path, base: str, untracked: bool):
    """Every file this branch would put in a pull request, as (path, status).

    Diffed against the **merge-base**, so commits that landed on the base after the
    branch started are not attributed to it, and taken against the *working tree*, so
    the fixes the review pipeline itself left uncommitted are counted too — they are
    part of what gets merged.
    """
    mb = run(["git", "merge-base", base, "HEAD"], cwd=root)
    base_ref = mb.stdout.strip() if mb.returncode == 0 else base
    diff = run(["git", "diff", "--name-status", "-M", base_ref], cwd=root)
    if diff.returncode != 0:
        raise SystemExit(f"[codeowners] cannot diff against {base}: {diff.stderr.strip()}")
    files = {}
    for row in diff.stdout.splitlines():
        cols = row.split("\t")
        if len(cols) < 2:
            continue
        # A rename is two paths and touches both sides: the old path is as owned as the new.
        status = cols[0][0]
        if status == "R":
            files[cols[1]] = "D"
            files[cols[2]] = "A"
        else:
            files[cols[1]] = status
    if untracked:
        others = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
        for path in others.stdout.splitlines():
            files.setdefault(path, "A")
    return sorted(files.items()), base_ref


def analyse(root: Path, base: str, untracked: bool) -> dict:
    found = [p for p in LOCATIONS if (root / p).is_file()]
    files, base_ref = changed_files(root, base, untracked)
    if not found:
        return {"state": NO_FILE, "codeowners": None, "shadowed": [], "rules": 0,
                "owned": [], "unowned": [f for f, _ in files], "problems": [],
                "base_ref": base_ref, "sectioned": False, "changed": len(files)}

    rel = found[0]
    rules, sectioned, problems = parse((root / rel).read_text(encoding="utf-8"))
    owned, unowned = [], []
    for path, status in files:
        hits = owners_for(path, rules, sectioned)
        if not hits:
            unowned.append(path)
            continue
        owned.append({
            "path": path,
            "status": status,
            "owners": sorted({o for r in hits for o in r.owners}),
            "rules": [{"pattern": r.pattern, "line": r.line, "section": r.section,
                       "optional": r.optional} for r in hits],
            # A file claimed only by optional sections suggests a reviewer, it does not block.
            "blocking": any(not r.optional for r in hits),
        })
    state = APPROVAL_REQUIRED if any(o["blocking"] for o in owned) else CLEAR
    return {"state": state, "codeowners": rel, "shadowed": found[1:], "rules": len(rules),
            "owned": owned, "unowned": unowned, "problems": problems, "base_ref": base_ref,
            "sectioned": sectioned, "changed": len(files)}


# ── rendering ─────────────────────────────────────────────────────────────────────
STATUS_LABEL = {"A": "added", "M": "modified", "D": "deleted", "C": "copied", "T": "retyped"}


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def file_link(root: Path, path: str, status: str, line: int = 1, label: str = None) -> str:
    label = html.escape(label if label is not None else path)
    if status == "D" or not (root / path).exists():
        return f'<code class="cow-gone">{label}</code>'
    return (f'<a class="cow-file" href="vscode://file/{(root / path).resolve()}:{line}:1">'
            f"{label}</a>")


def render(root: Path, data: dict) -> str:
    state = data["state"]
    owned, blocking = data["owned"], [o for o in data["owned"] if o["blocking"]]
    by_owner = {}
    for entry in blocking:
        for owner in entry["owners"]:
            by_owner.setdefault(owner, []).append(entry)

    flag = {APPROVAL_REQUIRED: "&#128681; APPROVAL REQUIRED",
            CLEAR: "NO OWNER TOUCHED", NO_FILE: "NOT CONFIGURED"}[state]
    parts = [
        f'<div class="cow cow-{state}">',
        '<div class="cow-verdict">'
        f'<span class="cow-seal">{flag}</span></div>',
    ]

    if data["shadowed"]:
        listed = ", ".join(f"<code>{html.escape(p)}</code>" for p in data["shadowed"])
        parts.append(f'<div class="cow-warn"><b>A second CODEOWNERS is being ignored.</b> '
                     f'{listed} exists too, but a host reads only the first of '
                     f'<code>{html.escape(data["codeowners"])}</code> — the other protects '
                     "nothing while looking like it does.</div>")

    for owner, entries in sorted(by_owner.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rows = []
        for e in sorted(entries, key=lambda x: x["path"]):
            rules = " ".join(
                f'<a class="cow-rule" data-tip="The rule that claimed this file"'
                f' href="vscode://file/{(root / data["codeowners"]).resolve()}:{r["line"]}:1">'
                f'{html.escape(r["pattern"])}</a>' for r in e["rules"]
            )
            # A rule that is just the file's own path says nothing the row has not
            # already said, twice. Only a *pattern* — one that could have claimed other
            # files too — is worth naming, and then the path is legible from the pattern,
            # so the row leads with the bare filename instead of repeating the directories.
            patterns = [r for r in e["rules"] if r["pattern"].lstrip("/") != e["path"]]
            by = (f'<span class="cow-by">claimed by {rules}</span>' if patterns else "")
            shown = Path(e["path"]).name if patterns else e["path"]
            rows.append(f'<li><span class="cow-st cow-st-{e["status"].lower()}">'
                        f'{STATUS_LABEL.get(e["status"], e["status"])}</span>'
                        f'{file_link(root, e["path"], e["status"], label=shown)}'
                        f'{by}</li>')
        parts.append(f'<div class="cow-row"><div class="cow-head">'
                     f'<span class="cow-owner">{html.escape(owner)}</span>'
                     f'<span class="cow-note">have to approve {plural(len(entries), "file")}:'
                     "</span></div>"
                     f'<ul class="cow-files">{"".join(rows)}</ul></div>')

    advisory = [o for o in owned if not o["blocking"]]
    if advisory:
        rows = "".join(
            f'<li><span class="cow-st cow-st-{e["status"].lower()}">'
            f'{STATUS_LABEL.get(e["status"], e["status"])}</span>'
            f'{file_link(root, e["path"], e["status"])}'
            f'<span class="cow-by">{", ".join(html.escape(o) for o in e["owners"])}</span></li>'
            for e in sorted(advisory, key=lambda x: x["path"]))
        parts.append('<div class="cow-kind">Suggested reviewers, not required '
                     f'<span class="cow-count">{len(advisory)}</span></div>'
                     f'<ul class="cow-files cow-advisory">{rows}</ul>')

    if data["problems"]:
        rows = "".join(f'<li><code>CODEOWNERS:{n}</code> <code>{html.escape(line)}</code> — '
                       f"{html.escape(why)}</li>" for n, line, why in data["problems"])
        parts.append('<div class="cow-warn"><b>Lines a host would skip.</b> They are in the '
                     f'file but they claim nothing:<ul class="cow-files">{rows}</ul></div>')

    parts.append("</div>")
    return "\n".join(parts)


CSS = """
.cow { --cow-bad:#c62828; --cow-ok:#2e7d32; --cow-warn:#b56b00; --cow-flat:#6b6b78;
       margin:.4rem 0 1rem; }
.cow-verdict { display:flex; align-items:center; gap:.9rem; border:1px solid var(--line);
               border-left:4px solid var(--cow-flat); border-radius:10px; padding:.8rem 1rem;
               background:var(--card); }
.cow-approval_required .cow-verdict { border-left-color:var(--cow-bad); }
.cow-no_owners_touched .cow-verdict { border-left-color:var(--cow-ok); }
.cow-seal { font:800 .7rem/1.9 inherit; letter-spacing:.08em; border-radius:5px;
            padding:.1rem .55rem; white-space:nowrap; background:#f0f0f4; color:#5d5d6b; }
.cow-approval_required .cow-seal { background:#fdeaea; color:#8a1c1c; }
.cow-no_owners_touched .cow-seal { background:#eef7ef; color:#245c30; }
.cow-prov { color:var(--muted); font-size:.8rem; line-height:1.7; margin:.5rem 0 1rem; }
.cow-kind { font:600 .82rem/1.6 inherit; text-transform:uppercase; letter-spacing:.06em;
            color:var(--muted); border-bottom:1px solid var(--line); padding-bottom:.3rem;
            margin-top:1.2rem; }
.cow-count { background:var(--code-bg); border-radius:999px; padding:0 .4rem; margin-left:.3rem;
             font-size:.72rem; }
.cow-row { background:var(--card); border:1px solid var(--line); border-left:3px solid
           var(--cow-bad); border-radius:8px; padding:.55rem .8rem; margin:.5rem 0; }
.cow-head { display:flex; align-items:baseline; gap:.6rem; flex-wrap:wrap; }
.cow-owner { font:700 13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg); }
.cow-note { color:var(--muted); font-size:.8rem; }
.cow-files { list-style:none; margin:.45rem 0 0; padding:0; display:grid; gap:.3rem; }
.cow-files li { font-size:.86rem; line-height:1.6; display:flex; align-items:baseline;
                gap:.5rem; flex-wrap:wrap; }
.cow-st { font:700 9.5px/1.7 ui-monospace,Menlo,monospace; text-transform:uppercase;
          letter-spacing:.05em; border-radius:4px; padding:0 .35rem; color:#fff;
          background:var(--cow-flat); }
.cow-st-a { background:#2e7d32; } .cow-st-m { background:#b56b00; }
.cow-st-d { background:#c62828; }
.cow-file, .cow-gone { font:500 12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; }
.cow-gone { color:var(--muted); text-decoration:line-through; }
.cow-by { color:var(--muted); font-size:.78rem; }
.cow-rule { font:600 11.5px/1.6 ui-monospace,Menlo,monospace; background:var(--code-bg);
            border-radius:3px; padding:0 .3rem; }
.cow-warn { margin:1rem 0 0; border-radius:8px; padding:.65rem .85rem; font-size:.86rem;
            line-height:1.7; background:#fdf3e2; color:#6b4a0f; border:1px solid #e5c98f; }
.cow-advisory { margin-top:.6rem; }
.cow-rest { margin:1.2rem 0 0; }
.cow-rest summary { cursor:pointer; color:var(--muted); font-size:.85rem; }
.cow-rest .cow-files { margin-top:.5rem; }
@media (prefers-color-scheme: dark) {
  .cow-seal { background:#26262f; color:#a5a5b4; }
  .cow-approval_required .cow-seal { background:#3a1f1f; color:#f2a0a0; }
  .cow-no_owners_touched .cow-seal { background:#1b2c1f; color:#9ad3a5; }
  .cow-warn { background:#3a3018; color:#e6c07b; border-color:#6b5520; }
}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", default="origin/main", help="git ref the change set starts from")
    ap.add_argument("--out", help="write the HTML fragment here instead of stdout")
    ap.add_argument("--json", action="store_true", help="print the machine summary on stdout")
    ap.add_argument("--state", action="store_true",
                    help=f"print only {APPROVAL_REQUIRED} / {CLEAR} / {NO_FILE}")
    ap.add_argument("--css", action="store_true", help="print the stylesheet this fragment needs")
    ap.add_argument("--no-untracked", action="store_true",
                    help="ignore new files that are not yet added to the index")
    args = ap.parse_args(argv)

    if args.css:
        print(CSS)
        return 0

    root = repo_root()
    data = analyse(root, args.base, not args.no_untracked)

    if args.state:
        print(data["state"])
        return 0

    fragment = render(root, data)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(fragment, encoding="utf-8")
        print(f"[codeowners] {data['state']} — wrote {out}", file=sys.stderr)
    elif not args.json:
        print(fragment)

    if args.json:
        summary = {k: v for k, v in data.items() if k != "problems"}
        summary["problems"] = [{"line": n, "text": t, "why": w} for n, t, w in data["problems"]]
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
