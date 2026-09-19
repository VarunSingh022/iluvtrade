# The seller-code sandbox contract

**Status: NOT IMPLEMENTED. Seller-code execution is disabled and there is no
code path that runs uploaded Python.**

This document specifies the contract a sandbox would have to satisfy. It exists
because the contract is the hard part — an isolation mechanism can be chosen
later, but the boundary it must enforce has to be decided first, and deciding it
under deadline pressure while a feature waits is how sandboxes end up porous.

## Why nothing was built

A partial sandbox is worse than none. It reads as a safety guarantee to
everyone downstream — the listing page, the operator, the buyer — while
providing an attacker with an ordinary Python process. Every control below has
to hold simultaneously; missing one usually means missing all of them, because
an attacker only needs a single escape.

So the current design removes the need for one: a listing names a strategy
**implementation registered in this repository** (see
`iluvtrade/alphalab_bridge/strategies.py`). Uploading code is not a feature, so
there is nothing to sandbox.

## Does the architecture leave room for one?

Yes, and this was checked rather than assumed:

| Property | Where it already holds |
|---|---|
| Strategy identity is separate from strategy code | `StrategyVersion.implementation_key` is an indirection, not an import |
| A version is immutable and content-hashed | `strategies.service.publish` / `verify_integrity` |
| Execution is already out-of-request | the backtest worker and session runner are separate threads today, and `claim_next` is written for an external worker |
| The engine boundary is one directory | `alphalab_bridge/`, enforced by test |
| Results are a serializable document | `alphalab_bridge.results.extract` already produces plain JSON |
| Refusals are first-class | `risk_refusals`, `skipped_records`, `unpriced_assets` |

What is missing is the isolation mechanism and the artifact model — not a
restructuring of the application.

## Input contract

What crosses into the sandbox, and nothing else:

```
StrategyArtifact          content-addressed, immutable
  ├── entry point         module:ClassName, declared not discovered
  ├── source archive      tar, size-capped, no symlinks, no absolute paths
  └── declared imports    an allowlist the artifact states and the loader enforces

RunInput
  ├── parameters          validated against the artifact's declared schema
  ├── market records      canonical Bar/Quote/Tick, already normalized
  ├── run identity        opaque; carries no tenant, user or account identifier
  └── clock reading       supplied per event; the sandbox has no real clock
```

Explicitly **not** passed in: database handles, credentials, broker sessions,
the organization id, the user id, environment variables, network configuration,
or any object with a method that reaches outside the sandbox. A strategy that
can see which tenant it is running for can behave differently for one victim.

## Output contract

```
RunOutput
  ├── intents             a bounded list of (instrument, target, timestamp)
  ├── declared state      JSON-serializable, size-capped (AlphaLab's
  │                       StrategyStateProtocol shape)
  ├── log lines           bounded count, bounded length, treated as untrusted text
  └── failure             a typed reason, never a traceback from inside
```

Intents are **data, not orders**. They re-enter the normal pipeline and pass
through allocation, risk and the OMS exactly as an in-repository strategy's do.
This is the property that makes the blast radius finite even if the sandbox
leaks: a hostile strategy still cannot exceed the run's risk limits, because
those are enforced by AlphaLab after the intent is returned.

## Resource limits

| Limit | Value | Why |
|---|---|---|
| CPU | 1 core, hard-capped | a busy loop must not starve the host |
| CPU time | 5 s per event, 60 s per run | wall-clock alone is defeated by sleeping |
| Memory | 256 MB, hard | OOM must kill the sandbox, not the host |
| Wall clock | 10 s per event, 300 s per run | catches blocking that CPU time does not |
| Output size | 1 MB per run | prevents exhausting the caller's memory |
| Intents | 1000 per event | a strategy cannot flood the OMS |
| Artifact size | 10 MB uncompressed | with a decompression ratio cap |
| Processes | 1, no fork, no exec | no subprocess escape |
| File descriptors | stdin/stdout only | nothing to reach through |

Every limit must be enforced **by the isolation mechanism**, not by Python code
inside the sandbox. `resource.setrlimit` and `signal.alarm` are advisory against
code that can call them too.

## Isolation

| Dimension | Requirement |
|---|---|
| Filesystem | read-only root; no writable path; no `/proc`, `/sys`, `/dev` beyond null and urandom |
| Network | **none**. No loopback, no DNS, no unix sockets. A strategy that can reach the network can exfiltrate a dataset. |
| Process | separate PID namespace; no `fork`, `exec`, `ptrace` |
| User | unprivileged, non-root, no capabilities, `no_new_privs` |
| Kernel surface | seccomp allowlist; `gVisor` or a microVM preferred to a plain container |
| Host mounts | none |
| Secrets | the sandbox process's environment is empty |
| Tenancy | one sandbox per run, destroyed after; never reused across organizations |

Reuse across tenants is the subtle one: a sandbox that persists lets one run
leave state that a later run — belonging to someone else — can read.

## Package policy

- No network, so no installation at run time.
- A fixed, pre-built image with a pinned set of libraries (numpy, pandas, and
  AlphaLab's own pure-Python packages).
- Imports enforced by a loader allowlist, not by inspecting source: a regex over
  `import` statements is defeated by `__import__`, `importlib`, and attribute
  traversal from any reachable object.
- No `ctypes`, no `cffi`, no `subprocess`, no `socket`, no `os.system`.

## Artifact identity

```
artifact_id = SHA-256(normalized archive bytes)
```

Content-addressed, so the same code always has the same identity and a
`StrategyVersion` can pin it exactly. A run records the `artifact_id` beside the
`strategy_version_id` it already records, which keeps the existing
reproducibility guarantee unchanged in shape.

## Failure semantics

**Fail closed, always.** Every one of these terminates the run and records a
typed reason:

| Condition | Outcome |
|---|---|
| Any limit exceeded | run fails; partial output discarded |
| Sandbox exits non-zero | run fails |
| Output not parseable, or over size | run fails |
| An intent fails validation | run fails; no intent from that event is used |
| The sandbox cannot be created | run fails; **never** fall back to in-process |

That last row is the one that matters most. A fallback path that runs the
strategy in-process "just this once" because the sandbox was unavailable
defeats every other control in this document.

## What would have to be true before enabling this

1. A chosen isolation mechanism, running on the deployment's host.
2. Every limit above enforced and **tested by attempting to breach it** — a
   sandbox test suite that only runs benign code proves nothing.
3. An artifact store with content addressing and size limits.
4. A review process for what gets listed, since sandboxing bounds damage but
   does not make hostile code acceptable.
5. An incident path: how a malicious artifact is identified, revoked, and how
   affected buyers are told.

Until all five exist, `implementation_key` stays an indirection into this
repository, and that is the honest position.
