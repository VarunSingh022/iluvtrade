# API

Base path `/api/v1`. Interactive documentation at `/api/docs`; the OpenAPI
schema at `/api/openapi.json`.

## Authentication

Two mechanisms:

- **Session cookie** (`iluvtrade_session`, HttpOnly) — what the browser app
  uses. State-changing requests must also send `X-Requested-With`, which a
  cross-origin form post cannot set without a preflight the browser refuses.

When an account has two-factor authentication enabled, `POST /auth/login`
without `mfa_code` answers `401` with an `X-MFA-Required: true` header — a
distinct signal, so a client presents a challenge instead of reporting the
password as wrong. A wrong code and a wrong password give the same message.
- **Bearer token** — `Authorization: Bearer <token>` from a login response.
  Accepted on any method, because a header token is not attached automatically
  by the browser.

## The error envelope

Every error, from any layer, has one shape:

```json
{ "error": { "code": "NotFoundError", "message": "Dataset version … is pending_approval, not approved." } }
```

Validation errors add `fields`. A client can branch on `error.code` without
parsing prose.

| Status | Meaning here |
|---|---|
| 400 | the request was understood and refused; the message says why |
| 401 | not authenticated |
| 403 | authenticated, not permitted — including entitlement refusals |
| 404 | absent, **or another tenant's** — deliberately indistinguishable |
| 409 | a conflict with immutable state, such as editing a published version |
| 413 | upload too large |
| 422 | the body did not validate; `error.fields` names the offending fields |
| 429 | rate limited; carries `Retry-After` and `error.policy` |

## Endpoints

### Meta

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | version, environment, **loaded AlphaLab version**, live-trading flag |

### Auth

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | creates the user and a personal workspace; logs in |
| POST | `/auth/login` | |
| POST | `/auth/logout` | 204 |
| GET | `/auth/me` | |
| PATCH | `/auth/me` | display name, and the *user* half of the live-trading gate |
| GET | `/auth/mfa` | enrolment status and remaining recovery codes |
| POST | `/auth/mfa/enrol` | issue a secret and recovery codes; **does not enable** |
| POST | `/auth/mfa/confirm` | enable, by proving the authenticator holds the secret |
| POST | `/auth/mfa/recovery-codes` | replace every code; requires a current factor |
| POST | `/auth/mfa/disable` | requires a current code — a session is not enough |

### Datasets

| Method | Path | Notes |
|---|---|---|
| POST | `/datasets/inspect` | detect a schema, store nothing |
| POST | `/datasets/upload` | multipart; returns the full detail with the quality report |
| POST | `/datasets/fetch` | from an allowlisted URL, through the same pipeline |
| GET | `/datasets` | datasets with their versions |
| GET | `/datasets/versions/{id}` | schema, quality, transformations, rejected rows, provenance |
| GET | `/datasets/versions/{id}/preview` | first rows of the canonical data |
| POST | `/datasets/versions/{id}/approve` | the gate — nothing may use a version until this |
| POST | `/datasets/versions/{id}/reject` | |

### Strategies

| Method | Path | Notes |
|---|---|---|
| GET | `/strategies/implementations` | what this deployment can run |
| GET · POST | `/strategies` | |
| GET | `/strategies/{id}` | |
| POST | `/strategies/{id}/versions` | parameters validated against the schema now |
| PATCH | `/strategies/versions/{id}` | drafts only; 409 on a published version |
| POST | `/strategies/versions/{id}/publish` | freezes it and computes `content_hash` |

### Backtests

| Method | Path | Notes |
|---|---|---|
| POST | `/backtests` | **202**; idempotent, `deduplicated` says whether it was a repeat |
| GET | `/backtests` | |
| GET | `/backtests/{id}` | |
| POST | `/backtests/{id}/cancel` | a running job is *asked* to stop; the response may still say running |
| GET | `/backtests/{id}/result` | metrics, the full result document, and the reproducibility record |

### RedDesk

| Method | Path | Notes |
|---|---|---|
| GET | `/reddesk/billing-status` | whether a purchase actually charges anything |
| GET | `/reddesk/discover` | published listings, cross-tenant by design |
| GET | `/reddesk/listings/{id}` | |
| GET | `/reddesk/my-listings` | this org's listings at any status |
| POST | `/reddesk/listings` | |
| POST | `/reddesk/listings/{id}/versions` | attach a published version and its evidence |
| GET | `/reddesk/listings/{id}/validate` | what blocks publication |
| POST | `/reddesk/listings/{id}/submit` · `/review` · `/publish` · `/withdraw` | |
| POST | `/reddesk/purchases` | returns the purchase **and** the entitlement |
| GET | `/reddesk/entitlements` | what this org may run |
| POST | `/reddesk/entitlements/{id}/advance` | rolling licences only; audited |
| POST | `/reddesk/listings/{id}/rate` | entitlement holders only |

### Brokers

| Method | Path | Notes |
|---|---|---|
| GET | `/brokers/supported` | includes `verified_against_live_venue` |
| GET · POST | `/brokers` | |
| GET | `/brokers/{id}` | |
| POST | `/brokers/{id}/zerodha/login-url` | |
| POST | `/brokers/{id}/zerodha/authorize` | exchanges `request_token`; the access token is never returned |
| POST | `/brokers/{id}/disconnect` | revokes and destroys the stored credential |

**No broker response carries a credential.** A test asserts it against every
endpoint.

### Trading

| Method | Path | Notes |
|---|---|---|
| GET · POST | `/trading/sessions` | live sessions need all three gates |
| GET | `/trading/sessions/{id}` | includes `is_live_state` |
| POST | `/trading/sessions/{id}/start` · `/pause` · `/resume` · `/stop` | |
| POST | `/trading/sessions/{id}/kill` | terminal; not resumable |
| GET | `/trading/sessions/{id}/orders` · `/fills` · `/positions` · `/events` | |

### Portfolio and platform

| Method | Path | Notes |
|---|---|---|
| GET | `/portfolio` | per-session valuations; paper and live **never summed** |
| GET | `/positions` · `/orders` | across sessions, tagged with mode |
| GET | `/dashboard` | |
| GET | `/notifications` · POST `/notifications/read-all` | |
| GET | `/audit` | admin only; each event carries its chain fields |
| GET | `/audit/verify` | admin only; recomputes the chain and reports every break |

## Rate limits

Login, registration, ingestion, fetching, backtest submission, broker token
exchange, marketplace writes and session starts are rate limited. A refusal is
`429` in the same envelope, with `error.policy`, `error.retry_after_seconds` and
a `Retry-After` header. Limits count failed calls too. See `SECURITY.md` for the
table.

## Correlation

Every response carries `X-Request-ID`. An inbound one is adopted rather than
replaced, so a trace that began at a load balancer stays one trace. The same id
is persisted on a backtest job and adopted by the worker thread, so one
identifier spans the HTTP request, the job and the engine run. See
[OBSERVABILITY.md](OBSERVABILITY.md).

## Conventions

- **Money is a string**, always. A float would lose the precision AlphaLab
  maintains and break the accounting identity.
- **Timestamps** are ISO-8601 with an offset for application events, and epoch
  seconds for market data.
- **`null` means AlphaLab produced nothing.** It is never replaced with a zero
  or an estimate.
- **Every response model is an allowlist**, so a field added to an ORM object
  cannot leak into a response.
- **Every request model forbids unknown fields**, so a typo is an error rather
  than a silently ignored setting.
