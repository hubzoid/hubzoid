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
import { setDensity, setTextScale, usePresentation } from "../../lib/presentation";
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
    if (!current) found.current = t.auth.missingPassword;
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

/** Appearance, text size and spacing: the person's account preferences,
 *  within what the deployment allows. Locked choices say who manages them. */
function AppearanceForm() {
  const view = usePresentation();
  const choice = <T extends string | number>(
    label: string,
    value: T,
    options: T[],
    render: (v: T) => string,
    onPick: (v: T) => void,
    locked: boolean,
  ) => (
    <div>
      <div className="hz-label" id={`pref-${label.replace(/\W+/g, "-")}`}>{label}</div>
      {locked ? (
        <p className="m-0 text-[15px] text-body">
          {render(value)} <span className="text-mute">· {t.theme.managed}</span>
        </p>
      ) : (
        <div role="radiogroup" aria-labelledby={`pref-${label.replace(/\W+/g, "-")}`} className="inline-flex flex-wrap gap-1 rounded-lg border border-line bg-raised p-0.5">
          {options.map((o) => (
            <button
              key={String(o)}
              type="button"
              role="radio"
              aria-checked={o === value}
              onClick={() => onPick(o)}
              className={`min-h-9 rounded-md px-3 text-sm ${o === value ? "bg-sunken text-ink" : "text-mute hover:text-ink"}`}
            >
              {render(o)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
  return (
    <div className="space-y-5">
      <div>
        <div className="hz-label">{t.theme.label}</div>
        {view.locked.mode ? (
          <p className="m-0 text-[15px] text-body">
            {t.theme[view.mode]} <span className="text-mute">· {t.theme.managed}</span>
          </p>
        ) : (
          <ThemeSwitch />
        )}
      </div>
      {choice(t.theme.textSize, view.textScale, view.allowed.textScales, (v) => t.theme.scale(v), setTextScale, view.locked.text_scale)}
      {choice(
        t.theme.density,
        view.density,
        view.allowed.densities,
        (v) => (v === "compact" ? t.theme.compact : t.theme.comfortable),
        setDensity,
        view.locked.density,
      )}
      <p className="m-0 text-xs text-mute" aria-live="polite">
        {view.saveError ? t.theme.saveFailed : view.saving ? t.theme.saving : view.signedIn ? t.theme.appearanceHint : ""}
      </p>
    </div>
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
        <AppearanceForm />
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
