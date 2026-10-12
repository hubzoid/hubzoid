"""The orders database. Values always go in as parameters, never in the SQL text."""
import sqlite3

from flask import g

DB_PATH = "storefront.db"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db
