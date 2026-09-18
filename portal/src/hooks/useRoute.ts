import { useEffect, useState } from "react";

/**
 * Hash routing so every screen is bookmarkable and the browser's back button
 * works without a server-side router (the SPA is served as static files).
 *
 * Routes:
 *   #/agents
 *   #/agents/<key>/access | runs | activity
 *   #/agents/<key>/runs/<workflow>[/<run id>]
 *   #/people[/<subject>]
 *   #/activity
 */
export type Route = { path: string; parts: string[] };

export const DEFAULT_ROUTE = "/agents";

export function href(path: string) {
  return "#" + path;
}

export function navigate(path: string) {
  location.hash = path;
}

export const agentHref = (key: string, tab = "access") =>
  href(`/agents/${encodeURIComponent(key)}/${tab}`);

export const personHref = (subject: string) =>
  href(`/people/${encodeURIComponent(subject)}`);

function parse(hash: string): Route {
  const path = hash.replace(/^#/, "") || DEFAULT_ROUTE;
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
  return { path, parts };
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
