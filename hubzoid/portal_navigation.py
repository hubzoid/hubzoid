"""Small edge-owned enhancement; never patches installed OWUI bundles."""
SCRIPT = r'''
(async () => {
  try {
    const response = await fetch('/portal/api/me', {credentials: 'same-origin'});
    if (!response.ok || document.getElementById('hubzoid-manage-access')) return;
    const link = document.createElement('a');
    link.id = 'hubzoid-manage-access';
    link.href = '/portal/';
    link.textContent = 'Manage agent access ↗';
    link.title = 'Manage agent permissions and view workflow runs. Accounts remain in Open WebUI.';
    Object.assign(link.style, {position:'fixed',bottom:'16px',right:'16px',zIndex:'9999',
      padding:'10px 14px',background:'#fff',color:'#222',border:'1px solid #ddd',
      borderRadius:'8px',font:'13px system-ui',boxShadow:'0 2px 8px #0001'});
    document.body.appendChild(link);
  } catch (_) { /* Chat remains usable if administration is unavailable. */ }
})();
'''


def inject(body: bytes) -> bytes:
    tag = b'<script src="/hubzoid-portal-navigation.js" defer></script>'
    return body.replace(b'</body>', tag + b'</body>', 1)
