"""The account tables (created by migration ``op_0009``), for SQLAlchemy Core.

These definitions only describe the tables so queries are portable between
SQLite and PostgreSQL. The migration creates them; nothing here does.
"""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

metadata = sa.MetaData()

users = sa.Table(
    "hz_users", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("email", sa.String(320), nullable=False),
    sa.Column("name", sa.Text),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("password_hash", sa.Text),
    sa.Column("password_enabled", sa.Integer, nullable=False),
    sa.Column("source", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("last_login_at", sa.Float),
)

identities = sa.Table(
    "hz_user_identities", metadata,
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("issuer", sa.String(512), primary_key=True),
    sa.Column("subject", sa.String(255), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("email", sa.String(320)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_login_at", sa.Float),
)

sessions = sa.Table(
    "hz_sessions", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_seen_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("idle_seconds", sa.Integer, nullable=False),
    sa.Column("user_agent", sa.Text),
    sa.Column("ip", sa.String(64)),
    sa.Column("method", sa.String(16)),
    sa.Column("revoked_at", sa.Float),
)

links = sa.Table(
    "hz_auth_links", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("purpose", sa.String(32), nullable=False),
    sa.Column("created_by", sa.String(320)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("used_at", sa.Float),
)

attempts = sa.Table(
    "hz_auth_attempts", metadata,
    sa.Column("key", sa.String(400), primary_key=True),
    sa.Column("window_start", sa.Float, nullable=False),
    sa.Column("failures", sa.Integer, nullable=False),
    sa.Column("locked_until", sa.Float),
)


def ready(engine: Engine) -> Engine:
    """The engine, with the operational schema brought up to date (once per
    process and engine)."""
    from .. import migrations

    migrations.upgrade(engine, "operational")
    return engine


def engine_for(hub_dir: Path) -> Engine:
    """The deployment's shared operational store, migrated."""
    from ..db import operational_engine

    return ready(operational_engine(Path(hub_dir)))
