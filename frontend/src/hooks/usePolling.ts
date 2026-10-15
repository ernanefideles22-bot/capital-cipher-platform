import { useEffect, useRef, useState } from "react";

export function usePolling<T>(fn: () => Promise<T>, intervalMs = 5000): T | null {
  const [data, setData] = useState<T | null>(null);
  const latestFn = useRef(fn);
  useEffect(() => { latestFn.current = fn; }, [fn]);
  useEffect(() => {
    let active = true;
    let pending = false;
    const tick = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await latestFn.current();
        if (active) setData(result);
      } catch {
        if (active) setData(null);
      } finally {
        pending = false;
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [intervalMs]);
  return data;
}
