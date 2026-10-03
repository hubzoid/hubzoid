import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { FlaskConical, MinusCircle, MoreHorizontal, Pencil, Plug, Trash2 } from "lucide-react";
import {
  ApiError,
  connectorsRequest,
  type Connector,
  type ConnectorInput,
  type ConnectorTest,
  type Hub,
  type Me,
  type OpenWebUIConnector,
} from "../api";
import { LoadState } from "../components/common";
import { useData } from "../hooks/useData";
import { useNavigationGuard } from "../hooks/useRoute";

const { Text, Title, Paragraph } = Typography;

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The registry, reloadable. Errors keep the server's own sentence. */
function useConnectors() {
  const [state, setState] = useState<{ data?: Connector[]; error?: string; status?: number }>({});
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    connectorsRequest<{ connectors: Connector[] }>("", "GET", undefined, controller.signal)
      .then((r) => {
        if (!controller.signal.aborted) setState({ data: r.connectors });
      })
      .catch((e) => {
        if (!controller.signal.aborted)
          setState((s) => ({ data: s.data, error: errorText(e), status: e instanceof ApiError ? e.status : 0 }));
      });
    return () => controller.abort();
  }, [revision]);
  const reload = useCallback(() => setRevision((r) => r + 1), []);
  return { ...state, reload };
}

/** What people see as the sign-in method. */
function SignInTag({ c }: { c: Connector }) {
  if (c.auth_type === "none")
    return (
      <Tooltip title="No sign-in: people turn it on for themselves and the server sees no account.">
        <Tag>No sign-in</Tag>
      </Tooltip>
    );
  const how = c.client_id
    ? "Uses the client registered with the provider in advance."
    : c.dynamic_client
      ? "Hubzoid registered itself with the provider."
      : "Hubzoid registers itself with the provider the first time someone connects.";
  return (
    <Tooltip title={how}>
      <Tag color="blue">OAuth{c.client_id ? " · own client" : ""}</Tag>
    </Tooltip>
  );
}

/**
 * An agent's Connectors tab. In the Hubzoid web app, connectors are registered
 * once for the deployment and offered in the agents that list them; this tab
 * manages the ones this agent offers (organization administrators). In Open
 * WebUI mode the servers are registered in Open WebUI and listed read-only.
 */
export function AgentConnectors({ hub, me }: { hub: Hub; me: Me }) {
  if (me.web_app === false) return <OpenWebUIConnectors hub={hub} />;
  if (!me.org_admin)
    return (
      <div className="panel">
        <Title level={2}>Connectors</Title>
        <Paragraph type="secondary">
          Organization administrators add the remote MCP servers people connect to in this agent. Who
          may use each one is in this agent’s access (“Connect &lt;name&gt;”).
        </Paragraph>
      </div>
    );
  return <ConnectorsScreen hub={hub} />;
}

function OpenWebUIConnectors({ hub }: { hub: Hub }) {
  const data = useData<{ servers: OpenWebUIConnector[]; native: boolean }>(
    "/openwebui-connectors?hub=" + encodeURIComponent(hub.key),
  );
  return (
    <div className="panel">
      <Title level={2}>Connectors</Title>
      <Paragraph type="secondary">
        This deployment uses Open WebUI, so its MCP servers are added and changed in Open WebUI
        (Admin Panel → Settings → Integrations). People connect their own account there. Who may use
        each one in {hub.name} is this agent’s access: “Connect &lt;name&gt;”.
      </Paragraph>
      {!data.data ? (
        <LoadState error={data.error} retry={data.reload} />
      ) : (
        <>
          {!data.data.native && (
            <Alert
              type="info"
              showIcon
              title="Personal connections are off"
              description="Set OWUI_NATIVE_MCP=true for the agent to use the servers people connect in Open WebUI."
              style={{ marginBottom: 12 }}
            />
          )}
          <Table<OpenWebUIConnector>
            rowKey="id"
            size="middle"
            pagination={false}
            dataSource={data.data.servers}
            locale={{ emptyText: <Empty description="No MCP servers are registered in Open WebUI." /> }}
            columns={[
              {
                title: "Server",
                key: "name",
                render: (_, c) => (
                  <div className="connector-cell">
                    <Text strong>{c.name}</Text>
                    <Text type="secondary" className="identity" style={{ display: "block" }}>
                      {c.id} · {c.url}
                    </Text>
                  </div>
                ),
              },
              { title: "Capability", key: "permission", render: (_, c) => <span className="identity">{c.permission}</span> },
              {
                title: "On",
                key: "enabled",
                width: 90,
                render: (_, c) => (c.enabled ? <Tag color="green">On</Tag> : <Tag>Off</Tag>),
              },
            ]}
          />
        </>
      )}
    </div>
  );
}

function ConnectorsScreen({ hub }: { hub: Hub }) {
  const { message, modal } = App.useApp();
  const data = useConnectors();
  const [offering, setOffering] = useState(false);
  const [editing, setEditing] = useState<Connector | "new" | null>(null);
  const [test, setTest] = useState<TestRun | null>(null);
  const [switching, setSwitching] = useState<string | null>(null);

  async function runTest(c: Connector) {
    setTest({ connector: c, busy: true });
    try {
      const result = await connectorsRequest<ConnectorTest>(`/${encodeURIComponent(c.id)}/test`, "POST");
      setTest((t) => (t?.connector.id === c.id ? { connector: c, busy: false, result } : t));
    } catch (e) {
      setTest((t) => (t?.connector.id === c.id ? { connector: c, busy: false, error: errorText(e) } : t));
    }
  }

  async function setEnabled(c: Connector, enabled: boolean) {
    setSwitching(c.id);
    try {
      await connectorsRequest(`/${encodeURIComponent(c.id)}`, "PATCH", { enabled });
      message.success(enabled ? `${c.name} is switched on.` : `${c.name} is switched off.`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setSwitching(null);
      data.reload();
    }
  }

  async function offer(id: string) {
    setOffering(true);
    try {
      await connectorsRequest(`/${encodeURIComponent(id)}/agents/${encodeURIComponent(hub.key)}`, "PUT");
      message.success(`${hub.name} now offers it. Grant “Connect …” in Access to the people who use it.`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setOffering(false);
      data.reload();
    }
  }

  function withdraw(c: Connector) {
    modal.confirm({
      title: `Stop offering ${c.name} in ${hub.name}?`,
      content: `People stop using it in ${hub.name}, and grants of “Connect ${c.name}” here are removed. Their connections stay, and other agents that offer it are not affected.`,
      okText: "Stop offering",
      okButtonProps: { danger: true },
      cancelText: "Keep it",
      onOk: async () => {
        try {
          await connectorsRequest(`/${encodeURIComponent(c.id)}/agents/${encodeURIComponent(hub.key)}`, "DELETE");
          message.success(`${hub.name} no longer offers ${c.name}.`);
        } catch (e) {
          message.error(errorText(e));
        } finally {
          data.reload();
        }
      },
    });
  }

  function remove(c: Connector) {
    const elsewhere = c.agents.filter((a) => a !== hub.key);
    modal.confirm({
      title: `Remove ${c.name} from every agent?`,
      content: (
        <>
          <Paragraph>
            {c.connections
              ? `${c.connections} ${c.connections === 1 ? "person loses their connection" : "people lose their connections"}. Hubzoid asks the provider to revoke that access where it supports revocation.`
              : "Nobody is connected to it."}
          </Paragraph>
          {elsewhere.length > 0 && (
            <Paragraph>
              {elsewhere.length === 1 ? "1 other agent offers" : `${elsewhere.length} other agents offer`} it too.
              To take it out of {hub.name} only, use Stop offering here.
            </Paragraph>
          )}
          <Paragraph style={{ marginBottom: 0 }}>
            Grants of “Connect {c.name}” stay listed as no longer available, so you can remove them.
          </Paragraph>
        </>
      ),
      okText: "Remove connector",
      okButtonProps: { danger: true },
      cancelText: "Keep it",
      onOk: async () => {
        try {
          await connectorsRequest(`/${encodeURIComponent(c.id)}`, "DELETE");
          message.success(`${c.name} was removed.`);
        } catch (e) {
          message.error(errorText(e));
        } finally {
          data.reload();
        }
      },
    });
  }

  const list = data.data?.filter((c) => c.agents.includes(hub.key));
  const others = data.data?.filter((c) => !c.agents.includes(hub.key)) ?? [];
  return (
    <>
      <div className="panel">
        <div className="panel-heading">
          <div>
            <Title level={2}>Connectors</Title>
            <Paragraph type="secondary">
              Remote MCP servers people connect with their own account, such as Gmail, to use in{" "}
              {hub.name}. Each person signs in for themselves, and the agent acts as them. People also
              need “Connect &lt;name&gt;” in this agent’s access.
            </Paragraph>
          </div>
          <Space wrap>
            {others.length > 0 && (
              <Select
                aria-label="Offer a connector another agent offers"
                placeholder="Offer an existing connector"
                value={null}
                loading={offering}
                style={{ minWidth: 240 }}
                options={others.map((c) => ({ value: c.id, label: c.name }))}
                onChange={(id) => id && void offer(id)}
              />
            )}
            <Button type="primary" icon={<Plug size={16} />} onClick={() => setEditing("new")}>
              Add connector
            </Button>
          </Space>
        </div>
        {!list ? (
          <LoadState error={data.error} retry={data.reload} />
        ) : (
          <>
            {data.error && (
              <Alert type="warning" showIcon title="Couldn’t refresh the list" description={data.error} style={{ marginBottom: 12 }} />
            )}
            <Table<Connector>
              rowKey="id"
              size="middle"
              dataSource={list}
              pagination={false}
              scroll={{ x: 900 }}
              locale={{
                emptyText: (
                  <Empty description="No connectors yet. Add one to let people connect an app with their own account.">
                    <Button onClick={() => setEditing("new")}>Add connector</Button>
                  </Empty>
                ),
              }}
              columns={[
                {
                  title: "Connector",
                  key: "name",
                  render: (_, c) => (
                    <div className="connector-cell">
                      <a
                        href="#"
                        onClick={(e) => {
                          e.preventDefault();
                          setEditing(c);
                        }}
                      >
                        {c.name}
                      </a>
                      <Text type="secondary" className="identity" style={{ display: "block" }}>
                        {c.id} · {c.url}
                      </Text>
                    </div>
                  ),
                },
                {
                  title: "Sign-in",
                  key: "auth",
                  width: 150,
                  render: (_, c) => <SignInTag c={c} />,
                },
                {
                  title: "Tools",
                  key: "tools",
                  width: 130,
                  render: (_, c) =>
                    c.tool_allowlist?.length ? (
                      <Tooltip title={c.tool_allowlist.join(", ")}>
                        <Tag>
                          {c.tool_allowlist.length} allowed
                        </Tag>
                      </Tooltip>
                    ) : (
                      <Text type="secondary">All tools</Text>
                    ),
                },
                {
                  title: "Connected",
                  key: "connections",
                  width: 110,
                  render: (_, c) => (
                    <Text>
                      {c.connections} {c.connections === 1 ? "person" : "people"}
                    </Text>
                  ),
                },
                {
                  title: "On",
                  key: "enabled",
                  width: 80,
                  render: (_, c) => (
                    <Switch
                      size="small"
                      checked={c.enabled}
                      loading={switching === c.id}
                      aria-label={`${c.name} switched ${c.enabled ? "on" : "off"}`}
                      onChange={(v) => void setEnabled(c, v)}
                    />
                  ),
                },
                {
                  title: "",
                  key: "actions",
                  width: 150,
                  align: "right",
                  render: (_, c) => (
                    <Space size={4}>
                      <Button icon={<FlaskConical size={14} />} onClick={() => void runTest(c)}>
                        Test
                      </Button>
                      <Dropdown
                        trigger={["click"]}
                        menu={{
                          items: [
                            { key: "edit", icon: <Pencil size={14} />, label: "Edit" },
                            { key: "withdraw", icon: <MinusCircle size={14} />, label: `Stop offering in ${hub.name}` },
                            { key: "delete", icon: <Trash2 size={14} />, label: "Remove from every agent", danger: true },
                          ],
                          onClick: ({ key }) =>
                            key === "edit" ? setEditing(c) : key === "withdraw" ? withdraw(c) : remove(c),
                        }}
                      >
                        <Button icon={<MoreHorizontal size={16} />} aria-label={`More actions for ${c.name}`} />
                      </Dropdown>
                    </Space>
                  ),
                },
              ]}
            />
          </>
        )}
      </div>
      <ConnectorDrawer
        key={editing === "new" ? "new" : (editing?.id ?? "closed")}
        hubKey={hub.key}
        target={editing}
        onClose={() => setEditing(null)}
        onSaved={(c, created) => {
          data.reload();
          if (created) void runTest(c);
        }}
      />
      <TestDrawer run={test} onRetry={(c) => void runTest(c)} onClose={() => setTest(null)} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Add and edit
// ---------------------------------------------------------------------------

type Draft = {
  id: string;
  name: string;
  url: string;
  auth: "oauth" | "none";
  clientId: string;
  secret: string;
  dropSecret: boolean;
  scopes: string;
  tools: string[];
  enabled: boolean;
};

const EMPTY: Draft = {
  id: "",
  name: "",
  url: "",
  auth: "oauth",
  clientId: "",
  secret: "",
  dropSecret: false,
  scopes: "",
  tools: [],
  enabled: true,
};

function draftOf(c: Connector): Draft {
  return {
    id: c.id,
    name: c.name,
    url: c.url,
    auth: c.auth_type,
    clientId: c.client_id ?? "",
    secret: "",
    dropSecret: false,
    scopes: c.scopes ?? "",
    tools: c.tool_allowlist ?? [],
    enabled: c.enabled,
  };
}

const slug = (name: string) => {
  let s = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (s && !/^[a-z]/.test(s)) s = "c_" + s;
  return s.slice(0, 40).replace(/_+$/, "");
};

function urlProblem(url: string): string | null {
  const v = url.trim();
  if (!v) return "Enter the server’s URL.";
  let parsed: URL;
  try {
    parsed = new URL(v);
  } catch {
    return "Enter a full URL, for example https://mcp.example.com/mcp.";
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && local))
    return "Use https:// (plain http only on this computer, for development).";
  if (parsed.hash) return "Remove the fragment (#…).";
  if (parsed.username || parsed.password) return "Remove the user name and password.";
  return null;
}

function ConnectorDrawer({
  hubKey,
  target,
  onClose,
  onSaved,
}: {
  hubKey: string;
  target: Connector | "new" | null;
  onClose: () => void;
  onSaved: (c: Connector, created: boolean) => void;
}) {
  const { message, modal } = App.useApp();
  const creating = target === "new";
  const existing = target && target !== "new" ? target : null;
  // The parent keys this drawer by its target, so each opening starts fresh.
  const [draft, setDraft] = useState<Draft>(() => (existing ? draftOf(existing) : EMPTY));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const initial = existing ? draftOf(existing) : EMPTY;
  const dirty = !!target && JSON.stringify(draft) !== JSON.stringify(initial);
  const reset = useCallback(() => {
    setDraft(EMPTY);
    setTouched(false);
  }, []);
  const guard = useMemo(() => (target ? { dirty, busy, discard: reset } : null), [target, dirty, busy, reset]);
  useNavigationGuard(guard);

  const id = creating ? draft.id.trim() || slug(draft.name) : draft.id;
  const problems = {
    name: draft.name.trim() ? null : "Enter a name people will recognise, for example Gmail.",
    id: creating && !/^[a-z][a-z0-9_]{1,39}$/.test(id) ? "Use 2 to 40 lowercase letters, digits or _, starting with a letter." : null,
    url: urlProblem(draft.url),
    secret: draft.auth === "oauth" && draft.secret && !draft.clientId.trim() ? "A client secret needs its client ID." : null,
  };
  const invalid = Object.values(problems).some(Boolean);
  const redirect = `${location.origin}/oauth/connectors/${id || "<id>"}/callback`;

  function close() {
    if (busy) return;
    if (!dirty) {
      onClose();
      return;
    }
    modal.confirm({
      title: "Discard your changes?",
      content: "What you entered will be lost.",
      okText: "Discard",
      okButtonProps: { danger: true },
      cancelText: "Keep editing",
      onOk: onClose,
    });
  }

  async function save() {
    setTouched(true);
    if (invalid) return;
    const oauth = draft.auth === "oauth";
    const body: ConnectorInput = {
      name: draft.name.trim(),
      url: draft.url.trim(),
      auth_type: draft.auth,
      scopes: oauth ? draft.scopes.trim() || null : null,
      tool_allowlist: draft.tools.length ? draft.tools : null,
      enabled: draft.enabled,
    };
    if (oauth) {
      body.client_id = draft.clientId.trim() || null;
      if (draft.secret) body.client_secret = draft.secret;
      else if (draft.dropSecret || !body.client_id) body.client_secret = null;
    }
    if (creating) body.id = id;
    setBusy(true);
    setFailure(null);
    try {
      const r = creating
        ? await connectorsRequest<{ connector: Connector }>(`?hub=${encodeURIComponent(hubKey)}`, "POST", body)
        : await connectorsRequest<{ connector: Connector }>(`/${encodeURIComponent(existing!.id)}`, "PATCH", body);
      const moved = existing && (existing.url !== body.url || existing.auth_type !== body.auth_type);
      message.success(
        creating
          ? `${r.connector.name} was added.`
          : moved && existing.connections
            ? `${r.connector.name} was saved. People connected to the old server need to connect again.`
            : `${r.connector.name} was saved.`,
      );
      setDraft(EMPTY);
      onSaved(r.connector, creating);
      onClose();
    } catch (e) {
      setFailure(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const title = creating ? "Add connector" : `Edit ${existing?.name ?? "connector"}`;
  const movesPeople =
    existing && existing.connections > 0 && (draft.url.trim() !== existing.url || draft.auth !== existing.auth_type);
  return (
    <Drawer
      title={title}
      aria-label={title}
      open={!!target}
      onClose={close}
      size={560}
      closable={!busy}
      mask={{ closable: !busy }}
      keyboard={!busy}
      destroyOnHidden
      footer={
        <Space className="drawer-actions">
          <Button onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button type="primary" loading={busy} disabled={touched && invalid} onClick={() => void save()}>
            {creating ? "Add connector" : "Save"}
          </Button>
        </Space>
      }
    >
      <div className="drawer-body">
        {failure && <Alert type="error" showIcon title="Not saved" description={failure} />}
        <Field id="connector-name" label="Name" problem={touched ? problems.name : null} help="What people see, for example Gmail.">
          <Input id="connector-name" value={draft.name} maxLength={80} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field
          id="connector-id"
          label="ID"
          problem={touched ? problems.id : null}
          help={
            creating
              ? `Also the capability connector_${id || "<id>"}. It can’t be changed later.`
              : `The capability is ${existing?.capability}.`
          }
        >
          <Input
            id="connector-id"
            className="identity"
            value={draft.id}
            placeholder={creating ? slug(draft.name) || "gmail" : undefined}
            disabled={!creating}
            maxLength={40}
            onChange={(e) => set("id", e.target.value.toLowerCase())}
          />
        </Field>
        <Field id="connector-url" label="Server URL" problem={touched ? problems.url : null} help="The remote MCP server’s endpoint.">
          <Input
            id="connector-url"
            className="identity"
            value={draft.url}
            placeholder="https://mcp.example.com/mcp"
            onChange={(e) => set("url", e.target.value)}
          />
        </Field>
        {movesPeople && (
          <Alert
            type="warning"
            showIcon
            title="People will need to connect again"
            description={`${existing!.connections} ${existing!.connections === 1 ? "person is" : "people are"} connected to the current server. A different server or sign-in method removes those connections.`}
          />
        )}
        <div className="field">
          <span className="field-label" id="connector-auth-label">Sign-in</span>
          <Segmented<"oauth" | "none">
            aria-labelledby="connector-auth-label"
            value={draft.auth}
            onChange={(v) => set("auth", v)}
            options={[
              { value: "oauth", label: "Each person signs in (OAuth)" },
              { value: "none", label: "No sign-in" },
            ]}
          />
          <Text type="secondary" className="field-help">
            {draft.auth === "oauth"
              ? "People authorize with their own account at the provider. Hubzoid stores their tokens encrypted."
              : "For servers that need no account. People turn it on for themselves."}
          </Text>
        </div>
        {draft.auth === "oauth" && (
          <>
            <Field
              id="connector-client-id"
              label="Client ID (optional)"
              help="Leave empty when the provider lets Hubzoid register itself. Otherwise register Hubzoid with the provider using the redirect URI below."
            >
              <Input
                id="connector-client-id"
                className="identity"
                value={draft.clientId}
                maxLength={512}
                onChange={(e) => set("clientId", e.target.value)}
              />
            </Field>
            {draft.clientId.trim() && (
              <Field
                id="connector-secret"
                label="Client secret (optional)"
                problem={touched ? problems.secret : null}
                help={
                  existing?.has_client_secret && !draft.dropSecret
                    ? "A secret is stored. Leave empty to keep it. It is never shown again."
                    : "Stored encrypted and never shown again."
                }
              >
                <Input.Password
                  id="connector-secret"
                  autoComplete="new-password"
                  value={draft.secret}
                  disabled={draft.dropSecret}
                  placeholder={existing?.has_client_secret && !draft.dropSecret ? "••••••••" : undefined}
                  onChange={(e) => set("secret", e.target.value)}
                />
                {existing?.has_client_secret && (
                  <Checkbox checked={draft.dropSecret} onChange={(e) => { set("dropSecret", e.target.checked); set("secret", ""); }}>
                    Remove the stored secret
                  </Checkbox>
                )}
              </Field>
            )}
            <div className="field">
              <span className="field-label">Redirect URI</span>
              <Text className="identity" copyable={{ text: existing?.redirect_uri ?? redirect }}>
                {existing?.redirect_uri ?? redirect}
              </Text>
            </div>
            <Field id="connector-scopes" label="Scopes (optional)" help="Separated by spaces. Leave empty to request what the server asks for.">
              <Input
                id="connector-scopes"
                className="identity"
                value={draft.scopes}
                onChange={(e) => set("scopes", e.target.value)}
              />
            </Field>
          </>
        )}
        <Field id="connector-tools" label="Allowed tools (optional)" help="Only these tools reach agents. Leave empty to allow every tool the server offers.">
          <Select
            id="connector-tools"
            mode="tags"
            value={draft.tools}
            tokenSeparators={[",", " "]}
            open={false}
            suffixIcon={null}
            placeholder="search_threads, get_thread"
            onChange={(v: string[]) => set("tools", v)}
            style={{ width: "100%" }}
          />
        </Field>
        <div className="field">
          <Space>
            <Switch id="connector-enabled" checked={draft.enabled} onChange={(v) => set("enabled", v)} />
            <label htmlFor="connector-enabled">Switched on</label>
          </Space>
          <Text type="secondary" className="field-help">
            Switched off, people can’t connect it and agents don’t use it. Existing connections are kept.
          </Text>
        </div>
      </div>
    </Drawer>
  );
}

function Field({
  id,
  label,
  help,
  problem,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  problem?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      {children}
      {(problem || help) && (
        <Text type={problem ? "danger" : "secondary"} className="field-help">
          {problem ?? help}
        </Text>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Test discovery
// ---------------------------------------------------------------------------

type TestRun = { connector: Connector; busy: boolean; result?: ConnectorTest; error?: string };

function TestDrawer({
  run,
  onRetry,
  onClose,
}: {
  run: TestRun | null;
  onRetry: (c: Connector) => void;
  onClose: () => void;
}) {
  const connector = run?.connector ?? null;
  const busy = !!run?.busy;
  const result = run?.result ?? null;
  const failure = run?.error ?? null;
  const title = connector ? `Test ${connector.name}` : "Test";
  const yesNo = (v: boolean | null | undefined) => (v ? "Yes" : "No");
  const items = result
    ? result.auth_type === "none"
      ? [
          { key: "status", label: "Server answered", children: result.status ?? "No answer" },
          { key: "auth", label: "Asks for sign-in", children: yesNo(result.requires_auth) },
        ]
      : [
          { key: "issuer", label: "Authorization server", children: <Text className="identity">{result.issuer}</Text> },
          {
            key: "registration",
            label: "Client",
            children:
              result.registration === "pre-registered"
                ? "Your pre-registered client"
                : result.registration === "dynamic"
                  ? "Hubzoid registers itself (dynamic registration)"
                  : "Needs a pre-registered client",
          },
          { key: "resource", label: "Resource", children: <Text className="identity">{result.resource ?? "Not sent"}</Text> },
          { key: "scope", label: "Scopes requested", children: <Text className="identity">{result.scope || "The server’s default"}</Text> },
          { key: "pkce", label: "PKCE", children: result.pkce === "S256" ? "S256" : "S256 (not advertised)" },
          { key: "revocation", label: "Revocation on disconnect", children: yesNo(!!result.revocation_endpoint) },
          { key: "iss", label: "Issuer in the response (RFC 9207)", children: yesNo(result.iss_parameter_supported) },
          {
            key: "redirect",
            label: "Redirect URI",
            children: (
              <Text className="identity" copyable>
                {result.redirect_uri}
              </Text>
            ),
          },
        ]
    : [];

  return (
    <Drawer title={title} aria-label={title} open={!!connector} onClose={onClose} size={560} destroyOnHidden>
      <div className="drawer-body" aria-live="polite">
        {busy && (
          <div role="status" aria-label="Testing" style={{ padding: 24, textAlign: "center" }}>
            <Spin />
          </div>
        )}
        {failure && <Alert type="error" showIcon title="The test could not run" description={failure} />}
        {result && (
          <>
            {result.ok ? (
              <Alert
                type="success"
                showIcon
                title="Ready"
                description={
                  result.auth_type === "none"
                    ? "The server answers without sign-in. People can turn it on."
                    : "Discovery succeeded. People can connect with their own account."
                }
              />
            ) : (
              <Alert type="error" showIcon title="Not ready" description={result.error?.message ?? "The server did not answer as expected."} />
            )}
            {items.length > 0 && <Descriptions column={1} size="small" bordered items={items} />}
            {result.notes?.map((n) => (
              <Text key={n} type="secondary" className="field-help">
                {n}
              </Text>
            ))}
            <Text type="secondary" className="field-help">
              A test only reads the server’s metadata. Nothing was registered or changed.
            </Text>
          </>
        )}
        {connector && !busy && (
          <div>
            <Button onClick={() => onRetry(connector)}>Test again</Button>
          </div>
        )}
      </div>
    </Drawer>
  );
}
