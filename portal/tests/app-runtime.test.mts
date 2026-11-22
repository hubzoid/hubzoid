// The chat runtime contract (src/app/chat/runtime.ts, runtime-rules.ts):
//   node --experimental-strip-types --no-warnings tests/app-runtime.test.mts
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ADAPTER_FIELDS, UNBOUND_FIELDS, deleteGate, forgetSentDrafts, laneFor, sendQueue, settleWithin } from "../src/app/chat/runtime-rules.ts";
import { LIVE_TIMING, LiveStream, type Clock, type StreamLike } from "../src/app/chat/live.ts";
import { documentOf } from "../src/app/chat/doc-links.ts";

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


// ---- live stream (live.ts, GitHub #127) ------------------------------------------------
const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};

class FakeClock implements Clock {
  t = 0;
  private seq = 0;
  private timers = new Map<number, { at: number; fn: () => void; every?: number }>();
  now = () => this.t;
  setTimeout = (fn: () => void, ms: number) => {
    this.timers.set(++this.seq, { at: this.t + ms, fn });
    return this.seq;
  };
  clearTimeout = (id: unknown) => void this.timers.delete(id as number);
  setInterval = (fn: () => void, ms: number) => {
    this.timers.set(++this.seq, { at: this.t + ms, fn, every: ms });
    return this.seq;
  };
  clearInterval = (id: unknown) => void this.timers.delete(id as number);
  async advance(ms: number) {
    const end = this.t + ms;
    for (;;) {
      const next = [...this.timers.entries()].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      const [id, timer] = next;
      this.t = timer.at;
      if (timer.every) timer.at += timer.every;
      else this.timers.delete(id);
      timer.fn();
      await flush();
    }
    this.t = end;
    await flush();
  }
}

class FakeStream implements StreamLike {
  readyState = 0;
  onopen: StreamLike["onopen"] = null;
  onmessage: StreamLike["onmessage"] = null;
  onerror: StreamLike["onerror"] = null;
  closed = false;
  after: number;
  constructor(after: number) {
    this.after = after;
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  opened() {
    this.readyState = 1;
    this.onopen?.({});
  }
  event(data: string, id = "") {
    this.onmessage?.({ data, lastEventId: id });
  }
  failed() {
    this.readyState = 2;
    this.onerror?.({});
  }
}

function harness(opts: { running?: boolean; changed?: boolean; reload?: () => Promise<boolean> } = {}) {
  const clock = new FakeClock();
  const state = { running: opts.running ?? true, changed: opts.changed ?? false, cursor: 5, reloads: 0, events: [] as string[] };
  const streams: FakeStream[] = [];
  const live = new LiveStream({
    clock,
    open: (after) => {
      const s = new FakeStream(after);
      streams.push(s);
      return s;
    },
    cursor: () => state.cursor,
    reload: opts.reload ?? (async () => {
      state.reloads++;
      return state.changed;
    }),
    watching: () => state.running,
    onEvent: (data) => state.events.push(data),
  });
  return { clock, state, streams, live };
}

// stopped_stream_never_reconnects
{
  const { clock, streams, live, state } = harness();
  live.start();
  streams[0].opened();
  streams[0].failed();
  live.stop();
  await clock.advance(120_000);
  assert.equal(streams.length, 1, "no stream after stop");
  assert.equal(state.reloads, 0, "no reload after stop");
}

// a_retry_already_reloading_when_stopped_does_nothing
{
  let finish: (v: boolean) => void = () => {};
  const { clock, streams, live } = harness({ reload: () => new Promise<boolean>((r) => (finish = r)) });
  live.start();
  streams[0].failed();
  await clock.advance(LIVE_TIMING.backoffFirst);
  live.stop();
  finish(false);
  await clock.advance(60_000);
  assert.equal(streams.length, 1);
}

// silent_stream_during_a_reply_is_checked_then_replaced_once
{
  const { clock, streams, live, state } = harness({ changed: true });
  live.start();
  streams[0].opened();
  await clock.advance(LIVE_TIMING.silence - 1);
  assert.equal(state.reloads, 0, "quiet for less than the silence limit");
  await clock.advance(LIVE_TIMING.watchdog);
  assert.equal(state.reloads, 2, "one check, then the reload before reconnecting");
  assert.equal(streams.length, 2);
  assert.ok(streams[0].closed);
  streams[1].opened();
  await clock.advance(LIVE_TIMING.watchdog * 2);
  assert.equal(streams.length, 2, "not replaced again right away");
}

// a_silent_stream_with_nothing_missed_stays
{
  const { clock, streams, live, state } = harness({ changed: false });
  live.start();
  streams[0].opened();
  streams[0].event('{"type":"run.changed"}', "5");
  await clock.advance(LIVE_TIMING.silence + LIVE_TIMING.watchdog);
  assert.equal(state.reloads, 1);
  assert.equal(streams.length, 1, "kept: the snapshot showed nothing new");
  state.cursor = 9; // events the stream never delivered
  await clock.advance(LIVE_TIMING.silence + LIVE_TIMING.watchdog);
  assert.equal(streams.length, 2, "replaced: the snapshot is past what it delivered");
  assert.equal(streams[1].after, 9);
}

// no_reply_running_no_checks
{
  const { clock, streams, live, state } = harness({ running: false });
  live.start();
  streams[0].opened();
  await clock.advance(120_000);
  assert.equal(state.reloads, 0);
}

// hidden_tab_gives_its_connection_back_and_catches_up_when_shown
{
  const { clock, streams, live, state } = harness();
  live.start();
  streams[0].opened();
  live.hidden();
  await clock.advance(LIVE_TIMING.hiddenRelease);
  assert.ok(streams[0].closed && live.isSuspended);
  await clock.advance(60_000);
  assert.equal(streams.length, 1, "the watchdog leaves a suspended tab alone");
  live.shown();
  await flush();
  assert.equal(state.reloads, 1);
  assert.equal(streams.length, 2);
  // Shown again before the release: nothing changes.
  streams[1].opened();
  live.hidden();
  await clock.advance(1000);
  live.shown();
  await clock.advance(LIVE_TIMING.hiddenRelease);
  assert.ok(!streams[1].closed);
}

// a_stream_stuck_connecting_is_replaced_after_its_deadline
{
  const { clock, streams, live } = harness({ running: false });
  live.start();
  await clock.advance(LIVE_TIMING.connectDeadline);
  assert.ok(streams[0].closed);
  await clock.advance(LIVE_TIMING.backoffFirst);
  assert.equal(streams.length, 2);
}

// events_reach_the_feed
{
  const { streams, live, state } = harness();
  live.start();
  streams[0].opened();
  streams[0].event('{"type":"message.updated"}');
  assert.deepEqual(state.events, ['{"type":"message.updated"}']);
}

// ---- sends one at a time, Stop right after Send ----------------------------------------
{
  const order: string[] = [];
  const queue = sendQueue();
  let releaseFirst: () => void = () => {};
  const first = queue(() => new Promise<void>((resolve, reject) => {
    order.push("first starts");
    releaseFirst = () => reject(new Error("refused"));
  }));
  const second = queue(async () => {
    order.push("second starts");
  });
  await flush();
  assert.deepEqual(order, ["first starts"], "the second send waits");
  releaseFirst();
  await assert.rejects(first, /refused/);
  await second;
  assert.deepEqual(order, ["first starts", "second starts"], "a refused send doesn't block the next");
}
{
  let wakeSleep: () => void = () => {};
  const waited = settleWithin(new Promise(() => {}), 10_000, () => new Promise<void>((r) => (wakeSleep = r)));
  wakeSleep();
  await waited; // a send that never answers can't hold Stop forever
  await settleWithin(Promise.reject(new Error("refused")), 10_000); // nor can a refused one
}

// ---- a reply's file chip opens this conversation's document only -----------------------
{
  const docs = { conversationId: "c1", titles: new Set(["report.html", "q3 plan.md"]) };
  const origin = "http://127.0.0.1:8080";
  assert.equal(documentOf("/artifacts/web-c1/report.html?t=x", docs, origin), "report.html");
  assert.equal(documentOf(`${origin}/b/sales/artifacts/web-c1/q3%20plan.md`, docs, origin), "q3 plan.md");
  assert.equal(documentOf("/artifacts/web-c2/report.html", docs, origin), null, "another conversation's file");
  assert.equal(documentOf("/artifacts/web-c1/other.html", docs, origin), null);
  assert.equal(documentOf("https://elsewhere.example/artifacts/web-c1/report.html", docs, origin), null);
  assert.equal(documentOf("/artifacts/web-c1/report.html", null, origin), null);
}

console.log("app-runtime: ok");
