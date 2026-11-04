// The agent's Get started cards on the empty chat, for the people who manage
// it (GET /api/hubs/<key>/get-started; only agents with a get-started.yaml
// say `get_started`). They load after the screen, so the composer and the
// suggestions never wait. A 403 or 404 shows nothing, a dropped connection
// fails quietly, and an invalid file is one short notice.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Popover } from "radix-ui";
import { CircleHelp, RefreshCw } from "lucide-react";
import { ApiError, enc, get, isAbort } from "../lib/api";
import { useApp } from "../lib/app-context";
import type { Agent, GetStarted as Answer, GetStartedCard } from "../lib/types";
import { t } from "../i18n/en";
import { Button, Notice } from "../components/ui";

/** The last answer per agent on this page, so a new chat shows it at once. */
const shown = new Map<string, Answer>();

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

/** Plain recovery text with each `hubzoid …` command apart. A command runs to
 *  the end of its sentence: a word ending in . , ; or :, or a capitalised word.
 *  A lone "." is the folder (hubzoid sandbox doctor .), and only a flag
 *  (hubzoid hub configure . --check) goes on after it. Quoted is surer. */
function plainParts(text: string, key: string): ReactNode[] {
  const words = text.split(" ");
  const parts: { code: boolean; text: string }[] = [];
  const add = (code: boolean, s: string) => {
    const last = parts[parts.length - 1];
    if (!code && last && !last.code) last.text += s;
    else parts.push({ code, text: s });
  };
  for (let i = 0; i < words.length; ) {
    const space = (n: number) => (n < words.length ? " " : "");
    if (words[i] !== "hubzoid" || i + 1 >= words.length) {
      add(false, words[i] + space(i + 1));
      i++;
      continue;
    }
    let j = i + 1;
    let stop = "";
    for (; j < words.length; j++) {
      const w = words[j];
      if (/^[A-Z][a-z]/.test(w)) break;
      if (w === "." && j + 1 < words.length && !words[j + 1].startsWith("-")) {
        j++;
        break;
      }
      if (/[,;:]$/.test(w) || (w.length > 1 && w.endsWith("."))) {
        stop = w.slice(-1);
        words[j] = w.slice(0, -1);
        j++;
        break;
      }
    }
    add(true, words.slice(i, j).join(" "));
    add(false, stop + space(j));
    i = j;
  }
  return parts.map((p, n) => (p.code ? <code key={`${key}.${n}`}>{p.text}</code> : p.text));
}

/** Recovery text with its commands in monospace: `quoted` ones, and plain
 *  `hubzoid …` commands. */
function fixParts(text: string): ReactNode[] {
  return text.split("`").flatMap((part, i) => (i % 2 ? [<code key={i}>{part}</code>] : plainParts(part, String(i))));
}

function Badge({ card, checking }: { card: GetStartedCard; checking: boolean }) {
  if (card.status === null) return null;
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
        <Popover.Content className="hz-menu hz-gs-fix" side="top" align="end" sideOffset={6} aria-label={label}>
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
        <h3 className="hz-gs-card-title">{card.title}</h3>
        {card.fix && <Help label={t.getStarted.howToFix(card.title)}>{fixParts(card.fix)}</Help>}
        <Badge card={card} checking={checking} />
      </div>
      {card.line && <p className="hz-gs-line" title={card.line}>{card.line}</p>}
      {card.detail && <p className="hz-gs-detail" title={card.detail}>{card.detail}</p>}
    </li>
  );
}

export function GetStartedCards({ agent }: { agent: Agent }) {
  const user = useApp().user;
  const hub = agent.hub || agent.id;
  const hideKey = `hz.getStarted:${user?.id ?? "local"}:${hub}`;
  // Someone the Console doesn't open for manages no agent: no request at all.
  const mayManage = user?.console !== false;
  const [hidden, setHidden] = useState(() => readHidden(hideKey));
  const [answer, setAnswer] = useState<Answer | null>(() => shown.get(hub) ?? null);
  const [gone, setGone] = useState(false);
  const [pending, setPending] = useState<"first" | "again" | null>(null);
  const [slow, setSlow] = useState(false);
  const request = useRef<AbortController | null>(null);

  const load = (again: boolean) => {
    request.current?.abort();
    const ctl = new AbortController();
    request.current = ctl;
    setPending(again ? "again" : "first");
    get<Answer>(`/api/hubs/${enc(hub)}/get-started${again ? "?refresh=1" : ""}`, { signal: ctl.signal, quiet401: true })
      .then((got) => {
        if (!got || !Array.isArray(got.cards)) return;
        shown.set(hub, got);
        setAnswer(got);
      })
      .catch((e) => {
        if (isAbort(e)) return;
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
          shown.delete(hub);
          setGone(true);
        }
        // Anything else fails quietly: what is shown stays.
      })
      .finally(() => {
        if (request.current === ctl) setPending(null);
      });
  };

  useEffect(() => {
    if (hidden || !mayManage) return;
    load(false);
    return () => request.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hub, hidden, mayManage]);

  // A first answer that takes a moment says so; a quick 403 never flashes.
  useEffect(() => {
    if (pending !== "first") {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), 400);
    return () => clearTimeout(timer);
  }, [pending]);

  if (!mayManage || gone) return null;
  if (hidden)
    return (
      <button type="button" className="hz-gs-show" onClick={() => { writeHidden(hideKey, false); setHidden(false); }}>
        {t.getStarted.show}
      </button>
    );
  const hide = () => {
    writeHidden(hideKey, true);
    setHidden(true);
  };

  if (answer?.error)
    return (
      <Notice tone="warning" className="hz-gs-notice">
        <span className="inline-flex items-center gap-1">
          {t.getStarted.invalid}
          <Help label={t.getStarted.why}>{answer.error}</Help>
        </span>
      </Notice>
    );

  const cards = answer?.cards ?? [];
  if (!answer ? !(pending === "first" && slow) : cards.length === 0) return null;
  const checking = pending === "again" || !answer;
  const need = cards.filter((c) => c.status === "warn" || c.status === "fail").length;
  const status = checking ? t.getStarted.checking : need ? t.getStarted.needYou(need) : t.getStarted.allSet;
  const title = answer?.title || t.getStarted.title;
  return (
    <section className="hz-gs" aria-label={title} aria-busy={checking || undefined}>
      <div className="hz-gs-head">
        <h2 className="hz-gs-title">{title}</h2>
        <span className="hz-gs-status" data-tone={checking ? "busy" : need ? "warn" : "ok"} aria-live="polite">
          {status}
        </span>
        {answer && (
          <span className="hz-gs-acts">
            <Button variant="ghost" size="sm" icon={<RefreshCw size={13} aria-hidden />} onClick={() => load(true)}
              disabled={pending !== null}>
              {t.getStarted.checkAgain}
            </Button>
            <Button variant="ghost" size="sm" onClick={hide}>
              {t.getStarted.hide}
            </Button>
          </span>
        )}
      </div>
      {cards.length > 0 && (
        <ul className="hz-gs-grid">
          {cards.map((card) => (
            <Card key={card.id} card={card} checking={checking} />
          ))}
        </ul>
      )}
    </section>
  );
}
