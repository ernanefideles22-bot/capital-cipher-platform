import type { ApiResponse } from "../types";

export type GateSymbol = "BTCUSDT" | "ETHUSDT" | "SOLUSDT";
export type GateTimeframe = "15m" | "1h" | "4h";

export interface ShadowStatus {
  status: string;
  checked_at?: string | null;
  execution_authorized?: boolean;
  markets?: Record<string, { status: string }>;
  cycle_count?: number;
  consecutive_ready_cycles?: number;
  cycle_duration_p50_ms?: number;
  cycle_duration_p95_ms?: number;
  latency_samples?: number;
}

export interface CollectionStatus {
  status: string;
  error_code?: string | null;
  journal_observations?: number;
  max_observations?: number;
  pending_queue_size?: number;
  worker_alive?: boolean;
  checked_at?: string | null;
  source_last_observed_at_utc?: string | null;
  execution_authorized?: boolean;
  data_is_current_proven?: boolean;
  shutdown_pending?: boolean;
}

export interface GateStatus {
  environment: "GATE_TESTNET";
  source_revision?: string | null;
  status: string;
  checked_at?: string | null;
  authenticated: boolean;
  execution_enabled?: boolean;
  canary_execution_enabled?: boolean;
  oms_release_authorized?: boolean;
  oms_risk_clear?: boolean;
  oms_flow_status?: string;
  storage_status?: string;
  recovery_status?: string;
  inventory_status?: string;
  inventory_checked_at?: string | null;
  professional_shadow?: ShadowStatus;
  strategy_shadow?: ShadowStatus;
  forward_research?: CollectionStatus;
}

export interface ResearchArm {
  initial_equity: number;
  completed_trades: number;
  realized_net_pnl: number;
  open_position: boolean;
  open_net_exit_estimate: number;
  candle_close_max_drawdown_pct: number;
}

export interface ResearchPair {
  symbol: string;
  strategy: string;
  entry_timeframe: string;
  window: { post_freeze_candles: number };
  baseline: ResearchArm;
  confluence: ResearchArm;
  evidence_status: string;
  cash_benchmark_pnl: number;
}

export interface ForwardReport {
  collection: CollectionStatus;
  comparison: {
    comparisons: ResearchPair[];
    minimum_post_freeze_15m_candles: number;
    minimum_closed_trades_each_arm: number;
    valuation?: { policy_frozen_at_utc: string };
  } | null;
}

export interface GateCandle {
  close_time_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface GateCandles {
  source: "GATE";
  market: "GATE_USDT_FUTURES_TESTNET";
  environment: "GATE_TESTNET";
  symbol: GateSymbol;
  timeframe: GateTimeframe;
  observed_at: string;
  closed_candles_only: true;
  candles: GateCandle[];
}

const BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api/v1").replace(/\/$/, "");

async function read<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}/gate${path}`, {
    method: "GET", cache: "no-store", credentials: "omit",
    signal: AbortSignal.timeout(10000),
  });
  const body = (await response.json().catch(() => null)) as ApiResponse<T> | null;
  if (!response.ok || !body?.success || body.data == null) {
    throw new Error("Telemetria Gate indisponível");
  }
  return body.data;
}

export const gateApi = {
  status: () => read<GateStatus>("/status"),
  forward: () => read<ForwardReport>("/research/forward"),
  candles: (symbol: GateSymbol, timeframe: GateTimeframe) => read<GateCandles>(
    `/market/candles?${new URLSearchParams({ symbol, timeframe })}`,
  ),
};
