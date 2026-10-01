"""Hubzoid's own engines use psycopg 3 for plain PostgreSQL URLs (hubzoid.db)."""
from __future__ import annotations

from hubzoid import db


def test_plain_postgresql_urls_use_psycopg():
    assert db.sqlalchemy_url("postgresql://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert db.sqlalchemy_url("postgres://u@h/d") == "postgresql+psycopg://u@h/d"


def test_explicit_drivers_and_sqlite_are_unchanged():
    for url in ("postgresql+psycopg://u@h/d", "postgresql+psycopg2://u@h/d", "sqlite:////tmp/x.db"):
        assert db.sqlalchemy_url(url) == url


def test_engine_for_plain_url_needs_no_psycopg2():
    eng = db._engine_for_url("postgresql://hz@127.0.0.1:1/never")  # no connection is made
    assert eng.dialect.driver == "psycopg"
