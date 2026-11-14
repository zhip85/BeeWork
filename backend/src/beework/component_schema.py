"""Component schemas (T-043, CHN-045): parse a component's data schema and validate a payload against it.

Two notations are accepted, both reduced to the same small tree:

* the simplified one of the PRD — an object whose keys are field names (a trailing ``?`` makes the field optional) and whose values
  are type expressions: ``string``, ``number``, ``integer``, ``boolean``, ``any``, ``T[]``, ``{ label: string; url?: string }``, or a nested
  object / one-element list written as JSON;
* standard JSON Schema (``type`` / ``properties`` / ``required`` / ``items`` / ``enum``), the subset a card needs.

``validate_component_payload`` is what T-059 uses on a model's output and what the publish dialog's trial run uses on the sample data.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

PRIMITIVES = ("string", "number", "integer", "boolean", "any")
MAX_DEPTH = 8
MAX_FIELDS = 60


class SchemaError(ValueError):
    """The schema text cannot be understood. ``path`` points at the field."""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(f"{path}：{message}" if path else message)
        self.path = path


@dataclass
class Node:
    kind: str  # string | number | integer | boolean | any | array | object
    items: "Node | None" = None
    fields: dict[str, "Node"] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)
    enum: list[Any] | None = None


# -- simplified type expressions ---------------------------------------------------------------------------


class _Expr:
    """A recursive-descent parser for ``T``, ``T[]`` and ``{ a: T; b?: T }``."""

    def __init__(self, text: str, path: str) -> None:
        self.text, self.pos, self.path = text, 0, path

    def fail(self, message: str) -> SchemaError:
        return SchemaError(f"{message}（“{self.text}”）", self.path)

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def parse(self, depth: int = 0) -> Node:
        if depth > MAX_DEPTH:
            raise self.fail("嵌套层级过深")
        self.skip()
        if self.peek() == "{":
            node = self.object(depth)
        else:
            match = re.compile(r"[A-Za-z]+").match(self.text, self.pos)
            if not match or match.group(0) not in PRIMITIVES:
                raise self.fail(f"不支持的类型，可用 {'、'.join(PRIMITIVES)}、T[]、{{ a: T }}")
            self.pos = match.end()
            node = Node(match.group(0))
        while self.peek() == "[":
            self.pos += 1
            if self.peek() != "]":
                raise self.fail("数组类型应写成 T[]")
            self.pos += 1
            node = Node("array", items=node)
        return node

    def object(self, depth: int) -> Node:
        self.pos += 1  # {
        node = Node("object")
        while True:
            if self.peek() == "}":
                self.pos += 1
                return node
            match = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\?)?\s*:").match(self.text, self.skip() or self.pos)
            if not match:
                raise self.fail("对象字段应写成 name: type")
            name = match.group(1)
            if name in node.fields:
                raise self.fail(f"字段 {name} 重复")
            self.pos = match.end()
            node.fields[name] = self.parse(depth + 1)
            if not match.group(2):
                node.required.add(name)
            if self.peek() == ";" or self.peek() == ",":
                self.pos += 1
            elif self.peek() not in ("}",):
                raise self.fail("字段之间用 ; 分隔")


def _from_simplified(value: Any, path: str, depth: int = 0) -> Node:
    if depth > MAX_DEPTH:
        raise SchemaError("嵌套层级过深", path)
    if isinstance(value, str):
        parser = _Expr(value, path)
        node = parser.parse()
        if parser.peek():
            raise parser.fail("类型之后有多余内容")
        return node
    if isinstance(value, list):
        if len(value) != 1:
            raise SchemaError("数组类型只写一个元素的类型，如 [\"string\"]", path)
        return Node("array", items=_from_simplified(value[0], f"{path}[]", depth + 1))
    if isinstance(value, dict):
        node = Node("object")
        if len(value) > MAX_FIELDS:
            raise SchemaError(f"字段不能超过 {MAX_FIELDS} 个", path)
        for key, inner in value.items():
            name = key[:-1] if key.endswith("?") else key
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise SchemaError(f"字段名“{key}”只能含字母、数字和下划线，且不以数字开头", path)
            if name in node.fields:
                raise SchemaError(f"字段 {name} 重复", path)
            node.fields[name] = _from_simplified(inner, f"{path}.{name}" if path else name, depth + 1)
            if not key.endswith("?"):
                node.required.add(name)
        return node
    raise SchemaError("类型应写成字符串、对象或单元素数组", path)


# -- standard JSON Schema -------------------------------------------------------------------------------------


def _from_json_schema(value: Any, path: str, depth: int = 0) -> Node:
    if depth > MAX_DEPTH:
        raise SchemaError("嵌套层级过深", path)
    if not isinstance(value, dict):
        raise SchemaError("JSON Schema 的每一层都应是对象", path)
    kind = value.get("type")
    if kind == "object" or (kind is None and "properties" in value):
        node = Node("object")
        properties = value.get("properties") or {}
        if not isinstance(properties, dict) or len(properties) > MAX_FIELDS:
            raise SchemaError(f"properties 应是对象且不超过 {MAX_FIELDS} 个字段", path)
        for name, inner in properties.items():
            node.fields[name] = _from_json_schema(inner, f"{path}.{name}" if path else name, depth + 1)
        required = value.get("required") or []
        if not isinstance(required, list) or any(r not in node.fields for r in required):
            raise SchemaError("required 只能列出 properties 里有的字段", path)
        node.required = set(required)
        return node
    if kind == "array":
        return Node("array", items=_from_json_schema(value.get("items") or {"type": "any"}, f"{path}[]", depth + 1))
    if kind in ("string", "number", "integer", "boolean"):
        node = Node(kind)
        if "enum" in value:
            if not isinstance(value["enum"], list) or not value["enum"]:
                raise SchemaError("enum 应是非空数组", path)
            node.enum = list(value["enum"])
        return node
    if kind is None or kind == "any":
        return Node("any")
    raise SchemaError(f"不支持的 type：{kind}", path)


def parse_component_schema(raw: Any) -> Node:
    """``raw`` is the schema as stored (text or already-decoded JSON). The result is the top-level object node."""
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"JSON 格式有误：{exc.msg}（第 {exc.lineno} 行）") from exc
    if not isinstance(value, dict):
        raise SchemaError("数据架构应是一个 JSON 对象")
    if value.get("type") == "object" or "properties" in value:
        node = _from_json_schema(value, "")
    else:
        node = _from_simplified(value, "")
    if node.kind != "object":
        raise SchemaError("数据架构的顶层应是对象")
    return node


# -- validation ---------------------------------------------------------------------------------------------


def _matches(kind: str, value: Any) -> bool:
    if kind == "any":
        return True
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _check(node: Node, value: Any, path: str, errors: list[str]) -> None:
    where = path or "（根）"
    if node.kind == "object":
        if not isinstance(value, dict):
            errors.append(f"{where} 应是对象")
            return
        for name in sorted(node.required - set(value)):
            errors.append(f"{path + '.' if path else ''}{name} 缺少必填字段")
        for name, inner in node.fields.items():
            if name in value and value[name] is not None:
                _check(inner, value[name], f"{path}.{name}" if path else name, errors)
            elif name in value and name in node.required:
                errors.append(f"{path + '.' if path else ''}{name} 不能为空")
        return
    if node.kind == "array":
        if not isinstance(value, list):
            errors.append(f"{where} 应是数组")
            return
        for index, item in enumerate(value):
            _check(node.items or Node("any"), item, f"{path}[{index}]", errors)
        return
    if not _matches(node.kind, value):
        errors.append(f"{where} 应是 {node.kind}")
    elif node.enum is not None and value not in node.enum:
        errors.append(f"{where} 应是 {' / '.join(map(str, node.enum))} 之一")


def validate_component_payload(schema: Node | Any, payload: Any) -> list[str]:
    """Problems of ``payload`` against the schema, as readable messages; empty means it is valid. Unknown extra fields are allowed."""
    node = schema if isinstance(schema, Node) else parse_component_schema(schema)
    errors: list[str] = []
    _check(node, payload, "", errors)
    return errors


def compact_schema(raw: Any) -> str:
    """The schema as one line of JSON, for the prompt that tells the model what to output (OC-073)."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
