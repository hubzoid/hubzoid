// Settings > Preferences > Security: two-step sign-in, where you're signed in,
// personal API keys, sign-in methods, and "sign out everywhere".
import { useEffect, useState, type FormEvent } from "react";
import { Copy, KeyRound, LogOut, MonitorSmartphone, ShieldCheck } from "lucide-react";
import { t } from "../i18n/en";
import { del, get, post } from "../lib/api";
import { useApp } from "../lib/app-context";
import { describeError } from "../lib/errors";
import { formatDate, formatRelative } from "../lib/format";
import { toast } from "../components/toast";
import { Button, Field, Notice, PageSpinner } from "../components/ui";
import { RecoveryCodes, SetupPanel, useReauth, type Setup } from "../components/Security";
import { Section } from "./SettingsFrame";

const S = t.security;

type Device = { id: string; device: string; place: string; signed_in_at: number; last_active_at: number; current: boolean };
type Key = { id: string; name: string; prefix: string; operations: string[]; hubs: string[] | null;
             created_at: number; expires_at: number | null; last_used_at: number | null; state: string };
type Security = {
  mode: string;
  mfa: { enabled: boolean; since: number | null; recovery_codes_left: number };
  mfa_policy: string;
  mfa_required: boolean;
  has_password: boolean;
  sessions: Device[];
  api_keys: Key[];
  linked: { provider: string; email: string }[];
  providers: { id: string; name: string }[];
};

export default function SecurityPage() {
  const app = useApp();
  const [data, setData] = useState<Security | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reauth = useReauth();
  const load = () =>
    get<Security>("/api/me/security")
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(describeError(e)));
  useEffect(() => {
    void load();
    const params = new URLSearchParams(location.hash.split("?")[1] ?? location.search);
    if (params.get("link") === "linked") toast(S.linkDone);
    else if (params.get("link")) toast(t.auth.codes[params.get("link") as string] ?? t.auth.oauthFallback, "error");
    if (params.get("reauth") === "verified") toast(t.reauth.verified);
    else if (params.get("reauth")) toast(t.auth.codes[params.get("reauth") as string] ?? t.auth.oauthFallback, "error");
    const used = sessionStorage.getItem("hz.usedRecovery");
    if (used) {
      sessionStorage.removeItem("hz.usedRecovery");
      toast(t.auth.usedRecovery(Number(used)), "info");
    }
  }, []);

  if (app.isLocal) return <Notice tone="info">{S.localNote}</Notice>;
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <PageSpinner />;
  return (
    <div data-testid="security">
      {reauth.dialog}
      <Section title={S.mfa} description={S.mfaHelp}>
        <Mfa data={data} reload={load} run={reauth.run} />
      </Section>
      <Section title={S.devices} description={S.devicesHelp}>
        <Devices devices={data.sessions} reload={load} />
      </Section>
      <Section title={S.keys} description={S.keysHelp}>
        <Keys keys={data.api_keys} reload={load} run={reauth.run} />
      </Section>
      <Section title={S.linked} description={S.linkedHelp}>
        <Methods data={data} run={reauth.run} />
      </Section>
      <Section title={S.revokeAll} description={S.revokeAllHelp}>
        <Button
          icon={<LogOut size={14} aria-hidden />}
          onClick={() =>
            void reauth
              .run(() => post("/api/me/revoke-credentials"))
              .then(() => {
                toast(S.revokedAll);
                void load();
              })
              .catch((e) => e?.code !== "reauth_cancelled" && toast(describeError(e), "error"))
          }
        >
          {S.revokeAll}
        </Button>
      </Section>
    </div>
  );
}

type Run = <T>(action: () => Promise<T>) => Promise<T>;

function Mfa({ data, reload, run }: { data: Security; reload: () => void; run: Run }) {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [codeError, setCodeError] = useState<string | null>(null);
  const start = async () => {
    setBusy(true);
    try {
      const res = await run(() => post<{ setup: Setup }>("/api/me/mfa/enroll"));
      setSetup(res.setup);
      setCodeError(null);
    } catch (e) {
      if ((e as { code?: string })?.code !== "reauth_cancelled") toast(describeError(e), "error");
    } finally {
      setBusy(false);
    }
  };
  const confirm = async (code: string) => {
    setBusy(true);
    setCodeError(null);
    try {
      const res = await post<{ recovery_codes: string[] }>("/api/me/mfa/confirm", { code });
      setSetup(null);
      setCodes(res.recovery_codes);
    } catch (e) {
      setCodeError(describeError(e));
    } finally {
      setBusy(false);
    }
  };
  const newCodes = async () => {
    try {
      const res = await run(() => post<{ recovery_codes: string[] }>("/api/me/mfa/recovery-codes"));
      setCodes(res.recovery_codes);
    } catch (e) {
      if ((e as { code?: string })?.code !== "reauth_cancelled") toast(describeError(e), "error");
    }
  };
  const off = async () => {
    try {
      await run(() => del("/api/me/mfa"));
      toast(S.turnedOff);
      reload();
    } catch (e) {
      if ((e as { code?: string })?.code !== "reauth_cancelled") toast(describeError(e), "error");
    }
  };
  if (codes)
    return (
      <RecoveryCodes
        codes={codes}
        onDone={() => {
          setCodes(null);
          toast(S.turnedOn);
          reload();
        }}
      />
    );
  if (setup)
    return (
      <div className="max-w-sm">
        <SetupPanel setup={setup} busy={busy} error={codeError} onCode={(c) => void confirm(c)} />
        <Button variant="ghost" size="sm" className="mt-2" onClick={() => setSetup(null)}>
          {t.cancel}
        </Button>
      </div>
    );
  const m = data.mfa;
  return (
    <div className="space-y-3 text-[13.5px]" data-testid="mfa-status">
      <p className="m-0 flex items-center gap-2 text-body">
        <ShieldCheck size={15} aria-hidden className={m.enabled ? "text-success" : "text-mute"} />
        <strong className="font-medium text-ink">{m.enabled ? S.mfaOn : S.mfaOff}</strong>
        {m.enabled && m.since && <span className="text-mute">{S.mfaSince(formatDate(m.since))}</span>}
        {data.mfa_required && <span className="text-mute">· {S.mfaRequired}</span>}
      </p>
      {m.enabled && <p className="m-0 text-mute">{S.codesLeft(m.recovery_codes_left)}</p>}
      <div className="flex flex-wrap gap-2">
        {!m.enabled && (
          <Button variant="primary" loading={busy} onClick={() => void start()}>
            {S.turnOn}
          </Button>
        )}
        {m.enabled && (
          <>
            <Button size="sm" loading={busy} onClick={() => void start()}>
              {S.newAuthenticator}
            </Button>
            <Button size="sm" onClick={() => void newCodes()}>
              {S.newCodes}
            </Button>
            {!data.mfa_required && (
              <Button size="sm" variant="ghost" onClick={() => void off()}>
                {S.turnOff}
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function Devices({ devices, reload }: { devices: Device[]; reload: () => void }) {
  const others = devices.filter((d) => !d.current);
  return (
    <div className="space-y-2">
      <ul className="m-0 list-none divide-y divide-line-soft p-0" data-testid="devices">
        {devices.map((d) => (
          <li key={d.id} className="flex items-center gap-3 py-2.5 text-[13.5px]">
            <MonitorSmartphone size={16} aria-hidden className="flex-none text-mute" />
            <div className="min-w-0 flex-1">
              <div className="text-ink">
                {d.device}
                {d.current && <span className="ml-2 rounded bg-sunken px-1.5 py-0.5 text-[11.5px] text-mute">{S.thisDevice}</span>}
              </div>
              <div className="text-[12.5px] text-mute">
                {[d.place, S.lastActive(formatRelative(d.last_active_at))].filter(Boolean).join(" · ")}
              </div>
            </div>
            {!d.current && (
              <Button size="sm" variant="ghost" onClick={() => void del(`/api/me/sessions/${d.id}`).then(reload).catch((e) => toast(describeError(e), "error"))}>
                {S.signOutDevice}
              </Button>
            )}
          </li>
        ))}
      </ul>
      {others.length > 0 ? (
        <Button size="sm" onClick={() => void post<{ ended: number }>("/api/me/sessions/revoke-others").then((r) => { toast(S.signedOutOthers(r.ended)); reload(); })}>
          {S.signOutOthers}
        </Button>
      ) : (
        <p className="m-0 text-[12.5px] text-mute">{S.noOthers}</p>
      )}
    </div>
  );
}

const PRESETS = [
  { id: "chat", label: S.presetChat, help: S.presetChatHelp },
  { id: "read", label: S.presetRead, help: S.presetReadHelp },
  { id: "all", label: S.presetAll, help: S.presetAllHelp },
];

function Keys({ keys, reload, run }: { keys: Key[]; reload: () => void; run: Run }) {
  const app = useApp();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("chat");
  const [scope, setScope] = useState<"all" | "some">("all");
  const [picked, setPicked] = useState<string[]>([]);
  const [days, setDays] = useState(90);
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const agents = app.agents.list ?? [];
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!name.trim()) return setError(S.keyNameHint);
    setBusy(true);
    try {
      const body: Record<string, unknown> = { name: name.trim(), preset, expires_in_days: days };
      if (preset === "chat" && scope === "some") body.hubs = picked;
      const res = await run(() => post<{ key: string }>("/api/me/api-keys", body));
      setMade(res.key);
      setOpen(false);
      setName("");
      reload();
    } catch (err) {
      if ((err as { code?: string })?.code !== "reauth_cancelled") setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3 text-[13.5px]">
      {made && (
        <Notice tone="success" title={S.keyMade} onDismiss={() => setMade(null)}>
          <p className="m-0">{S.keyOnce}</p>
          <div className="mt-2 flex items-center gap-2">
            <code className="select-all break-all rounded bg-raised px-2 py-1 text-[12.5px] text-ink" data-testid="new-key">{made}</code>
            <Button size="sm" icon={<Copy size={13} aria-hidden />} onClick={async () => {
              try {
                await navigator.clipboard.writeText(made);
                toast(S.copied);
              } catch {
                toast(S.copyFailed, "error");
              }
            }}>
              {S.copyKeyValue}
            </Button>
          </div>
        </Notice>
      )}
      {keys.length === 0 && !open && <p className="m-0 text-mute">{S.noKeys}</p>}
      {keys.length > 0 && (
        <ul className="m-0 list-none divide-y divide-line-soft p-0" data-testid="keys">
          {keys.map((k) => (
            <li key={k.id} className="flex items-center gap-3 py-2.5">
              <KeyRound size={15} aria-hidden className="flex-none text-mute" />
              <div className="min-w-0 flex-1">
                <div className="text-ink">
                  {k.name} <code className="ml-1 text-[12px] text-mute">{k.prefix}…</code>
                </div>
                <div className="text-[12.5px] text-mute">
                  {[
                    PRESETS.find((p) => p.id === (k.operations.length === 3 ? "all" : k.operations[0]))?.label,
                    k.hubs ? k.hubs.join(", ") : null,
                    k.state === "active" ? (k.last_used_at ? S.lastUsed(formatRelative(k.last_used_at)) : S.neverUsed)
                      : k.state === "expired" ? S.expired : S.revoked,
                    k.expires_at && k.state === "active" ? `${S.expires} ${formatDate(k.expires_at)}` : null,
                  ].filter(Boolean).join(" · ")}
                </div>
              </div>
              {k.state === "active" && (
                <Button size="sm" variant="ghost" onClick={() => void del(`/api/me/api-keys/${k.id}`).then(reload)}>
                  {S.revoke}
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {open ? (
        <form onSubmit={submit} className="max-w-md space-y-4 rounded-lg border border-line p-4" noValidate data-testid="new-key-form">
          {error && <Notice tone="error">{error}</Notice>}
          <Field label={S.keyName} value={name} onChange={(e) => setName(e.target.value)} help={S.keyNameHint} autoFocus maxLength={120} />
          <fieldset className="m-0 space-y-2 border-0 p-0">
            <legend className="hz-label">{S.keyAccess}</legend>
            {PRESETS.map((p) => (
              <label key={p.id} className="flex items-start gap-2">
                <input type="radio" name="preset" value={p.id} checked={preset === p.id} onChange={() => setPreset(p.id)} className="mt-1" />
                <span>
                  <span className="text-ink">{p.label}</span>
                  <span className="block text-[12.5px] text-mute">{p.help}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {preset === "chat" && agents.length > 1 && (
            <fieldset className="m-0 space-y-2 border-0 p-0">
              <legend className="hz-label">{S.keyAgents}</legend>
              <label className="flex items-center gap-2">
                <input type="radio" name="scope" checked={scope === "all"} onChange={() => setScope("all")} />
                {S.allAgents}
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name="scope" checked={scope === "some"} onChange={() => setScope("some")} />
                {S.someAgents}
              </label>
              {scope === "some" && (
                <div className="ml-6 space-y-1">
                  {agents.map((a) => (
                    <label key={a.hub ?? a.id} className="flex items-center gap-2">
                      <input type="checkbox" checked={picked.includes(a.hub ?? a.id)}
                        onChange={(e) => setPicked(e.target.checked ? [...picked, a.hub ?? a.id] : picked.filter((x) => x !== (a.hub ?? a.id)))} />
                      {a.name}
                    </label>
                  ))}
                  <p className="m-0 text-[12px] text-mute">{S.agentsChatOnly}</p>
                </div>
              )}
            </fieldset>
          )}
          <label className="block">
            <span className="hz-label">{S.expires}</span>
            <select className="hz-input" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[30, 90, 365].map((d) => (
                <option key={d} value={d}>{S.days(d)}</option>
              ))}
            </select>
          </label>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>{t.cancel}</Button>
            <Button type="submit" variant="primary" loading={busy}>{S.newKey}</Button>
          </div>
        </form>
      ) : (
        <Button size="sm" icon={<KeyRound size={13} aria-hidden />} onClick={() => setOpen(true)}>
          {S.newKey}
        </Button>
      )}
    </div>
  );
}

function Methods({ data, run }: { data: Security; run: Run }) {
  const linked = new Set(data.linked.map((l) => l.provider));
  const back = encodeURIComponent("/settings#/me/security");
  // Adding a sign-in confirms it's you first, then goes to the provider.
  const link = (id: string) =>
    void run(() => post("/api/me/identity-links/check"))
      .then(() => location.assign(`/oauth/${encodeURIComponent(id)}/login?link=1&redirect=${back}`))
      .catch((e) => (e as { code?: string })?.code !== "reauth_cancelled" && toast(describeError(e), "error"));
  return (
    <div className="space-y-2 text-[13.5px]">
      <ul className="m-0 list-none space-y-1.5 p-0">
        {data.has_password && <li className="text-ink">{S.passwordMethod}</li>}
        {data.linked.map((l) => (
          <li key={l.provider} className="text-ink">
            {S.linkedTo(data.providers.find((p) => p.id === l.provider)?.name ?? l.provider, l.email)}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        {data.providers.filter((p) => !linked.has(p.id)).map((p) => (
          <Button key={p.id} size="sm" onClick={() => link(p.id)}>
            {S.linkProvider(p.name)}
          </Button>
        ))}
      </div>
    </div>
  );
}
