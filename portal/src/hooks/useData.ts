import { useCallback, useEffect, useState } from "react";
import { request } from "../api";

export const errorText = (e: unknown) =>
  e instanceof Error ? e.message : String(e);

type Loaded<T> = { path: string; data?: T; error?: string };

/**
 * Fetch one JSON resource and keep it in sync with `path`.
 *
 * Stale-hub protection: until the request for the CURRENT path resolves, the
 * stored record still carries the previous path, so callers see an empty
 * (loading) record rather than another agent's data. Pass `null` to skip.
 */
export function useData<T>(path: string | null) {
  const [state, setState] = useState<Loaded<T>>({ path: "" });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (path === null) return;
    const controller = new AbortController();
    request<T>(path, undefined, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ path, data });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setState({ path, error: errorText(e) });
      });
    return () => controller.abort();
  }, [path, revision]);
  const reload = useCallback(() => setRevision((v) => v + 1), []);
  const current: Partial<Loaded<T>> =
    path !== null && state.path === path ? state : {};
  return {
    data: current.data,
    error: current.error,
    loading: path !== null && current.data === undefined && !current.error,
    reload,
  };
}
