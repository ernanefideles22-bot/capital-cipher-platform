import type {
  AgentHealth, AgentRankingRow, ApiResponse, AuditEvent, BacktestReport, Candle,
  Decision, PaperOrder, PaperPerformance, PerformanceReport, RiskStatus, SystemStatus,
} from "../types";

const BASE = "/api/v1";

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`);
  const body = (await response.json()) as ApiResponse<T>;
  if (!body.success || body.data === null) {
    throw new Error(body.error?.message ?? "Request failed");
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
  candles: (symbol: string, timeframe: string) =>
    get<{ candles: Candle[] }>(`/market/candles?symbol=${symbol}&timeframe=${timeframe}&limit=200`),
  auditEvents: () => get<{ events: AuditEvent[] }>("/audit/events"),
  auditChain: (correlationId: string) =>
    get<{ chain: AuditEvent[] }>(`/audit/correlation/${correlationId}`),
  performanceReport: (by: "symbol" | "timeframe") =>
    get<PerformanceReport>(`/reports/performance?by=${by}`),
  agentRanking: () => get<{ ranking: AgentRankingRow[] }>("/reports/agents/ranking"),
  backtestReports: () => get<{ reports: BacktestReport[] }>("/backtest/reports"),
  runBacktest: async (body: Record<string, unknown>) => {
    const response = await fetch(`${BASE}/backtest/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
