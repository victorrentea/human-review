#!/usr/bin/env python3
"""Everything GitHub-specific about the reviewed repository goes through one module.

`hrbuild/shared/forge.py` is the seam a second forge (Bitbucket, issue #4) plugs into: the
web URLs of the reviewed repository, the URL spellings that recognise one, and every `gh`
call. This fails the day a script calls `gh` or spells a `https://github.com/` URL itself,
because that call is one more place a second forge would have to be taught about, and one
nobody would find until a Bitbucket page linked to github.com.

Read from the source (AST for Python, lines for shell), not from a run: the point is where
the code is written, not what it did on this machine.

Run with:  python3 -m pytest test_forge_seam.py
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent.parent                     # skills/: human-review and record-review
SEAM = HERE / "hrbuild" / "shared" / "forge.py"

#: `github.com/<owner>/` links that are not the reviewed repository: the skill's own home
#: (footer, adopt panel, "clone victorrentea/human-review" tips, the extractor's source),
#: the demo projects the adopt panel quotes (petclinic, code-city), and the third-party
#: tools the API tab cites. Where the tool lives, not where the reviewed code is pushed.
FOREIGN_OWNERS = ("victorrentea/", "oasdiff/", "OpenAPITools/")

#: `(file under skills/, text the flagged string contains)`: the deliberate exceptions.
ALLOWED = {
    # Prompt permissions handed to the model (a deny-list of command prefixes), not a call.
    ("human-review/scripts/rerun-review.py", "gh pr"),
    # The skill's own Pages site, derived from *its* origin so a fork links to itself.
    ("human-review/scripts/readme-tour.py", "github.com[:/]"),
    # The skill's own issue tracker (`HOME_URL`), the footer's "report a problem".
    ("human-review/scripts/hrbuild/shared/footer.py", "{}/issues/new/choose"),
    # Page JavaScript: the review tab's "in VS Code" PR link, recognised again in the
    # browser for comments that arrive after the build. It mirrors `forge.pull_root`.
    ("human-review/scripts/hrbuild/tabs/review.py", "function hrVscPr(url)"),
}

#: A web path only a forge lays out this way, right after a repository URL.
FORGE_PATH = re.compile(r"/(?:blob|tree|commit|compare|pull|issues)/")

GH_SHELL = re.compile(r"(?:^|[\s$(|;&`])gh\s+(?:api|pr|run|issue|repo|workflow|auth|release)\b")


def _sources(suffix: str) -> list[Path]:
    return sorted(p for p in SKILLS.rglob(f"*{suffix}")
                  if "__pycache__" not in p.parts and not p.name.startswith("test_")
                  and p.name != "conftest.py")


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            out.add(id(n.body[0].value))
    return out


def _foreign_only(text: str) -> bool:
    hosts = re.findall(r"github\.com/(\S*)", text.replace("\\", ""))
    return bool(hosts) and all(h.startswith(FOREIGN_OWNERS) for h in hosts)


def python_findings(path: Path) -> list[tuple[int, str]]:
    """`(line, what)` for every `gh` call and every github.com string in `path`'s code.
    Docstrings are prose and left alone; log lines naming `gh issue view` start with a
    bracket or a backtick, never with `gh `, so they are not calls either."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and n.elts \
                and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == "gh":
            out.append((n.lineno, '["gh", …] argv'))
            continue
        if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "which" \
                and any(isinstance(a, ast.Constant) and a.value == "gh" for a in n.args):
            out.append((n.lineno, 'which("gh")'))
            continue
        if isinstance(n, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)
            # `f"{repo}/commit/{sha}"`: the forge's path layout glued onto a repo URL.
            if any(isinstance(a, ast.FormattedValue) and isinstance(b, ast.Constant)
                   and FORGE_PATH.match(b.value) for a, b in zip(n.values, n.values[1:])):
                out.append((n.lineno, text[:120]))
                continue
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and id(n) not in docs and id(n) not in parts:
            text = n.value
        else:
            continue
        if text.startswith("gh "):
            out.append((n.lineno, text[:120]))
        elif "github.com" in text.replace("\\", "") and not _foreign_only(text):
            out.append((n.lineno, text[:120]))
    return out


def shell_findings(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        if GH_SHELL.search(code) or "https://github.com/" in code and not _foreign_only(code):
            out.append((i, line.strip()[:120]))
    return out


def _allowed(rel: str, text: str) -> bool:
    return any(rel == f and needle in text.replace("\\", "") for f, needle in ALLOWED)


def test_nothing_outside_the_forge_module_calls_gh_or_spells_a_github_url():
    bad = []
    for path in _sources(".py") + _sources(".sh"):
        if path == SEAM:
            continue
        rel = path.relative_to(SKILLS).as_posix()
        found = python_findings(path) if path.suffix == ".py" else shell_findings(path)
        bad += [f"{rel}:{line}: {text!r}" for line, text in found if not _allowed(rel, text)]
    assert bad == [], ("GitHub-specific code outside hrbuild/shared/forge.py — move it into "
                       "the forge, or allow it in ALLOWED with the reason:\n" + "\n".join(bad))


def test_the_scan_sees_the_forge_modules_own_calls():
    """A scanner that finds nothing anywhere would pass the test above for ever."""
    found = " ".join(text for _, text in python_findings(SEAM))
    for needle in ('["gh", …] argv', 'which("gh")', "gh run list", "https://github.com/{}"):
        assert needle in found


def test_every_allowance_is_still_needed():
    """An exception whose code has gone is a hole someone else can walk through."""
    stale = []
    for f, needle in ALLOWED:
        path = SKILLS / f
        texts = [t for _, t in (python_findings(path) if path.suffix == ".py"
                                else shell_findings(path))] if path.is_file() else []
        if not any(needle in t.replace("\\", "") for t in texts):
            stale.append(f"{f}: {needle!r}")
    assert stale == []
