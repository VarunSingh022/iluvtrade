"""Typed request and response models.

Every route declares one. Two reasons beyond documentation: a response model is
an **allowlist**, so a field added to an ORM object cannot leak into an API
response by accident (which is how a credential column becomes a disclosure);
and a request model rejects unknown fields, so a typo'd parameter is an error
rather than a silently ignored setting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Strict(BaseModel):
    """Refuses unknown fields. Every request model inherits this."""

    model_config = ConfigDict(extra="forbid")


# --- auth ------------------------------------------------------------------


class RegisterRequest(Strict):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=200)
    organization_name: str | None = Field(default=None, max_length=200)


class LoginRequest(Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    organization_id: str | None = None
    #: A TOTP code or a recovery code, when the account has MFA enabled.
    mfa_code: str | None = Field(default=None, max_length=32)


class MfaCodeRequest(Strict):
    code: str = Field(min_length=6, max_length=32)


class MfaEnrolmentResponse(BaseModel):
    """Returned **once**. The secret and codes are never retrievable again."""

    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


class MfaStatusResponse(BaseModel):
    enabled: bool
    enrolment_pending: bool
    recovery_codes_remaining: int


class SessionResponse(BaseModel):
    #: Returned so a non-browser client can use bearer auth. The browser app
    #: uses the HttpOnly cookie set alongside and ignores this.
    token: str
    expires_at: datetime
    user: UserResponse


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    organization_id: str
    organization_name: str
    role: str
    live_trading_enabled: bool
    mfa_enabled: bool


# --- datasets --------------------------------------------------------------


class CleaningPolicyModel(Strict):
    duplicate_policy: Literal["keep_first", "keep_last", "reject_dataset"] = "keep_first"
    drop_out_of_order: bool = True
    fill_ohlc_from_close: bool = True
    default_volume_to_zero: bool = True
    derive_vwap: bool = True
    enforce_ohlc_consistency: bool = True
    require_positive_prices: bool = True
    default_symbol: str = Field(default="INSTRUMENT", max_length=60)
    timeframe: str = "1d"


class FetchDatasetRequest(Strict):
    url: str = Field(max_length=2000)
    dataset_name: str | None = Field(default=None, max_length=200)
    description: str = ""
    policy: CleaningPolicyModel | None = None


class DatasetVersionResponse(BaseModel):
    id: str
    dataset_id: str
    dataset_name: str
    version: int
    status: str
    row_count: int
    rejected_row_count: int
    symbol_count: int
    start_timestamp: float | None
    end_timestamp: float | None
    inferred_frequency: str | None
    quality_score: float | None
    canonical_hash: str | None
    failure_reason: str | None
    created_at: datetime
    approved_at: datetime | None


class DatasetVersionDetail(DatasetVersionResponse):
    schema_detection: dict[str, Any]
    quality: dict[str, Any]
    transformations: dict[str, Any]
    cleaning_policy: dict[str, Any]
    source: dict[str, Any]
    rejected_rows: dict[str, Any]


class DatasetResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    created_at: datetime
    versions: list[DatasetVersionResponse]


class RejectRequest(Strict):
    reason: str = Field(default="", max_length=1000)


# --- strategies ------------------------------------------------------------


class CreateStrategyRequest(Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    visibility: Literal["private", "organization", "marketplace"] = "private"


class CreateVersionRequest(Strict):
    implementation_key: str = Field(max_length=120)
    parameters: dict[str, Any] = Field(default_factory=dict)
    changelog: str = ""


class UpdateVersionRequest(Strict):
    implementation_key: str | None = None
    parameters: dict[str, Any] | None = None
    changelog: str | None = None


class StrategyVersionResponse(BaseModel):
    id: str
    strategy_id: str
    version: int
    status: str
    implementation_key: str
    default_parameters: dict[str, Any]
    parameters_schema: dict[str, Any]
    changelog: str
    content_hash: str | None
    frozen: bool
    certification_status: str
    published_at: datetime | None
    integrity_ok: bool


class StrategyResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    visibility: str
    owner_user_id: str
    created_at: datetime
    versions: list[StrategyVersionResponse]


# --- backtests -------------------------------------------------------------


class SubmitBacktestRequest(Strict):
    dataset_version_id: str
    strategy_version_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    starting_cash: str = "1000000.00"
    currency: str = Field(default="INR", max_length=8)
    exchange: str = Field(default="XNSE", max_length=16)
    risk_profile: Literal["research", "conservative"] = "research"
    commission_kind: Literal["percentage", "per_share"] = "percentage"
    commission_rate: str = "0.0003"
    risk_free_rate: float = 0.0
    seed: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class BacktestJobResponse(BaseModel):
    id: str
    status: str
    dataset_version_id: str
    strategy_version_id: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    attempts: int
    progress: float
    error_code: str | None
    error_message: str | None
    #: True when this submission returned an existing job rather than queueing a
    #: new one. The client uses it to avoid saying "queued" twice.
    deduplicated: bool = False


class BacktestResultResponse(BaseModel):
    run_id: str
    reproducibility: dict[str, Any]
    metrics: dict[str, Any]
    result: dict[str, Any]


# --- reddesk ---------------------------------------------------------------


class CreateListingRequest(Strict):
    strategy_id: str
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=400)
    description: str = ""
    methodology: str = ""
    risk_disclosure: str = ""
    price_amount: str = "0"
    price_currency: str = Field(default="INR", max_length=8)
    billing_cadence: Literal["one_time", "monthly", "annual"] = "one_time"
    version_access_policy: Literal["pinned", "rolling"] = "pinned"
    licence_terms: str = ""
    supported_brokers: list[str] = Field(default_factory=list)
    supported_instruments: list[str] = Field(default_factory=list)
    supported_data: list[str] = Field(default_factory=list)


class OfferVersionRequest(Strict):
    strategy_version_id: str
    evidence_backtest_run_id: str | None = None
    release_notes: str = ""
    make_current: bool = True


class ReviewListingRequest(Strict):
    approve: bool
    notes: str = ""


class PurchaseRequest(Strict):
    listing_id: str
    idempotency_key: str | None = Field(default=None, max_length=80)


class RateListingRequest(Strict):
    rating: int = Field(ge=1, le=5)
    body: str = Field(default="", max_length=4000)


class EntitlementResponse(BaseModel):
    id: str
    listing_id: str
    strategy_id: str
    granted_strategy_version_id: str
    version_access_policy: str
    status: str
    valid_from: datetime
    valid_until: datetime | None


# --- brokers ---------------------------------------------------------------


class CreateBrokerAccountRequest(Strict):
    broker: Literal["paper", "zerodha"]
    label: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="INR", max_length=8)


class AuthorizeZerodhaRequest(Strict):
    request_token: str = Field(min_length=1, max_length=200)


# --- trading ---------------------------------------------------------------


class CreateSessionRequest(Strict):
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["paper", "live"]
    strategy_version_id: str
    dataset_version_id: str | None = None
    broker_account_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    starting_cash: str = "1000000.00"
    base_currency: str = Field(default="INR", max_length=8)
    risk_profile: Literal["research", "conservative"] = "conservative"
    seed: int | None = None
    live_confirmed: bool = False


class KillSwitchRequest(Strict):
    reason: str = Field(min_length=1, max_length=400)


class TradingSessionResponse(BaseModel):
    id: str
    name: str
    mode: str
    status: str
    strategy_version_id: str
    dataset_version_id: str | None
    broker_account_id: str | None
    entitlement_id: str | None
    starting_cash: str
    base_currency: str
    seed: int
    records_processed: int
    cash: str | None
    equity: str | None
    realized_pnl: str | None
    unrealized_pnl: str | None
    commission_paid: str | None
    kill_switch_engaged: bool
    kill_switch_reason: str | None
    started_at: datetime | None
    stopped_at: datetime | None
    last_advanced_at: datetime | None
    failure_reason: str | None
    risk_config: dict[str, Any]
    #: True when nothing has advanced this session recently. The UI must not
    #: paint a stale figure as a live one (PHASE 14).
    is_live_state: bool


class SessionOrderResponse(BaseModel):
    sequence: int
    engine_order_id: str
    instrument: str
    side: str
    quantity: str
    filled_quantity: str
    average_fill_price: str | None
    status: str
    order_type: str
    submitted_timestamp: float | None


class SessionFillResponse(BaseModel):
    sequence: int
    engine_fill_id: str
    engine_order_id: str
    instrument: str
    side: str
    quantity: str
    price: str
    commission: str
    fill_timestamp: float | None


class SessionPositionResponse(BaseModel):
    instrument: str
    quantity: str
    average_price: str
    market_price: str | None
    unrealized_pnl: str | None
    realized_pnl: str | None


class SessionEventResponse(BaseModel):
    sequence: int
    created_at: datetime
    severity: str
    kind: str
    message: str


# --- platform --------------------------------------------------------------


class NotificationResponse(BaseModel):
    id: str
    severity: str
    kind: str
    title: str
    body: str
    resource_type: str | None
    resource_id: str | None
    created_at: datetime
    read_at: datetime | None


class AuditEventResponse(BaseModel):
    id: str
    created_at: datetime
    actor_user_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    payload: dict[str, Any]
    #: Chain position and links, exposed so a reader can verify independently
    #: rather than having to trust the server's own verification endpoint.
    sequence: int
    previous_hash: str
    event_hash: str


class UpdateSettingsRequest(Strict):
    display_name: str | None = Field(default=None, max_length=200)
    live_trading_enabled: bool | None = None


SessionResponse.model_rebuild()
