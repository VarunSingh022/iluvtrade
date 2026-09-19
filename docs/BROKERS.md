# Brokers

## The boundary

AlphaLab owns the venue contract: `alphalab.broker.protocol.BrokerProtocol`.
Every method takes a `BrokerState` and returns the next one plus the events it
produced, so one adapter can be driven by a backtest, a paper run and a live
session without behaving differently.

iluvtrade owns the other half — whose account it is, where the credentials live,
what state the connection is in, and who authorized it. That is
`backend/iluvtrade/brokers/`.

```
BrokerAccount        (org, venue, label, venue account id)
      │
BrokerConnection     (state, AES-GCM credential blob, token expiry)
      │
ZerodhaAdapter       implements alphalab.broker.BrokerProtocol
      │
ZerodhaClient        the only thing that touches a socket
      │
   Kite Connect v3
```

Splitting the adapter from the client is deliberate: the adapter is pure and
testable without a network, and the client is testable against a local server.

## Zerodha (Kite Connect v3)

### Contracts

Taken from <https://kite.trade/docs/connect/v3/> — the user, orders and
portfolio sections — and not invented:

| Operation | Contract |
|---|---|
| Login | `https://kite.zerodha.com/connect/login?v=3&api_key=…` |
| Session exchange | `POST /session/token` with `api_key`, `request_token`, `checksum` |
| Checksum | `SHA-256(api_key + request_token + api_secret)` |
| Auth header | `Authorization: token <api_key>:<access_token>` |
| Version header | `X-Kite-Version: 3` |
| Place order | `POST /orders/{variety}` |
| Modify / cancel | `PUT` / `DELETE /orders/{variety}/{order_id}` |
| Orders, trades | `GET /orders`, `GET /trades` |
| Positions, holdings | `GET /portfolio/positions`, `GET /portfolio/holdings` |
| Profile, margins | `GET /user/profile`, `GET /user/margins` |
| Logout | `DELETE /session/token` |

Enumerations are the documented ones: varieties `regular|amo|co|iceberg|auction`,
order types `MARKET|LIMIT|SL|SL-M`, products `CNC|NRML|MIS|MTF`, validity
`DAY|IOC|TTL`.

### The authorization flow

1. The user's browser goes to the Kite login URL. The API **key** is public and
   belongs there; the **secret** never leaves the server.
2. Kite redirects to the registered redirect URL with a short-lived,
   single-use `request_token` — which is why it is safe in a query string and
   an access token never would be.
3. The server exchanges it at `POST /session/token`, signing with the secret.
4. The access token is encrypted with AES-GCM bound to the connection id and
   stored. It is never returned to the browser.

### Token expiry is not a choice

Zerodha invalidates access tokens at **06:00 IST daily** — a regulatory
requirement. `zerodha.token_expiry()` computes the next occurrence of that
instant, so a token issued at 05:00 IST correctly expires at 06:00 the *same*
morning rather than "tomorrow". The UI shows when a connection will need
reauthorizing instead of discovering it at the first rejected order.

### Status mapping

Kite's statuses map to `alphalab.core.enums.OrderStatus`, not to
`BrokerOrderStatus` — the two are separate vocabularies, and the latter has only
three members describing our side of the wire. Interim states are mapped
explicitly rather than defaulted: treating `VALIDATION PENDING` as accepted
would report an order as working that the venue has not taken. An unrecognised
status becomes `PENDING`, because if Kite adds a state this table does not know,
"not yet working" is the safe reading and "working" is the one that loses money.

### Idempotency

`tag` carries the OMS order id (Kite allows 20 characters), so a response lost
in transit can be reconciled against `GET /orders` rather than resubmitted into
a duplicate.

`apply_execution` is idempotent in `execution_id`, which is load-bearing rather
than theoretical here: `GET /trades` returns the whole day's trades on every
poll, so without the guard each fill would be counted once per cycle.

### What is honest about it

**This connector has never executed against Zerodha.** It is implemented to the
published contracts and tested against a local HTTP server speaking the same
protocol — the session exchange, order placement with correct parameters,
account, positions, order status and idempotent execution all pass. But a
protocol-faithful local server is not a venue.

`VERIFIED_AGAINST_LIVE_VENUE = False` is exported, surfaced by
`GET /api/v1/brokers/supported`, and shown in the UI as "not verified against
the live venue". Reaching verification needs:

- a Kite Connect subscription (a paid, contracted relationship with Zerodha)
- a registered app with a redirect URL Zerodha has approved
- a funded trading account
- a session run against the real venue during market hours

None of these can be conjured by writing more code. They are external
dependencies, recorded as such.

### Also not implemented

- **Postback webhook.** Kite can push order updates to a publicly reachable URL.
  This deployment does not assume one exists, so the connector polls
  `GET /trades` instead. Polling is slower and rate-limited; the webhook is the
  right answer once there is a public endpoint to verify signatures against.
- **Market-data websocket.** Kite's websocket carries quotes, not order updates
  for arbitrary apps. A live feed adapter implementing
  `alphalab.market.source.MarketDataSource` is separate work.
- **Brokerage in fills.** Kite does not itemise charges on a trade; they arrive
  in the contract note. The connector reports commission as zero on a venue fill
  rather than inventing an estimate AlphaLab's accounting would then treat as
  real. Reconciling against the contract note is unbuilt.

## Adding another broker

1. Implement `BrokerProtocol` in a new module under `brokers/`, importing the
   vocabulary from `alphalab_bridge.broker` rather than from `alphalab`.
2. Add a member to `BrokerKind`.
3. Add the connection flow to `brokers/service.py`.
4. Add an entry to `GET /brokers/supported` stating honestly whether it has been
   run against the venue.

No trading logic changes. That is the point of the boundary being AlphaLab's
rather than ours.
