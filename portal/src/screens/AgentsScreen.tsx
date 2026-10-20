import { useContext, useState, type ReactNode } from "react";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar as AppAvatar } from "../app/components/ui";
import { displayName, formatRelative } from "../app/lib/format";

/** The chat app's name for an agent, unless it is only the folder name. */
export function agentName(chatName: string | undefined, hubName: string, key: string): string {
  if (chatName && chatName !== key) return chatName;
  return displayName(hubName) || hubName || key;
}
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

/** Evals as a dot and a word on an agent card. Nothing without eval cases. */
function EvalStatus({ hub }: { hub: Hub }) {
  const data = useData<{ cases: number; runs: number; latest: EvalScore | null }>(
    "/evals/summary?hub=" + encodeURIComponent(hub.key),
  );
  const d = data.data;
  if (!d || d.cases === 0) return null;
  const ok = !!d.latest && d.latest.passed === d.latest.total;
  return (
    <a href={agentHref(hub.key, "evals")} className={`hz-status ${!d.latest ? "is-muted" : ok ? "is-ok" : "is-bad"} hz-agent-card-act`}>
      <i aria-hidden />
      {d.latest ? `Evals ${d.latest.passed}/${d.latest.total}` : "Evals not run"}
    </a>
  );
}

/** Agents as a grid of cards: who it is, what it is for, and how it is used.
 *  The whole card opens the agent; Chat opens a new chat with it. */
export function AgentGrid({ hubs, workflows, usage }: { hubs: Hub[]; workflows?: Workflow[]; usage?: SummaryHub[] }) {
  const app = useContext(AppContext);
  const [search, setSearch] = useState("");
  const q = search.trim().toLowerCase();
  const named = hubs.map((h) => {
    const agent = app?.agents.list.find((a) => a.hub === h.key);
    return { hub: h, agent, name: agentName(agent?.name, h.name, h.key) };
  });
  const list = named.filter(({ hub, name, agent }) =>
    !q || `${name} ${hub.key} ${agent?.description ?? ""}`.toLowerCase().includes(q));
  return (
    <>
      {hubs.length > 6 && (
        <label className="hz-set-find hz-agents-find">
          <Search size={14} aria-hidden className="text-mute" />
          <input aria-label="Find an agent" placeholder="Find an agent" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      )}
      {list.length === 0 ? (
        <div className="hz-inbox-none">
          <strong>{q ? `No agent matches “${search}”` : "No agents yet"}</strong>
          <p>{q ? "Try another name." : "Add an agent folder to this deployment, or ask an administrator for access."}</p>
        </div>
      ) : (
        <div className="hz-agent-grid" role="list" aria-label="Agents">
          {list.map(({ hub: h, agent, name }) => {
            const flows = (workflows ?? []).filter((w) => w.hub === h.key);
            const scheduled = flows.filter((w) => w.schedule).length;
            const broken = flows.some((w) => w.state === "error" || w.state === "stale");
            const m = usage?.find((row) => row.key === h.key);
            return (
              <div key={h.key} className="hz-agent-card" role="listitem">
                <a href={agentHref(h.key)} className="hz-agent-card-link" aria-label={`Open ${name}`} />
                <div className="hz-agent-card-head">
                  <AppAvatar name={name} src={agent?.avatar_url} size={40} />
                  <div className="min-w-0 flex-1">
                    <div className="hz-agent-card-name">{name}</div>
                    <div className="hz-agent-card-key">{h.key}</div>
                  </div>
                  {h.can_chat && agent && (
                    <a className="hz-btn hz-btn-secondary hz-btn-sm hz-agent-card-act" href={`/new/${encodeURIComponent(agent.id)}`}>Chat</a>
                  )}
                </div>
                <p className="hz-agent-card-desc">{agent?.description || "No description yet."}</p>
                <dl className="hz-agent-card-facts">
                  <div><dt>People</dt><dd>{!m ? "—" : m.everyone ? "Everyone" : m.users_with_access}</dd></div>
                  <div><dt>Workflows</dt><dd title={scheduled ? `${scheduled} on a schedule` : undefined}>{workflows ? (flows.length || "None") : "—"}</dd></div>
                  <div><dt>Last used</dt><dd>{m?.last_activity ? formatRelative(m.last_activity) : "Not yet"}</dd></div>
                </dl>
                <div className="hz-agent-card-foot">
                  {broken ? <span className="hz-status is-bad"><i aria-hidden />A workflow needs attention</span>
                    : <span className="hz-status is-ok"><i aria-hidden />Ready</span>}
                  <EvalStatus hub={h} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
