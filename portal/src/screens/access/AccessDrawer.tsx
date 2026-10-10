import { useCallback, useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
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
import { Plus, Search } from "lucide-react";
import {
  ApiError,
  request,
  query,
  type Access,
  type AccessRow,
  type AccountCreated,
  type AccountGranted,
  type Hub,
  type Me,
  type Permission,
  type Person,
  type SignIn,
} from "../../api";
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
  splitSections,
  toCatalog,
  type Catalog,
} from "../../lib/format";
import {
  canRemoveAll,
  diff,
  draftFor,
  emptyRow,
  lockFor,
  pickDraft,
  planOperations,
  toggle,
  type Draft,
  type Lock,
} from "./plan";
import {
  CapabilityGroup,
  CapabilitySection,
  HelpText,
  HelpToggle,
  LegacyServiceTag,
} from "./AccessParts";
import { editDraft, loadAccessRow } from "./load";
import { NewUserSignIn, OneTimePassword, SignInDetails } from "../people/AccountDrawer";
import { asApiError, emailProblem, googleDomainProblem, partialDetail } from "../people/accountRules";
import { passwordProblem } from "../people/password";

const { Text, Title, Paragraph } = Typography;

/** Groups that are always open: entry ("Use this agent") and removable leftovers. */
const ALWAYS_OPEN = new Set(["hub", "obsolete"]);


/** What happened to a new account. The account system and the access store
 *  can't commit together, so a created account without its access is shown
 *  as exactly that, and every retry reuses the account. */
type Outcome =
  | { type: "created"; created: AccountCreated }
  | { type: "exists" | "partial" | "uncertain" | "failed"; error: ApiError };


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
    // Entry is added with others.
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
  me,
  draft,
  setDraft,
  onSaved,
  onReload,
}: {
  hub: Hub;
  access: Access;
  /** The viewer: whether they may create accounts, and how accounts sign in. */
  me?: Me;
  draft: Draft | null;
  setDraft: (d: Draft | null) => void;
  onSaved: (message?: string) => void;
  onReload: () => void;
}) {
  const { modal } = App.useApp();
  const [touched, setTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  // Open groups and pre-review problems belong to one drawer session: they
  // survive Back from review, and reset when the drawer closes.
  const [expanded, setExpanded] = useState<string[]>([]);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [aboutOpen, setAboutOpen] = useState(false);
  const [newName, setNewName] = useState("");
  // Optional: the number their WhatsApp and Telegram messages come from.
  const [newPhone, setNewPhone] = useState("");
  const [signIn, setSignIn] = useState<SignIn>("password");
  // Held only while this drawer is open; never stored anywhere else.
  const [password, setPassword] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [retryError, setRetryError] = useState<ApiError | null>(null);
  // The previous create's outcome was unknown: a duplicate now may be that attempt.
  const [afterUncertain, setAfterUncertain] = useState(false);
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
  const picking = draft?.mode === "pick";
  const adding = draft?.mode === "add";
  const isNew = adding;
  const canCreate = !!me?.can_create_accounts && me.accounts_configured !== false;
  const createBlocked =
    me?.accounts_configured === false
      ? "Creating accounts isn’t set up on this server."
      : me && !me.can_create_accounts
        ? "You can’t create accounts here."
        : undefined;
  const google = isNew && signIn === "google" && !!me?.sign_in?.google;
  const newProblems = {
    name: newName.trim() ? null : "Enter their name.",
    email: emailProblem(draft?.subject ?? "") ?? (google ? googleDomainProblem(subject, me?.sign_in) : null),
    password: google ? null : passwordProblem(password),
  };
  const subjectError = adding ? newProblems.email : null;
  const changes = draft ? diff(draft.row.perms, draft.selected) : { added: [], removed: [] };
  const hasChanges = changes.added.length > 0 || changes.removed.length > 0;
  // The new email already has an account: edit that person instead.
  const [lookup, setLookup] = useState<{ subject: string; person: Person | null } | null>(null);
  const existing = adding && lookup?.subject === subject ? lookup.person : null;
  useEffect(() => {
    if (!adding || emailProblem(subject)) return;
    let live = true;
    const t = setTimeout(() => {
      request<{ people: Person[] }>("/people" + query({ q: subject, limit: 20 }))
        .then((r) => live && setLookup({ subject, person: r.people.find((p) => p.subject === subject) ?? null }))
        .catch(() => undefined); // the create re-checks it
    }, 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [adding, subject]);
  const dirty =
    !!draft &&
    !picking &&
    !outcome &&
    draft.step !== "failed" &&
    (adding
      ? // The email chosen in the search is not unsaved work of its own.
        normalizeSubject(draft.subject) !== normalizeSubject(draft.search ?? "") ||
        !!newName ||
        !!newPhone ||
        !!password ||
        // Use this agent is chosen for a new user by default; anything else is a choice.
        draft.selected.length !== 1 ||
        draft.selected[0] !== USE_HUB
      : hasChanges);
  const busy = draft?.step === "saving" || checking;

  /** Close the drawer and forget this session's view state, password included. */
  const finish = useCallback(() => {
    setTouched(false);
    setExpanded([]);
    setProblems({});
    setAboutOpen(false);
    setNewName("");
    setNewPhone("");
    setSignIn("password");
    setPassword("");
    setOutcome(null);
    setRetryError(null);
    setAfterUncertain(false);
    setLookup(null);
    setDraft(null);
  }, [setDraft]);

  /** Open one person's access here, from the search or an existing email. */
  async function openPerson(target: string, known?: Person, keep: string[] = []) {
    if (!draft) return;
    const search = draft.search ?? (draft.mode === "add" ? draft.subject : undefined);
    setChecking(true);
    try {
      const row = await loadAccessRow(access.hub, normalizeSubject(target), known);
      const next = editDraft(row, search);
      setOutcome(null);
      setNewName("");
      setNewPhone("");
      setPassword("");
      setTouched(false);
      setProblems({});
      setDraft({ ...next, selected: [...new Set([...next.selected, ...keep])] });
    } finally {
      setChecking(false);
    }
  }

  /** The email belongs to an existing user: edit their access in this agent
   *  instead, keeping what was chosen. Nothing is saved until reviewed. */
  async function editExisting() {
    if (!draft) return;
    await openPerson(subject, existing ?? undefined, draft.selected);
  }

  const open = !!draft;
  const guard = useMemo(
    () => (open ? { dirty, busy, discard: finish } : null),
    [open, dirty, busy, finish],
  );
  useNavigationGuard(guard);

  function close() {
    if (!draft || busy) return;
    if (outcome?.type === "created") {
      finish();
      onSaved(`${outcome.created.name} was added`);
      return;
    }
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
    let target = subject;
    if (draft.mode === "add") {
      setTouched(true);
      if (newProblems.name || newProblems.email || newProblems.password) return;
      // Look the identity up before promising a change set, so an existing
      // grantee is edited instead of blindly re-granted.
      setChecking(true);
      try {
        const current = await request<Access>(
          "/access" + query({ hub: access.hub, q: target, limit: 200 }),
        );
        // A new user is created whatever this email already holds here; the
        // server refuses a duplicate and the result points to editing them.
        row = { ...emptyRow(target), display: newName.trim() };
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
      subject: target,
      step: "review",
      failure: undefined,
      notice: undefined,
      operations: planOperations(row.perms, draft.selected),
      progress: 0,
    });
  }

  /** The staged grants as account grants in this agent. */
  const accountGrants = (d: Draft) =>
    d.operations
      .filter((o) => o.action === "grant")
      .map((o) => ({ hub: access.hub, permission: o.permission }));

  /** Create the account and its access here. Every outcome is shown as it is. */
  async function createAccount(d: Draft) {
    setDraft({ ...d, step: "saving" });
    setRetryError(null);
    try {
      const created = await request<AccountCreated>("/accounts", {
        email: subject,
        name: newName.trim(),
        sign_in: google ? "google" : "password",
        ...(google ? {} : { password }),
        ...(me?.org_admin && newPhone.trim() ? { phone: newPhone.trim() } : {}),
        grants: accountGrants(d),
      });
      setOutcome({ type: "created", created });
    } catch (e) {
      const error = asApiError(e);
      const type =
        error.code === "account_exists"
          ? "exists"
          : error.code === "partial"
            ? "partial"
            : !error.certain
              ? "uncertain"
              : "failed";
      if (type === "uncertain") setAfterUncertain(true);
      setOutcome({ type, error });
    }
    setDraft({ ...d, step: "review" });
    onReload();
  }

  /** Give the staged access to the account that exists: "Grant access
   *  instead" after a duplicate, or the retry after a partial create. Never
   *  creates an account. */
  async function grantToAccount() {
    if (!draft || !outcome) return;
    const d = draft;
    setDraft({ ...d, step: "saving" });
    setRetryError(null);
    try {
      const result = await request<AccountGranted>("/accounts/grant", {
        email: subject,
        grants: accountGrants(d),
      });
      if (outcome.type === "partial") {
        // The account was made here; its password is still on screen to share.
        setOutcome({
          type: "created",
          created: { ok: true, subject, name: newName.trim(), grants: result.grants, sign_in: google ? "google" : "password" },
        });
      } else {
        finish();
        onSaved(`Access given to ${result.name || subject}`);
        return;
      }
    } catch (e) {
      setRetryError(asApiError(e));
    }
    setDraft({ ...d, step: "review" });
    onReload();
  }

  async function save() {
    if (!draft || draft.step !== "review" || busy) return;
    const { operations } = draft;
    const target = access.hub; // the loaded response's agent, never the route
    if (adding) {
      await createAccount(draft);
      return;
    }
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
  const outcomeTitle: Record<Outcome["type"], string> = {
    created: "User added",
    exists: "This user already exists",
    partial: "Account created, access not granted",
    uncertain: "Not confirmed",
    failed: "Nothing was created",
  };
  const title =
    draft?.step === "saving"
      ? isNew && !outcome
        ? "Creating…"
        : "Saving…"
      : outcome
        ? outcomeTitle[outcome.type]
        : draft?.step === "review"
          ? isNew
            ? "Review the new user"
            : "Review changes"
          : draft?.step === "failed"
            ? draft.uncertain
              ? "Save not confirmed"
              : "Nothing was saved"
            : draft?.mode === "pick"
              ? "Add user"
              : draft?.mode === "add"
                ? "New account"
                : draft && !draft.row.perms.length && !draft.row.inherited.length
                  ? "Give access"
                  : "Edit access";

  const footer = (() => {
    if (!draft) return null;
    if (outcome) {
      const saving = draft.step === "saving";
      if (outcome.type === "created")
        return (
          <Space className="drawer-actions">
            <Button type="primary" onClick={close}>
              Done
            </Button>
          </Space>
        );
      if (outcome.type === "failed")
        return (
          <Space className="drawer-actions" wrap>
            <Button onClick={finish}>Cancel</Button>
            <Button
              type="primary"
              onClick={() => {
                setOutcome(null);
                setDraft({ ...draft, step: "edit" });
              }}
            >
              Back to the form
            </Button>
          </Space>
        );
      return (
        <Space className="drawer-actions" wrap>
          <Button disabled={saving} onClick={finish}>
            {outcome.type === "exists" ? "Cancel" : "Done"}
          </Button>
          <Button
            type="primary"
            loading={saving}
            onClick={() =>
              void (outcome.type === "exists"
                ? editExisting()
                : outcome.type === "uncertain"
                  ? createAccount(draft)
                  : grantToAccount())
            }
          >
            {outcome.type === "exists" ? "Edit their access" : "Try again"}
          </Button>
        </Space>
      );
    }
    if (draft.step === "failed")
      return (
        <Space className="drawer-actions" wrap>
          <Button disabled={checking} onClick={finish}>
            Done
          </Button>
          <Button type="primary" loading={checking} onClick={() => void openPerson(subject)}>
            Reopen {name}
          </Button>
        </Space>
      );
    if (draft.step === "saving")
      return (
        <Space className="drawer-actions">
          <Button type="primary" loading>
            {isNew ? "Creating…" : "Saving…"}
          </Button>
        </Space>
      );
    if (draft.mode === "pick")
      return (
        <Space className="drawer-actions">
          <Button onClick={close}>Cancel</Button>
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
            {isNew
              ? "Create account"
              : removesEntry
                ? "Remove access"
                : lines === 1
                  ? "Save change"
                  : `Save ${lines} changes`}
          </Button>
        </Space>
      );
    }
    const back = draft.search !== undefined && (
      <Button onClick={() => {
        setTouched(false);
        setProblems({});
        setDraft(pickDraft(draft.search));
      }}>
        Back
      </Button>
    );
    return (
      <Space className="drawer-actions" wrap>
        <Button onClick={close}>Cancel</Button>
        {back}
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
          disabled={
            !hasChanges ||
            (adding && !!existing) ||
            (adding &&
              touched &&
              (!!subjectError || (isNew && !!(newProblems.name || newProblems.password))))
          }
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
      {draft && outcome && (
        <div className="drawer-body">
          <NewAccountOutcome
            outcome={outcome}
            email={subject}
            name={newName.trim()}
            hubName={hub.name}
            password={password}
            signIn={google ? "google" : "password"}
            afterUncertain={afterUncertain}
            retryError={retryError}
            catalog={catalog}
            onDone={close}
          />
        </div>
      )}
      {draft && !outcome && picking && (
        <div className="drawer-body">
          <PickPerson
            hubKey={access.hub}
            search={draft.subject}
            canCreate={canCreate}
            busy={checking}
            onSearch={(text) => setDraft({ ...draft, subject: text, search: text })}
            onPick={(p) => void openPerson(p.subject, p)}
            onCreate={(email) =>
              setDraft({ ...draftFor(), subject: email, search: draft.subject })
            }
          />
        </div>
      )}
      {draft && !outcome && !picking && (
        <div className="drawer-body">
          {draft.failure && draft.step !== "failed" && (
            <Alert type="error" showIcon title={draft.failure} />
          )}
          {draft.notice && <Alert type="info" showIcon title={draft.notice} />}

          {draft.step === "edit" && (
            <>
              {draft.mode === "add" ? (
                <>
                  {!canCreate && createBlocked && <Alert type="warning" showIcon title={createBlocked} />}
                  <div className="field">
                    <label className="field-label" htmlFor="new-account-name">
                      Name
                    </label>
                    <Input
                      id="new-account-name"
                      autoFocus
                      autoComplete="off"
                      value={newName}
                      status={touched && newProblems.name ? "error" : undefined}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                    {touched && newProblems.name && (
                      <Text type="danger" className="field-help">
                        {newProblems.name}
                      </Text>
                    )}
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="access-subject">
                      Email address
                    </label>
                    <Input
                      id="access-subject"
                      autoComplete="off"
                      inputMode="email"
                      placeholder="name@example.com"
                      value={draft.subject}
                      status={touched && newProblems.email ? "error" : undefined}
                      onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                    />
                    {existing ? (
                      <Text className="field-help">
                        {subject} already has an account.{" "}
                        <Button type="link" size="small" className="inline-link" onClick={() => void editExisting()}>
                          Edit their access
                        </Button>
                      </Text>
                    ) : (
                      <Text type={touched && newProblems.email ? "danger" : "secondary"} className="field-help">
                        {touched && newProblems.email ? newProblems.email : "They sign in with this email."}
                      </Text>
                    )}
                  </div>
                  {me?.org_admin && (
                    <div className="field">
                      <label className="field-label" htmlFor="new-account-phone">
                        Phone <Text type="secondary">(optional)</Text>
                      </label>
                      <Input
                        id="new-account-phone"
                        autoComplete="off"
                        inputMode="tel"
                        placeholder="+91 98000 00001"
                        value={newPhone}
                        onChange={(e) => setNewPhone(e.target.value)}
                      />
                      <Text type="secondary" className="field-help">
                        With the country code. Their WhatsApp and Telegram messages come from this number.
                      </Text>
                    </div>
                  )}
                  <NewUserSignIn
                    id="new-account-password"
                    signIn={signIn}
                    onSignIn={setSignIn}
                    options={me?.sign_in}
                    password={password}
                    onPassword={setPassword}
                    touched={touched}
                  />
                </>
              ) : (
                <>
                  <Identity row={draft.row} name={name} />
                  {!draft.row.perms.length && !draft.row.inherited.length && !access.public && (
                    <Text type="secondary">No access to {hub.name} yet. Choose what they can do.</Text>
                  )}
                </>
              )}

              {draft.row.suspended && (
                <Alert
                  type="warning"
                  showIcon
                  title="This person is blocked"
                  description="They can’t be given new access. Existing access can still be removed."
                  // Administrators reactivate them there. Others may not see them under People.
                  action={me?.org_admin && <a href={personHref(draft.row.subject)}>Open their details</a>}
                />
              )}
              {draft.row.account_unavailable && !draft.row.suspended && (
                <Alert
                  type="warning"
                  showIcon
                  title="Their account is unavailable"
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
                        : draft.selected.includes(p.permission) ||
                          draft.row.inherited.includes(p.permission);
                    const collapsible = !ALWAYS_OPEN.has(g.key);
                    return (
                      <CapabilityGroup
                        key={g.key}
                        id={g.key}
                        title={g.title}
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
                        {/* Sub-headings (Workflows, Access control) only group
                            rows for reading; counts stay with the group. */}
                        {splitSections(g.items).map((s) => (
                          <CapabilitySection key={s.key} id={`${g.key}-${s.key}`} title={s.title}>
                            {s.items.map((p) => {
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
                          </CapabilitySection>
                        ))}
                      </CapabilityGroup>
                    );
                  })}
                </div>
              </div>
            </>
          )}

          {isNew && (draft.step === "review" || draft.step === "saving") && (
            <Alert
              type="info"
              showIcon
              title={google ? "New account, signs in with Google" : "New account, signs in with a password"}
              description={
                google
                  ? `They sign in with Google as ${subject}. No password is set that anyone knows.`
                  : "The password is shown once after the account is created, to copy and share with them directly."
              }
            />
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

/** Who is being edited: a person (name, identity and account state). */
function Identity({ row, name }: { row: AccessRow; name: string }) {
  return <PersonIdentity row={row} name={name} />;
}

function PersonIdentity({ row, name }: { row: AccessRow; name: string }) {
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
            {row.status && (!service || row.status !== "service") && <AccountTag status={row.status} />}
            {/* Not signed up yet: no account, so no details page to open. */}
            {!service && row.subject !== EVERYONE && row.status !== "awaiting-signup" && (
              <Button type="link" size="small" className="inline-link" href={personHref(row.subject)}>
                View profile
              </Button>
            )}
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
          // Reopen <name> below says what to do next.
          description={draft.failure}
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

/**
 * The result of creating an account from an agent's Access tab. Account and
 * access live in two systems that can't commit together, so each state says
 * exactly what exists and what the next step does: nothing is described as
 * rolled back, and every retry reuses the account that exists.
 */
function NewAccountOutcome({
  outcome,
  email,
  name,
  hubName,
  password,
  signIn,
  afterUncertain,
  retryError,
  catalog,
  onDone,
}: {
  outcome: Outcome;
  email: string;
  name: string;
  hubName: string;
  password: string;
  signIn: SignIn;
  afterUncertain: boolean;
  retryError: ApiError | null;
  catalog: Catalog;
  /** Closes the drawer, as Done does. */
  onDone: () => void;
}) {
  const retry = retryError && (
    <Alert type="error" showIcon title="Access wasn’t granted" description={retryError.message} />
  );
  if (outcome.type === "created") {
    const granted = Object.values(outcome.created.grants).flat();
    // Any capability comes with Use this agent.
    const perms = granted.length ? [USE_HUB, ...granted.filter((p) => p !== USE_HUB)] : [];
    return (
      <>
        <Alert
          type="success"
          showIcon
          title={`${outcome.created.name || name} can now sign in as ${outcome.created.subject}`}
          description={
            perms.length
              ? `Access to ${hubName}: ${perms.map((p) => capabilityLabel(p, catalog)).join(", ")}.`
              : `No access to ${hubName} was granted.`
          }
        />
        <SignInDetails email={outcome.created.subject} password={password} signIn={outcome.created.sign_in ?? signIn} />
        <a href={personHref(outcome.created.subject)} onClick={onDone}>
          Open their details
        </a>
      </>
    );
  }
  if (outcome.type === "exists")
    return (
      <>
        <Alert
          type="info"
          showIcon
          title={`${email} already has an account`}
          description={`Nothing was changed. Edit their access to ${hubName} instead.`}
        />
        {afterUncertain && signIn === "password" && (
          <>
            <Paragraph style={{ margin: 0 }}>
              It may be the account your earlier attempt created. If so, it signs in with the password you set:
            </Paragraph>
            <OneTimePassword password={password} />
          </>
        )}
        {retry}
      </>
    );
  if (outcome.type === "partial")
    return (
      <>
        <Alert
          type="warning"
          showIcon
          title="The account was created, but access wasn’t granted"
          description={partialDetail(outcome.error)}
        />
        <SignInDetails email={email} password={password} signIn={signIn} />
        {retry}
      </>
    );
  if (outcome.type === "uncertain")
    return (
      <Alert
        type="warning"
        showIcon
        title="Couldn’t confirm whether the account was created"
        description={outcome.error.message}
      />
    );
  return <Alert type="error" showIcon title="Nothing was created" description={outcome.error.message} />;
}

/**
 * Add user, step one: choose who. Search people who already have an account
 * (picking one opens their access here), or enter a new email to create one.
 */
function PickPerson({
  hubKey,
  search,
  canCreate,
  busy,
  onSearch,
  onPick,
  onCreate,
}: {
  hubKey: string;
  search: string;
  canCreate: boolean;
  busy: boolean;
  onSearch: (text: string) => void;
  onPick: (person: Person) => void;
  onCreate: (email: string) => void;
}) {
  const [found, setFound] = useState<{ q: string; people: Person[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const q = search.trim();
  useEffect(() => {
    if (!q) return;
    let live = true;
    const t = setTimeout(() => {
      request<{ people: Person[] }>("/people" + query({ q, limit: 8 }))
        .then((r) => {
          if (!live) return;
          setFound({ q, people: r.people.filter((p) => !isService(p.subject)) });
          setError(null);
        })
        .catch((e) => live && setError(errorText(e)));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q]);

  const people = q && found?.q === q ? found.people : [];
  const email = normalizeSubject(q);
  const offerCreate = canCreate && !emailProblem(q) && found?.q === q && !people.some((p) => p.subject === email);
  const choices = [
    ...people.map((p) => () => onPick(p)),
    ...(offerCreate ? [() => onCreate(email)] : []),
  ];
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && choices.length) {
      e.preventDefault();
      choices[0]();
    }
  };

  return (
    <div className="pick-person">
      <div className="field">
        <label className="field-label" htmlFor="pick-person">
          Name or email
        </label>
        <Input
          id="pick-person"
          autoFocus
          autoComplete="off"
          prefix={<Search size={16} aria-hidden />}
          placeholder={canCreate ? "Search people, or enter a new email" : "Search people"}
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
        />
      </div>
      {error && <Alert type="error" showIcon title={error} />}
      {!q && (
        <Text type="secondary">
          {canCreate ? "Pick someone to give access, or enter a new email to create their account." : "Pick someone to give access."}
        </Text>
      )}
      {q && found?.q === q && !people.length && !offerCreate && (
        <Text type="secondary">
          No one matches “{q}”.{canCreate && emailProblem(q) ? " Enter their full email to create an account." : ""}
        </Text>
      )}
      {(people.length > 0 || offerCreate) && (
        <ul className="pick-list" aria-label="Matching people">
          {people.map((p) => {
            const here = (p.access?.[hubKey] ?? []).length > 0;
            return (
              <li key={p.subject}>
                <button type="button" className="pick-row" disabled={busy} onClick={() => onPick(p)}>
                  <PersonAvatar subject={p.subject} display={p.display ?? ""} size={32} />
                  <span className="pick-text">
                    <Text strong>{personName(p.subject, p.display ?? "")}</Text>
                    {p.display && <Text type="secondary" className="identity">{p.subject}</Text>}
                  </span>
                  {here ? <Tag style={{ marginInlineEnd: 0 }}>Has access</Tag> : p.status !== "active" && <AccountTag status={p.status} />}
                </button>
              </li>
            );
          })}
          {offerCreate && (
            <li>
              <button type="button" className="pick-row" disabled={busy} onClick={() => onCreate(email)}>
                <span className="pick-new" aria-hidden>
                  <Plus size={16} />
                </span>
                <span className="pick-text">
                  <Text strong>Create an account</Text>
                  <Text type="secondary" className="identity">{email}</Text>
                </span>
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
