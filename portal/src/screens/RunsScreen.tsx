import { useState } from "react";
import {
  Alert,
  Breadcrumb,
  Button,
  Collapse,
  Descriptions,
  Empty,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import { query, type Hub, type Run, type Workflow } from "../api";
import { useData } from "../hooks/useData";
import { agentHref, href } from "../hooks/useRoute";
import {
  LoadState,
  RecoveryScreen,
  RunStatusTag,
  When,
  WorkflowStateTag,
} from "../components/common";
import { describeCron, prettyOutput, formatDuration, formatTime, workflowState } from "../lib/format";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

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
}: {
  hub: Hub;
  workflow?: string;
  run?: string;
}) {
  if (workflow && run) return <RunDetail hub={hub} workflow={workflow} run={run} />;
  if (workflow) return <RunList key={`${hub.key}:${workflow}`} hub={hub} workflow={workflow} />;
  return <WorkflowList hub={hub} />;
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
          message="Scheduler not running — scheduled workflows won’t fire until the bridge restarts."
        />
      )}
      {downtime && (
        <Alert
          className="notice"
          type="warning"
          showIcon
          banner
          message={`${downtime.missed} scheduled ${downtime.missed === 1 ? "run" : "runs"} missed while the scheduler was down (not back-filled).`}
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

function WorkflowList({ hub }: { hub: Hub }) {
  const data = useData<{ workflows: Workflow[] }>("/workflows" + query({ hub: hub.key }));
  if (!data.data) return <LoadState error={data.error} retry={data.reload} />;
  const workflows = data.data.workflows;
  // last_dispatch / missed are the agent's scheduler-level values (the dispatcher
  // serves all of this agent's workflows), not per-workflow — show them once.
  const sched = workflows[0];
  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={2}>Workflows in {hub.name}</Title>
          <Paragraph type="secondary">
            Scheduled and manual workflows defined in this agent, with their next run.
          </Paragraph>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
          Refresh
        </Button>
      </div>
      {sched && (
        <Text type="secondary" className="hint">
          Scheduler: last dispatch {sched.last_dispatch ? formatTime(sched.last_dispatch) : "never"}
          {sched.missed ? ` · ${sched.missed} missed run${sched.missed === 1 ? "" : "s"}` : ""}
        </Text>
      )}
      <HealthAlerts workflows={workflows} />
      <Table<Workflow>
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
                  <div>No workflows defined in {hub.name}.</div>
                  <Text type="secondary">
                    Start with a <a href="https://hubzoid.com/docs/guides/markdown-tasks">Markdown scheduled task</a> for a plain-language brief. For Python steps, run <Text code copyable>hubzoid new workflow my-workflow &lt;hub-folder&gt;</Text>, then follow the printed run command.
                  </Text>
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
              w.schedule ? (
                <>
                  <div>{describeCron(w.schedule)}</div>
                  <Text code>{w.schedule}</Text>
                  <div>
                    <Text type="secondary">{w.timezone}</Text>
                  </div>
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
                <div>
                  <Text type="secondary" className="hint">
                    {workflowState(w.state).hint}
                  </Text>
                </div>
              </>
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
            render: (_, w) => <Button href={runsHref(hub, w.name)}>View runs</Button>,
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
          <Paragraph type="secondary">Most recent first. Open a run to see its result and steps.</Paragraph>
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
                    <div>No recorded runs of {workflow} yet.</div>
                    <Text type="secondary">
                      Runs appear here after the schedule fires or the workflow is started manually with the CLI.
                    </Text>
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
                  {r.id}
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
              title: "Result",
              key: "result",
              ellipsis: true,
              render: (_, r) =>
                r.error ? (
                  <Text type="danger" ellipsis>
                    {r.error}
                  </Text>
                ) : r.output ? (
                  <Text ellipsis>{r.output}</Text>
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
  const crumbs = (
    <Breadcrumb
      items={[
        { title: <a href={runsHref(hub)}>Workflows</a> },
        { title: <a href={runsHref(hub, workflow)}>{workflow}</a> },
        { title: <span className="identity">{run}</span> },
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
          { key: "id", label: "Run id", children: <Text className="identity" copyable>{r.id}</Text> },
          { key: "started", label: "Started", children: formatTime(r.started) },
          { key: "completed", label: "Completed", children: formatTime(r.completed) },
          { key: "duration", label: "Duration", children: formatDuration(r.duration_ms) },
        ]}
      />
      <div className="section">
        <Title level={3}>Result</Title>
        {r.error ? (
          <Alert type="error" showIcon title="The run failed" description={<pre className="output">{r.error}</pre>} />
        ) : r.output ? (
          <pre className="output">{prettyOutput(r.output)}</pre>
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
                  {step.error || prettyOutput(step.output) || "No output recorded."}
                </pre>
              ),
            }))}
          />
        )}
      </div>
      <Space>
        <Button href={runsHref(hub, workflow)}>Back to runs</Button>
        <Button href={agentHref(hub.key, "activity")}>Agent activity</Button>
      </Space>
    </div>
  );
}
