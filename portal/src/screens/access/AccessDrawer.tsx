import { useCallback, useId, useMemo, useState } from "react";
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
import { ApiError, request, query, type Access, type AccessRow, type Hub, type Permission } from "../../api";
import { errorText } from "../../hooks/useData";
import { personHref, useNavigationGuard } from "../../hooks/useRoute";
import { AccountTag, PersonAvatar } from "../../components/common";
import {
  EVERYONE,
  USE_HUB,
  capabilityLabel,
  capabilityNotes,
  groupCapabilities,
  isGrantable,
  isService,
  normalizeSubject,
  personName,
  toCatalog,
  validateSubject,
  type Catalog,
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
  type Lock,
} from "./plan";
import { CapabilityGroup, HelpText, HelpToggle, LegacyServiceTag } from "./AccessParts";

const { Text, Title } = Typography;

/** Groups that are always open: entry ("Use this agent") and removable leftovers. */
const ALWAYS_OPEN = new Set(["hub", "obsolete"]);
/** Shorter drawer titles where the shared catalogue title reads as jargon. */
const GROUP_TITLES: Record<string, string> = { restricted: "Restricted tools" };

const NO_NEW_SERVICES =
  "New service identities can’t be added. Workflows run as an ordinary account: enter that account’s email address.";

/** The email field for a new grantee. Workflows run as ordinary accounts, so a
 *  `workflow:` identity is accepted only when it already exists here (checked
 *  after the lookup), and the hint never suggests creating one. */
function validateNewSubject(raw: string): string | null {
  const value = normalizeSubject(raw);
  if (!value) return "Enter an email address.";
  const problem = validateSubject(value);
  if (isService(value)) return problem ? NO_NEW_SERVICES : null;
  if (!problem) return null;
  return value === EVERYONE ? problem : "Enter a valid email address.";
}

/**
 * Selected capabilities that can't be granted, checked before review. The
 * controls already prevent these; this catches an agent or account that changed
 * after the drawer opened, so a problem is never hidden in a closed group.
 */
function selectionProblems(
  row: AccessRow,
  selected: string[],
  access: Access,
  catalog: Catalog,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of diff(row.perms, selected).added) {
    const meta = catalog[p];
    if (p !== USE_HUB && !isGrantable(meta)) {
      out[p] =
        meta?.default === "included"
          ? "Comes with Use this agent, so it can’t be granted on its own. Clear it to continue."
          : "No longer available in this agent, so it can’t be granted. Clear it to continue.";
      continue;
    }
    const lock = lockFor(p, row, access, selected, meta);
    if (lock && lock.label !== "Required") out[p] = lock.reason;
  }
  return out;
}

/** A short configuration status next to the label: missing or not checked. */
function ConfigStatus({ p }: { p?: Permission }) {
  if (!p || p.obsolete || p.available === true || p.available === undefined) return null;
  return (
    <Text type={p.available === false ? "warning" : "secondary"} className="capability-status">
      {p.status || (p.available === false ? "Not configured" : "Not checked")}
    </Text>
  );
}

function CapabilityRow({
  p,
  checked,
  lock,
  publicOnly,
  problem,
  onChange,
}: {
  p: Permission;
  checked: boolean;
  lock: Lock;
  publicOnly: boolean;
  problem?: string;
  onChange: (on: boolean) => void;
}) {
  const [helpOpen, setHelpOpen] = useState(false);
  const helpId = useId();
  const problemId = useId();
  const quiet = lock?.label === "Included" || lock?.label === "No longer available";
  const help = [
    p.description,
    ...capabilityNotes(p),
    lock?.reason,
    !lock && publicOnly ? "Held through “Everyone signed in”. A direct grant keeps access if that is removed." : undefined,
  ].filter(Boolean).join(" ");
  return (
    <div className="capability" data-permission={p.permission}>
      <div className="capability-row">
        <Checkbox
          checked={checked}
          disabled={!!lock && !problem}
          aria-invalid={problem ? true : undefined}
          aria-describedby={problem ? problemId : undefined}
          onChange={(e) => onChange(e.target.checked)}
        >
          <span className="capability-title">
            <Text strong>{p.label}</Text>
            {p.sensitive && <Tag color="orange">Sensitive</Tag>}
            <ConfigStatus p={p} />
          </span>
        </Checkbox>
        {lock && (
          <Text type={quiet ? "secondary" : "warning"} className="capability-state">
            {lock.label ?? "Locked"}
          </Text>
        )}
        {!lock && publicOnly && <Text type="secondary" className="capability-state">Public</Text>}
        {help && (
          <HelpToggle label={p.label} open={helpOpen} controls={helpId} onToggle={() => setHelpOpen((o) => !o)} />
        )}
      </div>
      {problem && (
        <Text type="danger" id={problemId} className="capability-problem">
          {problem}
        </Text>
      )}
      {help && (
        <HelpText id={helpId} open={helpOpen}>
          {help}
        </HelpText>
      )}
    </div>
  );
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
  onSaved: () => void;
  onReload: () => void;
}) {
  const { modal } = App.useApp();
  const [touched, setTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  // Open groups and pre-review problems belong to one drawer session: they
  // survive Back from review, and reset when the drawer closes.
  const [expanded, setExpanded] = useState<string[]>([]);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [subjectProblem, setSubjectProblem] = useState<string | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const aboutId = useId();
  const catalog = useMemo(() => toCatalog(access.permissions), [access.permissions]);
  // Grouped for reading only; every row is the same control. Grants for tools
  // that no longer exist sit in their own group so they stay removable (never
  // grantable) — otherwise a renamed or deleted tool leaves a permission the
  // editor can never clear.
  const sections = useMemo(
    () => (draft ? groupCapabilities(access.permissions, draft.row.perms) : []),
    [access.permissions, draft],
  );
  const subject = draft ? normalizeSubject(draft.subject) : "";
  const subjectError = draft?.mode === "add" ? validateNewSubject(draft.subject) ?? subjectProblem : null;
  const changes = draft ? diff(draft.row.perms, draft.selected) : { added: [], removed: [] };
  const hasChanges = changes.added.length > 0 || changes.removed.length > 0;
  const dirty =
    !!draft &&
    draft.step !== "failed" &&
    (hasChanges || (draft.mode === "add" && draft.subject.trim() !== ""));
  const busy = draft?.step === "saving" || checking;

  /** Close the drawer and forget this session's view state. */
  const finish = useCallback(() => {
    setTouched(false);
    setExpanded([]);
    setProblems({});
    setSubjectProblem(null);
    setAboutOpen(false);
    setDraft(null);
  }, [setDraft]);

  const open = !!draft;
  const guard = useMemo(
    () => (open ? { dirty, busy, discard: finish } : null),
    [open, dirty, busy, finish],
  );
  useNavigationGuard(guard);

  function close() {
    if (!draft || busy) return;
    if (!dirty) {
      finish();
      return;
    }
    modal.confirm({
      title: "Discard unsaved changes?",
      content: "Nothing has been saved yet. Your edits will be lost.",
      okText: "Discard",
      okButtonProps: { danger: true },
      cancelText: "Keep editing",
      onOk: finish,
    });
  }

  /** Show problems in place, opening every group that holds one. */
  function flag(found: Record<string, string>) {
    setProblems(found);
    const keys = sections
      .filter((g) => g.items.some((p) => found[p.permission]))
      .map((g) => g.key);
    setExpanded((open) => [...new Set([...open, ...keys])]);
  }

  async function review() {
    if (!draft) return;
    let row = draft.row;
    let found: Record<string, string>;
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
          setProblems({});
          setDraft({
            ...draftFor(existing),
            notice: `${personName(existing.subject, existing.display)} already has access to ${hub.name}. Their current access is shown; adjust it and review again.`,
          });
          return;
        }
        // Workflows run as ordinary accounts; only an existing legacy record stays editable.
        if (isService(subject)) {
          setSubjectProblem(NO_NEW_SERVICES);
          return;
        }
        row = emptyRow(subject);
        found = selectionProblems(row, draft.selected, current, toCatalog(current.permissions));
      } catch (e) {
        setDraft({ ...draft, failure: `Couldn’t check current access: ${errorText(e)}` });
        return;
      } finally {
        setChecking(false);
      }
    } else {
      found = selectionProblems(row, draft.selected, access, catalog);
    }
    if (Object.keys(found).length) {
      flag(found);
      return;
    }
    setProblems({});
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
      finish();
      onSaved();
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
          <Button type="primary" onClick={finish}>
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
    if (draft.step === "review") {
      const removesEntry = changes.removed.includes(USE_HUB);
      const lines = changes.added.length + changes.removed.length;
      return (
        <Space className="drawer-actions" wrap>
          <Button onClick={close}>Cancel</Button>
          <Button onClick={() => setDraft({ ...draft, step: "edit" })}>Back</Button>
          <Button type="primary" danger={removesEntry} onClick={() => void save()}>
            {removesEntry ? "Remove access" : lines === 1 ? "Save change" : `Save ${lines} changes`}
          </Button>
        </Space>
      );
    }
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

  const problemCount = Object.keys(problems).length;

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
                  <label className="field-label" htmlFor="access-subject">
                    Email address
                  </label>
                  <Input
                    id="access-subject"
                    autoFocus
                    placeholder="name@example.com"
                    value={draft.subject}
                    status={touched && subjectError ? "error" : undefined}
                    onChange={(e) => {
                      setSubjectProblem(null);
                      setDraft({ ...draft, subject: e.target.value });
                    }}
                    onPressEnter={() => void review()}
                  />
                  <Text type={touched && subjectError ? "danger" : "secondary"} className="field-help">
                    {touched && subjectError
                      ? subjectError
                      : "No invitation is sent. Share the chat URL and ask them to sign in with this exact email. To create their sign-in, use People → Add account."}
                  </Text>
                </div>
              ) : (
                <Identity row={draft.row} name={name} />
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
              {problemCount > 0 && (
                <Alert
                  type="error"
                  showIcon
                  title={
                    problemCount === 1
                      ? "One selected capability can’t be granted. It is marked below."
                      : `${problemCount} selected capabilities can’t be granted. They are marked below.`
                  }
                />
              )}

              <div className="section">
                <div className="capabilities-heading">
                  <Title level={2} style={{ fontSize: 16, margin: 0 }}>Capabilities</Title>
                  <HelpToggle label="capabilities" open={aboutOpen} controls={aboutId} onToggle={() => setAboutOpen((o) => !o)} />
                </div>
                <HelpText id={aboutId} open={aboutOpen}>
                  Choose what this person can do. Changes are saved only after you review and confirm.
                </HelpText>
                <div className="capabilities" role="group" aria-label="Capabilities">
                  {sections.map((g) => {
                    // An included capability follows Use this agent, however it is held.
                    const entry =
                      draft.selected.includes(USE_HUB) ||
                      draft.row.inherited.includes(USE_HUB) ||
                      access.public;
                    const isChecked = (p: Permission) =>
                      p.default === "included"
                        ? entry
                        : draft.selected.includes(p.permission) || draft.row.inherited.includes(p.permission);
                    const collapsible = !ALWAYS_OPEN.has(g.key);
                    return (
                      <CapabilityGroup
                        key={g.key}
                        id={g.key}
                        title={GROUP_TITLES[g.key] ?? g.title}
                        collapsible={collapsible}
                        open={expanded.includes(g.key)}
                        onToggle={() =>
                          setExpanded((open) =>
                            open.includes(g.key) ? open.filter((k) => k !== g.key) : [...open, g.key],
                          )
                        }
                        // Held or chosen here; an included one comes with entry and isn't a choice.
                        selected={g.items.filter((p) => p.default !== "included" && isChecked(p)).length}
                        // Configuration is separate from permission: held but can't run yet.
                        unconfigured={g.items.filter((p) => !p.obsolete && p.available === false && isChecked(p)).length}
                        problems={g.items.filter((p) => problems[p.permission]).length}
                      >
                        {g.items.map((p) => {
                          const inherited = draft.row.inherited.includes(p.permission);
                          return (
                            <CapabilityRow
                              key={p.permission}
                              p={p}
                              checked={isChecked(p)}
                              lock={lockFor(p.permission, draft.row, access, draft.selected, p)}
                              publicOnly={
                                p.permission === USE_HUB &&
                                access.public &&
                                !draft.selected.includes(USE_HUB) &&
                                !inherited
                              }
                              problem={problems[p.permission]}
                              onChange={(on) => {
                                if (problems[p.permission]) {
                                  const rest = { ...problems };
                                  delete rest[p.permission];
                                  setProblems(rest);
                                }
                                setDraft({ ...draft, selected: toggle(draft.selected, p.permission, on) });
                              }}
                            />
                          );
                        })}
                      </CapabilityGroup>
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
              keepsAccess={draft.row.inherited.length > 0 || access.public}
            />
          )}
        </div>
      )}
    </Drawer>
  );
}

/** Who is being edited: name, identity and account state. */
function Identity({ row, name }: { row: AccessRow; name: string }) {
  const service = isService(row.subject);
  const [open, setOpen] = useState(false);
  const helpId = useId();
  return (
    <div className="person-heading">
      <Space align="center">
        <PersonAvatar subject={row.subject} display={row.display} size={40} />
        <div>
          <Text strong>{name}</Text>
          <div className="identity-tags">
            {name !== row.subject && (
              <Text type="secondary" className="identity">
                {row.subject}{" "}
              </Text>
            )}
            {service && <LegacyServiceTag />}
            {(!service || row.status !== "service") && <AccountTag status={row.status} />}
            {service && (
              <HelpToggle label="legacy service identities" open={open} controls={helpId} onToggle={() => setOpen((o) => !o)} />
            )}
          </div>
        </div>
      </Space>
      {service && (
        <HelpText id={helpId} open={open}>
          Created before workflows ran as user accounts. Its access is kept and can be changed or removed
          here. Workflows now run as an ordinary account, so give that account access instead.
        </HelpText>
      )}
    </div>
  );
}

function ReviewList({
  draft,
  name,
  hubName,
  catalog,
  keepsAccess,
}: {
  draft: Draft;
  name: string;
  hubName: string;
  catalog: Catalog;
  /** Still holds entry some other way (inherited, or everyone signed in). */
  keepsAccess: boolean;
}) {
  const { added, removed } = diff(draft.row.perms, draft.selected);
  const sensitive = added.filter((p) => catalog[p]?.sensitive);
  // Applied atomically, so every line shares one state. A confirmed failure is
  // "failed" (nothing saved); an unconfirmed one is "unknown".
  const status =
    draft.step === "failed"
      ? draft.uncertain
        ? "unknown"
        : "failed"
      : draft.step === "saving"
        ? "active"
        : "planned";
  return (
    <div className="access-review" data-status={status}>
      <div className="review-who">
        <PersonAvatar subject={draft.subject} display={draft.row.display} size={40} />
        <div className="review-who-text">
          <Text strong>{name}</Text>
          {name !== draft.subject && (
            <Text type="secondary" className="identity">
              {draft.subject}
            </Text>
          )}
          {isService(draft.subject) && (
            <span>
              <LegacyServiceTag />
            </span>
          )}
        </div>
      </div>
      <div className="review-agent">
        <Text type="secondary">Agent</Text>
        <Text strong>{hubName}</Text>
      </div>

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
                : `Reloading the current access. Reopen ${name} to try again.`}
            </>
          }
        />
      )}

      {added.length > 0 && (
        <div className="review-group">
          <Text strong id="review-adding">Adding</Text>
          <ul className="review-list" aria-labelledby="review-adding">
            {added.map((p) => (
              <li key={p} data-status={status}>
                {capabilityLabel(p, catalog)}
                {catalog[p]?.sensitive && <Tag color="orange">Sensitive</Tag>}
                {catalog[p]?.available === false && (
                  <Text type="warning" className="capability-status">
                    {" "}— can’t run until configured ({catalog[p].status || "Not configured"})
                  </Text>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {removed.length > 0 && (
        <div className="review-group">
          <Text strong id="review-removing">Removing</Text>
          <ul className="review-list" aria-labelledby="review-removing">
            {removed.map((p) => (
              <li key={p} data-status={status}>
                {capabilityLabel(p, catalog)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {removed.includes(USE_HUB) && (
        <Alert
          type="warning"
          showIcon
          title={
            keepsAccess
              ? `${name}’s direct access to ${hubName} will be removed`
              : `${name} will lose access to ${hubName}`
          }
          description={
            keepsAccess
              ? "Access held through organization administrator rights or “Everyone signed in” still applies."
              : undefined
          }
        />
      )}
      {sensitive.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="This grants a sensitive capability"
          description={`${sensitive.map((p) => capabilityLabel(p, catalog)).join(", ")}. Make sure ${name} should have this.`}
        />
      )}
    </div>
  );
}
