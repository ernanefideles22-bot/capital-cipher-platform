import type {
  AgentHealth, AgentRankingRow, ApiResponse, AuditEvent, BacktestReport, Candle,
  Decision, PaperOrder, PaperPerformance, PerformanceReport, RiskStatus, SystemStatus,
} from "../types";

// Keep the default same-origin for a dashboard served by the backend. A
// separate static host may provide an explicit HTTPS API origin at build time;
// credentials are never read from Vite environment variables.
const BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api/v1").replace(/\/$/, "");

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`);
  const body = (await response.json().catch(() => null)) as ApiResponse<T> | null;
  if (!response.ok || !body?.success || body.data === null) {
    throw new Error(body?.error?.message ?? `Request failed (${response.status})`);
  }
  return body.data;
}

export const api = {
  status: () => get<SystemStatus>("/status"),
  agents: () => get<{ agents: AgentHealth[] }>("/agents/status"),
  decisions: () => get<{ decisions: Decision[] }>("/decisions"),
  risk: () => get<RiskStatus>("/risk/status"),
  riskLimits: () => get<Record<string, number>>("/risk/limits"),
  paperOrders: () => get<{ orders: PaperOrder[] }>("/paper/orders"),
  paperPerformance: () => get<PaperPerformance>("/paper/performance"),
  candles: (symbol: string, timeframe: string) => {
    const params = new URLSearchParams({ symbol, timeframe, limit: "200" });
    return get<{ candles: Candle[] }>(`/market/candles?${params.toString()}`);
  },
  auditEvents: () => get<{ events: AuditEvent[] }>("/audit/events"),
  auditChain: (correlationId: string) =>
    get<{ chain: AuditEvent[] }>(`/audit/correlation/${correlationId}`),
  performanceReport: (by: "symbol" | "timeframe") =>
    get<PerformanceReport>(`/reports/performance?by=${by}`),
  agentRanking: () => get<{ ranking: AgentRankingRow[] }>("/reports/agents/ranking"),
  backtestReports: () => get<{ reports: BacktestReport[] }>("/backtest/reports"),
  runBacktest: async (body: Record<string, unknown>, apiKey: string) => {
    const response = await fetch(`${BASE}/backtest/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify(body),
    });
    return response.json() as Promise<ApiResponse<{ report: BacktestReport }>>;
  },
  killSwitch: async (reason: string, apiKey: string) => {
    const response = await fetch(`${BASE}/risk/kill-switch`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify({ reason }),
    });
    return response.json();
  },
};
