# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Schema for the access store's tables in the shared operational database.

All tables are `hz_`-prefixed (we never touch Open WebUI's schema). They are
created and upgraded by the versioned migrations in `hubzoid.migrations`
(the `operational` store); see `migrations/operational/versions/`.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine


def ensure_access_tables(engine: Engine) -> None:
    """Bring the operational tables to the current schema (once per process)."""
    from ..migrations import upgrade

    upgrade(engine, "operational")
