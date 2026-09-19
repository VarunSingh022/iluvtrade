"""The one canonicalization path, and the two source boundaries that feed it.

``ingest_upload`` and ``ingest_url`` differ only in how they obtain bytes and
what provenance they record. Both then call :func:`_canonicalize`, so there is
exactly one implementation of detect → validate → clean → report → store.

A version lands in ``PENDING_APPROVAL``. Nothing may use it until
:func:`approve` is called, which is the user-approval gate PHASE 3 requires
between the quality report and the canonical dataset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.common import storage
from iluvtrade.config import Settings, get_settings
from iluvtrade.data import canonical, quality
from iluvtrade.data.cleaning import CleaningPolicy, CleaningResult, clean
from iluvtrade.data.fetch import FetchError, fetch_csv
from iluvtrade.data.schema import DetectedSchema, detect_schema
from iluvtrade.db.base import new_id, utcnow
from iluvtrade.db.models.data import (
    Dataset,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    SourceKind,
)
from iluvtrade.platform import audit, notifications
from iluvtrade.platform.accounts import Principal, slugify
from iluvtrade.platform.tenancy import NotFoundError, require_owned, scoped

__all__ = [
    "IngestError",
    "IngestOutcome",
    "approve",
    "ingest_upload",
    "ingest_url",
    "inspect_bytes",
    "reject",
]


class IngestError(ValueError):
    """The payload could not be ingested. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What one ingestion produced."""

    dataset: Dataset
    version: DatasetVersion
    source: DataSource
    schema: DetectedSchema
    report: quality.QualityReport
    cleaning: CleaningResult


def _decode(payload: bytes) -> str:
    """Decode CSV bytes, trying the encodings that actually turn up.

    ``utf-8-sig`` first because Excel writes a BOM, and a BOM left in place
    makes the first column name ``\\ufeffDate`` — which then matches no alias and
    fails detection for a reason no user could guess.
    """

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError("The file is not text in any encoding this importer recognises.")


def inspect_bytes(payload: bytes) -> dict[str, Any]:
    """Detect a schema without storing anything. Powers the upload preview."""

    text = _decode(payload)
    schema = detect_schema(text)
    return schema.to_dict()


def _next_version(session: DbSession, dataset_id: str) -> int:
    highest = session.execute(
        select(func.max(DatasetVersion.version)).where(DatasetVersion.dataset_id == dataset_id)
    ).scalar_one_or_none()
    return int(highest or 0) + 1


def _dataset_for(session: DbSession, principal: Principal, name: str, description: str) -> Dataset:
    """Find the user's dataset of this name, or create it."""

    slug = slugify(name, fallback="dataset")
    existing = session.execute(
        scoped(Dataset, principal.organization_id).where(Dataset.slug == slug)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    dataset = Dataset(
        organization_id=principal.organization_id,
        slug=slug,
        name=name.strip() or slug,
        description=description,
        created_by_user_id=principal.user_id,
    )
    session.add(dataset)
    session.flush()
    return dataset


def _canonicalize(
    session: DbSession,
    principal: Principal,
    *,
    dataset: Dataset,
    source: DataSource,
    payload: bytes,
    policy: CleaningPolicy,
) -> IngestOutcome:
    """Detect, clean, report and store. The single canonicalization path."""

    text = _decode(payload)
    schema = detect_schema(text)
    result = clean(text, schema, policy)
    report = quality.analyse(result, schema)

    version = DatasetVersion(
        id=new_id(),
        organization_id=principal.organization_id,
        dataset_id=dataset.id,
        source_id=source.id,
        version=_next_version(session, dataset.id),
        status=(
            DatasetVersionStatus.FAILED
            if (result.fatal or not result.rows)
            else DatasetVersionStatus.PENDING_APPROVAL
        ),
        schema_json=json.dumps(schema.to_dict(), default=str),
        quality_json=json.dumps(report.to_dict(), default=str),
        transformations_json=json.dumps(
            {
                "logged": [t.to_dict() for t in result.transformations],
                "counts": dict(result.transformation_counts),
                "logged_is_truncated": result.transformation_total > len(result.transformations),
            },
            default=str,
        ),
        cleaning_policy_json=json.dumps(policy.to_dict()),
        row_count=len(result.rows),
        rejected_row_count=result.rejected_count,
        symbol_count=report.symbol_count,
        start_timestamp=report.start_timestamp,
        end_timestamp=report.end_timestamp,
        inferred_frequency=report.inferred_frequency,
        quality_score=report.score,
        failure_reason=result.fatal,
    )

    if result.rows:
        key = canonical.canonical_key(principal.organization_id, version.id)
        digest, count = storage.write_jsonl(key, (row.to_dict() for row in result.rows))
        version.canonical_path = key
        version.canonical_hash = digest
        version.row_count = count

    storage.write_json(
        canonical.rejected_key(principal.organization_id, version.id),
        {
            "logged": [r.to_dict() for r in result.rejected],
            "counts": dict(result.rejection_counts),
            "logged_is_truncated": result.rejected_count > len(result.rejected),
        },
    )

    session.add(version)
    session.flush()

    failed = version.status is DatasetVersionStatus.FAILED
    notifications.notify(
        session,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        kind="dataset.failed" if failed else "dataset.ready",
        title=(
            f"{dataset.name} could not be processed"
            if failed
            else f"{dataset.name} v{version.version} is ready for review"
        ),
        body=(
            (version.failure_reason or "No usable bars could be built from the file.")
            if failed
            else (
                f"{version.row_count} row(s) accepted, {version.rejected_row_count} rejected. "
                "Nothing can use it until you approve it."
            )
        ),
        resource_type="dataset_version",
        resource_id=version.id,
    )
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="dataset.version.created",
        resource_type="dataset_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        outcome="success" if version.status is not DatasetVersionStatus.FAILED else "failure",
        payload={
            "dataset_id": dataset.id,
            "version": version.version,
            "rows": version.row_count,
            "rejected": version.rejected_row_count,
            "score": report.score,
            "source_hash": source.content_hash,
        },
    )
    return IngestOutcome(dataset, version, source, schema, report, result)


def ingest_upload(
    session: DbSession,
    principal: Principal,
    *,
    filename: str,
    payload: bytes,
    dataset_name: str | None = None,
    description: str = "",
    policy: CleaningPolicy | None = None,
    settings: Settings | None = None,
) -> IngestOutcome:
    """Ingest bytes a user uploaded."""

    settings = settings or get_settings()
    if not payload:
        raise IngestError("The uploaded file is empty.")
    if len(payload) > settings.max_upload_bytes:
        raise IngestError(
            f"The file is {len(payload)} bytes; the limit is {settings.max_upload_bytes}."
        )

    safe_name = (filename or "upload.csv").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:200]
    source = DataSource(
        id=new_id(),
        organization_id=principal.organization_id,
        kind=SourceKind.UPLOAD,
        origin=safe_name,
        requested_uri=None,
        content_hash=storage.sha256(payload),
        size_bytes=len(payload),
        content_type="text/csv",
        retrieved_at=utcnow(),
        created_by_user_id=principal.user_id,
        raw_path=canonical.raw_key(principal.organization_id, new_id()),
    )
    storage.write_bytes(source.raw_path, payload)
    session.add(source)
    session.flush()

    dataset = _dataset_for(
        session, principal, dataset_name or safe_name.rsplit(".", 1)[0], description
    )
    return _canonicalize(
        session,
        principal,
        dataset=dataset,
        source=source,
        payload=payload,
        policy=policy or CleaningPolicy(),
    )


def ingest_url(
    session: DbSession,
    principal: Principal,
    *,
    url: str,
    dataset_name: str | None = None,
    description: str = "",
    policy: CleaningPolicy | None = None,
    settings: Settings | None = None,
) -> IngestOutcome:
    """Fetch an approved URL and ingest what comes back."""

    settings = settings or get_settings()
    try:
        fetched = fetch_csv(url, settings)
    except FetchError as exc:
        audit.record(
            session,
            organization_id=principal.organization_id,
            action="dataset.fetch.refused",
            resource_type="data_source",
            actor_user_id=principal.user_id,
            outcome="failure",
            payload={"url": url, "reason": str(exc)},
        )
        raise IngestError(str(exc)) from exc

    source = DataSource(
        id=new_id(),
        organization_id=principal.organization_id,
        kind=SourceKind.HTTP,
        origin=fetched.final_url,
        requested_uri=fetched.requested_url,
        content_hash=storage.sha256(fetched.content),
        size_bytes=len(fetched.content),
        content_type=fetched.content_type,
        retrieved_at=fetched.retrieved_at,
        created_by_user_id=principal.user_id,
        raw_path=canonical.raw_key(principal.organization_id, new_id()),
    )
    storage.write_bytes(source.raw_path, fetched.content)
    session.add(source)
    session.flush()

    fallback = fetched.final_url.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "fetched-dataset"
    dataset = _dataset_for(session, principal, dataset_name or fallback, description)
    return _canonicalize(
        session,
        principal,
        dataset=dataset,
        source=source,
        payload=fetched.content,
        policy=policy or CleaningPolicy(),
    )


def approve(session: DbSession, principal: Principal, version_id: str) -> DatasetVersion:
    """Mark a pending version usable. The gate between cleaning and research."""

    version = require_owned(session, DatasetVersion, version_id, principal.organization_id)
    if version.status is DatasetVersionStatus.APPROVED:
        return version
    if version.status is not DatasetVersionStatus.PENDING_APPROVAL:
        raise IngestError(f"A {version.status.value} dataset version cannot be approved.")
    version.status = DatasetVersionStatus.APPROVED
    version.approved_at = utcnow()
    version.approved_by_user_id = principal.user_id
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="dataset.version.approved",
        resource_type="dataset_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        payload={"version": version.version, "rows": version.row_count},
    )
    return version


def reject(
    session: DbSession, principal: Principal, version_id: str, reason: str = ""
) -> DatasetVersion:
    """Refuse a pending version. It stays for the record; it cannot be used."""

    version = require_owned(session, DatasetVersion, version_id, principal.organization_id)
    if version.status not in (
        DatasetVersionStatus.PENDING_APPROVAL,
        DatasetVersionStatus.APPROVED,
    ):
        raise IngestError(f"A {version.status.value} dataset version cannot be rejected.")
    version.status = DatasetVersionStatus.REJECTED
    version.failure_reason = reason or "Rejected by a reviewer."
    audit.record(
        session,
        organization_id=principal.organization_id,
        action="dataset.version.rejected",
        resource_type="dataset_version",
        resource_id=version.id,
        actor_user_id=principal.user_id,
        payload={"reason": reason},
    )
    return version


def require_approved(session: DbSession, organization_id: str, version_id: str) -> DatasetVersion:
    """Load a version that research is allowed to read, or refuse.

    The refusal is what stops a backtest running on data nobody reviewed.
    """

    version = require_owned(session, DatasetVersion, version_id, organization_id)
    if version.status is not DatasetVersionStatus.APPROVED:
        raise NotFoundError(
            f"Dataset version {version_id} is {version.status.value}, not approved. "
            "Approve it in the data workspace before using it."
        )
    if not version.canonical_path:
        raise NotFoundError(f"Dataset version {version_id} has no canonical data.")
    return version
