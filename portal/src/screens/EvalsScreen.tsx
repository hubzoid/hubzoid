import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Pagination,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import {
  query,
  type EvalCaseResult,
  type EvalCaseRow,
  type EvalRunDetail,
  type EvalRunState,
  type EvalRunSummary,
  type EvalToolCall,
  type EvalsOverview,
  type Hub,
} from "../api";
import { useData } from "../hooks/useData";
import { href, navigate } from "../hooks/useRoute";
import { LoadState, When } from "../components/common";
import { describeCron, formatDuration, formatTime } from "../lib/format";

const { Text, Title, Paragraph } = Typography;

/** How often the screen refreshes while an eval run is queued or running. */
const POLL_MS = 3000;

const evalsPath = (hub: Hub, stamp?: string) =>
  `/agents/${encodeURIComponent(hub.key)}/evals` + (stamp ? `/${encodeURIComponent(stamp)}` : "");

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const TRIGGERS: Record<string, string> = {
  console: "Console",
  schedule: "Schedule",
  cli: "Command line",
  ci: "CI",
};
const triggerLabel = (t: string | null) => (t ? TRIGGERS[t] ?? t : "Not recorded");

const ACTIVE = new Set(["ENQUEUED", "PENDING"]);
const FAILED = new Set(["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"]);

const EXAMPLE = "## Prompt\nWhat is the refund window?\n\n## Criteria\nStates 14 days.";

/** A few short arguments for a tool call's one-line summary. */
const KEY_ARGS = ["name", "path", "file", "query", "q", "url", "id", "title"];
function keyArgs(args: unknown): string {
  const clip = (s: string) => (s.length > 60 ? s.slice(0, 59) + "…" : s);
  if (args == null) return "";
  if (typeof args !== "object") return clip(String(args));
  if (Array.isArray(args)) return clip(JSON.stringify(args));
  const entries = Object.entries(args as Record<string, unknown>).filter(
    ([, v]) => ["string", "number", "boolean"].includes(typeof v),
  );
  entries.sort(([a], [b]) => {
    const ia = KEY_ARGS.indexOf(a);
    const ib = KEY_ARGS.indexOf(b);
    return (ia < 0 ? KEY_ARGS.length : ia) - (ib < 0 ? KEY_ARGS.length : ib);
  });
  return entries
    .slice(0, 3)
    .map(([k, v]) => `${k}: ${clip(String(v))}`)
    .join(", ");
}

function Verdict({ passed }: { passed: boolean }) {
  return (
    <Tag color={passed ? "green" : "red"} style={{ marginInlineEnd: 0 }}>
      {passed ? "Passed" : "Failed"}
    </Tag>
  );
}

export function EvalsScreen({ hub, run }: { hub: Hub; run?: string }) {
  const [page, setPage] = useState(1);
  const overview = useData<EvalsOverview>("/evals" + query({ hub: hub.key }), { keepStale: true });
  const runs = useData<{ runs: EvalRunSummary[]; total: number }>("/evals/runs" + query({ hub: hub.key, offset: (page-1)*50, limit: 50 }), {
    keepStale: true,
  });
  const active = overview.data?.active ?? null;
  const reloadAll = () => {
    overview.reload();
    runs.reload();
  };

  // While a run is queued or running, refresh until it finishes; then fetch the
  // runs once more so the new results file shows.
  const wasActive = useRef(false);
  const { reload: reloadOverview } = overview;
  const { reload: reloadRuns } = runs;
  useEffect(() => {
    if (!active) {
      if (wasActive.current) reloadRuns();
      wasActive.current = false;
      return;
    }
    wasActive.current = true;
    const timer = window.setInterval(() => {
      reloadOverview();
      reloadRuns();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [active, reloadOverview, reloadRuns]);

  if (!overview.data) return <LoadState error={overview.error} retry={overview.reload} />;
  const data = overview.data;

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={2}>Evals in {hub.name}</Title>
          <Paragraph type="secondary">
            Checks that this agent still answers as it should. Each case is a markdown file in the agent’s{" "}
            <Text code>evals</Text> folder.
          </Paragraph>
        </div>
        <Space wrap>
          <Button icon={<RefreshCw size={16} />} onClick={reloadAll}>
            Refresh
          </Button>

        </Space>
      </div>
      <RunStateAlerts data={data} />
      {data.errors.length > 0 && (
        <Alert
          className="notice"
          type="error"
          showIcon
          title="These case files can’t be read"
          description={
            <>
              <ul className="eval-errors">
                {data.errors.map((e) => (
                  <li key={e.file}>
                    <Text code>{e.file}</Text> {e.error}
                  </li>
                ))}
              </ul>
              Fix them before running evals. <a href={data.docs}>How eval cases work</a>
            </>
          }
        />
      )}
      {!data.folder || (data.cases.length === 0 && data.errors.length === 0) ? (
        <NoCases hub={hub} docs={data.docs} folder={data.folder} />
      ) : (
        <>
          <CasesTable hub={hub} cases={data.cases} />
          <div className="section">
            <Title level={3}>Runs</Title>
            {!runs.data ? (
              <LoadState error={runs.error} retry={runs.reload} rows={2} />
            ) : (
              <><RunsTable hub={hub} runs={runs.data.runs} /><Pagination current={page} pageSize={50} total={runs.data.total} onChange={setPage} showSizeChanger={false} /></>
            )}
          </div>
        </>
      )}
      {run && <RunDrawer hub={hub} stamp={run} />}
    </div>
  );
}

function RunStateAlerts({ data }: { data: EvalsOverview }) {
  const { active, last } = data;
  return (
    <>
      {data.state_error && <Alert className="notice" type="warning" showIcon title={data.state_error} />}
      {active && (
        <Alert
          className="notice"
          type="info"
          showIcon
          icon={<Spin size="small" />}
          title={active.status === "ENQUEUED" ? "An eval run is queued" : "Evals are running"}
          description={<RunStateText state={active} />}
        />
      )}
      {!active && last && FAILED.has(last.status) && (
        <Alert
          className="notice"
          type="error"
          showIcon
          title={last.status === "CANCELLED" ? "The last eval run was cancelled" : "The last eval run failed"}
          description={
            <>
              <RunStateText state={last} />
              {last.error && <pre className="output">{last.error}</pre>}
            </>
          }
        />
      )}
    </>
  );
}

function RunStateText({ state }: { state: EvalRunState }) {
  const what = state.cases ? plural(state.cases.length, "case", "cases") : "Every enabled case";
  const who = state.source === "console" ? (state.requested_by ? ` by ${state.requested_by}` : "") : " by the schedule";
  return (
    <span>
      {what}
      {state.judge === false ? ", without the judge" : ""}. Started{who}{" "}
      <When value={state.created} />.
      {ACTIVE.has(state.status) && (
        <>
          {" "}
          {state.status === "ENQUEUED"
            ? "It waits while other scheduled work in this agent finishes. "
            : ""}
          This page refreshes until it finishes.
        </>
      )}
    </span>
  );
}

function NoCases({ hub, docs, folder }: { hub: Hub; docs: string; folder: boolean }) {
  return (
    <Empty
      description={
        <div className="eval-empty">
          <div>
            {folder ? `The evals folder in ${hub.name} has no cases yet.` : `${hub.name} has no evals folder yet.`}
          </div>
          <Text type="secondary">
            Add a folder named <Text code>evals</Text> to the agent’s folder, with one markdown file per case. For
            example, <Text code>evals/refund-window.md</Text>:
          </Text>
          <pre className="output eval-example">{EXAMPLE}</pre>
          <Text type="secondary">
            Run cases from the command line, CI, or their schedule. Evals use real tools and can have side effects. <a href={docs}>How eval cases work</a>
          </Text>
        </div>
      }
    />
  );
}

function CasesTable({ hub, cases }: { hub: Hub; cases: EvalCaseRow[] }) {
  return (
    <Table<EvalCaseRow>
      rowKey="name"
      size="middle"
      dataSource={cases}
      pagination={false}
      scroll={{ x: 900 }}
      columns={[
        {
          title: "Case",
          key: "name",
          render: (_, c) => (
            <>
              <Space size={4} wrap>
                <Text strong>{c.name}</Text>
                {c.tags.map((t) => (
                  <Tag key={t} style={{ marginInlineEnd: 0 }}>
                    {t}
                  </Tag>
                ))}
              </Space>
              <div>
                <Text type="secondary" className="hint" ellipsis={{ tooltip: c.prompt }} style={{ maxWidth: 320 }}>
                  {c.prompt}
                </Text>
              </div>
            </>
          ),
        },
        {
          title: "Turns",
          key: "turns",
          width: 80,
          render: (_, c) => c.turns,
        },
        {
          title: "Schedule",
          key: "schedule",
          render: (_, c) =>
            c.schedule ? (
              <>
                <div>{describeCron(c.schedule)}</div>
                <Text code>{c.schedule}</Text>
              </>
            ) : (
              <Text type="secondary">On demand</Text>
            ),
        },
        {
          title: "Judged",
          key: "judged",
          render: (_, c) => (
            <Tooltip
              title={
                [c.judged ? "Graded by a judge model against its criteria." : "Checks only, no judge.", ...c.checks]
                  .filter(Boolean)
                  .join(" · ") || undefined
              }
            >
              {c.judged ? <Tag color="blue">Judged</Tag> : <Text type="secondary">Checks only</Text>}
            </Tooltip>
          ),
        },
        {
          title: "Runs as",
          key: "run_as",
          render: (_, c) =>
            c.run_as ? <Text className="identity">{c.run_as}</Text> : <Text type="secondary">Default</Text>,
        },
        {
          title: "State",
          key: "enabled",
          render: (_, c) => (c.enabled ? <Tag color="green">Enabled</Tag> : <Tag>Disabled</Tag>),
        },
        {
          title: "Latest result",
          key: "latest",
          render: (_, c) =>
            c.latest ? (
              <>
                <Space size={6} wrap>
                  <a href={href(evalsPath(hub, c.latest.stamp))} aria-label={`${c.name}: open its latest run`}>
                    <Verdict passed={c.latest.passed} />
                  </a>
                  <When value={c.latest.finished} />
                </Space>
                {!c.latest.passed && c.latest.reason && (
                  <div>
                    <Text type="danger" className="hint">
                      {c.latest.reason}
                    </Text>
                  </div>
                )}
              </>
            ) : (
              <Text type="secondary">Not run yet</Text>
            ),
        },
      ]}
    />
  );
}

function RunsTable({ hub, runs }: { hub: Hub; runs: EvalRunSummary[] }) {
  return (
    <Table<EvalRunSummary>
      rowKey="stamp"
      size="middle"
      dataSource={runs}
      scroll={{ x: 720 }}
      pagination={false}
      locale={{
        emptyText: (
          <Empty
            description={
              <>
                <div>No eval runs yet.</div>
                <Text type="secondary">
                  Use the command line to record the first results. Runs from the command line (
                  <Text code>hubzoid eval run</Text>) and from case schedules appear here too.
                </Text>
              </>
            }
          />
        ),
      }}
      columns={[
        {
          title: "Run",
          key: "stamp",
          render: (_, r) => (
            <a href={href(evalsPath(hub, r.stamp))} className="identity">
              {r.stamp}
            </a>
          ),
        },
        {
          title: "Trigger",
          key: "trigger",
          render: (_, r) => <Tag style={{ marginInlineEnd: 0 }}>{triggerLabel(r.trigger)}</Tag>,
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
          title: "Model",
          key: "model",
          render: (_, r) => (r.model ? <Text className="identity">{r.model}</Text> : <Text type="secondary">—</Text>),
        },
        { title: "Passed", key: "passed", width: 90, render: (_, r) => r.passed },
        {
          title: "Failed",
          key: "failed",
          width: 90,
          render: (_, r) => (r.failed ? <Text type="danger">{r.failed}</Text> : 0),
        },
      ]}
    />
  );
}

function RunDrawer({ hub, stamp }: { hub: Hub; stamp: string }) {
  const detail = useData<EvalRunDetail>(`/evals/runs/${encodeURIComponent(stamp)}` + query({ hub: hub.key }));
  const close = () => navigate(evalsPath(hub));
  const title = `Eval run ${stamp}`;
  const d = detail.data;
  return (
    <Drawer title={title} aria-label={title} open onClose={close} size={760} destroyOnHidden>
      {!d ? (
        detail.status === 404 ? (
          <Empty description={`No eval run ${stamp} is recorded for ${hub.name}. Its file may have been pruned.`} />
        ) : (
          <LoadState error={detail.error} retry={detail.reload} />
        )
      ) : (
        <div className="drawer-body">
          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2 }}
            items={[
              {
                key: "result",
                label: "Result",
                children: (
                  <Space size={6}>
                    <Verdict passed={d.failed === 0} />
                    <span>
                      {d.passed} passed, {d.failed} failed
                    </span>
                  </Space>
                ),
              },
              { key: "trigger", label: "Trigger", children: triggerLabel(d.trigger) },
              { key: "started", label: "Started", children: formatTime(d.started) },
              { key: "finished", label: "Finished", children: formatTime(d.finished) },
              { key: "model", label: "Model", children: d.model ? <Text className="identity">{d.model}</Text> : "—" },
              {
                key: "judge",
                label: "Judge",
                children: d.judged ? (d.judge_model ? <Text className="identity">{d.judge_model}</Text> : "On") : "Off",
              },
              ...(d.run_as
                ? [{ key: "run_as", label: "Runs as", children: <Text className="identity">{d.run_as}</Text> }]
                : []),
            ]}
          />
          {d.cases.length === 0 ? (
            <Text type="secondary">This run recorded no cases.</Text>
          ) : (
            <Collapse
              defaultActiveKey={d.cases.filter((c) => !c.passed).map((c) => c.name)}
              items={d.cases.map((c) => ({
                key: c.name,
                label: (
                  <Space wrap>
                    <Verdict passed={c.passed} />
                    <Text strong>{c.name}</Text>
                    <Text type="secondary">{formatDuration(Math.round(c.duration * 1000))}</Text>
                    {c.judge && !c.judge.error && (
                      <Tag color={c.judge.passed ? "green" : "red"} style={{ marginInlineEnd: 0 }}>
                        Judge {c.judge.score}/10
                      </Tag>
                    )}
                  </Space>
                ),
                children: <CaseResult result={c} />,
              }))}
            />
          )}
        </div>
      )}
    </Drawer>
  );
}

function CaseResult({ result: c }: { result: EvalCaseResult }) {
  if (c.private) return <Alert type="info" showIcon title="Private result" description="Details are visible only to the account this evaluation ran as. The verdict remains visible." />;
  const multi = (c.turns?.length ?? 0) > 1;
  return (
    <div className="eval-case">
      {!c.passed && c.reason && <Alert type="error" showIcon title={c.reason} />}
      {c.run_as && (
        <Text type="secondary">
          Ran as <Text className="identity">{c.run_as}</Text>
        </Text>
      )}
      <section>
        <Title level={4}>Checks</Title>
        {c.checks.length === 0 ? (
          <Text type="secondary">No checks in this case.</Text>
        ) : (
          <ul className="eval-checks">
            {c.checks.map((k, i) => (
              <li key={i}>
                <Tag color={k.passed ? "green" : "red"} style={{ marginInlineEnd: 6 }}>
                  {k.passed ? "Pass" : "Fail"}
                </Tag>
                <Text code>{k.kind}</Text>
                {k.detail ? <Text> {k.detail}</Text> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
      {c.judge && (
        <section>
          <Title level={4}>Judge</Title>
          {c.judge.error ? (
            <Text type="danger">The judge could not grade this answer: {c.judge.error}</Text>
          ) : (
            <>
              <Text strong>
                {c.judge.score}/10
              </Text>{" "}
              <Text type="secondary">
                (passes at {c.judge.threshold}
                {c.judge.model ? `, ${c.judge.model}` : ""})
              </Text>
              {c.judge.reasoning && <Paragraph className="eval-reasoning">{c.judge.reasoning}</Paragraph>}
            </>
          )}
        </section>
      )}
      <section>
        <Title level={4}>{multi ? "Conversation" : "Answer"}</Title>
        {multi ? (
          <ol className="eval-transcript" aria-label="Conversation">
            {c.turns!.map((t, i) => (
              <li key={i}>
                <div className="eval-turn-label">Turn {i + 1}</div>
                <div className="eval-msg user">
                  <span className="eval-who">Prompt</span>
                  <span>{t.prompt}</span>
                </div>
                <div className="eval-msg agent">
                  <span className="eval-who">Reply</span>
                  <span>{t.response || <Text type="secondary">(empty)</Text>}</span>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <>
            {c.prompt && (
              <div className="eval-prompt">
                <Text type="secondary">Prompt (from the case file now):</Text> {c.prompt}
              </div>
            )}
            {c.error ? (
              <Alert type="error" showIcon title="The run failed" description={<pre className="output">{c.error}</pre>} />
            ) : (
              <pre className="output">{c.answer || "(empty)"}</pre>
            )}
          </>
        )}
        {multi && c.error && (
          <Alert type="error" showIcon title="The run failed" description={<pre className="output">{c.error}</pre>} />
        )}
      </section>
      <section>
        <Title level={4}>Tool calls</Title>
        {c.tools.length === 0 ? (
          <Text type="secondary">No tools were called.</Text>
        ) : (
          <ol className="eval-tools" aria-label={`Tool calls in ${c.name}`}>
            {c.tools.map((t, i) => (
              <ToolCall key={i} call={t} multi={multi} />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function ToolCall({ call: t, multi }: { call: EvalToolCall; multi: boolean }) {
  const args = keyArgs(t.args);
  const full = t.args != null && typeof t.args === "object" ? JSON.stringify(t.args, null, 2) : null;
  return (
    <li className="eval-tool">
      <Space size={6} wrap>
        <Text code>{t.name}</Text>
        {args && <Text className="identity">{args}</Text>}
        {t.ok === true ? (
          <Tag color="green" style={{ marginInlineEnd: 0 }}>
            OK
          </Tag>
        ) : t.ok === false ? (
          <Tag color="red" style={{ marginInlineEnd: 0 }}>
            Error
          </Tag>
        ) : null}
        {t.duration_ms != null && <Text type="secondary">{formatDuration(t.duration_ms)}</Text>}
        {multi && t.turn != null && <Text type="secondary">Turn {t.turn}</Text>}
      </Space>
      {t.error && (
        <div>
          <Text type="danger">{t.error}</Text>
        </div>
      )}
      {(full || t.preview) && (
        <details className="eval-tool-more">
          <summary>Details</summary>
          {full && <pre className="output">{full}</pre>}
          {t.preview && (
            <>
              <Text type="secondary">Result preview</Text>
              <pre className="output">{t.preview}</pre>
            </>
          )}
        </details>
      )}
    </li>
  );
}
