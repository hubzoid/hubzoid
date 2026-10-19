// "What you can use": the agents and chat features open to the signed-in
// person, from the one access check (GET /api/me/access). Read only.
import { displayName } from "../lib/format";
import { useEffect, useState } from "react";
import { t } from "../i18n/en";
import { get } from "../lib/api";
import { describeError } from "../lib/errors";
import { Notice, Spinner } from "./ui";

type Access = {
  agents: { hub: string; name: string; extra: { permission: string; label: string }[] }[];
  features: { key: string; label: string; on: boolean }[];
  contact: string;
  via_key: boolean;
};

const M = t.myAccess;

export function MyAccess() {
  const [data, setData] = useState<Access | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    get<Access>("/api/me/access").then(setData).catch((e) => setError(describeError(e)));
  }, []);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <Spinner size={16} />;
  return (
    <div className="space-y-4 text-[13.5px]" data-testid="my-access">
      <div>
        <div className="hz-label">{M.agents}</div>
        {data.agents.length === 0 ? (
          <p className="m-0 text-mute">{M.none}</p>
        ) : (
          <ul className="m-0 list-none space-y-1 p-0">
            {data.agents.map((a) => (
              <li key={a.hub} className="text-ink">
                {displayName(a.name)}
                {a.extra.length > 0 && <span className="text-mute"> · {M.also(a.extra.map((x) => x.label).join(", "))}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <div className="hz-label">{M.features}</div>
        <ul className="m-0 grid list-none grid-cols-2 gap-x-4 gap-y-1 p-0 sm:grid-cols-[max-content_max-content]">
          {data.features.map((f) => (
            <li key={f.key} className="contents">
              <span className="text-ink">{f.label}</span>
              <span className={f.on ? "text-success" : "text-mute"}>{f.on ? M.on : M.off}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="m-0 text-[12.5px] text-mute">{M.ask(data.contact)}</p>
    </div>
  );
}
