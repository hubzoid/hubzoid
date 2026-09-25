import { useCallback, useEffect, useState } from "react";
import { ApiError, request } from "../api";

export const errorText = (e: unknown) =>
  e instanceof Error ? e.message : String(e);

type Loaded<T> = { path: string; data?: T; error?: string; status?: number; rev: number; at?: number };

/**
 * Fetch one JSON resource and keep it in sync with `path`.
 *
 * Stale-hub protection: until the request for the CURRENT path resolves, the
 * stored record still carries the previous path, so callers see an empty
 * (loading) record rather than another agent's data. Pass `null` to skip.
 *
 * `keepStale` (opt-in): on a *failed refresh of the same path*, keep the last good
 * data and expose the error alongside it, instead of dropping to an error screen.
 * This is only appropriate for read-only views (e.g. Runs auto-refresh): a mutating
 * editor must NOT keep stale rows editable after a failed recovery refresh, so it
 * leaves this off and re-locks on the error. A path change always drops stale data.
 */
export function useData<T>(path: string | null, opts?: { keepStale?: boolean }) {
  const keepStale = opts?.keepStale ?? false;
  const [state, setState] = useState<Loaded<T>>({ path: "", rev: -1 });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (path === null) return;
    const controller = new AbortController();
    request<T>(path, undefined, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted)
          setState({ path, data, rev: revision, at: Date.now() });
      })
      .catch((e) => {
        if (controller.signal.aborted) return;
        setState((s) => {
          const keep = keepStale && s.path === path && s.data !== undefined;
          return {
            path,
            data: keep ? s.data : undefined,
            error: errorText(e),
            status: e instanceof ApiError ? e.status : 0,
            rev: revision,
            at: keep ? s.at : undefined,
          };
        });
      });
    return () => controller.abort();
  }, [path, revision, keepStale]);
  const reload = useCallback(() => setRevision((v) => v + 1), []);
  const current: Partial<Loaded<T>> =
    path !== null && state.path === path ? state : {};
  // A reload() bumps `revision`; keep serving the old data until the response
  // for THIS revision lands. `refreshing` lets callers lock edits meanwhile so
  // nobody edits stale rows during a post-save refetch.
  const refreshing =
    path !== null && state.path === path && state.rev !== revision;
  return {
    data: current.data,
    error: current.error,
    status: current.status,
    loading: path !== null && current.data === undefined && !current.error,
    refreshing,
    at: current.at,
    reload,
  };
}
