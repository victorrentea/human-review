#!/usr/bin/env python3
"""The page is built by a package now — this is what the split is not allowed to break.

`build-review-html.py` was one 10,500-line file. It is an orchestrator over `hrbuild/`:
one module per tab under `tabs/`, what two or more tabs share under `shared/`, and the
stylesheet and scripts as real files under `assets/`. The move was mechanical and the
page it renders is byte-for-byte the one it rendered before.

Nothing here re-checks the *content* of a tab — every other test module does that. What
is pinned here is what only a split can get wrong, and what would go wrong silently:

  * an asset file that no longer round-trips into the string the page inlines, because
    somebody stripped a trailing newline or wrapped it in `<script>` twice;
  * the CSS and the JavaScript arriving in a different order than they used to, which
    changes which rule wins and which script runs before which;
  * a tab module that fell out of the page because the orchestrator stopped importing it;
  * a name defined in two modules at once, so which one the page gets depends on import
    order;
  * a name the package defines that the orchestrator no longer re-exports — every other
    script in this skill, and two dozen test modules, load this file by path and reach
    for functions on it.

Run with:  python3 -m pytest test_build_split_identity.py
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("build_review", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

PKG = HERE / "hrbuild"
SOURCE = (HERE / "build-review-html.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# one module per tab, and the orchestrator reaches every one of them
# --------------------------------------------------------------------------- #

#: Each tab module and the entry point the orchestrator calls into it for. A tab whose
#: module stops exporting its renderer has stopped being on the page; the panel it used
#: to fill would simply not be emitted, and no other test would notice.
TAB_ENTRY_POINTS = {
    "review": ("render_pile_block", "render_findings", "render_assumptions",
               "render_autofixes", "aftermath_html", "resolve_review_points"),
    "sequence": ("render_testpairs",),
    "tests": ("render_test_ledger", "render_requirements", "render_tests", "render_traces"),
    "demo": ("video_html", "embed_html"),
    "city": ("CITY_HEADING",),
    "logging": ("logging_fragment",),
    "owners": ("codeowners_fragment",),
    "cost": ("cost_ledger_html", "cost_ledger_report", "cost_chip"),
}


def test_every_tab_module_on_disk_is_one_this_test_knows_about():
    """A new tab gets a row above, so its renderer is held to the same promise."""
    on_disk = {p.stem for p in (PKG / "tabs").glob("*.py") if p.stem != "__init__"}
    assert on_disk == set(TAB_ENTRY_POINTS)


@pytest.mark.parametrize("tab", sorted(TAB_ENTRY_POINTS))
def test_the_orchestrator_imports_every_tab_module(tab):
    mod = importlib.import_module(f"hrbuild.tabs.{tab}")
    for entry in TAB_ENTRY_POINTS[tab]:
        assert hasattr(mod, entry), f"hrbuild/tabs/{tab}.py no longer defines {entry}"
        assert getattr(build, entry) is getattr(mod, entry), \
            f"build-review-html.py no longer re-exports {entry} from tabs/{tab}.py"


# --------------------------------------------------------------------------- #
# the assets round-trip, byte for byte
# --------------------------------------------------------------------------- #

def _asset_constants():
    """Every `NAME = _text(...)` / `NAME = _script(...)` in shared/assets.py."""
    tree = ast.parse((PKG / "shared" / "assets.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in ("_text", "_script")):
            yield node.targets[0].id, node.value.func.id, node.value.args[0].value


def test_every_asset_file_is_inlined_exactly_as_it_sits_on_disk():
    """No stripping, no re-indenting, no normalising — the file *is* what the page gets.

    A `.strip()` slipped in here would be invisible on screen and would change the bytes
    of every page this skill has ever produced."""
    assets = importlib.import_module("hrbuild.shared.assets")
    seen = 0
    for name, how, filename in _asset_constants():
        raw = (PKG / "assets" / filename).read_text(encoding="utf-8")
        want = raw if how == "_text" else "<script>\n" + raw + "</script>"
        assert getattr(assets, name) == want, f"{name} is not {filename} verbatim"
        seen += 1
    assert seen >= 20, "an asset went missing from shared/assets.py"


# --------------------------------------------------------------------------- #
# the base stylesheet is one file per module, and the page carries every one of them
# --------------------------------------------------------------------------- #

def test_the_base_stylesheet_is_the_css_files_concatenated_in_the_declared_order():
    """`CSS` is `assets/css/<name>.css` for every name in `CSS_FILES`, verbatim and in
    that order -- the order is the cascade, so it is declared once and read from there.

    `page.css` used to be one file of fifteen hundred lines with every tab's rules
    interleaved: the one place two agents changing two tabs collided. It is now one file
    per module, the same split as the Python under `tabs/` and `shared/`."""
    assets = importlib.import_module("hrbuild.shared.assets")
    want = "".join((PKG / "assets" / "css" / f"{name}.css").read_text(encoding="utf-8")
                   for name in assets.CSS_FILES)
    assert assets.CSS == want


def test_every_css_file_on_disk_is_one_the_page_emits():
    """A stylesheet dropped into `assets/css/` is not on the page until it is named in
    `CSS_FILES` -- and a name listed there with no file behind it is a build that cannot
    start. Either mistake is silent everywhere but here."""
    assets = importlib.import_module("hrbuild.shared.assets")
    on_disk = {p.stem for p in (PKG / "assets" / "css").glob("*.css")}
    assert on_disk == set(assets.CSS_FILES)
    assert len(assets.CSS_FILES) == len(set(assets.CSS_FILES)), "a file is emitted twice"


def test_every_tab_module_has_a_stylesheet_of_its_own_or_none_at_all():
    """A tab's rules live in `css/<tab>.css`, named after its module, so an agent working
    on one tab knows without looking which stylesheet is theirs. `core.css` is the page
    frame every tab shares and `frame.css` the strip and the panels, emitted last because
    they have to outrank whatever a tab says about scroll margins and hidden panels."""
    assets = importlib.import_module("hrbuild.shared.assets")
    tabs = {p.stem for p in (PKG / "tabs").glob("*.py") if p.stem != "__init__"}
    shared = {p.stem for p in (PKG / "shared").glob("*.py") if p.stem != "__init__"}
    for name in assets.CSS_FILES:
        assert name in tabs | shared | {"core", "frame"}, \
            f"css/{name}.css is named after no module: a tab's rules go in css/<tab>.css"
    assert assets.CSS_FILES[0] == "core" and assets.CSS_FILES[-1] == "frame"


def test_a_script_asset_is_javascript_and_not_an_html_fragment():
    """The wrapper is added at load time so the file on disk is lintable JavaScript.

    A `<script>` tag checked into a `.js` file would render identically and break every
    editor, formatter and linter pointed at it."""
    for path in sorted((PKG / "assets").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        assert "<script>" not in text, f"{path.name} carries its own <script> tag"
        assert text.endswith("\n"), f"{path.name} must end with the newline the page emits"


# --------------------------------------------------------------------------- #
# the order the page emits them in
# --------------------------------------------------------------------------- #

#: The stylesheet, in the order `main` writes it into the single `<style>` element. Order
#: is not cosmetic here: these are cascading rules, and `LATE_CSS` is named for the fact
#: that it has to come after the page's own and after whatever a generator contributed.
CSS_ORDER = ["CSS", "FOOTER_CSS", "extra_css", "LATE_CSS", "XREF_CSS"]

#: Every `<script>` the page carries, in the order they appear at the foot of the body.
#: `PAINT_HOLD_JS` is the exception that proves the rule — it is emitted up in the
#: `<head>`, and the release has to be the last thing after the strip has done its work.
JS_ORDER = [
    "SERVER_JS", "CAPTION_JS", "APP_ENV_JS", "GENSEQ_JS", "FOCUS_JS", "DGM_VIEWS_JS",
    "XREF_JS", "EDITOR_JS", "FRAME_JS", "TRACE_JS", "SEQLINK_JS", "SEQFOLD_JS",
    "HSCROLL_JS", "TABS_JS", "PAINT_RELEASE_JS", "RERUN_JS", "TIP_JS",
]


def _document_template() -> str:
    """The f-string `main` assembles the page from."""
    m = re.search(r'\n    doc = f"""(.*?)\n"""\n', SOURCE, re.S)
    assert m, "the document template moved — this test has to follow it"
    return m.group(1)


def test_the_stylesheet_is_assembled_in_the_order_it_always_was():
    style = re.search(r"<style>(.*?)</style>", _document_template(), re.S).group(1)
    assert re.findall(r"\{(\w+)", style) == CSS_ORDER


def test_the_scripts_are_emitted_in_the_order_they_always_were():
    template = _document_template()
    head, body = template.split("<body>", 1)
    assert "{PAINT_HOLD_JS}" in head, "the paint hold has to be in the <head> to hold anything"
    emitted = [n for n in re.findall(r"\{(\w+_JS)\}", body)]
    assert emitted == JS_ORDER


# --------------------------------------------------------------------------- #
# nothing is defined twice, and nothing the package defines is unreachable
# --------------------------------------------------------------------------- #

def _package_modules():
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        yield ".".join(path.relative_to(PKG.parent).with_suffix("").parts), path


def _top_level_names(path: Path):
    """What a module defines at the top level — not what it imported from a sibling."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    yield t.id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id


#: The three names a build *moves*, rather than reads: the two counters the Review tab's
#: numbered list keeps (`reset_list` / `opening_lede`) and the Logging tab's `--no-model`
#: switch, which `main` sets on `hrbuild.tabs.logging` before rendering anything.
#:
#: They are why the sweep below asks identity of everything else and only existence of
#: these: a re-export copies the *value* at import time, so `build.OFFLINE` is a snapshot
#: of a switch that has since moved. Reading one of them off `build-review-html.py` is a
#: bug waiting to happen, and so is patching one there — the module that declares it is
#: the only honest handle.
REBOUND = {"_LIST_OFFSET", "_LEDE_SHOWN", "OFFLINE"}


def test_nothing_else_in_the_package_rebinds_a_module_global():
    """A fourth one would join the list above — or, better, would not be written."""
    found = set()
    for _, path in _package_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Global):
                found.update(node.names)
    assert found <= REBOUND


def test_the_no_model_switch_is_set_on_the_module_that_reads_it():
    """`main` used to say `global OFFLINE`. In a package that would rebind a re-exported
    copy and leave the Logging tab calling the model on a `--no-model` build — which is
    the one failure mode that costs money."""
    assert "hrbuild.tabs.logging.OFFLINE = args.no_model" in SOURCE
    assert "global OFFLINE" not in SOURCE


def test_no_name_is_defined_in_two_modules_at_once():
    """Two homes for one name is a page whose content depends on import order."""
    where = {}
    clashes = []
    for dotted, path in _package_modules():
        for name in _top_level_names(path):
            if name.startswith("__") or name in ("ASSETS", "_text", "_script"):
                continue
            if name in where:
                clashes.append(f"{name}: {where[name]} and {dotted}")
            where[name] = dotted
    assert clashes == []


def test_everything_the_package_defines_is_re_exported_by_the_orchestrator():
    """`build-review-html.py` is the import surface the whole skill uses.

    `ds-audit.py`, `serve-review.py` and two dozen test modules load it by path and reach
    for a function on it. A helper that moved into a module and was not re-exported is an
    AttributeError in somebody else's script, at their build time, not at ours."""
    missing = []
    for dotted, path in _package_modules():
        mod = importlib.import_module(dotted)
        for name in _top_level_names(path):
            if name.startswith("__") or name in ("ASSETS", "_text", "_script"):
                continue
            # `HERE` is the one name with two homes on purpose: this file needs it before
            # it can import anything (it is what puts the scripts directory on sys.path),
            # and `shared/util.py` recomputes the same directory from its own location.
            # Equal, not identical — checked on its own below.
            if name == "HERE":
                continue
            # A rebindable switch: the re-export is a snapshot, so identity is the wrong
            # question. That it is reachable at all is still the promise.
            if name in REBOUND:
                assert hasattr(build, name)
                continue
            if getattr(build, name, None) is not getattr(mod, name):
                missing.append(f"{name} (hrbuild/{path.relative_to(PKG)})")
    assert missing == []


def test_the_package_and_the_orchestrator_agree_on_where_the_scripts_live():
    """`HERE` is computed twice — once here, before any import can happen, and once in
    `shared/util.py`, three directories up from itself. They have to be the same place:
    it is what every `subprocess` call in this build resolves its script against."""
    util = importlib.import_module("hrbuild.shared.util")
    assert util.HERE == build.HERE == HERE
