"""serverless-openapi.py: the API tab's two sides, for a service that keeps no spec file."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("serverless_openapi", HERE / "serverless-openapi.py")
so = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(so)

SLS = """
service: activities
functions:
  getThing:
    handler: src.handlers.get_thing.handler
    events:
      - http:
          method: GET
          path: /customers/{org_number}/things
          authorizer: aws_iam
  addThings:
    handler: src.handlers.add_things.handler
    events:
      - http:
          method: POST
          path: customers/{customer_id}/things
          authorizer:
            name: azure
  routed:
    handler: src.handlers.routed.handler
    events:
      - http:
          method: ANY
          path: /r/{proxy+}
  consume:
    handler: src.handlers.get_thing.handler
    events:
      - sqs:
          arn: x
"""

PY = {
    "src/__init__.py": "",
    "src/dto.py": (
        "from typing import Optional, List\nfrom pydantic import BaseModel\n\n\n"
        "class Base(BaseModel):\n    created_by: str\n\n\n"
        "class ThingInput(Base):\n    name: str\n    tags: List[str] = []\n"
        "    due: Optional[date] = None\n\n\n"
        "class ThingQuery(BaseModel):\n    page: int\n    q: Optional[str] = None\n"),
    "src/handlers/get_thing.py": (
        "from ludvig_shared.tools.event_proxy_responses import return_error, return_json_dump\n"
        "from src.dto import ThingQuery\n\n\n"
        "def handler(event, _):\n"
        "    query = ThingQuery(**event.query_params)\n"
        "    if event.query_params.get('include_deleted'):\n"
        "        return return_error(404, 'No things')\n"
        "    return return_json_dump(_load(query))\n\n\n"
        "def _load(q):\n    return return_error(409, 'Busy')\n"),
    "src/handlers/add_things.py": (
        "from src.dto import ThingInput\n\n\n"
        "def handler(request, _):\n"
        "    body = request.body\n"
        "    things = body.get('things', [])\n"
        "    inputs = [ThingInput(**t) for t in things]\n"
        "    user = body.get('userId')\n"
        "    return {'statusCode': 201, 'body': ''}\n"),
    "src/handlers/routed.py": (
        "from aws_lambda_powertools.event_handler import APIGatewayRestResolver\n"
        "app = APIGatewayRestResolver()\n\n\n"
        "@app.get('/r/a')\ndef a():\n    return 1\n\n\n"
        "def handler(event, context):\n    return app.resolve(event, context)\n"),
}


def _doc():
    return so.build(PY, {"serverless.yml": SLS}, "svc")


def test_every_http_event_is_an_operation_and_a_resolver_route_replaces_its_proxy():
    paths = _doc()["paths"]
    assert set(paths) == {"/customers/{org_number}/things", "/customers/{customer_id}/things", "/r/a"}
    assert set(paths["/customers/{org_number}/things"]) == {"get"}
    assert paths["/r/a"]["get"]["operationId"] == "routed"


def test_query_parameters_come_from_reads_and_from_a_model_built_from_the_query():
    op = _doc()["paths"]["/customers/{org_number}/things"]["get"]
    names = [(p["name"], p["in"]) for p in op["parameters"]]
    assert names == [("org_number", "path"), ("include_deleted", "query"),
                     ("page", "query"), ("q", "query")]
    page = next(p for p in op["parameters"] if p["name"] == "page")
    assert page["schema"] == {"type": "integer"}


def test_statuses_include_those_of_helpers_in_the_same_module():
    op = _doc()["paths"]["/customers/{org_number}/things"]["get"]
    assert op["responses"]["404"]["description"] == "No things"
    assert op["responses"]["409"]["description"] == "Busy"
    assert op["security"] == [{"sigv4": []}]


def test_a_body_field_of_models_and_a_plain_key_make_the_request_schema():
    doc = _doc()
    op = doc["paths"]["/customers/{customer_id}/things"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["things"] == {
        "type": "array", "items": {"$ref": "#/components/schemas/ThingInput"}}
    assert "userId" in schema["properties"]
    assert "201" in op["responses"] and op["security"] == [{"bearer": []}]


def test_a_model_s_schema_carries_its_base_and_knows_what_is_optional():
    s = _doc()["components"]["schemas"]["ThingInput"]
    assert list(s["properties"]) == ["created_by", "name", "tags", "due"]
    assert s["required"] == ["created_by", "name"]
    assert s["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert s["properties"]["due"]["nullable"] is True
