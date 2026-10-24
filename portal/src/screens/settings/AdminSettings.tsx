// Console → Settings → Admin: the diagnostic limits (diagnostics: in
// hubzoid.yaml, read-only here) and the registry of every settings area with
// its file and the commands that show it.
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Descriptions, Space, Table, Tag, Typography } from "antd";
import { control, ControlError } from "../../lib/controlApi";
import { InfoHelp, PAGED } from "../../components/common";

const { Text } = Typography;

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

/** Settings areas by the names Settings uses for them. */
const AREA_NAMES: Record<string, string> = {
  chat: "Chat defaults", branding: "Brand", admin: "Advanced", auth: "Sign-in", sign_in: "Sign-in",
  diagnostics: "Health", models: "Models", retrieval: "Sources", hitl: "Approvals",
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
      <Space size={6} align="center" style={{ width: "100%", justifyContent: "space-between" }}>
        <Text type="secondary">
          Set in <Text code>hubzoid.yaml</Text>.{" "}
          <InfoHelp text="Change them in hubzoid.yaml, or ask Claude Code or Codex to. Every agent picks up the change within a few seconds." />
        </Text>
        <Button size="small" onClick={() => void load()}>Reload</Button>
      </Space>
      {shown.file_state.valid === false && (
        <Alert type="warning" showIcon title="The settings file has a problem" description={shown.file_state.error?.message} />
      )}
      <Card title={shown.file && shown.file !== "default" ? `Diagnostics (${shown.file})` : "Diagnostics"} size="small">
        <Descriptions size="small" column={1}>
          <Descriptions.Item label="Check timeout">{d.check_timeout_seconds} seconds</Descriptions.Item>
          <Descriptions.Item label="Checks at once">{d.max_parallel_checks}</Descriptions.Item>
          <Descriptions.Item label="Support bundles expire after">{d.bundle_ttl_minutes} minutes</Descriptions.Item>
          <Descriptions.Item label="Network probes">{d.allow_network_probes ? "allowed (each run still asks)" : "off"}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="All settings" size="small">
        {/* One row per area: where it is set and the command that shows it.
            Everything here is read-only (founder review, HUB-140). */}
        <Table<Domain>
          size="small"
          rowKey="domain"
          pagination={PAGED}
          dataSource={domains}
          columns={[
            {
              title: "Area",
              dataIndex: "domain",
              render: (_: string, d) => (
                <Space size={4}>
                  <Text strong>{AREA_NAMES[d.domain] ?? d.domain.charAt(0).toUpperCase() + d.domain.slice(1)}</Text>
                  {d.note && <InfoHelp text={d.note} />}
                </Space>
              ),
            },
            {
              title: "Set in",
              dataIndex: "file",
              render: (f: string, d) => (
                <Space size={6} wrap>
                  {!f || f === "default" ? <Text type="secondary">Not set, defaults apply</Text> : <Text code>{f}</Text>}
                  {d.restart_required && <Tag>needs a restart</Tag>}
                </Space>
              ),
            },
            {
              title: "See it with",
              render: (_, d) => d.cli?.[0] ? <Text code copyable className="hz-mono-sm">{d.cli[0]}</Text> : <Text type="secondary">—</Text>,
            },
          ]}
        />
      </Card>
    </Space>
  );
}
