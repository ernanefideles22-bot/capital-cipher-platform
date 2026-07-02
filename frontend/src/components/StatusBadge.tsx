const COLORS: Record<string, string> = {
  PAPER: "bg-emerald-900 text-emerald-300 border-emerald-700",
  OFFLINE: "bg-slate-800 text-slate-400 border-slate-600",
  ERROR: "bg-red-900 text-red-300 border-red-700",
  DEGRADED: "bg-amber-900 text-amber-300 border-amber-700",
  LIVE: "bg-red-900 text-red-200 border-red-600",
  LIVE_LOCKED: "bg-red-950 text-red-300 border-red-800",
};

export default function StatusBadge({ value }: { value: string }) {
  const cls = COLORS[value] ?? "bg-slate-800 text-slate-300 border-slate-600";
  return (
    <span className={`px-2 py-0.5 rounded border text-xs font-mono font-bold ${cls}`}>
      {value}
    </span>
  );
}
