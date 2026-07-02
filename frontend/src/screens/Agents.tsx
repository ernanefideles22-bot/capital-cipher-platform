import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Agents() {
  const data = usePolling(api.agents);
  return (
    <table className="w-full text-sm text-slate-300">
      <thead>
        <tr className="text-left text-slate-500 border-b border-slate-800">
          <th className="py-2">Name</th><th>Status</th><th>Critical</th><th>Last Signal</th>
          <th>Confidence</th><th>Avg Latency</th><th>Runs</th><th>Failures</th>
        </tr>
      </thead>
      <tbody>
        {data?.agents.map((a) => (
          <tr key={a.name} className="border-b border-slate-900">
            <td className="py-2 font-mono">{a.name}</td>
            <td><span className={a.status === "READY" ? "text-emerald-400" : a.status === "FAILED" || a.status === "TIMEOUT" ? "text-red-400" : "text-slate-400"}>{a.status}</span></td>
            <td>{a.critical ? "yes" : "no"}</td>
            <td>{a.last_signal ?? "—"}</td>
            <td>{a.last_confidence ?? "—"}</td>
            <td>{a.avg_latency_ms.toFixed(1)} ms</td>
            <td>{a.total_runs}</td>
            <td className={a.total_failures > 0 ? "text-red-400" : ""}>{a.total_failures}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
