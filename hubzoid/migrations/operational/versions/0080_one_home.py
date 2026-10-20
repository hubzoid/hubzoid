"""One home for every setting: drop the database copies of files. Revision op_0080.

Brand, chat defaults, diagnostics limits, Workspace drafts and connector
offers now live only in files (proposal 2026-10-19-one-run-mode, section C).
These tables are dropped:

* ``hz_branding_drafts``, ``_reviews``, ``_revisions``, ``_activations``,
  ``_active``, ``_assets`` (each person's ``hz_branding_legal`` and
  ``_dismissals`` stay)
* ``hz_chat_config_heads``, ``_revisions`` (``hz_chat_preferences`` stays)
* ``hz_admin_config_plans``, ``_heads``, ``_revisions``
* ``hz_authoring_drafts``, ``_versions``
* ``hz_connector_agents`` (``hz_connectors`` stays as the cache of clients
  registered with a provider)

Nothing is dropped while it still holds something that has no other home:
if any of these tables (or a connector defined in Settings, or a typed key in
the secret store) has rows and ``hubzoid upgrade`` has not written them out
(the ``one_home_export`` marker in ``hz_meta``), the migration stops and says
to run it. Every table is dropped with IF EXISTS, so 2.0 development databases
and 1.3.2 databases (which never had most of them) reach the same head.
Earlier revisions are left exactly as they were (no revision id is reused).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "op_0080"
down_revision = "op_0071"
branch_labels = None
depends_on = None

DROPPED = (
    "hz_branding_drafts", "hz_branding_reviews", "hz_branding_revisions", "hz_branding_activations",
    "hz_branding_active", "hz_branding_assets",
    "hz_chat_config_heads", "hz_chat_config_revisions",
    "hz_admin_config_plans", "hz_admin_config_heads", "hz_admin_config_revisions",
    "hz_authoring_drafts", "hz_authoring_versions",
    "hz_connector_agents",
)
MARKER = "one_home_export"

#: what must be written out first: (table, a WHERE clause, what it is)
_HELD = (
    ("hz_branding_active", "1=1", "a published brand"),
    ("hz_chat_config_heads", "1=1", "chat defaults set in Settings"),
    ("hz_admin_config_heads", "1=1", "diagnostics limits set in Settings"),
    ("hz_authoring_drafts", "1=1", "Workspace drafts"),
    ("hz_authoring_versions", "1=1", "published Workspace versions"),
    ("hz_connectors", "created_by IS NOT NULL", "connectors defined in Settings"),
    ("hz_secrets", "scope IN ('hub', 'deployment')", "keys typed into Settings"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    exported = False
    if "hz_meta" in tables:
        exported = bool(bind.execute(sa.text("SELECT COUNT(*) FROM hz_meta WHERE k = :k"),
                                     {"k": MARKER}).scalar())
    if not exported:
        held = []
        for table, where, what in _HELD:
            if table in tables and bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar():
                held.append(what)
        if held:
            raise RuntimeError(
                "This deployment keeps " + ", ".join(held) + " in its database, and Hubzoid 2.0 keeps "
                "them in files. Stop it, take a backup (hubzoid backup), then run `hubzoid upgrade` "
                "to write them out, and start it again. See docs/UPGRADING.md.")
    for table in DROPPED:
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError("op_0080 drops tables; restore the backup taken before upgrading instead.")
