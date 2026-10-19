// Settings > Workflow access: who may run, schedule, change and see each
// workflow. Every change goes through the same API and file format as
// `hubzoid workflow-access` (workflows/access.yaml), with the revision it read,
// so two people editing at once never overwrite each other.
import { displayName } from "../lib/format";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Copy, X } from "lucide-react";
import { ApiError, get, hubUrl, post, put } from "../lib/api";
import { useApp } from "../lib/app-context";
import { t } from "../i18n/en";
import { Button, Notice, cx } from "../components/ui";
import { toast } from "../components/toast";
import { PageFrame } from "./PageFrame";

type Grant = { subject: string; workflow: string; action: string; source?: string };
type Policy = { revision: number; yaml: string; grants: Grant[]; effective: Grant[]; actions: string[] };
type Resource = { key: string; kind: string; source?: string; title?: string; allowed_actions: string[] };

const W = t.workflowAccess;

/** The policy as workflows/access.yaml, one entry per person and workflow (as the server writes it). */
export function toYaml(grants: Grant[], order: string[]): string {
  const grouped = new Map<string, string[]>();
  for (const g of grants) {
    const k = `${g.workflow}\u0000${g.subject}`;
    grouped.set(k, [...(grouped.get(k) ?? []), g.action]);
  }
  const keys = [...grouped.keys()].sort();
  const lines = ["version: 1", "default: deny", keys.length ? "grants:" : "grants: []"];
  for (const k of keys) {
    const [workflow, subject] = k.split("\u0000");
    const acts = order.filter((a) => grouped.get(k)!.includes(a));
    lines.push(`  - subject: ${subject}`, `    workflow: ${workflow === "*" ? '"*"' : workflow}`, `    actions: [${acts.join(", ")}]`);
  }
  return lines.join("\n") + "\n";
}

export default function WorkflowAccessPage() {
  const app = useApp();
  const agents = app.agents.list;
  const [agentId, setAgentId] = useState<string | null>(agents[0]?.id ?? null);
  const agent = agents.find((a) => a.id === agentId) ?? agents[0];
  const base = agent?.api_base ?? "";
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [resources, setResources] = useState<Resource[]>([]);
  const [people, setPeople] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // The agent whose policy is on screen: a reply for another agent is dropped,
  // so switching agents can never send one agent's grants to the other.
  const current = useRef(base);
  const load = useCallback(async () => {
    setError(null);
    if (current.current !== base) {
      current.current = base;
      setPolicy(null);
      setResources([]);
    }
    try {
      const [p, r, s] = await Promise.all([
        get<Policy>(hubUrl(base, "/api/workflow-access")),
        get<{ items: Resource[] }>(hubUrl(base, "/api/workflow-access/resources")),
        get<{ items: { subject: string }[] }>(hubUrl(base, "/api/workflow-access/subjects")),
      ]);
      if (current.current !== base) return;
      setPolicy(p);
      setResources(r.items);
      setPeople(s.items.map((x) => x.subject));
    } catch (e) {
      setError(e instanceof ApiError && e.hasServerMessage ? e.message : W.loadFailed);
    }
  }, [base]);
  useEffect(() => {
    void load();
  }, [load]);

  const save = async (next: Grant[], note: string) => {
    if (!policy || current.current !== base) return;
    setSaving(true);
    try {
      await put(hubUrl(base, "/api/workflow-access"), { yaml: toYaml(next, policy.actions), expected_revision: policy.revision });
      toast(note);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.code === "revision_conflict") {
        toast(W.conflict);
        await load();
      } else setError(e instanceof ApiError && e.hasServerMessage ? e.message : W.saveFailed);
    } finally {
      setSaving(false);
    }
  };

  const explicit = policy?.grants ?? [];
  const earlier = (policy?.effective ?? []).filter((g) => g.source && g.source !== "workflow access");

  return (
    <PageFrame title={W.title} description={W.lede}>
      <div className="flex flex-col gap-5" data-testid="workflow-access">
        {agents.length > 1 && (
          <label className="flex items-center gap-2 text-[13px] text-secondary">
            {W.agent}
            <select className="hz-input !w-auto" value={agent?.id ?? ""} onChange={(e) => setAgentId(e.target.value)}>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {displayName(a.name)}
                </option>
              ))}
            </select>
          </label>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {!policy && !error && <p className="text-mute">{W.loading}</p>}
        {policy && resources.length === 0 && <p className="text-mute">{W.noWorkflows}</p>}
        {policy &&
          [{ key: "*", kind: "all", allowed_actions: [] } as Resource, ...resources].map((r) => (
            <WorkflowBlock
              key={r.key}
              resource={r}
              grants={explicit.filter((g) => g.workflow === r.key)}
              earlier={earlier.filter((g) => g.workflow === r.key)}
              actions={policy.actions}
              people={people}
              saving={saving}
              onAdd={(subject, acts) =>
                save(
                  [...explicit.filter((g) => !(g.workflow === r.key && g.subject === subject)), ...acts.map((action) => ({ subject, workflow: r.key, action }))],
                  W.saved,
                )
              }
              onRemove={(subject) => save(explicit.filter((g) => !(g.workflow === r.key && g.subject === subject)), W.removed)}
            />
          ))}
        {policy && <Explain base={base} people={people} resources={resources} actions={policy.actions} />}
        {policy && <FileView policy={policy} />}
      </div>
    </PageFrame>
  );
}

function actionLabel(a: string) {
  return (W.actions as Record<string, string>)[a] ?? a;
}

function WorkflowBlock({
  resource,
  grants,
  earlier,
  actions,
  people,
  saving,
  onAdd,
  onRemove,
}: {
  resource: Resource;
  grants: Grant[];
  earlier: Grant[];
  actions: string[];
  people: string[];
  saving: boolean;
  onAdd: (subject: string, actions: string[]) => void;
  onRemove: (subject: string) => void;
}) {
  const bySubject = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const g of grants) m.set(g.subject, [...(m.get(g.subject) ?? []), g.action]);
    return m;
  }, [grants]);
  const [adding, setAdding] = useState(false);
  const [subject, setSubject] = useState("");
  const [chosen, setChosen] = useState<string[]>(["run"]);
  const all = resource.key === "*";
  const offered = all ? actions : actions.filter((a) => a !== "create");
  const toggle = (a: string) => setChosen((c) => (c.includes(a) ? c.filter((x) => x !== a) : [...c, a]));
  return (
    <section className="hz-wa-block" data-testid={`wa-${resource.key}`}>
      <div className="flex items-baseline gap-2">
        <h3 className="m-0 text-[14px] font-medium text-ink" title={all ? undefined : resource.key}>
          {all ? W.allWorkflows : resource.title || resource.key}
        </h3>
        <span className="text-[12px] text-mute">{all ? W.allHelp : resource.kind === "markdown" ? W.markdown : resource.kind === "builtin" ? W.builtin : W.code}</span>
      </div>
      {bySubject.size === 0 && earlier.length === 0 && <p className="m-0 text-[13px] text-mute">{W.nobody}</p>}
      {[...bySubject.entries()].map(([who, acts]) => (
        <div key={who} className="hz-wa-row" data-testid="wa-grant">
          <span className="min-w-0 truncate text-[13px] text-ink">{who === "everyone" ? W.everyone : who}</span>
          <span className="min-w-0 flex-1 truncate text-[13px] text-secondary">
            {actions.filter((a) => acts.includes(a)).map(actionLabel).join(", ")}
          </span>
          <button type="button" className="hz-wa-x" aria-label={W.removeFor(who)} disabled={saving} onClick={() => onRemove(who)}>
            <X size={13} aria-hidden />
          </button>
        </div>
      ))}
      {earlier.length > 0 && (
        <div className="flex flex-col gap-0.5" data-testid="wa-earlier">
          <span className="text-[12px] text-mute">{W.earlier}</span>
          {[...new Set(earlier.map((g) => g.subject))].map((who) => {
            const mine = earlier.filter((g) => g.subject === who);
            const sources = [...new Set(mine.map((g) => g.source))].join(", ");
            return (
              <div key={who} className="hz-wa-row text-[12.5px] text-mute">
                <span className="min-w-0 truncate">{who}</span>
                <span className="min-w-0 flex-1 truncate">
                  {actions.filter((a) => mine.some((g) => g.action === a)).map(actionLabel).join(", ")} · {sources}
                </span>
              </div>
            );
          })}
        </div>
      )}
      {adding ? (
        <form
          className="hz-wa-add"
          onSubmit={(e) => {
            e.preventDefault();
            if (subject.trim() && chosen.length) {
              onAdd(subject.trim().toLowerCase(), chosen);
              setAdding(false);
              setSubject("");
            }
          }}
        >
          <input
            className="hz-input"
            list={`wa-people-${resource.key}`}
            placeholder={W.personPlaceholder}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            aria-label={W.person}
            data-testid="wa-person"
            autoFocus
          />
          <datalist id={`wa-people-${resource.key}`}>
            {people.map((p) => (
              <option key={p} value={p} />
            ))}
          </datalist>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label={W.actionsLabel}>
            {offered.map((a) => (
              <button
                type="button"
                key={a}
                className={cx("hz-wa-chip", chosen.includes(a) && "is-on")}
                aria-pressed={chosen.includes(a)}
                onClick={() => toggle(a)}
                data-testid={`wa-action-${a}`}
                title={(W.help as Record<string, string>)[a]}
              >
                {chosen.includes(a) && <Check size={12} aria-hidden />}
                {actionLabel(a)}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <Button type="submit" size="sm" variant="primary" loading={saving} disabled={!subject.trim() || !chosen.length} data-testid="wa-save">
              {W.give}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
              {W.cancel}
            </Button>
          </div>
        </form>
      ) : (
        <button type="button" className="hz-wa-addlink" onClick={() => setAdding(true)} data-testid="wa-add">
          {W.add}
        </button>
      )}
    </section>
  );
}

function Explain({ base, people, resources, actions }: { base: string; people: string[]; resources: Resource[]; actions: string[] }) {
  const [subject, setSubject] = useState("");
  const [workflow, setWorkflow] = useState(resources[0]?.key ?? "");
  const [action, setAction] = useState("run");
  const [answer, setAnswer] = useState<string | null>(null);
  const ask = async () => {
    try {
      const out = await post<{ allowed: boolean; reason: string; sources: string[]; subject: string; workflow: string; action: string }>(
        hubUrl(base, "/api/workflow-access/explain"),
        { subject, workflow, action },
      );
      setAnswer(out.allowed ? W.mayBecause(out.subject, actionLabel(out.action), out.workflow, out.sources.join("; ")) : W.mayNot(out.subject, actionLabel(out.action), out.workflow, out.reason === "missing_grant" ? W.noGrant : out.reason));
    } catch (e) {
      setAnswer(e instanceof ApiError && e.hasServerMessage ? e.message : W.loadFailed);
    }
  };
  return (
    <section className="hz-wa-block" data-testid="wa-explain">
      <h3 className="m-0 text-[14px] font-medium text-ink">{W.check}</h3>
      <div className="flex flex-wrap items-center gap-2">
        <input className="hz-input !w-56" list="wa-people-explain" placeholder={W.personPlaceholder} value={subject} onChange={(e) => setSubject(e.target.value)} aria-label={W.person} data-testid="wa-explain-person" />
        <datalist id="wa-people-explain">
          {people.map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
        <select className="hz-input !w-auto" value={action} onChange={(e) => setAction(e.target.value)} aria-label={W.actionsLabel} data-testid="wa-explain-action">
          {actions.map((a) => (
            <option key={a} value={a}>
              {actionLabel(a)}
            </option>
          ))}
        </select>
        <select className="hz-input !w-auto" value={workflow} onChange={(e) => setWorkflow(e.target.value)} aria-label={W.workflow} data-testid="wa-explain-workflow">
          {resources.map((r) => (
            <option key={r.key} value={r.key}>
              {r.title || r.key}
            </option>
          ))}
        </select>
        <Button size="sm" onClick={ask} disabled={!subject.trim() || !workflow} data-testid="wa-explain-ask">
          {W.checkButton}
        </Button>
      </div>
      {answer && (
        <p className="m-0 text-[13px] text-secondary" data-testid="wa-explain-answer" role="status">
          {answer}
        </p>
      )}
    </section>
  );
}

function FileView({ policy }: { policy: Policy }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="hz-wa-block">
      <button type="button" className="hz-request-more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {W.asFile(policy.revision)} <ChevronDown size={12} aria-hidden className={cx("hz-act-chev", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <pre className="hz-tech" data-testid="wa-yaml">{policy.yaml}</pre>
          <div className="flex gap-2">
            <Button
              size="sm"
              icon={<Copy size={13} aria-hidden />}
              onClick={() => {
                void navigator.clipboard?.writeText(policy.yaml);
                toast(W.copied);
              }}
            >
              {W.copy}
            </Button>
            <span className="text-[12px] text-mute">{W.fileHelp}</span>
          </div>
        </>
      )}
    </section>
  );
}
