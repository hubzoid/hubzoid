// Hubzoid's sidebar: quiet rows for New chat, Search, Sources, Spaces and
// Workspace; folders; chats grouped by agent (or listed by date) with pin and
// options on hover and an unread dot; the account menu and the bell at the
// foot. On wide screens it can be hidden; on narrow screens it is a drawer.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Dialog as RDialog, DropdownMenu as Menu } from "radix-ui";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  Folder as FolderIcon,
  FolderInput,
  FolderPlus,
  Keyboard,
  LogOut,
  Monitor,
  Moon,
  MoonStar,
  MoreHorizontal,
  PanelLeft,
  Pencil,
  PencilRuler,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  Share2,
  SquarePen,
  Sun,
  Trash2,
  UsersRound,
  X,
} from "lucide-react";
import { t } from "../i18n/en";
import { clearDeviceCache } from "../lib/devices";
import { post } from "../lib/api";
import { createFolder, loadOrganization, organize, useOrganization } from "../lib/organization";
import { NotificationsButton } from "./Notifications";
import { agentById, useApp } from "../lib/app-context";
import {
  deleteConversation,
  loadConversations,
  loadMore,
  renameConversation,
  setArchived,
  useConversations,
} from "../lib/conversations";
import { dateGroup, displayName, type DateGroup } from "../lib/format";
import { linkClick, navigate, type Route } from "../lib/router";
import { unreadChats, useNotices } from "../lib/notices";
import { sidebar as sidebarState, useSidebarState } from "../lib/sidebarState";
import { useTheme, type ThemeMode } from "../lib/theme";
import type { Agent, Conversation } from "../lib/types";
import { describeError } from "../lib/errors";
import { ShareDialog } from "./ShareDialog";
import { toast } from "./toast";
import { AgentAvatar, BrandMark, Button, ConfirmDialog, Field, IconButton, Modal, Notice, UserAvatar } from "./ui";

const GROUPS: { key: DateGroup; label: string }[] = [
  { key: "today", label: t.sidebar.today },
  { key: "yesterday", label: t.sidebar.yesterday },
  { key: "week", label: t.sidebar.week },
  { key: "older", label: t.sidebar.older },
];
/** Chats shown per agent before "Show all". */
const VISIBLE = 5;

export function Sidebar({
  activeId,
  route,
  drawerOpen,
  onDrawerChange,
}: {
  activeId: string | null;
  route: Route["name"];
  drawerOpen: boolean;
  onDrawerChange: (open: boolean) => void;
}) {
  const { collapsed } = useSidebarState();
  // Close the drawer when the page changes.
  const lastRoute = useRef(`${route}:${activeId}`);
  useEffect(() => {
    const key = `${route}:${activeId}`;
    if (key !== lastRoute.current) {
      lastRoute.current = key;
      onDrawerChange(false);
    }
  }, [route, activeId, onDrawerChange]);

  return (
    <>
      {collapsed ? (
        <Rail route={route} />
      ) : (
        <aside className="hz-sidebar hidden lg:flex" data-testid="sidebar">
          <SidebarBody activeId={activeId} route={route} />
        </aside>
      )}
      <RDialog.Root open={drawerOpen} onOpenChange={onDrawerChange}>
        <RDialog.Portal>
          <RDialog.Overlay className="hz-overlay lg:hidden" />
          <RDialog.Content className="hz-drawer lg:hidden" aria-describedby={undefined}>
            <RDialog.Title className="sr-only">{t.sidebar.label}</RDialog.Title>
            <SidebarBody activeId={activeId} route={route} inDrawer onClose={() => onDrawerChange(false)} />
          </RDialog.Content>
        </RDialog.Portal>
      </RDialog.Root>
    </>
  );
}

/** The hidden sidebar's narrow rail: the main destinations stay one click away. */
function Rail({ route }: { route: Route["name"] }) {
  const app = useApp();
  const go = (href: string) => () => navigate(href);
  return (
    <aside className="hz-rail hidden lg:flex" aria-label={t.sidebar.label}>
      <IconButton label={t.sidebar.expand} onClick={sidebarState.toggleCollapsed} data-testid="sidebar-expand">
        <PanelLeft size={17} aria-hidden />
      </IconButton>
      <IconButton label={t.sidebar.newChat} onClick={() => app.newChat()} aria-current={route === "new" ? "page" : undefined}>
        <SquarePen size={17} aria-hidden />
      </IconButton>
      <IconButton label={t.sidebar.search} onClick={sidebarState.openSearch}>
        <Search size={17} aria-hidden />
      </IconButton>
      <IconButton label={t.sources.nav} onClick={go("/sources")} aria-current={route === "sources" ? "page" : undefined}>
        <BookOpen size={17} aria-hidden />
      </IconButton>
      <IconButton label={t.spaces.nav} onClick={go("/spaces")} aria-current={route === "spaces" ? "page" : undefined}>
        <UsersRound size={17} aria-hidden />
      </IconButton>
      {app.isAdmin && (
        <IconButton label={t.workspace.nav} onClick={go("/workspace")} aria-current={route === "workspace" ? "page" : undefined}>
          <PencilRuler size={17} aria-hidden />
        </IconButton>
      )}
      <span className="flex-1" />
      <NotificationsButton />
      <IconButton label={t.account.settings} onClick={go("/settings")} aria-current={route === "settings" ? "page" : undefined}>
        <Settings size={17} aria-hidden />
      </IconButton>
    </aside>
  );
}

/** Kept for headers that used to show it; the rail does this now. */
export function SidebarToggle() {
  return null;
}

function Brand() {
  const { branding, brandName } = useApp();
  return (
    <a
      href="/"
      onClick={(e) => linkClick(e, "/")}
      data-brand-slot="brand.logoFrame"
      className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-1.5 py-1 text-ink no-underline"
    >
      <BrandMark logoUrl={branding.logo_url} darkLogoUrl={branding.logo_dark_url} name={brandName} />
    </a>
  );
}

function NavRow({
  icon,
  label,
  href,
  onClick,
  current,
  hint,
  testId,
}: {
  icon: ReactNode;
  label: string;
  href?: string;
  onClick?: () => void;
  current?: boolean;
  hint?: string;
  testId?: string;
}) {
  const body = (
    <>
      {icon}
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {hint && <span className="hz-nav-hint">{hint}</span>}
    </>
  );
  if (href)
    return (
      <a href={href} onClick={(e) => linkClick(e, href)} aria-current={current ? "page" : undefined} className="hz-nav" data-testid={testId}>
        {body}
      </a>
    );
  return (
    <button type="button" onClick={onClick} aria-current={current ? "page" : undefined} className="hz-nav" data-testid={testId}>
      {body}
    </button>
  );
}

const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

function SidebarBody({
  activeId,
  route,
  inDrawer,
  onClose,
}: {
  activeId: string | null;
  route: Route["name"];
  inDrawer?: boolean;
  onClose?: () => void;
}) {
  const app = useApp();
  const list = useConversations();
  const view = useSidebarState();
  const notices = useNotices();
  const unread = useMemo(() => unreadChats(notices), [notices]);
  const [shareTarget, setShareTarget] = useState<Conversation | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const org = useOrganization();
  const [openFolders, setOpenFolders] = useState<Record<string, boolean>>({});
  const [newFolderFor, setNewFolderFor] = useState<Conversation | "none" | null>(null);
  const [folderName, setFolderName] = useState("");

  useEffect(() => {
    if (!list.loaded && !list.loading) void loadConversations("", list.archived);
    // The sidebar always lists every chat; search lives in its own dialog.
    else if (list.q) void loadConversations("", list.archived);
    if (!org.loaded) void loadOrganization();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const agents = app.agents.list;
  const multiAgent = agents.length > 1;
  const byAgent = multiAgent && view.view === "agents" && !list.archived;

  const placed = (c: Conversation) => org.placements.get(c.id);
  const isPinned = (c: Conversation) => placed(c)?.pinned_position != null;
  const pinned = useMemo(
    () =>
      list.archived
        ? []
        : list.items.filter(isPinned).sort((a, b) => (placed(a)!.pinned_position ?? 0) - (placed(b)!.pinned_position ?? 0)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [list.items, list.archived, org.placements],
  );
  const inFolder = (folderId: string) => list.items.filter((c) => placed(c)?.folder_id === folderId && !isPinned(c));
  const loose = (c: Conversation) => !!list.archived || (!isPinned(c) && !placed(c)?.folder_id);

  const onOrganize = async (c: Conversation, change: Parameters<typeof organize>[1]) => {
    try {
      await organize(c.id, change);
    } catch (error) {
      toast(describeError(error), "error");
    }
  };

  const rowProps = (c: Conversation, showAgent: boolean) => ({
    conversation: c,
    active: c.id === activeId,
    archivedView: list.archived,
    agentAvatar: showAgent ? agentById(agents, c.agent) : undefined,
    unread: unread.has(c.id) && c.id !== activeId,
    renaming: renaming === c.id,
    pinned: isPinned(c),
    folders: org.folders,
    folderId: placed(c)?.folder_id ?? null,
    onRenameStart: () => setRenaming(c.id),
    onRenameEnd: () => setRenaming(null),
    onShare: () => setShareTarget(c),
    onArchive: (archived: boolean) => void onArchive(c, archived),
    onPin: (pin: boolean) => void onOrganize(c, { pinned_position: pin ? 1 : null }),
    onMove: (folderId: string | null) => void onOrganize(c, { folder_id: folderId }),
    onNewFolder: () => {
      setFolderName("");
      setNewFolderFor(c);
    },
    onDelete: () => {
      setDeleteError(null);
      setDeleteTarget(c);
    },
  });

  const dated = (items: Conversation[]) => {
    const now = new Date();
    const map = new Map<DateGroup, Conversation[]>();
    for (const c of items) {
      const g = dateGroup(c.updated_at ?? c.created_at, now);
      map.set(g, [...(map.get(g) ?? []), c]);
    }
    return GROUPS.map((g) => ({ ...g, items: map.get(g.key) ?? [] })).filter((g) => g.items.length > 0);
  };

  // By agent: each agent's pinned chats, then its other unfiled chats by date.
  const agentGroups = useMemo(() => {
    if (!byAgent) return [];
    const known = new Map<string, Agent>();
    for (const a of agents) known.set(a.id, a);
    const ids = new Set<string>();
    for (const c of list.items) ids.add(agentById(agents, c.agent)?.id ?? c.agent);
    const activeAgent = list.items.find((c) => c.id === activeId)?.agent;
    return [...ids]
      .map((id) => {
        const agent = known.get(id) ?? { id, name: id };
        const chats = list.items.filter((c) => (agentById(agents, c.agent)?.id ?? c.agent) === id && (isPinned(c) || !placed(c)?.folder_id));
        const isActive = !!activeAgent && (agentById(agents, activeAgent)?.id ?? activeAgent) === id;
        return { agent, chats, isActive };
      })
      .filter((g) => g.chats.length > 0)
      .sort((a, b) => displayName(a.agent.name).localeCompare(displayName(b.agent.name)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byAgent, agents, list.items, org.placements, activeId]);

  const showArchived = (archived: boolean) => void loadConversations("", archived);

  const onDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteConversation({
        id: deleteTarget.id,
        api_base: deleteTarget.api_base ?? agentById(agents, deleteTarget.agent)?.api_base,
      });
      toast(t.sidebar.deletedToast);
      if (deleteTarget.id === activeId) app.newChat(deleteTarget.agent);
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(describeError(error));
    } finally {
      setDeleting(false);
    }
  };

  const onArchive = async (c: Conversation, archived: boolean) => {
    try {
      await setArchived(c.id, archived);
      toast(archived ? t.sidebar.archivedToast : t.sidebar.unarchivedToast);
      if (archived && c.id === activeId) app.newChat(c.agent);
    } catch (error) {
      toast(describeError(error), "error");
    }
  };

  let body: ReactNode;
  if (list.loading && list.items.length === 0) {
    body = (
      <div className="space-y-2.5 px-2.5 py-2" aria-hidden>
        {[70, 88, 60, 80, 52].map((w, i) => (
          <div key={i} className="hz-skeleton h-3.5" style={{ width: `${w}%` }} />
        ))}
      </div>
    );
  } else if (list.error && list.items.length === 0) {
    body = (
      <div className="px-1">
        <Notice
          tone="error"
          action={
            <Button size="sm" onClick={() => void loadConversations("", list.archived)}>
              {t.retry}
            </Button>
          }
        >
          {t.sidebar.loadError} {describeError(list.error)}
        </Notice>
      </div>
    );
  } else if (list.items.length === 0) {
    body = <p className="m-0 px-2.5 py-2 text-[12.5px] leading-relaxed text-mute">{list.archived ? t.sidebar.emptyArchived : t.sidebar.empty}</p>;
  } else if (byAgent) {
    body = agentGroups.map(({ agent, chats, isActive }) => (
      <AgentGroup
        key={agent.id}
        agent={agent}
        chats={chats}
        open={sidebarState.isOpen(agent.id, isActive || agentGroups.length === 1)}
        showAll={!!view.showAll[agent.id]}
        unread={chats.some((c) => unread.has(c.id) && c.id !== activeId)}
        renderRow={(c) => <ConversationRow key={c.id} {...rowProps(c, false)} />}
        isPinned={isPinned}
        dated={dated}
        onNew={() => app.newChat(agent.id)}
      />
    ));
  } else {
    body = (
      <>
        {pinned.length > 0 && (
          <section aria-label={t.sidebar.pinned}>
            <h3 className="hz-side-group">{t.sidebar.pinned}</h3>
            <ul className="hz-rows">{pinned.map((c) => <ConversationRow key={c.id} {...rowProps(c, multiAgent)} />)}</ul>
          </section>
        )}
        {dated(list.items.filter(loose)).map((group) => (
          <section key={group.key} aria-label={group.label}>
            <h3 className="hz-side-group">{group.label}</h3>
            <ul className="hz-rows">{group.items.map((c) => <ConversationRow key={c.id} {...rowProps(c, multiAgent)} />)}</ul>
          </section>
        ))}
      </>
    );
  }

  return (
    <nav aria-label={t.sidebar.label} className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center gap-1 px-2.5 pb-1 pt-2.5">
        <Brand />
        {inDrawer ? (
          <IconButton label={t.sidebar.closeMenu} onClick={onClose} tooltip={false}>
            <X size={17} aria-hidden />
          </IconButton>
        ) : (
          <IconButton label={t.sidebar.collapse} onClick={sidebarState.toggleCollapsed}>
            <PanelLeft size={17} aria-hidden />
          </IconButton>
        )}
      </div>
      <div className="flex flex-col gap-px px-2.5 pb-1">
        <NavRow
          icon={<SquarePen size={16} aria-hidden />}
          label={t.sidebar.newChat}
          onClick={() => app.newChat()}
          current={route === "new"}
          hint={mac ? "⇧⌘O" : "Ctrl⇧O"}
          testId="new-chat"
        />
        <NavRow icon={<Search size={16} aria-hidden />} label={t.sidebar.searchPlaceholder} onClick={sidebarState.openSearch} hint={mac ? "⌘K" : "Ctrl K"} testId="open-search" />
        <NavRow icon={<BookOpen size={16} aria-hidden />} label={t.sources.nav} href="/sources" current={route === "sources"} />
        <NavRow icon={<UsersRound size={16} aria-hidden />} label={t.spaces.nav} href="/spaces" current={route === "spaces"} />
        {app.isAdmin && <NavRow icon={<PencilRuler size={16} aria-hidden />} label={t.workspace.nav} href="/workspace" current={route === "workspace"} />}
      </div>

      <div className="hz-side-scroll" aria-busy={list.loading || undefined}>
        {list.archived ? (
          <div className="flex items-center gap-1 pb-1 pt-2">
            <IconButton label={t.sidebar.backToChats} onClick={() => showArchived(false)}>
              <ArrowLeft size={15} aria-hidden />
            </IconButton>
            <h2 className="m-0 text-[13px] font-medium text-ink">{t.sidebar.archivedTitle}</h2>
          </div>
        ) : (
          <>
            {org.folders.length > 0 && (
              <section aria-label={t.sidebar.folders}>
                <div className="hz-side-head">
                  <h2>{t.sidebar.folders}</h2>
                  <IconButton
                    label={t.sidebar.newFolder}
                    className="hz-side-head-act"
                    onClick={() => {
                      setFolderName("");
                      setNewFolderFor("none");
                    }}
                  >
                    <Plus size={14} aria-hidden />
                  </IconButton>
                </div>
                <ul className="hz-rows">
                  {org.folders.map((f) => {
                    const open = openFolders[f.id] ?? false;
                    const items = inFolder(f.id);
                    return (
                      <li key={f.id}>
                        <button
                          type="button"
                          className="hz-folder"
                          aria-expanded={open}
                          onClick={() => setOpenFolders((o) => ({ ...o, [f.id]: !open }))}
                        >
                          <FolderIcon size={15} aria-hidden className="text-mute" />
                          <span className="min-w-0 flex-1 truncate">{f.name}</span>
                          <span className="text-[11px] tabular-nums text-mute">{items.length || ""}</span>
                          {open ? <ChevronDown size={13} aria-hidden className="text-mute" /> : <ChevronRight size={13} aria-hidden className="text-mute" />}
                        </button>
                        {open && (
                          <ul className="hz-rows pl-4">
                            {items.map((c) => (
                              <ConversationRow key={c.id} {...rowProps(c, multiAgent)} />
                            ))}
                            {items.length === 0 && <li className="px-2.5 py-1 text-[12px] text-mute">{t.sidebar.folderEmpty}</li>}
                          </ul>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}
            <div className="hz-side-head">
              <h2>{t.sidebar.chats}</h2>
              {multiAgent ? (
                <Menu.Root modal={false}>
                  <Menu.Trigger asChild>
                    <button type="button" className="hz-side-view" aria-label={t.sidebar.viewOptions}>
                      {view.view === "agents" ? t.sidebar.byAgent : t.sidebar.recent}
                      <ChevronDown size={12} aria-hidden />
                    </button>
                  </Menu.Trigger>
                  <Menu.Portal>
                    <Menu.Content className="hz-menu w-[210px]" align="end" sideOffset={4}>
                      <Menu.RadioGroup value={view.view} onValueChange={(v) => sidebarState.setView(v as "agents" | "recent")}>
                        <Menu.RadioItem value="agents" className="hz-menu-item">
                          <span className="flex-1">{t.sidebar.byAgent}</span>
                          <Menu.ItemIndicator>
                            <Check size={14} aria-hidden />
                          </Menu.ItemIndicator>
                        </Menu.RadioItem>
                        <Menu.RadioItem value="recent" className="hz-menu-item">
                          <span className="flex-1">{t.sidebar.recent}</span>
                          <Menu.ItemIndicator>
                            <Check size={14} aria-hidden />
                          </Menu.ItemIndicator>
                        </Menu.RadioItem>
                      </Menu.RadioGroup>
                      <Menu.Separator className="hz-menu-sep" />
                      <Menu.Item
                        className="hz-menu-item"
                        onSelect={() => {
                          setFolderName("");
                          setNewFolderFor("none");
                        }}
                      >
                        <FolderPlus size={15} aria-hidden />
                        {t.sidebar.newFolder}
                      </Menu.Item>
                      <Menu.Item className="hz-menu-item" onSelect={() => showArchived(true)}>
                        <Archive size={15} aria-hidden />
                        {t.sidebar.archived}
                      </Menu.Item>
                    </Menu.Content>
                  </Menu.Portal>
                </Menu.Root>
              ) : (
                <IconButton
                  label={t.sidebar.newFolder}
                  className="hz-side-head-act"
                  onClick={() => {
                    setFolderName("");
                    setNewFolderFor("none");
                  }}
                >
                  <FolderPlus size={14} aria-hidden />
                </IconButton>
              )}
            </div>
          </>
        )}
        {body}
        {list.nextCursor && (
          <button type="button" className="hz-side-more" disabled={list.loadingMore} onClick={() => void loadMore()}>
            {t.sidebar.loadMore}
          </button>
        )}
      </div>

      <div className="flex items-center gap-1 px-2.5 pb-2.5 pt-1.5">
        <AccountMenu onArchived={() => showArchived(true)} />
        <NotificationsButton />
      </div>

      <ShareDialog conversation={shareTarget} onClose={() => setShareTarget(null)} />
      <Modal open={newFolderFor !== null} onOpenChange={(o) => !o && setNewFolderFor(null)} title={t.sidebar.newFolder}>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            const target = newFolderFor;
            void createFolder(folderName)
              .then((f) => {
                if (target && target !== "none") return organize(target.id, { folder_id: f.id });
              })
              .then(() => setNewFolderFor(null))
              .catch((error) => toast(describeError(error), "error"));
          }}
        >
          <Field label={t.sidebar.folderName} value={folderName} onChange={(e) => setFolderName(e.target.value)} autoFocus maxLength={80} />
          <div className="flex justify-end gap-2">
            <Button onClick={() => setNewFolderFor(null)}>{t.cancel}</Button>
            <Button type="submit" variant="primary" disabled={!folderName.trim()}>
              {t.save}
            </Button>
          </div>
        </form>
      </Modal>
      <ConfirmDialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && !deleting && setDeleteTarget(null)}
        title={t.sidebar.deleteTitle}
        body={t.sidebar.deleteBody(deleteTarget?.title || t.sidebar.untitled)}
        confirmLabel={t.sidebar.deleteConfirm}
        onConfirm={() => void onDelete()}
        busy={deleting}
        error={deleteError}
      />
    </nav>
  );
}

function AgentGroup({
  agent,
  chats,
  open,
  showAll,
  unread,
  renderRow,
  isPinned,
  dated,
  onNew,
}: {
  agent: Agent;
  chats: Conversation[];
  open: boolean;
  showAll: boolean;
  unread: boolean;
  renderRow: (c: Conversation) => ReactNode;
  isPinned: (c: Conversation) => boolean;
  dated: (items: Conversation[]) => { key: DateGroup; label: string; items: Conversation[] }[];
  onNew: () => void;
}) {
  const name = displayName(agent.name);
  const pinned = chats.filter(isPinned);
  const rest = chats.filter((c) => !isPinned(c));
  const ordered = [...pinned, ...rest];
  const shown = new Set((showAll ? ordered : ordered.slice(0, VISIBLE)).map((c) => c.id));
  const groups = dated(rest.filter((c) => shown.has(c.id)));
  return (
    <section className="hz-agent-group" data-open={open} aria-label={name}>
      <div className="hz-agent-head">
        <button type="button" className="hz-agent-toggle" aria-expanded={open} onClick={() => sidebarState.setOpen(agent.id, !open)}>
          <ChevronRight size={13} aria-hidden className="hz-agent-chev" />
          <AgentAvatar name={agent.name} src={agent.avatar_url} size={18} />
          <span className="min-w-0 flex-1 truncate">{name}</span>
          {unread && !open && <span className="hz-dot" aria-label={t.sidebar.unreadReply} />}
        </button>
        <IconButton label={t.sidebar.newChatWith(name)} className="hz-side-head-act" onClick={onNew}>
          <Plus size={14} aria-hidden />
        </IconButton>
      </div>
      {open && (
        <div className="hz-agent-body">
          {pinned.filter((c) => shown.has(c.id)).length > 0 && (
            <>
              <h3 className="hz-side-group is-nested">{t.sidebar.pinned}</h3>
              <ul className="hz-rows is-nested">{pinned.filter((c) => shown.has(c.id)).map(renderRow)}</ul>
            </>
          )}
          {groups.map((g) => (
            <div key={g.key}>
              <h3 className="hz-side-group is-nested">{g.label}</h3>
              <ul className="hz-rows is-nested">{g.items.map(renderRow)}</ul>
            </div>
          ))}
          {ordered.length > VISIBLE && (
            <button type="button" className="hz-side-more is-nested" onClick={() => sidebarState.toggleShowAll(agent.id)}>
              {showAll ? t.sidebar.showFewer : t.sidebar.showAll(ordered.length)}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function ConversationRow({
  conversation: c,
  active,
  archivedView,
  agentAvatar,
  unread,
  renaming,
  pinned,
  folders,
  folderId,
  onRenameStart,
  onRenameEnd,
  onShare,
  onArchive,
  onPin,
  onMove,
  onNewFolder,
  onDelete,
}: {
  conversation: Conversation;
  active: boolean;
  archivedView: boolean;
  agentAvatar?: { name: string; avatar_url?: string | null };
  unread: boolean;
  renaming: boolean;
  pinned: boolean;
  folders: { id: string; name: string }[];
  folderId: string | null;
  onRenameStart: () => void;
  onRenameEnd: () => void;
  onShare: () => void;
  onArchive: (archived: boolean) => void;
  onPin: (pin: boolean) => void;
  onMove: (folderId: string | null) => void;
  onNewFolder: () => void;
  onDelete: () => void;
}) {
  const title = c.title?.trim() || t.sidebar.untitled;
  const href = `/c/${encodeURIComponent(c.id)}`;
  const moreRef = useRef<HTMLButtonElement>(null);

  if (renaming)
    return (
      <li>
        <RenameField
          initial={c.title?.trim() || ""}
          onDone={async (value) => {
            onRenameEnd();
            const next = value.trim();
            if (!next || next === (c.title || "").trim()) return;
            try {
              await renameConversation(c.id, next);
              toast(t.sidebar.renamed);
            } catch (error) {
              toast(describeError(error), "error");
            }
            moreRef.current?.focus();
          }}
          onCancel={() => {
            onRenameEnd();
            setTimeout(() => moreRef.current?.focus(), 0);
          }}
        />
      </li>
    );

  return (
    <li className="hz-conv" data-active={active} data-unread={unread || undefined}>
      <a
        href={href}
        onClick={(e) => linkClick(e, href)}
        aria-current={active ? "page" : undefined}
        aria-label={unread ? `${title}, ${t.sidebar.unreadReply}` : undefined}
        className="flex items-center gap-2"
        title={title}
      >
        {agentAvatar && <AgentAvatar name={agentAvatar.name} src={agentAvatar.avatar_url} size={16} />}
        <span className="truncate">{title}</span>
      </a>
      {unread && (
        <span className="hz-conv-state" aria-hidden>
          <span className="hz-dot" />
        </span>
      )}
      {!archivedView && (
        <button
          type="button"
          className="hz-icon-btn hz-conv-pin"
          aria-label={pinned ? t.sidebar.unpin : t.sidebar.pin}
          aria-pressed={pinned}
          onClick={() => onPin(!pinned)}
        >
          <Pin size={13} aria-hidden />
        </button>
      )}
      <Menu.Root modal={false}>
        <Menu.Trigger asChild>
          <button ref={moreRef} type="button" className="hz-icon-btn hz-conv-more" aria-label={t.sidebar.options(title)}>
            <MoreHorizontal size={15} aria-hidden />
          </button>
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className="hz-menu w-[210px]" align="start" sideOffset={4}>
            <Menu.Item className="hz-menu-item" onSelect={onRenameStart}>
              <Pencil size={15} aria-hidden />
              {t.sidebar.rename}
            </Menu.Item>
            {!archivedView && (
              <Menu.Item className="hz-menu-item" onSelect={onShare}>
                <Share2 size={15} aria-hidden />
                {t.sidebar.share}
              </Menu.Item>
            )}
            {!archivedView && (
              <Menu.Item className="hz-menu-item" onSelect={() => onPin(!pinned)}>
                {pinned ? <PinOff size={15} aria-hidden /> : <Pin size={15} aria-hidden />}
                {pinned ? t.sidebar.unpin : t.sidebar.pin}
              </Menu.Item>
            )}
            {!archivedView && (
              <Menu.Sub>
                <Menu.SubTrigger className="hz-menu-item">
                  <FolderInput size={15} aria-hidden />
                  {t.sidebar.moveToFolder}
                  <ChevronRight size={13} aria-hidden className="ml-auto" />
                </Menu.SubTrigger>
                <Menu.Portal>
                  <Menu.SubContent className="hz-menu" sideOffset={4}>
                    {folders.map((f) => (
                      <Menu.Item key={f.id} className="hz-menu-item" onSelect={() => onMove(f.id)}>
                        <FolderIcon size={14} aria-hidden />
                        <span className="truncate">{f.name}</span>
                        {folderId === f.id && <Check size={13} aria-hidden className="ml-auto" />}
                      </Menu.Item>
                    ))}
                    {folderId && (
                      <Menu.Item className="hz-menu-item" onSelect={() => onMove(null)}>
                        <X size={14} aria-hidden />
                        {t.sidebar.noFolder}
                      </Menu.Item>
                    )}
                    <Menu.Separator className="hz-menu-sep" />
                    <Menu.Item className="hz-menu-item" onSelect={onNewFolder}>
                      <FolderPlus size={14} aria-hidden />
                      {t.sidebar.newFolder}
                    </Menu.Item>
                  </Menu.SubContent>
                </Menu.Portal>
              </Menu.Sub>
            )}
            <Menu.Item className="hz-menu-item" onSelect={() => onArchive(!archivedView)}>
              {archivedView ? <ArchiveRestore size={15} aria-hidden /> : <Archive size={15} aria-hidden />}
              {archivedView ? t.sidebar.unarchive : t.sidebar.archive}
            </Menu.Item>
            <Menu.Separator className="hz-menu-sep" />
            <Menu.Item className="hz-menu-item danger" onSelect={onDelete}>
              <Trash2 size={15} aria-hidden />
              {t.sidebar.delete}
            </Menu.Item>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
    </li>
  );
}

function RenameField({
  initial,
  onDone,
  onCancel,
}: {
  initial: string;
  onDone: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  const finished = useRef(false);
  useEffect(() => {
    // After the menu closes and returns focus, take it for the field.
    const timer = setTimeout(() => {
      ref.current?.focus();
      ref.current?.select();
    }, 30);
    return () => clearTimeout(timer);
  }, []);
  const finish = (save: boolean) => {
    if (finished.current) return;
    finished.current = true;
    if (save) onDone(value);
    else onCancel();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      finish(false);
    }
  };
  return (
    <div className="flex items-center gap-1 rounded-lg bg-hover p-0.5">
      <input
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => finish(true)}
        aria-label={t.sidebar.renameLabel}
        maxLength={200}
        className="hz-input !min-h-7 flex-1 !px-2 !py-0.5 !text-[13px]"
      />
      <button
        type="button"
        aria-label={t.save}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => finish(true)}
        className="hz-icon-btn !h-7 !w-7"
      >
        <Check size={14} aria-hidden />
      </button>
    </div>
  );
}

const themeOptions: { value: ThemeMode; label: string; icon: ReactNode }[] = [
  { value: "light", label: t.theme.light, icon: <Sun size={15} aria-hidden /> },
  { value: "dark", label: t.theme.dark, icon: <Moon size={15} aria-hidden /> },
  { value: "oled", label: t.theme.oled, icon: <MoonStar size={15} aria-hidden /> },
  { value: "system", label: t.theme.system, icon: <Monitor size={15} aria-hidden /> },
];

function AccountMenu({ onArchived }: { onArchived: () => void }) {
  const app = useApp();
  const { mode, setMode, allowed, locked } = useTheme();
  const [signingOut, setSigningOut] = useState(false);
  const name = app.isLocal ? t.account.local : app.user?.name || app.user?.email || "";
  const role = app.isAdmin ? t.account.admin : app.isLocal ? "" : t.account.member;

  const signOut = async () => {
    setSigningOut(true);
    try {
      await post("/api/auth/logout", undefined, { quiet401: true });
      clearDeviceCache();
      location.assign("/auth");
    } catch (error) {
      setSigningOut(false);
      if ((error as { status?: number })?.status === 401) location.assign("/auth");
      else toast(t.account.signOutFailed, "error");
    }
  };

  return (
    <Menu.Root>
      <Menu.Trigger asChild>
        <button type="button" className="hz-me" aria-label={`${t.account.menu}: ${displayName(name)}`}>
          <UserAvatar name={name || "?"} size={26} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-medium text-ink">{displayName(name)}</span>
            {role && <span className="block truncate text-[11.5px] leading-tight text-mute">{role}</span>}
          </span>
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="hz-menu w-[240px]" side="top" align="start" sideOffset={6}>
          {app.user?.email && <Menu.Label className="hz-menu-label truncate">{app.user.email}</Menu.Label>}
          <Menu.Item className="hz-menu-item" onSelect={() => navigate("/settings")}>
            <Settings size={15} aria-hidden />
            <span className="flex-1">{t.account.settings}</span>
            <span className="text-[11.5px] text-mute">{mac ? "⌘," : ""}</span>
          </Menu.Item>
          <Menu.Item className="hz-menu-item" onSelect={onArchived}>
            <Archive size={15} aria-hidden />
            {t.sidebar.archived}
          </Menu.Item>
          <Menu.Item className="hz-menu-item" onSelect={() => window.dispatchEvent(new Event("hz:shortcuts"))}>
            <Keyboard size={15} aria-hidden />
            <span className="flex-1">{t.account.shortcuts}</span>
            <span className="text-[11.5px] text-mute">?</span>
          </Menu.Item>
          {!locked && allowed.length > 1 && (
            <>
              <Menu.Separator className="hz-menu-sep" />
              <Menu.Label className="hz-menu-label">{t.theme.label}</Menu.Label>
              <Menu.RadioGroup value={mode} onValueChange={(v) => setMode(v as ThemeMode)}>
                {themeOptions.filter((o) => allowed.includes(o.value)).map((o) => (
                  <Menu.RadioItem key={o.value} value={o.value} className="hz-menu-item" onSelect={(e) => e.preventDefault()}>
                    {o.icon}
                    <span className="flex-1">{o.label}</span>
                    <Menu.ItemIndicator>
                      <Check size={14} aria-hidden />
                    </Menu.ItemIndicator>
                  </Menu.RadioItem>
                ))}
              </Menu.RadioGroup>
            </>
          )}
          {!app.isLocal && (
            <>
              <Menu.Separator className="hz-menu-sep" />
              <Menu.Item
                className="hz-menu-item"
                disabled={signingOut}
                onSelect={(e) => {
                  e.preventDefault();
                  void signOut();
                }}
              >
                <LogOut size={15} aria-hidden />
                {signingOut ? t.account.signingOut : t.account.signOut}
              </Menu.Item>
            </>
          )}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
