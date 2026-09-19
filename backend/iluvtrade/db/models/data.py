"""Dataset tables: source provenance, immutable versions, quality evidence.

The split is deliberate and matches PHASE 3's requirement that a user can
understand what happened to their data:

``DataSource``
    Where bytes came from, byte-for-byte: the original filename or URL, the
    size, the SHA-256 of exactly what arrived, and when. Never rewritten.
``Dataset``
    A stable logical name the user owns ("NIFTY 5-minute bars").
``DatasetVersion``
    One immutable canonicalization of one source under one cleaning policy,
    carrying its schema detection, its quality report and the full transformation
    log. A backtest references *this*, never the logical dataset, so a run's
    evidence cannot change under it.

Market data itself is **not** in these tables. ``canonical_path`` points at a
file under the storage root (PHASE 17).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from iluvtrade.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin, UtcDateTime


class SourceKind(enum.StrEnum):
    UPLOAD = "upload"
    HTTP = "http"


class DatasetVersionStatus(enum.StrEnum):
    """A version's position in the review workflow.

    ``PENDING_APPROVAL`` is the point PHASE 3 requires: cleaning has run, the
    quality report exists, and nothing may use the data until a human says so.
    """

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class DataSource(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Immutable provenance for one retrieval of one payload."""

    __tablename__ = "data_sources"

    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind, native_enum=False), nullable=False)
    #: Original filename for an upload; the final URL for a fetch.
    origin: Mapped[str] = mapped_column(String(2000), nullable=False)
    #: The URL as requested, before redirects. Null for uploads.
    requested_uri: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    #: Path, relative to the storage root, of the bytes exactly as received.
    raw_path: Mapped[str] = mapped_column(String(500), nullable=False)


class Dataset(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A named dataset the user owns. Versions hang off it."""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_dataset_slug"),)

    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)

    versions: Mapped[list[DatasetVersion]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="DatasetVersion.version"
    )


class DatasetVersion(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One immutable canonicalization, with its evidence.

    Nothing here is updated after ``status`` leaves ``PENDING_APPROVAL`` except
    the approval stamps themselves. A different cleaning policy produces a *new*
    version, which is what keeps a finished backtest's inputs pinned.
    """

    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uq_dataset_version"),)

    dataset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("data_sources.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DatasetVersionStatus] = mapped_column(
        Enum(DatasetVersionStatus, native_enum=False), nullable=False
    )

    # --- detection and evidence (all JSON documents, all written once) ----
    schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    quality_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    transformations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    cleaning_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # --- summary, denormalized for listing screens ------------------------
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    symbol_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)
    inferred_frequency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Path to the canonical records, relative to the storage root. Null while a
    #: version is ``FAILED``.
    canonical_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: SHA-256 of the canonical file. A backtest records this, so "the dataset
    #: changed" is detectable rather than assumed impossible.
    canonical_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")
