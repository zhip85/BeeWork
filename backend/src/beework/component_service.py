"""Widget templates and tenant components (T-043, CHN-040–046, PLT-110–114).

Content rules shared by the platform's template editor and the tenant's component editor live here: trigger-tag normalisation, the
version rule, copy / sync, what a channel snapshot freezes of a component, and the prompt that tells the model which components exist
(OC-073; T-059 calls the same function at run time).
"""

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.channel_types import TYPE_CODES
from beework.component_schema import (
    SchemaError,
    compact_schema,
    parse_component_schema,
    validate_component_payload,
)
from beework.errors import ApiError
from beework.models.channels import ChannelInstance
from beework.models.components import TenantComponent, WidgetTemplate

TYPES = ("card", "comparison", "timeline", "form")
MODES = ("native", "text", "json", "off")
# CHN-041: a new component is native on website / H5, text in IM, JSON for the open API.
DEFAULT_COMPATIBILITY = {"website": "native", "h5": "native", "whatsapp": "text", "wechat": "text", "open-api": "json"}
MAX_COMPONENTS_PER_TURN = 2

CARD_SCHEMA = {
    "name": "string", "subtitle": "string", "price": "string", "originalPrice?": "string", "stock": "number",
    "tags": "string[]", "rating": "number", "action": "{ label: string; url: string }",
}
CARD_SAMPLE = {
    "name": "Acme 降噪耳机 Pro", "subtitle": "主动降噪 · 40 小时续航", "price": "¥1,299", "originalPrice": "¥1,599", "stock": 23,
    "tags": ["新品", "顺丰包邮"], "rating": 4.8, "action": {"label": "立即购买", "url": "https://shop.acme.com/p/1029"},
}
COMPARISON_SCHEMA = {"title": "string", "features": "string[]", "products": "{ name: string; values: string[]; recommended?: boolean }[]"}
COMPARISON_SAMPLE = {
    "title": "耳机对比", "features": ["降噪", "续航", "价格"],
    "products": [{"name": "Pro", "values": ["主动", "40 小时", "¥1,299"], "recommended": True}, {"name": "Lite", "values": ["被动", "20 小时", "¥499"]}],
}
TIMELINE_SCHEMA = {
    "orderNo": "string", "carrier": "string", "trackingNo": "string", "status": "string", "eta": "string",
    "steps": "{ label: string; time: string; location?: string; done: boolean }[]",
}
TIMELINE_SAMPLE = {
    "orderNo": "A20260001", "carrier": "顺丰速运", "trackingNo": "SF1234567890", "status": "运输中", "eta": "明天 18:00 前",
    "steps": [{"label": "已下单", "time": "11-12 09:30", "done": True}, {"label": "已发货", "time": "11-12 18:00", "location": "上海", "done": True}, {"label": "派送中", "time": "11-14 08:00", "done": False}],
}
FORM_SCHEMA = {
    "title": "string", "service": "string", "dates": "{ date: string; weekday: string; available: boolean }[]",
    "slots": "{ time: string; available: boolean }[]", "selectedDate?": "string", "selectedSlot?": "string", "confirmLabel": "string",
}
FORM_SAMPLE = {
    "title": "预约到店服务", "service": "耳机检测", "confirmLabel": "确认预约",
    "dates": [{"date": "11-15", "weekday": "周六", "available": True}, {"date": "11-16", "weekday": "周日", "available": False}],
    "slots": [{"time": "10:00", "available": True}, {"time": "14:00", "available": True}], "selectedDate": "11-15",
}
TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "card": {"tag": "product_card", "schema": CARD_SCHEMA, "sample": CARD_SAMPLE},
    "comparison": {"tag": "product_comparison", "schema": COMPARISON_SCHEMA, "sample": COMPARISON_SAMPLE},
    "timeline": {"tag": "order_tracking", "schema": TIMELINE_SCHEMA, "sample": TIMELINE_SAMPLE},
    "form": {"tag": "appointment_picker", "schema": FORM_SCHEMA, "sample": FORM_SAMPLE},
}


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


# PLT-114
PRESETS = [
    ("商品卡片", "Product card", "card", "展示商品图片、价格、库存和主操作", "1.2", ("native", "native", "native", "native", "json")),
    ("商品对比", "Product comparison", "comparison", "对比两个或多个商品的关键属性", "1.0", ("native", "native", "text", "text", "json")),
    ("订单追踪", "Order tracking", "timeline", "展示配送状态、关键节点和物流详情", "2.1", ("native", "native", "native", "text", "json")),
    ("预约时间选择器", "Appointment picker", "form", "选择可用日期、时段并确认预约", "1.4", ("native", "native", "text", "off", "json")),
]


def ensure_default_templates(db: Session) -> None:
    """Install the four preset templates once (by trigger tag); a platform edit is never overwritten."""
    existing = set(db.scalars(select(WidgetTemplate.trigger_tag)))
    for name_zh, name_en, kind, description, version, modes in PRESETS:
        base = TYPE_DEFAULTS[kind]
        if base["tag"] in existing:
            continue
        db.add(WidgetTemplate(
            name_zh=name_zh, name_en=name_en, type=kind, description=description, trigger_tag=base["tag"], schema_text=pretty(base["schema"]),
            sample_data=pretty(base["sample"]), compatibility=dict(zip(("website", "h5", "whatsapp", "wechat", "open-api"), modes, strict=True)),
            version=version, status="published",
        ))
    db.flush()


# -- content rules ------------------------------------------------------------------------------------------


def normalize_tag(value: str) -> str:
    """CHN-045: lower case, anything outside a-z / 0-9 / _ becomes _."""
    return re.sub(r"[^a-z0-9_]", "_", value.strip().lower())[:40]


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def bump_minor(version: str) -> str:
    parts = list(version_key(version)) + [0, 0]
    return f"{parts[0]}.{parts[1] + 1}"


def check_content(schema_text: str, sample_data: str, compatibility: dict[str, str]) -> None:
    """The definition must parse; the sample data must be JSON and fit the schema (it is what the card preview and the trial run render)."""
    try:
        node = parse_component_schema(schema_text)
    except SchemaError as exc:
        raise ApiError(422, "component.schema_invalid", f"数据架构有误：{exc}") from exc
    try:
        sample = json.loads(sample_data or "{}")
    except json.JSONDecodeError as exc:
        raise ApiError(422, "component.sample_invalid", f"模拟数据 JSON 格式有误：{exc.msg}") from exc
    problems = validate_component_payload(node, sample)
    if problems:
        raise ApiError(422, "component.sample_invalid", "模拟数据不符合数据架构：" + "；".join(problems[:5]))
    if set(compatibility) - set(TYPE_CODES) or any(mode not in MODES for mode in compatibility.values()):
        raise ApiError(422, "component.compatibility_invalid", "兼容矩阵只能设置五种渠道，模式为 native / text / json / off")


def full_compatibility(compatibility: dict[str, str]) -> dict[str, str]:
    return {code: compatibility.get(code, DEFAULT_COMPATIBILITY[code]) for code in TYPE_CODES}


def content_of(item: WidgetTemplate | TenantComponent) -> dict[str, Any]:
    return {"trigger_tag": item.trigger_tag, "schema": item.schema_text, "sample_data": item.sample_data, "compatibility": full_compatibility(item.compatibility or {})}


# -- copy and sync (CHN-043/044) ---------------------------------------------------------------------------------


def copy_template(db: Session, tenant_id: UUID, template: WidgetTemplate, user_id: UUID | None) -> TenantComponent:
    if template.status != "published":
        raise ApiError(409, "component.template_unavailable", "该模板已弃用或尚未发布，不能复制")
    tag, taken = template.trigger_tag, set(db.scalars(select(TenantComponent.trigger_tag).where(TenantComponent.tenant_id == tenant_id)))
    suffix = 2
    while tag in taken:  # the tag is unique per tenant: a second copy gets _2, _3 …
        tag = f"{template.trigger_tag}_{suffix}"
        suffix += 1
    component = TenantComponent(
        tenant_id=tenant_id, name_zh=template.name_zh, name_en=template.name_en, type=template.type, description=template.description,
        trigger_tag=tag, schema_text=template.schema_text, sample_data=template.sample_data, compatibility=full_compatibility(template.compatibility or {}),
        version=template.version, status="enabled", template_id=template.id, template_version=template.version, created_by=user_id,
    )
    db.add(component)
    db.flush()
    return component


def sync_diff(component: TenantComponent, template: WidgetTemplate) -> list[dict[str, Any]]:
    """What a sync would overwrite (shown before the administrator confirms)."""
    changes = []
    for key, label in (("trigger_tag", "触发标签"), ("schema", "数据架构"), ("sample_data", "模拟数据"), ("compatibility", "渠道兼容")):
        before, after = content_of(component)[key], content_of(template)[key]
        if before != after:
            changes.append({"field": label, "from": before, "to": after})
    if component.version != template.version:
        changes.append({"field": "版本", "from": component.version, "to": template.version})
    return changes


def sync_component(db: Session, component: TenantComponent, template: WidgetTemplate) -> None:
    """Overwrite schema, sample data, tag, compatibility and version with the template's; keep the tenant's name and description."""
    taken = db.scalar(select(TenantComponent.id).where(
        TenantComponent.tenant_id == component.tenant_id, TenantComponent.trigger_tag == template.trigger_tag, TenantComponent.id != component.id
    ))
    if taken is not None:
        raise ApiError(409, "component.tag_taken", f"触发标签 {template.trigger_tag} 已被本租户的其他组件使用，请先改名后再同步")
    component.trigger_tag, component.schema_text, component.sample_data = template.trigger_tag, template.schema_text, template.sample_data
    component.compatibility = full_compatibility(template.compatibility or {})
    component.version = template.version
    component.template_version = template.version
    component.row_version += 1


def provision_tenant_components(db: Session, tenant_id: UUID) -> list[TenantComponent]:
    """New tenants start with the first three templates copied (PLT-114). Called by the tenant provisioning flow (T-100)."""
    ensure_default_templates(db)
    templates = list(db.scalars(select(WidgetTemplate).where(WidgetTemplate.status == "published").order_by(WidgetTemplate.created_at, WidgetTemplate.trigger_tag).limit(3)))
    return [copy_template(db, tenant_id, template, None) for template in templates]


# -- channels ------------------------------------------------------------------------------------------------


def channel_uses(db: Session, tenant_id: UUID, component_id: UUID) -> list[str]:
    """Names of channels whose *published* snapshot contains the component (RUL-013: such a component cannot be deleted)."""
    wanted = str(component_id)
    used = []
    for channel in db.scalars(select(ChannelInstance).where(ChannelInstance.tenant_id == tenant_id, ChannelInstance.published_snapshot.is_not(None))):
        if any(item.get("id") == wanted for item in (channel.published_snapshot or {}).get("components", [])):
            used.append(channel.name)
    return used


def selected_components(db: Session, channel: ChannelInstance) -> tuple[list[TenantComponent], list[str]]:
    """The channel's selected components that can be published, and what is wrong with the rest (gone, disabled, off for this type)."""
    components, problems = [], []
    for raw in channel.component_ids or []:
        try:
            ident = UUID(raw)
        except (ValueError, TypeError):
            continue
        item = db.scalar(select(TenantComponent).where(TenantComponent.id == ident, TenantComponent.tenant_id == channel.tenant_id))
        if item is None:
            problems.append("所选组件已被删除，请重新选择")
        elif item.status == "disabled":
            problems.append(f"组件“{item.name_zh}”已停用，请取消选择或重新启用")
        elif full_compatibility(item.compatibility or {})[channel.type] == "off":
            problems.append(f"组件“{item.name_zh}”在{channel.type}渠道上已关闭，请取消选择")
        else:
            components.append(item)
    return components, problems


def snapshot_components(channel: ChannelInstance, components: list[TenantComponent]) -> list[dict[str, Any]]:
    """What a publish freezes of each component (CHN-024/025): later edits of the library do not change it."""
    return [
        {
            "id": str(item.id), "name_zh": item.name_zh, "name_en": item.name_en, "type": item.type, "trigger_tag": item.trigger_tag,
            "schema": item.schema_text, "sample_data": item.sample_data, "version": item.version,
            "mode": full_compatibility(item.compatibility or {})[channel.type],
        }
        for item in components
    ]


def component_state(snapshot_components_: list[dict[str, Any]] | None) -> list[tuple[str, str]]:
    return sorted((item["id"], item["version"]) for item in snapshot_components_ or [])


def components_diff(live: list[dict[str, Any]], draft: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """CHN-021: added (+) and removed (−) against the live version; a changed version of a kept component is listed as changed."""
    before, after = {i["id"]: i for i in live}, {i["id"]: i for i in draft}
    return {
        "added": [after[k] for k in after if k not in before],
        "removed": [before[k] for k in before if k not in after],
        "changed": [{**after[k], "from_version": before[k]["version"]} for k in after if k in before and before[k]["version"] != after[k]["version"]],
    }


def injection_prompt(channel_name: str, items: list[dict[str, Any]], *, lang: str = "zh", limit: int = MAX_COMPONENTS_PER_TURN) -> str:
    """The text appended to the system prompt so the model knows which components it may emit (OC-073)."""
    usable = [i for i in items if i["mode"] != "off"]
    zh = lang != "en"
    if not usable:
        return (f"# 输出格式\n渠道：{channel_name}\n当前渠道无可用组件，请使用纯文本回复。" if zh
                else f"# Output format\nChannel: {channel_name}\nNo components are available on this channel; reply in plain text.")
    lines = []
    for item in usable:
        name = item["name_zh"] if zh or not item.get("name_en") else item["name_en"]
        note = ("（本渠道以文本降级展示）" if zh else " (shown as text on this channel)") if item["mode"] == "text" else (" (JSON payload)" if item["mode"] == "json" else "")
        lines.append(f"- <{item['trigger_tag']}> {name}{note} schema: {compact_schema(item['schema'])}")
    body = "\n".join(lines)
    if zh:
        return (f"# 输出格式\n渠道：{channel_name}\n当结构化信息更清晰时，按以下格式输出组件：<标签>{{符合 schema 的 JSON}}</标签>\n可用组件：\n{body}\n规则：\n"
                f"- 每轮最多输出 {limit} 个组件。\n- JSON 必须严格符合 schema，否则改用文本回复。\n- 组件前需附一句文字说明。")
    return (f"# Output format\nChannel: {channel_name}\nWhen structured information is clearer, output a component as <tag>{{JSON matching the schema}}</tag>\nAvailable components:\n{body}\nRules:\n"
            f"- Output at most {limit} components per turn.\n- The JSON must match the schema strictly; otherwise reply in text.\n- Add one sentence of text before a component.")


def trial_runs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CHN-023: validate each selected component's sample data against its schema — for real, not a fixed “passed”."""
    results = []
    for item in items:
        try:
            sample = json.loads(item["sample_data"] or "{}")
            problems = validate_component_payload(item["schema"], sample)
        except (json.JSONDecodeError, SchemaError) as exc:
            sample, problems = None, [str(exc)]
        results.append({"id": item["id"], "name": item["name_zh"], "trigger_tag": item["trigger_tag"], "mode": item["mode"], "sample": sample, "ok": not problems, "problems": problems})
    return results


def now_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
