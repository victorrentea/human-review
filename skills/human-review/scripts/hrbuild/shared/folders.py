"""The folder behind a box: a Java package's directory, a Maven module's.

The Structure tab draws the code's shape — `packages.puml`'s boxes are packages, the
frontend's are folders named by path (`<<src/app/owners>>`), the module graph's are Maven
modules — and a reader looking at `..mcp` wants to see what is
in it. So every such box carries the folder it stands for, relative to the checkout
(`data-folder`), and a link to that folder on github.com at the reviewed commit. Served,
`FOLDERS_JS` turns the click into "reveal it in the VS Code window that has this
checkout" (`serve-review.py`'s `/__reveal_folder__`); off disk the link is all there is.

Nothing here knows petclinic. The package root is not configured: a box's stereotype is an
ArchUnit package pattern (`..rest.dto`), and the root is whichever package every pattern
in the diagram sits under — read off the tracked sources of the module the `.puml` lives
in. A module is the directory of the tracked `pom.xml` whose own `artifactId` is the box's
name. A box neither explains gets no link, rather than a guessed one.
"""
from __future__ import annotations

import functools
import html
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath

from .changes import changed_files, files_pop
from .snippets import github_blob_base

#: A component declaration, in the spellings PlantUML accepts: `[Label]`, `[Label] as a`,
#: `component "Label" as a`, `component a as "Label"`, `component Name`, and the same with
#: rectangle/node/package/folder/frame. A bare word without a keyword is not one — that is
#: a `skinparam` or an arrow's first half.
_DECL = re.compile(
    r'^\s*(?:(?:component|rectangle|node|package|folder|frame|artifact)\s+)?'
    r'(?:\[(?P<b>[^\]]+)\]|"(?P<q>[^"]+)"|(?P<w>[\w.\-]+))'
    r'(?:\s+as\s+(?:"(?P<aq>[^"]+)"|\[(?P<ab>[^\]]+)\]|(?P<aw>[\w.\-]+)))?'
    r'(?P<rest>.*)$')
_KEYWORD = re.compile(r'^\s*(?:component|rectangle|node|package|folder|frame|artifact)\s')
_STEREO = re.compile(r'<<\s*([^>]+?)\s*>>')
#: PlantUML's own markup for one box: `<g class="entity" data-qualified-name="…">`, a rect
#: and the name. Nested groups or an `<a>` already inside mean somebody else links it.
_ENTITY = re.compile(r'(<g class="entity"[^>]*?data-qualified-name="(?P<q>[^"]*)"[^>]*)>'
                     r'(?P<inner>.*?)</g>', re.S)
_SOURCE_DIR = re.compile(r'^(?P<src>(?:.*/)?src/main/(?:java|kotlin))/(?P<pkg>.+)$')


def _ls_files(root: Path, *spec: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "--", *spec],
                         capture_output=True, text=True)
    return out.stdout.splitlines() if out.returncode == 0 else []


def puml_boxes(text: str) -> list[dict]:
    """`{name, label, stereotype}` for each box a `.puml` declares. `name` is what
    PlantUML writes as `data-qualified-name` (the alias, or the label without one)."""
    boxes = []
    for line in text.splitlines():
        m = _DECL.match(line)
        if not m or not (m["b"] or m["q"] or _KEYWORD.match(line)):
            continue
        first = m["b"] or m["q"] or m["w"]
        alias = m["aq"] or m["ab"] or m["aw"]
        # `component a as "Label"`: the quoted side is the label, whichever side it is on.
        label, name = first, alias or first
        if alias and m["aq"] and not m["q"]:
            label, name = alias, first
        st = _STEREO.search(m["rest"] or "")
        boxes.append({"name": name.strip(), "label": label.strip(),
                      "stereotype": st[1].strip() if st else None})
    return boxes


def _module_dir(rel: str, root: Path) -> str:
    """The build module a diagram belongs to: its nearest ancestor with a build file."""
    for parent in PurePosixPath(rel).parents:
        d = root / parent
        if any((d / f).is_file() for f in ("pom.xml", "build.gradle", "build.gradle.kts",
                                           "package.json")):
            return "" if str(parent) == "." else str(parent)
    return ""


@functools.lru_cache(maxsize=None)
def _packages(root: Path, module: str) -> dict[str, list[str]]:
    """Dotted package → the directories (repo-relative) that hold it, from tracked sources."""
    found: dict[str, list[str]] = {}
    for path in _ls_files(root, module or "."):
        if not path.endswith((".java", ".kt")):
            continue
        m = _SOURCE_DIR.match(str(PurePosixPath(path).parent))
        if not m:
            continue
        parts = m["pkg"].split("/")
        for i in range(1, len(parts) + 1):
            dotted = ".".join(parts[:i])
            where = f'{m["src"]}/{"/".join(parts[:i])}'
            if where not in found.setdefault(dotted, []):
                found[dotted].append(where)
    return found


def _pattern(stereotype: str) -> tuple[str, bool] | None:
    """An ArchUnit package identifier as `(package, relative)`: `..rest.dto` is any
    package ending in `rest.dto`, `com.acme.rest` that package exactly. A trailing `..`
    (and its subpackages) names the same top folder. Wildcards and alternations name
    no one folder, so they get none."""
    s = stereotype.strip()
    if not s or re.search(r"[*()\[\]|\s]", s):
        return None
    relative = s.startswith("..")
    s = s.strip(".")
    return (s, relative) if s and ".." not in s else None


def package_folders(rel: str, root: Path) -> dict[str, str]:
    """Box name → the folder of the package its stereotype names, for one `.puml`."""
    src = root / rel
    if not src.is_file():
        return {}
    wanted, paths = {}, {}
    module = _module_dir(rel, root)
    for box in puml_boxes(src.read_text(encoding="utf-8", errors="replace")):
        st = box["stereotype"]
        # A folder spelled as a path, relative to the module (`<<src/app/owners>>`): how a
        # language without packages — the Angular frontend — names its parts.
        if st and "/" in st and not re.search(r"[*\s]", st):
            folder = str(PurePosixPath(module or ".") / st.strip("/"))
            if (root / folder).is_dir():
                paths[box["name"]] = folder
            continue
        p = st and _pattern(st)
        if p:
            wanted[box["name"]] = p
    if not wanted:
        return paths
    pkgs = _packages(root, module)
    # For each box, every package its pattern could mean, keyed by the root it implies.
    options: dict[str, dict[str, str]] = {}
    for name, (pkg, relative) in wanted.items():
        hits = {}
        for dotted, dirs in pkgs.items():
            if dotted == pkg:
                hits[""] = dirs[0]
            elif relative and dotted.endswith("." + pkg):
                hits[dotted[: -len(pkg) - 1]] = dirs[0]
        options[name] = hits
    # The root package is the one most boxes agree on — `victor.training.petclinic` for
    # all nine of petclinic's — the shallowest on a tie. A box that does not fit it still
    # gets its folder when its own pattern has exactly one answer.
    votes = Counter(base for hits in options.values() for base in hits)
    common = min(votes, key=lambda b: (-votes[b], b.count("."), b)) if votes else None
    out = dict(paths)
    for name, hits in options.items():
        if common in hits:
            out[name] = hits[common]
        elif len(hits) == 1:
            out[name] = next(iter(hits.values()))
    return out


def _artifact_id(pom: Path) -> str | None:
    """The project's own `artifactId`, not its parent's."""
    try:
        tree = ET.parse(pom)
    except (ET.ParseError, OSError):
        return None
    for child in tree.getroot():
        if child.tag.rsplit("}", 1)[-1] == "artifactId":
            return (child.text or "").strip() or None
    return None


@functools.lru_cache(maxsize=None)
def maven_modules(root: Path) -> dict[str, str]:
    """`artifactId` → the module's directory, for every tracked `pom.xml`. All of them
    rather than one reactor's `<modules>`: a repository of sibling modules has no
    aggregator to read, and a reactor's modules are tracked poms like any other."""
    out = {}
    for pom in _ls_files(root, "pom.xml", "*/pom.xml"):
        if "/node_modules/" in f"/{pom}" or pom.startswith(".human-review/"):
            continue
        aid = _artifact_id(root / pom)
        folder = str(PurePosixPath(pom).parent)
        if aid and aid not in out:
            out[aid] = "." if folder == "." else folder
    return out


@functools.lru_cache(maxsize=None)
def _tree_base(root: Path) -> str | None:
    """`https://github.com/<owner>/<repo>/tree/<HEAD>` — the folder as the reviewed commit
    has it — or None without a github.com remote."""
    repo = github_blob_base(root)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return f"{repo}/tree/{head}" if repo and head else None


def folder_targets(rel: str, root: Path) -> dict[str, tuple[str, str]]:
    """Box name → `(label, repo-relative folder)` for every box in `rel` that is a package
    or a Maven module of this checkout."""
    src = root / rel
    if not src.is_file():
        return {}
    boxes = puml_boxes(src.read_text(encoding="utf-8", errors="replace"))
    pkgs = package_folders(rel, root)
    modules = maven_modules(root)
    by_dir = {PurePosixPath(d).name: d for d in modules.values() if d != "."}
    out = {}
    for box in boxes:
        folder = (pkgs.get(box["name"]) or modules.get(box["label"])
                  or modules.get(box["name"]) or by_dir.get(box["label"]))
        if folder:
            out.setdefault(box["name"], (box["label"], folder))
    return out


_RECT = re.compile(r'<rect\b[^>]*?/?>')
_ATTR = re.compile(r'\b(x|y|width|height|rx|ry)="([\d.]+)"')


@functools.lru_cache(maxsize=None)
def _branch_files(root: Path, base: str) -> tuple[dict, ...]:
    """What the branch changed since the page's base, under the header's own rules: the
    generated files and the review's bookkeeping left out, as `files +N` leaves them."""
    from .chips import REVIEW_BOOKKEEPING, _project_cfg, generated_globs
    specs = [f":(exclude,glob){p}" for p in generated_globs(_project_cfg(root))]
    specs += [f":(exclude,glob){p}" for p in REVIEW_BOOKKEEPING]
    return tuple(changed_files(root, f"{base}...HEAD", specs))


def box_impact(targets: dict[str, tuple[str, str]], root: Path, base: str) -> dict[str, dict]:
    """Box name → `{files, adds, dels, share}`: the files the branch changed in the box's
    folder, each counted once, in the deepest box that holds it (`..rest.dto` is not also
    `..rest`). `share` is the box's part of the lines added across every box drawn — the
    bar behind it (Victor, 14 Oct 2026: "see in what package the PR added more lines")."""
    folders = sorted(((f.rstrip("/"), name) for name, (_, f) in targets.items()),
                     key=lambda t: -len(t[0]))
    out: dict[str, dict] = {}
    for f in _branch_files(root, base):
        for folder, name in folders:
            if folder == "." or f["path"].startswith(folder + "/"):
                box = out.setdefault(name, {"files": [], "adds": 0, "dels": 0})
                box["files"].append(f)
                box["adds"] += f["adds"] or 0
                box["dels"] += f["dels"] or 0
                break
    total = sum(b["adds"] for b in out.values())
    for b in out.values():
        b["share"] = b["adds"] / total if total else 0.0
    return out


def _impact_bar(inner: str, share: float) -> str:
    """A bar across the bottom of the box's own shape, as wide as its share, under the
    name: PlantUML's first `<rect>` is the box, and what comes after it is drawn on top."""
    m = _RECT.search(inner)
    if not m or share <= 0:
        return inner
    a = {k: float(v) for k, v in _ATTR.findall(m.group(0))}
    if not all(k in a for k in ("x", "y", "width", "height")):
        return inner
    w = max(a["width"] * share, 3.0)
    r = min(a.get("rx", 0.0), w / 2)
    bar = (f'<rect class="pkg-impact" x="{a["x"]:g}" y="{a["y"]:g}" width="{w:.1f}" '
           f'height="{a["height"]:g}" rx="{r:g}" ry="{r:g}" fill="var(--dgm-impact)" '
           'fill-opacity=".45" pointer-events="none"/>')
    return inner[:m.end()] + bar + inner[m.end():]


def _center_label(inner: str) -> str:
    """A one-line box's name centred on its box. PlantUML places it at a left padding it
    computed from its own font metrics; the browser's sans-serif is narrower, so the name
    drifted left in a box sized for it (Victor, 20 Oct 2026). A box with more than one
    line — a class with fields — is left as drawn."""
    tag = re.compile(r'<text\b([^>]*)>')
    texts = tag.findall(inner)
    m = _RECT.search(inner)
    if len(texts) != 1 or not m or not re.search(r'\bx="', texts[0]):
        return inner
    a = {k: float(v) for k, v in _ATTR.findall(m.group(0))}
    if "x" not in a or "width" not in a:
        return inner
    attrs = re.sub(r'\bx="[\d.]+"', f'x="{a["x"] + a["width"] / 2:.2f}"', texts[0], count=1)
    if "text-anchor" not in attrs:
        attrs += ' text-anchor="middle"'
    return tag.sub(lambda _: f"<text{attrs}>", inner, count=1)


def _impact_pop(label: str, box: dict, base: str, root: Path, folder: str) -> str:
    n = len(box["files"])
    pct = round(box["share"] * 100)
    nums = " ".join(x for x in (f"+{box['adds']}" if box["adds"] else "",
                                f"−{box['dels']}" if box["dels"] else "") if x)
    # The first line says what the green is (Victor, 14 Oct 2026: "they won't get it"),
    # the second what is under it and what a click does.
    head = (f"Green bar: <b>{pct}%</b> of the lines this PR added in this diagram are in "
            f"<b>{html.escape(label)}</b><br>{nums} in {n} file{'s' if n != 1 else ''}"
            " · click a file for its diff")
    return files_pop(head, box["files"], base, root, strip=folder)


#: A box the diff itself paints: added (green) or removed (red), themed or not yet.
_PAINTED = re.compile(r'--dgm-diff-(?:add|del)\b|#(?:2E7D32|C62828)\b', re.I)

#: The card's "Only impacted" filter, beside its status chip, on a card that has a quiet box.
#: Off by default; `folders.js` hides the quiet boxes and every arrow to one, but keeps a
#: quiet box that a green or red arrow still points at, so the change never dangles.
ONLY_IMPACTED = ('<label class="dgm-only" data-tip="Hide the boxes this PR left alone, '
                 'and their arrows"><input type="checkbox">Only impacted</label>')


def only_impacted(markup: str) -> str:
    """The filter for a card whose `markup` has a quiet box, else ""."""
    return ONLY_IMPACTED if 'data-quiet=""' in markup else ""


def link_folders(markup: str, rel: str, root: Path) -> str:
    """Every box in `markup` (inlined SVGs of `rel`) that is a package or a module carries
    `data-folder` / `data-folder-name`, and its shapes are wrapped in a link to the folder
    on github.com when the checkout has a remote there. Boxes already linked are left be.

    A box the branch changed files in is shaded, left to right, by its share of the lines
    added, and hovering it lists those files, each a link to its diff (`data-tip-pop`)."""
    targets = folder_targets(rel, root) if rel else {}
    if not targets:
        return markup
    tree = _tree_base(root)
    from . import snippets
    base = snippets.SNIPPET_BASE
    impact = box_impact(targets, root, base)

    def one(m: re.Match) -> str:
        name = html.unescape(m["q"])
        hit = targets.get(name)
        if not hit or "<a " in m["inner"] or "<g" in m["inner"] or "data-folder=" in m[1]:
            return m.group(0)
        label, folder = hit
        attrs = (f' data-folder="{html.escape(folder, quote=True)}"'
                 f' data-folder-name="{html.escape(label, quote=True)}"')
        inner = _center_label(m["inner"])
        box = impact.get(name)
        if not box and not _PAINTED.search(m["inner"]):
            # Nothing of this box changed — no file in its folder, no green or red on it —
            # so it is drawn quiet, the same in the Diff as under New and Old, and the eye
            # goes to the boxes that did (Victor, 29 Oct 2026). `ONLY_IMPACTED` hides it.
            attrs += ' data-quiet=""'
        if box:
            pop = _impact_pop(label, box, base, root, folder)
            attrs += (f' data-impact="{box["share"]:.3f}"'
                      f' data-tip-pop="{html.escape(pop, quote=True)}"')
            inner = _impact_bar(inner, box["share"])
        if tree:
            href = tree if folder == "." else f"{tree}/{urllib.parse.quote(folder)}"
            tip = html.escape(f"Open {label} on GitHub", quote=True)
            inner = f'<a href="{html.escape(href, quote=True)}" data-tip="{tip}">{inner}</a>'
        return f"{m[1]}{attrs}>{inner}</g>"

    return _ENTITY.sub(one, markup)
