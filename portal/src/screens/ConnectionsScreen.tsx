import { useEffect, useState } from "react";
import { Alert, Button, Empty, List, Modal, Space, Tag, Typography, message } from "antd";
import { request } from "../api";
import { useData } from "../hooks/useData";
import { setQuery, useHashQuery } from "../hooks/useRoute";
import { LoadState, PageHeader } from "../components/common";
import { t } from "../app/i18n/en";
import { formatDate } from "../app/lib/format";
import { kindOf, type Kind } from "../app/lib/connections";
import type { Connection } from "../app/lib/types";

const { Text } = Typography;

const TAG: Record<Kind, [string, string]> = {
  connected: [t.connections.connected, "green"],
  attention: [t.connections.needsAttention, "gold"],
  off: [t.connections.notConnected, "default"],
  blocked: [t.connections.notAllowed, "default"],
  shared: [t.connections.shared, "default"],
};

/**
 * A person's own connections, in the Console (both UI modes). Sign-ins return
 * to /portal/connections, which brings their result here as ?connected=,
 * ?connector=&error= or ?disconnected=. What the note says comes from the list,
 * never from the address alone.
 */
export function ConnectionsScreen() {
  const data = useData<Connection[]>("/connections");
  const [q] = useHashQuery();
  // A sign-in's result is a one-time note: kept here, cleared from the address.
  const [back] = useState(() => ({ connected: q.connected, connector: q.connector, error: q.error }));
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    if (Object.keys(q).length) setQuery("/connections", {}, true);
  }, [q]);

  const connect = async (c: Connection) => {
    setBusy(c.connector_id);
    try {
      // No return_to: the sign-in comes back to /portal/connections with
      // ?connected=<id> or ?connector=<id>&error=<code>, which open this tab.
      const out = await request<{ authorize_url?: string }>(
        `/connections/${encodeURIComponent(c.connector_id)}/connect`, {});
      // No sign-in needed: it's connected now, so stay and say so.
      if (out.authorize_url && c.auth_type !== "none") { location.assign(out.authorize_url); return; }
      message.success(t.connections.connectedToast(c.name));
      data.reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "That did not work.");
    }
    setBusy(null);
  };
  const disconnect = (c: Connection) => Modal.confirm({
    title: t.connections.disconnectTitle(c.name),
    content: t.connections.disconnectBody(c.agents ?? []),
    okText: t.connections.disconnect,
    okButtonProps: { danger: true },
    onOk: async () => {
      try {
        await request(`/connections/${encodeURIComponent(c.connector_id)}`, undefined, undefined, "DELETE");
        message.success(t.connections.disconnectedToast(c.name));
        data.reload();
      } catch (e) {
        message.error(e instanceof Error ? e.message : "That did not work.");
      }
    },
  });

  const items = data.data;
  const find = (id?: string) => items?.find((c) => c.connector_id === id);
  const came = back.error ? undefined : find(back.connected);
  // Only for an app in the person's own list: the address alone names nothing.
  const failedName = back.error ? (find(back.connector || back.connected)?.name ?? null) : null;
  return (
    <>
      <PageHeader title={t.connections.title} description={t.connections.intro} />
      {came && kindOf(came) === "connected" && <Alert className="notice" type="success" showIcon closable message={t.connections.connectedToast(came.name)} />}
      {came && !["connected", "shared"].includes(kindOf(came)) && <Alert className="notice" type="error" showIcon closable message={t.connections.notConnectedNote(came.name)} />}
      {failedName && <Alert className="notice" type="error" showIcon closable message={t.connections.failed(failedName, back.error ?? "")} />}
      <div className="panel">
        {!items ? (
          <LoadState error={data.error} retry={data.reload} />
        ) : (
          <List
            dataSource={items}
            locale={{ emptyText: <Empty description={t.connections.empty} /> }}
            renderItem={(c) => {
              const kind = kindOf(c);
              const since = c.connected && c.connected_at ? formatDate(c.connected_at) : "";
              const note = kind === "blocked"
                ? (c.connected ? t.connections.lostAccessHelp : t.connections.notAllowedHelp)
                : [kind === "shared" ? t.connections.sharedHelp : "", c.agents?.length ? t.connections.usedBy(c.agents) : "",
                   since ? t.connections.since(since) : ""].filter(Boolean).join(" · ");
              const canDisconnect = kind === "connected" || kind === "attention" || (kind === "blocked" && c.connected);
              return (
                <List.Item actions={[
                  ...(canDisconnect ? [<Button key="d" size="small" onClick={() => disconnect(c)}
                    aria-label={`${t.connections.disconnect} ${c.name}`}>{t.connections.disconnect}</Button>] : []),
                  ...(kind === "attention" || kind === "off" ? [<Button key="c" size="small" type="primary"
                    loading={busy === c.connector_id} onClick={() => connect(c)}
                    aria-label={`${kind === "off" ? t.connections.connect : t.connections.reconnect} ${c.name}`}>
                    {kind === "off" ? t.connections.connect : t.connections.reconnect}</Button>] : []),
                ]}>
                  <List.Item.Meta
                    title={<Space size={8} wrap><span>{c.name}</span><Tag color={TAG[kind][1]}>{TAG[kind][0]}</Tag></Space>}
                    description={note ? <Text type="secondary">{note}</Text> : null}
                  />
                </List.Item>
              );
            }}
          />
        )}
      </div>
    </>
  );
}
