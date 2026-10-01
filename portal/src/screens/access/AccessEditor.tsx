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
import { Plus, Search, UsersRound } from "lucide-react";
import {
  groupsApi,
  request,
  query,
  type Access,
  type AccessRow,
  type AccessRowGroups,
  type Hub,
  type Me,
  type MeGroups,
  type Person,
} from "../../api";
import { errorText, useData } from "../../hooks/useData";
import {
  AccountTag,
  CapabilityTag,
  LoadState,
  PersonAvatar,
  PersonCell,
} from "../../components/common";
import { useHashQuery } from "../../hooks/useRoute";
import { EVERYONE, USE_HUB, isGroup, isService, normalizeSubject, personName, toCatalog } from "../../lib/format";
import { draftFor, emptyRow, orderCapabilities, viaGroups, type Draft } from "./plan";
import { AccessDrawer } from "./AccessDrawer";
import { LegacyServiceTag } from "./AccessParts";
import { GroupPicker } from "./GroupPicker";

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
  // Whether this viewer may create accounts, and how new accounts can sign in.
  const me = useData<Me>("/me");
  const [draft, setDraft] = useState<Draft | null>(null);
  // Each result is a new alert (keyed), so a repeated "Access updated" is announced again.
  const [notice, setNotice] = useState({ text: "", n: 0 });
  const announce = (text: string) => setNotice((prev) => ({ text, n: prev.n + 1 }));
  const [removingEveryone, setRemovingEveryone] = useState(false);
  const [pickingGroup, setPickingGroup] = useState(false);

  /** The access row of a user with no access to this agent yet: who they are
   *  and their account state (a blocked user shows as blocked before saving). */
  async function personRow(subject: string): Promise<AccessRow> {
    if (isGroup(subject)) return groupRow(subject);
    try {
      const res = await request<{ people: Person[] }>("/people" + query({ q: subject, limit: 20 }));
      const p = res.people.find((x) => x.subject === subject);
      if (p)
        return {
          ...emptyRow(subject),
          display: p.display ?? "",
          status: p.status,
          suspended: p.suspended,
          account_unavailable: p.account_unavailable,
        };
    } catch {
      // Unknown here: the server re-checks the account when saving.
    }
    return emptyRow(subject);
  }

  /** The access row of a group with no access to this agent yet. */
  async function groupRow(subject: string): Promise<AccessRow> {
    const id = subject.slice("group:".length);
    try {
      const { group } = await groupsApi.get(id);
      return groupDraftRow(subject, group.name, group.member_count);
    } catch {
      return groupDraftRow(subject, "", 0);
    }
  }

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
        if (row) {
          setDraft(draftFor(row));
          return;
        }
        // A user with no access here yet: edit them, starting with entry.
        const person = await personRow(edit);
        if (!cancelled) setDraft({ ...draftFor(person), selected: [USE_HUB] });
      } catch {
        if (!cancelled) setDraft({ ...draftFor(emptyRow(edit)), selected: [USE_HUB] });
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
      content: `${n === 1 ? "1 user opens" : `${n} users open`} ${hub.name} only through this and will lose entry, as will anyone who signs up later. Add the people who need it by name first. Named grants are not affected.`,
      okText: "Remove access for everyone",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: async () => {
        setRemovingEveryone(true);
        try {
          await request("/access/revoke", { subject: EVERYONE, hub: target, permission: USE_HUB });
          announce(`${hub.name} is no longer open to everyone signed in.`);
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
  // Groups exist in the web app mode; organization administrators give them access.
  const groupsHere = (me.data as MeGroups | undefined)?.groups === true;
  const canAddGroup = groupsHere && access.can_manage_admins;

  const columns = [
    {
      title: groupsHere ? "Person or group" : "Person",
      key: "person",
      render: (_: unknown, r: AccessRow) =>
        isGroup(r.subject) ? (
          <GroupCell row={r as AccessRowGroups} link={canAddGroup} />
        ) : (
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
                    : viaGroups(r, p).length
                      ? "group"
                      : "public"
              }
              groups={viaGroups(r, p)}
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
      width: 180,
      // A legacy `workflow:` identity keeps its grants and controls, labelled as such.
      render: (status: string, r: AccessRow) =>
        isService(r.subject) ? (
          <Space wrap size={[4, 4]}>
            <LegacyServiceTag />
            {status !== "service" && <AccountTag status={status} />}
          </Space>
        ) : (
          <AccountTag status={status} />
        ),
    },
    {
      title: "",
      key: "actions",
      width: 140,
      align: "right" as const,
      render: (_: unknown, r: AccessRow) =>
        isGroup(r.subject) && !access.can_manage_admins ? (
          <Tooltip title="Only organization administrators can change a group’s access.">
            <Text type="secondary" tabIndex={0}>Read-only</Text>
          </Tooltip>
        ) : r.subject === EVERYONE ? (
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
            {groupsHere
              ? "People and groups with access, and what each is allowed to do."
              : "People with direct access, and what each is allowed to do."}
          </Paragraph>
        </div>
        <Space wrap>
          {canAddGroup && (
            <Button icon={<UsersRound size={16} />} disabled={locked} onClick={() => setPickingGroup(true)}>
              Add group
            </Button>
          )}
          <Button
            type="primary"
            icon={<Plus size={16} />}
            disabled={locked}
            onClick={() => setDraft(draftFor())}
          >
            Add user
          </Button>
        </Space>
      </div>

      {!access.authoritative && (
        <Alert
          type="warning"
          showIcon
          title="This agent’s access is managed in the chat app"
          description="It hasn’t been moved to the dashboard yet, so access is read-only here and its existing access continues to apply. After migration (hubzoid access migrate) you can manage it here."
        />
      )}
      {notice.text && (
        <Alert
          key={notice.n}
          type="success"
          showIcon
          title={notice.text}
          closable={{ onClose: () => setNotice((prev) => ({ ...prev, text: "" })) }}
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
                  Add the first user
                </Button>
              )}
            </Empty>
          ),
        }}
      />

      {canAddGroup && (
        <GroupPicker
          open={pickingGroup}
          hubName={hub.name}
          exclude={access.rows.map((r) => (r as AccessRowGroups).group_id).filter((id): id is string => !!id)}
          onClose={() => setPickingGroup(false)}
          onPick={(group) => {
            setPickingGroup(false);
            // A group with no access here yet: start from entry, like a new user.
            const row = groupDraftRow(group.subject, group.name, group.member_count);
            setDraft({ ...draftFor(row), selected: [USE_HUB] });
          }}
        />
      )}

      <AccessDrawer
        hub={hub}
        access={access}
        me={me.data}
        draft={draft}
        setDraft={setDraft}
        onReload={data.reload}
        onSaved={(text) => {
          announce(text || "Access updated");
          data.reload();
        }}
      />
    </div>
  );
}

/** The row a group starts from when it has no access to this agent yet. */
function groupDraftRow(subject: string, name: string, members: number): AccessRowGroups {
  return {
    ...emptyRow(subject),
    kind: "group",
    status: "group",
    display: name,
    group_id: subject.slice("group:".length),
    members,
  };
}

/** A group in the access list: its name and size, linked to its members for
 *  the organization administrators who manage groups. */
function GroupCell({ row, link }: { row: AccessRowGroups; link: boolean }) {
  const members = row.members ?? 0;
  return (
    <Space align="center">
      <PersonAvatar subject={row.subject} display={row.display} />
      <div className="person-cell">
        {link && row.group_id ? (
          <a href={`#/groups/${encodeURIComponent(row.group_id)}`}>{row.display}</a>
        ) : (
          <Text strong>{row.display}</Text>
        )}
        <div>
          <Text type="secondary">
            Group · {members} {members === 1 ? "member" : "members"}
          </Text>
        </div>
      </div>
    </Space>
  );
}
