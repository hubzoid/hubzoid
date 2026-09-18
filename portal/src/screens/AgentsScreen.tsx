import { useState } from "react";
import { Button, Card, Empty, Input, Space, Tag, Tooltip, Typography } from "antd";
import { Search } from "lucide-react";
import { query, type Hub, type Me, type Overview, type Workflow } from "../api";
import { useData } from "../hooks/useData";
import { agentHref } from "../hooks/useRoute";
import { AgentAvatar, PageHeader } from "../components/common";

const { Text } = Typography;

export function AgentsScreen({ hubs }: { me: Me; hubs: Hub[] }) {
  const [search, setSearch] = useState("");
  const overview = useData<Overview>("/overview");
  const workflows = useData<{ workflows: Workflow[] }>("/workflows" + query({}));
  const list = hubs.filter(
    (h) =>
      h.name.toLowerCase().includes(search.toLowerCase()) ||
      h.key.toLowerCase().includes(search.toLowerCase()),
  );
  const byHub = (key: string) => (workflows.data?.workflows ?? []).filter((w) => w.hub === key);

  return (
    <>
      <PageHeader
        eyebrow="Your workspace"
        title="Agents"
        description="Choose an agent to decide who can use it, check its scheduled work, or review what happened."
      />

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
          {overview.data ? ` · ${overview.data.people} people` : ""}
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
                  {workflows.data &&
                    (stale ? (
                      <Tag color="red">Scheduler stopped</Tag>
                    ) : broken ? (
                      <Tag color="red">Workflow error</Tag>
                    ) : scheduled ? (
                      <Tag>
                        {scheduled} scheduled {scheduled === 1 ? "workflow" : "workflows"}
                      </Tag>
                    ) : flows.length ? (
                      <Tag>{flows.length} manual {flows.length === 1 ? "workflow" : "workflows"}</Tag>
                    ) : (
                      <Tag>No workflows</Tag>
                    ))}
                </Space>
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
