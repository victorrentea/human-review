#!/usr/bin/env python3
"""An OpenAPI document for a Python Serverless service that has none — read off its code.

The API tab diffs two OpenAPI documents with an independent differ (oasdiff). Ludvig's
services keep no OpenAPI file: their contract is the `http` events of `serverless*.yml` and
what each handler reads from the request. So this writes the document both sides of the
diff are compared on, from the tree at a ref or from the working tree:

  * **operations** — every `http` / `httpApi` event (method and path), and one per route of
    a Powertools `APIGatewayRestResolver` behind a proxy event; the function name is the
    `operationId`, its description the summary;
  * **security** — `authorizer: aws_iam` is SigV4, any other authorizer a bearer token;
  * **path parameters** — from the path template (`{org_number}`, `{proxy+}`);
  * **query parameters** — every name the handler reads off the query string
    (`query_params.get("start_date")`, `queryStringParameters["x"]`), and the fields of a
    model built from it (`Params(**request.query_params)`);
  * **request body** — a model built from the body (`Input(**request.body)`,
    `Input.model_validate(body)`, `Input(**item)` for each item of it) as a schema, else
    the keys read off the parsed body (`body.get("userId")`);
  * **responses** — 200, and every status the handler can return
    (`return_error(404, "…")`, `{"statusCode": 201}`), with its message as description;
  * **schemas** — a project class used above (pydantic `BaseModel`, dataclass, TypedDict):
    its annotated fields, with a default or `Optional` meaning not required.

It reads what the handler function and the functions of its own module that it calls do;
it does not follow the request into services. The document is a measurement for a diff,
not documentation: both sides are written by this same file, so what changed is what the
code changed.

Usage:
    serverless-openapi.py --out after.yaml                         # the working tree
    serverless-openapi.py --base origin/develop --out before.yaml  # the merge-base
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _complexity():
    spec = importlib.util.spec_from_file_location("endpoint_complexity", HERE / "endpoint-complexity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _complexity()

QUERY_ATTRS = {"query_params", "queryStringParameters", "query_string_parameters",
               "multiValueQueryStringParameters", "query"}
BODY_ATTRS = {"body", "json_body", "json"}
PARSE_METHODS = {"parse_obj", "model_validate", "parse_raw", "model_validate_json", "from_dict"}
ERROR_FUNCS = {"return_error", "error_response", "make_error", "http_error"}

PY_TYPES = {"str": {"type": "string"}, "int": {"type": "integer"},
            "float": {"type": "number"}, "Decimal": {"type": "number"},
            "bool": {"type": "boolean"}, "dict": {"type": "object"}, "Dict": {"type": "object"},
            "Any": {}, "object": {}, "datetime": {"type": "string", "format": "date-time"},
            "date": {"type": "string", "format": "date"}, "UUID": {"type": "string", "format": "uuid"},
            "constr": {"type": "string"}, "conint": {"type": "integer"},
            "EmailStr": {"type": "string", "format": "email"}}


class Spec:
    def __init__(self, prog):
        self.prog = prog
        self.schemas: dict[str, dict] = {}

    # -- schemas from project classes --

    def schema_of(self, ann, mod: str) -> dict:
        """An annotation -> a JSON schema (a `$ref` for a project class)."""
        if ann is None:
            return {}
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return {}
        if isinstance(ann, ast.Constant) and ann.value is None:
            return {}
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            left, right = self.schema_of(ann.left, mod), self.schema_of(ann.right, mod)
            return {**(left or right), "nullable": True} if not (left and right) else left
        if isinstance(ann, ast.Subscript):
            head = ec.Program._dotted(ann.value) or ""
            last = head.rsplit(".", 1)[-1]
            args = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            if last in ("Optional",):
                return {**self.schema_of(args[0], mod), "nullable": True}
            if last in ("List", "list", "Sequence", "Set", "set", "Tuple", "tuple", "Iterable"):
                return {"type": "array", "items": self.schema_of(args[0], mod)}
            if last in ("Dict", "dict", "Mapping"):
                return {"type": "object",
                        "additionalProperties": self.schema_of(args[-1], mod) or True}
            if last == "Union":
                parts = [self.schema_of(a, mod) for a in args
                         if not (isinstance(a, ast.Constant) and a.value is None)]
                return parts[0] if len(parts) == 1 else {"oneOf": parts}
            if last == "Literal":
                return {"enum": [a.value for a in args if isinstance(a, ast.Constant)]}
            return {}
        if isinstance(ann, ast.Call):                    # constr(max_length=…)
            name = (ec.Program._dotted(ann.func) or "").rsplit(".", 1)[-1]
            return dict(PY_TYPES.get(name, {}))
        name = ec.Program._dotted(ann) or ""
        cls = self.prog._class_of(ann, mod)
        if cls:
            return self.ref(cls)
        return dict(PY_TYPES.get(name.rsplit(".", 1)[-1], {}))

    def ref(self, cls: str) -> dict:
        short = cls.rsplit(".", 1)[-1]
        if short not in self.schemas:
            self.schemas[short] = {}                     # a cycle sees the name, not a loop
            self.schemas[short] = self.class_schema(cls)
        return {"$ref": f"#/components/schemas/{short}"}

    def class_schema(self, cls: str) -> dict:
        c = self.prog.classes[cls]
        mod = c["module"]
        tree = self.prog.modules[mod]["tree"]
        node = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                     and f"{mod}.{n.name}" == cls), None)
        props, required = {}, []
        if node is None:
            return {"type": "object"}
        # The project's own bases first, so a subclass carries what it inherits.
        for b in node.bases:
            base = self.prog._class_of(b, mod)
            if base and base != cls:
                inherited = self.class_schema(base)
                props.update(inherited.get("properties", {}))
                required += inherited.get("required", [])
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                    and not stmt.target.id.startswith("_"):
                name = stmt.target.id
                if (ec.Program._dotted(stmt.annotation) or "").endswith("ClassVar"):
                    continue
                s = self.schema_of(stmt.annotation, mod)
                props[name] = s
                optional = s.get("nullable") or stmt.value is not None
                if not optional and name not in required:
                    required.append(name)
                elif optional and name in required:
                    required.remove(name)
        out = {"type": "object", "properties": props}
        if required:
            out["required"] = required
        return out

    # -- what one handler reads and returns --

    def read(self, func_keys: list[str]) -> dict:
        query: dict[str, dict] = {}
        body_props: dict[str, dict] = {}
        body_schema = None
        statuses: dict[str, str] = {}
        for key in func_keys:
            f = self.prog.funcs[key]
            mod = f.module
            bodies = set()                               # local names bound to the parsed body
            for sub in ast.walk(f.node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name) and self._is_body(sub.value):
                    bodies.add(sub.targets[0].id)
            # A field of the body bound to a name (`packages = body.get("packages", [])`), and
            # a loop over one (`for p in packages`, `[X(**p) for p in body["packages"]]`):
            # a model built from the loop variable is the type of that field's items.
            field_of: dict[str, str] = {}

            def body_field(expr) -> str | None:
                if isinstance(expr, ast.Name):
                    return field_of.get(expr.id)
                got = self._read_key(expr)
                if got and isinstance(got[0], ast.Name) and got[0].id in bodies:
                    return got[1]
                return None
            for sub in ast.walk(f.node):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 \
                        and isinstance(sub.targets[0], ast.Name):
                    fld = body_field(sub.value)
                    if fld:
                        field_of[sub.targets[0].id] = fld
            item_of: dict[str, str] = {}
            for sub in ast.walk(f.node):
                loops = [(sub.target, sub.iter)] if isinstance(sub, (ast.For, ast.AsyncFor)) else \
                    [(g.target, g.iter) for g in getattr(sub, "generators", [])]
                for target, it in loops:
                    fld = body_field(it)
                    if fld and isinstance(target, ast.Name):
                        item_of[target.id] = fld
            for sub in ast.walk(f.node):
                # query_params.get("x") / query_params["x"]
                key_name = self._read_key(sub)
                if key_name:
                    recv, name = key_name
                    if self._attr(recv) in QUERY_ATTRS or (isinstance(recv, ast.Name)
                                                           and recv.id in QUERY_ATTRS):
                        query.setdefault(name, {"type": "string"})
                    elif isinstance(recv, ast.Name) and recv.id in bodies:
                        body_props.setdefault(name, {})
                if isinstance(sub, ast.Call):
                    model, source = self._model_built(sub, mod, bodies)
                    if model and source == "query":
                        for name, s in self.class_schema(model).get("properties", {}).items():
                            query[name] = s if s.get("type") in ("string", "integer", "number",
                                                                 "boolean") else {"type": "string"}
                    elif model and source == "item" and isinstance(sub.keywords[0].value
                                                                   if sub.keywords else None,
                                                                   ast.Name) \
                            and sub.keywords[0].value.id in item_of:
                        body_props[item_of[sub.keywords[0].value.id]] = {
                            "type": "array", "items": self.ref(model)}
                    elif model and source in ("body", "item"):
                        ref = self.ref(model)
                        body_schema = ref if source == "body" else {"type": "array", "items": ref}
                    code = self._status(sub)
                    if code:
                        statuses.setdefault(code[0], code[1])
                if isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if isinstance(k, ast.Constant) and k.value == "statusCode" \
                                and isinstance(v, ast.Constant) and isinstance(v.value, int):
                            statuses.setdefault(str(v.value), "")
        if body_schema is None and body_props:
            body_schema = {"type": "object", "properties": body_props}
        return {"query": query, "body": body_schema, "statuses": statuses}

    @staticmethod
    def _attr(node) -> str | None:
        return node.attr if isinstance(node, ast.Attribute) else None

    def _is_body(self, value) -> bool:
        """`request.body`, `handler_request.json()`, `json.loads(event["body"])`."""
        if isinstance(value, ast.Attribute) and value.attr in BODY_ATTRS:
            return True
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Attribute) and value.func.attr in BODY_ATTRS:
                return True
            if (ec.Program._dotted(value.func) or "").endswith("loads") and value.args:
                a = value.args[0]
                return isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Constant) \
                    and a.slice.value == "body" or (isinstance(a, ast.Call) and isinstance(
                        a.func, ast.Attribute) and a.func.attr == "get" and a.args
                        and isinstance(a.args[0], ast.Constant) and a.args[0].value == "body")
        if isinstance(value, ast.BoolOp):
            return any(self._is_body(v) for v in value.values)
        return False

    @staticmethod
    def _read_key(sub) -> tuple | None:
        if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                and isinstance(sub.slice.value, str):
            return sub.value, sub.slice.value
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "get" and sub.args and isinstance(sub.args[0], ast.Constant) \
                and isinstance(sub.args[0].value, str):
            return sub.func.value, sub.args[0].value
        return None

    def _model_built(self, call: ast.Call, mod: str, bodies: set) -> tuple[str | None, str]:
        """`(project class, "query" | "body" | "item")` when the call builds a model from
        the request; `(None, "")` otherwise."""
        cls, src = None, None
        if isinstance(call.func, ast.Attribute) and call.func.attr in PARSE_METHODS:
            cls = self.prog._class_of(call.func.value, mod)
            src = call.args[0] if call.args else None
        else:
            cls = self.prog._class_of(call.func, mod)
            star = [k.value for k in call.keywords if k.arg is None]
            src = star[0] if star else None
        if not cls or src is None:
            return None, ""
        if self._attr(src) in QUERY_ATTRS:
            return cls, "query"
        if self._is_body(src) or (isinstance(src, ast.Name) and src.id in bodies):
            return cls, "body"
        if isinstance(src, ast.Name):                    # `Input(**item)` inside a loop
            return cls, "item"
        return None, ""

    @staticmethod
    def _status(call: ast.Call) -> tuple[str, str] | None:
        name = (ec.Program._dotted(call.func) or "").rsplit(".", 1)[-1]
        if name not in ERROR_FUNCS or not call.args:
            return None
        code = call.args[0]
        if not (isinstance(code, ast.Constant) and isinstance(code.value, int)):
            return None
        msg = call.args[1] if len(call.args) > 1 else None
        text = msg.value if isinstance(msg, ast.Constant) and isinstance(msg.value, str) else ""
        return str(code.value), text


def operation_id(name: str, verb: str, path: str, seen: set) -> str:
    base = name if name not in seen else f"{name}_{verb.lower()}_" + re.sub(r"\W+", "_", path).strip("_")
    seen.add(base)
    return base


def build(py: dict[str, str], sls: dict[str, str], title: str = "service") -> dict:
    prog = ec.Program(py)
    spec = Spec(prog)
    functions = ec.serverless_functions(sls)
    by_name = {}
    ops_seen: set[str] = set()
    paths: dict[str, dict] = {}
    for fn in functions:
        mod, name = ec.handler_ref(fn["handler"])
        m = prog.modules.get(mod)
        if not m:
            continue
        key = m.get("defs", {}).get(name)
        routes = m.get("routes") or []
        for ev in fn["events"]:
            kind = next(iter(ev))
            if kind not in ("http", "httpApi"):
                continue
            val = ev[kind]
            auth = val.get("authorizer") if isinstance(val, dict) else None
            ops = ([(v, p, k) for v, p, k in routes] if routes
                   else [(*ec.event_entry(fn, ev)[1:], key)])
            for verb, path, fkey in ops:
                if not fkey or fkey not in prog.funcs:
                    continue
                same_module = [k for k in prog.flow(fkey)
                               if prog.funcs[k].module == prog.funcs[fkey].module]
                read = spec.read(same_module)
                op = {"operationId": operation_id(fn["name"], verb, path, ops_seen),
                      "x-handler": fn["handler"]}
                params = [{"name": p.rstrip("+"), "in": "path", "required": True,
                           "schema": {"type": "string"}}
                          for p in re.findall(r"\{([^}]+)\}", path)]
                params += [{"name": q, "in": "query", "required": False, "schema": s}
                           for q, s in sorted(read["query"].items())]
                if params:
                    op["parameters"] = params
                if read["body"] and verb not in ("GET", "DELETE", "HEAD"):
                    op["requestBody"] = {"required": True, "content": {
                        "application/json": {"schema": read["body"]}}}
                responses = {"200": {"description": "OK"}}
                for code, text in sorted(read["statuses"].items()):
                    responses[code] = {"description": text or f"HTTP {code}"}
                op["responses"] = responses
                if auth == "aws_iam":
                    op["security"] = [{"sigv4": []}]
                elif auth:
                    op["security"] = [{"bearer": []}]
                for v in (verb.split("/") if verb != "ANY" else ["GET", "POST", "PUT", "DELETE"]):
                    paths.setdefault(path, {})[v.lower()] = op
        by_name[fn["name"]] = fn
    doc = {"openapi": "3.0.3",
           "info": {"title": title, "version": "derived",
                    "description": "Derived from serverless*.yml and the handlers by "
                                   "human-review's serverless-openapi.py — a measurement for a "
                                   "diff, not the service's documentation."},
           "paths": dict(sorted(paths.items())),
           "components": {"schemas": dict(sorted(spec.schemas.items())),
                          "securitySchemes": {
                              "sigv4": {"type": "apiKey", "in": "header", "name": "Authorization",
                                        "description": "AWS SigV4 (authorizer: aws_iam)"},
                              "bearer": {"type": "http", "scheme": "bearer"}}}}
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="describe the merge-base with this ref, not the working tree")
    ap.add_argument("--out", help="where to write the YAML (default: stdout)")
    ap.add_argument("--title", default=None, help="info.title (default: the service: name)")
    args = ap.parse_args(argv)
    py, sls = ec.read_tree(args.base)
    title = args.title
    if not title:
        m = re.search(r"^service:\s*([\w-]+)", sls.get("serverless.yml", ""), re.M)
        title = m.group(1) if m else Path.cwd().name
    doc = build(py, sls, title)
    import yaml
    body = yaml.safe_dump(json.loads(json.dumps(doc)), sort_keys=False, allow_unicode=True,
                          width=100)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body, encoding="utf-8")
        n = sum(len(v) for v in doc["paths"].values())
        print(f"[openapi] {n} operations, {len(doc['components']['schemas'])} schemas -> {args.out}",
              file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
