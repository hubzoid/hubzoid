// A conversation's live event stream, kept alive (GitHub #127). Free of
// imports so the runtime contract test can run it with fakes
// (tests/app-runtime.test.mts).
//
// States: stopped, connecting (deadline), open, backoff (one retry timer),
// suspended (a tab hidden for a while gives its connection back: browsers
// allow six per site over plain HTTP). Every change of state bumps `epoch`, so
// a retry or reload already under way finds it stale and does nothing.
//
// While a reply runs, a watchdog checks the stream: one that is not open is
// reopened, and one that has been silent for a while is checked against a
// fresh snapshot. If the snapshot shows something the stream never delivered,
// the stream is dead without having said so: it is replaced.

export interface StreamLike {
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: string; lastEventId?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(): void;
}

export type Clock = {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(id: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(id: unknown): void;
};

export type LiveDeps = {
  /** Open the stream after this persisted event sequence. */
  open(after: number): StreamLike;
  /** The snapshot's cursor (the last persisted event it reflects). */
  cursor(): number;
  /** Reload the snapshot. Resolves true when the screen changed. */
  reload(): Promise<boolean>;
  /** A reply is running or a send is on its way: watch the stream. */
  watching(): boolean;
  /** One event's data (persisted or live). */
  onEvent(data: string): void;
  clock?: Clock;
};

export const OPEN = 1;
const CONNECTING = 0;
export const LIVE_TIMING = {
  connectDeadline: 10_000,
  silence: 15_000,
  watchdog: 5_000,
  hiddenRelease: 60_000,
  backoffFirst: 500,
  backoffMax: 15_000,
};

const realClock: Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (id) => clearInterval(id as ReturnType<typeof setInterval>),
};

export class LiveStream {
  private deps: LiveDeps;
  private clock: Clock;
  private source: StreamLike | null = null;
  private timer: unknown = null; // the one connect-deadline or backoff timer
  private hideTimer: unknown = null;
  private watchdog: unknown = null;
  private started = false;
  private suspended = false;
  private hiddenNow = false; // nobody is looking: no checks until shown
  private checking = false;
  private retry = 0;
  private epoch = 0;
  private lastEventAt = 0;
  /** The highest persisted sequence the stream (or the snapshot it started from) covers. */
  private lastSeq = 0;

  constructor(deps: LiveDeps) {
    this.deps = deps;
    this.clock = deps.clock ?? realClock;
  }

  get isOpen() {
    return this.source?.readyState === OPEN;
  }
  get isSuspended() {
    return this.suspended;
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.watchdog = this.clock.setInterval(() => this.tick(), LIVE_TIMING.watchdog);
    this.connect();
  }

  stop() {
    this.started = false;
    this.epoch++;
    this.clearTimer();
    if (this.hideTimer !== null) this.clock.clearTimeout(this.hideTimer);
    this.hideTimer = null;
    if (this.watchdog !== null) this.clock.clearInterval(this.watchdog);
    this.watchdog = null;
    this.drop();
  }

  /** The tab was hidden: give the connection back after a while. */
  hidden() {
    this.hiddenNow = true;
    if (!this.started || this.hideTimer !== null) return;
    this.hideTimer = this.clock.setTimeout(() => {
      this.hideTimer = null;
      this.suspended = true;
      this.epoch++;
      this.clearTimer();
      this.drop();
    }, LIVE_TIMING.hiddenRelease);
  }

  /** The tab is visible again, or the network is back: catch up now. */
  shown() {
    this.hiddenNow = false;
    if (this.hideTimer !== null) this.clock.clearTimeout(this.hideTimer);
    this.hideTimer = null;
    if (!this.started) return;
    if (!this.suspended && this.source !== null) {
      this.tick(); // a stream that went quiet while hidden is checked now
      return;
    }
    this.suspended = false;
    this.restart();
  }

  /** Replace the stream: reload the snapshot, then follow from its cursor. */
  restart() {
    if (!this.started || this.suspended) return;
    this.epoch++;
    this.clearTimer();
    this.drop();
    this.retry = 0;
    const epoch = this.epoch;
    void this.deps.reload().then(
      () => epoch === this.epoch && this.connect(),
      () => epoch === this.epoch && this.backoff(),
    );
  }

  private connect() {
    if (!this.started || this.suspended || this.source !== null) return;
    this.clearTimer();
    const after = this.deps.cursor();
    this.lastSeq = after;
    this.lastEventAt = this.clock.now();
    const es = this.deps.open(after);
    this.source = es;
    this.timer = this.clock.setTimeout(() => {
      // Still connecting after the deadline (a request stuck behind the
      // browser's connection limit, a proxy that never answers).
      if (this.source === es && es.readyState === CONNECTING) {
        this.drop();
        this.backoff();
      }
    }, LIVE_TIMING.connectDeadline);
    es.onopen = () => {
      if (this.source !== es) return;
      this.retry = 0;
      this.lastEventAt = this.clock.now();
      this.clearTimer();
    };
    es.onmessage = (event) => {
      if (this.source !== es) return;
      this.lastEventAt = this.clock.now();
      const seq = Number(event.lastEventId);
      if (event.lastEventId && Number.isFinite(seq) && seq > this.lastSeq) this.lastSeq = seq;
      this.deps.onEvent(event.data);
    };
    es.onerror = () => {
      if (this.source !== es) return;
      // Too far behind (410), a dropped connection, a restart: reload the
      // snapshot after a pause, then follow again from its cursor.
      this.drop();
      this.backoff();
    };
  }

  private backoff() {
    if (!this.started || this.suspended) return;
    this.clearTimer();
    const wait = Math.min(LIVE_TIMING.backoffMax, LIVE_TIMING.backoffFirst * 2 ** this.retry++);
    const epoch = this.epoch;
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      void this.deps.reload().then(
        () => epoch === this.epoch && this.connect(),
        () => epoch === this.epoch && this.backoff(),
      );
    }, wait);
  }

  private tick() {
    if (!this.started || this.suspended || this.hiddenNow || this.checking || !this.deps.watching()) return;
    if (this.source === null) {
      // Waiting out a backoff: a running reply shouldn't wait long.
      if (this.timer === null) this.connect();
      return;
    }
    if (this.source.readyState !== OPEN) return; // the connect deadline handles it
    if (this.clock.now() - this.lastEventAt < LIVE_TIMING.silence) return;
    this.checking = true;
    const epoch = this.epoch;
    const covered = this.lastSeq;
    void this.deps.reload().then(
      (changed) => {
        this.checking = false;
        if (epoch !== this.epoch) return;
        this.lastEventAt = this.clock.now();
        // The snapshot shows events or text the stream never delivered.
        if (changed || this.deps.cursor() > covered) this.restart();
      },
      () => {
        this.checking = false;
      },
    );
  }

  private drop() {
    const es = this.source;
    this.source = null;
    if (es) {
      es.onopen = es.onmessage = es.onerror = null;
      es.close();
    }
  }

  private clearTimer() {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }
}
