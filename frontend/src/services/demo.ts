const BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api/v1").replace(/\/$/, "");

export interface DemoStatus { configured: boolean; authenticated: boolean; automatic_enabled: boolean; checked_at?: string }
interface Output { agent_name: string; status: string; signal: string; confidence: number; reason: string }
export interface DemoAnalysis { id: string; symbol: string; timeframe: string; candle_closed_at: string; total_agents: number; primary_agents: string[]; agents: Output[]; decision: { candidate_action: string; confidence: number; reason: string }; risk: { approved: boolean; reason: string } | null }
interface Trade { id: string; symbol: string; side: string; state: string; quantity: string; entry_price?: number; stop_loss: string; take_profit: string; realized_pnl_usdt: number | null }
export interface DemoSnapshot { account: { equity_usdt: number; available_usdt: number; unrealized_pnl_usdt: number; positions: unknown[] }; execution: { automatic_enabled: boolean; halted: boolean; canary_verified: boolean; trades: Trade[]; events: { seq: number; time: string; kind: string }[]; analysis: DemoAnalysis | null } }
async function request<T>(path: string, key?: string, body?: unknown): Promise<T> {
  const response = await fetch(`${BASE}/binance-demo/${path}`, { method: body === undefined ? "GET" : "POST", headers: { ...(key ? { "X-API-Key": key } : {}), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(body === undefined ? 20000 : 125000) });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload?.success) {
    const detail = payload?.error?.message ?? payload?.detail?.message ?? payload?.detail;
    throw new Error(typeof detail === "string" ? detail : `Consulta Demo não confirmada (${response.status})`);
  }
  return payload.data as T;
}
export const demoApi = { status: () => request<DemoStatus>("status"), snapshot: (key: string) => request<DemoSnapshot>("snapshot", key), action: (action: "analysis" | "execute" | "close" | "automatic" | "reconcile" | "kill" | "reset" | "canary", key: string, body: unknown = {}) => request<unknown>(action, key, body) };
