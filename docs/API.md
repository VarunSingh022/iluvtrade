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

---

# The frozen surface

**84 routes.** `tests/security/test_api_surface.py` holds the complete list as a
literal and fails when reality and the list disagree — in **either** direction,
because a route that quietly disappears breaks a client just as thoroughly as
one that quietly appears.

Updating that list is the correct response to an intentional change. Doing it
without also asking "does anything call this?" is the mistake the list exists to
surface: the audit that produced it found two endpoints reachable by nobody —
no screen, no demo, no test — while the rating one of them produced was already
being rendered on the discovery screen.

Every route is checked by an enumerating test rather than a sampled one:

| Property | How it is checked |
|---|---|
| Authentication | Every route except the five public ones refuses an anonymous caller |
| CSRF | Every mutating route refuses a cookie-authenticated request without `X-Requested-With` |
| Error envelope | Every error response is `{"error": {"code", "message"}}` |
| Response model | Every route declares one, or is on a five-entry allowlist with the reason |
| Tenant scope | A 32-operation cross-tenant matrix; a foreign id is always 404, never 403 |
| Bounded lists | Collection endpoints accept a `limit` and clamp an absurd one |

## Public routes

Five, and each is anonymous-safe by construction rather than by omission:

| Route | Why it has to be public |
|---|---|
| `GET /api/health` | Liveness, for a load balancer |
| `POST /api/v1/auth/register` | There is no account yet |
| `POST /api/v1/auth/login` | Same |
| `GET /api/v1/auth/password-reset` | Describes the **deployment**, not any account: whether a reset can be delivered at all. The sign-in screen reads it to decide whether to offer a link that would go nowhere |
| `POST /api/v1/auth/password-reset/request` | Reached by someone who cannot sign in. Answers identically for a known and an unknown address |
| `POST /api/v1/auth/password-reset/confirm` | Same; requires a 256-bit token |

All are rate limited.

## Added in the release-candidate phase

### Accounts

| Route | Notes |
|---|---|
| `GET /api/v1/auth/organizations` | Workspaces this account may act in |
| `POST /api/v1/auth/switch-organization` | Issues a **new** session and revokes the current one. A session names one organization and every authorization decision reads it; mutating the row would let one token act in two tenants across its life |
| `GET /api/v1/auth/password-reset` | `{available, channel, notice}` |
| `POST /api/v1/auth/password-reset/request` | `202` when a channel exists, **`503 DeliveryUnavailable`** when none does — decided before the address is looked up, so it cannot enumerate accounts |
| `POST /api/v1/auth/password-reset/confirm` | Sets the password and revokes every session on the account |

### Organizations

| Route | Role | Notes |
|---|---|---|
| `GET /api/v1/organizations/members` | any member | Only the caller's own workspace; there is no parameter naming an organization |
| `GET /api/v1/organizations/invitations` | admin | Never carries a token |
| `POST /api/v1/organizations/invitations` | admin | Returns the token **once**, to the inviter. The role cannot exceed the inviter's own |
| `POST /api/v1/organizations/invitations/{id}/revoke` | admin | A foreign id is 404 |
| `POST /api/v1/organizations/invitations/accept` | any member | Requires the signed-in account's email to match the invitation's. There is no `role` field on this request |

### Audit

`GET /api/v1/audit/verify` now declares a response model
(`AuditVerificationResponse`) instead of returning a bare dict. It was the one
route a client could depend on a field of that was not part of the contract.

## Two response fields that are deliberately secret

`MfaEnrolmentResponse.secret` and `MfaEnrolmentResponse.recovery_codes`. MFA
enrolment cannot work otherwise: the TOTP secret has to reach the user's
authenticator and the recovery codes have to reach the user, and that response
is the single moment either exists outside the server.

The exception is acceptable **because the values are never served again** — the
secret is stored encrypted and the codes hashed — and that is asserted
separately, by sweeping every GET route for them after enrolment. The allowlist
is bound to two entries on one model by its own test. A third is a design
discussion, not a line to append.

## A 503 you should expect under SQLite

`DatabaseBusy` — SQLite serialises writers, and a long transaction (dataset
ingestion holds one for the length of the parse) can make a concurrent write
exceed `busy_timeout`. It carries `Retry-After` and says nothing was changed,
because a generic 500 would tell the user the opposite of the truth: that
something is broken and retrying is pointless.
