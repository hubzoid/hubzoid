import { useCallback, useEffect, useState } from "react";
import { Alert, Breadcrumb, Button, Descriptions, Empty, Input, Segmented, Space, Table, Tag, Typography } from "antd";
import { RefreshCw } from "lucide-react";
import { hubRequest } from "../api";
import type { Hub } from "../api";
import { InfoHelp, LoadState } from "../components/common";
import { errorText } from "../hooks/useData";
import { href } from "../hooks/useRoute";

const { Title, Text, Paragraph } = Typography;

// -- types (the /api/hubs/{hub}/intake responses) -------------------------------------
type Item = {
  id: string; channel_id: string; provider: string; subject: string | null; summary: string | null; state: string;
  reason: string | null; work_item_id: string | null; decided_by: string | null; decision_reason: string | null;
  revision: number; created: number; sender: { external_id?: string; display?: string };
  trust: { decision?: string; reason?: string; evidence?: Record<string, unknown> };
};
type Attachment = {
  id: string; filename: string; media_type: string; declared_bytes: number | null; actual_bytes: number | null;
  sha256: string | null; state: string; error_code: string | null;
};
type Full = Item & { text: string; attachments: Attachment[] };
type Page = { items: Item[]; next_cursor: string | null };

const when = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString() : "");
const size = (b: number) => (b < 1024 ? `${b} bytes` : b < 1048576 ? `${Math.round(b / 1024)} KB` : `${(b / 1048576).toFixed(1)} MB`);

const STATE: Record<string, { label: string; color: string }> = {
  received: { label: "Received", color: "processing" },
  quarantined: { label: "Waiting for review", color: "gold" },
  ready: { label: "Starting", color: "processing" },
  dispatched: { label: "Work started", color: "green" },
  rejected: { label: "Rejected", color: "default" },
  failed: { label: "Attachment problem", color: "red" },
};
const REASONS: Record<string, string> = {
  unknown_sender: "The sender is not on the allowlist",
  sender_unverified: "The sender's domain did not sign the message",
  dkim_unavailable: "The signature could not be checked (DNS did not answer)",
  manual_review: "This channel reviews every message",
  attachment_failed: "An attachment is not allowed or could not be stored",
  from_not_single: "The message has more than one sender",
  mime_too_complex: "The message structure is too complex to read safely",
  message_too_large: "The message is larger than 30 MiB",
  rejected_by_person: "Rejected by a person",
};

function useLoad<T>(url: string) {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  const [n, setN] = useState(0);
  const reload = useCallback(() => setN((x) => x + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    hubRequest<T>(url, "GET", undefined, controller.signal)
      .then((data) => !controller.signal.aborted && setState({ data }))
      .catch((e) => !controller.signal.aborted && setState({ error: errorText(e) }));
    return () => controller.abort();
  }, [url, n]);
  return { ...state, reload };
}

const base = (hub: Hub) => `/api/hubs/${encodeURIComponent(hub.key)}/intake`;
const intakeHref = (hub: Hub, id?: string) =>
  href(`/agents/${encodeURIComponent(hub.key)}/intake${id ? `/${encodeURIComponent(id)}` : ""}`);

function stateTag(state: string) {
  const s = STATE[state] ?? { label: state, color: "default" };
  return <Tag color={s.color}>{s.label}</Tag>;
}

/** Agents → agent → Intake. `rest` is [item id]. */
export function AgentIntake({ hub, rest }: { hub: Hub; rest: string[] }) {
  return rest[0] ? <ItemView hub={hub} id={rest[0]} /> : <ItemList hub={hub} />;
}

function ItemList({ hub }: { hub: Hub }) {
  const [state, setState] = useState<string>("quarantined");
  const list = useLoad<Page>(`${base(hub)}${state === "all" ? "" : `?state=${state}`}`);
  return (
    <div className="panel">
      <Title level={2}>
        Intake
        <InfoHelp text="Email and WhatsApp messages from outside the organization, before any work starts. The same list as hubzoid intake list." />
      </Title>
      <Paragraph type="secondary">
        A message from an allowed, verified sender starts its workflow. Everything else waits here. Nothing is sent to a
        sender and no model reads a message until it is released.
      </Paragraph>
      <Space style={{ marginBottom: 16 }} wrap>
        <Segmented
          value={state}
          onChange={(v) => setState(String(v))}
          options={[
            { label: "Waiting for review", value: "quarantined" },
            { label: "Attachment problem", value: "failed" },
            { label: "Work started", value: "dispatched" },
            { label: "Rejected", value: "rejected" },
            { label: "All", value: "all" },
          ]}
        />
        <Button icon={<RefreshCw size={14} />} onClick={list.reload}>Refresh</Button>
      </Space>
      {!list.data ? (
        <LoadState error={list.error} retry={list.reload} />
      ) : list.data.items.length === 0 ? (
        <Empty description={state === "quarantined" ? "Nothing is waiting for review." : "No messages here."} />
      ) : (
        <Table<Item>
          rowKey="id"
          dataSource={list.data.items}
          pagination={false}
          scroll={{ x: true }}
          columns={[
            { title: "From", render: (_, r) => <a href={intakeHref(hub, r.id)}>{r.sender.external_id || "Unknown"}</a> },
            { title: "Subject", render: (_, r) => <Text ellipsis style={{ maxWidth: 320 }}>{r.subject || r.summary || "(no subject)"}</Text> },
            { title: "Channel", dataIndex: "channel_id" },
            { title: "State", render: (_, r) => stateTag(r.state) },
            { title: "Why", render: (_, r) => (r.reason ? REASONS[r.reason] ?? r.reason : "") },
            { title: "Received", render: (_, r) => when(r.created) },
          ]}
        />
      )}
    </div>
  );
}

function ItemView({ hub, id }: { hub: Hub; id: string }) {
  const item = useLoad<Full>(`${base(hub)}/${encodeURIComponent(id)}`);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const decide = async (action: "release" | "reject" | "retry") => {
    if (!item.data) return;
    setBusy(action);
    setError(null);
    try {
      const out = await hubRequest<Item>(`${base(hub)}/${encodeURIComponent(id)}/${action}`, "POST", {
        expected_revision: item.data.revision, reason,
      });
      setDone(action === "release" ? `Released. Work item ${out.work_item_id?.slice(0, 8) ?? ""} started.`
        : action === "reject" ? "Rejected. Nothing was sent to the sender." : `Retried: now ${STATE[out.state]?.label ?? out.state}.`);
      item.reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  if (!item.data) return <LoadState error={item.error} retry={item.reload} />;
  const d = item.data;
  const open = ["quarantined", "failed", "received", "ready"].includes(d.state);
  return (
    <div className="panel">
      <Breadcrumb items={[{ title: <a href={intakeHref(hub)}>Intake</a> }, { title: d.subject || "(no subject)" }]} />
      <Title level={2} style={{ marginTop: 12 }}>{d.subject || "(no subject)"}</Title>
      {done && <Alert type="success" showIcon title={done} style={{ marginBottom: 16 }} />}
      {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} />}
      <Descriptions column={1} bordered size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="From">{d.sender.display ? `${d.sender.display} <${d.sender.external_id}>` : d.sender.external_id}</Descriptions.Item>
        <Descriptions.Item label="State">{stateTag(d.state)} {d.reason ? REASONS[d.reason] ?? d.reason : ""}</Descriptions.Item>
        <Descriptions.Item label="Sender check">
          {d.trust.decision === "accepted" ? "Allowed sender with a valid signature from their domain." :
            REASONS[d.trust.reason ?? ""] ?? d.trust.reason ?? "Not checked"}
        </Descriptions.Item>
        <Descriptions.Item label="Channel">{d.channel_id}</Descriptions.Item>
        <Descriptions.Item label="Received">{when(d.created)}</Descriptions.Item>
        {d.decided_by && <Descriptions.Item label="Decided by">{d.decided_by}: {d.decision_reason}</Descriptions.Item>}
        {d.work_item_id && (
          <Descriptions.Item label="Work item">
            <a href={href(`/work?item=${encodeURIComponent(d.work_item_id)}`)}>{d.work_item_id.slice(0, 8)}</a>
          </Descriptions.Item>
        )}
      </Descriptions>
      <Title level={3}>Message <Text type="secondary" style={{ fontSize: 14 }}>(untrusted text from outside)</Text></Title>
      <pre className="hz-untrusted" style={{ whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto", padding: 12,
        border: "1px solid var(--hz-border, #d9d9d9)", borderRadius: 6 }}>{d.text}</pre>
      {d.attachments.length > 0 && (
        <>
          <Title level={3}>Attachments</Title>
          <Table<Attachment>
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={d.attachments}
            columns={[
              { title: "File", render: (_, a) => a.state === "available"
                ? <a href={`${base(hub)}/${encodeURIComponent(id)}/attachments/${encodeURIComponent(a.id)}`}>{a.filename}</a>
                : a.filename },
              { title: "Type", dataIndex: "media_type" },
              { title: "Size", render: (_, a) => size(a.actual_bytes ?? a.declared_bytes ?? 0) },
              { title: "State", render: (_, a) => <Tag color={a.state === "available" ? "green" : "red"}>{a.state}{a.error_code ? `: ${a.error_code}` : ""}</Tag> },
            ]}
          />
        </>
      )}
      {open && (
        <div style={{ marginTop: 24 }}>
          <Title level={3}>Decide</Title>
          <Paragraph type="secondary">
            Releasing starts the work for this message, with you as the person who asked. It approves nothing the work
            later wants to send: each reply waits for your approval when the hub requires it.
          </Paragraph>
          <Input.TextArea
            aria-label="Reason"
            placeholder="Reason (required to release)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            maxLength={500}
            style={{ marginBottom: 12, maxWidth: 560 }}
          />
          <Space wrap>
            {d.state === "quarantined" && (
              <Button type="primary" loading={busy === "release"} disabled={!reason.trim()} onClick={() => decide("release")}>
                Release and start work
              </Button>
            )}
            {d.state === "failed" && (
              <Button loading={busy === "retry"} onClick={() => decide("retry")}>Fetch attachments again</Button>
            )}
            <Button danger loading={busy === "reject"} onClick={() => decide("reject")}>Reject</Button>
          </Space>
        </div>
      )}
    </div>
  );
}
