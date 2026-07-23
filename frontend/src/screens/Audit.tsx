import { useState } from "react";
import { useI18n } from "../i18n";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";

export default function Audit() {
  const { t } = useI18n();
  const data = usePolling(api.auditEvents, 5000);
  const [filter, setFilter] = useState("");
  const events = data?.events.filter((event) => !filter || event.audit_type.toLowerCase().includes(filter.toLowerCase()) || event.correlation_id.includes(filter));
  return <div className="space-y-4"><section><p className="text-xs font-medium uppercase tracking-[0.18em] text-cyan-400">{t("records")}</p><h2 className="mt-2 text-2xl font-semibold text-white">{t("audit")}</h2><p className="mt-1 text-sm text-slate-500">{t("descAudit")}</p></section><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t("filterAudit")} className="w-full max-w-xl rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600" /><div className="space-y-2 overflow-auto"><div className="sr-only">{events?.length ?? 0}</div>{events?.slice().reverse().map((event) => <details key={event.audit_id} className="rounded-xl border border-slate-800 bg-slate-900/70 p-3"><summary className="cursor-pointer text-sm text-slate-300"><span className="font-mono text-sky-400">{event.audit_type}</span> <span className="text-xs text-slate-500">{event.created_at} · {t("correlation")} {event.correlation_id.slice(0, 8)}…</span></summary><pre className="mt-3 overflow-auto text-xs text-slate-400">{JSON.stringify(event.payload, null, 2)}</pre></details>)}</div></div>;
}
