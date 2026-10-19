# Settings and diagnostics in the Console

Console → Settings lists every settings area with its file, owner and the
command that changes it (Chat, Appearance, Admin, Parity ledger). Console →
Diagnostics runs read-only checks.

## admin.yaml

```yaml
version: 1
diagnostics:
  check_timeout_seconds: 10    # 1 to 30
  max_parallel_checks: 4       # 1 to 8
  bundle_ttl_minutes: 60       # 5 to 1440
  allow_network_probes: false
```

Changes go through a plan, applied by its id and digest:

```bash
hubzoid admin config validate . --file admin.yaml --json
hubzoid admin config plan . --file admin.yaml --json        # plan id, digest, diff
hubzoid admin config apply . --plan <id> --digest <digest> --expected-revision <revision> --json
hubzoid admin config show . --json
hubzoid admin domain show . --json                          # every settings area
```

## Diagnostics

```bash
hubzoid admin diagnostics checks . --json
hubzoid admin diagnostics run . --wait --json               # passive checks
hubzoid admin diagnostics run . --profile active --consent-network --wait   # needs allow_network_probes
hubzoid admin diagnostics show <report> . --json
hubzoid admin diagnostics bundle <report> . --out ./support.zip
```

Passive checks read metadata only: storage and schema, disk space, model
sign-in readiness, sign-in setup, listening address, administrators,
schedules, connectors, telemetry, `chat/config.yaml` and the active brand.
They never call a model or show a secret value. Each check ends `ok`, `info`,
`warn`, `fail`, `skipped` or `timeout` with the next step. A report whose
worker stopped is marked `interrupted` and is not rerun; run it again.

Reports are kept until you delete them. A support bundle is a zip of `manifest.json`, `checks.json`, `versions.json`
and `config-provenance.json`. It has no chat content, prompts, files,
environment or secrets. Its download link expires after `bundle_ttl_minutes`;
the bundle itself is kept. Making a bundle again for the same report renews
the link. Delete a bundle with `hubzoid admin diagnostics bundle-delete <id>`
or `DELETE /api/admin/diagnostics/bundles/<id>`.

Monitoring commands print JSON with `--json`: `schedule list`, `schedule
status`, `schedule deliveries`, `access check`, `access list`, `admin list`,
`admin overview` and `test`.

Hosted MCP adds `agent_health`, `eval_status` and `recent_denials` for people
who manage the agent. Others don't see them.
