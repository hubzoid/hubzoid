"""Cards: a tool's structured result shown as an MCP App.

A hub binds one of its tools to a card in ``cards/<id>.yaml`` and turns cards
on in ``mcp-apps.yaml`` (``hubzoid.apps.config``). When the bound tool returns,
``hubzoid.apps.cards.wrap`` checks the result, stores an instance and gives the
model readable text made from the same data; the runtime adapters carry the
card to the chat on ``run_events.ToolResult``. Native chat renders it with
assistant-ui's MCP Apps renderer through ``hubzoid.apps.routes``; external
assistants read the same ``ui://`` resources from the hub's MCP server.

Nothing here can approve anything: an approval card links to the work item
page, where the existing approve control checks the person and the digest.
"""
