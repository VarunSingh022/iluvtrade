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

### Key versioning

Every credential envelope is `version(1) || key_id(8) || nonce(12) ||
ciphertext`, and the header is authenticated as GCM additional data alongside
the connection id — so neither the version nor the key id can be altered without
the decryption failing.

The key id is a truncated SHA-256 of the *derived* key: it says which key sealed
a blob without revealing anything about that key.

This is **not rotation**. Nothing re-encrypts anything, and changing
`ILUVTRADE_SECRET_KEY` still invalidates every stored credential — but the
stored blobs are now self-describing, which is the thing rotation cannot be
added without. Adding the header later would have required a migration over
every credential, guessing which key each one used.

A blob sealed under a different key now fails with a *specific* message naming
that as the cause, rather than a generic decryption failure.

**Still not implemented: rotation itself.** It needs a second active key, a
re-encryption pass, and a window in which both keys decrypt.

## Rate limiting

Per-**principal**, not per-IP. A proxy limits by address and is the right place
for volumetric abuse; it cannot tell two users behind one office NAT apart,
which is exactly the case where an account-level limit matters. Both belong in
a real deployment; neither replaces the other.

| Policy | Limit | Guards against |
|---|---|---|
| `login` | 10 / minute / address | credential guessing |
| `register` | 5 / hour / address | account-farming |
| `ingest` | 30 / minute / user | CPU and disk exhaustion |
| `fetch` | 10 / minute / user | using the server as an outbound proxy |
| `backtest` | 60 / minute / user | occupying every worker |
| `broker_auth` | 10 / 5 minutes / user | tripping the venue's own limits |
| `marketplace` | 60 / minute / user | durable-record spam |
| `session` | 30 / minute / user | thread exhaustion |

A refusal is `429` with `Retry-After`, in the same error envelope as everything
else. The count includes *failed* calls: a policy that only counted successes
would not stop abuse.

**The limiter is in-process.** Two API workers each enforce the limit
separately, so the effective limit is `limit x workers`. That is stated rather
than hidden. `RateLimitBackend` is the seam a Redis implementation plugs into;
for a single-node deployment the limits are exact.

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
| **Explicit CIDR deny-list** | ranges the stdlib misses — see below |
| IPv4-mapped IPv6 unwrapping | `::ffff:169.254.169.254` reaching the metadata service |
| Userinfo refusal | `https://data.example.com@evil.example.com/`, which points at *evil* |
| Manual redirects, every hop re-validated | the classic bypass: an allowed host redirecting to the metadata service |
| Streamed size ceiling | a lying `Content-Length` |
| Decompression ceiling | a zip bomb |
| Content-type check | importing a login page as if it were data |
| Timeout | a hanging socket |

The deny-list is not belt-and-braces; it is load-bearing. Python's
`ipaddress.is_private` **does not** cover `100.64.0.0/10` (RFC 6598
carrier-grade NAT — routable-looking, and real internal infrastructure at many
ISPs and clouds) or `192.88.99.0/24` on Python 3.12, and what it covers changes
between versions. Relying on the flags alone would mean the set of addresses
this application connects to silently changes with an interpreter upgrade. Both
gaps were found by a test and both are now explicitly denied.

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

## Audit tamper-evidence

Each event is hashed over its own canonical form and its predecessor's hash:

```
event_hash = SHA-256( canonical(event) || previous_hash )
```

Serialization is canonical — fixed field order, sorted keys, explicit nulls,
timestamps as integer microseconds — so the same event hashes identically on any
machine and survives a database round-trip with different sub-second precision.

The chain is **per organization**. A global chain would require reading one
tenant's events to verify another's, which is the coupling the rest of the
schema exists to avoid.

`(organization_id, sequence)` is unique, so two events cannot claim one position
— not even transiently. `GET /api/v1/audit/verify` recomputes a chain and
reports every break, and each audit response carries its own `sequence`,
`previous_hash` and `event_hash` so a reader can verify independently rather
than trusting the server's own check.

### What it detects

An altered payload, action or outcome; a deleted event; two events reordered;
an inserted event; a hash that does not match its content; a hash recomputed for
one event without recomputing every later one.

### What it does not

**This is not a WORM store and is not equivalent to one.** An attacker with
write access to the table can recompute the entire chain and leave it
consistent. Nothing self-contained can prevent that — it needs the head
published somewhere the attacker does not control: an external append-only
store, a transparency log, or a periodic signed anchor. None of those exists
here.

A chain **backfilled by migration** proves even less about the period before the
backfill: it hashes the rows as they stood at migration time. The migration says
so in its output rather than leaving it to be discovered.

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
| **Secret rotation** | a second active key and a re-encryption pass. Envelopes already carry a key id, so this no longer needs a migration first |
| **Shared rate-limit state** | the limiter is per-process; a Redis backend behind `RateLimitBackend` would make limits exact across workers |
| **MFA** | no second factor on any account |
| **Email verification and password reset** | there is no email channel at all |
| **Webhook signature verification** | no webhooks exist yet; a payment provider will need HMAC verification and replay protection |
| **External audit anchoring** | the hash chain detects row-level tampering but not a full rewrite; anchoring the head externally is what would |
| **Dependency scanning in CI** | no automated CVE checking |
| **Penetration testing** | none has been performed |
| **Load testing** | none has been performed |

## Reporting

Security issues should go to the repository owner privately, not through a
public issue.
