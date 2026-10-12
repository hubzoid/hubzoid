import { useEffect } from "react";

/** One bounded refresh at a time, only while visible. Always refresh on return. */
export function useVisibleRefresh(reload: () => void, busy: boolean, active = true) {
  useEffect(() => {
    const refresh = () => { if (!busy && document.visibilityState === "visible") reload(); };
    const timer = active ? window.setInterval(refresh, 10000) : undefined;
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [reload, busy, active]);
}
