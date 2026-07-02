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
      <header className="flex items-center justify-between px-5 py-3 border-b border-slate-800 bg-slate-950 sticky top-0 z-10">
        <div className="flex items-center gap-3">
          <span className="font-bold tracking-wide text-lg">CAPITAL CIPHER AI</span>
          {status && <StatusBadge value={status.mode} />}
          <span className="text-xs text-slate-500">
            market: {status?.market_data ?? "…"} · db: {status?.database ?? "…"}
          </span>
        </div>
        <KillSwitchButton active={status?.kill_switch_active ?? false} />
      </header>
      <nav className="flex gap-1 px-5 pt-3">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-3 py-1.5 rounded-t text-sm ${
              tab === t ? "bg-slate-900 text-white border border-b-0 border-slate-800"
                        : "text-slate-500 hover:text-slate-300"}`}>
            {t}
          </button>
        ))}
      </nav>
      <main className="p-5">
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
  );
}
