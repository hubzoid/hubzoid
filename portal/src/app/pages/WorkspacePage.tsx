// Workspace: edit the hub's own agent instructions, skills and shared prompts
// as reviewed drafts (validate, diff, approve the exact digest, publish), roll
// back to an earlier version, and check which OpenAPI operations would become
// tools. Claude Code and Codex edit the same files; this is the reviewed path.
import { useCallback, useEffect, useMemo, useState } from "react";
import { FileCode2, FilePlus2, History, ListChecks, Plus } from "lucide-react";
import { ApiError, get, patch, post } from "../lib/api";
import { describeError } from "../lib/errors";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { Button, Field, Modal, Notice, cx } from "../components/ui";
import { agentById, useApp } from "../lib/app-context";
import { PageFrame } from "./PageFrame";

const A = "/api/chat/authoring";
type FileItem = { path: string; kind: "agent" | "skill" | "prompt"; sha256: string; bytes: number };
type Draft = { id: string; path: string; kind: string; state: string; revision: number; digest: string; stale: boolean;
  content: string; diff: string; problems: { code: string; message: string; field?: string }[]; note: string | null };
type Version = { version_id: string; sha256: string; published_by: string; published_at: number; current: boolean };

/** `agent`: open that agent's instructions when this hub holds them. */
export default function WorkspacePage({ agent = null }: { agent?: string | null }) {
  const [tab, setTab] = useState<"files" | "openapi">("files");
  return (
    <PageFrame title={t.settingsPage.instructions} description={t.settingsPage.instructionsLede} wide>
      <nav className="hz-agent-tabs" aria-label={t.settingsPage.instructions}>
        {(["files", "openapi"] as const).map((k) => (
          <button key={k} type="button" onClick={() => setTab(k)} aria-current={tab === k ? "page" : undefined}
            className={cx("-mb-px border-b-2 px-3 pb-2 pt-1 text-[13px] font-medium",
              tab === k ? "border-accent text-ink" : "border-transparent text-mute hover:text-ink")}>
            {k === "files" ? t.workspace.files : t.workspace.openapi}
          </button>
        ))}
      </nav>
      {tab === "files" ? <Files agent={agent} /> : <OpenApi />}
    </PageFrame>
  );
}

function Files({ agent }: { agent: string | null }) {
  const app = useApp();
  const [hub, setHub] = useState<string | null>(null);
  const [files, setFiles] = useState<FileItem[] | null>(null);
  const elsewhere = !!agent && !!hub && agent.toLowerCase() !== hub;
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<null | "skill" | "prompt">(null);
  const [readOnly, setReadOnly] = useState(false);
  const load = useCallback(async () => {
    try {
      const got = await get<{ items: FileItem[]; hub?: string; read_only?: boolean }>(`${A}/files`);
      setFiles(got.items);
      setHub(got.hub ?? null);
      setReadOnly(!!got.read_only);
      setDrafts((await get<{ items: Draft[] }>(`${A}/drafts`)).items);
      // From an agent's page: its instructions, when this hub is that agent.
      if (agent && got.hub && agent.toLowerCase() === got.hub) {
        setSelected((s) => s ?? (got.items.some((f) => f.path === "AGENTS.md") ? "AGENTS.md" : null));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? t.workspace.denied : describeError(e));
      setFiles([]);
    }
  }, [agent]);
  useEffect(() => void load(), [load]);
  const groups = useMemo(() => ({
    agent: (files ?? []).filter((f) => f.kind === "agent"),
    skill: (files ?? []).filter((f) => f.kind === "skill"),
    prompt: (files ?? []).filter((f) => f.kind === "prompt"),
  }), [files]);
  if (error) return <Notice tone="error">{error}</Notice>;
  return (
    <>
    {hub && <p className="hz-workspace-context">{t.workspace.hubContext(agentById(app.agents.list, hub)?.name ?? hub)}</p>}
    {elsewhere && <Notice tone="info" className="mb-4">{t.workspace.elsewhere}</Notice>}
    {readOnly && <Notice tone="info" className="mb-4">{t.workspace.readOnly}</Notice>}
    <div className="hz-workspace-files">
      <div className="space-y-4">
        {(["agent", "skill", "prompt"] as const).map((kind) => (
          <div key={kind}>
            <div className="mb-1 flex items-center justify-between">
              <div className="text-[11px] font-semibold uppercase tracking-wide text-mute">{t.workspace.kinds[kind]}</div>
              {kind !== "agent" && !readOnly && (
                <button type="button" className="hz-icon-link" aria-label={t.workspace.newOf(kind)} title={t.workspace.newOf(kind)}
                  onClick={() => setCreating(kind)}><Plus size={14} aria-hidden /></button>
              )}
            </div>
            {groups[kind].length === 0 && <div className="px-2 text-[12px] text-mute">{t.workspace.none}</div>}
            {groups[kind].map((f) => {
              const open = drafts.find((d) => d.path === f.path && d.state !== "published");
              return (
                <button key={f.path} type="button" onClick={() => setSelected(f.path)}
                  className={cx("hz-file-row", selected === f.path && "hz-file-row-on")}>
                  <FileCode2 size={13} aria-hidden className="flex-none" />
                  <span className="min-w-0 flex-1 truncate text-left">{f.path}</span>
                  {open && <span className="hz-pill hz-pill-syncing">{t.workspace.states[open.state] ?? open.state}</span>}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="min-w-0">
        {selected ? (
          <Editor key={selected} path={selected} draft={drafts.find((d) => d.path === selected && d.state !== "published") ?? null}
            readOnly={readOnly} onChanged={load} />
        ) : (
          <div className="flex min-h-48 items-center justify-center px-6 py-10 text-center text-[13px] text-mute">{t.workspace.pick}</div>
        )}
      </div>
      <NewFile kind={creating} onClose={() => setCreating(null)} onCreated={async (path) => { setCreating(null); await load(); setSelected(path); }} />
    </div>
    </>
  );
}

function NewFile({ kind, onClose, onCreated }: { kind: null | "skill" | "prompt"; onClose: () => void; onCreated: (path: string) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  if (!kind) return null;
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const path = kind === "skill" ? `skills/${id}/SKILL.md` : `prompts/${id}.md`;
  const template = kind === "skill"
    ? `---\nname: ${name}\ndescription: When to use this skill.\n---\n\nSteps the agent follows.\n`
    : `---\nname: ${name}\ndescription: What this prompt is for.\nvariables:\n  - name: topic\n---\n\nWrite about {{topic}}.\n`;
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={t.workspace.newOf(kind)}>
      <form className="space-y-4" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await post(`${A}/drafts`, { path, content: template });
          onCreated(path);
        } catch (err) {
          toast(describeError(err), "error");
        } finally {
          setBusy(false);
        }
      }}>
        <Field label={t.workspace.name} value={name} onChange={(e) => setName(e.target.value)} autoFocus maxLength={60} />
        {id && <p className="m-0 text-[12px] text-mute">{path}</p>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t.cancel}</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!id}>{t.workspace.createDraft}</Button>
        </div>
      </form>
    </Modal>
  );
}

function Editor({ path, draft: initial, readOnly = false, onChanged }: {
  path: string; draft: Draft | null; readOnly?: boolean; onChanged: () => Promise<void> | void;
}) {
  const [draft, setDraft] = useState<Draft | null>(initial);
  const [text, setText] = useState(initial?.content ?? "");
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<Version[]>([]);
  const [showDiff, setShowDiff] = useState(false);
  const loadVersions = useCallback(() => {
    get<{ items: Version[] }>(`${A}/files/versions?path=${encodeURIComponent(path)}`).then((o) => setVersions(o.items)).catch(() => {});
  }, [path]);
  useEffect(() => loadVersions(), [loadVersions]);
  const act = async (fn: () => Promise<Draft | unknown>, done?: string) => {
    setBusy(true);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "id" in (out as Draft) && "content" in (out as Draft)) {
        setDraft(out as Draft);
        setText((out as Draft).content);
      }
      if (done) toast(done);
      await onChanged();
      loadVersions();
    } catch (e) {
      toast(describeError(e), "error");
    } finally {
      setBusy(false);
    }
  };
  if (!draft || readOnly)
    return (
      <div className="hz-card p-4">
        <div className="flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink">{path}</span>
          {!readOnly && (
            <Button size="sm" variant="primary" icon={<FilePlus2 size={13} aria-hidden />} loading={busy}
              onClick={() => act(() => post<Draft>(`${A}/drafts`, { path }))}>{t.workspace.startDraft}</Button>
          )}
        </div>
        <Versions versions={versions} onRollback={readOnly ? undefined
          : (v) => act(() => post<Draft>(`${A}/rollbacks`, { path, version_id: v }), t.workspace.rollbackDraft)} />
      </div>
    );
  const dirty = text !== draft.content;
  const editable = draft.state === "draft" || draft.state === "rejected";
  return (
    <div className="hz-card space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink">{path}</span>
        <span className="hz-pill">{t.workspace.states[draft.state] ?? draft.state}</span>
        {draft.stale && <span className="hz-pill hz-pill-failed">{t.workspace.stale}</span>}
      </div>
      {draft.note && <div className="text-[12px] text-mute">{draft.note}</div>}
      <textarea className="hz-artifact-editor min-h-[320px] w-full" value={text} readOnly={!editable}
        onChange={(e) => setText(e.target.value)} aria-label={t.workspace.editorLabel(path)} spellCheck={false} />
      {draft.problems.length > 0 && !dirty && (
        <Notice tone="warning">{draft.problems.map((p) => p.field ? `${p.field}: ${p.message}` : p.message).join(" · ")}</Notice>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {editable && (
          <Button size="sm" disabled={!dirty || busy} onClick={() =>
            act(() => patch<Draft>(`${A}/drafts/${draft.id}`, { content: text, expected_revision: draft.revision }), t.workspace.saved)}>
            {t.workspace.save}
          </Button>
        )}
        <Button size="sm" variant="secondary" icon={<ListChecks size={13} aria-hidden />} onClick={() => setShowDiff(!showDiff)}>
          {showDiff ? t.workspace.hideDiff : t.workspace.showDiff}
        </Button>
        <span className="flex-1" />
        {editable && dirty && <span className="text-[12px] text-mute">{t.workspace.saveFirst}</span>}
        {editable && (
          <Button size="sm" variant="primary" disabled={dirty || busy || draft.problems.length > 0}
            onClick={() => act(() => post<Draft>(`${A}/drafts/${draft.id}/submit`, { expected_revision: draft.revision }), t.workspace.submitted)}>
            {t.workspace.submit}
          </Button>
        )}
        {draft.state === "in_review" && (
          <>
            <Button size="sm" disabled={busy} onClick={() =>
              act(() => post<Draft>(`${A}/drafts/${draft.id}/review`, { decision: "reject", digest: draft.digest }), t.workspace.rejected)}>
              {t.workspace.reject}
            </Button>
            <Button size="sm" variant="primary" disabled={busy} onClick={() =>
              act(() => post<Draft>(`${A}/drafts/${draft.id}/review`, { decision: "approve", digest: draft.digest }), t.workspace.approved)}>
              {t.workspace.approve}
            </Button>
          </>
        )}
        {(draft.state === "approved" || draft.state === "publishing") && (
          <Button size="sm" variant="primary" disabled={busy} onClick={() =>
            act(async () => {
              await post(`${A}/drafts/${draft.id}/publish`, { digest: draft.digest });
              setDraft(null);
              return null;
            }, t.workspace.published)}>
            {t.workspace.publish}
          </Button>
        )}
      </div>
      {showDiff && <Diff text={draft.diff} />}
      <Versions versions={versions} onRollback={(v) => act(() => post<Draft>(`${A}/rollbacks`, { path, version_id: v }), t.workspace.rollbackDraft)} />
    </div>
  );
}

function Diff({ text }: { text: string }) {
  if (!text) return <div className="text-[12px] text-mute">{t.workspace.noChanges}</div>;
  return (
    <pre className="hz-diff" aria-label={t.workspace.showDiff}>
      {text.split("\n").map((line, i) => (
        <span key={i} className={line.startsWith("+") && !line.startsWith("+++") ? "hz-diff-add"
          : line.startsWith("-") && !line.startsWith("---") ? "hz-diff-del" : undefined}>{line + "\n"}</span>
      ))}
    </pre>
  );
}

function Versions({ versions, onRollback }: { versions: Version[]; onRollback?: (id: string) => void }) {
  if (!versions.length) return null;
  return (
    <div className="mt-2">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-mute">
        <History size={12} aria-hidden /> {t.workspace.versions}
      </div>
      <ul className="m-0 list-none space-y-1 p-0">
        {versions.map((v) => (
          <li key={v.version_id} className="hz-source-row">
            <span className="font-mono text-[11.5px] text-ink">{v.sha256.slice(0, 12)}</span>
            <span className="flex-1 text-[11.5px] text-mute">{new Date(v.published_at * 1000).toLocaleString()}</span>
            {v.current ? <span className="hz-pill hz-pill-ready">{t.workspace.current}</span>
              : onRollback && <button type="button" className="hz-artifact-edit" onClick={() => onRollback(v.version_id)}>{t.workspace.rollback}</button>}
          </li>
        ))}
      </ul>
    </div>
  );
}

type Op = { operation_id: string; method: string; path: string; summary: string; writes: boolean };

function OpenApi() {
  const [raw, setRaw] = useState("");
  const [ops, setOps] = useState<Op[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [tools, setTools] = useState<{ name: string; description: string }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async (sel?: string[]) => {
    setError(null);
    try {
      const spec = JSON.parse(raw);
      const out = await post<{ operations: Op[]; tools: typeof tools }>(`${A}/openapi/preview`, { spec, ...(sel ? { selected: sel } : {}) });
      setOps(out.operations);
      setTools(out.tools);
    } catch (e) {
      setError(e instanceof SyntaxError ? t.workspace.badJson : describeError(e));
    }
  };
  return (
    <div className="space-y-3">
      <p className="m-0 text-[12.5px] text-mute">{t.workspace.openapiBody}</p>
      <textarea className="hz-artifact-editor min-h-[200px] w-full" value={raw} onChange={(e) => setRaw(e.target.value)}
        aria-label={t.workspace.openapiLabel} placeholder='{"openapi": "3.0.3", ...}' spellCheck={false} />
      <Button size="sm" variant="primary" disabled={!raw.trim()} onClick={() => void run()}>{t.workspace.validate}</Button>
      {error && <Notice tone="error">{error}</Notice>}
      {ops && (
        <div className="hz-card space-y-2 p-4">
          <div className="text-[12.5px] font-semibold text-ink">{t.workspace.operations(ops.length)}</div>
          {ops.map((o) => (
            <label key={o.operation_id} className="flex items-center gap-2 text-[12.5px]">
              <input type="checkbox" checked={selected.includes(o.operation_id)}
                onChange={(e) => setSelected(e.target.checked ? [...selected, o.operation_id] : selected.filter((s) => s !== o.operation_id))} />
              <span className="font-mono text-[11.5px] text-mute">{o.method}</span>
              <span className="font-mono text-[11.5px] text-ink">{o.path}</span>
              <span className="truncate text-mute">{o.summary}</span>
              {o.writes && <span className="hz-pill hz-pill-failed">{t.workspace.writes}</span>}
            </label>
          ))}
          <Button size="sm" disabled={!selected.length} onClick={() => void run(selected)}>{t.workspace.previewTools}</Button>
          {tools && (
            <ul className="m-0 list-none space-y-1 p-0">
              {tools.map((tool) => <li key={tool.name} className="hz-source-row"><span className="font-mono text-[12px] text-ink">{tool.name}</span>
                <span className="truncate text-[11.5px] text-mute">{tool.description}</span></li>)}
            </ul>
          )}
          {tools && <p className="m-0 text-[11.5px] text-mute">{t.workspace.activationLater}</p>}
        </div>
      )}
    </div>
  );
}
