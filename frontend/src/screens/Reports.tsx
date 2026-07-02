import { ColorType, createChart } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { AgentRankingRow, PerformanceReport } from "../types";

export default function Reports() {
  const [by, setBy] = useState<"symbol" | "timeframe">("symbol");
  const [report, setReport] = useState<PerformanceReport | null>(null);
  const [ranking, setRanking] = useState<AgentRankingRow[]>([]);
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [perf, rank] = await Promise.all([api.performanceReport(by), api.agentRanking()]);
        if (!active) return;
        setReport(perf);
        setRanking(rank.ranking);
      } catch { /* empty state */ }
    };
    load();
    const id = setInterval(load, 8000);
    return () => { active = false; clearInterval(id); };
  }, [by]);

  useEffect(() => {
    if (!chartRef.current || !report || report.equity_curve.length < 2) return;
    const chart = createChart(chartRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      height: 220,
    });
    const series = chart.addLineSeries({ color: "#38bdf8" });
    series.setData(
      report.equity_curve.map((p, i) => ({
        time: (Math.floor(new Date(p.timestamp).getTime() / 1000) + i) as never,
        value: p.balance,
      }))
    );
    return () => chart.remove();
  }, [report]);

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-slate-400 text-sm uppercase mb-2">Simulated equity curve</h3>
        {report && report.equity_curve.length < 2 ? (
          <div className="text-slate-600 text-sm">No closed trades yet.</div>
        ) : (
          <div ref={chartRef} className="rounded border border-slate-800 overflow-hidden" />
        )}
      </div>
      <div>
        <div className="flex items-center gap-3 mb-2">
          <h3 className="text-slate-400 text-sm uppercase">Performance by</h3>
          <select value={by} onChange={(e) => setBy(e.target.value as "symbol" | "timeframe")}
            className="bg-slate-800 text-slate-200 rounded px-2 py-1 text-xs border border-slate-700">
            <option value="symbol">symbol</option>
            <option value="timeframe">timeframe</option>
          </select>
        </div>
        <table className="w-full text-sm text-slate-300">
          <thead>
            <tr className="text-left text-slate-500 border-b border-slate-800">
              <th className="py-1">{by}</th><th>Trades</th><th>Win%</th><th>Net PnL</th><th>PF</th>
            </tr>
          </thead>
          <tbody>
            {report?.breakdown.map((row) => (
              <tr key={row.key} className="border-b border-slate-900">
                <td className="py-1 font-mono">{row.key}</td>
                <td>{row.trades}</td>
                <td>{row.win_rate}%</td>
                <td className={row.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{row.net_pnl}</td>
                <td>{row.profit_factor ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div>
        <h3 className="text-slate-400 text-sm uppercase mb-2">
          Agent ranking <span className="text-slate-600 normal-case">(report only — docs/27)</span>
        </h3>
        <table className="w-full text-sm text-slate-300">
          <thead>
            <tr className="text-left text-slate-500 border-b border-slate-800">
              <th className="py-1">Agent</th><th>Reliability</th><th>Dir. accuracy</th>
              <th>Avg conf.</th><th>Overconf. losses</th><th>Sample</th><th>Score</th>
            </tr>
          </thead>
          <tbody>
            {ranking.map((row) => (
              <tr key={row.agent_name} className="border-b border-slate-900">
                <td className="py-1 font-mono">{row.agent_name}</td>
                <td>{row.reliability_score}%</td>
                <td>{row.directional_accuracy != null ? `${row.directional_accuracy}%` : "—"}</td>
                <td>{row.avg_confidence ?? "—"}</td>
                <td className={row.overconfident_losses > 0 ? "text-amber-400" : ""}>
                  {row.overconfident_losses}
                </td>
                <td className={row.sample_sufficient ? "text-emerald-400" : "text-slate-500"}>
                  {row.evaluated_decisions}{row.sample_sufficient ? "" : " (insufficient)"}
                </td>
                <td>{row.score ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
