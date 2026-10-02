"""Groups are gone: every grant and share a group held moves to its members.

Each `group:<id>` grant becomes the same grant for each member who is not
blocked, and each artifact shared with a group (by its name) is shared with
each of those members. Artifacts shared with a name that matches no Hubzoid
group (an Open WebUI group) are no longer shared with it. Then the group
tables are dropped. Nothing is lost for a member: they hold directly what they
held through the group.

Revision ID: op_0015
Revises: op_0014
"""
import time

import sqlalchemy as sa
from alembic import op

revision = "op_0015"
down_revision = "op_0014"
branch_labels = None
depends_on = None


def _exists(conn, sql: str, params: dict) -> bool:
    return conn.execute(sa.text(sql), params).first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    now = time.time()
    blocked = {k.split(":", 1)[1] for (k,) in conn.execute(sa.text(
        "SELECT k FROM hz_meta WHERE (k LIKE 'suspended:%' OR k LIKE 'account_unavailable:%') "
        "AND v = '1'"))}
    members: dict[str, set[str]] = {}
    for gid, email in conn.execute(sa.text("SELECT group_id, email FROM hz_group_members")):
        if email not in blocked:
            members.setdefault(gid, set()).add(email)
    by_name: dict[str, set[str]] = {}
    for gid, name in conn.execute(sa.text("SELECT id, name FROM hz_groups")):
        key = " ".join((name or "").split()).lower()
        by_name.setdefault(key, set()).update(members.get(gid, ()))

    grants = conn.execute(sa.text(
        "SELECT subject, hub, permission FROM hz_grants WHERE subject LIKE 'group:%'")).fetchall()
    for subject, hub, permission in grants:
        for email in sorted(members.get(subject[len("group:"):], ())):
            if not _exists(conn, "SELECT 1 FROM hz_identities WHERE subject=:s", {"s": email}):
                conn.execute(sa.text("INSERT INTO hz_identities (subject, email, pending, created) "
                                     "VALUES (:s, :s, 1, :t)"), {"s": email, "t": now})
            if not _exists(conn, "SELECT 1 FROM hz_grants WHERE subject=:s AND hub=:h AND "
                                 "permission=:p", {"s": email, "h": hub, "p": permission}):
                conn.execute(sa.text("INSERT INTO hz_grants (subject, hub, permission, created) "
                                     "VALUES (:s, :h, :p, :t)"),
                             {"s": email, "h": hub, "p": permission, "t": now})
            conn.execute(sa.text(
                "INSERT INTO hz_access_audit (ts, actor, action, subject, hub, permission) "
                "VALUES (:t, 'migration', 'grant_from_group', :s, :h, :p)"),
                {"t": now, "s": email, "h": hub, "p": permission})
        conn.execute(sa.text(
            "INSERT INTO hz_access_audit (ts, actor, action, subject, hub, permission) "
            "VALUES (:t, 'migration', 'revoke', :s, :h, :p)"),
            {"t": now, "s": subject, "h": hub, "p": permission})
    conn.execute(sa.text("DELETE FROM hz_grants WHERE subject LIKE 'group:%'"))

    shares = conn.execute(sa.text(
        "SELECT artifact_id, principal, added_by, added FROM hz_artifact_shares "
        "WHERE kind = 'group'")).fetchall()
    for artifact_id, principal, added_by, added in shares:
        for email in sorted(by_name.get((principal or "").lower(), ())):
            if not _exists(conn, "SELECT 1 FROM hz_artifact_shares WHERE artifact_id=:a AND "
                                 "kind='user' AND principal=:p", {"a": artifact_id, "p": email}):
                conn.execute(sa.text(
                    "INSERT INTO hz_artifact_shares (artifact_id, kind, principal, added_by, added) "
                    "VALUES (:a, 'user', :p, :b, :t)"),
                    {"a": artifact_id, "p": email, "b": added_by, "t": added or now})
    conn.execute(sa.text("DELETE FROM hz_artifact_shares WHERE kind = 'group'"))

    op.drop_table("hz_group_members")
    op.drop_table("hz_groups")
    if grants or shares or members:
        # Running bridges reload the policy on their next decision.
        conn.execute(sa.text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id = 1"))


def downgrade() -> None:
    raise NotImplementedError("Hubzoid migrations are forward-only; restore a backup instead")
