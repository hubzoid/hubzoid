// A shared conversation (/s/:id): a read-only copy rendered with the same
// message components as the chat.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { AssistantRuntimeProvider, useExternalStoreRuntime, type ThreadMessage } from "@assistant-ui/react";
import { Link2Off, WifiOff } from "lucide-react";
import { t } from "../i18n/en";
import { ApiError, enc, get } from "../lib/api";
import { agentById, useApp } from "../lib/app-context";
import { describeError } from "../lib/errors";
import { displayName, formatDate, pageTitle } from "../lib/format";
import { linkClick, navigate, signInHref } from "../lib/router";
import type { Agent, SharedConversation } from "../lib/types";
import { AgentAvatar, BrandMark, Button, PageSpinner, StateMessage } from "../components/ui";
import { toThreadMessages } from "../chat/convert";
import { Thread } from "../chat/Thread";
import { ThemeSwitch } from "./AuthLayout";

/** A runtime that only shows the snapshot: nothing can be sent or edited. */
function ReadOnlyRuntime({ messages, children }: { messages: ThreadMessage[]; children: ReactNode }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages,
    isRunning: false,
    isDisabled: true,
    onNew: async () => {},
  });
  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>;
}

export default function SharePage({ shareId }: { shareId: string }) {
  const app = useApp();
  const [share, setShare] = useState<SharedConversation | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    get<SharedConversation>(`/api/shares/${enc(shareId)}`, { quiet401: true })
      .then((data) => !cancelled && setShare(data))
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          navigate(signInHref(`/s/${encodeURIComponent(shareId)}`), { replace: true });
          return;
        }
        setError(e);
      });
    return () => {
      cancelled = true;
    };
  }, [shareId, attempt]);

  const messages = useMemo(
    () => (share ? toThreadMessages(share.messages ?? [], { conversationId: "", apiBase: "" }) : []),
    [share],
  );

  const agent: Agent = useMemo(() => {
    const id = share?.agent ?? "";
    return agentById(app.agents.list, id) ?? { id, name: id || t.agents.unknown };
  }, [share, app.agents.list]);

  const title = share?.title?.trim() || t.sidebar.untitled;
  useEffect(() => {
    document.title = pageTitle(share ? title : t.share.title, app.brandName);
  }, [share, title, app.brandName]);

  const header = (
    <header className="flex h-14 flex-none items-center justify-between border-b border-line px-4 sm:px-6">
      <a href="/" onClick={(e) => linkClick(e, "/")} className="flex min-w-0 items-center gap-2.5 no-underline">
        <BrandMark logoUrl={app.branding.logo_url} name={app.brandName} />
      </a>
      <ThemeSwitch />
    </header>
  );

  if (error) {
    const missing = error instanceof ApiError && (error.status === 404 || error.status === 403 || error.status === 410);
    return (
      <div className="flex min-h-dvh flex-col bg-bg">
        {header}
        <main id="main" className="flex flex-1 items-center justify-center">
          <StateMessage
            icon={missing ? <Link2Off size={28} aria-hidden /> : <WifiOff size={28} aria-hidden />}
            title={missing ? t.share.notFoundTitle : t.errors.network}
            action={
              missing ? (
                <Button variant="primary" onClick={() => navigate("/")}>
                  {t.goHome}
                </Button>
              ) : (
                <Button onClick={() => setAttempt((n) => n + 1)}>{t.retry}</Button>
              )
            }
          >
            {missing ? t.share.notFound : describeError(error)}
          </StateMessage>
        </main>
      </div>
    );
  }
  if (!share) return <PageSpinner />;

  const date = formatDate(share.created_at);
  return (
    <div className="flex h-dvh flex-col bg-bg">
      {header}
      <main id="main" className="flex min-h-0 flex-1 flex-col">
        <ReadOnlyRuntime messages={messages}>
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-[780px] px-4 pt-8 sm:px-6 sm:pt-12">
                <div className="flex items-center gap-2.5 text-[13px] text-mute">
                  <AgentAvatar name={agent.name} src={agent.avatar_url} size={22} />
                  <span>{displayName(agent.name)}</span>
                </div>
                <h1 className="m-0 mt-3 text-[26px] font-semibold leading-tight tracking-tight text-ink sm:text-[30px]">
                  {title}
                </h1>
                <p className="m-0 mt-2 text-[14px] text-mute" data-testid="share-byline">
                  {t.share.byline(share.owner_name || t.agents.unknown, date)}
                </p>
              </div>
              <div className="flex min-h-0 flex-col [&_[data-testid=thread-viewport]]:overflow-visible">
                <Thread agent={agent} agents={[]} canPickAgent={false} onPickAgent={() => {}} readOnly />
              </div>
              <footer className="mx-auto w-full max-w-[780px] px-4 pb-12 sm:px-6">
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-canvas px-4 py-3.5">
                  <p className="m-0 text-[13.5px] text-mute">{t.share.readOnly}</p>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => (app.session.authenticated ? app.newChat() : navigate("/auth"))}
                  >
                    {t.share.startOwn}
                  </Button>
                </div>
              </footer>
            </div>
          </div>
        </ReadOnlyRuntime>
      </main>
    </div>
  );
}
