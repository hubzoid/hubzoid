// "What you can use": the agents open to the signed-in person, each opening a
// chat, and the chat features they have, from the one access check
// (GET /api/me/access). What each agent allows in detail is for administrators
// (People, Access); here a person sees where to go (founder review).
import { displayName } from "../lib/format";
import { useEffect, useState } from "react";
import { t } from "../i18n/en";
import { get } from "../lib/api";
import { describeError } from "../lib/errors";
import { useApp } from "../lib/app-context";
import { AgentAvatar, Notice, Spinner } from "./ui";

type Access = {
  agents: { hub: string; name: string; extra: { permission: string; label: string }[] }[];
  features: { key: string; label: string; description?: string; on: boolean }[];
  contact: string;
  via_key: boolean;
};

const M = t.myAccess;

export function MyAccess() {
  const app = useApp();
  const [data, setData] = useState<Access | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    get<Access>("/api/me/access").then(setData).catch((e) => setError(describeError(e)));
  }, []);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <Spinner size={16} />;
  const on = data.features.filter((f) => f.on);
  return (
    <div className="hz-my-access text-[13.5px]" data-testid="my-access">
      <div>
        <div className="hz-label">{M.agents}</div>
        {data.agents.length === 0 ? (
          <p className="m-0 text-mute">{M.none}</p>
        ) : (
          <ul className="hz-my-list">
            {data.agents.map((a) => {
              const agent = app.agents.list.find((x) => x.hub === a.hub);
              const name = displayName(agent?.name ?? a.name);
              const manages = a.extra.some((x) => x.permission === "manage_access");
              const body = (
                <>
                  <AgentAvatar name={name} src={agent?.avatar_url} size={28} />
                  <span className="hz-my-agent-text">
                    <span className="hz-my-agent hz-row-name">{name}</span>
                    {agent?.description && <span className="hz-my-agent-desc">{agent.description}</span>}
                  </span>
                  {manages && <span className="hz-my-cap is-manage">{M.manager}</span>}
                </>
              );
              return (
                <li key={a.hub}>
                  {agent ? (
                    <a className="hz-my-agent-row hz-row-link" href={`/new/${encodeURIComponent(agent.id)}`}
                      title={agent.description ? `${name}: ${agent.description}` : M.open(name)}>
                      {body}
                    </a>
                  ) : (
                    <div className="hz-my-agent-row">{body}</div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <p className="m-0 text-[13px] text-secondary">
        {on.length ? M.featuresOn(on.map((f) => f.label).join(", ")) : M.noFeatures}
      </p>
      <p className="m-0 text-[12.5px] text-mute">{M.ask(data.contact)}</p>
    </div>
  );
}
