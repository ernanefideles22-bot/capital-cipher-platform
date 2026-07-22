import type { ReactNode } from "react";

type Tone = "neutral" | "positive" | "negative" | "warning";

const toneClasses: Record<Tone, string> = {
  neutral: "border-slate-800 bg-slate-900/80",
  positive: "border-emerald-900/80 bg-emerald-950/20",
  negative: "border-red-900/80 bg-red-950/20",
  warning: "border-amber-900/80 bg-amber-950/20",
};

export default function MetricCard({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
}) {
  return (
    <section className={`rounded-xl border p-4 shadow-sm ${toneClasses[tone]}`}>
      <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-slate-500">{label}</p>
      <div className="mt-2 text-2xl font-semibold tracking-tight text-slate-100">{value}</div>
      {detail && <div className="mt-1 text-xs text-slate-500">{detail}</div>}
    </section>
  );
}
