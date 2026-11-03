// One upload per file: POST /api/chat/files with the file as multipart field
// "file". The server checks the type against the bytes, so a returned file is
// ready (or refused with a reason).
import { errorFrom, hubUrl } from "./api";

export type FileDescriptor = {
  file_id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
  content_sha256: string;
  state: "validating" | "ready" | "quarantined" | "deleted";
  processing_error: string | null;
};

export async function uploadFile(
  file: File,
  opts: { apiBase?: string; onProgress?: (fraction: number) => void } = {},
): Promise<FileDescriptor> {
  const form = new FormData();
  form.append("file", file, file.name);
  const res = await fetch(hubUrl(opts.apiBase ?? "", "/api/chat/files"), {
    method: "POST",
    credentials: "same-origin",
    headers: { accept: "application/json" },
    body: form,
  });
  if (!res.ok) throw await errorFrom(res);
  opts.onProgress?.(1);
  return (await res.json()) as FileDescriptor;
}
