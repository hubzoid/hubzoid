import { useContext, useState, type ReactNode } from "react";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar as AppAvatar } from "../app/components/ui";
import { formatRelative } from "../app/lib/format";
import { agentName } from "../lib/agentName";
import { Button, Card, Empty, Input, Tag, Typography } from "antd";
import { ChevronRight, Search } from "lucide-react";
import { type EvalScore, type Hub, type SummaryHub, type Workflow } from "../api";
import { useData } from "../hooks/useData";
import { agentHref } from "../hooks/useRoute";
import { AgentAvatar } from "../components/common";

import { Cost } from "../components/UsageCost";

import { short } from "../lib/format";

const { Text } = Typography;

/** A status tag on an agent card that opens one of the agent's tabs. Sits
 *  above the card's own link, with a › so it reads as a link without hover. */
function StatusLink({ href, label, color, children }: { href: string; label: string; color?: string; children: ReactNode }) {
  return (
    <a href={href} className="agent-card-status" aria-label={label}>
      <Tag color={color}>
        {children}
        <ChevronRight size={12} aria-hidden />
      </Tag>
    </a>
  );
}

/** The agent's latest eval score, linking to its Evals tab. Nothing for an
 *  agent without eval cases, or while the count can't be read. Counts only. */
function EvalBadge({ hub }: { hub: Hub }) {
  const data = useData<{ cases: number; runs: number; latest: EvalScore | null }>(
    "/evals/summary?hub=" + encodeURIComponent(hub.key),
  );
  const d = data.data;
  if (!d || d.cases === 0) return null;
  const href = agentHref(hub.key, "evals");
  if (!d.latest) {
    return <StatusLink href={href} label={`${hub.name}: evals not run yet`}>Evals not run</StatusLink>;
  }
  const { passed, total } = d.latest;
  return (
    <StatusLink href={href} label={`${hub.name}: evals, ${passed} of ${total} passed`}
      color={passed === total ? "green" : "red"}>
      Evals {passed}/{total}
    </StatusLink>
  );
}

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
        >
          {search && <Button onClick={() => setSearch("")}>Clear search</Button>}
        </Empty>
      ) : (
        <div className="agent-grid" role="list" aria-label="Agents">
          {list.map((h) => {
            const flows = byHub(h.key);
            const metrics = usage?.find((row) => row.key === h.key);
            const scheduled = flows.filter((w) => w.schedule).length;
            const stale = flows.some((w) => w.state === "stale");
            const broken = flows.some((w) => w.state === "error");
            const runs = agentHref(h.key, "runs");
            const status = stale ? "Scheduler stopped"
              : broken ? "Workflow error"
              : scheduled ? `${flows.length} ${flows.length === 1 ? "workflow" : "workflows"} · ${scheduled} with a schedule`
              : flows.length ? `${flows.length} manual ${flows.length === 1 ? "workflow" : "workflows"}`
              : "No workflows";
            // The whole card opens the agent: its name is the one link,
            // stretched over the card. The status tags sit above it.
            return (
              <Card key={h.key} className="agent-card" role="listitem">
                <div className="agent-card-head">
                  <AgentAvatar size={44} />
                  <div className="agent-card-title">
                    <a href={agentHref(h.key)} className="agent-name agent-card-link">
                      {h.name}
                    </a>
                  </div>
                  <ChevronRight className="agent-card-chevron" size={20} aria-hidden />
                </div>
                <div className="agent-card-tags">
                  {workflows && (
                    <StatusLink href={runs} label={`${h.name}: ${status}`} color={stale || broken ? "red" : undefined}>
                      {status}
                    </StatusLink>
                  )}
                  <EvalBadge hub={h} />
                </div>
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
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}

/** One agent's status: a failing workflow first, then failing evals. */
function AgentStatus({ hub, broken }: { hub: Hub; broken: boolean }) {
  const data = useData<{ cases: number; runs: number; latest: EvalScore | null }>(
    "/evals/summary?hub=" + encodeURIComponent(hub.key),
  );
  if (broken) return <span className="hz-status is-bad"><i aria-hidden />Workflow failing</span>;
  const d = data.data;
  if (d?.latest && d.latest.passed < d.latest.total)
    return <span className="hz-status is-warn"><i aria-hidden />Evals {d.latest.passed} of {d.latest.total}</span>;
  return <span className="hz-status is-ok"><i aria-hidden />Ready</span>;
}

const PARTS: [keyof NonNullable<Hub["contents"]> | "workflows", string, string][] = [
  ["knowledge", "knowledge", "knowledge"],
  ["skills", "skill", "skills"],
  ["workflows", "workflow", "workflows"],
  ["subagents", "sub-agent", "sub-agents"],
  ["connectors", "connector", "connectors"],
  ["tool_files", "tool file", "tool files"],
];

/** What the agent's folder holds, as a short line: the numbers stand out. */
function FolderLine({ hub, workflows }: { hub: Hub; workflows: number | null }) {
  const c = hub.contents;
  if (!c) return <span className="hz-agent-folder is-empty">—</span>;
  const parts = PARTS.map(([key, one, many]) => {
    const n = key === "workflows" ? workflows ?? 0 : c[key];
    return n ? <span key={key}><b>{n}</b> {n === 1 ? one : many}</span> : null;
  }).filter(Boolean).slice(0, 4);
  if (!parts.length) return <span className="hz-agent-folder is-empty">Only instructions</span>;
  return <span className="hz-agent-folder">{parts}</span>;
}

type Filter = "all" | "attention" | "unused";

/** Agents as one row each: who it is, what its folder holds, who uses it and
 *  whether it is healthy. The row opens the agent; Chat starts a chat. */
export function AgentGrid({ hubs, workflows, usage }: { hubs: Hub[]; workflows?: Workflow[]; usage?: SummaryHub[] }) {
  const app = useContext(AppContext);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const q = search.trim().toLowerCase();
  const rows = hubs.map((h) => {
    const agent = app?.agents.list.find((a) => a.hub === h.key);
    const flows = (workflows ?? []).filter((w) => w.hub === h.key);
    return {
      hub: h, agent, name: agentName(agent?.name, h.name, h.key),
      flows: workflows ? flows.length : null,
      broken: flows.some((w) => w.state === "error" || w.state === "stale"),
      m: usage?.find((row) => row.key === h.key),
    };
  });
  const attention = rows.filter((r) => r.broken).length;
  const unused = rows.filter((r) => usage && !r.m?.last_activity).length;
  const list = rows.filter((r) =>
    (!q || `${r.name} ${r.hub.key}`.toLowerCase().includes(q))
    && (filter === "all" || (filter === "attention" ? r.broken : usage && !r.m?.last_activity)));
  const filters: [Filter, string, number][] = [["all", "All", rows.length], ["attention", "Needs attention", attention], ["unused", "Not used yet", unused]];
  return (
    <>
      <div className="hz-agents-bar">
        <label className="hz-set-find hz-agents-find">
          <Search size={14} aria-hidden className="text-mute" />
          <input aria-label="Find an agent" placeholder="Find an agent" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <div className="hz-agents-filters" role="group" aria-label="Show">
          {filters.filter(([key, , n]) => key === "all" || n > 0).map(([key, label, n]) => (
            <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label} <span>{n}</span>
            </button>
          ))}
        </div>
      </div>
      {list.length === 0 ? (
        <div className="hz-inbox-none">
          <strong>{q ? `No agent matches “${search}”` : "Nothing here"}</strong>
          <p>{q ? "Try another name." : "No agent fits this filter."}</p>
        </div>
      ) : (
        <div className="hz-agent-list" role="table" aria-label="Agents">
          <div className="hz-agent-row is-head" role="row">
            {/* Matches the rows' stretched link, so columns line up at every width. */}
            <span className="hz-agent-row-link" aria-hidden />
            <span role="columnheader">Agent</span>
            <span role="columnheader">In its folder</span>
            <span role="columnheader">People</span>
            <span role="columnheader">Chats, 30 days</span>
            <span role="columnheader">Spend, 30 days</span>
            <span role="columnheader">Last used</span>
            <span role="columnheader">Status</span>
            <span />
          </div>
          {list.map(({ hub: h, agent, name, flows, broken, m }) => (
            <div key={h.key} className="hz-agent-row" role="row">
              <a href={agentHref(h.key)} className="hz-agent-row-link" aria-label={`Open ${name}`} />
              <span className="hz-agent-who" role="cell">
                <AppAvatar name={name} src={agent?.avatar_url} size={40} />
                <span className="min-w-0">
                  <span className="hz-agent-name">{name}</span>
                  <span className="hz-agent-key">{h.key}</span>
                </span>
              </span>
              <span role="cell"><FolderLine hub={h} workflows={flows} /></span>
              <span role="cell" className="hz-agent-num">{!m ? "—" : m.everyone ? "Everyone" : m.users_with_access}</span>
              <span role="cell" className="hz-agent-num">{!m ? "—" : m.chats.toLocaleString()}</span>
              <span role="cell" className="hz-agent-when"><Cost usd={m?.cost_usd ?? null} unpriced={m?.unpriced ?? 0} /></span>
              <span role="cell" className={m?.last_activity ? "hz-agent-when" : "hz-agent-when is-empty"}>
                {m?.last_activity ? formatRelative(m.last_activity) : "Not yet"}
              </span>
              <span role="cell"><AgentStatus hub={h} broken={broken} /></span>
              <span role="cell" className="hz-agent-acts">
                {h.can_chat && agent && (
                  <a className="hz-btn hz-btn-secondary hz-btn-sm" href={`/new/${encodeURIComponent(agent.id)}`}>Chat</a>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
