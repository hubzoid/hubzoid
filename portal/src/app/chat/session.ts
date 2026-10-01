// One chat on screen: the conversation it writes to, the agent it talks to, and
// the three assistant-ui adapters that connect it to Hubzoid:
//   chat model  POST ${api_base}/api/chat, streamed (contract 6.2, 6.3)
//   history     the stored tree, loaded before the runtime mounts
//   attachments uploaded at once to POST ${api_base}/api/conversations/{id}/files
import type {
  AttachmentAdapter,
  ChatModelAdapter,
  ChatModelRunOptions,
  ChatModelRunResult,
  ExportedMessageRepository,
  PendingAttachment,
  ThreadHistoryAdapter,
  ThreadMessage,
} from "@assistant-ui/react";
import { t } from "../i18n/en";
import { ApiError, errorFrom, get, hubUrl, enc, patch, post, isAbort } from "../lib/api";
import { describeError } from "../lib/errors";
import { formatBytes } from "../lib/format";
import { IdMap, newConversationId, randomId } from "../lib/ids";
import type { Agent, Conversation, FileRef, RunStatus } from "../lib/types";
import { attachmentFromFile, fileRefOf, toThreadMessage } from "./convert";
import { MessageAccumulator, readEventStream } from "./stream";

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
export const MAX_FILES_PER_MESSAGE = 10;
const POLL_MS = 1500;

export type SessionEvents = {
  /** A new conversation now exists on the server (first message or upload). */
  onConversationCreated: (conversation: Conversation) => void;
  /** The server named the conversation (data-title). */
  onTitle: (conversationId: string, title: string) => void;
  /** A run ended; lists should refresh their order. */
  onRunSettled: (conversationId: string) => void;
  /** 401 in the middle of a run. */
  onUnauthorized: () => void;
};

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });

/** Poll GET /api/runs/{id} until the run leaves `running`. Yields each status. */
export async function* pollRun(apiBase: string, messageId: string, signal: AbortSignal): AsyncGenerator<RunStatus> {
  let failures = 0;
  while (!signal.aborted) {
    try {
      const status = await get<RunStatus>(hubUrl(apiBase, `/api/runs/${enc(messageId)}`), { signal });
      failures = 0;
      yield status;
      if (status && status.status && status.status !== "running") return;
    } catch (error) {
      if (isAbort(error) || signal.aborted) return;
      if (error instanceof ApiError && (error.status === 404 || error.status === 403 || error.status === 401)) {
        yield { status: "error" };
        return;
      }
      failures++;
      if (failures > 20) {
        yield { status: "error" };
        return;
      }
    }
    try {
      await sleep(POLL_MS, signal);
    } catch {
      return;
    }
  }
}

/** Ask the server to stop a run. A run that hasn't registered yet gets a retry. */
export async function cancelRun(apiBase: string, messageId: string) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await post(hubUrl(apiBase, `/api/runs/${enc(messageId)}/cancel`), undefined, { quiet401: true });
      return;
    } catch (error) {
      if (!(error instanceof ApiError) || (error.status !== 404 && error.status !== 409)) return;
      await sleep(700);
    }
  }
}

const errorStatus = (message: string) =>
  ({ type: "incomplete", reason: "error", error: message }) as const satisfies ChatModelRunResult["status"];

/** Upload one file with progress (fetch has no upload progress). */
function upload(
  url: string,
  file: File,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<FileRef> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    xhr.setRequestHeader("accept", "application/json");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      const response = new Response(xhr.responseText || null, {
        status: xhr.status,
        headers: { "content-type": xhr.getResponseHeader("content-type") || "application/json" },
      });
      if (xhr.status >= 200 && xhr.status < 300) {
        response
          .json()
          .then((data: FileRef) => resolve(data))
          .catch(() => reject(new ApiError(xhr.status, "bad_response", "")));
      } else errorFrom(response).then(reject, reject);
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "network"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

export class ChatSession {
  conversationId: string | null;
  agent: Agent;
  readonly ids = new IdMap();
  events: SessionEvents;
  /** How many files the composer holds now (set by the thread view). */
  countAttachments: () => number = () => 0;
  /** The server id of the reply streaming right now, for Stop. */
  activeReplyId: string | null = null;
  /** Files accepted by add() that the composer doesn't list yet. */
  private reservedAttachments = 0;
  private creating: Promise<string> | null = null;
  private preloaded: (ExportedMessageRepository & { runningIds?: string[] }) | null;

  constructor(options: {
    conversationId: string | null;
    agent: Agent;
    events: SessionEvents;
    repository?: (ExportedMessageRepository & { runningIds?: string[] }) | null;
  }) {
    this.conversationId = options.conversationId;
    this.agent = options.agent;
    this.events = options.events;
    this.preloaded = options.repository ?? null;
    for (const item of this.preloaded?.messages ?? []) this.ids.markPersisted(item.message.id);
  }

  get apiBase(): string {
    return this.agent.api_base || "";
  }

  get convertContext() {
    return { conversationId: this.conversationId ?? "", apiBase: this.apiBase };
  }

  /** The conversation to write to, created on first use. */
  ensureConversation(): Promise<string> {
    if (this.conversationId) return Promise.resolve(this.conversationId);
    if (!this.creating) {
      const proposed = newConversationId();
      this.creating = post<{ conversation?: Conversation }>(hubUrl(this.apiBase, "/api/conversations"), {
        agent: this.agent.id,
        id: proposed,
      })
        .then((data) => {
          const conversation: Conversation = {
            id: proposed,
            agent: this.agent.id,
            hub: this.agent.hub,
            api_base: this.agent.api_base,
            title: null,
            archived: false,
            created_at: Date.now() / 1000,
            updated_at: Date.now() / 1000,
            ...(data?.conversation ?? {}),
          };
          this.conversationId = conversation.id || proposed;
          this.events.onConversationCreated({ ...conversation, id: this.conversationId });
          return this.conversationId;
        })
        .catch((error) => {
          this.creating = null;
          throw error;
        });
    }
    return this.creating;
  }

  /** PATCH head_id so a reload opens the branch the person is looking at. */
  saveHead(localId: string | undefined) {
    if (!localId || !this.conversationId) return;
    const head = this.ids.toServer(localId);
    if (!this.ids.isPersisted(head)) return;
    patch(`/api/conversations/${enc(this.conversationId)}`, { head_id: head }, { quiet401: true }).catch(() => {
      /* the branch still shows; only the remembered head is stale */
    });
  }

  historyAdapter(): ThreadHistoryAdapter {
    return {
      load: async () => {
        const repo = this.preloaded ?? { messages: [] };
        return { headId: repo.headId ?? null, messages: repo.messages };
      },
      // The server stores every message itself as part of POST /api/chat.
      append: async () => {},
    };
  }

  chatModelAdapter(): ChatModelAdapter {
    return { run: (options: ChatModelRunOptions) => this.run(options) };
  }

  private userContent(message: ThreadMessage) {
    const content: Record<string, unknown>[] = [];
    const text = message.content
      .filter((p) => p.type === "text")
      .map((p) => (p as { text: string }).text)
      .join("\n\n");
    if (text) content.push({ type: "text", text });
    const attachments = message.role === "user" ? message.attachments : [];
    for (const attachment of attachments ?? []) {
      const ref = fileRefOf(attachment);
      if (ref)
        content.push({ type: "file", file_id: ref.file_id, name: ref.name, mime: ref.mime, size: ref.size });
    }
    return content;
  }

  private async *run(options: ChatModelRunOptions): AsyncGenerator<ChatModelRunResult, void> {
    const { messages, abortSignal } = options;
    const user = messages.at(-1);
    if (!user || user.role !== "user") {
      yield { status: errorStatus(t.chat.runError) };
      return;
    }
    let conversationId: string;
    try {
      conversationId = await this.ensureConversation();
    } catch (error) {
      if (abortSignal.aborted) return;
      yield { status: errorStatus(describeError(error, t.chat.runError)) };
      return;
    }

    const localReplyId = options.unstable_assistantMessageId ?? randomId("local_", 8);
    const userId = this.ids.toServer(user.id);
    const replyId = this.ids.toServer(localReplyId);
    const previous = messages.length >= 2 ? messages[messages.length - 2] : undefined;
    const regenerate = this.ids.isPersisted(userId);
    const body = {
      conversation_id: conversationId,
      agent: this.agent.id,
      parent_id: regenerate ? userId : previous ? this.ids.toServer(previous.id) : null,
      ...(regenerate ? {} : { message: { id: userId, content: this.userContent(user) } }),
      assistant_message_id: replyId,
    };

    // Stop (not a page change) also stops the run on the server: closing the
    // stream alone never does, by design.
    const onAbort = () => {
      const reason = abortSignal.reason as { detach?: boolean } | undefined;
      if (reason && reason.detach === true) return;
      void cancelRun(this.apiBase, this.ids.toServer(localReplyId));
    };
    abortSignal.addEventListener("abort", onAbort, { once: true });
    this.activeReplyId = replyId;

    const acc = new MessageAccumulator();
    let announcedTitle: string | null = null;
    let settled = false;
    try {
      let response: Response;
      try {
        response = await fetch(hubUrl(this.apiBase, "/api/chat"), {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify(body),
          signal: abortSignal,
        });
      } catch (error) {
        if (abortSignal.aborted || isAbort(error)) return;
        yield { status: errorStatus(t.errors.network) };
        return;
      }
      if (!response.ok || !response.body) {
        const error = await errorFrom(response);
        if (response.status === 401) this.events.onUnauthorized();
        yield { status: errorStatus(describeError(error, t.chat.runError)) };
        return;
      }

      let streamBroke = false;
      try {
        for await (const batch of readEventStream(response.body)) {
          let changed = false;
          for (const chunk of batch) changed = acc.apply(chunk) || changed;
          if (acc.started && !settled) {
            settled = true;
            this.ids.markPersisted(userId);
            if (acc.messageId && acc.messageId !== replyId) this.ids.bind(localReplyId, acc.messageId);
            this.ids.markPersisted(acc.messageId || replyId);
          }
          if (acc.title && acc.title !== announcedTitle) {
            announcedTitle = acc.title;
            this.events.onTitle(conversationId, acc.title);
          }
          if (changed) yield { content: acc.snapshot() };
        }
      } catch (error) {
        if (abortSignal.aborted || isAbort(error)) return;
        streamBroke = true;
      }

      if (!acc.finish && !acc.error) {
        // The connection closed before the end. The run keeps going on the
        // server, so follow it there instead of reporting a failure.
        if (streamBroke || acc.started) {
          yield* this.follow(this.ids.toServer(localReplyId), abortSignal, acc);
          return;
        }
        yield { status: errorStatus(t.chat.runError) };
        return;
      }
      if (acc.error || acc.finish === "error") {
        yield {
          content: acc.snapshot(),
          status: errorStatus(acc.error && acc.error !== "error" ? acc.error : t.chat.runError),
        };
      } else if (acc.finish === "cancelled") {
        yield { content: acc.snapshot(), status: { type: "incomplete", reason: "cancelled" } };
      } else {
        yield { content: acc.snapshot(), status: { type: "complete", reason: "stop" } };
      }
    } finally {
      abortSignal.removeEventListener("abort", onAbort);
      if (this.activeReplyId === replyId) this.activeReplyId = null;
      if (settled && !abortSignal.aborted) {
        this.saveHead(localReplyId);
      }
      if (this.conversationId) this.events.onRunSettled(this.conversationId);
    }
  }

  /** Follow a run on the server after the stream dropped. */
  private async *follow(
    replyId: string,
    signal: AbortSignal,
    acc: MessageAccumulator,
  ): AsyncGenerator<ChatModelRunResult, void> {
    for await (const status of pollRun(this.apiBase, replyId, signal)) {
      const message = status.message ? toThreadMessage(status.message, this.convertContext) : null;
      const content = message && message.role === "assistant" ? message.content : acc.snapshot();
      if (status.status === "running") {
        yield { content };
        continue;
      }
      if (status.status === "cancelled") yield { content, status: { type: "incomplete", reason: "cancelled" } };
      else if (status.status === "error")
        yield {
          content,
          status: errorStatus(
            (message?.role === "assistant" && message.status.type === "incomplete" && typeof message.status.error === "string" && message.status.error) ||
              t.chat.runError,
          ),
        };
      else yield { content, status: { type: "complete", reason: "stop" } };
      return;
    }
  }

  attachmentAdapter(): AttachmentAdapter {
    const uploads = new Map<string, FileRef & { previewUrl?: string }>();
    return {
      accept: "*",
      add: (state: { file: File }) => this.addAttachment(state.file, uploads),
      send: async (attachment: PendingAttachment) => {
        const ref = uploads.get(attachment.id) ?? fileRefOf(attachment);
        if (!ref) throw new Error(t.chat.uploadFailed(attachment.name));
        return { ...attachmentFromFile(ref, ref.previewUrl), id: attachment.id };
      },
      remove: async (attachment) => {
        const ref = uploads.get(attachment.id);
        if (ref?.previewUrl) URL.revokeObjectURL(ref.previewUrl);
        uploads.delete(attachment.id);
      },
    };
  }

  private async *addAttachment(
    file: File,
    uploads: Map<string, FileRef & { previewUrl?: string }>,
  ): AsyncGenerator<PendingAttachment, void> {
    if (file.size > MAX_UPLOAD_BYTES) throw new Error(t.chat.tooLarge(file.name, formatBytes(MAX_UPLOAD_BYTES)));
    // Files picked together arrive here before any of them reaches the
    // composer, so count the ones already accepted but not yet listed too.
    if (this.countAttachments() + this.reservedAttachments >= MAX_FILES_PER_MESSAGE)
      throw new Error(t.chat.tooMany(MAX_FILES_PER_MESSAGE));
    this.reservedAttachments++;
    let reserved = true;
    const release = () => {
      if (reserved) {
        reserved = false;
        this.reservedAttachments--;
      }
    };
    try {
      yield* this.uploadAttachment(file, uploads, release);
    } finally {
      release();
    }
  }

  private async *uploadAttachment(
    file: File,
    uploads: Map<string, FileRef & { previewUrl?: string }>,
    release: () => void,
  ): AsyncGenerator<PendingAttachment, void> {
    const isImage = file.type.startsWith("image/");
    const base = {
      id: randomId("a_", 12),
      type: isImage ? "image" : "file",
      name: file.name,
      contentType: file.type || "application/octet-stream",
      file,
    } as const;
    yield { ...base, status: { type: "running", reason: "uploading", progress: 0 } };
    // The composer lists it now; it counts there from here on.
    release();

    let conversationId: string;
    try {
      conversationId = await this.ensureConversation();
    } catch (error) {
      throw new Error(describeError(error, t.chat.uploadFailed(file.name)));
    }

    // Progress callbacks arrive outside the generator; queue them.
    let progress = 0;
    let wake: (() => void) | null = null;
    let result: FileRef | null = null;
    let failure: unknown = null;
    const done = upload(
      hubUrl(this.apiBase, `/api/conversations/${enc(conversationId)}/files`),
      file,
      (fraction) => {
        progress = fraction;
        wake?.();
      },
    ).then(
      (ref) => {
        result = ref;
        wake?.();
      },
      (error) => {
        failure = error;
        wake?.();
      },
    );
    let shown = -1;
    while (!result && !failure) {
      await new Promise<void>((resolve) => {
        wake = resolve;
      });
      wake = null;
      const pct = Math.min(99, Math.round(progress * 100));
      if (!result && !failure && pct !== shown) {
        shown = pct;
        yield { ...base, status: { type: "running", reason: "uploading", progress: pct / 100 } };
      }
    }
    await done;
    if (failure || !result) {
      if (failure instanceof ApiError && failure.status === 401) this.events.onUnauthorized();
      throw new Error(
        failure instanceof ApiError && failure.status === 413
          ? t.chat.tooLarge(file.name, formatBytes(MAX_UPLOAD_BYTES))
          : describeError(failure, t.chat.uploadFailed(file.name)),
      );
    }
    const uploaded = result as FileRef;
    const ref: FileRef & { previewUrl?: string } = {
      file_id: uploaded.file_id,
      name: uploaded.name || file.name,
      mime: uploaded.mime || file.type,
      size: typeof uploaded.size === "number" ? uploaded.size : file.size,
      kind: uploaded.kind || (isImage ? "image" : "file"),
      previewUrl: isImage ? URL.createObjectURL(file) : undefined,
    };
    uploads.set(base.id, ref);
    yield { ...base, status: { type: "requires-action", reason: "composer-send" } };
  }
}
