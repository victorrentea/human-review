"""The Structure tab's boxes as ways into their folders.

Build side: a package box (`[..web] <<..web>>`) resolves to its package's directory under
the root package every box agrees on, a module box to the directory of the `pom.xml` whose
own artifactId it names, and each one carries that folder and a github.com link to it.
Server side: `/__reveal_folder__` takes a path relative to the checkout, refuses anything
that is not a folder inside it, and asks the owning window's bridge to reveal it.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

from test_action_server import _call, server, srv  # noqa: F401  (fixture)
from hrbuild.shared import folders


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _write(root: Path, rel: str, text: str = "") -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <parent><groupId>g</groupId><artifactId>the-parent</artifactId><version>1</version></parent>
  <artifactId>{aid}</artifactId>
</project>
"""

PACKAGES = """@startuml
hide stereotype
[..web] <<..web>>
[..web.dto] <<..web.dto>>
component "Domain" as dom <<..domain>>
[..gone] <<..gone>>
[..any] <<..(a|b)..>>
[..web] --> [..domain]
@enduml
"""

MODULES = """@startuml
component "shop-api" as shop_api
component "shop-core" as shop_core
component "not-a-module" as nam
shop_api --> shop_core
@enduml
"""


@pytest.fixture
def repo(tmp_path):
    """Two modules under no aggregator; a package tree rooted at `com.acme.shop`, plus a
    decoy `web` package under another root that a suffix match alone would also take."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _write(r, "shop-api/pom.xml", POM.format(aid="shop-api"))
    _write(r, "libs/core/pom.xml", POM.format(aid="shop-core"))
    src = "shop-api/src/main/java/com/acme/shop"
    for f in ("web/A.java", "web/dto/B.java", "domain/C.java", "App.java"):
        _write(r, f"{src}/{f}", "class X {}")
    _write(r, "shop-api/src/main/java/com/acme/legacy/web/Old.java", "class X {}")
    _write(r, "shop-api/docs/packages.puml", PACKAGES)
    _write(r, "shop-api/docs/modules.puml", MODULES)
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "one")
    folders._packages.cache_clear()
    folders.maven_modules.cache_clear()
    folders._tree_base.cache_clear()
    folders.forge_for.cache_clear()
    folders._branch_files.cache_clear()
    yield r
    folders._packages.cache_clear()
    folders.maven_modules.cache_clear()
    folders._tree_base.cache_clear()
    folders.forge_for.cache_clear()


def test_a_package_box_maps_to_its_folder_under_the_root_every_box_agrees_on(repo):
    got = folders.package_folders("shop-api/docs/packages.puml", repo)
    base = "shop-api/src/main/java/com/acme/shop"
    # `..web` also matches com.acme.legacy.web; the root the other boxes share decides.
    assert got == {"..web": f"{base}/web", "..web.dto": f"{base}/web/dto",
                   "dom": f"{base}/domain"}


def test_a_box_whose_pattern_names_no_one_folder_gets_none(repo):
    got = folders.package_folders("shop-api/docs/packages.puml", repo)
    assert "..gone" not in got and "..any" not in got


def test_a_module_box_maps_to_the_pom_whose_own_artifact_id_it_names(repo):
    assert folders.maven_modules(repo) == {"shop-api": "shop-api", "shop-core": "libs/core"}
    got = folders.folder_targets("shop-api/docs/modules.puml", repo)
    assert got == {"shop_api": ("shop-api", "shop-api"), "shop_core": ("shop-core", "libs/core")}


def _entity(name: str, label: str) -> str:
    return (f'<g class="entity" data-qualified-name="{name}" id="e"><rect x="1"/>'
            f'<text>{label}</text></g>')


def test_a_box_links_to_its_folder_on_github_at_the_reviewed_commit(repo):
    _git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    head = _git(repo, "rev-parse", "HEAD")
    svg = _entity("..web.dto", "..web.dto") + _entity("nobody", "nobody")
    out = folders.link_folders(svg, "shop-api/docs/packages.puml", repo)
    assert ('data-folder="shop-api/src/main/java/com/acme/shop/web/dto" '
            'data-folder-name="..web.dto" data-quiet="">') in out
    assert (f'<a href="https://github.com/acme/shop/tree/{head}/shop-api/src/main/java/com/'
            f'acme/shop/web/dto" data-tip="Open ..web.dto on GitHub">') in out
    assert _entity("nobody", "nobody") in out
    assert " title=" not in out


def test_without_a_github_remote_the_box_has_its_folder_and_no_link(repo):
    out = folders.link_folders(_entity("shop_core", "shop-core"), "shop-api/docs/modules.puml",
                               repo)
    assert 'data-folder="libs/core"' in out and "<a " not in out


def test_a_box_somebody_already_linked_is_left_alone(repo):
    svg = ('<g class="entity" data-qualified-name="shop_api"><a href="x"><rect/></a></g>')
    assert folders.link_folders(svg, "shop-api/docs/modules.puml", repo) == svg


# --------------------------------------------------------------------------- #
# the server: which paths it accepts
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rel", ["../outside", "/etc", "shop-api/../../x", "~/x", "a\\b",
                                 "", "  ", None, 3, "missing", "shop-api/pom.xml"])
def test_anything_but_a_folder_inside_the_checkout_is_refused(repo, rel):
    (repo.parent / "outside").mkdir(exist_ok=True)
    assert srv.checkout_folder(repo, rel) is None


def test_a_symlink_out_of_the_checkout_is_refused(repo, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", repo / "link")
    assert srv.checkout_folder(repo, "link") is None


def test_a_folder_inside_the_checkout_resolves_both_ways(repo):
    spelt, real = srv.checkout_folder(repo, "libs/core")
    assert spelt == repo / "libs/core" and real == (repo / "libs/core").resolve()
    assert srv.checkout_folder(repo, ".")[1] == repo.resolve()
    assert srv.checkout_folder(None, "libs/core") is None


# --------------------------------------------------------------------------- #
# the server: the endpoint and the bridge
# --------------------------------------------------------------------------- #

def test_the_probe_says_it_can_reveal_a_folder(server, monkeypatch):
    monkeypatch.setattr(srv, "ROOT", Path("/somewhere"))
    assert json.loads(_call(server, "GET", srv.MARKER)[1])["revealFolder"] is True


def test_the_endpoint_reveals_the_folder_the_page_names(server, repo, monkeypatch):
    asked = []
    monkeypatch.setattr(srv, "ROOT", repo)
    monkeypatch.setattr(srv, "reveal_folder",
                        lambda *a: asked.append(a) or {"how": "revealed", "window": "repo"})
    status, payload = _call(server, "POST", srv.REVEAL_FOLDER, {"path": "libs/core"})
    assert status == 200, payload
    assert json.loads(payload)["how"] == "revealed"
    assert asked == [(repo / "libs/core", (repo / "libs/core").resolve())]


@pytest.mark.parametrize("headers, status", [
    ({"X-Human-Review-Token": "wrong"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Origin": "https://evil.example"}, 403),
    ({"Content-Type": "text/plain"}, 415),
])
def test_the_endpoint_is_guarded_like_the_other_verbs(server, repo, monkeypatch, headers, status):
    asked = []
    monkeypatch.setattr(srv, "ROOT", repo)
    monkeypatch.setattr(srv, "reveal_folder", lambda *a: asked.append(a) or {"how": "revealed"})
    got, _ = _call(server, "POST", srv.REVEAL_FOLDER, {"path": "libs/core"}, headers)
    assert got == status and asked == []


def test_the_endpoint_refuses_a_path_out_of_the_checkout(server, repo, monkeypatch):
    monkeypatch.setattr(srv, "ROOT", repo)
    monkeypatch.setattr(srv, "reveal_folder", lambda *a: pytest.fail("must not be asked"))
    status, payload = _call(server, "POST", srv.REVEAL_FOLDER, {"path": "../"})
    assert status == 404 and "not a folder" in payload


def test_the_endpoint_says_so_when_no_window_has_the_checkout(server, repo, monkeypatch):
    monkeypatch.setattr(srv, "ROOT", repo)
    monkeypatch.setattr(srv, "reveal_folder", lambda *a: None)
    status, payload = _call(server, "POST", srv.REVEAL_FOLDER, {"path": "shop-api"})
    assert status == 409 and "logo in the header" in payload


def _bridge(tmp_path, monkeypatch, folder: Path, reveal_ok: bool):
    """A fake VS Code window on `folder`, recording the commands it is asked to run."""
    registry = tmp_path / "home" / ".walkie-talkie" / "ide"
    registry.mkdir(parents=True)
    seen = []
    ping = json.dumps({"ok": True, "app": "vscode", "folder": folder.name, "folders": [
        {"name": folder.name, "path": str(folder), "realPath": str(folder.resolve())}]}).encode()

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, ping)

        def do_POST(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            seen.append({k: v[0] for k, v in q.items()})
            if q["id"][0] == "revealInExplorer" and not reveal_ok:
                self._send(400, b'{"ok":false}')
            else:
                self._send(200, b'{"ok":true}')

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    (registry / "vscode-0.json").write_text(json.dumps({"port": httpd.server_address[1],
                                                        "token": "t"}))
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return httpd, seen


def test_the_owning_window_reveals_the_folder_and_comes_forward(repo, tmp_path, monkeypatch):
    httpd, seen = _bridge(tmp_path, monkeypatch, repo, reveal_ok=True)
    try:
        got = srv.reveal_folder(*srv.checkout_folder(repo, "libs/core"))
    finally:
        httpd.shutdown()
    assert got == {"how": "revealed", "window": "repo"}
    assert seen == [{"id": "revealInExplorer", "uri": (repo / "libs/core").as_uri()},
                    {"id": "workbench.action.focusWindow"}]


def test_a_bridge_too_old_to_reveal_still_raises_the_window(repo, tmp_path, monkeypatch):
    httpd, seen = _bridge(tmp_path, monkeypatch, repo, reveal_ok=False)
    try:
        got = srv.reveal_folder(*srv.checkout_folder(repo, "shop-api"))
    finally:
        httpd.shutdown()
    assert got == {"how": "focused", "window": "repo"}
    assert [s["id"] for s in seen] == ["revealInExplorer", "workbench.action.focusWindow"]


def test_no_window_on_the_checkout_is_none(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    httpd, seen = _bridge(tmp_path, monkeypatch, other, reveal_ok=True)
    try:
        assert srv.reveal_folder(*srv.checkout_folder(repo, "shop-api")) is None
    finally:
        httpd.shutdown()
    assert seen == []


@pytest.mark.skipif(not shutil.which("plantuml"),
                    reason="the folder links are stamped on the SVG plantuml draws")
def test_a_gallery_card_links_its_boxes_and_a_sequence_card_does_not(repo, tmp_path):
    """The Structure tab's delta gallery and its context cards go through one door."""
    from hrbuild.shared import diagrams
    out = tmp_path / "out"
    (out / "assets" / "diagrams").mkdir(parents=True)
    row = diagrams.unchanged_row("modules", "shop-api/docs/modules.puml")
    html_ = diagrams.render_diagrams({"manifest": "assets/diagrams/MANIFEST.tsv"}, repo, out,
                                     [row])
    assert 'data-folder="libs/core"' in html_
    seq = dict(row, kind="sequence")
    assert "data-folder=" not in diagrams.render_diagrams(
        {"manifest": "assets/diagrams/MANIFEST.tsv"}, repo, out, [seq])
    card = diagrams.render_puml({"src": "shop-api/docs/packages.puml", "name": "Java packages"},
                                repo, out)
    assert 'data-folder-name="..web.dto"' in card


def _box(name: str, label: str) -> str:
    return (f'<g class="entity" data-qualified-name="{name}" id="e">'
            f'<rect fill="#ECEFF1" height="36" rx="2.5" ry="2.5" width="100" x="10" y="20"/>'
            f'<text>{label}</text></g>')


def test_a_box_is_shaded_by_its_share_of_the_lines_added_and_lists_its_files(repo, monkeypatch):
    """Victor, 14 Oct 2026: the packages the branch grew show it, as a bar behind the box as
    wide as its share of the lines added, and hovering one lists its files, each a diff link.
    A file is counted once, in the deepest box that holds it: web/dto is not also web."""
    from hrbuild.shared import snippets
    import html as _html
    base = _git(repo, "rev-parse", "HEAD")
    src = "shop-api/src/main/java/com/acme/shop"
    _write(repo, f"{src}/web/A.java", "class X {}\n" + "int a;\n" * 2)   # +3 −1: the line had no newline
    _write(repo, f"{src}/web/dto/D.java", "\n".join(["x"] * 6) + "\n")  # +6, added
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "two")
    monkeypatch.setattr(snippets, "SNIPPET_BASE", base)
    svg = _box("..web", "..web") + _box("..web.dto", "..web.dto") + _box("dom", "Domain")
    out = folders.link_folders(svg, "shop-api/docs/packages.puml", repo)
    web, dto, dom = out.split("</g>")[:3]
    assert 'data-impact="0.333"' in web and 'data-impact="0.667"' in dto
    assert "data-impact" not in dom and "pkg-impact" not in dom, "a box the branch left alone"
    assert 'class="pkg-impact" x="10" y="20" width="66.7" height="36"' in dto
    # The bar is drawn over the box's own shape and under its name.
    assert dto.index('class="pkg-impact"') < dto.index("<text>")
    pop = _html.unescape(dto.split('data-tip-pop="')[1].split('"')[0])
    assert pop.startswith('<p class="tippophead">Green bar: <b>67%</b> of the lines this PR '
                          "added in this diagram are in <b>..web.dto</b><br>+6 in 1 file")
    assert ">D.java</a>" in pop, "named relative to the box the reader hovered"
    assert f'data-diff-path="{src}/web/dto/D.java"' in pop and f'data-diff-base="{base}"' in pop
    assert "D.java" not in _html.unescape(web), "counted in the deepest box only"


def test_a_box_the_branch_left_alone_is_drawn_quiet_and_can_be_filtered(repo, monkeypatch):
    """Victor, 29 Oct 2026: the eye should go to the boxes that carry the change. A box with
    no file changed in its folder is stamped `data-quiet`, unless the diff paints it green
    or red; a card with such a box gets the "Show only impacted" filter, a card without none."""
    from hrbuild.shared import snippets
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "shop-api/src/main/java/com/acme/shop/web/A.java", "class X {}\nint a;\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    monkeypatch.setattr(snippets, "SNIPPET_BASE", base)
    added = _box("..web.dto", "..web.dto").replace(
        "<rect ", '<rect style="stroke:var(--dgm-diff-add);" ')
    svg = _box("..web", "..web") + added + _box("dom", "Domain")
    out = folders.link_folders(svg, "shop-api/docs/packages.puml", repo)
    web, dto, dom = out.split("</g>")[:3]
    assert "data-quiet" not in web, "the branch changed a file in it"
    assert "data-quiet" not in dto, "the diff paints it: it is the change"
    assert 'data-quiet=""' in dom
    assert folders.only_impacted(out) == folders.ONLY_IMPACTED
    assert folders.only_impacted(web + "</g>") == ""


def test_a_box_named_by_a_folder_path_maps_to_that_folder(tmp_path):
    """The frontend has folders, not packages: `[owners] <<src/app/owners>>`, relative to the
    module the diagram sits in (the nearest package.json)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write(r, "web/package.json", "{}")
    _write(r, "web/src/app/owners/owner.ts", "export {}")
    _write(r, "web/src/app/app.module.ts", "export {}")
    _write(r, "web/docs/frontend-packages.puml",
           "@startuml\n[owners] <<src/app/owners>>\n[app] <<src/app>>\n"
           "[ghost] <<src/app/ghost>>\n@enduml\n")
    got = folders.package_folders("web/docs/frontend-packages.puml", r)
    assert got == {"owners": "web/src/app/owners", "app": "web/src/app"}


def test_a_one_line_box_has_its_name_centred_on_the_box():
    """PlantUML pads the name from its own font metrics; the browser's font is narrower, so
    the name sat left in its box (Victor, 20 Oct 2026). A class box with fields is kept."""
    from hrbuild.shared.folders import _center_label
    one = '<rect x="7" y="44" width="136" height="36"/><text font-size="14" x="17" y="68">m</text>'
    assert '<text font-size="14" x="75.00" y="68" text-anchor="middle">' in _center_label(one)
    two = one + '<text x="17" y="90">id : Integer</text>'
    assert _center_label(two) == two
