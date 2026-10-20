// "Waiting for you": every approval and question across this person's agents,
// answered right here or opened in its chat. Each hub keeps its own requests,
// so the page asks every agent's hub and merges the lists.
import { useCallback, useEffect, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { get, hubUrl } from "../lib/api";
import { useApp } from "../lib/app-context";
import { linkClick } from "../lib/router";
import { t } from "../i18n/en";
import { FilesContext } from "../chat/parts";
import { RequestCard, type HitlRequest } from "../chat/RequestCard";
import { PageFrame } from "./PageFrame";

type Row = HitlRequest & { conversation_id?: string | null; created_at?: string; apiBase: string };

/** The distinct hub addresses of the agents this person can use. */
function useBases(): string[] {
  const app = useApp();
  const bases = new Set<string>();
  for (const a of app.agents.list) bases.add(a.api_base || "");
  if (bases.size === 0) bases.add("");
  return [...bases];
}

export async function loadPending(bases: string[], status = "pending"): Promise<Row[]> {
  const lists = await Promise.all(
    bases.map((base) =>
      get<{ items: HitlRequest[] }>(hubUrl(base, `/api/requests?status=${status}`))
        .then((r) => (r?.items ?? []).map((i) => ({ ...i, apiBase: base }) as Row))
        .catch(() => [] as Row[]),
    ),
  );
  return lists.flat().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
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
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", load);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return count;
}

export default function RequestsPage() {
  const bases = useBases();
  const key = bases.join("|");
  const [rows, setRows] = useState<Row[] | null>(null);
  const reload = useCallback(() => void loadPending(bases).then(setRows), [key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    reload();
  }, [reload]);
  const mine = (rows ?? []).filter((r) => r.allowed_actions.length > 0);
  const asked = (rows ?? []).filter((r) => r.allowed_actions.length === 0);
  return (
    <PageFrame title={t.requests.inboxTitle}>
      <div className="mx-auto w-full max-w-[720px] px-4 py-6" data-testid="requests-page">
        {rows === null ? (
          <p className="text-mute">{t.requests.loading}</p>
        ) : rows.length === 0 ? (
          <p className="text-mute" data-testid="requests-empty">
            {t.requests.inboxEmpty}
          </p>
        ) : (
          <>
            {mine.map((r) => (
              <RequestRow key={r.id} row={r} />
            ))}
            {asked.length > 0 && <h3 className="mb-1 mt-6 text-[13px] font-medium text-secondary">{t.requests.inboxAsked}</h3>}
            {asked.map((r) => (
              <RequestRow key={r.id} row={r} />
            ))}
          </>
        )}
      </div>
    </PageFrame>
  );
}

function RequestRow({ row }: { row: Row }) {
  const href = row.conversation_id ? `/c/${encodeURIComponent(row.conversation_id)}` : null;
  return (
    <div className="mb-3" data-testid="request-row">
      <FilesContext.Provider value={{ conversationId: row.conversation_id ?? null, apiBase: row.apiBase }}>
        <RequestCard requestId={row.id} toolName={row.tool ?? undefined} />
      </FilesContext.Provider>
      {href && (
        <a href={href} onClick={(e) => linkClick(e, href)} className="inline-flex items-center gap-1 text-[12.5px] text-mute">
          {t.requests.openChat} <ArrowUpRight size={12} aria-hidden />
        </a>
      )}
    </div>
  );
}
