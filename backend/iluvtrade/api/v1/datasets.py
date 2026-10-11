"""The data workspace API: inspect, upload, fetch, review, approve."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from iluvtrade.api.deps import current_principal, db_session, rate_limit, require_trader
from iluvtrade.api.v1.schemas import (
    CleaningPolicyModel,
    DatasetResponse,
    DatasetVersionDetail,
    DatasetVersionResponse,
    FetchDatasetRequest,
    InspectUrlRequest,
    RejectRequest,
)
from iluvtrade.common import storage
from iluvtrade.config import get_settings
from iluvtrade.data import canonical, ingest
from iluvtrade.data.cleaning import CleaningPolicy
from iluvtrade.data.temporal import TemporalDeclaration, TimestampConvention
from iluvtrade.db.models.data import Dataset, DatasetVersion, DataSource
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped

#: Starlette renamed this constant; read whichever this version defines.
_UNPROCESSABLE: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

router = APIRouter(prefix="/datasets", tags=["datasets"])


def _declaration(
    convention: str | None,
    timezone: str | None,
    bar_interval: str | None = None,
    missing_observations_confirmed_for: list[str] | None = None,
) -> TemporalDeclaration | None:
    """The user's timestamp declaration, or ``None`` -- which the import refuses."""

    if not convention:
        return None
    try:
        parsed = TimestampConvention(convention)
    except ValueError:
        choices = ", ".join(c.value for c in TimestampConvention)
        raise ingest.IngestError(
            f"{convention!r} is not a timestamp convention; choose one of: {choices}."
        ) from None
    named = frozenset(s.strip() for s in missing_observations_confirmed_for or () if s.strip())
    return TemporalDeclaration(parsed, timezone or None, bar_interval or None, named)


def _policy(model: CleaningPolicyModel | None) -> CleaningPolicy:
    return CleaningPolicy.from_dict(model.model_dump()) if model else CleaningPolicy()


def _version_response(session: DbSession, version: DatasetVersion) -> DatasetVersionResponse:
    dataset = session.get(Dataset, version.dataset_id)
    return DatasetVersionResponse(
        id=version.id,
        dataset_id=version.dataset_id,
        dataset_name=dataset.name if dataset else "",
        version=version.version,
        status=version.status.value,
        row_count=version.row_count,
        rejected_row_count=version.rejected_row_count,
        symbol_count=version.symbol_count,
        start_timestamp=version.start_timestamp,
        end_timestamp=version.end_timestamp,
        inferred_frequency=version.inferred_frequency,
        **ingest.interval_summary(version),
        quality_score=version.quality_score,
        canonical_hash=version.canonical_hash,
        failure_reason=version.failure_reason,
        created_at=version.created_at,
        approved_at=version.approved_at,
    )


async def _read_upload(upload: UploadFile) -> bytes:
    """Read an upload, refusing anything over the limit as it streams.

    Trusting ``Content-Length`` would let a lying client push an unbounded body
    into memory, so the ceiling is enforced against bytes actually read.
    """

    settings = get_settings()
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"The file exceeds the {settings.max_upload_bytes} byte limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/inspect", dependencies=[Depends(rate_limit("ingest"))])
async def inspect(
    file: UploadFile = File(...),
    timestamp_convention: str | None = Form(default=None),
    timezone: str | None = Form(default=None),
    bar_interval: str | None = Form(default=None),
    policy_json: str | None = Form(default=None),
    _principal: Principal = Depends(current_principal),
) -> dict:
    """Detect a CSV's schema without storing anything.

    Powers the upload preview: the user sees what was detected *before* deciding
    to import. Given a timestamp declaration, it also runs the import's interval
    check (``interval_check``) and names the instruments whose own bars cannot
    show the declared interval, so a missing-observation confirmation can name
    exactly those. ``interval_check.preview_id`` binds that check to these bytes,
    the declaration and ``policy_json`` (the same cleaning policy the upload
    will send); an upload confirming missing observations must send it back.
    """

    _refuse_nul(
        ("timestamp_convention", timestamp_convention),
        ("timezone", timezone),
        ("bar_interval", bar_interval),
        ("policy_json", policy_json),
    )
    declaration = _declaration(timestamp_convention, timezone, bar_interval)
    policy = _policy(_policy_model(policy_json))
    # Detection parses the whole file; on the event loop it would stall every
    # other request on this worker for as long as that takes.
    payload = await _read_upload(file)
    return await run_in_threadpool(ingest.inspect_bytes, payload, declaration, policy)


@router.post("/inspect-url", dependencies=[Depends(rate_limit("fetch"))])
def inspect_url(
    payload: InspectUrlRequest,
    _principal: Principal = Depends(require_trader),
) -> dict:
    """Fetch an allowlisted URL and inspect it, storing nothing.

    The fetch path's preview: same allowlist and address checks as
    ``/datasets/fetch``. Its ``interval_check.preview_id`` binds the check to the
    bytes fetched now; ``/datasets/fetch`` re-fetches and refuses a confirmation
    whose preview no longer matches what the resource serves.
    """

    return ingest.inspect_url(
        payload.url,
        _declaration(payload.timestamp_convention, payload.timezone, payload.bar_interval),
        _policy(payload.policy),
    )


def _policy_model(policy_json: str | None) -> CleaningPolicyModel | None:
    """Parse a multipart ``policy_json`` field, or answer 422."""

    try:
        return CleaningPolicyModel.model_validate(json.loads(policy_json)) if policy_json else None
    except ValueError as exc:  # malformed JSON, or a field the policy does not have
        # Answered 500 until the hardening pass: the form field is parsed here,
        # not by FastAPI, so a bad one never became a validation error.
        raise HTTPException(
            status_code=_UNPROCESSABLE, detail=f"policy_json is not a valid cleaning policy: {exc}"
        ) from None


def _refuse_nul(*fields: tuple[str, str | None]) -> None:
    """Multipart fields are not request models, so ``schemas.Strict``'s NUL check
    does not reach them; PostgreSQL text cannot hold one."""

    for label, value in fields:
        if value and "\x00" in value:
            raise HTTPException(
                status_code=_UNPROCESSABLE, detail=f"{label} may not contain a NUL character."
            )


@router.post(
    "/upload",
    response_model=DatasetVersionDetail,
    status_code=201,
    dependencies=[Depends(rate_limit("ingest"))],
)
async def upload(
    file: UploadFile = File(...),
    dataset_name: str | None = Form(default=None),
    description: str = Form(default=""),
    policy_json: str | None = Form(default=None),
    timestamp_convention: str | None = Form(default=None),
    timezone: str | None = Form(default=None),
    bar_interval: str | None = Form(default=None),
    missing_observations_confirmed_for: list[str] = Form(default=[]),
    preview_id: str | None = Form(default=None),
    missing_observations_confirmed: str | None = Form(default=None),
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> DatasetVersionDetail:
    """Import a CSV. Produces a version awaiting approval.

    ``timestamp_convention`` (and ``timezone`` where the values need one) state
    what the file's timestamps mean; ``POST /datasets/inspect`` reports which
    are required. The import refuses without them, before storing anything.

    The route is ``async`` only to read the upload; everything that touches the
    database runs in the thread pool. Run on the event loop, a blocking
    statement stalls every other request on the worker — and when it is waiting
    for a lock held by a request whose commit needs that same event loop, it
    waits forever. On PostgreSQL two concurrent uploads in one workspace did
    exactly that (``tests/postgres``); SQLite hid it behind ``busy_timeout``.
    """

    _refuse_nul(
        ("filename", file.filename),
        ("dataset_name", dataset_name),
        ("description", description),
        ("policy_json", policy_json),
        ("timestamp_convention", timestamp_convention),
        ("timezone", timezone),
        ("bar_interval", bar_interval),
        *(
            ("missing_observations_confirmed_for", name)
            for name in missing_observations_confirmed_for
        ),
    )
    if len(missing_observations_confirmed_for) > 10_000 or any(
        len(name) > 60 for name in missing_observations_confirmed_for
    ):
        raise HTTPException(
            status_code=_UNPROCESSABLE,
            detail=(
                "missing_observations_confirmed_for names at most 10000 instruments "
                "of 60 characters."
            ),
        )
    if missing_observations_confirmed is not None:
        # The retired dataset-wide flag. Named and refused rather than ignored,
        # so no client believes it still waives anything.
        raise HTTPException(
            status_code=_UNPROCESSABLE,
            detail=(
                "missing_observations_confirmed is no longer accepted. Name each instrument in "
                "missing_observations_confirmed_for and send the preview_id of an interval "
                "check of this file."
            ),
        )
    if preview_id is not None and len(preview_id) > 64:
        raise HTTPException(status_code=_UNPROCESSABLE, detail="preview_id is too long.")
    policy_model = _policy_model(policy_json)
    payload = await _read_upload(file)

    def _ingest() -> DatasetVersionDetail:
        outcome = ingest.ingest_upload(
            session,
            principal,
            filename=file.filename or "upload.csv",
            payload=payload,
            dataset_name=dataset_name,
            description=description,
            policy=_policy(policy_model),
            temporal=_declaration(
                timestamp_convention, timezone, bar_interval, missing_observations_confirmed_for
            ),
            preview_id=preview_id,
        )
        return _detail(session, outcome.version)

    return await run_in_threadpool(_ingest)


@router.post(
    "/fetch",
    response_model=DatasetVersionDetail,
    status_code=201,
    dependencies=[Depends(rate_limit("fetch"))],
)
def fetch(
    payload: FetchDatasetRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> DatasetVersionDetail:
    """Import a CSV from an allowlisted URL, through the same pipeline."""

    outcome = ingest.ingest_url(
        session,
        principal,
        url=payload.url,
        dataset_name=payload.dataset_name,
        description=payload.description,
        policy=_policy(payload.policy),
        temporal=_declaration(
            payload.timestamp_convention,
            payload.timezone,
            payload.bar_interval,
            payload.missing_observations_confirmed_for,
        ),
        preview_id=payload.preview_id,
    )
    return _detail(session, outcome.version)


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> list[DatasetResponse]:
    datasets = session.execute(
        scoped(Dataset, principal.organization_id).order_by(Dataset.created_at.desc())
    ).scalars()
    return [
        DatasetResponse(
            id=dataset.id,
            slug=dataset.slug,
            name=dataset.name,
            description=dataset.description,
            created_at=dataset.created_at,
            versions=[_version_response(session, v) for v in dataset.versions],
        )
        for dataset in datasets
    ]


@router.get("/versions/{version_id}", response_model=DatasetVersionDetail)
def get_version(
    version_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> DatasetVersionDetail:
    version = require_owned(session, DatasetVersion, version_id, principal.organization_id)
    return _detail(session, version)


def _detail(session: DbSession, version: DatasetVersion) -> DatasetVersionDetail:
    source = session.get(DataSource, version.source_id)
    rejected: dict = {}
    key = canonical.rejected_key(version.organization_id, version.id)
    if storage.exists(key):
        rejected = storage.read_json(key)
    base = _version_response(session, version)
    return DatasetVersionDetail(
        **base.model_dump(),
        schema_detection=json.loads(version.schema_json or "{}"),
        quality=json.loads(version.quality_json or "{}"),
        transformations=json.loads(version.transformations_json or "{}"),
        cleaning_policy=json.loads(version.cleaning_policy_json or "{}"),
        temporal=json.loads(version.temporal_json or "{}"),
        source=(
            {
                "kind": source.kind.value,
                "origin": source.origin,
                "requested_uri": source.requested_uri,
                "content_hash": source.content_hash,
                "size_bytes": source.size_bytes,
                "content_type": source.content_type,
                "retrieved_at": source.retrieved_at.isoformat(),
            }
            if source
            else {}
        ),
        rejected_rows=rejected,
    )


@router.post("/versions/{version_id}/approve", response_model=DatasetVersionResponse)
def approve(
    version_id: str,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> DatasetVersionResponse:
    """Approve a version. Nothing may use a dataset until this happens."""

    return _version_response(session, ingest.approve(session, principal, version_id))


@router.post("/versions/{version_id}/reject", response_model=DatasetVersionResponse)
def reject(
    version_id: str,
    payload: RejectRequest,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> DatasetVersionResponse:
    return _version_response(session, ingest.reject(session, principal, version_id, payload.reason))


@router.get("/versions/{version_id}/preview")
def preview(
    version_id: str,
    limit: int = 50,
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The first rows of the canonical data, exactly as stored."""

    version = require_owned(session, DatasetVersion, version_id, principal.organization_id)
    if not version.canonical_path:
        return {"rows": [], "row_count": 0}
    rows = []
    for index, row in enumerate(storage.read_jsonl(version.canonical_path)):
        if index >= min(limit, 500):
            break
        rows.append(row)
    return {"rows": rows, "row_count": version.row_count}
