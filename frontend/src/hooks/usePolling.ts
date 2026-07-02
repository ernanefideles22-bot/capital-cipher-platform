import { useEffect, useState } from "react";

export function usePolling<T>(fn: () => Promise<T>, intervalMs = 5000): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let active = true;
    const tick = async () => {
      try {
        const result = await fn();
        if (active) setData(result);
      } catch {
        /* surfaced via UI empty states; errors must not crash the dashboard */
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      active = false;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return data;
}
