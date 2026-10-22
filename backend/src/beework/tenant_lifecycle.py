"""Tenant lifecycle state machine (PRD 3.12.6, 5.1)."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class LifecycleAction:
    label: str
    to_status: str
    requires_review: bool = False


LIFECYCLE_ACTIONS: dict[str, dict[str, LifecycleAction]] = {
    "trial": {
        "convert_to_active": LifecycleAction("转为正式", "active"),
        "extend_trial": LifecycleAction("延长试用 14 天", "trial"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "active": {
        "mark_grace": LifecycleAction("标记欠费（宽限）", "grace"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "grace": {
        "record_payment": LifecycleAction("登记回款", "active"),
        "make_read_only": LifecycleAction("转为只读", "read_only"),
    },
    "read_only": {
        "record_payment": LifecycleAction("登记回款", "active"),
        "suspend": LifecycleAction("停用", "suspended", requires_review=True),
    },
    "suspended": {
        "restore": LifecycleAction("恢复", "active"),
        "schedule_deletion": LifecycleAction("进入待删除", "pending_deletion", requires_review=True),
    },
    "pending_deletion": {
        "restore_to_suspended": LifecycleAction("恢复为停用", "suspended"),
        "delete_permanently": LifecycleAction("永久删除", "deleted", requires_review=True),
    },
    "deleted": {},
}

TRIAL_EXTENSION = timedelta(days=14)
REVIEW_TIMEOUT = timedelta(hours=72)


def lifecycle_action(current_status: str, action: str) -> LifecycleAction | None:
    return LIFECYCLE_ACTIONS.get(current_status, {}).get(action)
