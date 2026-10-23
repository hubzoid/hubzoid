// Settings → Brand: what people see, and where it comes from. The brand lives
// in the deployment folder (`brand:` in hubzoid.yaml, files in branding/), so
// this view is read-only: change the files, or ask Claude Code or Codex to.
// Every agent picks the change up within a few seconds; history is Git.
import { useEffect, useState } from "react";
import { InfoHelp } from "../../components/common";
import { Alert, Button, Card, Descriptions, Space, Table, Typography } from "antd";
import { control } from "../../lib/controlApi";

const { Text } = Typography;

type Asset = { key: string; path: string; digest: string; media_type: string; generated?: boolean };
type Source = {
  folder: string;
  file: string;
  file_exists: boolean;
  how_to_change: string;
  active: { revision: string | null; source?: string; valid?: boolean; error?: string | null };
  legacy: { file: string; disposition: string; to?: string; reason?: string }[];
};
type Active = { configuration: Record<string, any>; assets: Asset[]; source: string; error?: string | null } | null;

export function Appearance() {
  const [source, setSource] = useState<Source | null>(null);
  const [active, setActive] = useState<Active>(null);
  const [error, setError] = useState<Error | null>(null);

  const load = async () => {
    try {
      const [s, a] = await Promise.all([
        control<Source>("/api/branding/source"),
        control<{ active: Active }>("/api/branding/active"),
      ]);
      setSource(s);
      setActive(a.active);
      setError(null);
    } catch (e) {
      setError(e as Error);
    }
  };
  useEffect(() => {
    void load();
  }, []);

  if (!source) return error ? <Alert type="error" showIcon title="Couldn't load the brand" description={error.message} /> : <Card loading />;
  const identity = active?.configuration?.identity ?? {};
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }} data-testid="brand-read-only">
      {/* One quiet line; the how-to and the folder sit behind the ? (founder review). */}
      <Space size={6} align="center" style={{ width: "100%", justifyContent: "space-between" }}>
        <Text type="secondary">
          The brand comes from <Text code>hubzoid.yaml</Text> and <Text code>branding/</Text>.{" "}
          <InfoHelp text={`${source.how_to_change} Folder: ${source.folder}`} />
        </Text>
        <Button size="small" onClick={() => void load()}>Reload</Button>
      </Space>
      {active?.error || source.active.error ? (
        <Alert type="warning" showIcon title="The brand file has a problem" description={active?.error ?? source.active.error} />
      ) : null}
      {active ? (
        <Card size="small" title={`From ${active.source}`}>
          <Descriptions size="small" column={1}>
            <Descriptions.Item label="Name">{identity.name ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="Short name">{identity.short_name ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="Description">{identity.description ?? "—"}</Descriptions.Item>
          </Descriptions>
          <Table
            size="small"
            rowKey="key"
            pagination={false}
            dataSource={active.assets}
            columns={[
              { title: "Slot", dataIndex: "key" },
              { title: "File", dataIndex: "path", render: (p: string, a: Asset) => (a.generated ? <Text type="secondary">made from the favicon</Text> : <Text code>{p}</Text>) },
              { title: "Type", dataIndex: "media_type" },
            ]}
          />
        </Card>
      ) : (
        <Card size="small">
          <Text type="secondary">No brand set yet, so the default brand shows.</Text>
        </Card>
      )}
      {source.legacy.length > 0 && (
        <Card size="small" title="Files in branding/">
          <Table
            size="small"
            rowKey="file"
            pagination={false}
            dataSource={source.legacy}
            columns={[
              { title: "File", dataIndex: "file" },
              { title: "Used", dataIndex: "disposition" },
              { title: "Note", render: (_: unknown, r) => r.to ?? r.reason ?? "" },
            ]}
          />
        </Card>
      )}
    </Space>
  );
}
