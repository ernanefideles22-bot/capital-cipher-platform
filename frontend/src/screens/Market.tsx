import { createChart, ColorType, type IChartApi } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../services/api";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"];

export default function Market() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("15m");
  const [empty, setEmpty] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      height: 420,
    });
    chartRef.current = chart;
    const series = chart.addCandlestickSeries();

    let active = true;
    const load = async () => {
      try {
        const { candles } = await api.candles(symbol, timeframe);
        if (!active) return;
        setEmpty(candles.length === 0);
        series.setData(
          candles.map((c) => ({
            time: (new Date(c.closed_at).getTime() / 1000) as never,
            open: c.open, high: c.high, low: c.low, close: c.close,
          }))
        );
      } catch {
        setEmpty(true);
      }
    };
    load();
    const id = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(id);
      chart.remove();
    };
  }, [symbol, timeframe]);

  return (
    <div>
      <div className="flex gap-2 mb-3">
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
          className="bg-slate-800 text-slate-200 rounded px-2 py-1 text-sm border border-slate-700">
          {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}
          className="bg-slate-800 text-slate-200 rounded px-2 py-1 text-sm border border-slate-700">
          {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
        </select>
      </div>
      {empty && (
        <div className="text-amber-400 text-sm mb-2">
          No candles stored yet. Start the backend with ENABLE_MARKET_DATA=1 to stream public data.
        </div>
      )}
      <div ref={containerRef} className="rounded-lg overflow-hidden border border-slate-800" />
    </div>
  );
}
