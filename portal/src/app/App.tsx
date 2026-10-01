// The Hubzoid chat app: boots from GET /api/auth/session, sends signed-out
// people to /auth, and lays out the sidebar and the current page. Heavy pages
// (the chat thread with assistant-ui and markdown) load on demand.
import "./app.css";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, WifiOff } from "lucide-react";
import { t } from "./i18n/en";
import { get, setUnauthorizedHandler } from "./lib/api";
import { AppContext, type AgentsState, type AppContextValue } from "./lib/app-context";
import { upsertConversation } from "./lib/conversations";
import { displayName } from "./lib/format";
import { navigate, parseRoute, signInHref, useLocation, type Route } from "./lib/router";
import { applyTheme, useTheme } from "./lib/theme";
import type { AgentsResponse, Branding, Conversation, Session, SessionUser } from "./lib/types";
import { Sidebar } from "./components/Sidebar";
import { announce, LiveRegion, Toaster } from "./components/toast";
import { PageErrorBoundary } from "./components/ErrorBoundary";
import { Button, PageSpinner, StateMessage, TooltipProvider } from "./components/ui";

applyTheme();

const ChatPage = lazy(() => import("./chat/ChatPage"));
const SharePage = lazy(() => import("./pages/SharePage"));
const SignInPage = lazy(() => import("./pages/SignInPage"));
const SetPasswordPage = lazy(() => import("./pages/SetPasswordPage"));
const AccountPage = lazy(() => import("./pages/AccountPage"));
const ConnectionsPage = lazy(() => import("./pages/ConnectionsPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

const PUBLIC_ROUTES: Route["name"][] = ["signin", "set-password", "share"];

function normalizeSession(raw: Partial<Session> | null | undefined): Session {
  return {
    authenticated: !!raw?.authenticated,
    mode: raw?.mode || "accounts",
    user: raw?.user ?? null,
    providers: Array.isArray(raw?.providers) ? raw!.providers : [],
    password: raw?.password !== false,
    signup: !!raw?.signup,
    branding_name: raw?.branding_name,
  };
}

export default function App() {
  useTheme();
  const location = useLocation();
  const route = parseRoute(location.path, location.search);
  const [session, setSession] = useState<Session | null>(null);
  const [bootError, setBootError] = useState<unknown>(null);
  const [branding, setBranding] = useState<Branding>({});
  const [agents, setAgents] = useState<Omit<AgentsState, "reload">>({
    status: "loading",
    list: [],
    defaultAgent: null,
    error: null,
  });
  const [newChatNonce, setNewChatNonce] = useState(0);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const loadSession = useCallback(async () => {
    try {
      const data = await get<Session>("/api/auth/session", { quiet401: true });
      const next = normalizeSession(data);
      setSession(next);
      setBootError(null);
      return next;
    } catch (error) {
      if ((error as { status?: number })?.status === 401) {
        const next = normalizeSession({ authenticated: false });
        setSession(next);
        return next;
      }
      setBootError(error);
      return null;
    }
  }, []);

  useEffect(() => {
    void loadSession();
    get<Branding>("/api/branding", { quiet401: true })
      .then((b) => setBranding(b ?? {}))
      .catch(() => {});
  }, [loadSession]);

  // A session that ends mid-use (expired, revoked elsewhere) goes back to sign-in.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setSession((s) => (s ? { ...s, authenticated: false, user: null } : s));
      if (!location.path.startsWith("/auth")) navigate(signInHref(), { replace: true });
    });
    return () => setUnauthorizedHandler(null);
  }, [location.path]);

  const authenticated = !!session?.authenticated;
  const isPublic = PUBLIC_ROUTES.includes(route.name);

  useEffect(() => {
    if (!session) return;
    if (!authenticated && !isPublic) navigate(signInHref(location.href), { replace: true });
    else if (authenticated && route.name === "signin") navigate(route.redirect, { replace: true });
  }, [session, authenticated, isPublic, route, location.href]);

  const loadAgents = useCallback(() => {
    setAgents((a) => ({ ...a, status: "loading", error: null }));
    get<AgentsResponse>("/api/agents")
      .then((data) =>
        setAgents({
          status: "ready",
          list: Array.isArray(data?.agents) ? data.agents : [],
          defaultAgent: data?.default_agent ?? null,
          error: null,
        }),
      )
      .catch((error) => setAgents({ status: "error", list: [], defaultAgent: null, error }));
  }, []);

  useEffect(() => {
    if (authenticated) loadAgents();
  }, [authenticated, loadAgents]);

  // Branding: favicon and an optional stylesheet from the hub's branding folder.
  useEffect(() => {
    if (branding.favicon_url) {
      let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        document.head.appendChild(link);
      }
      link.href = branding.favicon_url;
      link.removeAttribute("type");
    }
    if (branding.custom_css_url && !document.querySelector("link[data-hz-branding]")) {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = branding.custom_css_url;
      css.dataset.hzBranding = "true";
      document.head.appendChild(css);
    }
  }, [branding.favicon_url, branding.custom_css_url]);

  const brandName = displayName(branding.name || session?.branding_name || "") || t.product;

  const setUser = useCallback((user: SessionUser) => {
    setSession((s) => (s ? { ...s, user: { ...s.user, ...user } } : s));
  }, []);

  const newChat = useCallback((agentId?: string | null) => {
    setNewChatNonce((n) => n + 1);
    setDrawerOpen(false);
    navigate(agentId ? `/?agent=${encodeURIComponent(agentId)}` : "/");
  }, []);

  // Screen readers hear the new page's name after an in-app navigation.
  const [firstPath] = useState(location.path);
  useEffect(() => {
    if (location.path === firstPath) return;
    const timer = setTimeout(() => announce(document.title.split(" · ")[0]), 350);
    return () => clearTimeout(timer);
  }, [location.path, firstPath]);

  // Ctrl+Shift+O (Cmd+Shift+O on a Mac) starts a new chat, as in other chat apps.
  useEffect(() => {
    if (!authenticated) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.shiftKey && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "o") {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authenticated, newChat]);

  const value: AppContextValue | null = useMemo(() => {
    if (!session) return null;
    return {
      session,
      user: session.user ?? null,
      isLocal: session.mode === "local",
      isAdmin: session.user?.role === "admin",
      branding,
      brandName,
      agents: { ...agents, reload: loadAgents },
      refreshSession: loadSession,
      setUser,
      newChat,
      newChatNonce,
      openSidebar: () => setDrawerOpen(true),
    };
  }, [session, branding, brandName, agents, loadAgents, loadSession, setUser, newChat, newChatNonce]);

  // A brand-new chat keeps its page (and its streaming reply) when it gets
  // its URL: the conversation it created is rendered under the new chat's key.
  const [adopted, setAdopted] = useState<{ key: string; id: string } | null>(null);
  const newKey = `new:${newChatNonce}:${route.name === "new" ? (route.agent ?? "") : ""}`;
  const chatKey =
    route.name === "conversation"
      ? adopted?.id === route.id
        ? adopted.key
        : `c:${route.id}`
      : newKey;

  // The chat on screen. A creation can finish after the person pressed New
  // chat or left: only the page that asked for it moves to the new URL.
  const activeChatKey = useRef<string | null>(null);
  const onScreenKey = route.name === "new" || route.name === "conversation" ? chatKey : null;
  useEffect(() => {
    activeChatKey.current = onScreenKey;
  }, [onScreenKey]);

  const onConversationCreated = useCallback((conversation: Conversation, key: string) => {
    upsertConversation(conversation);
    if (key !== activeChatKey.current) return;
    const path = window.location.pathname;
    const onScreen = path === "/" || path.startsWith("/new");
    if (!onScreen) return;
    setAdopted({ key, id: conversation.id });
    navigate(`/c/${encodeURIComponent(conversation.id)}`, { replace: true });
  }, []);

  if (bootError && !session) {
    const offline = (bootError as { status?: number })?.status === 0;
    return (
      <main className="flex min-h-dvh items-center justify-center bg-bg">
        <StateMessage
          icon={offline ? <WifiOff size={28} aria-hidden /> : <AlertTriangle size={28} aria-hidden />}
          title={offline ? t.errors.network : t.errors.bootTitle}
          action={
            <Button variant="primary" onClick={() => void loadSession()}>
              {t.retry}
            </Button>
          }
        >
          {offline ? null : t.errors.boot}
        </StateMessage>
      </main>
    );
  }
  if (!session || !value) return <PageSpinner />;

  const wrap = (node: ReactNode) => (
    <AppContext.Provider value={value}>
      <TooltipProvider delayDuration={400}>
        <PageErrorBoundary resetKey={location.path}>{node}</PageErrorBoundary>
        <Toaster />
        <LiveRegion />
      </TooltipProvider>
    </AppContext.Provider>
  );

  // Pages without the sidebar.
  if (route.name === "signin" || route.name === "set-password") {
    if (route.name === "signin" && authenticated) return <PageSpinner />;
    return wrap(
      <Suspense fallback={<PageSpinner />}>
        {route.name === "signin" ? (
          <SignInPage redirect={route.redirect} error={route.error} />
        ) : (
          <SetPasswordPage token={route.token} />
        )}
      </Suspense>,
    );
  }
  if (route.name === "share")
    return wrap(
      <Suspense fallback={<PageSpinner />}>
        <SharePage shareId={route.id} />
      </Suspense>,
    );
  if (!authenticated) return <PageSpinner />;

  let page: ReactNode;
  if (route.name === "new" || route.name === "conversation")
    page = (
      <ChatPage
        key={chatKey}
        pageKey={chatKey}
        conversationId={route.name === "conversation" ? route.id : null}
        requestedAgent={route.name === "new" ? route.agent : null}
        onConversationCreated={onConversationCreated}
      />
    );
  else if (route.name === "account") page = <AccountPage />;
  else if (route.name === "connections")
    page = <ConnectionsPage connected={route.connected} connector={route.connector} error={route.error} />;
  else page = <NotFoundPage />;

  const activeId = route.name === "conversation" ? route.id : null;

  return wrap(
    <>
      <a
        href="#main"
        className="sr-only z-[90] rounded-md bg-ink px-3 py-2 text-sm text-bg focus:not-sr-only focus:fixed focus:left-3 focus:top-3"
      >
        {t.skipToContent}
      </a>
      <div className="flex h-dvh w-full overflow-hidden bg-bg text-ink">
        <Sidebar activeId={activeId} route={route.name} drawerOpen={drawerOpen} onDrawerChange={setDrawerOpen} />
        <main id="main" tabIndex={-1} className="relative flex min-w-0 flex-1 flex-col outline-none">
          <PageErrorBoundary resetKey={location.href}>
            <Suspense fallback={<PageSpinner />}>{page}</Suspense>
          </PageErrorBoundary>
        </main>
      </div>
    </>,
  );
}
