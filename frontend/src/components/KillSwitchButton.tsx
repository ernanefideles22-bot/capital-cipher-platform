import { useEffect, useState } from "react";
import { useI18n } from "../i18n";
import { api } from "../services/api";

export default function KillSwitchButton({ active }: { active: boolean }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => { if (active) setConfirmed(false); }, [active]);

  const trigger = async () => {
    const reason = window.prompt(t("killSwitchReason"));
    if (!reason) return;
    const apiKey = window.prompt(t("adminApiKey"));
    if (!apiKey) return;
    setBusy(true);
    setError(null);
    try {
      await api.killSwitch(reason, apiKey);
      setConfirmed(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("requestFailed"));
    } finally {
      setBusy(false);
    }
  };

  if (active || confirmed) {
    return (
      <div className="px-3 py-1.5 bg-red-950 border border-red-600 text-red-300 rounded font-bold text-sm animate-pulse">
        {t("systemLocked")}
      </div>
    );
  }
  return (
    <div>
      <button
      onClick={trigger}
      disabled={busy}
      className="px-3 py-1.5 bg-red-800 hover:bg-red-700 text-white rounded font-bold text-sm border border-red-600"
    >
      {busy ? "…" : t("triggerKillSwitch")}
      </button>
      {error && <p role="alert" className="mt-2 max-w-xs text-xs text-red-300">{error}</p>}
    </div>
  );
}
