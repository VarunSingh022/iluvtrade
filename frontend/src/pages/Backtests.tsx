import { Link } from "react-router-dom";

import { Card, ErrorBanner, Loading, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import type { BacktestJob } from "../lib/api";
import { shortId, when } from "../lib/format";
import { useAsync, usePolling } from "../lib/useAsync";

export default function BacktestsPage() {
  const jobs = useAsync<BacktestJob[]>(() => api.get<BacktestJob[]>("/backtests?limit=100"), []);
  const busy = (jobs.data ?? []).some((job) => job.status === "queued" || job.status === "running");
  usePolling(jobs.reload, busy);

  if (jobs.loading) return <Loading what="Loading backtests" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Backtests</h1>
          <p>Runs execute in the background. A failed run says why.</p>
        </div>
        <Link className="btn primary" to="/research">New backtest</Link>
      </div>

      <ErrorBanner error={jobs.error} />

      <Card>
        {(jobs.data ?? []).length === 0 ? (
          <p className="dim" style={{ margin: 0 }}>No backtests yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Queued</th>
                  <th>Finished</th>
                  <th>Failure</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(jobs.data ?? []).map((job) => (
                  <tr key={job.id}>
                    <td className="mono tiny">{shortId(job.id)}</td>
                    <td><StatusBadge status={job.status} /></td>
                    <td className="tiny faint">{when(job.queued_at)}</td>
                    <td className="tiny faint">{when(job.finished_at)}</td>
                    <td className="tiny">
                      {job.error_code ? (
                        <span className="neg">{job.error_code}: {job.error_message?.slice(0, 80)}</span>
                      ) : (
                        <span className="faint">—</span>
                      )}
                    </td>
                    <td><Link className="tiny" to={`/backtests/${job.id}`}>Open</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
