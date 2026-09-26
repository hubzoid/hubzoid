"""Small edge-owned enhancement; never patches installed OWUI bundles.

Open WebUI is a single-page app: the page loads once and login/logout happen via
in-app navigation without a full reload. So a one-shot check on load would show the
link before an admin has signed in and leave it visible after they sign out. The
script therefore RE-EVALUATES the session — adding the link when `/portal/api/me`
authorizes and removing it otherwise — on load, on history navigation, when the tab
regains focus, and on a slow interval. It is idempotent and never throws into chat.
The link carries Lucide's `shield-cog` icon (ISC, as in the Console) at Open WebUI's
sidebar size and stroke, and keeps its name and a tooltip when the sidebar is
collapsed.

With `HUBZOID_HIDE_OWUI_USERS` on (`script(hide_users=True)`), Open WebUI's user
list is out of reach in the browser, while the rest of its Admin Panel stays:
  * the Admin Panel entry in the user menu opens Settings > Integrations over the
    Groups page (`ADMIN_LANDING`, the same page the edge sends `/admin` to), and
    the Admin Panel's Users tab opens Groups. Both links are handled when clicked,
    before Open WebUI routes to its user list, so the list never shows;
  * any other in-app route to `/admin`, `/admin/users` or the user list (Open
    WebUI's own redirects) is hidden at once and replaced by Groups;
  * the user-list (Overview) tab is hidden.
Groups, Evaluations, Functions and Settings stay reachable, and only Open WebUI
administrators see the Admin Panel at all. Hiding links is not access control:
the edge refuses account-admin writes. A typed `/admin/users/overview` opens the
Console's People screen (edge).

For every signed-in person it also explains an empty chat: a blocked account,
or an account with no agent yet, sees a notice instead of an unexplained
"Select a model"."""
SCRIPT = r'''
(() => {
  const HIDE_USERS = false;
  const ID = 'hubzoid-manage-access';
  // Where Open WebUI's Admin Panel opens while its user list is hidden.
  const ADMIN_LANDING = '/admin/users/groups?settings=admin%3Aintegrations';
  const GROUPS = '/admin/users/groups';
  let authorized = false, revision = 0, queued = false, observedSidebar;
  const style = document.createElement('style');
  // Sized like Open WebUI's own sidebar items: a 16px icon with a 1.5 stroke,
  // 13px text, 8px gap; a 32px square when the sidebar is collapsed.
  style.textContent = `
    :has(> #${ID}) { flex-direction: column; }
    #${ID} { position:relative; display:flex; align-items:center; gap:8px; flex-shrink:0;
      box-sizing:border-box; width:100%; min-height:32px; padding:6px 8px;
      margin-bottom:2px; border-radius:12px; color:inherit; text-decoration:none;
      font:inherit; font-size:13px; line-height:20px; }
    #${ID}:hover { background:rgba(128,128,128,.12); }
    #${ID}:focus-visible { outline:2px solid #E5572A; outline-offset:-2px; }
    #${ID} svg { width:16px; height:16px; flex-shrink:0; }
    #${ID}[data-compact] { justify-content:center; width:32px; height:32px; min-height:32px;
      padding:0; margin:0 auto 2px; border-radius:8px; }
    #${ID}[data-compact] span { display:none; }
    #${ID}[data-compact]:focus-visible::after { content:'Admin Console'; position:absolute;
      left:calc(100% + 8px); top:50%; transform:translateY(-50%); white-space:nowrap;
      padding:4px 8px; border-radius:6px; background:#171717; color:#fff; font-size:12px;
      line-height:16px; pointer-events:none; z-index:50; }
  `;
  document.head.appendChild(style);
  if (HIDE_USERS) {
    // Accounts are managed in the Hubzoid Console. Groups stay in Open WebUI.
    // While an in-app route heads for the user list, keep it hidden until Groups shows.
    const hidden = document.createElement('style');
    hidden.textContent = 'a[href="/admin/users/overview"]{display:none !important;}' +
      'html[data-hz-leaving] :has(> #users-tabs-container){visibility:hidden !important;}';
    document.head.appendChild(hidden);
  }
  // In-app navigation through Open WebUI's own router (SvelteKit handles a click
  // on a same-origin link inside its root), so the page does not reload.
  function go(url, replace) {
    const root = document.querySelector('body > div[style*="contents"]');
    if (!root) { replace ? location.replace(url) : location.assign(url); return; }
    const a = document.createElement('a');
    a.href = url;
    a.hidden = true;
    if (replace) a.setAttribute('data-sveltekit-replacestate', '');
    root.appendChild(a);
    a.click();
    a.remove();
  }
  const USERS_ROUTES = ['/admin', '/admin/users', '/admin/users/overview'];
  const path = (url) => url.pathname.replace(/\/+$/, '') || '/';
  let leaving = 0, left = 0;
  function usersPage() {
    if (!HIDE_USERS) return;
    const away = USERS_ROUTES.includes(path(location));
    const root = document.documentElement;
    if (root.hasAttribute('data-hz-leaving') !== away) root.toggleAttribute('data-hz-leaving', away);
    // After Open WebUI's own redirect has started, so this navigation is the last
    // one; once, not again for every change the page makes while it runs.
    if (away && !leaving && Date.now() - left > 1500) leaving = setTimeout(() => {
      leaving = 0;
      if (!USERS_ROUTES.includes(path(location))) return;
      left = Date.now();
      go(GROUPS, true);
    }, 0);
  }
  // The Admin Panel entry (user menu) and the Admin Panel's Users tab both link to
  // /admin, which Open WebUI routes on to its user list. Take them to an allowed
  // page instead, before any of that renders. Open WebUI's own handler still runs
  // first (it closes the menu); this navigation starts after it and wins.
  addEventListener('click', (e) => {
    if (!HIDE_USERS || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
    if (!a) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || !USERS_ROUTES.includes(path(url))) return;
    const to = path(url) === '/admin' && !a.closest('nav') ? ADMIN_LANDING : GROUPS;
    e.preventDefault();
    let done = false;
    const once = () => { if (!done) { done = true; go(to); } };
    a.addEventListener('click', once, {once: true});
    setTimeout(once, 0);
  }, true);
  const NOTE = 'hubzoid-access-notice';
  function notice(text) {
    let n = document.getElementById(NOTE);
    if (!text) { n?.remove(); return; }
    if (!n) {
      n = document.createElement('div');
      n.id = NOTE;
      n.setAttribute('role', 'status');
      // Below the chat header and click-through, so the menu (and sign-out) stay usable.
      n.style.cssText = 'position:fixed;top:56px;left:50%;transform:translateX(-50%);z-index:9999;pointer-events:none;' +
        'max-width:min(560px,calc(100vw - 32px));padding:10px 14px;border-radius:8px;font:14px/1.4 system-ui,sans-serif;' +
        'background:#fff4e0;color:#5c3900;border:1px solid #f0c987;box-shadow:0 2px 8px rgba(0,0,0,.12)';
      document.body.appendChild(n);
    }
    if (n.textContent !== text) n.textContent = text;
  }
  async function access() {
    if (location.pathname.startsWith('/auth')) { notice(''); return; }
    try {
      const r = await fetch('/portal/api/chat-access', {credentials: 'same-origin'});
      if (!r.ok) { notice(''); return; }
      const a = await r.json();
      if (a.blocked) notice('Your access to agents has been blocked by an administrator. Contact your administrator if you think this is a mistake.');
      else if (a.allowed === 0) notice('You do not have access to any agent yet. Ask an administrator to give you access.');
      else notice('');
    } catch (_) { /* never break chat */ }
  }
  const resize = new ResizeObserver(place);
  function place() {
    queued = false;
    const existing = document.getElementById(ID);
    const sidebar = document.getElementById('sidebar');
    const profile = sidebar?.querySelector('button[aria-label="User menu"]');
    if (!authorized || !profile) { existing?.remove(); return; }
    // Both OWUI layouts wrap the profile button in a menu-trigger span.
    // Insert a sibling, never an interactive link inside the profile button.
    const anchor = profile.closest('[role="button"][aria-haspopup]') || profile;
    const parent = anchor.parentElement;
    if (!parent) return;
    let link = existing;
    if (!link) {
      link = document.createElement('a');
      link.id = ID;
      link.href = '/portal/';
      link.setAttribute('aria-label', 'Hubzoid Admin Console');
      link.title = 'Hubzoid Admin Console \u2014 manage access and view workflow runs';
      // Lucide shield-cog (ISC).
      link.innerHTML = '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>'
        + '<circle cx="12.077" cy="11.695" r="3"/>'
        + '<path d="M10.929 14.467l-.383.924M10.929 8.923L10.546 8M13.225 8.923L13.608 8M13.607 15.391l-.382-.924M14.849 10.547l.923-.383M14.849 12.843l.923.383M9.305 10.547l-.923-.383M9.305 12.843l-.923.383"/>'
        + '</svg><span>Admin Console</span>';
    }
    if (link.parentElement !== parent || link.nextElementSibling !== anchor)
      parent.insertBefore(link, anchor);
    const compact = sidebar.getBoundingClientRect().width < 100;
    if (link.hasAttribute('data-compact') !== compact)
      link.toggleAttribute('data-compact', compact);
    if (observedSidebar !== sidebar) {
      resize.disconnect();
      resize.observe(sidebar);
      observedSidebar = sidebar;
    }
  }
  function schedulePlace() {
    if (!queued) { queued = true; requestAnimationFrame(place); }
  }
  async function sync() {
    const current = ++revision;
    let ok = false;
    try {
      const r = await fetch('/portal/api/me?brief=1', {credentials: 'same-origin'});
      ok = r.ok;
    } catch (_) { ok = false; }
    if (current !== revision) return;
    authorized = ok;
    place();
    access();
  }
  // OWUI replaces the sidebar when expanding/collapsing and after SPA login.
  // Observe layout changes without issuing extra authentication requests,
  // except on an in-app route change (sign-in lands on the chat that way).
  let lastPath = location.pathname;
  new MutationObserver(() => {
    schedulePlace(); usersPage();
    if (location.pathname !== lastPath) { lastPath = location.pathname; sync(); }
  }).observe(document.body, {childList:true, subtree:true});
  usersPage();
  sync();
  addEventListener('popstate', () => { usersPage(); sync(); });
  addEventListener('visibilitychange', () => { if (!document.hidden) sync(); });
  setInterval(sync, 15000);
})();
'''


def script(hide_users: bool = False) -> str:
    """The navigation script, with the Users-page redirect when enabled."""
    if not hide_users:
        return SCRIPT
    return SCRIPT.replace("const HIDE_USERS = false;", "const HIDE_USERS = true;", 1)


def inject(body: bytes) -> bytes:
    tag = b'<script src="/hubzoid-portal-navigation.js" defer></script>'
    return body.replace(b'</body>', tag + b'</body>', 1)
