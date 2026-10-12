# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""The artifact viewer page: a thin toolbar (title, time, Share, Download) with the
content below. One self-contained page, no external assets, rendered with DOM
text nodes only (never innerHTML with artifact data). A strict CSP with a
per-response nonce covers its one script and one stylesheet."""
from __future__ import annotations

import html
import json

_CSS = """
:root{--bg:#fafaf8;--panel:#ffffff;--ink:#0b0b0c;--body:#1f1f22;--muted:#6b6b70;--line:#e7e6e2;
--soft:#f4f3ef;--accent:#b5471f;--accent-ink:#fff;--accent-soft:#fff0e9;--accent-line:#efb9a5;
--warn-bg:#fff5da;--warn-ink:#805500;--danger:#a3322a;--info-bg:#eef3fa;--info:#345e91;
--ok-bg:#eaf4ed;--ok:#276444;
--font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#151516;--panel:#222224;--ink:#fafaf8;--body:#e9e9e6;
--muted:#b5b5bc;--line:#3c3c40;--soft:#26262a;--accent:#f28b66;--accent-ink:#0b0b0c;--accent-soft:#2a160f;
--accent-line:#5a3424;--warn-bg:#352b19;--warn-ink:#e6bf72;--danger:#ffb4b4;--info-bg:#1c2d43;--info:#adccf0;
--ok-bg:#183328;--ok:#a1d8b6}}
*{box-sizing:border-box}html,body{margin:0;height:100%}input{accent-color:var(--accent)}
body{background:var(--bg);color:var(--ink);font:14px/1.45 var(--font);display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:10px;padding:8px 16px;border-bottom:1px solid var(--line);
background:var(--panel);min-height:54px;flex-wrap:wrap}
header .t{flex:1;min-width:0}
header h1{font-size:15px;font-weight:600;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header .m{color:var(--muted);font-size:12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.badge{display:inline-block;font-size:11.5px;line-height:1;padding:4px 8px;border-radius:999px;background:var(--soft);color:var(--body)}
.badge.team{background:var(--info-bg);color:var(--info)}.badge.mine{background:var(--accent-soft);color:var(--accent)}
.badge.warn{background:var(--warn-bg);color:var(--warn-ink)}
button,.btn{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);
padding:6px 12px;border-radius:8px;cursor:pointer;text-decoration:none;white-space:nowrap}
button:hover,.btn:hover{background:var(--soft)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button.danger{color:var(--danger)}
button:disabled{opacity:.55;cursor:default}
button:focus-visible,.btn:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#notice{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 16px;font-size:13px;
background:var(--info-bg);border-bottom:1px solid var(--line);color:var(--info)}
.q{display:inline-grid;place-items:center;width:16px;height:16px;border:1px solid var(--line);border-radius:50%;font-size:10.5px;color:var(--muted);cursor:help;vertical-align:1px}
#notice.err{background:var(--warn-bg);color:var(--warn-ink)}#notice[hidden]{display:none}
main{flex:1;min-height:0;display:flex}
main>iframe{flex:1;border:0;width:100%;background:#fff}
main>.pad{flex:1;overflow:auto;padding:16px}
img.preview{max-width:100%;height:auto;display:block;margin:0 auto;background:#fff}
pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 var(--mono);margin:0}
table{border-collapse:collapse;font-size:13px}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
tr:first-child td{font-weight:600;background:var(--panel)}
.note{color:var(--muted);margin:16px 0}
.center{margin:auto;max-width:440px;padding:32px 16px;text-align:center}
.center h1{font-size:20px;margin:0 0 8px}
.center p{color:var(--muted);margin:0 0 16px}
#share{position:fixed;top:60px;right:16px;width:min(420px,calc(100vw - 32px));background:var(--panel);
border:1px solid var(--line);border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.12);padding:16px;display:none;z-index:5}
#share.open{display:block}
#share h2{font-size:14px;margin:0 0 10px}
#share label{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
#share label.off{color:var(--muted)}
#share small{display:block;color:var(--muted)}
#share label.field{display:block;margin:8px 0 4px;font-weight:600}
#share textarea{width:100%;min-height:70px;font:inherit;border:1px solid var(--line);border-radius:8px;
padding:6px;background:var(--bg);color:var(--ink)}
#share select{font:inherit}
.warn{background:var(--warn-bg);color:var(--warn-ink);border-radius:8px;padding:8px;margin:8px 0;font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
.linkbox{width:100%;font:12px var(--mono);padding:6px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--ink)}
#status{color:var(--muted);font-size:12px;min-height:1em;margin-top:6px}
@media print{header,#share,#notice{display:none}}
"""

_JS = r"""
(function(){
const cfg = JSON.parse(document.getElementById('hz-cfg').textContent);
const $ = (id) => document.getElementById(id);
const el = (tag, attrs, text) => { const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text; return n; };
let meta = null;

// A terminal state: a heading instead of "Loading…", the reason, and a way on.
function fail(msg, heading){
  const h = heading || (cfg.mode === 'public' ? 'Link not available' : 'Artifact not available');
  document.title = h; $('title').textContent = h; $('meta').textContent = '';
  const c = el('div', {class:'center'}); c.append(el('p', {}, msg));
  c.append(el('a', {class:'btn', href:'/'}, cfg.mode === 'public' ? 'Go to the sign-in page' : 'Back to the chat'));
  $('content').replaceChildren(c); }

async function load(){
  let r;
  try {
    if (cfg.mode === 'public') {
      const token = decodeURIComponent(location.hash.slice(1));
      if (!token) return fail('This link is incomplete. Ask the owner for the full link.');
      r = await fetch('/portal/p/open', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({token})});
    } else {
      r = await fetch(cfg.api, {credentials:'same-origin'});
    }
  } catch (e) { return fail('Could not reach the server. Try again.'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) return fail(data.detail || 'This artifact is not available.');
  meta = data; render();
}

function when(ts){
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const day = new Date(now); day.setHours(0,0,0,0);
  const that = new Date(d); that.setHours(0,0,0,0);
  const diff = Math.round((that - day) / 86400000);
  if (diff === 0) return 'today ' + hm;
  if (diff === -1) return 'yesterday ' + hm;
  if (diff === 1) return 'tomorrow ' + hm;
  return d.toLocaleDateString([], {weekday:'short', day:'numeric', month:'short'}) + ' ' + hm;
}

function header(){
  const m = $('meta'); m.replaceChildren();
  const s = meta.series;
  if (!s) { m.textContent = new Date(meta.created * 1000).toLocaleString() + ' · ' + meta.filename; return; }
  m.append(el('span', {class: 'badge ' + (s.scope === 'team' ? 'team' : 'mine')}, s.scope === 'team' ? 'Team report' : 'Your report'));
  if (s.state === 'stopped') {
    m.append(el('span', {class:'badge'}, 'No longer updating'));
    m.append(el('span', {}, 'Last updated ' + when(s.updated)));
  } else if (s.state === 'failed') {
    m.append(el('span', {class:'badge warn'}, 'Not updated since ' + when(s.updated)));
    m.append(el('span', {}, 'The last update failed.'));
  } else {
    m.append(el('span', {}, 'Updated ' + when(s.updated) + (s.next ? ' · Next update ' + when(s.next) : '')));
  }
  const up = $('updatebtn');
  up.hidden = !s.can_update;
  if (s.can_update) {
    up.disabled = s.update_wait > 0;
    up.title = s.update_wait > 0 ? 'You can update again in ' + Math.ceil(s.update_wait / 60) + ' minutes.'
      : 'Reruns the run that made this copy, with the same inputs.';
  }
  if (s.company) $('sharebtn').hidden = false;
}

let shown = null, polling = null;
function poll(){
  if (!meta || !meta.series || polling) return;
  const url = cfg.api + '/latest';
  polling = setInterval(async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      const r = await fetch(url, {credentials:'same-origin', headers: shown ? {'If-None-Match': '"' + shown + '"'} : {}});
      if (r.status !== 200) return;
      const d = await r.json();
      if (d.artifact && d.artifact !== shown) {
        $('notice-text').textContent = 'A newer copy is ready (' + when(d.updated) + ').';
        $('showbtn').hidden = false; $('notice').className = ''; $('notice').hidden = false;
      }
    } catch (e) { /* offline: try again next time */ }
  }, 60000);
}

async function showNewest(){
  $('notice').hidden = true;
  const r = await fetch(cfg.api, {credentials:'same-origin'});
  if (r.ok) { meta = await r.json(); render(); }
}

async function updateNow(){
  const up = $('updatebtn'); up.disabled = true; up.textContent = 'Updating…';
  try {
    await post(cfg.api + '/update', {});
    $('notice-text').textContent = 'Updating. The new copy appears here when it is ready.';
    $('notice').className = ''; $('notice').hidden = false; $('showbtn').hidden = true;
    shown = shown || (meta && meta.id);
  } catch (e) {
    up.textContent = 'Update now'; up.disabled = false;
    $('notice-text').textContent = e.message; $('notice').className = 'err'; $('notice').hidden = false;
    $('showbtn').hidden = true;
  }
}

function render(){
  document.title = meta.title;
  $('title').textContent = meta.title;
  shown = meta.id;
  $('updatebtn').textContent = 'Update now';
  $('showbtn').hidden = false;
  header();
  const dl = $('download'); dl.href = meta.download_url; dl.hidden = false;
  if (meta.role === 'owner' && meta.sharing) $('sharebtn').hidden = false;
  const box = $('content'); box.replaceChildren();
  if (meta.kind === 'html') {
    box.append(el('iframe', {src: meta.content_url, title: meta.title,
      sandbox: 'allow-scripts allow-popups allow-modals allow-downloads', referrerpolicy: 'no-referrer'}));
  } else if (meta.kind === 'pdf') {
    box.append(el('iframe', {src: meta.content_url, title: meta.title, referrerpolicy: 'no-referrer'}));
  } else if (meta.kind === 'image' || meta.kind === 'svg') {
    const pad = el('div', {class:'pad'}); pad.append(el('img', {src: meta.content_url, alt: meta.title, class:'preview'})); box.append(pad);
  } else if (meta.preview && meta.preview.kind === 'csv') {
    const pad = el('div', {class:'pad'}); const t = el('table');
    for (const row of meta.preview.rows) { const tr = el('tr'); for (const c of row) tr.append(el('td', {}, c)); t.append(tr); }
    pad.append(t);
    if (meta.preview.truncated) pad.append(el('p', {class:'note'}, 'Showing the first rows. Download the file for all of it.'));
    box.append(pad);
  } else if (meta.preview && meta.preview.kind === 'text') {
    const pad = el('div', {class:'pad'}); pad.append(el('pre', {}, meta.preview.text));
    if (meta.preview.truncated) pad.append(el('p', {class:'note'}, 'Showing the beginning. Download the file for all of it.'));
    box.append(pad);
  } else {
    const c = el('div', {class:'center'});
    c.append(el('p', {}, 'This file type is not previewed here.'));
    const a = el('a', {class:'btn', href: meta.download_url}, 'Download ' + meta.filename); c.append(a);
    box.append(c);
  }
  poll();
}

function status(msg){ $('status').textContent = msg || ''; }

async function post(url, body, method){
  const r = await fetch(url, {method: method || 'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || 'That did not work.');
  return data;
}

function openCompany(){
  const c = meta.series.company; const p = $('share'); p.replaceChildren();
  p.append(el('h2', {}, 'Who can view this report'));
  const opt = (value, label, help, enabled, checked) => {
    const l = el('label', enabled ? {} : {class:'off'});
    const i = el('input', {type:'radio', name:'aud', value}); if (!enabled) i.disabled = true;
    if (checked) i.checked = true;
    const d = el('div'); d.append(el('div', {}, label)); if (help) d.append(el('small', {}, help));
    l.append(i, d); p.append(l); return d; };
  const can = c.available && c.allowed;
  opt('owner', 'Only you', '', true, !c.on);
  const company = opt('company', 'Anyone at ' + c.domains + ' with the link',
      can ? 'They sign in with their ' + c.domains + ' account.'
          : (!c.available ? c.reason : 'Needs \u201cShare reports with the company domain\u201d. Ask an admin.'),
      can, c.on);
  const why = 'They can\'t use the agent or see other reports. The link always opens the newest copy; '
    + 'anyone who downloaded a copy keeps it.';
  company.firstChild.append(' ', el('span', {class:'q', tabindex:'0', role:'img', title: why, 'aria-label': why}, '?'));
  const area = el('div'); p.append(area);
  const draw = () => { area.replaceChildren();
    if (meta.series.company.on && meta.series.company.url) {
      const box = el('input', {class:'linkbox', readonly:'readonly', 'aria-label':'Company link'}); box.value = meta.series.company.url;
      const copy = el('button', {}, 'Copy link');
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(box.value); status('Link copied.'); } catch (e) { box.select(); } });
      const r = el('div', {class:'row'}); r.append(box, copy); area.append(r);
    } };
  draw();
  const row = el('div', {class:'row'});
  const save = el('button', {class:'primary'}, 'Save sharing');
  const close = el('button', {}, 'Close');
  row.append(save, close); p.append(row, el('div', {id:'status', role:'status'}));
  save.addEventListener('click', async () => {
    const v = (p.querySelector('input[name=aud]:checked') || {}).value;
    try { const r = await post(cfg.api + '/company', {action: v === 'company' ? 'on' : 'off'});
      meta.series.company.on = !!r.on; meta.series.company.url = r.url || null; draw(); status('Saved.'); }
    catch (e) { status(e.message); } });
  close.addEventListener('click', () => p.classList.remove('open'));
  p.classList.add('open');
}

function openShare(){
  if (meta.series && meta.series.company) return openCompany();
  const s = meta.sharing; const p = $('share'); p.replaceChildren();
  p.append(el('h2', {}, 'Who can view this artifact'));
  const opt = (value, label, help, enabled) => {
    const l = el('label', enabled ? {} : {class:'off'});
    const i = el('input', {type:'radio', name:'aud', value}); if (!enabled) i.disabled = true;
    if ((s.audience === value)) i.checked = true;
    const d = el('div'); d.append(el('div', {}, label)); if (help) d.append(el('small', {}, help));
    l.append(i, d); p.append(l); return i; };
  opt('owner', 'Only you', '', true);
  opt('people', 'Specific people', 'People who can use this agent.', true);
  opt('hub', 'Anyone with access to this agent', 'Everyone who can currently use it.', true);
  opt('link', 'Anyone with the link', s.can_public_link ? 'No sign-in. Anyone who has the link can view it.' : (s.public_link_hint || 'You do not have permission to create public links here.'), s.can_public_link);
  const peopleBox = el('div');
  peopleBox.append(el('label', {for:'share-people', class:'field'}, 'People who can view it'));
  const people = el('textarea', {id:'share-people', 'aria-describedby':'share-people-hint', placeholder:'name@company.com'});
  people.value = s.people.map(x => x.principal).join('\n');
  peopleBox.append(people, el('small', {id:'share-people-hint'}, 'One email address per line. Each must be able to use this agent.'));
  p.append(peopleBox);
  const linkArea = el('div'); p.append(linkArea);
  const save = el('button', {class:'primary'}, 'Save');
  // One flow for public links: choosing "Anyone with the link" and pressing the
  // main button creates the link. An existing link can be replaced or turned off.
  const sync = () => { const v = (p.querySelector('input[name=aud]:checked') || {}).value;
    peopleBox.hidden = v !== 'people'; linkArea.hidden = v !== 'link';
    save.textContent = (v === 'link' && !(meta.sharing && meta.sharing.link)) ? 'Create public link' : 'Save';
    create.hidden = !(meta.sharing && meta.sharing.link); };
  p.querySelectorAll('input[name=aud]').forEach(i => i.addEventListener('change', sync));
  // Public link controls.
  linkArea.append(el('div', {class:'warn'}, 'Anyone who has this link can open the artifact without signing in, until it expires or you turn it off. Share it only with people who should see it.'));
  if (s.link) linkArea.append(el('p', {class:'note'}, 'A link is active until ' + new Date(s.link.expires * 1000).toLocaleString() + '. Links are shown once. Creating a new link turns the old one off.'));
  const days = el('select'); for (const d of [1, 7, 30, 90]) { const o = el('option', {value: d}, d + (d === 1 ? ' day' : ' days')); if (d === s.default_days) o.selected = true; days.append(o); }
  const lrow = el('div', {class:'row'});
  const create = el('button', {}, 'Create new link');
  const off = el('button', {class:'danger'}, 'Turn off link'); off.hidden = !s.link;
  const out = el('input', {class:'linkbox', readonly:'readonly'}); out.hidden = true;
  lrow.append(el('span', {}, 'Expires after'), days, create, off); linkArea.append(lrow, out);
  const makeLink = async () => {
    try { const r = await post(cfg.api + '/link', {action:'create', days: Number(days.value)});
      out.value = r.url; out.hidden = false; out.select(); status('Link created. Copy it now; it is not shown again.');
      meta = await (await fetch(cfg.api, {credentials:'same-origin'})).json(); off.hidden = false; sync();
    } catch (e) { status(e.message); } };
  create.addEventListener('click', makeLink);
  off.addEventListener('click', async () => {
    try { await post(cfg.api + '/link', {action:'revoke'}); status('The link no longer works.');
      meta = await (await fetch(cfg.api, {credentials:'same-origin'})).json(); openShare(); } catch (e) { status(e.message); } });
  const row = el('div', {class:'row'});
  const close = el('button', {}, 'Close');
  const del = el('button', {class:'danger'}, 'Delete artifact');
  row.append(save, close, del); p.append(row, el('div', {id:'status', role:'status'}));
  save.addEventListener('click', async () => {
    const v = (p.querySelector('input[name=aud]:checked') || {}).value;
    if (v === 'link') {
      if (meta.sharing && meta.sharing.link) { status('The public link is on. Use Create new link to replace it, or Turn off link.'); return; }
      return makeLink(); }
    const list = people.value.split('\n').map(x => x.trim()).filter(Boolean).map(x => ({kind:'user', principal: x}));
    try { await post(cfg.api + '/audience', {audience: v, people: v === 'people' ? list : []});
      meta = await (await fetch(cfg.api, {credentials:'same-origin'})).json(); status('Saved.'); }
    catch (e) { status(e.message); } });
  close.addEventListener('click', () => p.classList.remove('open'));
  del.addEventListener('click', async () => {
    if (!window.confirm('Delete this artifact for everyone? This cannot be undone.')) return;
    try { await post(cfg.api, undefined, 'DELETE'); p.classList.remove('open'); fail('The artifact was deleted.'); $('download').hidden = true; $('sharebtn').hidden = true; }
    catch (e) { status(e.message); } });
  sync(); p.classList.add('open');
}

$('sharebtn').addEventListener('click', () => { if (meta && (meta.sharing || (meta.series && meta.series.company))) openShare(); });
$('updatebtn').addEventListener('click', updateNow);
$('showbtn').addEventListener('click', showNewest);
load();
})();
"""


def shell(*, mode: str, api: str | None, nonce: str) -> str:
    cfg = json.dumps({"mode": mode, "api": api}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Artifact</title>
<style nonce="{nonce}">{_CSS}</style></head>
<body>
<header><div class="t"><h1 id="title">Loading…</h1><div class="m" id="meta"></div></div>
<button id="updatebtn" hidden>Update now</button>
<button id="sharebtn" hidden>Share</button>
<a id="download" class="btn" hidden>Download</a></header>
<div id="notice" role="status" hidden><span id="notice-text"></span><button id="showbtn">Show it</button></div>
<main id="content"><div class="center">Loading…</div></main>
<div id="share" role="dialog" aria-label="Sharing"></div>
<script type="application/json" id="hz-cfg">{cfg}</script>
<script nonce="{nonce}">{_JS}</script>
</body></html>"""


def message(*, title: str, body: str, nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style nonce="{nonce}">{_CSS}</style></head>
<body><main><div class="center"><h1>{html.escape(title)}</h1>
<p>{html.escape(body)}</p></div></main></body></html>"""


_SMALL_JS = r"""
(function(){
const cfg = JSON.parse(document.getElementById('hz-cfg').textContent);
const btn = (id) => document.getElementById(id);
btn('switch').addEventListener('click', async () => {
  try { await fetch('/api/auth/logout', {method:'POST', credentials:'same-origin'}); } catch (e) {}
  location.href = '/auth?redirect=' + encodeURIComponent(cfg.back);
});
})();
"""


def unavailable(*, email: str | None, back: str, nonce: str) -> str:
    """One page for every company-link refusal, so it can't test links or emails."""
    who = (f"The link may be turned off, or it isn't shared with {html.escape(email)}."
           if email else "The link may be turned off or incomplete.")
    cfg = json.dumps({"kind": "unavailable", "back": back}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>This report isn't available</title><style nonce="{nonce}">{_CSS}</style></head>
<body><main><div class="center"><h1>This report isn't available</h1><p>{who}</p>
<button id="switch">Sign in with another account</button></div></main>
<script type="application/json" id="hz-cfg">{cfg}</script>
<script nonce="{nonce}">{_SMALL_JS}</script></body></html>"""

