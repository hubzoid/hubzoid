"""Enables `python -m hubzoid …` as an alias for the installed `hubzoid` CLI.

Used by the no-install clone path:
    git clone hubzoid && cd hubzoid && pip install -r requirements.txt
    python -m hubzoid run demo-hub
"""
from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app()
