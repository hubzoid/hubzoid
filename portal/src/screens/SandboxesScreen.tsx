import { useCallback, useState } from "react";
import { InfoHelp, PAGED } from "../components/common";
import { useUnderTabs } from "../lib/underTabs";
import {
  Alert, App, Button, Card, Descriptions, Drawer, Empty, Input, Modal, Segmented, Select, Space, Spin, Table, Tag,
  Typography,
} from "antd";
import { ApiError, request, query } from "../api";
import { useData } from "../hooks/useData";
import { useVisibleRefresh } from "../hooks/useVisibleRefresh";

const { Text, Title, Paragraph } = Typography;

type Sandbox = {
  id: string; owner_kind: string; owner_id: string; item_id: string | null; profile_id: string;
  profile_digest: string; provider_binding: string; image: string | null; state: string; end_reason: string | null;
  created: number; ready_at: number | null; last_activity: number | null; stopped_at: number | null;
  ended_at: number | null; lifetime_deadline: number; idle_deadline: number | null; revision: number;
  network: { setup?: { mode: string; hosts?: string[] }; agent?: { mode: string }; effective?: string; verified_at?: number };
  resources?: { cpus: number; memory_mib: number; pids: number };
  setup: { exit_code: number; timed_out: boolean } | null;
  checkouts: { path: string; commit: string; source: string }[];
  exports: { file: string; commits: number; sha256: string }[];
  error: string | null;
};
type Listing = {
  sandboxes: Sandbox[]; enabled: boolean; providers: Record<string, { kind: string; runtime: string }>;
  limits: { max_active_per_hub: number }; last_reap: { at: number; orphans: number } | null;
};
type Check = { id: string; status: "pass" | "warn" | "fail"; summary: string; recovery_action: string | null };
type Doctor = { valid: boolean; checks: Check[] };
type Reap = { dry_run: boolean; stop: { id: string }[]; release: { id: string; reason: string }[]; orphans: { id: string; role: string }[] };
type HubOption = { key: string; name: string };

const STATE_COLORS: Record<string, string> = {
  allocating: "processing", setting_up: "processing", ready: "green", stopped: "default",
  deleted: "default", lost: "red", start_failed: "red", setup_failed: "red",
};
const STATE_LABELS: Record<string, string> = {
  allocating: "Preparing", setting_up: "Installing dependencies", ready: "Running", stopped: "Paused",
  deleted: "Removed", lost: "Lost", start_failed: "Failed to start", setup_failed: "Setup failed",
};
const FILTERS = [
  { label: "All", value: "" }, { label: "Running", value: "ready" }, { label: "Paused", value: "stopped" },
  { label: "Lost", value: "lost" }, { label: "Removed", value: "deleted" },
];
const when = (t: number | null | undefined) => (t ? new Date(t * 1000).toLocaleString() : "");
const errorMessage = (e: unknown) => {
  if (e instanceof ApiError) {
    const detail = e.data?.detail as { message?: string } | string | undefined;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : String(e);
};

/** Sandboxes of a hub: health, the workspaces agents are using, and cleanup.
 *  Shows metadata only, never a sandbox's files or command output. */
export function SandboxesScreen({ hubs }: { hubs: HubOption[] }) {
  const underTabs = useUnderTabs();
  const { message } = App.useApp();
  const [hub, setHub] = useState<string | undefined>(hubs[0]?.key);
  const [state, setState] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [doctor, setDoctor] = useState<Doctor | null>(null);
  const [checking, setChecking] = useState(false);
  const [reap, setReap] = useState<Reap | null>(null);
  const [busy, setBusy] = useState(false);
  const base = hub ? `/hubs/${encodeURIComponent(hub)}/sandboxes` : null;
  const list = useData<Listing>(base ? `${base}${query({ state })}` : null, { keepStale: true });
  useVisibleRefresh(list.reload, list.refreshing, !open);

  const runDoctor = useCallback(async () => {
    if (!base) return;
    setChecking(true);
    try {
      setDoctor(await request<Doctor>(`${base}/doctor`, {}, undefined, "POST"));
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setChecking(false);
    }
  }, [base, message]);

  const runReaper = useCallback(async (dryRun: boolean) => {
    if (!base) return;
    setBusy(true);
    try {
      const out = await request<Reap>(`${base}/reaper`, { dry_run: dryRun }, undefined, "POST");
      setReap(out);
      if (!dryRun) message.success("Cleanup done.");
      list.reload();
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [base, list, message]);

  if (!hubs.length) return <Empty description="You have no agents yet." />;
  return (
    <div>
      {!underTabs && <Title level={2} style={{ marginTop: 0 }}>Sandboxes</Title>}
      <Paragraph type="secondary">
        Isolated workspaces where the agent runs commands, removed when the work is done.
        <InfoHelp text="Each belongs to one agent call and runs offline after its reviewed setup. Your hub's files and outputs are never touched by cleanup. The same controls are in hubzoid sandbox." />
      </Paragraph>
      <Space wrap style={{ marginBottom: 16 }}>
        {hubs.length > 1 && (
          <Select aria-label="Agent" value={hub} onChange={setHub} style={{ minWidth: 200 }}
            options={hubs.map((h) => ({ value: h.key, label: h.name }))} />
        )}
        <Segmented aria-label="Filter by state" value={state} onChange={(v) => setState(String(v))} options={FILTERS} />
        <Button loading={checking} onClick={runDoctor}>Check health</Button>
        <Button loading={busy} onClick={() => runReaper(true)}>Preview cleanup</Button>
      </Space>

      {doctor && (
        <Card size="small" title={doctor.valid ? "Healthy" : "Needs attention"} style={{ marginBottom: 16 }}
          extra={<Button size="small" onClick={() => setDoctor(null)}>Close</Button>}>
          <Space direction="vertical" style={{ width: "100%" }}>
            {doctor.checks.map((c) => (
              <Alert key={c.id} type={c.status === "pass" ? "success" : c.status === "warn" ? "warning" : "error"}
                showIcon title={c.summary} description={c.recovery_action ?? undefined} />
            ))}
          </Space>
        </Card>
      )}

      {reap && (
        <Card size="small" style={{ marginBottom: 16 }}
          title={reap.dry_run ? "Cleanup preview" : "Cleanup done"}
          extra={<Space>
            {reap.dry_run && (reap.stop.length + reap.release.length + reap.orphans.length > 0) && (
              <Button danger loading={busy} onClick={() => runReaper(false)}>Run cleanup</Button>
            )}
            <Button size="small" onClick={() => setReap(null)}>Close</Button>
          </Space>}>
          <Text>
            {reap.dry_run ? "Would pause" : "Paused"} {reap.stop.length} idle, {reap.dry_run ? "remove" : "removed"}{" "}
            {reap.release.length} finished or expired, and {reap.dry_run ? "delete" : "deleted"} {reap.orphans.length}{" "}
            leftover containers or networks.
          </Text>
        </Card>
      )}

      {list.error && !list.data ? (
        <Alert type="error" showIcon title="Couldn’t load sandboxes" description={list.error}
          action={<Button onClick={list.reload}>Try again</Button>} />
      ) : list.loading ? (
        <Spin />
      ) : !list.data?.enabled && (list.data?.sandboxes.length ?? 0) === 0 ? (
        <Empty description="Sandboxes are off for this agent.">
          <Text type="secondary">Add <Text code>sandbox.yaml</Text> with <Text code>enabled: true</Text> and a Docker
            provider, then run <Text code>hubzoid sandbox doctor</Text>.</Text>
        </Empty>
      ) : (list.data?.sandboxes.length ?? 0) === 0 ? (
        <Empty description={state ? "No sandboxes in this state." : "No sandboxes yet. One starts when an agent with a sandbox runs."} />
      ) : (
        <Table<Sandbox>
          rowKey="id" size="middle" pagination={PAGED} dataSource={list.data?.sandboxes}
          rowClassName="clickable"
          onRow={(row) => ({ onClick: () => setOpen(row.id) })}
          columns={[
            { title: "Owner", render: (_, r) => (<>
              <Text strong className="hz-row-name">{r.owner_kind === "station_call" ? "Agent call" : r.owner_kind}</Text><br />
              <Text type="secondary" className="mono">{r.owner_id.slice(0, 13)}</Text>
            </>) },
            { title: "Profile", dataIndex: "profile_id" },
            { title: "State", render: (_, r) => (
              <Tag color={STATE_COLORS[r.state] ?? "default"} title={r.end_reason ?? undefined}>
                {STATE_LABELS[r.state] ?? r.state}{r.end_reason && r.state === "deleted" ? ` · ${r.end_reason}` : ""}
              </Tag>) },
            { title: "Network", render: (_, r) => <Text type="secondary">{r.network?.effective === "deny_all" ? "Offline" : "—"}</Text> },
            { title: "Started", dataIndex: "created", render: (v) => when(v) },
            { title: "", render: (_, r) => <Button type="link" onClick={() => setOpen(r.id)}>Open</Button> },
          ]}
        />
      )}
      {base && open && (
        <SandboxDrawer base={base} id={open} onClose={() => { setOpen(null); list.reload(); }} />
      )}
    </div>
  );
}

function SandboxDrawer({ base, id, onClose }: { base: string; id: string; onClose: () => void }) {
  const { message } = App.useApp();
  const box = useData<Sandbox>(`${base}/${encodeURIComponent(id)}`);
  const [action, setAction] = useState<"stop" | "delete" | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const s = box.data;
  const live = s && ["ready", "stopped", "allocating", "setting_up"].includes(s.state);

  const act = async () => {
    if (!s || !action) return;
    setBusy(true);
    try {
      await request(action === "stop" ? `${base}/${encodeURIComponent(id)}/stop` : `${base}/${encodeURIComponent(id)}`,
        { reason, expected_revision: s.revision }, undefined, action === "stop" ? "POST" : "DELETE");
      message.success(action === "stop" ? "Paused. Its files are kept." : "Removed. Its owner will see that the sandbox was lost.");
      setAction(null);
      setReason("");
      box.reload();
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer open width={640} onClose={onClose} title="Sandbox">
      {box.error ? <Alert type="error" showIcon title={box.error} /> : !s ? <Spin /> : (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Descriptions size="small" column={1} bordered items={[
            { key: "id", label: "Sandbox", children: <Text className="mono">{s.id}</Text> },
            { key: "owner", label: "Owner", children: <Text className="mono">{s.owner_kind}:{s.owner_id}</Text> },
            { key: "state", label: "State", children: <Tag color={STATE_COLORS[s.state] ?? "default"}>{STATE_LABELS[s.state] ?? s.state}</Tag> },
            { key: "profile", label: "Profile", children: <>{s.profile_id} <Text type="secondary" className="mono">{s.profile_digest.slice(0, 19)}</Text></> },
            { key: "res", label: "Limits", children: s.resources ? `${s.resources.cpus} CPU · ${s.resources.memory_mib} MiB · ${s.resources.pids} processes` : "" },
            { key: "setup", label: "Setup network", children: s.network?.setup?.mode === "allowlist"
              ? `${s.network.setup.hosts?.length ?? 0} package registries, during setup only` : "None" },
            { key: "agent", label: "Agent commands", children: s.network?.effective === "deny_all"
              ? <>Offline <Text type="secondary">(checked {when(s.network.verified_at)})</Text></> : "Not ready" },
            { key: "setupr", label: "Setup result", children: s.setup ? `exit ${s.setup.exit_code}${s.setup.timed_out ? " (timed out)" : ""}` : "No setup command" },
            { key: "created", label: "Started", children: when(s.created) },
            s.ended_at ? { key: "life", label: "Ended", children: when(s.ended_at) }
              : { key: "life", label: "Expires", children: when(s.lifetime_deadline) },
          ]} />
          {s.checkouts.length > 0 && (
            <Card size="small" title="Checkouts">
              {s.checkouts.map((c) => <div key={c.path}><Text code>{c.path}</Text> from <Text className="mono">{c.source}</Text> at <Text className="mono">{c.commit.slice(0, 12)}</Text></div>)}
            </Card>
          )}
          {s.exports.length > 0 && (
            <Card size="small" title="Exported patches">
              {s.exports.map((e) => <div key={e.file}><Text code>{e.file}</Text> · {e.commits} commit{e.commits === 1 ? "" : "s"} · <Text className="mono">{e.sha256.slice(0, 19)}</Text></div>)}
            </Card>
          )}
          {s.error && <Alert type="error" showIcon title="What went wrong" description={s.error} />}
          {live && (
            <Card size="small" title="Controls">
              <Space>
                {s.state === "ready" && <Button onClick={() => setAction("stop")}>Pause</Button>}
                <Button danger onClick={() => setAction("delete")}>Remove</Button>
              </Space>
            </Card>
          )}
          <Modal open={action !== null} title={action === "stop" ? "Pause this sandbox?" : "Remove this sandbox?"}
            okText={action === "stop" ? "Pause" : "Remove"} okButtonProps={{ danger: action === "delete", disabled: !reason.trim(), loading: busy }}
            onOk={act} onCancel={() => setAction(null)}>
            <Paragraph>
              {action === "stop"
                ? "Its processes stop and its files are kept. Its agent starts it again when it next needs it."
                : "It is deleted now. The agent call using it sees that its sandbox was lost and stops; it is never recreated empty."}
            </Paragraph>
            <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
              placeholder="Why? This is recorded." aria-label="Reason" />
          </Modal>
        </Space>
      )}
    </Drawer>
  );
}
