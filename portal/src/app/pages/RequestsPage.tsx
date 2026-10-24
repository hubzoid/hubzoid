// Inbox: one place for everything that needs this person or happened for them,
// like a notifications page. "Needs you" holds approvals and questions they can
// answer right here; "Updates" holds replies that finished elsewhere and
// requests waiting on someone else; "My work" holds the longer jobs this person
// asked for; "Decided" holds what was answered. Each hub keeps its own
// requests, so the page asks every agent's hub and merges them.
import { Suspense, useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { AlertTriangle, ArrowUpRight, Check, CircleHelp, CirclePause, MailOpen, MessageSquare, ShieldCheck } from "lucide-react";
import { get, hubUrl } from "../lib/api";
import { useApp } from "../lib/app-context";
import { formatRelative } from "../lib/format";
import { markAllRead, markNotice, noticeLabel, useNotices, type Notice } from "../lib/notices";
import { linkClick, navigate } from "../lib/router";
import { t } from "../i18n/en";
import { FilesContext } from "../chat/parts";
import { RequestCard, type HitlRequest } from "../chat/RequestCard";
import { Select } from "../components/Select";
import { Button, Notice as Callout, cx } from "../components/ui";
import { lazyChunk } from "../../lib/lazyChunk";
import { PageFrame } from "./PageFrame";

type Row = HitlRequest & { conversation_id?: string | null; created_at?: string; apiBase: string };
type Tab = "needs" | "updates" | "work" | "done";

/** The distinct hub addresses of the agents this person can use. */
function useBases(): string[] {
  const app = useApp();
  const bases = new Set<string>();
  for (const a of app.agents.list) bases.add(a.api_base || "");
  if (bases.size === 0) bases.add("");
  return [...bases];
}

const ConsoleEmbed = lazyChunk(() => import("../../ConsoleEmbed"));

export async function loadPending(bases: string[], status = "pending"): Promise<Row[]> {
  return (await loadChecked(bases, status)).rows;
}

/** The requests of every agent, and the agents that could not be checked: a
 *  failed check is never shown as "nothing waiting" (HUB-140 F40). */
async function loadChecked(bases: string[], status = "pending"): Promise<{ rows: Row[]; failed: string[] }> {
  const failed: string[] = [];
  const lists = await Promise.all(
    bases.map((base) =>
      get<{ items: HitlRequest[] }>(hubUrl(base, `/api/requests?status=${status}`))
        .then((r) => (r?.items ?? []).map((i) => ({ ...i, apiBase: base }) as Row))
        .catch(() => {
          failed.push(base);
          return [] as Row[];
        }),
    ),
  );
  const rows = lists.flat().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  return { rows, failed };
}

/** How many requests wait for this person's answer (for the sidebar). */
export function usePendingCount(): number {
  const bases = useBases();
  const key = bases.join("|");
  const [count, setCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () =>
      void loadPending(bases).then((rows) => alive && setCount(rows.filter((r) => r.allowed_actions.length > 0).length));
    load();
    // One count for the sidebar: refreshed on focus, and every minute only
    // while this tab is visible (each agent is one request, so hidden tabs
    // and short intervals multiply quickly).
    const timer = window.setInterval(() => document.visibilityState === "visible" && load(), 60_000);
    const onVisible = () => document.visibilityState === "visible" && load();
    window.addEventListener("focus", load);
    window.addEventListener("hz:inbox-changed", load);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", load);
      window.removeEventListener("hz:inbox-changed", load);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return count;
}

/** The sidebar's Inbox count: what needs an answer, plus unread updates. */
export function useInboxCount(): number {
  const pending = usePendingCount();
  const unread = useNotices().filter((n) => n.state === "unread").length;
  return pending + unread;
}

const seconds = (value?: string | number | null) =>
  value == null ? 0 : typeof value === "number" ? value : Date.parse(value) / 1000 || 0;

export default function RequestsPage() {
  const app = useApp();
  const bases = useBases();
  const key = bases.join("|");
  const notices = useNotices();
  const [pending, setPending] = useState<Row[] | null>(null);
  const [failed, setFailed] = useState<string[]>([]);
  const [decided, setDecided] = useState<Row[] | null>(null);
  // ?tab=work (and ?hub=&item=) opens My work, as old Settings links do.
  const asked = new URLSearchParams(location.search);
  const [tab, setTab] = useState<Tab>(asked.get("tab") === "work" ? "work" : "needs");
  useEffect(() => {
    const hub = asked.get("hub"), item = asked.get("item");
    if (hub && item) history.replaceState(null, "", `${location.pathname}${location.search}#/me/work/${hub}/${item}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [agent, setAgent] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const reload = useCallback(() => {
    void loadChecked(bases).then((r) => {
      setPending(r.rows);
      setFailed(r.failed);
    });
    void loadPending(bases, "all").then((rows) => setDecided(rows.filter((r) => r.status !== "pending")));
    window.dispatchEvent(new Event("hz:inbox-changed"));
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => reload(), [reload]);

  const agentOf = useCallback(
    (base: string | undefined, name?: string | null) =>
      app.agents.list.find((a) => (base !== undefined && (a.api_base || "") === base) || (name && (a.id === name || a.hub === name))),
    [app.agents.list],
  );
  const keep = (base?: string, name?: string | null) => !agent || agentOf(base, name)?.id === agent;

  const needs = (pending ?? []).filter((r) => r.allowed_actions.length > 0 && keep(r.apiBase));
  const waiting = (pending ?? []).filter((r) => r.allowed_actions.length === 0 && keep(r.apiBase));
  const updates = notices.filter((n) => keep(undefined, n.hub));
  const done = (decided ?? []).filter((r) => keep(r.apiBase)).slice(0, 50);
  const unread = updates.filter((n) => n.state === "unread").length;

  // Until a tab is chosen, land on the first one with something in it.
  const counts: Record<Tab, number> = { needs: needs.length, updates: unread, work: 0, done: 0 };
  const [chose, setChose] = useState(asked.get("tab") === "work");
  const shown: Tab = chose || pending === null ? tab : needs.length === 0 && unread > 0 ? "updates" : tab;

  const agentOptions = useMemo(
    () => [{ value: "", label: "All agents" }, ...app.agents.list.map((a) => ({ value: a.id, label: a.name }))],
    [app.agents.list],
  );

  return (
    <PageFrame
      title="Inbox"
      description="Approvals, questions and updates from your agents."
      wide
      actions={
        shown === "updates" && unread > 0 ? (
          <Button variant="secondary" size="sm" onClick={() => void markAllRead()}>
            {t.notices.markAll}
          </Button>
        ) : undefined
      }
    >
      <div data-testid="requests-page">
        <div className="hz-inbox-bar">
          <nav className="hz-inbox-tabs" aria-label="Inbox">
            {([
              ["needs", "Needs you"],
              ["updates", "Updates"],
              ["work", "My work"],
              ["done", "Decided"],
            ] as const).map(([id, label]) => (
              <button
                key={id}
                type="button"
                aria-current={shown === id ? "page" : undefined}
                onClick={() => {
                  setChose(true);
                  setTab(id);
                }}
              >
                {label}
                {counts[id] > 0 && <span className="hz-inbox-n">{counts[id]}</span>}
              </button>
            ))}
          </nav>
          {app.agents.list.length > 1 && (
            <Select aria-label="Agent" value={agent} onValueChange={setAgent} options={agentOptions} className="hz-inbox-filter" />
          )}
        </div>

        {shown === "needs" && failed.length > 0 && (
          <Callout tone="warning" className="mb-3">
            Couldn't check {failed.map((b) => agentOf(b)?.name ?? "an agent").join(", ")}.{" "}
            <button type="button" className="hz-link-btn" onClick={reload}>Retry</button>
          </Callout>
        )}
        {shown === "work" ? (
          <Suspense fallback={<p className="hz-inbox-empty">{t.requests.loading}</p>}>
            <ConsoleEmbed personal="work" />
          </Suspense>
        ) : pending === null ? (
          <p className="hz-inbox-empty">{t.requests.loading}</p>
        ) : shown === "needs" ? (
          needs.length === 0 && failed.length > 0 ? null : needs.length === 0 ? (
            <Empty icon={<Check size={18} aria-hidden />} title="You're all caught up" text="Approvals and questions from your agents appear here." testId="requests-empty" />
          ) : (
            <ul className="hz-inbox-list">
              {needs.map((r) => (
                <RequestRow key={r.id} row={r} agentName={agentOf(r.apiBase)?.name} open={open === r.id}
                  toggle={() => setOpen(open === r.id ? null : r.id)} onDone={reload} />
              ))}
            </ul>
          )
        ) : shown === "updates" ? (
          updates.length === 0 && waiting.length === 0 ? (
            <Empty icon={<MessageSquare size={18} aria-hidden />} title="No updates" text="Replies that finish while you're elsewhere appear here." />
          ) : (
            <>
              <ul className="hz-inbox-list">
                {updates.map((n) => (
                  <NoticeRow key={n.id} notice={n} agentName={agentOf(undefined, n.hub)?.name} />
                ))}
              </ul>
              {waiting.length > 0 && (
                <>
                  <h3 className="hz-inbox-sub">Waiting on someone else</h3>
                  <ul className="hz-inbox-list">
                    {waiting.map((r) => (
                      <RequestRow key={r.id} row={r} agentName={agentOf(r.apiBase)?.name} open={open === r.id}
                        toggle={() => setOpen(open === r.id ? null : r.id)} onDone={reload} />
                    ))}
                  </ul>
                </>
              )}
            </>
          )
        ) : done.length === 0 ? (
          <Empty icon={<Check size={18} aria-hidden />} title="Nothing decided yet" text="Approvals and questions you or others answered appear here." />
        ) : (
          <ul className="hz-inbox-list">
            {done.map((r) => (
              <DoneRow key={r.id} row={r} agentName={agentOf(r.apiBase)?.name} />
            ))}
          </ul>
        )}
      </div>
    </PageFrame>
  );
}

function Empty({ icon, title, text, testId }: { icon: ReactNode; title: string; text: string; testId?: string }) {
  return (
    <div className="hz-inbox-none" data-testid={testId}>
      <span className="hz-inbox-icon">{icon}</span>
      <strong>{title}</strong>
      <p>{text}</p>
    </div>
  );
}

function Meta({ children }: { children: ReactNode }) {
  return <span className="hz-inbox-meta">{children}</span>;
}

/** An approval or question: the whole row opens it in place, so it can be
 *  answered here; Open chat goes to the conversation it came from. */
function RequestRow({ row, agentName, open, toggle, onDone }: {
  row: Row;
  agentName?: string;
  open: boolean;
  toggle: () => void;
  onDone: () => void;
}) {
  const href = row.conversation_id ? `/c/${encodeURIComponent(row.conversation_id)}` : null;
  const mine = row.allowed_actions.length > 0;
  const question = row.kind === "question";
  const title = row.title || (question ? row.question : null) || (row.tool ? `Run ${row.tool}` : t.requests.needed);
  return (
    <li className={cx("hz-inbox-item", mine && "is-unread", open && "is-open")} data-testid="request-row">
      <button type="button" className="hz-inbox-row" aria-expanded={open} onClick={toggle}>
        <span className="hz-inbox-dot" aria-hidden />
        <span className={cx("hz-inbox-icon", !question && "is-accent")}>
          {question ? <CircleHelp size={16} aria-hidden /> : <ShieldCheck size={16} aria-hidden />}
        </span>
        <span className="hz-inbox-text">
          <span className="hz-inbox-title">{title}</span>
          <span className="hz-inbox-ctx">
            {[agentName, row.tool && !question ? row.tool : null, mine ? null : t.requests.waitingFor(row.requester_email ?? "")]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </span>
        <span className={cx("hz-status", mine ? (question ? "is-info" : "is-accent") : "is-muted")}>
          <i aria-hidden />
          {mine ? (question ? "Question" : "Needs approval") : "Waiting"}
        </span>
        <Meta>{formatRelative(seconds(row.created_at))}</Meta>
      </button>
      {open && (
        <div className="hz-inbox-detail">
          <FilesContext.Provider value={{ conversationId: row.conversation_id ?? null, apiBase: row.apiBase }}>
            <RequestCard requestId={row.id} toolName={row.tool ?? undefined} />
          </FilesContext.Provider>
          <div className="hz-inbox-detail-foot">
            {href && (
              <a href={href} onClick={(e) => linkClick(e, href)}>
                {t.requests.openChat} <ArrowUpRight size={12} aria-hidden />
              </a>
            )}
            <button type="button" className="hz-link-btn" onClick={onDone}>Refresh</button>
          </div>
        </div>
      )}
    </li>
  );
}

/** A reply that finished, failed or stopped while this person was elsewhere.
 *  The row opens the chat; Done clears it from the inbox. */
function NoticeRow({ notice, agentName }: { notice: Notice; agentName?: string }) {
  const unread = notice.state === "unread";
  const failed = notice.kind === "run.failed";
  const stopped = notice.kind === "run.interrupted";
  const open = () => {
    if (unread) void markNotice(notice.id, "read").catch(() => {});
    navigate(notice.link);
  };
  return (
    <li className={cx("hz-inbox-item", unread && "is-unread")}>
      <div className="hz-inbox-row" role="link" tabIndex={0} onClick={open}
        onKeyDown={(e) => {
          // Only the row itself: Enter on Done or Mark as read is that button's.
          if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); }
        }}>
        <span className="hz-inbox-dot" aria-hidden />
        <span className={cx("hz-inbox-icon", failed && "is-bad")}>
          {failed ? <AlertTriangle size={16} aria-hidden /> : stopped ? <CirclePause size={16} aria-hidden /> : <MessageSquare size={16} aria-hidden />}
        </span>
        <span className="hz-inbox-text">
          <span className="hz-inbox-title">{notice.title || t.sidebar.untitled}</span>
          <span className="hz-inbox-ctx">{[agentName, noticeLabel(notice.kind)].filter(Boolean).join(" · ")}</span>
        </span>
        <span className="hz-inbox-acts">
          {unread && (
            <button type="button" className="hz-icon-btn" aria-label={t.notices.markRead} title={t.notices.markRead}
              onClick={(e) => { e.stopPropagation(); void markNotice(notice.id, "read").catch(() => {}); }}>
              <MailOpen size={14} aria-hidden />
            </button>
          )}
          <button type="button" className="hz-icon-btn" aria-label="Done" title="Done"
            onClick={(e) => { e.stopPropagation(); void markNotice(notice.id, "dismissed").catch(() => {}); }}>
            <Check size={14} aria-hidden />
          </button>
        </span>
        <span className={cx("hz-status", failed ? "is-bad" : stopped ? "is-warn" : "is-ok")}>
          <i aria-hidden />
          {failed ? "Failed" : stopped ? "Stopped" : "Ready"}
        </span>
        <Meta>{formatRelative(notice.created_at)}</Meta>
      </div>
    </li>
  );
}

const OUTCOME: Record<string, string> = {
  approved: t.requests.approved, rejected: t.requests.rejected, answered: t.requests.answered, cancelled: t.requests.cancelled,
};

function DoneRow({ row, agentName }: { row: Row; agentName?: string }) {
  const href = row.conversation_id ? `/c/${encodeURIComponent(row.conversation_id)}` : null;
  const title = row.title || row.question || (row.tool ? `Run ${row.tool}` : t.requests.title);
  const bad = row.status === "rejected" || row.status === "cancelled";
  const body = (
    <>
      <span className="hz-inbox-dot" aria-hidden />
      <span className="hz-inbox-icon">{row.kind === "question" ? <CircleHelp size={16} aria-hidden /> : <ShieldCheck size={16} aria-hidden />}</span>
      <span className="hz-inbox-text">
        <span className="hz-inbox-title">{title}</span>
        <span className="hz-inbox-ctx">{[agentName, row.decided_by_email ? `by ${row.decided_by_email}` : null, row.note].filter(Boolean).join(" · ")}</span>
      </span>
      <span className={cx("hz-status", bad ? "is-muted" : "is-ok")}>
        <i aria-hidden />
        {OUTCOME[row.status] ?? row.status}
      </span>
      <Meta>{formatRelative(seconds(row.created_at))}</Meta>
    </>
  );
  return (
    <li className="hz-inbox-item">
      {href ? (
        <a className="hz-inbox-row" href={href} onClick={(e) => linkClick(e, href)}>{body}</a>
      ) : (
        <div className="hz-inbox-row">{body}</div>
      )}
    </li>
  );
}
