"""Webhook endpoints (T-087, SET-091): add, edit, test, rotate the secret, read the last 100 deliveries, redeliver."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from beework import webhooks as service
from beework.api.conventions import ApiError, check_row_version, validate_public_https_url
from beework.auth import require_csrf
from beework.db import get_db
from beework.events import EVENT_TYPES
from beework.models import AuditEvent, Membership, utc_now
from beework.models.webhooks import WebhookDelivery, WebhookEndpoint
from beework.rbac import require_tenant_permission
from beework.schemas.webhooks import (
    DeliveryList,
    DeliveryResponse,
    DeliverySummary,
    WebhookCreated,
    WebhookList,
    WebhookResponse,
    WebhookWrite,
)

router = APIRouter()
MAX_ENDPOINTS = 20


def _response(endpoint: WebhookEndpoint) -> WebhookResponse:
    last = DeliverySummary(status=endpoint.last_status, status_code=endpoint.last_status_code, at=endpoint.last_delivery_at) if endpoint.last_status else None
    return WebhookResponse(
        id=endpoint.id, url=endpoint.url, events=list(endpoint.events or []), enabled=endpoint.enabled, secret_mask=f"whsec_••••{endpoint.secret_last4}",
        disabled_reason=endpoint.disabled_reason, last_delivery=last, failing_since=endpoint.failing_since, row_version=endpoint.row_version, created_at=endpoint.created_at,
    )


def _load(db: Session, membership: Membership, webhook_id: UUID, *, lock: bool = False) -> WebhookEndpoint:
    statement = select(WebhookEndpoint).where(WebhookEndpoint.id == webhook_id, WebhookEndpoint.tenant_id == membership.tenant_id)
    endpoint = db.scalar(statement.with_for_update() if lock else statement)
    if endpoint is None:
        raise ApiError(404, "not_found", "未找到指定端点")
    return endpoint


def _audit(db: Session, membership: Membership, action: str, endpoint: WebhookEndpoint) -> None:
    db.add(AuditEvent(tenant_id=membership.tenant_id, actor_user_id=membership.user_id, action=action, details={"webhook_id": str(endpoint.id), "url": endpoint.url[:200]}))


@router.get("/webhooks", response_model=WebhookList)
def list_webhooks(membership: Membership = Depends(require_tenant_permission("settings:view")), db: Session = Depends(get_db)) -> WebhookList:
    rows = db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.tenant_id == membership.tenant_id).order_by(WebhookEndpoint.created_at.desc()))
    return WebhookList(items=[_response(e) for e in rows], events=list(EVENT_TYPES))


@router.post("/webhooks", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED)
def create_webhook(
    payload: WebhookWrite,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WebhookCreated:
    """The signing secret is in this response and nowhere else."""
    url = validate_public_https_url(payload.url)  # an internal address is refused here, and again on every delivery
    if len(list(db.scalars(select(WebhookEndpoint.id).where(WebhookEndpoint.tenant_id == membership.tenant_id)))) >= MAX_ENDPOINTS:
        raise ApiError(409, "webhook.limit", f"最多添加 {MAX_ENDPOINTS} 个端点")
    secret = service.new_secret()
    endpoint = WebhookEndpoint(tenant_id=membership.tenant_id, url=url, events=payload.events, enabled=payload.enabled, created_by=membership.user_id, secret_encrypted="", secret_last4="")
    service.seal_secret(endpoint, secret)
    db.add(endpoint)
    db.flush()
    _audit(db, membership, "webhook.created", endpoint)
    db.commit()
    return WebhookCreated(**_response(endpoint).model_dump(), signing_secret=secret)


@router.put("/webhooks/{webhook_id}", response_model=WebhookResponse)
def update_webhook(
    webhook_id: UUID,
    payload: WebhookWrite,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WebhookResponse:
    endpoint = _load(db, membership, webhook_id, lock=True)
    if payload.row_version is not None:
        check_row_version(endpoint, payload.row_version, db)
    if payload.url != endpoint.url:
        endpoint.url = validate_public_https_url(payload.url)
    endpoint.events = payload.events
    if payload.enabled and not endpoint.enabled:
        endpoint.failing_since = None  # turned on again by hand: a new run of failures starts from zero
    endpoint.enabled = payload.enabled
    if payload.enabled:
        endpoint.disabled_reason = None
    endpoint.row_version += 1
    _audit(db, membership, "webhook.updated", endpoint)
    db.commit()
    return _response(endpoint)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    webhook_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    endpoint = _load(db, membership, webhook_id, lock=True)
    _audit(db, membership, "webhook.deleted", endpoint)
    db.execute(delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id))
    db.delete(endpoint)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/webhooks/{webhook_id}/rotate-secret", response_model=WebhookCreated)
def rotate_secret(
    webhook_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> WebhookCreated:
    """A new signing secret, shown once. Deliveries still waiting to be retried are signed with the new one."""
    endpoint = _load(db, membership, webhook_id, lock=True)
    secret = service.new_secret()
    service.seal_secret(endpoint, secret)
    endpoint.row_version += 1
    _audit(db, membership, "webhook.secret_rotated", endpoint)
    db.commit()
    return WebhookCreated(**_response(endpoint).model_dump(), signing_secret=secret)


@router.post("/webhooks/{webhook_id}/test", response_model=DeliveryResponse, status_code=status.HTTP_202_ACCEPTED)
def test_webhook(
    webhook_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> DeliveryResponse:
    """Sends a ``webhook.test`` event now (even to a switched-off endpoint, so a fix can be checked before turning it on)."""
    endpoint = _load(db, membership, webhook_id)
    delivery = service.queue_delivery(db, endpoint, uuid4(), service.TEST_EVENT, {"message": "这是来自 BeeWork 的测试事件", "sent_at": utc_now().isoformat()}, is_test=True)
    _audit(db, membership, "webhook.tested", endpoint)
    db.commit()
    return _delivery(delivery)


def _delivery(row: WebhookDelivery) -> DeliveryResponse:
    return DeliveryResponse(
        id=row.id, event=row.event_type, status=row.status, attempts=row.attempts, status_code=row.status_code, duration_ms=row.duration_ms, error=row.error,
        is_test=row.is_test, created_at=row.created_at, last_attempt_at=row.last_attempt_at, next_retry_at=row.next_retry_at,
    )


@router.get("/webhooks/{webhook_id}/deliveries", response_model=DeliveryList)
def list_deliveries(webhook_id: UUID, membership: Membership = Depends(require_tenant_permission("settings:view")), db: Session = Depends(get_db)) -> DeliveryList:
    endpoint = _load(db, membership, webhook_id)
    rows = db.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id).order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id).limit(service.KEEP_DELIVERIES))
    return DeliveryList(items=[_delivery(r) for r in rows])


@router.post("/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", response_model=DeliveryResponse, status_code=status.HTTP_202_ACCEPTED)
def redeliver(
    webhook_id: UUID,
    delivery_id: UUID,
    membership: Membership = Depends(require_tenant_permission("settings:edit")),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> DeliveryResponse:
    """Send the same delivery again (same id, same body). The receiver sees the delivery id it already knows."""
    endpoint = _load(db, membership, webhook_id)
    delivery = db.scalar(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id, WebhookDelivery.endpoint_id == endpoint.id))
    if delivery is None:
        raise ApiError(404, "not_found", "未找到指定投递")
    delivery.status, delivery.next_retry_at = "pending", None
    delivery.created_at = utc_now()  # a manual resend gets a new 24 hour window
    service.enqueue(
        db, "webhooks.deliver", {"delivery_id": str(delivery.id)}, tenant_id=delivery.tenant_id,
        idempotency_key=f"webhooks.deliver:{delivery.id}:manual:{delivery.attempts}",
    )
    _audit(db, membership, "webhook.redelivered", endpoint)
    db.commit()
    return _delivery(delivery)
