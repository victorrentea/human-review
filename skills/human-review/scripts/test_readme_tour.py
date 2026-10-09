"""The README tour: pictures of the featured demo, and every page that shows them.

No browser here — the shooting is Playwright's. What these pin is the text around the
pictures: that only the managed regions move, that every link is derived from the one
featured slug the README names, and that a publish of any other slug touches nothing.
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), str(HERE / f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tour = _load("readme-tour")

README = """# human-review

intro prose

<!-- featured-demo:begin slug=demo -->
stale try-it paragraph
<!-- featured-demo:end -->

more prose

## A tour, one tab at a time

<!-- tour:begin -->
stale table
<!-- tour:end -->

## Install
"""

LANDING = """<main>
  <h1>/human-review</h1>
  <p class="lede">Static snapshots.</p>

  <h2>Published snapshots</h2>
</main>
"""

TAB_PAGE = """# API
<!-- panel: api -->

> Every operation the branch moved.

<!-- shot:begin -->
<!-- shot:end -->

## What you see

hand-written
"""


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/SomeFork/human-review.git"], check=True)
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    snap = tmp_path / "demo" / "demo"
    (snap / "tour").mkdir(parents=True)
    (snap / "review.html").write_text("<html></html>", encoding="utf-8")
    (snap / "content.json").write_text(json.dumps(
        {"title": "Link <b>Visit</b>", "subtitle": 'A <a href="x">visit</a> now has a vet'}),
        encoding="utf-8")
    (snap / "tour" / "tour.json").write_text(json.dumps({"slug": "demo", "shots": [
        {"panel": "review", "title": "Review", "file": "review.jpg"},
        {"panel": "api", "title": "API", "file": "api.jpg"},
    ]}), encoding="utf-8")
    (tmp_path / "demo" / "index.html").write_text(LANDING, encoding="utf-8")
    (tmp_path / "docs" / "tabs").mkdir(parents=True)
    (tmp_path / "docs" / "tabs" / "api.md").write_text(TAB_PAGE, encoding="utf-8")
    return tmp_path


def test_the_featured_slug_is_read_off_the_readme_marker():
    assert tour.featured_slug(README) == "demo"
    assert tour.featured_slug("no marker") is None


def test_replace_region_keeps_everything_outside_the_markers():
    out = tour.replace_region(README, tour.TOUR_BEGIN, tour.TOUR_END, "fresh")
    assert "fresh" in out and "stale table" not in out
    assert out.startswith("# human-review\n\nintro prose")
    assert out.endswith("<!-- tour:end -->\n\n## Install\n")
    assert "stale try-it paragraph" in out


def test_replace_region_refuses_a_missing_marker():
    with pytest.raises(ValueError):
        tour.replace_region("nothing", tour.TOUR_BEGIN, tour.TOUR_END, "x")


def test_a_fork_links_to_its_own_pages(repo):
    assert tour.pages_base(repo) == "https://somefork.github.io/human-review"


def test_a_tab_page_gives_its_panel_title_and_summary(repo):
    info = tour.read_tab_page(repo / "docs" / "tabs" / "api.md")
    assert info == {"panel": "api", "title": "API",
                    "summary": "Every operation the branch moved.", "file": "api.md"}


def test_one_run_rewrites_readme_tab_pages_and_landing_and_nothing_else(repo):
    assert tour.main(["--repo", str(repo), "--no-shoot"]) == 0

    readme = (repo / "README.md").read_text(encoding="utf-8")
    live = "https://somefork.github.io/human-review/demo/review.html"
    assert "stale" not in readme
    assert f'href="{live}"' in readme
    assert 'src="demo/demo/tour/review.jpg"' in readme
    # A tab with a page links to it and carries its summary; one without is still shown.
    assert "### [API](docs/tabs/api.md)" in readme
    assert "Every operation the branch moved." in readme
    assert "### Review" in readme
    assert f"{live}#api" in readme
    assert "intro prose" in readme and "## Install" in readme

    page = (repo / "docs" / "tabs" / "api.md").read_text(encoding="utf-8")
    assert 'src="../../demo/demo/tour/api.jpg"' in page
    assert "hand-written" in page

    landing = (repo / "demo" / "index.html").read_text(encoding="utf-8")
    assert landing.index('class="lede"') < landing.index("<!-- featured:begin -->") \
        < landing.index("Published snapshots")
    assert 'href="demo/review.html"' in landing
    assert 'src="demo/tour/review.jpg"' in landing
    assert 'href="demo/review.html#api"' in landing
    # The hero is a link; a link inside it would break the card out of its box.
    assert 'href="x"' not in landing

    # Idempotent: a second run changes nothing.
    before = [(p, p.read_bytes()) for p in (repo / "README.md", repo / "demo" / "index.html")]
    tour.main(["--repo", str(repo), "--no-shoot"])
    assert all(p.read_bytes() == b for p, b in before)


def test_publishing_another_slug_leaves_the_readme_alone(repo):
    before = (repo / "README.md").read_text(encoding="utf-8")
    assert tour.main(["--repo", str(repo), "--slug", "petclinic-pr", "--no-shoot"]) == 0
    assert (repo / "README.md").read_text(encoding="utf-8") == before


def test_feature_moves_the_marker_and_every_link_with_it(repo):
    other = repo / "demo" / "other"
    (other / "tour").mkdir(parents=True)
    (other / "review.html").write_text("<html></html>", encoding="utf-8")
    (other / "tour" / "tour.json").write_text(json.dumps({"shots": [
        {"panel": "api", "title": "API", "file": "api.jpg"}]}), encoding="utf-8")
    tour.main(["--repo", str(repo), "--feature", "other", "--no-shoot"])
    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert tour.featured_slug(readme) == "other"
    assert "demo/other/tour/api.jpg" in readme and "demo/demo/tour" not in readme
