// Usage: what each agent used this month against its limits, as bars, and the
// limits themselves as a few fields (budgets.yaml stays one click away). The
// totals for a period sit above. Same API as `hubzoid budgets`.
import { useContext, useState } from "react";
import { App, Input, Modal, Segmented } from "antd";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ApiError, request, type Hub, type Summary } from "../api";
import { useData } from "../hooks/useData";
import { useHashQuery } from "../hooks/useRoute";
import { AppContext } from "../app/lib/app-context";
import { AgentAvatar } from "../app/components/ui";
import { short } from "../lib/format";
import { agentName } from "../lib/agentName";

type Limits = Record<string, Record<string, string | number>>;
type Status = {
  present: boolean; revision: string; yaml: string; limits: Limits;
  month: { key: string; input_tokens: number; output_tokens: number; cost_usd: string; unpriced_calls: number };
  reserved: { input_tokens: number; output_tokens: number; cost_usd: string };
  open_reservations: Reservation[];
};
type Reservation = { id: string; run_id: string; item_id: string | null; model: string | null; cost_usd: number;
  output_tokens: number; created: number; unknown: boolean };

const PERIODS = [
  { value: "24h", label: "24 hours" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
];
const usd = (n: number | null | undefined) => (n == null ? "—" : n < 0.01 && n > 0 ? "< $0.01" : `$${n.toFixed(2)}`);

export function UsageScreen({ hubs }: { hubs: Hub[] }) {
  const [q, setQ] = useHashQuery();
  const period = PERIODS.some((p) => p.value === q.period) ? q.period! : "30d";
  const summary = useData<Summary>(`/summary?period=${period}`, { keepStale: true });
  const s = summary.data;
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="hz-usage">
      <div className="hz-usage-head">
        <div>
          <h2>Usage</h2>
          <p>What your agents used, and the limits that keep spending in check.</p>
        </div>
        <Segmented aria-label="Period" value={period} onChange={(v) => setQ({ period: v === "30d" ? undefined : String(v) })} options={PERIODS} />
      </div>
      <ul className="hz-usage-stats" aria-label="Totals">
        <Stat label="Messages" value={s ? s.totals.messages.toLocaleString() : "—"}
          sub={s ? `Across ${s.totals.chats.toLocaleString()} ${s.totals.chats === 1 ? "conversation" : "conversations"}` : undefined} />
        <Stat label="Users" value={s?.user_accounts?.accounts == null ? "—" : s.user_accounts.accounts.toLocaleString()}
          sub={s?.user_accounts ? `Across ${s.user_accounts.hubs} ${s.user_accounts.hubs === 1 ? "hub" : "hubs"}` : undefined} />
        <Stat label="Tokens used" value={s ? short(s.totals.input_tokens + s.totals.output_tokens) : "—"}
          sub={s ? `${short(s.totals.input_tokens)} in · ${short(s.totals.output_tokens)} out` : undefined} />
        <Stat label="Workflow runs" value={!s ? "—" : !s.has_workflows ? "0" : s.runs_available ? (s.totals.runs ?? 0).toLocaleString() : "—"}
          sub={s?.has_workflows && s.runs_available ? `${(s.totals.failed ?? 0).toLocaleString()} failed` : undefined} />
        <Stat label="Approx. cost" value={s ? usd(s.totals.cost_usd) : "—"} sub="Estimated from model prices" />
      </ul>
      <h3 className="hz-usage-sub">This month by agent</h3>
      <p className="hz-usage-note">Each model call is checked against its agent's limits first. An agent with no limit is never stopped.</p>
      <ul className="hz-usage-list">
        {hubs.map((h) => (
          <AgentUsage key={h.key} hub={h} open={open === h.key} toggle={() => setOpen(open === h.key ? null : h.key)} />
        ))}
      </ul>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <li>
      <span className="hz-usage-stat-label">{label}</span>
      <span className="hz-usage-stat-value">{value}</span>
      {sub && <span className="hz-usage-stat-sub">{sub}</span>}
    </li>
  );
}

/** Month limits worth a bar: output tokens, input tokens and dollars. */
const BARS: { key: string; label: string; used: (s: Status) => number; held: (s: Status) => number; format: (n: number) => string }[] = [
  { key: "max_output_tokens", label: "output tokens", used: (s) => s.month.output_tokens, held: (s) => s.reserved.output_tokens, format: (n) => short(n) },
  { key: "max_input_tokens", label: "input tokens", used: (s) => s.month.input_tokens, held: (s) => s.reserved.input_tokens, format: (n) => short(n) },
  { key: "max_cost_usd", label: "", used: (s) => Number(s.month.cost_usd), held: (s) => Number(s.reserved.cost_usd), format: (n) => `$${n.toFixed(2)}` },
];

function AgentUsage({ hub, open, toggle }: { hub: Hub; open: boolean; toggle: () => void }) {
  const app = useContext(AppContext);
  const agent = app?.agents.list.find((a) => a.hub === hub.key);
  const name = agentName(agent?.name, hub.name, hub.key);
  const base = `/hubs/${encodeURIComponent(hub.key)}/budgets`;
  const data = useData<Status>(base, { keepStale: true });
  const s = data.data;
  const month = s?.limits?.month ?? {};
  const bars = s ? BARS.filter((b) => month[b.key] != null) : [];
  return (
    <li className={open ? "hz-usage-item is-open" : "hz-usage-item"}>
      <button type="button" className="hz-usage-row" aria-expanded={open} onClick={toggle}>
        <AgentAvatar name={name} src={agent?.avatar_url} size={28} />
        <span className="hz-usage-name">{name}</span>
        <span className="hz-usage-bars">
          {!s ? (
            <span className="hz-usage-muted">{data.error ? "Couldn’t load" : "Loading…"}</span>
          ) : bars.length === 0 ? (
            <span className="hz-usage-free">
              <span>{short(s.month.output_tokens)} output tokens · {usd(Number(s.month.cost_usd))}</span>
              <span className="hz-usage-muted">No monthly limit</span>
            </span>
          ) : (
            bars.map((b) => {
              const cap = Number(month[b.key]);
              // What running calls hold counts against the limit too.
              const held = b.held(s);
              const used = b.used(s) + held;
              const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
              const tone = pct >= 100 ? "is-bad" : pct >= 80 ? "is-warn" : "";
              return (
                <span key={b.key} className="hz-bar-wrap">
                  <span className="hz-bar-text">
                    <span>{b.format(used)} of {b.format(cap)} {b.label}{held > 0 ? `, ${b.format(held)} held by running calls` : ""}</span>
                    <span className={tone ? `hz-bar-pct ${tone}` : "hz-bar-pct"}>{pct >= 100 ? "Limit reached" : `${Math.round(pct)}%`}</span>
                  </span>
                  <span className={`hz-bar ${tone}`} role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={used}
                    aria-label={`${name}: ${b.format(used)} of ${b.format(cap)} ${b.label} this month`}>
                    <span style={{ width: `${pct}%` }} />
                  </span>
                </span>
              );
            })
          )}
        </span>
        <span className="hz-usage-edit">{s && Object.keys(s.limits ?? {}).length ? "Edit limits" : "Set limits"}</span>
        {open ? <ChevronDown size={16} aria-hidden /> : <ChevronRight size={16} aria-hidden />}
      </button>
      {open && s && (
        <>
          <LimitsEditor key={s.revision} base={base} status={s} saved={data.reload} />
          {s.open_reservations.length > 0 && <Reservations base={base} items={s.open_reservations} released={data.reload} />}
        </>
      )}
    </li>
  );
}

const FIELDS: { scope: string; key: string; label: string; help: string }[] = [
  { scope: "month", key: "max_output_tokens", label: "Output tokens per month", help: "The agent stops answering until next month once this is used." },
  { scope: "month", key: "max_cost_usd", label: "Dollars per month", help: "Estimated from model prices. A runtime without prices, like Codex, can't be capped in dollars." },
  { scope: "run", key: "max_turns", label: "Turns per reply or run", help: "Stops one reply or workflow run that keeps going." },
  { scope: "run", key: "max_output_tokens", label: "Output tokens per reply or run", help: "" },
];

function toYaml(limits: Limits): string {
  const scopes = Object.entries(limits).filter(([, caps]) => Object.keys(caps).length > 0);
  if (!scopes.length) return "version: 1\nlimits: {}\n";
  return `version: 1\nlimits:\n${scopes.map(([scope, caps]) => `  ${scope}:\n${Object.entries(caps).map(([k, v]) => `    ${k}: ${v}`).join("\n")}`).join("\n")}\n`;
}

const errorMessage = (e: unknown) => {
  if (e instanceof ApiError) {
    const detail = e.data?.detail as { message?: string } | string | undefined;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (typeof detail === "string") return detail;
  }
  return e instanceof Error ? e.message : String(e);
};

/** A few fields for the common limits. Other limits in the file are kept as
 *  they are; Edit as file shows the whole budgets.yaml. */
function LimitsEditor({ base, status, saved }: { base: string; status: Status; saved: () => void }) {
  const { message } = App.useApp();
  // Remounted for each saved revision (its key), so the fields start from it.
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [`${f.scope}.${f.key}`, String(status.limits?.[f.scope]?.[f.key] ?? "")])));
  const [asFile, setAsFile] = useState(false);
  const [yaml, setYaml] = useState(status.yaml);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const initial = Object.fromEntries(FIELDS.map((f) => [`${f.scope}.${f.key}`, String(status.limits?.[f.scope]?.[f.key] ?? "")]));
  const fieldsChanged = FIELDS.some((f) => (values[`${f.scope}.${f.key}`] ?? "").trim() !== initial[`${f.scope}.${f.key}`]);
  const fileChanged = yaml !== status.yaml;
  const commented = /^\s*#/m.test(status.yaml);

  /** The file the fields describe: every other limit in the file kept. */
  const fromFields = (): string | null => {
    const next: Limits = JSON.parse(JSON.stringify(status.limits ?? {}));
    for (const f of FIELDS) {
      const raw = (values[`${f.scope}.${f.key}`] ?? "").trim();
      next[f.scope] = next[f.scope] ?? {};
      if (raw === "") delete next[f.scope][f.key];
      else if (!/^\d+(\.\d+)?$/.test(raw)) {
        setProblem(`${f.label} must be a number.`);
        return null;
      } else next[f.scope][f.key] = Number(raw);
    }
    return toYaml(next);
  };

  // Switching keeps what was typed: fields become the file; file edits are
  // only dropped after a confirm, since the fields can't show every limit.
  const switchMode = () => {
    setProblem(null);
    if (!asFile) {
      if (fieldsChanged) {
        const next = fromFields();
        if (next === null) return;
        setYaml(next);
      }
      setAsFile(true);
      return;
    }
    if (fileChanged && !window.confirm("Switch to fields? Your edits to the file aren't saved and will be lost.")) return;
    setYaml(status.yaml);
    setAsFile(false);
  };

  const save = async () => {
    let body = yaml;
    if (!asFile) {
      const next = fromFields();
      if (next === null) return;
      body = next;
    }
    setBusy(true);
    try {
      await request(base, { yaml: body, expected_revision: status.revision }, undefined, "PUT");
      setProblem(null);
      message.success("Limits saved. The next model call is checked against them.");
      saved();
    } catch (e) {
      setProblem(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="hz-limits">
      {asFile ? (
        <textarea className="hz-input hz-limits-file" rows={10} value={yaml} onChange={(e) => setYaml(e.target.value)}
          spellCheck={false} aria-label="budgets.yaml" />
      ) : (
        <div className="hz-limits-grid">
          {FIELDS.map((f) => (
            <label key={`${f.scope}.${f.key}`} className="hz-limits-field">
              <span className="hz-label">{f.label}</span>
              <input className="hz-input" inputMode="decimal" placeholder="No limit" value={values[`${f.scope}.${f.key}`] ?? ""}
                onChange={(e) => setValues({ ...values, [`${f.scope}.${f.key}`]: e.target.value })} />
              {f.help && <span className="hz-limits-help">{f.help}</span>}
            </label>
          ))}
        </div>
      )}
      {problem && <p className="hz-limits-problem" role="alert">{problem}</p>}
      <div className="hz-limits-foot">
        <button type="button" className="hz-btn hz-btn-primary hz-btn-sm" disabled={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save limits"}
        </button>
        <button type="button" className="hz-link-btn" onClick={switchMode}>
          {asFile ? "Edit as fields" : "Edit as file"}
        </button>
        <span className="hz-usage-muted">Empty means no limit. Stored in the agent's budgets.yaml.</span>
      </div>
      {!asFile && commented && (
        <p className="hz-limits-help" style={{ marginTop: 10 }}>Saving these fields rewrites budgets.yaml without its comments. Edit as file to keep them.</p>
      )}
    </div>
  );
}

/** Calls that hold part of the limit while they run. One whose call never
 *  reported keeps holding it until someone releases it, with a reason. */
function Reservations({ base, items, released }: { base: string; items: Reservation[]; released: () => void }) {
  const { message } = App.useApp();
  const [releasing, setReleasing] = useState<Reservation | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const release = async () => {
    if (!releasing) return;
    setBusy(true);
    try {
      await request(`${base}/reservations/${encodeURIComponent(releasing.id)}/release`, { reason }, undefined, "POST");
      message.success("Released.");
      setReleasing(null);
      setReason("");
      released();
    } catch (e) {
      message.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="hz-limits hz-held">
      <h4>Held by running calls</h4>
      <ul>
        {items.map((r) => (
          <li key={r.id}>
            <span className="mono">{r.run_id.slice(0, 24)}</span>
            <span className="hz-usage-muted">since {new Date(r.created * 1000).toLocaleString()}</span>
            {r.unknown ? (
              <>
                <span className="hz-status is-warn"><i aria-hidden />Never reported</span>
                <button type="button" className="hz-btn hz-btn-secondary hz-btn-sm" onClick={() => setReleasing(r)}>Release</button>
              </>
            ) : (
              <span className="hz-status is-info"><i aria-hidden />Running</span>
            )}
          </li>
        ))}
      </ul>
      <Modal open={releasing !== null} title="Release this reservation?" okText="Release"
        okButtonProps={{ disabled: !reason.trim(), loading: busy }} onOk={() => void release()} onCancel={() => setReleasing(null)}>
        <p>Its call never reported its usage, so the amount it held still counts against your limits. Release it only
          after checking what the model provider charged.</p>
        <Input.TextArea rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="What you checked. This is recorded." aria-label="Reason" />
      </Modal>
    </div>
  );
}
