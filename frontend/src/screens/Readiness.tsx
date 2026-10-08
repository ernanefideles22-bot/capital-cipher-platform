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

export default function Readiness() {
  const status = usePolling(api.status, 5000);
  const risk = usePolling(api.risk, 5000);
  const agentsData = usePolling(api.agents, 5000);
  const rankingData = usePolling(api.agentRanking, 10000);
  const paperData = usePolling(api.paperOrders, 5000);

  const agents = agentsData?.agents ?? [];
  const readyAgents = agents.filter((agent) => agent.status === "READY").length;
  const failedAgents = agents.filter((agent) => ["FAILED", "TIMEOUT"].includes(agent.status)).length;
  const orders = paperData?.orders ?? [];
  const ranking = rankingData?.ranking ?? [];

  const topSpecialists = useMemo(() => ranking.slice(0, 8), [ranking]);

  const runtimeHealthy = Boolean(status) && status?.mode === "PAPER" && !status?.kill_switch_active;
  const marketHealthy = status?.market_data === "CONNECTED";

  return (
    <div className="space-y-6">
      <section className="flex flex-col justify-between gap-4 xl:flex-row xl:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-cyan-400">Control Room</p>
          <h2 className="mt-2 text-2xl font-semibold text-white">Readiness e desenvolvimento dos testes</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">Telemetria real do runtime PAPER e registro visual da última baseline validada. Esta tela não habilita TESTNET nem LIVE.</p>
        </div>
        <div className="flex flex-wrap gap-2"><StatePill ok={runtimeHealthy} text={runtimeHealthy ? "PAPER saudável" : "Verificar runtime"} /><StatePill ok={marketHealthy} text={marketHealthy ? "Market data conectado" : "Market data pendente"} /><StatePill ok text={`CI ${CI_RUN} aprovado`} /></div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Modo</div><div className="mt-2 text-2xl font-semibold text-cyan-200">{status?.mode ?? "—"}</div><div className="mt-2 text-xs text-slate-600">Execução externa permanece desabilitada</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Agentes prontos</div><div className="mt-2 text-2xl font-semibold text-emerald-300">{readyAgents}<span className="text-sm font-normal text-slate-600"> / {agents.length || 300}</span></div><div className="mt-2 text-xs text-slate-600">Falhas/timeout: {failedAgents}</div></article>
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

      <section className="grid gap-5 xl:grid-cols-2">
        <article className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
          <div className="flex items-center justify-between"><div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Risco ao vivo</div><h3 className="mt-1 text-lg font-semibold text-white">Estado do Risk Manager</h3></div><StatePill ok={!status?.kill_switch_active} text={status?.kill_switch_active ? "Bloqueado" : "Operacional"} /></div>
          <pre className="mt-4 max-h-64 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-4 text-[11px] leading-5 text-slate-400">{risk ? JSON.stringify(risk, null, 2) : "Aguardando telemetria..."}</pre>
        </article>

        <article className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
          <div><div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Próxima fase</div><h3 className="mt-1 text-lg font-semibold text-white">Especialistas — ranking observado</h3><p className="mt-1 text-xs leading-5 text-slate-600">Este quadro usa o ranking já fornecido pelo backend. Ele será a base visual para acompanhar promoção, perda de influência e comportamento por regime conforme implementarmos a próxima fase.</p></div>
          <div className="mt-4 overflow-hidden rounded-lg border border-slate-800"><table className="w-full text-xs"><thead className="bg-slate-900 text-left text-[10px] uppercase tracking-wider text-slate-600"><tr><th className="px-3 py-2">#</th><th>Agente</th><th className="pr-3 text-right">Score</th></tr></thead><tbody>{topSpecialists.map((row, index) => <tr key={`${row.agent_name}-${index}`} className="border-t border-slate-900 text-slate-300"><td className="px-3 py-2 text-slate-600">{index + 1}</td><td className="font-mono">{row.agent_name}</td><td className="pr-3 text-right font-mono text-cyan-300">{typeof row.score === "number" ? row.score.toFixed(4) : "—"}</td></tr>)}</tbody></table>{topSpecialists.length === 0 && <div className="px-3 py-8 text-center text-xs text-slate-600">Aguardando ranking dos agentes.</div>}</div>
        </article>
      </section>
    </div>
  );
}
