// Settings > Memory: the notes an agent keeps about you (hubzoid.personal_memory).
// Plain text you can read, edit or delete; only you see it.
import { useCallback, useEffect, useState } from "react";
import { ApiError, del, get, hubUrl, put } from "../lib/api";
import { useApp } from "../lib/app-context";
import { t } from "../i18n/en";
import { Button, Notice } from "../components/ui";
import { toast } from "../components/toast";

const M = t.memory;
const MAX = 4000;
type Memory = { text: string; revision: number; updated_at: string | null };

export default function MemoryPage() {
  const app = useApp();
  const agents = app.agents.list;
  const [agentId, setAgentId] = useState<string | null>(agents[0]?.id ?? null);
  const agent = agents.find((a) => a.id === agentId) ?? agents[0];
  const url = hubUrl(agent?.api_base ?? "", "/api/memory");
  const [mem, setMem] = useState<Memory | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setMem(null); // another agent's notes never stay on screen to be saved here
    try {
      const m = await get<Memory>(url);
      setMem(m);
      setText(m.text);
    } catch (e) {
      setError(e instanceof ApiError && e.hasServerMessage ? e.message : M.loadFailed);
    }
  }, [url]);
  useEffect(() => {
    void load();
  }, [load]);

  const run = async (what: "save" | "clear") => {
    if (!mem) return;
    setBusy(what);
    try {
      const out = what === "save" ? await put<Memory>(url, { text, expected_revision: mem.revision }) : await del<Memory>(url);
      setMem(out);
      setText(out.text);
      toast(what === "save" ? M.saved : M.cleared);
    } catch (e) {
      if (e instanceof ApiError && e.code === "revision_conflict") {
        toast(M.conflict);
        await load();
      } else setError(e instanceof ApiError && e.hasServerMessage ? e.message : M.saveFailed);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-3" data-testid="memory">
      {agents.length > 1 && (
        <label className="flex items-center gap-2 text-[13px] text-secondary">
          {M.agent}
          <select className="hz-input !w-auto" value={agent?.id ?? ""} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {error && <Notice tone="error">{error}</Notice>}
      {!mem && !error && <p className="text-mute">{M.loading}</p>}
      {mem && (
        <>
          {!mem.text && <p className="m-0 text-[13px] text-mute">{M.empty}</p>}
          <textarea
            className="hz-input min-h-[180px] font-mono text-[13px]"
            value={text}
            maxLength={MAX}
            placeholder={M.placeholder}
            onChange={(e) => setText(e.target.value)}
            aria-label={M.title}
            data-testid="memory-text"
          />
          <p className="m-0 text-[12px] text-mute">{M.count(text.length, MAX)}</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="primary" size="sm" loading={busy === "save"} disabled={!!busy || text === mem.text} onClick={() => run("save")} data-testid="memory-save">
              {M.save}
            </Button>
            {mem.text && (
              <Button variant="ghost" size="sm" loading={busy === "clear"} disabled={!!busy} onClick={() => run("clear")} data-testid="memory-clear">
                {M.clear}
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
