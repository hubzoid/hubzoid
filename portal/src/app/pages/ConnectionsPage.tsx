// Personal connections (contract 6.6): connect an account through its OAuth
// page, see its status, disconnect it.
import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Plug } from "lucide-react";
import { t } from "../i18n/en";
import { del, enc, get, post } from "../lib/api";
import { describeError } from "../lib/errors";
import { formatDate } from "../lib/format";
import { navigate } from "../lib/router";
import type { Connection } from "../lib/types";
import { toast } from "../components/toast";
import { Button, ConfirmDialog, Notice, Spinner, cx } from "../components/ui";
import { SettingsFrame } from "./SettingsFrame";

type Kind = "connected" | "attention" | "off" | "blocked";

function kindOf(c: Connection): Kind {
  if (c.allowed === false) return "blocked";
  const status = (c.status || "").toLowerCase();
  if (c.connected && ["", "ok", "active", "connected", "valid"].includes(status)) return "connected";
  if (c.connected || ["expired", "error", "revoked", "invalid", "needs_reauth", "reauth", "failed"].includes(status))
    return "attention";
  return "off";
}

const badge: Record<Kind, { text: string; className: string }> = {
  connected: { text: t.connections.connected, className: "bg-success-soft text-success" },
  attention: { text: t.connections.needsAttention, className: "bg-warning-soft text-warning" },
  off: { text: t.connections.notConnected, className: "bg-sunken text-mute" },
  blocked: { text: t.connections.notAllowed, className: "bg-sunken text-mute" },
};

export default function ConnectionsPage({ connected }: { connected: string | null }) {
  const [items, setItems] = useState<Connection[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Connection | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [justConnected, setJustConnected] = useState<string | null>(connected);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await get<Connection[] | { connections?: Connection[]; items?: Connection[] }>("/api/connections");
      setItems(Array.isArray(data) ? data : data?.connections ?? data?.items ?? []);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // ?connected=<id> is a one-time success note from the OAuth callback.
  useEffect(() => {
    if (connected) navigate("/account/connections", { replace: true });
  }, [connected]);

  const connect = async (c: Connection) => {
    setBusy(c.connector_id);
    try {
      const res = await post<{ authorize_url?: string }>(`/api/connections/${enc(c.connector_id)}/connect`);
      if (res?.authorize_url) {
        location.assign(res.authorize_url);
        return;
      }
      await load();
      setBusy(null);
    } catch (e) {
      toast(describeError(e), "error");
      setBusy(null);
    }
  };

  const disconnect = async () => {
    if (!confirm) return;
    setBusy(confirm.connector_id);
    setConfirmError(null);
    try {
      await del(`/api/connections/${enc(confirm.connector_id)}`);
      toast(t.connections.disconnectedToast(confirm.name));
      setConfirm(null);
      await load();
    } catch (e) {
      setConfirmError(describeError(e));
    } finally {
      setBusy(null);
    }
  };

  const connectedName = justConnected ? items?.find((c) => c.connector_id === justConnected)?.name ?? justConnected : null;

  return (
    <SettingsFrame title={t.connections.title} current="/account/connections">
      <p className="m-0 mb-6 max-w-prose text-[14.5px] leading-relaxed text-mute">{t.connections.intro}</p>
      {connectedName && (
        <Notice tone="success" className="mb-6" onDismiss={() => setJustConnected(null)}>
          {t.connections.connectedToast(connectedName)}
        </Notice>
      )}
      {error ? (
        <Notice tone="error" action={<Button size="sm" onClick={() => void load()}>{t.retry}</Button>}>
          {t.connections.loadError} {describeError(error)}
        </Notice>
      ) : items === null ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : items.length === 0 ? (
        <div className="hz-card flex flex-col items-center px-6 py-10 text-center">
          <Plug size={24} aria-hidden className="text-mute" />
          <p className="m-0 mt-3 max-w-sm text-[14.5px] leading-relaxed text-mute">{t.connections.empty}</p>
        </div>
      ) : (
        <ul className="hz-card m-0 list-none divide-y divide-line-soft p-0" aria-label={t.connections.title}>
          {items.map((c) => {
            const kind = kindOf(c);
            const since = c.connected && c.connected_at ? formatDate(c.connected_at) : "";
            return (
              <li key={c.connector_id} className="flex flex-wrap items-center gap-x-4 gap-y-3 px-4 py-4 sm:px-5" data-testid={`connection-${c.connector_id}`}>
                <span className="flex h-10 w-10 flex-none items-center justify-center rounded-xl border border-line bg-sunken font-mono text-sm font-semibold text-ink">
                  {c.name.slice(0, 1).toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[15px] font-semibold text-ink">{c.name}</span>
                    <span className={cx("hz-badge", badge[kind].className)}>
                      {kind === "connected" && <CheckCircle2 size={12} aria-hidden />}
                      {badge[kind].text}
                    </span>
                  </div>
                  <p className="m-0 mt-0.5 text-[13px] text-mute">
                    {kind === "blocked" ? t.connections.notAllowedHelp : since ? t.connections.since(since) : " "}
                  </p>
                </div>
                <div className="flex flex-none gap-2">
                  {kind === "connected" && (
                    <Button
                      size="sm"
                      onClick={() => {
                        setConfirmError(null);
                        setConfirm(c);
                      }}
                      aria-label={`${t.connections.disconnect} ${c.name}`}
                    >
                      {t.connections.disconnect}
                    </Button>
                  )}
                  {kind === "attention" && (
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setConfirmError(null);
                          setConfirm(c);
                        }}
                        aria-label={`${t.connections.disconnect} ${c.name}`}
                      >
                        {t.connections.disconnect}
                      </Button>
                      <Button size="sm" variant="primary" loading={busy === c.connector_id} onClick={() => void connect(c)} aria-label={`${t.connections.reconnect} ${c.name}`}>
                        {t.connections.reconnect}
                      </Button>
                    </>
                  )}
                  {kind === "off" && (
                    <Button size="sm" variant="primary" loading={busy === c.connector_id} onClick={() => void connect(c)} aria-label={`${t.connections.connect} ${c.name}`}>
                      {busy === c.connector_id ? t.connections.connecting : t.connections.connect}
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <ConfirmDialog
        open={!!confirm}
        onOpenChange={(o) => !o && !busy && setConfirm(null)}
        title={t.connections.disconnectTitle(confirm?.name ?? "")}
        body={t.connections.disconnectBody}
        confirmLabel={t.connections.disconnect}
        onConfirm={() => void disconnect()}
        busy={!!busy}
        error={confirmError}
      />
    </SettingsFrame>
  );
}
