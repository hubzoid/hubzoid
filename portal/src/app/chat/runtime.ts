// The chat on screen, rendered from the server's conversation graph through
// assistant-ui's external store. The browser never owns the graph: every
// action is a server operation (POST /operations, /queue, PUT /view ...), and
// the screen follows the server's snapshot plus its event stream
// (GET /events: persisted events trigger a fresh snapshot, live reply
// snapshots patch the reply being written).
//
// The adapter fields we bind are listed in ADAPTER_FIELDS (runtime-rules.ts); a contract test
// (portal/tests/app-runtime.test.mts) fails when the pinned assistant-ui
// declarations stop offering any of them.
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import {
  useExternalStoreRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
  type ThreadMessage,
} from "@assistant-ui/react";
import { ApiError, enc, get, hubUrl, patch, post, put } from "../lib/api";
import { randomId } from "../lib/ids";
import type { ServerMessage, ServerPart } from "../lib/types";
import { fileRefOf, toRepository, type ConvertContext } from "./convert";
import { deleteGate, laneFor } from "./runtime-rules";

/** The ExternalStoreAdapter fields Hubzoid binds (checked against the pin). */
export { ADAPTER_FIELDS, UNBOUND_FIELDS } from "./runtime-rules";


export type Branch = {
  id: string;
  writer_actor_id: string;
  base_message_id: string | null;
  head_id: string | null;
  revision: number;
  queue_paused: boolean;
  queue_revision: number;
};
export type Run = { id: string; branch_id: string; message_id: string; actor_id: string; status: string };
export type QueueItem = {
  id: string;
  branch_id: string;
  author_id: string;
  lane: "queue" | "steer";
  revision: number;
  state: string;
  reason?: string | null;
  content?: ServerPart[];
};
export type GraphMessage = ServerMessage & {
  branch_id?: string | null;
  revises_id?: string | null;
  author_id?: string | null;
  editor_id?: string | null;
  run_id?: string | null;
  context_changed?: boolean;
};
export type Snapshot = {
  conversation: { id: string; graph_revision: number; temporary: boolean; expires_at: number | null; owner_id: string; title?: string | null };
  role: "owner" | "editor" | "viewer";
  permissions: { read: boolean; write: boolean; manage: boolean };
  messages: GraphMessage[];
  branches: Branch[];
  view: { branch_id: string | null; view_head_id: string | null; revision: number };
  runs: Run[];
  queue: QueueItem[];
  cursor: number;
};

type Pending = { text: string; parts: ServerPart[]; parentId: string | null };

const RUNNING = new Set(["admitted", "streaming", "waiting", "stopping"]);
const LIVE_QUEUE = new Set(["queued", "paused", "blocked", "needs_review", "dispatching"]);

function newKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : randomId("k_", 24);
}

/** A message's typed text and attachment parts as server content. */
export function contentOf(message: Pick<AppendMessage, "content" | "attachments">): ServerPart[] {
  const parts: ServerPart[] = [];
  const text = message.content
    .filter((p) => p.type === "text")
    .map((p) => (p as { text: string }).text)
    .join("\n\n");
  if (text) parts.push({ type: "text", text });
  for (const attachment of message.attachments ?? []) {
    const ref = fileRefOf(attachment);
    if (ref) parts.push({ type: ref.kind === "image" ? "image" : "file", file_id: ref.file_id, name: ref.name, mime: ref.mime, size: ref.size });
  }
  return parts;
}

/** One conversation's server state for this viewer, kept current. */
export class ConversationFeed {
  snapshot: Snapshot | null = null;
  live = new Map<string, ServerPart[]>();
  pending: Pending | null = null;
  error: ApiError | null = null;
  loading = true;
  version = 0;
  private listeners = new Set<() => void>();
  private source: EventSource | null = null;
  private refetchTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private retry = 0;

  conversationId: string | null;
  apiBase: string;
  me: string | null;

  constructor(conversationId: string | null, apiBase: string, me: string | null, initial?: Snapshot | null) {
    this.conversationId = conversationId;
    this.apiBase = apiBase;
    this.me = me;
    if (initial) {
      this.snapshot = initial;
      this.loading = false;
    } else if (!conversationId) {
      this.loading = false;
    }
  }

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  getVersion = () => this.version;

  private emit() {
    this.version++;
    for (const fn of this.listeners) fn();
  }

  url(path: string) {
    return hubUrl(this.apiBase, path);
  }

  async load(): Promise<void> {
    if (!this.conversationId) return;
    try {
      const snap = await get<Snapshot>(this.url(`/api/conversations/${enc(this.conversationId)}`), { quiet401: true });
      this.snapshot = snap;
      this.error = null;
      // A finished reply is in the snapshot now: drop its live copy.
      const running = new Set(snap.messages.filter((m) => m.status === "running").map((m) => m.id));
      for (const id of [...this.live.keys()]) if (!running.has(id)) this.live.delete(id);
      if (this.pending && snap.messages.some((m) => m.role === "user" && this.matchesPending(m))) this.pending = null;
    } catch (error) {
      this.error = error instanceof ApiError ? error : new ApiError(0, "network", String(error));
    } finally {
      this.loading = false;
      this.emit();
    }
  }

  private matchesPending(m: GraphMessage) {
    const text = (Array.isArray(m.content) ? m.content : [])
      .filter((p) => p.type === "text")
      .map((p) => (p as { text: string }).text)
      .join("\n\n");
    return !!this.pending && text === this.pending.text;
  }

  refetchSoon() {
    if (this.refetchTimer) return;
    this.refetchTimer = setTimeout(() => {
      this.refetchTimer = null;
      void this.load();
    }, 120);
  }

  /** Follow the conversation's events (reconnects with Last-Event-ID). */
  connect() {
    if (!this.conversationId || this.closed || this.source) return;
    const after = this.snapshot?.cursor ?? 0;
    const es = new EventSource(this.url(`/api/conversations/${enc(this.conversationId)}/events?after=${after}`), {
      withCredentials: true,
    });
    this.source = es;
    es.onopen = () => {
      this.retry = 0;
    };
    es.onmessage = (event) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.type === "message.updated" && typeof data.object_id === "string") {
        this.live.set(data.object_id, (data.content as ServerPart[]) ?? []);
        this.emit();
        return;
      }
      if (data.type === "conversation.titled" && this.snapshot) {
        this.snapshot = { ...this.snapshot, conversation: { ...this.snapshot.conversation, title: String(data.title || "") } };
        this.emit();
        return;
      }
      if (data.type === "access.revoked" || data.type === "conversation.deleted" || data.type === "conversation.expired") {
        this.snapshot = null;
        this.error = new ApiError(404, "not_found", "This conversation is no longer available.");
        this.disconnect();
        this.emit();
        return;
      }
      this.refetchSoon();
    };
    es.onerror = () => {
      // Too far behind (410) or a dropped connection: reload the snapshot,
      // then follow again from its cursor.
      es.close();
      this.source = null;
      if (this.closed) return;
      const wait = Math.min(15000, 500 * 2 ** this.retry++);
      setTimeout(() => {
        void this.load().then(() => this.connect());
      }, wait);
    };
  }

  disconnect() {
    this.source?.close();
    this.source = null;
  }

  close() {
    this.closed = true;
    this.disconnect();
    if (this.refetchTimer) clearTimeout(this.refetchTimer);
  }

  // -- derived state -------------------------------------------------------------
  get viewedBranch(): Branch | null {
    const s = this.snapshot;
    if (!s) return null;
    return s.branches.find((b) => b.id === s.view.branch_id) ?? null;
  }

  get activeRun(): Run | null {
    const s = this.snapshot;
    const b = this.viewedBranch;
    if (!s) return null;
    const head = s.view.view_head_id;
    return (
      s.runs.find((r) => RUNNING.has(r.status) && (b ? r.branch_id === b.id : false)) ??
      s.runs.find((r) => RUNNING.has(r.status) && r.message_id === head) ??
      null
    );
  }

  get myQueue(): QueueItem[] {
    const s = this.snapshot;
    const b = this.viewedBranch;
    if (!s || !b) return [];
    return s.queue.filter((q) => q.branch_id === b.id && q.author_id === this.me && LIVE_QUEUE.has(q.state));
  }

  messages(): GraphMessage[] {
    const s = this.snapshot;
    const out: GraphMessage[] = (s?.messages ?? []).map((m) => {
      const live = this.live.get(m.id);
      return live && m.status === "running" ? { ...m, content: live } : m;
    });
    if (this.pending) {
      const pid = this.pending.parentId;
      const now = Date.now() / 1000;
      out.push({ id: "pending-user", parent_id: pid, role: "user", content: this.pending.parts, status: "complete", created_at: now });
      out.push({ id: "pending-reply", parent_id: "pending-user", role: "assistant", content: [], status: "running", created_at: now });
    }
    return out;
  }

  headId(): string | null {
    if (this.pending) return "pending-reply";
    return this.snapshot?.view.view_head_id ?? null;
  }

  // -- operations --------------------------------------------------------------------
  async operation<T = Record<string, unknown>>(body: Record<string, unknown>): Promise<T> {
    const out = await post<T>(this.url(`/api/conversations/${enc(this.conversationId!)}/operations`), { version: 1, ...body }, {
      headers: { "Idempotency-Key": newKey() },
    });
    this.refetchSoon();
    return out;
  }

  /** The viewer's own branch at the viewed head, opened when needed. */
  async writableBranch(): Promise<Branch> {
    const s = this.snapshot!;
    const head = s.view.view_head_id;
    const own = s.branches.find((b) => b.writer_actor_id === this.me && b.head_id === head);
    if (own) return own;
    const made = await post<{ branch: Branch }>(this.url(`/api/conversations/${enc(this.conversationId!)}/branches`), { from_message_id: head }, {
      headers: { "Idempotency-Key": newKey() },
    });
    this.snapshot = { ...s, branches: [...s.branches, made.branch], view: { ...s.view, branch_id: made.branch.id } };
    return made.branch;
  }

  /** Send now on an idle branch, or queue behind the running reply. */
  /** This person's choices for their next messages: the runtime's own web search, a model. */
  webSearch = false;
  model: string | null = null;

  async submit(parts: ServerPart[], text: string, lane: "queue" | "steer" = "queue") {
    const branch = await this.writableBranch();
    const busy = !!this.activeRun || this.myQueue.length > 0 || branch.queue_paused;
    if (!busy) {
      this.pending = { text, parts, parentId: branch.head_id };
      this.emit();
    }
    try {
      await post(this.url(`/api/conversations/${enc(this.conversationId!)}/queue`), {
        branch_id: branch.id, content: parts, lane,
        ...(this.webSearch ? { web_search: true } : {}),
        ...(this.model ? { model_selection: { mode: "model", model: this.model } } : {}),
      }, {
        headers: { "Idempotency-Key": newKey() },
      });
    } catch (error) {
      this.pending = null;
      this.emit();
      throw error;
    }
    await this.load();
  }

  async patchQueue(item: QueueItem, body: Record<string, unknown>) {
    await patch(this.url(`/api/conversations/${enc(this.conversationId!)}/queue/${enc(item.id)}`), { expected_revision: item.revision, ...body });
    await this.load();
  }

  async queueControl(action: "pause" | "resume" | "clear") {
    const b = this.viewedBranch;
    if (!b) return;
    await post(this.url(`/api/conversations/${enc(this.conversationId!)}/queue-control`), {
      branch_id: b.id,
      action,
      expected_revision: b.queue_revision,
    });
    await this.load();
  }

  async cancel(parkQueue = true) {
    const run = this.activeRun;
    if (!run) return;
    await post(this.url(`/api/runs/${enc(run.id)}/cancel`), { park_queue: parkQueue });
    await this.load();
  }

  async setView(headId: string | null) {
    const s = this.snapshot;
    if (!s) return;
    const branch =
      s.branches.find((b) => b.head_id === headId && b.writer_actor_id === this.me) ??
      s.branches.find((b) => b.head_id === headId) ??
      null;
    this.snapshot = { ...s, view: { ...s.view, branch_id: branch?.id ?? null, view_head_id: headId } };
    this.emit();
    try {
      await put(this.url(`/api/conversations/${enc(this.conversationId!)}/view`), { branch_id: branch?.id ?? null, view_head_id: headId });
    } catch {
      /* the choice stays on screen; it's only not remembered */
    }
  }

  async deletionPreview(messageId: string) {
    return post<DeletionPreview>(this.url(`/api/conversations/${enc(this.conversationId!)}/deletion-preview`), {
      message_id: messageId,
      expected_revision: this.snapshot?.conversation.graph_revision,
    });
  }
}

export type DeletionPreview = {
  message_id: string;
  removed: string[];
  reparented: Record<string, string>;
  shares: string[];
  snapshot_links?: string[];
  graph_revision: number;
  preview_hash: string;
};

/** The assistant-ui runtime bound to one conversation feed. */
export function useHubzoidRuntime({
  feed,
  ctx,
  ensureFeed,
  attachments,
  confirmedDeletes,
  onError,
  speech,
  dictation,
}: {
  feed: ConversationFeed;
  ctx: ConvertContext;
  /** Create the conversation on first send; resolves to its feed. */
  ensureFeed: () => Promise<ConversationFeed>;
  attachments?: NonNullable<ExternalStoreAdapter["adapters"]>["attachments"];
  /** Previews the person confirmed in the deletion dialog, by message id. */
  confirmedDeletes: Map<string, DeletionPreview>;
  onError: (error: unknown) => void;
  /** Browser speech, only when the person has the grant (HUB-77). */
  speech?: NonNullable<ExternalStoreAdapter["adapters"]>["speech"];
  dictation?: NonNullable<ExternalStoreAdapter["adapters"]>["dictation"];
}) {
  const version = useSyncExternalStore(feed.subscribe, feed.getVersion);
  useEffect(() => {
    feed.connect();
    return () => feed.disconnect();
  }, [feed]);

  const messageRepository = useMemo(() => {
    const repo = toRepository(feed.messages(), feed.headId(), { ...ctx, conversationId: feed.conversationId ?? "" });
    return { headId: feed.headId() ?? repo.headId, messages: repo.messages };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feed, version, ctx]);

  const latest = useRef(feed);
  latest.current = feed;

  const send = useCallback(
    async (message: AppendMessage, lane: "queue" | "steer") => {
      try {
        const f = await ensureFeed();
        await f.submit(contentOf(message), message.content.filter((p) => p.type === "text").map((p) => (p as { text: string }).text).join("\n\n"), lane);
      } catch (error) {
        onError(error);
      }
    },
    [ensureFeed, onError],
  );

  const queueItems = feed.myQueue;
  const toState = (q: QueueItem) => {
    const text = (q.content ?? []).filter((p) => p.type === "text").map((p) => (p as { text: string }).text).join("\n\n");
    return { id: q.id, prompt: text, parts: [{ type: "text" as const, text }] };
  };

  const adapter: ExternalStoreAdapter = {
    messageRepository,
    isRunning: !!feed.activeRun || !!feed.pending,
    isLoading: feed.loading,
    isDisabled: !!feed.snapshot && !feed.snapshot.permissions.write,
    isSendDisabled: false,
    onNew: (message) => send(message, laneFor(message)),
    queue: {
      items: queueItems.filter((q) => q.lane === "queue").map(toState),
      steerItems: queueItems.filter((q) => q.lane === "steer").map(toState),
      enqueue: (message) => void send(message, laneFor(message)),
      steer: (message) => void send(message, laneFor(message)),
      move: (id, placement) => {
        const item = latest.current.myQueue.find((q) => q.id === id);
        if (!item) return;
        const unanchored = !placement.insertAfter && !placement.insertBefore;
        void latest.current
          .patchQueue(item, { action: "move", lane: placement.lane ?? item.lane, before_id: placement.insertBefore ?? null })
          .then(() => (unanchored && placement.lane === "steer" ? latest.current.cancel(false) : undefined))
          .catch(onError);
      },
      edit: (id, message) => {
        const item = latest.current.myQueue.find((q) => q.id === id);
        if (item) void latest.current.patchQueue(item, { action: "edit", content: contentOf(message) }).catch(onError);
      },
      remove: (id) => {
        const item = latest.current.myQueue.find((q) => q.id === id);
        if (item) void latest.current.patchQueue(item, { action: "remove" }).catch(onError);
      },
      // The server parks the queue on cancel; nothing to do here.
      __internal_notifyCancelled: () => {},
    },
    onEdit: async (message) => {
      const f = latest.current;
      const target = message.sourceId;
      if (!target) return;
      const role = f.snapshot?.messages.find((m) => m.id === target)?.role;
      const mode = role === "user" && message.startRun !== false ? "generate" : "save";
      try {
        await f.operation({ kind: "revise", target_message_id: target, mode, content: contentOf(message) });
        await f.load();
      } catch (error) {
        onError(error);
      }
    },
    onReload: async (parentId, config) => {
      if (!parentId) return;
      const refinement = (config.runConfig?.custom as { refinement?: string } | undefined)?.refinement;
      try {
        await latest.current.operation({ kind: "regenerate", target_message_id: parentId, ...(refinement ? { refinement } : {}) });
        await latest.current.load();
      } catch (error) {
        onError(error);
      }
    },
    onResume: async () => {
      const f = latest.current;
      const head = f.snapshot?.view.view_head_id;
      const msg = f.snapshot?.messages.find((m) => m.id === head);
      if (!msg || msg.role !== "assistant") return;
      try {
        await f.operation({ kind: "continue", target_message_id: msg.id });
        await f.load();
      } catch (error) {
        onError(error);
      }
    },
    onCancel: async () => {
      try {
        await latest.current.cancel(true);
      } catch (error) {
        onError(error);
      }
    },
    onDelete: async (messageId) => {
      const preview = deleteGate(confirmedDeletes, messageId);
      await latest.current.operation({
        kind: "delete",
        target_message_id: messageId,
        expected_revision: preview.graph_revision,
        preview_hash: preview.preview_hash,
      });
      await latest.current.load();
    },
    unstable_onBranchChange: (event) => void latest.current.setView(event.headId),
    // Branch switching needs it; the graph itself is never written from here.
    setMessages: (_messages: readonly ThreadMessage[]) => {},
    onRefetchThread: () => latest.current.load(),
    adapters: { attachments, speech, dictation },
  };
  return useExternalStoreRuntime(adapter);
}
