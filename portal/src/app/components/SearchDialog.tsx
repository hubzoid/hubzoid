// Search every chat you can open: titles first, then matches inside messages,
// each with its agent. Opens from the sidebar or with ⌘K / Ctrl K.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Dialog as RDialog } from "radix-ui";
import { Search } from "lucide-react";
import { t } from "../i18n/en";
import { get } from "../lib/api";
import { agentById, useApp } from "../lib/app-context";
import { formatDate } from "../lib/format";
import { navigate } from "../lib/router";
import { sidebar, useSidebarState } from "../lib/sidebarState";
import type { Conversation, ConversationPage } from "../lib/types";
import { AgentAvatar, cx } from "./ui";

type Hit = { kind: string; title: string; snippet: string; highlights?: [number, number][]; open_target: { conversation_id: string; message_id?: string | null } };
type Row = { id: string; title: string; snippet?: string; highlights?: [number, number][]; agent?: string; when?: Conversation["updated_at"] };

/** A one-line snippet that starts near the first match, with the matches marked. */
function Snippet({ text, marks }: { text: string; marks?: [number, number][] }) {
  const sorted = [...(marks ?? [])].sort((a, b) => a[0] - b[0]);
  const from = sorted.length && sorted[0][0] > 40 ? Math.max(0, text.lastIndexOf(" ", sorted[0][0] - 24) + 1) : 0;
  const parts: ReactNode[] = from ? ["…"] : [];
  let at = from;
  for (const [a, b] of sorted) {
    if (a < at) continue;
    parts.push(text.slice(at, a), <mark key={a} className="hz-search-mark">{text.slice(a, b)}</mark>);
    at = b;
  }
  parts.push(text.slice(at));
  return <span className="block truncate text-[12px] text-mute">{parts}</span>;
}

export function SearchDialog() {
  const { searchOpen } = useSidebarState();
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        sidebar.openSearch();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <RDialog.Root open={searchOpen} onOpenChange={(o) => (o ? sidebar.openSearch() : sidebar.closeSearch())}>
      <RDialog.Portal>
        <RDialog.Overlay className="hz-overlay" />
        <RDialog.Content
          className="hz-search"
          aria-describedby={undefined}
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            (e.currentTarget as HTMLElement | null)?.querySelector<HTMLInputElement>("input")?.focus();
          }}
        >
          <RDialog.Title className="sr-only">{t.sidebar.search}</RDialog.Title>
          {searchOpen && <SearchBody />}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

function SearchBody() {
  const app = useApp();
  const [q, setQ] = useState("");
  const [titles, setTitles] = useState<Conversation[]>([]);
  const [hits, setHits] = useState<Hit[]>([]);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState(0);
  const [failed, setFailed] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const generation = useRef(0);

  useEffect(() => {
    const query = q.trim();
    const timer = setTimeout(() => {
      const mine = ++generation.current;
      const current = () => mine === generation.current;
      setBusy(true);
      const params = new URLSearchParams({ archived: "0", limit: "20" });
      if (query) params.set("q", query);
      const a = get<ConversationPage>(`/api/conversations?${params}`, { quiet401: true }).then((p) => current() && setTitles(p.items ?? []));
      const b = query.length >= 2
        ? get<{ items: Hit[] }>(`/api/chat/search?q=${encodeURIComponent(query)}&types=message&limit=20`, { quiet401: true })
            .then((r) => current() && setHits(r.items))
        : Promise.resolve(setHits([]));
      void Promise.allSettled([a, b]).then((out) => {
        if (!current()) return;
        setBusy(false);
        setFailed(out.some((o) => o.status === "rejected"));
      });
    }, query ? 180 : 0);
    return () => clearTimeout(timer);
  }, [q]);

  const rows: Row[] = useMemo(() => {
    const seen = new Set<string>();
    const out: Row[] = [];
    for (const c of titles) {
      seen.add(c.id);
      out.push({ id: c.id, title: c.title?.trim() || t.sidebar.untitled, agent: c.agent, when: c.updated_at ?? c.created_at });
    }
    for (const h of hits) {
      const id = h.open_target.conversation_id;
      if (seen.has(id)) {
        const row = out.find((r) => r.id === id);
        if (row && !row.snippet) Object.assign(row, { snippet: h.snippet, highlights: h.highlights });
        continue;
      }
      seen.add(id);
      out.push({ id, title: h.title || t.sidebar.untitled, snippet: h.snippet, highlights: h.highlights });
    }
    return out.slice(0, 30);
  }, [titles, hits]);

  useEffect(() => setActive(0), [q]);
  useEffect(() => setActive((i) => Math.min(i, Math.max(0, rows.length - 1))), [rows.length]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const open = (row: Row) => {
    sidebar.closeSearch();
    navigate(`/c/${encodeURIComponent(row.id)}`);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!rows.length) return;
      setActive((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length);
    } else if (e.key === "Enter" && rows[active]) {
      e.preventDefault();
      open(rows[active]);
    }
  };
  const multi = app.agents.list.length > 1;

  return (
    <>
      <div className="hz-search-input">
        <Search size={18} aria-hidden className="text-mute" />
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t.sidebar.searchPlaceholder}
          aria-label={t.sidebar.search}
          role="combobox"
          aria-expanded
          aria-controls="hz-search-results"
          aria-activedescendant={rows.length ? `hz-search-${active}` : undefined}
        />
        {busy && <span className="hz-spin-dot" aria-hidden />}
      </div>
      <div className="hz-search-results" id="hz-search-results" role="listbox" ref={listRef} aria-label={t.sidebar.search}>
        {failed && <p className="m-0 px-3 pt-3 text-[12.5px] text-danger">{t.sidebar.loadError}</p>}
        {!rows.length && !busy && !failed && <p className="m-0 px-3 py-6 text-center text-[13px] text-mute">{q.trim() ? t.sidebar.emptySearch(q.trim()) : t.sidebar.empty}</p>}
        {!q.trim() && rows.length > 0 && <div className="hz-search-label">{t.sidebar.recent}</div>}
        {rows.map((r, i) => {
          const agent = r.agent ? agentById(app.agents.list, r.agent) : undefined;
          return (
            <div
              key={r.id}
              id={`hz-search-${i}`}
              data-index={i}
              role="option"
              aria-selected={i === active}
              className={cx("hz-search-row", i === active && "is-active")}
              onMouseMove={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => open(r)}
            >
              {multi && agent ? <AgentAvatar name={agent.name} src={agent.avatar_url} size={18} /> : <Search size={14} aria-hidden className="text-mute" />}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium text-ink">{r.title}</span>
                {r.snippet && <Snippet text={r.snippet} marks={r.highlights} />}
              </span>
              {r.when && <span className="flex-none text-[11.5px] tabular-nums text-mute">{formatDate(r.when, { month: "short", day: "numeric" })}</span>}
            </div>
          );
        })}
      </div>
      <div className="hz-search-foot">
        <span><kbd className="hz-kbd">↑</kbd> <kbd className="hz-kbd">↓</kbd> {t.sidebar.searchMove}</span>
        <span><kbd className="hz-kbd">↵</kbd> {t.sidebar.searchOpen}</span>
        <span><kbd className="hz-kbd">esc</kbd> {t.close}</span>
      </div>
    </>
  );
}
