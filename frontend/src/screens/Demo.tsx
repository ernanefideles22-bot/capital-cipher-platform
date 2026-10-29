import { useEffect, useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { demoApi, type DemoSnapshot, type DemoAnalysis } from "../services/demo";

const money = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
const time = (value: string | undefined) => value ? new Date(value).toLocaleString("pt-BR") : "—";

export default function Demo() {
  const status = usePolling(demoApi.status, 5000);
  const [adminKey, setAdminKey] = useState("");
  const [activeKey, setActiveKey] = useState("");
  const [snapshot, setSnapshot] = useState<DemoSnapshot | null>(null);
  const [analysis, setAnalysis] = useState<DemoAnalysis | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("ETHUSDT");
  const [timeframe, setTimeframe] = useState("15m");

  useEffect(() => {
    if (!activeKey) return;
    let active = true;
    let pending = false;
    async function refresh() {
      if (pending) return;
      pending = true;
      try {
        const next = await demoApi.snapshot(activeKey);
        if (active) { setSnapshot(next); setPollError(null); }
      } catch (cause) {
        if (active) { setSnapshot(null); setPollError(cause instanceof Error ? cause.message : "Falha na consulta Demo"); }
      } finally { pending = false; }
    }
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [activeKey]);

  async function act(name: string, operation: () => Promise<unknown>) {
    if (busy || !activeKey) return;
    setBusy(name); setError(null); setNotice(null);
    try {
      const result = await operation();
      if (name === "analysis") setAnalysis(result as DemoAnalysis);
      else setNotice(name === "canary" ? "Teste operacional concluído: compra e venda protegidas, conta sem posições ao final." : "Ação confirmada pelo executor Demo.");
      setSnapshot(await demoApi.snapshot(activeKey));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Ação não confirmada. Confira o histórico antes de repetir."); }
    finally { setBusy(null); }
  }

  const execution = snapshot?.execution;
  const trades = execution?.trades ?? [];
  const current = analysis ?? execution?.analysis;
  const primary = new Set(current?.primary_agents ?? []);
  const outputs = current?.agents ?? [];
  const proposed = current?.risk?.approved && ["BUY", "SELL"].includes(current.decision.candidate_action);
  const ready = !!status?.authenticated && !!execution && !execution.halted;

  return <div className="space-y-6">
    <section className="flex flex-wrap items-end justify-between gap-4">
      <div><p className="text-xs font-semibold uppercase tracking-[.18em] text-amber-400">Binance · USD-M Futures</p><h2 className="mt-2 text-2xl font-semibold text-white">Futuros Demo</h2><p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">Conta de testes com fundos virtuais. Saldo, posições, decisões e ordens deste painel pertencem à Binance Demo.</p></div>
      <span role="status" className={`rounded-full border px-3 py-1.5 text-xs ${status?.authenticated ? "border-emerald-800 bg-emerald-950/30 text-emerald-300" : "border-amber-800 text-amber-300"}`}>{status?.authenticated ? "Autenticação confirmada" : status ? "Conexão pendente" : "Consultando conexão…"}</span>
    </section>
    {!activeKey && <form onSubmit={e => { e.preventDefault(); setActiveKey(adminKey.trim()); setAdminKey(""); }} className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <label htmlFor="demo-admin" className="text-sm font-medium text-slate-200">Acesso administrativo do Capital Cipher</label><p className="mt-1 text-xs leading-5 text-slate-500">Use a chave administrativa do projeto para consultar a conta e operar a Demo. Não insira a API Key da Binance. A chave fica apenas nesta tela, em memória.</p>
      <div className="mt-4 flex flex-col gap-3 sm:flex-row"><input id="demo-admin" type="password" autoComplete="off" value={adminKey} onChange={e => setAdminKey(e.target.value)} required className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"/><button className="rounded-lg bg-cyan-700 px-5 py-2 text-sm font-semibold text-white hover:bg-cyan-600">Abrir conta Demo</button></div>
    </form>}
    {(error || pollError) && <div role="alert" className="rounded-lg border border-red-800 bg-red-950/20 p-4 text-sm text-red-300">{error || pollError}</div>}
    {notice && <div role="status" className="rounded-lg border border-emerald-800 bg-emerald-950/20 p-4 text-sm text-emerald-300">{notice}</div>}
    {activeKey && <>
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-400"><span>Última verificação: {time(status?.checked_at)}</span><button onClick={() => { setActiveKey(""); setSnapshot(null); setAnalysis(null); }} className="rounded border border-slate-700 px-3 py-1.5">Bloquear painel</button></div>
      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[
        ["Patrimônio virtual · USDT", money(snapshot?.account.equity_usdt)],
        ["Disponível · USDT", money(snapshot?.account.available_usdt)],
        ["PnL aberto · USDT", money(snapshot?.account.unrealized_pnl_usdt)],
        ["Automação Demo", execution ? execution.automatic_enabled ? "Ativa" : "Pausada" : "—"],
      ].map(([label,value]) => <div key={label} className="rounded-xl border border-slate-800 bg-slate-900/50 p-5"><p className="text-xs text-slate-500">{label}</p><p className="mt-3 text-2xl font-semibold text-white">{value}</p></div>)}</section>
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5"><h3 className="font-semibold text-white">Controle da execução</h3><p className="mt-2 text-xs leading-6 text-slate-400">Automação: ETHUSDT / 15m · Alavancagem 1× · Uma posição por vez · Até 200 USDT por ordem · Risco máximo de 0,1% do patrimônio por entrada · Stop-loss e take-profit na Binance.</p>
        <p className="mt-2 text-xs text-amber-300">{execution?.halted ? "Executor bloqueado. Consulte os eventos e reconcilie a conta." : execution?.canary_verified ? "Teste operacional aprovado. A rentabilidade dos agentes ainda precisa de avaliação." : "A automação depende do teste operacional de compra, venda e encerramento. O teste usa até 50 USDT virtuais por entrada."}</p>
        <div className="mt-4 flex flex-wrap gap-2">
          <button disabled={!!busy || !ready} onClick={() => void act("canary", () => demoApi.action("canary", activeKey))} className="rounded-lg border border-amber-700 px-3 py-2 text-xs text-amber-200 disabled:opacity-40">{busy === "canary" ? "Testando…" : "Testar ciclo com fundos virtuais"}</button>
          <button disabled={!!busy || !execution?.canary_verified || execution.halted} onClick={() => void act("automatic", () => demoApi.action("automatic", activeKey, { enabled: !execution?.automatic_enabled }))} className="rounded-lg bg-cyan-700 px-3 py-2 text-xs font-semibold disabled:opacity-40">{execution?.automatic_enabled ? "Pausar automação" : "Iniciar automação Demo"}</button>
          <button disabled={!!busy || !execution} onClick={() => void act("reconcile", () => demoApi.action("reconcile", activeKey))} className="rounded-lg border border-slate-700 px-3 py-2 text-xs disabled:opacity-40">Reconciliar conta</button>
          <button disabled={!!busy || !execution?.halted} onClick={() => void act("reset", () => demoApi.action("reset", activeKey))} className="rounded-lg border border-slate-700 px-3 py-2 text-xs disabled:opacity-40">Revalidar bloqueio técnico</button>
          <button disabled={!!busy || !execution} onClick={() => void act("kill", () => demoApi.action("kill", activeKey))} className="rounded-lg border border-red-600 bg-red-950 px-3 py-2 text-xs font-bold text-red-200 disabled:opacity-40">Parar e encerrar posições Demo</button>
        </div>{busy && <p role="status" className="mt-3 text-xs text-cyan-300">Processando {busy}… Aguarde a confirmação do executor antes de repetir.</p>}
      </section>
      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-5"><h3 className="font-semibold text-white">Agentes e decisão</h3><p className="mt-2 text-xs leading-6 text-slate-400">A mesma coorte de 300 agentes do projeto analisa candles fechados de Futuros Demo. Os três principais consolidam a decisão; especialistas continuam em shadow. Fontes externas ausentes são indicadas como indisponíveis.</p>
        <div className="mt-4 flex flex-wrap gap-3"><label className="text-xs text-slate-400">Ativo<select value={symbol} onChange={e => setSymbol(e.target.value)} className="ml-2 rounded border border-slate-700 bg-slate-950 px-2 py-2">{["ETHUSDT","BTCUSDT","SOLUSDT"].map(s => <option key={s}>{s}</option>)}</select></label><label className="text-xs text-slate-400">Período<select value={timeframe} onChange={e => setTimeframe(e.target.value)} className="ml-2 rounded border border-slate-700 bg-slate-950 px-2 py-2">{["1m","5m","15m","1h","4h"].map(s => <option key={s}>{s}</option>)}</select></label>
          <button disabled={!!busy || !ready} onClick={() => void act("analysis", () => demoApi.action("analysis", activeKey, { symbol, timeframe }))} className="rounded bg-cyan-700 px-4 py-2 text-xs font-semibold disabled:opacity-40">Analisar com agentes</button>
          <button disabled={!!busy || !ready || !proposed || !current} onClick={() => current && void act("execute", () => demoApi.action("execute", activeKey, { proposal_id: current.id }))} className="rounded border border-amber-700 px-4 py-2 text-xs text-amber-200 disabled:opacity-40">Executar proposta na Demo</button>
        </div>
        {current && <div className="mt-5 rounded-lg border border-slate-800 p-4"><p className="text-sm text-cyan-200">{current.decision.candidate_action} · Confiança {current.decision.confidence}% · {current.symbol} / {current.timeframe}</p><p className="mt-2 text-xs text-slate-400">{current.decision.reason}</p><p className="mt-2 text-xs text-slate-500">Risco: {current.risk?.approved ? "aprovado" : current.risk?.reason ?? "sem proposta operacional"} · {current.total_agents} agentes · Candle: {time(current.candle_closed_at)}</p></div>}
        <div className="mt-4 max-h-80 overflow-auto rounded-lg border border-slate-800"><table className="w-full text-left text-xs"><caption className="sr-only">Resultados dos agentes para o último snapshot de Futuros Demo</caption><thead className="sticky top-0 bg-slate-900 text-slate-400"><tr>{["Agente","Papel","Status","Sinal","Confiança"].map(h => <th key={h} className="px-3 py-3">{h}</th>)}</tr></thead><tbody>{outputs.map(o => <tr key={o.agent_name} className="border-t border-slate-800/70"><td className="max-w-64 px-3 py-2 font-mono text-slate-300" title={o.reason}>{o.agent_name}</td><td className="px-3 py-2 text-slate-500">{primary.has(o.agent_name) ? "Principal" : "Shadow"}</td><td className="px-3 py-2 text-slate-400">{o.status}</td><td className="px-3 py-2 text-cyan-200">{o.signal}</td><td className="px-3 py-2">{o.confidence}%</td></tr>)}</tbody></table>{outputs.length === 0 && <p className="p-4 text-xs text-slate-500">Execute uma análise para consultar os agentes.</p>}</div>
      </section>
      <section className="rounded-xl border border-slate-800 p-5"><h3 className="font-semibold text-white">Posições e histórico Demo</h3><div className="mt-4 overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="text-slate-500"><tr>{["Ativo / direção","Estado","Quantidade","Entrada","Stop-loss","Take-profit","PnL líquido · USDT","Ação"].map(h => <th key={h} className="px-3 py-3">{h}</th>)}</tr></thead><tbody>{trades.map(t => <tr key={t.id} className="border-t border-slate-800"><td className="px-3 py-3">{t.symbol} · {t.side}</td><td className="px-3 py-3 text-amber-200">{t.state}</td><td className="px-3 py-3">{t.quantity}</td><td className="px-3 py-3">{money(t.entry_price)}</td><td className="px-3 py-3">{t.stop_loss}</td><td className="px-3 py-3">{t.take_profit}</td><td className="px-3 py-3">{money(t.realized_pnl_usdt)}</td><td className="px-3 py-3">{!["CLOSED","REJECTED"].includes(t.state) && <button disabled={!!busy} onClick={() => void act("close", () => demoApi.action("close", activeKey, { proposal_id: t.id }))} className="rounded border border-red-800 px-3 py-1 text-red-300 disabled:opacity-40">Encerrar</button>}</td></tr>)}</tbody></table>{trades.length === 0 && <p className="p-4 text-xs text-slate-500">Nenhuma ordem Demo registrada neste executor.</p>}</div>
        <p className="mt-3 text-xs text-slate-500">Posições observadas na Binance: {snapshot?.account.positions.length ?? "—"} · PnL líquido aparece após confirmação dos fills e taxas.</p>
      </section>
      <section className="rounded-xl border border-slate-800 p-5"><h3 className="font-semibold">Eventos do executor</h3><div className="mt-3 max-h-64 overflow-auto">{execution?.events.map(e => <p key={e.seq} className="border-b border-slate-800 py-2 font-mono text-[11px] text-slate-400">{time(e.time)} · {e.kind}</p>)}</div></section>
    </>}
  </div>;
}
