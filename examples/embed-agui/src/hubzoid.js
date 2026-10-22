// What both pages share: the person's pass from your app's server, and
// Hubzoid's history routes (AG-UI has no history standard of its own).

export async function me() {
  const r = await fetch("/me");
  return r.json();
}

// A fresh pass before each request: your app's server signs it for whoever is
// signed in to your app. Hubzoid never makes or stores these.
export async function getPass() {
  const r = await fetch("/hubzoid/pass", { cache: "no-store" });
  if (!r.ok) throw new Error("Sign in to the app first.");
  return r.text();
}

export async function hubzoid(base, path, init = {}) {
  const pass = await getPass();
  const r = await fetch(`${base}${path}`, {
    ...init,
    credentials: "omit",                       // the pass says who it is; no cookies
    headers: { ...(init.headers || {}), Authorization: `Bearer ${pass}` },
  });
  if (r.status === 204) return null;
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
