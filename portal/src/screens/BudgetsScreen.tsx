import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Descriptions, Empty, Input, Modal, Select, Space, Spin, Table, Tag, Typography } from "antd";
import { ApiError, request } from "../api";
import { useData } from "../hooks/useData";

const { Text, Title, Paragraph } = Typography;

type Reservation = { id: string; run_id: string; item_id: string | null; model: string | null; cost_usd: number;
  output_tokens: number; created: number; unknown: boolean };
type Status = {
  present: boolean; revision: string; yaml: string;
  limits: Record<string, Record<string, string | number>>;
  month: { key: string; input_tokens: number; output_tokens: number; cost_usd: string; unpriced_calls: number };
  reserved: { input_tokens: number; output_tokens: number; cost_usd: string };
  open_reservations: Reservation[];
};
type HubOption = { key: string; name: string };

const SCOPE_LABELS: Record<string, string> = { run: "Each run", work_item: "Each work item", month: "This hub, per month" };
const KEY_LABELS: Record<string, string> = {
  max_turns: "turns", max_input_tokens: "input tokens", max_output_tokens: "output tokens", max_cost_usd: "USD",
};
const EXAMPLE = `version: 1
limits:
  run:
    max_turns: 40
    max_output_tokens: 100000
  month:
    max_output_tokens: 2000000
`;
const when = (t: number) => new Date(t * 1000).toLocaleString();
const errorMessage = (e: unknown) => {
  if (e instanceof ApiError) {
    const detail = e.data?.detail as { message?: string } | string | undefined;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : String(e);
};

/** Usage limits (budgets.yaml): what is in force, this month's usage, the
 *  file itself, and reservations whose call never reported. */
export function BudgetsScreen({ hubs }: { hubs: HubOption[] }) {
  const { message } = App.useApp();
  const [hub, setHub] = useState<string | undefined>(hubs[0]?.key);
  const base = hub ? `/hubs/${encodeURIComponent(hub)}/budgets` : null;
  const data = useData<Status>(base, { keepStale: true });
  const [draft, setDraft] = useState<string>("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [releasing, setReleasing] = useState<Reservation | null>(null);
  const [reason, setReason] = useState("");
  const s = data.data;

  useEffect(() => {
    if (s) setDraft(s.yaml || EXAMPLE);
  }, [s?.revision]); // eslint-disable-line react-hooks/exhaustive-deps

  const check = async () => {
    if (!base) return;
    try {
      await request(`${base}/validate`, { yaml: draft }, undefined, "POST");
      setProblem(null);
      message.success("The limits are valid.");
    } catch (e) {
      setProblem(errorMessage(e));
    }
  };
  const save = async () => {
    if (!base || !s) return;
    setBusy(true);
    try {
      await request(base, { yaml: draft, expected_revision: s.revision }, undefined, "PUT");
      setProblem(null);
      message.success("Saved. The next model call is checked against these limits.");
      data.reload();
    } catch (e) {
      setProblem(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const release = async () => {
    if (!base || !releasing) return;
    setBusy(true);
    try {
      await request(`${base}/reservations/${encodeURIComponent(releasing.id)}/release`, { reason }, undefined, "POST");
      message.success("Released.");
      setReleasing(null);
      setReason("");
      data.reload();
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  if (!hubs.length) return <Empty description="You have no agents yet." />;
  return (
    <div>
      <Title level={2} style={{ marginTop: 0 }}>Usage limits</Title>
      <Paragraph type="secondary">
        Optional limits on tokens, turns or dollars for each run, each work item and the whole hub per month. Every
        model call is checked first; a call over a limit does not start. Nothing is limited until you set it. The
        same file is <Text code>budgets.yaml</Text> and <Text code>hubzoid budgets</Text>.
      </Paragraph>
      {hubs.length > 1 && (
        <Select aria-label="Agent" value={hub} onChange={setHub} style={{ minWidth: 200, marginBottom: 16 }}
          options={hubs.map((h) => ({ value: h.key, label: h.name }))} />
      )}
      {data.error && s && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }} title="Couldn’t refresh. What you see may be out of date."
          description={data.error} action={<Button onClick={data.reload}>Try again</Button>} />
      )}
      {data.error && !s ? (
        <Alert type="error" showIcon title="Couldn’t load usage limits" description={data.error}
          action={<Button onClick={data.reload}>Try again</Button>} />
      ) : !s ? <Spin /> : (
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Card size="small" title="In force">
            {!s.present || Object.keys(s.limits).length === 0 ? <Text type="secondary">Nothing is limited.</Text> : (
              <Descriptions size="small" column={1} items={Object.entries(s.limits).map(([scope, caps]) => ({
                key: scope, label: SCOPE_LABELS[scope] ?? scope,
                children: <Space wrap>{Object.entries(caps).map(([k, v]) => <Tag key={k}>{String(v)} {KEY_LABELS[k] ?? k}</Tag>)}</Space>,
              }))} />
            )}
          </Card>
          <Card size="small" title={`This month (${s.month.key})`}>
            <Space wrap size="large">
              <Text>{s.month.input_tokens.toLocaleString()} input tokens</Text>
              <Text>{s.month.output_tokens.toLocaleString()} output tokens</Text>
              <Text>{Number(s.month.cost_usd).toFixed(4)} USD</Text>
              {s.month.unpriced_calls > 0 && <Text type="secondary">{s.month.unpriced_calls} calls with no price</Text>}
              {Number(s.reserved.cost_usd) > 0 || s.reserved.output_tokens > 0
                ? <Text type="secondary">Held by running calls: {Number(s.reserved.cost_usd).toFixed(4)} USD, {s.reserved.output_tokens.toLocaleString()} output tokens</Text>
                : null}
            </Space>
          </Card>
          {s.open_reservations.length > 0 && (
            <Card size="small" title="Held by running calls">
              <Table<Reservation> rowKey="id" size="small" pagination={false} dataSource={s.open_reservations}
                columns={[
                  { title: "Run", dataIndex: "run_id", render: (v) => <Text className="mono">{String(v).slice(0, 24)}</Text> },
                  { title: "Since", dataIndex: "created", render: (v) => when(v) },
                  { title: "", render: (_, r) => r.unknown
                    ? <Space><Tag color="gold">Never reported</Tag><Button size="small" onClick={() => setReleasing(r)}>Release</Button></Space>
                    : <Tag className="hz-tag-accent">Running</Tag> },
                ]} />
            </Card>
          )}
          <Card size="small" title="budgets.yaml">
            <Space direction="vertical" style={{ width: "100%" }}>
              <Input.TextArea rows={10} value={draft} onChange={(e) => setDraft(e.target.value)}
                className="mono" aria-label="budgets.yaml" spellCheck={false} />
              {problem && <Alert type="error" showIcon title={problem} />}
              <Space>
                <Button onClick={check}>Check</Button>
                <Button type="primary" loading={busy} disabled={!!data.error || data.refreshing} onClick={save}>Save</Button>
                <Text type="secondary">Revision {s.revision}</Text>
              </Space>
            </Space>
          </Card>
        </Space>
      )}
      <Modal open={releasing !== null} title="Release this reservation?" okText="Release"
        okButtonProps={{ disabled: !reason.trim(), loading: busy }} onOk={release} onCancel={() => setReleasing(null)}>
        <Paragraph>
          Its call never reported its usage, so the amount it held still counts against your limits. Release it only
          after checking what the model provider charged.
        </Paragraph>
        <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="What you checked. This is recorded." aria-label="Reason" />
      </Modal>
    </div>
  );
}
