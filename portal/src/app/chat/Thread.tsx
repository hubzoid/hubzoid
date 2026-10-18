// The conversation itself, built from assistant-ui primitives: messages with
// their parts, branch picker, copy, edit and regenerate, the composer with
// attachments, and the empty state with the agent's greeting and suggestions.
import { useEffect, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { enc, get, hubUrl, post, put } from "../lib/api";
import {
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiEvent,
  useAuiState,
  type EmptyMessagePartComponent,
  type TextMessagePartComponent,
} from "@assistant-ui/react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Paperclip,
  Pencil,
  ListOrdered,
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Play,
  RefreshCw,
  Square,
  Trash2,
  Upload,
  X,
  FileText,
  FolderOpen,
  Globe,
} from "lucide-react";
import { t } from "../i18n/en";
import { displayName, formatBytes } from "../lib/format";
import type { Agent, FileRef } from "../lib/types";
import { attachmentFromFile } from "./convert";
import { announce, toast } from "../components/toast";
import { AgentAvatar, Button, ConfirmDialog, IconButton, Notice, cx } from "../components/ui";
import { useFeed } from "./feed-context";
import { useSubmitMode } from "../lib/devices";
import type { DeletionPreview } from "./runtime";
import { useApp } from "../lib/app-context";
import { MarkdownText } from "./Markdown";
import { CombineAction, CompareAction, ModelLabel, ModelPicker } from "./models";
import { PromptPicker } from "./prompts";
import { ComposerAttachment, MessageAttachment, Reasoning, ToolEntry } from "./parts";

export type ThreadProps = {
  agent: Agent;
  /** A brand-new chat with more than one agent available: it can still change agent. */
  canPickAgent: boolean;
  /** Opens the agent menu in the header. */
  onChangeAgent: () => void;
  requestedMissing?: boolean;
  readOnly?: boolean;
  /** Stop for a run this page didn't start (reloaded while running). */
  onStopFollowed?: () => void;
  isFollowing?: boolean;
  /** The branch on screen changed (a switch, or a run that ended). */
  onHeadChanged?: (headId: string | undefined) => void;
  composerDisabledReason?: string | null;
  notice?: ReactNode;
};

// On touch screens focusing the composer on load would pop up the keyboard.
const coarsePointer = typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches;

function useIsLocalRunActive() {
  return useAuiState((s) => s.thread.isRunning);
}

export function Thread(props: ThreadProps) {
  const { agent, readOnly } = props;
  const aui = useAui();
  const isEmpty = useAuiState((s) => s.thread.isEmpty);

  // Screen readers hear when a reply starts and how it ended.
  useAuiEvent({ scope: "*", event: "thread.runStart" }, () => announce(t.chat.responding));
  useAuiEvent({ scope: "*", event: "thread.runEnd" }, () => {
    setTimeout(() => {
      const last = aui.thread().getState().messages.at(-1);
      props.onHeadChanged?.(last?.id);
      const status = last?.role === "assistant" ? last.status : undefined;
      if (status?.type === "incomplete" && status.reason === "cancelled") announce(t.chat.responseStopped);
      else if (status?.type === "incomplete") announce(t.chat.responseFailed);
      else announce(t.chat.responseDone);
    }, 0);
  });
  useAuiEvent({ scope: "*", event: "message.branchSwitched" }, () => {
    setTimeout(() => props.onHeadChanged?.(aui.thread().getState().messages.at(-1)?.id), 0);
  });
  useAuiEvent({ scope: "*", event: "composer.attachmentAddError" }, (event) => {
    toast(event.message || t.errors.generic, "error");
  });

  const body = (
    <ThreadPrimitive.Viewport
      autoScroll={!isEmpty}
      className="relative flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain"
      data-testid="thread-viewport"
    >
      {isEmpty && !readOnly ? (
        <EmptyThread {...props} />
      ) : (
        <div className="mx-auto w-full max-w-[760px] flex-1 px-4 pb-6 pt-5 sm:px-6">
          <ThreadPrimitive.Messages>
            {({ message }) =>
              message.role === "user" ? (
                message.composer.isEditing ? (
                  <EditComposer />
                ) : (
                  <UserMessage readOnly={readOnly} />
                )
              ) : message.composer.isEditing ? (
                <EditComposer />
              ) : (
                <AssistantMessage agent={agent} readOnly={readOnly} />
              )
            }
          </ThreadPrimitive.Messages>
        </div>
      )}
      {!readOnly && (
        <ThreadPrimitive.ViewportFooter className="sticky bottom-0 z-10 mt-auto bg-gradient-to-t from-bg from-70% to-transparent px-3 pb-3 pt-2 sm:px-6 sm:pb-5">
          <div className="relative mx-auto w-full max-w-[780px]">
            {!isEmpty && (
            <ThreadPrimitive.ScrollToBottom asChild>
              <button
                type="button"
                aria-label={t.chat.scrollDown}
                className="absolute -top-12 left-1/2 flex h-9 w-9 -translate-x-1/2 items-center justify-center rounded-full border border-line bg-raised text-mute hover:text-ink disabled:invisible"
              >
                <ArrowDown size={16} aria-hidden />
              </button>
            </ThreadPrimitive.ScrollToBottom>
            )}
            {props.notice}
            <QueueList />
            <Composer {...props} />
          </div>
        </ThreadPrimitive.ViewportFooter>
      )}
    </ThreadPrimitive.Viewport>
  );

  return (
    <ThreadPrimitive.Root className="relative flex min-h-0 flex-1 flex-col">
      {readOnly ? (
        body
      ) : (
        <ComposerPrimitive.AttachmentDropzone className="group/drop relative flex min-h-0 flex-1 flex-col">
          {body}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-3 z-20 hidden items-center justify-center rounded-2xl border-2 border-dashed border-accent/60 bg-bg/85 group-data-[dragging=true]/drop:flex"
          >
            <div className="flex flex-col items-center gap-2 text-accent-text">
              <Upload size={26} aria-hidden />
              <span className="text-[14px] font-medium">{t.chat.dropHere}</span>
            </div>
          </div>
        </ComposerPrimitive.AttachmentDropzone>
      )}
    </ThreadPrimitive.Root>
  );
}

// ---- empty state --------------------------------------------------------------

function suggestionText(s: NonNullable<Agent["suggestions"]>[number]): { label: string; prompt: string } | null {
  if (typeof s === "string") return s.trim() ? { label: s.trim(), prompt: s.trim() } : null;
  const prompt = (s.prompt || s.text || s.title || "").trim();
  if (!prompt) return null;
  return { label: (s.title || prompt).trim(), prompt };
}

function EmptyThread({ agent, canPickAgent, onChangeAgent, requestedMissing }: ThreadProps) {
  const name = displayName(agent.name);
  // The deployment's welcome copy and workspace suggestions (its brand),
  // kept apart from the agent's own suggestions.
  const brandCopy = useApp().branding.manifest?.copy;
  const workspaceSuggestions = (brandCopy?.suggestions ?? []).filter((s) => s.trim()).slice(0, 6);
  const suggestions = (agent.suggestions ?? []).map(suggestionText).filter(Boolean).slice(0, 4) as {
    label: string;
    prompt: string;
  }[];
  return (
    <div className="mx-auto flex w-full max-w-[780px] flex-1 flex-col justify-center px-4 py-10 sm:px-6">
      {requestedMissing && (
        <Notice tone="warning" className="mb-6">
          {t.agents.requestedUnavailable(name)}
        </Notice>
      )}
      <div className="flex flex-col items-center text-center">
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={52} />
        <h1 className="m-0 mt-4 text-[19px] font-semibold leading-tight tracking-tight text-ink sm:text-[22px]">
          {t.agents.greeting(name)}
        </h1>
        {agent.description && (
          <p className="m-0 mt-2 max-w-xl text-[14px] leading-relaxed text-mute">{agent.description}</p>
        )}
        {canPickAgent && (
          <button
            type="button"
            onClick={onChangeAgent}
            className="mt-3 inline-flex items-center gap-1 rounded-md px-2 py-1 text-[13px] font-medium text-accent-text hover:bg-hover"
          >
            {t.agents.change}
            <ChevronDown size={14} aria-hidden />
          </button>
        )}
      </div>
      {(brandCopy?.welcome_title || brandCopy?.welcome_body) && (
        <div data-brand-slot="brand.welcome" className="mt-6 rounded-xl text-center">
          {brandCopy.welcome_title && <p className="m-0 text-[14px] font-medium text-ink">{brandCopy.welcome_title}</p>}
          {brandCopy.welcome_body && (
            <p className="m-0 mt-1 whitespace-pre-line text-[14px] text-mute">{brandCopy.welcome_body}</p>
          )}
        </div>
      )}
      {workspaceSuggestions.length > 0 && (
        <section aria-label={t.brand.workspaceSuggestions} className="mt-5">
          <h2 className="m-0 mb-2 text-xs font-medium uppercase tracking-wide text-mute">{t.brand.workspaceSuggestions}</h2>
          <div className="flex flex-wrap gap-2">
            {workspaceSuggestions.map((s) => (
              <ThreadPrimitive.Suggestion key={s} prompt={s} send={false} asChild>
                <button
                  type="button"
                  className="rounded-full border border-line bg-raised px-3 py-1.5 text-left text-[13px] text-body hover:bg-hover"
                >
                  {s}
                </button>
              </ThreadPrimitive.Suggestion>
            ))}
          </div>
        </section>
      )}
      {suggestions.length > 0 && (
        <section aria-label={t.agents.suggestions} className="mt-6 grid gap-2 sm:mt-8 sm:grid-cols-2 sm:gap-2.5">
          {suggestions.map((s) => (
            <ThreadPrimitive.Suggestion key={s.prompt} prompt={s.prompt} send asChild>
              <button
                type="button"
                className="rounded-xl border border-line bg-raised px-4 py-3 text-left text-[14px] leading-snug text-body transition-colors hover:border-accent/40 hover:bg-hover"
              >
                {s.label}
              </button>
            </ThreadPrimitive.Suggestion>
          ))}
        </section>
      )}
    </div>
  );
}

// ---- messages -----------------------------------------------------------------

const UserText: TextMessagePartComponent = ({ text }) => <>{text}</>;

function BranchPicker() {
  return (
    <BranchPickerPrimitive.Root hideWhenSingleBranch className="inline-flex items-center gap-0.5 text-xs text-mute">
      <BranchPickerPrimitive.Previous asChild>
        <IconButton label={t.chat.previous} className="!h-7 !w-7">
          <ChevronLeft size={15} aria-hidden />
        </IconButton>
      </BranchPickerPrimitive.Previous>
      <span className="min-w-[2.6rem] text-center tabular-nums" data-testid="branch-count">
        <BranchPickerPrimitive.Number />
        {t.chat.branchSeparator}
        <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <IconButton label={t.chat.next} className="!h-7 !w-7">
          <ChevronRight size={15} aria-hidden />
        </IconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
}

function CopyAction() {
  const copied = useAuiState((s) => s.message.isCopied);
  return (
    <ActionBarPrimitive.Copy asChild copiedDuration={1600}>
      <IconButton label={copied ? t.copied : t.chat.copyMessage} className="!h-7 !w-7">
        {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
      </IconButton>
    </ActionBarPrimitive.Copy>
  );
}

function UserMessage({ readOnly }: { readOnly?: boolean }) {
  const hasText = useAuiState((s) =>
    s.message.content.some((p) => p.type === "text" && (p as { text: string }).text.trim() !== ""),
  );
  const isLast = useAuiState((s) => s.message.isLast);
  return (
    <MessagePrimitive.Root
      className="hz-message flex flex-col items-end gap-1 pb-1 pt-4"
      data-last={isLast}
      data-role="user"
      aria-label={t.chat.userLabel}
      role="article"
    >
      <div className="flex max-w-full flex-wrap justify-end gap-2 empty:hidden">
        <MessagePrimitive.Attachments>{({ attachment }) => <MessageAttachment attachment={attachment} />}</MessagePrimitive.Attachments>
      </div>
      {hasText && (
        <div className="hz-user-bubble max-w-[min(85%,640px)]" data-testid="user-text">
          <MessagePrimitive.Parts components={{ Text: UserText }} />
        </div>
      )}
      <div className="flex min-h-7 items-center gap-0.5">
        <BranchPicker />
        <ActionBarPrimitive.Root hideWhenRunning className="hz-actions flex items-center gap-0.5">
          {!readOnly && (
            <ActionBarPrimitive.Edit asChild>
              <IconButton label={t.chat.edit} className="!h-7 !w-7">
                <Pencil size={14} aria-hidden />
              </IconButton>
            </ActionBarPrimitive.Edit>
          )}
          <CopyAction />
          {!readOnly && <DeleteAction />}
        </ActionBarPrimitive.Root>
      </div>
      {!readOnly && isLast && <RespondAction />}
      <ContextNote />
    </MessagePrimitive.Root>
  );
}

const Working: EmptyMessagePartComponent = ({ status }) => {
  if (status.type !== "running") return null;
  return (
    <div className="hz-working py-1" data-testid="working">
      <span className="hz-dots" aria-hidden>
        <span />
        <span />
        <span />
      </span>
      <span>{t.chat.working}</span>
    </div>
  );
};

function MessageStatusNote({ readOnly }: { readOnly?: boolean }) {
  const status = useAuiState((s) => (s.message.role === "assistant" ? s.message.status : null));
  if (!status || status.type !== "incomplete") return null;
  if (status.reason === "cancelled")
    return (
      <div className="mt-1 flex items-center gap-2">
        <p className="m-0 text-[13px] italic text-mute" data-testid="stopped">
          {t.chat.stopped}
        </p>
        {!readOnly && <ContinueAction />}
      </div>
    );
  const raw = (status as { error?: unknown }).error;
  const detail = typeof raw === "string" ? raw : raw && typeof raw === "object" && "message" in raw ? String((raw as { message: unknown }).message) : "";
  return (
    <Notice
      tone="error"
      className="mt-2"
      action={
        readOnly ? undefined : (
          <ActionBarPrimitive.Reload asChild>
            <Button size="sm" icon={<RefreshCw size={14} aria-hidden />}>
              {t.retry}
            </Button>
          </ActionBarPrimitive.Reload>
        )
      }
    >
      {detail && detail !== t.chat.runError ? t.chat.runErrorDetail(detail) : t.chat.runError}
    </Notice>
  );
}

function AssistantMessage({ agent, readOnly }: { agent: Agent; readOnly?: boolean }) {
  const isLast = useAuiState((s) => s.message.isLast);
  const running = useAuiState((s) => s.message.role === "assistant" && s.message.status.type === "running");
  const hasContent = useAuiState((s) => s.message.content.length > 0);
  const watermark = useApp().branding.manifest?.presentation.response_watermark;
  return (
    <MessagePrimitive.Root
      className="hz-message flex gap-3 pb-3 pt-2"
      data-last={isLast}
      data-role="assistant"
      aria-label={t.chat.assistantLabel(displayName(agent.name))}
      aria-busy={running || undefined}
      role="article"
    >
      <div className="hidden pt-0.5 sm:block">
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={24} />
      </div>
      <div className="min-w-0 flex-1">
        <MessagePrimitive.Parts
          components={{
            Text: MarkdownText,
            Reasoning,
            tools: { Fallback: ToolEntry },
            Empty: Working,
          }}
        />
        {running && hasContent && (
          <span className="hz-dots mt-1 inline-flex" aria-hidden data-testid="streaming">
            <span />
            <span />
            <span />
          </span>
        )}
        <MessageStatusNote readOnly={readOnly} />
        <ContextNote />
        {!readOnly && isLast && !running && <FollowUps />}
        <div className="-ml-1.5 mt-1 flex min-h-7 items-center gap-0.5">
          <BranchPicker />
          <ActionBarPrimitive.Root hideWhenRunning className="hz-actions flex items-center gap-0.5">
            <CopyAction />
            {!readOnly && (
              <ActionBarPrimitive.Reload asChild>
                <IconButton label={t.chat.regenerate} className="!h-7 !w-7">
                  <RefreshCw size={14} aria-hidden />
                </IconButton>
              </ActionBarPrimitive.Reload>
            )}
            {!readOnly && (
              <ActionBarPrimitive.Edit asChild>
                <IconButton label={t.chat.editAnswer} className="!h-7 !w-7">
                  <Pencil size={14} aria-hidden />
                </IconButton>
              </ActionBarPrimitive.Edit>
            )}
            {!readOnly && <ReadAloud />}
            {!readOnly && <FeedbackActions />}
            {!readOnly && <CompareAction />}
            {!readOnly && <CombineAction />}
            {!readOnly && <DeleteAction />}
          </ActionBarPrimitive.Root>
          <ModelLabel />
          {watermark && !running && (
            <span className="ml-auto truncate pl-2 text-[11.5px] text-mute" data-testid="response-watermark">
              {watermark}
            </span>
          )}
        </div>
      </div>
    </MessagePrimitive.Root>
  );
}

/** Files on the message being edited go with the new version unless removed. */
function EditAttachments() {
  const count = useAuiState((s) => s.composer.attachments.length);
  if (!count) return null;
  return (
    <div className="flex gap-2 overflow-x-auto px-1 pb-2 pt-1">
      <ComposerPrimitive.Attachments>{({ attachment }) => <ComposerAttachment attachment={attachment} />}</ComposerPrimitive.Attachments>
    </div>
  );
}

function EditComposer() {
  const aui = useAui();
  const role = useAuiState((s) => s.message.role);
  return (
    <MessagePrimitive.Root className={cx("flex py-3", role === "user" ? "justify-end" : "justify-start")} data-role={`${role}-editing`}>
      <ComposerPrimitive.Root className="w-full max-w-[min(90%,640px)] rounded-2xl border border-accent/60 bg-raised p-2">
        <EditAttachments />
        <ComposerPrimitive.Input
          autoFocus
          submitMode="enter"
          aria-label={t.chat.edit}
          className="block max-h-72 min-h-[44px] w-full resize-none bg-transparent px-2 py-1.5 text-[14px] leading-relaxed text-ink outline-none"
        />
        <div className="flex justify-end gap-2 px-1 pb-0.5 pt-1">
          <ComposerPrimitive.Cancel asChild>
            <Button size="sm" variant="ghost">
              {t.chat.editCancel}
            </Button>
          </ComposerPrimitive.Cancel>
          <Button size="sm" variant={role === "user" ? "secondary" : "primary"} onClick={() => aui.composer().send({ startRun: false })} data-testid="edit-save-only">
            {t.chat.editSaveOnly}
          </Button>
          {role === "user" && (
            <ComposerPrimitive.Send asChild>
              <Button size="sm" variant="primary" data-testid="edit-save-generate">
                {t.chat.editSave}
              </Button>
            </ComposerPrimitive.Send>
          )}
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
}

// ---- composer -----------------------------------------------------------------

/** Let this turn use the runtime's own web search (shown only when the
 * deployment allows it and the person holds the grant). */
function WebSearchToggle() {
  const value = useFeed();
  const [on, setOn] = useState(false);
  if (!value?.features?.web_search) return null;
  return (
    <IconButton
      label={on ? t.chat.webSearchOn : t.chat.webSearchOff}
      size="lg"
      aria-pressed={on}
      className={cx("!h-9 !w-9", on && "!bg-accent/15 !text-accent")}
      onClick={() => {
        setOn(!on);
        value.feed.webSearch = !on;
      }}
    >
      <Globe size={18} aria-hidden />
    </IconButton>
  );
}

type MyFile = { file_id: string; name: string; mime_type: string; size_bytes: number; state: string; created_at: number };

/** Attach a file you uploaded before (here or in another conversation): a new
 * reference to the same bytes, no upload. Lists only files you can read. */
function ReuseFile() {
  const aui = useAui();
  const value = useFeed();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<MyFile[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setItems(null);
    get<{ items: MyFile[] }>(hubUrl(value?.apiBase ?? "", "/api/chat/files?limit=30"))
      .then((out) => setItems(out.items.filter((f) => f.state === "ready")))
      .catch(() => setItems([]));
  }, [open, value?.apiBase]);
  if (!value?.ensureConversation) return null;
  const pick = async (f: MyFile) => {
    setBusy(f.file_id);
    try {
      const cid = await value.ensureConversation!();
      const ref = await post<FileRef & { id: string }>(
        hubUrl(value.apiBase ?? "", `/api/conversations/${enc(cid)}/files/reuse`), { file_id: f.file_id });
      const att = attachmentFromFile(ref);
      await aui.composer().addAttachment({ id: att.id, type: att.type, name: att.name, contentType: att.contentType,
        content: att.content });
      setOpen(false);
    } catch (e) {
      value.onError(e);
    } finally {
      setBusy(null);
    }
  };
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <IconButton label={t.chat.reuseFile} size="lg" className="!h-9 !w-9">
          <FolderOpen size={18} aria-hidden />
        </IconButton>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="hz-menu w-72" side="top" align="start" sideOffset={6}>
          <div className="px-2 pb-1 pt-1.5 text-[12px] font-semibold text-mute">{t.chat.reuseTitle}</div>
          {items === null && <div className="px-2 py-2 text-[13px] text-mute">{t.loading}</div>}
          {items?.length === 0 && <div className="px-2 py-2 text-[13px] text-mute">{t.chat.reuseEmpty}</div>}
          <div className="max-h-64 overflow-auto">
            {items?.map((f) => (
              <button key={f.file_id} type="button" className="hz-menu-item w-full" disabled={!!busy} onClick={() => void pick(f)}>
                <FileText size={14} aria-hidden />
                <span className="min-w-0 flex-1 truncate text-left">{f.name}</span>
                <span className="text-[11px] text-mute">{formatBytes(f.size_bytes)}</span>
              </button>
            ))}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function Composer({ agent, onStopFollowed, isFollowing, composerDisabledReason }: ThreadProps) {
  const aui = useAui();
  const submitMode = useSubmitMode();
  // Shared text (the install share target) lands in the draft, never sent.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const shared = ["share_title", "share_text", "share_url"].map((k) => params.get(k)).filter(Boolean).join("\n");
    if (!shared) return;
    aui.composer().setText(shared);
    history.replaceState(null, "", location.pathname);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const running = useIsLocalRunActive();
  const hasAttachments = useAuiState((s) => s.composer.attachments.length > 0);
  const name = displayName(agent.name);
  const disabled = !!composerDisabledReason;

  const stop = () => {
    if (isFollowing && onStopFollowed) onStopFollowed();
    else aui.thread().cancelRun();
  };

  return (
    <ComposerPrimitive.Root
      className={cx(
        "rounded-2xl border border-line bg-raised transition-colors focus-within:border-[color-mix(in_srgb,var(--hz-accent)_55%,var(--hz-line))]",
        disabled && "opacity-70",
      )}
      data-testid="composer"
    >
      {hasAttachments && (
        <div className="flex gap-2 overflow-x-auto px-3 pt-3" aria-label={t.chat.attach}>
          <ComposerPrimitive.Attachments>{({ attachment }) => <ComposerAttachment attachment={attachment} />}</ComposerPrimitive.Attachments>
        </div>
      )}
      <ComposerPrimitive.Input
        autoFocus={!coarsePointer}
        submitMode={submitMode}
        cancelOnEscape={false}
        disabled={disabled}
        placeholder={disabled ? composerDisabledReason! : t.chat.placeholder(name)}
        aria-label={t.chat.placeholder(name)}
        maxRows={12}
        className="block max-h-[40dvh] min-h-[44px] w-full resize-none bg-transparent px-3.5 pb-1 pt-3 text-[14px] leading-relaxed text-ink outline-none placeholder:text-mute"
      />
      <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5">
        <div className="hz-composer-tools flex min-w-0 flex-1 items-center gap-0.5">
          <ComposerPrimitive.AddAttachment asChild disabled={disabled}>
            <IconButton label={t.chat.attach} size="lg" className="!h-9 !w-9">
              <Paperclip size={18} aria-hidden />
            </IconButton>
          </ComposerPrimitive.AddAttachment>
          {!disabled && <ReuseFile />}
          {!disabled && <PromptPicker />}
          {!disabled && <WebSearchToggle />}
          {!disabled && <ModelPicker />}
          {!disabled && <Dictation />}
        </div>
        <div className="flex flex-none items-center gap-1.5">
          {running && (
            <IconButton
              label={t.chat.stop}
              onClick={stop}
              className="!h-8 !w-8 !rounded-full !bg-ink !text-bg hover:!bg-ink/85"
              data-testid="stop"
            >
              <Square size={12} fill="currentColor" aria-hidden />
            </IconButton>
          )}
          <ComposerPrimitive.Send asChild>
            <IconButton
              label={running ? t.chat.queueSend : t.chat.send}
              className="!h-8 !w-8 !rounded-full !bg-accent !text-accent-ink hover:!bg-accent-hover disabled:!bg-sunken disabled:!text-mute disabled:!opacity-100"
              data-testid="send"
            >
              <ArrowUp size={16} aria-hidden />
            </IconButton>
          </ComposerPrimitive.Send>
        </div>
      </div>
    </ComposerPrimitive.Root>
  );
}

// ---- server queue, continue, delete --------------------------------------------

/** Follow-ups queued on this branch, in the order the server will send them. */
function QueueList() {
  const value = useFeed();
  if (!value) return null;
  const { feed, onError } = value;
  const items = [...feed.myQueue].sort((a, b) => (a.lane === b.lane ? 0 : a.lane === "steer" ? -1 : 1));
  const paused = !!feed.viewedBranch?.queue_paused && items.length > 0;
  if (!items.length) return null;
  const text = (parts?: { type: string; text?: string }[]) =>
    (parts ?? []).filter((p) => p.type === "text").map((p) => p.text ?? "").join(" ");
  return (
    <section className="hz-queue mb-2 rounded-xl border border-line bg-raised px-3 py-2" aria-label={t.chat.queueTitle(items.length)} data-testid="queue">
      <div className="mb-1 flex items-center gap-2 text-[12px] text-mute">
        <ListOrdered size={13} aria-hidden />
        <span className="flex-1">{paused ? t.chat.queuePaused : t.chat.queueTitle(items.length)}</span>
        {paused && (
          <Button size="sm" variant="ghost" icon={<Play size={12} aria-hidden />} onClick={() => void feed.queueControl("resume").catch(onError)}>
            {t.chat.queueResume}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => void feed.queueControl("clear").catch(onError)}>
          {t.chat.queueClear}
        </Button>
      </div>
      <ol className="m-0 list-none space-y-1 p-0">
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-2 text-[13px]" data-testid="queue-item" data-state={item.state}>
            <span className="min-w-0 flex-1 truncate text-ink">{text(item.content as { type: string; text?: string }[])}</span>
            {item.state !== "queued" && item.state !== "paused" && (
              <span className="text-[12px] text-mute">{t.chat.queueBlocked(item.reason || item.state)}</span>
            )}
            {feed.activeRun && item.lane !== "steer" && (
              <IconButton
                label={t.chat.queueSendNext}
                className="!h-6 !w-6"
                onClick={() =>
                  void feed
                    .patchQueue(item, { action: "move", lane: "steer" })
                    .then(() => feed.cancel(false))
                    .catch(onError)
                }
              >
                <ArrowUp size={12} aria-hidden />
              </IconButton>
            )}
            <IconButton label={t.chat.queueRemove} className="!h-6 !w-6" onClick={() => void feed.patchQueue(item, { action: "remove" }).catch(onError)}>
              <X size={12} aria-hidden />
            </IconButton>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** A saved message with no reply yet (a save-only edit) can be answered. */
function RespondAction() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const [busy, setBusy] = useState(false);
  const feed = value?.feed;
  const branch = feed?.viewedBranch;
  if (!feed || !branch || id.startsWith("pending") || feed.activeRun || !feed.snapshot?.permissions.write) return null;
  if (branch.head_id !== id || branch.writer_actor_id !== feed.me) return null;
  return (
    <div className="flex justify-end">
      <Button
        size="sm"
        variant="ghost"
        loading={busy}
        icon={<Play size={12} aria-hidden />}
        data-testid="respond"
        onClick={() => {
          setBusy(true);
          void feed
            .operation({ kind: "respond", branch_id: branch.id, expected_revision: branch.revision, target_message_id: id })
            .then(() => feed.load())
            .catch(value!.onError)
            .finally(() => setBusy(false));
        }}
      >
        {t.chat.respond}
      </Button>
    </div>
  );
}

function ContinueAction() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const [busy, setBusy] = useState(false);
  if (!value || id.startsWith("pending")) return null;
  return (
    <Button
      size="sm"
      variant="ghost"
      loading={busy}
      icon={<Play size={12} aria-hidden />}
      data-testid="continue"
      onClick={() => {
        setBusy(true);
        void value.feed
          .operation({ kind: "continue", target_message_id: id })
          .then(() => value.feed.load())
          .catch(value.onError)
          .finally(() => setBusy(false));
      }}
    >
      {t.chat.continueReply}
    </Button>
  );
}

/** Delete goes through a server preview the person confirms first. */
function DeleteAction() {
  const value = useFeed();
  const aui = useAui();
  const id = useAuiState((s) => s.message.id);
  const [preview, setPreview] = useState<DeletionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!value || id.startsWith("pending") || !value.feed.snapshot?.permissions.write) return null;
  const open = () => {
    setError(null);
    void value.feed.deletionPreview(id).then(setPreview).catch(value.onError);
  };
  const confirm = async () => {
    if (!preview) return;
    setBusy(true);
    value.confirmedDeletes.set(id, preview);
    try {
      await aui.message().delete();
      setPreview(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <IconButton label={t.chat.deleteMessage} className="!h-7 !w-7" onClick={open} data-testid="delete-message">
        <Trash2 size={14} aria-hidden />
      </IconButton>
      <ConfirmDialog
        open={!!preview}
        onOpenChange={(o) => !busy && !o && setPreview(null)}
        title={t.chat.deletePreviewTitle}
        body={
          preview
            ? t.chat.deletePreviewBody(preview.removed.length - 1, Object.keys(preview.reparented).length, preview.shares.length + (preview.snapshot_links?.length ?? 0))
            : ""
        }
        confirmLabel={t.chat.deleteConfirm}
        onConfirm={() => void confirm()}
        busy={busy}
        error={error}
      />
    </>
  );
}

/** A message whose earlier context was deleted says so. */
function ContextNote() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const changed = value?.feed.snapshot?.messages.find((m) => m.id === id)?.context_changed;
  if (!changed) return null;
  return <p className="m-0 mt-0.5 text-[12px] text-mute" data-testid="context-changed">{t.chat.contextChanged}</p>;
}

// ---- feedback and follow-ups ----------------------------------------------------

const REASONS = ["accurate", "useful", "missing_source", "incorrect", "other"] as const;

/** Rate this answer. A rating belongs to you and to this exact answer. */
function FeedbackActions() {
  const value = useFeed();
  const id = useAuiState((s) => s.message.id);
  const status = useAuiState((s) => (s.message.role === "assistant" ? s.message.status.type : null));
  const [mine, setMine] = useState<{ rating: string; revision: number } | null>(null);
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const conv = value?.feed.conversationId;
  const temporary = value?.feed.snapshot?.conversation.temporary;
  if (!value || !conv || id.startsWith("pending") || status === "running" || temporary) return null;
  const url = `/api/conversations/${encodeURIComponent(conv)}/messages/${encodeURIComponent(id)}/feedback`;
  const save = async (rating: "positive" | "negative", extra: Record<string, unknown> = {}) => {
    try {
      const cur = mine ?? (await get<{ feedback: { rating: string; revision: number } | null }>(url)).feedback;
      const out = await put<{ rating: string; revision: number }>(url, {
        expected_revision: cur ? cur.revision : "absent",
        rating,
        ...extra,
      });
      setMine(out);
      toast(t.chat.feedbackThanks);
    } catch (e) {
      value.onError(e);
    }
  };
  return (
    <>
      <IconButton
        label={t.chat.feedbackGood}
        className={cx("!h-7 !w-7", mine?.rating === "positive" && "!text-accent")}
        onClick={() => void save("positive")}
        data-testid="feedback-positive"
      >
        <ThumbsUp size={14} aria-hidden />
      </IconButton>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <IconButton label={t.chat.feedbackBad} className={cx("!h-7 !w-7", mine?.rating === "negative" && "!text-accent")} data-testid="feedback-negative">
            <ThumbsDown size={14} aria-hidden />
          </IconButton>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className="hz-menu w-72 space-y-2 !p-3" sideOffset={6} align="start">
            <p className="m-0 text-[13px] font-medium text-ink">{t.chat.feedbackWhat}</p>
            <div className="flex flex-wrap gap-1.5">
              {REASONS.map((r) => (
                <button
                  key={r}
                  type="button"
                  className="hz-chip"
                  aria-pressed={reasons.includes(r)}
                  onClick={() => setReasons((all) => (all.includes(r) ? all.filter((x) => x !== r) : [...all, r]))}
                >
                  {t.chat.feedbackReason[r]}
                </button>
              ))}
            </div>
            <textarea
              className="hz-input !min-h-16 w-full text-[13px]"
              placeholder={t.chat.feedbackComment}
              value={comment}
              maxLength={4000}
              onChange={(e) => setComment(e.target.value)}
            />
            <div className="flex justify-end">
              <Button
                size="sm"
                variant="primary"
                onClick={() => {
                  setOpen(false);
                  void save("negative", { reason_ids: reasons, comment: comment.trim() || null });
                }}
              >
                {t.chat.feedbackSend}
              </Button>
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </>
  );
}

/** Suggested next questions for the latest answer, when the helper is on. */
function FollowUps() {
  const value = useFeed();
  const aui = useAui();
  const [items, setItems] = useState<{ title: string; prompt: string }[]>([]);
  const branch = value?.feed.viewedBranch;
  const conv = value?.feed.conversationId;
  const key = branch ? `${branch.id}:${branch.revision}` : "";
  const enabled = !!value?.features?.helper_followups;
  useEffect(() => {
    if (!conv || !branch || !enabled) return;
    let stop = false;
    setItems([]);
    post<{ id: string; state: string; result: { items: { title: string; prompt: string }[] } | null }>("/api/chat/helpers", {
      kind: "followups",
      conversation_id: conv,
      branch_id: branch.id,
      input_revision: branch.revision,
    }, { quiet401: true })
      .then(async (job) => {
        for (let i = 0; i < 20 && !stop; i++) {
          if (job.state === "complete" && job.result) return setItems(job.result.items.slice(0, 4));
          if (job.state === "failed" || job.state === "stale") return;
          await new Promise((r) => setTimeout(r, 1000));
          const list = await get<{ items: typeof job[] }>(
            `/api/chat/helpers?conversation_id=${encodeURIComponent(conv)}&branch_id=${encodeURIComponent(branch.id)}`,
          );
          job = list.items.find((j) => j.id === job.id) ?? job;
        }
      })
      .catch(() => {
        /* helper off or unavailable: no suggestions */
      });
    return () => {
      stop = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, conv, enabled]);
  if (!items.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5" aria-label={t.chat.followUps} data-testid="followups">
      {items.map((item) => (
        <button
          key={item.prompt}
          type="button"
          className="hz-chip"
          onClick={() => {
            aui.composer().setText(item.prompt);
            aui.composer().send();
          }}
        >
          <Sparkles size={12} aria-hidden />
          {item.title}
        </button>
      ))}
    </div>
  );
}

// ---- voice (browser speech, HUB-77) -------------------------------------------------

/** Speak into the draft. The browser transcribes; nothing is sent. */
function Dictation() {
  const value = useFeed();
  const listening = useAuiState((s) => s.composer.dictation != null);
  if (!value?.features?.chat_dictation) return null;
  return listening ? (
    <ComposerPrimitive.StopDictation asChild>
      <IconButton label={t.chat.dictateStop} size="lg" className="!h-9 !w-9 !text-accent" data-testid="dictate-stop">
        <MicOff size={17} aria-hidden />
      </IconButton>
    </ComposerPrimitive.StopDictation>
  ) : (
    <ComposerPrimitive.Dictate asChild>
      <IconButton label={t.chat.dictate} size="lg" className="!h-9 !w-9" data-testid="dictate">
        <Mic size={17} aria-hidden />
      </IconButton>
    </ComposerPrimitive.Dictate>
  );
}

/** Read this answer aloud with the browser's own voice. */
function ReadAloud() {
  const value = useFeed();
  const speaking = useAuiState((s) => s.message.speech != null);
  if (!value?.features?.chat_read_aloud) return null;
  return speaking ? (
    <ActionBarPrimitive.StopSpeaking asChild>
      <IconButton label={t.chat.readAloudStop} className="!h-7 !w-7" data-testid="read-aloud-stop">
        <VolumeX size={14} aria-hidden />
      </IconButton>
    </ActionBarPrimitive.StopSpeaking>
  ) : (
    <ActionBarPrimitive.Speak asChild>
      <IconButton label={t.chat.readAloud} className="!h-7 !w-7" data-testid="read-aloud">
        <Volume2 size={14} aria-hidden />
      </IconButton>
    </ActionBarPrimitive.Speak>
  );
}
