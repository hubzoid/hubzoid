// How the parts of a message look: tool calls as collapsible entries, reasoning
// as a quiet panel (collapsed by default), and attached files and images.
import { createContext, useContext, useId, useState, type ReactNode } from "react";
import type {
  Attachment,
  ReasoningMessagePartComponent,
  ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import { AttachmentPrimitive } from "@assistant-ui/react";
import { Check, ChevronDown, CircleSlash, FileText, Loader2, Wrench, X, XCircle } from "lucide-react";
import { t } from "../i18n/en";
import { formatBytes, truncate } from "../lib/format";
import { cx } from "../components/ui";
import { fileRefOf, fileUrl } from "./convert";
import { safeStringify } from "./stream";

/** Where this thread's files live, for previews and downloads. */
export const FilesContext = createContext<{ conversationId: string | null; apiBase: string }>({
  conversationId: null,
  apiBase: "",
});

function shortValue(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(truncate(value, 48));
  if (value === null || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `[${value.length}]`;
  if (typeof value === "object") return "{…}";
  return "";
}

export function summarizeArgs(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const entries = Object.entries(args as Record<string, unknown>).filter(([, v]) => v !== undefined);
  if (entries.length === 0) return "";
  if (entries.length === 1 && typeof entries[0][1] === "string") return truncate(entries[0][1] as string, 64);
  return truncate(
    entries
      .slice(0, 3)
      .map(([k, v]) => `${k}: ${shortValue(v)}`)
      .join(", "),
    72,
  );
}

function resultError(result: unknown): string | null {
  if (!result || typeof result !== "object") return typeof result === "string" && result ? result : null;
  const r = result as { message?: unknown; error?: unknown; errorText?: unknown };
  for (const v of [r.message, r.error, r.errorText]) if (typeof v === "string" && v.trim()) return v.trim();
  return null;
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return safeStringify(value);
  }
}

type ToolState = "running" | "done" | "failed" | "stopped";

export const ToolEntry: ToolCallMessagePartComponent = ({ toolName, args, result, isError, status }) => {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  let state: ToolState;
  if (result !== undefined) state = isError ? "failed" : "done";
  else if (status?.type === "running" || status?.type === "requires-action") state = "running";
  else if (status?.type === "incomplete" && status.reason === "cancelled") state = "stopped";
  else if (status?.type === "incomplete") state = "failed";
  else state = "done";
  const summary = summarizeArgs(args);
  const error = state === "failed" ? resultError(result) : null;
  const hasArgs = !!args && typeof args === "object" && Object.keys(args as object).length > 0;

  const badge: Record<ToolState, ReactNode> = {
    running: (
      <span className="hz-badge bg-info-soft text-info">
        <Loader2 size={12} className="hz-spin" aria-hidden />
        {t.tools.running}
      </span>
    ),
    done: (
      <span className="hz-badge bg-success-soft text-success">
        <Check size={12} aria-hidden />
        {t.tools.done}
      </span>
    ),
    failed: (
      <span className="hz-badge bg-danger-soft text-danger">
        <XCircle size={12} aria-hidden />
        {t.tools.failed}
      </span>
    ),
    stopped: (
      <span className="hz-badge bg-sunken text-mute">
        <CircleSlash size={12} aria-hidden />
        {t.tools.stopped}
      </span>
    ),
  };

  return (
    <div className="hz-panel my-2 overflow-hidden" data-tool-state={state} data-testid="tool-entry">
      <button
        type="button"
        className="flex w-full min-w-0 items-center gap-2.5 px-3 py-2 text-left hover:bg-hover"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
      >
        <Wrench size={14} aria-hidden className="flex-none text-mute" />
        <span className="flex min-w-0 flex-1 items-baseline gap-2">
          <span className="flex-none font-mono text-[13px] font-semibold text-ink">{toolName}</span>
          {summary && <span className="min-w-0 truncate font-mono text-[12.5px] text-mute">{summary}</span>}
        </span>
        <span className="sr-only">,</span>
        {badge[state]}
        <ChevronDown
          size={15}
          aria-hidden
          className={cx("flex-none text-mute transition-transform", open && "rotate-180")}
        />
      </button>
      {error && !open && <p className="m-0 border-t border-line-soft px-3 py-2 text-[13px] text-danger">{error}</p>}
      <div id={bodyId} hidden={!open} className="border-t border-line-soft bg-canvas px-3 py-2.5">
        <div className="hz-eyebrow mb-1.5">{t.tools.input}</div>
        {hasArgs ? (
          <pre className="m-0 max-h-72 overflow-auto whitespace-pre-wrap break-words font-mono text-[12.5px] leading-relaxed text-body">
            {pretty(args)}
          </pre>
        ) : (
          <p className="m-0 text-[13px] text-mute">{t.tools.noInput}</p>
        )}
        {error && (
          <>
            <div className="hz-eyebrow mb-1.5 mt-3">{t.tools.error}</div>
            <p className="m-0 text-[13px] text-danger">{error}</p>
          </>
        )}
      </div>
    </div>
  );
};

export const Reasoning: ReasoningMessagePartComponent = ({ text, status }) => {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const running = status?.type === "running";
  const hasText = !!text && text.trim().length > 0;
  if (!hasText && !running) return null;
  if (!hasText)
    return (
      <div className="my-2 flex items-center gap-2 text-sm" data-testid="reasoning">
        <span className="hz-shimmer font-medium">{t.reasoning.thinking}</span>
      </div>
    );
  return (
    <div className="my-2" data-testid="reasoning">
      <button
        type="button"
        className="inline-flex items-center gap-1.5 rounded-md py-1 pr-1.5 text-sm font-medium text-mute hover:text-ink"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={running ? "hz-shimmer" : undefined}>{running ? t.reasoning.thinking : t.reasoning.thought}</span>
        <ChevronDown size={14} aria-hidden className={cx("transition-transform", open && "rotate-180")} />
        <span className="sr-only">{open ? t.reasoning.hide : t.reasoning.show}</span>
      </button>
      <div
        id={bodyId}
        hidden={!open}
        className="mt-1.5 whitespace-pre-wrap border-l-2 border-line py-1 pl-3.5 text-[14px] leading-relaxed text-mute"
      >
        {text}
      </div>
    </div>
  );
};

/** Grouped reasoning parts render as one panel each; nothing extra needed. */

function useAttachmentUrl(attachment: Attachment): string | null {
  const files = useContext(FilesContext);
  const ref = fileRefOf(attachment);
  const preview = (ref as { previewUrl?: string } | null)?.previewUrl;
  if (preview) return preview;
  if (attachment.file && attachment.type === "image") {
    try {
      return URL.createObjectURL(attachment.file);
    } catch {
      /* fall through */
    }
  }
  if (ref && files.conversationId) return fileUrl({ conversationId: files.conversationId, apiBase: files.apiBase }, ref.file_id);
  return null;
}

/** A file or image inside a sent message. */
export function MessageAttachment({ attachment }: { attachment: Attachment }) {
  const url = useAttachmentUrl(attachment);
  const ref = fileRefOf(attachment);
  const isImage = attachment.type === "image";
  if (isImage && url)
    return (
      <a
        href={url}
        target="_blank"
        rel="noopener"
        className="block overflow-hidden rounded-xl border border-line bg-sunken"
        aria-label={t.chat.imagePreview(attachment.name)}
      >
        <img src={url} alt={attachment.name} className="block max-h-56 max-w-[240px] object-cover" loading="lazy" />
      </a>
    );
  const body = (
    <>
      <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent-text">
        <FileText size={16} aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[13.5px] font-medium text-ink">{attachment.name}</span>
        <span className="block text-xs text-mute">
          {[ref?.mime?.split("/").pop()?.toUpperCase(), ref?.size ? formatBytes(ref.size) : ""].filter(Boolean).join(" · ") ||
            t.chat.file}
        </span>
      </span>
    </>
  );
  return url ? (
    <a
      href={url}
      target="_blank"
      rel="noopener"
      className="flex max-w-[260px] items-center gap-2.5 rounded-xl border border-line bg-raised py-2 pl-2 pr-3 no-underline hover:bg-hover"
    >
      {body}
    </a>
  ) : (
    <div className="flex max-w-[260px] items-center gap-2.5 rounded-xl border border-line bg-raised py-2 pl-2 pr-3">
      {body}
    </div>
  );
}

/** A file waiting in the composer: upload progress, errors, remove. */
export function ComposerAttachment({ attachment }: { attachment: Attachment }) {
  const url = useAttachmentUrl(attachment);
  const status = attachment.status;
  const uploading = status.type === "running";
  const failed = status.type === "incomplete";
  const pct = uploading ? Math.round(((status as { progress?: number }).progress ?? 0) * 100) : 100;
  const isImage = attachment.type === "image";
  const statusText = uploading ? t.chat.uploading(pct) : failed ? t.chat.uploadError : t.chat.uploaded;
  return (
    <div
      className={cx(
        "relative flex h-14 max-w-[240px] flex-none items-center gap-2.5 rounded-xl border bg-raised py-1.5 pl-1.5 pr-8",
        failed ? "border-danger/60" : "border-line",
      )}
      data-testid="composer-attachment"
      data-status={status.type}
    >
      {isImage && url ? (
        <img src={url} alt={t.chat.imagePreview(attachment.name)} className="h-11 w-11 flex-none rounded-lg object-cover" />
      ) : (
        <span className="flex h-11 w-11 flex-none items-center justify-center rounded-lg bg-accent-soft text-accent-text">
          <FileText size={18} aria-hidden />
        </span>
      )}
      <span className="min-w-0">
        <span className="block truncate text-[13px] font-medium text-ink">{attachment.name}</span>
        <span className={cx("block text-xs", failed ? "text-danger" : "text-mute")} aria-live="polite">
          {failed && (status as { message?: string }).message ? (status as { message?: string }).message : statusText}
        </span>
      </span>
      {uploading && (
        <span
          className="absolute inset-x-2 bottom-1 h-0.5 overflow-hidden rounded-full bg-line"
          role="progressbar"
          aria-label={attachment.name}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
        >
          <span className="block h-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
        </span>
      )}
      <AttachmentPrimitive.Remove asChild>
        <button
          type="button"
          aria-label={t.chat.removeAttachment(attachment.name)}
          className="absolute right-1 top-1 rounded-md p-1 text-mute hover:bg-hover hover:text-ink"
        >
          <X size={14} aria-hidden />
        </button>
      </AttachmentPrimitive.Remove>
    </div>
  );
}
