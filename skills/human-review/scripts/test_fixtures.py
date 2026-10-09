"""DB fixture colours and the fixture each E2E test starts from (hrbuild/shared/fixtures.py)."""
import json
import subprocess
from pathlib import Path

from hrbuild.shared import fixtures as fx

GLUE = """import {Given, When} from '@cucumber/cucumber';
// a comment that says seed must not count
Given('the clinic has these owners', async function () {
  expect(found, 'is the DB seeded by Flyway?').toBeTruthy();
});
Given('a pet born on {word}', async function (d) { await axios.post('/owners', {}); });
Given('the green household', async function () { await post('/__reset/green'); });
When('I search owners for {string}', async function (s) {});
"""

FEATURE = """Feature: owners
  Background:
    Given the clinic has these owners

  Scenario: Search
    When I search owners for "Pot"

  @fixture:green
  Scenario: Tagged
    When I search owners for "x"
"""

OWN_DATA = """Feature: own
  Background:
    Given a pet born on 2020-01-01

  Scenario: Makes its own rows
    When I search owners for "a"

  Scenario: Loads green itself
    Given the green household
"""

SPEC = """import {test} from './support/trace-fixture';
import * as dsl from './my.dsl';

test('uses the seed through the DSL', async () => {
  await dsl.owner();
});
"""

DSL = """// the seed's owner 1
const SEEDED_OWNER = 1;
export async function owner() { return SEEDED_OWNER; }
"""

OWN_SPEC = """import {test} from './support/trace-fixture';
// Mentions the seed only in a comment.
test('creates its own owner', async () => {
  await axios.post('/owners', {});
});
test('asks for blue explicitly', async () => {
  await loadFixture('blue');
});
"""


def repo(tmp_path: Path, colours=None) -> Path:
    files = {
        "be/db/fixtures/green.sql": "-- green",
        "be/db/fixtures/blue.sql": "-- blue",
        "e2e/src/owners.feature": FEATURE,
        "e2e/src/own.feature": OWN_DATA,
        "e2e/src/owners.glue.ts": GLUE,
        "e2e/src/a.spec.ts": SPEC,
        "e2e/src/my.dsl.ts": DSL,
        "e2e/src/b.spec.ts": OWN_SPEC,
    }
    if colours is not None:
        files["be/db/fixtures/fixture-colors.json"] = json.dumps(colours)
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_unconfigured_fixtures_take_the_palette_in_folder_order(tmp_path):
    reg = fx.fixture_colors(repo(tmp_path))
    assert reg["seed"] == fx.FIXTURE_SEED
    assert reg["colors"] == {"blue": fx.FIXTURE_PALETTE[0], "green": fx.FIXTURE_PALETTE[1]}


def test_configured_colour_wins_and_the_palette_skips_it(tmp_path):
    reg = fx.fixture_colors(repo(tmp_path, {"green": fx.FIXTURE_PALETTE[0], "seed": "#777777"}))
    assert reg["seed"] == "#777777"
    assert reg["colors"]["green"] == fx.FIXTURE_PALETTE[0]
    assert reg["colors"]["blue"] == fx.FIXTURE_PALETTE[1]


def test_a_colour_that_is_not_one_is_ignored(tmp_path):
    reg = fx.fixture_colors(repo(tmp_path, {"green": "red;}</style><script>"}))
    assert reg["colors"]["green"] in fx.FIXTURE_PALETTE


def test_each_test_starts_from_what_its_code_says(tmp_path):
    tests = fx.fixtures_by_test(repo(tmp_path))
    # Background step definition asserts seeded rows.
    assert tests["e2e/src/owners.feature:5"] == "seed"
    # An explicit tag beats the Background's seed.
    assert tests["e2e/src/owners.feature:9"] == "green"
    # A scenario whose steps load the fixture through the reset endpoint.
    assert tests["e2e/src/own.feature:8"] == "green"
    # Through an imported DSL, whose code names the seeded rows.
    assert tests["e2e/src/a.spec.ts:4"] == "seed"
    assert tests["e2e/src/b.spec.ts:6"] == "blue"


def test_unknown_start_gets_no_entry_rather_than_a_guess(tmp_path):
    tests = fx.fixtures_by_test(repo(tmp_path))
    assert "e2e/src/own.feature:5" not in tests          # makes its own rows
    assert "e2e/src/b.spec.ts:3" not in tests            # "seed" only in a comment


def test_a_test_that_leans_on_nothing_is_still_on_the_seed_and_listed_apart(tmp_path):
    """Nothing resets the DB between E2E tests, so they all run on Default; one that makes
    its own rows is `free` — a hollow ring — never "no dot", which read as "not Default"."""
    reg = fx.fixture_registry(repo(tmp_path))
    assert "e2e/src/own.feature:5" in reg["free"] and "e2e/src/b.spec.ts:3" in reg["free"]
    assert not set(reg["free"]) & set(reg["tests"])
    assert fx.fixtures_free(tmp_path) == reg["free"]


HOOKS = """package x.functional;
import io.cucumber.java.Before;
public class DatabaseHooks {
    @Before
    public void reset() {
        jdbc.execute("TRUNCATE TABLE visits, pets, owners RESTART IDENTITY CASCADE");
    }
}
"""


def test_a_jvm_suite_that_truncates_before_each_scenario_starts_them_empty(tmp_path):
    root = repo(tmp_path)
    for name, text in {"api/src/test/java/x/functional/DatabaseHooks.java": HOOKS,
                       "api/src/test/resources/features/a.feature":
                           "Feature: a\n  Scenario: one\n    Given x\n\n"
                           "  @fixture:green\n  Scenario: two\n    Given y\n"}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    reg = fx.fixture_registry(root)
    assert reg["empty"] == {"api/src/test/resources/features/a.feature:2":
                            {"tables": ["visits", "pets", "owners"], "hook": "DatabaseHooks"}}
    assert "api/src/test/resources/features/a.feature:2" not in reg["free"]
    # The hook is its module's: the e2e/ suite next to it is not truncated by it.
    assert not any(k.startswith("e2e/") for k in reg["empty"])


def test_registry_block_is_script_safe_and_absent_without_fixtures(tmp_path):
    block = fx.render_fixture_registry(repo(tmp_path))
    assert block.startswith('<script type="application/json" id="hr-fixtures">')
    assert "</" not in block[len('<script'):-len("</script>")]
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    assert fx.render_fixture_registry(empty) == ""


def test_the_script_words_the_tip_like_the_demo_bar_and_opens_the_dataset():
    js = (Path(fx.__file__).resolve().parent.parent / "assets" / "fixtures.js").read_text()
    assert "'DB Fixture: ' + (name === 'seed' ? 'Default' : name)" in js
    assert "window.hrOpenDataset" in js
    assert "MutationObserver" in js
    assert "DB Fixture: Default \\u00b7 doesn\\u2019t rely on its rows" in js
    assert "Starts empty (truncated)" in js
