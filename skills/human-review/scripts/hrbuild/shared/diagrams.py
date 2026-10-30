"""The diagram gallery: manifests, view switchers, draw.io widgets, .puml renders."""
from __future__ import annotations

import html
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .actions import declare_action, rebuild_interpreter
from .commands import (RERUN_MARK_SCRIPT, TAB_RERUN_TIP, drawio_open_html, rerun_face,
                       rerun_html)
from .folders import link_folders, only_impacted
from .genseq import (genseq_details_at_base, genseq_details_at_render, genseq_file,
                     test_of_genseq)
from .svg import inline_svg
from .util import _pretty

def read_manifest(path: Path):
    rows = []
    if not path.is_file():
        return rows
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            rows.append(dict(zip(header, line.split("\t"))))
    return rows


VIEW_WORDS = {"new": "New", "old": "Old"}


def dgm_views_html(panes, initial: str = "diff", bar: bool = True) -> str:
    """The Diff / New-Old control and its panes — the ONE implementation of it.

    `panes` is an ordered list of `(view, inner-html)`, `"diff"` first when there is one.
    Three states off two buttons: the second holds both words and toggles between them,
    with the live one underlined, because a third pill costs as much room as the two that
    carry the argument.

    `initial` is which state the widget opens on. It is "diff" for a *structural* diagram,
    where the delta is a clean two-colour drawing and the whole point of the section. The
    UX audit passes "new": its delta is a pixel mask over a screenshot, and a half-ghosted
    photo of a form has to be decoded before it says anything.

    A sequence diagram has no Diff at all (Victor, 30 Oct 2026): it is drawn from traces,
    and a run that reorders two concurrent calls — or a generator that relabels an arrow —
    reorders the rows, so the delta reported changes nobody made and confused more than it
    showed. Its panes are New and Old alone, and no Diff button is drawn for a widget with
    no Diff pane. `bar=False` leaves the buttons out: a sequence beside its test wears them
    in the test's row instead (`seq_views_bar`, the same buttons).

    Callers: `render_diagrams` below, for every PlantUML delta, and `drawio_widget_html`,
    for the hand-drawn conceptual model. Behaviour and styling live in `DGM_VIEWS_JS` / the
    stylesheet and are delegated off `document`, so a widget that lands in a section body
    is driven by the same code as one emitted here — a second implementation of this would
    drift within a week.
    """
    views = [v for v, _ in panes]
    pair = [v for v in views if v in VIEW_WORDS]
    # Nothing to switch to: a lone "Diff" button is a control that does nothing, and a
    # frame colour-coding one state is a legend for a single entry. Emit the picture.
    if not pair or len(views) == 1:
        return "".join(body for _, body in panes)
    # A caller asking to open on a side that did not render would open on nothing at all,
    # so an absent side falls back to the first pane rather than blanking the widget.
    if initial not in views:
        initial = views[0]
    panels = "".join(
        f'<div class="dgmpane" data-view="{v}"{"" if v == initial else " hidden"}>{body}</div>'
        for v, body in panes)
    buttons = _dgm_buttons(views, initial) if bar else ""
    return (f'<div class="dgmviews" data-state="{initial}">'
            + (f'<div class="dgmbar">{buttons}</div>' if buttons else "") + panels + "</div>")


def _dgm_buttons(views, initial: str) -> str:
    """The widget's buttons: Diff when there is a delta pane, then the New/Old pair."""
    buttons = []
    if "diff" in views:
        buttons.append('<button type="button" class="dgm-diff" data-go="diff" '
                       f'aria-pressed="{str(initial == "diff").lower()}" '
                       'data-tip="Green added, red removed">Diff</button>')
    pair = [v for v in views if v in VIEW_WORDS]
    if pair:
        buttons.append(
            '<button type="button" class="dgm-newold" data-go="newold" '
            f'aria-pressed="{str(initial != "diff").lower()}" '
            'data-tip="The diagram on this branch, or at the base: click New or Old.">'
            + "/".join(f'<u data-view="{v}"{' class="on"' if v == initial else ""}>'
                       f'{VIEW_WORDS[v]}</u>' for v in pair)
            + "</button>")
    return "".join(buttons)


def seq_views_bar(card: str) -> str:
    """The New/Old buttons a sequence card left out (`bar=False`), for its test's row.

    Read off the card itself, so the row offers exactly the panes the card holds: none
    for a sequence this branch added (there is no Old), none for an unchanged one. The
    bar carries its own `data-state`, which `DGM_VIEWS_JS` keeps in step with the card's
    `.dgmviews`, since the stylesheet can no longer reach it from there."""
    m = re.search(r'"dgmviews" data-state="(\w+)">', card)
    if not m:
        return ""
    views = re.findall(r'<div class="dgmpane" data-view="(\w+)"', card)
    return (f'<span class="dgmbar seqviews" data-state="{m[1]}">'
            f'{_dgm_buttons(views, m[1])}</span>')


def _diagram_views(row, assets: Path, full_svg: Path, root: Path, bar: bool = True):
    """One diagram's panes: the delta (with its focus chooser inside it) plus whichever
    of the undiffed pair `puml-diff.sh` managed to render. Returns the markup and whether
    a New/Old pair exists — the header only advertises itself as a toggle when it does.

    A sequence diagram has no delta pane (see `dgm_views_html`): New, and Old beside it
    when the base had one. Only a manifest whose New side did not render falls back to
    the delta picture, alone — the one drawing of this branch's run there is."""
    sides = []
    for view, column in (("new", "new_svg"), ("old", "old_svg")):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            sides.append((view, f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'))
    if row.get("kind") == "sequence":
        if not sides or sides[0][0] != "new":
            return _focus_views(row, assets, full_svg, root), False
        # A sequence this branch added has nothing to compare with: its picture alone.
        if row.get("status") == "added" or len(sides) == 1:
            return sides[0][1], False
        return dgm_views_html(sides, initial="new", bar=bar), True
    panes = [("diff", _focus_views(row, assets, full_svg, root))] + sides
    if len(panes) == 1:
        return panes[0][1], False
    # A diagram this branch added has nothing to compare with: Diff (all green) and a lone
    # New are two buttons that change nothing (Victor, 4 Oct 2026). Its picture alone.
    if row.get("status") == "added" and panes[1][0] == "new":
        return panes[1][1], False
    return dgm_views_html(panes, initial="diff", bar=bar), True


# `{{drawio:conceptual}}` — the hand-drawn diagram's three pictures, read off
# `.human-review/assets/` at build time.
#
# It exists because this one diagram is the only thing on the page the report *asks the
# reader to go and change*: red is automation's to-do, and the picture is supposed to look
# different once a human has re-laid it out in draw.io. Markup pasted into content.json
# freezes it — the reader re-draws the map, rebuilds the page, and still sees the drawing
# that was current when a model last wrote the section, with a legend still promising a
# layout that has since been drawn. The token keeps the pictures in the files
# `drawio-diff.py` writes, so re-running that step and rebuilding is the whole refresh.
DRAWIO_TOKEN = re.compile(r"\{\{drawio:(?P<name>[A-Za-z0-9_.-]+)\}\}")

# Meanings, not colours: the swatch is already the colour, so the bold goes on the one
# thing the reader cannot see.
#
# The to-do row names the state and stops. It used to carry the whole instruction as well
# — what the red is, and to go open it in draw.io and turn every line black — and that
# sentence read as a puzzle at the size a legend is read at: the reader is standing in
# front of a picture, and the legend was explaining a workflow. The instruction is not
# lost, it is written on the map itself, in red, by `conceptual-model-patch.py`, where
# the person who can act on it is already looking. Both rows are read off the verdict, so
# the page stops saying it the moment it stops being true.
#
# The green row stops at "added by this PR" for the same reason. "New against the base
# branch" was the definition of "added by this PR" — the same fact, restated for a reader
# who has evidently understood it, since they are reading a legend on a diff.
CM_LEGEND_NEW = '<span class="new"><i></i><b>added by this PR</b></span>'

CM_LEGEND_TODO = ('<span class="todo"><i></i>'
                  "<b>still waiting for a manual re-layout</b></span>")


def trace_legend(verdict: dict) -> str:
    """What the weights and the red dashes mean, on a drawing laid over the traces
    (`drawio-diff.py --traces`). A row is there only when the picture shows one: a legend
    entry for a mark that is nowhere on the drawing sends the reader looking for it."""
    t = verdict.get("traces")
    if not t:
        return ""
    rows = []
    if t.get("walked"):
        rows.append('<span class="walked"><i></i><b>walked by a test</b></span>')
    if t.get("unwalked"):
        rows.append('<span class="unwalked"><i></i><b>no test walks it</b></span>')
    if t.get("undrawn"):
        rows.append('<span class="undrawn"><i></i><b>called in the traces, '
                    'missing from the drawing</b></span>')
    return f'<p class="cmlegend">{"".join(rows)}</p>' if rows else ""


def drawio_unchanged(verdict: dict) -> bool:
    """Did `drawio-diff.py` find nothing — no box or line added, removed, changed or
    moved, and nothing left red for a re-layout? An absent verdict is not "unchanged":
    it is a step that has not run, and the widget says that instead."""
    # Calls the traces made that no arrow carries are drawn in red by the overlay: a
    # to-do for whoever owns the picture, so the card keeps its editing row.
    undrawn = (verdict.get("traces") or {}).get("undrawn")
    return bool(verdict) and not undrawn and not any(
        verdict.get(k) for k in ("added", "removed", "changed", "moved", "red"))


def drawio_unchanged_at(name: str, assets: Path) -> bool:
    vfile = assets / f"{name}-diff.json"
    try:
        return drawio_unchanged(json.loads(vfile.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


#: The manifest key of one draw.io card's own green ring: `serve-review.py` answers
#: `POST /__rerun__ {card: <name>}` with the command declared under it, and nothing else.
def card_rerun_id(name: str) -> str:
    return f"__rerun__:card:{name}"


#: Beside this module's package: the program the card's ring runs (`refresh-card.py`).
REFRESH_CARD = Path(__file__).resolve().parents[2] / "refresh-card.py"

CARD_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def card_rerun_html(name: str, assets: Path, root: Path) -> str:
    """The green ring on a draw.io card's own title: redraw *this* picture and nothing else.

    The tab's ring re-runs the tab's whole producer — on the Data tab that is every
    PlantUML delta as well, then the whole page: forty seconds for one dragged box. This one
    re-runs the one `drawio-diff.py` the card was drawn by (the command its verdict
    recorded) and splices the card back into the page in place (`refresh-card.py`), so the
    reader is looking at the new layout about a second after pressing, scrolled where they
    were. The same face and the same words as the tab's ring: to the reader it is the same
    press, it is only narrower.

    Declared here, where the card is drawn, so the build and `refresh-card.py` put the same
    line in the manifest. Empty — and the tab's ring takes the title, as it always did —
    when the program is not beside us or the review directory is not inside the checkout."""
    if not CARD_NAME.match(name) or not REFRESH_CARD.is_file():
        return ""
    try:
        rel = str(assets.parent.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return ""
    declare_action(card_rerun_id(name),
                   f"cd {shlex.quote(str(Path(root).resolve()))} && "
                   f"{rebuild_interpreter()} {shlex.quote(str(REFRESH_CARD))}"
                   f" --dir {shlex.quote(rel)} --name {shlex.quote(name)}",
                   reload=True, label=f"Redraw the {name} diagram and put it back in place")
    tip = html.escape(TAB_RERUN_TIP, quote=True)
    return ('<span class="tabre">'
            '<button type="button" class="chip chip-rerun chip-served tabrerun" hidden '
            f'aria-disabled="true" data-rerun="__rerun__" data-card="{name}" '
            f'aria-label="Redraw this diagram" data-tip="{tip}">'
            f'{rerun_face(RERUN_MARK_SCRIPT)}</button></span>')


def _drawio_unchanged_card(name: str, verdict: dict, assets: Path, root: Path) -> str:
    """The hand-drawn diagram, when the branch did not touch it: the card a `puml`
    context diagram gets — title, UNCHANGED, the file, the whole picture.

    Everything else the widget carries is about a change. Diff / New-Old switches
    between two identical drawings; the red frame marks a delta that is empty; and
    Revert has nothing to put back.

    **Edit in app / Edit on web stay** (Victor, 30 Oct 2026): an untouched drawing is
    still one the reader may want to change, and hunting for the file was the only way
    in. After a save the card's own green ring redraws it, and a drawing that now differs
    from the base comes back as the full widget, Revert included."""
    svg = next((assets / f"{name}-{suffix}.svg" for suffix in ("new", "original", "diff")
                if (assets / f"{name}-{suffix}.svg").is_file()), None)
    if svg is None:
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-new.svg</code></p>')
    rel = verdict.get("diagram") or ""
    title = _pretty(Path(rel).name.split(".")[0]) if rel else _pretty(name)
    edit = drawio_open_html(verdict.get("drawio_url") or "", verdict.get("drawio_web_url") or "")
    # `data-drawio` names the card, for `refresh-card.py` to find it in the finished page and
    # for the page to swap it in place; the ring after the title is the card's own.
    return (f'<div class="diagram" data-drawio="{html.escape(name, quote=True)}">'
            f'<div class="head"><b>{html.escape(title)}</b>'
            + card_rerun_html(name, assets, root) + UNCHANGED_BADGE
            + diagram_provenance(rel, root, verdict.get("tested_against") or "")
            + (f'<div class="rerun-acts">{edit}</div>' if edit else "")
            + (_source_link(rel, root) if rel else "") + '</div>'
            f'<div class="svgbox">{inline_svg(svg, root)}</div>'
            + trace_legend(verdict) + '</div>')


def drawio_widget_html(name: str, assets: Path, root: Path, rebuild: str = "") -> str:
    """The Diff / New / Old widget for one `drawio-diff.py` output set.

    Which pane it opens on is not a style choice, it is a reading of the verdict: while
    anything in the drawing is still red, the delta is a picture of automation's routing
    rather than of the change, and `New` is the pane that answers first. Once the layout
    has been drawn by hand there is no red left, and `Diff` earns the open — which is the
    same rule the author used to have to remember and re-type after every re-layout.
    """
    verdict = {}
    vfile = assets / f"{name}-diff.json"
    if vfile.is_file():
        verdict = json.loads(vfile.read_text(encoding="utf-8"))
    if drawio_unchanged(verdict):
        return _drawio_unchanged_card(name, verdict, assets, root)
    red = bool(verdict.get("red"))
    green = any(not a.get("already_red") for a in verdict.get("added") or [])

    legend = (f'<p class="cmlegend">{CM_LEGEND_NEW if green else ""}'
              f'{CM_LEGEND_TODO if red else ""}</p>') if (green or red) else ""
    # Repeated under `New`, where the red is on screen with nothing else to explain it.
    # The green is not: nothing is coloured green in the undiffed drawing.
    todo_only = f'<p class="cmlegend">{CM_LEGEND_TODO}</p>' if red else ""
    traced = trace_legend(verdict)

    panes = []
    for view, suffix, tail in (("diff", "diff", legend + traced),
                               ("new", "new", todo_only + traced),
                               ("old", "original", traced)):
        svg = assets / f"{name}-{suffix}.svg"
        if svg.is_file():
            panes.append((view, f'<div class="svgbox">{inline_svg(svg, root)}</div>{tail}'))
    if not panes:
        print(f"[review] no {name}-*.svg under {assets} — run the diagrams step",
              file=sys.stderr)
        return (f'<p class="sub">not rendered — run the <code>diagrams</code> step to '
                f'write <code>{html.escape(name)}-diff.svg</code></p>')
    # `rerun_html` first, `dgm_views_html` after: this is where the caption sentence used
    # to sit, above the picture, naming the artefact and how honest it is. That prose is
    # gone from `content.json` now — the edit offer and its two buttons are the one thing
    # above the picture instead, so the block reads title, then what to do about the
    # drawing, then the drawing itself.
    #
    # Inside the card every other diagram on the tab sits in — title, file on the right —
    # and drawn here, not in `content.json`: the layout rewrites the `conceptual` section's
    # body to the bare token, so a card written around it by hand never reached the page,
    # and the one diagram the reader is asked to edit was the one with no frame (Victor,
    # 5 Oct 2026).
    rel = verdict.get("diagram") or ""
    title = _pretty(Path(rel).name.split(".")[0]) if rel else _pretty(name)
    # The action buttons in the header, right-aligned before the file name: they are the
    # card's presses, beside the green ring `place_tab_reruns` puts after the title.
    acts, under = rerun_html(verdict.get("rerun"), rebuild, name,
                             verdict.get("drawio_url") or "",
                             verdict.get("drawio_web_url") or "",
                             verdict.get("redraw"), verdict.get("revert"),
                             verdict.get("reveal"), verdict.get("tested_against") or "",
                             verdict.get("tested_against_path") or "")
    return (f'<div class="diagram dgm-toggles" data-drawio="{html.escape(name, quote=True)}">'
            f'<div class="head"><b>{html.escape(title)}</b>'
            + card_rerun_html(name, assets, root)
            + diagram_provenance(rel, root, verdict.get("tested_against") or "") + acts
            + (_source_link(rel, root) if rel else "") + '</div>'
            + under
            + dgm_views_html(panes, initial="new" if red else "diff")
            + '</div>')


def expand_drawio(text: str, out_dir: Path, root: Path, rebuild: str) -> str:
    return DRAWIO_TOKEN.sub(
        lambda m: drawio_widget_html(m["name"], out_dir / "assets", root, rebuild), text)


DGM_SRC_ANCHOR = re.compile(r'<a class="dgm-src"(?P<attrs>[^>]*)>(?P<face>[^<]+)</a>')


def shorten_dgm_src(markup: str) -> str:
    """A diagram header names its file by **name**, with the path on hover.

    `petclinic-backend/docs/ConceptualModel.drawio.png` spends two segments on where the
    repository keeps its documents before reaching the one word that answers "which
    drawing is this?" — and it does it in the header of a card whose title already said
    *Conceptual Model*. The name alone is the same answer in a quarter of the width; the
    path is still one hover away, which is where a reader goes only when they want to
    find the file rather than read the picture.

    This is the rule `srcbar_html` already applies to every quoted block on the page,
    down to the wording of the tip, so the two rows read the same way. It runs over
    rendered markup rather than at each call site because the conceptual model's header
    is written by hand into `content.json` — a rule enforced only in `_source_link`
    would hold for the generated PlantUML cards and quietly not for the one card the
    reader is being asked to go and edit.

    Idempotent: a face with no slash left in it is already short (or is a file at the
    repository root, which has no path to move)."""
    def one(m: re.Match) -> str:
        rel = html.unescape(m["face"])
        if "/" not in rel:
            return m.group(0)
        attrs = re.sub(r'\s+data-tip="[^"]*"', "", m["attrs"])
        tip = html.escape(f"Open in VS Code: {rel}", quote=True)
        return (f'<a class="dgm-src"{attrs} data-tip="{tip}">'
                f'{html.escape(Path(rel).name)}</a>')
    return DGM_SRC_ANCHOR.sub(one, markup)


def _source_link(rel: str, root: Path, out_dir: Path | None = None,
                 face: str | None = None) -> str:
    """The path already shown on the right of the header, made the link to the file.

    It used to be plain text with a second `<a>name.puml</a>` under the title — two
    controls for one destination, and the shorter of the two said less.

    `face` replaces the name on the card: a sequence diagram's generated file name is the
    test's name again plus `.genseq.puml`, so its card says `.puml` and keeps the path on
    the hover (Victor, 4 Oct 2026). A traced diagram that is not committed lives in the
    review's own copy (`assets/genseq/`), and links there."""
    # A traced diagram opens the copy the picture was drawn from — this run's, when the
    # traced run drew one, else the committed file — the rule the Sequence tab reads by
    # (`genseq_file`). It opened the committed file for some rows and the run's for others.
    target = genseq_file(rel, root) if rel.endswith(".genseq.puml") else root / rel
    if not target.is_file() and out_dir is not None:
        target = Path(out_dir) / "assets" / "genseq" / rel
    if target.is_file():
        href = f"vscode://file/{target.resolve()}:1:1"
        if face:
            tip = html.escape(f"Open in VS Code: {rel}", quote=True)
            return f'<a class="dgm-src" href="{href}" data-tip="{tip}">{html.escape(face)}</a>'
        return shorten_dgm_src(f'<a class="dgm-src" href="{href}">{html.escape(rel)}</a>')
    if face:
        return f'<span class="dgm-src" data-tip="{html.escape(rel, quote=True)}">{html.escape(face)}</span>'
    return f'<span>{html.escape(rel)}</span>'


# ── Where a diagram's truth comes from: tested against something, or generated ─────────
#
# A card's credential, after its status badge: a ✓ on a green dot and the fewest words —
# *ArchUnit-tested vs code* when a test reads the diagram file and checks it, *Generated
# from …* when the file is a generator's output. Read off the repository, never assumed:
# a diagram nothing tests and nothing generates gets no badge, which is a fact too.

#: Source files that may test or generate a diagram. Docs, configs and the diagram formats
#: themselves only mention it.
_PROV_CODE = (".java", ".kt", ".kts", ".groovy", ".scala", ".py", ".sh", ".js", ".mjs",
              ".ts", ".rb", ".go", ".gradle")

#: What a generator (or a test) reads, by the first thing its source names that says so.
#: Ordered: a script that dumps the schema out of a container also names Maven, and one
#: that walks the classes also names `.puml`.
_PROV_SOURCES = (
    (re.compile(r"pom\.xml|<modules?>", re.I), "pom.xml"),
    (re.compile(r"genseq|opentelemetry|\btraces?\b", re.I), "traces"),
    (re.compile(r"openapi", re.I), "OpenAPI"),
    # A schema dumped from a database the migrations built: the diagram follows the
    # incremental scripts, not a hand-kept DDL file — say so (Victor, 20 Oct 2026).
    (re.compile(r"(?s)(?=.*(?:flyway|liquibase|db/migration))"
                r".*(?:information_schema|DatabaseMetaData|pg_dump|\bDB\.sql\b|schema\.sql)",
                re.I), "DB schema built from incremental scripts"),
    (re.compile(r"information_schema|DatabaseMetaData|pg_dump|\bDB\.sql\b|schema\.sql",
                re.I), "DB schema"),
    (re.compile(r"com\.tngtech\.archunit|ClassFileImporter|src/main/java|getDeclaredFields"),
     "code"),
)
_WRITES = re.compile(r"\bwrite\w*\s*\(|newBufferedWriter|FileWriter|FileOutputStream"
                     r"|open\([^)]*['\"]w|>\s*\"?\$")
_TEST_PATH = re.compile(r"(^|/)(src/test|tests?|__tests__|spec)/|(Test|Tests|Spec|_test|\.spec"
                        r"|\.test)\.\w+$|(^|/)test_\w+\.py$")


def _prov_source(text: str) -> str:
    for rx, said in _PROV_SOURCES:
        if rx.search(text):
            return said
    return ""


def prov_badge(text: str, tip: str, href: str = "", warn: bool = False) -> str:
    """The credential itself: a link to the test or generator when there is one. `warn`
    is the lack of one — a view nothing checks — drawn as an amber `!` instead of the ✓."""
    face = f'<i aria-hidden="true">{"!" if warn else chr(0x2713)}</i>{html.escape(text)}'
    tip = html.escape(tip, quote=True)
    cls = "dgm-prov warn" if warn else "dgm-prov"
    if href:
        return (f'<a class="{cls}" href="{html.escape(href, quote=True)}" '
                f'data-tip="{tip}">{face}</a>')
    return f'<span class="{cls}" data-tip="{tip}">{face}</span>'


def _mentions(root: Path, rel: str) -> list[tuple[str, str]]:
    """`(path, text)` of every source file in the checkout that names the diagram file."""
    name = Path(rel).name
    try:
        out = subprocess.run(["git", "-C", str(root), "grep", "-l", "-F", "-e", name],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for p in sorted(out.splitlines()):
        if p == rel or not p.endswith(_PROV_CODE) or p.startswith(".human-review/"):
            continue
        try:
            found.append((p, (Path(root) / p).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return found


def diagram_provenance(rel: str, root: Path, against: str = "") -> str:
    """The badge for the diagram at `rel` (repository-relative), or "".

    **Generated** when the file sits under a `generated/` folder: the generator is the
    source file that names it and writes something, its own header saying it generates
    preferred; what it reads is named (`_PROV_SOURCES`), else the generator is.
    **Tested** otherwise, when a test names the file in a string literal — it opens it:
    *ArchUnit-tested vs code* when that test is ArchUnit's, *Tested vs <what>* when not.

    `against` is what the project declares the test checks the drawing against
    (`tested_against` in human-review.json, e.g. *Java Domain Model*). It goes in the
    hover, with the test's name — the badge face keeps its few words. It used to be a
    sentence of its own under a draw.io card's header, "Unit-tested against the Java
    Domain Model.", a whole line for what this badge already claims (Victor, 20 Oct 2026).
    Declared but with no test found, the badge still says so, unlinked: the declaration
    is the only carrier of that fact left on the card."""
    if not rel:
        return ""

    def tested_tip(who: str, what: str = "") -> str:
        """Which test, what it compares the drawing with, and what a difference costs."""
        return (f"{who} checks this drawing against the {what or against}: "
                "a mismatch fails the build")

    name = Path(rel).name
    mentions = _mentions(root, rel)
    if "generated" in Path(rel).parts[:-1]:
        return generated_badge(rel, root, mentions)
    # The file named in a string literal, in any of the quotes a test language uses: Java's
    # "…", and the single quotes and backticks of a JS guardrail (`packages-arch.test.mjs`'s
    # `'docs/frontend-packages.puml'`, which a double-quote-only match missed).
    literal = re.compile("|".join(f"{q}[^{q}\\n]*{re.escape(name)}{q}" for q in "\"'`"))
    tests = [(p, t) for p, t in mentions if _TEST_PATH.search(p) and literal.search(t)]
    if not tests:
        return prov_badge("Unit-tested", tested_tip("A unit test")) if against else ""
    archunit = [(p, t) for p, t in tests if "com.tngtech.archunit" in t]
    path, text = (archunit or tests)[0]
    href = f"vscode://file/{(Path(root) / path).resolve()}:1:1"
    # `ConceptualModelDiagramTest`, but `packages-arch.test.mjs`: a stem with a dot left in
    # it is no name a reader would search for.
    stem = Path(path).stem
    test = stem if "." not in stem else Path(path).name
    source = "code" if archunit else (_prov_source(text) or "code")
    # Undeclared, the hover still says which test and what a mismatch costs — what it is
    # checked against is then the badge's own word (*code*, *traces*).
    tip = tested_tip(test, "" if against else source)
    if archunit:
        return prov_badge("ArchUnit-tested vs code", tip, href)
    return prov_badge(f"Tested vs {source}", tip, href)


def generated_badge(rel: str, root: Path, mentions: list | None = None,
                    source: str = "", tip: str = "") -> str:
    """*Generated from code*, linked to the source file that writes `rel`, or "" when none
    does. A diagram under `generated/` gets it from `diagram_provenance`; the API tab asks
    for its spec (`openapi.yaml`, written by a test from the controllers) directly."""
    name = Path(rel).name
    writers = [(p, t) for p, t in (_mentions(root, rel) if mentions is None else mentions)
               if _WRITES.search(t)]
    # The one that writes it on the line that names it, then the one whose header says
    # it generates, then a script before a test.
    same_line = re.compile(r"^.*" + re.escape(name) + r".*$", re.M)
    writers.sort(key=lambda pt: (
        not any(_WRITES.search(m.group(0)) for m in same_line.finditer(pt[1])),
        not re.search(r"generat", pt[1][:3000], re.I),
        bool(_TEST_PATH.search(pt[0]))))
    if not writers:
        return ""
    path, _ = writers[0]
    source = source or next((s for s in (_prov_source(t) for _, t in writers) if s), "")
    href = f"vscode://file/{(Path(root) / path).resolve()}:1:1"
    return prov_badge(f"Generated from {source}" if source
                      else f"Generated by {Path(path).name}",
                      tip.format(test=Path(path).name) if tip
                      else f"Written by {Path(path).name}", href)


def spec_version(path: Path) -> str:
    """`info.version` of an OpenAPI spec, YAML or JSON, or "" when it has none. Read with
    PyYAML when it is installed, else by pattern: the build does not require PyYAML, and
    the version is one scalar under `info:` in every spec a generator writes."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(text)
        ver = ((doc or {}).get("info") or {}).get("version") if isinstance(doc, dict) else None
        return "" if ver is None else str(ver)
    except Exception:  # no PyYAML, or a spec it cannot read: the patterns below
        pass
    flow = re.search(r'^info:\s*\{[^}]*?\bversion:\s*["\']?([^"\',}]+)', text, re.M)
    block = re.search(r'^info:[ \t]*\n((?:(?:[ \t]+.*)?\n)*)', text, re.M)
    inner = block and re.search(r'^[ \t]+version:[ \t]*["\']?([^"\'\n#]+?)["\']?[ \t]*$',
                                block.group(1), re.M)
    js = re.search(r'"info"\s*:\s*\{[^{}]*?"version"\s*:\s*"([^"]+)"', text)
    m = flow or inner or js
    return m.group(1).strip() if m else ""


def api_spec_provenance(band: str, root: Path, out_dir: Path | None = None) -> str:
    """The API tab's head, finished with what the spec itself says about its origin.

    * The spec's version ends the verdict band's line, after the differ's report
      (Victor, 29 Oct 2026): it used to sit beside the visual diff's title, a row lower.
    * *Generated from code*, linked to the test that writes the spec, then the spec
      itself, linked like a card's `.puml` (Victor, 29 Oct 2026), goes to the right end
      of the visual diff's bar: it is projected into that bar's `prov` slot as a light
      child of its shadow host, so it keeps this page's styles. With no visual diff on
      the tab it ends the band instead, where it sat before.

    The spec is `spec` in human-review.json (`openapi.yaml` by default). Unchanged when
    the band is missing or the spec is not in the checkout."""
    # The band's own `</div>`: it holds only spans, and the section goes on past it (the
    # visual diff, whose script has `</div>`s of its own in strings).
    start = band.find('<div class="apiverdict')
    end = band.find("</div>", start) if start >= 0 else -1
    if end < 0:
        return band
    try:
        cfg = json.loads((Path(root) / "human-review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    spec = (cfg.get("spec") if isinstance(cfg, dict) else None) or "openapi.yaml"
    if not (Path(root) / spec).is_file():
        return band
    ver = spec_version(Path(root) / spec)
    stamp = (f'<span class="apiver" data-tip="The version in {html.escape(Path(spec).name)}'
             f'\'s info block">v{html.escape(ver)}</span>') if ver else ""
    badge = generated_badge(spec, root, source="code",
                            tip="{test} writes this contract from the Java controllers and "
                                "DTOs, so it is the code's, never hand-edited")
    # The name, the path on the hover: a diagram card's file link, IDE-following tip and all.
    prov = (f'{badge}{_source_link(spec, root, out_dir, face=Path(spec).name)}'
            if badge else "")
    host = re.search(r'<div class="oavhost" id="embed-swaggerdiff"[^>]*>(?=</div>)', band)
    if prov and host:
        band = (band[:host.end()] + f'<span slot="prov" class="apiprov">{prov}</span>'
                + band[host.end():])
        prov = ""
    elif prov:
        prov = f'<span class="apiprov">{prov}</span>'
    return band[:end] + stamp + prov + band[end:]


def _provenance(rel: str, root: Path) -> str:
    """Links back to what produced a diagram: the test that generated it, and the .puml.

    A sequence diagram is evidence only if the reviewer can reach the scenario behind it.
    `test_of_genseq` is what derives that — from the picture's own `src://` handle, or
    failing that from its name — so the guide never has to be told, and the link survives
    both the per-scenario naming and the generator filing its output away from the test."""
    links = []
    if rel.endswith('.genseq.puml'):
        test = test_of_genseq(rel, root)
        if (root / test).is_file():
            links.append(f'<a class="srcref" href="vscode://file/{(root / test).resolve()}:1:1">'
                          f'generated by {html.escape(Path(test).name)}</a>')
    return ('<p class="prov">' + " ".join(links) + '</p>') if links else ''


# Which radius a reviewer meets first — the change and nothing else.
#
# It opened on the whole diagram for a while, on the reasoning that a pruned view is a
# claim that the rest does not matter. In practice the whole DB and DomainModel deltas are
# a wall of forty unchanged entities with the change somewhere inside, so it moved to one
# hop of context, and then to none: the first question a delta has to answer is *what
# changed*, and every element beside it is a candidate the eye still has to rule out. Zero
# is the only level that cannot mislead about that. Each wider radius is one click away,
# and the reader takes it the moment the change alone does not explain itself.
DEFAULT_FOCUS = "0"


def _focus_views(row, assets: Path, full_svg: Path, root: Path) -> str:
    """The delta at each focus level, one visible at a time, with the chooser above them.

    All of them are inlined rather than fetched on demand: the guide has to survive being
    emailed as a single file, and a chooser whose other options 404 is worse than none.
    """
    levels = []
    for pair in (row.get("focus") or "").split(","):
        level, sep, name = pair.partition(":")
        svg = assets / name
        if sep and svg.is_file():
            levels.append((level, svg))
    levels.append(("all", full_svg))

    if len(levels) == 1:                       # sequence diagrams, and anything unpruned
        return f'<div class="svgbox">{inline_svg(full_svg, root)}</div>'

    default = DEFAULT_FOCUS if any(l == DEFAULT_FOCUS for l, _ in levels) else "all"
    buttons = "".join(
        f'<button type="button" data-level="{html.escape(level)}" '
        f'aria-pressed="{"true" if level == default else "false"}">{html.escape(level)}</button>'
        for level, _ in levels
    )
    boxes = "".join(
        f'<div class="svgbox" data-level="{html.escape(level)}"'
        f'{"" if level == default else " hidden"}>{inline_svg(svg, root)}</div>'
        for level, svg in levels
    )
    return (
        '<div class="focus"><span class="lbl">Diff + extra neighbours:</span>'
        + buttons + "</div>" + boxes
    )


def select_rows(rows, block) -> list:
    """The manifest rows one diagram block is responsible for.

    Tabs split the gallery by what a diagram *answers* — sequence diagrams sit next to
    the tests that generated them, DB and DomainModel next to each other — so a block
    names either the families it takes (`kind`) or the diagrams themselves (`only`)."""
    kinds = block.get("kind")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        rows = [r for r in rows if r["kind"] in kinds]
    only = block.get("only")
    if only:
        rows = [r for r in rows if r["name"] in only]
    if block.get("except"):
        rows = [r for r in rows if r["name"] not in block["except"]]
    return rows


def _why_not_drawn(row, assets: Path) -> str:
    """What a pair says where its picture should be — in PlantUML's words, not ours.

    This used to read `not rendered — see <file>.diff.puml`, which says nothing a reader
    can act on: it sounds like a step that has not run yet, and it points at a source file
    that looks perfectly fine, because the fault is one line of it. PlantUML does not fail
    on a bad line — it draws a picture of the complaint and exits 0 — so the pipeline
    spots the words "Syntax Error", drops that picture, and leaves the complaint in a
    `.err` sidecar. Read it back here: the message, the line, and a click that opens the
    source on it."""
    puml = row.get("diff_puml") or ""
    err = assets / f"{puml}.err"
    reason = {}
    if puml and err.is_file():
        try:
            reason = json.loads(err.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reason = {}
    line = reason.get("line") or 0
    src = assets / puml if puml else None
    face = html.escape(puml or "the delta source")
    link = (f'<a class="dgm-src" href="vscode://file/{src.resolve()}:{line or 1}:1">{face}</a>'
            if src and src.is_file() else f"<code>{face}</code>")
    if not reason:
        return f'<p class="sub">not rendered — see {link}</p>'
    where = f" at line {line}" if line else ""
    offending = (f'<br><code>{html.escape(reason.get("source") or "")}</code>'
                 if reason.get("source") else "")
    return (f'<p class="sub">diagram could not be drawn: '
            f'{html.escape(reason.get("message") or "PlantUML could not parse it")}'
            f'{where} of {link}{offending}</p>')


#: The status of a diagram the branch did not change: on a manifest row (`puml-diff.sh`
#: files a structural diagram whose text moved but whose elements and relationships did
#: not as `unchanged`; the C2 projection files its own the same way), and on a row made
#: up for a diagram an `only` names that no manifest row covers.
UNCHANGED = "unchanged"

#: The one look "this branch left it alone" has, wherever a diagram is drawn: the badge
#: the Structure tab's `puml` cards have always carried. A C2 row filed as unchanged used
#: to wear the grey `sev-low` badge a *deleted* diagram wears, inside the full Diff /
#: New-Old frame — two looks for one fact on the same tab.
UNCHANGED_BADGE = f'<span class="badge sev-info">{UNCHANGED}</span>'

#: …and its opposite, on a structural card the branch did change. It used to say nothing
#: ("modified" is what a delta gallery always shows), and the C2 card was then the one card
#: on the Structure tab with no state at all — just a red frame that read as an error.
CHANGED_BADGE = ('<span class="badge sev-med" data-tip="This branch changed the diagram">'
                 'changed</span>')

#: The packages and modules cards shade a box green by the share of its code the branch
#: changed, beside a badge saying `unchanged`: the badge is about the drawing, said so.
UNCHANGED_CONTEXT_TIP = "Diagram unchanged; green = code changed inside"

def _unchanged_body(row, assets: Path, root: Path, out_dir: Path) -> str:
    """An unchanged diagram is the whole current picture and nothing else.

    No Diff / New-Old switch, no red frame, no focus chips: each of those is a control
    over a delta, and there is no delta. The whole diagram, not focus 0 — the radius
    chooser exists to find the change inside a large picture, and with no change the
    focus-0 view is an empty note saying so. The undiffed `new` render is preferred
    (`puml-diff.sh` already drew it); a row made up from source has none, and is drawn
    the way a `puml` context card is."""
    for column in ("new_svg", "svg"):
        name = (row.get(column) or "").strip()
        if name and (assets / name).is_file():
            return f'<div class="svgbox">{inline_svg(assets / name, root)}</div>'
    cache, why_not = _context_svg(row["source"], root, out_dir)
    return f'<div class="svgbox">{inline_svg(cache, root)}</div>' if cache else why_not


def find_diagram_source(name: str, root: Path) -> str | None:
    """The repository's `.puml` for a diagram an `only` names and no manifest row has.

    `puml-diff.sh` files only the diagrams whose text changed, so a block written as
    `only: ["DomainModel", "DB"]` used to show just the half that moved — and on a branch
    whose migration only added indexes, which the ERD does not draw, the Data tab had no
    DB at all. A name the author asked for is a promise to show that diagram; when it did
    not change it is shown as it stands, with the UNCHANGED badge. Looked up by file
    name, outside the review directory; an ambiguous name takes the shallowest path and
    says so."""
    out = subprocess.run(["git", "ls-files", "--", f"{name}.puml", f"*/{name}.puml"],
                         cwd=root, capture_output=True, text=True)
    hits = sorted((p for p in out.stdout.splitlines()
                   if p and not p.startswith(".human-review/")),
                  key=lambda p: (p.count("/"), p))
    if len(hits) > 1:
        print(f"[review] {name}.puml is ambiguous ({', '.join(hits)}) — showing {hits[0]}",
              file=sys.stderr)
    return hits[0] if hits else None


def unchanged_row(name: str, rel: str) -> dict:
    """A manifest-shaped row for a diagram this branch left alone, drawn from source."""
    return {"name": name, "source": rel, "kind": "structural", "status": UNCHANGED,
            "diff_puml": "", "svg": "", "focus": "", "new_svg": "", "old_svg": ""}


def render_diagrams(spec, root: Path, out_dir: Path, rows=None, bare: str = "") -> str:
    """`bare` is the test file a pair's heading already names.

    A sequence diagram inside a test pair used to print three answers to one question in
    four centimetres: a title that was the test's file name with `.genseq` on the end, a
    `generated by <test>` line under it, and the `.puml` path on the right — above a fold
    whose summary was that same file name. Only the last is news. So the title and the
    provenance line go, and the href they carried for the scenario links rides on the card
    as `data-test-src` instead."""
    manifest = out_dir / spec.get("manifest", "assets/diagrams/MANIFEST.tsv")
    if rows is None:
        rows = read_manifest(manifest)
        if spec.get("only"):
            rows = [r for r in rows if r["name"] in spec["only"]]
    if not rows:
        return '<p class="sub">No PlantUML diagram changed on this branch.</p>'
    notes = spec.get("notes", {})
    # A card's heading, when the block it came from had a name for it (a `puml` context
    # card the branch changed keeps its "Java packages", not the file's `packages`).
    labels = spec.get("labels") or {}
    order = {"structural": 0, "sequence": 1}
    # An explicit `only` is a running order, not just a filter: an author who writes
    # ["DomainModel", "DB"] means the domain first. Alphabetical only decides the rest.
    wanted = spec.get("only") or []
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 9),
                                       wanted.index(r["name"]) if r["name"] in wanted else 99,
                                       r["name"]))
    parts = []
    for r in rows:
        note = notes.get(r["name"], "")
        svg_rel = manifest.parent / r["svg"] if r.get("svg") else None
        if r.get("status") == UNCHANGED:
            body, toggles = _unchanged_body(r, manifest.parent, root, out_dir), False
        elif svg_rel and svg_rel.is_file():
            body, toggles = _diagram_views(r, manifest.parent, svg_rel, root, bar=not bare)
        else:
            body, toggles = _why_not_drawn(r, manifest.parent), False
        # A package's or a Maven module's box opens its folder — every view of it, the
        # delta and the whole picture alike. A sequence's boxes are participants, not code.
        if r.get("kind") != "sequence":
            body = link_folders(body, r["source"], root)
        test_src = (f' data-test-src="vscode://file/{(root / bare).resolve()}:1:1"'
                    if bare and (root / bare).is_file() else "")
        # A card beside its test (`bare`) has no header row of its own any more: its New/Old
        # buttons and its `.puml` link ride in the test's row (`render_testpairs`), so the
        # picture starts one line higher (Victor, 30 Oct 2026). Only a badge with something
        # to say keeps a head.
        head = (
            ("" if bare else f'<b>{html.escape(labels.get(r["name"]) or _pretty(r["name"]))}</b>')
            # A badge earns its place by saying something surprising. "modified" is what
            # a diagram in a delta gallery always is, and "structural" is legible from the
            # picture — so only the states that carry information get one.
            + (UNCHANGED_BADGE if r["status"] == UNCHANGED else
               # A new diagram says "new", once: inside a test pair the card's own
               # `new test` chip already says it, so nothing here (Victor, 4 Oct 2026).
               "" if r["status"] == "added" and bare else
               '<span class="badge sev-high">new</span>' if r["status"] == "added" else
               f'<span class="badge sev-low">{html.escape(r["status"])}</span>'
               if r["status"] != "modified" else
               "" if bare or r.get("kind") == "sequence" else CHANGED_BADGE)
            + only_impacted(body)
            + ("" if bare or r.get("kind") == "sequence" else _row_provenance(r, root))
            + ("" if bare else _source_link(r["source"], root, out_dir,
                                            ".puml" if r.get("kind") == "sequence" else None)))
        parts.append(
            f'<div class="diagram{" dgm-toggles" if toggles and not bare else ""}'
            f'{" dgm-bare" if bare else ""}"{test_src}>'
            + (f'<div class="head">{head}</div>' if head else "")
            + (f"<p>{note}</p>" if note else "")
            + ("" if bare else _provenance(r["source"], root))
            + genseq_details_at_render(r, manifest.parent, root)
            + ("" if r["status"] == UNCHANGED
               else genseq_details_at_base(r, manifest.parent, root))
            + body
            # A producer's own line about the row (the C2 projection: "projected from the
            # committed sequences, not re-traced"). Text, escaped: a manifest is not HTML.
            + (f'<p class="sub dgm-stale">{html.escape(r["note"])}</p>'
               if (r.get("note") or "").strip() else "")
            + '</div>'
        )
    return "\n".join(parts)


def _row_provenance(r: dict, root: Path) -> str:
    """A manifest row's credential: the producer's own word when it drew the picture itself
    (`generated`, the C2 projection's `traces`), else what the repository says."""
    made = (r.get("generated") or "").strip()
    if made:
        return prov_badge(f"Generated from {made}", f"Drawn by this review from the {made}")
    return diagram_provenance(r.get("source") or "", root)


def render_puml(block, root: Path, out_dir: Path) -> str:
    """A diagram this branch did *not* change, drawn as context rather than as a delta.

    Package structure is the case that asks for it: a reviewer wants to see the shape
    the change landed in even on the \u2014 common, and good \u2014 branches that left it alone.
    Rendered here from the committed source, so the page carries no stale SVG."""
    src = root / block["src"]
    cache, why_not = _context_svg(block["src"], root, out_dir)
    if not cache:
        return why_not
    svg = link_folders(inline_svg(cache, root), block["src"], root)
    return (
        '<div class="diagram">'
        f'<div class="head"><b>{html.escape(block.get("name", src.stem))}</b>'
        f'<span class="badge sev-info"'
        + (f' data-tip="{UNCHANGED_CONTEXT_TIP}"' if block.get("status", UNCHANGED) == UNCHANGED
           else "")
        + f'>{html.escape(block.get("status", "unchanged"))}</span>'
        + only_impacted(svg)
        + diagram_provenance(block["src"], root)
        + _source_link(block["src"], root) + '</div>'
        + (f'<p>{block["note"]}</p>' if block.get("note") else "")
        + _provenance(block["src"], root)
        + f'<div class="svgbox">{svg}</div></div>'
    )


def _context_svg(rel: str, root: Path, out_dir: Path):
    """The committed `.puml` drawn as it stands, cached under assets/ by mtime.

    Returns `(svg_path, "")`, or `(None, <the paragraph to print instead>)`: a missing
    file and a missing PlantUML are the two ways there is no picture, and each is named
    rather than swallowed. Shared by the `puml` block and by a test pair whose sequence
    exists but carried no delta \u2014 the same picture, drawn the same way."""
    src = root / rel
    if not src.is_file():
        return None, f'<p class="sub">no diagram at <code>{html.escape(rel)}</code></p>'
    cache = out_dir / "assets" / (Path(rel).stem + ".context.svg")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file() or cache.stat().st_mtime < src.stat().st_mtime:
        try:
            out = subprocess.run(["plantuml", "-tsvg", "-pipe"],
                                 input=src.read_bytes(), capture_output=True)
        except FileNotFoundError:  # not on PATH: the same paragraph, not a traceback
            out = None
        if out is None or out.returncode != 0 or not out.stdout:
            return None, (f'<p class="sub">plantuml could not render '
                          f'<code>{html.escape(rel)}</code> \u2014 is it installed?</p>')
        cache.write_bytes(out.stdout)
    return cache, ""
