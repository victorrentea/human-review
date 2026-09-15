"""The audit's screens are the app's whole catalogue, and the runner checks the diff against
it: a changed routed component no catalogue URL reaches is the finding the audit cannot
make by itself. Pure functions of source text and a file list, tested as such."""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("run_steps", HERE / "run-steps.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

PETS_ROUTING = """
const petRoutes: Routes = [
  {path: 'pets', component: PetListComponent},
  {path: 'pets/add', component: PetAddComponent},
  {
    path: 'pets/:id',
    children: [
      { path: 'edit', component: PetEditComponent },
      { path: 'visits\\/add', component: VisitAddComponent }
    ]
  },
  // {path: 'pets/:id/history', component: PetHistoryComponent},
  {path: 'vets/:id/edit', component: VetEditComponent, resolve: {vet: VetResolver}}
];
@NgModule({ imports: [RouterModule.forChild(petRoutes)] })
export class PetsRoutingModule {}
"""


def test_routes_come_out_flat_with_children_joined_onto_their_parent():
    assert rs.angular_routes(PETS_ROUTING) == [
        ("pets", "PetListComponent"),
        ("pets/add", "PetAddComponent"),
        ("pets/:id/edit", "PetEditComponent"),
        ("pets/:id/visits/add", "VisitAddComponent"),      # the escaped slash unescaped
        ("vets/:id/edit", "VetEditComponent"),             # `resolve: {…}` is not a route
    ]


def test_a_commented_out_route_is_not_a_route():
    assert "PetHistoryComponent" not in dict(rs.angular_routes(PETS_ROUTING)).values()


@pytest.mark.parametrize("pattern,url,hit", [
    ("pets/:id/visits/add", "pets/11/visits/add", True),
    ("pets/:id/visits/add", "http://localhost:4300/pets/11/visits/add?x=1#top", True),
    ("pets/:id/edit", "pets/11/visits/add", False),
    ("pets/:id", "pets/11/edit", False),                 # one parameter, one segment
    ("visits/:id/edit", "visits/1/edit", True),
    ("**", "anything/at/all", True),
    ("", "", True),
])
def test_a_catalogue_url_reaches_a_route(pattern, url, hit):
    assert rs.route_matches(pattern, url) is hit


def _app(tmp_path: Path) -> Path:
    """A small Angular tree: a routed edit form, a routed list page, and an unrouted child
    list embedded by two routed hosts."""
    src = tmp_path / "frontend" / "src" / "app"
    (src / "visits").mkdir(parents=True)
    (src / "pets").mkdir(parents=True)
    (src / "visits" / "visits-routing.module.ts").write_text("""
const routes: Routes = [
  {path: 'visits', component: VisitsPageComponent},
  {path: 'visits/:id/edit', component: VisitEditComponent},
];
export class VisitsRoutingModule {}
""")
    (src / "pets" / "pets-routing.module.ts").write_text("""
const petRoutes: Routes = [
  {path: 'pets', component: PetListComponent},
  {path: 'pets/:id', children: [{ path: 'visits\\/add', component: VisitAddComponent }]},
];
""")
    def comp(folder, stem, cls, selector, template=""):
        d = src / folder / stem
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.component.ts").write_text(
            f"@Component({{ selector: '{selector}', templateUrl: './{stem}.component.html' }})\n"
            f"export class {cls} {{}}\n")
        (d / f"{stem}.component.html").write_text(template)
        (d / f"{stem}.component.spec.ts").write_text("describe('x', () => {});")
    comp("visits", "visit-edit", "VisitEditComponent", "app-visit-edit", "<form></form>")
    comp("visits", "visits-page", "VisitsPageComponent", "app-visits-page", "<table></table>")
    comp("visits", "visit-list", "VisitListComponent", "app-visit-list", "<ul></ul>")
    comp("visits", "visit-add", "VisitAddComponent", "app-visit-add",
         "<form></form><app-visit-list [visits]='pet.visits'></app-visit-list>")
    comp("pets", "pet-list", "PetListComponent", "app-pet-list",
         "<app-visit-list [visits]='pet.visits'></app-visit-list>")
    (src / "visits" / "vet-name.pipe.ts").write_text("export class VetNamePipe {}")
    return tmp_path


def test_a_changed_routed_component_the_catalogue_misses_is_named_with_its_route(tmp_path):
    root = _app(tmp_path)
    changed = ["frontend/src/app/visits/visit-edit/visit-edit.component.html",
               "frontend/src/app/visits/visit-edit/visit-edit.component.spec.ts"]
    catalogue = {"Book a visit": "pets/11/visits/add", "Edit a pet": "pets/11/edit"}
    assert rs.unlisted_screens(changed, catalogue, ["frontend/src"], root) == [
        {"component": "VisitEditComponent", "route": "visits/:id/edit"}]


def test_a_catalogue_that_reaches_the_route_reports_nothing(tmp_path):
    root = _app(tmp_path)
    changed = ["frontend/src/app/visits/visit-edit/visit-edit.component.ts"]
    assert rs.unlisted_screens(changed, {"Edit a visit": "visits/1/edit"},
                               ["frontend/src"], root) == []


def test_an_unrouted_child_is_followed_one_hop_to_the_screens_that_embed_it(tmp_path):
    root = _app(tmp_path)
    changed = ["frontend/src/app/visits/visit-list/visit-list.component.html"]
    got = rs.unlisted_screens(changed, {"Book a visit": "pets/11/visits/add"},
                              ["frontend/src"], root)
    # VisitAddComponent embeds it too, but the catalogue reaches that one.
    assert got == [{"component": "PetListComponent", "route": "pets", "via": "<app-visit-list>"}]


def test_a_pipe_or_a_spec_alone_is_left_to_the_dom_diff(tmp_path):
    root = _app(tmp_path)
    changed = ["frontend/src/app/visits/vet-name.pipe.ts",
               "frontend/src/app/visits/visit-edit/visit-edit.component.spec.ts"]
    assert rs.unlisted_screens(changed, {}, ["frontend/src"], root) == []


def test_the_real_pr_shape_every_changed_screen_is_named_once(tmp_path):
    """The case that started this: four visit components changed, a two-screen list that
    covered one of them, and the edit form — the screen the branch was about — never
    audited. Each missing route is named exactly once."""
    root = _app(tmp_path)
    changed = [f"frontend/src/app/visits/{s}/{s}.component.{ext}"
               for s in ("visit-add", "visit-edit", "visit-list", "visits-page")
               for ext in ("ts", "html")]
    catalogue = {"Book a visit": "pets/11/visits/add", "Edit a pet": "pets/11/edit"}
    got = rs.unlisted_screens(changed, catalogue, ["frontend/src"], root)
    assert sorted((u["component"], u["route"]) for u in got) == [
        ("PetListComponent", "pets"),
        ("VisitEditComponent", "visits/:id/edit"),
        ("VisitsPageComponent", "visits"),
    ]
