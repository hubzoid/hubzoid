"""Sandboxes: isolated, bounded workspaces where agents run commands and edit
files, on Docker today. See docs/sandbox.md.

The Agents SDK's sandbox layer (`agents.sandbox`) does the work inside the
container for every runtime. Hubzoid adds the limits, the network phases,
a record per sandbox and the profile files.
"""
