import { createContext, useContext } from "react";
import type { Agent, Branding, Session, SessionUser } from "./types";

export type AgentsState = {
  status: "loading" | "ready" | "error";
  list: Agent[];
  defaultAgent: string | null;
  error: unknown;
  reload: () => void;
};

export type AppContextValue = {
  session: Session;
  user: SessionUser | null;
  isLocal: boolean;
  isAdmin: boolean;
  branding: Branding;
  brandName: string;
  agents: AgentsState;
  refreshSession: () => Promise<Session | null>;
  setUser: (user: SessionUser) => void;
  /** Start a fresh new-chat screen even when already on "/". */
  newChat: (agentId?: string | null) => void;
  newChatNonce: number;
  openSidebar: () => void;
};

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp outside the chat app");
  return value;
}

export function agentById(agents: Agent[], id: string | null | undefined): Agent | undefined {
  if (!id) return undefined;
  return agents.find((a) => a.id === id) ?? agents.find((a) => a.hub === id);
}
