import { useEffect, useState } from "react";
import { cn } from "./lib/utils";
import {
  LayoutDashboard,
  GitBranch,
  KeyRound,
  ShieldCheck,
  ScrollText,
  Users,
  RefreshCw,
  ExternalLink,
} from "lucide-react";
import {
  Button,
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Badge,
  Table,
  Th,
  Td,
  Tr,
} from "./components/ui";
import {
  request,
  query,
  type Me,
  type Hub,
  type Access,
  type Permission,
  type Workflow,
  type Run,
  type Person,
  type AuditRow,
  type Overview,
  type Sync,
} from "./api";

const nav = [
  ["overview", "Overview", LayoutDashboard],
  ["access", "Access", KeyRound],
  ["people", "People", Users],
  ["workflows", "Workflows", GitBranch],
  ["permissions", "Permissions", ShieldCheck],
  ["audit", "Audit", ScrollText],
] as const;
const input =
  "rounded-md border bg-panel px-3 py-2 text-sm focus:outline-2 focus:outline-accent";
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
function useData<T>(path: string) {
  const [state, setState] = useState<{
    path: string;
    data?: T;
    error?: string;
  }>({ path: "" });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    request<T>(path, undefined, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ path, data });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setState({ path, error: message(e) });
      });
    return () => controller.abort();
  }, [path, revision]);
  // Stale-hub protection: until the effect resolves for the CURRENT path, the
  // stored state still carries the previous path, so we surface an empty
  // (loading) record rather than another hub's data. Deriving this during
  // render avoids a synchronous setState inside the effect.
  return {
    ...(state.path === path ? state : {}),
    reload: () => setRevision((v) => v + 1),
  };
}
function Notice({ text, error = false }: { text: string; error?: boolean }) {
  return (
    <div
      role={error ? "alert" : "status"}
      className={`rounded-md border p-3 text-sm ${error ? "bg-danger-bg text-danger" : "bg-canvas text-mute"}`}
    >
      {text}
    </div>
  );
}
function Load({ error, retry }: { error?: string; retry: () => void }) {
  return error ? (
    <div className="space-y-3">
      <Notice error text={error} />
      <Button variant="outline" onClick={retry}>
        Try again
      </Button>
    </div>
  ) : (
    <p role="status" className="text-mute p-4">
      Loading…
    </p>
  );
}
function Pager({
  offset,
  setOffset,
  total,
  count,
}: {
  offset: number;
  setOffset: (n: number) => void;
  total?: number;
  count: number;
}) {
  return (
    <div className="flex gap-3 items-center p-4 border-t text-sm text-mute">
      <Button
        variant="outline"
        disabled={offset === 0}
        onClick={() => setOffset(Math.max(0, offset - 50))}
      >
        Previous
      </Button>
      <span>
        {count ? `${offset + 1}–${offset + count}` : "No results"}
        {total !== undefined ? ` of ${total}` : ""}
      </span>
      <Button
        variant="outline"
        disabled={total !== undefined ? offset + count >= total : count < 50}
        onClick={() => setOffset(offset + 50)}
      >
        Next
      </Button>
    </div>
  );
}
function time(value: number | string | null | undefined) {
  if (value == null) return "—";
  return new Date(
    typeof value === "number" && value < 1e12 ? value * 1000 : value,
  ).toLocaleString();
}
function Status({ value }: { value: string }) {
  return (
    <Badge
      variant={
        /error|fail|block|stale/i.test(value)
          ? "danger"
          : /success|active|scheduled|ok/i.test(value)
            ? "ok"
            : "idle"
      }
    >
      {value.replaceAll("-", " ")}
    </Badge>
  );
}

function OverviewScreen({ me }: { me: Me }) {
  const d = useData<Overview>("/overview");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  if (!d.data) return <Load error={d.error} retry={d.reload} />;
  const x = d.data;
  async function sync() {
    setBusy(true);
    try {
      const r = await request<Sync>("/sync", {});
      setNote(r.error || `Visibility sync ${r.state}`);
      d.reload();
    } catch (e) {
      setNote(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
        {[
          ["Agents", x.hubs],
          ["People & services", x.people],
          ["Access grants", x.grants],
          ["Managed here", `${x.managed} / ${x.hubs}`],
        ].map(([k, v]) => (
          <Card key={k}>
            <CardContent>
              <p className="text-sm text-mute">{k}</p>
              <p className="text-3xl font-semibold mt-2">{v}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Access management</CardTitle>
        </CardHeader>
        <CardContent>
          <p>
            {x.legacy
              ? `${x.legacy} agent(s) still use legacy access. Preview and migrate them before relying on portal grants.`
              : "All registered agents use the shared permission store."}
          </p>
          <p className="mt-3 text-sm text-mute">
            Open WebUI visibility: {x.visibility.state} · Last sync{" "}
            {time(x.visibility.updated)}
          </p>
          {x.visibility.error && <Notice error text={x.visibility.error} />}
          <div className="flex gap-3 mt-4">
            {me.org_admin && (
              <Button disabled={busy} onClick={sync}>
                <RefreshCw size={14} /> {busy ? "Syncing…" : "Sync now"}
              </Button>
            )}
            <a className="underline text-sm py-2" href="/">
              Open chat
            </a>
          </div>
          {note && <Notice text={note} />}
        </CardContent>
      </Card>
      <Notice text="Accounts and sign-in are managed in Open WebUI. This portal manages who can use each agent and its tools. Access changes take effect immediately; agent visibility refreshes within 30 seconds." />
    </div>
  );
}
function AccessScreen({ hub }: { hub: string }) {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const d = useData<Access>("/access" + query({ hub, q: search, offset }));
  const [subject, setSubject] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const acc = d.data;
  async function mutate(person: string, perm: string, on: boolean) {
    if (!acc || acc.hub !== hub || busy) return;
    if (
      on &&
      perm === "use_hub" &&
      !window.confirm(
        `Remove ${person}'s direct access and ALL permissions in ${hub}? Public access, if enabled, still applies.`,
      )
    )
      return;
    const target = acc.hub;
    setBusy(true);
    setErr("");
    setNote("");
    try {
      await request("/access/" + (on ? "revoke" : "grant"), {
        subject: person,
        hub: target,
        permission: perm,
      });
      setSubject("");
      setNote("Saved. Open WebUI visibility updates within 30 seconds.");
      d.reload();
    } catch (e) {
      setErr(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Agent access</CardTitle>
        <input
          aria-label="Search access"
          placeholder="Search people or services"
          className={`${input} ml-auto`}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setOffset(0);
          }}
        />
      </CardHeader>
      {err && (
        <div className="p-4">
          <Notice error text={err} />
        </div>
      )}
      {note && (
        <div className="px-4 pt-4">
          <Notice text={note} />
        </div>
      )}
      {!acc ? (
        <Load error={d.error} retry={d.reload} />
      ) : (
        <>
          {!acc.authoritative && (
            <Notice text="This agent still uses legacy access. Grants saved here take effect only after migration." />
          )}
          {acc.public && (
            <Notice text="Everyone signed in can use this agent. Removing a direct grant does not remove public access." />
          )}
          <div className="overflow-x-auto">
            <Table>
              <thead>
                <Tr>
                  <Th>Person or service</Th>
                  <Th>Permissions</Th>
                  <Th>Account</Th>
                </Tr>
              </thead>
              <tbody>
                {acc.rows.length === 0 ? (
                  <Tr>
                    <Td colSpan={3}>
                      No matching access grants. Add an existing user or their
                      signup email below.
                    </Td>
                  </Tr>
                ) : (
                  acc.rows.map((row) => (
                    <Tr key={row.subject}>
                      <Td>
                        <div className="font-medium">
                          {row.subject === "*"
                            ? "Everyone signed in"
                            : row.display}
                        </div>
                        <div className="text-xs text-mute">{row.subject}</div>
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-2">
                          {acc.permissions.map((p) => {
                            const direct = row.perms.includes(p.permission);
                            const inherited = row.inherited.includes(
                              p.permission,
                            );
                            const effective = row.effective.includes(
                              p.permission,
                            );
                            const protectedAdmin =
                              !acc.can_manage_admins &&
                              (p.permission === "manage_access" ||
                                (p.permission === "use_hub" &&
                                  row.effective.includes("manage_access")));
                            return (
                              <button
                                key={p.permission}
                                aria-pressed={effective}
                                title={p.description || p.permission}
                                disabled={
                                  busy ||
                                  inherited ||
                                  protectedAdmin ||
                                  (row.subject === "*" &&
                                    (!acc.can_manage_admins ||
                                      p.permission !== "use_hub"))
                                }
                                onClick={() =>
                                  mutate(row.subject, p.permission, direct)
                                }
                                className={cn(
                                  input,
                                  "py-1 disabled:opacity-60",
                                  effective && "bg-accent text-accent-fg",
                                )}
                              >
                                {p.label}
                                {inherited
                                  ? " (inherited)"
                                  : !direct && effective
                                    ? " (public)"
                                    : ""}
                                {p.sensitive ? " · sensitive" : ""}
                              </button>
                            );
                          })}
                        </div>
                      </Td>
                      <Td>
                        <Status value={row.status} />
                      </Td>
                    </Tr>
                  ))
                )}
              </tbody>
            </Table>
          </div>
          <Pager
            offset={offset}
            setOffset={setOffset}
            total={acc.total}
            count={acc.rows.length}
          />
          <form
            className="p-4 border-t space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              mutate(subject.trim(), "use_hub", false);
            }}
          >
            <div className="flex flex-wrap gap-2">
              <label className="sr-only" htmlFor="new-user">
                Email or workflow service
              </label>
              <input
                id="new-user"
                className={`${input} min-w-64`}
                placeholder="Email or workflow:name"
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                required
              />
              <Button disabled={busy || !subject.trim()}>
                Grant agent access
              </Button>
            </div>
            <p className="text-xs text-mute">
              This grants permission; it does not create an account or send an
              invitation. The person signs up through Open WebUI using this
              email.
            </p>
          </form>
        </>
      )}
    </Card>
  );
}
function PeopleScreen({ me }: { me: Me }) {
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const d = useData<{ people: Person[]; total: number }>(
    "/people" + query({ q, offset }),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function act(path: string, body: unknown) {
    setBusy(true);
    setErr("");
    try {
      await request(path, body);
      d.reload();
    } catch (e) {
      setErr(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>People & services</CardTitle>
        <input
          aria-label="Search people"
          className={`${input} ml-auto`}
          placeholder="Search people"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        {me.org_admin && (
          <Button
            disabled={busy}
            variant="outline"
            onClick={() => act("/people/refresh", {})}
          >
            Refresh accounts
          </Button>
        )}
      </CardHeader>
      <div className="p-4">
        <Notice text="Open WebUI owns accounts, invitations and sign-in. Blocking here removes direct grants and denies agent access, including public agents. Reactivating does not restore old grants." />
        {err && <Notice error text={err} />}
      </div>
      {!d.data ? (
        <Load error={d.error} retry={d.reload} />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <thead>
              <Tr>
                <Th>Person</Th>
                <Th>Account</Th>
                <Th>Effective access by agent</Th>
                <Th>Actions</Th>
              </Tr>
            </thead>
            <tbody>
              {d.data.people.map((p) => (
                <Tr key={p.subject}>
                  <Td>
                    {p.display || p.subject}
                    <p className="text-xs text-mute">{p.subject}</p>
                  </Td>
                  <Td>
                    <Status
                      value={
                        p.blocked
                          ? "blocked"
                          : p.subject.startsWith("workflow:")
                            ? "service"
                            : !p.owui_id
                              ? "awaiting-signup"
                              : p.pending
                                ? "pending-approval"
                                : "active"
                      }
                    />
                  </Td>
                  <Td>
                    {Object.entries(p.access)
                      .filter(([, perms]) => perms.length)
                      .map(([h, perms]) => (
                        <p key={h} className="text-xs">
                          <strong>{h}</strong>: {perms.join(", ")}
                        </p>
                      ))}
                  </Td>
                  <Td>
                    {me.org_admin && (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() => {
                          if (
                            window.confirm(
                              `${p.blocked ? "Reactivate" : "Block agent access for"} ${p.subject}?`,
                            )
                          )
                            act("/people/block", {
                              subject: p.subject,
                              suspended: !p.blocked,
                            });
                        }}
                      >
                        {p.blocked ? "Reactivate" : "Block access"}
                      </Button>
                    )}
                    {me.org_admin &&
                      !p.blocked &&
                      !p.subject.startsWith("workflow:") && (
                        <Button
                          variant="outline"
                          className="mt-2"
                          disabled={busy}
                          onClick={() => {
                            if (
                              window.confirm(
                                `${p.organization_admin ? "Remove" : "Grant"} organization administrator rights for ${p.subject}?`,
                              )
                            )
                              act(
                                "/access/" +
                                  (p.organization_admin ? "revoke" : "grant"),
                                {
                                  subject: p.subject,
                                  hub: "*",
                                  permission: "manage_access",
                                },
                              );
                          }}
                        >
                          {p.organization_admin
                            ? "Remove org admin"
                            : "Make org admin"}
                        </Button>
                      )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      {d.data && (
        <Pager
          offset={offset}
          setOffset={setOffset}
          count={d.data.people.length}
          total={d.data.total}
        />
      )}
      {me.org_admin && (
        <div className="p-4 border-t">
          <a href="/admin" className="underline text-sm">
            Manage Open WebUI accounts ↗
          </a>
        </div>
      )}
    </Card>
  );
}
function PermissionsScreen({ hub }: { hub: string }) {
  const d = useData<{ permissions: Permission[] }>(
    "/permissions" + query({ hub }),
  );
  if (!d.data) return <Load error={d.error} retry={d.reload} />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Available permissions</CardTitle>
      </CardHeader>
      <Table>
        <thead>
          <Tr>
            <Th>Permission</Th>
            <Th>Description</Th>
            <Th>Sensitivity</Th>
          </Tr>
        </thead>
        <tbody>
          {d.data.permissions.map((p) => (
            <Tr key={p.permission}>
              <Td>
                {p.label}
                <p className="font-mono text-xs text-mute">{p.permission}</p>
              </Td>
              <Td>{p.description || "—"}</Td>
              <Td>
                {p.sensitive ? (
                  <Badge variant="warn">Sensitive</Badge>
                ) : (
                  "Standard"
                )}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}
function Runs({
  hub,
  name,
  back,
}: {
  hub: string;
  name: string;
  back: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState("");
  const d = useData<{ runs: Run[] }>(
    "/runs" + query({ hub, workflow: name, offset, run_id: detail }),
  );
  return (
    <Card>
      <CardHeader>
        <Button variant="outline" onClick={detail ? () => setDetail("") : back}>
          ← Back
        </Button>
        <CardTitle>
          {hub} / {name}
        </CardTitle>
        <Button variant="outline" className="ml-auto" onClick={d.reload}>
          Refresh
        </Button>
      </CardHeader>
      {!d.data ? (
        <Load error={d.error} retry={d.reload} />
      ) : (
        <>
          <div className="overflow-x-auto">
            <Table>
              <thead>
                <Tr>
                  <Th>Run</Th>
                  <Th>Status</Th>
                  <Th>Started</Th>
                  <Th>Duration</Th>
                </Tr>
              </thead>
              <tbody>
                {!d.data.runs.length ? (
                  <Tr>
                    <Td colSpan={4}>No recorded runs yet.</Td>
                  </Tr>
                ) : (
                  d.data.runs.map((r) => (
                    <Tr key={r.id}>
                      <Td>
                        <button
                          className="underline font-mono text-xs"
                          onClick={() => {
                            setOffset(0);
                            setDetail(r.id);
                          }}
                        >
                          {r.id}
                        </button>
                      </Td>
                      <Td>
                        <Status value={r.status} />
                      </Td>
                      <Td>{time(r.started)}</Td>
                      <Td>
                        {r.duration_ms === null
                          ? "—"
                          : `${(r.duration_ms / 1000).toFixed(1)}s`}
                      </Td>
                    </Tr>
                  ))
                )}
              </tbody>
            </Table>
          </div>
          {!detail && (
            <Pager
              offset={offset}
              setOffset={setOffset}
              count={d.data.runs.length}
            />
          )}
          {detail &&
            d.data.runs.map((r) => (
              <CardContent key={r.id}>
                <h3 className="font-medium">Result</h3>
                <pre className="whitespace-pre-wrap break-all text-xs p-3 bg-canvas">
                  {r.error || r.output || "No output yet"}
                </pre>
                <h3 className="font-medium mt-4">Execution steps</h3>
                {r.steps?.map((step, i) => (
                  <details key={i} className="border rounded-md p-3 mt-2">
                    <summary>
                      {step.name} · {time(step.started)}{" "}
                      {step.error ? "· Failed" : ""}
                    </summary>
                    <pre className="whitespace-pre-wrap break-all text-xs mt-2">
                      {step.error || step.output || "No output"}
                    </pre>
                  </details>
                ))}
              </CardContent>
            ))}
        </>
      )}
    </Card>
  );
}
function WorkflowsScreen({ hub }: { hub: string }) {
  const d = useData<{ workflows: Workflow[] }>("/workflows" + query({ hub }));
  const [selected, setSelected] = useState<Workflow | null>(null);
  if (selected)
    return (
      <Runs
        hub={selected.hub}
        name={selected.name}
        back={() => setSelected(null)}
      />
    );
  if (!d.data) return <Load error={d.error} retry={d.reload} />;
  const stale = d.data.workflows.some((w) => w.state === "stale");
  const downtime = d.data.workflows.find(
    (w) => w.downtime && w.downtime.missed,
  )?.downtime;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workflows</CardTitle>
        <Button className="ml-auto" variant="outline" onClick={d.reload}>
          Refresh
        </Button>
      </CardHeader>
      {stale && (
        <div className="px-4 pt-4">
          <Notice
            error
            text="No dispatcher heartbeat recently. The scheduler process may be stopped; scheduled runs will not fire until it restarts."
          />
        </div>
      )}
      {downtime && (
        <div className="px-4 pt-4">
          <Notice
            text={`${downtime.missed} scheduled run(s) were missed during downtime (${time(downtime.since)} – ${time(downtime.until)}). Missed runs are not back-filled.`}
          />
        </div>
      )}
      <div className="overflow-x-auto">
        <Table>
          <thead>
            <Tr>
              <Th>Agent / workflow</Th>
              <Th>Schedule</Th>
              <Th>State</Th>
              <Th>Next run</Th>
              <Th>Insights</Th>
            </Tr>
          </thead>
          <tbody>
            {!d.data.workflows.length ? (
              <Tr>
                <Td colSpan={5}>
                  No workflows defined. Add workflows/&lt;name&gt;/main.py to an
                  agent.
                </Td>
              </Tr>
            ) : (
              d.data.workflows.map((w) => (
                <Tr key={`${w.hub}:${w.name}`}>
                  <Td>
                    <p className="text-xs text-mute">{w.hub}</p>
                    {w.name}
                  </Td>
                  <Td>
                    {w.schedule || "Manual"}
                    <p className="text-xs text-mute">{w.timezone}</p>
                  </Td>
                  <Td>
                    <Status value={w.state} />
                    {w.error && (
                      <p className="text-danger text-xs mt-2">{w.error}</p>
                    )}
                  </Td>
                  <Td>
                    {time(w.next_run)}
                    <p className="text-xs text-mute">
                      Skipped slots: {w.missed}
                    </p>
                  </Td>
                  <Td>
                    <Button variant="outline" onClick={() => setSelected(w)}>
                      View runs
                    </Button>
                  </Td>
                </Tr>
              ))
            )}
          </tbody>
        </Table>
      </div>
    </Card>
  );
}
function AuditScreen({ hub }: { hub: string }) {
  const [kind, setKind] = useState("access-changes");
  const [user, setUser] = useState("");
  const [denied, setDenied] = useState(false);
  const [offset, setOffset] = useState(0);
  const d = useData<{ rows: AuditRow[] }>(
    "/" +
      kind +
      query({
        hub,
        user,
        denied: kind === "audit" ? denied : undefined,
        offset,
      }),
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit history</CardTitle>
        <select
          aria-label="Audit type"
          className={input}
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setOffset(0);
          }}
        >
          <option value="access-changes">Access changes</option>
          <option value="audit">Tool decisions</option>
        </select>
        <input
          className={input}
          aria-label="Filter audit by user"
          placeholder="Exact user email"
          value={user}
          onChange={(e) => {
            setUser(e.target.value);
            setOffset(0);
          }}
        />
        {kind === "audit" && (
          <label className="text-sm">
            <input
              type="checkbox"
              checked={denied}
              onChange={(e) => {
                setDenied(e.target.checked);
                setOffset(0);
              }}
            />{" "}
            Denied only
          </label>
        )}
      </CardHeader>
      {!d.data ? (
        <Load error={d.error} retry={d.reload} />
      ) : (
        <>
          <div className="overflow-x-auto">
            <Table>
              <thead>
                <Tr>
                  <Th>Time</Th>
                  <Th>Agent</Th>
                  <Th>Actor</Th>
                  <Th>Person / tool</Th>
                  <Th>Change / decision</Th>
                </Tr>
              </thead>
              <tbody>
                {!d.data.rows.length ? (
                  <Tr>
                    <Td colSpan={5}>No matching events.</Td>
                  </Tr>
                ) : (
                  d.data.rows.map((r, i) => (
                    <Tr key={i}>
                      <Td>{time(r.ts)}</Td>
                      <Td>{r.hub === "*" ? "Organization" : r.hub}</Td>
                      <Td>{r.actor || r.user || "—"}</Td>
                      <Td>{r.subject || r.tool || "—"}</Td>
                      <Td>
                        {r.action || r.decision}{" "}
                        {r.permission || r.reason || ""}
                      </Td>
                    </Tr>
                  ))
                )}
              </tbody>
            </Table>
          </div>
          <Pager
            offset={offset}
            setOffset={setOffset}
            count={d.data.rows.length}
          />
        </>
      )}
    </Card>
  );
}
export default function App() {
  const me = useData<Me>("/me");
  const hubs = useData<{ hubs: Hub[] }>("/hubs");
  const [view, setView] = useState("overview");
  const [hub, setHub] = useState("");
  const first = hubs.data?.hubs[0]?.key || "";
  const current =
    hub === "*" && ["workflows", "audit"].includes(view)
      ? "*"
      : hub === "*" || !hub
        ? first
        : hub;
  if (!me.data || !hubs.data)
    return (
      <div className="max-w-xl mx-auto p-8 space-y-4">
        <h1 className="text-xl font-semibold">Hubzoid administration</h1>
        <Load
          error={me.error || hubs.error}
          retry={() => {
            me.reload();
            hubs.reload();
          }}
        />
        <a href="/" className="underline">
          Return to chat / sign in
        </a>
      </div>
    );
  return (
    <div className="min-h-screen bg-canvas text-ink md:flex">
      <aside className="md:w-56 shrink-0 border-r bg-panel p-3 flex md:flex-col gap-1 flex-wrap md:min-h-screen">
        <div className="p-3 font-semibold">
          Hubzoid{" "}
          <span className="block text-xs font-normal text-mute">
            Administration
          </span>
        </div>
        <nav
          aria-label="Administration"
          className="flex md:flex-col flex-wrap gap-1"
        >
          {nav.map(([id, label, Icon]) => (
            <button
              key={id}
              aria-current={view === id ? "page" : undefined}
              onClick={() => setView(id)}
              className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm text-left ${view === id ? "border bg-canvas text-accent" : "text-mute hover:bg-canvas"}`}
            >
              <Icon size={16} />
              {label}
            </button>
          ))}
        </nav>
        <div className="md:flex-1" />
        <a href="/" className="flex gap-2 text-sm p-3 underline">
          <ExternalLink size={14} /> Open chat
        </a>
        <p className="text-xs text-mute border-t p-3 break-all">
          {me.data.subject}
          <br />
          {me.data.org_admin
            ? "Organization administrator"
            : "Agent administrator"}
        </p>
      </aside>
      <main className="flex-1 min-w-0">
        <header className="border-b bg-panel flex flex-wrap items-center gap-3 px-6 py-4">
          <h1 className="font-semibold">
            {nav.find(([id]) => id === view)?.[1]}
          </h1>
          {["access", "permissions", "workflows", "audit"].includes(view) && (
            <label className="ml-auto flex gap-2 items-center text-sm">
              Agent
              <select
                aria-label="Agent"
                value={current}
                onChange={(e) => setHub(e.target.value)}
                className={input}
              >
                {["workflows", "audit"].includes(view) && (
                  <option value="*">All agents</option>
                )}
                {hubs.data.hubs.map((h) => (
                  <option key={h.key} value={h.key}>
                    {h.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </header>
        <div className="p-4 md:p-6" key={`${view}:${current}`}>
          {view === "overview" && <OverviewScreen me={me.data} />}
          {view === "access" && current && <AccessScreen hub={current} />}
          {view === "people" && <PeopleScreen me={me.data} />}
          {view === "permissions" && current && (
            <PermissionsScreen hub={current} />
          )}
          {view === "workflows" && (
            <WorkflowsScreen hub={current === "*" ? "" : current} />
          )}
          {view === "audit" && (
            <AuditScreen hub={current === "*" ? "" : current} />
          )}
        </div>
      </main>
    </div>
  );
}
