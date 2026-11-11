"""alembic-review.py: the Data tab's migrations card."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("alembic_review", HERE / "alembic-review.py")
ar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ar)

OLD = '''"""create things"""
from alembic import op
import sqlalchemy as sa
revision = "a1"
down_revision = None


def upgrade():
    op.create_table("things", sa.Column("id", sa.Integer, primary_key=True))


def downgrade():
    op.drop_table("things")
'''

NEW = '''"""tighten things"""
from alembic import op
import sqlalchemy as sa
revision = "b2"
down_revision = "a1"


def upgrade():
    op.add_column("things", sa.Column("name", sa.String(), nullable=False))
    op.add_column("things", sa.Column("kind", sa.String(), nullable=False, server_default="x"))
    op.drop_column("things", "legacy")
    op.execute("UPDATE things SET kind = 'y'")


def downgrade():
    pass
'''


def _repo(tmp_path):
    r = tmp_path / "svc"
    v = r / "src" / "alembic" / "versions"
    v.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (v / "a1_create.py").write_text(OLD)
    run("add", "-A")
    run("commit", "-qm", "base")
    run("tag", "base")
    return r, v


def test_each_op_is_listed_and_the_risky_ones_flagged(tmp_path, monkeypatch):
    r, v = _repo(tmp_path)
    (v / "b2_tighten.py").write_text(NEW)
    monkeypatch.chdir(r)
    rev = ar.revision("src/alembic/versions/b2_tighten.py", NEW)
    assert (rev["revision"], rev["down"], rev["message"], rev["noDowngrade"]) == \
        ("b2", "a1", "tighten things", True)
    got = [(o["text"], [f[1] for f in o["flags"]]) for o in rev["ops"]]
    assert got == [
        ("add_column things.name sa.String() NOT NULL", ["fails on rows"]),
        ("add_column things.kind sa.String() NOT NULL", []),
        ("drop_column things.legacy", ["destructive"]),
        ("execute UPDATE things SET kind = 'y'", ["raw SQL"]),
    ]
    assert ar.changed("base", ar.versions_dirs()) == [("A", "src/alembic/versions/b2_tighten.py")]


def test_an_edited_old_migration_and_models_without_a_migration_are_red(tmp_path, monkeypatch):
    r, v = _repo(tmp_path)
    (v / "a1_create.py").write_text(OLD.replace('"things"', '"stuff"'))
    gen = r / ".human-review" / "generated-puml"
    gen.mkdir(parents=True)
    (gen / "DB.base.puml").write_text("@startuml\nentity a {\n}\n@enduml\n")
    (gen / "DB.puml").write_text("@startuml\nentity a {\n  x : Int\n}\n@enduml\n")
    monkeypatch.chdir(r)
    out = r / "m.html"
    assert ar.main(["--base", "base", "--out", str(out)]) == 0
    page = out.read_text()
    assert "Edits a migration that exists at the base" in page and "a1_create.py" in page
    assert "The models changed and the branch adds no migration" in page


def test_two_revisions_after_one_make_two_heads():
    a = {"path": "x/a.py", "revision": "a", "down": "base", "message": "a", "ops": [],
         "noDowngrade": False}
    b = dict(a, path="x/b.py", revision="b")
    page = ar.render([a, b], [], [], None, Path("/r"))
    assert "two heads" in page
