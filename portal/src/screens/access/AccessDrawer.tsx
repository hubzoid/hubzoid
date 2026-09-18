import { useMemo, useState } from "react";
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
import { CheckCircle2, Circle, XCircle } from "lucide-react";
import { request, query, type Access, type Hub } from "../../api";
import { errorText } from "../../hooks/useData";
import { personHref, useNavigationGuard } from "../../hooks/useRoute";
import { AccountTag, PersonAvatar } from "../../components/common";
import {
  USE_HUB,
  capabilityLabel,
  normalizeSubject,
  personName,
  toCatalog,
  validateSubject,
} from "../../lib/format";
import {
  canRemoveAll,
  diff,
  draftFor,
  emptyRow,
  lockFor,
  planOperations,
  toggle,
  type Draft,
  type Operation,
} from "./plan";

const { Text, Title, Paragraph } = Typography;

export function AccessDrawer({
  hub,
  access,
  draft,
  setDraft,
  onSaved,
  onReload,
}: {
  hub: Hub;
  access: Access;
  draft: Draft | null;
  setDraft: (d: Draft | null) => void;
  onSaved: (subject: string, operations: Operation[]) => void;
  onReload: () => void;
}) {
  const { modal } = App.useApp();
  const [touched, setTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  const catalog = useMemo(() => toCatalog(access.permissions), [access.permissions]);
  const subject = draft ? normalizeSubject(draft.subject) : "";
  const subjectError = draft?.mode === "add" ? validateSubject(draft.subject) : null;
  const changes = draft ? diff(draft.row.perms, draft.selected) : { added: [], removed: [] };
  const hasChanges = changes.added.length > 0 || changes.removed.length > 0;
  const dirty =
    !!draft &&
    draft.step !== "failed" &&
    (hasChanges || (draft.mode === "add" && draft.subject.trim() !== ""));
  const busy = draft?.step === "saving" || checking;

  const open = !!draft;
  const guard = useMemo(
    () => (open ? { dirty, busy, discard: () => setDraft(null) } : null),
    [open, dirty, busy, setDraft],
  );
  useNavigationGuard(guard);

  function close() {
    if (!draft || busy) return;
    if (!dirty) {
      setDraft(null);
      return;
    }
    modal.confirm({
      title: "Discard unsaved changes?",
      content: "Nothing has been saved yet. Your edits will be lost.",
      okText: "Discard",
      okButtonProps: { danger: true },
      cancelText: "Keep editing",
      onOk: () => setDraft(null),
    });
  }

  async function review() {
    if (!draft) return;
    let row = draft.row;
    if (draft.mode === "add") {
      setTouched(true);
      if (subjectError) return;
      // Look the identity up before promising a change set, so an existing
      // grantee is edited instead of blindly re-granted.
      setChecking(true);
      try {
        const current = await request<Access>(
          "/access" + query({ hub: access.hub, q: subject, limit: 200 }),
        );
        const existing = current.rows.find((r) => r.subject === subject);
        if (existing) {
          setDraft({
            ...draftFor(existing),
            notice: `${personName(existing.subject, existing.display)} already has access to ${hub.name}. Their current access is shown; adjust it and review again.`,
          });
          return;
        }
        row = emptyRow(subject);
      } catch (e) {
        setDraft({ ...draft, failure: `Couldn’t check current access: ${errorText(e)}` });
        return;
      } finally {
        setChecking(false);
      }
    }
    setDraft({
      ...draft,
      row,
      subject,
      step: "review",
      failure: undefined,
      notice: undefined,
      operations: planOperations(row.perms, draft.selected),
      progress: 0,
    });
  }

  async function save() {
    if (!draft || draft.step !== "review" || busy) return;
    const { operations } = draft;
    const target = access.hub; // the loaded response's agent, never the route
    let progress = 0;
    setDraft({ ...draft, step: "saving", progress });
    for (const op of operations) {
      try {
        await request("/access/" + op.action, {
          subject,
          hub: target,
          permission: op.permission,
        });
        progress += 1;
        setDraft({ ...draft, step: "saving", progress });
      } catch (e) {
        setDraft({ ...draft, step: "failed", progress, failure: errorText(e) });
        onReload();
        return;
      }
    }
    setDraft(null);
    onSaved(subject, operations);
  }

  const name = draft ? personName(subject || draft.subject, draft.row.display) : "";
  const title =
    draft?.step === "review"
      ? "Review changes"
      : draft?.step === "saving"
        ? "Saving…"
        : draft?.step === "failed"
          ? "Some changes were not saved"
          : draft?.mode === "add"
            ? "Add a person"
            : "Edit access";

  const footer = (() => {
    if (!draft) return null;
    if (draft.step === "failed")
      return (
        <Space className="drawer-actions">
          <Button type="primary" onClick={() => setDraft(null)}>
            Done
          </Button>
        </Space>
      );
    if (draft.step === "saving")
      return (
        <Space className="drawer-actions">
          <Button type="primary" loading>
            Saving {draft.progress + 1} of {draft.operations.length}…
          </Button>
        </Space>
      );
    if (draft.step === "review")
      return (
        <Space className="drawer-actions">
          <Button onClick={() => setDraft({ ...draft, step: "edit" })}>Back</Button>
          <Button type="primary" onClick={() => void save()}>
            Save {draft.operations.length === 1 ? "change" : `${draft.operations.length} changes`}
          </Button>
        </Space>
      );
    return (
      <Space className="drawer-actions" wrap>
        <Button onClick={close}>Cancel</Button>
        {draft.mode === "edit" && canRemoveAll(draft.row, access) && (
          <Button
            danger
            onClick={() =>
              setDraft({
                ...draft,
                selected: [],
                step: "review",
                operations: planOperations(draft.row.perms, []),
              })
            }
          >
            Remove all access
          </Button>
        )}
        <Button
          type="primary"
          loading={checking}
          disabled={!hasChanges || (draft.mode === "add" && touched && !!subjectError)}
          onClick={() => void review()}
        >
          Review changes
        </Button>
      </Space>
    );
  })();

  return (
    <Drawer
      title={title}
      open={!!draft}
      onClose={close}
      size={520}
      closable={!busy}
      mask={{ closable: !busy }}
      keyboard={!busy}
      extra={<Text type="secondary">{hub.name}</Text>}
      footer={footer}
      destroyOnHidden
    >
      {draft && (
        <div className="drawer-body">
          {draft.failure && draft.step !== "failed" && (
            <Alert type="error" showIcon title={draft.failure} />
          )}
          {draft.notice && <Alert type="info" showIcon title={draft.notice} />}

          {draft.step === "edit" && (
            <>
              {draft.mode === "add" ? (
                <div className="field">
                  <label className="field-label" htmlFor="access-subject">
                    Email address or service identity
                  </label>
                  <Input
                    id="access-subject"
                    autoFocus
                    placeholder="name@example.com"
                    value={draft.subject}
                    status={touched && subjectError ? "error" : undefined}
                    onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                    onPressEnter={() => void review()}
                  />
                  <Text type={touched && subjectError ? "danger" : "secondary"} className="field-help">
                    {touched && subjectError
                      ? subjectError
                      : "Grants access to this identity; it does not create an account or send an invitation. The person signs up in the chat app with this email."}
                  </Text>
                </div>
              ) : (
                <Space align="center" className="person-heading">
                  <PersonAvatar subject={draft.row.subject} display={draft.row.display} size={40} />
                  <div>
                    <Text strong>{name}</Text>
                    <div>
                      {name !== draft.row.subject && (
                        <Text type="secondary" className="identity">
                          {draft.row.subject}{" "}
                        </Text>
                      )}
                      <AccountTag status={draft.row.status} />
                    </div>
                  </div>
                </Space>
              )}

              {draft.row.status === "blocked" && (
                <Alert
                  type="warning"
                  showIcon
                  title="This person is blocked"
                  description={
                    <>
                      They cannot be granted access until they are reactivated.{" "}
                      <a href={personHref(draft.row.subject)}>Open their details under People.</a>
                    </>
                  }
                />
              )}

              <div className="section">
                <Title level={5}>Capabilities</Title>
                <Paragraph type="secondary">
                  Choose what {draft.mode === "add" ? "this person" : name} can do in {hub.name}. Nothing is saved until you review and confirm.
                </Paragraph>
                <div className="capabilities" role="group" aria-label="Capabilities">
                  {access.permissions.map((p) => {
                    const lock = lockFor(p.permission, draft.row, access, draft.selected);
                    const inherited = draft.row.inherited.includes(p.permission);
                    const checked = draft.selected.includes(p.permission) || inherited;
                    const publicOnly =
                      p.permission === USE_HUB &&
                      access.public &&
                      !draft.selected.includes(USE_HUB) &&
                      !inherited;
                    return (
                      <div className="capability" key={p.permission}>
                        <Checkbox
                          checked={checked}
                          disabled={!!lock}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              selected: toggle(draft.selected, p.permission, e.target.checked),
                            })
                          }
                        >
                          <span className="capability-title">
                            <Text strong>{p.label}</Text>
                            {p.sensitive && <Tag color="orange">Sensitive</Tag>}
                          </span>
                        </Checkbox>
                        <div className="capability-help">
                          {p.description && <Text type="secondary">{p.description} </Text>}
                          {lock && <Text type="warning">{lock.reason}</Text>}
                          {!lock && publicOnly && (
                            <Text type="secondary">
                              Already available to everyone signed in. A direct grant keeps their access if public access is turned off later.
                            </Text>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </>
          )}

          {(draft.step === "review" || draft.step === "saving" || draft.step === "failed") && (
            <ReviewList
              draft={draft}
              name={name}
              hubName={hub.name}
              catalog={catalog}
            />
          )}
        </div>
      )}
    </Drawer>
  );
}

function ReviewList({
  draft,
  name,
  hubName,
  catalog,
}: {
  draft: Draft;
  name: string;
  hubName: string;
  catalog: ReturnType<typeof toCatalog>;
}) {
  const { added, removed } = diff(draft.row.perms, draft.selected);
  const impliedEntry = added.includes(USE_HUB) && added.length > 1;
  const cascade = removed.includes(USE_HUB) && removed.length > 1;
  const sensitive = added.filter((p) => catalog[p]?.sensitive);
  const status = (index: number) => {
    if (draft.step === "review") return "planned";
    if (index < draft.progress) return "done";
    if (draft.step === "failed" && index === draft.progress) return "failed";
    if (draft.step === "saving" && index === draft.progress) return "active";
    return "pending";
  };
  return (
    <div className="review">
      <Space align="center" className="person-heading">
        <PersonAvatar subject={draft.subject} display={draft.row.display} size={40} />
        <div>
          <Text strong>{name}</Text>
          <div>
            <Text type="secondary">
              {name !== draft.subject ? `${draft.subject} · ` : ""}
              {hubName}
            </Text>
          </div>
        </div>
      </Space>

      {draft.step === "failed" && (
        <Alert
          type="error"
          showIcon
          title={`Saved ${draft.progress} of ${draft.operations.length} ${draft.operations.length === 1 ? "change" : "changes"}`}
          description={
            <>
              {draft.failure}
              <br />
              Current access has been reloaded. Reopen {name} to see what applies now and try the rest again.
            </>
          }
        />
      )}

      {added.length > 0 && (
        <div className="section">
          <Text strong>Allow</Text>
          <ul className="review-list">
            {added.map((p) => (
              <li key={p}>
                {capabilityLabel(p, catalog)}
                {p === USE_HUB && impliedEntry && (
                  <Text type="secondary"> — included automatically with any capability</Text>
                )}
                {catalog[p]?.sensitive && <Tag color="orange">Sensitive</Tag>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {removed.length > 0 && (
        <div className="section">
          <Text strong>Remove</Text>
          <ul className="review-list">
            {removed.map((p) => (
              <li key={p}>{capabilityLabel(p, catalog)}</li>
            ))}
          </ul>
        </div>
      )}
      {removed.includes(USE_HUB) && (
        <Alert
          type="warning"
          showIcon
          title={
            cascade
              ? `Removing “${capabilityLabel(USE_HUB, catalog)}” removes every capability in ${hubName}`
              : `${name} will no longer be able to open ${hubName}`
          }
          description={
            cascade
              ? "One request removes them all. Inherited or public access, if any, still applies."
              : "Inherited or public access, if any, still applies."
          }
        />
      )}
      {sensitive.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="This grants a sensitive capability"
          description={`${sensitive.map((p) => capabilityLabel(p, catalog)).join(", ")} — make sure ${name} should have this.`}
        />
      )}

      <div className="section">
        <Text strong>Requests</Text>
        <Paragraph type="secondary" style={{ margin: "4px 0 8px" }}>
          Each change is saved with its own request, in this order. If one fails, the earlier ones stay saved and you’ll see exactly which did.
        </Paragraph>
        <ol className="operation-list">
          {draft.operations.map((op, i) => {
            const s = status(i);
            return (
              <li key={`${op.action}:${op.permission}`} data-status={s}>
                {s === "done" ? (
                  <CheckCircle2 size={16} className="op-done" />
                ) : s === "failed" ? (
                  <XCircle size={16} className="op-failed" />
                ) : (
                  <Circle size={16} className="op-pending" />
                )}
                <span>
                  {op.action === "grant" ? "Allow" : "Remove"} {capabilityLabel(op.permission, catalog)}
                  {op.action === "revoke" && op.permission === USE_HUB && cascade ? " (and everything with it)" : ""}
                </span>
                {s === "failed" && <Tag color="red">Failed</Tag>}
                {s === "done" && <Tag color="green">Saved</Tag>}
                {s === "pending" && draft.step === "failed" && <Tag>Not attempted</Tag>}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
