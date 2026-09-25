import { Alert, Card, Segmented, Table, Tooltip, Typography } from "antd";
import { type Summary, type SummaryHub } from "../api";
import { useData } from "../hooks/useData";
import { agentHref, useHashQuery } from "../hooks/useRoute";
import { LoadState, PageHeader } from "../components/common";
import { formatTime, relativeTime } from "../lib/format";

const { Text } = Typography;

const PERIODS = [
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];

const count = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());

const short = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);

function tokens(input: number, output: number) {
  return `${short(input)} in · ${short(output)} out`;
}

function Cost({ usd, unpriced }: { usd: number | null; unpriced: number }) {
  if (usd == null)
    return (
      <Tooltip title={unpriced ? "No model price is known for these calls." : "No model calls in this period."}>
        <Text type="secondary">Unavailable</Text>
      </Tooltip>
    );
  const value = usd < 0.01 && usd > 0 ? "< $0.01" : `$${usd.toFixed(2)}`;
  return unpriced ? (
    <Tooltip title={`${unpriced.toLocaleString()} calls had no known price and are not included.`}>
      <span>{value}*</span>
    </Tooltip>
  ) : (
    <>{value}</>
  );
}

function Stat({
  label,
  value,
  hint,
  sub,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  sub?: React.ReactNode;
}) {
  return (
    <Card size="small" className="stat">
      <div className="stat-label">
        {hint ? (
          <Tooltip title={hint}>
            <span className="stat-hint">{label}</span>
          </Tooltip>
        ) : (
          label
        )}
      </div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </Card>
  );
}

function Access({ row }: { row: SummaryHub }) {
  if (row.users_with_access == null)
    return (
      <Tooltip title="Access for this agent is still managed with chat app groups.">
        <Text type="secondary">In chat app</Text>
      </Tooltip>
    );
  if (row.everyone) return <>Everyone</>;
  return <>{count(row.users_with_access)}</>;
}

export function HomeScreen() {
  const [q, setQ] = useHashQuery();
  const period = PERIODS.some((p) => p.value === q.period) ? q.period : "7d";
  const summary = useData<Summary>(`/summary?period=${period}`, { keepStale: true });
  const s = summary.data;

  return (
    <>
      <PageHeader
        eyebrow="Your workspace"
        title="Overview"
        description="How people are using your agents. Counted by Hubzoid for every chat surface."
        extra={
          <Segmented
            aria-label="Period"
            value={period}
            onChange={(v) => setQ({ period: v === "7d" ? undefined : String(v) })}
            options={PERIODS}
          />
        }
      />
      {!s ? (
        <LoadState error={summary.error} retry={summary.reload} />
      ) : (
        <>
          {s.recording_since == null ? (
            <Alert
              type="info"
              showIcon
              className="home-note"
              title="No conversations recorded yet"
              description="Numbers appear here after the first message to any agent."
            />
          ) : s.recording_since > s.since ? (
            <Text type="secondary" className="home-note">
              Counting since {formatTime(s.recording_since)}, when this deployment started recording usage.
            </Text>
          ) : null}
          <div className="stat-grid" role="list" aria-label="Totals">
            <Stat label="Conversations" value={count(s.totals.chats)} />
            <Stat label="Messages" value={count(s.totals.messages)} hint="Messages people sent, on every surface." />
            <Stat label="Active people" value={count(s.totals.active_users)} hint="Signed-in people who sent at least one message." />
            <Stat
              label="Tokens"
              value={short(s.totals.input_tokens + s.totals.output_tokens)}
              sub={tokens(s.totals.input_tokens, s.totals.output_tokens)}
              hint="Includes scheduled work and workflow model calls."
            />
            <Stat
              label="Estimated cost"
              value={<Cost usd={s.totals.cost_usd} unpriced={s.totals.unpriced} />}
              hint="From model prices. Your provider's bill is the reference."
            />
            <Stat label="Tool denials" value={count(s.totals.denials)} hint="Restricted tool calls that were refused." />
            {s.has_workflows && (
              <>
                <Stat
                  label="Workflow runs"
                  value={s.runs_available ? count(s.totals.runs) : "Unavailable"}
                />
                <Stat
                  label="Failed runs"
                  value={s.runs_available ? count(s.totals.failed) : "Unavailable"}
                />
              </>
            )}
          </div>
          <Table<SummaryHub>
            className="home-table"
            rowKey="key"
            size="middle"
            pagination={false}
            scroll={{ x: true }}
            dataSource={s.hubs}
            locale={{ emptyText: "No agents are assigned to you." }}
            onRow={(row) => ({
              onClick: () => {
                location.hash = agentHref(row.key).slice(1);
              },
              style: { cursor: "pointer" },
            })}
            columns={[
              {
                title: "Agent",
                key: "name",
                render: (_, row) => (
                  <a href={agentHref(row.key)} onClick={(e) => e.stopPropagation()}>
                    {row.name}
                  </a>
                ),
              },
              { title: "Conversations", dataIndex: "chats", align: "right", render: count },
              { title: "Messages", dataIndex: "messages", align: "right", render: count },
              { title: "Active people", dataIndex: "active_users", align: "right", render: count },
              {
                title: "Tokens",
                key: "tokens",
                render: (_, row) => tokens(row.input_tokens, row.output_tokens),
              },
              {
                title: "Est. cost",
                key: "cost",
                align: "right",
                render: (_, row) => <Cost usd={row.cost_usd} unpriced={row.unpriced} />,
              },
              {
                title: "Last message",
                dataIndex: "last_activity",
                render: (v: number | null) =>
                  v == null ? (
                    <Text type="secondary">None yet</Text>
                  ) : (
                    <Tooltip title={formatTime(v)}>{relativeTime(v)}</Tooltip>
                  ),
              },
              { title: "People with access", key: "access", render: (_, row) => <Access row={row} /> },
              { title: "Denials", dataIndex: "denials", align: "right", render: count },
              ...(s.has_workflows
                ? [
                    {
                      title: "Runs",
                      key: "runs",
                      align: "right" as const,
                      render: (_: unknown, row: SummaryHub) => (row.has_workflows ? count(row.runs) : ""),
                    },
                    {
                      title: "Failed",
                      key: "failed",
                      align: "right" as const,
                      render: (_: unknown, row: SummaryHub) =>
                        row.has_workflows ? (
                          row.failed ? <Text type="danger">{count(row.failed)}</Text> : count(row.failed)
                        ) : (
                          ""
                        ),
                    },
                  ]
                : []),
            ]}
          />
        </>
      )}
    </>
  );
}
