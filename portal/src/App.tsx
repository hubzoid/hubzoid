import { useCallback, useEffect, useState } from 'react'
import { LayoutDashboard, GitBranch, KeyRound, ShieldCheck, ScrollText, Plus, Check } from 'lucide-react'
import { Button, Card, CardContent, CardHeader, CardTitle, Badge, Table, Th, Td, Tr } from './components/ui'
import { cn } from './lib/utils'
import { api, type Me, type Hub, type Access, type Perm, type Workflow, type AuditRow, type Overview } from './api'

const NAV = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'workflows', label: 'Workflows', icon: GitBranch },
  { id: 'access', label: 'Access', icon: KeyRound },
  { id: 'perms', label: 'Permissions', icon: ShieldCheck },
  { id: 'audit', label: 'Audit log', icon: ScrollText },
] as const

function Loading() { return <div className="p-8 text-mute text-sm">Loading…</div> }
function ErrorBox({ e }: { e: string }) { return <div className="p-4 m-4 rounded-md border bg-danger-bg text-danger text-sm">{e}</div> }

/* ---------- Overview ---------- */
function OverviewScreen() {
  const [d, setD] = useState<Overview | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.overview().then(setD).catch((e) => setErr(String(e))) }, [])
  if (err) return <ErrorBox e={err} />
  if (!d) return <Loading />
  const tiles = [
    { k: 'Hubs', v: d.hubs }, { k: 'People & services', v: d.people },
    { k: 'Grants', v: d.grants }, { k: 'Casbin authoritative', v: d.authoritative ? 'yes' : 'no' },
  ]
  return <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
    {tiles.map((t) => <Card key={t.k}><CardContent>
      <div className="text-[11px] uppercase tracking-wider text-mute">{t.k}</div>
      <div className="text-2xl font-semibold mt-1">{t.v}</div>
    </CardContent></Card>)}
  </div>
}

/* ---------- Workflows (view-only) ---------- */
function WorkflowsScreen() {
  const [wf, setWf] = useState<Workflow[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.workflows().then(setWf).catch((e) => setErr(String(e))) }, [])
  if (err) return <ErrorBox e={err} />
  if (!wf) return <Loading />
  return <Card>
    <CardHeader><CardTitle>Workflows</CardTitle><span className="text-[11px] text-mute ml-2">view-only · code-driven</span></CardHeader>
    <Table><thead><Tr><Th>Name</Th><Th>Schedule</Th><Th>Timezone</Th></Tr></thead>
      <tbody>{wf.length === 0
        ? <Tr><Td className="text-mute" colSpan={3}>No workflows defined under <code>workflows/</code>.</Td></Tr>
        : wf.map((w) => <Tr key={w.name}><Td className="font-medium">{w.name}</Td>
            <Td className="text-mute">{w.schedule || '—'}</Td><Td className="text-mute">{w.timezone || 'UTC'}</Td></Tr>)}
      </tbody></Table>
  </Card>
}

/* ---------- Access (editable) ---------- */
function AccessScreen({ hub }: { hub: string }) {
  const [acc, setAcc] = useState<Access | null>(null)
  const [err, setErr] = useState('')
  const [newSubject, setNewSubject] = useState('')
  const load = useCallback(() => { api.access(hub).then(setAcc).catch((e) => setErr(String(e))) }, [hub])
  useEffect(() => { load() }, [load])
  if (err) return <ErrorBox e={err} />
  if (!acc) return <Loading />

  const toggle = async (subject: string, perm: string, on: boolean) => {
    try {
      if (on) await api.revoke(subject, hub, perm)
      else await api.grant(subject, hub, perm)
      load()
    } catch (e) { setErr(String(e)) }
  }
  const addSubject = async () => {
    if (!newSubject.trim()) return
    try { await api.grant(newSubject.trim(), hub, 'use_hub'); setNewSubject(''); load() }
    catch (e) { setErr(String(e)) }
  }
  const prod = (p: string) => /prod|datadog/.test(p)

  return <Card>
    <CardHeader><CardTitle>Access · {hub}</CardTitle>
      {acc.editable
        ? <span className="text-[11px] text-ok ml-2">editable</span>
        : <span className="text-[11px] text-mute ml-2">view-only · not your hub</span>}
    </CardHeader>
    <div className="px-5 pt-2 text-[12px] text-mute">Grant each person or workflow the exact permissions they need.
      <span className="font-mono"> use_hub</span> opens the hub; granting any permission adds it automatically.</div>
    <Table><thead><Tr><Th>Person or workflow</Th><Th>Permissions</Th><Th>Center</Th></Tr></thead>
      <tbody>{acc.rows.length === 0
        ? <Tr><Td className="text-mute" colSpan={3}>Nobody has access yet.</Td></Tr>
        : acc.rows.map((row) => <Tr key={row.subject}>
            <Td><span className="text-[13px]">{row.subject}</span>{row.kind === 'service' && <Badge variant="outline" dot={false} className="ml-2">service</Badge>}</Td>
            <Td>{acc.permissions.map((p) => {
              const on = row.perms.includes(p)
              return <button key={p} disabled={!acc.editable} onClick={() => toggle(row.subject, p, on)}
                className={cn('inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[12px] mr-1 mb-1',
                  on ? 'bg-accent text-accent-fg border-accent' : 'bg-panel text-mute',
                  !acc.editable ? 'opacity-60 cursor-default' : 'cursor-pointer hover:border-accent')}>
                {on && <Check size={12} />}{prod(p) && <span className="text-danger">●</span>}<span className="font-mono">{p}</span>
              </button>
            })}</Td>
            <Td className="text-mute text-[12px]">{row.center || '—'}</Td>
          </Tr>)}
      </tbody></Table>
    {acc.editable && <div className="flex gap-2 p-4 border-t items-center">
      <input value={newSubject} onChange={(e) => setNewSubject(e.target.value)} placeholder="email or workflow:name"
        className="h-8 rounded-md border bg-panel px-2.5 text-[13px] outline-none focus:border-accent w-64" />
      <Button size="sm" onClick={addSubject}><Plus size={14} /> Add</Button>
    </div>}
  </Card>
}

/* ---------- Permissions (view-only) ---------- */
function PermsScreen({ hub }: { hub: string }) {
  const [perms, setPerms] = useState<Perm[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api.permissions(hub).then(setPerms).catch((e) => setErr(String(e))) }, [hub])
  if (err) return <ErrorBox e={err} />
  if (!perms) return <Loading />
  return <Card>
    <CardHeader><CardTitle>Permissions · {hub}</CardTitle><span className="text-[11px] text-mute ml-2 font-mono">read-only</span></CardHeader>
    <Table><thead><Tr><Th>Permission</Th><Th>Environment</Th></Tr></thead>
      <tbody>{perms.map((p) => <Tr key={p.permission}>
        <Td className="font-mono text-[13px]">{p.permission}</Td>
        <Td>{p.prod ? <Badge variant="danger">production</Badge> : <Badge variant="idle">non-prod</Badge>}</Td>
      </Tr>)}</tbody></Table>
  </Card>
}

/* ---------- Audit ---------- */
function AuditScreen() {
  const [rows, setRows] = useState<AuditRow[] | null>(null)
  const [denied, setDenied] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => { api.audit({ limit: 200, denied }).then(setRows).catch((e) => setErr(String(e))) }, [denied])
  if (err) return <ErrorBox e={err} />
  if (!rows) return <Loading />
  return <Card>
    <CardHeader><CardTitle>Audit log</CardTitle>
      <div className="flex-1" />
      <label className="text-[12px] text-mute flex items-center gap-1.5">
        <input type="checkbox" checked={denied} onChange={(e) => setDenied(e.target.checked)} /> denied only</label>
    </CardHeader>
    <Table><thead><Tr><Th>Time</Th><Th>User</Th><Th>Decision</Th><Th>Tool</Th><Th>Reason</Th></Tr></thead>
      <tbody>{rows.length === 0
        ? <Tr><Td className="text-mute" colSpan={5}>No access decisions logged yet.</Td></Tr>
        : rows.map((r, i) => <Tr key={i}>
            <Td className="text-mute text-[12px]">{r.ts || ''}</Td>
            <Td className="text-[12px]">{r.user || '?'}</Td>
            <Td>{r.decision === 'allow' ? <Badge variant="ok">allow</Badge> : <Badge variant="danger">deny</Badge>}</Td>
            <Td className="font-mono text-[12px]">{r.tool || '?'}</Td>
            <Td className="text-mute text-[12px]">{r.surface || ''}·{r.reason || ''}</Td>
          </Tr>)}
      </tbody></Table>
  </Card>
}

/* ---------- App shell ---------- */
export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [hubs, setHubs] = useState<Hub[]>([])
  const [hub, setHub] = useState('')
  const [view, setView] = useState<string>('overview')
  const [fatal, setFatal] = useState('')

  useEffect(() => {
    Promise.all([api.me(), api.hubs()]).then(([m, hs]) => {
      setMe(m); setHubs(hs); setHub(hs[0]?.key || '')
    }).catch((e) => setFatal(String(e)))
  }, [])

  if (fatal) return <ErrorBox e={`Not authorized or API unreachable. ${fatal}`} />
  if (!me) return <Loading />

  return <div className="min-h-screen bg-canvas text-ink flex">
    <aside className="w-52 border-r bg-panel p-3 flex flex-col gap-1 shrink-0">
      <div className="px-2 py-3 font-semibold text-[15px]">Hubzoid</div>
      {NAV.map((n) => { const Icon = n.icon
        return <button key={n.id} onClick={() => setView(n.id)}
          className={cn('flex items-center gap-2.5 px-2.5 py-1.5 rounded-md text-[13px] font-medium text-left',
            view === n.id ? 'bg-canvas text-ink border' : 'text-mute hover:bg-canvas hover:text-ink')}>
          <Icon size={15} className={view === n.id ? 'text-accent' : ''} />{n.label}</button> })}
      <div className="flex-1" />
      <div className="px-2 py-2 text-[11px] text-mute border-t">{me.subject}{me.org_admin && ' · org admin'}</div>
    </aside>
    <main className="flex-1 min-w-0">
      <header className="h-14 border-b bg-panel flex items-center px-6 gap-3">
        <div className="font-semibold text-[15px] capitalize">{NAV.find((n) => n.id === view)?.label}</div>
        <div className="flex-1" />
        {(view === 'access' || view === 'perms') && hubs.length > 0 &&
          <select value={hub} onChange={(e) => setHub(e.target.value)}
            className="h-8 rounded-md border bg-panel px-2 text-[13px]">
            {hubs.map((h) => <option key={h.key} value={h.key}>{h.name}</option>)}
          </select>}
      </header>
      <div className="p-6">
        {view === 'overview' && <OverviewScreen />}
        {view === 'workflows' && <WorkflowsScreen />}
        {view === 'access' && hub && <AccessScreen hub={hub} />}
        {view === 'perms' && hub && <PermsScreen hub={hub} />}
        {view === 'audit' && <AuditScreen />}
      </div>
    </main>
  </div>
}
