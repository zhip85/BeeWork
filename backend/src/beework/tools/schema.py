"""A small JSON Schema validator for tool arguments (the subset tool definitions use).

Supported: ``type`` (object, array, string, number, integer, boolean, null; a list of types), ``properties``,
``required``, ``additionalProperties`` (bool), ``enum``, ``items``, ``minimum`` / ``maximum``, ``minLength`` /
``maxLength``, ``minItems`` / ``maxItems``. Unknown keywords are ignored. Returns the problems as ``path: reason``
strings; an empty list means the value is valid.
"""

from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}
MAX_DEPTH = 8


def validate(value: Any, schema: dict[str, Any] | None, path: str = "$", depth: int = 0) -> list[str]:
    if not schema or depth > MAX_DEPTH:
        return []
    problems: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES.get(name, lambda _v: True)(value) for name in names):
            return [f"{path}: 类型应为 {'/'.join(names)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: 不在允许的取值内")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: 长度不足")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: 长度超限")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: 小于最小值")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: 大于最大值")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: 元素过少")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: 元素过多")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                problems += validate(item, item_schema, f"{path}[{index}]", depth + 1)
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name}: 缺少必填参数")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    problems.append(f"{path}.{name}: 不支持的参数")
        for name, sub in properties.items():
            if name in value:
                problems += validate(value[name], sub, f"{path}.{name}", depth + 1)
    return problems
