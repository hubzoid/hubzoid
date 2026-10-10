// Shapes of the Hubzoid web app API (docs/design/hubzoid-app.md, section 6).
// Fields the contract marks optional, or that a server might not send yet, are
// optional here so the UI degrades instead of crashing.

export type Role = "admin" | "user";

export type SessionUser = {
  id: string;
  email: string;
  name: string;
  role: Role | string;
  /** True when the Console opens for this person (Manage access on an agent or more). */
  console?: boolean;
};

export type Provider = { id: string; name: string };

export type Session = {
  authenticated: boolean;
  mode: "local" | "accounts" | string;
  user?: SessionUser | null;
  providers?: Provider[];
  password?: boolean;
  signup?: boolean;
  branding_name?: string;
};

export type Agent = {
  id: string;
  name: string;
  description?: string;
  suggestions?: (string | { title?: string; prompt?: string; text?: string })[];
  avatar_url?: string | null;
  hub?: string;
  api_base?: string;
};

export type AgentsResponse = { agents: Agent[]; default_agent?: string | null };

export type Branding = {
  name?: string | null;
  logo_url?: string | null;
  favicon_url?: string | null;
  custom_css_url?: string | null;
};

/** Timestamps arrive as epoch seconds (the store keeps floats) or ISO strings. */
export type Timestamp = number | string | null | undefined;

export type Conversation = {
  id: string;
  title?: string | null;
  agent: string;
  hub?: string;
  api_base?: string;
  archived?: boolean | number;
  created_at?: Timestamp;
  updated_at?: Timestamp;
};

export type ConversationPage = { items: Conversation[]; next_cursor?: string | null };

export type FileRef = {
  file_id: string;
  name: string;
  mime: string;
  size: number;
  kind?: "image" | "file";
};

// Message content parts as stored and returned (contract 6.4).
export type TextPart = { type: "text"; text: string };
export type ReasoningPart = { type: "reasoning"; text: string };
export type ToolCallPart = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  args?: unknown;
  result?: unknown;
  isError?: boolean;
};
export type FilePart = { type: "file" | "image"; file_id: string; name: string; mime: string; size: number };
export type ServerPart = TextPart | ReasoningPart | ToolCallPart | FilePart | { type: string; [k: string]: unknown };

export type MessageStatus = "complete" | "running" | "cancelled" | "error";

export type ServerMessage = {
  id: string;
  parent_id: string | null;
  role: "user" | "assistant" | string;
  content: ServerPart[] | string;
  status?: MessageStatus | string;
  created_at?: Timestamp;
  error?: string | null;
};

export type ConversationDetail = {
  conversation: Conversation;
  messages: ServerMessage[];
  head_id?: string | null;
};

export type RunStatus = { status: MessageStatus | string; message?: ServerMessage | null };

export type ShareLink = { share_id: string; url?: string };

export type SharedConversation = {
  title?: string | null;
  agent?: string | null;
  owner_name?: string | null;
  created_at?: Timestamp;
  messages: ServerMessage[];
};

export type Connection = {
  connector_id: string;
  name: string;
  connected: boolean;
  status?: string | null;
  connected_at?: Timestamp;
  allowed?: boolean;
};

export type AuthLink = { valid: boolean; purpose?: string; email?: string };
