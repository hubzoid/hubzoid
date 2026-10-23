import { useCallback, useEffect, useMemo, useState } from "react";
import { useUnderTabs } from "../lib/underTabs";
import { Alert, Button, Empty, Segmented, Select, Space, Table, Tag, Typography } from "antd";
import { Download, RefreshCw, ThumbsDown, ThumbsUp } from "lucide-react";
import { hubRequest } from "../api";
import type { Hub } from "../api";
import { InfoHelp, LoadState, PAGED } from "../components/common";
import { errorText } from "../hooks/useData";
import { href } from "../hooks/useRoute";

const { Title, Text, Paragraph } = Typography;

// -- types (the /api/hubs/{hub}/feedback responses) -------------------------------------
type Item = {
  id: string;
  rating: "positive" | "negative";
  reason_ids: string[];
  comment: string | null;
  quote?: string | null;
  excerpt?: string | null;
  source: "web" | "tool" | "slack" | "whatsapp" | "cli";
  target_kind: "message" | "channel_reply";
  author_kind: "account" | "external";
  author?: string;
  channel_id?: string | null;
  updated_at: number;
  comment_hidden?: boolean;
  source_hidden?: boolean;
  context?: { kind: "conversation"; conversation_id: string } | { kind: "intake"; intake_id: string } | null;
};
type Page = { items: Item[]; next_cursor: string | null; counts: { positive: number; negative: number } };

const SOURCES: Record<Item["source"], string> = {
  web: "Web chat",
  tool: "Asked in chat",
  slack: "Slack",
  whatsapp: "WhatsApp",
  cli: "CLI",
};
const REASONS: Record<string, string> = {
  accurate: "Accurate",
  useful: "Useful",
  missing_source: "Missing source",
  incorrect: "Incorrect",
  other: "Other",
};
const PERIODS = [
  { label: "7 days", value: "7" },
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "All time", value: "all" },
];

const when = (t: number) => new Date(t * 1000).toLocaleString();
const base = (hub: Hub) => `/api/hubs/${encodeURIComponent(hub.key)}/feedback`;

function query(f: Filters, cursor?: string | null): string {
  const q = new URLSearchParams();
  if (f.rating !== "all") q.set("rating", f.rating);
  if (f.source !== "all") q.set("source", f.source);
  if (f.reason !== "all") q.set("reason", f.reason);
  if (f.period !== "all") q.set("since", String(Math.floor(Date.now() / 1000) - Number(f.period) * 86400));
  if (cursor) q.set("cursor", cursor);
  const s = q.toString();
  return s ? `?${s}` : "";
}

type Filters = { rating: string; source: string; reason: string; period: string };

/** Agents → agent → Feedback: every rating of this agent's answers, from every surface. */
export function AgentFeedback({ hub }: { hub: Hub }) {
  const underTabs = useUnderTabs();
  const [filters, setFilters] = useState<Filters>({ rating: "all", source: "all", reason: "all", period: "30" });
  const [n, setN] = useState(0);
  const reload = useCallback(() => setN((x) => x + 1), []);
  // What is on screen, and for which request: loading is "not yet the current one".
  const key = `${hub.key}|${JSON.stringify(filters)}|${n}`;
  const [shown, setShown] = useState<{ key: string; page?: Page; items: Item[]; error?: string }>({ key: "", items: [] });
  const [moreBusy, setMoreBusy] = useState(false);
  const loading = shown.key !== key || moreBusy;
  const page = shown.page ?? null;
  const items = shown.items;
  const error = shown.key === key ? shown.error : undefined;

  useEffect(() => {
    const controller = new AbortController();
    hubRequest<Page>(`${base(hub)}${query(filters)}`, "GET", undefined, controller.signal)
      .then((data) => !controller.signal.aborted && setShown({ key, page: data, items: data.items }))
      .catch((e) => !controller.signal.aborted && setShown((old) => ({ ...old, key, error: errorText(e) })));
    return () => controller.abort();
  }, [hub, filters, key]);

  const more = async () => {
    if (!page?.next_cursor) return;
    setMoreBusy(true);
    try {
      const next = await hubRequest<Page>(`${base(hub)}${query(filters, page.next_cursor)}`);
      setShown((old) => ({ key: old.key, page: next, items: [...old.items, ...next.items] }));
    } catch (e) {
      setShown((old) => ({ ...old, error: errorText(e) }));
    } finally {
      setMoreBusy(false);
    }
  };

  const set = (key: keyof Filters) => (value: string | number) => setFilters((f) => ({ ...f, [key]: String(value) }));
  const exportQuery = useMemo(() => query(filters).replace(/^\?/, "&"), [filters]);

  return (
    <div className="panel" data-testid="feedback-review">
      {!underTabs && <Title level={2}>Feedback</Title>}
      <Paragraph type="secondary">
        Ratings people gave this agent's answers. They don't change the agent.
        <InfoHelp text="Thumbs in the web chat, a rating someone asked the agent to save, Slack buttons and WhatsApp reactions. You see a comment and its answer only where you could open that conversation yourself. The same list is in hubzoid chat feedback list." />
      </Paragraph>
      <Space style={{ marginBottom: 12 }} wrap>
        <Segmented
          aria-label="Rating"
          value={filters.rating}
          onChange={set("rating")}
          options={[
            { label: "All", value: "all" },
            { label: "Helpful", value: "positive" },
            { label: "Not helpful", value: "negative" },
          ]}
        />
        <Select
          aria-label="Source"
          value={filters.source}
          onChange={set("source")}
          style={{ minWidth: 150 }}
          options={[{ label: "Every source", value: "all" }, ...Object.entries(SOURCES).map(([value, label]) => ({ label, value }))]}
        />
        <Select
          aria-label="Reason"
          value={filters.reason}
          onChange={set("reason")}
          style={{ minWidth: 150 }}
          options={[{ label: "Any reason", value: "all" }, ...Object.entries(REASONS).map(([value, label]) => ({ label, value }))]}
        />
        <Segmented aria-label="Period" value={filters.period} onChange={set("period")} options={PERIODS} />
        <Button icon={<RefreshCw size={14} />} onClick={reload}>
          Refresh
        </Button>
      </Space>
      <Space style={{ marginBottom: 16 }} wrap>
        {page && (
          <Text data-testid="feedback-counts" style={{ display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
            <ThumbsUp size={13} aria-hidden /> {page.counts.positive} helpful
            <span aria-hidden>·</span>
            <ThumbsDown size={13} aria-hidden /> {page.counts.negative} not helpful
          </Text>
        )}
        <Button size="small" icon={<Download size={13} />} href={`${base(hub)}/export?format=csv${exportQuery}`}>
          CSV
        </Button>
        <Button size="small" icon={<Download size={13} />} href={`${base(hub)}/export?format=json${exportQuery}`}>
          JSON
        </Button>
      </Space>
      {!page ? (
        <LoadState error={error} retry={reload} />
      ) : (
        <>
          {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} action={<Button size="small" onClick={reload}>Try again</Button>} />}
          {items.length === 0 ? (
            <Empty
              description={
                filters.rating === "all" && filters.source === "all" && filters.reason === "all"
                  ? "No feedback yet. Ratings appear here when people rate answers."
                  : "No feedback matches these filters."
              }
            />
          ) : (
            <Table<Item>
              rowKey="id"
              dataSource={items}
              pagination={PAGED}
              loading={loading}
              scroll={{ x: true }}
              size="middle"
              columns={[
                {
                  title: "Rating",
                  width: 120,
                  render: (_, r) =>
                    r.rating === "positive" ? (
                      <Tag color="green" icon={<ThumbsUp size={12} aria-hidden />}> Helpful</Tag>
                    ) : (
                      <Tag color="red" icon={<ThumbsDown size={12} aria-hidden />}> Not helpful</Tag>
                    ),
                },
                {
                  title: "Feedback",
                  render: (_, r) => (
                    <div style={{ maxWidth: 360 }}>
                      {r.reason_ids.map((x) => (
                        <Tag key={x}>{REASONS[x] ?? x}</Tag>
                      ))}
                      {r.comment && <Paragraph style={{ margin: "4px 0 0", whiteSpace: "pre-wrap" }}>{r.comment}</Paragraph>}
                      {r.quote && r.quote !== r.comment && (
                        <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
                          They asked: “{r.quote}”
                        </Text>
                      )}
                      {r.comment_hidden && (
                        <Text type="secondary" italic>
                          Comment hidden: you can't open where it came from.
                        </Text>
                      )}
                    </div>
                  ),
                },
                {
                  title: "Answer",
                  render: (_, r) =>
                    r.excerpt ? (
                      <Text style={{ maxWidth: 320, display: "inline-block" }} type="secondary">
                        {r.excerpt}
                      </Text>
                    ) : (
                      <Text type="secondary" italic>
                        {r.source_hidden ? "Hidden" : "No longer available"}
                      </Text>
                    ),
                },
                { title: "Source", width: 130, render: (_, r) => <Tag>{SOURCES[r.source] ?? r.source}</Tag> },
                { title: "From", render: (_, r) => r.author || (r.author_kind === "external" ? "Outside sender" : "A person") },
                { title: "When", width: 180, render: (_, r) => when(r.updated_at) },
                {
                  title: "Open",
                  render: (_, r) =>
                    r.context?.kind === "conversation" ? (
                      <a href={`/c/${encodeURIComponent(r.context.conversation_id)}`}>Conversation</a>
                    ) : r.context?.kind === "intake" ? (
                      <a href={href(`/agents/${encodeURIComponent(hub.key)}/intake/${encodeURIComponent(r.context.intake_id)}`)}>
                        Intake item
                      </a>
                    ) : (
                      ""
                    ),
                },
              ]}
            />
          )}
          {page.next_cursor && (
            <div style={{ marginTop: 12 }}>
              <Button onClick={() => void more()} loading={loading}>
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
