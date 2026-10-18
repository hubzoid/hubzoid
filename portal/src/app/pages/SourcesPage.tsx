// Sources: collections of your own material the agent can search and cite
// (files, notes, public web pages), who can read each one, syncing, and a
// place to try a question and see the passages it would cite.
import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, FileText, Globe, Link2, Plus, RefreshCw, Search, StickyNote, Trash2, Upload, X } from "lucide-react";
import { ApiError, del, get, post, put } from "../lib/api";
import { describeError } from "../lib/errors";
import { formatBytes } from "../lib/format";
import { uploadFile } from "../lib/files";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { Button, ConfirmDialog, Field, IconButton, Modal, Notice, StateMessage } from "../components/ui";
import { PageFrame } from "./PageFrame";

type SourceSpec = { id: string; kind: "upload" | "file" | "directory" | "text" | "url"; file_id?: string; path?: string;
  text?: string; title?: string; url?: string };
type Manifest = { version: 1; id: string; title: string; description: string; readers: string[]; writers: string[];
  agents: string[]; sources: SourceSpec[] };
type SourceState = { source_id: string; key: string; kind: string; title: string; state: string; error: string | null };
type Job = { job_id: string; state: string; counters: { sources?: number; passages?: number; failed?: number };
  error: string | null };
type Collection = { id: string; title: string; revision: number; manifest: Manifest; owned: boolean; can_write: boolean;
  ready: boolean; sources: SourceState[]; last_job: Job | null };
type Result = { citation_id: string; title: string; excerpt: string; collection_id: string;
  locator: { page: number | null } };
type MyFile = { file_id: string; name: string; size_bytes: number; state: string };

const R = "/api/chat/retrieval";

function slug(title: string): string {
  const s = title.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
  return s || `collection-${Math.random().toString(36).slice(2, 7)}`;
}

function nextId(m: Manifest, base: string): string {
  const root = slug(base).slice(0, 40) || "source";
  let id = root;
  for (let n = 2; m.sources.some((s) => s.id === id); n++) id = `${root}-${n}`;
  return id;
}

const KIND_ICON = { upload: FileText, file: FileText, directory: BookOpen, text: StickyNote, url: Globe } as const;

export default function SourcesPage() {
  const [items, setItems] = useState<Collection[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const load = useCallback(async () => {
    try {
      setItems((await get<{ items: Collection[] }>(`${R}/collections`)).items);
      setError(null);
    } catch (e) {
      setError(describeError(e));
      setItems([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <PageFrame
      title={t.sources.title}
      description={t.sources.description}
      actions={<Button variant="primary" size="sm" icon={<Plus size={14} aria-hidden />} onClick={() => setCreating(true)}>
        {t.sources.newCollection}</Button>}
    >
      {error && <Notice tone="error" className="mb-4">{error}</Notice>}
      {items === null ? (
        <div className="space-y-3">
          <div className="hz-skeleton h-24 w-full" />
          <div className="hz-skeleton h-24 w-full" />
        </div>
      ) : items.length === 0 ? (
        <StateMessage icon={<BookOpen size={20} aria-hidden />} title={t.sources.emptyTitle}>{t.sources.emptyBody}</StateMessage>
      ) : (
        <div className="space-y-4">
          {items.map((c) => <CollectionCard key={c.id} c={c} onChanged={load} />)}
        </div>
      )}
      <TryQuestion hasReady={!!items?.some((c) => c.ready)} />
      <NewCollection open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); void load(); }} />
    </PageFrame>
  );
}

function NewCollection({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await put(`${R}/collections/${encodeURIComponent(slug(title))}`, {
        expected_revision: "absent", manifest: { title: title.trim(), sources: [] } });
      setTitle("");
      onCreated();
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? t.sources.exists : describeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onOpenChange={(o) => !o && !busy && onClose()} title={t.sources.newCollection}>
      <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Field label={t.sources.collectionTitle} value={title} onChange={(e) => setTitle(e.target.value)} autoFocus
          maxLength={120} placeholder={t.sources.titlePlaceholder} />
        {error && <Notice tone="error">{error}</Notice>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t.cancel}</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!title.trim()}>{t.sources.create}</Button>
        </div>
      </form>
    </Modal>
  );
}

function CollectionCard({ c, onChanged }: { c: Collection; onChanged: () => Promise<void> | void }) {
  const [adding, setAdding] = useState<null | "file" | "text" | "url">(null);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<Job | null>(c.last_job);
  const fileInput = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => setJob(c.last_job), [c.last_job]);

  const apply = async (m: Manifest) => {
    await put(`${R}/collections/${encodeURIComponent(c.id)}`, { expected_revision: c.revision, manifest: m });
    await onChanged();
  };
  const addSource = async (spec: SourceSpec) => {
    setBusy(true);
    try {
      await apply({ ...c.manifest, sources: [...c.manifest.sources, spec] });
      setAdding(null);
    } catch (e) {
      toast(describeError(e), "error");
    } finally {
      setBusy(false);
    }
  };
  const remove = async (key: string) => {
    const root = key.split("/")[0];
    try {
      await apply({ ...c.manifest, sources: c.manifest.sources.filter((s) => s.id !== root) });
      toast(t.sources.removed);
    } catch (e) {
      toast(describeError(e), "error");
    }
  };
  const sync = async () => {
    setBusy(true);
    try {
      let j = await post<Job>(`${R}/collections/${encodeURIComponent(c.id)}/jobs`, { operation: "sync" });
      setJob(j);
      for (let i = 0; i < 240 && !["ready", "failed", "cancelled"].includes(j.state); i++) {
        await new Promise((r) => setTimeout(r, 750));
        j = await get<Job>(`${R}/jobs/${encodeURIComponent(j.job_id)}`);
        setJob(j);
      }
      await onChanged();
      toast(j.state === "ready" ? t.sources.synced(j.counters.passages ?? 0) : t.sources.syncFailed, j.state === "ready" ? "success" : "error");
    } catch (e) {
      toast(describeError(e), "error");
    } finally {
      setBusy(false);
    }
  };
  const setReaders = async (value: string) => {
    const readers = value === "everyone" ? ["*"] : value === "me" ? [] : c.manifest.readers.filter((r) => r !== "*");
    try {
      await apply({ ...c.manifest, readers });
    } catch (e) {
      toast(describeError(e), "error");
    }
  };
  const upload = async (file: File) => {
    setBusy(true);
    setProgress(0);
    try {
      const desc = await uploadFile(file, { onProgress: setProgress });
      if (desc.state !== "ready") throw new Error(desc.processing_error || t.sources.uploadNotReady);
      await addSource({ id: nextId(c.manifest, file.name), kind: "upload", file_id: desc.file_id, title: file.name });
    } catch (e) {
      toast(describeError(e), "error");
    } finally {
      setProgress(null);
      setBusy(false);
    }
  };

  const state = job && ["queued", "extracting", "indexing"].includes(job.state) ? "syncing"
    : !c.ready ? "never" : job?.state === "failed" ? "failed" : "ready";
  const everyone = c.manifest.readers.includes("*");
  return (
    <section className="hz-card" aria-labelledby={`col-${c.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={`col-${c.id}`} className="m-0 min-w-0 flex-1 truncate text-[15px] font-semibold text-ink">{c.title}</h2>
        <span className={`hz-pill hz-pill-${state}`}>{t.sources.state[state]}</span>
        {c.can_write && (
          <Button size="sm" icon={<RefreshCw size={13} aria-hidden />} loading={busy && state === "syncing"} onClick={() => void sync()}
            disabled={busy || !c.manifest.sources.length}>
            {t.sources.sync}
          </Button>
        )}
        {c.owned && (
          <IconButton label={t.sources.deleteCollection(c.title)} onClick={() => setDeleting(true)}>
            <Trash2 size={15} aria-hidden />
          </IconButton>
        )}
      </div>
      {job && (job.state === "ready" || job.state === "failed") && (
        <p className="m-0 mt-1 text-[12px] text-mute">
          {job.state === "ready" ? t.sources.lastSync(job.counters.sources ?? 0, job.counters.passages ?? 0, job.counters.failed ?? 0)
            : t.sources.lastSyncFailed(job.error ?? "")}
        </p>
      )}
      <ul className="m-0 mt-3 list-none space-y-1 p-0">
        {c.sources.length === 0 && c.manifest.sources.length === 0 && (
          <li className="text-[13px] text-mute">{t.sources.noSources}</li>
        )}
        {c.manifest.sources.map((s) => {
          const Icon = KIND_ICON[s.kind] ?? FileText;
          const indexed = c.sources.filter((x) => x.key === s.id || x.key.startsWith(`${s.id}/`));
          const failed = indexed.find((x) => x.state === "failed");
          return (
            <li key={s.id} className="hz-source-row">
              <Icon size={14} aria-hidden className="flex-none text-mute" />
              <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{s.title || s.url || s.path || s.id}</span>
              <span className="text-[11px] text-mute">
                {failed ? t.sources.failedSource(failed.error ?? "") : indexed.length ? t.sources.indexed : t.sources.notIndexed}
              </span>
              {c.can_write && (
                <IconButton label={t.sources.removeSource(s.title || s.id)} onClick={() => void remove(s.id)}>
                  <X size={14} aria-hidden />
                </IconButton>
              )}
            </li>
          );
        })}
      </ul>
      {c.can_write && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <input ref={fileInput} type="file" hidden onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void upload(f); }} />
          <Button size="sm" variant="secondary" icon={<Upload size={13} aria-hidden />} onClick={() => fileInput.current?.click()} disabled={busy}>
            {progress !== null ? t.sources.uploading(Math.round(progress * 100)) : t.sources.addUpload}
          </Button>
          <Button size="sm" variant="secondary" icon={<FileText size={13} aria-hidden />} onClick={() => setAdding("file")} disabled={busy}>{t.sources.addExisting}</Button>
          <Button size="sm" variant="secondary" icon={<StickyNote size={13} aria-hidden />} onClick={() => setAdding("text")} disabled={busy}>{t.sources.addNote}</Button>
          <Button size="sm" variant="secondary" icon={<Link2 size={13} aria-hidden />} onClick={() => setAdding("url")} disabled={busy}>{t.sources.addPage}</Button>
          <span className="flex-1" />
          {c.owned && (
            <label className="flex items-center gap-1.5 text-[12px] text-mute">
              {t.sources.readers}
              <select className="hz-select-sm" value={everyone ? "everyone" : c.manifest.readers.length ? "people" : "me"}
                onChange={(e) => void setReaders(e.target.value)}>
                <option value="me">{t.sources.onlyMe}</option>
                <option value="everyone">{t.sources.everyone}</option>
                {c.manifest.readers.length > 0 && !everyone && <option value="people">{t.sources.people(c.manifest.readers.length)}</option>}
              </select>
            </label>
          )}
        </div>
      )}
      <AddSource kind={adding} manifest={c.manifest} onClose={() => setAdding(null)} onAdd={addSource} busy={busy} />
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={t.sources.deleteCollection(c.title)}
        body={t.sources.confirmDelete(c.title)}
        confirmLabel={t.sources.deleteConfirm}
        onConfirm={async () => {
          try { await del(`${R}/collections/${encodeURIComponent(c.id)}`); setDeleting(false); await onChanged(); }
          catch (e) { toast(describeError(e), "error"); }
        }}
      />
    </section>
  );
}

function AddSource({ kind, manifest, onClose, onAdd, busy }: {
  kind: null | "file" | "text" | "url"; manifest: Manifest; onClose: () => void; onAdd: (s: SourceSpec) => void; busy: boolean;
}) {
  const [title, setTitle] = useState("");
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<MyFile[] | null>(null);
  useEffect(() => {
    setTitle("");
    setValue("");
    if (kind === "file") get<{ items: MyFile[] }>("/api/chat/files?limit=50").then((o) => setFiles(o.items.filter((f) => f.state === "ready"))).catch(() => setFiles([]));
  }, [kind]);
  if (!kind) return null;
  const heading = kind === "file" ? t.sources.addExisting : kind === "text" ? t.sources.addNote : t.sources.addPage;
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={heading}>
      {kind === "file" ? (
        <div className="max-h-80 space-y-1 overflow-auto">
          {files === null && <div className="text-[13px] text-mute">{t.loading}</div>}
          {files?.length === 0 && <div className="text-[13px] text-mute">{t.chat.reuseEmpty}</div>}
          {files?.map((f) => (
            <button key={f.file_id} type="button" className="hz-menu-item w-full" disabled={busy}
              onClick={() => onAdd({ id: nextId(manifest, f.name), kind: "upload", file_id: f.file_id, title: f.name })}>
              <FileText size={14} aria-hidden />
              <span className="min-w-0 flex-1 truncate text-left">{f.name}</span>
              <span className="text-[11px] text-mute">{formatBytes(f.size_bytes)}</span>
            </button>
          ))}
        </div>
      ) : (
        <form className="space-y-4" onSubmit={(e) => {
          e.preventDefault();
          if (kind === "text") onAdd({ id: nextId(manifest, title || "note"), kind: "text", title: title.trim() || t.sources.note, text: value });
          else onAdd({ id: nextId(manifest, new URL(value).hostname), kind: "url", url: value.trim(), title: title.trim() || undefined });
        }}>
          <Field label={t.sources.sourceTitle} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
          {kind === "text" ? (
            <label className="block">
              <span className="hz-label">{t.sources.noteText}</span>
              <textarea className="hz-textarea" rows={8} value={value} onChange={(e) => setValue(e.target.value)} required />
            </label>
          ) : (
            <Field label={t.sources.pageUrl} value={value} onChange={(e) => setValue(e.target.value)} placeholder="https://"
              inputMode="url" required />
          )}
          {kind === "url" && <p className="m-0 text-[12px] text-mute">{t.sources.pageHint}</p>}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>{t.cancel}</Button>
            <Button type="submit" variant="primary" loading={busy}
              disabled={!value.trim() || (kind === "url" && !/^https:\/\/[^\s]+$/.test(value.trim()))}>{t.sources.add}</Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function TryQuestion({ hasReady }: { hasReady: boolean }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Result[] | null>(null);
  const [busy, setBusy] = useState(false);
  if (!hasReady) return null;
  return (
    <section className="hz-card mt-6" aria-labelledby="try-q">
      <h2 id="try-q" className="m-0 text-[14px] font-semibold text-ink">{t.sources.tryTitle}</h2>
      <p className="m-0 mt-1 text-[12px] text-mute">{t.sources.tryBody}</p>
      <form className="mt-3 flex gap-2" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try { setResults((await post<{ results: Result[] }>(`${R}/queries`, { query: q })).results); }
        catch (err) { toast(describeError(err), "error"); }
        finally { setBusy(false); }
      }}>
        <input className="hz-input flex-1" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.sources.tryPlaceholder}
          aria-label={t.sources.tryTitle} maxLength={4000} />
        <Button type="submit" size="sm" icon={<Search size={13} aria-hidden />} loading={busy} disabled={!q.trim()}>{t.sources.search}</Button>
      </form>
      {results && (
        <ol className="m-0 mt-3 list-none space-y-2 p-0">
          {results.length === 0 && <li className="text-[13px] text-mute">{t.sources.noMatches}</li>}
          {results.map((r, i) => (
            <li key={r.citation_id} className="hz-passage">
              <div className="text-[12px] font-semibold text-ink">{i + 1}. {r.title}{r.locator.page ? ` · ${t.sources.page(r.locator.page)}` : ""}</div>
              <div className="mt-1 line-clamp-4 whitespace-pre-wrap text-[12.5px] leading-relaxed text-body">{r.excerpt}</div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
