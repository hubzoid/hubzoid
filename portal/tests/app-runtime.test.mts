// The chat runtime contract (src/app/chat/runtime.ts, runtime-rules.ts):
//   node --experimental-strip-types --no-warnings tests/app-runtime.test.mts
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ADAPTER_FIELDS, UNBOUND_FIELDS, deleteGate, forgetSentDrafts, laneFor } from "../src/app/chat/runtime-rules.ts";

const here = new URL(".", import.meta.url);
const read = (rel: string) => readFileSync(new URL(rel, here), "utf8");

// adapter_names_match_pin: every field we bind is declared by the pinned package.
const declared = read("../node_modules/@assistant-ui/core/dist/runtimes/external-store/external-store-adapter.d.ts");
for (const field of ADAPTER_FIELDS) assert.match(declared, new RegExp(`\\b${field}\\??:`), `pin lost ${field}`);
const queue = read("../node_modules/@assistant-ui/core/dist/runtime/queue/external-thread-queue-adapter.d.ts");
for (const field of ["items", "steerItems", "enqueue", "steer", "move", "edit", "remove", "__internal_notifyCancelled"])
  assert.match(queue, new RegExp(`\\b${field}\\??:`), `pin lost queue.${field}`);
const pinned = JSON.parse(read("../node_modules/@assistant-ui/react/package.json")).version;
assert.equal(pinned, "0.15.22", "assistant-ui pin changed: re-check the adapter contract");

// queue_lane_follows_explicit_steer_flag
assert.equal(laneFor({}), "queue");
assert.equal(laneFor({ steer: false }), "queue");
assert.equal(laneFor({ steer: true }), "steer");

// sent_text_leaves_its_origin_draft_but_no_other_page
{
  const fake = (store: Record<string, unknown>) => Object.assign(store, {
    getItem: (k: string) => (typeof store[k] === "string" ? (store[k] as string) : null),
    removeItem: (k: string) => { delete store[k]; },
  });
  const left = (store: Record<string, unknown>) => Object.keys(store).filter((k) => typeof store[k] === "string").sort();
  const store = fake({
    "hz.draft-origin:c_new": "hz.draft:u1:/new/sales", "hz.draft:u1:/new/sales": " Review this ",
    "hz.draft:u1:/?agent=ops": "Review this", "hz.draft:u2:/new/sales": "Review this",
    "hz.draft:u1:/c/c_new": "Review this", "hz.draft:u2:/c/c_new": "Review this",
    "hz.draft:u1:/c/c_other": "Review this", "unrelated": "Review this",
  });
  forgetSentDrafts(store, "Review this", "c_new");
  // Only the page it was started from and its own conversation, for that person.
  assert.deepEqual(left(store), ["hz.draft:u1:/?agent=ops", "hz.draft:u1:/c/c_other",
                                 "hz.draft:u2:/c/c_new", "hz.draft:u2:/new/sales", "unrelated"]);
  // A file-only first message consumes the record, so later sends touch nothing.
  const files = fake({ "hz.draft-origin:c_f": "hz.draft:u1:/", "hz.draft:u1:/": "later draft" });
  forgetSentDrafts(files, "", "c_f");
  forgetSentDrafts(files, "later draft", "c_f");
  assert.deepEqual(left(files), ["hz.draft:u1:/"]);
}

// on_delete_requires_confirmed_preview
const confirmed = new Map<string, { preview_hash: string }>();
assert.throws(() => deleteGate(confirmed, "m_1"), /Confirm the deletion preview/);
confirmed.set("m_1", { preview_hash: "sha256:x" });
assert.equal(deleteGate(confirmed, "m_1").preview_hash, "sha256:x");
assert.throws(() => deleteGate(confirmed, "m_1"), /Confirm/); // used once

// no_browser_tool_result_callbacks
const runtime = read("../src/app/chat/runtime.ts");
for (const field of UNBOUND_FIELDS) assert.doesNotMatch(runtime, new RegExp(`\\b${field}\\s*:`), `${field} must not be bound`);

console.log("app-runtime: ok");
