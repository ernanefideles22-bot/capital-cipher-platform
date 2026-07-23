import { useEffect, useState } from "react";
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

const NAV_GROUPS: { label: string; tabs: Tab[] }[] = [
  { label: "Control room", tabs: ["Overview", "Market"] },
  { label: "Research", tabs: ["Agents", "Decisions", "Backtest", "Reports"] },
  { label: "Controls", tabs: ["Risk", "Paper"] },
  { label: "Records", tabs: ["Audit"] },
];

const TAB_META: Record<Tab, { short: string; description: string }> = {
  Overview: { short: "OV", description: "System health and paper performance" },
  Market: { short: "MK", description: "Public market data" },
  Agents: { short: "AG", description: "Agent health and throughput" },
  Decisions: { short: "DC", description: "Decision and audit chains" },
  Risk: { short: "RK", description: "Central risk controls" },
  Paper: { short: "PP", description: "Simulated orders and PnL" },
  Backtest: { short: "BT", description: "Stored-candle experiments" },
  Reports: { short: "RP", description: "Performance attribution" },
  Audit: { short: "AU", description: "Immutable activity record" },
};

function formatRefresh(value: Date | null): string {
  if (!value) return "waiting for telemetry";
  return value.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function App() {
  const [tab, setTab] = useState<Tab>("Overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const status = usePolling(api.status, 5000);

  useEffect(() => {
    if (status) setLastRefresh(new Date());
  }, [status]);

  const selectTab = (nextTab: Tab) => {
    setTab(nextTab);
    setSidebarOpen(false);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {sidebarOpen && (
        <button aria-label="Close navigation" onClick={() => setSidebarOpen(false)} className="fixed inset-0 z-20 bg-slate-950/70 lg:hidden" />
      )}
      <aside className={`fixed inset-y-0 left-0 z-30 flex w-64 flex-col border-r border-slate-800/90 bg-slate-950/95 px-4 py-5 shadow-2xl backdrop-blur transition-transform duration-200 lg:translate-x-0 ${sidebarOpen ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center gap-3 px-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-cyan-700/60 bg-cyan-950/60 text-xs font-black tracking-tight text-cyan-300">CC</span>
          <div className="min-w-0">
            <div className="truncate text-sm font-bold tracking-[0.16em] text-slate-100">CAPITAL CIPHER</div>
            <div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-slate-500">Research platform</div>
          </div>
        </div>
        <div className="mt-7 rounded-xl border border-emerald-900/70 bg-emerald-950/20 px-3 py-3">
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-emerald-300">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]" />
            PAPER environment
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-500">Operational telemetry with protected safety controls. Live execution is unavailable.</p>
        </div>
        <nav aria-label="Platform sections" className="mt-7 flex-1 space-y-6 overflow-y-auto">
          {NAV_GROUPS.map((group) => (
            <div key={group.label}>
              <div className="px-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">{group.label}</div>
              <div className="mt-2 space-y-1">
                {group.tabs.map((t) => (
                  <button key={t} onClick={() => selectTab(t)} aria-current={tab === t ? "page" : undefined}
                    className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${tab === t ? "bg-cyan-950/60 text-cyan-200 ring-1 ring-inset ring-cyan-800/70" : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"}`}>
                    <span className={`flex h-6 w-6 items-center justify-center rounded-md text-[10px] font-bold ${tab === t ? "bg-cyan-900/80 text-cyan-300" : "bg-slate-900 text-slate-600"}`}>{TAB_META[t].short}</span>
                    <span>{t}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="border-t border-slate-800 pt-4 text-[11px] leading-5 text-slate-600">
          <div>Platform v0.26 · audited workflow</div>
          <div>Telemetry refreshes every 5 seconds</div>
        </div>
      </aside>

      <div className="lg:pl-64">
        <header className="sticky top-0 z-10 border-b border-slate-800/90 bg-slate-950/90 backdrop-blur">
          <div className="flex min-h-[72px] items-center justify-between gap-3 px-4 py-3 sm:px-6 xl:px-8">
            <div className="flex min-w-0 items-center gap-3">
              <button aria-label="Open navigation" onClick={() => setSidebarOpen(true)} className="rounded-lg border border-slate-800 bg-slate-900 px-2.5 py-2 text-slate-300 hover:bg-slate-800 lg:hidden">☰</button>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="truncate text-sm font-semibold tracking-wide text-slate-100 sm:text-base">{tab}</h1>
                  {status && <StatusBadge value={status.mode} />}
                </div>
                <p className="mt-1 truncate text-xs text-slate-500">{TAB_META[tab].description}</p>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-3">
              <div className="hidden text-right xl:block">
                <div className="text-[10px] uppercase tracking-[0.16em] text-slate-600">Last refresh</div>
                <div className="mt-0.5 font-mono text-xs text-slate-400">{formatRefresh(lastRefresh)}</div>
              </div>
              <div className="hidden items-center gap-2 text-xs text-slate-500 md:flex">
                <span className={`h-1.5 w-1.5 rounded-full ${status?.market_data === "CONNECTED" ? "bg-emerald-400" : "bg-amber-400"}`} />
                <span>Market {status?.market_data ?? "..."}</span>
              </div>
              <KillSwitchButton active={status?.kill_switch_active ?? false} />
            </div>
          </div>
        </header>

        <div className="mx-auto max-w-[1800px] px-4 pb-8 sm:px-6 xl:px-8">
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-cyan-900/60 bg-cyan-950/25 px-4 py-3 text-xs text-cyan-100 shadow-[0_12px_40px_rgba(8,145,178,0.06)]">
            <span><span className="font-semibold tracking-wide text-cyan-200">PAPER ONLY</span><span className="mx-2 text-cyan-700">/</span>no live execution or private exchange keys are available in this interface.</span>
            <span className="text-cyan-400/80">Audited workflow</span>
          </div>
          <main className="py-7">
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
    </div>
  );
}
