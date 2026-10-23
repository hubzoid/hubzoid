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

1. Register the app in your `hubzoid.yaml` (`hubzoid embed secret erpnext`), with
   `origins: [http://127.0.0.1:5180]`.
2. Build and start:

   ```bash
   npm install
   npm run build
   HUBZOID_URL=http://127.0.0.1:3780/b/finance \
   HUBZOID_CLIENT_ID=erpnext HUBZOID_CLIENT_SECRET=... \
   HUBZOID_AGENT=finance PORT=5180 COPILOTKIT_TELEMETRY_DISABLED=true npm start
   ```

3. Open `http://127.0.0.1:5180/login?user=alice` (the demo sign-in stands in for
   your app's own), then try the assistant-ui page and `/copilotkit.html`.

## Files

| File | What |
|---|---|
| `server.mjs` | Your app's server: its sign-in, `/hubzoid/pass`, its orders API and MCP server, the CopilotKit runtime |
| `src/assistant-ui.jsx` | assistant-ui page: AG-UI agent, thread list, approvals, rejoin |
| `src/copilotkit.jsx` | CopilotKit page: its chat and approvals through the runtime |
| `src/hubzoid.js` | Fetch a pass; call Hubzoid's history routes |
