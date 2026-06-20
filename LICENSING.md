# Licensing

Hubzoid is **open core**.

| Part | Path | License | Use |
|---|---|---|---|
| **Core** | everything except `ee/` | **MIT** (see [`LICENSE`](LICENSE)) | Free for everyone. Use, modify, self-host, redistribute. |
| **Enterprise** | [`ee/`](ee/) | **Hubzoid Enterprise License** (see [`ee/LICENSE`](ee/LICENSE)) | Source-available. Read and modify for dev/testing freely. **Production use needs a paid license key.** |

## How the enterprise tier is gated

Enterprise features check a license key at runtime before they activate. With
no valid key, the runtime stays on the **community** tier and those features
stay off. The core keeps working exactly as before.

- The key is an ED25519-signed token. Verification is **offline** (works
  air-gapped). See [`hubzoid/licensing.py`](hubzoid/licensing.py).
- The **private** signing key lives only with Hubzoid, never in this repo.
- The **public** key is embedded in the shipped code, so anyone can verify a
  key but nobody can forge one.
- A customer sets `LICENSE_KEY=<token>` in their hub's `.env`.

```bash
hubzoid license                 # show the active tier, customer, features, expiry
hubzoid license keygen          # (Hubzoid) generate the signing keypair, once
hubzoid license issue ...       # (Hubzoid) sign a customer key with the private key
hubzoid license verify --key …  # check a token offline
```

## What is in each tier

**Core (MIT):** the markdown-to-agent runtime (both backends), the
OpenAI-compatible API and bridge, loaders, tools, skills, knowledge,
sub-agents, MCP connectors, the Open WebUI integration and white-labeling,
single-hub single-team operation, and baseline security.

**Enterprise (`ee/`):** multi-role and multi-tenant access management
(per-role tool authorization, per-identity data scoping, audit, approvals),
managed and fleet-scale scheduling, the multi-tenant control plane, audit and
cost governance, and SLA monitoring. These are gated by the license key.

## Honest note

The enterprise source is visible, so the key check is a speed bump backed by a
contract, not DRM. The protection is **legal** (the Hubzoid Enterprise License
forbids unlicensed production use) plus **practical** (real buyers will not run
forked, unsupported code). The check only has to make paying the easier path.

The "Hubzoid" name and logo are trademarks of WaveAssist Technologies Pvt Ltd
and are not licensed by either license above.
