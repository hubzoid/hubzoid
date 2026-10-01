// Checks for the chat app's router (src/app/lib/router.ts): which paths it
// renders itself and which go to the server, and the sign-in redirect rules.
//   node --experimental-strip-types tests/app-router.test.mts
import assert from "node:assert/strict";
import { isAppPath, parseRoute, safeRedirect } from "../src/app/lib/router.ts";

// The chat app's own routes: the server answers them with the page shell.
for (const path of ["/", "/new", "/new/finance", "/c/c_abc123", "/s/sh_abc123", "/auth", "/auth/set-password", "/account", "/account/connections"])
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
  "/b/finance/artifacts/c_1/x.csv",
  "/connect/abc",
  "/newsletter",
  "/accounts",
  "/authorize",
])
  assert.equal(isAppPath(path), false, path);

// Sign-in keeps only same-origin relative redirects.
assert.equal(safeRedirect("/mcp/oauth/consent?ticket=t"), "/mcp/oauth/consent?ticket=t");
assert.equal(safeRedirect("//evil.example/x"), "/");
assert.equal(safeRedirect("https://evil.example/x"), "/");
assert.equal(safeRedirect("/\\evil.example"), "/");
assert.deepEqual(parseRoute("/auth", new URLSearchParams("redirect=%2Fmcp%2Foauth%2Fconsent%3Fticket%3Dt")), {
  name: "signin",
  redirect: "/mcp/oauth/consent?ticket=t",
  error: null,
});

console.log("router checks passed");
