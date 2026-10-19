// Resumable uploads to /api/chat/files: start, send 4 MiB chunks at the
// server's offset (each with its SHA-256), complete with the file's hash, then
// wait for the type check. A lost reply is retried at the server's offset.
import { ApiError, get, hubUrl, post } from "./api";

export type FileDescriptor = {
  file_id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
  content_sha256: string;
  state: "validating" | "ready" | "quarantined" | "deleted";
  processing_error: string | null;
};

async function sha256(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function uploadFile(
  file: File,
  opts: { apiBase?: string; conversationId?: string; onProgress?: (fraction: number) => void } = {},
): Promise<FileDescriptor> {
  const base = opts.apiBase ?? "";
  const whole = await file.arrayBuffer();
  const hash = await sha256(whole);
  const up = await post<{ upload_id: string; offset: number; chunk_bytes: number }>(
    hubUrl(base, "/api/chat/files/uploads"),
    { name: file.name, mime_type: file.type, size_bytes: file.size, conversation_id: opts.conversationId ?? null,
      content_sha256: hash },
    { headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
  let offset = up.offset;
  while (offset < file.size) {
    const chunk = whole.slice(offset, offset + up.chunk_bytes);
    const res = await fetch(hubUrl(base, `/api/chat/files/uploads/${encodeURIComponent(up.upload_id)}/bytes`), {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Upload-Offset": String(offset), "Upload-Chunk-SHA256": await sha256(chunk) },
      body: chunk,
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 409 && typeof body?.detail?.offset === "number") {
      offset = body.detail.offset; // resume where the server is
      continue;
    }
    if (!res.ok) throw new ApiError(res.status, body?.detail?.code ?? "upload_failed", body?.detail?.message ?? "Upload failed.");
    offset = body.offset;
    opts.onProgress?.(offset / Math.max(1, file.size));
  }
  let desc = await post<FileDescriptor>(hubUrl(base, `/api/chat/files/uploads/${encodeURIComponent(up.upload_id)}/complete`),
    { content_sha256: hash });
  for (let i = 0; i < 60 && desc.state === "validating"; i++) {
    await new Promise((r) => setTimeout(r, 250));
    desc = await get<FileDescriptor>(hubUrl(base, `/api/chat/files/${encodeURIComponent(desc.file_id)}`));
  }
  return desc;
}
