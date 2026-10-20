import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Tooltip, Typography } from "antd";
import { Lock, Plus } from "lucide-react";
import { ApiError, secretsRequest } from "../api";
import type { Hub, SecretList, SecretLevel, SecretRow } from "../api";
import { InfoHelp, LoadState } from "../components/common";
import { errorText } from "../hooks/useData";

const { Title, Text } = Typography;

const LEVELS: Record<SecretLevel, string> = {
  deployment: "Shared by all agents",
  hub: "This agent",
  restricted: "Restricted tools only",
};
const STATUS: Record<SecretRow["status"], { label: string; color: string }> = {
  set: { label: "Set", color: "green" },
  missing: { label: "Not set", color: "orange" },
  blank: { label: "Empty", color: "orange" },
  unreadable: { label: "Cannot be read: set again", color: "red" },
  rebind: { label: "Set again for this destination", color: "red" },
};
const WHY: Record<string, string> = {
  model: "Needed by this agent's model",
  webhook: "Needed to verify a webhook",
  declared: "Listed in the agent's .env.example",
  setting: "Declared in the agent's settings.yaml",
};

type Editing = { mode: "add" } | { mode: "set"; row: SecretRow } | null;

/** Agents → agent → Secrets: write-only values, their sources and locks. */
export function AgentSecrets({ hub }: { hub: Hub }) {
  const url = `/api/hubs/${encodeURIComponent(hub.key)}/secrets`;
  const [state, setState] = useState<{ data?: SecretList; error?: string }>({});
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<Editing>(null);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const reload = useCallback(() => setRevision((r) => r + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    secretsRequest<SecretList>(url, "GET", undefined, controller.signal)
      .then((data) => !controller.signal.aborted && setState({ data }))
      .catch((e) => !controller.signal.aborted && setState({ error: errorText(e) }));
    return () => controller.abort();
  }, [url, revision]);

  // A stored secret is changed where it is stored; a new one where it will be used.
  const target = (row: { name: string; level: SecretLevel; stored?: SecretRow["stored"] }) =>
    (row.stored ? row.stored.scope === "deployment" : row.level === "deployment")
      ? `/api/secrets/${encodeURIComponent(row.name)}`
      : `${url}/${encodeURIComponent(row.name)}`;

  const remove = async (row: SecretRow) => {
    if (!row.stored) return;
    try {
      await secretsRequest(`${target(row)}?expected_revision=${row.stored.revision}`, "DELETE");
      setNotice({ type: "success", text: `${row.name} removed. The agent stops using it after it restarts.` });
    } catch (e) {
      setNotice({ type: "error", text: errorText(e) });
    }
    reload();
  };

  if (!state.data) return <LoadState error={state.error} retry={reload} />;
  const { secrets, restricted_hidden, can_manage } = state.data;

  return (
    <div className="panel">
      <Title level={2}>
        Secrets
        <InfoHelp text="API keys and tokens this agent uses. The same list as hubzoid secrets list." />
      </Title>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        title="Values are write-only. Once saved, nobody can read them back here."
        description="A saved value applies after the agent restarts. A value set in the environment, a .env file or an AWS secret always wins and shows as locked."
      />
      {restricted_hidden > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title={`${restricted_hidden} restricted secret${restricted_hidden === 1 ? "" : "s"} not shown`}
          description="Restricted tools use them. Organization administrators manage them."
        />
      )}
      {notice && (
        <Alert type={notice.type} showIcon closable onClose={() => setNotice(null)} style={{ marginBottom: 16 }}
          title={notice.text} />
      )}
      {can_manage && (
        <Space style={{ marginBottom: 12 }}>
          <Button type="primary" icon={<Plus size={15} />} onClick={() => setEditing({ mode: "add" })}>
            Add secret
          </Button>
        </Space>
      )}
      <Table<SecretRow>
        rowKey={(r) => `${r.level}:${r.name}`}
        size="small"
        pagination={false}
        dataSource={secrets}
        locale={{ emptyText: "No secrets are set or expected for this agent. Add one, or list the names it needs in .env.example." }}
        columns={[
          {
            title: "Name",
            key: "name",
            render: (_, r) => (
              <Space direction="vertical" size={0}>
                <Text strong code>{r.name}</Text>
                {r.expected && r.status !== "set" && <Text type="secondary">{WHY[r.expected] ?? r.expected}</Text>}
              </Space>
            ),
          },
          { title: "Used by", key: "level", render: (_, r) => LEVELS[r.level] },
          {
            title: "Status",
            key: "status",
            render: (_, r) => (
              <Space size={4} wrap>
                <Tag color={STATUS[r.status].color}>{STATUS[r.status].label}</Tag>
                {r.restart_needed && (
                  <Tooltip title="Saved or removed since the agent started. Restart it to apply.">
                    <Tag>Restart to apply</Tag>
                  </Tooltip>
                )}
              </Space>
            ),
          },
          {
            title: "Source",
            key: "source",
            render: (_, r) => (
              <Space direction="vertical" size={0}>
                <Space size={4}>
                  {r.locked && (
                    <Tooltip title={r.lock_reason}>
                      <Lock size={13} aria-label="Locked" />
                    </Tooltip>
                  )}
                  <span>{r.source ?? "None"}</span>
                </Space>
                {r.stored_status === "overridden" && (
                  <Text type="secondary">Stored value unused: {r.locked ? r.source : "this agent stores its own"}</Text>
                )}
              </Space>
            ),
          },
          {
            title: "Updated",
            key: "updated",
            render: (_, r) =>
              r.stored ? (
                <Text type="secondary">
                  {r.stored.updated_by}, {new Date(r.stored.updated_at).toLocaleString()}
                </Text>
              ) : null,
          },
          ...(can_manage
            ? [
                {
                  title: "",
                  key: "actions",
                  render: (_: unknown, r: SecretRow) =>
                    r.locked ? (
                      <Tooltip title={r.lock_reason}>
                        <Button size="small" disabled>Locked</Button>
                      </Tooltip>
                    ) : (
                      <Space size={4}>
                        <Button size="small" onClick={() => setEditing({ mode: "set", row: r })}>
                          {r.stored ? "Replace" : "Set"}
                        </Button>
                        {r.stored && (
                          <Popconfirm
                            title={`Remove ${r.name}?`}
                            description="The agent stops using it after it restarts."
                            okText="Remove"
                            okButtonProps={{ danger: true }}
                            onConfirm={() => remove(r)}
                          >
                            <Button size="small" danger>Remove</Button>
                          </Popconfirm>
                        )}
                      </Space>
                    ),
                },
              ]
            : []),
        ]}
      />
      <Text type="secondary" style={{ display: "block", marginTop: 12 }}>
        From a terminal: <Text code>hubzoid secrets list {hub.key}</Text>
      </Text>
      {editing && (
        <SecretEditor
          hub={hub}
          editing={editing}
          urlFor={target}
          onClose={(saved) => {
            setEditing(null);
            if (saved) {
              setNotice({ type: "success", text: `${saved} saved. It applies after the agent restarts.` });
              reload();
            }
          }}
        />
      )}
    </div>
  );
}

function SecretEditor({
  hub,
  editing,
  urlFor,
  onClose,
}: {
  hub: Hub;
  editing: Exclude<Editing, null>;
  urlFor: (row: { name: string; level: SecretLevel; stored?: SecretRow["stored"] }) => string;
  onClose: (saved?: string) => void;
}) {
  const row = editing.mode === "set" ? editing.row : null;
  const [form] = Form.useForm<{ name: string; level: SecretLevel; value: string }>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const values = await form.validateFields();
    const name = row ? row.name : values.name.trim();
    const level = row ? row.level : values.level;
    setSaving(true);
    setError(null);
    try {
      const stored = row?.stored;
      const body: Record<string, unknown> = {
        value: values.value,
        expected_revision: stored?.revision ?? "absent",
      };
      // A shared secret is never restricted; a replaced one keeps its setting.
      if (!stored && level !== "deployment") body.restricted = level === "restricted";
      await secretsRequest(urlFor({ name, level, stored }), "PUT", body);
      onClose(name);
    } catch (e) {
      setError(e instanceof ApiError && e.code === "revision_conflict"
        ? "Someone changed this secret since you opened it. Close this and try again."
        : errorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={row ? `${row.stored ? "Replace" : "Set"} ${row.name}` : "Add a secret"}
      okText="Save"
      confirmLoading={saving}
      onOk={save}
      onCancel={() => onClose()}
      destroyOnHidden
    >
      {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 12 }} />}
      <Form form={form} layout="vertical" initialValues={{ level: "hub" }} requiredMark={false}>
        {!row && (
          <>
            <Form.Item
              name="name"
              label="Name"
              rules={[
                { required: true, message: "Enter the name the agent reads, for example OPENAI_API_KEY." },
                { pattern: /^[A-Za-z_][A-Za-z0-9_]*$/, message: "Use letters, digits and underscores." },
              ]}
            >
              <Input placeholder="OPENAI_API_KEY" autoComplete="off" />
            </Form.Item>
            <Form.Item name="level" label="Used by">
              <Select
                options={(Object.keys(LEVELS) as SecretLevel[]).map((value) => ({ value, label: LEVELS[value] }))}
              />
            </Form.Item>
          </>
        )}
        <Form.Item
          name="value"
          label="Value"
          extra="Nobody can read it back after you save it."
          rules={[{ required: true, whitespace: true, message: "Enter the value." }]}
        >
          <Input.Password autoComplete="new-password" visibilityToggle={false} />
        </Form.Item>
      </Form>
      <Text type="secondary">
        From a terminal: <Text code>hubzoid secrets set {hub.key} --name {row?.name ?? "NAME"}</Text>
      </Text>
    </Modal>
  );
}
