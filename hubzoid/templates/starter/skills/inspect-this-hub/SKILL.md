---
name: inspect-this-hub
description: Enumerates the contents of the current hub using built-in file tools. A live demo of read_file, list_files, list_skills, list_knowledge.
---

# Inspect this hub

Invoke this skill when the user asks any of:

- "What is in this hub?"
- "Show me your skills."
- "What knowledge do you have?"
- "List the files."

The point of this skill is to demonstrate that a Hubzoid agent can
introspect its own environment using the pre-shipped tools.

## Procedure

1. Call `list_skills()`. Print the names and one-line descriptions.
2. Call `list_knowledge()`. Print the names and one-line descriptions.
3. Call `list_files('agents/**/AGENTS.md')`. Report the sub-agents.
4. Call `list_files('tools_local/*.py')`. Report custom tools, skipping
   any file starting with `_`.
5. Call `list_files('connectors/*.json')`. Report MCP config if present.

## Output format

Reply with one section per category, in this order.

```
**Main agent**
- {name from AGENTS.md frontmatter}

**Sub-agents** ({count})
- {name}: {description}
...

**Skills** ({count})
- {name}: {description}
...

**Knowledge** ({count})
- {name}: {description}
...

**Custom tools** ({count})
- {filename}: {one-line summary if obvious from filename}
...

**MCP connectors** ({count})
- {server name}
```

End with a single line offering a next move:

> "Want me to load any of these for you? Just ask."

## Constraints

- Do not fabricate. If a folder is empty, say so.
- Do not read every file. Use the list tools. The user wants a directory,
  not a dump.
- Skip files starting with `.` and `_`.

## Acceptance criteria

A successful run lists every skill, knowledge file, sub-agent, custom
tool, and MCP server in the current hub. No invented entries. No
omissions.
