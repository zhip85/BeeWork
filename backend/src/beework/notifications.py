"""In-app notifications (T-023): ``notify`` writes in the caller's transaction; other domains call it for their events."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework import approvals
from beework.models import Membership
from beework.models.approvals import ApprovalRequest
from beework.models.notifications import InAppNotification


def safe_link(link: str | None) -> str | None:
    """Notification links are in-app relative paths only; anything else would be an open redirect."""
    if link is None or link == "":
        return None
    if not link.startswith("/") or link.startswith("//") or "\\" in link or any(ch.isspace() or ord(ch) < 32 for ch in link):
        raise ValueError(f"通知链接必须是站内相对路径：{link!r}")
    return link


def notify(
    db: Session,
    *,
    tenant_id: UUID,
    type: str,
    title: str,
    body: str = "",
    link: str | None = None,
    params: dict[str, Any] | None = None,
    recipient_ids: Iterable[UUID] | None = None,
    role: str | None = None,
) -> list[InAppNotification]:
    """Notify the given users and/or every active member holding a built-in ``role``; one row per distinct user."""
    link = safe_link(link)
    recipients: dict[UUID, None] = dict.fromkeys(r for r in (recipient_ids or []) if r is not None)
    if role is not None:
        for user_id in db.scalars(
            select(Membership.user_id).where(
                Membership.tenant_id == tenant_id, Membership.status == "active", Membership.role == role
            )
        ):
            recipients[user_id] = None
    rows = [
        InAppNotification(
            tenant_id=tenant_id, recipient_id=user_id, type=type, title=title[:200], body=body[:500],
            params=params or {}, link=link,
        )
        for user_id in recipients
    ]
    db.add_all(rows)
    return rows


# ---------------------------------------------------------------------------
# Approval events (T-022 hook).
# ---------------------------------------------------------------------------

_APPROVAL_TITLES = {
    "approval.submitted": "待你审批：{title}",
    "approval.reminder": "审批待办已超过 24 小时：{title}",
    "approval.approved": "审批已通过：{title}",
    "approval.rejected": "审批被驳回：{title}",
    "approval.expired": "审批已过期：{title}",
    "approval.withdrawn": "审批已被撤回（已有更新的变更直接生效）：{title}",
    "approval.apply_failed": "审批通过但生效失败：{title}",
}


def _approval_notifier(db: Session, event: str, request: ApprovalRequest, recipients: list[UUID]) -> None:
    template = _APPROVAL_TITLES.get(event)
    if template is None or not recipients:
        return
    notify(
        db, tenant_id=request.tenant_id, type=event, title=template.format(title=request.title),
        link=f"/approvals?id={request.id}", params={"title": request.title}, recipient_ids=recipients,
    )


if _approval_notifier not in approvals.NOTIFIERS:
    approvals.NOTIFIERS.append(_approval_notifier)
