"""Notification rules at work (T-088, SET-100–102, PRD 5.8): the bus subscriber, the four channels, digests and recipients.

A rule says: when ``event`` happens, tell ``recipients`` over ``channel``, at once or as an hourly / daily digest. Events arrive from the bus
(T-087). Recipients are resolved when the message is sent — a role means whoever holds it now — and never reach beyond the rule's own tenant.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import notification_catalog as catalog
from beework import sms
from beework.config import settings
from beework.events import subscribe
from beework.mail import MailRateLimited, normalize_locale, send_mail
from beework.model_secrets import decrypt_model_api_key
from beework.models import Membership, User
from beework.models.notification_rules import NotificationQueueItem, NotificationRule
from beework.models.webhooks import DomainEvent, WebhookEndpoint
from beework.tools.http import ToolHttpError, safe_request
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DIGEST_LINES = 20


def aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# -- who gets it -------------------------------------------------------------------------------------------------


def email_addresses(db: Session, rule: NotificationRule) -> list[tuple[str, str]]:
    """``(address, locale)`` of everyone an email rule reaches *now*: the chosen members, whoever holds the chosen roles today, and the typed addresses."""
    people = rule.recipients or {}
    found: dict[str, str] = {}
    member_ids = [UUID(m) for m in people.get("members", [])]
    memberships = select(Membership.user_id).where(Membership.tenant_id == rule.tenant_id)
    wanted = set(member_ids) | set(db.scalars(memberships.where(Membership.role.in_(people.get("roles", [])))) if people.get("roles") else [])
    if wanted:
        in_tenant = set(db.scalars(memberships))  # a member id from another tenant is never honoured
        for user in db.scalars(select(User).where(User.id.in_(wanted & in_tenant))):
            if user.email:
                found[user.email.lower()] = normalize_locale(user.locale)
    for address in people.get("emails", []):
        found.setdefault(address.strip().lower(), "zh-CN")
    return sorted(found.items())


# -- sending -----------------------------------------------------------------------------------------------------


def send_message(db: Session, rule: NotificationRule, build: Callable[[str], tuple[str, str, str]], *, transport=None, resolver=None) -> int:
    """One message over the rule's channel; returns how many recipients it was handed to.

    ``build(locale)`` renders ``(title, body, link)``: mails are written in each recipient's own language, the other channels in Chinese."""
    people = rule.recipients or {}
    base = settings.public_api_url.rstrip("/")

    def link_of(link: str) -> str:
        return f"{base}{link}" if base and link.startswith("/") else ""

    title, body, link = build("zh-CN")
    full_link = link_of(link)
    if rule.channel == "email":
        count = 0
        for address, preferred in email_addresses(db, rule):
            mail_title, mail_body, mail_link = build(preferred)
            try:
                send_mail(db, tenant_id=rule.tenant_id, to=address, template="generic_notification", locale=preferred,
                          context={"title": mail_title, "message": mail_body, "link": link_of(mail_link)})
                count += 1
            except MailRateLimited:
                logger.info("Notification mail to one recipient skipped: rate limit")
        return count
    if rule.channel == "slack":
        url = decrypt_model_api_key(people["url_sealed"])
        text = f"*{title}*\n{body}" + (f"\n{full_link}" if full_link else "")
        result = safe_request("POST", url, json={"text": text}, timeout=10.0, max_bytes=16 * 1024, transport=transport, resolver=resolver)
        if not 200 <= result.status < 300:
            raise RuntimeError(f"Slack 返回 {result.status}")
        return 1
    if rule.channel == "sms":
        for phone in people.get("phones", []):
            sms.send(phone, f"{title}: {body}"[:300])
        return len(people.get("phones", []))
    from beework.webhooks import (
        queue_delivery,  # a Webhook endpoint receives the notification as a delivery of its own
    )

    count = 0
    for endpoint in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == rule.tenant_id, WebhookEndpoint.id.in_([UUID(i) for i in people.get("webhook_ids", [])]))):
        queue_delivery(db, endpoint, uuid4(), "notification", {"event": rule.event, "title": title, "message": body, "link": full_link})
        count += 1
    return count


# -- the bus subscriber -------------------------------------------------------------------------------------------


def _interested(db: Session, tenant_id: UUID, event_type: str) -> bool:
    return any(event_type in catalog.interested_event_types(rule.event) for rule in db.scalars(select(NotificationRule).where(NotificationRule.tenant_id == tenant_id, NotificationRule.enabled.is_(True))))


@subscribe(interested=_interested)
def on_event(db: Session, event: DomainEvent) -> None:
    rule_events = catalog.rule_events_of(event.type, event.payload)
    if not rule_events:
        return
    for rule in db.scalars(select(NotificationRule).where(NotificationRule.tenant_id == event.tenant_id, NotificationRule.enabled.is_(True), NotificationRule.event.in_(rule_events))):
        if rule.frequency == "realtime":
            enqueue(db, "notifications.send", {"rule_id": str(rule.id), "event_id": str(event.id), "rule_event": rule.event}, tenant_id=rule.tenant_id, idempotency_key=f"notifications.send:{rule.id}:{event.id}")
        elif db.scalar(select(NotificationQueueItem.id).where(NotificationQueueItem.rule_id == rule.id, NotificationQueueItem.event_id == event.id)) is None:
            db.add(NotificationQueueItem(tenant_id=rule.tenant_id, rule_id=rule.id, event_id=event.id, rule_event=rule.event, payload=event.payload))


@job_handler("notifications.send")
def send_job(db: Session, payload: dict[str, Any]) -> None:
    rule = db.get(NotificationRule, UUID(payload["rule_id"]))
    event = db.get(DomainEvent, UUID(payload["event_id"]))
    if rule is None or event is None or not rule.enabled or rule.tenant_id != event.tenant_id:
        return  # switched off or deleted since: nothing is sent
    send_message(db, rule, lambda locale: catalog.render(rule.event, event.payload, locale))
    db.commit()


# -- digests ----------------------------------------------------------------------------------------------------


def digest_text(items: list[NotificationQueueItem], locale: str = "zh-CN") -> tuple[str, str, str]:
    zh = not locale.startswith("en")
    first = items[0].rule_event
    label = catalog.LABELS[first][0 if zh else 1]
    lines = [catalog.render(item.rule_event, item.payload, locale)[1] for item in items[:DIGEST_LINES]]
    extra = len(items) - len(lines)
    if extra > 0:
        lines.append(f"…另有 {extra} 条" if zh else f"… and {extra} more")
    title = f"BeeWork · {label}（{len(items)} 条）" if zh else f"BeeWork · {label} ({len(items)})"
    return title, "\n".join(f"- {line}" for line in lines), catalog.render(first, items[0].payload, locale)[2]


def run_digests(db: Session, now: datetime | None = None, *, transport=None, resolver=None) -> int:
    """Send what the hourly rules collected (every run) and what the daily rules collected (when the configured hour comes). One message per rule."""
    current = now or datetime.now(UTC)
    daily_due = current.hour == settings.notification_daily_hour_utc
    frequencies = ["hourly"] + (["daily"] if daily_due else [])
    sent = 0
    for rule in db.scalars(select(NotificationRule).where(NotificationRule.frequency.in_(frequencies))):
        items = list(db.scalars(select(NotificationQueueItem).where(NotificationQueueItem.rule_id == rule.id, NotificationQueueItem.sent_at.is_(None)).order_by(NotificationQueueItem.created_at)))
        if not items:
            continue
        if rule.enabled:  # a rule switched off since still empties its queue, but says nothing
            try:
                send_message(db, rule, lambda locale, items=items: digest_text(items, locale), transport=transport, resolver=resolver)
            except (ToolHttpError, RuntimeError, ValueError) as exc:
                logger.warning("Digest of rule %s failed: %s", rule.id, exc)
                continue  # the items stay queued; the next run tries again
        for item in items:
            item.sent_at = current
        sent += 1
        db.commit()
    return sent


@job_handler("notifications.digest")
def digest_job(db: Session, _payload: dict[str, Any]) -> None:
    run_digests(db)


def purge_sent(db: Session, now: datetime | None = None, *, keep: timedelta = timedelta(days=7)) -> None:
    from sqlalchemy import delete

    db.execute(delete(NotificationQueueItem).where(NotificationQueueItem.sent_at.is_not(None), NotificationQueueItem.sent_at < (now or datetime.now(UTC)) - keep))
