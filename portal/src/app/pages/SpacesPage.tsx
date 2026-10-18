// Spaces: team channels and direct messages (threads, mentions, @agent,
// reactions, pins), notes with revisions, and a week calendar with reminders.
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BellOff, Bell, CalendarDays, ChevronLeft, ChevronRight, Hash, MessageSquare, NotebookPen, Pencil, Pin, Plus,
  Reply, Send, Trash2, User, X } from "lucide-react";
import { ApiError, del, get, patch, post, put } from "../lib/api";
import { describeError } from "../lib/errors";
import { navigate } from "../lib/router";
import { useApp } from "../lib/app-context";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { Button, Field, IconButton, Modal, Notice, StateMessage, cx } from "../components/ui";
import { PageFrame } from "./PageFrame";

type Member = { user_id: string; name: string; email: string; role: string };
type Space = { id: string; kind: "channel" | "dm"; name: string; open: boolean; revision: number; members: Member[];
  muted: boolean; role: string; unread: number };
type Post = { id: string; parent_id: string | null; author: { kind: string; name: string; id?: string }; body: string;
  state: string; revision: number; edited: boolean; pinned: boolean; replies: number; created_at: number; mine: boolean;
  reactions: { emoji: string; count: number; mine: boolean }[] };
const EMOJI = ["👍", "✅", "👀", "❤️", "🎉", "🙏"];

const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });

export default function SpacesPage({ spaceId, tab }: { spaceId: string | null; tab: string | null }) {
  const view = tab === "notes" || tab === "calendar" ? tab : "channels";
  const go = (next: string) => navigate(next === "channels" ? (spaceId ? `/spaces/${spaceId}` : "/spaces") : `/spaces?tab=${next}`);
  return (
    <PageFrame title={t.spaces.title} description={t.spaces.description} wide>
      <nav className="mb-4 flex gap-1 border-b border-line" aria-label={t.spaces.title}>
        {(["channels", "notes", "calendar"] as const).map((k) => (
          <button key={k} type="button" onClick={() => go(k)} aria-current={view === k ? "page" : undefined}
            className={cx("-mb-px flex items-center gap-1.5 border-b-2 px-3 pb-2 pt-1 text-[13px] font-medium",
              view === k ? "border-accent text-ink" : "border-transparent text-mute hover:text-ink")}>
            {k === "channels" ? <MessageSquare size={14} aria-hidden /> : k === "notes" ? <NotebookPen size={14} aria-hidden /> : <CalendarDays size={14} aria-hidden />}
            {t.spaces.tabs[k]}
          </button>
        ))}
      </nav>
      {view === "channels" ? <Channels spaceId={spaceId} /> : view === "notes" ? <Notes /> : <Calendar />}
    </PageFrame>
  );
}

// -- channels ---------------------------------------------------------------------------------
function Channels({ spaceId }: { spaceId: string | null }) {
  const [spaces, setSpaces] = useState<Space[] | null>(null);
  const [creating, setCreating] = useState<null | "channel" | "dm">(null);
  const load = useCallback(() => get<{ items: Space[] }>("/api/chat/spaces").then((o) => setSpaces(o.items)).catch(() => setSpaces([])), []);
  useEffect(() => void load(), [load]);
  const current = spaces?.find((s) => s.id === spaceId) ?? null;
  return (
    <div className="grid min-h-[520px] gap-4 md:grid-cols-[220px_1fr]">
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-mute">{t.spaces.channels}</span>
          <button type="button" className="hz-icon-link" aria-label={t.spaces.newChannel} title={t.spaces.newChannel} onClick={() => setCreating("channel")}>
            <Plus size={14} aria-hidden /></button>
        </div>
        {spaces?.filter((s) => s.kind === "channel").map((s) => <SpaceRow key={s.id} s={s} on={s.id === spaceId} />)}
        <div className="mb-1 mt-4 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-mute">{t.spaces.direct}</span>
          <button type="button" className="hz-icon-link" aria-label={t.spaces.newDm} title={t.spaces.newDm} onClick={() => setCreating("dm")}>
            <Plus size={14} aria-hidden /></button>
        </div>
        {spaces?.filter((s) => s.kind === "dm").map((s) => <SpaceRow key={s.id} s={s} on={s.id === spaceId} />)}
      </div>
      <div className="min-w-0">
        {current ? <Channel key={current.id} space={current} onChanged={load} />
          : <StateMessage icon={<Hash size={20} aria-hidden />} title={t.spaces.pickTitle}>{t.spaces.pickBody}</StateMessage>}
      </div>
      <NewSpace kind={creating} onClose={() => setCreating(null)} onCreated={(s) => { setCreating(null); void load(); navigate(`/spaces/${s.id}`); }} />
    </div>
  );
}

function SpaceRow({ s, on }: { s: Space; on: boolean }) {
  return (
    <button type="button" onClick={() => navigate(`/spaces/${s.id}`)} className={cx("hz-file-row", on && "hz-file-row-on")}>
      {s.kind === "channel" ? <Hash size={13} aria-hidden /> : <User size={13} aria-hidden />}
      <span className={cx("min-w-0 flex-1 truncate text-left", s.unread > 0 && "font-semibold text-ink")}>{s.name}</span>
      {s.muted && <BellOff size={12} aria-hidden className="text-mute" />}
      {s.unread > 0 && <span className="hz-pill hz-pill-syncing">{s.unread}</span>}
    </button>
  );
}

function NewSpace({ kind, onClose, onCreated }: { kind: null | "channel" | "dm"; onClose: () => void; onCreated: (s: Space) => void }) {
  const [name, setName] = useState("");
  const [people, setPeople] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!kind) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const emails = people.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      onCreated(await post<Space>("/api/chat/spaces", { kind, name, people: emails, open }));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={kind === "dm" ? t.spaces.newDm : t.spaces.newChannel}>
      <form className="space-y-4" onSubmit={submit}>
        {kind === "channel" && <Field label={t.spaces.channelName} value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={80} />}
        <Field label={kind === "dm" ? t.spaces.person : t.spaces.peopleLabel} value={people} onChange={(e) => setPeople(e.target.value)}
          placeholder="name@example.com" autoFocus={kind === "dm"} />
        {kind === "channel" && (
          <label className="flex items-center gap-2 text-[13px] text-body">
            <input type="checkbox" checked={open} onChange={(e) => setOpen(e.target.checked)} /> {t.spaces.openLabel}
          </label>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t.cancel}</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={kind === "dm" ? !people.trim() : !name.trim()}>{t.spaces.create}</Button>
        </div>
      </form>
    </Modal>
  );
}

function Channel({ space, onChanged }: { space: Space; onChanged: () => void }) {
  const [data, setData] = useState<{ items: Post[]; pinned: Post[] } | null>(null);
  const [thread, setThread] = useState<Post | null>(null);
  const load = useCallback(() => get<{ items: Post[]; pinned: Post[] }>(`/api/chat/spaces/${space.id}/posts`).then(setData).catch(() => {}), [space.id]);
  useEffect(() => {
    void load();
    const es = new EventSource(`/api/chat/spaces/${encodeURIComponent(space.id)}/events`);
    es.onmessage = (e) => {
      if (String(e.data).includes("access.revoked")) { es.close(); navigate("/spaces"); return; }
      void load();
    };
    return () => es.close();
  }, [load, space.id]);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [data?.items.length]);
  const others = space.members.filter((m) => m.role !== "owner" || space.kind === "dm").map((m) => m.name);
  return (
    <div className="hz-channel">
      <div className="hz-channel-head">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold text-ink">{space.kind === "channel" ? `# ${space.name}` : space.name}</div>
          <div className="truncate text-[11.5px] text-mute">{space.open ? t.spaces.openTo : t.spaces.membersOf(space.members.length, others.slice(0, 4).join(", "))}</div>
        </div>
        <IconButton label={space.muted ? t.spaces.unmute : t.spaces.mute} onClick={async () => {
          await post(`/api/chat/spaces/${space.id}/mute`, { muted: !space.muted });
          onChanged();
        }}>
          {space.muted ? <BellOff size={15} aria-hidden /> : <Bell size={15} aria-hidden />}
        </IconButton>
      </div>
      {data && data.pinned.length > 0 && (
        <div className="hz-pinned">
          <Pin size={12} aria-hidden /> {data.pinned.map((p) => p.body.slice(0, 80)).join(" · ")}
        </div>
      )}
      <div className="hz-posts">
        {data?.items.length === 0 && <div className="p-4 text-[13px] text-mute">{t.spaces.empty}</div>}
        {data?.items.map((p) => <PostRow key={p.id} p={p} onChanged={load} onThread={() => setThread(p)} />)}
        <div ref={end} />
      </div>
      <Composer spaceId={space.id} onPosted={load} />
      {thread && <Thread space={space} root={thread} onClose={() => setThread(null)} onChanged={load} />}
    </div>
  );
}

function PostRow({ p, onChanged, onThread, inThread }: { p: Post; onChanged: () => void; onThread?: () => void; inThread?: boolean }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(p.body);
  if (p.state === "deleted")
    return <div className="hz-post"><div className="text-[12.5px] italic text-mute">{t.spaces.deleted}</div></div>;
  const act = (fn: () => Promise<unknown>) => fn().then(onChanged).catch((e) => toast(describeError(e), "error"));
  return (
    <div className={cx("hz-post group", p.author.kind === "agent" && "hz-post-agent")}>
      <div className="flex items-baseline gap-2">
        <span className="text-[13px] font-semibold text-ink">{p.author.name}</span>
        <span className="text-[11px] text-mute">{when(p.created_at)}{p.edited ? ` · ${t.spaces.edited}` : ""}{p.pinned ? ` · ${t.spaces.pinned}` : ""}</span>
      </div>
      {editing ? (
        <form className="mt-1 flex gap-2" onSubmit={(e) => { e.preventDefault(); act(() => patch(`/api/chat/posts/${p.id}`, { body: text, expected_revision: p.revision })).then(() => setEditing(false)); }}>
          <input className="hz-input flex-1" value={text} onChange={(e) => setText(e.target.value)} autoFocus aria-label={t.spaces.editPost} />
          <Button size="sm" type="submit" variant="primary">{t.save}</Button>
          <Button size="sm" onClick={() => setEditing(false)}>{t.cancel}</Button>
        </form>
      ) : p.state === "pending" ? (
        <div className="mt-0.5 text-[13px] text-mute">{t.spaces.agentThinking}</div>
      ) : (
        <div className="hz-md hz-post-body"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{p.body}</ReactMarkdown></div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-1">
        {p.reactions.map((r) => (
          <button key={r.emoji} type="button" className={cx("hz-reaction", r.mine && "hz-reaction-mine")}
            onClick={() => act(() => post(`/api/chat/posts/${p.id}/reactions`, { emoji: r.emoji, on: !r.mine }))}
            aria-label={t.spaces.reactionLabel(r.emoji, r.count)}>{r.emoji} {r.count}</button>
        ))}
        {!inThread && p.replies > 0 && (
          <button type="button" className="hz-reaction" onClick={onThread}>{t.spaces.replies(p.replies)}</button>
        )}
        <span className="hz-post-tools">
          {EMOJI.slice(0, 3).map((e) => (
            <button key={e} type="button" className="hz-icon-link !h-6 !w-6 text-[13px]" aria-label={t.spaces.react(e)}
              onClick={() => act(() => post(`/api/chat/posts/${p.id}/reactions`, { emoji: e, on: true }))}>{e}</button>
          ))}
          {!inThread && <button type="button" className="hz-icon-link !h-6 !w-6" aria-label={t.spaces.reply} onClick={onThread}><Reply size={13} aria-hidden /></button>}
          <button type="button" className="hz-icon-link !h-6 !w-6" aria-label={p.pinned ? t.spaces.unpin : t.spaces.pin}
            onClick={() => act(() => post(`/api/chat/posts/${p.id}/pin`, { on: !p.pinned }))}><Pin size={13} aria-hidden /></button>
          {p.mine && <button type="button" className="hz-icon-link !h-6 !w-6" aria-label={t.spaces.editPost} onClick={() => { setText(p.body); setEditing(true); }}><Pencil size={13} aria-hidden /></button>}
          {p.mine && <button type="button" className="hz-icon-link !h-6 !w-6" aria-label={t.spaces.deletePost} onClick={() => act(() => del(`/api/chat/posts/${p.id}`))}><Trash2 size={13} aria-hidden /></button>}
        </span>
      </div>
    </div>
  );
}

function Composer({ spaceId, parentId, onPosted }: { spaceId: string; parentId?: string; onPosted: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    try {
      await post(`/api/chat/spaces/${spaceId}/posts`, { body: text, ...(parentId ? { parent_id: parentId } : {}) });
      setText("");
      onPosted();
    } catch (err) {
      toast(describeError(err), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="hz-space-composer" onSubmit={send}>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={1} placeholder={parentId ? t.spaces.replyPlaceholder : t.spaces.placeholder}
        aria-label={parentId ? t.spaces.replyPlaceholder : t.spaces.placeholder}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(e as unknown as FormEvent); } }} />
      <IconButton label={t.spaces.send} type="submit" disabled={busy || !text.trim()}><Send size={15} aria-hidden /></IconButton>
    </form>
  );
}

function Thread({ space, root, onClose, onChanged }: { space: Space; root: Post; onClose: () => void; onChanged: () => void }) {
  const [items, setItems] = useState<Post[]>([]);
  const load = useCallback(() => get<{ items: Post[] }>(`/api/chat/spaces/${space.id}/posts?parent_id=${root.id}`).then((o) => setItems(o.items)).catch(() => {}), [space.id, root.id]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [load]);
  return (
    <aside className="hz-thread" aria-label={t.spaces.thread}>
      <div className="hz-channel-head">
        <span className="flex-1 text-[13px] font-semibold text-ink">{t.spaces.thread}</span>
        <IconButton label={t.close} onClick={onClose}><X size={15} aria-hidden /></IconButton>
      </div>
      <div className="hz-posts">
        <PostRow p={root} onChanged={() => { void load(); onChanged(); }} inThread />
        <div className="hz-thread-sep">{t.spaces.replies(items.length)}</div>
        {items.map((p) => <PostRow key={p.id} p={p} onChanged={() => { void load(); onChanged(); }} inThread />)}
      </div>
      <Composer spaceId={space.id} parentId={root.id} onPosted={() => { void load(); onChanged(); }} />
    </aside>
  );
}

// -- notes ---------------------------------------------------------------------------------------
type Note = { id: string; title: string; body: string; revision: number; updated_at: number };

function Notes() {
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [open, setOpen] = useState<Note | null>(null);
  const load = useCallback(() => get<{ items: Note[] }>("/api/chat/notes").then((o) => setNotes(o.items)).catch(() => setNotes([])), []);
  useEffect(() => void load(), [load]);
  return (
    <div className="grid gap-4 md:grid-cols-[220px_1fr]">
      <div>
        <Button size="sm" variant="secondary" icon={<Plus size={13} aria-hidden />} className="mb-2 w-full" onClick={async () => {
          const n = await post<Note>("/api/chat/notes", { title: t.spaces.untitledNote, body: "" });
          await load();
          setOpen(n);
        }}>{t.spaces.newNote}</Button>
        {notes?.map((n) => (
          <button key={n.id} type="button" onClick={() => setOpen(n)} className={cx("hz-file-row", open?.id === n.id && "hz-file-row-on")}>
            <NotebookPen size={13} aria-hidden /><span className="min-w-0 flex-1 truncate text-left">{n.title}</span>
          </button>
        ))}
        {notes?.length === 0 && <div className="px-2 text-[12px] text-mute">{t.spaces.noNotes}</div>}
      </div>
      <div className="min-w-0">
        {open ? <NoteEditor key={open.id} note={open} onSaved={(n) => { setOpen(n); void load(); }} />
          : <StateMessage icon={<NotebookPen size={20} aria-hidden />} title={t.spaces.notesTitle}>{t.spaces.notesBody}</StateMessage>}
      </div>
    </div>
  );
}

function NoteEditor({ note, onSaved }: { note: Note; onSaved: (n: Note) => void }) {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [base, setBase] = useState(note.revision);
  const [conflict, setConflict] = useState<Note | null>(null);
  const [revs, setRevs] = useState<{ revision: number; created_at: number }[]>([]);
  const loadRevs = useCallback(() => get<{ items: { revision: number; created_at: number }[] }>(`/api/chat/notes/${note.id}/revisions`).then((o) => setRevs(o.items)).catch(() => {}), [note.id]);
  useEffect(() => void loadRevs(), [loadRevs]);
  const save = async () => {
    try {
      const n = await put<Note>(`/api/chat/notes/${note.id}`, { title, body, expected_revision: base });
      setBase(n.revision);
      onSaved(n);
      void loadRevs();
      toast(t.spaces.noteSaved);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const cur = (e as ApiError & { detail?: { current?: Note } }).detail?.current;
        setConflict(cur ?? (await get<{ items: Note[] }>("/api/chat/notes")).items.find((x) => x.id === note.id) ?? null);
      } else toast(describeError(e), "error");
    }
  };
  return (
    <div className="hz-card space-y-2">
      <input className="hz-input text-[15px] font-semibold" value={title} onChange={(e) => setTitle(e.target.value)} aria-label={t.spaces.noteTitle} />
      <textarea className="hz-artifact-editor min-h-[300px] w-full !font-sans !text-[13.5px]" value={body} onChange={(e) => setBody(e.target.value)} aria-label={t.spaces.noteBody} />
      <div className="flex items-center gap-2">
        <span className="flex-1 text-[11.5px] text-mute">{t.spaces.revisionOf(base)}</span>
        <Button size="sm" variant="primary" onClick={() => void save()} disabled={title === note.title && body === note.body}>{t.save}</Button>
      </div>
      {conflict && (
        <Notice tone="warning">
          <div>{t.spaces.conflict}</div>
          <div className="mt-2 flex gap-2">
            <Button size="sm" onClick={async () => {
              await post("/api/chat/notes", { title: `${title} (${t.spaces.copy})`, body });
              setConflict(null);
              setTitle(conflict.title); setBody(conflict.body); setBase(conflict.revision);
              onSaved(conflict);
              toast(t.spaces.keptBoth);
            }}>{t.spaces.keepBoth}</Button>
            <Button size="sm" onClick={() => { setTitle(conflict.title); setBody(conflict.body); setBase(conflict.revision); setConflict(null); }}>{t.spaces.loadTheirs}</Button>
          </div>
        </Notice>
      )}
      {revs.length > 1 && (
        <div className="pt-1">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-mute">{t.spaces.revisions}</div>
          {revs.map((r) => (
            <div key={r.revision} className="hz-source-row">
              <span className="text-[12px] text-ink">{t.spaces.revisionOf(r.revision)}</span>
              <span className="flex-1 text-[11.5px] text-mute">{new Date(r.created_at * 1000).toLocaleString()}</span>
              {r.revision !== base && (
                <button type="button" className="hz-artifact-edit" onClick={async () => {
                  try {
                    const n = await post<Note>(`/api/chat/notes/${note.id}/restore`, { revision: r.revision, expected_revision: base });
                    setTitle(n.title); setBody(n.body); setBase(n.revision); onSaved(n); void loadRevs();
                  } catch (e) { toast(describeError(e), "error"); }
                }}>{t.spaces.restore}</button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// -- calendar ------------------------------------------------------------------------------------
type Occurrence = { id: string; title: string; occurrence_at: number; occurrence_end: number; repeat: string;
  reminder_minutes: number | null; timezone: string; mine: boolean; attendees: string[] };

function startOfWeek(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

function Calendar() {
  const [week, setWeek] = useState(() => startOfWeek(new Date()));
  const [items, setItems] = useState<Occurrence[]>([]);
  const [creating, setCreating] = useState(false);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => new Date(week.getTime() + i * 86400000)), [week]);
  const load = useCallback(() => {
    const s = week.getTime() / 1000;
    get<{ items: Occurrence[] }>(`/api/chat/calendar?start=${s}&end=${s + 7 * 86400}`).then((o) => setItems(o.items)).catch(() => setItems([]));
  }, [week]);
  useEffect(() => void load(), [load]);
  const app = useApp();
  void app;
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <IconButton label={t.spaces.prevWeek} onClick={() => setWeek(new Date(week.getTime() - 7 * 86400000))}><ChevronLeft size={16} aria-hidden /></IconButton>
        <IconButton label={t.spaces.nextWeek} onClick={() => setWeek(new Date(week.getTime() + 7 * 86400000))}><ChevronRight size={16} aria-hidden /></IconButton>
        <span className="text-[13.5px] font-semibold text-ink">{days[0].toLocaleDateString(undefined, { day: "numeric", month: "short" })} – {days[6].toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</span>
        <span className="flex-1" />
        <Button size="sm" onClick={() => setWeek(startOfWeek(new Date()))}>{t.spaces.today}</Button>
        <Button size="sm" variant="primary" icon={<Plus size={13} aria-hidden />} onClick={() => setCreating(true)}>{t.spaces.newEvent}</Button>
      </div>
      <div className="hz-week">
        {days.map((d) => {
          const dayItems = items.filter((o) => new Date(o.occurrence_at * 1000).toDateString() === d.toDateString());
          const today = d.toDateString() === new Date().toDateString();
          return (
            <div key={d.toISOString()} className={cx("hz-day", today && "hz-day-today")}>
              <div className="hz-day-head">{d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })}</div>
              {dayItems.map((o) => (
                <div key={o.id + o.occurrence_at} className="hz-event" title={o.title}>
                  <div className="text-[11px] text-mute">{new Date(o.occurrence_at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</div>
                  <div className="truncate text-[12.5px] font-medium text-ink">{o.title}</div>
                  <div className="text-[10.5px] text-mute">{[o.repeat !== "none" ? t.spaces.repeats[o.repeat] : null, o.reminder_minutes != null ? t.spaces.reminderShort(o.reminder_minutes) : null].filter(Boolean).join(" · ")}</div>
                  {o.mine && <button type="button" className="hz-event-x" aria-label={t.spaces.deleteEvent(o.title)} onClick={async () => { await del(`/api/chat/calendar/${o.id}`); load(); }}><X size={11} aria-hidden /></button>}
                </div>
              ))}
            </div>
          );
        })}
      </div>
      <NewEvent open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); load(); }} />
    </div>
  );
}

function NewEvent({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [title, setTitle] = useState("");
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [time, setTime] = useState("09:00");
  const [minutes, setMinutes] = useState(60);
  const [repeat, setRepeat] = useState("none");
  const [reminder, setReminder] = useState("10");
  const [attendees, setAttendees] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!open) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const start = new Date(`${date}T${time}`).getTime() / 1000;
    try {
      await post("/api/chat/calendar", { title, starts_at: start, ends_at: start + minutes * 60, timezone: zone, repeat,
        reminder_minutes: reminder === "" ? null : Number(reminder),
        attendees: attendees.split(/[\s,]+/).filter(Boolean) });
      onCreated();
    } catch (err) {
      setError(describeError(err));
    }
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={t.spaces.newEvent}>
      <form className="space-y-3" onSubmit={submit}>
        <Field label={t.spaces.eventTitle} value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={200} />
        <div className="grid grid-cols-3 gap-2">
          <label className="block"><span className="hz-label">{t.spaces.date}</span><input className="hz-input" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
          <label className="block"><span className="hz-label">{t.spaces.time}</span><input className="hz-input" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></label>
          <label className="block"><span className="hz-label">{t.spaces.minutes}</span><input className="hz-input" type="number" min={5} max={1440} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} /></label>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label className="block"><span className="hz-label">{t.spaces.repeat}</span>
            <select className="hz-input" value={repeat} onChange={(e) => setRepeat(e.target.value)}>
              <option value="none">{t.spaces.repeats.none}</option><option value="daily">{t.spaces.repeats.daily}</option><option value="weekly">{t.spaces.repeats.weekly}</option>
            </select></label>
          <label className="block"><span className="hz-label">{t.spaces.reminder}</span>
            <select className="hz-input" value={reminder} onChange={(e) => setReminder(e.target.value)}>
              <option value="">{t.spaces.noReminder}</option><option value="0">{t.spaces.atStart}</option><option value="5">{t.spaces.reminderShort(5)}</option>
              <option value="10">{t.spaces.reminderShort(10)}</option><option value="30">{t.spaces.reminderShort(30)}</option><option value="60">{t.spaces.reminderShort(60)}</option>
            </select></label>
        </div>
        <Field label={t.spaces.attendees} value={attendees} onChange={(e) => setAttendees(e.target.value)} placeholder="name@example.com" />
        <p className="m-0 text-[11.5px] text-mute">{t.spaces.zoneNote(zone)}</p>
        {error && <Notice tone="error">{error}</Notice>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t.cancel}</Button>
          <Button type="submit" variant="primary" disabled={!title.trim()}>{t.spaces.create}</Button>
        </div>
      </form>
    </Modal>
  );
}
