from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from beework.models import Base
from beework.models.mixins import TenantOwnedMixin, TimestampMixin, VersionedMixin


class WidgetTemplate(Base, TimestampMixin):
    """A platform "SaaS" component template (PLT-110–114). Tenants copy it; the template itself is read-only for them."""

    __tablename__ = "widget_templates"
    __table_args__ = (UniqueConstraint("trigger_tag", name="uq_widget_templates_tag"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name_zh: Mapped[str] = mapped_column(String(80), nullable=False)
    name_en: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # card | comparison | timeline | form
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    trigger_tag: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_text: Mapped[str] = mapped_column("schema", Text, nullable=False)
    sample_data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    compatibility: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # channel type -> native | text | json | off
    version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # published | draft | deprecated
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class TenantComponent(Base, TenantOwnedMixin, TimestampMixin, VersionedMixin):
    """A tenant's own component: a copy of a template (``template_id``) or built from scratch (CHN-040–046)."""

    __tablename__ = "tenant_components"
    __table_args__ = (
        UniqueConstraint("tenant_id", "trigger_tag", name="uq_tenant_components_tag"),
        UniqueConstraint("id", "tenant_id", name="uq_tenant_components_id_tenant_id"),
        Index("ix_tenant_components_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name_zh: Mapped[str] = mapped_column(String(80), nullable=False)
    name_en: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    trigger_tag: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_text: Mapped[str] = mapped_column("schema", Text, nullable=False)
    sample_data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    compatibility: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.1")  # bumps on every content change
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # enabled | draft | disabled
    template_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("widget_templates.id", ondelete="SET NULL"))
    template_version: Mapped[str | None] = mapped_column(String(16))
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
