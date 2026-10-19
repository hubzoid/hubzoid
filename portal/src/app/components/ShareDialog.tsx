// Share a conversation: a snapshot link (previewed, then published for named
// people, everyone signed in here, or the public when allowed), or live
// collaborators who read along or write on their own branches.
import { useEffect, useId, useState } from "react";
import { CircleHelp, Copy, Link2, Link2Off, UserMinus, Users } from "lucide-react";
import { t } from "../i18n/en";
import { del, enc, get, post, put } from "../lib/api";
import { describeError } from "../lib/errors";
import type { Conversation, ShareLink } from "../lib/types";
import { toast } from "./toast";
import { Button, Field, Modal, Notice, Spinner, Tip, cx } from "./ui";

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

type Audience = "deployment" | "named" | "public";
type Share = { id: string; url?: string; revision: number; status: string; audience: { type: Audience }; allow_copy: boolean; created_at: number };
/** A snapshot link as a full address on this site: the server gives a path. */
const linkOf = (share: Share) => shareUrl({ share_id: share.id, url: share.url });
type Preview = { preview_id: string; digest: string; omissions: string[]; projection: { messages: unknown[] } };
type Member = { principal_id: string; role: "editor" | "viewer"; email?: string | null; name?: string | null };

/** Hover or focus for help; tap to keep it open on touch devices. */
function ShareExplanation({ children, help }: { children: string; help: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return <div>
    <div className="flex items-center gap-1 text-[13px] text-mute">
      <p className="m-0">{children}</p>
      <Tip label={help}>
        <button type="button" className="hz-icon-btn flex-none" aria-label="About sharing" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          <CircleHelp size={14} aria-hidden />
        </button>
      </Tip>
    </div>
    <p id={id} hidden={!open} className="m-0 mt-1 text-[13px] text-mute">{help}</p>
  </div>;
}

export function ShareDialog({
  conversation,
  onClose,
}: {
  conversation: Pick<Conversation, "id" | "title"> | null;
  onClose: () => void;
}) {
  const open = !!conversation;
  const id = conversation?.id;
  const [tab, setTab] = useState<"snapshot" | "live">("snapshot");
  const [shares, setShares] = useState<Share[] | null>(null);
  const [audience, setAudience] = useState<Audience>("deployment");
  const [people, setPeople] = useState("");
  const [allowCopy, setAllowCopy] = useState(true);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [members, setMembers] = useState<{ revision: number; members: Member[] } | null>(null);
  const [invite, setInvite] = useState("");
  const [role, setRole] = useState<"viewer" | "editor">("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    if (!id) return;
    get<{ items: Share[] }>(`/api/shares?conversation_id=${enc(id)}`)
      .then((r) => setShares(r.items.filter((s) => s.status === "active")))
      .catch((e) => setError(describeError(e, t.share.loadError)));
    get<{ revision: number; members: Member[] }>(`/api/conversations/${enc(id)}/collaborators`)
      .then(setMembers)
      .catch(() => setMembers(null));
  };
  useEffect(() => {
    setPreview(null);
    setError(null);
    setShares(null);
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };

  const makePreview = () =>
    run(async () => {
      const principals = people.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
      setPreview(
        await post<Preview>("/api/shares/previews", {
          conversation_id: id,
          audience: audience === "named" ? { type: "named", principals } : { type: audience },
          allow_copy: allowCopy,
        }),
      );
    });

  const publish = () =>
    run(async () => {
      if (!preview) return;
      const share = await post<Share>("/api/shares", { preview_id: preview.preview_id, digest: preview.digest },
        { headers: { "Idempotency-Key": crypto.randomUUID() } });
      setPreview(null);
      if (await copyText(linkOf(share))) toast(t.share.copied);
      reload();
    });

  const revoke = (share: Share) =>
    run(async () => {
      await del(`/api/shares/${enc(share.id)}`, { body: { expected_revision: share.revision } });
      toast(t.share.revoked);
      reload();
    });

  const setCollaborators = (next: Member[]) =>
    run(async () => {
      const out = await put<{ revision: number; members: Member[] }>(`/api/conversations/${enc(id!)}/collaborators`, {
        expected_revision: members?.revision ?? 0,
        members: next.map((m) => ({ principal_id: m.principal_id, role: m.role })),
      });
      setMembers(out);
      setInvite("");
    });

  return (
    <Modal open={open} onOpenChange={(o) => !o && !busy && onClose()} title={t.share.title}>
      <div className="hz-tabs mb-3" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "snapshot"} onClick={() => setTab("snapshot")}>
          <Link2 size={14} aria-hidden /> {t.share.snapshot}
        </button>
        <button type="button" role="tab" aria-selected={tab === "live"} onClick={() => setTab("live")}>
          <Users size={14} aria-hidden /> {t.share.live}
        </button>
      </div>
      {error && <Notice tone="error" className="mb-3">{error}</Notice>}
      {tab === "snapshot" ? (
        <div className="space-y-3" data-testid="share-snapshot">
          <ShareExplanation help={t.share.snapshotHelp}>{t.share.snapshotBody}</ShareExplanation>
          {shares === null ? (
            <Spinner size={14} />
          ) : (
            shares.map((share) => (
              <div key={share.id} className="flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-[13px]">
                {/* The full address, so people can see and select what they share. */}
                <input
                  readOnly
                  aria-label={t.share.linkLabel}
                  value={linkOf(share)}
                  onFocus={(e) => e.currentTarget.select()}
                  className="min-w-0 flex-1 truncate border-0 bg-transparent p-0 text-[13px] text-ink outline-none"
                />
                <span className="text-[12px] text-mute">{t.share.audience[share.audience.type]}</span>
                <Button size="sm" variant="ghost" icon={<Copy size={13} aria-hidden />} onClick={() => void copyText(linkOf(share)).then((ok) => ok && toast(t.share.copied))}>
                  {t.share.copy}
                </Button>
                <Button size="sm" variant="ghost" icon={<Link2Off size={13} aria-hidden />} onClick={() => void revoke(share)} disabled={busy}>
                  {t.share.revoke}
                </Button>
              </div>
            ))
          )}
          {!preview ? (
            <div className="space-y-2.5 rounded-lg border border-line p-3">
              <fieldset className="m-0 flex flex-wrap gap-3 border-0 p-0 text-[13px]">
                <legend className="mb-1 text-[12px] font-medium text-mute">{t.share.who}</legend>
                {(["deployment", "named", "public"] as Audience[]).map((a) => (
                  <label key={a} className="flex items-center gap-1.5">
                    <input type="radio" name="audience" checked={audience === a} onChange={() => setAudience(a)} />
                    {t.share.audience[a]}
                  </label>
                ))}
              </fieldset>
              {audience === "named" && (
                <Field label={t.share.people} value={people} onChange={(e) => setPeople(e.target.value)} placeholder="ana@example.org, ben@example.org" />
              )}
              <label className="flex items-center gap-1.5 text-[13px]">
                <input type="checkbox" checked={allowCopy} onChange={(e) => setAllowCopy(e.target.checked)} />
                {t.share.allowCopy}
              </label>
              <Button size="sm" onClick={() => void makePreview()} loading={busy} data-testid="share-preview">
                {t.share.previewButton}
              </Button>
            </div>
          ) : (
            <div className="space-y-2 rounded-lg border border-line p-3 text-[13px]" data-testid="share-preview-result">
              <p className="m-0 text-ink">{t.share.previewSummary(preview.projection.messages.length)}</p>
              {preview.omissions.length > 0 && (
                <p className="m-0 text-mute">{t.share.omitted(preview.omissions.join(", "))}</p>
              )}
              <div className="flex justify-end gap-2">
                <Button size="sm" variant="ghost" onClick={() => setPreview(null)}>
                  {t.cancel}
                </Button>
                <Button size="sm" variant="primary" loading={busy} onClick={() => void publish()} data-testid="share-publish">
                  {t.share.publish}
                </Button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3" data-testid="share-live">
          <ShareExplanation help={t.share.liveHelp}>{t.share.liveBody}</ShareExplanation>
          <ul className="m-0 list-none space-y-1 p-0">
            {(members?.members ?? []).map((m) => (
              <li key={m.principal_id} className="flex items-center gap-2 rounded-lg border border-line px-3 py-1.5 text-[13px]">
                <span className="min-w-0 flex-1 truncate">{m.name || m.email || m.principal_id}</span>
                <span className="text-[12px] text-mute">{t.share.roles[m.role]}</span>
                <button
                  type="button"
                  className="text-mute hover:text-ink"
                  aria-label={t.share.remove}
                  onClick={() => void setCollaborators((members?.members ?? []).filter((x) => x.principal_id !== m.principal_id))}
                >
                  <UserMinus size={14} aria-hidden />
                </button>
              </li>
            ))}
          </ul>
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (invite.trim()) void setCollaborators([...(members?.members ?? []), { principal_id: invite.trim(), role }]);
            }}
          >
            <div className="flex-1">
              <Field label={t.share.addPerson} value={invite} onChange={(e) => setInvite(e.target.value)} placeholder="name@company.com" />
            </div>
            <select className={cx("hz-input !w-auto")} value={role} onChange={(e) => setRole(e.target.value as "viewer" | "editor")} aria-label={t.share.role}>
              <option value="viewer">{t.share.roles.viewer}</option>
              <option value="editor">{t.share.roles.editor}</option>
            </select>
            <Button type="submit" size="sm" loading={busy} disabled={!invite.trim()}>
              {t.share.add}
            </Button>
          </form>
        </div>
      )}
    </Modal>
  );
}

/** A pre-2.0 link's URL (kept for old shares). */
export type { ShareLink };
