"""Canonical, unique inbound phone ownership. Revision op_0017."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from hubzoid.inbound.normalize import normalize_phone
from hubzoid.migrations import SchemaError

revision = "op_0017"
down_revision = "op_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    normalized = []
    owners = {}
    for subject, phone in conn.execute(sa.text(
            "SELECT subject, phone FROM hz_identities WHERE phone IS NOT NULL")):
        digits = normalize_phone(phone)
        if str(phone).strip() and not 6 <= len(digits) <= 15:
            raise SchemaError("An existing phone assignment is invalid. In the previous "
                              "version's Console, clear or correct phone numbers before upgrading.")
        if digits in owners and digits:
            raise SchemaError("An existing phone number belongs to more than one person. "
                              "In the previous version's Console, clear or reassign duplicate "
                              "phone numbers before upgrading; no owner was selected.")
        owners[digits] = subject
        normalized.append({"s": subject, "p": digits or None})
    # Validate the complete set before modifying any value (also safe on SQLite).
    for values in normalized:
        conn.execute(sa.text("UPDATE hz_identities SET phone=:p WHERE subject=:s"), values)
    op.create_index("hz_identities_phone_unique", "hz_identities", ["phone"], unique=True)


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
