import { Link } from "react-router-dom";

import { Badge, Card, ErrorBanner, Loading, ModeBadge, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import { money } from "../lib/format";
import { useAsync } from "../lib/useAsync";

interface OrderRow {
  session_id: string;
  session_name: string;
  mode: string;
  sequence: number;
  engine_order_id: string;
  instrument: string;
  side: string;
  quantity: string;
  filled_quantity: string;
  average_fill_price: string | null;
  status: string;
  order_type: string;
}

export default function OrdersPage() {
  const orders = useAsync<OrderRow[]>(() => api.get<OrderRow[]>("/orders?limit=500"), []);

  if (orders.loading) return <Loading what="Loading orders" />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Orders</h1>
          <p>
            Every order across every session, newest first. Each carries AlphaLab's own order id, so
            a row here joins back to the engine's state.
          </p>
        </div>
      </div>

      <ErrorBanner error={orders.error} />

      <Card>
        {(orders.data ?? []).length === 0 ? (
          <p className="dim" style={{ margin: 0 }}>No orders yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Session</th><th>Mode</th><th>Instrument</th><th>Side</th>
                  <th className="num">Quantity</th><th className="num">Filled</th>
                  <th className="num">Avg price</th><th>Status</th><th>Engine id</th>
                </tr>
              </thead>
              <tbody>
                {(orders.data ?? []).map((order) => (
                  <tr key={`${order.session_id}-${order.sequence}`}>
                    <td><Link to={`/sessions/${order.session_id}`}>{order.session_name}</Link></td>
                    <td><ModeBadge mode={order.mode} /></td>
                    <td className="mono">{order.instrument}</td>
                    <td><Badge tone={order.side === "buy" ? "accent" : "neutral"}>{order.side}</Badge></td>
                    <td className="num">{order.quantity}</td>
                    <td className="num">{order.filled_quantity}</td>
                    <td className="num">{money(order.average_fill_price)}</td>
                    <td><StatusBadge status={order.status} /></td>
                    <td className="mono tiny faint">{order.engine_order_id.slice(0, 8)}</td>
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
