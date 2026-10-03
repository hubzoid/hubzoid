# Hubzoid for Claude Code

Set `HUBZOID_MCP_URL` to your hub's public MCP URL, then start Claude Code:

```sh
export HUBZOID_MCP_URL=https://your-domain.example/mcp
claude --plugin-dir /absolute/path/to/plugins/claude/hubzoid
```

Use `/mcp` to authenticate. Sign in through Open WebUI, then approve Hubzoid's
consent screen. The plugin contains no credentials; Claude stores its own OAuth
credentials. The hub sends its instructions and available tools over MCP.

This is a local plugin template, not a published marketplace listing. For the
simplest connection use `claude mcp add` instead, as documented in
[the setup guide](../../../docs/mcp-oauth-claude.md). Avoid adding both configurations
for the same hub: that would expose duplicate tools.
