"""Webhook endpoints and their deliveries (T-087, SET-091, NFR-020).

A delivery is one event for one endpoint. It is POSTed as JSON with the event name, the delivery id, a timestamp and an HMAC-SHA256
signature of ``"{timestamp}.{body}"``; any non-2xx answer, error or 10 seconds without one is a failure. Failures are retried after 1, 5
and 30 minutes and then hourly, for at most 24 hours from the event; the delivery id never changes, so receivers can de-duplicate. An
endpoint that has only failed for 24 hours is switched off and the tenant's administrators are told.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from beework.events import subscribe
from beework.model_secrets import decrypt_model_api_key, encrypt_model_api_key
from beework.models import AuditEvent, utc_now
from beework.models.webhooks import DomainEvent, WebhookDelivery, WebhookEndpoint
from beework.notifications import notify
from beework.tools.http import ToolHttpError, safe_request
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
RETRY_WINDOW = timedelta(hours=24)
DISABLE_AFTER = timedelta(hours=24)
KEEP_DELIVERIES = 100
TEST_EVENT = "webhook.test"
RETRY_SCHEDULE = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))  # then every hour
HOURLY = timedelta(hours=1)


def aware(moment: datetime) -> datetime:
    """SQLite hands datetimes back without a zone; they are always UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def retry_delay(failed_attempts: int) -> timedelta:
    """Wait before the next attempt, after the n-th failed one: 1, 5, 30 minutes, then hourly."""
    return RETRY_SCHEDULE[failed_attempts - 1] if failed_attempts <= len(RETRY_SCHEDULE) else HOURLY


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def signing_secret(endpoint: WebhookEndpoint) -> str:
    return decrypt_model_api_key(endpoint.secret_encrypted)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>`` over ``"{timestamp}.{body}"`` — the receiver recomputes it with the same secret (see docs/webhooks.md)."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def seal_secret(endpoint: WebhookEndpoint, secret: str) -> None:
    endpoint.secret_encrypted = encrypt_model_api_key(secret)
    endpoint.secret_last4 = secret[-4:]


def body_of(delivery: WebhookDelivery) -> bytes:
    """The exact bytes sent, so the signature and the retries are always over the same body."""
    envelope = {"id": str(delivery.id), "event": delivery.event_type, "created_at": aware(delivery.created_at).astimezone(UTC).isoformat(), "data": delivery.payload}
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


# -- creating deliveries -------------------------------------------------------------------------------------


def queue_delivery(db: Session, endpoint: WebhookEndpoint, event_id: UUID, event_type: str, payload: dict[str, Any], *, is_test: bool = False, run_after: datetime | None = None) -> WebhookDelivery:
    existing = db.scalar(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id, WebhookDelivery.event_id == event_id))
    if existing is not None:
        return existing  # the dispatch job ran twice: one delivery per event and endpoint
    delivery = WebhookDelivery(tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id, event_id=event_id, event_type=event_type, payload=payload, is_test=is_test)
    db.add(delivery)
    db.flush()
    enqueue(db, "webhooks.deliver", {"delivery_id": str(delivery.id)}, tenant_id=endpoint.tenant_id, idempotency_key=f"webhooks.deliver:{delivery.id}:0", run_after=run_after)
    prune(db, endpoint.id)
    return delivery


def _has_endpoint(db: Session, tenant_id: UUID, event_type: str) -> bool:
    return any(event_type in (e.events or []) for e in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == tenant_id, WebhookEndpoint.enabled.is_(True))))


@subscribe(interested=_has_endpoint)
def fan_out(db: Session, event: DomainEvent) -> None:
    """Every enabled endpoint of the tenant that subscribed to the event gets a delivery."""
    for endpoint in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == event.tenant_id, WebhookEndpoint.enabled.is_(True))):
        if event.type in (endpoint.events or []):
            queue_delivery(db, endpoint, event.id, event.type, event.payload)


def prune(db: Session, endpoint_id: UUID) -> None:
    """Only the latest 100 deliveries of an endpoint are kept (SET-091)."""
    keep = list(db.scalars(select(WebhookDelivery.id).where(WebhookDelivery.endpoint_id == endpoint_id).order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id).limit(KEEP_DELIVERIES)))
    if len(keep) == KEEP_DELIVERIES:
        db.execute(delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id, WebhookDelivery.id.not_in(keep)))


# -- sending ---------------------------------------------------------------------------------------------------


def attempt(db: Session, delivery: WebhookDelivery, endpoint: WebhookEndpoint, now: datetime, *, transport=None, resolver=None) -> bool:
    """Send once and record the outcome on the delivery. Returns whether it was delivered."""
    body = body_of(delivery)
    timestamp = str(int(now.timestamp()))
    headers = {
        "Content-Type": "application/json", "User-Agent": "BeeWork-Webhook/1", "X-BeeWork-Event": delivery.event_type,
        "X-BeeWork-Delivery": str(delivery.id), "X-BeeWork-Timestamp": timestamp, "X-BeeWork-Signature": sign(signing_secret(endpoint), timestamp, body),
    }
    started = time.monotonic()
    delivery.attempts += 1
    delivery.last_attempt_at = now
    status_code, error = None, None
    try:
        result = safe_request("POST", endpoint.url, headers=headers, content=body, timeout=TIMEOUT_SECONDS, max_bytes=64 * 1024, transport=transport, resolver=resolver)
        status_code = result.status
        if not 200 <= result.status < 300:
            error = f"对方返回 {result.status}"
    except ToolHttpError as exc:
        error = exc.message[:200]
    except Exception:  # noqa: BLE001 - whatever went wrong, the delivery failed; the reason shown never carries the exception text
        logger.warning("Webhook delivery %s failed unexpectedly", delivery.id, exc_info=True)
        error = "投递失败"
    delivery.duration_ms = int((time.monotonic() - started) * 1000)
    delivery.status_code, delivery.error = status_code, error
    return error is None


def record_outcome(db: Session, delivery: WebhookDelivery, endpoint: WebhookEndpoint, delivered: bool, now: datetime) -> None:
    endpoint.last_delivery_at = now
    endpoint.last_status_code = delivery.status_code
    if delivered:
        delivery.status, delivery.delivered_at, delivery.next_retry_at = "delivered", now, None
        endpoint.last_status, endpoint.failing_since = "delivered", None
        return
    endpoint.last_status = "failed"
    if endpoint.failing_since is None:
        endpoint.failing_since = now
    if now - aware(delivery.created_at) >= RETRY_WINDOW:
        delivery.status, delivery.next_retry_at = "dead", None
    else:
        delay = retry_delay(delivery.attempts)
        delivery.status, delivery.next_retry_at = "retrying", now + delay
        enqueue(
            db, "webhooks.deliver", {"delivery_id": str(delivery.id)}, tenant_id=delivery.tenant_id,
            idempotency_key=f"webhooks.deliver:{delivery.id}:{delivery.attempts}", run_after=delivery.next_retry_at,
        )
    if endpoint.enabled and now - aware(endpoint.failing_since) >= DISABLE_AFTER:
        disable_failing(db, endpoint)


def disable_failing(db: Session, endpoint: WebhookEndpoint) -> None:
    """24 hours of failures and no success: switch the endpoint off and tell the administrators (it can be turned on again)."""
    endpoint.enabled, endpoint.disabled_reason = False, "failing"
    endpoint.row_version += 1
    db.add(AuditEvent(tenant_id=endpoint.tenant_id, action="webhook.auto_disabled", details={"webhook_id": str(endpoint.id), "url": endpoint.url[:200]}))
    notify(
        db, tenant_id=endpoint.tenant_id, role="tenant_admin", type="webhook.disabled", title="Webhook 端点连续 24 小时推送失败，已自动停用",
        body=endpoint.url[:200], link="/settings/webhooks", params={"url": endpoint.url[:200]},
    )


def deliver(db: Session, delivery_id: UUID, *, now: datetime | None = None, transport=None, resolver=None) -> None:
    current = now or datetime.now(UTC)
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.status in ("delivered", "dead", "cancelled"):
        return
    endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is None or (not endpoint.enabled and not delivery.is_test):
        delivery.status, delivery.next_retry_at = "cancelled", None  # switched off or deleted since: nothing more is sent
        db.commit()
        return
    delivered = attempt(db, delivery, endpoint, current, transport=transport, resolver=resolver)
    record_outcome(db, delivery, endpoint, delivered, current)
    db.commit()


@job_handler("webhooks.deliver")
def deliver_job(db: Session, payload: dict[str, Any]) -> None:
    deliver(db, UUID(payload["delivery_id"]), now=utc_now())
