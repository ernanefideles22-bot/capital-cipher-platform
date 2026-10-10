import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { api } from "../services/api";
import type { Candle, Decision } from "../types";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"];

function timeframeSeconds(timeframe: string): number {
  const amount = Number(timeframe.slice(0, -1));
  const unit = timeframe.at(-1);
  return amount * (unit === "m" ? 60 : unit === "h" ? 3600 : 86400);
}

function historicalBarTime(candle: Candle, timeframe: string): UTCTimestamp {
  const closeMs = new Date(candle.closed_at).getTime();
  return Math.floor((closeMs + 1) / 1000 - timeframeSeconds(timeframe)) as UTCTimestamp;
}

function markerTime(decision: Decision, candles: Candle[], timeframe: string): UTCTimestamp | null {
  const decisionMs = new Date(decision.created_at).getTime();
  const eligible = candles.filter((candle) => new Date(candle.closed_at).getTime() <= decisionMs + 15_000);
  const candle = eligible.at(-1);
  return candle ? historicalBarTime(candle, timeframe) : null;
}

export default function Market() {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const historyRef = useRef<Candle[]>([]);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [empty, setEmpty] = useState(false);
  const [live, setLive] = useState(false);
  const [lastPrice, setLastPrice] = useState<number | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const [decisionCount, setDecisionCount] = useState(0);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#0f172a" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#334155" },
      timeScale: {
        borderColor: "#334155",
        timeVisible: true,
        secondsVisible: false,
      },
      height: 500,
    });
    chartRef.current = chart;

    const candleSeries = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      priceLineVisible: true,
      lastValueVisible: true,
    });
    candleSeriesRef.current = candleSeries;

    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    volumeSeriesRef.current = volumeSeries;

    const resizeObserver = new ResizeObserver(() => {
      if (!containerRef.current) return;
      chart.applyOptions({ width: containerRef.current.clientWidth });
    });
    resizeObserver.observe(containerRef.current);

    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;

    const applyDecisionMarkers = async () => {
      try {
        const { decisions } = await api.decisions();
        if (!active) return;
        const relevant = decisions
          .filter((decision) => decision.symbol === symbol && decision.timeframe === timeframe)
          .slice(-40);
        setDecisionCount(relevant.length);
        const markers: SeriesMarker<Time>[] = relevant
          .map((decision) => {
            const time = markerTime(decision, historyRef.current, timeframe);
            if (time === null) return null;
            const action = decision.candidate_action.toUpperCase();
            const buy = action === "BUY";
            const sell = action === "SELL";
            return {
              time,
              position: buy ? "belowBar" : sell ? "aboveBar" : "inBar",
              color: buy ? "#22c55e" : sell ? "#ef4444" : "#f59e0b",
              shape: buy ? "arrowUp" : sell ? "arrowDown" : "circle",
              text: `${action} ${Math.round(decision.confidence)}%`,
            } as SeriesMarker<Time>;
          })
          .filter((marker): marker is SeriesMarker<Time> => marker !== null)
          .sort((a, b) => Number(a.time) - Number(b.time));
        candleSeries.setMarkers(markers);
      } catch {
        // The chart remains usable even if decision telemetry is temporarily unavailable.
      }
    };

    const loadHistory = async (fit = false) => {
      try {
        const { candles } = await api.candles(symbol, timeframe);
        if (!active) return;
        historyRef.current = candles;
        setEmpty(candles.length === 0);
        candleSeries.setData(
          candles.map((candle) => ({
            time: historicalBarTime(candle, timeframe),
            open: candle.open,
            high: candle.high,
            low: candle.low,
            close: candle.close,
          })),
        );
        volumeSeries.setData(
          candles.map((candle) => ({
            time: historicalBarTime(candle, timeframe),
            value: candle.volume,
            color: candle.close >= candle.open ? "rgba(34,197,94,0.45)" : "rgba(239,68,68,0.45)",
          })),
        );
        if (candles.length) {
          setLastPrice(candles.at(-1)?.close ?? null);
          if (fit) chart.timeScale().fitContent();
        }
        await applyDecisionMarkers();
      } catch {
        if (active) setEmpty(true);
      }
    };

    const connectLive = () => {
      if (!active) return;
      socket?.close();
      socket = new WebSocket(
        `wss://stream.binance.com:9443/ws/${symbol.toLowerCase()}@kline_${timeframe}`,
      );
      socket.onopen = () => {
        if (!active) return;
        setLive(true);
      };
      socket.onmessage = (event) => {
        if (!active) return;
        try {
          const payload = JSON.parse(event.data) as {
            E?: number;
            k?: {
              t: number;
              o: string;
              h: string;
              l: string;
              c: string;
              v: string;
              x: boolean;
            };
          };
          const kline = payload.k;
          if (!kline) return;
          const time = Math.floor(kline.t / 1000) as UTCTimestamp;
          const open = Number(kline.o);
          const high = Number(kline.h);
          const low = Number(kline.l);
          const close = Number(kline.c);
          const volume = Number(kline.v);
          candleSeries.update({ time, open, high, low, close });
          volumeSeries.update({
            time,
            value: volume,
            color: close >= open ? "rgba(34,197,94,0.45)" : "rgba(239,68,68,0.45)",
          });
          setLastPrice(close);
          setLastUpdate(new Date(payload.E ?? Date.now()).toISOString());
          setLive(true);
          if (kline.x) void loadHistory(false);
        } catch {
          // Ignore malformed third-party display packets; backend remains authoritative.
        }
      };
      socket.onerror = () => {
        if (active) setLive(false);
      };
      socket.onclose = () => {
        if (!active) return;
        setLive(false);
        reconnectTimer = window.setTimeout(connectLive, 2_000);
      };
    };

    void loadHistory(true);
    connectLive();
    const historyTimer = window.setInterval(() => void loadHistory(false), 15_000);
    const decisionTimer = window.setInterval(() => void applyDecisionMarkers(), 5_000);

    return () => {
      active = false;
      window.clearInterval(historyTimer);
      window.clearInterval(decisionTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      historyRef.current = [];
    };
  }, [symbol, timeframe]);

  return (
    <div className="space-y-4">
      <section>
        <p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("controlRoom")}</p>
        <h2 className="mt-2 text-2xl font-semibold text-white">{t("market")}</h2>
        <p className="mt-1 text-sm text-slate-500">TradingView Lightweight Charts + Binance public market feed. Agentes continuam usando o pipeline auditado do backend.</p>
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={t("symbol")}
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-200"
        >
          {SYMBOLS.map((value) => <option key={value}>{value}</option>)}
        </select>
        <select
          aria-label={t("timeframe")}
          value={timeframe}
          onChange={(event) => setTimeframe(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-200"
        >
          {TIMEFRAMES.map((value) => <option key={value}>{value}</option>)}
        </select>
        <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${live ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" : "border-amber-500/40 bg-amber-500/10 text-amber-300"}`}>
          {live ? "● LIVE" : "○ RECONECTANDO"}
        </span>
        <span className="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-300">
          {lastPrice === null ? "Preço —" : `Preço ${lastPrice.toLocaleString("pt-BR", { maximumFractionDigits: 4 })}`}
        </span>
        <span className="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-400">
          {decisionCount} decisões marcadas
        </span>
        {lastUpdate && (
          <span className="text-xs text-slate-500">
            tick {new Date(lastUpdate).toLocaleTimeString("pt-BR")}
          </span>
        )}
      </div>

      {empty && <div className="text-sm text-amber-400">{t("noCandles")} — o candle em formação continuará aparecendo pelo feed ao vivo.</div>}
      <div ref={containerRef} className="min-h-[500px] overflow-hidden rounded-xl border border-slate-800" />
      <div className="grid gap-3 text-xs text-slate-500 md:grid-cols-3">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-3">Velas e volume atualizam pelo stream público Binance.</div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-3">BUY/SELL/HOLD exibidos no gráfico vêm das decisões persistidas do Capital Cipher.</div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-3">O gráfico é observacional: não envia ordens e não altera Risk/OMS.</div>
      </div>
    </div>
  );
}
