// Checks for the chat app's stream reader (src/app/chat/stream.ts): SSE framing
// and how AI SDK UI message stream chunks become assistant-ui message parts.
//   node --experimental-strip-types tests/app-stream.test.mts
import assert from "node:assert/strict";
import { MessageAccumulator, readEventStream } from "../src/app/chat/stream.ts";

const run = (chunks: object[]) => {
  const acc = new MessageAccumulator();
  for (const c of chunks) acc.apply(c as never);
  return acc;
};
// reused text id after a tool call stays in order
let acc = run([
  { type: "start", messageId: "m_abcdefgh", messageMetadata: { conversationId: "c_1" } },
  { type: "text-start", id: "t1" }, { type: "text-delta", id: "t1", delta: "Before. " }, { type: "text-end", id: "t1" },
  { type: "tool-input-available", toolCallId: "c1", toolName: "read_knowledge", input: { path: "a.md" } },
  { type: "tool-output-available", toolCallId: "c1", output: { status: "ok" } },
  { type: "text-start", id: "t1" }, { type: "text-delta", id: "t1", delta: "After." }, { type: "text-end", id: "t1" },
  { type: "data-title", data: { title: "Pricing" }, transient: true },
  { type: "finish", messageMetadata: { status: "complete" } },
]);
let parts = acc.snapshot() as any[];
assert.deepEqual(parts.map((p) => p.type), ["text", "tool-call", "text"]);
assert.equal(parts[0].text, "Before. ");
assert.equal(parts[2].text, "After.");
assert.equal(parts[1].isError, false);
assert.equal(acc.title, "Pricing");
assert.equal(acc.finish, "complete");
assert.equal(acc.messageId, "m_abcdefgh");
// deltas without starts, interleaved with a tool, and a failing tool
acc = run([
  { type: "text-delta", id: "x", delta: "One " },
  { type: "text-delta", id: "x", delta: "two." },
  { type: "tool-input-available", toolCallId: "c2", toolName: "search", input: "q" },
  { type: "tool-output-error", toolCallId: "c2", errorText: "Timed out." },
  { type: "text-delta", id: "x", delta: "Three." },
  { type: "error", errorText: "Provider down" },
]);
parts = acc.snapshot() as any[];
assert.deepEqual(parts.map((p) => p.type), ["text", "tool-call", "text"]);
assert.equal(parts[0].text, "One two.");
assert.deepEqual(parts[1].args, { input: "q" });
assert.equal(parts[1].isError, true);
assert.deepEqual(parts[1].result, { status: "error", message: "Timed out." });
assert.equal(acc.error, "Provider down");
// reasoning indicator only
acc = run([{ type: "reasoning-start", id: "r" }, { type: "reasoning-delta", id: "r", delta: "" }, { type: "reasoning-end", id: "r" }]);
assert.deepEqual(acc.snapshot().map((p: any) => [p.type, p.text]), [["reasoning", ""]]);
// unchanged parts keep identity between snapshots
acc = run([{ type: "text-delta", id: "a", delta: "x" }]);
const s1 = acc.snapshot();
acc.apply({ type: "tool-input-available", toolCallId: "t", toolName: "n", input: {} } as never);
const s2 = acc.snapshot();
assert.equal(s1[0], s2[0]);
// SSE framing: split chunks, CRLF, comments, [DONE]
const enc = new TextEncoder();
const body = new ReadableStream<Uint8Array>({
  start(c) {
    for (const piece of [': ping\r\n\r\n', 'data: {"type":"text-de', 'lta","id":"t","delta":"hi"}\r\n\r\ndata: {"type":"finish"}\n\n', "data: [DONE]\n\n", 'data: {"type":"text-delta","id":"t","delta":"late"}\n\n'])
      c.enqueue(enc.encode(piece));
    c.close();
  },
});
const seen: string[] = [];
for await (const batch of readEventStream(body)) for (const chunk of batch) seen.push(chunk.type + ("delta" in chunk ? `:${chunk.delta}` : ""));
assert.deepEqual(seen, ["text-delta:hi", "finish"]);
console.log("stream checks passed");
