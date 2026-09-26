import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Empty,
  Input,
  Space,
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
import { useHashQuery } from "../../hooks/useRoute";
import { EVERYONE, USE_HUB, normalizeSubject, personName, toCatalog } from "../../lib/format";
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
  const [removingEveryone, setRemovingEveryone] = useState(false);

  // Deep link from Person → Edit access (#/agents/<hub>/access?edit=<subject>):
  // open that person's editor directly instead of the whole access list.
  const [q] = useHashQuery();
  const openedEdit = useRef<string>("");
  useEffect(() => {
    const edit = q.edit ? normalizeSubject(q.edit) : "";
    const token = `${hub.key}:${edit}`;
    if (!edit || draft || openedEdit.current === token) return;
    openedEdit.current = token;
    let cancelled = false;
    void (async () => {
      try {
        const res = await request<Access>(
          "/access" + query({ hub: hub.key, q: edit, limit: 200 }),
        );
        if (cancelled) return;
        const row = res.rows.find((r) => r.subject === edit);
        setDraft(row ? draftFor(row) : { ...draftFor(), subject: edit });
      } catch {
        if (!cancelled) setDraft({ ...draftFor(), subject: edit });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [q.edit, draft, hub.key]);

  const access = data.data;
  const catalog = useMemo(() => toCatalog(access?.permissions), [access?.permissions]);
  const order = useMemo(
    () => (access?.permissions ?? []).map((p) => p.permission),
    [access?.permissions],
  );

  // "Everyone signed in" can no longer be granted. An existing grant (carried
  // over from before named grants were required) stays visible and working
  // until an organization administrator removes it here.
  function askRemoveEveryone() {
    if (!access) return;
    const target = access.hub;
    const n = access.public_reliant ?? 0;
    modal.confirm({
      title: `Remove access for everyone signed in to ${hub.name}?`,
      content: `${n === 1 ? "1 chat account opens" : `${n} chat accounts open`} ${hub.name} only through this and will lose entry, as will anyone who signs up later. Add the people who need it by name first. Named grants are not affected.`,
      okText: "Remove access for everyone",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: async () => {
        setRemovingEveryone(true);
        try {
          await request("/access/revoke", { subject: EVERYONE, hub: target, permission: USE_HUB });
          setNotice(`${hub.name} is no longer open to everyone signed in.`);
          data.reload();
        } catch (e) {
          message.error(errorText(e));
        } finally {
          setRemovingEveryone(false);
        }
      },
    });
  }

  if (!access) return <LoadState error={data.error} retry={data.reload} />;

  // After a save (esp. a partial failure) the rows are refetched. Lock the
  // entry points until fresh data lands so nobody edits stale permissions. A legacy
  // (un-migrated) hub is read-only here: its access lives in the chat app and the API
  // refuses edits, so the controls are locked to match.
  const refreshing = data.refreshing;
  const locked = refreshing || !access.editable;

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
          access.can_manage_admins ? (
            <Button
              danger
              onClick={askRemoveEveryone}
              loading={removingEveryone}
              disabled={locked}
              aria-label="Remove access for everyone signed in"
            >
              Remove
            </Button>
          ) : (
            <Tooltip title="Only organization administrators can remove this.">
              <Text type="secondary" tabIndex={0}>Read-only</Text>
            </Tooltip>
          )
        ) : (
          <Button
            onClick={() => setDraft(draftFor(r))}
            disabled={locked}
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
          <Title level={2}>Access to {hub.name}</Title>
          <Paragraph type="secondary">
            People and services with direct access, and what each is allowed to do.
          </Paragraph>
        </div>
        <Button
          type="primary"
          icon={<Plus size={16} />}
          disabled={locked}
          onClick={() => setDraft(draftFor())}
        >
          Add person
        </Button>
      </div>

      {!access.authoritative && (
        <Alert
          type="warning"
          showIcon
          title="This agent’s access is managed in the chat app"
          description="It hasn’t been moved to the dashboard yet, so access is read-only here and its existing access continues to apply. After migration (hubzoid access migrate) you can manage it here."
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

      {access.public && (
        <Alert
          type="info"
          showIcon
          title="Everyone signed in can use this agent"
          description={`This was carried over from before named grants were required and can’t be granted again. To replace it, add the people who need ${hub.name} by name, then remove “Everyone signed in” below.`}
        />
      )}

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
                <Button type="primary" disabled={locked} onClick={() => setDraft(draftFor())}>
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
