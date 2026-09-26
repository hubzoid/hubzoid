import { useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Drawer,
  Input,
  Radio,
  Space,
  Tag,
  Typography,
  Tooltip,
} from "antd";
import { Circle, CircleHelp, XCircle } from "lucide-react";
import { ApiError, request, query, type Access, type Hub } from "../../api";
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

function CapabilityHelp({ label, text }: { label: string; text: string }) {
  return <Tooltip title={text} trigger={["hover", "focus", "click"]}>
    <button type="button" className="capability-info" aria-label={`About ${label}`}>
      <CircleHelp size={16} aria-hidden="true" />
    </button>
  </Tooltip>;
}

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
  const [identityType, setIdentityType] = useState<"person" | "service">("person");
  const [touched, setTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  const catalog = useMemo(() => toCatalog(access.permissions), [access.permissions]);
  const catalogPerms = useMemo(
    () => new Set(access.permissions.map((p) => p.permission)),
    [access.permissions],
  );
  // Grants for tools that no longer exist in the agent's catalogue. They can't
  // be re-added, but must be individually removable — otherwise a renamed/
  // deleted tool leaves a permission the editor can never clear.
  const orphans = draft ? draft.row.perms.filter((p) => !catalogPerms.has(p)) : [];
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
    setDraft({ ...draft, step: "saving", progress: 0 });
    try {
      // One atomic request: the whole change set applies on the revision we
      // loaded, or none of it does (a concurrent edit returns 409). No partial
      // saves, so there is nothing to reconcile by hand.
      await request("/access/apply", {
        subject,
        hub: target,
        expected_revision: access.revision,
        operations: operations.map((o) => ({ action: o.action, permission: o.permission })),
      });
      setDraft(null);
      onSaved(subject, operations);
    } catch (e) {
      // A 4xx client rejection committed nothing; a 5xx or network drop is
      // uncertain — the atomic write may have landed just before the response
      // was lost. Say so honestly and reload to show the real current state.
      const uncertain = e instanceof ApiError ? !e.certain : true;
      setDraft({ ...draft, step: "failed", progress: 0, failure: errorText(e), uncertain });
      onReload();
    }
  }

  const name = draft ? personName(subject || draft.subject, draft.row.display) : "";
  const title =
    draft?.step === "review"
      ? "Review changes"
      : draft?.step === "saving"
        ? "Saving…"
        : draft?.step === "failed"
          ? draft.uncertain
            ? "Save not confirmed"
            : "Nothing was saved"
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
            Saving…
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
      aria-label={title}
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
                  <Radio.Group aria-label="Identity type" value={identityType} onChange={(e) => { setIdentityType(e.target.value); setDraft({ ...draft, subject: e.target.value === "service" ? "workflow:" : "" }); }} options={[{label: "Person", value: "person"}, {label: "Service", value: "service"}]} />
                  <label className="field-label" htmlFor="access-subject">
                    {identityType === "person" ? "Email address" : "Service identity"}
                  </label>
                  <Input
                    id="access-subject"
                    autoFocus
                    placeholder={identityType === "person" ? "name@example.com" : "workflow:reporting"}
                    value={draft.subject}
                    status={touched && subjectError ? "error" : undefined}
                    onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                    onPressEnter={() => void review()}
                  />
                  <Text type={touched && subjectError ? "danger" : "secondary"} className="field-help">
                    {touched && subjectError
                      ? subjectError
                      : identityType === "service" ? "Use a stable workflow:name identity for a configured service. This does not create credentials." : "No invitation is sent. Share the chat URL and ask them to sign in with this exact email. To create their sign-in, use People → Add account."}
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

              {draft.row.suspended && (
                <Alert
                  type="warning"
                  showIcon
                  title="This person is blocked by an administrator"
                  description={
                    <>
                      They cannot be granted access until they are reactivated.{" "}
                      <a href={personHref(draft.row.subject)}>Open their details under People.</a>
                    </>
                  }
                />
              )}
              {draft.row.account_unavailable && !draft.row.suspended && (
                <Alert
                  type="warning"
                  showIcon
                  title="Their chat account is unavailable"
                  description="It was removed or not found. Access resumes automatically if the account reappears; it can’t be changed here."
                />
              )}

              <div className="section">
                <div className="capabilities-heading">
                  <Title level={2} style={{ fontSize: 16, margin: 0 }}>Capabilities</Title>
                  <CapabilityHelp label="capabilities" text="Choose what this person can do. Changes are saved only after you review and confirm." />
                </div>
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
                      <div className="capability capability-row" key={p.permission}>
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
                        {lock && <Text type="warning" className="capability-state">{lock.label ?? "Locked"}</Text>}
                        {!lock && publicOnly && <Text type="secondary" className="capability-state">Public</Text>}
                        {(p.description || lock || publicOnly) && <CapabilityHelp label={p.label} text={[
                          p.description,
                          lock?.reason,
                          !lock && publicOnly ? "Available to everyone signed in. A direct grant keeps access if public access is turned off." : undefined,
                        ].filter(Boolean).join(" ")} />}

                      </div>
                    );
                  })}
                  {orphans.map((p) => (
                    <div className="capability" key={p}>
                      <Checkbox
                        checked={draft.selected.includes(p)}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            selected: toggle(draft.selected, p, e.target.checked),
                          })
                        }
                      >
                        <span className="capability-title">
                          <Text strong>{p}</Text>
                          <Tag>No longer available</Tag>
                        </span>
                      </Checkbox>
                      <div className="capability-help">
                        <Text type="secondary">
                          This capability no longer exists in {hub.name}. You can remove it, but it can’t be granted again.
                        </Text>
                      </div>
                    </div>
                  ))}
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
  // Applied atomically, so every row shares the same state. A confirmed failure
  // is "failed" (nothing saved); an unconfirmed one is "unknown".
  const status = () =>
    draft.step === "failed"
      ? draft.uncertain
        ? "unknown"
        : "failed"
      : draft.step === "saving"
        ? "active"
        : "planned";
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
          type={draft.uncertain ? "warning" : "error"}
          showIcon
          title={
            draft.uncertain
              ? "Couldn’t confirm whether the changes were saved"
              : "No changes were saved"
          }
          description={
            <>
              {draft.failure}
              <br />
              {draft.uncertain
                ? `Reloading the current access. Reopen ${name} to see what actually applies now before trying again.`
                : `Nothing changed. Reloading the current access — reopen ${name} to try again.`}
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
        <Text strong>Changes</Text>
        <Paragraph type="secondary" style={{ margin: "4px 0 8px" }}>
          Applied together in one step. If it can’t be applied — for example someone else changed access first — nothing changes and you can review again.
        </Paragraph>
        <ol className="operation-list">
          {draft.operations.map((op) => {
            const s = status();
            return (
              <li key={`${op.action}:${op.permission}`} data-status={s}>
                {s === "failed" ? (
                  <XCircle size={16} className="op-failed" />
                ) : (
                  <Circle size={16} className="op-pending" />
                )}
                <span>
                  {op.action === "grant" ? "Allow" : "Remove"} {capabilityLabel(op.permission, catalog)}
                  {op.action === "revoke" && op.permission === USE_HUB && cascade ? " (and everything with it)" : ""}
                </span>
                {s === "failed" && <Tag color="red">Not saved</Tag>}
                {s === "unknown" && <Tag color="gold">Unknown</Tag>}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
