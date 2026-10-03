// Set or reset a password from a one-time link (/auth/set-password?token=).
import { useEffect, useState, type FormEvent } from "react";
import { t } from "../i18n/en";
import { enc, get, post } from "../lib/api";
import { useApp } from "../lib/app-context";
import { linkClick, navigate } from "../lib/router";
import type { AuthLink } from "../lib/types";
import { Button, Field, Notice, Spinner } from "../components/ui";
import { AuthLayout } from "./AuthLayout";
import { authErrorText } from "./SignInPage";

export default function SetPasswordPage({ token: linkToken }: { token: string | null }) {
  const app = useApp();
  // The one-time token leaves the address bar (and the browser history) once
  // read; this page keeps it until the password is set.
  const [token] = useState(linkToken);
  useEffect(() => {
    if (linkToken) navigate(location.pathname, { replace: true });
  }, [linkToken]);
  const [link, setLink] = useState<AuthLink | null>(null);
  const [status, setStatus] = useState<"checking" | "ready" | "invalid">(token ? "checking" : "invalid");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<{ password?: string; confirm?: string }>({});
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    get<AuthLink>(`/api/auth/link/${enc(token)}`, { quiet401: true })
      .then((data) => {
        if (cancelled) return;
        setLink(data);
        setStatus(data?.valid ? "ready" : "invalid");
      })
      .catch(() => !cancelled && setStatus("invalid"));
    return () => {
      cancelled = true;
    };
  }, [token]);

  // The server names the purposes set_password and reset_password.
  const reset = link?.purpose === "reset_password" || link?.purpose === "reset";
  const title = reset ? t.setPassword.titleReset : t.setPassword.titleSet;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token || busy) return;
    const next: typeof errors = {};
    if (password.length < 8) next.password = t.auth.passwordTooShort;
    if (!next.password && confirm !== password) next.confirm = t.setPassword.mismatch;
    setErrors(next);
    setMessage(null);
    if (Object.keys(next).length) return;
    setBusy(true);
    try {
      await post(`/api/auth/link/${enc(token)}`, { password }, { quiet401: true });
      await app.refreshSession();
      navigate("/", { replace: true });
    } catch (err) {
      setMessage(authErrorText(err));
      setBusy(false);
    }
  };

  return (
    <AuthLayout title={status === "invalid" ? t.setPassword.invalidTitle : title}>
      <div className="hz-card px-6 py-7 sm:px-8 sm:py-8">
        {status === "checking" && (
          <div className="flex items-center gap-3 text-sm text-mute">
            <Spinner size={16} label={t.setPassword.checking} />
            {t.setPassword.checking}
          </div>
        )}
        {status === "invalid" && (
          <>
            <h1 className="m-0 text-[22px] font-semibold tracking-tight text-ink">{t.setPassword.invalidTitle}</h1>
            <p className="m-0 mt-2 text-[14.5px] leading-relaxed text-mute">
              {token ? t.setPassword.invalid : t.setPassword.missingToken}
            </p>
            <a href="/auth" onClick={(e) => linkClick(e, "/auth")} className="hz-btn hz-btn-secondary mt-6 no-underline">
              {t.setPassword.toSignIn}
            </a>
          </>
        )}
        {status === "ready" && (
          <>
            <h1 className="m-0 text-[22px] font-semibold tracking-tight text-ink">{title}</h1>
            {link?.email && <p className="m-0 mt-1.5 text-[14px] text-mute">{t.setPassword.subtitle(link.email)}</p>}
            {message && (
              <Notice tone="error" className="mt-5">
                {message}
              </Notice>
            )}
            <form onSubmit={submit} noValidate className="mt-6 space-y-4">
              {link?.email && (
                <input type="email" name="username" autoComplete="username" value={link.email} readOnly hidden />
              )}
              <Field
                label={t.setPassword.newPassword}
                type="password"
                autoComplete="new-password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                error={errors.password}
                help={t.auth.passwordHint}
              />
              <Field
                label={t.setPassword.confirm}
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                error={errors.confirm}
              />
              <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy}>
                {busy ? t.setPassword.submitting : t.setPassword.submit}
              </Button>
            </form>
          </>
        )}
      </div>
    </AuthLayout>
  );
}
