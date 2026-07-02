import Card from "../components/Card";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Paper() {
  const perf = usePolling(api.paperPerformance);
  const orders = usePolling(api.paperOrders, 4000);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Card title="Closed Trades">{perf?.closed_trades ?? "…"}</Card>
        <Card title="Win Rate">{perf ? `${perf.win_rate.toFixed(1)}%` : "…"}</Card>
        <Card title="Net PnL">
          {perf ? (
            <span className={perf.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
              {perf.net_pnl.toFixed(2)}
            </span>
          ) : "…"}
        </Card>
        <Card title="Fees (est.)">{perf?.fees_total?.toFixed(2) ?? "…"}</Card>
        <Card title="Max Drawdown">{perf ? `${perf.max_drawdown_percent.toFixed(2)}%` : "…"}</Card>
      </div>
      <table className="w-full text-sm text-slate-300">
        <thead>
          <tr className="text-left text-slate-500 border-b border-slate-800">
            <th className="py-2">Order</th><th>Symbol</th><th>Side</th><th>Entry</th>
            <th>Stop</th><th>Take</th><th>Status</th><th>PnL</th>
          </tr>
        </thead>
        <tbody>
          {orders?.orders.map((o) => (
            <tr key={o.paper_order_id} className="border-b border-slate-900">
              <td className="py-2 font-mono text-xs">{o.paper_order_id.slice(0, 8)}…</td>
              <td>{o.symbol}</td>
              <td className={o.side === "BUY" ? "text-emerald-400" : "text-red-400"}>{o.side}</td>
              <td>{o.entry_price}</td>
              <td>{o.stop_loss ?? "—"}</td>
              <td>{o.take_profit ?? "—"}</td>
              <td>{o.status}</td>
              <td className={(o.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>
                {o.pnl?.toFixed(2) ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
