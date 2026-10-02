// Choosing the agent for a new chat: the agent's name in the chat header opens
// a menu to search every agent, the ones this browser used recently first. One
// compact control, whether a person has two agents or twenty.
import { useEffect, useId, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { Check, ChevronDown, Search } from "lucide-react";
import { t } from "../i18n/en";
import { displayName } from "../lib/format";
import { recentAgents } from "../lib/recentAgents";
import type { Agent } from "../lib/types";
import { AgentAvatar, cx } from "../components/ui";

const byName = (a: Agent, b: Agent) => displayName(a.name).localeCompare(displayName(b.name));

/** Recent agents first (most recent at the top), then the rest by name. */
function orderAgents(agents: Agent[], recent: string[]): { recent: Agent[]; rest: Agent[] } {
  const ids = new Set(agents.map((a) => a.id));
  const recentIds = recent.filter((id) => ids.has(id));
  const first = recentIds.map((id) => agents.find((a) => a.id === id)!);
  return { recent: first, rest: agents.filter((a) => !recentIds.includes(a.id)).sort(byName) };
}

function matches(a: Agent, q: string): boolean {
  return [displayName(a.name), a.name, a.id, a.description ?? ""].some((s) => s.toLowerCase().includes(q));
}

type SwitcherProps = {
  agent: Agent;
  agents: Agent[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (agent: Agent) => void;
  /** The header's own title block, which becomes the menu's button. */
  children: ReactNode;
};

/** The header title as a menu button for the agent of a new chat. */
export function AgentSwitcher({ agent, agents, open, onOpenChange, onPick, children }: SwitcherProps) {
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={t.agents.switchLabel(displayName(agent.name))}
          className="-ml-1.5 flex min-w-0 items-center gap-2.5 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-hover data-[state=open]:bg-hover"
          data-testid="agent-switcher"
        >
          {children}
          <ChevronDown size={16} aria-hidden className="flex-none text-mute" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          collisionPadding={8}
          className="hz-menu z-[60] w-[min(400px,calc(100vw-16px))] !p-0 shadow-lg"
          onOpenAutoFocus={(e) => {
            // The search box takes focus, not the first row.
            e.preventDefault();
            (e.currentTarget as HTMLElement | null)?.querySelector<HTMLInputElement>("input")?.focus();
          }}
        >
          <AgentMenu
            agent={agent}
            agents={agents}
            onPick={(a) => {
              onPick(a);
              onOpenChange(false);
            }}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function AgentMenu({ agent, agents, onPick }: { agent: Agent; agents: Agent[]; onPick: (agent: Agent) => void }) {
  const [query, setQuery] = useState("");
  // Read when the menu opens: picking reorders nothing while it is open.
  const [recent] = useState(recentAgents);
  const q = query.trim().toLowerCase();
  const groups = useMemo(() => {
    if (q) return [{ label: null, items: agents.filter((a) => matches(a, q)).sort(byName) }];
    const ordered = orderAgents(agents, recent);
    return ordered.recent.length
      ? [
          { label: t.agents.recent, items: ordered.recent },
          { label: t.agents.all, items: ordered.rest },
        ]
      : [{ label: null, items: ordered.rest }];
  }, [agents, recent, q]);
  const flat = groups.flatMap((g) => g.items);
  // The keyboard starts on the agent already chosen; a search starts at the top.
  const [active, setActive] = useState(() => Math.max(0, flat.findIndex((a) => a.id === agent.id)));
  const listId = useId();
  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    document.getElementById(`${listId}-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, listId]);

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!flat.length) return;
      const move = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + move + flat.length) % flat.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (flat[active]) onPick(flat[active]);
    }
  };

  let index = -1;
  return (
    <div>
      <div className="relative border-b border-line p-2">
        <Search size={15} aria-hidden className="pointer-events-none absolute left-5 top-1/2 -translate-y-1/2 text-mute" />
        <input
          type="search"
          role="combobox"
          aria-expanded
          aria-controls={listId}
          aria-activedescendant={flat.length ? optionId(active) : undefined}
          aria-label={t.agents.search}
          placeholder={t.agents.searchPlaceholder(agents.length)}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          className="hz-input !min-h-9 !rounded-lg !border-transparent !bg-transparent !py-1.5 !pl-9 !text-sm !shadow-none [&::-webkit-search-cancel-button]:hidden"
        />
      </div>
      <ul id={listId} role="listbox" aria-label={t.agents.choose} className="m-0 max-h-[min(380px,60vh)] list-none overflow-y-auto p-1.5">
        {!flat.length && <li className="px-3 py-4 text-center text-[13.5px] text-mute">{t.agents.noMatch(query.trim())}</li>}
        {groups.map((g) =>
          g.items.length ? (
            <li key={g.label ?? "all"} role="presentation">
              {g.label && (
                <div role="presentation" className="px-2.5 pb-1 pt-2 text-[11.5px] font-medium text-mute">
                  {g.label}
                </div>
              )}
              <ul role="group" aria-label={g.label ?? undefined} className="m-0 list-none p-0">
                {g.items.map((a) => {
                  index += 1;
                  const i = index;
                  const selected = a.id === agent.id;
                  return (
                    <li
                      key={a.id}
                      id={optionId(i)}
                      role="option"
                      aria-selected={selected}
                      onMouseDown={(e) => e.preventDefault()}
                      onMouseMove={() => setActive(i)}
                      onClick={() => onPick(a)}
                      className={cx("flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-1.5", i === active && "bg-hover")}
                    >
                      <AgentAvatar name={a.name} src={a.avatar_url} size={26} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[14px] font-medium text-ink">{displayName(a.name)}</span>
                        {a.description && <span className="block truncate text-[12.5px] leading-snug text-mute">{a.description}</span>}
                      </span>
                      {selected && <Check size={15} aria-hidden className="flex-none text-accent-text" />}
                    </li>
                  );
                })}
              </ul>
            </li>
          ) : null,
        )}
      </ul>
    </div>
  );
}
