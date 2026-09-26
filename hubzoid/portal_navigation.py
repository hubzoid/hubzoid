"""Small edge-owned enhancement; never patches installed OWUI bundles.

Open WebUI is a single-page app: the page loads once and login/logout happen via
in-app navigation without a full reload. So a one-shot check on load would show the
link before an admin has signed in and leave it visible after they sign out. The
script therefore RE-EVALUATES the session — adding the link when `/portal/api/me`
authorizes and removing it otherwise — on load, on history navigation, when the tab
regains focus, and on a slow interval. It is idempotent and never throws into chat.

With `HUBZOID_HIDE_OWUI_USERS` on (`script(hide_users=True)`), the same script
also sends in-app navigation to Open WebUI's user list (`/admin/users/overview`)
to the Console's People screen and lands the Users section (`/admin/users`, where
the Admin Panel opens) on Groups, as the edge does for a full page load. It
hides the user-list tab. Groups, Evaluations and Functions stay reachable.

For every signed-in person it also explains an empty chat: a blocked account,
or an account with no agent yet, sees a notice instead of an unexplained
"Select a model"."""
SCRIPT = r'''
(() => {
  const HIDE_USERS = false;
  const ID = 'hubzoid-manage-access';
  let authorized = false, revision = 0, queued = false, observedSidebar;
  const style = document.createElement('style');
  style.textContent = `
    :has(> #${ID}) { flex-direction: column; }
    #${ID} { display:flex; align-items:center; gap:12px; flex-shrink:0;
      box-sizing:border-box; width:100%; min-height:36px; padding:8px 10px;
      margin-bottom:4px; border-radius:8px; color:inherit; text-decoration:none;
      font:inherit; font-size:13px; }
    #${ID}:hover { background:rgba(128,128,128,.12); }
    #${ID}:focus-visible { outline:2px solid #E5572A; outline-offset:-2px; }
    #${ID} svg { width:18px; height:18px; flex-shrink:0; }
    #${ID}[data-compact] { justify-content:center; padding:8px 0; }
    #${ID}[data-compact] span { display:none; }
  `;
  document.head.appendChild(style);
  if (HIDE_USERS) {
    // Accounts are managed in the Hubzoid Console. Groups stay in Open WebUI.
    const hidden = document.createElement('style');
    hidden.textContent = 'a[href="/admin/users/overview"]{display:none !important;}';
    document.head.appendChild(hidden);
  }
  function usersPage() {
    if (!HIDE_USERS) return;
    const p = location.pathname.replace(/\/+$/, '');
    if (p === '/admin/users/overview') location.replace('/portal/#/people');
    else if (p === '/admin/users') location.replace('/admin/users/groups');
  }
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
      link.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 3v18M9 9h12"/></svg><span>Admin Console</span>';
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
