import { createChart, ColorType, type IChartApi } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { api } from "../services/api";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"];

export default function Market() {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("15m");
  const [empty, setEmpty] = useState(false);
  useEffect(() => { if (!containerRef.current) return; const chart = createChart(containerRef.current, { layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#94a3b8" }, grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } }, height: 420 }); chartRef.current = chart; const series = chart.addCandlestickSeries(); let active = true; const load = async () => { try { const { candles } = await api.candles(symbol, timeframe); if (!active) return; setEmpty(candles.length === 0); series.setData(candles.map((c) => ({ time: (new Date(c.closed_at).getTime() / 1000) as never, open: c.open, high: c.high, low: c.low, close: c.close }))); } catch { setEmpty(true); } }; load(); const id = setInterval(load, 5000); return () => { active = false; clearInterval(id); chart.remove(); }; }, [symbol, timeframe]);
  return <div className="space-y-4"><section><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("controlRoom")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("market")}</h2><p className="mt-1 text-sm text-slate-500">{t("descMarket")}</p></section><div className="flex gap-2"><select aria-label={t("symbol")} value={symbol} onChange={(event) => setSymbol(event.target.value)} className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-200">{SYMBOLS.map((value) => <option key={value}>{value}</option>)}</select><select aria-label={t("timeframe")} value={timeframe} onChange={(event) => setTimeframe(event.target.value)} className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-200">{TIMEFRAMES.map((value) => <option key={value}>{value}</option>)}</select></div>{empty && <div className="text-sm text-amber-400">{t("noCandles")}</div>}<div ref={containerRef} className="overflow-hidden rounded-xl border border-slate-800" /></div>;
}
