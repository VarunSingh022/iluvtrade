"""The data workspace API: inspect, upload, fetch, review, approve."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session as DbSession

from iluvtrade.api.deps import current_principal, db_session, require_trader
from iluvtrade.api.v1.schemas import (
    CleaningPolicyModel,
    DatasetResponse,
    DatasetVersionDetail,
    DatasetVersionResponse,
    FetchDatasetRequest,
    RejectRequest,
)
from iluvtrade.common import storage
from iluvtrade.config import get_settings
from iluvtrade.data import canonical, ingest
from iluvtrade.data.cleaning import CleaningPolicy
from iluvtrade.db.models.data import Dataset, DatasetVersion, DataSource
from iluvtrade.platform.accounts import Principal
from iluvtrade.platform.tenancy import require_owned, scoped

router = APIRouter(prefix="/datasets", tags=["datasets"])


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


@router.post("/inspect")
async def inspect(
    file: UploadFile = File(...),
    _principal: Principal = Depends(current_principal),
) -> dict:
    """Detect a CSV's schema without storing anything.

    Powers the upload preview: the user sees what was detected *before* deciding
    to import.
    """

    return ingest.inspect_bytes(await _read_upload(file))


@router.post("/upload", response_model=DatasetVersionDetail, status_code=201)
async def upload(
    file: UploadFile = File(...),
    dataset_name: str | None = Form(default=None),
    description: str = Form(default=""),
    policy_json: str | None = Form(default=None),
    session: DbSession = Depends(db_session),
    principal: Principal = Depends(require_trader),
) -> DatasetVersionDetail:
    """Import a CSV. Produces a version awaiting approval."""

    policy_model = (
        CleaningPolicyModel.model_validate(json.loads(policy_json)) if policy_json else None
    )
    outcome = ingest.ingest_upload(
        session,
        principal,
        filename=file.filename or "upload.csv",
        payload=await _read_upload(file),
        dataset_name=dataset_name,
        description=description,
        policy=_policy(policy_model),
    )
    return _detail(session, outcome.version)


@router.post("/fetch", response_model=DatasetVersionDetail, status_code=201)
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
