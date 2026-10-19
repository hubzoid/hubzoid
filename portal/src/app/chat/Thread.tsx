// The conversation itself, built from assistant-ui primitives: prose answers
// with a hover action bar (copy, retry, feedback and a menu for the rest),
// user bubbles, quoting a selection into the reply, the branch picker, the
// composer (+ menu, model, dictation, send that becomes Stop, and Queue or
// Interrupt while a reply streams), the flat queue above it, and the quiet
// empty state with the composer in the middle.
import { useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { DropdownMenu as Menu, Popover } from "radix-ui";
import { enc, get, hubUrl, post, put } from "../lib/api";
import {
  ActionBarPrimitive,
  BranchPickerPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  SelectionToolbarPrimitive,
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
  Braces,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  CornerDownRight,
  Download,
  FileText,
  FolderOpen,
  Globe,
  ListOrdered,
  Mic,
  MicOff,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Plus,
  Quote,
  RefreshCw,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Upload,
  Volume2,
  VolumeX,
  X,
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
import { ComposerAttachment, MessageAttachment, Reasoning, ToolEntry, ToolGroup } from "./parts";

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

  const empty = isEmpty && !readOnly;
  const body = (
    <ThreadPrimitive.Viewport
      autoScroll={!isEmpty}
      className="relative flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain"
      data-testid="thread-viewport"
    >
      {empty ? (
        <EmptyThread {...props} />
      ) : (
        <div className="hz-convo">
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
      {!readOnly && !empty && (
        <ThreadPrimitive.ViewportFooter className="hz-dock">
          <div className="relative mx-auto w-full max-w-[768px]">
            <ThreadPrimitive.ScrollToBottom asChild>
              <button type="button" aria-label={t.chat.scrollDown} className="hz-latest">
                <ArrowDown size={14} aria-hidden />
                {t.chat.latest}
              </button>
            </ThreadPrimitive.ScrollToBottom>
            {props.notice}
            <QueueList />
            <Composer {...props} />
            <p className="hz-disclaimer">{t.chat.disclaimer(displayName(agent.name))}</p>
          </div>
        </ThreadPrimitive.ViewportFooter>
      )}
      {!readOnly && (
        <SelectionToolbarPrimitive.Root className="hz-selection-bar">
          <SelectionToolbarPrimitive.Quote className="hz-selection-quote">
            <Quote size={13} aria-hidden />
            {t.chat.quoteReply}
          </SelectionToolbarPrimitive.Quote>
        </SelectionToolbarPrimitive.Root>
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
            className="pointer-events-none absolute inset-3 z-20 hidden items-center justify-center rounded-2xl border-2 border-dashed border-line bg-bg/85 group-data-[dragging=true]/drop:flex"
          >
            <div className="flex flex-col items-center gap-2 text-secondary">
              <Upload size={24} aria-hidden />
              <span className="text-[13px] font-medium">{t.chat.dropHere}</span>
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

function EmptyThread(props: ThreadProps) {
  const { agent, requestedMissing } = props;
  const name = displayName(agent.name);
  // The deployment's welcome copy and workspace suggestions (its brand),
  // kept apart from the agent's own suggestions.
  const brandCopy = useApp().branding.manifest?.copy;
  const workspaceSuggestions = (brandCopy?.suggestions ?? []).filter((s) => s.trim()).slice(0, 6);
  const suggestions = (agent.suggestions ?? []).map(suggestionText).filter(Boolean).slice(0, 4) as {
    label: string;
    prompt: string;
  }[];
  const pills = [...suggestions, ...workspaceSuggestions.map((s) => ({ label: s, prompt: s }))].slice(0, 6);
  return (
    <div className="hz-empty">
      {requestedMissing && (
        <Notice tone="warning" className="mb-6 w-full max-w-[768px]">
          {t.agents.requestedUnavailable(name)}
        </Notice>
      )}
      <h1 className="hz-empty-title">{brandCopy?.welcome_title || t.agents.greeting(name)}</h1>
      {(brandCopy?.welcome_body || agent.description) && (
        <p className="hz-empty-sub" data-brand-slot="brand.welcome">
          {brandCopy?.welcome_body || agent.description}
        </p>
      )}
      <div className="w-full max-w-[768px]">
        {props.notice}
        <Composer {...props} />
      </div>
      {pills.length > 0 && (
        <section aria-label={t.agents.suggestions} className="hz-pills">
          {pills.map((s) => (
            <ThreadPrimitive.Suggestion key={s.prompt} prompt={s.prompt} send={false} asChild>
              <button type="button" className="hz-pill">
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

/** User text, with a leading "> quote" shown as the passage it replies to. */
const UserText: TextMessagePartComponent = ({ text }) => {
  const lines = text.split("\n");
  let n = 0;
  while (n < lines.length && lines[n].startsWith("> ")) n++;
  if (!n) return <>{text}</>;
  return (
    <>
      <span className="hz-bubble-quote">{lines.slice(0, n).map((l) => l.slice(2)).join("\n")}</span>
      {lines.slice(n).join("\n").replace(/^\n+/, "")}
    </>
  );
};

function BranchPicker() {
  return (
    <BranchPickerPrimitive.Root hideWhenSingleBranch className="hz-branch">
      <BranchPickerPrimitive.Previous asChild>
        <IconButton label={t.chat.previous} className="!h-7 !w-6">
          <ChevronLeft size={14} aria-hidden />
        </IconButton>
      </BranchPickerPrimitive.Previous>
      <span className="tabular-nums" data-testid="branch-count">
        <BranchPickerPrimitive.Number />
        {t.chat.branchSeparator}
        <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <IconButton label={t.chat.next} className="!h-7 !w-6">
          <ChevronRight size={14} aria-hidden />
        </IconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
}

function CopyAction() {
  const copied = useAuiState((s) => s.message.isCopied);
  return (
    <ActionBarPrimitive.Copy asChild copiedDuration={1600}>
      <IconButton label={copied ? t.copied : t.chat.copyMessage}>
        {copied ? <Check size={15} aria-hidden /> : <Copy size={15} aria-hidden />}
      </IconButton>
    </ActionBarPrimitive.Copy>
  );
}

function UserMessage({ readOnly }: { readOnly?: boolean }) {
  const hasText = useAuiState((s) =>
    s.message.content.some((p) => p.type === "text" && (p as { text: string }).text.trim() !== ""),
  );
  const isLast = useAuiState((s) => s.message.isLast);
  const del = useDelete();
  return (
    <MessagePrimitive.Root
      className="hz-message hz-user"
      data-last={isLast}
      data-role="user"
      aria-label={t.chat.userLabel}
      role="article"
    >
      <div className="flex max-w-full flex-wrap justify-end gap-2 empty:hidden">
        <MessagePrimitive.Attachments>{({ attachment }) => <MessageAttachment attachment={attachment} />}</MessagePrimitive.Attachments>
      </div>
      {hasText && (
        <div className="hz-user-bubble" data-testid="user-text">
          <MessagePrimitive.Parts components={{ Text: UserText }} />
        </div>
      )}
      <div className="hz-bar is-user">
        <BranchPicker />
        <ActionBarPrimitive.Root hideWhenRunning className="hz-actions">
          <CopyAction />
          {!readOnly && (
            <ActionBarPrimitive.Edit asChild>
              <IconButton label={t.chat.edit}>
                <Pencil size={15} aria-hidden />
              </IconButton>
            </ActionBarPrimitive.Edit>
          )}
          {!readOnly && del.available && (
            <IconButton label={t.chat.deleteMessage} onClick={del.open} data-testid="delete-message">
              <Trash2 size={15} aria-hidden />
            </IconButton>
          )}
        </ActionBarPrimitive.Root>
      </div>
      {del.dialog}
      {!readOnly && isLast && <RespondAction />}
      <ContextNote />
    </MessagePrimitive.Root>
  );
}

const Working: EmptyMessagePartComponent = ({ status }) => {
  if (status.type !== "running") return null;
  return (
    <div className="hz-working" data-testid="working">
      <span className="hz-spin-dot" aria-hidden />
      <span>{t.chat.working}</span>
    </div>
  );
};

function MessageStatusNote({ readOnly }: { readOnly?: boolean }) {
  const status = useAuiState((s) => (s.message.role === "assistant" ? s.message.status : null));
  if (!status || status.type !== "incomplete") return null;
  if (status.reason === "cancelled")
    return (
      <div className="mt-1.5 flex items-center gap-2">
        <p className="m-0 text-[12px] text-mute" data-testid="stopped">
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
            <Button size="sm" icon={<RefreshCw size={13} aria-hidden />}>
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
  const speaking = useAuiState((s) => s.message.speech != null);
  const value = useFeed();
  const del = useDelete();
  const watermark = useApp().branding.manifest?.presentation.response_watermark;
  const canRead = !!value?.features?.chat_read_aloud;
  return (
    <MessagePrimitive.Root
      className="hz-message hz-assistant"
      data-last={isLast}
      data-role="assistant"
      aria-label={t.chat.assistantLabel(displayName(agent.name))}
      aria-busy={running || undefined}
      role="article"
    >
      <div className="hz-assistant-avatar">
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={22} />
      </div>
      <div className="min-w-0">
        <MessagePrimitive.Parts
          components={{
            Text: MarkdownText,
            Reasoning,
            tools: { Fallback: ToolEntry },
            ToolGroup,
            Empty: Working,
          }}
        />
        {running && hasContent && <span className="hz-caret" aria-hidden data-testid="streaming" />}
        <MessageStatusNote readOnly={readOnly} />
        <ContextNote />
        {!readOnly && isLast && !running && <FollowUps />}
        <div className="hz-bar">
          <BranchPicker />
          <ActionBarPrimitive.Root hideWhenRunning className="hz-actions">
            <CopyAction />
            {!readOnly && (
              <ActionBarPrimitive.Reload asChild>
                <IconButton label={t.chat.regenerate}>
                  <RefreshCw size={15} aria-hidden />
                </IconButton>
              </ActionBarPrimitive.Reload>
            )}
            {!readOnly && <FeedbackActions />}
            {!readOnly && <CompareAction />}
            {!readOnly && <CombineAction />}
            {speaking && (
              <ActionBarPrimitive.StopSpeaking asChild>
                <IconButton label={t.chat.readAloudStop} data-testid="read-aloud-stop">
                  <VolumeX size={15} aria-hidden />
                </IconButton>
              </ActionBarPrimitive.StopSpeaking>
            )}
            <Menu.Root modal={false}>
              <Menu.Trigger asChild>
                <IconButton label={t.chat.messageActions} tooltip={false} data-testid="message-more">
                  <MoreHorizontal size={15} aria-hidden />
                </IconButton>
              </Menu.Trigger>
              <Menu.Portal>
                <Menu.Content className="hz-menu w-[210px]" side="top" align="start" sideOffset={4}>
                  {!readOnly && (
                    <ActionBarPrimitive.Edit asChild>
                      <Menu.Item className="hz-menu-item">
                        <Pencil size={15} aria-hidden />
                        {t.chat.editAnswer}
                      </Menu.Item>
                    </ActionBarPrimitive.Edit>
                  )}
                  {!readOnly && canRead && !speaking && (
                    <ActionBarPrimitive.Speak asChild>
                      <Menu.Item className="hz-menu-item" data-testid="read-aloud">
                        <Volume2 size={15} aria-hidden />
                        {t.chat.readAloud}
                      </Menu.Item>
                    </ActionBarPrimitive.Speak>
                  )}
                  <ActionBarPrimitive.ExportMarkdown asChild>
                    <Menu.Item className="hz-menu-item">
                      <Download size={15} aria-hidden />
                      {t.chat.exportMarkdown}
                    </Menu.Item>
                  </ActionBarPrimitive.ExportMarkdown>
                  {!readOnly && del.available && (
                    <>
                      <Menu.Separator className="hz-menu-sep" />
                      <Menu.Item className="hz-menu-item danger" onSelect={del.open} data-testid="delete-message">
                        <Trash2 size={15} aria-hidden />
                        {t.chat.deleteMessage}
                      </Menu.Item>
                    </>
                  )}
                </Menu.Content>
              </Menu.Portal>
            </Menu.Root>
          </ActionBarPrimitive.Root>
          <ModelLabel />
          {watermark && !running && (
            <span className="ml-auto truncate pl-2 text-[11.5px] text-mute" data-testid="response-watermark">
              {watermark}
            </span>
          )}
        </div>
      </div>
      {del.dialog}
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
      <ComposerPrimitive.Root className="hz-composer is-edit">
        <EditAttachments />
        <ComposerPrimitive.Input
          autoFocus
          submitMode="enter"
          aria-label={t.chat.edit}
          className="hz-composer-input"
        />
        <div className="flex justify-end gap-1.5 pt-1">
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

type MyFile = { file_id: string; name: string; mime_type: string; size_bytes: number; state: string; created_at: number };

/** Attach a file you uploaded before (here or in another conversation): a new
 * reference to the same bytes, no upload. Lists only files you can read. */
function ReuseFile({ open, onOpenChange: setOpen, children }: { open: boolean; onOpenChange: (open: boolean) => void; children: ReactNode }) {
  const aui = useAui();
  const value = useFeed();
  const [items, setItems] = useState<MyFile[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setItems(null);
    get<{ items: MyFile[] }>(hubUrl(value?.apiBase ?? "", "/api/chat/files?limit=30"))
      .then((out) => setItems(out.items.filter((f) => f.state === "ready")))
      .catch(() => setItems([]));
  }, [open, value?.apiBase]);
  if (!value?.ensureConversation) return <>{children}</>;
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
      <Popover.Anchor asChild>{children}</Popover.Anchor>
      <Popover.Portal>
        <Popover.Content className="hz-menu w-72" side="top" align="start" sideOffset={8} aria-label={t.chat.reuseTitle}>
          <div className="hz-menu-label">{t.chat.reuseTitle}</div>
          {items === null && <div className="px-2 py-2 text-[12.5px] text-mute">{t.loading}</div>}
          {items?.length === 0 && <div className="px-2 py-2 text-[12.5px] text-mute">{t.chat.reuseEmpty}</div>}
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

/** The + menu: upload, reuse a file, a saved prompt, and web search for this chat. */
function PlusMenu({ disabled, web, onWeb }: { disabled: boolean; web: boolean | null; onWeb: (on: boolean) => void }) {
  const value = useFeed();
  const [panel, setPanel] = useState<null | "reuse" | "prompt">(null);
  const trigger = (
    <Menu.Trigger asChild disabled={disabled}>
      <IconButton label={t.chat.addMenu} size="lg" tooltip={false} data-testid="composer-add">
        <Plus size={19} aria-hidden />
      </IconButton>
    </Menu.Trigger>
  );
  return (
    <Menu.Root modal={false}>
      <PromptPicker open={panel === "prompt"} onOpenChange={(o) => setPanel(o ? "prompt" : null)}>
        <ReuseFile open={panel === "reuse"} onOpenChange={(o) => setPanel(o ? "reuse" : null)}>
          {trigger}
        </ReuseFile>
      </PromptPicker>
      <Menu.Portal>
        <Menu.Content className="hz-menu w-[250px]" side="top" align="start" sideOffset={8}>
          <ComposerPrimitive.AddAttachment asChild>
            <Menu.Item className="hz-menu-item" aria-label={t.chat.attach}>
              <Upload size={15} aria-hidden />
              {t.chat.upload}
            </Menu.Item>
          </ComposerPrimitive.AddAttachment>
          {value?.ensureConversation && (
            <Menu.Item className="hz-menu-item" onSelect={() => setPanel("reuse")}>
              <FolderOpen size={15} aria-hidden />
              {t.chat.reuseShort}
            </Menu.Item>
          )}
          <Menu.Item className="hz-menu-item" onSelect={() => setPanel("prompt")}>
            <Braces size={15} aria-hidden />
            {t.chat.promptShort}
          </Menu.Item>
          {web !== null && (
            <>
              <Menu.Separator className="hz-menu-sep" />
              <Menu.CheckboxItem className="hz-menu-item" checked={web} onCheckedChange={(c) => onWeb(c === true)} onSelect={(e) => e.preventDefault()}>
                <Globe size={15} aria-hidden />
                <span className="flex-1">{t.chat.webSearch}</span>
                <span className={cx("hz-switch", web && "is-on")} aria-hidden />
              </Menu.CheckboxItem>
            </>
          )}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

function Composer({ agent, onStopFollowed, isFollowing, composerDisabledReason }: ThreadProps) {
  const aui = useAui();
  const value = useFeed();
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
  const running = useAuiState((s) => s.thread.isRunning);
  const hasDraft = useAuiState((s) => !s.composer.isEmpty);
  const hasAttachments = useAuiState((s) => s.composer.attachments.length > 0);
  const name = displayName(agent.name);
  const disabled = !!composerDisabledReason;
  const [web, setWeb] = useState(false);
  const webAllowed = !!value?.features?.web_search;
  const setWebSearch = (on: boolean) => {
    setWeb(on);
    if (value) value.feed.webSearch = on;
  };

  const stop = () => {
    if (isFollowing && onStopFollowed) onStopFollowed();
    else aui.thread().cancelRun();
  };
  const queue = () => aui.composer().send({ steer: false });
  const interrupt = () => {
    aui.composer().send({ steer: true });
    // The steered message goes next; stopping the reply lets it go now.
    setTimeout(() => void value?.feed.cancel(false).catch(value.onError), 0);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // While a reply streams, Enter does what the visible button says: queue.
    if (running && e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && submitMode === "enter") {
      e.preventDefault();
      if (hasDraft) queue();
    }
  };

  return (
    <ComposerPrimitive.Root className={cx("hz-composer", disabled && "is-disabled")} data-testid="composer">
      <ComposerPrimitive.Quote className="hz-composer-quote">
        <Quote size={13} aria-hidden className="flex-none text-mute" />
        <ComposerPrimitive.QuoteText className="min-w-0 flex-1 truncate" />
        <ComposerPrimitive.QuoteDismiss asChild>
          <IconButton label={t.chat.removeQuote} className="!h-6 !w-6" tooltip={false}>
            <X size={13} aria-hidden />
          </IconButton>
        </ComposerPrimitive.QuoteDismiss>
      </ComposerPrimitive.Quote>
      {hasAttachments && (
        <div className="flex gap-2 overflow-x-auto px-1 pb-1 pt-0.5" aria-label={t.chat.attach}>
          <ComposerPrimitive.Attachments>{({ attachment }) => <ComposerAttachment attachment={attachment} />}</ComposerPrimitive.Attachments>
        </div>
      )}
      {web && (
        <div className="flex flex-wrap gap-1.5 px-1">
          <span className="hz-status-chip">
            <Globe size={12} aria-hidden />
            {t.chat.webOnChip}
            <button type="button" aria-label={t.chat.webSearchOn} onClick={() => setWebSearch(false)}>
              <X size={12} aria-hidden />
            </button>
          </span>
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
        onKeyDown={onKeyDown}
        className="hz-composer-input"
      />
      <div className="hz-composer-bar">
        <PlusMenu disabled={disabled} web={webAllowed ? web : null} onWeb={setWebSearch} />
        {!disabled && <ModelPicker />}
        <span className="flex-1" />
        {!disabled && <Dictation />}
        {running && hasDraft ? (
          <>
            <button type="button" className="hz-stop-ghost" onClick={stop} aria-label={t.chat.stop} data-testid="stop">
              <Square size={10} fill="currentColor" aria-hidden />
              {t.chat.stopShort}
            </button>
            <div className="hz-split">
              <button type="button" onClick={queue} data-testid="send" aria-label={t.chat.queueSend}>
                <ListOrdered size={14} aria-hidden />
                {t.chat.queue}
              </button>
              <Menu.Root modal={false}>
                <Menu.Trigger asChild>
                  <button type="button" className="hz-split-more" aria-label={t.chat.queueOptions} data-testid="send-options">
                    <ChevronDown size={14} aria-hidden />
                  </button>
                </Menu.Trigger>
                <Menu.Portal>
                  <Menu.Content className="hz-menu w-[280px]" side="top" align="end" sideOffset={8}>
                    <Menu.Item className="hz-menu-item !items-start !py-2" onSelect={queue}>
                      <ListOrdered size={15} aria-hidden className="mt-0.5" />
                      <span>
                        {t.chat.queueAfter}
                        <small>{t.chat.queueAfterHelp}</small>
                      </span>
                    </Menu.Item>
                    <Menu.Item className="hz-menu-item !items-start !py-2" onSelect={interrupt} data-testid="interrupt">
                      <CornerDownRight size={15} aria-hidden className="mt-0.5" />
                      <span>
                        {t.chat.interrupt}
                        <small>{t.chat.interruptHelp}</small>
                      </span>
                    </Menu.Item>
                  </Menu.Content>
                </Menu.Portal>
              </Menu.Root>
            </div>
          </>
        ) : running ? (
          <button type="button" className="hz-send" onClick={stop} aria-label={t.chat.stop} data-testid="stop">
            <Square size={11} fill="currentColor" aria-hidden />
          </button>
        ) : (
          <ComposerPrimitive.Send asChild>
            <button type="button" className="hz-send" aria-label={t.chat.send} data-testid="send">
              <ArrowUp size={17} aria-hidden />
            </button>
          </ComposerPrimitive.Send>
        )}
      </div>
    </ComposerPrimitive.Root>
  );
}

// ---- server queue, continue, delete --------------------------------------------

/** Follow-ups queued on this branch, in the order the server will send them. */
function QueueList() {
  const value = useFeed();
  const aui = useAui();
  if (!value) return null;
  const { feed, onError } = value;
  const items = [...feed.myQueue].sort((a, b) => (a.lane === b.lane ? 0 : a.lane === "steer" ? -1 : 1));
  const paused = !!feed.viewedBranch?.queue_paused && items.length > 0;
  if (!items.length) return null;
  const text = (parts?: { type: string; text?: string }[]) =>
    (parts ?? []).filter((p) => p.type === "text").map((p) => p.text ?? "").join(" ");
  return (
    <section className="hz-queue" aria-label={t.chat.queueTitle(items.length)} data-testid="queue">
      <div className="hz-queue-head">
        <ListOrdered size={13} aria-hidden />
        <span>{t.chat.queueCount(items.length)}</span>
        {paused && (
          <>
            <span className="text-warning">· {t.chat.queuePausedShort}</span>
            <button type="button" className="hz-link-btn" onClick={() => void feed.queueControl("resume").catch(onError)}>
              {t.chat.queueResume}
            </button>
          </>
        )}
        <span className="flex-1" />
        <Menu.Root modal={false}>
          <Menu.Trigger asChild>
            <IconButton label={t.chat.queueMenu} className="!h-6 !w-6" tooltip={false}>
              <MoreHorizontal size={14} aria-hidden />
            </IconButton>
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Content className="hz-menu w-[190px]" side="top" align="end" sideOffset={4}>
              {paused ? (
                <Menu.Item className="hz-menu-item" onSelect={() => void feed.queueControl("resume").catch(onError)}>
                  <Play size={14} aria-hidden />
                  {t.chat.queueResume}
                </Menu.Item>
              ) : (
                <Menu.Item className="hz-menu-item" onSelect={() => void feed.queueControl("pause").catch(onError)}>
                  <Pause size={14} aria-hidden />
                  {t.chat.queuePause}
                </Menu.Item>
              )}
              <Menu.Item className="hz-menu-item danger" onSelect={() => void feed.queueControl("clear").catch(onError)}>
                <Trash2 size={14} aria-hidden />
                {t.chat.queueClear}
              </Menu.Item>
            </Menu.Content>
          </Menu.Portal>
        </Menu.Root>
      </div>
      <ol className="m-0 list-none p-0">
        {items.map((item, i) => {
          const body = text(item.content as { type: string; text?: string }[]);
          return (
            <li key={item.id} className="hz-queue-row" data-testid="queue-item" data-state={item.state}>
              <span className="w-4 flex-none text-[11.5px] tabular-nums text-mute">{i + 1}</span>
              <span className="min-w-0 flex-1 truncate">{body}</span>
              {item.lane === "steer" && <span className="hz-tag">{t.chat.queueNext}</span>}
              {item.state !== "queued" && item.state !== "paused" && (
                <span className="text-[11.5px] text-mute">{t.chat.queueBlocked(item.reason || item.state)}</span>
              )}
              <span className="hz-queue-acts">
                <IconButton
                  label={t.chat.queueEdit}
                  className="!h-6 !w-6"
                  onClick={() =>
                    void feed
                      .patchQueue(item, { action: "remove" })
                      .then(() => aui.composer().setText(body))
                      .catch(onError)
                  }
                >
                  <Pencil size={12} aria-hidden />
                </IconButton>
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
                    <CornerDownRight size={12} aria-hidden />
                  </IconButton>
                )}
                <IconButton label={t.chat.queueRemove} className="!h-6 !w-6" onClick={() => void feed.patchQueue(item, { action: "remove" }).catch(onError)}>
                  <X size={12} aria-hidden />
                </IconButton>
              </span>
            </li>
          );
        })}
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

/** Delete goes through a server preview the person confirms first. The dialog
 * lives beside the message, so it outlives the menu that opened it. */
function useDelete(): { available: boolean; open: () => void; dialog: ReactNode } {
  const value = useFeed();
  const aui = useAui();
  const id = useAuiState((s) => s.message.id);
  const [preview, setPreview] = useState<DeletionPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const available = !!value && !id.startsWith("pending") && !!value.feed.snapshot?.permissions.write;
  const open = () => {
    if (!value) return;
    setError(null);
    void value.feed.deletionPreview(id).then(setPreview).catch(value.onError);
  };
  const confirm = async () => {
    if (!preview || !value) return;
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
  const dialog = available ? (
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
  ) : null;
  return { available, open, dialog };
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
        aria-pressed={mine?.rating === "positive"}
        onClick={() => void save("positive")}
        data-testid="feedback-positive"
      >
        <ThumbsUp size={15} aria-hidden />
      </IconButton>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <IconButton label={t.chat.feedbackBad} aria-pressed={mine?.rating === "negative"} data-testid="feedback-negative">
            <ThumbsDown size={15} aria-hidden />
          </IconButton>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className="hz-menu w-72 space-y-2 !p-3" sideOffset={6} align="start" aria-label={t.chat.feedbackWhat}>
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
    <div className="mt-3 flex flex-wrap gap-1.5" aria-label={t.chat.followUps} data-testid="followups">
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

// ---- voice (browser speech) ---------------------------------------------------------

/** Speak into the draft. The browser transcribes; nothing is sent. */
function Dictation() {
  const value = useFeed();
  const listening = useAuiState((s) => s.composer.dictation != null);
  if (!value?.features?.chat_dictation) return null;
  return listening ? (
    <ComposerPrimitive.StopDictation asChild>
      <IconButton label={t.chat.dictateStop} size="lg" className="!text-accent" data-testid="dictate-stop">
        <MicOff size={17} aria-hidden />
      </IconButton>
    </ComposerPrimitive.StopDictation>
  ) : (
    <ComposerPrimitive.Dictate asChild>
      <IconButton label={t.chat.dictate} size="lg" data-testid="dictate">
        <Mic size={17} aria-hidden />
      </IconButton>
    </ComposerPrimitive.Dictate>
  );
}
