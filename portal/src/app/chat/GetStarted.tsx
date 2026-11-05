// The agent's Get started cards on the empty chat, for the people who manage
// it (GET /api/hubs/<key>/get-started; only agents with a get-started.yaml
// say `get_started`). They load after the screen, so the composer and the
// suggestions never wait, and nothing shows until the first answer. A 403 or
// 404 shows nothing and isn't asked again on this page, a dropped connection
// fails quietly, and an invalid file is one short notice.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { CircleHelp, RefreshCw } from "lucide-react";
import { ApiError, enc, get, isAbort } from "../lib/api";
import type { Agent, GetStarted as Answer, GetStartedCard } from "../lib/types";
import { refusedCards as refused, shownCards as shown } from "../lib/getStarted";
import { t } from "../i18n/en";
import { Button, Notice } from "../components/ui";

function readHidden(key: string): boolean {
  try {
    return localStorage.getItem(key) === "hidden";
  } catch {
    return false;
  }
}

function writeHidden(key: string, hidden: boolean) {
  try {
    if (hidden) localStorage.setItem(key, "hidden");
    else localStorage.removeItem(key);
  } catch {
    /* no storage: hidden for this page only */
  }
}

/** Recovery text: what is in `backticks` is a command, in monospace; the rest
 *  is plain text. */
function fixParts(text: string): ReactNode[] {
  const parts = text.split("`");
  // An even count means one backtick has no pair: it stays in the text.
  if (parts.length % 2 === 0) {
    const tail = parts.pop();
    parts[parts.length - 1] += `\`${tail}`;
  }
  return parts.map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

function Badge({ card, checking }: { card: GetStartedCard; checking: boolean }) {
  if (card.status == null) return null;
  if (checking) return <span className="hz-pill">{t.getStarted.checking}</span>;
  if (card.status === "fail") return <span className="hz-pill hz-pill-failed">{t.getStarted.needsYou}</span>;
  if (card.status === "warn") return <span className="hz-pill hz-pill-check">{t.getStarted.check}</span>;
  return <span className="hz-pill hz-pill-ready">{t.getStarted.ready}</span>;
}

function Help({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className="hz-icon-btn hz-gs-help" aria-label={label}>
          <CircleHelp size={14} aria-hidden />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="hz-menu hz-gs-fix"
          side="top"
          align="end"
          sideOffset={6}
          collisionPadding={12}
          aria-label={label}
        >
          {children}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function Card({ card, checking }: { card: GetStartedCard; checking: boolean }) {
  return (
    <li className="hz-gs-card" data-status={card.status ?? "static"}>
      <div className="hz-gs-card-head">
        <h3 className="hz-gs-card-title" title={card.title}>
          {card.title}
        </h3>
        {card.fix && <Help label={t.getStarted.howToFix(card.title)}>{fixParts(card.fix)}</Help>}
        <Badge card={card} checking={checking} />
      </div>
      {card.line && <p className="hz-gs-line" title={card.line}>{card.line}</p>}
      {card.detail && <p className="hz-gs-detail" title={card.detail}>{card.detail}</p>}
    </li>
  );
}

/** Drawn when `getStartedKey` (lib/getStarted) gives a key, and keyed by it. */
export function GetStartedCards({ agent, cardsKey }: { agent: Agent; cardsKey: string }) {
  const hub = agent.hub || agent.id;
  const hideKey = `hz.getStarted:${cardsKey}`;
  const [hidden, setHidden] = useState(() => readHidden(hideKey));
  const [answer, setAnswer] = useState<Answer | null>(() => shown.get(cardsKey) ?? null);
  const [gone, setGone] = useState(() => refused.has(cardsKey));
  /** Check again is asking. Its button stays focusable and ignores clicks meanwhile. */
  const [checking, setChecking] = useState(false);
  /** Get started was pressed before there was an answer: the link stays, with focus, until it comes. */
  const [revealing, setRevealing] = useState(false);
  const request = useRef<AbortController | null>(null);
  const showRef = useRef<HTMLButtonElement>(null);
  const hideRef = useRef<HTMLButtonElement>(null);
  const againRef = useRef<HTMLButtonElement>(null);
  /** The control that takes focus once it is drawn, after the focused one went away. */
  const focusNext = useRef<"show" | "hide" | "again" | null>(null);

  const load = (again: boolean) => {
    request.current?.abort();
    const ctl = new AbortController();
    request.current = ctl;
    if (again) setChecking(true);
    get<Answer>(`/api/hubs/${enc(hub)}/get-started${again ? "?refresh=1" : ""}`, { signal: ctl.signal, quiet401: true })
      .then((got) => {
        if (!got || !Array.isArray(got.cards)) return;
        shown.set(cardsKey, got);
        // The notice and the cards each draw their own Check again.
        if (againRef.current && againRef.current === document.activeElement) focusNext.current = "again";
        setAnswer(got);
        setRevealing(false);
      })
      .catch((e) => {
        if (isAbort(e)) return;
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
          shown.delete(cardsKey);
          refused.add(cardsKey);
          setGone(true);
        }
        // Anything else fails quietly: what is shown stays.
      })
      .finally(() => {
        if (request.current !== ctl) return;
        request.current = null;
        setChecking(false);
      });
  };

  useEffect(() => {
    if (hidden || gone) return;
    load(false);
    return () => request.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cardsKey, hidden, gone]);

  // Hide, Get started, and an answer that swaps the notice and the cards,
  // unmount the focused control: focus moves to the one that replaces it.
  useEffect(() => {
    const next = focusNext.current;
    if (!next) return;
    const el = { show: showRef, hide: hideRef, again: againRef }[next].current;
    if (!el) return; // not drawn yet: after a later render
    focusNext.current = null;
    if (document.activeElement !== el) el.focus();
  });

  if (gone) return null;
  const show = () => {
    focusNext.current = "hide";
    if (hidden) {
      writeHidden(hideKey, false);
      setRevealing(!answer);
      setHidden(false);
    } else if (!request.current) {
      load(false); // the last ask failed quietly: ask again
    }
  };
  if (hidden || revealing)
    return (
      <button ref={showRef} type="button" className="hz-gs-show" onClick={show}>
        {t.getStarted.show}
      </button>
    );
  // Nothing until the first answer, so a slow 403 never flashes a heading.
  if (!answer) return null;

  const hide = () => {
    writeHidden(hideKey, true);
    focusNext.current = "show";
    setHidden(true);
  };
  const actions = (
    <span className="hz-gs-acts">
      <Button
        ref={againRef}
        variant="ghost"
        size="sm"
        icon={<RefreshCw size={13} aria-hidden />}
        aria-disabled={checking || undefined}
        onClick={() => {
          if (!checking) load(true);
        }}
      >
        {t.getStarted.checkAgain}
      </Button>
      <Button ref={hideRef} variant="ghost" size="sm" aria-label={t.getStarted.hideLabel} onClick={hide}>
        {t.getStarted.hide}
      </Button>
    </span>
  );

  if (answer.error)
    return (
      <Notice tone="warning" className="hz-gs-notice">
        <span className="hz-gs-notice-row">
          <span className="inline-flex items-center gap-1">
            {t.getStarted.invalid}
            <Help label={t.getStarted.why}>{answer.error}</Help>
          </span>
          {actions}
        </span>
      </Notice>
    );

  const cards = answer.cards;
  if (cards.length === 0) return null;
  const need = cards.filter((c) => c.status === "warn" || c.status === "fail").length;
  const status = checking ? t.getStarted.checking : need ? t.getStarted.needYou(need) : t.getStarted.allSet;
  const title = answer.title || t.getStarted.title;
  return (
    <section className="hz-gs" aria-label={title} aria-busy={checking || undefined}>
      <div className="hz-gs-head">
        <h2 className="hz-gs-title">{title}</h2>
        <span className="hz-gs-status" data-tone={checking ? "busy" : need ? "warn" : "ok"} aria-live="polite">
          {status}
        </span>
        {actions}
      </div>
      <ul className="hz-gs-grid">
        {cards.map((card) => (
          <Card key={card.id} card={card} checking={checking} />
        ))}
      </ul>
    </section>
  );
}
