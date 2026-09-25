import { Alert, Button, Card, Segmented, Tooltip, Typography } from "antd";
import { type Hub, type Summary, type Workflow } from "../api";
import { useData } from "../hooks/useData";
import { useHashQuery } from "../hooks/useRoute";
import { LoadState, PageHeader } from "../components/common";
import { formatTime, short } from "../lib/format";
import { Cost } from "../components/UsageCost";
import { AgentCards } from "./AgentsScreen";

const { Text } = Typography;
const PERIODS = [
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];
const count = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());

function Stat({ label, value, hint, sub }: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  sub?: React.ReactNode;
}) {
  return (
    <Card size="small" className="stat" role="listitem">
      <div className="stat-label">
        {label}
        {hint && <Tooltip title={hint} trigger={["hover", "focus", "click"]}>
          <button className="stat-hint" aria-label={`About ${label}`}>ⓘ</button>
        </Tooltip>}
      </div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </Card>
  );
}

export function HomeScreen({ hubs, reloadHubs }: { hubs: Hub[]; reloadHubs: () => void }) {
  const [q, setQ] = useHashQuery();
  const period = PERIODS.some((p) => p.value === q.period) ? q.period : "7d";
  const summary = useData<Summary>(`/summary?period=${period}`, { keepStale: true });
  const workflows = useData<{ workflows: Workflow[] }>("/workflows", { keepStale: true });
  const s = summary.data;
  function refresh() {
    summary.reload();
    workflows.reload();
    reloadHubs();
  }

  return (
    <>
      <PageHeader title="Agents" extra={
        <div className="workspace-controls">
          <Segmented aria-label="Period" value={period}
            onChange={(v) => setQ({ period: v === "7d" ? undefined : String(v) })}
            options={PERIODS} />
          <div className="workspace-refresh">
            {summary.at && <Text type="secondary" className="updated-at" title={formatTime(summary.at)}>
              Updated {new Date(summary.at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
            </Text>}
            <Button aria-label="Refresh" onClick={refresh} loading={summary.refreshing || workflows.refreshing}>Refresh</Button>
          </div>
        </div>
      } />
      {!s ? <LoadState error={summary.error} retry={summary.reload} rows={2} /> : <>
        {summary.error && <Alert type="warning" showIcon className="home-note"
          title="Couldn’t refresh totals" description="Showing the last available numbers. Try refreshing again." />}
        <div className="stat-grid" role="list" aria-label="Totals">
          <Stat label="Messages" value={count(s.totals.messages)}
            sub={`Across ${count(s.totals.chats)} ${s.totals.chats === 1 ? "conversation" : "conversations"}`} />
          <Stat label="Users" value={count(s.totals.active_users)}
            hint="People who sent a message in the selected period." />
          <Stat label="Tokens used" value={short(s.totals.input_tokens + s.totals.output_tokens)}
            sub={`${short(s.totals.input_tokens)} in · ${short(s.totals.output_tokens)} out`}
            hint="Includes chat, background calls and workflow model calls." />
          <Stat label="Workflow runs" value={!s.has_workflows ? "0" : s.runs_available ? count(s.totals.runs) : "—"}
            sub={!s.has_workflows ? undefined : s.runs_available ? `${count(s.totals.failed)} failed` : "Unavailable"} />
          <Stat label="Approx. cost" value={<Cost usd={s.totals.cost_usd} unpriced={s.totals.unpriced} />}
            sub="USD" hint="Estimated from model token prices, including cached usage where available. Subscription-backed calls can have an estimate without an extra per-call charge. Missing prices are excluded. Your provider’s bill is authoritative." />
        </div>
      </>}
      {workflows.error && <Alert type="warning" showIcon className="home-note"
        title="Couldn’t refresh workflow status" description="Open an agent to check its workflows, or try refreshing again." />}
      <AgentCards hubs={hubs} workflows={workflows.data?.workflows} usage={s?.hubs} />
    </>
  );
}
