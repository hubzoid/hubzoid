"""Small edge-owned enhancement; never patches installed OWUI bundles.

Open WebUI is a single-page app: the page loads once and login/logout happen via
in-app navigation without a full reload. So a one-shot check on load would show the
link before an admin has signed in and leave it visible after they sign out. The
script therefore RE-EVALUATES the session — adding the link when `/portal/api/me`
authorizes and removing it otherwise — on load, on history navigation, when the tab
regains focus, and on a slow interval. It is idempotent and never throws into chat."""
SCRIPT = r'''
(() => {
  const ID = 'hubzoid-manage-access';
  async function sync() {
    let ok = false;
    try {
      const r = await fetch('/portal/api/me', {credentials: 'same-origin'});
      ok = r.ok;
    } catch (_) { ok = false; }  // chat stays usable if administration is unreachable
    const existing = document.getElementById(ID);
    if (!ok) { if (existing) existing.remove(); return; }   // signed out / not an admin
    if (existing || !document.body) return;                 // already shown
    const link = document.createElement('a');
    link.id = ID;
    link.href = '/portal/';
    link.textContent = 'Manage agent access ↗';
    link.title = 'Manage agent permissions and view workflow runs. Accounts remain in Open WebUI.';
    Object.assign(link.style, {position:'fixed',bottom:'16px',right:'16px',zIndex:'9999',
      padding:'10px 14px',background:'#fff',color:'#222',border:'1px solid #ddd',
      borderRadius:'8px',font:'13px system-ui',boxShadow:'0 2px 8px #0001'});
    document.body.appendChild(link);
  }
  sync();
  addEventListener('popstate', sync);
  addEventListener('visibilitychange', () => { if (!document.hidden) sync(); });
  setInterval(sync, 15000);
})();
'''


def inject(body: bytes) -> bytes:
    tag = b'<script src="/hubzoid-portal-navigation.js" defer></script>'
    return body.replace(b'</body>', tag + b'</body>', 1)
