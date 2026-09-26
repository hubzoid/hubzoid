"""Connection journeys: a personal link that connects the caller's account for
an app (for example Gmail), confirms the verified result on a browser page and
back in the chat that asked. Pages live under `/portal/connect/`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter


def build_router(hub_dir: Path) -> APIRouter:  # noqa: ARG001
    return APIRouter(prefix="/portal/connect")


def permissions(hub_dir: Path) -> list[dict]:  # noqa: ARG001
    """Connector capabilities (`connector_<app>`) this hub offers."""
    return []
