"""Notification rules (T-088, SET-100/101): who is told about which event, over which channel and how often."""

import re
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import notification_catalog as catalog
from beework import sms
from beework.api.conventions import ApiError, check_row_version, validate_public_https_url
from beework.auth import require_csrf
from beework.db import get_db
from beework.model_secrets import encrypt_model_api_key
from beework.models import AuditEvent, Membership, User, utc_now
from beework.models.notification_rules import NotificationRule
from beework.models.webhooks import WebhookEndpoint
from beework.notify_rules import EMAIL
from beework.rbac import ROLE_LABELS, require_tenant_permission
from beework.schemas.notification_rules import Catalog, NamedItem, RuleList, RuleResponse, RuleWrite

router = APIRouter()
MAX_RULES = 100
MAX_PEOPLE = 50
PHONE = re.compile(r"^\+?[0-9]{7,15}$")


def _mask_url(url: str) -> str:
    host = re.sub(r"^https://", "", url).split("/", 1)[0]
    return f"{host}/••••{url[-4:]}"


def _summary(rule: NotificationRule) -> str:
    people = rule.recipients or {}
    if rule.channel == "email":
        parts = [f"{len(people.get('members', []))} 名成员" if people.get("members") else "", f"{len(people.get('roles', []))} 个角色" if people.get("roles") else "", f"{len(people.get('emails', []))} 个邮箱" if people.get("emails") else ""]
        return " · ".join(p for p in parts if p) or "无接收人"
    if rule.channel == "slack":
        return people.get("url_mask", "")
    if rule.channel == "sms":
        return f"{len(people.get('phones', []))} 个手机号"
    return f"{len(people.get('webhook_ids', []))} 个 Webhook 端点"


def _response(rule: NotificationRule) -> RuleResponse:
    shown = {k: v for k, v in (rule.recipients or {}).items() if k != "url_sealed"}
    return RuleResponse(id=rule.id, event=rule.event, channel=rule.channel, recipients=shown, summary=_summary(rule), frequency=rule.frequency, enabled=rule.enabled, row_version=rule.row_version, created_at=rule.created_at)


def _load(db: Session, membership: Membership, rule_id: UUID, *, lock: bool = False) -> NotificationRule:
    statement = select(NotificationRule).where(NotificationRule.id == rule_id, NotificationRule.tenant_id == membership.tenant_id)
    rule = db.scalar(statement.with_for_update() if lock else statement)
    if rule is None:
        raise ApiError(404, "not_found", "未找到指定通知规则")
    return rule


def _clean_recipients(db: Session, membership: Membership, payload: RuleWrite, existing: NotificationRule | None) -> dict:
    """The recipients as stored, checked for the channel: members of this tenant, known roles, addresses, a public https Slack address, phones, own endpoints."""
    raw = payload.recipients
    if payload.channel == "email":
        members = list(dict.fromkeys(str(m) for m in raw.get("members", [])))
        roles = list(dict.fromkeys(str(r) for r in raw.get("roles", [])))
        emails = list(dict.fromkeys(str(a).strip().lower() for a in raw.get("emails", [])))
        if len(members) + len(roles) + len(emails) > MAX_PEOPLE:
            raise ApiError(422, "notification.too_many", f"接收人最多 {MAX_PEOPLE} 个")
        if not (members or roles or emails):
            raise ApiError(422, "notification.no_recipients", "请至少选择一个接收人")
        known = {str(u) for u in db.scalars(select(Membership.user_id).where(Membership.tenant_id == membership.tenant_id))}
        if any(m not in known for m in members):
            raise ApiError(422, "notification.invalid_recipient", "接收人里有不属于本企业的成员")
        if any(r not in ROLE_LABELS for r in roles):
            raise ApiError(422, "notification.invalid_recipient", "接收人里有未知的角色")
        if any(not EMAIL.match(a) or len(a) > 200 for a in emails):
            raise ApiError(422, "notification.invalid_recipient", "邮箱格式不正确")
        return {"members": members, "roles": roles, "emails": emails}
    if payload.channel == "slack":
        url = str(raw.get("url") or "").strip()
        if not url and existing is not None and existing.channel == "slack":
            return dict(existing.recipients)  # left blank on an edit: keep the stored address
        url = validate_public_https_url(url)  # Slack's Incoming Webhook address: public https only
        return {"url_sealed": encrypt_model_api_key(url), "url_mask": _mask_url(url)}
    if payload.channel == "sms":
        if not sms.configured():
            raise ApiError(409, "notification.sms_unconfigured", "短信通道尚未配置服务商，暂不可用")
        phones = list(dict.fromkeys(str(p).strip() for p in raw.get("phones", [])))
        if not phones or len(phones) > MAX_PEOPLE or any(not PHONE.match(p) for p in phones):
            raise ApiError(422, "notification.invalid_recipient", "请填写手机号（7–15 位数字，可带 +）")
        return {"phones": phones}
    ids = list(dict.fromkeys(str(i) for i in raw.get("webhook_ids", [])))
    if not ids:
        raise ApiError(422, "notification.no_recipients", "请至少选择一个 Webhook 端点")
    owned = {str(i) for i in db.scalars(select(WebhookEndpoint.id).where(WebhookEndpoint.tenant_id == membership.tenant_id))}
    if any(i not in owned for i in ids):
        raise ApiError(422, "notification.invalid_recipient", "选择的 Webhook 端点不存在")
    return {"webhook_ids": ids}


def _audit(db: Session, membership: Membership, action: str, rule: NotificationRule) -> None:
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action, details={"rule_id": str(rule.id), "event": rule.event, "channel": rule.channel}))


@router.get("/notification-rules/catalog", response_model=Catalog)
def rule_catalog(membership: Membership = Depends(require_tenant_permission("settings:view")), db: Session = Depends(get_db)) -> Catalog:
    members = [NamedItem(id=str(u.id), name=f"{u.display_name} <{u.email}>") for u in db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(Membership.tenant_id == membership.tenant_id).order_by(User.display_name))]
    hooks = [NamedItem(id=str(w.id), name=w.url) for w in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == membership.tenant_id).order_by(WebhookEndpoint.created_at))]
    return Catalog(
        events=[{"key": k, "zh": catalog.LABELS[k][0], "en": catalog.LABELS[k][1]} for k in catalog.RULE_EVENTS],
        channels=[{"key": c, "available": c != "sms" or sms.configured(), "reason": None if c != "sms" or sms.configured() else "unconfigured"} for c in catalog.CHANNELS],
        frequencies=list(catalog.FREQUENCIES), roles=[NamedItem(id=k, name=v) for k, v in ROLE_LABELS.items()], members=members, webhooks=hooks,
    )


@router.get("/notification-rules", response_model=RuleList)
def list_rules(
    q: str | None = Query(default=None, max_length=50),
    membership: Membership = Depends(require_tenant_permission("settings:view")),
    db: Session = Depends(get_db),
) -> RuleList:
    rules = list(db.scalars(select(NotificationRule).where(NotificationRule.tenant_id == membership.tenant_id).order_by(NotificationRule.created_at.desc())))
    if q and q.strip():
        needle = q.strip().lower()
        rules = [r for r in rules if needle in catalog.LABELS[r.event][0].lower() or needle in catalog.LABELS[r.event][1].lower() or needle in r.event or needle in r.channel]
    return RuleList(items=[_response(r) for r in rules])


def _check_head(payload: RuleWrite) -> None:
    if payload.event not in catalog.RULE_EVENTS:
        raise ApiError(422, "notification.invalid_event", f"不支持的事件：{payload.event}")


@router.post("/notification-rules", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RuleWrite,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RuleResponse:
    _check_head(payload)
    if len(list(db.scalars(select(NotificationRule.id).where(NotificationRule.tenant_id == membership.tenant_id)))) >= MAX_RULES:
        raise ApiError(409, "notification.limit", f"最多创建 {MAX_RULES} 条规则")
    rule = NotificationRule(
        tenant_id=membership.tenant_id, event=payload.event, channel=payload.channel, frequency=payload.frequency, enabled=payload.enabled,
        recipients=_clean_recipients(db, membership, payload, None), created_by=membership.user_id,
    )
    db.add(rule)
    db.flush()
    _audit(db, membership, "notification_rule.created", rule)
    db.commit()
    return _response(rule)


@router.put("/notification-rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    rule_id: UUID,
    payload: RuleWrite,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> RuleResponse:
    rule = _load(db, membership, rule_id, lock=True)
    if payload.row_version is not None:
        check_row_version(rule, payload.row_version, db)
    _check_head(payload)
    recipients = _clean_recipients(db, membership, payload, rule)
    rule.event, rule.channel, rule.frequency, rule.enabled, rule.recipients = payload.event, payload.channel, payload.frequency, payload.enabled, recipients
    rule.row_version += 1
    rule.updated_at = utc_now()
    _audit(db, membership, "notification_rule.updated", rule)
    db.commit()
    return _response(rule)


@router.delete("/notification-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    from sqlalchemy import delete

    from beework.models.notification_rules import NotificationQueueItem

    rule = _load(db, membership, rule_id, lock=True)
    _audit(db, membership, "notification_rule.deleted", rule)
    db.execute(delete(NotificationQueueItem).where(NotificationQueueItem.rule_id == rule.id))
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

