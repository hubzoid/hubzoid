// Checks for the chat app's router (src/app/lib/router.ts): which paths it
// renders itself and which go to the server, and the sign-in redirect rules.
//   node --experimental-strip-types tests/app-router.test.mts
import assert from "node:assert/strict";
import { isAppPath, parseRoute, safeRedirect, signInHref } from "../src/app/lib/router.ts";

// The chat app's own routes: the server answers them with the page shell.
for (const path of ["/", "/new", "/new/finance", "/c/c_abc123", "/s/sh_abc123", "/auth", "/auth/set-password", "/account", "/account/connections", "/inbox", "/requests"])
  assert.equal(isAppPath(path), true, path);
// Everything else is the server's: a full page load, never the app's "Page not found".
for (const path of [
  "/mcp/oauth/consent", // the hosted MCP server's consent page, after signing in
  "/mcp",
  "/portal/",
  "/portal/artifacts/a_1",
  "/api/auth/session",
  "/oauth/google/login",
  "/artifacts/c_1/report.md",
  "/branding/logo.svg",
  "/branding-preview/retired",
  "/b/finance/artifacts/c_1/x.csv",
  "/connect/abc",
  "/newsletter",
  "/accounts",
  "/authorize",
])
  assert.equal(isAppPath(path), false, path);

// Both Inbox addresses survive the merged shell, while the file-owned brand has no preview route.
assert.deepEqual(parseRoute("/inbox", new URLSearchParams()), { name: "requests" });
assert.deepEqual(parseRoute("/requests", new URLSearchParams()), { name: "requests" });
assert.deepEqual(parseRoute("/branding-preview/retired", new URLSearchParams()), { name: "not-found" });

// Sign-in keeps only same-origin relative redirects. They are read against
// this page's origin, as the browser will read them.
(globalThis as { location?: unknown }).location = { origin: "https://hub.example.com" };
assert.equal(safeRedirect("/mcp/oauth/consent?ticket=t"), "/mcp/oauth/consent?ticket=t");
assert.equal(safeRedirect("/c/c_abc#latest"), "/c/c_abc#latest");
assert.equal(safeRedirect("//evil.example/x"), "/");
assert.equal(safeRedirect("https://evil.example/x"), "/");
assert.equal(safeRedirect("/\\evil.example"), "/");
// Browsers drop tabs and newlines anywhere in a URL, so "/<tab>/evil.example"
// is "//evil.example": another site. Control characters and backslashes are refused.
for (const value of ["/\t/evil.example", "/\n/evil.example", "/\r/evil.example", "/\t\\evil.example", "/x\\y", "/c/a\u0000b", "/c/a\u007fb", "\t//evil.example"])
  assert.equal(safeRedirect(value), "/", JSON.stringify(value));
assert.deepEqual(parseRoute("/auth", new URLSearchParams("redirect=%2F%09%2Fevil.example")), {
  name: "signin",
  redirect: "/",
  error: null,
  step: null,
});
// What comes back is the normalised path on this site.
assert.equal(safeRedirect("/c/../account"), "/account");
assert.equal(safeRedirect("https://hub.example.com/account"), "/");
// A Settings section lives in the fragment: sign-in brings the person back to it.
assert.equal(signInHref("/settings#/confirm/cr_1"), `/auth?redirect=${encodeURIComponent("/settings#/confirm/cr_1")}`);
assert.equal(safeRedirect("/c/a b"), "/c/a%20b");
assert.equal(safeRedirect("/account", "/x"), "/account");
assert.equal(safeRedirect("//evil.example", "/fallback"), "/fallback");
assert.deepEqual(parseRoute("/auth", new URLSearchParams("redirect=%2Fmcp%2Foauth%2Fconsent%3Fticket%3Dt")), {
  name: "signin",
  redirect: "/mcp/oauth/consent?ticket=t",
  error: null,
  step: null,
});
// The sign-in step (two-step sign-in) is carried in the address.
assert.equal((parseRoute("/auth", new URLSearchParams("step=code")) as { step: string | null }).step, "code");

console.log("router checks passed");
