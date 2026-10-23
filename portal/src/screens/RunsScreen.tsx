import { inputField } from "../components/WorkflowInputs";
import { useState } from "react";
import {
  Alert,
  Breadcrumb,
  Button,
  Collapse,
  Descriptions,
  Dropdown,
  Empty,
  Form,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  Tooltip,
  message,
} from "antd";
import { RefreshCw, Copy, Play, MoreHorizontal } from "lucide-react";
import { useUnderTabs } from "../lib/underTabs";
import {
  query,
  request,
  type Hub,
  type Run,
  type Webhook,
  type Workflow,
} from "../api";
import { useData } from "../hooks/useData";
import { agentHref, href, hrefWith } from "../hooks/useRoute";
import {
  LoadState,
  RecoveryScreen,
  RunStatusTag,
  When,
  WorkflowStateTag,
  InfoHelp as Help,
} from "../components/common";
import { describeCron, prettyOutput, formatDuration, formatTime, workflowState } from "../lib/format";
import { WorkflowResult } from "../components/WorkflowResult";
import { resultSummary, workflowCommand, createWorkflowCommand, shortRunId } from "../lib/results";

const { Text, Title, Paragraph } = Typography;
const TRIGGERS: Record<string, string> = {
  schedule: "Team schedule", personal_schedule: "Personal schedule", tool: "Chat",
  operator: "Run now", cli: "CLI", webhook: "Webhook", update: "Update now",
};
const PAGE = 50;
function CopyCommand({ command, label }: { command: string; label: string }) {
  return <Tooltip title="Run on the Hubzoid server." trigger={["hover", "focus"]}>
    <Button size="small" icon={<Copy size={14} />} onClick={async () => {
      try { await navigator.clipboard.writeText(command); message.success("Command copied"); }
      catch { message.error("Could not copy. Check clipboard permission."); }
    }}>{label}</Button>
  </Tooltip>;
}

const runsHref = (hub: Hub, workflow?: string, run?: string) =>
  href(
    `/agents/${encodeURIComponent(hub.key)}/runs` +
      (workflow ? `/${encodeURIComponent(workflow)}` : "") +
      (run ? `/${encodeURIComponent(run)}` : ""),
  );

export function RunsScreen({
  hub,
  workflow,
  run,
  embedded = false,
}: {
  hub: Hub;
  workflow?: string;
  run?: string;
  embedded?: boolean;
}) {
  if (workflow && run) return <RunDetail hub={hub} workflow={workflow} run={run} />;
  if (workflow) return <RunList key={`${hub.key}:${workflow}`} hub={hub} workflow={workflow} />;
  return <WorkflowList hub={hub} embedded={embedded} />;
}

function HealthAlerts({ workflows }: { workflows: Workflow[] }) {
  const stale = workflows.some((w) => w.state === "stale");
  const downtime = workflows.find((w) => w.downtime && w.downtime.missed)?.downtime;
  const health = workflows.find((w) => w.error && w.state === "error");
  return (
    <>
      {stale && (
        <Alert
          className="notice"
          type="error"
          showIcon
          banner
          message="The scheduler stopped. Scheduled workflows won't run until the agent restarts."
        />
      )}
      {downtime && (
        <Alert
          className="notice"
          type="warning"
          showIcon
          banner
          message={`${downtime.missed} scheduled ${downtime.missed === 1 ? "run was" : "runs were"} missed while the agent was stopped.`}
        />
      )}
      {health && (
        <Alert
          className="notice"
          type="error"
          showIcon
          banner
          message={`A workflow couldn’t be loaded: ${health.error}`}
        />
      )}
    </>
  );
}

function WorkflowList({ hub, embedded }: { hub: Hub; embedded: boolean }) {
  const data = useData<{ workflows: Workflow[] }>("/workflows" + query({ hub: hub.key }));
  const underTabs = useUnderTabs();
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const workflows = data.data.workflows;
  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          {!underTabs && <Title level={2}>Workflows</Title>}
          <Text type="secondary">Work this agent does on a timetable, on a webhook or when started.</Text>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
          Refresh
        </Button>
      </div>
      <HealthAlerts workflows={workflows} />
      {embedded && workflows.length > 0 ? <WorkflowRows hub={hub} workflows={workflows} /> : <Table<Workflow>
        rowKey={(w) => `${w.hub}:${w.name}`}
        size="middle"
        dataSource={workflows}
        pagination={false}
        scroll={{ x: 720 }}
        locale={{
          emptyText: (
            <Empty
              description={
                <>
                  <div>No workflows yet <Help text="Use a Markdown task for a plain-language brief, or create a Python workflow with the server command." /></div>
                  <Space><Button href="https://hubzoid.com/docs/guides/markdown-tasks" target="_blank" rel="noopener noreferrer">Guide</Button>
                  <CopyCommand label="Copy create command" command={createWorkflowCommand(hub.path || hub.key)} /></Space>
                </>
              }
            />
          ),
        }}
        columns={[
          {
            title: "Workflow",
            key: "name",
            render: (_, w) => (
              <>
                <a href={runsHref(hub, w.name)}>{w.name}</a>
                {w.error && (
                  <div>
                    <Text type="danger">{w.error}</Text>
                  </div>
                )}
              </>
            ),
          },
          {
            title: "Schedule",
            key: "schedule",
            render: (_, w) =>
              w.webhook ? (
                <>
                  <div>On webhook</div>
                  <Text code>{w.webhook}</Text>
                </>
              ) : w.schedule ? (
                <>
                  <div>{w.schedule_text ?? describeCron(w.schedule)}</div>
                  <Space size={6}><Text type="secondary" style={{ whiteSpace: "nowrap" }}>{w.timezone}</Text><Help text={`Cron: ${w.schedule}`} /></Space>
                </>
              ) : (
                <Text type="secondary">On demand</Text>
              ),
          },
          {
            title: "State",
            key: "state",
            render: (_, w) => (
              <>
                <WorkflowStateTag state={w.state} />
                <Help text={workflowState(w.state).hint} />
              </>
            ),
          },
          {
            title: "Runs as",
            key: "runs_as",
            render: (_, w) =>
              !w.runs_as ? (
                <Text type="secondary">—</Text>
              ) : w.runs_as.error ? (
                <Text type="danger" className="hint">
                  Cannot run: {w.runs_as.error}
                </Text>
              ) : (
                <>
                  <Text className="identity">{w.runs_as.account}</Text>
                  {w.runs_as.via && <Help text={w.runs_as.via} />}
                </>
              ),
          },
          {
            title: <span style={{ whiteSpace: "nowrap" }}>In chat <Help text="People given this workflow in Access can run it in chat for themselves. Schedule: they can also save a personal schedule, if allowed to schedule reports." /></span>,
            key: "chat",
            render: (_, w) =>
              w.as_tool ? (
                <Space size={4} wrap>
                  <Tag>Run</Tag>
                  {w.schedulable && <Tag>Schedule</Tag>}
                </Space>
              ) : (
                <Text type="secondary">—</Text>
              ),
          },
          {
            title: "Next run",
            key: "next",
            render: (_, w) =>
              w.next_run ? (
                <>
                  <When value={w.next_run} />
                  <div>
                    <Text type="secondary">{formatTime(w.next_run)}</Text>
                  </div>
                </>
              ) : (
                <Text type="secondary">—</Text>
              ),
          },
          {
            title: "",
            key: "runs",
            align: "right",
            render: (_, w) => <Space wrap>{!w.webhook && !w.name.startsWith("md:") && !w.error && <RunNow hub={hub} workflow={w} />}{!w.webhook && <CopyCommand label="Copy run command" command={workflowCommand("run", w.name, hub.path || hub.key)} />}<Button href={runsHref(hub, w.name)}>View runs</Button></Space>,
          },
        ]}
      />}
      <Webhooks hub={hub} />
      <Text type="secondary">
        People's own repeating requests on this agent are in <a href={hrefWith("/schedules", { hub: hub.key })}>Team schedules</a>.
      </Text>
    </div>
  );
}

/** One line that says whether a workflow will run, and what to do if not. */
function statusLine(w: Workflow): { text: React.ReactNode; tone?: "danger" | "warning" } {
  const when = w.webhook ? "when its webhook receives an event"
    : w.schedule ? `${(w.schedule_text ?? describeCron(w.schedule)).replace(/^./, (c) => c.toLowerCase())} (${w.timezone})` : "";
  const file = w.source ? <Text code>{w.source}</Text> : "its file";
  switch (w.state) {
    case "definition-disabled":
      return { text: <>Off. Turn it on with <Text code>enabled: true</Text> in {file}.</> };
    case "schedules_off":
      return { text: <>Schedules are off on this machine (<Text code>HUBZOID_SCHEDULES</Text>), so it won't run {when}.</>, tone: "warning" };
    case "disabled":
      return { text: <>Schedules are off here. Set <Text code>HUBZOID_SCHEDULES=1</Text> to run it {when}.</>, tone: "warning" };
    case "paused":
      return { text: <>Paused. Resume it with <Text code>hubzoid schedule resume</Text>.</>, tone: "warning" };
    case "stale":
      return { text: "The scheduler stopped. Restart the agent to run it again.", tone: "danger" };
    case "error":
      return { text: <>Can't run: {w.error}</>, tone: "danger" };
    case "event":
      return { text: `On. Runs ${when}.` };
    case "manual":
      return { text: "Runs when someone starts it." };
    default:
      return { text: <>On. Runs {when}.{w.next_run ? <> Next run {formatTime(w.next_run)}.</> : null}</> };
  }
}

/** Who a run acts as, in words. The local owner is "you". */
function runsAs(w: Workflow): string | null {
  if (!w.runs_as) return null;
  if (w.runs_as.error) return `Can't run: ${w.runs_as.error}`;
  return w.runs_as.account === "admin@localhost" ? "Runs as you (the local owner)" : `Runs as ${w.runs_as.account}`;
}

/** In Settings: each workflow by name, what it does, and one status line
 *  (HUB-140 F31). The run command sits in a small menu. */
function WorkflowRows({ hub, workflows }: { hub: Hub; workflows: Workflow[] }) {
  return <ul className="hz-workflow-list" aria-label="Workflows">
    {workflows.map((w) => {
      const status = statusLine(w);
      return <li key={w.name}>
        <div className="hz-workflow-info">
          <div className="hz-workflow-title"><a href={runsHref(hub, w.name)}>{w.title ?? w.name}</a></div>
          {w.purpose && <Text type="secondary">{w.purpose}</Text>}
          <Text type={status.tone}>{status.text}</Text>
          {runsAs(w) && <Text type={w.runs_as?.error ? "danger" : "secondary"} className="hz-workflow-account">{runsAs(w)}</Text>}
          {w.as_tool && <div><Tag>Run in chat</Tag>{w.schedulable && <Tag>Personal schedules</Tag>}</div>}
        </div>
        <div className="hz-workflow-actions">
          {!w.webhook && !w.name.startsWith('md:') && !w.error && <RunNow hub={hub} workflow={w} />}
          <Button href={runsHref(hub, w.name)}>View runs</Button>
          {!w.webhook && (
            <Dropdown trigger={["click"]} menu={{ items: [{ key: "copy", label: "Copy run command",
              onClick: () => void navigator.clipboard.writeText(workflowCommand('run', w.name, hub.path || hub.key)).then(() => message.success("Copied")) }] }}>
              <Button aria-label="More" icon={<MoreHorizontal size={16} />} />
            </Dropdown>
          )}
        </div>
      </li>;
    })}
  </ul>;
}

function RunNow({ hub, workflow }: { hub: Hub; workflow: Workflow }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  const schema = workflow.input_schema;
  const account = workflow.runs_as?.account ?? "the workflow's account";
  const start = async () => {
    const values = schema ? await form.validateFields() : {};
    const inputs = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined && v !== null && v !== ""));
    setBusy(true);
    try {
      const out = await request<{ run_id: string; already_running: boolean }>("/workflows/run", {
        hub: hub.key, workflow: workflow.name, inputs,
      });
      message.success(out.already_running ? "A run is already in progress." : "Run started.");
      setOpen(false);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "Could not start the run.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button size="small" icon={<Play size={14} />} onClick={() => (schema ? setOpen(true) : start())}>
        Run now
      </Button>
      <Modal title={`Run ${workflow.name}`} open={open} onCancel={() => setOpen(false)}
        okText="Run" onOk={start} confirmLoading={busy} destroyOnHidden>
        <Paragraph type="secondary">
          Runs as {account}, the workflow's account.
        </Paragraph>
        <Form form={form} layout="vertical">
          {schema && Object.entries(schema.properties).map(([name, prop]) =>
            inputField(name, prop, (schema.required ?? []).includes(name), undefined, schema.$defs))}
        </Form>
      </Modal>
    </>
  );
}

const WEBHOOK_STATES = [
  ["accepted", "Waiting", "default"],
  ["running", "Running", "blue"],
  ["succeeded", "Succeeded", "green"],
  ["failed", "Failed", "red"],
] as const;

// Read-only: the webhooks this agent declares, their last day of events and the
// latest failures. Redriving stays a server command an operator runs.
function Webhooks({ hub }: { hub: Hub }) {
  const data = useData<{ webhooks: Webhook[]; error?: string }>("/webhooks" + query({ hub: hub.key }));
  if (!data.data) return data.error ? <LoadState error={data.error} retry={data.reload} /> : null;
  const { webhooks, error } = data.data;
  if (!webhooks.length && !error) return null;
  return (
    <div className="section">
      <Title level={5}>Webhooks</Title>
      {error && <Alert className="notice" type="error" showIcon message={`Webhook settings could not be read: ${error}`} />}
      <Table<Webhook>
        rowKey="name"
        size="middle"
        dataSource={webhooks}
        pagination={false}
        scroll={{ x: 720 }}
        expandable={{
          rowExpandable: (h) => h.failures.length > 0,
          expandedRowRender: (h) => (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {h.failures.map((f) => (
                <div key={f.id}>
                  <Text strong>{f.workflow}</Text>{" "}
                  <Text type="secondary">
                    · failed {formatTime(f.updated)} after {f.attempt}{" "}
                    {f.attempt === 1 ? "attempt" : "attempts"}
                  </Text>
                  {f.error && (
                    <div>
                      <Text type="danger">{f.error}</Text>
                    </div>
                  )}
                  <div>
                    <CopyCommand label="Copy redrive command" command={f.redrive} />
                  </div>
                </div>
              ))}
            </Space>
          ),
        }}
        columns={[
          {
            title: "Webhook",
            key: "name",
            render: (_, h) => (
              <>
                <div>{h.name}</div>
                <Text code copyable className="hint">
                  {h.url}
                </Text>
              </>
            ),
          },
          {
            title: "Starts",
            key: "workflows",
            render: (_, h) =>
              h.workflows.length ? (
                h.workflows.map((w) => (
                  <div key={w}>
                    <a href={runsHref(hub, w)}>{w}</a>
                  </div>
                ))
              ) : (
                <Text type="secondary">No workflow</Text>
              ),
          },
          { title: "Verification", key: "verify", render: (_, h) => <Text>{h.verify}</Text> },
          {
            title: "Last 24 hours",
            key: "last_24h",
            render: (_, h) => (
              <Space size={4} wrap>
                {WEBHOOK_STATES.filter(([k]) => h.last_24h[k] > 0).map(([k, label, color]) => (
                  <Tag key={k} color={color} style={{ marginInlineEnd: 0 }}>
                    {h.last_24h[k]} {label.toLowerCase()}
                  </Tag>
                ))}
                {WEBHOOK_STATES.every(([k]) => !h.last_24h[k]) && <Text type="secondary">No events</Text>}
              </Space>
            ),
          },
        ]}
      />
    </div>
  );
}

function RunList({ hub, workflow }: { hub: Hub; workflow: string }) {
  const [page, setPage] = useState(1);
  const data = useData<{ runs: Run[] }>(
    "/runs" + query({ hub: hub.key, workflow, offset: (page - 1) * PAGE, limit: PAGE }),
  );
  return (
    <div className="panel">
      <Breadcrumb
        items={[
          { title: <a href={runsHref(hub)}>Workflows</a> },
          { title: workflow },
        ]}
      />
      <div className="panel-heading">
        <div>
          <Title level={2}>
            Runs of {workflow} <Text type="secondary">· {hub.name}</Text>
          </Title>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
          Refresh
        </Button>
      </div>
      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : (
        <Table<Run>
          rowKey="id"
          size="middle"
          dataSource={data.data.runs}
          scroll={{ x: 640 }}
          pagination={{
            current: page,
            pageSize: PAGE,
            onChange: setPage,
            showSizeChanger: false,
            // The API does not return a total; allow paging forward while a full page came back.
            total: (page - 1) * PAGE + data.data.runs.length + (data.data.runs.length === PAGE ? 1 : 0),
            hideOnSinglePage: true,
          }}
          locale={{
            emptyText: (
              <Empty
                description={
                  <>
                    <div>No runs yet <Help text="Runs appear after the schedule fires or an operator starts the workflow on the server." /></div>
                  </>
                }
              />
            ),
          }}
          columns={[
            {
              title: "Run",
              key: "id",
              render: (_, r) => (
                <a href={runsHref(hub, workflow, r.id)} className="identity run-id-link" title={r.id}>
                  {shortRunId(r.id)}
                </a>
              ),
            },
            {
              title: "Status",
              key: "status",
              width: 130,
              render: (_, r) => <RunStatusTag status={r.status} />,
            },
            {
              title: "Started",
              key: "started",
              render: (_, r) => (
                <>
                  <When value={r.started} />
                  <div>
                    <Text type="secondary">{formatTime(r.started)}</Text>
                  </div>
                </>
              ),
            },
            {
              title: "Duration",
              key: "duration",
              width: 120,
              render: (_, r) => formatDuration(r.duration_ms),
            },
            {
              title: "Trigger",
              key: "trigger",
              render: (_, r) => r.trigger ? <Tag>{TRIGGERS[r.trigger] ?? r.trigger}</Tag> : <Text type="secondary">—</Text>,
            },
            {
              title: "Started by",
              key: "started_by",
              render: (_, r) => r.started_by ? <Text className="identity">{r.started_by}</Text> : <Text type="secondary">{r.trigger === "personal_schedule" ? "Schedule" : r.trigger === "webhook" ? "Webhook" : "Schedule"}</Text>,
            },
            {
              title: "Runs as",
              key: "run_as",
              render: (_, r) =>
                r.run_as ? <Text className="identity">{r.run_as}</Text> : <Text type="secondary">—</Text>,
            },
            {
              title: "Inputs",
              key: "inputs",
              render: (_, r) => r.inputs ? <Text type="secondary">{r.inputs}</Text> : <Text type="secondary">—</Text>,
            },
            {
              title: "Result",
              key: "result",
              ellipsis: true,
              render: (_, r) =>
                r.error ? (
                  <Text type="danger" ellipsis>
                    {r.error}
                  </Text>
                ) : r.output ? (
                  <Text ellipsis>{resultSummary(r.output)}</Text>
                ) : r.redacted ? (
                  <Text type="secondary">Private <Help text={`Only ${r.run_as || "the run's account"} can see this result.`} /></Text>
                ) : (
                  <Text type="secondary">—</Text>
                ),
            },
          ]}
        />
      )}
    </div>
  );
}

function RunDetail({ hub, workflow, run }: { hub: Hub; workflow: string; run: string }) {
  const data = useData<{ runs: Run[] }>(
    "/runs" + query({ hub: hub.key, workflow, run_id: run }),
  );
  const catalog = useData<{ workflows: Workflow[] }>("/workflows" + query({ hub: hub.key }));
  const definition = catalog.data?.workflows.find((w) => w.name === workflow);
  const crumbs = (
    <Breadcrumb
      items={[
        { title: <a href={runsHref(hub)}>Workflows</a> },
        { title: <a href={runsHref(hub, workflow)}>{workflow}</a> },
        { title: <span className="identity" title={run}>{shortRunId(run)}</span> },
      ]}
    />
  );
  if (!data.data)
    return (
      <div className="panel">
        {crumbs}
        <LoadState error={data.error} retry={data.reload} />
      </div>
    );
  const r = data.data.runs[0];
  if (!r)
    return (
      <div className="panel">
        {crumbs}
        <RecoveryScreen
          title="Run not found"
          subtitle={`No run ${run} of ${workflow} is recorded for ${hub.name}. It may have been pruned, or the link is from another deployment.`}
          to={`/agents/${encodeURIComponent(hub.key)}/runs/${encodeURIComponent(workflow)}`}
          label="Back to runs"
        />
      </div>
    );
  const steps = r.steps ?? [];
  return (
    <div className="panel">
      {crumbs}
      <div className="panel-heading">
        <div>
          <Title level={2}>
            <Space>
              <span>Run of {workflow}</span>
              <RunStatusTag status={r.status} />
            </Space>
          </Title>
          <Paragraph type="secondary">
            {hub.name} · started {formatTime(r.started)}
          </Paragraph>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
          Refresh
        </Button>
      </div>
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, lg: 4 }}
        items={[
          { key: "id", label: "Run id", children: <Text className="identity" title={r.id} copyable={{ text: r.id }}>{shortRunId(r.id)}</Text> },
          { key: "started", label: "Started", children: formatTime(r.started) },
          { key: "completed", label: "Completed", children: formatTime(r.completed) },
          { key: "duration", label: "Duration", children: formatDuration(r.duration_ms) },
          {
            key: "run_as",
            label: "Runs as",
            children: r.run_as ? <Text className="identity">{r.run_as}</Text> : "—",
          },
        ]}
      />
      <div className="section">
        <Title level={3}>Result</Title>
        {r.error ? (
          <Alert type="error" showIcon title="The run failed" description={<pre className="output">{r.error}</pre>} />
        ) : r.output ? (
          <WorkflowResult output={r.output} />
        ) : r.redacted ? (
          <Text type="secondary">
            Private <Help text={`Only ${r.run_as || "the account the run acted as"} can see this result. Managing the hub does not grant access.`} />
          </Text>
        ) : (
          <Text type="secondary">
            {/pending|enqueued/i.test(r.status) ? "Still running — no output yet." : "The run produced no output."}
          </Text>
        )}
      </div>
      <div className="section">
        <Title level={3}>Steps</Title>
        {steps.length === 0 ? (
          <Text type="secondary">No steps were recorded for this run.</Text>
        ) : (
          <Collapse
            items={steps.map((step, i) => ({
              key: String(i),
              label: (
                <Space wrap>
                  <Text strong>{i + 1}.</Text>
                  <Text code>{step.name}</Text>
                  {step.error ? <Tag color="red">Failed</Tag> : <Tag color="green">Done</Tag>}
                  <Text type="secondary">
                    {formatTime(step.started)}
                    {step.started && step.completed
                      ? ` · ${formatDuration(step.completed - step.started)}`
                      : ""}
                  </Text>
                </Space>
              ),
              children: (
                <pre className="output">
                  {step.error ||
                    prettyOutput(step.output) ||
                    (step.redacted
                      ? `Private to ${r.run_as || "the account the run acted as"}.`
                      : "No output recorded.")}
                </pre>
              ),
            }))}
          />
        )}
      </div>
      <Space>
        {/PENDING|ENQUEUED|DELAYED/i.test(r.status) && <CopyCommand label="Copy cancel command" command={workflowCommand("cancel", r.id, hub.path || hub.key)} />}
        {definition && !definition.webhook && !/PENDING|ENQUEUED|DELAYED/i.test(r.status) && <><CopyCommand label="Copy run again command" command={workflowCommand("run", workflow, hub.path || hub.key)} /><Help text="Starts a new run using the current workflow and account settings. Completed side effects are not undone." /></>}
        <Button href={runsHref(hub, workflow)}>Back to runs</Button>
        <Button href={agentHref(hub.key, "activity")}>Agent activity</Button>
      </Space>
    </div>
  );
}
