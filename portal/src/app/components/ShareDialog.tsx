// Create, copy and revoke the read-only link for one conversation
// (GET/POST/DELETE /api/conversations/{id}/share).
import { useEffect, useState } from "react";
import { Copy, Link2, Link2Off } from "lucide-react";
import { t } from "../i18n/en";
import { ApiError, del, enc, get, post } from "../lib/api";
import { describeError } from "../lib/errors";
import type { Conversation, ShareLink } from "../lib/types";
import { toast } from "./toast";
import { Button, Modal, Notice, Spinner } from "./ui";

export function shareUrl(link: ShareLink): string {
  if (link.url && /^https?:\/\//.test(link.url)) return link.url;
  const path = link.url && link.url.startsWith("/") ? link.url : `/s/${encodeURIComponent(link.share_id)}`;
  return `${location.origin}${path}`;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Older browsers and insecure origins: fall back to a selection copy.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    return ok;
  }
}

export { copyText };

export function ShareDialog({
  conversation,
  onClose,
}: {
  conversation: Pick<Conversation, "id" | "title"> | null;
  onClose: () => void;
}) {
  const open = !!conversation;
  const [link, setLink] = useState<ShareLink | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [busy, setBusy] = useState<"create" | "revoke" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = conversation?.id;

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setStatus("loading");
    setLink(null);
    setError(null);
    get<ShareLink>(`/api/conversations/${enc(id)}/share`)
      .then((data) => {
        if (cancelled) return;
        setLink(data && data.share_id ? data : null);
        setStatus("ready");
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setLink(null);
          setStatus("ready");
        } else {
          setError(describeError(e, t.share.loadError));
          setStatus("error");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const create = async () => {
    if (!id) return;
    setBusy("create");
    setError(null);
    try {
      const data = await post<ShareLink>(`/api/conversations/${enc(id)}/share`);
      setLink(data);
      if (data?.share_id && (await copyText(shareUrl(data)))) toast(t.share.copied);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  };

  const revoke = async () => {
    if (!id) return;
    setBusy("revoke");
    setError(null);
    try {
      await del(`/api/conversations/${enc(id)}/share`);
      setLink(null);
      toast(t.share.revoked);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  };

  const url = link?.share_id ? shareUrl(link) : "";

  return (
    <Modal
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title={t.share.title}
      description={t.share.body}
    >
      {status === "loading" ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : (
        <div className="space-y-4">
          {error && <Notice tone="error">{error}</Notice>}
          {url ? (
            <>
              <div>
                <label className="hz-label" htmlFor="hz-share-url">
                  {t.share.linkLabel}
                </label>
                <div className="flex gap-2">
                  <input
                    id="hz-share-url"
                    readOnly
                    value={url}
                    onFocus={(e) => e.currentTarget.select()}
                    className="hz-input min-w-0 flex-1 font-mono !text-[13px]"
                  />
                  <Button
                    variant="primary"
                    icon={<Copy size={15} aria-hidden />}
                    onClick={async () => {
                      if (await copyText(url)) toast(t.share.copied);
                    }}
                  >
                    {t.share.copy}
                  </Button>
                </div>
              </div>
              <div className="flex justify-between gap-2 border-t border-line-soft pt-4">
                <Button
                  variant="ghost"
                  className="!text-danger"
                  icon={<Link2Off size={15} aria-hidden />}
                  loading={busy === "revoke"}
                  onClick={() => void revoke()}
                >
                  {busy === "revoke" ? t.share.revoking : t.share.revoke}
                </Button>
                <Button onClick={onClose}>{t.close}</Button>
              </div>
            </>
          ) : status === "ready" ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="m-0 text-sm text-mute">{t.share.none}</p>
              <Button
                variant="primary"
                icon={<Link2 size={15} aria-hidden />}
                loading={busy === "create"}
                onClick={() => void create()}
              >
                {busy === "create" ? t.share.creating : t.share.create}
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </Modal>
  );
}
