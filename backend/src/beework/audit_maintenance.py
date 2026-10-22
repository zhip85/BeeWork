import argparse
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from beework.db import SessionLocal
from beework.models import (
    AuditChainCheckpoint,
    AuditEvent,
    _audit_event_hash,
    _lock_audit_chain,
)

AUDIT_RETENTION_DAYS = 365


def verify_audit_chain(db: Session, chain_scope: str) -> dict[str, int | str | bool]:
    checkpoint = db.get(AuditChainCheckpoint, chain_scope)
    checkpoint_sequence = checkpoint.through_sequence if checkpoint else 0
    previous_hash = checkpoint.through_hash if checkpoint else None
    expected_sequence = checkpoint_sequence + 1
    checked = 0
    is_valid = True
    events = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.chain_scope == chain_scope,
            AuditEvent.chain_sequence.is_not(None),
        )
        .order_by(AuditEvent.chain_sequence)
    )
    for audit_event in events:
        if (
            audit_event.chain_sequence != expected_sequence
            or audit_event.previous_hash != previous_hash
            or audit_event.event_hash != _audit_event_hash(audit_event)
        ):
            is_valid = False
            break
        checked += 1
        expected_sequence += 1
        previous_hash = audit_event.event_hash

    unchained_count = db.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.chain_scope == chain_scope,
            AuditEvent.chain_sequence.is_(None),
        )
    ) or 0
    return {
        "chain_scope": chain_scope,
        "is_valid": is_valid,
        "checked_events": checked,
        "unchained_legacy_events": unchained_count,
        "through_sequence": expected_sequence - 1 if is_valid else expected_sequence,
    }


def prune_expired_audit_events(
    db: Session,
    *,
    retention_days: int = AUDIT_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    if retention_days < 1:
        raise ValueError("retention_days must be at least one")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    cutoff = current_time - timedelta(days=retention_days)

    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('beework.audit_retention', 'on', true)")
        )

    removed = 0
    chain_scopes = list(db.scalars(select(AuditEvent.chain_scope).distinct()))
    for chain_scope in chain_scopes:
        if db.bind and db.bind.dialect.name == "postgresql":
            _lock_audit_chain(db.connection(), chain_scope)
        checkpoint = db.get(AuditChainCheckpoint, chain_scope)
        checkpoint_sequence = checkpoint.through_sequence if checkpoint else 0
        checkpoint_hash = checkpoint.through_hash if checkpoint else None
        events = db.execute(
            select(
                AuditEvent.chain_sequence,
                AuditEvent.created_at,
                AuditEvent.event_hash,
            )
            .where(
                AuditEvent.chain_scope == chain_scope,
                AuditEvent.chain_sequence > checkpoint_sequence,
            )
            .order_by(AuditEvent.chain_sequence)
        )

        prune_through = checkpoint_sequence
        prune_hash = checkpoint_hash
        for sequence, created_at, event_hash in events:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if created_at >= cutoff:
                break
            prune_through = sequence
            prune_hash = event_hash

        if prune_through > checkpoint_sequence and prune_hash:
            if checkpoint:
                checkpoint.through_sequence = prune_through
                checkpoint.through_hash = prune_hash
                checkpoint.updated_at = current_time
            else:
                db.add(
                    AuditChainCheckpoint(
                        chain_scope=chain_scope,
                        through_sequence=prune_through,
                        through_hash=prune_hash,
                        updated_at=current_time,
                    )
                )
            result = db.execute(
                delete(AuditEvent).where(
                    AuditEvent.chain_scope == chain_scope,
                    AuditEvent.chain_sequence <= prune_through,
                )
            )
            removed += result.rowcount or 0

        legacy_result = db.execute(
            delete(AuditEvent).where(
                AuditEvent.chain_scope == chain_scope,
                AuditEvent.chain_sequence.is_(None),
                AuditEvent.created_at < cutoff,
            )
        )
        removed += legacy_result.rowcount or 0

    db.commit()
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune expired BeeWork audit events.")
    parser.add_argument("--retention-days", type=int, default=AUDIT_RETENTION_DAYS)
    parser.add_argument(
        "--interval-seconds",
        type=int,
        help="Repeat the cleanup after this many seconds; omit for a single run.",
    )
    args = parser.parse_args()
    if args.interval_seconds is not None and args.interval_seconds < 60:
        parser.error("--interval-seconds must be at least 60")

    while True:
        with SessionLocal() as db:
            removed = prune_expired_audit_events(
                db,
                retention_days=args.retention_days,
            )
        print(
            f"Removed {removed} audit events older than {args.retention_days} days.",
            flush=True,
        )
        if args.interval_seconds is None:
            return
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
