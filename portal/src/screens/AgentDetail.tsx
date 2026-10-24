import { Suspense, useContext } from "react";
import { Breadcrumb, Spin } from "antd";
import { MessageSquare } from "lucide-react";
import { type Hub, type Me } from "../api";
import { agentHref, href } from "../hooks/useRoute";
import { lazyChunk } from "../lib/lazyChunk";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar } from "../app/components/ui";
import { BarePage } from "../app/pages/barePage";
import { AccessEditor } from "./access/AccessEditor";
import { RunsScreen } from "./RunsScreen";
import { EvalsScreen } from "./EvalsScreen";
import { AgentConnectors } from "./ConnectorsScreen";
import { AgentSecrets } from "./SecretsScreen";
import { AgentChannels } from "./ChannelsScreen";
import { AgentIntake } from "./IntakeScreen";
import { AgentFeedback } from "./FeedbackScreen";
import { AgentSetup } from "./SetupScreen";
import { AgentEditing, useEditing } from "../app/lib/editing";
import { UnderTabs } from "../lib/underTabs";
import { WorkItemsScreen } from "./WorkItemsScreen";
import { agentName } from "../lib/agentName";
import { SandboxesScreen } from "./SandboxesScreen";

const WorkspacePage = lazyChunk(() => import("../app/pages/WorkspacePage"));
const HubChangesPage = lazyChunk(() => import("../app/pages/HubChangesPage"));
const WorkflowAccessPage = lazyChunk(() => import("../app/pages/WorkflowAccessPage"));

export type AgentTab =
  | "instructions" | "changes" | "access" | "permissions" | "connectors" | "runs" | "work" | "intake"
  | "sandboxes" | "evals" | "feedback" | "config" | "channels";

type Section = { key: AgentTab; label: string; parts?: [AgentTab, string][] };

/** The agent page's tabs. A tab never repeats its own name one level down;
 *  rarely used parts show as a second row only when the agent has them. */
function sections(hub: Hub, editable: boolean): Section[] {
  const has = hub.has ?? { work: false, intake: false, sandboxes: false };
  const work: [AgentTab, string][] = [];
  if (has.work) work.push(["work", "Work items"]);
  if (has.intake) work.push(["intake", "Incoming messages"]);
  if (has.sandboxes) work.push(["sandboxes", "Sandboxes"]);
  return [
    { key: "instructions", label: "Instructions",
      parts: editable ? [["instructions", "Files"], ["changes", "Changes to review"]] : undefined },
    { key: "access", label: "Access", parts: [["access", "People"], ["permissions", "Workflow permissions"]] },
    { key: "connectors", label: "Connectors" },
    { key: "runs", label: "Workflows" },
    ...(work.length ? [{ key: work[0][0], label: "Work", parts: work.length > 1 ? work : undefined }] : []),
    { key: "evals", label: "Evals" },
    { key: "feedback", label: "Feedback" },
    { key: "config", label: "Configuration" },
  ];
}
const sectionOf = (list: Section[], tab: AgentTab) =>
  list.find((s) => s.key === tab || s.parts?.some(([k]) => k === tab))
  ?? (tab === "channels" ? list.find((s) => s.key === "config") : undefined) ?? list[0];

export function AgentDetail({
  hub,
  hubs: _hubs,
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
  /** Inside the chat app's Settings. */
  embedded?: boolean;
}) {
  const app = useContext(AppContext);
  const agent = app?.agents.list.find((a) => a.hub === hub.key);
  const name = agentName(agent?.name, hub.name, hub.key);
  const editing = useEditing(hub.key);
  const SECTIONS = sections(hub, editing.editable);
  const section = sectionOf(SECTIONS, tab);
  const chat = hub.can_chat && agent ? `/new/${encodeURIComponent(agent.id)}` : null;
  return (
    <>
      <Breadcrumb items={[{ title: <a href={href("/agents")}>Agents</a> }, { title: name }]} />
      <div className="hz-agent-head">
        <AgentAvatar name={name} src={agent?.avatar_url} size={44} />
        <div className="min-w-0 flex-1">
          <h1>{name}</h1>
          {agent?.description && <p>{agent.description}</p>}
        </div>
        <a className="hz-btn hz-btn-ghost" href={href(`/activity?agent=${encodeURIComponent(hub.key)}`)}>
          View activity
        </a>
        {chat && (
          <a className="hz-btn hz-btn-secondary" href={chat}>
            <MessageSquare size={15} aria-hidden /> Open chat
          </a>
        )}
      </div>
      <nav className="hz-agent-tabs" aria-label="Agent sections">
        {SECTIONS.map((s) => (
          <a key={s.key} href={agentHref(hub.key, s.key)} aria-current={section.key === s.key ? "page" : undefined}>{s.label}</a>
        ))}
      </nav>
      {section.parts && (
        <nav className="hz-subtabs" aria-label={section.label}>
          {section.parts.map(([key, label]) => (
            <a key={key} href={agentHref(hub.key, key)} aria-current={tab === key ? "page" : undefined}>
              {label}{key === "changes" && <span className="hz-beta">Beta</span>}
            </a>
          ))}
        </nav>
      )}
      <AgentEditing.Provider value={editing}>
      <UnderTabs.Provider value={tab !== "config"}>
      <div key={`${hub.key}:${tab}`}>
        {tab === "access" && <AccessEditor hub={hub} embedded={embedded} />}
        {tab === "permissions" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><WorkflowAccessPage hub={hub.key} /></Suspense>
          </BarePage.Provider>
        )}
        {tab === "connectors" && <AgentConnectors hub={hub} me={me} embedded={embedded} />}
        {tab === "instructions" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><WorkspacePage agent={hub.key} readOnly={!editing.editable} note={editing.message} /></Suspense>
          </BarePage.Provider>
        )}
        {tab === "changes" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><HubChangesPage hub={hub.key} /></Suspense>
          </BarePage.Provider>
        )}
        {tab === "runs" && <RunsScreen hub={hub} workflow={rest[0]} run={rest[1]} embedded={embedded} />}
        {tab === "work" && <WorkItemsScreen hubs={[hub]} selected={rest[0] ? { hub: hub.key, item: rest[0] } : undefined} />}
        {tab === "sandboxes" && <SandboxesScreen hubs={[hub]} />}
        {tab === "evals" && <EvalsScreen hub={hub} run={rest[0]} embedded={embedded} />}
        {tab === "config" && (
          // One page with headings: what the agent declares, its secrets, its channels.
          <div className="hz-config-page">
            <AgentSetup hub={hub} />
            <AgentSecrets hub={hub} />
            <AgentChannels hub={hub} rest={[]} />
          </div>
        )}
        {tab === "channels" && <AgentChannels hub={hub} rest={rest} />}
        {tab === "intake" && <AgentIntake hub={hub} rest={rest} />}
        {tab === "feedback" && <AgentFeedback hub={hub} />}
      </div>
      </UnderTabs.Provider>
      </AgentEditing.Provider>
    </>
  );
}
