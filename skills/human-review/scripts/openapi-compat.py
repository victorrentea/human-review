#!/usr/bin/env python3
"""Is the new REST contract backward compatible? — answered by a tool, not by us.

`openapi-diff.py`, next door, is *our* reading of the spec: it classifies every
difference and it is the thing a human actually reads. This script asks the same
question of **OpenAPITools/openapi-diff** — the reference implementation, a Java
library with its own rule set, used in other people's CI — and puts its verdict
at the top of the contract tab:

    no_changes  ·  compatible  ·  incompatible

Two differs, one contract. That is the point: a verdict nobody can accuse of
being the same code that produced the change, and a **cross-check** that says out
loud when the two disagree. A disagreement is not noise — it is the single most
review-worthy line on the page, because exactly one of the two is wrong about
whether somebody's client breaks.

The tool ships as a fat jar on Maven Central. This script resolves it in order:
`--jar`, `$OPENAPI_DIFF_JAR`, the cache under `~/.cache/human-review/`, then a
download (sha1-verified against Maven's own checksum), then `--docker`. Nothing
to install by hand, and nothing that silently runs a jar it did not verify.

Emits an HTML fragment for `build-review-html.py` to `includeHtml`, in the same
shape as its siblings; `--css` prints the stylesheet it needs.

Usage (from the repository root — the project is resolved from the CWD):
    openapi-compat.py --base origin/main --out .human-review/assets/openapi-compat.html
    openapi-compat.py --css > .human-review/assets/openapi-compat.css
    openapi-compat.py before.yaml after.yaml --state
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema_tree  # noqa: E402 - resolved from next to this file, not from the project

try:
    import yaml
except ImportError:  # pragma: no cover - the message is the whole handling
    raise SystemExit("[openapi-compat] needs PyYAML: python3 -m pip install pyyaml")

VERSION = "2.1.7"
MAVEN = (
    "https://repo1.maven.org/maven2/org/openapitools/openapidiff/openapi-diff-cli/"
    f"{VERSION}/openapi-diff-cli-{VERSION}-all.jar"
)
DOCKER_IMAGE = f"openapitools/openapi-diff:{VERSION}"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "human-review"

NO_CHANGES, COMPATIBLE, INCOMPATIBLE, NOT_RUN = (
    "no_changes", "compatible", "incompatible", "not_run",
)
VERDICT_HEADLINE = {
    NO_CHANGES: "The contract did not move",
    COMPATIBLE: "Backward compatible",
    INCOMPATIBLE: "Not backward compatible",
    NOT_RUN: "The compatibility check did not run",
}

# Diff nodes reachable twice: `changedElements` is the same children the named keys
# already carry, so walking both turns one finding into thirty-two.
SKIP_KEYS = {"oldOperation", "newOperation", "oldSchema", "newSchema", "context",
             "changedElements", "oldValue", "newValue"}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def repo_root() -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"], check=True).stdout.strip())


# ── getting hold of the tool ──────────────────────────────────────────────────────
def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=180) as r:  # noqa: S310 - pinned host
        return r.read()


def resolve_jar(explicit: str | None) -> Path:
    """The jar, downloaded once into the cache and verified against Maven's sha1."""
    for candidate in (explicit, os.environ.get("OPENAPI_DIFF_JAR")):
        if candidate:
            p = Path(candidate).expanduser()
            if not p.is_file():
                raise SystemExit(f"[openapi-compat] no jar at {p}")
            return p

    cached = CACHE / f"openapi-diff-cli-{VERSION}-all.jar"
    if cached.is_file():
        return cached

    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"[openapi-compat] fetching openapi-diff {VERSION} into {CACHE}", file=sys.stderr)
    blob = fetch(MAVEN)
    want = fetch(MAVEN + ".sha1").decode().split()[0].strip()
    got = hashlib.sha1(blob).hexdigest()  # noqa: S324 - Maven publishes sha1, not our choice
    if got != want:
        raise SystemExit(f"[openapi-compat] checksum mismatch: {got} != {want}")
    tmp = cached.with_suffix(".part")
    tmp.write_bytes(blob)
    tmp.replace(cached)
    return cached


def invoke(before: Path, after: Path, outputs: dict, use_docker: bool, jar: str | None):
    """Run the tool once. `outputs` maps a CLI flag to the file it should write."""
    args = []
    for flag, path in outputs.items():
        args += [f"--{flag}", str(path)]

    if use_docker or (not shutil.which("java") and shutil.which("docker")):
        if not shutil.which("docker"):
            raise SystemExit("[openapi-compat] neither java nor docker is available")
        # Everything the container touches has to live under one mounted directory.
        mount = before.parent
        args = [a if not a.startswith("/") else f"/specs/{Path(a).name}" for a in args]
        cmd = ["docker", "run", "--rm", "-v", f"{mount}:/specs", DOCKER_IMAGE,
               f"/specs/{before.name}", f"/specs/{after.name}", *args, "--off"]
    else:
        if not shutil.which("java"):
            raise SystemExit("[openapi-compat] java is not on PATH — pass --docker")
        cmd = ["java", "-jar", str(resolve_jar(jar)), str(before), str(after), *args, "--off"]

    proc = run(cmd)
    # The tool exits non-zero only under --fail-on-*, which we never pass; anything
    # else here is a real failure and its stderr is the only useful thing to show.
    if proc.returncode != 0 and not all(Path(p).exists() for p in outputs.values()):
        raise SystemExit(f"[openapi-compat] {' '.join(cmd[:3])} failed:\n{proc.stderr.strip()}")
    return proc


# ── reading the verdict out of the JSON ───────────────────────────────────────────
def is_diff_node(n) -> bool:
    return isinstance(n, dict) and "incompatible" in n


def children(node: dict):
    for key, value in node.items():
        if key in SKIP_KEYS:
            continue
        if is_diff_node(value):
            yield key, value
        elif isinstance(value, list):
            for item in value:
                if is_diff_node(item):
                    yield key, item
        elif isinstance(value, dict):
            for sub, item in value.items():
                if is_diff_node(item):
                    yield f"{key}[{sub}]", item


def names(value):
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, list):
        return [v if isinstance(v, str) else str(v) for v in value]
    return []


def describe(crumbs: list, node: dict) -> str:
    """One sentence for one incompatible leaf, in the currency a caller thinks in."""
    kind = crumbs[-1] if crumbs else ""
    increased, missing = names(node.get("increased")), names(node.get("missing"))
    gone = names(node.get("missingProperties"))
    if gone:
        return "removes " + ", ".join(f"<code>{html.escape(p)}</code>" for p in gone)
    if kind.startswith("required") and increased:
        return "now requires " + ", ".join(f"<code>{html.escape(p)}</code>" for p in increased)
    if missing:
        return "drops " + ", ".join(f"<code>{html.escape(p)}</code>" for p in missing)
    old, new = node.get("oldValue"), node.get("newValue")
    if old is not None or new is not None:
        left = f"<b class=oac-del>{html.escape(str(old))}</b>" if old is not None else "<i>unset</i>"
        right = f"<b class=oac-add>{html.escape(str(new))}</b>" if new is not None else "<i>unset</i>"
        return f"<code>{html.escape(kind)}</code> {left} → {right}"
    return f"<code>{html.escape(kind)}</code> changed incompatibly"


def leaves(node: dict, crumbs: list, out: list):
    """The deepest incompatible nodes — a parent is only incompatible because of them."""
    inner = [(k, v) for k, v in children(node) if v.get("incompatible")]
    if not inner:
        out.append((crumbs, describe(crumbs, node)))
        return
    for key, child in inner:
        leaves(child, crumbs + [key], out)


def read_report(report: dict) -> dict:
    """Everything the fragment needs, flattened out of the tool's recursive JSON."""
    breaks, additive = [], []
    for e in report.get("missingEndpoints") or []:
        breaks.append({
            "method": e.get("method", ""), "path": e.get("pathUrl", ""),
            "reasons": [([], "the operation is gone")],
        })
    for e in report.get("newEndpoints") or []:
        additive.append({"method": e.get("method", ""), "path": e.get("pathUrl", ""),
                         "note": "new operation"})
    for op in report.get("changedOperations") or []:
        subject = {"method": op.get("httpMethod", ""), "path": op.get("pathUrl", "")}
        if op.get("incompatible"):
            found = []
            leaves(op, [], found)
            # Same reason reachable by two named paths (requestBody and its content)
            # is still one reason.
            seen, unique = set(), []
            for crumbs, text in found:
                if text in seen:
                    continue
                seen.add(text)
                unique.append((crumbs, text))
            breaks.append({**subject, "reasons": unique})
        elif op.get("different"):
            additive.append({**subject, "note": "changed, compatibly"})
    deprecated = [{"method": e.get("method", ""), "path": e.get("pathUrl", "")}
                  for e in report.get("deprecatedEndpoints") or []]

    if report.get("unchanged"):
        state = NO_CHANGES
    elif report.get("incompatible"):
        state = INCOMPATIBLE
    else:
        state = COMPATIBLE
    return {"state": state, "breaks": breaks, "additive": additive, "deprecated": deprecated}


# ── the second opinion: what our own classifier said ──────────────────────────────
def our_verdict(argv: list) -> dict | None:
    """`openapi-diff.py`'s own answer, so the page can say when the two disagree."""
    sibling = Path(__file__).with_name("openapi-diff.py")
    if not sibling.is_file():
        return None
    proc = run([sys.executable, str(sibling), *argv, "--json"])
    if proc.returncode != 0:
        print(f"[openapi-compat] cross-check skipped: {proc.stderr.strip()}", file=sys.stderr)
        return None
    try:
        subjects = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    breaking = [f"{s['name']} — {c['text']}"
                for s in subjects for c in s.get("changes", []) if c["level"] == "breaking"]
    breaking += [s["name"] for s in subjects if s["status"] == "removed" and not s.get("changes")]
    return {"breaking": breaking, "subjects": len(subjects)}


# ── rendering ─────────────────────────────────────────────────────────────────────
VERB_CLASS = {"GET": "get", "POST": "post", "PUT": "put", "PATCH": "put", "DELETE": "delete"}


def op_label(method: str, path: str) -> str:
    verb = VERB_CLASS.get(method.upper(), "get")
    return (f'<span class="oac-verb oac-{verb}">{html.escape(method.upper())}</span>'
            f'<code class="oac-path">{html.escape(path)}</code>')


def render(result: dict, ours: dict | None, provenance: str, changelog: str,
           before: dict | None = None, after: dict | None = None) -> str:
    state = result["state"]
    breaks, additive = result["breaks"], result["additive"]
    n_reasons = sum(len(b["reasons"]) for b in breaks)

    if state == INCOMPATIBLE:
        sub = (f"{n_reasons} change{'s' if n_reasons != 1 else ''} across "
               f"{len(breaks)} operation{'s' if len(breaks) != 1 else ''} would break a client "
               "that is already calling this API.")
    elif state == COMPATIBLE:
        sub = (f"{len(additive)} operation{'s' if len(additive) != 1 else ''} moved, and none of "
               "it can break a client compiled against the base spec.")
    else:
        sub = "The two specs are structurally identical."

    parts = [
        f'<div class="oac oac-{state}">',
        '<div class="oac-verdict">'
        f'<span class="oac-seal">{html.escape(state.replace("_", " ")).upper()}</span>'
        f'<div><div class="oac-headline">{VERDICT_HEADLINE[state]}</div>'
        f'<div class="oac-sub">{sub}</div></div></div>',
        f'<p class="oac-prov">{provenance}</p>',
    ]

    def tree(method: str, path: str) -> str:
        # The verdict says whether it breaks; the tree says what it now looks like.
        if before is None or after is None:
            return ""
        return schema_tree.operation_tree(method, path, before, after)

    if breaks:
        parts.append('<div class="oac-kind">What breaks '
                     f'<span class="oac-count">{n_reasons}</span></div>')
        for b in breaks:
            items = "".join(f"<li>{text}</li>" for _, text in b["reasons"])
            parts.append(f'<div class="oac-row oac-bad"><div class="oac-head">'
                         f'{op_label(b["method"], b["path"])}</div>'
                         f'<ul class="oac-reasons">{items}</ul>'
                         f'{tree(b["method"], b["path"])}</div>')

    if result["deprecated"]:
        rows = "".join(f'<div class="oac-row oac-warn"><div class="oac-head">'
                       f'{op_label(d["method"], d["path"])}'
                       '<span class="oac-note">deprecated</span></div></div>'
                       for d in result["deprecated"])
        parts.append(f'<div class="oac-kind">Newly deprecated</div>{rows}')

    if additive:
        rows = "".join(f'<div class="oac-row"><div class="oac-head">'
                       f'{op_label(a["method"], a["path"])}'
                       f'<span class="oac-note">{html.escape(a["note"])}</span></div>'
                       f'{tree(a["method"], a["path"])}</div>'
                       for a in additive)
        parts.append('<div class="oac-kind">Compatible movement '
                     f'<span class="oac-count">{len(additive)}</span></div>{rows}')

    parts.append(cross_check(state, ours))
    if changelog:
        parts.append('<details class="oac-log"><summary>The full changelog, as the tool '
                     "wrote it</summary>" + changelog + "</details>")
    parts.append("</div>")
    return "\n".join(p for p in parts if p)


def cross_check(state: str, ours: dict | None) -> str:
    if ours is None:
        return ""
    we_break, they_break = bool(ours["breaking"]), state == INCOMPATIBLE
    if we_break == they_break:
        verdict = "breaking" if we_break else "safe"
        return ('<div class="oac-agree"><b>Both differs agree.</b> '
                f'<code>openapi-diff.py</code> read {ours["subjects"]} changed subject'
                f'{"s" if ours["subjects"] != 1 else ""} out of the same two revisions and also '
                f'calls this <b>{verdict}</b>. The verdict is not one tool\'s opinion.</div>')
    listed = "".join(f"<li>{html.escape(x)}</li>" for x in ours["breaking"][:8]) or "<li>—</li>"
    if we_break and not they_break:
        body = ("our own classifier flagged something as breaking that the reference "
                "implementation considers compatible. Either we are over-strict, or the tool's "
                "rule set does not cover this shape — read the lines below and decide, because "
                "one of the two is wrong about somebody's client:")
    else:
        body = ("the reference implementation found an incompatibility our classifier missed. "
                "Trust the tool here and treat the section above as the real list; "
                "<code>openapi-diff.py</code> has a gap worth fixing. It flagged:")
    return (f'<div class="oac-disagree"><b>The two differs disagree</b> — {body}'
            f'<ul class="oac-reasons">{listed}</ul></div>')


# ── the tool's markdown, as HTML we control ───────────────────────────────────────
def inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    return out


def markdown_to_html(md: str) -> str:
    """openapi-diff's own changelog dialect: `#` headings, `*` bullets, `>` doc quotes."""
    out, depth = [], 0

    def close(to: int):
        nonlocal depth
        while depth > to:
            out.append("</ul>")
            depth -= 1

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip() or set(line.strip()) == {"-"}:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            close(0)
            level = len(heading.group(1))
            body = heading.group(2)
            op = re.match(r"^`(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)`\s+(\S+)", body)
            if op:
                out.append(f'<div class="oac-log-op">{op_label(op.group(1), op.group(2))}</div>')
            else:
                out.append(f'<div class="oac-log-h oac-log-h{level}">{inline(body)}</div>')
            continue
        bullet = re.match(r"^(\s*)\*\s+(.*)$", line)
        if bullet:
            want = len(bullet.group(1)) // 4 + 1
            while depth < want:
                out.append('<ul class="oac-log-list">')
                depth += 1
            close(want)
            out.append(f"<li>{inline(bullet.group(2))}</li>")
            continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            doc = f'<div class="oac-log-doc">{inline(quote.group(1))}</div>'
            # A doc line describes the bullet above it, so it belongs *inside* that
            # <li> — a <div> loose in a <ul> is invalid and renders unindented.
            if depth and out and out[-1].endswith("</li>"):
                out[-1] = out[-1][: -len("</li>")] + doc + "</li>"
            else:
                out.append(doc)
            continue
        close(0)
        out.append(f'<p class="oac-log-p">{inline(line.strip())}</p>')
    close(0)
    return "\n".join(out)


CSS = """
.oac { --oac-bad:#c62828; --oac-ok:#2e7d32; --oac-warn:#b56b00; --oac-flat:#6b6b78;
       margin:.4rem 0 1rem; }
.oac-verdict { display:flex; align-items:center; gap:.9rem; border:1px solid var(--line);
               border-left:4px solid var(--oac-flat); border-radius:10px; padding:.8rem 1rem;
               background:var(--card); }
.oac-incompatible .oac-verdict { border-left-color:var(--oac-bad); }
.oac-compatible   .oac-verdict { border-left-color:var(--oac-ok); }
.oac-seal { font:800 .7rem/1.9 inherit; letter-spacing:.08em; border-radius:5px;
            padding:.1rem .55rem; white-space:nowrap; background:#f0f0f4; color:#5d5d6b; }
.oac-incompatible .oac-seal { background:#fdeaea; color:#8a1c1c; }
.oac-compatible   .oac-seal { background:#eef7ef; color:#245c30; }
.oac-headline { font-weight:700; font-size:1rem; }
.oac-sub { color:var(--muted); font-size:.86rem; line-height:1.6; }
.oac-prov { color:var(--muted); font-size:.8rem; line-height:1.7; margin:.5rem 0 1rem; }
.oac-kind { font:600 .82rem/1.6 inherit; text-transform:uppercase; letter-spacing:.06em;
            color:var(--muted); border-bottom:1px solid var(--line); padding-bottom:.3rem;
            margin-top:1.2rem; }
.oac-count { background:var(--code-bg); border-radius:999px; padding:0 .4rem; margin-left:.3rem;
             font-size:.72rem; }
.oac-row { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--line);
           border-radius:8px; padding:.55rem .8rem; margin:.5rem 0; }
.oac-row.oac-bad  { border-left-color:var(--oac-bad); }
.oac-row.oac-warn { border-left-color:var(--oac-warn); }
.oac-head { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
.oac-verb { font:700 10.5px/1.7 ui-monospace,Menlo,monospace; border-radius:4px; padding:0 .38rem;
            color:#fff; letter-spacing:.04em; }
.oac-get { background:#2f6fb5; } .oac-post { background:#2e7d32; }
.oac-put { background:#b56b00; } .oac-delete { background:#c62828; }
.oac-path { font:600 12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg); }
.oac-note { color:var(--muted); font-size:.8rem; }
.oac-reasons { list-style:none; margin:.45rem 0 0; padding:0; display:grid; gap:.28rem; }
.oac-reasons li { font-size:.88rem; line-height:1.6; }
.oac-reasons li::before { content:"↳"; color:var(--muted); margin-right:.45rem; }
.oac code { font:600 12px/1.5 ui-monospace,Menlo,monospace; background:var(--code-bg);
            border-radius:3px; padding:0 .25rem; }
.oac b.oac-add { color:#2e7d32; } .oac b.oac-del { color:#c62828; }
.oac-agree, .oac-disagree { margin:1.2rem 0 0; border-radius:8px; padding:.65rem .85rem;
                            font-size:.86rem; line-height:1.7; }
.oac-agree { background:var(--code-bg); color:var(--muted); }
.oac-agree b { color:var(--fg); }
.oac-disagree { background:#fdf3e2; color:#6b4a0f; border:1px solid #e5c98f; }
.oac-log { margin:1.4rem 0 0; }
.oac-log summary { cursor:pointer; color:var(--muted); font-size:.85rem; }
.oac-log-op { margin:.9rem 0 .2rem; display:flex; align-items:center; gap:.5rem; }
.oac-log-h { color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.05em;
             margin:.8rem 0 .2rem; }
.oac-log-h3 { font-size:.95rem; text-transform:none; letter-spacing:0; color:var(--fg);
              font-weight:700; }
.oac-log-list { margin:.2rem 0 .2rem 1.1rem; padding:0; }
.oac-log-list li { font-size:.86rem; line-height:1.6; }
.oac-log-doc { color:var(--muted); font-size:.8rem; border-left:2px solid var(--line);
               padding-left:.55rem; margin:.15rem 0 .3rem; }
.oac-log-p { font-size:.86rem; margin:.3rem 0; }
@media (prefers-color-scheme: dark) {
  .oac-seal { background:#26262f; color:#a5a5b4; }
  .oac-incompatible .oac-seal { background:#3a1f1f; color:#f2a0a0; }
  .oac-compatible   .oac-seal { background:#1b2c1f; color:#9ad3a5; }
  .oac b.oac-add { color:#8fd39c; } .oac b.oac-del { color:#f08a8a; }
  .oac-disagree { background:#3a3018; color:#e6c07b; border-color:#6b5520; }
}
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("before", nargs="?", help="spec to compare from (default: --base's copy)")
    ap.add_argument("after", nargs="?", help="spec to compare to (default: the working tree)")
    ap.add_argument("--base", default="origin/main", help="git ref to read the base spec from")
    ap.add_argument("--spec", default="openapi.yaml", help="repo-relative path to the spec")
    ap.add_argument("--out", help="write the HTML fragment here instead of stdout")
    ap.add_argument("--json", action="store_true", help="emit the flattened verdict as JSON")
    ap.add_argument("--state", action="store_true",
                    help="print only no_changes / compatible / incompatible")
    ap.add_argument("--css", action="store_true", help="print the stylesheet this fragment needs")
    ap.add_argument("--jar", help="path to openapi-diff-cli-*-all.jar (default: fetch & cache)")
    ap.add_argument("--docker", action="store_true", help=f"run {DOCKER_IMAGE} instead of java")
    ap.add_argument("--no-cross-check", action="store_true",
                    help="skip comparing the verdict against openapi-diff.py")
    args = ap.parse_args(argv)

    if args.css:
        print(CSS)
        print(schema_tree.CSS)
        return 0

    sibling_args, spec_rel = [], args.spec
    with tempfile.TemporaryDirectory(prefix="openapi-compat-") as tmp:
        tmpdir = Path(tmp)
        if args.before and args.after:
            before, after = Path(args.before).resolve(), Path(args.after).resolve()
            spec_rel = args.after
            sibling_args = [args.before, args.after]
            provenance = (f"<code>{html.escape(before.name)}</code> → "
                          f"<code>{html.escape(after.name)}</code>")
        else:
            root = repo_root()
            target = root / spec_rel
            if not target.is_file():
                raise SystemExit(f"[openapi-compat] no spec at {target} — pass --spec")
            merge_base = run(["git", "merge-base", args.base, "HEAD"], cwd=root)
            base_ref = merge_base.stdout.strip() if merge_base.returncode == 0 else args.base
            base_spec = run(["git", "show", f"{base_ref}:{spec_rel}"], cwd=root)
            sibling_args = ["--base", args.base, "--spec", args.spec]
            provenance = (
                f"<code>{html.escape(spec_rel)}</code> at the merge-base "
                f"<code>{html.escape(base_ref[:8])}</code> against the working tree, read by "
                f"<b>OpenAPITools/openapi-diff {VERSION}</b> — the reference implementation, "
                "not ours."
            )
            if base_spec.returncode != 0 or not base_spec.stdout.strip():
                # No spec at the base means no client compiled against one. Nothing to break.
                frag = render({"state": NO_CHANGES, "breaks": [], "additive": [],
                               "deprecated": []}, None,
                              provenance + " The spec did not exist at the merge-base, so there is "
                              "no prior contract to break.", "")
                return emit(args, frag, {"state": NO_CHANGES})
            before = tmpdir / "before.yaml"
            before.write_text(base_spec.stdout, encoding="utf-8")
            after = tmpdir / "after.yaml"
            after.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

        # Docker can only see one mounted directory, so both specs have to sit in it.
        if args.docker and before.parent != tmpdir:
            for src, name in ((before, "before.yaml"), (after, "after.yaml")):
                shutil.copyfile(src, tmpdir / name)
            before, after = tmpdir / "before.yaml", tmpdir / "after.yaml"

        report_json, changelog_md = tmpdir / "report.json", tmpdir / "report.md"
        invoke(before, after, {"json": report_json, "markdown": changelog_md},
               args.docker, args.jar)
        report = json.loads(report_json.read_text(encoding="utf-8"))
        changelog = (markdown_to_html(changelog_md.read_text(encoding="utf-8"))
                     if changelog_md.exists() else "")
        # Read inside the block: the temp copies are gone the moment it closes.
        before_spec = yaml.safe_load(before.read_text(encoding="utf-8")) or {}
        after_spec = yaml.safe_load(after.read_text(encoding="utf-8")) or {}

    result = read_report(report)
    if args.state:
        print(result["state"])
        return 0

    ours = None if args.no_cross_check else our_verdict(sibling_args)
    frag = render(result, ours, provenance, changelog, before_spec, after_spec)
    return emit(args, frag, result)


def emit(args, fragment: str, result: dict) -> int:
    if args.json:
        payload = json.dumps({k: v for k, v in result.items() if k != "breaks"} |
                             {"breaks": [{"method": b["method"], "path": b["path"],
                                          "reasons": [re.sub("<[^>]+>", "", t)
                                                      for _, t in b["reasons"]]}
                                         for b in result.get("breaks", [])]}, indent=1)
        print(payload)
    elif args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(fragment, encoding="utf-8")
        print(f"[openapi-compat] wrote {out} — {result['state']}", file=sys.stderr)
    else:
        print(fragment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
