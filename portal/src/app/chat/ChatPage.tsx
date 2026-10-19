// A chat page: a new chat (/, with the agent picker) or a stored conversation
// (/c/:id). Loads the conversation first so every failure has a plain state,
// then mounts assistant-ui's external store over the server's conversation
// graph (runtime.ts): the server owns messages, branches, runs and the queue.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantRuntimeProvider, WebSpeechDictationAdapter, WebSpeechSynthesisAdapter } from "@assistant-ui/react";
import { Archive, Files, Lock, Menu as MenuIcon, MoreHorizontal, Pencil, SearchX, Share2, Trash2, WifiOff } from "lucide-react";
import { DropdownMenu as Menu } from "radix-ui";
import { t } from "../i18n/en";
import { ApiError, enc, get, hubUrl, post } from "../lib/api";
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
import { displayName, pageTitle } from "../lib/format";
import { navigate } from "../lib/router";
import type { Agent, Conversation, ConversationDetail } from "../lib/types";
import { ShareDialog } from "../components/ShareDialog";
import { announce, toast } from "../components/toast";
import { AgentAvatar, Button, ConfirmDialog, Field, IconButton, Modal, Notice, StateMessage } from "../components/ui";
import { FilesContext } from "./parts";
import { ChatSession } from "./session";
import { ConversationFeed, useHubzoidRuntime, type DeletionPreview, type Snapshot } from "./runtime";
import { FeedContext, type Features } from "./feed-context";
import { Thread } from "./Thread";
import { ArtifactPanel, loadArtifacts, type Artifact } from "./ArtifactPanel";
import type { ModelsInfo } from "./models";
import { AgentSwitcher } from "./AgentPicker";
import { SidebarToggle } from "../components/Sidebar";
import { lastAgent, rememberAgent, rememberLast } from "../lib/recentAgents";
import { readConversation, useNotices } from "../lib/notices";

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
        <StateMessage
          icon={<Lock size={28} aria-hidden />}
          title={t.agents.noneTitle}
          action={<Button onClick={app.agents.reload}>{t.retry}</Button>}
        >
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
      <header className="flex h-12 flex-none items-center gap-2 px-3 lg:hidden">
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
      <div className="flex h-12 flex-none items-center gap-3 px-4">
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

  // Whether this page is still on screen. A page that has gone (New chat was
  // pressed while its conversation was being created, or the person moved on)
  // never moves the page on screen.
  const onScreen = useRef(true);
  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
    };
  }, []);

  const [session] = useState(() => {
    const s = new ChatSession({
      conversationId: detail?.conversation.id ?? null,
      agent: detail ? { ...initialAgent, api_base: detail.conversation.api_base ?? initialAgent.api_base } : initialAgent,
      repository: null,
      events: {
        onConversationCreated: (c) => {
          setConversation(c);
          rememberAgent(c.agent);
          // It exists now, so it joins the list either way.
          if (onScreen.current) onConversationCreated(c, pageKey);
          else upsertConversation(c);
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
        onUnauthorized: () => {
          if (onScreen.current) navigate(`/auth?redirect=${encodeURIComponent(location.pathname)}`, { replace: true });
        },
      },
    });
    return s;
  });
  const me = app.user?.id ?? null;
  const [feed] = useState(
    () =>
      new ConversationFeed(
        detail?.conversation.id ?? null,
        session.apiBase,
        me ?? ((detail as unknown as Snapshot | null)?.conversation.owner_id ?? null),
        (detail as unknown as Snapshot | null) ?? null,
      ),
  );
  useEffect(() => () => feed.close(), [feed]);
  const ensureFeed = useCallback(async () => {
    if (feed.conversationId) return feed;
    const id = await session.ensureConversation();
    feed.conversationId = id;
    feed.apiBase = session.apiBase;
    await feed.load();
    if (!feed.me) feed.me = feed.snapshot?.conversation.owner_id ?? null;
    feed.connect();
    return feed;
  }, [feed, session]);
  const [confirmedDeletes] = useState(() => new Map<string, DeletionPreview>());
  const onError = useCallback((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) session.events.onUnauthorized();
    else toast(describeError(error, t.chat.runError), "error");
  }, [session]);

  const [pickerOpen, setPickerOpen] = useState(false);
  const canPick = !conversation && app.agents.list.length > 1;
  // Inside a conversation, choosing another agent starts a new chat with it.
  const canSwitch = app.agents.list.length > 1;
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
    if (!session.conversationId && agent.id) rememberLast(agent.id);
  }, [agent.id, session]);

  const attachments = useMemo(() => session.attachmentAdapter(), [session]);
  // The models this person may pick or compare (HUB-78).
  const [models, setModels] = useState<ModelsInfo | undefined>(undefined);
  useEffect(() => {
    get<ModelsInfo>(hubUrl(session.apiBase, "/api/chat/models"), { quiet401: true }).then(setModels).catch(() => {});
  }, [session]);
  // Member features (B037): voice and the rest only with a grant.
  const [features, setFeatures] = useState<Features>({});
  useEffect(() => {
    get<{ features?: Features }>("/api/chat/capabilities", { quiet401: true })
      .then((c) => setFeatures(c.features ?? {}))
      .catch(() => setFeatures({}));
    // Leaving the page stops anything being read aloud.
    return () => {
      try {
        window.speechSynthesis?.cancel();
      } catch {
        /* no speech synthesis here */
      }
    };
  }, []);
  const speechAdapters = useMemo(() => {
    const out: { speech?: WebSpeechSynthesisAdapter; dictation?: WebSpeechDictationAdapter } = {};
    if (features.chat_read_aloud && typeof window !== "undefined" && "speechSynthesis" in window) out.speech = new WebSpeechSynthesisAdapter();
    if (features.chat_dictation && WebSpeechDictationAdapter.isSupported()) out.dictation = new WebSpeechDictationAdapter();
    return out;
  }, [features]);
  const convertCtx = useMemo(() => session.convertContext, [session, conversation?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const runtime = useHubzoidRuntime({ feed, ctx: convertCtx, ensureFeed, attachments, confirmedDeletes, onError, ...speechAdapters });

  const noticeCount = useNotices().length;
  useEffect(() => {
    if (conversation?.id) readConversation(conversation.id);
  }, [conversation?.id, noticeCount]);

  // Tell the server this person is looking at this chat (no notice for it then).
  useEffect(() => {
    let device = "";
    try {
      device = localStorage.getItem("hz.device") || "";
      if (!device) {
        device = Math.random().toString(36).slice(2, 14);
        localStorage.setItem("hz.device", device);
      }
    } catch {
      device = "tab-" + Math.random().toString(36).slice(2, 10);
    }
    const ping = () => {
      if (!feed.conversationId) return;
      void post("/api/chat/notification-presence", { device_id: device, conversation_id: feed.conversationId, focused: !document.hidden }, { quiet401: true }).catch(() => {});
    };
    ping();
    const timer = setInterval(ping, 15000);
    document.addEventListener("visibilitychange", ping);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", ping);
    };
  }, [feed]);

  // Documents the agent made here, beside the conversation. A new or changed
  // document opens the panel once when a reply finishes (wide screens only).
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [panel, setPanel] = useState<{ open: boolean; id: string | null }>({ open: false, id: null });
  const known = useRef<Map<string, string>>(new Map());
  const refreshArtifacts = useCallback(
    async (autoOpen: boolean) => {
      const cid = feed.conversationId ?? conversationRef.current?.id;
      if (!cid) return;
      try {
        const list = await loadArtifacts(session.apiBase, cid);
        const fresh = list.find((a) => known.current.get(a.artifact_id) !== a.current_version_id);
        known.current = new Map(list.map((a) => [a.artifact_id, a.current_version_id]));
        setArtifacts(list);
        if (autoOpen && fresh && window.innerWidth >= 1024) setPanel({ open: true, id: fresh.artifact_id });
      } catch {
        /* the conversation still works without its documents list */
      }
    },
    [feed, session],
  );
  useEffect(() => {
    void refreshArtifacts(false);
  }, [refreshArtifacts, conversation?.id]);

  // The server's title and run endings flow back to the page and the list.
  const wasRunning = useRef(false);
  useEffect(
    () =>
      feed.subscribe(() => {
        const serverTitle = feed.snapshot?.conversation.title;
        if (serverTitle && serverTitle !== title && feed.conversationId) session.events.onTitle(feed.conversationId, serverTitle);
        const running = !!feed.activeRun;
        if (wasRunning.current && !running && feed.conversationId) {
          session.events.onRunSettled(feed.conversationId);
          announce(t.chat.responseDone);
          void refreshArtifacts(true);
        }
        wasRunning.current = running;
      }),
    [feed, session, title, refreshArtifacts],
  );
  useEffect(() => {
    session.countAttachments = () => runtime.thread.composer.getState().attachments.length;
  }, [runtime, session]);

  const shownTitle = title?.trim() || (conversation ? t.sidebar.untitled : t.sidebar.newChat);
  useEffect(() => {
    // A new chat is "New chat", not the agent's name: with one hub that is the brand too.
    document.title = pageTitle(conversation ? shownTitle : t.sidebar.newChat, app.brandName);
  }, [shownTitle, conversation, app.brandName]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <FilesContext.Provider value={{ conversationId: conversation?.id ?? null, apiBase: session.apiBase }}>
       <div className="flex min-h-0 min-w-0 flex-1">
       <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <ChatHeader
          agent={agent}
          title={shownTitle}
          conversation={conversation}
          onRenamed={(next) => setTitle(next)}
          picker={
            canSwitch
              ? { open: pickerOpen, onOpenChange: setPickerOpen, onPick: (a: Agent) => (conversation ? app.newChat(a.id) : pickAgent(a)) }
              : null
          }
          documents={artifacts.length ? { count: artifacts.length, open: panel.open,
            onToggle: () => setPanel((p) => ({ open: !p.open, id: p.id })) } : null}
        />
        <FeedContext.Provider value={{ feed, confirmedDeletes, onError, features, apiBase: session.apiBase, models,
          ensureConversation: () => session.ensureConversation() }}>
          <Thread
            agent={agent}
            canPickAgent={canPick}
            onChangeAgent={() => setPickerOpen(true)}
            requestedMissing={requestedMissing}
            notice={
              !agentKnown && app.agents.status === "ready" ? (
                <Notice tone="warning" className="mb-2">
                  {t.agents.unavailable}
                </Notice>
              ) : null
            }
          />
        </FeedContext.Provider>
       </div>
        {panel.open && artifacts.length > 0 && (
          <ArtifactPanel
            apiBase={session.apiBase}
            artifacts={artifacts}
            selectedId={panel.id}
            onSelect={(id) => setPanel({ open: true, id })}
            onClose={() => setPanel((p) => ({ open: false, id: p.id }))}
            onChanged={() => void refreshArtifacts(false)}
            canEdit={feed.snapshot?.permissions?.write ?? true}
          />
        )}
       </div>
      </FilesContext.Provider>
    </AssistantRuntimeProvider>
  );
}

function ChatHeader({
  agent,
  title,
  conversation,
  onRenamed,
  picker,
  documents,
}: {
  agent: Agent;
  title: string;
  conversation: Conversation | null;
  onRenamed: (title: string) => void;
  /** The conversation's documents: the button that shows or hides them. */
  documents: { count: number; open: boolean; onToggle: () => void } | null;
  /** A new chat with several agents: the title opens the agent menu. */
  picker: { open: boolean; onOpenChange: (open: boolean) => void; onPick: (agent: Agent) => void } | null;
}) {
  const app = useApp();
  const [sharing, setSharing] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState(title);
  const agentName = displayName(agent.name);
  const agentBlock = (
    <>
      <AgentAvatar name={agent.name} src={agent.avatar_url} size={18} />
      <span className="truncate" data-testid="chat-agent">
        {agentName}
      </span>
    </>
  );
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
      app.newChat(agent.id);
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
      app.newChat(agent.id);
    } catch (e) {
      toast(describeError(e), "error");
    }
  };

  return (
    <header className="hz-chat-header">
      <div className="lg:hidden">
        <IconButton label={t.sidebar.openMenu} onClick={app.openSidebar} size="lg">
          <MenuIcon size={19} aria-hidden />
        </IconButton>
      </div>
      <SidebarToggle />
      <div className="flex min-w-0 flex-1 items-center gap-1">
        {picker ? (
          <AgentSwitcher agent={agent} agents={app.agents.list} {...picker}>
            {agentBlock}
          </AgentSwitcher>
        ) : (
          <span className="hz-hubpick is-static">{agentBlock}</span>
        )}
        {conversation && (
          <>
            <span className="hz-crumb-sep" aria-hidden>
              /
            </span>
            <h1 className="hz-chat-title" data-testid="chat-title">
              {title}
            </h1>
          </>
        )}
        {!conversation && <h1 className="sr-only" data-testid="chat-title">{t.sidebar.newChat}</h1>}
      </div>
      {conversation && (
        <div className="flex flex-none items-center gap-0.5">
          {documents && (
            <Button
              size="sm"
              variant="ghost"
              icon={<Files size={14} aria-hidden />}
              onClick={documents.onToggle}
              aria-pressed={documents.open}
              className={documents.open ? "!bg-active" : undefined}
            >
              {t.artifacts.openCount(documents.count)}
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            icon={<Share2 size={14} aria-hidden />}
            onClick={() => setSharing(true)}
            className="hidden sm:inline-flex"
          >
            {t.chat.share}
          </Button>
          <Menu.Root>
            <Menu.Trigger asChild>
              <IconButton label={t.chat.moreActions} tooltip={false}>
                <MoreHorizontal size={17} aria-hidden />
              </IconButton>
            </Menu.Trigger>
            <Menu.Portal>
              <Menu.Content className="hz-menu w-[200px]" align="end" sideOffset={4}>
                <Menu.Item className="hz-menu-item sm:!hidden" onSelect={() => setSharing(true)}>
                  <Share2 size={15} aria-hidden />
                  {t.chat.share}
                </Menu.Item>
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
