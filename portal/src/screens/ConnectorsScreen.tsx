import { useCallback, useEffect, useState } from "react";
import { Alert, Space, Table, Tag, Tooltip, Typography } from "antd";
import {
  ApiError,
  connectorsRequest,
  type Connector,
  type Hub,
  type HubMcpServer,
  type Me,
} from "../api";
import { InfoHelp, LoadState } from "../components/common";
import { useData } from "../hooks/useData";
import { CATALOG } from "./connectorCatalog";
import { LOGOS } from "./connectorLogos";

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
  if (c.auth_type === "shared")
    return (
      <Tooltip title="One company key for everyone. Nobody connects.">
        <Tag color="purple">Shared key</Tag>
      </Tooltip>
    );
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
 * An agent's Connectors tab, the same in both UI modes. Connectors are
 * registered once for the deployment and offered in the agents that list them
 * (organization administrators). The hub folder's own servers are listed
 * read-only below.
 */
export function AgentConnectors({ hub, me, embedded = false }: { hub: Hub; me: Me; embedded?: boolean }) {
  if (!me.org_admin)
    return (
      <div className="panel">
        <Title level={2}>
          Connectors
          <InfoHelp text="Connectors are defined in connectors/<id>.yaml in the deployment folder. Who may use each one is in this agent’s Access." />
        </Title>
        <HubFolderServers hub={hub} />
      </div>
    );
  return <ConnectorsScreen hub={hub} embedded={embedded} />;
}

/** The hub folder's connectors/.mcp.json, read-only. Names and settings only. */
function HubFolderServers({ hub, panel = false }: { hub: Hub; panel?: boolean }) {
  const data = useData<{ servers: HubMcpServer[]; warning: string | null }>(
    "/hub-mcp-servers?hub=" + encodeURIComponent(hub.key),
  );
  if (!data.data) return panel ? null : <LoadState error={data.error} retry={data.reload} />;
  const { servers, warning } = data.data;
  if (!servers.length && !warning) return null;
  return (
    <div className={panel ? "panel hub-folder-servers" : "hub-folder-servers"} style={panel ? undefined : { marginTop: 20 }}>
      <Title level={4}>
        From the hub folder
        <InfoHelp text="Servers in connectors/.mcp.json. Everyone who can use this agent gets them. Change them in the hub folder." />
      </Title>
      {warning && <Alert type="warning" showIcon title={warning} style={{ marginBottom: 8 }} />}
      <Table<HubMcpServer>
        rowKey="name"
        size="small"
        pagination={false}
        dataSource={servers}
        columns={[
          { title: "Server", key: "name", render: (_, s) => <Text strong>{s.name}</Text> },
          {
            title: "Kind",
            key: "kind",
            render: (_, s) =>
              s.source === "hubzoid" ? "Added by Hubzoid" : s.kind === "command" ? "Local command" : "URL",
          },
          {
            title: "Settings",
            key: "env",
            render: (_, s) =>
              s.env.length === 0 ? (
                <Tag color="green">Ready</Tag>
              ) : s.env.every((e) => e.set) ? (
                <Tag color="green">Ready</Tag>
              ) : (
                <Tooltip title={`Missing in .env: ${s.env.filter((e) => !e.set).map((e) => e.name).join(", ")}`}>
                  <Tag color="orange">Missing settings</Tag>
                </Tooltip>
              ),
          },
        ]}
      />
    </div>
  );
}


/**
 * The deployment's connectors, read-only. Each is defined in
 * connectors/<id>.yaml in the deployment folder (the hub folder for a
 * deployment of one): change the file, or ask Claude Code or Codex to. People
 * connect their own accounts on their Connections page.
 */
function ConnectorsScreen({ hub, embedded }: { hub: Hub; embedded: boolean }) {
  const { data, error, reload } = useConnectors();
  if (!data) return <LoadState error={error} retry={reload} />;
  const here = data.filter((c) => (c.agents ?? []).includes(hub.key));
  const other = data.filter((c) => !(c.agents ?? []).includes(hub.key));
  const columns = [
    {
      title: "Connector",
      key: "name",
      render: (_: unknown, c: Connector) => (
        <Space>
          <ConnectorLogo id={c.id} url={c.url} name={c.name} />
          <span>
            <Text strong>{c.name}</Text>
            <br />
            <Text type="secondary" code>connectors/{c.id}.yaml</Text>
          </span>
        </Space>
      ),
    },
    { title: "Sign-in", key: "auth", render: (_: unknown, c: Connector) => <SignInTag c={c} /> },
    { title: "Server", key: "url", render: (_: unknown, c: Connector) => <Text type="secondary">{c.url}</Text> },
    {
      title: "State",
      key: "state",
      render: (_: unknown, c: Connector) => (c.enabled ? <Tag color="green">On</Tag> : <Tag>Switched off</Tag>),
    },
  ];
  return (
    <div className={embedded ? undefined : "panel"} data-testid="connectors-read-only">
      <Title level={embedded ? 4 : 2}>Connectors</Title>
      <Alert
        type="info"
        showIcon
        title="Connectors are defined in files"
        description="Each one is connectors/<id>.yaml in the deployment folder: its server, how people sign in, its tools and the agents that offer it. Change the file (or ask Claude Code or Codex to); secrets stay in .env. Who may use a connector is in this agent's Access."
        style={{ marginBottom: 12 }}
      />
      <Table<Connector> rowKey="id" size="small" pagination={false} dataSource={here} columns={columns}
        locale={{ emptyText: "No connector is offered in this agent." }} />
      {other.length > 0 && (
        <>
          <Paragraph type="secondary" style={{ marginTop: 16 }}>Offered in other agents:</Paragraph>
          <Table<Connector> rowKey="id" size="small" pagination={false} dataSource={other} columns={columns} />
        </>
      )}
      <HubFolderServers hub={hub} />
    </div>
  );
}

export function ConnectorLogo({ id, url, name }: { id: string; url: string; name: string }) {
  const host = (() => {
    try {
      return new URL(url).host;
    } catch {
      return "";
    }
  })();
  const known = LOGOS[id] ?? LOGOS[CATALOG.find((e) => host && new URL(e.url).host === host)?.id ?? ""];
  return (
    <span className="catalog-tile" aria-hidden>
      {known ? (
        <svg viewBox="0 0 24 24" className={known.ink ? `logo ink-${known.ink}` : "logo"} style={{ color: known.hex }}>
          <path d={known.path} fill="currentColor" />
        </svg>
      ) : (
        name.slice(0, 1).toUpperCase()
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Test discovery
// ---------------------------------------------------------------------------
