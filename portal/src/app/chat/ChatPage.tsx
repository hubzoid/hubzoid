// A chat page: a new chat (/, with the agent picker) or a stored conversation
// (/c/:id). Loads the conversation first so every failure has a plain state,
// then mounts assistant-ui's local runtime with Hubzoid's adapters.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantRuntimeProvider, useAui, useLocalRuntime } from "@assistant-ui/react";
import { Archive, Lock, Menu as MenuIcon, MoreHorizontal, Pencil, SearchX, Share2, Trash2, WifiOff } from "lucide-react";
import { DropdownMenu as Menu } from "radix-ui";
import { t } from "../i18n/en";
import { ApiError, enc, get } from "../lib/api";
import { agentById, useApp } from "../lib/app-context";
import {
  deleteConversation,
  findConversation,
  loadConversations,
  patchLocal,
  renameConversation,
  setArchived,
  upsertConversation,
  useConversations,
} from "../lib/conversations";
import { describeError } from "../lib/errors";
import { displayName } from "../lib/format";
import { navigate } from "../lib/router";
import type { Agent, Conversation, ConversationDetail } from "../lib/types";
import { ShareDialog } from "../components/ShareDialog";
import { announce, toast } from "../components/toast";
import { AgentAvatar, Button, ConfirmDialog, Field, IconButton, Modal, Notice, StateMessage } from "../components/ui";
import { toRepository, toThreadMessage } from "./convert";
import { FilesContext } from "./parts";
import { cancelRun, ChatSession, pollRun } from "./session";
import { Thread } from "./Thread";

const LAST_AGENT_KEY = "hz-last-agent";

function rememberAgent(id: string) {
  try {
    localStorage.setItem(LAST_AGENT_KEY, id);
  } catch {
    /* storage unavailable */
  }
}
function lastAgent(): string | null {
  try {
    return localStorage.getItem(LAST_AGENT_KEY);
  } catch {
    return null;
  }
}

type Load = { status: "loading" } | { status: "ready"; detail: ConversationDetail | null } | { status: "error"; error: unknown };

export default function ChatPage({
  pageKey,
  conversationId,
  requestedAgent,
  onConversationCreated,
}: {
  pageKey: string;
  conversationId: string | null;
  requestedAgent: string | null;
  onConversationCreated: (conversation: Conversation, key: string) => void;
}) {
  const app = useApp();
  // The id this page was opened with. A new chat keeps its page when it gets an id.
  const [openedWith] = useState(conversationId);
  const [load, setLoad] = useState<Load>(openedWith ? { status: "loading" } : { status: "ready", detail: null });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!openedWith) return;
    let cancelled = false;
    setLoad({ status: "loading" });
    get<ConversationDetail>(`/api/conversations/${enc(openedWith)}`)
      .then((detail) => !cancelled && setLoad({ status: "ready", detail }))
      .catch((error) => !cancelled && setLoad({ status: "error", error }));
    return () => {
      cancelled = true;
    };
  }, [openedWith, attempt]);

  const agents = app.agents.list;
  const detail = load.status === "ready" ? load.detail : null;

  // Which agent: the conversation's, else ?agent=, the last one used, the default, the first.
  const initial = useMemo(() => {
    if (detail) {
      const c = detail.conversation;
      const found = agentById(agents, c.agent);
      const agent: Agent = found ?? {
        id: c.agent,
        name: c.agent,
        hub: c.hub,
        api_base: c.api_base ?? "",
      };
      return { agent, missing: false, known: !!found };
    }
    const requested = agentById(agents, requestedAgent);
    const agent =
      requested ?? agentById(agents, lastAgent()) ?? agentById(agents, app.agents.defaultAgent) ?? agents[0];
    return { agent, missing: !!requestedAgent && !requested, known: true };
  }, [detail, agents, requestedAgent, app.agents.defaultAgent]);

  if (load.status === "loading" || (!openedWith && app.agents.status === "loading")) return <ChatSkeleton />;

  if (load.status === "error") {
    const error = load.error;
    const status = error instanceof ApiError ? error.status : 0;
    return (
      <PageFrame title={t.chat.loadError}>
        <StateMessage
          icon={status === 403 ? <Lock size={28} aria-hidden /> : status === 404 ? <SearchX size={28} aria-hidden /> : <WifiOff size={28} aria-hidden />}
          title={status === 403 ? t.chat.forbidden : status === 404 ? t.chat.notFound : t.chat.loadError}
          action={
            <>
              {status !== 403 && status !== 404 && (
                <Button onClick={() => setAttempt((n) => n + 1)}>{t.retry}</Button>
              )}
              <Button variant="primary" onClick={() => app.newChat()}>
                {t.sidebar.newChat}
              </Button>
            </>
          }
        >
          {status === 403 || status === 404 ? null : describeError(error)}
        </StateMessage>
      </PageFrame>
    );
  }

  if (!openedWith && app.agents.status === "error")
    return (
      <PageFrame title={t.agents.loadError}>
        <StateMessage
          icon={<WifiOff size={28} aria-hidden />}
          title={t.agents.loadError}
          action={<Button onClick={app.agents.reload}>{t.retry}</Button>}
        >
          {describeError(app.agents.error)}
        </StateMessage>
      </PageFrame>
    );

  if (!initial.agent)
    return (
      <PageFrame title={t.agents.noneTitle}>
        <StateMessage icon={<Lock size={28} aria-hidden />} title={t.agents.noneTitle}>
          {t.agents.none}
        </StateMessage>
      </PageFrame>
    );

  return (
    <ChatView
      key={`${pageKey}:${attempt}`}
      pageKey={pageKey}
      detail={detail}
      initialAgent={initial.agent}
      agentKnown={initial.known}
      requestedMissing={initial.missing}
      onConversationCreated={onConversationCreated}
    />
  );
}

/** Header and body for states that aren't a conversation. */
function PageFrame({ title, children }: { title: string; children: React.ReactNode }) {
  const app = useApp();
  useEffect(() => {
    document.title = `${title} · ${app.brandName}`;
  }, [title, app.brandName]);
  return (
    <>
      <header className="flex h-14 flex-none items-center gap-2 border-b border-line px-3 lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={20} aria-hidden />
        </IconButton>
      </header>
      <div className="flex flex-1 items-center justify-center overflow-y-auto">{children}</div>
    </>
  );
}

function ChatSkeleton() {
  return (
    <div className="flex h-full flex-col" aria-busy="true">
      <div className="flex h-14 flex-none items-center gap-3 border-b border-line px-4">
        <div className="hz-skeleton h-7 w-7 rounded-lg" />
        <div className="hz-skeleton h-4 w-40" />
      </div>
      <div className="mx-auto w-full max-w-[780px] flex-1 space-y-6 px-6 pt-10">
        <div className="ml-auto h-10 w-1/2 rounded-2xl hz-skeleton" />
        <div className="space-y-2.5">
          <div className="hz-skeleton h-4 w-11/12" />
          <div className="hz-skeleton h-4 w-10/12" />
          <div className="hz-skeleton h-4 w-7/12" />
        </div>
      </div>
      <span className="sr-only" role="status">
        {t.loading}
      </span>
    </div>
  );
}

function ChatView({
  pageKey,
  detail,
  initialAgent,
  agentKnown,
  requestedMissing,
  onConversationCreated,
}: {
  pageKey: string;
  detail: ConversationDetail | null;
  initialAgent: Agent;
  agentKnown: boolean;
  requestedMissing: boolean;
  onConversationCreated: (conversation: Conversation, key: string) => void;
}) {
  const app = useApp();
  const [agent, setAgent] = useState<Agent>(initialAgent);
  const [conversation, setConversation] = useState<Conversation | null>(detail?.conversation ?? null);
  const [title, setTitle] = useState<string | null>(detail?.conversation.title ?? null);
  // A new chat is named from its first message at once; the generated title
  // can arrive after the reply, so the list is checked again a few times.
  const titleChecks = useRef(detail?.conversation.title ? 2 : 0);

  // A title the server generated after the reply (seen through the list) wins.
  const listItems = useConversations().items;
  const listTitle = conversation ? (listItems.find((c) => c.id === conversation.id)?.title ?? null) : null;
  useEffect(() => {
    if (listTitle && listTitle !== title) setTitle(listTitle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listTitle]);

  const conversationRef = useRef(conversation);
  conversationRef.current = conversation;

  const [session] = useState(() => {
    const repo = detail
      ? toRepository(detail.messages ?? [], detail.head_id, {
          conversationId: detail.conversation.id,
          apiBase: detail.conversation.api_base ?? initialAgent.api_base,
        })
      : null;
    const s = new ChatSession({
      conversationId: detail?.conversation.id ?? null,
      agent: detail ? { ...initialAgent, api_base: detail.conversation.api_base ?? initialAgent.api_base } : initialAgent,
      repository: repo,
      events: {
        onConversationCreated: (c) => {
          setConversation(c);
          onConversationCreated(c, pageKey);
        },
        onTitle: (id, next) => {
          setTitle(next);
          patchLocal(id, { title: next });
        },
        onRunSettled: (id) => {
          const known = findConversation(id);
          const c = conversationRef.current;
          upsertConversation({
            ...(c ?? { id, agent: s.agent.id }),
            ...(known ?? {}),
            id,
            updated_at: Date.now() / 1000,
          });
          if (titleChecks.current < 2) {
            titleChecks.current++;
            for (const delay of [2500, 8000]) setTimeout(() => void loadConversations(undefined, undefined, true), delay);
          }
        },
        onUnauthorized: () => navigate(`/auth?redirect=${encodeURIComponent(location.pathname)}`, { replace: true }),
      },
    });
    return s;
  });
  const runningIds = useMemo(() => {
    if (!detail) return [];
    return toRepository(detail.messages ?? [], detail.head_id, session.convertContext).runningIds;
  }, [detail, session]);

  const pickAgent = useCallback(
    (next: Agent) => {
      if (session.conversationId) return;
      session.agent = next;
      setAgent(next);
      rememberAgent(next.id);
    },
    [session],
  );

  useEffect(() => {
    if (!session.conversationId && agent.id) rememberAgent(agent.id);
  }, [agent.id, session]);

  const chatModel = useMemo(() => session.chatModelAdapter(), [session]);
  // A new chat has nothing to load; without a history adapter it starts empty
  // (not "loading"), so the empty state renders at once, scrolled to the top.
  const history = useMemo(() => (detail ? session.historyAdapter() : undefined), [session, detail]);
  const attachments = useMemo(() => session.attachmentAdapter(), [session]);
  const runtime = useLocalRuntime(chatModel, { adapters: { history, attachments } });
  useEffect(() => {
    session.countAttachments = () => runtime.thread.composer.getState().attachments.length;
  }, [runtime, session]);

  const shownTitle = title?.trim() || (conversation ? t.sidebar.untitled : t.sidebar.newChat);
  useEffect(() => {
    document.title = `${conversation ? shownTitle : displayName(agent.name) || shownTitle} · ${app.brandName}`;
  }, [shownTitle, conversation, agent.name, app.brandName]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <FilesContext.Provider value={{ conversationId: conversation?.id ?? null, apiBase: session.apiBase }}>
        <ChatHeader
          agent={agent}
          title={shownTitle}
          conversation={conversation}
          onRenamed={(next) => setTitle(next)}
        />
        <FollowRuns session={session} runningIds={runningIds} detail={detail}>
          {(following, stopFollowed) => (
            <Thread
              agent={agent}
              agents={app.agents.list}
              canPickAgent={!conversation}
              onPickAgent={pickAgent}
              requestedMissing={requestedMissing}
              isFollowing={following}
              onStopFollowed={stopFollowed}
              onHeadChanged={(id) => session.saveHead(id)}
              notice={
                !agentKnown && app.agents.status === "ready" ? (
                  <Notice tone="warning" className="mb-2">
                    {t.agents.unavailable}
                  </Notice>
                ) : null
              }
            />
          )}
        </FollowRuns>
      </FilesContext.Provider>
    </AssistantRuntimeProvider>
  );
}

/**
 * A reply that was still being written when this page loaded: poll the run
 * until it ends, showing its progress, then load the finished conversation.
 */
function FollowRuns({
  session,
  runningIds,
  detail,
  children,
}: {
  session: ChatSession;
  runningIds: string[];
  detail: ConversationDetail | null;
  children: (following: boolean, stop: () => void) => React.ReactNode;
}) {
  const aui = useAui();
  const [following, setFollowing] = useState<string | null>(runningIds.at(-1) ?? null);

  useEffect(() => {
    if (!runningIds.length || !detail) return;
    const controller = new AbortController();
    const messages = [...(detail.messages ?? [])];
    const ctx = session.convertContext;
    const importTree = () => {
      const state = aui.thread().getState();
      const head = state.messages.at(-1)?.id ?? detail.head_id;
      const repo = toRepository(messages, head, ctx);
      aui.thread().import({ headId: repo.headId, messages: repo.messages });
    };
    const ended = new Map<string, string>();
    (async () => {
      for (const id of runningIds) {
        setFollowing(id);
        for await (const status of pollRun(session.apiBase, id, controller.signal)) {
          if (status.status !== "running") ended.set(id, String(status.status || "complete"));
          const index = messages.findIndex((m) => m.id === id);
          if (index === -1) break;
          const current = messages[index];
          const next = status.message
            ? { ...current, ...status.message, id, parent_id: current.parent_id }
            : { ...current };
          next.status = status.status === "running" ? "running" : (status.status as typeof next.status) || "complete";
          if (status.status === "error" && !status.message) next.error = next.error || "";
          messages[index] = next;
          if (toThreadMessage(next, ctx)) importTree();
        }
      }
      if (controller.signal.aborted || !session.conversationId) return;
      // Load the finished conversation (titles, final text) once everything settled.
      try {
        const fresh = await get<ConversationDetail>(`/api/conversations/${enc(session.conversationId)}`, {
          signal: controller.signal,
        });
        // A run that ended (or vanished) never shows as running again.
        const settled = (fresh.messages ?? []).map((m) =>
          m.status === "running" && ended.has(m.id) ? { ...m, status: ended.get(m.id) as typeof m.status } : m,
        );
        messages.splice(0, messages.length, ...settled);
        for (const m of fresh.messages ?? []) session.ids.markPersisted(m.id);
        importTree();
        announce(t.chat.responseDone);
        if (fresh.conversation?.title) patchLocal(fresh.conversation.id, { title: fresh.conversation.title });
      } catch {
        /* the polled state is already on screen */
      }
      setFollowing(null);
    })().finally(() => {
      if (!controller.signal.aborted) setFollowing(null);
    });
    return () => controller.abort();
    // Runs once for the conversation this page opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stop = useCallback(() => {
    if (following) void cancelRun(session.apiBase, following);
  }, [following, session]);

  return <>{children(!!following, stop)}</>;
}

function ChatHeader({
  agent,
  title,
  conversation,
  onRenamed,
}: {
  agent: Agent;
  title: string;
  conversation: Conversation | null;
  onRenamed: (title: string) => void;
}) {
  const app = useApp();
  const [sharing, setSharing] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState(title);
  const agentName = displayName(agent.name);

  const doRename = async () => {
    if (!conversation) return;
    const next = draft.trim();
    if (!next) return;
    setBusy(true);
    setError(null);
    try {
      await renameConversation(conversation.id, next);
      onRenamed(next);
      setRenaming(false);
      toast(t.sidebar.renamed);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    if (!conversation) return;
    setBusy(true);
    setError(null);
    try {
      await deleteConversation({ id: conversation.id, api_base: conversation.api_base ?? agent.api_base });
      toast(t.sidebar.deletedToast);
      setDeleting(false);
      navigate("/", { replace: true });
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  const doArchive = async () => {
    if (!conversation) return;
    try {
      await setArchived(conversation.id, true);
      toast(t.sidebar.archivedToast);
      void loadConversations(undefined, undefined, true);
      navigate("/", { replace: true });
    } catch (e) {
      toast(describeError(e), "error");
    }
  };

  return (
    <header className="flex h-14 flex-none items-center gap-2 border-b border-line bg-bg px-2 sm:px-4">
      <div className="lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={20} aria-hidden />
        </IconButton>
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <AgentAvatar name={agent.name} src={agent.avatar_url} size={28} />
        <div className="min-w-0">
          <h1 className="m-0 truncate text-[15px] font-semibold leading-tight text-ink" data-testid="chat-title">
            {conversation ? title : agentName}
          </h1>
          <p className="m-0 truncate text-xs leading-tight text-mute" data-testid="chat-agent">
            {conversation ? agentName : t.sidebar.newChat}
          </p>
        </div>
      </div>
      {conversation && (
        <div className="flex flex-none items-center gap-1">
          <Button
            size="sm"
            variant="secondary"
            icon={<Share2 size={14} aria-hidden />}
            onClick={() => setSharing(true)}
            className="hidden sm:inline-flex"
          >
            {t.chat.share}
          </Button>
          <IconButton label={t.chat.share} onClick={() => setSharing(true)} className="sm:!hidden" size="lg">
            <Share2 size={18} aria-hidden />
          </IconButton>
          <Menu.Root>
            <Menu.Trigger asChild>
              <IconButton label={t.chat.moreActions} size="lg" tooltip={false}>
                <MoreHorizontal size={18} aria-hidden />
              </IconButton>
            </Menu.Trigger>
            <Menu.Portal>
              <Menu.Content className="hz-menu" align="end" sideOffset={4}>
                <Menu.Item
                  className="hz-menu-item"
                  onSelect={() => {
                    setDraft(title === t.sidebar.untitled ? "" : title);
                    setError(null);
                    setRenaming(true);
                  }}
                >
                  <Pencil size={15} aria-hidden />
                  {t.sidebar.rename}
                </Menu.Item>
                <Menu.Item className="hz-menu-item" onSelect={() => void doArchive()}>
                  <Archive size={15} aria-hidden />
                  {t.sidebar.archive}
                </Menu.Item>
                <Menu.Separator className="hz-menu-sep" />
                <Menu.Item
                  className="hz-menu-item danger"
                  onSelect={() => {
                    setError(null);
                    setDeleting(true);
                  }}
                >
                  <Trash2 size={15} aria-hidden />
                  {t.sidebar.delete}
                </Menu.Item>
              </Menu.Content>
            </Menu.Portal>
          </Menu.Root>
        </div>
      )}
      <ShareDialog conversation={sharing ? conversation : null} onClose={() => setSharing(false)} />
      <Modal open={renaming} onOpenChange={(o) => !busy && setRenaming(o)} title={t.sidebar.rename}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void doRename();
          }}
          className="space-y-4"
        >
          <Field label={t.sidebar.renameLabel} value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus maxLength={200} />
          {error && <Notice tone="error">{error}</Notice>}
          <div className="flex justify-end gap-2">
            <Button onClick={() => setRenaming(false)}>{t.cancel}</Button>
            <Button type="submit" variant="primary" loading={busy} disabled={!draft.trim()}>
              {t.save}
            </Button>
          </div>
        </form>
      </Modal>
      <ConfirmDialog
        open={deleting}
        onOpenChange={(o) => !busy && setDeleting(o)}
        title={t.sidebar.deleteTitle}
        body={t.sidebar.deleteBody(title)}
        confirmLabel={t.sidebar.deleteConfirm}
        onConfirm={() => void doDelete()}
        busy={busy}
        error={error}
      />
    </header>
  );
}
