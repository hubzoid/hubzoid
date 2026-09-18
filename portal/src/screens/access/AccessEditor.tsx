import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Empty,
  Input,
  Space,
  Switch,
  Table,
  Tooltip,
  Typography,
} from "antd";
import { Plus, Search } from "lucide-react";
import { request, query, type Access, type AccessRow, type Hub } from "../../api";
import { errorText, useData } from "../../hooks/useData";
import {
  AccountTag,
  CapabilityTag,
  LoadState,
  PersonCell,
} from "../../components/common";
import { EVERYONE, USE_HUB, personName, toCatalog } from "../../lib/format";
import { draftFor, orderCapabilities, type Draft } from "./plan";
import { AccessDrawer } from "./AccessDrawer";

const { Text, Title, Paragraph } = Typography;
const PAGE = 50;

export function AccessEditor({ hub }: { hub: Hub }) {
  const { modal, message } = App.useApp();
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(input.trim());
      setPage(1);
    }, 250);
    return () => clearTimeout(t);
  }, [input]);

  const data = useData<Access>(
    "/access" + query({ hub: hub.key, q: search, offset: (page - 1) * PAGE, limit: PAGE }),
  );
  const [draft, setDraft] = useState<Draft | null>(null);
  const [notice, setNotice] = useState("");
  const [publicBusy, setPublicBusy] = useState(false);
  const access = data.data;
  const catalog = useMemo(() => toCatalog(access?.permissions), [access?.permissions]);
  const order = useMemo(
    () => (access?.permissions ?? []).map((p) => p.permission),
    [access?.permissions],
  );

  function askPublic(next: boolean) {
    if (!access) return;
    const target = access.hub;
    modal.confirm({
      title: next
        ? `Open ${hub.name} to everyone who signs in?`
        : `Turn off public access to ${hub.name}?`,
      content: next
        ? "Anyone with a chat account will be able to use this agent. Restricted tools still require their own capabilities."
        : "Only people listed with direct access will be able to use this agent. Direct grants are not affected.",
      okText: next ? "Open to everyone" : "Turn off public access",
      okButtonProps: { danger: !next },
      cancelText: "Cancel",
      onOk: async () => {
        setPublicBusy(true);
        try {
          await request("/access/" + (next ? "grant" : "revoke"), {
            subject: EVERYONE,
            hub: target,
            permission: USE_HUB,
          });
          setNotice(
            next
              ? `${hub.name} is now open to everyone signed in.`
              : `${hub.name} is no longer open to everyone.`,
          );
          data.reload();
        } catch (e) {
          message.error(errorText(e));
        } finally {
          setPublicBusy(false);
        }
      },
    });
  }

  if (!access) return <LoadState error={data.error} retry={data.reload} />;

  const columns = [
    {
      title: "Person",
      key: "person",
      render: (_: unknown, r: AccessRow) => (
        <PersonCell subject={r.subject} display={r.display} />
      ),
    },
    {
      title: "Capabilities",
      key: "capabilities",
      render: (_: unknown, r: AccessRow) => (
        <Space wrap size={[6, 6]}>
          {orderCapabilities(r.effective, order).map((p) => (
            <CapabilityTag
              key={p}
              permission={p}
              catalog={catalog}
              via={
                r.inherited.includes(p)
                  ? "inherited"
                  : r.perms.includes(p) || r.subject === EVERYONE
                    ? undefined
                    : "public"
              }
            />
          ))}
          {r.effective.length === 0 && <Text type="secondary">No access</Text>}
        </Space>
      ),
    },
    {
      title: "Account",
      dataIndex: "status",
      key: "status",
      width: 160,
      render: (status: string) => <AccountTag status={status} />,
    },
    {
      title: "",
      key: "actions",
      width: 140,
      align: "right" as const,
      render: (_: unknown, r: AccessRow) =>
        r.subject === EVERYONE ? (
          <Text type="secondary">Public access</Text>
        ) : (
          <Button
            onClick={() => setDraft(draftFor(r))}
            aria-label={`Edit access for ${personName(r.subject, r.display)}`}
          >
            Edit access
          </Button>
        ),
    },
  ];

  return (
    <div className="panel">
      <div className="panel-heading">
        <div>
          <Title level={4}>Who can use {hub.name}</Title>
          <Paragraph type="secondary">
            People and services with direct access, and what each is allowed to do.
          </Paragraph>
        </div>
        <Button type="primary" icon={<Plus size={16} />} onClick={() => setDraft(draftFor())}>
          Add person
        </Button>
      </div>

      {!access.authoritative && (
        <Alert
          type="warning"
          showIcon
          title="This agent still uses legacy access"
          description="Changes saved here take effect only after the agent is moved to managed access (hubzoid access migrate). Existing access continues to apply until then."
        />
      )}
      {notice && (
        <Alert
          type="success"
          showIcon
          title={notice}
          description="The chat app’s agent list updates within about 30 seconds."
          closable={{ onClose: () => setNotice("") }}
        />
      )}

      <div className="public-access">
        <Tooltip
          title={
            access.can_manage_admins
              ? undefined
              : "Only organization administrators can change public access."
          }
        >
          <Switch
            checked={access.public}
            disabled={!access.can_manage_admins || publicBusy}
            loading={publicBusy}
            aria-label="Public access"
            onChange={askPublic}
          />
        </Tooltip>
        <div>
          <Text strong>Public access</Text>
          <div>
            <Text type="secondary">
              {access.public
                ? "Everyone who can sign in can use this agent. Direct grants below still control restricted tools."
                : "Only the people listed below can use this agent."}
            </Text>
          </div>
        </div>
      </div>

      <div className="toolbar">
        <Input
          aria-label="Search people with access"
          prefix={<Search size={16} />}
          placeholder="Search by name or email"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          allowClear
          style={{ maxWidth: 360 }}
        />
        <Text type="secondary">
          {access.total} {access.total === 1 ? "entry" : "entries"}
        </Text>
      </div>

      <Table<AccessRow>
        rowKey="subject"
        size="middle"
        dataSource={access.rows}
        columns={columns}
        scroll={{ x: 720 }}
        pagination={{
          current: page,
          pageSize: PAGE,
          total: access.total,
          onChange: setPage,
          showSizeChanger: false,
          hideOnSinglePage: true,
        }}
        locale={{
          emptyText: (
            <Empty
              description={
                search
                  ? `No one matching “${search}” has access to ${hub.name}`
                  : `No one has direct access to ${hub.name} yet`
              }
            >
              {!search && (
                <Button type="primary" onClick={() => setDraft(draftFor())}>
                  Add the first person
                </Button>
              )}
            </Empty>
          ),
        }}
      />

      <AccessDrawer
        hub={hub}
        access={access}
        draft={draft}
        setDraft={setDraft}
        onReload={data.reload}
        onSaved={(subject, operations) => {
          const removedAll = operations.some(
            (op) => op.action === "revoke" && op.permission === USE_HUB,
          );
          setNotice(
            removedAll
              ? `${subject} no longer has direct access to ${hub.name}.`
              : `Access updated for ${subject}.`,
          );
          data.reload();
        }}
      />
    </div>
  );
}
