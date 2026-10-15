import Card from "../components/Card";
import { useI18n } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Risk() {
  const { t } = useI18n();
  const risk = usePolling(api.risk);
  const limits = usePolling(api.riskLimits);
  return <div className="space-y-5"><section><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("controls")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("risk")}</h2></section><div className="grid grid-cols-2 gap-3 md:grid-cols-4"><Card title={t("dailyPnl")}><span className={(risk?.daily_pnl_percent ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>{risk?.daily_pnl_percent?.toFixed(2) ?? "…"}%</span></Card><Card title={t("consecutiveLosses")}>{risk?.consecutive_losses ?? "…"}</Card><Card title={t("openPositions")}>{risk?.open_positions ?? "…"}</Card><Card title={t("blockedOperations")}>{risk?.blocked_operations ?? "…"}</Card></div><div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4"><h3 className="mb-3 text-sm font-semibold text-slate-300">{t("configuredLimits")}</h3><table className="text-sm text-slate-300"><tbody>{limits && Object.entries(limits).map(([key, value]) => <tr key={key} className="border-b border-slate-900"><td className="py-2 pr-8 font-mono text-xs text-slate-500">{key}</td><td>{String(value)}</td></tr>)}</tbody></table></div><p className="text-xs leading-5 text-slate-600">{t("phasePaperOnly")}</p></div>;
}
