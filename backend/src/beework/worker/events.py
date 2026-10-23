import json
import logging
import time
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from beework.config import settings
from beework.models.jobs import RunEvent

logger = logging.getLogger(__name__)

# A stream ends after yielding one of these.
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "cancelled"})


def _valkey_client():
    return redis.Redis.from_url(settings.valkey_url, decode_responses=True)


def publish_run_event(
    db: Session,
    run_id: UUID,
    event_type: str,
    data: dict[str, Any],
    *,
    tenant_id: UUID,
    redis_client=None,
) -> RunEvent:
    """Persist the event, commit, then wake subscribers through Valkey.

    The database is the source of truth: a failed Valkey publish is logged and
    never loses the event, because streams also catch up from the table.
    """
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:run_id, 0))"), {"run_id": str(run_id)})
    seq = (db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0) + 1
    event = RunEvent(
        run_id=run_id,
        tenant_id=tenant_id,
        seq=seq,
        type=event_type,
        data=data,
        created_at=datetime.now(UTC),
    )
    db.add(event)
    db.commit()
    payload = json.dumps(_event_payload(event), default=str)
    client = redis_client or _valkey_client()
    try:
        client.publish(f"run:{run_id}", payload)
    except redis.RedisError:
        logger.warning("Valkey publish failed for run %s; subscribers will catch up from the table", run_id)
    finally:
        if redis_client is None:
            client.close()
    return event


def run_event_tenant_id(db: Session, run_id: UUID) -> UUID | None:
    return db.scalar(select(RunEvent.tenant_id).where(RunEvent.run_id == run_id).limit(1))


def stream_run_events(
    db: Session,
    run_id: UUID,
    after_seq: int = 0,
    *,
    redis_client=None,
    poll_seconds: float = 5.0,
    heartbeat: bool = False,
) -> Generator[dict[str, Any] | None, None, None]:
    """Yield a run's events in order: durable history first, then live ones.

    The table is the only source of event content. Valkey messages merely wake the
    loop early; ``poll_seconds`` bounds the wait so nothing published while
    subscribing, or while Valkey is down, is ever missed. The subscription is
    opened *before* the first read so there is no gap between history and live.
    The stream ends after a terminal event. With ``heartbeat`` it yields ``None``
    on idle ticks so the HTTP layer can notice a disconnected client.
    """
    pubsub = None
    client = None
    try:
        client = redis_client or _valkey_client()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(f"run:{run_id}")
    except redis.RedisError:
        logger.warning("Valkey unavailable; streaming run %s by polling the table", run_id)
        pubsub = None
    last_seq = after_seq
    try:
        while True:
            events = list(
                db.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > last_seq)
                    .order_by(RunEvent.seq)
                )
            )
            db.rollback()  # do not hold a connection while waiting
            for event in events:
                last_seq = event.seq
                yield _event_payload(event)
                if event.type in TERMINAL_EVENT_TYPES:
                    return
            if not events and heartbeat:
                yield None
            _wait(pubsub, poll_seconds)
    finally:
        if pubsub is not None:
            pubsub.close()
        if client is not None and redis_client is None:
            client.close()


def _wait(pubsub, seconds: float) -> None:
    if pubsub is None:
        time.sleep(seconds)
        return
    try:
        pubsub.get_message(timeout=seconds)
    except redis.RedisError:
        time.sleep(seconds)


def _event_payload(event: RunEvent) -> dict[str, Any]:
    return {"seq": event.seq, "type": event.type, "data": event.data, "created_at": event.created_at.isoformat()}
