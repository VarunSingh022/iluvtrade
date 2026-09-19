import { useState } from "react";

import { Badge, Banner, Card, ErrorBanner, Loading, Stat, StatusBadge, Tabs } from "../components/ui";
import { ApiError, api } from "../lib/api";
import type { BacktestJob, Entitlement, Listing, Strategy } from "../lib/api";
import { count, money, percent, ratio, shortId, when } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export default function RedDeskPage() {
  const [tab, setTab] = useState("discover");
  const discover = useAsync<Listing[]>(() => api.get<Listing[]>("/reddesk/discover"), []);
  const mine = useAsync<Listing[]>(() => api.get<Listing[]>("/reddesk/my-listings"), []);
  const entitlements = useAsync<Entitlement[]>(() => api.get<Entitlement[]>("/reddesk/entitlements"), []);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  async function buy(listing: Listing) {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const result = await api.post<{ entitlement: Entitlement }>("/reddesk/purchases", {
        listing_id: listing.id,
        idempotency_key: `buy-${listing.id}`,
      });
      setNotice(
        `Acquired "${listing.title}". Your entitlement grants strategy version ` +
          `${shortId(result.entitlement.granted_strategy_version_id)} under a ` +
          `${result.entitlement.version_access_policy} licence.`,
      );
      entitlements.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>RedDesk</h1>
          <p>
            The strategy marketplace. A purchase grants a licence to run a specific strategy
            version — not ownership of its source, and not whichever version the creator publishes
            next.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}

      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "discover", label: "Discover" },
          { id: "entitlements", label: `What you can run (${(entitlements.data ?? []).length})` },
          { id: "sell", label: `Your listings (${(mine.data ?? []).length})` },
        ]}
      />

      {tab === "discover" && (
        <>
          <ErrorBanner error={discover.error} />
          {discover.loading ? (
            <Loading />
          ) : (discover.data ?? []).length === 0 ? (
            <Card><p className="dim" style={{ margin: 0 }}>Nothing published yet.</p></Card>
          ) : (
            <div className="grid cols-2">
              {(discover.data ?? []).map((listing) => (
                <Card
                  key={listing.id}
                  title={listing.title}
                  actions={
                    <span className="mono small">
                      {listing.price.currency} {money(listing.price.amount)}
                      <span className="faint"> / {listing.price.cadence.replace("_", " ")}</span>
                    </span>
                  }
                >
                  <p className="small">{listing.summary}</p>

                  <div className="row" style={{ marginBottom: "0.5rem" }}>
                    <Badge tone="neutral">{listing.version_access_policy} licence</Badge>
                    {listing.rating.average !== null && (
                      <Badge tone="accent">★ {listing.rating.average} ({listing.rating.count})</Badge>
                    )}
                    {listing.supported_brokers.map((broker) => (
                      <Badge tone="neutral" key={broker}>{broker}</Badge>
                    ))}
                  </div>

                  {listing.evidence ? (
                    <div className="card" style={{ background: "var(--surface-2)", padding: "0.6rem" }}>
                      <div className="stat-label" style={{ marginBottom: "0.35rem" }}>Backtest evidence</div>
                      <div className="grid cols-4">
                        <div><div className="tiny faint">Return</div><div className="mono small">{percent(listing.evidence.total_return)}</div></div>
                        <div><div className="tiny faint">Sharpe</div><div className="mono small">{ratio(listing.evidence.sharpe_ratio)}</div></div>
                        <div><div className="tiny faint">Max DD</div><div className="mono small">{percent(listing.evidence.max_drawdown)}</div></div>
                        <div><div className="tiny faint">Orders</div><div className="mono small">{count(listing.evidence.order_count)}</div></div>
                      </div>
                      <div className="tiny faint" style={{ marginTop: "0.4rem" }}>
                        {listing.evidence.disclaimer}
                      </div>
                    </div>
                  ) : (
                    <Banner tone="warn">This listing cites no backtest run.</Banner>
                  )}

                  <details style={{ marginTop: "0.6rem" }}>
                    <summary className="small" style={{ cursor: "pointer" }}>Methodology, risk and licence</summary>
                    <div className="small" style={{ marginTop: "0.4rem" }}>
                      <p><strong>Methodology.</strong> {listing.methodology}</p>
                      <p><strong>Risk.</strong> {listing.risk_disclosure}</p>
                      <p><strong>Licence.</strong> {listing.licence_terms}</p>
                    </div>
                  </details>

                  <div className="row end" style={{ marginTop: "0.6rem" }}>
                    <button className="primary" disabled={busy} onClick={() => void buy(listing)} type="button">
                      Acquire
                    </button>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </>
      )}

      {tab === "entitlements" && (
        <Card>
          <p className="small dim">
            Every deployment path asks this list before running a strategy your workspace does not
            own, and it answers with one concrete version — never "the latest".
          </p>
          {(entitlements.data ?? []).length === 0 ? (
            <p className="dim" style={{ margin: 0 }}>You hold no entitlements.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Strategy version granted</th><th>Licence</th><th>Status</th><th>From</th><th>Until</th></tr>
                </thead>
                <tbody>
                  {(entitlements.data ?? []).map((row) => (
                    <tr key={row.id}>
                      <td className="mono tiny">{row.granted_strategy_version_id}</td>
                      <td><Badge tone="neutral">{row.version_access_policy}</Badge></td>
                      <td><StatusBadge status={row.status} /></td>
                      <td className="tiny faint">{when(row.valid_from)}</td>
                      <td className="tiny faint">{row.valid_until ? when(row.valid_until) : "perpetual"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "sell" && <SellTab listings={mine} />}
    </>
  );
}

function SellTab({ listings }: { listings: ReturnType<typeof useAsync<Listing[]>> }) {
  const strategies = useAsync<Strategy[]>(() => api.get<Strategy[]>("/strategies"), []);
  const jobs = useAsync<BacktestJob[]>(() => api.get<BacktestJob[]>("/backtests?status=completed"), []);
  const [form, setForm] = useState({
    strategy_id: "",
    title: "",
    summary: "",
    description: "",
    methodology: "",
    risk_disclosure: "",
    price_amount: "0",
    licence_terms: "",
  });
  const [versionId, setVersionId] = useState("");
  const [evidenceJob, setEvidenceJob] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [notice, setNotice] = useState<string | undefined>();

  const published = (strategies.data ?? []).flatMap((strategy) =>
    strategy.versions.filter((version) => version.status === "published").map((version) => ({ strategy, version })),
  );

  async function createListing() {
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const listing = await api.post<Listing>("/reddesk/listings", {
        ...form,
        price_currency: "INR",
        billing_cadence: "monthly",
        version_access_policy: "pinned",
        supported_brokers: ["paper"],
        supported_instruments: [],
        supported_data: [],
      });
      if (versionId) {
        let runId: string | null = null;
        if (evidenceJob) {
          const result = await api.get<{ run_id: string }>(`/backtests/${evidenceJob}/result`);
          runId = result.run_id;
        }
        await api.post(`/reddesk/listings/${listing.id}/versions`, {
          strategy_version_id: versionId,
          evidence_backtest_run_id: runId,
          release_notes: "",
          make_current: true,
        });
      }
      setNotice("Listing created as a draft. Submit it for review when it validates.");
      listings.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function advance(listing: Listing, action: "submit" | "review" | "publish") {
    setBusy(true);
    setError(undefined);
    try {
      await api.post(
        `/reddesk/listings/${listing.id}/${action}`,
        action === "review" ? { approve: true, notes: "Approved." } : undefined,
      );
      listings.reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error && <Banner tone="err">{error}</Banner>}
      {notice && <Banner tone="ok">{notice}</Banner>}

      <Card title="List a strategy">
        <div className="grid cols-2">
          <div>
            <div className="field">
              <label>Strategy version to sell</label>
              <select
                value={versionId}
                onChange={(event) => {
                  setVersionId(event.target.value);
                  const found = published.find((entry) => entry.version.id === event.target.value);
                  if (found) setForm((f) => ({ ...f, strategy_id: found.strategy.id, title: f.title || found.strategy.name }));
                }}
              >
                <option value="">Choose a published version…</option>
                {published.map(({ strategy, version }) => (
                  <option key={version.id} value={version.id}>{strategy.name} · v{version.version}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Backtest evidence</label>
              <select value={evidenceJob} onChange={(event) => setEvidenceJob(event.target.value)}>
                <option value="">No evidence</option>
                {(jobs.data ?? []).filter((job) => job.strategy_version_id === versionId).map((job) => (
                  <option key={job.id} value={job.id}>{shortId(job.id)} · {when(job.finished_at)}</option>
                ))}
              </select>
              <div className="tiny faint" style={{ marginTop: 2 }}>
                Only a run against this exact version can be cited.
              </div>
            </div>
            <div className="field"><label>Title</label><input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></div>
            <div className="field"><label>Summary</label><input value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} /></div>
            <div className="field"><label>Monthly price (INR)</label><input value={form.price_amount} onChange={(e) => setForm({ ...form, price_amount: e.target.value })} /></div>
          </div>
          <div>
            <div className="field"><label>Description</label><textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="At least 40 characters." /></div>
            <div className="field"><label>Methodology</label><textarea value={form.methodology} onChange={(e) => setForm({ ...form, methodology: e.target.value })} /></div>
            <div className="field"><label>Risk disclosure</label><textarea value={form.risk_disclosure} onChange={(e) => setForm({ ...form, risk_disclosure: e.target.value })} /></div>
            <div className="field"><label>Licence terms</label><textarea value={form.licence_terms} onChange={(e) => setForm({ ...form, licence_terms: e.target.value })} /></div>
          </div>
        </div>
        <button className="primary" disabled={busy || !versionId} onClick={() => void createListing()} type="button">
          Create draft listing
        </button>
      </Card>

      {(listings.data ?? []).map((listing) => (
        <Card key={listing.id} title={listing.title} actions={<StatusBadge status={listing.status} />}>
          <div className="grid cols-3">
            <Stat label="Price" value={`${listing.price.currency} ${money(listing.price.amount)}`} sub={listing.price.cadence} />
            <Stat label="Versions offered" value={listing.version_history.length} sub={listing.evidence ? "evidence attached" : "no evidence"} />
            <Stat label="Rating" value={listing.rating.average ?? "—"} sub={`${listing.rating.count} review(s)`} />
          </div>
          <div className="row end" style={{ marginTop: "0.6rem" }}>
            {listing.status === "draft" && <button disabled={busy} onClick={() => void advance(listing, "submit")} type="button">Submit for review</button>}
            {listing.status === "submitted" && <button disabled={busy} onClick={() => void advance(listing, "review")} type="button">Approve</button>}
            {listing.status === "approved" && <button className="primary" disabled={busy} onClick={() => void advance(listing, "publish")} type="button">Publish</button>}
          </div>
        </Card>
      ))}
    </>
  );
}
