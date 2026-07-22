import StatusBadge from "../components/StatusBadge";
import MetricCard from "../components/MetricCard";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Overview() {
  const status = usePolling(api.status);
  const risk = usePolling(api.risk);
  const perf = usePolling(api.paperPerformance);
  const agents = usePolling(api.agents);
  const decisions = usePolling(api.decisions);
  const activeAgents = agents?.agents.filter((agent) => agent.enabled).length;
  const pnlTone = !perf ? "neutral" : perf.net_pnl < 0 ? "negative" : "positive";
  const drawdownTone = perf && perf.max_drawdown_percent > 0 ? "warning" : "neutral";

  return (
    <div className="space-y-7">
      <section className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">Operations overview</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-white">Paper-trading control room</h1>
          <p className="mt-1 text-sm text-slate-500">Live operational state from the platform APIs. Values are never fabricated in the interface.</p>
        </div>
        <p className="text-xs text-slate-500">Refreshes automatically</p>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Platform health</h2>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="System mode" value={status ? <StatusBadge value={status.mode} /> : "…"} detail="Execution environment" />
          <MetricCard label="Market data" value={status?.market_data ?? "…"} detail="Public data adapter" />
          <MetricCard label="Database" value={status?.database ?? "…"} detail="Persistence health" />
          <MetricCard
            label="Kill switch"
            value={risk?.kill_switch_active ? <span className="text-red-400">ACTIVE</span> : <span className="text-emerald-400">Clear</span>}
            detail={risk?.kill_switch_active ? risk.kill_switch_reason ?? "No reason supplied" : "Risk controls allow paper operations"}
            tone={risk?.kill_switch_active ? "negative" : "positive"}
          />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Research and risk</h2>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="Active agents" value={activeAgents ?? "…"} detail="Enabled analytical agents" />
          <MetricCard label="Recent decisions" value={decisions?.decisions.length ?? "…"} detail="Latest evaluated candidates" />
          <MetricCard label="Open positions" value={risk?.open_positions ?? "…"} detail="Paper positions under risk control" />
          <MetricCard label="Blocked operations" value={risk?.blocked_operations ?? "…"} detail="Risk policy interventions" tone={(risk?.blocked_operations ?? 0) > 0 ? "warning" : "neutral"} />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Paper performance</h2>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard label="Net PnL" value={perf ? <span className={perf.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{perf.net_pnl.toFixed(2)} USDT</span> : "…"} detail="Net of estimated fees" tone={pnlTone} />
          <MetricCard label="Simulated drawdown" value={perf ? `${perf.max_drawdown_percent.toFixed(2)}%` : "…"} detail="Maximum observed drawdown" tone={drawdownTone} />
          <MetricCard label="Paper balance" value={perf ? `${perf.balance.toFixed(2)} USDT` : "…"} detail={perf ? `Initial: ${perf.initial_balance.toFixed(2)} USDT` : "Awaiting performance data"} />
          <MetricCard label="Win rate" value={perf ? `${perf.win_rate.toFixed(1)}%` : "…"} detail={perf ? `${perf.closed_trades} closed paper trades` : "Awaiting closed trades"} />
        </div>
      </section>
    </div>
  );
}
