import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Drawer,
  Input,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Copy, KeyRound, Link2 } from "lucide-react";
import {
  type ApiError,
  request,
  type AccountCreatedWithLink,
  type AccountGranted,
  type Hub,
  type Me,
  type SignIn,
  type SignInOptions,
  shareableLink,
  usesSignInLinks,
} from "../../api";
import { useCatalogs } from "../../hooks/useCatalogs";
import { personHref, useNavigationGuard } from "../../hooks/useRoute";
import { MANAGE_ACCESS, USE_HUB, capabilityLabel, formatTime, groupCapabilities, isGrantable, splitSections } from "../../lib/format";
import { orderCapabilities, toggle } from "../access/plan";
import { CapabilityGroup, CapabilitySection } from "../access/AccessParts";
import { generatePassword, passwordProblem } from "./password";
import { asApiError, emailProblem, googleDomainProblem, partialDetail } from "./accountRules";

const { Text, Title, Paragraph } = Typography;

export function PasswordField({
  id,
  value,
  onChange,
  touched,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  touched?: boolean;
}) {
  const problem = touched ? passwordProblem(value) : null;
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        Password
      </label>
      <Space.Compact style={{ width: "100%" }}>
        <Input.Password
          id={id}
          autoComplete="new-password"
          value={value}
          status={problem ? "error" : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
        <Button icon={<KeyRound size={16} />} onClick={() => onChange(generatePassword())}>
          Generate
        </Button>
      </Space.Compact>
      <Text type={problem ? "danger" : "secondary"} className="field-help">
        {problem ?? "Shown once after saving so you can share it. It is not stored in the Console."}
      </Text>
    </div>
  );
}

/** Shows a password once, with Copy. The caller clears it when closing. */
export function OneTimePassword({ password }: { password: string }) {
  const { message } = App.useApp();
  async function copy() {
    try {
      await navigator.clipboard.writeText(password);
      message.success("Password copied.");
    } catch {
      message.warning("Couldn’t copy. Select the password and copy it yourself.");
    }
  }
  return (
    <div className="field">
      <label className="field-label" htmlFor="one-time-password">
        Password
      </label>
      <Space.Compact style={{ width: "100%" }}>
        <Input id="one-time-password" readOnly value={password} className="identity" />
        <Button icon={<Copy size={16} />} onClick={() => void copy()}>
          Copy
        </Button>
      </Space.Compact>
      <Text type="secondary" className="field-help">
        Share it with them directly. It won’t be shown again. They can change it after signing in.
      </Text>
    </div>
  );
}

/** Shows a one-time sign-in link once, with Copy and when it expires. The
 *  caller drops it when closing; the server keeps only a digest. */
export function OneTimeLink({ link, expiresAt }: { link: string; expiresAt?: number }) {
  const { message } = App.useApp();
  const url = shareableLink(link);
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      message.success("Sign-in link copied.");
    } catch {
      message.warning("Couldn’t copy. Select the link and copy it yourself.");
    }
  }
  return (
    <div className="field">
      <label className="field-label" htmlFor="one-time-link">
        One-time sign-in link
      </label>
      <Space.Compact style={{ width: "100%" }}>
        <Input id="one-time-link" readOnly value={url} className="identity" onFocus={(e) => e.target.select()} />
        <Button icon={<Copy size={16} />} onClick={() => void copy()}>
          Copy sign-in link
        </Button>
      </Space.Compact>
      <Text type="secondary" className="field-help">
        Share it with them directly. It works once{expiresAt ? `, until ${formatTime(expiresAt)}` : ""}, and lets them
        set their own password. It won’t be shown again.
      </Text>
    </div>
  );
}

/**
 * How a new user signs in: a password (typed or generated), or "Google
 * sign-in only" beside it. Google is offered only when the chat app attaches a
 * Google sign-in to an existing account by email; otherwise the choice is
 * shown disabled with a short hint. Choosing Google hides the password.
 */
export function NewUserSignIn({
  signIn,
  onSignIn,
  options,
  password,
  onPassword,
  touched,
  id,
  links,
}: {
  signIn: SignIn;
  onSignIn: (v: SignIn) => void;
  options?: SignInOptions;
  password: string;
  onPassword: (v: string) => void;
  touched?: boolean;
  id: string;
  /** Hubzoid accounts: they set their own password with a one-time link. */
  links?: boolean;
}) {
  const available = !!options?.google;
  const google = available && signIn === "google";
  const domains = options?.google_domains?.filter(Boolean) ?? [];
  const problem = touched && !google && !links ? passwordProblem(password) : null;
  const choice = (
    <Checkbox
      checked={google}
      disabled={!available}
      onChange={(e) => onSignIn(e.target.checked ? "google" : "password")}
    >
      Google sign-in only
    </Checkbox>
  );
  return (
    <div className="field">
      <div className="field-label-row">
        <label className="field-label" htmlFor={id}>
          Password
        </label>
        {available ? (
          choice
        ) : (
          <Tooltip
            title={
              links
                ? "Needs Google sign-in set up here, with OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true."
                : "Needs Google sign-in set up for the chat app, with OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true."
            }
          >
            <span>{choice}</span>
          </Tooltip>
        )}
      </div>
      {google ? (
        <Text type="secondary" className="field-help">
          They sign in with Google using this email{domains.length ? ` (${domains.join(", ")} only)` : ""}. No
          password is set that anyone knows.
        </Text>
      ) : links ? (
        <Text type="secondary" className="field-help" id={id}>
          <Link2 size={14} style={{ verticalAlign: "-2px" }} /> They set their own password with a one-time sign-in
          link. You copy the link after saving and share it with them.
        </Text>
      ) : (
        <>
          <Space.Compact style={{ width: "100%" }}>
            <Input.Password
              id={id}
              autoComplete="new-password"
              value={password}
              status={problem ? "error" : undefined}
              onChange={(e) => onPassword(e.target.value)}
            />
            <Button icon={<KeyRound size={16} />} onClick={() => onPassword(generatePassword())}>
              Generate
            </Button>
          </Space.Compact>
          <Text type={problem ? "danger" : "secondary"} className="field-help">
            {problem ?? "Shown once after saving so you can share it. It is not stored in the Console."}
          </Text>
        </>
      )}
    </div>
  );
}

/**
 * The sign-in details to share once an account exists: the chat address, the
 * email and, for a password account, the password with Copy. Nothing here is
 * stored; the caller drops the password when the drawer closes.
 */
export function SignInDetails({
  email,
  password,
  signIn,
  link,
  expiresAt,
  linkError,
}: {
  email: string;
  password: string;
  signIn: SignIn;
  /** Hubzoid accounts: the one-time link to set their password. */
  link?: string | null;
  expiresAt?: number;
  linkError?: string;
}) {
  const { message } = App.useApp();
  const chat = `${window.location.origin}/`;
  if (signIn === "google")
    return (
      <Paragraph style={{ margin: 0 }}>
        They sign in at <Text className="identity">{chat}</Text> with Google as{" "}
        <Text className="identity">{email}</Text>. There is no password to share.
      </Paragraph>
    );
  if (link || linkError || (!password && link !== undefined))
    return (
      <>
        <Paragraph style={{ margin: 0 }}>
          They open the link, set a password and are signed in. Afterwards they sign in at{" "}
          <Text className="identity">{chat}</Text> as <Text className="identity">{email}</Text>.
        </Paragraph>
        {link ? (
          <OneTimeLink link={link} expiresAt={expiresAt} />
        ) : (
          <Alert
            type="warning"
            showIcon
            title="The sign-in link couldn’t be made"
            description={linkError || "Open their details and use Reset password to make a new link."}
          />
        )}
      </>
    );
  async function copyAll() {
    try {
      await navigator.clipboard.writeText(`Sign in at ${chat}\nEmail: ${email}\nPassword: ${password}`);
      message.success("Sign-in details copied.");
    } catch {
      message.warning("Couldn’t copy. Copy the email and password yourself.");
    }
  }
  return (
    <>
      <Paragraph style={{ margin: 0 }}>
        They sign in at <Text className="identity">{chat}</Text> as <Text className="identity">{email}</Text>.
      </Paragraph>
      <OneTimePassword password={password} />
      <div>
        <Button icon={<Copy size={16} />} onClick={() => void copyAll()}>
          Copy sign-in details
        </Button>
      </div>
    </>
  );
}

type Step = "edit" | "review" | "done" | "exists" | "partial" | "uncertain" | "failed";

/** Add user from People: a new account with initial access in agents the viewer manages. */
export function AccountDrawer({
  open,
  me,
  hubs,
  onClose,
  onCreated,
}: {
  open: boolean;
  me: Me;
  hubs: Hub[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { modal } = App.useApp();
  const [step, setStep] = useState<Step>("edit");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [signIn, setSignIn] = useState<SignIn>("password");
  const [password, setPassword] = useState("");
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  // Agent sections and their optional capability groups start collapsed.
  const [openKeys, setOpenKeys] = useState<string[]>([]);
  const flip = (key: string) =>
    setOpenKeys((keys) => (keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key]));
  const [touched, setTouched] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  // A retry of "grant access" that failed, shown under the outcome it retried.
  const [retryError, setRetryError] = useState<ApiError | null>(null);
  const [created, setCreated] = useState<AccountCreatedWithLink | null>(null);
  // The previous attempt's outcome was unknown: a duplicate now may be that attempt.
  const [afterUncertain, setAfterUncertain] = useState(false);

  const grantable = me.grantable ?? {};
  const options = me.sign_in;
  const google = signIn === "google" && !!options?.google;
  // Hubzoid accounts: no password is set here; they get a one-time link.
  const links = usesSignInLinks(me);
  // Agents this viewer manages, in the Console's order. Legacy agents are
  // listed but take no grants here: their access is still in the chat app.
  const managed = hubs.filter((h) => h.key in grantable);
  const catalogs = useCatalogs(managed.map((h) => h.key));

  const subject = email.trim().toLowerCase();
  const emailIssue = emailProblem(email) ?? (google ? googleDomainProblem(subject, options) : null);
  const nameProblem = name.trim() ? null : "Enter their name.";
  const grants = useMemo(
    () =>
      Object.entries(selected).flatMap(([hub, perms]) =>
        perms.map((permission) => ({ hub, permission })),
      ),
    [selected],
  );
  const needsGrant = !me.org_admin && grants.length === 0;
  const invalid = !!(emailIssue || nameProblem || (!google && !links && passwordProblem(password)) || needsGrant);
  // Unsaved input exists only before saving; after a result there is nothing to lose.
  const dirty =
    (step === "edit" || step === "review") && (!!email || !!name || !!password || grants.length > 0);

  const reset = useCallback(() => {
    setStep("edit");
    setBusy(false);
    setEmail("");
    setName("");
    setSignIn("password");
    setPassword("");
    setSelected({});
    setOpenKeys([]);
    setTouched(false);
    setFailure(null);
    setRetryError(null);
    setCreated(null);
    setAfterUncertain(false);
  }, []);
  const guard = useMemo(
    () => (open ? { dirty, busy, discard: reset } : null),
    [open, dirty, busy, reset],
  );
  useNavigationGuard(guard);

  function finish() {
    reset(); // drops the password from memory
    onClose();
  }

  function close() {
    if (busy) return;
    if (!dirty) {
      finish();
      return;
    }
    modal.confirm({
      title: "Discard this user?",
      content: "Nothing has been created yet. What you entered will be lost.",
      okText: "Discard",
      okButtonProps: { danger: true },
      cancelText: "Keep editing",
      onOk: finish,
    });
  }

  async function save() {
    setBusy(true);
    setRetryError(null);
    try {
      const result = await request<AccountCreatedWithLink>("/accounts", {
        email: subject,
        name: name.trim(),
        sign_in: google ? "google" : "password",
        ...(google || links ? {} : { password }),
        grants,
      });
      setCreated(result);
      setFailure(null);
      setStep("done");
    } catch (e) {
      const err = asApiError(e);
      setFailure(err);
      if (err.code === "account_exists") setStep("exists");
      else if (err.code === "partial") setStep("partial");
      else if (!err.certain) {
        setAfterUncertain(true);
        setStep("uncertain");
      } else setStep("failed");
    } finally {
      setBusy(false);
      onCreated(); // refresh People: something may have changed
    }
  }

  /** Grant the chosen access to the account a partial create made. Never
   *  creates a second account, and never touches an account made elsewhere. */
  async function grantExisting() {
    setBusy(true);
    setRetryError(null);
    try {
      const result = await request<AccountGranted>("/accounts/grant", { email: subject, grants });
      // The account was made here, with the password (or its link) still on screen.
      setCreated({
        ok: true,
        subject,
        name: name.trim(),
        grants: result.grants,
        sign_in: google ? "google" : "password",
        ...partialLink(failure),
      });
      setStep("done");
    } catch (e) {
      const err = asApiError(e);
      // Agents granted before a later one failed need no retry.
      const done = (err.data.granted ?? {}) as Record<string, string[]>;
      if (Object.keys(done).length)
        setSelected((all) => Object.fromEntries(Object.entries(all).filter(([h]) => !(h in done))));
      setRetryError(err);
    } finally {
      setBusy(false);
      onCreated();
    }
  }

  const hubName = (key: string) => hubs.find((h) => h.key === key)?.name ?? key;
  const accessLine = (g: Record<string, string[]>) =>
    Object.keys(g).length ? `Access granted in ${Object.keys(g).map(hubName).join(", ")}.` : "No agent access was granted yet.";

  const title = busy
    ? "Saving…"
    : {
        edit: "Add user",
        review: "Review the new user",
        done: "User added",
        exists: "This user already exists",
        partial: "Account created, access not granted",
        uncertain: "Not confirmed",
        failed: "Nothing was created",
      }[step];

  const footer =
    step === "edit" ? (
      <Space className="drawer-actions">
        <Button onClick={close}>Cancel</Button>
        <Button
          type="primary"
          disabled={touched && invalid}
          onClick={() => {
            setTouched(true);
            if (!invalid) setStep("review");
          }}
        >
          Review
        </Button>
      </Space>
    ) : step === "review" ? (
      <Space className="drawer-actions">
        <Button disabled={busy} onClick={() => setStep("edit")}>Back</Button>
        <Button type="primary" loading={busy} onClick={() => void save()}>
          Create account
        </Button>
      </Space>
    ) : step === "exists" ? (
      <Space className="drawer-actions" wrap>
        <Button disabled={busy} onClick={finish}>Cancel</Button>
        <Button type="primary" href={personHref(subject)} onClick={finish}>
          Edit this user
        </Button>
      </Space>
    ) : step === "partial" ? (
      <Space className="drawer-actions" wrap>
        <Button disabled={busy} onClick={finish}>Done</Button>
        {grants.length > 0 && (
          <Button type="primary" loading={busy} onClick={() => void grantExisting()}>
            Try again
          </Button>
        )}
      </Space>
    ) : step === "uncertain" ? (
      <Space className="drawer-actions" wrap>
        <Button disabled={busy} onClick={finish}>Done</Button>
        <Button type="primary" loading={busy} onClick={() => void save()}>
          Try again
        </Button>
      </Space>
    ) : step === "failed" ? (
      <Space className="drawer-actions">
        <Button onClick={() => setStep("edit")}>Back to the form</Button>
        <Button type="primary" onClick={finish}>
          Done
        </Button>
      </Space>
    ) : (
      <Space className="drawer-actions">
        <Button type="primary" onClick={finish}>
          Done
        </Button>
      </Space>
    );

  return (
    <Drawer
      title={title}
      aria-label={title}
      open={open}
      onClose={step === "edit" || step === "review" ? close : finish}
      size={520}
      closable={!busy}
      mask={{ closable: !busy }}
      keyboard={!busy}
      footer={footer}
      destroyOnHidden
    >
      <div className="drawer-body">
        {me.accounts_configured === false && (
          <Alert
            type="warning"
            showIcon
            title="Account management isn’t set up on this server"
            description="The Console needs the chat app’s internal address and its service account (HUBZOID_GATEWAY_ADMIN_EMAIL and _PASSWORD). Until then, accounts can’t be created here."
          />
        )}

        {step === "edit" && (
          <>
            <div className="field">
              <label className="field-label" htmlFor="account-name">
                Name
              </label>
              <Input
                id="account-name"
                autoFocus
                autoComplete="off"
                value={name}
                status={touched && nameProblem ? "error" : undefined}
                onChange={(e) => setName(e.target.value)}
              />
              <Text type={touched && nameProblem ? "danger" : "secondary"} className="field-help">
                {touched && nameProblem ? nameProblem : "Shown in the chat app and here."}
              </Text>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="account-email">
                Email address
              </label>
              <Input
                id="account-email"
                autoComplete="off"
                placeholder="name@example.com"
                value={email}
                status={touched && emailIssue ? "error" : undefined}
                onChange={(e) => setEmail(e.target.value)}
              />
              <Text type={touched && emailIssue ? "danger" : "secondary"} className="field-help">
                {touched && emailIssue ? emailIssue : "They sign in with this email. Access is granted to it."}
              </Text>
            </div>
            <NewUserSignIn
              id="account-password"
              signIn={signIn}
              onSignIn={setSignIn}
              options={options}
              password={password}
              onPassword={setPassword}
              touched={touched}
              links={links}
            />

            <div className="section">
              <Title level={2} style={{ fontSize: 16 }}>Initial access</Title>
              {touched && needsGrant && (
                <Alert type="error" showIcon title="Choose access in at least one agent you manage." />
              )}
              {managed.length === 0 && <Text type="secondary">You don’t manage any agent.</Text>}
              {managed.map((hub) => {
                const allowed = new Set(grantable[hub.key] ?? []);
                const catalog = catalogs[hub.key] ?? {};
                const chosen = selected[hub.key] ?? [];
                const agentKey = `agent:${hub.key}`;
                // Included and obsolete capabilities have nothing to grant.
                const groups = groupCapabilities(
                  Object.values(catalog).filter((p) => isGrantable(p)),
                  [],
                );
                return (
                  <CapabilityGroup
                    key={hub.key}
                    id={`new-user-${hub.key}`}
                    title={hub.name}
                    collapsible
                    open={openKeys.includes(agentKey)}
                    onToggle={() => flip(agentKey)}
                    selected={chosen.length}
                    unconfigured={0}
                    problems={0}
                  >
                    {!hub.authoritative ? (
                      <Text type="secondary">Access to this agent is managed in the chat app.</Text>
                    ) : (
                      <div className="capabilities" role="group" aria-label={`Access to ${hub.name}`}>
                        {groups.map((g) => {
                          const groupKey = `${hub.key}:${g.key}`;
                          const perms = orderCapabilities(
                            g.items.map((i) => i.permission),
                            Object.keys(catalog),
                          );
                          return (
                            <CapabilityGroup
                              key={g.key}
                              id={`new-user-${hub.key}-${g.key}`}
                              title={g.title}
                              collapsible={g.key !== "hub"}
                              open={openKeys.includes(groupKey)}
                              onToggle={() => flip(groupKey)}
                              selected={perms.filter((p) => chosen.includes(p)).length}
                              unconfigured={perms.filter((p) => chosen.includes(p) && catalog[p]?.available === false).length}
                              problems={0}
                            >
                              {/* Sub-headings (Workflows, Access control) only group
                                  rows for reading; counts stay with the group. */}
                              {splitSections(g.items).map((s) => (
                                <CapabilitySection key={s.key} id={`new-user-${hub.key}-${g.key}-${s.key}`} title={s.title}>
                                  {orderCapabilities(s.items.map((i) => i.permission), Object.keys(catalog)).map((p) => {
                                    const outside = !allowed.has(p);
                                    // Held, but only organization administrators may give it.
                                    const adminOnly =
                                      outside && (p === MANAGE_ACCESS || catalog[p]?.delegate_grantable === false);
                                    const required = p === USE_HUB && chosen.some((c) => c !== USE_HUB);
                                    return (
                                      <div className="capability-row" key={p}>
                                        <Checkbox
                                          checked={chosen.includes(p)}
                                          disabled={outside || required}
                                          onChange={(e) =>
                                            setSelected({
                                              ...selected,
                                              [hub.key]: toggle(chosen, p, e.target.checked),
                                            })
                                          }
                                        >
                                          <span className="capability-title">
                                            <Text strong={!outside}>{capabilityLabel(p, catalog)}</Text>
                                            {catalog[p]?.sensitive && <Tag color="orange">Sensitive</Tag>}
                                            {catalog[p]?.available === false && (
                                              <Text type="warning" className="capability-status">
                                                {catalog[p].status || "Not configured"}
                                              </Text>
                                            )}
                                          </span>
                                        </Checkbox>
                                        {catalog[p]?.description && (
                                          <Tooltip title={catalog[p].description}>
                                            <Text type="secondary" className="capability-state" aria-label={catalog[p].description}>
                                              ⓘ
                                            </Text>
                                          </Tooltip>
                                        )}
                                        {outside && (
                                          <Tooltip
                                            title={
                                              adminOnly
                                                ? "Only organization administrators can grant this."
                                                : "You can only give capabilities you hold in this agent yourself."
                                            }
                                          >
                                            <Text type="secondary" className="capability-state">
                                              {adminOnly ? "Admins only" : "Outside your access"}
                                            </Text>
                                          </Tooltip>
                                        )}
                                        {required && (
                                          <Text type="warning" className="capability-state">
                                            Required
                                          </Text>
                                        )}
                                      </div>
                                    );
                                  })}
                                </CapabilitySection>
                              ))}
                            </CapabilityGroup>
                          );
                        })}
                      </div>
                    )}
                  </CapabilityGroup>
                );
              })}
            </div>
          </>
        )}

        {step === "review" && (
          <div className="review">
            <Paragraph style={{ margin: 0 }}>
              <Text strong>{name.trim()}</Text> <Text type="secondary" className="identity">{subject}</Text>
            </Paragraph>
            <div className="section">
              <Text strong>New account</Text>
              <ul className="review-list">
                <li>
                  {google
                    ? "Normal user role, signs in with Google using this email"
                    : links
                      ? "Normal user role, sets their own password with a one-time sign-in link"
                      : "Normal user role, signs in with this email and the password you set"}
                </li>
              </ul>
            </div>
            <div className="section">
              <Text strong>Access</Text>
              {grants.length === 0 ? (
                <Paragraph type="secondary">No agent access yet (agents open to everyone still apply).</Paragraph>
              ) : (
                <ul className="review-list">
                  {Object.entries(selected)
                    .filter(([, perms]) => perms.length)
                    .map(([hub, perms]) => (
                      <li key={hub}>
                        {hubName(hub)}:{" "}
                        {orderCapabilities(perms, Object.keys(catalogs[hub] ?? {}))
                          .map((p) => capabilityLabel(p, catalogs[hub]))
                          .join(", ")}
                      </li>
                    ))}
                </ul>
              )}
            </div>
            {!google && (
              <Alert
                type="info"
                showIcon
                title={
                  links
                    ? "The sign-in link is shown once after the account is created"
                    : "The password is shown once after the account is created"
                }
                description={
                  links
                    ? "Copy it then and share it with them directly. It works once and expires after 72 hours."
                    : "Copy it then and share it with them directly."
                }
              />
            )}
          </div>
        )}

        {step === "done" && created && (
          <>
            <Alert
              type="success"
              showIcon
              title={`${created.name} can now sign in as ${created.subject}`}
              description={accessLine(created.grants)}
            />
            <SignInDetails
              email={created.subject}
              password={password}
              signIn={created.sign_in ?? signIn}
              {...linkProps(created, links)}
            />
            <a href={personHref(created.subject)} onClick={finish}>
              Open their details
            </a>
          </>
        )}

        {step === "exists" && (
          <>
            <Alert
              type="info"
              showIcon
              title="This user already exists"
              description="Nothing was changed. Edit their access from their details."
            />
            {afterUncertain && !google && !links && (
              <>
                <Paragraph style={{ margin: 0 }}>
                  It may be the account your earlier attempt created. If so, it signs in with the password you set:
                </Paragraph>
                <OneTimePassword password={password} />
              </>
            )}
            {afterUncertain && !google && links && (
              <Paragraph style={{ margin: 0 }}>
                It may be the account your earlier attempt created. Open their details and use Reset password to
                make a sign-in link for them.
              </Paragraph>
            )}
          </>
        )}

        {step === "partial" && failure && (
          <>
            <Alert type="warning" showIcon title="The account was created, but access wasn’t granted" description={partialDetail(failure)} />
            <SignInDetails
              email={subject}
              password={password}
              signIn={google ? "google" : "password"}
              {...linkProps(partialLink(failure), links)}
            />
          </>
        )}

        {step === "uncertain" && failure && (
          <Alert type="warning" showIcon title="Couldn’t confirm whether the account was created" description={failure.message} />
        )}

        {retryError && step === "partial" && (
          <Alert type="error" showIcon title="Access wasn’t granted" description={retryError.message} />
        )}

        {step === "failed" && failure && <Alert type="error" showIcon title="Nothing was created" description={failure.message} />}
      </div>
    </Drawer>
  );
}

/** The one-time link a partial create still made (the account exists). */
function partialLink(failure: ApiError | null) {
  const data = failure?.data ?? {};
  return {
    ...(typeof data.link === "string" || data.link === null ? { link: data.link as string | null } : {}),
    ...(typeof data.expires_at === "number" ? { expires_at: data.expires_at } : {}),
    ...(typeof data.link_error === "string" ? { link_error: data.link_error } : {}),
  };
}

/** SignInDetails props for a created account in link mode. */
function linkProps(
  result: { link?: string | null; expires_at?: number; link_error?: string },
  links: boolean,
) {
  if (!links) return {};
  return { link: result.link ?? null, expiresAt: result.expires_at, linkError: result.link_error };
}
