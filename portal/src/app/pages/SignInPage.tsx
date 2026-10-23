// Sign in with a password or an external provider, and create an account when
// self sign-up is on. Every error code from contract 6.1 reads as a sentence.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Building2 } from "lucide-react";
import { t } from "../i18n/en";
import { ApiError, get, post } from "../lib/api";
import { useApp } from "../lib/app-context";
import { describeError } from "../lib/errors";
import { navigate } from "../lib/router";
import type { SessionUser } from "../lib/types";
import { Button, Field, Notice } from "../components/ui";
import { CodeForm, RecoveryCodes, SetupPanel, type Setup } from "../components/Security";
import { AuthLayout } from "./AuthLayout";

const EMAIL = /^[^\s@]+@[^\s@]+$/;

export function authErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "rate_limited" || error.status === 429) return t.errors.rateLimited(error.retryAfter);
    const known = t.auth.codes[error.code];
    if (known) return known;
    if (error.status === 401) return t.auth.codes.invalid_credentials;
  }
  return describeError(error);
}

function ProviderIcon({ id, kind }: { id: string; kind?: string }) {
  if (kind === "saml") return <Building2 size={15} aria-hidden />;
  if (id === "google")
    return (
      <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden>
        <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
        <path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
        <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
        <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
      </svg>
    );
  if (id === "microsoft")
    return (
      <svg width="15" height="15" viewBox="0 0 23 23" aria-hidden>
        <path fill="#f35325" d="M1 1h10v10H1z" />
        <path fill="#81bc06" d="M12 1h10v10H12z" />
        <path fill="#05a6f0" d="M1 12h10v10H1z" />
        <path fill="#ffba08" d="M12 12h10v10H12z" />
      </svg>
    );
  return null;
}

type Stage = "form" | "code" | "setup" | "codes";
type LoginAnswer = { state?: string; user?: SessionUser; email?: string; setup?: Setup;
                     recovery_codes?: string[]; recovery_codes_left?: number };

export default function SignInPage({ redirect, error, step }: { redirect: string; error: string | null; step?: string | null }) {
  const app = useApp();
  const session = app.session;
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [stage, setStage] = useState<Stage>(step === "code" ? "code" : step === "setup" ? "setup" : "form");
  const [who, setWho] = useState("");
  const [setup, setSetup] = useState<Setup | null>(null);
  const [codes, setCodes] = useState<string[]>([]);
  const [useRecovery, setUseRecovery] = useState(false);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [forgot, setForgot] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "success" | "warning"; text: string } | null>(
    session.ended === "suspended" ? { tone: "error", text: t.auth.codes.suspended }
      : error ? { tone: error === "session_ended" ? "warning" : "error", text: t.auth.codes[error] ?? t.auth.oauthFallback } : null,
  );
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string; name?: string }>({});
  const emailRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    emailRef.current?.focus();
  }, [mode]);

  // A provider or a set-password link was the first step: load the set-up again.
  useEffect(() => {
    if (step !== "setup") return;
    get<LoginAnswer>("/api/auth/mfa/setup", { quiet401: true })
      .then((r) => {
        setSetup(r.setup ?? null);
        setWho(r.email ?? "");
      })
      .catch((err) => restart(err));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  const restart = (err: unknown) => {
    setStage("form");
    setSetup(null);
    setCodeError(null);
    setMessage({ tone: "error", text: authErrorText(err) });
  };

  /** What the first step answered: signed in, a code to enter, or set-up. */
  const after = async (res: LoginAnswer | undefined) => {
    if (res?.state === "mfa_required") {
      setWho(res.email ?? email.trim());
      setStage("code");
      return;
    }
    if (res?.state === "mfa_setup") {
      setWho(res.email ?? email.trim());
      setSetup(res.setup ?? null);
      setStage("setup");
      return;
    }
    await finish();
  };

  const verify = async (value: string) => {
    setBusy(true);
    setCodeError(null);
    try {
      const res = await post<LoginAnswer>(
        "/api/auth/mfa/verify",
        useRecovery ? { recovery_code: value } : { code: value },
        { quiet401: true },
      );
      if (typeof res?.recovery_codes_left === "number") sessionStorage.setItem("hz.usedRecovery", String(res.recovery_codes_left));
      await finish();
    } catch (err) {
      if (err instanceof ApiError && (err.code === "step_expired" || err.status === 410)) return restart(err);
      const left = err instanceof ApiError ? (err.detail?.tries_left as number | undefined) : undefined;
      setCodeError(`${authErrorText(err)}${typeof left === "number" ? ` ${t.auth.triesLeft(left)}` : ""}`);
    } finally {
      setBusy(false);
    }
  };

  const confirmSetup = async (value: string) => {
    setBusy(true);
    setCodeError(null);
    try {
      const res = await post<LoginAnswer>("/api/auth/mfa/setup", { code: value }, { quiet401: true });
      setCodes(res?.recovery_codes ?? []);
      setStage("codes");
    } catch (err) {
      if (err instanceof ApiError && (err.code === "step_expired" || err.status === 410)) return restart(err);
      const left = err instanceof ApiError ? (err.detail?.tries_left as number | undefined) : undefined;
      setCodeError(`${authErrorText(err)}${typeof left === "number" ? ` ${t.auth.triesLeft(left)}` : ""}`);
    } finally {
      setBusy(false);
    }
  };

  const providers = session.providers ?? [];
  const passwordOn = session.password !== false;
  const nothing = !passwordOn && providers.length === 0;

  const finish = async () => {
    await app.refreshSession();
    if (redirect.startsWith("/portal")) location.assign(redirect);
    else navigate(redirect, { replace: true });
  };

  const validate = () => {
    const errors: typeof fieldErrors = {};
    if (mode === "signup" && !name.trim()) errors.name = t.auth.missingName;
    if (!email.trim()) errors.email = t.auth.missingEmail;
    else if (!EMAIL.test(email.trim())) errors.email = t.auth.invalidEmail;
    if (!password) errors.password = t.auth.missingPassword;
    else if (mode === "signup" && password.length < 8) errors.password = t.auth.passwordTooShort;
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setMessage(null);
    if (!validate()) return;
    setBusy(true);
    try {
      if (mode === "signin") {
        const res = await post<LoginAnswer>("/api/auth/login", { email: email.trim(), password }, { quiet401: true });
        setPassword("");
        await after(res);
        return;
      }
      const res = await post<{ status?: string }>(
        "/api/auth/signup",
        { email: email.trim(), name: name.trim(), password },
        { quiet401: true },
      );
      if (res?.status === "active") {
        const answer = await post<LoginAnswer>("/api/auth/login", { email: email.trim(), password }, { quiet401: true });
        await after(answer);
        return;
      }
      setMode("signin");
      setPassword("");
      setMessage({ tone: "success", text: t.auth.signupPending });
    } catch (err) {
      const tone = err instanceof ApiError && err.code === "pending" ? "warning" : "error";
      setMessage({ tone, text: authErrorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const brandCopy = app.branding.manifest?.copy;
  const heading =
    mode === "signin" ? brandCopy?.signin_title || t.auth.title(app.brandName) : t.auth.signupTitle(app.brandName);
  const subtitle = mode === "signin" ? brandCopy?.signin_body || t.auth.subtitle : t.auth.signupSubtitle;

  const back = (
    <button type="button" className="mt-4 w-full text-center text-[13px] text-mute hover:text-ink" onClick={() => restart(new ApiError(410, "step_expired", ""))}>
      {t.auth.startOver}
    </button>
  );

  if (stage === "code")
    return (
      <AuthLayout title={t.auth.codeTitle}>
        <div className="hz-signin" data-testid="mfa-step">
          <h1 className="m-0 text-center text-[1.5rem] font-medium tracking-tight text-ink">{t.auth.codeTitle}</h1>
          <p className="m-0 mt-2 text-center text-[0.9286rem] leading-relaxed text-mute">
            {useRecovery ? t.auth.recoveryHelp : t.auth.codeSubtitle(who || t.auth.email.toLowerCase())}
          </p>
          <div className="mt-6">
            {useRecovery ? (
              <RecoveryForm busy={busy} error={codeError} onSubmit={(v) => void verify(v)} />
            ) : (
              <CodeForm busy={busy} error={codeError} onSubmit={(v) => void verify(v)} />
            )}
            <button type="button" className="mt-3 w-full text-center text-[13px] text-accent-text hover:underline"
              onClick={() => { setUseRecovery(!useRecovery); setCodeError(null); }}>
              {useRecovery ? t.auth.useCode : t.auth.useRecovery}
            </button>
            {back}
          </div>
        </div>
      </AuthLayout>
    );

  if (stage === "setup" || stage === "codes")
    return (
      <AuthLayout title={t.auth.setupTitle}>
        <div className="hz-signin">
          <h1 className="m-0 text-center text-[1.5rem] font-medium tracking-tight text-ink">{t.auth.setupTitle}</h1>
          {stage === "setup" && <p className="m-0 mt-2 text-center text-[0.9286rem] leading-relaxed text-mute">{t.auth.setupSubtitle}</p>}
          <div className="mt-6">
            {stage === "codes" ? (
              <RecoveryCodes codes={codes} onDone={() => void finish()} />
            ) : setup ? (
              <SetupPanel setup={setup} busy={busy} error={codeError} onCode={(v) => void confirmSetup(v)} />
            ) : (
              <p className="text-center text-mute">{t.loading}</p>
            )}
            {stage === "setup" && back}
          </div>
        </div>
      </AuthLayout>
    );

  return (
    <AuthLayout title={mode === "signin" ? t.auth.signIn : t.auth.signupSubmit}>
      <div className="hz-signin">
        <h1 className="m-0 text-[1.7143rem] font-medium tracking-tight text-center text-ink">{heading}</h1>
        <p className="m-0 mt-2 whitespace-pre-line text-center text-[0.9286rem] leading-relaxed text-mute">{subtitle}</p>

        {message && (
          <Notice tone={message.tone} className="mt-5">
            {message.text}
          </Notice>
        )}

        {nothing && (
          <Notice tone="warning" className="mt-5">
            {t.auth.noMethods}
          </Notice>
        )}

        {passwordOn && (
          <form onSubmit={submit} noValidate className="mt-7 space-y-4">
            {mode === "signup" && (
              <Field
                label={t.auth.name}
                name="name"
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                error={fieldErrors.name}
                ref={emailRef}
              />
            )}
            <Field
              ref={mode === "signin" ? emailRef : undefined}
              label={t.auth.email}
              name="email"
              type="email"
              autoComplete={mode === "signin" ? "username" : "email"}
              inputMode="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              error={fieldErrors.email}
            />
            <Field
              label={t.auth.password}
              name="password"
              type="password"
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              error={fieldErrors.password}
              help={mode === "signup" ? t.auth.passwordHint : undefined}
            />
            {mode === "signin" && (
              <div className="-mt-2 text-right">
                <button type="button" className="text-[12.5px] text-mute hover:text-ink" onClick={() => setForgot(!forgot)}>
                  {t.auth.forgot}
                </button>
                {forgot && <p className="m-0 mt-1 text-left text-[12.5px] text-mute" role="status">{t.auth.forgotHelp}</p>}
              </div>
            )}
            <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy}>
              {mode === "signin"
                ? busy
                  ? t.auth.signingIn
                  : t.auth.signIn
                : busy
                  ? t.auth.signupSubmitting
                  : t.auth.signupSubmit}
            </Button>
          </form>
        )}
        {mode === "signin" && providers.length > 0 && passwordOn && (
          <div className="my-6 flex items-center gap-3 text-xs text-mute" aria-hidden>
            <span className="h-px flex-1 bg-line" />
            {t.auth.or}
            <span className="h-px flex-1 bg-line" />
          </div>
        )}

        {mode === "signin" && providers.length > 0 && (
          <div className="hz-auth-providers space-y-2.5">
            {providers.map((p) => (
              <a
                key={p.id}
                href={`${p.login_url ?? `/oauth/${encodeURIComponent(p.id)}/login`}?redirect=${encodeURIComponent(redirect)}`}
                className="hz-btn hz-btn-secondary hz-btn-lg w-full no-underline"
              >
                <ProviderIcon id={p.id} kind={p.kind} />
                {t.auth.continueWith(p.name || p.id)}
              </a>
            ))}
          </div>
        )}

      </div>

      {session.signup && passwordOn && (
        <p className="mt-5 text-center text-[14px] text-mute">
          {mode === "signin" ? t.auth.noAccount : t.auth.haveAccount}{" "}
          <button
            type="button"
            className="font-medium text-accent-text underline-offset-4 hover:underline"
            onClick={() => {
              setMode(mode === "signin" ? "signup" : "signin");
              setMessage(null);
              setFieldErrors({});
            }}
          >
            {mode === "signin" ? t.auth.createAccount : t.auth.signIn}
          </button>
        </p>
      )}
    </AuthLayout>
  );
}

function RecoveryForm({ onSubmit, busy, error }: { onSubmit: (v: string) => void; busy?: boolean; error?: string | null }) {
  const [value, setValue] = useState("");
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (value.trim()) onSubmit(value.trim()); }} noValidate className="space-y-3">
      <Field label={t.auth.recoveryCode} autoFocus autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)}
        error={error} placeholder="XXXX-XXXX-XXXX-XXXX" data-testid="mfa-recovery" />
      <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy} disabled={!value.trim()}>
        {busy ? t.auth.verifying : t.auth.verify}
      </Button>
    </form>
  );
}
