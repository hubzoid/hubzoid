import { useEffect, useMemo, useRef, useState } from "react";
import { useUnderTabs } from "../lib/underTabs";
import {
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { ChevronRight, Download, RefreshCw } from "lucide-react";
import { query, type AuditRow, type Hub, type Person } from "../api";
import { useData } from "../hooks/useData";
import { useCatalogs } from "../hooks/useCatalogs";
import { useHashQuery } from "../hooks/useRoute";
import { LoadState, SentenceText, When } from "../components/common";
import {
  ORG,
  describeAccessChange,
  describeAuthEvent,
  describeDecision,
  describeToolCall,
  formatTime,
  personName,
  relativeTime,
  type ActivityContext,
} from "../lib/format";

const { Text, Title, Paragraph } = Typography;
const PAGE = 20;   // one page of rows everywhere (founder review, HUB-140)

type Feed = "changes" | "decisions" | "calls" | "signins";
const FEEDS: Feed[] = ["changes", "decisions", "calls", "signins"];
// What each feed exports (GET /audit/export ?kind=).
const EXPORT_KINDS: Record<Feed, string> = {
  changes: "admin,auth", decisions: "access", calls: "tool", signins: "auth",
};
const RANGES: Record<string, number> = { "1d": 86400, "7d": 604800, "30d": 2592000 };

export function ActivityScreen({ hubs, hub }: { hubs: Hub[]; hub?: Hub }) {
  const underTabs = useUnderTabs();
  // Every filter + page lives in the URL (shareable, restored on refresh/Back).
  const [q, setQ] = useHashQuery();
  const feed: Feed = FEEDS.includes(q.feed as Feed) ? (q.feed as Feed) : "changes";
  const audited = feed === "calls" || feed === "signins";
  const agent = hub ? hub.key : q.agent || "";
  const person = (q.person || "").toLowerCase();
  const actor = (q.actor || "").toLowerCase();
  const action = q.action || "";
  const tool = q.tool || "";
  const outcome = q.outcome || "";
  const result = q.result || "";
  const channel = q.channel || "";
  const range = q.range || ""; // relative token, kept for the Select display
  const sinceAbs = q.since || ""; // absolute ISO boundaries — the actual window
  const untilAbs = q.until || "";
  const page = Math.max(1, Number(q.page) || 1);
  const filtersActive = !!(
    person || actor || action || tool || outcome || result || channel ||
    range || sinceAbs || untilAbs || (!hub && agent)
  );
  // One debounce timer PER text input so they never cancel each other's pending
  // change (tool and channel previously shared a timer).
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  // A key that only changes when filters are RESET — the debounced text inputs are
  // keyed by it, not by their (changing) value, so committing a debounced value does
  // not remount the input and steal focus; Reset does remount them to clear.
  const [resetToken, setResetToken] = useState(0);
  const debounce = (field: string, value: string | undefined) => {
    clearTimeout(timers.current[field]);
    timers.current[field] = setTimeout(() => setQ({ [field]: value, page: undefined }), 300);
  };
  // Cancel any pending debounce on unmount, so a keystroke's timer cannot fire after
  // navigation and rewrite the NEXT screen's URL query.
  useEffect(
    () => () => {
      for (const t of Object.values(timers.current)) clearTimeout(t);
    },
    [],
  );

  // Activity is always a FIXED investigation (there is no live mode): a chosen range
  // is frozen to absolute since/until in the URL, so reload and shared links reopen
  // the exact same window regardless of the clock, and the fixed upper bound stops
  // new events from shifting later pages. A legacy range-only link is frozen on open.
  useEffect(() => {
    if (RANGES[range] && !sinceAbs && !untilAbs) {
      const now = Date.now();
      setQ({
        since: new Date(now - RANGES[range] * 1000).toISOString(),
        until: new Date(now).toISOString(),
      });
    }
  }, [range, sinceAbs, untilAbs, setQ]);
  const chooseRange = (v?: string) => {
    if (!v) return setQ({ range: undefined, since: undefined, until: undefined, page: undefined });
    const now = Date.now();
    setQ({
      range: v,
      since: new Date(now - RANGES[v] * 1000).toISOString(),
      until: new Date(now).toISOString(),
      page: undefined,
    });
  };
  const sinceMs = sinceAbs ? Date.parse(sinceAbs) : 0;
  const untilMs = untilAbs ? Date.parse(untilAbs) : 0;
  const commonParams = {
    hub: agent,
    user: person,
    offset: (page - 1) * PAGE,
    limit: PAGE,
  };
  const params =
    feed === "changes"
      ? {
          ...commonParams,
          actor,
          action,
          since: sinceMs ? Math.floor(sinceMs / 1000) : undefined,
          until: untilMs ? Math.floor(untilMs / 1000) : undefined,
        }
      : {
          ...commonParams,
          kind: feed === "calls" ? "tool" : feed === "signins" ? "auth" : undefined,
          outcome: outcome || undefined,
          result: feed === "calls" ? result || undefined : undefined,
          tool: feed === "signins" ? undefined : tool,
          surface: feed === "signins" ? undefined : channel,
          since: sinceMs ? new Date(sinceMs).toISOString() : undefined,
          until: untilMs ? new Date(untilMs).toISOString() : undefined,
        };
  const data = useData<{ rows: AuditRow[]; stored?: boolean }>(
    (feed === "changes" ? "/access-changes" : "/audit") + query(params),
  );
  // Everything of this feed's kind in the chosen agent and time, as JSON lines.
  const exportHref =
    "/portal/api/audit/export" +
    query({
      hub: agent || undefined,
      kind: EXPORT_KINDS[feed],
      since: sinceMs ? new Date(sinceMs).toISOString() : undefined,
      until: untilMs ? new Date(untilMs).toISOString() : undefined,
    });
  const resetFilters = () => {
    setResetToken((t) => t + 1); // remount the text inputs so their defaultValue clears
    setQ({
      person: undefined, actor: undefined, action: undefined, tool: undefined,
      outcome: undefined, result: undefined, channel: undefined, range: undefined,
      since: undefined, until: undefined, page: undefined,
    });
  };
  // Display names for identities that appear in the feed (best effort).
  const people = useData<{ people: Person[] }>("/people" + query({ limit: 200 }));
  const names = useMemo(() => {
    const map: Record<string, string> = {};
    for (const p of people.data?.people ?? []) map[p.subject] = personName(p.subject, p.display);
    return map;
  }, [people.data]);
  const catalogs = useCatalogs(hub ? [hub.key] : hubs.map((h) => h.key));
  const ctx: ActivityContext = useMemo(
    () => ({
      agentName: (key) =>
        key === ORG ? "the organization" : (hubs.find((h) => h.key === key)?.name ?? key),
      catalog: (key) => catalogs[key],
      // Fall back to personName (not the raw subject) so the "*" wildcard reads
      // "Everyone signed in" and unknown subjects still render sensibly.
      people: (subject) => names[subject] ?? personName(subject),
    }),
    [hubs, catalogs, names],
  );

  const rows = data.data?.rows ?? [];
  const describe = (r: AuditRow) => describeRow(feed, r, ctx);
  const [detail, setDetail] = useState<AuditRow | null>(null);

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          {!underTabs && <Title level={hub ? 2 : 1} style={{ fontSize: 28 }}>{hub ? `Activity in ${hub.name}` : "Activity across your agents"}</Title>}
          <Paragraph type="secondary">
            {feed === "changes"
              ? "Who changed access, for whom, and to what."
              : feed === "decisions"
                ? "Every restricted tool call, and whether it was allowed."
                : feed === "calls"
                  ? "Every tool call: who, which tool, allowed or refused, and how it ended."
                  : "Failed sign-ins, lockouts and personal API key use."}
          </Paragraph>
        </div>
        <Space>
          <Button icon={<Download size={16} />} href={exportHref} download="hubzoid-audit.jsonl"
            title="Every record of this kind in the chosen agent and time, as JSON lines">
            Export
          </Button>
          <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
            Refresh
          </Button>
        </Space>
      </div>

      <div className="toolbar">
        <Segmented<Feed>
          value={feed}
          onChange={(v) => setQ({ feed: v, page: undefined })}
          options={[
            { label: "Access changes", value: "changes" },
            { label: "Tool decisions", value: "decisions" },
            { label: "Tool calls", value: "calls" },
            { label: "Sign-in events", value: "signins" },
          ]}
        />
        {!hub && (
          <Select
            aria-label="Agent"
            value={agent || undefined}
            onChange={(v) => setQ({ agent: v, page: undefined })}
            placeholder="All agents"
            allowClear
            style={{ minWidth: 180 }}
            options={hubs.map((h) => ({ value: h.key, label: h.name }))}
          />
        )}
        <Select
          aria-label="Time range"
          value={range || undefined}
          onChange={chooseRange}
          placeholder="Any time"
          allowClear
          style={{ minWidth: 140 }}
          options={[
            { value: "1d", label: "Last 24 hours" },
            { value: "7d", label: "Last 7 days" },
            { value: "30d", label: "Last 30 days" },
          ]}
        />
        <Input
          key={`person:${resetToken}`}
          aria-label={feed === "changes" ? "Filter by affected person" : "Filter by person"}
          placeholder={feed === "changes" ? "Affected person’s email" : "Person’s email"}
          defaultValue={person}
          onChange={(e) => debounce("person", e.target.value.trim().toLowerCase() || undefined)}
          allowClear
          style={{ maxWidth: 220 }}
        />
        {feed === "changes" && (
          <>
            <Input
              key={`actor:${resetToken}`}
              aria-label="Filter by who changed it"
              placeholder="Changed by (email)"
              defaultValue={actor}
              onChange={(e) => debounce("actor", e.target.value.trim().toLowerCase() || undefined)}
              allowClear
              style={{ maxWidth: 200 }}
            />
            <Select
              aria-label="Action"
              value={action || undefined}
              onChange={(v) => setQ({ action: v, page: undefined })}
              placeholder="Any action"
              allowClear
              style={{ minWidth: 150 }}
              options={[
                { value: "grant", label: "Granted" },
                { value: "revoke", label: "Removed" },
                { value: "suspend", label: "Blocked" },
                { value: "reactivate", label: "Reactivated" },
                { value: "run_start", label: "Started a workflow" },
                { value: "workflow_pause", label: "Paused a workflow" },
                { value: "workflow_resume", label: "Resumed a workflow" },
                { value: "run_cancel", label: "Cancelled a run" },
              ]}
            />
          </>
        )}
        {(feed === "decisions" || feed === "calls") && (
          <>
            <Select
              aria-label="Outcome"
              value={outcome || undefined}
              onChange={(v) => setQ({ outcome: v, page: undefined })}
              placeholder="Any outcome"
              allowClear
              style={{ minWidth: 140 }}
              options={[
                { value: "allow", label: "Allowed" },
                { value: "deny", label: "Denied" },
              ]}
            />
            {feed === "calls" && (
              <Select
                aria-label="Result"
                value={result || undefined}
                onChange={(v) => setQ({ result: v, page: undefined })}
                placeholder="Any result"
                allowClear
                style={{ minWidth: 130 }}
                options={[
                  { value: "ok", label: "Finished" },
                  { value: "error", label: "Failed" },
                ]}
              />
            )}
            <Input
              key={`tool:${resetToken}`}
              aria-label="Filter by tool"
              placeholder="Tool"
              defaultValue={tool}
              onChange={(e) => debounce("tool", e.target.value.trim() || undefined)}
              allowClear
              style={{ maxWidth: 160 }}
            />
            <Input
              key={`channel:${resetToken}`}
              aria-label="Filter by channel"
              placeholder="Channel"
              defaultValue={channel}
              onChange={(e) => debounce("channel", e.target.value.trim() || undefined)}
              allowClear
              style={{ maxWidth: 140 }}
            />
          </>
        )}
        {filtersActive && (
          <Button type="link" onClick={resetFilters}>
            Reset filters
          </Button>
        )}
        {data.at && (
          <Text type="secondary" style={{ marginLeft: "auto" }}>
            updated {relativeTime(data.at)}
          </Text>
        )}
      </div>

      {audited && data.data && data.data.stored === false && (
        <Paragraph type="secondary" className="hint">
          Tool calls and sign-in events are kept when <Text code>HUBZOID_AUDIT</Text> is <Text code>db</Text> or{" "}
          <Text code>both</Text> in the deployment's settings. See docs/DEPLOYING.md.
        </Paragraph>
      )}
      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : (
        <Table<AuditRow>
          rowKey={(r, i) => `${r.ts}:${i}`}
          size="middle"
          dataSource={rows}
          rowClassName="clickable"
          onRow={(r) => ({
            onClick: () => setDetail(r),
            // The whole row opens the event, from the keyboard too.
            tabIndex: 0,
            "aria-label": `Event details: ${r.tool ?? r.action ?? ""}`.trim(),
            onKeyDown: (e: React.KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setDetail(r); } },
          })}
          scroll={{ x: 640 }}
          pagination={{
            current: page,
            pageSize: PAGE,
            onChange: (p) => setQ({ page: p > 1 ? String(p) : undefined }),
            showSizeChanger: false,
            total: (page - 1) * PAGE + rows.length + (rows.length === PAGE ? 1 : 0),
            hideOnSinglePage: true,
          }}
          locale={{
            emptyText: (
              <Empty
                description={
                  filtersActive
                    ? "Nothing matches these filters."
                    : feed === "changes"
                      ? "No access changes recorded yet. Grants, removals and blocks will appear here."
                      : feed === "decisions"
                        ? "No tool decisions recorded yet. They appear as soon as someone uses a restricted tool."
                        : feed === "calls"
                          ? "No tool calls recorded yet."
                          : "No sign-in events recorded yet."
                }
              >
                {filtersActive && <Button onClick={resetFilters}>Reset filters</Button>}
              </Empty>
            ),
          }}
          columns={[
            {
              title: "When",
              key: "when",
              width: 150,
              render: (_, r) => <When value={r.ts} />,
            },
            {
              title: "What happened",
              key: "what",
              render: (_, r) => {
                const s = describe(r);
                return (
                  <Space align="start">
                    {s.tag ? (
                      <Tag color={s.tone === "negative" ? "red" : s.tone === "positive" ? "green" : undefined}
                        style={{ marginTop: 2 }}>
                        {s.tag}
                      </Tag>
                    ) : s.tone === "negative" ? (
                      <Tag color="red" style={{ marginTop: 2 }}>
                        {feed === "changes" ? "Removed" : "Denied"}
                      </Tag>
                    ) : s.tone === "positive" ? (
                      <Tag color="green" style={{ marginTop: 2 }}>
                        {feed === "changes" ? "Allowed" : "Allowed"}
                      </Tag>
                    ) : (
                      <Tag style={{ marginTop: 2 }}>Info</Tag>
                    )}
                    <SentenceText sentence={s} />
                  </Space>
                );
              },
            },
            ...(!hub
              ? [
                  {
                    title: "Agent",
                    key: "agent",
                    width: 180,
                    render: (_: unknown, r: AuditRow) => (
                      <Text>{r.hub === ORG ? "Organization" : ctx.agentName(r.hub)}</Text>
                    ),
                  },
                ]
              : []),
            {
              title: "",
              key: "open",
              width: 40,
              align: "right" as const,
              render: () => <ChevronRight size={16} aria-hidden className="row-chevron" />,
            },
          ]}
        />
      )}
      <EventDetail
        row={detail}
        feed={feed}
        ctx={ctx}
        agentLabel={detail ? (detail.hub === ORG ? "Organization" : ctx.agentName(detail.hub)) : ""}
        onClose={() => setDetail(null)}
      />
    </div>
  );
}

function describeRow(feed: Feed, row: AuditRow, ctx: ActivityContext) {
  if (feed === "changes") return describeAccessChange(row, ctx);
  if (feed === "calls") return describeToolCall(row, ctx);
  if (feed === "signins") return describeAuthEvent(row, ctx);
  return describeDecision(row, ctx);
}

function EventDetail({
  row,
  feed,
  ctx,
  agentLabel,
  onClose,
}: {
  row: AuditRow | null;
  feed: Feed;
  ctx: ActivityContext;
  agentLabel: string;
  onClose: () => void;
}) {
  const code = (v?: string | null) =>
    v ? <Text copyable className="identity">{v}</Text> : <Text type="secondary">—</Text>;
  const plain = (v?: string | number | null) =>
    v === undefined || v === null || v === "" ? <Text type="secondary">—</Text> : <Text>{String(v)}</Text>;
  const items = row
    ? feed === "calls"
      ? [
          { key: "when", label: "When", children: <Text>{formatTime(row.ts)}</Text> },
          { key: "decision", label: "Access", children: plain(row.decision === "deny" ? "Refused" : "Allowed") },
          { key: "result", label: "Result", children: plain(row.outcome === "error" ? `Failed (${row.error ?? "error"})`
                                                               : row.outcome === "ok" ? "Finished" : undefined) },
          { key: "user", label: "Person", children: code(row.user) },
          { key: "tool", label: "Tool", children: code(row.tool) },
          { key: "server", label: "From", children: code(row.server) },
          { key: "channel", label: "Channel", children: code(row.surface) },
          { key: "agent", label: "Agent", children: <Text>{agentLabel}</Text> },
          { key: "took", label: "Took", children: plain(typeof row.duration_ms === "number" ? `${row.duration_ms} ms` : undefined) },
          { key: "size", label: "Result size", children: plain(typeof row.result_bytes === "number" ? `${row.result_bytes} bytes` : undefined) },
          { key: "args", label: "Arguments", children: row.args === undefined ? plain(undefined)
              : <Text code style={{ whiteSpace: "pre-wrap" }}>{typeof row.args === "string" ? row.args : JSON.stringify(row.args, null, 1)}</Text> },
          { key: "client", label: "Address", children: code(row.client) },
          { key: "chat", label: "Chat", children: code(row.chat_id) },
          { key: "run", label: "Workflow run", children: code(row.run_id) },
          { key: "reason", label: "Reason", children: plain(row.reason) },
        ]
      : feed === "signins"
      ? [
          { key: "when", label: "When", children: <Text>{formatTime(row.ts)}</Text> },
          { key: "event", label: "Event", children: code(row.event) },
          { key: "user", label: "Account", children: code(row.user ?? row.actor) },
          { key: "client", label: "Address", children: code(row.client) },
          { key: "locked", label: "Locked for", children: plain(row.locked_seconds ? `${row.locked_seconds} s` : undefined) },
        ]
      : feed === "changes"
      ? [
          { key: "when", label: "When", children: <Text>{formatTime(row.ts)}</Text> },
          { key: "action", label: "Action", children: <Text>{row.action ?? "—"}</Text> },
          { key: "actor", label: "Changed by", children: code(row.actor) },
          { key: "subject", label: "Affected", children: code(row.subject) },
          { key: "agent", label: "Agent", children: <Text>{agentLabel}</Text> },
          {
            key: "cap",
            // Control-plane events keep a short JSON summary here instead.
            label: (row.permission ?? "").trimStart().startsWith("{") ? "Details" : "Capability",
            children: code(row.permission),
          },
        ]
      : [
          { key: "when", label: "When", children: <Text>{formatTime(row.ts)}</Text> },
          { key: "decision", label: "Outcome", children: <Text>{row.decision ?? "—"}</Text> },
          { key: "user", label: "Person", children: code(row.user) },
          { key: "tool", label: "Tool", children: code(row.tool) },
          { key: "channel", label: "Channel", children: code(row.surface) },
          { key: "agent", label: "Agent", children: <Text>{agentLabel}</Text> },
          { key: "reason", label: "Reason", children: <Text>{row.reason ?? "—"}</Text> },
        ]
    : [];
  return (
    <Drawer
      open={!!row}
      onClose={onClose}
      size={420}
      title="Event details"
      destroyOnHidden
    >
      {row && (
        <div className="drawer-body">
          <SentenceText sentence={describeRow(feed, row, ctx)} />
          <Descriptions size="small" column={1} items={items} />
        </div>
      )}
    </Drawer>
  );
}
