import { useCallback, useEffect, useState } from "react";
import {
  Alert, App, Button, Card, Checkbox, Descriptions, Drawer, Empty, Input, Radio, Segmented, Select,
  Space, Spin, Steps, Table, Tag, Typography,
} from "antd";
import { ApiError, request, query } from "../api";
import { useData } from "../hooks/useData";
import { useVisibleRefresh } from "../hooks/useVisibleRefresh";

const { Text, Title, Paragraph } = Typography;

type Stage = { stage: string; step: number; status: "done" | "failed"; finished: number | null };
type Operation = {
  id: string; item_id: string | null; name: string; action: string | null; mode: string; recovery: string;
  state: string; args_digest: string; revision: number; target: Record<string, unknown>;
  attempts: { n: number; state: string; started: number; ended?: number }[];
  approval: { approver: string; at: number } | null; error: string | null;
  result?: unknown; evidence?: unknown; resolution?: Record<string, unknown> | null;
};
type WorkItem = {
  id: string; workflow: string; logical_key: string; generation: number; route: string; state: string;
  reason: string | null; requester: string; execution: number; revision: number; created: number;
  updated: number; error: string | null; input?: unknown; result?: unknown; pending_messages?: number;
  stages?: Stage[]; operations?: Operation[]; route_detail?: { stages: string[] | null; actions: string[] | null };
};
type HubOption = { key: string; name: string };

const STATE_COLORS: Record<string, string> = {
  accepted: "default", running: "processing", waiting: "gold", needs_input: "orange",
  completed: "green", stopped: "default", failed: "red", cancelled: "default",
};
const STATE_LABELS: Record<string, string> = {
  accepted: "Accepted", running: "Running", waiting: "Waiting for approval", needs_input: "Needs a decision",
  completed: "Done", stopped: "Stopped", failed: "Failed", cancelled: "Cancelled",
};
const OP_LABELS: Record<string, string> = {
  pending: "Ready to send", awaiting_approval: "Waiting for approval", sending: "Sending",
  succeeded: "Done", failed_before_send: "Not sent", rejected: "Refused", ambiguous: "Outcome unknown",
  resolved_done: "Done (confirmed by a person)", abandoned: "Stopped",
};
const FILTERS = [
  { label: "All", value: "" }, { label: "Waiting", value: "waiting" }, { label: "Needs a decision", value: "needs_input" },
  { label: "Running", value: "running" }, { label: "Done", value: "completed" }, { label: "Stopped", value: "stopped" },
  { label: "Failed", value: "failed" },
];

const when = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString() : "");
const errorMessage = (e: unknown) => {
  if (e instanceof ApiError) {
    const detail = e.data?.detail as { message?: string } | string | undefined;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : String(e);
};

function StateTag({ state, reason }: { state: string; reason?: string | null }) {
  return (
    <Tag color={STATE_COLORS[state] ?? "default"} title={reason ?? undefined}>
      {STATE_LABELS[state] ?? state}
    </Tag>
  );
}

export function WorkItemsScreen({ hubs }: { hubs: HubOption[] }) {
  const [hub, setHub] = useState<string | undefined>(hubs[0]?.key);
  const [state, setState] = useState<string>("");
  const [open, setOpen] = useState<string | null>(null);
  const path = hub ? `/hubs/${encodeURIComponent(hub)}/work-items${query({ state })}` : null;
  const items = useData<{ items: WorkItem[] }>(path, { keepStale: true });
  useVisibleRefresh(items.reload, items.refreshing, !open);

  if (!hubs.length) return <Empty description="You have no agents yet." />;
  return (
    <div>
      <Title level={2} style={{ marginTop: 0 }}>Work items</Title>
      <Paragraph type="secondary">
        Durable work an agent does for someone: each item keeps its saved stages, and every action it takes
        outside the hub is recorded. Approve actions and decide uncertain outcomes here, from the CLI
        (<Text code>hubzoid work-items</Text>, <Text code>hubzoid operations</Text>) or the API.
      </Paragraph>
      <Space wrap style={{ marginBottom: 16 }}>
        {hubs.length > 1 && (
          <Select aria-label="Agent" value={hub} onChange={setHub} style={{ minWidth: 200 }}
            options={hubs.map((h) => ({ value: h.key, label: h.name }))} />
        )}
        <Segmented aria-label="Filter by state" value={state} onChange={(v) => setState(String(v))} options={FILTERS} />
      </Space>
      {items.error && !items.data ? (
        <Alert type="error" showIcon title="Couldn’t load work items" description={items.error}
          action={<Button onClick={items.reload}>Try again</Button>} />
      ) : items.loading ? (
        <Spin />
      ) : (items.data?.items.length ?? 0) === 0 ? (
        <Empty description={state ? "No work in this state." : "No work started yet."}>
          <Text type="secondary">
            Start work with <Text code>hubzoid work-items start</Text> or from a workflow that declares work items.
          </Text>
        </Empty>
      ) : (
        <Table<WorkItem>
          rowKey="id"
          size="middle"
          dataSource={items.data?.items}
          pagination={false}
          onRow={(row) => ({ onClick: () => setOpen(row.id), style: { cursor: "pointer" } })}
          columns={[
            { title: "Work", render: (_, r) => (<>
              <Text strong>{r.workflow}</Text><br /><Text type="secondary" className="mono">{r.logical_key}</Text>
            </>) },
            { title: "State", render: (_, r) => <StateTag state={r.state} reason={r.reason} /> },
            { title: "Asked by", dataIndex: "requester", render: (v) => v || <Text type="secondary">unattended</Text> },
            { title: "Updated", dataIndex: "updated", render: (v) => when(v) },
            { title: "", render: (_, r) => <Button type="link" onClick={() => setOpen(r.id)}>Open</Button> },
          ]}
        />
      )}
      {hub && open && (
        <WorkItemDrawer hub={hub} id={open} onClose={() => { setOpen(null); items.reload(); }} />
      )}
    </div>
  );
}

function WorkItemDrawer({ hub, id, onClose }: { hub: string; id: string; onClose: () => void }) {
  const base = `/hubs/${encodeURIComponent(hub)}`;
  const item = useData<WorkItem>(`${base}/work-items/${encodeURIComponent(id)}`, { keepStale: true });
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const active = item.data && ["accepted", "running"].includes(item.data.state);
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(item.reload, 2000);
    return () => window.clearInterval(t);
  }, [active, item.reload]);

  const act = useCallback(async (path: string, body: unknown, done: string) => {
    setBusy(true);
    try {
      await request(path, body, undefined, "POST");
      message.success(done);
      item.reload();
    } catch (e) {
      message.error(errorMessage(e));
      item.reload();
    } finally {
      setBusy(false);
    }
  }, [item, message]);

  const w = item.data;
  return (
    <Drawer open onClose={onClose} size={720} title={w ? `${w.workflow} · ${w.logical_key}` : "Work item"}>
      {!w ? (item.error ? <Alert type="error" showIcon title={item.error} /> : <Spin />) : (
        <Space direction="vertical" size="large" style={{ width: "100%" }}>
          <Descriptions size="small" column={2} bordered items={[
            { key: "state", label: "State", children: <StateTag state={w.state} reason={w.reason} /> },
            { key: "route", label: "Route", children: w.route },
            { key: "gen", label: "Generation", children: w.generation },
            { key: "exec", label: "Execution", children: w.execution },
            { key: "by", label: "Asked by", children: w.requester || "unattended" },
            { key: "rev", label: "Revision", children: w.revision },
          ]} />
          {w.state === "running" && <Alert type="info" showIcon title="Running" description="It keeps going if you close this page." />}
          {w.state === "waiting" && <Alert type="warning" showIcon title="Waiting for approval"
            description="An action below waits for the person who asked for this work to approve its exact content." />}
          {w.state === "needs_input" && w.reason === "operation_ambiguous" && <Alert type="warning" showIcon
            title="We cannot confirm whether an action completed"
            description="Nothing is retried automatically. The person who asked for this work decides below." />}
          {w.state === "needs_input" && w.reason === "definition_changed" && <Alert type="warning" showIcon
            title="The workflow code changed while this work was active" description={w.error} />}
          {(w.state === "failed" || w.state === "stopped") && w.error && <Alert
            type={w.state === "failed" ? "error" : "info"} showIcon title={STATE_LABELS[w.state]} description={w.error} />}

          <Card size="small" title="Stages">
            {(w.stages?.length ?? 0) === 0 ? <Text type="secondary">No stage has finished yet.</Text> : (
              <Steps direction="vertical" size="small" current={w.stages!.length}
                items={w.stages!.map((s) => ({ title: s.stage, status: s.status === "done" ? "finish" : "error",
                  description: s.finished ? `Saved ${when(s.finished)}` : undefined }))} />
            )}
          </Card>

          <Card size="small" title="Actions outside the hub">
            {(w.operations?.length ?? 0) === 0 ? <Text type="secondary">None recorded.</Text> : (
              <Space direction="vertical" style={{ width: "100%" }}>
                {w.operations!.map((op) => (
                  <OperationCard key={op.id} base={base} op={op} busy={busy} act={act} />
                ))}
              </Space>
            )}
          </Card>

          {w.result !== undefined && w.result !== null && (
            <Card size="small" title="Result"><pre className="output">{JSON.stringify(w.result, null, 2)}</pre></Card>
          )}
          {w.input !== undefined && w.input !== null && (
            <Card size="small" title="Input"><pre className="output">{JSON.stringify(w.input, null, 2)}</pre></Card>
          )}

          <ItemControls base={base} w={w} busy={busy} act={act} />
        </Space>
      )}
    </Drawer>
  );
}

function OperationCard({ base, op, busy, act }: {
  base: string; op: Operation; busy: boolean;
  act: (path: string, body: unknown, done: string) => Promise<void>;
}) {
  const detail = useData<Operation & { payload?: { arguments: unknown } }>(
    op.state === "awaiting_approval" || op.state === "ambiguous" ? `${base}/operations/${op.id}` : null);
  const [decision, setDecision] = useState<"done" | "retry" | "abandon">("done");
  const [evidence, setEvidence] = useState("");
  const [ack, setAck] = useState(false);
  const opPath = `${base}/operations/${encodeURIComponent(op.id)}`;
  return (
    <Card size="small" type="inner" title={<Space>
      <Text strong>{op.name}</Text>{op.action && <Tag>{op.action}</Tag>}<Tag>{op.mode}</Tag>
      <Tag color={op.state === "ambiguous" ? "orange" : op.state === "awaiting_approval" ? "gold" :
        ["succeeded", "resolved_done"].includes(op.state) ? "green" : "default"}>{OP_LABELS[op.state] ?? op.state}</Tag>
    </Space>}>
      <Text type="secondary">Target </Text><Text code>{JSON.stringify(op.target)}</Text><br />
      <Text type="secondary">Content digest </Text><Text code copyable>{op.args_digest}</Text>
      {op.error && <Alert style={{ marginTop: 8 }} type="warning" showIcon title={op.error} />}
      {op.approval && <div><Text type="secondary">Approved by {op.approval.approver}</Text></div>}
      {op.state === "awaiting_approval" && (
        <div style={{ marginTop: 12 }}>
          {detail.data?.payload ? (<>
            <Text strong>Exactly what will be sent</Text>
            <ExactContent value={detail.data.payload.arguments} />
            <Space>
              <Button type="primary" loading={busy} onClick={() => act(`${opPath}/approve`,
                { digest: op.args_digest, expected_revision: op.revision }, "Approved. The work continues and sends it once.")}>
                Approve and send
              </Button>
              <Button danger loading={busy} onClick={() => act(`${opPath}/deny`,
                { expected_revision: op.revision }, "Denied. Nothing was sent.")}>Deny</Button>
            </Space>
          </>) : detail.error ? (
            <Text type="secondary">Waiting for the person who asked for this work to approve it.</Text>
          ) : <Spin size="small" />}
        </div>
      )}
      {op.state === "ambiguous" && (detail.data?.payload ? (
        <div style={{ marginTop: 12 }}>
          <Text strong>What happened?</Text>
          <Radio.Group style={{ display: "block", marginTop: 8 }} value={decision} onChange={(e) => setDecision(e.target.value)}
            options={[
              { value: "done", label: "It happened. I checked." },
              { value: "retry", label: "Try again (it may happen twice)" },
              { value: "abandon", label: "Abandon this action" },
            ]} />
          {decision === "done" && <Input.TextArea style={{ marginTop: 8 }} rows={2} value={evidence}
            onChange={(e) => setEvidence(e.target.value)} placeholder="Where you confirmed it: a link, an id or a note" />}
          {decision === "retry" && <Checkbox style={{ marginTop: 8 }} checked={ack} onChange={(e) => setAck(e.target.checked)}>
            I understand this may send it a second time
          </Checkbox>}
          <div style={{ marginTop: 8 }}>
            <Button type="primary" loading={busy}
              disabled={(decision === "done" && !evidence.trim()) || (decision === "retry" && !ack)}
              onClick={() => act(`${opPath}/resolve`, { decision, evidence: evidence || null,
                acknowledge_duplicate_risk: ack, expected_revision: op.revision }, "Decision recorded.")}>
              Record decision
            </Button>
          </div>
        </div>
      ) : detail.error ? (
        <div style={{ marginTop: 8 }}><Text type="secondary">Only the person who asked for this work can decide.</Text></div>
      ) : null)}
    </Card>
  );
}

/** The exact arguments, readable: each field on its own, multi-line text kept as written. */
function ExactContent({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return <pre className="output">{JSON.stringify(value, null, 2)}</pre>;
  return (
    <Descriptions size="small" column={1} bordered style={{ margin: "8px 0 12px" }}
      items={Object.entries(value as Record<string, unknown>).map(([k, v]) => ({
        key: k, label: k,
        children: typeof v === "string"
          ? <div style={{ whiteSpace: "pre-wrap" }}>{v}</div>
          : <Text code>{JSON.stringify(v)}</Text>,
      }))} />
  );
}

function ItemControls({ base, w, busy, act }: {
  base: string; w: WorkItem; busy: boolean;
  act: (path: string, body: unknown, done: string) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [stage, setStage] = useState<string | undefined>(undefined);
  const itemPath = `${base}/work-items/${encodeURIComponent(w.id)}`;
  const terminal = ["completed", "stopped", "failed", "cancelled"].includes(w.state);
  const restartable = ["needs_input", "failed", "stopped"].includes(w.state) && w.reason !== "operation_ambiguous";
  return (
    <Card size="small" title="Controls">
      <Space direction="vertical" style={{ width: "100%" }}>
        {!terminal && (
          <Space>
            <Button danger loading={busy} onClick={() => act(`${itemPath}/cancel`, { expected_revision: w.revision },
              "Cancelled. What it already did stays recorded.")}>Cancel work</Button>
            {(w.state === "waiting" || w.state === "needs_input") && w.reason !== "definition_changed"
              && !(w.operations ?? []).some((o) => o.state === "ambiguous" || (o.state === "awaiting_approval" && !o.approval)) && (
              <Button loading={busy} onClick={() => act(`${itemPath}/resume`, { expected_revision: w.revision },
                "Continuing from the last saved step.")}>Continue</Button>
            )}
          </Space>
        )}
        {restartable && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Text>Restart on the current code. Earlier saved stages are reused.</Text>
            <Space wrap>
              <Select allowClear placeholder="After the last saved stage" style={{ minWidth: 220 }} value={stage}
                onChange={setStage} options={(w.stages ?? []).map((s) => ({ value: s.stage, label: `Re-run from ${s.stage}` }))} />
              <Input placeholder="Why (required)" value={reason} onChange={(e) => setReason(e.target.value)} style={{ minWidth: 240 }} />
              <Button loading={busy} disabled={!reason.trim()} onClick={() => act(`${itemPath}/restart`,
                { stage: stage ?? null, reason, expected_revision: w.revision }, "Restarted.")}>Restart</Button>
            </Space>
          </Space>
        )}
        {terminal && <Text type="secondary">This work has finished. Its record stays.</Text>}
      </Space>
    </Card>
  );
}
