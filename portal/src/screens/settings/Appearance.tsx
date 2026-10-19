// Console → Settings → Appearance: the deployment brand's publication.
// Drafts are complete candidates with a digest; a person reviews the exact
// digest, then activates. Rollback makes a new draft from an older revision
// that goes through the same review. The CLI (`hubzoid branding ...`) and
// the API do the same things with the same checks.
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Form, Input, Modal, Space, Table, Tag, Typography, Upload } from "antd";
import { control, ControlError, newKey } from "../../lib/controlApi";
import { refreshTheme } from "../../lib/presentation";

const { Text, Paragraph } = Typography;

type Asset = { key: string; path: string; digest: string; media_type: string; generated?: boolean };
type Draft = {
  id: string;
  revision: number;
  state: string;
  digest: string;
  source: string;
  creator: string;
  created_at: number;
  updated_at: number;
  validation: { valid: boolean; problems: { code: string; message: string }[] };
  configuration: Record<string, any>;
  assets: Asset[];
  preview_url: string;
  review: { review_id: string; decision: string; actor: string } | null;
  needs_revalidation?: boolean;
};
type HistoryItem = {
  activation_id: string;
  to_revision: string;
  from_revision: string | null;
  actor: string;
  kind: string;
  generation: number;
  created_at: number;
  digest: string;
  name: string | null;
};
type ActiveSource = {
  revision: string;
  generation: number;
  digest: string;
  configuration: Record<string, any>;
  assets: Asset[];
} | null;
type Change = { pointer: string; old: unknown; new: unknown };
type DiffResult = { configuration: Change[]; theme: Change[]; assets: { key: string; old: string | null; new: string | null }[] };

const when = (ts: number) => new Date(ts * 1000).toLocaleString();

function stateTag(state: string) {
  const color =
    state === "valid" ? "blue" : state === "reviewed" ? "green" : state === "activated" ? "default" : state === "rejected" ? "red" : "orange";
  const label =
    state === "draft" ? "Needs changes" : state === "valid" ? "Ready for review" : state === "reviewed" ? "Approved"
      : state === "activated" ? "Published" : state === "rejected" ? "Rejected" : state;
  return <Tag color={color}>{label}</Tag>;
}

async function fileB64(file: File): Promise<string> {
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(bin);
}

export function Appearance() {
  const { message, modal } = App.useApp();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [active, setActive] = useState<ActiveSource>(null);
  const [error, setError] = useState<ControlError | null>(null);
  const [diff, setDiff] = useState<{ draft: Draft; diff: DiffResult } | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, h, a] = await Promise.all([
        control<{ items: Draft[] }>("/api/branding/drafts"),
        control<{ items: HistoryItem[] }>("/api/branding/history"),
        control<{ active: ActiveSource }>("/api/branding/active"),
      ]);
      setDrafts(d.items);
      setHistory(h.items);
      setActive(a.active);
      setError(null);
    } catch (e) {
      setError(e as ControlError);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const act = async (label: string, fn: () => Promise<unknown>, success: string) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
      message.success(success);
      await load();
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(null);
    }
  };

  const fromSource = () =>
    act("source", () => control("/api/branding/drafts/from-source", { method: "POST", idempotencyKey: newKey() }), "Draft created from branding.yaml.");

  const showDiff = async (draft: Draft) => {
    try {
      setDiff({ draft, diff: await control<DiffResult>(`/api/branding/drafts/${draft.id}/diff`) });
    } catch (e) {
      setError(e as ControlError);
    }
  };

  const approve = (draft: Draft) =>
    act(
      `review:${draft.id}`,
      () =>
        control(`/api/branding/drafts/${draft.id}/reviews`, {
          idempotencyKey: newKey(),
          body: { digest: draft.digest, expected_revision: draft.revision, decision: "approve" },
        }),
      `Approved digest ${draft.digest.slice(7, 19)}.`,
    );

  const activate = (draft: Draft) =>
    modal.confirm({
      title: "Activate this brand for everyone?",
      content: "Sign-in, chat, shared pages and the Console switch to this exact reviewed revision. You can roll back later.",
      okText: "Activate",
      onOk: () =>
        act(
          `activate:${draft.id}`,
          async () => {
            await control(`/api/branding/drafts/${draft.id}/activate`, {
              idempotencyKey: newKey(),
              body: { review_id: draft.review?.review_id, expected_revision: active?.generation ?? 0 },
            });
            await refreshTheme();
            window.dispatchEvent(new Event("hz:brand-changed"));
          },
          "Brand activated.",
        ),
    });

  const rollback = (item: HistoryItem) =>
    act(
      `rollback:${item.activation_id}`,
      () => control("/api/branding/rollbacks", { idempotencyKey: newKey(), body: { target_revision: item.to_revision } }),
      "Rollback draft created. Review and activate it to switch back.",
    );

  if (!drafts) return error ? <Alert type="error" showIcon title="Couldn't load branding" description={error.message} /> : <Card loading />;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Text type="secondary">
        {active ? (
          <>
            Published brand: <Text strong>{active.configuration.identity?.name}</Text>. Changes are drafts until someone
            approves and publishes them.
          </>
        ) : (
          "Using Hubzoid branding. Edit the brand to set your name, logo and colours."
        )}
      </Text>

      {error && (
        <Alert
          type={error.code === "revision_conflict" ? "warning" : "error"}
          showIcon
          title={error.code === "revision_conflict" ? "Something changed since you loaded this" : "That didn't work"}
          description={error.message}
          action={<Button onClick={() => void load()}>Reload</Button>}
        />
      )}

      <Space wrap>
        <Button type="primary" onClick={() => setEditing(true)}>
          Edit brand
        </Button>
        <Button onClick={() => void fromSource()} loading={busy === "source"}>
          Import brand settings
        </Button>
        {active && (
          <Button href={`/api/branding/revisions/${active.revision}/export`}>Export active files</Button>
        )}
      </Space>

      <Card title="Drafts" size="small">
        <Table<Draft>
          size="small"
          rowKey="id"
          dataSource={drafts}
          pagination={false}
          locale={{ emptyText: "No drafts yet." }}
          columns={[
            { title: "Draft", render: (_, d) => d.configuration.identity?.name },
            {
              title: "State",
              render: (_, d) => (
                <>
                  {stateTag(d.state)}
                  {d.needs_revalidation && d.state !== "activated" && <Tag color="orange">checks changed: revalidate</Tag>}
                </>
              ),
            },
            { title: "Updated", render: (_, d) => when(d.updated_at) },
            {
              title: "",
              render: (_, d) => (
                <Space wrap size={4}>
                  <Button size="small" href={d.preview_url} target="_blank">
                    Preview
                  </Button>
                  <Button size="small" onClick={() => void showDiff(d)}>
                    Changes
                  </Button>
                  {d.needs_revalidation && d.state !== "activated" && (
                    <Button
                      size="small"
                      onClick={() =>
                        void act(`revalidate:${d.id}`, () => control(`/api/branding/drafts/${d.id}/validate`, { method: "POST", idempotencyKey: newKey() }), "Draft checked again.")
                      }
                    >
                      Revalidate
                    </Button>
                  )}
                  {d.state === "valid" && (
                    <Button size="small" onClick={() => void approve(d)} loading={busy === `review:${d.id}`}>
                      Approve draft
                    </Button>
                  )}
                  {d.state === "reviewed" && d.review?.decision === "approve" && (
                    <Button size="small" type="primary" onClick={() => activate(d)} loading={busy === `activate:${d.id}`}>
                      Activate
                    </Button>
                  )}
                  {d.state === "draft" && d.validation.problems.length > 0 && (
                    <Button size="small" type="text" onClick={() => void showDiff(d)}>
                      <Text type="warning">
                        {d.validation.problems.length === 1 ? "1 issue" : `${d.validation.problems.length} issues`}
                      </Text>
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Card title="Published before" size="small">
        <Table<HistoryItem>
          size="small"
          rowKey="activation_id"
          dataSource={history}
          pagination={false}
          locale={{ emptyText: "Nothing activated yet." }}
          columns={[
            { title: "Brand", dataIndex: "name" },
            { title: "By", dataIndex: "actor" },
            { title: "When", render: (_, h) => when(h.created_at) },
            {
              title: "",
              render: (_, h) =>
                h.to_revision === active?.revision ? (
                  <Tag color="green">Published</Tag>
                ) : (
                  <Button size="small" onClick={() => void rollback(h)} loading={busy === `rollback:${h.activation_id}`}>
                    Roll back to this
                  </Button>
                ),
            },
          ]}
        />
      </Card>

      <Modal open={!!diff} onCancel={() => setDiff(null)} footer={null} title="Changes against the active brand" width={720}>
        {diff && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Paragraph type="secondary" style={{ margin: 0 }}>
              Approving covers exactly these changes. Editing again needs a new approval.
            </Paragraph>
            <Table<Change>
              size="small"
              rowKey="pointer"
              pagination={false}
              dataSource={[...diff.diff.configuration, ...diff.diff.theme.map((c) => ({ ...c, pointer: `theme${c.pointer}` }))]}
              locale={{ emptyText: "No setting changes." }}
              columns={[
                { title: "Setting", dataIndex: "pointer", render: (p: string) => <Text code>{p}</Text> },
                { title: "Now", dataIndex: "old", render: (v) => <Text type="secondary">{JSON.stringify(v)}</Text> },
                { title: "After", dataIndex: "new", render: (v) => <Text strong>{JSON.stringify(v)}</Text> },
              ]}
            />
            {diff.diff.assets.length > 0 && (
              <Table
                size="small"
                rowKey="key"
                pagination={false}
                dataSource={diff.diff.assets}
                columns={[
                  { title: "File", dataIndex: "key" },
                  { title: "Now", dataIndex: "old", render: (v) => (v ? <Text code>{String(v).slice(7, 19)}</Text> : "—") },
                  { title: "After", dataIndex: "new", render: (v) => (v ? <Text code>{String(v).slice(7, 19)}</Text> : "removed") },
                ]}
              />
            )}
          </Space>
        )}
      </Modal>

      {editing && (
        <EditBrand
          active={active}
          onClose={() => setEditing(false)}
          onCreated={async () => {
            setEditing(false);
            message.success("Draft created. Preview it, approve the digest, then activate.");
            await load();
          }}
        />
      )}
    </Space>
  );
}

function EditBrand({ active, onClose, onCreated }: { active: ActiveSource; onClose: () => void; onCreated: () => void }) {
  const base = active?.configuration ?? { version: 1 };
  const en = base.copy?.en ?? {};
  const [form] = Form.useForm();
  const [logo, setLogo] = useState<File | null>(null);
  const [favicon, setFavicon] = useState<File | null>(null);
  const [error, setError] = useState<ControlError | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const v = await form.validateFields();
    const configuration = JSON.parse(JSON.stringify(base));
    configuration.version = 1;
    configuration.identity = { ...(base.identity ?? {}), name: v.name, short_name: v.short_name || v.name.slice(0, 24), description: v.description || "" };
    configuration.copy = {
      en: {
        ...en,
        welcome_title: v.welcome_title || null,
        welcome_body: v.welcome_body || null,
        signin_title: v.signin_title || null,
        signin_body: v.signin_body || null,
        footer: v.footer || null,
        suggestions: (v.suggestions || "").split("\n").map((s: string) => s.trim()).filter(Boolean),
      },
    };
    configuration.links = { ...(base.links ?? {}), help: v.help || null };
    configuration.announcement = v.announcement ? { id: `a-${Date.now()}`, text_by_locale: { en: v.announcement }, dismissible: true } : null;
    configuration.presentation = { ...(base.presentation ?? {}), response_watermark: v.watermark || null };
    configuration.assets = { ...(base.assets ?? {}) };
    const files: Record<string, string> = {};
    if (logo) {
      const path = `branding/${logo.name.replace(/[^A-Za-z0-9._-]/g, "_")}`;
      configuration.assets.logo_light = path;
      files[path] = await fileB64(logo);
    }
    if (favicon) {
      const path = `branding/${favicon.name.replace(/[^A-Za-z0-9._-]/g, "_")}`;
      configuration.assets.favicon = path;
      files[path] = await fileB64(favicon);
    }
    setBusy(true);
    setError(null);
    try {
      await control("/api/branding/drafts", {
        idempotencyKey: newKey(),
        body: { configuration, theme: (active as any)?.theme ?? null, files, keep_active_files: true },
      });
      onCreated();
    } catch (e) {
      setError(e as ControlError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open title="Edit brand (creates a draft)" onCancel={onClose} onOk={() => void submit()} okText="Create draft" confirmLoading={busy} width={680}>
      <Paragraph type="secondary">
        Nothing changes for teammates until a draft is reviewed and activated. Language: English.
      </Paragraph>
      {error && <Alert type="error" showIcon title="Not saved" description={error.message} style={{ marginBottom: 12 }} />}
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          name: base.identity?.name ?? "Hubzoid",
          short_name: base.identity?.short_name ?? "",
          description: base.identity?.description ?? "",
          welcome_title: en.welcome_title ?? "",
          welcome_body: en.welcome_body ?? "",
          signin_title: en.signin_title ?? "",
          signin_body: en.signin_body ?? "",
          footer: en.footer ?? "",
          suggestions: (en.suggestions ?? []).join("\n"),
          help: base.links?.help ?? "",
          announcement: base.announcement?.text_by_locale?.en ?? "",
          watermark: base.presentation?.response_watermark ?? "",
        }}
      >
        <Form.Item name="name" label="Name" rules={[{ required: true, max: 80 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="short_name" label="Short name" rules={[{ max: 24 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="description" label="Description" rules={[{ max: 240 }]}>
          <Input />
        </Form.Item>
        <Space wrap>
          <Upload beforeUpload={(f) => (setLogo(f), false)} maxCount={1} accept=".svg,.png,.webp,.jpg,.jpeg" onRemove={() => setLogo(null)}>
            <Button>Logo (SVG, PNG, WebP or JPEG)</Button>
          </Upload>
          <Upload beforeUpload={(f) => (setFavicon(f), false)} maxCount={1} accept=".svg,.png" onRemove={() => setFavicon(null)}>
            <Button>Favicon (SVG or PNG)</Button>
          </Upload>
        </Space>
        <Form.Item name="signin_title" label="Sign-in title" rules={[{ max: 160 }]} style={{ marginTop: 12 }}>
          <Input />
        </Form.Item>
        <Form.Item name="signin_body" label="Sign-in text" rules={[{ max: 2000 }]}>
          <Input.TextArea autoSize />
        </Form.Item>
        <Form.Item name="welcome_title" label="Welcome title" rules={[{ max: 160 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="welcome_body" label="Welcome text" rules={[{ max: 2000 }]}>
          <Input.TextArea autoSize />
        </Form.Item>
        <Form.Item name="suggestions" label="Workspace suggestions (one per line)">
          <Input.TextArea autoSize />
        </Form.Item>
        <Form.Item name="footer" label="Footer" rules={[{ max: 2000 }]}>
          <Input />
        </Form.Item>
        <Form.Item name="help" label="Help link (https:// or /path)">
          <Input />
        </Form.Item>
        <Form.Item name="announcement" label="Announcement (leave empty for none)" rules={[{ max: 2000 }]}>
          <Input.TextArea autoSize />
        </Form.Item>
        <Form.Item name="watermark" label="Text under each answer" rules={[{ max: 240 }]}>
          <Input />
        </Form.Item>
      </Form>
    </Modal>
  );
}
