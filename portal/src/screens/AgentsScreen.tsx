import { useState } from "react";
import { Button, Card, Empty, Input, Space, Tag, Tooltip, Typography } from "antd";
import { Search } from "lucide-react";
import { type Hub, type SummaryHub, type Workflow } from "../api";
import { agentHref } from "../hooks/useRoute";
import { AgentAvatar } from "../components/common";

import { Cost } from "../components/UsageCost";

import { short } from "../lib/format";

const { Text } = Typography;

export function AgentCards({ hubs, workflows, usage }: { hubs: Hub[]; workflows?: Workflow[]; usage?: SummaryHub[] }) {
  const [search, setSearch] = useState("");
  const list = hubs.filter(
    (h) =>
      h.name.toLowerCase().includes(search.toLowerCase()) ||
      h.key.toLowerCase().includes(search.toLowerCase()),
  );
  const byHub = (key: string) => (workflows ?? []).filter((w) => w.hub === key);

  return (
    <>
      <div className="agents-bar">
        <Input
          aria-label="Search agents"
          prefix={<Search size={16} />}
          placeholder="Find an agent"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          allowClear
          style={{ maxWidth: 320 }}
        />
        <Text type="secondary" className="agents-count">
          {hubs.length} {hubs.length === 1 ? "agent" : "agents"}
        </Text>
      </div>

      {list.length === 0 ? (
        <Empty
          description={
            search
              ? `No agent matches “${search}”.`
              : "No agents are registered in this deployment, or none are assigned to you."
          }
        />
      ) : (
        <div className="agent-grid">
          {list.map((h) => {
            const flows = byHub(h.key);
            const metrics = usage?.find((row) => row.key === h.key);
            const scheduled = flows.filter((w) => w.schedule).length;
            const stale = flows.some((w) => w.state === "stale");
            const broken = flows.some((w) => w.state === "error");
            return (
              <Card
                key={h.key}
                className="agent-card"
                hoverable
                onClick={() => {
                  location.hash = `/agents/${encodeURIComponent(h.key)}/access`;
                }}
              >
                <div className="agent-card-head">
                  <AgentAvatar size={44} />
                  <div className="agent-card-title">
                    <a href={agentHref(h.key)} className="agent-name" onClick={(e) => e.stopPropagation()}>
                      {h.name}
                    </a>
                    <Text type="secondary" className="identity">
                      {h.key}
                    </Text>
                  </div>
                </div>
                <Space wrap size={[6, 6]} className="agent-card-tags">
                  {h.authoritative ? (
                    <Tag color="green">Managed here</Tag>
                  ) : (
                    <Tooltip title="Access changes take effect only after migration.">
                      <Tag color="gold">Legacy access</Tag>
                    </Tooltip>
                  )}
                  {workflows &&
                    (stale ? (
                      <Tag color="red">Scheduler stopped</Tag>
                    ) : broken ? (
                      <Tag color="red">Workflow error</Tag>
                    ) : scheduled ? (
                      <Tag>
                        {flows.length} {flows.length === 1 ? "workflow" : "workflows"} · {scheduled} with a schedule
                      </Tag>
                    ) : flows.length ? (
                      <Tag>{flows.length} manual {flows.length === 1 ? "workflow" : "workflows"}</Tag>
                    ) : (
                      <Tag>No workflows</Tag>
                    ))}
                </Space>
                {metrics && <dl className="agent-card-usage" aria-label={`${h.name} usage`}>
                  <div>
                    <dt>Tokens used</dt>
                    <dd>{short(metrics.input_tokens + metrics.output_tokens)}</dd>
                    <small>{short(metrics.input_tokens)} in · {short(metrics.output_tokens)} out</small>
                  </div>
                  <div>
                    <dt>Approx. cost</dt>
                    <dd><Cost usd={metrics.cost_usd} unpriced={metrics.unpriced} /></dd>
                    <small>USD</small>
                  </div>
                </dl>}
                <div className="agent-card-actions" onClick={(e) => e.stopPropagation()}>
                  <Button type="primary" href={agentHref(h.key, "access")}>
                    Manage access
                  </Button>
                  <div className="agent-card-secondary">
                    <Button href={agentHref(h.key, "runs")}>Runs &amp; schedules</Button>
                    <Button href={agentHref(h.key, "activity")}>Activity</Button>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}
