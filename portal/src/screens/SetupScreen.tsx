import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Form, Input, InputNumber, Select, Space, Switch, Table, Tag, Tooltip, Typography } from "antd";
import { ApiError, hubRequest } from "../api";
import type { Hub, HubCheck, HubSetting, HubSettings, SettingProblem } from "../api";
import { InfoHelp, LoadState, PAGED } from "../components/common";
import { errorText } from "../hooks/useData";
import { checkStatus, optionLabel, settingLabel, settingPrefix } from "../lib/format";
import { useAgentEditing } from "../app/lib/editing";

const { Title, Text } = Typography;

function control(s: HubSetting) {
  if (s.enum) return <Select allowClear={!s.required} options={s.enum.map((v) => ({ value: v, label: optionLabel(v) }))} />;
  if (s.type === "integer" || s.type === "number")
    return <InputNumber style={{ width: "100%" }} precision={s.type === "integer" ? 0 : undefined} />;
  if (s.type === "boolean") return <Switch />;
  if (s.type === "string_list") return <Select mode="tags" tokenSeparators={[","]} open={false} />;
  return <Input placeholder={s.example != null ? `For example: ${String(s.example)}` : undefined} autoComplete="off" />;
}

/** Agents → agent → Setup: the settings the agent declares, and its own checks. */
export function AgentSetup({ hub }: { hub: Hub }) {
  const editing = useAgentEditing();
  const base = `/api/hubs/${encodeURIComponent(hub.key)}`;
  const [state, setState] = useState<{ data?: HubSettings; error?: string }>({});
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [fieldProblems, setFieldProblems] = useState<SettingProblem[]>([]);
  const [checks, setChecks] = useState<{ running: boolean; data?: HubCheck[]; error?: string }>({ running: false });
  const [form] = Form.useForm();
  const reload = useCallback(() => setRevision((r) => r + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    hubRequest<HubSettings>(`${base}/settings`, "GET", undefined, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setState({ data });
        form.setFieldsValue(Object.fromEntries(data.settings.filter((s) => !s.secret).map((s) => [s.name, s.value])));
      })
      .catch((e) => !controller.signal.aborted && setState({ error: errorText(e) }));
    return () => controller.abort();
  }, [base, revision, form]);

  if (!state.data) return <LoadState error={state.error} retry={reload} />;
  const { settings, problems } = state.data;
  const plain = settings.filter((s) => !s.secret);
  const secrets = settings.filter((s) => s.secret);
  // Labels, not keys: the key shows on hover for whoever edits the files.
  const prefix = settingPrefix(settings.map((s) => s.name));
  const nameOf = (s: HubSetting) => <span title={s.name}>{s.label || settingLabel(s.name, prefix)}</span>;

  const save = async () => {
    const entered = await form.validateFields();
    // Start from what hub.settings.yaml holds (even where the environment wins
    // for now) and change only the fields someone edited.
    const values: Record<string, unknown> = Object.fromEntries(
      plain.filter((s) => s.stored !== undefined && s.stored !== null).map((s) => [s.name, s.stored]));
    for (const s of plain) {
      if (!form.isFieldTouched(s.name)) continue;
      const v = entered[s.name];
      if (v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0)) delete values[s.name];
      else values[s.name] = v;
    }
    const refs = Object.fromEntries(
      secrets.filter((s) => s.ref && s.source === "hub.settings.yaml").map((s) => [`${s.name}_ref`, s.ref]));
    setSaving(true);
    setNotice(null);
    setFieldProblems([]);
    try {
      const done = await hubRequest<{ revision: number; changed_keys: string[] }>(`${base}/settings`, "PUT", {
        version: 1, values, secrets: refs, expected_revision: state.data!.revision || "absent",
      });
      setNotice({
        type: "success",
        text: done.changed_keys.length
          ? "Saved. Applies after a restart."
          : "Nothing changed.",
      });
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.code === "validation_failed" && Array.isArray(e.data.fields))
        setFieldProblems(e.data.fields as SettingProblem[]);
      setNotice({
        type: "error",
        text: e instanceof ApiError && e.code === "revision_conflict"
          ? "Changed by someone else. Reload and try again."
          : errorText(e),
      });
    } finally {
      setSaving(false);
    }
  };

  const runChecks = async () => {
    setChecks({ running: true, data: checks.data });
    try {
      const done = await hubRequest<{ checks: HubCheck[] }>(`${base}/doctor`, "POST", {});
      setChecks({ running: false, data: done.checks });
    } catch (e) {
      setChecks({ running: false, data: checks.data, error: errorText(e) });
    }
  };

  const problemFor = (name: string) =>
    [...fieldProblems, ...problems].filter((p) => p.key === name).map((p) => p.message)[0];

  return (
    <div className="panel">
      <Title level={2}>
        Setup
        <InfoHelp text={`Declared in settings.yaml; hover a setting for its key. CLI: hubzoid hub settings ${hub.key}, hubzoid hub doctor ${hub.key}.`} />
      </Title>
      {notice && (
        <Alert type={notice.type} showIcon closable onClose={() => setNotice(null)} style={{ marginBottom: 16 }}
          title={notice.text} />
      )}
      {settings.length === 0 ? (
        <Text type="secondary" className="hint">This agent has no settings.</Text>
      ) : (
        <>
          {problems.length > 0 && (
            <Alert type="warning" showIcon style={{ marginBottom: 16 }}
              title={`Setup needs attention: ${problems.length} problem${problems.length === 1 ? "" : "s"}`}
              description={<ul style={{ margin: 0, paddingLeft: 18 }}>{problems.map((p, i) => <li key={i}>{p.message}</li>)}</ul>} />
          )}
          {plain.length > 0 && (
            <Form form={form} layout="vertical" requiredMark style={{ maxWidth: 640 }} disabled={!editing.editable}>
              {plain.map((s) => (
                <Form.Item
                  key={s.name}
                  name={s.name}
                  label={
                    <Space size={6}>
                      {nameOf(s)}
                      {s.source === "environment" && (
                        <Tooltip title={`${s.env ?? s.name} on the server overrides the saved value.`}>
                          <Tag>Set by the server</Tag>
                        </Tooltip>
                      )}
                    </Space>
                  }
                  extra={s.description}
                  required={s.required}
                  valuePropName={s.type === "boolean" ? "checked" : "value"}
                  validateStatus={problemFor(s.name) ? "error" : undefined}
                  help={problemFor(s.name)}
                >
                  {control(s)}
                </Form.Item>
              ))}
              {editing.editable ? (
                <>
                  <Button type="primary" loading={saving} onClick={save}>Save settings</Button>
                  <Text type="secondary" style={{ marginLeft: 12 }}>Applies after a restart.</Text>
                </>
              ) : (
                <Text type="secondary">{editing.message ?? "Read-only here."}</Text>
              )}
            </Form>
          )}
          {secrets.length > 0 && (
            <>
              <Title level={4} style={{ marginTop: 24 }}>Secret settings</Title>
              <Text type="secondary">Values stay hidden. Set on the server.</Text>
              <Table<HubSetting>
                rowKey="name" size="small" pagination={PAGED} style={{ marginTop: 8 }} dataSource={secrets}
                columns={[
                  { title: "Name", key: "name", render: (_, s) =>
                    <span title={`${s.name} · ${s.ref}`}>{s.label || settingLabel(s.name, prefix)}</span> },
                  { title: "Status", key: "status", render: (_, s) =>
                    <Tag color={s.available ? "green" : s.required ? "red" : "orange"}>{s.available ? "Available" : "Not set"}</Tag> },
                  { title: "", key: "why", render: (_, s) => <Text type="secondary">{s.description}</Text> },
                ]}
              />
            </>
          )}
        </>
      )}
      <Title level={4} style={{ marginTop: 24 }}>Checks</Title>
      <Button onClick={runChecks} loading={checks.running} style={{ marginBottom: 8 }}>Run checks</Button>
      {checks.error && <Alert type="error" showIcon title={checks.error} style={{ marginBottom: 8 }} />}
      {checks.data && (
        <Table<HubCheck>
          rowKey="id" size="small" pagination={PAGED} dataSource={checks.data}
          locale={{ emptyText: "No checks." }}
          columns={[
            { title: "Check", key: "id", render: (_, c) => <span title={c.id}>{optionLabel(c.id.replaceAll(".", " "))}</span> },
            { title: "Result", key: "status", render: (_, c) => {
              const r = checkStatus(c.status);
              return <Tag color={r.color}>{r.label}</Tag>;
            } },
            {
              title: "Summary", key: "summary", render: (_, c) => {
                const recovery = c.detail && typeof c.detail === "object" && "recovery" in c.detail
                  ? String((c.detail as { recovery?: unknown }).recovery ?? "") : "";
                return (
                  <Space direction="vertical" size={0}>
                    <span>{c.summary}</span>
                    {recovery && c.status !== "ok" && <Text type="secondary">{recovery}</Text>}
                  </Space>
                );
              },
            },
          ]}
        />
      )}
    </div>
  );
}
