import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { HelpCircle, RefreshCw, Search, UserPlus } from "lucide-react";
import { ApiError, request, query, type Hub, type Me, type Overview, type Person } from "../api";
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
  MANAGE_ACCESS,
  ORG,
  isService,
  personName,
  relativeTime,
} from "../lib/format";
import { orderCapabilities } from "./access/plan";
import { AccountDrawer, OneTimePassword, PasswordField } from "./people/AccountDrawer";
import { passwordProblem } from "./people/password";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

/** A field label with a hover "?" carrying the explanation, so the detail lives
 *  in a tooltip instead of crowding the row. */
function HelpLabel({ text, help }: { text: string; help: string }) {
  return (
    <Space size={4}>
      {text}
      {/* Focusable + tap/click triggers so the explanation is reachable by
          keyboard and touch, not hover only. */}
      <Tooltip title={help} trigger={["hover", "focus", "click"]}>
        <span className="help-icon" role="button" tabIndex={0} aria-label={help}>
          <HelpCircle size={13} />
        </span>
      </Tooltip>
    </Space>
  );
}

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
              { value: "regular", label: "Regular" },
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
                    {p.organization_admin && <RoleBadge>Org admin</RoleBadge>}
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
  if (v.state === "legacy")
    return (
      <Text type="secondary" className="sync-note">
        Chat app visibility isn’t mirrored yet: no agent has been moved to managed access.
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const name = person ? personName(person.subject, person.display) : (subject ?? "");
  const access = useMemo(
    () => (person ? Object.entries(person.access).filter(([, perms]) => perms.length) : []),
    [person],
  );

  function close() {
    if (busy) return;
    navigate("/people");
  }

  async function act(path: string, body: unknown, done: string) {
    setBusy(true);
    setError("");
    try {
      // Prefer the backend's own explanation (e.g. reactivation that leaves
      // access paused because the chat account is gone) over a blanket message.
      const res = await request<{ message?: string | null }>(path, body);
      message.success(res?.message || done);
      data.reload();
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const confirmBlock = () =>
    person &&
    modal.confirm({
      title: `Block ${name}?`,
      content:
        "They immediately lose access to every agent, including agents open to everyone. Their chat account and history stay in the chat app. Reactivating later does not restore the access removed now.",
      okText: "Block access",
      okButtonProps: { danger: true },
      onOk: () =>
        act("/people/block", { subject: person.subject, suspended: true }, `${name} is blocked.`),
    });

  const confirmReactivate = () =>
    person &&
    modal.confirm({
      title: `Reactivate ${name}?`,
      content:
        "They can sign in and use agents that are open to everyone again. Access removed when they were blocked is not restored — grant it again per agent.",
      okText: "Reactivate",
      onOk: () =>
        act("/people/block", { subject: person.subject, suspended: false }, `${name} is active again.`),
    });

  const confirmOrgAdmin = (grant: boolean) =>
    person &&
    modal.confirm({
      title: grant
        ? `Make ${name} an organization administrator?`
        : `Remove organization administrator rights from ${name}?`,
      content: grant
        ? "They will be able to manage access for every agent, including other administrators."
        : "Their direct access to individual agents stays. The last organization administrator cannot be removed.",
      okText: grant ? "Make administrator" : "Remove rights",
      okButtonProps: { danger: !grant },
      onOk: () =>
        act(
          "/access/" + (grant ? "grant" : "revoke"),
          { subject: person.subject, hub: ORG, permission: MANAGE_ACCESS },
          grant ? `${name} is now an organization administrator.` : `${name} is no longer an organization administrator.`,
        ),
    });

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
          "Person"
        )
      }
    >
      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : !person ? (
        <Empty
          description={
            <>
              <div>No one with the identity {subject} has access to an agent you manage.</div>
              <Text type="secondary">They appear here once they hold any access.</Text>
            </>
          }
        >
          <Button href={href("/people")}>Back to people</Button>
        </Empty>
      ) : (
        <div className="drawer-body">
          {error && <Alert type="error" showIcon title={error} />}
          <Descriptions
            size="small"
            column={1}
            items={[
              {
                key: "identity",
                label: (
                  <HelpLabel
                    text="Identity"
                    help={
                      isService(person.subject)
                        ? "A legacy service identity; it runs without a chat account."
                        : "Access is granted to this identity. Whoever signs in with it holds the access."
                    }
                  />
                ),
                children: <span className="identity">{person.subject}</span>,
              },
              {
                key: "account",
                label: (
                  <HelpLabel
                    text="Chat account"
                    help={
                      person.blocked
                        ? "Blocked here; every agent denies them regardless of grants."
                        : isService(person.subject)
                          ? "Not applicable to a service identity."
                          : !person.owui_id
                            ? "No chat account yet. Access applies as soon as they sign up with this email."
                            : person.pending
                              ? "Signed up; waiting for an administrator to approve the account."
                              : "Signed up and approved."
                    }
                  />
                ),
                children: <AccountTag status={person.status} />,
              },
              {
                key: "role",
                label: (
                  <HelpLabel
                    text="Role"
                    help={
                      person.organization_admin
                        ? "Can manage access for every agent, including this portal."
                        : "Standard access. Can only use the agents they are granted."
                    }
                  />
                ),
                children: person.organization_admin ? (
                  <RoleBadge>Organization administrator</RoleBadge>
                ) : (
                  <Text type="secondary">Regular access</Text>
                ),
              },
            ]}
          />

          <div className="section">
            <Title level={2} style={{ fontSize: 16 }}>Access by agent</Title>
            {access.length === 0 ? (
              <Text type="secondary">
                {person.blocked ? "All access was removed when they were blocked." : "No direct access to any agent you manage."}
              </Text>
            ) : (
              <div className="access-by-agent">
                {access.map(([h, perms]) => (
                  <div key={h} className="agent-access">
                    <div className="agent-access-heading">
                      <Text strong>{hubs.find((x) => x.key === h)?.name ?? h}</Text>
                      <a
                        href={hrefWith(`/agents/${encodeURIComponent(h)}/access`, {
                          edit: person.subject,
                        })}
                      >
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
            )}
          </div>

          {me.org_admin && (
            <div className="section">
              <Title level={2} style={{ fontSize: 16 }}>Administrative actions</Title>
              <Paragraph type="secondary">
                These apply across every agent and are recorded in Activity. Each asks for confirmation.
              </Paragraph>
              <Space wrap>
                {person.suspended ? (
                  <Button loading={busy} onClick={confirmReactivate}>
                    Reactivate
                  </Button>
                ) : (
                  // Shown even when the chat account is unavailable: blocking is the
                  // explicit offboard — it suspends them and removes every retained
                  // grant, which an unavailable account still needs.
                  <Button danger loading={busy} onClick={confirmBlock}>
                    Block access
                  </Button>
                )}
                {!person.blocked && !isService(person.subject) && (
                  <Button loading={busy} onClick={() => confirmOrgAdmin(!person.organization_admin)}>
                    {person.organization_admin ? "Remove administrator rights" : "Make organization administrator"}
                  </Button>
                )}
              </Space>
              {person.account_unavailable && !person.suspended && (
                <Alert
                  className="notice"
                  type="warning"
                  showIcon
                  banner
                  message="This person’s chat account is unavailable — removed or not found. Access resumes automatically if the account reappears; to offboard fully, remove it in the chat app."
                />
              )}
              {!me.account_admin && (
                <Paragraph type="secondary" style={{ marginTop: 12 }}>
                  To suspend or delete the chat account itself, use the <a href="/admin">chat app’s account settings</a>.
                </Paragraph>
              )}
            </div>
          )}
          {me.account_admin && person.owui_id && !isService(person.subject) && person.subject !== me.subject && (
            <ChatAccount person={person} name={name} onChanged={() => { data.reload(); onChanged(); }} />
          )}
        </div>
      )}
    </Drawer>
  );
}

type AccountInfo = { subject: string; name: string | null; role: string | null };

/**
 * The person's chat sign-in, for organization administrators: approve a
 * pending signup, reset the password (shown once), switch the chat-app admin
 * role, or delete the account. The server re-checks every action.
 */
function ChatAccount({
  person,
  name,
  onChanged,
}: {
  person: Person;
  name: string;
  onChanged: () => void;
}) {
  const { modal, message } = App.useApp();
  const info = useData<AccountInfo>(`/accounts/${encodeURIComponent(person.subject)}`);
  const [panel, setPanel] = useState<"" | "password" | "delete">("");
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState("");
  const [touched, setTouched] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/accounts/${encodeURIComponent(person.subject)}`;
  const role = info.data?.role;

  async function call(sub: string, body: unknown, done: string, method?: "POST" | "DELETE") {
    setBusy(true);
    setError("");
    try {
      await request(path + sub, body, undefined, method);
      message.success(done);
      info.reload();
      onChanged();
      return true;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword() {
    setTouched(true);
    if (passwordProblem(password)) return;
    if (await call("/password", { password }, `New password set for ${name}. Their other sessions were signed out.`)) {
      setShown(password);
      setPassword("");
      setTouched(false);
    }
  }

  const confirmApprove = () =>
    modal.confirm({
      title: `Approve ${name}’s account?`,
      content: "They can sign in and use agents they have access to, including agents open to everyone.",
      okText: "Approve",
      onOk: () => call("/approve", {}, `${name} is approved.`),
    });

  const confirmRole = (next: "user" | "admin") =>
    modal.confirm({
      title: next === "admin" ? `Make ${name} a chat-app administrator?` : `Make ${name} a regular chat-app user?`,
      content:
        next === "admin"
          ? "They get the chat app’s administrator settings (models, connections, settings). This does not give them Console access or agent access."
          : "They lose the chat app’s administrator settings. Console and agent access are unchanged.",
      okText: next === "admin" ? "Make administrator" : "Make regular user",
      okButtonProps: { danger: next === "admin" },
      onOk: () => call("/role", { role: next }, "Chat-app role updated."),
    });

  return (
    <div className="section">
      <Title level={2} style={{ fontSize: 16 }}>Chat account</Title>
      <Paragraph type="secondary">
        Their sign-in for the chat app. Changes apply immediately and are recorded in Activity.
      </Paragraph>
      {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 12 }} />}
      {!info.data ? (
        info.error ? (
          <Alert type="warning" showIcon title="Couldn’t read the chat account" description={info.error} />
        ) : (
          <LoadState retry={info.reload} rows={1} />
        )
      ) : (
        <>
          <Descriptions
            size="small"
            column={1}
            items={[
              {
                key: "role",
                label: "Chat-app role",
                children:
                  role === "pending" ? (
                    <Tag color="gold">Awaiting approval</Tag>
                  ) : role === "admin" ? (
                    <RoleBadge>Administrator</RoleBadge>
                  ) : (
                    <Text>User</Text>
                  ),
              },
            ]}
          />
          <Space wrap style={{ marginTop: 8 }}>
            {role === "pending" && (
              <Button type="primary" loading={busy} onClick={confirmApprove}>
                Approve account
              </Button>
            )}
            <Button disabled={busy} onClick={() => { setPanel(panel === "password" ? "" : "password"); setShown(""); }}>
              Reset password
            </Button>
            {role && role !== "pending" && (
              <Button disabled={busy} onClick={() => confirmRole(role === "admin" ? "user" : "admin")}>
                {role === "admin" ? "Make regular chat-app user" : "Make chat-app administrator"}
              </Button>
            )}
            <Button danger disabled={busy} onClick={() => setPanel(panel === "delete" ? "" : "delete")}>
              Delete account
            </Button>
          </Space>
        </>
      )}
      {panel === "password" && (
        <div className="agent-access" style={{ marginTop: 12 }}>
          {shown ? (
            <OneTimePassword password={shown} />
          ) : (
            <>
              <PasswordField id="reset-password" value={password} onChange={setPassword} touched={touched} />
              <Space>
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
        <div className="agent-access" style={{ marginTop: 12 }}>
          <Paragraph>
            Deleting removes all of {name}’s access and their chat account, including their chat history. It can’t be undone.
          </Paragraph>
          <div className="field">
            <label className="field-label" htmlFor="delete-confirm">
              Type {person.subject} to confirm
            </label>
            <Input id="delete-confirm" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
          </div>
          <Space style={{ marginTop: 8 }}>
            <Button
              danger
              type="primary"
              loading={busy}
              disabled={typed.trim().toLowerCase() !== person.subject}
              onClick={() =>
                void call("", { confirm_email: typed.trim() }, `${name}’s account was deleted.`, "DELETE").then((ok) => {
                  if (ok) navigate("/people");
                })
              }
            >
              Delete account
            </Button>
            <Button disabled={busy} onClick={() => { setPanel(""); setTyped(""); }}>
              Cancel
            </Button>
          </Space>
        </div>
      )}
    </div>
  );
}
