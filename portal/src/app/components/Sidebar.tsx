// Hubzoid's own conversation sidebar: new chat, search, chats grouped by date,
// inline rename, share, archive and delete, the archived view and the account
// menu. On narrow screens it is a drawer.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Dialog as RDialog, DropdownMenu as Menu } from "radix-ui";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  Check,
  ExternalLink,
  LogOut,
  Monitor,
  Moon,
  MoreHorizontal,
  Pencil,
  Plug,
  Search,
  Share2,
  SquarePen,
  Sun,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { t } from "../i18n/en";
import { post } from "../lib/api";
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
import { useTheme, type ThemeMode } from "../lib/theme";
import type { Conversation } from "../lib/types";
import { describeError } from "../lib/errors";
import { ShareDialog } from "./ShareDialog";
import { toast } from "./toast";
import { AgentAvatar, Button, ConfirmDialog, IconButton, Notice, Spinner, UserAvatar, Wordmark, cx } from "./ui";

const GROUPS: { key: DateGroup; label: string }[] = [
  { key: "today", label: t.sidebar.today },
  { key: "yesterday", label: t.sidebar.yesterday },
  { key: "week", label: t.sidebar.week },
  { key: "older", label: t.sidebar.older },
];

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
      <aside className="hidden w-[272px] flex-none border-r border-line bg-canvas lg:flex">
        <SidebarBody activeId={activeId} route={route} />
      </aside>
      <RDialog.Root open={drawerOpen} onOpenChange={onDrawerChange}>
        <RDialog.Portal>
          <RDialog.Overlay className="hz-overlay lg:hidden" />
          <RDialog.Content className="hz-drawer lg:hidden" aria-describedby={undefined}>
            <RDialog.Title className="sr-only">{t.sidebar.label}</RDialog.Title>
            <div className="absolute right-2 top-3 z-10">
              <RDialog.Close asChild>
                <IconButton label={t.sidebar.closeMenu} tooltip={false}>
                  <X size={18} aria-hidden />
                </IconButton>
              </RDialog.Close>
            </div>
            <SidebarBody activeId={activeId} route={route} inDrawer />
          </RDialog.Content>
        </RDialog.Portal>
      </RDialog.Root>
    </>
  );
}

function Brand() {
  const { branding, brandName } = useApp();
  return (
    <a
      href="/"
      onClick={(e) => linkClick(e, "/")}
      className="flex min-w-0 items-center gap-2.5 rounded-lg px-1.5 py-1 text-ink no-underline"
    >
      {branding.logo_url ? (
        <>
          <img src={branding.logo_url} alt="" className="h-7 w-7 flex-none rounded-md object-contain" />
          <span className="truncate text-[15px] font-semibold tracking-tight">{brandName}</span>
        </>
      ) : brandName === t.product ? (
        <Wordmark className="text-[17px]" />
      ) : (
        <>
          <span
            aria-hidden
            className="flex h-7 w-7 flex-none items-center justify-center rounded-md border border-line bg-raised font-mono text-[13px] font-semibold"
          >
            <span className="text-brand">/</span>
            {brandName.slice(0, 1).toLowerCase()}
          </span>
          <span className="truncate text-[15px] font-semibold tracking-tight">{brandName}</span>
        </>
      )}
    </a>
  );
}

function SidebarBody({ activeId, route, inDrawer }: { activeId: string | null; route: Route["name"]; inDrawer?: boolean }) {
  const app = useApp();
  const list = useConversations();
  const [query, setQuery] = useState(list.q);
  const [shareTarget, setShareTarget] = useState<Conversation | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);

  // First load, and a debounced search.
  useEffect(() => {
    if (!list.loaded && !list.loading) void loadConversations(list.q, list.archived);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const q = query.trim();
    if (q === list.q) return;
    const timer = setTimeout(() => void loadConversations(q, list.archived), 250);
    return () => clearTimeout(timer);
  }, [query, list.q, list.archived]);

  const multiAgent = app.agents.list.length > 1;

  const groups = useMemo(() => {
    const now = new Date();
    const map = new Map<DateGroup, Conversation[]>();
    for (const c of list.items) {
      const g = dateGroup(c.updated_at ?? c.created_at, now);
      map.set(g, [...(map.get(g) ?? []), c]);
    }
    return GROUPS.map((g) => ({ ...g, items: map.get(g.key) ?? [] })).filter((g) => g.items.length > 0);
  }, [list.items]);

  const showArchived = (archived: boolean) => {
    setQuery("");
    void loadConversations("", archived);
  };

  const onDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteConversation({
        id: deleteTarget.id,
        api_base: deleteTarget.api_base ?? agentById(app.agents.list, deleteTarget.agent)?.api_base,
      });
      toast(t.sidebar.deletedToast);
      if (deleteTarget.id === activeId) navigate("/", { replace: true });
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
      if (archived && c.id === activeId) navigate("/", { replace: true });
    } catch (error) {
      toast(describeError(error), "error");
    }
  };

  let body: ReactNode;
  if (list.loading && list.items.length === 0) {
    body = (
      <div className="space-y-2 px-2 py-2" aria-hidden>
        {[70, 90, 60, 80, 50].map((w, i) => (
          <div key={i} className="hz-skeleton h-5" style={{ width: `${w}%` }} />
        ))}
      </div>
    );
  } else if (list.error && list.items.length === 0) {
    body = (
      <div className="px-2">
        <Notice
          tone="error"
          action={
            <Button size="sm" onClick={() => void loadConversations(list.q, list.archived)}>
              {t.retry}
            </Button>
          }
        >
          {t.sidebar.loadError} {describeError(list.error)}
        </Notice>
      </div>
    );
  } else if (list.items.length === 0) {
    body = (
      <p className="px-3 py-2 text-[13.5px] leading-relaxed text-mute">
        {list.q ? t.sidebar.emptySearch(list.q) : list.archived ? t.sidebar.emptyArchived : t.sidebar.empty}
      </p>
    );
  } else {
    body = (
      <>
        {groups.map((group) => (
          <section key={group.key} aria-labelledby={`grp-${group.key}${inDrawer ? "-d" : ""}`} className="mb-3">
            <h2
              id={`grp-${group.key}${inDrawer ? "-d" : ""}`}
              className="m-0 px-2.5 pb-1 pt-2 text-xs font-medium text-mute"
            >
              {group.label}
            </h2>
            <ul className="m-0 list-none space-y-px p-0">
              {group.items.map((c) => (
                <ConversationRow
                  key={c.id}
                  conversation={c}
                  active={c.id === activeId}
                  archivedView={list.archived}
                  agentAvatar={multiAgent ? agentById(app.agents.list, c.agent) : undefined}
                  renaming={renaming === c.id}
                  onRenameStart={() => setRenaming(c.id)}
                  onRenameEnd={() => setRenaming(null)}
                  onShare={() => setShareTarget(c)}
                  onArchive={(archived) => void onArchive(c, archived)}
                  onDelete={() => {
                    setDeleteError(null);
                    setDeleteTarget(c);
                  }}
                />
              ))}
            </ul>
          </section>
        ))}
        {list.nextCursor && (
          <div className="px-2 pb-2">
            <Button size="sm" variant="ghost" className="w-full" loading={list.loadingMore} onClick={() => void loadMore()}>
              {t.sidebar.loadMore}
            </Button>
          </div>
        )}
      </>
    );
  }

  return (
    <nav aria-label={t.sidebar.label} className="flex h-full min-h-0 w-full flex-col">
      <div className={cx("flex items-center gap-2 px-3 pb-2 pt-3.5", inDrawer && "pr-12")}>
        <Brand />
      </div>
      <div className="space-y-2 px-3 pb-2">
        <button
          type="button"
          onClick={() => app.newChat()}
          aria-keyshortcuts="Control+Shift+O Meta+Shift+O"
          className={cx(
            "hz-btn hz-btn-secondary w-full justify-start",
            route === "new" && "border-accent/40",
          )}
        >
          <SquarePen size={16} aria-hidden className="text-mute" />
          {t.sidebar.newChat}
        </button>
        <div className="relative">
          <Search size={15} aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-mute" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape" && query) {
                e.preventDefault();
                setQuery("");
              }
            }}
            placeholder={t.sidebar.searchPlaceholder}
            aria-label={t.sidebar.search}
            className="hz-input !min-h-9 !rounded-lg !py-1.5 !pl-9 !text-sm [&::-webkit-search-cancel-button]:hidden"
          />
          {query && (
            <button
              type="button"
              aria-label={t.sidebar.clearSearch}
              onClick={() => setQuery("")}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-md p-1 text-mute hover:bg-hover hover:text-ink"
            >
              <X size={14} aria-hidden />
            </button>
          )}
        </div>
      </div>

      {list.archived && (
        <div className="flex items-center gap-1 px-2 pb-1">
          <IconButton label={t.sidebar.backToChats} onClick={() => showArchived(false)}>
            <ArrowLeft size={16} aria-hidden />
          </IconButton>
          <h2 className="m-0 text-sm font-semibold">{t.sidebar.archivedTitle}</h2>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2" aria-busy={list.loading || undefined}>
        {list.loading && list.items.length > 0 && (
          <div className="flex justify-center py-1">
            <Spinner size={14} />
          </div>
        )}
        {body}
      </div>

      <div className="border-t border-line px-2 py-2">
        {!list.archived && (
          <button
            type="button"
            onClick={() => showArchived(true)}
            className="mb-1 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] text-mute hover:bg-hover hover:text-ink"
          >
            <Archive size={15} aria-hidden />
            {t.sidebar.archived}
          </button>
        )}
        <AccountMenu />
      </div>

      <ShareDialog conversation={shareTarget} onClose={() => setShareTarget(null)} />
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

function ConversationRow({
  conversation: c,
  active,
  archivedView,
  agentAvatar,
  renaming,
  onRenameStart,
  onRenameEnd,
  onShare,
  onArchive,
  onDelete,
}: {
  conversation: Conversation;
  active: boolean;
  archivedView: boolean;
  agentAvatar?: { name: string; avatar_url?: string | null };
  renaming: boolean;
  onRenameStart: () => void;
  onRenameEnd: () => void;
  onShare: () => void;
  onArchive: (archived: boolean) => void;
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
    <li className="hz-conv" data-active={active}>
      <a
        href={href}
        onClick={(e) => linkClick(e, href)}
        aria-current={active ? "page" : undefined}
        className="flex items-center gap-2"
        title={title}
      >
        {agentAvatar && <AgentAvatar name={agentAvatar.name} src={agentAvatar.avatar_url} size={18} />}
        <span className="truncate">{title}</span>
      </a>
      <Menu.Root modal={false}>
        <Menu.Trigger asChild>
          <button ref={moreRef} type="button" className="hz-icon-btn hz-conv-more" aria-label={t.sidebar.options(title)}>
            <MoreHorizontal size={16} aria-hidden />
          </button>
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content className="hz-menu" align="start" sideOffset={4}>
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
    <div className="flex items-center gap-1 rounded-lg bg-hover p-1">
      <input
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => finish(true)}
        aria-label={t.sidebar.renameLabel}
        maxLength={200}
        className="hz-input !min-h-8 flex-1 !px-2 !py-1 !text-sm"
      />
      <button
        type="button"
        aria-label={t.save}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => finish(true)}
        className="hz-icon-btn !h-8 !w-8"
      >
        <Check size={15} aria-hidden />
      </button>
    </div>
  );
}

const themeOptions: { value: ThemeMode; label: string; icon: ReactNode }[] = [
  { value: "light", label: t.theme.light, icon: <Sun size={15} aria-hidden /> },
  { value: "dark", label: t.theme.dark, icon: <Moon size={15} aria-hidden /> },
  { value: "system", label: t.theme.system, icon: <Monitor size={15} aria-hidden /> },
];

function AccountMenu() {
  const app = useApp();
  const { mode, setMode } = useTheme();
  const [signingOut, setSigningOut] = useState(false);
  const name = app.isLocal ? t.account.local : app.user?.name || app.user?.email || "";
  const email = app.user?.email && app.user.email !== name ? app.user.email : "";

  const signOut = async () => {
    setSigningOut(true);
    try {
      await post("/api/auth/logout", undefined, { quiet401: true });
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
        <button
          type="button"
          className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-hover data-[state=open]:bg-hover"
          aria-label={`${t.account.menu}: ${displayName(name)}`}
        >
          <UserAvatar name={name || "?"} size={28} />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-ink">{displayName(name)}</span>
            {email && <span className="block truncate text-xs text-mute">{email}</span>}
          </span>
          <MoreHorizontal size={16} aria-hidden className="text-mute" />
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="hz-menu w-[240px]" side="top" align="start" sideOffset={6}>
          <Menu.Label className="hz-menu-label">
            <span className="block truncate font-medium text-ink">{displayName(name)}</span>
            {app.user?.email && <span className="block truncate">{app.user.email}</span>}
          </Menu.Label>
          <Menu.Separator className="hz-menu-sep" />
          <Menu.Item className="hz-menu-item" onSelect={() => navigate("/account")}>
            <UserRound size={15} aria-hidden />
            {t.account.account}
          </Menu.Item>
          <Menu.Item className="hz-menu-item" onSelect={() => navigate("/account/connections")}>
            <Plug size={15} aria-hidden />
            {t.account.connections}
          </Menu.Item>
          {app.isAdmin && (
            <Menu.Item className="hz-menu-item" onSelect={() => location.assign("/portal/")}>
              <ExternalLink size={15} aria-hidden />
              {t.account.console}
            </Menu.Item>
          )}
          <Menu.Separator className="hz-menu-sep" />
          <Menu.Label className="hz-menu-label !pb-1 !pt-1">{t.theme.label}</Menu.Label>
          <Menu.RadioGroup value={mode} onValueChange={(v) => setMode(v as ThemeMode)}>
            {themeOptions.map((o) => (
              <Menu.RadioItem key={o.value} value={o.value} className="hz-menu-item" onSelect={(e) => e.preventDefault()}>
                {o.icon}
                <span className="flex-1">{o.label}</span>
                <Menu.ItemIndicator>
                  <Check size={15} aria-hidden className="!text-accent-text" />
                </Menu.ItemIndicator>
              </Menu.RadioItem>
            ))}
          </Menu.RadioGroup>
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
