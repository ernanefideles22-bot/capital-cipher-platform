import { useState } from "react";
import { api } from "../services/api";

export default function KillSwitchButton({ active }: { active: boolean }) {
  const [busy, setBusy] = useState(false);

  const trigger = async () => {
    const reason = window.prompt("Kill switch reason (this is audited):");
    if (!reason) return;
    const apiKey = window.prompt("Admin API key:");
    if (!apiKey) return;
    setBusy(true);
    await api.killSwitch(reason, apiKey);
    setBusy(false);
    window.location.reload();
  };

  if (active) {
    return (
      <div className="px-3 py-1.5 bg-red-950 border border-red-600 text-red-300 rounded font-bold text-sm animate-pulse">
        SYSTEM LOCKED — KILL SWITCH ACTIVE
      </div>
    );
  }
  return (
    <button
      onClick={trigger}
      disabled={busy}
      className="px-3 py-1.5 bg-red-800 hover:bg-red-700 text-white rounded font-bold text-sm border border-red-600"
    >
      KILL SWITCH
    </button>
  );
}
