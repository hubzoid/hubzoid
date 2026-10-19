"""The chat parity ledger: what Hubzoid's own chat must preserve, each item
with one accountable owner and a runnable acceptance check, and the tooling
that validates it, measures coverage and freezes a reviewed digest.

Files (deployment folder): ``parity.yaml`` (manifest), the obligation ledger
(JSONL), source records (JSON), the provider census (JSONL) and owner-prepared,
redacted deployment inventories (JSON). The validator is pure: it never imports
the sources it describes, never reads ``.env`` or key folders, and never
prints a sensitive value.
"""
