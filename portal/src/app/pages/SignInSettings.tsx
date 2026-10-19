// Workspace settings > Sign-in (administrators): who must use two-step
// sign-in, and company sign-in through a SAML identity provider with SCIM.
import { useEffect, useState, type FormEvent } from "react";
import { Building2, Copy, KeyRound } from "lucide-react";
import { t } from "../i18n/en";
import { del, get, post, put } from "../lib/api";
import { describeError } from "../lib/errors";
import { toast } from "../components/toast";
import { Button, Field, Notice, PageSpinner } from "../components/ui";
import { useReauth } from "../components/Security";
import { Section } from "./SettingsFrame";

const D = t.directory;
const P = t.peopleSecurity;

type Provider = {
  id: string;
  name: string;
  enabled: boolean;
  idp: { idp_entity_id?: string; idp_sso_url?: string; metadata_url?: string | null };
  paste: { entity_id: string; acs_url: string; metadata_url: string; scim_url: string };
  scim_tokens: { id: string; name: string; prefix: string; created_at: number; revoked_at: number | null; last_used_at: number | null }[];
  people: { user_name: string; active: boolean }[];
};

export default function SignInSettings() {
  const reauth = useReauth();
  return (
    <div data-testid="sign-in-settings">
      {reauth.dialog}
      <Section title={P.mfaRule} description={P.mfaRuleHelp}>
        <MfaRule run={reauth.run} />
      </Section>
      <Section title={D.title} description={D.help}>
        <Directories run={reauth.run} />
      </Section>
    </div>
  );
}

type Run = <T>(action: () => Promise<T>) => Promise<T>;
const cancelled = (e: unknown) => (e as { code?: string })?.code === "reauth_cancelled";

function MfaRule({ run }: { run: Run }) {
  const [policy, setPolicy] = useState<string | null>(null);
  useEffect(() => {
    get<{ policy: string }>("/api/auth/mfa/policy").then((r) => setPolicy(r.policy)).catch(() => setPolicy("admins"));
  }, []);
  if (!policy) return <PageSpinner />;
  const choose = async (next: string) => {
    try {
      const r = await run(() => put<{ policy: string }>("/api/auth/mfa/policy", { policy: next }));
      setPolicy(r.policy);
      toast(P.done);
    } catch (e) {
      if (!cancelled(e)) toast(describeError(e), "error");
    }
  };
  return (
    <div role="radiogroup" aria-label={P.mfaRule} className="space-y-2 text-[13.5px]" data-testid="mfa-policy">
      {(["none", "admins", "all"] as const).map((p) => (
        <label key={p} className="flex items-center gap-2">
          <input type="radio" name="mfa-policy" checked={policy === p} onChange={() => void choose(p)} />
          {P[p]}
        </label>
      ))}
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-[13px]">
      <div className="text-mute">{label}</div>
      <div className="mt-0.5 flex items-center gap-2">
        <code className="select-all break-all rounded bg-sunken px-2 py-0.5 text-[12.5px] text-ink">{value}</code>
        <button type="button" className="hz-icon-btn" aria-label={`${t.security.copyKey}: ${label}`}
          onClick={() => void navigator.clipboard.writeText(value).then(() => toast(t.security.copied), () => toast(t.security.copyFailed, "error"))}>
          <Copy size={13} aria-hidden />
        </button>
      </div>
    </div>
  );
}

function Directories({ run }: { run: Run }) {
  const [data, setData] = useState<{ available: boolean; why: string; providers: Provider[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const load = () => get<typeof data>("/api/auth/directories").then(setData).catch((e) => setError(describeError(e)));
  useEffect(() => {
    void load();
    const params = new URLSearchParams(location.hash.split("?")[1] ?? "");
    if (params.get("saml_test") === "ok") toast(D.tested(params.get("as") ?? ""));
  }, []);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <PageSpinner />;
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await run(fn);
      await load();
    } catch (e) {
      if (!cancelled(e)) toast(describeError(e), "error");
    }
  };
  return (
    <div className="space-y-4 text-[13.5px]">
      {!data.available && <Notice tone="warning">{D.unavailable} {data.why}</Notice>}
      {token && (
        <Notice tone="success" title={D.tokenMade} onDismiss={() => setToken(null)}>
          <p className="m-0">{D.tokenOnce}</p>
          <CopyRow label="SCIM token" value={token} />
        </Notice>
      )}
      {data.providers.map((p) => (
        <div key={p.id} className="space-y-3 rounded-lg border border-line p-4" data-testid={`provider-${p.id}`}>
          <div className="flex flex-wrap items-center gap-2">
            <Building2 size={15} aria-hidden className="text-mute" />
            <strong className="font-medium text-ink">{p.name}</strong>
            <span className={p.enabled ? "text-success" : "text-mute"}>· {p.enabled ? D.on : D.off}</span>
            <span className="text-mute">· {D.people(p.people.filter((x) => x.active).length)}</span>
            <span className="flex-1" />
            <a className="hz-btn hz-btn-ghost hz-btn-sm no-underline"
              href={`/api/auth/saml/${encodeURIComponent(p.id)}/login?test=1&redirect=${encodeURIComponent("/settings#/settings/sign-in")}`}>
              {D.test}
            </a>
            <Button size="sm" variant={p.enabled ? "ghost" : "primary"}
              onClick={() => void act(() => post(`/api/auth/directories/${p.id}/${p.enabled ? "disable" : "enable"}`))}>
              {p.enabled ? D.turnOff : D.turnOn}
            </Button>
          </div>
          <details>
            <summary className="cursor-pointer text-[13px] text-accent-text">{D.paste}</summary>
            <div className="mt-2 space-y-2">
              <CopyRow label={D.entityId} value={p.paste.entity_id} />
              <CopyRow label={D.acsUrl} value={p.paste.acs_url} />
              <CopyRow label={D.scimUrl} value={p.paste.scim_url} />
              <p className="m-0 text-[12.5px] text-mute">{D.nameIdNote}</p>
            </div>
          </details>
          <div>
            <div className="hz-label">{D.tokens}</div>
            <ul className="m-0 list-none space-y-1 p-0">
              {p.scim_tokens.filter((x) => !x.revoked_at).map((x) => (
                <li key={x.id} className="flex items-center gap-2">
                  <KeyRound size={13} aria-hidden className="text-mute" />
                  <span>{x.name}</span>
                  <code className="text-[12px] text-mute">{x.prefix}…</code>
                  <Button size="sm" variant="ghost" onClick={() => void act(() => del(`/api/auth/directories/${p.id}/scim-tokens/${x.id}`))}>
                    {t.security.revoke}
                  </Button>
                </li>
              ))}
            </ul>
            <Button size="sm" className="mt-1" onClick={() => void run(() => post<{ token: string }>(`/api/auth/directories/${p.id}/scim-tokens`, { name: p.name }))
              .then((r) => { setToken(r.token); void load(); })
              .catch((e) => !cancelled(e) && toast(describeError(e), "error"))}>
              {D.newToken}
            </Button>
          </div>
        </div>
      ))}
      {adding ? (
        <AddProvider run={run} onDone={() => { setAdding(false); void load(); }} onCancel={() => setAdding(false)} />
      ) : (
        <Button icon={<Building2 size={14} aria-hidden />} onClick={() => setAdding(true)} disabled={!data.available}>
          {D.add}
        </Button>
      )}
    </div>
  );
}

function AddProvider({ run, onDone, onCancel }: { run: Run; onDone: () => void; onCancel: () => void }) {
  const [id, setId] = useState("okta");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [xml, setXml] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await run(() => post("/api/auth/directories", { id: id.trim(), name: name.trim(), metadata_url: url.trim(), metadata_xml: xml }));
      onDone();
    } catch (err) {
      if (!cancelled(err)) setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="max-w-lg space-y-3 rounded-lg border border-line p-4" noValidate data-testid="add-provider">
      {error && <Notice tone="error">{error}</Notice>}
      <Field label={D.id} value={id} onChange={(e) => setId(e.target.value.toLowerCase())} help={D.idHint} />
      <Field label={D.name} value={name} onChange={(e) => setName(e.target.value)} help={D.nameHint} />
      <Field label={D.metadataUrl} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…/metadata" />
      <label className="block">
        <span className="hz-label">{D.metadataXml}</span>
        <textarea className="hz-input min-h-[90px] font-mono text-[12px]" value={xml} onChange={(e) => setXml(e.target.value)} />
      </label>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>{t.cancel}</Button>
        <Button type="submit" variant="primary" loading={busy}>{D.save}</Button>
      </div>
    </form>
  );
}
