import { useMemo } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

const VALIDATED_REVISION = "9d06596923d841ea33755d51c244654bffa00b5e";
const CI_RUN = "#89";
const MONTH12_BUNDLE = "61a8523d-058d-4736-bf3a-cc6ef83ea48a";
const MONTH12_HASH = "be0368fdcdea9fd4b0dedb5f55ac5100a4f92951a100bf2867264f9550f6ba93";

function StatePill({ ok, text }: { ok: boolean; text: string }) {
  return <span className={`inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold ${ok ? "border-emerald-800/70 bg-emerald-950/40 text-emerald-300" : "border-amber-800/70 bg-amber-950/40 text-amber-300"}`}>{text}</span>;
}

function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function decimal(value: number | null): string {
  return value === null ? "—" : value.toFixed(4);
}

function criterion(ok: boolean, label: string) {
  return <span className={`rounded border px-2 py-1 text-[10px] ${ok ? "border-emerald-900 bg-emerald-950/20 text-emerald-300" : "border-slate-800 bg-slate-950 text-slate-600"}`}>{label}</span>;
}

export default function Readiness() {
  const status = usePolling(api.status, 5000);
  const risk = usePolling(api.risk, 5000);
  const agentsData = usePolling(api.agents, 5000);
  const specialistData = usePolling(api.specialistScorecards, 10000);
  const candidateData = usePolling(api.specialistCandidates, 10000);
  const paperData = usePolling(api.paperOrders, 5000);

  const agents = agentsData?.agents ?? [];
  const readyAgents = agents.filter((agent) => agent.status === "READY").length;
  const failedAgents = agents.filter((agent) => ["FAILED", "TIMEOUT"].includes(agent.status)).length;
  const orders = paperData?.orders ?? [];
  const scorecards = specialistData?.scorecards ?? [];
  const candidates = candidateData?.candidates ?? [];
  const evaluated = scorecards.filter((card) => card.status === "EVALUATED").length;
  const eligibleCandidates = candidates.filter((item) => item.eligible_for_regime_shadow_test);

  const topSpecialists = useMemo(() => [...scorecards].sort((a, b) => {
    if (a.status !== b.status) return a.status === "EVALUATED" ? -1 : 1;
    const aContribution = a.mean_marginal_contribution ?? Number.NEGATIVE_INFINITY;
    const bContribution = b.mean_marginal_contribution ?? Number.NEGATIVE_INFINITY;
    if (aContribution !== bContribution) return bContribution - aContribution;
    return (b.accuracy ?? 0) - (a.accuracy ?? 0);
  }).slice(0, 10), [scorecards]);

  const runtimeHealthy = Boolean(status) && status?.mode === "PAPER" && !status?.kill_switch_active;
  const marketHealthy = status?.market_data === "CONNECTED";

  return (
    <div className="space-y-6">
      <section className="flex flex-col justify-between gap-4 xl:flex-row xl:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">Control Room</p>
          <h2 className="mt-2 text-2xl font-semibold text-white">Readiness e desenvolvimento dos testes</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">Telemetria real do runtime PAPER, baseline validada e evolução observacional dos agentes para especialistas. Esta tela não habilita TESTNET nem LIVE.</p>
        </div>
        <div className="flex flex-wrap gap-2"><StatePill ok={runtimeHealthy} text={runtimeHealthy ? "PAPER saudável" : "Verificar runtime"} /><StatePill ok={marketHealthy} text={marketHealthy ? "Market data conectado" : "Market data pendente"} /><StatePill ok text={`CI ${CI_RUN} aprovado`} /></div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-6">
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Modo</div><div className="mt-2 text-2xl font-semibold text-cyan-200">{status?.mode ?? "—"}</div><div className="mt-2 text-xs text-slate-600">Execução externa permanece desabilitada</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Agentes prontos</div><div className="mt-2 text-2xl font-semibold text-emerald-300">{readyAgents}<span className="text-sm font-normal text-slate-600"> / {agents.length || 300}</span></div><div className="mt-2 text-xs text-slate-600">Falhas/timeout: {failedAgents}</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Especialistas avaliados</div><div className="mt-2 text-2xl font-semibold text-violet-300">{evaluated}<span className="text-sm font-normal text-slate-600"> / {scorecards.length || "—"}</span></div><div className="mt-2 text-xs text-slate-600">Mínimo atual: 30 amostras</div></article>
        <article className="rounded-xl border border-violet-900/60 bg-violet-950/15 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-violet-500">Candidatos shadow</div><div className="mt-2 text-2xl font-semibold text-violet-200">{eligibleCandidates.length}</div><div className="mt-2 text-xs text-slate-600">Elegíveis para teste por regime</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Ordens PAPER</div><div className="mt-2 text-2xl font-semibold text-white">{orders.length}</div><div className="mt-2 text-xs text-slate-600">Visão atual retornada pelo OMS</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Kill switch</div><div className={`mt-2 text-2xl font-semibold ${status?.kill_switch_active ? "text-red-300" : "text-emerald-300"}`}>{status ? (status.kill_switch_active ? "ATIVO" : "LIVRE") : "—"}</div><div className="mt-2 text-xs text-slate-600">Controle central de emergência</div></article>
      </section>

      <section className="grid gap-5 xl:grid-cols-[1.2fr_0.8fr]">
        <article className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Canário end-to-end</div><h3 className="mt-1 text-lg font-semibold text-white">Fluxo validado no CI</h3></div><StatePill ok text="PASSED" /></div>
          <div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {["Risk gate", "OMS durable order", "Venue fill", "Position + equity", "Reconciliation", "Kill switch", "Reduce-only flatten", "Flat-state verification"].map((step, index) => <div key={step} className="rounded-lg border border-emerald-900/40 bg-emerald-950/10 px-3 py-3"><div className="text-[10px] font-semibold text-emerald-500">{String(index + 1).padStart(2, "0")}</div><div className="mt-1 text-xs text-slate-300">{step}</div></div>)}
          </div>
        </article>

        <article className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
          <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Baseline validada</div>
          <dl className="mt-4 space-y-3 text-sm"><div><dt className="text-xs text-slate-600">Source revision</dt><dd className="mt-1 break-all font-mono text-xs text-cyan-200">{VALIDATED_REVISION}</dd></div><div><dt className="text-xs text-slate-600">Month 12</dt><dd className="mt-1 text-emerald-300">10/10 checks PASSED</dd></div><div><dt className="text-xs text-slate-600">Bundle</dt><dd className="mt-1 break-all font-mono text-xs text-slate-300">{MONTH12_BUNDLE}</dd></div><div><dt className="text-xs text-slate-600">SHA-256</dt><dd className="mt-1 break-all font-mono text-[11px] text-slate-500">{MONTH12_HASH}</dd></div></dl>
        </article>
      </section>

      <section className="rounded-xl border border-violet-900/60 bg-violet-950/10 p-5">
        <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-end"><div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-400">Próxima fase controlada</div><h3 className="mt-1 text-lg font-semibold text-white">Candidatura para teste por regime em SHADOW</h3><p className="mt-1 max-w-4xl text-xs leading-5 text-slate-500">A candidatura exige simultaneamente amostra mínima, acurácia &gt; 50%, Brier &lt; 0,25 e contribuição marginal positiva. Ser candidato não altera consenso, peso, Risk Manager ou OMS.</p></div><div className="flex gap-2"><StatePill ok={candidateData?.decision_authority === false} text="Sem autoridade" /><StatePill ok={candidateData?.automatic_weight_adjustment === false} text="Sem auto-weight" /></div></div>
        <div className="mt-4 space-y-2">
          {candidates.slice(0, 12).map((candidate) => <div key={`${candidate.agent_name}-${candidate.agent_version}`} className={`rounded-lg border p-3 ${candidate.eligible_for_regime_shadow_test ? "border-violet-800/70 bg-violet-950/20" : "border-slate-800 bg-slate-950/50"}`}><div className="flex flex-col justify-between gap-3 xl:flex-row xl:items-center"><div><div className="font-mono text-xs text-cyan-100">{candidate.agent_name}<span className="ml-2 text-[10px] text-slate-700">v{candidate.agent_version}</span></div><div className={`mt-1 text-[10px] font-semibold ${candidate.eligible_for_regime_shadow_test ? "text-violet-300" : candidate.status === "OBSERVING" ? "text-amber-400" : "text-slate-500"}`}>{candidate.status.replaceAll("_", " ")}</div></div><div className="flex flex-wrap gap-1.5">{criterion(candidate.criteria.minimum_sample_reached, "amostra")}{criterion(candidate.criteria.accuracy_above_50_percent, "accuracy > 50%")}{criterion(candidate.criteria.brier_below_random_baseline, "Brier < 0,25")}{criterion(candidate.criteria.positive_marginal_contribution, "contribuição +")}</div><div className="grid grid-cols-3 gap-4 text-right text-[10px] text-slate-600"><div><div>Accuracy</div><div className="mt-1 font-mono text-slate-300">{pct(candidate.accuracy)}</div></div><div><div>Brier</div><div className="mt-1 font-mono text-slate-300">{decimal(candidate.mean_brier_loss)}</div></div><div><div>Marginal</div><div className="mt-1 font-mono text-slate-300">{decimal(candidate.mean_marginal_contribution)}</div></div></div></div></div>)}
          {candidates.length === 0 && <div className="rounded-lg border border-slate-800 px-4 py-10 text-center text-xs text-slate-600">Aguardando amostras liquidadas para classificar candidatos.</div>}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-end"><div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-violet-400">Agentes → especialistas</div><h3 className="mt-1 text-lg font-semibold text-white">Avaliação shadow observacional</h3><p className="mt-1 max-w-4xl text-xs leading-5 text-slate-600">Dados reais do AgentEvaluationService: quantidade de amostras, acurácia direcional, Brier loss e contribuição marginal contra o ensemble. Ainda não existe autoridade de decisão nem ajuste automático de peso.</p></div><div className="flex gap-2"><StatePill ok={specialistData?.decision_authority === false} text="Sem autoridade de decisão" /><StatePill ok={specialistData?.automatic_weight_adjustment === false} text="Pesos não automáticos" /></div></div>
        <div className="mt-4 overflow-x-auto rounded-lg border border-slate-800"><table className="min-w-[900px] w-full text-xs"><thead className="bg-slate-900 text-left text-[10px] uppercase tracking-wider text-slate-600"><tr><th className="px-3 py-2">#</th><th>Agente</th><th>Status</th><th>Amostras</th><th>Acurácia</th><th>Brier</th><th>Contribuição marginal</th></tr></thead><tbody>{topSpecialists.map((card, index) => <tr key={`${card.agent_name}-${card.agent_version}`} className="border-t border-slate-900 text-slate-300"><td className="px-3 py-2 text-slate-600">{index + 1}</td><td className="font-mono text-cyan-100">{card.agent_name}<span className="ml-2 text-[10px] text-slate-700">v{card.agent_version}</span></td><td><span className={card.status === "EVALUATED" ? "text-emerald-300" : "text-amber-300"}>{card.status === "EVALUATED" ? "AVALIADO" : "AMOSTRA INSUFICIENTE"}</span></td><td>{card.sample_count} / {card.minimum_samples}</td><td>{pct(card.accuracy)}</td><td className="font-mono">{decimal(card.mean_brier_loss)}</td><td className={`font-mono ${(card.mean_marginal_contribution ?? 0) > 0 ? "text-emerald-300" : (card.mean_marginal_contribution ?? 0) < 0 ? "text-red-300" : "text-slate-500"}`}>{decimal(card.mean_marginal_contribution)}</td></tr>)}</tbody></table>{topSpecialists.length === 0 && <div className="px-3 py-10 text-center text-xs text-slate-600">Aguardando forecasts liquidados para formar scorecards.</div>}</div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="flex items-center justify-between"><div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Risco ao vivo</div><h3 className="mt-1 text-lg font-semibold text-white">Estado do Risk Manager</h3></div><StatePill ok={!status?.kill_switch_active} text={status?.kill_switch_active ? "Bloqueado" : "Operacional"} /></div>
        <pre className="mt-4 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-4 text-[11px] leading-5 text-slate-400">{risk ? JSON.stringify(risk, null, 2) : "Aguardando telemetria..."}</pre>
      </section>
    </div>
  );
}
