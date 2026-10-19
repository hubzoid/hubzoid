// Approval and question cards: a tool call that stopped to ask a person, shown
// where it happened in the chat. The server is the only source of state: the
// card reads the request, sends one decision with the revision and digest it
// showed, and never claims success it did not get back.
import { useCallback, useContext, useEffect, useId, useState } from "react";
import { Check, ChevronDown, CircleSlash, Hand, Loader2, MessageCircleQuestion, X } from "lucide-react";
import { ApiError, enc, get, hubUrl, post } from "../lib/api";
import { t } from "../i18n/en";
import { Button, cx } from "../components/ui";
import { FilesContext, humanTool, summarizeArgs, toolLabel } from "./parts";

export type HitlRequest = {
  id: string;
  kind: "approval" | "question";
  status: "pending" | "approved" | "rejected" | "answered" | "cancelled";
  revision: number;
  title: string;
  tool?: string | null;
  arguments?: Record<string, unknown> | null;
  digest: string;
  question?: string | null;
  options?: { label: string; description?: string }[] | null;
  subject?: { name?: string; url?: string; version?: number } | null;
  decision?: string | null;
  answer?: string | null;
  note?: string | null;
  decided_by_email?: string | null;
  requester_email?: string | null;
  allowed_actions: string[];
  can_cancel?: boolean;
};

const POLL_MS = 4000;

function useRequest(id: string) {
  const files = useContext(FilesContext);
  const url = hubUrl(files.apiBase, `/api/requests/${enc(id)}`);
  const [req, setReq] = useState<HitlRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setReq(await get<HitlRequest>(url));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? t.requests.gone : t.requests.loadFailed);
    }
  }, [url]);
  useEffect(() => {
    void load();
  }, [load]);
  // Another tab, a channel or the Console may decide it: keep the card current.
  useEffect(() => {
    if (req && req.status !== "pending") return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [req, load]);
  return { req, setReq, error, load, url };
}

function outcomeLine(req: HitlRequest): { icon: JSX.Element; text: string } {
  const who = req.decided_by_email ? ` · ${req.decided_by_email}` : "";
  switch (req.status) {
    case "approved":
      return {
        icon: <Check size={13} className="flex-none text-mute" aria-hidden />,
        text:
          (req.decision === "approve_chat"
            ? t.requests.approvedChat
            : req.decision === "approve_always"
              ? t.requests.approvedAlways
              : t.requests.approved) + who,
      };
    case "rejected":
      return {
        icon: <X size={13} className="flex-none text-mute" aria-hidden />,
        text: `${req.decision === "request_changes" ? t.requests.changesRequested : t.requests.rejected}${who}${req.note ? `: ${req.note}` : ""}`,
      };
    case "answered":
      return { icon: <Check size={13} className="flex-none text-mute" aria-hidden />, text: `${t.requests.answered}: ${req.answer}` };
    default:
      return { icon: <CircleSlash size={13} className="flex-none text-mute" aria-hidden />, text: t.requests.cancelled };
  }
}

export function RequestCard({ requestId, toolName, args }: { requestId: string; toolName?: string; args?: unknown }) {
  const { req, setReq, error, load, url } = useRequest(requestId);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [answer, setAnswer] = useState("");
  const [open, setOpen] = useState(false);
  const detailsId = useId();

  if (error) return <p className="m-0 py-1 text-[13px] text-mute" data-testid="request-card">{error}</p>;
  if (!req)
    return (
      <div className="hz-request is-loading" data-testid="request-card" aria-busy>
        <Loader2 size={13} className="hz-spin text-mute" aria-hidden /> <span className="text-mute">{t.requests.loading}</span>
      </div>
    );

  const decide = async (decision: string, extra: Record<string, unknown> = {}) => {
    setBusy(decision);
    setFailure(null);
    try {
      const out = await post<HitlRequest>(`${url}/decisions`, {
        decision,
        expected_revision: req.revision,
        digest: req.digest,
        ...extra,
      });
      setReq(out);
    } catch (e) {
      // An unknown outcome is checked, never assumed.
      setFailure(e instanceof ApiError && e.hasServerMessage ? e.message : t.requests.unknownOutcome);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const pending = req.status === "pending";
  const mine = req.allowed_actions.length > 0;
  const label = toolLabel(toolName || req.tool || "");
  const shownArgs = req.arguments ?? (args as Record<string, unknown> | undefined) ?? {};
  const summary = summarizeArgs(shownArgs);

  if (req.kind === "question") {
    return (
      <div className={cx("hz-request", !pending && "is-done")} data-testid="request-card" data-kind="question" data-status={req.status}>
        <div className="hz-request-head">
          <MessageCircleQuestion size={14} className="flex-none text-secondary" aria-hidden />
          <span className="text-ink">{req.question || req.title}</span>
        </div>
        {pending && mine ? (
          <>
            {!!req.options?.length && (
              <div className="hz-request-options" role="group" aria-label={t.requests.options}>
                {req.options.map((o) => (
                  <button
                    key={o.label}
                    type="button"
                    className="hz-request-option"
                    disabled={!!busy}
                    onClick={() => decide("answer", { answer: o.label })}
                    data-testid="request-option"
                  >
                    <span className="block text-[13px] text-ink">{o.label}</span>
                    {o.description && <span className="block text-[12px] text-mute">{o.description}</span>}
                  </button>
                ))}
              </div>
            )}
            <form
              className="hz-request-answer"
              onSubmit={(e) => {
                e.preventDefault();
                if (answer.trim()) void decide("answer", { answer: answer.trim() });
              }}
            >
              <input
                className="hz-input"
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder={req.options?.length ? t.requests.orType : t.requests.typeAnswer}
                aria-label={t.requests.typeAnswer}
                data-testid="request-answer"
              />
              <Button type="submit" size="sm" loading={busy === "answer"} disabled={!answer.trim() || !!busy} data-testid="request-answer-send">
                {t.requests.send}
              </Button>
            </form>
          </>
        ) : pending ? (
          <p className="m-0 text-[12.5px] text-mute">{t.requests.waitingFor(req.requester_email || "")}</p>
        ) : (
          <p className="hz-request-outcome">{outcomeLine(req).icon}{outcomeLine(req).text}</p>
        )}
        {failure && <p className="m-0 text-[12.5px] text-danger" role="alert">{failure}</p>}
      </div>
    );
  }

  const isPlan = !!req.subject;
  return (
    <div className={cx("hz-request", !pending && "is-done")} data-testid="request-card" data-kind="approval" data-status={req.status}>
      <div className="hz-request-head">
        <Hand size={14} className="flex-none text-secondary" aria-hidden />
        <span className="text-ink">{pending ? (isPlan ? t.requests.planNeeded : t.requests.needed) : t.requests.title}</span>
        <span className="min-w-0 truncate text-secondary">· {isPlan ? req.title : humanTool(label.tool || req.title)}</span>
        {label.server && <span className="hz-tag is-quiet">{label.server}</span>}
      </div>
      {isPlan && req.subject?.url ? (
        <a className="text-[13px]" href={req.subject.url} target="_blank" rel="noopener">
          {t.requests.openPlan(req.subject.name || req.title, req.subject.version)}
        </a>
      ) : (
        summary && <p className="m-0 truncate text-[13px] text-mute">{summary}</p>
      )}
      {!isPlan && (
        <>
          <button type="button" className="hz-request-more" aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen((o) => !o)}>
            {t.requests.exact} <ChevronDown size={12} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
          </button>
          <div id={detailsId} hidden={!open}>
            <pre className="hz-tech" data-testid="request-args">{JSON.stringify(shownArgs, null, 2)}</pre>
            <p className="m-0 text-[11.5px] text-mute">
              {t.requests.digest} <span className="font-mono">{req.digest.slice(0, 19)}…</span>
            </p>
          </div>
        </>
      )}
      {pending && mine && !rejecting && (
        <div className="hz-request-actions">
          <Button variant="primary" size="sm" loading={busy === "approve"} disabled={!!busy} onClick={() => decide("approve")} data-testid="request-approve">
            {t.requests.approve}
          </Button>
          {req.allowed_actions.includes("approve_chat") && (
            <Button size="sm" loading={busy === "approve_chat"} disabled={!!busy} onClick={() => decide("approve_chat")} data-testid="request-approve-chat">
              {t.requests.approveChat}
            </Button>
          )}
          {req.allowed_actions.includes("approve_always") && (
            <Button size="sm" loading={busy === "approve_always"} disabled={!!busy} onClick={() => decide("approve_always")} data-testid="request-approve-always">
              {t.requests.approveAlways}
            </Button>
          )}
          <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => setRejecting(true)} data-testid="request-reject">
            {isPlan ? t.requests.requestChanges : t.requests.reject}
          </Button>
        </div>
      )}
      {pending && mine && rejecting && (
        <form
          className="hz-request-answer"
          onSubmit={(e) => {
            e.preventDefault();
            void decide(isPlan ? "request_changes" : "reject", { note: note.trim() || undefined });
          }}
        >
          <input
            className="hz-input"
            value={note}
            autoFocus
            onChange={(e) => setNote(e.target.value)}
            placeholder={isPlan ? t.requests.whatChange : t.requests.noteOptional}
            aria-label={t.requests.note}
            data-testid="request-note"
          />
          <Button type="submit" size="sm" loading={!!busy} disabled={!!busy || (isPlan && !note.trim())} data-testid="request-reject-send">
            {isPlan ? t.requests.send : t.requests.reject}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setRejecting(false)}>
            {t.requests.back}
          </Button>
        </form>
      )}
      {pending && !mine && <p className="m-0 text-[12.5px] text-mute">{t.requests.waitingFor(req.requester_email || "")}</p>}
      {!pending && <p className="hz-request-outcome">{outcomeLine(req).icon}{outcomeLine(req).text}</p>}
      {failure && <p className="m-0 text-[12.5px] text-danger" role="alert">{failure}</p>}
    </div>
  );
}
