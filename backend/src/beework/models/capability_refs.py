from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base, utc_now
from beework.models.mixins import TenantOwnedMixin


class ResourceReference(Base, TenantOwnedMixin):
    """Who points at what (T-070, GLB-083, RUL-011/012). The rows of one referrer and layer are replaced as a whole
    whenever that referrer's configuration is saved or published — never edited one by one.

    ``referrer_type``: ``assistant`` | ``flow``. ``layer``: ``draft`` | ``live``. ``relation``: ``mount`` |
    ``node`` | ``route_target`` | ``fallback``. ``target_type``: ``kb`` | ``assistant`` | ``flow`` | ``skill`` |
    ``mcp`` | ``api`` | ``chatflow_connection`` | ``workflow_connection``.
    """

    __tablename__ = "resource_references"
    __table_args__ = (
        UniqueConstraint(
            "referrer_type", "referrer_id", "layer", "relation", "target_type", "target_id",
            name="uq_resource_references_edge",
        ),
        Index("ix_resource_references_target", "tenant_id", "target_type", "target_id"),
        Index("ix_resource_references_referrer", "referrer_type", "referrer_id", "layer"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    referrer_type: Mapped[str] = mapped_column(String(16), nullable=False)
    referrer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    layer: Mapped[str] = mapped_column(String(8), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
