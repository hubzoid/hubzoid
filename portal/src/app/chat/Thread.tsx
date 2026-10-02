// The conversation itself, built from assistant-ui primitives: messages with
// their parts, branch picker, copy, edit and regenerate, the composer with
// attachments, and the empty state with the agent picker (AgentPicker) and suggestions.
import { type ReactNode } from "react";
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
  ChevronLeft,
  ChevronRight,
  Copy,
  Paperclip,
  Pencil,
  RefreshCw,
  Square,
  Upload,
} from "lucide-react";
import { t } from "../i18n/en";
import { displayName } from "../lib/format";
import type { Agent } from "../lib/types";
import { announce, toast } from "../components/toast";
import { AgentAvatar, Button, IconButton, Notice, cx } from "../components/ui";
import { AgentPicker } from "./AgentPicker";
import { MarkdownText } from "./Markdown";
import { ComposerAttachment, MessageAttachment, Reasoning, ToolEntry } from "./parts";

export type ThreadProps = {
  agent: Agent;
  agents: Agent[];
  /** A brand-new chat can still change agent. */
  canPickAgent: boolean;
  onPickAgent: (agent: Agent) => void;
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
        <div className="mx-auto w-full max-w-[780px] flex-1 px-4 pb-6 pt-6 sm:px-6">
          <ThreadPrimitive.Messages>
            {({ message }) =>
              message.role === "user" ? (
                message.composer.isEditing ? (
                  <EditComposer />
                ) : (
                  <UserMessage readOnly={readOnly} />
                )
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
              <span className="text-[15px] font-medium">{t.chat.dropHere}</span>
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

function EmptyThread({ agent, agents, canPickAgent, onPickAgent, requestedMissing }: ThreadProps) {
  const name = displayName(agent.name);
  const suggestions = (agent.suggestions ?? []).map(suggestionText).filter(Boolean).slice(0, 4) as {
    label: string;
    prompt: string;
  }[];
  const picking = canPickAgent && agents.length > 1;
  return (
    <div className="mx-auto flex w-full max-w-[780px] flex-1 flex-col justify-center px-4 py-10 sm:px-6">
      {requestedMissing && (
        <Notice tone="warning" className="mb-6">
          {t.agents.requestedUnavailable(name)}
        </Notice>
      )}
      {picking && <AgentPicker agent={agent} agents={agents} onPick={onPickAgent} />}
      <div className="flex flex-col items-center text-center">
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={52} />
        <h1 className="m-0 mt-4 text-[22px] font-semibold leading-tight tracking-tight text-ink sm:text-[28px]">
          {t.agents.greeting(name)}
        </h1>
        {agent.description && !picking && (
          <p className="m-0 mt-2 max-w-xl text-[15px] leading-relaxed text-mute">{agent.description}</p>
        )}
      </div>
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
        </ActionBarPrimitive.Root>
      </div>
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
      <p className="m-0 mt-1 text-[13px] italic text-mute" data-testid="stopped">
        {t.chat.stopped}
      </p>
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
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={28} />
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
          </ActionBarPrimitive.Root>
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
  return (
    <MessagePrimitive.Root className="flex justify-end py-3" data-role="user-editing">
      <ComposerPrimitive.Root className="w-full max-w-[min(90%,640px)] rounded-2xl border border-accent/60 bg-raised p-2">
        <EditAttachments />
        <ComposerPrimitive.Input
          autoFocus
          submitMode="enter"
          aria-label={t.chat.edit}
          className="block max-h-72 min-h-[44px] w-full resize-none bg-transparent px-2 py-1.5 text-[15px] leading-relaxed text-ink outline-none"
        />
        <div className="flex justify-end gap-2 px-1 pb-0.5 pt-1">
          <ComposerPrimitive.Cancel asChild>
            <Button size="sm" variant="ghost">
              {t.chat.editCancel}
            </Button>
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send asChild>
            <Button size="sm" variant="primary">
              {t.chat.editSave}
            </Button>
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
}

// ---- composer -----------------------------------------------------------------

function Composer({ agent, onStopFollowed, isFollowing, composerDisabledReason }: ThreadProps) {
  const aui = useAui();
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
        "rounded-[20px] border border-line bg-raised transition-colors focus-within:border-[color-mix(in_srgb,var(--hz-accent)_55%,var(--hz-line))]",
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
        submitMode="enter"
        cancelOnEscape={false}
        disabled={disabled}
        placeholder={disabled ? composerDisabledReason! : t.chat.placeholder(name)}
        aria-label={t.chat.placeholder(name)}
        maxRows={12}
        className="block max-h-[40dvh] min-h-[52px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[15px] leading-relaxed text-ink outline-none placeholder:text-mute"
      />
      <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5">
        <ComposerPrimitive.AddAttachment asChild disabled={disabled}>
          <IconButton label={t.chat.attach} size="lg" className="!h-9 !w-9">
            <Paperclip size={18} aria-hidden />
          </IconButton>
        </ComposerPrimitive.AddAttachment>
        {running ? (
          <IconButton
            label={t.chat.stop}
            onClick={stop}
            className="!h-9 !w-9 !rounded-full !bg-ink !text-bg hover:!bg-ink/85"
            data-testid="stop"
          >
            <Square size={13} fill="currentColor" aria-hidden />
          </IconButton>
        ) : (
          <ComposerPrimitive.Send asChild>
            <IconButton
              label={t.chat.send}
              className="!h-9 !w-9 !rounded-full !bg-accent !text-accent-ink hover:!bg-accent-hover disabled:!bg-sunken disabled:!text-mute disabled:!opacity-100"
              data-testid="send"
            >
              <ArrowUp size={18} aria-hidden />
            </IconButton>
          </ComposerPrimitive.Send>
        )}
      </div>
    </ComposerPrimitive.Root>
  );
}
