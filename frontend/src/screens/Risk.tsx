import Card from "../components/Card";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Risk() {
  const risk = usePolling(api.risk);
  const limits = usePolling(api.riskLimits);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card title="Daily PnL %">
          <span className={(risk?.daily_pnl_percent ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>
            {risk?.daily_pnl_percent?.toFixed(2) ?? "…"}%
          </span>
        </Card>
        <Card title="Consecutive Losses">{risk?.consecutive_losses ?? "…"}</Card>
        <Card title="Open Positions">{risk?.open_positions ?? "…"}</Card>
        <Card title="Blocked Operations">{risk?.blocked_operations ?? "…"}</Card>
      </div>
      <div>
        <h3 className="text-slate-400 text-sm uppercase mb-2">Configured Limits (docs/06)</h3>
        <table className="text-sm text-slate-300">
          <tbody>
            {limits && Object.entries(limits).map(([key, value]) => (
              <tr key={key} className="border-b border-slate-900">
                <td className="py-1 pr-8 font-mono text-slate-500">{key}</td>
                <td>{String(value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-slate-600">
        Phase 1: PAPER mode only. LIVE activation, real API keys and real orders are not available
        in this interface by design (docs/14, docs/16).
      </p>
    </div>
  );
}
