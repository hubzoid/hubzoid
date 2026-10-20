# Connector setup from Settings and files

Requested by the owner on 21 October 2026: restore the connector UI while
keeping deployment YAML as the source of truth. This request authorizes
implementation.

Restore the existing catalog, Add connector, edit, test, enable, offer,
withdraw and delete controls. They use the existing administrator-only,
same-origin API. API writes persist in `connectors/<id>.yaml` beside
`hubzoid.yaml`; manual file edits remain supported. Hub-owned `.mcp.json`
servers remain a separate read-only list.

Secret values entered in Settings go into the deployment `.env`, with
references in YAML. Neither listings nor errors expose secret values.
Creating and offering a connector should be one file write. Validate before
writing, reject duplicate IDs (including `.yml` files), reject symlinks, use
atomic replacement and a deployment write lock, and reject stale edits from
the UI. Keep valid manual fields; invalid files must be repaired explicitly
rather than silently overwritten.

Preserve existing access rules and connection cleanup when a server changes
or is deleted. Offering a connector does not grant anybody access.
No production deployment or real provider connection is part of this change.

UI saves may normalize YAML formatting and comments; say so in the editor.
Environment files retain unrelated content. Replacing a secret adds a new,
unique environment reference, so running processes do not retain an old value.
Unused old values remain for operator cleanup rather than deleting a value
that another file may still reference.
