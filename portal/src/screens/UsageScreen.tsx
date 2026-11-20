// Usage: one period and one agent filter drive every section. Totals against
// the previous period, tokens and cost over time, by channel and by what
// started the calls, a table of agents with their monthly limits (a row opens
// them as fields, budgets.yaml one click away), response times, by model and
// by person. Numbers come from GET /usage and /usage/people (hz_usage), plus
// /summary for workflow runs, missed slots, denied tools and accounts.
import { useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { App, Button, Input, Modal, Segmented, Select, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Info, RefreshCw, Search } from "lucide-react";
import { ApiError, request, type Hub, type Summary, type UsageHub, type UsageInsights, type UsageModel,
  type UsagePeople, type UsagePerson, type UsageTotals } from "../api";
import { useData } from "../hooks/useData";
import { useHashQuery } from "../hooks/useRoute";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar } from "../app/components/ui";
import { formatRelative } from "../app/lib/format";
import { short } from "../lib/format";
import { agentName } from "../lib/agentName";
import { Lines, Spark, StackedBars } from "../components/UsageCharts";

type Limits = Record<string, Record<string, string | number>>;
type Status = {
  present: boolean; revision: string; yaml: string; limits: Limits;
  month: { key: string; input_tokens: number; output_tokens: number; cost_usd: string; unpriced_calls: number };
  reserved: { input_tokens: number; output_tokens: number; cost_usd: string };
  open_reservations: Reservation[];
};
type Reservation = { id: string; run_id: string; item_id: string | null; model: string | null; cost_usd: number;
  output_tokens: number; created: number; unknown: boolean };

const PERIODS = [
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];
const DEFAULT_PERIOD = "7d";
const usd = (n: number | null | undefined) => (n == null ? "—" : n < 0.01 && n > 0 ? "< $0.01" : `$${n.toFixed(2)}`);
const secs = (ms: number | null | undefined) =>
  ms == null ? "—" : ms >= 60000 ? `${(ms / 60000).toFixed(1).replace(/\.0$/, "")} min` : `${(ms / 1000).toFixed(1).replace(/\.0$/, "")} s`;
const pct = (part: number, whole: number) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

const CHANNELS: Record<string, string> = { web: "Web chat", slack: "Slack", whatsapp: "WhatsApp", telegram: "Telegram",
  mcp: "MCP", workflow: "Workflows", api: "API" };
const ORIGINS: Record<string, string> = { chat: "Chat", schedule: "Schedules", manual: "Run by hand", event: "Events",
  mcp: "MCP", eval: "Evals" };
const PEOPLE_PAGE = 8;
const AGENTS_PAGE = 10;

/** Bucket labels: the hour a bucket starts, or the day it ends, in local time. */
function bucketLabels(d: UsageInsights) {
  return d.series.map((p) => {
    if (d.bucket < 86400) return new Date(p.start * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return new Date((p.start + d.bucket - 1) * 1000).toLocaleDateString([], { day: "numeric", month: "short" });
  });
}

export function UsageScreen({ hubs }: { hubs: Hub[] }) {
  const app = useContext(AppContext);
  const [q, setQ] = useHashQuery();
  const period = PERIODS.some((p) => p.value === q.period) ? q.period! : DEFAULT_PERIOD;
  const label = PERIODS.find((p) => p.value === period)!.label;
  const agent = hubs.some((h) => h.key === q.agent) ? q.agent! : null;
  const hubParam = agent ? `&hub=${encodeURIComponent(agent)}` : "";
  const insights = useData<UsageInsights>(`/usage?period=${period}${hubParam}`, { keepStale: true });
  const summary = useData<Summary>(`/summary?period=${period}`, { keepStale: true });
  const d = insights.data;
  const s = summary.data;
  const nameOf = (key: string) => {
    const h = hubs.find((x) => x.key === key);
    return agentName(app?.agents.list.find((a) => a.hub === key)?.name, h?.name ?? key, key);
  };
  const refresh = () => { insights.reload(); summary.reload(); };

  return (
    <div className="hz-usage">
      <div className="hz-usage-head">
        <div>
          <h2>Usage</h2>
          <p>What agents used, how they ran, and their limits.</p>
        </div>
        <div className="hz-usage-controls">
          {d && <span className="hz-usage-updated">Updated {new Date(d.generated * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>}
          <Button aria-label="Refresh" icon={<RefreshCw size={14} />} onClick={refresh} loading={insights.refreshing || summary.refreshing} />
          {hubs.length > 1 && (
            <Select aria-label="Agent" showSearch optionFilterProp="label" style={{ minWidth: 190 }} value={agent ?? ""}
              onChange={(v) => setQ({ agent: v || undefined })}
              options={[{ value: "", label: "All agents" }, ...hubs.map((h) => ({ value: h.key, label: nameOf(h.key) }))]} />
          )}
          <Segmented aria-label="Period" value={period} options={PERIODS}
            onChange={(v) => setQ({ period: v === DEFAULT_PERIOD ? undefined : String(v) })} />
        </div>
      </div>
      {!d ? (
        <p className="hz-usage-muted" role="status">{insights.error ? `Couldn’t load usage: ${insights.error}` : "Loading…"}</p>
      ) : d.recording_since == null ? (
        <div className="hz-usage-card" style={{ textAlign: "center", padding: "48px 20px" }}>
          <h3 style={{ margin: "0 0 6px" }}>No usage yet</h3>
          <p className="hz-usage-muted" style={{ margin: 0 }}>Usage appears after the first chat reply, workflow run or MCP call.</p>
        </div>
      ) : (
        <>
          {insights.error && <p className="hz-usage-notice" role="alert">Couldn’t refresh. Showing the last numbers.</p>}
          {!d.previous && d.recording_since > d.since - (d.until - d.since) && (
            <div className="hz-usage-notice" role="note">
              <Info size={16} aria-hidden style={{ flex: "none", marginTop: 2 }} />
              <span>Recording started {new Date(d.recording_since * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}, so the {label} before this period can’t be compared.</span>
            </div>
          )}
          <Totals d={d} s={s} agent={agent} label={label} />
          <OverTime d={d} label={label} scope={agent ? nameOf(agent) : "all agents"} />
          <div className="hz-usage-grid2">
            <ByChannel d={d} />
            <ByOrigin d={d} />
          </div>
          <Agents d={d} s={s} hubs={agent ? hubs.filter((h) => h.key === agent) : hubs} label={label} nameOf={nameOf} />
          <ResponseTimes d={d} scope={agent ? nameOf(agent) : "all agents"} />
          <ByModel d={d} />
          <People period={period} hubParam={hubParam} nameOf={nameOf} />
          <p className="hz-usage-muted" style={{ marginTop: 18 }}>Costs are estimates from model prices. Your provider’s bill is final.</p>
        </>
      )}
    </div>
  );
}

function Change({ now, before, label, unit = "%", absolute = false }: { now: number; before: number | null | undefined; label: string; unit?: string; absolute?: boolean }) {
  if (before == null) return null;
  const diff = absolute ? now - before : before > 0 ? Math.round(((now - before) / before) * 100) : null;
  if (diff == null) return <span className="hz-usage-change">None in the previous {label}</span>;
  if (Math.abs(diff) < (absolute ? 0.05 : 1)) return <span className="hz-usage-change">Same as the previous {label}</span>;
  const shown = absolute ? Math.abs(diff).toFixed(unit === " pts" ? 1 : 0) : Math.abs(diff);
  return <span className="hz-usage-change"><b>{diff > 0 ? "▲" : "▼"} {shown}{unit}</b> vs previous {label}</span>;
}

function Stat({ label, value, sub, tone, spark, change }: { label: string; value: string; sub?: ReactNode; tone?: "warn" | "bad"; spark?: number[]; change?: ReactNode }) {
  return (
    <li>
      <span className="hz-usage-stat-label">{label}</span>
      <span className="hz-usage-stat-row"><span className="hz-usage-stat-value">{value}</span>{spark && <Spark values={spark} />}</span>
      {sub && <span className={tone ? `hz-usage-stat-sub is-${tone}` : "hz-usage-stat-sub"}>{sub}</span>}
      {change}
    </li>
  );
}

const tokens = (t: UsageTotals) => t.input_tokens + t.output_tokens;
const rate = (t: UsageTotals) => (t.calls ? (t.errors / t.calls) * 100 : 0);

function Totals({ d, s, agent, label }: { d: UsageInsights; s?: Summary; agent: string | null; label: string }) {
  const t = d.totals, p = d.previous;
  const runsFrom = s ? (agent ? s.hubs.find((h) => h.key === agent) : s.totals) : undefined;
  const per = label;
  return (
    <ul className="hz-usage-stats is-six" aria-label="Totals">
      <Stat label="Messages" value={t.messages.toLocaleString()} sub={`Across ${t.chats.toLocaleString()} ${t.chats === 1 ? "conversation" : "conversations"}`}
        change={<Change now={t.messages} before={p?.messages} label={per} />} />
      <Stat label="People active" value={t.people.toLocaleString()}
        sub={s?.user_accounts?.accounts != null ? `Of ${s.user_accounts.accounts.toLocaleString()} accounts` : undefined}
        change={<Change now={t.people} before={p?.people} label={per} unit="" absolute />} />
      <Stat label="Tokens" value={short(tokens(t))} sub={`${pct(t.cache_read_tokens, t.input_tokens)}% read from cache`}
        spark={d.series.map((x) => x.input_tokens + x.output_tokens)} change={<Change now={tokens(t)} before={p ? tokens(p) : null} label={per} />} />
      <Stat label="Approx. cost" value={usd(t.cost_usd ?? 0)} tone={t.unpriced ? "warn" : undefined}
        sub={t.unpriced ? `${t.unpriced.toLocaleString()} ${t.unpriced === 1 ? "call" : "calls"} without a price` : "Estimated from model prices"}
        spark={d.series.map((x) => x.cost_usd)} change={<Change now={t.cost_usd ?? 0} before={p ? p.cost_usd ?? 0 : null} label={per} />} />
      <Stat label="Workflow runs" value={runsFrom?.runs == null ? (s && !s.has_workflows ? "0" : "—") : runsFrom.runs.toLocaleString()}
        tone={runsFrom?.failed ? "bad" : undefined}
        sub={runsFrom?.runs == null ? undefined : `${(runsFrom.failed ?? 0).toLocaleString()} failed${runsFrom.missed ? ` · ${runsFrom.missed} missed ${runsFrom.missed === 1 ? "slot" : "slots"}` : ""}`} />
      <Stat label="Errors" value={`${rate(t).toFixed(1)}%`}
        sub={`${t.errors.toLocaleString()} of ${t.calls.toLocaleString()} recorded calls${runsFrom?.denials ? ` · ${runsFrom.denials} tools denied` : ""}`}
        change={<Change now={rate(t)} before={p ? rate(p) : null} label={per} unit=" pts" absolute />} />
    </ul>
  );
}

function Card({ title, sub, tools, children }: { title: string; sub?: string; tools?: ReactNode; children: ReactNode }) {
  return (
    <section className="hz-usage-card" aria-label={title}>
      <div className="hz-usage-card-head">
        <div><h3>{title}</h3>{sub && <p>{sub}</p>}</div>
        {tools && <div className="hz-usage-tools">{tools}</div>}
      </div>
      {children}
    </section>
  );
}

function OverTime({ d, label, scope }: { d: UsageInsights; label: string; scope: string }) {
  const [measure, setMeasure] = useState<"tokens" | "cost">("tokens");
  const [asTable, setAsTable] = useState(false);
  const labels = bucketLabels(d);
  const missing = d.recording_since == null ? 0 : d.series.filter((p) => p.start + d.bucket <= d.recording_since!).length;
  const t = d.totals;
  const series = measure === "tokens"
    ? [
      { key: "input", label: "Input", color: "var(--hz-viz-ink)", values: d.series.map((p) => Math.max(0, p.input_tokens - p.cache_read_tokens)) },
      { key: "cache", label: "Input read from cache", color: "var(--hz-viz-soft)", values: d.series.map((p) => p.cache_read_tokens) },
      { key: "output", label: "Output", color: "var(--hz-brand)", values: d.series.map((p) => p.output_tokens) },
    ]
    : [{ key: "cost", label: "Estimated cost", color: "var(--hz-viz-ink)", values: d.series.map((p) => p.cost_usd) }];
  const fmt = measure === "tokens" ? short : (n: number) => `$${n < 10 ? n.toFixed(1).replace(/\.0$/, "") : Math.round(n)}`;
  return (
    <Card title={measure === "tokens" ? "Tokens over time" : "Cost over time"}
      sub={`${d.bucket < 86400 ? "Per hour" : "Per day"}, ${scope}. Hover for details.`}
      tools={<>
        <Segmented size="small" aria-label="Measure" value={measure} onChange={(v) => setMeasure(v as "tokens" | "cost")}
          options={[{ value: "tokens", label: "Tokens" }, { value: "cost", label: "Cost" }]} />
        <Segmented size="small" aria-label="View" value={asTable ? "table" : "chart"} onChange={(v) => setAsTable(v === "table")}
          options={[{ value: "chart", label: "Chart" }, { value: "table", label: "Table" }]} />
        <span className="hz-usage-total"><b>{measure === "tokens" ? short(tokens(t)) : usd(t.cost_usd ?? 0)}</b><span>Total in {label}</span></span>
      </>}>
      {asTable ? (
        <div className="hz-chart-table">
          <Table size="small" pagination={false} rowKey="start"
            dataSource={d.series.map((p, i) => ({ ...p, label: labels[i], missing: i < missing })).reverse()}
            columns={[
              { title: d.bucket < 86400 ? "Hour" : "Day", dataIndex: "label" },
              ...(measure === "tokens" ? [
                { title: "Input", align: "right" as const, render: (_: unknown, r: { missing: boolean; input_tokens: number; cache_read_tokens: number }) => r.missing ? "Not recorded" : short(Math.max(0, r.input_tokens - r.cache_read_tokens)) },
                { title: "From cache", align: "right" as const, render: (_: unknown, r: { missing: boolean; cache_read_tokens: number }) => r.missing ? "" : short(r.cache_read_tokens) },
                { title: "Output", align: "right" as const, render: (_: unknown, r: { missing: boolean; output_tokens: number }) => r.missing ? "" : short(r.output_tokens) },
              ] : [
                { title: "Cost", align: "right" as const, render: (_: unknown, r: { missing: boolean; cost_usd: number }) => r.missing ? "Not recorded" : usd(r.cost_usd) },
              ]),
            ]} />
        </div>
      ) : (
        <StackedBars labels={labels} series={series} format={fmt} missingBefore={missing}
          label={`${measure} per ${d.bucket < 86400 ? "hour" : "day"}`}
          tooltip={(i) => (
            <>
              <b>{labels[i]}</b>
              {i < missing ? <div><span>Not recorded</span></div> : measure === "tokens" ? (
                <>
                  {[...series].reverse().map((ser) => (
                    <div key={ser.key}><span><i style={{ background: ser.color }} />{ser.label}</span><span>{short(ser.values[i])}</span></div>
                  ))}
                  <div style={{ borderTop: "1px solid var(--hz-line)", marginTop: 4, paddingTop: 4 }}><span>Total</span><span>{short(d.series[i].input_tokens + d.series[i].output_tokens)}</span></div>
                </>
              ) : <div><span>Estimated cost</span><span>{usd(d.series[i].cost_usd)}</span></div>}
            </>
          )} />
      )}
      <div className="hz-usage-legend">
        {measure === "tokens" && series.map((ser) => <span key={ser.key}><i style={{ background: ser.color }} />{ser.label}</span>)}
        {missing > 0 && <span><i className="is-hatch" />Before recording started</span>}
      </div>
    </Card>
  );
}

function Bars({ rows, head }: { rows: { name: string; share: number; value: string; sub: string }[]; head: [string, string, string, string] }) {
  const max = Math.max(0, ...rows.map((r) => r.share)) || 1;
  if (!rows.length) return <p className="hz-usage-muted">Nothing in this period.</p>;
  return (
    <div className="hz-usage-bars2">
      <span className="is-th">{head[0]}</span><span className="is-th">{head[1]}</span><span className="is-th is-r">{head[2]}</span><span className="is-th is-r is-c4">{head[3]}</span>
      {rows.map((r) => (
        <span key={r.name} style={{ display: "contents" }}>
          <span>{r.name}</span>
          <span><span className="hz-usage-hbar" style={{ width: `${(r.share / max) * 100}%` }} /></span>
          <span className="is-r">{r.value}</span>
          <span className="is-r is-sub is-c4">{r.sub}</span>
        </span>
      ))}
    </div>
  );
}

function ByChannel({ d }: { d: UsageInsights }) {
  const rows = d.by_surface.map((r) => ({ name: CHANNELS[r.surface] ?? r.surface, share: r.cost_usd || r.calls / 1e6,
    value: usd(r.cost_usd), sub: r.calls.toLocaleString() }));
  return (
    <Card title="By channel" sub="Where people and workflows reached your agents.">
      <Bars rows={rows} head={["Channel", "Cost", "Cost", "Calls"]} />
    </Card>
  );
}

function ByOrigin({ d }: { d: UsageInsights }) {
  const total = d.by_trigger.reduce((n, r) => n + r.calls, 0);
  const rows = [...d.by_trigger].sort((a, b) => (a.trigger == null ? 1 : b.trigger == null ? -1 : b.calls - a.calls))
    .map((r) => ({ name: r.trigger == null ? "Earlier, not tracked" : ORIGINS[r.trigger] ?? r.trigger,
      share: r.calls, value: `${pct(r.calls, total)}%`, sub: usd(r.cost_usd) }));
  return (
    <Card title="How it was used" sub="Model calls by what started them.">
      <Bars rows={rows} head={["Started by", "Share of calls", "Calls", "Cost"]} />
    </Card>
  );
}

type AgentRow = UsageHub & { name: string; last: number | null };

function Agents({ d, s, hubs, label, nameOf }: { d: UsageInsights; s?: Summary; hubs: Hub[]; label: string; nameOf: (k: string) => string }) {
  const app = useContext(AppContext);
  const [find, setFind] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  // Saving limits or releasing a hold reloads both the row's bar and the panel.
  const [limitsRev, setLimitsRev] = useState(0);
  const empty = { calls: 0, messages: 0, chats: 0, people: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
    cache_write_tokens: 0, reasoning_tokens: null, cost_usd: 0, unpriced: 0, errors: 0, cancelled: 0, p50_ms: null, p95_ms: null };
  const rows: AgentRow[] = hubs.map((h) => ({ ...empty, ...(d.by_hub.find((r) => r.key === h.key) ?? {}), key: h.key,
    name: nameOf(h.key), last: s?.hubs.find((x) => x.key === h.key)?.last_activity ?? null }));
  const needle = find.trim().toLowerCase();
  const shown = rows.filter((r) => !needle || r.name.toLowerCase().includes(needle) || r.key.toLowerCase().includes(needle));
  const num = (k: keyof AgentRow) => (a: AgentRow, b: AgentRow) => Number(a[k] ?? -1) - Number(b[k] ?? -1);
  const columns: ColumnsType<AgentRow> = [
    { title: "Agent", key: "name", sorter: (a, b) => a.name.localeCompare(b.name), render: (_, r) => (
      <span className="hz-usage-who">
        <AgentAvatar name={r.name} src={app?.agents.list.find((a) => a.hub === r.key)?.avatar_url} size={28} />
        <span style={{ minWidth: 0 }}><b>{r.name}</b><small>{r.last ? `Last used ${formatRelative(r.last)}` : "Not used yet"}</small></span>
      </span>) },
    { title: "Messages", dataIndex: "messages", align: "right", sorter: num("messages"), responsive: ["md"], render: (v: number) => v.toLocaleString() },
    { title: "People", dataIndex: "people", align: "right", responsive: ["lg"], render: (v: number) => v.toLocaleString() },
    { title: "Tokens", key: "tokens", align: "right", responsive: ["lg"], sorter: (a, b) => tokens(a) - tokens(b), render: (_, r) => short(tokens(r)) },
    { title: "Cost", dataIndex: "cost_usd", align: "right", sorter: num("cost_usd"), defaultSortOrder: "descend", render: (v: number | null) => usd(v ?? 0) },
    { title: "Errors", dataIndex: "errors", align: "right", sorter: num("errors"), responsive: ["md"],
      render: (v: number, r) => (v ? <Tag color={r.calls && v / r.calls > 0.02 ? "warning" : undefined}>{v}</Tag> : "0") },
    { title: "Response p50", dataIndex: "p50_ms", align: "right", sorter: num("p50_ms"), responsive: ["lg"], render: (v: number | null) => secs(v) },
    { title: "Monthly limit", key: "limit", responsive: ["md"], render: (_, r) => <LimitCell hub={r.key} name={r.name} rev={limitsRev} /> },
  ];
  return (
    <Card title={`By agent, last ${label}`} sub="Limits count this calendar month. Each model call checks its agent's limit first. Open a row to set limits."
      tools={hubs.length > 1 ? (
        <label className="hz-set-find hz-agents-find" style={{ margin: 0 }}>
          <Search size={14} aria-hidden className="text-mute" />
          <input aria-label="Find an agent" placeholder="Find an agent" value={find} onChange={(e) => setFind(e.target.value)} />
        </label>
      ) : undefined}>
      <Table<AgentRow> className="hz-usage-table" size="middle" rowKey="key" dataSource={shown} columns={columns}
        scroll={{ x: 560 }} rowClassName="clickable"
        pagination={{ pageSize: AGENTS_PAGE, hideOnSinglePage: true, showSizeChanger: false, showTotal: (n, [a, b]) => `${a} to ${b} of ${n}` }}
        locale={{ emptyText: needle ? `No agent matches “${find}”.` : "No agents." }}
        expandable={{
          expandedRowKeys: open ? [open] : [],
          onExpand: (expanded, r) => setOpen(expanded ? r.key : null),
          expandRowByClick: true,
          expandedRowRender: (r) => <LimitsPanel hub={r.key} rev={limitsRev} changed={() => setLimitsRev((n) => n + 1)} />,
        }} />
    </Card>
  );
}

/** Month limits worth a bar: output tokens, input tokens and dollars. */
const BARS: { key: string; label: string; used: (s: Status) => number; held: (s: Status) => number; format: (n: number) => string }[] = [
  { key: "max_output_tokens", label: "output tokens", used: (s) => s.month.output_tokens, held: (s) => s.reserved.output_tokens, format: (n) => short(n) },
  { key: "max_input_tokens", label: "input tokens", used: (s) => s.month.input_tokens, held: (s) => s.reserved.input_tokens, format: (n) => short(n) },
  { key: "max_cost_usd", label: "", used: (s) => Number(s.month.cost_usd), held: (s) => Number(s.reserved.cost_usd), format: (n) => `$${n.toFixed(2)}` },
];

function LimitCell({ hub, name, rev }: { hub: string; name: string; rev: number }) {
  const data = useData<Status>(`/hubs/${encodeURIComponent(hub)}/budgets${rev ? `?v=${rev}` : ""}`, { keepStale: true });
  const s = data.data;
  if (!s) return <span className="hz-usage-muted">{data.error ? "Couldn’t load" : "Loading…"}</span>;
  const month = s.limits?.month ?? {};
  const bar = BARS.find((b) => month[b.key] != null);
  if (!bar) return <span className="hz-usage-muted">{usd(Number(s.month.cost_usd))} this month · No limit</span>;
  const cap = Number(month[bar.key]);
  const held = bar.held(s);
  const total = bar.used(s) + held;
  const share = cap > 0 ? Math.min(100, (total / cap) * 100) : 0;
  const tone = share >= 100 ? "is-bad" : share >= 80 ? "is-warn" : "";
  return (
    <span className="hz-usage-limit hz-bar-wrap">
      <span className="hz-bar-text">
        <span>{bar.format(total)} of {bar.format(cap)} {bar.label}{bar.label ? " " : ""}this month{held > 0 ? `, ${bar.format(held)} held` : ""}</span>
        <span className={tone ? `hz-bar-pct ${tone}` : "hz-bar-pct"}>{share >= 100 ? "Limit reached" : `${Math.round(share)}%`}</span>
      </span>
      <span className={`hz-meter ${tone}`} role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={total}
        aria-label={`${name}: ${bar.format(total)} of ${bar.format(cap)} ${bar.label}${bar.label ? " " : ""}this month`}>
        <span style={{ width: `${share}%` }} />
      </span>
    </span>
  );
}

function LimitsPanel({ hub, rev, changed }: { hub: string; rev: number; changed: () => void }) {
  const base = `/hubs/${encodeURIComponent(hub)}/budgets`;
  const data = useData<Status>(`${base}${rev ? `?v=${rev}` : ""}`, { keepStale: true });
  const s = data.data;
  if (!s) return <p className="hz-usage-muted">{data.error ? "Couldn’t load limits" : "Loading…"}</p>;
  return (
    <>
      <LimitsEditor key={s.revision} base={base} status={s} saved={changed} />
      {s.open_reservations.length > 0 && <Reservations base={base} items={s.open_reservations} released={changed} />}
    </>
  );
}

function ResponseTimes({ d, scope }: { d: UsageInsights; scope: string }) {
  const labels = bucketLabels(d);
  const missing = d.recording_since == null ? 0 : d.series.filter((p) => p.start + d.bucket <= d.recording_since!).length;
  const fmt = (ms: number) => (ms >= 60000 ? `${Math.round(ms / 60000)}m` : `${Math.round(ms / 1000)}s`);
  return (
    <Card title="Response time" sub={`How long a reply or a workflow model call took, ${scope}. Failed calls are left out.`}>
      <div className="hz-usage-rt">
        <div><b>{secs(d.latency.first_token_p50_ms)}</b><span>First words in web chat, p50</span></div>
        <div><b>{secs(d.latency.p50_ms)}</b><span>Full reply or call, p50</span></div>
        <div><b>{secs(d.latency.p95_ms)}</b><span>Slowest 5%, p95</span></div>
      </div>
      <Lines labels={labels} format={fmt} label="p50 and p95 response time"
        lines={[
          { key: "p95", label: "p95", color: "var(--hz-brand)", values: d.series.map((p, i) => (i < missing ? null : p.p95_ms)) },
          { key: "p50", label: "p50", color: "var(--hz-viz-ink)", values: d.series.map((p, i) => (i < missing ? null : p.p50_ms)) },
        ]}
        tooltip={(i) => (
          <>
            <b>{labels[i]}</b>
            {d.series[i].p50_ms == null ? <div><span>No calls</span></div> : (
              <>
                <div><span><i style={{ background: "var(--hz-brand)" }} />p95</span><span>{secs(d.series[i].p95_ms)}</span></div>
                <div><span><i style={{ background: "var(--hz-viz-ink)" }} />p50</span><span>{secs(d.series[i].p50_ms)}</span></div>
              </>
            )}
          </>
        )} />
      <div className="hz-usage-legend"><span><i style={{ background: "var(--hz-viz-ink)", height: 2 }} />p50, half of calls finish within this</span><span><i style={{ background: "var(--hz-brand)", height: 2 }} />p95</span></div>
    </Card>
  );
}

function ByModel({ d }: { d: UsageInsights }) {
  const [all, setAll] = useState(false);
  const rows = all ? d.by_model : d.by_model.slice(0, 6);
  const columns: ColumnsType<UsageModel> = [
    { title: "Model", dataIndex: "model", render: (v: string | null) => <span className="hz-usage-model">{v ?? "Unknown"}</span> },
    { title: "Calls", dataIndex: "calls", align: "right", render: (v: number) => v.toLocaleString() },
    { title: "Input", dataIndex: "input_tokens", align: "right", responsive: ["md"], render: (v: number) => short(v) },
    { title: "From cache", key: "cache", align: "right", responsive: ["md"], render: (_, r) => `${pct(r.cache_read_tokens, r.input_tokens)}%` },
    { title: "Output", dataIndex: "output_tokens", align: "right", responsive: ["md"], render: (v: number) => short(v) },
    { title: "Reasoning", dataIndex: "reasoning_tokens", align: "right", responsive: ["lg"],
      render: (v: number | null) => (v == null ? <Tooltip title="Not reported. Claude counts thinking inside output tokens."><span className="hz-usage-muted">In output</span></Tooltip> : short(v)) },
    { title: "Cost", dataIndex: "cost_usd", align: "right", render: (v: number | null, r) => (v == null || (r.unpriced && !v)
      ? <Tooltip title="No price is known for this model. Cost totals leave it out."><Tag color="warning">No price</Tag></Tooltip> : usd(v)) },
  ];
  return (
    <Card title="By model" sub="Grouped by the model that answered. A turn's background and sub-agent calls count with it.">
      <Table<UsageModel> className="hz-usage-table" size="middle" rowKey={(r) => r.model ?? "unknown"} dataSource={rows} columns={columns}
        pagination={false} scroll={{ x: 520 }} locale={{ emptyText: "No model calls in this period." }} />
      {d.by_model.length > 6 && (
        <p style={{ margin: "10px 0 0" }}><Button type="link" size="small" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${d.by_model.length}`}</Button></p>
      )}
    </Card>
  );
}

function useDebounced<T>(value: T, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function People({ period, hubParam, nameOf }: { period: string; hubParam: string; nameOf: (k: string) => string }) {
  const [find, setFind] = useState("");
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<{ by: string; order: "asc" | "desc" }>({ by: "cost", order: "desc" });
  const q = useDebounced(find.trim());
  useEffect(() => setPage(0), [q, period, hubParam]);
  const path = `/usage/people?period=${period}${hubParam}&q=${encodeURIComponent(q)}&sort=${sort.by}&order=${sort.order}&page=${page}&size=${PEOPLE_PAGE}`;
  const data = useData<UsagePeople>(path, { keepStale: true });
  const shown = data.data;
  const order = (by: string) => (sort.by === by ? (sort.order === "asc" ? "ascend" : "descend") : null);
  const columns = useMemo<ColumnsType<UsagePerson>>(() => [
    { title: "Person or schedule", key: "name", sorter: true, sortOrder: order("name"), render: (_, r) => (
      <span className="hz-usage-who">
        <span style={{ minWidth: 0 }}><b>{r.subject ?? "Evals and system calls"}</b><small>{r.kind === "person" ? "Person" : r.kind === "schedule" ? "Schedule" : r.kind === "workflow" ? "Workflow" : "No person"}</small></span>
      </span>) },
    { title: "Agents used", key: "agents", responsive: ["lg"], render: (_, r) => r.agents.map(nameOf).join(", ") },
    { title: "Messages", key: "messages", align: "right", sorter: true, sortOrder: order("messages"), responsive: ["md"],
      render: (_, r) => (r.kind === "person" ? r.messages.toLocaleString() : <span className="hz-usage-muted">Not chat</span>) },
    { title: "Tokens", key: "tokens", align: "right", sorter: true, sortOrder: order("tokens"), responsive: ["md"], render: (_, r) => short(r.tokens) },
    { title: "Cost", key: "cost", align: "right", sorter: true, sortOrder: order("cost"), render: (_, r) => usd(r.cost_usd) },
    { title: "Last active", key: "last", sorter: true, sortOrder: order("last"), responsive: ["md"], render: (_, r) => formatRelative(r.last) },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [sort, nameOf]);
  return (
    <Card title="By person" sub="Who and which schedules used the agents. Visible to admins. A delegated admin sees their own agents only."
      tools={
        <label className="hz-set-find hz-agents-find" style={{ margin: 0 }}>
          <Search size={14} aria-hidden className="text-mute" />
          <input aria-label="Find a person" placeholder="Find a person" value={find} onChange={(e) => setFind(e.target.value)} />
        </label>
      }>
      {data.error ? <p className="hz-usage-muted" role="alert">Couldn’t load people: {data.error}</p> : (
        <Table<UsagePerson> className="hz-usage-table" size="middle" rowKey={(r) => r.subject ?? "anonymous"}
          dataSource={shown?.rows ?? []} columns={columns} loading={!shown} scroll={{ x: 520 }}
          locale={{ emptyText: q ? `No one matches “${q}”.` : "No one used the agents in this period." }}
          onChange={(_p, _f, sorter, extra) => {
            if (extra.action !== "sort") return;   // pages go through pagination.onChange
            const one = Array.isArray(sorter) ? sorter[0] : sorter;
            if (!one?.order || !one.columnKey) setSort({ by: "cost", order: "desc" });
            else setSort({ by: String(one.columnKey), order: one.order === "ascend" ? "asc" : "desc" });
            setPage(0);
          }}
          pagination={{ current: page + 1, pageSize: PEOPLE_PAGE, total: shown?.total ?? 0, hideOnSinglePage: true,
            showSizeChanger: false, onChange: (p) => setPage(p - 1), showTotal: (n, [a, b]) => `${a} to ${b} of ${n}` }} />
      )}
    </Card>
  );
}

const FIELDS: { scope: string; key: string; label: string; help: string }[] = [
  { scope: "month", key: "max_output_tokens", label: "Output tokens per month", help: "Stops the agent until next month." },
  { scope: "month", key: "max_cost_usd", label: "Dollars per month", help: "Estimated. Codex can't be capped in dollars." },
  { scope: "run", key: "max_turns", label: "Turns per reply or run", help: "Stops a runaway reply or run." },
  { scope: "run", key: "max_output_tokens", label: "Output tokens per reply or run", help: "" },
];

function toYaml(limits: Limits): string {
  const scopes = Object.entries(limits).filter(([, caps]) => Object.keys(caps).length > 0);
  if (!scopes.length) return "version: 1\nlimits: {}\n";
  return `version: 1\nlimits:\n${scopes.map(([scope, caps]) => `  ${scope}:\n${Object.entries(caps).map(([k, v]) => `    ${k}: ${v}`).join("\n")}`).join("\n")}\n`;
}

const errorMessage = (e: unknown) => {
  if (e instanceof ApiError) {
    const detail = e.data?.detail as { message?: string } | string | undefined;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : String(e);
};

/** A few fields for the common limits. Other limits in the file are kept as
 *  they are; Edit as file shows the whole budgets.yaml. */
function LimitsEditor({ base, status, saved }: { base: string; status: Status; saved: () => void }) {
  const { message } = App.useApp();
  // Remounted for each saved revision (its key), so the fields start from it.
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [`${f.scope}.${f.key}`, String(status.limits?.[f.scope]?.[f.key] ?? "")])));
  const [asFile, setAsFile] = useState(false);
  const [yaml, setYaml] = useState(status.yaml);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const initial = Object.fromEntries(FIELDS.map((f) => [`${f.scope}.${f.key}`, String(status.limits?.[f.scope]?.[f.key] ?? "")]));
  const fieldsChanged = FIELDS.some((f) => (values[`${f.scope}.${f.key}`] ?? "").trim() !== initial[`${f.scope}.${f.key}`]);
  const fileChanged = yaml !== status.yaml;
  const commented = /^\s*#/m.test(status.yaml);

  /** The file the fields describe: every other limit in the file kept. */
  const fromFields = (): string | null => {
    const next: Limits = JSON.parse(JSON.stringify(status.limits ?? {}));
    for (const f of FIELDS) {
      const raw = (values[`${f.scope}.${f.key}`] ?? "").trim();
      next[f.scope] = next[f.scope] ?? {};
      if (raw === "") delete next[f.scope][f.key];
      else if (!/^\d+(\.\d+)?$/.test(raw)) {
        setProblem(`${f.label} must be a number.`);
        return null;
      } else next[f.scope][f.key] = Number(raw);
    }
    return toYaml(next);
  };

  // Switching keeps what was typed: fields become the file; file edits are
  // only dropped after a confirm, since the fields can't show every limit.
  const switchMode = () => {
    setProblem(null);
    if (!asFile) {
      if (fieldsChanged) {
        const next = fromFields();
        if (next === null) return;
        setYaml(next);
      }
      setAsFile(true);
      return;
    }
    if (fileChanged && !window.confirm("Switch to fields? Your edits to the file aren't saved and will be lost.")) return;
    setYaml(status.yaml);
    setAsFile(false);
  };

  const save = async () => {
    let body = yaml;
    if (!asFile) {
      const next = fromFields();
      if (next === null) return;
      body = next;
    }
    setBusy(true);
    try {
      await request(base, { yaml: body, expected_revision: status.revision }, undefined, "PUT");
      setProblem(null);
      message.success("Limits saved.");
      saved();
    } catch (e) {
      setProblem(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="hz-limits">
      {asFile ? (
        <textarea className="hz-input hz-limits-file" rows={10} value={yaml} onChange={(e) => setYaml(e.target.value)}
          spellCheck={false} aria-label="budgets.yaml" />
      ) : (
        <div className="hz-limits-grid">
          {FIELDS.map((f) => (
            <label key={`${f.scope}.${f.key}`} className="hz-limits-field">
              <span className="hz-label">{f.label}</span>
              <input className="hz-input" inputMode="decimal" placeholder="No limit" value={values[`${f.scope}.${f.key}`] ?? ""}
                onChange={(e) => setValues({ ...values, [`${f.scope}.${f.key}`]: e.target.value })} />
              {f.help && <span className="hz-limits-help">{f.help}</span>}
            </label>
          ))}
        </div>
      )}
      {problem && <p className="hz-limits-problem" role="alert">{problem}</p>}
      <div className="hz-limits-foot">
        <button type="button" className="hz-btn hz-btn-primary hz-btn-sm" disabled={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save limits"}
        </button>
        <button type="button" className="hz-link-btn" onClick={switchMode}>
          {asFile ? "Edit as fields" : "Edit as file"}
        </button>
        <span className="hz-usage-muted">Empty means no limit.</span>
      </div>
      {!asFile && commented && (
        <p className="hz-limits-help" style={{ marginTop: 10 }}>Saving drops the file's comments. Edit as file keeps them.</p>
      )}
    </div>
  );
}

/** Calls that hold part of the limit while they run. One whose call never
 *  reported keeps holding it until someone releases it, with a reason. */
function Reservations({ base, items, released }: { base: string; items: Reservation[]; released: () => void }) {
  const { message } = App.useApp();
  const [releasing, setReleasing] = useState<Reservation | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const release = async () => {
    if (!releasing) return;
    setBusy(true);
    try {
      await request(`${base}/reservations/${encodeURIComponent(releasing.id)}/release`, { reason }, undefined, "POST");
      message.success("Released.");
      setReleasing(null);
      setReason("");
      released();
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="hz-limits hz-held">
      <h4>Held by running calls</h4>
      <ul>
        {items.map((r) => (
          <li key={r.id}>
            <span className="mono">{r.run_id.slice(0, 24)}</span>
            <span className="hz-usage-muted">since {new Date(r.created * 1000).toLocaleString()}</span>
            {r.unknown ? (
              <>
                <span className="hz-status is-warn"><i aria-hidden />Never reported</span>
                <button type="button" className="hz-btn hz-btn-secondary hz-btn-sm" onClick={() => setReleasing(r)}>Release</button>
              </>
            ) : (
              <span className="hz-status is-info"><i aria-hidden />Running</span>
            )}
          </li>
        ))}
      </ul>
      <Modal open={releasing !== null} title="Release this reservation?" okText="Release"
        okButtonProps={{ disabled: !reason.trim(), loading: busy }} onOk={() => void release()} onCancel={() => setReleasing(null)}>
        <p>Its call never reported usage, so the hold still counts. Check the provider's charge first.</p>
        <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="What you checked. This is recorded." aria-label="Reason" />
      </Modal>
    </div>
  );
}
