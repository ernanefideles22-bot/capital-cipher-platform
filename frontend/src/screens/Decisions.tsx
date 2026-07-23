import { useState } from "react";
import { useI18n } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import type { AuditEvent } from "../types";

const RISK_COLORS: Record<string, string> = { APPROVED: "text-emerald-400", REDUCED: "text-amber-400", BLOCKED: "text-red-400", KILL_SWITCH: "text-red-500", PENDING: "text-slate-400" };

export default function Decisions() {
  const { t, status: translateStatus } = useI18n();
  const data = usePolling(api.decisions, 4000);
  const [chain, setChain] = useState<AuditEvent[] | null>(null);
  const [chainId, setChainId] = useState<string | null>(null);
  const openChain = async (correlationId: string) => { const result = await api.auditChain(correlationId); setChain(result.chain); setChainId(correlationId); };
  return <div className="space-y-4"><section><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("research")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("decisions")}</h2></section><div className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]"><div className="overflow-auto rounded-xl border border-slate-800 bg-slate-950/40"><table className="min-w-[700px] w-full text-sm text-slate-300"><thead><tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500"><th className="px-4 py-3">{t("time")}</th><th>{t("symbol")}</th><th>{t("action")}</th><th>{t("confidence")}</th><th>{t("riskStatus")}</th><th /></tr></thead><tbody>{data?.decisions.map((decision) => <tr key={decision.decision_id} className="border-b border-slate-900"><td className="px-4 py-3 font-mono text-xs">{new Date(decision.created_at).toLocaleTimeString()}</td><td>{decision.symbol}</td><td className="font-bold">{translateStatus(decision.candidate_action)}</td><td>{decision.confidence}</td><td className={RISK_COLORS[decision.risk_status] ?? ""}>{translateStatus(decision.risk_status)}</td><td><button onClick={() => openChain(decision.correlation_id)} className="text-sky-400 hover:underline text-xs">{t("inspectChain")}</button></td></tr>)}</tbody></table></div><div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4 max-h-[70vh] overflow-auto"><div className="mb-3 text-xs uppercase tracking-wider text-slate-500">{t("decisionChain")} {chainId ? `(${chainId.slice(0, 8)}…)` : ""}</div>{chain ? chain.map((event) => <details key={event.audit_id} className="mb-2 border-b border-slate-800 pb-2"><summary className="cursor-pointer text-sm text-slate-300"><span className="font-mono text-sky-400">{event.audit_type}</span> <span className="text-slate-500 text-xs">{event.created_at}</span></summary><pre className="mt-2 overflow-auto text-xs text-slate-400">{JSON.stringify(event.payload, null, 2)}</pre></details>) : <div className="text-sm text-slate-600">{t("selectDecision")}</div>}</div></div></div>;
}
