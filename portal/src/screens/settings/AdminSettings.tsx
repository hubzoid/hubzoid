// Console → Settings → Admin: admin.yaml (diagnostic limits) through a plan
// (exact digest and diff) then apply, and the registry of every settings
// domain with its file, owner, revision and the commands that change it.
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Descriptions, InputNumber, Space, Switch, Table, Tag, Typography } from "antd";
import { control, ControlError, newKey } from "../../lib/controlApi";

const { Text, Paragraph } = Typography;

type Diagnostics = {
  check_timeout_seconds: number;
  max_parallel_checks: number;
  report_retention_days: number | null;
  bundle_ttl_minutes: number;
  allow_network_probes: boolean;
};
type Shown = {
  revision: number;
  digest: string;
  values: { version: 1; diagnostics: Diagnostics };
  provenance: Record<string, { value: unknown; source: string }>;
  file_state: { exists: boolean; valid?: boolean; matches_active?: boolean };
};
type Plan = { plan_id: string; digest: string; base_revision: number; diff: { pointer: string; old: unknown; new: unknown }[]; expires_at: number };
type Domain = {
  domain: string;
  owner: string;
  file: string;
  revision?: number | string | null;
  read_only?: boolean;
  can_write?: boolean;
  restart_required?: boolean;
  cli?: string[];
  console?: string;
  note?: string;
  values?: { key: string; value?: string; source: string }[];
};

export function AdminSettings() {
  const { message } = App.useApp();
  const [shown, setShown] = useState<Shown | null>(null);
  const [draft, setDraft] = useState<Diagnostics | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [domains, setDomains] = useState<Domain[]>([]);
  const [error, setError] = useState<ControlError | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([control<Shown>("/api/admin/config"), control<{ items: Domain[] }>("/api/admin/domains")]);
      setShown(s);
      setDraft(s.values.diagnostics);
      setDomains(d.items);
      setPlan(null);
      setError(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const edit = (patch: Partial<Diagnostics>) => {
    setDraft((d) => (d ? { ...d, ...patch } : d));
    setPlan(null);
  };

  const makePlan = async () => {
    if (!shown || !draft) return;
    setBusy(true);
    try {
      setPlan(
        await control<Plan>("/api/admin/config/plans", {
          idempotencyKey: newKey(),
          body: { values: { version: 1, diagnostics: draft }, expected_revision: shown.revision },
        }),
      );
      setError(null);
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    if (!plan) return;
    setBusy(true);
    try {
      const res = await control<{ revision: number }>(`/api/admin/config/plans/${plan.plan_id}/apply`, {
        idempotencyKey: newKey(),
        body: { digest: plan.digest, expected_revision: plan.base_revision },
      });
      message.success(`Applied as revision ${res.revision}.`);
      await load();
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(false);
    }
  };

  if (!shown || !draft) return error ? <Alert type="error" showIcon title="Couldn't load admin settings" description={error.message} /> : <Card loading />;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error && (
        <Alert
          type="error"
          showIcon
          title={error.code === "revision_conflict" ? "The admin settings changed. Make a fresh plan." : "That didn't work"}
          description={error.message}
          action={<Button onClick={() => void load()}>Reload</Button>}
        />
      )}
      <Card title="Diagnostics (admin.yaml)" size="small" extra={<Text type="secondary">Revision {shown.revision}</Text>}>
        <Space direction="vertical">
          <label>
            Check timeout (seconds){" "}
            <InputNumber min={1} max={30} value={draft.check_timeout_seconds} onChange={(v) => edit({ check_timeout_seconds: Number(v) })} />
          </label>
          <label>
            Checks at once <InputNumber min={1} max={8} value={draft.max_parallel_checks} onChange={(v) => edit({ max_parallel_checks: Number(v) })} />
          </label>
          <label>
            Keep reports for (days, empty keeps them){" "}
            <InputNumber min={1} max={90} value={draft.report_retention_days ?? undefined} onChange={(v) => edit({ report_retention_days: v == null ? null : Number(v) })} />
          </label>
          <label>
            Support bundles expire after (minutes){" "}
            <InputNumber min={5} max={1440} value={draft.bundle_ttl_minutes} onChange={(v) => edit({ bundle_ttl_minutes: Number(v) })} />
          </label>
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Switch size="small" checked={draft.allow_network_probes} onChange={(v) => edit({ allow_network_probes: v })} />
            Allow network probes (each run still asks)
          </label>
          <Space>
            <Button onClick={() => void makePlan()} loading={busy && !plan}>
              Review changes
            </Button>
            <Button type="primary" disabled={!plan || plan.diff.length === 0} onClick={() => void apply()} loading={busy && !!plan}>
              Apply this plan
            </Button>
          </Space>
          {plan && (
            <Alert
              type="info"
              showIcon
              title={plan.diff.length ? `Plan ${plan.plan_id.slice(0, 8)}: ${plan.diff.length} change(s)` : "No changes"}
              description={
                <>
                  {plan.diff.map((c) => (
                    <div key={c.pointer}>
                      <Text code>{c.pointer}</Text>: {JSON.stringify(c.old)} → <strong>{JSON.stringify(c.new)}</strong>
                    </div>
                  ))}
                  <Text type="secondary">Digest {plan.digest.slice(0, 23)}… · expires in 30 minutes</Text>
                </>
              }
            />
          )}
        </Space>
      </Card>

      <Card title="All settings" size="small">
        <Paragraph type="secondary">
          Each area keeps its own file and rules. Process settings from the environment are shown read-only and need a
          restart to change.
        </Paragraph>
        <Table<Domain>
          size="small"
          rowKey="domain"
          pagination={false}
          dataSource={domains}
          columns={[
            { title: "Area", dataIndex: "domain", render: (d: string) => <Text strong>{d}</Text> },
            { title: "File", dataIndex: "file", render: (f: string) => <Text code>{f}</Text> },
            {
              title: "In effect",
              render: (_, d) =>
                d.values ? (
                  d.values.map((v) => (
                    <div key={v.key}>
                      <Text code>{v.key}</Text> {v.value ?? ""} <Tag>{v.source}</Tag>
                    </div>
                  ))
                ) : (
                  <>revision {String(d.revision ?? "—")}</>
                ),
            },
            {
              title: "Change with",
              render: (_, d) => (
                <>
                  {d.read_only && <Tag>read-only here</Tag>}
                  {d.restart_required && <Tag color="gold">restart</Tag>}
                  {d.cli?.slice(0, 1).map((c) => (
                    <div key={c}>
                      <Text code copyable>{c}</Text>
                    </div>
                  ))}
                  {d.note && <Text type="secondary">{d.note}</Text>}
                </>
              ),
            },
          ]}
        />
      </Card>
      <Descriptions size="small" bordered>
        <Descriptions.Item label="admin.yaml file">
          {shown.file_state.exists ? (shown.file_state.matches_active ? "matches the active revision" : "differs from the active revision") : "not present (defaults)"}
        </Descriptions.Item>
      </Descriptions>
    </Space>
  );
}
