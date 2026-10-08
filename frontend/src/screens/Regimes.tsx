import { useMemo } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

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

export default function Regimes() {
  const data = usePolling(api.regimeShadow, 10000);
  const rows = data?.rows ?? [];

  const grouped = useMemo(() => {
    const map = new Map<string, typeof rows>();
    for (const row of rows) {
      const bucket = map.get(row.agent_name) ?? [];
      bucket.push(row);
      map.set(row.agent_name, bucket);
    }
    return [...map.entries()];
  }, [rows]);

  return (
    <div className="space-y-6">
      <section className="flex flex-col justify-between gap-3 xl:flex-row xl:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-violet-400">Regime shadow</p>
          <h2 className="mt-2 text-2xl font-semibold text-white">Especialistas por regime de mercado</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-slate-500">
            Performance dos candidatos usando o mesmo classificador determinístico do TrendAgent. Somente candles fechados até o instante da previsão entram na classificação histórica.
          </p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded-full border border-violet-900 bg-violet-950/30 px-3 py-1.5 text-violet-300">Candidatos: {data?.candidate_count ?? 0}</span>
          <span className="rounded-full border border-emerald-900 bg-emerald-950/30 px-3 py-1.5 text-emerald-300">Sem autoridade de decisão</span>
          <span className="rounded-full border border-cyan-900 bg-cyan-950/30 px-3 py-1.5 text-cyan-300">Look-ahead protegido</span>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Linhas avaliadas</div><div className="mt-2 text-2xl font-semibold text-white">{rows.length}</div><div className="mt-2 text-xs text-slate-600">Agente × regime com outcome liquidado</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Histórico indisponível</div><div className="mt-2 text-2xl font-semibold text-amber-300">{data?.unavailable_historical_forecasts ?? 0}</div><div className="mt-2 text-xs text-slate-600">Forecasts fora da janela retida de candles</div></article>
        <article className="rounded-xl border border-slate-800 bg-slate-950/50 p-4"><div className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Critério mínimo por regime</div><div className="mt-2 text-2xl font-semibold text-cyan-200">30</div><div className="mt-2 text-xs text-slate-600">Mesmo mínimo da avaliação geral</div></article>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="min-w-[980px] w-full text-xs">
            <thead className="bg-slate-900 text-left text-[10px] uppercase tracking-wider text-slate-600">
              <tr><th className="px-3 py-2">Agente</th><th>Regime</th><th>Amostras</th><th>Acurácia</th><th>Brier</th><th>Contribuição marginal</th><th>Status</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.agent_name}-${row.agent_version}-${row.market_regime}`} className="border-t border-slate-900 text-slate-300">
                  <td className="px-3 py-2 font-mono text-cyan-100">{row.agent_name}<span className="ml-2 text-[10px] text-slate-700">v{row.agent_version}</span></td>
                  <td>{REGIME_LABELS[row.market_regime] ?? row.market_regime}</td>
                  <td>{row.sample_count} / {row.minimum_samples}</td>
                  <td className={(row.accuracy ?? 0) > 0.5 ? "text-emerald-300" : "text-slate-300"}>{pct(row.accuracy)}</td>
                  <td className="font-mono">{decimal(row.mean_brier_loss)}</td>
                  <td className={`font-mono ${row.mean_marginal_contribution > 0 ? "text-emerald-300" : row.mean_marginal_contribution < 0 ? "text-red-300" : "text-slate-500"}`}>{decimal(row.mean_marginal_contribution)}</td>
                  <td><span className={row.sample_sufficient ? "text-emerald-300" : "text-amber-300"}>{row.sample_sufficient ? "AMOSTRA SUFICIENTE" : "COLETANDO"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <div className="px-4 py-12 text-center text-sm text-slate-600">Nenhum candidato possui histórico por regime disponível ainda. O painel será preenchido conforme os forecasts forem liquidados.</div>}
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
        <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Cobertura por candidato</div>
        <div className="mt-4 grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
          {grouped.map(([agent, agentRows]) => (
            <article key={agent} className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
              <div className="font-mono text-xs text-cyan-100">{agent}</div>
              <div className="mt-3 space-y-2">{agentRows.map((row) => <div key={row.market_regime} className="flex items-center justify-between gap-3 text-xs"><span className="text-slate-500">{REGIME_LABELS[row.market_regime] ?? row.market_regime}</span><span className="font-mono text-slate-300">{pct(row.accuracy)} · n={row.sample_count}</span></div>)}</div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
