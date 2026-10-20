// Rating an answer: thumbs up, or thumbs down with reasons and a comment.
//
// A rating belongs to the person and to one exact answer. The conversation's
// ratings load once with it (GET /api/conversations/{id}/feedback), so a reload
// shows them. A button shows as chosen only once the server saved it; a failed
// save keeps the form open with what was typed; a rating changed in another
// window is reloaded, never overwritten.
import { useEffect, useState, useSyncExternalStore } from "react";
import { Popover } from "radix-ui";
import { Loader2, ThumbsDown, ThumbsUp } from "lucide-react";
import { ApiError, enc, get, hubUrl, put, request } from "../lib/api";
import { t } from "../i18n/en";
import { toast } from "../components/toast";
import { Button, IconButton } from "../components/ui";

export type Rating = {
  id: string;
  message_id: string;
  rating: "positive" | "negative";
  reason_ids: string[];
  comment: string | null;
  revision: number;
};

// What can be wrong with an answer (the deployment also accepts "accurate" and "useful").
const REASONS = ["incorrect", "missing_source", "other"] as const;

// -- the conversation's ratings, shared by every answer on screen ---------------------------

type Store = {
  items: Map<string, Rating>;
  state: "idle" | "loading" | "ready" | "error";
  version: number;
  listeners: Set<() => void>;
};

const stores = new Map<string, Store>();

function storeFor(key: string): Store {
  let s = stores.get(key);
  if (!s) {
    s = { items: new Map(), state: "idle", version: 0, listeners: new Set() };
    stores.set(key, s);
  }
  return s;
}

function changed(s: Store) {
  s.version += 1;
  s.listeners.forEach((l) => l());
}

async function load(apiBase: string | undefined, conv: string, s: Store) {
  if (s.state === "loading") return;
  s.state = "loading";
  try {
    const out = await get<{ items: Rating[] }>(hubUrl(apiBase, `/api/conversations/${enc(conv)}/feedback`));
    s.items = new Map(out.items.map((r) => [r.message_id, r]));
    s.state = "ready";
  } catch {
    s.state = "error"; // the buttons still work: a save tells the truth
  }
  changed(s);
}

function setMine(s: Store, messageId: string, row: Rating | null) {
  if (row) s.items.set(messageId, row);
  else s.items.delete(messageId);
  changed(s);
}

/** This person's saved rating of one answer (undefined while loading). */
function useMine(apiBase: string | undefined, conv: string, messageId: string) {
  const key = `${apiBase ?? ""}|${conv}`;
  const s = storeFor(key);
  useSyncExternalStore(
    (l) => {
      s.listeners.add(l);
      return () => s.listeners.delete(l);
    },
    () => s.version,
  );
  useEffect(() => {
    if (s.state === "idle") void load(apiBase, conv, s);
  }, [apiBase, conv, s]);
  return { mine: s.items.get(messageId) ?? null, ready: s.state === "ready" || s.state === "error", store: s };
}

// -- the controls -------------------------------------------------------------------------

export function FeedbackControls({
  apiBase,
  conversationId,
  messageId,
  onError,
}: {
  apiBase?: string;
  conversationId: string;
  messageId: string;
  onError: (error: unknown) => void;
}) {
  const { mine, store } = useMine(apiBase, conversationId, messageId);
  const [saving, setSaving] = useState<null | "positive" | "negative" | "remove">(null);
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const url = hubUrl(apiBase, `/api/conversations/${enc(conversationId)}/messages/${enc(messageId)}/feedback`);

  // Opening the form starts from what is saved (or what was typed and not saved yet).
  const openForm = (next: boolean) => {
    if (next && !problem) {
      setReasons(mine?.rating === "negative" ? [...mine.reason_ids] : []);
      setComment(mine?.rating === "negative" ? (mine.comment ?? "") : "");
    }
    setOpen(next);
  };

  /** The server's current rating, after a conflict: shown, never overwritten. */
  const reload = async () => {
    try {
      const out = await get<{ feedback: Rating | null }>(url);
      setMine(store, messageId, out.feedback);
    } catch {
      /* the next save says what happened */
    }
  };

  const failed = async (error: unknown, keepForm: boolean) => {
    if (error instanceof ApiError && error.status === 409 && error.code === "revision_conflict") {
      await reload();
      setProblem(t.chat.feedbackConflict);
      if (!keepForm) toast(t.chat.feedbackConflict, "info");
      return;
    }
    if (error instanceof ApiError && error.code === "feature_disabled") {
      setProblem(t.chat.feedbackOff);
      if (!keepForm) toast(t.chat.feedbackOff, "error");
      return;
    }
    const message = error instanceof ApiError && error.hasServerMessage ? error.message : t.chat.feedbackSaveFailed;
    setProblem(message);
    if (!keepForm) onError(error);
  };

  const save = async (rating: "positive" | "negative", extra: Record<string, unknown>, keepForm: boolean) => {
    setSaving(rating);
    setProblem(null);
    try {
      const out = await put<Rating>(url, { expected_revision: mine ? mine.revision : "absent", rating, ...extra });
      setMine(store, messageId, out);
      if (keepForm) setOpen(false);
      toast(rating === "positive" ? t.chat.feedbackSavedGood : t.chat.feedbackSaved);
    } catch (error) {
      await failed(error, keepForm);
    } finally {
      setSaving(null);
    }
  };

  const remove = async (keepForm: boolean) => {
    if (!mine) return;
    setSaving("remove");
    setProblem(null);
    try {
      await request(url, { method: "DELETE", body: { expected_revision: mine.revision } });
      setMine(store, messageId, null);
      if (keepForm) setOpen(false);
      toast(t.chat.feedbackRemoved);
    } catch (error) {
      await failed(error, keepForm);
    } finally {
      setSaving(null);
    }
  };

  const shown = [...REASONS, ...reasons.filter((r) => !(REASONS as readonly string[]).includes(r))];
  const busy = saving !== null;
  return (
    <>
      <IconButton
        label={mine?.rating === "positive" ? t.chat.feedbackGoodUndo : t.chat.feedbackGood}
        aria-pressed={mine?.rating === "positive"}
        aria-busy={saving === "positive" || undefined}
        disabled={busy}
        onClick={() => void (mine?.rating === "positive" ? remove(false) : save("positive", {}, false))}
        data-testid="feedback-positive"
        data-saved={mine?.rating === "positive" || undefined}
      >
        {saving === "positive" ? <Loader2 size={15} className="hz-spin" aria-hidden /> : <ThumbsUp size={15} aria-hidden />}
      </IconButton>
      <Popover.Root open={open} onOpenChange={openForm}>
        <Popover.Trigger asChild>
          <IconButton
            label={mine?.rating === "negative" ? t.chat.feedbackBadEdit : t.chat.feedbackBad}
            aria-pressed={mine?.rating === "negative"}
            aria-haspopup="dialog"
            disabled={busy && !open}
            data-testid="feedback-negative"
            data-saved={mine?.rating === "negative" || undefined}
          >
            <ThumbsDown size={15} aria-hidden />
          </IconButton>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            className="hz-menu hz-feedback-form space-y-2 !p-3"
            sideOffset={6}
            align="start"
            collisionPadding={12}
            role="dialog"
            aria-label={t.chat.feedbackWhat}
            data-testid="feedback-form"
          >
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault();
                void save("negative", { reason_ids: reasons, comment: comment.trim() || null }, true);
              }}
            >
              <p className="m-0 text-[13px] font-medium text-ink">{t.chat.feedbackWhat}</p>
              <div className="flex flex-wrap gap-1.5" role="group" aria-label={t.chat.feedbackReasons}>
                {shown.map((r) => (
                  <button
                    key={r}
                    type="button"
                    className="hz-chip"
                    aria-pressed={reasons.includes(r)}
                    data-testid={`feedback-reason-${r}`}
                    onClick={() => setReasons((all) => (all.includes(r) ? all.filter((x) => x !== r) : [...all, r]))}
                  >
                    {t.chat.feedbackReason[r] ?? r}
                  </button>
                ))}
              </div>
              <label className="sr-only" htmlFor={`fb-comment-${messageId}`}>
                {t.chat.feedbackComment}
              </label>
              <textarea
                id={`fb-comment-${messageId}`}
                className="hz-input !min-h-16 w-full text-[13px]"
                placeholder={t.chat.feedbackComment}
                value={comment}
                maxLength={4000}
                data-testid="feedback-comment"
                onChange={(e) => setComment(e.target.value)}
              />
              {problem && (
                <p role="alert" className="m-0 text-[12.5px] text-danger" data-testid="feedback-problem">
                  {problem}
                </p>
              )}
              <div className="flex flex-wrap items-center justify-end gap-2">
                {mine && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="mr-auto"
                    loading={saving === "remove"}
                    disabled={busy}
                    onClick={() => void remove(true)}
                    data-testid="feedback-remove"
                  >
                    {t.chat.feedbackRemove}
                  </Button>
                )}
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
                  {t.cancel}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  type="submit"
                  loading={saving === "negative"}
                  disabled={busy}
                  data-testid="feedback-send"
                >
                  {problem ? t.chat.feedbackRetry : t.chat.feedbackSend}
                </Button>
              </div>
            </form>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </>
  );
}
