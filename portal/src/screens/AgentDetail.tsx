import { Button, Breadcrumb, Space, Tabs } from "antd";
import type { Hub, Me } from "../api";
import { agentHref, href, navigate } from "../hooks/useRoute";
import { AgentAvatar, PageHeader } from "../components/common";
import { AccessEditor } from "./access/AccessEditor";
import { RunsScreen } from "./RunsScreen";
import { ActivityScreen } from "./ActivityScreen";
import { EvalsScreen } from "./EvalsScreen";
import { AgentConnectors } from "./ConnectorsScreen";
import { AgentSecrets } from "./SecretsScreen";
import { AgentChannels } from "./ChannelsScreen";

export type AgentTab = "access" | "connectors" | "secrets" | "channels" | "runs" | "evals" | "activity";

export function AgentDetail({
  hub,
  hubs,
  me,
  tab,
  rest,
  embedded = false,
}: {
  hub: Hub;
  hubs: Hub[];
  me: Me;
  tab: AgentTab;
  /** Remaining route segments after the tab (workflow and run id, or an eval run). */
  rest: string[];
  /** Inside the chat app's Settings, where Agent instructions also lives. */
  embedded?: boolean;
}) {
  return (
    <>
      <Breadcrumb
        items={[{ title: <a href={href("/agents")}>Agents</a> }, { title: hub.name }]}
      />
      <PageHeader
        title={
          <span className="agent-title" title={hub.key}>
            <AgentAvatar size={32} />
            <span>{hub.name}</span>
          </span>
        }
        description="Who can use it, what it connects to, its secrets and channels, workflows, quality checks and activity."
        extra={
          <Space>
            {embedded && me.org_admin && (
              <Button href={`/settings#/workspace?agent=${encodeURIComponent(hub.key)}`}>Edit instructions</Button>
            )}
            {hub.can_chat && hub.model_id && (
              <Button href={`/?models=${encodeURIComponent(hub.model_id)}`}>Open chat</Button>
            )}
          </Space>
        }
      />
      {embedded ? (
        <nav className="hz-agent-tabs" aria-label="Agent sections">
          {([['access', 'Access'], ['connectors', 'Connectors'], ['secrets', 'Secrets'], ['channels', 'Channels'], ['runs', 'Runs & schedules'], ['evals', 'Evals'], ['activity', 'Activity']] as const).map(([key, label]) => (
            <a key={key} href={agentHref(hub.key, key)} aria-current={tab === key ? 'page' : undefined}>{label}</a>
          ))}
        </nav>
      ) : <Tabs
        activeKey={tab}
        onChange={(key) => navigate(`/agents/${encodeURIComponent(hub.key)}/${key}`)}
        items={[
          { key: "access", label: <a href={agentHref(hub.key, "access")}>Access</a> },
          { key: "connectors", label: <a href={agentHref(hub.key, "connectors")}>Connectors</a> },
          { key: "secrets", label: <a href={agentHref(hub.key, "secrets")}>Secrets</a> },
          { key: "channels", label: <a href={agentHref(hub.key, "channels")}>Channels</a> },
          { key: "runs", label: <a href={agentHref(hub.key, "runs")}>Runs &amp; schedules</a> },
          { key: "evals", label: <a href={agentHref(hub.key, "evals")}>Evals</a> },
          { key: "activity", label: <a href={agentHref(hub.key, "activity")}>Activity</a> },
        ]}
      />}
      <div key={`${hub.key}:${tab}`}>
        {tab === "access" && <AccessEditor hub={hub} embedded={embedded} />}
        {tab === "connectors" && <AgentConnectors hub={hub} me={me} embedded={embedded} />}
        {tab === "secrets" && <AgentSecrets hub={hub} />}
        {tab === "channels" && <AgentChannels hub={hub} rest={rest} />}
        {tab === "runs" && <RunsScreen hub={hub} workflow={rest[0]} run={rest[1]} embedded={embedded} />}
        {tab === "evals" && <EvalsScreen hub={hub} run={rest[0]} embedded={embedded} />}
        {tab === "activity" && <ActivityScreen hubs={hubs} hub={hub} />}
      </div>
    </>
  );
}
