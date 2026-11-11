"""structure-diagrams.py: the Structure tab's Packages and Containers, from the source."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("structure_diagrams", HERE / "structure-diagrams.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

PY = {
    "src/__init__.py": "",
    "src/assembly.py": "from src.core.service import S\n",
    "src/core/__init__.py": "",
    "src/core/service.py": "from src.infrastructure.repo import Repo\nimport boto3\n",
    "src/infrastructure/repo.py": "from ..core import service\n",
    "src/handlers/h.py": "from src.core.service import S\nfrom src.infrastructure import repo\n",
    "lambdas/consumers/c.py": "from src.core.service import S\n",
}


def test_a_box_per_package_under_each_root_and_an_arrow_per_import_between_them():
    text = sd.packages(PY)
    boxes = [l for l in text.splitlines() if l.startswith("component")]
    assert boxes == [
        'component "lambdas.consumers" as p_lambdas_consumers <<lambdas/consumers>>',
        'component "src" as p_src <<src>>',
        'component "src.core" as p_src_core <<src/core>>',
        'component "src.handlers" as p_src_handlers <<src/handlers>>',
        'component "src.infrastructure" as p_src_infrastructure <<src/infrastructure>>',
    ]
    arrows = {l for l in text.splitlines() if "..>" in l}
    assert arrows == {"p_lambdas_consumers ..> p_src_core", "p_src ..> p_src_core",
                      "p_src_core ..> p_src_infrastructure", "p_src_infrastructure ..> p_src_core",
                      "p_src_handlers ..> p_src_core", "p_src_handlers ..> p_src_infrastructure"}


SLS = {"serverless.yml": """
service: things
functions:
  api:
    handler: src.handlers.h.handler
    events:
      - http:
          method: GET
          path: /x
  consume:
    handler: lambdas/consumers/c.handler
    events:
      - sqs:
          arn: !GetAtt ThingQueue.Arn
  imported:
    handler: lambdas/consumers/c.other
    events:
      - sqs:
          arn: !ImportValue ${self:provider.stage}-things-form-queue-arn
  nightly:
    handler: lambdas/consumers/c.nightly
    events:
      - schedule:
          rate: rate(1 day)
resources:
  Resources:
    ThingQueue:
      Type: AWS::SQS::Queue
      Properties:
        RedrivePolicy:
          deadLetterTargetArn: !GetAtt ThingDLQ.Arn
    ThingDLQ:
      Type: AWS::SQS::Queue
    ThingRule:
      Type: AWS::Events::Rule
      Properties:
        Targets:
          - Arn: !GetAtt ThingQueue.Arn
    Things:
      Type: AWS::DynamoDB::Table
"""}


def test_containers_are_stacks_triggers_and_what_they_declare_or_import():
    text = sd.containers(SLS)
    assert 'component "HTTP API (1 endpoints)" as api_things' in text
    for fn in ("consume", "imported", "nightly"):
        assert f'component "{fn}" as f_things_{fn}' in text
    assert 'queue "ThingQueue\\n<size:9>SQS</size>" as r_things_ThingQueue' in text
    assert 'database "Things\\n<size:9>DynamoDB</size>" as r_things_Things' in text
    assert 'queue "things-form-queue\\n<size:9>SQS, imported</size>" as x_things_form_queue' in text
    edges = {l for l in text.splitlines() if "-->" in l}
    assert edges == {
        "r_things_ThingQueue --> f_things_consume",
        "x_things_form_queue --> f_things_imported",
        "x_Scheduler --> f_things_nightly : rate(1 day)",
        "r_things_ThingQueue --> r_things_ThingDLQ : DLQ",
        "x_default_event_bus --> r_things_ThingQueue : rule",
    }
