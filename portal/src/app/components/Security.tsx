// Pieces shared by sign-in and Settings > Security: the 6-digit code input,
// the authenticator set-up panel, recovery codes to save, and the "Confirm
// it's you" dialog that a sensitive change asks for.
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Copy, Download } from "lucide-react";
import { t } from "../i18n/en";
import { ApiError, post } from "../lib/api";
import { Button, Field, Modal, Notice } from "./ui";

export type Setup = { qr: string; key: string; uri: string; issuer?: string };

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** One field for a 6-digit code: numbers only, paste works, submits on the
 *  sixth digit, and a Verify button for keyboards and screen readers. */
export function CodeForm({
  onSubmit,
  busy,
  error,
  label = t.auth.code,
  submitLabel = t.auth.verify,
  autoFocus = true,
  footer,
}: {
  onSubmit: (code: string) => void;
  busy?: boolean;
  error?: string | null;
  label?: string;
  submitLabel?: string;
  autoFocus?: boolean;
  footer?: ReactNode;
}) {
  const [code, setCode] = useState("");
  const ref = useRef<HTMLInputElement>(null);
  const sent = useRef("");
  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);
  useEffect(() => {
    if (error) {
      setCode("");
      sent.current = "";
      ref.current?.focus();
    }
  }, [error]);
  const change = (value: string) => {
    const digits = value.replace(/\D/g, "").slice(0, 6);
    setCode(digits);
    if (digits.length === 6 && !busy && sent.current !== digits) {
      sent.current = digits;
      onSubmit(digits);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (code.length === 6 && !busy) {
      sent.current = code;
      onSubmit(code);
    }
  };
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <Field
        ref={ref}
        label={label}
        name="one-time-code"
        autoComplete="one-time-code"
        inputMode="numeric"
        pattern="[0-9]*"
        maxLength={7}
        value={code}
        onChange={(e) => change(e.target.value)}
        error={error}
        className="hz-code-field"
        data-testid="mfa-code"
      />
      <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy} disabled={code.length !== 6}>
        {busy ? t.auth.verifying : submitLabel}
      </Button>
      {footer}
    </form>
  );
}

/** Scan the QR code (or type the key), then enter the first code. */
export function SetupPanel({ setup, onCode, busy, error }: { setup: Setup; onCode: (code: string) => void; busy?: boolean; error?: string | null }) {
  const [copied, setCopied] = useState<string | null>(null);
  return (
    <div className="space-y-4" data-testid="mfa-setup">
      <p className="m-0 text-[13.5px] text-body">{t.security.step1}</p>
      <div className="flex justify-center">
        <img src={setup.qr} alt="" width={176} height={176} className="rounded-lg bg-white p-2" />
      </div>
      <div className="text-[12.5px] text-mute">
        {t.security.cantScan}
        <div className="mt-1 flex items-center gap-2">
          <code className="select-all break-all rounded bg-sunken px-2 py-1 text-[13px] text-ink" data-testid="mfa-key">
            {setup.key}
          </code>
          <Button size="sm" variant="ghost" icon={<Copy size={13} aria-hidden />} onClick={async () => setCopied((await copy(setup.key.replace(/\s/g, ""))) ? t.security.copied : t.security.copyFailed)}>
            {t.security.copyKey}
          </Button>
        </div>
        {copied && <p className="m-0 mt-1" aria-live="polite">{copied}</p>}
      </div>
      <p className="m-0 text-[13.5px] text-body">{t.security.step2}</p>
      <CodeForm onSubmit={onCode} busy={busy} error={error} autoFocus={false} />
    </div>
  );
}

/** Ten recovery codes, shown once: copy, download, and confirm they're saved. */
export function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const [saved, setSaved] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const text = codes.join("\n");
  const download = () => {
    const url = URL.createObjectURL(new Blob([`Hubzoid recovery codes\n\n${text}\n`], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "hubzoid-recovery-codes.txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="space-y-4" data-testid="recovery-codes">
      <div>
        <h3 className="m-0 text-[14px] font-semibold text-ink">{t.security.codesTitle}</h3>
        <p className="m-0 mt-1 text-[13px] leading-relaxed text-mute">{t.security.codesHelp}</p>
      </div>
      <ol className="m-0 grid list-none grid-cols-2 gap-x-6 gap-y-1.5 rounded-lg bg-sunken p-3 font-mono text-[13px] text-ink">
        {codes.map((c) => (
          <li key={c} className="select-all">{c}</li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" icon={<Copy size={13} aria-hidden />} onClick={async () => setNote((await copy(text)) ? t.security.copied : t.security.copyFailed)}>
          {t.security.copyCodes}
        </Button>
        <Button size="sm" icon={<Download size={13} aria-hidden />} onClick={download}>
          {t.security.downloadCodes}
        </Button>
        {note && <span className="self-center text-[12.5px] text-mute" aria-live="polite">{note}</span>}
      </div>
      <label className="flex items-center gap-2 text-[13.5px]">
        <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="codes-saved" />
        {t.security.saved}
      </label>
      <Button variant="primary" className="w-full" disabled={!saved} onClick={onDone}>
        {t.security.done}
      </Button>
    </div>
  );
}

type ReauthOptions = {
  password?: boolean;
  code?: boolean;
  providers?: string[];
  company?: { id: string; name: string };
};

/** Run ``action``; if the server answers "confirm it's you", ask in a small
 *  dialog, then run it again. */
export function useReauth() {
  const [ask, setAsk] = useState<{ options: ReauthOptions; retry: () => Promise<void>; reject: (e: unknown) => void } | null>(null);
  const run = async <T,>(action: () => Promise<T>): Promise<T> => {
    try {
      return await action();
    } catch (err) {
      if (!(err instanceof ApiError) || err.code !== "reauth_required") throw err;
      const options = (err.detail?.reauth as ReauthOptions) ?? { password: true };
      return await new Promise<T>((resolve, reject) => {
        setAsk({
          options,
          retry: async () => {
            resolve(await action());
          },
          reject,
        });
      });
    }
  };
  const dialog = ask ? (
    <ReauthDialog
      options={ask.options}
      onClose={() => {
        ask.reject(new ApiError(403, "reauth_cancelled", t.reauth.help));
        setAsk(null);
      }}
      onVerified={async () => {
        const pending = ask;
        setAsk(null);
        try {
          await pending.retry();
        } catch (e) {
          pending.reject(e);
        }
      }}
    />
  ) : null;
  return { run, dialog };
}

function ReauthDialog({ options, onClose, onVerified }: { options: ReauthOptions; onClose: () => void; onVerified: () => void }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/reauth", { password, code }, { quiet401: true });
      onVerified();
    } catch (err) {
      setError(err instanceof ApiError && err.hasServerMessage ? err.message : t.auth.codes.invalid_credentials);
    } finally {
      setBusy(false);
    }
  };
  const back = location.pathname + location.hash;
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={t.reauth.title} description={t.reauth.help}>
      {!options.password && !options.code ? (
        <div className="space-y-2">
          {options.company && (
            <a className="hz-btn hz-btn-secondary w-full no-underline" href={`/api/auth/saml/${encodeURIComponent(options.company.id)}/login?reauth=1&redirect=${encodeURIComponent(back)}`}>
              {t.reauth.withProvider(options.company.name)}
            </a>
          )}
          {(options.providers ?? []).map((p) => (
            <a key={p} className="hz-btn hz-btn-secondary w-full no-underline" href={`/oauth/${encodeURIComponent(p)}/login?reauth=1&redirect=${encodeURIComponent(back)}`}>
              {t.reauth.withProvider(p === "google" ? "Google" : p)}
            </a>
          ))}
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-3" noValidate data-testid="reauth">
          {error && <Notice tone="error">{error}</Notice>}
          {options.password && (
            <Field label={t.reauth.password} type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
          )}
          {options.code && (
            <Field label={t.reauth.code} inputMode="numeric" autoComplete="one-time-code" autoFocus={!options.password} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} />
          )}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" onClick={onClose}>{t.cancel}</Button>
            <Button type="submit" variant="primary" loading={busy}>{t.reauth.confirm}</Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
