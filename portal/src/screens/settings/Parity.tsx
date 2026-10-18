// Console → Settings → Parity: the chat parity ledger report. Validates the
// deployment's parity.yaml, shows counts per owner and every blocker, and
// records candidates, digest-bound reviews and freezes. A freeze says the
// requirements are fixed, never that the product passed them.
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Empty, Space, Statistic, Table, Tag, Typography } from "antd";
import { control, ControlError, newKey } from "../../lib/controlApi";

const { Text, Paragraph } = Typography;

type Issue = { code: string; message: string };
type Report = {
  valid: boolean;
  digest: string;
  status: string;
  counts: { obligations: number; acceptance_cases: number; unmapped_locators: number; by_owner: Record<string, number> };
  errors: Issue[];
  blockers: Issue[];
  note: string;
};
type Candidate = {
  id: string;
  digest: string;
  status: string;
  revision: number;
  created_by: string;
  created_at: number;
  reviews: { id: string; decision: string; digest: string; actor: string }[];
  freeze: { freeze_id: string } | null;
};

export function Parity() {
  const { message } = App.useApp();
  const [report, setReport] = useState<Report | null>(null);
  const [items, setItems] = useState<Candidate[]>([]);
  const [error, setError] = useState<ControlError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const list = useCallback(async () => {
    try {
      setItems((await control<{ items: Candidate[] }>("/api/parity/candidates")).items);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void list();
  }, [list]);

  const step = async (label: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      if (ok) message.success(ok);
      await list();
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Paragraph type="secondary" style={{ margin: 0 }}>
        What Hubzoid's chat must preserve, each item with one owner and an acceptance check. Reads the deployment's{" "}
        <Text code>parity.yaml</Text>. Inventories are owner-prepared and redacted; nothing here reads credentials or chats.
      </Paragraph>
      {error && <Alert type="error" showIcon title="That didn't work" description={error.message} />}
      <Space>
        <Button
          onClick={() =>
            void step("validate", async () => setReport(await control<Report>("/api/parity/validations", { body: { from_source: true } })))
          }
          loading={busy === "validate"}
        >
          Validate parity.yaml
        </Button>
        <Button
          type="primary"
          onClick={() =>
            void step("candidate", () => control("/api/parity/candidates", { idempotencyKey: newKey(), body: { from_source: true } }), "Candidate recorded.")
          }
          loading={busy === "candidate"}
        >
          Record a candidate
        </Button>
      </Space>
      {report && (
        <Card size="small" title={<>Report <Tag color={report.status === "ready_for_review" ? "green" : report.valid ? "gold" : "red"}>{report.status}</Tag></>}>
          <Space size={32} wrap>
            <Statistic title="Obligations" value={report.counts.obligations} />
            <Statistic title="Acceptance cases" value={report.counts.acceptance_cases} />
            <Statistic title="Unmapped locators" value={report.counts.unmapped_locators} />
            <Statistic title="Blockers" value={report.blockers.length} />
            <Statistic title="Errors" value={report.errors.length} />
          </Space>
          <Paragraph type="secondary" style={{ marginTop: 8 }}>
            {report.note} Digest <Text code>{report.digest.slice(0, 23)}…</Text>
          </Paragraph>
          <Paragraph style={{ marginBottom: 4 }}>
            {Object.entries(report.counts.by_owner).map(([k, v]) => (
              <Tag key={k}>
                {k}: {v}
              </Tag>
            ))}
          </Paragraph>
          <Table<Issue>
            size="small"
            rowKey={(r) => r.code + r.message}
            pagination={{ pageSize: 8 }}
            dataSource={[...report.errors, ...report.blockers]}
            columns={[
              { title: "Code", dataIndex: "code", render: (c: string) => <Tag color={report.errors.some((e) => e.code === c) ? "red" : "gold"}>{c}</Tag> },
              { title: "What to do", dataIndex: "message" },
            ]}
          />
        </Card>
      )}
      <Card size="small" title="Candidates">
        {items.length === 0 ? (
          <Empty description="No candidates yet." />
        ) : (
          <Table<Candidate>
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={items}
            columns={[
              { title: "Candidate", dataIndex: "id", render: (v: string) => <Text code>{v.slice(0, 8)}</Text> },
              { title: "Digest", dataIndex: "digest", render: (v: string) => <Text code>{v.slice(7, 19)}</Text> },
              { title: "Status", dataIndex: "status", render: (s: string) => <Tag color={s === "frozen" ? "green" : s === "blocked" ? "gold" : undefined}>{s}</Tag> },
              {
                title: "",
                render: (_, c) => {
                  const last = c.reviews[c.reviews.length - 1];
                  const approved = last && last.decision === "approve" && last.digest === c.digest ? last : undefined;
                  if (c.freeze) return <Text type="secondary">Frozen. Requirements fixed, implementation not yet qualified.</Text>;
                  return (
                    <Space>
                      {!approved && (
                        <Button
                          size="small"
                          onClick={() =>
                            void step(`review:${c.id}`, () =>
                              control(`/api/parity/candidates/${c.id}/reviews`, {
                                idempotencyKey: newKey(),
                                body: { digest: c.digest, expected_revision: c.revision, decision: "approve" },
                              }),
                            )
                          }
                        >
                          Approve this digest
                        </Button>
                      )}
                      {approved && (
                        <Button
                          size="small"
                          type="primary"
                          disabled={c.status === "blocked"}
                          title={c.status === "blocked" ? "Blockers remain: record a new candidate once they are resolved." : undefined}
                          onClick={() =>
                            void step(
                              `freeze:${c.id}`,
                              () => control(`/api/parity/candidates/${c.id}/freeze`, { idempotencyKey: newKey(), body: { digest: c.digest, review_id: approved.id, expected_revision: c.revision } }),
                              "Frozen.",
                            )
                          }
                        >
                          Freeze
                        </Button>
                      )}
                    </Space>
                  );
                },
              },
            ]}
          />
        )}
      </Card>
    </Space>
  );
}
