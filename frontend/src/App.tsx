import { useEffect, useState } from "react";
import KillSwitchButton from "./components/KillSwitchButton";
import StatusBadge from "./components/StatusBadge";
import { I18nProvider, useI18n, type Language } from "./i18n";
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
type TranslationKey = Parameters<ReturnType<typeof useI18n>["t"]>[0];

const NAV_GROUPS: { label: TranslationKey; tabs: Tab[] }[] = [
  { label: "controlRoom", tabs: ["Overview", "Market"] },
  { label: "research", tabs: ["Agents", "Decisions", "Backtest", "Reports"] },
  { label: "controls", tabs: ["Risk", "Paper"] },
  { label: "records", tabs: ["Audit"] },
];

const TAB_META: Record<Tab, { short: string; label: TranslationKey; description: TranslationKey }> = {
  Overview: { short: "OV", label: "overview", description: "descOverview" },
  Market: { short: "MK", label: "market", description: "descMarket" },
  Agents: { short: "AG", label: "agents", description: "descAgents" },
  Decisions: { short: "DC", label: "decisions", description: "descDecisions" },
  Risk: { short: "RK", label: "risk", description: "descRisk" },
  Paper: { short: "PP", label: "paper", description: "descPaper" },
  Backtest: { short: "BT", label: "backtest", description: "descBacktest" },
  Reports: { short: "RP", label: "reports", description: "descReports" },
  Audit: { short: "AU", label: "audit", description: "descAudit" },
};

function formatRefresh(value: Date | null, language: Language, waiting: string): string {
  if (!value) return waiting;
  return value.toLocaleTimeString(language, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function AppShell() {
  const { language, setLanguage, t, status: translateStatus } = useI18n();
  const [tab, setTab] = useState<Tab>("Overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const systemStatus = usePolling(api.status, 5000);

  useEffect(() => { if (systemStatus) setLastRefresh(new Date()); }, [systemStatus]);
  const selectTab = (nextTab: Tab) => { setTab(nextTab); setSidebarOpen(false); };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      {sidebarOpen && <button aria-label={t("closeNavigation")} onClick={() => setSidebarOpen(false)} className="fixed inset-0 z-20 bg-slate-950/70 lg:hidden" />}
      <aside className={`fixed inset-y-0 left-0 z-30 flex w-64 flex-col border-r border-slate-800/90 bg-slate-950/95 px-4 py-5 shadow-2xl backdrop-blur transition-transform duration-200 lg:translate-x-0 ${sidebarOpen ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center gap-3 px-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-cyan-700/60 bg-cyan-950/60 text-xs font-black tracking-tight text-cyan-300">CC</span>
          <div className="min-w-0"><div className="truncate text-sm font-bold tracking-[0.16em] text-slate-100">CAPITAL CIPHER</div><div className="mt-0.5 text-[10px] uppercase tracking-[0.18em] text-slate-500">{t("brandSubtitle")}</div></div>
        </div>
        <div className="mt-7 rounded-xl border border-emerald-900/70 bg-emerald-950/20 px-3 py-3">
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-emerald-300"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]" />{t("paperEnvironment")}</div>
          <p className="mt-1 text-xs leading-5 text-slate-500">{t("paperDescription")}</p>
        </div>
        <nav aria-label={t("controlRoom")} className="mt-7 flex-1 space-y-6 overflow-y-auto">
          {NAV_GROUPS.map((group) => <div key={group.label}>
            <div className="px-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">{t(group.label)}</div>
            <div className="mt-2 space-y-1">{group.tabs.map((item) => <button key={item} onClick={() => selectTab(item)} aria-current={tab === item ? "page" : undefined} className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${tab === item ? "bg-cyan-950/60 text-cyan-200 ring-1 ring-inset ring-cyan-800/70" : "text-slate-400 hover:bg-slate-900 hover:text-slate-200"}`}><span className={`flex h-6 w-6 items-center justify-center rounded-md text-[10px] font-bold ${tab === item ? "bg-cyan-900/80 text-cyan-300" : "bg-slate-900 text-slate-600"}`}>{TAB_META[item].short}</span><span>{t(TAB_META[item].label)}</span></button>)}</div>
          </div>)}
        </nav>
        <div className="border-t border-slate-800 pt-4 text-[11px] leading-5 text-slate-600"><div>{t("platformVersion")}</div><div>{t("refreshEvery")}</div></div>
      </aside>

      <div className="lg:pl-64">
        <header className="sticky top-0 z-10 border-b border-slate-800/90 bg-slate-950/90 backdrop-blur">
          <div className="flex min-h-[72px] items-center justify-between gap-3 px-4 py-3 sm:px-6 xl:px-8">
            <div className="flex min-w-0 items-center gap-3"><button aria-label={t("openNavigation")} onClick={() => setSidebarOpen(true)} className="rounded-lg border border-slate-800 bg-slate-900 px-2.5 py-2 text-slate-300 hover:bg-slate-800 lg:hidden">☰</button><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h1 className="truncate text-sm font-semibold tracking-wide text-slate-100 sm:text-base">{t(TAB_META[tab].label)}</h1>{systemStatus && <StatusBadge value={systemStatus.mode} />}</div><p className="mt-1 truncate text-xs text-slate-500">{t(TAB_META[tab].description)}</p></div></div>
            <div className="flex shrink-0 items-center gap-3">
              <div className="hidden text-right xl:block"><div className="text-[10px] uppercase tracking-[0.16em] text-slate-600">{t("lastRefresh")}</div><div className="mt-0.5 font-mono text-xs text-slate-400">{formatRefresh(lastRefresh, language, t("waitingTelemetry"))}</div></div>
              <div className="hidden items-center gap-2 text-xs text-slate-500 md:flex"><span className={`h-1.5 w-1.5 rounded-full ${systemStatus?.market_data === "CONNECTED" ? "bg-emerald-400" : "bg-amber-400"}`} /><span>{t("marketConnected")} {systemStatus ? translateStatus(systemStatus.market_data) : "..."}</span></div>
              <label className="flex items-center gap-2 text-xs text-slate-500"><span className="sr-only">{t("language")}</span><select aria-label={t("language")} value={language} onChange={(event) => setLanguage(event.target.value as Language)} className="rounded-lg border border-slate-800 bg-slate-900 px-2 py-1.5 text-xs text-slate-300"><option value="pt-BR">{t("portuguese")}</option><option value="en-US">{t("english")}</option></select></label>
              <KillSwitchButton active={systemStatus?.kill_switch_active ?? false} />
            </div>
          </div>
        </header>
        <div className="mx-auto max-w-[1800px] px-4 pb-8 sm:px-6 xl:px-8">
          <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-cyan-900/60 bg-cyan-950/25 px-4 py-3 text-xs text-cyan-100 shadow-[0_12px_40px_rgba(8,145,178,0.06)]"><span><span className="font-semibold tracking-wide text-cyan-200">{t("paperOnly")}</span><span className="mx-2 text-cyan-700">/</span>{t("noLiveExecution")}</span><span className="text-cyan-400/80">{t("auditedWorkflow")}</span></div>
          <main className="py-7">{tab === "Overview" && <Overview />}{tab === "Market" && <Market />}{tab === "Agents" && <Agents />}{tab === "Decisions" && <Decisions />}{tab === "Risk" && <Risk />}{tab === "Paper" && <Paper />}{tab === "Backtest" && <Backtest />}{tab === "Reports" && <Reports />}{tab === "Audit" && <Audit />}</main>
        </div>
      </div>
    </div>
  );
}

export default function App() { return <I18nProvider><AppShell /></I18nProvider>; }
