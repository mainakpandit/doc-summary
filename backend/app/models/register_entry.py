import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models import Base


class RegisterEntry(Base):
    __tablename__ = "register_entries"
    __table_args__ = (
        UniqueConstraint(
            "corpus_id", "feature_key", name="register_entries_corpus_id_feature_key_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    corpus_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corpora.id"), nullable=False
    )
    feature_key: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_updated_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=True
    )

    field_sources: Mapped[list["RegisterFieldSource"]] = relationship(lazy="selectin")


class RegisterFieldSource(Base):
    __tablename__ = "register_field_sources"

    register_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("register_entries.id", ondelete="CASCADE"), primary_key=True
    )
    field_name: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id"), primary_key=True
    )
