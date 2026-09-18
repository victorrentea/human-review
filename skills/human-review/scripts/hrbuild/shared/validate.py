"""What a content file must carry before the page is worth building."""
from __future__ import annotations

from pathlib import Path

# What an entry must carry. A nested tuple means *any one of these* — the rule is that an
# item has to say something past its title, not that it has to say it in a particular key.
# `body` was the only accepted place for months, because a model writing the content file
# put its prose there; an item parsed out of `review-points.md` often has no prose at all
# and carries its whole argument in `why:` (a finding that was declined) or in
# `alternative:` (a reading that was not taken). Both are the item saying something
# checkable, and refusing them would mean the branch's own record could not satisfy a rule
# written for a different author.
REQUIRED = {
    "sections": ("id", "title"),
    "tabs": ("id", "label"),
    "findings": ("title", ("body", "why")),
    "assumptions": ("title", ("body", "why", "alternative")),
    "autofixes": ("title",),
}


def validate(spec: dict, out_dir: Path) -> list[str]:
    """Every problem in the content file, named, in one pass.

    A bare ``KeyError: 'png'`` from 300 lines further down tells the author nothing about
    which entry was wrong. ``resolve_refs`` already collects and names its failures; this is
    the same courtesy for the rest of the file, and it runs before any subprocess so a bad
    content file costs a second rather than a full page build."""
    problems = []
    for key, fields in REQUIRED.items():
        for i, item in enumerate(spec.get(key) or []):
            for f in fields:
                if isinstance(f, tuple):
                    if not any(item.get(alt) for alt in f):
                        problems.append(
                            f"{key}[{i}] has none of "
                            + ", ".join(repr(alt) for alt in f)
                            + " — an item has to say something past its title")
                    continue
                # An explicit empty title is a decision, not an omission: a section whose
                # content announces itself does not need a heading repeating the tab name
                # above it. A *missing* key is still the mistake it always was.
                if f == "title" and f in item and not item[f]:
                    continue
                if not item.get(f):
                    problems.append(f"{key}[{i}] is missing {f!r}")
    v = spec.get("verdict")
    if v is not None and "score" not in v:
        problems.append("verdict is missing 'score' (0-10, drives the pip scale)")
    city = spec.get("codecity")
    if city is not None:
        for f in ("png", "href"):
            if f not in city:
                problems.append(f"codecity is missing {f!r}")
        if city.get("png") and not (out_dir / city["png"]).is_file():
            problems.append(f"codecity.png -> {city['png']} does not exist — did step 4 run?")
    for i, s in enumerate(spec.get("sections") or []):
        inc = s.get("includeHtml")
        if inc and not (out_dir / inc).is_file():
            problems.append(f"sections[{i}] ({s.get('id')}) includeHtml -> {inc} "
                            "does not exist — did the step that produces it run?")
    # A requirement may name tests only when the page can say what happened to them. The
    # alternative — rendering every one as "unchanged" because no manifest was loaded —
    # would be the page inventing an answer, which is the one thing it must never do.
    tc = spec.get("testChanges")
    if tc and not (out_dir / tc).is_file():
        problems.append(f"testChanges -> {tc} does not exist — run scripts/test-changes.py first")
    pt = spec.get("playwrightTraces")
    if pt and not (out_dir / pt).is_file():
        problems.append(f"playwrightTraces -> {pt} does not exist — "
                        "run scripts/playwright-traces.py first")
    if any(b.get("type") == "traces" for t_ in spec.get("tabs") or []
           for b in t_.get("blocks") or []) and not pt:
        problems.append("a tab declares a 'traces' block, but no top-level "
                        "'playwrightTraces' manifest says which recordings to show")
    for i, s in enumerate(spec.get("sections") or []):
        for j, req in enumerate(s.get("requirements") or []):
            if not req.get("text"):
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] is missing 'text'")
            for k, t in enumerate(req.get("tests") or []):
                if not t.get("name"):
                    problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] "
                                    f"tests[{k}] is missing 'name'")
            if req.get("tests") and not tc:
                problems.append(f"sections[{i}] ({s.get('id')}) requirements[{j}] names tests, "
                                "but no top-level 'testChanges' manifest says what the change "
                                "set did to them")
    for c in spec.get("extraCss") or []:
        if not (out_dir / c).is_file():
            problems.append(f"extraCss -> {c} does not exist "
                            "(the fragment's --css was never written)")
    ids = {s.get("id") for s in spec.get("sections") or []}
    for t_ in spec.get("tabs") or []:
        for b in t_.get("blocks") or []:
            if b.get("type") == "section" and b.get("id") not in ids:
                problems.append(f"tabs[{t_.get('id')}] references section {b.get('id')!r}, "
                                "which is not in 'sections'")
    return problems
