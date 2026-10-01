// Reads the AI SDK UI message stream (v1) that POST /api/chat returns over SSE
// (contract 6.3) and folds it into assistant-ui message parts: text, reasoning
// and tool calls, in the order they started.
import type { ThreadAssistantMessagePart } from "@assistant-ui/react";

export type StreamChunk = { type: string; [key: string]: unknown };

/** Yields the chunks of each network read; stops at `[DONE]` or end of body. */
export async function* readEventStream(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamChunk[]> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: true });
      if (done) buffer += decoder.decode() + "\n\n";
      const batch: StreamChunk[] = [];
      let finished = false;
      let boundary: RegExpExecArray | null;
      const separator = /\r?\n\r?\n/g;
      let consumed = 0;
      while ((boundary = separator.exec(buffer))) {
        const block = buffer.slice(consumed, boundary.index);
        consumed = boundary.index + boundary[0].length;
        const data = block
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).replace(/^ /, ""))
          .join("\n");
        if (!data) continue;
        if (data.trim() === "[DONE]") {
          finished = true;
          break;
        }
        try {
          const chunk = JSON.parse(data);
          if (chunk && typeof chunk.type === "string") batch.push(chunk);
        } catch {
          /* ignore a malformed event rather than lose the reply */
        }
      }
      buffer = buffer.slice(consumed);
      if (batch.length) yield batch;
      if (finished || done) return;
    }
  } finally {
    reader.releaseLock();
  }
}

type MutablePart =
  | { kind: "text"; id: string; text: string }
  | { kind: "reasoning"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      toolName: string;
      args: Record<string, unknown>;
      argsText: string;
      result?: unknown;
      isError?: boolean;
    };

export type FinishStatus = "complete" | "cancelled" | "error";

/** Tool input as the object assistant-ui expects. */
export function toArgs(input: unknown): Record<string, unknown> {
  if (input && typeof input === "object" && !Array.isArray(input)) return input as Record<string, unknown>;
  if (input === undefined || input === null || input === "") return {};
  return { input };
}

export class MessageAccumulator {
  private parts: MutablePart[] = [];
  private cache: ThreadAssistantMessagePart[] = [];
  private dirty = new Set<number>();
  messageId: string | null = null;
  conversationId: string | null = null;
  title: string | null = null;
  error: string | null = null;
  finish: FinishStatus | null = null;
  started = false;

  private find(kind: MutablePart["kind"], id: string) {
    for (let i = this.parts.length - 1; i >= 0; i--) {
      const p = this.parts[i];
      if (p.kind === kind && p.id === id) return i;
    }
    return -1;
  }

  private touch(index: number) {
    this.dirty.add(index);
  }

  /**
   * The part a text or reasoning chunk belongs to. Text only ever grows at the
   * end of the message: a chunk for an id whose part is no longer last (a tool
   * call came in between, or the server reused the id) starts a new part, so
   * the reply always reads in the order it was written.
   */
  private ensureText(kind: "text" | "reasoning", id: string, start = false) {
    const last = this.parts.length - 1;
    const tail = this.parts[last];
    if (tail && tail.kind === kind && tail.id === id && !(start && tail.text !== "")) return last;
    this.parts.push({ kind, id, text: "" });
    this.touch(this.parts.length - 1);
    return this.parts.length - 1;
  }

  private ensureTool(id: string, toolName?: string) {
    let i = this.find("tool", id);
    if (i === -1) {
      this.parts.push({ kind: "tool", id, toolName: toolName || "tool", args: {}, argsText: "" });
      i = this.parts.length - 1;
      this.touch(i);
    }
    return i;
  }

  /** Apply one chunk. Returns true when the visible parts changed. */
  apply(chunk: StreamChunk): boolean {
    const before = this.dirty.size;
    const str = (key: string) => (typeof chunk[key] === "string" ? (chunk[key] as string) : "");
    switch (chunk.type) {
      case "start": {
        this.started = true;
        if (str("messageId")) this.messageId = str("messageId");
        const meta = chunk.messageMetadata as { conversationId?: string } | undefined;
        if (meta?.conversationId) this.conversationId = meta.conversationId;
        break;
      }
      case "text-start":
        this.ensureText("text", str("id"), true);
        break;
      case "text-delta": {
        const i = this.ensureText("text", str("id"));
        const delta = str("delta");
        if (delta) {
          (this.parts[i] as { text: string }).text += delta;
          this.touch(i);
        }
        break;
      }
      case "reasoning-start":
        this.ensureText("reasoning", str("id"), true);
        break;
      case "reasoning-delta": {
        const i = this.ensureText("reasoning", str("id"));
        const delta = str("delta");
        if (delta) {
          (this.parts[i] as { text: string }).text += delta;
          this.touch(i);
        }
        break;
      }
      case "tool-input-start":
        this.ensureTool(str("toolCallId"), str("toolName"));
        break;
      case "tool-input-delta": {
        const i = this.ensureTool(str("toolCallId"));
        const part = this.parts[i] as Extract<MutablePart, { kind: "tool" }>;
        part.argsText += str("inputTextDelta");
        this.touch(i);
        break;
      }
      case "tool-input-available": {
        const i = this.ensureTool(str("toolCallId"), str("toolName"));
        const part = this.parts[i] as Extract<MutablePart, { kind: "tool" }>;
        if (str("toolName")) part.toolName = str("toolName");
        part.args = toArgs(chunk.input);
        part.argsText = safeStringify(part.args);
        this.touch(i);
        break;
      }
      case "tool-input-error": {
        const i = this.ensureTool(str("toolCallId"), str("toolName"));
        const part = this.parts[i] as Extract<MutablePart, { kind: "tool" }>;
        part.args = toArgs(chunk.input);
        part.argsText = safeStringify(part.args);
        part.result = { status: "error", message: str("errorText") };
        part.isError = true;
        this.touch(i);
        break;
      }
      case "tool-output-available": {
        const i = this.ensureTool(str("toolCallId"));
        const part = this.parts[i] as Extract<MutablePart, { kind: "tool" }>;
        part.result = chunk.output ?? { status: "ok" };
        part.isError = isErrorResult(part.result);
        this.touch(i);
        break;
      }
      case "tool-output-error": {
        const i = this.ensureTool(str("toolCallId"));
        const part = this.parts[i] as Extract<MutablePart, { kind: "tool" }>;
        part.result = { status: "error", message: str("errorText") };
        part.isError = true;
        this.touch(i);
        break;
      }
      case "data-title": {
        const data = chunk.data as { title?: unknown } | undefined;
        if (typeof data?.title === "string" && data.title.trim()) this.title = data.title.trim();
        break;
      }
      case "error":
        this.error = str("errorText") || "error";
        break;
      case "abort":
        this.finish = "cancelled";
        break;
      case "finish": {
        const meta = chunk.messageMetadata as { status?: string } | undefined;
        const status = meta?.status;
        this.finish = status === "cancelled" ? "cancelled" : status === "error" ? "error" : "complete";
        break;
      }
      default:
        break; // start-step, finish-step, other data-* events
    }
    return this.dirty.size !== before;
  }

  /** Immutable parts for assistant-ui; unchanged parts keep their identity. */
  snapshot(): ThreadAssistantMessagePart[] {
    if (this.dirty.size === 0 && this.cache.length === this.parts.length) return this.cache;
    const next = this.parts.map((p, i) => {
      if (!this.dirty.has(i) && this.cache[i]) return this.cache[i];
      if (p.kind === "text") return { type: "text", text: p.text } as ThreadAssistantMessagePart;
      if (p.kind === "reasoning") return { type: "reasoning", text: p.text } as ThreadAssistantMessagePart;
      return {
        type: "tool-call",
        toolCallId: p.id,
        toolName: p.toolName,
        args: p.args,
        argsText: p.argsText,
        ...(p.result !== undefined ? { result: p.result, isError: !!p.isError } : {}),
      } as ThreadAssistantMessagePart;
    });
    this.dirty.clear();
    this.cache = next;
    return next;
  }
}

export function isErrorResult(result: unknown): boolean {
  if (!result || typeof result !== "object") return false;
  const r = result as { status?: unknown; ok?: unknown; error?: unknown };
  return r.status === "error" || r.status === "failed" || r.ok === false || (typeof r.error === "string" && !!r.error);
}

export function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return "";
  }
}
