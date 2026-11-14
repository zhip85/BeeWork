"""OC · components at run time (T-059, OC-073/074).

The channel's *published snapshot* decides which components the model may use, never the live library (CHN-025). The prompt tells the
model the tags; after the model has answered, this stage looks for ``<tag>{json}</tag>`` blocks, checks each against its schema and turns it
into what the channel can show: ``native`` / ``json`` components become widgets (the SDK renders them, the open API returns them in
``widgets``), ``text`` ones become text through the fallback template, ``off`` ones are never injected and dropped if the model emits them.
Nothing here keeps user text: the trace gets tags, modes and problem lists.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from beework.component_schema import SchemaError, parse_component_schema, validate_component_payload
from beework.component_service import build_component_prompt
from beework.runtime.compiler import Node, RuntimeDeps, RuntimeState, register_stage
from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.oc import ComponentsConfig


@dataclass
class ComponentRuntime:
    """What a run knows of its channel's components: the frozen definitions and the channel's name for the prompt."""

    components: list[dict[str, Any]]  # snapshot entries: id, name_zh, name_en, type, trigger_tag, schema, sample_data, version, mode
    channel_name: str
    locale: str = "zh"

    def usable(self) -> list[dict[str, Any]]:
        return [c for c in self.components if c.get("mode") != "off"]

    def prompt(self, config: ComponentsConfig) -> str | None:
        return build_component_prompt(self.components, config, self.locale, self.channel_name) if self.usable() else None


def active(config: ComponentsConfig, runtime: "ComponentRuntime | None") -> bool:
    return bool(runtime is not None and config.enabled and runtime.usable())


def _fields_text(data: dict[str, Any]) -> str:
    lines = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}：{value}")
    return "\n".join(lines)


def render_text(template: str, component: dict[str, Any], data: dict[str, Any]) -> str:
    """The fallback for channels that cannot show a component (OC-071 ``{{title}}`` / ``{{fields}}``)."""
    title = data.get("title") or data.get("name") or component.get("name_zh") or component["trigger_tag"]
    rest = {k: v for k, v in data.items() if k not in ("title", "name")}
    return template.replace("{{title}}", str(title)).replace("{{fields}}", _fields_text(rest)).strip()


def _blocks(text: str, known: dict[str, dict[str, Any]]) -> list[tuple[int, int, str, str]]:
    """``(start, end, tag, inner)`` of every ``<tag>…</tag>`` of a known tag, in order."""
    if not known:
        return []
    pattern = re.compile(r"<(" + "|".join(re.escape(tag) for tag in known) + r")>(.*?)</\1>", re.DOTALL)
    return [(m.start(), m.end(), m.group(1), m.group(2)) for m in pattern.finditer(text)]


def _check(component: dict[str, Any], inner: str) -> tuple[Any, list[str]]:
    try:
        data = json.loads(inner)
    except json.JSONDecodeError:
        return None, ["JSON 无法解析"]
    try:
        problems = validate_component_payload(parse_component_schema(component["schema"]), data)
    except SchemaError as exc:
        return data, [f"组件定义有误：{exc}"]
    return data, problems


def apply_components(text: str, runtime: ComponentRuntime, config: ComponentsConfig) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """``(text, widgets, info, invalid_problems)``. ``info`` is the trace record of every block found; ``invalid_problems`` lists the problems
    that the ``retry`` policy can feed back to the model (it is empty when nothing failed)."""
    known = {c["trigger_tag"]: c for c in runtime.components}
    widgets: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    failures: list[str] = []
    out: list[str] = []
    cursor = 0
    for start, end, tag, inner in _blocks(text, known):
        out.append(text[cursor:start])
        cursor = end
        component = known[tag]
        mode = component.get("mode", "off")
        record = {"tag": tag, "mode": mode, "ok": True, "problems": [], "kept": False}
        info.append(record)
        if mode == "off":
            record.update(ok=False, problems=["该渠道已关闭此组件"])
            continue
        if sum(1 for r in info[:-1] if r["kept"]) >= config.max_per_turn:
            record.update(ok=False, problems=["超过每轮组件上限，已丢弃"])
            continue
        data, problems = _check(component, inner)
        if problems:
            record.update(ok=False, problems=problems)
            failures.append(f"<{tag}>：" + "；".join(problems))
            if config.on_invalid == "text" and isinstance(data, dict):
                out.append(render_text(config.text_template, component, data))  # the content survives, as text
                record["kept"] = True
            continue
        record["kept"] = True
        if mode == "text":
            out.append(render_text(config.text_template, component, data))
        else:  # native: the channel's SDK renders it; json: the open API returns it in ``widgets``
            widgets.append({"tag": tag, "type": component["type"], "mode": mode, "name": component.get("name_zh", ""), "version": component.get("version"), "data": data})
    out.append(text[cursor:])
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return cleaned, widgets, info, failures


@register_stage("components", replace=True)
def components_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    config = dsl.oc.components

    def node(state: RuntimeState) -> RuntimeState:
        runtime: ComponentRuntime | None = deps.components
        if state.get("blocked") or not active(config, runtime):
            return {}
        assert runtime is not None
        text = state.get("text") or ""
        cleaned, widgets, info, failures = apply_components(text, runtime, config)
        usage = state.get("usage")
        retried = False
        if failures and config.on_invalid == "retry" and deps.retry_components is not None:
            retried = True
            with deps.clock.external():
                again, extra = deps.retry_components(state, text, failures)
            if again is not None:
                usage = _add(usage, extra)
                # the second answer is judged once more; whatever is still wrong is degraded to text, never asked for again
                degraded = config.model_copy(update={"on_invalid": "text"})
                cleaned, widgets, info, failures = apply_components(again, runtime, degraded)
        update: RuntimeState = {"text": cleaned, "widgets": widgets, "component_info": {"blocks": info, "retried": retried}}
        if usage is not None:
            update["usage"] = usage
        return update

    return node


def _add(total: dict[str, int | None] | None, new: dict[str, int | None] | None) -> dict[str, int | None]:
    total, new = total or {"input": None, "output": None}, new or {}
    return {key: (total.get(key) or 0) + (new.get(key) or 0) if (total.get(key) is not None or new.get(key) is not None) else None for key in ("input", "output")}
