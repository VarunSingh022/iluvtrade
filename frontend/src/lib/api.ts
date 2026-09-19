/**
 * The typed API client.
 *
 * Two rules it enforces so no call site has to remember them:
 *
 * - `credentials: "include"` on every request, because the session is an
 *   HttpOnly cookie the browser must send.
 * - `X-Requested-With` on every state-changing request. The backend refuses a
 *   cookie-authenticated POST without it, which is what stops a cross-origin
 *   form post from acting as the signed-in user.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields?: { field: string; message: string }[];

  constructor(status: number, code: string, message: string, fields?: { field: string; message: string }[]) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }

  /** Whether the caller should send the user to the login screen. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function request<T>(method: string, path: string, body?: unknown, isForm = false): Promise<T> {
  const headers: Record<string, string> = {};
  if (MUTATING.has(method)) headers["X-Requested-With"] = "XMLHttpRequest";
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";

  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "include",
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? (JSON.parse(text) as unknown) : null;

  if (!response.ok) {
    const envelope = (payload as { error?: { code?: string; message?: string; fields?: { field: string; message: string }[] } } | null)?.error;
    const error = new ApiError(
      response.status,
      envelope?.code ?? "HTTPError",
      envelope?.message ?? `Request failed with ${response.status}.`,
      envelope?.fields,
    );
    // A session that expired mid-visit would otherwise surface as a 401 on
    // whichever screen happened to be open, with the shell still rendered
    // around it and nothing returning the user to sign in. Announced once,
    // here, so every screen inherits the behaviour.
    //
    // `MfaRequired` is excluded: it is also a 401, but the caller is *not*
    // signed in yet and is mid-login. Clearing the user there would wipe the
    // login form the moment the challenge appeared.
    if (response.status === 401 && error.code !== "MfaRequired" && !path.startsWith("/auth/login")) {
      window.dispatchEvent(new CustomEvent("iluvtrade:session-expired"));
    }
    throw error;
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  postForm: <T>(path: string, form: FormData) => request<T>("POST", path, form, true),
};

/** Health is outside /api/v1, so it has its own call. */
export async function health(): Promise<HealthResponse> {
  const response = await fetch("/api/health", { credentials: "include" });
  return (await response.json()) as HealthResponse;
}

// --- types mirroring the backend's response models -------------------------

export interface HealthResponse {
  status: string;
  version: string;
  environment: string;
  engine: { name: string; version: string };
  live_trading_enabled: boolean;
  /**
   * What this deployment actually enforces, as opposed to what it configured.
   * Declared here because Settings renders it: an undeclared field that a
   * screen reads is a contract nobody is checking.
   */
  rate_limiting: {
    enabled: boolean;
    shared_across_instances: boolean;
    backend: string;
    caveat: string;
  };
  notification_channels: string[];
  payment_provider: string;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  organization_id: string;
  organization_name: string;
  role: string;
  live_trading_enabled: boolean;
  mfa_enabled: boolean;
}

export interface SessionResponse {
  token: string;
  expires_at: string;
  user: User;
}

// --- two-factor authentication ---------------------------------------------

export interface MfaStatus {
  enabled: boolean;
  enrolment_pending: boolean;
  recovery_codes_remaining: number;
}

/**
 * Returned exactly once, by enrolment.
 *
 * Nothing in the app may persist any of this: the secret goes to the
 * authenticator app by being scanned or typed, the recovery codes go to the
 * user by being written down. Both live in React state for the length of the
 * enrolment and are gone on navigation — never localStorage, never a URL,
 * never a log.
 */
export interface MfaEnrolment {
  secret: string;
  provisioning_uri: string;
  recovery_codes: string[];
}

// --- organizations ----------------------------------------------------------

export interface MembershipSummary {
  organization_id: string;
  organization_name: string;
  role: string;
  is_current: boolean;
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  joined_at: string;
}

export interface Invitation {
  id: string;
  email: string;
  role: string;
  status: string;
  invited_by_user_id: string;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface InvitationCreated {
  invitation: Invitation;
  /** Shown once to the inviter. Never stored and never served again. */
  token: string;
  share_instructions: string;
}

export interface ChainBreak {
  sequence: number;
  event_id: string;
  reason: string;
}

export interface AuditVerification {
  organization_id: string;
  events_checked: number;
  intact: boolean;
  head_hash: string;
  breaks: ChainBreak[];
}

// --- password reset ----------------------------------------------------------

export interface PasswordResetAvailability {
  available: boolean;
  channel: string;
  notice: string;
}

export interface Finding {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
  count: number;
}

export interface Quality {
  total_input_rows: number;
  accepted_rows: number;
  rejected_rows: number;
  symbol_count: number;
  symbols: string[];
  start_timestamp: number | null;
  end_timestamp: number | null;
  inferred_frequency: string | null;
  median_gap_seconds: number | null;
  duplicate_count: number;
  missing_value_count: number;
  malformed_row_count: number;
  invalid_timestamp_count: number;
  out_of_order_count: number;
  invalid_price_count: number;
  invalid_volume_count: number;
  gap_count: number;
  rejection_breakdown: Record<string, number>;
  transformation_breakdown: Record<string, number>;
  findings: Finding[];
  score: number;
}

export interface FieldMatch {
  role: string;
  column: string | null;
  column_index: number | null;
  confidence: number;
  method: string;
  note: string;
}

export interface DetectedSchema {
  columns: string[];
  delimiter: string;
  has_header: boolean;
  fields: Record<string, FieldMatch>;
  timestamp_format: string;
  timestamp_ambiguous: boolean;
  missing_required: string[];
  ohlc_complete: boolean;
  sample_rows: string[][];
  warnings: string[];
}

export interface DatasetVersion {
  id: string;
  dataset_id: string;
  dataset_name: string;
  version: number;
  status: string;
  row_count: number;
  rejected_row_count: number;
  symbol_count: number;
  start_timestamp: number | null;
  end_timestamp: number | null;
  inferred_frequency: string | null;
  quality_score: number | null;
  canonical_hash: string | null;
  failure_reason: string | null;
  created_at: string;
  approved_at: string | null;
}

export interface RejectedRow {
  line_number: number;
  reason: string;
  detail: string;
  raw: string;
}

export interface Transformation {
  line_number: number;
  kind: string;
  column: string;
  before: string;
  after: string;
}

export interface DatasetVersionDetail extends DatasetVersion {
  schema_detection: DetectedSchema;
  quality: Quality;
  transformations: { logged: Transformation[]; counts: Record<string, number>; logged_is_truncated: boolean };
  cleaning_policy: Record<string, unknown>;
  source: {
    kind?: string;
    origin?: string;
    requested_uri?: string | null;
    content_hash?: string;
    size_bytes?: number;
    content_type?: string | null;
    retrieved_at?: string;
  };
  rejected_rows: { logged: RejectedRow[]; counts: Record<string, number>; logged_is_truncated: boolean };
}

export interface Dataset {
  id: string;
  slug: string;
  name: string;
  description: string;
  created_at: string;
  versions: DatasetVersion[];
}

export interface ParameterSpec {
  name: string;
  kind: string;
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  description: string;
}

export interface Implementation {
  key: string;
  name: string;
  description: string;
  parameters: ParameterSpec[];
}

export interface StrategyVersion {
  id: string;
  strategy_id: string;
  version: number;
  status: string;
  implementation_key: string;
  default_parameters: Record<string, unknown>;
  parameters_schema: { implementation?: string; parameters?: ParameterSpec[] };
  changelog: string;
  content_hash: string | null;
  frozen: boolean;
  certification_status: string;
  published_at: string | null;
  integrity_ok: boolean;
}

export interface Strategy {
  id: string;
  slug: string;
  name: string;
  description: string;
  visibility: string;
  owner_user_id: string;
  created_at: string;
  versions: StrategyVersion[];
}

export interface BacktestJob {
  id: string;
  status: string;
  dataset_version_id: string;
  strategy_version_id: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  attempts: number;
  progress: number;
  error_code: string | null;
  error_message: string | null;
  deduplicated: boolean;
}

export interface BacktestResult {
  run_id: string;
  reproducibility: {
    dataset_version_id: string;
    dataset_canonical_hash: string | null;
    strategy_version_id: string;
    strategy_content_hash: string | null;
    seed: number;
    engine: string;
    parameters: Record<string, unknown>;
    universe: string[];
    configuration: Record<string, unknown>;
  };
  metrics: Record<string, number | string | null>;
  result: {
    valuation: Record<string, string | number | null>;
    positions: Array<Record<string, string | null>>;
    orders: Array<Record<string, string | number | null>>;
    fills: Array<Record<string, string | number | null>>;
    equity_curve: Array<{ timestamp: number; equity: string | null; cash: string | null }>;
    report: Record<string, never> | null | Record<string, Record<string, number | null> | number | string | null>;
    risk_refusals?: { count: number; logged: Array<Record<string, unknown>> };
    skipped_records?: Array<Record<string, unknown>>;
    unpriced_assets?: Array<Record<string, unknown>>;
  };
}

export interface Listing {
  id: string;
  slug: string;
  title: string;
  summary: string;
  description: string;
  methodology: string;
  risk_disclosure: string;
  status: string;
  creator_organization_id: string;
  strategy_id: string;
  price: { amount: string; currency: string; cadence: string };
  version_access_policy: string;
  licence_terms: string;
  supported_brokers: string[];
  supported_instruments: string[];
  supported_data: string[];
  current_strategy_version_id: string | null;
  version_history: Array<{ strategy_version_id: string; release_notes: string; is_current: boolean; has_evidence: boolean }>;
  evidence: {
    backtest_run_id: string;
    records_processed: number;
    order_count: number;
    starting_cash: string;
    ending_equity: string;
    total_return: number | null;
    cagr: number | null;
    sharpe_ratio: number | null;
    max_drawdown: number | null;
    volatility: number | null;
    engine: string;
    seed: number;
    disclaimer: string;
  } | null;
  rating: { average: number | null; count: number };
  published_at: string | null;
}

export interface BillingStatus {
  provider: string;
  moves_money: boolean;
  state: "REAL_PAYMENT_PROCESSING_ENABLED" | "READY_FOR_PROVIDER_INTEGRATION";
  notice: string;
  payouts_settleable: boolean;
}

export interface Entitlement {
  id: string;
  listing_id: string;
  strategy_id: string;
  granted_strategy_version_id: string;
  version_access_policy: string;
  status: string;
  valid_from: string;
  valid_until: string | null;
}

export interface BrokerConnection {
  account_id: string;
  broker: string;
  label: string;
  state: string;
  venue_account_id: string | null;
  venue_user_name: string | null;
  base_currency: string;
  credential_hint: string | null;
  token_expires_at: string | null;
  connected_at: string | null;
  last_heartbeat_at: string | null;
  last_error: string | null;
  verified_against_live_venue: boolean;
}

export interface SupportedBroker {
  broker: string;
  name: string;
  verified_against_live_venue: boolean;
  notes: string;
}

export interface TradingSession {
  id: string;
  name: string;
  mode: "paper" | "live";
  status: string;
  strategy_version_id: string;
  dataset_version_id: string | null;
  broker_account_id: string | null;
  entitlement_id: string | null;
  starting_cash: string;
  base_currency: string;
  seed: number;
  records_processed: number;
  cash: string | null;
  equity: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  commission_paid: string | null;
  kill_switch_engaged: boolean;
  kill_switch_reason: string | null;
  started_at: string | null;
  stopped_at: string | null;
  last_advanced_at: string | null;
  failure_reason: string | null;
  risk_config: Record<string, unknown>;
  is_live_state: boolean;
}

export interface SessionOrder {
  sequence: number;
  engine_order_id: string;
  instrument: string;
  side: string;
  quantity: string;
  filled_quantity: string;
  average_fill_price: string | null;
  status: string;
  order_type: string;
  submitted_timestamp: number | null;
}

export interface SessionFill {
  sequence: number;
  engine_fill_id: string;
  engine_order_id: string;
  instrument: string;
  side: string;
  quantity: string;
  price: string;
  commission: string;
  fill_timestamp: number | null;
}

export interface SessionPosition {
  instrument: string;
  quantity: string;
  average_price: string;
  market_price: string | null;
  unrealized_pnl: string | null;
  realized_pnl: string | null;
}

export interface SessionEvent {
  sequence: number;
  created_at: string;
  severity: string;
  kind: string;
  message: string;
}

export interface Notification {
  id: string;
  severity: string;
  kind: string;
  title: string;
  body: string;
  resource_type: string | null;
  resource_id: string | null;
  created_at: string;
  read_at: string | null;
}

export interface AuditEvent {
  id: string;
  created_at: string;
  actor_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  payload: Record<string, unknown>;
}

export interface Dashboard {
  datasets: { approved: number; pending: number };
  strategies: number;
  backtests: { queued: number; running: number; completed: number; failed: number };
  sessions: { running: number; total: number; halted: number };
  unread_notifications: number;
}

export interface Portfolio {
  modes: Record<string, {
    sessions: Array<{
      session_id: string;
      name: string;
      status: string;
      currency: string;
      starting_cash: string;
      cash: string | null;
      equity: string | null;
      realized_pnl: string | null;
      unrealized_pnl: string | null;
      commission_paid: string | null;
      strategy_version_id: string;
      records_processed: number;
    }>;
    totals: { equity: string; realized_pnl: string; unrealized_pnl: string; commission_paid: string };
  }>;
  note: string;
}
