import { useEffect, useRef, useState } from "react";
import {
  Button,
  Checkbox,
  Empty,
  Input,
  Select,
  Table,
  Typography,
} from "antd";
import { RefreshCw } from "lucide-react";
import { query, type Hub, type Run } from "../api";
import { useData } from "../hooks/useData";
import { useHashQuery, href } from "../hooks/useRoute";
import { LoadState, RunStatusTag, When } from "../components/common";
import { formatDuration, formatTime, relativeTime } from "../lib/format";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;
const RANGES: Record<string, number> = { "1d": 86400, "7d": 604800, "30d": 2592000 };
const REFRESH_MS = 10000;

// UI status buckets the backend understands (observe.STATUS_BUCKETS). "running"
// spans PENDING/ENQUEUED, so auto-refresh keys off it to keep active runs fresh.
const STATUS_OPTIONS = [
  { value: "running", label: "Running" },
  { value: "succeeded", label: "Succeeded" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

type RunsResponse = { runs: Run[]; has_more?: boolean };

const detailHref = (r: Run) =>
  href(
    `/agents/${encodeURIComponent(r.hub)}/runs/${encodeURIComponent(r.name)}/${encodeURIComponent(r.id)}`,
  );

export function AllRunsScreen({ hubs }: { hubs: Hub[] }) {
  // Every filter + page lives in the URL: refresh, Back and shared links restore it.
  const [q, setQ] = useHashQuery();
  const agent = q.agent || "";
  const status = q.status || "";
  const range = q.range || ""; // relative span token (for the Select + live sliding)
  const sinceAbs = q.since || ""; // absolute ISO boundaries (fixed investigation)
  const untilAbs = q.until || "";
  const runId = (q.run || "").trim();
  const auto = q.auto === "1";
  const page = Math.max(1, Number(q.page) || 1);
  const filtersActive = !!(agent || status || range || sinceAbs || untilAbs || runId);
  const runTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // Keyed by a reset token (not the changing value) so a debounced commit doesn't
  // remount the input and steal focus; Reset bumps it to clear the field.
  const [resetToken, setResetToken] = useState(0);
  // Cancel a pending run-id debounce on unmount so it cannot rewrite the next
  // screen's URL query after navigation.
  useEffect(() => () => clearTimeout(runTimer.current), []);

  // Two window modes, kept explicitly separate:
  //  • LIVE (auto + relative range): cutoff = now − span, recomputed from the current
  //    clock and slid forward on each auto-refresh. Nothing absolute is stored.
  //  • FIXED (default): absolute since/until live in the URL, so reload and shared
  //    links reopen the exact same window regardless of the clock; the fixed upper
  //    bound (`until`) also stops newly-arrived runs from shifting later pages.
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Freeze a relative span (seconds) to absolute {since, until} at the current clock.
  const frozen = (span: number) => {
    const now = Date.now();
    return {
      since: new Date(now - span * 1000).toISOString(),
      until: new Date(now).toISOString(),
    };
  };
  let sinceParam: string | undefined;
  let untilParam: string | undefined;
  if (auto && RANGES[range]) {
    sinceParam = new Date(nowMs - RANGES[range] * 1000).toISOString(); // live, sliding
  } else if (sinceAbs || untilAbs) {
    sinceParam = sinceAbs || undefined; // fixed, absolute, shareable
    untilParam = untilAbs || undefined;
  }

  // A hand-crafted or legacy link that carries only a relative range (no auto, no
  // absolute bounds) is frozen to absolute boundaries on open, so from then on it is
  // a stable, shareable fixed window.
  useEffect(() => {
    if (!auto && RANGES[range] && !sinceAbs && !untilAbs) {
      const now = Date.now();
      setQ({
        since: new Date(now - RANGES[range] * 1000).toISOString(),
        until: new Date(now).toISOString(),
      });
    }
  }, [auto, range, sinceAbs, untilAbs, setQ]);

  const params = {
    hub: agent || undefined,
    status: status || undefined,
    run_id: runId || undefined,
    since: sinceParam,
    until: untilParam,
    offset: (page - 1) * PAGE,
    limit: PAGE,
  };
  // keepStale: a failed background refresh of THIS query keeps the last rows (with a
  // note) instead of an error screen — safe here because Runs is read-only. A filter
  // change (new path) still shows loading, never stale rows.
  const data = useData<RunsResponse>("/runs" + query(params), { keepStale: true });

  // Auto-refresh without overlapping requests: a tick acts only when no refresh is in
  // flight. With a live relative range it advances the cutoff (sliding window); with no
  // range it re-fetches the same query. Cleared on unmount / when toggled off.
  const refreshingRef = useRef(false);
  useEffect(() => {
    refreshingRef.current = data.refreshing || data.loading;
  }, [data.refreshing, data.loading]);
  const reload = data.reload;
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(() => {
      if (refreshingRef.current) return;
      if (RANGES[range]) setNowMs(Date.now());
      else reload();
    }, REFRESH_MS);
    return () => clearInterval(id);
  }, [auto, range, reload]);

  // Choosing a range: live keeps it relative; fixed freezes it to absolute bounds now.
  const chooseRange = (v?: string) => {
    if (!v) return setQ({ range: undefined, since: undefined, until: undefined, page: undefined });
    if (auto) return setQ({ range: v, since: undefined, until: undefined, page: undefined });
    const f = frozen(RANGES[v]);
    setQ({ range: v, since: f.since, until: f.until, page: undefined });
  };
  // Toggling live: on drops the frozen bounds (slide the relative range); off freezes
  // the current range to absolute bounds so the investigation stops moving.
  const toggleAuto = (on: boolean) => {
    if (on) return setQ({ auto: "1", since: undefined, until: undefined });
    if (RANGES[range]) {
      const f = frozen(RANGES[range]);
      return setQ({ auto: undefined, since: f.since, until: f.until });
    }
    setQ({ auto: undefined });
  };

  const resetFilters = () => {
    setResetToken((t) => t + 1); // remount the run-id input so its defaultValue clears
    setQ({
      agent: undefined,
      status: undefined,
      range: undefined,
      since: undefined,
      until: undefined,
      run: undefined,
      page: undefined,
    });
  };

  const shown = data.data;
  const rows = shown?.runs ?? [];
  const hasMore = !!shown?.has_more;
  const agentName = (key: string) => hubs.find((h) => h.key === key)?.name ?? key;

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={1} style={{ fontSize: 28 }}>Runs across your agents</Title>
          <Paragraph type="secondary">
            Every workflow run from the agents you manage, newest first. Filter, then
            open a run to see its result and steps. Scheduler health lives on each
            agent’s Runs &amp; schedules tab.
          </Paragraph>
        </div>
        <Button icon={<RefreshCw size={16} />} onClick={data.reload} loading={data.refreshing}>
          Refresh
        </Button>
      </div>

      <div className="toolbar">
        <Select
          aria-label="Agent"
          value={agent || undefined}
          onChange={(v) => setQ({ agent: v, page: undefined })}
          placeholder="All agents"
          allowClear
          style={{ minWidth: 180 }}
          options={hubs.map((h) => ({ value: h.key, label: h.name }))}
        />
        <Select
          aria-label="Status"
          value={status || undefined}
          onChange={(v) => setQ({ status: v, page: undefined })}
          placeholder="Any status"
          allowClear
          style={{ minWidth: 150 }}
          options={STATUS_OPTIONS}
        />
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
          key={`run:${resetToken}`}
          aria-label="Filter by run id"
          placeholder="Run id"
          defaultValue={runId}
          onChange={(e) => {
            const v = e.target.value.trim();
            clearTimeout(runTimer.current);
            runTimer.current = setTimeout(
              () => setQ({ run: v || undefined, page: undefined }),
              300,
            );
          }}
          allowClear
          style={{ maxWidth: 260 }}
        />
        <Checkbox checked={auto} onChange={(e) => toggleAuto(e.target.checked)}>
          Auto-refresh
        </Checkbox>
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

      {data.error && shown && (
        <Text type="secondary" className="hint">
          Couldn’t refresh just now — showing the last loaded runs.
        </Text>
      )}

      {!shown ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : (
        <Table<Run>
          rowKey={(r) => `${r.hub}:${r.id}`}
          size="middle"
          dataSource={rows}
          scroll={{ x: 820 }}
          pagination={{
            current: page,
            pageSize: PAGE,
            onChange: (p) => setQ({ page: p > 1 ? String(p) : undefined }),
            showSizeChanger: false,
            // The API reports has_more instead of a total; allow paging forward
            // only while another page exists.
            total: (page - 1) * PAGE + rows.length + (hasMore ? 1 : 0),
            hideOnSinglePage: true,
          }}
          locale={{
            emptyText: (
              <Empty
                description={
                  filtersActive
                    ? "No runs match these filters."
                    : "No workflow runs recorded yet. They appear here after a schedule fires or a workflow is started manually."
                }
              >
                {filtersActive && <Button onClick={resetFilters}>Reset filters</Button>}
              </Empty>
            ),
          }}
          columns={[
            {
              title: "Agent",
              key: "agent",
              width: 160,
              render: (_, r) => <Text>{agentName(r.hub)}</Text>,
            },
            {
              title: "Workflow",
              key: "workflow",
              width: 180,
              render: (_, r) => <a href={detailHref(r)}>{r.name}</a>,
            },
            {
              title: "Run",
              key: "id",
              render: (_, r) => (
                <a href={detailHref(r)} className="identity run-id-link" title={r.id}>
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
              width: 170,
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
              width: 110,
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
