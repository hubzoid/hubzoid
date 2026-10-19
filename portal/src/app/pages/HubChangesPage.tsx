// Settings > Hub changes: proposed changes to an agent's instructions and
// knowledge. Reviewers read the exact diff and apply or reject it; an applied
// change can be undone by a new proposal that needs its own review. The same
// API as `hubzoid hub-changes`.
import { Select } from "../components/Select";
import { useCallback, useEffect, useState } from "react";
import { ApiError, get, hubUrl, post } from "../lib/api";
import { useApp } from "../lib/app-context";
import { t } from "../i18n/en";
import { Button, Notice } from "../components/ui";
import { toast } from "../components/toast";
import { PageFrame } from "./PageFrame";
import { DecideButtons, DiffView, statusText, useHubChange, type HubChange } from "../chat/HubChangeCard";

const H = t.hubChanges;
const TABS = ["pending", "applied", "attention", "all"] as const;

function selectedId(): number | null {
  const id = new URLSearchParams(location.hash.split("?")[1] ?? "").get("id");
  return id && /^\d+$/.test(id) ? Number(id) : null;
}

export default function HubChangesPage() {
  const app = useApp();
  const agents = app.agents.list;
  const [agentId, setAgentId] = useState<string | null>(agents[0]?.id ?? null);
  const agent = agents.find((a) => a.id === agentId) ?? agents[0];
  const base = agent?.api_base ?? "";
  const [tab, setTab] = useState<(typeof TABS)[number]>("pending");
  const [items, setItems] = useState<HubChange[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(selectedId);

  useEffect(() => {
    const on = () => setOpen(selectedId());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const q = tab === "all" ? "" : `?status=${tab}`;
      setItems((await get<{ items: HubChange[] }>(hubUrl(base, `/api/hub-changes${q}`))).items);
    } catch (e) {
      setError(e instanceof ApiError && e.hasServerMessage ? e.message : H.loadFailed);
    }
  }, [base, tab]);
  useEffect(() => {
    void load();
  }, [load]);

  const show = (id: number | null) => {
    history.replaceState(null, "", `/settings#/hub-changes${id ? `?id=${id}` : ""}`);
    setOpen(id);
  };

  return (
    <PageFrame title={H.title} description={H.lede}>
      <div className="flex flex-col gap-4" data-testid="hub-changes">
        {agents.length > 1 && (
          <label className="flex items-center gap-2 text-[13px] text-secondary">
            {H.agent}
            <Select className="!w-auto" aria-label={H.agent} value={agent?.id ?? ""} onValueChange={setAgentId} options={agents.map(a => ({value: a.id, label: a.name}))} />
          </label>
        )}
        {open ? (
          <ChangeDetail base={base} id={open} onBack={() => (show(null), void load())} />
        ) : (
          <>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={H.title}>
              {TABS.map((k) => (
                <button key={k} type="button" aria-pressed={tab === k} className="hz-chip" onClick={() => setTab(k)} data-testid={`hub-changes-${k}`}>
                  {H.tabs[k]}
                </button>
              ))}
            </div>
            {error && <Notice tone="error">{error}</Notice>}
            {!items && !error && <p className="text-mute">{H.loading}</p>}
            {items && items.length === 0 && <p className="text-mute">{H.empty}</p>}
            {items && items.length > 0 && (
              <ul className="m-0 flex list-none flex-col p-0" data-testid="hub-change-list">
                {items.map((c) => (
                  <li key={c.id} className="border-b border-line last:border-0">
                    <button type="button" className="flex w-full flex-col items-start gap-0.5 py-2.5 text-left hover:bg-hover" onClick={() => show(c.id)}>
                      <span className="text-[13.5px] text-ink">
                        #{c.id} {c.title}
                      </span>
                      <span className="text-[12.5px] text-mute">
                        {statusText(c)} · {c.paths.join(", ")} · {c.proposed_by}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </PageFrame>
  );
}

function ChangeDetail({ base, id, onBack }: { base: string; id: number; onBack: () => void }) {
  const { change, setChange, error, url } = useHubChange(base, id);
  const [busy, setBusy] = useState(false);
  const undo = async () => {
    setBusy(true);
    try {
      const out = await post<HubChange>(`${url}/undo`, {});
      toast(H.undone);
      history.replaceState(null, "", `/settings#/hub-changes?id=${out.id}`);
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    } catch (e) {
      toast(e instanceof ApiError && e.hasServerMessage ? e.message : H.unknownOutcome);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-3" data-testid="hub-change-detail">
      <button type="button" className="self-start text-[13px] text-secondary hover:text-ink" onClick={onBack}>
        ← {H.back}
      </button>
      {error && <Notice tone="error">{error}</Notice>}
      {change && (
        <>
          <h3 className="m-0 text-[15px] font-medium text-ink">
            #{change.id} {change.title}
          </h3>
          <p className="m-0 text-[12.5px] text-mute">{H.proposedBy(change.proposed_by, change.proposed_via, change.created_at.replace("T", " ").slice(0, 16))}</p>
          {change.reason && (
            <p className="m-0 text-[13px] text-secondary">
              <span className="text-mute">{H.why}: </span>
              {change.reason}
            </p>
          )}
          <p className="m-0 text-[13px] text-secondary" data-testid="hub-change-status">
            {statusText(change)}
          </p>
          {change.problem && <Notice tone={change.status === "applied" ? "info" : "error"}>{change.problem}</Notice>}
          {change.diff && <DiffView diff={change.diff} />}
          {change.status === "pending" && change.can_review && <DecideButtons change={change} url={url} onDone={setChange} />}
          {change.status === "applied" && (
            <Button size="sm" className="self-start" loading={busy} onClick={undo} data-testid="hub-change-undo">
              {H.undo}
            </Button>
          )}
        </>
      )}
    </div>
  );
}
