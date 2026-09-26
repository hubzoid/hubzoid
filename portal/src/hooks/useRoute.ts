import { useCallback, useEffect, useState } from "react";

/**
 * Hash routing so every screen is bookmarkable and the browser's back button
 * works without a server-side router (the SPA is served as static files).
 *
 * Routes:
 *   #/home (the default)
 *   #/agents
 *   #/agents/<key>/access | runs | activity
 *   #/agents/<key>/runs/<workflow>[/<run id>]
 *   #/people[/<subject>]
 *   #/activity
 *   #/confirm/<change request id>   (the link an agent tool hands a manager)
 */
export type Route = { path: string; parts: string[]; query: Record<string, string> };

export const DEFAULT_ROUTE = "/home";

export function href(path: string) {
  return "#" + path;
}

/** Build a hash href with a query string (drops empty values). */
export function hrefWith(path: string, query: Record<string, string | undefined>) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v) q.set(k, v);
  const s = q.toString();
  return "#" + path + (s ? "?" + s : "");
}

export function navigate(path: string) {
  location.hash = path;
}

/** Replace only the query of the current route (filters/pagination), keeping the
 *  path. `replace` avoids stacking a history entry per keystroke. */
export function setQuery(
  path: string,
  query: Record<string, string | undefined>,
  replace = false,
) {
  const url = hrefWith(path, query);
  if (replace) history.replaceState(null, "", url);
  else location.hash = url.slice(1);
}

export const agentHref = (key: string, tab = "access") =>
  href(`/agents/${encodeURIComponent(key)}/${tab}`);

export const personHref = (subject: string) =>
  href(`/people/${encodeURIComponent(subject)}`);

export const confirmHref = (id: string) => href(`/confirm/${encodeURIComponent(id)}`);

function parse(hash: string): Route {
  const raw = hash.replace(/^#/, "") || DEFAULT_ROUTE;
  const qIndex = raw.indexOf("?");
  const path = (qIndex === -1 ? raw : raw.slice(0, qIndex)) || DEFAULT_ROUTE;
  const search = qIndex === -1 ? "" : raw.slice(qIndex + 1);
  const parts = path
    .split("/")
    .slice(1)
    .map((p) => {
      try {
        return decodeURIComponent(p);
      } catch {
        return p;
      }
    });
  const query: Record<string, string> = {};
  for (const [k, v] of new URLSearchParams(search)) query[k] = v;
  return { path, parts, query };
}

/**
 * Read and update the current route's query string (filters + pagination), so a
 * screen's investigation state lives in the URL: refresh, Back and shared links
 * all restore it. Updating pushes a new hash (Back steps through filter states).
 */
export function useHashQuery(): [
  Record<string, string>,
  (next: Record<string, string | undefined>) => void,
] {
  const [query, setQ] = useState<Record<string, string>>(
    () => parse(location.hash).query,
  );
  useEffect(() => {
    const on = () => setQ(parse(location.hash).query);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const update = useCallback((next: Record<string, string | undefined>) => {
    const cur = parse(location.hash);
    setQuery(cur.path, { ...cur.query, ...next });
  }, []);
  return [query, update];
}

/**
 * A screen with unsaved work registers a guard. The router consults it before
 * letting a hash change through: while a request is in flight navigation is
 * refused outright; with a dirty draft the user is asked to confirm discarding.
 */
export type NavigationGuard = {
  dirty: boolean;
  busy: boolean;
  discard: () => void;
};

let guard: NavigationGuard | null = null;

export function useNavigationGuard(state: NavigationGuard | null) {
  useEffect(() => {
    guard = state;
    return () => {
      guard = null;
    };
  }, [state]);
  useEffect(() => {
    if (!state || (!state.dirty && !state.busy)) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [state]);
}

type Blocked = { to: string; reason: "busy" | "dirty"; proceed: () => void };

export function useRoute(onBlocked: (blocked: Blocked) => void) {
  const [route, setRoute] = useState<Route>(() => parse(location.hash));
  useEffect(() => {
    const change = (e: HashChangeEvent) => {
      const next = parse(location.hash);
      if (guard && (guard.busy || guard.dirty)) {
        // Put the URL back where it was; the app state never moved.
        history.replaceState(null, "", e.oldURL);
        const discard = guard.discard;
        onBlocked({
          to: next.path,
          reason: guard.busy ? "busy" : "dirty",
          proceed: () => {
            discard();
            guard = null;
            navigate(next.path);
          },
        });
        return;
      }
      setRoute(next);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, [onBlocked]);
  return route;
}
