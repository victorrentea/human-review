#!/usr/bin/env python3
"""The C4 views a repository writes in Structurizr DSL, drawn by Structurizr itself.

A `workspace { model { … } views { … } }` file is the C4 model as text, and its views are
meant to be *seen* the way Simon Brown's renderer draws them — the boxes, the colours, the
auto-layout that Structurizr Lite shows on the right of its view explorer. Redrawing them
through C4-PlantUML (what a guardrail test in the repository may already do) gives a
different picture of the same model; this step asks Structurizr for its own.

How, in three moves, none of which opens a port:

  1. **Export a static site.** `structurizr/structurizr export -f static` (the image that
     replaced Lite and the CLI) parses the DSL and writes the Structurizr diagram viewer —
     its JointJS renderer, its themes, its dark mode — as plain files, with the workspace
     baked into `workspace.js`. One `docker run --rm`, about a second; the container is
     named by the id Docker hands back (`--cidfile`) and only that id is ever removed.
  2. **Ask the viewer for SVGs.** A headless Chromium opens that site from disk and calls
     the viewer's own scripting API — `structurizr.scripting.exportViews(…, {format:
     'svg'})`, the call Structurizr's puppeteer export script makes — once in light mode
     and once in dark, cropped, without the title/date block the card's header already
     carries.
  3. **Compare the two models, not the two pictures.** The same is done for the
     merge-base's copy of the DSL (read out of git). A view is `unchanged` when every
     element and relationship it shows — names, descriptions, technology, tags — and its
     own definition and the workspace's styles are the same on both sides. Pixels are no
     use for this: the renderer lays out on the client, and ids move between runs.

**Which test reads it.** A DSL is often parsed by a guardrail test (petclinic's
`C3ArchTest` checks the components against the Java packages). Every test source that
names the DSL file is read for *what level it checks* — `getComponents()` / `instanceof
Component` for components, `getContainers()` / `instanceof Container` for containers — so a
card can say truthfully whether its boxes are compared with the code or only drawn by hand.

Output (default `.human-review/assets/c4/`):

    <view>.new.light.svg / .new.dark.svg   the view at the work tree, light and dark
    <view>.old.light.svg / .old.dark.svg   the same at the merge-base (only when it differs)
    MANIFEST.tsv                           one row per view, read by hrbuild/shared/c4.py
    verdict.json                           what ran, on which inputs, and why not

Exit codes: 0 = drawn, 3 = no Structurizr workspace in this repository (a quiet skip),
4 = Docker or the renderer is unavailable (a soft skip: the tab says so), 2 = bad usage.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE = os.environ.get("HUMAN_REVIEW_STRUCTURIZR_IMAGE", "structurizr/structurizr")

#: The view types in the order a reviewer wants them: the containers first (the C2 is the
#: picture a C4 model exists for), then the components, then the context and the rest.
TYPE_ORDER = {"Container": 0, "Component": 1, "SystemContext": 2, "SystemLandscape": 3,
              "Dynamic": 4, "Deployment": 5, "Custom": 6, "Filtered": 7, "Image": 8}

#: `workspace.views.<list>` → the view type its entries are.
VIEW_LISTS = {"systemLandscapeViews": "SystemLandscape", "systemContextViews": "SystemContext",
              "containerViews": "Container", "componentViews": "Component",
              "dynamicViews": "Dynamic", "deploymentViews": "Deployment",
              "customViews": "Custom", "filteredViews": "Filtered", "imageViews": "Image"}

#: Directives whose argument is a path relative to the file that holds them.
DIRECTIVE = re.compile(r'^\s*!(include|docs|adrs|image|script)\s+(?:"([^"]+)"|(\S+))', re.M)

COLUMNS = ("name", "title", "description", "type", "status", "source",
           "new_light", "new_dark", "old_light", "old_dark", "note")

RENDER_TIMEOUT = 120


def git(root: Path, *args: str, binary: bool = False):
    out = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if out.returncode != 0:
        return None
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines()
                     if not re.match(r"\s*(#|//)", line))


def is_workspace(text: str) -> bool:
    """A file Structurizr can be pointed at, not a fragment another one `!include`s."""
    return bool(re.match(r"\s*workspace\b", _strip_comments(text)))


class Side:
    """One side of the comparison: the work tree, or a commit read out of git."""

    def __init__(self, root: Path, rev: str | None):
        self.root, self.rev = root, rev

    def read(self, rel: str) -> bytes | None:
        if self.rev is None:
            p = self.root / rel
            return p.read_bytes() if p.is_file() else None
        return git(self.root, "show", f"{self.rev}:{rel}", binary=True)

    def is_dir(self, rel: str) -> bool:
        if self.rev is None:
            return (self.root / rel).is_dir()
        return git(self.root, "cat-file", "-t", f"{self.rev}:{rel}") == "tree\n"

    def files_under(self, rel: str) -> list[str]:
        if self.rev is None:
            out = git(self.root, "ls-files", "-co", "--exclude-standard", "--", rel) or ""
        else:
            # `ls-tree` takes path prefixes, not globs: list the tree and match here.
            out = git(self.root, "ls-tree", "-r", "--name-only", self.rev) or ""
            return [p for p in out.splitlines() if p and (
                fnmatch.fnmatch(p, rel) if any(c in rel for c in "*?[")
                else p == rel or p.startswith(rel.rstrip("/") + "/"))]
        return [p for p in out.splitlines() if p]

    def dsl_files(self) -> list[str]:
        return [p for p in self.files_under("*.dsl")
                if not p.startswith(".human-review/") and p.endswith(".dsl")]


def workspaces(side: Side) -> list[str]:
    out = []
    for rel in side.dsl_files():
        data = side.read(rel)
        if data is not None and is_workspace(data.decode("utf-8", "replace")):
            out.append(rel)
    return out


def _norm(rel: str) -> str | None:
    parts = []
    for seg in rel.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None          # outside the repository: not ours to copy
            parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def closure(side: Side, entry: str) -> dict[str, bytes]:
    """The workspace file and everything it pulls in by a relative path, recursively."""
    files: dict[str, bytes] = {}
    todo = [entry]
    while todo:
        rel = todo.pop()
        if rel in files:
            continue
        data = side.read(rel)
        if data is None:
            continue
        files[rel] = data
        if not rel.endswith(".dsl"):
            continue
        here = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for m in DIRECTIVE.finditer(data.decode("utf-8", "replace")):
            target = m.group(2) or m.group(3)
            if "://" in target:
                continue
            ref = _norm(f"{here}/{target}" if here else target)
            if ref is None:
                continue
            if side.is_dir(ref):
                todo.extend(side.files_under(ref))
            else:
                todo.append(ref)
    return files


def fingerprint(*sets: dict[str, bytes] | None) -> str:
    h = hashlib.sha256(IMAGE.encode())
    for files in sets:
        h.update(b"\0side\0")
        for rel in sorted(files or {}):
            h.update(rel.encode() + b"\0" + hashlib.sha256(files[rel]).digest())
    return h.hexdigest()[:20]


def docker_ready() -> str | None:
    """None when `docker run` can work, else why not — in words for the card."""
    if shutil.which("docker") is None:
        return "Docker is not installed"
    try:
        out = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                             capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "Docker did not answer within 15 s"
    if out.returncode != 0 or not out.stdout.strip():
        return "Docker is not running"
    return None


def export_static(work: Path, entry: str) -> tuple[Path | None, str]:
    """`structurizr export -f static` in a throwaway container. No port is published:
    the container reads the DSL from a bind mount and writes the site back into it."""
    cid = work / ".cid"
    cid.unlink(missing_ok=True)
    # As the caller: the image runs as a fixed uid that cannot write into a host-owned
    # directory on Linux (Docker Desktop on a Mac maps it either way).
    cmd = ["docker", "run", "--rm", "--cidfile", str(cid), "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{work}:/w", IMAGE, "export", "-w", f"/w/{entry}", "-f", "static",
           "-o", "/w/.hr-static"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        if cid.is_file() and cid.read_text().strip():
            # Only the container this call started, by the id Docker wrote for it.
            subprocess.run(["docker", "rm", "-f", cid.read_text().strip()],
                           capture_output=True)
        return None, f"structurizr export timed out after {RENDER_TIMEOUT} s"
    site = work / ".hr-static"
    if out.returncode != 0 or not (site / "workspace.js").is_file():
        tail = [l for l in (out.stderr + out.stdout).splitlines()
                if "ERROR" in l or "Exception" in l or "error" in l.lower()]
        why = (tail[-1] if tail else f"exit {out.returncode}").strip().replace(" /w/", " ")
        if "Unable to find image" in out.stderr and out.returncode != 0:
            why = f"could not pull {IMAGE}"
        return None, f"Structurizr could not parse {entry}: {why[:300]}"
    return site, ""


def workspace_json(site: Path) -> dict:
    js = (site / "workspace.js").read_text(encoding="utf-8")
    m = re.search(r"jsonAsString\s*=\s*'([^']*)'", js)
    return json.loads(base64.b64decode(m.group(1)).decode("utf-8")) if m else {}


# ── comparing two workspaces view by view ────────────────────────────────────────────

def _walk_elements(model: dict):
    """Every element with the chain of names that identifies it across two parses (ids are
    assigned in declaration order and move when anything above them is added)."""
    def walk(items, kind, prefix):
        for e in items or []:
            canon = f"{prefix}{kind}:{e.get('name')}"
            yield e, canon
            yield from walk(e.get("containers"), "Container", canon + "/")
            yield from walk(e.get("components"), "Component", canon + "/")
            yield from walk(e.get("children"), "DeploymentNode", canon + "/")
            yield from walk(e.get("infrastructureNodes"), "InfrastructureNode", canon + "/")
            yield from walk(e.get("softwareSystemInstances"), "SoftwareSystemInstance",
                            canon + "/")
            yield from walk(e.get("containerInstances"), "ContainerInstance", canon + "/")
    yield from walk(model.get("people"), "Person", "")
    yield from walk(model.get("softwareSystems"), "SoftwareSystem", "")
    yield from walk(model.get("customElements"), "Custom", "")
    yield from walk(model.get("deploymentNodes"), "DeploymentNode", "")


def view_signatures(ws: dict) -> dict[str, dict]:
    """`{view key: {"type", "title", "description", "sig"}}` for one parsed workspace."""
    model = ws.get("model") or {}
    canon, rels = {}, {}
    for e, c in _walk_elements(model):
        canon[e.get("id")] = (c, e)
        for r in e.get("relationships") or []:
            rels[r.get("id")] = r

    def element(eid):
        c, e = canon.get(eid, (f"?{eid}", {}))
        return [c, e.get("description") or "", e.get("technology") or "",
                sorted((e.get("tags") or "").split(",")), e.get("url") or ""]

    def relationship(rid):
        r = rels.get(rid) or {}
        return [canon.get(r.get("sourceId"), (f"?{r.get('sourceId')}",))[0],
                canon.get(r.get("destinationId"), (f"?{r.get('destinationId')}",))[0],
                r.get("description") or "", r.get("technology") or "",
                sorted((r.get("tags") or "").split(","))]

    views = ws.get("views") or {}
    shared = json.dumps({k: views.get("configuration", {}).get(k)
                         for k in ("styles", "branding", "themes", "terminology")},
                        sort_keys=True)
    out = {}
    for lst, vtype in VIEW_LISTS.items():
        for v in views.get(lst) or []:
            layout = v.get("automaticLayout")
            body = {
                "type": vtype, "title": v.get("title") or "",
                "description": v.get("description") or "",
                "scope": [canon.get(v.get(k), (None,))[0]
                          for k in ("softwareSystemId", "containerId", "elementId")
                          if v.get(k)],
                "layout": layout,
                "elements": sorted(element(x.get("id")) + ([] if layout else
                                                           [x.get("x"), x.get("y")])
                                   for x in v.get("elements") or []),
                "relationships": sorted(relationship(x.get("id"))
                                        for x in v.get("relationships") or []),
                "shared": shared,
            }
            out[v["key"]] = {"type": vtype, "title": v.get("title") or "",
                             "description": v.get("description") or "",
                             "order": len(out),
                             "sig": hashlib.sha256(json.dumps(body, sort_keys=True,
                                                              default=str).encode())
                                    .hexdigest()}
    return out


# ── which test reads the DSL, and at what level ──────────────────────────────────────

LEVEL_PROBES = {
    "Component": re.compile(r"getComponents\s*\(|instanceof\s+Component\b"),
    "Container": re.compile(r"getContainers\s*\(|instanceof\s+Container\b"),
}


def tests_reading(root: Path, entry: str) -> list[dict]:
    """Test sources that name the workspace file, and which C4 levels each one checks."""
    name = Path(entry).name
    out = git(root, "grep", "-l", "-F", name, "--", "*test*", "*Test*", "*spec*") or ""
    found = []
    for rel in sorted({p for p in out.splitlines() if p and not p.endswith(".dsl")
                       and not p.endswith(".md") and not p.startswith(".human-review/")}):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append({"path": rel,
                      "levels": [lvl for lvl, rx in LEVEL_PROBES.items() if rx.search(text)],
                      # Whether it reads the arrows at all: without it, only the boxes
                      # can be claimed as checked.
                      "arrows": bool(RELATIONSHIP_PROBE.search(text))})
    return found


RELATIONSHIP_PROBE = re.compile(r"get(?:Efferent|Afferent)?Relationships\s*\(")

LEVEL_WORDS = {"Component": "components", "Container": "containers"}


def tested_note(vtype: str, tests: list[dict]) -> str:
    """One line under the card: are these boxes compared with the code, or drawn by hand?

    The *checked* sentence is parsed back by `hrbuild/shared/c4.py:_CHECKED_NOTE`, which
    moves it into the card's header as *ArchUnit-checked by <test>*: reword it there too."""
    if not tests:
        return "Hand-maintained: no test reads this workspace."
    checking = [t for t in tests if vtype in t["levels"]]
    if checking:
        names = ", ".join(Path(t["path"]).name for t in checking)
        if any(t.get("arrows") for t in checking):
            return (f"Its {LEVEL_WORDS[vtype]} and their arrows are checked against the "
                    f"code by {names}.")
        return (f"Its {LEVEL_WORDS[vtype]} are checked against the code by {names}; "
                "its arrows are not.")
    names = ", ".join(Path(t["path"]).name for t in tests)
    levels = sorted({l for t in tests for l in t["levels"]})
    if levels:
        what = " and ".join(LEVEL_WORDS[l] for l in levels)
        return (f"Hand-maintained: {names} parses this workspace but checks only its "
                f"{what}; nothing on this view is compared with the code.")
    return f"Hand-maintained: {names} parses this workspace; nothing on it is compared " \
           "with the code."


# ── rendering ────────────────────────────────────────────────────────────────────────

EXPORT_JS = """(args) => new Promise((resolve, reject) => {
    structurizr.scripting.setDarkMode(args.dark);
    const got = {};
    structurizr.scripting.exportViews(args.keys.slice(),
        {format: 'svg', prefix: '', animation: false, metadata: false, crop: true},
        (view, diagram) => { got[view.key] = diagram.content; },
        () => resolve(got));
    setTimeout(() => reject('exportViews did not finish in 60 s'), 60000);
})"""


def render_site(browser, site: Path, keys: list[str]) -> dict[str, dict[str, str]]:
    """`{view key: {"light": svg, "dark": svg}}`, each drawn by the site's own viewer."""
    out: dict[str, dict[str, str]] = {k: {} for k in keys}
    for mode in ("light", "dark"):
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                                color_scheme=mode)
        try:
            page.goto((site / "index.html").as_uri())
            page.wait_for_function(
                "window.structurizr && structurizr.scripting"
                " && structurizr.scripting.isDiagramRendered()", timeout=30000)
            got = page.evaluate(EXPORT_JS, {"dark": mode == "dark", "keys": keys})
        finally:
            page.close()
        for key, uri in got.items():
            svg = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
            out.setdefault(key, {})[mode] = svg
    return out


def safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)


def write_verdict(out_dir: Path, **doc) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verdict.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def materialise(files: dict[str, bytes], into: Path) -> None:
    for rel, data in files.items():
        p = into / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def ensure_image() -> str | None:
    """Pull the image on its own clock: a first pull is ~430 MB, which must neither count
    against the export's timeout nor be reported as one."""
    have = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if have.returncode == 0:
        return None
    try:
        pull = subprocess.run(["docker", "pull", IMAGE], capture_output=True, text=True,
                              timeout=900)
    except subprocess.TimeoutExpired:
        return f"pulling {IMAGE} took longer than 15 min"
    return None if pull.returncode == 0 else f"could not pull {IMAGE}"


#: The title/description/date block Structurizr keeps in the SVG, hidden, when exported
#: with `metadata: false`. The date makes every render differ from the last one by bytes.
HIDDEN_TSPAN = re.compile(r'<tspan\b[^>]*display="none"[^>]*>.*?</tspan>', re.S)


def draw(root: Path, entries: list[str], sides: dict, mb: str | None, base_ref: str,
         out_dir: Path) -> tuple[list[dict], list[str], bool]:
    """Every view of every workspace, into `out_dir`. Returns (rows, problems, broken):
    `broken` when the branch's own DSL does not parse — a finding, said on the tab."""
    from playwright.sync_api import sync_playwright

    rows, problems, broken = [], [], False
    with tempfile.TemporaryDirectory(prefix="hr-c4-") as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for idx, entry in enumerate(entries):
                # Two workspaces may both have a `C1` (or Structurizr's auto keys): their
                # files are kept apart by the workspace's position, when there are two.
                pre = f"{idx}-" if len(entries) > 1 else ""
                tests = tests_reading(root, entry)
                parsed = {}
                for side in ("new", "old"):
                    files = sides[entry][side]
                    if not files:
                        continue
                    work = Path(tmp) / f"{idx}.{side}"
                    materialise(files, work)
                    site, err = export_static(work, entry)
                    if site is None:
                        where = "on this branch" if side == "new" else "at the merge-base"
                        problems.append(f"{err} ({where})")
                        broken |= side == "new"
                        continue
                    parsed[side] = (site, view_signatures(workspace_json(site)))
                if sides[entry]["new"] and "new" not in parsed:
                    continue          # the branch's DSL is broken: no picture claims otherwise
                if "new" not in parsed and "old" not in parsed:
                    continue
                new_sigs = parsed.get("new", (None, {}))[1]
                old_sigs = parsed.get("old", (None, {}))[1]
                uncompared = ""
                if mb is None:
                    uncompared = (f" No merge-base with {base_ref} was found, so this view is "
                                  "not compared with the base.")
                elif sides[entry]["old"] and "old" not in parsed:
                    uncompared = (" The merge-base's copy of the DSL did not parse, so this "
                                  "view is not compared with it.")
                if uncompared:
                    old_sigs = None
                status = {}
                for key in set(new_sigs) | set(old_sigs or {}):
                    if old_sigs is None:
                        status[key] = "uncompared"
                    elif key not in old_sigs:
                        status[key] = "added"
                    elif key not in new_sigs:
                        status[key] = "deleted"
                    else:
                        status[key] = ("unchanged" if new_sigs[key]["sig"] ==
                                       old_sigs[key]["sig"] else "modified")
                try:
                    drawn_new = render_site(browser, parsed["new"][0], list(new_sigs)) \
                        if "new" in parsed else {}
                    want_old = [k for k, s in status.items() if s in ("modified", "deleted")]
                    drawn_old = render_site(browser, parsed["old"][0], want_old) \
                        if want_old and "old" in parsed else {}
                except Exception as e:      # noqa: BLE001 - the viewer, not the model
                    problems.append(f"Structurizr's viewer could not draw {entry}: "
                                    f"{str(e).splitlines()[0][:200]}")
                    continue
                for key, st in status.items():
                    meta = new_sigs.get(key) or (old_sigs or {}).get(key)
                    row = {"name": key, "title": meta["title"],
                           "description": meta["description"], "type": meta["type"],
                           "status": st, "source": entry,
                           "note": tested_note(meta["type"], tests)
                           + (uncompared if st == "uncompared" else "")}
                    for side, drawn in (("new", drawn_new), ("old", drawn_old)):
                        for mode, svg in (drawn.get(key) or {}).items():
                            name = f"{pre}{safe(key)}.{side}.{mode}.svg"
                            (out_dir / name).write_text(HIDDEN_TSPAN.sub("", svg),
                                                        encoding="utf-8")
                            row[f"{side}_{mode}"] = name
                    row["_order"] = (TYPE_ORDER.get(meta["type"], 9), idx, meta["order"])
                    rows.append(row)
        finally:
            browser.close()
    return rows, problems, broken


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main",
                    help="the ref to compare against (its merge-base with HEAD is used)")
    ap.add_argument("--out-dir", default=".human-review/assets/c4")
    ap.add_argument("--root", default=".", help="the repository under review")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    mb = (git(root, "merge-base", "HEAD", args.base) or "").strip() or None
    head, base = Side(root, None), (Side(root, mb) if mb else None)

    entries = sorted(set(workspaces(head)) | set(workspaces(base) if base else []))
    if not entries:
        print("structurizr-views: no Structurizr DSL workspace in this repository")
        shutil.rmtree(out_dir, ignore_errors=True)
        # Said once, so a plain refresh does not keep asking (refresh-report `c4_pending`);
        # the card renders nothing for it.
        write_verdict(out_dir, state="none", reason="no Structurizr DSL workspace")
        return 3

    sides = {e: {"new": closure(head, e) if (root / e).is_file() else None,
                 "old": closure(base, e) if base and base.read(e) is not None else None}
             for e in entries}
    fp = fingerprint(*(s for e in entries for s in (sides[e]["new"], sides[e]["old"])))

    def unavailable(why: str) -> int:
        # A previous render of exactly these inputs is still the truth: keep it.
        try:
            held = json.loads((out_dir / "verdict.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = {}
        if held.get("state") == "drawn" and held.get("fingerprint") == fp:
            print(f"structurizr-views: {why}; the views on disk are of these same inputs")
            return 0
        shutil.rmtree(out_dir, ignore_errors=True)
        write_verdict(out_dir, state="unavailable", reason=why, fingerprint=fp,
                      workspaces=entries)
        print(f"structurizr-views: not drawn — {why}", file=sys.stderr)
        return 4

    why = docker_ready()
    if why is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            why = ("Python Playwright is not installed "
                   "(pip install playwright && playwright install chromium)")
    why = why or ensure_image()
    if why:
        return unavailable(why)

    # Drawn beside the old output and swapped in at the end: a run that dies half-way
    # (no Chromium, a viewer that hangs) leaves the previous views, not an empty folder.
    stage = out_dir.with_name(out_dir.name + ".drawing")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        rows, problems, broken = draw(root, entries, sides, mb, args.base, stage)
    except Exception as e:              # noqa: BLE001 - Chromium missing, or similar
        shutil.rmtree(stage, ignore_errors=True)
        return unavailable("the headless browser could not start ("
                           + (str(e).splitlines() or ["?"])[0][:200] + ")")

    rows.sort(key=lambda r: r["_order"])
    clean = lambda v: str(v or "").replace("\t", " ").replace("\n", " ")
    (stage / "MANIFEST.tsv").write_text(
        "\t".join(COLUMNS) + "\n"
        + "".join("\t".join(clean(r.get(c)) for c in COLUMNS) + "\n" for r in rows),
        encoding="utf-8")
    state = "drawn" if rows and not broken else "failed"
    write_verdict(stage, state=state, reason="; ".join(problems), fingerprint=fp,
                  workspaces=entries, image=IMAGE, base=mb,
                  views={r["name"]: r["status"] for r in rows})
    shutil.rmtree(out_dir, ignore_errors=True)
    stage.rename(out_dir)
    for p in problems:
        print(f"structurizr-views: {p}", file=sys.stderr)
    print(f"structurizr-views: {len(rows)} view(s) drawn from {', '.join(entries)} "
          f"({', '.join(f'{r['name']}={r['status']}' for r in rows)})")
    # A DSL that does not parse is the branch's finding, written where the tab reads it;
    # nothing drawn at all is this step failing.
    return 0 if rows or broken else 1


if __name__ == "__main__":
    raise SystemExit(main())
