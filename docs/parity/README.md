# Chat parity ledger

What Hubzoid's own chat must preserve from the Open WebUI reference and the
selected LibreChat features. One obligation is one testable outcome with one
owning issue, its source locations, a migration treatment and an acceptance
case. Browser, provider and runtime variants are cases, not extra features.

| File | Contents |
|---|---|
| `parity.yaml` | The manifest |
| `obligations.jsonl` | The ledger (`HZP-` ids) |
| `sources.json` | Inspected references, file hashes and every source locator |
| `providers.jsonl` | Provider census (one or two key providers per category) |
| `input/dep-example.json` | A synthetic, redacted deployment inventory |

```bash
hubzoid parity validate . --file docs/parity/parity.yaml --json
hubzoid parity coverage . --file docs/parity/parity.yaml --json
hubzoid parity ineffective . --file docs/parity/parity.yaml --json
hubzoid parity candidate . --file docs/parity/parity.yaml --json
hubzoid parity review <candidate> . --digest <digest> --decision approve --expected-revision 1
hubzoid parity freeze <candidate> . --digest <digest> --review <review id> --expected-revision 2 \
  --source-root <folder with the inspected reference files>
```

Every review moves the candidate's revision, so only the latest review counts:
an approval followed by a rejection can't freeze. A freeze rechecks the stored
bundle and re-hashes every recorded reference file (from `--source-root`, or
`source_root` in `parity.yaml`); a source marked `upstream_verified` needs a
40-character commit or a `sha256:` archive digest plus file hashes. Setting
values in inventories are plain values only: URLs with user names, passwords
or tokens are refused.

`validate` exits 6 while evidence is still missing. Owners are proposed from
each row's section and wording and need owner review. A freeze fixes the
requirements; it is never a claim that the product passed them. Real
deployment inventories are prepared by their owners, redacted, and never
committed here.
