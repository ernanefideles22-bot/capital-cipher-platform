import { useMemo, useState } from "react";
import { useI18n } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Agents() {
  const { t, status: translateStatus } = useI18n();
  const data = usePolling(api.agents);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const agents = data?.agents ?? [];
  const statuses = useMemo(() => Array.from(new Set(agents.map((agent) => agent.status))).sort(), [agents]);
  const filtered = agents.filter((agent) => (!filter || agent.name.toLowerCase().includes(filter.toLowerCase())) && (statusFilter === "ALL" || agent.status === statusFilter));
  const ready = agents.filter((agent) => agent.status === "READY").length;
  const failures = agents.filter((agent) => ["FAILED", "TIMEOUT"].includes(agent.status)).length;

  return (
    <div className="space-y-5">
      <section className="flex flex-col justify-between gap-3 md:flex-row md:items-end"><div><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("research")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("agentOperations")}</h2><p className="mt-1 text-sm text-slate-500">{t("agentOperationsSubtitle")}</p></div><div className="flex gap-2 text-xs text-slate-500"><span className="rounded-lg border border-emerald-900/60 bg-emerald-950/20 px-3 py-2">{t("readyAgents")}: <strong className="text-emerald-300">{ready}</strong></span><span className="rounded-lg border border-slate-800 bg-slate-900 px-3 py-2">{t("failedAgents")}: <strong className={failures ? "text-red-300" : "text-slate-300"}>{failures}</strong></span></div></section>
      <div className="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-950/50 p-3 sm:flex-row"><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("searchAgents")} className="min-w-0 flex-1 rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600" /><select aria-label={t("status")} value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-300"><option value="ALL">{t("allStatuses")}</option>{statuses.map((value) => <option key={value} value={value}>{translateStatus(value)}</option>)}</select></div>
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950/40"><div className="max-h-[68vh] overflow-auto"><table className="min-w-[920px] w-full text-sm text-slate-300"><thead className="sticky top-0 z-[1] bg-slate-950"><tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500"><th className="px-4 py-3">{t("name")}</th><th>{t("status")}</th><th>{t("critical")}</th><th>{t("lastSignal")}</th><th>{t("confidence")}</th><th>{t("averageLatency")}</th><th>{t("runs")}</th><th>{t("failures")}</th></tr></thead><tbody>{filtered.map((agent) => <tr key={agent.name} className="border-b border-slate-900/80 hover:bg-slate-900/60"><td className="px-4 py-3 font-mono text-xs text-cyan-100">{agent.name}</td><td><span className={agent.status === "READY" ? "text-emerald-400" : agent.status === "FAILED" || agent.status === "TIMEOUT" ? "text-red-400" : "text-slate-400"}>{translateStatus(agent.status)}</span></td><td>{agent.critical ? t("yes") : t("no")}</td><td>{agent.last_signal ?? "—"}</td><td>{agent.last_confidence ?? "—"}</td><td>{agent.avg_latency_ms.toFixed(1)} ms</td><td>{agent.total_runs}</td><td className={agent.total_failures > 0 ? "text-red-400" : ""}>{agent.total_failures}</td></tr>)}</tbody></table>{filtered.length === 0 && <div className="px-4 py-12 text-center text-sm text-slate-600">{t("noAgents")}</div>}</div></div>
    </div>
  );
}
