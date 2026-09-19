# Security

What is implemented, what it defends against, and what is deferred. The last
section matters most: a security document that lists only wins is not useful.

## Identity and sessions

| Control | Implementation |
|---|---|
| Password storage | Argon2id, parameters explicit in `config.py` so a change is a reviewable diff |
| Password policy | 12 characters minimum, length only — composition rules push people toward `Passw0rd!` |
| Rehashing | `check_needs_rehash` on every successful login |
| Session tokens | `secrets.token_urlsafe(32)`, stored only as HMAC-SHA256 under the app secret |
| Timing | An unknown address is verified against a real dummy hash, so "no such user" and "wrong password" cost the same |
| Cookie | `HttpOnly`, `SameSite=Lax`, `Secure` in production |
| Revocation | Logout sets `revoked_at`; membership removal kills the session at resolve time |

A database disclosure does not yield usable sessions: the rows hold HMACs, not
tokens.

## CSRF

Cookie authentication is honoured for safe methods, and for unsafe methods only
when `X-Requested-With` is present. A cross-origin form post cannot set that
header without a preflight the browser will refuse. A bearer token is accepted
on any method, because a header token is not attached automatically.

`tests/integration/test_workflow.py` asserts both halves.

## Tenant isolation

The strongest control in the application, and the one most carefully enforced.

- `OrgScopedMixin` marks a model as tenant-scoped.
- `platform.tenancy.scoped(model, org_id)` **refuses** to build a query for a
  model without it. There is no path where a tenant table is queried unfiltered.
- `require_owned()` fetches by id and *then* checks ownership, raising
  `NotFoundError` — not "forbidden", because telling an attacker an id exists
  but belongs to someone else is itself a disclosure.
- `tests/security/test_tenant_isolation.py` enumerates every table in the
  metadata. A new table either carries `organization_id` or is listed as global
  with a written reason.

## Broker credentials

| Control | Implementation |
|---|---|
| At rest | AES-256-GCM under an HKDF-derived key |
| Integrity | GCM authenticates; a tampered blob fails to decrypt |
| Binding | The connection id is additional authenticated data, so a ciphertext copied to another connection will not decrypt |
| In memory | `SecretString` excludes the value from `repr`, `str`, equality and hashing |
| In transit to the client | Never. No response model has a field that could carry one |
| In logs and audit | `audit.redact()` replaces any credential-shaped key before serialization |

`tests/security/test_broker_secrets.py` checks every broker endpoint's response
body for credential markers, and asserts the crypto properties directly.

**Not implemented: key rotation.** Changing `ILUVTRADE_SECRET_KEY` invalidates
every session and every stored broker credential. A real rotation needs
versioned key material, a re-encryption pass and a window where both keys
decrypt. It is not built and is not pretended to be.

## Data ingestion

### SSRF

The fetcher is the most dangerous surface in the application, so the controls are
layered:

| Control | Stops |
|---|---|
| Scheme allowlist (http/https) | `file://`, `gopher://`, `ftp://` |
| Host allowlist, empty by default | everything, until an administrator names a host |
| Port allowlist | an allowlisted host being used to reach SSH or a database on it |
| **DNS-resolution address check** | a public hostname resolving to `169.254.169.254`, `10.0.0.0/8`, loopback, link-local |
| Manual redirects, every hop re-validated | the classic bypass: an allowed host redirecting to the metadata service |
| Streamed size ceiling | a lying `Content-Length` |
| Decompression ceiling | a zip bomb |
| Content-type check | importing a login page as if it were data |
| Timeout | a hanging socket |

### Uploads

- Size enforced against bytes actually read, not the declared length.
- Filename is stripped to a basename; storage keys are server-generated UUIDs.
- `common.storage.resolve()` refuses any key that escapes the storage root, so a
  crafted id cannot read `../../etc/passwd`.
- The file is parsed as CSV only. No deserialization of untrusted formats.

## Strategy execution

**Marketplace strategies are not executed as code.** A listing names an
implementation registered in this repository; uploading Python is not a feature.

This is the honest position, not the finished one. PHASE 10 of the original
brief describes an isolation boundary — CPU, memory and wall-clock limits, a
read-only filesystem, no network, an import allowlist, no subprocesses, secret
isolation — and **none of it is built**. Building half of it would read as safety
while providing none, so the feature that would need it does not exist.

To support seller code, that boundary has to come first. It is the single
largest piece of deferred work in this repository.

## Application hardening

- Every route declares a response model, so a field added to an ORM object
  cannot leak into a response by accident.
- Every request model forbids unknown fields, so a typo'd parameter is an error
  rather than a silently ignored setting.
- SQL is exclusively through SQLAlchemy's expression language. No string
  interpolation into queries anywhere.
- CSP without `unsafe-inline` for scripts; the frontend is a built bundle with
  no inline script, so an injected `<script>` has nowhere to execute.
- `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`, and HSTS in production.
- Unhandled exceptions return a generic message; the detail goes to the log,
  because an internal error's text can carry a query, a path or a value.

## Trading safety

- Risk limits are enforced **inside AlphaLab**, before the OMS sees an order.
  Nothing in this application can route around them, because the refusal happens
  in the engine rather than in a check this application performs first.
- A refusal is an event with its own kind, never a silent no-op.
- Live trading needs three independent gates: deployment configuration, the
  user's own setting, and a per-session confirmation.
- The kill switch is available to any trader — an emergency stop that needs
  someone else's permission is not an emergency stop — and `HALTED` is terminal.
- Order submission is idempotent by construction: the Zerodha connector tags
  each order with the OMS order id, so a lost response reconciles rather than
  duplicating.

## Deferred, and honestly so

| Gap | What it needs |
|---|---|
| **Sandboxed strategy execution** | container or gVisor isolation, resource limits, an import allowlist. The largest single piece of deferred work. |
| **Secret rotation** | versioned key material and a re-encryption pass |
| **Rate limiting** | nothing throttles login or API calls; a reverse proxy or a middleware with a shared store |
| **MFA** | no second factor on any account |
| **Email verification and password reset** | there is no email channel at all |
| **Webhook signature verification** | no webhooks exist yet; a payment provider will need HMAC verification and replay protection |
| **Audit log tamper-evidence** | the trail is append-only by convention, not by hash chaining or an append-only store |
| **Dependency scanning in CI** | no automated CVE checking |
| **Penetration testing** | none has been performed |

## Reporting

Security issues should go to the repository owner privately, not through a
public issue.
