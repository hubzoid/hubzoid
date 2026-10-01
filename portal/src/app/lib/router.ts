// A small history-API router for the chat app. Paths:
//   /                      new chat (?agent=<id>, legacy ?models=<id>)
//   /c/:id                 a conversation
//   /s/:shareId            a shared, read-only conversation
//   /auth                  sign in (?redirect=, ?error=)
//   /auth/set-password     set or reset a password (?token=)
//   /account               profile, password, appearance
//   /account/connections   personal connections (?connected=<id>)
import { useSyncExternalStore, type MouseEvent } from "react";

const EVENT = "hz:navigate";

function subscribe(callback: () => void) {
  window.addEventListener("popstate", callback);
  window.addEventListener(EVENT, callback);
  return () => {
    window.removeEventListener("popstate", callback);
    window.removeEventListener(EVENT, callback);
  };
}

const snapshot = () => location.pathname + location.search;

export function useLocation(): { path: string; search: URLSearchParams; href: string } {
  const href = useSyncExternalStore(subscribe, snapshot);
  const q = href.indexOf("?");
  const path = q === -1 ? href : href.slice(0, q);
  const search = new URLSearchParams(q === -1 ? "" : href.slice(q));
  return { path: path.replace(/\/+$/, "") || "/", search, href };
}

/** Same-origin relative paths only: "/x" yes, "//evil" and "https://" no. */
export function safeRedirect(value: string | null | undefined, fallback = "/"): string {
  if (!value) return fallback;
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return fallback;
  return value;
}

/**
 * Paths the chat app renders itself: the routes above, which the server answers
 * with the page shell (hubzoid/webapp.py SHELL_PATHS). Anything else is a full
 * page load, so a server page that sent someone to sign in (the hosted MCP
 * server's consent page at /mcp/..., the Console at /portal/...) gets them back.
 */
export function isAppPath(path: string): boolean {
  return /^\/(?:$|new(?:\/|$)|c\/|s\/|auth(?:\/|$)|account(?:\/|$))/.test(path);
}

export function navigate(to: string, options: { replace?: boolean } = {}) {
  if (!isAppPath(to.split("?")[0])) {
    if (options.replace) location.replace(to);
    else location.assign(to);
    return;
  }
  if (to === snapshot()) return;
  if (options.replace) history.replaceState(null, "", to);
  else history.pushState(null, "", to);
  window.dispatchEvent(new Event(EVENT));
}

/** Click handler for in-app links: keeps modified clicks (new tab) native. */
export function linkClick(event: MouseEvent<HTMLAnchorElement>, to: string, after?: () => void) {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey ||
    !isAppPath(to.split("?")[0])
  )
    return;
  event.preventDefault();
  navigate(to);
  after?.();
}

export type Route =
  | { name: "new"; agent: string | null }
  | { name: "conversation"; id: string }
  | { name: "share"; id: string }
  | { name: "signin"; redirect: string; error: string | null }
  | { name: "set-password"; token: string | null }
  | { name: "account" }
  | { name: "connections"; connected: string | null; connector: string | null; error: string | null }
  | { name: "not-found" };

export function parseRoute(path: string, search: URLSearchParams): Route {
  const parts = path.split("/").filter(Boolean).map((p) => {
    try {
      return decodeURIComponent(p);
    } catch {
      return p;
    }
  });
  if (parts.length === 0 || (parts[0] === "new" && parts.length <= 2))
    return { name: "new", agent: search.get("agent") || search.get("models") || parts[1] || null };
  if (parts[0] === "c" && parts[1] && parts.length === 2) return { name: "conversation", id: parts[1] };
  if (parts[0] === "s" && parts[1] && parts.length === 2) return { name: "share", id: parts[1] };
  if (parts[0] === "auth") {
    if (parts.length === 1)
      return { name: "signin", redirect: safeRedirect(search.get("redirect")), error: search.get("error") };
    if (parts[1] === "set-password" || parts[1] === "reset-password")
      return { name: "set-password", token: search.get("token") };
  }
  if (parts[0] === "account") {
    if (parts.length === 1) return { name: "account" };
    if (parts[1] === "connections" && parts.length === 2)
      return {
        name: "connections",
        connected: search.get("connected"),
        connector: search.get("connector"),
        error: search.get("error"),
      };
  }
  return { name: "not-found" };
}

/** The sign-in URL that brings the person back here afterwards. */
export function signInHref(current = location.pathname + location.search): string {
  const redirect = safeRedirect(current);
  return redirect === "/" ? "/auth" : `/auth?redirect=${encodeURIComponent(redirect)}`;
}
