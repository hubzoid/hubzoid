import { t } from "../i18n/en";
import { ApiError } from "./api";

/** A plain sentence for anything that went wrong talking to the server. */
export function describeError(error: unknown, fallback: string = t.errors.generic): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return t.errors.network;
    if (error.code === "rate_limited" || error.status === 429) return t.errors.rateLimited(error.retryAfter);
    if (error.hasServerMessage) return error.message;
    if (error.status === 401) return t.errors.sessionExpired;
    if (error.status === 403) return t.errors.forbidden;
    if (error.status === 404) return t.errors.notFound;
    if (error.status >= 500) return t.errors.server;
  }
  if (error instanceof Error && error.message && !(error instanceof ApiError)) return error.message;
  return fallback;
}
