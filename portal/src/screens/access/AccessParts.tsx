import { useEffect, useState, type ReactNode } from "react";
import { Alert, Button, Input, Segmented, Space, Tag, Typography } from "antd";
import { ChevronDown, CircleHelp, Search } from "lucide-react";
import { request, query, type AccountOption } from "../../api";
import { errorText } from "../../hooks/useData";
import { AccountTag, PersonAvatar, RoleBadge } from "../../components/common";
import { personName } from "../../lib/format";

const { Text } = Typography;

/**
 * Help that works without hover: a real button that reveals its explanation
 * inline, for keyboard, touch and screen-reader users alike. The text is a
 * separate element so a caller can place it below the row it explains.
 */
export function HelpToggle({
  label,
  open,
  controls,
  onToggle,
}: {
  label: string;
  open: boolean;
  controls: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className="capability-info"
      aria-label={`About ${label}`}
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
    >
      <CircleHelp size={16} aria-hidden="true" />
    </button>
  );
}

export function HelpText({ id, open, children }: { id: string; open: boolean; children: ReactNode }) {
  return (
    <div id={id} className="help-text" hidden={!open}>
      {children}
    </div>
  );
}

/**
 * One capability group. Collapsible groups start closed and say what they hold
 * in the header; the rows stay mounted while closed, so nothing selected is
 * lost. Always-open groups (entry, and removable leftovers) have no toggle.
 */
export function CapabilityGroup({
  id,
  title,
  collapsible,
  open,
  onToggle,
  selected,
  unconfigured,
  problems,
  children,
}: {
  id: string;
  title: string;
  collapsible: boolean;
  open: boolean;
  onToggle: () => void;
  selected: number;
  unconfigured: number;
  problems: number;
  children: ReactNode;
}) {
  const titleId = `capability-group-${id}`;
  const panelId = `${titleId}-panel`;
  const shown = !collapsible || open;
  return (
    <div className="capability-group" role="group" aria-labelledby={titleId}>
      <h3 className="capability-group-heading">
        {collapsible ? (
          <button
            type="button"
            className="capability-group-toggle"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={onToggle}
          >
            {/* Spaces between parts keep the accessible name readable:
                "Restricted tools · 2 selected". Flex layout ignores them. */}
            <span className="capability-group-title" id={titleId}>{title}</span>
            {selected > 0 && <>{" "}<Text type="secondary" className="capability-group-meta">· {selected} selected</Text></>}
            {unconfigured > 0 && (
              <>{" "}<Text type="warning" className="capability-group-meta">· {unconfigured} not configured</Text></>
            )}
            {problems > 0 && <>{" "}<Text type="danger" className="capability-group-meta">· needs attention</Text></>}
            <ChevronDown size={16} aria-hidden="true" className="capability-group-chevron" />
          </button>
        ) : (
          <span className="capability-group-title" id={titleId}>{title}</span>
        )}
      </h3>
      <div id={panelId} className="capability-group-panel" hidden={!shown}>
        {children}
      </div>
    </div>
  );
}

/** A `workflow:<name>` identity from before workflows ran as user accounts. */
export function LegacyServiceTag() {
  return (
    <Tag color="default" className="legacy-service-tag" style={{ marginInlineEnd: 0 }}>
      Legacy service identity
    </Tag>
  );
}

/** Add user's first choice: give an existing account access, or create one. */
export type AddKind = "existing" | "new" | "email";

export function AddChoice({
  value,
  onChange,
  canCreate,
  createBlocked,
}: {
  value: AddKind;
  onChange: (kind: AddKind) => void;
  canCreate: boolean;
  /** Why New account is unavailable, when it is. */
  createBlocked?: string;
}) {
  return (
    <div className="field">
      <Segmented<AddKind>
        block
        aria-label="Who to add"
        value={value === "email" ? "existing" : value}
        onChange={onChange}
        options={[
          { label: "Existing account", value: "existing" },
          { label: "New account", value: "new", disabled: !canCreate },
        ]}
      />
      <Text type="secondary" className="field-help">
        {value === "new"
          ? "Create their sign-in and give access in one step."
          : !canCreate && createBlocked
            ? `Someone who already signs in. ${createBlocked}`
            : "Someone who already signs in to the chat app."}
      </Text>
    </div>
  );
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Find an existing chat account. Only real accounts are offered (never an
 * email-only grant or a legacy service identity). An agent manager sees the
 * people in their agents, and anyone else by typing their full email, the same
 * scope as People. With no match for an email the choices are explicit:
 * create a new account, or pre-approve the email without one.
 */
export function AccountPicker({
  orgAdmin,
  canCreate,
  onChoose,
  onCreate,
  onPreApprove,
  onType,
}: {
  orgAdmin: boolean;
  canCreate: boolean;
  onType?: () => void;
  onChoose: (account: AccountOption) => void;
  onCreate: (email: string) => void;
  onPreApprove: (email: string) => void;
}) {
  const [text, setText] = useState("");
  const [result, setResult] = useState<{ q: string; rows: AccountOption[]; error?: string } | null>(null);
  const q = text.trim().toLowerCase();
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      request<{ accounts: AccountOption[] }>("/accounts" + query({ q, limit: 8 }))
        .then((r) => live && setResult({ q, rows: r.accounts }))
        .catch((e) => live && setResult({ q, rows: [], error: errorText(e) }));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q]);
  const current = result && result.q === q ? result : null;
  const exact = current?.rows.some((r) => r.subject === q);
  return (
    <div className="field">
      <label className="field-label" htmlFor="access-account-search">
        Account
      </label>
      <Input
        id="access-account-search"
        autoFocus
        autoComplete="off"
        allowClear
        prefix={<Search size={16} />}
        placeholder="Search by name or email"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          onType?.();
        }}
      />
      <Text type="secondary" className="field-help">
        {orgAdmin
          ? "Choose who gets access."
          : "People in agents you manage are listed. Type anyone else’s full email to find their account."}
      </Text>
      {current?.error && <Alert type="error" showIcon title={`Couldn’t search accounts: ${current.error}`} />}
      {current && current.rows.length > 0 && (
        <ul className="account-options" aria-label="Matching accounts" style={{ listStyle: "none", margin: "8px 0 0", padding: 0 }}>
          {current.rows.map((a) => (
            <li key={a.subject} style={{ marginBottom: 4 }}>
              <Button
                block
                onClick={() => onChoose(a)}
                aria-label={`Choose ${personName(a.subject, a.display)} (${a.subject})`}
                style={{ height: "auto", padding: "6px 10px", justifyContent: "flex-start", textAlign: "left" }}
              >
                <Space align="center" wrap>
                  <PersonAvatar subject={a.subject} display={a.display} size={28} />
                  <span>
                    <Text strong>{personName(a.subject, a.display)}</Text>{" "}
                    {a.display && <Text type="secondary" className="identity">{a.subject}</Text>}
                  </span>
                  {a.status !== "active" && <AccountTag status={a.status} />}
                  {a.organization_admin && <RoleBadge>Org admin</RoleBadge>}
                </Space>
              </Button>
            </li>
          ))}
        </ul>
      )}
      {current && !current.error && !exact && EMAIL.test(q) && (
        <Alert
          type="info"
          showIcon
          title={`No account uses ${q}`}
          description={
            <Space wrap style={{ marginTop: 4 }}>
              {canCreate && (
                <Button type="primary" onClick={() => onCreate(q)}>
                  Create a new account
                </Button>
              )}
              <Button type="link" onClick={() => onPreApprove(q)} style={{ paddingInline: 0 }}>
                Pre-approve this email instead
              </Button>
            </Space>
          }
        />
      )}
      {current && !current.error && current.rows.length === 0 && q && !EMAIL.test(q) && (
        <Text type="secondary">
          No matching account.{orgAdmin ? "" : " Type their full email address."}
        </Text>
      )}
    </div>
  );
}

/** The account Add user will give access to, with a way to choose again. */
export function ChosenAccount({ account, onChange }: { account: AccountOption; onChange: () => void }) {
  const name = personName(account.subject, account.display);
  return (
    <div className="field">
      <span className="field-label">Account</span>
      <div className="person-heading">
        <Space align="center" wrap>
          <PersonAvatar subject={account.subject} display={account.display} size={40} />
          <div>
            <Text strong>{name}</Text>
            <div className="identity-tags">
              {name !== account.subject && (
                <Text type="secondary" className="identity">
                  {account.subject}{" "}
                </Text>
              )}
              <AccountTag status={account.status} />
            </div>
          </div>
          <Button onClick={onChange} aria-label="Choose a different account">
            Change
          </Button>
        </Space>
      </div>
    </div>
  );
}
