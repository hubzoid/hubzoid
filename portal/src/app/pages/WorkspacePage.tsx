// An agent's instructions, skills and shared prompts, readable by whoever
// manages it, with each file's Git history. Changing them from the browser
// (proposed, approved, committed) is Beta, off by default: Claude Code, Codex
// or an editor change the same files.
import { useCallback, useEffect, useMemo, useState } from "react";
import { FileCode2, History, ListChecks, Plus } from "lucide-react";
import { ApiError, get, hubUrl, post } from "../lib/api";
import { describeError } from "../lib/errors";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { Button, Field, Modal, Notice, cx } from "../components/ui";
import { agentById, useApp } from "../lib/app-context";
import { PageFrame } from "./PageFrame";

const A = "/api/chat/authoring";
type FileItem = { path: string; kind: "agent" | "skill" | "prompt"; sha256: string; bytes: number };
type Pending = { id: number; title: string; paths: string[] };
type Version = { commit: string; author: string; at: number; subject: string };

/** `agent`: open that agent's instructions when this hub holds them.
 *  `readOnly`: in-app editing is off; `note` says where to change them. */
export default function WorkspacePage({ agent = null, readOnly = false, note = null }: {
  agent?: string | null; readOnly?: boolean; note?: string | null;
}) {
  return (
    <PageFrame title={t.settingsPage.instructions} description={t.settingsPage.instructionsLede} wide>
      <Files agent={agent} forceReadOnly={readOnly} note={note} />
    </PageFrame>
  );
}

function Files({ agent, forceReadOnly, note }: { agent: string | null; forceReadOnly: boolean; note: string | null }) {
  const app = useApp();
  // From an agent's page, that agent's own files: in a deployment the bare
  // /api/... answers for the first agent (founder review: GitZoid showed
  // Starter Hub's files).
  const base = agent ? app.agents.list.find((a) => a.hub === agent)?.api_base ?? `/b/${encodeURIComponent(agent)}` : "";
  const [hub, setHub] = useState<string | null>(null);
  const [files, setFiles] = useState<FileItem[] | null>(null);
  const [pending, setPending] = useState<Pending[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<null | "skill" | "prompt">(null);
  const [readOnly, setReadOnly] = useState(false);
  const load = useCallback(async () => {
    try {
      const got = await get<{ items: FileItem[]; hub?: string; read_only?: boolean }>(hubUrl(base, `${A}/files`));
      setFiles(got.items);
      setHub(got.hub ?? null);
      setReadOnly(!!got.read_only || forceReadOnly);
      setPending((await get<{ items: Pending[] }>(hubUrl(base, "/api/hub-changes?status=pending"))).items);
      // From an agent's page: its instructions, when this hub is that agent.
      if (agent && got.hub && agent.toLowerCase() === got.hub) {
        setSelected((s) => s ?? (got.items.some((f) => f.path === "AGENTS.md") ? "AGENTS.md" : null));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? t.workspace.denied : describeError(e));
      setFiles([]);
    }
  }, [agent, base, forceReadOnly]);
  useEffect(() => void load(), [load]);
  const groups = useMemo(() => ({
    agent: (files ?? []).filter((f) => f.kind === "agent"),
    skill: (files ?? []).filter((f) => f.kind === "skill"),
    prompt: (files ?? []).filter((f) => f.kind === "prompt"),
  }), [files]);
  if (error) return <Notice tone="error">{error}</Notice>;
  return (
    <>
    {hub && !agent && <p className="hz-workspace-context">{t.workspace.hubContext(agentById(app.agents.list, hub)?.name ?? hub)}</p>}
    {readOnly && <p className="mb-4 text-[13px] text-mute">{note ?? t.workspace.readOnly}</p>}
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
              const open = pending.find((c) => c.paths.includes(f.path));
              return (
                <button key={f.path} type="button" onClick={() => setSelected(f.path)}
                  className={cx("hz-file-row", selected === f.path && "hz-file-row-on")}>
                  <FileCode2 size={13} aria-hidden className="flex-none" />
                  <span className="min-w-0 flex-1 truncate text-left">{f.path}</span>
                  {open && <span className="hz-pill hz-pill-syncing">{t.workspace.pending}</span>}
                </button>
              );
            })}
          </div>
        ))}
      </div>
      <div className="min-w-0">
        {selected ? (
          <Editor key={selected} base={base} path={selected} readOnly={readOnly} onChanged={load} />
        ) : (
          <div className="flex min-h-48 items-center justify-center px-6 py-10 text-center text-[13px] text-mute">{t.workspace.pick}</div>
        )}
      </div>
      <NewFile base={base} kind={creating} onClose={() => setCreating(null)} onCreated={async (path) => { setCreating(null); await load(); setSelected(path); }} />
    </div>
    </>
  );
}

function NewFile({ base, kind, onClose, onCreated }: { base: string; kind: null | "skill" | "prompt"; onClose: () => void; onCreated: (path: string) => void }) {
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
          const ch = await post<{ id: number }>(hubUrl(base, "/api/hub-changes"), { title: `New ${path}`, edits: [{ path, content: template }] });
          toast(t.workspace.proposed(ch.id));
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

function Editor({ base, path, readOnly = false, onChanged }: {
  base: string; path: string; readOnly?: boolean; onChanged: () => Promise<void> | void;
}) {
  const [original, setOriginal] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [versions, setVersions] = useState<Version[]>([]);
  const [busy, setBusy] = useState(false);
  const [proposed, setProposed] = useState<number | null>(null);
  useEffect(() => {
    get<{ text: string }>(hubUrl(base, `${A}/files/content?path=${encodeURIComponent(path)}`))
      .then((o) => { setOriginal(o.text); setText(o.text); }).catch((e) => toast(describeError(e), "error"));
    get<{ items: Version[] }>(hubUrl(base, `${A}/files/versions?path=${encodeURIComponent(path)}`)).then((o) => setVersions(o.items)).catch(() => {});
  }, [base, path]);
  const changed = original !== null && text !== original;
  const propose = async () => {
    setBusy(true);
    try {
      const ch = await post<{ id: number }>(hubUrl(base, "/api/hub-changes"), {
        title: `Edit ${path}`, edits: [{ path, old_text: original, new_text: text }],
      });
      setProposed(ch.id);
      await onChanged();
    } catch (e) {
      toast(describeError(e), "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <FileCode2 size={14} aria-hidden />
        <span className="font-mono text-[12.5px]">{path}</span>
      </div>
      <textarea className="hz-artifact-editor min-h-[320px] w-full" aria-label={t.workspace.editorLabel(path)} value={text} readOnly={readOnly}
        onChange={(e) => { setText(e.target.value); setProposed(null); }} spellCheck={false}
        data-testid="workspace-editor" />
      {proposed !== null && <Notice tone="info">{t.workspace.proposed(proposed)}</Notice>}
      {!readOnly && (
        <div className="flex items-center gap-2">
          <Button variant="primary" loading={busy} disabled={!changed} onClick={() => void propose()}
            data-testid="workspace-propose"><ListChecks size={14} aria-hidden /> {t.workspace.propose}</Button>
          {!changed && <span className="text-[12px] text-mute">{t.workspace.unchanged}</span>}
          <span className="hz-beta" title="Changing agent files from the browser is in Beta">Beta</span>
        </div>
      )}
      <Versions versions={versions} />
    </div>
  );
}

function Versions({ versions }: { versions: Version[] }) {
  return (
    <div className="mt-2">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-mute">
        <History size={12} aria-hidden /> {t.workspace.history}
      </div>
      {!versions.length && <div className="text-[12px] text-mute">{t.workspace.noHistory}</div>}
      <ul className="m-0 list-none space-y-1 p-0">
        {versions.map((v) => (
          <li key={v.commit} className="hz-source-row">
            <span className="font-mono text-[11.5px] text-ink">{v.commit.slice(0, 10)}</span>
            <span className="min-w-0 flex-1 truncate text-[11.5px]">{v.subject}</span>
            <span className="text-[11.5px] text-mute">{v.author} · {new Date(v.at * 1000).toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

