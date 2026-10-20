import { Suspense, useContext, type ReactNode } from "react";
import { Breadcrumb, Spin } from "antd";
import { ArrowUpRight, BookOpen, CalendarClock, FlaskConical, Folder, MessageSquare, UsersRound } from "lucide-react";
import { query, type EvalScore, type Hub, type Me, type Summary, type Workflow } from "../api";
import { agentHref, href } from "../hooks/useRoute";
import { useData } from "../hooks/useData";
import { lazyChunk } from "../lib/lazyChunk";
import { AppContext } from "../app/lib/app-context";
import { formatRelative } from "../app/lib/format";
import { AgentAvatar } from "../app/components/ui";
import { BarePage } from "../app/pages/PageFrame";
import { AccessEditor } from "./access/AccessEditor";
import { RunsScreen } from "./RunsScreen";
import { ActivityScreen } from "./ActivityScreen";
import { EvalsScreen } from "./EvalsScreen";
import { AgentConnectors } from "./ConnectorsScreen";
import { AgentSecrets } from "./SecretsScreen";
import { AgentChannels } from "./ChannelsScreen";
import { AgentIntake } from "./IntakeScreen";
import { AgentSetup } from "./SetupScreen";
import { WorkItemsScreen } from "./WorkItemsScreen";
import { agentName } from "./AgentsScreen";
import { SandboxesScreen } from "./SandboxesScreen";

const WorkspacePage = lazyChunk(() => import("../app/pages/WorkspacePage"));
const HubChangesPage = lazyChunk(() => import("../app/pages/HubChangesPage"));

export type AgentTab =
  | "overview" | "access" | "connectors" | "instructions" | "changes" | "runs" | "work" | "sandboxes"
  | "evals" | "setup" | "secrets" | "channels" | "intake" | "activity";

/** The agent page's sections: a few tabs, each holding its related parts as
 *  sub-tabs (a nested view), instead of one tab per screen. */
const SECTIONS: { key: AgentTab; label: string; parts?: [AgentTab, string][] }[] = [
  { key: "overview", label: "Overview" },
  { key: "access", label: "Access" },
  { key: "connectors", label: "Connectors" },
  { key: "instructions", label: "Instructions", parts: [["instructions", "Files"], ["changes", "Changes to review"]] },
  { key: "runs", label: "Runs & schedules", parts: [["runs", "Workflows"], ["work", "Work items"], ["sandboxes", "Sandboxes"]] },
  { key: "evals", label: "Quality" },
  { key: "setup", label: "Setup", parts: [["setup", "Settings"], ["secrets", "Secrets"], ["channels", "Channels"], ["intake", "Intake"]] },
  { key: "activity", label: "Activity" },
];
export const AGENT_TABS: AgentTab[] = SECTIONS.flatMap((s) => (s.parts ? s.parts.map(([k]) => k) : [s.key]));
const sectionOf = (tab: AgentTab) => SECTIONS.find((s) => s.key === tab || s.parts?.some(([k]) => k === tab)) ?? SECTIONS[0];

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
  /** Inside the chat app's Settings. */
  embedded?: boolean;
}) {
  const app = useContext(AppContext);
  const agent = app?.agents.list.find((a) => a.hub === hub.key);
  const name = agentName(agent?.name, hub.name, hub.key);
  const section = sectionOf(tab);
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
            <a key={key} href={agentHref(hub.key, key)} aria-current={tab === key ? "page" : undefined}>{label}</a>
          ))}
        </nav>
      )}
      <div key={`${hub.key}:${tab}`}>
        {tab === "overview" && <Overview hub={hub} />}
        {tab === "access" && <AccessEditor hub={hub} embedded={embedded} />}
        {tab === "connectors" && <AgentConnectors hub={hub} me={me} embedded={embedded} />}
        {tab === "instructions" && (
          <BarePage.Provider value>
            <Suspense fallback={<Spin />}><WorkspacePage agent={hub.key} /></Suspense>
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
        {tab === "setup" && <AgentSetup hub={hub} />}
        {tab === "secrets" && <AgentSecrets hub={hub} />}
        {tab === "channels" && <AgentChannels hub={hub} rest={rest} />}
        {tab === "intake" && <AgentIntake hub={hub} rest={rest} />}
        {tab === "activity" && <ActivityScreen hubs={hubs} hub={hub} />}
      </div>
    </>
  );
}

/** What this agent is and how it is used, from the records the other tabs
 *  show in full. Each fact opens the tab that manages it. */
function Overview({ hub }: { hub: Hub }) {
  const summary = useData<Summary>("/summary?period=30d", { keepStale: true });
  const workflows = useData<{ workflows: Workflow[] }>("/workflows" + query({ hub: hub.key }));
  const evals = useData<{ cases: number; runs: number; latest: EvalScore | null }>("/evals/summary" + query({ hub: hub.key }));
  const s = summary.data?.hubs.find((h) => h.key === hub.key);
  const flows = workflows.data?.workflows ?? [];
  const scheduled = flows.filter((w) => w.schedule).length;
  const failing = flows.filter((w) => w.state === "error" || w.state === "stale").length;
  const e = evals.data;
  return (
    <div className="hz-overview">
      <Fact icon={<Folder size={16} aria-hidden />} label="Folder" to={agentHref(hub.key, "instructions")}
        value={<code>{hub.path || hub.key}</code>} note="Instructions, knowledge, skills and tools live here." />
      <Fact icon={<UsersRound size={16} aria-hidden />} label="Who can use it" to={agentHref(hub.key, "access")}
        value={!s ? "…" : s.everyone ? "Everyone signed in" : `${s.users_with_access} ${s.users_with_access === 1 ? "person" : "people"}`}
        note={s ? `${s.active_users} active in the last 30 days` : undefined} />
      <Fact icon={<MessageSquare size={16} aria-hidden />} label="Chats, last 30 days" to={agentHref(hub.key, "activity")}
        value={!s ? "…" : `${s.chats.toLocaleString()} ${s.chats === 1 ? "chat" : "chats"}`}
        note={s?.last_activity ? `Last used ${formatRelative(s.last_activity)}` : "Not used yet"} />
      <Fact icon={<CalendarClock size={16} aria-hidden />} label="Workflows" to={agentHref(hub.key, "runs")}
        value={!workflows.data ? "…" : flows.length === 0 ? "None" : `${flows.length} · ${scheduled} on a schedule`}
        note={failing ? `${failing} need attention` : flows.length ? "All running normally" : "Add one in the workflows folder."}
        tone={failing ? "bad" : undefined} />
      <Fact icon={<FlaskConical size={16} aria-hidden />} label="Quality checks" to={agentHref(hub.key, "evals")}
        value={!e ? "…" : e.cases === 0 ? "No evals yet" : e.latest ? `${e.latest.passed} of ${e.latest.total} passed` : `${e.cases} cases, not run`}
        tone={e?.latest && e.latest.passed < e.latest.total ? "bad" : undefined} />
      <Fact icon={<BookOpen size={16} aria-hidden />} label="Model" to={agentHref(hub.key, "setup")} value={hub.model_id || "Default"} />
    </div>
  );
}

function Fact({ icon, label, value, note, to, tone }: {
  icon: ReactNode; label: string; value: ReactNode; note?: string; to: string; tone?: "bad";
}) {
  return (
    <a className="hz-fact" href={to}>
      <span className="hz-fact-icon">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="hz-fact-label">{label}</span>
        <span className="hz-fact-value">{value}</span>
        {note && <span className={tone === "bad" ? "hz-fact-note is-bad" : "hz-fact-note"}>{note}</span>}
      </span>
      <ArrowUpRight size={14} aria-hidden className="hz-fact-go" />
    </a>
  );
}
