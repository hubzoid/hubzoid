import { Button, Breadcrumb, Tabs, Tag, Tooltip } from "antd";
import type { Hub } from "../api";
import { agentHref, href, navigate } from "../hooks/useRoute";
import { AgentAvatar, PageHeader } from "../components/common";
import { AccessEditor } from "./access/AccessEditor";
import { RunsScreen } from "./RunsScreen";
import { ActivityScreen } from "./ActivityScreen";
import { EvalsScreen } from "./EvalsScreen";

export type AgentTab = "access" | "runs" | "evals" | "activity";

export function AgentDetail({
  hub,
  hubs,
  tab,
  rest,
}: {
  hub: Hub;
  hubs: Hub[];
  tab: AgentTab;
  /** Remaining route segments after the tab (workflow and run id, or an eval run). */
  rest: string[];
}) {
  return (
    <>
      <Breadcrumb
        items={[{ title: <a href={href("/agents")}>Agents</a> }, { title: hub.name }]}
      />
      <PageHeader
        title={
          <span className="agent-title">
            <AgentAvatar size={40} />
            <span>{hub.name}</span>
            {hub.authoritative ? (
              <Tag color="green">Managed here</Tag>
            ) : (
              <Tooltip title="Access changes take effect only after migration.">
                <Tag color="gold">Legacy access</Tag>
              </Tooltip>
            )}
          </span>
        }
        description={
          <>
            Agent <span className="identity">{hub.key}</span> · decide who can use it, check its scheduled work and evals, and review what happened.
          </>
        }
      />
      {hub.can_chat && hub.model_id && <Button href={`/?models=${encodeURIComponent(hub.model_id)}`} style={{ marginBottom: 16 }}>Open chat with {hub.name} ↗</Button>}
      <Tabs
        activeKey={tab}
        onChange={(key) => navigate(`/agents/${encodeURIComponent(hub.key)}/${key}`)}
        items={[
          { key: "access", label: <a href={agentHref(hub.key, "access")}>Access</a> },
          { key: "runs", label: <a href={agentHref(hub.key, "runs")}>Runs &amp; schedules</a> },
          { key: "evals", label: <a href={agentHref(hub.key, "evals")}>Evals</a> },
          { key: "activity", label: <a href={agentHref(hub.key, "activity")}>Activity</a> },
        ]}
      />
      <div key={`${hub.key}:${tab}`}>
        {tab === "access" && <AccessEditor hub={hub} />}
        {tab === "runs" && <RunsScreen hub={hub} workflow={rest[0]} run={rest[1]} />}
        {tab === "evals" && <EvalsScreen hub={hub} run={rest[0]} />}
        {tab === "activity" && <ActivityScreen hubs={hubs} hub={hub} />}
      </div>
    </>
  );
}
