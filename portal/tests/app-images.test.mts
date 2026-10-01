// Checks for which images in an answer load by themselves
// (src/app/chat/images.ts): this site's own files at once, anything else only
// when the person asks, so an image address can't carry chat data away.
//   node --experimental-strip-types tests/app-images.test.mts
import assert from "node:assert/strict";
import { imageSource } from "../src/app/chat/images.ts";

(globalThis as { location?: unknown }).location = { origin: "https://hub.example.com", host: "hub.example.com" };

// Files the agent wrote, uploads and branding, on this site, show at once.
for (const [src, shown] of [
  ["/artifacts/c_abc123/chart.png", "/artifacts/c_abc123/chart.png"],
  ["/b/finance/artifacts/c_abc123/out/chart.png", "/b/finance/artifacts/c_abc123/out/chart.png"],
  ["/artifacts/c_abc123/chart.png?sig=abc", "/artifacts/c_abc123/chart.png?sig=abc"],
  ["https://hub.example.com/artifacts/c_abc123/chart.png", "/artifacts/c_abc123/chart.png"],
  ["/api/conversations/c_abc123/files/f_123", "/api/conversations/c_abc123/files/f_123"],
  ["/b/finance/api/conversations/c_abc123/files/f_123", "/b/finance/api/conversations/c_abc123/files/f_123"],
  ["/branding/logo.svg", "/branding/logo.svg"],
])
  assert.deepEqual(imageSource(src), { inline: true, src: shown }, src);

// Anything else waits for a click and names where it would load from.
assert.deepEqual(imageSource("https://attacker.example/collect?data=SECRET"), {
  inline: false,
  src: "https://attacker.example/collect?data=SECRET",
  host: "attacker.example",
});
for (const [src, host] of [
  ["//attacker.example/x.png", "attacker.example"],
  ["http://attacker.example:8080/x.png", "attacker.example:8080"],
  ["https://hub.example.com.attacker.example/artifacts/c_1/x.png", "hub.example.com.attacker.example"],
  ["https://attacker.example/artifacts/c_1/x.png", "attacker.example"],
  // On this site but not one of its file endpoints.
  ["/api/agents", "hub.example.com"],
  ["/artifacts/../api/auth/session", "hub.example.com"],
  ["/artifacts/%2e%2e/api/agents", "hub.example.com"],
  ["/oauth/google/login?redirect=/", "hub.example.com"],
])
{
  const out = imageSource(src);
  assert.ok(out && !out.inline, src);
  assert.equal(out.host, host, src);
}

// Nothing to load.
for (const src of [undefined, null, "", "javascript:alert(1)", "data:image/png;base64,AAAA", "ftp://attacker.example/x.png"])
  assert.equal(imageSource(src), null, String(src));

console.log("image checks passed");
