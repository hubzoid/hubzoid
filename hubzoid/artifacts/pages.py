# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""The report viewer page: a thin toolbar (title, time, Share, Download) with the
content below. One self-contained page, no external assets, rendered with DOM
text nodes only (never innerHTML with report data). A strict CSP with a
per-response nonce covers its one script and one stylesheet."""
from __future__ import annotations

import html
import json

_CSS = """
:root{--bg:#fbfaf7;--panel:#ffffff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e4e1d9;
--accent:#2f5d8a;--accent-ink:#fff;--warn-bg:#fff4e0;--warn-ink:#7a4b00;--danger:#a3322a;
--font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--panel:#1f1f1d;--ink:#ecebe6;
--muted:#a3a19a;--line:#34332f;--accent:#7fb0e0;--accent-ink:#0f1720;--warn-bg:#3a2d12;
--warn-ink:#f3cf8a;--danger:#ef8a80}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:14px/1.45 var(--font);display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid var(--line);
background:var(--panel);min-height:52px}
header .t{flex:1;min-width:0}
header h1{font-size:15px;font-weight:600;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header .m{color:var(--muted);font-size:12px}
button,.btn{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);
padding:6px 12px;border-radius:6px;cursor:pointer;text-decoration:none;white-space:nowrap}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button.danger{color:var(--danger)}
button:disabled{opacity:.5;cursor:default}
main{flex:1;min-height:0;display:flex}
main>iframe{flex:1;border:0;width:100%;background:#fff}
main>.pad{flex:1;overflow:auto;padding:16px}
img.preview{max-width:100%;height:auto;display:block;margin:0 auto;background:#fff}
pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
table{border-collapse:collapse;font-size:13px}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
tr:first-child td{font-weight:600;background:var(--panel)}
.note{color:var(--muted);margin:16px 0}
.center{margin:auto;max-width:520px;padding:32px 16px;text-align:center}
#share{position:fixed;top:60px;right:16px;width:min(420px,calc(100vw - 32px));background:var(--panel);
border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:16px;display:none;z-index:5}
#share.open{display:block}
#share h2{font-size:14px;margin:0 0 10px}
#share label{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
#share label.off{color:var(--muted)}
#share small{display:block;color:var(--muted)}
#share label.field{display:block;margin:8px 0 4px;font-weight:600}
#share textarea{width:100%;min-height:70px;font:inherit;border:1px solid var(--line);border-radius:6px;
padding:6px;background:var(--bg);color:var(--ink)}
#share select{font:inherit}
.warn{background:var(--warn-bg);color:var(--warn-ink);border-radius:6px;padding:8px;margin:8px 0;font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.linkbox{width:100%;font:12px ui-monospace,Menlo,monospace;padding:6px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--ink)}
#status{color:var(--muted);font-size:12px;min-height:1em;margin-top:6px}
@media print{header,#share{display:none}}
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
  const h = heading || (cfg.mode === 'public' ? 'Link not available' : 'Report not available');
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
  if (!r.ok) return fail(data.detail || 'This report is not available.');
  meta = data; render();
}

function render(){
  document.title = meta.title;
  $('title').textContent = meta.title;
  $('meta').textContent = new Date(meta.created * 1000).toLocaleString() + ' · ' + meta.filename;
  const dl = $('download'); dl.href = meta.download_url; dl.hidden = false;
  if (meta.role === 'owner') $('sharebtn').hidden = false;
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
}

function status(msg){ $('status').textContent = msg || ''; }

async function post(url, body, method){
  const r = await fetch(url, {method: method || 'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || 'That did not work.');
  return data;
}

function openShare(){
  const s = meta.sharing; const p = $('share'); p.replaceChildren();
  p.append(el('h2', {}, 'Who can view this report'));
  const opt = (value, label, help, enabled) => {
    const l = el('label', enabled ? {} : {class:'off'});
    const i = el('input', {type:'radio', name:'aud', value}); if (!enabled) i.disabled = true;
    if ((s.audience === value)) i.checked = true;
    const d = el('div'); d.append(el('div', {}, label)); if (help) d.append(el('small', {}, help));
    l.append(i, d); p.append(l); return i; };
  opt('owner', 'Only you', '', true);
  opt('people', 'Specific people or groups', s.hub_managed ? 'People who can use this agent.' : s.unmanaged_note, s.hub_managed);
  opt('hub', 'Anyone with access to this agent', s.hub_managed ? 'Everyone who can currently use it.' : s.unmanaged_note, s.hub_managed);
  opt('link', 'Anyone with the link', s.can_public_link ? 'No sign-in. Anyone who has the link can view it.' : (s.public_link_hint || 'You do not have permission to create public links here.'), s.can_public_link);
  const peopleBox = el('div');
  peopleBox.append(el('label', {for:'share-people', class:'field'}, 'People or groups who can view it'));
  const people = el('textarea', {id:'share-people', 'aria-describedby':'share-people-hint', placeholder:'name@company.com'});
  people.value = s.people.map(x => x.kind === 'group' ? 'group:' + x.principal : x.principal).join('\n');
  peopleBox.append(people, el('small', {id:'share-people-hint'}, 'One per line: an email address, or group:name for a group. Each must be able to use this agent.'));
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
  linkArea.append(el('div', {class:'warn'}, 'Anyone who has this link can open the report without signing in, until it expires or you turn it off. Share it only with people who should see this report.'));
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
  const del = el('button', {class:'danger'}, 'Delete report');
  row.append(save, close, del); p.append(row, el('div', {id:'status', role:'status'}));
  save.addEventListener('click', async () => {
    const v = (p.querySelector('input[name=aud]:checked') || {}).value;
    if (v === 'link') {
      if (meta.sharing && meta.sharing.link) { status('The public link is on. Use Create new link to replace it, or Turn off link.'); return; }
      return makeLink(); }
    const list = people.value.split('\n').map(x => x.trim()).filter(Boolean).map(x =>
      x.toLowerCase().startsWith('group:') ? {kind:'group', principal: x.slice(6).trim()} : {kind:'user', principal: x});
    try { await post(cfg.api + '/audience', {audience: v, people: v === 'people' ? list : []});
      meta = await (await fetch(cfg.api, {credentials:'same-origin'})).json(); status('Saved.'); }
    catch (e) { status(e.message); } });
  close.addEventListener('click', () => p.classList.remove('open'));
  del.addEventListener('click', async () => {
    if (!window.confirm('Delete this report for everyone? This cannot be undone.')) return;
    try { await post(cfg.api, undefined, 'DELETE'); p.classList.remove('open'); fail('The report was deleted.'); $('download').hidden = true; $('sharebtn').hidden = true; }
    catch (e) { status(e.message); } });
  sync(); p.classList.add('open');
}

$('sharebtn').addEventListener('click', () => { if (meta && meta.sharing) openShare(); });
load();
})();
"""


def shell(*, mode: str, api: str | None, nonce: str) -> str:
    cfg = json.dumps({"mode": mode, "api": api}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Report</title>
<style nonce="{nonce}">{_CSS}</style></head>
<body>
<header><div class="t"><h1 id="title">Loading…</h1><div class="m" id="meta"></div></div>
<button id="sharebtn" hidden>Share</button>
<a id="download" class="btn" hidden>Download</a></header>
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
<body><main><div class="center"><h1 style="font-size:18px">{html.escape(title)}</h1>
<p>{html.escape(body)}</p></div></main></body></html>"""
