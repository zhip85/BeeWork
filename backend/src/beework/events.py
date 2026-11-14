"""The event bus (T-087): ``emit_event`` writes an outbox row in the caller's transaction; the Worker hands it to the subscribers.

An event carries ids and a short summary — never the text of a conversation or a secret (the payload is what a Webhook receiver gets).
Subscribers are Webhook endpoints (``webhooks.py``) and, later, notification rules (T-088, which registers with ``subscribe``).
"""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from beework.models import utc_now
from beework.models.webhooks import DomainEvent
from beework.worker.queue import enqueue, job_handler

logger = logging.getLogger(__name__)

EVENT_TYPES = ("conversation.completed", "handoff.requested", "approval.updated", "usage.threshold", "assistant.published")
Subscriber = Callable[[Session, DomainEvent], None]
Interest = Callable[[Session, UUID, str], bool]
SUBSCRIBERS: list[Subscriber] = []
INTERESTS: dict[Subscriber, Interest] = {}


def subscribe(subscriber: Subscriber | None = None, *, interested: Interest | None = None):
    """Register a consumer of the events; the Worker calls it once per event and it must be idempotent (a retry calls it again).

    ``interested(db, tenant_id, event_type)`` is a cheap check made when the event is emitted: when no subscriber is interested the event is only
    recorded and no dispatch job is queued. Without it a subscriber is interested in everything."""

    def register(fn: Subscriber) -> Subscriber:
        SUBSCRIBERS.append(fn)
        if interested is not None:
            INTERESTS[fn] = interested
        return fn

    return register(subscriber) if subscriber is not None else register


def _anyone_listening(db: Session, tenant_id: UUID, event_type: str) -> bool:
    return any(INTERESTS[fn](db, tenant_id, event_type) if fn in INTERESTS else True for fn in SUBSCRIBERS)


def emit_event(db: Session, tenant_id: UUID, event_type: str, payload: dict[str, Any]) -> DomainEvent:
    """Record that something happened. Joins the caller's transaction: if it rolls back, the event never existed."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")
    event = DomainEvent(tenant_id=tenant_id, type=event_type, payload=payload)
    db.add(event)
    db.flush()
    if _anyone_listening(db, tenant_id, event_type):
        enqueue(db, "events.dispatch", {"event_id": str(event.id)}, tenant_id=tenant_id, idempotency_key=f"events.dispatch:{event.id}")
    else:
        event.dispatched_at = utc_now()  # recorded, with nobody to tell: no job, no queue noise
    return event


@job_handler("events.dispatch")
def dispatch_event(db: Session, payload: dict[str, Any]) -> None:
    event = db.get(DomainEvent, UUID(payload["event_id"]))
    if event is None or event.dispatched_at is not None:
        return
    for subscriber in list(SUBSCRIBERS):
        subscriber(db, event)  # a failing subscriber fails the job, which is retried; subscribers are idempotent
    event.dispatched_at = utc_now()
    db.commit()
