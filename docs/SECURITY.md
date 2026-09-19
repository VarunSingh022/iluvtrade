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

## Two-factor authentication

TOTP (RFC 6238) via `pyotp`. Nothing cryptographic is invented; what this
application decides is the part that matters more than the algorithm:

| Decision | Why |
|---|---|
| **Enrolment is two-step** | A secret is issued, and MFA is enabled only once a code proves the authenticator has it. Enabling on issue locks out anyone whose authenticator never received it — unrecoverable for the last admin of an organization. |
| **A code cannot be replayed** | TOTP codes are valid for a window, so one works twice within it. The last accepted counter is stored and a code at or below it is refused. |
| **One step of drift, no more** | A wider window multiplies the codes valid at any instant. |
| **The secret is encrypted at rest** | Same AES-GCM envelope as a broker credential, bound to the user id. A database disclosure otherwise mints valid codes forever. |
| **Recovery codes are hashed** | Argon2id, exactly as passwords are — they are password-equivalent. Single-use, and shown once. |
| **Disabling requires proof** | A current code or a recovery code, not merely a session. Disabling MFA is the first thing a stolen session would be used for. |
| **A wrong password is refused before the code is considered** | A caller must not learn whether an account has MFA without first proving the password. |

The secret and the recovery codes appear in exactly one response, at enrolment.
`tests/security/test_api_contract.py` holds a two-entry allowlist for that and
asserts the values are never re-served by any endpoint.

**Not implemented:** WebAuthn/passkeys, and per-organization enforcement (a user
enables MFA on their own account; an admin cannot yet require it).

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

### Rotation

Implemented. `ILUVTRADE_RETIRED_SECRET_KEYS` lists keys that may still
*decrypt*; nothing is ever encrypted under one.

```bash
# 1. append the current key to the retired list, set the new one, restart
# 2. see what would change
iluvtrade rotate-credentials --dry-run
# 3. re-seal
iluvtrade rotate-credentials
# 4. only now remove the old key from the retired list
```

The order matters: removing the old key first makes every stored credential
unreadable. `decrypt_credentials` names that specific cause rather than
reporting a generic failure, so the mistake is recoverable.

A credential that cannot be decrypted is **left exactly as it was** and
reported. Destroying the ciphertext would turn a recoverable misconfiguration
into permanent loss.

**Retired keys are never dropped automatically.** Removing one is the
operator's decision, taken after a rotation reports nothing left under it.

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
| **Shared rate-limit state** | a Redis backend behind `RateLimitBackend`. `SharedBackend` documents what it must provide — atomic increment-and-expire, server-side expiry, a deliberate fail-open/fail-closed choice, and the store's own clock. Not written, because an untested `INCR`/`EXPIRE` pair that races is worse than a limiter known to be local |
| **Email / push delivery** | an SMTP relay or push provider behind `NotificationChannel`. The catalogue already marks which kinds are worth sending |
| **Email verification and password reset** | there is no email channel at all |
| **Webhook signature verification** | no webhooks exist yet; a payment provider will need HMAC verification and replay protection |
| **External audit anchoring** | the hash chain detects row-level tampering but not a full rewrite; anchoring the head externally is what would |
| **Dependency scanning in CI** | no automated CVE checking |
| **Penetration testing** | none has been performed |
| **Load testing** | none has been performed |

## Reporting

Security issues should go to the repository owner privately, not through a
public issue.

---

# Release-candidate additions

The sections below were added during the release-candidate audit. Each records
a decision, not just a feature.

## Production configuration fails closed

`Settings.deployment_problems()` enumerates the production settings that are
unsafe, and `get_settings()` refuses to return them. A misconfigured production
deployment does not start.

It replaces a check that **could never fire**. The previous guard read
`if settings.is_production and not settings.secret_key` — but `secret_key` has
a `default_factory`, so an unset key is a perfectly valid random string and the
condition was always false. Production would have started with a per-process
key: every session dropped on restart, and every stored broker credential and
TOTP secret sealed under a key that no longer exists.

What is refused, and why each one matters:

| Setting | Refused when | Because |
|---|---|---|
| `ILUVTRADE_SECRET_KEY` | unset, under 32 characters, or a placeholder | It derives the session HMAC *and* the credential-encryption key |
| `ILUVTRADE_RETIRED_SECRET_KEYS` | contains the active key | The rotation silently becomes a no-op |
| `ILUVTRADE_AUTO_CREATE_TABLES` | on | `create_all` ignores drift; Alembic owns the production schema |
| `ILUVTRADE_DATABASE_URL` | SQLite, without an explicit acknowledgement | Threaded workers and session runners write concurrently |
| `ILUVTRADE_ALLOWED_ORIGINS` | `*`, or any non-https origin | This API allows credentials |
| `ILUVTRADE_FETCH_ALLOW_LOOPBACK` | on | It is the SSRF escape hatch that exists for tests |
| `ILUVTRADE_RATE_LIMIT_ENABLED` | off, with nothing claiming to enforce limits elsewhere | Unlimited password guessing |
| `ILUVTRADE_LIVE_TRADING_ENABLED` | on, with no broker credentials | Every live session would fail at the venue |
| `ILUVTRADE_PAYMENT_PROVIDER` | not an implemented provider | Better than discovering it at the first purchase |

Two of these have deliberate escape hatches — SQLite, and disabling in-process
rate limiting — and both require setting a variable that *names the trade*
(`ILUVTRADE_ALLOW_SQLITE_IN_PRODUCTION`, `ILUVTRADE_RATE_LIMIT_ENFORCED_EXTERNALLY`).
Turning a control off should be a statement about where it went, not a quiet
omission.

`iluvtrade check-config` reports the same assessment without starting the
application, and `deploy/entrypoint.sh` runs it before every start.

### A related defect: the documented syntax did not work

`.env.example` documented `ILUVTRADE_FETCH_ALLOWED_HOSTS` as comma-separated.
pydantic-settings parses a complex field from the environment as JSON, so
following the documentation was a **startup crash** — on the SSRF allowlist,
which is the setting an operator most needs to be able to change. Both forms
are now accepted, and a parametrised test covers comma-separated, whitespace-
padded, JSON and empty.

## Two-factor authentication in the browser

The backend was complete before this audit; nothing in the product reached it.

Three rules the UI keeps:

1. **Nothing is persisted.** The secret and the recovery codes live in React
   state for the length of the enrolment and are gone on navigation — no
   `localStorage`, no `sessionStorage`, no URL. A test asserts both stores are
   empty after enrolment.
2. **The secret is never shown again**, because the server cannot show it: it
   is stored encrypted and no endpoint returns it.
3. **Turning MFA off requires a current code.** The server enforces it; the UI
   says why, rather than presenting a bare button.

The QR code is rendered from the module matrix as React `<rect>` elements
rather than through the library's `createImgTag`/`createSvgTag`, which return
markup strings and would mean `dangerouslySetInnerHTML` on the one page that
displays a one-time secret.

### The login challenge is distinguishable, twice

A correct password on an MFA-enabled account answers **401 with
`error.code == "MfaRequired"`** and an `X-MFA-Required` header.

Both, on purpose. A cross-origin client cannot read a response header unless
CORS exposes it — and it did not, until this audit added `expose_headers` — and
a proxy is free to strip one. The body always arrives. Without a distinguishable
answer the UI tells someone with a working password that it is wrong, and the
account becomes unreachable through the app.

## Password reset

Everything except delivery is implemented and tested: a 256-bit token from
`secrets`, stored only as an HMAC under the application secret, valid for one
hour, usable once, superseding any outstanding token, and revoking **every
session on the account** when used — which is the point of a reset rather than
a settings-page password change.

`POST /auth/password-reset/request` answers **503**, identically for a known and
an unknown address, because the refusal is decided before the address is looked
up. It is therefore not an enumeration oracle. A `202 Accepted` would have been
the easy thing to return and would have been a lie.

`iluvtrade issue-password-reset <email>` is the operator path. It needs shell
access on the application host — which already implies database access — and it
writes the same audit event a delivered reset would. It was exercised end to end
against a running server during this audit.

## Invitations

Three things together stop a leaked invitation token from being a membership:
the token is hashed at rest, accepting requires being **signed in**, and the
signed-in account's email must match the address the invitation names. The role
is fixed at creation and there is no field on the accept request that could
carry one; an inviter also cannot grant a role above their own.

Every refusal — unknown, expired, revoked, already used, wrong address — returns
the same message. Which one it was is information about someone else's
workspace.

## Vulnerability classes kept absent by construction

`tests/security/test_code_execution_surface.py` parses the application's own
source with `ast` and fails when a construct appears:

| Rules out | Check |
|---|---|
| Arbitrary code execution | No `eval`, `exec`, `compile`, `__import__` |
| Unsafe deserialization | No `pickle`, `marshal`, `dill`, `shelve`, `yaml` |
| Command injection | No `subprocess`, `os.system`, `os.popen` |
| SQL injection | No f-string or concatenation passed to `execute`/`text` |
| XSS | No `dangerouslySetInnerHTML`, `innerHTML`, `new Function`, `document.write` |
| Path traversal | No filesystem call outside `storage.py`, whose `resolve()` refuses a key that escapes the root |

A grep proves nothing about tomorrow. These fail on the commit that introduces
the construct.

## Dependency risk

`scripts/audit-dependencies.sh` compares `npm audit` against
`scripts/accepted-advisories.txt` and fails on anything not yet assessed. Seven
advisories are currently open; the assessment is what makes them acceptable, not
their presence on a list.

**Development tooling — never reaches the browser.** `vitest` (critical),
`vite` (high), `esbuild`, `@vitest/mocker`, `vite-node`. Every one is in
`devDependencies`. The vulnerabilities are "a website you visit can talk to your
dev server" and "the Vitest UI server can read files" — real, and scoped to a
developer's machine, not to a deployment.

**Shipped in the bundle — assessed as not reachable here.**
`react-router` / `react-router-dom` (moderate), with two advisories:

* *Arbitrary constructor injection via `deserializeErrors()` in SSR hydration.*
  This application has no SSR. It uses `BrowserRouter`, not
  `createBrowserRouter`, and never hydrates server-rendered error state.
* *Open redirect via a backslash in `<Link>` and `useNavigate`.* Exploitable
  only when a route target is built from user-controlled input. Every `to=` and
  `navigate()` in this codebase is a literal path or a server-generated UUID;
  there is no free-text path anywhere.

The fix for both is a **major** version bump (`react-router-dom` 6 → 7), taken
at the end of a release-candidate audit, to close advisories that are not
reachable. That trade was not worth making. It is recorded here so the decision
is visible and can be revisited — not omitted so the report looks clean.

The npm dependency set is also pinned by a test: `dependencies` must be exactly
`react`, `react-dom`, `react-router-dom` and `qrcode-generator`. A fifth entry
is one more package with script access to a signed-in session, which is a
decision rather than a line to append.

---

# Two correctness defects found during the forensic pass

Both are recorded here rather than only in a commit message, because both are
the kind of bug that passes every test until the day it does not.

## 1. The test suite wrote into the developer's real database

**What happened.** On 2026-09-19 the suite wrote **33 rows** into
`var/iluvtrade.db` — a user, an organization, a membership, a subscription, an
auth session, a dataset with its source and version, a strategy and version, a
trading session with its events, orders, fills and position, four notifications
and ten audit events.

**Root cause.** `SessionRunner.launch()` starts a **daemon** thread. Nothing
waits for it. The per-test fixture, on teardown, cleared the settings cache and
reset the engine — while a runner was still polling. The runner's next call to
`get_session_factory()` re-resolved settings *without* the monkeypatched
`ILUVTRADE_DATABASE_URL` and got the default: the developer's own file.

It also produced a flake that looked unrelated, because a background thread's
exception is reported by pytest against whichever test is running *next*.

**Why it is a correctness bug and not a cleanup task.** A daemon thread that
re-reads configuration after its caller is gone will pick up whatever the
process defaults to. In a test that is the developer's database; in a
deployment it would be whatever a reloaded configuration said, which is a
different class of problem with the same shape.

**Three mechanisms now stand in the way**, kept together because they fail
differently:

| | |
|---|---|
| **Prevention** | The test suite patches `create_engine` and **refuses** to build an engine for that path — before a connection opens, not after a row is written. An explicit `allows_real_database` fixture is the documented exception; nothing uses it |
| **Containment** | Every application thread is stopped and joined **before** the settings cache is cleared. `SessionRunner.join_all()` and the new `stop_all_pools()` do this, and the fixture asserts nothing survived |
| **Detection** | The file's SHA-256 is compared across the whole run, in case something reaches it by a route the guard does not recognise |

`tests/test_isolation.py` exercises all three, and **reproduces the original
failure sequence** — settings cache cleared, override removed, engine reset,
then a call from a background thread — asserting it now raises.

Four consecutive full runs leave the file byte-identical.

**Cleanup.** The 33 rows were removed by primary key, inside one transaction,
after a byte-identical backup was taken and a rehearsal on a copy. The
partition was unambiguous: 82 rows belonged to the original demo organization,
31 to the test one, **zero to neither**, and **zero rows referenced across the
two**. Every one of the 85 original rows is present afterwards with an
identical row hash. The per-tenant audit chain made this safe by construction —
the demo chain's head hash is unchanged, because its hashes never referenced
the other tenant's events.

## 2. A session could be started more than once

**What happened.** Five simultaneous `POST /trading/sessions/{id}/start`
requests produced **three** successes. Three runner threads then fed bars into
one portfolio, so the position would have been triple.

**Root cause.** `_transition` was a read-modify-write:

```python
row = get(...)                      # SELECT   — all three read CREATED
if row.status not in allowed_from:  # check    — all three pass
    raise
row.status = to                     # UPDATE   — all three write
```

Three separate steps with nothing between them. Every caller read `CREATED`
before any of them wrote.

**Fix.** The current status is now part of the `WHERE` clause of a single
`UPDATE`, so the database picks the winner exactly once and a caller that
matched no row is refused. This is the pattern the runner's own
`STARTING → RUNNING` claim already used; it was simply missing here.

**How it is now tested.** The threaded test caught the old implementation about
**one run in six**, which is not a regression test worth relying on. Simulating
the losing caller's stale view is no better — mutating the ORM object marks it
dirty and SQLAlchemy flushes it before the next statement, so the simulation
writes the very row it was pretending to have read.

So the regression test watches the **SQL that actually reaches the database**
and asserts the status change is one `UPDATE` whose `WHERE` carries the current
status. It fails on the old implementation 5 times out of 5, and is not a race
at all.

---

# Three packaging defects found by the pre-publication pass

None is a vulnerability. All three are the same shape: **something worked only
because the development environment already happened to be in the right
state**, and no test could see it because every test ran in that environment.

## 1. The package could not be installed

`pyproject.toml` declared `readme = "../README.md"`. Hatchling refuses a readme
outside the project directory, so `pip install -e ".[dev]"` — the command in
`DEPLOYMENT.md`, in the Dockerfile and in CI — failed at metadata generation.

Nothing noticed because `pytest` sets `pythonpath = ["."]` and every command
was run from `backend/`, where the working directory is on `sys.path`. The
package had never been installed; the `iluvtrade` console script declared in
`[project.scripts]` did not exist.

Fixed by removing the key. The evidence that it is fixed is a clean virtual
environment: the install succeeds, the console script runs, `import iluvtrade`
works from another directory, and the suite passes 501 there.

## 2. `email-validator` was an undeclared dependency

`EmailStr` raises `ImportError` — not a validation error — when
`email-validator` is absent. Registration, login, invitations and password
reset all carry an email field, so **every one of those endpoints would fail at
request time** on a clean install.

It was present in the development environment by accident. Installing into a
clean one produced 153 errors from 498 tests.

Fixed by declaring `pydantic[email]` rather than bare `pydantic`.

This is the second undeclared dependency to ship (`pyotp` was the first), so
there is now a structural guard: `tests/unit/test_dependency_declaration.py`
walks every import in the package and fails on one that is not declared. It
would have caught `pyotp`. It **cannot** catch this one — nothing here imports
`email_validator`, pydantic does — and the test says so rather than implying
otherwise. Only a clean-environment install reveals a transitive gap, which is
what CI's `pip install -e ".[dev]"` on a fresh runner is for.

## 3. `types-requests` was declared and never installed

Nothing imports `requests`; mypy is clean without it. A dependency that is
absent from every environment is one nobody would notice breaking. Removed.

## Why this matters more than the defects themselves

Every gate was green throughout. 498 tests, ruff, mypy, Alembic, the demo — all
passing, in an environment where the application could not have been installed
and would have failed on its first registration request.

A test suite tells you the code is consistent with itself. It does not tell you
the *package* is installable, and no amount of adding tests inside that
environment would have. The two things that found these were building a wheel
from only the paths the Dockerfile copies, and installing into an empty
virtual environment.
