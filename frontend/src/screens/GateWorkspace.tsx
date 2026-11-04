import GateChart from "../components/GateChart";
import type { ReactNode } from "react";
import { usePolling } from "../hooks/usePolling";
import { gateApi, type CollectionStatus, type ResearchArm, type ShadowStatus } from "../services/gate";

const MARKETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
const numeric = (value: number | undefined, digits = 0) => typeof value === "number" && Number.isFinite(value)
  ? value.toLocaleString("pt-BR", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
const stamp = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString("pt-BR", { timeZone: "America/Manaus" }) : "Não informado";
const age = (value: string | null | undefined) => value ? (Date.now() - Date.parse(value)) / 60000 : NaN;

function Pill({ good, children }: { good: boolean; children: ReactNode }) {
  return <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-wider ${good ? "border-emerald-900 bg-emerald-950/40 text-emerald-300" : "border-amber-900 bg-amber-950/30 text-amber-300"}`}><span className={`h-1.5 w-1.5 rounded-full ${good ? "bg-emerald-400" : "bg-amber-400"}`} />{children}</span>;
}

function ShadowCard({ label, title, state, readyStatus }: { label: string; title: string; state?: ShadowStatus; readyStatus: string }) {
  const ready = MARKETS.filter((s) => state?.markets?.[s]?.status === readyStatus).length;
  const fresh = age(state?.checked_at);
  const healthy = state?.status === "AVAILABLE" && ready === 3 && fresh >= 0 && fresh <= 30;
  const labelState = healthy ? "Disponível" : !state ? "Sem telemetria"
    : !Number.isFinite(fresh) || fresh < 0 || fresh > 30 ? "Sem atualização recente"
    : state.status === "AVAILABLE" ? "Parcial" : state.status;
  return <section className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 transition-colors hover:border-cyan-800/70">
    <div className="flex items-center justify-between"><span className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">{label}</span><Pill good={healthy}>{labelState}</Pill></div>
    <h2 className="mt-4 font-semibold text-slate-100">{title}</h2>
    <div className="mt-3 text-3xl font-semibold tracking-tight">{state ? `${ready}/3` : "—"}<span className="ml-2 text-xs font-normal text-slate-500">mercados prontos</span></div>
    <div className="mt-4 flex flex-wrap gap-2">{MARKETS.map((s) => <span key={s} className={`rounded-md border px-2 py-1 text-[10px] ${state?.markets?.[s]?.status === readyStatus ? "border-emerald-900 text-emerald-300" : "border-slate-700 text-slate-500"}`}>{s.replace("USDT", "")}</span>)}</div>
    <p className="mt-4 text-[11px] text-slate-500">Último ciclo: {stamp(state?.checked_at)} · Manaus</p>
    {state?.cycle_count !== undefined && <p className="mt-1 text-[11px] text-slate-500">Ciclo {numeric(state.cycle_count)} · sequência pronta {numeric(state.consecutive_ready_cycles)}</p>}
  </section>;
}

function CollectionCard({ state }: { state?: CollectionStatus }) {
  const observations = state?.journal_observations;
  const maximum = state?.max_observations;
  const usage = typeof observations === "number" && maximum ? Math.max(0, Math.min(100, observations / maximum * 100)) : null;
  const freshness = age(state?.source_last_observed_at_utc);
  const healthy = state?.status === "AVAILABLE" && state.worker_alive === true && state.shutdown_pending !== true
    && usage !== null && usage < 90 && freshness >= 0 && freshness <= 30;
  return <section className="rounded-2xl border border-slate-800 bg-slate-900/40 p-5 transition-colors hover:border-cyan-800/70">
    <div className="flex items-center justify-between"><span className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">S3 · Coleta financeira</span><Pill good={healthy}>{state?.status ?? "Sem telemetria"}</Pill></div>
    <h2 className="mt-4 font-semibold">Observações no diário</h2>
    <div className="mt-3 text-3xl font-semibold">{numeric(observations)}<span className="ml-2 text-xs font-normal text-slate-500">/ {numeric(maximum)}</span></div>
    <div className="mt-4 h-1.5 overflow-hidden rounded bg-slate-800"><div className={usage !== null && usage >= 90 ? "h-full bg-amber-400" : "h-full bg-cyan-400"} style={{ width: `${usage ?? 0}%` }} /></div>
    <p className="mt-4 text-[11px] text-slate-500">Fila pendente: {numeric(state?.pending_queue_size)} · worker: {state?.worker_alive === true ? "ativo" : state?.worker_alive === false ? "parado" : "não informado"}</p>
    <p className="mt-1 text-[11px] text-slate-500">Fila não é commit. Observações não são operações.</p>
  </section>;
}

function ArmCell({ arm }: { arm: ResearchArm }) {
  return <div className="min-w-40"><div className={`font-mono text-sm ${arm.realized_net_pnl < 0 ? "text-rose-300" : arm.realized_net_pnl > 0 ? "text-emerald-300" : "text-slate-300"}`}>{numeric(arm.realized_net_pnl, 2)} USDT</div>
    <div className="mt-1 text-[10px] text-slate-500">{numeric(arm.completed_trades)} encerradas · capital {numeric(arm.initial_equity, 2)}</div>
    <div className="mt-1 text-[10px] text-slate-500">DD por candle: {numeric(arm.candle_close_max_drawdown_pct, 2)}%</div>
    <div className="mt-1 text-[10px] text-slate-500">{arm.open_position ? `Posição aberta · saída estimada ${numeric(arm.open_net_exit_estimate, 2)} USDT` : "Sem posição aberta"}</div>
  </div>;
}

export default function GateWorkspace({ onOpenLegacy }: { onOpenLegacy: () => void }) {
  const status = usePolling(gateApi.status, 10000);
  const financial = usePolling(gateApi.forward, 15000);
  const collection = financial?.collection ?? status?.forward_research;
  const rows = financial?.comparison?.comparisons ?? [];
  const executionOff = status?.execution_enabled === false && status?.canary_execution_enabled === false
    && status?.oms_release_authorized === false && status?.professional_shadow?.execution_authorized === false
    && status?.strategy_shadow?.execution_authorized === false && status?.forward_research?.execution_authorized === false;
  const checkedAge = age(status?.checked_at);
  const connectionFresh = checkedAge >= 0 && checkedAge <= 30;
  const s3Age = age(collection?.source_last_observed_at_utc);
  const s3Stale = !Number.isFinite(s3Age) || s3Age < 0 || s3Age > 30;
  const minBars = financial?.comparison?.minimum_post_freeze_15m_candles;
  const minTrades = financial?.comparison?.minimum_closed_trades_each_arm;
  const floorMet = rows.filter((row) => row.evidence_status === "PRELIMINARY_DESCRIPTIVE_SAMPLE").length;

  return <div className="min-h-screen bg-[#070d18] text-slate-200">
    <header className="border-b border-slate-800 bg-slate-950/80">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-4 px-5 py-5 sm:px-8">
        <div className="flex items-center gap-3"><span className="flex h-10 w-10 items-center justify-center rounded-xl border border-cyan-800 bg-cyan-950/50 text-sm font-black text-cyan-300 shadow-[0_0_25px_rgba(34,211,238,0.08)]">CC</span><div><div className="text-xs font-semibold tracking-[0.22em]">CAPITAL CIPHER</div><div className="mt-1 text-[10px] uppercase tracking-[0.17em] text-slate-500">Gate.io · Research workspace</div></div></div>
        <div className="flex flex-wrap items-center gap-3"><Pill good={status?.authenticated === true && connectionFresh}>{status?.authenticated === true && connectionFresh ? "Gate conectada" : status ? "Revisar conexão" : "Gate indisponível"}</Pill><button onClick={onOpenLegacy} className="rounded-lg border border-slate-700 px-3 py-2 text-[11px] text-slate-400 transition-colors hover:border-slate-500 hover:text-white">Abrir legado Binance / PAPER</button></div>
      </div>
    </header>
    <main className="mx-auto max-w-[1600px] space-y-6 px-5 py-7 sm:px-8">
      <section className="flex flex-wrap items-end justify-between gap-4">
        <div><p className="text-[10px] font-semibold uppercase tracking-[0.25em] text-cyan-400">Pesquisa · Observação · Evidência</p><h1 className="mt-3 text-2xl font-semibold tracking-tight sm:text-3xl">Inteligência sob observação</h1><p className="mt-2 max-w-2xl text-sm text-slate-500">Leitura de mercado, confluência e medição financeira da Gate em uma única visão.</p></div>
        <div className="text-right text-[11px] text-slate-500"><div>Conexão verificada · Manaus</div><div className="mt-1 font-mono text-slate-300">{stamp(status?.checked_at)}</div></div>
      </section>

      <div role={executionOff ? "status" : "alert"} className={`rounded-xl border px-4 py-3 text-xs leading-6 ${executionOff ? "border-cyan-900 bg-cyan-950/20 text-cyan-200" : "border-amber-800 bg-amber-950/30 text-amber-200"}`}>
        <strong>{executionOff ? "Envio e liberação de ordens desabilitados na telemetria consultada." : "Bloqueio de execução não confirmado: revise a telemetria Gate."}</strong>
        <span className="ml-2">Esta área faz somente leitura e não oferece controles de execução.</span>
      </div>
      {!status && <p role="alert" className="rounded-xl border border-amber-900 bg-amber-950/20 p-4 text-sm text-amber-300">Sem status Gate. Dados do backend PAPER não substituem esta conexão.</p>}
      <section className="grid gap-4 lg:grid-cols-3">
        <ShadowCard label="S1 · Leitura profissional" title="Contexto do mercado" state={status?.professional_shadow} readyStatus="READ_ONLY_CHART_READY" />
        <ShadowCard label="S2 · Decisão contextual" title="Confluência das estratégias" state={status?.strategy_shadow} readyStatus="READ_ONLY_CONFLUENCE_READY" />
        <CollectionCard state={collection} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,2.5fr)_minmax(260px,1fr)]">
        <GateChart />
        <aside className="rounded-2xl border border-slate-800 bg-slate-900/30 p-5">
          <p className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">Integridade operacional</p>
          <dl className="mt-6 space-y-5 text-xs">
            {[["Storage Gate", status?.storage_status], ["Recovery", status?.recovery_status], ["OMS Gate", status?.oms_flow_status], ["Inventário · status da leitura", status?.inventory_status]].map(([name, value]) => <div key={name} className="flex items-center justify-between gap-3"><dt className="text-slate-500">{name}</dt><dd className="text-right font-mono text-[11px] text-slate-300">{value ?? "Não informado"}</dd></div>)}
            <div><dt className="text-slate-500">Latência S1 · amostra recente</dt><dd className="mt-2 font-mono text-slate-300">P50 {numeric(status?.professional_shadow?.cycle_duration_p50_ms === undefined ? undefined : status.professional_shadow.cycle_duration_p50_ms / 1000, 2)}s · P95 {numeric(status?.professional_shadow?.cycle_duration_p95_ms === undefined ? undefined : status.professional_shadow.cycle_duration_p95_ms / 1000, 2)}s</dd><dd className="mt-1 text-[10px] text-slate-600">{numeric(status?.professional_shadow?.latency_samples)} amostras</dd></div>
          </dl>
          <div className="mt-6 border-t border-slate-800 pt-4 text-[11px] leading-5 text-slate-500">Inventário disponível não informa quantidades nesta API. Não representa auditoria completa da conta.<br />Revisão Gate: <span className="break-all font-mono">{status?.source_revision ?? "não informada pelo serviço"}</span></div>
        </aside>
      </section>

      <section className="overflow-hidden rounded-2xl border border-slate-800 bg-slate-900/25">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 px-5 py-5"><div><p className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">S3 · Pesquisa prospectiva simulada · 15m</p><h2 className="mt-2 text-lg font-semibold">Evidência financeira por estratégia</h2></div><Pill good={false}>{rows.length ? `${floorMet}/${rows.length} no piso preliminar` : "Sem resultado financeiro disponível"}</Pill></div>
        <div className="space-y-2 px-5 py-4 text-xs leading-5 text-slate-400">
          <p>Lucro/prejuízo líquido das operações encerradas na simulação. Contas independentes, sem soma como carteira e sem fills reais comprovados.</p>
          <p>Piso para análise preliminar: {numeric(minBars)} candles elegíveis por mercado e {numeric(minTrades)} operações encerradas em cada braço. Atingir o piso não comprova vantagem econômica.</p>
          <p className={s3Stale ? "text-amber-300" : "text-slate-500"}>Última observação: {stamp(collection?.source_last_observed_at_utc)} · Manaus. {s3Stale ? "Continuidade recente não confirmada." : "Coleta com atividade recente."} Atualidade dos dados de entrada: {collection?.data_is_current_proven === true ? "declarada verificada pela fonte" : "não comprovada pela fonte"}.</p>
        </div>
        {rows.length ? <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead className="bg-slate-950/50 text-[10px] uppercase tracking-wider text-slate-500"><tr>{["Mercado / estratégia", "Candles elegíveis", "STANDALONE · encerrado", "S1_CONFLUENCE · encerrado", "Caixa", "Amostra"].map((h) => <th key={h} className="px-5 py-3 font-medium">{h}</th>)}</tr></thead><tbody className="divide-y divide-slate-800/70">{rows.map((row) => <tr key={`${row.symbol}:${row.strategy}`} className="transition-colors hover:bg-slate-800/25"><td className="px-5 py-4"><div className="font-semibold text-slate-200">{row.symbol}</div><div className="mt-1 max-w-48 break-words text-[10px] text-slate-500">{row.strategy} · entradas {row.entry_timeframe}</div></td><td className="px-5 py-4 font-mono">{numeric(row.window.post_freeze_candles)}</td><td className="px-5 py-4"><ArmCell arm={row.baseline} /></td><td className="px-5 py-4"><ArmCell arm={row.confluence} /></td><td className="px-5 py-4 font-mono text-slate-400">{numeric(row.cash_benchmark_pnl, 2)}</td><td className="px-5 py-4 text-[10px] text-amber-300">{row.evidence_status === "PRELIMINARY_DESCRIPTIVE_SAMPLE" ? "ANÁLISE PRELIMINAR" : "AMOSTRA INSUFICIENTE"}</td></tr>)}</tbody></table></div> : <p className="px-5 py-8 text-sm text-slate-500">{financial ? `Coleta: ${financial.collection.status}. Nenhuma comparação disponível.` : "Não foi possível obter o relatório financeiro Gate."} Ausência de dados não significa PnL zero.</p>}
        <div className="space-y-1 border-t border-slate-800 px-5 py-4 text-[11px] leading-5 text-slate-500"><p>Posições abertas e saídas estimadas estão separadas do resultado encerrado. Drawdown por candle não mede o pior movimento intrabar.</p><p>Esta fonte não apresenta custos encerrados detalhados, profit factor ou expectativa por operação nesta visão. Não há validação de estresse de custos ou regimes independentes aqui.</p><p>Freeze da política: {stamp(financial?.comparison?.valuation?.policy_frozen_at_utc)} · Manaus. S4/S5 não são homologados por esta telemetria. Estabilidade não demonstra lucratividade.</p></div>
      </section>
    </main>
  </div>;
}
