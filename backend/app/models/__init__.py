"""Declarative base and model registry.

Importing this package registers every mapped class on `Base.metadata`.
Column shapes here must match `alembic/versions/001_initial.py` exactly;
that migration is hand-written SQL and is the source of truth for the
actual schema, not these models.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from backend.app.models.audit import AuditEvent
from backend.app.models.chunk import Chunk
from backend.app.models.claim import Claim, ClaimSource
from backend.app.models.conflict import Conflict
from backend.app.models.corpus import Corpus
from backend.app.models.cost import CostEvent
from backend.app.models.document import Document
from backend.app.models.finding import Finding, finding_sources
from backend.app.models.register_entry import RegisterEntry, RegisterFieldSource
from backend.app.models.review import Review
from backend.app.models.run import Run

__all__ = [
    "AuditEvent",
    "Base",
    "Chunk",
    "Claim",
    "ClaimSource",
    "Conflict",
    "Corpus",
    "CostEvent",
    "Document",
    "Finding",
    "RegisterEntry",
    "RegisterFieldSource",
    "Review",
    "Run",
    "finding_sources",
]
