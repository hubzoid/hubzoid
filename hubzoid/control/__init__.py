"""Shared plumbing for deployment files that people and agents change through
files, the CLI, the API and the Console: canonical digests, strict YAML,
idempotent writes, capability checks and the deployment folder.

Every 2.0 control-plane area (chat configuration, branding, admin settings,
the parity ledger) uses these pieces instead of inventing its own, so a
revision, a digest, an idempotency key and an error mean the same thing
everywhere (see the 2.0 conventions).
"""
