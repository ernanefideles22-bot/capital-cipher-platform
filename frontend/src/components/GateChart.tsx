import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { gateApi, type GateSymbol, type GateTimeframe } from "../services/gate";

const SYMBOLS: GateSymbol[] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const FRAMES: GateTimeframe[] = ["15m", "1h", "4h"];
const SECONDS: Record<GateTimeframe, number> = { "15m": 900, "1h": 3600, "4h": 14400 };

export default function GateChart() {
  const container = useRef<HTMLDivElement>(null);
  const [symbol, setSymbol] = useState<GateSymbol>("BTCUSDT");
  const [timeframe, setTimeframe] = useState<GateTimeframe>("15m");
  const [price, setPrice] = useState<number | null>(null);
  const [error, setError] = useState(false);
  const [received, setReceived] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current) return;
    setPrice(null); setReceived(null); setError(false);
    const chart = createChart(container.current, {
      height: 370,
      layout: { background: { type: ColorType.Solid, color: "#080f1d" }, textColor: "#8292ac" },
      grid: { vertLines: { color: "#142033" }, horzLines: { color: "#142033" } },
      rightPriceScale: { borderColor: "#213049" },
      timeScale: { borderColor: "#213049", timeVisible: true },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#34d399", downColor: "#fb7185", borderVisible: false,
      wickUpColor: "#34d399", wickDownColor: "#fb7185",
    });
    let active = true;
    let pending = false;
    let fitted = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const report = await gateApi.candles(symbol, timeframe);
        if (!active) return;
        if (report.source !== "GATE" || report.environment !== "GATE_TESTNET"
          || report.market !== "GATE_USDT_FUTURES_TESTNET" || report.symbol !== symbol
          || report.timeframe !== timeframe || report.closed_candles_only !== true
          || !report.candles.length) throw new Error("Fonte inválida");
        const bars = report.candles.map((bar) => ({
          time: (bar.close_time_ms / 1000 - SECONDS[timeframe]) as UTCTimestamp,
          open: bar.open, high: bar.high, low: bar.low, close: bar.close,
        }));
        series.setData(bars);
        setPrice(bars.at(-1)?.close ?? null);
        setReceived(report.observed_at);
        setError(false);
        if (!fitted) { chart.timeScale().fitContent(); fitted = true; }
      } catch {
        if (active) {
          series.setData([]); setError(true); setPrice(null); setReceived(null); fitted = false;
        }
      } finally { pending = false; }
    };
    const resize = new ResizeObserver(() => {
      if (container.current) chart.applyOptions({ width: container.current.clientWidth });
    });
    resize.observe(container.current);
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => { active = false; window.clearInterval(timer); resize.disconnect(); chart.remove(); };
  }, [symbol, timeframe]);

  return <section className="overflow-hidden rounded-2xl border border-slate-800 bg-[#080f1d]">
    <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 px-5 py-4">
      <div><div className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">Gate.io · Futuros Testnet</div>
        <h2 className="mt-1 text-lg font-semibold">{symbol} <span className="ml-2 font-mono text-sm text-slate-400">{price === null ? "—" : price.toLocaleString("pt-BR", { maximumFractionDigits: 4 })}</span></h2>
      </div>
      <div className="flex gap-2">
        <select aria-label="Mercado Gate" value={symbol} onChange={(e) => setSymbol(e.target.value as GateSymbol)} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs">{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
        <select aria-label="Timeframe Gate" value={timeframe} onChange={(e) => setTimeframe(e.target.value as GateTimeframe)} className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs">{FRAMES.map((f) => <option key={f}>{f}</option>)}</select>
      </div>
    </div>
    <div className="relative"><div ref={container} />
      {error && <div role="alert" className="absolute inset-0 flex items-center justify-center bg-[#080f1d]/95 p-6 text-center text-sm text-amber-300">Candles Gate indisponíveis. A fonte permanece Gate; nenhum preço de outra corretora é usado.</div>}
    </div>
    <div className="border-t border-slate-800 px-5 py-3 text-[11px] leading-5 text-slate-500">Candles encerrados · atualização a cada 30 s · horários do gráfico em UTC.<br />
      {received ? `Snapshot recebido às ${new Date(received).toLocaleTimeString("pt-BR", { timeZone: "America/Manaus" })} (Manaus). ` : "Aguardando snapshot Gate. "}
      Este gráfico não representa ordens ou posições. A coleta financeira S3 usa entradas de 15m.</div>
  </section>;
}
