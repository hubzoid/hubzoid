// How the parts of a message look: tool calls as collapsible entries, reasoning
// as a quiet panel (collapsed by default), and attached files and images.
import { createContext, useContext, useId, useState, type ReactNode } from "react";
import type {
  Attachment,
  ReasoningMessagePartComponent,
  ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import { AttachmentPrimitive, useAuiState } from "@assistant-ui/react";
import { Wrench, ChevronDown, CircleSlash, FileText, Sparkles, X, XCircle } from "lucide-react";
import { t } from "../i18n/en";
import { formatBytes, truncate } from "../lib/format";
import { cx } from "../components/ui";
import { fileRefOf, fileUrl } from "./convert";
import { safeStringify } from "./stream";
import { RequestCard } from "./RequestCard";
import { HubChangeCard } from "./HubChangeCard";

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

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return safeStringify(value);
  }
}

type ToolState = "running" | "done" | "failed" | "stopped";

/**
 * How a tool's name reads: a tool from an MCP server, which runtimes name
 * mcp__<server>__<tool>, is its own name plus the server's ("my_<connector>"
 * for a personal connection: the connector). Other names are as they are.
 */
export function toolLabel(name: string): { tool: string; server: string | null } {
  const match = /^mcp__(.+?)__(.+)$/.exec(name);
  if (!match) return { tool: name, server: null };
  return { tool: match[2], server: match[1].replace(/^my_/, "") || null };
}

/** "search_sources" reads as "Search sources". */
export function humanTool(name: string): string {
  const words = name.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : name;
}

const stateIcon: Record<ToolState, ReactNode> = {
  running: <Wrench size={13} className="flex-none text-mute" aria-hidden />,
  done: <Wrench size={13} className="flex-none text-mute" aria-hidden />,
  failed: <XCircle size={13} className="flex-none text-danger" aria-hidden />,
  stopped: <CircleSlash size={13} className="flex-none text-mute" aria-hidden />,
};

/** A call that stopped to ask a person carries {status: "waiting", request_id}. */
function waitingRequest(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const r = result as { status?: unknown; request_id?: unknown };
  return r.status === "waiting" && typeof r.request_id === "string" ? r.request_id : null;
}

/** A hub change tool returns {hub_change: {id, ...}, message} as JSON text. */
function hubChange(result: unknown): number | null {
  let r = result;
  if (typeof r === "string" && r.startsWith('{"hub_change"')) {
    try {
      r = JSON.parse(r);
    } catch {
      return null;
    }
  }
  const id = (r as { hub_change?: { id?: unknown } } | null)?.hub_change?.id;
  return typeof id === "number" ? id : null;
}

export const ToolEntry: ToolCallMessagePartComponent = (props) => {
  const requestId = waitingRequest(props.result);
  if (requestId) return <RequestCard requestId={requestId} toolName={props.toolName} args={props.args} />;
  const changeId = hubChange(props.result);
  if (changeId !== null) return <HubChangeCard id={changeId} />;
  return <ToolActivity {...props} />;
};

const ToolActivity: ToolCallMessagePartComponent = ({ toolName, args, result, isError, status }) => {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  let state: ToolState;
  if (result !== undefined) state = isError ? "failed" : "done";
  else if (status?.type === "running" || status?.type === "requires-action") state = "running";
  else if (status?.type === "incomplete" && status.reason === "cancelled") state = "stopped";
  else if (status?.type === "incomplete") state = "failed";
  else state = "done";
  const label = toolLabel(toolName);
  const hasArgs = !!args && typeof args === "object" && Object.keys(args as object).length > 0;
  const stateText = { running: t.tools.running, done: t.tools.done, failed: t.tools.failed, stopped: t.tools.stopped }[state];

  return (
    <div className="hz-act" data-tool-state={state} data-testid="tool-entry">
      <button type="button" className="hz-act-row" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
        {stateIcon[state]}
        <span className={cx("hz-act-label", state === "running" && "is-running")}>{humanTool(label.tool)}</span>
        {label.server && (
          <span className="hz-tag is-quiet" data-testid="tool-server">
            {label.server}
          </span>
        )}
        <span className={state === "failed" || state === "stopped" ? "text-mute" : "sr-only"}>, {stateText}</span>
        <ChevronDown size={13} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
      </button>
      <div id={bodyId} hidden={!open} className="hz-act-detail">
        <div className="mb-1 text-[11.5px] text-mute">
          {t.tools.input} · <span className="font-mono">{toolName}</span>
        </div>
        {hasArgs ? (
          <pre className="hz-tech">{pretty(args)}</pre>
        ) : (
          <p className="m-0 text-[12.5px] text-mute">{t.tools.noInput}</p>
        )}
        {result !== undefined && (
          <>
            <div className="mb-1 mt-3 text-[11.5px] text-mute">{t.tools.result}</div>
            <pre className="hz-tech">{typeof result === "string" ? result : pretty(result)}</pre>
          </>
        )}
      </div>
    </div>
  );
};

/** Consecutive tool calls fold into one quiet summary row ("3 tool calls"). */
export function ToolGroup({ startIndex, endIndex, children }: { startIndex: number; endIndex: number; children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const count = endIndex - startIndex + 1;
  const pending = useAuiState((s) =>
    s.message.content
      .slice(startIndex, endIndex + 1)
      .filter((p) => p.type === "tool-call" && (p as { result?: unknown }).result === undefined).length,
  );
  const messageStatus = useAuiState((s) => s.message.status);
  const running = messageStatus?.type === "running";
  const currentTool = useAuiState((s) => {
    const calls = s.message.content.slice(startIndex, endIndex + 1);
    const current = calls.findLast((p) => p.type === "tool-call" && p.result === undefined);
    return current?.type === "tool-call" ? humanTool(toolLabel(current.toolName).tool) : "";
  });
  // A card waiting for the person is never folded away.
  const asking = useAuiState((s) =>
    s.message.content
      .slice(startIndex, endIndex + 1)
      .some((p) => p.type === "tool-call" && (!!waitingRequest((p as { result?: unknown }).result) || hubChange((p as { result?: unknown }).result) !== null)),
  );
  const failed = useAuiState((s) =>
    s.message.content.slice(startIndex, endIndex + 1).some((p) => p.type === "tool-call" && (p as { isError?: boolean }).isError),
  );
  if (count < 2 || asking) return <>{children}</>;
  const busy = running && pending > 0;
  const stopped = pending > 0 && messageStatus?.type === "incomplete" && messageStatus.reason === "cancelled";
  const hasFailure = failed || (pending > 0 && messageStatus?.type === "incomplete" && !stopped);
  const state = busy ? "running" : hasFailure ? "failed" : stopped ? "stopped" : "done";
  return (
    <div className="hz-act" data-testid="tool-group" data-tool-state={state}>
      <button type="button" className="hz-act-row" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
        {stateIcon[state]}
        <span className={cx("hz-act-label", busy && "is-running")}>{busy ? currentTool || t.tools.usingN(count) : t.tools.usedN(count)}</span>
        {(hasFailure || stopped) && <span className="text-mute">· {hasFailure ? t.tools.failed : t.tools.stopped}</span>}
        <ChevronDown size={13} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
      </button>
      <div id={bodyId} hidden={!open} className="hz-act-steps">{open && children}</div>
    </div>
  );
}

export const Reasoning: ReasoningMessagePartComponent = ({ text, status }) => {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const running = status?.type === "running";
  const hasText = !!text && text.trim().length > 0;
  if (!hasText && !running) return null;
  if (!hasText)
    return (
      <div className="hz-act" data-testid="reasoning">
        <span className="hz-act-row is-static">
          <Sparkles size={13} aria-hidden className="flex-none text-mute" />
          <span className="text-secondary">{t.reasoning.thinking}</span>
        </span>
      </div>
    );
  return (
    <div className="hz-act" data-testid="reasoning">
      <button type="button" className="hz-act-row" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
        <Sparkles size={13} aria-hidden className="flex-none text-mute" />
        <span className="text-secondary">{running ? t.reasoning.thinking : t.reasoning.thought}</span>
        <ChevronDown size={13} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
        <span className="sr-only">{open ? t.reasoning.hide : t.reasoning.show}</span>
      </button>
      <div id={bodyId} hidden={!open} className="hz-act-reason">
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

/** "PDF", "CSV", "TXT": the extension reads better than a MIME subtype. */
function fileKind(name: string, mime?: string): string {
  const ext = /\.([a-z0-9]{1,6})$/i.exec(name)?.[1];
  if (ext) return ext.toUpperCase();
  const sub = mime?.split("/").pop();
  return sub && sub.length <= 8 ? sub.toUpperCase() : "";
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
      <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-sunken text-secondary">
        <FileText size={15} aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[13px] font-medium text-ink">{attachment.name}</span>
        <span className="block text-xs text-mute">
          {[fileKind(attachment.name, ref?.mime), ref?.size ? formatBytes(ref.size) : ""].filter(Boolean).join(" · ") ||
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
        "relative flex h-[52px] max-w-[240px] flex-none items-center gap-2.5 rounded-xl border bg-raised py-1.5 pl-1.5 pr-8",
        failed ? "border-danger/60" : "border-line",
      )}
      data-testid="composer-attachment"
      data-status={status.type}
    >
      {isImage && url ? (
        <img src={url} alt={t.chat.imagePreview(attachment.name)} className="h-11 w-11 flex-none rounded-lg object-cover" />
      ) : (
        <span className="flex h-10 w-10 flex-none items-center justify-center rounded-lg bg-sunken text-secondary">
          <FileText size={16} aria-hidden />
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
