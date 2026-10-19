// Documents the agent made in this conversation, beside it: Preview and Source,
// every version (generated or edited, with who and when), Copy, Download,
// full screen, and editing the source as a new version.
//
// Generated pages (HTML, SVG) run only inside the sandboxed artifact frame
// (/api/chat/artifact-frame): an opaque origin, scripts allowed, nothing else,
// and no network. The page's source reaches the frame as a message; this
// window never inserts it into its own document.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, Maximize2, Pencil, X } from "lucide-react";
import { ApiError, enc, get, hubUrl, post } from "../lib/api";
import { t } from "../i18n/en";
import { Button } from "../components/ui";
import { CopyButton } from "./Markdown";

export type ArtifactVersion = {
  version_id: string;
  ordinal: number;
  provenance: "generated" | "edited" | "imported";
  author_id: string;
  run_id: string | null;
  created_at: number;
  size_bytes: number;
  mime_type: string;
  base_version_id: string | null;
};
export type Artifact = {
  artifact_id: string;
  title: string;
  kind: "html" | "svg" | "markdown" | "image" | "pdf" | "text" | "download";
  current_version_id: string;
  revision: number;
  updated_at: number;
  versions: ArtifactVersion[];
};

const TEXT_KINDS = new Set(["html", "svg", "markdown", "text"]);

export async function loadArtifacts(apiBase: string, conversationId: string): Promise<Artifact[]> {
  const out = await get<{ items: Artifact[] }>(
    hubUrl(apiBase, `/api/chat/artifacts?conversation_id=${enc(conversationId)}`),
    { quiet401: true },
  );
  return out.items;
}

function contentUrl(apiBase: string, a: Artifact, versionId: string, extra = "") {
  return hubUrl(apiBase, `/api/chat/artifacts/${enc(a.artifact_id)}/versions/${enc(versionId)}/content${extra}`);
}

/** A generated page in the sandboxed frame. */
function SandboxFrame({ apiBase, kind, source, title }: { apiBase: string; kind: string; source: string; title: string }) {
  const ref = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.source !== ref.current?.contentWindow) return;
      if ((e.data as { type?: string })?.type === "hz-frame-ready") setReady(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);
  useEffect(() => {
    // The frame's origin is opaque ("null"), so "*" is the only target that reaches it.
    if (ready) ref.current?.contentWindow?.postMessage({ type: "hz-render", kind, source }, "*");
  }, [ready, kind, source]);
  return (
    <iframe
      ref={ref}
      title={title}
      className="hz-artifact-frame"
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      src={hubUrl(apiBase, "/api/chat/artifact-frame")}
    />
  );
}

export function ArtifactPanel({
  apiBase,
  artifacts,
  selectedId,
  onSelect,
  onClose,
  onChanged,
  canEdit,
}: {
  apiBase: string;
  artifacts: Artifact[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
  onChanged: () => void;
  canEdit: boolean;
}) {
  const artifact = artifacts.find((a) => a.artifact_id === selectedId) ?? artifacts[0];
  const [versionId, setVersionId] = useState<string | null>(null);
  const [tab, setTab] = useState<"preview" | "source">("preview");
  const [text, setText] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const body = useRef<HTMLDivElement>(null);

  const current = versionId ?? artifact?.current_version_id ?? null;
  const version = useMemo(() => artifact?.versions.find((v) => v.version_id === current), [artifact, current]);

  useEffect(() => {
    setVersionId(null);
    setEditing(false);
    setTab("preview");
  }, [artifact?.artifact_id]);

  useEffect(() => {
    if (!artifact || !current) return;
    let cancelled = false;
    setText(null);
    setFailed(null);
    setBlobUrl(null);
    let made: string | null = null;
    (async () => {
      try {
        if (TEXT_KINDS.has(artifact.kind)) {
          const out = await get<{ text: string }>(contentUrl(apiBase, artifact, current, "?as=text"));
          if (!cancelled) setText(out.text);
        } else if (artifact.kind === "image") {
          const res = await fetch(contentUrl(apiBase, artifact, current), { credentials: "same-origin" });
          if (!res.ok) throw new Error(String(res.status));
          made = URL.createObjectURL(new Blob([await res.arrayBuffer()], { type: version?.mime_type || "image/png" }));
          if (!cancelled) setBlobUrl(made);
        }
      } catch {
        if (!cancelled) setFailed(t.artifacts.previewFailed);
      }
    })();
    return () => {
      cancelled = true;
      if (made) URL.revokeObjectURL(made);
    };
  }, [apiBase, artifact, current, version?.mime_type]);

  const save = useCallback(async () => {
    if (!artifact || !current) return;
    setSaving(true);
    setFailed(null);
    try {
      await post(hubUrl(apiBase, `/api/chat/artifacts/${enc(artifact.artifact_id)}/versions`), {
        content: draft,
        base_version_id: current,
        expected_revision: artifact.revision,
      });
      setEditing(false);
      setVersionId(null);
      onChanged();
    } catch (e) {
      setFailed(e instanceof ApiError && e.status === 409 ? t.artifacts.conflict : t.artifacts.saveFailed);
    } finally {
      setSaving(false);
    }
  }, [apiBase, artifact, current, draft, onChanged]);

  if (!artifact) return null;
  const isLatest = current === artifact.current_version_id;
  const preview = (() => {
    if (failed && !editing) return <div className="hz-artifact-empty">{failed}</div>;
    if (artifact.kind === "html" || artifact.kind === "svg")
      return text === null ? <div className="hz-skeleton h-40 w-full" /> : (
        <SandboxFrame apiBase={apiBase} kind={artifact.kind} source={text} title={artifact.title} />
      );
    if (artifact.kind === "markdown")
      return text === null ? <div className="hz-skeleton h-40 w-full" /> : (
        <div className="hz-md hz-artifact-doc">
          {/* No raw HTML: a markdown document renders as text and structure only. */}
          <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{text}</ReactMarkdown>
        </div>
      );
    if (artifact.kind === "image") return blobUrl ? <img className="hz-artifact-img" src={blobUrl} alt={artifact.title} /> : null;
    if (artifact.kind === "text") return <pre className="hz-artifact-source">{text ?? ""}</pre>;
    return <div className="hz-artifact-empty">{t.artifacts.noPreview}</div>;
  })();

  return (
    <aside className="hz-artifact-panel" aria-label={t.artifacts.panel}>
      <div className="hz-artifact-head">
        {artifacts.length > 1 ? (
          <select
            className="hz-artifact-select font-semibold"
            value={artifact.artifact_id}
            onChange={(e) => onSelect(e.target.value)}
            aria-label={t.artifacts.choose}
          >
            {artifacts.map((a) => (
              <option key={a.artifact_id} value={a.artifact_id}>
                {a.title}
              </option>
            ))}
          </select>
        ) : (
          <span className="truncate text-[13px] font-semibold text-ink">{artifact.title}</span>
        )}
        <select
          className="hz-artifact-select"
          value={current ?? ""}
          onChange={(e) => setVersionId(e.target.value)}
          aria-label={t.artifacts.version}
        >
          {[...artifact.versions].reverse().map((v) => (
            <option key={v.version_id} value={v.version_id}>
              {t.artifacts.versionLabel(v.ordinal, v.provenance, v.version_id === artifact.current_version_id)}
            </option>
          ))}
        </select>
        <span className="flex-1" />
        {TEXT_KINDS.has(artifact.kind) && text !== null && <CopyButton text={text} label={t.artifacts.copy} />}
        <a
          className="hz-icon-link"
          href={contentUrl(apiBase, artifact, current ?? "")}
          download={artifact.title}
          aria-label={t.artifacts.download(artifact.title)}
          title={t.artifacts.downloadShort}
        >
          <Download size={15} aria-hidden />
        </a>
        <button
          type="button"
          className="hz-icon-link"
          aria-label={t.artifacts.fullscreen}
          title={t.artifacts.fullscreen}
          onClick={() => void body.current?.requestFullscreen?.().catch(() => {})}
        >
          <Maximize2 size={15} aria-hidden />
        </button>
        <button type="button" className="hz-icon-link" aria-label={t.artifacts.close} title={t.artifacts.close} onClick={onClose}>
          <X size={16} aria-hidden />
        </button>
      </div>
      {TEXT_KINDS.has(artifact.kind) && (
        <div className="hz-artifact-tabs" role="tablist" aria-label={artifact.title}>
          <button role="tab" type="button" aria-selected={tab === "preview" && !editing} onClick={() => { setTab("preview"); setEditing(false); }}>
            {t.artifacts.preview}
          </button>
          <button role="tab" type="button" aria-selected={tab === "source" || editing} onClick={() => setTab("source")}>
            {t.artifacts.source}
          </button>
          <span className="flex-1" />
          {canEdit && isLatest && text !== null && !editing && (
            <button type="button" className="hz-artifact-edit" onClick={() => { setDraft(text); setEditing(true); }}>
              <Pencil size={13} aria-hidden /> {t.artifacts.edit}
            </button>
          )}
        </div>
      )}
      <div className="hz-artifact-body" ref={body}>
        {editing ? (
          <div className="flex h-full flex-col gap-2 p-3">
            <textarea
              className="hz-artifact-editor"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              aria-label={t.artifacts.editLabel}
              spellCheck={false}
            />
            {failed && <div className="text-[12px]" style={{ color: "var(--hz-danger)" }}>{failed}</div>}
            <div className="flex justify-end gap-2">
              <Button size="sm" onClick={() => setEditing(false)}>{t.cancel}</Button>
              <Button size="sm" variant="primary" loading={saving} disabled={draft === text} onClick={() => void save()}>
                {t.artifacts.saveVersion}
              </Button>
            </div>
          </div>
        ) : tab === "source" && TEXT_KINDS.has(artifact.kind) ? (
          <pre className="hz-artifact-source">{text ?? ""}</pre>
        ) : (
          preview
        )}
      </div>
      {version && (
        <div className="hz-artifact-foot">
          {t.artifacts.meta(version.ordinal, version.provenance, new Date(version.created_at * 1000))}
        </div>
      )}
    </aside>
  );
}
