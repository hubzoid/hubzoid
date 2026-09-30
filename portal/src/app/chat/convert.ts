// Stored conversations (contract 6.2 and 6.4) as assistant-ui messages. The
// server keeps a tree (parent ids) plus the branch the person last viewed
// (head_id); assistant-ui wants parents before children and a leaf as head.
import type {
  CompleteAttachment,
  ExportedMessageRepository,
  MessageStatus,
  ThreadAssistantMessagePart,
  ThreadMessage,
} from "@assistant-ui/react";
import { hubUrl, enc } from "../lib/api";
import { toDate } from "../lib/format";
import type { FileRef, ServerMessage, ServerPart } from "../lib/types";
import { isErrorResult, safeStringify, toArgs } from "./stream";

export const FILE_DATA_NAME = "hubzoid-file";

export type ConvertContext = { conversationId: string; apiBase?: string };

export function fileUrl(ctx: ConvertContext, fileId: string): string {
  return hubUrl(ctx.apiBase, `/api/conversations/${enc(ctx.conversationId)}/files/${enc(fileId)}`);
}

/** A completed upload as an assistant-ui attachment. The FileRef travels in a
 *  data part so a resend or an edit can name the file again. */
export function attachmentFromFile(ref: FileRef, previewUrl?: string): CompleteAttachment {
  const isImage = ref.kind === "image" || ref.mime?.startsWith("image/");
  return {
    id: ref.file_id,
    type: isImage ? "image" : "file",
    name: ref.name,
    contentType: ref.mime,
    status: { type: "complete" },
    content: [
      { type: "data", name: FILE_DATA_NAME, data: { ...ref, kind: isImage ? "image" : "file", previewUrl } },
    ],
  };
}

/** The FileRef inside an attachment we created (or null for anything else). */
export function fileRefOf(attachment: { content?: readonly unknown[] }): (FileRef & { previewUrl?: string }) | null {
  for (const part of attachment.content ?? []) {
    const p = part as { type?: string; name?: string; data?: FileRef };
    if (p?.type === "data" && p.name === FILE_DATA_NAME && p.data?.file_id) return p.data;
  }
  return null;
}

function partsOf(content: ServerMessage["content"]): ServerPart[] {
  if (typeof content === "string") return content ? [{ type: "text", text: content }] : [];
  return Array.isArray(content) ? content : [];
}

const str = (v: unknown) => (typeof v === "string" ? v : "");

function assistantParts(parts: ServerPart[], ctx: ConvertContext): ThreadAssistantMessagePart[] {
  const out: ThreadAssistantMessagePart[] = [];
  for (const part of parts) {
    const p = part as Record<string, unknown>;
    if (p.type === "text") {
      const text = str(p.text);
      if (text) out.push({ type: "text", text });
    } else if (p.type === "reasoning") {
      out.push({ type: "reasoning", text: str(p.text) });
    } else if (p.type === "tool-call") {
      const args = toArgs(p.args);
      const hasResult = p.result !== undefined && p.result !== null;
      out.push({
        type: "tool-call",
        toolCallId: str(p.toolCallId) || `tool-${out.length}`,
        toolName: str(p.toolName) || "tool",
        args: args as never,
        argsText: safeStringify(args),
        ...(hasResult ? { result: p.result, isError: p.isError === true || isErrorResult(p.result) } : {}),
      });
    } else if ((p.type === "file" || p.type === "image") && str(p.file_id)) {
      // A file the agent attached: shown as a download link.
      const name = str(p.name) || "file";
      out.push({ type: "text", text: `[${name}](${fileUrl(ctx, str(p.file_id))})` });
    }
  }
  return out;
}

function statusOf(message: ServerMessage): MessageStatus {
  switch (message.status) {
    case "running":
      return { type: "running" };
    case "cancelled":
      return { type: "incomplete", reason: "cancelled" };
    case "error":
      return { type: "incomplete", reason: "error", error: message.error || "" };
    default:
      return { type: "complete", reason: "unknown" };
  }
}

export function toThreadMessage(message: ServerMessage, ctx: ConvertContext): ThreadMessage | null {
  const createdAt = toDate(message.created_at) ?? new Date();
  const parts = partsOf(message.content);
  if (message.role === "user") {
    const text = parts
      .filter((p) => p.type === "text")
      .map((p) => str((p as { text?: unknown }).text))
      .join("\n\n");
    const attachments = parts
      .filter((p) => (p.type === "file" || p.type === "image") && str((p as { file_id?: unknown }).file_id))
      .map((p) => {
        const f = p as unknown as FileRef & { type: string };
        return attachmentFromFile({
          file_id: f.file_id,
          name: f.name || "file",
          mime: f.mime || "",
          size: Number(f.size) || 0,
          kind: f.type === "image" || f.mime?.startsWith("image/") ? "image" : "file",
        });
      });
    return {
      id: message.id,
      role: "user",
      createdAt,
      content: [{ type: "text", text }],
      attachments,
      metadata: { custom: {} },
    };
  }
  if (message.role === "assistant") {
    return {
      id: message.id,
      role: "assistant",
      createdAt,
      content: assistantParts(parts, ctx),
      status: statusOf(message),
      metadata: {
        unstable_state: null,
        unstable_annotations: [],
        unstable_data: [],
        steps: [],
        custom: {},
      },
    };
  }
  return null;
}

/**
 * The whole tree for assistant-ui: parents before children (whatever order the
 * server used), orphans re-rooted, and a leaf as head. `resetHead` truncates
 * below the head, so a head with children is walked down to its newest leaf.
 */
export function toRepository(
  messages: ServerMessage[],
  headId: string | null | undefined,
  ctx: ConvertContext,
): ExportedMessageRepository & { runningIds: string[] } {
  const byId = new Map<string, ServerMessage>();
  for (const m of messages) if (m && m.id && (m.role === "user" || m.role === "assistant")) byId.set(m.id, m);

  const order = (a: ServerMessage, b: ServerMessage) =>
    (toDate(a.created_at)?.getTime() ?? 0) - (toDate(b.created_at)?.getTime() ?? 0);
  const children = new Map<string | null, ServerMessage[]>();
  for (const m of byId.values()) {
    const parent = m.parent_id && byId.has(m.parent_id) ? m.parent_id : null;
    const list = children.get(parent) ?? [];
    list.push(m);
    children.set(parent, list);
  }
  for (const list of children.values()) list.sort(order);

  const items: ExportedMessageRepository["messages"] = [];
  const runningIds: string[] = [];
  const visit = (parent: string | null) => {
    for (const m of children.get(parent) ?? []) {
      const message = toThreadMessage(m, ctx);
      if (!message) continue;
      items.push({ parentId: parent, message });
      if (m.role === "assistant" && m.status === "running") runningIds.push(m.id);
      visit(m.id);
    }
  };
  visit(null);

  let head: string | null = headId && byId.has(headId) ? headId : null;
  if (!head) {
    const newest = [...byId.values()].sort(order).pop();
    head = newest?.id ?? null;
  }
  while (head) {
    const kids = children.get(head);
    if (!kids || kids.length === 0) break;
    head = kids[kids.length - 1].id;
  }
  return { headId: head, messages: items, runningIds };
}

/** Messages of one branch (a share snapshot) as a flat thread. */
export function toThreadMessages(messages: ServerMessage[], ctx: ConvertContext): ThreadMessage[] {
  // A snapshot without parent ids is already in reading order.
  if (!messages.some((m) => m && m.parent_id))
    return messages.map((m) => toThreadMessage(m, ctx)).filter((m): m is ThreadMessage => m !== null);
  const repo = toRepository(messages, null, ctx);
  // A share is one branch already; keep the path from the root to the head.
  const byId = new Map(repo.messages.map((m) => [m.message.id, m]));
  const path: ThreadMessage[] = [];
  let cursor = repo.headId ? byId.get(repo.headId) : undefined;
  while (cursor) {
    path.unshift(cursor.message);
    cursor = cursor.parentId ? byId.get(cursor.parentId) : undefined;
  }
  return path;
}
