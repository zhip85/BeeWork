from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from beework.api.conventions import ApiError, Page, Pagination, get_pagination, paginate
from beework.auth import get_tenant_membership, get_tenant_membership_for_own_state, require_csrf
from beework.db import get_db
from beework.models import Membership, utc_now
from beework.models.notifications import InAppNotification
from beework.schemas.notifications import MarkedRead, NotificationItem, UnreadCount

router = APIRouter()


def _mine(membership: Membership):
    return (
        InAppNotification.tenant_id == membership.tenant_id,
        InAppNotification.recipient_id == membership.user_id,
    )


def _item(row: InAppNotification) -> NotificationItem:
    return NotificationItem(
        id=row.id, type=row.type, title=row.title, body=row.body, params=row.params or {},
        link=row.link, read_at=row.read_at, created_at=row.created_at,
    )


@router.get("/notifications", response_model=Page[NotificationItem])
def list_notifications(
    unread: bool = False,
    pagination: Pagination = Depends(get_pagination),
    membership: Membership = Depends(get_tenant_membership),
    db: Session = Depends(get_db),
) -> Page[NotificationItem]:
    statement = select(InAppNotification).where(*_mine(membership))
    if unread:
        statement = statement.where(InAppNotification.read_at.is_(None))
    rows, total = paginate(
        db, statement.order_by(InAppNotification.created_at.desc(), InAppNotification.id), pagination
    )
    return Page[NotificationItem](items=[_item(r) for r in rows], total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/notifications/unread-count", response_model=UnreadCount)
def unread_count(
    membership: Membership = Depends(get_tenant_membership), db: Session = Depends(get_db)
) -> UnreadCount:
    count = db.scalar(
        select(func.count()).select_from(InAppNotification).where(*_mine(membership), InAppNotification.read_at.is_(None))
    )
    return UnreadCount(unread=count or 0)


@router.post("/notifications/read-all", response_model=MarkedRead)
def read_all(
    membership: Membership = Depends(get_tenant_membership_for_own_state),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MarkedRead:
    result = db.execute(
        update(InAppNotification)
        .where(*_mine(membership), InAppNotification.read_at.is_(None))
        .values(read_at=utc_now())
    )
    db.commit()
    return MarkedRead(updated=result.rowcount or 0)


@router.post("/notifications/{notification_id}/read", response_model=NotificationItem)
def read_one(
    notification_id: UUID,
    membership: Membership = Depends(get_tenant_membership_for_own_state),
    _: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> NotificationItem:
    row = db.scalar(select(InAppNotification).where(InAppNotification.id == notification_id, *_mine(membership)))
    if row is None:
        raise ApiError(404, "not_found", "未找到指定通知")
    if row.read_at is None:
        row.read_at = utc_now()
        db.commit()
    return _item(row)
