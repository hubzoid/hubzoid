import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { MoreHorizontal, RefreshCw, Search, UserPlus } from "lucide-react";
import {
  ApiError,
  request,
  query,
  type BlockResult,
  type Hub,
  type Me,
  type Overview,
  type Person,
  type SignInLink,
  usesSignInLinks,
} from "../api";
import { errorText, useData } from "../hooks/useData";
import { useCatalogs } from "../hooks/useCatalogs";
import { href, hrefWith, navigate, personHref, useHashQuery } from "../hooks/useRoute";
import {
  AccountTag,
  CapabilityTag,
  LoadState,
  PersonAvatar,
  PersonCell,
  RoleBadge,
} from "../components/common";
import {
  isService,
  personName,
  relativeTime,
} from "../lib/format";
import { orderCapabilities } from "./access/plan";
import { AccountDrawer, OneTimeLink, OneTimePassword, PasswordField } from "./people/AccountDrawer";
import { passwordProblem } from "./people/password";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

type PeoplePage = { people: Person[]; total: number };

export function PeopleScreen({
  me,
  hubs,
  selected,
}: {
  me: Me;
  hubs: Hub[];
  selected?: string;
}) {
  const { message } = App.useApp();
  // Filters live in the URL so refresh, Back and shared links restore the view.
  const [q, setQuery] = useHashQuery();
  const search = q.q || "";
  const status = q.status || "";
  const role = q.role || "";
  const agent = q.agent || "";
  const page = Math.max(1, Number(q.page) || 1);
  const filtersActive = !!(search || status || role || agent);
  const searchTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  // Keyed by a reset token (not the changing value) so a debounced commit doesn't
  // remount the input and steal focus; Reset bumps it to clear the field.
  const [resetToken, setResetToken] = useState(0);
  // Cancel a pending search debounce on unmount so it cannot rewrite the next
  // screen's URL query after navigation.
  useEffect(() => () => clearTimeout(searchTimer.current), []);
  const data = useData<PeoplePage>(
    "/people" +
      query({ q: search, status, role, agent, offset: (page - 1) * PAGE, limit: PAGE }),
  );
  const overview = useData<Overview>("/overview");
  const [refreshing, setRefreshing] = useState(false);
  const [adding, setAdding] = useState(false);
  const agentName = (key: string) => hubs.find((h) => h.key === key)?.name ?? key;
  const resetFilters = () => {
    setResetToken((t) => t + 1); // remount the search input so its defaultValue clears
    setQuery({ q: undefined, status: undefined, role: undefined, agent: undefined, page: undefined });
  };

  async function refreshAccounts() {
    setRefreshing(true);
    try {
      const r = await request<{ count: number }>("/people/refresh", {});
      message.success(`Checked ${r.count} chat ${r.count === 1 ? "account" : "accounts"}.`);
      data.reload();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <>
      <div className="panel">
        <div className="panel-heading">
          <div>
            <Title level={1} style={{ fontSize: 28 }}>People</Title>
            <Paragraph type="secondary">
              {me.can_create_accounts
                ? "Everyone with access to an agent you manage. Add user creates their sign-in and access in one step."
                : "Everyone with access to an agent you manage. Accounts and sign-in live in the chat app; access is decided here."}
            </Paragraph>
          </div>
          {(me.org_admin || me.can_create_accounts) && (
            <Space wrap>
              {me.org_admin && (
                <Tooltip title="Re-reads the chat app’s account directory to update account states. Does not create accounts.">
                  <Button icon={<RefreshCw size={16} />} loading={refreshing} onClick={() => void refreshAccounts()}>
                    Refresh accounts
                  </Button>
                </Tooltip>
              )}
              {me.can_create_accounts && (
                <Button type="primary" icon={<UserPlus size={16} />} onClick={() => setAdding(true)}>
                  Add user
                </Button>
              )}
            </Space>
          )}
        </div>

        {overview.data && <SyncStatus overview={overview.data} me={me} onChanged={overview.reload} />}

        <div className="toolbar">
          <Input
            key={`search:${resetToken}`}
            aria-label="Search people"
            prefix={<Search size={16} />}
            placeholder="Search by name or email"
            defaultValue={search}
            onChange={(e) => {
              const v = e.target.value;
              clearTimeout(searchTimer.current);
              searchTimer.current = setTimeout(
                () => setQuery({ q: v.trim() || undefined, page: undefined }),
                250,
              );
            }}
            allowClear
            style={{ maxWidth: 260 }}
          />
          <Select
            aria-label="Account status"
            value={status || undefined}
            onChange={(v) => setQuery({ status: v, page: undefined })}
            placeholder="Any status"
            allowClear
            style={{ minWidth: 170 }}
            options={[
              { value: "active", label: "Active" },
              { value: "awaiting-signup", label: "Not signed up yet" },
              { value: "pending-approval", label: "Awaiting approval" },
              { value: "blocked", label: "Blocked" },
              { value: "service", label: "Legacy service identity" },
            ]}
          />
          <Select
            aria-label="Role"
            value={role || undefined}
            onChange={(v) => setQuery({ role: v, page: undefined })}
            placeholder="Any role"
            allowClear
            style={{ minWidth: 150 }}
            options={[
              { value: "admin", label: "Administrator" },
              { value: "regular", label: "User" },
              { value: "service", label: "Legacy service identity" },
            ]}
          />
          <Select
            aria-label="Agent"
            value={agent || undefined}
            onChange={(v) => setQuery({ agent: v, page: undefined })}
            placeholder="Any agent"
            allowClear
            style={{ minWidth: 170 }}
            options={hubs.map((h) => ({ value: h.key, label: h.name }))}
          />
          {filtersActive && (
            <Button type="link" onClick={resetFilters}>
              Reset filters
            </Button>
          )}
          <span style={{ marginLeft: "auto" }} />
          {data.data && (
            <Text type="secondary">
              {data.data.total} {data.data.total === 1 ? "person" : "people"}
              {data.at ? ` · updated ${relativeTime(data.at)}` : ""}
            </Text>
          )}
        </div>

        {!data.data ? (
          <LoadState error={data.error} retry={data.reload} />
        ) : (
          <Table<Person>
            rowKey="subject"
            size="middle"
            dataSource={data.data.people}
            scroll={{ x: 720 }}
            pagination={{
              current: page,
              pageSize: PAGE,
              total: data.data.total,
              onChange: (p) => setQuery({ page: p > 1 ? String(p) : undefined }),
              showSizeChanger: false,
              hideOnSinglePage: true,
            }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    filtersActive
                      ? "No one matches these filters."
                      : "No one has access yet. Grant access from an agent’s Access tab; people appear here once they hold any access."
                  }
                >
                  {filtersActive && <Button onClick={resetFilters}>Reset filters</Button>}
                </Empty>
              ),
            }}
            columns={[
              {
                title: "Person",
                key: "person",
                render: (_, p) => (
                  <PersonCell subject={p.subject} display={p.display} link={personHref(p.subject)} />
                ),
              },
              {
                title: "Account",
                key: "account",
                width: 170,
                render: (_, p) => (
                  <Space size={4} wrap>
                    <AccountTag status={p.status} />
                    {p.organization_admin && <RoleBadge>Administrator</RoleBadge>}
                  </Space>
                ),
              },
              {
                title: "Agents",
                key: "agents",
                render: (_, p) => {
                  const entries = Object.entries(p.access).filter(([, perms]) => perms.length);
                  if (!entries.length) return <Text type="secondary">No agent access</Text>;
                  return (
                    <Space wrap size={[6, 6]}>
                      {entries.map(([h, perms]) => (
                        <Tooltip key={h} title={`${perms.length} ${perms.length === 1 ? "capability" : "capabilities"} — open for details`}>
                          <Tag>
                            {agentName(h)} · {perms.length} {perms.length === 1 ? "capability" : "capabilities"}
                          </Tag>
                        </Tooltip>
                      ))}
                    </Space>
                  );
                },
              },
              {
                title: "",
                key: "actions",
                width: 110,
                align: "right",
                render: (_, p) => (
                  <Button href={personHref(p.subject)} aria-label={`Details for ${personName(p.subject, p.display)}`}>
                    Details
                  </Button>
                ),
              },
            ]}
          />
        )}
      </div>
      <PersonDrawer
        subject={selected}
        me={me}
        hubs={hubs}
        onChanged={data.reload}
      />
      {me.can_create_accounts && (
        <AccountDrawer
          open={adding}
          me={me}
          hubs={hubs}
          onClose={() => setAdding(false)}
          onCreated={data.reload}
        />
      )}
    </>
  );
}

/**
 * Chat-app visibility mirror. Enforcement never depends on it, but a failing
 * sync means people may not see (or still see) an agent in the chat list.
 */
function SyncStatus({
  overview,
  me,
  onChanged,
}: {
  overview: Overview;
  me: Me;
  onChanged: () => void;
}) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const v = overview.visibility;
  async function retry() {
    setBusy(true);
    try {
      const r = await request<{ state: string; error?: string }>("/sync", {});
      if (r.state === "error") message.warning(r.error || "Still not connected. Try again shortly.");
      else message.success("Chat app visibility is in sync.");
      onChanged();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (v.state === "direct") return null;
  if (v.state === "error")
    return (
      <Alert
        type="warning"
        showIcon
        title="Recent access changes haven’t reached the chat app yet"
        description={
          <>
            Access is enforced regardless, but the agent list people see in the chat app may be out of date. Retry below after checking the chat connection.
            {v.updated ? ` Last attempt ${relativeTime(v.updated)}.` : ""}
          </>
        }
        action={
          me.org_admin ? (
            <Button size="small" loading={busy} onClick={() => void retry()}>
              Try again now
            </Button>
          ) : undefined
        }
      />
    );
  if (v.state === "ok")
    return (
      <Text type="secondary" className="sync-note">
        Chat app visibility in sync{v.updated ? ` (${relativeTime(v.updated)})` : ""}.
      </Text>
    );
  return (
    <Text type="secondary" className="sync-note">
      Chat app visibility sync hasn’t run in this session{" "}
      {me.org_admin && (
        <Button type="link" size="small" loading={busy} onClick={() => void retry()}>
          Run it now
        </Button>
      )}
    </Text>
  );
}

type AccountInfo = {
  subject: string;
  name: string | null;
  role: string | null;
  sign_in?: "password" | "google" | null;
  /** "admin" | "user" when both halves agree; "console_only" | "chat_only" when not. */
  administrator?: string;
};

const MISMATCH: Record<string, string> = {
  console_only: "Administrator in the Console, but a user in the chat app. Choose a role to fix it.",
  chat_only: "Administrator in the chat app, but not in the Console. Choose a role to fix it.",
};

/**
 * A user's details: who they are, their role, their access by agent, and (for
 * administrators) a password reset, and Reactivate and Delete in the "…" menu. Every action is
 * re-checked by the server and recorded in Activity.
 */
function PersonDrawer({
  subject,
  me,
  hubs,
  onChanged,
}: {
  subject?: string;
  me: Me;
  hubs: Hub[];
  onChanged: () => void;
}) {
  const { modal, message } = App.useApp();
  const open = !!subject;
  const data = useData<PeoplePage>(
    subject ? "/people" + query({ q: subject, limit: 200 }) : null,
  );
  const person = data.data?.people.find((p) => p.subject === subject);
  const catalogs = useCatalogs(hubs.map((h) => h.key));
  const manageAccount =
    !!me.account_admin && !!person?.owui_id && !isService(person.subject) && person.subject !== me.subject;
  // There is no Block here, but a block (an Open WebUI account replaced, one
  // carried over by migration, or an older one) can be lifted.
  const canReactivate = me.org_admin && !!person?.suspended;
  const info = useData<AccountInfo>(
    manageAccount && person ? `/accounts/${encodeURIComponent(person.subject)}` : null,
  );
  const [panel, setPanel] = useState<"" | "password" | "delete">("");
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState("");
  // Hubzoid accounts: a reset gives a one-time link instead of a password.
  const links = usesSignInLinks(me);
  const [shownLink, setShownLink] = useState<SignInLink | null>(null);
  const [touched, setTouched] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [roleRetry, setRoleRetry] = useState<"" | "user" | "admin">("");
  const name = person ? personName(person.subject, person.display) : (subject ?? "");
  const access = useMemo(
    () => (person ? Object.entries(person.access).filter(([, perms]) => perms.length) : []),
    [person],
  );
  const addable = hubs.filter(
    (h) => h.key in (me.grantable ?? {}) && !access.some(([k]) => k === h.key),
  );
  const path = person ? `/accounts/${encodeURIComponent(person.subject)}` : "";
  const role = info.data?.administrator;

  function close() {
    if (busy) return;
    setPanel("");
    setPassword("");
    setShown("");
    setShownLink(null);
    setTyped("");
    navigate("/people");
  }

  async function call(sub: string, body: unknown, done: string, method?: "POST" | "DELETE") {
    setBusy(true);
    setError("");
    try {
      await request(path + sub, body, undefined, method);
      message.success(done);
      info.reload();
      data.reload();
      onChanged();
      return true;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function setRole(next: "user" | "admin") {
    setBusy(true);
    setError("");
    setRoleRetry("");
    try {
      await request(path + "/role", { role: next });
      message.success(next === "admin" ? `${name} is an Administrator.` : `${name} is a User.`);
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      setError(err ? err.message : errorText(e));
      // One side is set and the other isn't: the same request finishes it.
      if (err?.code === "role_partial") setRoleRetry(next);
    } finally {
      setBusy(false);
      info.reload();
      data.reload();
      onChanged();
    }
  }

  const confirmRole = (next: "user" | "admin") =>
    modal.confirm({
      title: next === "admin" ? `Make ${name} an Administrator?` : `Make ${name} a User?`,
      content:
        next === "admin"
          ? "They can manage users and access for every agent, and the chat app’s settings."
          : "They keep their access to agents but can no longer manage users, access or the chat app’s settings.",
      okText: next === "admin" ? "Make Administrator" : "Make User",
      okButtonProps: { danger: next === "admin" },
      onOk: () => setRole(next),
    });

  async function reactivate() {
    if (!person) return;
    setBusy(true);
    setError("");
    try {
      const result = await request<BlockResult>("/people/block", { subject: person.subject, suspended: false });
      // The server says when they still can't use agents (their account is unavailable).
      if (result.message) message.warning(result.message);
      else message.success(`${name} is active again.`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
    } finally {
      setBusy(false);
      info.reload();
      data.reload();
      onChanged();
    }
  }

  const confirmReactivate = () =>
    modal.confirm({
      title: `Reactivate ${name}?`,
      content:
        "They can sign in again and use agents open to everyone. Access removed when they were blocked isn’t restored. Give it again per agent.",
      okText: "Reactivate",
      onOk: () => reactivate(),
    });

  async function resetPassword() {
    setTouched(true);
    if (passwordProblem(password)) return;
    if (await call("/password", { password }, `New password set for ${name}. Their other sessions were signed out.`)) {
      setShown(password);
      setPassword("");
      setTouched(false);
    }
  }

  /** Hubzoid accounts: the current password stops working, their sessions
   *  end, and a one-time link lets them set a new one. */
  async function resetWithLink() {
    setBusy(true);
    setError("");
    try {
      const result = await request<SignInLink>(path + "/password", {});
      setShownLink(result);
      message.success(`Password reset for ${name}. They were signed out everywhere.`);
      info.reload();
      data.reload();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const roleControl = () => {
    if (!manageAccount)
      return person?.organization_admin ? <RoleBadge>Administrator</RoleBadge> : <Text>User</Text>;
    if (!info.data)
      return info.error ? <Text type="warning">Couldn’t read their role</Text> : <Text type="secondary">Loading…</Text>;
    if (role === "pending")
      return (
        <Space wrap>
          <Tag color="gold">Awaiting approval</Tag>
          <Button size="small" loading={busy} onClick={() => void call("/approve", {}, `${name} is approved.`)}>
            Approve
          </Button>
        </Space>
      );
    const value = role === "admin" ? "admin" : role === "user" ? "user" : undefined;
    return (
      <Space wrap size={[8, 4]}>
        <Select
          aria-label="Role"
          style={{ minWidth: 160 }}
          value={value}
          placeholder="Choose a role"
          disabled={busy}
          options={[
            { value: "user", label: "User" },
            { value: "admin", label: "Administrator" },
          ]}
          onChange={(v) => confirmRole(v as "user" | "admin")}
        />
        {role && MISMATCH[role] && (
          <Tooltip title={MISMATCH[role]}>
            <Tag color="orange">Needs attention</Tag>
          </Tooltip>
        )}
      </Space>
    );
  };

  return (
    <Drawer
      open={open}
      onClose={close}
      size={520}
      closable={!busy}
      mask={{ closable: !busy }}
      keyboard={!busy}
      destroyOnHidden
      title={
        person ? (
          <Space align="center">
            <PersonAvatar subject={person.subject} display={person.display} size={36} />
            <span>{name}</span>
          </Space>
        ) : (
          "User"
        )
      }
      extra={
        (manageAccount || canReactivate) && (
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                ...(canReactivate ? [{ key: "reactivate", label: "Reactivate", disabled: busy }] : []),
                ...(manageAccount ? [{ key: "delete", danger: true, label: "Delete user" }] : []),
              ],
              onClick: ({ key }) => (key === "reactivate" ? confirmReactivate() : setPanel("delete")),
            }}
          >
            <Button type="text" aria-label="More actions" icon={<MoreHorizontal size={18} />} />
          </Dropdown>
        )
      }
    >
      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : !person ? (
        <Empty description="No user with this email has access to an agent you manage.">
          <Button href={href("/people")}>Back to people</Button>
        </Empty>
      ) : (
        <div className="drawer-body">
          {error && (
            <Alert
              type="error"
              showIcon
              title={error}
              action={
                roleRetry && (
                  <Button size="small" loading={busy} onClick={() => void setRole(roleRetry)}>
                    Try again
                  </Button>
                )
              }
            />
          )}
          {person.account_unavailable && !person.suspended && (
            <Alert type="warning" showIcon title="Their account is unavailable (removed or not found)." />
          )}
          <Descriptions
            size="small"
            column={1}
            items={[
              { key: "email", label: "Email", children: <span className="identity">{person.subject}</span> },
              { key: "status", label: "Status", children: <AccountTag status={person.status} /> },
              { key: "role", label: "Role", children: roleControl() },
              ...(me.org_admin && !isService(person.subject)
                ? [
                    {
                      key: "phone",
                      label: "Phone",
                      children: (
                        <PhoneEditor
                          subject={person.subject}
                          phone={person.phone ?? null}
                          onSaved={() => {
                            data.reload();
                            onChanged();
                          }}
                        />
                      ),
                    },
                  ]
                : []),
              ...(manageAccount
                ? [
                    {
                      key: "password",
                      label: "Password",
                      children:
                        info.data?.sign_in === "google" ? (
                          <Text type="secondary">Signs in with Google. Their password is managed through Google.</Text>
                        ) : (
                          <Button
                            size="small"
                            disabled={busy}
                            onClick={() => {
                              setPanel(panel === "password" ? "" : "password");
                              setShown("");
                              setShownLink(null);
                            }}
                          >
                            Reset password
                          </Button>
                        ),
                    },
                  ]
                : []),
            ]}
          />

          {panel === "password" && links && (
            <div className="agent-access">
              {shownLink ? (
                shownLink.link ? (
                  <OneTimeLink link={shownLink.link} expiresAt={shownLink.expires_at} />
                ) : (
                  <Alert type="warning" showIcon title="The sign-in link couldn’t be made. Try again." />
                )
              ) : (
                <>
                  <Paragraph style={{ margin: 0 }}>
                    Their current password stops working and they are signed out everywhere. You get a one-time
                    sign-in link to share with them, so they can set a new password.
                  </Paragraph>
                  <Space wrap>
                    <Button type="primary" loading={busy} onClick={() => void resetWithLink()}>
                      Reset and create sign-in link
                    </Button>
                    <Button disabled={busy} onClick={() => setPanel("")}>
                      Cancel
                    </Button>
                  </Space>
                </>
              )}
            </div>
          )}

          {panel === "password" && !links && (
            <div className="agent-access">
              {shown ? (
                <OneTimePassword password={shown} />
              ) : (
                <>
                  <PasswordField id="reset-password" value={password} onChange={setPassword} touched={touched} />
                  <Space wrap>
                    <Button type="primary" loading={busy} onClick={() => void resetPassword()}>
                      Set password
                    </Button>
                    <Button disabled={busy} onClick={() => { setPanel(""); setPassword(""); }}>
                      Cancel
                    </Button>
                  </Space>
                </>
              )}
            </div>
          )}

          {panel === "delete" && (
            <div className="agent-access">
              <Text strong>Delete {name}?</Text>
              <Paragraph style={{ margin: 0 }}>
                Their sign-in account and their chats are deleted, and all their access is removed. Activity history,
                usage records and artifacts they published are kept; their public links stop working. This can’t be
                undone.
              </Paragraph>
              <div className="field">
                <label className="field-label" htmlFor="delete-confirm">
                  Type {person.subject} to confirm
                </label>
                <Input
                  id="delete-confirm"
                  autoComplete="off"
                  inputMode="email"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                />
              </div>
              <Space wrap>
                <Button
                  danger
                  type="primary"
                  loading={busy}
                  disabled={typed.trim().toLowerCase() !== person.subject}
                  onClick={() =>
                    void call("", { confirm_email: typed.trim() }, `${name} was deleted.`, "DELETE").then((ok) => {
                      if (ok) navigate("/people");
                    })
                  }
                >
                  Delete user
                </Button>
                <Button disabled={busy} onClick={() => { setPanel(""); setTyped(""); }}>
                  Cancel
                </Button>
              </Space>
            </div>
          )}

          <div className="section">
            <Title level={2} style={{ fontSize: 16 }}>Access by agent</Title>
            {access.length === 0 && <Text type="secondary">No access to any agent you manage.</Text>}
            <div className="access-by-agent">
              {access.map(([h, perms]) => (
                <div key={h} className="agent-access">
                  <div className="agent-access-heading">
                    <Text strong>{hubs.find((x) => x.key === h)?.name ?? h}</Text>
                    <a href={hrefWith(`/agents/${encodeURIComponent(h)}/access`, { edit: person.subject })}>
                      Edit access
                    </a>
                  </div>
                  <Space wrap size={[6, 6]}>
                    {orderCapabilities(perms, Object.keys(catalogs[h] ?? {})).map((p) => (
                      <CapabilityTag key={p} permission={p} catalog={catalogs[h]} />
                    ))}
                  </Space>
                </div>
              ))}
            </div>
            {!person.blocked && addable.length > 0 && (
              <Select
                aria-label="Add an agent"
                placeholder="Add an agent"
                style={{ width: "100%", marginTop: 8 }}
                value={null}
                options={addable.map((h) => ({ value: h.key, label: h.name }))}
                onChange={(k) => {
                  location.hash = hrefWith(`/agents/${encodeURIComponent(String(k))}/access`, {
                    edit: person.subject,
                  }).replace(/^#/, "");
                }}
              />
            )}
            {person.suspended && (
              <Paragraph type="secondary" style={{ margin: "8px 0 0" }}>
                {canReactivate
                  ? "Blocked. Reactivate them to give access."
                  : "Blocked. Ask an Administrator to reactivate them."}
              </Paragraph>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}


/** The number a person's WhatsApp and Telegram messages come from: a sender
 *  with this number is them. Organization administrators only. */
function PhoneEditor({ subject, phone, onSaved }: {
  subject: string;
  phone: string | null;
  onSaved: () => void;
}) {
  const { message } = App.useApp();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{ phone: string | null }>(
        `/accounts/${encodeURIComponent(subject)}/phone`, { phone: value });
      message.success(result.phone ? "Phone number saved." : "Phone number removed.");
      setEditing(false);
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (!editing)
    return (
      <Space wrap>
        {phone ? <span className="identity">+{phone}</span> : <Text type="secondary">None</Text>}
        <Button
          size="small"
          onClick={() => {
            setValue(phone ? "+" + phone : "");
            setError("");
            setEditing(true);
          }}
        >
          {phone ? "Change" : "Add"}
        </Button>
      </Space>
    );
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Space wrap>
        <Input
          aria-label="Phone number"
          aria-describedby="phone-help"
          placeholder="+91 98000 00001"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onPressEnter={() => void save()}
          style={{ maxWidth: 220 }}
        />
        <Button type="primary" size="small" loading={busy} onClick={() => void save()}>
          Save
        </Button>
        <Button size="small" disabled={busy} onClick={() => setEditing(false)}>
          Cancel
        </Button>
      </Space>
      <Text type="secondary" id="phone-help">
        With the country code. WhatsApp and Telegram messages from this number are theirs. Leave it
        empty to remove it.
      </Text>
      {error && <Text type="danger" role="alert">{error}</Text>}
    </Space>
  );
}
