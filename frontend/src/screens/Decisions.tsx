import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import type { AuditEvent } from "../types";

const RISK_COLORS: Record<string, string> = {
  APPROVED: "text-emerald-400", REDUCED: "text-amber-400",
  BLOCKED: "text-red-400", KILL_SWITCH: "text-red-500", PENDING: "text-slate-400",
};

export default function Decisions() {
  const data = usePolling(api.decisions, 4000);
  const [chain, setChain] = useState<AuditEvent[] | null>(null);
  const [chainId, setChainId] = useState<string | null>(null);

  const openChain = async (correlationId: string) => {
    const result = await api.auditChain(correlationId);
    setChain(result.chain);
    setChainId(correlationId);
  };

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <table className="w-full text-sm text-slate-300 self-start">
        <thead>
          <tr className="text-left text-slate-500 border-b border-slate-800">
            <th className="py-2">Time</th><th>Symbol</th><th>Action</th>
            <th>Conf.</th><th>Risk</th><th></th>
          </tr>
        </thead>
        <tbody>
          {data?.decisions.map((d) => (
            <tr key={d.decision_id} className="border-b border-slate-900">
              <td className="py-2 font-mono text-xs">{new Date(d.created_at).toLocaleTimeString()}</td>
              <td>{d.symbol}</td>
              <td className="font-bold">{d.candidate_action}</td>
              <td>{d.confidence}</td>
              <td className={RISK_COLORS[d.risk_status] ?? ""}>{d.risk_status}</td>
              <td>
                <button onClick={() => openChain(d.correlation_id)}
                  className="text-sky-400 hover:underline text-xs">chain</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="bg-slate-900 border border-slate-800 rounded-lg p-3 max-h-[70vh] overflow-auto">
        <div className="text-xs uppercase text-slate-500 mb-2">
          Decision chain {chainId ? `(${chainId.slice(0, 8)}…)` : ""}
        </div>
        {chain ? chain.map((event) => (
          <details key={event.audit_id} className="mb-2 border-b border-slate-800 pb-1">
            <summary className="cursor-pointer text-slate-300 text-sm">
              <span className="font-mono text-sky-400">{event.audit_type}</span>{" "}
              <span className="text-slate-500 text-xs">{event.created_at}</span>
            </summary>
            <pre className="text-xs text-slate-400 overflow-auto mt-1">
              {JSON.stringify(event.payload, null, 2)}
            </pre>
          </details>
        )) : <div className="text-slate-600 text-sm">Select a decision to inspect its full chain.</div>}
      </div>
    </div>
  );
}
