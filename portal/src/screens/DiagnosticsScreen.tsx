// Console → Diagnostics: read-only checks of the deployment, run on request.
// Each check says ok, info, warn, fail, skipped or timeout with the next step.
// A report survives a restart as "interrupted" (never rerun on its own) and
// can produce a support bundle of metadata only. Same API as
// `hubzoid admin diagnostics ...`.
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Checkbox, Col, Descriptions, Empty, Row, Select, Space, Statistic, Table, Tag, Typography } from "antd";
import type { Hub } from "../api";
import { control, ControlError, newKey } from "../lib/controlApi";

const { Text, Paragraph, Title } = Typography;

type Check = {
  check_id: string;
  status: string;
  summary: string;
  details: Record<string, unknown>;
  duration_ms: number | null;
  remediation: string | null;
  owner: string;
};
type Report = {
  report_id: string;
  status: string;
  profile: string;
  hub_id: string | null;
  requested_by: string;
  created_at: number;
  completed_at: number | null;
  planned: string[];
  checks: Check[];
  counts: Record<string, number>;
  error_code: string | null;
};
type ReportRow = Omit<Report, "checks" | "counts" | "planned">;
type Card_ = { value: number | string | null; availability: string; unpriced_turns?: number; note?: string };
type Overview = { interval_days: number; cards: Record<string, Card_> };
type Bundle = { bundle_id: string; sha256: string; size_bytes: number; expires_at: number; download: string; manifest: { omitted: string[] } };

const COLOURS: Record<string, string> = { ok: "green", info: "blue", warn: "gold", fail: "red", skipped: "default", timeout: "magenta" };
const when = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

function figure(card: Card_ | undefined, format?: (v: number) => string) {
  if (!card || card.value === null || card.value === undefined) return <Text type="secondary">Unknown</Text>;
  return typeof card.value === "number" && format ? format(card.value) : String(card.value);
}

export function DiagnosticsScreen({ hubs, embedded = false }: { hubs: Hub[]; embedded?: boolean }) {
  const { message } = App.useApp();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [reports, setReports] = useState<ReportRow[] | null>(null);
  const [selected, setSelected] = useState<Report | null>(null);
  const [scopeHub, setScopeHub] = useState<string | undefined>(undefined);
  const [network, setNetwork] = useState(false);
  const [error, setError] = useState<ControlError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [bundle, setBundle] = useState<Bundle | null>(null);

  const load = useCallback(async () => {
    try {
      const [o, r] = await Promise.all([
        control<Overview>("/api/admin/overview"),
        control<{ items: ReportRow[] }>("/api/admin/diagnostics/reports"),
      ]);
      setOverview(o);
      setReports(r.items);
      setError(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (id: string) => {
    try {
      setSelected(await control<Report>(`/api/admin/diagnostics/reports/${id}`));
      setBundle(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);

  // Poll the open report while it is queued or running.
  useEffect(() => {
    if (!selected || !["queued", "running"].includes(selected.status)) return;
    const t = window.setTimeout(() => void open(selected.report_id).then(load), 1000);
    return () => window.clearTimeout(t);
  }, [selected, open, load]);

  const run = async () => {
    setBusy("run");
    setError(null);
    try {
      const res = await control<{ report_id: string }>("/api/admin/diagnostics/reports", {
        idempotencyKey: newKey(),
        body: { profile: network ? "active" : "passive", scope: { hub_id: scopeHub ?? null }, consent_network: network },
      });
      await load();
      await open(res.report_id);
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    if (!selected) return;
    try {
      setSelected(await control<Report>(`/api/admin/diagnostics/reports/${selected.report_id}/cancel`, { method: "POST", idempotencyKey: newKey() }));
    } catch (e) {
      setError(e as ControlError);
    }
  };

  const makeBundle = async () => {
    if (!selected) return;
    setBusy("bundle");
    try {
      setBundle(await control<Bundle>(`/api/admin/diagnostics/reports/${selected.report_id}/bundles`, { method: "POST", idempotencyKey: newKey() }));
      message.success("Support bundle ready. It contains metadata only.");
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(null);
    }
  };

  const c = overview?.cards;
  return (
    <div className="screen">
      <Title level={2} style={{ marginTop: 0 }}>
        Health &amp; support
      </Title>
      <Paragraph type="secondary">
        Run read-only checks and review reports.
      </Paragraph>
      {error && (
        <Alert
          type={error.status === 403 ? "warning" : "error"}
          showIcon
          title={error.status === 403 ? "You can't do that here" : "That didn't work"}
          description={error.message}
          style={{ marginBottom: 16 }}
          action={<Button onClick={() => void load()}>Retry</Button>}
        />
      )}
      {!embedded && <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title={`Turns (${overview?.interval_days ?? 7} days)`} valueRender={() => figure(c?.turns)} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="Tokens" valueRender={() => figure(c?.tokens, (v) => v.toLocaleString())} /></Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Estimated cost (USD)" valueRender={() => figure(c?.cost_usd, (v) => `$${v.toFixed(2)}`)} />
            {!!c?.cost_usd?.unpriced_turns && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {c.cost_usd.unpriced_turns} turn(s) have no price: not counted as zero.
              </Text>
            )}
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small"><Statistic title="Last report" valueRender={() => figure(c?.diagnostics)} /></Card>
        </Col>
      </Row>}

      <Card title="Run checks" size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Select
            aria-label="Scope"
            style={{ width: 240 }}
            value={scopeHub ?? "__deployment"}
            onChange={(v) => setScopeHub(v === "__deployment" ? undefined : v)}
            options={[{ value: "__deployment", label: "Whole deployment" }, ...hubs.map((h) => ({ value: h.key, label: h.name }))]}
          />
          <Checkbox checked={network} onChange={(e) => setNetwork(e.target.checked)}>
            Check connected services too (contacts them; an administrator must allow it)
          </Checkbox>
          <Button type="primary" onClick={() => void run()} loading={busy === "run"}>
            Run checks
          </Button>
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={9}>
          <Card title="Reports" size="small">
            <Table<ReportRow>
              size="small"
              rowKey="report_id"
              pagination={false}
              dataSource={reports ?? []}
              loading={!reports}
              locale={{ emptyText: "No diagnostic reports yet. Run checks to make one." }}
              onRow={(r) => ({ onClick: () => void open(r.report_id), style: { cursor: "pointer" } })}
              rowClassName={(r) => (r.report_id === selected?.report_id ? "ant-table-row-selected" : "")}
              columns={[
                { title: "When", render: (_, r) => when(r.created_at) },
                { title: "Scope", render: (_, r) => r.hub_id ?? "deployment" },
                { title: "Status", render: (_, r) => <Tag color={r.status === "completed" ? "green" : r.status === "interrupted" ? "orange" : undefined}>{r.status}</Tag> },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={15}>
          {!selected ? (
            <Card size="small">
              <Empty description="Pick a report to see its checks." />
            </Card>
          ) : (
            <Card
              size="small"
              title={`Report ${selected.report_id.slice(0, 8)}`}
              extra={
                <Space>
                  {["queued", "running"].includes(selected.status) ? (
                    <Button size="small" onClick={() => void cancel()}>Cancel</Button>
                  ) : (
                    <>
                      <Button size="small" onClick={() => void run()}>Run again</Button>
                      <Button size="small" onClick={() => void makeBundle()} loading={busy === "bundle"}>
                        Support bundle
                      </Button>
                    </>
                  )}
                </Space>
              }
            >
              <Descriptions size="small" column={2} style={{ marginBottom: 12 }}>
                <Descriptions.Item label="Status">
                  <Tag>{selected.status}</Tag>
                  {selected.status === "interrupted" && <Text type="secondary">The worker stopped. It was not rerun.</Text>}
                </Descriptions.Item>
                <Descriptions.Item label="Requested by">{selected.requested_by}</Descriptions.Item>
                <Descriptions.Item label="Profile">{selected.profile}</Descriptions.Item>
                <Descriptions.Item label="Finished">{when(selected.completed_at)}</Descriptions.Item>
              </Descriptions>
              {bundle && (
                <Alert
                  type="success"
                  showIcon
                  style={{ marginBottom: 12 }}
                  title={<a href={bundle.download}>Download support bundle ({Math.round(bundle.size_bytes / 1024)} KB)</a>}
                  description={`SHA-256 ${bundle.sha256.slice(0, 16)}… · expires ${when(bundle.expires_at)} · leaves out ${bundle.manifest.omitted.join(", ")}`}
                />
              )}
              <Table<Check>
                size="small"
                rowKey="check_id"
                pagination={false}
                dataSource={selected.checks}
                locale={{ emptyText: ["queued", "running"].includes(selected.status) ? "Running…" : "No checks recorded." }}
                columns={[
                  { title: "Check", dataIndex: "check_id", render: (v: string) => <Text code>{v}</Text> },
                  { title: "Status", dataIndex: "status", render: (s: string) => <Tag color={COLOURS[s]}>{s}</Tag> },
                  {
                    title: "Result",
                    render: (_, ch) => (
                      <>
                        {ch.summary}
                        {ch.remediation && ["warn", "fail", "timeout"].includes(ch.status) && (
                          <div>
                            <Text type="secondary">Next: {ch.remediation}</Text>
                          </div>
                        )}
                      </>
                    ),
                  },
                ]}
              />
            </Card>
          )}
        </Col>
      </Row>
    </div>
  );
}
