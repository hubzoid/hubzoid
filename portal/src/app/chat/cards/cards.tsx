// Cards (MCP Apps) in native chat. A tool part whose result carries
// `_meta["hubzoid/card"]` is shown as that card: assistant-ui's McpAppRenderer
// draws the frame, McpAppsRemoteHost sends the frame's requests to this hub's
// card host route, and the server rechecks every one of them. Around the frame
// this file draws the host's own chrome, which the card cannot change: the
// tool, that it is a card, its live state and the "Review in Hub" link.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import {
  AuiConfig,
  McpAppRenderer,
  McpAppsRemoteHost,
  Tools,
  useAuiState,
  type ToolCallMessagePartComponent,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import { ExternalLink, LayoutPanelTop } from "lucide-react";
import { t } from "../../i18n/en";
import { setCardApiBase } from "./sandboxFrame";

export type CardMeta = { instance_id: string; card_id: string; kind: string; tool?: string; resource_uri: string };

/** The card a tool result shows, if any. */
export function cardOf(result: unknown): CardMeta | null {
  const meta = (result as { _meta?: Record<string, unknown> } | null)?._meta?.["hubzoid/card"];
  if (!meta || typeof meta !== "object") return null;
  const m = meta as Partial<CardMeta>;
  return typeof m.instance_id === "string" && typeof m.resource_uri === "string" && m.resource_uri.startsWith("ui://")
    ? (m as CardMeta)
    : null;
}

export function cardText(result: unknown): string {
  const content = (result as { content?: { type?: string; text?: string }[] } | null)?.content;
  return (content ?? []).filter((c) => c?.type === "text" && typeof c.text === "string").map((c) => c.text).join("\n");
}

/** Where this thread's cards are served, and whether they may be live. */
export const CardsContext = createContext<{ hub: string | null; apiBase: string; live: boolean }>({
  hub: null,
  apiBase: "",
  live: false,
});

/** Links a card asks to open: this site's pages in place, https elsewhere
 *  after the person confirms. Nothing else (no other schemes). */
function openLink({ url }: { url: string }) {
  let target: URL;
  try {
    target = new URL(url, location.origin);
  } catch {
    return { isError: true };
  }
  if (target.origin === location.origin) {
    location.assign(target.href);
    return {};
  }
  if (target.protocol !== "https:") return { isError: true };
  if (window.confirm(t.cards.openExternal(target.host))) window.open(target.href, "_blank", "noopener,noreferrer");
  return {};
}

/** The scopes ChatPage adds to the runtime: the MCP App renderer for cards. */
export function cardsConfig(apiBase: string, hub: string, isDark: boolean) {
  setCardApiBase(apiBase);
  return AuiConfig({
    tools: Tools({
      mcpApp: McpAppRenderer({
        host: McpAppsRemoteHost({ url: `${apiBase}/portal/api/hubs/${encodeURIComponent(hub)}/apps/host` }),
        hostInfo: { name: "Hubzoid", version: "2.0" },
        hostContext: { theme: isDark ? "dark" : "light", displayMode: "inline", availableDisplayModes: ["inline"] },
        sandbox: { product: "hubzoid-card" },
        maxHeight: 640,
        handlers: {
          openLink,
          // A built-in card never writes into the conversation.
          sendMessage: () => ({ isError: true, ok: false, reason: "Cards cannot send messages here." }),
        },
        forPart: (part) => {
          const card = cardOf(part.result);
          const text = cardText(part.result);
          return {
            handlers: { allowedTools: card?.tool ? [card.tool, "hub_app_status"] : ["hub_app_status"] },
            loadingFallback: <CardText text={text} note={t.cards.loading} />,
            errorFallback: (error: Error) => <CardText text={text} note={t.cards.failed(error.message)} />,
          };
        },
      }),
    }),
  });
}

function CardText({ text, note }: { text: string; note: string }) {
  return (
    <div className="hz-card-text" data-testid="card-text">
      <p className="m-0 mb-1 text-[12px] text-mute" role="status">{note}</p>
      <pre className="m-0 whitespace-pre-wrap font-sans text-[13px]">{text}</pre>
    </div>
  );
}

type Status = { state: string; label: string; link?: string; facts?: Record<string, unknown> };
type InstanceView = { live: boolean; inactive_reason: string | null; status?: Status; revoked: number | null };

/** The host chrome's own read of the card: is it live, and the authoritative
 *  state of the work or approval it names. */
function useInstance(apiBase: string, hub: string | null, id: string, kind: string): InstanceView | null {
  const [view, setView] = useState<InstanceView | null>(null);
  useEffect(() => {
    if (!hub) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const r = await fetch(`${apiBase}/portal/api/hubs/${encodeURIComponent(hub)}/apps/instances/${encodeURIComponent(id)}`,
          { credentials: "include" });
        if (!r.ok) throw new Error(String(r.status));
        const v = (await r.json()) as InstanceView;
        if (stop) return;
        setView(v);
        const terminal = !v.status || ["completed", "failed", "cancelled", "stopped", "succeeded", "rejected",
          "failed_before_send", "resolved_done", "abandoned", "changed", "unavailable"].includes(v.status.state);
        if ((kind === "progress" || kind === "approval") && !terminal && !document.hidden) timer = setTimeout(load, 5000);
      } catch {
        if (!stop) setView({ live: false, inactive_reason: "unavailable", revoked: null });
      }
    };
    void load();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [apiBase, hub, id, kind]);
  return view;
}

export function CardEntry(props: ToolCallMessagePartProps & { header: ReactNode }) {
  const { result } = props;
  const card = cardOf(result)!;
  const text = cardText(result);
  const ctx = useContext(CardsContext);
  const Render = useAuiState((s) => (s as { tools?: { mcpApp?: { render: ToolCallMessagePartComponent } } }).tools?.mcpApp?.render);
  const view = useInstance(ctx.apiBase, ctx.live ? ctx.hub : null, card.instance_id, card.kind);
  // An older version still shows (its HTML is kept); the server refuses its
  // calls. Revoked, removed or disabled cards show their text.
  const live = ctx.live && !!Render && (view === null || view.live || view.inactive_reason === "resource_changed");
  const status = view?.status;
  return (
    <div className="hz-card" data-testid="card" data-card-kind={card.kind} data-card-live={live}>
      {props.header}
      <div className="hz-card-chrome" data-testid="card-chrome">
        <LayoutPanelTop size={13} aria-hidden className="flex-none text-mute" />
        <span className="text-[12px] text-mute">{t.cards.label(card.kind)}</span>
        {status && <span className="hz-tag is-quiet" data-testid="card-state">{status.label}</span>}
        {status?.link && (card.kind === "approval" || card.kind === "progress") && (
          <a className="hz-card-review" href={status.link} data-testid="card-review">
            {card.kind === "approval" ? t.cards.review : t.cards.open}
            <ExternalLink size={12} aria-hidden />
          </a>
        )}
      </div>
      {live && Render ? (
        <div className="hz-card-frame">
          <Render {...props} mcp={{ app: { resourceUri: card.resource_uri, serverId: card.instance_id } }} />
        </div>
      ) : (
        <CardText text={text} note={ctx.live ? t.cards.inactive(view?.inactive_reason ?? "") : t.cards.passive} />
      )}
    </div>
  );
}
