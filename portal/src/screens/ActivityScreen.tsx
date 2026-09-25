import { useEffect, useMemo, useRef, useState } from "react";
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
import { RefreshCw } from "lucide-react";
import { query, type AuditRow, type Hub, type Person } from "../api";
import { useData } from "../hooks/useData";
import { useCatalogs } from "../hooks/useCatalogs";
import { useHashQuery } from "../hooks/useRoute";
import { LoadState, SentenceText, When } from "../components/common";
import {
  ORG,
  describeAccessChange,
  describeDecision,
  formatTime,
  personName,
  relativeTime,
  type ActivityContext,
} from "../lib/format";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

type Feed = "changes" | "decisions";
const RANGES: Record<string, number> = { "1d": 86400, "7d": 604800, "30d": 2592000 };

export function ActivityScreen({ hubs, hub }: { hubs: Hub[]; hub?: Hub }) {
  // Every filter + page lives in the URL (shareable, restored on refresh/Back).
  const [q, setQ] = useHashQuery();
  const feed: Feed = q.feed === "decisions" ? "decisions" : "changes";
  const agent = hub ? hub.key : q.agent || "";
  const person = (q.person || "").toLowerCase();
  const actor = (q.actor || "").toLowerCase();
  const action = q.action || "";
  const tool = q.tool || "";
  const outcome = q.outcome || "";
  const channel = q.channel || "";
  const range = q.range || ""; // relative token, kept for the Select display
  const sinceAbs = q.since || ""; // absolute ISO boundaries — the actual window
  const untilAbs = q.until || "";
  const page = Math.max(1, Number(q.page) || 1);
  const filtersActive = !!(
    person || actor || action || tool || outcome || channel ||
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
          outcome: outcome || undefined,
          tool,
          surface: channel,
          since: sinceMs ? new Date(sinceMs).toISOString() : undefined,
          until: untilMs ? new Date(untilMs).toISOString() : undefined,
        };
  const data = useData<{ rows: AuditRow[] }>(
    (feed === "changes" ? "/access-changes" : "/audit") + query(params),
  );
  const resetFilters = () => {
    setResetToken((t) => t + 1); // remount the text inputs so their defaultValue clears
    setQ({
      person: undefined, actor: undefined, action: undefined, tool: undefined,
      outcome: undefined, channel: undefined, range: undefined,
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
  const describe = (r: AuditRow) =>
    feed === "changes" ? describeAccessChange(r, ctx) : describeDecision(r, ctx);
  const [detail, setDetail] = useState<AuditRow | null>(null);

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={hub ? 2 : 1} style={{ fontSize: 28 }}>{hub ? `Activity in ${hub.name}` : "Activity across your agents"}</Title>
          <Paragraph type="secondary">
            {feed === "changes"
              ? "Who changed access, for whom, and to what. Recorded every time access is granted, removed, blocked or migrated."
              : "Every restricted tool call and whether it was allowed. Recorded by the agent runtime when the tool is invoked."}
          </Paragraph>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload}>
          Refresh
        </Button>
      </div>

      <div className="toolbar">
        <Segmented<Feed>
          value={feed}
          onChange={(v) => setQ({ feed: v, page: undefined })}
          options={[
            { label: "Access changes", value: "changes" },
            { label: "Tool decisions", value: "decisions" },
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
                { value: "workflow_pause", label: "Paused a workflow" },
                { value: "workflow_resume", label: "Resumed a workflow" },
                { value: "run_cancel", label: "Cancelled a run" },
              ]}
            />
          </>
        )}
        {feed === "decisions" && (
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

      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : (
        <Table<AuditRow>
          rowKey={(r, i) => `${r.ts}:${i}`}
          size="middle"
          dataSource={rows}
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
                      : "No tool decisions recorded yet. They appear as soon as someone uses a restricted tool."
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
                    {s.tone === "negative" ? (
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
              key: "details",
              width: 90,
              align: "right" as const,
              render: (_: unknown, r: AuditRow) => (
                <Button size="small" onClick={() => setDetail(r)}>
                  Details
                </Button>
              ),
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
  const items = row
    ? feed === "changes"
      ? [
          { key: "when", label: "When", children: <Text>{formatTime(row.ts)}</Text> },
          { key: "action", label: "Action", children: <Text>{row.action ?? "—"}</Text> },
          { key: "actor", label: "Changed by", children: code(row.actor) },
          { key: "subject", label: "Affected", children: code(row.subject) },
          { key: "agent", label: "Agent", children: <Text>{agentLabel}</Text> },
          {
            key: "cap",
            label: "Capability",
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
          <SentenceText
            sentence={feed === "changes" ? describeAccessChange(row, ctx) : describeDecision(row, ctx)}
          />
          <Descriptions size="small" column={1} items={items} />
        </div>
      )}
    </Drawer>
  );
}
