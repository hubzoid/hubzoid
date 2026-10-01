import { useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Result,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { Pencil, Plus, Search, Trash2, UserPlus } from "lucide-react";
import {
  ApiError,
  groupsApi,
  type GroupDetail,
  type GroupMember,
  type GroupSummary,
  type Hub,
  type Me,
  type MeGroups,
} from "../../api";
import { errorText } from "../../hooks/useData";
import { useCatalogs } from "../../hooks/useCatalogs";
import { href, hrefWith, navigate } from "../../hooks/useRoute";
import { AccountTag, CapabilityTag, LoadState, PersonAvatar, PersonCell, When } from "../../components/common";
import { USE_HUB, personName } from "../../lib/format";
import { orderCapabilities } from "../access/plan";
import { emailsProblem, nameProblem, parseEmails, useGroup, useGroupList } from "./useGroups";

const { Text, Title, Paragraph } = Typography;

const groupHref = (id: string) => href(`/groups/${encodeURIComponent(id)}`);

/** A member's account, as the access list shows it. */
function memberStatus(m: GroupMember): string | null {
  if (m.blocked) return "blocked";
  if (m.account === "active") return "active";
  if (m.account === "pending") return "pending-approval";
  if (m.account === "none") return "awaiting-signup";
  return null;
}

/**
 * Groups: people who share access. Organization administrators create them,
 * add and remove members, and give a group access from an agent's Access tab.
 * Every member gets what the group holds; the server decides and records it.
 */
export function GroupsScreen({ me, hubs, selected }: { me: Me; hubs: Hub[]; selected?: string }) {
  const available = me.org_admin && (me as MeGroups).groups === true;
  const list = useGroupList(available);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");

  if (!me.org_admin)
    return (
      <Result
        status="403"
        title="Groups are managed by organization administrators"
        subTitle="Ask an organization administrator to add people to a group or change what a group can do."
        extra={<Button href={href("/agents")}>Back to agents</Button>}
      />
    );
  if (!available)
    return (
      <Result
        status="info"
        title="Groups aren’t available here"
        subTitle="This deployment still runs the Open WebUI chat app, where groups are managed in Open WebUI. Groups arrive with the Hubzoid web app."
        extra={<Button href={href("/agents")}>Back to agents</Button>}
      />
    );

  const groups = list.data ?? [];
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? groups.filter((g) => `${g.name} ${g.description}`.toLowerCase().includes(needle))
    : groups;

  return (
    <>
      <div className="panel">
        <div className="panel-heading">
          <div>
            <Title level={1} style={{ fontSize: 28 }}>Groups</Title>
            <Paragraph type="secondary">
              People who share access. Give a group access from an agent’s Access tab: everyone in it gets
              that access, and loses it when they leave the group.
            </Paragraph>
          </div>
          <Button type="primary" icon={<Plus size={16} />} onClick={() => setCreating(true)}>
            New group
          </Button>
        </div>

        <div className="toolbar">
          <Input
            aria-label="Search groups"
            prefix={<Search size={16} />}
            placeholder="Search groups"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            allowClear
            style={{ maxWidth: 300 }}
          />
          {list.data && (
            <Text type="secondary">
              {groups.length} {groups.length === 1 ? "group" : "groups"}
            </Text>
          )}
        </div>

        {!list.data ? (
          <LoadState error={list.error} retry={list.reload} />
        ) : (
          <Table<GroupSummary>
            rowKey="id"
            size="middle"
            dataSource={shown}
            scroll={{ x: 640 }}
            pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    needle ? `No group matches “${filter.trim()}”.` : "No groups yet. Create one to give several people the same access."
                  }
                >
                  {!needle && (
                    <Button type="primary" onClick={() => setCreating(true)}>
                      Create the first group
                    </Button>
                  )}
                </Empty>
              ),
            }}
            columns={[
              {
                title: "Group",
                key: "group",
                render: (_, g) => (
                  <Space align="center">
                    <PersonAvatar subject={g.subject} display={g.name} />
                    <div className="person-cell">
                      <a href={groupHref(g.id)}>{g.name}</a>
                      {g.description && (
                        <div>
                          <Text type="secondary">{g.description}</Text>
                        </div>
                      )}
                    </div>
                  </Space>
                ),
              },
              {
                title: "Members",
                key: "members",
                width: 120,
                render: (_, g) => (
                  <Text>
                    {g.member_count} {g.member_count === 1 ? "person" : "people"}
                  </Text>
                ),
              },
              {
                title: "Access",
                key: "access",
                width: 170,
                render: (_, g) =>
                  g.grant_count ? (
                    <Text>
                      {g.grant_count} {g.grant_count === 1 ? "grant" : "grants"}
                    </Text>
                  ) : (
                    <Text type="secondary">No access yet</Text>
                  ),
              },
              {
                title: "",
                key: "actions",
                width: 110,
                align: "right",
                render: (_, g) => (
                  <Button href={groupHref(g.id)} aria-label={`Open ${g.name}`}>
                    Open
                  </Button>
                ),
              },
            ]}
          />
        )}
      </div>

      <NewGroupModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(group) => {
          setCreating(false);
          list.reload();
          navigate(`/groups/${encodeURIComponent(group.id)}`);
        }}
      />
      <GroupDrawer id={selected} hubs={hubs} onChanged={list.reload} />
    </>
  );
}

function NewGroupModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (group: GroupDetail) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [emails, setEmails] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const parsed = parseEmails(emails);
  const problems = { name: nameProblem(name), emails: emailsProblem(parsed) };

  function reset() {
    setName("");
    setDescription("");
    setEmails("");
    setTouched(false);
    setFailure("");
  }

  async function create() {
    setTouched(true);
    if (problems.name || problems.emails) return;
    setBusy(true);
    setFailure("");
    try {
      const { group } = await groupsApi.create({
        name: name.trim(),
        description: description.trim() || null,
        emails: parsed,
      });
      reset();
      onCreated(group);
    } catch (e) {
      setFailure(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="New group"
      open={open}
      onCancel={() => {
        if (busy) return;
        reset();
        onClose();
      }}
      okText="Create group"
      onOk={() => void create()}
      confirmLoading={busy}
      destroyOnHidden
    >
      <div className="drawer-body">
        {failure && <Alert type="error" showIcon title={failure} />}
        <div className="field">
          <label className="field-label" htmlFor="group-name">
            Name
          </label>
          <Input
            id="group-name"
            autoFocus
            autoComplete="off"
            value={name}
            maxLength={100}
            status={touched && problems.name ? "error" : undefined}
            onChange={(e) => setName(e.target.value)}
          />
          <Text type={touched && problems.name ? "danger" : "secondary"} className="field-help">
            {touched && problems.name ? problems.name : "For example Finance team or Night shift."}
          </Text>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="group-description">
            Description <Text type="secondary">(optional)</Text>
          </label>
          <Input.TextArea
            id="group-description"
            value={description}
            maxLength={500}
            autoSize={{ minRows: 2, maxRows: 4 }}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="group-emails">
            Members <Text type="secondary">(optional)</Text>
          </label>
          <Input.TextArea
            id="group-emails"
            value={emails}
            placeholder="name@example.com, other@example.com"
            autoSize={{ minRows: 2, maxRows: 6 }}
            status={touched && problems.emails ? "error" : undefined}
            onChange={(e) => setEmails(e.target.value)}
          />
          <Text type={touched && problems.emails ? "danger" : "secondary"} className="field-help">
            {touched && problems.emails
              ? problems.emails
              : "Email addresses, separated by commas or new lines. They don’t need an account yet."}
          </Text>
        </div>
      </div>
    </Modal>
  );
}

/**
 * One group: its details, members and access. Every change is saved at once
 * and recorded in Activity; the server re-checks each one.
 */
function GroupDrawer({ id, hubs, onChanged }: { id?: string; hubs: Hub[]; onChanged: () => void }) {
  const { modal, message } = App.useApp();
  const group = useGroup(id);
  const g = group.data;
  const catalogs = useCatalogs(g ? g.access.map((a) => a.hub) : []);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [adding, setAdding] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "add" | "delete" | string>("");
  const [failure, setFailure] = useState("");
  const parsed = parseEmails(adding);
  const addProblem = touched ? emailsProblem(parsed) : null;
  const agentName = (key: string) => hubs.find((h) => h.key === key)?.name;
  const open = !!id;

  function close() {
    if (busy) return;
    setEditing(false);
    setAdding("");
    setTouched(false);
    setFailure("");
    navigate("/groups");
  }

  const refresh = () => {
    group.reload();
    onChanged();
  };

  async function run(kind: string, action: () => Promise<unknown>, done?: string) {
    setBusy(kind);
    setFailure("");
    try {
      await action();
      if (done) message.success(done);
      refresh();
      return true;
    } catch (e) {
      setFailure(errorText(e));
      if (e instanceof ApiError && e.status === 404) refresh();
      return false;
    } finally {
      setBusy("");
    }
  }

  async function saveDetails() {
    if (!g || nameProblem(name)) return;
    const ok = await run("save", () =>
      groupsApi.update(g.id, { name: name.trim(), description: description.trim() || null }), "Group updated");
    if (ok) setEditing(false);
  }

  async function addMembers() {
    setTouched(true);
    if (!g || !parsed.length || emailsProblem(parsed)) return;
    const ok = await run("add", async () => {
      const r = await groupsApi.addMembers(g.id, parsed);
      message.success(
        r.added.length === 0
          ? "They were already in the group."
          : `Added ${r.added.length} ${r.added.length === 1 ? "person" : "people"}.`,
      );
    });
    if (ok) {
      setAdding("");
      setTouched(false);
    }
  }

  function askRemove(m: GroupMember) {
    if (!g) return;
    const who = personName(m.email, m.display);
    modal.confirm({
      title: `Remove ${who} from ${g.name}?`,
      content: g.access.length
        ? "They lose the access this group gives. Access they hold directly or through other groups stays."
        : "This group gives no access yet, so nothing else changes.",
      okText: "Remove from group",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: () => run(`member:${m.email}`, () => groupsApi.removeMember(g.id, m.email), `${who} was removed`),
    });
  }

  function askDelete() {
    if (!g) return;
    const grants = g.access.reduce((n, a) => n + a.permissions.length, 0);
    modal.confirm({
      title: `Delete the group ${g.name}?`,
      content:
        grants > 0
          ? `Its ${g.member_count === 1 ? "member loses" : `${g.member_count} members lose`} the access it gives in ${g.access.length === 1 ? "1 agent" : `${g.access.length} agents`}. Access they hold directly or through other groups stays. This can’t be undone.`
          : "It gives no access, so nobody loses anything. This can’t be undone.",
      okText: "Delete group",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: async () => {
        const ok = await run("delete", () => groupsApi.remove(g.id), `${g.name} was deleted`);
        if (ok) navigate("/groups");
      },
    });
  }

  const members = g?.members ?? [];
  const title = g ? g.name : "Group";
  const accessRows = useMemo(() => g?.access ?? [], [g]);

  return (
    <Drawer
      title={title}
      aria-label={title}
      open={open}
      onClose={close}
      size={560}
      closable={!busy}
      mask={{ closable: !busy }}
      destroyOnHidden
      footer={
        g && (
          <Space className="drawer-actions">
            <Button danger icon={<Trash2 size={16} />} loading={busy === "delete"} onClick={askDelete}>
              Delete group
            </Button>
          </Space>
        )
      }
    >
      {!g ? (
        group.status === 404 ? (
          <Empty description="This group doesn’t exist any more. It may have been deleted." />
        ) : (
          <LoadState error={group.error} retry={group.reload} />
        )
      ) : (
        <div className="drawer-body">
          {failure && <Alert type="error" showIcon title={failure} closable={{ onClose: () => setFailure("") }} />}

          <div className="section group-section">
            {editing ? (
              <>
                <div className="field">
                  <label className="field-label" htmlFor="edit-group-name">
                    Name
                  </label>
                  <Input
                    id="edit-group-name"
                    value={name}
                    maxLength={100}
                    status={nameProblem(name) ? "error" : undefined}
                    onChange={(e) => setName(e.target.value)}
                  />
                  {nameProblem(name) && (
                    <Text type="danger" className="field-help">
                      {nameProblem(name)}
                    </Text>
                  )}
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="edit-group-description">
                    Description
                  </label>
                  <Input.TextArea
                    id="edit-group-description"
                    value={description}
                    maxLength={500}
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    onChange={(e) => setDescription(e.target.value)}
                  />
                </div>
                <Space>
                  <Button onClick={() => setEditing(false)} disabled={busy === "save"}>
                    Cancel
                  </Button>
                  <Button type="primary" loading={busy === "save"} disabled={!!nameProblem(name)}
                          onClick={() => void saveDetails()}>
                    Save
                  </Button>
                </Space>
              </>
            ) : (
              <div className="agent-access-heading">
                <div>
                  <Text type={g.description ? undefined : "secondary"}>{g.description || "No description."}</Text>
                  <div>
                    <Text type="secondary">
                      Created <When value={g.created_at} />
                      {g.created_by ? ` by ${g.created_by}` : ""}
                      {g.source === "migrated" ? " · moved from Open WebUI" : ""}
                    </Text>
                  </div>
                </div>
                <Button
                  icon={<Pencil size={16} />}
                  aria-label={`Rename or describe ${g.name}`}
                  onClick={() => {
                    setName(g.name);
                    setDescription(g.description);
                    setEditing(true);
                  }}
                >
                  Edit
                </Button>
              </div>
            )}
          </div>

          <div className="section group-section">
            <Title level={2} style={{ fontSize: 16, margin: 0 }}>Access</Title>
            {accessRows.length === 0 ? (
              <Text type="secondary">
                This group gives no access yet. Open an agent’s Access tab and choose Add group.
              </Text>
            ) : (
              accessRows.map((a) => {
                const known = agentName(a.hub);
                return (
                  <div key={a.hub} className="group-access">
                    <div className="agent-access-heading">
                      <Text strong>{known ?? a.hub_name}</Text>
                      {known && (
                        <a href={hrefWith(`/agents/${encodeURIComponent(a.hub)}/access`, { edit: g.subject })}>
                          Change access
                        </a>
                      )}
                    </div>
                    <Space wrap size={[6, 6]}>
                      {orderCapabilities(a.permissions, []).map((p) => (
                        <CapabilityTag key={p} permission={p} catalog={catalogs[a.hub]} />
                      ))}
                      {!a.permissions.includes(USE_HUB) && <Text type="secondary">No entry to this agent</Text>}
                    </Space>
                  </div>
                );
              })
            )}
          </div>

          <div className="section group-section">
            <Title level={2} style={{ fontSize: 16, margin: 0 }}>
              Members · {members.length}
            </Title>
            <div className="field">
              <label className="field-label" htmlFor="add-members">
                Add people
              </label>
              <Space.Compact style={{ width: "100%" }}>
                <Input
                  id="add-members"
                  value={adding}
                  placeholder="name@example.com, other@example.com"
                  status={addProblem ? "error" : undefined}
                  onChange={(e) => setAdding(e.target.value)}
                  onPressEnter={() => void addMembers()}
                />
                <Button
                  type="primary"
                  icon={<UserPlus size={16} />}
                  loading={busy === "add"}
                  disabled={!parsed.length}
                  onClick={() => void addMembers()}
                >
                  Add
                </Button>
              </Space.Compact>
              <Text type={addProblem ? "danger" : "secondary"} className="field-help">
                {addProblem ?? "Email addresses. They get the group’s access once they sign in with that email."}
              </Text>
            </div>
            <Table<GroupMember>
              rowKey="email"
              size="small"
              dataSource={members}
              pagination={{ pageSize: 25, hideOnSinglePage: true, showSizeChanger: false }}
              locale={{ emptyText: <Empty description="No one is in this group yet." /> }}
              columns={[
                {
                  title: "Person",
                  key: "person",
                  render: (_, m) => <PersonCell subject={m.email} display={m.display} />,
                },
                {
                  title: "Account",
                  key: "account",
                  width: 150,
                  render: (_, m) => {
                    const status = memberStatus(m);
                    return status ? <AccountTag status={status} /> : <Tag>Unknown</Tag>;
                  },
                },
                {
                  title: "",
                  key: "actions",
                  width: 100,
                  align: "right",
                  render: (_, m) => (
                    <Button
                      size="small"
                      danger
                      loading={busy === `member:${m.email}`}
                      onClick={() => askRemove(m)}
                      aria-label={`Remove ${personName(m.email, m.display)} from ${g.name}`}
                    >
                      Remove
                    </Button>
                  ),
                },
              ]}
            />
          </div>
        </div>
      )}
    </Drawer>
  );
}
