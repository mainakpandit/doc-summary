import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Table, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.models import Base


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'error')", name="findings_severity_check"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="findings_status_check"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    reviewer: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# finding_sources (migration 001) has no primary key and no unique
# constraint (`finding_id` NOT NULL; `chunk_id`/`claim_id` both nullable),
# so it isn't a declarative class — see docs/assumptions.md. Mapped as a
# bare Core Table sharing Base.metadata instead; populated with
# `insert(finding_sources)` by agent/nodes/examine.py, the first caller
# that needs to write to it.
finding_sources = Table(
    "finding_sources",
    Base.metadata,
    Column(
        "finding_id",
        UUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chunk_id", UUID(as_uuid=True), ForeignKey("chunks.id"), nullable=True),
    Column("claim_id", UUID(as_uuid=True), ForeignKey("claims.id"), nullable=True),
)
