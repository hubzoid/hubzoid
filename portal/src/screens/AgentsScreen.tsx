import { useState, type ReactNode } from "react";
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
