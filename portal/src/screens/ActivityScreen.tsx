import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
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
import { LoadState, SentenceText, When } from "../components/common";
import {
  ORG,
  describeAccessChange,
  describeDecision,
  personName,
  type ActivityContext,
} from "../lib/format";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

type Feed = "changes" | "decisions";

export function ActivityScreen({ hubs, hub }: { hubs: Hub[]; hub?: Hub }) {
  const [feed, setFeed] = useState<Feed>("changes");
  const [agent, setAgent] = useState<string>(hub?.key ?? "");
  const [personInput, setPersonInput] = useState("");
  const [person, setPerson] = useState("");
  const [denied, setDenied] = useState(false);
  const [page, setPage] = useState(1);
  useEffect(() => {
    const t = setTimeout(() => {
      setPerson(personInput.trim().toLowerCase());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [personInput]);

  const scope = hub?.key ?? agent;
  const data = useData<{ rows: AuditRow[] }>(
    (feed === "changes" ? "/access-changes" : "/audit") +
      query({
        hub: scope,
        user: person,
        denied: feed === "decisions" ? denied : undefined,
        offset: (page - 1) * PAGE,
        limit: PAGE,
      }),
  );
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

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={4}>{hub ? `Activity in ${hub.name}` : "Activity across your agents"}</Title>
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
          onChange={(v) => {
            setFeed(v);
            setPage(1);
          }}
          options={[
            { label: "Access changes", value: "changes" },
            { label: "Tool decisions", value: "decisions" },
          ]}
        />
        {!hub && (
          <Select
            aria-label="Agent"
            value={agent}
            onChange={(v) => {
              setAgent(v);
              setPage(1);
            }}
            style={{ minWidth: 200 }}
            options={[
              { value: "", label: "All agents" },
              ...hubs.map((h) => ({ value: h.key, label: h.name })),
            ]}
          />
        )}
        <Input
          aria-label="Filter by person"
          placeholder="Person’s exact email"
          value={personInput}
          onChange={(e) => setPersonInput(e.target.value)}
          allowClear
          style={{ maxWidth: 260 }}
        />
        {feed === "decisions" && (
          <Checkbox
            checked={denied}
            onChange={(e) => {
              setDenied(e.target.checked);
              setPage(1);
            }}
          >
            Denied only
          </Checkbox>
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
            onChange: setPage,
            showSizeChanger: false,
            total: (page - 1) * PAGE + rows.length + (rows.length === PAGE ? 1 : 0),
            hideOnSinglePage: true,
          }}
          locale={{
            emptyText: (
              <Empty
                description={
                  feed === "changes"
                    ? person
                      ? `No access changes recorded for ${person}.`
                      : "No access changes recorded yet. Grants, removals and blocks will appear here."
                    : denied
                      ? "No denied tool calls recorded. Denials appear when someone calls a restricted tool without the capability."
                      : "No tool decisions recorded yet. They appear as soon as someone uses a restricted tool."
                }
              />
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
          ]}
        />
      )}
    </div>
  );
}
