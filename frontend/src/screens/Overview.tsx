import StatusBadge from "../components/StatusBadge";
import MetricCard from "../components/MetricCard";
import { useI18n, type Language, type TranslationKey } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import type { AgentHealth } from "../types";

type Family = "core" | "technical" | "derivatives" | "macro" | "statistics" | "other";
const FAMILY_KEYS: Record<Family, TranslationKey> = {
  core: "familyCore", technical: "familyTechnical", derivatives: "familyDerivatives", macro: "familyMacro", statistics: "familyStatistics", other: "familyOther",
};

function classifyAgent(name: string): Family {
  if (/MarketData|DataQuality|DataFreshness|LiquidityProxy/i.test(name)) return "core";
  if (/Funding|OpenInterest|Basis|LongShort|Liquidation|Perpetual|Options|Implied|Gamma|PutCall|OIConcentration/i.test(name)) return "derivatives";
  if (/DXY|PolicyRate|RealYield|Inflation|Macro|Nasdaq|SPX/i.test(name)) return "macro";
  if (/Autocorrelation|Skew|Tail|Hurst|Entropy|Variance|Regime|Fractal|Ratio|Distribution|Drawdown|Recovery|Return/i.test(name)) return "statistics";
  if (/Quant|Trend|Momentum|Volatility|Volume|VWAP|MACD|EMA|MeanReversion|Breakout|Support|Candle|MoneyFlow|Bollinger|Stochastic|Williams|CCI|Donchian|Keltner|OBV|Chaikin|Parkinson|Garman|Ulcer|Pivot|Strength|Coppock|Aroon|Acceleration|Expansion|Compression/i.test(name)) return "technical";
  return "other";
}

function formatNumber(value: number | undefined, language: Language, fractionDigits = 0): string {
  return value == null ? "…" : new Intl.NumberFormat(language, { maximumFractionDigits: fractionDigits }).format(value);
}

export default function Overview() {
  const { language, t } = useI18n();
  const status = usePolling(api.status);
  const risk = usePolling(api.risk);
  const perf = usePolling(api.paperPerformance);
  const agents = usePolling(api.agents);
  const decisions = usePolling(api.decisions);
  const agentRows = agents?.agents ?? [];
  const ready = agentRows.filter((agent) => agent.status === "READY").length;
  const critical = agentRows.filter((agent) => agent.critical).length;
  const failed = agentRows.filter((agent) => ["FAILED", "TIMEOUT"].includes(agent.status)).length;
  const activeAgents = agentRows.filter((agent) => agent.enabled).length;
  const familyCounts = agentRows.reduce<Record<Family, number>>((counts, agent) => {
    const family = classifyAgent(agent.name);
    counts[family] += 1;
    return counts;
  }, { core: 0, technical: 0, derivatives: 0, macro: 0, statistics: 0, other: 0 });
  const pnlTone = !perf ? "neutral" : perf.net_pnl < 0 ? "negative" : "positive";
  const drawdownTone = perf && perf.max_drawdown_percent > 0 ? "warning" : "neutral";
  const pipeline: { label: TranslationKey; detail: TranslationKey }[] = [
    { label: "dataIngestion", detail: "dataIngestionDetail" }, { label: "specialistResearch", detail: "specialistResearchDetail" }, { label: "consensus", detail: "consensusDetail" }, { label: "riskGate", detail: "riskGateDetail" }, { label: "paperExecution", detail: "paperExecutionDetail" }, { label: "immutableAudit", detail: "immutableAuditDetail" },
  ];

  return (
    <div className="space-y-7">
      <section className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end"><div><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("operationsOverview")}</p><h1 className="mt-2 text-2xl font-semibold tracking-tight text-white">{t("controlRoomTitle")}</h1><p className="mt-1 max-w-3xl text-sm text-slate-500">{t("controlRoomSubtitle")}</p></div><p className="text-xs text-slate-500">{t("refreshAutomatically")}</p></section>

      <section><h2 className="mb-3 text-sm font-semibold text-slate-300">{t("platformHealth")}</h2><div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label={t("systemMode")} value={status ? <StatusBadge value={status.mode} /> : "…"} detail={t("executionEnvironment")} />
        <MetricCard label={t("marketData")} value={status?.market_data ? <StatusBadge value={status.market_data} /> : "…"} detail={t("publicDataAdapter")} />
        <MetricCard label={t("database")} value={status?.database ? <StatusBadge value={status.database} /> : "…"} detail={t("persistenceHealth")} />
        <MetricCard label={t("killSwitch")} value={risk?.kill_switch_active ? <span className="text-red-400">{t("active")}</span> : <span className="text-emerald-400">{t("clear")}</span>} detail={risk?.kill_switch_active ? risk.kill_switch_reason ?? t("requestFailed") : t("riskControlsAllow")} tone={risk?.kill_switch_active ? "negative" : "positive"} />
      </div></section>

      <section><h2 className="mb-3 text-sm font-semibold text-slate-300">{t("researchRisk")}</h2><div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label={t("activeAgents")} value={activeAgents || "…"} detail={`${t("enabledAnalyticalAgents")} · ${agentRows.length || "…"}`} />
        <MetricCard label={t("recentDecisions")} value={decisions?.decisions.length ?? "…"} detail={t("latestEvaluatedCandidates")} />
        <MetricCard label={t("openPositions")} value={risk?.open_positions ?? "…"} detail={t("paperPositionsUnderRisk")} />
        <MetricCard label={t("blockedOperations")} value={risk?.blocked_operations ?? "…"} detail={t("riskPolicyInterventions")} tone={(risk?.blocked_operations ?? 0) > 0 ? "warning" : "neutral"} />
      </div></section>

      <section className="rounded-2xl border border-slate-800/90 bg-slate-950/50 p-5 shadow-[0_20px_80px_rgba(2,6,23,0.35)]"><div className="flex flex-col justify-between gap-2 md:flex-row md:items-end"><div><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("institutionalSnapshot")}</p><h2 className="mt-2 text-xl font-semibold text-white">{t("agentCohort")}</h2><p className="mt-1 text-sm text-slate-500">{t("institutionalSnapshotSubtitle")}</p></div><div className="text-right text-xs text-slate-500">{agentRows.length ? `${formatNumber(agentRows.length, language)} ${t("agents").toLowerCase()}` : "…"}</div></div>
        <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4"><MetricCard label={t("readyAgents")} value={formatNumber(ready, language)} detail={agentRows.length ? `${formatNumber((ready / agentRows.length) * 100, language, 1)}${t("cohortPercentage")}` : t("waitingTelemetry")} tone={failed ? "warning" : "positive"} /><MetricCard label={t("criticalAgents")} value={formatNumber(critical, language)} detail={t("enabledAnalyticalAgents")} /><MetricCard label={t("failedAgents")} value={formatNumber(failed, language)} detail={failed ? t("riskPolicyInterventions") : t("noRunsYet")} tone={failed ? "negative" : "neutral"} /><MetricCard label={t("runs")} value={formatNumber(agentRows.reduce((sum, agent) => sum + agent.total_runs, 0), language)} detail={t("noRunsYet")} /></div>
        <div className="mt-6 grid gap-5 xl:grid-cols-[1.1fr_1fr]">
          <div><div className="mb-3 flex items-center justify-between"><h3 className="text-sm font-semibold text-slate-300">{t("coverageByFamily")}</h3><span className="text-[11px] text-slate-600">{t("familyNote")}</span></div><div className="space-y-3">{(Object.keys(FAMILY_KEYS) as Family[]).map((family) => { const count = familyCounts[family]; const percentage = agentRows.length ? (count / agentRows.length) * 100 : 0; return <div key={family}><div className="mb-1 flex justify-between text-xs"><span className="text-slate-400">{t(FAMILY_KEYS[family])}</span><span className="font-mono text-slate-500">{count}</span></div><div className="h-2 overflow-hidden rounded-full bg-slate-900"><div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-400" style={{ width: `${percentage}%` }} /></div></div>; })}</div></div>
          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"><h3 className="text-sm font-semibold text-slate-300">{t("orchestrationPipeline")}</h3><p className="mt-1 text-xs leading-5 text-slate-500">{t("pipelineSubtitle")}</p><div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">{pipeline.map((stage, index) => <div key={stage.label} className="relative rounded-lg border border-slate-800 bg-slate-950/70 p-3"><div className="text-[10px] font-mono text-cyan-500">0{index + 1}</div><div className="mt-2 text-xs font-semibold text-slate-200">{t(stage.label)}</div><div className="mt-1 text-[11px] leading-4 text-slate-600">{t(stage.detail)}</div></div>)}</div></div>
        </div>
      </section>

      <section><h2 className="mb-3 text-sm font-semibold text-slate-300">{t("paperPerformance")}</h2><div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label={t("netPnl")} value={perf ? <span className={perf.net_pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{perf.net_pnl.toFixed(2)} USDT</span> : "…"} detail={t("netEstimatedFees")} tone={pnlTone} />
        <MetricCard label={t("simulatedDrawdown")} value={perf ? `${perf.max_drawdown_percent.toFixed(2)}%` : "…"} detail={t("maxObservedDrawdown")} tone={drawdownTone} />
        <MetricCard label={t("paperBalance")} value={perf ? `${perf.balance.toFixed(2)} USDT` : "…"} detail={perf ? `${t("initial")}: ${perf.initial_balance.toFixed(2)} USDT` : t("awaitingPerformance")} />
        <MetricCard label={t("winRate")} value={perf ? `${perf.win_rate.toFixed(1)}%` : "…"} detail={perf ? `${perf.closed_trades} ${t("closedPaperTrades")}` : t("awaitingPerformance")} />
      </div></section>
    </div>
  );
}
