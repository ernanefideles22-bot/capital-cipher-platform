import { useState } from "react";
import { api } from "../services/api";
import type { BacktestReport } from "../types";
import { usePolling } from "../hooks/usePolling";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const TIMEFRAMES = ["15m", "1h", "4h"];

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded p-3">
      <div className="text-xs text-slate-500 uppercase">{label}</div>
      <div className="text-slate-100 font-mono">{value}</div>
    </div>
  );
}

export default function Backtest() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("15m");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<BacktestReport | null>(null);
  const history = usePolling(api.backtestReports, 10000);

  const run = async () => {
    setRunning(true);
    setError(null);
    const result = await api.runBacktest({ symbol, timeframe, source: "store" });
    setRunning(false);
    if (!result.success || !result.data) {
      setError(result.error?.message ?? "Backtest failed");
      return;
    }
    setReport(result.data.report);
  };

  return (
    <div className="space-y-4">
      <div className="flex gap-2 items-center">
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
          className="bg-slate-800 text-slate-200 rounded px-2 py-1 text-sm border border-slate-700">
          {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
          className="bg-slate-800 text-slate-200 rounded px-2 py-1 text-sm border border-slate-700">
          {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
        </select>
        <button onClick={run} disabled={running}
          className="px-4 py-1.5 bg-sky-800 hover:bg-sky-700 rounded text-sm font-bold disabled:opacity-50">
          {running ? "Running…" : "Run backtest (stored candles)"}
        </button>
      </div>
      {error && <div className="text-red-400 text-sm">{error}</div>}
      {report && (
        <div className="grid grid-cols-3 md:grid-cols-5 gap-2">
          <Metric label="Trades" value={report.total_trades} />
          <Metric label="Win Rate" value={`${report.win_rate}%`} />
          <Metric label="Profit Factor" value={report.profit_factor ?? "—"} />
          <Metric label="Expectancy" value={report.expectancy} />
          <Metric label="Max DD" value={`${report.max_drawdown}%`} />
          <Metric label="Net PnL" value={report.net_pnl} />
          <Metric label="Net PnL %" value={`${report.net_pnl_percent}%`} />
          <Metric label="Fees" value={report.fees} />
          <Metric label="Slippage" value={report.slippage} />
          <Metric label="Blocked by risk" value={report.blocked_by_risk} />
        </div>
      )}
      <p className="text-xs text-slate-600">
        A positive backtest does not authorize live trading — it only authorizes further
        paper-trading investigation (docs/17).
      </p>
      <div>
        <h3 className="text-slate-400 text-sm uppercase mb-2">Previous runs</h3>
        <table className="w-full text-sm text-slate-300">
          <thead>
            <tr className="text-left text-slate-500 border-b border-slate-800">
              <th className="py-1">Symbol</th><th>TF</th><th>Period</th><th>Trades</th>
              <th>Win%</th><th>PF</th><th>Net PnL</th><th>Max DD</th>
            </tr>
          </thead>
          <tbody>
            {history?.reports.map((r) => (
              <tr key={r.backtest_id} className="border-b border-slate-900">
                <td className="py-1">{r.symbol}</td>
                <td>{r.timeframe}</td>
                <td className="text-xs">{r.start_date} → {r.end_date}</td>
                <td>{r.total_trades}</td>
                <td>{r.win_rate}%</td>
                <td>{r.profit_factor ?? "—"}</td>
                <td className={r.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{r.net_pnl}</td>
                <td>{r.max_drawdown}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
