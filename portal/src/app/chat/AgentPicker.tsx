// Choosing the agent for a new chat. A few agents show as cards; more show as a
// searchable list with the ones this browser used recently first, so twenty
// agents stay as quick to pick from as two.
import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
import { Check, Search, X } from "lucide-react";
import { t } from "../i18n/en";
import { displayName } from "../lib/format";
import { recentAgents } from "../lib/recentAgents";
import type { Agent } from "../lib/types";
import { AgentAvatar, cx } from "../components/ui";

/** Up to this many agents show as cards; more get the searchable list. */
const CARD_LIMIT = 6;

type PickerProps = { agent: Agent; agents: Agent[]; onPick: (agent: Agent) => void };

export function AgentPicker(props: PickerProps) {
  return (
    <section aria-labelledby="hz-pick-agent" className="mb-8 sm:mb-10">
      <h2 id="hz-pick-agent" className="hz-eyebrow m-0 mb-3">
        {t.agents.choose}
      </h2>
      {props.agents.length > CARD_LIMIT ? <AgentList {...props} /> : <AgentCards {...props} />}
    </section>
  );
}

function AgentCards({ agent, agents, onPick }: PickerProps) {
  return (
    <div role="radiogroup" aria-labelledby="hz-pick-agent" className="grid gap-2.5 sm:grid-cols-2">
      {agents.map((a) => {
        const selected = a.id === agent.id;
        return (
          <button
            key={a.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onPick(a)}
            onKeyDown={(e) => {
              const i = agents.findIndex((x) => x.id === a.id);
              const move = e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 0;
              if (!move) return;
              e.preventDefault();
              onPick(agents[(i + move + agents.length) % agents.length]);
              const group = e.currentTarget.parentElement;
              setTimeout(() => group?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus(), 0);
            }}
            tabIndex={selected ? 0 : -1}
            className={cx(
              "flex items-start gap-3 rounded-xl border p-3 text-left transition-colors sm:p-3.5",
              selected ? "border-accent bg-accent-soft/60" : "border-line bg-raised hover:bg-hover",
            )}
          >
            <span className="hidden sm:block">
              <AgentAvatar name={a.name} src={a.avatar_url} size={36} />
            </span>
            <span className="sm:hidden">
              <AgentAvatar name={a.name} src={a.avatar_url} size={28} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="truncate text-[14.5px] font-semibold text-ink">{displayName(a.name)}</span>
                {selected && <Check size={15} aria-hidden className="flex-none text-accent-text" />}
              </span>
              {a.description && (
                <span className="mt-0.5 line-clamp-1 text-[13px] leading-snug text-mute sm:line-clamp-2">
                  {a.description}
                </span>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}

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

function AgentList({ agent, agents, onPick }: PickerProps) {
  const [query, setQuery] = useState("");
  // Read once: picking reorders nothing until the next new chat.
  const [recent] = useState(recentAgents);
  // The keyboard starts on the agent already chosen.
  const [active, setActive] = useState(() => {
    const ordered = orderAgents(agents, recent);
    return Math.max(0, [...ordered.recent, ...ordered.rest].findIndex((a) => a.id === agent.id));
  });
  const listId = useId();
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
    } else if (e.key === "Escape" && query) {
      e.preventDefault();
      setQuery("");
      setActive(0);
    }
  };

  let index = -1;
  return (
    <div className="rounded-xl border border-line bg-raised">
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
          className="hz-input !min-h-9 !rounded-lg !py-1.5 !pl-9 !text-sm [&::-webkit-search-cancel-button]:hidden"
        />
        {query && (
          <button
            type="button"
            aria-label={t.agents.clearSearch}
            onClick={() => {
              setQuery("");
              setActive(0);
            }}
            className="absolute right-3.5 top-1/2 -translate-y-1/2 rounded-md p-1 text-mute hover:bg-hover hover:text-ink"
          >
            <X size={14} aria-hidden />
          </button>
        )}
      </div>
      <ul
        id={listId}
        role="listbox"
        aria-label={t.agents.choose}
        className="m-0 max-h-[296px] list-none overflow-y-auto p-1.5"
      >
        {!flat.length && <li className="px-3 py-4 text-center text-[13.5px] text-mute">{t.agents.noMatch(query.trim())}</li>}
        {groups.map((g) =>
          g.items.length ? (
            <li key={g.label ?? "all"} role="presentation">
              {g.label && (
                <div role="presentation" className="hz-eyebrow px-2.5 pb-1 pt-2">
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
                      className={cx(
                        "flex cursor-pointer items-center gap-3 rounded-lg px-2.5 py-2",
                        i === active && "bg-hover",
                        selected && "bg-accent-soft/60",
                      )}
                    >
                      <AgentAvatar name={a.name} src={a.avatar_url} size={28} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[14px] font-semibold text-ink">{displayName(a.name)}</span>
                        {a.description && (
                          <span className="block truncate text-[12.5px] leading-snug text-mute">{a.description}</span>
                        )}
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
