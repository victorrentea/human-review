"""sqlalchemy-erd.py and the generated-diagram path of puml-diff.sh: the Data tab's `DB`
delta for a service that commits no diagram."""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("sqlalchemy_erd", HERE / "sqlalchemy-erd.py")
erd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(erd)

MODELS = '''
from sqlalchemy import Column, String, Integer, ForeignKey, Enum, Table
from sqlalchemy.orm import mapped_column, relationship


class Customer(Base):
    __tablename__ = "customers"
    org_number = Column(String, primary_key=True)
    status = Column(Enum(Status), nullable=True)


class Delivery(Base):
    __tablename__ = "deliveries"
    uuid = Column(String, primary_key=True)
    org_number = Column(String, ForeignKey("customers.org_number"), nullable=False)
    note = mapped_column("delivery_note", String)
    customer = relationship("Customer")


links = Table("links", Base.metadata,
              Column("delivery_uuid", String, ForeignKey("deliveries.uuid")))
'''


def test_tables_columns_and_foreign_keys_are_read_off_the_source():
    model = erd.tables({"src/models.py": MODELS})
    assert set(model) == {"customers", "deliveries", "links"}
    members = [c.member() for c in model["deliveries"]["columns"]]
    assert members == ["uuid : String <<PK>>", "org_number : String <<FK>>",
                       "delivery_note? : String"]
    assert model["customers"]["columns"][1].member() == "status? : Enum(Status)"
    text = erd.puml(model)
    assert "t_deliveries }o--|| t_customers : org_number" in text
    assert "t_links }o--o| t_deliveries : delivery_uuid" in text
    assert '[[src://src/models.py:6{Click to open in editor}]]' in text


def test_a_column_the_branch_adds_is_drawn_green_on_the_db_delta(tmp_path):
    repo = tmp_path / "svc"
    (repo / "src").mkdir(parents=True)
    run = lambda *a, **kw: subprocess.run(a, cwd=repo, check=True, capture_output=True,
                                          text=True, **kw)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "src" / "models.py").write_text(MODELS)
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "tag", "base")
    (repo / "src" / "models.py").write_text(MODELS.replace(
        '    status = Column(Enum(Status), nullable=True)\n',
        '    status = Column(Enum(Status), nullable=True)\n    vat_free = Column(Boolean)\n'))
    gen = repo / ".human-review" / "generated-puml"
    gen.mkdir(parents=True)
    env = dict(os.environ, PATH=os.environ["PATH"])
    run("python3", str(HERE / "sqlalchemy-erd.py"), "--out", str(gen / "DB.puml"), env=env)
    run("python3", str(HERE / "sqlalchemy-erd.py"), "--base", "base", "--out",
        str(gen / "DB.base.puml"), env=env)
    run(str(HERE / "puml-diff.sh"), "base", ".human-review/assets/diagrams",
        env=dict(env, PUML_GENERATED=".human-review/generated-puml"))
    rows = (repo / ".human-review/assets/diagrams/MANIFEST.tsv").read_text().splitlines()[1:]
    assert [r.split("\t")[:4] for r in rows] == [
        ["DB", ".human-review/generated-puml/DB.puml", "structural", "modified"]]
    delta = (repo / ".human-review/assets/diagrams/DB.diff.puml").read_text()
    assert "<color:#2E7D32>vat_free? : Boolean</color>" in delta
