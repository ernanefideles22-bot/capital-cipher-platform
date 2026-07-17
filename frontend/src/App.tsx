import { useState } from "react";
import KillSwitchButton from "./components/KillSwitchButton";
import StatusBadge from "./components/StatusBadge";
import { usePolling } from "./hooks/usePolling";
import { api } from "./services/api";
import Agents from "./screens/Agents";
import Audit from "./screens/Audit";
import Decisions from "./screens/Decisions";
import Market from "./screens/Market";
import Overview from "./screens/Overview";
import Paper from "./screens/Paper";
import Risk from "./screens/Risk";
import Backtest from "./screens/Backtest";
import Reports from "./screens/Reports";

const TABS = ["Overview", "Market", "Agents", "Decisions", "Risk", "Paper", "Backtest", "Reports", "Audit"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [tab, setTab] = useState<Tab>("Overview");
  const status = usePolling(api.status, 5000);

  return (
    <div className="min-h-screen text-slate-100">
      <header className="sticky top-0 z-10 border-b border-slate-800/90 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-screen-2xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_14px_rgba(52,211,153,0.9)]" />
              <span className="truncate text-sm font-bold tracking-[0.12em] text-slate-100 sm:text-base">CAPITAL CIPHER</span>
              {status && <StatusBadge value={status.mode} />}
            </div>
            <p className="mt-1 truncate text-xs text-slate-500">
              Paper trading control room · market: {status?.market_data ?? "…"} · database: {status?.database ?? "…"}
            </p>
          </div>
          <div className="shrink-0">
            <KillSwitchButton active={status?.kill_switch_active ?? false} />
          </div>
        </div>
      </header>
      <div className="mx-auto max-w-screen-2xl px-4 pb-8 sm:px-6">
        <div className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-cyan-900/60 bg-cyan-950/25 px-3 py-2 text-xs text-cyan-100">
          <span><span className="font-semibold">PAPER ONLY</span> · no live execution or private exchange keys are available in this interface.</span>
          <span className="hidden shrink-0 text-cyan-300 sm:inline">Audited workflow</span>
        </div>
        <nav aria-label="Platform sections" className="mt-4 flex gap-1 overflow-x-auto border-b border-slate-800 pb-px">
          {TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)} aria-current={tab === t ? "page" : undefined}
              className={`shrink-0 rounded-t-lg px-3 py-2 text-sm font-medium transition-colors ${
                tab === t
                  ? "border border-b-slate-950 border-slate-800 bg-slate-950 text-slate-100"
                  : "text-slate-500 hover:bg-slate-900/70 hover:text-slate-300"}`}>
              {t}
            </button>
          ))}
        </nav>
        <main className="py-6">
          {tab === "Overview" && <Overview />}
          {tab === "Market" && <Market />}
          {tab === "Agents" && <Agents />}
          {tab === "Decisions" && <Decisions />}
          {tab === "Risk" && <Risk />}
          {tab === "Paper" && <Paper />}
          {tab === "Backtest" && <Backtest />}
          {tab === "Reports" && <Reports />}
          {tab === "Audit" && <Audit />}
        </main>
      </div>
    </div>
  );
}
