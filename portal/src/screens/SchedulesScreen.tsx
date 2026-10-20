import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Grid,
  Input,
  List,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { Copy, MoreHorizontal, Pause, Play } from "lucide-react";
import {
  query,
  request,
  type Hub,
  type MyAgent,
  type MyData,
  type MySchedule,
  type PersonalSchedule,
  type Report,
  type ScheduleRun,
  type ScheduleEdit,
  type PersonalRun,
  type Workflow,
} from "../api";
import { inputField } from "../components/WorkflowInputs";
import { useVisibleRefresh } from "../hooks/useVisibleRefresh";
import { useData } from "../hooks/useData";
import { useHashQuery } from "../hooks/useRoute";
import { InfoHelp as Help, LoadState, PageHeader, RunStatusTag, When } from "../components/common";
import { formatDuration } from "../lib/format";
import { displayName } from "../app/lib/format";

const { Text, Title } = Typography;

/** A schedule row in either view: a person's own (with links and its agent's
 *  `api`), or any person's for an admin (with owner and cost, no report link). */
type Row = Omit<PersonalSchedule, "owner" | "cost_30d"> & {
  hub: string;
  owner?: string;
  cost_30d?: number;
  reports?: MySchedule["reports"];
};
type Action = "pause" | "resume" | "run_now" | "stop";
type Act = (s: Row, action: Action) => Promise<void>;

const STATE_TAG: Record<string, string> = { active: "green", testing: "blue", paused: "gold" };
const REPORT_STATE: Record<string, [string, string]> = {
  live: ["Live", "green"], failed: ["Last update failed", "gold"], stopped: ["No longer updating", "default"],
};
const live = (s: Row) => s.state === "active" || s.state === "testing";
const adminPaused = (s: Row) => s.state === "paused" && s.reason === "admin";
const at = (seconds: number | null | undefined) => (seconds ? new Date(seconds * 1000).toISOString() : null);

/**
 * Schedules & reports: one page for everyone. "Yours" is the signed-in
 * person's own schedules and report links on every agent; admins also get
 * "Everyone", one agent at a time. The server decides what each view returns.
 */
export function SchedulesScreen({ admin, hubs, embedded = false }: { admin: boolean; hubs?: Hub[]; embedded?: boolean }) {
  const [q, setQ] = useHashQuery();
  const view = q.view === "reports" ? "reports" : admin && q.view === "workflows" ? "workflows"
    : admin && (q.view === "everyone" || q.scope === "everyone") ? "everyone" : "mine";
  const tabs: [string, string][] = [["mine", admin ? "Yours" : "Schedules"],
    ...(admin ? [["everyone", "Everyone's"], ["workflows", "Agent workflows"]] as [string, string][] : []), ["reports", "Reports"]];
  return (
    <>
      <PageHeader
        title="Schedules"
        description={embedded ? "Work your agents run on a timetable, as the person who set it up. Make one in chat, for example “every Monday at 9”." : undefined}
      />
      <nav className="hz-agent-tabs" aria-label="Schedules">
        {tabs.map(([key, label]) => (
          <a key={key} href={`#/schedules${key === "mine" ? "" : `?view=${key}`}`} aria-current={view === key ? "page" : undefined}
            onClick={(e) => { e.preventDefault(); setQ({ view: key === "mine" ? undefined : key, scope: undefined }); }}>{label}</a>
        ))}
      </nav>
      {view === "everyone" ? <Everyone hubs={hubs ?? []} hub={q.hub} setHub={(hub) => setQ({ hub })} />
        : view === "workflows" ? <AgentWorkflows hubs={hubs ?? []} />
        : view === "reports" ? <MineReports /> : <Mine />}
    </>
  );
}

// ---- yours ---------------------------------------------------------------------

function Mine() {
  const data = useData<MyData>("/my");
  const [editing, setEditing] = useState<Row | null>(null);
  useVisibleRefresh(data.reload, data.refreshing || data.loading);
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const { agents, schedules } = data.data;
  const api = (hub: string) => agents.find((a) => a.key === hub)?.api;
  const name = (hub: string) => displayName(agents.find((a) => a.key === hub)?.name ?? hub);
  const act: Act = async (s, action) => {
    if (action === "stop" && !(await confirm(`Stop "${s.name}"?`, "It won't run again. Its live link keeps the last copy.", "Stop schedule"))) return;
    try {
      await request(`/my/schedules/${encodeURIComponent(s.id)}/${action}`, { hub: s.hub }, undefined, "POST", api(s.hub));
      message.success(DONE[action]);
      data.reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "That did not work.");
    }
  };
  return (
    <div className="panel">
      <SchedulesTable
        rows={schedules}
        mine
        edit={setEditing}
        agentName={agents.length > 1 ? name : undefined}
        act={act}
        runsPath={(s) => `/my/schedules/${encodeURIComponent(s.id)}/runs${query({ hub: s.hub })}`}
        empty="Nothing scheduled yet. In chat, ask an agent to repeat something, for example “send me the low stock list every Monday at 9”."
      />
      {editing && <ScheduleEditor key={editing.id} row={editing} api={api(editing.hub)}
        close={() => setEditing(null)} saved={() => { setEditing(null); data.reload(); }} />}
    </div>
  );
}

/** Reports: what can be run, your own copies, and your recent one-off requests. */
function MineReports() {
  const data = useData<MyData>("/my");
  useVisibleRefresh(data.reload, data.refreshing || data.loading);
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const { agents, reports } = data.data;
  const name = (hub: string) => displayName(agents.find((a) => a.key === hub)?.name ?? hub);
  const catalog = data.data.catalog ?? [];
  return (
    <div className="panel">
      <ReportCatalog catalog={catalog} agents={agents} />
      {reports.length > 0 && (
        <Section title="Your reports" help="Your own copies. Each link always shows the newest one.">
          <Table
            rowKey="id"
            size="middle"
            dataSource={reports}
            pagination={false}
            scroll={{ x: 640 }}
            rowClassName="clickable"
            onRow={(r) => ({ onClick: () => window.open(r.url, "_blank", "noopener") })}
            columns={[
              {
                title: "Report", key: "title",
                render: (_, r) => <><Text strong>{r.title} ↗</Text>
                  {agents.length > 1 && <div><Text type="secondary">{name(r.hub)}</Text></div>}</>,
              },
              {
                title: "Updated", key: "updated",
                render: (_, r) => <Space size={10} wrap>{r.updated ? <When value={at(r.updated)} /> : "—"}<Dot tone={REPORT_STATE[r.state][1]} label={REPORT_STATE[r.state][0]} /></Space>,
              },
              {
                title: <span>Company link <Help text="Share it or turn it off from the report page." /></span>,
                key: "company", align: "right",
                render: (_, r) => r.company_on ? <Dot tone="blue" label="On" /> : <Text type="secondary">Off</Text>,
              },
            ]}
          />
        </Section>
      )}
      <RecentRuns agents={agents} />
      {catalog.length === 0 && reports.length === 0 && (
        <Empty description="No reports yet. Agents that offer reports list them here, with the request to type in chat." />
      )}
    </div>
  );
}

/** Every agent's own scheduled workflows in one list. A row opens the
 *  workflow in its agent's Runs & schedules. */
function AgentWorkflows({ hubs }: { hubs: Hub[] }) {
  const data = useData<{ workflows: Workflow[] }>("/workflows", { keepStale: true });
  useVisibleRefresh(data.reload, data.refreshing || data.loading);
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const rows = data.data.workflows.filter((w) => w.schedule);
  const hubName = (key: string) => hubs.find((h) => h.key === key)?.name ?? key;
  const state = (w: Workflow) => w.state === "error" ? <Dot tone="red" label="Error" />
    : w.state === "stale" ? <Dot tone="red" label="Scheduler stopped" />
    : w.state === "paused" ? <Dot tone="gold" label="Paused" /> : <Dot tone="green" label="Active" />;
  return (
    <div className="panel">
      <Table<Workflow>
        rowKey={(w) => `${w.hub}:${w.name}`}
        size="middle"
        dataSource={rows}
        pagination={false}
        scroll={{ x: 760 }}
        rowClassName="clickable"
        onRow={(w) => ({ onClick: () => { location.hash = `/agents/${encodeURIComponent(w.hub)}/runs/${encodeURIComponent(w.name)}`; } })}
        locale={{ emptyText: <Empty description="No agent has a scheduled workflow. A workflow gets a schedule in its folder's workflow file." /> }}
        columns={[
          { title: "Workflow", key: "name", render: (_, w) => <><Text strong>{w.name}</Text><div style={{ marginTop: 2 }}>{state(w)}</div></> },
          { title: "Agent", key: "hub", render: (_, w) => hubName(w.hub) },
          { title: "When", key: "when", render: (_, w) => <><div>{w.schedule}</div><Text type="secondary">{w.timezone}</Text></> },
          { title: "Next run", key: "next", render: (_, w) => w.next_run ? <When value={w.next_run} /> : <Text type="secondary">—</Text> },
          { title: "Last started", key: "last", render: (_, w) => w.last_dispatch ? <When value={w.last_dispatch} /> : <Text type="secondary">Not yet</Text> },
        ]}
      />
    </div>
  );
}

/** Status as a dot and a word. */
function Dot({ tone, label }: { tone: string; label: string }) {
  const cls = tone === "green" ? "is-ok" : tone === "gold" ? "is-warn" : tone === "red" ? "is-bad" : tone === "blue" ? "is-info" : "is-muted";
  return <span className={`hz-status ${cls}`}><i aria-hidden />{label}</span>;
}

const DONE: Record<Action, string> = {
  pause: "Paused.", resume: "Resumed.", run_now: "Running now.", stop: "Stopped.",
};

function confirm(title: string, content: string, okText: string) {
  return new Promise<boolean>((resolve) => Modal.confirm({
    title, content, okText, okButtonProps: { danger: true },
    onOk: () => resolve(true), onCancel: () => resolve(false),
  }));
}

function Section({ title, help, extra, children }: { title: string; help: string; extra?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="section">
      <Space style={{ justifyContent: "space-between", width: "100%" }} wrap>
        <Title level={5} style={{ margin: 0 }}>{title} <Help text={help} /></Title>
        {extra}
      </Space>
      {children}
    </div>
  );
}

// ---- the shared table and its drawer ---------------------------------------------

function SchedulesTable({
  rows,
  mine,
  agentName,
  act,
  runsPath,
  empty,
  edit,
}: {
  rows: Row[];
  mine: boolean;
  agentName?: (hub: string) => string;
  act: Act;
  runsPath: (s: Row) => string;
  empty: string;
  edit?: (row: Row) => void;
}) {
  // By id, so the drawer shows the row as it is after each reload.
  const [openId, setOpenId] = useState<string | null>(null);
  const open = rows.find((r) => r.id === openId) ?? null;
  const screens = Grid.useBreakpoint();
  return (
    <>
      {!screens.md ? <List dataSource={rows} locale={{ emptyText: <Empty description={empty} /> }}
        renderItem={(s) => <List.Item><Space direction="vertical" style={{ width: "100%" }}>
          <Button type="link" style={{ padding: 0, whiteSpace: "normal", height: "auto" }} onClick={() => setOpenId(s.id)}>{s.name}</Button>
          <Dot tone={STATE_TAG[s.state] ?? "default"} label={s.state_label} />
          {s.owner && <Text className="identity">{s.owner}</Text>}
          {agentName && <Text type="secondary">{agentName(s.hub)}</Text>}
          {s.next_fire && <Text>Next: <When value={at(s.next_fire)} /></Text>}
          <Text>{s.when} ({s.timezone})</Text><Text>{s.last_label}</Text>
          {s.reports?.map((r) => <a key={r.url} href={r.url}>{r.title} ↗</a>)}
          <Actions s={s} mine={mine} act={act} edit={edit} />
        </Space></List.Item>} /> : <Table<Row>
        rowKey="id"
        size="middle"
        dataSource={rows}
        pagination={false}
        scroll={{ x: 900 }}
        rowClassName="clickable"
        onRow={(s) => ({ onClick: () => setOpenId(s.id) })}
        locale={{ emptyText: <Empty description={empty} /> }}
        columns={[
          ...(mine ? [] : [{ title: "Person", key: "owner", render: (_: unknown, s: Row) => <Text className="identity">{s.owner}</Text> }]),
          ...(agentName ? [{ title: "Agent", key: "agent", render: (_: unknown, s: Row) => agentName(s.hub) }] : []),
          {
            title: "Schedule", key: "name",
            render: (_, s) => <><Text strong>{s.name}</Text><div style={{ marginTop: 2 }}><Dot tone={STATE_TAG[s.state] ?? "default"} label={s.state_label} /></div></>,
          },
          {
            title: "Report", key: "report",
            render: (_, s) => <>
              {s.reports?.length
                ? s.reports.map((r) => <div key={r.url}><a href={r.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>{r.title} ↗</a></div>)
                : <div>{s.report}</div>}
              {s.inputs && <Text type="secondary">{s.inputs}</Text>}
            </>,
          },
          { title: "When", key: "when", render: (_, s) => <><div>{s.when}</div><Text type="secondary" style={{ whiteSpace: "nowrap" }}>{s.timezone}</Text></> },
          {
            title: "Next run", key: "next",
            render: (_, s) => s.next_fire ? <When value={at(s.next_fire)} /> : <Text type="secondary">—</Text>,
          },
          {
            title: "Last result", key: "last",
            render: (_, s) => s.last_status === "failed" ? <Dot tone="red" label={s.last_label} /> : <Text>{s.last_label}</Text>,
          },
          ...(mine ? [] : [{
            title: <span style={{ whiteSpace: "nowrap" }}>30-day cost</span>, key: "cost",
            render: (_: unknown, s: Row) => <Tooltip title="Model cost of this schedule's runs in the last 30 days."><Text>${(s.cost_30d ?? 0).toFixed(2)}</Text></Tooltip>,
          }]),
          { title: "", key: "acts", align: "right", render: (_, s) => <Actions s={s} mine={mine} act={act} edit={edit} /> },
        ]}
      />}
      <ScheduleDrawer edit={edit} row={open} close={() => setOpenId(null)} runsPath={runsPath} mine={mine} act={act} />
    </>
  );
}

/** Pause or Resume, and a menu with Run now (own schedules) and Stop. An
 *  admin's pause is theirs to lift; the owner sees it but can't. */
function Actions({ s, mine, act, edit }: { s: Row; mine: boolean; act: Act; edit?: (row: Row) => void }) {
  const canResume = s.state === "paused" && (mine ? !adminPaused(s) : adminPaused(s));
  const more = [
    ...(mine && !adminPaused(s) ? [{ key: "run_now", label: "Run now" }] : []),
    { key: "stop", label: "Stop", danger: true },
  ];
  return (
    <Space size={6} onClick={(e) => e.stopPropagation()}>
      {edit && <Button size="small" onClick={() => edit(s)}>Edit</Button>}
      {live(s) && <Button size="small" icon={<Pause size={14} />} onClick={() => act(s, "pause")}>Pause</Button>}
      {s.state === "paused" && (
        <Tooltip title={canResume ? undefined : mine ? "Only an admin can resume it." : "Only its owner can resume it."}>
          <Button size="small" icon={<Play size={14} />} disabled={!canResume} onClick={() => act(s, "resume")}>Resume</Button>
        </Tooltip>
      )}
      <Dropdown trigger={["click"]} menu={{ items: more, onClick: ({ key }) => act(s, key as Action) }}>
        <Button size="small" aria-label={`More for ${s.name}`} icon={<MoreHorizontal size={14} />} />
      </Dropdown>
    </Space>
  );
}

function ScheduleDrawer({
  row,
  close,
  runsPath,
  mine,
  act,
  edit,
}: {
  edit?: (row: Row) => void;
  row: Row | null;
  close: () => void;
  runsPath: (s: Row) => string;
  mine: boolean;
  act: Act;
}) {
  const runs = useData<{ runs: ScheduleRun[] }>(row ? runsPath(row) : null);
  useVisibleRefresh(runs.reload, runs.refreshing || runs.loading, !!row);
  return (
    <Drawer open={!!row} onClose={close} size={460} title={row?.name} destroyOnHidden
      extra={row && <Actions s={row} mine={mine} edit={edit} act={async (s, a) => { await act(s, a); if (a === "stop") close(); }} />}>
      {row && (
        <>
          <Descriptions column={1} size="small" items={[
            ...(row.owner ? [{ key: "owner", label: "Person", children: row.owner }] : []),
            { key: "report", label: "Report", children: row.reports?.length
              ? row.reports.map((r) => <div key={r.url}><a href={r.url} target="_blank" rel="noreferrer">{r.title} ↗</a></div>)
              : row.report },
            ...(row.inputs ? [{ key: "inputs", label: "Inputs", children: row.inputs }] : []),
            { key: "when", label: "When", children: `${row.when} (${row.timezone})` },
            { key: "state", label: "State", children: <Dot tone={STATE_TAG[row.state] ?? "default"} label={row.state_label} /> },
            { key: "next", label: "Next run", children: row.next_fire ? <When value={at(row.next_fire)} /> : "—" },
          ]} />
          {row.recovery && <Alert type="warning" showIcon message={row.recovery} />}
          {row.last_run && <div><Text type="secondary">Run reference: </Text><Text copyable style={{ overflowWrap: "anywhere" }}>{row.last_run}</Text></div>}
          <Title level={5} style={{ marginTop: 20 }}>Latest runs</Title>
          {runs.data ? (
            <List
              size="small"
              dataSource={runs.data.runs}
              locale={{ emptyText: "No runs yet." }}
              renderItem={(r) => (
                <List.Item>
                  <Space wrap>
                    <RunStatusTag status={r.status} />
                    {r.id && <Text copyable>{r.id}</Text>}
                    {r.started ? <When value={new Date(r.started).toISOString()} /> : "—"}
                    {r.duration_ms != null && <Text type="secondary">{formatDuration(r.duration_ms)}</Text>}
                  </Space>
                </List.Item>
              )}
            />
          ) : (
            <LoadState error={runs.error} retry={runs.reload} />
          )}
        </>
      )}
    </Drawer>
  );
}

// ---- everyone (admins) ---------------------------------------------------------

function Everyone({ hubs, hub, setHub }: { hubs: Hub[]; hub?: string; setHub: (hub: string) => void }) {
  const selected = hubs.find((h) => h.key === hub) ?? hubs[0];
  if (!selected) return <Alert type="info" showIcon message="You don't manage any agents." />;
  return (
    <div className="panel">
      <Select
        style={{ width: 280 }}
        value={selected.key}
        onChange={setHub}
        aria-label="Agent"
        options={hubs.map((h) => ({ value: h.key, label: h.name }))}
      />
      <PeopleSchedules key={`s-${selected.key}`} hub={selected} />
      <LiveReports key={`r-${selected.key}`} hub={selected} />
    </div>
  );
}

function PeopleSchedules({ hub }: { hub: Hub }) {
  const data = useData<{ schedules: PersonalSchedule[] }>("/schedules" + query({ hub: hub.key }));
  useVisibleRefresh(data.reload, data.refreshing || data.loading);
  const act: Act = async (s, action) => {
    if (action === "stop" && !(await confirm(`Stop ${s.owner}'s schedule "${s.name}"?`,
      "They'll see that an admin stopped it. Its live link keeps the last copy.", "Stop schedule"))) return;
    try {
      await request(`/schedules/${encodeURIComponent(s.id)}/${action}`, { hub: hub.key });
      message.success(DONE[action]);
      data.reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "That did not work.");
    }
  };
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const rows = data.data.schedules.map((s) => ({ ...s, hub: hub.key }));
  const active = rows.filter(live).length;
  return (
    <Section title="People's schedules"
      help="Saved from chat. Each runs as the person who saved it. Admins can pause and stop them, and resume only what they paused."
      extra={rows.length > 0 && <Text type="secondary">{active} of {rows.length} active</Text>}>
      <SchedulesTable
        rows={rows}
        mine={false}
        act={act}
        runsPath={(s) => `/schedules/${encodeURIComponent(s.id)}/runs${query({ hub: hub.key })}`}
        empty="No one has scheduled this agent's reports yet. People allowed to schedule do it from chat."
      />
    </Section>
  );
}

function LiveReports({ hub }: { hub: Hub }) {
  const data = useData<{ reports: Report[]; company: { on: boolean; reason: string; domains: string } }>(
    "/reports" + query({ hub: hub.key }));
  const link = async (r: Report, action: "copy" | "replace" | "off") => {
    if (action !== "copy" && !(await confirm(
      action === "replace" ? "Replace this link?" : "Turn off this link?",
      action === "replace"
        ? "The current link stops working now. Share the new one with your team."
        : `People at ${data.data?.company.domains ?? "the company domain"} can no longer open it. Anyone who saved a copy keeps it.`,
      action === "replace" ? "Replace link" : "Turn off link"))) return;
    try {
      const out = await request<{ url?: string }>(`/reports/${encodeURIComponent(r.id)}/company`, { hub: hub.key, action });
      if (out.url) {
        try { await navigator.clipboard.writeText(out.url); message.success("Link copied."); }
        catch { Modal.info({ title: "Company link", content: <Text copyable code>{out.url}</Text> }); }
      } else {
        message.success("Link turned off.");
      }
      data.reload();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "That did not work.");
    }
  };
  if (!data.data) return data.error ? <LoadState error={data.error} retry={data.reload} /> : null;
  const { reports, company } = data.data;
  if (!reports.length) return null;
  return (
    <Section title="Live reports"
      help="Each report's live link, which always shows the newest copy. A company link lets people at your company domain open it without using the agent. Personal reports stay with their owner; admins can only turn their link off."
      extra={company.on ? <Tag color="blue">Company links on for {company.domains}</Tag> : <Tag>Company links off</Tag>}>
      {!company.on && <Alert className="notice" type="info" showIcon message={company.reason} />}
      <Table<Report>
        rowKey="id"
        size="middle"
        dataSource={reports}
        pagination={false}
        scroll={{ x: 900 }}
        columns={[
          { title: "Report", key: "title", render: (_, r) => <><div>{r.title}</div><Text type="secondary" className="identity">{r.key}</Text></> },
          { title: "Kind", key: "scope", render: (_, r) => r.scope === "team" ? "Team" : <><div>Personal</div><Text type="secondary">{r.owner}</Text></> },
          {
            title: "Updated", key: "updated",
            render: (_, r) => <>{r.updated ? <When value={at(r.updated)} /> : "—"}<div><Tag color={REPORT_STATE[r.state][1]}>{REPORT_STATE[r.state][0]}</Tag></div></>,
          },
          {
            title: "Company link", key: "company", align: "right",
            render: (_, r) => {
              const more = [
                ...(r.scope === "team" ? [{ key: "replace", label: "Replace link" }] : []),
                { key: "off", label: "Turn off link", danger: true },
              ];
              if (!r.company_on) {
                // Only for a report whose workflow shares it, after someone turned its link off.
                return r.scope === "team" && company.on && r.audience === "domain"
                  ? <Button size="small" onClick={() => link(r, "replace")}>Make a link</Button>
                  : <Text type="secondary">Off</Text>;
              }
              return (
                <Space size={6}>
                  {r.scope === "team" ? <Button size="small" icon={<Copy size={14} />} onClick={() => link(r, "copy")}>Copy</Button> : <Tag>On</Tag>}
                  <Dropdown trigger={["click"]} menu={{ items: more, onClick: ({ key }) => link(r, key as "replace" | "off") }}>
                    <Button size="small" aria-label="More" icon={<MoreHorizontal size={14} />} />
                  </Dropdown>
                </Space>
              );
            },
          },
        ]}
      />
    </Section>
  );
}

function ReportCatalog({ catalog, agents }: { catalog: NonNullable<MyData["catalog"]>; agents: MyAgent[] }) {
  if (!catalog.length) return null;
  return <Section title="Reports you can run" help="Reports this agent offers with your current access. Open chat and use the suggested request.">
    <List dataSource={catalog} renderItem={(r) => <List.Item>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Text strong>{r.title}</Text><Text type="secondary">{displayName(agents.find((a) => a.key === r.hub)?.name ?? r.hub)} · {r.schedulable ? "Run once or schedule" : "Run once"}</Text>
        <Text copyable={{ text: `Run ${r.title} for me` }}>Ask in chat: “Run {r.title} for me”</Text>
        {r.schema && Object.keys(r.schema.properties).length > 0 && <Text type="secondary">The agent asks for: {Object.entries(r.schema.properties).map(([k, p]) => (p as { title?: string }).title || k.replace(/_/g, " ")).join(", ")}</Text>}
        <Button href={agents.find((a) => a.key === r.hub)?.chat ?? "/"}>Open chat ↗</Button>
      </Space>
    </List.Item>} />
  </Section>;
}

function RecentRuns({ agents }: { agents: MyAgent[] }) {
  const [selected, setSelected] = useState<string>();
  const usable = agents.filter((a) => a.chat);
  const agent = usable.find((a) => a.key === selected) ?? usable[0];
  const data = useData<{ runs: PersonalRun[] }>(agent ? `/my/runs${query({ hub: agent.key })}` : null);
  useVisibleRefresh(data.reload, data.refreshing || data.loading, !!agent);
  if (!agent) return null;
  return <Section title="Recent requests" help="Your recent one-off reports and answers on one agent, including work that continued after chat stopped waiting. Pick the agent to see its requests.">
    {usable.length > 1 && <Select aria-label="Recent requests agent" value={agent.key} onChange={setSelected}
      options={usable.map((a) => ({ value: a.key, label: displayName(a.name) }))} style={{ minWidth: 220 }} />}
    {data.data ? <List dataSource={data.data.runs} locale={{ emptyText: usable.length > 1
        ? `No recent requests on ${displayName(agent.name)}. Pick another agent above, or ask in chat to run a report.`
        : "No recent requests yet. Ask in chat to run a report." }}
      renderItem={(r) => <List.Item><Space direction="vertical" style={{ width: "100%" }}>
        <Space wrap><Text strong>{r.report}</Text><RunStatusTag status={r.status} /></Space>
        {r.links.map((link) => <a key={link.url} href={link.url}>{link.title} ↗</a>)}
        {r.answer && <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }} ellipsis={{ rows: 3, expandable: true }}>{r.answer}</Typography.Paragraph>}
        {r.recovery && <Text type="warning">{r.recovery}</Text>}
        <Text type="secondary" copyable style={{ overflowWrap: "anywhere" }}>{r.id}</Text>
      </Space></List.Item>} /> : <LoadState error={data.error} retry={data.reload} />}
  </Section>;
}

function ScheduleEditor({ row, api, close, saved }: { row: Row; api?: string; close: () => void; saved: () => void }) {
  const data = useData<ScheduleEdit>(`/my/schedules/${encodeURIComponent(row.id)}${query({ hub: row.hub })}`);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const save = async () => {
    let values;
    try { values = await form.validateFields(); } catch { return; }
    setSaving(true);
    try {
      await request(`/my/schedules/${encodeURIComponent(row.id)}`, { ...values, hub: row.hub,
        inputs: data.data?.schema ? values.inputs ?? {} : data.data?.inputs ?? {}, revision: data.data?.revision }, undefined, "PUT", api);
      message.success("Schedule updated. Paused schedules stay paused."); saved();
    } catch (error) { message.error(error instanceof Error ? error.message : "Could not save."); }
    finally { setSaving(false); }
  };
  return <Modal open title={`Edit ${row.name}`} onCancel={close} onOk={save} okText="Save changes"
    confirmLoading={saving} okButtonProps={{ disabled: !data.data }} destroyOnHidden>
    {data.data ? <Form form={form} layout="vertical" initialValues={data.data}>
      <Form.Item name="cadence" label="When" rules={[{ required: true }]} extra="For example: every monday at 09:00, daily at 18:00, every 2 hours"><Input /></Form.Item>
      <Form.Item name="timezone" label="Time zone" rules={[{ required: true }]} extra="For example: Asia/Kolkata"><Input /></Form.Item>
      {data.data.schema ? Object.entries(data.data.schema.properties).map(([name, prop]) => inputField(name, prop, data.data!.schema!.required?.includes(name) ?? false, "inputs", data.data!.schema!.$defs))
        : <Alert type="info" message="Input fields are unavailable until the agent starts. Existing inputs will be kept." />}
      <Text type="secondary">Changes apply to future runs. A run already in progress keeps its original inputs.</Text>
    </Form> : <LoadState error={data.error} retry={data.reload} />}
  </Modal>;
}
