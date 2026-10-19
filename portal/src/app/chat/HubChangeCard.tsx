// Hub change card: a proposed change to the hub's own instructions or
// knowledge, shown where the agent proposed it. The server is the only source
// of state; a reviewer decides with the revision the card showed.
import { useCallback, useContext, useEffect, useState } from "react";
import { Check, ChevronDown, FilePen, Loader2, TriangleAlert, X } from "lucide-react";
import { ApiError, enc, get, hubUrl, post } from "../lib/api";
import { t } from "../i18n/en";
import { Button, cx } from "../components/ui";
import { FilesContext } from "./parts";

export type HubChange = {
  id: number;
  title: string;
  reason?: string | null;
  status: "pending" | "applying" | "applied" | "rejected" | "conflict" | "needs_attention";
  revision: number;
  proposed_by: string;
  proposed_via: string;
  decided_by?: string | null;
  note?: string | null;
  commit_sha?: string | null;
  publish_state?: string | null;
  problem?: string | null;
  undoes?: number | null;
  created_at: string;
  paths: string[];
  diff?: string;
  can_review?: boolean;
};

const H = t.hubChanges;

/** A unified diff, with added and removed lines marked. */
export function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="hz-diff" data-testid="hub-change-diff">
      {diff.split("\n").map((line, i) => (
        <span
          key={i}
          className={cx(
            line.startsWith("+") && !line.startsWith("+++") && "is-add",
            line.startsWith("-") && !line.startsWith("---") && "is-del",
            line.startsWith("@@") && "is-hunk",
          )}
        >
          {line + "\n"}
        </span>
      ))}
    </pre>
  );
}

export function statusText(c: HubChange): string {
  switch (c.status) {
    case "applied":
      return H.applied(c.decided_by || "", c.commit_sha ? c.commit_sha.slice(0, 7) : null);
    case "rejected":
      return H.rejected(c.decided_by || "") + (c.note ? `: ${c.note}` : "");
    case "conflict":
      return H.conflict;
    case "needs_attention":
      return H.attention;
    case "applying":
      return H.applying;
    default:
      return H.pending;
  }
}

export function useHubChange(base: string, id: number) {
  const url = hubUrl(base, `/api/hub-changes/${enc(String(id))}`);
  const [change, setChange] = useState<HubChange | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setChange(await get<HubChange>(url));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.hasServerMessage ? e.message : H.loadFailed);
    }
  }, [url]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (change && change.status !== "pending") return;
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [change, load]);
  return { change, setChange, error, load, url };
}

export function DecideButtons({ change, url, onDone }: { change: HubChange; url: string; onDone: (c: HubChange) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const decide = async (decision: "approve" | "reject") => {
    setBusy(decision);
    setFailure(null);
    try {
      onDone(await post<HubChange>(`${url}/decision`, { decision, expected_revision: change.revision }));
    } catch (e) {
      setFailure(e instanceof ApiError && e.hasServerMessage ? e.message : H.unknownOutcome);
    } finally {
      setBusy(null);
    }
  };
  return (
    <>
      <div className="hz-request-actions">
        <Button size="sm" variant="primary" loading={busy === "approve"} disabled={!!busy} onClick={() => decide("approve")} data-testid="hub-change-approve">
          {H.approve}
        </Button>
        <Button size="sm" loading={busy === "reject"} disabled={!!busy} onClick={() => decide("reject")} data-testid="hub-change-reject">
          {H.reject}
        </Button>
      </div>
      {failure && <p className="m-0 text-[12.5px] text-danger" role="alert">{failure}</p>}
    </>
  );
}

export function HubChangeCard({ id }: { id: number }) {
  const files = useContext(FilesContext);
  const { change, setChange, error, url } = useHubChange(files.apiBase, id);
  const [open, setOpen] = useState(false);
  if (error) return <p className="m-0 py-1 text-[13px] text-mute" data-testid="hub-change-card">{error}</p>;
  if (!change)
    return (
      <div className="hz-request is-loading" data-testid="hub-change-card" aria-busy>
        <Loader2 size={13} className="hz-spin text-mute" aria-hidden /> <span className="text-mute">{H.loading}</span>
      </div>
    );
  const pending = change.status === "pending";
  const bad = change.status === "conflict" || change.status === "needs_attention";
  return (
    <div className={cx("hz-request", !pending && "is-done")} data-testid="hub-change-card" data-status={change.status}>
      <div className="hz-request-head">
        <FilePen size={14} className="flex-none text-secondary" aria-hidden />
        <span className="text-ink">{H.cardTitle(change.id)}</span>
        <span className="min-w-0 truncate text-secondary">· {change.title}</span>
      </div>
      <p className="m-0 truncate text-[13px] text-mute">{change.paths.join(", ")}</p>
      <button type="button" className="hz-request-more" aria-expanded={open} onClick={() => setOpen(!open)} data-testid="hub-change-toggle">
        {open ? H.hideDiff : H.showDiff} <ChevronDown size={12} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
      </button>
      {open && change.diff && <DiffView diff={change.diff} />}
      {pending && change.can_review ? (
        <DecideButtons change={change} url={url} onDone={setChange} />
      ) : (
        <p className="hz-request-outcome">
          {bad ? (
            <TriangleAlert size={13} className="flex-none text-danger" aria-hidden />
          ) : change.status === "rejected" ? (
            <X size={13} className="flex-none text-mute" aria-hidden />
          ) : pending ? null : (
            <Check size={13} className="flex-none text-mute" aria-hidden />
          )}
          {pending ? H.waitingReview : statusText(change)}
          {change.problem && <span className="block text-[12.5px] text-mute">{change.problem}</span>}
        </p>
      )}
      <a className="text-[12.5px]" href={`/settings#/hub-changes?id=${change.id}`} data-testid="hub-change-open">
        {H.openReview}
      </a>
    </div>
  );
}
