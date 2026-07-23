import Card from "../components/Card";
import { useI18n } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Paper() {
  const { t, status: translateStatus } = useI18n();
  const perf = usePolling(api.paperPerformance);
  const orders = usePolling(api.paperOrders, 4000);
  return <div className="space-y-5"><section><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("controls")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("paper")}</h2></section><div className="grid grid-cols-2 gap-3 md:grid-cols-5"><Card title={t("closedTrades")}>{perf?.closed_trades ?? "…"}</Card><Card title={t("winRate")}>{perf ? `${perf.win_rate.toFixed(1)}%` : "…"}</Card><Card title={t("netPnl")}><span className={perf && perf.net_pnl < 0 ? "text-red-400" : "text-emerald-400"}>{perf?.net_pnl.toFixed(2) ?? "…"}</span></Card><Card title={t("feesEstimated")}>{perf?.fees_total?.toFixed(2) ?? "…"}</Card><Card title={t("simulatedDrawdown")}>{perf ? `${perf.max_drawdown_percent.toFixed(2)}%` : "…"}</Card></div><div className="overflow-auto rounded-xl border border-slate-800 bg-slate-950/40"><table className="min-w-[820px] w-full text-sm text-slate-300"><thead><tr className="border-b border-slate-800 text-left text-[11px] uppercase tracking-wider text-slate-500"><th className="px-4 py-3">{t("order")}</th><th>{t("symbol")}</th><th>{t("side")}</th><th>{t("entry")}</th><th>{t("stop")}</th><th>{t("take")}</th><th>{t("orderStatus")}</th><th>{t("netPnl")}</th></tr></thead><tbody>{orders?.orders.map((order) => <tr key={order.paper_order_id} className="border-b border-slate-900"><td className="px-4 py-3 font-mono text-xs">{order.paper_order_id.slice(0, 8)}…</td><td>{order.symbol}</td><td className={order.side === "BUY" ? "text-emerald-400" : "text-red-400"}>{translateStatus(order.side)}</td><td>{order.entry_price}</td><td>{order.stop_loss ?? "—"}</td><td>{order.take_profit ?? "—"}</td><td>{translateStatus(order.status)}</td><td className={(order.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>{order.pnl?.toFixed(2) ?? "—"}</td></tr>)}</tbody></table></div></div>;
}
