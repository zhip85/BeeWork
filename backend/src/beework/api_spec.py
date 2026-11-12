"""Turning an API description into tools (T-073, CAP-040).

* **OpenAPI 3.0 / 3.1** (JSON or YAML): one tool per path + method. The name is the ``operationId`` (missing: method + path,
  normalised); the arguments come from ``parameters`` and ``requestBody``. Only references *inside the document*
  (``#/...``) are followed — a reference to another file or URL is refused, never fetched (SSRF).
* **GraphQL SDL**: one tool per field of ``Query`` / ``Mutation`` (``Mutation`` fields are writes).

A tool carries a ``binding`` that says how a call is turned into an HTTP request; the executor's provider reads it. Parsing
failures raise :class:`SpecError` with the line and the reason (CAP-040).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

MAX_SPEC_BYTES = 1_000_000
MAX_TOOLS = 200
MAX_REF_DEPTH = 6
MAX_INLINE_NODES = 200_000  # per document: a small spec of schemas that reference each other must not expand without bound
MAX_TOOLS_JSON_BYTES = 4_000_000
HTTP_METHODS = ("get", "put", "post", "delete", "patch")
SAFE_METHODS = {"get"}
NAME_OK = re.compile(r"[^A-Za-z0-9_-]+")
TOOL_NAME_MAX = 64


class SpecError(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self) -> str:
        return f"第 {self.line} 行：{self.message}" if self.line else self.message


@dataclass
class ApiTool:
    name: str
    description: str
    writes: bool
    parameters: dict[str, Any]
    binding: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "writes": self.writes,
            "input_schema": self.parameters, "binding": self.binding,
        }


def _line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return None


def _clean_name(raw: str) -> str:
    name = re.sub(r"_+", "_", NAME_OK.sub("_", raw)).strip("_")
    return (name or "tool")[:TOOL_NAME_MAX]


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate in taken:
        suffix = f"_{n}"
        candidate = f"{name[: TOOL_NAME_MAX - len(suffix)]}{suffix}"
        n += 1
    taken.add(candidate)
    return candidate


def kind_of(text: str) -> str:
    """``openapi`` or ``graphql``: what the pasted text looks like (used when the form does not say)."""
    head = text.lstrip()[:2000]
    if head.startswith(("{", "openapi", "swagger", '"openapi"', "'openapi'")) or re.search(r"^\s*openapi\s*:", text, re.M):
        return "openapi"
    return "graphql"


# ---------------------------------------------------------------------------
# OpenAPI.
# ---------------------------------------------------------------------------


def _load_document(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError("规范文件超过 1 MB")
    stripped = text.lstrip()
    try:
        if stripped.startswith("{"):
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"JSON 格式错误：{exc.msg}", exc.lineno) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise SpecError(f"YAML 格式错误：{getattr(exc, 'problem', None) or '无法解析'}", mark.line + 1 if mark else None) from exc
    if not isinstance(data, dict):
        raise SpecError("规范的根必须是一个对象")
    return data


class _Resolver:
    """Follows ``$ref`` inside the document only and inlines it (with a depth limit and a cycle guard)."""

    def __init__(self, document: dict[str, Any], text: str) -> None:
        self.document, self.text = document, text
        self.nodes = 0

    def lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise SpecError(f"不允许引用外部文件或地址：{ref}", _line_of(self.text, ref))
        node: Any = self.document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SpecError(f"找不到引用：{ref}", _line_of(self.text, ref))
            node = node[part]
        return node

    def inline(self, node: Any, seen: tuple[str, ...] = (), depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_INLINE_NODES:
            raise SpecError("规范展开后过大（引用嵌套过深或过多），请精简或拆分规范")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                target = self.lookup(ref)  # raises for an external reference even when nested deeper
                if ref in seen or depth >= MAX_REF_DEPTH:
                    return {"type": "object", "description": "（循环或过深的引用已省略）"}
                return self.inline(target, (*seen, ref), depth + 1)
            return {key: self.inline(value, seen, depth) for key, value in node.items() if key != "$ref"}
        if isinstance(node, list):
            return [self.inline(item, seen, depth) for item in node]
        return node


def _schema_of(resolver: _Resolver, schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    inlined = resolver.inline(schema)
    return inlined if isinstance(inlined, dict) else {}


def parse_openapi(text: str) -> list[ApiTool]:
    document = _load_document(text)
    version = str(document.get("openapi") or "")
    if not version.startswith(("3.0", "3.1")):
        raise SpecError("只支持 OpenAPI 3.0 / 3.1（缺少 openapi 字段，或这是 Swagger 2.0）", _line_of(text, "swagger") or 1)
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecError("规范里没有 paths", _line_of(text, "paths"))
    resolver = _Resolver(document, text)
    tools: list[ApiTool] = []
    taken: set[str] = set()
    for path, item in paths.items():
        if isinstance(item, dict) and isinstance(item.get("$ref"), str):
            item = resolver.lookup(item["$ref"])  # a path item reference; only the parts a tool needs are inlined below
        if not isinstance(item, dict):
            raise SpecError(f"路径 {path} 的定义必须是对象", _line_of(text, str(path)))
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            tools.append(_openapi_tool(resolver, text, taken, path, method, operation, shared))
            if len(tools) > MAX_TOOLS:
                raise SpecError(f"接口数量超过上限 {MAX_TOOLS}，请精简规范")
    if not tools:
        raise SpecError("没有可用的接口（path 下需要 get / post / put / patch / delete）", _line_of(text, "paths"))
    if len(json.dumps([t.as_dict() for t in tools], ensure_ascii=False)) > MAX_TOOLS_JSON_BYTES:
        raise SpecError("解析出的工具定义过大，请精简规范")
    return tools


def _openapi_tool(resolver: _Resolver, text: str, taken: set[str], path: str, method: str, operation: dict[str, Any], shared: list) -> ApiTool:
    # Only what a tool needs is read and inlined — never responses, callbacks or examples.
    raw_name = str(operation.get("operationId") or f"{method}_{path}")
    name = _unique(_clean_name(raw_name), taken)
    description = str(operation.get("summary") or operation.get("description") or f"{method.upper()} {path}")[:300]
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for parameter in [*shared, *(operation.get("parameters") if isinstance(operation.get("parameters"), list) else [])]:
        parameter = resolver.inline(parameter)
        if not isinstance(parameter, dict) or not parameter.get("name") or parameter.get("in") not in ("path", "query", "header"):
            continue  # cookies and malformed entries are not offered to the model
        merged[(str(parameter["in"]), str(parameter["name"]))] = parameter
    properties: dict[str, Any] = {}
    required: list[str] = []
    binding: dict[str, Any] = {"type": "rest", "method": method.upper(), "path": path, "path_params": [], "query_params": [], "header_params": [], "body": False}
    for (location, pname), parameter in merged.items():
        schema = _schema_of(resolver, parameter.get("schema")) or {"type": "string"}
        if parameter.get("description") and "description" not in schema:
            schema = {**schema, "description": str(parameter["description"])[:200]}
        if pname in properties:  # the same name in two places: the second one gets its location as a prefix
            pname_key = f"{location}_{pname}"
        else:
            pname_key = pname
        properties[pname_key] = schema
        binding[f"{location}_params"].append({"name": pname, "arg": pname_key})
        if parameter.get("required") or location == "path":
            required.append(pname_key)
    body = resolver.inline(operation.get("requestBody")) if isinstance(operation.get("requestBody"), dict) else None
    if isinstance(body, dict):
        content = body.get("content") if isinstance(body.get("content"), dict) else {}
        media = content.get("application/json") or next(iter(content.values()), None)
        if isinstance(media, dict):
            schema = _schema_of(resolver, media.get("schema")) or {"type": "object"}
            body_arg = "request_body" if "body" in properties else "body"  # a parameter may already be called "body"
            properties[body_arg] = {**schema, "description": schema.get("description") or "请求体（JSON）"}
            binding["body"], binding["body_arg"] = True, body_arg
            if body.get("required"):
                required.append(body_arg)
    parameters = {"type": "object", "properties": properties, "required": required}
    return ApiTool(name=name, description=description, writes=method not in SAFE_METHODS, parameters=parameters, binding=binding)


# ---------------------------------------------------------------------------
# GraphQL SDL.
# ---------------------------------------------------------------------------

SCALARS = {"String": "string", "ID": "string", "Int": "integer", "Float": "number", "Boolean": "boolean"}
TOKEN = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|#[^\n]*|[A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?|[{}()\[\]:!=@$|&,]|\.\.\.')


def _tokens(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace() or text[pos] == ",":
            pos += 1
            continue
        match = TOKEN.match(text, pos)
        if match is None:
            raise SpecError(f"无法识别的字符 {text[pos]!r}", text.count("\n", 0, pos) + 1)
        value = match.group()
        pos = match.end()
        if value.startswith("#"):
            continue
        out.append((value, text.count("\n", 0, match.start()) + 1))
    return out


class _Sdl:
    def __init__(self, text: str) -> None:
        self.t = _tokens(text)
        self.i = 0
        self.objects: dict[str, list[dict[str, Any]]] = {}  # type name -> its fields
        self.inputs: dict[str, dict[str, Any]] = {}
        self.enums: dict[str, list[str]] = {}
        self.unions: set[str] = set()
        self.roots = {"query": "Query", "mutation": "Mutation"}

    def peek(self, offset: int = 0) -> str | None:
        return self.t[self.i + offset][0] if self.i + offset < len(self.t) else None

    def line(self) -> int | None:
        return self.t[min(self.i, len(self.t) - 1)][1] if self.t else None

    def take(self, expected: str | None = None) -> str:
        if self.i >= len(self.t):
            raise SpecError("规范意外结束", self.t[-1][1] if self.t else None)
        value = self.t[self.i][0]
        if expected is not None and value != expected:
            raise SpecError(f"期望 {expected}，实际是 {value}", self.t[self.i][1])
        self.i += 1
        return value

    def skip_description(self) -> None:
        while (token := self.peek()) is not None and (token.startswith('"')):
            self.i += 1

    def skip_directives(self) -> None:
        while self.peek() == "@":
            self.take("@")
            self.take()
            if self.peek() == "(":
                self.skip_group("(", ")")

    def skip_group(self, open_: str, close: str) -> None:
        depth = 0
        while self.i < len(self.t):
            token = self.take()
            depth += token == open_
            depth -= token == close
            if depth == 0:
                return
        raise SpecError(f"缺少 {close}", self.t[-1][1])

    def type_ref(self) -> str:
        """A type reference such as ``[Int!]!`` as the source text."""
        if self.peek() == "[":
            self.take("[")
            inner = self.type_ref()
            self.take("]")
            text = f"[{inner}]"
        else:
            text = self.take()
        if self.peek() == "!":
            self.take("!")
            text += "!"
        return text

    def value(self) -> None:
        token = self.take()
        if token in ("[", "{"):
            self.i -= 1
            self.skip_group(token, "]" if token == "[" else "}")

    def arguments(self) -> list[dict[str, Any]]:
        args: list[dict[str, Any]] = []
        self.take("(")
        while self.peek() != ")":
            self.skip_description()
            name = self.take()
            self.take(":")
            type_text = self.type_ref()
            if self.peek() == "=":
                self.take("=")
                self.value()
            self.skip_directives()
            args.append({"name": name, "type": type_text})
        self.take(")")
        return args

    def fields(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        self.take("{")
        while self.peek() != "}":
            if self.peek() is None:
                raise SpecError("缺少 }", self.t[-1][1])
            description = ""
            while (token := self.peek()) is not None and token.startswith('"'):
                description = token.strip('"').strip()
                self.i += 1
            name = self.take()
            args = self.arguments() if self.peek() == "(" else []
            type_text = "String"
            if self.peek() == ":":
                self.take(":")
                type_text = self.type_ref()
            self.skip_directives()
            out.append({"name": name, "args": args, "type": type_text, "description": description})
        self.take("}")
        return out

    def parse(self) -> None:
        while self.i < len(self.t):
            self.skip_description()
            keyword = self.take()
            if keyword == "extend":
                keyword = self.take()
            if keyword == "schema":
                self.skip_directives()
                self.take("{")
                while self.peek() != "}":
                    op = self.take()
                    self.take(":")
                    self.roots[op] = self.take()
                self.take("}")
            elif keyword in ("type", "interface"):
                name = self.take()
                if self.peek() == "implements":
                    self.take()
                    while self.peek() not in ("{", "@", None):
                        self.take()
                self.skip_directives()
                if self.peek() == "{":
                    self.objects.setdefault(name, []).extend(self.fields())
            elif keyword == "input":
                name = self.take()
                self.skip_directives()
                self.inputs[name] = {f["name"]: f["type"] for f in self.fields()}
            elif keyword == "enum":
                name = self.take()
                self.skip_directives()
                self.take("{")
                values: list[str] = []
                while self.peek() != "}":
                    self.skip_description()
                    values.append(self.take())
                    self.skip_directives()
                self.take("}")
                self.enums[name] = values
            elif keyword == "directive":  # directive @name(args) [repeatable] on LOCATION | LOCATION: accepted and ignored
                self.take("@")
                self.take()
                if self.peek() == "(":
                    self.skip_group("(", ")")
                if self.peek() == "repeatable":
                    self.take()
                self.take("on")
                if self.peek() == "|":
                    self.take("|")
                self.take()
                while self.peek() == "|":
                    self.take("|")
                    self.take()
            elif keyword in ("scalar", "union", "fragment"):
                if keyword == "union":
                    self.unions.add(self.take())
                    self.skip_directives()
                    if self.peek() == "=":
                        self.take("=")
                        self.take()
                        while self.peek() == "|":
                            self.take("|")
                            self.take()
                elif keyword == "scalar":
                    self.take()
                    self.skip_directives()
                else:
                    raise SpecError(f"暂不支持 {keyword} 定义", self.t[self.i - 1][1])
            else:
                raise SpecError(f"无法识别的定义 {keyword!r}", self.t[self.i - 1][1])


def _base_type(type_text: str) -> str:
    return type_text.replace("[", "").replace("]", "").replace("!", "")


def _json_schema(sdl: _Sdl, type_text: str, depth: int = 0) -> dict[str, Any]:
    text = type_text.rstrip("!")
    if text.startswith("["):
        return {"type": "array", "items": _json_schema(sdl, text[1:-1], depth)}
    if text in SCALARS:
        return {"type": SCALARS[text]}
    if text in sdl.enums:
        return {"type": "string", "enum": sdl.enums[text]}
    if text in sdl.inputs and depth < 4:
        fields = sdl.inputs[text]
        return {
            "type": "object", "properties": {name: _json_schema(sdl, ftype, depth + 1) for name, ftype in fields.items()},
            "required": [name for name, ftype in fields.items() if ftype.endswith("!")],
        }
    return {"type": "string"}  # a custom scalar (DateTime, …) travels as a string


def _selection(sdl: _Sdl, type_text: str) -> str:
    """The scalar fields of the return type, so a call that returns an object returns something readable."""
    name = _base_type(type_text)
    if name in SCALARS or name in sdl.enums or name not in sdl.objects:
        return ""  # a scalar / enum / union return needs no selection (a union would need fragments: not supported)
    def leaf(fld: dict[str, Any]) -> bool:  # a scalar, an enum or a custom scalar: anything that is not an object / union
        base = _base_type(fld["type"])
        return not fld["args"] and base not in sdl.objects and base not in sdl.unions

    scalars = [f["name"] for f in sdl.objects[name] if leaf(f)]
    return " { " + " ".join(scalars[:30] or ["__typename"]) + " }"


def parse_graphql(text: str) -> list[ApiTool]:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError("规范文件超过 1 MB")
    sdl = _Sdl(text)
    sdl.parse()
    tools: list[ApiTool] = []
    taken: set[str] = set()
    for operation in ("query", "mutation"):
        root = sdl.roots[operation]
        for fld in sdl.objects.get(root, []):
            if fld["name"].startswith("__"):
                continue
            properties = {arg["name"]: _json_schema(sdl, arg["type"]) for arg in fld["args"]}
            required = [arg["name"] for arg in fld["args"] if arg["type"].endswith("!")]
            binding = {
                "type": "graphql", "operation": operation, "field": fld["name"],
                "args": {arg["name"]: arg["type"] for arg in fld["args"]}, "selection": _selection(sdl, fld["type"]),
            }
            tools.append(
                ApiTool(
                    name=_unique(_clean_name(fld["name"]), taken), description=(fld["description"] or f"GraphQL {operation} {fld['name']}")[:300],
                    writes=operation == "mutation", parameters={"type": "object", "properties": properties, "required": required},
                    binding=binding,
                )
            )
    if not tools:
        raise SpecError("没有找到 Query 或 Mutation 的字段", 1)
    if len(tools) > MAX_TOOLS:
        raise SpecError(f"接口数量超过上限 {MAX_TOOLS}，请精简规范")
    return tools


def parse_spec(text: str, api_type: str) -> list[ApiTool]:
    """``api_type`` is ``rest`` (an OpenAPI document) or ``graphql`` (SDL)."""
    if not text.strip():
        raise SpecError("规范内容为空", 1)
    return parse_graphql(text) if api_type == "graphql" else parse_openapi(text)
