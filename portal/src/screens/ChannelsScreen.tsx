import { useCallback, useEffect, useState } from "react";
import { useAgentEditing } from "../app/lib/editing";
import {
  Alert, Button, Descriptions, Empty, Form, Input, Popconfirm, Select, Space, Switch, Table, Tabs, Tag,
  Tooltip, Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import { hubRequest } from "../api";
import type { Hub } from "../api";
import { InfoHelp, LoadState } from "../components/common";
import { errorText } from "../hooks/useData";
import { href, useHashQuery } from "../hooks/useRoute";

const { Title, Text, Paragraph } = Typography;

// -- types (the /api/hubs/{hub}/channels responses) ---------------------------------
type Connection = {
  id: string; provider: string; status: string; revision: number; target: string; threads: number;
  last_delivery_at: number | null; backlog: number; quarantined: number; denied: number;
  uncertain_replies: number | null; updated: number;
};
type ChannelList = { items: Connection[]; not_applied: { id: string; file: string; status: string }[] };
type Exported = {
  id: string; provider: string; revision: number; status: string; config: Record<string, unknown>;
  digest: string; file: string; file_matches: boolean | null; applied_by: string; updated: number;
};
type Check = { id: string; status: "ok" | "warn" | "fail" | "skip"; message: string; code: string; recovery: string };
type Doctor = { checks: Check[]; ready: boolean; webhook_url: string | null };
type Delivery = {
  id: string; delivery_id: string; kind: string; status: string; reason: string | null; thread_id: string | null;
  actor: { external_id?: string; display?: string }; route: Record<string, unknown>; created: number;
};
type Reply = {
  id: string; kind: string; state: string; thread_id: string | null; error: string | null; attempts: number;
  delivery?: { status: string; at: number; error?: string | null } | null;
  created: number;
};
type Thread = {
  id: string; thread_key: string; origin: { provider: string; url: string; coordinates: Record<string, unknown> };
  subscribed: boolean; conversations: Record<string, string>; work_item_id: string | null; requester: string;
  revision: number; updated: number;
};
type ThreadDetail = Thread & { deliveries: Delivery[]; replies: Reply[]; work_item?: { id: string; state: string; workflow: string } };
type Link = { id: string; workspace_id: string; external_id: string; account: string; display: string; created: number };
type FieldError = { field: string; line: number | null; message: string; code: string };

const when = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString() : "Never");

const STATUS: Record<string, { label: string; color: string }> = {
  enabled: { label: "Enabled", color: "green" },
  disabled: { label: "Paused", color: "default" },
  not_applied: { label: "Not applied", color: "orange" },
};
const DELIVERY: Record<string, { label: string; color: string }> = {
  accepted: { label: "Waiting to route", color: "processing" },
  routing: { label: "Routing", color: "processing" },
  waiting: { label: "Queued in thread", color: "gold" },
  routed: { label: "Started work", color: "green" },
  duplicate: { label: "Repeat, ignored", color: "default" },
  ignored: { label: "Ignored", color: "default" },
  disabled: { label: "Paused, no work", color: "default" },
  denied: { label: "Refused", color: "orange" },
  quarantined: { label: "Quarantined", color: "red" },
  failed: { label: "Failed", color: "red" },
};
const REPLY: Record<string, { label: string; color: string }> = {
  pending: { label: "Ready to send", color: "default" },
  sending: { label: "Sending", color: "processing" },
  succeeded: { label: "Posted", color: "green" },
  ambiguous: { label: "Outcome unknown", color: "red" },
  resolved_done: { label: "Posted (confirmed)", color: "green" },
  abandoned: { label: "Given up", color: "default" },
  failed_before_send: { label: "Not sent", color: "orange" },
  rejected: { label: "Refused by provider", color: "red" },
  awaiting_approval: { label: "Waiting for approval", color: "gold" },
};
const REASONS: Record<string, string> = {
  identity_unmapped: "No account link for this person",
  account_inactive: "Hubzoid account not active",
  hub_access_denied: "No access to this agent",
  group_not_allowed: "Not in an allowed group",
  empty_request: "Empty message",
  not_addressed: "Not addressed to the agent",
  connection_disabled: "Channel paused",
  queue_full: "Too many waiting in this thread",
  backlog_full: "Too much waiting on this channel",
  delivery_conflict: "Same delivery id, different content",
  same_event: "Same message already received",
  bot_author: "Sent by a bot",
  thread_busy: "Someone else's request is running",
};

function tag(map: Record<string, { label: string; color: string }>, key: string) {
  const m = map[key] ?? { label: key, color: "default" };
  return <Tag color={m.color}>{m.label}</Tag>;
}

function base(hub: Hub) {
  return `/api/hubs/${encodeURIComponent(hub.key)}/channels`;
}

function useLoad<T>(url: string | null) {
  const [state, setState] = useState<{ data?: T; error?: string }>({});
  const [n, setN] = useState(0);
  const reload = useCallback(() => setN((x) => x + 1), []);
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    hubRequest<T>(url, "GET", undefined, controller.signal)
      .then((data) => !controller.signal.aborted && setState({ data }))
      .catch((e) => !controller.signal.aborted && setState({ error: errorText(e) }));
    return () => controller.abort();
  }, [url, n]);
  return { ...state, reload };
}

/** Agents → agent → Channels. `rest` is [connection id, "threads", thread id]. */
export function AgentChannels({ hub, rest }: { hub: Hub; rest: string[] }) {
  if (rest[0] && rest[1] === "threads" && rest[2]) return <ThreadView hub={hub} cid={rest[0]} tid={rest[2]} />;
  if (rest[0]) return <ConnectionView hub={hub} cid={rest[0]} tab={rest[1]} />;
  return <ChannelList hub={hub} />;
}

const channelHref = (hub: Hub, ...parts: string[]) =>
  href(`/agents/${encodeURIComponent(hub.key)}/channels/${parts.map(encodeURIComponent).join("/")}`);

// -- the list ------------------------------------------------------------------------
function ChannelList({ hub }: { hub: Hub }) {
  const list = useLoad<ChannelList>(base(hub));
  if (!list.data) return <LoadState error={list.error} retry={list.reload} />;
  const { items, not_applied } = list.data;
  return (
    <div className="panel">
      <Title level={2}>
        Channels
        <InfoHelp text="Slack, GitHub, Linear, email and WhatsApp threads this agent answers in. The same list as hubzoid channels list." />
      </Title>
      <Paragraph type="secondary">
        Each channel is a file, <Text code>channels/&lt;id&gt;.yaml</Text>, in this agent's folder. Replies always go back to
        the thread a request came from. Credentials are secret references, never values.
      </Paragraph>
      {not_applied.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title={`${not_applied.length} channel file(s) are not applied yet`}
          description={not_applied.map((f) => f.file).join(", ") + ". Paste one below and apply it, or run hubzoid channels apply."}
        />
      )}
      {items.length === 0 ? (
        <Empty description="No channels yet. Add a channel file below, or ask your assistant to set one up with hubzoid channels apply." />
      ) : (
        <Table<Connection>
          rowKey="id"
          dataSource={items}
          pagination={false}
          columns={[
            { title: "Channel", dataIndex: "id", render: (id: string) => <a href={channelHref(hub, id)}>{id}</a> },
            { title: "Provider", dataIndex: "provider" },
            { title: "Status", dataIndex: "status", render: (s: string) => tag(STATUS, s) },
            { title: "Revision", dataIndex: "revision" },
            { title: "Answers with", dataIndex: "target" },
            { title: "Threads", dataIndex: "threads" },
            { title: "Waiting", dataIndex: "backlog" },
            {
              title: "Uncertain replies", dataIndex: "uncertain_replies",
              render: (n: number | null) => (n ? <Tag color="red">{n}</Tag> : n ?? "?"),
            },
            { title: "Last delivery", dataIndex: "last_delivery_at", render: when },
          ]}
        />
      )}
      <ConfigEditor hub={hub} onApplied={list.reload} />
    </div>
  );
}

// -- add or change a connection ---------------------------------------------------------
function ConfigEditor({ hub, onApplied, initial, cid, revision }: {
  hub: Hub; onApplied: () => void; initial?: string; cid?: string; revision?: number;
}) {
  const [text, setText] = useState(initial ?? "");
  const [result, setResult] = useState<{ type: "success" | "error" | "info"; text: string; fields?: FieldError[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const files = useAgentEditing();
  useEffect(() => { if (initial !== undefined) setText(initial); }, [initial]);

  const validate = async () => {
    setBusy(true);
    try {
      const out = await hubRequest<{ valid: boolean; errors: FieldError[]; normalized?: { id: string } }>(
        `${base(hub)}/validate`, "POST", { text });
      setResult(out.valid
        ? { type: "info", text: `Valid. Nothing was sent to the provider (${out.normalized?.id}).` }
        : { type: "error", text: "Not valid yet.", fields: out.errors });
    } catch (e) {
      setResult({ type: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    setBusy(true);
    try {
      const idMatch = /^id:\s*([a-z0-9][a-z0-9_-]*)\s*$/m.exec(text);
      const id = cid ?? idMatch?.[1];
      if (!id) throw new Error("The file needs an id line, for example id: support-slack.");
      const out = await hubRequest<{ id: string; revision: number; status: string }>(
        `${base(hub)}/${encodeURIComponent(id)}/config`, "PUT",
        { text, expected_revision: revision ?? "absent" });
      setResult({ type: "success", text: `${out.id} applied: revision ${out.revision}, ${out.status === "enabled" ? "enabled" : "paused"}.` });
      onApplied();
    } catch (e: unknown) {
      const fields = (e as { data?: { fields?: FieldError[] } }).data?.fields;
      setResult({ type: "error", text: errorText(e), fields: Array.isArray(fields) ? fields : undefined });
    } finally {
      setBusy(false);
    }
  };

  if (!files.editable)
    // Off (the default): the file is read here and changed where it lives.
    return (
      <div style={{ marginTop: 24 }}>
        <Paragraph type="secondary">
          {cid ? <>This channel is <Text code>channels/{cid}.yaml</Text>. </> : <>A channel is a file, <Text code>channels/&lt;id&gt;.yaml</Text>. </>}
          {files.message}
        </Paragraph>
        {cid && initial && <pre className="hz-tech">{initial}</pre>}
      </div>
    );
  return (
    <div style={{ marginTop: 24 }}>
      <Title level={4}>{cid ? "Change this channel" : "Add a channel"}</Title>
      <Paragraph type="secondary">
        The same file your assistant writes. Applying saves it as <Text code>channels/&lt;id&gt;.yaml</Text> and makes it
        active. {cid ? "It must still match the revision shown above." : "A new channel starts paused until enabled: true."}
      </Paragraph>
      <Input.TextArea
        aria-label="Channel file"
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoSize={{ minRows: 8, maxRows: 24 }}
        style={{ fontFamily: "var(--hz-mono, monospace)" }}
        placeholder={"version: 1\nid: support-slack\nprovider: slack\nenabled: false\nagent: main\nallowed_groups: [support]\nprovider_config:\n  ..."}
      />
      <Space style={{ marginTop: 8 }}>
        <Button onClick={validate} loading={busy} disabled={!text.trim()}>Validate</Button>
        <Button type="primary" onClick={apply} loading={busy} disabled={!text.trim()}>Apply</Button>
      </Space>
      {result && (
        <Alert
          style={{ marginTop: 12 }}
          type={result.type}
          showIcon
          title={result.text}
          description={result.fields?.length ? (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {result.fields.map((f, i) => (
                <li key={i}><Text code>{f.field}</Text>{f.line ? ` (line ${f.line})` : ""}: {f.message}</li>
              ))}
            </ul>
          ) : undefined}
        />
      )}
    </div>
  );
}

// -- one connection --------------------------------------------------------------------
function toYaml(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) return value.length ? value.map((v) => `\n${pad}- ${toYaml(v, indent + 1).trimStart()}`).join("") : " []";
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => {
        const inner = toYaml(v, indent + 1);
        return `\n${pad}${k}:${typeof v === "object" && v !== null && !(Array.isArray(v) && !v.length) ? inner : " " + inner.trim()}`;
      }).join("");
  }
  return typeof value === "string" && /[:#{}[\],&*!|>'"%@`]|^\s|\s$/.test(value) ? JSON.stringify(value) : String(value);
}

function ConnectionView({ hub, cid, tab }: { hub: Hub; cid: string; tab?: string }) {
  const url = `${base(hub)}/${encodeURIComponent(cid)}`;
  const exp = useLoad<Exported>(`${url}/config`);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [toggling, setToggling] = useState(false);
  const files = useAgentEditing();
  // Back from GitHub's App setup (HUB-140, session B): say it worked.
  const [q] = useHashQuery();
  const active = tab && ["overview", "threads", "deliveries", "replies", "links", "settings"].includes(tab) ? tab : "overview";
  if (!exp.data) return <LoadState error={exp.error} retry={exp.reload} />;
  const c = exp.data;
  const enabled = c.status === "enabled";

  const toggle = async () => {
    setToggling(true);
    try {
      const out = await hubRequest<{ revision: number; status: string }>(`${url}/enabled`, "POST",
        { enabled: !enabled, expected_revision: c.revision });
      setNotice({ type: "success", text: out.status === "enabled"
        ? `Resumed (revision ${out.revision}). New requests start work again.`
        : `Paused (revision ${out.revision}). Deliveries are recorded but start no work. Work already running finishes and replies.` });
      exp.reload();
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    } finally {
      setToggling(false);
    }
  };

  return (
    <div className="panel">
      <Space align="center" style={{ marginBottom: 8 }} wrap>
        <a href={href(`/agents/${encodeURIComponent(hub.key)}/channels`)}>Channels</a>
        <Text type="secondary">/</Text>
        <Title level={2} style={{ margin: 0 }}>{c.id}</Title>
        {tag(STATUS, c.status)}
        <Text type="secondary">{c.provider} · revision {c.revision}</Text>
        {files.editable && <Popconfirm
          title={enabled ? "Pause this channel?" : "Resume this channel?"}
          description={enabled ? "New requests are recorded but start no work. Running work still replies." : "Run the checks first. New requests will start work."}
          onConfirm={toggle}
        >
          <Button loading={toggling}>{enabled ? "Pause" : "Resume"}</Button>
        </Popconfirm>}
      </Space>
      {notice && <Alert style={{ marginBottom: 12 }} type={notice.type} showIcon closable title={notice.text} onClose={() => setNotice(null)} />}
      {q.connected === "github" && (
        <Alert style={{ marginBottom: 12 }} type="success" showIcon title="Connected to GitHub."
          description="Run the checks on the Overview tab, then give people Start work from this channel in Access." />
      )}
      {c.file_matches === false && (
        <Alert style={{ marginBottom: 12 }} type="warning" showIcon
          title={`${c.file} was edited after revision ${c.revision} was applied.`}
          description="The running channel uses the applied revision. Apply the file to make the edit active." />
      )}
      <Tabs
        activeKey={active}
        onChange={(key) => { location.hash = `/agents/${encodeURIComponent(hub.key)}/channels/${encodeURIComponent(cid)}/${key}`; }}
        items={[
          { key: "overview", label: "Overview", children: <Overview url={url} c={c} /> },
          { key: "threads", label: "Threads", children: <Threads hub={hub} url={url} cid={cid} /> },
          { key: "deliveries", label: "Deliveries", children: <Deliveries url={url} /> },
          { key: "replies", label: "Replies", children: <Replies url={url} /> },
          { key: "links", label: "Account links", children: <Links url={url} /> },
          {
            key: "settings", label: "File",
            children: <ConfigEditor hub={hub} cid={cid} revision={c.revision} onApplied={exp.reload}
              initial={toYaml({ version: 1, ...c.config }).trimStart() + "\n"} />,
          },
        ]}
      />
    </div>
  );
}

function Overview({ url, c }: { url: string; c: Exported }) {
  const [doc, setDoc] = useState<{ data?: Doctor; error?: string; busy?: boolean }>({});
  const [actor, setActor] = useState("");
  const run = async () => {
    setDoc({ busy: true });
    try {
      setDoc({ data: await hubRequest<Doctor>(`${url}/doctor`, "POST", actor ? { actor } : {}) });
    } catch (e) {
      setDoc({ error: errorText(e) });
    }
  };
  const cfg = c.config as Record<string, unknown>;
  const colors: Record<string, string> = { ok: "green", warn: "gold", fail: "red", skip: "default" };
  return (
    <>
      <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="Answers with">{String(cfg.agent ? `agent ${cfg.agent}` : `workflow ${cfg.workflow}`)}</Descriptions.Item>
        <Descriptions.Item label="Acts as">{String(cfg.run_as)}</Descriptions.Item>
        <Descriptions.Item label="Who may ask">{(cfg.allowed_groups as string[] | undefined)?.join(", ") || "No groups (workflow and intake rules only)"}</Descriptions.Item>
        <Descriptions.Item label="Follow-ups">{cfg.subscriptions === "explicit" ? "Need a new mention" : "Continue the thread's work"}</Descriptions.Item>
        <Descriptions.Item label="File">{c.file}</Descriptions.Item>
        <Descriptions.Item label="Applied by">{c.applied_by || "?"} · {when(c.updated)}</Descriptions.Item>
      </Descriptions>
      <ProviderPanel url={url} provider={c.provider} />
      <Title level={4}>Checks</Title>
      <Paragraph type="secondary">Read-only. Nothing is posted and no provider setting changes.</Paragraph>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input aria-label="Provider user id to check" placeholder="Optional: a provider user id to check" value={actor}
          onChange={(e) => setActor(e.target.value)} style={{ width: 300 }} />
        <Button type="primary" icon={<RefreshCw size={14} />} onClick={run} loading={doc.busy}>Run checks</Button>
      </Space>
      {doc.error && <Alert type="error" showIcon title={doc.error} />}
      {doc.data && (
        <>
          <Alert style={{ marginBottom: 12 }} type={doc.data.ready ? "success" : "warning"} showIcon
            title={doc.data.ready ? "Ready." : "Not ready yet. Fix the items marked fail."}
            description={doc.data.webhook_url ? <>Provider delivery URL: <Text code copyable>{doc.data.webhook_url}</Text></> : undefined} />
          <Table<Check>
            rowKey="id" size="small" pagination={false} dataSource={doc.data.checks}
            columns={[
              { title: "Check", dataIndex: "id", width: 200 },
              { title: "Result", dataIndex: "status", width: 90, render: (s: string) => <Tag color={colors[s]}>{s}</Tag> },
              { title: "What it found", dataIndex: "message" },
              { title: "What to do", dataIndex: "recovery", render: (r: string) => r ? <Text type="secondary">{r}</Text> : null },
            ]}
          />
        </>
      )}
    </>
  );
}

function Threads({ hub, url, cid }: { hub: Hub; url: string; cid: string }) {
  const data = useLoad<{ items: Thread[] }>(`${url}/threads`);
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  if (!data.data.items.length) return <Empty description="No threads yet. A mention or message in the provider starts one." />;
  return (
    <Table<Thread>
      rowKey="id" size="small" dataSource={data.data.items} pagination={{ pageSize: 25 }}
      columns={[
        { title: "Thread", dataIndex: "thread_key", render: (k: string, t) => <a href={channelHref(hub, cid, "threads", t.id)}>{k}</a> },
        { title: "Asked by", dataIndex: "requester" },
        { title: "Follow-ups", dataIndex: "subscribed", render: (s: boolean) => (s ? "Continue the work" : "Need a mention") },
        { title: "Work", render: (_: unknown, t) => t.work_item_id ? `Work item ${t.work_item_id.slice(0, 8)}` : `${Object.keys(t.conversations).length} conversation(s)` },
        { title: "Last activity", dataIndex: "updated", render: when },
      ]}
    />
  );
}

function Deliveries({ url }: { url: string }) {
  const [status, setStatus] = useState<string | undefined>();
  const data = useLoad<{ items: Delivery[] }>(`${url}/deliveries${status ? `?status=${status}` : ""}`);
  const [notice, setNotice] = useState<string | null>(null);
  const redrive = async (d: Delivery) => {
    try {
      await hubRequest(`${url}/deliveries/${encodeURIComponent(d.id)}/redrive`, "POST", {});
      setNotice(`Delivery ${d.id.slice(0, 8)} will be routed again with today's permissions.`);
      data.reload();
    } catch (e) {
      setNotice(errorText(e));
    }
  };
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <Select allowClear placeholder="Any outcome" style={{ width: 220 }} value={status} onChange={setStatus}
          aria-label="Filter by outcome"
          options={Object.entries(DELIVERY).map(([value, v]) => ({ value, label: v.label }))} />
        <Button onClick={data.reload} icon={<RefreshCw size={14} />}>Refresh</Button>
      </Space>
      {notice && <Alert style={{ marginBottom: 12 }} type="info" showIcon closable title={notice} onClose={() => setNotice(null)} />}
      {!data.data ? <LoadState error={data.error} retry={data.reload} /> : !data.data.items.length ? (
        <Empty description="No deliveries yet. doctor reports webhook_delivery: unverified until the provider sends one." />
      ) : (
        <Table<Delivery>
          rowKey="id" size="small" dataSource={data.data.items} pagination={{ pageSize: 25 }}
          columns={[
            { title: "Received", dataIndex: "created", render: when },
            { title: "From", render: (_: unknown, d) => d.actor?.display || d.actor?.external_id || "?" },
            { title: "Kind", dataIndex: "kind" },
            { title: "Outcome", dataIndex: "status", render: (s: string) => tag(DELIVERY, s) },
            { title: "Why", dataIndex: "reason", render: (r: string | null) => (r ? REASONS[r] ?? r : null) },
            {
              title: "", render: (_: unknown, d) => ["failed", "denied", "disabled"].includes(d.status) ? (
                <Tooltip title="Route it again under its original identity, with today's permissions. Never new content.">
                  <Button size="small" onClick={() => redrive(d)}>Redrive</Button>
                </Tooltip>
              ) : null,
            },
          ]}
        />
      )}
    </>
  );
}

function Replies({ url }: { url: string }) {
  const data = useLoad<{ items: Reply[] }>(`${url}/outbox`);
  const [notice, setNotice] = useState<{ type: "success" | "error" | "info"; text: string } | null>(null);
  const decide = async (r: Reply, decision: string, extra: Record<string, unknown> = {}) => {
    try {
      const out = await hubRequest<{ state: string; found?: boolean | null; message?: string }>(
        `${url}/outbox/${encodeURIComponent(r.id)}/resolve`, "POST", { decision, ...extra });
      setNotice({
        type: "info",
        text: decision === "check"
          ? (out.found ? "Found in the thread: recorded as posted." : out.message ?? "Not found. It stays uncertain.")
          : `Recorded: ${decision}. The reply is now ${REPLY[out.state]?.label ?? out.state}.`,
      });
      data.reload();
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
  };
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  return (
    <>
      <Paragraph type="secondary">
        Every reply is recorded before it is sent. When a post's outcome is unknown it is never sent again on its own:
        check for it, record that you saw it, or retry knowing it may appear twice.
      </Paragraph>
      {notice && <Alert style={{ marginBottom: 12 }} type={notice.type} showIcon closable title={notice.text} onClose={() => setNotice(null)} />}
      {!data.data.items.length ? <Empty description="No replies yet." /> : (
        <Table<Reply>
          rowKey="id" size="small" dataSource={data.data.items} pagination={{ pageSize: 25 }}
          columns={[
            { title: "Recorded", dataIndex: "created", render: when },
            { title: "Kind", dataIndex: "kind" },
            { title: "State", dataIndex: "state", render: (s: string) => tag(REPLY, s) },
            { title: "Attempts", dataIndex: "attempts" },
            { title: "Delivery", render: (_: unknown, r: Reply) => (r.delivery ? <Tag>{r.delivery.status}</Tag> : "") },
            { title: "Error", dataIndex: "error", render: (e: string | null) => (e ? <Text type="secondary">{e}</Text> : null) },
            {
              title: "", render: (_: unknown, r) => r.state === "ambiguous" ? (
                <Space wrap>
                  <Button size="small" onClick={() => decide(r, "check")}>Check thread</Button>
                  <Popconfirm title="Record that you saw this reply in the thread?" onConfirm={() => decide(r, "done", { evidence: { note: "Seen in the thread by an administrator" } })}>
                    <Button size="small">I saw it</Button>
                  </Popconfirm>
                  <Popconfirm title="Send it again? It may then appear twice." onConfirm={() => decide(r, "retry", { acknowledge_duplicate_risk: true })}>
                    <Button size="small" danger>Retry</Button>
                  </Popconfirm>
                  <Popconfirm title="Give up on this reply?" onConfirm={() => decide(r, "abandon")}>
                    <Button size="small">Give up</Button>
                  </Popconfirm>
                </Space>
              ) : null,
            },
          ]}
        />
      )}
    </>
  );
}

function Links({ url }: { url: string }) {
  const data = useLoad<{ items: Link[] }>(`${url}/links`);
  const [form] = Form.useForm();
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const add = async (values: { external_id: string; account: string; workspace_id?: string; display?: string }) => {
    try {
      const out = await hubRequest<Link>(`${url}/links`, "POST", values);
      setNotice({ type: "success", text: `${out.external_id} now acts as ${out.account}.` });
      form.resetFields();
      data.reload();
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
  };
  const remove = async (l: Link) => {
    try {
      await hubRequest(`${url}/links/${encodeURIComponent(l.id)}`, "DELETE");
      setNotice({ type: "success", text: `Link removed. ${l.external_id} gets the not-linked notice next time.` });
      data.reload();
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
  };
  return (
    <>
      <Paragraph type="secondary">
        A provider account acts as a Hubzoid account only when linked here, by its immutable user id. Display names and
        email addresses in messages never count.
      </Paragraph>
      {notice && <Alert style={{ marginBottom: 12 }} type={notice.type} showIcon closable title={notice.text} onClose={() => setNotice(null)} />}
      {!data.data ? <LoadState error={data.error} retry={data.reload} /> : (
        <Table<Link>
          rowKey="id" size="small" dataSource={data.data.items} pagination={false}
          locale={{ emptyText: "No links yet. Nobody can start work until linked." }}
          columns={[
            { title: "Provider user", dataIndex: "external_id" },
            { title: "Name", dataIndex: "display" },
            { title: "Workspace", dataIndex: "workspace_id" },
            { title: "Acts as", dataIndex: "account" },
            { title: "Linked", dataIndex: "created", render: when },
            {
              title: "", render: (_: unknown, l) => (
                <Popconfirm title="Remove this link?" onConfirm={() => remove(l)}><Button size="small" danger>Remove</Button></Popconfirm>
              ),
            },
          ]}
        />
      )}
      <Title level={5} style={{ marginTop: 16 }}>Link an account</Title>
      <Form form={form} layout="inline" onFinish={add}>
        <Form.Item name="external_id" rules={[{ required: true, message: "The provider's user id" }]}>
          <Input placeholder="Provider user id (U0123...)" aria-label="Provider user id" />
        </Form.Item>
        <Form.Item name="account" rules={[{ required: true, message: "The Hubzoid account email" }]}>
          <Input placeholder="Hubzoid account email" aria-label="Hubzoid account email" />
        </Form.Item>
        <Form.Item name="display"><Input placeholder="Name (optional)" aria-label="Name" /></Form.Item>
        <Form.Item name="workspace_id"><Input placeholder="Workspace (optional)" aria-label="Workspace" /></Form.Item>
        <Form.Item><Button type="primary" htmlType="submit">Link</Button></Form.Item>
      </Form>
    </>
  );
}

// -- one thread ---------------------------------------------------------------------------
function ThreadView({ hub, cid, tid }: { hub: Hub; cid: string; tid: string }) {
  const url = `${base(hub)}/${encodeURIComponent(cid)}/threads/${encodeURIComponent(tid)}`;
  const data = useLoad<ThreadDetail>(url);
  const [busy, setBusy] = useState(false);
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const t = data.data;
  const toggle = async (on: boolean) => {
    setBusy(true);
    try {
      await hubRequest(`${url}/subscription`, "PUT", { subscribed: on, expected_revision: t.revision });
      data.reload();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="panel">
      <Space style={{ marginBottom: 8 }} wrap>
        <a href={channelHref(hub, cid)}>{cid}</a>
        <Text type="secondary">/</Text>
        <Title level={3} style={{ margin: 0 }}>{t.thread_key}</Title>
        {t.origin.url && <a href={t.origin.url} target="_blank" rel="noreferrer">Open in {t.origin.provider}</a>}
      </Space>
      <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="Replies go to">{t.origin.provider} · {JSON.stringify(t.origin.coordinates)}</Descriptions.Item>
        <Descriptions.Item label="Asked by">{t.requester || "?"}</Descriptions.Item>
        <Descriptions.Item label="Follow-ups without a mention">
          <Switch checked={t.subscribed} loading={busy} onChange={toggle} aria-label="Follow-ups continue the work" />
        </Descriptions.Item>
        <Descriptions.Item label="Conversations">
          {Object.values(t.conversations).length
            ? Object.entries(t.conversations).map(([acct, conv]) => <div key={acct}><a href={`/c/${encodeURIComponent(conv)}`}>{conv}</a></div>)
            : "None"}
        </Descriptions.Item>
        {t.work_item && (
          <Descriptions.Item label="Work item"><a href={href(`/work?item=${encodeURIComponent(t.work_item.id)}`)}>{t.work_item.workflow} · {t.work_item.state}</a></Descriptions.Item>
        )}
      </Descriptions>
      <Title level={4}>Deliveries</Title>
      <Table<Delivery>
        rowKey="id" size="small" pagination={false} dataSource={t.deliveries}
        columns={[
          { title: "Received", dataIndex: "created", render: when },
          { title: "From", render: (_: unknown, d) => d.actor?.display || d.actor?.external_id || "?" },
          { title: "Outcome", dataIndex: "status", render: (s: string) => tag(DELIVERY, s) },
          { title: "Why", dataIndex: "reason", render: (r: string | null) => (r ? REASONS[r] ?? r : null) },
        ]}
      />
      <Title level={4} style={{ marginTop: 16 }}>Replies</Title>
      <Table<Reply>
        rowKey="id" size="small" pagination={false} dataSource={t.replies}
        columns={[
          { title: "Recorded", dataIndex: "created", render: when },
          { title: "Kind", dataIndex: "kind" },
          { title: "State", dataIndex: "state", render: (s: string) => tag(REPLY, s) },
        ]}
      />
    </div>
  );
}

// -- provider-specific setup ------------------------------------------------------------
function ProviderPanel({ url, provider }: { url: string; provider: string }) {
  if (provider === "slack") return <SlackPanel url={url} />;
  if (provider === "github") return <GitHubPanel url={url} />;
  if (provider === "linear") return <LinearPanel url={url} />;
  if (provider === "email") return <EmailPanel url={url} />;
  return null;
}

type EmailStatus = {
  transport: string; mailbox: string; started: boolean; last_poll: number | null; last_error: string | null;
  draft_verified: boolean; connected: boolean; senders_allowed: number; authentication: string; pending: number;
  gap: { messages: number; at: number } | null;
};

function EmailPanel({ url }: { url: string }) {
  const status = useLoad<EmailStatus>(`${url}/email/status`);
  const [note, setNote] = useState<{ type: "success" | "error" | "info"; text: string } | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const act = async (what: "connect" | "poll" | "rehearse-draft") => {
    setNote(null);
    try {
      const out = await hubRequest<Record<string, unknown>>(`${url}/email/${what}`, "POST", {});
      if (what === "connect") setLink(String(out.authorization_url));
      else if (what === "poll") setNote(out.state === "busy" ? { type: "info", text: "The mailbox is being read right now. Try again in a moment." }
        : out.state === "paused" ? { type: "info", text: "The channel is paused, so mail is not read." }
        : { type: "success", text: out.state === "started" ? "Started: mail from now on is read." : `Read ${out.messages} new message(s).` });
      else setNote({ type: "success", text: String(out.note) });
      status.reload();
    } catch (e) {
      setNote({ type: "error", text: errorText(e) });
    }
  };
  if (!status.data) return status.error ? <Alert type="warning" showIcon title={status.error} style={{ marginBottom: 16 }} /> : null;
  const d = status.data;
  return (
    <div style={{ marginBottom: 16 }}>
      <Title level={4}>Mailbox {d.mailbox} {d.connected ? <Tag color="green">connected</Tag> : <Tag>not connected</Tag>}</Title>
      <Descriptions size="small" column={1} bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="Transport">{d.transport}</Descriptions.Item>
        <Descriptions.Item label="Reading">{d.started ? `since start, last read ${when(d.last_poll)}` : "not started"}{d.pending ? ` · ${d.pending} waiting to be read` : ""}</Descriptions.Item>
        <Descriptions.Item label="Senders who start work">{d.senders_allowed} allowed address(es), {d.authentication === "aligned_dkim" ? "with a valid signature from their domain" : "every message reviewed"}</Descriptions.Item>
        <Descriptions.Item label="Drafts">{d.draft_verified ? "checked: real drafts work" : "not checked yet"}</Descriptions.Item>
        {d.last_error && <Descriptions.Item label="Last problem">{d.last_error}</Descriptions.Item>}
        {d.gap && <Descriptions.Item label="Not re-read">{d.gap.messages} older message(s) after a mailbox reset ({when(d.gap.at)})</Descriptions.Item>}
      </Descriptions>
      <Space wrap>
        {d.transport !== "imap" && <Button onClick={() => act("connect")}>{d.connected ? "Reconnect" : "Get the consent link"}</Button>}
        <Button onClick={() => act("poll")}>Read new mail now</Button>
        <Popconfirm title="Save one draft in the mailbox (to itself) and read it back? Nothing is sent." onConfirm={() => act("rehearse-draft")}>
          <Button>Check drafts</Button>
        </Popconfirm>
      </Space>
      {link && <Paragraph style={{ marginTop: 8 }}><a href={link} target="_blank" rel="noreferrer">Open this signed in as {d.mailbox}</a> (works once, for 10 minutes)</Paragraph>}
      {note && <Alert style={{ marginTop: 8 }} type={note.type} showIcon title={note.text} />}
    </div>
  );
}

type GitHubSetup = { state: string; app_id: number | null; slug: string | null; installation_url: string | null; error: string | null };

function GitHubPanel({ url }: { url: string }) {
  const status = useLoad<GitHubSetup>(`${url}/github/setup`);
  const [form] = Form.useForm();
  const [reg, setReg] = useState<string | null>(null);
  const [repos, setRepos] = useState<{ id: number; full_name: string }[] | null>(null);
  const [notice, setNotice] = useState<{ type: "success" | "error" | "info"; text: string } | null>(null);
  const start = async (v: { owner: string; type: string; name: string }) => {
    try {
      const out = await hubRequest<{ registration_url: string }>(`${url}/github/setup`, "POST", v);
      setReg(out.registration_url);
      status.reload();
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
  };
  const list = async () => {
    try {
      setRepos((await hubRequest<{ items: { id: number; full_name: string }[] }>(`${url}/github/repositories`)).items);
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
  };
  const st = status.data?.state ?? "none";
  const files = useAgentEditing();
  return (
    <div style={{ marginBottom: 16 }}>
      <Title level={4}>GitHub App</Title>
      <Paragraph type="secondary">
        Your own App, owned by your account or organization. GitHub hands its private key and webhook secret to this
        hub's secret store directly. Setup state: <Tag>{st}</Tag>
      </Paragraph>
      {notice && <Alert style={{ marginBottom: 8 }} type={notice.type} showIcon closable title={notice.text} onClose={() => setNotice(null)} />}
      {(st === "none" || st === "failed") && !files.editable && (
        <Paragraph type="secondary">Set up the App with <Text code>hubzoid channels github connect</Text>. {files.message}</Paragraph>
      )}
      {(st === "none" || st === "failed") && files.editable && (
        <Form form={form} layout="inline" onFinish={start} initialValues={{ type: "organization" }}>
          <Form.Item name="owner" rules={[{ required: true, message: "The owner's GitHub login" }]}>
            <Input placeholder="Owner (example-company)" aria-label="GitHub owner" />
          </Form.Item>
          <Form.Item name="type">
            <Select aria-label="Owner type" style={{ width: 150 }} options={[{ value: "organization", label: "Organization" }, { value: "user", label: "User" }]} />
          </Form.Item>
          <Form.Item name="name" rules={[{ required: true, message: "The App's name" }]}>
            <Input placeholder="App name" aria-label="App name" />
          </Form.Item>
          <Form.Item><Button type="primary" htmlType="submit">Start App setup</Button></Form.Item>
        </Form>
      )}
      {reg && <Paragraph><a href={reg} target="_blank" rel="noreferrer">Continue to GitHub to create the App</a> (the link works once, for an hour)</Paragraph>}
      {status.data?.installation_url && <Paragraph><a href={status.data.installation_url} target="_blank" rel="noreferrer">Install the App on your repositories</a></Paragraph>}
      {st === "ready" && <Button onClick={list}>Show reachable repositories</Button>}
      {repos && <Paragraph style={{ marginTop: 8 }}>{repos.length ? repos.map((r) => <Tag key={r.id}>{r.full_name} ({r.id})</Tag>) : "None yet."}</Paragraph>}
    </div>
  );
}

type LinearSetup = { callback_url: string; webhook_url: string; scopes: string[]; steps: string[]; installed: boolean };

function LinearPanel({ url }: { url: string }) {
  const setup = useLoad<LinearSetup>(`${url}/linear/setup`);
  const [link, setLink] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const connect = async () => {
    try {
      setLink((await hubRequest<{ authorization_url: string }>(`${url}/linear/connect`, "POST", {})).authorization_url);
    } catch (e) {
      setError(errorText(e));
    }
  };
  if (!setup.data) return setup.error ? <Alert type="warning" showIcon title={setup.error} style={{ marginBottom: 16 }} /> : null;
  return (
    <div style={{ marginBottom: 16 }}>
      <Title level={4}>Linear app {setup.data.installed ? <Tag color="green">installed</Tag> : <Tag>not installed</Tag>}</Title>
      <ol>{setup.data.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
      <Paragraph>Callback URL: <Text code copyable>{setup.data.callback_url}</Text></Paragraph>
      <Paragraph>Webhook URL: <Text code copyable>{setup.data.webhook_url}</Text></Paragraph>
      <Button onClick={connect}>{setup.data.installed ? "Reconnect" : "Get the approval link"}</Button>
      {link && <Paragraph style={{ marginTop: 8 }}><a href={link} target="_blank" rel="noreferrer">A workspace admin approves the app here</a> (works once, for 10 minutes)</Paragraph>}
      {error && <Alert style={{ marginTop: 8 }} type="error" showIcon title={error} />}
    </div>
  );
}

function download(name: string, text: string) {
  const blob = new Blob([text], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function SlackPanel({ url }: { url: string }) {
  const [state, setState] = useState<{ busy?: boolean; error?: string; scopes?: string[] }>({});
  const get = async () => {
    setState({ busy: true });
    try {
      const out = await hubRequest<{ manifest: unknown; revision: number; required_scopes: string[] }>(`${url}/slack/manifest`);
      download("slack-app-manifest.json", JSON.stringify(out.manifest, null, 2));
      setState({ scopes: out.required_scopes });
    } catch (e) {
      setState({ error: errorText(e) });
    }
  };
  return (
    <div style={{ marginBottom: 16 }}>
      <Title level={4}>Slack app</Title>
      <Paragraph type="secondary">
        Create the app in your own Slack workspace from this manifest (api.slack.com/apps, Create New App, From a manifest).
        It holds no credentials. Store the tokens with hubzoid secrets set, never here.
      </Paragraph>
      <Button onClick={get} loading={state.busy}>Download app manifest</Button>
      {state.error && <Alert style={{ marginTop: 8 }} type="error" showIcon title={state.error} />}
      {state.scopes && <Paragraph style={{ marginTop: 8 }}>Permissions it asks for: {state.scopes.map((sc) => <Tag key={sc}>{sc}</Tag>)}</Paragraph>}
    </div>
  );
}
