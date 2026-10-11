"""The one canonicalization path, and the two source boundaries that feed it.

``ingest_upload`` and ``ingest_url`` differ only in how they obtain bytes and
what provenance they record. Both then call :func:`_canonicalize`, so there is
exactly one implementation of detect → validate → clean → report → store.

A version lands in ``PENDING_APPROVAL``. Nothing may use it until
:func:`approve` is called, which is the user-approval gate PHASE 3 requires
between the quality report and the canonical dataset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from iluvtrade.alphalab_bridge import ALPHALAB_VERSION
from iluvtrade.common import storage
from iluvtrade.config import Settings, get_settings
from iluvtrade.data import canonical, quality
from iluvtrade.data import temporal as temporal_rules
from iluvtrade.data.cleaning import (
    CleaningPolicy,
    CleaningResult,
    Transformation,
    TransformationKind,
    clean,
)
from iluvtrade.data.fetch import FetchError, fetch_csv
from iluvtrade.data.schema import DetectedSchema, TimestampFormat, detect_schema
from iluvtrade.data.temporal import TemporalDeclaration, TimestampConvention, TimestampSurvey
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


def inspect_bytes(
    payload: bytes,
    declaration: TemporalDeclaration | None = None,
    policy: CleaningPolicy | None = None,
) -> dict[str, Any]:
    """Detect a schema without storing anything. Powers the upload preview.

    With a ``declaration``, also checks it against each instrument's bars as the
    import would (``interval_check``) and names the instruments whose own bars
    cannot show the declared interval -- the ones a missing-observation
    confirmation would have to name -- so the user decides about each before
    importing. ``content_sha256`` and ``preview_id`` (:func:`preview_identity`)
    bind that check to these bytes, the declaration and the cleaning ``policy``;
    an import confirming missing observations must send the ``preview_id`` back.
    """

    text = _decode(payload)
    schema = detect_schema(text)
    found = temporal_rules.survey(text, schema) if _readable(schema) else TimestampSurvey()
    # What the user has to declare before importing: the conventions that make
    # sense for these values, and whether they need a zone.
    detected = schema.to_dict() | {"timestamp_semantics": found.to_dict()}
    if declaration is not None and _readable(schema):
        effective = policy or CleaningPolicy()
        detected["interval_check"] = _interval_check(text, schema, declaration, found, effective)
        # Bound to the declaration as validation normalizes it (``session_date``
        # implies ``1d``; interval codes canonical), exactly as the import sees it.
        try:
            bound = temporal_rules.validate(declaration, found)
        except temporal_rules.TemporalError:
            bound = declaration  # refused above; nothing can be confirmed against it
        detected["interval_check"] |= preview_identity(payload, bound, effective)
    return detected


def inspect_url(
    url: str,
    declaration: TemporalDeclaration | None = None,
    policy: CleaningPolicy | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fetch an allowlisted URL and inspect what came back; store nothing.

    The fetch goes through the same allowlist, address checks and size limit as
    an import. The returned ``preview_id`` binds the check to the bytes fetched
    *now*: if the resource serves different bytes when it is imported, the
    confirmation made here is refused.
    """

    try:
        fetched = fetch_csv(url, settings or get_settings())
    except FetchError as exc:
        raise IngestError(str(exc)) from exc
    return inspect_bytes(fetched.content, declaration, policy) | {"fetched_url": fetched.final_url}


def _interval_check(
    text: str,
    schema: DetectedSchema,
    declaration: TemporalDeclaration,
    found: TimestampSurvey,
    policy: CleaningPolicy,
) -> dict[str, Any]:
    """The import's interval check, run without storing and without the confirmation."""

    try:
        checked = temporal_rules.validate(declaration, found)
        result = clean(text, schema, policy, checked)
        if result.fatal:
            return {"refused": result.fatal, "spacing": None, "ambiguous": []}
        assert checked.bar_interval is not None and found.kind is not None
        stamps: dict[str, list[float]] = {}
        for row in result.rows:
            stamps.setdefault(row.symbol, []).append(row.timestamp)
        report = temporal_rules.check_spacing(
            stamps, checked, found.kind, enforce_confirmation=False
        )
    except temporal_rules.TemporalError as exc:
        return {"refused": str(exc), "spacing": None, "ambiguous": []}
    return {
        "refused": None,
        "bar_interval": checked.bar_interval,
        "spacing": report.to_dict(),
        "ambiguous": list(report.ambiguous),
    }


def _readable(schema: DetectedSchema) -> bool:
    """Whether bars can be read at all; if not, cleaning fails the version and says why."""

    return not (
        schema.missing_required
        or schema.timestamp_ambiguous
        or schema.timestamp_format is TimestampFormat.UNKNOWN
    )


def _declared(
    payload: bytes, declaration: TemporalDeclaration | None
) -> tuple[TemporalDeclaration | None, TimestampSurvey]:
    """The declaration, checked against the file before anything is stored.

    A file whose bars cannot be read at all is let through without one: the
    import then produces a FAILED version naming the real problem, which is
    more useful than a demand to declare timestamps a file does not have.
    """

    text = _decode(payload)
    schema = detect_schema(text)
    if not _readable(schema):
        return declaration, TimestampSurvey()
    found = temporal_rules.survey(text, schema)
    try:
        return temporal_rules.validate(declaration, found), found
    except temporal_rules.TemporalError as exc:
        raise IngestError(str(exc)) from exc


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


def _check_spacing(
    result: CleaningResult, declaration: TemporalDeclaration | None, found: TimestampSurvey
) -> temporal_rules.SpacingReport | None:
    """Validate the declared bar interval against each instrument's bars, before any is moved.

    The observed spacing can refuse the declaration (a shorter gap, an irregular
    one, or an instrument whose own bars never show the interval and whose gaps
    are not confirmed as missing observations) but never replaces it. A refusal
    fails the version with the reason.
    """

    if declaration is None or result.fatal or not result.rows:
        return None
    assert declaration.bar_interval is not None  # ``validate`` fills it in
    assert found.kind is not None  # ``validate`` refuses a file without one kind
    stamps: dict[str, list[float]] = {}
    for row in result.rows:
        stamps.setdefault(row.symbol, []).append(row.timestamp)
    try:
        return temporal_rules.check_spacing(stamps, declaration, found.kind)
    except temporal_rules.TemporalError as exc:
        result.fatal = f"The declared bar interval does not fit the data: {exc}"
        result.rows = []
        return None


def _to_interval_end(result: CleaningResult, declaration: TemporalDeclaration | None) -> int:
    """Move interval-start bars to the end of their *declared* interval. Returns how many.

    The interval is the declared one, validated by :func:`_check_spacing` -- never
    the median or most common gap.
    """

    if (
        declaration is None
        or declaration.convention is not TimestampConvention.INTERVAL_START
        or result.fatal
        or not result.rows
    ):
        return 0
    assert declaration.bar_interval is not None
    moved = []
    for row in result.rows:
        try:
            end = temporal_rules.end_of_interval_for(row, declaration.bar_interval)
        except temporal_rules.TemporalError as exc:
            result.fatal = f"Start-stamped {declaration.bar_interval} bars cannot be moved: {exc}"
            result.rows = []
            return 0
        moved.append(replace(row, timestamp=end))
        result._transform(
            Transformation(
                row.source_line,
                TransformationKind.MOVED_TO_INTERVAL_END,
                "timestamp",
                repr(row.timestamp),
                repr(end),
            )
        )
    result.rows = moved
    return len(moved)


def _temporal_record(
    declaration: TemporalDeclaration | None,
    found: TimestampSurvey,
    moved: int,
    spacing: temporal_rules.SpacingReport | None,
) -> dict[str, Any]:
    """What the version's timestamps mean, recorded with it.

    Stored once, with the canonical file whose instants it produced: the raw
    bytes are kept by the source, so the reading can be reproduced and audited.
    """

    if declaration is None:
        return {}
    return {
        "record_version": temporal_rules.RECORD_VERSION,
        "convention": declaration.convention.value,
        "timezone": declaration.timezone,
        "bar_interval": declaration.bar_interval,
        "bar_interval_source": (
            "implied by session_date"
            if declaration.convention is TimestampConvention.SESSION_DATE
            else "declared"
        ),
        "timestamp_kind": found.kind.value if found.kind else None,
        "interpreted_by": f"alphalab {ALPHALAB_VERSION}",
        "rule": (
            "bare date = 23:59:59.999999 of that civil date in the declared zone "
            "(alphalab.data.parse_timestamp, DATE_ONLY, DateOnlyPolicy.END_OF_DAY); "
            "no exchange calendar or closing time is used"
            if found.kind is temporal_rules.TimestampKind.DATE
            else (
                "start-stamped bars moved to the end of their declared interval "
                "(alphalab.market.normalize_wire_bar, BarStamp.INTERVAL_START)"
                if declaration.convention is TimestampConvention.INTERVAL_START
                else "timestamps name the end of each bar's interval, as AlphaLab stamps it"
            )
        ),
        "missing_observations_confirmed_for": sorted(
            declaration.missing_observations_confirmed_for
        ),
        "spacing": spacing.to_dict() if spacing else None,
        "moved_to_interval_end": moved,
    }


def _canonicalize(
    session: DbSession,
    principal: Principal,
    *,
    dataset: Dataset,
    source: DataSource,
    payload: bytes,
    policy: CleaningPolicy,
    declaration: TemporalDeclaration | None,
    found: TimestampSurvey,
) -> IngestOutcome:
    """Detect, clean, report and store. The single canonicalization path."""

    text = _decode(payload)
    schema = detect_schema(text)
    result = clean(text, schema, policy, declaration)
    spacing = _check_spacing(result, declaration, found)
    moved = _to_interval_end(result, declaration)
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
        temporal_json=json.dumps(
            _temporal_record(declaration, found, moved, spacing), sort_keys=True
        ),
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
    temporal: TemporalDeclaration | None = None,
    preview_id: str | None = None,
) -> IngestOutcome:
    """Ingest bytes a user uploaded.

    ``temporal`` is what the file's timestamps mean. It is required for any file
    whose bars can be read, and checked against the file before anything is
    stored (:mod:`iluvtrade.data.temporal`).
    """

    settings = settings or get_settings()
    if not payload:
        raise IngestError("The uploaded file is empty.")
    if len(payload) > settings.max_upload_bytes:
        raise IngestError(
            f"The file is {len(payload)} bytes; the limit is {settings.max_upload_bytes}."
        )
    declaration, found = _declared(payload, temporal)
    _require_preview(payload, declaration, policy or CleaningPolicy(), preview_id)

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
        declaration=declaration,
        found=found,
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
    temporal: TemporalDeclaration | None = None,
    preview_id: str | None = None,
) -> IngestOutcome:
    """Fetch an approved URL and ingest what comes back, under ``temporal``.

    A missing-observation confirmation needs the ``preview_id`` of an
    :func:`inspect_url` check of the same bytes: it is recomputed from what this
    fetch returned, so a resource that changed since the preview is refused.
    """

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
    declaration, found = _declared(fetched.content, temporal)
    _require_preview(fetched.content, declaration, policy or CleaningPolicy(), preview_id)

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
        declaration=declaration,
        found=found,
    )


def declaration_of(version: DatasetVersion) -> TemporalDeclaration | None:
    """The timestamp convention ``version`` was imported under, or ``None``."""

    return temporal_rules.declaration_from_record(json.loads(version.temporal_json or "{}"))


def _require_runnable(version: DatasetVersion) -> None:
    """Refuse a version whose bars cannot be placed in time honestly.

    A version is runnable only with a recorded declaration that includes a
    declared bar interval validated per instrument. Three kinds have none:
    versions imported before declarations existed (dates read as UTC midnight,
    wall-clock times as UTC -- a bar's close could precede its knowability),
    versions whose bar interval was inferred from the median gap, and versions
    whose declared interval was validated across the whole file at once, where
    one instrument's bars could vouch for another's. Every path that runs a version
    -- approval, backtest submission, the backtest worker, session creation and
    the paper runner -- calls this.
    """

    if declaration_of(version) is None:
        raise IngestError(
            f"Dataset version {version.id} has no validated timestamp provenance: it was "
            "imported before timestamp conventions and bar intervals were declared and "
            "validated for each instrument, so its bars may be placed at the wrong instants. "
            "Re-import the file declaring what its "
            "timestamps mean and its bar interval; the original bytes are kept with the source."
        )


def require_runnable(version: DatasetVersion) -> None:
    """Public form of the runnability check, for workers that load a version directly."""

    _require_runnable(version)


def bar_interval_of(version: DatasetVersion) -> str:
    """The declared, validated bar interval a runnable version's bars cover."""

    declaration = declaration_of(version)
    if declaration is None or declaration.bar_interval is None:
        _require_runnable(version)
        raise AssertionError("unreachable")  # pragma: no cover
    return declaration.bar_interval


def interval_summary(version: DatasetVersion) -> dict[str, Any]:
    """The declared bar interval and how far the data supported it, for display.

    ``interval_status`` is ``validated`` when every instrument's own bars were
    consistent with the declared interval and showed it at least once (timestamps
    cannot prove a bar's length; this is consistency, not proof);
    ``missing_observations_confirmed`` when some instrument's bars could not show
    it and it was accepted only on the user's named declaration that its gaps are
    missing observations (those instruments are listed) -- user-declared, never
    verified; ``unvalidated`` for a version
    with no validated provenance, which nothing runs on; ``none`` for a version
    whose import failed. The observed spacing (``inferred_frequency``) is a
    separate, descriptive statistic and is never the interval.
    """

    record = json.loads(version.temporal_json or "{}")
    declaration = temporal_rules.declaration_from_record(record)
    if declaration is None or version.status is DatasetVersionStatus.FAILED:
        return {
            "bar_interval": None,
            "interval_status": (
                "none" if version.status is DatasetVersionStatus.FAILED else "unvalidated"
            ),
            "accepted_on_confirmation": [],
        }
    confirmed = list((record.get("spacing") or {}).get("accepted_on_confirmation") or [])
    return {
        "bar_interval": declaration.bar_interval,
        "interval_status": "missing_observations_confirmed" if confirmed else "validated",
        "accepted_on_confirmation": confirmed,
    }


def canonical_rows(version: DatasetVersion) -> list[dict[str, Any]]:
    """A version's canonical rows, verified against the hash recorded at import.

    Every path that computes on a version reads it through here. A file that
    is missing, truncated or altered since import raises :class:`IngestError`
    rather than feeding the engine different bars under the same version id.
    """

    if not version.canonical_path or not version.canonical_hash:
        raise IngestError(f"Dataset version {version.id} has no canonical data.")
    try:
        rows = storage.read_jsonl_verified(
            version.canonical_path, version.canonical_hash, expected_count=version.row_count
        )
    except storage.StorageError as exc:
        raise IngestError(
            f"Dataset version {version.id}'s canonical data cannot be used: {exc}"
        ) from exc
    if len(rows) != version.row_count:
        raise IngestError(
            f"Dataset version {version.id} records {version.row_count} rows but its canonical "
            f"data holds {len(rows)}."
        )
    return rows


#: What each temporal-evidence status means, stated with the evidence wherever
#: it is shown, so a result or listing cannot repeat the interval without it.
_EVIDENCE_MEANING = {
    "validated": (
        "The bar interval was declared by the importer. Every instrument's timestamps are "
        "consistent with it under the documented rule and sit one interval apart at least "
        "once. Timestamps cannot prove how long a bar is: this is consistency with the "
        "observations, not proof."
    ),
    "missing_observations_confirmed": (
        "The bar interval was declared by the importer. For the instruments listed, the "
        "timestamps never sit one interval apart and cannot show it; the importer asserted "
        "that their observations are missing at this interval rather than being longer bars. "
        "That assertion is the user's. Neither iluvtrade nor AlphaLab verified it, and it "
        "cannot be verified from timestamps."
    ),
    "unvalidated": (
        "The dataset version has no validated timestamp provenance (legacy or unresolved). "
        "Its bars may be placed at the wrong instants; nothing runs on it."
    ),
    "none": "The dataset version's import failed; it has no bars.",
    "not_recorded": (
        "Recorded before temporal evidence was kept. What this record's bar interval rested on "
        "was not recorded, and is not reconstructed from today's rules."
    ),
}

#: Bumped if the evidence object's fields or meanings change.
TEMPORAL_EVIDENCE_VERSION = 1


def temporal_evidence(version: DatasetVersion) -> dict[str, Any]:
    """What ``version``'s bar times rest on, as a self-describing snapshot.

    The authority is the version's own immutable record, never a client claim.
    Backtest runs and paper sessions store this when they execute or are created,
    and RedDesk listing evidence repeats the cited run's copy, so the limitation
    travels with every result built on the version.
    """

    record = json.loads(version.temporal_json or "{}")
    summary = interval_summary(version)
    status = str(summary["interval_status"])
    return {
        "evidence_version": TEMPORAL_EVIDENCE_VERSION,
        "status": status,
        "meaning": _EVIDENCE_MEANING[status],
        "dataset_version_id": version.id,
        "dataset_canonical_hash": version.canonical_hash,
        "provenance_record_version": record.get("record_version"),
        "convention": record.get("convention"),
        "timezone": record.get("timezone"),
        "bar_interval": summary["bar_interval"],
        "user_declared_instruments": list(summary["accepted_on_confirmation"]),
    }


def evidence_snapshot(stored: str | None) -> dict[str, Any]:
    """A stored snapshot, or the explicit "not recorded" evidence for a legacy row."""

    if stored:
        return dict(json.loads(stored))
    return {
        "evidence_version": TEMPORAL_EVIDENCE_VERSION,
        "status": "not_recorded",
        "meaning": _EVIDENCE_MEANING["not_recorded"],
    }


#: Bumped if what a preview identity covers changes.
PREVIEW_IDENTITY_VERSION = 1


def preview_identity(
    payload: bytes, declaration: TemporalDeclaration, policy: CleaningPolicy
) -> dict[str, str]:
    """Bind an interval preview to the exact bytes and every setting that shapes it.

    ``content_sha256`` is the SHA-256 of the raw bytes; ``preview_id`` is the
    SHA-256 of a canonical document holding that digest, the timestamp
    convention, zone and declared interval, and the effective cleaning policy.
    It is a binding, not a credential: it proves nothing about who may import,
    only that a decision was made looking at *these* bytes read *this* way.
    The missing-observation confirmation itself is deliberately outside it.
    """

    content = hashlib.sha256(payload).hexdigest()
    implied = "1d" if declaration.convention is TimestampConvention.SESSION_DATE else None
    document = {
        "preview_identity_version": PREVIEW_IDENTITY_VERSION,
        "content_sha256": content,
        "convention": declaration.convention.value,
        "timezone": declaration.timezone or None,
        "bar_interval": declaration.bar_interval or implied,
        "cleaning_policy": policy.to_dict(),
    }
    canonical_document = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return {
        "content_sha256": content,
        "preview_id": hashlib.sha256(canonical_document.encode("utf-8")).hexdigest(),
    }


def _require_preview(
    payload: bytes,
    declaration: TemporalDeclaration | None,
    policy: CleaningPolicy,
    preview_id: str | None,
) -> None:
    """Refuse a confirmation not bound to a preview of these exact bytes and settings.

    A missing-observation confirmation is a decision the user made looking at an
    interval preview. It must have been made about this file, read this way: a
    confirmation for an earlier file must not authorize a different file just
    because an instrument of the same name is ambiguous in both. Without a
    confirmation no preview is needed -- an ambiguous instrument is refused
    either way -- but a preview id that is supplied must still match.
    """

    confirming = declaration is not None and bool(declaration.missing_observations_confirmed_for)
    if not confirming and not preview_id:
        return
    if declaration is None:
        raise IngestError("A preview_id was given without a timestamp declaration.")
    if not preview_id:
        raise IngestError(
            "Missing observations were confirmed without a preview_id. Check the declared "
            "interval against this file (POST /datasets/inspect with the declaration) and "
            "send the preview_id it returns with the confirmation."
        )
    if preview_identity(payload, declaration, policy)["preview_id"] != preview_id:
        raise IngestError(
            "The preview_id does not match this file and these settings: the file's bytes, "
            "its timestamp declaration or its cleaning policy differ from what was previewed. "
            "Check the interval against the file being imported, and decide again."
        )


def approve(session: DbSession, principal: Principal, version_id: str) -> DatasetVersion:
    """Mark a pending version usable. The gate between cleaning and research."""

    version = require_owned(session, DatasetVersion, version_id, principal.organization_id)
    if version.status is DatasetVersionStatus.APPROVED:
        return version
    if version.status is not DatasetVersionStatus.PENDING_APPROVAL:
        raise IngestError(f"A {version.status.value} dataset version cannot be approved.")
    _require_runnable(version)
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
    _require_runnable(version)
    return version
