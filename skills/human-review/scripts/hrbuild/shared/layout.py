"""The tab layout and the scope bar are the skill's, not the content file's.

Every tab after Review shows something a script produced — the film, the REST contract,
the diagram deltas, the matrix, the city, the audit, the bars, the logging scan, the owners
check. Which blocks those tabs carry, and what the script-produced sections include and
embed, is therefore a fact about the skill, the same on every branch. It used to be
re-typed into `content.json` by whichever model ran the review, off an example in
`reference/content-schema.md`, and a model that improvised produced a different page:

- an API tab of four sections — its own prose and snippet on top, then the text diff, the
  compatibility table and the pb33f report — instead of the verdict band over the visual
  diff;
- a Data tab with a section of its own (*"Page first, associations second"*, prose and two
  hand-picked snippets) between the generated diagrams and the conceptual model;
- a Demo tab declared with no blocks, which the build drops as empty — so the one tab whose
  absence most needs explaining (no film) vanished instead of saying why;
- a scope chip `gate green: CI for 7f71…`, true of every page that exists at all, since a
  red gate means there is no page.

So the build normalises all of it before anything renders, and says on stderr what it threw
away. The content file still names the tabs (and so still carries the Review tab's ledes,
each tab's hover, and the few per-branch selections a script cannot make — which diagrams;
not which tests to quote beside their sequences, which the build reads off the diagrams and
the tags), but a script-owned tab keeps only blocks of
the kinds its scripts produce, its sections are rebuilt from `SECTIONS`, and a chip that is
not computed does not reach the bar.
"""
from __future__ import annotations

import copy
import re

from .diagrams import select_rows

#: The canonical script-produced sections, by id. What a model writes into one of these is
#: replaced, except the keys in `MODEL_KEYS` — the few per-run inputs no script derives.
LAYOUT_SECTIONS: dict[str, dict] = {
    "video": {"id": "video", "title": "", "video": "assets/feature.webm"},
    "swaggerdiff": {
        "id": "swaggerdiff", "title": "",
        "includeHtml": "assets/openapi-verdict.html",
        "embed": {"src": "assets/openapi-visual-diff.html#only-touched", "class": "oavhost",
                  "label": "openapi-visual-diff — the REST contract at the base against "
                           "the working tree",
                  "missing": "run scripts/openapi-visual-diff.py (needs `brew install oasdiff`)"},
    },
    "conceptual": {"id": "conceptual", "title": "", "body": "{{drawio:conceptual}}"},
    "deployment": {"id": "deployment", "title": "", "body": "{{drawio:deployment}}"},
    "requirements-map": {"id": "requirements-map", "title": "",
                         "includeHtml": "assets/requirements-map.html", "includeFirst": True},
    "ds-audit": {"id": "ds-audit", "title": "", "includeHtml": "assets/ds-audit.html"},
    "complexity-delta": {"id": "complexity-delta", "title": "",
                         "includeHtml": "assets/complexity-delta.html"},
}

#: Keys of a canonical section the content file may still set: where the film landed (the
#: project's `steps.video.out`), and the environment row and app links under it.
LAYOUT_MODEL_KEYS = {"video": ("video", "appLinks", "runtime")}

#: Which step produces each canonical include — named in the notice that replaces a
#: fragment the run did not write, so the reader knows what to re-run.
LAYOUT_PRODUCER = {"swaggerdiff": "api", "requirements-map": "rerun-model.py (the Tests matrix)",
            "ds-audit": "dsaudit", "complexity-delta": "complexity"}

#: What the strip prints for a few tabs, whatever label the content file declared: the row
#: fits on one line only with the short forms. Only the button's text — the tab's id, its
#: panel heading, the aria-label and the lede check keep the declared label.
SHORT_TAB_LABELS = {"city": "City", "logging": "Logs", "owners": "Codeowners"}

#: Script-owned tabs, in the default strip order. `label` is used only for a tab the build
#: adds itself — a declared tab keeps its own, since a label is free and an id is not.
#: `blocks` are the non-section block types
#: the tab may carry (each a script's output, or a selection among them); `sections` the
#: canonical sections it carries, `required` the ones added when missing, and `first`
#: whether the sections lead the panel (the matrix opens Tests; the conceptual model closes
#: Data).
LAYOUT_TABS: dict[str, dict] = {
    "behaviour":    {"label": "Demo", "blocks": (), "sections": ("video",),
                     "required": ("video",)},
    "api":          {"label": "API", "blocks": (), "sections": ("swaggerdiff",),
                     "required": ("swaggerdiff",)},
    "data":         {"label": "Data", "blocks": ("diagrams", "puml"),
                     "sections": ("conceptual",), "required": ()},
    "requirements": {"label": "Tests", "blocks": ("tests", "traces"),
                     "sections": ("requirements-map",), "required": (), "first": True},
    "sequence":     {"label": "Sequence", "blocks": ("testpairs", "diagrams", "puml"),
                     "sections": (), "required": ()},
    "packages":     {"label": "Structure", "blocks": ("diagrams", "puml", "c4"),
                     "sections": ("deployment",), "required": ()},
    "city":         {"label": "City", "blocks": ("codecity",), "sections": (),
                     "required": ()},
    "complexity":   {"label": "Complexity", "blocks": (), "sections": ("complexity-delta",),
                     "required": ("complexity-delta",)},
    "dsaudit":      {"label": "UX", "blocks": (), "sections": ("ds-audit",),
                     "required": ("ds-audit",)},
    "logging":      {"label": "Logs", "blocks": ("logging",), "sections": (),
                     "required": ()},
    "owners":       {"label": "Codeowners", "blocks": ("codeowners",), "sections": (),
                     "required": ()},
}

#: A canonical section that is on its tab whenever the step that feeds it wrote this file,
#: declared or not. The hand-drawn deployment diagram stands right above the container
#: view it is checked against (Victor, 20 Oct 2026: it used to close the tab, under it),
#: only when the project configured one
#: (`steps.c2.drawio`) — a model writing the content file cannot know that it did.
LAYOUT_WHEN_WRITTEN = {"deployment": "assets/deployment-diff.json"}

def _deployment_above_containers(blocks: list) -> None:
    """The deployment section, moved in front of the C2 container view's own block (the
    one reading `assets/c2/MANIFEST.tsv`) when the tab has both."""
    dep = next((i for i, b in enumerate(blocks) if b.get("type", "section") == "section"
                and b.get("id") == "deployment"), None)
    c2 = next((i for i, b in enumerate(blocks) if b.get("type") == "diagrams"
               and "/c2/" in f'/{b.get("manifest") or ""}'), None)
    if dep is not None and c2 is not None and dep > c2:
        blocks.insert(c2, blocks.pop(dep))


#: A block that is on its tab whenever the step that feeds it wrote this file, declared or
#: not, after everything else the tab carries: the repository's own C4 views, drawn by
#: Structurizr (`structurizr-views.py`), close the Structure tab. A model writing the
#: content file cannot know the repository keeps a `.dsl`; the step does. Its verdict is
#: written whether or not Docker was there, so a run that could not draw says so in place.
LAYOUT_BLOCK_WHEN_WRITTEN = {"packages": ({"type": "c4"}, "assets/c4/verdict.json")}

#: The tab that is on the page whenever its step ran, declared or not: no film is the case
#: that most needs saying, and an absent pill says nothing.
LAYOUT_ALWAYS = "behaviour"

#: The Sequence block's shape, whatever the content file wrote. The tests it quotes are
#: derived (`hrbuild/tabs/sequence.py` `derived_snippets`): eval run 5 typed them in by hand
#: and copied another branch's line ranges. Its heading and its leftover group are the
#: skill's words too — run 5 also wrote `"title": ""`, and the tab lost the heading the
#: reference opens on. "…and no diagram came back" (until 20 Oct 2026) said neither what
#: was missing nor what to do; each file under it now says why (`undrawn_reason`), and the
#: body says how to draw them.
LAYOUT_TESTPAIRS = {"snippets": {"auto": "genseq"},
                    "unpaired": {"id": "tests-nosequence",
                                 "title": "Tagged for tracing, but no diagram yet",
                                 "body": "Draw them with ↻ on this tab, or "
                                         "<code>refresh-report.py --steps sequence</code>."}}


def _squash(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _own_testpairs(block: dict, tid: str, warnings: list[str]) -> None:
    """Snippets derived, heading and leftover group the skill's — said on stderr."""
    typed = [k for k in ("snippets", "title", "body") if k in block
             and block[k] != LAYOUT_TESTPAIRS.get(k)]
    if typed:
        warnings.append(f"tab {tid!r}: the testpairs block's {', '.join(typed)} dropped — the "
                        "tests beside the sequences are read off the diagrams and the tags, "
                        "and the heading is the skill's")
    for k in ("title", "body"):
        block.pop(k, None)
    block.update(copy.deepcopy(LAYOUT_TESTPAIRS))


def _own_diagram_title(block: dict, tid: str, warnings: list[str]) -> None:
    """A heading that only repeats the card under it goes.

    Run 5's Structure tab: a bare `<h2>C2 Containers</h2>` straight above the card whose
    own title is `C2-Containers` — the reference example in `content-schema.md` carried the
    title, and the model copied it. The card names itself; a heading that says the same
    words is the name twice."""
    title = block.get("title")
    if not title:
        return
    names = [*(block.get("only") or []), block.get("name") or "",
             (block.get("context") or {}).get("name") or ""]
    if any(n and _squash(n) == _squash(title) for n in names):
        block.pop("title")
        warnings.append(f"tab {tid!r}: heading {title!r} dropped — it repeats the name on "
                        "the diagram card under it")


def _video_step_ran(out_dir) -> bool:
    """The film, the recorder's verdict beside it, or the ledger's mark that the step ran."""
    return any((out_dir / p).exists() for p in
               ("assets/feature.webm", "assets/feature.verdict.json", ".step-video"))


def _layout_section(sid: str, model: dict | None, out_dir) -> dict:
    sec = copy.deepcopy(LAYOUT_SECTIONS[sid])
    for k in LAYOUT_MODEL_KEYS.get(sid, ()):
        if model and model.get(k):
            sec[k] = model[k]
    inc = sec.get("includeHtml")
    if inc and not (out_dir / inc).is_file():
        # A canonical fragment the run did not write is a step that did not run, said in
        # the panel — not a build that refuses, and not a tab that silently disappears.
        del sec["includeHtml"]
        sec["body"] = (f'<p class="sub">Not produced — <code>{inc}</code> is missing; '
                       f'run the <code>{LAYOUT_PRODUCER.get(sid, sid)}</code> step.</p>')
    return sec


def _layout_overridden(model: dict, sid: str) -> list[str]:
    """The keys of a model-written canonical section the build is about to replace."""
    canon, keep = LAYOUT_SECTIONS[sid], set(LAYOUT_MODEL_KEYS.get(sid, ())) | {"id"}
    return sorted(k for k in set(model) | set(canon)
                  if k not in keep and model.get(k) != canon.get(k)
                  and (model.get(k) or canon.get(k)))


def own_layout(spec: dict, out_dir) -> list[str]:
    """Normalise `spec["tabs"]`, the script-owned `sections` and `scope` in place.

    Returns one warning per thing the content file asked for and did not get. Tabs this
    module does not know (`review`, or any tab a test invents) pass through untouched."""
    warnings: list[str] = []
    tabs = spec.get("tabs")
    if not isinstance(tabs, list):
        return warnings
    by_id = {s.get("id"): s for s in spec.get("sections") or []}
    used: set[str] = set()
    orphans: set[str] = set()

    for tab in tabs:
        rule = LAYOUT_TABS.get(tab.get("id"))
        if rule is None:
            continue
        tid = tab["id"]
        if tab.get("intro"):
            warnings.append(f"tab {tid!r}: 'intro' dropped — this tab shows what its "
                            "script produced and nothing a model wrote")
            tab.pop("intro")
        kept, sections, dropped = [], [], []
        for b in tab.get("blocks") or []:
            kind = b.get("type", "section")
            if kind == "section" and b.get("id") in rule["sections"]:
                if b["id"] not in sections:
                    sections.append(b["id"])
            elif kind in rule["blocks"]:
                if kind == "testpairs":
                    _own_testpairs(b, tid, warnings)
                elif kind in ("diagrams", "puml"):
                    _own_diagram_title(b, tid, warnings)
                kept.append(b)
            else:
                dropped.append(f"{kind} {b.get('id')!r}" if b.get("id") else kind)
                if kind == "section":
                    orphans.add(b.get("id"))
        for sid in rule["required"]:
            if sid not in sections:
                sections.append(sid)
        for sid in rule["sections"]:
            wrote = LAYOUT_WHEN_WRITTEN.get(sid)
            if wrote and sid not in sections and (out_dir / wrote).is_file():
                sections.append(sid)
        if dropped:
            warnings.append(f"tab {tid!r}: dropped {', '.join(dropped)} — the tab is "
                            "script-owned; prose and snippets of your own do not go on it")
        sec_blocks = [{"type": "section", "id": sid} for sid in rule["sections"]
                      if sid in sections]
        tab["blocks"] = sec_blocks + kept if rule.get("first") else kept + sec_blocks
        _deployment_above_containers(tab["blocks"])
        auto = LAYOUT_BLOCK_WHEN_WRITTEN.get(tid)
        if auto and (out_dir / auto[1]).is_file() and not any(
                b.get("type") == auto[0]["type"] for b in tab["blocks"]):
            tab["blocks"].append(copy.deepcopy(auto[0]))
        used.update(sections)

    present = {t.get("id") for t in tabs}
    if LAYOUT_ALWAYS not in present and _video_step_ran(out_dir):
        at = next((i + 1 for i, t in enumerate(tabs) if t.get("id") == "review"), 0)
        tabs.insert(at, {"id": LAYOUT_ALWAYS, "label": LAYOUT_TABS[LAYOUT_ALWAYS]["label"],
                         "blocks": [{"type": "section", "id": "video"}]})
        used.add("video")

    # The tabs this module owns stand in its order, whatever order the content file wrote
    # them in (Victor, 20 Oct 2026: Complexity right after City, beside the other picture of
    # the code). A tab it does not know keeps its own slot.
    order = list(LAYOUT_TABS)
    slots = [i for i, t in enumerate(tabs) if t.get("id") in LAYOUT_TABS]
    for i, t in zip(slots, sorted((tabs[i] for i in slots), key=lambda t: order.index(t["id"]))):
        tabs[i] = t

    # A section only a dropped block pointed at goes with it: rendered, it would still
    # resolve its snippets for a page that never shows them.
    referenced = {b.get("id") for t in tabs for b in t.get("blocks") or []
                  if b.get("type", "section") == "section"}
    out = [s for s in spec.get("sections") or []
           if s.get("id") not in used and not (s.get("id") in orphans
                                               and s.get("id") not in referenced)]
    for sid in LAYOUT_SECTIONS:
        if sid in used:
            model = by_id.get(sid)
            if model and _layout_overridden(model, sid):
                warnings.append(f"section {sid!r} is built by the skill — its "
                                + ", ".join(_layout_overridden(model, sid))
                                + " in the content file differed and were replaced")
            out.append(_layout_section(sid, model, out_dir))
    if used or orphans:
        spec["sections"] = out

    scope = spec.get("scope")
    if isinstance(scope, list):
        typed = [c for c in scope if not c.get("auto")]
        if typed:
            spec["scope"] = [c for c in scope if c.get("auto")]
            names = [str(c.get("label") or c.get("value") or "?") for c in typed]
            hint = (' — {"auto": "diffstat"} measures files and lines at build time'
                    if {"files", "lines"} & set(names) else "")
            warnings.append(f"scope: {' and '.join(names)} typed by hand — dropped. The bar "
                            "carries computed chips only ({\"auto\": \"diffstat\"|\"tests\"|"
                            "\"autofixed\"})" + hint)
    return warnings



# ── which tab a changed diagram lands on when no block named it ─────────────────────────
#
# `puml-diff.sh` files every `.puml` the branch changed, wherever it lives, but a tab only
# shows the rows a block selects — `only: ["DomainModel", "DB"]` on Data, a `testpairs` on
# Sequence. A diagram nobody thought to list (PR #49's `petclinic-frontend/docs/
# frontend-packages.puml`, 20 Oct 2026) fell through every filter, and the build could only
# warn that "no tab claims" it. Naming it in the content file is a fix for one PR; the next
# one to touch a diagram nobody listed hits it again. So the build homes it by what it
# draws: classes and entities are data, boxes and arrows between them are structure.

#: A PlantUML declaration of a data shape — a class or an ERD table.
_DATA_DECL = re.compile(r"^\s*(?:abstract\s+class|class|entity|enum|interface|annotation|"
                        r"object|map|json)\s", re.M)
#: …and of a structural box: a component, a package, a module, a node.
_STRUCT_DECL = re.compile(r"^\s*(?:component|package|node|folder|frame|rectangle|"
                          r"artifact|database|cloud|\[[^\]]+\])", re.M)

#: The tab a diagram goes to when its own is not on the page, in order of preference.
_DIAGRAM_FALLBACK = ("packages", "data")


def diagram_home(row: dict, root) -> str:
    """The id of the tab a manifest row belongs on: `sequence` for a sequence diagram,
    `data` when the picture declares more classes and tables than boxes, else `packages`
    (Structure). Read off the drawing itself, so a new diagram folder needs no setting."""
    if row.get("kind") == "sequence":
        return "sequence"
    try:
        text = (root / row.get("source", "")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return ("data" if len(_DATA_DECL.findall(text)) > len(_STRUCT_DECL.findall(text))
            else "packages")


def claim_changed_diagrams(spec: dict, rows: list[dict], root) -> list[str]:
    """Give every changed diagram of the shared gallery a block on some tab, in place.

    A `puml` context card whose source the branch changed becomes the delta card for it,
    where it stood, keeping its name: drawn from source with an `unchanged` badge it would
    say the opposite of the card the build would otherwise add elsewhere. Any row still
    unclaimed joins its home tab (`diagram_home`): the tab's own shared `diagrams` block
    when it has one, else a new one after the tab's last diagram. Returns one line per
    diagram homed, for stderr."""
    tabs = spec.get("tabs") or []
    by_source = {r.get("source"): r for r in rows}
    claimed: set[str] = set()
    for tab in tabs:
        for b in tab.get("blocks") or []:
            kind = b.get("type", "section")
            if kind == "diagrams" and not b.get("manifest"):
                # A block that names its diagram by the card's label (`only: ["Frontend
                # packages"]`) and points at it through `context` is that diagram's block,
                # not a stranger to it. PR #49: the row went unclaimed, was appended to the
                # Java packages block as a delta, and this block drew the same file again
                # as `unchanged` underneath.
                ctx = b.get("context") or {}
                row = by_source.get(ctx.get("src"))
                if row and row["name"] not in (b.get("only") or []):
                    only = b.get("only") or []
                    b["only"] = ([row["name"] if n == ctx.get("name") else n for n in only]
                                 if ctx.get("name") in only else [*only, row["name"]])
                    if ctx.get("name"):
                        b["labels"] = {**(b.get("labels") or {}), row["name"]: ctx["name"]}
                claimed.update(r["name"] for r in select_rows(rows, b))
            elif kind == "testpairs":
                claimed.update(r["name"] for r in select_rows(rows, b)
                               if r.get("kind") == "sequence")
    notes: list[str] = []
    for tab in tabs:
        blocks = tab.get("blocks") or []
        for i, b in enumerate(blocks):
            row = by_source.get(b.get("src")) if b.get("type") == "puml" else None
            if not row or row["name"] in claimed:
                continue
            new = {"type": "diagrams", "only": [row["name"]]}
            if b.get("name"):
                new["labels"] = {row["name"]: b["name"]}
            for k in ("title", "body", "id"):
                if k in b:
                    new[k] = b[k]
            blocks[i] = new
            claimed.add(row["name"])
            notes.append(f"{row['name']}: changed, so its context card on tab "
                         f"{tab.get('id')!r} shows the delta")
    present = {t.get("id"): t for t in tabs}
    for row in rows:
        if row["name"] in claimed:
            continue
        home = diagram_home(row, root)
        tab = present.get(home) or next(
            (present[t] for t in _DIAGRAM_FALLBACK if t in present), None)
        if tab is None:
            continue
        blocks = tab.setdefault("blocks", [])
        shared = next((b for b in blocks if b.get("type") == "diagrams"
                       and not b.get("manifest") and b.get("only")), None)
        if shared is not None:
            shared["only"] = [*shared["only"], row["name"]]
        else:
            at = max((i + 1 for i, b in enumerate(blocks)
                      if b.get("type") == "puml"
                      or (b.get("type") == "diagrams" and not b.get("manifest"))), default=0)
            blocks.insert(at, {"type": "diagrams", "only": [row["name"]]})
        claimed.add(row["name"])
        notes.append(f"{row['name']}: changed and named by no block — shown on tab "
                     f"{tab.get('id')!r}")
    return notes
