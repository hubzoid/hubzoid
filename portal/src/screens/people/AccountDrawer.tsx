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
  Typography,
} from "antd";
import { Copy, KeyRound } from "lucide-react";
import { ApiError, request, type AccountCreated, type Hub, type Me } from "../../api";
import { errorText } from "../../hooks/useData";
import { useCatalogs } from "../../hooks/useCatalogs";
import { hrefWith, personHref, useNavigationGuard } from "../../hooks/useRoute";
import { MANAGE_ACCESS, USE_HUB, capabilityLabel, isGrantable } from "../../lib/format";
import { orderCapabilities, toggle } from "../access/plan";
import { generatePassword, passwordProblem } from "./password";

const { Text, Title, Paragraph } = Typography;

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

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

type Step = "edit" | "review" | "saving" | "done" | "failed";

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
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [touched, setTouched] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [created, setCreated] = useState<AccountCreated | null>(null);

  const grantable = me.grantable ?? {};
  // Agents this viewer manages, in the Console's order. Legacy agents are
  // listed but take no grants here: their access is still in the chat app.
  const managed = hubs.filter((h) => h.key in grantable);
  const catalogs = useCatalogs(managed.map((h) => h.key));

  const subject = email.trim().toLowerCase();
  const emailProblem = !subject ? "Enter an email address." : EMAIL.test(subject) ? null : "Enter a valid email address.";
  const nameProblem = name.trim() ? null : "Enter their name.";
  const grants = useMemo(
    () =>
      Object.entries(selected).flatMap(([hub, perms]) =>
        perms.map((permission) => ({ hub, permission })),
      ),
    [selected],
  );
  const needsGrant = !me.org_admin && grants.length === 0;
  const invalid = !!(emailProblem || nameProblem || passwordProblem(password) || needsGrant);
  // Unsaved input exists only before saving; after a result there is nothing to lose.
  const dirty =
    (step === "edit" || step === "review") && (!!email || !!name || !!password || grants.length > 0);
  const busy = step === "saving";

  const reset = useCallback(() => {
    setStep("edit");
    setEmail("");
    setName("");
    setPassword("");
    setSelected({});
    setTouched(false);
    setFailure(null);
    setCreated(null);
  }, []);
  const guard = useMemo(
    () => (open ? { dirty, busy, discard: reset } : null),
    [open, dirty, busy, reset],
  );
  useNavigationGuard(guard);

  function close() {
    if (busy) return;
    if (!dirty) {
      reset();
      onClose();
      return;
    }
    modal.confirm({
      title: "Discard this account?",
      content: "Nothing has been created yet. What you entered will be lost.",
      okText: "Discard",
      okButtonProps: { danger: true },
      cancelText: "Keep editing",
      onOk: () => {
        reset();
        onClose();
      },
    });
  }

  function finish() {
    reset(); // drops the password from memory
    onClose();
  }

  async function save() {
    setStep("saving");
    setFailure(null);
    try {
      const result = await request<AccountCreated>("/accounts", {
        email: subject,
        name: name.trim(),
        password,
        grants,
      });
      setCreated(result);
      setStep("done");
      onCreated();
    } catch (e) {
      setFailure(e instanceof ApiError ? e : new ApiError(errorText(e), 0));
      setStep("failed");
      onCreated(); // refresh People: a partial failure may have changed something
    }
  }

  const hubName = (key: string) => hubs.find((h) => h.key === key)?.name ?? key;

  const title =
    step === "done"
      ? "Account created"
      : step === "review"
        ? "Review the new account"
        : step === "saving"
          ? "Creating…"
          : step === "failed"
            ? "Account not created"
            : "Add account";

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
        <Button onClick={() => setStep("edit")}>Back</Button>
        <Button type="primary" onClick={() => void save()}>
          Create account
        </Button>
      </Space>
    ) : step === "saving" ? (
      <Space className="drawer-actions">
        <Button type="primary" loading>
          Creating…
        </Button>
      </Space>
    ) : step === "failed" ? (
      <Space className="drawer-actions">
        {failure?.certain && failure.code !== "partial" && (
          <Button onClick={() => setStep("edit")}>Back to the form</Button>
        )}
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
      onClose={step === "done" ? finish : close}
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
            <Paragraph type="secondary" style={{ margin: 0 }}>
              Creates a chat sign-in with the normal user role. No invitation is sent: you share the password yourself.
            </Paragraph>
            <div className="field">
              <label className="field-label" htmlFor="account-email">
                Email address
              </label>
              <Input
                id="account-email"
                autoFocus
                autoComplete="off"
                placeholder="name@example.com"
                value={email}
                status={touched && emailProblem ? "error" : undefined}
                onChange={(e) => setEmail(e.target.value)}
              />
              <Text type={touched && emailProblem ? "danger" : "secondary"} className="field-help">
                {touched && emailProblem ? emailProblem : "They sign in with this email. Access is granted to it."}
              </Text>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="account-name">
                Name
              </label>
              <Input
                id="account-name"
                autoComplete="off"
                value={name}
                status={touched && nameProblem ? "error" : undefined}
                onChange={(e) => setName(e.target.value)}
              />
              <Text type={touched && nameProblem ? "danger" : "secondary"} className="field-help">
                {touched && nameProblem ? nameProblem : "Shown in the chat app and here."}
              </Text>
            </div>
            <PasswordField id="account-password" value={password} onChange={setPassword} touched={touched} />

            <div className="section">
              <Title level={2} style={{ fontSize: 16 }}>Initial access</Title>
              <Paragraph type="secondary" style={{ margin: "0 0 8px" }}>
                {me.org_admin
                  ? "Optional. You can grant access later from any agent’s Access tab."
                  : "Choose access in at least one agent you manage. You can only give capabilities you hold yourself."}
              </Paragraph>
              {touched && needsGrant && (
                <Alert type="error" showIcon title="Choose access in at least one agent you manage." />
              )}
              {managed.length === 0 && <Text type="secondary">You don’t manage any agent.</Text>}
              {managed.map((hub) => {
                const allowed = new Set(grantable[hub.key] ?? []);
                const catalog = catalogs[hub.key] ?? {};
                // Included and obsolete capabilities have nothing to grant.
                const perms = orderCapabilities(
                  Object.keys(catalog).filter((p) => isGrantable(catalog[p])),
                  Object.keys(catalog),
                );
                const chosen = selected[hub.key] ?? [];
                return (
                  <div key={hub.key} className="agent-access" style={{ marginBottom: 12 }}>
                    <div className="agent-access-heading">
                      <Text strong>{hub.name}</Text>
                    </div>
                    {!hub.authoritative ? (
                      <Text type="secondary">
                        Access to this agent is still managed in the chat app, so it can’t be granted here.
                      </Text>
                    ) : (
                      <div className="capabilities" role="group" aria-label={`Access to ${hub.name}`}>
                        {perms.map((p) => {
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
                              {outside && (
                                <Text
                                  type="secondary"
                                  className="capability-state"
                                  title={
                                    adminOnly
                                      ? "Only organization administrators can grant this."
                                      : "You can only give capabilities you hold in this agent yourself."
                                  }
                                >
                                  {adminOnly ? "Admins only" : "Outside your access"}
                                </Text>
                              )}
                              {required && (
                                <Text type="warning" className="capability-state">
                                  Required
                                </Text>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}

        {(step === "review" || step === "saving") && (
          <div className="review">
            <Paragraph style={{ margin: 0 }}>
              <Text strong>{name.trim()}</Text> <Text type="secondary" className="identity">{subject}</Text>
            </Paragraph>
            <div className="section">
              <Text strong>Chat account</Text>
              <ul className="review-list">
                <li>Normal user role, signs in with this email and the password you set</li>
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
            <Alert
              type="info"
              showIcon
              title="The password is shown once after the account is created"
              description="Copy it then and share it with them directly."
            />
          </div>
        )}

        {step === "done" && created && (
          <>
            <Alert
              type="success"
              showIcon
              title={`${created.name} can now sign in as ${created.subject}`}
              description={
                Object.keys(created.grants).length
                  ? `Access granted in ${Object.keys(created.grants).map(hubName).join(", ")}.`
                  : "No agent access was granted yet."
              }
            />
            <OneTimePassword password={password} />
            <a href={personHref(created.subject)} onClick={finish}>
              Open their details
            </a>
          </>
        )}

        {step === "failed" && failure && <CreateFailure failure={failure} subject={subject} grants={selected} hubName={hubName} />}
      </div>
    </Drawer>
  );
}

function CreateFailure({
  failure,
  subject,
  grants,
  hubName,
}: {
  failure: ApiError;
  subject: string;
  grants: Record<string, string[]>;
  hubName: (key: string) => string;
}) {
  if (failure.code === "account_exists") {
    const hubsWithAccess = Object.entries(grants).filter(([, p]) => p.length).map(([h]) => h);
    return (
      <Alert
        type="info"
        showIcon
        title="This person already has an account"
        description={
          <>
            {failure.message} Nothing was created.
            <div style={{ marginTop: 8 }}>
              {hubsWithAccess.length ? (
                <Space wrap>
                  {hubsWithAccess.map((h) => (
                    <Button key={h} href={hrefWith(`/agents/${encodeURIComponent(h)}/access`, { edit: subject })}>
                      Grant access in {hubName(h)}
                    </Button>
                  ))}
                </Space>
              ) : (
                <Button href={personHref(subject)}>Open their details</Button>
              )}
            </div>
          </>
        }
      />
    );
  }
  if (failure.code === "partial")
    return <Alert type="warning" showIcon title="The account needs attention" description={failure.message} />;
  if (!failure.certain)
    return (
      <Alert
        type="warning"
        showIcon
        title="Couldn’t confirm whether the account was created"
        description={`${failure.message} Refresh accounts under People and check before trying again.`}
      />
    );
  return <Alert type="error" showIcon title="Nothing was created" description={failure.message} />;
}
