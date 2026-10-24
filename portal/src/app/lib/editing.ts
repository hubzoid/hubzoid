// Whether this agent's files may be changed from the browser (Beta, off by
// default: hubzoid/editing.py). Off, Settings shows the files read-only with
// where to change them instead.
import { createContext, useContext, useEffect, useState } from "react";
import { get, hubUrl } from "./api";
import { useApp } from "./app-context";

export type Editing = { editable: boolean; in_app_editing: boolean; files_from: "local" | "git" | "image"; message: string | null };

const OFF: Editing = { editable: false, in_app_editing: false, files_from: "local", message: null };
const cache = new Map<string, Promise<Editing>>();

export function useEditing(hub?: string): Editing {
  const app = useApp();
  const base = hub ? app.agents.list.find((a) => a.hub === hub)?.api_base ?? `/b/${encodeURIComponent(hub)}` : "";
  const [state, setState] = useState<Editing>(OFF);
  useEffect(() => {
    let alive = true;
    const key = base || "/";
    if (!cache.has(key)) cache.set(key, get<Editing>(hubUrl(base, "/api/editing"), { quiet401: true }).catch(() => OFF));
    void cache.get(key)!.then((e) => alive && setState(e));
    return () => {
      alive = false;
    };
  }, [base]);
  return state;
}

/** The agent page's answer, for its tabs: a screen hides controls that would
 *  write the agent's files when it is false (Codex review of 2.0.2). Outside
 *  an agent page nothing is known, so nothing is hidden. */
export const AgentEditing = createContext<Editing | null>(null);

export function useAgentEditing(): Editing {
  return useContext(AgentEditing) ?? { ...OFF, editable: true };
}
