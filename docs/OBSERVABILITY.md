# Observability

**Status: IMPLEMENTED (correlation, structured logs). Metrics and tracing: NOT IMPLEMENTED.**

## The question this answers

"A user says their backtest produced the wrong number — show me everything that
happened." Answering it means joining an HTTP request to the service call it
made, to the job that queued, to the AlphaLab run that executed, to the row that
stored the result.

## Correlation

One identifier spans all of it:

```
X-Request-ID  ──►  CorrelationMiddleware      mints or adopts
                          │
                   ContextVar (per request)
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
   service call    BacktestJob.correlation_id   SessionRunner
                          │                       (adopts at launch)
                          ▼
                    worker thread
                   (adopts on claim)
                          │
                          ▼
                   AlphaLab run
                          │
                          ▼
                   BacktestRun row
```

An inbound `X-Request-ID` is **adopted, not replaced**, so a trace that began at
a load balancer stays one trace. It is sanitised first — caller-controlled text
that ends up in log lines — to alphanumerics, `-` and `_`, at most 64
characters. It is echoed on the response so a user reporting a problem can quote
it.

### Why a ContextVar, and where it stops

Threading an id through every signature is more explicit, and it is deliberately
not what this does: it would have to pass through `alphalab`, and this
application does not add parameters to the engine's API. A ContextVar stops at
that boundary.

The cost is that a background thread must adopt it explicitly. There are exactly
two such places, and both are commented:

| Where | How |
|---|---|
| Backtest worker | reads `BacktestJob.correlation_id`, persisted at submission |
| Session runner | captures `correlation_id()` in the request thread, adopts it in the runner's |

## Structured logs

JSON lines, because these are meant to be aggregated and a human-readable format
a shipper then re-parses loses the structure twice.

```json
{"ts":"2026-09-19T15:15:44+0530","level":"INFO",
 "logger":"iluvtrade.backtests.worker","message":"Executing backtest over 50 records",
 "correlation_id":"trace-abc-0001","job_id":"84db91fd-…",
 "organization_id":"7052b47e-…","dataset_version_id":"68dfee66-…"}
```

`ILUVTRADE_STRUCTURED_LOGGING=false` gives plain lines for a terminal.

## Secrets in logs

`SecretSafeFormatter` redacts credential-shaped values from the message, the
exception text and any `extra=` field. It covers `key=value`, `key: 'value'`,
`key: "value"`, quoted keys (`{'password': …}`), and `Bearer`/`Basic`/`token`
headers.

**It is a backstop, not the control.** The control is that credentials are
`SecretString` and never reach a log line at all. A formatter that was the only
defence would be one regex away from failing — which is exactly what happened
during development: the first version matched `access_token=` but not the far
commoner `token=`, so it did nothing for the usual case. Both directions are
now tested: ten credential shapes must be redacted, and four ordinary
operational messages must survive intact, because over-redaction destroys the
logs' usefulness and is its own failure.

## Job observability

| Fact | Where |
|---|---|
| Queue state | `BacktestJob.status` — QUEUED / RUNNING / COMPLETED / FAILED / CANCELLED |
| Attempts | `BacktestJob.attempts`, bounded by `backtest_max_attempts` |
| Structured failure | `error_code` and `error_message`, kept separate |
| Which request queued it | `BacktestJob.correlation_id` |
| What the engine refused | `risk_refusals` in the run artifact; `risk_rejected` session events |
| What a session did | `session_events`, per-run, sequenced |

## What the health endpoint reports

`GET /api/health` deliberately says more than "ok", because each of these is
otherwise something an operator has to assume:

- the **loaded AlphaLab version** — "which engine produced these numbers"
- whether live trading is possible at all
- whether rate limits are **shared across instances** (they are not)
- which notification channels are attached (none)
- which payment provider is configured

## Not implemented

| Gap | What it needs |
|---|---|
| Metrics | A Prometheus endpoint or a StatsD client. The natural attachment points are the middleware (request duration by route and status), the worker (queue depth, job duration, failure rate), and the runner (records per second, refusals). |
| Distributed tracing | OpenTelemetry spans. The correlation id is already the right join key. |
| Log aggregation | The JSON format is aggregator-ready; nothing ships them anywhere. |
| Alerting | Nothing watches anything. |
| Dashboards | Deliberately none — a dashboard that renders a metric nobody collects is decoration. |
