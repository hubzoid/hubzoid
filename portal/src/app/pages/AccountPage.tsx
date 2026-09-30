// Account: display name, password, appearance and sign out.
import { useState, type FormEvent } from "react";
import { LogOut } from "lucide-react";
import { t } from "../i18n/en";
import { ApiError, patch, post } from "../lib/api";
import { useApp } from "../lib/app-context";
import { describeError } from "../lib/errors";
import type { SessionUser } from "../lib/types";
import { toast } from "../components/toast";
import { Button, Field, Notice } from "../components/ui";
import { ThemeSwitch } from "./AuthLayout";
import { Section, SettingsFrame } from "./SettingsFrame";
import { authErrorText } from "./SignInPage";

function ProfileForm() {
  const app = useApp();
  const [name, setName] = useState(app.user?.name ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = name.trim() !== (app.user?.name ?? "").trim();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setError(t.account.nameRequired);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await patch<{ user?: SessionUser }>("/api/auth/me", { name: name.trim() });
      app.setUser(res?.user ?? { ...(app.user as SessionUser), name: name.trim() });
      toast(t.account.nameSaved);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <Field
        label={t.account.displayName}
        autoComplete="name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        error={error}
        maxLength={120}
      />
      <div>
        <div className="hz-label">{t.account.email}</div>
        <p className="m-0 text-[15px] text-body">{app.user?.email}</p>
      </div>
      <div>
        <div className="hz-label">{t.account.role}</div>
        <p className="m-0 text-[15px] text-body">{app.isAdmin ? t.account.admin : t.account.member}</p>
      </div>
      <Button type="submit" variant="primary" loading={busy} disabled={!dirty}>
        {busy ? t.saving : t.save}
      </Button>
    </form>
  );
}

function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<{ current?: string; next?: string; confirm?: string }>({});
  const [message, setMessage] = useState<{ tone: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const found: typeof errors = {};
    if (!current) found.current = t.auth.missingFields;
    if (next.length < 8) found.next = t.auth.passwordTooShort;
    else if (next !== confirm) found.confirm = t.account.passwordMismatch;
    setErrors(found);
    setMessage(null);
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      await post("/api/auth/password", { current_password: current, new_password: next }, { quiet401: true });
      setCurrent("");
      setNext("");
      setConfirm("");
      setMessage({ tone: "success", text: t.account.passwordChanged });
    } catch (err) {
      if (
        err instanceof ApiError &&
        (err.code === "invalid_credentials" ||
          err.code === "wrong_password" ||
          ((err.status === 401 || err.status === 403) && err.code !== "unauthenticated"))
      )
        setErrors({ current: t.account.wrongPassword });
      else setMessage({ tone: "error", text: authErrorText(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <Field
        label={t.account.currentPassword}
        type="password"
        autoComplete="current-password"
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
        error={errors.current}
      />
      <Field
        label={t.account.newPassword}
        type="password"
        autoComplete="new-password"
        value={next}
        onChange={(e) => setNext(e.target.value)}
        error={errors.next}
        help={t.auth.passwordHint}
      />
      <Field
        label={t.account.confirmPassword}
        type="password"
        autoComplete="new-password"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
        error={errors.confirm}
      />
      <Button type="submit" variant="primary" loading={busy}>
        {t.account.changePassword}
      </Button>
    </form>
  );
}

export default function AccountPage() {
  const app = useApp();
  const [signingOut, setSigningOut] = useState(false);

  const signOut = async () => {
    setSigningOut(true);
    try {
      await post("/api/auth/logout", undefined, { quiet401: true });
      location.assign("/auth");
    } catch (err) {
      setSigningOut(false);
      if ((err as { status?: number })?.status === 401) location.assign("/auth");
      else toast(t.account.signOutFailed, "error");
    }
  };

  return (
    <SettingsFrame title={t.account.title} current="/account">
      {app.isLocal ? (
        <Notice tone="info" className="mb-8">
          {t.account.localNote}
        </Notice>
      ) : (
        <>
          <Section title={t.account.profile} description={t.account.profileHelp}>
            <ProfileForm />
          </Section>
          <Section title={t.account.password} description={t.account.passwordHelp}>
            <PasswordForm />
          </Section>
        </>
      )}
      <Section title={t.account.appearance} description={t.account.appearanceHelp}>
        <ThemeSwitch />
      </Section>
      {!app.isLocal && (
        <Section title={t.account.session} description={t.account.sessionHelp}>
          <Button icon={<LogOut size={15} aria-hidden />} loading={signingOut} onClick={() => void signOut()}>
            {signingOut ? t.account.signingOut : t.account.signOut}
          </Button>
        </Section>
      )}
    </SettingsFrame>
  );
}
