# Example: Hubzoid chat inside your app

A stand-in company app ("Acme ERP") that embeds a Hubzoid agent over AG-UI,
with two pages: one built with [assistant-ui](https://www.assistant-ui.com) and
one with [CopilotKit](https://www.copilotkit.ai). It uses only their packages,
`@ag-ui/client` and `jose`. See [docs/embedding.md](../../docs/embedding.md).

What it shows: a person signed in to the app chats as themselves, sees tool
calls, approves an action, reopens past chats and rejoins a reply after a
reload, stops or deletes a chat, and sends the page they are on. The agent's
tools reach the app's own data as that person: `/api/orders` (a hub tool) and
an MCP server at `/mcp` (a connector) each answer only with that person's
orders, and `/source` lists every call they received. `&mode=jwt` at sign-in
uses mode 2 (needs `IDP_PRIVATE_KEY_FILE` and `IDP_PUBLIC_KEY_FILE`).

## Run it

1. Register the app: `hubzoid embed secret erpnext --hub <your hub> --origin
   http://127.0.0.1:5180 --token-to app_lookup`, and paste the entry it prints
   under `apps:` in `hubzoid.yaml`.
2. Build and start:

   ```bash
   npm install
   npm run build
   HUBZOID_URL=http://127.0.0.1:3780/b/finance \
   HUBZOID_CLIENT_ID=erpnext HUBZOID_CLIENT_SECRET=... \
   HUBZOID_AGENT=finance PORT=5180 COPILOTKIT_TELEMETRY_DISABLED=true npm start
   ```

   Optional: `IDP_PRIVATE_KEY_FILE` and `IDP_PUBLIC_KEY_FILE` (an RSA key pair)
   make this server stand in for your identity provider, for mode 2;
   `OTHER_SITE_PORT=5190` serves a page on another address that tries a copied
   pass and is refused.
3. Open `http://127.0.0.1:5180/login?user=alice` (the demo sign-in stands in for
   your app's own; add `&mode=jwt` for mode 2), then try the assistant-ui page,
   `/copilotkit.html` and `/source`.
4. For the connector route, add `http://127.0.0.1:5180/mcp` in Settings ›
   Agents › Connectors › Custom server with "No sign-in", and list
   `connector:<its id>` in the app's `access.tools` and `token_to`.

## Files

| File | What |
|---|---|
| `server.mjs` | Your app's server: its sign-in, `/hubzoid/pass`, its orders API and MCP server, the CopilotKit runtime |
| `src/assistant-ui.jsx` | assistant-ui page: AG-UI agent, thread list, approvals, rejoin |
| `src/copilotkit.jsx` | CopilotKit page: its chat and approvals through the runtime |
| `src/hubzoid.js` | Fetch a pass; call Hubzoid's history routes |
