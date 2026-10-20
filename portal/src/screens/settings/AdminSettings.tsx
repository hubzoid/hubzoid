// Console → Settings → Admin: the diagnostic limits (diagnostics: in
// hubzoid.yaml, read-only here) and the registry of every settings area with
// its file and the commands that show it.
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Descriptions, Space, Table, Tag, Typography } from "antd";
import { control, ControlError } from "../../lib/controlApi";

const { Text, Paragraph } = Typography;

type Diagnostics = {
  check_timeout_seconds: number;
  max_parallel_checks: number;
  bundle_ttl_minutes: number;
  allow_network_probes: boolean;
};
type Shown = {
  revision: number;
  file: string;
  values: { version: 1; diagnostics: Diagnostics };
  file_state: { exists: boolean; valid?: boolean; path?: string; error?: { message: string } };
};
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
  const [shown, setShown] = useState<Shown | null>(null);
  const [domains, setDomains] = useState<Domain[]>([]);
  const [error, setError] = useState<ControlError | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([control<Shown>("/api/admin/config"), control<{ items: Domain[] }>("/api/admin/domains")]);
      setShown(s);
      setDomains(d.items);
      setError(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  if (!shown) return error ? <Alert type="error" showIcon title="Couldn't load admin settings" description={error.message} /> : <Card loading />;
  const d = shown.values.diagnostics;
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        title="These settings live in files"
        description="Change them in hubzoid.yaml (or ask Claude Code or Codex to); every agent picks it up within a few seconds."
        action={<Button onClick={() => void load()}>Reload</Button>}
      />
      {shown.file_state.valid === false && (
        <Alert type="warning" showIcon title="The settings file has a problem" description={shown.file_state.error?.message} />
      )}
      <Card title={`Diagnostics (${shown.file})`} size="small">
        <Descriptions size="small" column={1}>
          <Descriptions.Item label="Check timeout">{d.check_timeout_seconds} seconds</Descriptions.Item>
          <Descriptions.Item label="Checks at once">{d.max_parallel_checks}</Descriptions.Item>
          <Descriptions.Item label="Support bundles expire after">{d.bundle_ttl_minutes} minutes</Descriptions.Item>
          <Descriptions.Item label="Network probes">{d.allow_network_probes ? "allowed (each run still asks)" : "off"}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="All settings" size="small">
        <Paragraph type="secondary">
          Each area has one home, a file. Process settings from the environment are shown too and need a restart to
          change.
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
    </Space>
  );
}
