// Console → Settings → Chat: the deployment's chat defaults, read-only. They
// live in hubzoid.yaml (chat:); this shows the values every agent reads and
// where they come from.
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Checkbox, Collapse, Descriptions, Input, Select, Space, Switch, Table, Tag, Typography, ConfigProvider } from "antd";
import { control, ControlError } from "../../lib/controlApi";

const { Text, Paragraph } = Typography;

type Config = {
  version: 1;
  presentation: {
    locale: { default: "en"; allowed: string[] };
    appearance: {
      default_mode: string;
      allowed_modes: string[];
      default_density: string;
      allowed_densities: string[];
      default_text_scale: number;
      allowed_text_scales: number[];
    };
    preferences: { mode: boolean; density: boolean; text_scale: boolean; timezone: boolean };
    timezone: string;
    theme_source: string | null;
  };
  [ns: string]: unknown;
};

type Active = {
  revision: number;
  digest: string;
  config: Config;
  applied_by: string | null;
  applied_at: number | null;
  file: { path: string; exists: boolean; valid?: boolean; matches_active?: boolean; pending_changes?: number; error?: { message: string } };
  sources: Record<string, { value: unknown; source: string }>;
};

type Change = { pointer: string; old: unknown; new: unknown };

const MODES = ["light", "dark", "oled", "system"];
const SCALES = [0.875, 1, 1.125, 1.25, 1.5, 1.75, 2];

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v));
}

export function ChatSettings() {
  const [active, setActive] = useState<Active | null>(null);
  const [draft, setDraft] = useState<Config | null>(null);
  const [changes, setChanges] = useState<Change[] | null>(null);
  const [error, setError] = useState<ControlError | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const a = await control<Active>("/api/chat/config?sources=true");
      setActive(a);
      setDraft(clone(a.config));
      setChanges(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);


  const edit = (fn: (c: Config) => void) => {
    setDraft((d) => {
      if (!d) return d;
      const next = clone(d);
      fn(next);
      return next;
    });
    setChanges(null);
    setDone(null);
  };

  if (!active || !draft)
    return error ? <Alert type="error" showIcon title="Couldn't load chat settings" description={error.message} /> : <Card loading />;

  const a = draft.presentation.appearance;
  const prefs = draft.presentation.preferences;
  return (
    <ConfigProvider componentDisabled>
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Text type="secondary" data-testid="chat-defaults-read-only">
        {active.file.exists
          ? <>Set in <Text code>{active.file.path}</Text>. Edit that file to change them. People can still change what it allows for themselves.</>
          : <>No chat settings yet, so the defaults apply. Add a <Text code>chat:</Text> section to <Text code>hubzoid.yaml</Text> to change them.</>}
      </Text>
      {active.file.valid === false && (
        <Alert type="warning" showIcon title="The settings file has a problem" description={(active.file as any).error?.message} />
      )}
      {error && (
        <Alert
          type="error"
          showIcon
          title={error.code === "revision_conflict" ? "Someone changed the chat settings since you loaded them" : "Couldn't apply"}
          description={error.message}
          action={error.code === "revision_conflict" ? <Button onClick={() => void load()}>Reload</Button> : undefined}
        />
      )}
      {done && <Alert type="success" showIcon title={done} />}

      <Card title="Appearance" size="small">
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
            <Text strong>Default appearance</Text>
            <br />
            <Select
              aria-label="Default appearance"
              style={{ width: 220 }}
              value={a.default_mode}
              options={a.allowed_modes.map((m) => ({ value: m, label: m === "oled" ? "Black (OLED)" : m[0].toUpperCase() + m.slice(1) }))}
              onChange={(v) => edit((c) => (c.presentation.appearance.default_mode = v))}
            />
          </div>
          <div>
            <Text strong>Appearances people may choose</Text>
            <br />
            <Checkbox.Group
              value={a.allowed_modes}
              options={MODES.map((m) => ({ value: m, label: m === "oled" ? "Black (OLED)" : m[0].toUpperCase() + m.slice(1) }))}
              onChange={(v) =>
                edit((c) => {
                  const list = MODES.filter((m) => (v as string[]).includes(m));
                  c.presentation.appearance.allowed_modes = list.length ? list : [c.presentation.appearance.default_mode];
                  if (!list.includes(c.presentation.appearance.default_mode) && list.length)
                    c.presentation.appearance.default_mode = list[0];
                })
              }
            />
          </div>
          <div>
            <Text strong>Default text size</Text>
            <br />
            <Select
              aria-label="Default text size"
              style={{ width: 220 }}
              value={a.default_text_scale}
              options={a.allowed_text_scales.map((s) => ({ value: s, label: `${Math.round(s * 100)}%` }))}
              onChange={(v) => edit((c) => (c.presentation.appearance.default_text_scale = v))}
            />{" "}
            <Select
              mode="multiple"
              aria-label="Allowed text sizes"
              style={{ minWidth: 260 }}
              value={a.allowed_text_scales}
              options={SCALES.map((s) => ({ value: s, label: `${Math.round(s * 100)}%` }))}
              onChange={(v) =>
                edit((c) => {
                  const list = [...(v as number[])].sort((x, y) => x - y);
                  c.presentation.appearance.allowed_text_scales = list.length ? list : [1];
                  if (!list.includes(c.presentation.appearance.default_text_scale))
                    c.presentation.appearance.default_text_scale = list[0] ?? 1;
                })
              }
            />
          </div>
          <div>
            <Text strong>Timezone</Text>
            <br />
            <Input
              aria-label="Timezone"
              style={{ width: 220 }}
              value={draft.presentation.timezone}
              onChange={(e) => edit((c) => (c.presentation.timezone = e.target.value))}
            />
            <Paragraph type="secondary" style={{ margin: 0 }}>For example: UTC or America/New_York.</Paragraph>
          </div>
        </Space>
      </Card>

      <Card title="Personal overrides" size="small">
        <Space direction="vertical">
          {(["mode", "text_scale", "density", "timezone"] as const).map((k) => (
            <label key={k} style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Switch
                size="small"
                checked={prefs[k]}
                onChange={(v) => edit((c) => (c.presentation.preferences[k] = v))}
              />
              {k === "mode" ? "Appearance" : k === "text_scale" ? "Text size" : k === "density" ? "Spacing" : "Timezone"}
              {!prefs[k] && <Tag>Managed by you for everyone</Tag>}
            </label>
          ))}
        </Space>
      </Card>

      {changes && (
        <Table<Change>
          size="small"
          rowKey="pointer"
          pagination={false}
          dataSource={changes}
          locale={{ emptyText: "No changes." }}
          columns={[
            { title: "Setting", dataIndex: "pointer", render: (p: string) => settingLabel(p) },
            { title: "Now", dataIndex: "old", render: (v) => <Text type="secondary">{plainValue(v)}</Text> },
            { title: "After apply", dataIndex: "new", render: (v) => <Text strong>{plainValue(v)}</Text> },
          ]}
        />
      )}

      <Collapse
        ghost
        size="small"
        items={[
          {
            key: "details",
            label: "Technical details",
            children: (
              <Space direction="vertical" size={12} style={{ width: "100%" }}>
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="Revision">{active.revision === 0 ? "Product defaults" : `#${active.revision}`}</Descriptions.Item>
                  <Descriptions.Item label="Digest">
                    <Text code copyable={{ text: active.digest }}>{active.digest.slice(0, 19)}…</Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="File">
                    <Text code>chat/config.yaml</Text>{" "}
                    {!active.file.exists
                      ? "not present"
                      : active.file.valid === false
                        ? `invalid: ${active.file.error?.message ?? ""}`
                        : active.file.matches_active
                          ? "matches what is applied"
                          : `${active.file.pending_changes} change(s) not applied (hubzoid chat config apply)`}
                  </Descriptions.Item>
                </Descriptions>
                <Table
                  size="small"
                  rowKey="pointer"
                  pagination={false}
                  tableLayout="fixed"
                  dataSource={Object.entries(active.sources).map(([pointer, v]) => ({ pointer, ...v }))}
                  columns={[
                    {
                      title: "Setting",
                      dataIndex: "pointer",
                      width: "45%",
                      render: (p: string) => (
                        <span>
                          {settingLabel(p)}
                          <br />
                          <Text type="secondary" style={{ fontSize: 11.5, fontFamily: "var(--font-mono, monospace)" }}>{p}</Text>
                        </span>
                      ),
                    },
                    { title: "Value", dataIndex: "value", render: (v) => plainValue(v) },
                    { title: "From", dataIndex: "source", width: 120, render: (src: string) => (src === "deployment" ? "Your settings" : "Default") },
                  ]}
                />
              </Space>
            ),
          },
        ]}
      />
    </Space>
    </ConfigProvider>
  );
}

const LABELS: Record<string, string> = {
  "/presentation/locale/default": "Language",
  "/presentation/locale/allowed": "Languages people may choose",
  "/presentation/appearance/default_mode": "Default theme",
  "/presentation/appearance/allowed_modes": "Themes people may choose",
  "/presentation/appearance/default_density": "Default spacing",
  "/presentation/appearance/allowed_densities": "Spacing people may choose",
  "/presentation/appearance/default_text_scale": "Default text size",
  "/presentation/appearance/allowed_text_scales": "Text sizes people may choose",
  "/presentation/timezone": "Time zone",
  "/presentation/preferences/mode": "People may change the theme",
  "/presentation/preferences/text_scale": "People may change the text size",
  "/presentation/preferences/density": "People may change the spacing",
  "/presentation/preferences/timezone": "People may change the time zone",
};

/** A plain name for a settings key: known ones by name, others from their last part. */
function settingLabel(pointer: string): string {
  if (LABELS[pointer]) return LABELS[pointer];
  const last = pointer.split("/").filter(Boolean).slice(-2).join(" ").replace(/_/g, " ");
  return last ? last[0].toUpperCase() + last.slice(1) : pointer;
}

function plainValue(v: unknown): string {
  if (v === true) return "On";
  if (v === false) return "Off";
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map((x) => plainValue(x)).join(", ");
  if (typeof v === "number") return v > 0 && v <= 3 ? `${Math.round(v * 100)}%` : String(v);
  if (typeof v === "string") return v === "oled" ? "Black" : v.length <= 20 ? v[0]?.toUpperCase() + v.slice(1) : v;
  return JSON.stringify(v);
}
