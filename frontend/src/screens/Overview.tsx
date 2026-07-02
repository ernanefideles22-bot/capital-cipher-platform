import Card from "../components/Card";
import StatusBadge from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Overview() {
  const status = usePolling(api.status);
  const risk = usePolling(api.risk);
  const perf = usePolling(api.paperPerformance);
  const agents = usePolling(api.agents);
  const decisions = usePolling(api.decisions);

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Card title="System Mode">{status ? <StatusBadge value={status.mode} /> : "…"}</Card>
      <Card title="Market Data">{status?.market_data ?? "…"}</Card>
      <Card title="Database">{status?.database ?? "…"}</Card>
      <Card title="Kill Switch">
        {risk?.kill_switch_active ? (
          <span className="text-red-400 font-bold">ACTIVE</span>
        ) : (
          <span className="text-emerald-400">inactive</span>
        )}
      </Card>
      <Card title="Active Agents">{agents ? agents.agents.filter((a) => a.enabled).length : "…"}</Card>
      <Card title="Decisions (recent)">{decisions ? decisions.decisions.length : "…"}</Card>
      <Card title="Paper PnL (net)">
        {perf ? (
          <span className={perf.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
            {perf.net_pnl.toFixed(2)} USDT
          </span>
        ) : ("…")}
      </Card>
      <Card title="Simulated Drawdown">{perf ? `${perf.max_drawdown_percent.toFixed(2)}%` : "…"}</Card>
      <Card title="Balance (simulated)">{perf ? perf.balance.toFixed(2) : "…"}</Card>
      <Card title="Win Rate">{perf ? `${perf.win_rate.toFixed(1)}%` : "…"}</Card>
      <Card title="Open Positions">{risk?.open_positions ?? "…"}</Card>
      <Card title="Blocked Operations">{risk?.blocked_operations ?? "…"}</Card>
    </div>
  );
}
