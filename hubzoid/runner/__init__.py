"""Hubzoid on Amazon Bedrock AgentCore Runtime: the runner and its client.

Off unless a hub sets ``HUBZOID_RUNNER=agentcore``. The always-on hub service
keeps sign-in, history, approvals, workflows and the live event stream. Agent
turns that run tools go to an AgentCore runtime (one microVM per chat) running
this same package as ``hubzoid runner serve``:

  * ``protocol``  the request and event format both sides speak
  * ``client``    ``AgentCoreRuntime``, the hub service side (a ``Runtime``)
  * ``server``    the AgentCore HTTP contract inside the microVM
  * ``context``   what a turn's process context holds, carried across
  * ``calls``     work orders: one database record per call, claimed once
  * ``role``      which kind of process this is (hub service, runner, MCP)
  * ``mcp_app``   the hub's MCP server as an AgentCore MCP runtime

Nothing here is imported when the setting is off.
"""
