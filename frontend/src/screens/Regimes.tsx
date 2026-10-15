import { useMemo, useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import type { RegimeShadowRow } from "../types";

function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function decimal(value: number): string {
  return value.toFixed(4);
}

const REGIME_LABELS: Record<string, string> = {
  BULL_TREND: "Bull trend",
  BEAR_TREND: "Bear trend",
  RANGE: "Range",
  HIGH_VOLATILITY: "Alta volatilidade",
  LOW_VOLATILITY: "Baixa volatilidade",
  UNDEFINED: "Indefinido",
};

const STATUS_LABELS: Record<RegimeShadowRow["status"], string> = {
  COLLECTING: "COLETANDO",
  SHADOW_SPECIALIST: "ESPECIALISTA SHADOW",
  OBSERVED_NOT_QUALIFIED: "NÃO QUALIFICADO",
};

function criterion(ok: boolean, label: string) {
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[9px] ${ok ? "border-emerald-900 bg-emerald-950/20 text-emerald-300" : "border-slate-800 bg-slate-950 text-slate-600"}`}>
      {label}
    </span>
  );
}

export default function Regimes() {
  const data = usePolling(api.regimeShadow, 10000);
  const rows = data?.rows ?? [];
  const [regimeFilter, setRegimeFilter] = useState("ALL");
  const [statusFilter, setStatusFilter] = useState("ALL");

  const filteredRows = useMemo(() => {
    return [...rows]
      .filter((row) => regimeFilter === "ALL" || row.market_regime === regimeFilter)
      .filter((row) => statusFilter === "ALL" || row.status === statusFilter)
      .sort((a, b) => {
        if (a.market_regime !== b.market_regime) return a.market_regime.localeCompare(b.market_regime);
        return a.rank_within_regime - b.rank_within_regime;
      });
  }, [rows, regimeFilter, statusFilter]);

  const grouped = useMemo(() => {
    const map = new Map<string, RegimeShadowRow[]>();
    for (const row of rows) {
      const bucket = map.get(row.agent_name) ?? [];
      bucket.push(row);
      map.set(row.agent_name, bucket);
    }
    return [...map.entries()].map(([agent, agentRows]) => {
      const ranked = [...agentRows].sort((a, b) => {
        if (a.qualified_shadow_specialist !== b.qualified_shadow_specialist) {
          return a.qualified_shadow_specialist ? -1 : 1;
        }
        if ((a.accuracy ?? -1) !== (b.accuracy ?? -1)) {
          return (b.accuracy ?? -1) - (a.accuracy ?? -1);
        }
        return b.sample_count - a.sample_count;
      });
      return [agent, ranked] as const;
    });
  }, [rows]);

  return (
    <div className="space-y-6">
      <section className="flex flex-col justify-between gap-3 xl:flex-row xl:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-400">Regime shadow</p>
          <h2 className="mt-2 text-2xl font-semibold text-white">Especialistas por regime de mercado</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-slate-500">
            Acompanhamento observacional dos candidatos usando o mesmo classificador do TrendAgent e somente candles disponíveis até o instante de cada previsão. Nenhum resultado desta tela altera pesos, Risk Manager ou OMS.
          </p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-full border border-violet-900 bg-violet-950/30 px-3 py-1.5 text-violet-300">Candidatos: {data?.candidate_count ?? 0}</span>
          <span className="rounded-full border border-emerald-900 bg-emerald-950/30 px-3 py-1.5 text-emerald-300">Sem autoridade de decisão</span>
          <span className="rounded-full border border-cyan-900 bg-cyan-950/30 px-3 py-1.5 text-cyan-300">Look-ahead protegido</span>
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <article className="rounded-xl border border-emerald-900/60 bg-emerald-950/10 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-emerald-500">Especialistas shadow</div>
          <div className="mt-2 text-3xl font-semibold text-emerald-300">{data?.shadow_specialist_count ?? 0}</div>
          <div className="mt-2 text-xs text-slate-600">Agente × regime que já cumpre todos os critérios</div>
        </article>
        <article className="rounded-xl border border-amber-900/60 bg-amber-950/10 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-amber-500">Coletando</div>
          <div className="mt-2 text-3xl font-semibold text-amber-300">{data?.collecting_count ?? 0}</div>
          <div className="mt-2 text-xs text-slate-600">Ainda abaixo de {data?.qualification.minimum_samples ?? 30} outcomes no regime</div>
        </article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Observados não qualificados</div>
          <div className="mt-2 text-3xl font-semibold text-slate-300">{data?.observed_not_qualified_count ?? 0}</div>
          <div className="mt-2 text-xs text-slate-600">Amostra suficiente, mas falhou em pelo menos um critério</div>
        </article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4">
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Histórico indisponível</div>
          <div className="mt-2 text-3xl font-semibold text-slate-300">{data?.unavailable_historical_forecasts ?? 0}</div>
          <div className="mt-2 text-xs text-slate-600">Forecasts fora da janela retida de candles</div>
        </article>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Critérios de promoção shadow</div>
            <div className="mt-2 flex flex-wrap gap-2 text-[10px]">
              <span className="rounded border border-slate-800 px-2 py-1 text-slate-400">n ≥ {data?.qualification.minimum_samples ?? 30}</span>
              <span className="rounded border border-slate-800 px-2 py-1 text-slate-400">accuracy &gt; {pct(data?.qualification.accuracy_above ?? 0.5)}</span>
              <span className="rounded border border-slate-800 px-2 py-1 text-slate-400">Brier &lt; {decimal(data?.qualification.brier_below ?? 0.25)}</span>
              <span className="rounded border border-slate-800 px-2 py-1 text-slate-400">contribuição marginal &gt; 0</span>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <select value={regimeFilter} onChange={(event) => setRegimeFilter(event.target.value)} className="rounded-md border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-300">
              <option value="ALL">Todos os regimes</option>
              {Object.entries(REGIME_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="rounded-md border border-slate-800 bg-slate-950 px-3 py-2 text-xs text-slate-300">
              <option value="ALL">Todos os status</option>
              <option value="SHADOW_SPECIALIST">Especialista shadow</option>
              <option value="COLLECTING">Coletando</option>
              <option value="OBSERVED_NOT_QUALIFIED">Não qualificado</option>
            </select>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="min-w-[1320px] w-full text-xs">
            <thead className="bg-slate-900 text-left text-[10px] uppercase tracking-wider text-slate-600">
              <tr>
                <th className="px-3 py-2">Rank</th><th>Agente</th><th>Regime</th><th>Progresso</th><th>Amostras</th><th>Acurácia</th><th>Brier</th><th>Contribuição</th><th>Critérios</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {filteredRows.map((row) => (
                <tr key={`${row.agent_name}-${row.agent_version}-${row.market_regime}`} className="border-t border-slate-900 text-slate-300">
                  <td className="px-3 py-3 font-mono text-slate-500">#{row.rank_within_regime}</td>
                  <td className="font-mono text-cyan-100">{row.agent_name}<span className="ml-2 text-[10px] text-slate-700">v{row.agent_version}</span></td>
                  <td>{REGIME_LABELS[row.market_regime] ?? row.market_regime}</td>
                  <td>
                    <div className="w-32">
                      <div className="mb-1 flex justify-between text-[9px] text-slate-600"><span>{row.progress_percent.toFixed(0)}%</span><span>{row.minimum_samples}</span></div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-slate-900"><div className="h-full bg-violet-500" style={{ width: `${row.progress_percent}%` }} /></div>
                    </div>
                  </td>
                  <td>{row.sample_count} / {row.minimum_samples}</td>
                  <td className={(row.accuracy ?? 0) > 0.5 ? "text-emerald-300" : "text-slate-300"}>{pct(row.accuracy)}</td>
                  <td className={row.mean_brier_loss < 0.25 ? "font-mono text-emerald-300" : "font-mono"}>{decimal(row.mean_brier_loss)}</td>
                  <td className={`font-mono ${row.mean_marginal_contribution > 0 ? "text-emerald-300" : row.mean_marginal_contribution < 0 ? "text-red-300" : "text-slate-500"}`}>{decimal(row.mean_marginal_contribution)}</td>
                  <td><div className="flex max-w-[260px] flex-wrap gap-1">{criterion(row.criteria.minimum_sample_reached, "amostra")}{criterion(row.criteria.accuracy_above_50_percent, "accuracy")}{criterion(row.criteria.brier_below_random_baseline, "Brier")}{criterion(row.criteria.positive_marginal_contribution, "marginal")}</div></td>
                  <td><span className={row.status === "SHADOW_SPECIALIST" ? "text-emerald-300" : row.status === "COLLECTING" ? "text-amber-300" : "text-red-300"}>{STATUS_LABELS[row.status]}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {filteredRows.length === 0 && <div className="px-4 py-12 text-center text-sm text-slate-600">Nenhum resultado disponível para este filtro. O painel será preenchido conforme os forecasts forem liquidados.</div>}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Melhor regime observado por candidato</div>
        <div className="mt-4 grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
          {grouped.map(([agent, agentRows]) => {
            const best = agentRows[0];
            return (
              <article key={agent} className={`rounded-lg border p-4 ${best?.qualified_shadow_specialist ? "border-emerald-900/60 bg-emerald-950/10" : "border-slate-800 bg-slate-950/60"}`}>
                <div className="flex items-start justify-between gap-3">
                  <div><div className="font-mono text-xs text-cyan-100">{agent}</div><div className="mt-1 text-[10px] text-slate-600">{best ? REGIME_LABELS[best.market_regime] ?? best.market_regime : "Sem regime"}</div></div>
                  {best && <span className={`text-[9px] font-semibold ${best.qualified_shadow_specialist ? "text-emerald-300" : "text-amber-300"}`}>{STATUS_LABELS[best.status]}</span>}
                </div>
                {best && <div className="mt-4 grid grid-cols-3 gap-3 text-[10px]"><div><div className="text-slate-600">Accuracy</div><div className="mt-1 font-mono text-slate-300">{pct(best.accuracy)}</div></div><div><div className="text-slate-600">Amostras</div><div className="mt-1 font-mono text-slate-300">{best.sample_count}</div></div><div><div className="text-slate-600">Rank</div><div className="mt-1 font-mono text-slate-300">#{best.rank_within_regime}</div></div></div>}
              </article>
            );
          })}
          {grouped.length === 0 && <div className="text-sm text-slate-600">Ainda não há histórico de regime para os candidatos atuais.</div>}
        </div>
      </section>
    </div>
  );
}
