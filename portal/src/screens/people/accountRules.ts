import { ApiError, type SignInOptions } from "../../api";
import { errorText } from "../../hooks/useData";

/** Input rules shared by Add user on People and on an agent's Access tab.
 *  The server applies the same rules; these only explain problems early. */

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function emailProblem(value: string): string | null {
  const v = value.trim().toLowerCase();
  if (!v) return "Enter an email address.";
  return EMAIL.test(v) ? null : "Enter a valid email address.";
}

/** Google sign-in may be limited to some domains (OAUTH_ALLOWED_DOMAINS). */
export function googleDomainProblem(email: string, options?: SignInOptions): string | null {
  const domains = options?.google_domains;
  if (!domains) return null;
  const domain = email.trim().toLowerCase().split("@").pop() ?? "";
  if (domains.includes(domain)) return null;
  const allowed = domains.filter(Boolean);
  return allowed.length
    ? `Google sign-in here accepts only ${allowed.join(", ")} addresses.`
    : "Google sign-in accepts no email domains on this deployment.";
}

export const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(errorText(e), 0));

/** A partial create's reason and next step (the title already says what exists). */
export function partialDetail(error: ApiError): string {
  const reason = typeof error.data.reason === "string" ? error.data.reason : "";
  return reason
    ? `${reason} Try again to grant access. The account won’t be created twice.`
    : error.message;
}
