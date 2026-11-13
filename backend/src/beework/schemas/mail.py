"""Platform-facing mail delivery records (T-008). Recipients only ever leave as masks."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MailDeliveryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None
    job_id: UUID | None
    template: str
    locale: str
    recipient_masked: str
    status: str
    error: str | None
    attempts: int
    sent_at: datetime | None
    created_at: datetime
