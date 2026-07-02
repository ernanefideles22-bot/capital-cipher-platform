// Types derived from /contracts JSON Schemas (ADR-003 contract-first).

export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: { code: string; message: string; details: Record<string, unknown> } | null;
  meta: { request_id: string; timestamp: string };
}

export interface SystemStatus {
  mode: string;
  kill_switch_active: boolean;
  market_data: string;
  orchestrator: string;
  risk: string;
  database: string;
}

export interface AgentHealth {
  name: string;
  status: string;
  version: string;
  critical: boolean;
  enabled: boolean;
  last_run_at: string | null;
  avg_latency_ms: number;
  error_rate: number;
  total_runs: number;
  total_failures: number;
  last_signal: string | null;
  last_confidence: number | null;
}

export interface Decision {
  decision_id: string;
  correlation_id: string;
  symbol: string;
  timeframe: string;
  candidate_action: string;
  confidence: number;
  strategy: string;
  reason: string;
  agent_summary: { name: string; signal: string; confidence: number; reason: string }[];
  warnings: string[];
  risk_status: string;
  created_at: string;
}

export interface RiskStatus {
  daily_pnl_percent: number;
  consecutive_losses: number;
  open_positions: number;
  blocked_operations: number;
  kill_switch_active: boolean;
  kill_switch_reason: string | null;
}

export interface PaperOrder {
  paper_order_id: string;
  symbol: string;
  side: string;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  position_size: number;
  status: string;
  pnl: number | null;
  opened_at: string | null;
  closed_at: string | null;
}

export interface PaperPerformance {
  total_trades: number;
  open_trades: number;
  closed_trades: number;
  win_rate: number;
  net_pnl: number;
  gross_pnl: number;
  fees_total: number;
  max_drawdown_percent: number;
  consecutive_losses: number;
  balance: number;
  initial_balance: number;
}

export interface Candle {
  symbol: string;
  timeframe: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  closed_at: string;
}

export interface AuditEvent {
  audit_id: string;
  correlation_id: string;
  audit_type: string;
  entity_type: string;
  entity_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface BacktestReport {
  backtest_id: string;
  symbol: string;
  timeframe: string;
  start_date: string;
  end_date: string;
  candles_processed: number;
  decisions: number;
  actionable_decisions: number;
  blocked_by_risk: number;
  total_trades: number;
  win_rate: number;
  profit_factor: number | null;
  expectancy: number;
  max_drawdown: number;
  max_consecutive_losses: number;
  net_pnl: number;
  net_pnl_percent: number;
  fees: number;
  slippage: number;
  final_balance: number;
  equity_curve?: { timestamp: string; balance: number }[];
  created_at: string;
}

export interface SymbolPerformance {
  key: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_pnl: number;
  profit_factor: number | null;
}

export interface AgentRankingRow {
  agent_name: string;
  reliability_score: number;
  directional_accuracy: number | null;
  avg_confidence: number | null;
  signal_distribution: Record<string, number>;
  overconfident_losses: number;
  evaluated_decisions: number;
  score: number | null;
  sample_sufficient: boolean;
  avg_latency_ms: number;
  total_runs: number;
  total_failures: number;
  note: string;
}

export interface PerformanceReport {
  overall: PaperPerformance;
  breakdown_by: string;
  breakdown: SymbolPerformance[];
  equity_curve: { timestamp: string; balance: number }[];
}
