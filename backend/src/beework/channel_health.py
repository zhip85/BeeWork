"""Channel health checks (T-041, CHN-002 状态“异常”, PRD 5.8).

The worker asks each live channel's adapter whether it is healthy. After ``UNHEALTHY_AFTER`` failures in a row the channel is marked
“异常” and the tenant's administrators are notified; the first success after that sets it back to “启用” and tells them. A channel
keeps serving while it is in error — the next check may recover it — so this never stops traffic by itself.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from beework.channel_adapters import adapter_for
from beework.events import emit_event
from beework.models import AuditEvent, utc_now
from beework.models.channels import ChannelInstance
from beework.notifications import notify

log = logging.getLogger(__name__)
UNHEALTHY_AFTER = 3


def check_channel(db: Session, channel: ChannelInstance, now: datetime | None = None) -> str | None:
    """One check. Returns ``"error"`` / ``"recovered"`` when the status changed, else ``None``."""
    try:
        result = adapter_for(channel.type).health_check(channel)
    except Exception:  # noqa: BLE001 — an adapter bug counts as a failed check, it must not stop the job
        log.warning("channel %s health check crashed", channel.id)
        from beework.channel_adapters import HealthResult

        result = HealthResult(False, "健康检查执行失败")
    if result is None:
        return None
    channel.health_checked_at = now or utc_now()
    if result.ok:
        channel.health_failures, channel.health_error = 0, None
        if channel.status == "error":
            channel.status = "active"
            channel.row_version += 1
            db.add(AuditEvent(tenant_id=channel.tenant_id, action="channel.health_recovered", details={"channel_id": str(channel.id), "name": channel.name}))
            notify(db, tenant_id=channel.tenant_id, role="tenant_admin", type="channel.recovered", title=f"渠道“{channel.name}”已恢复正常", link="/channels", params={"name": channel.name})
            return "recovered"
        return None
    channel.health_failures += 1
    channel.health_error = (result.detail or "健康检查失败")[:200]
    if channel.health_failures >= UNHEALTHY_AFTER and channel.status == "active":
        channel.status = "error"
        channel.row_version += 1
        db.add(AuditEvent(tenant_id=channel.tenant_id, action="channel.health_failed", details={"channel_id": str(channel.id), "name": channel.name, "error": channel.health_error}))
        emit_event(db, channel.tenant_id, "channel.unhealthy", {"channel_id": str(channel.id), "name": channel.name, "type": channel.type})
        notify(
            db, tenant_id=channel.tenant_id, role="tenant_admin", type="channel.unhealthy", title=f"渠道“{channel.name}”连续检查失败，已标为异常",
            body=channel.health_error, link="/channels", params={"name": channel.name},
        )
        return "error"
    return None


def channel_health_job(db: Session, _payload: dict) -> None:
    channels = list(db.scalars(select(ChannelInstance).where(ChannelInstance.status.in_(("active", "error")), ChannelInstance.published_snapshot.is_not(None))))
    for channel in channels:
        check_channel(db, channel)
        db.commit()
