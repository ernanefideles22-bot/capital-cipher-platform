import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Audit() {
  const data = usePolling(api.auditEvents, 5000);
  const [filter, setFilter] = useState("");
  const events = data?.events.filter(
    (e) =>
      !filter ||
      e.audit_type.toLowerCase().includes(filter.toLowerCase()) ||
      e.correlation_id.includes(filter)
  );
  return (
    <div>
      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by audit_type or correlation_id…"
        className="bg-slate-800 border border-slate-700 rounded px-3 py-1.5 text-sm text-slate-200 w-96 mb-3"
      />
      <div className="space-y-2 max-h-[70vh] overflow-auto">
        {events?.slice().reverse().map((event) => (
          <details key={event.audit_id} className="bg-slate-900 border border-slate-800 rounded p-2">
            <summary className="cursor-pointer text-sm text-slate-300">
              <span className="font-mono text-sky-400">{event.audit_type}</span>{" "}
              <span className="text-slate-500 text-xs">
                {event.created_at} · corr {event.correlation_id.slice(0, 8)}…
              </span>
            </summary>
            <pre className="text-xs text-slate-400 overflow-auto mt-2">
              {JSON.stringify(event.payload, null, 2)}
            </pre>
          </details>
        ))}
      </div>
    </div>
  );
}
