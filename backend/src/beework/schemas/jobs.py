from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    payload: dict
    tenant_id: UUID | None
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_by: str | None
    locked_at: datetime | None
    last_error: str | None
    idempotency_key: str | None
    created_at: datetime
    finished_at: datetime | None

    @model_validator(mode="before")
    @classmethod
    def hide_sealed_payload(cls, data: Any) -> Any:
        """A mail job's payload holds the encrypted recipient and context: the jobs page shows the delivery it belongs to, nothing more."""
        payload = getattr(data, "payload", None) if not isinstance(data, dict) else data.get("payload")
        if isinstance(payload, dict) and "sealed" in payload:
            safe = {k: v for k, v in payload.items() if k != "sealed"}
            if isinstance(data, dict):
                return {**data, "payload": safe}
            return {name: getattr(data, name) for name in cls.model_fields} | {"payload": safe}
        return data
